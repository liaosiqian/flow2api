"""Async task manager for non-blocking generation via ?async=true."""

import asyncio
import json
import random
import re
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..core.logger import debug_logger
from ..core.models import RequestLog
from ..core.monitoring import record_generation_result
from ..core.account_tiers import (
    normalize_user_paygate_tier,
    supports_model_for_tier,
)
from .generation_handler import MODEL_CONFIG


TASK_EXPIRY_HOURS = 2
CLEANUP_INTERVAL_SECONDS = 300
MAX_QUEUE_DEPTH = 10

VIDEO_TYPE_MAP = {
    "t2v": "video_text",
    "i2v": "video_start_image",
    "r2v": "video_reference_images",
    "extend": "video_extend",
}

MARKDOWN_IMAGE_RE = re.compile(r"!\[.*?\]\((.*?)\)")
HTML_VIDEO_RE = re.compile(r"<video[^>]+src=['\"](.*?)['\"]", re.IGNORECASE)


@dataclass
class AsyncTask:
    task_id: str
    type: str
    status: str = "submitted"
    progress: int = 0
    token_id: Optional[int] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: datetime = field(default_factory=lambda: datetime.utcnow() + timedelta(hours=TASK_EXPIRY_HOURS))

    def to_submit_response(self) -> Dict[str, Any]:
        return {
            "success": True,
            "task_id": self.task_id,
            "type": self.type,
            "status": self.status,
            "created_at": self.created_at.isoformat() + "Z",
        }

    def to_status_response(self) -> Dict[str, Any]:
        resp: Dict[str, Any] = {
            "success": True,
            "task_id": self.task_id,
            "type": self.type,
            "status": self.status,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at.isoformat() + "Z",
            "updated_at": self.updated_at.isoformat() + "Z",
        }
        return resp

    def to_list_item(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "type": self.type,
            "status": self.status,
            "created_at": self.created_at.isoformat() + "Z",
        }


@dataclass
class MediaContext:
    media_id: str
    token_id: int
    project_id: str
    session_id: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)


class QueueFullError(Exception):
    pass


class MediaIdExpiredError(Exception):
    pass


def _generate_task_id() -> str:
    ts = int(time.time() * 1000)
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"gen_{ts}_{suffix}"


def infer_task_type(model: str, model_config: Dict[str, Any]) -> str:
    gen_type = model_config.get("type", "")
    if gen_type == "image":
        return "image_generate"
    video_type = model_config.get("video_type", "")
    return VIDEO_TYPE_MAP.get(video_type, "video_start_image")


class AsyncTaskManager:
    """Manages in-memory async generation tasks and media_id→token mappings."""

    def __init__(self, generation_handler, flow_client, token_manager, load_balancer, db):
        self._tasks: Dict[str, AsyncTask] = {}
        self._media_map: Dict[str, MediaContext] = {}
        self._handler = generation_handler
        self._flow_client = flow_client
        self._token_manager = token_manager
        self._load_balancer = load_balancer
        self._db = db
        self._cleanup_handle: Optional[asyncio.Task] = None

    # ==================== Lifecycle ====================

    async def start(self):
        self._cleanup_handle = asyncio.create_task(self._cleanup_loop())
        debug_logger.log_info("[ASYNC] Task manager started")

    async def stop(self):
        if self._cleanup_handle:
            self._cleanup_handle.cancel()
            try:
                await self._cleanup_handle
            except asyncio.CancelledError:
                pass
        debug_logger.log_info("[ASYNC] Task manager stopped")

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            self._cleanup_expired()

    def _cleanup_expired(self):
        now = datetime.utcnow()
        expired = [tid for tid, t in self._tasks.items() if now > t.expires_at]
        for tid in expired:
            del self._tasks[tid]
        expired_media = [
            mid for mid, ctx in self._media_map.items()
            if now - ctx.created_at > timedelta(hours=TASK_EXPIRY_HOURS)
        ]
        for mid in expired_media:
            del self._media_map[mid]
        if expired or expired_media:
            debug_logger.log_info(
                f"[ASYNC] Cleanup: {len(expired)} tasks, {len(expired_media)} media mappings expired"
            )

    # ==================== Queue Management ====================

    def get_active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status in ("submitted", "processing"))

    def submit(self, task_type: str) -> AsyncTask:
        active = self.get_active_count()
        if active >= MAX_QUEUE_DEPTH:
            raise QueueFullError(f"Queue full ({active}/{MAX_QUEUE_DEPTH} tasks)")
        task = AsyncTask(task_id=_generate_task_id(), type=task_type)
        self._tasks[task.task_id] = task
        debug_logger.log_info(f"[ASYNC] Task submitted: {task.task_id} type={task_type}")
        return task

    def get_task(self, task_id: str) -> Optional[AsyncTask]:
        return self._tasks.get(task_id)

    def list_tasks(self, limit: int = 20) -> List[AsyncTask]:
        sorted_tasks = sorted(
            self._tasks.values(), key=lambda t: t.created_at, reverse=True
        )
        return sorted_tasks[:limit]

    # ==================== Media Map ====================

    def register_media(self, media_id: str, token_id: int, project_id: str, session_id: Optional[str] = None):
        self._media_map[media_id] = MediaContext(
            media_id=media_id,
            token_id=token_id,
            project_id=project_id,
            session_id=session_id,
        )

    def get_media_context(self, media_id: str) -> Optional[MediaContext]:
        ctx = self._media_map.get(media_id)
        if ctx and datetime.utcnow() - ctx.created_at > timedelta(hours=TASK_EXPIRY_HOURS):
            del self._media_map[media_id]
            return None
        return ctx

    # ==================== Background Execution ====================

    def start_execution(
        self,
        task: AsyncTask,
        model: str,
        prompt: str,
        images: Optional[List[bytes]] = None,
        base_url_override: Optional[str] = None,
        video_media_id: Optional[str] = None,
        image_count: int = 1,
    ):
        asyncio.create_task(
            self._execute(task, model, prompt, images, base_url_override, video_media_id, image_count)
        )

    async def _execute(
        self,
        task: AsyncTask,
        model: str,
        prompt: str,
        images: Optional[List[bytes]],
        base_url_override: Optional[str],
        video_media_id: Optional[str],
        image_count: int,
    ):
        start_time = time.time()
        log_id = None
        model_config = None
        operation = "generate_unknown"
        try:
            task.status = "processing"
            task.progress = 5
            task.updated_at = datetime.utcnow()

            model_config = MODEL_CONFIG.get(model)
            if not model_config:
                raise ValueError(f"Unknown model: {model}")

            gen_type = model_config["type"]
            operation = f"generate_{gen_type}"

            request_payload = {
                "model": model,
                "prompt": prompt[:2000],
                "has_images": images is not None and len(images) > 0,
                "image_count": image_count if gen_type == "image" else None,
                "task_id": task.task_id,
            }

            log_id = await self._write_request_log(
                token_id=None, operation=operation,
                status_code=102, duration=0,
                status_text="started", progress=5,
                request_data=request_payload,
                response_data={"status": "processing", "task_id": task.task_id},
            )

            token = await self._select_and_prepare_token(model, gen_type)
            task.token_id = token.id
            task.progress = 15
            task.updated_at = datetime.utcnow()

            project_id = await self._token_manager.ensure_project_exists(token.id)
            task.progress = 22
            task.updated_at = datetime.utcnow()

            if gen_type == "image":
                await self._execute_image(task, token, project_id, model, model_config, prompt, images, base_url_override, image_count)
            else:
                await self._execute_video(task, token, project_id, model, model_config, prompt, images, base_url_override, video_media_id)

            duration = time.time() - start_time
            is_video = gen_type == "video"
            await self._token_manager.record_usage(token.id, is_video=is_video)
            await self._token_manager.record_success(token.id)
            record_generation_result(gen_type, "success", duration)

            response_data = {
                "status": "success",
                "model": model,
                "prompt": prompt[:500],
                "performance": {"total_ms": int(duration * 1000)},
            }
            if task.result:
                if task.result.get("images"):
                    response_data["urls"] = [img.get("url", "") for img in task.result["images"]]
                elif task.result.get("video_url"):
                    response_data["url"] = task.result["video_url"]

            await self._write_request_log(
                token_id=token.id, operation=operation,
                status_code=200, duration=duration,
                status_text="completed", progress=100,
                request_data=request_payload,
                response_data=response_data,
                log_id=log_id,
            )

            debug_logger.log_info(
                f"[ASYNC] Task {task.task_id} completed in {duration:.1f}s"
            )

        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)[:1000]
            task.updated_at = datetime.utcnow()
            duration = time.time() - start_time
            record_generation_result(
                model_config["type"] if model_config else "unknown", "failed", duration
            )
            if task.token_id:
                try:
                    await self._token_manager.record_error(task.token_id)
                except Exception:
                    pass

            request_payload_fallback = {
                "model": model,
                "prompt": prompt[:2000],
                "has_images": images is not None and len(images) > 0,
                "task_id": task.task_id,
            }

            await self._write_request_log(
                token_id=task.token_id, operation=operation,
                status_code=500, duration=duration,
                status_text="failed", progress=task.progress,
                request_data=request_payload if 'request_payload' in locals() else request_payload_fallback,
                response_data={"error": str(exc)[:500], "performance": {"total_ms": int(duration * 1000)}},
                log_id=log_id,
            )

            debug_logger.log_error(
                f"[ASYNC] Task {task.task_id} failed after {duration:.1f}s: {exc}"
            )

    async def _write_request_log(
        self,
        token_id: Optional[int],
        operation: str,
        status_code: int,
        duration: float,
        status_text: str,
        progress: int,
        request_data: Dict[str, Any],
        response_data: Dict[str, Any],
        log_id: Optional[int] = None,
    ) -> Optional[int]:
        try:
            request_body = json.dumps(request_data, ensure_ascii=False)
            response_body = json.dumps(response_data, ensure_ascii=False)

            if log_id:
                await self._db.update_request_log(
                    log_id,
                    token_id=token_id,
                    operation=operation,
                    request_body=request_body,
                    response_body=response_body,
                    status_code=status_code,
                    duration=duration,
                    status_text=status_text,
                    progress=max(0, min(100, int(progress))),
                )
                return log_id

            log = RequestLog(
                token_id=token_id,
                operation=operation,
                request_body=request_body,
                response_body=response_body,
                status_code=status_code,
                duration=duration,
                status_text=status_text,
                progress=max(0, min(100, int(progress))),
            )
            return await self._db.add_request_log(log)
        except Exception as e:
            debug_logger.log_error(f"[ASYNC] Failed to write request log: {e}")
            return None

    async def _select_and_prepare_token(self, model: str, gen_type: str):
        is_image = gen_type == "image"
        token = await self._load_balancer.select_token(
            for_image_generation=is_image,
            for_video_generation=not is_image,
            model=model,
            reserve=False,
            enforce_concurrency_filter=False,
            track_pending=True,
        )
        if not token:
            raise RuntimeError("No available token")

        token = await self._token_manager.ensure_valid_token(token)
        if not token:
            raise RuntimeError("Token AT invalid or refresh failed")

        if not supports_model_for_tier(model, token.user_paygate_tier):
            raise RuntimeError(f"Model {model} requires higher tier account")

        return token

    # ==================== Image Execution ====================

    async def _execute_image(
        self, task, token, project_id, model, model_config, prompt, images, base_url_override, image_count
    ):
        normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)

        image_inputs = []
        if images:
            for image_bytes in images:
                media_id = await self._flow_client.upload_image(
                    token.at, image_bytes, model_config["aspect_ratio"], project_id=project_id
                )
                image_inputs.append({
                    "name": media_id,
                    "imageInputType": "IMAGE_INPUT_TYPE_REFERENCE",
                })

        task.progress = 35
        task.updated_at = datetime.utcnow()

        async def _progress_cb(status_text: str, progress: int):
            task.progress = min(progress, 70)
            task.updated_at = datetime.utcnow()

        result, session_id, _ = await self._flow_client.generate_image(
            at=token.at,
            project_id=project_id,
            prompt=prompt,
            model_name=model_config["model_name"],
            aspect_ratio=model_config["aspect_ratio"],
            image_inputs=image_inputs,
            image_count=image_count,
            token_id=token.id,
            token_image_concurrency=token.image_concurrency,
            progress_callback=_progress_cb,
        )

        task.progress = 72
        task.updated_at = datetime.utcnow()

        media = result.get("media", [])
        if not media:
            raise RuntimeError("Empty generation result from Google")

        result_images = []
        for item in media:
            fife_url = item.get("image", {}).get("generatedImage", {}).get("fifeUrl")
            item_media_id = item.get("name")
            mime_type = item.get("image", {}).get("generatedImage", {}).get("mimeType", "image/png")
            if fife_url:
                result_images.append({
                    "url": fife_url,
                    "media_id": item_media_id,
                    "mime_type": mime_type,
                })
                if item_media_id:
                    self.register_media(item_media_id, token.id, project_id, session_id)

        upsample_resolution = model_config.get("upsample")
        if upsample_resolution and media[0].get("name"):
            task.progress = 80
            task.updated_at = datetime.utcnow()
            resolution_name = "4K" if "4K" in upsample_resolution else "2K"
            for idx, item in enumerate(media):
                media_id = item.get("name")
                if not media_id or idx >= len(result_images):
                    continue
                try:
                    encoded_image = await self._flow_client.upsample_image(
                        at=token.at,
                        project_id=project_id,
                        media_id=media_id,
                        target_resolution=upsample_resolution,
                        user_paygate_tier=normalized_tier,
                        session_id=session_id,
                        token_id=token.id,
                    )
                    if encoded_image:
                        cached_filename = await self._handler.file_cache.cache_base64_image(
                            encoded_image, resolution_name
                        )
                        base_url = (base_url_override or "").strip().rstrip("/") or ""
                        local_url = f"{base_url}/tmp/{cached_filename}" if base_url else f"/tmp/{cached_filename}"
                        result_images[idx] = {
                            **result_images[idx],
                            "url": local_url,
                            "upsampled": True,
                            "resolution": resolution_name,
                        }
                except Exception as exc:
                    debug_logger.log_warning(f"[ASYNC] Auto-upsample failed: {exc}")

        task.status = "done"
        task.progress = 100
        task.result = {"images": result_images}
        task.updated_at = datetime.utcnow()

    # ==================== Video Execution ====================

    async def _execute_video(
        self, task, token, project_id, model, model_config, prompt, images, base_url_override, video_media_id
    ):
        from ..core.config import config as app_config

        normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)
        video_type = model_config.get("video_type", "i2v")
        model_key = model_config["model_key"]

        mc_copy = dict(model_config)
        resolved_key, _ = self._handler._resolve_video_model_key_for_tier(mc_copy, normalized_tier)
        mc_copy["model_key"] = resolved_key

        start_media_id = None
        end_media_id = None
        reference_images = []

        aspect_ratio = mc_copy.get("aspect_ratio", "VIDEO_ASPECT_RATIO_LANDSCAPE")

        if video_type == "t2v":
            images = None
        elif video_type == "i2v" and images:
            start_media_id = await self._flow_client.upload_image(
                token.at, images[0], aspect_ratio, project_id=project_id
            )
            if len(images) >= 2:
                end_media_id = await self._flow_client.upload_image(
                    token.at, images[1], aspect_ratio, project_id=project_id
                )
        elif video_type == "r2v" and images:
            for img in images:
                mid = await self._flow_client.upload_image(
                    token.at, img, aspect_ratio, project_id=project_id
                )
                reference_images.append({
                    "imageUsageType": "IMAGE_USAGE_TYPE_ASSET",
                    "mediaId": mid,
                })

        task.progress = 30
        task.updated_at = datetime.utcnow()

        base_kwargs: Dict[str, Any] = {
            "at": token.at,
            "project_id": project_id,
            "prompt": prompt,
            "model_key": resolved_key,
            "aspect_ratio": aspect_ratio,
            "user_paygate_tier": normalized_tier,
            "token_id": token.id,
            "token_video_concurrency": getattr(token, "video_concurrency", -1),
        }
        use_v2 = mc_copy.get("use_v2_model_config", False)

        if video_type == "t2v":
            result = await self._flow_client.generate_video_text(
                **base_kwargs, use_v2_model_config=use_v2,
            )
        elif video_type == "i2v":
            if end_media_id:
                result = await self._flow_client.generate_video_start_end(
                    **base_kwargs, use_v2_model_config=use_v2,
                    start_media_id=start_media_id, end_media_id=end_media_id,
                )
            else:
                result = await self._flow_client.generate_video_start_image(
                    **base_kwargs, use_v2_model_config=use_v2,
                    start_media_id=start_media_id,
                )
        elif video_type == "r2v":
            result = await self._flow_client.generate_video_reference_images(
                **base_kwargs, reference_images=reference_images,
            )
        elif video_type == "extend":
            if not video_media_id:
                raise ValueError("video_media_id required for extend mode")
            result = await self._flow_client.generate_video_extend(
                **base_kwargs,
                video_media_id=video_media_id,
            )
        else:
            raise ValueError(f"Unknown video_type: {video_type}")

        operations = result.get("operations", [])
        if not operations:
            raise RuntimeError("Video task creation failed: no operations returned")

        task.progress = 40
        task.updated_at = datetime.utcnow()

        poll_interval = getattr(app_config, "poll_interval", 3)
        max_polls = getattr(app_config, "max_poll_attempts", 500)
        consecutive_errors = 0

        for poll_count in range(max_polls):
            await asyncio.sleep(poll_interval)

            progress_pct = 40 + int((poll_count / max_polls) * 55)
            task.progress = min(progress_pct, 95)
            task.updated_at = datetime.utcnow()

            try:
                status_result = await self._flow_client.check_video_status(
                    at=token.at,
                    operations=operations,
                    token_id=token.id,
                )
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                debug_logger.log_warning(f"[ASYNC] Video poll error #{consecutive_errors}: {exc}")
                if consecutive_errors >= 3:
                    raise RuntimeError(f"Video polling failed after 3 consecutive errors: {exc}")
                continue

            checked_ops = status_result.get("operations", [])
            if not checked_ops:
                continue

            op = checked_ops[0]
            status = op.get("status", "")

            if status == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
                metadata = op.get("operation", {}).get("metadata", {})
                video_info = metadata.get("video", {})
                video_url = video_info.get("fifeUrl")

                if not video_url:
                    raise RuntimeError("Video completed but no URL in result")

                task.status = "done"
                task.progress = 100
                task.result = {
                    "video_url": video_url,
                    "duration_seconds": 8,
                }
                task.updated_at = datetime.utcnow()
                return

            if status == "MEDIA_GENERATION_STATUS_FAILED":
                error_msg = op.get("operation", {}).get("error", {}).get("message", "Video generation failed")
                raise RuntimeError(f"Video generation failed: {error_msg}")

        raise RuntimeError(f"Video generation timed out after {max_polls * poll_interval}s")

    # ==================== Image Upsample Execution ====================

    async def execute_upsample(
        self,
        task: AsyncTask,
        media_id: str,
        target_resolution: str = "UPSAMPLE_IMAGE_RESOLUTION_4K",
        base_url_override: Optional[str] = None,
    ):
        asyncio.create_task(
            self._execute_upsample_bg(task, media_id, target_resolution, base_url_override)
        )

    async def _execute_upsample_bg(
        self, task, media_id, target_resolution, base_url_override
    ):
        start_time = time.time()
        log_id = None
        operation = "upsample_image"
        resolution_name = "4K" if "4K" in target_resolution else "2K"
        request_payload = {
            "media_id": media_id,
            "target_resolution": target_resolution,
            "resolution_name": resolution_name,
            "task_id": task.task_id,
        }

        try:
            task.status = "processing"
            task.progress = 10
            task.updated_at = datetime.utcnow()

            log_id = await self._write_request_log(
                token_id=None, operation=operation,
                status_code=102, duration=0,
                status_text="started", progress=5,
                request_data=request_payload,
                response_data={"status": "processing", "task_id": task.task_id},
            )

            ctx = self.get_media_context(media_id)
            if not ctx:
                raise MediaIdExpiredError(
                    "media_id expired or unknown, please regenerate the image first"
                )

            token = await self._token_manager.get_token(ctx.token_id)
            if not token:
                raise RuntimeError(f"Token {ctx.token_id} not found")

            token = await self._token_manager.ensure_valid_token(token)
            if not token:
                raise RuntimeError("Token AT invalid or refresh failed")

            task.token_id = token.id
            task.progress = 30
            task.updated_at = datetime.utcnow()

            normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)

            encoded_image = await self._flow_client.upsample_image(
                at=token.at,
                project_id=ctx.project_id,
                media_id=media_id,
                target_resolution=target_resolution,
                user_paygate_tier=normalized_tier,
                session_id=ctx.session_id,
                token_id=token.id,
            )

            task.progress = 80
            task.updated_at = datetime.utcnow()

            if not encoded_image:
                raise RuntimeError("Upsample returned empty result")

            cached_filename = await self._handler.file_cache.cache_base64_image(
                encoded_image, resolution_name
            )
            base_url = (base_url_override or "").strip().rstrip("/") or ""
            local_url = f"{base_url}/tmp/{cached_filename}" if base_url else f"/tmp/{cached_filename}"

            task.status = "done"
            task.progress = 100
            task.result = {
                "upsampled_image_url": local_url,
                "resolution": resolution_name,
            }
            task.updated_at = datetime.utcnow()

            duration = time.time() - start_time
            record_generation_result("image", "success", duration)
            debug_logger.log_info(
                f"[ASYNC] Upsample {task.task_id} completed in {duration:.1f}s"
            )

            await self._write_request_log(
                token_id=token.id, operation=operation,
                status_code=200, duration=duration,
                status_text="completed", progress=100,
                request_data=request_payload,
                response_data={
                    "status": "success",
                    "media_id": media_id,
                    "resolution": resolution_name,
                    "upsampled_url": local_url,
                    "performance": {"total_ms": int(duration * 1000)},
                },
                log_id=log_id,
            )

        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)[:1000]
            task.updated_at = datetime.utcnow()
            duration = time.time() - start_time
            record_generation_result("image", "failed", duration)
            debug_logger.log_error(
                f"[ASYNC] Upsample {task.task_id} failed: {exc}"
            )

            await self._write_request_log(
                token_id=task.token_id, operation=operation,
                status_code=500, duration=duration,
                status_text="failed", progress=task.progress,
                request_data=request_payload,
                response_data={
                    "error": str(exc)[:500],
                    "media_id": media_id,
                    "resolution": resolution_name,
                    "performance": {"total_ms": int(duration * 1000)},
                },
                log_id=log_id,
            )

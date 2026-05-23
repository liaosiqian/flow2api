import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from fastapi import WebSocket

from ..core.logger import debug_logger


@dataclass
class ExtensionConnection:
    websocket: WebSocket
    route_key: str = ""
    client_label: str = ""
    connected_at: float = field(default_factory=time.time)


class ExtensionCaptchaService:
    _instance: Optional["ExtensionCaptchaService"] = None
    _lock = asyncio.Lock()

    def __init__(self, db=None):
        self.db = db
        self.active_connections: list[ExtensionConnection] = []
        self.pending_requests: dict[str, tuple[asyncio.Future, WebSocket]] = {}
        self.pending_generation_requests: dict[str, tuple[asyncio.Future, WebSocket]] = {}

    @classmethod
    async def get_instance(cls, db=None) -> "ExtensionCaptchaService":
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db=db)
        elif db is not None and cls._instance.db is None:
            cls._instance.db = db
        return cls._instance

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        conn = ExtensionConnection(
            websocket=websocket,
            route_key=(websocket.query_params.get("route_key") or "").strip(),
            client_label=(websocket.query_params.get("client_label") or "").strip(),
        )
        self.active_connections.append(conn)
        debug_logger.log_info(
            f"[Extension Captcha] Client connected. Total: {len(self.active_connections)}, "
            f"route_key={conn.route_key or '-'}, label={conn.client_label or '-'}"
        )

    def disconnect(self, websocket: WebSocket):
        for conn in list(self.active_connections):
            if conn.websocket is websocket:
                self.active_connections.remove(conn)
                debug_logger.log_info(
                    f"[Extension Captcha] Client disconnected. Total: {len(self.active_connections)}, "
                    f"route_key={conn.route_key or '-'}, label={conn.client_label or '-'}"
                )
                return

    def _find_connection(self, websocket: WebSocket) -> Optional[ExtensionConnection]:
        for conn in self.active_connections:
            if conn.websocket is websocket:
                return conn
        return None

    def _select_connection(self, route_key: str) -> Optional[ExtensionConnection]:
        normalized_key = (route_key or "").strip()
        if normalized_key:
            for conn in self.active_connections:
                if conn.route_key == normalized_key:
                    return conn
            return None
        # Empty token routes are only allowed to use an empty extension route.
        # A keyed route such as "9223" belongs to a specific browser/account
        # and must never be borrowed by another token just because it is the
        # only extension online.
        for conn in self.active_connections:
            if not conn.route_key:
                return conn
        return None

    def _describe_routes(self) -> str:
        labels = []
        for conn in self.active_connections:
            label = conn.route_key or "(empty)"
            if conn.client_label:
                label = f"{label}:{conn.client_label}"
            labels.append(label)
        return ", ".join(labels)

    def describe_routes(self) -> str:
        return self._describe_routes()

    async def _send_ack(self, websocket: WebSocket, payload: Dict[str, Any]):
        try:
            await websocket.send_text(json.dumps(payload))
        except Exception:
            pass

    async def _resolve_route_key(self, token_id: Optional[int]) -> str:
        if not token_id or not self.db:
            return ""
        try:
            token = await self.db.get_token(token_id)
            if token and token.extension_route_key:
                return token.extension_route_key.strip()
        except Exception as e:
            debug_logger.log_warning(f"[Extension Captcha] Failed to resolve route key for token {token_id}: {e}")
        return ""

    def _has_connection_for_route_key(self, route_key: str) -> bool:
        return self._select_connection(route_key) is not None

    async def has_connection_for_token(self, token_id: Optional[int]) -> tuple[bool, str]:
        route_key = await self._resolve_route_key(token_id)
        return self._has_connection_for_route_key(route_key), route_key

    async def handle_message(self, websocket: WebSocket, data: str):
        try:
            payload = json.loads(data)
            message_type = payload.get("type")

            if message_type == "register":
                conn = self._find_connection(websocket)
                if conn:
                    conn.route_key = (payload.get("route_key") or conn.route_key or "").strip()
                    conn.client_label = (payload.get("client_label") or conn.client_label or "").strip()
                    debug_logger.log_info(
                        f"[Extension Captcha] Client registered route_key={conn.route_key or '-'}, "
                        f"label={conn.client_label or '-'}"
                    )
                    await self._send_ack(
                        websocket,
                        {
                            "type": "register_ack",
                            "route_key": conn.route_key,
                            "client_label": conn.client_label,
                        },
                    )
                return

            req_id = payload.get("req_id")
            if req_id and req_id in self.pending_requests:
                future, owner_websocket = self.pending_requests[req_id]
                if websocket is not owner_websocket:
                    debug_logger.log_warning(f"[Extension Captcha] Ignoring response from non-owner connection: {req_id}")
                    return
                if not future.done():
                    future.set_result(payload)
                return
            if req_id and req_id in self.pending_generation_requests:
                future, owner_websocket = self.pending_generation_requests[req_id]
                if websocket is not owner_websocket:
                    debug_logger.log_warning(f"[Extension Captcha] Ignoring generation response from non-owner: {req_id}")
                    return
                if not future.done():
                    future.set_result(payload)
                return
        except Exception as e:
            debug_logger.log_error(f"[Extension Captcha] Error handling message: {e}")

    async def get_token(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        timeout: int = 20,
        token_id: Optional[int] = None,
    ) -> Optional[str]:
        if not self.active_connections:
            debug_logger.log_warning("[Extension Captcha] No active extension connections available.")
            raise RuntimeError("Chrome Extension not connected or Google Labs tab not open.")

        route_key = await self._resolve_route_key(token_id)
        conn = self._select_connection(route_key)
        if conn is None:
            available = self._describe_routes() or "none"
            raise RuntimeError(
                f"No Chrome Extension connection matches token_id={token_id} route_key='{route_key}'. "
                f"Available route keys: {available}"
            )

        req_id = f"req_{uuid.uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self.pending_requests[req_id] = (future, conn.websocket)

        request_data = {
            "type": "get_token",
            "req_id": req_id,
            "action": action,
            "project_id": project_id,
            "route_key": route_key,
        }

        try:
            debug_logger.log_info(
                f"[Extension Captcha] Dispatching token request via route_key={route_key or '-'}, "
                f"label={conn.client_label or '-'}, project_id={project_id}, action={action}"
            )
            await conn.websocket.send_text(json.dumps(request_data))
            result = await asyncio.wait_for(future, timeout=timeout)

            if result.get("status") == "success":
                return result.get("token")

            error_msg = result.get("error")
            debug_logger.log_error(f"[Extension Captcha] Error from extension: {error_msg}")
            return None

        except asyncio.TimeoutError:
            debug_logger.log_error(f"[Extension Captcha] Timeout waiting for token (req_id: {req_id})")
            return None
        except Exception as e:
            debug_logger.log_error(f"[Extension Captcha] Communication error: {e}")
            return None
        finally:
            self.pending_requests.pop(req_id, None)

    async def refresh_session_naturally(
        self,
        timeout: int = 70,
        token_id: Optional[int] = None,
        active: bool = False,
        keep_open: bool = False,
    ) -> Optional[str]:
        if not self.active_connections:
            debug_logger.log_warning("[Extension Captcha] No connections for natural Labs refresh.")
            return None

        route_key = await self._resolve_route_key(token_id)
        conn = self._select_connection(route_key)
        if conn is None:
            debug_logger.log_warning(
                f"[Extension Captcha] No connection for natural refresh token_id={token_id} route_key='{route_key}'"
            )
            return None

        req_id = f"labs_refresh_req_{uuid.uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self.pending_requests[req_id] = (future, conn.websocket)

        request_data = {
            "type": "open_labs_refresh",
            "req_id": req_id,
            "active": active,
            "keep_open": keep_open,
            "wait_ms": 12000,
            "reload_wait_ms": 12000,
        }

        try:
            debug_logger.log_info(
                f"[Extension Captcha] Dispatching natural Labs refresh via "
                f"route_key={route_key or '-'}, label={conn.client_label or '-'}"
            )
            await conn.websocket.send_text(json.dumps(request_data))
            result = await asyncio.wait_for(future, timeout=timeout)

            if result.get("status") == "success":
                st = result.get("session_token")
                if st:
                    debug_logger.log_info(
                        f"[Extension Captcha] Natural Labs refresh success "
                        f"(len={len(st)}, final_url={result.get('final_url') or '-'})"
                    )
                    return st

            error_msg = result.get("error", "unknown")
            debug_logger.log_error(
                f"[Extension Captcha] Natural Labs refresh failed: {error_msg}"
            )
            return None

        except asyncio.TimeoutError:
            debug_logger.log_error(
                f"[Extension Captcha] Natural Labs refresh timeout ({timeout}s)"
            )
            return None
        except Exception as e:
            debug_logger.log_error(f"[Extension Captcha] Natural Labs refresh error: {e}")
            return None
        finally:
            self.pending_requests.pop(req_id, None)

    async def refresh_session_token(
        self,
        timeout: int = 60,
        token_id: Optional[int] = None,
    ) -> Optional[str]:
        """Ask the Chrome extension to perform OAuth re-auth and return a new session token.

        The extension handles the full flow: delete old cookie → CSRF → POST signin/google
        → navigate Google OAuth → extract new cookie → return it.
        """
        if not self.active_connections:
            debug_logger.log_warning("[Extension Captcha] No connections for session refresh.")
            return None

        route_key = await self._resolve_route_key(token_id)
        conn = self._select_connection(route_key)
        if conn is None:
            debug_logger.log_warning(
                f"[Extension Captcha] No connection for token_id={token_id} route_key='{route_key}'"
            )
            return None

        req_id = f"session_req_{uuid.uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self.pending_requests[req_id] = (future, conn.websocket)

        request_data = {
            "type": "refresh_session",
            "req_id": req_id,
        }

        try:
            debug_logger.log_info(
                f"[Extension Captcha] Dispatching refresh_session via "
                f"route_key={route_key or '-'}, label={conn.client_label or '-'}"
            )
            await conn.websocket.send_text(json.dumps(request_data))
            result = await asyncio.wait_for(future, timeout=timeout)

            if result.get("status") == "success":
                st = result.get("session_token")
                if st:
                    debug_logger.log_info(
                        f"[Extension Captcha] Session refresh success (len={len(st)})"
                    )
                    return st

            error_msg = result.get("error", "unknown")
            debug_logger.log_error(
                f"[Extension Captcha] Session refresh failed: {error_msg}"
            )
            return None

        except asyncio.TimeoutError:
            debug_logger.log_error(
                f"[Extension Captcha] Session refresh timeout ({timeout}s)"
            )
            return None
        except Exception as e:
            debug_logger.log_error(f"[Extension Captcha] Session refresh error: {e}")
            return None
        finally:
            self.pending_requests.pop(req_id, None)

    async def report_flow_error(self, project_id: str, error_reason: str, error_message: str = ""):
        _ = project_id, error_message
        debug_logger.log_warning(f"[Extension Captcha] Flow error reported (ignoring): {error_reason}")

    async def _generation_request_once(
        self,
        conn: "ExtensionConnection",
        *,
        message_type: str,
        request_payload: Dict[str, Any],
        timeout: int,
    ) -> Dict[str, Any]:
        req_id = f"gen_req_{uuid.uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self.pending_generation_requests[req_id] = (future, conn.websocket)
        safe_timeout = max(5, int(timeout or 30))
        browser_timeout = max(5, safe_timeout - 2)
        message = {"type": message_type, "req_id": req_id, **request_payload}
        message["route_key"] = conn.route_key or ""
        message["client_label"] = conn.client_label or ""
        message.setdefault("timeout_seconds", browser_timeout)
        message.setdefault("timeout_ms", browser_timeout * 1000)
        try:
            print(f"[EXT-GEN-DEBUG] Sending {message_type} req_id={req_id} timeout={safe_timeout}s", flush=True)
            debug_logger.log_info(
                f"[EXT-GEN] Dispatching {message_type} via label={conn.client_label or '-'}, "
                f"url={str(request_payload.get('url',''))[:80]}"
            )
            await conn.websocket.send_text(json.dumps(message))
            print(f"[EXT-GEN-DEBUG] Message sent, waiting for response...", flush=True)
            result = await asyncio.wait_for(future, timeout=safe_timeout)
            print(f"[EXT-GEN-DEBUG] Got response: status={result.get('status') if isinstance(result, dict) else 'invalid'}", flush=True)
            if not isinstance(result, dict):
                raise RuntimeError("Invalid extension generation response format")
            if result.get("status") == "success":
                return result
            error_msg = str(result.get("error") or "Extension generation request failed")
            raise RuntimeError(error_msg)
        except asyncio.TimeoutError as exc:
            print(f"[EXT-GEN-DEBUG] TIMEOUT after {safe_timeout}s for {message_type}", flush=True)
            raise RuntimeError(f"Extension generation {message_type} timeout after {safe_timeout}s") from exc
        finally:
            self.pending_generation_requests.pop(req_id, None)

    async def submit_generation_via_extension(
        self,
        *,
        url: str,
        method: str = "POST",
        headers: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        timeout: int = 60,
        token_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not self.active_connections:
            raise RuntimeError("No extension connections for generation proxy")
        route_key = await self._resolve_route_key(token_id)
        conn = self._select_connection(route_key)
        if conn is None:
            available = self._describe_routes() or "none"
            raise RuntimeError(
                f"No extension connection for generation proxy (route_key='{route_key}'). "
                f"Available: {available}"
            )
        payload = {
            "url": str(url or "").strip(),
            "method": str(method or "POST").strip().upper(),
            "headers": dict(headers or {}),
            "json_data": json_data if isinstance(json_data, dict) else {},
        }
        return await self._generation_request_once(
            conn,
            message_type="submit_generation",
            request_payload=payload,
            timeout=timeout,
        )

    async def poll_generation_via_extension(
        self,
        *,
        url: str,
        method: str = "POST",
        headers: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        timeout: int = 45,
        token_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not self.active_connections:
            raise RuntimeError("No extension connections for generation poll")
        route_key = await self._resolve_route_key(token_id)
        conn = self._select_connection(route_key)
        if conn is None:
            raise RuntimeError(f"No extension connection for generation poll (route_key='{route_key}')")
        payload = {
            "url": str(url or "").strip(),
            "method": str(method or "POST").strip().upper(),
            "headers": dict(headers or {}),
            "json_data": json_data if isinstance(json_data, dict) else {},
        }
        return await self._generation_request_once(
            conn,
            message_type="poll_generation",
            request_payload=payload,
            timeout=timeout,
        )

    async def submit_atomic_generation(
        self,
        *,
        url: str,
        method: str = "POST",
        headers: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        token_path: str = "clientContext.recaptchaContext.token",
        recaptcha_action: str = "IMAGE_GENERATION",
        timeout: int = 60,
        token_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Execute reCAPTCHA + API request atomically in one browser tab."""
        if not self.active_connections:
            raise RuntimeError("No extension connections for atomic generation")
        capped_timeout = min(timeout, 90)
        route_key = await self._resolve_route_key(token_id)
        conn = self._select_connection(route_key)
        if conn is None:
            available = self._describe_routes() or "none"
            raise RuntimeError(
                f"No extension connection for atomic generation (route_key='{route_key}'). "
                f"Available: {available}"
            )
        payload = {
            "url": str(url or "").strip(),
            "method": str(method or "POST").strip().upper(),
            "headers": dict(headers or {}),
            "json_data": json_data if isinstance(json_data, dict) else {},
            "token_path": token_path,
            "recaptcha_action": recaptcha_action,
        }
        return await self._generation_request_once(
            conn,
            message_type="atomic_generation",
            request_payload=payload,
            timeout=capped_timeout,
        )

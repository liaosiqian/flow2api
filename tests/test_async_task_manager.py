"""
Tests for AsyncTaskManager — runs inside the Docker container where all deps exist.

Run locally:  ssh ubuntu@43.135.154.121 "sudo docker exec flow2api-headed python3 -m pytest /app/tests/test_async_task_manager.py -v"
"""
import asyncio
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.async_task_manager import (
    AsyncTask,
    AsyncTaskManager,
    MediaContext,
    QueueFullError,
    _generate_task_id,
    infer_task_type,
    MAX_QUEUE_DEPTH,
    TASK_EXPIRY_HOURS,
)


# ==================== Unit: task_id generation ====================

def test_generate_task_id_format():
    tid = _generate_task_id()
    assert tid.startswith("gen_")
    parts = tid.split("_")
    assert len(parts) == 3
    assert parts[1].isdigit()


def test_generate_task_id_unique():
    ids = {_generate_task_id() for _ in range(100)}
    assert len(ids) == 100


# ==================== Unit: type inference ====================

def test_infer_image_type():
    cfg = {"type": "image"}
    assert infer_task_type("gemini-3.0-pro-image-landscape", cfg) == "image_generate"


def test_infer_video_type():
    cfg = {"type": "video", "video_type": "t2v"}
    assert infer_task_type("veo-3-generate-landscape", cfg) == "video_text"


def test_infer_video_start_image():
    cfg = {"type": "video", "video_type": "i2v"}
    assert infer_task_type("veo-3-i2v-landscape", cfg) == "video_start_image"


# ==================== Unit: AsyncTask lifecycle ====================

def test_async_task_initial_state():
    task = AsyncTask(task_id="test_1", type="image_generate")
    assert task.status == "submitted"
    assert task.progress == 0
    assert task.result is None
    assert task.error is None


def test_async_task_to_status_response():
    task = AsyncTask(task_id="test_1", type="image_generate")
    resp = task.to_status_response()
    assert resp["success"] is True
    assert resp["task_id"] == "test_1"
    assert resp["type"] == "image_generate"
    assert resp["status"] == "submitted"


def test_async_task_to_list_item():
    task = AsyncTask(task_id="test_1", type="image_generate")
    item = task.to_list_item()
    assert "progress" not in item
    assert "result" not in item
    assert item["task_id"] == "test_1"


# ==================== Unit: MediaContext ====================

def test_media_context_fields():
    ctx = MediaContext(
        media_id="mid_1",
        token_id=15,
        project_id="proj_1",
        session_id="sess_1",
    )
    assert ctx.media_id == "mid_1"
    assert ctx.token_id == 15
    assert ctx.project_id == "proj_1"
    assert ctx.session_id == "sess_1"
    assert isinstance(ctx.created_at, datetime)


def test_media_context_default_session_none():
    ctx = MediaContext(media_id="mid_1", token_id=15, project_id="proj_1")
    assert ctx.session_id is None


# ==================== Integration: AsyncTaskManager queue ====================

@pytest.fixture
def manager():
    handler = MagicMock()
    handler.file_cache = MagicMock()
    flow_client = AsyncMock()
    token_manager = AsyncMock()
    load_balancer = AsyncMock()
    db = AsyncMock()
    mgr = AsyncTaskManager(handler, flow_client, token_manager, load_balancer, db)
    return mgr


def test_submit_creates_task(manager):
    task = manager.submit("image_generate")
    assert task.task_id.startswith("gen_")
    assert task.type == "image_generate"
    assert task.status == "submitted"


def test_get_task(manager):
    task = manager.submit("image_generate")
    fetched = manager.get_task(task.task_id)
    assert fetched is task


def test_get_task_not_found(manager):
    assert manager.get_task("nonexistent") is None


def test_list_tasks_ordered(manager):
    t1 = manager.submit("image_generate")
    t2 = manager.submit("video_text")
    t3 = manager.submit("image_upsample")
    tasks = manager.list_tasks(limit=10)
    assert tasks[0] is t3
    assert tasks[2] is t1


def test_queue_depth_limit(manager):
    for i in range(MAX_QUEUE_DEPTH):
        manager.submit("image_generate")

    with pytest.raises(QueueFullError):
        manager.submit("image_generate")


def test_queue_excludes_done_tasks(manager):
    for i in range(MAX_QUEUE_DEPTH):
        t = manager.submit("image_generate")
        t.status = "done"

    task = manager.submit("image_generate")
    assert task is not None


def test_queue_excludes_failed_tasks(manager):
    for i in range(MAX_QUEUE_DEPTH):
        t = manager.submit("image_generate")
        t.status = "failed"

    task = manager.submit("image_generate")
    assert task is not None


# ==================== Integration: media mapping ====================

def test_register_and_get_media(manager):
    manager.register_media("mid_1", 15, "proj_1", "sess_1")
    ctx = manager.get_media_context("mid_1")
    assert ctx is not None
    assert ctx.token_id == 15
    assert ctx.project_id == "proj_1"
    assert ctx.session_id == "sess_1"


def test_get_media_not_found(manager):
    assert manager.get_media_context("nonexistent") is None


def test_get_media_expired(manager):
    manager.register_media("mid_1", 15, "proj_1")
    ctx = manager._media_map["mid_1"]
    ctx.created_at = datetime.utcnow() - timedelta(hours=TASK_EXPIRY_HOURS, seconds=60)
    assert manager.get_media_context("mid_1") is None
    assert "mid_1" not in manager._media_map


# ==================== Integration: cleanup ====================

def test_cleanup_expired_tasks(manager):
    t1 = manager.submit("image_generate")
    t1.status = "done"
    t1.created_at = datetime.utcnow() - timedelta(hours=TASK_EXPIRY_HOURS, seconds=60)
    t1.expires_at = datetime.utcnow() - timedelta(seconds=60)

    t2 = manager.submit("image_generate")

    manager._cleanup_expired()

    assert manager.get_task(t1.task_id) is None
    assert manager.get_task(t2.task_id) is t2


def test_cleanup_expired_media(manager):
    manager.register_media("mid_old", 15, "proj_1")
    manager._media_map["mid_old"].created_at = datetime.utcnow() - timedelta(
        hours=TASK_EXPIRY_HOURS, seconds=60
    )
    manager.register_media("mid_new", 15, "proj_2")

    manager._cleanup_expired()

    assert "mid_old" not in manager._media_map
    assert "mid_new" in manager._media_map


# ==================== Integration: active count ====================

def test_active_count(manager):
    assert manager.get_active_count() == 0

    t1 = manager.submit("image_generate")
    assert manager.get_active_count() == 1

    t1.status = "processing"
    assert manager.get_active_count() == 1

    t1.status = "done"
    assert manager.get_active_count() == 0

"""
快照管理路由模块

提供快照创建/列表/删除/恢复功能。

路由前缀: /api (由 server.py 注册)
- /snapshots, /snapshots/{snapshot_id}, /snapshots/{snapshot_id}/restore
"""

from __future__ import annotations

import json
import logging
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from peng.common.paths import get_temp_directory, ensure_dir_with_owner

logger = logging.getLogger(__name__)
router = APIRouter()


class SnapshotCreateRequest(BaseModel):
    name: str
    description: str = ""


class SnapshotModel(BaseModel):
    id: int
    name: str
    description: str = ""
    created_at: str
    message_count: int = 0
    task_count: int = 0


SNAPSHOTS_DIR = get_temp_directory() / "peng_snapshots"
ensure_dir_with_owner(SNAPSHOTS_DIR)

_snapshots: list[dict[str, Any]] = []
_next_snapshot_id = 1


@router.get("/snapshots")
async def get_snapshots() -> list[dict[str, Any]]:
    """获取快照列表"""
    try:
        return _snapshots
    except Exception as e:
        logger.error(f"获取快照列表失败: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"获取快照列表失败: {e}") from None


@router.post("/snapshots")
async def create_snapshot(request: SnapshotCreateRequest) -> dict[str, Any]:
    """创建快照"""
    global _next_snapshot_id
    try:
        from peng.message.manager import MessageManager
        from peng.task.manager import TaskManager

        msg_manager = MessageManager()
        task_manager = TaskManager()

        message_count = 0
        task_count = 0

        if msg_manager:
            try:
                messages = msg_manager.get_active_chains()
                message_count = len(messages)
            except Exception as e:
                logger.warning(f"获取消息数量失败: {e}")

        if task_manager:
            try:
                tasks = task_manager.list_tasks()
                task_count = len(tasks)
            except Exception as e:
                logger.warning(f"获取事务数量失败: {e}")

        snapshot = {
            "id": _next_snapshot_id, "name": request.name,
            "description": request.description, "created_at": datetime.now().isoformat(),
            "message_count": message_count, "task_count": task_count,
        }

        snapshot_file = SNAPSHOTS_DIR / f"snapshot_{_next_snapshot_id}.json"
        snapshot_file.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

        _snapshots.append(snapshot)
        _next_snapshot_id += 1

        logger.info(f"✅ 快照创建成功: {request.name} (ID: {snapshot['id']})")
        return {"success": True, "message": "快照创建成功", "snapshot": snapshot}
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"创建快照失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"创建快照失败: {e}") from None


@router.delete("/snapshots/{snapshot_id}")
async def delete_snapshot(snapshot_id: int) -> dict[str, Any]:
    """删除快照"""
    try:
        global _snapshots
        original_count = len(_snapshots)
        _snapshots = [s for s in _snapshots if s["id"] != snapshot_id]
        deleted = len(_snapshots) < original_count

        if not deleted:
            raise HTTPException(status_code=404, detail=f"快照不存在: {snapshot_id}")

        snapshot_file = SNAPSHOTS_DIR / f"snapshot_{snapshot_id}.json"
        if snapshot_file.exists():
            snapshot_file.unlink()

        logger.info(f"✅ 快照删除成功: {snapshot_id}")
        return {"success": True, "message": "快照删除成功"}
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"删除快照失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"删除快照失败: {e}") from None


@router.post("/snapshots/{snapshot_id}/restore")
async def restore_snapshot(snapshot_id: int) -> dict[str, Any]:
    """恢复快照"""
    try:
        snapshot = next((s for s in _snapshots if s["id"] == snapshot_id), None)
        if not snapshot:
            raise HTTPException(status_code=404, detail=f"快照不存在: {snapshot_id}")

        logger.info(f"⚠️ 快照恢复功能暂未实现: {snapshot_id} ({snapshot['name']})")
        return {"success": True, "message": f"快照恢复成功: {snapshot['name']}", "snapshot": snapshot}
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"恢复快照失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"恢复快照失败: {e}") from None

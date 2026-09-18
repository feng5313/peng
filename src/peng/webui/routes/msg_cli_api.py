#!/usr/bin/env python3
"""
Message CLI API 路由

为 bin/msg 提供 RESTful API，避免 CLI 直接操作数据库。
所有路由前缀: /api/cli/msg/

设计原则：
- 薄路由层：直接调用 MessageManager / MessageDatabase / cli_service
- 返回 JSON 数据，由 CLI 端负责格式化显示
- 统一错误处理
"""

from __future__ import annotations

import logging
import traceback
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from peng.message.manager import MessageManager
from peng.message.database import MessageDatabase
from peng.message import cli_service
from peng.config import get_agent_name, get_master_name

logger = logging.getLogger(__name__)
AGENT_NAME = get_agent_name()
MASTER_NAME = get_master_name()

router = APIRouter(prefix="/cli/msg", tags=["cli-msg"])


# ==================== Pydantic 模型 ====================


class SendMessageRequest(BaseModel):
    """发送消息请求"""
    content: str
    reply_to: int | None = None
    timeout: int = 0
    from_: str | None = None
    to: str | None = None
    file: str | None = None
    nest: str | None = None
    workdir: str | None = None


class FieldUpdateRequest(BaseModel):
    """字段更新请求"""
    value: str = ""
    file: str | None = None


class StatusUpdateRequest(BaseModel):
    """状态更新请求"""
    status: str  # read/processing/done/failed


class MoveMessageRequest(BaseModel):
    """移动消息请求"""
    target_id: int | None = None


# ==================== 辅助函数 ====================


def _get_message_manager() -> MessageManager:
    """获取 MessageManager 实例"""
    return MessageManager()


def _get_message_db() -> MessageDatabase:
    """获取 MessageDatabase 实例"""
    return MessageDatabase()


def _get_msg_or_404(msg_id: int) -> dict[str, Any]:
    """获取消息或返回 404"""
    db = _get_message_db()
    msg = db.get_message(msg_id)
    if not msg:
        raise HTTPException(status_code=404, detail=f"消息 #{msg_id} 不存在")
    return msg


# ==================== API 路由 ====================


@router.post("/send")
async def send_message(request: SendMessageRequest) -> dict[str, Any]:
    """发送消息"""
    result = cli_service.send_message(
        request.content, request.reply_to, request.timeout, request.from_, request.to,
        nest=request.nest, workdir=request.workdir,
    )
    return result


@router.get("/list")
async def list_messages(
    mode: str = "unarchived",  # all/unarchived/outbox/unread/finished/processing
    order: str = "ASC",
) -> dict[str, Any]:
    """列出消息"""
    if mode == "all":
        return cli_service.read_all_messages(order=order)
    elif mode == "outbox":
        return cli_service.read_outbox(show_all=True)
    elif mode == "unread":
        return cli_service.list_unread_inbox()
    elif mode == "finished":
        return cli_service.show_finished()
    elif mode == "processing":
        return cli_service.show_processing_messages()
    else:
        return cli_service.read_unarchived_messages(order=order)


@router.get("/{msg_id}")
async def get_message_detail(msg_id: int) -> dict[str, Any]:
    """获取消息详情"""
    msg = _get_msg_or_404(msg_id)

    # 获取关联事务信息
    task_info = {}
    try:
        mm = _get_message_manager()
        task_id = mm.get_task_id(msg_id)
        if task_id:
            task_info["task_id"] = task_id
            task = mm._task_manager.get_task(task_id)
            if task:
                task_info["task_status"] = task.get("status", "pending")
    except Exception:
        pass

    result = dict(msg)
    result["task_info"] = task_info
    return result


@router.get("/{msg_id}/tasks")
async def get_message_tasks(msg_id: int) -> dict[str, Any]:
    """获取消息关联的事务"""
    return cli_service.get_tasks_by_message(msg_id)


@router.post("/{msg_id}/reply")
async def reply_message(msg_id: int, request: SendMessageRequest) -> dict[str, Any]:
    """回复消息"""
    result = cli_service.send_message(
        request.content, msg_id, request.timeout, request.from_, request.to,
        nest=request.nest, workdir=request.workdir,
    )
    return result


@router.delete("/{msg_id}")
async def delete_message(msg_id: int, force: bool = False) -> dict[str, Any]:
    """删除消息"""
    mm = _get_message_manager()
    result = mm.delete_message(msg_id, force)
    return result


@router.get("/{msg_id}/status")
async def get_message_status(msg_id: int) -> dict[str, Any]:
    """获取消息状态"""
    msg = _get_msg_or_404(msg_id)
    return {"msg_id": msg_id, "status": msg.get("status", "unknown")}


@router.put("/{msg_id}/status")
async def set_message_status(msg_id: int, request: StatusUpdateRequest) -> dict[str, Any]:
    """设置消息状态"""
    _get_msg_or_404(msg_id)
    db = _get_message_db()
    success = db.update_message_status(msg_id, request.status)
    if not success:
        raise HTTPException(status_code=500, detail="设置状态失败")
    return {"success": True, "msg_id": msg_id, "status": request.status}


@router.get("/status-stats")
async def get_status_stats() -> dict[str, Any]:
    """获取消息状态统计"""
    return cli_service.show_status()


@router.get("/check-stale")
async def check_stale() -> dict[str, Any]:
    """检测失效消息"""
    return cli_service.check_stale_messages()


@router.get("/check-paused")
async def check_paused() -> dict[str, Any]:
    """检测暂停消息"""
    return cli_service.check_paused_messages()


@router.post("/{msg_id}/control/{action}")
async def control_message_task(msg_id: int, action: str) -> dict[str, Any]:
    """事务控制（pause/cancel/restart/resume/up/down）"""
    result = cli_service.control_message_task(msg_id, action)
    return result


@router.get("/search")
async def search_messages(keyword: str) -> dict[str, Any]:
    """搜索消息"""
    return cli_service.search_messages(keyword)


@router.get("/{msg_id}/tree")
async def get_message_tree(msg_id: int) -> dict[str, Any]:
    """获取消息分支树"""
    return cli_service.get_message_tree(msg_id)


@router.get("/{msg_id}/context")
async def get_message_context(msg_id: int) -> dict[str, Any]:
    """获取消息上下文"""
    msg = _get_msg_or_404(msg_id)
    db = _get_message_db()

    chain_head_id = None
    if msg.get("is_chain_head") == 1 or msg.get("reply_to") is None:
        chain_head_id = msg_id
    else:
        chain_head_id = msg.get("chain_head_id")
        if not chain_head_id:
            mm = _get_message_manager()
            tree = mm.get_message_tree(msg_id)
            if tree:
                chain_head_id = tree.root.message.id

    if not chain_head_id:
        raise HTTPException(status_code=400, detail="无法确定链首消息")

    context = db.build_context_history(chain_head_id)
    if not context:
        raise HTTPException(status_code=500, detail="无法构建上下文")

    return {"context": context, "chain_head_id": chain_head_id}


@router.post("/{msg_id}/move")
async def move_message(msg_id: int, request: MoveMessageRequest) -> dict[str, Any]:
    """移动消息"""
    mm = _get_message_manager()
    result = mm.move_message(msg_id, request.target_id)
    return result


# ==================== 消息字段操作 ====================


@router.get("/{msg_id}/field/{field_name}")
async def get_message_field(msg_id: int, field_name: str) -> dict[str, Any]:
    """获取消息字段"""
    msg = _get_msg_or_404(msg_id)
    value = msg.get(field_name, "")
    return {"msg_id": msg_id, "field": field_name, "value": value}


@router.put("/{msg_id}/field/{field_name}")
async def set_message_field(msg_id: int, field_name: str, request: FieldUpdateRequest) -> dict[str, Any]:
    """设置消息字段"""
    msg = _get_msg_or_404(msg_id)
    db = _get_message_db()

    # 🔧 Task#1893：记录更新前的旧值（供前端撤销/重做）
    old_value = msg.get(field_name)

    value = request.value
    if request.file:
        try:
            with open(request.file, "r", encoding="utf-8") as f:
                value = f.read()
            import os
            try:
                os.remove(request.file)
            except OSError:
                pass
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"读取文件失败: {e}")

    # 🔧 Task#484修复：前端不再通过textToArgs转义换行符，REST API直接接收原始内容
    # 移除之前的粗暴 replace("\\n", "\n")，避免破坏原始内容中本身就有的 \n 转义符

    success = db.update_message(msg_id, **{field_name: value})
    if not success:
        raise HTTPException(status_code=500, detail="无法更新消息")

    result: dict[str, Any] = {
        "success": True, "msg_id": msg_id, "field": field_name, "value": value,
        "old_value": old_value,  # 🔧 Task#1893：撤销/重做支持
    }

    # 如果是 content 字段且为根消息（reply_to IS NULL），同步更新关联的根事务 content
    if field_name == "content" and value:
        msg = db.get_message(msg_id)
        if msg and msg.get("reply_to") is None:
            from peng.task.database import TaskDatabase
            task_db = TaskDatabase()
            related_task = task_db.get_by_message(msg_id)
            if related_task:
                task_info = related_task[0] if isinstance(related_task, list) else related_task
                if not task_info.get("parent_task_id"):
                    task_success = task_db.update_task(task_info["id"], content=value)
                    result["task_synced"] = task_success
                    result["task_id"] = task_info["id"]

    return result


@router.delete("/{msg_id}/field/{field_name}")
async def clear_message_field(msg_id: int, field_name: str) -> dict[str, Any]:
    """清空消息字段"""
    _get_msg_or_404(msg_id)
    db = _get_message_db()

    success = db.update_message(msg_id, **{field_name: ""})
    if not success:
        raise HTTPException(status_code=500, detail="无法更新消息")

    result: dict[str, Any] = {"success": True, "msg_id": msg_id, "field": field_name, "cleared": True}

    # 如果是 content 字段且为根消息，同步清空关联的根事务 content
    if field_name == "content":
        msg = db.get_message(msg_id)
        if msg and msg.get("reply_to") is None:
            from peng.task.database import TaskDatabase
            task_db = TaskDatabase()
            related_task = task_db.get_by_message(msg_id)
            if related_task:
                task_info = related_task[0] if isinstance(related_task, list) else related_task
                if not task_info.get("parent_task_id"):
                    task_success = task_db.update_task(task_info["id"], content="")
                    result["task_synced"] = task_success
                    result["task_id"] = task_info["id"]

    return result

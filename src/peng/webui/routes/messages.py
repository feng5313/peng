"""
消息管理路由模块

提供消息 CRUD、归档、移动、重启等功能。

路由前缀: /api (由 server.py 注册)
- /messages, /messages/{msg_id}, /messages/{msg_id}/reply, /messages/{msg_id}/archive 等
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from typing import Any

from fastapi import APIRouter, HTTPException

from peng.config import get_master_name
from peng.message.manager import MessageManager
from peng.message.types import MessageTree
from peng.task.manager import TaskManager
from peng.webui.routes._common import (
    AGENT_NAME,
    BroadcastMessageRequest,
    CreateMessageRequest,
    MessageMoveRequest,
    ReplyMessageRequest,
)
from peng.webui.routes.workdir_config import _remove_topped_message, _remove_topped_task

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/messages/{msg_id}/collapsed")
async def get_message_collapsed(msg_id: int) -> dict[str, Any]:
    """获取消息的折叠状态"""
    msg_manager = MessageManager()
    try:
        collapsed = msg_manager.get_message_collapsed(msg_id)
        return {"msg_id": msg_id, "collapsed": collapsed}
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"获取消息折叠状态失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.post("/messages/{msg_id}/collapsed")
async def set_message_collapsed(msg_id: int, collapsed: bool = True) -> dict[str, Any]:
    """设置消息的折叠状态"""
    msg_manager = MessageManager()
    try:
        success = msg_manager.set_message_collapsed(msg_id, collapsed)
        if not success:
            raise HTTPException(status_code=404, detail="消息不存在")
        return {"msg_id": msg_id, "collapsed": collapsed, "success": success}
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"设置消息折叠状态失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.get("/messages")
async def get_messages(
    status: str | None = None, limit: int = 50, include_archived: bool = False,
    exclude_tree: bool = False
) -> list[dict[str, Any]]:
    """获取消息列表（树形结构）
    
    Args:
        exclude_tree: 为True时不返回tree字段（用于轮询场景，减少传输量）
    """
    # 🔧 Task#215: 将同步DB操作放到线程池，避免阻塞uvicorn事件循环
    def _do_query():
        msg_manager = MessageManager()
        messages = msg_manager.get_active_chains(status=status, include_archived=include_archived)
        messages = messages[:limit]

        # 🔧 Task#249：轮询模式(exclude_tree)时跳过tree查询，大幅减少数据量和CPU开销
        if exclude_tree:
            result = []
            for msg in messages:
                msg_data = dict(msg)
                msg_data["tree"] = None  # 不返回tree，前端用缓存
                result.append(msg_data)
            return result

        chain_head_ids = [msg["id"] for msg in messages]
        trees = msg_manager.get_message_trees_batch(chain_head_ids)

        result = []
        for msg in messages:
            msg_data = dict(msg)
            message_tree = trees.get(msg["id"])
            if message_tree:
                msg_data["tree"] = message_tree.to_dict()
                msg_data["message_count"] = message_tree.total_count
            else:
                msg_data["tree"] = None
                msg_data["message_count"] = 0
            result.append(msg_data)
        return result

    try:
        return await asyncio.to_thread(_do_query)
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"获取消息列表失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.get("/messages/{msg_id}")
async def get_message(msg_id: int) -> dict[str, Any]:
    """获取消息详情"""
    # 🔧 Task#215: 将同步DB操作放到线程池
    def _do_query():
        msg_manager = MessageManager()
        msg: dict[str, Any] | None = msg_manager.get_message_with_context(msg_id)
        if msg is None:
            return None

        chain = msg_manager.get_message_tree(msg_id)
        if chain:
            msg["reply_chain"] = [
                {
                    "id": m.id, "timestamp": m.timestamp, "sender": m.sender,
                    "receiver": m.receiver, "content": m.content, "title": m.title,
                    "workdir": m.workdir, "status": m.status, "created_at": m.created_at,
                    "read_at": m.read_at, "archived_at": m.archived_at, "locked_by": m.locked_by,
                    "locked_at": m.locked_at, "heartbeat_at": m.heartbeat_at, "process_id": m.process_id,
                    "reply_to": m.reply_to, "chain_head_id": m.chain_head_id,
                    "chain_context": m.chain_context, "error": m.error, "caller": m.caller,
                    "caller_session_id": m.caller_session_id, "is_chain_head": m.is_chain_head,
                    "is_cancelled": m.is_cancelled, "timeout": m.timeout, "is_paused": m.is_paused,
                    "collapsed": m.collapsed, "is_archived": m.is_archived,
                    "pending_delete": m.pending_delete, "nest": m.nest,
                }
                for m in chain.all_messages
            ]
            msg["chain_head_id"] = chain.head_id

        return msg

    try:
        result = await asyncio.to_thread(_do_query)
        if result is None:
            raise HTTPException(status_code=404, detail="消息不存在")
        return result
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"获取消息详情失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.post("/messages")
async def create_message(request: CreateMessageRequest) -> dict[str, Any]:
    """创建新消息并异步处理"""
    msg_manager = MessageManager()
    try:
        content = request.content
        if request.files:
            file_info = "\n\n[附件]\n"
            for f in request.files:
                path = f.get("path", "")
                name = f.get("name", "unknown")
                file_info += f"{name}: {path}\n"
            content += file_info

        chain_head_id = None
        if request.reply_to:
            chain = msg_manager.get_message_tree(request.reply_to)
            if chain:
                chain_head_id = chain.head_id

        effective_reply_to = request.reply_to

        msg_id, msg = msg_manager.create_message(
            content=content,
            sender=get_master_name(),
            receiver=AGENT_NAME,
            caller="webui",
            reply_to=effective_reply_to,
            nest=request.nest,
        )

        final_chain_head_id = chain_head_id or msg_id

        if msg.get("is_duplicate"):
            return {"id": msg_id, "chain_head_id": final_chain_head_id, "duplicate": True, "message": "消息已存在（重复）"}

        # 消息创建广播已由 MessageDatabase.create_message 的 _notify_webui 统一处理
        # 不再在此处重复广播，避免前端收到两次 message_update 导致闪烁

        ui_kwargs = {
            "timeout": 300,
            "thinking_mode": request.thinking_mode,
            "thinking_budget": request.thinking_budget,
            "search_enabled": request.search_enabled,
            "model": request.model,
            "workdir": request.workdir,
        }

        return {
            "id": msg_id, "chain_head_id": final_chain_head_id, "duplicate": False,
            "message": "消息创建成功", "action": "processing", "success": True,
            "ui_settings": {
                "thinking_mode": request.thinking_mode,
                "thinking_budget": request.thinking_budget,
                "search_enabled": request.search_enabled,
                "model": request.model,
            },
        }
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"创建消息失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.post("/messages/{msg_id}/reply")
async def reply_message(msg_id: int, request: ReplyMessageRequest) -> dict[str, Any]:
    """回复消息"""
    msg_manager = MessageManager()
    try:
        content = request.content
        if request.files:
            file_info = "\n\n[附件]\n"
            for f in request.files:
                path = f.get("path", "")
                name = f.get("name", "unknown")
                file_info += f"{name}: {path}\n"
            content += file_info

        chain = msg_manager.get_chain_head(msg_id)
        chain_head_id = chain["id"] if chain else msg_id

        new_msg_id, msg = msg_manager.create_message(
            content=content, sender=get_master_name(), receiver=AGENT_NAME,
            caller="webui", reply_to=msg_id, nest=request.nest,
        )
        logger.debug(f"📬 正在回复消息 #{new_msg_id}: {content[:50]}...")
        if msg.get("is_duplicate"):
            return {"id": new_msg_id, "chain_head_id": chain_head_id, "is_duplicate": True, "error": "消息已存在（重复）", "success": False}

        # 消息创建广播已由 MessageDatabase.create_message 的 _notify_webui 统一处理
        return {"id": new_msg_id, "chain_head_id": chain_head_id, "message": "回复成功", "success": True}
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"回复消息失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.delete("/messages/{msg_id}")
async def delete_message(msg_id: int) -> dict[str, Any]:
    """删除消息"""
    msg_manager = MessageManager()
    task_manager = TaskManager()

    if msg_manager is None:
        raise HTTPException(status_code=503, detail="消息管理器未初始化")

    try:
        # 🔧 Task#1346：将同步DB操作放到线程池，避免阻塞uvicorn事件循环
        def _do_delete():
            # 🔧 Task#1893：删除前备份快照（供撤销删除恢复，失败不阻塞删除）
            from .undelete import backup_for_delete

            backup_file = backup_for_delete("message", msg_id)

            # Step 1: 收集 task_ids（用于后续WebSocket广播，删除前收集）
            all_task_ids = []
            if task_manager:
                tasks = task_manager.get_tasks_by_message(msg_id)
                for task in tasks:
                    all_task_ids.append(task["id"])
                    descendants = task_manager.db.get_descendants(task["id"])
                    all_task_ids.extend([d["id"] for d in descendants])

            # Step 2: 委托 manager 统一执行删除（tasks + messages）
            result = msg_manager.delete_message(msg_id)
            deleted_tasks = result.get("deleted_tasks", 0)
            deleted_messages = result.get("deleted_messages", 0)

            if deleted_messages == 0 and "error" in result:
                return None  # 消息不存在

            # 🔧 Task#228：删除消息时移除顶置状态（在线程内完成）
            _remove_topped_message(msg_id)

            return {
                "all_task_ids": all_task_ids,
                "deleted_tasks": deleted_tasks,
                "backup_file": backup_file,
            }

        result = await asyncio.to_thread(_do_delete)
        if result is None:
            raise HTTPException(status_code=404, detail="消息不存在")

        from ..websocket import broadcast_message_update, broadcast_task_update
        await broadcast_message_update(msg_id, "deleted", {}, source="webui")
        # 通知前端事务已被删除，触发事务面板刷新
        for tid in result["all_task_ids"]:
            await broadcast_task_update(tid, "deleted", source="webui")
        return {
            "success": True, "message": "消息已删除",
            "deleted_tasks": result["deleted_tasks"],
            "backup_file": result.get("backup_file"),  # 🔧 Task#1893：撤销删除凭据
        }
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"删除消息失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.delete("/messages/{msg_id}/recursive")
async def delete_message_recursive(msg_id: int) -> dict[str, Any]:
    """递归删除消息（删除当前消息及其所有子消息）"""
    msg_manager = MessageManager()
    task_manager = TaskManager()

    if msg_manager is None:
        raise HTTPException(status_code=503, detail="消息管理器未初始化")

    try:
        # 🔧 Task#1346：将同步DB操作放到线程池，避免阻塞uvicorn事件循环
        def _do_delete_recursive():
            msg = msg_manager.get_message(msg_id)
            if not msg:
                return None

            # 🔧 Task#1893：删除前备份快照（供撤销删除恢复，失败不阻塞删除）
            from .undelete import backup_for_delete

            backup_file = backup_for_delete("message", msg_id)

            reply_to = msg.get("reply_to")
            chain_head_id = msg.get("chain_head_id")

            # 执行删除（manager内部会处理task+message删除）
            result = msg_manager.delete_message(msg_id)
            deleted_count = result.get("deleted_messages")
            deleted_tasks_count = result.get("deleted_tasks", 0)

            # 🔧 Task#228：移除顶置状态（在线程内完成，避免阻塞事件循环）
            _remove_topped_message(msg_id)

            return {
                "deleted_count": deleted_count,
                "deleted_tasks_count": deleted_tasks_count,
                "deleted_task_ids": [],  # DB层 delete_task_recursive 已广播
                "reply_to": reply_to,
                "chain_head_id": chain_head_id,
                "backup_file": backup_file,
            }

        result = await asyncio.to_thread(_do_delete_recursive)
        if result is None:
            raise HTTPException(status_code=404, detail="消息不存在")

        # 🔧 Task#1346：build_context_history 不阻塞HTTP响应
        # chain_context 是给下次LLM处理用的，不需要在HTTP响应前完成
        # 放到后台线程异步执行，大幅减少用户感知延迟
        chain_head_id = result.pop("chain_head_id", None)
        reply_to = result.pop("reply_to", None)
        if reply_to and chain_head_id:
            def _rebuild_context():
                try:
                    msg_manager.db.build_context_history(chain_head_id)
                except Exception as e:
                    logger.warning(f"后台重建链#{chain_head_id}上下文失败: {e}")
            asyncio.get_event_loop().run_in_executor(None, _rebuild_context)

        from ..websocket import broadcast_message_update, broadcast_task_update
        await broadcast_message_update(msg_id, "deleted", {}, source="webui")
        # 通知前端事务已被删除，触发事务面板刷新
        if result["deleted_tasks_count"] > 0:
            for tid in result["deleted_task_ids"]:
                await broadcast_task_update(tid, "deleted", source="webui")
        return {
            "success": True, "message": f"已删除 {result['deleted_count']} 条消息",
            "deleted_count": result["deleted_count"],
            "backup_file": result.get("backup_file"),  # 🔧 Task#1893：撤销删除凭据
        }
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"递归删除消息失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.post("/messages/{msg_id}/archive")
async def archive_message(msg_id: int) -> dict[str, Any]:
    """归档消息（同时归档关联的根事务）"""
    msg_manager = MessageManager()
    task_manager = TaskManager()
    try:
        # 🔧 Task#1346：将同步DB操作放到线程池，避免阻塞uvicorn事件循环
        def _do_archive():
            success = msg_manager.archive_message(msg_id)
            if not success:
                return None

            archived_tasks = 0
            archived_task_ids = []
            if task_manager:
                root_tasks = task_manager.db.get_root_tasks_by_message(msg_id)
                for task in root_tasks:
                    archived_task_ids.append(task["id"])
                    descendants = task_manager.db.get_descendants(task["id"])
                    archived_task_ids.extend([d["id"] for d in descendants])
                    archived_tasks += task_manager.db.archive_task_tree(task["id"])
                if archived_tasks > 0:
                    logger.info(f"归档消息 {msg_id} 关联的 {archived_tasks} 个事务（含子事务）")

            # 🔧 Task#228：归档消息时移除顶置状态（在线程内完成，避免多次文件IO阻塞事件循环）
            _remove_topped_message(msg_id)
            for tid in archived_task_ids:
                _remove_topped_task(tid)

            return {"archived_tasks": archived_tasks, "archived_task_ids": archived_task_ids}

        result = await asyncio.to_thread(_do_archive)
        if result is None:
            raise HTTPException(status_code=404, detail="消息不存在")

        from ..websocket import broadcast_message_update, broadcast_task_update
        await broadcast_message_update(msg_id, "archived", {}, source="webui")
        # 通知前端事务已被归档，触发事务面板刷新
        if result["archived_tasks"] > 0:
            for tid in result["archived_task_ids"]:
                await broadcast_task_update(tid, "archived", source="webui")
        return {"success": True, "message": "消息已归档", "archived_tasks": result["archived_tasks"]}
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"归档消息失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.post("/messages/{msg_id}/unarchive")
async def unarchive_message(msg_id: int) -> dict[str, Any]:
    """取消归档消息"""
    msg_manager = MessageManager()
    task_manager = TaskManager()
    try:
        # 🔧 Task#1346：将同步DB操作放到线程池，避免阻塞uvicorn事件循环
        def _do_unarchive():
            success = msg_manager.unarchive_message(msg_id)
            if not success:
                return None

            unarchived_tasks = 0
            unarchived_task_ids = []
            if task_manager:
                root_tasks = task_manager.db.get_root_tasks_by_message(msg_id)
                for task in root_tasks:
                    unarchived_task_ids.append(task["id"])
                    descendants = task_manager.db.get_descendants(task["id"])
                    unarchived_task_ids.extend([d["id"] for d in descendants])
                    unarchived_tasks += task_manager.db.unarchive_task_tree(task["id"])
                if unarchived_tasks > 0:
                    logger.info(f"取消归档消息 {msg_id} 关联的 {unarchived_tasks} 个事务（含子事务）")

            return {"unarchived_tasks": unarchived_tasks, "unarchived_task_ids": unarchived_task_ids}

        result = await asyncio.to_thread(_do_unarchive)
        if result is None:
            raise HTTPException(status_code=404, detail="消息不存在")

        from ..websocket import broadcast_message_update, broadcast_task_update
        await broadcast_message_update(msg_id, "unarchived", {}, source="webui")
        # 通知前端事务已取消归档，触发事务面板刷新
        if result["unarchived_tasks"] > 0:
            for tid in result["unarchived_task_ids"]:
                await broadcast_task_update(tid, "unarchived", source="webui")
        return {"success": True, "message": "消息已取消归档", "unarchived_tasks": result["unarchived_tasks"]}
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"取消归档消息失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.post("/messages/{msg_id}/move")
async def move_message(msg_id: int, request: MessageMoveRequest):
    """移动消息到新的父节点"""
    msg_manager = MessageManager()
    try:
        result = msg_manager.move_message(msg_id, request.target_id)
        if not result["success"]:
            raise HTTPException(status_code=400, detail=result["message"])

        target_desc = f"#{request.target_id}" if request.target_id else "根"
        logger.info(f"✅ 消息 #{msg_id} 已移动到 {target_desc}")

        from ..websocket import broadcast_message_update
        await broadcast_message_update(msg_id, "moved", {"target_id": request.target_id}, source="webui")
        return {
            "success": True,
            "message": result.get("message"),
            # 🔧 Task#1893：撤销/重做支持——透传移动前状态（swapped=True 表示
            # 目标为子孙时执行了内容交换，撤销需按 swap_old_contents 恢复）
            "swapped": result.get("swapped", False),
            "previous_reply_to": result.get("previous_reply_to"),
            "swap_ids": result.get("swap_ids"),
            "swap_old_contents": result.get("swap_old_contents"),
        }
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"移动消息失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.post("/messages/{msg_id}/restart")
async def restart_message(msg_id: int) -> dict[str, Any]:
    """重启消息及其关联的事务"""
    msg_manager = MessageManager()
    try:
        success = await msg_manager.restart_message(msg_id)
        if not success:
            raise HTTPException(status_code=400, detail="消息重启失败")

        from ..websocket import broadcast_message_update
        await broadcast_message_update(msg_id, "restarted", {}, source="webui")
        return {"success": True, "message": "消息已重启"}
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"重启消息失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.post("/messages/{msg_id}/top")
async def toggle_message_top(msg_id: int) -> dict[str, Any]:
    """切换消息的顶置状态，同时同步关联根事务的顶置"""
    try:
        from peng.webui.routes.workdir_config import _load_webui_json, _save_webui_json
        config = _load_webui_json()

        if "webui" not in config:
            config["webui"] = {}

        topped_msg_ids: list[int] = config["webui"].get("topped_message_ids", [])
        topped_task_ids: list[int] = config["webui"].get("topped_task_ids", [])

        if msg_id in topped_msg_ids:
            topped_msg_ids.remove(msg_id)
            topped = False
        else:
            topped_msg_ids.insert(0, msg_id)
            topped = True

        config["webui"]["topped_message_ids"] = topped_msg_ids

        # 🔧 同步关联根事务的顶置状态
        task_manager = TaskManager()
        root_tasks = task_manager.db.get_root_tasks_by_message(msg_id)
        for task in root_tasks:
            tid = task["id"]
            if topped and tid not in topped_task_ids:
                topped_task_ids.insert(0, tid)
            elif not topped and tid in topped_task_ids:
                topped_task_ids.remove(tid)
        config["webui"]["topped_task_ids"] = topped_task_ids

        _save_webui_json(config)

        return {
            "success": True, "topped": topped,
            "topped_message_ids": topped_msg_ids,
            "topped_task_ids": topped_task_ids,
        }
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"切换消息顶置失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.post("/internal/broadcast-message")
async def internal_broadcast_message(request: BroadcastMessageRequest) -> dict[str, Any]:
    """内部接口：子进程通过 HTTP 请求触发主进程 WebSocket 广播"""
    try:
        from ..websocket import broadcast_message_update
        await broadcast_message_update(request.msg_id, request.action, request.details, source=request.source)
        return {"success": True}
    except Exception as e:
        logger.error(f"内部消息广播失败: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e)) from None

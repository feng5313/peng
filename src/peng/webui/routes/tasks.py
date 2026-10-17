"""
事务管理路由模块

提供事务 CRUD、操作（暂停/恢复/取消/重启/移动/删除）、内部广播等功能。

路由前缀: /api (由 server.py 注册)
- /tasks, /tasks/{task_id}, /tasks/{task_id}/action, /tasks/{task_id}/move 等
- /internal/broadcast-task
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from typing import Any

from fastapi import APIRouter, HTTPException

from peng.message.manager import MessageManager
from peng.task.manager import TaskManager
from peng.webui.routes._common import (
    BroadcastTaskRequest,
    MoveTaskRequest,
    TaskActionRequest,
)
from peng.webui.routes.workdir_config import _remove_topped_task

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/tasks/{task_id}/collapsed")
async def get_task_collapsed(task_id: int) -> dict[str, Any]:
    """获取事务的折叠状态"""
    task_manager = TaskManager()
    try:
        collapsed = task_manager.get_task_collapsed(task_id)
        return {"task_id": task_id, "collapsed": collapsed}
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"获取事务折叠状态失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.post("/tasks/{task_id}/collapsed")
async def set_task_collapsed(task_id: int, collapsed: bool = True) -> dict[str, Any]:
    """设置事务的折叠状态"""
    task_manager = TaskManager()
    try:
        success = task_manager.set_task_collapsed(task_id, collapsed)
        if not success:
            raise HTTPException(status_code=404, detail="事务不存在")
        return {"task_id": task_id, "collapsed": collapsed, "success": success}
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"设置事务折叠状态失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


def _strip_task_context(tasks: list[dict[str, Any]]) -> None:
    """从事务列表中递归移除 context 字段"""
    for task in tasks:
        task.pop("context", None)
        children = task.get("children")
        if children:
            _strip_task_context(children)


@router.get("/tasks")
async def get_tasks(
    status: str | None = None,
    include_completed: bool = False,
    for_displayed_messages: bool = False,
    include_archived: bool = False,
    message_ids: str | None = None,
) -> list[dict[str, Any]]:
    """获取事务树"""
    # 🔧 Task#215: 将同步DB操作放到线程池，避免阻塞uvicorn事件循环
    frontend_message_ids = None
    if message_ids is not None:
        try:
            parsed = [int(x.strip()) for x in message_ids.split(",") if x.strip()]
            frontend_message_ids = set(parsed)
        except (ValueError, TypeError):
            frontend_message_ids = None

    def _do_query():
        task_manager = TaskManager()
        tasks = task_manager.list_tasks(status=status, exclude_fields=["context", "checklist", "report"])

        if for_displayed_messages:
            chain_head_ids = None
            if frontend_message_ids is not None:
                chain_head_ids = frontend_message_ids
            else:
                msg_manager = MessageManager()
                if msg_manager is not None:
                    chain_head_messages = msg_manager.get_active_chains(include_archived=include_archived)
                    chain_head_ids = {m["id"] for m in chain_head_messages}

            if chain_head_ids is not None:
                original_count = len(tasks)
                tasks = [t for t in tasks if t.get("message_id") in chain_head_ids]
                filtered_count = len(tasks)
                if filtered_count < original_count:
                    logger.debug(f"事务过滤: {original_count} -> {filtered_count}")

        if not include_completed and status is None:
            terminal_statuses = {"completed", "failed", "cancelled"}
            active_parent_ids = set()
            for t in tasks:
                if t.get("status") not in terminal_statuses and t.get("parent_task_id"):
                    parent_id = t.get("parent_task_id")
                    while parent_id and parent_id not in active_parent_ids:
                        active_parent_ids.add(parent_id)
                        parent_task = next((pt for pt in tasks if pt["id"] == parent_id), None)
                        parent_id = parent_task.get("parent_task_id") if parent_task else None

            tasks = [
                t
                for t in tasks
                if t.get("status") not in terminal_statuses or t["id"] in active_parent_ids
            ]

        task_map = {t["id"]: t for t in tasks}
        root_tasks = []

        for task in tasks:
            parent_id = task.get("parent_task_id")
            if parent_id and parent_id in task_map:
                parent = task_map[parent_id]
                if "children" not in parent:
                    parent["children"] = []
                parent["children"].append(task)
            else:
                root_tasks.append(task)

        _strip_task_context(root_tasks)
        return root_tasks

    try:
        return await asyncio.to_thread(_do_query)
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"获取事务列表失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.get("/tasks/{task_id}")
async def get_task(task_id: int) -> dict[str, Any]:
    """获取事务详情"""
    task_manager = TaskManager()
    try:
        task: dict[str, Any] | None = task_manager.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="事务不存在")

        children = task_manager.db.get_children(task_id)
        task["children"] = children
        return task
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"获取事务详情失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.get("/tasks/{task_id}/detail-html")
async def get_task_detail_html(task_id: int) -> dict[str, Any]:
    """获取事务详情完整 HTML（WebUI 事务详情弹窗内容）。

    🔧 事务#2246：原先前端通过 /api/command 启动 bin/task 子进程生成
    （解释器启动 + uv_sync + venv 重启 + 全量 import + 新建 DB 连接 ≈0.7s，
    且 subprocess.run 阻塞事件循环）。现改为服务进程内直接渲染，
    复用单例 TaskManager 的 DB 连接，响应从 ~700ms 降至几十毫秒。

    使用 asyncio.to_thread 执行：1) 不阻塞事件循环；2) 工作线程内无运行中的
    事件循环，渲染函数对远程 nest AGENTS.md 的 asyncio.run 读取分支可正常
    工作，行为与原 CLI 子进程模式完全一致。

    🔧 事务#2246（远程 nest 卡死修复）：渲染为同步函数跑在 to_thread 工作线程里，
    内部若对远程 nest AGENTS.md 用 asyncio.run，会在临时新循环上跨循环 await 主循环
    future → 卡满超时（实测远程 nest 事务 ~30s）。故远程 nest 的 AGENTS.md 在**主
    事件循环**上 await load_task_agents_md 预取（实测 0.00s），经 override 注入渲染。
    """
    try:
        from peng.cli._task_impl import render_task_detail_html

        # 🔧 远程 nest：在主事件循环上预取工作目录 AGENTS.md（避免跨事件循环卡死）；
        #     本地 nest 传 None，由渲染函数内纯同步本地直读（无跨循环，行为不变）。
        agents_md_override: str | None = None
        task_manager = TaskManager()
        task_row = task_manager.get_task(task_id)
        if task_row is not None and (task_row.get("nest") or "local") != "local":
            from peng.llm.client import load_task_agents_md

            agents_md_override = await load_task_agents_md(
                {"nest": task_row.get("nest"), "workdir": task_row.get("workdir")}
            )

        html = await asyncio.to_thread(render_task_detail_html, task_id, agents_md_override)
        if html is None:
            raise HTTPException(status_code=404, detail="事务不存在")
        return {"success": True, "output": html}
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"获取事务详情HTML失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.get("/tasks/{task_id}/context-info")
async def get_task_context_info(task_id: int) -> dict[str, Any]:
    """获取事务上下文摘要信息（tokens、费用、分项用量）。

    用于 WebUI hover tooltip 显示，轻量级接口。
    """
    task_manager = TaskManager()
    try:
        task = task_manager.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="事务不存在")

        context = task.get("context")
        provider = task.get("provider", "")

        def _compute():
            from peng.task.database import (
                get_context_tokens,
                get_context_cost,
                get_context_usage_detail,
                get_context_color,
            )

            tokens = get_context_tokens(context)
            cost = get_context_cost(context)
            usage = get_context_usage_detail(context)
            color_name = ""
            if tokens > 2000:
                color_name = get_context_color(tokens, provider)
            return tokens, cost, usage, color_name

        tokens, cost, usage, color_name = await asyncio.to_thread(_compute)

        # 从 provider 配置获取单价（元/百万tokens）
        price_input_hit = 0
        price_input_miss = 0
        price_output = 0
        if provider:
            try:
                from peng.config.loader import load_config

                cfg = load_config()
                pcfg = cfg.llm.get_provider(current=provider, locked=True)
                price_input_hit = pcfg.get("price_input_hit", 0) or 0
                price_input_miss = pcfg.get("price_input_miss", 0) or 0
                price_output = pcfg.get("price_output", 0) or 0
            except Exception:
                pass

        return {
            "task_id": task_id,
            "tokens": tokens,
            "cost": round(cost, 2),
            "input_hit": usage["input_hit"],
            "input_miss": usage["input_miss"],
            "output": usage["output"],
            "price_input_hit": price_input_hit,
            "price_input_miss": price_input_miss,
            "price_output": price_output,
            "provider": provider,
            "context_color": color_name,
        }
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"获取事务上下文摘要失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.post("/tasks/{task_id}/action")
async def task_action(task_id: int, request: TaskActionRequest) -> dict[str, Any]:
    """执行事务操作（暂停/恢复/取消/重启/开始执行）"""
    task_manager = TaskManager()
    try:
        action = request.action

        if action == "pause":
            await task_manager.pause_task(task_id, reason=f"WebUI 操作：pause")
            message = "事务已暂停"
        elif action == "resume":
            await task_manager.resume_task(task_id, reason=f"WebUI 操作：resume")
            message = "事务已恢复"
        elif action == "cancel":
            await task_manager.cancel_task(task_id, reason=f"WebUI 操作：cancel")
            message = "事务已取消"
        elif action == "restart":
            await task_manager.restart_task(task_id, reason=f"WebUI 操作：restart")
            message = "事务已重启"
        elif action == "start":
            task = task_manager.get_task(task_id)
            if task and task.get("status") == "pending":
                task_manager.start_task(task_id)
                task = task_manager.get_task(task_id)
                task_manager._execute_task_process(task_id)
                message = "事务已开始执行"
            else:
                raise HTTPException(status_code=400, detail="只有 pending 状态的事务可以开始执行")
        else:
            raise HTTPException(status_code=400, detail=f"未知操作: {action}")

        from ..websocket import broadcast_task_update, broadcast_message_update
        await broadcast_task_update(task_id, action)
        # 🔧 Task#382：暂停/取消操作时，同步广播消息状态更新，确保前端消息状态实时更新
        if action in ("pause", "cancel"):
            task = task_manager.get_task(task_id)
            if task and task.get("message_id"):
                await broadcast_message_update(
                    task["message_id"], action, source="webui"
                )
        return {"success": True, "message": message}

    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"事务操作失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.post("/tasks/{task_id}/move")
async def move_task(task_id: int, request: MoveTaskRequest) -> dict[str, Any]:
    """移动事务到新的父事务下或变成树根"""
    task_manager = TaskManager()
    try:
        # 🔧 Task#1893：移动前记录原父事务ID（供前端撤销）
        previous_parent_task_id = None
        task_before = task_manager.get_task(task_id)
        if task_before:
            previous_parent_task_id = task_before.get("parent_task_id")

        success = task_manager.db.move_task(task_id=task_id, target_id=request.target_id)
        if not success:
            raise HTTPException(status_code=400, detail=f"事务 {task_id} 移动失败，请检查事务状态")

        task = task_manager.get_task(task_id)

        from ..websocket import broadcast_task_update
        await broadcast_task_update(task_id, "moved")

        return {
            "success": True,
            "message": "事务移动成功",
            "task": task,
            "previous_parent_task_id": previous_parent_task_id,  # 🔧 Task#1893
        }
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"事务移动失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: int) -> dict[str, Any]:
    """删除事务及其所有子事务（树根事务不可删除）"""
    task_manager = TaskManager()
    try:
        task = task_manager.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="事务不存在")

        if not task.get("parent_task_id"):
            raise HTTPException(status_code=400, detail="树根事务不可删除")

        # 🔧 Task#1893：删除前备份快照（供撤销删除恢复，失败不阻塞删除）
        from .undelete import backup_for_delete

        backup_file = await asyncio.to_thread(backup_for_delete, "task", task_id)

        deleted_count = task_manager.db.delete_task_recursive(task_id)

        from ..websocket import broadcast_task_update
        await broadcast_task_update(task_id, "deleted")

        return {
            "success": True,
            "message": f"事务已删除，共 {deleted_count} 个事务",
            "deleted_count": deleted_count,
            "backup_file": backup_file,  # 🔧 Task#1893：撤销删除凭据
        }
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"删除事务失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.post("/tasks/{task_id}/top")
async def toggle_task_top(task_id: int) -> dict[str, Any]:
    """切换根事务的顶置状态，同时同步关联根消息的顶置"""
    try:
        task_manager = TaskManager()
        task = task_manager.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="事务不存在")
        # 只有根事务可以顶置
        if task.get("parent_task_id"):
            raise HTTPException(status_code=400, detail="只有根事务可以顶置")

        from peng.webui.routes.workdir_config import _load_webui_json, _save_webui_json

        config = _load_webui_json()

        if "webui" not in config:
            config["webui"] = {}

        topped_task_ids: list[int] = config["webui"].get("topped_task_ids", [])
        topped_msg_ids: list[int] = config["webui"].get("topped_message_ids", [])

        if task_id in topped_task_ids:
            topped_task_ids.remove(task_id)
            topped = False
        else:
            topped_task_ids.insert(0, task_id)
            topped = True

        config["webui"]["topped_task_ids"] = topped_task_ids

        # 🔧 同步关联根消息的顶置状态
        msg_id = task.get("message_id")
        if msg_id:
            if topped and msg_id not in topped_msg_ids:
                topped_msg_ids.insert(0, msg_id)
            elif not topped and msg_id in topped_msg_ids:
                topped_msg_ids.remove(msg_id)
            config["webui"]["topped_message_ids"] = topped_msg_ids

        _save_webui_json(config)

        return {
            "success": True, "topped": topped,
            "topped_task_ids": topped_task_ids,
            "topped_message_ids": topped_msg_ids,
        }
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"切换事务顶置失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.post("/internal/broadcast-task")
async def internal_broadcast_task(request: BroadcastTaskRequest) -> dict[str, Any]:
    """内部接口：子进程通过 HTTP 请求触发主进程 WebSocket 广播"""
    try:
        from ..websocket import broadcast_task_update
        await broadcast_task_update(request.task_id, request.action, request.details)
        return {"success": True}
    except Exception as e:
        logger.error(f"内部广播失败: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e)) from None

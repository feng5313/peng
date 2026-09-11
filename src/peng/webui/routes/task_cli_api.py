#!/usr/bin/env python3
"""
Task CLI API 路由

为 bin/task 提供 RESTful API，避免 CLI 直接操作数据库。
所有路由前缀: /api/cli/task/

设计原则：
- 薄路由层：直接调用 TaskManager/MessageManager
- 返回 JSON 数据，由 CLI 端负责格式化显示
- 统一错误处理
"""

from __future__ import annotations

import logging
import traceback
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from peng.task.manager import TaskManager
from peng.task.info import parse_info_to_list_with_status, serialize_info_list
from peng.common.daemon_registry import (
    cleanup_dead_daemons,
    kill_all_daemons_sync,
    kill_task_daemons_sync,
    list_daemons,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cli/task", tags=["cli-task"])


# ==================== Pydantic 模型 ====================


class FieldUpdateRequest(BaseModel):
    """字段更新请求"""
    value: str = ""
    file: str | None = None  # 文件路径（读取后删除）


class SubtaskCreateRequest(BaseModel):
    """创建子事务请求"""
    content: str
    prompt: str | None = None
    workdir: str | None = None
    small_model: bool = False
    creator: str = ""
    file: str | None = None  # 文件路径（读取后删除）


class TaskControlRequest(BaseModel):
    """事务控制请求"""
    action: str  # pause/cancel/up/down/restart/resume/start


class StatusUpdateRequest(BaseModel):
    """状态更新请求"""
    status: str  # pending/running/completed/failed


class InfoUpdateRequest(BaseModel):
    """Info 更新请求"""
    info: str = ""
    delete: bool = False
    unread: bool = False
    read: bool = False
    file: str | None = None


class TimerUpdateRequest(BaseModel):
    """定时器更新请求"""
    seconds: int | None = None
    delete: bool = False


class TaskMoveRequest(BaseModel):
    """事务移动请求"""
    target_id: int | None = None


# ==================== 辅助函数 ====================


def _get_task_manager() -> TaskManager:
    """获取 TaskManager 实例"""
    return TaskManager()


def _read_file_content(file_path: str) -> str:
    """读取文件内容并删除临时文件"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        try:
            import os
            os.remove(file_path)
        except OSError:
            pass
        return content
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"读取文件失败: {e}")


def _get_task_or_404(task_id: int) -> dict[str, Any]:
    """获取事务或返回 404"""
    tm = _get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"事务 {task_id} 不存在")
    return task


# ==================== API 路由 ====================


@router.get("/trees")
async def get_trees(
    all: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """获取事务树列表"""
    tm = _get_task_manager()
    mm = None
    from datetime import datetime

    if not all:
        # 检查归档
        all_root_tasks = tm.db.list_tasks(
            limit=None, root_only=True, include_archived=True
        )
        from peng.message.manager import MessageManager

        mm = MessageManager()
        now = datetime.now().isoformat()
        for task in all_root_tasks:
            message_id = task.get("message_id")
            if message_id:
                msg = mm.get_message(message_id)
                if msg and msg.get("is_archived"):
                    if not task.get("is_archived"):
                        tm.db.update_task(task["id"], is_archived=1, archived_at=now)

    root_tasks = tm.db.list_tasks(
        limit=limit,
        root_only=True,
        include_archived=all,
    )

    trees = []
    for root in root_tasks:
        tree_text = tm.db.get_tree_text(root["id"], content_max_len=80)
        trees.append(
            {
                "task": root,
                "tree_text": tree_text,
            }
        )

    return {"trees": trees, "count": len(root_tasks), "label": "所有根事务树" if all else "未归档的根事务树"}


@router.get("/{task_id}")
async def get_task_detail(task_id: int) -> dict[str, Any]:
    """获取事务详情"""
    task = _get_task_or_404(task_id)
    tm = _get_task_manager()

    # 获取子事务
    children = tm.get_children(task_id)
    task["children"] = children

    # 获取依赖
    deps = tm.get_dependencies(task_id)
    dependents = tm.get_dependents(task_id)
    task["dependencies"] = []
    for dep_id in deps:
        dep = tm.get_task(dep_id)
        if dep:
            task["dependencies"].append(dep)
    task["dependents"] = []
    for dep_id in dependents:
        dep = tm.get_task(dep_id)
        if dep:
            task["dependents"].append(dep)

    return task


@router.get("/{task_id}/tree")
async def get_task_tree(task_id: int) -> dict[str, Any]:
    """获取事务分支树"""
    task = _get_task_or_404(task_id)
    tm = _get_task_manager()

    tree_text = tm.db.get_tree_text(task_id, content_max_len=0)
    descendants = tm.db.get_descendants(task_id)

    return {
        "task": task,
        "tree_text": tree_text,
        "count": len(descendants) + 1,
    }


@router.get("/{task_id}/full-tree")
async def get_task_full_tree(task_id: int) -> dict[str, Any]:
    """获取事务完整树（从根事务开始）"""
    task = _get_task_or_404(task_id)
    tm = _get_task_manager()

    root_id = task.get("root_id") or task_id
    tree_text = tm.db.get_tree_text(root_id, content_max_len=0)
    subtree = tm.get_subtree(root_id)

    return {
        "task": task,
        "root_id": root_id,
        "tree_text": tree_text,
        "count": len(subtree),
    }


@router.get("/{task_id}/messages")
async def get_task_messages(task_id: int) -> dict[str, Any]:
    """获取事务关联的消息链"""
    task = _get_task_or_404(task_id)
    tm = _get_task_manager()

    from peng.message.manager import MessageManager

    mm = MessageManager()

    # 找到根事务
    root_id = task.get("root_id") or task_id
    root_task = tm.get_task(root_id) if root_id != task_id else task
    if not root_task:
        raise HTTPException(status_code=404, detail=f"未找到根事务 #{root_id}")

    message_id = root_task.get("message_id")
    if not message_id:
        return {"chain_context": None, "message_id": None}

    root_msg = mm.get_message(message_id)
    if not root_msg:
        return {"chain_context": None, "message_id": message_id}

    return {
        "chain_context": root_msg.get("chain_context"),
        "message_id": message_id,
        "root_id": root_id,
    }


@router.get("/{task_id}/context")
async def get_task_context(task_id: int) -> dict[str, Any]:
    """获取/设置事务上下文"""
    task = _get_task_or_404(task_id)
    return {
        "context": task.get("context", ""),
        "task_id": task_id,
    }


@router.put("/{task_id}/context")
async def set_task_context(task_id: int, request: FieldUpdateRequest) -> dict[str, Any]:
    """设置事务上下文"""
    _get_task_or_404(task_id)
    tm = _get_task_manager()

    value = request.value
    if request.file:
        value = _read_file_content(request.file)

    if request.file and not value:
        # 文件读取后为空，不做操作
        pass

    success = tm.db.update_task(task_id, context=value)
    if not success:
        raise HTTPException(status_code=500, detail="无法更新事务上下文")
    return {"success": True, "field": "context", "value": value}


class ContextMessageOperationRequest(BaseModel):
    """上下文消息操作请求"""
    message_index: int  # 消息索引（0-based）


@router.delete("/{task_id}/context/message")
async def delete_context_message(task_id: int, request: ContextMessageOperationRequest) -> dict[str, Any]:
    """删除上下文中指定索引的消息（含其关联的 tool 消息）

    删除规则（按迭代完整删除）：
    - 如果删除 user 消息：同时删除该 user 后面连续的 assistant + tool 消息
    - 如果删除 assistant 消息：同时删除该 assistant 后面连续的 tool 消息
    - 如果删除 tool 消息：仅删除该 tool 消息本身
    """
    import json as _json

    task = _get_task_or_404(task_id)
    tm = _get_task_manager()

    context_str = task.get("context", "")
    try:
        context_data = _json.loads(context_str) if context_str else {}
    except Exception:
        raise HTTPException(status_code=500, detail="上下文JSON解析失败")

    if isinstance(context_data, dict):
        messages = context_data.get("messages", [])
    else:
        messages = context_data
        context_data = {"messages": messages}

    idx = request.message_index
    if idx < 0 or idx >= len(messages):
        raise HTTPException(status_code=400, detail=f"消息索引 {idx} 超出范围(0-{len(messages)-1})")

    role = messages[idx].get("role", "")
    indices_to_delete = {idx}

    # 收集关联消息：向后扫描连续的 tool 消息
    j = idx + 1
    while j < len(messages):
        next_role = messages[j].get("role", "")
        if role == "user" and next_role in ("assistant", "tool"):
            indices_to_delete.add(j)
            if next_role == "tool":
                j += 1
                continue
            else:
                # assistant 消息后还可能有 tool 消息
                j += 1
                continue
        elif role == "assistant" and next_role == "tool":
            indices_to_delete.add(j)
            j += 1
            continue
        break

    # 过滤掉被删除的消息
    new_messages = [msg for i, msg in enumerate(messages) if i not in indices_to_delete]
    context_data["messages"] = new_messages
    new_context = _json.dumps(context_data, ensure_ascii=False, indent=2)

    success = tm.db.update_task(task_id, context=new_context)
    if not success:
        raise HTTPException(status_code=500, detail="无法更新事务上下文")
    return {"success": True, "deleted_count": len(indices_to_delete)}


@router.post("/{task_id}/context/truncate")
async def truncate_context_from(task_id: int, request: ContextMessageOperationRequest) -> dict[str, Any]:
    """从指定消息索引开始截断所有后续消息（包含该索引）

    截断规则：删除从 message_index 开始的所有后续消息
    """
    import json as _json

    task = _get_task_or_404(task_id)
    tm = _get_task_manager()

    context_str = task.get("context", "")
    try:
        context_data = _json.loads(context_str) if context_str else {}
    except Exception:
        raise HTTPException(status_code=500, detail="上下文JSON解析失败")

    if isinstance(context_data, dict):
        messages = context_data.get("messages", [])
    else:
        messages = context_data
        context_data = {"messages": messages}

    idx = request.message_index
    if idx < 0 or idx >= len(messages):
        raise HTTPException(status_code=400, detail=f"消息索引 {idx} 超出范围(0-{len(messages)-1})")

    # 截断：保留 0 到 idx-1 的消息
    removed_count = len(messages) - idx
    new_messages = messages[:idx]
    context_data["messages"] = new_messages
    new_context = _json.dumps(context_data, ensure_ascii=False, indent=2)

    success = tm.db.update_task(task_id, context=new_context)
    if not success:
        raise HTTPException(status_code=500, detail="无法更新事务上下文")
    return {"success": True, "removed_count": removed_count}


@router.post("/{task_id}/subtask")
async def create_subtask(task_id: int, request: SubtaskCreateRequest) -> dict[str, Any]:
    """创建子事务"""
    tm = _get_task_manager()
    parent = tm.get_task(task_id)
    if not parent:
        raise HTTPException(status_code=404, detail=f"父事务 {task_id} 不存在")

    content = request.content
    if request.file:
        file_content = _read_file_content(request.file)
        if file_content:
            content = file_content

    if not content:
        raise HTTPException(status_code=400, detail="缺少子事务需求描述")

    # 自动标记为复杂事务
    if not parent.get("is_complex"):
        tm.db.update_task(task_id, is_complex=1)

    try:
        child_id = tm.create_subtask(
            parent_task_id=task_id,
            content=content,
            description=None,
            small_model=request.small_model,
            prompt=request.prompt,
            workdir=request.workdir,
            creator=request.creator,
        )
        if not child_id:
            raise HTTPException(status_code=500, detail="创建子事务失败")

        return {
            "success": True,
            "child_id": child_id,
            "parent_id": task_id,
            "parent_content": parent.get("content", ""),
            "content": content,
            "prompt": request.prompt,
            "workdir": request.workdir,
            "creator": request.creator,
            "small_model": request.small_model,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建失败: {traceback.format_exc()}")


@router.post("/{task_id}/control")
async def task_control(task_id: int, request: TaskControlRequest) -> dict[str, Any]:
    """事务控制操作（pause/cancel/up/down/restart/resume/start）"""
    task = _get_task_or_404(task_id)
    tm = _get_task_manager()
    action = request.action

    # 递归收集子事务
    def collect_recursive(tid: int) -> list[int]:
        ids = [tid]
        children = tm.get_children(tid)
        for child in children:
            child_id = child.get("id")
            if child_id:
                ids.extend(collect_recursive(child_id))
        return ids

    all_task_ids = []
    if action in ("pause", "cancel", "up", "down"):
        all_task_ids = collect_recursive(task_id)
    else:
        all_task_ids = [task_id]

    # 检查状态
    if action == "pause" and task["status"] in ("completed", "failed", "cancelled"):
        raise HTTPException(status_code=400, detail=f"事务已{task['status']}，无法暂停")

    if action == "start" and task["status"] != "pending":
        raise HTTPException(
            status_code=400, detail=f"只有待执行(pending)状态的事务可以启动"
        )

    if action == "restart" and task["status"] == "running":
        raise HTTPException(status_code=400, detail="事务正在运行，请先暂停或取消")

    # 控制表映射
    control_map = {
        "pause": "pause",
        "cancel": "cancel",
        "up": "resume",
        "down": "kill",
        "restart": "restart",
        "resume": "resume",
        "start": "start",
    }

    control_action = control_map.get(action, action)

    try:
        for tid in all_task_ids:
            tm.db.insert_task_control(tid, control_action)

        action_names = {
            "pause": "暂停",
            "cancel": "取消",
            "up": "提高优先级",
            "down": "降低优先级",
            "restart": "重启",
            "resume": "恢复",
            "start": "启动",
        }
        return {
            "success": True,
            "action": action,
            "action_name": action_names.get(action, action),
            "task_id": task_id,
            "affected_count": len(all_task_ids),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"控制操作失败: {e}")


@router.get("/{task_id}/status")
async def get_task_status(task_id: int) -> dict[str, Any]:
    """获取事务状态"""
    task = _get_task_or_404(task_id)
    return {"task_id": task_id, "status": task.get("status", "unknown")}


@router.put("/{task_id}/status")
async def set_task_status(task_id: int, request: StatusUpdateRequest) -> dict[str, Any]:
    """设置事务状态"""
    task = _get_task_or_404(task_id)
    tm = _get_task_manager()

    current_status = task.get("status")
    success = tm.update_task_status(task_id, current_status, request.status)

    if not success:
        raise HTTPException(status_code=500, detail="无法更新事务状态")

    return {"success": True, "task_id": task_id, "status": request.status}


@router.delete("/{task_id}")
async def delete_task(task_id: int, force: bool = False) -> dict[str, Any]:
    """递归删除事务"""
    task = _get_task_or_404(task_id)
    tm = _get_task_manager()

    if not force and task.get("status") == "running":
        raise HTTPException(status_code=400, detail="事务正在处理中，禁止删除，请先停止事务")

    try:
        deleted_count = tm.db.delete_task_recursive(task_id, force)
        return {"success": True, "deleted_count": deleted_count, "task_id": task_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除失败: {e}")


@router.post("/{task_id}/kill")
async def kill_task(task_id: int) -> dict[str, Any]:
    """杀死指定事务的所有后台daemon进程"""
    _get_task_or_404(task_id)

    import asyncio

    cleaned = await asyncio.to_thread(cleanup_dead_daemons)
    result = await asyncio.to_thread(kill_task_daemons_sync, task_id)

    return {
        "cleaned": cleaned,
        "killed": result["killed"],
        "already_dead": result["already_dead"],
        "errors": result["errors"],
        "total": len(result["killed"]) + len(result["already_dead"]) + len(result["errors"]),
    }


@router.post("/killall")
async def kill_all_tasks() -> dict[str, Any]:
    """杀死所有后台daemon进程"""
    import asyncio

    cleaned = await asyncio.to_thread(cleanup_dead_daemons)
    result = await asyncio.to_thread(kill_all_daemons_sync)

    return {
        "cleaned": cleaned,
        "killed": result["killed"],
        "already_dead": result["already_dead"],
        "errors": result["errors"],
        "total": len(result["killed"]) + len(result["already_dead"]) + len(result["errors"]),
        "task_count": len({str(e.get("task_id") or "") for e in result["killed"] + result["already_dead"] + result["errors"]}),
    }


@router.get("/daemon")
async def get_daemon(nest: str | None = None) -> dict[str, Any]:
    """显示所有已注册的后台daemon

    Args:
        nest: 按nest过滤（"local"=本地，其他=远程nest名称，None=全部）
    """
    import asyncio

    nest_filter = None
    if nest:
        if nest.lower() == "local":
            nest_filter = "local"
        else:
            nest_filter = nest

    cleaned = await asyncio.to_thread(cleanup_dead_daemons)
    all_data = await asyncio.to_thread(list_daemons)

    tm = _get_task_manager()
    # 按task分组
    task_groups: dict[str, list] = {}
    for daemon_id, entry in all_data.items():
        entry_nest = entry.get("nest")
        # 按nest过滤
        if nest_filter is None:
            matched = True
        elif nest_filter == "local":
            matched = entry_nest is None
        else:
            matched = entry_nest == nest_filter
        if matched:
            task_groups.setdefault(str(entry.get("task_id") or ""), []).append(entry)

    tasks = []
    for task_key, entries in task_groups.items():
        task = None
        if task_key:
            task = tm.get_task(int(task_key))
        tasks.append({
            "task_id": int(task_key) if task_key else None,
            "pids": [e["pid"] for e in entries],
            "entries": entries,  # 完整daemon条目（含id/command/nest/log_file）
            "task": task,
        })

    return {
        "cleaned": cleaned,
        "tasks": tasks,
        "task_count": len(tasks),
        "process_count": sum(len(t["entries"]) for t in tasks),
        "nest_filter": nest_filter,
    }


@router.post("/{task_id}/move")
async def move_task(task_id: int, request: TaskMoveRequest) -> dict[str, Any]:
    """移动事务"""
    _get_task_or_404(task_id)
    tm = _get_task_manager()

    success = tm.db.move_task(task_id=task_id, target_id=request.target_id)
    if not success:
        raise HTTPException(status_code=400, detail="移动失败，请检查事务状态")

    target_desc = f"#{request.target_id}" if request.target_id else "根"
    return {"success": True, "task_id": task_id, "target": target_desc}


@router.put("/{task_id}/small-model")
async def set_small_model(task_id: int) -> dict[str, Any]:
    """设置事务使用小模型"""
    _get_task_or_404(task_id)
    tm = _get_task_manager()
    success = tm.db.update_task(task_id, small_model=1)
    if not success:
        raise HTTPException(status_code=500, detail="无法更新事务")
    return {"success": True, "task_id": task_id, "small_model": True}


@router.put("/{task_id}/big-model")
async def set_big_model(task_id: int) -> dict[str, Any]:
    """设置事务使用大模型"""
    _get_task_or_404(task_id)
    tm = _get_task_manager()
    success = tm.db.update_task(task_id, small_model=0)
    if not success:
        raise HTTPException(status_code=500, detail="无法更新事务")
    return {"success": True, "task_id": task_id, "small_model": False}


@router.get("/{task_id}/field/{field_name}")
async def get_task_field(task_id: int, field_name: str) -> dict[str, Any]:
    """获取事务字段值"""
    task = _get_task_or_404(task_id)
    value = task.get(field_name)
    return {"task_id": task_id, "field": field_name, "value": value}


@router.put("/{task_id}/field/{field_name}")
async def set_task_field(task_id: int, field_name: str, request: FieldUpdateRequest) -> dict[str, Any]:
    """设置事务字段值"""
    _get_task_or_404(task_id)
    tm = _get_task_manager()

    value = request.value
    if request.file:
        value = _read_file_content(request.file)

    # 特殊字段清空值
    if value == "" and field_name in ("info", "prompt", "metadata"):
        empty_value = "[]"
    else:
        empty_value = ""

    # 如果 delete 标记（通过空字符串触发）
    if request.file and not value:
        if field_name in ("info", "prompt", "metadata"):
            empty_value = "[]"
        value = empty_value

    success = tm.db.update_task(task_id, **{field_name: value})
    if not success:
        raise HTTPException(status_code=500, detail="无法更新事务")

    result: dict[str, Any] = {"success": True, "task_id": task_id, "field": field_name, "value": value}

    # 如果是 content 字段且为根事务，同步更新关联的根消息 content
    if field_name == "content" and value:
        task = tm.get_task(task_id)
        if task and not task.get("parent_task_id"):
            message_id = task.get("message_id")
            if message_id:
                from peng.message.database import MessageDatabase

                msg_db = MessageDatabase()
                msg = msg_db.get_message(message_id)
                if msg and msg.get("reply_to") is None:
                    msg_success = msg_db.update_message(message_id, content=value)
                    result["message_synced"] = msg_success
                    result["message_id"] = message_id

    return result


@router.delete("/{task_id}/field/{field_name}")
async def clear_task_field(task_id: int, field_name: str) -> dict[str, Any]:
    """清空事务字段"""
    _get_task_or_404(task_id)
    tm = _get_task_manager()

    if field_name in ("info", "prompt", "metadata"):
        empty_value = "[]"
    else:
        empty_value = ""

    success = tm.db.update_task(task_id, **{field_name: empty_value})
    if not success:
        raise HTTPException(status_code=500, detail="无法更新事务")

    return {"success": True, "task_id": task_id, "field": field_name, "cleared": True}


@router.get("/{task_id}/info")
async def get_task_info(task_id: int) -> dict[str, Any]:
    """获取事务信息（列表格式）"""
    task = _get_task_or_404(task_id)
    current_info = task.get("info", "[]") or "[]"
    info_list = parse_info_to_list_with_status(current_info)

    return {
        "task_id": task_id,
        "info_list": info_list,
        "count": len(info_list),
        "unread_count": sum(1 for item in info_list if not item["read"]),
    }


@router.post("/{task_id}/info")
async def add_task_info(task_id: int, request: InfoUpdateRequest) -> dict[str, Any]:
    """添加/修改事务信息"""
    task = _get_task_or_404(task_id)
    tm = _get_task_manager()

    if request.delete:
        success = tm.db.update_task(task_id, info="[]")
        return {"success": success, "action": "clear", "task_id": task_id}

    if request.unread or request.read:
        current_info = task.get("info", "[]") or "[]"
        info_list = parse_info_to_list_with_status(current_info)
        for item in info_list:
            item["read"] = request.read
        new_value = serialize_info_list(info_list)
        success = tm.db.update_task(task_id, info=new_value)
        return {
            "success": success,
            "action": "mark_all_" + ("read" if request.read else "unread"),
            "task_id": task_id,
            "count": len(info_list),
        }

    # 添加新信息
    info_value = request.info
    if request.file:
        info_value = _read_file_content(request.file)

    if info_value:
        current_info = task.get("info", "[]") or "[]"
        info_list = parse_info_to_list_with_status(current_info)
        info_list.append({"content": info_value, "read": False})
        new_value = serialize_info_list(info_list)
        success = tm.db.update_task(task_id, info=new_value)
        if success:
            # 🔧 Task#1449：只有当事务不在running状态时才插入resume控制信号
            # 如果事务已在running，info会在下一个迭代自动被检测到并注入
            # 避免running状态下强制重启导致多进程竞态调用LLM API（引发HTTP 400错误）
            current_status = task.get("status", "")
            if current_status != "running":
                tm.db.insert_task_control(task_id, "resume")
        return {
            "success": success,
            "action": "add",
            "task_id": task_id,
            "added": info_value,
            "count": len(info_list),
            "unread_count": sum(1 for item in info_list if not item["read"]),
            "task_running": current_status == "running",
        }

    return {"success": False, "action": "none", "task_id": task_id}


@router.delete("/{task_id}/info")
async def clear_task_info(task_id: int) -> dict[str, Any]:
    """清空事务信息"""
    _get_task_or_404(task_id)
    tm = _get_task_manager()
    success = tm.db.update_task(task_id, info="[]")
    return {"success": success, "task_id": task_id}


class InfoItemDeleteRequest(BaseModel):
    """删除单条 info 项"""
    index: int  # info 项索引（0-based）


@router.delete("/{task_id}/info/item")
async def delete_task_info_item(task_id: int, request: InfoItemDeleteRequest) -> dict[str, Any]:
    """删除事务信息中的单条记录"""
    task = _get_task_or_404(task_id)
    tm = _get_task_manager()

    current_info = task.get("info", "[]") or "[]"
    info_list = parse_info_to_list_with_status(current_info)

    idx = request.index
    if idx < 0 or idx >= len(info_list):
        raise HTTPException(status_code=400, detail=f"info索引 {idx} 超出范围(0-{len(info_list)-1})")

    # 删除指定索引的项
    removed_item = info_list.pop(idx)
    new_value = serialize_info_list(info_list)
    success = tm.db.update_task(task_id, info=new_value)

    return {
        "success": success,
        "task_id": task_id,
        "removed_content": removed_item.get("content", ""),
        "remaining_count": len(info_list),
    }


class InfoItemReadRequest(BaseModel):
    """修改单条 info 项的已读状态"""
    index: int  # info 项索引（0-based）
    read: bool  # True=标记已读，False=标记未读


@router.put("/{task_id}/info/item/read")
async def set_task_info_item_read(task_id: int, request: InfoItemReadRequest) -> dict[str, Any]:
    """修改事务信息中单条记录的已读状态"""
    task = _get_task_or_404(task_id)
    tm = _get_task_manager()

    current_info = task.get("info", "[]") or "[]"
    info_list = parse_info_to_list_with_status(current_info)

    idx = request.index
    if idx < 0 or idx >= len(info_list):
        raise HTTPException(status_code=400, detail=f"info索引 {idx} 超出范围(0-{len(info_list)-1})")

    info_list[idx]["read"] = request.read
    new_value = serialize_info_list(info_list)
    success = tm.db.update_task(task_id, info=new_value)

    return {
        "success": success,
        "task_id": task_id,
        "index": idx,
        "read": request.read,
    }


@router.get("/{task_id}/timer")
async def get_task_timer(task_id: int) -> dict[str, Any]:
    """获取事务定时器"""
    task = _get_task_or_404(task_id)
    timer_value = task.get("timer", 0)
    started_at = task.get("started_at")

    result: dict[str, Any] = {
        "task_id": task_id,
        "timer": timer_value,
        "started_at": started_at,
    }

    if timer_value > 0 and started_at:
        from datetime import datetime
        last_start_time = datetime.fromisoformat(started_at)
        next_start_time = last_start_time.timestamp() + timer_value
        result["next_start"] = datetime.fromtimestamp(next_start_time).isoformat()

    return result


@router.put("/{task_id}/timer")
async def set_task_timer(task_id: int, request: TimerUpdateRequest) -> dict[str, Any]:
    """设置事务定时器"""
    _get_task_or_404(task_id)
    tm = _get_task_manager()

    if request.delete:
        success = tm.db.update_task(task_id, timer=0)
        return {"success": success, "action": "clear", "task_id": task_id}

    if request.seconds is not None:
        timer_seconds = request.seconds
        if timer_seconds < 0:
            raise HTTPException(status_code=400, detail="timer值不能为负数")
        success = tm.db.update_task(task_id, timer=timer_seconds)
        return {
            "success": success,
            "action": "set",
            "task_id": task_id,
            "timer": timer_seconds,
        }

    return {"success": False, "action": "none"}


class HeartbeatUpdateRequest(BaseModel):
    """心跳超时更新请求"""
    seconds: int | None = None
    delete: bool = False  # 重置为默认值


@router.put("/{task_id}/heartbeat")
async def set_task_heartbeat(task_id: int, request: HeartbeatUpdateRequest) -> dict[str, Any]:
    """设置事务心跳超时"""
    _get_task_or_404(task_id)
    tm = _get_task_manager()

    if request.delete:
        success = tm.db.update_task(task_id, heartbeat_timeout=0)
        return {"success": success, "action": "reset", "task_id": task_id}

    if request.seconds is not None:
        timeout_seconds = request.seconds
        if timeout_seconds < 0:
            raise HTTPException(status_code=400, detail="心跳超时值不能为负数")
        success = tm.db.update_task(task_id, heartbeat_timeout=timeout_seconds)
        return {
            "success": success,
            "action": "set",
            "task_id": task_id,
            "heartbeat_timeout": timeout_seconds,
        }

    return {"success": False, "action": "none"}


@router.get("/{task_id}/output")
async def get_task_output(
    task_id: int,
    begin: int | None = None,
    end: int | None = None,
    head: int | None = None,
    tail: int | None = None,
    line_count: bool = False,
    verbose: bool = False,
    delete: bool = False,
) -> dict[str, Any]:
    """获取事务输出文件内容"""
    _get_task_or_404(task_id)
    tm = _get_task_manager()
    task = tm.get_task(task_id)

    message_id = task.get("message_id")
    if not message_id:
        return {"error": "事务没有关联消息", "task_id": task_id}

    filename = f"task-{task_id}.out"

    try:
        from peng.config.loader import load_config
        from peng.config import get_project_root

        config = load_config()
        output_dir = (
            Path(config.paths.output_dir)
            if hasattr(config, "paths")
            else get_project_root() / "data" / "output"
        )
    except Exception:
        from peng.config import get_project_root
        output_dir = get_project_root() / "data" / "output"

    filepath = output_dir / filename
    if not filepath.exists():
        return {"error": f"找不到输出文件: {filename}", "filepath": str(filepath)}

    # 删除
    if delete:
        try:
            filepath.unlink()
            return {"success": True, "action": "delete", "filepath": str(filepath)}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"删除文件失败: {e}")

    # 读取
    try:
        lines = filepath.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取文件失败: {e}")

    total_lines = len(lines)

    if line_count:
        return {
            "filepath": str(filepath),
            "task_id": task_id,
            "message_id": message_id,
            "total_lines": total_lines,
        }

    # 处理行号范围
    begin_line = (begin - 1) if begin else 0
    end_line = end if end else total_lines

    if head:
        begin_line = 0
        end_line = min(head, total_lines)
    elif tail:
        begin_line = max(0, total_lines - tail)
        end_line = total_lines

    begin_line = max(0, begin_line)
    end_line = min(total_lines, end_line)

    selected_lines = []
    for i in range(begin_line, end_line):
        line_content = lines[i]
        parts = line_content.split("\t")
        if verbose and len(parts) >= 4:
            selected_lines.append({
                "line_num": i + 1,
                "timestamp": parts[0],
                "output_type": parts[1],
                "task_id_display": parts[2],
                "content": "\t".join(parts[3:]),
            })
        else:
            selected_lines.append({"line_num": i + 1, "content": line_content})

    return {
        "filepath": str(filepath),
        "task_id": task_id,
        "message_id": message_id,
        "total_lines": total_lines,
        "begin": begin_line + 1,
        "end": end_line,
        "lines": selected_lines,
    }


@router.delete("/{task_id}/output")
async def delete_task_output(task_id: int) -> dict[str, Any]:
    """删除事务输出文件"""
    _get_task_or_404(task_id)
    tm = _get_task_manager()
    task = tm.get_task(task_id)

    filename = f"task-{task_id}.out"
    try:
        from peng.config.loader import load_config
        from peng.config import get_project_root

        config = load_config()
        output_dir = (
            Path(config.paths.output_dir)
            if hasattr(config, "paths")
            else get_project_root() / "data" / "output"
        )
    except Exception:
        from peng.config import get_project_root
        output_dir = get_project_root() / "data" / "output"

    filepath = output_dir / filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail=f"找不到输出文件: {filename}")

    try:
        filepath.unlink()
        return {"success": True, "action": "delete", "filepath": str(filepath)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除文件失败: {e}")

"""
PID注册表管理模块

管理事务处理过程中产生的后台进程PID，按task_id分组记录在data/pid.json中。
用于跟踪nohup等不跟随事务进程结束的后台进程。

格式 (v2 - Task#72: nest-aware):
{
    "task_id_1": [
        {"pid": pid1, "nest": null},
        {"pid": pid2, "nest": "remote_nest_name"}
    ],
    "task_id_2": [
        {"pid": pid3, "nest": null}
    ]
}

向后兼容: 旧格式 [pid1, pid2, ...] 会被自动转换为 v2 格式
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from peng.config import get_project_root

logger = logging.getLogger(__name__)

# PID记录文件路径
_PID_FILE_NAME = "pid.json"


def _get_pid_file_path() -> Path:
    """获取pid.json文件路径"""
    return get_project_root() / "data" / _PID_FILE_NAME


def _normalize_pid_entry(entry) -> dict[str, Any]:
    """将PID记录条目标准化为v2格式

    Args:
        entry: 旧格式(pid int)或新格式({"pid": int, "nest": str|None})

    Returns:
        {"pid": int, "nest": str|None} 格式的字典
    """
    if isinstance(entry, dict):
        return {"pid": entry.get("pid", 0), "nest": entry.get("nest")}
    # 旧格式：纯整数PID
    return {"pid": int(entry), "nest": None}


def _load_pid_data() -> dict[str, list[dict[str, Any]]]:
    """加载pid.json数据（自动兼容旧格式）

    Returns:
        {task_id_str: [{"pid": pid, "nest": nest|None}, ...]} 格式的字典
    """
    pid_file = _get_pid_file_path()
    if not pid_file.exists():
        return {}
    try:
        with open(pid_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 验证数据格式
        if not isinstance(data, dict):
            logger.warning(f"pid.json格式错误: 期望dict，实际{type(data)}")
            return {}
        # 🔧 Task#72：自动转换旧格式到v2格式
        needs_save = False
        for key in data:
            if not isinstance(data[key], list):
                continue
            normalized = []
            has_old_format = False
            for entry in data[key]:
                norm = _normalize_pid_entry(entry)
                normalized.append(norm)
                if not isinstance(entry, dict):
                    has_old_format = True
            data[key] = normalized
            if has_old_format:
                needs_save = True
        if needs_save:
            _save_pid_data(data)
        return data
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"读取pid.json失败: {e}")
        return {}


def _save_pid_data(data: dict[str, list[dict[str, Any]]]) -> bool:
    """保存pid.json数据

    Args:
        data: {task_id_str: [{"pid": pid, "nest": nest}, ...]} 格式的字典

    Returns:
        是否保存成功
    """
    from peng.common.paths import ensure_dir_with_owner, chown_to_project_owner
    pid_file = _get_pid_file_path()
    ensure_dir_with_owner(pid_file.parent)
    try:
        with open(pid_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        chown_to_project_owner(pid_file)
        return True
    except IOError as e:
        logger.error(f"保存pid.json失败: {e}")
        return False


def _pid_in_entries(pid: int, entries: list[dict[str, Any]]) -> bool:
    """检查PID是否已在条目列表中"""
    return any(e.get("pid") == pid for e in entries)


def add_pid(task_id: int, pid: int, nest: str | None = None) -> bool:
    """为指定task_id注册一个后台进程PID

    Args:
        task_id: 事务ID
        pid: 进程PID
        nest: nest名称（Task#72：记住nest来源）

    Returns:
        是否注册成功
    """
    if not task_id or not pid:
        return False
    data = _load_pid_data()
    key = str(task_id)
    if key not in data:
        data[key] = []
    if not _pid_in_entries(pid, data[key]):
        data[key].append({"pid": pid, "nest": nest})
    return _save_pid_data(data)


def add_pids(task_id: int, pids: list[int], nest: str | None = None) -> bool:
    """为指定task_id批量注册后台进程PID

    Args:
        task_id: 事务ID
        pids: 进程PID列表
        nest: nest名称（Task#72：记住nest来源）

    Returns:
        是否注册成功
    """
    if not task_id or not pids:
        return False
    data = _load_pid_data()
    key = str(task_id)
    if key not in data:
        data[key] = []
    for pid in pids:
        if not _pid_in_entries(pid, data[key]):
            data[key].append({"pid": pid, "nest": nest})
    return _save_pid_data(data)


def get_pids(task_id: int, nest: str | None = None) -> list[int]:
    """获取指定task_id的所有已注册PID

    Args:
        task_id: 事务ID
        nest: 可选nest过滤（Task#72），None表示不过滤

    Returns:
        PID列表（可能包含已退出的进程）
    """
    data = _load_pid_data()
    key = str(task_id)
    entries = data.get(key, [])
    if nest is None:
        return [e["pid"] for e in entries]
    # 🔧 Task#72：按nest过滤
    return [e["pid"] for e in entries if e.get("nest") == nest]


def get_all_tasks() -> dict[str, list[dict[str, Any]]]:
    """获取所有task_id及其PID记录（v2格式）

    Returns:
        {task_id_str: [{"pid": pid, "nest": nest}, ...]} 格式的完整数据
    """
    return _load_pid_data()


def _is_pid_alive(pid: int) -> bool:
    """检查进程是否存活（排除僵尸进程，跨平台）

    注意：只能检查本机进程，远程nest的PID无法检查

    Args:
        pid: 进程PID

    Returns:
        进程是否存活（非僵尸）
    """
    if pid <= 0:
        return False

    # Linux: 检查/proc/<pid>/stat来排除僵尸进程
    if sys.platform.startswith("linux"):
        try:
            stat_path = f"/proc/{pid}/stat"
            with open(stat_path, "r") as f:
                stat = f.read()
                # stat格式: pid (comm) state ...
                # state是第3个字段
                parts = stat.split()
                if len(parts) >= 3:
                    state = parts[2]
                    # Z表示僵尸进程
                    if state == "Z":
                        return False
        except (FileNotFoundError, IOError, PermissionError):
            return False

    # 使用跨平台封装检查进程是否存在
    from peng.common.platform import process_exists
    return process_exists(pid)


def remove_task_pids(task_id: int) -> list[dict[str, Any]]:
    """移除指定task_id的所有PID记录

    Args:
        task_id: 事务ID

    Returns:
        被移除的PID记录列表
    """
    data = _load_pid_data()
    key = str(task_id)
    removed = data.pop(key, [])
    _save_pid_data(data)
    return removed


def cleanup_dead_pids(task_id: int | None = None, nest: str | None = None) -> int:
    """清理已退出的进程PID记录

    Args:
        task_id: 指定task_id时只清理该task_id的，None时清理所有
        nest: 🔧 Task#72：只清理指定nest的PID（仅对本地PID检查存活状态）

    Returns:
        清理的PID数量
    """
    data = _load_pid_data()
    cleaned = 0

    if task_id is not None:
        # 只清理指定task_id
        key = str(task_id)
        if key in data:
            alive_entries = []
            for entry in data[key]:
                entry_nest = entry.get("nest")
                # 🔧 Task#72：按nest过滤
                if nest is not None and entry_nest != nest:
                    alive_entries.append(entry)
                    continue
                # 本地PID检查存活状态，远程nest的PID假定存活（无法本地检查）
                if entry_nest is not None:
                    alive_entries.append(entry)
                elif _is_pid_alive(entry["pid"]):
                    alive_entries.append(entry)
                else:
                    cleaned += 1
            if alive_entries:
                data[key] = alive_entries
            else:
                del data[key]
            if cleaned > 0:
                _save_pid_data(data)
    else:
        # 清理所有task_id
        changed = False
        for key in list(data.keys()):
            alive_entries = []
            for entry in data[key]:
                entry_nest = entry.get("nest")
                # 🔧 Task#72：按nest过滤
                if nest is not None and entry_nest != nest:
                    alive_entries.append(entry)
                    continue
                # 本地PID检查存活状态，远程nest的PID假定存活
                if entry_nest is not None:
                    alive_entries.append(entry)
                elif _is_pid_alive(entry["pid"]):
                    alive_entries.append(entry)
                else:
                    cleaned += 1
            if len(alive_entries) != len(data[key]):
                changed = True
            if alive_entries:
                data[key] = alive_entries
            else:
                del data[key]
                changed = True
        if changed:
            _save_pid_data(data)

    return cleaned


def kill_task_pids(task_id: int, nest: str | None = None) -> dict[str, Any]:
    """杀死指定task_id的所有已注册进程

    先清理已退出的进程，然后杀死存活的进程，等待确认退出后移除记录。
    🔧 Task#72：支持按nest过滤，远程nest进程通过nest连接kill

    Args:
        task_id: 事务ID
        nest: 可选nest过滤

    Returns:
        {"killed": [成功杀死的PID], "already_dead": [已退出的PID], "errors": [失败的PID]}
    """
    data = _load_pid_data()
    key = str(task_id)
    entries = data.get(key, [])

    # 🔧 Task#72：按nest过滤
    if nest is not None:
        entries = [e for e in entries if e.get("nest") == nest]

    if not entries:
        return {"killed": [], "already_dead": [], "errors": []}

    # 分组：本地 vs 远程nest
    local_entries = [e for e in entries if e.get("nest") is None]
    remote_entries = [e for e in entries if e.get("nest") is not None]

    killed = []
    already_dead = []
    errors = []
    pending_pids = []

    # 处理本地进程
    for entry in local_entries:
        pid = entry["pid"]
        if not _is_pid_alive(pid):
            already_dead.append(pid)
            continue
        try:
            from peng.common.platform import kill_process
            kill_process(
                pid, force=False, kill_tree=False,
                caller="pid_registry.kill_task_pids",
                reason=f"task_id={task_id}",
            )
            killed.append(pid)
            pending_pids.append(pid)
        except (ProcessLookupError, PermissionError) as e:
            errors.append({"pid": pid, "nest": None, "error": str(e)})
        except OSError as e:
            errors.append({"pid": pid, "nest": None, "error": str(e)})

    # 等待本地进程确认退出
    if pending_pids:
        deadline = time.time() + 5
        while pending_pids and time.time() < deadline:
            time.sleep(0.2)
            still_pending = [pid for pid in pending_pids if _is_pid_alive(pid)]
            pending_pids = still_pending

        # 对仍未退出的进程尝试SIGKILL
        if pending_pids:
            from peng.common.platform import kill_process
            for pid in pending_pids:
                try:
                    kill_process(
                        pid, force=True, kill_tree=False,
                        caller="pid_registry.kill_task_pids",
                        reason=f"task_id={task_id}, force kill",
                    )
                except (ProcessLookupError, PermissionError, OSError):
                    pass

            deadline2 = time.time() + 2
            while pending_pids and time.time() < deadline2:
                time.sleep(0.2)
                still_pending = [pid for pid in pending_pids if _is_pid_alive(pid)]
                pending_pids = still_pending

    # 🔧 Task#72：处理远程nest进程
    if remote_entries:
        # 按nest分组
        nest_groups: dict[str, list[int]] = {}
        for entry in remote_entries:
            n = entry.get("nest", "")
            if n not in nest_groups:
                nest_groups[n] = []
            nest_groups[n].append(entry["pid"])

        for nest_name, pids in nest_groups.items():
            try:
                from peng.nest.bridge import NestBridgeManager
                manager = NestBridgeManager.get_instance()
                conn = manager.get_connection(nest_name)
                if conn:
                    from peng.nest.protocol import NestRequest
                    # 通过nest发送kill命令（同步方式，适用于nest bridge的同步接口）
                    pid_str = " ".join(str(p) for p in pids)
                    request = NestRequest(
                        type="bash",
                        params={
                            "command": f"kill {pid_str} 2>/dev/null; sleep 1; kill -9 {pid_str} 2>/dev/null",
                            "_skip_security": True,
                        },
                    )
                    # nest bridge 的 send_request 是 async 的
                    # 在同步上下文中，尝试使用 asyncio.run 或者标记为待处理
                    try:
                        import asyncio
                        loop = asyncio.get_running_loop()
                        # 已经在async上下文中，创建一个task
                        import concurrent.futures
                        with concurrent.futures.ThreadPoolExecutor() as pool:
                            future = pool.submit(
                                asyncio.run,
                                conn.send_request(request, timeout=10.0)
                            )
                            response = future.result(timeout=15.0)
                        if response.error:
                            for pid in pids:
                                errors.append({"pid": pid, "nest": nest_name, "error": str(response.error)})
                        else:
                            killed.extend(pids)
                    except RuntimeError:
                        # 没有运行中的事件循环，直接 asyncio.run
                        response = asyncio.run(conn.send_request(request, timeout=10.0))
                        if response.error:
                            for pid in pids:
                                errors.append({"pid": pid, "nest": nest_name, "error": str(response.error)})
                        else:
                            killed.extend(pids)
                else:
                    for pid in pids:
                        errors.append({"pid": pid, "nest": nest_name, "error": f"nest连接未找到: {nest_name}"})
            except Exception as e:
                for pid in pids:
                    errors.append({"pid": pid, "nest": nest_name, "error": str(e)})

    # 清理已确认退出的PID记录
    confirmed_dead = set(already_dead)
    for pid in killed:
        # 本地PID检查是否真的退出了
        if pid in [e["pid"] for e in local_entries]:
            if not _is_pid_alive(pid):
                confirmed_dead.add(pid)
        else:
            # 远程PID，假定kill成功
            confirmed_dead.add(pid)
    _remove_confirmed_pids(task_id, confirmed_dead)

    return {
        "killed": killed,
        "already_dead": already_dead,
        "errors": errors,
    }


def _remove_confirmed_pids(task_id: int, confirmed_pids: set[int]) -> None:
    """从记录中移除已确认退出的PID，保留仍存活的PID

    Args:
        task_id: 事务ID
        confirmed_pids: 已确认退出的PID集合
    """
    if not confirmed_pids:
        return
    data = _load_pid_data()
    key = str(task_id)
    if key not in data:
        return
    data[key] = [e for e in data[key] if e.get("pid") not in confirmed_pids]
    if not data[key]:
        del data[key]
    _save_pid_data(data)

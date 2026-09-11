"""
Daemon注册表管理模块

管理 daemon 工具启动的非阻塞后台进程，按 daemon id 记录在 data/daemon.json 中。
是后台进程的唯一注册表（原 pid_registry/pid.json 机制已废弃删除）。

daemon id 格式: "d" + 4位随机hex（如 d3f9a2），冲突自动重试。

🔒 daemon生命周期保证：daemon进程不跟随task进程结束！只能自行结束或stop/kill结束：
- 三重保障:
  1. 启动侧脱钩：本地daemon经"中间启动进程即退"模式创建（POSIX: setsid+被init收养；
     Windows: PowerShell Start-Process父进程即退断链），task进程cancel的kill_tree递归children不含daemon
  2. 杀手侧保护：platform._kill_process_tree_posix 收集children时排除已注册本地daemon PID
  3. daemon自成会话/进程组（POSIX），killpg(task进程组)不会波及

停止方式:
- 工具层: bash/daemon 工具的 stop 内置命令（stop <id> / stop all）
- CLI: task kill -d <id>（支持多个 -d）/ task kill <task_id> / task killall

记录自动清理（cleanup_dead_daemons）:
- 本地daemon: process_exists 检查PID，已退出即清理记录
- 远程daemon: 按nest分组批量检查远程PID存活（Windows: PowerShell Get-Process；
  Linux/macOS: ps -o pid=），已退出即清理记录；nest不可达/查询失败时保守保留
- 调用时机: task daemon 显示、task kill/killall、WebUI daemon列表/kill API、fly reset

跨平台支持:
- 本地/远程 Linux: setsid 启动（daemon为会话首进程，pgid==pid），killpg 杀整组
- 远程 macOS: nohup 启动（无 setsid），kill pid + pkill -P 杀子进程
- Windows: bat包装daemon命令 + PowerShell Start-Process创建（wmic在Win11 24H2+已移除），
  taskkill /T /F 杀进程树
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DAEMON_FILE_NAME = "daemon.json"


def normalize_nest(nest: Any) -> str | None:
    """归一化nest名称：local/空/None → None（表示本地进程）"""
    if nest is None:
        return None
    s = str(nest).strip()
    if s.lower() in ("", "local", "null", "none"):
        return None
    return s


def _get_daemon_file_path() -> Path:
    """获取daemon.json文件路径"""
    from peng.config import get_project_root

    return get_project_root() / "data" / _DAEMON_FILE_NAME


def _load_daemon_data() -> dict[str, dict[str, Any]]:
    """加载daemon.json数据"""
    daemon_file = _get_daemon_file_path()
    if not daemon_file.exists():
        return {}
    try:
        with open(daemon_file, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning(f"daemon.json格式错误: 期望dict，实际{type(data)}")
            return {}
        return data
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"读取daemon.json失败: {e}")
        return {}


def _save_daemon_data(data: dict[str, dict[str, Any]]) -> bool:
    """保存daemon.json数据"""
    from peng.common.paths import chown_to_project_owner, ensure_dir_with_owner

    daemon_file = _get_daemon_file_path()
    ensure_dir_with_owner(daemon_file.parent)
    try:
        with open(daemon_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        chown_to_project_owner(daemon_file)
        return True
    except OSError as e:
        logger.error(f"保存daemon.json失败: {e}")
        return False


def generate_daemon_id() -> str:
    """生成不冲突的daemon id: "d" + 4位随机hex"""
    data = _load_daemon_data()
    while True:
        daemon_id = "d" + secrets.token_hex(2)
        if daemon_id not in data:
            return daemon_id


def register_daemon(
    task_id: int | None,
    command: str,
    pid: int,
    nest: str | None = None,
    log_file: str | None = None,
    workdir: str | None = None,
    platform: str | None = None,
    daemon_id: str | None = None,
) -> dict[str, Any]:
    """注册一个daemon进程

    Args:
        task_id: 启动daemon的事务ID
        command: daemon运行的命令
        pid: daemon进程PID
        nest: nest名称（None/"local"=本地）
        log_file: 日志文件路径（daemon所在机器上的绝对路径）
        workdir: daemon工作目录
        platform: daemon所在机器平台（linux/darwin/windows）
        daemon_id: 指定daemon id（启动辅助函数已用该id生成日志文件名时必须传入，
                   保持id与日志文件名一致；默认自动生成）

    Returns:
        完整的daemon条目
    """
    data = _load_daemon_data()
    if daemon_id is None:
        daemon_id = generate_daemon_id()
    else:
        while daemon_id in data:  # 冲突保护
            daemon_id = generate_daemon_id()
    entry = {
        "id": daemon_id,
        "task_id": task_id,
        "command": command,
        "pid": int(pid),
        "nest": normalize_nest(nest),
        "log_file": log_file,
        "workdir": workdir,
        "platform": platform,
        "start_time": time.time(),
    }
    data[daemon_id] = entry
    _save_daemon_data(data)
    return entry


def get_daemon(daemon_id: str) -> dict[str, Any] | None:
    """获取指定daemon条目"""
    return _load_daemon_data().get(daemon_id)


def list_daemons() -> dict[str, dict[str, Any]]:
    """列出所有daemon条目"""
    return _load_daemon_data()


def get_task_daemons(task_id: int) -> list[dict[str, Any]]:
    """获取指定事务的所有daemon条目"""
    data = _load_daemon_data()
    return [e for e in data.values() if e.get("task_id") == task_id]


def get_tasks_summary() -> dict[str, list[dict[str, Any]]]:
    """按task_id分组返回所有daemon条目（CLI/WebUI显示用）

    Returns:
        {task_id_str: [entry, ...]}，task_id为空的daemon归入""键
    """
    data = _load_daemon_data()
    summary: dict[str, list[dict[str, Any]]] = {}
    for entry in data.values():
        key = str(entry.get("task_id") or "")
        summary.setdefault(key, []).append(entry)
    return summary


def remove_daemon(daemon_id: str) -> dict[str, Any] | None:
    """移除daemon条目，返回被移除的条目"""
    data = _load_daemon_data()
    entry = data.pop(daemon_id, None)
    if entry is not None:
        _save_daemon_data(data)
    return entry


def cleanup_dead_daemons() -> int:
    """清理已退出的daemon记录（本地+远程）

    - 本地daemon(nest=None): process_exists 检查PID存活
    - 远程daemon: 按(nest,platform)分组批量检查远程PID存活
      （Windows: PowerShell Get-Process；Linux/macOS: ps -o pid=），
      每nest一次RPC、每批≤50个PID防命令行超长；
      nest不可达/查询失败时保守保留该nest全部记录（防误删活daemon）

    Returns:
        清理的记录数量
    """
    from peng.common.platform import process_exists

    data = _load_daemon_data()
    if not data:
        return 0

    alive: dict[str, dict[str, Any]] = {}
    dead_ids: list[str] = []

    # 1) 本地检查存活 + 收集远程待查分组
    remote_groups: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
    for daemon_id, entry in data.items():
        if normalize_nest(entry.get("nest")) is None:
            if process_exists(int(entry.get("pid") or 0)):
                alive[daemon_id] = entry
            else:
                dead_ids.append(daemon_id)
        else:
            key = (str(entry.get("nest")), entry.get("platform"))
            remote_groups.setdefault(key, []).append(entry)

    # 2) 远程按(nest,platform)分组批量检查
    for (nest_name, platform), entries in remote_groups.items():
        alive_pids = _check_remote_group_alive(nest_name, platform, entries)
        if alive_pids is None:
            # nest不可达/查询失败：保守保留该nest全部记录
            logger.debug(f"远程daemon存活检查失败(nest={nest_name})，保守保留 {len(entries)} 条记录")
            for entry in entries:
                alive[entry["id"]] = entry
            continue
        for entry in entries:
            if int(entry.get("pid") or 0) in alive_pids:
                alive[entry["id"]] = entry
            else:
                dead_ids.append(entry["id"])

    if dead_ids:
        _save_daemon_data(alive)
        details = ", ".join(
            "{}(pid={},nest={})".format(i, data[i].get("pid"), data[i].get("nest"))
            for i in dead_ids
        )
        logger.info(f"已清理 {len(dead_ids)} 个已退出daemon的记录: {details}")
    return len(dead_ids)


# ============================================================
# 远程daemon批量存活检查（cleanup_dead_daemons远程部分）
# ============================================================

_REMOTE_CHECK_BATCH_SIZE = 50  # 每批PID数上限（防Windows cmd ~8191字符命令行超长）


def _build_remote_check_command(pids: list[int], platform: str | None) -> str:
    """构建远程批量检查PID存活命令（输出存活PID，每行一个）

    - Windows: PowerShell Get-Process 批量查询（cmd外层执行，双引号包裹，
      引号内管道符不会被cmd解析）
    - Linux/macOS: ps -o pid= -p 逗号列表（无需kill权限，不存在PID无输出）
    """
    pid_list = ",".join(str(p) for p in pids)
    if platform and "windows" in platform.lower():
        return (
            "powershell -NoProfile -NonInteractive -Command "
            f'"Get-Process -Id {pid_list} -ErrorAction SilentlyContinue | '
            'Select-Object -ExpandProperty Id"'
        )
    return f"ps -o pid= -p {pid_list} 2>/dev/null"


def _parse_alive_pids(stdout: str | None) -> set[int]:
    """解析批量存活检查命令输出→存活PID集合（容错提取每行首个数字）"""
    alive: set[int] = set()
    for line in (stdout or "").splitlines():
        m = re.search(r"(\d+)", line)
        if m:
            alive.add(int(m.group(1)))
    return alive


def _http_nest_exec(nest_name: str, command: str) -> dict[str, Any] | None:
    """经本地fly service HTTP API在nest上执行命令（CLI独立进程场景）

    CLI本地模式下独立进程内无nest连接（连接在fly service进程内），
    经 /api/nest/exec 端点转发执行。

    Returns:
        {"exit_code": int, "stdout": str, "stderr": str}；服务不可达/失败返回None
    """
    import json as _json
    import urllib.parse
    import urllib.request

    try:
        port = 6688
        try:
            from peng.config.loader import load_config

            cfg = load_config()
            if hasattr(cfg, "webui") and cfg.webui:
                port = cfg.webui.port
        except Exception:
            pass
        params = urllib.parse.urlencode(
            {"nest": nest_name, "command": command, "timeout": 15}
        )
        url = f"http://localhost:{port}/api/nest/exec?{params}"
        req = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = _json.loads(resp.read().decode("utf-8"))
        if result.get("success"):
            return result
        logger.debug(f"HTTP nest exec失败(nest={nest_name}): {result.get('error')}")
        return None
    except Exception as e:
        logger.debug(f"HTTP nest exec异常(nest={nest_name}): {e}")
        return None


def _check_remote_alive_pids_sync(
    nest_name: str, pids: list[int], platform: str | None
) -> set[int] | None:
    """通过nest批量检查远程PID存活（同步；服务进程直连，CLI进程经HTTP API fallback）

    Returns:
        存活PID集合；nest不可达/查询失败返回None（调用方保守保留记录）
    """
    if not pids:
        return set()
    cmd = _build_remote_check_command(pids, platform)

    result: dict[str, Any] | None = None

    # 1) 进程内nest连接（fly service/WebUI/CLI转发服务端进程内）
    try:
        from peng.nest.bridge import NestBridgeManager
        from peng.nest.protocol import NestRequest

        conn = NestBridgeManager.get_instance().get_connection(nest_name)
        if conn:
            request = NestRequest(
                type="bash",
                params={"command": cmd, "_skip_security": True},
            )
            response = _run_async(conn.send_request(request, timeout=15.0))
            if not response.error:
                result = response.data or {}
            else:
                logger.debug(f"进程内nest连接查询失败(nest={nest_name}): {response.error}")
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"进程内nest连接异常(nest={nest_name}): {e}")

    # 2) fallback: 本地fly service HTTP API（CLI独立进程场景）
    if result is None:
        result = _http_nest_exec(nest_name, cmd)

    if result is None:
        return None
    return _parse_alive_pids(result.get("stdout", ""))


def _check_remote_group_alive(
    nest_name: str, platform: str | None, entries: list[dict[str, Any]]
) -> set[int] | None:
    """批量检查同nest的远程daemon PID存活（分批防命令行超长）

    Returns:
        存活PID集合；任一批查询失败返回None（整组保守保留）
    """
    alive_pids: set[int] = set()
    for i in range(0, len(entries), _REMOTE_CHECK_BATCH_SIZE):
        batch = entries[i : i + _REMOTE_CHECK_BATCH_SIZE]
        pids = [int(e.get("pid") or 0) for e in batch if int(e.get("pid") or 0) > 0]
        if not pids:
            continue
        batch_alive = _check_remote_alive_pids_sync(nest_name, pids, platform)
        if batch_alive is None:
            return None
        alive_pids |= batch_alive
    return alive_pids


# ============================================================
# 进程存活与杀进程
# ============================================================

def _local_pid_alive(pid: int) -> bool:
    """本地进程存活检查（排除僵尸，platform.process_exists已处理）"""
    from peng.common.platform import process_exists

    return process_exists(pid)


def _kill_local_daemon(pid: int) -> tuple[bool, str]:
    """杀死本地daemon进程树

    Linux/macOS: daemon以会话首进程启动（pgid==pid），killpg杀整组
    Windows: taskkill /T /F 杀进程树

    Returns:
        (成功, 消息)
    """
    if pid <= 0:
        return False, f"无效PID: {pid}"
    if not _local_pid_alive(pid):
        return True, "进程已自行退出"

    try:
        if sys.platform == "win32":
            from peng.common.platform import kill_process

            ok = kill_process(
                pid,
                force=True,
                kill_tree=True,
                caller="daemon_registry._kill_local_daemon",
                reason=f"stop daemon pid={pid}",
            )
            return (bool(ok), "已杀死进程树" if ok else "杀死进程树失败")

        # POSIX: 先SIGTERM进程组，等待退出，再SIGKILL
        def _signal_group(sig: int) -> None:
            try:
                os.killpg(pid, sig)
            except (ProcessLookupError, PermissionError):
                os.kill(pid, sig)

        try:
            _signal_group(signal.SIGTERM)
        except ProcessLookupError:
            return True, "进程已自行退出"

        deadline = time.time() + 5
        while time.time() < deadline:
            if not _local_pid_alive(pid):
                return True, "已停止(SIGTERM)"
            time.sleep(0.2)

        try:
            _signal_group(signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

        deadline = time.time() + 3
        while time.time() < deadline:
            if not _local_pid_alive(pid):
                return True, "已强制杀死(SIGKILL)"
            time.sleep(0.2)

        return False, "强制杀死后进程仍存活（可能为僵尸或权限不足）"
    except ProcessLookupError:
        return True, "进程已自行退出"
    except PermissionError as e:
        return False, f"权限不足: {e}"
    except OSError as e:
        return False, str(e)


def _build_remote_kill_command(pid: int, platform: str | None) -> str:
    """构建远程杀daemon命令（杀进程及其子进程，并验证退出）"""
    if platform and "windows" in platform.lower():
        return f"taskkill /PID {pid} /T /F & exit /b 0"
    if platform and "linux" in platform.lower():
        # Linux: daemon是会话首进程（pgid==pid），负PID杀整组
        return (
            f"kill -TERM -- -{pid} 2>/dev/null; kill -TERM {pid} 2>/dev/null; "
            f"sleep 1; kill -KILL -- -{pid} 2>/dev/null; kill -KILL {pid} 2>/dev/null; "
            f"if kill -0 {pid} 2>/dev/null; then echo ALIVE; else echo DEAD; fi"
        )
    # macOS等无setsid平台: kill pid + pkill子进程
    return (
        f"kill -TERM {pid} 2>/dev/null; pkill -TERM -P {pid} 2>/dev/null; "
        f"sleep 1; kill -KILL {pid} 2>/dev/null; pkill -KILL -P {pid} 2>/dev/null; "
        f"if kill -0 {pid} 2>/dev/null; then echo ALIVE; else echo DEAD; fi"
    )


def _parse_remote_kill_result(result: dict | None, platform: str | None) -> tuple[bool, str]:
    """解析远程kill命令结果"""
    stdout = (result or {}).get("stdout", "") or ""
    stderr = (result or {}).get("stderr", "") or ""
    if "ALIVE" in stdout:
        return False, f"远程kill后进程仍存活: {stderr.strip()[:200]}"
    if "DEAD" in stdout:
        return True, "已停止(远程)"
    # 无验证输出（如Windows taskkill），按exit code判断
    exit_code = (result or {}).get("exit_code", -1)
    if exit_code == 0:
        return True, "已停止(远程)"
    return False, f"远程kill失败: {stderr.strip()[:200] or f'exit_code={exit_code}'}"


async def _kill_remote_daemon(
    nest_name: str, pid: int, platform: str | None = None
) -> tuple[bool, str]:
    """通过nest异步杀死远程daemon（工具层使用）"""
    from peng.nest.client import bash as nest_bash

    cmd = _build_remote_kill_command(pid, platform)
    try:
        resp = await nest_bash(command=cmd, timeout=30000, nest_name=nest_name)
        if resp.error:
            return False, str(resp.error)
        return _parse_remote_kill_result(resp.data, platform)
    except Exception as e:
        return False, f"远程kill异常: {e}"


def _run_async(coro):
    """在同步上下文中执行协程（运行中有事件循环时用线程池）"""
    import asyncio

    try:
        asyncio.get_running_loop()
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result(timeout=60)
    except RuntimeError:
        return asyncio.run(coro)


def _kill_remote_daemon_sync(
    nest_name: str, pid: int, platform: str | None = None
) -> tuple[bool, str]:
    """通过nest bridge同步杀死远程daemon（CLI使用）"""
    from peng.nest.bridge import NestBridgeManager
    from peng.nest.protocol import NestRequest

    manager = NestBridgeManager.get_instance()
    conn = manager.get_connection(nest_name)
    if not conn:
        return False, f"nest连接未找到: {nest_name}"

    cmd = _build_remote_kill_command(pid, platform)
    request = NestRequest(
        type="bash",
        params={"command": cmd, "_skip_security": True},
    )
    try:
        response = _run_async(conn.send_request(request, timeout=30.0))
        if response.error:
            return False, str(response.error)
        return _parse_remote_kill_result(response.data, platform)
    except Exception as e:
        return False, str(e)


def _stop_entry_sync(entry: dict[str, Any]) -> tuple[bool, str]:
    """同步停止一个daemon条目（本地直接杀/远程经nest bridge）"""
    nest_name = normalize_nest(entry.get("nest"))
    pid = int(entry.get("pid") or 0)
    if nest_name is None:
        return _kill_local_daemon(pid)
    return _kill_remote_daemon_sync(nest_name, pid, entry.get("platform"))


async def _stop_entry(entry: dict[str, Any]) -> tuple[bool, str]:
    """异步停止一个daemon条目（工具层使用）"""
    nest_name = normalize_nest(entry.get("nest"))
    pid = int(entry.get("pid") or 0)
    if nest_name is None:
        return _kill_local_daemon(pid)
    return await _kill_remote_daemon(nest_name, pid, entry.get("platform"))


# ============================================================
# 工具层停止接口（bash/daemon 工具的 stop 内置命令）
# ============================================================

async def stop_daemon_ids(ids: list[str]) -> str:
    """停止一个或多个daemon（工具层stop内置命令使用）

    Returns:
        人类可读的执行结果（每个id一行）
    """
    parts: list[str] = []
    for daemon_id in ids:
        entry = get_daemon(daemon_id)
        if not entry:
            parts.append(f"❌ daemon '{daemon_id}' 不存在（可用 'stop all' 清理全部，或用 task daemon 查看列表）")
            continue
        ok, msg = await _stop_entry(entry)
        cmd_preview = (entry.get("command") or "")[:60]
        if ok:
            remove_daemon(daemon_id)
            log = entry.get("log_file")
            log_hint = f"，日志保留: {log}" if log else ""
            parts.append(f"✅ daemon {daemon_id} (PID {entry.get('pid')}) 已停止: {msg}。命令: {cmd_preview}{log_hint}")
        else:
            parts.append(f"❌ daemon {daemon_id} (PID {entry.get('pid')}) 停止失败: {msg}")
    return "\n".join(parts)


async def stop_all_daemons() -> str:
    """停止所有daemon（'stop all'）"""
    data = list_daemons()
    if not data:
        return "当前没有运行中的daemon"
    return await stop_daemon_ids(list(data.keys()))


# ============================================================
# CLI同步停止接口（task kill / killall）
# 返回结构统一（对齐原pid_registry消费者）:
# {"killed": [{"id","pid","nest"}], "already_dead": [{"id","pid","nest"}],
#  "errors": [{"id","pid","nest","error"}], "details": [str]}
# ============================================================

def _result_entry(entry: dict[str, Any], error: str | None = None) -> dict[str, Any]:
    """构建kill结果条目"""
    base = {"id": entry.get("id"), "pid": entry.get("pid"), "nest": entry.get("nest")}
    if error is not None:
        base["error"] = error
    return base


def kill_daemon_by_id_sync(daemon_id: str) -> dict[str, Any]:
    """按daemon id停止daemon（task kill -d）"""
    result: dict[str, Any] = {"killed": [], "already_dead": [], "errors": [], "details": []}
    entry = get_daemon(daemon_id)
    if not entry:
        result["errors"].append({"id": daemon_id, "pid": None, "nest": None, "error": "daemon不存在"})
        return result
    ok, msg = _stop_entry_sync(entry)
    if ok:
        remove_daemon(daemon_id)
        bucket = "already_dead" if "自行退出" in msg else "killed"
        result[bucket].append(_result_entry(entry))
        result["details"].append(f"{daemon_id}(PID {entry.get('pid')}): {msg}")
    else:
        result["errors"].append(_result_entry(entry, error=msg))
    return result


def _kill_entries_sync(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """批量同步停止daemon条目"""
    result: dict[str, Any] = {"killed": [], "already_dead": [], "errors": [], "details": []}
    for entry in entries:
        daemon_id = entry.get("id", "?")
        ok, msg = _stop_entry_sync(entry)
        if ok:
            remove_daemon(daemon_id)
            bucket = "already_dead" if "自行退出" in msg else "killed"
            result[bucket].append(_result_entry(entry))
            result["details"].append(f"{daemon_id}(PID {entry.get('pid')}): {msg}")
        else:
            result["errors"].append(_result_entry(entry, error=msg))
    return result


def kill_task_daemons_sync(task_id: int) -> dict[str, Any]:
    """停止指定事务的所有daemon（task kill <task_id>）"""
    return _kill_entries_sync(get_task_daemons(task_id))


def kill_all_daemons_sync() -> dict[str, Any]:
    """停止所有daemon（task killall）"""
    return _kill_entries_sync(list(list_daemons().values()))


# ============================================================
# daemon启动辅助（daemon工具使用）
# ============================================================

def _log_paths(workdir: str | Path | None, daemon_id: str) -> tuple[str, str]:
    """计算daemon日志目录/文件路径（POSIX风格，Windows会转换分隔符）"""
    import ntpath
    import posixpath

    if sys.platform == "win32":
        base = ntpath
    else:
        base = posixpath
    wd = str(workdir) if workdir else os.getcwd()
    log_dir = base.join(wd, ".peng", "daemon")
    log_file = base.join(log_dir, f"daemon_{daemon_id}.log")
    return log_dir, log_file


def _build_windows_daemon_bat(command: str, workdir: str | None, log_file: str) -> str:
    """构建Windows daemon启动bat内容

    daemon命令包装进bat文件，启动器只需引用bat路径（规避引号转义）。
    """
    lines = ["@echo off"]
    if workdir:
        lines.append(f'cd /d "{workdir}"')
    lines.append(f'{command} >> "{log_file}" 2>&1')
    return "\r\n".join(lines) + "\r\n"


def _build_windows_start_ps(bat_file: str) -> str:
    """构建 PowerShell Start-Process 启动脚本体（本地/远程通用）

    使用 PowerShell 而非 wmic：wmic 在 Win11 24H2+ 已移除，PowerShell 全版本可用。
    Start-Process 启动的cmd父进程为本powershell（立即退出）→
    断开与task进程的进程树链，task进程cancel的taskkill /T 不会波及daemon。
    PID 经 stdout 输出回传。

    注意：脚本体内部只用单引号（远程经cmd外层双引号包裹时无嵌套转义问题）。
    """
    return (
        "$p = Start-Process -FilePath 'cmd.exe' "
        f"-ArgumentList '/c','{bat_file}' "
        "-WindowStyle Hidden -PassThru; Write-Output $p.Id"
    )


def start_local_daemon(
    command: str, workdir: str | Path | None, daemon_id: str
) -> tuple[int, str]:
    """本地启动非阻塞daemon进程（与task进程完全脱钩）

    🔒 daemon进程不跟随task进程结束！只能自行结束或stop/kill结束。
    启动模式（关键：中间启动进程立即退出，daemon脱离task进程树）：
    - POSIX: 中间bash执行 "setsid nohup ... & echo $!" 后立即退出并reparent：
      daemon经setsid自成会话首进程，被init收养。
      task进程cancel时的kill_tree递归收集/proc/children不再包含daemon。
    - Windows: bat包装daemon命令 + PowerShell Start-Process创建（wmic在Win11 24H2+已移除）；
      启动的cmd父进程为powershell（立即退出），断开与task进程的进程树链，
      taskkill /T 不会波及daemon。

    Args:
        command: 要运行的命令
        workdir: 工作目录
        daemon_id: daemon id（用于日志文件名）

    Returns:
        (pid, log_file绝对路径)

    Raises:
        OSError: 启动失败
    """
    import shlex

    log_dir, log_file = _log_paths(workdir, daemon_id)
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    if sys.platform == "win32":
        bat_file = Path(log_dir) / f"daemon_{daemon_id}.bat"
        bat_file.write_text(
            _build_windows_daemon_bat(command, str(workdir) if workdir else None, log_file),
            encoding="utf-8",
        )
        # PowerShell Start-Process（wmic在Win11 24H2+已移除）
        # list形式传参：无cmd引号转义问题
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                _build_windows_start_ps(str(bat_file)),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        m = re.search(r"^\s*(\d+)\s*$", result.stdout, re.MULTILINE)
        if result.returncode != 0 or not m:
            raise OSError(
                f"PowerShell启动daemon失败(rc={result.returncode}): "
                f"{result.stdout[:200]} {result.stderr[:200]}"
            )
        pid = int(m.group(1))
    else:
        inner = shlex.quote(command)
        # setsid使daemon自成会话首进程（非交互shell下$!即最终PID）
        start_cmd = (
            f"setsid nohup bash -c {inner} >> {shlex.quote(log_file)} 2>&1 < /dev/null & "
            f'echo "DAEMON_PID=$!"'
        )
        proc = subprocess.Popen(
            ["/bin/bash", "-c", start_cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(workdir) if workdir else None,
            env=os.environ.copy(),
        )
        try:
            out, err = proc.communicate(timeout=15)  # 中间bash立即退出，daemon已被init收养
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise OSError("daemon启动超时（中间shell未退出）") from None
        m = re.search(r"DAEMON_PID=(\d+)", out.decode("utf-8", errors="replace"))
        if not m:
            raise OSError(
                f"daemon启动失败（无法解析DAEMON_PID）: "
                f"{out.decode('utf-8', errors='replace')[:200]} "
                f"{err.decode('utf-8', errors='replace')[:200]}"
            )
        pid = int(m.group(1))

        # 等待daemon完成会话脱钩（pgid==pid），消除exec链竞态：
        # communicate()返回时setsid可能尚未执行setsid()（WSL等环境下exec链较慢），
        # 立即读pgid会得到旧进程组。短命命令进程退出也视为就绪。
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                if os.getpgid(pid) == pid:
                    break
            except (ProcessLookupError, PermissionError):
                break  # 进程已退出（短命命令）
            time.sleep(0.05)

    logger.info(f"daemon {daemon_id} 本地启动: pid={pid}, cmd={command[:100]}, log={log_file}")
    return pid, str(Path(log_file).resolve())


async def start_remote_daemon(
    client,
    command: str,
    workdir: str | None,
    daemon_id: str,
    is_windows: bool = False,
    platform: str | None = None,
    timeout: int = 30000,
) -> tuple[int, str]:
    """通过nest远程启动非阻塞daemon进程

    Unix远程: setsid nohup bash -c 'cmd' >> log 2>&1 & echo DAEMON_PID=$!
    Windows远程: PowerShell Start-Process -PassThru（返回PID）

    Args:
        client: nest客户端（已set_nest_name）
        command: 要运行的命令
        workdir: 远程工作目录
        daemon_id: daemon id
        is_windows: 远程nest是否Windows平台
        platform: 远程平台字符串（用于记录）
        timeout: 启动命令超时（毫秒）

    Returns:
        (远程pid, 远程日志文件路径)

    Raises:
        ToolError: 启动失败或无法解析PID
    """
    import shlex

    if is_windows or (platform or "").lower().startswith("win"):
        # Windows远程：bat包装daemon命令（经nest write_file写bat），
        # PowerShell Start-Process启动（wmic在Win11 24H2+已移除，PowerShell全版本可用）
        wd = workdir or "%CD%"
        log_dir = f"{wd}\\.peng\\daemon"
        log_file = f"{log_dir}\\daemon_{daemon_id}.log"
        bat_file = f"{log_dir}\\daemon_{daemon_id}.bat"

        bat_content = _build_windows_daemon_bat(command, workdir, log_file)
        try:
            await client.write_file(bat_file, bat_content)
        except Exception as e:
            from peng.tools.base import ToolError

            raise ToolError(f"远程写入daemon bat失败: {e}") from e

        # cmd外层双引号包PS命令体，PS体内部全单引号（无嵌套转义问题；cmd不解析$变量）
        ps_body = _build_windows_start_ps(bat_file)
        start_cmd = f'powershell -NoProfile -NonInteractive -Command "{ps_body}"'
        result = await client.bash(command=start_cmd, cwd=workdir, timeout=timeout)
        stdout = result.get("stdout", "") or ""
        m = re.search(r"^\s*(\d+)\s*$", stdout, re.MULTILINE)
        if not m:
            from peng.tools.base import ToolError

            raise ToolError(
                f"远程daemon启动失败（无法解析PowerShell输出的PID）: {stdout[:200]} "
                f"{result.get('stderr', '')[:200]}"
            )
        return int(m.group(1)), log_file

    # Unix远程
    wd = workdir or "."
    log_dir = f"{wd}/.peng/daemon"
    log_file = f"{log_dir}/daemon_{daemon_id}.log"
    daemon_cmd = f"nohup bash -c {shlex.quote(command)} >> {shlex.quote(log_file)} 2>&1 < /dev/null"
    if platform and "linux" in platform.lower():
        # Linux: setsid使daemon成为会话首进程（非交互shell下$!即最终PID），可killpg整组杀
        # ⚠️ 用分号分隔mkdir（不能用&&）：&的优先级会使整个AND列表成为后台作业，
        #    该作业继承nest命令的stdout管道fd且内部同步等待daemon退出，导致远程命令超时。
        #    分号后&仅作用于setsid链（三流全重定向到日志），不持有管道fd。
        start_cmd = (
            f"mkdir -p {shlex.quote(log_dir)}; "
            f"setsid {daemon_cmd} & "
            f'echo "DAEMON_PID=$!"'
        )
    else:
        # macOS等无setsid平台
        start_cmd = (
            f"mkdir -p {shlex.quote(log_dir)}; "
            f"{daemon_cmd} & "
            f'echo "DAEMON_PID=$!"'
        )
    result = await client.bash(command=start_cmd, cwd=workdir, timeout=timeout)
    stdout = result.get("stdout", "") or ""
    m = re.search(r"DAEMON_PID=(\d+)", stdout)
    if not m:
        from peng.tools.base import ToolError

        raise ToolError(
            f"远程daemon启动失败（无法解析DAEMON_PID）: {stdout[:200]} "
            f"{result.get('stderr', '')[:200]}"
        )
    return int(m.group(1)), log_file

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

# daemon退出码标记：包装命令在daemon进程结束时把退出码以
# "{EXIT_CODE_MARKER}=<exit_code>" 形式追加写入日志文件尾部，
# 供 daemon 退出检测（watcher）解析实际返回值
EXIT_CODE_MARKER = "DAEMON_EXIT_CODE"


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
    job: bool = False,
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
        job: Windows Job模式（宿主及全部后代绑入KILL_ON_JOB_CLOSE的Job，
             stop杀宿主即连锁清杀所有孤儿后代；False=NOJOB降级/Unix）

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
        "job": bool(job),
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


def _http_nest_exec(
    nest_name: str, command: str, timeout: int = 15
) -> dict[str, Any] | None:
    """经本地fly service HTTP API在nest上执行命令（CLI独立进程场景）

    CLI本地模式下独立进程内无nest连接（连接在fly service进程内），
    经 /api/nest/exec 端点转发执行。

    Args:
        nest_name: nest名称
        command: 要执行的bash命令
        timeout: 命令超时秒数（HTTP超时为其+5）

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
            {"nest": nest_name, "command": command, "timeout": timeout}
        )
        url = f"http://localhost:{port}/api/nest/exec?{params}"
        req = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
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


async def _check_remote_group_alive_async(
    nest_name: str, platform: str | None, entries: list[dict[str, Any]]
) -> set[int] | None:
    """异步批量检查同nest远程daemon存活（get_alive_task_daemons使用）

    与同步版_check_remote_group_alive同策略：分批防命令行超长，
    任一批查询失败返回None（调用方保守视为存活）。
    """
    alive_pids: set[int] = set()
    for i in range(0, len(entries), _REMOTE_CHECK_BATCH_SIZE):
        batch = entries[i : i + _REMOTE_CHECK_BATCH_SIZE]
        pids = [int(e.get("pid") or 0) for e in batch if int(e.get("pid") or 0) > 0]
        if not pids:
            continue
        cmd = _build_remote_check_command(pids, platform)
        try:
            from peng.nest.client import bash as nest_bash

            resp = await nest_bash(command=cmd, timeout=15000, nest_name=nest_name)
            if resp.error:
                logger.debug(f"远程daemon批量存活检查失败({nest_name}): {resp.error}")
                return None
            alive_pids |= _parse_alive_pids((resp.data or {}).get("stdout", ""))
        except Exception as e:
            logger.debug(f"远程daemon批量存活检查异常({nest_name}): {e}")
            return None
    return alive_pids


async def get_alive_task_daemons(task_id: int) -> list[dict[str, Any]]:
    """获取指定事务的存活daemon条目（report汇报门禁使用，事务#2507）

    - 本地(nest=None): process_exists逐个直查（无RPC开销）
    - 远程: 按(nest,platform)分组批量RPC检查（复用_build_remote_check_command，
      每nest每批≤50个PID）；nest查询失败保守视为存活（防误放行遗留daemon）

    Returns:
        存活（或状态未知）的daemon条目列表，每条目附:
        - alive_status: "alive"（确认存活）或 "unknown"（远程查询失败，保守保留）
    """
    from peng.common.platform import process_exists

    result: list[dict[str, Any]] = []
    remote_groups: dict[tuple[str, str | None], list[dict[str, Any]]] = {}

    for entry in get_task_daemons(task_id):
        if normalize_nest(entry.get("nest")) is None:
            pid = int(entry.get("pid") or 0)
            if pid > 0 and process_exists(pid):
                item = dict(entry)
                item["alive_status"] = "alive"
                result.append(item)
        else:
            key = (str(entry.get("nest")), entry.get("platform"))
            remote_groups.setdefault(key, []).append(entry)

    for (nest_name, platform), group in remote_groups.items():
        alive_pids = await _check_remote_group_alive_async(nest_name, platform, group)
        for entry in group:
            pid = int(entry.get("pid") or 0)
            item = dict(entry)
            if alive_pids is None:
                item["alive_status"] = "unknown"
                result.append(item)
            elif pid in alive_pids:
                item["alive_status"] = "alive"
                result.append(item)

    return result


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
    """构建远程杀daemon命令（杀进程及其子进程，并验证退出）

    Windows（事务#2729修复）:
    - 旧命令 "taskkill /PID {pid} /T /F & exit /b 0" 尾部的 "& exit /b 0"
      强制退出码0，而 _parse_remote_kill_result 对Windows无ALIVE/DEAD验证
      输出时按 exit_code==0 判定成功——导致 taskkill 无论成败（进程不存在/
      权限不足/树断裂孤儿残留）一律谎报"已停止(远程)"
    - 新命令: taskkill 杀树 + ping 延迟1秒（timeout 命令在 stdin 重定向的
      非交互环境下报"输入重定向不支持"错误，改用 ping 计时）+ PowerShell
      按 PID 验证存活并输出 ALIVE/DEAD 标记（解析协议 Linux 分支已用）
    - Job 模式（启动端已绑 KILL_ON_JOB_CLOSE Job，见 start 端）下宿主 cmd
      被杀时其持有的最后一个 job 句柄关闭，内核连锁清杀全部孤儿后代
      （含 start /b 脱链进程）；NOJOB 降级模式验证宿主 PID 诚实报告
    """
    if platform and "windows" in platform.lower():
        return (
            f"taskkill /PID {pid} /T /F >nul 2>&1 & "
            f"ping -n 2 127.0.0.1 >nul & "
            f"powershell -NoProfile -NonInteractive -Command "
            f"\"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ 'ALIVE' }} else {{ 'DEAD' }}\""
        )
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
    """通过nest同步杀死远程daemon（CLI使用）

    与_check_remote_alive_pids_sync同策略的两级通道：
    1. 进程内nest连接（fly service/WebUI/CLI转发服务端进程内）
    2. fallback: 本地fly service HTTP API（CLI独立进程场景，进程内无连接）
    """
    cmd = _build_remote_kill_command(pid, platform)
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
            response = _run_async(conn.send_request(request, timeout=30.0))
            if not response.error:
                result = response.data or {}
            else:
                logger.debug(f"进程内nest连接kill失败(nest={nest_name}): {response.error}")
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"进程内nest连接异常(nest={nest_name}): {e}")

    # 2) fallback: 本地fly service HTTP API（CLI独立进程场景）
    if result is None:
        result = _http_nest_exec(nest_name, cmd, timeout=30)

    if result is None:
        return False, f"nest连接未找到: {nest_name}"
    return _parse_remote_kill_result(result, platform)


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


def _build_windows_daemon_bats(
    command: str, workdir: str | None, log_file: str, inner_bat_file: str
) -> tuple[str, str]:
    """构建Windows daemon的outer/inner两个bat内容

    用户命令不内联到outer的命令行（cmd引号规则无法任意嵌套：内联命令
    含引号+管道/&&时被宿主cmd错误解析），而是原样写入inner.bat独立执行：

    - inner（daemon_<id>_cmd.bat）: 用户命令原样写入（任意引号/管道/
      &&/exit无需转义），独立子cmd执行——exit只退inner宿主、退出码透传
    - outer（daemon_<id>.bat）: chcp 65001（UTF-8解析bat，中文命令可读）
      + 调inner（重定向日志）+ 捕获ERRORLEVEL写标记行（用户exit不影响，
      标记行必定执行）；">>"前保留空格（防"1>>"句柄歧义）；标记行前
      echo.空行（防程序末行无换行粘连）

    Returns:
        (outer_bat内容, inner_bat内容)
    """
    inner = f"@echo off\r\n{command}\r\n"
    lines = ["@echo off", "chcp 65001 >nul"]
    if workdir:
        lines.append(f'cd /d "{workdir}"')
    lines.append(f'cmd /s /c "{inner_bat_file}" >> "{log_file}" 2>&1')
    lines.append(f'echo.>> "{log_file}"')
    lines.append(f'echo {EXIT_CODE_MARKER}=%ERRORLEVEL% >> "{log_file}"')
    outer = "\r\n".join(lines) + "\r\n"
    return outer, inner


def wrap_command_with_exit_code(command: str, log_file: str) -> str:
    """Unix daemon命令包装：命令结束后把退出码写入日志尾部（供退出watcher解析）

    用户命令作为子bash执行（隔离用户命令中的exit/多行/控制结构），
    外层bash捕获其退出码后追加标记行，再以相同退出码退出
    （daemon PID即外层bash，killpg整组杀不受影响）。

    注意：
    - printf格式串前导\\n：用户命令末行无换行时（如部分程序print无\\n），
      防止标记行与末行输出粘连（预览可读性）；末行已有换行则多一空行，无害
    - %s接变量而非内插到格式串（防退出码数值被printf解释为格式符）
    """
    import shlex

    q_log = shlex.quote(log_file)
    return (
        f"bash -c {shlex.quote(command)}\n"
        f"__peng_ec=$?\n"
        f"printf '\\n{EXIT_CODE_MARKER}=%s\\n' \"$__peng_ec\" >> {q_log}\n"
        f"exit $__peng_ec"
    )


def parse_exit_code(log_tail: str) -> int | None:
    """从日志文本解析daemon退出码（取最后一次出现的标记行）

    Args:
        log_tail: 日志文本（通常为文件尾部片段）

    Returns:
        退出码；无标记时返回None
    """
    matches = re.findall(rf"{EXIT_CODE_MARKER}=(\-?\d+)", log_tail or "")
    return int(matches[-1]) if matches else None


# ============================================================
# Windows Job启动器（事务#2729：stop杀不净孤儿进程的根治）
# ============================================================
# 旧模式的缺陷：daemon宿主cmd.exe是唯一记录PID，taskkill /T 按PPID树杀后代；
# 当用户命令含 start /b（后台派生）时宿主cmd提前退出，孙进程成为孤儿
# （PPID链断裂），taskkill /T 杀不到，stop 谎报成功后残留进程继续运行
# （#2726: 测试实例残留导致 install.bat LNK1104 exe被锁失败多次）。
#
# 根治：宿主cmd + 其全部后代绑入 Job（KILL_ON_JOB_CLOSE）。Job成员关系由
# 内核按进程继承维护，孤儿进程也无法逃逸（Job未允许breakaway）。句柄链：
#   powershell创建job → CREATE_SUSPENDED挂起创建宿主cmd（零竞态：宿主未执行
#   任何代码，其后代必然继承Job）→ DuplicateHandle把job句柄复制进宿主进程
#   → Assign入Job → ResumeThread → powershell退出（自己那份句柄关闭，
#   宿主持有的副本维持job存活，不触发kill-on-close）。
# 此后宿主无论正常退出还是被taskkill强杀，其句柄表被清理→job最后一个句柄
# 关闭→内核自动杀掉Job内全部残余进程（含孤儿）。
# DuplicateHandle失败时不Assign（否则powershell退出时自己那份就是最后句柄，
# 会立即触发kill-on-close误杀daemon），降级NOJOB模式（行为同旧版）。

_WINDOWS_JOB_START_PS1 = r"""
param([string]$BatFile)
# peng daemon Windows Job launcher (#2729)
# Wraps the daemon host cmd.exe and ALL its descendants into a Job with
# KILL_ON_JOB_CLOSE. When the host exits (normally or killed by taskkill),
# its job handle is closed, and the kernel kills every leftover descendant,
# including orphan processes spawned via "start /b" (they cannot escape the
# Job because breakaway is not allowed).
# Output: PID=<n> and MODE=JOB or MODE=NOJOB (fallback without Job).

$ErrorActionPreference = 'Stop'

$src = @'
using System;
using System.Runtime.InteropServices;
public static class PengJobStart {
    [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    public static extern IntPtr CreateJobObject(IntPtr a, string n);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool SetInformationJobObject(IntPtr h, int c, ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION i, int len);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool AssignProcessToJobObject(IntPtr h, IntPtr p);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool DuplicateHandle(IntPtr sp, IntPtr sh, IntPtr tp, out IntPtr th, int access, bool inherit, int options);
    [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    public static extern bool CreateProcessW(IntPtr app, string cmd, IntPtr pa, IntPtr ta, bool inh, int flags, IntPtr env, IntPtr cwd, ref STARTUPINFO si, out PROCESS_INFORMATION pi);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern uint ResumeThread(IntPtr t);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool CloseHandle(IntPtr h);
    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
        public long PerProcessUserTimeLimit; public long PerJobUserTimeLimit;
        public uint LimitFlags; public UIntPtr MinimumWorkingSetSize; public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit; public UIntPtr Affinity; public uint PriorityClass; public uint SchedulingClass;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct IO_COUNTERS {
        public ulong ReadOperationCount; public ulong WriteOperationCount; public ulong OtherOperationCount;
        public ulong ReadTransferCount; public ulong WriteTransferCount; public ulong OtherTransferCount;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit; public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed; public UIntPtr PeakJobMemoryUsed;
    }
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    public struct STARTUPINFO {
        public int cb; public string lpReserved; public string lpDesktop; public string lpTitle;
        public int dwX; public int dwY; public int dwXSize; public int dwYSize;
        public int dwXCountChars; public int dwYCountChars; public int dwFillAttribute; public int dwFlags;
        public short wShowWindow; public short cbReserved2; public IntPtr lpReserved2;
        public IntPtr hStdInput; public IntPtr hStdOutput; public IntPtr hStdError;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct PROCESS_INFORMATION {
        public IntPtr hProcess; public IntPtr hThread; public int dwProcessId; public int dwThreadId;
    }
}
'@

function Write-Result([int]$ProcId, [string]$Mode) {
    Write-Output ("PID=" + $ProcId)
    Write-Output ("MODE=" + $Mode)
    exit 0
}

function Start-Fallback {
    # NOJOB degraded mode: plain Start-Process (orphan descendants cannot be
    # cleaned up automatically; stop falls back to taskkill /T behavior).
    $p = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', $BatFile -WindowStyle Hidden -PassThru
    Write-Result $p.Id 'NOJOB'
}

try { Add-Type -TypeDefinition $src } catch { Start-Fallback }

$job = [IntPtr]::Zero
try {
    $job = [PengJobStart]::CreateJobObject([IntPtr]::Zero, $null)
    if ($job -eq [IntPtr]::Zero) { Start-Fallback }
    $info = New-Object PengJobStart+JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    $info.BasicLimitInformation.LimitFlags = 0x2000
    $null = [PengJobStart]::SetInformationJobObject($job, 9, [ref]$info, [System.Runtime.InteropServices.Marshal]::SizeOf($info))
} catch { Start-Fallback }

$si = New-Object PengJobStart+STARTUPINFO
$si.cb = [System.Runtime.InteropServices.Marshal]::SizeOf($si)
$pi = New-Object PengJobStart+PROCESS_INFORMATION
$cmdline = 'cmd.exe /c "' + $BatFile + '"'
# CREATE_NO_WINDOW(0x08000000) | CREATE_SUSPENDED(0x4)
$created = $false
try {
    # Pass IntPtr.Zero as lpApplicationName and lpCurrentDirectory:
    # PowerShell marshals a $null STRING ARGUMENT into an EMPTY STRING, and
    # an empty string as lpApplicationName/lpCurrentDirectory fails with
    # ERROR_INVALID_NAME (123). IntPtr-typed params accept IntPtr.Zero cleanly.
    # (Struct string FIELDS initialized to $null do marshal as null pointers,
    # so STARTUPINFO's lpReserved/lpDesktop/lpTitle are safe as string.)
    $created = [PengJobStart]::CreateProcessW([IntPtr]::Zero, $cmdline, [IntPtr]::Zero, [IntPtr]::Zero, $false, 0x800004, [IntPtr]::Zero, [IntPtr]::Zero, [ref]$si, [ref]$pi)
} catch { $created = $false }
if (-not $created) { Start-Fallback }

$mode = 'NOJOB'
try {
    $dup = [IntPtr]::Zero
    # Duplicate the job handle INTO the suspended host process FIRST, only
    # then assign. If duplication fails we must not assign: with only our own
    # handle left, this PowerShell exiting would trigger KILL_ON_JOB_CLOSE
    # immediately and kill the daemon.
    $dupOk = [PengJobStart]::DuplicateHandle([PengJobStart]::GetCurrentProcess(), $job, $pi.hProcess, [ref]$dup, 0, $false, 2)
    if ($dupOk) {
        if ([PengJobStart]::AssignProcessToJobObject($job, $pi.hProcess)) { $mode = 'JOB' }
    } else {
        $null = [PengJobStart]::CloseHandle($job)
    }
} catch { }

$null = [PengJobStart]::ResumeThread($pi.hThread)
$null = [PengJobStart]::CloseHandle($pi.hThread)
$null = [PengJobStart]::CloseHandle($pi.hProcess)
Write-Result $pi.dwProcessId $mode
"""


def _windows_daemon_ps1_file(log_dir: str) -> str:
    """Job启动器ps1文件路径（各daemon日志目录共用一份，幂等覆盖写）"""
    import ntpath

    return ntpath.join(log_dir, "daemon_start.ps1")


def _parse_windows_daemon_start_output(stdout: str) -> tuple[int | None, bool]:
    """解析Windows Job启动器输出 → (pid, job_mode)

    输出协议（_WINDOWS_JOB_START_PS1）: "PID=<n>" + "MODE=JOB|NOJOB"
    MODE缺省视为NOJOB（降级，行为同旧版）
    """
    m_pid = re.search(r"PID=(\d+)", stdout or "")
    m_mode = re.search(r"MODE=(JOB|NOJOB)", stdout or "")
    pid = int(m_pid.group(1)) if m_pid else None
    job = bool(m_mode and m_mode.group(1) == "JOB")
    return pid, job


def _start_windows_daemon_native(bat_file: str, workdir: str | None) -> tuple[int, bool]:
    """Windows原生Job启动（ctypes实现，事务#2781替代PowerShell ps1启动器）

    🔧 背景事务#2778/#2781：旧ps1方案Add-Type每次新powershell进程都要
    csc.exe重新编译（无磁盘缓存），实测偶发2~9.5分钟卡顿（杀软实时扫描
    csc.exe/临时程序集+系统繁忙叠加，.peng/daemon下bat→log时间戳差为证），
    期间daemon工具一直不返回。本实现零编译零外部进程，毫秒级完成。

    流程与daemon_start.ps1完全一致：
    CreateJobObject(KILL_ON_JOB_CLOSE) →
    CreateProcessW(CREATE_SUSPENDED|CREATE_NO_WINDOW)宿主cmd →
    DuplicateHandle(job句柄→宿主进程，宿主存续期Job存活，父进程退出不误杀) →
    AssignProcessToJobObject → ResumeThread → CloseHandle×3。
    Job API任一步失败自动降级NOJOB（宿主裸跑，行为同ps1 Start-Fallback）。

    Returns:
        (宿主pid, job_mode)——job_mode=False表示NOJOB降级（无Job保护）
    """
    import ctypes
    from ctypes import wintypes

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            (n, ctypes.c_ulonglong)
            for n in (
                "ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount",
            )
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),  # ULONG_PTR
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", ctypes.c_void_p),
            ("hStdOutput", ctypes.c_void_p),
            ("hStdError", ctypes.c_void_p),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", ctypes.c_void_p),
            ("hThread", ctypes.c_void_p),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _v = ctypes.c_void_p
    k32.CreateJobObjectW.restype = _v
    k32.CreateJobObjectW.argtypes = [_v, wintypes.LPCWSTR]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = [_v, wintypes.INT, _v, wintypes.DWORD]
    k32.GetCurrentProcess.restype = _v
    k32.GetCurrentProcess.argtypes = []
    k32.DuplicateHandle.restype = wintypes.BOOL
    k32.DuplicateHandle.argtypes = [
        _v, _v, _v, ctypes.POINTER(_v), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
    ]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [_v, _v]
    k32.ResumeThread.restype = wintypes.DWORD
    k32.ResumeThread.argtypes = [_v]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [_v]
    k32.CreateProcessW.restype = wintypes.BOOL
    k32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, _v, _v, wintypes.BOOL,
        wintypes.DWORD, _v, wintypes.LPCWSTR, _v, _v,
    ]

    CREATE_SUSPENDED = 0x4
    CREATE_NO_WINDOW = 0x08000000
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9
    DUPLICATE_SAME_ACCESS = 0x2

    # ---- Job对象（失败降级NOJOB） ----
    job = None
    try:
        job = k32.CreateJobObjectW(None, None)
        if job:
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(
                job, JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info),
            ):
                k32.CloseHandle(job)
                job = None
    except Exception:
        if job:
            try:
                k32.CloseHandle(job)
            except Exception:
                pass
        job = None

    # ---- 创建宿主cmd（挂起+无窗口，句柄不继承） ----
    cmdline = ctypes.create_unicode_buffer(f'cmd.exe /c "{bat_file}"')
    si = _STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    pi = _PROCESS_INFORMATION()
    created = k32.CreateProcessW(
        None, cmdline, None, None, False,
        CREATE_SUSPENDED | CREATE_NO_WINDOW,
        None, workdir, ctypes.byref(si), ctypes.byref(pi),
    )
    if not created:
        err = ctypes.get_last_error()
        raise OSError(err, f"CreateProcessW失败(错误码{err}): cmd.exe /c \"{bat_file}\"")

    job_mode = False
    try:
        if job:
            # 先把job句柄复制进宿主进程再Assign（顺序关键，同ps1）：
            # 复制失败不Assign（防本函数CloseHandle(job)触发KILL_ON_JOB_CLOSE误杀）
            dup = k32.DuplicateHandle(
                k32.GetCurrentProcess(), job, pi.hProcess,
                ctypes.byref(_v()), 0, False, DUPLICATE_SAME_ACCESS,
            )
            if dup and k32.AssignProcessToJobObject(job, pi.hProcess):
                job_mode = True
        k32.ResumeThread(pi.hThread)
    finally:
        k32.CloseHandle(pi.hThread)
        k32.CloseHandle(pi.hProcess)
        if job:
            k32.CloseHandle(job)
    return pi.dwProcessId, job_mode


def start_local_daemon(
    command: str, workdir: str | Path | None, daemon_id: str
) -> tuple[int, str, bool]:
    """本地启动非阻塞daemon进程（与task进程完全脱钩）

    🔒 daemon进程不跟随task进程结束！只能自行结束或stop/kill结束。
    启动模式（关键：中间启动进程立即退出，daemon脱离task进程树）：
    - POSIX: 中间bash执行 "setsid nohup ... & echo $!" 后立即退出并reparent：
      daemon经setsid自成会话首进程，被init收养。
      task进程cancel时的kill_tree递归收集/proc/children不再包含daemon。
    - Windows（事务#2729改造）: bat包装daemon命令 + Job启动器ps1创建宿主cmd
      并绑入KILL_ON_JOB_CLOSE的Job（宿主及其全部后代含start /b孤儿均无法
      逃逸；宿主退出/被杀时内核自动清杀Job内全部残余进程）；
      Job创建失败时降级NOJOB（Start-Process，行为同旧版，孤儿无法自动清理）。
      宿主cmd父进程为powershell（立即退出），断开与task进程的进程树链。

    Args:
        command: 要运行的命令
        workdir: 工作目录
        daemon_id: daemon id（用于日志文件名）

    Returns:
        (pid, log_file绝对路径, job_mode)——job_mode=True表示已绑Job
        （stop杀宿主即连锁清杀全部后代），False为NOJOB降级模式

    Raises:
        OSError: 启动失败
    """
    import shlex

    log_dir, log_file = _log_paths(workdir, daemon_id)
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    job_mode = False
    if sys.platform == "win32":
        bat_file = Path(log_dir) / f"daemon_{daemon_id}.bat"
        inner_file = Path(log_dir) / f"daemon_{daemon_id}_cmd.bat"
        outer, inner = _build_windows_daemon_bats(
            command, str(workdir) if workdir else None, log_file, str(inner_file)
        )
        inner_file.write_text(inner, encoding="utf-8")
        bat_file.write_text(outer, encoding="utf-8")
        # 🔧 事务#2781：ctypes原生Job启动（毫秒级，零编译零外部进程）。
        # 旧PowerShell ps1方案Add-Type偶发2~9.5分钟卡顿（杀软扫描csc.exe，
        # 事务#2778 daemon工具长时间不返回的根因），弃用；ps1路径仅作为
        # 旧版nest服务回退保留（见start_remote_daemon）。
        pid, job_mode = _start_windows_daemon_native(
            str(bat_file), str(workdir) if workdir else None
        )
    else:
        inner = shlex.quote(wrap_command_with_exit_code(command, log_file))
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

    logger.info(
        f"daemon {daemon_id} 本地启动: pid={pid}, job={job_mode}, cmd={command[:100]}, log={log_file}"
    )
    return pid, str(Path(log_file).resolve()), job_mode


async def start_remote_daemon(
    client,
    command: str,
    workdir: str | None,
    daemon_id: str,
    is_windows: bool = False,
    platform: str | None = None,
    timeout: int = 30000,
) -> tuple[int, str, bool]:
    """通过nest远程启动非阻塞daemon进程

    Unix远程: setsid nohup bash -c 'cmd' >> log 2>&1 & echo DAEMON_PID=$!
    Windows远程（事务#2729改造）: Job启动器ps1创建宿主cmd并绑入
    KILL_ON_JOB_CLOSE的Job（宿主及全部后代含start /b孤儿无法逃逸，
    宿主退出/被杀时内核自动清杀Job内全部残余）；Job失败降级NOJOB
    （Start-Process，行为同旧版）

    Args:
        client: nest客户端（已set_nest_name）
        command: 要运行的命令
        workdir: 远程工作目录
        daemon_id: daemon id
        is_windows: 远程nest是否Windows平台
        platform: 远程平台字符串（用于记录）
        timeout: 启动命令超时（毫秒）

    Returns:
        (远程pid, 远程日志文件路径, job_mode)——job_mode=True表示已绑Job

    Raises:
        ToolError: 启动失败或无法解析PID
    """
    import shlex

    if is_windows or (platform or "").lower().startswith("win"):
        # 🔧 事务#2781：优先走原生daemon_start协议——nest服务端ctypes直接
        # Job启动（毫秒级返回）。旧ps1路径（PowerShell Add-Type）实测偶发
        # 2~9.5分钟卡顿（杀软扫描csc.exe，事务#2778 daemon工具长时间不
        # 返回的根因），仅作为旧版nest服务（无daemon_start处理器）回退。
        try:
            result = await client.daemon_start(
                command=command, workdir=workdir, daemon_id=daemon_id, timeout=timeout
            )
            pid = result.get("pid")
            if pid:
                return int(pid), result.get("log_file", ""), bool(result.get("job"))
        except Exception as e:
            if "不支持的请求类型" not in str(e):
                raise
            logger.warning(
                f"nest服务不支持daemon_start（旧版本），回退ps1路径: {e}"
            )

        # Windows远程：用户命令原样写入inner.bat（零转义），outer.bat调用并
        # 捕获退出码（经nest write_file写bat），Job启动器ps1启动
        # （wmic在Win11 24H2+已移除，PowerShell全版本可用）
        wd = workdir or "%CD%"
        log_dir = f"{wd}\\.peng\\daemon"
        log_file = f"{log_dir}\\daemon_{daemon_id}.log"
        bat_file = f"{log_dir}\\daemon_{daemon_id}.bat"
        inner_file = f"{log_dir}\\daemon_{daemon_id}_cmd.bat"

        outer, inner = _build_windows_daemon_bats(command, workdir, log_file, inner_file)
        ps1_file = _windows_daemon_ps1_file(log_dir)
        try:
            await client.write_file(inner_file, inner)
            await client.write_file(bat_file, outer)
            # ps1写文件后以-File执行：长脚本不经cmd命令行（8191字符限制+引号转义地狱）
            await client.write_file(ps1_file, _WINDOWS_JOB_START_PS1)
        except Exception as e:
            from peng.tools.base import ToolError

            raise ToolError(f"远程写入daemon启动文件失败: {e}") from e

        start_cmd = (
            f'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass '
            f'-File "{ps1_file}" -BatFile "{bat_file}"'
        )
        # ps1含Add-Type首次编译（实测冷启动可达30s+，受杀软扫描影响），
        # 执行超时取max(启动timeout, 120s)防误杀启动器
        ps_timeout = max(timeout, 120000)
        result = await client.bash(command=start_cmd, cwd=workdir, timeout=ps_timeout)
        stdout = (result.get("stdout", "") or "") + "\n" + (result.get("stderr", "") or "")
        pid, job_mode = _parse_windows_daemon_start_output(stdout)
        if pid is None:
            from peng.tools.base import ToolError

            raise ToolError(
                f"远程daemon启动失败（无法解析PowerShell输出的PID）: {stdout[:200]} "
                f"{result.get('stderr', '')[:200]}"
            )
        return pid, log_file, job_mode

    # Unix远程
    wd = workdir or "."
    log_dir = f"{wd}/.peng/daemon"
    log_file = f"{log_dir}/daemon_{daemon_id}.log"
    daemon_cmd = (
        f"nohup bash -c "
        f"{shlex.quote(wrap_command_with_exit_code(command, log_file))} "
        f">> {shlex.quote(log_file)} 2>&1 < /dev/null"
    )
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
    # Unix无Job概念（False）：Linux依赖setsid进程组杀整组，macOS用kill+pkill -P
    return int(m.group(1)), log_file, False


# ============================================================
# daemon退出检测辅助（daemon工具watcher使用）
# ============================================================

_LOG_TAIL_BYTES = 4096  # 日志尾部读取字节数（足够包含退出码标记行）

# ANSI转义序列清理正则：CSI序列（\x1b[...字母，含光标移动/SGR/擦除）、
# OSC序列（\x1b]...\x07或\x1b\\，如超链接）、单字符转义（\x1bM等）
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-Z\\-_]"
)
# 除\n\t外的C0控制字符（\r单独预处理：进度条场景保留每段末状态）
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _decode_log_tail(data: bytes, drop_first_line: bool = False) -> str:
    """daemon日志尾部字节块→干净文本（多编码探测+清理）

    处理三重乱码/污染源（事务#1909实测）：
    1. 编码不确定：程序日志可能UTF-8（Unix/现代程序）或GBK（中文Windows程序），
       探测顺序 utf-8 strict → gbk strict → utf-8 replace 兜底（与nest端
       _decode_output同思路）
    2. 尾部切割碎字符：从文件中间切割时首行可能被切碎多字节字符/截断半行，
       由调用方传drop_first_line=True丢弃首行（退出码标记行在文件最末不受影响；
       文件小于读取窗口时块即全文，首行是真实内容不能丢）
    3. 终端污染：ANSI转义序列（颜色/光标）与控制字符清理；
       \\r为进度条回刷→按\\n分段（每段即该行最终状态）

    Args:
        data: 日志文件尾部原始字节块
        drop_first_line: 块首行是否为切割碎行（offset>0时True）

    Returns:
        清理后的文本；空字节返回空字符串
    """
    if not data:
        return ""
    # 探测编码（strict失败逐级降级；latin-1永不失败但二进制无意义，不入列）
    text: str | None = None
    for encoding in ("utf-8", "gbk"):
        try:
            text = data.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        text = data.decode("utf-8", errors="replace")

    # \r回刷→\n（进度条多帧分段，预览取尾部行自然保留最终状态）
    text = text.replace("\r", "\n")
    # 丢弃碎首行（仅中间切割时）
    if drop_first_line:
        first_nl = text.find("\n")
        if first_nl >= 0:
            text = text[first_nl + 1 :]
    # 清理ANSI转义序列与控制字符
    text = _ANSI_ESCAPE_RE.sub("", text)
    text = _CONTROL_CHARS_RE.sub("", text)
    return text


def read_local_log_tail(log_file: str, max_bytes: int = _LOG_TAIL_BYTES) -> str:
    """读取本地日志文件尾部并解码为干净文本（多编码探测，见_decode_log_tail）

    Returns:
        尾部文本；读取失败返回空字符串
    """
    try:
        with open(log_file, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            offset = max(0, size - max_bytes)
            f.seek(offset)
            return _decode_log_tail(f.read(), drop_first_line=offset > 0)
    except OSError as e:
        logger.debug(f"读取日志尾部失败({log_file}): {e}")
        return ""


async def check_daemon_alive(entry: dict[str, Any]) -> bool | None:
    """检查daemon进程是否存活

    - 本地(nest=None): process_exists 直查
    - 远程: 经nest执行批量存活检查命令（复用_build_remote_check_command）

    Args:
        entry: daemon注册表条目

    Returns:
        True=存活 False=已退出 None=远程查询失败（保守视为存活）
    """
    from peng.common.platform import process_exists

    nest_name = normalize_nest(entry.get("nest"))
    pid = int(entry.get("pid") or 0)
    if pid <= 0:
        return False
    if nest_name is None:
        return process_exists(pid)

    from peng.nest.client import bash as nest_bash

    cmd = _build_remote_check_command([pid], entry.get("platform"))
    try:
        resp = await nest_bash(command=cmd, timeout=15000, nest_name=nest_name)
        if resp.error:
            logger.debug(f"远程daemon存活检查失败({nest_name}): {resp.error}")
            return None
        return pid in _parse_alive_pids((resp.data or {}).get("stdout", ""))
    except Exception as e:
        logger.debug(f"远程daemon存活检查异常({nest_name}): {e}")
        return None


async def _read_remote_log_tail(nest_name: str, log_file: str) -> tuple[bytes, bool]:
    """经nest read_bytes协议读取远程日志尾部字节（base64传输，二进制安全）

    走字节协议彻底绕过命令行文本编码层（PowerShell按ANSI误读UTF-8日志、
    nest端_decode_output二次转换等不可逆破坏，事务#1909实测乱码根源），
    Windows/Unix通用，编码探测在Python端统一进行（_decode_log_tail）。

    Returns:
        (尾部字节块(≤_LOG_TAIL_BYTES), 是否从文件中间切割(offset>0))；失败返回(b"", False)
    """
    import base64

    from peng.nest.client import read_bytes as nest_read_bytes

    try:
        # 第一次请求：探测文件总大小（chunk_size=1，total_size随响应返回）
        probe = await nest_read_bytes(log_file, offset=0, chunk_size=1, nest_name=nest_name)
        if probe.error:
            logger.debug(f"远程探测日志大小失败({nest_name}): {probe.error}")
            return b"", False
        total_size = int((probe.data or {}).get("total_size") or 0)
        if total_size <= 0:
            return b"", False
        offset = max(0, total_size - _LOG_TAIL_BYTES)
        resp = await nest_read_bytes(
            log_file,
            offset=offset,
            chunk_size=_LOG_TAIL_BYTES,
            nest_name=nest_name,
        )
        if resp.error:
            logger.debug(f"远程读取日志尾部失败({nest_name}): {resp.error}")
            return b"", False
        content_b64 = (resp.data or {}).get("content_b64") or ""
        data = base64.b64decode(content_b64) if content_b64 else b""
        return data, offset > 0
    except Exception as e:
        logger.debug(f"远程读取日志尾部异常({nest_name}): {e}")
        return b"", False


async def read_daemon_exit_code(entry: dict[str, Any]) -> tuple[int | None, str]:
    """读取daemon日志尾部并解析退出码

    - 本地: 直读日志文件尾部
    - 远程: 经nest read_bytes字节协议读取（多编码探测统一在解码层）

    Args:
        entry: daemon注册表条目（含log_file/nest/platform）

    Returns:
        (exit_code, log_tail)：exit_code解析不到时为None（容错，不阻断提醒注入）
    """
    log_file = entry.get("log_file") or ""
    if not log_file:
        return None, ""

    nest_name = normalize_nest(entry.get("nest"))
    if nest_name is None:
        tail = read_local_log_tail(log_file)
        return parse_exit_code(tail), tail

    data, from_middle = await _read_remote_log_tail(nest_name, log_file)
    tail = _decode_log_tail(data, drop_first_line=from_middle)
    return parse_exit_code(tail), tail

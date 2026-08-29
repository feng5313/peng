"""
Nest CLI - 命令行界面

子命令：
- nest install [--name NAME] [--host HOST] [--port PORT]: 安装并启动 nest 服务（支持多实例）
- nest uninstall [--name NAME]: 停止并卸载 nest 服务（不指定名称则卸载所有）
- nest start: 启动 nest 服务
- nest stop: 停止 nest 服务
- nest restart: 重启 nest 服务
- nest status: 查看 nest 服务状态

多实例说明：
- 每个实例使用独立的任务计划/systemd 服务
- 未指定名称时自动根据 host 命名
- Windows 任务名称: PengNest-<name>
- Linux 服务名称: peng-nest-<name>

Linux: 注册为 systemd user 服务
Windows: 使用 schtasks 任务计划程序（以用户身份运行，可访问桌面）
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# subprocess 通用参数：避免 Windows GBK 编码问题
_SUBPROC_RUN_KW = dict(encoding="utf-8", errors="replace")


def _safe_print(msg: str, **kwargs) -> None:
    """安全打印，处理 Windows GBK 编码问题"""
    kwargs.setdefault("flush", True)
    try:
        print(msg, **kwargs)
    except UnicodeEncodeError:
        safe_msg = msg.encode("ascii", errors="replace").decode("ascii")
        print(safe_msg, **kwargs)


# 服务名称
SERVICE_NAME = "peng-nest"
SERVICE_DISPLAY_NAME = "Peng Nest (鹏窝)"
SERVICE_DESCRIPTION = "鹏窝 - 远程控制服务"
# Windows 任务计划前缀（实际名称为 PengNest-<name>）
TASK_NAME_PREFIX = "PengNest"


def _get_project_root() -> Path:
    """获取项目根目录"""
    return Path(__file__).resolve().parent.parent.parent.parent


def _get_nest_script() -> Path:
    """获取 nest 入口脚本路径"""
    return _get_project_root() / "bin" / "nest"


def _get_python_cmd() -> list[str]:
    """获取 Python 命令"""
    project_root = _get_project_root()
    if sys.platform == "win32":
        # 优先使用 pythonw.exe（无控制台窗口，适合后台服务）
        venv_pythonw = project_root / ".venv" / "Scripts" / "pythonw.exe"
        if venv_pythonw.exists():
            return [str(venv_pythonw)]
        venv_python = project_root / ".venv" / "Scripts" / "python.exe"
    else:
        venv_python = project_root / ".venv" / "bin" / "python"

    if venv_python.exists():
        return [str(venv_python)]
    return [sys.executable]


def _get_start_cmd(host: str | None = None, port: int | None = None) -> list[str]:
    """获取服务启动命令

    优先使用传入参数，否则从 config/nest.json 读取配置。
    """
    cmd = _get_python_cmd() + [str(_get_nest_script()), "connect"]

    if host is None and port is None:
        # 从 config 读取
        try:
            config = _load_nest_config()
            instances = config.get("instances", {})
            if instances:
                inst = next(iter(instances.values()))
                cfg_host = inst.get("host", "localhost")
                cfg_port = inst.get("port", 8899)
                cmd.extend(["--host", cfg_host])
                cmd.extend(["--port", str(cfg_port)])
        except Exception:
            pass
    else:
        # 使用传入参数
        if host is not None:
            cmd.extend(["--host", host])
        if port is not None:
            cmd.extend(["--port", str(port)])

    return cmd


def _get_task_name(name: str | None = None) -> str:
    """根据实例名生成 Windows 任务计划名称

    Args:
        name: 实例名称，None 则返回默认名称（向后兼容）
    """
    if name:
        return f"{TASK_NAME_PREFIX}-{name}"
    return TASK_NAME_PREFIX


def _get_systemd_service_name(name: str | None = None) -> str:
    """根据实例名生成 Linux systemd 服务名称"""
    if name:
        return f"{SERVICE_NAME}-{name}"
    return SERVICE_NAME


def _auto_name(host: str | None = None) -> str:
    """自动生成实例名

    使用系统 hostname 作为实例名。
    host 参数保留用于兼容，但不影响命名。
    """
    import platform

    base = platform.node().strip().lower()
    if not base:
        base = "default"

    return base


# ==================== 通用操作 ====================


def _is_service_running(name: str | None = None) -> bool:
    """检查服务是否正在运行"""
    if sys.platform == "win32":
        return _is_service_running_windows(name)
    else:
        return _is_service_running_linux(name)


def _is_service_installed(name: str | None = None) -> bool:
    """检查服务是否已安装"""
    if sys.platform == "win32":
        return _is_service_installed_windows(name)
    else:
        return _is_service_installed_linux(name)


# ==================== Windows (schtasks 任务计划程序) ====================

# 任务计划名称（向后兼容）
TASK_NAME = "PengNest"


def _is_task_installed_by_name(task_name: str) -> bool:
    """Windows: 检查指定名称的 schtasks 任务是否已创建"""
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", task_name],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _is_task_installed_windows() -> bool:
    """Windows: 检查默认 schtasks 任务是否已创建"""
    return _is_task_installed_by_name(TASK_NAME)


def _is_service_installed_windows(name: str | None = None) -> bool:
    """Windows: 检查 nest 任务计划是否已创建"""
    return _is_task_installed_by_name(_get_task_name(name))


def _is_service_running_windows(name: str | None = None) -> bool:
    """Windows: 检查 nest 进程是否在运行"""
    return _is_nest_process_running(name)


def _is_nest_process_running(name: str | None = None) -> bool:
    """检查是否有 nest 进程在运行（serve 或 connect 命令）

    Args:
        name: 实例名称，None 则检查所有 nest 进程
    """
    try:
        import psutil

        my_pid = os.getpid()
        for proc in psutil.process_iter(["pid", "name", "cmdline", "exe"]):
            try:
                if proc.info["pid"] == my_pid:
                    continue
                exe = (proc.info.get("exe") or "").lower()
                # 只检查 python/pythonw 进程
                if not ("python" in exe):
                    continue
                cmdline = proc.info.get("cmdline") or []
                cmdline_str = " ".join(cmdline)
                # 匹配包含 nest 脚本路径或 nest 入口模块的进程
                if "nest" in cmdline_str and ("connect" in cmdline_str or "serve" in cmdline_str):
                    # 如果指定了名称，只匹配含该名称的进程
                    if name:
                        try:
                            name_idx = cmdline.index("--name")
                            if cmdline[name_idx + 1] != name:
                                continue
                        except (ValueError, IndexError):
                            continue
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except ImportError:
        pass

    # 后备方案：通过 wmic 查找
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                [
                    "wmic",
                    "process",
                    "where",
                    "name='python.exe' or name='pythonw.exe'",
                    "get",
                    "CommandLine,ProcessId",
                ],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            for line in result.stdout.split("\n"):
                if "nest" in line and ("connect" in line or "serve" in line):
                    return True
        except Exception:
            pass

    return False


def _get_current_user() -> str:
    """获取当前用户名"""
    import os

    return os.environ.get("USERNAME", "")


def _kill_all_nest_processes(name: str | None = None) -> int:
    """杀掉所有 nest 相关进程

    Args:
        name: 实例名称，None 则杀掉所有 nest 进程

    Returns:
        杀掉的进程数量
    """
    killed = 0
    access_denied_pids: list[int] = []  # 权限不足未杀掉的 PID

    def _match_name(cmdline: list[str], target_name: str | None) -> bool:
        """检查进程命令行是否匹配指定名称"""
        if not target_name:
            return True
        try:
            name_idx = cmdline.index("--name")
            return cmdline[name_idx + 1] == target_name
        except (ValueError, IndexError):
            return False

    def _match_name_wmic(line: str, target_name: str | None) -> bool:
        """检查 wmic 命令行文本是否匹配指定名称"""
        if not target_name:
            return True
        return f"--name {target_name}" in line

    # 方法1: 用 psutil 查找并杀进程
    try:
        import psutil

        my_pid = os.getpid()
        for proc in psutil.process_iter(["pid", "name", "cmdline", "exe"]):
            try:
                if proc.info["pid"] == my_pid:
                    continue
                exe = (proc.info.get("exe") or "").lower()
                if "python" not in exe:
                    continue
                cmdline = proc.info.get("cmdline") or []
                cmdline_str = " ".join(cmdline)
                if "nest" in cmdline_str and ("connect" in cmdline_str or "serve" in cmdline_str):
                    if not _match_name(cmdline, name):
                        continue
                    try:
                        proc.kill()
                        killed += 1
                    except psutil.AccessDenied:
                        # 权限不足（如 SYSTEM 进程），记录 PID 后续用 UAC 提权杀
                        access_denied_pids.append(proc.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except ImportError:
        pass

    # 方法2: 用 wmic + taskkill 查找 python 进程中包含 bin/nest 的
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                [
                    "wmic",
                    "process",
                    "where",
                    "name='python.exe' or name='pythonw.exe'",
                    "get",
                    "ProcessId,CommandLine",
                ],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            for line in result.stdout.split("\n"):
                if "nest" in line and ("connect" in line or "serve" in line):
                    if not _match_name_wmic(line, name):
                        continue
                    parts = line.strip().split()
                    for part in reversed(parts):
                        try:
                            pid = int(part)
                            r = subprocess.run(
                                ["taskkill", "/F", "/T", "/PID", str(pid)],
                                capture_output=True,
                                timeout=10,
                            )
                            if r.returncode == 0:
                                killed += 1
                            elif pid not in access_denied_pids:
                                access_denied_pids.append(pid)
                            break
                        except ValueError:
                            continue
        except Exception:
            pass

    # 方法3: UAC 提权杀权限不足的进程（如 SYSTEM 用户运行的子进程）
    if sys.platform == "win32" and access_denied_pids:
        _safe_print(f"[nest] 普通权限无法终止 {len(access_denied_pids)} 个进程，尝试 UAC 提权...")
        _kill_pids_elevated(access_denied_pids)
        # 验证是否成功
        import psutil

        for pid in access_denied_pids:
            if not psutil.pid_exists(pid):
                killed += 1

    return killed


def _kill_pids_elevated(pids: list[int]) -> bool:
    """通过 UAC 提权用 taskkill 杀多个进程

    Args:
        pids: 要杀的 PID 列表

    Returns:
        是否成功执行提权命令
    """
    if not pids or sys.platform != "win32":
        return False

    import ctypes
    from pathlib import Path

    # 构建多行 taskkill 命令
    commands = " & ".join(f"taskkill /F /PID {pid}" for pid in pids)
    log_dir = Path(__file__).resolve().parent.parent.parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = str(log_dir / "nest_uac_kill.log")

    try:
        ret = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            "cmd",
            f'/c {commands} >> "{log_file}" 2>&1',
            None,
            0,  # SW_HIDE
        )
        if ret <= 32:
            _safe_print(f"[nest] UAC 提权失败 (返回值: {ret})")
            return False

        # 等待 UAC 确认和进程终止
        time.sleep(3)
        return True
    except Exception as e:
        _safe_print(f"[nest] UAC 提权异常: {e}")
        return False


def _is_task_currently_running_windows(task_name: str) -> bool:
    """Windows: 通过 schtasks 检查任务是否正在运行

    此方法可跨提权边界检测，因为 schtasks /query 查询的是 Task Scheduler 服务，
    不受调用进程权限级别影响（只要任务是当前用户创建的）。

    注意：schtasks 在中文 Windows 上输出 GBK 编码，不能使用 encoding="utf-8"。
    使用 bytes 模式匹配来避免编码问题。

    Args:
        task_name: 任务计划名称

    Returns:
        True: 任务正在运行
    """
    try:
        # 注意：不指定 encoding，使用 bytes 模式避免 GBK/UTF-8 编码问题
        result = subprocess.run(
            ["schtasks", "/query", "/tn", task_name, "/fo", "csv", "/nh"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode != 0:
            return False
        # schtasks /fo csv 输出格式: "\\TaskName","Next Run Time","Status"
        # 在 bytes 中检测状态：
        # - 英文: b"Running"
        # - 中文 GBK: b"\xd5\xfd\xd4\xda\xd4\xcb\xd0\xd0" (正在运行)
        stdout = result.stdout
        if b"Running" in stdout or b"\xd5\xfd\xd4\xda\xd4\xcb\xd0\xd0" in stdout:
            return True
        return False
    except Exception:
        return False


def _run_elevated(
    cmd_str: str, wait_for_running: bool = True, name: str | None = None
) -> tuple[bool, str]:
    """Windows: 通过 UAC 提权执行命令，使用 bat 文件并验证结果

    Args:
        cmd_str: 要执行的命令字符串（多个命令用 & 连接）
        wait_for_running: 是否等待 nest 进程启动
        name: 实例名称，用于等待时匹配特定进程

    Returns:
        (成功标志, 消息)
    """
    import ctypes
    

    project_root = _get_project_root()
    log_dir = project_root / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = str(log_dir / "nest_uac.log")
    bat_file = str(log_dir / "nest_uac.bat")

    # 写入 bat 文件
    with open(bat_file, "w", encoding="utf-8") as f:
        f.write("@echo off\n")
        f.write(f'echo [{time.strftime("%Y-%m-%d %H:%M:%S")}] UAC start >> "{log_file}" 2>&1\n')
        f.write(f'{cmd_str} >> "{log_file}" 2>&1\n')
        f.write(
            f'echo [{time.strftime("%Y-%m-%d %H:%M:%S")}] UAC end, exitcode=%ERRORLEVEL% >> "{log_file}" 2>&1\n'
        )

    ret = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        "cmd",
        f'/c "{bat_file}"',
        None,
        0,
    )
    if ret <= 32:
        return False, f"UAC 提升失败 (返回值: {ret})，可能用户取消了弹窗"

    if not wait_for_running:
        time.sleep(3)
        return True, ""

    # 等待 nest 进程启动
    task_name = _get_task_name(name) if name else TASK_NAME_PREFIX

    for i in range(30):
        time.sleep(1)
        if _is_nest_process_running(name):
            return True, ""
        if _is_task_currently_running_windows(task_name):
            return True, ""

    # 等待超时，读取日志
    if Path(log_file).exists():
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                log_content = f.read()
            if (
                "error" in log_content.lower()
                or "failed" in log_content.lower()
                or "拒绝" in log_content
            ):
                return False, f"安装失败: {log_content[-500:]}"
        except Exception:
            pass
    return False, "等待进程启动超时，请检查 logs/nest_uac.log"


def _create_task_xml(task_name: str, command: str, args: str) -> str:
    """生成 schtasks 任务的 XML 配置

    特性：
    - 登录时自动启动，且每5分钟重复运行作为崩溃恢复安全网
    - 失败后1分钟自动重启（RestartOnFailure）
    - 电池模式不停止
    - 空闲状态不停止
    - 同名任务不重复启动（IgnoreNew）
    - nest connect 内部有 while self._running 无限重连循环+5秒冷却时间，
      正常运行时不会退出，schtasks 的5分钟重复只是崩溃恢复的安全网
    """
    from datetime import datetime

    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Peng Nest ({task_name}) - 鹏窝远程控制服务</Description>
    <Date>{now}</Date>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <GroupId>S-1-5-4</GroupId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
  </Settings>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </LogonTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{args}</Arguments>
    </Exec>
  </Actions>
</Task>"""


def install_service_windows(
    name: str | None = None, host: str | None = None, port: int | None = None
) -> tuple[bool, str]:
    """Windows: 通过任务计划程序安装 nest（以当前用户身份运行，可访问桌面）

    使用 schtasks 创建计划任务：
    - /ru Interactive: 以当前登录用户身份运行（可截屏/键鼠输入）
    - /rl highest: 最高权限运行
    - /sc onlogon: 用户登录时自动启动
    - 通过 UAC 提权创建（需要管理员权限创建 /rl highest 任务）

    Args:
        name: 实例名称（从 config/nest.json 中查找）
        host: 远程 peng 服务地址（覆盖配置）
        port: 远程 peng 服务端口（覆盖配置）

    Returns:
        (成功标志, 消息)
    """
    task_name = _get_task_name(name)
    display_name = f"nest({name})" if name else "nest"

    # 先杀掉该实例的旧进程
    _safe_print(f"[INFO] 清理 {display_name} 旧进程...")
    _kill_all_nest_processes(name)
    time.sleep(2)

    # 根据 name 或 host/port 确定启动命令
    if name:
        config = _load_nest_config()
        instances = config.get("instances", {})
        inst_key = name if name in instances else None
        if not inst_key:
            for k, v in instances.items():
                if v.get("name") == name:
                    inst_key = k
                    break
        if not inst_key:
            _safe_print(f"[INFO] 实例 '{name}' 不存在于 config/nest.json 中，自动创建")
            inst_key = name
            instances[name] = {
                "name": name,
                "workdir": str(_get_project_root()),
                "host": host or "localhost",
                "port": port or 8899,
                "reconnect_interval": 5,
            }
            config["instances"] = instances
            import json

            config_path = _get_project_root() / "config" / "nest.json"
            config_path.write_text(
                json.dumps(config, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        inst = instances[inst_key]
        inst_host = inst.get("host", "localhost")
        inst_port = inst.get("port", 8899)
        if host and ":" in host:
            h, p = host.rsplit(":", 1)
            try:
                inst_host = h
                inst_port = int(p)
            except ValueError:
                pass
        elif host:
            inst_host = host
        if port:
            inst_port = port
        start_cmd = _get_python_cmd() + [
            str(_get_nest_script()),
            "connect",
            "--name",
            inst_key,
            "--host",
            inst_host,
            "--port",
            str(inst_port),
        ]
    elif host or port:
        start_cmd = _get_python_cmd() + [str(_get_nest_script()), "connect"]
        if host:
            start_cmd.extend(["--host", host])
        if port:
            start_cmd.extend(["--port", str(port)])
    else:
        start_cmd = _get_start_cmd()

    # 构建 schtasks 命令
    app_args = " ".join(f'"{c}"' if " " in c else c for c in start_cmd)

    # 生成任务 XML 并通过临时文件创建（比 schtasks /create 命令行参数更完整可靠）
    python_cmd = start_cmd[0]  # python/pythonw 可执行文件
    nest_args = " ".join(start_cmd[1:])  # bin/nest connect --name xxx --host xxx --port xxx
    task_xml = _create_task_xml(task_name, python_cmd, nest_args)

    # 写入临时 XML 文件
    xml_file = str(_get_project_root() / "logs" / f"nest_task_{task_name}.xml")
    Path(xml_file).parent.mkdir(exist_ok=True)
    with open(xml_file, "w", encoding="utf-16") as f:
        f.write(task_xml)

    # 先删除旧任务，再用 XML 创建新任务，最后立即运行
    cmd_parts = [
        f'schtasks /delete /tn "{task_name}" /f 2>NUL',
        f'schtasks /create /tn "{task_name}" /xml "{xml_file}" /f',
        f'schtasks /run /tn "{task_name}"',
    ]

    cmd_str = " & ".join(cmd_parts)
    _safe_print(f"[INFO] 创建任务计划: {task_name}")
    _safe_print(f"[INFO] 启动命令: {app_args}")

    return _run_elevated(cmd_str, wait_for_running=True, name=name)


def get_schtasks_install_commands_windows() -> list[str] | None:
    """
    Windows: 返回 nest 安装所需的 schtasks 命令列表。

    返回 schtasks 命令供 fly install 通过 UAC 提权后一次性执行。
    以当前用户身份运行（/ru Interactive），可访问桌面（截屏/键鼠输入）。

    Returns:
        命令列表，如果已经在运行则返回 None
    """
    # 检查是否已经有 nest 进程在运行
    if _is_nest_process_running():
        return None

    start_cmd = _get_start_cmd()
    python_cmd = start_cmd[0]
    nest_args = " ".join(f'"{c}"' if " " in c else c for c in start_cmd[1:])

    # 生成 XML 并写入临时文件
    task_xml = _create_task_xml(TASK_NAME, python_cmd, nest_args)
    xml_file = str(_get_project_root() / "logs" / f"nest_task_{TASK_NAME}.xml")
    Path(xml_file).parent.mkdir(exist_ok=True)
    with open(xml_file, "w", encoding="utf-16") as f:
        f.write(task_xml)

    commands = [
        f'schtasks /delete /tn "{TASK_NAME}" /f 2>NUL',
        f'schtasks /create /tn "{TASK_NAME}" /xml "{xml_file}" /f',
        f'schtasks /run /tn "{TASK_NAME}"',
    ]

    return commands


def uninstall_service_windows(name: str | None = None) -> tuple[bool, str]:
    """Windows: 卸载 nest（停止进程 + 删除任务计划）

    只弹一次 UAC：将停止命令和删除命令合并到一次提权中执行。

    Args:
        name: 实例名称，None 则卸载所有 nest 实例
    """
    display_name = f"nest({name})" if name else "所有 nest"

    # 收集需要操作的任务名
    task_names_to_operate: list[str] = []
    if name:
        task_names_to_operate.append(_get_task_name(name))
    else:
        task_names_to_operate = _list_all_nest_task_names_windows()

    if not task_names_to_operate:
        # 无任务计划，只尝试杀进程
        _kill_all_nest_processes(name)
        return True, f"{display_name} 无需卸载（无任务计划）"

    # 先尝试非提权方式杀进程（同权限级别有效）
    _kill_all_nest_processes(name)
    time.sleep(1)

    # 收集需要通过 UAC 停止和删除的命令
    # 检测哪些任务仍在运行（跨提权边界检测）
    uac_commands: list[str] = []
    for task_name in task_names_to_operate:
        if not _is_task_installed_by_name(task_name):
            continue
        # 如果任务仍在运行，先 /end 停止
        if _is_task_currently_running_windows(task_name):
            uac_commands.append(f'schtasks /end /tn "{task_name}"')
        # 删除任务
        uac_commands.append(f'schtasks /delete /tn "{task_name}" /f')

    if not uac_commands:
        return True, f"{display_name} 已卸载"

    # 一次性 UAC 提权：停止 + 删除合并
    cmd_str = " & ".join(uac_commands)
    _safe_print(f"[INFO] 卸载 {display_name}，通过 UAC 停止并删除任务计划...")
    ok, _ = _run_elevated(cmd_str, wait_for_running=False)
    if not ok:
        return False, f"{display_name} 卸载失败（UAC 提权失败）"

    time.sleep(2)

    # 验证：检查任务是否已删除
    remaining = [tn for tn in task_names_to_operate if _is_task_installed_by_name(tn)]
    if not remaining:
        return True, f"{display_name} 已卸载（任务计划已删除）"

    # 如果任务还在，再用 taskkill + delete 做第二次 UAC
    # （schtasks /end 对 /rl highest 进程可能无效）
    _safe_print(f"[INFO] 任务仍在运行，尝试 taskkill 强制终止...")
    kill_commands: list[str] = []
    for tn in remaining:
        kill_commands.append(f'taskkill /F /FI "WINDOWTITLE eq {tn}*" 2>NUL')
        kill_commands.append(f'schtasks /delete /tn "{tn}" /f')
    ok2, _ = _run_elevated(" & ".join(kill_commands), wait_for_running=False)
    if ok2:
        time.sleep(2)

    remaining2 = [tn for tn in task_names_to_operate if _is_task_installed_by_name(tn)]
    if not remaining2:
        return True, f"{display_name} 已卸载（强制终止后任务计划已删除）"

    return True, f"{display_name} 部分卸载，请检查 UAC 弹窗或用管理员终端执行"


def _list_all_nest_task_names_windows() -> list[str]:
    """Windows: 列出所有 PengNest 相关的任务计划名称

    注意：schtasks /query /fo csv 输出的任务名带有前导反斜杠（如 \\PengNest-epyc），
    需要去除前导反斜杠后再匹配。
    """
    task_names = []
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/fo", "csv", "/nh"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        for line in result.stdout.split("\n"):
            # CSV 格式: "\\TaskName","Next Run Time","Status"
            line = line.strip()
            if not line:
                continue
            # 提取任务名（去掉引号和前导反斜杠）
            parts = line.split(",")
            if parts:
                tn = parts[0].strip('"').lstrip("\\")
                if tn == TASK_NAME_PREFIX or tn.startswith(f"{TASK_NAME_PREFIX}-"):
                    task_names.append(tn)
    except Exception:
        # 后备：至少包含默认任务名
        task_names.append(TASK_NAME_PREFIX)
    return task_names


def start_service_windows(name: str | None = None) -> tuple[bool, str]:
    """Windows: 启动 nest（通过 UAC 提权运行 schtasks /run，禁止直接启动）

    Args:
        name: 实例名称，None 则启动默认实例
    """
    task_name = _get_task_name(name)

    if _is_nest_process_running(name):
        return True, "nest 进程已在运行"

    if not _is_task_installed_by_name(task_name):
        return False, "请先运行 nest install 安装服务"

    # 直接通过 UAC 提权运行 schtasks /run
    ok, msg = _run_elevated(
        f'schtasks /run /tn "{task_name}"',
        wait_for_running=True,
        name=name,
    )
    if ok:
        time.sleep(2)
        if _is_nest_process_running(name):
            return True, "已启动"
        return False, "启动失败，请检查日志"
    return False, "启动失败，请在弹窗中点击「是」"


def stop_service_windows(name: str | None = None) -> tuple[bool, str]:
    """Windows: 停止 nest（通过 UAC 提权运行 schtasks /end，禁止直接操作进程）

    Args:
        name: 实例名称，None 则停止所有 nest 进程
    """
    display_name = f"nest({name})" if name else "所有 nest"

    # 收集需要停止的 schtasks 任务名
    task_names: list[str] = []
    if name:
        tn = _get_task_name(name)
        if _is_task_installed_by_name(tn):
            task_names.append(tn)
    else:
        for tn in _list_all_nest_task_names_windows():
            task_names.append(tn)

    if not task_names:
        # 没有任务计划，检查进程是否在运行
        if _is_nest_process_running(name):
            return False, "服务未正确安装，请先运行 nest install"
        return True, "未在运行"

    # 检查是否有任务正在运行
    running_tasks = [tn for tn in task_names if _is_task_currently_running_windows(tn)]

    if not running_tasks and not _is_nest_process_running(name):
        return True, "未在运行"

    # 直接通过 UAC 提权运行 schtasks /end 停止所有运行中的任务
    end_cmds = [f'schtasks /end /tn "{tn}"' for tn in task_names]
    cmd_str = " & ".join(end_cmds)
    ok, msg = _run_elevated(cmd_str, wait_for_running=False)
    if not ok:
        return False, "停止失败，请在弹窗中点击「是」"

    time.sleep(2)

    # 确认已停止
    if _is_nest_process_running(name):
        return False, "停止失败，请重试"

    return True, "已停止"


def _kill_nest_elevated(name: str | None = None) -> None:
    """通过 UAC 提权杀掉 nest 进程

    使用 wmic 查找进程 PID，然后通过 UAC 提权的 taskkill 杀掉。

    Args:
        name: 实例名称，None 则杀掉所有 nest 进程
    """
    import ctypes

    # 收集需要杀的 PID（wmic 不受 psutil AccessDenied 限制，可发现提权进程）
    pids_to_kill: list[int] = []
    if sys.platform == "win32":
        wmic_filter = "name='python.exe' or name='pythonw.exe'"
        try:
            result = subprocess.run(
                ["wmic", "process", "where", wmic_filter, "get", "ProcessId,CommandLine"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            for line in result.stdout.split("\n"):
                line_lower = line.lower()
                if "nest" not in line_lower or (
                    "connect" not in line_lower and "serve" not in line_lower
                ):
                    continue
                if name and f"--name {name}" not in line:
                    continue
                parts = line.strip().split()
                for part in reversed(parts):
                    try:
                        pid = int(part)
                        pids_to_kill.append(pid)
                        break
                    except ValueError:
                        continue
        except Exception:
            pass

    if not pids_to_kill:
        return

    # 通过 UAC 提权杀进程
    commands = " & ".join(f"taskkill /F /PID {pid}" for pid in pids_to_kill)
    log_dir = _get_project_root() / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = str(log_dir / "nest_uac_kill.log")

    ret = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        "cmd",
        f'/c {commands} >> "{log_file}" 2>&1',
        None,
        0,
    )
    if ret <= 32:
        _safe_print(f"[WARN] UAC 提权杀进程失败 (返回值: {ret})")
    else:
        _safe_print(f"[INFO] 已通过 UAC 提权发送 taskkill，等待进程终止...")
        time.sleep(3)


# ==================== Linux (systemd) ====================


def _sudo_cmd(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """执行 sudo 命令，自动询问密码"""
    return subprocess.run(["sudo"] + args, **kwargs)


def _stop_service_by_kill_linux(service_name: str) -> tuple[bool, str]:
    """通过 kill 命令停止 systemd 服务进程（替代 systemctl stop）

    先通过 systemctl show 获取主进程 PID，然后发送 SIGTERM 信号，
    等待进程退出，超时则发送 SIGKILL 强制终止。
    """
    import signal as _signal

    # 通过 systemctl show 获取服务主进程 PID
    try:
        r_pid = subprocess.run(
            ["systemctl", "show", "-p", "MainPID", service_name],
            capture_output=True,
            **_SUBPROC_RUN_KW,
            timeout=5,
        )
        # 输出格式: MainPID=12345
        pid_line = r_pid.stdout.strip().split("=")[-1]
        pid = int(pid_line)
    except (ValueError, IndexError):
        return False, "无法获取服务进程 PID"

    if pid <= 1:
        # PID=0 表示服务未运行
        return True, "服务未运行"

    # 先发送 SIGTERM 优雅退出
    try:
        os.kill(pid, _signal.SIGTERM)
    except ProcessLookupError:
        return True, "进程已不存在"
    except PermissionError:
        # 需要 sudo 来 kill 其他用户的进程
        try:
            _sudo_cmd(["kill", "-TERM", str(pid)], capture_output=True, **_SUBPROC_RUN_KW, timeout=5)
        except Exception as e:
            return False, f"发送 SIGTERM 失败: {e}"

    # 等待进程退出（最多 2.5 秒）
    for _ in range(5):
        time.sleep(0.5)
        try:
            os.kill(pid, 0)  # 检查进程是否存在
        except ProcessLookupError:
            return True, "服务已通过 kill 停止"
        except PermissionError:
            # 进程还存在但没有权限，说明进程还在运行
            pass

    # 超时，发送 SIGKILL 强制终止
    try:
        os.kill(pid, _signal.SIGKILL)
    except ProcessLookupError:
        return True, "进程已退出"
    except PermissionError:
        try:
            _sudo_cmd(["kill", "-KILL", str(pid)], capture_output=True, **_SUBPROC_RUN_KW, timeout=5)
        except Exception as e:
            return False, f"发送 SIGKILL 失败: {e}"

    time.sleep(1)
    return True, "服务已通过 kill (SIGKILL) 强制停止"


def _get_systemd_unit_path(name: str | None = None) -> Path:
    """获取系统级 systemd unit 文件路径"""
    service_name = _get_systemd_service_name(name)
    return Path(f"/etc/systemd/system/{service_name}.service")


def _is_service_installed_linux(name: str | None = None) -> bool:
    """Linux: 检查系统级服务是否已安装"""
    unit_path = _get_systemd_unit_path(name)
    if unit_path.exists():
        return True
    # 也通过 systemctl 检查
    service_name = _get_systemd_service_name(name)
    try:
        r = subprocess.run(
            ["systemctl", "is-enabled", service_name],
            capture_output=True,
            **_SUBPROC_RUN_KW,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _is_service_running_linux(name: str | None = None) -> bool:
    """Linux: 检查系统级服务是否正在运行"""
    service_name = _get_systemd_service_name(name)
    try:
        r = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True,
            **_SUBPROC_RUN_KW,
            timeout=5,
        )
        return r.stdout.strip() == "active"
    except Exception:
        return False


def _write_systemd_unit(
    name: str | None = None, host: str | None = None, port: int | None = None
) -> Path:
    """写入系统级 systemd unit 文件（使用 sudo 获取权限）"""
    import grp
    import pwd
    import tempfile

    start_cmd = _get_start_cmd(host, port)
    if name:
        start_cmd = _get_python_cmd() + [str(_get_nest_script()), "connect", "--name", name]
        if host:
            start_cmd.extend(["--host", host])
        if port:
            start_cmd.extend(["--port", str(port)])
    project_root = _get_project_root()
    service_name = _get_systemd_service_name(name)

    # 🔧 修复：以安装者用户运行而非 root
    # 当用户用 sudo 执行 nest install 时，SUDO_USER 是真实用户名
    current_user = os.environ.get("SUDO_USER") or os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name
    try:
        current_group = grp.getgrgid(pwd.getpwnam(current_user).pw_gid).gr_name
    except (KeyError, OSError):
        current_group = current_user

    unit_content = f"""[Unit]
Description={SERVICE_DISPLAY_NAME} ({name or "default"})
After=network.target

[Service]
Type=simple
User={current_user}
Group={current_group}
WorkingDirectory={project_root}
ExecStart={" ".join(start_cmd)}
KillSignal=SIGTERM
KillMode=process
Restart=on-failure
RestartSec=5
Environment=PATH=/usr/local/bin:/usr/bin:/bin
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""

    unit_path = _get_systemd_unit_path(name)

    # 通过临时文件 + sudo cp 写入系统级目录
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".service", delete=False, encoding="utf-8"
    ) as f:
        f.write(unit_content)
        temp_path = f.name

    try:
        result = _sudo_cmd(
            ["cp", temp_path, str(unit_path)],
            capture_output=True,
            **_SUBPROC_RUN_KW,
            timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError(f"写入 unit 文件失败: {result.stderr}")
    finally:
        try:
            os.unlink(temp_path)
        except Exception:
            pass

    # 重新加载 systemd
    _sudo_cmd(["systemctl", "daemon-reload"], capture_output=True, **_SUBPROC_RUN_KW, timeout=10)

    return unit_path


def install_service_linux(
    name: str | None = None, host: str | None = None, port: int | None = None
) -> tuple[bool, str]:
    """Linux: 安装 nest 系统级服务（使用 sudo 获取权限）"""
    service_name = _get_systemd_service_name(name)
    display_name = f"nest({name})" if name else "nest"

    if _is_service_installed_linux(name) and _is_service_running_linux(name):
        return True, f"{display_name} 服务已安装且正在运行"

    try:
        _write_systemd_unit(name, host, port)

        # 启用并启动
        _sudo_cmd(
            ["systemctl", "enable", service_name],
            capture_output=True,
            **_SUBPROC_RUN_KW,
            timeout=10,
        )
        r = _sudo_cmd(
            ["systemctl", "start", service_name],
            capture_output=True,
            **_SUBPROC_RUN_KW,
            timeout=15,
        )

        if r.returncode == 0:
            return True, f"{display_name} 服务安装并启动成功"
        return False, f"启动失败: {r.stderr}"
    except Exception as e:
        return False, f"安装失败: {e}"


def uninstall_service_linux(name: str | None = None) -> tuple[bool, str]:
    """Linux: 卸载 nest 系统级服务（使用 sudo 获取权限）"""
    service_name = _get_systemd_service_name(name)
    display_name = f"nest({name})" if name else "nest"

    if name:
        # 卸载指定实例
        try:
            # 使用 kill 方式停止服务
            success, msg = _stop_service_by_kill_linux(service_name)
            if not success:
                print(f"警告: 停止 {display_name} 失败: {msg}")
            _sudo_cmd(
                ["systemctl", "disable", service_name],
                capture_output=True,
                **_SUBPROC_RUN_KW,
                timeout=10,
            )

            unit_path = _get_systemd_unit_path(name)
            if unit_path.exists():
                _sudo_cmd(
                    ["rm", "-f", str(unit_path)],
                    capture_output=True,
                    **_SUBPROC_RUN_KW,
                    timeout=10,
                )

            _sudo_cmd(
                ["systemctl", "daemon-reload"], capture_output=True, **_SUBPROC_RUN_KW, timeout=10
            )

            return True, f"{display_name} 服务已卸载"
        except Exception as e:
            return False, f"卸载失败: {e}"
    else:
        # 卸载所有实例
        unit_dir = Path("/etc/systemd/system")
        results = []
        for unit_file in unit_dir.glob(f"{SERVICE_NAME}*.service"):
            sn = unit_file.stem
            try:
                # 使用 kill 方式停止服务
                success, msg = _stop_service_by_kill_linux(sn)
                if not success:
                    print(f"警告: 停止 {sn} 失败: {msg}")
                _sudo_cmd(
                    ["systemctl", "disable", sn],
                    capture_output=True,
                    **_SUBPROC_RUN_KW,
                    timeout=10,
                )
                _sudo_cmd(
                    ["rm", "-f", str(unit_file)],
                    capture_output=True,
                    **_SUBPROC_RUN_KW,
                    timeout=10,
                )
                results.append(sn)
            except Exception:
                pass
        if results:
            _sudo_cmd(
                ["systemctl", "daemon-reload"], capture_output=True, **_SUBPROC_RUN_KW, timeout=10
            )
            return True, f"已卸载 {len(results)} 个 nest 服务: {', '.join(results)}"
        return True, "没有找到已安装的 nest 服务"


def start_service_linux(name: str | None = None) -> tuple[bool, str]:
    """Linux: 启动 nest 系统级服务"""
    service_name = _get_systemd_service_name(name)

    if not _is_service_installed_linux(name):
        return False, "nest 服务未安装，请先运行 nest install"

    try:
        r = _sudo_cmd(
            ["systemctl", "start", service_name],
            capture_output=True,
            **_SUBPROC_RUN_KW,
            timeout=15,
        )
        if r.returncode == 0:
            return True, "nest 服务已启动"
        return False, f"启动失败: {r.stderr}"
    except Exception as e:
        return False, f"启动失败: {e}"


def stop_service_linux(name: str | None = None) -> tuple[bool, str]:
    """Linux: 停止 nest 系统级服务（使用 kill 方式）"""
    service_name = _get_systemd_service_name(name)

    return _stop_service_by_kill_linux(service_name)


# ==================== 统一入口 ====================


def _save_host_config(
    name: str | None = None, host: str | None = None, port: int | None = None
) -> None:
    """保存实例配置到 nest.json

    Args:
        name: 实例名称，None 则保存到第一个实例（向后兼容）
        host: 远程 peng 服务地址
        port: 远程 peng 服务端口
    """
    import json

    project_root = _get_project_root()
    config_path = project_root / "config" / "nest.json"

    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    else:
        config = {"instances": {}}

    instances = config.get("instances", {})

    if name:
        # 保存到指定实例
        if name not in instances:
            instances[name] = {
                "name": name,
                "host": "localhost",
                "port": 8899,
                "reconnect_interval": 5,
            }
        inst = instances[name]
    else:
        # 向后兼容：保存到第一个实例
        if not instances:
            instances["local"] = {
                "name": "本机peng服务",
                "host": "localhost",
                "port": 8899,
                "reconnect_interval": 5,
            }
        inst_key = next(iter(instances))
        inst = instances[inst_key]

    if host is not None:
        # 解析 host:port 格式
        if ":" in host:
            h, p = host.rsplit(":", 1)
            inst["host"] = h
            try:
                inst["port"] = int(p)
            except ValueError:
                inst["host"] = host
        else:
            inst["host"] = host
    if port is not None:
        inst["port"] = port

    config["instances"] = instances

    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info(
        f"已更新 nest.json 配置: name={name}, host={inst.get('host')}, port={inst.get('port')}"
    )


def _save_instance_config(name: str, host: str, port: int, daemon: bool = False, ssh: str | None = None) -> None:
    """保存或更新实例配置到 nest.json"""
    import json

    project_root = _get_project_root()
    config_path = project_root / "config" / "nest.json"

    config = _load_nest_config()

    instances = config.get("instances", {})
    
    # 兼容旧格式：instances为list时转换为dict
    if isinstance(instances, list):
        new_instances = {}
        for item in instances:
            if isinstance(item, dict) and "name" in item:
                new_instances[item["name"]] = item
        instances = new_instances
        config["instances"] = instances

    if name in instances:
        # 更新已有实例
        instances[name]["host"] = host
        instances[name]["port"] = port
        instances[name]["daemon"] = daemon
        instances[name]["ssh"] = ssh
    else:
        # 创建新实例
        instances[name] = {
            "name": name,
            "workdir": str(_get_project_root()),
            "host": host,
            "port": port,
            "reconnect_interval": 5,
            "daemon": daemon,
            "ssh": ssh,
            "daemon_disconnect_timeout": 120,  # 守护模式：连续断开2分钟触发修复
        }

    # 记录本机 nest 身份标识（供 bin/msg 等本机 CLI 工具识别当前 nest）
    config["self"] = name

    config["instances"] = instances
    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _remove_instance_config(name: str) -> bool:
    """从 nest.json 中移除指定实例配置

    如果该实例是默认 nest，同时清除 "default" 字段。

    Returns:
        是否成功移除
    """
    import json

    project_root = _get_project_root()
    config_path = project_root / "config" / "nest.json"

    if not config_path.exists():
        return False

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    instances = config.get("instances", {})
    if name not in instances:
        return False

    del instances[name]
    config["instances"] = instances

    # 如果被移除的实例是默认 nest，清除 default 字段
    if config.get("default") == name:
        del config["default"]

    # 如果被移除的实例是本机 nest 身份，清除 self 字段
    if config.get("self") == name:
        del config["self"]

    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return True


def _remove_all_instance_configs() -> bool:
    """从 nest.json 中移除所有实例配置和默认设置

    Returns:
        是否成功移除
    """
    import json

    project_root = _get_project_root()
    config_path = project_root / "config" / "nest.json"

    if not config_path.exists():
        return False

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    config["instances"] = {}
    if "default" in config:
        del config["default"]
    if "self" in config:
        del config["self"]

    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return True


def do_install(name: str | None = None, host: str | None = None, port: int | None = None, daemon: bool = False, ssh: str | None = None) -> int:
    """安装服务

    支持安装多个 nest 实例：
    - --host 参数为必填项
    - 如果未指定 --name，自动使用系统 hostname
    - 每个实例使用独立的任务计划/systemd 服务
    - -d --daemon: 启用服务守护模式（远端服务失效时自动修复）
    - -s --ssh: 远程控制SSH命令（守护模式必须配合使用）

    Args:
        name: 实例名称，None 则自动使用系统 hostname
        host: 远程 peng 服务地址（必填）
        port: 远程 peng 服务端口（覆盖配置）
        daemon: 是否启用服务守护模式
        ssh: 远程控制SSH命令
    """
    # host 为必填参数
    if not host:
        _safe_print("[FAIL] 必须指定 --host 参数（远程 peng 服务地址）", file=sys.stderr)
        return 1

    # 守护模式必须配合SSH命令
    if daemon and not ssh:
        _safe_print("[FAIL] 启用守护模式(-d)时必须指定 --ssh 参数（远程控制SSH命令）", file=sys.stderr)
        return 1

    # 解析 host 中的 port
    actual_host = host
    actual_port = port
    if host and ":" in host:
        h, p = host.rsplit(":", 1)
        try:
            actual_host = h
            actual_port = int(p)
        except ValueError:
            pass

    # 自动命名：如果未指定 name，使用系统 hostname
    if not name:
        name = _auto_name(actual_host)
        _safe_print(f"[INFO] 自动命名实例: {name}")

    # 保存 host/port/daemon/ssh 配置到 nest.json
    _save_instance_config(
        name,
        actual_host,
        actual_port or 8899,
        daemon=daemon,
        ssh=ssh,
    )

    if sys.platform == "win32":
        success, msg = install_service_windows(name=name, host=actual_host, port=actual_port)
    else:
        success, msg = install_service_linux(name=name, host=actual_host, port=actual_port)
    _safe_print(f"{'[OK]' if success else '[FAIL]'} {msg}")

    # 安装成功后自动设置为默认 nest
    if success:
        _set_default_nest(name)
        _safe_print(f"[OK] 默认 nest 已设置为: {name}")

        # Firefox GC优化（减少WebUI卡顿）
        try:
            from peng.webui.firefox_optimize import optimize_all_firefox_profiles
            count = optimize_all_firefox_profiles()
            if count > 0:
                _safe_print(f"[OK] 🦊 Firefox GC优化已应用（{count}个配置文件），重启Firefox生效")
        except Exception as e:
            _safe_print(f"[INFO] Firefox优化跳过: {e}")

    return 0 if success else 1


def _set_default_nest(name: str) -> None:
    """设置默认 nest 实例到 config/nest.json"""
    import json

    project_root = _get_project_root()
    config_path = project_root / "config" / "nest.json"

    config = _load_nest_config()
    instances = config.get("instances", {})

    if name not in instances:
        _safe_print(f"[WARN] 实例 '{name}' 不存在于配置中，仍设置默认", file=sys.stderr)

    config["default"] = name
    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def do_default(name: str) -> int:
    """设置默认 nest 实例

    Args:
        name: 要设置为默认的实例名称
    """
    config = _load_nest_config()
    instances = config.get("instances", {})

    if name not in instances:
        _safe_print(
            f"❌ 实例 '{name}' 不存在。可用实例: {list(instances.keys()) or '无'}", file=sys.stderr
        )
        return 1

    _set_default_nest(name)
    _safe_print(f"✅ 默认 nest 已设置为: {name}")
    return 0


def do_uninstall(name: str | None = None) -> int:
    """卸载服务

    Args:
        name: 实例名称，None 则卸载所有 nest 实例
    """
    if name:
        display_name = f"nest({name})"
    else:
        display_name = "所有 nest 实例"
    _safe_print(f"[INFO] 卸载 {display_name}...")

    if sys.platform == "win32":
        success, msg = uninstall_service_windows(name)
    else:
        success, msg = uninstall_service_linux(name)

    # 卸载成功后，从配置中移除实例
    if success:
        if name:
            _remove_instance_config(name)
        else:
            _remove_all_instance_configs()

    _safe_print(f"{'[OK]' if success else '[FAIL]'} {msg}")
    return 0 if success else 1


def do_start() -> int:
    """启动服务"""
    if sys.platform == "win32":
        config = _load_nest_config()
        instance_names = _valid_instance_names(config)
        if not instance_names:
            _safe_print("[FAIL] 请先运行 nest install 安装服务")
            return 1
        all_ok = True
        for name in instance_names:
            success, msg = start_service_windows(name)
            _safe_print(f"  {'[OK]' if success else '[FAIL]'} {name} - {msg}")
            if not success:
                all_ok = False
        return 0 if all_ok else 1
    else:
        success, msg = start_service_linux()
        _safe_print(f"{'[OK]' if success else '[FAIL]'} {msg}")
        return 0 if success else 1


def do_stop() -> int:
    """停止服务"""
    if sys.platform == "win32":
        config = _load_nest_config()
        instance_names = _valid_instance_names(config)
        if not instance_names:
            _safe_print("[FAIL] 没有配置任何实例")
            return 1
        all_ok = True
        for name in instance_names:
            success, msg = stop_service_windows(name)
            _safe_print(f"  {'[OK]' if success else '[FAIL]'} {name} - {msg}")
            if not success:
                all_ok = False
        return 0 if all_ok else 1
    else:
        success, msg = stop_service_linux()
        _safe_print(f"{'[OK]' if success else '[FAIL]'} {msg}")
        return 0 if success else 1


def do_restart() -> int:
    """重启 config/nest.json 中所有 nest 实例

    遍历配置中的每个实例，逐个停止再启动。
    Windows 下将所有需要 UAC 提权的操作合并到一次 UAC 弹窗中执行。
    """
    config = _load_nest_config()
    instance_names = _valid_instance_names(config)

    if not instance_names:
        _safe_print("[INFO] config/nest.json 中没有配置任何实例")
        return 0

    _safe_print(f"[INFO] 准备重启 {len(instance_names)} 个实例: {', '.join(instance_names)}")

    if sys.platform == "win32":
        return _do_restart_windows_all(instance_names)
    else:
        return _do_restart_linux_all(instance_names)


def _do_restart_windows_all(instance_names: list[str]) -> int:
    """Windows: 重启所有实例，一次 UAC 提权完成 stop + start"""
    MAX_RETRIES = 3

    # 收集所有 schtasks 命令：先 end 停止，再 run 启动
    cmds: list[str] = []
    for name in instance_names:
        task_name = _get_task_name(name)
        if _is_task_installed_by_name(task_name):
            cmds.append(f'schtasks /end /tn "{task_name}"')
        else:
            _safe_print(f"  [FAIL] {name} - 请先运行 nest install 安装服务")
            return 1

    # PID cleanup
    project_root = _get_project_root()
    for name in instance_names:
        pid_file = project_root / "data" / f"nest_{name}.pid"
        cmds.append(f'del /f "{pid_file}" 2>nul')

    for name in instance_names:
        task_name = _get_task_name(name)
        cmds.append(f'schtasks /run /tn "{task_name}"')

    _safe_print("[INFO] === 停止阶段 ===")
    cmd_str = " & ".join(cmds)
    ok, msg = _run_elevated(cmd_str, wait_for_running=False)
    if not ok:
        _safe_print(f"  [FAIL] 重启失败，请在弹窗中点击「是」")
        return 1

    _safe_print("[INFO] === 启动阶段 ===")

    # 通过 schtasks 任务状态检测确认启动（跨提权边界有效）
    # 启动阶段会失败重试最多 MAX_RETRIES 次
    all_ok = True
    for name in instance_names:
        task_name = _get_task_name(name)
        started = False
        for attempt in range(1, MAX_RETRIES + 1):
            if attempt > 1:
                _safe_print(f"  [INFO] nest({name}) 第 {attempt}/{MAX_RETRIES} 次重试启动...")
                # 重新执行 schtasks /run（无需再次 UAC，任务本身已是 HighestAvailable）
                # PID cleanup before retry
                try:
                    (project_root / "data" / f"nest_{name}.pid").unlink(missing_ok=True)
                except OSError:
                    pass
                retry_cmd = f'schtasks /run /tn "{task_name}"'
                subprocess.run(retry_cmd, shell=True, timeout=10,
                               capture_output=True, encoding="utf-8", errors="replace")
            for i in range(15):
                time.sleep(1)
                if _is_nest_process_running(name):
                    started = True
                    break
                if _is_task_currently_running_windows(task_name):
                    started = True
                    break
            if started:
                break
        if started:
            _safe_print(f"  [OK] nest({name}) 已启动")
        else:
            _safe_print(f"  [FAIL] nest({name}) 重试 {MAX_RETRIES} 次后仍未启动")
            all_ok = False

    return 0 if all_ok else 1


def _do_restart_linux_all(instance_names: list[str]) -> int:
    """Linux: 重启所有实例（systemctl，无需 UAC）"""
    config = _load_nest_config()
    instances = config.get("instances", {})

    # 第一阶段：停止所有实例
    for name in instance_names:
        _safe_print(f"[INFO] 停止 nest({name})...")
        success, msg = stop_service_linux(name)
        _safe_print(f"  {'[OK]' if success else '[WARN]'} {msg}")

    time.sleep(2)

    # 第二阶段：启动所有实例
    all_ok = True
    for name in instance_names:
        _safe_print(f"[INFO] 启动 nest({name})...")
        success, msg = start_service_linux(name)
        _safe_print(f"  {'[OK]' if success else '[FAIL]'} {msg}")
        if not success:
            all_ok = False

    return 0 if all_ok else 1


def do_status() -> int:
    """查看状态

    从服务端 API 获取实时 nest 连接信息，输出格式与 bin/fly status 的 nest 部分一致。
    标注哪个 nest 是自身（hostname 匹配本机的 nest）。
    支持在远端 nest 上运行：先尝试 localhost，失败后从 nest config 获取服务端地址重试。
    """
    import json as _json
    import socket as _socket
    import urllib.error
    import urllib.request

    _safe_print("")

    # 获取 webui 端口
    webui_port = 6688
    try:
        from peng.config.loader import load_config
        _cfg = load_config()
        if hasattr(_cfg, 'webui') and _cfg.webui:
            webui_port = _cfg.webui.port
    except Exception:
        pass

    # 从 nest config 获取服务端地址列表，config 为空时回退到 localhost
    api_data = None
    api_hosts = []
    try:
        nest_config = _load_nest_config()
        for inst in nest_config.get("instances", {}).values():
            h = inst.get("host", "")
            if h and h not in api_hosts:
                api_hosts.append(h)
    except Exception:
        pass
    if not api_hosts:
        api_hosts = ["localhost"]

    for api_host in api_hosts:
        try:
            url = f"http://{api_host}:{webui_port}/api/nest/list"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=3) as resp:
                api_data = _json.loads(resp.read())
            break
        except Exception:
            continue

    if api_data is None:
        # 所有地址均不可达，回退到进程检测
        local_nest_running = _is_nest_process_running(name="local")
        icon = "🟢" if local_nest_running else "🔴"
        state = "运行中" if local_nest_running else "已停止"
        _safe_print(f"  {icon} nest          : {state}")
        _safe_print("")
        return 0

    connections = {
        k: v for k, v in api_data.get("nests", {}).items() if v.get("connected")
    }
    if connections:
        _safe_print(f"  🟢 nest          : 运行中 ({len(connections)}个连接)")
        my_hostname = _socket.gethostname()
        my_platform = "Windows" if sys.platform == "win32" else "Linux"
        for key, info in connections.items():
            platform = info.get("platform", "?")
            hostname = info.get("hostname", "?")
            pid = info.get("pid", "?")
            workdir = info.get("workdir", "")
            workdir_str = f", workdir={workdir}" if workdir else ""
            # 标注自身 nest（hostname + platform 均匹配本机）
            is_self = hostname == my_hostname and platform == my_platform
            self_tag = " (自身)" if is_self else ""
            _safe_print(f"    🪹 {key} ({platform}/{hostname}, pid={pid}{workdir_str}){self_tag}")
    else:
        any_nest_running = _is_nest_process_running()
        if any_nest_running:
            _safe_print(f"  🟢 nest          : 运行中 (暂无已注册连接)")
        else:
            _safe_print(f"  🔴 nest          : 已停止")

    _safe_print("")
    return 0


def _load_nest_config() -> dict:
    """加载 config/nest.json 配置"""
    import json

    project_root = _get_project_root()
    config_path = project_root / "config" / "nest.json"

    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    # 默认配置
    return {"instances": {}}


def _valid_instance_names(config: dict) -> list[str]:
    """返回配置中合法的实例名列表（过滤空字符串等非法键）

    历史缺陷：webui set-workdir 在 nest 连接 name 为空时，远程
    _handle_set_workdir 会用 instances.setdefault("", {}) 写入空字符串实例键，
    导致 restart/start/stop 遍历到 "" 时 _get_task_name("") 回退为默认任务名，
    查不到任务而整体失败（事务#1665）。此处统一过滤非法键。
    """
    names = []
    for key in config.get("instances", {}):
        if isinstance(key, str) and key.strip():
            names.append(key)
        else:
            _safe_print(
                f"[WARN] 跳过非法实例键 {key!r}（疑似历史脏数据，请检查 config/nest.json）",
                file=sys.stderr,
            )
    return names


def _setup_nest_logging(name: str | None = None) -> None:
    """配置 nest connect 日志写文件

    pythonw.exe 无控制台，print/logging 输出会丢失。
    必须将日志写入文件才能排查问题。
    """
    from peng.common.paths import chown_to_project_owner

    log_dir = _get_project_root() / "logs"
    log_dir.mkdir(exist_ok=True)
    chown_to_project_owner(log_dir)
    log_name = name or "nest"
    log_file = log_dir / f"nest_{log_name}.log"

    # 🔧 修复：如果日志文件被 root 创建（之前以 root 运行的服务），当前用户无权写入
    # 尝试删除旧的 root 所有的日志文件（无法 chown，只能删除+重建）
    if log_file.exists() and not os.access(log_file, os.W_OK):
        try:
            # 尝试直接删除（如果目录有写权限，即使文件属于 root 也可能可以删除）
            log_file.unlink(missing_ok=True)
            logger.warning(f"已删除无写权限的旧日志文件: {log_file}")
        except PermissionError:
            # 无法删除，尝试用不同的文件名
            
            log_file = log_dir / f"nest_{log_name}_{int(time.time())}.log"
            logger.warning(f"无法删除旧日志文件，使用新文件: {log_file}")

    root_logger = logging.getLogger()
    # 避免重复添加 handler
    if any(
        isinstance(h, logging.FileHandler)
        and hasattr(h, "baseFilename")
        and log_name in getattr(h, "baseFilename", "")
        for h in root_logger.handlers
    ):
        return

    root_logger.setLevel(logging.INFO)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    chown_to_project_owner(log_file)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root_logger.addHandler(file_handler)

    logger.info(f"nest 日志系统已初始化: {log_file}")


def _ensure_single_instance(name: str, host: str, port: int) -> bool:
    """确保只有一个同参数的 nest 实例在运行（不同参数允许共存）

    🔧 事务#1598 改用 OS 级独占文件锁（原子、免竞态）：
    - 锁文件路径: data/nest_<name>_<hash8(host:port)>.lock（含参数指纹，
      不同 host/port 的实例互不干扰，可同时运行；同参数才互斥）
    - 用 fcntl.flock (POSIX) / msvcrt.locking (Windows) 对锁文件加排他锁
    - 锁随进程存活：进程退出/崩溃时由 OS 自动释放，无需清理
    - 两个实例同时启动时，只有一个能拿到锁，另一个立即退出（解决同时启动竞态）

    旧 PID 文件方案的缺陷（已废弃）：
    - 检查-写入非原子，同秒启动的双进程都通过检查（epyc 双进程事故根因）
    - pythonw.exe cmdline AccessDenied 时误判为无实例，导致重复启动
    - PID 复用误判

    Args:
        name: 实例名称
        host: 连接地址
        port: 连接端口

    Returns:
        True: 当前实例可以继续运行
        False: 已有同参数实例持有锁，当前实例应退出
    """
    global _single_instance_lock_fh

    # 参数指纹：同 name 不同 host/port 的实例允许共存（master#7644）
    import hashlib

    param_hash = hashlib.sha1(f"{host.lower()}:{port}".encode()).hexdigest()[:8]
    lock_file = (
        _get_project_root() / "data" / f"nest_{name or 'default'}_{param_hash}.lock"
    )

    from peng.common.paths import ensure_dir_with_owner
    ensure_dir_with_owner(lock_file.parent)

    # 尝试获取排他锁（非阻塞）
    try:
        fh = open(lock_file, "a+")
    except OSError as e:
        # 无法创建锁文件（权限等）——记录后放行，保持旧行为不阻断服务
        _safe_print(f"[Nest] 无法创建单实例锁文件（跳过检测继续启动）: {e}")
        return True

    try:
        if os.name == "nt":
            import msvcrt
            # MSVCRL lock: 整个文件 1 字节排他锁；已锁时立即抛 OSError
            while True:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    # 锁被占用
                    _fh_owner_pid = _read_lock_owner(fh)
                    _safe_print(
                        f"[Nest] 检测到同参数实例已在运行"
                        + (f" (PID: {_fh_owner_pid})" if _fh_owner_pid else "")
                        + "，当前实例退出"
                    )
                    fh.close()
                    return False
            # 拿到锁：写入 PID 供诊断（truncate 后写）
            try:
                fh.seek(0)
                fh.truncate()
                fh.write(f"{os.getpid()}\n")
                fh.flush()
            except OSError:
                pass
            # 注意：fh 必须保持打开直到进程退出，锁才有效。
            # 挂到模块级引用（函数开头已global声明）防止 GC 关闭
            _single_instance_lock_fh = fh
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                _fh_owner_pid = _read_lock_owner(fh)
                _safe_print(
                    f"[Nest] 检测到同参数实例已在运行"
                    + (f" (PID: {_fh_owner_pid})" if _fh_owner_pid else "")
                    + "，当前实例退出"
                )
                fh.close()
                return False
            try:
                fh.seek(0)
                fh.truncate()
                fh.write(f"{os.getpid()}\n")
                fh.flush()
            except OSError:
                pass
            _single_instance_lock_fh = fh
        # 兼容：清理旧版 PID 文件（避免误导诊断）
        try:
            pid_file = (
                _get_project_root() / "data" / f"nest_{name or 'default'}.pid"
            )
            if pid_file.exists():
                pid_file.unlink()
        except OSError:
            pass
    except Exception as e:
        # 任何意外异常都不阻断服务启动（保持旧行为）
        _safe_print(f"[Nest] 单实例锁异常（跳过检测继续启动）: {e}")
        try:
            fh.close()
        except Exception:
            pass
        return True

    return True


def _read_lock_owner(fh) -> str | None:
    """读取锁文件中记录的 PID（仅用于日志诊断，不影响判定）"""
    try:
        fh.flush()
        pos = fh.tell()
        fh.seek(0)
        content = fh.read(64).strip()
        fh.seek(pos)
        return content if content.isdigit() else None
    except Exception:
        return None


# 模块级引用：持有已加锁文件句柄，防止 GC 关闭导致锁失效
_single_instance_lock_fh = None


def do_connect(
    name: str | None = None,
    host: str | None = None,
    port: int | None = None,
    skip_single_instance_check: bool = False,
) -> int:
    """
    连接到 peng 服务（前台运行）

    不依赖 config/nest.json，直接通过命令行参数指定连接信息。

    Args:
        name: 实例名称（None 则自动使用系统 hostname）
        host: 远程 peng 服务地址（必填，host 或 host:port）
        port: 远程 peng 服务端口
        skip_single_instance_check: 跳过单实例检测（内置 local nest 使用）
    """
    from peng.nest.security import NestSecurity
    from peng.nest.server import NestConnector

    if not host:
        _safe_print("[ERROR] 必须指定 --host 参数（远程 peng 服务地址）")
        return 1

    # 自动命名：如果未指定 name，使用系统 hostname
    if not name:
        name = _auto_name()
        _safe_print(f"[INFO] 自动命名实例: {name}")

    # 配置日志写文件（pythonw.exe无控制台，必须写文件才能排查问题）
    _setup_nest_logging(name)

    # 解析 host 中可能包含的 port（如 --host localhost:8899）
    if ":" in host:
        h, p = host.rsplit(":", 1)
        try:
            parsed_port = int(p)
            host = h
            if port is None:
                port = parsed_port
        except ValueError:
            pass  # 不是有效端口号，可能是 IPv6 地址等，保持原样

    cfg_host = host
    cfg_port = port or 8899

    # 单实例检测：如果同参数实例已正常运行则退出，异常则取代之
    # 内置 local nest 通过 skip_single_instance_check 跳过此检测
    if not skip_single_instance_check:
        try:
            if not _ensure_single_instance(name, cfg_host, cfg_port):
                return 0
        except Exception as e:
            _safe_print(f"[Nest] 单实例检测失败（继续启动）: {e}")

    security = NestSecurity()

    # 🔧 从 config/nest.json 读取 reconnect_interval
    _cfg_reconnect_interval = 5
    try:
        _nest_cfg = _load_nest_config()
        _inst_cfg = _nest_cfg.get("instances", {}).get(name, {})
        _cfg_reconnect_interval = _inst_cfg.get("reconnect_interval", 5)
    except Exception:
        pass

    connector = NestConnector(
        name=name,
        host=cfg_host,
        port=cfg_port,
        reconnect_interval=_cfg_reconnect_interval,
        security=security,
    )
    
    # 🔧 守护模式：从 config/nest.json 读取 daemon/ssh 配置
    try:
        nest_cfg = _load_nest_config()
        inst_cfg = nest_cfg.get("instances", {}).get(name, {})
        if inst_cfg.get("daemon"):
            connector.daemon = True
            connector.ssh = inst_cfg.get("ssh")
            connector.daemon_disconnect_timeout = inst_cfg.get("daemon_disconnect_timeout", 120)
            _safe_print(f"[Nest] 守护模式已启用 (ssh={connector.ssh}, timeout={connector.daemon_disconnect_timeout}s)")
    except Exception as e:
        _safe_print(f"[Nest] 读取守护配置失败（继续）: {e}")

    try:
        asyncio.run(connector.start())
    except KeyboardInterrupt:
        _safe_print("\n[Nest] 收到 Ctrl+C，正在停止...")
    finally:
        connector.stop()

    _safe_print("[Nest] 进程退出")
    return 0


def do_serve(host: str = "localhost", port: int = 8899) -> int:
    """
    连接到 peng 服务（兼容旧命令，等同于 do_connect）
    """
    return do_connect()
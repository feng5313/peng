"""
Windows 服务管理模块（Task Scheduler）

使用 Windows Task Scheduler + VBS wrapper 管理服务（peng + PostgreSQL）。
VBS wrapper 实现隐藏窗口 + 崩溃自动重启。
通过 ShellExecuteW("runas") 弹出 UAC 获取管理员权限（仅 install/uninstall 需要）。

旧版 NSSM 函数保留作为 fallback，但 fly.py 已改用 Task Scheduler。

功能：
- fly install  : 创建 VBS + 注册计划任务 + 启动（需 UAC）
- fly uninstall: 停止 + 删除计划任务（需 UAC）
- fly start    : schtasks /run 启动（无需 UAC）
- fly stop     : schtasks /end 停止（无需 UAC）
- fly status   : 查询状态（无需 UAC）
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# 服务名称
SERVICE_NAME = "peng"

# 服务显示名称
SERVICE_DISPLAY_NAME = "Peng AI Assistant"

# 服务描述
SERVICE_DESCRIPTION = "鹏 - AI 编程助手后台服务"


def _get_project_root() -> Path:
    """获取项目根目录"""
    return Path(__file__).resolve().parent.parent.parent.parent


def _get_fly_cmd() -> list[str]:
    """获取 fly service 启动命令"""
    project_root = _get_project_root()
    fly_exe = project_root / ".venv" / "Scripts" / "fly.exe"
    if fly_exe.exists():
        return [str(fly_exe), "service"]

    # 回退到 python 方式
    python_exe = str(project_root / ".venv" / "Scripts" / "python.exe")
    if not Path(python_exe).exists():
        python_exe = sys.executable
    return [python_exe, str(project_root / "bin" / "fly"), "service"]


def _find_nssm() -> str | None:
    """查找 nssm 可执行文件路径
    
    查找策略：
    1. 从已安装的 peng 服务注册表读取 nssm 路径（最可靠，SYSTEM 账户下也有效）
    2. shutil.which 查找 PATH
    3. 常见安装路径
    """
    # 策略1：从 peng 服务注册表读取 nssm 路径（SYSTEM 账户下 USERPROFILE 不正确）
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}",
            0, winreg.KEY_READ,
        )
        try:
            image_path, _ = winreg.QueryValueEx(key, "ImagePath")
            if image_path and "nssm" in image_path.lower():
                nssm_path = str(image_path)
                if Path(nssm_path).exists():
                    return nssm_path
        finally:
            winreg.CloseKey(key)
    except Exception:
        pass

    # 策略2：PATH 查找
    nssm_path = shutil.which("nssm")
    if nssm_path:
        return nssm_path

    # 策略3：常见安装路径
    common_paths = [
        Path(os.environ.get("USERPROFILE", "")) / "scoop" / "shims" / "nssm.exe",
        Path(os.environ.get("USERPROFILE", "")) / "scoop" / "apps" / "nssm" / "current" / "nssm.exe",
        Path("C:\\ProgramData\\chocolatey\\bin\\nssm.exe"),
        Path("C:\\Tools\\nssm.exe"),
    ]
    # 当 USERPROFILE 是 SYSTEM 账户时，枚举实际用户目录
    for user_dir in Path("C:\\Users").iterdir():
        if user_dir.name in ("Public", "Default", "Default User", "All Users"):
            continue
        if user_dir.is_dir():
            common_paths.append(user_dir / "scoop" / "shims" / "nssm.exe")
            common_paths.append(user_dir / "scoop" / "apps" / "nssm" / "current" / "nssm.exe")
    for p in common_paths:
        if p.exists():
            return str(p)

    return None


def _run_nssm_elevated(cmd_str: str, wait_for_state: str | None = None) -> tuple[bool, str]:
    """
    通过 runas (UAC) 执行一组 nssm 命令

    使用临时 bat 文件来执行，避免 cmd /c 中重定向和引号嵌套问题。

    Args:
        cmd_str: 用 & 或 && 连接的命令字符串
        wait_for_state: 等待服务达到指定状态（"STOPPED" 或 "RUNNING"），None 表示不等待

    Returns:
        (成功标志, 消息)
    """
    # 生成临时 bat 文件
    log_dir = _get_project_root() / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = str(log_dir / "nssm_uac.log")
    bat_file = str(log_dir / "nssm_uac.bat")
    
    # 写入 bat 文件：所有命令的输出都重定向到日志
    with open(bat_file, "w", encoding="utf-8") as f:
        f.write("@echo off\n")
        f.write(f'echo [%date% %time%] UAC start >> "{log_file}" 2>&1\n')
        f.write(f'{cmd_str} >> "{log_file}" 2>&1\n')
        f.write(f'echo [%date% %time%] UAC end >> "{log_file}" 2>&1\n')
    
    ret = ctypes.windll.shell32.ShellExecuteW(
        None,           # hwnd
        "runas",        # 触发 UAC 弹窗
        "cmd",          # 用 cmd 执行
        f'/c "{bat_file}"',
        None,           # 工作目录
        0,              # SW_HIDE - 隐藏窗口
    )
    if ret <= 32:
        return False, f"UAC 提升失败 (返回值: {ret})，可能用户取消了弹窗"

    if wait_for_state:
        print(f"⏳ 等待服务状态变为 {wait_for_state}...")
        sys.stdout.flush()
        for i in range(60):  # 最多等60秒（PostgreSQL启动需~19秒+timeout 20秒+缓冲）
            time.sleep(1)
            try:
                result = subprocess.run(
                    ["sc", "query", SERVICE_NAME],
                    capture_output=True,
                    encoding="utf-8", errors="replace",
                    timeout=5,
                )
                if result.returncode == 0:
                    if wait_for_state in result.stdout.upper():
                        print(f"✅ 服务状态: {wait_for_state}")
                        sys.stdout.flush()
                        break
                elif result.returncode != 0 and i > 30:
                    # 服务可能已卸载或不存在，继续等待（UAC命令可能还在执行安装）
                    pass
            except Exception:
                pass
        else:
            print("⚠️ 等待超时，请用 fly status 检查")
            sys.stdout.flush()

    return True, "UAC 命令已执行"


def _cleanup_legacy_registry_autostart() -> None:
    """清理旧的注册表自启动项（HKCU\\...\\Run\\PengService）"""
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        try:
            winreg.DeleteValue(key, "PengService")
            logger.info("已清理旧的注册表自启动项 (PengService)")
        except FileNotFoundError:
            pass
        finally:
            winreg.CloseKey(key)
    except Exception as e:
        logger.warning(f"清理旧注册表自启动项失败（可忽略）: {e}")


def _is_service_installed() -> bool:
    """检查 peng 服务是否已安装（使用 sc query，不需要管理员权限）"""
    try:
        result = subprocess.run(
            ["sc", "query", SERVICE_NAME],
            capture_output=True,
            encoding="utf-8", errors="replace",
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def _is_service_running() -> bool:
    """检查 peng 服务是否正在运行"""
    try:
        result = subprocess.run(
            ["sc", "query", SERVICE_NAME],
            capture_output=True,
            encoding="utf-8", errors="replace",
            timeout=10,
        )
        if result.returncode == 0:
            return "RUNNING" in result.stdout.upper()
        return False
    except Exception:
        return False


# ========== 公共接口（被 fly.py 调用） ==========

def install_as_autostart() -> tuple[bool, str]:
    """
    使用 nssm 安装 peng 为 Windows 服务

    通过 runas 自动弹出 UAC，一次执行所有 nssm 命令。

    Returns:
        (成功与否, 消息)
    """
    if sys.platform != "win32":
        return False, "此功能仅支持 Windows 平台"

    nssm = _find_nssm()
    if not nssm:
        return False, (
            "未找到 nssm，请先安装:\n"
            "  scoop install nssm\n"
            "  或 choco install nssm\n"
            "  或从 https://nssm.cc 下载"
        )

    _cleanup_legacy_registry_autostart()

    cmd = _get_fly_cmd()
    project_root = _get_project_root()

    # 准备日志目录
    log_dir = project_root / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = str(log_dir / "nssm_service.log")

    # 构造所有 nssm 命令，用 && 串起来
    nssm_q = f'"{nssm}"'
    app_args = " ".join(f'"{c}"' if " " in c else c for c in cmd)

    commands = [
        f"echo 正在安装 peng 服务...",
    ]

    # 如果服务已存在，先移除
    if _is_service_installed():
        commands.append(f"{nssm_q} stop {SERVICE_NAME}")
        commands.append(f"{nssm_q} remove {SERVICE_NAME} confirm")

    # nssm install <name> <app> [<args> ...]
    commands.append(f'{nssm_q} install {SERVICE_NAME} {app_args}')
    commands.append(f'{nssm_q} set {SERVICE_NAME} DisplayName "{SERVICE_DISPLAY_NAME}"')
    commands.append(f'{nssm_q} set {SERVICE_NAME} Description "{SERVICE_DESCRIPTION}"')
    commands.append(f'{nssm_q} set {SERVICE_NAME} AppDirectory "{project_root}"')
    commands.append(f'{nssm_q} set {SERVICE_NAME} AppStdout "{log_file}"')
    commands.append(f'{nssm_q} set {SERVICE_NAME} AppStderr "{log_file}"')
    commands.append(f'{nssm_q} set {SERVICE_NAME} AppRotateFiles 1')
    commands.append(f'{nssm_q} set {SERVICE_NAME} AppRotateBytes 0')
    commands.append(f'{nssm_q} set {SERVICE_NAME} AppRotateOnline 1')
    commands.append(f'{nssm_q} set {SERVICE_NAME} Start SERVICE_AUTO_START')
    # AppStopMethodConsole: 控制台程序停止方式（Python 是控制台程序）
    # 默认 1500 = WM_QUIT + PostMessage(WM_CLOSE) + WM_QUIT（无 Ctrl+C，Python 不响应！）
    # 需要加上 0x0001 (Ctrl+C) 让 Python 收到 SIGINT 信号，触发优雅关闭
    # 0x0200 (TerminateProcess) 作为最后兜底
    # 最终值 = 0x1701 = 5889 (用十进制避免 cmd 中 0x 前缀被误解析)
    commands.append(f'{nssm_q} set {SERVICE_NAME} AppStopMethodConsole 5889')
    # AppThrottle: 发送停止信号后等待多久(ms)才强制 TerminateProcess。给 Python 5秒优雅退出
    commands.append(f'{nssm_q} set {SERVICE_NAME} AppThrottle 5000')
    commands.append(f'{nssm_q} start {SERVICE_NAME}')
    commands.append(f"echo peng 服务安装完成!")

    cmd_str = " & ".join(commands)

    print(f"\n  安装命令: nssm install {SERVICE_NAME} {app_args}")
    print(f"  工作目录: {project_root}")
    print(f"  日志文件: {log_file}\n")

    return _run_nssm_elevated(cmd_str)


def uninstall_autostart() -> tuple[bool, str]:
    """
    使用 nssm 卸载 peng Windows 服务

    Returns:
        (成功与否, 消息)
    """
    if sys.platform != "win32":
        return False, "此功能仅支持 Windows 平台"

    _cleanup_legacy_registry_autostart()

    nssm = _find_nssm()
    if not nssm:
        return True, "nssm 不可用，已清理旧的注册表自启动项（如有）"

    if not _is_service_installed():
        return True, "peng 服务未安装（无需卸载）"

    nssm_q = f'"{nssm}"'
    commands = [
        f"echo 正在卸载 peng 服务...",
        f"{nssm_q} stop {SERVICE_NAME}",
        f"{nssm_q} remove {SERVICE_NAME} confirm",
        f"echo peng 服务已卸载!",
    ]
    cmd_str = " & ".join(commands)

    return _run_nssm_elevated(cmd_str)


def _get_service_state() -> str | None:
    """
    获取 peng 服务的当前状态（使用 sc query，不需要管理员权限）

    Returns:
        服务状态字符串（如 "RUNNING", "STOPPED", "STOP_PENDING", "START_PENDING"），
        查询失败返回 None
    """
    try:
        result = subprocess.run(
            ["sc", "query", SERVICE_NAME],
            capture_output=True,
            encoding="utf-8", errors="replace",
            timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                if "STATE" in line.upper() and ":" in line:
                    # 格式: STATE : 4  RUNNING (或 STOP_PENDING 等)
                    state_part = line.split(":")[1].strip()
                    # 提取状态名称（去掉数字前缀）
                    parts = state_part.split()
                    if len(parts) >= 2:
                        return parts[1].upper()
                    elif parts:
                        return parts[0].upper()
        return None
    except Exception:
        return None


def _get_service_pid() -> int | None:
    """获取服务进程 PID（使用 sc queryex，不需要管理员权限）"""
    try:
        result = subprocess.run(
            ["sc", "queryex", SERVICE_NAME],
            capture_output=True,
            encoding="utf-8", errors="replace",
            timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                if "PID" in line.upper() and ":" in line:
                    pid_str = line.split(":")[1].strip().split()[0]
                    try:
                        pid = int(pid_str)
                        if pid > 0:
                            return pid
                    except ValueError:
                        pass
    except Exception as e:
        logger.warning(f"获取服务 PID 失败: {e}")
    return None


def _kill_pid(pid: int, kill_tree: bool = False) -> bool:
    """
    用 taskkill /F 杀进程，返回是否成功。
    
    ⚠️ 默认 kill_tree=False，不杀进程树！
    PostgreSQL 进程可能存在于进程树中，绝不能在 fly stop/start/restart 时被误杀。
    PostgreSQL 只能由 fly install/uninstall 管理。
    """
    try:
        args = ["taskkill", "/F", "/PID", str(pid)]
        if kill_tree:
            args.insert(1, "/T")
        result = subprocess.run(
            args,
            capture_output=True,
            encoding="utf-8", errors="replace",
            timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False


def _kill_pid_elevated(pid: int, kill_tree: bool = False) -> bool:
    """
    通过 UAC 提权用 taskkill /F 杀进程。
    
    ⚠️ 默认 kill_tree=False，不杀进程树！
    PostgreSQL 进程可能存在于进程树中，绝不能在 fly stop/start/restart 时被误杀。
    PostgreSQL 只能由 fly install/uninstall 管理。
    """
    cmd_args = "/F /T" if kill_tree else "/F"
    cmd_str = f'taskkill {cmd_args} /PID {pid}'
    log_dir = _get_project_root() / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = str(log_dir / "nssm_uac.log")

    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "cmd",
        f'/c {cmd_str} >> "{log_file}" 2>&1',
        None, 0,
    )
    if ret <= 32:
        logger.warning(f"UAC 提权失败 (返回值: {ret})")
        return False

    # 等待 UAC 命令执行完成
    time.sleep(3)
    return True


def stop_service() -> tuple[bool, str]:
    """
    停止 peng 服务

    策略（优先用 NSSM/sc 正式停止，避免自动重启）：
    1. 已停止 → 返回成功
    2. 尝试 nssm stop（NSSM 会正确清理子进程且不会自动重启）
    3. 尝试 sc stop（Windows 原生服务控制）
    4. 以上都失败 → taskkill /F /T 强制杀进程树
    5. 等待验证服务已 STOPPED
    
    注意：不能直接 taskkill 杀进程！NSSM 的 AppExit Default 配置为 Restart，
    直接杀进程会导致 NSSM 自动重启服务。

    Returns:
        (成功与否, 消息)
    """
    if sys.platform != "win32":
        return False, "此功能仅支持 Windows 平台"

    if not _is_service_installed():
        return True, "peng 服务未安装"

    state = _get_service_state()
    logger.info(f"当前服务状态: {state}")

    if state == "STOPPED":
        return True, "peng 服务已处于停止状态"

    # 方法1: 优先用 nssm stop（NSSM 会正确停止服务且不会自动重启）
    nssm = _find_nssm()
    if nssm:
        logger.info("尝试 nssm stop...")
        try:
            result = subprocess.run(
                [nssm, "stop", SERVICE_NAME],
                capture_output=True,
                encoding="utf-8", errors="replace",
                timeout=30,
            )
            if result.returncode == 0:
                # 等待服务状态变为 STOPPED
                for i in range(15):
                    time.sleep(1)
                    new_state = _get_service_state()
                    if new_state == "STOPPED" or new_state is None:
                        return True, "peng 服务已停止"
                return True, "peng 服务已停止（nssm stop）"
            logger.info(f"nssm stop 返回 {result.returncode}: {result.stderr}")
        except Exception as e:
            logger.info(f"nssm stop 异常: {e}")

    # 方法2: 尝试 sc stop（Windows 原生服务控制）
    logger.info("尝试 sc stop...")
    try:
        result = subprocess.run(
            ["sc", "stop", SERVICE_NAME],
            capture_output=True,
            encoding="utf-8", errors="replace",
            timeout=15,
        )
        if result.returncode == 0:
            # 等待服务状态变为 STOPPED
            for i in range(15):
                time.sleep(1)
                new_state = _get_service_state()
                if new_state == "STOPPED" or new_state is None:
                    return True, "peng 服务已停止"
            # sc stop 成功但服务还没 STOPPED，可能是进程卡住
            logger.info("sc stop 已发送但服务未停止，可能需要强制终止")
        else:
            logger.info(f"sc stop 返回 {result.returncode}: {result.stderr}")
    except Exception as e:
        logger.info(f"sc stop 异常: {e}")

    # 方法3: 强制杀进程（最后手段）
    pid = _get_service_pid()
    if not pid:
        # 检查是否已经停了
        if _get_service_state() == "STOPPED":
            return True, "peng 服务已停止"
        return True, "peng 服务未在运行（无法获取 PID）"

    logger.info(f"强制终止进程 {pid}（含子进程树）...")

    if _kill_pid(pid, kill_tree=True):
        logger.info(f"已终止进程 {pid} 及其子进程树")
    else:
        logger.info("taskkill 失败，尝试 UAC 提权...")
        if not _kill_pid_elevated(pid, kill_tree=True):
            return False, f"无法终止进程 {pid}（UAC 提权失败）"
        logger.info(f"已通过 UAC 终止进程 {pid} 及其子进程树")

    # 等待服务状态变为 STOPPED
    for i in range(10):
        time.sleep(1)
        new_state = _get_service_state()
        if new_state == "STOPPED":
            return True, "peng 服务已停止"
        if new_state is None:
            return True, "peng 服务已停止"

    return True, "进程已终止，服务状态更新中"


def restart_service() -> tuple[bool, str]:
    """
    重启 peng 服务

    Returns:
        (成功与否, 消息)
    """
    if sys.platform != "win32":
        return False, "此功能仅支持 Windows 平台"

    if not _is_service_installed():
        return False, "peng 服务未安装，请先运行 fly install"

    nssm = _find_nssm()
    if nssm:
        try:
            result = subprocess.run(
                [nssm, "restart", SERVICE_NAME],
                capture_output=True,
                encoding="utf-8", errors="replace",
                timeout=120,
            )
            if result.returncode == 0:
                return True, "peng 服务已重启"
            stderr = (result.stderr or "").strip()
            if "拒绝访问" in stderr or "Access is denied" in stderr:
                return False, (
                    "需要管理员权限才能重启服务。请用管理员终端运行:\n"
                    f"  nssm restart {SERVICE_NAME}"
                )
            return False, f"重启服务失败: {stderr or (result.stdout or "").strip()}"
        except subprocess.TimeoutExpired:
            return False, "重启服务超时"
        except Exception as e:
            return False, f"重启服务异常: {e}"

    return False, "nssm 不可用，无法重启服务"


def is_service_running() -> bool:
    """
    检查 peng 服务是否正在运行（使用 sc query，不需要管理员权限）
    """
    return _is_service_running()


# ==================== Task Scheduler 服务管理 ====================
# PG 18 拒绝以管理员/SYSTEM 身份运行，因此不能用 nssm（默认 SYSTEM 账户）。
# 改用 Windows Task Scheduler：以当前用户非提权身份运行，
# VBS wrapper 隐藏窗口 + 崩溃自动重启。
# peng 服务同样使用 Task Scheduler，避免 nssm 的 STOP_PENDING 卡死问题。

PENG_TASK_NAME = "peng_service"
PG_TASK_NAME = "peng_postgresql"
PG_SERVICE_NAME = "postgresql_peng"  # 保留兼容旧版检查


def _find_pg_ctl() -> str | None:
    """查找 pg_ctl.exe 路径"""
    from peng.db.postgres_auto_deploy import _find_pg_bin_on_windows

    pg_bin = _find_pg_bin_on_windows()
    if pg_bin:
        pg_ctl = pg_bin / "pg_ctl.exe"
        if pg_ctl.exists():
            return str(pg_ctl)
    return None


def _find_pg_data_dir() -> str | None:
    """查找 PostgreSQL 数据目录"""
    from peng.db.postgres_auto_deploy import _get_pg_data_dir_on_windows

    data_dir = _get_pg_data_dir_on_windows()
    if data_dir.exists():
        return str(data_dir)
    return None


# ---------- VBS Wrapper ----------

def _get_vbs_dir() -> Path:
    """VBS wrapper 脚本存放目录"""
    return Path.home() / "AppData" / "Local" / "peng"


def _create_service_vbs(exe_path: str, args: str, vbs_path: str) -> str:
    """创建 VBS wrapper（隐藏窗口 + 崩溃自动重启循环）并返回路径"""
    # 用 Chr(34) 生成双引号，避免 VBS 引号转义地狱
    # 生成的 Run 命令: objShell.Run Chr(34) & exe & Chr(34) & " -D " & Chr(34) & data & Chr(34), 0, True
    # 对于简单 args（不含引号）: objShell.Run Chr(34) & exe & Chr(34) & " args", 0, True
    if '"' in args:
        # 把 "path" 替换成 Chr(34) & "path" & Chr(34)
        safe_args = args.replace('"', '" & Chr(34) & "')
        cmd_line = f'Chr(34) & "{exe_path}" & Chr(34) & " {safe_args}"'
    else:
        cmd_line = f'Chr(34) & "{exe_path}" & Chr(34) & " {args}"'
    vbs_content = (
        'On Error Resume Next\n'
        'Do\n'
        '  Set objShell = CreateObject("WScript.Shell")\n'
        f'  objShell.Run {cmd_line}, 0, True\n'
        '  WScript.Sleep 5000\n'
        'Loop\n'
    )
    Path(vbs_path).parent.mkdir(parents=True, exist_ok=True)
    with open(vbs_path, "w", encoding="utf-8") as f:
        f.write(vbs_content)
    return vbs_path


def create_peng_vbs() -> str:
    """创建 peng 服务的 VBS wrapper（fly.exe service）"""
    fly_cmd = _get_fly_cmd()
    exe = fly_cmd[0]
    args = " ".join(fly_cmd[1:])
    vbs_path = str(_get_vbs_dir() / "start_peng_hidden.vbs")
    return _create_service_vbs(exe, args, vbs_path)


def create_pg_vbs(pg_data_dir: str) -> str:
    """创建 PostgreSQL 的 VBS wrapper"""
    from peng.db.postgres_auto_deploy import _find_pg_bin_on_windows
    pg_bin = _find_pg_bin_on_windows()
    if not pg_bin:
        raise FileNotFoundError("未找到 PostgreSQL bin 目录")
    postgres_exe = str(pg_bin / "postgres.exe")
    vbs_path = str(_get_vbs_dir() / "db" / "postgresql" / "start_pg_hidden.vbs")
    return _create_service_vbs(postgres_exe, f'-D "{pg_data_dir}"', vbs_path)


# ---------- Task Scheduler 查询 ----------

def _is_task_installed(task_name: str) -> bool:
    """检查计划任务是否已注册"""
    try:
        r = subprocess.run(
            ["schtasks", "/query", "/tn", task_name],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _is_peng_installed() -> bool:
    """检查 peng 计划任务是否已安装"""
    return _is_task_installed(PENG_TASK_NAME)


def _is_pg_installed() -> bool:
    """检查 PG 计划任务是否已安装"""
    return _is_task_installed(PG_TASK_NAME)


# 兼容旧版名称
_is_pg_task_installed = _is_pg_installed


def _is_pg_service_installed() -> bool:
    """检查 PG 是否已安装（计划任务或旧版 nssm 服务）"""
    if _is_pg_installed():
        return True
    # 兼容旧版 nssm 服务
    try:
        result = subprocess.run(
            ["sc", "query", PG_SERVICE_NAME],
            capture_output=True, encoding="utf-8", errors="replace", timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def _is_process_running(name: str) -> bool:
    """检查指定进程是否在运行"""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=5,
        )
        return name in r.stdout
    except Exception:
        return False


_is_pg_service_running = lambda: _is_process_running("postgres.exe")


# ---------- Task Scheduler 操作 ----------

def _run_task(task_name: str) -> bool:
    """运行计划任务（无需 UAC）"""
    try:
        r = subprocess.run(
            ["schtasks", "/run", "/tn", task_name],
            capture_output=True, timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False


def _end_task(task_name: str) -> bool:
    """结束计划任务（无需 UAC）"""
    try:
        r = subprocess.run(
            ["schtasks", "/end", "/tn", task_name],
            capture_output=True, timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False


# ---------- 公共接口（Task Scheduler 版本） ----------

def start_peng() -> tuple[bool, str]:
    """启动 peng 服务（通过计划任务，无需 UAC）"""
    if not _is_peng_installed():
        return False, "peng 服务未安装，请先运行 fly install"
    if _is_process_running("fly.exe"):
        return True, "peng 已在运行"
    if _run_task(PENG_TASK_NAME):
        return True, "peng 服务已启动"
    return False, "启动 peng 服务失败"


def _kill_service_processes_by_pid() -> int:
    """按 PID 精确终止 fly service 进程（fly.exe wrapper / python 服务进程）

    ⚠️ 绝不能用 `taskkill /F /IM fly.exe` 按映像名全杀：
    那会把正在执行的 fly stop / fly restart 自身的 fly.exe wrapper
    一并杀掉，导致命令中途自杀中断（restart 流程中断、停止信号文件
    残留、start 阶段永不执行）。

    只终止命令行匹配 fly service 的进程，并排除当前进程祖先链。

    Returns:
        终止的进程数
    """
    # 延迟导入避免模块级循环依赖
    from peng.ui.service import _get_all_service_pids

    killed = 0
    for pid in _get_all_service_pids():
        try:
            r = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
            if r.returncode == 0:
                killed += 1
                logger.info(f"已终止 fly service 进程 PID {pid}")
        except subprocess.SubprocessError as e:
            logger.warning(f"终止 fly service 进程 PID {pid} 失败: {e}")
    return killed


def stop_peng() -> tuple[bool, str]:
    """停止 peng 服务"""
    _end_task(PENG_TASK_NAME)
    # schtasks /end 会终止 wscript，但 fly.exe 服务进程可能残留，按 PID 精确清理
    # （不能按映像名 taskkill /IM fly.exe 全杀，会误杀 fly stop/restart 自身）
    killed = _kill_service_processes_by_pid()
    if killed:
        logger.info(f"已终止 {killed} 个残留 fly service 进程")
    return True, "peng 服务已停止"


def start_pg() -> tuple[bool, str]:
    """启动 PostgreSQL（通过计划任务，无需 UAC）"""
    if not _is_pg_installed():
        return False, "PostgreSQL 未安装，请先运行 fly install"
    if _is_process_running("postgres.exe"):
        return True, "PostgreSQL 已在运行"
    if _run_task(PG_TASK_NAME):
        return True, "PostgreSQL 已启动"
    return False, "启动 PostgreSQL 失败"


def stop_pg() -> tuple[bool, str]:
    """停止 PostgreSQL"""
    _end_task(PG_TASK_NAME)
    # schtasks /end 会终止该任务对应的 wscript，但 postgres.exe 可能还在运行
    subprocess.run(["taskkill", "/F", "/IM", "postgres.exe"], capture_output=True, timeout=10)
    return True, "PostgreSQL 已停止"


# 兼容旧版函数名
start_pg_service = start_pg
stop_pg_service = stop_pg


def _get_current_username() -> str:
    """获取当前 Windows 用户名"""
    return os.environ.get("USERNAME", os.environ.get("USER", ""))


def get_all_services_status() -> dict:
    """
    获取所有服务（peng + PostgreSQL）的状态

    Returns:
        dict with keys: peng_state, peng_installed, pg_state, pg_installed
    """
    peng_installed = _is_peng_installed()
    pg_installed = _is_pg_installed() or _is_pg_service_installed()
    peng_running = _is_process_running("fly.exe")
    pg_running = _is_process_running("postgres.exe")
    return {
        "peng_installed": peng_installed,
        "peng_state": "RUNNING" if peng_running else ("STOPPED" if peng_installed else None),
        "pg_installed": pg_installed,
        "pg_state": "RUNNING" if pg_running else ("STOPPED" if pg_installed else None),
    }

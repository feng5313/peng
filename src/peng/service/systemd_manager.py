"""
Systemd 服务管理模块

提供系统级 systemd 服务的安装、卸载、启动、停止功能。
使用 sudo 获取管理员权限，服务安装到 /etc/systemd/system/。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

# 服务名称
SERVICE_NAME = "peng"


def _sudo_cmd(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """执行 sudo 命令，自动询问密码"""
    return subprocess.run(["sudo"] + args, **kwargs)


def get_service_file_path() -> Path:
    """获取系统级 systemd service 文件路径"""
    return Path(f"/etc/systemd/system/{SERVICE_NAME}.service")


def get_project_root() -> Path:
    """获取项目根目录"""
    # 从脚本位置向上查找
    script_path = Path(__file__).resolve()
    # 向上追溯到 peng 目录的父目录
    current = script_path
    while current != current.parent:
        if (current / "src" / "peng").exists():
            return current
        current = current.parent
    # 默认返回 /mnt/wsl/work/peng
    return Path("/mnt/wsl/work/peng")


def generate_service_content() -> str:
    """生成systemd service文件内容（以安装者用户运行）"""
    import grp
    import pwd

    project_root = get_project_root()
    fly_path = project_root / "bin" / "fly"

    # 优先使用 venv python 执行 fly 脚本（避免系统 python 缺少 pydantic 等依赖）
    venv_python = project_root / ".venv" / "bin" / "python"
    if venv_python.exists():
        exec_start = f"{venv_python} {fly_path} service --force"
        venv_bin = str(project_root / ".venv" / "bin")
        path_env = f"{venv_bin}:/usr/bin:/bin:/usr/local/bin"
    else:
        exec_start = f"{fly_path} service --force"
        path_env = "/usr/bin:/bin:/usr/local/bin"

    # 🔧 修复：以安装者用户运行而非 root
    # 当用户用 sudo 执行 fly install 时，SUDO_USER 是真实用户名
    current_user = (
        os.environ.get("SUDO_USER") or os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name
    )
    try:
        current_group = grp.getgrgid(pwd.getpwnam(current_user).pw_gid).gr_name
    except (KeyError, OSError):
        current_group = current_user

    # 使用绝对路径
    service_content = f"""[Unit]
Description=Peng AI Assistant Service
After=network.target postgresql.service

[Service]
Type=simple
User={current_user}
Group={current_group}
WorkingDirectory={project_root}
ExecStart={exec_start}
# 注意：不要设置 ExecStop=bin/fly stop
# ExecStop 会在 systemctl restart 时被调用，而 bin/fly stop 内部又会调用
# systemctl stop，导致 "Job for peng.service canceled" 错误（循环调用）。
# 直接用 SIGKILL 停止进程，systemd 立即终止不等超时。
# Restart=always: 进程被杀/崩溃后自动重启，但 systemctl stop 不会重启（systemd 识别为主动停止）
# fly stop 内部调用 systemctl stop，所以 fly stop 也不会触发重启
Restart=always
RestartSec=5
# 不限制重启次数（去掉 RestartMaxAttempts）
StartLimitBurst=10
StartLimitIntervalSec=60
KillMode=mixed
KillSignal=SIGKILL
TimeoutStopSec=2

# 环境变量
Environment=PATH={path_env}
Environment=PYTHONIOENCODING=utf-8

# 标准输出和错误日志
StandardOutput=journal
StandardError=journal

# 资源限制
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
"""
    return service_content


def _restart_service_direct() -> Tuple[bool, str]:
    """
    直接进程管理方式重启服务（不使用 systemd）

    适用于 systemd 不可用的情况（如 WSL 环境中用户级 systemd 不可用）。

    Returns:
        (成功标志，消息)
    """
    import time
    from pathlib import Path

    logger.info("使用直接进程管理方式重启服务...")

    # 1. 停止现有服务（通过 kill_all_services）
    try:
        from peng.ui.service import kill_all_services

        killed = kill_all_services()
        if killed > 0:
            logger.info(f"✅ 已停止 {killed} 个服务进程")
            time.sleep(2)  # 等待进程完全退出
        else:
            logger.info("ℹ️  没有发现运行中的服务进程")
    except Exception as e:
        logger.warning(f"停止服务时出现异常: {e}")

    # 2. 启动新服务
    project_root = get_project_root()

    from peng.common.platform import IS_WINDOWS

    if IS_WINDOWS:
        # Windows: 优先用 fly.exe（uv sync 安装的 entry point）
        fly_exe = project_root / ".venv" / "Scripts" / "fly.exe"
        if fly_exe.exists():
            fly_path = str(fly_exe)
        else:
            # 回退到 python -m 方式
            import sys

            fly_path = str(sys.executable)
            cmd = [fly_path, "-m", "peng.ui.fly", "service"]
    else:
        fly_path = project_root / "bin" / "fly"
        cmd = [str(fly_path), "service"]

    if not IS_WINDOWS and not fly_path.exists():
        return False, f"❌ 启动脚本不存在: {fly_path}"

    if IS_WINDOWS and fly_path.endswith("fly.exe"):
        cmd = [fly_path, "service"]

    # 检查启动脚本是否存在（Linux）
    if not IS_WINDOWS and not Path(fly_path).exists():
        return False, f"❌ 启动脚本不存在: {fly_path}"

    try:
        popen_kwargs = {}
        if IS_WINDOWS:
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
            popen_kwargs["stdout"] = subprocess.DEVNULL
            popen_kwargs["stderr"] = subprocess.DEVNULL
        else:
            popen_kwargs["start_new_session"] = True
            popen_kwargs["stdout"] = subprocess.DEVNULL
            popen_kwargs["stderr"] = subprocess.DEVNULL

        process = subprocess.Popen(cmd, **popen_kwargs)

        # 等待进程启动
        time.sleep(3)

        # 检查进程是否还在运行
        if process.poll() is None:
            logger.info(f"✅ 服务已启动 (PID: {process.pid})")
            return True, f"✅ 服务已重启 (PID: {process.pid})"
        else:
            error_msg = f"❌ 服务启动失败，进程退出码: {process.returncode}"
            logger.error(error_msg)
            return False, error_msg

    except Exception as e:
        error_msg = f"❌ 启动服务异常: {e}"
        logger.error(error_msg)
        return False, error_msg


def is_systemd_available() -> bool:
    """检查系统级 systemd 是否可用"""
    try:
        result = subprocess.run(
            ["systemctl", "--version"], capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        logger.warning(f"systemd不可用：{e}")
        return False


def is_service_installed() -> bool:
    """检查系统级服务是否已安装"""
    service_file = get_service_file_path()
    if service_file.exists():
        return True
    # 也通过 systemctl 检查（可能文件被手动删除但 systemd 仍有缓存）
    try:
        result = subprocess.run(
            ["systemctl", "is-enabled", SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def reload_systemd_daemon() -> bool:
    """重新加载系统级 systemd 配置"""
    try:
        result = _sudo_cmd(
            ["systemctl", "daemon-reload"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.error(f"daemon-reload失败：{result.stderr}")
            return False
        return True
    except (subprocess.TimeoutExpired, Exception) as e:
        logger.error(f"daemon-reload异常：{e}")
        return False


def install_service() -> Tuple[bool, str]:
    """
    安装系统级 systemd 服务（使用 sudo 获取权限）

    Returns:
        (成功标志，消息)
    """
    # 检查systemd
    if not is_systemd_available():
        return False, "❌ systemd不可用，无法安装服务"

    # 检查是否已安装，如果已安装则先备份当前版本
    service_file = get_service_file_path()
    if service_file.exists():
        logger.info("📦 检测到已安装的service，正在备份当前版本...")
        try:
            from peng.service.version_manager import backup_current_version

            backup_ok, backup_msg, version_name = backup_current_version()
            if backup_ok and version_name:
                logger.info(f"✅ 当前版本已备份为：{version_name}")
            else:
                logger.warning(f"备份警告：{backup_msg}")
        except Exception as e:
            logger.warning(f"备份当前版本时出现异常：{e}")

    # 生成 service 文件内容
    service_content = generate_service_content()

    # 通过 sudo 写入系统级 service 文件
    try:
        # 使用临时文件 + sudo cp 的方式写入
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".service", delete=False, encoding="utf-8"
        ) as f:
            f.write(service_content)
            temp_path = f.name

        try:
            # sudo cp 临时文件到 /etc/systemd/system/
            result = _sudo_cmd(
                ["cp", temp_path, str(service_file)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                error_msg = f"❌ 写入service文件失败：{result.stderr}"
                logger.error(error_msg)
                return False, error_msg
            logger.info(f"✅ 已创建service文件：{service_file}")
        finally:
            # 清理临时文件
            try:
                os.unlink(temp_path)
            except Exception:
                pass
    except Exception as e:
        error_msg = f"❌ 创建service文件失败：{e}"
        logger.error(error_msg)
        return False, error_msg

    # 重新加载systemd配置
    if not reload_systemd_daemon():
        return False, "❌ 重新加载systemd配置失败"

    # 启用服务
    try:
        result = _sudo_cmd(
            ["systemctl", "enable", SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.warning(f"enable警告：{result.stderr}")
    except Exception as e:
        logger.warning(f"enable异常：{e}")

    # 启动服务
    try:
        result = _sudo_cmd(
            ["systemctl", "start", SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.warning(f"start警告：{result.stderr}")
            # 启动失败不视为安装失败
    except Exception as e:
        logger.warning(f"start异常：{e}")

    # LSP依赖项安装由 fly service 后台自动完成（_lsp_installation_loop），不在此处执行

    return (
        True,
        f"✅ 服务已安装：{service_file}\n请运行 'systemctl status {SERVICE_NAME}' 查看状态",
    )


def uninstall_service() -> Tuple[bool, str]:
    """
    卸载系统级 systemd 服务（使用 sudo 获取权限）

    Returns:
        (成功标志，消息)
    """
    if not is_service_installed():
        return False, "⚠️  服务未安装"

    # 停止服务
    stop_result = stop_service()
    if not stop_result[0]:
        logger.warning(f"停止服务警告：{stop_result[1]}")

    # 禁用服务
    try:
        result = _sudo_cmd(
            ["systemctl", "disable", SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.warning(f"disable警告：{result.stderr}")
    except Exception as e:
        logger.warning(f"disable异常：{e}")

    # 删除service文件
    service_file = get_service_file_path()
    try:
        if service_file.exists():
            result = _sudo_cmd(
                ["rm", "-f", str(service_file)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                error_msg = f"❌ 删除service文件失败：{result.stderr}"
                logger.error(error_msg)
                return False, error_msg
            logger.info(f"✅ 已删除service文件：{service_file}")
    except Exception as e:
        error_msg = f"❌ 删除service文件失败：{e}"
        logger.error(error_msg)
        return False, error_msg

    # 重新加载systemd配置
    reload_systemd_daemon()

    return True, "✅ 服务已卸载"


def ensure_user_systemd_running() -> Tuple[bool, str]:
    """
    确保系统级 systemd 可用

    Returns:
        (成功标志，消息)
    """
    if is_systemd_available():
        return True, "✅ systemd 可用"
    return False, "❌ systemd 不可用"


def restart_service() -> Tuple[bool, str]:
    """
    重启系统级 systemd 服务

    Returns:
        (成功标志，消息)
    """
    # 确保 systemd 可用
    if not is_systemd_available():
        # 如果 systemd 不可用，尝试直接进程管理
        logger.warning("systemd 不可用，尝试直接进程管理方式")
        return _restart_service_direct()

    # 检查服务是否已安装
    if not is_service_installed():
        return False, "⚠️  服务未安装，请先运行 'bin/fly install'"

    try:
        result = _sudo_cmd(
            ["systemctl", "restart", SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            error_msg = f"❌ 重启失败：{result.stderr}"
            logger.error(error_msg)
            return False, error_msg

        return True, f"✅ 服务已重启"
    except subprocess.TimeoutExpired:
        error_msg = "❌ 重启超时"
        logger.error(error_msg)
        return False, error_msg
    except Exception as e:
        error_msg = f"❌ 重启异常：{e}"
        logger.error(error_msg)
        return False, error_msg


def stop_service() -> Tuple[bool, str]:
    """
    停止系统级 systemd 服务

    服务配置了 KillSignal=SIGKILL + TimeoutStopSec=2，
    systemctl stop 会直接发 SIGKILL 立即终止，最多等 2 秒。

    Returns:
        (成功标志，消息)
    """
    if not is_service_installed():
        return False, "⚠️  服务未安装"

    try:
        result = _sudo_cmd(
            ["systemctl", "stop", SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True, "✅ 服务已停止"
        stderr_lower = result.stderr.lower()
        if "not loaded" in stderr_lower or "inactive" in stderr_lower:
            return True, "ℹ️  服务已停止"
        return False, f"❌ 停止失败：{result.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return False, "❌ 停止超时"
    except Exception as e:
        return False, f"❌ 停止异常：{e}"


def start_service() -> Tuple[bool, str]:
    """
    启动系统级 systemd 服务

    Returns:
        (成功标志，消息)
    """
    if not is_service_installed():
        return False, "⚠️  服务未安装，请先运行 'bin/fly install'"

    try:
        result = _sudo_cmd(
            ["systemctl", "start", SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            error_msg = f"❌ 启动失败：{result.stderr}"
            logger.error(error_msg)
            return False, error_msg

        return True, "✅ 服务已启动"
    except subprocess.TimeoutExpired:
        error_msg = "❌ 启动超时"
        logger.error(error_msg)
        return False, error_msg
    except Exception as e:
        error_msg = f"❌ 启动异常：{e}"
        logger.error(error_msg)
        return False, error_msg


def get_service_status() -> Tuple[bool, str]:
    """
    获取系统级服务状态

    Returns:
        (成功标志，状态信息)
    """
    if not is_service_installed():
        return False, "⚠️  服务未安装"

    try:
        result = subprocess.run(
            ["systemctl", "status", SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return True, result.stdout
    except subprocess.TimeoutExpired:
        return False, "❌ 查询状态超时"
    except Exception as e:
        return False, f"❌ 查询状态异常：{e}"


def check_service_active() -> bool:
    """检查系统级服务是否正在运行"""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() == "active"
    except Exception:
        return False

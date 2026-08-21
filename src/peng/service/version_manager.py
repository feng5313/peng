"""
版本管理模块

提供鹏服务的版本管理、备份和回退功能。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List

logger = logging.getLogger(__name__)

# 默认服务名称
DEFAULT_SERVICE_NAME = "peng"


def get_versions_dir(service_name: str = DEFAULT_SERVICE_NAME) -> Path:
    """获取版本管理目录"""
    return Path.home() / ".config" / service_name / "versions"


# 版本管理目录（向后兼容）
VERSIONS_DIR = get_versions_dir(DEFAULT_SERVICE_NAME)
CURRENT_LINK = VERSIONS_DIR / "current"
ROLLBACK_INFO_FILE = VERSIONS_DIR / "rollback.json"


def get_version_dir(version: str, service_name: str = DEFAULT_SERVICE_NAME) -> Path:
    """
    获取指定版本的目录

    Args:
        version: 版本名称
        service_name: 服务名称（默认"peng"）
    """
    versions_dir = get_versions_dir(service_name)
    return versions_dir / "versions" / version


def generate_version_name() -> str:
    """生成版本名称（基于时间戳）"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def get_service_file_path(service_name: str = DEFAULT_SERVICE_NAME) -> Path:
    """
    获取系统级 systemd service 文件路径

    Args:
        service_name: 服务名称（默认"peng"）
    """
    return Path(f"/etc/systemd/system/{service_name}.service")


def ensure_versions_dir(service_name: str = DEFAULT_SERVICE_NAME) -> None:
    """
    确保版本目录存在

    Args:
        service_name: 服务名称（默认"peng"）
    """
    versions_dir = get_versions_dir(service_name)
    versions_dir.mkdir(parents=True, exist_ok=True)
    (versions_dir / "versions").mkdir(parents=True, exist_ok=True)


def save_version_info(version_dir: Path, service_file_path: Path, status: str = "active") -> None:
    """保存版本信息"""
    version_info = {
        "version": version_dir.name,
        "timestamp": datetime.now().isoformat(),
        "service_file": str(service_file_path),
        "status": status
    }

    version_info_file = version_dir / "version.json"
    with open(version_info_file, 'w', encoding='utf-8') as f:
        json.dump(version_info, f, indent=2, ensure_ascii=False)


def load_version_info(version_dir: Path) -> Optional[dict]:
    """加载版本信息"""
    version_info_file = version_dir / "version.json"
    if not version_info_file.exists():
        return None

    try:
        with open(version_info_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"加载版本信息失败：{e}")
        return None


def backup_current_version(service_name: str = DEFAULT_SERVICE_NAME) -> Tuple[bool, str, Optional[str]]:
    """
    备份当前版本

    Args:
        service_name: 服务名称（默认"peng"）

    Returns:
        (成功标志，消息，版本名称)
    """
    try:
        # 确保版本目录存在
        ensure_versions_dir(service_name)

        # 获取当前service文件
        service_file = get_service_file_path(service_name)
        if not service_file.exists():
            logger.warning("当前未安装service，无需备份")
            return True, "ℹ️  当前未安装service，无需备份", None

        # 生成新版本名称
        version_name = generate_version_name()
        version_dir = get_version_dir(version_name, service_name)

        # 创建版本目录
        version_dir.mkdir(parents=True, exist_ok=True)

        # 备份service文件
        backup_service_file = version_dir / f"{service_name}.service"
        shutil.copy2(service_file, backup_service_file)
        logger.info(f"✅ 已备份service文件到：{backup_service_file}")

        # 保存版本信息
        save_version_info(version_dir, service_file, status="backup")

        # 更新current符号链接
        versions_dir = get_versions_dir(service_name)
        current_link = versions_dir / "current"
        if current_link.exists():
            current_link.unlink()
        current_link.symlink_to(version_dir.relative_to(versions_dir))
        logger.info(f"✅ current指向：{version_dir}")

        return True, f"✅ 已备份当前版本：{version_name}", version_name

    except Exception as e:
        error_msg = f"❌ 备份版本失败：{e}"
        logger.error(error_msg, exc_info=True)
        return False, error_msg, None


def get_available_versions(service_name: str = DEFAULT_SERVICE_NAME) -> List[Tuple[str, dict]]:
    """
    获取所有可用版本列表（版本名, 版本信息）

    Args:
        service_name: 服务名称（默认"peng"）
    """
    versions_dir = get_versions_dir(service_name) / "versions"
    if not versions_dir.exists():
        return []

    versions = []
    for version_dir in sorted(versions_dir.iterdir(), reverse=True):
        if version_dir.is_dir():
            version_info = load_version_info(version_dir)
            if version_info:
                versions.append((version_dir.name, version_info))

    return versions


def rollback_to_version(version: str, service_name: str = DEFAULT_SERVICE_NAME) -> Tuple[bool, str]:
    """
    回退到指定版本

    Args:
        version: 版本名称

    Returns:
        (成功标志，消息)
    """
    try:
        # 检查版本是否存在
        version_dir = get_version_dir(version, service_name)
        if not version_dir.exists():
            return False, f"❌ 版本 {version} 不存在"

        # 加载版本信息
        version_info = load_version_info(version_dir)
        if not version_info:
            return False, f"❌ 无法加载版本 {version} 的信息"

        # 检查备份的service文件是否存在
        backup_service_file = version_dir / f"{service_name}.service"
        if not backup_service_file.exists():
            return False, f"❌ 版本 {version} 的备份文件不存在"

        # 停止当前服务
        try:
            subprocess.run(
                ["sudo", "systemctl", "stop", service_name],
                capture_output=True,
                timeout=10
            )
            logger.info("✅ 已停止当前服务")
        except Exception as e:
            logger.warning(f"停止服务警告：{e}")

        # 恢复service文件（通过 sudo cp 写入系统级目录）
        service_file = get_service_file_path(service_name)
        result = subprocess.run(
            ["sudo", "cp", str(backup_service_file), str(service_file)],
            capture_output=True, timeout=10,
        )
        if result.returncode != 0:
            return False, f"❌ 恢复service文件失败: {result.stderr.decode() if result.stderr else 'unknown error'}"
        logger.info(f"✅ 已恢复service文件：{service_file}")

        # 更新current符号链接
        versions_dir = get_versions_dir(service_name)
        current_link = versions_dir / "current"
        if current_link.exists():
            current_link.unlink()
        current_link.symlink_to(version_dir.relative_to(versions_dir))

        # 保存回退信息
        rollback_info_file = versions_dir / "rollback.json"
        rollback_info = {
            "rollback_from": "current",
            "rollback_to": version,
            "timestamp": datetime.now().isoformat()
        }
        with open(rollback_info_file, 'w', encoding='utf-8') as f:
            json.dump(rollback_info, f, indent=2, ensure_ascii=False)

        # 重新加载systemd配置
        try:
            subprocess.run(
                ["sudo", "systemctl", "daemon-reload"],
                capture_output=True,
                timeout=10
            )
            logger.info("✅ 已重新加载systemd配置")
        except Exception as e:
            logger.warning(f"daemon-reload警告：{e}")

        # 启动服务
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "start", service_name],
                capture_output=True,
                timeout=10
            )
            if result.returncode != 0:
                logger.warning(f"启动服务警告：{result.stderr}")
        except Exception as e:
            logger.warning(f"启动服务异常：{e}")

        # 更新版本信息状态
        save_version_info(version_dir, service_file, status="active")

        return True, f"✅ 已回退到版本：{version}\n服务已重启，请运行 'systemctl status peng' 查看状态"

    except Exception as e:
        error_msg = f"❌ 回退失败：{e}"
        logger.error(error_msg, exc_info=True)
        return False, error_msg


def get_current_version(service_name: str = DEFAULT_SERVICE_NAME) -> Optional[str]:
    """
    获取当前版本名称
    
    Args:
        service_name: 服务名称（默认"peng"）
    """
    versions_dir = get_versions_dir(service_name)
    current_link = versions_dir / "current"
    
    if not current_link.exists():
        return None

    try:
        target = current_link.resolve()
        if target.parent.name == "versions":
            return target.name
        return None
    except Exception as e:
        logger.error(f"获取当前版本失败：{e}")
        return None


def check_service_health(service_name: str = DEFAULT_SERVICE_NAME) -> Tuple[bool, str]:
    """
    检查服务健康状态

    Returns:
        (健康标志，消息)
    """
    try:
        # 检查服务是否active
        result = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode != 0:
            return False, f"服务未运行：{result.stdout.strip()}"

        # 检查服务是否有错误日志
        result = subprocess.run(
            ["journalctl", "-u", service_name, "-n", "20", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=10
        )

        logs = result.stdout

        # 检查是否有错误
        if "Error" in logs or "Exception" in logs or "Traceback" in logs:
            # 提取最近的错误信息
            error_lines = [line for line in logs.split('\n') if 'Error' in line or 'Exception' in line]
            if error_lines:
                return False, f"服务日志中发现错误：{error_lines[-1][:200]}"

        return True, "✅ 服务健康"

    except subprocess.TimeoutExpired:
        return False, "健康检查超时"
    except Exception as e:
        return False, f"健康检查失败：{e}"


def auto_rollback_on_failure(service_name: str = DEFAULT_SERVICE_NAME) -> Tuple[bool, str]:
    """
    检查服务健康状态，失败时自动回退

    Returns:
        (成功标志，消息)
    """
    # 检查服务健康
    health_ok, health_msg = check_service_health(service_name)
    if health_ok:
        return True, "✅ 服务健康，无需回退"

    logger.warning(f"⚠️  服务不健康：{health_msg}，准备自动回退")

    # 获取可用版本
    versions = get_available_versions(service_name)
    if len(versions) < 2:
        return False, "⚠️  没有可回退的版本"

    # 跳过当前版本，回退到上一个版本
    current_version = get_current_version(service_name)
    previous_version = None

    for version_name, version_info in versions:
        if version_name != current_version:
            previous_version = version_name
            break

    if not previous_version:
        return False, "⚠️  没有找到可回退的上一个版本"

    logger.info(f"🔄 准备回退到版本：{previous_version}")

    # 执行回退
    success, msg = rollback_to_version(previous_version, service_name)
    if success:
        return True, f"✅ 服务不健康，已自动回退到版本 {previous_version}\n{msg}"
    else:
        return False, f"❌ 自动回退失败：{msg}"

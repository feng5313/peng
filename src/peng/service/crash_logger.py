"""
崩溃日志记录模块

记录服务崩溃时的详细信息，包括错误堆栈、系统状态、日志片段等，
帮助避免重复犯同样的错误。
"""

from __future__ import annotations

import json
import logging
import subprocess
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# 默认服务名称
DEFAULT_SERVICE_NAME = "peng"


def get_crash_logs_dir(service_name: str = DEFAULT_SERVICE_NAME) -> Path:
    """
    获取崩溃日志目录

    Args:
        service_name: 服务名称（默认"peng"）
    """
    return Path.home() / ".config" / service_name / "versions" / "crash_logs"


def ensure_crash_logs_dir(service_name: str = DEFAULT_SERVICE_NAME) -> None:
    """
    确保崩溃日志目录存在

    Args:
        service_name: 服务名称（默认"peng"）
    """
    crash_logs_dir = get_crash_logs_dir(service_name)
    crash_logs_dir.mkdir(parents=True, exist_ok=True)


def get_crash_log_file(version: str, service_name: str = DEFAULT_SERVICE_NAME) -> Path:
    """
    获取指定版本的崩溃日志文件路径

    Args:
        version: 版本号
        service_name: 服务名称（默认"peng"）
    """
    crash_logs_dir = get_crash_logs_dir(service_name)
    return crash_logs_dir / f"{version}_crash.json"


def capture_system_state(port: int = 6688, service_name: str = DEFAULT_SERVICE_NAME) -> Dict[str, Any]:
    """
    捕获系统状态

    Args:
        port: 服务端口（默认6688）
        service_name: 服务名称（默认"peng"）
    """
    state = {
        "timestamp": datetime.now().isoformat(),
        "processes": [],
        "port_status": {},
        "disk_usage": {},
        "memory_info": {}
    }

    try:
        # 获取服务相关进程
        result = subprocess.run(
            ["pgrep", "-a", "-f", service_name],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            state["processes"] = result.stdout.strip().split('\n')
    except Exception as e:
        state["processes"] = [f"获取进程信息失败：{e}"]

    try:
        # 检查端口状态
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(('127.0.0.1', port))
        sock.close()
        state["port_status"][str(port)] = "listening" if result == 0 else "not_listening"
    except Exception as e:
        state["port_status"][str(port)] = f"检查失败：{e}"

    try:
        # 检查磁盘使用情况
        result = subprocess.run(
            ["df", "-h", str(Path.home())],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split('\n')
            if len(lines) >= 2:
                parts = lines[1].split()
                state["disk_usage"] = {
                    "total": parts[1] if len(parts) > 1 else "unknown",
                    "used": parts[2] if len(parts) > 2 else "unknown",
                    "available": parts[3] if len(parts) > 3 else "unknown",
                    "percent": parts[4] if len(parts) > 4 else "unknown"
                }
    except Exception as e:
        state["disk_usage"] = {"error": str(e)}

    return state


def capture_journal_logs(service_name: str = DEFAULT_SERVICE_NAME, lines: int = 100) -> str:
    """
    捕获systemd journal日志

    Args:
        service_name: 服务名称（默认"peng"）
        lines: 捕获的行数

    Returns:
        日志内容
    """
    try:
        result = subprocess.run(
            ["journalctl", "--user", "-u", service_name, "-n", str(lines), "--no-pager"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            return result.stdout
        else:
            return f"获取日志失败：{result.stderr}"
    except subprocess.TimeoutExpired:
        return "获取日志超时"
    except Exception as e:
        return f"获取日志异常：{e}"


def extract_error_from_logs(logs: str) -> Dict[str, Any]:
    """
    从日志中提取错误信息

    Args:
        logs: 日志内容

    Returns:
        错误信息字典
    """
    errors = {
        "error_count": 0,
        "recent_errors": [],
        "tracebacks": []
    }

    log_lines = logs.split('\n')

    # 查找错误行
    error_lines = []
    for i, line in enumerate(log_lines):
        if any(keyword in line for keyword in ['ERROR', 'Error', 'Exception', 'Traceback']):
            error_lines.append((i, line))

    errors["error_count"] = len(error_lines)

    # 提取最近10个错误
    for i, line in error_lines[-10:]:
        errors["recent_errors"].append({
            "line_number": i,
            "content": line[:500]  # 限制长度
        })

    # 提取堆栈信息
    in_traceback = False
    traceback_lines = []
    traceback_start = 0

    for i, line in enumerate(log_lines):
        if 'Traceback' in line:
            in_traceback = True
            traceback_start = i
            traceback_lines = [line]
        elif in_traceback:
            traceback_lines.append(line)
            # 堆栈通常以错误行结束（行首没有时间戳）
            if line.strip() and not any(c.isdigit() for c in line[:5]):
                errors["tracebacks"].append({
                    "start_line": traceback_start,
                    "end_line": i,
                    "content": '\n'.join(traceback_lines[-50:])  # 限制堆栈长度
                })
                in_traceback = False
                traceback_lines = []

    return errors


def record_crash(
    version: str,
    crash_type: str,
    error_message: str = "",
    exception: Optional[Exception] = None,
    port: int = 6688,
    service_name: str = DEFAULT_SERVICE_NAME,
    additional_info: Optional[Dict[str, Any]] = None
) -> Tuple[bool, str]:
    """
    记录崩溃信息

    Args:
        version: 崩溃的版本
        crash_type: 崩溃类型（startup_failure/runtime_crash）
        error_message: 错误消息
        exception: 异常对象
        port: 服务端口（默认6688）
        service_name: 服务名称（默认"peng"）
        additional_info: 额外信息

    Returns:
        (成功标志，消息)
    """
    try:
        ensure_crash_logs_dir(service_name)

        # 收集崩溃信息
        crash_info = {
            "version": version,
            "crash_type": crash_type,
            "timestamp": datetime.now().isoformat(),
            "error_message": error_message,
            "exception": str(exception) if exception else None,
            "exception_type": type(exception).__name__ if exception else None
        }

        # 捕获异常堆栈
        if exception:
            crash_info["traceback"] = traceback.format_exc()

        # 捕获系统状态
        crash_info["system_state"] = capture_system_state(port, service_name)

        # 捕获应用日志
        try:
            app_log_path = Path.cwd() / "logs" / f"{service_name}.log"
            if app_log_path.exists():
                result = subprocess.run(
                    ["tail", "-n", "200", str(app_log_path)],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0:
                    crash_info["app_log_tail"] = result.stdout
        except Exception as e:
            crash_info["app_log_error"] = f"获取应用日志失败：{e}"

        # 捕获systemd日志
        journal_logs = capture_journal_logs(service_name, 100)
        crash_info["journal_logs"] = journal_logs
        crash_info["journal_errors"] = extract_error_from_logs(journal_logs)

        # 添加额外信息
        if additional_info:
            crash_info["additional_info"] = additional_info

        # 保存崩溃日志
        crash_log_file = get_crash_log_file(version, service_name)
        with open(crash_log_file, 'w', encoding='utf-8') as f:
            json.dump(crash_info, f, indent=2, ensure_ascii=False)

        logger.info(f"✅ 已记录崩溃日志：{crash_log_file}")
        return True, f"✅ 已记录崩溃日志到：{crash_log_file}"

    except Exception as e:
        error_msg = f"❌ 记录崩溃日志失败：{e}"
        logger.error(error_msg, exc_info=True)
        return False, error_msg


def get_crash_logs_list(service_name: str = DEFAULT_SERVICE_NAME) -> list:
    """
    获取所有崩溃日志列表

    Args:
        service_name: 服务名称（默认"peng"）
    """
    try:
        ensure_crash_logs_dir(service_name)
        crash_logs_dir = get_crash_logs_dir(service_name)
        crash_logs = []

        for crash_file in sorted(crash_logs_dir.glob("*_crash.json"), reverse=True):
            try:
                with open(crash_file, 'r', encoding='utf-8') as f:
                    crash_data = json.load(f)
                crash_logs.append({
                    "file": crash_file.name,
                    "version": crash_data.get("version", "unknown"),
                    "crash_type": crash_data.get("crash_type", "unknown"),
                    "timestamp": crash_data.get("timestamp", "unknown"),
                    "error_message": crash_data.get("error_message", "")[:100]
                })
            except Exception as e:
                logger.error(f"读取崩溃日志失败 {crash_file}：{e}")

        return crash_logs

    except Exception as e:
        logger.error(f"获取崩溃日志列表失败：{e}")
        return []


def get_crash_log_detail(version: str, service_name: str = DEFAULT_SERVICE_NAME) -> Optional[Dict[str, Any]]:
    """
    获取指定版本的崩溃日志详情

    Args:
        version: 版本号
        service_name: 服务名称（默认"peng"）
    """
    try:
        crash_log_file = get_crash_log_file(version, service_name)
        if not crash_log_file.exists():
            return None

        with open(crash_log_file, 'r', encoding='utf-8') as f:
            return json.load(f)

    except Exception as e:
        logger.error(f"读取崩溃日志详情失败：{e}")
        return None


def print_crash_summary(crash_log: Dict[str, Any]) -> str:
    """
    打印崩溃日志摘要

    Returns:
        摘要文本
    """
    lines = []
    lines.append("=" * 80)
    lines.append("📋 崩溃日志摘要")
    lines.append("=" * 80)
    lines.append(f"版本：{crash_log.get('version', 'unknown')}")
    lines.append(f"崩溃类型：{crash_log.get('crash_type', 'unknown')}")
    lines.append(f"时间：{crash_log.get('timestamp', 'unknown')}")
    lines.append(f"异常类型：{crash_log.get('exception_type', 'unknown')}")
    lines.append("")
    lines.append("错误消息：")
    lines.append(crash_log.get('error_message', '无')[:500])
    lines.append("")

    # 显示最近的错误
    journal_errors = crash_log.get('journal_errors', {})
    recent_errors = journal_errors.get('recent_errors', [])
    if recent_errors:
        lines.append(f"最近错误（共{journal_errors.get('error_count', 0)}个）：")
        for err in recent_errors[-5:]:  # 只显示最近5个
            lines.append(f"  - {err['content'][:200]}")
        lines.append("")

    # 系统状态
    system_state = crash_log.get('system_state', {})
    lines.append("系统状态：")
    port_status = system_state.get('port_status', {})
    if port_status:
        port_num = list(port_status.keys())[0] if port_status else "unknown"
        lines.append(f"  - 端口{port_num}：{port_status.get(port_num, 'unknown')}")
    lines.append(f"  - 进程数：{len(system_state.get('processes', []))}")
    lines.append("")

    lines.append("=" * 80)
    lines.append("提示：使用 'fly crash-info <version>' 查看完整信息（包括堆栈、日志等）")
    lines.append("=" * 80)

    return '\n'.join(lines)


def clear_crash_logs(older_than_days: int = 30, service_name: str = DEFAULT_SERVICE_NAME) -> Tuple[int, str]:
    """
    清理旧的崩溃日志

    Args:
        older_than_days: 清理多少天前的日志
        service_name: 服务名称（默认"peng"）

    Returns:
        (清理数量，消息)
    """
    try:
        import os
        from datetime import datetime, timedelta

        ensure_crash_logs_dir(service_name)
        crash_logs_dir = get_crash_logs_dir(service_name)
        cutoff_time = datetime.now() - timedelta(days=older_than_days)

        removed_count = 0
        for crash_file in crash_logs_dir.glob("*_crash.json"):
            try:
                # 从文件名解析版本时间
                version = crash_file.stem.replace('_crash', '')
                file_time = datetime.strptime(version, "%Y%m%d_%H%M%S")

                if file_time < cutoff_time:
                    os.remove(crash_file)
                    removed_count += 1
                    logger.info(f"已清理旧崩溃日志：{crash_file}")
            except Exception as e:
                logger.warning(f"清理崩溃日志失败 {crash_file}：{e}")

        msg = f"✅ 已清理 {removed_count} 个超过 {older_than_days} 天的崩溃日志"
        return removed_count, msg

    except Exception as e:
        return 0, f"❌ 清理崩溃日志失败：{e}"

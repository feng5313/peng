"""
Nest 工具辅助函数

提供所有工具共用的 nest 客户端访问方法和本地 fallback 辅助。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_nest_client():
    """
    获取 nest 客户端
    
    Returns:
        _NestClientCompat 实例
    """
    try:
        from peng.nest.client import get_nest_client
        return get_nest_client()
    except Exception:
        return None


def is_local_nest(context: dict[str, Any]) -> bool:
    """检查当前事务关联的 nest 是否为 local"""
    nest_name = context.get("nest") or "local"
    return nest_name == "local"


# 平台信息缓存（TTL 30秒）：避免工具定义每次生成时（name/description/parameters）
# 重复查询 webui API。nest 平台在事务生命周期内基本不变，30秒缓存足够安全。
_platform_cache: dict[str, tuple[float, str]] = {}
_PLATFORM_CACHE_TTL = 30.0


def get_nest_platform(nest_name: str) -> str:
    """获取指定 nest 的平台信息（Windows/Linux/macOS 等）

    两级查询（重要：事务子进程场景 NestBridgeManager 无连接）：
    1. NestBridgeManager（服务主进程场景，内存中有连接信息，快速路径）
    2. webui HTTP API /api/nest/list（事务子进程场景，NestBridgeManager 为空，
       通过本地 webui API 查询可靠获取平台信息）

    结果带 30 秒 TTL 缓存，避免工具定义生成时重复查询。

    Args:
        nest_name: nest 的 display_key 或原始名称

    Returns:
        平台字符串（如 "Windows"、"Linux"、"Darwin"），获取失败返回 ""
    """
    if not nest_name:
        return ""

    # 缓存命中（TTL 内）
    import time as _time

    now = _time.time()
    cached = _platform_cache.get(nest_name)
    if cached and now - cached[0] < _PLATFORM_CACHE_TTL:
        return cached[1]

    platform = ""

    # 第一级：NestBridgeManager 快速路径（服务主进程）
    try:
        from peng.nest.bridge import NestBridgeManager

        manager = NestBridgeManager.get_instance()
        if manager._connections:
            conn = manager.get_connection(nest_name)
            if conn and conn.info:
                platform = conn.info.get("platform", "")
    except Exception as e:
        logger.debug(f"[nest_helper] NestBridgeManager 获取平台失败: {e}")

    # 第二级：webui HTTP API（事务子进程场景，最可靠）
    if not platform:
        try:
            from peng.config.loader import load_config

            config = load_config()
            port = config.webui.port if hasattr(config, "webui") and config.webui else 6688

            import httpx

            resp = httpx.get(
                f"http://localhost:{port}/api/nest/list",
                timeout=5.0,
            )
            data = resp.json()
            nests = data.get("nests", {})

            # 精确匹配 display_key
            if nest_name in nests:
                platform = nests[nest_name].get("platform", "")

            # 模糊匹配 original_name
            if not platform:
                for key, info in nests.items():
                    if info.get("original_name") == nest_name:
                        platform = info.get("platform", "")
                        break
        except Exception as e:
            logger.debug(f"[nest_helper] webui API 获取平台失败: {e}")

    # 写入缓存（无论成功失败都缓存，避免反复查询失败）
    _platform_cache[nest_name] = (now, platform)
    return platform


def notify_fallback(tool_name: str, operation: str, context: dict[str, Any]) -> None:
    """当工具回退到本地执行时，通过 output_manager 写入提示信息
    
    Args:
        tool_name: 工具名称（如 "read", "write", "edit"）
        operation: 具体操作描述（如 "读取文件 /path/to/file"）
        context: 工具上下文（需包含 task_id）
    """
    task_id = context.get("task_id")
    if not task_id:
        return
    
    try:
        from peng.task.output_manager import get_output_manager
        msg = f"⚠️ [fallback] {tool_name} 工具因 nest 不可用，本地执行: {operation}"
        get_output_manager().write_output(task_id, "system", msg + "\n")
    except Exception:
        pass

"""
Nest管理和连接池监控路由模块

提供 Nest 列表/选择/工作目录设置、连接池状态监控等功能。

路由前缀: /api (由 server.py 注册)
- /nest/list, /nest/select, /nest/set-workdir
- /connection-pool/status, /connection-pool/alerts, /connection-pool/cleanup-idle, /connection-pool/cleanup-blocking
"""

from __future__ import annotations

import logging
import traceback
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from peng.webui.routes._common import _get_selected_nest_conn

logger = logging.getLogger(__name__)
router = APIRouter()


# ==================== 连接池监控 API ====================


@router.get("/connection-pool/status")
async def get_connection_pool_status():
    """获取连接池状态"""
    try:
        from peng.db.connection_monitor import get_monitor_status, get_pool_status
        pool_status = get_pool_status()
        monitor_status = get_monitor_status()
        if pool_status:
            return {"success": True, "pool_status": pool_status.to_dict(), "monitor_status": monitor_status}
        else:
            return {"success": False, "message": "连接池未初始化", "monitor_status": monitor_status}
    except Exception as e:
        logger.error(f"获取连接池状态失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取连接池状态失败: {e}")


@router.get("/connection-pool/alerts")
async def get_connection_pool_alerts(
    level: str | None = Query(None, description="告警级别过滤"),
    since: str | None = Query(None, description="起始时间（ISO格式）"),
    limit: int | None = Query(None, description="最大数量"),
):
    """获取连接池告警列表"""
    try:
        from peng.db.connection_monitor import get_alerts
        alerts = get_alerts(level=level, since=since, limit=limit)
        return {"success": True, "alerts": alerts, "count": len(alerts)}
    except Exception as e:
        logger.error(f"获取连接池告警失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取连接池告警失败: {e}")


@router.delete("/connection-pool/alerts")
async def clear_connection_pool_alerts(
    before: str | None = Query(None, description="清除此时间之前的告警"),
):
    """清除连接池告警"""
    try:
        from peng.db.connection_monitor import clear_alerts
        cleared = clear_alerts(before=before)
        return {"success": True, "cleared": cleared}
    except Exception as e:
        logger.error(f"清除连接池告警失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"清除连接池告警失败: {e}")


@router.post("/connection-pool/cleanup-idle")
async def cleanup_idle_connections(
    idle_threshold: int = Query(300, description="空闲时间阈值（秒）"),
    force: bool = Query(False, description="是否强制清理"),
):
    """清理空闲连接"""
    try:
        from peng.db.connection_monitor import cleanup_idle_connections
        cleaned = cleanup_idle_connections(idle_threshold=idle_threshold, force=force)
        return {"success": True, "cleaned": cleaned}
    except Exception as e:
        logger.error(f"清理空闲连接失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"清理空闲连接失败: {e}")


@router.post("/connection-pool/cleanup-blocking")
async def cleanup_blocking_connections():
    """清理阻塞连接"""
    try:
        from peng.db.connection_monitor import cleanup_idle_in_transaction_connections
        cleaned = cleanup_idle_in_transaction_connections()
        return {"success": True, "cleaned": cleaned}
    except Exception as e:
        logger.error(f"清理阻塞连接失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"清理阻塞连接失败: {e}")


# ==================== Nest API ====================


@router.get("/nest/list")
async def get_nest_list() -> dict[str, Any]:
    """获取可用的 nest 列表"""
    import time

    try:
        from peng.config.loader import load_config
        config = load_config()
        nest_config = config.nest
        nests = {}

        try:
            from peng.nest.bridge import NestBridgeManager
            manager = NestBridgeManager.get_instance()
            for conn_info in manager.list_connections():
                key = conn_info["display_key"]
                nests[key] = {
                    "name": conn_info.get("display_name", conn_info["name"]),
                    "host": conn_info.get("hostname", ""),
                    "port": 8899,
                    "connected": True,
                    "connected_at": conn_info["connected_at"],
                    "hostname": conn_info.get("hostname", ""),
                    "platform": conn_info.get("platform", ""),
                    "linux_id": conn_info.get("linux_id", ""),
                    "linux_name": conn_info.get("linux_name", ""),
                    "pid": conn_info.get("pid"),
                    "workdir": conn_info.get("workdir", ""),
                    "original_name": conn_info["name"],
                }
        except Exception:
            pass

        for name, ep in nest_config.nests.items():
            has_active = any(k == name or v.get("original_name") == name for k, v in nests.items())
            if not has_active and name not in nests:
                nests[name] = {"name": ep.name, "host": ep.host, "port": ep.port, "connected": False}

        from peng.config.loader import load_config as _lc
        _webui_config = _lc()
        default_nest = getattr(_webui_config.webui, 'nest', '') or nest_config.default_nest
        if default_nest not in nests:
            for key, info in nests.items():
                if info.get("connected"):
                    default_nest = key
                    break

        return {"default_nest": default_nest, "nests": nests, "enabled": nest_config.enabled}
    except Exception as e:
        return {"default_nest": "local", "nests": {"local": {"name": "本地", "host": "localhost", "port": 8899, "connected": False}}, "enabled": True}


@router.post("/nest/select")
async def select_nest(request: dict[str, Any]) -> dict[str, Any]:
    """切换当前使用的 nest"""
    try:
        nest_key = request.get("nest", "local")

        from peng.nest.client import set_default_nest
        set_default_nest(nest_key)

        from peng.webui.routes.workdir_config import _save_nest_to_config
        _save_nest_to_config(nest_key)

        return {"success": True, "default_nest": nest_key}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/nest/set-workdir")
async def nest_set_workdir(request: dict[str, Any]) -> dict[str, Any]:
    """设置远程nest的workdir"""
    try:
        nest_key = request.get("nest", "")
        workdir = request.get("workdir", "")

        if not nest_key or nest_key == "local":
            return {"success": False, "error": "本地nest不需要远程同步"}
        if not workdir:
            return {"success": False, "error": "workdir不能为空"}

        nest_conn = _get_selected_nest_conn(nest_key)
        if not nest_conn:
            return {"success": False, "error": f"nest连接未找到: {nest_key}"}

        from peng.nest.protocol import NestRequest
        original_name = getattr(nest_conn, 'name', nest_key)

        set_request = NestRequest(
            type="set_workdir",
            params={"workdir": workdir, "nest_name": original_name},
        )
        response = await nest_conn.send_request(set_request, timeout=10.0)

        if response.error:
            return {"success": False, "error": f"远程保存失败: {response.error}"}

        if hasattr(nest_conn, 'info') and isinstance(nest_conn.info, dict):
            nest_conn.info["workdir"] = workdir

        logger.info(f"远程nest workdir已同步: {nest_key} -> {workdir}")
        return {"success": True, "workdir": workdir}
    except Exception as e:
        logger.error(f"设置远程nest workdir失败: {e}\n{traceback.format_exc()}")
        return {"success": False, "error": str(e)}


@router.post("/nest/exec")
async def exec_on_nest(nest: str, command: str, timeout: int = 60):
    """在指定nest上执行bash命令（非流式，等待命令完成返回结果）"""
    try:
        from peng.nest.bridge import NestBridgeManager
        from peng.nest.protocol import NestRequest

        mgr = NestBridgeManager.get_instance()
        conn = mgr.get_connection(nest)
        if not conn:
            return {"success": False, "error": f"nest '{nest}' 未连接"}

        request = NestRequest(
            type="bash",
            params={"command": command, "timeout": timeout * 1000, "_skip_security": True},
        )
        response = await conn.send_request(request, timeout=timeout + 5.0)

        if response.error:
            return {"success": False, "error": response.error}

        data = response.data or {}
        return {
            "success": True,
            "exit_code": data.get("exit_code"),
            "stdout": data.get("stdout", ""),
            "stderr": data.get("stderr", ""),
        }
    except Exception as e:
        logger.error(f"nest exec失败: {e}\n{traceback.format_exc()}")
        return {"success": False, "error": str(e)}


@router.post("/nest/exec/stream")
async def exec_on_nest_stream(nest: str, command: str, timeout: int = 300):
    """在指定nest上执行bash命令（流式，通过SSE实时返回输出）
    
    使用 Server-Sent Events (SSE) 格式实时推送 stdout/stderr 输出行。
    适用于 fly watch 等长时间运行的命令。
    
    SSE 事件格式：
        event: stdout
        data: <一行输出>
        
        event: stderr
        data: <一行输出>
        
        event: done
        data: {"exit_code": 0}
        
        event: error
        data: <错误信息>
    """
    import asyncio
    import json as _json

    async def _stream_generator():
        try:
            from peng.nest.bridge import NestBridgeManager
            from peng.nest.protocol import NestRequest

            mgr = NestBridgeManager.get_instance()
            conn = mgr.get_connection(nest)
            if not conn:
                err_msg = f"nest '{nest}' 未连接"
                yield f"event: error\ndata: {_json.dumps({'error': err_msg})}\n\n"
                return

            request = NestRequest(
                type="bash",
                params={
                    "command": command,
                    "timeout": timeout * 1000,
                    "_skip_security": True,
                    "stream": True,  # 启用流式模式
                },
            )

            async for resp in conn.send_request_streaming(request, timeout=timeout + 5.0):
                if resp.type == "stream" and resp.data:
                    source = resp.data.get("source", "stdout")
                    data = resp.data.get("data", "")
                    if source == "exit":
                        # 进程结束
                        exit_code = int(data) if data else -1
                        yield f"event: done\ndata: {_json.dumps({'exit_code': exit_code})}\n\n"
                    else:
                        # stdout/stderr 输出行
                        yield f"event: {source}\ndata: {_json.dumps(data)}\n\n"
                elif resp.type == "error":
                    yield f"event: error\ndata: {_json.dumps({'error': resp.error or '未知错误'})}\n\n"
                    return
        except Exception as e:
            logger.error(f"nest exec/stream失败: {e}\n{traceback.format_exc()}")
            yield f"event: error\ndata: {_json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        _stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx 不缓冲
        },
    )

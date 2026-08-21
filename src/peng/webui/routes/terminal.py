"""
终端管理路由模块

提供终端会话创建/列表/删除等功能（旧版和新版API）。

路由前缀: /api (由 server.py 注册)
- /terminal/sessions, /terminal/create, /terminal/sessions/{session_id}
"""

from __future__ import annotations

import asyncio
import logging
import os
import traceback
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from peng.webui.websocket import terminal_session_manager, TerminalSession

logger = logging.getLogger(__name__)
router = APIRouter()


# ==================== Pydantic 模型 ====================


class TerminalSessionInfo(BaseModel):
    session_id: str
    pid: int | None = None
    created_at: float
    connection_count: int
    screen: str
    cursor_x: int = 0
    cursor_y: int = 0
    nest: str = "local"


class TerminalSessionListResponse(BaseModel):
    sessions: list[TerminalSessionInfo]


class TerminalSessionCreateResponse(BaseModel):
    session_id: str
    pid: int
    message: str


class CreateTerminalRequest(BaseModel):
    nest: str | None = None
    cols: int | None = None
    rows: int | None = None
    cwd: str | None = None


# ==================== 旧版终端API ====================


@router.get("/terminal/sessions")
async def get_terminal_sessions_legacy() -> dict[str, Any]:
    """获取所有活跃的终端会话列表（旧版格式）"""
    try:
        sessions = []
        for session_id, session in terminal_session_manager.sessions.items():
            if session.is_active():
                sessions.append(
                    {"session_id": session_id, "pid": session.child.pid if session.child else None, "nest": session.nest}
                )
        from peng.webui.websocket import remote_terminal_session_manager
        for front_session_id, session in remote_terminal_session_manager.sessions.items():
            sessions.append(
                {"session_id": front_session_id, "pid": None, "nest": session.nest_name}
            )
        logger.info(f"获取终端会话列表: {len(sessions)} 个活跃会话")
        return {"sessions": sessions, "count": len(sessions)}
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"获取终端会话列表失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


@router.post("/terminal/create")
async def create_terminal(request: CreateTerminalRequest) -> dict[str, Any]:
    """创建新的远程终端会话（后端驱动模式）"""
    try:
        from peng.webui.websocket import (
            remote_terminal_session_manager,
            _remote_terminal_sessions,
            manager as ws_manager,
        )

        nest_name = request.nest or "local"
        cols = request.cols or 80
        rows = request.rows or 24

        # 远程nest终端
        if nest_name and nest_name != "local":
            from peng.nest.bridge import NestBridgeManager
            from peng.nest.protocol import NestRequest

            nest_manager = NestBridgeManager.get_instance()
            conn = nest_manager.get_connection(nest_name)
            if not conn:
                raise HTTPException(status_code=400, detail=f"Nest '{nest_name}' 未连接")

            try:
                from peng.config.loader import load_config
                config = load_config()
                terminal_shell = config.webui.get("terminal", {}).get("shell")
                default_shell = terminal_shell if terminal_shell else None
            except Exception:
                default_shell = None

            create_params = {
                "shell": default_shell,
                "cols": cols,
                "rows": rows,
                "env": {"TERM": "xterm-256color", "LANG": "zh_CN.UTF-8", "LC_ALL": "zh_CN.UTF-8"},
                "_skip_security": True,
            }
            if request.cwd:
                create_params["cwd"] = request.cwd
            create_request = NestRequest(type="terminal_create", params=create_params)
            response = await conn.send_request(create_request, timeout=30.0)

            if response.error:
                raise HTTPException(status_code=500, detail=f"创建远程终端失败: {response.error}")

            remote_session_id = response.data.get("session_id")
            if not remote_session_id:
                raise HTTPException(status_code=500, detail="远程终端创建返回数据异常")

            import uuid as _uuid
            front_session_id = str(_uuid.uuid4())
            session = remote_terminal_session_manager.create_session(
                session_id=front_session_id, remote_session_id=remote_session_id, nest_name=nest_name,
            )

            _remote_terminal_sessions[front_session_id] = {
                "nest": nest_name, "remote_session_id": remote_session_id, "serialized_screen": "",
            }

            conn.register_terminal_callback(remote_session_id, session._on_terminal_output)
            session.start_forward_task(conn)

            nest_count = sum(1 for s in remote_terminal_session_manager.sessions.values() if s.nest_name == nest_name)
            title = f"{nest_name}-{nest_count}"

            logger.info(f"✅ 通过API创建远程终端: session_id={front_session_id}, remote_id={remote_session_id}, nest={nest_name}")

            await ws_manager.broadcast({
                "type": "terminal_event", "event": "terminal_created",
                "session_id": front_session_id, "nest": nest_name, "title": title,
            })

            return {"success": True, "session_id": front_session_id, "nest": nest_name, "title": title}
        else:
            # 本地终端
            from peng.webui.websocket import manager as ws_manager, PtyProcess, TerminalSession
            from peng.common.platform import IS_WINDOWS as _IS_WIN

            if _IS_WIN:
                raise HTTPException(status_code=400, detail="本地终端功能在Windows上需要pywinpty库")

            try:
                from peng.config.loader import load_config
                config = load_config()
                default_shell = config.webui.get("terminal", {}).get("shell", "/bin/zsh")
            except Exception:
                default_shell = "/bin/zsh"

            spawn_kwargs = {"dimensions": (rows, cols)}
            if request.cwd:
                spawn_kwargs["cwd"] = request.cwd
            pty_proc = PtyProcess.spawn(default_shell, **spawn_kwargs)

            session_id = str(uuid.uuid4())
            session = terminal_session_manager.create_session(
                session_id=session_id, pty_proc=pty_proc,
                read_task=asyncio.create_task(asyncio.sleep(0)), nest="local",
            )

            loop = asyncio.get_event_loop()

            async def _start_pty_reader():
                import codecs
                decoder = codecs.getincrementaldecoder("utf-8")("replace")

                def on_pty_readable():
                    try:
                        data = os.read(pty_proc.master_fd, 65536)
                        if data:
                            text = decoder.decode(data)
                            if session and session.websockets:
                                asyncio.ensure_future(session.broadcast(text))
                    except OSError:
                        loop.remove_reader(pty_proc.master_fd)
                        if session:
                            asyncio.ensure_future(session.broadcast({"type": "process_exited", "session_id": session_id}))

                loop.add_reader(pty_proc.master_fd, on_pty_readable)

            await _start_pty_reader()

            local_count = sum(1 for s in terminal_session_manager.sessions.values() if getattr(s, 'nest', None) == 'local')
            title = f"local-{local_count}"

            logger.info(f"✅ 通过API创建本地终端: session_id={session_id}")

            await ws_manager.broadcast({
                "type": "terminal_event", "event": "terminal_created",
                "session_id": session_id, "nest": "local", "title": title,
            })

            return {"success": True, "session_id": session_id, "nest": "local", "title": title}

    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"创建终端失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n\nTraceback:\n{tb}") from None


# ==================== 新版终端API ====================


@router.get("/terminal/sessions", response_model=TerminalSessionListResponse)
async def get_terminal_sessions() -> TerminalSessionListResponse:
    """获取所有活跃的终端会话列表（新版格式，带屏幕内容）"""
    try:
        sessions = []

        for session_id, session in terminal_session_manager.sessions.items():
            logger.info(f"检查会话: session_id={session_id}, is_active={session.is_active()}, pid={session.child.pid}, websockets={len(session.websockets)}")
            if session.is_active():
                screen, cursor_x, cursor_y = session.get_current_screen()
                logger.info(f"会话 {session_id} 屏幕内容长度: {len(screen)} 字节, 光标位置: ({cursor_x}, {cursor_y})")

                sessions.append(TerminalSessionInfo(
                    session_id=session_id, pid=session.child.pid,
                    created_at=session.created_at, connection_count=len(session.websockets),
                    screen=screen, cursor_x=cursor_x, cursor_y=cursor_y, nest=session.nest,
                ))

        logger.info(f"获取终端会话列表: 总共{len(terminal_session_manager.sessions)}个会话，{len(sessions)}个活跃会话")

        from peng.webui.websocket import remote_terminal_session_manager
        from peng.nest.bridge import NestBridgeManager
        nest_mgr = NestBridgeManager.get_instance()
        stale_remote_sessions = []
        for front_session_id, session in remote_terminal_session_manager.sessions.items():
            nest_conn = nest_mgr.get_connection(session.nest_name)
            if not nest_conn:
                stale_remote_sessions.append(front_session_id)
                continue
            sessions.append(TerminalSessionInfo(
                session_id=front_session_id, pid=None, created_at=0,
                connection_count=session.ws_count, screen=session.serialized_screen,
                cursor_x=0, cursor_y=0, nest=session.nest_name,
            ))
        for sid in stale_remote_sessions:
            remote_terminal_session_manager.remove_session(sid)
            logger.info(f"清理nest已断开的远程终端会话: session_id={sid}")

        return TerminalSessionListResponse(sessions=sessions)
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"获取终端会话列表失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"获取终端会话列表失败: {e}") from None


@router.post("/terminal/sessions", response_model=TerminalSessionCreateResponse)
async def create_terminal_session() -> TerminalSessionCreateResponse:
    """预分配终端会话ID"""
    try:
        session_id = str(uuid.uuid4())
        logger.info(f"预分配终端会话 ID: {session_id}")
        return TerminalSessionCreateResponse(
            session_id=session_id, pid=0, message="会话ID已生成，请使用WebSocket连接创建终端",
        )
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"创建终端会话失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"创建终端会话失败: {e}") from None


@router.delete("/terminal/sessions/{session_id}")
async def delete_terminal_session(session_id: str) -> dict[str, Any]:
    """关闭指定的终端会话"""
    try:
        if session_id not in terminal_session_manager.sessions:
            raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")

        terminal_session_manager.cleanup_session(session_id)
        logger.info(f"终端会话已关闭: session_id={session_id}")
        return {"success": True, "message": f"会话 {session_id} 已关闭"}
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"关闭终端会话失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"关闭终端会话失败: {e}") from None

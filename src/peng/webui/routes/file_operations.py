"""
文件操作路由模块

提供文件读写、列表、上传、编辑器自动保存、文件变化监测等功能。

路由前缀: /api (由 server.py 注册)
- /files/exists, /files/read, /files/write, /files/list, /files/image, /files/watch
- /upload, /upload/multiple
- /editor/auto-save
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import shlex
import sys
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from peng.common.paths import get_project_root, get_temp_directory, chown_to_project_owner, ensure_dir_with_owner
from peng.webui.routes._common import (
    AGENT_NAME,
    ALLOWED_EXTENSIONS,
    EDITOR_TEMP_DIR,
    IMAGE_EXTENSIONS,
    MAX_FILE_SIZE,
    UPLOAD_DIR,
    _get_selected_nest_conn,
    detect_language,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _resolve_local_data_path(file_path: str) -> str | None:
    """检查路径是否是 local nest 项目目录的 data/ 子目录路径

    如果是，返回本地绝对路径；否则返回 None。
    用于确保 WebUI 文件操作对 data/ 目录始终访问本地项目目录。
    """
    from peng.common.paths import resolve_local_data_path
    return resolve_local_data_path(file_path)


# ==================== FileWatcher ====================


class FileWatcher:
    """文件变化监测类，支持本地文件和远程nest文件"""

    def __init__(self, poll_interval: float = 1.0):
        self.poll_interval = poll_interval
        self._files: dict[str, dict[str, Any]] = {}
        self._watch_task: asyncio.Task | None = None
        self._user_saved_files: set[str] = set()

    @staticmethod
    def _normalize_path(path: str) -> str:
        """规范化文件路径"""
        file_path = Path(path)
        if not file_path.is_absolute():
            file_path = file_path.resolve()
        return str(file_path)

    async def register(self, path: str, content: str, nest: str | None = None) -> None:
        """注册文件到监测（支持本地和远程nest文件）

        Args:
            path: 文件路径
            content: 文件内容
            nest: nest名称。None或"local"表示本地文件，其他表示远程nest
        """
        try:
            path_str = self._normalize_path(path)
            # "local" nest 实际上是本地文件
            is_local = not nest or nest == "local"

            if is_local:
                # 本地文件：使用本地stat
                stat = Path(path_str).stat()
                self._files[path_str] = {
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "original_content": content,
                    "nest": None,
                }
                logger.info(
                    f"📄 本地文件已注册监测: {path_str} (mtime={stat.st_mtime:.3f}, size={stat.st_size})"
                )
            else:
                # 远程nest文件：异步获取远程stat信息
                stat_info = await self._get_nest_stat(path_str, nest)
                mtime = stat_info["mtime"] if stat_info else 0
                size = stat_info["size"] if stat_info else 0
                self._files[path_str] = {
                    "mtime": mtime,
                    "size": size,
                    "original_content": content,
                    "nest": nest,
                }
                logger.info(
                    f"📄 远程文件已注册监测: {path_str} (mtime={mtime:.3f}, size={size}, nest={nest})"
                )
        except Exception as e:
            logger.warning(f"注册文件监测失败: {path}, error={e}")

    def unregister(self, path: str) -> None:
        """注销文件监测"""
        try:
            path_str = self._normalize_path(path)
            if path_str in self._files:
                del self._files[path_str]
                logger.info(f"📄 文件已注销监测: {path_str}")
        except Exception as e:
            logger.warning(f"注销文件监测失败: {path}, error={e}")

    def update_after_save(self, path: str, content: str | None = None) -> None:
        """更新文件状态（保存后调用）"""
        try:
            path_str = self._normalize_path(path)

            if path_str not in self._files:
                return

            file_info = self._files[path_str]
            nest = file_info.get("nest")

            if nest:
                # 远程nest文件：直接用传入的内容更新
                if content:
                    file_info["original_content"] = content
                # 通过远程stat更新mtime/size
                try:
                    stat_info = self._get_nest_stat_sync(path_str, nest)
                    if stat_info:
                        file_info["mtime"] = stat_info["mtime"]
                        file_info["size"] = stat_info["size"]
                except Exception:
                    pass
                logger.info(f"📄 远程文件状态已更新: {path_str}")
            else:
                # 本地文件
                stat = Path(path_str).stat()
                file_info["mtime"] = stat.st_mtime
                file_info["size"] = stat.st_size
                if content:
                    file_info["original_content"] = content
                else:
                    try:
                        file_info["original_content"] = Path(path_str).read_text(encoding="utf-8")
                    except Exception:
                        pass
                logger.info(f"📄 文件状态已更新: {path_str}")
        except Exception as e:
            logger.warning(f"更新文件状态失败: {path}, error={e}")

    def mark_user_saved(self, path: str) -> None:
        """标记文件已被用户保存"""
        try:
            path_str = self._normalize_path(path)
            self._user_saved_files.add(path_str)

            def clear_mark():
                self._user_saved_files.discard(path_str)

            asyncio.get_event_loop().call_later(self.poll_interval * 3, clear_mark)
        except Exception as e:
            logger.warning(f"标记用户保存失败: {path}, error={e}")

    @staticmethod
    def _get_nest_stat_sync(path_str: str, nest: str) -> dict | None:
        """同步方式获取nest文件stat（用于非async方法）"""
        try:
            from peng.nest.bridge import NestBridgeManager
            manager = NestBridgeManager.get_instance()
            conn = manager.get_connection(nest)
            if not conn:
                return None
            # 注意：不能在async上下文中直接调用async方法
            # 这里用简单的方式：返回None，让async方法处理
            return None
        except Exception:
            return None

    async def _get_nest_stat(self, path_str: str, nest: str) -> dict | None:
        """异步获取远程nest文件的stat信息"""
        try:
            from peng.nest.bridge import NestBridgeManager
            from peng.nest.protocol import NestRequest

            manager = NestBridgeManager.get_instance()
            conn = manager.get_connection(nest)
            if not conn:
                return None

            request = NestRequest(type="stat", params={"path": path_str, "_skip_security": True})
            response = await conn.send_request(request, timeout=10.0)

            if response.error:
                return None

            data = response.data
            if data and data.get("is_file"):
                return {
                    "mtime": data.get("mtime", 0),
                    "size": data.get("size", 0),
                }
        except Exception as e:
            logger.debug(f"获取远程文件stat失败: {path_str}, nest={nest}, error={e}")
        return None

    async def _get_nest_file_content(self, path_str: str, nest: str) -> str | None:
        """异步读取远程nest文件内容"""
        try:
            from peng.nest.bridge import NestBridgeManager
            from peng.nest.protocol import NestRequest

            manager = NestBridgeManager.get_instance()
            conn = manager.get_connection(nest)
            if not conn:
                return None

            request = NestRequest(
                type="read",
                params={"path": path_str, "offset": 1, "limit": 100000, "_skip_security": True},
            )
            response = await conn.send_request(request, timeout=30.0)

            if response.error:
                return None

            data = response.data
            if not data or data.get("type") != "file":
                return None

            lines = data.get("lines", [])
            content_lines = []
            for line in lines:
                if ": " in line:
                    content_lines.append(line.split(": ", 1)[1])
                else:
                    content_lines.append(line)
            return "\n".join(content_lines)
        except Exception as e:
            logger.warning(f"读取远程文件内容失败: {path_str}, nest={nest}, error={e}")
            return None

    async def _check_nest_file_changed(self, path_str: str, file_info: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
        """检查远程nest文件是否变化"""
        nest = file_info.get("nest")
        if not nest:
            return False, None

        try:
            stat_info = await self._get_nest_stat(path_str, nest)
            if stat_info is None:
                return False, None

            current_mtime = stat_info["mtime"]
            current_size = stat_info["size"]

            if current_mtime != file_info["mtime"] or current_size != file_info["size"]:
                new_content = await self._get_nest_file_content(path_str, nest)
                if new_content is not None:
                    file_data = {
                        "file_path": path_str,
                        "old_content": file_info["original_content"],
                        "new_content": new_content,
                        "nest": nest,
                    }
                    return True, file_data
        except Exception as e:
            logger.warning(f"检查远程文件变化失败: {path_str}, nest={nest}, error={e}")
        return False, None

    def _check_file_changed(self, path_str: str, file_info: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
        """检查本地文件是否变化"""
        try:
            stat = Path(path_str).stat()
            current_mtime = stat.st_mtime
            current_size = stat.st_size

            if current_mtime != file_info["mtime"] or current_size != file_info["size"]:
                new_content = Path(path_str).read_text(encoding="utf-8")
                file_data = {
                    "file_path": path_str,
                    "old_content": file_info["original_content"],
                    "new_content": new_content,
                    "nest": file_info.get("nest"),
                }
                return True, file_data
        except Exception as e:
            logger.warning(f"检查文件变化失败: {path_str}, error={e}")
        return False, None

    async def _poll_loop(self) -> None:
        """轮询循环（同时处理本地和远程nest文件）"""
        while True:
            try:
                changed_files = []
                for path_str, file_info in list(self._files.items()):
                    nest = file_info.get("nest")

                    if nest:
                        # 远程nest文件：异步检查
                        is_changed, file_data = await self._check_nest_file_changed(path_str, file_info)
                    else:
                        # 本地文件：同步检查
                        is_changed, file_data = self._check_file_changed(path_str, file_info)

                    if is_changed and file_data:
                        if path_str in self._user_saved_files:
                            logger.info(f"📄 忽略用户保存的变化: {path_str}")
                            self.update_after_save(path_str, content=file_data["new_content"])
                        else:
                            changed_files.append(file_data)
                            self._files[path_str]["original_content"] = file_data["new_content"]
                            # 更新mtime/size
                            if nest:
                                # nest文件：异步获取stat
                                stat_info = await self._get_nest_stat(path_str, nest)
                                if stat_info:
                                    self._files[path_str]["mtime"] = stat_info["mtime"]
                                    self._files[path_str]["size"] = stat_info["size"]
                            else:
                                try:
                                    stat = Path(path_str).stat()
                                    self._files[path_str]["mtime"] = stat.st_mtime
                                    self._files[path_str]["size"] = stat.st_size
                                except Exception:
                                    pass

                if changed_files:
                    from peng.webui.websocket import manager

                    for file_data in changed_files:
                        await manager.broadcast({
                            "type": "file_changed",
                            "file_path": file_data["file_path"],
                            "old_content": file_data["old_content"],
                            "new_content": file_data["new_content"],
                            "nest": file_data.get("nest"),
                            "user_edited": False,
                            "timestamp": time.time(),
                        })
                    logger.info(f"📡 广播了 {len(changed_files)} 个文件变化")
            except Exception as e:
                logger.error(f"轮询异常: {e}\n{traceback.format_exc()}")

            await asyncio.sleep(self.poll_interval)

    def start(self) -> None:
        """启动文件监测"""
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.create_task(self._poll_loop())
            logger.info("✅ 文件监测轮询已启动")

    def stop(self) -> None:
        """停止文件监测"""
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
            logger.info("✅ 文件监测轮询已停止")


# 全局文件监测实例
file_watcher = FileWatcher()


# ==================== Nest 远程文件操作 ====================


async def _nest_file_list(nest_conn, dir_path: str) -> dict:
    """通过nest获取远程目录列表"""
    from peng.nest.protocol import NestRequest

    is_platform_windows = nest_conn.info.get("platform", "") in ("Windows", "windows")

    if dir_path == "":
        if not is_platform_windows:
            return await _nest_file_list(nest_conn, "/")

        request = NestRequest(
            type="list_drives",
            params={"_skip_security": True},
        )
        response = await nest_conn.send_request(request, timeout=30.0)

        if response.error:
            raise Exception(response.error)

        data = response.data
        if not data or data.get("type") != "drives":
            raise Exception(f"返回数据格式异常: {data}")

        drives = data.get("drives", [])
        files = []
        for drive in drives:
            drive_name = drive.rstrip("/")
            files.append({
                "name": drive_name,
                "path": drive,
                "type": "directory",
            })

        return {
            "success": True,
            "path": "",
            "parent_path": None,
            "files": files,
        }

    safe_path = dir_path.replace("\\", "/")

    request = NestRequest(
        type="list_dir",
        params={"path": safe_path, "_skip_security": True},
    )
    response = await nest_conn.send_request(request, timeout=30.0)

    if response.error:
        raise Exception(response.error)

    data = response.data
    if not data or data.get("type") != "directory":
        raise Exception(f"返回数据格式异常: {data}")

    entries = data.get("entries", [])
    files = []

    remote_path = data.get("path", safe_path)
    remote_path = remote_path.replace("\\", "/")

    parent_path = None
    stripped = remote_path.rstrip("/")
    if "/" in stripped:
        parent_candidate = stripped.rsplit("/", 1)[0]
        if parent_candidate != stripped:
            parent_path = parent_candidate if parent_candidate else "/"
            if is_platform_windows and len(parent_path) == 2 and parent_path[1] == ":" and parent_path[0].isalpha():
                parent_path = parent_path + "/"

    if parent_path is None and is_platform_windows:
        if len(stripped) == 2 and stripped[1] == ":" and stripped[0].isalpha():
            parent_path = ""

    for entry in entries:
        # 兼容两种格式：旧格式为字符串 "name/" 或 "name"，新格式为 dict
        if isinstance(entry, dict):
            is_dir = entry.get("is_dir", False)
            name = entry.get("name", "")
            entry_size = entry.get("size")
            entry_mtime = entry.get("mtime")
            if entry_mtime is not None:
                try:
                    from datetime import datetime as _dt
                    entry_mtime = _dt.fromtimestamp(entry_mtime).strftime("%Y-%m-%d %H:%M")
                except (OSError, ValueError):
                    entry_mtime = None
        else:
            # 旧格式：字符串
            is_dir = entry.endswith("/")
            name = entry.rstrip("/")
            entry_size = None
            entry_mtime = None
        if not name:
            continue
        if remote_path == "/":
            entry_path = "/" + name
        else:
            entry_path = remote_path.rstrip("/") + "/" + name
        files.append({
            "name": name,
            "path": entry_path,
            "type": "directory" if is_dir else "file",
            "size": entry_size,
            "mtime": entry_mtime,
        })

    files.sort(key=lambda x: (0 if x["type"] == "directory" else 1, x["name"].lower()))

    return {
        "success": True,
        "path": remote_path,
        "parent_path": parent_path,
        "files": files,
    }


async def _nest_file_read(nest_conn, file_path: str) -> dict:
    """通过nest读取远程文件内容"""
    from peng.nest.protocol import NestRequest

    request = NestRequest(
        type="read",
        params={"path": file_path, "offset": 1, "limit": 100000, "_skip_security": True},
    )
    response = await nest_conn.send_request(request, timeout=30.0)

    if response.error:
        raise Exception(response.error)

    data = response.data
    if not data or data.get("type") != "file":
        if data and data.get("type") == "binary":
            raise Exception("不支持远程二进制文件读取，请在本地编辑")
        raise Exception(f"返回数据格式异常: {data}")

    lines = data.get("lines", [])
    content_lines = []
    for line in lines:
        if ": " in line:
            content_lines.append(line.split(": ", 1)[1])
        else:
            content_lines.append(line)

    content = "\n".join(content_lines)
    language = detect_language(file_path)

    return {
        "content": content,
        "language": language,
        "path": data.get("path", file_path),
    }


async def _nest_file_write(nest_conn, file_path: str, content: str) -> dict:
    """通过nest写入远程文件"""
    from peng.nest.protocol import NestRequest

    request = NestRequest(
        type="write",
        params={"path": file_path, "content": content, "_skip_security": True},
    )
    response = await nest_conn.send_request(request, timeout=30.0)

    if response.error:
        raise Exception(response.error)

    return {"success": True, "message": f"文件已保存: {file_path}"}


async def _nest_file_exists(nest_conn, file_path: str) -> dict:
    """通过nest检查远程文件是否存在，返回stat信息"""
    from peng.nest.protocol import NestRequest

    request = NestRequest(
        type="stat",
        params={"path": file_path, "_skip_security": True},
    )
    response = await nest_conn.send_request(request, timeout=30.0)

    if response.error:
        return {"success": True, "exists": False}

    data = response.data or {}
    is_file = data.get("is_file", False)
    is_dir = data.get("is_dir", False)
    
    # 判断是否图片
    IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg', '.ico', '.tiff', '.tif', '.avif', '.heic', '.heif'}
    file_ext = Path(file_path).suffix.lower()
    is_image = file_ext in IMAGE_EXTENSIONS
    
    return {
        "success": True, 
        "exists": is_file,
        "size": data.get("size") if is_file else None,
        "is_image": is_image,
        "is_dir": is_dir,
    }


async def _nest_create_directory(nest_conn, parent_path: str, dir_name: str) -> dict:
    """通过nest在远程创建目录（跨平台兼容）"""
    from peng.nest.protocol import NestRequest

    safe_parent = parent_path.replace("\\", "/")
    new_dir_path = safe_parent.rstrip("/") + "/" + dir_name

    is_windows = nest_conn.info.get("platform", "") in ("Windows", "windows")
    if is_windows:
        # Windows cmd.exe: mkdir 默认创建多级目录（无需 /P 标志，/P 无效！）
        # 用反斜杠路径 + 双引号包裹，双引号内部转义为 \"
        win_path = new_dir_path.replace("/", "\\")
        quoted_path = win_path.replace('"', '\\"')
        cmd = f'mkdir "{quoted_path}" && echo OK'
    else:
        # Linux/macOS/WSL：使用 shlex.quote 安全转义（单引号）
        cmd = f"mkdir -p {shlex.quote(new_dir_path)} && echo OK"

    request = NestRequest(
        type="bash",
        params={
            "command": cmd,
            "_skip_security": True,
        },
    )
    response = await nest_conn.send_request(request, timeout=30.0)

    if response.error:
        raise Exception(response.error)

    # 检查 bash 命令执行结果
    data = response.data if isinstance(response.data, dict) else {}
    exit_code = data.get("exit_code", -1)
    stderr = (data.get("stderr", "") or "").strip()
    stdout = (data.get("stdout", "") or "").strip()

    if exit_code != 0 or "OK" not in stdout:
        detail = f"exit_code={exit_code}" + (f", stderr={stderr}" if stderr else "")
        raise Exception(f"创建目录失败 ({detail})")

    return {
        "success": True,
        "path": new_dir_path,
        "message": f"目录已创建: {dir_name}",
    }


# ==================== 上传错误类型 ====================


class UploadErrorType:
    FILE_TYPE_NOT_ALLOWED = "FILE_TYPE_NOT_ALLOWED"
    FILE_SIZE_EXCEEDED = "FILE_SIZE_EXCEEDED"
    NETWORK_ERROR = "NETWORK_ERROR"
    SERVER_ERROR = "SERVER_ERROR"
    IO_ERROR = "IO_ERROR"
    PERMISSION_ERROR = "PERMISSION_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class UploadError(Exception):
    def __init__(self, error_type: str, message: str, details: dict | None = None):
        self.error_type = error_type
        self.message = message
        self.details = details or {}
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": self.error_type,
            "message": self.message,
            "details": self.details,
        }


# ==================== 上传回调 ====================

_upload_callbacks: list[Callable] = []


def register_upload_callback(callback):
    _upload_callbacks.append(callback)


def unregister_upload_callback(callback) -> bool:
    if callback in _upload_callbacks:
        _upload_callbacks.remove(callback)
        return True
    return False


def _notify_upload_callbacks(response) -> None:
    for callback in _upload_callbacks:
        try:
            callback(response)
        except Exception as e:
            logger.error(f"上传回调函数执行失败：{callback.__name__}, 错误：{e}")


def _classify_upload_error(e: Exception) -> UploadError:
    if isinstance(e, UploadError):
        return e
    if "不支持的文件类型" in str(e):
        return UploadError(error_type=UploadErrorType.FILE_TYPE_NOT_ALLOWED, message=str(e))
    if "文件大小超过限制" in str(e):
        return UploadError(error_type=UploadErrorType.FILE_SIZE_EXCEEDED, message=str(e))
    network_keywords = ["connection", "network", "timeout", "socket", "connect"]
    if any(kw in str(e).lower() for kw in network_keywords):
        return UploadError(error_type=UploadErrorType.NETWORK_ERROR, message=f"网络错误：{str(e)}", details={"suggestion": "请检查网络连接后重试"})
    if isinstance(e, PermissionError) or "permission" in str(e).lower():
        return UploadError(error_type=UploadErrorType.PERMISSION_ERROR, message=f"权限错误：{str(e)}", details={"suggestion": "请检查文件权限"})
    if isinstance(e, (IOError, OSError)) or "io" in str(e).lower():
        return UploadError(error_type=UploadErrorType.IO_ERROR, message=f"IO 错误：{str(e)}")
    return UploadError(error_type=UploadErrorType.SERVER_ERROR, message=f"服务器错误：{str(e)}")


# ==================== Pydantic 响应模型 ====================


class UploadResponse(BaseModel):
    success: bool
    path: str | None = None
    filename: str | None = None
    size: int | None = None
    error: str | None = None
    error_type: str | None = None
    retry_count: int | None = None
    retryable: bool = False


class FileReadResponse(BaseModel):
    content: str
    language: str
    path: str


class FileExistsResponse(BaseModel):
    success: bool
    exists: bool
    size: int | None = None  # 文件大小（字节）
    is_image: bool | None = None  # 是否为图片文件
    is_dir: bool | None = None  # 是否为目录


class FileChunkResponse(BaseModel):
    """分块读取文件响应"""
    success: bool
    content: str  # 这一块的文本内容
    path: str  # 文件绝对路径
    language: str  # 文件语言
    offset: int  # 本次读取的字节偏移
    chunk_size: int  # 本次读取的字节数
    total_size: int  # 文件总大小
    has_more: bool  # 是否还有更多数据


class FileWriteRequest(BaseModel):
    path: str
    content: str


class FileWriteResponse(BaseModel):
    success: bool
    message: str | None = None


class FileInfo(BaseModel):
    name: str
    path: str
    type: str
    size: int | None = None  # 文件大小（字节），目录为 None
    mtime: str | None = None  # 最后修改时间，格式 YYYY-MM-DD HH:MM


class FileListResponse(BaseModel):
    success: bool
    path: str
    parent_path: str | None = None
    files: list[FileInfo]
    message: str | None = None


class EditorAutoSaveRequest(BaseModel):
    tabId: str
    path: str | None = None
    content: str
    editType: str | None = None
    rootMsgId: int | None = None
    isContentEditor: bool | None = None
    editId: int | None = None


class EditorAutoSaveResponse(BaseModel):
    success: bool
    tabId: str


class EditorAutoSaveReadResponse(BaseModel):
    success: bool
    content: str | None = None
    tabId: str | None = None
    editType: str | None = None
    rootMsgId: int | None = None
    isContentEditor: bool | None = None
    editId: int | None = None


class CreateDirectoryRequest(BaseModel):
    parent_path: str
    dir_name: str


class CreateDirectoryResponse(BaseModel):
    success: bool
    path: str | None = None
    message: str | None = None


class FileDeleteRequest(BaseModel):
    """文件/目录删除请求"""
    path: str


class FileDeleteResponse(BaseModel):
    success: bool
    backup_path: str | None = None
    original_path: str | None = None
    message: str | None = None


class FileRestoreRequest(BaseModel):
    """文件/目录恢复请求"""
    backup_path: str
    original_path: str


class FileRestoreResponse(BaseModel):
    success: bool
    restored_path: str | None = None
    message: str | None = None


class ToolRollbackRequest(BaseModel):
    """工具调用回退请求"""
    rollback_meta: dict


class ToolRollbackResponse(BaseModel):
    success: bool
    message: str | None = None
    undo_display: str | None = None


# ==================== 路由端点 ====================


@router.post("/upload", response_model=UploadResponse)
async def upload_file(file: UploadFile = File(...)) -> UploadResponse:  # noqa: B008
    """上传文件"""
    try:
        ext = Path(file.filename or "").suffix.lower()
        if ext and ext not in ALLOWED_EXTENSIONS:
            return UploadResponse(success=False, error=f"不支持的文件类型: {ext}")

        content = await file.read()

        if len(content) > MAX_FILE_SIZE:
            return UploadResponse(success=False, error=f"文件大小超过限制 ({MAX_FILE_SIZE // 1024 // 1024}MB)")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = uuid.uuid4().hex[:8]
        safe_filename = f"{timestamp}_{unique_id}_{file.filename or 'unknown'}"
        file_path = UPLOAD_DIR / safe_filename

        file_path.write_bytes(content)
        logger.info(f"文件上传成功: {file.filename} -> {file_path}")

        return UploadResponse(success=True, path=str(file_path), filename=file.filename, size=len(content))

    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"文件上传失败: {e}\n{tb}")
        return UploadResponse(success=False, error=f"{e}\n\nTraceback:\n{tb}")


@router.post("/upload/multiple")
async def upload_multiple_files(files: list[UploadFile] = File(...)) -> list[UploadResponse]:  # noqa: B008
    """批量上传文件"""
    results = []
    for file in files:
        result = await upload_file(file)
        results.append(result)
    return results


@router.get("/files/exists", response_model=FileExistsResponse)
async def file_exists(path: str, nest: str | None = None) -> FileExistsResponse:
    """检查文件是否存在"""
    # 🔧 data/ 目录始终检查本地
    local_data = _resolve_local_data_path(path)
    if local_data:
        path = local_data  # 使用本地绝对路径
        nest = None        # 强制本地

    nest_conn = _get_selected_nest_conn(nest)
    if nest_conn:
        try:
            result = await _nest_file_exists(nest_conn, path)
            return FileExistsResponse(
                success=result["success"], 
                exists=result["exists"],
                size=result.get("size"),
                is_image=result.get("is_image"),
                is_dir=result.get("is_dir"),
            )
        except Exception as e:
            logger.error(f"远程文件检查失败(nest={nest}): {e}")
            return FileExistsResponse(success=False, exists=False)

    try:
        file_path = Path(path)
        if not file_path.is_absolute():
            file_path = Path.cwd() / file_path
        is_dir = file_path.is_dir()
        exists = file_path.exists() and file_path.is_file()
        file_size = None
        is_image = None
        if exists:
            try:
                file_size = file_path.stat().st_size
            except OSError:
                file_size = None
            is_image = file_path.suffix.lower() in IMAGE_EXTENSIONS
        return FileExistsResponse(success=True, exists=exists, size=file_size, is_image=is_image, is_dir=is_dir)
    except Exception as e:
        logger.error(f"检查文件存在性失败: {e}")
        return FileExistsResponse(success=False, exists=False)


@router.get("/files/image")
async def serve_image(path: str, nest: str | None = None) -> Response:
    """提供图片文件服务"""
    import mimetypes

    logger.info(f"[ImageViewer] 请求图片: path={path}, nest={nest}")

    # 🔧 data/ 目录始终从本地读取
    local_data = _resolve_local_data_path(path)
    if local_data:
        path = local_data
        nest = None

    nest_conn = _get_selected_nest_conn(nest)
    if nest_conn:
        try:
            from peng.nest.protocol import NestRequest

            request = NestRequest(type="read", params={"path": path, "_skip_security": True})
            response = await nest_conn.send_request(request, timeout=30.0)

            if response.error:
                raise HTTPException(status_code=404, detail=f"远程文件不存在: {path}")

            data = response.data
            if data and data.get("type") == "binary":
                image_bytes = base64.b64decode(data["data"])
                mime = data.get("mime", "application/octet-stream")
                return Response(content=image_bytes, media_type=mime)
            else:
                raise HTTPException(status_code=400, detail=f"非图片文件: {path}")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"远程图片读取失败(nest={nest}): {e}")
            raise HTTPException(status_code=500, detail=f"远程图片读取失败: {e}")

    try:
        file_path = Path(path)
        if not file_path.is_absolute():
            file_path = Path.cwd() / file_path

        if not file_path.exists() or not file_path.is_file():
            raise HTTPException(status_code=404, detail=f"文件不存在: {path}")

        ext = file_path.suffix.lower()
        if ext not in IMAGE_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"非图片文件: {path}")

        with open(file_path, "rb") as f:
            image_bytes = f.read()

        mime = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        return Response(content=image_bytes, media_type=mime)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"读取图片失败: {e}")
        raise HTTPException(status_code=500, detail=f"读取图片失败: {e}")


@router.get("/files/read", response_model=FileReadResponse)
async def read_file(path: str, nest: str | None = None) -> FileReadResponse:
    """读取文件内容"""
    # 🔧 data/ 目录始终从本地读取
    local_data = _resolve_local_data_path(path)
    if local_data:
        path = local_data
        nest = None

    nest_conn = _get_selected_nest_conn(nest)
    if nest_conn:
        try:
            result = await _nest_file_read(nest_conn, path)
            # 注册远程文件到变化监测
            await file_watcher.register(result.get("path", path), result["content"], nest=nest)
            return FileReadResponse(content=result["content"], language=result["language"], path=result["path"])
        except Exception as e:
            logger.error(f"远程文件读取失败(nest={nest}): {e}")
            raise HTTPException(status_code=500, detail=f"远程文件读取失败: {e}")

    try:
        file_path = Path(path)
        if not file_path.is_absolute():
            file_path = Path.cwd() / file_path

        if not file_path.exists():
            raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
        if not file_path.is_file():
            raise HTTPException(status_code=400, detail=f"不是文件: {path}")

        file_size = file_path.stat().st_size
        if file_size > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f"文件太大 ({file_size // 1024 // 1024}MB > {MAX_FILE_SIZE // 1024 // 1024}MB)")

        binary_extensions = {
            ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".tiff", ".tif",
            ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
            ".mp3", ".mp4", ".avi", ".mov", ".wav", ".flac",
            ".exe", ".dll", ".so", ".dylib", ".bin", ".dat",
        }
        file_ext = file_path.suffix.lower()
        if file_ext in binary_extensions:
            with open(file_path, "rb") as f:
                raw_bytes = f.read()
            content = base64.b64encode(raw_bytes).decode("ascii")
            language = "binary"
        else:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            language = detect_language(str(file_path))

        logger.info(f"文件读取成功: {path}")
        await file_watcher.register(str(file_path), content, nest=nest)

        return FileReadResponse(content=content, language=language, path=str(file_path))
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"读取文件失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"读取文件失败: {e}")


@router.get("/files/read-chunk", response_model=FileChunkResponse)
async def read_file_chunk(
    path: str,
    offset: int = 0,
    chunk_size: int = 1048576,  # 默认1MB
    nest: str | None = None,
) -> FileChunkResponse:
    """分块读取文件内容（用于大文件异步加载）
    
    Args:
        path: 文件路径
        offset: 字节偏移（从0开始）
        chunk_size: 本次读取的最大字节数（默认1MB）
        nest: nest名称
    """
    # 🔧 data/ 目录始终从本地读取
    local_data = _resolve_local_data_path(path)
    if local_data:
        path = local_data
        nest = None

    # 限制单次最大读取量
    chunk_size = min(chunk_size, 5 * 1024 * 1024)  # 最大5MB

    nest_conn = _get_selected_nest_conn(nest)
    if nest_conn:
        try:
            # 所有nest统一走websocket请求
            from peng.nest.protocol import NestRequest
            request = NestRequest(
                type="read_bytes",
                params={"path": path, "offset": offset, "chunk_size": chunk_size, "_skip_security": True},
            )
            response = await nest_conn.send_request(request, timeout=30.0)
            if response.error:
                raise HTTPException(status_code=500, detail=response.error)
            data = response.data
            return FileChunkResponse(
                success=True,
                content=data["content"],
                path=data["path"],
                language=detect_language(path),
                offset=data["offset"],
                chunk_size=data["chunk_size"],
                total_size=data["total_size"],
                has_more=data["has_more"],
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"远程分块读取失败: {e}")

    try:
        file_path = Path(path)
        if not file_path.is_absolute():
            file_path = Path.cwd() / file_path

        if not file_path.exists():
            raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
        if not file_path.is_file():
            raise HTTPException(status_code=400, detail=f"不是文件: {path}")

        total_size = file_path.stat().st_size
        language = detect_language(str(file_path))

        with open(file_path, "r", encoding="utf-8") as f:
            f.seek(offset)
            content = f.read(chunk_size)

        actual_read = len(content.encode("utf-8"))
        has_more = (offset + actual_read) < total_size

        return FileChunkResponse(
            success=True,
            content=content,
            path=str(file_path),
            language=language,
            offset=offset,
            chunk_size=actual_read,
            total_size=total_size,
            has_more=has_more,
        )
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"分块读取文件失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"分块读取文件失败: {e}")


@router.post("/files/write", response_model=FileWriteResponse)
async def write_file(request: FileWriteRequest, nest: str | None = None) -> FileWriteResponse:
    """写入文件内容"""
    # 🔧 data/ 目录始终写入本地
    local_data = _resolve_local_data_path(request.path)
    if local_data:
        request = FileWriteRequest(path=local_data, content=request.content)
        nest = None

    nest_conn = _get_selected_nest_conn(nest)
    if nest_conn:
        try:
            result = await _nest_file_write(nest_conn, request.path, request.content)
            # 标记用户保存并更新文件监测状态，避免触发虚假变更广播
            file_watcher.mark_user_saved(request.path)
            file_watcher.update_after_save(request.path, content=request.content)
            return FileWriteResponse(success=True, message=result.get("message", f"文件已保存: {request.path}"))
        except Exception as e:
            logger.error(f"远程文件写入失败(nest={nest}): {e}")
            raise HTTPException(status_code=500, detail=f"远程文件写入失败: {e}")

    try:
        file_path = Path(request.path)
        if not file_path.is_absolute():
            file_path = Path.cwd() / file_path

        if len(request.content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f"内容太大 ({len(request.content) // 1024 // 1024}MB > {MAX_FILE_SIZE // 1024 // 1024}MB)")

        file_path.parent.mkdir(parents=True, exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(request.content)
        chown_to_project_owner(file_path)

        logger.info(f"文件写入成功: {request.path}")
        file_watcher.mark_user_saved(str(file_path))
        file_watcher.update_after_save(str(file_path))

        return FileWriteResponse(success=True, message=f"文件已保存: {request.path}")
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"写入文件失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"写入文件失败: {e}")


@router.delete("/files/watch")
async def unregister_file_watch(path: str = Query(...)) -> dict[str, Any]:
    """注销文件监测"""
    try:
        file_watcher.unregister(path)
        return {"success": True, "message": f"文件监测已注销: {path}"}
    except Exception as e:
        logger.error(f"注销文件监测失败: {e}")
        return {"success": False, "message": f"注销失败: {e}"}


@router.get("/files/list", response_model=FileListResponse)
async def list_files(path: str = ".", nest: str | None = None) -> FileListResponse:
    """列出目录内容"""
    # 🔧 data/ 目录始终从本地列出
    local_data = _resolve_local_data_path(path)
    if local_data:
        path = local_data
        nest = None

    nest_conn = _get_selected_nest_conn(nest)
    if nest_conn:
        try:
            logger.info(f"远程文件列表: nest={nest}, path={path}")
            result = await _nest_file_list(nest_conn, path)
            return FileListResponse(
                success=result["success"],
                path=result["path"],
                parent_path=result.get("parent_path"),
                files=[FileInfo(**f) for f in result.get("files", [])],
            )
        except Exception as e:
            logger.warning(f"远程目录列表失败(nest={nest}, path={path}): {e}")
            safe_path = path.replace("\\", "/")
            stripped = safe_path.rstrip("/")
            fallback_parent = None
            if "/" in stripped:
                fallback_parent = stripped.rsplit("/", 1)[0] or "/"
            return FileListResponse(
                success=False, path=path, parent_path=fallback_parent,
                files=[], message=f"远程目录列表失败: {e}",
            )

    try:
        dir_path = Path(path)
        if not dir_path.is_absolute():
            dir_path = Path.cwd() / dir_path

        if not dir_path.exists():
            return FileListResponse(success=False, path=path, files=[], message=f"目录不存在: {path}")

        if not dir_path.is_dir():
            return FileListResponse(success=False, path=path, files=[], message=f"不是目录: {path}")

        files = []
        for item in dir_path.iterdir():
            name = item.name
            if name.startswith(".") and name not in (".peng",):
                continue
            is_dir = item.is_dir()
            try:
                stat = item.stat()
                file_size = None if is_dir else stat.st_size
                mtime_str = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            except (OSError, ValueError):
                file_size = None
                mtime_str = None
            files.append(FileInfo(
                name=name,
                path=str(item),
                type="directory" if is_dir else "file",
                size=file_size,
                mtime=mtime_str,
            ))

        # 排序：目录在前
        files.sort(key=lambda x: (0 if x.type == "directory" else 1, x.name.lower()))

        parent_path = str(dir_path.parent) if dir_path.parent != dir_path else None

        return FileListResponse(
            success=True,
            path=str(dir_path),
            parent_path=parent_path,
            files=files,
        )
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"列出目录内容失败: {e}\n{tb}")
        return FileListResponse(success=False, path=path, files=[], message=f"列出目录内容失败: {e}")


@router.post("/files/create-directory", response_model=CreateDirectoryResponse)
async def create_directory(request: CreateDirectoryRequest, nest: str | None = None) -> CreateDirectoryResponse:
    """创建目录"""
    # 🔧 data/ 目录始终在本地创建
    local_data = _resolve_local_data_path(f"{request.parent_path}/{request.dir_name}")
    if local_data:
        request = CreateDirectoryRequest(
            parent_path=str(Path(local_data).parent),
            dir_name=Path(local_data).name,
        )
        nest = None

    nest_conn = _get_selected_nest_conn(nest)
    if nest_conn:
        try:
            result = await _nest_create_directory(nest_conn, request.parent_path, request.dir_name)
            return CreateDirectoryResponse(success=True, path=result.get("path"), message=result.get("message"))
        except Exception as e:
            logger.error(f"远程创建目录失败(nest={nest}): {e}")
            raise HTTPException(status_code=500, detail=f"远程创建目录失败: {e}")

    try:
        parent_path = Path(request.parent_path)
        if not parent_path.is_absolute():
            parent_path = Path.cwd() / parent_path

        new_dir = parent_path / request.dir_name
        ensure_dir_with_owner(new_dir)

        logger.info(f"目录创建成功: {new_dir}")
        return CreateDirectoryResponse(success=True, path=str(new_dir), message=f"目录已创建: {request.dir_name}")
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"创建目录失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"创建目录失败: {e}")


# ==================== 文件删除（移动到临时目录/deleted） ====================


def _get_local_deleted_dir() -> Path:
    """获取本地删除文件备份目录：系统临时目录/deleted"""
    import tempfile
    deleted_dir = Path(tempfile.gettempdir()) / "deleted"
    ensure_dir_with_owner(deleted_dir)
    return deleted_dir


def _extract_stdout(response) -> str:
    """从 nest bash 响应中提取 stdout 文本"""
    data = response.data
    if isinstance(data, dict):
        return data.get("stdout", "") or ""
    return str(data) if data else ""


async def _nest_file_delete(nest_conn, file_path: str) -> dict:
    """通过nest将远程文件/目录移动到远程系统临时目录/deleted

    使用简单bash命令（mv/mkdir），兼容所有平台。
    """
    from peng.nest.protocol import NestRequest

    safe_path = file_path.replace("\\", "/")
    file_name = Path(safe_path).name
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 用带时间戳的文件名避免重名冲突，mv成功后echo输出最终路径
    backup_name = f"{file_name}.{ts}"
    is_windows = nest_conn.info.get("platform", "") in ("Windows", "windows")
    if is_windows:
        # Windows cmd.exe：move 不支持跨卷目录移动（D:→C:）
        # 用 move || robocopy 兜底，robocopy /E /MOVE 跨卷移动目录
        win_path = safe_path.replace("/", "\\")
        quoted_path = win_path.replace('"', '\\"')
        dst = f"%TEMP%\\deleted\\{backup_name}"
        cmd = (
            f'mkdir "%TEMP%\\deleted" 2>nul & '
            f'(move /Y "{quoted_path}" "{dst}" >nul 2>&1) || '
            f'(robocopy "{quoted_path}" "{dst}" /E /MOVE >nul) & '
            f'echo %TEMP%\\deleted\\{backup_name}'
        )
    else:
        # Linux/macOS/WSL：使用 shlex.quote（单引号）安全转义
        cmd = (
            f'DELETED_DIR="${{TMPDIR:-${{TEMP:-${{TMP:-/tmp}}}}}}/deleted" && '
            f"mkdir -p \"$DELETED_DIR\" && "
            f"mv {shlex.quote(safe_path)} \"$DELETED_DIR/{backup_name}\" && "
            f'echo "$DELETED_DIR/{backup_name}"'
        )

    request = NestRequest(
        type="bash",
        params={"command": cmd, "_skip_security": True},
    )
    response = await nest_conn.send_request(request, timeout=30.0)

    if response.error:
        raise Exception(response.error)

    backup_path = _extract_stdout(response)
    if not backup_path:
        _data = response.data if isinstance(response.data, dict) else {}
        _ec = _data.get("exit_code", -1)
        _se = (_data.get("stderr", "") or "").strip()
        _detail = f"exit_code={_ec}" + (f", stderr={_se}" if _se else "")
        raise Exception(f"删除操作未返回路径，可能文件不存在 ({_detail})")

    return {
        "success": True,
        "backup_path": backup_path.strip(),
        "original_path": safe_path,
    }


@router.post("/files/delete", response_model=FileDeleteResponse)
async def delete_file(request: FileDeleteRequest, nest: str | None = None) -> FileDeleteResponse:
    """删除文件/目录（移动到系统临时目录/deleted子目录作为备份）

    本地文件移动到本地系统临时目录/deleted
    远程nest文件移动到远程系统临时目录/deleted
    """
    # 🔧 data/ 目录始终在本地操作
    local_data = _resolve_local_data_path(request.path)
    if local_data:
        request = FileDeleteRequest(path=local_data)
        nest = None

    nest_conn = _get_selected_nest_conn(nest)
    if nest_conn:
        try:
            result = await _nest_file_delete(nest_conn, request.path)
            return FileDeleteResponse(
                success=True,
                backup_path=result["backup_path"],
                original_path=result["original_path"],
            )
        except Exception as e:
            logger.error(f"远程文件删除失败(nest={nest}): {e}")
            raise HTTPException(status_code=500, detail=f"远程文件删除失败: {e}")

    try:
        import shutil
        import tempfile

        file_path = Path(request.path)
        if not file_path.is_absolute():
            file_path = Path.cwd() / file_path

        if not file_path.exists():
            raise HTTPException(status_code=404, detail=f"文件不存在: {request.path}")

        deleted_dir = _get_local_deleted_dir()
        file_name = file_path.name

        # 构建备份路径，处理重名
        backup_path = deleted_dir / file_name
        if backup_path.exists():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = deleted_dir / f"{file_name}.{ts}"

        shutil.move(str(file_path), str(backup_path))
        chown_to_project_owner(backup_path)

        logger.info(f"文件已删除(备份): {request.path} -> {backup_path}")
        return FileDeleteResponse(
            success=True,
            backup_path=str(backup_path),
            original_path=str(file_path),
        )
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"删除文件失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"删除文件失败: {e}")


# ==================== 文件恢复（从临时目录/deleted移回） ====================


async def _nest_file_restore(nest_conn, backup_path: str, original_path: str) -> dict:
    """通过nest将远程备份文件/目录移回原路径

    使用简单bash命令（mkdir/mv），兼容所有平台。
    """
    from peng.nest.protocol import NestRequest

    safe_backup = backup_path.replace("\\", "/")
    safe_original = original_path.replace("\\", "/")
    parent_dir = str(Path(safe_original).parent)

    is_windows = nest_conn.info.get("platform", "") in ("Windows", "windows")
    if is_windows:
        # Windows cmd.exe：move 替代 mv，反斜杠路径 + 双引号
        win_parent = parent_dir.replace("/", "\\")
        win_backup = safe_backup.replace("/", "\\")
        win_original = safe_original.replace("/", "\\")
        quoted_parent = win_parent.replace('"', '\\"')
        quoted_backup = win_backup.replace('"', '\\"')
        quoted_original = win_original.replace('"', '\\"')
        cmd = f'mkdir "{quoted_parent}" 2>nul & (move /Y "{quoted_backup}" "{quoted_original}" >nul 2>&1) || (robocopy "{quoted_backup}" "{quoted_original}" /E /MOVE >nul) & echo OK'
    else:
        # Linux/macOS/WSL：使用 shlex.quote（单引号）安全转义
        cmd = f"mkdir -p {shlex.quote(parent_dir)} && mv {shlex.quote(safe_backup)} {shlex.quote(safe_original)} && echo 'OK'"

    request = NestRequest(
        type="bash",
        params={"command": cmd, "_skip_security": True},
    )
    response = await nest_conn.send_request(request, timeout=30.0)

    if response.error:
        raise Exception(response.error)

    stdout = _extract_stdout(response)
    if not stdout or "OK" not in stdout:
        raise Exception(f"恢复操作失败，可能备份文件不存在: {safe_backup}")

    return {
        "success": True,
        "restored_path": safe_original,
    }


@router.post("/files/restore", response_model=FileRestoreResponse)
async def restore_file(request: FileRestoreRequest, nest: str | None = None) -> FileRestoreResponse:
    """恢复已删除的文件/目录（从系统临时目录/deleted移回原路径）

    本地文件：backup_path在本地，移回original_path
    远程nest文件：backup_path在远程，移回original_path（通过nest）
    """
    # 🔧 data/ 目录始终在本地操作
    local_data = _resolve_local_data_path(request.original_path)
    if local_data:
        request = FileRestoreRequest(backup_path=request.backup_path, original_path=local_data)
        nest = None

    nest_conn = _get_selected_nest_conn(nest)
    if nest_conn:
        try:
            result = await _nest_file_restore(nest_conn, request.backup_path, request.original_path)
            return FileRestoreResponse(
                success=True,
                restored_path=result["restored_path"],
            )
        except Exception as e:
            logger.error(f"远程文件恢复失败(nest={nest}): {e}")
            raise HTTPException(status_code=500, detail=f"远程文件恢复失败: {e}")

    try:
        import shutil

        backup_path = Path(request.backup_path)
        original_path = Path(request.original_path)

        if not backup_path.exists():
            raise HTTPException(status_code=404, detail=f"备份文件不存在: {request.backup_path}")

        # 确保原父目录存在
        original_path.parent.mkdir(parents=True, exist_ok=True)

        shutil.move(str(backup_path), str(original_path))
        chown_to_project_owner(original_path)

        logger.info(f"文件已恢复: {request.backup_path} -> {original_path}")
        return FileRestoreResponse(
            success=True,
            restored_path=str(original_path),
        )
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"恢复文件失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"恢复文件失败: {e}")


# ==================== 工具调用回滚 ====================


@router.post("/tool-rollback", response_model=ToolRollbackResponse)
async def tool_rollback(request: ToolRollbackRequest) -> ToolRollbackResponse:
    """工具调用回滚端点

    根据rollback_meta中的信息，将文件回滚到工具调用前的状态。
    回滚前自动创建快照，成功后通过 OutputManager 写入回滚记录（含"撤销"超链接）。
    执行前如果事务正在运行，先暂停事务。
    """
    try:
        from peng.service.tool_backup import ToolBackupManager

        rollback_meta = request.rollback_meta
        if not rollback_meta:
            return ToolRollbackResponse(success=False, message="缺少回滚元数据")

        task_id = rollback_meta.get("task_id", 0)

        import asyncio

        # 执行前检查事务是否运行中，如果是则暂停
        was_running = False
        if task_id:
            try:
                from peng.task.manager import TaskManager
                tm = TaskManager()
                task = tm.db.get_task(task_id)
                if task and task.get("status") == "running":
                    logger.info(f"回滚前暂停运行中的事务#{task_id}")
                    await tm.pause_task(task_id, reason="用户执行回滚操作")
                    was_running = True
            except Exception as e:
                logger.warning(f"回滚前暂停事务失败: {e}")

        # 同步执行回滚（返回4元组：成功标志/消息/undo_meta/是否有实际变化）
        success, message, undo_meta, changed = await asyncio.to_thread(
            ToolBackupManager.rollback, rollback_meta
        )

        # 🔧 无实际变化：不写回滚记录到output_manager（无回滚链接/撤销链接），
        # 避免无意义记录污染输出；若回滚前暂停了事务则自动恢复运行
        if success and not changed:
            logger.info(f"工具调用回滚无实际变化，跳过记录: {message}")
            if was_running:
                try:
                    resume_ok = await tm.resume_task(
                        task_id, reason="回滚无实际变化，自动恢复运行"
                    )
                    if resume_ok:
                        message += "（事务已自动恢复运行）"
                except Exception as e:
                    logger.warning(f"回滚无变化后恢复事务失败: {e}")
            return ToolRollbackResponse(
                success=True, message=message, undo_display=None
            )

        undo_display = None
        if success:
            logger.info(f"工具调用回滚成功: {message}")

            # 通过 OutputManager 写入回滚记录到 xterm
            if task_id:
                try:
                    from peng.task.output_manager import get_output_manager
                    record = ToolBackupManager.generate_rollback_record(
                        rollback_meta, undo_meta, message,
                        was_running=was_running,
                    )
                    get_output_manager().write_output(task_id, "rollback", record + "\n")
                except Exception as e:
                    logger.warning(f"写入回滚记录到OutputManager失败: {e}")

            if undo_meta:
                undo_display = f"✅ {message}（可点击撤销）"
            else:
                undo_display = f"✅ {message}"

            if was_running:
                undo_display += "（事务已暂停）"
        else:
            logger.warning(f"工具调用回滚失败: {message}")

        return ToolRollbackResponse(
            success=success, message=message, undo_display=undo_display
        )

    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"工具调用回滚异常: {e}\n{tb}")
        return ToolRollbackResponse(success=False, message=f"回滚异常: {e}")


@router.get("/rollback-meta/{meta_id}")
async def get_rollback_meta(meta_id: str):
    """获取回滚元数据（持久化回滚ID "<task_id>-<seq>" 格式）

    🔧 回滚ID方案：新格式 rollback:id:<task_id>-<seq> 的完整 meta 持久化在
    data/rollback_ids/task-<id>/<seq>.json，优先内存热缓存，其次读文件。
    旧格式（内存池uuid短ID）仅剩内存池可用，重启后不存在。
    """
    from peng.service.tool_backup import (
        load_rollback_meta_persistent,
        get_rollback_meta_by_id,
    )

    rollback_meta = load_rollback_meta_persistent(meta_id)
    if rollback_meta is None:
        # 兼容旧内存池 uuid 短ID（服务未重启时有效）
        rollback_meta = get_rollback_meta_by_id(meta_id)
    if rollback_meta is None:
        return {"success": False, "message": "回滚信息不存在或已过期，请重新运行事务以生成新的回滚链接"}
    return {"success": True, "rollback_meta": rollback_meta}


@router.post("/tool-rollback-preview")
async def tool_rollback_preview(request: Request):
    """回滚diff预览端点（只读，不暂停事务）

    动态计算回滚将导致的所有文件变更（git diff形式），
    供前端确认对话框在用户确认前展示。
    同时计算 checklist 变化（对比当前 checklist 与快照备份的 checklist.md），
    独立计算：任一失败不影响另一个。
    注意：预览不暂停事务（只有用户确认回滚时 /api/tool-rollback 才暂停），
    diff为预览时刻的快照；确认回滚时 rollback() 会重新动态检测实际变更。
    """
    import asyncio

    try:
        from peng.service.tool_backup import HistoryBackupManager, ToolBackupManager

        body = await request.json()
        rollback_meta = body.get("rollback_meta") if isinstance(body, dict) else None
        if not rollback_meta:
            return {"success": False, "diff": "", "changed_files": [], "truncated": False,
                    "message": "缺少回滚元数据"}

        file_task = asyncio.to_thread(
            ToolBackupManager.preview_rollback_diff, rollback_meta
        )

        # checklist diff：仅快照包含 history_commit 时可计算（迭代/undo快照），
        # 工具级快照无 checklist 备份，返回 available=False 前端不显示区块
        task_id = rollback_meta.get("task_id", 0)
        history_commit = rollback_meta.get("history_commit", "")
        timestamp = (rollback_meta.get("timestamp") or "").replace("T", " ")
        iteration = rollback_meta.get("iteration")
        if iteration:
            label = f"迭代#{iteration} 快照[{timestamp}]"
        elif (rollback_meta.get("tool_name") or "").startswith("撤销:"):
            label = f"撤销前快照[{timestamp}]"
        else:
            label = f"快照[{timestamp}]"

        cl_task = asyncio.to_thread(
            HistoryBackupManager.preview_checklist_diff,
            task_id, history_commit, label,
        )

        result, checklist_result = await asyncio.gather(file_task, cl_task)
        result["checklist_diff"] = checklist_result
        return result
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"回滚diff预览异常: {e}\n{tb}")
        return {"success": False, "diff": "", "changed_files": [], "truncated": False,
                "message": f"预览异常: {e}"}


@router.post("/editor/auto-save", response_model=EditorAutoSaveResponse)
async def editor_auto_save(request: EditorAutoSaveRequest) -> EditorAutoSaveResponse:
    """编辑器自动保存"""
    try:
        ensure_dir_with_owner(EDITOR_TEMP_DIR)
        temp_file_name = f"{request.tabId}.tmp"
        temp_file_path = EDITOR_TEMP_DIR / temp_file_name

        save_data = {
            "content": request.content,
            "editType": request.editType,
            "rootMsgId": request.rootMsgId,
            "isContentEditor": request.isContentEditor,
            "editId": request.editId,
        }
        temp_file_path.write_text(json.dumps(save_data, ensure_ascii=False), encoding="utf-8")
        return EditorAutoSaveResponse(success=True, tabId=request.tabId)
    except Exception as e:
        logger.error(f"编辑器自动保存失败: {e}")
        return EditorAutoSaveResponse(success=False, tabId=request.tabId)


@router.get("/editor/auto-save", response_model=EditorAutoSaveReadResponse)
async def get_editor_auto_save(tabId: str = Query(...)) -> EditorAutoSaveReadResponse:
    """读取编辑器自动保存的临时文件"""
    try:
        temp_file_name = f"{tabId}.tmp"
        temp_file_path = EDITOR_TEMP_DIR / temp_file_name

        if not temp_file_path.exists():
            return EditorAutoSaveReadResponse(success=False, content=None, tabId=tabId)

        content = temp_file_path.read_text(encoding="utf-8")
        try:
            data = json.loads(content)
            return EditorAutoSaveReadResponse(
                success=True, content=data.get("content"), tabId=tabId,
                editType=data.get("editType"), rootMsgId=data.get("rootMsgId"),
                isContentEditor=data.get("isContentEditor"), editId=data.get("editId"),
            )
        except json.JSONDecodeError:
            return EditorAutoSaveReadResponse(success=True, content=content, tabId=tabId)
    except Exception as e:
        logger.error(f"读取编辑器临时文件失败: {e}")
        return EditorAutoSaveReadResponse(success=False, content=None, tabId=tabId)


@router.delete("/editor/auto-save", response_model=EditorAutoSaveResponse)
async def delete_editor_auto_save(tabId: str = Query(...)) -> EditorAutoSaveResponse:
    """删除编辑器自动保存的临时文件"""
    try:
        temp_file_name = f"{tabId}.tmp"
        temp_file_path = EDITOR_TEMP_DIR / temp_file_name
        if temp_file_path.exists():
            temp_file_path.unlink()
        return EditorAutoSaveResponse(success=True, tabId=tabId)
    except Exception as e:
        logger.error(f"删除编辑器临时文件失败: {e}")
        return EditorAutoSaveResponse(success=False, tabId=tabId)


# ==================== 编辑器KV存储（大数据走服务端，不占localStorage） ====================

@router.post("/editor/kv/{key}")
async def editor_kv_save(key: str, request: Request):
    """保存编辑器大数据到服务端临时文件（替代localStorage存储）"""
    try:
        safe_key = "".join(c for c in key if c.isalnum() or c in "-_")
        if not safe_key:
            return {"success": False, "error": "无效的key"}
        ensure_dir_with_owner(EDITOR_TEMP_DIR)
        kv_file = EDITOR_TEMP_DIR / f"kv_{safe_key}.json"
        body = await request.body()
        kv_file.write_bytes(body)
        return {"success": True, "key": safe_key, "size": len(body)}
    except Exception as e:
        logger.error(f"编辑器KV保存失败: {e}")
        return {"success": False, "error": str(e)}


@router.get("/editor/kv/{key}")
async def editor_kv_load(key: str):
    """从服务端临时文件加载编辑器大数据"""
    try:
        safe_key = "".join(c for c in key if c.isalnum() or c in "-_")
        if not safe_key:
            return {"success": False, "error": "无效的key"}
        kv_file = EDITOR_TEMP_DIR / f"kv_{safe_key}.json"
        if not kv_file.exists():
            return {"success": False, "data": None}
        data = kv_file.read_bytes()
        return {"success": True, "data": json.loads(data)}
    except Exception as e:
        logger.error(f"编辑器KV加载失败: {e}")
        return {"success": False, "data": None}


@router.delete("/editor/kv/{key}")
async def editor_kv_delete(key: str):
    """删除服务端编辑器KV数据"""
    try:
        safe_key = "".join(c for c in key if c.isalnum() or c in "-_")
        kv_file = EDITOR_TEMP_DIR / f"kv_{safe_key}.json"
        if kv_file.exists():
            kv_file.unlink()
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/editor/clear-all-temp")
async def clear_all_editor_temp():
    """清除所有编辑器自动保存的临时文件（用于 /reset-webui 命令）"""
    try:
        deleted_count = 0
        errors = []
        if EDITOR_TEMP_DIR.exists():
            for f in EDITOR_TEMP_DIR.glob("*.tmp"):
                try:
                    f.unlink()
                    deleted_count += 1
                except Exception as e:
                    errors.append(f"{f.name}: {e}")
            # 同时清除KV存储文件
            for f in EDITOR_TEMP_DIR.glob("kv_*.json"):
                try:
                    f.unlink()
                    deleted_count += 1
                except Exception as e:
                    errors.append(f"{f.name}: {e}")
        logger.info(f"编辑器临时文件已清除: {deleted_count}个文件")
        return {"success": True, "deleted_count": deleted_count, "errors": errors}
    except Exception as e:
        logger.error(f"清除编辑器临时文件失败: {e}")
        return {"success": False, "error": str(e)}

"""
工作目录和配置路由模块

提供工作目录验证/设置、WebUI配置获取/保存、项目结构等功能。

路由前缀: /api (由 server.py 注册)
- /validate-workdir, /set-workdir, /get-workdir
- /config, /config/ui-settings, /output-source-filters
- /project-structure
"""

from __future__ import annotations

import json
import logging
import os
import time
import traceback
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from peng.common.paths import set_working_directory, get_project_root
from peng.config import clear_config_cache, get_project_root as _get_project_root
from peng.webui.routes._common import (
    AGENT_NAME,
    DEFAULT_OUTPUT_HISTORY_LIMIT,
    _PROJECT_STRUCTURE_CACHE_TTL,
    _get_selected_nest_conn,
    _get_nest_project_root,
    _project_structure_cache,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# 全局存储当前工作目录状态（被 server.py 通过 api._current_workdir 引用）
_current_workdir: str | None = None


# ==================== Pydantic 模型 ====================


class ValidateWorkdirResponse(BaseModel):
    valid: bool
    path: str | None = None
    error: str | None = None


class SetWorkdirResponse(BaseModel):
    success: bool
    path: str | None = None
    error: str | None = None


# ==================== 配置保存辅助函数 ====================


def _get_webui_json_path() -> Path:
    """获取 webui.json 的写入路径（data 目录覆盖文件）

    写入时优先写入 data/webui.json（运行时覆盖层），不存在时自动创建。
    """
    project_root = get_project_root()
    return project_root / "data" / "webui.json"


def _load_webui_json() -> dict:
    """读取 webui.json（合并 config 基础配置和 data 覆盖配置）"""
    from peng.config.loader import _load_config_with_overlay

    project_root = get_project_root()
    return _load_config_with_overlay(
        project_root / "config" / "webui.json",
        project_root / "data" / "webui.json",
    )


def _save_webui_json(config: dict) -> None:
    """写入 webui.json 到 data 目录（仅覆盖层）"""
    from peng.common.paths import ensure_dir_with_owner

    data_path = _get_webui_json_path()
    ensure_dir_with_owner(data_path.parent)
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    clear_config_cache(data_path)


def _save_workdir_to_config(workdir: str, config_path: Path | None = None) -> bool:
    """保存工作目录到配置文件（写入 data 目录覆盖层）"""
    from peng.config import clear_config_cache, get_project_root

    try:
        config = _load_webui_json()
        if "webui" not in config:
            config["webui"] = {}
        config["webui"]["workdir"] = workdir
        _save_webui_json(config)

        logger.info(f"工作目录已保存到配置: {workdir}")
        return True
    except Exception as e:
        logger.error(f"保存工作目录到配置失败: {e}\n{traceback.format_exc()}")
        return False
def _save_nest_to_config(nest: str, config_path: Path | None = None) -> bool:
    """保存nest选择到配置文件（写入 data 目录覆盖层）"""
    from peng.config import clear_config_cache, get_project_root

    try:
        config = _load_webui_json()
        if "webui" not in config:
            config["webui"] = {}
        config["webui"]["nest"] = nest
        _save_webui_json(config)

        logger.info(f"nest已保存到配置: {nest}")
        return True
    except Exception as e:
        logger.error(f"保存nest到配置失败: {e}\n{traceback.format_exc()}")
        return False
def _save_output_source_filters(filters: dict, config_path: Path | None = None) -> bool:
    """保存输出来源过滤器状态到配置文件（写入 data 目录覆盖层）"""
    from peng.config import clear_config_cache, get_project_root

    try:
        config = _load_webui_json()

        if "webui" not in config:
            config["webui"] = {}

        existing_filters = config["webui"].get("output_source_filters", {})
        for source, enabled in filters.items():
            if source in existing_filters:
                existing_filters[source]["enabled"] = bool(enabled)
            else:
                existing_filters[source] = {"name": source, "enabled": bool(enabled)}

        config["webui"]["output_source_filters"] = existing_filters
        _save_webui_json(config)

        logger.info(f"输出来源过滤器状态已保存到配置: {filters}")
        return True
    except Exception as e:
        logger.error(f"保存过滤器状态到配置失败: {e}\n{traceback.format_exc()}")
        return False
def _save_ui_settings(settings: dict, config_path: Path | None = None) -> bool:
    """保存UI设置到配置文件（写入 data 目录覆盖层）"""
    from peng.config import clear_config_cache, get_project_root

    try:
        config = _load_webui_json()

        if "webui" not in config:
            config["webui"] = {}

        if "font" in settings:
            font = settings["font"]
            if not isinstance(config["webui"].get("font"), dict):
                config["webui"]["font"] = {}
            for key in ("family", "size", "size_xterm", "size_editor"):
                if key in font:
                    config["webui"]["font"][key] = font[key]

        for key in ("sidebar_left_width", "sidebar_right_width"):
            if key in settings:
                config["webui"][key] = int(settings[key])

        for key in ("selected_message_id", "selected_task_id", "selected_branch_message_id"):
            if key in settings:
                val = settings[key]
                config["webui"][key] = val if val is not None else None

        # 🔧 Task#228：顶置列表
        for key in ("topped_message_ids", "topped_task_ids"):
            if key in settings:
                config["webui"][key] = settings[key]

        _save_webui_json(config)

        logger.info(f"UI设置已保存到配置: {settings}")
        return True
    except Exception as e:
        logger.error(f"保存UI设置到配置失败: {e}\n{traceback.format_exc()}")
        return False


async def _get_remote_workdir(nest_conn) -> str | None:
    """从远程nest的配置中读取workdir"""
    import json as json_module
    from peng.nest.protocol import NestRequest

    project_root = _get_nest_project_root(nest_conn)
    if not project_root:
        logger.warning("远程nest未提供project_root，无法读取workdir配置")
        return None

    config_path = f"{project_root}/config/webui.json"
    request = NestRequest(type="read", params={"path": config_path, "_skip_security": True})
    try:
        response = await nest_conn.send_request(request, timeout=10.0)
        if response.error:
            return None

        data = response.data
        if isinstance(data, dict) and data.get("type") == "file":
            lines = data.get("lines", [])
            content_lines = []
            for line in lines:
                if ": " in line:
                    content_lines.append(line.split(": ", 1)[1])
                else:
                    content_lines.append(line)
            content = "\n".join(content_lines)
            config_data = json_module.loads(content)
            return config_data.get("webui", {}).get("workdir", "~")
    except Exception as e:
        logger.warning(f"读取远程nest workdir异常: {e}")
    return None


def _remove_topped_message(msg_id: int) -> None:
    """从顶置列表中移除指定消息ID"""
    _remove_topped_id("topped_message_ids", msg_id)


def _remove_topped_task(task_id: int) -> None:
    """从顶置列表中移除指定事务ID"""
    _remove_topped_id("topped_task_ids", task_id)


def _remove_topped_id(key: str, item_id: int) -> None:
    """从webui配置的指定顶置列表中移除一个ID（写入 data 目录覆盖层）"""
    try:
        config = _load_webui_json()

        topped_ids: list = config.get("webui", {}).get(key, [])
        if item_id in topped_ids:
            topped_ids.remove(item_id)
            if "webui" not in config:
                config["webui"] = {}
            config["webui"][key] = topped_ids
            _save_webui_json(config)
    except Exception as e:
        logger.warning(f"移除顶置ID失败: {e}")


@router.get("/validate-workdir", response_model=ValidateWorkdirResponse)
async def validate_workdir(path: str) -> ValidateWorkdirResponse:
    """验证工作目录是否有效"""
    try:
        expanded_path = Path(path).expanduser()
        resolved_path = expanded_path.resolve()
        if resolved_path.is_dir():
            return ValidateWorkdirResponse(valid=True, path=str(resolved_path))
        else:
            return ValidateWorkdirResponse(valid=False, error="路径不存在或不是目录")
    except Exception as e:
        return ValidateWorkdirResponse(valid=False, error=str(e))


@router.post("/set-workdir", response_model=SetWorkdirResponse)
async def set_workdir(path: str, nest: str | None = None, save_only: bool = False) -> SetWorkdirResponse:
    """设置工作目录"""
    global _current_workdir

    if save_only:
        _save_workdir_to_config(path)
        _current_workdir = path
        return SetWorkdirResponse(success=True, path=path)

    nest_conn = _get_selected_nest_conn(nest)
    if nest_conn:
        safe_path = path.replace("\\", "/")
        from peng.nest.protocol import NestRequest
        original_name = getattr(nest_conn, 'name', nest or '')
        set_request = NestRequest(
            type="set_workdir",
            params={"workdir": safe_path, "nest_name": original_name},
        )
        try:
            response = await nest_conn.send_request(set_request, timeout=10.0)
            if response.error:
                return SetWorkdirResponse(success=False, error=f"远程设置失败: {response.error}")
        except Exception as e:
            return SetWorkdirResponse(success=False, error=f"远程设置异常: {e}")

        if isinstance(nest_conn.info, dict):
            nest_conn.info["workdir"] = safe_path

        return SetWorkdirResponse(success=True, path=safe_path)

    try:
        expanded_path = Path(path).expanduser()
        resolved_path = expanded_path.resolve()
        success = set_working_directory(str(resolved_path))
        if not success:
            return SetWorkdirResponse(success=False, error="路径无效或设置失败")
        _current_workdir = str(resolved_path)
        return SetWorkdirResponse(success=True, path=str(resolved_path))
    except Exception as e:
        logger.error(f"切换工作目录失败: {path}, 错误: {e}\n{traceback.format_exc()}")
        return SetWorkdirResponse(success=False, error=str(e))


@router.get("/get-workdir")
async def get_workdir(nest: str | None = None) -> dict[str, str | None]:
    """获取当前工作目录"""
    nest_conn = _get_selected_nest_conn(nest)
    if nest_conn:
        remote_workdir = await _get_remote_workdir(nest_conn)
        return {"path": remote_workdir or "/"}
    return {"path": _current_workdir or os.getcwd()}


@router.post("/config/ui-settings")
async def save_ui_settings(request: Request) -> dict[str, bool | str]:
    """保存UI设置到 config/webui.json"""
    try:
        body = await request.json()
        if not isinstance(body, dict):
            return {"success": False, "error": "请求体必须是JSON对象"}
        success = _save_ui_settings(body)
        return {"success": True} if success else {"success": False, "error": "保存失败"}
    except Exception as e:
        logger.error(f"保存UI设置API失败: {e}\n{traceback.format_exc()}")
        return {"success": False, "error": str(e)}


@router.post("/output-source-filters")
async def save_output_source_filters(request: Request) -> dict[str, bool | str]:
    """保存输出来源过滤器状态"""
    try:
        body = await request.json()
        if not isinstance(body, dict):
            return {"success": False, "error": "请求体必须是JSON对象"}
        success = _save_output_source_filters(body)
        return {"success": True} if success else {"success": False, "error": "保存失败"}
    except Exception as e:
        logger.error(f"保存输出过滤器状态API失败: {e}\n{traceback.format_exc()}")
        return {"success": False, "error": str(e)}


@router.get("/config")
async def get_config() -> dict[str, Any]:
    """获取 Web UI 配置"""
    try:
        from peng.config.loader import load_config as load_peng_config
        from peng.config import get_agent_name, get_master_name

        config = load_peng_config()
        config_dict = config.model_dump()
        webui_config = config_dict.get("webui", {})
        webui_themes = config_dict.get("webui_themes", {})

        current_workdir = webui_config.get("workdir") or _current_workdir or os.getcwd()

        return {
            "enabled": webui_config.get("enabled", True),
            "host": webui_config.get("host", "0.0.0.0"),
            "port": webui_config.get("port", 6688),
            "theme": webui_config.get("theme", "dark"),
            "messages_refresh_interval": webui_themes.get("messages_refresh_interval", webui_config.get("messages_refresh_interval", 10000)),
            "tasks_refresh_interval": webui_themes.get("tasks_refresh_interval", webui_config.get("tasks_refresh_interval", 10000)),
            "output_source_filters": webui_config.get("output_source_filters", {}),
            "themes": webui_themes,
            "workdir": current_workdir,
            "xterm": webui_config.get("xterm", {"scrollback": DEFAULT_OUTPUT_HISTORY_LIMIT}),
            "content_trunc_lines": webui_config.get("content_trunc_lines", 10),
            "content_trunc_chars": webui_config.get("content_trunc_chars", 300),
            "font": webui_config.get("font", {"family": "JetBrains Mono, Fira Code, Consolas, Monaco, monospace", "size": 14, "size_xterm": 14, "size_editor": 14}),
            "sidebar_left_width": webui_config.get("sidebar_left_width", 320),
            "sidebar_right_width": webui_config.get("sidebar_right_width", 220),
            "agent_name": config_dict.get("agent_name", "鹏🐣"),
            "master_name": config_dict.get("master_name", "master"),
            "thinking_mode": config_dict.get("llm", {}).get("thinking_mode", "auto"),
            "thinking_budget": config_dict.get("llm", {}).get("thinking_budget", 0),
            "platform": "windows" if os.name == "nt" else "linux",
            "project_root": str(get_project_root()),
            "nest": config_dict.get("nest", {}),
            "selected_message_id": webui_config.get("selected_message_id"),
            "selected_task_id": webui_config.get("selected_task_id"),
            "selected_branch_message_id": webui_config.get("selected_branch_message_id"),
            "topped_message_ids": webui_config.get("topped_message_ids", []),
            "topped_task_ids": webui_config.get("topped_task_ids", []),
            # 🔧 Task#262：大文件加载配置
            "large_file_threshold": webui_config.get("large_file_threshold", 3 * 1024 * 1024),
            "large_file_parallel_threshold": webui_config.get("large_file_parallel_threshold", 6 * 1024 * 1024),
            "large_file_parallel_concurrency": webui_config.get("large_file_parallel_concurrency", 4),
            "large_file_chunk_size": webui_config.get("large_file_chunk_size", 1024 * 1024),
            # 🔧 输入历史记录最大条数
            "input_history_limit": webui_config.get("input_history_limit", 500),
            # 🔧 Task#418：编辑器配置（emoji文字模式、自动创建空文档等）
            "editor": webui_config.get("editor", {"emoji_text_mode": False, "auto_create_empty_doc": False}),
        }
    except Exception as e:
        tb = traceback.format_exc()
        logger.warning(f"获取配置失败: {e}\n{tb}")
        return {
            "enabled": True, "host": "0.0.0.0", "port": 6688, "theme": "dark",
            "messages_refresh_interval": 10000, "tasks_refresh_interval": 10000,
            "themes": {}, "workdir": os.getcwd(),
            "xterm": {"scrollback": DEFAULT_OUTPUT_HISTORY_LIMIT},
        }


def _is_gitignored(name: str, is_dir: bool) -> bool:
    """简单判断目录/文件是否被 .gitignore 忽略"""
    ignored_dirs = {
        "__pycache__", ".git", ".venv", "venv", "node_modules", ".mypy_cache",
        ".pytest_cache", ".ruff_cache", ".eggs", ".idea", ".vscode", "dist",
        "build", ".worktrees", ".hive", "htmlcov", ".coverage", ".claude", ".opencode",
    }
    ignored_file_patterns = {".pyc", ".pyo", ".bak", ".bak2", ".log", ".egg-info"}

    if is_dir:
        return name in ignored_dirs
    else:
        for ext in ignored_file_patterns:
            if name.endswith(ext):
                return True
        return False


@router.get("/project-structure")
async def get_project_structure() -> dict[str, Any]:
    """获取项目目录结构"""
    try:
        project_root = get_project_root()
        current_workdir = str(project_root)

        cache = _project_structure_cache
        now = time.time()
        if cache["workdir"] == current_workdir and cache["dirs"] and now - cache["scan_time"] < _PROJECT_STRUCTURE_CACHE_TTL:
            return {"projectDirs": cache["dirs"], "projectFiles": cache["files"]}

        project_dirs = []
        project_files = []

        for item in project_root.iterdir():
            name = item.name
            if name.startswith("."):
                continue
            if item.is_dir():
                if not _is_gitignored(name, is_dir=True):
                    project_dirs.append(name)
            elif item.is_file():
                if not _is_gitignored(name, is_dir=False):
                    project_files.append(name)

        max_total_dirs = 500
        dirs_to_scan = list(project_dirs)

        while dirs_to_scan and len(project_dirs) < max_total_dirs:
            dir_name = dirs_to_scan.pop(0)
            dir_path = project_root / dir_name
            if not dir_path.is_dir():
                continue
            try:
                for item in dir_path.iterdir():
                    if len(project_dirs) >= max_total_dirs:
                        break
                    name = item.name
                    if name.startswith("."):
                        continue
                    if item.is_dir():
                        if not _is_gitignored(name, is_dir=True):
                            relative_path = f"{dir_name}/{name}"
                            project_dirs.append(relative_path)
                            dirs_to_scan.append(relative_path)
            except (PermissionError, OSError):
                continue

        sorted_dirs = sorted(project_dirs)
        sorted_files = sorted(project_files)
        cache["workdir"] = current_workdir
        cache["dirs"] = sorted_dirs
        cache["files"] = sorted_files
        cache["scan_time"] = now

        return {"projectDirs": sorted_dirs, "projectFiles": sorted_files}
    except Exception as e:
        logger.error(f"获取项目结构失败: {e}")
        return {"projectDirs": [], "projectFiles": []}

"""
系统命令和调试路由模块

提供内置命令执行（/cmd, /msg, /task等）、系统状态、WebUI调试等功能。

路由前缀: /api (由 server.py 注册)
- /command, /status
- /webui/debug-webui, /webui/client-state, /webui/debug-exec, /webui/debug-eval
- /webui/debug-nest-exec, /webui/debug-nest-eval, /webui/debug, /webui/eval-response
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from peng.common.paths import get_temp_directory
from peng.message.manager import MessageManager
from peng.task.manager import TaskManager
from peng.webui.routes._common import (
    AGENT_NAME,
    CommandRequest,
    _build_python_cmd,
    _get_color_env,
    _add_color_flag,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ==================== 方案A: Pending Eval缓存（刷新重连支持） ====================

# 缓存已超时的eval请求，等浏览器重连后重发
# 格式: {eval_id: {"code": str, "created_at": float, "retry_count": int}}
_pending_eval_cache: dict[str, dict] = {}
_PENDING_EVAL_MAX_AGE = 120  # 缓存最大存活时间(秒)
_PENDING_EVAL_MAX_RETRY = 3  # 最大重试次数


def _cache_pending_eval(eval_id: str, code: str):
    """缓存一个超时的eval请求，等待浏览器重连后重发"""
    # 清理过期的缓存
    now = time.time()
    expired = [eid for eid, data in _pending_eval_cache.items()
               if now - data["created_at"] > _PENDING_EVAL_MAX_AGE]
    for eid in expired:
        del _pending_eval_cache[eid]

    _pending_eval_cache[eval_id] = {
        "code": code,
        "created_at": now,
        "retry_count": 0,
    }
    logger.info(f"[debug-reconnect] 已缓存eval请求 {eval_id}, 当前缓存数: {len(_pending_eval_cache)}")


def _clear_cached_eval(eval_id: str):
    """清除已成功执行的缓存eval"""
    if eval_id in _pending_eval_cache:
        del _pending_eval_cache[eval_id]


def _get_pending_evals() -> list[dict]:
    """获取所有待重试的eval请求（不超过最大重试次数）"""
    now = time.time()
    result = []
    expired = []
    for eval_id, data in _pending_eval_cache.items():
        if now - data["created_at"] > _PENDING_EVAL_MAX_AGE:
            expired.append(eval_id)
            continue
        if data["retry_count"] >= _PENDING_EVAL_MAX_RETRY:
            expired.append(eval_id)
            continue
        data["retry_count"] += 1
        result.append({"eval_id": eval_id, "code": data["code"]})
    for eid in expired:
        del _pending_eval_cache[eid]
    return result


def _write_eval_result_to_client_state(client_id: str, eval_id: str, result: Any):
    """将eval结果写入client-state文件，即使pending future已超时也能被读取"""
    try:
        client_state_dir = Path(get_temp_directory()) / "webui_client_states"
        client_state_dir.mkdir(parents=True, exist_ok=True)
        safe_id = "".join(c for c in str(client_id) if c.isalnum() or c in "-_")
        state_file = client_state_dir / f"client_{safe_id}.json"
        if state_file.exists():
            with open(state_file, 'r', encoding='utf-8') as f:
                cs = json.load(f)
        else:
            cs = {"client_id": client_id}
        if "_delayed_eval_results" not in cs:
            cs["_delayed_eval_results"] = {}
        cs["_delayed_eval_results"][eval_id] = {
            "result": _serialize(result),
            "received_at": datetime.now().isoformat(),
        }
        with open(state_file, 'w', encoding='utf-8') as f:
            json.dump(cs, f, ensure_ascii=False, indent=2)
        logger.info(f"[debug-reconnect] eval结果 {eval_id} 已写入client-state")
    except Exception as e:
        logger.warning(f"[debug-reconnect] 写入eval结果到client-state失败: {e}")


# ==================== 路由端点 ====================


@router.get("/status")
async def get_status() -> dict[str, Any]:
    """获取系统状态"""
    from ..websocket import manager as ws_manager

    msg_manager = MessageManager()
    task_manager = TaskManager()

    status = {
        "status": "running",
        "managers": {"message": msg_manager is not None, "task": task_manager is not None},
        "connections": len(ws_manager.active_connections),
    }

    if msg_manager:
        try:
            status["messages"] = msg_manager.count_by_status()
        except Exception as e:
            logger.error(f"❌ 获取消息统计失败: {e}\n{traceback.format_exc()}")
            status["messages"] = {}

    if task_manager:
        try:
            tasks = task_manager.list_tasks(exclude_fields=["context", "checklist", "report", "tool_calls_log", "tool_results_log"])
            status["tasks"] = {
                "total": len(tasks),
                "running": len([t for t in tasks if t.get("status") == "running"]),
                "paused": len([t for t in tasks if t.get("status") == "paused"]),
                "completed": len([t for t in tasks if t.get("status") == "completed"]),
            }
        except Exception as e:
            logger.error(f"❌ 获取事务统计失败: {e}\n{traceback.format_exc()}")
            status["tasks"] = {}

    return status


@router.post("/command")
async def execute_command(request: CommandRequest) -> dict[str, Any]:
    """执行内置命令（如 /model, /msg, /task, /cmd, /cd, /clear-all-data）"""
    cmd = request.command.strip()

    try:
        # /model 命令
        if cmd == "/model" or cmd.startswith("/model "):
            try:
                from peng.config.loader import load_config
                config = load_config()
                provider_data = config.llm.get_provider()
                model_name = provider_data.get("model", "未设置") if provider_data else "未设置"
                result_text = f"当前模型：{model_name} (provider: {config.llm.current_provider})\n大模型已采用自动轮选机制，无需手动切换。"
                return {"success": True, "command": "/model", "output": result_text}
            except Exception as e:
                return {"success": False, "command": "/model", "output": f"获取模型信息失败: {e}"}

        # /msg 命令
        if cmd == "/msg" or cmd.startswith("/msg "):
            project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
            msg_bin = project_root / "bin" / "msg"
            parts = cmd.split(maxsplit=2)
            subcmd = parts[1].strip() if len(parts) > 1 else ""
            args = parts[2].strip() if len(parts) > 2 else ""
            msg_content_subcmds = {"send", "reply", "content", "title", "search"}
            cmd_args = ["--local", subcmd]

            if subcmd in msg_content_subcmds and args:
                if subcmd in ("reply", "content", "title"):
                    arg_parts = args.split(maxsplit=1)
                    if arg_parts and arg_parts[0].isdigit():
                        cmd_args.append(arg_parts[0])
                        content = arg_parts[1] if len(arg_parts) > 1 else ""
                    else:
                        content = args
                else:
                    content = args

                if content:
                    from datetime import datetime as _dt
                    _ts = _dt.now().strftime("%Y%m%d_%H%M%S_%f")
                    temp_path = str(get_temp_directory() / f"peng-msg-{_ts}.txt")
                    with open(temp_path, "w", encoding="utf-8") as _tf:
                        _tf.write(content)
                    cmd_args.extend(["--file", temp_path])
            elif args:
                cmd_args.extend(args.split())

            cmd_list = _build_python_cmd(msg_bin, cmd_args)
            _msg_env = os.environ.copy()
            _msg_env.update(_get_color_env())
            try:
                result = subprocess.run(cmd_list, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, env=_msg_env)
                output = result.stdout or ""
                if result.stderr:
                    output += "\n" + result.stderr
                return {"success": result.returncode == 0, "command": "/msg", "output": output.strip() or "命令执行完成，无输出"}
            except subprocess.TimeoutExpired:
                return {"success": False, "command": "/msg", "output": "❌ 命令执行超时"}
            except Exception as e:
                return {"success": False, "command": "/msg", "output": f"❌ 执行失败: {e}"}

        # /task 命令
        if cmd == "/task" or cmd.startswith("/task "):
            project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
            task_bin = project_root / "bin" / "task"
            parts = cmd.split(maxsplit=2)
            subcmd = parts[1].strip() if len(parts) > 1 else ""
            args = parts[2].strip() if len(parts) > 2 else ""
            task_content_subcmds = {"info", "content", "title", "prompt", "description", "subtask", "result", "metadata", "workdir", "branch", "context"}
            cmd_args = ["--local", subcmd]

            if subcmd in task_content_subcmds and args:
                arg_parts = args.split(maxsplit=1)
                if arg_parts and arg_parts[0].isdigit():
                    cmd_args.append(arg_parts[0])
                    content = arg_parts[1] if len(arg_parts) > 1 else ""
                else:
                    content = args

                if content:
                    from datetime import datetime as _dt
                    _ts = _dt.now().strftime("%Y%m%d_%H%M%S_%f")
                    temp_path = str(get_temp_directory() / f"peng-task-{_ts}.txt")
                    with open(temp_path, "w", encoding="utf-8") as _tf:
                        _tf.write(content)
                    cmd_args.extend(["--file", temp_path])
            elif args:
                cmd_args.extend(args.split())

            cmd_list = _build_python_cmd(task_bin, cmd_args)
            _task_env = os.environ.copy()
            _task_env.update(_get_color_env())
            try:
                result = subprocess.run(cmd_list, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, env=_task_env)
                output = result.stdout or ""
                if result.stderr:
                    output += "\n" + result.stderr
                return {"success": result.returncode == 0, "command": "/task", "output": output.strip() or "命令执行完成，无输出"}
            except subprocess.TimeoutExpired:
                return {"success": False, "command": "/task", "output": "❌ 命令执行超时"}
            except Exception as e:
                return {"success": False, "command": "/task", "output": f"❌ 执行失败: {e}"}

        # /cmd 命令
        if cmd == "/cmd" or cmd.startswith("/cmd "):
            cmd_str = cmd[5:].strip()
            if not cmd_str:
                return {"success": False, "command": "/cmd", "output": "用法: /cmd <command> [args...]"}

            if sys.platform == "win32":
                cmd_list = ["cmd", "/c", "chcp 65001 >nul 2>&1 & " + cmd_str]
            else:
                try:
                    cmd_list = shlex.split(cmd_str)
                except ValueError as e:
                    return {"success": False, "command": "/cmd", "output": f"❌ 命令解析失败: {e}"}

            logger.info(f"/cmd 执行命令: {cmd_list}")

            env = os.environ.copy()
            env.update(_get_color_env())
            if sys.platform != "win32":
                cmd_list = _add_color_flag(cmd_list)

            try:
                result = subprocess.run(cmd_list, capture_output=True, env=env, text=True, encoding="utf-8", errors="replace", timeout=120)
                output = result.stdout or ""
                if result.stderr:
                    output += "\n" + result.stderr
                return {"success": result.returncode == 0, "command": "/cmd", "output": output}
            except subprocess.TimeoutExpired:
                return {"success": False, "command": "/cmd", "output": "❌ 命令执行超时（120秒）"}
            except FileNotFoundError:
                return {"success": False, "command": "/cmd", "output": f"❌ 命令不存在: {cmd_list[0]}"}
            except Exception as e:
                return {"success": False, "command": "/cmd", "output": f"❌ 执行失败: {e}"}

        # /cd 命令
        if cmd == "/cd" or cmd.startswith("/cd "):
            parts = cmd.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                return {"success": True, "command": "/cd", "output": f"当前工作目录: {os.getcwd()}\n用法: /cd <目录路径>"}
            dir_path = os.path.expanduser(parts[1].strip())
            dir_path = os.path.abspath(dir_path)
            if not os.path.isdir(dir_path):
                return {"success": False, "command": "/cd", "output": f"❌ 目录不存在: {dir_path}"}
            try:
                os.chdir(dir_path)
                new_dir = os.getcwd()
                return {"success": True, "command": "/cd", "output": f"✅ 工作目录已切换到: {new_dir}", "path": new_dir}
            except Exception as e:
                return {"success": False, "command": "/cd", "output": f"❌ 切换目录失败: {e}"}

        # /clear-all-data 命令
        if cmd == "/clear-all-data --force" or cmd.startswith("/clear-all-data "):
            project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
            fly_bin = project_root / "bin" / "fly"
            args = cmd.split(maxsplit=1)
            fly_args = args[1].strip() if len(args) > 1 else ""
            fly_cmd_args = ["clear-all-data"]
            if fly_args:
                fly_cmd_args.extend(fly_args.split())
            cmd_list = _build_python_cmd(fly_bin, fly_cmd_args)
            logger.warning(f"/clear-all-data 执行命令: {cmd_list}")
            try:
                result = subprocess.run(cmd_list, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, cwd=str(project_root))
                output = result.stdout or ""
                if result.stderr:
                    output += "\n" + result.stderr
                return {"success": result.returncode == 0, "command": "/clear-all-data", "output": output.strip() or "命令执行完成，无输出"}
            except subprocess.TimeoutExpired:
                return {"success": False, "command": "/clear-all-data", "output": "❌ 命令执行超时（120秒）"}
            except Exception as e:
                return {"success": False, "command": "/clear-all-data", "output": f"❌ 执行失败: {e}"}

        return {"success": False, "command": cmd, "output": f"未知命令: {cmd}"}
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"执行命令失败: {e}\n{tb}")
        return {"success": False, "command": cmd, "output": f"执行失败: {e}\n\nTraceback:\n{tb}"}


# ==================== WebUI Debug API ====================


def _get_workdir():
    """获取当前workdir（从workdir_config模块）"""
    from peng.webui.routes.workdir_config import _current_workdir
    return _current_workdir


@router.get("/webui/debug-webui")
async def webui_debug_state():
    """返回WebUI服务端调试状态"""
    from peng.config.loader import load_config
    from peng.webui.websocket import manager as ws_manager

    result = {
        "timestamp": datetime.now().isoformat(),
        "server": {"agent_name": AGENT_NAME, "workdir": _get_workdir()},
        "config": {}, "messages": [], "tasks": [],
        "websocket": {"active_connections": len(ws_manager.active_connections) if hasattr(ws_manager, 'active_connections') else 0},
        "terminal_sessions": [], "client_state": None,
    }

    try:
        config = load_config()
        webui_cfg = config.webui if hasattr(config, 'webui') else {}
        result["config"] = {"host": getattr(webui_cfg, 'host', None), "port": getattr(webui_cfg, 'port', None), "workdir": getattr(webui_cfg, 'workdir', None)}
    except Exception as e:
        result["config"] = {"error": str(e)}

    try:
        msg_mgr = MessageManager()
        messages = msg_mgr.db.get_all_messages()
        result["messages"] = {"total": len(messages), "archived": sum(1 for m in messages if m.get('archived_at')), "items": [{"id": m.get('id'), "parent_id": m.get('parent_id'), "status": m.get('status'), "archived": bool(m.get('archived_at')), "content_preview": (m.get('content', '') or '')[:80], "created_at": str(m.get('created_at', ''))} for m in messages[:50]]}
    except Exception as e:
        result["messages"] = {"error": str(e)}

    try:
        task_mgr = TaskManager()
        tasks = task_mgr.list_tasks()
        result["tasks"] = {"total": len(tasks), "items": [{"id": t.get('id'), "parent_id": t.get('parent_id'), "message_id": t.get('message_id'), "status": t.get('status'), "description": (t.get('description', '') or '')[:80], "workdir": t.get('workdir')} for t in tasks[:50]]}
    except Exception as e:
        result["tasks"] = {"error": str(e)}

    try:
        from peng.webui.websocket import terminal_session_manager
        result["terminal_sessions"] = [{"id": sid, "nest": getattr(s, 'nest', ''), "pid": getattr(getattr(s, 'pty_proc', None), 'pid', None)} for sid, s in terminal_session_manager.sessions.items()]
    except Exception as e:
        result["terminal_sessions"] = {"error": str(e)}

    ws_count = result["websocket"]["active_connections"]
    has_debug_session = False

    try:
        client_state_dir = Path(get_temp_directory()) / "webui_client_states"
        if client_state_dir.exists():
            now = time.time()
            state_files = sorted([f for f in client_state_dir.glob("client_*.json") if now - f.stat().st_mtime < 60], key=lambda f: f.stat().st_mtime, reverse=True)
            if state_files:
                if len(state_files) == 1:
                    with open(state_files[0], 'r', encoding='utf-8') as f:
                        result["client_state"] = json.load(f)
                        has_debug_session = result["client_state"].get("_is_debug", False)
                else:
                    clients = []
                    for sf in state_files[:10]:
                        try:
                            with open(sf, 'r', encoding='utf-8') as f:
                                cs = json.load(f)
                                cs["_source_file"] = sf.name
                                if cs.get("_is_debug"):
                                    has_debug_session = True
                                clients.append(cs)
                        except Exception:
                            pass
                    result["client_state"] = clients
    except Exception:
        pass

    if ws_count == 0:
        try:
            client_state_dir = Path(get_temp_directory()) / "webui_client_states"
            if client_state_dir.exists():
                for f in client_state_dir.glob("client_*.json"):
                    try:
                        f.unlink()
                    except Exception:
                        pass
        except Exception:
            pass

    result["_debug_hints"] = {"ws_connections": ws_count, "has_debug_browser": has_debug_session, "hint": "如果没有调试浏览器连接，执行: agent-browser open http://localhost:6688 --args '--no-sandbox' && sleep 6 && curl http://localhost:6688/api/webui/debug-webui"}
    if result["client_state"] is None:
        result["client_state"] = {"_hint": "无浏览器连接WebUI，请用agent-browser或手动打开浏览器"}

    return result


@router.post("/webui/client-state")
async def webui_client_state(request: Request):
    """接收前端JS上报的客户端状态"""
    try:
        body = await request.json()
        client_id = body.get("client_id", "default")
        client_id = "".join(c for c in str(client_id) if c.isalnum() or c in "-_")
        client_state_dir = Path(get_temp_directory()) / "webui_client_states"
        client_state_dir.mkdir(parents=True, exist_ok=True)
        client_state_file = client_state_dir / f"client_{client_id}.json"
        with open(client_state_file, 'w', encoding='utf-8') as f:
            json.dump(body, f, ensure_ascii=False, indent=2)
        return {"success": True, "client_id": client_id}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _serialize(obj, depth=0):
    """通用序列化"""
    import dataclasses
    if depth > 5:
        return str(obj)[:200]
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")[:5000]
    if isinstance(obj, dict):
        return {str(k): _serialize(v, depth+1) for k, v in list(obj.items())[:100]}
    if isinstance(obj, (list, tuple, set)):
        return [_serialize(i, depth+1) for i in list(obj)[:100]]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _serialize(dataclasses.asdict(obj), depth+1)
    if hasattr(obj, '__dict__'):
        return _serialize({k: v for k, v in obj.__dict__.items() if not k.startswith('_')}, depth+1)
    return str(obj)[:500]


def _build_debug_locals(request):
    """构建debug执行的locals"""
    from peng.config.loader import load_config
    config = load_config()
    return {
        "app": request.app, "state": request.app.state, "config": config,
        "result": {}, "MessageManager": MessageManager, "TaskManager": TaskManager,
        "terminal_session_manager": __import__('peng.webui.websocket', fromlist=['terminal_session_manager']).terminal_session_manager,
        "Path": Path, "json": json, "os": os, "sys": sys, "time": time,
        "datetime": datetime, "asyncio": asyncio, "traceback": traceback, "logging": logging,
    }


@router.post("/webui/debug-exec")
async def webui_debug_exec(request: Request):
    """在服务端Python进程中用exec执行代码"""
    try:
        body = await request.json()
        code = body.get("code", "")
        if not code:
            return {"success": False, "error": "code字段不能为空"}
        if len(code) > 10000:
            return {"success": False, "error": f"代码过长({len(code)}字符)，最大10000字符"}

        local_vars = _build_debug_locals(request)
        exec(code, {"__builtins__": __builtins__}, local_vars)

        try:
            serialized_result = _serialize(local_vars.get("result", {}))
        except Exception:
            serialized_result = str(local_vars.get("result", {}))[:2000]
        return {"success": True, "result": serialized_result}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)}", "traceback": traceback.format_exc()[-2000:]}


@router.post("/webui/debug-eval")
async def webui_debug_eval_python(request: Request):
    """在服务端Python进程中执行代码，可获取/修改任何运行时状态"""
    try:
        body = await request.json()
        code = body.get("code", "")
        if not code:
            return {"success": False, "error": "code字段不能为空"}
        if len(code) > 10000:
            return {"success": False, "error": f"代码过长({len(code)}字符)，最大10000字符"}

        local_vars = _build_debug_locals(request)
        exec_output = exec(code, {"__builtins__": __builtins__}, local_vars)

        result = local_vars.get("result", {})
        try:
            eval_result = eval(code, {"__builtins__": __builtins__}, local_vars)
            if eval_result is not None and not result:
                result = eval_result
        except SyntaxError:
            pass

        try:
            serialized_result = _serialize(result)
        except Exception:
            serialized_result = str(result)[:2000]
        return {"success": True, "result": serialized_result}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)}", "traceback": traceback.format_exc()[-2000:]}


@router.post("/webui/debug-nest-exec")
async def webui_debug_nest_exec(request: Request):
    """在指定远端或 local nest 端用 exec 执行 Python 代码"""
    try:
        body = await request.json()
        code = body.get("code", "")
        nest_name = body.get("nest")
        if not code:
            return {"success": False, "error": "code字段不能为空"}
        if len(code) > 10000:
            return {"success": False, "error": f"代码过长({len(code)}字符)，最大10000字符"}

        from peng.nest.client import python_exec as nest_python_exec
        response = await nest_python_exec(code, nest_name=nest_name, timeout=30.0)
        if response.error:
            return {"success": False, "error": response.error}
        return response.data if isinstance(response.data, dict) else {"success": True, "result": response.data}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)}", "traceback": traceback.format_exc()[-2000:]}


@router.post("/webui/debug-nest-eval")
async def webui_debug_nest_eval(request: Request):
    """在指定远端或 local nest 端用 eval 执行 Python 表达式"""
    try:
        body = await request.json()
        code = body.get("code", "")
        nest_name = body.get("nest")
        if not code:
            return {"success": False, "error": "code字段不能为空"}
        if len(code) > 10000:
            return {"success": False, "error": f"代码过长({len(code)}字符)，最大10000字符"}

        from peng.nest.client import python_eval as nest_python_eval
        response = await nest_python_eval(code, nest_name=nest_name, timeout=30.0)
        if response.error:
            return {"success": False, "error": response.error}
        return response.data if isinstance(response.data, dict) else {"success": True, "result": response.data}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)}", "traceback": traceback.format_exc()[-2000:]}


@router.get("/webui/debug")
async def webui_debug_help():
    """返回调试API使用说明"""
    return {
        "usage": "curl http://localhost:6688/api/webui/debug  # 就是这个接口，返回使用说明",
        "api": {
            "POST /api/webui/debug-exec": {"desc": "【服务端】Python exec执行代码", "params": {"code": "Python代码字符串(最长10000字符)"}},
            "POST /api/webui/debug-eval": {"desc": "【服务端】执行Python代码，可获取/修改运行时任何状态", "params": {"code": "Python代码字符串(最多10000字符)"}},
            "POST /api/webui/debug-nest-exec": {"desc": "【Nest远程】在指定nest端用exec执行Python语句", "params": {"code": "Python代码", "nest": "nest名称(可选)"}},
            "POST /api/webui/debug-nest-eval": {"desc": "【Nest远程】在指定nest端用eval执行Python表达式", "params": {"code": "Python表达式", "nest": "nest名称(可选)"}},
            "GET /api/webui/debug-webui": {"desc": "获取WebUI完整状态快照（含client-state中的自测试结果）"},
            "POST /api/webui/debug": {"desc": "【浏览器】在浏览器中执行任意JS代码并返回结果（刷新后自动缓存重试）"},
            "POST /api/webui/client-ready": {"desc": "【浏览器】刷新后通知服务端ready，触发缓存eval重发", "params": {"client_id": "客户端ID", "is_debug": "是否调试浏览器"}},
        },
    }


@router.post("/webui/debug")
async def webui_debug_eval(request: Request):
    """在浏览器中执行任意JS代码并返回结果（收集所有客户端响应）"""
    try:
        body = await request.json()
        code = body.get("code", "")
        if not code:
            return {"success": False, "error": "code字段不能为空"}
        if len(code) > 10000:
            return {"success": False, "error": f"代码过长({len(code)}字符)，最大10000字符"}

        from peng.webui.websocket import manager as ws_manager
        active = ws_manager.active_connections if hasattr(ws_manager, 'active_connections') else []
        if not active:
            return {"success": False, "error": "无浏览器连接。请先用agent-browser或手动打开WebUI页面。"}

        eval_id = f"eval_{int(time.time()*1000)}"

        # 🔧 Task#427：修复debug eval只收集第一个客户端响应就关闭的bug
        # 改用 Event + results list 模式，等待所有客户端响应或超时
        if not hasattr(webui_debug_eval, '_pending'):
            webui_debug_eval._pending = {}

        pending_entry = {
            'event': asyncio.Event(),
            'results': [],
            'expected_count': len(active),
        }
        webui_debug_eval._pending[eval_id] = pending_entry

        broadcast_msg = {"type": "debug_eval", "eval_id": eval_id, "code": code}
        await ws_manager.broadcast(broadcast_msg)

        # 等待所有客户端响应，或超时（最多5秒）
        try:
            deadline = asyncio.get_event_loop().time() + 5.0
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(pending_entry['event'].wait(), timeout=remaining)
                    break  # event被set说明所有客户端都响应了
                except asyncio.TimeoutError:
                    break  # 超时，返回已收集到的结果
        finally:
            webui_debug_eval._pending.pop(eval_id, None)

        results = pending_entry['results']
        if not results:
            # 没有任何响应，缓存eval请求等浏览器重连
            _cache_pending_eval(eval_id, code)
            return {"success": False, "error": "等待浏览器执行结果超时。请求已缓存，等浏览器重连后自动重试。", "eval_id": eval_id}

        return {"success": True, "eval_id": eval_id, "results": results,
                "collected": len(results), "expected": pending_entry['expected_count']}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)}"}


@router.post("/webui/eval-response")
async def webui_eval_response(request: Request):
    """接收浏览器返回的JS执行结果"""
    try:
        body = await request.json()
        eval_id = body.get("eval_id", "")
        result = body.get("result")

        if not eval_id:
            return {"success": False, "error": "eval_id不能为空"}

        if hasattr(webui_debug_eval, '_pending') and eval_id in webui_debug_eval._pending:
            entry = webui_debug_eval._pending[eval_id]
            # 🔧 Task#427：追加结果而不是pop后set_result，支持多客户端
            entry['results'].append({
                "client_id": body.get("client_id", "unknown"),
                "is_debug": body.get("is_debug", False),
                "result": result
            })
            # 所有客户端都响应了，通知等待的协程
            if len(entry['results']) >= entry['expected_count']:
                entry['event'].set()
            # 同时从重试缓存中清除（如果存在）
            _clear_cached_eval(eval_id)
            return {"success": True}

        # 即使没有pending entry，也写入client-state（页面刷新后entry已超时释放）
        _write_eval_result_to_client_state(body.get("client_id", "unknown"), eval_id, result)
        return {"success": False, "error": f"未找到对应的eval请求: {eval_id}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/webui/client-ready")
async def webui_client_ready(request: Request):
    """
    方案A: 浏览器刷新后发送ready信号
    
    前端在页面加载3秒后自动调用此端点，通知服务端"我已经准备好了"。
    服务端会检查是否有缓存的中pending eval请求（刷新前发出的但超时的），
    如果有就通过WebSocket重新发送给浏览器执行。
    """
    try:
        body = await request.json()
        client_id = body.get("client_id", "unknown")
        is_debug = body.get("is_debug", False)
        
        # 获取待重试的eval请求
        pending_evals = _get_pending_evals()
        
        if pending_evals:
            # 通过WebSocket重发缓存的eval请求
            from peng.webui.websocket import manager as ws_manager
            active = ws_manager.active_connections if hasattr(ws_manager, 'active_connections') else []
            
            if active:
                for pe in pending_evals:
                    retry_msg = {
                        "type": "debug_eval_retry",
                        "eval_id": pe["eval_id"],
                        "code": pe["code"],
                    }
                    await ws_manager.broadcast(retry_msg)
                    logger.info(f"[debug-reconnect] 重发缓存eval {pe['eval_id']} 给重连的客户端 {client_id}")
                
                return {"success": True, "pending_evals": len(pending_evals), "message": f"已重发{len(pending_evals)}个缓存的eval请求"}
            else:
                # 有缓存但没有WebSocket连接，把eval放回缓存
                for pe in pending_evals:
                    pe["retry_count"] = pe.get("retry_count", 1) - 1  # 回退retry_count
                return {"success": True, "pending_evals": len(pending_evals), "message": "有待重试的eval但WebSocket未连接"}
        
        return {"success": True, "pending_evals": 0, "message": "无缓存的eval请求"}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)}"}

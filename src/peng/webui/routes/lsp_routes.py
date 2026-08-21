"""
LSP路由模块

提供 LSP 依赖项状态查询、安装、以及代码智能功能。

路由前缀: /api (由 server.py 注册)
- /lsp/dependencies, /lsp/install/{dependency_name}, /lsp/install-all
- /lsp/definition, /lsp/references, /lsp/hover, /lsp/symbols,
  /lsp/implementation, /lsp/diagnostics, /lsp/completion
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import socket
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)
router = APIRouter()

# 全局AdapterManager实例（按workdir缓存）
_adapter_managers: dict[str, Any] = {}

# daemon存活缓存：{(nest_name, workdir): (is_alive, timestamp)}
_daemon_alive_cache: dict[tuple[str, str], tuple[bool, float]] = {}
_DAEMON_CACHE_TTL = 60.0  # 60秒内不重复ping


def _get_workdir() -> str:
    """获取当前工作目录"""
    try:
        from peng.webui.routes.workdir_config import _current_workdir
        return _current_workdir or os.getcwd()
    except ImportError:
        return os.getcwd()


def _get_adapter_manager(workdir: str) -> Any:
    """获取或创建AdapterManager实例（按workdir缓存）"""
    if workdir not in _adapter_managers:
        from peng.tools.lsp.manager import AdapterManager
        _adapter_managers[workdir] = AdapterManager(workdir)
    return _adapter_managers[workdir]


def _is_windows_nest(conn) -> bool:
    """检测远程nest是否为Windows平台"""
    platform = conn.info.get("platform", "") if conn and conn.info else ""
    return "Windows" in platform


def _get_python_cmd(conn, lsp_query_path: str = "") -> str:
    """根据nest平台返回正确的python命令（优先使用项目.venv）"""
    is_windows = _is_windows_nest(conn)
    
    if lsp_query_path:
        # 从 bin/lsp-query 路径推导项目根目录
        # 使用字符串操作避免 os.path 在跨平台路径上出错
        sep = "\\" if is_windows else "/"
        parts = lsp_query_path.replace("\\", "/").rstrip("/").split("/")
        if parts and parts[-1] == "lsp-query" and len(parts) >= 3:
            # 去掉 bin/lsp-query，得到项目根目录
            project_root = sep.join(parts[:-2])
            if is_windows:
                return project_root + sep + ".venv" + sep + "Scripts" + sep + "pythonw.exe"
            else:
                return project_root + sep + ".venv" + sep + "bin" + sep + "python3"
    
    return "pythonw" if is_windows else "python3"


def _shell_quote(conn, s: str) -> str:
    """根据nest平台选择正确的shell引用方式"""
    if _is_windows_nest(conn):
        # Windows cmd: 用双引号包裹
        return f'"{s}"'
    else:
        import shlex
        return shlex.quote(s)


@router.get("/lsp/dependencies")
async def get_lsp_dependencies():
    """获取所有LSP依赖项的状态"""
    try:
        from peng.tools.lsp.dependencies import LspDependencyManager
        manager = LspDependencyManager()
        return {"success": True, "dependencies": manager.get_dependencies_status()}
    except Exception as e:
        logger.error(f"获取LSP依赖项状态失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取LSP依赖项状态失败: {e}")


@router.post("/lsp/install/{dependency_name}")
async def install_lsp_dependency(dependency_name: str):
    """手动安装指定的LSP依赖项"""
    try:
        from peng.tools.lsp.dependencies import LspDependencyManager, InstallStatus
        manager = LspDependencyManager()
        dependency = manager.get_dependency(dependency_name)

        if dependency is None:
            raise HTTPException(status_code=404, detail=f"未找到依赖项: {dependency_name}")

        if manager.is_installed(dependency):
            return {"success": True, "message": f"{dependency_name} 已安装", "status": "already_installed"}

        async def install_and_notify():
            from peng.webui.websocket import broadcast_toast

            async def progress_callback(name, status, error_message):
                if status == InstallStatus.INSTALLED:
                    await broadcast_toast("success", "安装成功", f"{name} 已安装")
                elif status == InstallStatus.INSTALL_FAILED:
                    await broadcast_toast("error", "安装失败", f"{name} 失败: {error_message}")

            success = await manager.install(dependency, progress_callback)
            return success

        task = asyncio.create_task(install_and_notify())
        return {"success": True, "message": f"正在后台安装 {dependency_name}", "status": "installing"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"安装LSP依赖项失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"安装LSP依赖项失败: {e}")


@router.post("/lsp/install-all")
async def install_all_lsp_dependencies():
    """手动触发安装所有未安装的LSP依赖项"""
    try:
        from peng.tools.lsp.dependencies import LspDependencyManager
        from peng.webui.websocket import broadcast_toast

        async def install_all():
            manager = LspDependencyManager()

            async def progress_callback(name, status, error_message):
                from peng.tools.lsp.dependencies import InstallStatus
                if status == InstallStatus.INSTALLED:
                    await broadcast_toast("success", "安装成功", f"{name} 已安装")
                elif status == InstallStatus.INSTALL_FAILED:
                    await broadcast_toast("error", "安装失败", f"{name} 失败: {error_message}")

            results = await manager.check_and_install_all(progress_callback)

            installed_count = sum(1 for success in results.values() if success)
            failed_count = len(results) - installed_count
            total_count = len(results)

            if failed_count == 0:
                await broadcast_toast("success", "LSP依赖项安装完成", f"所有 {total_count} 个LSP依赖项已安装成功")
            else:
                await broadcast_toast("warning", "LSP依赖项安装完成（部分失败）", f"成功 {installed_count}/{total_count}，失败 {failed_count}")

            return results

        asyncio.create_task(install_all())
        return {"success": True, "message": "正在后台安装所有LSP依赖项"}
    except Exception as e:
        logger.error(f"安装所有LSP依赖项失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"安装所有LSP依赖项失败: {e}")


# ==================== LSP代码智能API ====================


def _daemon_address(workdir: str) -> tuple[str, int]:
    """根据workdir计算daemon的TCP地址（与bin/lsp-query中_server_address一致）"""
    import hashlib
    normalized = workdir.replace("\\", "/").rstrip("/")
    h = int(hashlib.md5(normalized.encode()).hexdigest(), 16)
    port = 19000 + (h % 1000)
    return ("127.0.0.1", port)


def _query_local_daemon(workdir: str, action: str, file_path: str,
                        line: int, column: int, content: str = "") -> Optional[dict]:
    """
    直接通过TCP连接本地daemon执行LSP查询（不走nest exec，零开销）。
    返回None表示daemon不可用，需要回退。
    """
    host, port = _daemon_address(workdir)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(15)
        sock.connect((host, port))

        request = {
            "action": action,
            "file_path": file_path,
            "line": line,
            "column": column,
            "workdir": workdir,
        }
        if content:
            request["content"] = content

        msg = _json.dumps(request) + "\n"
        sock.sendall(msg.encode("utf-8"))

        buf = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                break

        sock.close()
        response_text = buf.decode("utf-8").strip()
        if not response_text:
            return None

        return _json.loads(response_text)
    except (ConnectionRefusedError, ConnectionResetError, socket.timeout,
            _json.JSONDecodeError, OSError) as e:
        return None
    except Exception as e:
        logger.debug(f"本地daemon直连失败: {e}")
        return None


def _ping_local_daemon(workdir: str) -> bool:
    """直接通过TCP ping本地daemon，不走nest exec"""
    result = _query_local_daemon(workdir, "ping", "", 1, 1)
    return result is not None and result.get("alive") is True


async def _ensure_local_daemon(workdir: str):
    """
    确保本地daemon运行。先检查缓存，缓存miss时直连ping，不通则启动。
    全程不走nest exec。
    """
    cache_key = ("local", workdir)
    cached = _daemon_alive_cache.get(cache_key)
    if cached and cached[0] and (time.monotonic() - cached[1]) < _DAEMON_CACHE_TTL:
        return  # 缓存命中，daemon存活

    # 直连ping（不走nest exec）
    if _ping_local_daemon(workdir):
        _daemon_alive_cache[cache_key] = (True, time.monotonic())
        return

    # daemon不通，启动新daemon
    import subprocess
    local_lsp_query = _get_local_lsp_query()
    project_root = str(Path(__file__).resolve().parents[4])
    venv_python = os.path.join(project_root, ".venv", "bin", "python3")
    python_cmd = venv_python if os.path.isfile(venv_python) else "python3"

    try:
        subprocess.Popen(
            [python_cmd, local_lsp_query, "--serve", workdir],
            stdout=open(os.devnull, "w"),
            stderr=open(os.devnull, "w"),
            start_new_session=True,
        )
        logger.info(f"已启动本地LSP daemon: workdir={workdir}")
        await asyncio.sleep(0.3)  # 给daemon启动时间
        # 不更新缓存，下次请求自然会ping确认
    except Exception as e:
        logger.warning(f"启动本地LSP daemon失败: {e}")


async def _find_remote_lsp_query(conn, nest_name: str) -> Optional[str]:
    """查找远程nest上的bin/lsp-query路径，结果缓存"""
    cache_key = f"_lsp_query_path:{nest_name}"
    if cache_key in _adapter_managers:
        return _adapter_managers[cache_key]

    from peng.nest.protocol import NestRequest

    if _is_windows_nest(conn):
        # Windows: 检查常见路径
        check_cmd = (
            'for %d in (D:\\work\\peng C:\\work\\peng %USERPROFILE%\\peng) do '
            '@if exist "%d\\bin\\lsp-query" @(echo %d\\bin\\lsp-query & goto :found)\n'
            ':found'
        )
    else:
        # Linux/macOS: 检查常见路径
        check_cmd = (
            "for d in /mnt/wsl/work/peng /mnt/p/work/peng /opt/peng $HOME/peng; do "
            "test -f \"$d/bin/lsp-query\" && echo \"$d/bin/lsp-query\" && break; done"
        )
    find_request = NestRequest(
        type="bash",
        params={"command": check_cmd, "timeout": 5000, "_skip_security": True},
    )
    find_resp = await conn.send_request(find_request, timeout=10.0)
    remote_path = (find_resp.data or {}).get("stdout", "").strip() if find_resp.data and not find_resp.error else ""
    if not remote_path:
        return None
    _adapter_managers[cache_key] = remote_path
    logger.info(f"远程nest '{nest_name}' lsp-query路径: {remote_path}")
    return remote_path


def _get_local_lsp_query() -> str:
    """获取本地bin/lsp-query绝对路径"""
    peng_root = str(Path(__file__).resolve().parents[4])
    return os.path.join(peng_root, "bin", "lsp-query")


async def _handle_nest_lsp(body: dict, action: str) -> dict:
    """
    统一的LSP处理。
    
    Local nest: 直接通过TCP连接daemon（零开销，不走nest exec bash）。
    Remote nest: 通过nest exec执行bin/lsp-query，daemon缓存减少ping开销。
    """
    nest_name = body.get("nest") or "local"
    file_path = body.get("file_path", "")
    line = body.get("line", 1)
    column = body.get("column", 1)
    content = body.get("content", "")
    
    logger.info(f"LSP {action} request: nest={nest_name}, file={file_path}:{line}:{column}, has_content={bool(content)}")

    # 推导被编辑文件的项目根目录（作为LSP的workdir参数）
    lsp_workdir = _get_workdir()
    if file_path:
        # 统一用字符串操作推导项目根目录（避免Path跨平台问题）
        normalized = file_path.replace("\\", "/")
        # 找 "src/" 目录，取其父目录作为项目根
        src_idx = normalized.find("/src/")
        if src_idx > 0:
            lsp_workdir = normalized[:src_idx]
        elif "/" in normalized:
            # 没有src目录，取文件所在目录的上级
            parts = normalized.rsplit("/", 1)
            lsp_workdir = parts[0] if len(parts) > 1 else normalized

    # ===== Local nest: 直接TCP连接daemon，零开销 =====
    if nest_name == "local":
        try:
            await _ensure_local_daemon(lsp_workdir)
            # 直接通过TCP查询daemon
            result = await asyncio.get_event_loop().run_in_executor(
                None, _query_local_daemon, lsp_workdir, action, file_path, line, column, content
            )
            if result is not None:
                logger.info(f"LSP {action} local direct: file={file_path}, success={result.get('success')}")
                return result
            # daemon不可用，回退到单次模式（通过nest exec）
            logger.warning(f"本地daemon不可用，回退nest exec")
        except Exception as e:
            logger.warning(f"本地daemon直连异常: {e}")

    # ===== Remote nest: 通过lsp_query协议，nest agent自管理daemon =====
    try:
        from peng.nest.bridge import NestBridgeManager
        from peng.nest.protocol import NestRequest

        mgr = NestBridgeManager.get_instance()
        conn = mgr.get_connection(nest_name)
        if not conn:
            return {"success": False, "error": f"nest '{nest_name}' 未连接"}

        # 直接发lsp_query请求，nest agent自己管理daemon启动/连接
        lsp_params = {
            "action": action,
            "file_path": file_path,
            "line": line,
            "column": column,
            "workdir": lsp_workdir,
            "_skip_security": True,
        }
        if content:
            lsp_params["content"] = content

        lsp_request = NestRequest(type="lsp_query", params=lsp_params)
        response = await conn.send_request(lsp_request, timeout=30.0)

        if response.error:
            return {"success": False, "error": response.error}

        result = response.data
        if isinstance(result, dict):
            return result

        return {"success": False, "error": f"无法解析LSP结果: {str(result)[:200]}"}
    except Exception as e:
        logger.error(f"LSP lsp_query失败: {e}\n{traceback.format_exc()}")
        return {"success": False, "error": str(e)}


def _make_lsp_route(action_name: str):
    """创建统一的LSP路由处理函数"""
    async def handler(request: Request):
        try:
            body = await request.json()
            return await _handle_nest_lsp(body, action_name)
        except Exception as e:
            logger.error(f"LSP {action_name}失败: {e}\n{traceback.format_exc()}")
            return {"success": False, "error": str(e)}
    return handler


@router.post("/lsp/definition")
async def lsp_definition(request: Request):
    return await _make_lsp_route("definition")(request)


@router.post("/lsp/references")
async def lsp_references(request: Request):
    return await _make_lsp_route("references")(request)


@router.post("/lsp/hover")
async def lsp_hover(request: Request):
    return await _make_lsp_route("hover")(request)


@router.post("/lsp/symbols")
async def lsp_symbols(request: Request):
    return await _make_lsp_route("symbols")(request)


@router.post("/lsp/implementation")
async def lsp_implementation(request: Request):
    return await _make_lsp_route("implementation")(request)


@router.post("/lsp/diagnostics")
async def lsp_diagnostics(request: Request):
    return await _make_lsp_route("diagnostics")(request)


@router.post("/lsp/completion")
async def lsp_completion(request: Request):
    return await _make_lsp_route("completion")(request)


@router.post("/lsp/status")
async def lsp_status(request: Request):
    """
    检查指定文件/语言的LSP可用状态。
    用于前端显示LSP状态指示器。
    Local nest直接调用_check_status不走nest exec。
    """
    try:
        body = await request.json()
        file_path = body.get("file_path", "")
        nest_name = body.get("nest") or "local"

        if not file_path:
            return {"success": False, "error": "无文件路径"}

        ext = Path(file_path).suffix.lower()
        from peng.tools.lsp.language import get_server_type
        language = get_server_type(ext)

        if not language:
            return {
                "success": True,
                "available": False,
                "language": None,
                "reason": "不支持的语言",
            }

        # 推导workdir
        lsp_workdir = _get_workdir()
        if file_path:
            normalized = file_path.replace("\\", "/")
            src_idx = normalized.find("/src/")
            if src_idx > 0:
                lsp_workdir = normalized[:src_idx]

        # Local nest: 直接调用check_status（不走nest exec）
        if nest_name == "local":
            import subprocess
            local_lsp_query = _get_local_lsp_query()
            project_root = str(Path(__file__).resolve().parents[4])
            venv_python = os.path.join(project_root, ".venv", "bin", "python3")
            python_cmd = venv_python if os.path.isfile(venv_python) else "python3"
            try:
                proc = subprocess.run(
                    [python_cmd, local_lsp_query, "--check-status", file_path, lsp_workdir],
                    capture_output=True, text=True, timeout=10,
                )
                if proc.returncode == 0 and proc.stdout.strip().startswith("{"):
                    return _json.loads(proc.stdout.strip())
            except Exception:
                pass
            return {
                "success": True,
                "available": False,
                "language": language,
                "reason": "状态检查失败",
            }

        # Remote nest: 通过nest exec
        local_lsp_query = _get_local_lsp_query()

        from peng.nest.bridge import NestBridgeManager
        from peng.nest.protocol import NestRequest

        mgr = NestBridgeManager.get_instance()
        conn = mgr.get_connection(nest_name)
        if not conn:
            return {
                "success": True,
                "available": False,
                "language": language,
                "reason": f"nest '{nest_name}' 未连接",
            }

        lsp_query_path = await _find_remote_lsp_query(conn, nest_name)
        if not lsp_query_path:
            return {
                "success": True,
                "available": False,
                "language": language,
                "reason": f"远程nest '{nest_name}' 上未找到bin/lsp-query",
            }

        python_cmd = _get_python_cmd(conn, lsp_query_path)
        q = lambda s: _shell_quote(conn, s)
        check_cmd = f"{python_cmd} {q(lsp_query_path)} --check-status {q(file_path)} {q(lsp_workdir)}"
        check_request = NestRequest(
            type="bash",
            params={"command": check_cmd, "timeout": 10000, "_skip_security": True},
        )
        resp = await conn.send_request(check_request, timeout=15.0)

        if resp.error:
            return {
                "success": True,
                "available": False,
                "language": language,
                "reason": resp.error[:200],
            }

        stdout = (resp.data or {}).get("stdout", "").strip()
        exit_code = (resp.data or {}).get("exit_code", -1)

        if exit_code == 0 and stdout.startswith("{"):
            try:
                return _json.loads(stdout)
            except Exception:
                pass

        return {
            "success": True,
            "available": False,
            "language": language,
            "reason": f"状态检查失败: {stdout[:200]}",
        }

    except Exception as e:
        return {
            "success": True,
            "available": False,
            "language": None,
            "reason": str(e)[:200],
        }


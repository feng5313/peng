"""
Nest 本地执行器

在 nest 服务端本地执行命令、文件操作、窗口查询和截屏。
这是默认的执行器，直接操作本地文件系统和进程。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import re
import shlex
import signal
import subprocess
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from peng.common.paths import chown_to_project_owner, ensure_dir_with_owner
from peng.nest.protocol import NestResponse, ResponseType

logger = logging.getLogger(__name__)

# 平台检测
IS_WINDOWS = sys.platform == "win32"

# Windows PTY 支持
_WIN_PTY_AVAILABLE = False
if IS_WINDOWS:
    try:
        from winpty import PtyProcess as _WinPtyProcess

        _WIN_PTY_AVAILABLE = True
    except ImportError:
        pass


def _decode_output(data: bytes) -> str:
    """解码命令输出（尝试多种编码）"""
    if not data:
        return ""
    for encoding in ["utf-8", "gbk", "cp936", "latin-1"]:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _read_text_safe(path, errors="replace") -> str:
    """安全读取文本文件（尝试多种编码）"""
    for encoding in ["utf-8", "gbk", "cp936", "latin-1"]:
        try:
            with open(path, encoding=encoding, errors=errors, newline="") as f:
                return f.read()
        except (UnicodeDecodeError, LookupError):
            continue
    # 最后用utf-8 + replace兜底
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


def _write_text_safe(path, content: str, original_path: str = None) -> str:
    """安全写入文本文件，尽量保持原编码。
    返回实际使用的编码。
    """
    # 简单策略：尝试检测原文件编码，否则用utf-8
    encoding = "utf-8"
    if original_path and Path(original_path).exists():
        try:
            raw = Path(original_path).read_bytes()
            if b"\x00" not in raw[:4096]:
                for enc in ["utf-8", "gbk", "cp936", "latin-1"]:
                    try:
                        raw.decode(enc)
                        encoding = enc
                        break
                    except (UnicodeDecodeError, LookupError):
                        continue
        except Exception:
            pass

    with open(path, "w", encoding=encoding, newline="") as f:
        f.write(content)
    chown_to_project_owner(path)
    return encoding


def _build_line_edit_context(
    lines: list[str],
    operation: str,
    start_line: int | None,
    end_line: int | None,
    insert_line: int | None,
    affected_lines: list[str],
) -> list[str]:
    """生成 line_edit 编辑区域附近的带行号上下文预览。

    返回编辑后文件中编辑点前后各 CONTEXT_PAD 行的 "行号: 内容" 列表，
    让 LLM 在连续编辑时能清楚看到当前实际行号分布。
    """
    CONTEXT_PAD = 5
    new_total = len(lines)

    # 计算编辑影响区域的行号范围（1-based，编辑后的新行号）
    if operation == "delete" and start_line and end_line:
        # 删除后，原 start_line 位置现在是原来 end_line+1 的内容
        center = start_line  # 删除区域的起始位置
    elif operation == "replace" and start_line and end_line:
        # 替换后，新内容从 start_line 开始，共 len(affected_lines) 行
        center = start_line + max(len(affected_lines) - 1, 0) if affected_lines else start_line
    elif operation == "insert" and insert_line is not None:
        center = insert_line if affected_lines else insert_line
    else:
        center = 1

    # 上下文窗口范围（1-based）
    ctx_start = max(1, center - CONTEXT_PAD)
    ctx_end = min(new_total, center + CONTEXT_PAD)
    # 如果替换/插入了多行，确保窗口覆盖整个新内容
    if affected_lines:
        if operation == "replace" and start_line:
            ctx_end = max(ctx_end, min(new_total, start_line + len(affected_lines) + CONTEXT_PAD - 1))
        elif operation == "insert" and insert_line is not None:
            ctx_end = max(ctx_end, min(new_total, insert_line + len(affected_lines) + CONTEXT_PAD))

    result = []
    for i in range(ctx_start, ctx_end + 1):
        line_content = lines[i - 1]
        # 截断过长的行
        if len(line_content) > 200:
            line_content = line_content[:200] + "..."
        result.append(f"{i}: {line_content}")
    return result


class LocalExecutor:
    """本地执行器 - 在 nest 服务端直接执行操作"""

    def __init__(self, security=None):
        """
        初始化本地执行器

        Args:
            security: NestSecurity 实例，用于安全检查
        """
        self.security = security

    async def handle_request(
        self, request_type: str, params: dict[str, Any]
    ) -> NestResponse | AsyncIterator[NestResponse]:
        """
        处理请求

        Args:
            request_type: 请求类型
            params: 请求参数

        Returns:
            NestResponse 或异步迭代器（bash 流式输出）
        """
        handler = getattr(self, f"_handle_{request_type}", None)
        if not handler:
            return NestResponse.make_error(
                params.get("_request_id", ""), f"不支持的请求类型: {request_type}"
            )

        # 🔧 支持 _skip_security 参数：WebUI远程文件/终端操作不受黑白名单限制
        skip_security = params.pop("_skip_security", False)

        try:
            # 临时禁用安全检查
            original_security = self.security
            if skip_security:
                self.security = None
            try:
                return await handler(params)
            finally:
                if skip_security:
                    self.security = original_security
        except Exception as e:
            logger.error(f"处理请求失败: type={request_type}, error={e}\n{traceback.format_exc()}")
            return NestResponse.make_error(params.get("_request_id", ""), f"执行失败: {e}")

    # ==================== 系统命令 ====================

    async def _handle_ping(self, params: dict) -> NestResponse:
        """处理 ping 请求"""
        import platform

        result = {
            "status": "ok",
            "platform": platform.system(),
            "hostname": platform.node(),
            "python": platform.python_version(),
        }
        # Linux: 读取 /etc/os-release 获取发行版信息
        if sys.platform.startswith("linux"):
            try:
                os_release = {}
                with open("/etc/os-release", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if "=" in line:
                            k, _, v = line.partition("=")
                            os_release[k] = v.strip('"')
                result["linux_id"] = os_release.get("ID", "")
                result["linux_name"] = os_release.get("NAME", "Linux")
                result["linux_version"] = os_release.get("VERSION_ID", "")
            except Exception:
                pass
        return NestResponse.make_result(params.get("_request_id", ""), result)

    async def _handle_python_exec(self, params: dict) -> NestResponse:
        """在 nest 端 Python 进程中用 exec 执行代码（执行语句、修改状态）

        params:
            code: str - Python 代码
        """
        import json
        import dataclasses
        from datetime import datetime

        code = params.get("code", "")
        if not code:
            return NestResponse.make_error(params.get("_request_id", ""), "code不能为空")

        if len(code) > 10000:
            return NestResponse.make_error(
                params.get("_request_id", ""), f"代码过长({len(code)}字符)，最大10000字符"
            )

        result = {}

        local_vars = {
            "result": result,
            "Path": Path,
            "json": json,
            "os": os,
            "sys": sys,
            "time": time,
            "datetime": datetime,
            "asyncio": asyncio,
            "traceback": traceback,
            "logging": logging,
        }

        try:
            exec(code, {"__builtins__": __builtins__}, local_vars)

            # 序列化 result
            def serialize(obj, depth=0):
                if depth > 5:
                    return str(obj)[:200]
                if obj is None or isinstance(obj, (bool, int, float, str)):
                    return obj
                if isinstance(obj, bytes):
                    return obj.decode("utf-8", errors="replace")[:5000]
                if isinstance(obj, dict):
                    return {str(k): serialize(v, depth + 1) for k, v in list(obj.items())[:100]}
                if isinstance(obj, (list, tuple, set)):
                    return [serialize(i, depth + 1) for i in list(obj)[:100]]
                if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
                    return serialize(dataclasses.asdict(obj), depth + 1)
                if hasattr(obj, "__dict__"):
                    return serialize(
                        {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}, depth + 1
                    )
                return str(obj)[:500]

            serialized_result = serialize(result)
            return NestResponse.make_result(
                params.get("_request_id", ""), {"success": True, "result": serialized_result}
            )
        except Exception as e:
            return NestResponse.make_result(
                params.get("_request_id", ""),
                {
                    "success": False,
                    "error": f"{type(e).__name__}: {str(e)}",
                    "traceback": traceback.format_exc()[-2000:],
                },
            )

    async def _handle_python_eval(self, params: dict) -> NestResponse:
        """在 nest 端 Python 进程中用 eval 执行表达式（查询状态）

        params:
            code: str - Python 表达式
        """
        import json
        import dataclasses
        from datetime import datetime

        code = params.get("code", "")
        if not code:
            return NestResponse.make_error(params.get("_request_id", ""), "code不能为空")

        if len(code) > 10000:
            return NestResponse.make_error(
                params.get("_request_id", ""), f"代码过长({len(code)}字符)，最大10000字符"
            )

        result = {}

        local_vars = {
            "result": result,
            "Path": Path,
            "json": json,
            "os": os,
            "sys": sys,
            "time": time,
            "datetime": datetime,
            "asyncio": asyncio,
            "traceback": traceback,
            "logging": logging,
        }

        try:
            exec(code, {"__builtins__": __builtins__}, local_vars)

            # 如果代码用了 eval 风格（最后一行是表达式），尝试 eval
            try:
                eval_result = eval(code, {"__builtins__": __builtins__}, local_vars)
                if eval_result is not None and not result:
                    result = eval_result
            except SyntaxError:
                pass  # exec 模式，结果在 result 或 local_vars 中

            # 序列化结果
            def serialize(obj, depth=0):
                if depth > 5:
                    return str(obj)[:200]
                if obj is None or isinstance(obj, (bool, int, float, str)):
                    return obj
                if isinstance(obj, bytes):
                    return obj.decode("utf-8", errors="replace")[:5000]
                if isinstance(obj, dict):
                    return {str(k): serialize(v, depth + 1) for k, v in list(obj.items())[:100]}
                if isinstance(obj, (list, tuple, set)):
                    return [serialize(i, depth + 1) for i in list(obj)[:100]]
                if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
                    return serialize(dataclasses.asdict(obj), depth + 1)
                if hasattr(obj, "__dict__"):
                    return serialize(
                        {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}, depth + 1
                    )
                return str(obj)[:500]

            serialized_result = serialize(result)
            return NestResponse.make_result(
                params.get("_request_id", ""), {"success": True, "result": serialized_result}
            )
        except Exception as e:
            return NestResponse.make_result(
                params.get("_request_id", ""),
                {
                    "success": False,
                    "error": f"{type(e).__name__}: {str(e)}",
                    "traceback": traceback.format_exc()[-2000:],
                },
            )

    # 已知daemon存活状态缓存: workdir -> (port, alive_time)
    _daemon_cache: dict = {}

    def _get_lsp_daemon_port(self, workdir: str) -> int:
        """计算daemon TCP端口"""
        import hashlib

        normalized = workdir.replace("\\", "/").rstrip("/")
        h = int(hashlib.md5(normalized.encode()).hexdigest(), 16)
        return 19000 + (h % 1000)

    def _ping_daemon(self, port: int) -> bool:
        """TCP ping daemon"""
        import socket, json

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect(("127.0.0.1", port))
            s.sendall(
                (
                    json.dumps(
                        {"action": "ping", "file_path": "", "line": 1, "column": 1, "workdir": ""}
                    )
                    + "\n"
                ).encode()
            )
            buf = b""
            while True:
                c = s.recv(4096)
                if not c:
                    break
                buf += c
                if b"\n" in buf:
                    break
            s.close()
            r = json.loads(buf.decode().strip())
            return r.get("alive") is True
        except Exception:
            return False

    def _start_daemon(self, workdir: str, port: int) -> bool:
        """启动LSP daemon进程"""
        import subprocess, sys

        # 找到lsp-query路径
        project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        lsp_query = os.path.join(project_root, "bin", "lsp-query")
        if not os.path.isfile(lsp_query):
            return False
        # 找python（Windows用pythonw.exe避免弹控制台窗口）
        if sys.platform == "win32":
            venv_py = os.path.join(project_root, ".venv", "Scripts", "pythonw.exe")
            python_cmd = (
                venv_py
                if os.path.isfile(venv_py)
                else sys.executable.replace("python.exe", "pythonw.exe")
            )
        else:
            venv_py = os.path.join(project_root, ".venv", "bin", "python3")
            python_cmd = venv_py if os.path.isfile(venv_py) else sys.executable

        try:
            if sys.platform == "win32":
                import subprocess

                subprocess.Popen(
                    [python_cmd, lsp_query, "--serve", workdir],
                    creationflags=0x08000000,  # CREATE_NO_WINDOW - 不创建任何控制台窗口
                    stdout=open(os.devnull, "w"),
                    stderr=open(os.devnull, "w"),
                )
            else:
                subprocess.Popen(
                    [python_cmd, lsp_query, "--serve", workdir],
                    start_new_session=True,
                    stdout=open(os.devnull, "w"),
                    stderr=open(os.devnull, "w"),
                )
            logger.info(f"LSP daemon启动: workdir={workdir}, port={port}")
            return True
        except Exception as e:
            logger.warning(f"LSP daemon启动失败: {e}")
            return False

    async def _handle_lsp_query(self, params: dict) -> NestResponse:
        """
        LSP查询：nest agent自己管理daemon，直接TCP连接查询。
        daemon不存在时自动启动。完全不需要bash exec。

        params: action, file_path, line, column, workdir, content(optional)
        """
        import socket as _sock
        import json as _j
        import time

        action = params.get("action", "")
        file_path = params.get("file_path", "")
        line = params.get("line", 1)
        column = params.get("column", 1)
        workdir = params.get("workdir", "")
        content = params.get("content", "")
        req_id = params.get("_request_id", "")

        if not workdir:
            return NestResponse.make_error(req_id, "workdir不能为空")

        port = self._get_lsp_daemon_port(workdir)

        # 检查缓存：30秒内确认存活的daemon跳过ping
        cache = self.__class__._daemon_cache
        cached = cache.get(workdir)
        if cached and (time.monotonic() - cached[1]) < 30:
            pass  # 缓存命中，直接查询
        elif not self._ping_daemon(port):
            # daemon不通，启动新daemon
            if not self._start_daemon(workdir, port):
                return NestResponse.make_result(
                    req_id, {"success": False, "error": f"daemon启动失败 workdir={workdir}"}
                )
            import asyncio

            await asyncio.sleep(0.5)  # 等daemon启动
            if not self._ping_daemon(port):
                await asyncio.sleep(1.0)  # 再等1秒
            cache[workdir] = (port, time.monotonic())

        # TCP查询daemon
        try:
            sock = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            sock.settimeout(180)  # clangd --background-index initialize需要120s+
            sock.connect(("127.0.0.1", port))

            request = {
                "action": action,
                "file_path": file_path,
                "line": line,
                "column": column,
                "workdir": workdir,
            }
            if content:
                request["content"] = content

            sock.sendall((_j.dumps(request) + "\n").encode("utf-8"))
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
                return NestResponse.make_result(
                    req_id, {"success": False, "error": "daemon返回空响应"}
                )

            result = _j.loads(response_text)
            # daemon查询成功，更新缓存
            cache[workdir] = (port, time.monotonic())
            return NestResponse.make_result(req_id, result)

        except Exception as e:
            # 查询失败，清除缓存
            cache.pop(workdir, None)
            return NestResponse.make_result(
                req_id, {"success": False, "error": f"daemon查询失败: {type(e).__name__}: {e}"}
            )

    async def _handle_set_workdir(self, params: dict) -> NestResponse:
        """处理 set_workdir 请求（host → nest: os.chdir + 保存到config/nest.json）

        params:
            workdir: str - 要保存的工作目录路径
            nest_name: str - nest实例名称（用于定位nest.json中的instance）
        """
        import json

        workdir = params.get("workdir", "")
        nest_name = params.get("nest_name", "")

        if not workdir:
            return NestResponse.make_error(params.get("_request_id", ""), "workdir不能为空")

        try:
            # 1. 验证目录是否存在，不存在则拒绝设置
            expanded = Path(workdir).expanduser().resolve()
            if not expanded.is_dir():
                return NestResponse.make_error(
                    params.get("_request_id", ""), f"工作目录不存在: {expanded}"
                )

            # 2. os.chdir 切换实际工作目录
            try:
                os.chdir(str(expanded))
                workdir = str(expanded)
                logger.info(f"远程os.chdir成功: {workdir}")
            except Exception as chdir_err:
                logger.warning(f"远程os.chdir失败: {chdir_err}")
                return NestResponse.make_error(
                    params.get("_request_id", ""), f"切换工作目录失败: {chdir_err}"
                )

            # 2. 保存到 config/nest.json（触发重新注册同步给host）
            project_root = Path(__file__).resolve().parent.parent.parent.parent
            nest_json_path = project_root / "config" / "nest.json"

            if not nest_json_path.exists():
                return NestResponse.make_error(
                    params.get("_request_id", ""), f"配置文件不存在: {nest_json_path}"
                )

            # 读取并更新
            with open(nest_json_path, encoding="utf-8") as f:
                nest_data = json.load(f)

            instances = nest_data.setdefault("instances", {})

            # 🔧 事务#1665 修复：nest_name 为空时禁止 setdefault("", {}) 写入空字符串实例键，
            # 否则 nest restart/start/stop 遍历到 "" 会因任务名回退为默认名而整体失败。
            # 回退策略：优先 config["self"]（本机 nest 身份），其次首个合法实例键。
            target_name = (nest_name or "").strip()
            if not target_name:
                self_name = (nest_data.get("self") or "").strip()
                if self_name and self_name in instances:
                    target_name = self_name
                else:
                    target_name = next(
                        (k for k in instances if isinstance(k, str) and k.strip()), ""
                    )
                if not target_name:
                    return NestResponse.make_error(
                        params.get("_request_id", ""),
                        "nest_name为空且nest.json中无可用实例，无法保存workdir",
                    )
                logger.warning(f"set_workdir nest_name为空，回退使用实例: {target_name}")

            instance = instances.setdefault(target_name, {})
            instance["workdir"] = workdir

            with open(nest_json_path, "w", encoding="utf-8") as f:
                json.dump(nest_data, f, ensure_ascii=False, indent=2)

            logger.info(f"远程workdir已保存到nest.json: {nest_name} -> {workdir}")

            return NestResponse.make_result(
                params.get("_request_id", ""), {"success": True, "workdir": workdir}
            )
        except Exception as e:
            logger.error(f"保存workdir到nest.json失败: {e}\n{traceback.format_exc()}")
            return NestResponse.make_error(params.get("_request_id", ""), str(e))

    # ==================== Shell 命令 ====================

    async def _handle_bash(self, params: dict) -> NestResponse | AsyncIterator[NestResponse]:
        """
        执行 shell 命令

        params:
            command: str - 要执行的命令
            cwd: str - 工作目录（可选）
            timeout: int - 超时时间（毫秒，默认120000）
            env: dict - 环境变量（可选）
            stream: bool - 是否流式输出（可选，默认 false）

        当 stream=true 时，返回 AsyncIterator[NestResponse]，
        逐行发送 stdout/stderr 输出，最后发送带 done=true 的最终结果。
        """
        command = params.get("command", "")
        cwd = params.get("cwd")
        timeout = params.get("timeout", 120000) / 1000  # 毫秒转秒
        use_stream = params.get("stream", False)

        # 安全检查
        if self.security:
            allowed, reason = self.security.check_command(command)
            if not allowed:
                return NestResponse.make_error(
                    params.get("_request_id", ""), f"命令被安全策略拒绝: {reason}"
                )

        if not command:
            return NestResponse.make_error(params.get("_request_id", ""), "命令不能为空")

        # 准备环境
        env = os.environ.copy()
        if params.get("env"):
            env.update(params["env"])

        # 解析工作目录
        workdir = Path(cwd).resolve() if cwd else Path.cwd()

        if use_stream:
            # 流式模式：返回 AsyncIterator
            return self._bash_stream(params, command, workdir, env, timeout)

        # 非流式模式（原有逻辑）
        proc = None
        try:
            if IS_WINDOWS:
                shell_cmd = command
            else:
                shell_cmd = f"/bin/bash -c {shlex.quote(command)}"

            proc = await asyncio.create_subprocess_shell(
                shell_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env=env,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                if proc and proc.returncode is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                return NestResponse.make_error(
                    params.get("_request_id", ""), f"命令执行超时（{int(timeout * 1000)}ms）"
                )

            stdout_text = _decode_output(stdout)
            stderr_text = _decode_output(stderr)

            # 🔧 事务#1737: 输出总量硬上限——防 cat 大文件等巨量输出
            # 撑爆WebSocket响应帧（>50MB触发1009断连死循环）
            # 超限时保留 stdout 头部和 stderr 头部，并加省略标记
            _MAX_BASH_OUTPUT = 8 * 1024 * 1024  # 8MB
            _output_truncated = False
            if len(stdout_text) + len(stderr_text) > _MAX_BASH_OUTPUT:
                _keep = _MAX_BASH_OUTPUT // 2
                if len(stdout_text) > _keep:
                    stdout_text = (
                        stdout_text[:_keep] + "\n...[stdout过大，已截断]...\n"
                    )
                    _output_truncated = True
                if len(stderr_text) > _keep:
                    stderr_text = (
                        stderr_text[:_keep] + "\n...[stderr过大，已截断]...\n"
                    )
                    _output_truncated = True

            result = {
                "stdout": stdout_text,
                "stderr": stderr_text,
                "exit_code": proc.returncode,
            }
            if _output_truncated:
                result["output_truncated"] = True

            return NestResponse.make_result(params.get("_request_id", ""), result)

        except Exception as e:
            return NestResponse.make_error(params.get("_request_id", ""), f"命令执行失败: {e}")
        finally:
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass

    async def _bash_stream(
        self, params: dict, command: str, workdir: Path, env: dict, timeout: float
    ) -> AsyncIterator[NestResponse]:
        """流式执行 bash 命令，逐行发送输出"""
        request_id = params.get("_request_id", "")

        if IS_WINDOWS:
            shell_cmd = command
        else:
            shell_cmd = f"/bin/bash -c {shlex.quote(command)}"

        proc = None
        try:
            proc = await asyncio.create_subprocess_shell(
                shell_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env=env,
            )

            # 并发读取 stdout 和 stderr，逐行发送
            async def _read_lines(stream: asyncio.StreamReader, source: str) -> None:
                """逐行读取并放入队列"""
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace")
                    await queue.put(("line", source, text))

            queue: asyncio.Queue[tuple[str, str, str | int]] = asyncio.Queue()

            # 启动读取任务
            tasks = [
                asyncio.create_task(_read_lines(proc.stdout, "stdout")),
                asyncio.create_task(_read_lines(proc.stderr, "stderr")),
            ]

            # 等待进程结束的任务
            async def _wait_proc():
                await proc.wait()
                await queue.put(("done", "", proc.returncode))

            tasks.append(asyncio.create_task(_wait_proc()))

            # 从队列读取并生成流式响应
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    # 超时
                    for t in tasks:
                        t.cancel()
                    if proc.returncode is None:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    yield NestResponse.make_error(
                        request_id, f"命令执行超时（{int(timeout * 1000)}ms）"
                    )
                    return

                try:
                    msg_type, source, data = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    for t in tasks:
                        t.cancel()
                    if proc.returncode is None:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    yield NestResponse.make_error(
                        request_id, f"命令执行超时（{int(timeout * 1000)}ms）"
                    )
                    return

                if msg_type == "line":
                    yield NestResponse.make_stream(
                        request_id,
                        {"source": source, "data": data},
                        done=False,
                    )
                elif msg_type == "done":
                    # 进程结束，发送最终结果
                    for t in tasks:
                        t.cancel()
                    yield NestResponse.make_stream(
                        request_id,
                        {"source": "exit", "data": str(data)},
                        done=True,
                    )
                    return
        except Exception as e:
            yield NestResponse.make_error(request_id, f"命令执行失败: {e}")
        finally:
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass

    # ==================== 文件操作 ====================

    async def _handle_read(self, params: dict) -> NestResponse:
        """
        读取文件或目录

        params:
            path: str - 文件/目录路径
            offset: int - 起始行号（从1开始，默认1）
            limit: int - 最大行数（默认2000）
        """
        file_path = params.get("path", "")
        offset = params.get("offset", 1)
        limit = params.get("limit", 2000)

        # 安全检查
        if self.security:
            allowed, reason = self.security.check_path_access(file_path)
            if not allowed:
                return NestResponse.make_error(
                    params.get("_request_id", ""), f"路径访问被安全策略拒绝: {reason}"
                )

        path = Path(file_path).expanduser().resolve()
        # 🔧 Windows: resolve()会把映射盘符(P:)解析为UNC路径(\\vnas\public)，保留盘符格式
        if IS_WINDOWS and str(path).startswith("\\\\") and not file_path.startswith("\\\\"):
            path = Path(file_path).expanduser()

        if not path.exists():
            return NestResponse.make_error(
                params.get("_request_id", ""), f"文件未找到: {file_path}"
            )

        if path.is_dir():
            return self._read_directory(path, params)

        # 检查是否为图片/PDF（🔧 补充 mimetypes 不识别的格式如 webp）
        mime, _ = mimetypes.guess_type(str(path))
        _extra_image_extensions = {".webp", ".ico", ".tiff", ".tif", ".avif", ".heic", ".heif"}
        is_image = (
            mime and mime.startswith("image/")
        ) or path.suffix.lower() in _extra_image_extensions
        if is_image or mime == "application/pdf":
            return self._read_binary(path, params)

        return self._read_file(path, offset, limit, params)

    def _read_directory(self, path: Path, params: dict) -> NestResponse:
        """读取目录"""
        try:
            entries = []
            for entry in sorted(path.iterdir(), key=lambda x: x.name):
                is_dir = entry.is_dir() if not entry.is_symlink() else entry.resolve().is_dir()
                name = entry.name
                entry_info = {"name": name, "is_dir": is_dir}
                try:
                    stat = entry.stat()
                    entry_info["size"] = None if is_dir else stat.st_size
                    entry_info["mtime"] = stat.st_mtime
                except (OSError, ValueError):
                    entry_info["size"] = None
                    entry_info["mtime"] = None
                entries.append(entry_info)

            return NestResponse.make_result(
                params.get("_request_id", ""),
                {
                    "type": "directory",
                    "path": str(path),
                    "entries": entries,
                },
            )
        except PermissionError:
            return NestResponse.make_error(params.get("_request_id", ""), f"权限被拒绝: {path}")

    def _read_file(self, path: Path, offset: int, limit: int, params: dict) -> NestResponse:
        """读取文本文件"""
        try:
            text = _read_text_safe(path)
            all_lines = text.splitlines(keepends=True)
        except Exception as e:
            return NestResponse.make_error(params.get("_request_id", ""), f"读取文件失败: {e}")

        start = offset - 1
        raw = []
        total_chars = 0
        truncated = False
        for i, line in enumerate(all_lines):
            if i < start:
                continue
            if len(raw) >= limit:
                break
            # 🔧 事务#1737: 单行与总量上限——防超大单行文件(如base64/压缩数据)
            # 撑爆WebSocket响应帧（>50MB触发1009断连死循环）
            if len(line) > 2000:
                line = line[:2000] + f"...(截断,原长度{len(line)})"
            total_chars += len(line)
            if total_chars > 4 * 1024 * 1024:
                truncated = True
                break
            raw.append(line.rstrip("\n\r"))

        # 带行号
        result_lines = [f"{i + offset}: {line}" for i, line in enumerate(raw)]

        return NestResponse.make_result(
            params.get("_request_id", ""),
            {
                "type": "file",
                "path": str(path),
                "lines": result_lines,
                "total_lines": len(all_lines),
                "start": offset,
                "end": offset + len(result_lines) - 1,
                "truncated": truncated,
            },
        )

    def _read_binary(self, path: Path, params: dict) -> NestResponse:
        """读取二进制文件（图片/PDF）"""
        # 🔧 事务#1737: 单响应硬上限。超大文件整体base64会让响应帧超过
        # bridge 的 max_size(50MB)，触发 code=1009 断连，并陷入
        # "重连→重发→再超限"死循环，拖垮整个 nest（epyc 不可用事故根因）
        _MAX_BINARY_READ_BYTES = 20 * 1024 * 1024  # 20MB
        try:
            file_size = path.stat().st_size
            if file_size > _MAX_BINARY_READ_BYTES:
                return NestResponse.make_error(
                    params.get("_request_id", ""),
                    f"文件过大无法整体读取: {path} "
                    f"({file_size / 1048576:.1f}MB > 20MB)。"
                    f"请用 read_chunks 分块读取，或压缩/裁剪后再试",
                )
            with open(path, "rb") as f:
                file_bytes = f.read()

            mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            base64_data = base64.b64encode(file_bytes).decode("utf-8")

            return NestResponse.make_result(
                params.get("_request_id", ""),
                {
                    "type": "binary",
                    "path": str(path),
                    "mime": mime,
                    "data": base64_data,
                    "size": len(file_bytes),
                },
            )
        except Exception as e:
            return NestResponse.make_error(params.get("_request_id", ""), f"读取文件失败: {e}")

    async def _handle_write(self, params: dict) -> NestResponse:
        """
        写入文件

        params:
            path: str - 文件路径
            content: str - 文件内容
        """
        file_path = params.get("path", "")
        content = params.get("content", "")

        # 安全检查
        if self.security:
            allowed, reason = self.security.check_path_access(file_path)
            if not allowed:
                return NestResponse.make_error(
                    params.get("_request_id", ""), f"路径访问被安全策略拒绝: {reason}"
                )

        path = Path(file_path).expanduser().resolve()

        # 获取旧文件行数和内容（如果文件已存在）
        old_total_lines = 0
        old_content = ""
        is_new_file = not path.exists()
        if not is_new_file:
            try:
                old_content = _read_text_safe(path)
                old_total_lines = len(old_content.splitlines())
            except Exception:
                pass

        try:
            ensure_dir_with_owner(path.parent)
            _write_text_safe(path, content, original_path=str(path) if path.exists() else None)

            new_total_lines = len(content.splitlines()) if content else 0

            return NestResponse.make_result(
                params.get("_request_id", ""),
                {
                    "path": str(path),
                    "written": True,
                    "size": len(content.encode("utf-8")),
                    "is_new": is_new_file,
                    "old_total_lines": old_total_lines,
                    "new_total_lines": new_total_lines,
                    "old_content": old_content,
                },
            )
        except Exception as e:
            return NestResponse.make_error(params.get("_request_id", ""), f"写入文件失败: {e}")

    async def _handle_write_bytes(self, params: dict) -> NestResponse:
        """
        分块写入二进制文件（base64 编码传输，二进制安全）

        params:
            path: str - 文件路径
            data_b64: str - base64 编码的数据块（可为空）
            mode: str - 写入模式 "create"（默认，新建/覆盖）或 "append"（追加）
            mtime: float - 可选，写入后设置文件修改时间（同步保序用）
        """
        import base64 as _b64

        file_path = params.get("path", "")
        data_b64 = params.get("data_b64", "")
        mode = params.get("mode", "create")

        # 安全检查
        if self.security:
            allowed, reason = self.security.check_path_access(file_path)
            if not allowed:
                return NestResponse.make_error(
                    params.get("_request_id", ""), f"路径访问被安全策略拒绝: {reason}"
                )

        path = Path(file_path).expanduser().resolve()

        try:
            data = _b64.b64decode(data_b64) if data_b64 else b""
            ensure_dir_with_owner(path.parent)
            if mode == "append":
                with open(path, "ab") as f:
                    f.write(data)
            else:
                with open(path, "wb") as f:
                    f.write(data)
            chown_to_project_owner(path)

            # 可选：恢复源文件 mtime（用于增量同步比较）
            mtime = params.get("mtime")
            if mtime is not None:
                try:
                    os.utime(path, (float(mtime), float(mtime)))
                except Exception:
                    pass

            return NestResponse.make_result(
                params.get("_request_id", ""),
                {
                    "path": str(path),
                    "written": True,
                    "size": len(data),
                    "mode": mode,
                },
            )
        except Exception as e:
            return NestResponse.make_error(params.get("_request_id", ""), f"写入文件失败: {e}")

    async def _handle_edit(self, params: dict) -> NestResponse:
        """
        编辑文件（精确替换）

        params:
            path: str - 文件路径
            oldString: str - 要替换的文本
            newString: str - 替换后的文本
            replace_all: bool - 是否替换所有出现
        """
        file_path = params.get("path", "")
        oldString = params.get("oldString", "")
        newString = params.get("newString", "")
        replace_all = params.get("replace_all", False)

        # 安全检查
        if self.security:
            allowed, reason = self.security.check_path_access(file_path)
            if not allowed:
                return NestResponse.make_error(
                    params.get("_request_id", ""), f"路径访问被安全策略拒绝: {reason}"
                )

        path = Path(file_path).expanduser().resolve()

        if not path.exists():
            return NestResponse.make_error(
                params.get("_request_id", ""), f"文件未找到: {file_path}"
            )

        try:
            content = _read_text_safe(path)

            if oldString == newString:
                return NestResponse.make_error(
                    params.get("_request_id", ""),
                    "oldString 和 newString 相同，无需更改。请确认你的edit工具调用参数是否正确！或者使用line_edit行编辑工具。",
                )

            # 统一换行符：将文件内容和 oldString/newString 都规范化为 \n 进行匹配，
            # 避免文件 CRLF 与 LLM 发送的 LF 不匹配
            file_has_crlf = "\r\n" in content
            norm_content = content.replace("\r\n", "\n")
            norm_old = oldString.replace("\r\n", "\n")
            norm_new = newString.replace("\r\n", "\n")

            if norm_old not in norm_content:
                # 🔧 事务#2083：精确匹配失败时回退到容错匹配链（与本地
                # EditTool._execute_local_edit 的 replace_content 同源），
                # 自动修复 LLM 构造 oldString 时的缩进/空白类细微偏差。
                # 事务#2080事故：51行oldString仅1行差1个前导空格，被此处
                # 单次精确匹配直接拒绝；而本地路径的 line_trimmed 等4层
                # 容错本可唯一匹配自动修复——远程路径此前完全没有容错。
                try:
                    from peng.tools.file import ToolError, replace_content

                    norm_content = replace_content(
                        norm_content, norm_old, norm_new, replace_all
                    )
                    count = 1  # 容错链匹配串动态变化，条数按单处替换上报
                except ImportError:
                    return NestResponse.make_error(
                        params.get("_request_id", ""),
                        "在文件中未找到 oldString。请精确指定空格、制表符、换行符等内容。或者使用line_edit行编辑工具。",
                    )
                except ToolError as e:
                    return NestResponse.make_error(
                        params.get("_request_id", ""),
                        f"{e}。或者使用line_edit行编辑工具。",
                    )
            else:
                # 检查是否有多处匹配
                count = norm_content.count(norm_old)
                if count > 1 and not replace_all:
                    return NestResponse.make_error(
                        params.get("_request_id", ""),
                        f"oldString找到 {count} 处匹配，请提供更多上下文或使用 replaceAll。或者使用line_edit行编辑工具。",
                    )

                # 执行替换（在规范化后的内容上）
                if replace_all:
                    norm_content = norm_content.replace(norm_old, norm_new)
                else:
                    norm_content = norm_content.replace(norm_old, norm_new, 1)

            # 如果原文件是 CRLF，将结果转回 CRLF
            if file_has_crlf:
                norm_content = norm_content.replace("\n", "\r\n")

            _write_text_safe(path, norm_content, original_path=str(path))

            old_total_lines = len(content.splitlines())
            new_total_lines = len(norm_content.splitlines())

            return NestResponse.make_result(
                params.get("_request_id", ""),
                {
                    "path": str(path),
                    "edited": True,
                    "replacements": count if replace_all else 1,
                    "old_total_lines": old_total_lines,
                    "new_total_lines": new_total_lines,
                },
            )
        except Exception as e:
            return NestResponse.make_error(params.get("_request_id", ""), f"编辑文件失败: {e}")

    async def _handle_line_edit(self, params: dict) -> NestResponse:
        """
        按行编辑文件（删除/替换/插入）

        params:
            path: str - 文件路径
            operation: str - 操作类型: "delete" | "replace" | "insert"
            start_line: int - 起始行号（1-based，包含），delete/replace 必填
            end_line: int - 结束行号（1-based，包含），delete/replace 必填
            line: int - 目标行号（1-based），insert 必填
            content: str - 新内容，replace/insert 必填
            position: str - 插入位置: "before" | "after"（默认 "after"），insert 时有效
        """
        file_path = params.get("path", "")
        operation = params.get("operation", "")

        # 归一化：LLM 有时用 <br> 代替换行符，统一替换为 \n
        raw_content = params.get("content", "")
        if raw_content:
            params["content"] = re.sub(r"<br\s*/?>", "\n", raw_content)

        # 安全检查
        if self.security:
            allowed, reason = self.security.check_path_access(file_path)
            if not allowed:
                return NestResponse.make_error(
                    params.get("_request_id", ""), f"路径访问被安全策略拒绝: {reason}"
                )

        path = Path(file_path).expanduser().resolve()

        if not path.exists():
            return NestResponse.make_error(
                params.get("_request_id", ""), f"文件未找到: {file_path}"
            )

        try:
            content_raw = _read_text_safe(path)
            # 保留原始换行符信息
            line_ending = "\r\n" if "\r\n" in content_raw else "\n"
            had_trailing_newline = content_raw.endswith(("\n", "\r"))
            lines = content_raw.splitlines()

            old_total = len(lines)

            # 编辑前行快照（#1559重复检测用：检测邻接旧行需用编辑前状态）
            lines_before_snapshot = list(lines)

            # 统一初始化所有行号变量，避免 delete/replace 分支无 line、insert 分支无 start_line/end_line 导致 NameError
            start_line = end_line = line = None
            affected_lines: list[str] = []

            if operation == "delete":
                start_line = params.get("start_line")
                end_line = params.get("end_line")
                if start_line is None or end_line is None:
                    return NestResponse.make_error(
                        params.get("_request_id", ""), "delete 操作需要 start_line 和 end_line 参数"
                    )
                start_line = int(start_line)
                end_line = int(end_line)
                if start_line < 1 or end_line > old_total or start_line > end_line:
                    return NestResponse.make_error(
                        params.get("_request_id", ""),
                        f"行号范围无效: start_line={start_line}, end_line={end_line}, 文件共 {old_total} 行",
                    )
                del lines[start_line - 1 : end_line]
                action_desc = (
                    f"删除第 {start_line}-{end_line} 行（共 {end_line - start_line + 1} 行）"
                )
                affected_lines = []

            elif operation == "replace":
                start_line = params.get("start_line")
                end_line = params.get("end_line")
                new_content = params.get("content", "")
                if start_line is None or end_line is None:
                    return NestResponse.make_error(
                        params.get("_request_id", ""),
                        "replace 操作需要 start_line 和 end_line 参数",
                    )
                start_line = int(start_line)
                end_line = int(end_line)
                if start_line < 1 or end_line > old_total or start_line > end_line:
                    return NestResponse.make_error(
                        params.get("_request_id", ""),
                        f"行号范围无效: start_line={start_line}, end_line={end_line}, 文件共 {old_total} 行",
                    )
                affected_lines = new_content.split("\n") if new_content else []
                lines[start_line - 1 : end_line] = affected_lines
                action_desc = (
                    f"替换第 {start_line}-{end_line} 行为 {len(affected_lines)} 行新内容"
                )

            elif operation == "insert":
                line = params.get("line")
                new_content = params.get("content", "")
                position = params.get("position", "after")
                if line is None:
                    return NestResponse.make_error(
                        params.get("_request_id", ""), "insert 操作需要 line 参数"
                    )
                line = int(line)
                if line < 1 or line > old_total:
                    return NestResponse.make_error(
                        params.get("_request_id", ""),
                        f"行号无效: line={line}, 文件共 {old_total} 行",
                    )
                affected_lines = new_content.split("\n") if new_content else []
                if position == "before":
                    insert_pos = line - 1
                else:
                    insert_pos = line
                lines[insert_pos:insert_pos] = affected_lines
                action_desc = f"在第 {line} 行{('前' if position == 'before' else '后')}插入 {len(affected_lines)} 行"

            else:
                return NestResponse.make_error(
                    params.get("_request_id", ""),
                    f"不支持的操作类型: {operation}（支持: delete, replace, insert）",
                )

            new_total = len(lines)

            # 重复内容检测（#1559）：content边界/内部与旧文件邻接行的疑似重复，
            # 成功编辑后告警不阻断，帮助LLM立即发现参数错误
            dup_warning: str | None = None
            try:
                from peng.common.line_edit_check import detect_line_edit_duplication

                dup_warning = detect_line_edit_duplication(
                    lines_before=lines_before_snapshot,
                    operation=operation,
                    start_line=start_line,
                    end_line=end_line,
                    insert_line=line,
                    affected_lines=affected_lines,
                    position=params.get("position", "after"),
                )
            except Exception as e:
                logger.debug(f"line_edit 重复检测失败(不影响编辑): {e}")

            # 写回文件
            new_content_raw = line_ending.join(lines)
            if had_trailing_newline and new_content_raw:
                new_content_raw += line_ending

            # 在写回前构建 context_preview，即使构建失败也不影响文件已正确编辑
            try:
                context_preview = _build_line_edit_context(
                    lines, operation, start_line, end_line, line, affected_lines
                )
            except Exception:
                context_preview = []

            _write_text_safe(path, new_content_raw, original_path=str(path))

            if dup_warning:
                action_desc += f"\n{dup_warning}"

            return NestResponse.make_result(
                params.get("_request_id", ""),
                {
                    "path": str(path),
                    "operation": operation,
                    "description": action_desc,
                    "warning": dup_warning,
                    "old_total_lines": old_total,
                    "new_total_lines": new_total,
                    "old_content": content_raw,
                    "new_content": new_content_raw,
                    "context_preview": context_preview,
                },
            )
        except Exception as e:
            return NestResponse.make_error(params.get("_request_id", ""), f"按行编辑失败: {e}")

    async def _handle_stat(self, params: dict) -> NestResponse:
        """
        获取文件信息

        params:
            path: str - 文件路径
        """
        file_path = params.get("path", "")

        path = Path(file_path).expanduser().resolve()

        if not path.exists():
            return NestResponse.make_error(
                params.get("_request_id", ""), f"文件未找到: {file_path}"
            )

        try:
            stat = path.stat()
            return NestResponse.make_result(
                params.get("_request_id", ""),
                {
                    "path": str(path),
                    "is_dir": path.is_dir(),
                    "is_file": path.is_file(),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "mode": stat.st_mode,
                },
            )
        except Exception as e:
            return NestResponse.make_error(params.get("_request_id", ""), f"获取文件信息失败: {e}")

    async def _handle_read_bytes(self, params: dict) -> NestResponse:
        """
        字节级分块读取文件（base64 编码传输，二进制安全）

        params:
            path: str - 文件路径
            offset: int - 字节偏移（默认0）
            chunk_size: int - 读取字节数（默认1MB）
            encoding: str - 可选，"base64" 时返回 base64 编码（二进制安全）；
                          未指定时保持旧行为返回 utf-8 文本（WebUI read-chunk 兼容）
        """
        import base64 as _b64

        file_path = params.get("path", "")
        offset = params.get("offset", 0)
        chunk_size = params.get("chunk_size", 1048576)
        use_b64 = params.get("encoding") == "base64"

        path = Path(file_path).expanduser().resolve()

        if not path.exists() or not path.is_file():
            return NestResponse.make_error(
                params.get("_request_id", ""), f"文件不存在: {file_path}"
            )

        try:
            file_size = path.stat().st_size
            if use_b64:
                # 二进制安全：直接按字节读取并 base64 编码
                with open(path, "rb") as f:
                    f.seek(offset)
                    raw = f.read(chunk_size)
                actual_size = len(raw)
                return NestResponse.make_result(
                    params.get("_request_id", ""),
                    {
                        "content_b64": _b64.b64encode(raw).decode("ascii"),
                        "path": str(path),
                        "offset": offset,
                        "chunk_size": actual_size,
                        "total_size": file_size,
                        "has_more": (offset + actual_size) < file_size,
                    },
                )
            # 兼容旧行为：文本模式（WebUI read-chunk 使用）
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                content = f.read(chunk_size)
                actual_size = len(content.encode("utf-8"))

            return NestResponse.make_result(
                params.get("_request_id", ""),
                {
                    "content": content,
                    "path": str(path),
                    "offset": offset,
                    "chunk_size": actual_size,
                    "total_size": file_size,
                    "has_more": (offset + actual_size) < file_size,
                },
            )
        except Exception as e:
            return NestResponse.make_error(params.get("_request_id", ""), f"分块读取失败: {e}")

    async def _handle_list_dir(self, params: dict) -> NestResponse:
        """
        列出目录内容

        params:
            path: str - 目录路径
        """
        return await self._handle_read(params)

    async def _handle_list_tree(self, params: dict) -> NestResponse:
        """
        递归列出目录树（sync 同步用）

        params:
            path: str - 目录路径
        返回:
            files: [{path, size, mtime}] 相对路径列表（仅文件）
            total: 文件总数
            total_size: 总字节数
        """
        base = Path(params.get("path", "")).expanduser().resolve()

        # 安全检查
        if self.security:
            allowed, reason = self.security.check_path_access(str(base))
            if not allowed:
                return NestResponse.make_error(
                    params.get("_request_id", ""), f"路径访问被安全策略拒绝: {reason}"
                )

        if not base.exists():
            return NestResponse.make_result(
                params.get("_request_id", ""),
                {"exists": False, "files": [], "total": 0, "total_size": 0},
            )
        if not base.is_dir():
            return NestResponse.make_error(
                params.get("_request_id", ""), f"不是目录: {base}"
            )

        files = []
        total_size = 0
        # os.walk 收集所有文件（跳过符号链接避免循环）
        for root, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if not Path(root, d).is_symlink()]
            for name in names:
                p = Path(root) / name
                try:
                    if p.is_symlink():
                        continue
                    st = p.stat()
                except OSError:
                    continue
                rel = p.relative_to(base).as_posix()
                files.append({"path": rel, "size": st.st_size, "mtime": st.st_mtime})
                total_size += st.st_size

        return NestResponse.make_result(
            params.get("_request_id", ""),
            {
                "exists": True,
                "path": str(base),
                "files": files,
                "total": len(files),
                "total_size": total_size,
            },
        )

    async def _handle_delete(self, params: dict) -> NestResponse:
        """
        删除文件或目录（sync --delete 用）

        params:
            path: str - 文件或目录路径
        """
        target = Path(params.get("path", "")).expanduser().resolve()

        # 安全检查
        if self.security:
            allowed, reason = self.security.check_path_access(str(target))
            if not allowed:
                return NestResponse.make_error(
                    params.get("_request_id", ""), f"路径访问被安全策略拒绝: {reason}"
                )

        if not target.exists():
            return NestResponse.make_result(
                params.get("_request_id", ""), {"path": str(target), "deleted": False, "note": "不存在"}
            )

        try:
            if target.is_dir() and not target.is_symlink():
                import shutil

                shutil.rmtree(target)
            else:
                target.unlink()
            return NestResponse.make_result(
                params.get("_request_id", ""), {"path": str(target), "deleted": True}
            )
        except Exception as e:
            return NestResponse.make_error(params.get("_request_id", ""), f"删除失败: {e}")

    async def _handle_list_drives(self, params: dict) -> NestResponse:
        """
        列出可用盘符（Windows平台专用）

        Returns:
            盘符列表，如 ["C:/", "D:/"]
        """
        import string

        if not IS_WINDOWS:
            return NestResponse.make_error(
                params.get("_request_id", ""), "list_drives 仅支持 Windows 平台"
            )

        try:
            drives = []
            for letter in string.ascii_uppercase:
                drive_path = f"{letter}:\\"
                if os.path.exists(drive_path):
                    drives.append(f"{letter}:/")

            return NestResponse.make_result(
                params.get("_request_id", ""),
                {
                    "type": "drives",
                    "drives": drives,
                },
            )
        except Exception as e:
            return NestResponse.make_error(params.get("_request_id", ""), f"列出盘符失败: {e}")

    # ==================== 搜索 ====================

    async def _handle_glob(self, params: dict) -> NestResponse:
        """
        Glob 模式匹配

        params:
            pattern: str - glob 模式
            path: str - 搜索目录（可选，默认当前目录）
        """
        pattern = params.get("pattern", "")
        search_path = params.get("path", ".")

        # 安全检查
        if self.security:
            allowed, reason = self.security.check_path_access(search_path)
            if not allowed:
                return NestResponse.make_error(
                    params.get("_request_id", ""), f"路径访问被安全策略拒绝: {reason}"
                )

        base_path = Path(search_path).expanduser().resolve()

        if not base_path.exists():
            return NestResponse.make_error(
                params.get("_request_id", ""), f"目录不存在: {search_path}"
            )

        try:
            matches = list(base_path.rglob(pattern))

            # 使用 .gitignore 过滤
            from peng.common.gitignore import GitIgnoreFilter

            gitignore_filter = GitIgnoreFilter(base_path)
            if gitignore_filter.has_gitignore:
                matches = gitignore_filter.filter_paths(matches)

            # 排序（按修改时间）
            matches_with_mtime = []
            for m in matches:
                try:
                    matches_with_mtime.append((m, m.stat().st_mtime))
                except OSError:
                    continue

            matches_with_mtime.sort(key=lambda x: x[1], reverse=True)

            # 格式化输出
            result = []
            for match, _ in matches_with_mtime[:100]:
                try:
                    rel_path = match.relative_to(base_path)
                    result.append(str(rel_path))
                except ValueError:
                    result.append(str(match))

            return NestResponse.make_result(
                params.get("_request_id", ""),
                {
                    "matches": result,
                    "total": len(matches_with_mtime),
                },
            )
        except Exception as e:
            return NestResponse.make_error(params.get("_request_id", ""), f"glob 匹配失败: {e}")

    async def _handle_grep(self, params: dict) -> NestResponse:
        """
        正则搜索文件内容

        params:
            pattern: str - 正则表达式
            path: str - 搜索路径（文件或目录）
            glob: str - 文件模式过滤（可选）
            ignore_case: bool - 忽略大小写（默认false）
            max_matches: int - 最大匹配数（默认100）
        """
        pattern = params.get("pattern", "")
        search_path = params.get("path", ".")
        glob_pattern = params.get("glob")
        ignore_case = params.get("ignore_case", False)
        max_matches = params.get("max_matches", 100)

        # 安全检查
        if self.security:
            allowed, reason = self.security.check_path_access(search_path)
            if not allowed:
                return NestResponse.make_error(
                    params.get("_request_id", ""), f"路径访问被安全策略拒绝: {reason}"
                )

        base_path = Path(search_path).expanduser().resolve()

        if not base_path.exists():
            return NestResponse.make_error(
                params.get("_request_id", ""), f"路径不存在: {search_path}"
            )

        # 编译正则
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return NestResponse.make_error(params.get("_request_id", ""), f"无效的正则表达式: {e}")

        # 收集文件
        if base_path.is_file():
            files = [base_path]
        else:
            if glob_pattern:
                files = list(base_path.rglob(glob_pattern))
            else:
                files = [f for f in base_path.rglob("*") if f.is_file()]

        # 使用 .gitignore 过滤
        if base_path.is_dir():
            from peng.common.gitignore import GitIgnoreFilter

            gitignore_filter = GitIgnoreFilter(base_path)
            if gitignore_filter.has_gitignore:
                files = gitignore_filter.filter_paths(files)

        # 搜索
        ignore_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv"}
        matches = []
        files_with_matches = set()

        for f in files:
            if any(part in ignore_dirs for part in f.parts):
                continue
            try:
                with open(f, encoding="utf-8", errors="ignore") as fh:
                    for line_num, line_text in enumerate(fh, 1):
                        if regex.search(line_text):
                            files_with_matches.add(str(f))
                            if len(matches) < max_matches:
                                try:
                                    rel_path = f.relative_to(base_path)
                                except ValueError:
                                    rel_path = f
                                matches.append(f"{rel_path}:{line_num}:{line_text.rstrip()}")
            except Exception:
                continue

        return NestResponse.make_result(
            params.get("_request_id", ""),
            {
                "matches": matches,
                "files_count": len(files_with_matches),
                "total_matches": len(matches),
            },
        )

    # ==================== 窗口管理 ====================

    async def _handle_list_windows(self, params: dict) -> NestResponse:
        """
        列出窗口

        params:
            query: str - 查询条件（程序名或窗口标题，可选）
        """
        if self.security:
            allowed, reason = self.security.check_window_query()
            if not allowed:
                return NestResponse.make_error(
                    params.get("_request_id", ""), f"窗口查询被安全策略拒绝: {reason}"
                )

        try:
            windows = self._get_windows(params.get("query", ""))
            return NestResponse.make_result(params.get("_request_id", ""), {"windows": windows})
        except Exception as e:
            return NestResponse.make_error(params.get("_request_id", ""), f"查询窗口失败: {e}")

    def _get_windows(self, query: str = "") -> list[dict]:
        """获取窗口列表"""
        if IS_WINDOWS:
            return self._get_windows_windows(query)
        else:
            return self._get_windows_linux(query)

    def _get_windows_windows(self, query: str = "") -> list[dict]:
        """Windows 平台获取窗口列表"""
        try:
            import ctypes
            from ctypes import wintypes

            EnumWindows = ctypes.windll.user32.EnumWindows
            GetWindowTextW = ctypes.windll.user32.GetWindowTextW
            GetWindowThreadProcessId = ctypes.windll.user32.GetWindowThreadProcessId
            IsWindowVisible = ctypes.windll.user32.IsWindowVisible

            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            windows = []

            def enum_callback(hwnd, lparam):
                if IsWindowVisible(hwnd):
                    length = 256
                    title = ctypes.create_unicode_buffer(length)
                    GetWindowTextW(hwnd, title, length)

                    pid = wintypes.DWORD()
                    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

                    title_str = title.value.strip()
                    if title_str:
                        win_info = {
                            "hwnd": hwnd,
                            "title": title_str,
                            "pid": pid.value,
                            "visible": True,
                        }

                        # 过滤
                        if query:
                            if query.lower() not in title_str.lower():
                                try:
                                    import psutil

                                    proc = psutil.Process(pid.value)
                                    if query.lower() not in proc.name().lower():
                                        return True
                                except Exception:
                                    return True

                        windows.append(win_info)
                return True

            EnumWindows(WNDENUMPROC(enum_callback), 0)
            return windows

        except Exception as e:
            logger.error(f"获取Windows窗口列表失败: {e}")
            return []

    def _get_windows_linux(self, query: str = "") -> list[dict]:
        """Linux 平台获取窗口列表"""
        try:
            result = subprocess.run(
                ["wmctrl", "-l", "-p"], capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                return []

            windows = []
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split(None, 5)
                if len(parts) >= 5:
                    win_info = {
                        "window_id": parts[0],
                        "workspace": int(parts[1]),
                        "pid": int(parts[2]),
                        "hostname": parts[3],
                        "title": parts[4] if len(parts) == 5 else parts[4] + " " + parts[5],
                    }

                    if query:
                        if query.lower() not in win_info["title"].lower():
                            continue

                    windows.append(win_info)

            return windows
        except FileNotFoundError:
            logger.warning("wmctrl 未安装，无法列出窗口")
            return []
        except Exception as e:
            logger.error(f"获取Linux窗口列表失败: {e}")
            return []

    async def _handle_screenshot(self, params: dict) -> NestResponse:
        """
        截屏

        params:
            window: str - 窗口标题或ID（可选，不指定则全屏）
            region: dict - 截取区域 {"x":0,"y":0,"width":800,"height":600}（可选）
            format: str - 输出格式 "png"（默认）或 "jpg"
            save_path: str - 保存路径（可选，指定则保存文件到 nest 端并返回路径）
        """
        if self.security:
            allowed, reason = self.security.check_screenshot()
            if not allowed:
                return NestResponse.make_error(
                    params.get("_request_id", ""), f"截屏被安全策略拒绝: {reason}"
                )

        try:
            import time as _time

            output_format = params.get("format", "png")
            window = params.get("window")
            region = params.get("region")
            save_path = params.get("save_path")

            result = self._take_screenshot(window, region, output_format)

            # _take_screenshot 现在返回 (bytes, None) 或 (None, error_msg)
            if isinstance(result, tuple):
                screenshot_data, error_msg = result
            else:
                # 兼容旧返回值
                screenshot_data = result
                error_msg = None if result else "未知截屏错误"

            if screenshot_data is None:
                return NestResponse.make_error(
                    params.get("_request_id", ""), f"截屏失败: {error_msg}"
                )

            mime = f"image/{output_format}"
            size = len(screenshot_data)

            # 如果指定了保存路径，保存到 nest 端文件系统
            if save_path:
                # 相对路径基于 nest 端 project_root 解析
                save_path_obj = Path(save_path)
                if not save_path_obj.is_absolute():
                    project_root = Path(__file__).resolve().parent.parent.parent.parent
                    save_path_obj = project_root / save_path
                save_path = str(save_path_obj)

                save_dir = Path(save_path).parent
                ensure_dir_with_owner(save_dir)
                with open(save_path, "wb") as f:
                    f.write(screenshot_data)
                chown_to_project_owner(save_path)
                logger.info(f"截屏已保存到 nest 端: {save_path} ({size} bytes)")

                return NestResponse.make_result(
                    params.get("_request_id", ""),
                    {
                        "file_path": str(save_path),
                        "mime": mime,
                        "format": output_format,
                        "size": size,
                    },
                )

            # 兼容旧模式：返回 base64 数据
            base64_data = base64.b64encode(screenshot_data).decode("utf-8")
            return NestResponse.make_result(
                params.get("_request_id", ""),
                {
                    "data": base64_data,
                    "mime": mime,
                    "format": output_format,
                    "size": size,
                },
            )
        except Exception as e:
            import traceback as _tb

            error_detail = f"{e}\n{_tb.format_exc()}"
            logger.error(f"截屏异常: {error_detail}")
            return NestResponse.make_error(params.get("_request_id", ""), f"截屏失败: {e}")

    def _take_screenshot(
        self, window: str | None = None, region: dict | None = None, fmt: str = "png"
    ) -> tuple[bytes | None, str | None]:
        """执行截屏，返回 (data, error_msg)"""
        if IS_WINDOWS:
            return self._take_screenshot_windows(window, region, fmt)
        else:
            return self._take_screenshot_linux(window, region, fmt)

    def _take_screenshot_windows(
        self, window: str | None = None, region: dict | None = None, fmt: str = "png"
    ) -> tuple[bytes | None, str | None]:
        """Windows 截屏，返回 (data, error_msg)

        优先使用 ImageGrab，失败时回退到 Win32 GDI API 截屏。
        """
        # 尝试方式1: PIL ImageGrab
        try:
            from PIL import ImageGrab as _IG

            data = self._screenshot_via_imagegrab(_IG, window, region, fmt)
            if data is not None:
                return data, None
        except ImportError:
            pass  # Pillow未安装，尝试GDI
        except Exception as e:
            logger.debug(f"ImageGrab截屏失败，尝试GDI回退: {e}")

        # 尝试方式2: Win32 GDI API（不依赖Pillow的桌面会话要求）
        try:
            data = self._screenshot_via_gdi(window, region, fmt)
            if data is not None:
                return data, None
        except Exception as e:
            logger.debug(f"GDI截屏也失败: {e}")

        return None, "所有截屏方式均失败（ImageGrab和GDI API）"

    def _screenshot_via_imagegrab(self, ImageGrab, window, region, fmt):
        """使用PIL ImageGrab截屏"""
        if window:
            import ctypes
            from ctypes import wintypes

            hwnd = ctypes.windll.user32.FindWindowW(None, window)
            if not hwnd:
                return None
            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            bbox = (rect.left, rect.top, rect.right, rect.bottom)
            if region:
                bbox = (
                    bbox[0] + region.get("x", 0),
                    bbox[1] + region.get("y", 0),
                    bbox[0] + region.get("x", 0) + region.get("width", bbox[2] - bbox[0]),
                    bbox[1] + region.get("y", 0) + region.get("height", bbox[3] - bbox[1]),
                )
            img = ImageGrab.grab(bbox=bbox)
        else:
            if region:
                bbox = (
                    region.get("x", 0),
                    region.get("y", 0),
                    region.get("x", 0) + region.get("width", 1920),
                    region.get("y", 0) + region.get("height", 1080),
                )
                img = ImageGrab.grab(bbox=bbox)
            else:
                img = ImageGrab.grab()

        import io

        buf = io.BytesIO()
        img.save(buf, format="PNG" if fmt == "png" else "JPEG")
        return buf.getvalue()

    def _screenshot_via_gdi(self, window, region, fmt):
        """使用Win32 GDI API截屏（不依赖PIL ImageGrab的桌面会话）"""
        import ctypes
        import struct
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        # 获取屏幕DC
        screen_w = user32.GetSystemMetrics(0)  # SM_CXSCREEN
        screen_h = user32.GetSystemMetrics(1)  # SM_CYSCREEN

        if screen_w <= 0 or screen_h <= 0:
            return None

        src_dc = user32.GetDC(0)
        if not src_dc:
            return None

        try:
            mem_dc = gdi32.CreateCompatibleDC(src_dc)
            bitmap = gdi32.CreateCompatibleBitmap(src_dc, screen_w, screen_h)
            gdi32.SelectObject(mem_dc, bitmap)

            # BitBlt 拷贝屏幕
            gdi32.BitBlt(mem_dc, 0, 0, screen_w, screen_h, src_dc, 0, 0, 0x00CC0020)  # SRCCOPY

            # 获取位图数据 - BITMAPINFOHEADER (40 bytes)
            # I=biSize, ii=biWidth/biHeight, H=biPlanes, H=biBitCount,
            # I=biCompression, I=biSizeImage, ii=biXPelsPerMeter/biYPelsPerMeter,
            # I=biClrUsed, I=biClrImportant
            bmi = ctypes.create_string_buffer(40)
            struct.pack_into(
                "IiiHHIIiiII",
                bmi,
                0,
                40,  # biSize
                screen_w,  # biWidth
                screen_h,  # biHeight
                1,  # biPlanes
                32,  # biBitCount (BGRA)
                0,  # biCompression (BI_RGB)
                0,  # biSizeImage
                0,  # biXPelsPerMeter
                0,  # biYPelsPerMeter
                0,  # biClrUsed
                0,  # biClrImportant
            )

            pixel_data = ctypes.create_string_buffer(screen_w * screen_h * 4)
            rows = gdi32.GetDIBits(
                mem_dc, bitmap, 0, screen_h, pixel_data, bmi, 0
            )  # DIB_RGB_COLORS

            if rows == 0:
                return None

            # 转换为PNG
            try:
                from PIL import Image

                img = Image.frombytes("RGBA", (screen_w, screen_h), pixel_data.raw, "raw", "BGRA")
                # 处理区域裁剪
                if region:
                    x = region.get("x", 0)
                    y = region.get("y", 0)
                    w = region.get("width", screen_w)
                    h = region.get("height", screen_h)
                    img = img.crop((x, y, x + w, y + h))

                import io

                buf = io.BytesIO()
                save_fmt = "PNG" if fmt == "png" else "JPEG"
                if save_fmt == "JPEG":
                    img = img.convert("RGB")
                img.save(buf, format=save_fmt)
                return buf.getvalue()
            except ImportError:
                # 没有PIL，无法转PNG，返回BMP原始数据
                return None

        finally:
            user32.ReleaseDC(0, src_dc)
            gdi32.DeleteDC(mem_dc)
            gdi32.DeleteObject(bitmap)

    def _take_screenshot_linux(
        self, window: str | None = None, region: dict | None = None, fmt: str = "png"
    ) -> tuple[bytes | None, str | None]:
        """Linux 截屏，返回 (data, error_msg)"""
        try:
            import subprocess

            cmd = ["scrot", "-o", "/dev/stdout"]
            if fmt == "jpg":
                cmd.extend(["--quality", "85"])

            if window:
                # 使用 import 命令（ImageMagick）截取窗口
                cmd = ["import", "-window", window, f"{fmt}:-"]
            elif region:
                geometry = f"{region['width']}x{region['height']}+{region['x']}+{region['y']}"
                cmd = ["import", "-crop", geometry, f"{fmt}:-"]
            else:
                cmd = ["import", "-window", "root", f"{fmt}:-"]

            result = subprocess.run(cmd, capture_output=True, timeout=10)
            if result.returncode == 0 and result.stdout:
                return result.stdout, None

            stderr_text = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
            return None, f"截屏命令失败 (exit={result.returncode}): {stderr_text}"
        except FileNotFoundError as e:
            return None, f"截屏工具未安装: {e}"
        except Exception as e:
            return None, f"Linux截屏异常: {e}"

    # ==================== 技能加载 ====================

    async def _handle_load_skill(self, params: dict) -> NestResponse:
        """
        加载技能文件

        params:
            path: str - 技能文件路径（SKILL.md）
        """
        file_path = params.get("path", "")

        # 安全检查
        if self.security:
            allowed, reason = self.security.check_path_access(file_path)
            if not allowed:
                return NestResponse.make_error(
                    params.get("_request_id", ""), f"路径访问被安全策略拒绝: {reason}"
                )

        path = Path(file_path).expanduser().resolve()

        if not path.exists():
            return NestResponse.make_error(
                params.get("_request_id", ""), f"技能文件未找到: {file_path}"
            )

        try:
            content = path.read_text(encoding="utf-8")
            return NestResponse.make_result(
                params.get("_request_id", ""),
                {
                    "path": str(path),
                    "content": content,
                },
            )
        except Exception as e:
            return NestResponse.make_error(params.get("_request_id", ""), f"读取技能文件失败: {e}")

    # ==================== 远程输入 ====================

    async def _handle_input(self, params: dict) -> NestResponse:
        """
        远程鼠标/键盘输入

        params:
            actions: list[dict] - 输入动作序列，每个动作包含:
                type: "mouse_move" | "mouse_click" | "mouse_scroll" |
                      "key_press" | "key_down" | "key_up" | "type_text"
                x, y: 坐标（鼠标操作）
                button: "left"|"middle"|"right"（鼠标点击，默认left）
                clicks: 点击次数（默认1）
                delta: 滚动量（正=向上，负=向下）
                key: 按键名（键盘操作，如 "enter", "a", "ctrl+a"）
                text: 要输入的文本（type_text）
            window: str - 目标窗口标题（可选，不指定则在当前焦点窗口操作）
            activate: bool - 是否激活目标窗口（默认 False）
        """
        if self.security:
            allowed, reason = self.security.check_input()
            if not allowed:
                return NestResponse.make_error(
                    params.get("_request_id", ""), f"远程输入被安全策略拒绝: {reason}"
                )

        try:
            actions = params.get("actions", [])
            window_title = params.get("window")
            activate = params.get("activate", False)

            if not actions:
                return NestResponse.make_error(
                    params.get("_request_id", ""), "未提供输入动作（actions 为空）"
                )

            # 如果指定了窗口，先查找窗口句柄
            hwnd = None
            if window_title:
                hwnd = self._find_window_by_title(window_title)
                if not hwnd:
                    return NestResponse.make_error(
                        params.get("_request_id", ""), f"未找到窗口: {window_title}"
                    )

                # 如果需要激活窗口
                if activate:
                    self._activate_window(hwnd)

            results = []
            for action in actions:
                result = self._execute_input_action(action, hwnd)
                results.append(result)

            return NestResponse.make_result(
                params.get("_request_id", ""),
                {
                    "success": True,
                    "results": results,
                    "window": window_title,
                    "activated": activate,
                },
            )

        except Exception as e:
            return NestResponse.make_error(params.get("_request_id", ""), f"远程输入失败: {e}")

    def _find_window_by_title(self, title: str) -> int | None:
        """根据标题查找窗口句柄"""
        if IS_WINDOWS:
            return self._find_window_windows(title)
        else:
            return self._find_window_linux(title)

    def _find_window_windows(self, title: str) -> int | None:
        """Windows 平台根据标题查找窗口句柄"""
        try:
            import ctypes
            from ctypes import wintypes

            # 精确匹配
            hwnd = ctypes.windll.user32.FindWindowW(None, title)
            if hwnd:
                return hwnd

            # 模糊匹配
            result = [None]

            EnumWindows = ctypes.windll.user32.EnumWindows
            GetWindowTextW = ctypes.windll.user32.GetWindowTextW
            IsWindowVisible = ctypes.windll.user32.IsWindowVisible
            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            title_lower = title.lower()

            def enum_callback(hwnd, lparam):
                if IsWindowVisible(hwnd):
                    buf = ctypes.create_unicode_buffer(256)
                    GetWindowTextW(hwnd, buf, 256)
                    if title_lower in buf.value.lower():
                        result[0] = hwnd
                        return False  # 找到即停止
                return True

            EnumWindows(WNDENUMPROC(enum_callback), 0)
            return result[0]

        except Exception as e:
            logger.error(f"查找窗口失败: {e}")
            return None

    def _find_window_linux(self, title: str) -> int | None:
        """Linux 平台根据标题查找窗口ID"""
        try:
            result = subprocess.run(
                ["xdotool", "search", "--name", title], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                # 返回第一个匹配的窗口ID
                return int(result.stdout.strip().split("\n")[0])
            return None
        except Exception as e:
            logger.error(f"Linux查找窗口失败: {e}")
            return None

    def _activate_window(self, hwnd: int) -> None:
        """激活窗口"""
        if IS_WINDOWS:
            try:
                import ctypes

                ctypes.windll.user32.SetForegroundWindow(hwnd)
                ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            except Exception as e:
                logger.warning(f"激活窗口失败: {e}")
        else:
            try:
                subprocess.run(
                    ["xdotool", "windowactivate", str(hwnd)], capture_output=True, timeout=5
                )
            except Exception as e:
                logger.warning(f"Linux激活窗口失败: {e}")

    def _execute_input_action(self, action: dict, hwnd: int | None = None) -> dict:
        """执行单个输入动作"""
        action_type = action.get("type", "")

        try:
            if action_type.startswith("mouse"):
                return self._execute_mouse_action(action, hwnd)
            elif action_type.startswith("key") or action_type == "type_text":
                return self._execute_key_action(action, hwnd)
            else:
                return {
                    "type": action_type,
                    "success": False,
                    "error": f"未知的动作类型: {action_type}",
                }
        except Exception as e:
            return {"type": action_type, "success": False, "error": str(e)}

    def _execute_mouse_action(self, action: dict, hwnd: int | None = None) -> dict:
        """执行鼠标动作"""
        action_type = action.get("type", "")
        x = action.get("x", 0)
        y = action.get("y", 0)

        if IS_WINDOWS:
            return self._execute_mouse_windows(action_type, x, y, action, hwnd)
        else:
            return self._execute_mouse_linux(action_type, x, y, action, hwnd)

    def _execute_mouse_windows(
        self, action_type: str, x: int, y: int, action: dict, hwnd: int | None = None
    ) -> dict:
        """Windows 平台鼠标操作"""
        import ctypes

        # 如果有目标窗口，将坐标转换为屏幕绝对坐标
        if hwnd:
            rect = ctypes.wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            screen_x = rect.left + x
            screen_y = rect.top + y
        else:
            screen_x = x
            screen_y = y

        MOUSEEVENTF_MOVE = 0x0001
        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004
        MOUSEEVENTF_RIGHTDOWN = 0x0008
        MOUSEEVENTF_RIGHTUP = 0x0010
        MOUSEEVENTF_MIDDLEDOWN = 0x0020
        MOUSEEVENTF_MIDDLEUP = 0x0040
        MOUSEEVENTF_WHEEL = 0x0800
        MOUSEEVENTF_ABSOLUTE = 0x8000

        # 获取屏幕分辨率以计算绝对坐标
        screen_w = ctypes.windll.user32.GetSystemMetrics(0)
        screen_h = ctypes.windll.user32.GetSystemMetrics(1)

        def to_absolute(px, py):
            """将像素坐标转换为绝对坐标（0-65535范围）"""
            return int(px * 65535 / screen_w), int(py * 65535 / screen_h)

        if action_type == "mouse_move":
            abs_x, abs_y = to_absolute(screen_x, screen_y)
            ctypes.windll.user32.mouse_event(
                MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, abs_x, abs_y, 0, 0
            )
            return {"type": action_type, "x": screen_x, "y": screen_y, "success": True}

        elif action_type == "mouse_click":
            button = action.get("button", "left")
            clicks = action.get("clicks", 1)

            abs_x, abs_y = to_absolute(screen_x, screen_y)
            ctypes.windll.user32.mouse_event(
                MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, abs_x, abs_y, 0, 0
            )

            btn_map = {
                "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
                "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
                "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
            }
            down_flag, up_flag = btn_map.get(button, (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))

            for _ in range(clicks):
                ctypes.windll.user32.mouse_event(down_flag, 0, 0, 0, 0)
                ctypes.windll.user32.mouse_event(up_flag, 0, 0, 0, 0)

            return {
                "type": action_type,
                "x": screen_x,
                "y": screen_y,
                "button": button,
                "clicks": clicks,
                "success": True,
            }

        elif action_type == "mouse_scroll":
            delta = action.get("delta", -3)  # 正=向上，负=向下；WHEEL_DELTA=120
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta * 120, 0)
            return {
                "type": action_type,
                "x": screen_x,
                "y": screen_y,
                "delta": delta,
                "success": True,
            }

        return {"type": action_type, "success": False, "error": f"未知鼠标动作: {action_type}"}

    def _execute_mouse_linux(
        self, action_type: str, x: int, y: int, action: dict, hwnd: int | None = None
    ) -> dict:
        """Linux 平台鼠标操作"""
        window_arg = ["--window", str(hwnd)] if hwnd else []

        if action_type == "mouse_move":
            subprocess.run(
                ["xdotool"] + window_arg + ["mousemove", str(x), str(y)],
                capture_output=True,
                timeout=5,
            )
            return {"type": action_type, "x": x, "y": y, "success": True}

        elif action_type == "mouse_click":
            button = action.get("button", "left")
            clicks = action.get("clicks", 1)
            btn_num = {"left": "1", "middle": "2", "right": "3"}.get(button, "1")

            # 先移动到位置
            subprocess.run(
                ["xdotool"] + window_arg + ["mousemove", str(x), str(y)],
                capture_output=True,
                timeout=5,
            )
            subprocess.run(
                ["xdotool", "click", "--repeat", str(clicks), btn_num],
                capture_output=True,
                timeout=5,
            )
            return {
                "type": action_type,
                "x": x,
                "y": y,
                "button": button,
                "clicks": clicks,
                "success": True,
            }

        elif action_type == "mouse_scroll":
            delta = action.get("delta", -3)
            # xdotool click 4=向上, 5=向下
            btn = "4" if delta > 0 else "5"
            count = abs(delta)
            subprocess.run(
                ["xdotool", "click", "--repeat", str(count), btn], capture_output=True, timeout=5
            )
            return {"type": action_type, "x": x, "y": y, "delta": delta, "success": True}

        return {"type": action_type, "success": False, "error": f"未知鼠标动作: {action_type}"}

    def _execute_key_action(self, action: dict, hwnd: int | None = None) -> dict:
        """执行键盘动作"""
        action_type = action.get("type", "")

        if IS_WINDOWS:
            return self._execute_key_windows(action_type, action, hwnd)
        else:
            return self._execute_key_linux(action_type, action, hwnd)

    def _execute_key_windows(self, action_type: str, action: dict, hwnd: int | None = None) -> dict:
        """Windows 平台键盘操作"""
        import ctypes

        if action_type == "type_text":
            text = action.get("text", "")
            # 使用 SendMessage 发送 WM_CHAR 消息
            target_hwnd = hwnd or ctypes.windll.user32.GetForegroundWindow()
            for ch in text:
                ctypes.windll.user32.SendMessageW(target_hwnd, 0x0102, ord(ch), 0)  # WM_CHAR
            return {"type": action_type, "text_length": len(text), "success": True}

        elif action_type in ("key_press", "key_down", "key_up"):
            key = action.get("key", "")
            vk_code = self._key_to_vk(key)
            if vk_code is None:
                return {
                    "type": action_type,
                    "key": key,
                    "success": False,
                    "error": f"未知的按键: {key}",
                }

            KEYEVENTF_KEYDOWN = 0x0000
            KEYEVENTF_KEYUP = 0x0002

            if action_type == "key_down" or action_type == "key_press":
                ctypes.windll.user32.keybd_event(vk_code, 0, KEYEVENTF_KEYDOWN, 0)
            if action_type == "key_up" or action_type == "key_press":
                ctypes.windll.user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)

            return {"type": action_type, "key": key, "vk_code": vk_code, "success": True}

        return {"type": action_type, "success": False, "error": f"未知键盘动作: {action_type}"}

    def _execute_key_linux(self, action_type: str, action: dict, hwnd: int | None = None) -> dict:
        """Linux 平台键盘操作"""
        window_arg = ["--window", str(hwnd)] if hwnd else []

        if action_type == "type_text":
            text = action.get("text", "")
            subprocess.run(
                ["xdotool"] + window_arg + ["type", "--delay", "0", text],
                capture_output=True,
                timeout=10,
            )
            return {"type": action_type, "text_length": len(text), "success": True}

        elif action_type in ("key_press", "key_down", "key_up"):
            key = action.get("key", "")
            xdo_key = self._key_to_xdotool(key)

            if action_type == "key_down":
                subprocess.run(
                    ["xdotool"] + window_arg + ["keydown", xdo_key], capture_output=True, timeout=5
                )
            elif action_type == "key_up":
                subprocess.run(
                    ["xdotool"] + window_arg + ["keyup", xdo_key], capture_output=True, timeout=5
                )
            else:  # key_press
                subprocess.run(
                    ["xdotool"] + window_arg + ["key", xdo_key], capture_output=True, timeout=5
                )

            return {"type": action_type, "key": key, "success": True}

        return {"type": action_type, "success": False, "error": f"未知键盘动作: {action_type}"}

    @staticmethod
    def _key_to_vk(key: str) -> int | None:
        """将按键名映射为 Windows 虚拟键码"""
        import ctypes

        # 常用虚拟键码映射
        VK_MAP = {
            "enter": 0x0D,
            "return": 0x0D,
            "tab": 0x09,
            "escape": 0x1B,
            "esc": 0x1B,
            "backspace": 0x08,
            "delete": 0x2E,
            "insert": 0x2D,
            "home": 0x24,
            "end": 0x23,
            "pageup": 0x21,
            "page_up": 0x21,
            "pagedown": 0x22,
            "page_down": 0x22,
            "up": 0x26,
            "down": 0x28,
            "left": 0x25,
            "right": 0x27,
            "space": 0x20,
            "f1": 0x70,
            "f2": 0x71,
            "f3": 0x72,
            "f4": 0x73,
            "f5": 0x74,
            "f6": 0x75,
            "f7": 0x76,
            "f8": 0x77,
            "f9": 0x78,
            "f10": 0x79,
            "f11": 0x7A,
            "f12": 0x7B,
            "shift": 0x10,
            "ctrl": 0x11,
            "alt": 0x12,
            "win": 0x5B,
            "capslock": 0x14,
            "numlock": 0x90,
            "scrolllock": 0x91,
        }

        key_lower = key.lower().strip()

        # 处理组合键（如 ctrl+a）
        if "+" in key_lower:
            # 只返回最后一个键的 vk，组合键的按下/释放由调用者处理
            parts = key_lower.split("+")
            return LocalExecutor._key_to_vk(parts[-1])

        # 单字符
        if len(key_lower) == 1:
            return ctypes.windll.user32.VkKeyScanW(ord(key_lower)) & 0xFF

        return VK_MAP.get(key_lower)

    @staticmethod
    def _key_to_xdotool(key: str) -> str:
        """将按键名映射为 xdotool 按键名"""
        key_map = {
            "enter": "Return",
            "return": "Return",
            "tab": "Tab",
            "escape": "Escape",
            "esc": "Escape",
            "backspace": "BackSpace",
            "delete": "Delete",
            "insert": "Insert",
            "home": "Home",
            "end": "End",
            "pageup": "Page_Up",
            "page_up": "Page_Up",
            "pagedown": "Page_Down",
            "page_down": "Page_Down",
            "up": "Up",
            "down": "Down",
            "left": "Left",
            "right": "Right",
            "space": "space",
            "f1": "F1",
            "f2": "F2",
            "f3": "F3",
            "f4": "F4",
            "f5": "F5",
            "f6": "F6",
            "f7": "F7",
            "f8": "F8",
            "f9": "F9",
            "f10": "F10",
            "f11": "F11",
            "f12": "F12",
            "shift": "Shift",
            "ctrl": "Ctrl",
            "alt": "Alt",
            "win": "Super",
            "capslock": "Caps_Lock",
        }

        key_lower = key.lower().strip()

        # 处理组合键（如 ctrl+a）
        if "+" in key_lower:
            parts = key_lower.split("+")
            mapped = "+".join(key_map.get(p, p) for p in parts)
            return mapped

        return key_map.get(key_lower, key_lower)

    # ==================== 交互式终端（WebUI远程终端） ====================

    # 终端会话存储：{session_id: PtyProcess}
    _terminal_sessions: dict[str, Any] = {}
    # 终端输出回调：{session_id: callable}
    _terminal_callbacks: dict[str, Any] = {}

    async def _handle_terminal_create(self, params: dict) -> NestResponse:
        """
        创建PTY终端会话

        params:
            shell: str - shell路径（默认/bin/zsh或cmd.exe）
            cols: int - 列数（默认80）
            rows: int - 行数（默认24）
            env: dict - 环境变量（可选）
            _push_callback: callable - 输出推送回调（由server.py注入）

        Returns:
            {"session_id": "uuid", "pid": int}
        """
        if IS_WINDOWS:
            return await self._handle_terminal_create_windows(params)

        import fcntl
        import pty
        import struct
        import termios

        shell = params.get("shell") or "/bin/zsh"
        cols = params.get("cols", 80)
        rows = params.get("rows", 24)
        cwd = params.get("cwd")
        push_callback = params.get("_push_callback")

        try:
            master_fd, slave_fd = pty.openpty()

            # 设置终端大小
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

            # 合并环境变量
            proc_env = os.environ.copy()
            if params.get("env"):
                proc_env.update(params["env"])
            proc_env.setdefault("TERM", "xterm-256color")
            proc_env.setdefault("LANG", "zh_CN.UTF-8")
            proc_env.setdefault("LC_ALL", "zh_CN.UTF-8")

            pid = os.fork()
            if pid == 0:
                # 子进程
                try:
                    os.close(master_fd)
                    os.setsid()
                    TIOCSCTTY = 0x540E
                    fcntl.ioctl(slave_fd, TIOCSCTTY, 0)
                    os.dup2(slave_fd, 0)
                    os.dup2(slave_fd, 1)
                    os.dup2(slave_fd, 2)
                    if slave_fd > 2:
                        os.close(slave_fd)
                    fcntl.ioctl(0, termios.TIOCSWINSZ, winsize)
                    # 🔧 Task#185：切换到指定工作目录
                    if cwd:
                        os.chdir(cwd)
                    # 🔧 修复：使用 execvpe 传递 proc_env 环境变量（execvp 不接受自定义 env）
                    os.execvpe(shell, [shell], proc_env)
                except Exception:
                    os._exit(1)
            else:
                # 父进程
                os.close(slave_fd)

                session_id = params.get("_request_id", str(uuid.uuid4()))

                # 存储session
                pty_info = {
                    "pid": pid,
                    "master_fd": master_fd,
                    "session_id": session_id,
                }
                self._terminal_sessions[session_id] = pty_info

                # 如果有推送回调，注册异步读取器
                if push_callback:
                    self._terminal_callbacks[session_id] = push_callback
                    # 在事件循环中注册读取器
                    loop = asyncio.get_event_loop()

                    def on_readable():
                        try:
                            data = os.read(master_fd, 65536)
                            if data:
                                text = data.decode("utf-8", errors="replace")
                                # 通过回调推送输出
                                asyncio.ensure_future(push_callback(session_id, text))
                            else:
                                # EOF - 进程退出
                                loop.remove_reader(master_fd)
                                self._terminal_sessions.pop(session_id, None)
                                self._terminal_callbacks.pop(session_id, None)
                                asyncio.ensure_future(
                                    push_callback(session_id, None)  # None 表示进程退出
                                )
                        except OSError:
                            loop.remove_reader(master_fd)
                            self._terminal_sessions.pop(session_id, None)
                            self._terminal_callbacks.pop(session_id, None)
                            try:
                                asyncio.ensure_future(push_callback(session_id, None))
                            except Exception:
                                pass

                    loop.add_reader(master_fd, on_readable)

                logger.info(f"终端会话已创建: session_id={session_id}, pid={pid}")

                return NestResponse.make_result(
                    params.get("_request_id", ""),
                    {
                        "session_id": session_id,
                        "pid": pid,
                    },
                )
        except Exception as e:
            logger.error(f"创建PTY失败: {e}\n{traceback.format_exc()}")
            return NestResponse.make_error(params.get("_request_id", ""), f"创建终端失败: {e}")

    async def _handle_terminal_create_windows(self, params: dict) -> NestResponse:
        """
        Windows 平台创建 PTY 终端会话（使用 pywinpty）

        params:
            shell: str - shell路径（默认 powershell.exe）
            cols: int - 列数（默认80）
            rows: int - 行数（默认24）
            env: dict - 环境变量（可选）
            _push_callback: callable - 输出推送回调（由server.py注入）

        Returns:
            {"session_id": "uuid", "pid": int}
        """
        if not _WIN_PTY_AVAILABLE:
            return NestResponse.make_error(
                params.get("_request_id", ""),
                "Windows 终端功能需要 pywinpty 库，请执行: pip install pywinpty",
            )

        shell = params.get("shell") or "powershell.exe"
        cols = params.get("cols", 80)
        rows = params.get("rows", 24)
        cwd = params.get("cwd")
        push_callback = params.get("_push_callback")

        try:
            # 合并环境变量
            proc_env = os.environ.copy()
            if params.get("env"):
                proc_env.update(params["env"])
            proc_env.setdefault("TERM", "xterm-256color")

            # 使用 pywinpty 创建终端
            spawn_kwargs = {"env": proc_env, "dimensions": (rows, cols)}
            if cwd:
                spawn_kwargs["cwd"] = cwd
            proc = _WinPtyProcess.spawn(shell, **spawn_kwargs)

            session_id = params.get("_request_id", str(uuid.uuid4()))
            pid = proc.pid

            # 存储session（Windows 使用 proc 对象替代 master_fd）
            pty_info = {
                "pid": pid,
                "proc": proc,  # pywinpty.PtyProcess 实例
                "session_id": session_id,
                "platform": "windows",
            }
            self._terminal_sessions[session_id] = pty_info

            # 如果有推送回调，启动异步读取循环
            if push_callback:
                self._terminal_callbacks[session_id] = push_callback
                import concurrent.futures

                executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

                async def _windows_read_loop():
                    """Windows PTY 异步读取循环"""
                    loop = asyncio.get_event_loop()
                    try:
                        while True:
                            # 使用线程池执行阻塞读取
                            try:
                                data = await loop.run_in_executor(executor, proc.read, 65536)
                                if data:
                                    # 通过回调推送输出
                                    await push_callback(session_id, data)
                                else:
                                    # 进程可能已退出
                                    if not proc.isalive():
                                        break
                                    await asyncio.sleep(0.02)
                            except Exception:
                                # EOFError 或其他异常表示进程退出
                                break
                    except asyncio.CancelledError:
                        pass
                    finally:
                        self._terminal_sessions.pop(session_id, None)
                        self._terminal_callbacks.pop(session_id, None)
                        executor.shutdown(wait=False)
                        try:
                            await push_callback(session_id, None)  # None 表示进程退出
                        except Exception:
                            pass

                # 启动读取任务
                read_task = asyncio.create_task(_windows_read_loop())
                pty_info["read_task"] = read_task

            logger.info(f"Windows 终端会话已创建: session_id={session_id}, pid={pid}")

            return NestResponse.make_result(
                params.get("_request_id", ""),
                {
                    "session_id": session_id,
                    "pid": pid,
                },
            )
        except Exception as e:
            logger.error(f"创建Windows PTY失败: {e}\n{traceback.format_exc()}")
            return NestResponse.make_error(params.get("_request_id", ""), f"创建终端失败: {e}")

    async def _handle_terminal_write(self, params: dict) -> NestResponse:
        """
        向PTY写入数据

        params:
            session_id: str - 会话ID
            data: str - 要写入的数据
        """
        session_id = params.get("session_id", "")
        data = params.get("data", "")

        session = self._terminal_sessions.get(session_id)
        if not session:
            return NestResponse.make_error(
                params.get("_request_id", ""), f"终端会话不存在: {session_id}"
            )

        try:
            if session.get("platform") == "windows":
                # Windows: 使用 pywinpty.PtyProcess.write
                session["proc"].write(data)
            else:
                os.write(session["master_fd"], data.encode("utf-8"))
            return NestResponse.make_result(params.get("_request_id", ""), {"written": True})
        except Exception as e:
            return NestResponse.make_error(params.get("_request_id", ""), f"写入终端失败: {e}")

    async def _handle_terminal_resize(self, params: dict) -> NestResponse:
        """
        调整PTY窗口大小

        params:
            session_id: str - 会话ID
            cols: int - 列数
            rows: int - 行数
        """
        session_id = params.get("session_id", "")
        cols = params.get("cols", 80)
        rows = params.get("rows", 24)

        session = self._terminal_sessions.get(session_id)
        if not session:
            return NestResponse.make_error(
                params.get("_request_id", ""), f"终端会话不存在: {session_id}"
            )

        try:
            if session.get("platform") == "windows":
                # Windows: 使用 pywinpty.PtyProcess.setwinsize
                session["proc"].setwinsize(rows, cols)
            else:
                import fcntl
                import struct
                import termios

                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(session["master_fd"], termios.TIOCSWINSZ, winsize)
            return NestResponse.make_result(params.get("_request_id", ""), {"resized": True})
        except Exception as e:
            return NestResponse.make_error(params.get("_request_id", ""), f"调整终端大小失败: {e}")

    async def _handle_terminal_close(self, params: dict) -> NestResponse:
        """
        关闭PTY终端会话

        params:
            session_id: str - 会话ID
        """
        session_id = params.get("session_id", "")

        session = self._terminal_sessions.pop(session_id, None)
        self._terminal_callbacks.pop(session_id, None)

        if not session:
            return NestResponse.make_result(
                params.get("_request_id", ""), {"closed": True, "note": "会话已不存在"}
            )

        if session.get("platform") == "windows":
            # Windows: 取消读取任务，终止 pywinpty 进程
            read_task = session.get("read_task")
            if read_task and not read_task.done():
                read_task.cancel()
            try:
                session["proc"].terminate(force=True)
            except Exception:
                pass
            try:
                session["proc"].close(force=True)
            except Exception:
                pass
        else:
            # POSIX: 移除 reader，关闭 master_fd
            try:
                loop = asyncio.get_event_loop()
                loop.remove_reader(session["master_fd"])
            except (OSError, ValueError):
                pass

            try:
                # 发送Ctrl+C然后强制终止
                os.write(session["master_fd"], b"\x03")
                os.kill(session["pid"], signal.SIGTERM)
            except Exception:
                pass

            try:
                os.close(session["master_fd"])
            except OSError:
                pass

        logger.info(f"终端会话已关闭: session_id={session_id}")

        return NestResponse.make_result(params.get("_request_id", ""), {"closed": True})

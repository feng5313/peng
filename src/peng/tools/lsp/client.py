"""
LSP客户端核心模块

提供与语言服务器通信的基础功能
"""

import asyncio
import json
import logging
import sys
import uuid
from typing import Any, Optional
from urllib.parse import quote as url_quote

logger = logging.getLogger(__name__)


def _path_to_uri(path: str) -> str:
    """
    将文件路径转换为正确的file:// URI。
    Windows: file:///D:/work/tetris/src/main.cpp
    Linux:   file:///home/user/project/src/main.cpp
    """
    # 标准化路径分隔符为 /
    normalized = path.replace("\\", "/")
    # 确保以 / 开头（Windows路径如 D:/... 需要加前导/）
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    # URL编码特殊字符（保留:/和字母数字）
    encoded = url_quote(normalized, safe="/:@!$&'()*+,;=-._~")
    return f"file://{encoded}"


class JSONRPCError(Exception):
    """JSON-RPC错误"""
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"JSON-RPC Error {code}: {message}")


class LSPClient:
    """
    LSP客户端基类

    管理与语言服务器的进程通信
    """

    def __init__(self, server_command: list[str], root_path: str, init_options: dict = None):
        """
        初始化LSP客户端

        Args:
            server_command: 启动服务器的命令列表
            root_path: 项目根目录
            init_options: 服务器特定的初始化选项（如clangd的usePlaceholders等）
        """
        self.server_command = server_command
        self.root_path = root_path
        self.init_options = init_options or {}
        self.process: Optional[asyncio.subprocess.Process] = None
        self.request_id = 0
        self.pending_requests: dict[int, asyncio.Future] = {}
        self._initialized = False
        self._reader_task: Optional[asyncio.Task] = None
        self._buffer = ""
        # 存储诊断信息: {uri: [diagnostic]}
        self._diagnostics: dict[str, list[dict]] = {}

    async def start(self) -> None:
        """启动语言服务器进程"""
        if self.process is not None:
            logger.warning("LSP服务器进程已经启动")
            return

        logger.info(f"启动LSP服务器: {' '.join(self.server_command)}")
        
        # Windows上需要 CREATE_NO_WINDOW 避免弹出控制台窗口
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

        self.process = await asyncio.create_subprocess_exec(
            *self.server_command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )

        # 异步读取stderr，防止缓冲区满导致clangd阻塞
        asyncio.create_task(self._drain_stderr())

        # 启动读取任务
        self._reader_task = asyncio.create_task(self._read_loop())

        # 异步消费stderr，防止缓冲区满导致LSP服务器进程阻塞挂死
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        # 初始化服务器
        await self._initialize()

    async def _drain_stderr(self) -> None:
        """持续读取并丢弃stderr，防止缓冲区满导致子进程阻塞"""
        if self.process is None or self.process.stderr is None:
            return
        try:
            while True:
                line = await self.process.stderr.readline()
                if not line:
                    break
                # 可选：记录到debug日志
                # logger.debug(f"[LSP stderr] {line.decode('utf-8', errors='replace').strip()}")
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _initialize(self) -> None:
        """初始化LSP服务器"""
        init_params: dict[str, Any] = {
            "processId": None,
            "rootUri": _path_to_uri(self.root_path),
            "rootPath": self.root_path,
            "capabilities": {
                "textDocument": {
                    "definition": {"linkSupport": True},
                    "references": {},
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "documentSymbol": {},
                    "implementation": {},
                    "callHierarchy": {},
                    "completion": {
                        "completionItem": {"snippetSupport": False},
                        "completionItemKind": {},
                        "contextSupport": True,
                    },
                },
                "workspace": {
                    "symbol": {},
                    "callHierarchy": {},
                },
            },
        }
        
        # 添加服务器特定的init_options（如clangd的usePlaceholders等）
        if self.init_options:
            init_params["initializationOptions"] = self.init_options

        try:
            # initialize需要更长超时（--background-index下clangd构建索引需要时间）
            result = await self.send_request("initialize", init_params, timeout=120.0)
            logger.info(f"LSP服务器初始化成功: {result.get('capabilities', {})}")
            
            # 发送initialized通知
            await self.send_notification("initialized", {})
            self._initialized = True
        except Exception as e:
            logger.error(f"LSP服务器初始化失败: {e}")
            raise

    async def send_request(self, method: str, params: dict, timeout: float = 60.0) -> Any:
        """
        发送请求并等待响应

        Args:
            method: 方法名
            params: 参数
            timeout: 超时时间（秒），默认60秒

        Returns:
            响应结果
        """
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("LSP服务器未启动")

        self.request_id += 1
        request_id = self.request_id

        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }

        content = json.dumps(request)
        message = f"Content-Length: {len(content)}\r\n\r\n{content}"

        # 创建Future等待响应
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self.pending_requests[request_id] = future

        # 发送请求
        self.process.stdin.write(message.encode("utf-8"))
        await self.process.stdin.drain()

        # 等待响应
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self.pending_requests.pop(request_id, None)
            raise RuntimeError(f"LSP请求超时({timeout}s): {method}")

    async def send_notification(self, method: str, params: dict) -> None:
        """
        发送通知（不等待响应）

        Args:
            method: 方法名
            params: 参数
        """
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("LSP服务器未启动")

        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }

        content = json.dumps(notification)
        message = f"Content-Length: {len(content)}\r\n\r\n{content}"

        self.process.stdin.write(message.encode("utf-8"))
        await self.process.stdin.drain()

    async def _read_loop(self) -> None:
        """读取服务器响应的循环"""
        if self.process is None or self.process.stdout is None:
            return

        while True:
            try:
                # 读取头部
                headers = {}
                while True:
                    try:
                        line = await self.process.stdout.readline()
                    except Exception:
                        return  # 进程已结束
                    if not line:
                        return
                    line = line.decode("utf-8").strip()
                    if not line:
                        break
                    if ":" in line:
                        key, value = line.split(":", 1)
                        headers[key.strip().lower()] = value.strip()

                # 读取内容（必须精确读取content_length字节，readexactly保证完整）
                content_length = int(headers.get("content-length", 0))
                if content_length > 0:
                    content_bytes = await self.process.stdout.readexactly(content_length)
                    content = content_bytes.decode("utf-8", errors="replace")
                    await self._handle_message(content)
                else:
                    # 没有Content-Length，跳过
                    pass
            except asyncio.CancelledError:
                return
            except Exception as e:
                # 单个消息解析失败不应终止整个读取循环！
                # clangd可能发送格式异常的超大diagnostics消息
                logger.warning(f"读取LSP消息异常（继续读取）: {e}")
                continue

    async def _handle_message(self, content: str) -> None:
        """处理服务器消息"""
        try:
            message = json.loads(content)
            
            # 响应
            if "id" in message:
                request_id = message["id"]
                if request_id in self.pending_requests:
                    future = self.pending_requests.pop(request_id)
                    if "error" in message:
                        future.set_exception(JSONRPCError(
                            message["error"].get("code", -1),
                            message["error"].get("message", "Unknown error"),
                            message["error"].get("data")
                        ))
                    else:
                        future.set_result(message.get("result"))
            # 通知
            elif "method" in message:
                method = message["method"]
                params = message.get("params", {})
                await self._handle_notification(method, params)
        except json.JSONDecodeError as e:
            # 通知消息解析失败不影响功能，响应消息需要检查是否有pending future
            logger.warning(f"JSON解析错误(len={len(content)}): {e}")
            # 尝试从残缺JSON中提取id，如果有pending future则设异常
            import re
            id_match = re.search(r'"id"\s*:\s*(\d+)', content[:200])
            if id_match:
                request_id = int(id_match.group(1))
                future = self.pending_requests.pop(request_id, None)
                if future and not future.done():
                    future.set_exception(RuntimeError(f"响应JSON解析失败: {e}"))

    async def _handle_notification(self, method: str, params: dict) -> None:
        """处理服务器通知"""
        logger.debug(f"收到通知: {method}")

        # 处理诊断信息通知
        if method == "textDocument/publishDiagnostics":
            self._handle_diagnostics(params)
        # 可以在这里处理其他通知类型

    async def text_document_did_open(self, file_path: str, language_id: str, content: str) -> None:
        """通知服务器打开文档"""
        if not self._initialized:
            return

        await self.send_notification("textDocument/didOpen", {
            "textDocument": {
                "uri": _path_to_uri(file_path),
                "languageId": language_id,
                "version": 1,
                "text": content,
            }
        })

    async def goto_definition(self, file_path: str, line: int, column: int) -> Any:
        """跳转到定义"""
        if not self._initialized:
            raise RuntimeError("LSP服务器未初始化")

        return await self.send_request("textDocument/definition", {
            "textDocument": {"uri": _path_to_uri(file_path)},
            "position": {"line": line - 1, "character": column - 1},
        })

    async def find_references(self, file_path: str, line: int, column: int) -> Any:
        """查找引用"""
        if not self._initialized:
            raise RuntimeError("LSP服务器未初始化")

        return await self.send_request("textDocument/references", {
            "textDocument": {"uri": _path_to_uri(file_path)},
            "position": {"line": line - 1, "character": column - 1},
            "context": {"includeDeclaration": True},
        })

    async def hover(self, file_path: str, line: int, column: int) -> Any:
        """获取悬停信息"""
        if not self._initialized:
            raise RuntimeError("LSP服务器未初始化")

        return await self.send_request("textDocument/hover", {
            "textDocument": {"uri": _path_to_uri(file_path)},
            "position": {"line": line - 1, "character": column - 1},
        })

    async def document_symbols(self, file_path: str) -> Any:
        """获取文档符号"""
        if not self._initialized:
            raise RuntimeError("LSP服务器未初始化")

        return await self.send_request("textDocument/documentSymbol", {
            "textDocument": {"uri": _path_to_uri(file_path)},
        })

    async def workspace_symbol(self, query: str) -> Any:
        """工作区符号搜索"""
        if not self._initialized:
            raise RuntimeError("LSP服务器未初始化")

        return await self.send_request("workspace/symbol", {
            "query": query,
        })

    async def completion(self, file_path: str, line: int, column: int) -> Any:
        """代码补全"""
        if not self._initialized:
            raise RuntimeError("LSP服务器未初始化")

        return await self.send_request("textDocument/completion", {
            "textDocument": {"uri": _path_to_uri(file_path)},
            "position": {"line": line - 1, "character": column - 1},
        })

    async def implementation(self, file_path: str, line: int, column: int) -> Any:
        """查找实现"""
        if not self._initialized:
            raise RuntimeError("LSP服务器未初始化")

        return await self.send_request("textDocument/implementation", {
            "textDocument": {"uri": _path_to_uri(file_path)},
            "position": {"line": line - 1, "character": column - 1},
        })

    async def prepare_call_hierarchy(self, file_path: str, line: int, column: int) -> Any:
        """准备调用层次"""
        if not self._initialized:
            raise RuntimeError("LSP服务器未初始化")

        return await self.send_request("textDocument/prepareCallHierarchy", {
            "textDocument": {"uri": _path_to_uri(file_path)},
            "position": {"line": line - 1, "character": column - 1},
        })

    async def incoming_calls(self, file_path: str, line: int, column: int) -> Any:
        """查找调用者"""
        if not self._initialized:
            raise RuntimeError("LSP服务器未初始化")

        # 先准备调用层次
        items = await self.prepare_call_hierarchy(file_path, line, column)
        if not items:
            return []

        # 获取第一个项目的调用者
        item = items[0] if isinstance(items, list) else items
        return await self.send_request("callHierarchy/incomingCalls", {
            "item": item,
        })

    async def outgoing_calls(self, file_path: str, line: int, column: int) -> Any:
        """查找被调用者"""
        if not self._initialized:
            raise RuntimeError("LSP服务器未初始化")

        # 先准备调用层次
        items = await self.prepare_call_hierarchy(file_path, line, column)
        if not items:
            return []

        # 获取第一个项目的被调用者
        item = items[0] if isinstance(items, list) else items
        return await self.send_request("callHierarchy/outgoingCalls", {
            "item": item,
        })

    async def close(self) -> None:
        """关闭客户端"""
        for task in (self._reader_task, getattr(self, '_stderr_task', None)):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self.process:
            try:
                await self.send_notification("shutdown", {})
                await self.send_notification("exit", {})
            except Exception:
                pass

            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.process.kill()
            except Exception:
                pass

            self.process = None
            self._initialized = False

    def _handle_diagnostics(self, params: dict) -> None:
        """处理诊断信息通知"""
        uri = params.get("uri", "")
        diagnostics = params.get("diagnostics", [])
        self._diagnostics[uri] = diagnostics
        logger.debug(f"收到诊断信息: {uri} - {len(diagnostics)} 条")

    def get_diagnostics(self, uri: str) -> list[dict]:
        """
        获取指定URI的诊断信息

        Args:
            uri: 文件URI

        Returns:
            诊断信息列表
        """
        return self._diagnostics.get(uri, [])

    def get_all_diagnostics(self) -> dict[str, list[dict]]:
        """
        获取所有诊断信息

        Returns:
            {uri: [diagnostic]}
        """
        return self._diagnostics.copy()

    def clear_diagnostics(self, uri: str) -> None:
        """
        清除指定URI的诊断信息

        Args:
            uri: 文件URI
        """
        if uri in self._diagnostics:
            del self._diagnostics[uri]
            logger.debug(f"清除诊断信息: {uri}")
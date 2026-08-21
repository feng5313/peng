"""
LSP适配器基类

定义所有语言适配器的通用接口
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

from ..client import LSPClient
from ..path_resolver import get_global_resolver, ServerType

logger = logging.getLogger(__name__)


class BaseAdapter(ABC):
    """
    LSP适配器基类

    所有语言适配器都应继承此类并实现抽象方法
    """

    def __init__(self, root_path: str):
        """
        初始化适配器

        Args:
            root_path: 项目根目录
        """
        self.root_path = root_path
        self._client: Optional[LSPClient] = None
        self._lock = asyncio.Lock()

    @property
    @abstractmethod
    def language(self) -> str:
        """
        语言名称

        Returns:
            语言标识符
        """
        pass

    @property
    @abstractmethod
    def file_extensions(self) -> list[str]:
        """
        支持的文件扩展名

        Returns:
            扩展名列表（如[".py", ".pyx"]）
        """
        pass

    @abstractmethod
    def get_server_command(self) -> list[str]:
        """
        获取启动语言服务器的命令

        Returns:
            命令列表

        Raises:
            RuntimeError: 如果服务器不可用
        """
        pass

    @property
    @abstractmethod
    def server_name(self) -> str:
        """
        LSP服务器名称（用于依赖项管理）

        Returns:
            服务器名称（如"python-lsp-server"）
        """
        pass

    @property
    @abstractmethod
    def server_type(self) -> ServerType:
        """
        LSP服务器类型（用于路径解析）

        Returns:
            服务器类型（如ServerType.PYLSP）
        """
        pass

    def _get_server_command_from_resolver(self) -> Optional[list[str]]:
        """
        使用LspPathResolver获取服务器命令

        Returns:
            命令列表，如果未找到则返回None
        """
        resolver = get_global_resolver(Path(self.root_path))
        command = resolver.get_server_command(self.server_type)
        return command

    async def _ensure_server_installed(self) -> None:
        """
        确保LSP服务器已安装，如果未安装则触发后台安装

        注意：此方法不阻塞，安装在后台进行
        """
        try:
            from ..dependencies import LspDependencyManager

            manager = LspDependencyManager()
            dependency = manager.get_dependency(self.server_name)

            if dependency and not manager.is_installed(dependency):
                logger.info(f"{self.server_name} 未安装，触发后台自动安装")
                # 后台安装，不等待
                asyncio.create_task(
                    manager.install(
                        dependency,
                        progress_callback=self._on_install_progress
                    )
                )
        except ImportError:
            logger.warning("LspDependencyManager不可用，无法自动安装")
        except Exception as e:
            logger.error(f"触发自动安装失败: {e}")

    async def _on_install_progress(
        self,
        name: str,
        status: Any,
        error_message: Optional[str] = None
    ) -> None:
        """
        安装进度回调

        Args:
            name: 依赖项名称
            status: 安装状态
            error_message: 错误信息
        """
        try:
            from peng.webui.websocket import broadcast_toast
            from ..dependencies import InstallStatus

            if status == InstallStatus.INSTALLING:
                await broadcast_toast(
                    "info",
                    "正在安装",
                    f"{name} 正在后台安装中..."
                )
            elif status == InstallStatus.INSTALLED:
                await broadcast_toast(
                    "success",
                    "安装成功",
                    f"{name} 已安装完成"
                )
                logger.info(f"{name} 安装完成")
            elif status == InstallStatus.INSTALL_FAILED:
                await broadcast_toast(
                    "error",
                    "安装失败",
                    f"{name} 安装失败: {error_message}"
                )
                logger.error(f"{name} 安装失败: {error_message}")
        except ImportError:
            logger.debug("WebSocket不可用，跳过toast通知")
        except Exception as e:
            logger.error(f"发送安装进度通知失败: {e}")


    def has_server(self) -> bool:
        """
        检查语言服务器是否可用

        Returns:
            服务器是否可用
        """
        try:
            self.get_server_command()
            return True
        except Exception:
            return False

    async def get_client(self) -> LSPClient:
        """
        获取LSP客户端实例

        如果客户端未启动，则启动它

        Returns:
            LSP客户端实例
        """
        async with self._lock:
            if self._client is None:
                command = self.get_server_command()
                import sys as _sys
                print(f"[LSP] 启动命令: {' '.join(str(c) for c in command)}", file=_sys.stderr, flush=True)
                # 支持adapter特定的init_options（如clangd的usePlaceholders等）
                init_options = getattr(self, '_init_options', None) or {}
                self._client = LSPClient(command, self.root_path, init_options)
                await self._client.start()
                logger.info(f"{self.language} LSP客户端已启动")
            return self._client

    async def close(self) -> None:
        """关闭LSP客户端"""
        async with self._lock:
            if self._client is not None:
                await self._client.close()
                self._client = None
                logger.info(f"{self.language} LSP客户端已关闭")

    async def definition(
        self,
        file_path: str,
        line: int,
        column: int,
    ) -> str:
        """
        查找定义

        Args:
            file_path: 文件路径
            line: 行号（从1开始）
            column: 列号（从1开始）

        Returns:
            格式化的结果字符串
        """
        try:
            client = await self.get_client()
            uri = self._path_to_uri(file_path)

            # 通知文件已打开
            text = Path(file_path).read_text(encoding="utf-8")
            await client.did_open(uri, self.language, text)

            # 查找定义（LSP行号和列号从0开始）
            result = await client.definition(uri, line - 1, column - 1)

            return self._format_definition_result(result)

        except Exception as e:
            logger.error(f"{self.language} 查找定义失败: {e}")
            import traceback
            return f"错误: {e}\n{traceback.format_exc()}"

    async def references(
        self,
        file_path: str,
        line: int,
        column: int,
    ) -> str:
        """
        查找引用

        Args:
            file_path: 文件路径
            line: 行号（从1开始）
            column: 列号（从1开始）

        Returns:
            格式化的结果字符串
        """
        try:
            client = await self.get_client()
            uri = self._path_to_uri(file_path)

            # 通知文件已打开
            text = Path(file_path).read_text(encoding="utf-8")
            await client.did_open(uri, self.language, text)

            # 查找引用
            result = await client.references(uri, line - 1, column - 1)

            return self._format_references_result(result)

        except Exception as e:
            logger.error(f"{self.language} 查找引用失败: {e}")
            import traceback
            return f"错误: {e}\n{traceback.format_exc()}"

    async def hover(
        self,
        file_path: str,
        line: int,
        column: int,
    ) -> str:
        """
        获取悬停信息

        Args:
            file_path: 文件路径
            line: 行号（从1开始）
            column: 列号（从1开始）

        Returns:
            格式化的结果字符串
        """
        try:
            client = await self.get_client()
            uri = self._path_to_uri(file_path)

            # 通知文件已打开
            text = Path(file_path).read_text(encoding="utf-8")
            await client.did_open(uri, self.language, text)

            # 获取悬停信息
            result = await client.hover(uri, line - 1, column - 1)

            return self._format_hover_result(result)

        except Exception as e:
            logger.error(f"{self.language} 获取悬停信息失败: {e}")
            import traceback
            return f"错误: {e}\n{traceback.format_exc()}"

    async def symbols(self, file_path: str) -> str:
        """
        获取文档符号

        Args:
            file_path: 文件路径

        Returns:
            格式化的结果字符串
        """
        try:
            client = await self.get_client()
            uri = self._path_to_uri(file_path)

            # 通知文件已打开
            text = Path(file_path).read_text(encoding="utf-8")
            await client.did_open(uri, self.language, text)

            # 获取符号
            result = await client.document_symbol(uri)

            return self._format_symbols_result(result)

        except Exception as e:
            logger.error(f"{self.language} 获取符号失败: {e}")
            import traceback
            return f"错误: {e}\n{traceback.format_exc()}"

    async def workspace_symbol(self, query: str) -> str:
        """工作区符号搜索"""
        try:
            client = await self.get_client()
            result = await client.workspace_symbol(query)
            return self._format_workspace_symbol_result(result)
        except Exception as e:
            return "错误: {}".format(e)

    async def implementation(self, file_path: str, line: int, column: int) -> str:
        """查找实现"""
        try:
            client = await self.get_client()
            uri = self._path_to_uri(file_path)
            text = Path(file_path).read_text(encoding="utf-8")
            await client.did_open(uri, self.language, text)
            result = await client.implementation(uri, line - 1, column - 1)
            return self._format_definition_result(result)
        except Exception as e:
            return "错误: {}".format(e)

    async def incoming_calls(self, file_path: str, line: int, column: int) -> str:
        """查找调用者"""
        try:
            client = await self.get_client()
            uri = self._path_to_uri(file_path)
            text = Path(file_path).read_text(encoding="utf-8")
            await client.did_open(uri, self.language, text)
            result = await client.incoming_calls(uri, line - 1, column - 1)
            return self._format_call_hierarchy_result(result, "调用者")
        except Exception as e:
            return "错误: {}".format(e)

    async def outgoing_calls(self, file_path: str, line: int, column: int) -> str:
        """查找被调用者"""
        try:
            client = await self.get_client()
            uri = self._path_to_uri(file_path)
            text = Path(file_path).read_text(encoding="utf-8")
            await client.did_open(uri, self.language, text)
            result = await client.outgoing_calls(uri, line - 1, column - 1)
            return self._format_call_hierarchy_result(result, "被调用者")
        except Exception as e:
            return "错误: {}".format(e)

    async def diagnostics(self, file_path: str) -> str:
        """获取诊断信息"""
        try:
            client = await self.get_client()
            uri = self._path_to_uri(file_path)
            text = Path(file_path).read_text(encoding="utf-8")
            await client.did_open(uri, self.language, text)
            # 触发诊断（大多数LSP服务器在didOpen后自动发送诊断）
            diagnostics = client.get_diagnostics(uri)
            return self._format_diagnostics_result(diagnostics, file_path)
        except Exception as e:
            return "错误: {}".format(e)

    def _format_workspace_symbol_result(self, result: Any) -> str:
        if not result:
            return "未找到符号"
        symbols = result if isinstance(result, list) else [result]
        lines = ["找到 {} 个符号:".format(len(symbols))]
        for sym in symbols[:20]:
            name = sym.get("name", "")
            kind = sym.get("kind", "")
            kind_name = self._symbol_kind_to_string(kind)
            location = sym.get("location", {})
            if location:
                uri = location.get("uri", "")
                if uri:
                    path = self._uri_to_path(uri)
                    range_info = location.get("range", {})
                    start = range_info.get("start", {})
                    line_num = start.get("line", 0) + 1
                    lines.append("  {}: {} @ {}:{}".format(kind_name, name, path, line_num))
                else:
                    lines.append("  {}: {}".format(kind_name, name))
            else:
                lines.append("  {}: {}".format(kind_name, name))
        return "\n".join(lines)

    def _format_call_hierarchy_result(self, result: Any, label: str) -> str:
        if not result:
            return "未找到{}".format(label)
        calls = result if isinstance(result, list) else [result]
        lines = ["找到 {} 个{}:".format(len(calls), label)]
        for call in calls[:20]:
            item = call.get("from" if label == "调用者" else "to", {})
            if not item:
                continue
            name = item.get("name", "")
            kind = item.get("kind", "")
            kind_name = self._symbol_kind_to_string(kind)
            from_ranges = call.get("fromRanges", [])
            if from_ranges:
                range_info = from_ranges[0]
                start = range_info.get("start", {})
                line_num = start.get("line", 0) + 1
                lines.append("  {}: {} @ 行 {}".format(kind_name, name, line_num))
            else:
                lines.append("  {}: {}".format(kind_name, name))
        return "\n".join(lines)

    def _path_to_uri(self, file_path: str) -> str:
        """
        将文件路径转换为URI

        Args:
            file_path: 文件路径

        Returns:
            文件URI
        """
        path = Path(file_path).resolve()
        return f"file://{path}"

    def _uri_to_path(self, uri: str) -> str:
        """
        将URI转换为文件路径

        Args:
            uri: 文件URI

        Returns:
            文件路径
        """
        if uri.startswith("file://"):
            return uri[7:]
        return uri

    def _format_definition_result(self, result: Any) -> str:
        """格式化定义结果"""
        if not result:
            return "未找到定义"

        if isinstance(result, list):
            if not result:
                return "未找到定义"
            locations = result
        else:
            locations = [result]

        lines = [f"找到 {len(locations)} 个定义:"]
        for loc in locations[:10]:  # 最多显示10个
            uri = loc.get("uri", "")
            if uri:
                path = self._uri_to_path(uri)
                range_info = loc.get("range", {})
                start = range_info.get("start", {})
                line_num = start.get("line", 0) + 1
                lines.append(f"  {path}:{line_num}")
            else:
                lines.append(f"  {loc}")

        return "\n".join(lines)

    def _format_references_result(self, result: Any) -> str:
        """格式化引用结果"""
        if not result:
            return "未找到引用"

        locations = result if isinstance(result, list) else [result]

        lines = [f"找到 {len(locations)} 处引用:"]
        for loc in locations[:20]:  # 最多显示20个
            uri = loc.get("uri", "")
            if uri:
                path = self._uri_to_path(uri)
                range_info = loc.get("range", {})
                start = range_info.get("start", {})
                line_num = start.get("line", 0) + 1
                lines.append(f"  {path}:{line_num}")
            else:
                lines.append(f"  {loc}")

        return "\n".join(lines)

    def _format_hover_result(self, result: Any) -> str:
        """格式化悬停信息结果"""
        if not result:
            return "无悬停信息"

        contents = result.get("contents", "")
        if isinstance(contents, dict):
            # 处理MarkupContent
            value = contents.get("value", "")
            return f"悬停信息:\n{value}"
        elif isinstance(contents, list):
            # 处理多个内容
            return "\n".join(str(c) for c in contents)
        else:
            return f"悬停信息:\n{contents}"

    def _format_symbols_result(self, result: Any) -> str:
        """格式化符号结果"""
        if not result:
            return "未找到符号"

        def extract_symbols(symbols, depth=0):
            """递归提取符号"""
            lines = []
            indent = "  " * depth

            for sym in symbols:
                name = sym.get("name", "")
                kind = sym.get("kind", "")
                kind_name = self._symbol_kind_to_string(kind)

                range_info = sym.get("range", {})
                start = range_info.get("start", {})
                line_num = start.get("line", 0) + 1

                lines.append(f"{indent}{kind_name}: {name} (行 {line_num})")

                # 递归处理子符号
                children = sym.get("children", [])
                if children:
                    lines.extend(extract_symbols(children, depth + 1))

            return lines

        symbols = result if isinstance(result, list) else [result]
        lines = ["文件符号:"]
        lines.extend(extract_symbols(symbols))

        return "\n".join(lines)

    def _symbol_kind_to_string(self, kind: int) -> str:
        """
        将符号种类转换为字符串

        Args:
            kind: LSP符号种类常量

        Returns:
            符号类型字符串
        """
        kind_map = {
            1: "File",
            2: "Module",
            3: "Namespace",
            4: "Package",
            5: "Class",
            6: "Method",
            7: "Property",
            8: "Field",
            9: "Constructor",
            10: "Enum",
            11: "Interface",
            12: "Function",
            13: "Variable",
            14: "Constant",
            15: "String",
            16: "Number",
            17: "Boolean",
            18: "Array",
            19: "Object",
            20: "Key",
            21: "Null",
            22: "EnumMember",
            23: "Struct",
            24: "Event",
            25: "Operator",
            26: "TypeParameter",
        }
        return kind_map.get(kind, f"Unknown({kind})")

    def _format_diagnostics_result(self, diagnostics: list[dict], file_path: str) -> str:
        """
        格式化诊断信息结果

        Args:
            diagnostics: 诊断信息列表
            file_path: 文件路径

        Returns:
            格式化的诊断信息字符串
        """
        if not diagnostics:
            return "未发现诊断信息（代码无错误或警告）"

        lines = [f"诊断信息: {file_path}"]
        lines.append(f"共 {len(diagnostics)} 条")

        for diag in diagnostics:
            # 获取诊断级别
            severity = diag.get("severity", 1)  # 默认为Error
            severity_map = {
                1: "错误",
                2: "警告",
                3: "信息",
                4: "提示"
            }
            severity_str = severity_map.get(severity, "未知")

            # 获取位置
            range_info = diag.get("range", {})
            start = range_info.get("start", {})
            line = start.get("line", 0) + 1
            column = start.get("character", 0) + 1

            # 获取消息
            message = diag.get("message", "")
            source = diag.get("source", "LSP")

            # 获取相关代码（如果有）
            code = diag.get("code", "")
            code_str = f" [{code}]" if code else ""

            lines.append(f"  [{severity_str}] 行 {line}, 列 {column}: {message}{code_str} ({source})")


        return "\n".join(lines)

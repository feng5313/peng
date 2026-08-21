"""
LSP工具模块

提供代码智能功能，支持多语言LSP服务器
"""

import logging
from pathlib import Path
from typing import Any, Optional

from peng.common.paths import get_working_directory
from peng.tools.base import Tool, ToolError

logger = logging.getLogger(__name__)

# 尝试导入新的LSP适配器
try:
    from .manager import AdapterManager
    NEW_LSP_AVAILABLE = True
except ImportError:
    logger.warning("新的LSP适配器模块不可用，将使用回退实现")
    NEW_LSP_AVAILABLE = False

# 导入回退实现
from .fallback import (
    _is_extension_supported,
    extract_symbol_from_line,
    get_hover_info,
    get_symbols,
    search_definition,
    search_references,
)


class LSPTool(Tool):
    """
    LSP工具 - 混合实现

    优先使用专业的语言服务器，不可用时回退到纯Python实现
    """

    def __init__(self):
        super().__init__()
        self._manager: Optional[AdapterManager] = None
        self._use_new_lsp = NEW_LSP_AVAILABLE

    @property
    def name(self) -> str:
        return "lsp"

    @property
    def description(self) -> str:
        desc = """LSP工具，提供代码智能功能。
支持的操作：
- definition: 跳转到定义
- references: 查找引用
- symbols: 查找符号
- hover: 获取文档
- workspaceSymbol: 工作区符号搜索
- implementation: 查找实现
- incomingCalls: 查找调用者
- outgoingCalls: 查找被调用者

支持的语言（需要安装对应语言服务器）：
"""
        if NEW_LSP_AVAILABLE:
            desc += """- Python3 (python-lsp-server)
- C/C++ (clangd)
- Rust (rust-analyzer)
- GoLang (gopls)
- TypeScript/JavaScript (typescript-language-server)
- Zig (zls)
- Lua (lua-language-server)

回退模式：纯Python实现（当语言服务器不可用时）
"""
        else:
            desc += """- Python, JavaScript, TypeScript, Go, Rust, Java, Kotlin, Ruby, PHP
- C/C++, Lua, Zig（基础支持）

当前模式：纯Python实现
"""
        return desc

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["definition", "references", "symbols", "hover", "workspaceSymbol", "implementation", "incomingCalls", "outgoingCalls", "diagnostics"],
                    "description": "要执行的LSP操作",
                },
                "file_path": {
                    "type": "string",
                    "description": "文件路径",
                },
                "line": {
                    "type": "integer",
                    "description": "行号（从1开始）",
                },
                "column": {
                    "type": "integer",
                    "description": "列号（从1开始）",
                },
                "symbol": {
                    "type": "string",
                    "description": "符号名称（用于symbols操作）",
                },
                "root_path": {
                    "type": "string",
                    "description": "项目根目录（可选）",
                },
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> str:
        """执行LSP操作"""
        action: str = kwargs.get("action", "")
        file_path: str = kwargs.get("file_path", "")
        line: int = kwargs.get("line", 1)
        column: int = kwargs.get("column", 1)
        symbol: str = kwargs.get("symbol", "")
        root_path: str = kwargs.get("root_path", "")

        # 检查文件扩展名是否支持（对于某些操作可以不提供file_path）
        if file_path and not _is_extension_supported(file_path):
            raise ToolError(f"不支持的文件类型: {file_path}")

        # 优先使用新的LSP适配器
        if self._use_new_lsp:
            try:
                return await self._execute_with_new_lsp(action, file_path, line, column, symbol, root_path)
            except Exception as e:
                logger.warning(f"新LSP适配器失败: {e}，回退到纯Python实现")
                # 回退到旧实现
                pass

        # 使用旧的纯Python实现
        return await self._execute_with_fallback(action, file_path, line, column, symbol, root_path)

    async def _execute_with_new_lsp(
        self,
        action: str,
        file_path: str,
        line: int,
        column: int,
        symbol: str,
        root_path: str,
    ) -> str:
        """使用新的LSP适配器执行"""
        from .language import is_supported

        if not is_supported(Path(file_path).suffix):
            raise ToolError(f"新LSP适配器不支持的文件类型: {file_path}")

        # 确定项目根目录
        workdir = self.get_context("workdir")
        workdir = Path(workdir) if workdir else get_working_directory()
        root = Path(root_path) if root_path else workdir

        # 初始化管理器
        if self._manager is None:
            self._manager = AdapterManager(str(root))

        # 获取适配器
        adapter = self._manager.get_adapter(file_path)
        if adapter is None:
            raise ToolError(f"无法创建 {file_path} 的LSP适配器")

        # 执行操作
        if action == "definition":
            result = await adapter.definition(file_path, line, column)
        elif action == "references":
            result = await adapter.references(file_path, line, column)
        elif action == "symbols":
            result = await adapter.symbols(file_path)
        elif action == "hover":
            result = await adapter.hover(file_path, line, column)
        elif action == "workspaceSymbol":
            # workspaceSymbol使用symbol参数作为查询字符串
            query = symbol if symbol else ""
            result = await adapter.workspace_symbol(query)
        elif action == "implementation":
            result = await adapter.implementation(file_path, line, column)
        elif action == "incomingCalls":
            result = await adapter.incoming_calls(file_path, line, column)
        elif action == "outgoingCalls":
            result = await adapter.outgoing_calls(file_path, line, column)
        elif action == "diagnostics":
            result = await adapter.diagnostics(file_path)
        else:
            raise ToolError(f"未知的LSP操作: {action}")

        return result

    async def _execute_with_fallback(
        self,
        action: str,
        file_path: str,
        line: int,
        column: int,
        symbol: str,
        root_path: str,
    ) -> str:
        """使用旧的纯Python实现执行（回退）"""
        logger.info(f"使用回退实现执行: {action}")

        workdir = self.get_context("workdir")
        workdir = Path(workdir) if workdir else get_working_directory()
        search_path = Path(root_path) if root_path else workdir

        # 高级LSP操作只在新LSP模式下支持
        if action in ["workspaceSymbol", "implementation", "incomingCalls", "outgoingCalls", "diagnostics"]:
            return f"操作 '{action}' 需要安装对应语言服务器（例如: uv pip install python-lsp-server）"

        if action == "definition":
            if not symbol:
                # 尝试从文件中提取符号
                symbol = extract_symbol_from_line(file_path, line, column)
                if not symbol:
                    return f"无法确定符号位置: {file_path}:{line}:{column}"
            return search_definition(symbol, search_path)

        elif action == "references":
            if not symbol:
                # 尝试从文件提取符号
                symbol = extract_symbol_from_line(file_path, line, column)

            if not symbol:
                raise ToolError("需要提供 symbol 参数或指定文件位置")

            return search_references(symbol, search_path)

        elif action == "symbols":
            return get_symbols(file_path, symbol)

        elif action == "hover":
            if not symbol:
                symbol = extract_symbol_from_line(file_path, line, column)

            if not symbol:
                raise ToolError("需要提供 symbol 参数")

            return get_hover_info(symbol)

        else:
            raise ToolError(f"未知的LSP操作: {action}")

    async def cleanup(self) -> None:
        """清理资源"""
        if self._manager is not None:
            await self._manager.close_all()
            self._manager = None

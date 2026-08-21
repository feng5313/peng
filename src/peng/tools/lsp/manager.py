"""
LSP适配器管理器

管理不同语言的LSP适配器
"""

import logging
from pathlib import Path
from typing import Any, Optional, List, Dict

from .adapters import (
    BaseAdapter,
    PythonAdapter,
    ClangdAdapter,
    RustAnalyzerAdapter,
    GoplsAdapter,
    ZlsAdapter,
)

# 尝试导入TSLanguageServerAdapter（TS/JS适配器）
try:
    from .adapters import TSLanguageServerAdapter
except ImportError:
    TSLanguageServerAdapter = None

# 尝试导入LuaLSAdapter（Lua适配器）
try:
    from .adapters import LuaLSAdapter
except ImportError:
    LuaLSAdapter = None

from .language import get_server_type, is_supported

logger = logging.getLogger(__name__)


class AdapterManager:
    """
    LSP适配器管理器

    负责创建和管理各语言的LSP适配器
    """

    def __init__(self, root_path: str):
        """
        初始化适配器管理器

        Args:
            root_path: 项目根目录
        """
        self.root_path = root_path
        self._adapters: Dict[str, BaseAdapter] = {}

    def get_adapter(self, file_path: str) -> Optional[BaseAdapter]:
        """
        根据文件路径获取对应的适配器

        Args:
            file_path: 文件路径

        Returns:
            适配器实例，如果不支持则返回None
        """
        extension = Path(file_path).suffix
        server_type = get_server_type(extension)

        if server_type is None:
            logger.debug(f"文件扩展名 {extension} 不支持LSP")
            return None

        # 检查是否已有适配器
        if server_type in self._adapters:
            return self._adapters[server_type]

        # 创建新的适配器
        adapter = self._create_adapter(server_type)
        if adapter:
            self._adapters[server_type] = adapter
            logger.info(f"为 {server_type} 创建了适配器")

        return adapter

    def _create_adapter(self, server_type: str) -> Optional[BaseAdapter]:
        """
        根据服务器类型创建适配器

        Args:
            server_type: 服务器类型

        Returns:
            适配器实例，如果不支持则返回None
        """
        # 注册所有适配器
        adapter_map = {
            "python": PythonAdapter,
            "c": ClangdAdapter,
            "cpp": ClangdAdapter,
            "rust": RustAnalyzerAdapter,
            "golang": GoplsAdapter,
            "typescript": TSLanguageServerAdapter,
            "javascript": TSLanguageServerAdapter,
            "zig": ZlsAdapter,
            "lua": LuaLSAdapter,
        }

        adapter_class = adapter_map.get(server_type)
        if adapter_class is None:
            logger.warning(f"未知的服务器类型: {server_type}")
            return None

        try:
            return adapter_class(self.root_path)
        except Exception as e:
            logger.error(f"创建 {server_type} 适配器失败: {e}")
            return None

    async def close_all(self) -> None:
        """关闭所有适配器"""
        for adapter in self._adapters.values():
            try:
                await adapter.close()
            except Exception as e:
                logger.error(f"关闭适配器失败: {e}")

        self._adapters.clear()
        logger.info("所有LSP适配器已关闭")

    def is_supported(self, file_path: str) -> bool:
        """
        检查文件是否支持LSP

        Args:
            file_path: 文件路径

        Returns:
            是否支持LSP
        """
        return is_supported(Path(file_path).suffix)

    async def definition(self, file_path: str, line: int, column: int) -> Optional[Dict]:
        """
        查找定义

        Args:
            file_path: 文件路径
            line: 行号（从1开始）
            column: 列号（从1开始）

        Returns:
            定义位置信息，如果不支持则返回None
        """
        adapter = self.get_adapter(file_path)
        if adapter is None:
            logger.debug(f"文件 {file_path} 不支持LSP definition")
            return None

        try:
            return await adapter.definition(file_path, line, column)
        except Exception as e:
            logger.error(f"执行definition失败: {e}")
            return None

    async def references(self, file_path: str, line: int, column: int) -> List[Dict]:
        """
        查找引用

        Args:
            file_path: 文件路径
            line: 行号（从1开始）
            column: 列号（从1开始）

        Returns:
            引用位置列表
        """
        adapter = self.get_adapter(file_path)
        if adapter is None:
            logger.debug(f"文件 {file_path} 不支持LSP references")
            return []

        try:
            return await adapter.references(file_path, line, column)
        except Exception as e:
            logger.error(f"执行references失败: {e}")
            return []

    async def hover(self, file_path: str, line: int, column: int) -> Optional[Dict]:
        """
        获取悬停信息

        Args:
            file_path: 文件路径
            line: 行号（从1开始）
            column: 列号（从1开始）

        Returns:
            悬停信息，如果不支持则返回None
        """
        adapter = self.get_adapter(file_path)
        if adapter is None:
            logger.debug(f"文件 {file_path} 不支持LSP hover")
            return None

        try:
            return await adapter.hover(file_path, line, column)
        except Exception as e:
            logger.error(f"执行hover失败: {e}")
            return None

    async def symbols(self, file_path: str) -> List[Dict]:
        """
        获取文档符号

        Args:
            file_path: 文件路径

        Returns:
            符号列表
        """
        adapter = self.get_adapter(file_path)
        if adapter is None:
            logger.debug(f"文件 {file_path} 不支持LSP symbols")
            return []

        try:
            return await adapter.symbols(file_path)
        except Exception as e:
            logger.error(f"执行symbols失败: {e}")
            return []

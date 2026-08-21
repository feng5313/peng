"""
Rust LSP适配器

使用rust-analyzer作为语言服务器
"""

import logging
from pathlib import Path
import re
from pathlib import Path

from .base import BaseAdapter
from ..path_resolver import get_global_resolver, ServerType

logger = logging.getLogger(__name__)


class RustAnalyzerAdapter(BaseAdapter):
    """
    Rust语言的LSP适配器

    使用rust-analyzer作为语言服务器
    """

    @property
    def language(self) -> str:
        return "rust"

    @property
    def file_extensions(self) -> list[str]:
        return [".rs"]

    @property
    def server_name(self) -> str:
        """LSP服务器名称（用于依赖项管理）"""
        return "rust-analyzer"

    @property
    def server_type(self) -> ServerType:
        """LSP服务器类型（用于路径解析）"""
        return ServerType.RUST_ANALYZER

    def get_server_command(self) -> list[str]:
        """
        获取启动rust-analyzer的命令

        Returns:
            命令列表
        """
        # 使用路径解析器查找rust-analyzer（不依赖PATH）
        resolver = get_global_resolver(Path(self.root_path))
        command = resolver.get_server_command(ServerType.RUST_ANALYZER)

        if command is None:
            raise RuntimeError(
                "未找到rust-analyzer，请安装：\n"
                "  rustup component add rust-analyzer\n"
                "或手动安装到以下任一位置：\n"
                "  1. Rust工具链: ~/.cargo/bin/rust-analyzer\n"
                "  2. 用户目录: ~/.local/bin/rust-analyzer\n"
                "  3. 系统目录: /usr/local/bin/rust-analyzer"
            )

        return command

    async def definition(self, file_path: str, line: int, column: int) -> str:
        """查找Rust定义"""
        if not self.has_server():
            logger.warning("未找到rust-analyzer，使用回退实现")
            return self._fallback_definition(file_path, line, column)

        try:
            result = await super().definition(file_path, line, column)
            return result
        except Exception as e:
            logger.error(f"rust-analyzer失败: {e}，使用回退实现")
            return self._fallback_definition(file_path, line, column)

    def _fallback_definition(self, file_path: str, line: int, column: int) -> str:
        """
        回退实现：简单的正则搜索
        """
        try:
            path = Path(file_path)
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()

            if 0 < line <= len(lines):
                current_line = lines[line - 1]
                pattern = r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b"
                words = re.findall(pattern, current_line)

                col_start = 0
                for word in words:
                    word_start = current_line.find(word, col_start)
                    if word_start == -1:
                        break
                    word_end = word_start + len(word)
                    if word_start < column <= word_end:
                        return self._search_definition_in_files(word, path.parent)
                    col_start = word_end

            return f"无法确定符号位置: {file_path}:{line}:{column}"

        except Exception as e:
            logger.error(f"回退实现失败: {e}")
            return f"错误: {e}"

    def _search_definition_in_files(self, symbol: str, search_path) -> str:
        """在文件中搜索符号定义"""
        patterns = [
            rf"\bfn\s+{re.escape(symbol)}\b",
            rf"\bstruct\s+{re.escape(symbol)}\b",
            rf"\benum\s+{re.escape(symbol)}\b",
            rf"\bimpl\s+{re.escape(symbol)}\b",
            rf"\btrait\s+{re.escape(symbol)}\b",
            rf"\bmod\s+{re.escape(symbol)}\b",
            rf"\bconst\s+{re.escape(symbol)}\b",
            rf"\blet\s+mut\s+{re.escape(symbol)}\b",
            rf"\blet\s+{re.escape(symbol)}\b",
        ]

        results = []
        for rs_file in search_path.rglob("*.rs"):
            if any(p in rs_file.parts for p in [".git", "__pycache__", "node_modules", "target"]):
                continue

            try:
                with open(rs_file, encoding="utf-8", errors="ignore") as f:
                    for i, line_text in enumerate(f, 1):
                        for pattern in patterns:
                            if re.search(pattern, line_text):
                                rel_path = rs_file.relative_to(search_path)
                                results.append(f"{rel_path}:{i}:{line_text.strip()}")
                                break
                        if len(results) >= 10:
                            break
                if len(results) >= 10:
                    break
            except Exception:
                continue

        if results:
            return "找到定义:\n" + "\n".join(results[:10])
        return f"未找到 '{symbol}' 的定义"

    async def references(self, file_path: str, line: int, column: int) -> str:
        """查找Rust引用"""
        if not self.has_server():
            logger.warning("未找到rust-analyzer，使用回退实现")
            return self._fallback_references(file_path, line, column)

        try:
            result = await super().references(file_path, line, column)
            return result
        except Exception as e:
            logger.error(f"rust-analyzer失败: {e}，使用回退实现")
            return self._fallback_references(file_path, line, column)

    def _fallback_references(self, file_path: str, line: int, column: int) -> str:
        """回退实现：查找引用"""
        try:
            path = Path(file_path)
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()

            symbol = None
            if 0 < line <= len(lines):
                current_line = lines[line - 1]
                pattern = r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b"
                words = re.findall(pattern, current_line)

                col_start = 0
                for word in words:
                    word_start = current_line.find(word, col_start)
                    if word_start == -1:
                        break
                    word_end = word_start + len(word)
                    if word_start < column <= word_end:
                        symbol = word
                        break
                    col_start = word_end

            if not symbol:
                return f"无法确定符号位置: {file_path}:{line}:{column}"

            # 搜索引用
            results = []
            search_pattern = rf"\b{re.escape(symbol)}\b"
            for rs_file in path.parent.rglob("*.rs"):
                if any(p in rs_file.parts for p in [".git", "__pycache__", "node_modules", "target"]):
                    continue

                try:
                    with open(rs_file, encoding="utf-8", errors="ignore") as f:
                        for i, line_text in enumerate(f, 1):
                            if re.search(search_pattern, line_text):
                                rel_path = rs_file.relative_to(path.parent)
                                results.append(f"{rel_path}:{i}:{line_text.strip()}")
                                if len(results) >= 20:
                                    break
                except Exception:
                    continue
                if len(results) >= 20:
                    break

            if results:
                return f"找到 {len(results)} 处引用:\n" + "\n".join(results[:20])
            return f"未找到 '{symbol}' 的引用"

        except Exception as e:
            logger.error(f"回退实现失败: {e}")
            return f"错误: {e}"

    async def symbols(self, file_path: str) -> str:
        """查找Rust符号"""
        if not self.has_server():
            logger.warning("未找到rust-analyzer，使用回退实现")
            return self._fallback_symbols(file_path)

        try:
            result = await super().symbols(file_path)
            return result
        except Exception as e:
            logger.error(f"rust-analyzer失败: {e}，使用回退实现")
            return self._fallback_symbols(file_path)

    def _fallback_symbols(self, file_path: str) -> str:
        """回退实现：查找符号"""
        try:
            path = Path(file_path)
            if not path.exists():
                return f"文件不存在: {file_path}"

            results = []
            with open(path, encoding="utf-8") as f:
                for i, line_text in enumerate(f, 1):
                    # 查找函数、结构体、枚举、trait、impl、模块定义
                    if re.search(r"\b(fn|struct|enum|trait|impl|mod)\s+\w+", line_text):
                        results.append(f"{i}: {line_text.strip()}")

            if results:
                return f"文件 {file_path} 中的符号:\n" + "\n".join(results[:30])
            return f"未在 {file_path} 中找到符号"

        except Exception as e:
            logger.error(f"回退实现失败: {e}")
            return f"错误: {e}"

    async def hover(self, file_path: str, line: int, column: int) -> str:
        """获取Rust悬停信息"""
        if not self.has_server():
            logger.warning("未找到rust-analyzer，使用回退实现")
            return self._fallback_hover(file_path, line, column)

        try:
            result = await super().hover(file_path, line, column)
            return result
        except Exception as e:
            logger.error(f"rust-analyzer失败: {e}，使用回退实现")
            return self._fallback_hover(file_path, line, column)

    def _fallback_hover(self, file_path: str, line: int, column: int) -> str:
        """回退实现：获取悬停信息"""
        try:
            path = Path(file_path)
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()

            symbol = None
            if 0 < line <= len(lines):
                current_line = lines[line - 1]
                pattern = r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b"
                words = re.findall(pattern, current_line)

                col_start = 0
                for word in words:
                    word_start = current_line.find(word, col_start)
                    if word_start == -1:
                        break
                    word_end = word_start + len(word)
                    if word_start < column <= word_end:
                        symbol = word
                        break
                    col_start = word_end

            if not symbol:
                return f"无法确定符号位置: {file_path}:{line}:{column}"

            # 尝试在文件中查找该符号的类型定义
            type_hints = []
            patterns = [
                rf"\bstruct\s+{re.escape(symbol)}\s*(<[^>]*>)?\s*(\{{[^}}]*\}})?",
                rf"\benum\s+{re.escape(symbol)}\s*(<[^>]*>)?\s*(\{{[^}}]*\}})?",
                rf"\btrait\s+{re.escape(symbol)}\s*(<[^>]*>)?",
                rf"\bfn\s+{re.escape(symbol)}\s*<[^>]*>\s*\(",
                rf"\bfn\s+{re.escape(symbol)}\s*\(",
                rf"\bimpl\s+{re.escape(symbol)}\s*(<[^>]*>)?",
                rf"\bconst\s+{re.escape(symbol)}\s*:",
                rf"\btype\s+{re.escape(symbol)}\s*=",
            ]

            with open(path, encoding="utf-8") as f:
                for i, line_text in enumerate(f, 1):
                    for pattern in patterns:
                        match = re.search(pattern, line_text)
                        if match:
                            type_hints.append(f"  行 {i}: {line_text.strip()}")
                    if len(type_hints) >= 5:
                        break

            if type_hints:
                return f"'{symbol}' 可能的定义:\n" + "\n".join(type_hints)
            return f"未找到 '{symbol}' 的类型信息"

        except Exception as e:
            logger.error(f"回退实现失败: {e}")
            return f"错误: {e}"

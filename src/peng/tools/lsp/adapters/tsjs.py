"""
TypeScript/JavaScript LSP适配器

使用typescript-language-server作为语言服务器
"""

import logging
from pathlib import Path
import re
from pathlib import Path
from typing import Optional

from .base import BaseAdapter
from ..path_resolver import get_global_resolver, ServerType

logger = logging.getLogger(__name__)


class TSLanguageServerAdapter(BaseAdapter):
    """
    Python语言的LSP适配器

    使用typescript-language-server作为语言服务器
    """

    @property
    def language(self) -> str:
        return "typescript"

    @property
    def file_extensions(self) -> list[str]:
        return ['.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs']

    @property
    def server_name(self) -> str:
        """LSP服务器名称（用于依赖项管理）"""
        return "typescript-language-server"

    @property
    def server_type(self) -> ServerType:
        """LSP服务器类型（用于路径解析）"""
        return ServerType.TYPESCRIPT_LANGUAGE_SERVER

    def get_server_command(self) -> list[str]:
        """
        获取启动typescript-language-server的命令

        Returns:
            命令列表
        """
        # 使用路径解析器查找typescript-language-server（不依赖PATH）
        resolver = get_global_resolver(Path(self.root_path))
        command = resolver.get_server_command(ServerType.TYPESCRIPT_LANGUAGE_SERVER)

        if command is None:
            raise RuntimeError(
                "未找到typescript-language-server，请安装：\n"
                "  npm install -g typescript-language-server\n"
                "或手动安装到以下任一位置：\n"
                "  1. npm全局目录: ~/.npm-global/bin/typescript-language-server\n"
                "  2. 用户目录: ~/.local/bin/typescript-language-server\n"
                "  3. 系统目录: /usr/local/bin/typescript-language-server"
            )

        return command

    async def definition(self, file_path: str, line: int, column: int) -> str:
        """查找TypeScript/JavaScript定义"""
        # 先检查服务器是否可用
        if not self.has_server():
            logger.warning("未找到typescript-language-server，使用回退实现")
            return self._fallback_definition(file_path, line, column)

        try:
            result = await super().definition(file_path, line, column)
            return result
        except Exception as e:
            logger.error(f"typescript-language-server失败: {e}，使用回退实现")
            return self._fallback_definition(file_path, line, column)

    def _fallback_definition(self, file_path: str, line: int, column: int) -> str:
        """
        回退实现：简单的正则搜索

        当没有安装typescript-language-server时使用
        """
        try:
            path = Path(file_path)
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()

            if 0 < line <= len(lines):
                current_line = lines[line - 1]
                # 简单提取单词
                pattern = r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b"
                words = re.findall(pattern, current_line)

                # 找到光标位置附近的词
                col_start = 0
                for word in words:
                    word_start = current_line.find(word, col_start)
                    if word_start == -1:
                        break
                    word_end = word_start + len(word)
                    if word_start < column <= word_end:
                        # 搜索该符号的定义
                        return self._search_definition_in_files(word, path.parent)
                    col_start = word_end

            return f"无法确定符号位置: {file_path}:{line}:{column}"

        except Exception as e:
            logger.error(f"回退实现失败: {e}")
            return f"错误: {e}"

    def _search_definition_in_files(self, symbol: str, search_path) -> str:
        """在文件中搜索符号定义（简单实现）"""
        patterns = [
            rf"\bdef\s+{re.escape(symbol)}\b",
            rf"\bclass\s+{re.escape(symbol)}\b",
        ]

        results = []
        for py_file in search_path.rglob("*.py"):
            # 跳过某些目录
            if any(p in py_file.parts for p in [".git", "__pycache__", "node_modules"]):
                continue

            try:
                with open(py_file, encoding="utf-8", errors="ignore") as f:
                    for i, line_text in enumerate(f, 1):
                        for pattern in patterns:
                            if re.search(pattern, line_text):
                                rel_path = py_file.relative_to(search_path)
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
        """查找Python引用"""
        if not self.has_server():
            logger.warning("未找到typescript-language-server，使用回退实现")
            return self._fallback_references(file_path, line, column)

        try:
            result = await super().references(file_path, line, column)
            return result
        except Exception as e:
            logger.error(f"typescript-language-server失败: {e}，使用回退实现")
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

                # 找到光标位置附近的词
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
            for py_file in path.parent.rglob("*.py"):
                if any(p in py_file.parts for p in [".git", "__pycache__", "node_modules"]):
                    continue

                try:
                    with open(py_file, encoding="utf-8", errors="ignore") as f:
                        for i, line_text in enumerate(f, 1):
                            if re.search(search_pattern, line_text):
                                rel_path = py_file.relative_to(path.parent)
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
        """查找Python符号"""
        if not self.has_server():
            logger.warning("未找到typescript-language-server，使用回退实现")
            return self._fallback_symbols(file_path)

        try:
            result = await super().symbols(file_path)
            return result
        except Exception as e:
            logger.error(f"typescript-language-server失败: {e}，使用回退实现")
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
                    # 查找函数和类定义
                    if re.search(r"\bdef\s+\w+|^\s*class\s+\w+", line_text):
                        results.append(f"{i}: {line_text.strip()}")

            if results:
                return f"文件 {file_path} 中的符号:\n" + "\n".join(results[:30])
            return f"未在 {file_path} 中找到符号"

        except Exception as e:
            logger.error(f"回退实现失败: {e}")
            return f"错误: {e}"

    async def hover(self, file_path: str, line: int, column: int) -> str:
        """获取Python悬停信息"""
        if not self.has_server():
            logger.warning("未找到typescript-language-server，使用回退实现")
            return self._fallback_hover(file_path, line, column)

        try:
            result = await super().hover(file_path, line, column)
            return result
        except Exception as e:
            logger.error(f"typescript-language-server失败: {e}，使用回退实现")
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

                # 找到光标位置附近的词
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

            # 尝试获取文档
            try:
                import io
                import pydoc
                from contextlib import redirect_stdout

                f_obj = io.StringIO()
                with redirect_stdout(f_obj):
                    pydoc.help(symbol)
                doc = f_obj.getvalue()

                if doc and "No Python documentation found" not in doc:
                    return f"'{symbol}' 文档:\n{doc[:2000]}"
            except Exception as e:
                logger.debug(f"获取pydoc失败: {e}")

            return f"未找到 '{symbol}' 的文档信息"

        except Exception as e:
            logger.error(f"回退实现失败: {e}")
            return f"错误: {e}"

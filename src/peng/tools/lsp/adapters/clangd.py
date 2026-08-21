"""
C/C++ LSP适配器

使用clangd作为语言服务器
"""

import logging
from pathlib import Path
import re
from pathlib import Path
from typing import Optional

from .base import BaseAdapter
from ..path_resolver import get_global_resolver, ServerType

logger = logging.getLogger(__name__)


class ClangdAdapter(BaseAdapter):
    """
    C/C++语言的LSP适配器

    使用clangd作为语言服务器
    """

    @property
    def language(self) -> str:
        return "cpp"

    @property
    def file_extensions(self) -> list[str]:
        return [
            ".c", ".h",
            ".cpp", ".hpp", ".cc", ".cxx", ".cppm", ".ixx", ".hxx", ".inl"
        ]

    @property
    def server_name(self) -> str:
        """LSP服务器名称（用于依赖项管理）"""
        return "clangd"

    @property
    def server_type(self) -> ServerType:
        """LSP服务器类型（用于路径解析）"""
        return ServerType.CLANGD

    def get_server_command(self) -> list[str]:
        """
        获取启动clangd的命令

        Returns:
            命令列表
        """
        from pathlib import Path as P
        
        # 对齐neovim LazyVim的clangd参数
        # 关键：--function-arg-placeholders 和 init_options 影响成员变量补全
        clangd_args = [
            "--background-index",            # 启用后台索引
            "--clang-tidy",                  # 启用clang-tidy
            "--header-insertion=iwyu",       # IWYU风格头文件插入
            "--completion-style=detailed",  # 详细补全
            "--function-arg-placeholders",  # 函数参数占位符（关键！影响s_name.补全）
            "--fallback-style=llvm",        # 回退到LLVM风格
        ]
        
        # 保存init_options供_initialize使用
        # 这些是neovim clangd配置中的关键选项
        self._init_options = {
            "usePlaceholders": True,         # 使用占位符（如 $1, $2）
            "completeUnimported": True,      # 补全未导入的类型
            "clangdFileStatus": True,        # 启用clangd文件状态
        }
        for sys_clangd in [P("/usr/local/bin/clangd"), P("/usr/bin/clangd")]:
            if sys_clangd.exists():
                return [str(sys_clangd)] + clangd_args
        
        # 回退到路径解析器
        resolver = get_global_resolver(P(self.root_path))
        command = resolver.get_server_command(
            ServerType.CLANGD,
            additional_args=clangd_args
        )

        if command is None:
            raise RuntimeError(
                "未找到clangd，请安装：\n"
                "  Ubuntu/Debian: sudo apt install clangd\n"
                "  macOS: brew install clangd\n"
                "或手动安装到以下任一位置：\n"
                "  1. 用户目录: ~/.local/bin/clangd\n"
                "  2. 系统目录: /usr/local/bin/clangd 或 /usr/bin/clangd"
            )

        return command

    async def definition(self, file_path: str, line: int, column: int) -> str:
        """查找C/C++定义"""
        if not self.has_server():
            logger.warning("未找到clangd，使用回退实现")
            return self._fallback_definition(file_path, line, column)

        try:
            result = await super().definition(file_path, line, column)
            return result
        except Exception as e:
            logger.error(f"clangd失败: {e}，使用回退实现")
            return self._fallback_definition(file_path, line, column)

    def _fallback_definition(self, file_path: str, line: int, column: int) -> str:
        """回退实现：简单的正则搜索"""
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
            rf"\b(?:class|struct)\s+{re.escape(symbol)}\b",
            rf"\b(?:void|int|char|float|double|bool|auto|std::string)\s+{re.escape(symbol)}\s*\(",
            rf"\b{re.escape(symbol)}\s*=\s*",
        ]

        results = []
        for ext in [".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx"]:
            for code_file in search_path.rglob(f"*{ext}"):
                if any(p in code_file.parts for p in [".git", "__pycache__", "node_modules"]):
                    continue

                try:
                    with open(code_file, encoding="utf-8", errors="ignore") as f:
                        for i, line_text in enumerate(f, 1):
                            for pattern in patterns:
                                if re.search(pattern, line_text):
                                    rel_path = code_file.relative_to(search_path)
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
        """查找C/C++引用"""
        if not self.has_server():
            logger.warning("未找到clangd，使用回退实现")
            return self._fallback_references(file_path, line, column)

        try:
            result = await super().references(file_path, line, column)
            return result
        except Exception as e:
            logger.error(f"clangd失败: {e}，使用回退实现")
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
            for ext in [".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx"]:
                for code_file in path.parent.rglob(f"*{ext}"):
                    if any(p in code_file.parts for p in [".git", "__pycache__", "node_modules"]):
                        continue

                    try:
                        with open(code_file, encoding="utf-8", errors="ignore") as f:
                            for i, line_text in enumerate(f, 1):
                                if re.search(search_pattern, line_text):
                                    rel_path = code_file.relative_to(path.parent)
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
        """查找C/C++符号"""
        if not self.has_server():
            logger.warning("未找到clangd，使用回退实现")
            return self._fallback_symbols(file_path)

        try:
            result = await super().symbols(file_path)
            return result
        except Exception as e:
            logger.error(f"clangd失败: {e}，使用回退实现")
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
                    # 查找类、结构体、函数定义
                    if re.search(r"\b(?:class|struct|namespace|enum)\s+\w+|^\s*\w+.*\([^)]*\)\s*(?:const\s*)?{?;?", line_text):
                        results.append(f"{i}: {line_text.strip()}")

            if results:
                return f"文件 {file_path} 中的符号:\n" + "\n".join(results[:30])
            return f"未在 {file_path} 中找到符号"

        except Exception as e:
            logger.error(f"回退实现失败: {e}")
            return f"错误: {e}"

    async def hover(self, file_path: str, line: int, column: int) -> str:
        """获取C/C++悬停信息"""
        if not self.has_server():
            logger.warning("未找到clangd，使用回退实现")
            return self._fallback_hover(file_path, line, column)

        try:
            result = await super().hover(file_path, line, column)
            return result
        except Exception as e:
            logger.error(f"clangd失败: {e}，使用回退实现")
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

            # 尝试在包含文件中查找定义
            results = []
            patterns = [
                rf"\b(?:class|struct)\s+{re.escape(symbol)}\b",
                rf"\b(?:void|int|char|float|double|bool|auto)\s+{re.escape(symbol)}\s*\(",
            ]

            for ext in [".h", ".hpp", ".hxx"]:
                for code_file in path.parent.rglob(f"*{ext}"):
                    if any(p in code_file.parts for p in [".git", "__pycache__", "node_modules"]):
                        continue

                    try:
                        with open(code_file, encoding="utf-8", errors="ignore") as f:
                            for i, line_text in enumerate(f, 1):
                                for pattern in patterns:
                                    if re.search(pattern, line_text):
                                        rel_path = code_file.relative_to(path.parent)
                                        results.append(f"{rel_path}:{i}:{line_text.strip()}")
                                        break
                                if len(results) >= 5:
                                    break
                        if len(results) >= 5:
                            break
                    except Exception:
                        continue

            if results:
                return f"'{symbol}' 可能的定义:\n" + "\n".join(results[:5])
            return f"未找到 '{symbol}' 的文档信息"

        except Exception as e:
            logger.error(f"回退实现失败: {e}")
            return f"错误: {e}"
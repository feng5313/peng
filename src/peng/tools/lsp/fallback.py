"""
LSP回退实现（纯Python实现）

当语言服务器不可用时使用
"""

import logging
import re
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 支持的代码文件扩展名
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt",
    ".rb", ".php", ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".hxx",
    ".lua", ".zig"
}

# 预编译正则表达式（用于标识符提取）
_IDENTIFIER_PATTERN = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b")


def _is_extension_supported(file_path: str) -> bool:
    """检查文件扩展名是否支持"""
    if not file_path:
        return False
    ext = Path(file_path).suffix
    return ext in CODE_EXTENSIONS


def _search_in_files(
    pattern: str,
    search_path: Path,
    file_extensions: set[str] | None = None,
    max_results: int = 20,
) -> list[str]:
    """
    在文件中搜索模式（纯 Python 实现，跨平台）

    Args:
        pattern: 正则表达式模式
        search_path: 搜索目录
        file_extensions: 文件扩展名过滤
        max_results: 最大结果数

    Returns:
        匹配结果列表（格式：文件路径:行号:内容）
    """
    if file_extensions is None:
        file_extensions = CODE_EXTENSIONS

    results: list[str] = []
    regex = re.compile(pattern)

    # 忽略的目录
    ignore_dirs = {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".mypy_cache",
        ".ruff_cache",
    }

    # 收集所有文件
    code_files = []
    try:
        for file_path in search_path.rglob("*"):
            # 跳过忽略目录
            if any(part in ignore_dirs for part in file_path.parts):
                continue
            # 只处理代码文件
            if file_path.is_file() and file_path.suffix in file_extensions:
                code_files.append(file_path)
    except Exception as e:
        logger.error(f"❌ _search_in_files() - fallback.py: 异常 - {e}\n{traceback.format_exc()}")

    if not code_files:
        return results

    def _search_file(file_path: Path) -> list[str]:
        """搜索单个文件"""
        local_results = []
        try:
            with open(file_path, encoding="utf-8", errors="ignore") as f:
                for line_num, line_text in enumerate(f, 1):
                    if regex.search(line_text):
                        rel_path = file_path.relative_to(search_path)
                        local_results.append(f"{rel_path}:{line_num}:{line_text.strip()}")
        except Exception as e:
            logger.error(f"❌ _search_file() - fallback.py: 异常 - {e}\n{traceback.format_exc()}")
        return local_results

    # 并行搜索文件
    with ThreadPoolExecutor(max_workers=8) as executor:
        all_results = list(executor.map(_search_file, code_files))

    # 合并结果，直到达到max_results
    for local_results in all_results:
        results.extend(local_results)
        if len(results) >= max_results:
            return results[:max_results]

    return results


def search_definition(symbol: str, search_path: Path) -> str:
    """
    搜索符号定义（纯 Python 实现）

    Args:
        symbol: 符号名称
        search_path: 搜索路径

    Returns:
        搜索结果字符串
    """
    # 定义模式
    patterns = [
        rf"\bdef\s+{re.escape(symbol)}\b",
        rf"\bclass\s+{re.escape(symbol)}\b",
        rf"\bfunction\s+{re.escape(symbol)}\b",
        rf"\bconst\s+{re.escape(symbol)}\b",
        rf"\blet\s+{re.escape(symbol)}\b",
        rf"\bvar\s+{re.escape(symbol)}\b",
        rf"\b{re.escape(symbol)}\s*=",
    ]

    results = []
    for pattern in patterns:
        found = _search_in_files(pattern, search_path, max_results=10)
        results.extend(found)
        if len(results) >= 10:
            break

    if results:
        return "找到定义:\n" + "\n".join(results[:10])
    return f"未找到 '{symbol}' 的定义"


def search_references(symbol: str, search_path: Path) -> str:
    """
    搜索符号引用（纯 Python 实现）

    Args:
        symbol: 符号名称
        search_path: 搜索路径

    Returns:
        搜索结果字符串
    """
    pattern = rf"\b{re.escape(symbol)}\b"
    results = _search_in_files(pattern, search_path, max_results=20)

    if results:
        return f"找到 {len(results)} 处引用:\n" + "\n".join(results[:20])
    return f"未找到 '{symbol}' 的引用"


def extract_symbol_from_line(file_path: str, line: int, column: int) -> str | None:
    """
    从文件中提取光标位置的符号

    Args:
        file_path: 文件路径
        line: 行号（从1开始）
        column: 列号（从1开始）

    Returns:
        符号名称，如果无法提取则返回None
    """
    try:
        path = Path(file_path)
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        if 0 < line <= len(lines):
            current_line = lines[line - 1]
            words = _IDENTIFIER_PATTERN.findall(current_line)
            # 找到光标位置附近的词
            col_start = 0
            for word in words:
                word_start = current_line.find(word, col_start)
                if word_start == -1:
                    break
                word_end = word_start + len(word)
                if word_start < column <= word_end:
                    return word
                col_start = word_end
    except Exception as e:
        logger.error(f"提取符号失败: {e}")
    return None


def get_symbols(file_path: str, symbol_filter: str = "") -> str:
    """
    获取文件符号（纯 Python 实现）

    Args:
        file_path: 文件路径
        symbol_filter: 符号过滤器（可选）

    Returns:
        符号列表字符串
    """
    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        raise ValueError(f"文件不存在: {file_path}")

    results = []
    try:
        with open(path, encoding="utf-8") as f:
            for i, line_text in enumerate(f, 1):
                if "def " in line_text or "class " in line_text or "function " in line_text:
                    if not symbol_filter or symbol_filter in line_text:
                        results.append(f"{i}: {line_text.strip()}")
    except Exception as e:
        logger.error(f"读取文件失败: {e}\n{traceback.format_exc()}")
        raise ValueError(f"读取文件失败: {e}") from e

    if results:
        return f"文件 {file_path} 中的符号:\n" + "\n".join(results[:30])
    return f"未在 {file_path} 中找到符号"


def get_hover_info(symbol: str) -> str:
    """
    获取符号的悬停信息（纯 Python 实现）

    Args:
        symbol: 符号名称

    Returns:
        悬停信息字符串
    """
    # 尝试使用 pydoc 获取文档
    try:
        import io
        import pydoc
        from contextlib import redirect_stdout

        f_io = io.StringIO()
        with redirect_stdout(f_io):
            pydoc.help(symbol)
        doc = f_io.getvalue()

        if doc and "No Python documentation found" not in doc:
            return f"'{symbol}' 文档:\n{doc[:2000]}"
    except Exception as e:
        logger.error(f"pydoc失败: {e}")

    return f"未找到 '{symbol}' 的文档信息"

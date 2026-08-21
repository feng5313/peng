"""
语言检测和映射模块
"""

from pathlib import Path
from typing import Optional

# 文件扩展名到语言的映射
EXTENSION_TO_LANGUAGE = {
    # Python
    ".py": "python",
    ".pyx": "python",
    ".pyi": "python",
    ".pyw": "python",

    # C/C++
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".cppm": "cpp",
    ".ixx": "cpp",
    ".hxx": "cpp",
    ".inl": "cpp",

    # Lua
    ".lua": "lua",

    # TypeScript/JavaScript
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",

    # Zig
    ".zig": "zig",

    # Rust
    ".rs": "rust",

    # Go
    ".go": "golang",

    # 可选支持
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".dart": "dart",
    ".cs": "csharp",
    ".csx": "csharp",
    ".r": "r",
    ".R": "r",
}


def detect_language(file_path: str) -> Optional[str]:
    """
    根据文件路径检测语言

    Args:
        file_path: 文件路径

    Returns:
        语言名称，如果无法检测则返回None
    """
    path = Path(file_path)
    ext = path.suffix.lower()
    return EXTENSION_TO_LANGUAGE.get(ext)


def get_file_extensions(language: str) -> list[str]:
    """
    获取语言对应的文件扩展名

    Args:
        language: 语言名称

    Returns:
        扩展名列表
    """
    return [ext for ext, lang in EXTENSION_TO_LANGUAGE.items() if lang == language]
    return [ext for ext, lang in EXTENSION_TO_LANGUAGE.items() if lang == language]


def get_server_type(extension: str) -> Optional[str]:
    """
    根据文件扩展名获取LSP服务器类型

    Args:
        extension: 文件扩展名（如.py, .js等）

    Returns:
        LSP服务器类型，如果不支持则返回None
    """
    if not extension:
        return None
    if not extension.startswith('.'):
        extension = '.' + extension
    return EXTENSION_TO_LANGUAGE.get(extension.lower())


def is_supported(extension: str) -> bool:
    """
    检查文件扩展名是否支持LSP

    Args:
        extension: 文件扩展名

    Returns:
        是否支持LSP
    """
    return get_server_type(extension) is not None

"""
LSP适配器模块

为不同语言提供统一的LSP接口
"""

from .base import BaseAdapter
from .python import PythonAdapter
from .clangd import ClangdAdapter
from .rust import RustAnalyzerAdapter
from .golang import GoplsAdapter
from .zig import ZlsAdapter
from .lua import LuaLSAdapter

try:
    from .tsjs import TSLanguageServerAdapter
except ImportError:
    TSLanguageServerAdapter = None

# 别名，方便使用
TypeScriptAdapter = TSLanguageServerAdapter
JavaScriptAdapter = TSLanguageServerAdapter

__all__ = [
    "BaseAdapter",
    "PythonAdapter",
    "ClangdAdapter",
    "RustAnalyzerAdapter",
    "GoplsAdapter",
    "TSLanguageServerAdapter",
    "TypeScriptAdapter",
    "JavaScriptAdapter",
    "ZlsAdapter",
    "LuaLSAdapter",
]
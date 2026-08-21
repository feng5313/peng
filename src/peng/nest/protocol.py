"""
Nest 消息协议定义

WebSocket 通信使用 JSON 消息格式。

请求格式：
{
    "id": "uuid-string",
    "type": "bash|read|write|edit|glob|grep|list_dir|stat|list_windows|screenshot|ping",
    "params": { ... }
}

响应格式：
{
    "id": "uuid-string",           # 对应请求的 id
    "type": "result|error|stream", # 响应类型
    "data": ...,                   # 结果数据
    "error": "...",                # 错误信息（type=error时）
    "done": true                   # 流式响应结束标记（type=stream时）
}
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from typing import Any


class RequestType(str, enum.Enum):
    """请求类型"""
    # 系统命令
    PING = "ping"
    
    # Shell 命令执行
    BASH = "bash"
    
    # 文件操作
    READ = "read"          # 读取文件/目录
    WRITE = "write"        # 写入文件
    EDIT = "edit"          # 编辑文件（精确替换）
    LINE_EDIT = "line_edit"  # 按行编辑（删除/替换/插入）
    STAT = "stat"          # 获取文件信息
    LIST_DIR = "list_dir"  # 列出目录内容
    LIST_DRIVES = "list_drives"  # 列出盘符（Windows平台）
    
    # 搜索
    GLOB = "glob"          # Glob 模式匹配
    GREP = "grep"          # 正则搜索
    
    # 窗口管理
    LIST_WINDOWS = "list_windows"   # 列出窗口
    SCREENSHOT = "screenshot"       # 截屏
    INPUT = "input"                 # 鼠标/键盘输入
    
    # 技能
    LOAD_SKILL = "load_skill"  # 加载技能文件
    
    # 交互式终端（WebUI远程终端使用）
    TERMINAL_CREATE = "terminal_create"    # 创建PTY终端会话
    TERMINAL_WRITE = "terminal_write"      # 向PTY写入数据
    TERMINAL_RESIZE = "terminal_resize"    # 调整PTY窗口大小
    TERMINAL_CLOSE = "terminal_close"      # 关闭PTY终端会话
    
    # 配置同步（host ↔ nest 双向同步 workdir）
    SET_WORKDIR = "set_workdir"            # host → nest: 设置远程nest的workdir
    
    # 调试（在 nest 端执行 Python 代码）
    PYTHON_EXEC = "python_exec"             # exec 执行 Python 语句（修改状态）
    PYTHON_EVAL = "python_eval"             # eval 执行 Python 表达式（查询状态）
    
    # CLI 远程执行（task/msg 命令远程转发）
    CLI_COMMAND = "cli_command"             # 远程执行 CLI 命令（task/msg）
    
    # LSP查询（绕过bash，直接在nest agent进程中TCP连接daemon）
    LSP_QUERY = "lsp_query"


class ResponseType(str, enum.Enum):
    """响应类型"""
    RESULT = "result"    # 最终结果
    ERROR = "error"      # 错误
    STREAM = "stream"    # 流式数据（bash 命令的实时输出）


@dataclass
class NestRequest:
    """Nest 请求消息"""
    type: str  # RequestType value
    params: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    
    def to_dict(self) -> dict[str, Any]:
        """转换为字典（用于 JSON 序列化）"""
        return {
            "id": self.id,
            "type": self.type,
            "params": self.params,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NestRequest:
        """从字典创建"""
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            type=data["type"],
            params=data.get("params", {}),
        )


@dataclass
class NestResponse:
    """Nest 响应消息"""
    id: str           # 对应请求的 id
    type: str = ResponseType.RESULT.value  # 响应类型
    data: Any = None  # 结果数据
    error: str | None = None  # 错误信息
    done: bool = True  # 是否完成（流式响应使用）
    
    def to_dict(self) -> dict[str, Any]:
        """转换为字典（用于 JSON 序列化）"""
        result = {
            "id": self.id,
            "type": self.type,
        }
        if self.data is not None:
            result["data"] = self.data
        if self.error is not None:
            result["error"] = self.error
        if not self.done:
            result["done"] = False
        return result
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NestResponse:
        """从字典创建"""
        return cls(
            id=data["id"],
            type=data.get("type", ResponseType.RESULT.value),
            data=data.get("data"),
            error=data.get("error"),
            done=data.get("done", True),
        )
    
    @classmethod
    def make_error(cls, request_id: str, error: str) -> NestResponse:
        """创建错误响应"""
        return cls(
            id=request_id,
            type=ResponseType.ERROR.value,
            error=error,
        )
    
    def get(self, key: str, default=None):
        """兼容 dict 接口 - 访问 data 字典"""
        if isinstance(self.data, dict):
            return self.data.get(key, default)
        return default
    
    def __getitem__(self, key):
        """兼容 dict 接口"""
        if isinstance(self.data, dict):
            return self.data[key]
        raise KeyError(key)
    
    @classmethod
    def make_result(cls, request_id: str, data: Any) -> NestResponse:
        """创建成功响应"""
        return cls(
            id=request_id,
            type=ResponseType.RESULT.value,
            data=data,
        )
    
    @classmethod
    def make_stream(cls, request_id: str, data: Any, done: bool = False) -> NestResponse:
        """创建流式响应"""
        return cls(
            id=request_id,
            type=ResponseType.STREAM.value,
            data=data,
            done=done,
        )


class NestError(Exception):
    """Nest 协议错误"""
    
    def __init__(self, message: str, code: str = "NEST_ERROR"):
        super().__init__(message)
        self.code = code

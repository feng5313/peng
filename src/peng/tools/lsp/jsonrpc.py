"""
JSON-RPC协议实现

提供LSP通信所需的JSON-RPC消息编解码功能
"""

import json
from typing import Any


class JSONRPCRequest:
    """JSON-RPC请求"""
    
    def __init__(self, method: str, params: Any = None, request_id: int | None = None):
        self.method = method
        self.params = params
        self.id = request_id
    
    def to_dict(self) -> dict:
        """转换为字典"""
        result = {
            "jsonrpc": "2.0",
            "method": self.method,
        }
        if self.params is not None:
            result["params"] = self.params
        if self.id is not None:
            result["id"] = self.id
        return result
    
    def encode(self) -> str:
        """编码为JSON字符串"""
        return json.dumps(self.to_dict())
    
    def encode_with_headers(self) -> bytes:
        """编码为带LSP头的消息"""
        content = self.encode()
        header = f"Content-Length: {len(content)}\r\n\r\n"
        return (header + content).encode("utf-8")


class JSONRPCResponse:
    """JSON-RPC响应"""
    
    def __init__(self, request_id: int | str, result: Any = None, error: dict | None = None):
        self.id = request_id
        self.result = result
        self.error = error
    
    @classmethod
    def from_dict(cls, data: dict) -> "JSONRPCResponse":
        """从字典创建"""
        return cls(
            request_id=data.get("id"),
            result=data.get("result"),
            error=data.get("error")
        )
    
    def is_error(self) -> bool:
        """是否是错误响应"""
        return self.error is not None


def parse_jsonrpc_message(data: str) -> dict:
    """解析JSON-RPC消息"""
    return json.loads(data)


def decode_content_length(header: str) -> int | None:
    """从头部解析Content-Length"""
    for line in header.split("\r\n"):
        if line.lower().startswith("content-length:"):
            try:
                return int(line.split(":")[1].strip())
            except (ValueError, IndexError):
                return None
    return None
"""
共享类型定义

统一的数据类型，避免重复定义。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from peng.common import jsonp as json


class MetaSingleton(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in MetaSingleton._instances:
            MetaSingleton._instances[cls] = super().__call__(*args, **kwargs)
        return MetaSingleton._instances[cls]
@dataclass
class ToolCall:
    """工具调用"""

    id: str
    name: str
    arguments: dict[str, Any]

    @classmethod
    def from_api_format(cls, data: dict[str, Any]) -> ToolCall:
        """
        从API格式解析

        Args:
            data: API返回的工具调用数据

        Returns:
            ToolCall实例
        """
        args = data.get("function", {}).get("arguments", "{}")
        if isinstance(args, str):
            args = json.loads(args)
        return cls(
            id=data.get("id", ""),
            name=data.get("function", {}).get("name", ""),
            arguments=args,
        )

    def to_api_format(self) -> dict[str, Any]:
        """转换为API格式"""
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }

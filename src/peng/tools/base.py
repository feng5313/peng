"""
工具基类

定义工具的基本接口。
支持双通道输出：summary（返回给 LLM）和 display（打印到终端）。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ToolError(Exception):
    """工具执行错误"""


@dataclass
class DualOutput:
    """
    双通道输出 - 分离 LLM 上下文和终端显示

    用于工具返回大量数据时，避免污染 LLM 上下文：
    - summary: 精简信息，发送给 LLM（进入对话历史）
    - display: 完整内容，仅打印到终端（不进入 LLM 上下文）
    - attachments: 附件列表（图片、PDF等二进制文件）

    示例:
        return DualOutput(
            summary="成功写入文件: src/main.py (+10/-5 lines)",
            display=colored_diff  # 带颜色的完整 diff
        )

    照搬 opencode read.ts 的图片/PDF 支持:
        return DualOutput(
            summary="Image read successfully",
            attachments=[{
                "type": "file",
                "mime": "image/png",
                "url": "data:image/png;base64,..."
            }]
        )
    """

    summary: str  # 返回给 LLM 的精简摘要
    display: str | None = None  # 打印到终端的完整内容（可选）
    attachments: list[dict[str, str]] | None = field(default_factory=list)  # 附件列表

    def __str__(self) -> str:
        """默认字符串表示返回摘要"""
        return self.summary


class Tool(ABC):
    """工具基类"""

    # 工具别名列表（子类可覆盖）
    # 别名用于在注册表中注册多个名称指向同一个工具实例
    # 例如 BashTool 在 Windows 平台暴露为 "cmd"，但注册主名称为 "bash"
    ALIASES: tuple[str, ...] = ()

    def __init__(self):
        """初始化工具基类"""
        self._llm_client = None
        self._context: dict[str, Any] = {}  # 工具上下文（message_id, task_id等）

    def set_context(self, **kwargs) -> None:
        """
        设置工具上下文

        Args:
            **kwargs: 上下文键值对（如message_id=123, task_id=456）
        """
        self._context.update(kwargs)

    def get_context(self, key: str) -> Any | None:
        """
        获取工具上下文

        Args:
            key: 上下文键名

        Returns:
            上下文值，不存在则返回None
        """
        return self._context.get(key)

    def set_llm_client(self, client) -> None:
        """
        注入LLM客户端

        工具内部可以调用 self._llm_client 使用模型。
        工具可以自行决定使用大模型还是小模型。

        Args:
            client: LLM客户端实例
        """
        self._llm_client = client

    @property
    def llm_client(self):
        """
        获取LLM客户端

        Returns:
            LLM客户端实例，未注入则返回None
        """
        return self._llm_client

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称（可能动态变化，如 bash/cmd）"""

    @property
    def primary_name(self) -> str:
        """工具注册主名称（固定不变，用于注册表 key）
        
        默认实现：返回第一个非动态的名称。
        如果工具的 name 是动态的（如 BashTool 根据 nest 平台变化），
        子类应覆盖此属性返回固定名称。
        """
        return self.name

    @property
    @abstractmethod
    def description(self) -> str:
        """工具描述"""

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """参数定义（JSON Schema格式）"""

    @abstractmethod
    async def execute(self, **kwargs: Any) -> Any:
        """
        执行工具

        Args:
            **kwargs: 工具参数

        Returns:
            执行结果

        Raises:
            ToolError: 执行失败
        """

    def to_openai_format(self) -> dict[str, Any]:
        """
        转换为OpenAI工具格式

        Returns:
            OpenAI格式的工具定义
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

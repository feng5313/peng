"""
LLM模块

提供大模型相关功能：
- GLMClient: LLM API 客户端
- PengAgent: Agent 核心类
- ToolExecutionEngine: 工具执行引擎
- MessageHistory: 消息历史管理
"""

from peng.llm.agent import (
    AgentAction,
    AgentError,
    AgentState,
    MaxIterationsExceededError,
    PengAgent,
)
from peng.llm.client import GLMClient, StreamChunk, StreamStallError, ToolCall
from peng.llm.tool_engine import (
    CompleteEvent,
    ContentEvent,
    EngineEvent,
    MessageHistory,
    ThinkingEvent,
    ToolCallEvent,
    ToolExecutionEngine,
    ToolResultEvent,
)

__all__ = [
    # Client
    "GLMClient",
    "StreamChunk",
    "StreamStallError",
    "ToolCall",
    # Agent
    "PengAgent",
    "AgentState",
    "AgentAction",
    "AgentError",
    "MaxIterationsExceededError",
    # Tool Engine
    "ToolExecutionEngine",
    "MessageHistory",
    "EngineEvent",
    "ContentEvent",
    "ThinkingEvent",
    "ToolCallEvent",
    "ToolResultEvent",
    "CompleteEvent",
]

"""
工具执行引擎

从 Agent 中抽取的核心能力，提供可复用的工具调用循环：
1. LLM 流式调用
2. 工具调用检测
3. 工具执行（并行）
4. 迭代循环控制
5. 内容注入机制
6. 钩子系统支持

被以下模块复用：
- Agent: 完整对话执行
"""

import sys
import os
import asyncio
import hashlib
import logging
import posixpath
import re
import time
import traceback

import httpx
from abc import ABC
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from peng.common.paths import get_working_directory

from peng.common.utils import generate_colored_diff
from peng.service.tool_backup import ToolBackupManager

# from peng.common import json as json
import json
from peng.common.colors import ANSI_COLORS, colorize, get_colors
from peng.config import get_agent_name, get_master_name, load_config
from peng.hooks.manager import HookManager
from peng.llm.client import GLMClient, LLMError, StreamStallError
from peng.task.info import is_urgent_content
from peng.task.output_manager import get_output_manager
from peng.tools.base import DualOutput, Tool, ToolError
from peng.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# 完成信号常量
_COMPLETION_SIGNALS = frozenset({"ANALYSIS_DONE", "TASK_DONE", "ACCEPTANCE_DONE", "DONE"})

# 退化循环检测安全验证行：用于检测pattern是否过于宽泛（会误报正常思考行）
# 例如未转义的字符类 [STOP LOOP] 会匹配任意S/T/O/P/空格/L字符，导致几乎每行都命中
_SAFE_THINKING_LINE = "This is a normal thinking line about the current task."

# 🔧 事务#1959 提取为模块级常量（原为HTTP 400处理分支内的局部变量，
# 单元测试只能维护副本、无法感知真实代码漂移）。分类命中后接入逐级清除
# 自愈策略（多模态附件→user消息→尾部轮次），避免换provider必然重复失败。
# 内容审核拦截类关键词（优先于解析类判定）
CONTENT_FILTER_KEYWORDS = [
    "不安全或敏感内容",
    "敏感内容",
    "内容审核",
    "内容可能包含不安全",
    "content_filter",
    "sensitive content",
    "安全审核",
    "违规内容",
]
# 内容解析失败类关键词 —— 上下文中的图片/附件损坏或provider不支持该内容
# 类型时，换provider必然同样失败（事务#1818：损坏BMP导致3个provider全部
# 400无限轮换2.5小时），正确做法：接入逐级清除策略（级1清附件）。
CONTENT_PARSE_ERROR_KEYWORDS = [
    "could not decode image",
    "loading image",
    "imagedata",
    "image data",
    "image_url",
    "invalid image",
    "image parse",
    "failed to load image",
    # 🔧 事务#2119：provider 读不出附件图片（本地 Strata 引擎对无法按声明解码的
    # 附件返回 400 "the image could not be read: cannot read the image <path>"；
    # 外部 API 对不支持格式亦有同类文案）。这是**请求内容问题**（附件字节与其
    # 声明格式不符/该 provider 读不了该图），换 provider 必然同样失败——此前
    # 不匹配任何关键词被当普通 4xx：重试2次→切换 provider→5轮全部失败→等待
    # 循环，事务卡死（事务#2081 实测）。必须归入内容解析类：不冷却不切换
    # provider，清附件并把 provider 报错**返回给大模型**（提示其重新读取/转码）。
    "could not be read",
    "cannot read the image",
    "cannot read image",
    "could not read the image",
    "could not read image",
    "failed to read the image",
    "failed to read image",
    "unable to read the image",
    "unable to read image",
    "图片无法读取",
    "无法读取图片",
    "无法读取该图片",
    "无法解析图片",
    "图片格式",
    "图片解析",
    "图片输入格式",
    "base64",
    "b64_",
    "content.type",
    # 🔧 事务#1959：GLM对空payload附件的报错"messages[0].content[0].file
    # 必须传入file_id、file_url、file_data至少之一"——0字节图片透传触发，
    # 此前不匹配任何关键词被当普通4xx无限重试（事务#1955），必须接入
    # 逐级清除自愈
    "file_id",
    "file_url",
    "file_data",
]
# 🔧 事务#2002：上下文长度超标类关键词 —— 请求token数超过provider模型
# 最大上下文长度时，换provider必然同样失败（上下文是provider无关的），
# 正确做法：compact压缩上下文后重试，而非切换provider。
# 此前不匹配任何关键词被当普通4xx处理（事务#1993：glm额度耗尽→切换
# s-qwen3.8→400上下文长度超标→按普通4xx重试2次→跳出→全provider不可用
# →等待循环），必须接入compact自愈策略。
CONTEXT_LENGTH_KEYWORDS = [
    "context length",
    "context_length",
    "maximum context",
    "exceeds the model",
    "exceeds the maximum",
    "token count exceeds",
    "requested token count",
    "reduce the number of!",
    "reduce the number of tokens",
    "上下文长度",
    "上下文超限",
    "超出上下文",
    "上下文长度超标",
    "token limit",
    "max tokens",
    "too many tokens",
]

# 🔧 事务#2084：从错误消息中提取服务端声称的上下文窗口大小（tokens）。
# 用于识别"伪上下文超标"：自研推理服务（如epyc的Strata server）在引擎
# 未就绪/重载中时返回 "prompt (X tokens) + max tokens (Y) exceeds the
# context (0); requests are never truncated"——声称的context为0，与
# /v1/models报告的n_ctx=262144矛盾。此类错误被"max tokens"关键词误匹配
# 为上下文超标，导致compact无效→restart死循环（事务#2080实测93次）。
_SERVER_CONTEXT_LIMIT_PATTERNS = [
    # Strata格式：exceeds the context (0) / exceeds the context (262144)
    re.compile(r"exceeds\s+the\s+context\s*\((\d+)\)", re.IGNORECASE),
    # 通用格式：context length is 8192 / context_size: 4096 / context = 32768
    re.compile(r"context[\s_-]*(?:length|size|limit)?[\s_-]*(?:is|=|:)\s*(\d+)", re.IGNORECASE),
    # vllm/ollama格式：maximum context length is 8192 / maximum context of 32768
    re.compile(r"maximum\s+context(?:\s*length)?(?:\s+is|\s+of)\s*(\d+)", re.IGNORECASE),
]
# 任何LLM的上下文窗口都≥2048；声称值低于此下限必然是服务端状态异常
# （引擎未就绪/配置损坏），绝非真实上下文容量。
SERVER_CONTEXT_LIMIT_SANITY_FLOOR = 1024


def extract_server_context_limit(err_msg: str) -> int | None:
    """从错误消息提取服务端声称的上下文窗口大小。

    Returns:
        int: 成功提取的context值（tokens）
        None: 消息中无context数值信息
    """
    if not err_msg:
        return None
    for pattern in _SERVER_CONTEXT_LIMIT_PATTERNS:
        m = pattern.search(err_msg)
        if m:
            try:
                return int(m.group(1))
            except (TypeError, ValueError):
                continue
    return None


# ==================== 事件类型 ====================


@dataclass
class EngineEvent:
    """引擎事件基类"""


@dataclass
class ContentEvent(EngineEvent):
    """内容事件"""

    content: str


@dataclass
class ThinkingEvent(EngineEvent):
    """思考事件"""

    content: str


@dataclass
class ToolCallEvent(EngineEvent):
    """工具调用事件"""

    call: Any  # ToolCall


@dataclass
class ToolResultEvent(EngineEvent):
    """工具结果事件"""

    call: Any  # ToolCall
    result: str  # summary（发送给 LLM）
    display: str | None = None  # display（显示给用户，如 diff）
    injections: list[str] = field(default_factory=list)  # 注入的内容列表
    rollback_meta: dict | None = None  # 回退元数据（用于一键回退）


@dataclass
class CompleteEvent(EngineEvent):
    """完成事件"""

    content: str
    iterations: int


# ==================== 消息历史管理 ====================


class MessageHistory:
    """
    消息历史管理

    管理 LLM 对话的消息历史，支持添加各种类型的消息并转换为 API 格式。

    内部存储格式统一使用 reasoning_content 字段存储 thinking 内容，
    不再使用 @思考记录： 前缀的 hack 方式。

    thinking 消息存储为: {"role": "assistant", "reasoning_content": "..."}
    assistant 消息可选带: {"role": "assistant", "content": "...", "reasoning_content": "...", "tool_calls": [...]}
    """

    # 内部标记字段：表示此消息是纯 thinking 消息（没有 content/tool_calls）
    _THINKING_ONLY = "_thinking_only"

    def __init__(self):
        self._messages: list[dict] = []
        self._system_message: dict | None = None
        self._prompt_tokens: int = 0  # API返回的真实prompt token数（最近一次LLM调用）
        self._total_tokens_consumed: int = 0  # 事务累计总token消耗（所有LLM调用的total_tokens之和）
        # 费用和分项token统计
        self._total_cost: float = 0.0  # 事务累计费用（元）
        self._total_input_hit_tokens: int = 0  # 累计命中缓存的输入tokens
        self._total_input_miss_tokens: int = 0  # 累计未命中缓存的输入tokens
        self._total_output_tokens: int = 0  # 累计输出tokens
        self._pricing: dict = {}  # provider定价配置 {price_input_hit, price_input_miss, price_output}
        self._provider_name: str = ""  # 当前provider名称（用于按provider记账）
        self._cost_breakdown: dict[
            str, dict
        ] = {}  # 按provider分别记录费用 {name: {cost, input_hit, input_miss, output, calls}}
        self._last_usage: dict = {}  # 最近一次API返回的usage（用于费用校验）
        self._user_merge_floor: int = 0  # 迭代边界：只允许合并此索引之后的user消息
        # 🔧 事务#2114：迭代边界索引记录（每次mark_iteration_start记录len(_messages)，
        # 保留最近8条）。供drop工具定位"上一轮迭代"（最近一批工具结果所在迭代），
        # 禁止跨多个迭代清理，只修改上下文尾部区域以保护前缀缓存
        self._iteration_marks: list[int] = []

    def size(self):
        return len(self._messages) + 1

    def add_system(self, content: str) -> None:
        """添加系统消息"""
        if not self._system_message:
            self._system_message = {"role": "system", "content": content}
        else:
            self._system_message["content"] += "\n" + content

    def get_system(self) -> str:
        if self._system_message and self._system_message["content"]:
            return self._system_message["content"]
        return ""

    def mark_iteration_start(self) -> None:
        """标记新迭代开始，防止跨迭代合并user消息破坏上下文前缀缓存

        同时记录迭代边界索引（事务#2114），供drop工具限定只能清理
        上一轮迭代的工具结果（禁止跨迭代，且只修改上下文尾部区域以保护前缀缓存）
        """
        self._user_merge_floor = len(self._messages)
        self._iteration_marks.append(len(self._messages))
        if len(self._iteration_marks) > 8:
            self._iteration_marks = self._iteration_marks[-8:]

    def add_user(self, content: Any, merge: bool = True) -> None:
        """添加用户消息

        Args:
            content: 用户消息内容（str 或 OpenAI 多模态 content part 列表；
                     多模态列表不参与合并，独立成消息）
            merge: 是否允许合并到上一条user消息（同一迭代内合并多条info，
                    跨迭代禁止合并以保护上下文前缀缓存）
        """
        if (
            merge
            and not isinstance(content, list)
            and self._messages
            and self._messages[-1]["role"] == "user"
            and isinstance(self._messages[-1].get("content"), str)
            and (len(self._messages) - 1) >= self._user_merge_floor
        ):
            # 同一迭代内的user消息合并
            self._messages[-1]["content"] += "\n" + content
        else:
            self._messages.append({"role": "user", "content": content})

    _THINKING_MAX_LINES = 300  # 非 DeepSeek 保留最后 N 行 thinking

    def _trim_thinking(self, thinking: str, max_lines: int = 300) -> str:
        """截断 thinking 内容，保留最后 max_lines 行，头部添加截断提示"""
        if not thinking:
            return thinking
        lines = thinking.split("\n")
        if len(lines) <= max_lines:
            return thinking
        # 保留最后 max_lines 行，头部添加截断提示
        truncated_count = len(lines) - max_lines
        template = load_config().llm.prompts.thinking_truncated
        result = (
            template.format(truncated_count=truncated_count) + "\n" + "\n".join(lines[-max_lines:])
        )
        return result

    def add_thinking(self, thinking: str, is_deepseek: bool = False) -> None:
        """添加思考内容到历史

        使用标准 reasoning_content 字段存储，兼容所有大模型 API。

        Args:
            thinking: thinking 文本内容
            is_deepseek: 是否为 DeepSeek（DeepSeek 保留完整 thinking，其他截断到最后300行）
        """
        if not thinking:
            return
        # 非 DeepSeek 截断到最后 300 行
        if not is_deepseek:
            thinking = self._trim_thinking(thinking, max_lines=self._THINKING_MAX_LINES)
        if not thinking:
            return
        # 存储为标准格式
        msg = {"role": "assistant", "reasoning_content": thinking, self._THINKING_ONLY: True}
        self._messages.append(msg)

    def add_assistant(
        self, content: str, tool_calls: list[Any] | None = None, _reasoning: str = ""
    ) -> None:
        """添加助手消息

        Args:
            content: 助手消息内容
            tool_calls: 工具调用列表
            _reasoning: reasoning_content 内容（与 tool_calls 合并到同一条消息）
        """
        msg = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = [
                tc.to_api_format() if hasattr(tc, "to_api_format") else tc for tc in tool_calls
            ]
        if _reasoning:
            msg["reasoning_content"] = _reasoning
        self._messages.append(msg)

    def add_tool_result(
        self,
        tool_call_id: str,
        tool_name: str,
        content: str | list,
        *,
        add_timestamp: bool = True,
    ) -> None:
        """添加工具结果消息

        Args:
            tool_call_id: 工具调用ID
            tool_name: 工具名称
            content: 结果内容（str 或 OpenAI 多模态 content part 列表，
                     如 read 工具返回的图片/PDF附件）
            add_timestamp: 是否在内容前添加时间戳（从历史恢复时应为False）
        """
        if add_timestamp:
            timestamp = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
            if isinstance(content, str):
                content = f"{timestamp} {content}"
            elif isinstance(content, list):
                # 多模态：时间戳写入首个text part（无text part则前置一个）
                text_part = next(
                    (p for p in content if isinstance(p, dict) and p.get("type") == "text"), None
                )
                if text_part is not None:
                    text_part["text"] = f"{timestamp} {text_part.get('text', '')}"
                else:
                    content = [{"type": "text", "text": timestamp}] + list(content)
        self._messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": tool_name,
                "content": content,
            }
        )

    def find_tool_result(self, tool_name: str, reverse=False) -> int:
        if reverse:
            i = len(self._messages) - 1
            while i > 0:
                if "name" in self._messages[i] and self._messages[i]["name"] == tool_name:
                    return i
                i -= 1
        else:
            i = 0
            while i < len(self._messages):
                if "name" in self._messages[i] and self._messages[i]["name"] == tool_name:
                    return i
                i += 1
        return -1

    # drop工具清理后的替换文案（事务#2114，单一事实源）
    _DROPPED_PLACEHOLDER = "垃圾信息，已被drop工具清理。"

    @staticmethod
    def _content_text(content) -> str:
        """提取消息content的纯文本（多模态列表取text part拼接）"""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(p.get("text", ""))
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        return str(content) if content else ""

    def get_drop_window(self) -> tuple[int, int] | None:
        """获取"上一轮迭代"（最近一批工具结果所在迭代）的消息索引窗口 [start, end)（事务#2114）

        end = 最后一次mark（当前迭代的消息——含正在执行的工具批次——全部在其后，
        天然被排除）；start = 最近一条 tool 消息所在迭代的起始边界。

        从模型视角，"上一轮迭代"= 它看到的最近一批工具结果：中间隔着的无工具
        空转迭代（空响应重试/纯文本轮/中断跳过等）不产生工具结果，不应把窗口
        顶到错过最近一批结果；而更早批次的工具结果（隔着真实工具迭代）不允许
        清理（"不得跨多个迭代drop"，同时保护上下文前缀缓存）。

        Returns:
            (start, end)：可清理的消息索引范围（已按当前消息数钳制）；
            None：无迭代边界或历史中尚无工具结果
        """
        if not self._iteration_marks:
            return None
        end = min(self._iteration_marks[-1], len(self._messages))
        if end <= 0:
            return None
        # 定位最近一条 tool 消息（最近一批工具结果的尾部）
        last_tool = -1
        for i in range(end - 1, -1, -1):
            if self._messages[i].get("role") == "tool":
                last_tool = i
                break
        if last_tool < 0:
            return None  # 历史中尚无工具结果
        # 窗口起点 = 该消息所在迭代的起始边界（≤last_tool 的最大mark，无则0）
        start = 0
        for m in self._iteration_marks:
            if m <= last_tool:
                start = m
            else:
                break
        return (start, end)

    def drop_tool_result(self, drop_id: str, replace: str) -> tuple[bool, str]:
        """按id清理上一轮迭代的工具返回结果（事务#2114）

        只允许清理上一轮迭代（最近一批工具结果所在迭代，见get_drop_window）：
        命中的 tool 消息 content 整体替换为占位文案（同时清除多模态附件part，
        节省视觉token），不改动 assistant 消息中的 tool_calls 与参数。
        中间隔着真实工具迭代的更早批次结果禁止清理（不得跨多个迭代drop）。

        Args:
            drop_id: 4位数字ID（来自工具结果末尾drop提示中的 "id xxxx"）

        Returns:
            (是否成功, 说明文本)
        """
        drop_id = str(drop_id).strip()
        if not re.fullmatch(r"\d{4}", drop_id):
            return False, f"错误：id必须为4位数字（收到 '{drop_id}'）。"
        marker = f"id {drop_id}"
        window = self.get_drop_window()
        if window is None:
            return False, "错误：当前不存在可清理的上一轮迭代工具结果。"
        start, end = window

        for i in range(start, end):
            msg = self._messages[i]
            if msg.get("role") != "tool":
                continue
            if marker in self._content_text(msg.get("content")):
                original_len = len(self._content_text(msg.get("content")))
                msg["content"] = replace
                logger.info(
                    f"[MessageHistory] drop工具已清理上一轮迭代工具结果 "
                    f"id={drop_id} (消息#{i}, 原{original_len}字符)"
                )
                return (True, f"已清理：id {drop_id} 对应的工具返回结果")
        # 窗口内未命中：区分"id不存在"与"存在但不在上一轮迭代"两种情况，给出精确报错
        for i, msg in enumerate(self._messages):
            if msg.get("role") == "tool" and marker in self._content_text(msg.get("content")):
                return (
                    False,
                    f"错误：id {drop_id} 对应的工具结果不属于上一轮迭代（消息#{i}），"
                    f"禁止跨迭代清理。只能清理上一轮迭代中带drop提示的工具结果。",
                )
        return (
            False,
            f"错误：未找到 id {drop_id} 对应的工具结果。请核对工具结果末尾提示中的4位数字ID。",
        )

    def from_api_format(self, messages):
        """从 API 格式加载消息，兼容旧格式（@思考记录：前缀）和标准格式（reasoning_content）"""
        self.clear()
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            if role == "system":
                self.add_system(content)
            elif role == "user":
                # content 可能为多模态列表（list[dict]），add_user 直接透传（不合并）
                self.add_user(content, merge=not isinstance(content, list))
            elif role == "assistant":
                tool_calls = msg.get("tool_calls")
                reasoning = msg.get("reasoning_content", "")
                if reasoning and not tool_calls and not content:
                    # 纯 thinking 消息（标准格式）
                    self._messages.append(
                        {
                            "role": "assistant",
                            "reasoning_content": reasoning,
                            self._THINKING_ONLY: True,
                        }
                    )
                elif reasoning and tool_calls:
                    # 带工具调用 + thinking 的合并消息
                    self._messages.append(
                        {
                            "role": "assistant",
                            "content": content,
                            "tool_calls": tool_calls,
                            "reasoning_content": reasoning,
                        }
                    )
                elif reasoning and content:
                    # 带 thinking 但无 tool_calls：拆分为 thinking + assistant
                    self._messages.append(
                        {
                            "role": "assistant",
                            "reasoning_content": reasoning,
                            self._THINKING_ONLY: True,
                        }
                    )
                    self._messages.append({"role": "assistant", "content": content})
                else:
                    # 无 reasoning 的普通 assistant 消息（最常见情况）
                    # 必须保留 tool_calls，否则后续 tool 消息找不到前驱
                    msg_dict: dict = {"role": "assistant", "content": content}
                    if tool_calls:
                        msg_dict["tool_calls"] = tool_calls
                    self._messages.append(msg_dict)
            elif role == "tool":
                self.add_tool_result(
                    msg.get("tool_call_id", ""), msg.get("name", ""), content, add_timestamp=False
                )

    def to_api_format(
        self,
        thinking_type: str | None = None,
        vision: bool = False,
        preserve_thinking: bool = False,
    ) -> list[dict]:
        """转换为 API 格式

        Args:
            thinking_type: 当前 provider 的 thinking 处理类型
                - "thinking" (DeepSeek/OpenAI): reasoning_content 字段
                - None/其他 (如 "glm"): 不输出 reasoning_content（直接忽略 thinking 消息）
                  实测 GLM5.2 等不会复用 history 中的 thinking 内容，总是重新思考，
                  所以转换为 content 只是浪费上下文 token，不如直接跳过
            vision: 当前 provider 是否支持多模态（vision）
                - True: 保留 user 消息中的多模态 content part 列表
                - False: 将多模态列表降级为纯文本（提取 text part，保留提示文字）
            preserve_thinking: 是否保留思考内容（事务#2111，qwen 系列可选）
                - True: 非 "thinking" 风格（如 qwen/chat_template_kwargs）也保留
                  reasoning_content。实机验证 swift/llama.cpp 后端会把 assistant
                  消息中的 reasoning_content 渲染回 prompt（模型可复用上轮思考），
                  代价是消耗额外输入 token，故默认 False 剥离以节省 token

        输出规则：
        1. 纯 thinking 消息（_thinking_only=True）：
            - 保留模式（thinking_type="thinking" 或 preserve_thinking=True）时，
              如果下一条是 assistant 消息则合并（后端对独立空 content 的 thinking
              消息读不到，合并形式实测有效）；否则独立输出
            - 非保留模式时：跳过（GLM 等不复用 thinking）
        2. 带 reasoning_content 的 assistant + tool_calls 消息：保留模式直接输出，
           非保留模式去掉 reasoning_content
        3. 普通 assistant 消息：直接输出（"thinking" 风格补空串满足 DeepSeek 要求）
        4. user 消息 content 为多模态列表时：按 vision 参数保留或降级
        """
        result = []
        if self._system_message:
            result.append(self._system_message)

        # 是否在序列化中保留思考内容（事务#2111：qwen 系列 preserve_thinking 可选，
        # 默认 False；DeepSeek 风格 "thinking" 因 API 强制要求始终保留）
        keep_reasoning = thinking_type == "thinking" or bool(preserve_thinking)

        # 跟踪最近一次带 tool_calls 的 assistant 在 result 中的索引
        pending_tc_idx = -1
        last_tc_ids = set()

        def _cleanup_unanswered_tool_calls(asst_msg: dict, responded_ids: set):
            """清理 assistant 消息中没有对应 tool 响应的 tool_calls"""
            tool_calls = asst_msg.get("tool_calls", [])
            if not tool_calls:
                return
            # 只保留有响应的 tool_calls
            answered = [tc for tc in tool_calls if tc.get("id") in responded_ids]
            if len(answered) < len(tool_calls):
                logger.debug(
                    f"[MessageHistory] 清理 {len(tool_calls) - len(answered)}/{len(tool_calls)} "
                    f"未响应的 tool_calls"
                )
                if answered:
                    asst_msg["tool_calls"] = answered
                else:
                    # 全部无响应：移除 tool_calls，变为普通 assistant
                    asst_msg.pop("tool_calls", None)

        def _flush_pending_cleanup():
            """在添加新的非 tool 消息之前，清理待处理 assistant 的未响应 tool_calls"""
            nonlocal pending_tc_idx, last_tc_ids
            if pending_tc_idx >= 0 and pending_tc_idx < len(result):
                asst = result[pending_tc_idx]
                if asst.get("role") == "assistant" and "tool_calls" in asst:
                    _cleanup_unanswered_tool_calls(asst, last_tc_ids)
            pending_tc_idx = -1
            last_tc_ids = set()

        i = 0
        while i < len(self._messages):
            msg = self._messages[i]

            # 构建输出消息（过滤内部字段）
            def _clean_output(m: dict) -> dict:
                """去除内部字段，生成干净的 API 输出"""
                out = {}
                for k, v in m.items():
                    if not k.startswith("_"):
                        out[k] = v
                return out

            is_thinking_only = msg.get("role") == "assistant" and msg.get(self._THINKING_ONLY)

            if is_thinking_only:
                if not keep_reasoning:
                    # 非保留模式：跳过纯 thinking 消息
                    # 实测 GLM5.2 等不会复用转换的 thinking 内容，总是重新思考
                    i += 1
                    continue
                # 保留模式（"thinking" 或 preserve_thinking）：检查是否可以合并到
                # 下一条 assistant 消息（事务#2111实机验证：qwen 后端对独立空
                # content 的 thinking 消息读不到，合并进相邻 assistant 才有效）
                next_msg = self._messages[i + 1] if i + 1 < len(self._messages) else None
                if (
                    next_msg
                    and next_msg.get("role") == "assistant"
                    and not next_msg.get(self._THINKING_ONLY)
                    and "reasoning_content" not in next_msg
                ):
                    # 合并到下一条 assistant 消息（带 tool_calls 或普通消息均合并，
                    # 避免产生两条连续 assistant 消息——DeepSeek 等后端不支持）
                    # 先清理前一条 assistant 的未响应 tool_calls
                    _flush_pending_cleanup()
                    merged = _clean_output(next_msg)
                    merged["reasoning_content"] = msg.get("reasoning_content", "")
                    result.append(merged)
                    if "tool_calls" in next_msg:
                        pending_tc_idx = len(result) - 1
                    i += 2
                    continue
                else:
                    # 独立输出（后面没有可合并的 assistant 消息）
                    _flush_pending_cleanup()
                    out = _clean_output(msg)
                    # 纯 thinking 消息需要 content 字段（DeepSeek 要求）
                    if "content" not in out:
                        out["content"] = ""
                    result.append(out)
                    i += 1
                    continue

            # 带 reasoning_content 的 assistant 消息
            if msg.get("role") == "assistant" and "reasoning_content" in msg:
                _flush_pending_cleanup()
                if keep_reasoning:
                    out = _clean_output(msg)
                    result.append(out)
                else:
                    # 非保留模式：去掉 reasoning_content
                    out = _clean_output(msg)
                    out.pop("reasoning_content", None)
                    result.append(out)
                if "tool_calls" in msg:
                    pending_tc_idx = len(result) - 1
                i += 1
                continue

            # tool 消息：必须前一条是带 tool_calls 的 assistant
            if msg.get("role") == "tool":
                if pending_tc_idx >= 0 and pending_tc_idx < len(result):
                    out = _clean_output(msg)
                    # 多模态降级：tool 结果 content 为多模态列表时，
                    # 非 vision provider 降级为纯文本（提取 text part）
                    if not vision and isinstance(out.get("content"), list):
                        from peng.llm.multimodal import strip_multimodal_content

                        out["content"] = strip_multimodal_content(out["content"])
                    result.append(out)
                    # 将 tool_call_id 标记为已响应
                    last_tc_ids.add(msg.get("tool_call_id"))
                else:
                    # 孤立 tool 消息：跳过（历史损坏/compact导致）
                    logger.debug(
                        f"[MessageHistory] 跳过孤立 tool 消息: name={msg.get('name')}, "
                        f"tool_call_id={msg.get('tool_call_id')}"
                    )
                i += 1
                continue

            # 普通 assistant/user/system 消息
            _flush_pending_cleanup()
            out = _clean_output(msg)

            # 多模态降级：user 消息 content 为多模态列表时，
            # 非 vision provider 降级为纯文本（提取 text part）
            if not vision and isinstance(out.get("content"), list):
                from peng.llm.multimodal import strip_multimodal_content

                out["content"] = strip_multimodal_content(out["content"])

            # 🔧 事务#2002：DeepSeek thinking mode 要求 —— 带 tools 参数的请求中，
            # **所有** assistant 消息（含无 tool_calls 的普通消息）都必须携带
            # reasoning_content（空串 "" 可接受，缺失则 400）。官方文档（Tool Calls）：
            # "for requests carrying the tools parameter, the reasoning_content must
            # be fully passed back to the API in all subsequent requests — even for
            # turns where the model did not perform a tool call. If your code does not
            # correctly pass back reasoning_content, the API will return a 400 error."
            # 实测（deepseek-flash）：tool_calls 消息与普通 assistant 消息只要有一个
            # 缺失 reasoning_content 即报 400 "The `reasoning_content` in the thinking
            # mode must be passed back to the API."；补空串 "" 后 200。
            # 事务#1997 事故：report 钩子注入的普通 assistant 消息（"🪝 我需要确认…"）
            # 无 reasoning_content → 下一轮 400 → 换 provider 无法自愈 → 全 provider
            # 失败死循环（20:08~20:19 连续 7 轮）。
            if (
                thinking_type == "thinking"
                and msg.get("role") == "assistant"
                and "reasoning_content" not in msg
                and "reasoning_content" not in out
            ):
                # 尝试回溯合并：检查前一条是否是独立输出的 thinking 消息
                if (
                    result
                    and result[-1].get("role") == "assistant"
                    and result[-1].get("reasoning_content")
                    and not result[-1].get("content")
                    and "tool_calls" not in result[-1]
                ):
                    # 回溯合并：将 reasoning_content 合并到当前 assistant 消息
                    out["reasoning_content"] = result[-1].pop("reasoning_content", "")
                    result.pop()  # 移除独立的 thinking 消息
                else:
                    # 无前置 thinking 可合并（模型直接输出 tool_calls/普通消息无 thinking，
                    # 或钩子注入的 assistant 消息）：补空串满足 DeepSeek API 要求
                    out["reasoning_content"] = ""

            result.append(out)
            if msg.get("role") == "assistant" and "tool_calls" in msg:
                pending_tc_idx = len(result) - 1
            i += 1

        # 尾部清理：最后一条如果是带 tool_calls 的 assistant
        _flush_pending_cleanup()

        return result

    def to_context(
        self,
        thinking_type: str | None = None,
        vision: bool = True,
        preserve_thinking: bool = False,
    ) -> list | dict:
        """转换为可持久化的context格式（包含元数据）

        Args:
            thinking_type: 当前 provider 的 thinking 处理类型
            vision: 持久化默认保留多模态内容（True），上下文恢复后再按
                    当前 provider 决定是否降级
            preserve_thinking: 是否保留思考内容（事务#2111）。持久化与请求
                    序列化使用同一开关：开启时 DB context 中保留 reasoning_content
                    （重启/恢复后模型仍可复用），关闭时剥离节省存储与token

        Returns:
            新格式 dict: {"messages": [...], "_prompt_tokens": N, "_total_tokens_consumed": N}
        """
        return {
            "messages": self.to_api_format(
                thinking_type=thinking_type, vision=vision, preserve_thinking=preserve_thinking
            ),
            "_prompt_tokens": self._prompt_tokens,
            "_total_tokens_consumed": self._total_tokens_consumed,
            "_total_cost": self._total_cost,
            "_total_input_hit_tokens": self._total_input_hit_tokens,
            "_total_input_miss_tokens": self._total_input_miss_tokens,
            "_total_output_tokens": self._total_output_tokens,
            "_pricing": self._pricing,
            "_cost_breakdown": self._cost_breakdown,
        }

    def from_str(self, text: str):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                # 新格式：{"messages": [...], "_prompt_tokens": N, "_total_tokens_consumed": N}
                messages = data.get("messages", [])
                self._prompt_tokens = data.get("_prompt_tokens", 0)
                self._total_tokens_consumed = data.get("_total_tokens_consumed", 0)
                self._total_cost = data.get("_total_cost", 0.0)
                self._total_input_hit_tokens = data.get("_total_input_hit_tokens", 0)
                self._total_input_miss_tokens = data.get("_total_input_miss_tokens", 0)
                self._total_output_tokens = data.get("_total_output_tokens", 0)
                # 恢复pricing（作为兜底，正常流程会在每次LLM调用前重新设置）
                self._pricing = data.get("_pricing", {}) or {}
                # 恢复按provider费用明细
                self._cost_breakdown = data.get("_cost_breakdown", {}) or {}
            else:
                # 旧格式：[...] 纯消息数组
                messages = data
                self._prompt_tokens = 0
            self.from_api_format(messages)
            self.remove_empty_tail()
            return True
        except Exception as e:
            logger.debug(f"❌ ♻️ [MessageHistory] 从字符串转换历史上下文失败。{e}")
            return False

    def remove_empty_tail(self):
        """移除末尾的空 assistant 消息（不含 reasoning_content 的）"""
        while self._messages:
            last = self._messages[-1]
            if (
                last.get("role") == "assistant"
                and not last.get("content")
                and not last.get("reasoning_content")
                and not last.get("tool_calls")
            ):
                self._messages.pop()
            else:
                break

    # 多模态附件 part 类型集合（OpenAI/qwen 兼容格式的多模态 content part）
    _ATTACHMENT_PART_TYPES = frozenset(
        {
            "image_url",
            "video_url",
            "input_audio",
            "input_image",
            "input_video",
            "input_file",
            "file",
            "audio",
        }
    )

    # 附件清除后的占位说明（告知模型附件只发送一次，需重看时用read工具）
    _ATTACHMENT_REMOVED_NOTICE = (
        "[系统提示：本消息附带的多模态附件（图片/视频/音频/PDF）已随上一轮请求发送给你，"
        "现已从上下文清除（多模态数据只允许发送一次以节省token）。"
        "请基于此前已查看的附件内容继续工作；如需再次查看，请重新调用read工具读取原文件。]"
    )

    # _inject_multimodal_files 注入时附加的系统提示前缀（清除附件时同步替换，避免误导）
    _INJECT_NOTICE_PREFIX = "[系统提示：上方/下方附加的多模态文件"

    def has_pending_attachments(self) -> bool:
        """history中是否存在尚未发送的多模态附件part（图片/视频/音频/PDF等）

        用于 provider 选择与请求构建：存在待发送附件时应选择支持vision的
        provider，并在 to_api_format 中保留附件part（vision=True），否则
        vision provider也看不到图片。
        """
        from peng.llm.multimodal import has_attachment_part

        return any(has_attachment_part(m.get("content")) for m in self._messages)

    # 🔧 事务#2119：附件被 provider **拒绝读取**（400 图片读不出/解析失败）时的
    # 占位说明。与上方"已发送"说明语义完全不同：附件从未成功送达，模型从未
    # 看到内容，绝不能沿用"已随上一轮请求发送给你、基于已查看内容继续"的提示
    # （会诱导模型臆测图片内容）。此处把 provider 的报错**返回给大模型**，
    # 并给出可执行的补救方向（重新读取/转码），实现错误反馈闭环。
    _ATTACHMENT_REJECTED_NOTICE_TEMPLATE = (
        "[系统提示：本消息附带的多模态附件（图片/视频/音频/PDF）无法被大模型"
        "provider 读取，已被 provider 拒绝并从此请求中移除（provider 报错：{error}）。"
        "你尚未看到该附件内容，禁止臆测其内容。如需该信息，请重新调用 read 工具"
        "读取该文件（必要时先转换为 PNG/JPEG 等通用格式再读取），或改用文字方式获取。]"
    )

    @classmethod
    def attachment_rejected_notice(cls, error: str = "") -> str:
        """构建"附件被 provider 拒绝读取"的说明（把报错返回给大模型）"""
        return cls._ATTACHMENT_REJECTED_NOTICE_TEMPLATE.format(error=error or "未知错误")

    def strip_sent_attachments(self, notice: str | None = None) -> int:
        """清除history中已发送的多模态附件part（只发送一次机制）

        多模态附件（base64图片/视频/音频/PDF）体积大且多数provider的视觉token
        不享受缓存折扣，留在history中会导致后续每轮LLM调用重复发送。
        在LLM调用成功完成后调用本方法，将已随本次请求发送的附件part移除：

        1. text part保留，追加附件清除说明（同步替换原"已直接注入"提示，
           避免清除附件后误导模型以为仍能看到附件）
        2. 只剩单个text part时content简化为str（与普通消息格式一致）
        3. 纯附件消息content替换为清除说明（防御性，正常总有text part）

        Args:
            notice: 附件清除说明文案（None=默认"已随上一轮请求发送给你"说明）。
                🔧 事务#2119：附件被 provider **拒绝读取**（400 图片读不出）时
                必须传 attachment_rejected_notice(...) —— 把 provider 报错
                返回给大模型，而不是谎称附件已送达。

        Returns:
            清除的附件part总数（0表示无附件被清除）
        """
        _notice = notice or self._ATTACHMENT_REMOVED_NOTICE
        removed = 0
        for msg in self._messages:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            attachment_parts = [
                p
                for p in content
                if isinstance(p, dict) and p.get("type") in self._ATTACHMENT_PART_TYPES
            ]
            if not attachment_parts:
                continue
            removed += len(attachment_parts)
            texts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            text = "\n".join(t for t in texts if t)
            # 替换注入时的"已直接注入"提示：附件已清除，原提示会误导模型幻觉图片内容
            if self._INJECT_NOTICE_PREFIX in text:
                text = text.split(self._INJECT_NOTICE_PREFIX, 1)[0].rstrip()
            text = (text + "\n" if text else "") + _notice
            msg["content"] = text
        return removed

    # 多模态附件 part 的估算 token 数（qwen-vl 图像约数百token，
    # base64字符长度/3.5 会严重高估，按每附件固定值估算）
    _MULTIMODAL_PART_ESTIMATED_TOKENS = 1000

    def estimate_tokens(
        self,
        begin=0,
        end=-1,
        thinking_type: str | None = None,
        preserve_thinking: bool = False,
    ) -> int:
        """估算 token 数量（基于实际 API payload 大小）

        使用消息的 JSON 序列化大小来估算，确保与 API 发送内容一致。
        估算规则：JSON 字符长度 / 3.5（混合中英文的平均 token 比率）
        多模态 content part（image_url/video_url/input_audio/file）按固定
        附件 token 数估算（provider 按视觉token计费，非文本字符数）。

        🔧 事务#2114：与 to_api_format 序列化语义对齐——preserve_thinking=false
        且 thinking_type 非 "thinking" 时，实际请求会剥离 assistant 消息的
        reasoning_content 并跳过纯thinking消息，估算同样不计入，否则会系统性
        高估上下文长度，导致"上下文紧张"误报（催促compact/restart提前触发）。
        同时剔除 `_` 前缀内部字段（实际请求不发送）。

        Args:
            thinking_type: 当前provider的thinking处理类型（None/其他=剥离思考）
            preserve_thinking: 是否保留思考内容（与to_api_format同义）
        """
        keep_reasoning = thinking_type == "thinking" or bool(preserve_thinking)
        all_msgs = ([self._system_message] if self._system_message else []) + self._messages
        if end == -1:
            sliced = all_msgs[begin:]
        else:
            sliced = all_msgs[begin:end]

        total_chars = 0
        part_tokens = 0
        for msg in sliced:
            if not msg:
                continue
            # 对齐to_api_format：非保留模式跳过纯thinking消息（实际请求不发送）
            if (
                not keep_reasoning
                and msg.get("role") == "assistant"
                and msg.get(self._THINKING_ONLY)
            ):
                continue
            # 只估算实际发送内容：剔除内部字段与非保留模式的reasoning_content
            est_msg = {k: v for k, v in msg.items() if not k.startswith("_")}
            if not keep_reasoning and "reasoning_content" in est_msg:
                est_msg.pop("reasoning_content", None)
            content = est_msg.get("content") if isinstance(est_msg, dict) else None
            if isinstance(content, list):
                # 多模态消息：text part 按字符估算，附件 part 按固定 token 估算
                text_chars = 0
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_chars += len(part.get("text", ""))
                    else:
                        part_tokens += self._MULTIMODAL_PART_ESTIMATED_TOKENS
                total_chars += text_chars + 100  # 100: 消息结构开销
                continue
            try:
                msg_str = json.dumps(est_msg, ensure_ascii=False)
            except (TypeError, ValueError):
                msg_str = str(est_msg)
            total_chars += len(msg_str)

        # 混合中英文：平均约 3.5 字符/token；附件按固定 token 数累加
        return total_chars * 2 // 7 + part_tokens

    def set_pricing(self, pricing: dict | None, provider_name: str = "") -> None:
        """设置provider定价配置，用于费用计算

        Args:
            pricing: {"price_input_hit": float, "price_input_miss": float, "price_output": float}
                     单位：元/百万tokens
            provider_name: 当前provider名称（用于按provider记账）
        """
        self._pricing = pricing or {}
        if provider_name:
            self._provider_name = provider_name

    def update_usage(self, usage: dict | None) -> None:
        """更新API返回的真实token用量，并累加事务总token消耗和费用

        支持多种API格式的缓存命中字段：
        - DeepSeek: prompt_cache_hit_tokens, prompt_cache_miss_tokens
        - OpenAI: prompt_tokens_details.cached_tokens
        - 通用: cached_tokens, cache_hit_tokens

        Args:
            usage: API返回的usage字典，包含 prompt_tokens, completion_tokens, total_tokens
        """
        if usage and isinstance(usage, dict):
            prompt_tokens = usage.get("prompt_tokens")
            if prompt_tokens is not None:
                self._prompt_tokens = int(prompt_tokens)
            # 累加总token消耗（以API返回的total_tokens为准，不做估算）
            total_tokens = usage.get("total_tokens")
            if total_tokens is not None:
                self._total_tokens_consumed += int(total_tokens)

            # 提取输出tokens
            completion_tokens = usage.get("completion_tokens", 0) or 0
            self._total_output_tokens += int(completion_tokens)

            # 提取缓存命中/未命中输入tokens
            cached_tokens = 0
            # DeepSeek格式
            cache_hit = usage.get("prompt_cache_hit_tokens")
            cache_miss = usage.get("prompt_cache_miss_tokens")
            if cache_hit is not None:
                cached_tokens = int(cache_hit)
                uncached_tokens = (
                    int(cache_miss)
                    if cache_miss is not None
                    else (int(prompt_tokens or 0) - cached_tokens)
                )
            else:
                # OpenAI/GLM格式: prompt_tokens_details.cached_tokens
                ptd = usage.get("prompt_tokens_details")
                if isinstance(ptd, dict):
                    cached_tokens = int(ptd.get("cached_tokens", 0) or 0)
                # 通用格式
                if cached_tokens == 0:
                    cached_tokens = int(usage.get("cached_tokens", 0) or 0)
                if cached_tokens == 0:
                    cached_tokens = int(usage.get("cache_hit_tokens", 0) or 0)
                uncached_tokens = int(prompt_tokens or 0) - cached_tokens

            self._total_input_hit_tokens += cached_tokens
            self._total_input_miss_tokens += uncached_tokens

            # 计算费用（单位：元/百万tokens → 元/token = 价格/1,000,000）
            price_hit = self._pricing.get("price_input_hit", 0) or 0
            price_miss = self._pricing.get("price_input_miss", 0) or 0
            price_out = self._pricing.get("price_output", 0) or 0
            cost = (
                cached_tokens * price_hit / 1_000_000
                + uncached_tokens * price_miss / 1_000_000
                + int(completion_tokens) * price_out / 1_000_000
            )
            self._total_cost += cost

            # 按provider分别记账
            p_name = self._provider_name or "unknown"
            if p_name not in self._cost_breakdown:
                self._cost_breakdown[p_name] = {
                    "cost": 0.0,
                    "input_hit": 0,
                    "input_miss": 0,
                    "output": 0,
                    "calls": 0,
                }
            bd = self._cost_breakdown[p_name]
            bd["cost"] += cost
            bd["input_hit"] += cached_tokens
            bd["input_miss"] += uncached_tokens
            bd["output"] += int(completion_tokens)
            bd["calls"] += 1

            # 记录最近一次usage（用于费用校验）
            self._last_usage = {
                "prompt_tokens": int(prompt_tokens or 0),
                "completion_tokens": int(completion_tokens),
                "total_tokens": int(total_tokens or 0),
                "cached_tokens": cached_tokens,
                "cost": cost,
            }

            # 缓存命中率日志（优化1：GLM缓存命中监控）
            pt_val = int(prompt_tokens or 0)
            if pt_val > 0:
                cache_rate = cached_tokens / pt_val * 100
                logger.info(
                    f"[MessageHistory] {p_name}: "
                    f"prompt={pt_val}(缓存命中{cached_tokens}/{cache_rate:.0f}%), "
                    f"output={completion_tokens}, 本次费用={cost:.6f}元, "
                    f"累计={self._total_cost:.4f}元"
                )
            else:
                logger.debug(
                    f"[MessageHistory] 更新token用量: output={completion_tokens}, "
                    f"累计消耗={self._total_tokens_consumed}, 累计费用={self._total_cost:.4f}元"
                )

    def get_token_count(
        self,
        begin=0,
        end=-1,
        estimate=True,
        thinking_type: str | None = None,
        preserve_thinking: bool = False,
    ) -> int:
        """获取当前上下文的token数量

        优先使用API返回的真实prompt_tokens（反映完整上下文长度），
        如果没有真实数据则回退到estimate_tokens估算。

        注意：_prompt_tokens 是最近一次LLM调用时API报告的prompt token数，
        它包含了发送给API的完整消息列表（system + 所有messages）。
        由于工具执行后messages可能增长，实际token数会大于等于此值。
        为了保守起见，直接使用此值即可（compact/阈值检查都是在LLM调用前进行的，
        此时_prompt_tokens反映的就是当前上下文的真实大小）。

        Args:
            begin: 起始消息索引（仅用于fallback估算）
            end: 结束消息索引（仅用于fallback估算）
            thinking_type: 当前provider的thinking处理类型（fallback估算用，
                默认None=按剥离思考估算，与主流provider实际请求一致）
            preserve_thinking: 是否保留思考内容（fallback估算用，默认False）
        """
        if self._prompt_tokens > 0 or not estimate:
            return self._prompt_tokens
        return self.estimate_tokens(
            begin, end, thinking_type=thinking_type, preserve_thinking=preserve_thinking
        )

    def get_total_tokens_consumed(self) -> int:
        """获取事务累计总token消耗（所有LLM调用的total_tokens之和）"""
        return self._total_tokens_consumed

    def get_total_cost(self) -> float:
        """获取事务累计费用（元）"""
        return self._total_cost

    def get_input_hit_tokens(self) -> int:
        """获取累计命中缓存的输入tokens"""
        return self._total_input_hit_tokens

    def get_input_miss_tokens(self) -> int:
        """获取累计未命中缓存的输入tokens"""
        return self._total_input_miss_tokens

    def get_output_tokens(self) -> int:
        """获取累计输出tokens"""
        return self._total_output_tokens

    def get_cost_breakdown(self) -> dict:
        """获取按provider分别记录的费用明细

        Returns:
            {provider_name: {cost, input_hit, input_miss, output, calls}}
        """
        return self._cost_breakdown

    def get_last_usage(self) -> dict:
        """获取最近一次API返回的usage信息（用于费用校验）

        Returns:
            {prompt_tokens, completion_tokens, total_tokens, cached_tokens, cost}
        """
        return self._last_usage

    def clear(self) -> None:
        """清空历史"""
        self._messages.clear()
        self._system_message = None
        # 迭代边界随之失效（事务#2114）
        self._iteration_marks = []

    def reset_messages_keep_stats(self) -> dict:
        """清空对话消息但保留累计统计（费用、token用量），返回新的context字典。

        用于事务重启（keep_context=False）场景：对话历史需要清除重新开始，
        但累计费用和token消耗应该跨重启持续累积。

        Returns:
            包含空消息但保留统计数据的context字典（会被update_tasks自动序列化为JSON）
        """
        self._messages.clear()
        self._system_message = None
        self._prompt_tokens = 0  # 重置当前上下文token数（统计是累计的）
        # 迭代边界随之失效（事务#2114）
        self._iteration_marks = []
        return self.to_context()

    # compact 时截断长参数的工具名称
    _COMPACT_TRUNCATE_TOOLS: frozenset[str] = frozenset(
        {"edit", "write", "checklist", "report", "message"}
    )
    # content 最大保留字节数
    _COMPACT_CONTENT_MAX = 2048
    # tool_call 单个参数最大保留字节数
    _COMPACT_ARG_MAX = 1024

    def _truncate_str(self, s: str, max_len: int = 2048) -> str:
        """将字符串截断到指定长度，保留前后部分"""
        if not isinstance(s, str) or len(s) <= max_len:
            return s
        half = (max_len - 5) // 2
        return s[:half] + "\n...\n" + s[-(max_len - half - 5) :]

    def _truncate_tool_call_args(
        self, tool_calls: list[dict], max_arg_len: int = 1024
    ) -> list[dict]:
        """截断 tool_call 中的长参数"""
        result = []
        for tc in tool_calls:
            tc = dict(tc)
            func = tc.get("function", {})
            if func and "arguments" in func:
                args_str = func["arguments"]
                if isinstance(args_str, str) and len(args_str) > max_arg_len:
                    try:
                        args = json.loads(args_str)
                    except (json.JSONDecodeError, TypeError):
                        args = None
                    if isinstance(args, dict):
                        truncated = False
                        for key, val in args.items():
                            if isinstance(val, str) and len(val) > max_arg_len:
                                args[key] = self._truncate_str(val, max_arg_len)
                                truncated = True
                        if truncated:
                            func = dict(func)
                            func["arguments"] = json.dumps(args, ensure_ascii=False)
                            tc["function"] = func
            result.append(tc)
        return result

    def compact(self, compact_threshold, compact_ratio) -> tuple[int, int]:
        """压缩历史上下文：不删除消息元素，只截短 content 和 tool_call 参数

        策略：
        1. 保护头部消息（原始需求上下文）
        2. 保护尾部消息（最新对话上下文）
        3. 中间消息：截短大 content 和长参数工具调用的参数
        4. 截短顺序：按消息大小降序处理
        """
        original = compacted = self.get_token_count()
        if original <= compact_threshold:
            return original, compacted

        total = len(self._messages)
        if total < 6:
            return original, compacted

        target = int(compact_threshold * compact_ratio)

        # 保护头尾
        protected_head = 5
        protected_tail = 6

        # 收集中间消息及其大小
        mid_indices = list(range(protected_head, total - protected_tail))
        if not mid_indices:
            return original, compacted

        # 按消息大小降序排列
        msg_sizes = []
        for i in mid_indices:
            msg = self._messages[i]
            if not msg:
                continue
            content = msg.get("content", "")
            if isinstance(content, str) and "@no-compact!" in content:
                continue
            try:
                size = len(json.dumps(msg, ensure_ascii=False))
            except (TypeError, ValueError):
                size = len(str(msg))
            msg_sizes.append((size, i))

        msg_sizes.sort(key=lambda x: -x[0])

        # 从大到小截短
        saved_chars = 0
        for size, i in msg_sizes:
            if compacted - saved_chars <= target:
                break

            msg = self._messages[i]
            old_size = len(json.dumps(msg, ensure_ascii=False))

            # 多模态消息：截短 text part（保留附件 part 不动，
            # 附件由 LLM 按需引用；旧附件若需要可通过 @file= 重新注入）
            if isinstance(msg.get("content"), list):
                new_parts = []
                for part in msg["content"]:
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "text"
                        and isinstance(part.get("text"), str)
                        and len(part["text"]) > self._COMPACT_CONTENT_MAX
                    ):
                        part = dict(part)
                        part["text"] = self._truncate_str(part["text"], self._COMPACT_CONTENT_MAX)
                    new_parts.append(part)
                msg["content"] = new_parts
                new_size = len(json.dumps(msg, ensure_ascii=False))
                saved_chars += old_size - new_size
                continue

            # 截短 content
            content = msg.get("content")
            if isinstance(content, str) and len(content) > self._COMPACT_CONTENT_MAX:
                msg["content"] = self._truncate_str(content, self._COMPACT_CONTENT_MAX)

            # 截短 tool_call 参数（仅限长参数工具）
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                needs_truncate = False
                for tc in tool_calls:
                    tc_name = tc.get("function", {}).get("name", "")
                    if tc_name in self._COMPACT_TRUNCATE_TOOLS:
                        needs_truncate = True
                        break
                if needs_truncate:
                    msg["tool_calls"] = self._truncate_tool_call_args(
                        tool_calls, self._COMPACT_ARG_MAX
                    )

            # 截短 reasoning_content
            reasoning = msg.get("reasoning_content")
            if isinstance(reasoning, str) and len(reasoning) > self._COMPACT_CONTENT_MAX:
                msg["reasoning_content"] = self._truncate_str(reasoning, self._COMPACT_CONTENT_MAX)

            # 截短 tool result content
            if msg.get("role") == "tool":
                tool_content = msg.get("content")
                if isinstance(tool_content, str) and len(tool_content) > self._COMPACT_CONTENT_MAX:
                    msg["content"] = self._truncate_str(tool_content, self._COMPACT_CONTENT_MAX)

            new_size = len(json.dumps(msg, ensure_ascii=False))
            saved_chars += old_size - new_size

        # 清理 @compact! 标记：不删除消息，只清空 content
        for m in self._messages:
            content = m.get("content")
            if isinstance(content, str) and "@compact!" in content:
                m["content"] = ""

        compacted = self.get_token_count()
        return original, compacted


# ==================== 工具执行引擎 ====================
class ToolExecutionEngine:
    """
    工具执行引擎

    从 Agent 中抽取的核心能力，提供可复用的工具调用循环。
    支持流式输出、并行工具执行、内容注入和钩子系统。
    """

    def __init__(
        self,
        llm_client: GLMClient,
        tool_registry: ToolRegistry,
        max_iterations: int | None = None,
        message_manager=None,
    ):
        """
        初始化引擎

        Args:
            llm_client: LLM 客户端
            tool_registry: 工具注册表
            max_iterations: 最大迭代次数（None 则从配置读取）
            message_manager: 消息管理器（可选，用于钩子系统）
        """
        from peng.message.manager import MessageManager

        self.message_manager = message_manager if message_manager else MessageManager()
        self.task_manager = self.message_manager.get_task_manager()

        self.llm = llm_client
        self.tools = tool_registry
        # 如果没有传入 max_iterations，尝试从 llm_client.config 获取
        if max_iterations is None:
            max_iterations = getattr(llm_client.config, "max_iterations", 65536)
        self.max_iterations = max_iterations

        self._iteration = 0  # 当前迭代次数
        self._consecutive_blocks = 0  # 连续被flow:complete钩子block的次数
        self._max_consecutive_blocks = 3  # 连续block上限，超过则强制完成

        # 钩子系统 v3.0（延迟初始化）
        self._hook_manager = None

        # 阶段9：暂停检测支持
        self._message_id = None
        self._task_id = None

        # Checklist调用检测
        self._checklist_reminded_id = -1

        # 迭代备份去重：上次成功备份的指纹（sha256(history_context+NUL+checklist)）。
        # 下一轮迭代开始时若指纹相同（如API受限重试期间history无任何变化），
        # 跳过回滚备份记录等操作。None 表示尚无成功备份基线。
        self._last_backup_fingerprint: str | None = None

        # 系统报错去重：追踪最近的系统报错签名，避免重复报告撑爆上下文
        self._recent_system_errors: list[str] = []

        # 🔧 事务#2061续：上次注入"上下文紧张"提示的迭代号（-极大值保证首轮注入）。
        # context_too_large是持续状态型告警，用迭代节流（每3轮一次）替代报错去重，
        # 防止单一签名永久卡死去重窗口导致一次注入后永久静默（task#2077事故）
        self._last_context_tight_iter: int = -1_000_000

        # 思考过载检测：跨迭代累积thinking字节数，超阈值且无有效编辑工作时强制关闭思考模式
        self._thinking_overload_bytes: int = 0  # 累积thinking字节数
        self._force_thinking_disabled: bool = False  # 是否强制关闭思考模式（仅影响当前事务）

        # thinking回顾扫描游标：上次已消费的输出文件行号（提醒时从游标后扫描thinking行，
        # run_stream 每轮开始重置为当前文件行数以跳过历史内容）
        self._thinking_scan_line: int = 0

        # 🔧 事务#2114：drop清理提示的唯一4位数字ID分配器（1000-9999顺序分配，
        # 跳过已用ID；用尽后重新扫描复用最旧ID——由于drop只允许清理上一轮迭代的
        # 结果，相隔9000次分配的同ID提示不可能同处一个窗口，无冲突风险）。
        # 注意：不随 run_stream 重入重置，避免resume后与history中已存提示ID撞车
        self._drop_id_seq: int = 9999
        self._used_drop_ids: set[int] = set()

    @property
    def _error_dedup_rounds(self) -> int:
        """动态获取报错去重轮数（从最新配置读取，支持热更新）"""
        return getattr(self.llm.config, "error_dedup_rounds", 5)

    # ==================== 重复行检测（相似度匹配） ====================
    # 相似度检测配置
    _DUP_LINE_THRESHOLD = 16  # 行最短长度（短行不检测）
    _DUP_LINE_WINDOW = 25  # 窗口大小（保留最近N行）
    _SIMILARITY_THRESHOLD = 0.80  # thinking/content相似度阈值：超过此值视为重复

    @staticmethod
    def _line_similarity(a: str, b: str) -> float:
        """计算两个字符串的相似度（0.0~1.0）

        使用 rapidfuzz 的 Levenshtein 归一化相似度。
        比 difflib.SequenceMatcher.ratio() 更准确：
        - SequenceMatcher 基于最长公共子序列，对'大部分相同中间小段不同'过于宽容
        - Levenshtein 基于编辑距离，精确反映实际差异量

        时间戳等微小差异对长字符串影响极小，不影响检测。
        """
        if not a or not b:
            return 0.0
        from rapidfuzz.distance import Levenshtein

        return Levenshtein.normalized_similarity(a, b)

    # 纯代码符号白名单：只含这些字符的短行（如 } { }; () 等）不参与死循环检测。
    # 这类符号在正常代码中高频出现（大括号配对、分号等），若参与检测会大量误报。
    _SYMBOL_ONLY_CHARS = frozenset("{}()[];,.:=+-*/<>!&|?%^~#@ \t")

    @classmethod
    def _is_symbol_only_line(cls, line: str) -> bool:
        """判断是否纯代码符号行（如 } { }; 等），这类行应跳过死循环检测"""
        return bool(line) and all(c in cls._SYMBOL_ONLY_CHARS for c in line)

    @staticmethod
    def _scan_consecutive_symbols(text: str, run: int, threshold: int) -> int:
        """纯符号无效输出检测：跟踪连续非字母字符（无字母、无中文）计数

        模型退化时会产生符号垃圾流（如 "!!!!!..."、"))))))..."），
        连续 threshold 个字符中无任何字母/中文时判定为无效输出。

        字符分类：str.isalpha()（含中英文等所有语言字母）视为有效内容并重置计数；
        其他字符（标点/运算符/空白/数字等）累加计数。
        正常代码/JSON输出中几百个连续非字母字符几乎不可能出现
        （大括号配对、缩进等通常几十字符内必混有字母）。

        Args:
            text: 流式chunk文本
            run: 当前连续符号计数（跨chunk累积，由调用方持有状态）
            threshold: 判定阈值（peng.json:llm.symbol_output_threshold）

        Returns:
            更新后的连续符号计数（达到threshold后提前返回，调用方应停止累加）
        """
        for c in text:
            if c.isalpha():
                run = 0
            else:
                run += 1
                if run >= threshold:
                    return run
        return run

    # ==================== 重复词语无效输出检测 ====================

    @staticmethod
    def _is_cjk_char(c: str) -> bool:
        """判断是否为CJK统一汉字（含扩展A）"""
        return "\u4e00" <= c <= "\u9fff" or "\u3400" <= c <= "\u4dbf"

    @staticmethod
    def _scan_repeated_words(text: str, state: dict, threshold: int) -> bool:
        """检测流式文本中连续N个完全相同的英文单词或中文词语（含单字）。

        分词策略：
        - 英文token：连续ASCII字母+数字混合序列（如"c0"、"x1"、"v2"整体为一个token）
          （事务#1653修复：原先只取字母序列，技术文本"c0=0-15, c1=16-31, c2=32-47..."
          中的c0/c1/c2被切成6个连续"c"token→误判重复词语→正常内容被丢弃）
          字母数字混合后：数字变化的标识符（c0,c1,c2...）token各不相同不再误报；
          完全相同的标识符复读（c0 c0 c0...）仍可检出。
        - 中文单字：单个CJK字符作为一个token
        - 中文短词组（2-4字）：通过正则检测重复模式

        状态通过 state dict 跨chunk保持：
        - 'buf': 跨chunk的未完成英文单词尾部（chunk边界可能截断单词）
        - 'last': 上一个完成的token（小写）
        - 'count': 与last相同的连续出现次数

        Args:
            text: 流式chunk文本
            state: 状态字典（原地修改）
            threshold: 判定阈值（连续相同token达到此数时触发）

        Returns:
            True 表示检测到无效重复输出（达到threshold）
        """
        if threshold <= 0:
            return False

        buf = state.get("buf", "") + text

        last = state.get("last", "")
        count = state.get("count", 0)

        i = 0
        n = len(buf)
        detected = False
        pending_tail = ""  # 跨chunk未完成的英文单词尾部

        while i < n:
            c = buf[i]

            if c.isascii() and (c.isalpha() or c.isdigit()):
                # 英文token：消费连续ASCII字母+数字（c0/x1/rgb8整体，#1653修复）
                j = i + 1
                while j < n and buf[j].isascii() and (buf[j].isalpha() or buf[j].isdigit()):
                    j += 1

                if j == n:
                    # chunk边界截断英文token，保留尾部等待下轮补全
                    # （注意：纯数字前缀如"16bytes"也暂存，下轮若仍是数字字母混合
                    # 仍是一个token；不完整时宁可暂存也不误切分）
                    pending_tail = buf[i:]
                    break

                token = buf[i:j].lower()
                i = j

                # 含数字的字母数字混合token（如c0、十六进制字面量、格式化占位符
                # 片段"4f"等）视为"其他内容"打断重复链（#1739修复，与#1688纯数字
                # token策略一致）：代码/格式串中这类token天然高频重复（如C的fprintf
                # 格式串里浮点占位符片段连续出现），跳过不重置→误判复读→正常代码
                # 输出被丢弃。自然语言的退化复读（the/the/the…、of/of/of…）是纯
                # 字母词连续重复，不受影响；混合标识符的"复读"（c0 c0 c0）在代码
                # 输出中几乎总是合法内容（如函数实参表），放弃检出以根除误报。
                if any(ch.isdigit() for ch in token):
                    last = ""
                    count = 0
                    continue

                # 更新连续计数（分隔符不重置计数，允许"the the the"中间有空格）
                if token == last:
                    count += 1
                else:
                    last = token
                    count = 1

                if count >= threshold:
                    detected = True
                    break

            elif ToolExecutionEngine._is_cjk_char(c):
                # CJK单字作为token（含单字如"的的的的的的"）
                token = c.lower()

                if token == last:
                    count += 1
                else:
                    last = token
                    count = 1

                if count >= threshold:
                    detected = True
                    break

                i += 1

            else:
                # 分隔符（空格、标点、换行等）：不重置计数
                i += 1

        # --- 阶段2：中文短词组重复检测（2-4字模式重复N次） ---
        # 检测如"好的好的好的好的好的好的"这类短词组复读模式。
        # #1739修复：必须基于跨chunk累积缓冲检测，不能用本轮临时buf——
        # 小chunk流式（如2字符/chunk）时每轮buf最多约3个汉字，凑不齐
        # threshold次词组重复所需长度（如6次×2字=12字）→ 永远漏检。
        if not detected and threshold >= 4:
            import re as _re

            # 跨chunk原始尾部缓冲：threshold次×4字 + 分隔/标点余量
            keep_len = threshold * 4 + 16
            raw_tail = (state.get("raw_tail", "") + text)[-keep_len:]

            # 匹配：同一2-4字CJK序列连续出现threshold次（无分隔或仅标点分隔）
            pattern = _re.compile(
                r"([\u4e00-\u9fff\u3400-\u4dbf]{2,4})\1{" + str(threshold - 1) + r"}"
            )
            if pattern.search(raw_tail):
                detected = True
            raw_tail_cleared = False
        else:
            raw_tail = ""
            raw_tail_cleared = True

        # --- 更新状态 ---
        if detected:
            state.clear()  # 检测到后重置全部状态（调用方会break停止流式处理）
        else:
            state["last"] = last
            state["count"] = count
            state["buf"] = pending_tail  # ""=无pending，非空=等待下轮补全英文单词
            if not raw_tail_cleared:
                state["raw_tail"] = raw_tail

        return detected

    # ==================== 重复短语无效输出检测 ====================

    @staticmethod
    def _scan_repeated_phrases(text: str, state: dict, threshold: int, maxlen: int) -> bool:
        """检测流式文本中连续N个完全相同的字符串（英文短语/中文词语，≤maxlen字符）。

        背景（事务#1599漏检场景）：s-qwen3.8思考退化时单行内复读同一英文短语：
        "these are all independent so I'll batch them together now — " × 19次。
        单词不重复（漏过repeated_word检测）、全部在单行内（漏过行重复检测），
        需要短语级检测兜底。

        算法（尾部锚定+贪心反向验证，流式O(n·m)）：
        1. 维护跨chunk累积buf（最长 maxlen*threshold*2 + 分隔符余量）；
        2. 尝试每个候选unit长度L（2..min(maxlen, buf_len//threshold)）：
           假设buf末尾是复读的最后一次出现，unit=buf末尾L个字符；
        3. 从尾端反向贪心匹配：unit外部前导分隔符（空格/标点/换行等非字母数字
           中文，≤8个）跳过后，要求buf[pos-L:pos]==unit，统计连续重复次数；
           注：unit允许自带尾部分隔符（如"now — "），反向匹配时unit外的分隔符
           跳过后窗口自动对齐（unit两侧分隔符自适应）；
        4. 重复次数≥threshold 且 unit满足资格（#1739收紧：首字符为字母/CJK，
           且含≥3连续ASCII字母或含CJK，排除代码格式串片段误报）→ 检出。

        性能：仅对buf尾部操作，每次chunk最多 maxLen × threshold 次比较，
        maxlen=256时约256×10=2560次短字符串比较，微秒级。

        状态通过 state dict 跨chunk保持：
        - 'buf': 跨chunk累积的待检文本尾部

        Args:
            text: 流式chunk文本
            state: 状态字典（原地修改）
            threshold: 连续重复次数阈值（thinking/content_dead_loop_threshold）
            maxlen: 短语最大字符数（repeated_phrase_maxlen，0=禁用）

        Returns:
            True 表示检测到短语级复读（达到threshold次）
        """
        if threshold <= 0 or maxlen < 2:
            return False

        # 累积缓冲：足够容纳 threshold 个最大unit + 分隔符余量
        max_buf = maxlen * threshold + 8 * threshold
        buf = state.get("buf", "") + text
        if len(buf) > max_buf:
            buf = buf[-max_buf:]

        def is_word_char(c: str) -> bool:
            return c.isalpha() or c.isdigit() or ToolExecutionEngine._is_cjk_char(c)

        n = len(buf)
        # unit至少2字符且至少能重复threshold次
        max_unit_len = min(maxlen, n // threshold)
        detected = False

        for unit_len in range(2, max_unit_len + 1):
            unit = buf[n - unit_len :]
            # unit资格（#1739修复收紧）：
            # 1. 含字母或中文（排除纯符号/纯数字/纯空白）；
            # 2. 首字符必须是字母或CJK——代码文本中合法的高频重复片段
            #    （如fprintf格式串里被逗号分隔的浮点占位符序列切片）几乎都以
            #    标点/数字开头，以词首锚定可根除误报；
            # 3. 含≥3个连续ASCII字母或含CJK——真正的自然语言/中文短语复读
            #    （如英文实词短语、中文词组）必然满足，孤立的短字母组合
            #    （十六进制对、单字母参数等）不再触发。
            if not any(c.isalpha() for c in unit):
                continue
            first = unit[0]
            if not (first.isalpha() and first.isascii()) and not ToolExecutionEngine._is_cjk_char(
                first
            ):
                continue
            has_word_core = False
            letter_run = 0
            for c in unit:
                if c.isalpha() and c.isascii():
                    letter_run += 1
                    if letter_run >= 3:
                        has_word_core = True
                        break
                else:
                    letter_run = 0
            if not has_word_core and not any(ToolExecutionEngine._is_cjk_char(c) for c in unit):
                continue
            # 反向贪心验证：从末尾向前数连续重复
            repeats = 1
            pos = n - unit_len
            while pos > 0 and repeats < threshold:
                # 跳过unit外部的分隔符（非字母数字中文字符，最多8个）。
                # 自适应对齐：unit若以分隔符结尾（如"now — "），其前一次出现
                # 尾部的分隔符同属unit，因此跳过前先检查当前pos左侧字符是否
                # 恰好是unit末尾分隔符的延续——统一策略：跳过≤8个分隔符后，
                # 若窗口内容==unit则命中；否则再从原始pos重试一次「不跳分隔符」
                # 的精确对齐（覆盖unit两侧分隔符共享的场景）。
                start = pos
                skipped = 0
                while start > 0 and skipped <= 8 and not is_word_char(buf[start - 1]):
                    start -= 1
                    skipped += 1
                candidates = []
                if skipped > 0 and skipped <= 8:
                    candidates.append(start)  # 跳过分隔符后的对齐点
                candidates.append(pos)  # 不跳分隔符的原始对齐点
                matched = False
                for cand in candidates:
                    if cand < unit_len:
                        continue
                    if buf[cand - unit_len : cand] == unit:
                        pos = cand - unit_len
                        matched = True
                        break
                if not matched:
                    break
                repeats += 1
            if repeats >= threshold:
                detected = True
                break

        if detected:
            state.clear()  # 检测到后重置状态（调用方会break停止流式处理）
        else:
            state["buf"] = buf

        return detected

    def _detect_duplicate_lines(
        self,
        text: str,
        task_id: int,
        dup_count: int,
        recent_lines: list[str],
        buffer: str,
        dead_loop_threshold: int = 3,
        similarity_threshold: float | None = None,
        window_size: int | None = None,
        label: str = "内容",
    ) -> tuple[int, list[str], str, bool]:
        """
        公共重复行检测：窗口内相似度匹配

        将流式chunk拼接成完整行，在滑动窗口内检测相似行重复。
        当连续命中达到阈值时触发。

        被 _process_thinking_chunk 和 content死循环检测 共同复用。

        行匹配策略（v2，修复短行死循环漏检）：
        - 长行（> _DUP_LINE_THRESHOLD）：相似度匹配（≥ sim_thresh）
        - 短行（≤ _DUP_LINE_THRESHOLD）：精确匹配（必须完全相同）
          「让我搜索。」等短行反复输出是典型死循环信号，但相似度匹配
          对短行过于宽松（如「}」vs「{」也可能高分），精确匹配可避免
          正常代码短行误报，同时捕获真正的短行死循环。

        Args:
            text: 流式chunk文本
            task_id: 事务ID（日志用）
            dup_count: 当前连续重复命中计数
            recent_lines: 当前窗口内行列表
            buffer: 未完成行的缓冲
            dead_loop_threshold: 触发阈值
            similarity_threshold: 相似度阈值（None=用_SIMILARITY_THRESHOLD）
            window_size: 窗口大小（None=用_DUP_LINE_WINDOW）
            label: 日志标签（"思考"/"content"）

        Returns:
            (dup_count, recent_lines, buffer, detected)
        """
        sim_thresh = similarity_threshold or self._SIMILARITY_THRESHOLD
        win = window_size or self._DUP_LINE_WINDOW

        buffer += text
        while "\n" in buffer:
            line_end = buffer.index("\n") + 1
            line = buffer[:line_end].rstrip("\n")
            buffer = buffer[line_end:]

            # 空行跳过（不参与计数，也不重置计数）
            if not line:
                continue

            # 纯代码符号行（如 } { }; 等）跳过：正常代码中高频出现，
            # 不是死循环信号，参与检测会误报
            if self._is_symbol_only_line(line):
                continue

            recent_lines.append(line)
            if len(recent_lines) > win:
                recent_lines.pop(0)

            window_without_current = recent_lines[:-1]
            is_similar = False
            if len(line) > self._DUP_LINE_THRESHOLD:
                # 长行：窗口内相似度匹配（变异体重复也检测，如"让我搜索X"vs"让我用grep搜索X"）
                for prev_line in window_without_current:
                    if self._line_similarity(line, prev_line) >= sim_thresh:
                        is_similar = True
                        break
            else:
                # 短行：只与紧邻的上一行精确匹配（必须完全相同）
                # 短行死循环的本质是"连续重复同一短句"（如"让我搜索。"x60），
                # 只比较上一行可避免交替模式（return;/break;/continue;循环）累积误报
                if len(recent_lines) >= 2 and line == recent_lines[-2]:
                    is_similar = True

            if is_similar:
                dup_count += 1
                if dup_count >= dead_loop_threshold:
                    logger.warning(
                        f"⚡ [ToolEngine] 事务#{task_id}检测到{label}重复死循环"
                        f"(窗口{len(recent_lines)}行内相似重复{dup_count}次): {line[:60]}..."
                    )
                    return dup_count, recent_lines, buffer, True
            else:
                dup_count = 0

        return dup_count, recent_lines, buffer, False

    def _process_thinking_chunk(
        self,
        chunk_thinking: str,
        task_id: int,
        should_skip_iteration: bool,
        thinking_duplicate_count: int,
        thinking_recent_lines: list[str],
        thinking_buffer: str,
        wait_hit_count: int = 0,
        wait_threshold: int = 3,
        dead_loop_threshold=3,
    ) -> tuple[bool, int, list[str], str, int]:
        """
        处理thinking流式chunk，检测：
        1. 重复行瓶颈（相似度匹配，复用_detect_duplicate_lines）
        2. 精神分裂式思考模式（wait/等等 行首命中 >= 2）

        Returns:
            (should_skip_iteration, thinking_duplicate_count, thinking_recent_lines, thinking_buffer, wait_hit_count)
        """
        # 公共重复检测（内部管理buffer拼接）
        thinking_duplicate_count, thinking_recent_lines, thinking_buffer, dup_detected = (
            self._detect_duplicate_lines(
                chunk_thinking,
                task_id,
                thinking_duplicate_count,
                thinking_recent_lines,
                thinking_buffer,
                dead_loop_threshold=dead_loop_threshold,
                label="思考",
            )
        )

        # wait/等等检测：从同一buffer中提取的行已经在_detect_duplicate_lines中处理，
        # 但wait检测需要独立扫描完整行。用thinking_buffer的残余+chunk来检测。
        # 简化：直接扫描chunk_thinking中的完整行
        wait_scan = chunk_thinking
        while "\n" in wait_scan:
            line_end = wait_scan.index("\n") + 1
            line = wait_scan[:line_end].rstrip("\n")
            wait_scan = wait_scan[line_end:]

            prefix = line[:30].lower()
            if "wait" in prefix or "等等" in prefix:
                wait_hit_count += 1
                if wait_hit_count >= wait_threshold:
                    logger.info(
                        f"⚡ [ToolEngine] 事务#{task_id}检测到精神分裂式思考"
                        f"(wait/等等命中{wait_hit_count}次)"
                    )
                    return (
                        True,
                        thinking_duplicate_count,
                        thinking_recent_lines,
                        thinking_buffer,
                        wait_hit_count,
                    )

        if dup_detected:
            return (
                True,
                thinking_duplicate_count,
                thinking_recent_lines,
                thinking_buffer,
                wait_hit_count,
            )

        return (
            should_skip_iteration,
            thinking_duplicate_count,
            thinking_recent_lines,
            thinking_buffer,
            wait_hit_count,
        )

    # ==================== 精神分裂式思考检测（wait/等等） ====================

    @property
    def _prompts(self):
        """动态获取最新的提示字符串配置（每次调用都重新读取config，支持热更新）"""
        return load_config().llm.prompts

    # ==================== 跨迭代死循环检测 ====================

    # ==================== 迭代 PUA（每 N 轮注入） ====================

    def _iteration_to_str(self, tool_calls: list, tool_results: list) -> str:
        """把一轮迭代的工具调用+结果拼接为完整字符串，用于相似度比较。

        对于编辑类工具（edit/line_edit/write），content不同=有效操作≠死循环。
        签名只保留工具名+content hash，不含文件路径（文件路径过长会主导相似度计算，
        导致对同一文件的不同内容编辑签名相似度≥0.90而误报死循环）。
        完全相同的编辑（包括相同content）才视为可能死循环。
        """

        # 编辑类工具：实际有效操作，content是关键区分维度
        _EDIT_TOOLS = frozenset({"edit", "line_edit", "write"})

        parts = []
        for tc, result in zip(tool_calls, tool_results or []):
            name = tc.name if hasattr(tc, "name") else tc.get("function", {}).get("name", "")
            args = tc.arguments if hasattr(tc, "arguments") else {}
            if not isinstance(args, dict):
                try:
                    args = json.loads(args) if isinstance(args, str) else {}
                except Exception:
                    args = {}

            if name in _EDIT_TOOLS:
                # 编辑工具签名：工具名 + content hash（不含文件路径！）
                # 文件路径过长会主导相似度计算，导致同一文件不同编辑误报死循环
                # content不同→hash不同→签名完全不同→不误报
                # content完全相同→hash相同→签名相同→能检测真正的死循环
                content_blob = ""
                for k in ("content", "oldString", "newString"):
                    v = args.get(k, "")
                    if isinstance(v, str):
                        content_blob += v
                if content_blob:
                    content_hash = hashlib.md5(content_blob.encode()).hexdigest()[:12]
                    parts.append(f"{name}:{content_hash}")
                else:
                    # 无内容参数（如write只有filePath）：用文件名做签名
                    fp = args.get("filePath", "")
                    parts.append(f"{name}:{hashlib.md5(fp.encode()).hexdigest()[:12]}")
            else:
                result_str = ""
                if hasattr(result, "summary"):
                    result_str = result.summary or ""
                elif isinstance(result, str):
                    result_str = result
                parts.append(f"{name} {json.dumps(args, ensure_ascii=False)} => {result_str}")
        return "\n".join(parts)

    def _is_final_report_call(self, tool_calls: list) -> bool:
        """判断本轮迭代是否以 report progress=100 结束（事务即将正常收尾）

        事务总管等监控型事务会周期性 report 汇报监控状态，这是其正常职责。
        且 report progress=100 时 report 钩子会立即强行中止事务，
        此时不应触发死循环检测，否则会打断正常的事务收尾流程。

        Args:
            tool_calls: 本轮迭代的工具调用列表

        Returns:
            True: 本轮以 report progress=100 收尾，应跳过死循环检测
        """
        for tc in tool_calls:
            name = tc.name if hasattr(tc, "name") else tc.get("function", {}).get("name", "")
            if name != "report":
                continue
            args = tc.arguments if hasattr(tc, "arguments") else {}
            if not isinstance(args, dict):
                try:
                    args = json.loads(args) if isinstance(args, str) else {}
                except Exception:
                    args = {}
            # report 且 progress>=100：事务即将正常结束
            if int(args.get("progress", 0) or 0) >= 100:
                return True
        return False

    def _check_cross_iteration_loop(
        self,
        tool_calls: list,
        content: str,
        thinking: str,
        history: MessageHistory,
        task_id: int,
        tool_results: list = None,
    ) -> bool:
        """检测跨迭代死循环（完整字符串相似度比较）

        方法极其简单：
        1. 把每轮迭代的工具调用+结果拼成一个完整字符串
        2. 用 SequenceMatcher.ratio() 直接算与历史轮次的纯文本相似度
        3. 窗口内相似轮次达到阈值 → 死循环

        Returns:
            True 表示检测到死循环并已处理
        """
        # report progress=100：事务即将正常收尾（report钩子会立即中止事务），
        # 事务总管等监控型事务周期性report是正常职责，跳过死循环检测
        if self._is_final_report_call(tool_calls):
            return False

        if not hasattr(self, "_iteration_strings"):
            self._iteration_strings: list[str] = []

        current = self._iteration_to_str(tool_calls, tool_results or [])

        # 每轮迭代都纳入相似度比较，不做任何短路
        self._iteration_strings.append(current)

        WINDOW = 8
        if len(self._iteration_strings) > WINDOW:
            self._iteration_strings = self._iteration_strings[-WINDOW:]

        # edit/line_edit/write 是实际有效工作，适度放宽检测参数：
        # 提高相似度阈值(0.90→0.95) + 增加触发轮数(3→5)
        # 配合hash签名，不同内容的编辑相似度远低于0.95，不会误报
        # 但完全相同的编辑连续5轮仍能检测
        _EDIT_TOOLS = frozenset({"edit", "line_edit", "write"})
        is_all_edit = tool_calls and all(
            (tc.name if hasattr(tc, "name") else tc.get("function", {}).get("name", ""))
            in _EDIT_TOOLS
            for tc in tool_calls
        )
        if is_all_edit:
            THRESHOLD = 0.95
            MIN_SIMILAR = 8
        else:
            THRESHOLD = 0.90
            MIN_SIMILAR = 3

        if len(self._iteration_strings) <= MIN_SIMILAR:
            return False

        similar_count = 0
        for prev in self._iteration_strings[:-1]:
            sim = self._line_similarity(current, prev)
            if sim >= THRESHOLD:
                similar_count += 1

        if similar_count < MIN_SIMILAR:
            # 窗口内相似轮次不足，说明模型已经跳出了之前的死循环，重置计数器
            self._consecutive_dead_loops = 0
            return False
        self._consecutive_dead_loops += 1
        loop_n = self._consecutive_dead_loops

        logger.warning(
            f"⚡ [ToolEngine] 事务#{task_id}检测到死循环(第{loop_n}次)！"
            f"最近{len(self._iteration_strings)}轮中有{similar_count}轮相似度≥{THRESHOLD}。"
        )

        messages = getattr(history, "_messages", [])

        if loop_n >= 3:
            fail_msg = "干预失败，需🦉 事务总管协助处理"
            logger.warning(f"⚡ [ToolEngine] 事务#{task_id}已连续{loop_n}次死循环，{fail_msg}")
            get_output_manager().write_output(
                task_id=task_id,
                content=f"{ANSI_COLORS.get('bright_red')}{fail_msg}{ANSI_COLORS.get('reset')}\n",
                source="system",
            )
            self._save_history(history)
            self.task_manager.db.update_tasks([task_id], status="failed", process_id=None)
            time.sleep(3)
            sys.exit(1)

        # 回滚失败的写操作历史
        rollback_count = similar_count * 3 + (10 if loop_n >= 2 else 0)
        rolled = 0
        while rollback_count > 0 and len(messages) > 10:
            msg = messages[-1]
            if msg.get("role") == "user":
                break
            if msg.get("role") == "tool" and msg.get("name", "") in (
                "write",
                "edit",
                "bash",
                "cmd",
            ):
                tool_content = msg.get("content", "")
                _is_tool_failed = (
                    "❌" in tool_content
                    or '"error"' in tool_content
                    or "未找到" in tool_content
                    or "not found" in tool_content.lower()
                    or "必填" in tool_content
                    or "参数" in tool_content
                    or "失败" in tool_content
                )
                if not _is_tool_failed:
                    break
            messages.pop()
            rollback_count -= 1
            rolled += 1

        import random

        hint = random.choice(self._prompts.dead_loop_hints)
        if loop_n >= 2:
            hint = f"⚠️ 这是第{loop_n}次检测到死循环！行为模式重复。{hint} 如果再次陷入同样的循环，系统将中止事务！"
        else:
            hint = f"⚠️  死循环检测： {similar_count}轮迭代相似度≥{THRESHOLD}。{hint}"
        history.add_user(hint)
        self._save_history(history)
        get_output_manager().write_output(
            task_id=task_id,
            content=f"{ANSI_COLORS.get('bright_red')}{hint}{ANSI_COLORS.get('reset')}\n",
            source="system",
        )
        self._iteration_strings = [current]
        return True

    def _error_signature(self, content: str) -> str:
        """生成系统报错的标准化签名（用于去重比较）

        去除ANSI颜色码、时间戳、具体数字（token计数、重试次数等可变部分），
        保留错误类型和结构特征。

        Args:
            content: 原始报错内容

        Returns:
            标准化后的错误签名字符串
        """
        import re

        # 去除ANSI颜色码
        sig = re.sub(r"\x1b\[[0-9;]*m", "", content)
        # 将所有连续数字替换为 #（token计数、重试次数、HTTP状态码、时间戳等可变部分）
        sig = re.sub(r"\d+", "#", sig)
        return sig.strip()

    # 内容审核逐级清除的下限保护：history消息数（不含system）裁剪到该值仍被
    # 拦截则放弃重试并结束事务，避免死循环空耗
    _CONTENT_FILTER_MIN_MESSAGES = 6

    # 兜底转码的绝对安全图片MIME集合（🔧 事务#1834）。比入口白名单
    # PROVIDER_SAFE_IMAGE_MIMES 更严格：webp/gif 也有 provider 不支持，
    # 兜底救援时统一转码 JPEG（所有vision provider支持度最高且体积最小）
    _REENCODE_SAFE_IMAGE_MIMES = frozenset({"image/jpeg", "image/png"})

    def _reencode_history_images(self, history) -> int:
        """将history中非绝对安全格式的图片附件转码为JPEG（就地修改）。

        🔧 事务#1834：内容解析失败(HTTP 400图片输入格式/解析错误)的兜底
        救援层——history中可能存有provider不支持的图片格式（如修复前入库
        的BMP附件、provider连webp也不支持等），级1直接清除附件会永久丢失
        视觉信息，转码为provider通用JPEG后重试可完整保留。

        处理范围：content为list的消息中 image_url/input_image part 的
        data:image/* base64 URL（image_url嵌套/平铺两种格式均兼容）。
        转码失败的附件（数据损坏等）跳过，留待级1清除策略处理。

        🔧 事务#2119：是否转码以**实际内容**（PIL识别）判断，而非附件自称的
        mime——文件被误命名时（截图 BMP 存成 .png，事务#2081 实测 w_R1.png
        魔数为 'BM'）附件自称 image/png 而字节是 BMP，provider 按声明解码必然
        失败（400 "the image could not be read"），只看声明会漏判为"安全格式"。

        Returns:
            成功转码的附件数（0=无可转码附件）
        """
        import base64 as _b64
        import io as _io

        from peng.tools.file import PIL_FORMAT_TO_MIME

        reencoded = 0
        for msg in getattr(history, "_messages", []):
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") not in (
                    "image_url",
                    "input_image",
                ):
                    continue
                # 提取 url：image_url嵌套dict/平铺str/part顶层 三种格式
                holder = part.get("image_url")
                if isinstance(holder, dict):
                    url = holder.get("url")
                elif isinstance(holder, str):
                    url = holder
                else:
                    url = part.get("url")
                if not isinstance(url, str) or not url.startswith("data:image/"):
                    continue
                header, _sep, b64_payload = url.partition(";base64,")
                mime = header.removeprefix("data:").split(";", 1)[0]
                if not b64_payload:
                    continue
                try:
                    from PIL import Image

                    img = Image.open(_io.BytesIO(_b64.b64decode(b64_payload)))
                    img.load()
                    # 🔧 事务#2119：是否转码以**实际内容**（PIL识别）判断，而非
                    # 附件自称的 mime。文件被误命名时（截图 BMP 存成 .png，事务
                    # #2081 实测 w_R1.png 魔数为 'BM'）附件自称 image/png 而字节
                    # 是 BMP，provider 按声明解码必然失败（本地引擎 400
                    # "the image could not be read: cannot read the image"）；
                    # 只看声明会漏判为"安全格式"而跳过救援。按内容转码修复，
                    # 既解决 400 又不丢失视觉信息。
                    _actual_mime = PIL_FORMAT_TO_MIME.get((img.format or "").upper())
                    if _actual_mime == mime and mime in self._REENCODE_SAFE_IMAGE_MIMES:
                        # 实际格式与声明一致且属安全格式：无需转码
                        continue
                    # JPEG不支持alpha/CMYK：RGBA/LA/P白底合成，CMYK转RGB
                    if img.mode in ("RGBA", "LA", "P"):
                        img = img.convert("RGBA")
                        bg = Image.new("RGB", img.size, (255, 255, 255))
                        bg.paste(img, mask=img.split()[-1])
                        img = bg
                    elif img.mode not in ("RGB", "L"):
                        img = img.convert("RGB")
                    buf = _io.BytesIO()
                    img.save(buf, format="JPEG", quality=90)
                    new_url = (
                        f"data:image/jpeg;base64,{_b64.b64encode(buf.getvalue()).decode('utf-8')}"
                    )
                    if isinstance(holder, dict):
                        holder["url"] = new_url
                    elif isinstance(holder, str):
                        part["image_url"] = new_url
                    else:
                        part["url"] = new_url
                    reencoded += 1
                except Exception as e:
                    logger.warning(
                        f"[ToolEngine] 图片附件转码救援失败(留待级1清除): mime={mime}: {e}"
                    )
        return reencoded

    def _content_filter_next_action(self, history) -> str:
        """内容审核拦截(HTTP 400敏感内容)的逐级升级清除策略。

        背景：上下文中存在触发provider内容审核的内容时，重试相同内容必然
        再次失败。触发源可能在任意消息中——最常见是多模态图片附件，而附件
        可能位于user消息也可能位于tool结果消息（如read工具读图）；只删
        user消息文本无法消除触发源，会导致无限重试死循环。

        策略：每失败一轮调用本方法执行下一级清除动作（无效动作自动跳级），
        逐级扩大清除范围直至审核通过：
          级0: 🔧 事务#1834 内容解析失败类错误且首次进入（deletions==0）时，
               先尝试转码救援：将history中非安全格式图片附件转码JPEG后重试，
               完整保留视觉信息（级1清除附件会永久丢失视觉，导致模型反复
               read反复失败的死循环）。救援不递增级别编号，转码后仍失败则
               无可转附件（返回0）自然落入级1。
          级1: 清除history中所有消息的多模态附件part（复用只发送一次机制
               的strip_sent_attachments，覆盖user/tool消息）
          级2: 删除最后一条user消息（可能是触发审核的文本输入）
          级3+: 裁剪最后一个完整轮次（从最后一个user消息到尾部），逐步
               逼近并裁掉更早的可疑内容；轮次整体裁剪天然保持
               assistant(tool_calls)与tool结果的配对完整
        下限保护：消息数裁剪到 _CONTENT_FILTER_MIN_MESSAGES 仍被拦截时，
        说明可疑内容是用户原始任务本身，放弃重试。

        Args:
            history: MessageHistory 对象（就地修改）

        Returns:
            执行的清除动作描述（用于系统消息输出）

        Raises:
            RuntimeError: 所有级别用尽且消息数已到下限仍被拦截
        """
        messages = getattr(history, "_messages", [])
        stage = self._content_filter_deletions

        # 🔧 事务#1834：内容解析失败类错误（图片输入格式/解析错误）优先
        # 转码救援（仅首次，deletions==0）：history中存有provider不支持
        # 的图片格式（如修复前入库的BMP，事务#1822）时，转码为通用JPEG
        # 后重试可完整保留视觉信息。转码后再失败则无可转附件（返回0），
        # 自然落入下方级1清除逻辑，级别序列保持不变。
        if self._content_filter_deletions == 0 and "内容解析失败" in (
            getattr(self, "_content_filter_kind", "") or ""
        ):
            reencoded = self._reencode_history_images(history)
            if reencoded > 0:
                return f"转码 {reencoded} 个图片附件为JPEG（provider通用格式）"

        while True:
            if stage == 0:
                # 级1：清除多模态附件（审核拦截最常见触发源，含tool消息中的附件）
                stage += 1
                # 🔧 事务#2119：解析失败类错误（provider 读不出附件/附件字节与声明
                # 不符）用**错误反馈文案**替换附件——把 provider 报错返回给大模型，
                # 让它知道该附件从未送达并自行重新读取/转码；"已随上一轮请求发送
                # 给你"的默认说明在此语义下是假的（会诱导模型臆测图片内容）。
                _notice = None
                if "内容解析失败" in (getattr(self, "_content_filter_kind", "") or ""):
                    _notice = history.attachment_rejected_notice(self.llm.error or "")
                removed = history.strip_sent_attachments(notice=_notice)
                if removed > 0:
                    self._content_filter_deletions = stage
                    return f"清除多模态附件 {removed} 个（图片/音视频/PDF）"
                continue  # 无附件可清，自动跳级
            if stage == 1:
                # 级2：删除最后一条user消息
                stage += 1
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].get("role") == "user":
                        deleted_msg = messages.pop(i)
                        self._content_filter_deletions = stage
                        content = deleted_msg.get("content", "")
                        desc = (
                            f"{len(content)}字符" if isinstance(content, str) else "多模态content"
                        )
                        return f"删除最后一条user消息({desc})"
                continue  # 无user消息可删，自动跳级
            # 级3+：裁剪最后一个完整轮次（从最后一个user消息到尾部），
            # 每次删除一个轮次逐步逼近更早的可疑内容；user开启的轮次整体
            # 裁剪天然保持assistant(tool_calls)与tool结果的配对完整
            if len(messages) <= self._CONTENT_FILTER_MIN_MESSAGES:
                raise RuntimeError(
                    f"内容审核拦截持续存在：已逐级清除多模态附件/user消息/"
                    f"{max(0, stage - 1)}个尾部轮次，上下文已裁剪至下限"
                    f"({len(messages)}条)仍被provider拦截(400)，疑似用户原始"
                    f"任务内容本身触发审核，停止重试。请人工检查事务上下文。"
                )
            stage += 1
            last_user_idx = -1
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    last_user_idx = i
                    break
            if last_user_idx == -1:
                # 无user消息（全为assistant/tool）：清空全部，下次调用触发下限raise
                removed_count = len(messages)
                del messages[:]
            else:
                removed_count = len(messages) - last_user_idx
                del messages[last_user_idx:]
            self._content_filter_deletions = stage
            return f"裁剪上下文尾部 {removed_count} 条消息"

    def _should_report_system_error(self, error_content: str) -> bool:
        """检查系统报错是否应该被报告（去重检测）

        检测最近的系统报错历史中是否已有类似的报错签名。
        如果在最近 error_dedup_rounds 轮内已有类似报错，则不再重复报告。

        Args:
            error_content: 错误内容

        Returns:
            True: 应该报告（首次出现或与最近的报错不同）
            False: 重复报错，应跳过
        """
        signature = self._error_signature(error_content)

        # 检查最近的报错签名中是否已有类似的
        recent = self._recent_system_errors[-self._error_dedup_rounds :]
        if signature in recent:
            return False

        # 记录本次报错签名
        self._recent_system_errors.append(signature)
        # 只保留最近 error_dedup_rounds * 2 条（允许一定的变化窗口）
        max_keep = self._error_dedup_rounds * 2
        if len(self._recent_system_errors) > max_keep:
            self._recent_system_errors = self._recent_system_errors[-max_keep:]

        return True

    def _has_pending_interrupts(self) -> bool:
        """
        检测事务是否有待处理的**控制信号**中断：
        - 未处理的控制信号（cancel/pause/resume/restart）

        注意：新回复消息和未读info**不在此处检测**（用户明确要求：回复消息/info
        不要打断思考）。它们由每轮迭代开始时的 _get_unread_reply/_get_task_info
        自然注入处理——当前思考流和工具调用循环正常完成后，下一轮迭代边界
        才处理新消息/info，不中断、不丢弃当前迭代已产生的思考内容。

        例外：以感叹号开头的新回复消息/未读info是紧急指示，由
        _has_urgent_interrupts 独立检测并打断思考立刻处理（事务#1860）。

        控制信号（pause/cancel等）是系统级控制操作，必须立即响应，
        因此仍在流式循环中检测（thinking每chunk + 每32chunk各一次）。

        Returns:
            True 表示有控制信号中断，应该停止当前思考跳过本次迭代
        """
        task_id = self._task_id
        if not task_id:
            return False

        # 检查未处理的控制信号
        try:
            controls = self.task_manager.db.get_unprocessed_task_controls()
            if controls:
                for control in controls:
                    if control["task_id"] == task_id:
                        action = control["action"]
                        if action in ("cancel", "pause", "resume", "restart"):
                            logger.info(
                                f"⚡ [ToolEngine] 事务#{task_id}检测到控制信号[{action}]，停止思考跳过本次迭代"
                            )
                            return True
        except Exception as e:
            logger.debug(f"[ToolEngine] 检查控制信号失败: {e}")

        return False

    def _has_urgent_interrupts(self) -> str | None:
        """检测是否有以感叹号开头的**紧急**新回复消息/新info（事务#1860）

        以感叹号（半角 ! 或全角 ！）开头的新回复消息/未读info视为紧急指示，
        优先级等同控制信号，必须立刻响应：
        - 打断当前思考流（调用方设置 should_skip_iteration，与控制信号同路径）
        - 打断正在运行的bash工具调用进程（tools/bash.py _interrupt_monitor）
        - 下一轮迭代边界 _get_unread_reply/_get_task_info 立刻注入处理

        普通消息/info不打断思考，仍由迭代边界自然注入（见 _has_pending_interrupts）。

        消费点唯一性：本方法只做**预览**（不消费），标记已读仍由迭代边界的
        _get_unread_reply/_get_task_info 负责，保证打断后消息必然被处理。
        bash侧监控同样只预览（processing状态/未读info），与消费语义一致。

        Returns:
            紧急中断原因描述（None表示无紧急中断）
        """
        task_id = self._task_id
        if not task_id:
            return None

        # 节流：思考流每个thinking chunk都会调用，DB查询限频1秒；
        # 缓存仅在节流窗口内复用（检测到中断时立刻复位，避免消费后过期缓存）
        now = time.monotonic()
        if now - self._last_urgent_check_ts < 1.0:
            return self._last_urgent_interrupt_reason
        self._last_urgent_check_ts = now

        reason = None

        # 1. 检查未读info（预览，不消费）
        try:
            info_items = self.task_manager.db.get_task_info(task_id, mode="unread")
            for item in info_items:
                content = (item.get("content") or "").replace("\\n", "\n")
                if is_urgent_content(content):
                    reason = f"收到感叹号开头的紧急info：{content.strip()[:100]}"
                    break
        except Exception as e:
            logger.debug(f"[ToolEngine] 检查紧急info失败: {e}")

        # 2. 检查未读回复消息（仅根事务负责，与 _get_unread_reply 一致；预览不消费）
        if not reason and not self.task_manager.get_parent_id(task_id):
            try:
                for reply in self._get_processing_messages(self._message_id):
                    if is_urgent_content(reply.content):
                        reason = (
                            f"收到感叹号开头的紧急消息#{reply.id} "
                            f"[{reply.timestamp}] {reply.sender}：{(reply.content or '').strip()[:100]}"
                        )
                        break
            except Exception as e:
                logger.debug(f"[ToolEngine] 检查紧急回复消息失败: {e}")

        if reason:
            logger.info(f"⚡ [ToolEngine] 事务#{task_id}检测到紧急中断：{reason}")
            # 复位节流：中断将触发跳迭代，消费由迭代边界完成，
            # 复位避免1秒窗口内的过期缓存导致重复跳迭代
            self._last_urgent_check_ts = 0.0

        self._last_urgent_interrupt_reason = reason
        return reason

    def _get_processing_messages(self, current_message_id: int | None) -> list[dict[str, Any]]:
        """
        检查是否有新的待处理回复消息（支持事务执行过程中响应新需求）

        Args:
            current_message_id: 当前处理的根消息ID

        Returns:
            新消息列表（只返回与当前事务相关的消息）
        """
        try:
            # 过滤出与当前消息链相关的消息
            # 策略：只返回当前消息链的回复消息（reply_to指向链首或链中的消息）
            processing_messages = []
            if current_message_id:
                # 获取当前消息的回复链
                chain = self.message_manager.get_message_tree(current_message_id)
                if chain is None:
                    logger.warning(
                        f"📜  事务#{self._task_id}的消息树#{current_message_id} 不存在，可能是消息已被删除。"
                    )
                    return []
                # 过滤：回复给链中任一消息的未读消息
                for msg in chain.all_messages:
                    if (
                        msg.id != current_message_id
                        and msg.status == "processing"
                        and msg.receiver == get_agent_name()
                    ):  # 仅处理发送给agent的消息
                        processing_messages.append(msg)
            logger.debug(
                f"📜  事务#{self._task_id}的消息树#{current_message_id}中待处理消息数量: {len(processing_messages)}"
            )
            return processing_messages

        except Exception as e:
            logger.warning(f"检查事务#{self._task_id}的待定消息失败: {e}\n{traceback.format_exc()}")
            return []

    # Windows 盘符路径模式（D:/... 或 D:\...）：平台未知时据此判定大小写折叠
    _WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:")

    @staticmethod
    def _workdir_compare_key(workdir: str, platform: str = "") -> str:
        """将 workdir 规范化为可比较键（用于 nest+workdir 冲突检测）

        处理同一目录的多种字符串写法（事务#1920：#1873 的 'D:\\work\\fcc'
        与 #1919 的 'D:/work/fcc' 严格相等比较漏检）：
        - 斜杠方向统一（Windows 反斜杠 ↔ 正斜杠）
        - normpath：合并重复斜杠、解析 . / ..、去尾部斜杠
        - 大小写折叠：Windows/macOS 文件系统不区分大小写；Linux 区分不折叠；
          platform 未知时按盘符路径（^[A-Za-z]:）判定 Windows 风格折叠

        Args:
            workdir: 事务的 workdir 原始字符串
            platform: nest 平台（"Windows"/"Linux"/"Darwin"等，空为未知）

        Returns:
            规范化后的比较键字符串
        """
        w = (workdir or "").strip().replace("\\", "/")
        w = posixpath.normpath(w)
        plat = (platform or "").strip().lower()
        case_insensitive = (
            plat.startswith("win") or plat.startswith("darwin") or plat.startswith("mac")
        )
        if case_insensitive or ToolExecutionEngine._WIN_DRIVE_RE.match(w):
            w = w.casefold()
        return w

    def _check_workdir_conflicts(self):
        """检测并通知 nest+workdir 冲突的运行中事务

        在事务 start/restart/resume 时调用。
        查找与本事务 nest 和 workdir 都相同的其他**根**运行中事务，
        对每个冲突事务注入 info 提示；本事务自身的警告保存到
        self._workdir_conflict_warnings，由迭代循环内 _get_task_info
        中已验证安全的 history.add_user 点注入（不打断当前thinking/操作，
        由迭代自然处理）。

        冲突判定：nest 非空 且 workdir 非空，nest 大小写不敏感相同，
        且 workdir 规范化后相同（_workdir_compare_key：斜杠方向/尾斜杠/
        . / .. / 大小写按 nest 平台折叠，事务#1920）。
        排除子事务（有 parent_task_id 的），因为子事务的文件操作受父事务调度，
        与外部根事务的冲突检测意义不大，反而会造成噪音。
        """
        try:
            task = self._task
            if not task:
                return

            my_nest = (task.get("nest") or "").strip()
            my_workdir = (task.get("workdir") or "").strip()
            # nest 或 workdir 为空时不检测（都未指定不构成实际冲突）
            if not my_nest or not my_workdir:
                return

            my_task_id = self._task_id

            # 事务总管(#2)不参与冲突检测：总管只监控调度、不修改文件，
            # 与其他事务共享 nest+workdir 是正常设计，冲突警告对其纯属噪音
            if my_task_id == 2:
                return

            # 获取所有运行中事务
            running_tasks = self.task_manager.db.get_running_tasks()

            # 本事务的比较键：nest 大小写不敏感（防配置写法差异漏检），
            # workdir 按 nest 平台规范化（斜杠方向/尾斜杠/./../大小写，
            # 事务#1920：同一目录 'D:\\work\\fcc' vs 'D:/work/fcc' 漏检修复）
            my_platform = self._get_nest_platform(my_nest)
            my_nest_key = my_nest.casefold()
            my_workdir_key = self._workdir_compare_key(my_workdir, my_platform)

            # 过滤出 nest 和 workdir 都相同的冲突事务（排除自身和子事务）
            conflict_tasks = []
            for t in running_tasks:
                if t.get("id") == my_task_id:
                    continue
                # 排除子事务：子事务的文件操作受父事务调度，不单独参与冲突检测
                if t.get("parent_task_id"):
                    continue
                # 排除事务总管#2：总管只监控调度、不修改文件，永不构成实际冲突
                if t.get("id") == 2:
                    continue
                t_nest = (t.get("nest") or "").strip()
                t_workdir = (t.get("workdir") or "").strip()
                if not t_nest or not t_workdir:
                    continue
                # nest 相同（大小写不敏感）才比较 workdir（同一 nest 平台语义一致）
                if t_nest.casefold() != my_nest_key:
                    continue
                # workdir 规范化键比较（同一目录不同写法视为冲突）
                if self._workdir_compare_key(t_workdir, my_platform) == my_workdir_key:
                    conflict_tasks.append(t)

            if not conflict_tasks:
                return

            # 构建冲突信息
            conflict_ids = [str(t["id"]) for t in conflict_tasks]
            conflict_list = ", ".join(f"#{tid}" for tid in conflict_ids)

            logger.info(
                f"[ToolEngine] 事务#{my_task_id} 检测到 nest+workdir 冲突: "
                f"nest={my_nest}, workdir={my_workdir}, 冲突事务: {conflict_list}"
            )

            # 本事务的冲突警告：保存到列表，由迭代循环内 _get_task_info 的安全注入点
            # 以 history.add_user 注入（不通过info注入打断当前thinking/操作，由迭代自然处理；
            # 允许重复添加以便 resume 时再次提醒）
            self_info = (
                f"⚠️ 冲突警告：检测到以下运行中的事务与本事务使用相同的 nest({my_nest}) 和 workdir({my_workdir})："
                f"事务{conflict_list}。"
                f"请特别注意避免编辑同一文件，避免使用git reset/checkout/revert等危险操作，多用git diff核对变更，切勿覆盖对方有效代码！必要时用info工具沟通！"
            )
            self._workdir_conflict_warnings.append(f"\n{self_info}\n")

            # 向每个冲突事务注入 info
            for t in conflict_tasks:
                tid = t["id"]
                other_info = (
                    f"⚠️ 冲突警告：检测到新启动的事务#{my_task_id} 与本事务使用相同的 nest({my_nest}) 和 workdir({my_workdir})。"
                    f"请特别注意避免编辑同一文件，避免使用git reset/checkout/revert等危险操作，多用git diff核对变更，切勿覆盖对方有效代码！必要时用task工具info子命令沟通！"
                )
                try:
                    # 事务#2089：记录发送者，接收方知道是哪个事务的冲突警告
                    self.task_manager.db.add_task_info(
                        task_id=tid,
                        info_content=other_info,
                        sender=f"事务#{my_task_id}",
                        dedup=True,
                    )
                except Exception as e:
                    logger.debug(f"[ToolEngine] 向冲突事务#{tid} 注入info失败: {e}")

        except Exception as e:
            logger.warning(f"[ToolEngine] 事务#{self._task_id} 检测workdir冲突失败: {e}")

    def _get_task_full_context(self, history):
        text = ""
        task = self.task_manager.get_task(self._task_id)
        workdir = task.get("workdir")
        if not workdir:
            workdir = get_working_directory()
        # 只有根事务才需获取消息完整上下文
        if not task.get("parent_task_id"):
            message_id = self._message_id
            # 获取消息链头部
            chain_head = self.message_manager.get_chain_head(message_id)
            if chain_head:
                chain_head_id = chain_head.get("id", message_id)
                # 构建消息链上下文
                text = self.message_manager.db.build_context_history(chain_head_id, False)
                if text:
                    # 添加消息树到历史
                    text = f"[所有关联消息如下：]\n{text}\n\n"
            tree = self.task_manager.db.get_tree_text(self._task_id, content_max_len=8192)
            text = f"\n{text}\n本事务默认工作目录：{workdir}\n=============\n本事务#{self._task_id} 需求描述：{tree}\n=============\n"
        else:
            content = task.get("content") or ""
            text = f"\n{text}\n本事务默认工作目录：{workdir}\n=============\n本事务#{self._task_id} 需求描述：{content}\n=============\n"
        history.add_user(text)

    def _get_unread_reply(self, history):
        message_id = self._message_id
        task_id = self._task_id
        # 在self.history中添加message_id消息的消息树和最新回复
        self._processing_messages = []
        # 只有根事务负责检查新消息
        if not self.task_manager.get_parent_id(task_id):
            try:
                # 新消息检测仅用于用户通知，实际注入在tool_engine的下一轮LLM调用前处理
                self._processing_messages = self._get_processing_messages(message_id)
                for reply in self._processing_messages:
                    # 单独添加最新回复到历史
                    sender = reply.sender
                    content = reply.content
                    history.add_user(
                        f"\n待处理消息#{reply.id} [{reply.timestamp}]   {sender}：{content}\n"
                    )
                    get_output_manager().write_output(
                        task_id=task_id,
                        source="task",
                        content=f"📜 {ANSI_COLORS.get('gray')} 待处理消息#{reply.id} [{reply.timestamp}] {sender} ：{content}{ANSI_COLORS.get('reset')}\n",
                    )
                # 标记为已读
                for reply in self._processing_messages:
                    reply_id = reply.id
                    self.message_manager.db.update_message_status(reply_id, "done")
                self._processing_messages = []
            except Exception as e:
                logger.warning(
                    f"[ToolEngine] 添加最新回复到历史失败: {e}\n{traceback.format_exc()}"
                )
                self._processing_messages = []

    def _get_task_info(self, history, all=False):
        # === Info字段处理机制：检查事务Info字段并注入 ===
        # 只使用未读info，使用后标记为已读（不删除）
        # 通过 TaskDatabase 的 info 管理函数操作，确保与 bin/task info 命令格式一致

        db = self.task_manager.db
        task = db.get_task(self._task_id)
        task_nest = self._task.get("nest") if self._task else None
        task_workdir = self._task.get("workdir") if self._task else None

        if task_nest and task_workdir:
            # 只在首次注入或 nest/workdir 变化时才注入，避免每次迭代重复
            if task_nest != self._last_injected_nest or task_workdir != self._last_injected_workdir:
                # 获取 nest 平台信息（Windows/Linux 等）
                nest_platform = self._get_nest_platform(task_nest)
                platform_str = f"，平台：{nest_platform}" if nest_platform else ""
                history.add_user(
                    f"\n本事务#{self._task_id}工作nest：{task_nest}，工作目录：{task_workdir}{platform_str}\n"
                )
                self._last_injected_nest = task_nest
                self._last_injected_workdir = task_workdir

        # nest+workdir 冲突警告注入：此处位于迭代循环内（mark_iteration_start 之后），
        # history.add_user 的 merge 安全，由迭代自然处理，不打断当前thinking/操作；
        # 注入后清空，保证每次 run_stream 仅注入一次（resume 时重新检测可再次注入）
        if self._workdir_conflict_warnings:
            for warning in self._workdir_conflict_warnings:
                history.add_user(warning)
                get_output_manager().write_output(
                    task_id=self._task_id,
                    source="task",
                    content=f"{ANSI_COLORS.get('bright_red')}{warning.strip()}{ANSI_COLORS.get('reset')}\n",
                )
            self._workdir_conflict_warnings.clear()

        # 获取未读或全部 info 列表
        mode = "all" if all else "unread"
        info_items = db.get_task_info(self._task_id, mode=mode)
        if not info_items:
            return

        # 记住要标记为已读的内容（当前未读项）
        items_to_mark = list(info_items)

        # 提取内容并去重（保留顺序，去除完全相同内容的重复项）
        seen = set()
        deduped_items = []
        for item in items_to_mark:
            content = item["content"].replace("\\n", "\n")
            if content not in seen:
                seen.add(content)
                deduped_items.append(item)

        # 限制注入的最大info数量，只保留最新的条目
        MAX_INFO_INJECT = 10
        if len(deduped_items) > MAX_INFO_INJECT:
            logger.warning(
                f"[ToolEngine] 事务#{self._task_id}有{len(deduped_items)}条未读info，"
                f"仅注入最新{MAX_INFO_INJECT}条"
            )
            deduped_items = deduped_items[-MAX_INFO_INJECT:]

        # 提取内容（事务#2089：带发送者标识 [来自 事务#N/风主🍃]，旧数据无sender不显示）
        # 注意：紧急检测（is_urgent_content）必须用原始内容，前缀会破坏首字符判断
        raw_contents = [item["content"].replace("\\n", "\n") for item in deduped_items]
        contents = []
        for item, raw in zip(deduped_items, raw_contents):
            sender = item.get("sender")
            contents.append(f"[来自 {sender}] {raw}" if sender else raw)
        info_text = "\n".join(contents)

        # 标记**本次读到的未读info**为已读（事务#2064：精确按项标记，
        # 不再用 mode="all"——消费瞬间用户/其他进程新注入的info（含感叹号
        # 紧急指示）会被 all 误标已读导致内容丢失且紧急中断检测永远查不到）
        success = db.mark_info_items_read(self._task_id, items_to_mark, read=True)
        if not success:
            logger.error(
                f"❌[ToolEngine] 严重错误！！！info已读状态更新错误 task_id={self._task_id}"
            )
        else:
            # 同步更新缓存，避免下次调用时从旧缓存读取到未读info导致重复添加
            updated_info_list = db.get_task_info(self._task_id, mode="all")
            from peng.task.info import serialize_info_list

            self._task["info"] = serialize_info_list(updated_info_list)

        if info_text:
            # 🔧 事务#2064：紧急info（感叹号开头）注入用强制性文案——
            # 实测#2077事故：普通"最新指示"文案被模型当成背景噪声无视，
            # 打断4次仍继续死循环（同一脚本写到第31版）。紧急指示必须
            # 以最高优先级措辞注入，明确要求停止当前行为模式。
            # 事务#2089：紧急检测用原始内容（无[来自]前缀），指示人用真实发送者
            _has_urgent = any(is_urgent_content(c) for c in raw_contents)
            if _has_urgent:
                # 紧急指示人：优先取紧急项的发送者（事务#N/风主🍃），旧数据回退master名
                urgent_sender = next(
                    (
                        item.get("sender")
                        for item, raw in zip(deduped_items, raw_contents)
                        if is_urgent_content(raw) and item.get("sender")
                    ),
                    get_master_name(),
                )
                history.add_user(
                    f"\n⛔⛔⛔ 紧急指示（最高优先级）⛔⛔⛔\n"
                    f"{info_text}\n\n"
                    f"你正在执行的操作已被强制打断。上述指示以感叹号标记=最高优先级"
                    f"（来源见各条[来自]前缀），"
                    f"你之前的行为方向已被{urgent_sender}否定！"
                    f"立刻停止当前重复性/同类操作，认真理解上述指示，"
                    f"从根本上改变处理方向后再行动。继续原模式=无视指示。\n"
                )
            else:
                history.add_user(f"\n关于本事务#{self._task_id}的最新指示：{info_text}\n")
        logger.debug(
            f"[ToolEngine] Info字段处理: {len(items_to_mark)}个未读info（去重后{len(deduped_items)}个）已标记为已读"
        )
        get_output_manager().write_output(
            task_id=self._task_id,
            source="task",
            # 事务#2089续：来源由各条[来自]前缀表达（风主反馈"风主🍃 ：[来自 风主🍃]"重复）；
            # 无sender的系统消息不显示来源（此前误标为风主）
            content=f"🏷️  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {ANSI_COLORS.get('bold')}{ANSI_COLORS.get('bright_green')}{info_text}{ANSI_COLORS.get('reset')}\n",
        )

    def _get_nest_platform(self, nest_name: str) -> str:
        """获取指定 nest 的平台信息（Windows/Linux 等）

        使用公共函数 get_nest_platform()（两级查询）：
        1. NestBridgeManager（主服务进程场景）
        2. webui HTTP API（事务子进程场景，NestBridgeManager 无连接）

        Args:
            nest_name: nest 的 display_key

        Returns:
            平台信息字符串（如 "Windows"、"Linux"），获取失败返回空字符串
        """
        try:
            from peng.tools._nest_helper import get_nest_platform

            return get_nest_platform(nest_name)
        except Exception as e:
            logger.debug(f"[ToolEngine] 获取nest平台信息失败: {e}")
            return ""

    def _is_multimodal_provider(self) -> bool:
        """当前 provider 是否具备多模态能力（vision=true/yes/on/enabled/only）

        优先从 self.llm.provider_config 读取 vision 字段（chat_stream 每次
        选取 provider 后更新，provider 切换时自动跟随）。
        首次迭代 provider_config 尚未设置时，通过 chat_stream 相同的选择逻辑
        （get_provider + prefer_vision）预判将要使用的 provider，确保第一轮
        LLM 调用前就能完成多模态注入（否则模型看不到图片会先调read工具）。
        """
        provider_config = getattr(self.llm, "provider_config", None)
        if not provider_config:
            # 预判provider：与 chat_stream 保持一致的选择逻辑
            try:
                task = self._task or {}
                content = task.get("content") or ""
                cfg = load_config().llm
                fetch_result = cfg.fetch_providers(content) or cfg.fetch_providers(
                    self._history.get_system()
                )
                favorites = fetch_result[0] if fetch_result else None
                prefer_vision = False
                if content:
                    from peng.llm.multimodal import extract_file_refs

                    prefer_vision = bool(extract_file_refs(content))
                # 未读info也参与检测（与chat_stream一致）
                info_raw = task.get("info") or "[]"
                try:
                    info_list = json.loads(info_raw) if isinstance(info_raw, str) else info_raw
                    if isinstance(info_list, list):
                        unread = "\n".join(
                            i.get("content", "")
                            for i in info_list
                            if isinstance(i, dict) and not i.get("read")
                        )
                        if unread:
                            prefer_vision = prefer_vision or bool(extract_file_refs(unread))
                except Exception:
                    pass
                provider_data = cfg.get_provider(
                    current=getattr(self.llm, "provider", None),
                    favorites=favorites,
                    prefer_vision=prefer_vision,
                )
                if provider_data:
                    provider_config = provider_data
            except Exception as e:
                logger.debug(f"[ToolEngine] 预判provider失败(忽略): {e}")
        vision = (provider_config or {}).get("vision", "disabled")
        return str(vision).lower() in ("true", "yes", "on", "enabled", "only")

    def _has_vision_provider_available(self) -> bool:
        """配置中是否存在启用的、可承担普通LLM任务的vision provider

        read等工具返回多模态附件时，若当前provider不支持vision但存在可用的
        vision provider，仍将附件注入history（由chat_stream的prefer_vision
        选择逻辑切换到vision provider发送）；若完全没有vision provider则
        丢弃附件避免history堆积大体积base64。
        """
        try:
            cfg = load_config().llm
            from peng.config.loader import is_provider_vision_runtime_disabled

            for _name, p in cfg.providers.items():
                if (
                    getattr(p, "enable", False)
                    and p.is_vision_enabled()
                    and not p.is_vision_only()
                    and not is_provider_vision_runtime_disabled(_name)
                ):
                    return True
        except Exception as e:
            logger.debug(f"[ToolEngine] 检查vision provider可用性失败(忽略): {e}")
        return False

    async def _inject_multimodal_files(self, history: MessageHistory) -> None:
        """多模态注入：将本轮新增 user 消息中引用的多模态文件转为 content part

        仅当当前 provider 具备多模态能力时执行。扫描本轮迭代新增的 user
        消息（自上次注入以来的文本内容），提取 "@file=<path>" 和 webui
        "[附件]" 引用，按扩展名识别类型（image/video/audio/pdf），读取文件
        构建 OpenAI 兼容 content part，替换该消息的 content 为多模态列表。

        已注入的消息（content 为 list）跳过，避免重复加载。
        加载失败时保留纯文本并追加错误提示。
        """
        if not self._is_multimodal_provider():
            return

        from peng.llm.multimodal import extract_file_refs, load_multimodal_parts

        # 本轮新增 user 消息：从上次注入游标开始扫描
        msgs = history._messages
        start = getattr(self, "_mm_scan_cursor", 0)
        if start > len(msgs):
            start = len(msgs)
        task = self._task or {}
        workdir = task.get("workdir") or get_working_directory()
        nest_name = task.get("nest")

        cursor = start
        for i in range(start, len(msgs)):
            msg = msgs[i]
            cursor = i + 1
            if msg.get("role") != "user" or not isinstance(msg.get("content"), str):
                continue
            content = msg["content"]
            refs = extract_file_refs(content)
            if not refs:
                continue

            parts: list[dict] = [
                {
                    "type": "text",
                    "text": (
                        content
                        + "\n[系统提示：上方/下方附加的多模态文件（图片/视频/音频/PDF）已直接注入本消息，"
                        "请直接查看分析其中的内容。]"
                    ),
                }
            ]
            notes: list[str] = []
            loaded = 0
            for ref in refs:
                try:
                    abs_path, part, status = await load_multimodal_parts(
                        ref, workdir=workdir, nest_name=nest_name
                    )
                except Exception as e:
                    logger.warning(f"[ToolEngine] 多模态文件加载异常 {ref}: {e}")
                    notes.append(f"@file={ref} 加载异常: {e}")
                    continue
                if part is not None:
                    parts.append(part)
                    loaded += 1
                    logger.info(
                        f"[ToolEngine] 事务#{self._task_id} 多模态注入: {abs_path} ({status})"
                    )
                else:
                    reason = {
                        "not_found": "文件不存在",
                        "unsupported": "非多模态文件类型",
                        "too_large": "文件超过大小限制",
                        "error": "读取失败",
                        # 🔧 事务#1959：0字节/损坏文件明确反馈（生成未完成的截图等）
                        "empty": "文件为空(0字节)，可能生成/截图未完成",
                        "invalid": "文件数据无效（损坏/过小/格式校验失败），请重新生成",
                    }.get(status, status)
                    notes.append(f"@file={ref} 无法注入: {reason}")

            if loaded:
                if notes:
                    parts[0]["text"] = content + "\n[多模态注入提示]\n" + "\n".join(notes)
                msg["content"] = parts
                get_output_manager().write_output(
                    task_id=self._task_id,
                    source="task",
                    content=f"🖼️ [多模态] 已注入 {loaded} 个多模态文件到上下文\n",
                )
            elif notes:
                # 全部失败：保留文本并追加提示（模型可自行用read工具处理）
                msg["content"] = content + "\n[多模态注入提示]\n" + "\n".join(notes)

        self._mm_scan_cursor = cursor

    def _engine_restart_with_breaker(self, task_id: int, detail: str) -> bool:
        """🔧 事务#2084 Fix3：引擎自触发restart死循环熔断。

        背景：事务#2080因服务端推理引擎未就绪（返回context(0)被误判为上下文
        超标），引擎反复插入restart控制——每次restart清空历史重新请求，服务端
        问题依旧，20分钟内死循环93次，烧钱且刷屏。restart只适用于"上下文
        确实装不下"的自愈场景；若连续多次restart后仍走到本处，说明根因在
        服务端/配置层面，restart永远解决不了，必须停下等待恢复或人工介入。

        熔断规则：15分钟窗口内该事务已有≥2条restart控制记录时，本次不再
        restart，改为插入pause控制并输出明确诊断。服务恢复/人工检查后
        resume即可继续（历史未破坏）。

        Args:
            task_id: 事务ID
            detail: 触发restart的原因描述（写入诊断信息）

        Returns:
            True: 已插入restart控制（事务将被重启）
            False: 已熔断改为pause（事务将被暂停）
        """
        _recent = 0
        try:
            _recent = int(
                self.task_manager.db.count_recent_task_controls(task_id, "restart", minutes=15)
            )
        except Exception as _cnt_err:
            logger.warning(
                f"[ToolEngine] 统计事务#{task_id}近期restart次数失败(按0处理): {_cnt_err}"
            )
        if _recent >= 2:
            _bk_msg = (
                f"⛔  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]"
                f"{ANSI_COLORS.get('bright_red')}"
                f"事务#{task_id}触发restart熔断：15分钟内已是第{_recent + 1}次restart"
                f"仍失败（{detail}）。继续restart只会清空历史死循环，已自动暂停。"
                f"疑似服务端异常（如推理引擎未就绪/配置错误），请检查provider服务状态，"
                f"恢复后resume即可继续。"
                f"{ANSI_COLORS.get('reset')}\n"
            )
            logger.error(f"⛔ [ToolEngine] 事务#{task_id} {_bk_msg}")
            get_output_manager().write_output(task_id=task_id, content=_bk_msg, source="system")
            try:
                self.task_manager.db.insert_task_control(task_id, "pause")
            except Exception as _ins_err:
                logger.error(f"[ToolEngine] 熔断插入pause控制失败: {_ins_err}")
            return False
        try:
            self.task_manager.db.insert_task_control(task_id, "restart")
        except Exception as _ins_err:
            logger.error(f"[ToolEngine] 插入restart控制失败: {_ins_err}")
        return True

    def _provider_preserve_thinking(self) -> bool:
        """读取当前 provider 的 preserve_thinking 配置（事务#2111）

        provider_config 由 refresh_provider_vars/chat_stream 在选中 provider 后
        同步；缺失时（尚未连接任何 provider）返回 False（默认剥离思考内容省token）。
        """
        cfg = getattr(self.llm, "provider_config", None)
        return bool(cfg.get("preserve_thinking", False)) if isinstance(cfg, dict) else False

    # ==================== 上下文长度优化（事务#2114） ====================

    def _get_drop_hint_threshold(self) -> int:
        """当前provider的drop清理提示阈值（字节，事务#2114）

        优先级：provider级 drop_hint_threshold（peng.json llm.providers.<name>）
        > 全局 llm.drop_hint_threshold（默认1024）。<=0 表示禁用。
        """
        cfg = getattr(self.llm, "provider_config", None)
        if isinstance(cfg, dict) and cfg.get("drop_hint_threshold") is not None:
            try:
                return int(cfg["drop_hint_threshold"])
            except (TypeError, ValueError):
                pass
        try:
            return int(getattr(self.llm.config, "drop_hint_threshold", 1024))
        except (TypeError, ValueError):
            return 1024

    def _allocate_drop_id(self) -> str:
        """分配唯一的4位数字ID（1000-9999，事务#2114）

        顺序递增并跳过已用ID；全部用尽时清空已用集重新分配（相隔9000次
        分配的同ID提示不可能同处一个drop窗口，无跨迭代误清理风险）。
        """
        for _ in range(9000):
            self._drop_id_seq += 1
            if self._drop_id_seq > 9999:
                self._drop_id_seq = 1000
            if self._drop_id_seq not in self._used_drop_ids:
                self._used_drop_ids.add(self._drop_id_seq)
                return f"{self._drop_id_seq:04d}"
        # 9000个ID全部在用（理论不可能）：重置已用集重新分配
        self._used_drop_ids = {self._drop_id_seq}
        return f"{self._drop_id_seq:04d}"

    def _append_drop_hint(self, history_content):
        """超长工具结果末尾追加drop清理提示（事务#2114）

        结果字节数超过阈值（provider级可配，默认1024）时追加带唯一4位数字ID的
        清理提示，模型确认结果毫无用处时可调用drop工具清理以节省上下文空间。

        - 多模态列表content：按text part字节数判断并追加到首个text part
          （附件part不计入——附件发送一次后即剥离，非持久上下文负担）
        - 任何异常都静默返回原内容，绝不影响工具结果入history

        Args:
            history_content: 即将进入history的工具结果（str或多模态content part列表）

        Returns:
            追加提示后的内容（未超阈值或禁用时原样返回）
        """
        threshold = self._get_drop_hint_threshold()
        if threshold <= 0:
            return history_content
        try:
            if isinstance(history_content, str):
                if len(history_content.encode("utf-8")) <= threshold:
                    return history_content
                hint = self._prompts.drop_hint.format(drop_id=self._allocate_drop_id())
                return history_content + hint
            if isinstance(history_content, list):
                text_parts = [
                    p for p in history_content if isinstance(p, dict) and p.get("type") == "text"
                ]
                size = sum(len(str(p.get("text", "")).encode("utf-8")) for p in text_parts)
                if size <= threshold or not text_parts:
                    return history_content
                hint = self._prompts.drop_hint.format(drop_id=self._allocate_drop_id())
                text_parts[0]["text"] = str(text_parts[0].get("text", "")) + hint
                return history_content
        except Exception as e:
            logger.debug(f"[ToolEngine] 追加drop清理提示失败(忽略): {e}")
        return history_content

    def _remind_thinking_only(
        self, history: MessageHistory, thinking: str, content: str, tool_calls: list
    ) -> bool:
        """思考后无正式内容提醒（事务#2114）

        模型只输出了思考内容（无content无tool_calls）时，思考成果不会保留在
        后续上下文中（to_api_format默认剥离reasoning_content），注入user提醒
        模型补输出正式内容总结，避免思考成果丢失。

        注意：有tool_calls时是正常工具迭代（思考已转化为行动），不提醒；
        且此时注入user消息会破坏 assistant(tool_calls)→tool 的API格式。

        Returns:
            是否注入了提醒
        """
        if thinking and thinking.strip() and not content.strip() and not tool_calls:
            history.add_user(self._prompts.thinking_only_reminder)
            return True
        return False

    def _save_history(self, history):
        try:
            context = json.dumps(
                history.to_context(
                    thinking_type=self.llm.thinking_type,
                    preserve_thinking=self._provider_preserve_thinking(),
                ),
                ensure_ascii=False,
                indent=2,
            )
            if self.task_manager.db.update_task(self._task_id, context=context):
                logger.debug(f"🗃️ ♻️ [ToolEngine] {self._task_id} 保存历史上下文成功。")
                return True
            else:
                logger.debug(f"❌ ♻️ [ToolEngine] {self._task_id} 保存历史上下文失败。")
                return False
        except Exception as e:
            logger.debug(f"❌ ♻️ [ToolEngine] {self._task_id} 保存历史上下文失败。{e}")
            pass
        return False

    async def _create_iteration_backup(self, task_id: int, history: MessageHistory) -> None:
        """每轮迭代开始：创建统一备份快照并输出回滚超链接

        备份内容（复用 ToolBackupManager / HistoryBackupManager / ExternalFileBackup）：
        - 工作目录 git 快照 → 分支 peng_backups/task#id（git）或备份目录 git 仓库（非git）
        - history context + checklist → data/history/task#id
        - 外部文件基线 → data/file_backups/task#id HEAD

        回滚超链接写入 output_manager（xterm 显示），点击走 /api/tool-rollback。
        失败仅记录日志，不阻塞迭代。

        🔧 去重优化：计算指纹 sha256(history_context+NUL+checklist)，
        与上次成功备份的指纹相同时（如API受限重试期间、中断跳过迭代等
        导致上一轮迭代未产生任何history/checklist变化），跳过本次全部
        备份操作，避免冗余的git快照、history空commit和回滚链接输出。

        Args:
            task_id: 事务ID
            history: 消息历史对象
        """
        try:
            # 捕获当前 checklist（从 DB 读取，迭代开始时尚未有本批次变更）
            checklist = ""
            try:
                _task_bk = self.task_manager.db.get_task(task_id)
                checklist = (_task_bk.get("checklist", "") or "") if _task_bk else ""
            except Exception:
                pass

            history_context = json.dumps(
                history.to_context(
                    thinking_type=self.llm.thinking_type,
                    preserve_thinking=self._provider_preserve_thinking(),
                ),
                ensure_ascii=False,
            )

            # 指纹去重：history messages 与 checklist 均与上次成功备份相同时跳过。
            # 🔧 指纹只计算 messages+checklist（剥离 to_context 的统计字段：
            # _prompt_tokens/_total_* 等每次LLM调用必变，纳入指纹会导致空转
            # 轮次也判定为"有变化"从而产生冗余备份和回滚链接）。
            # checklist 纳入指纹：用户经webui修改checklist（无工具调用）时仍能重新备份。
            try:
                _ctx = json.loads(history_context)
                _fingerprint_source = (
                    json.dumps(
                        _ctx.get("messages", _ctx) if isinstance(_ctx, dict) else _ctx,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\x00"
                    + checklist
                )
            except Exception:
                _fingerprint_source = history_context + "\x00" + checklist
            fingerprint = hashlib.sha256(_fingerprint_source.encode("utf-8")).hexdigest()
            if self._last_backup_fingerprint == fingerprint:
                logger.debug(f"迭代备份跳过(task#{task_id}): history/checklist与上次备份相同")
                return

            task_nest = self._task.get("nest") if self._task else None
            task_workdir = self._task.get("workdir") if self._task else None
            if not task_workdir:
                task_workdir = str(get_working_directory())

            iteration = self._iteration + 1  # self._iteration 在工具执行后才自增

            # 🔧 事务#2087：迭代快照同样受总预算约束——大仓库 add -A 分钟级
            # 耗时会卡住整轮迭代（LLM 调用前静默）。超预算跳过本轮快照
            # （to_thread 线程后台自然完成），无回滚链接为安全降级。
            from peng.config.loader import get_snapshot_budget_seconds

            _iter_budget = get_snapshot_budget_seconds()
            try:
                if _iter_budget > 0:
                    meta = await asyncio.wait_for(
                        asyncio.to_thread(
                            ToolBackupManager.create_iteration_snapshot,
                            task_id=task_id,
                            history_context=history_context,
                            checklist=checklist,
                            iteration=iteration,
                            workdir=task_workdir,
                            nest_name=task_nest,
                        ),
                        timeout=_iter_budget,
                    )
                else:
                    meta = await asyncio.to_thread(
                        ToolBackupManager.create_iteration_snapshot,
                        task_id=task_id,
                        history_context=history_context,
                        checklist=checklist,
                        iteration=iteration,
                        workdir=task_workdir,
                        nest_name=task_nest,
                    )
            except asyncio.TimeoutError:
                logger.warning(
                    f"迭代快照超预算{_iter_budget}s(task#{task_id}, iter#{iteration})，跳过本轮快照"
                )
                _iter_warn = (
                    f"⚠️ 迭代备份快照超过 {_iter_budget}s 预算已跳过"
                    f"（本轮无回滚点，上一轮快照仍可用）。\n"
                )
                get_output_manager().write_output(
                    task_id=task_id,
                    content=f"{ANSI_COLORS.get('bright_yellow')}{_iter_warn}{ANSI_COLORS.get('reset')}",
                    source="system",
                )
                return
            if not meta:
                logger.debug(f"迭代备份未生成(task#{task_id}, iter#{iteration})")
                return

            # 备份成功：更新指纹基线（全部失败时不更新，下一轮自动重试）
            self._last_backup_fingerprint = fingerprint

            display = ToolBackupManager.generate_iteration_backup_display(meta)
            if display:
                get_output_manager().write_output(
                    task_id=task_id,
                    content=f"{ANSI_COLORS.get('bright_cyan', '')}{display}{ANSI_COLORS.get('reset', '')}\n",
                    source="system",
                )
        except Exception as e:
            logger.debug(f"迭代备份失败(task#{task_id}): {e}", exc_info=True)

    def _count_output_lines(self, task_id: int) -> int:
        """统计事务输出文件当前总行数"""
        try:
            file_path = get_output_manager().get_output_file_path(task_id)
            with open(file_path, "rb") as f:
                return sum(1 for _ in f)
        except FileNotFoundError:
            return 0
        except Exception as e:
            logger.warning(f"[ToolEngine] 事务#{task_id} 统计输出文件行数失败: {e}")
            return 0

    def _find_thinking_lines(self, task_id: int) -> tuple[int, int]:
        """扫描输出文件游标之后的行，返回thinking内容行号范围（1-based闭区间）

        输出文件为TSV格式：{timestamp}\\t{source}\\t{task_id}\\t{content}，
        只认 source=thinking 的行，避免行缓冲时序误差把 system/answer 等
        非思考内容行误算进范围。

        Returns:
            (start_line, end_line)；范围内无thinking行时返回 (0, 0)
        """
        try:
            file_path = get_output_manager().get_output_file_path(task_id)
            with open(file_path, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return (0, 0)
        except Exception as e:
            logger.warning(f"[ToolEngine] 事务#{task_id} 扫描thinking行失败: {e}")
            return (0, 0)
        start_line, end_line = 0, 0
        for i in range(self._thinking_scan_line, len(lines)):
            parts = lines[i].split("\t", 2)
            if len(parts) >= 2 and parts[1] == "thinking":
                if not start_line:
                    start_line = i + 1
                end_line = i + 1
        return (start_line, end_line)

    def _remind_thinking_in_output(self, history: MessageHistory) -> None:
        """提醒模型上次思考内容在事务输出文件中的行号范围（用于不支持thinking持久化的模型）

        对于 thinking_persistence=False 的模型（如GLM系列），模型不会复用 history 中的
        reasoning_content，每次都重新思考，导致重复思考浪费 token。
        思考内容已经由流式输出链路（ThinkingEvent→executor.write_output source=thinking）
        实时写入事务输出文件（task-{id}.out）。
        此处直接扫描文件中的 thinking 标记行得到精确行号范围：
        扫描不到 thinking 行（本迭代实际没有思考内容）则不提醒。

        Args:
            history: 消息历史对象（用于 add_user 通知模型）
        """
        return  # 暂时禁用
        if not self._task_id:
            return
        try:
            start_line, end_line = self._find_thinking_lines(self._task_id)
            if not start_line or end_line < start_line:
                return
            # 消费后推进游标到当前文件末尾，避免下轮重复扫描已回顾的内容
            self._thinking_scan_line = self._count_output_lines(self._task_id)
            history.add_user(
                f"上次思考内容已记录在事务输出文件第{start_line}-{end_line}行，"
                f"可用 task 工具的 output 子命令回顾：task output {self._task_id} "
                f"--begin {start_line} --end {end_line}，"
                "切勿重复思考相同内容，否则将被投诉欺诈token费用！"
            )
            logger.debug(
                f"[ToolEngine] 事务#{self._task_id} 已提醒思考内容行号范围 {start_line}-{end_line}"
            )
        except Exception as e:
            logger.warning(f"[ToolEngine] 事务#{self._task_id} 注入思考内容回顾提醒失败: {e}")

    def _restore_history(self, history: MessageHistory):
        task = self.task_manager.get_task(self._task_id)
        context = task.get("context")
        if context:
            # 将消息列表恢复为 MessageHistory 对象
            if history.from_str(context):
                logger.debug(f"✅ ♻️ [ToolEngine] {self._task_id} 加载历史上下文成功。")
                return True
        return False

    @staticmethod
    def _parse_compact_override(text: str) -> int | None:
        """从文本中解析 @max=<number>[K|M] 指令

        支持格式：
        - @max=128K   → 128 * 1024 = 131072
        - @max=1M     → 1 * 1048576 = 1048576
        - @max=300000 → 300000（无后缀=原值）

        如果有多个匹配，取最后一个（最新的）。

        Returns:
            解析出的 compact 阈值，无匹配则返回 None
        """
        import re

        matches = re.findall(r"@compact\s*=\s*(\d+(?:\.\d+)?)\s*([KMkm]?)", text)
        if not matches:
            return None
        value_str, suffix = matches[-1]  # 取最后一个匹配
        value = float(value_str)
        suffix = suffix.upper()
        if suffix == "K":
            value *= 1024
        elif suffix == "M":
            value *= 1024 * 1024
        return int(value)

    @staticmethod
    def _parse_think_override(text: str) -> str | None:
        """从文本中解析 @think=<value> 指令（临时改变当前事务思考模式/推理强度）

        支持格式（不区分大小写）：
        - @think=no / off / disable / disabled → 禁用思考（"disabled"）
        - @think=yes / on / enable / enabled   → 开启思考（"enabled"）
        - @think=auto                          → 模型自动决定（"auto"）
        - @think=low / medium / high / xhigh / max → 开启思考并设置对应推理档位

        如果有多个匹配，取最后一个（最新的）。

        Returns:
            解析出的思考模式，无匹配则返回 None
        """
        import re

        matches = re.findall(r"@think\s*=\s*([A-Za-z]+)", text, re.IGNORECASE)
        if not matches:
            return None
        value = matches[-1].lower()  # 取最后一个匹配
        mapping = {
            "no": "disabled",
            "off": "disabled",
            "disable": "disabled",
            "disabled": "disabled",
            "yes": "enabled",
            "on": "enabled",
            "enable": "enabled",
            "enabled": "enabled",
            "auto": "auto",
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": "xhigh",
            "max": "max",
        }
        result = mapping.get(value)
        if result is None:
            logger.warning(
                f"[ToolEngine] @think 指令值无效: '{value}'，合法值: {', '.join(mapping)}"
            )
        return result

    def _get_effective_thinking_mode(self) -> str | None:
        """获取 @think 指令的思考模式覆盖（与 @compact 覆盖机制一致）

        每轮迭代 LLM 调用前调用。查询事务最新 content 和所有 info（已读+未读）文本，
        如果包含 @think=<value> 则以此值临时覆盖 provider 的 thinking_mode。

        优先级：info 中最新的 > content 中的 > 无覆盖（None，走 provider 默认+自动干预）

        Returns:
            覆盖的思考模式（disabled/enabled/auto/low/medium/high/xhigh/max），
            无 @think 指令则返回 None
        """
        if not self._task_id:
            return None

        texts_to_check: list[str] = []

        # 1. 事务 content
        try:
            task = self.task_manager.get_task(self._task_id)
            if task:
                content = task.get("content") or ""
                if content:
                    texts_to_check.append(content)
        except Exception:
            pass

        # 2. 所有 info（已读 + 未读）
        try:
            info_items = self.task_manager.db.get_task_info(self._task_id, mode="all")
            if info_items:
                for item in info_items:
                    c = item.get("content", "")
                    if c:
                        texts_to_check.append(c)
        except Exception:
            pass

        # 从所有文本中查找 @think 指令，取最后一个匹配
        for text in reversed(texts_to_check):
            override = self._parse_think_override(text)
            if override is not None:
                logger.info(
                    f"[ToolEngine] 事务#{self._task_id} 检测到 @think 指令覆盖: "
                    f"{override} (provider默认: {self.llm.thinking_mode})"
                )
                return override

        return None

    def _get_effective_compact_threshold(self) -> int:
        """获取有效的 compact_threshold：检查 @compact 指令覆盖

        每轮迭代开始时调用。查询事务最新 content 和所有 info（已读+未读）文本，
        如果包含 @max=<number>[K|M] 则以此值替代 provider 的 compact_threshold。

        优先级：info 中最新的 > content 中的 > provider 默认值

        Returns:
            有效的 compact_threshold 值
        """
        default = self.llm.compact_threshold
        if not self._task_id:
            return default

        texts_to_check: list[str] = []

        # 1. 事务 content
        try:
            task = self.task_manager.get_task(self._task_id)
            if task:
                content = task.get("content") or ""
                if content:
                    texts_to_check.append(content)
        except Exception:
            pass

        # 2. 所有 info（已读 + 未读）
        try:
            info_items = self.task_manager.db.get_task_info(self._task_id, mode="all")
            if info_items:
                for item in info_items:
                    c = item.get("content", "")
                    if c:
                        texts_to_check.append(c)
        except Exception:
            pass

        # 从所有文本中查找 @compact 指令，取最后一个匹配
        for text in reversed(texts_to_check):
            override = self._parse_compact_override(text)
            if override is not None:
                logger.info(
                    f"[ToolEngine] 事务#{self._task_id} 检测到 @compact 指令覆盖: "
                    f"{override} (provider默认: {default})"
                )
                return override

        return default

    def check_progress_and_compact(self, threshold: int, history: MessageHistory):
        # 🔧 事务#2061：上下文紧张检测取「API真实值」与「估算值」的较大者。
        # 事务#2002曾从 get_token_count(estimate=False) 改为纯 estimate_tokens()
        # （JSON字符/3.5），但该估算对中文内容严重低估（GLM实测约2.6字符/token，
        # task#2060：真实393K token的上下文估算仅288K，低估27%），且估算不含每次
        # 请求都携带的tools schema，导致真实上下文远超阈值时估算值仍低于阈值，
        # "上下文紧张"提示永不触发。
        # _prompt_tokens 是当前provider最近一次成功响应报告的精确prompt token数
        # （含system prompt与tools schema，由update_usage每轮迭代后更新），常态下
        # 是权威值；仅provider切换后首轮短暂过期（旧provider计数，事务#2002要
        # 解决的场景），由estimate_tokens兜底。取max双向保证不漏检：真实值准而
        # 估算低时用真实值，切换期真实值过期时用估算。
        # 🔧 事务#2114：估算按当前provider的thinking_type/preserve_thinking对齐
        # 实际请求序列化语义（剥离思考时不计入思考内容），避免系统性高估
        current = max(
            history.get_token_count(estimate=False),
            history.estimate_tokens(
                thinking_type=self.llm.thinking_type,
                preserve_thinking=self._provider_preserve_thinking(),
            ),
        )
        task = self.task_manager.get_task(self._task_id)
        progress = task.get("progress", 0) or 0
        if current > threshold:
            # 🔧 事务#2061续：context_too_large是持续状态型告警（模型执行restart
            # 前每轮迭代都超标），绝不能走_should_report_system_error去重——该机制
            # 跳过时不追加签名，单一签名会永久卡在去重窗口内，一次注入后永久静默
            # （task#2077实测：14:22进程重启首轮注入后，上下文涨到560K+三小时零提示，
            # 模型从未被催促更新checklist重启）。改为迭代节流：每3轮注入一次持续
            # 催促，直至模型restart（上下文清空状态消失）或降回阈值下。
            if self._iteration - self._last_context_tight_iter >= 3:
                content = colorize(
                    self._prompts.context_too_large.format(
                        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        task_id=self._task_id,
                        current_kb=current // 1024,
                        threshold_kb=threshold // 1024,
                        progress=progress,
                    ),
                    get_colors().message("warning"),
                )
                history.add_user(content)
                get_output_manager().write_output(
                    task_id=self._task_id,
                    content=content,
                    source="system",
                )
                self._last_context_tight_iter = self._iteration

    async def run_stream(
        self,
        message_id: int | None = None,
        task_id: int | None = None,
        tools: list[dict] | None = None,
        hook_manager: HookManager | None = None,
        history: MessageHistory | None = None,
    ):
        """
        流式执行带工具支持的对话循环

        Args:
            messages: 消息列表
            tools: 工具定义列表
            history: 消息历史对象
        Yields:
            EngineEvent: 事件流
        """
        logger.debug(
            f"[ToolEngine] run_stream 被调用: message_id={message_id}, task_id={task_id}, tools={len(tools) if tools else 0} hook_manager={hook_manager} history={history} "
        )

        # 阶段9：保存message_id和task_id，用于暂停检测
        self._message_id = message_id
        self._task_id = task_id
        self._task = self.task_manager.get_task(task_id)
        self._iteration = 0
        # 🔧 事务#2061续：重置上下文紧张节流游标（run_stream重入/resume恢复时首轮即注入）
        self._last_context_tight_iter = -1_000_000
        self._consecutive_blocks = 0
        self._consecutive_empty = 0  # 连续空响应计数
        self._max_consecutive_empty = 2  # 连续空响应上限，超过则强制结束
        self._history = history
        self.compact_since_last_progress = 0
        self.last_progress = 0
        # Checklist调用检测初始化
        self._checklist_reminded_id = -1
        # 迭代备份去重指纹重置：run_stream 可能被多次调用（如resume恢复），
        # 每次运行开始重新建立备份基线，避免跨运行误判"无变化"
        self._last_backup_fingerprint = None
        # 死循环检测
        self._iteration_strings: list[str] = []
        self._consecutive_dead_loops: int = 0
        # 思考过载检测初始化
        self._thinking_overload_bytes: int = 0
        self._force_thinking_disabled: bool = False
        # 内容审核连续删除user消息计数（防止无限删除）
        self._content_filter_deletions: int = 0
        # 内容类错误类型描述（"内容审核拦截"/"内容解析失败..."），用于系统消息文案
        self._content_filter_kind: str = ""
        # 🔧 事务#2002：上下文长度超标连续compact轮次计数（LLM调用成功时重置）。
        # 每轮400上下文长度超标执行一次compact，连续3轮compact后仍超标说明
        # 上下文无法压缩到provider能力内，拒绝处理（重启事务），绝不破坏上下文。
        self._context_overflow_rounds: int = 0
        # 全provider失败等待重试的连续轮次计数（仅用于警告提示，不暂停事务）
        self._all_failed_rounds: int = 0
        # 🔧 事务#2235：网络瞬时错误(ReadError等)熔断计数——
        # _transient_rounds：内层5次重试耗尽的连续轮次（成功一次流即重置）。
        # content非空的断流在TransportError分支直接保留响应走正常收尾（不重试），
        # 到达耗尽分支的均为content为空的断流，连续3轮耗尽即熔断转长冷却等待。
        # 背景：#2235事故中is_transient_error耗尽后sleep(5)+continue无限死磕，
        # 单迭代30分钟959次ReadError，服务器每次完整输出后断流，响应全丢重烧
        self._transient_rounds: int = 0
        # 有tool_call的迭代计数（用于PUA注入间隔判断）
        self._tool_call_iter_count: int = 0
        # nest+workdir 冲突警告列表：_check_workdir_conflicts 检测并保存，
        # 由迭代循环内 _get_task_info 的 history.add_user 注入（不打断，迭代自然处理）
        self._workdir_conflict_warnings: list[str] = []
        # 跟踪上次注入的 nest/workdir，避免每次迭代重复注入
        self._last_injected_nest: str | None = None
        self._last_injected_workdir: str | None = None
        # 紧急中断（感叹号开头的消息/info）检测节流缓存（事务#1860）：
        # _has_urgent_interrupts 思考流每chunk调用，DB查询限频1秒；
        # 迭代边界消费消息/info后重置，避免过期缓存导致重复跳迭代
        self._last_urgent_check_ts: float = 0.0
        # 🔧 事务#2064：工具批次死循环检测——最近批次签名队列与连续重复计数
        # （#2077事故：同一脚本连写31版仅文件名递增，参数不同模式相同）
        self._tool_batch_sigs: list[str] = []
        self._tool_batch_sig_repeat: int = 0
        self._last_urgent_interrupt_reason: str | None = None
        # 多模态注入游标：记录已扫描过的 history 消息索引（run_stream 重入时重置）
        self._mm_scan_cursor: int = 0
        # thinking回顾扫描游标：重置为当前输出文件行数，跳过历史迭代内容，
        # 本轮提醒只扫描游标之后的 thinking 行（_remind_thinking_in_output 消费后推进）
        self._thinking_scan_line: int = self._count_output_lines(task_id)
        # 🔧 事务#2002：to_api_format序列化键跟踪（provider/vision/thinking_type）。
        # provider切换时thinking_type变化（如glm→qwen）会改变to_api_format输出
        # （reasoning_content被strip/保留），导致"上下文增量异常"前缀比较误报。
        # 序列化键变化时重置history_list基线，避免合法格式变化被误报为上下文破坏。
        self._api_format_key: tuple | None = None
        # 检测 nest+workdir 冲突并向冲突事务发送 info 提示（start/restart/resume 时均触发）
        self._check_workdir_conflicts()
        if len(self._history._messages) == 0:
            # 如果history为空（fresh start），获取消息上下文和事务内容
            self._get_task_full_context(self._history)
        # 首轮迭代前预热：新进程尚未连接过provider，client临时变量（compact_threshold等）
        # 仍是__init__填入的全局默认值。按当前有效provider实时解析并更新，
        # 确保首轮compact检查与即将连接的provider一致
        # （事务#1699：flash配置512K被全局320K抢占，重启后首轮即误判上下文紧张无限重启）
        if self.llm.provider is None:
            try:
                # 传入事务content：解析@<provider>标记（含@<provider>!锁定语法），
                # 使预热选中的provider与chat_stream一致（事务#1793：预热按priority
                # 自动选择会污染self.provider，导致chat_stream锁定语法失效）
                warmup_task = self.task_manager.get_task(task_id)
                warmup_content = warmup_task.get("content") if warmup_task else None
                self.llm.refresh_provider_vars(content=warmup_content + history.get_system())
            except Exception as e:
                logger.debug(f"[ToolEngine] provider临时变量预热失败(忽略): {e}")
        history_list = []  # 增量异常检测用
        while True:
            self._task = self.task_manager.get_task(task_id)
            self.exit_on_unexpected_status()
            # 每轮迭代开始：检查 @compact 指令覆盖
            effective_compact_threshold = self._get_effective_compact_threshold()
            self.check_progress_and_compact(effective_compact_threshold, history)

            # 标记新迭代开始，防止跨迭代合并user消息破坏上下文前缀缓存
            history.mark_iteration_start()

            # 根事务处理新回复消息
            self._get_unread_reply(history)
            # 所有事务都可以获取临时指示
            self._get_task_info(history)
            # 迭代边界已消费新消息/info，重置紧急中断检测节流缓存（事务#1860）：
            # 强制下一轮思考流重新查询，避免1秒窗口内的过期缓存重复跳迭代
            self._last_urgent_check_ts = 0.0
            self._last_urgent_interrupt_reason = None
            # 多模态注入：@file=<path>/webui附件 引用的文件转为多模态 content part
            # （仅 vision provider 生效；在 _save_history 之前保证持久化保留附件）
            await self._inject_multimodal_files(history)
            self._save_history(history)

            # 每轮迭代开始：统一备份（checklist/history/git快照/外部文件基线）
            # 并在 output_manager（xterm）输出回滚超链接。复用工具备份机制。
            await self._create_iteration_backup(task_id, history)

            metadata = {
                "history": history,
            }
            results = await hook_manager.execute_before_hooks(
                message_id=message_id,
                task_id=task_id,
                category="flow",
                name="prompt",
                metadata=metadata,
            )
            history = metadata["history"]
            for action, reason in results:
                if action == "block":
                    # flow==prompt 时，block 则结束 run_stream
                    if reason:
                        history.add_user(reason)
                    yield CompleteEvent(
                        content=reason or "钩子阻止了 LLM 调用",
                        iterations=self._iteration,
                    )
                    return

            if hook_manager and history:
                metadata = {
                    "history": history,
                    "task": self._task,
                }
                results = await hook_manager.execute_after_hooks(
                    message_id=message_id,
                    task_id=task_id,
                    category="flow",
                    name="prompt",
                    metadata=metadata,
                )
                history = metadata["history"]
                for action, reason in results:
                    if action == "block":
                        msg_content = (
                            f"[ToolEngine] 钩子在流调用后阻止了流程继续: prompt, 原因: {reason}"
                        )
                        logger.debug(msg_content)
                        history.add_user(msg_content)
                        should_complete = True

            # 1. 流式调用 LLM（vision provider 保留多模态 content part）
            # 存在待发送附件时强制vision=True（请求携带附件part，chat_stream
            # 检测到后会切换到vision provider），否则vision provider也看不到图片
            _vision_provider = self._is_multimodal_provider() or (
                history.has_pending_attachments() and self._has_vision_provider_available()
            )
            # 🔧 事务#2111：qwen 系列 preserve_thinking=True 时请求中保留
            # reasoning_content（模型可复用上轮思考，代价是额外输入token）
            _preserve_thinking = self._provider_preserve_thinking()
            current_messages = history.to_api_format(
                thinking_type=self.llm.thinking_type,
                vision=_vision_provider,
                preserve_thinking=_preserve_thinking,
            )
            if history.size() > 5:
                # 🔧 事务#2002：序列化键（provider+thinking_type+vision+preserve_thinking）
                # 变化时重置增量检测基线。provider切换（如glm→qwen）会改变thinking_type，
                # to_api_format输出格式随之变化（reasoning_content被strip/保留），
                # 前缀比较必然失败，误报"上下文增量异常"并输出大段history diff。
                # 格式变化是合法的provider适配行为，不是上下文破坏，不应告警。
                # （事务#2111：preserve_thinking 热改配置同样改变序列化格式，纳入键）
                _fmt_key = (
                    self.llm.provider,
                    self.llm.thinking_type,
                    _vision_provider,
                    _preserve_thinking,
                )
                if self._api_format_key is not None and _fmt_key != self._api_format_key:
                    logger.info(
                        f"[ToolEngine] 事务#{task_id} to_api_format序列化键变化"
                        f"({self._api_format_key} → {_fmt_key})，重置增量检测基线"
                        f"（provider切换导致格式变化，非上下文破坏）"
                    )
                    history_list.clear()
                self._api_format_key = _fmt_key
                history_list.append(json.dumps(current_messages, ensure_ascii=False, indent=2))
                if len(history_list) > 2:
                    history_list.pop(0)
                    prev = history_list[-2][:-5]  # 需去除尾部 ] }
                    if not history_list[-1].startswith(prev):
                        diff = generate_colored_diff(history_list[-2], history_list[-1], "history")
                        warn_msg = f"[tool_engine]上下文增量异常！\n{diff}"
                        logger.error(warn_msg)
                        get_output_manager().write_output(
                            task_id=task_id,
                            content=f"{ANSI_COLORS.get('bright_red')}{warn_msg}{ANSI_COLORS.get('reset')}\n",
                            source="system",
                        )
            thinking = ""
            content = ""
            tool_calls: list[Any] = []
            chunk_count = 0
            llm_finish = False
            should_quit = False
            retry = 0
            is_rate_limited = False  # 429限流标志：禁止冷却和切换provider，立刻重试抢流量
            is_content_filtered = (
                False  # 400内容审核标志：禁止冷却和切换provider，重试1次后删user输入继续
            )
            is_transient_error = (
                False  # 网络瞬时错误标志(ReadError等)：禁止切换provider，同provider重试
            )
            is_context_overflow = (
                False  # 🔧 事务#2002：400上下文长度超标标志：禁止切换provider，
                # compact压缩上下文后重试（上下文是provider无关的，换provider必然同样失败）
            )
            should_skip_iteration = False  # 中断标志：检测到新消息/info/控制信号时跳过本次迭代
            thinking_duplicate_count = 0  # 重复计数
            thinking_recent_lines = []  # 最近N行
            thinking_buffer = ""  # 缓存thinking流式输出，拼接成完整行后判断重复
            wait_hit_count = 0  # wait/等等 精神分裂式思考命中计数
            content_dup_count = 0  # content窗口内相似行重复计数
            content_recent_lines = []  # content窗口
            content_buffer = ""  # content行缓冲
            content_loop_detected = False  # content死循环检测标志
            degenerate_loop_detected = False  # 思考退化循环检测标志（模型自行输出degenerate loop）
            # 纯符号无效输出检测：thinking/content连续N个纯符号字符（无字母无中文）判定无效输出
            _symbol_threshold = self.llm.config.symbol_output_threshold  # 0=禁用
            thinking_symbol_run = 0  # thinking流连续符号计数（跨chunk累积）
            content_symbol_run = 0  # content流连续符号计数（跨chunk累积）
            symbol_output_detected = False  # 纯符号无效输出检测标志
            # 重复词语无效输出检测：thinking/content连续N个相同英文单词或中文词语判定无效输出
            _repeated_word_threshold = self.llm.config.repeated_word_threshold  # 0=禁用
            thinking_repeated_state: dict = {}  # thinking流重复词检测状态（跨chunk累积）
            content_repeated_state: dict = {}  # content流重复词检测状态（跨chunk累积）
            repeated_word_detected = False  # 重复词语无效输出检测标志
            # 重复短语无效输出检测（单词级/行级检测的兜底，事务#1599漏检场景）
            _repeated_phrase_maxlen = self.llm.config.repeated_phrase_maxlen  # <2=禁用
            thinking_phrase_state: dict = {}  # thinking流重复短语检测状态（跨chunk累积）
            content_phrase_state: dict = {}  # content流重复短语检测状态（跨chunk累积）
            stream = None  # 🔧 事务#2178：本次LLM调用的流生成器引用（异常清理用）
            while not llm_finish:
                try:
                    thinking = ""
                    content = ""
                    tool_calls: list[Any] = []
                    chunk_count = 0
                    # 纯符号计数在重试时不重置：跨重试累积，尽早检测垃圾流
                    # （重试时模型可能继续输出符号垃圾，重置计数会推迟检测）

                    # 重试时 provider 可能已切换（thinking_type/vision/preserve_thinking
                    # 变化），需重建消息
                    if retry > 0:
                        current_messages = history.to_api_format(
                            thinking_type=self.llm.thinking_type,
                            vision=self._is_multimodal_provider()
                            or (
                                history.has_pending_attachments()
                                and self._has_vision_provider_available()
                            ),
                            preserve_thinking=self._provider_preserve_thinking(),
                        )

                    # 思考模式确定（优先级从高到低）：
                    # 1. @think=<value> 指令覆盖：用户显式指定（事务content/info中），
                    #    与固定模式一样尊重用户显式配置，不做自动干预
                    # 2. 思考过载检测：强制关闭思考模式时传入 disabled
                    #    注意：仅 auto 模式允许干预（auto=自动管理思考模式，发现过度思考时禁用/降级）
                    #    固定模式（enabled/low/high/max/disabled）尊重用户显式配置，不做强制干预
                    effective_thinking_override = self._get_effective_thinking_mode()
                    if effective_thinking_override is None:
                        if self._force_thinking_disabled and self.llm.thinking_mode == "auto":
                            effective_thinking_override = "disabled"

                    stream = self.llm.chat_stream(
                        message_id=message_id,
                        task_id=task_id,
                        messages=current_messages,
                        message_manager=self.message_manager,
                        tools=tools,
                        thinking_mode=effective_thinking_override,
                        retry=retry,
                        # 429限流/400内容审核/网络瞬时错误：禁止标记provider失败，不切换不冷却
                        failed=False
                        if (is_rate_limited or is_content_filtered or is_transient_error)
                        else None,
                    )
                    logger.debug("[ToolEngine] chat_stream调用成功，开始迭代")
                    # 每次LLM调用前更新定价（provider可能切换）
                    _provider_cfg = getattr(self.llm, "provider_config", None) or {}
                    history.set_pricing(
                        {
                            "price_input_hit": _provider_cfg.get("price_input_hit", 0) or 0,
                            "price_input_miss": _provider_cfg.get("price_input_miss", 0) or 0,
                            "price_output": _provider_cfg.get("price_output", 0) or 0,
                        },
                        provider_name=self.llm.provider or "unknown",
                    )
                    async for chunk in stream:
                        chunk_count += 1
                        if chunk_count == 1:
                            logger.debug(
                                f"[ToolEngine] iteration={self._iteration}, 收到第一个chunk: type={type(chunk).__name__}"
                            )
                        # 2. 处理 thinking
                        if chunk.thinking:
                            thinking += chunk.thinking
                            yield ThinkingEvent(content=chunk.thinking)
                            # 纯符号无效输出检测：连续threshold个非字母字符判定无效
                            if _symbol_threshold > 0 and not symbol_output_detected:
                                thinking_symbol_run = self._scan_consecutive_symbols(
                                    chunk.thinking, thinking_symbol_run, _symbol_threshold
                                )
                                if thinking_symbol_run >= _symbol_threshold:
                                    symbol_output_detected = True
                                    should_skip_iteration = True
                                    logger.warning(
                                        f"⚡ [ToolEngine] 事务#{task_id}检测到思考纯符号无效输出"
                                        f"(连续{thinking_symbol_run}个非字母字符)，丢弃最后2行"
                                    )
                                    break
                            # 重复词语无效输出检测：连续threshold个相同英文单词或中文词语判定无效
                            if (
                                _repeated_word_threshold > 0
                                and not repeated_word_detected
                                and not symbol_output_detected
                            ):
                                if self._scan_repeated_words(
                                    chunk.thinking,
                                    thinking_repeated_state,
                                    _repeated_word_threshold,
                                ):
                                    repeated_word_detected = True
                                    should_skip_iteration = True
                                    logger.warning(
                                        f"⚡ [ToolEngine] 事务#{task_id}检测到思考重复词语无效输出"
                                        f"(连续{_repeated_word_threshold}个相同词语)，丢弃最后2行"
                                    )
                                    break
                            # 重复短语无效输出检测：连续N个相同字符串(≤repeated_phrase_maxlen字符)
                            # 阈值用thinking_dead_loop_threshold（事务#1599漏检场景：单行内
                            # 复读"these are all independent..."×19，单词级/行级检测均漏掉）
                            if (
                                _repeated_phrase_maxlen >= 2
                                and not repeated_word_detected
                                and not symbol_output_detected
                            ):
                                _ph_thresh = self.llm.config.thinking_dead_loop_threshold
                                if self._scan_repeated_phrases(
                                    chunk.thinking,
                                    thinking_phrase_state,
                                    _ph_thresh,
                                    _repeated_phrase_maxlen,
                                ):
                                    repeated_word_detected = True
                                    should_skip_iteration = True
                                    logger.warning(
                                        f"⚡ [ToolEngine] 事务#{task_id}检测到思考重复短语无效输出"
                                        f"(连续{_ph_thresh}个相同短语，≤{_repeated_phrase_maxlen}字符)，丢弃最后2行"
                                    )
                                    break
                            (
                                should_skip_iteration,
                                thinking_duplicate_count,
                                thinking_recent_lines,
                                thinking_buffer,
                                wait_hit_count,
                            ) = self._process_thinking_chunk(
                                chunk.thinking,
                                task_id,
                                should_skip_iteration,
                                thinking_duplicate_count,
                                thinking_recent_lines,
                                thinking_buffer,
                                wait_hit_count=wait_hit_count,
                                wait_threshold=self.llm.config.overthinking_threshold,
                                dead_loop_threshold=self.llm.config.thinking_dead_loop_threshold,
                            )
                            # 思考退化循环检测：模型自行检测到degenerate loop但无法退出，强制停止思考
                            if not degenerate_loop_detected:
                                _deg_patterns = self.llm.config.degenerate_loop_patterns
                                if _deg_patterns:
                                    import re as _re

                                    # 预编译所有pattern，跳过无效正则
                                    _deg_compiled = []
                                    for _pat in _deg_patterns:
                                        try:
                                            _compiled = _re.compile(_pat, _re.IGNORECASE)
                                        except _re.error:
                                            logger.warning(
                                                f"⚠️ [ToolEngine] 退化循环检测pattern无效，已跳过: {_pat!r}"
                                            )
                                            continue
                                        # 安全验证：如果pattern匹配普通正常思考行，说明过于宽泛（如未转义的
                                        # 字符类 [STOP LOOP] 会匹配任意S/T/O/P/空格/L），跳过并警告防误报
                                        if _compiled.search(_SAFE_THINKING_LINE):
                                            logger.warning(
                                                f"⚠️ [ToolEngine] 退化循环检测pattern过于宽泛，"
                                                f"会误报正常思考行，已跳过: {_pat!r}"
                                            )
                                            continue
                                        _deg_compiled.append(_pat)

                                    _deg_scan = chunk.thinking
                                    while "\n" in _deg_scan:
                                        _line_end = _deg_scan.index("\n") + 1
                                        _line = _deg_scan[:_line_end]
                                        _deg_scan = _deg_scan[_line_end:]
                                        for _pat in _deg_compiled:
                                            try:
                                                if _re.search(_pat, _line, _re.IGNORECASE):
                                                    degenerate_loop_detected = True
                                                    should_skip_iteration = True
                                                    logger.info(
                                                        f"⚡ [ToolEngine] 事务#{task_id}检测到思考退化循环"
                                                        f"(pattern={_pat!r}): {_line.strip()[:80]}"
                                                    )
                                                    break
                                            except _re.error:
                                                pass  # 无效正则跳过
                                        if degenerate_loop_detected:
                                            break
                            # thinking阶段：每个chunk检测控制信号中断（仅pause/cancel等控制信号），
                            # 以及感叹号开头的紧急新回复消息/info（打断思考立刻处理，事务#1860）。
                            # 普通新回复消息/info不打断思考，由迭代边界自然处理
                            _interrupt = self._has_pending_interrupts()
                            _urgent_reason = None if _interrupt else self._has_urgent_interrupts()
                            if _interrupt or _urgent_reason:
                                self.exit_on_unexpected_status()
                                if _interrupt:
                                    logger.info(
                                        f"⚡ [ToolEngine] 事务#{task_id}thinking阶段检测到控制信号，停止思考保存进度"
                                    )
                                else:
                                    logger.info(
                                        f"⚡ [ToolEngine] 事务#{task_id}thinking阶段检测到紧急中断，停止思考立刻处理：{_urgent_reason}"
                                    )
                                should_skip_iteration = True
                            if should_skip_iteration:
                                break

                        # 3. 处理内容
                        if chunk.content:
                            content += chunk.content
                            yield ContentEvent(content=chunk.content)
                            # 纯符号无效输出检测：连续threshold个非字母字符判定无效
                            if _symbol_threshold > 0 and not symbol_output_detected:
                                content_symbol_run = self._scan_consecutive_symbols(
                                    chunk.content, content_symbol_run, _symbol_threshold
                                )
                                if content_symbol_run >= _symbol_threshold:
                                    symbol_output_detected = True
                                    should_skip_iteration = True
                                    logger.warning(
                                        f"⚡ [ToolEngine] 事务#{task_id}检测到content纯符号无效输出"
                                        f"(连续{content_symbol_run}个非字母字符)，丢弃最后2行"
                                    )
                                    break
                            # 重复词语无效输出检测：连续threshold个相同英文单词或中文词语判定无效
                            if (
                                _repeated_word_threshold > 0
                                and not repeated_word_detected
                                and not symbol_output_detected
                            ):
                                if self._scan_repeated_words(
                                    chunk.content, content_repeated_state, _repeated_word_threshold
                                ):
                                    repeated_word_detected = True
                                    should_skip_iteration = True
                                    logger.warning(
                                        f"⚡ [ToolEngine] 事务#{task_id}检测到content重复词语无效输出"
                                        f"(连续{_repeated_word_threshold}个相同词语)，丢弃最后2行"
                                    )
                                    break
                            # 重复短语无效输出检测：连续N个相同字符串(≤repeated_phrase_maxlen字符)
                            # 阈值用content_dead_loop_threshold（正常content是给用户的最终输出，
                            # 阈值8次更保守防误报；thinking退化复读是主要漏检场景）
                            if (
                                _repeated_phrase_maxlen >= 2
                                and not repeated_word_detected
                                and not symbol_output_detected
                            ):
                                _ph_thresh = self.llm.config.content_dead_loop_threshold
                                if self._scan_repeated_phrases(
                                    chunk.content,
                                    content_phrase_state,
                                    _ph_thresh,
                                    _repeated_phrase_maxlen,
                                ):
                                    repeated_word_detected = True
                                    should_skip_iteration = True
                                    logger.warning(
                                        f"⚡ [ToolEngine] 事务#{task_id}检测到content重复短语无效输出"
                                        f"(连续{_ph_thresh}个相同短语，≤{_repeated_phrase_maxlen}字符)，丢弃最后2行"
                                    )
                                    break
                            # content死循环检测：复用公共_detect_duplicate_lines
                            # 相似度阈值0.75：模型死循环时输出高度相似的变体句子（如
                            # "让我搜索X" vs "让我用grep搜索X" 相似度≈0.80），
                            # 0.95太严会导致这类死循环完全检测不到。
                            # 安全性：需要content_dead_loop_threshold（默认8）次连续命中才触发，
                            # 正常编码中连续8行0.75+相似几乎不可能。
                            (
                                content_dup_count,
                                content_recent_lines,
                                content_buffer,
                                content_loop_detected,
                            ) = self._detect_duplicate_lines(
                                chunk.content,
                                task_id,
                                content_dup_count,
                                content_recent_lines,
                                content_buffer,
                                dead_loop_threshold=self.llm.config.content_dead_loop_threshold,
                                similarity_threshold=0.75,
                                label="content",
                            )
                            if content_loop_detected:
                                should_skip_iteration = True
                                break

                        # 4. 收集工具调用
                        if chunk.tool_calls:
                            tool_calls = chunk.tool_calls
                            logger.debug(f"[ToolEngine] 收到tool_calls: {tool_calls}")
                        # 4.1 收集API返回的真实token用量
                        if chunk.usage:
                            history.update_usage(chunk.usage)
                        self.task_manager.update_heartbeat(task_id)
                        self.exit_on_unexpected_status()

                        # 5. 中断检测：每隔32个chunk检查一次控制信号（仅pause/cancel等），
                        # 以及感叹号开头的紧急新回复消息/info（打断思考立刻处理，事务#1860）。
                        # 普通新回复消息/info不打断思考（用户明确要求），由每轮迭代开始的
                        # _get_unread_reply/_get_task_info 在迭代边界自然注入处理
                        if chunk_count % 32 == 0:
                            _interrupt = self._has_pending_interrupts()
                            _urgent_reason = None if _interrupt else self._has_urgent_interrupts()
                            if _interrupt or _urgent_reason:
                                self.exit_on_unexpected_status()
                                if _interrupt:
                                    logger.info(
                                        f"⚡ [ToolEngine] 事务#{task_id}检测到中断信号，停止思考跳过本次迭代"
                                    )
                                else:
                                    logger.info(
                                        f"⚡ [ToolEngine] 事务#{task_id}检测到紧急中断，停止思考立刻处理：{_urgent_reason}"
                                    )
                                should_skip_iteration = True
                                break
                        # 🔧 事务#2064：每chunk兜底检测（复用_has_urgent_interrupts
                        # 内置1秒节流，DB开销可控）——补齐两个盲区：①短响应
                        # （chunk数<32）整轮零检测；②非thinking响应（无thinking
                        # chunk不触发上方检测）。检测到紧急info/消息立刻打断，
                        # 不留到下一轮迭代边界
                        if not should_skip_iteration:
                            _interrupt = self._has_pending_interrupts()
                            _urgent_reason = None if _interrupt else self._has_urgent_interrupts()
                            if _interrupt or _urgent_reason:
                                self.exit_on_unexpected_status()
                                if _interrupt:
                                    logger.info(
                                        f"⚡ [ToolEngine] 事务#{task_id}流chunk检测到中断信号，停止思考跳过本次迭代"
                                    )
                                else:
                                    logger.info(
                                        f"⚡ [ToolEngine] 事务#{task_id}流chunk检测到紧急中断，停止思考立刻处理：{_urgent_reason}"
                                    )
                                should_skip_iteration = True
                                break
                    # 🔧 事务#2178：显式关闭本次LLM调用的流生成器（紧急中断/无效输出
                    # 等break跳出async for时Python不会关闭生成器：client侧finally不执行、
                    # httpx连接池与信号量槽位被僵尸请求占用，直到下一轮chat_stream重赋值
                    # 才被GC回收——期间服务端继续往没人读的socket吐字、多事务共享GPU时
                    # KV/批处理槽被僵尸挤占，正是"感叹号紧急info经常导致从头prefill甚至
                    # 卡死"的根因。正常跑完的流aclose为幂等no-op。）
                    await self._aclose_llm_stream(stream)
                    llm_finish = True
                except GeneratorExit:
                    # 🔧 事务#2178：run_stream自身被关闭（yield处收到GeneratorExit）时，
                    # 进行中的流生成器不会被自动关闭，显式清理后再传播退出
                    await self._aclose_llm_stream(stream)
                    raise
                except asyncio.CancelledError as e:
                    text = f"⚠️  事务#{task_id} [ToolEngine]检测到中断，立刻结束大模型处理{e}。\n"
                    logger.info(text)
                    # 🔧 事务#2178：取消可能发生在yield处（生成器不会自动关闭），显式清理
                    await self._aclose_llm_stream(stream)
                    return
                except StreamStallError as e:
                    # SSE流停滞：立即切换provider，不需要多次重试
                    retry += 1
                    is_rate_limited = False  # 非429，重置限流标志
                    is_content_filtered = False  # 非内容审核错误，重置标志
                    is_transient_error = False  # 非网络瞬时错误，重置标志
                    provider_name = e.provider or self.llm.provider or "unknown"
                    logger.info(
                        f"🤖 事务#{task_id}大模型SSE流停滞超过{e.timeout}秒provider={provider_name}，立即重试。"
                    )
                    self.llm.error = f"SSE流停滞超过{e.timeout}秒 (provider={provider_name})"
                    if retry >= 5:
                        logger.error(
                            f"🤖 事务#{task_id}SSE流停滞已重试{retry}次仍失败，跳出重试循环。"
                        )
                        break
                    self.exit_on_unexpected_status()
                    continue
                except LLMError as e:
                    # LLM API 返回错误（如上下文长度超限）：切换provider重试
                    retry += 1
                    is_rate_limited = False  # 非429，重置限流标志
                    is_content_filtered = False  # 非内容审核错误，重置标志
                    is_transient_error = False  # 非网络瞬时错误，重置标志
                    provider_name = e.provider or self.llm.provider or "unknown"
                    logger.warning(f"🤖 事务#{task_id}大模型API错误(provider={provider_name}): {e}")
                    self.llm.error = f"{e} (provider={provider_name})"
                    if retry >= 5:
                        logger.error(
                            f"🤖 事务#{task_id}大模型API错误已重试{retry}次仍失败，"
                            f"跳出重试循环。最后错误: {e}"
                        )
                        break
                    self.exit_on_unexpected_status()
                    continue
                except httpx.HTTPStatusError as e:
                    retry += 1
                    is_rate_limited = False  # 非429，重置限流标志（下方按状态码重新判断）
                    is_content_filtered = False  # 非内容审核错误，重置标志（下方按状态码重新判断）
                    is_transient_error = False  # 非网络瞬时错误，重置标志
                    icon = self.llm.config.get_icon(self.llm.provider)
                    # 提取详细的HTTP错误信息：状态码、响应体、请求URL
                    status_code = e.response.status_code
                    url = str(e.request.url) if e.request else "unknown"
                    # httpx流式响应需要先aread()才能访问text
                    # 但在同步异常处理中无法await，所以先用content，为空时记录headers辅助调试
                    try:
                        resp_content = e.response.content
                        resp_text = (
                            resp_content.decode("utf-8", errors="ignore")[:500]
                            if resp_content
                            else ""
                        )
                    except Exception:
                        resp_text = ""
                    # 如果响应体为空（流式模式下常见），记录响应头信息辅助调试
                    if not resp_text:
                        resp_headers = dict(e.response.headers) if e.response.headers else {}
                        content_length = resp_headers.get("content-length", "0")
                        content_type = resp_headers.get("content-type", "unknown")
                        resp_text = f"(流式响应体为空, content-length={content_length}, content-type={content_type})"
                    if resp_text:
                        # 尝试解析JSON格式的错误消息
                        try:
                            import json as _json

                            err_json = _json.loads(resp_text)
                            # 提取常见的错误字段
                            err_msg = (
                                err_json.get("error", {}).get("message", "")
                                if isinstance(err_json.get("error"), dict)
                                else err_json.get("error", "")
                                or err_json.get("message", "")
                                or resp_text[:100]
                            )
                        except Exception:
                            err_msg = resp_text[:100]
                    else:
                        err_msg = ""
                    # HTTP状态码具体含义映射
                    HTTP_STATUS_MEANINGS = {
                        400: "请求参数错误",
                        401: "认证失败/API密钥无效",
                        403: "权限不足/访问被拒绝",
                        404: "接口不存在",
                        408: "请求超时",
                        429: "请求过于频繁(限流)",
                        500: "服务器内部错误",
                        502: "网关错误/上游服务异常",
                        503: "服务暂时不可用",
                        504: "网关超时",
                    }
                    status_meaning = HTTP_STATUS_MEANINGS.get(status_code, f"服务器错误")
                    detail = f"HTTP {status_code} {status_meaning}"
                    if err_msg:
                        detail += f" - {err_msg}"
                    self.llm.error = detail
                    # 区分429类型：临时限流 vs 配额耗尽
                    # 临时限流：抢流量重试同一provider
                    # 配额耗尽：切换provider（未锁定时）
                    if status_code == 429:
                        # 检测配额耗尽特征（中英文关键词）
                        quota_keywords = [
                            "使用上限",
                            "配额",
                            "额度",
                            "quota",
                            "exceeded",
                            "limit reached",
                            "exhausted",
                            "insufficient_quota",
                            "上限",
                            "plan",
                            "subscription",
                            "billing",
                        ]
                        is_quota_exhausted = any(
                            kw in err_msg.lower() for kw in [k.lower() for k in quota_keywords]
                        )
                        if is_quota_exhausted:
                            # 配额耗尽：不抢流量，按普通错误处理（切换provider）
                            is_rate_limited = False
                            logger.warning(
                                f"{icon} {self.llm.provider} 事务#{task_id}大模型API配额耗尽(429)。"
                                f"切换provider！详情: {err_msg[:150]}\n"
                            )
                        else:
                            # 临时限流：不冷却不切换，间隔0.5秒重试抢流量
                            is_rate_limited = True
                            logger.warning(
                                f"{icon} {self.llm.provider} 事务#{task_id}大模型API限流(429)。"
                                f"不冷却不切换，0.5秒后重试抢流量(第{retry}次) URL={url}\n"
                            )
                    else:
                        is_rate_limited = False
                        # 🔧 事务#2002：检测400上下文长度超标：请求token数超过provider
                        # 模型最大上下文长度。上下文是provider无关的，换provider必然
                        # 同样失败（事务#1993：glm额度耗尽→切换s-qwen3.8→400上下文
                        # 长度超标→按普通4xx重试2次→跳出→全provider不可用→等待循环，
                        # 且误报"上下文增量异常"输出大段history diff）。正确做法：
                        # 不切换provider，compact压缩上下文后重试；compact无效则拒绝
                        # 处理（重启事务），绝不破坏上下文。
                        _matched_ctx_overflow = status_code == 400 and any(
                            kw.lower() in err_msg.lower() for kw in CONTEXT_LENGTH_KEYWORDS
                        )
                        # 🔧 事务#2084 Fix1：伪上下文超标识别——服务端声称的context值
                        # 荒谬（<1024，如Strata引擎未就绪时返回context(0)）时，说明
                        # 是服务端状态异常而非请求真实超标。此时走普通4xx路径
                        # （provider切换+冷却等待，事务保活不清历史不restart），
                        # 服务恢复后自动继续；绝不能进compact/restart分支（事务#2080
                        # 曾因context(0)误判陷入93次restart死循环）。
                        if _matched_ctx_overflow:
                            _srv_ctx_limit = extract_server_context_limit(err_msg)
                            if (
                                _srv_ctx_limit is not None
                                and _srv_ctx_limit < SERVER_CONTEXT_LIMIT_SANITY_FLOOR
                            ):
                                _matched_ctx_overflow = False
                                logger.warning(
                                    f"{icon} {self.llm.provider} 事务#{task_id}大模型API"
                                    f"400错误声称context={_srv_ctx_limit}（低于合理下限"
                                    f"{SERVER_CONTEXT_LIMIT_SANITY_FLOOR}），判定为服务端"
                                    f"引擎未就绪/状态异常而非真实上下文超标。按普通错误"
                                    f"处理（切换provider/冷却等待，不清历史）。"
                                    f"详情: {err_msg[:200]}\n"
                                )
                        if _matched_ctx_overflow:
                            is_context_overflow = True
                            is_content_filtered = False
                            logger.warning(
                                f"{icon} {self.llm.provider} 事务#{task_id}大模型API"
                                f"上下文长度超标(400)。不冷却不切换，compact压缩上下文"
                                f"后重试。详情: {err_msg[:200]}\n"
                            )
                        else:
                            is_context_overflow = False
                        # 检测400内容类错误：特殊处理
                        # 这类错误不应冷却或切换provider，而是接入逐级清除策略
                        # （多模态附件→user消息→尾部轮次）后重试，见 is_content_filtered
                        # 🔧 事务#1959：关键词列表提取为模块级常量（单一事实源）
                        _matched_filter = status_code == 400 and any(
                            kw.lower() in err_msg.lower() for kw in CONTENT_FILTER_KEYWORDS
                        )
                        _matched_parse = status_code == 400 and any(
                            kw.lower() in err_msg.lower() for kw in CONTENT_PARSE_ERROR_KEYWORDS
                        )
                        if _matched_filter or _matched_parse:
                            is_content_filtered = True
                            # 记录内容错误类型，用于外层清除重试时的系统消息文案
                            self._content_filter_kind = (
                                "内容审核拦截"
                                if _matched_filter
                                else "内容解析失败(疑似损坏附件/内容类型不支持)"
                            )
                            # 🔧 事务#1819：content.type参数非法 = provider后端实际
                            # 不支持多模态（如配置vision=true但vLLM/sglang未启用视觉，
                            # 事务#1818中的s-qwen3.8）。运行期禁用其vision能力，
                            # 防止后续再选中它发送附件（自适应配置错误）。
                            # 注意：图片解码失败类错误不代表provider能力问题，不禁用。
                            if "content.type" in err_msg.lower():
                                try:
                                    from peng.config.loader import disable_provider_vision

                                    disable_provider_vision(
                                        self.llm.provider or "", ttl_seconds=600
                                    )
                                    logger.warning(
                                        f"[ToolEngine] 已运行期禁用 {self.llm.provider} 的vision能力"
                                        f"(10分钟)：后端不支持多模态content"
                                    )
                                except Exception as _dv_err:
                                    logger.debug(
                                        f"[ToolEngine] 运行期禁用vision失败(忽略): {_dv_err}"
                                    )
                            logger.warning(
                                f"{icon} {self.llm.provider} 事务#{task_id}大模型API"
                                f"{self._content_filter_kind}(400)。不冷却不切换，"
                                f"接入逐级清除策略后重试。详情: {err_msg[:200]}\n"
                            )
                        else:
                            is_content_filtered = False
                            logger.warning(
                                f"{icon} {self.llm.provider} 事务#{task_id}大模型API调用失败。"
                                f"HTTP {status_code} URL={url} 响应={resp_text[:200]}\n"
                            )
                    # 不可恢复的客户端错误(4xx)不应无限重试，最多重试2次后跳出
                    # 可恢复的服务端错误(5xx)最多重试5次
                    # 429临时限流特殊处理：大幅增加重试次数（抢流量策略）
                    # 429配额耗尽：按普通错误处理（2次后切换provider）
                    # 400内容审核：重试1次（_max_retry=2），之后由外层删除user输入继续
                    # 🔧 事务#2002：400上下文长度超标：不切换provider，compact压缩
                    # 上下文后重试（最多3次，外层每轮compact一次），仍失败则拒绝处理
                    if status_code == 429 and is_rate_limited:
                        _max_retry = 30
                    elif status_code == 429 and not is_rate_limited:
                        _max_retry = 2  # 配额耗尽：快速切换
                    elif status_code == 400 and is_context_overflow:
                        # 上下文长度超标：错误是确定性的（非瞬时），重试必然同样
                        # 失败，只有compact压缩上下文才能解决。_max_retry=0使首次
                        # 超标即跳出内层循环（retry=0>=0），由外层执行compact
                        # （外层每轮压缩一次，连续3轮compact后仍超标则拒绝处理
                        # 重启事务），避免浪费无效重试调用。
                        _max_retry = 0
                    elif status_code == 400 and is_content_filtered:
                        _max_retry = 2  # 内容审核：重试1次，之后删user输入
                    elif 400 <= status_code < 500:
                        _max_retry = 2
                    else:
                        _max_retry = 5
                    if retry >= _max_retry:
                        logger.error(
                            f"{icon} {self.llm.provider} 事务#{task_id}大模型API调用"
                            f"已重试{retry}次仍失败(HTTP {status_code})，跳出重试循环。"
                            f"最后错误: {detail}"
                        )
                        _sys_err = f"❌  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]{ANSI_COLORS.get('bright_red')}大模型API调用失败({detail})，已重试{retry}次，跳出重试循环。{ANSI_COLORS.get('reset')}\n"
                        if self._should_report_system_error(_sys_err):
                            get_output_manager().write_output(
                                task_id=task_id,
                                content=_sys_err,
                                source="system",
                            )
                        break
                    # 429限流：重试前等待0.5秒，避免疯狂空转
                    if is_rate_limited:
                        await asyncio.sleep(0.5)
                    self.exit_on_unexpected_status()
                    continue
                except httpx.TransportError as e:
                    # 网络传输错误分层处理（事务#2016）：
                    # - ConnectError/RemoteProtocolError：无法建立连接/对端异常断开，
                    #   是服务进程崩溃或重启中的明确信号（如本地sglang段错误自动重启）。
                    #   同provider快速重试2次抗网络闪断，仍失败则不标记瞬时错误，
                    #   由外层"所有provider失败"分支按冷却等待后重新选择provider
                    #   （mark_failed自适应冷却+按优先级切换），绝不无限死磕已崩溃的服务。
                    #   （原行为：无限重试同一provider直到服务自行恢复，导致事务
                    #   在服务重启期间长时间卡在prefill状态）
                    # - 其他传输错误（ReadTimeout/ReadError/WriteError等）：多为网络
                    #   抖动，保持原策略：同provider重试5次，标记瞬时错误不切换provider。
                    retry += 1
                    is_rate_limited = False
                    is_content_filtered = False
                    icon = self.llm.config.get_icon(self.llm.provider)
                    err_str = str(e).strip()
                    if err_str:
                        self.llm.error = f"{type(e).__name__}: {err_str}"
                    else:
                        self.llm.error = f"{type(e).__name__}"
                    service_unavailable = isinstance(
                        e, (httpx.ConnectError, httpx.RemoteProtocolError)
                    )
                    max_retry = 2 if service_unavailable else 5
                    if service_unavailable:
                        # 关键：清除可能残留的瞬时错误标记（如前一轮ReadTimeout），
                        # 确保耗尽后走"所有provider失败"分支实现provider切换
                        is_transient_error = False
                        logger.info(
                            f"{icon} {self.llm.provider} 事务#{task_id}大模型服务不可用"
                            f"({type(e).__name__})，0.5秒后同provider重试"
                            f"(第{retry}/{max_retry}次)，重试耗尽将切换其他provider。"
                        )
                    else:
                        is_transient_error = True  # 标记瞬时错误，阻止provider切换
                        # 🔧 事务#2235：断流但已收到实质content——保留响应，不丢弃重烧！
                        # #2235事故：服务器每次都完整输出thinking+content（"上下文将满，
                        # 我把全部发现固化到checklist后重启事务"）后流尾ReadError，
                        # 原实现整个响应丢弃→重试→312K上下文重发+重复生成→无限烧钱
                        # 死循环（30分钟959次ReadError，tool_call永远没机会传输）。
                        # 保留判据极简：content非空。保留后视为本流有效结束
                        # （llm_finish=True跳出重试循环），走正常迭代收尾：
                        # thinking/content写入history（已烧的token不浪费），
                        # 模型下一轮可见自己已说过的内容，上下文演进不再原地打转。
                        if content.strip():
                            _rc_msg = (
                                f"⚠️  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]"
                                f"{ANSI_COLORS.get('bright_yellow')}"
                                f"事务#{task_id}流被{type(e).__name__}中断，但已收到完整响应内容"
                                f"({len(content)}字符)，保留本次响应不再重试（避免丢弃重烧）。"
                                f"若响应被截断，模型下一轮会看到自己的部分输出并自然继续。"
                                f"{ANSI_COLORS.get('reset')}\n"
                            )
                            logger.warning(
                                f"⚠️ [ToolEngine] 事务#{task_id} {type(e).__name__}断流"
                                f"但content非空({len(content)}字符)，保留响应走正常收尾"
                            )
                            if self._should_report_system_error(_rc_msg):
                                get_output_manager().write_output(
                                    task_id=task_id,
                                    content=_rc_msg,
                                    source="system",
                                )
                            # 成功语义：重置熔断计数（与4706区成功重置一致）
                            self._transient_rounds = 0
                            llm_finish = True
                            break
                        logger.info(
                            f"{icon} {self.llm.provider} 事务#{task_id}大模型网络瞬时错误"
                            f"({type(e).__name__})，不切换provider，0.5秒后同provider重试(第{retry}次)。\n"
                            f"堆栈: {traceback.format_exc()}"
                        )
                    if retry >= max_retry:
                        if service_unavailable:
                            logger.warning(
                                f"{icon} {self.llm.provider} 事务#{task_id}大模型服务不可用"
                                f"({type(e).__name__})已重试{retry}次仍失败，"
                                f"按冷却等待后自动切换其他provider。"
                                f"最后错误: {self.llm.error}"
                            )
                        else:
                            logger.error(
                                f"{icon} {self.llm.provider} 事务#{task_id}大模型网络瞬时错误"
                                f"已重试{retry}次仍失败，跳出重试循环。"
                                f"最后错误: {self.llm.error}"
                            )
                        break
                    await asyncio.sleep(0.5)  # 网络瞬时错误：短暂等待后重试
                    self.exit_on_unexpected_status()
                    continue
                except Exception as e:
                    retry += 1
                    is_rate_limited = False  # 非429错误，重置限流标志
                    is_content_filtered = False  # 非内容审核错误，重置标志
                    is_transient_error = False  # 非网络瞬时错误，重置标志
                    icon = self.llm.config.get_icon(self.llm.provider)
                    # 使用异常的完整字符串表示，包含具体错误信息
                    err_str = str(e).strip()
                    if err_str:
                        self.llm.error = f"{type(e).__name__}: {err_str}"
                    else:
                        self.llm.error = f"{type(e).__name__}"
                    logger.info(
                        f"{icon} {self.llm.provider} 事务#{task_id}大模型API调用失败。"
                        f"API报错: {self.llm.error}\n"
                        f"堆栈: {traceback.format_exc()}"
                    )
                    if retry >= 5:
                        logger.error(
                            f"{icon} {self.llm.provider} 事务#{task_id}大模型API调用"
                            f"已重试{retry}次仍失败，跳出重试循环。"
                            f"最后错误: {self.llm.error}"
                        )
                        break
                    self.exit_on_unexpected_status()
                    continue
                retry = 0
                self.llm.error = ""
                # LLM调用成功，重置内容审核删除计数
                self._content_filter_deletions = 0
                self._content_filter_kind = ""
                # LLM调用成功，重置全provider失败连续轮次计数
                self._all_failed_rounds = 0
                # 🔧 事务#2002：LLM调用成功，重置上下文长度超标compact轮次计数
                self._context_overflow_rounds = 0
                # 🔧 事务#2235：LLM调用成功，重置瞬时错误熔断计数
                self._transient_rounds = 0

            # 如果重试循环因错误耗尽而break退出（llm_finish仍为False），
            # 说明所有provider都不可用（如全部被限流/冷却中）。
            # 此时不应继续处理空响应（会导致上下文被清空），而应等待provider恢复后重试。
            if not llm_finish:
                if is_rate_limited:
                    # 429限流：0.5秒间隔重试同一provider抢流量
                    logger.info(
                        f"🔥 [ToolEngine] 事务#{task_id} 429限流重试耗尽，"
                        f"0.5秒间隔继续抢流量！provider={self.llm.provider}"
                    )
                    await asyncio.sleep(0.5)
                    continue  # 回到外层 while True 循环，重试同一provider
                if is_transient_error:
                    # 🔧 事务#2235：网络瞬时错误无限死磕兜底——原实现5次重试
                    # 耗尽后sleep(5)+continue回外层无限重来（retry清零）。content
                    # 非空的断流已在TransportError分支保留响应走正常收尾（不重试），
                    # 到达此处的均为content为空的断流（thinking阶段即断）。连续
                    # 3轮耗尽（15次流全失败）说明服务器持续性故障，放弃5秒高频
                    # 死磕，落入下方公共provider失败等待路径（30-300秒长冷却+
                    # _all_failed_rounds计数+==5醒目警告，事务保活，恢复后自动继续）
                    self._transient_rounds += 1
                    if self._transient_rounds >= 3:
                        _te_msg = (
                            f"⛔  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]"
                            f"{ANSI_COLORS.get('bright_red')}"
                            f"事务#{task_id}网络瞬时错误持续断流（连续第{self._transient_rounds}轮耗尽，"
                            f"provider={self.llm.provider}，流均在收到内容前中断），停止5秒高频重试，"
                            f"转入长冷却等待。请检查该provider服务状态（部署日志/网络）。"
                            f"事务保持运行，冷却后自动重试。"
                            f"{ANSI_COLORS.get('reset')}\n"
                        )
                        logger.error(
                            f"⛔ [ToolEngine] 事务#{task_id} 瞬时错误熔断"
                            f"(连续{self._transient_rounds}轮耗尽)，转入公共provider失败等待路径"
                        )
                        if self._should_report_system_error(_te_msg):
                            get_output_manager().write_output(
                                task_id=task_id,
                                content=_te_msg,
                                source="system",
                            )
                        # 不continue：落入下方公共provider失败等待路径
                    else:
                        logger.info(
                            f"🌐 [ToolEngine] 事务#{task_id} 网络瞬时错误重试耗尽"
                            f"(第{self._transient_rounds}轮)，5秒后继续重试同一provider={self.llm.provider}"
                        )
                        await asyncio.sleep(5)
                        continue  # 回到外层 while True 循环，重试同一provider（未被标记失败）
                if is_context_overflow:
                    # 🔧 事务#2002：400上下文长度超标：上下文是provider无关的，
                    # 换provider必然同样失败。正确做法：compact压缩上下文后重试。
                    # 连续3轮compact后仍超标说明上下文无法压缩到provider能力内，
                    # 拒绝处理（重启事务），绝不破坏上下文。
                    self._context_overflow_rounds += 1
                    # 用当前provider的compact_threshold作为compact目标
                    _co_threshold = self.llm.compact_threshold
                    # 每轮逐步收紧compact目标（0.6→0.48→0.36），加速压缩
                    _co_ratio = max(0.36, 0.6 - (self._context_overflow_rounds - 1) * 0.12)
                    _co_orig, _co_after = history.compact(_co_threshold * _co_ratio, 0.6)
                    if _co_orig > _co_after:
                        # compact改变了history，重置增量检测基线，避免误报
                        history_list.clear()
                        self._save_history(history)
                    # 🔧 事务#2084 Fix2：矛盾判定（第二道防线，覆盖Fix1未解析出
                    # context数值的错误格式）。compact毫无效果（orig==after）且
                    # 当前上下文+输出预留仍在已知provider能力（compact_threshold）
                    # 之内，服务端却报"超标"——数学上自相矛盾，说明服务端状态异常
                    # （引擎未就绪/配置与实际不符），compact/restart都解决不了。
                    # 走公共provider失败等待路径（事务保活、历史保留），服务恢复
                    # 后自动继续。绝不restart（事务#2080曾因此陷入93次restart死循环）。
                    # 注：max_tokens取全局配置（≥provider级值），使矛盾判定更保守。
                    if (
                        _co_orig == _co_after
                        and _co_after + int(getattr(self.llm.config, "max_tokens", 0) or 0)
                        < _co_threshold
                    ):
                        self._context_overflow_rounds = 0  # 非真实超标，重置轮次
                        _co_msg = (
                            f"⚠️  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]"
                            f"{ANSI_COLORS.get('bright_yellow')}"
                            f"事务#{task_id}报上下文超标，但compact后上下文({_co_after})远小于"
                            f"provider能力({_co_threshold})——服务端声称的容量与已知配置矛盾，"
                            f"判定为服务端引擎未就绪/状态异常。不重启不清历史，等待服务恢复后重试..."
                            f"{ANSI_COLORS.get('reset')}\n"
                        )
                        logger.warning(f"⚠️ [ToolEngine] 事务#{task_id} {_co_msg}")
                        get_output_manager().write_output(
                            task_id=task_id,
                            content=_co_msg,
                            source="system",
                        )
                        # 落入下方公共provider失败等待路径（不continue不break）
                    else:
                        if self._context_overflow_rounds >= 3:
                            # 连续3轮compact后仍超标：拒绝处理，重启事务
                            # （🔧 事务#2084 Fix3：经熔断器防死循环——15分钟内
                            # 第3次restart改为pause等待人工/服务恢复）
                            _co_msg = (
                                f"⛔  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]"
                                f"{ANSI_COLORS.get('bright_red')}"
                                f"事务#{task_id}上下文长度超标，连续{self._context_overflow_rounds}轮"
                                f"compact后仍无法压缩到provider能力内"
                                f"（compact: {_co_orig} → {_co_after}, 阈值{_co_threshold}）。"
                                f"拒绝处理，重启事务。"
                                f"{ANSI_COLORS.get('reset')}\n"
                            )
                            logger.error(f"⛔ [ToolEngine] {_co_msg}")
                            get_output_manager().write_output(
                                task_id=task_id,
                                content=_co_msg,
                                source="system",
                            )
                            self._engine_restart_with_breaker(
                                task_id, detail=f"上下文超标compact无效({_co_orig}→{_co_after})"
                            )
                            should_complete = True
                            break
                        _co_msg = (
                            f"⚠️  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]"
                            f"{ANSI_COLORS.get('bright_yellow')}"
                            f"事务#{task_id}上下文长度超标，compact: {_co_orig} → {_co_after}"
                            f"（第{self._context_overflow_rounds}/3轮），继续重试..."
                            f"{ANSI_COLORS.get('reset')}\n"
                        )
                        logger.warning(f"⚠️ [ToolEngine] {_co_msg}")
                        get_output_manager().write_output(
                            task_id=task_id,
                            content=_co_msg,
                            source="system",
                        )
                        continue  # 回到外层 while True 循环，重试（已执行compact）
                if is_content_filtered:
                    # 400内容类错误（内容审核拦截/内容解析失败）：上下文中存在
                    # 触发错误的内容，重试相同内容必然再次失败。采用逐级升级
                    # 清除策略（多模态附件→user消息→尾部轮次），每失败一轮
                    # 扩大清除范围，直至通过。
                    _cf_kind = getattr(self, "_content_filter_kind", "") or "内容类错误"
                    try:
                        _cf_action = self._content_filter_next_action(history)
                    except RuntimeError as _cf_err:
                        # 逐级清除已到下限仍被拦截：结束重试防死循环，
                        # 异常向上传播由executor置事务为failed并记录明确原因
                        logger.error(f"🛡️ [ToolEngine] 事务#{task_id} {_cf_err}")
                        get_output_manager().write_output(
                            task_id=task_id,
                            content=(
                                f"{ANSI_COLORS.get('bright_red')}"
                                f"🛡️  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]"
                                f"{_cf_kind}清除策略已用尽，停止重试。{_cf_err}"
                                f"{ANSI_COLORS.get('reset')}\n"
                            ),
                            source="system",
                        )
                        raise
                    # 清除操作改变了history（清附件/删消息），重置增量检测基线，
                    # 避免合法清除操作误报"上下文增量异常"
                    history_list.clear()
                    self._save_history(history)
                    logger.warning(
                        f"🛡️ [ToolEngine] 事务#{task_id} {_cf_kind}，"
                        f"{_cf_action}后继续重试(级别{self._content_filter_deletions})。"
                    )
                    _sys_err = (
                        f"{ANSI_COLORS.get('bright_yellow')}"
                        f"🛡️ {_cf_kind}，已{_cf_action}并重试"
                        f"(级别{self._content_filter_deletions})..."
                        f"{ANSI_COLORS.get('reset')}\n"
                    )
                    get_output_manager().write_output(
                        task_id=task_id,
                        content=_sys_err,
                        source="system",
                    )
                    continue  # 回到外层 while True 循环，重试（已执行清除动作）
                # 全provider连续失败轮次计数：达到阈值仅输出醒目警告，
                # 不暂停事务（事务继续等待重试直至provider恢复）
                self._all_failed_rounds += 1
                if self._all_failed_rounds == 5:
                    _breaker_msg = (
                        f"⛔  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]"
                        f"{ANSI_COLORS.get('bright_red')}"
                        f"警告：已连续{self._all_failed_rounds}轮所有大模型provider"
                        f"请求均失败（最后错误: {self.llm.error or 'unknown'}）。"
                        f"事务保持运行，将持续等待并重试，请检查上下文内容/"
                        f"网络/provider配置。"
                        f"{ANSI_COLORS.get('reset')}\n"
                    )
                    logger.error(f"⛔ [ToolEngine] 事务#{task_id} {_breaker_msg}")
                    get_output_manager().write_output(
                        task_id=task_id,
                        content=_breaker_msg,
                        source="system",
                    )
                # 根据provider冷却时间动态计算等待时间
                wait_time = self.llm.config.get_min_cooldown_remaining()
                # 取冷却剩余时间+5秒余量，至少30秒，最多300秒
                wait_time = max(30, min(int(wait_time) + 5, 300))
                logger.warning(
                    f"⚠️ [ToolEngine] 事务#{task_id} 所有provider重试均失败"
                    f"(连续第{self._all_failed_rounds}轮)，等待{wait_time}秒后重试..."
                )
                _sys_err = (
                    f"⏳  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]"
                    f"{ANSI_COLORS.get('bright_yellow')}"
                    f"所有大模型provider均不可用，等待{wait_time}秒后重试..."
                    f"(连续第{self._all_failed_rounds}轮)"
                    f"{ANSI_COLORS.get('reset')}\n"
                )
                if self._should_report_system_error(_sys_err):
                    get_output_manager().write_output(
                        task_id=task_id,
                        content=_sys_err,
                        source="system",
                    )
                # 使用绝对deadline避免误差积累（exit_on_unexpected_status执行时间不可忽略）
                deadline = time.monotonic() + wait_time
                while time.monotonic() < deadline:
                    self.exit_on_unexpected_status()
                    # 🔧 事务#2064：重试等待期间同样检测紧急中断/控制信号——
                    # 此前该等待循环（429限流最长300秒）是检测真空期，用户发的
                    # 感叹号紧急info/消息完全无法打断。检测到则立刻跳出等待，
                    # 经下方continue回到外层迭代边界消费处理（与思考流打断同语义）
                    _wait_interrupt = self._has_pending_interrupts()
                    _wait_urgent = None if _wait_interrupt else self._has_urgent_interrupts()
                    if _wait_interrupt or _wait_urgent:
                        if _wait_interrupt:
                            logger.info(
                                f"⚡ [ToolEngine] 事务#{task_id}provider重试等待期间"
                                f"检测到控制信号，立刻中断等待处理"
                            )
                        else:
                            logger.info(
                                f"⚡ [ToolEngine] 事务#{task_id}provider重试等待期间"
                                f"检测到紧急中断，立刻中断等待处理：{_wait_urgent}"
                            )
                        # 复位紧急检测节流缓存，确保迭代边界消费后重新检测
                        self._last_urgent_check_ts = 0.0
                        self._last_urgent_interrupt_reason = None
                        break
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        await asyncio.sleep(min(1, remaining))
                continue  # 回到外层 while True 循环重试

            # === 多模态只发送一次机制 ===
            # 本次LLM调用已成功完成，请求中实际携带的多模态附件（user消息注入的
            # @file=/[附件] 与 read等工具结果附件）立即从history清除，防止同一
            # 附件在后续每轮迭代重复发送（视觉token通常无缓存折扣，重复发送
            # 成倍浪费token/费用）。
            # 仅当实际使用的provider具备vision能力时清除（chat_stream已把
            # provider_config更新为本次实际provider的配置）；若请求中provider
            # 切换为非vision导致附件被降级未发送，则保留附件待下一轮发送。
            # 历史被重写，重置增量检测基线（与compact后的处理一致）。
            if self._is_multimodal_provider():
                _removed = history.strip_sent_attachments()
                if _removed:
                    logger.info(
                        f"[ToolEngine] 事务#{task_id} 已从history清除{_removed}个"
                        f"已发送的多模态附件（只发送一次机制）"
                    )
                    history_list = []  # 跳过增量异常检测
                    self._save_history(history)

            # === 思考过载检测：累积thinking字节数，超阈值且无有效编辑时强制关闭思考模式 ===
            # 注意：此检测必须在 should_skip_iteration 检查之前执行，
            # 否则被跳过的迭代中的thinking字节数不会被统计。
            # 仅 auto 模式生效（固定模式尊重用户显式配置的思考级别）
            overload_threshold = self.llm.config.thinking_overload_threshold
            if overload_threshold > 0 and not self._force_thinking_disabled:
                if self.llm.thinking_mode != "auto":
                    # 固定模式：跳过思考字节数累积（不做强制禁用干预）
                    self._thinking_overload_bytes = 0
                else:
                    if thinking:
                        self._thinking_overload_bytes += len(thinking.encode("utf-8"))
                    if self._thinking_overload_bytes >= overload_threshold:
                        self._force_thinking_disabled = True
                        _overload_kb = overload_threshold // 1024
                        _overload_msg = (
                            f"\n🧠⚡ [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                            f"事务#{task_id}连续思考超过{_overload_kb}K字节"
                            f"但未进行有效编辑工作，已自动关闭思考模式以节省资源。"
                            f"调用write/edit/line_edit进行有效工作后将恢复思考模式。\n"
                        )
                        logger.info(f"[ToolEngine] {_overload_msg.strip()}")
                        get_output_manager().write_output(
                            task_id=task_id,
                            content=f"{ANSI_COLORS.get('bright_yellow')}{_overload_msg}{ANSI_COLORS.get('reset')}",
                            source="system",
                        )

            # 如果检测到中断信号（新消息/info/控制信号），跳过本次迭代
            # 直接进入下一轮迭代，下一轮会重新获取新消息/info并注入到历史中
            if should_skip_iteration:
                logger.info(f"⚡ [ToolEngine] 事务#{task_id}因中断信号跳过本次迭代，重新开始新一轮")

                # 纯符号/重复词语无效输出：丢弃最后2行，保留其余有效内容到history，
                # 注入提醒后继续下一轮迭代，避免垃圾流污染上下文但保留已消耗token的有效部分
                if symbol_output_detected or repeated_word_detected:
                    # 确定干预提示文本
                    if repeated_word_detected:
                        intervention = self._prompts.repeated_word_intervention
                        _detected_type = "重复词语"
                    else:
                        intervention = self._prompts.symbol_output_intervention
                        _detected_type = "纯符号"

                    get_output_manager().write_output(
                        task_id=task_id,
                        content=f"{ANSI_COLORS.get('bright_red')}{intervention}{ANSI_COLORS.get('reset')}\n",
                        source="system",
                    )

                    # 丢弃最后2行：从thinking和content中各移除最后2行
                    def _drop_last_lines(text: str, n_lines: int = 2) -> str:
                        """移除文本最后n_lines行"""
                        if not text:
                            return text
                        lines = text.split("\n")
                        if len(lines) <= n_lines:
                            # 总行数不足n_lines：全部丢弃（避免残留不完整内容）
                            return ""
                        return "\n".join(lines[:-n_lines])

                    thinking_trimmed = _drop_last_lines(thinking)
                    content_trimmed = _drop_last_lines(content)

                    # 保存保留的有效部分到history（避免浪费已消耗token）
                    _is_deepseek = "deepseek" in (self.llm.provider or "")
                    if thinking_trimmed:
                        history.add_thinking(thinking_trimmed, is_deepseek=_is_deepseek)
                    if content_trimmed:
                        history.add_assistant(content_trimmed)

                    # 注入干预提醒（作为user消息，有指令权威）
                    history.add_user(intervention)
                    self._save_history(history)
                    logger.info(
                        f"⚡ [ToolEngine] 事务#{task_id}{_detected_type}无效输出已丢弃最后2行"
                        f"(thinking保留{len(thinking_trimmed)}字符, content保留{len(content_trimmed)}字符)，"
                        f"注入提醒继续迭代"
                    )
                    self._iteration += 1
                    continue

                # 保存已接收的thinking内容到history，避免浪费已消耗的token
                # 截断最后不完整的行（无\n结尾的流式残留）
                thinking_to_save = thinking
                if thinking_to_save and not thinking_to_save.endswith("\n"):
                    last_nl = thinking_to_save.rfind("\n")
                    if last_nl >= 0:
                        thinking_to_save = thinking_to_save[: last_nl + 1]
                    else:
                        # 单行不完整：保留整行（比丢弃好）
                        pass

                # 确定干预提示（思考退化循环 / content死循环 / 精神分裂思考 / 思考瓶颈）
                # 注意：必须作为 user 消息注入，作为 assistant 消息会被模型当成自己的话忽略
                intervention = None
                if degenerate_loop_detected:
                    intervention = self._prompts.degenerate_loop_intervention
                    get_output_manager().write_output(
                        task_id=task_id,
                        content=f"{ANSI_COLORS.get('bright_red')}{intervention}{ANSI_COLORS.get('reset')}\n",
                        source="system",
                    )
                elif content_loop_detected:
                    # content死循环：截断重复内容，只保留第一个唯一行
                    # content可能已经是上千行重复文本，全部保存会撑爆上下文
                    if "\n" in content:
                        first_line = content.split("\n")[0]
                        content = first_line  # 只保留第一行有效内容
                    intervention = self._prompts.content_dead_loop_intervention
                    get_output_manager().write_output(
                        task_id=task_id,
                        content=f"{ANSI_COLORS.get('bright_red')}{intervention}{ANSI_COLORS.get('reset')}\n",
                        source="system",
                    )
                elif wait_hit_count >= self.llm.config.overthinking_threshold:
                    intervention = self._prompts.overthinking_warning
                    get_output_manager().write_output(
                        task_id=task_id,
                        content=f"{ANSI_COLORS.get('bright_yellow')}{intervention}{ANSI_COLORS.get('reset')}\n",
                        source="system",
                    )
                elif thinking_duplicate_count >= self.llm.config.thinking_dead_loop_threshold:
                    intervention = self._prompts.thinking_dead_loop_intervention

                if thinking_to_save:
                    _is_deepseek = "deepseek" in (self.llm.provider or "")
                    history.add_thinking(thinking_to_save, is_deepseek=_is_deepseek)
                    if content:
                        history.add_assistant(content)
                    self._save_history(history)
                    logger.info(
                        f"⚡ [ToolEngine] 事务#{task_id}中断时保存了已接收的thinking内容({len(thinking_to_save)}字符)到history"
                    )
                elif content:
                    # 无thinking但有content（如content死循环场景）：保存截断后的content
                    history.add_assistant(content)
                    self._save_history(history)
                    logger.info(
                        f"⚡ [ToolEngine] 事务#{task_id}中断时保存了content({len(content)}字符)到history"
                    )
                # 干预提示作为 user 消息注入（user 消息有指令权威，assistant 消息会被忽略）
                if intervention:
                    history.add_user(intervention)
                # 对于不支持thinking持久化的模型（如GLM），提醒模型按行号回顾输出文件中的思考内容
                if thinking_to_save and not getattr(self.llm, "provider_config", {}).get(
                    "thinking_persistence", True
                ):
                    self._remind_thinking_in_output(history)
                continue
            # 🔧 事务#2114：思考后无正式内容——注入user提醒（思考成果不会保留在
            # 后续上下文中）。必须注入在 add_assistant **之前**：本轮空 assistant
            # 随后会被空响应处理按 messages[-1]=="assistant" 移除，若提醒在其后
            # 会阻止该移除（连续空响应时空assistant+提醒对不断累积）；
            # 前置注入后空assistant被正常pop，提醒保留为history尾部
            self._remind_thinking_only(history, thinking, content, tool_calls)

            # 6. 更新历史：添加助手消息
            # 当有 tool_calls 时，将 reasoning_content 合并到同一个 assistant 消息中
            # 避免产生两条连续 assistant 消息（DeepSeek 不支持）
            _is_deepseek = "deepseek" in (self.llm.provider or "")
            if thinking and tool_calls:
                # 合并模式：一条消息同时包含 reasoning_content + tool_calls
                # 不调用 add_thinking（避免产生两条连续 assistant 消息）
                history.add_thinking("", is_deepseek=_is_deepseek)
                history_list = []
                history.add_assistant(content, tool_calls, _reasoning=thinking)
            else:
                # 分离模式：thinking 和 assistant 分开存储
                history.add_thinking(thinking, is_deepseek=_is_deepseek)
                history_list = []
                history.add_assistant(content, tool_calls)

            self._save_history(history)
            # 7. 先发送所有工具调用事件（让用户看到并行调用的工具）
            keep_list = []
            for tc in tool_calls:
                # === 钩子系统 v3.0: Before 工具调用 (tools: before) ===
                skip = False
                if hook_manager:
                    from peng.hooks.types import ToolMetadata

                    tool_name = (
                        tc.name
                        if hasattr(tc, "name")
                        else tc.get("function", {}).get("name", "unknown")
                    )
                    tool_args = (
                        tc.arguments
                        if hasattr(tc, "arguments")
                        else json.loads(tc.get("function", {}).get("arguments", "{}"))
                    )

                    tool_metadata = ToolMetadata(name=tool_name, args=tool_args)

                    metadata = {
                        "history": history,
                        "tool": tool_metadata,
                        "task": self._task,
                    }

                    results = await hook_manager.execute_before_hooks(
                        message_id=message_id,
                        task_id=task_id,
                        category="tools",
                        name=tool_name,
                        metadata=metadata,
                    )

                    # 钩子可能修改了 history，tool name 和 args
                    history = metadata["history"]
                    final_tool_name = metadata["tool"].name
                    final_tool_args = metadata["tool"].args
                    for action, reason in results:
                        if action == "block":
                            # 阻止工具调用，reason 不为空则加入 history
                            if reason:
                                history.add_user(" 工具调用被阻止，原因: " + reason)
                            logger.debug(
                                f"[ToolEngine] 钩子阻止工具调用: {tool_name}, 原因: {reason}"
                            )
                            # 跳过这个工具调用
                            skip = True

                    # 更新工具调用（如果钩子修改了）
                    if final_tool_name != tool_name or final_tool_args != tool_args:
                        if hasattr(tc, "name"):
                            tc.name = final_tool_name
                            tc.arguments = final_tool_args
                        else:
                            tc["function"]["name"] = final_tool_name
                            tc["function"]["arguments"] = json.dumps(final_tool_args)
                if not skip:
                    keep_list.append(tc)
                    yield ToolCallEvent(call=tc)
            # 去除被hooks阻止的工具
            tool_calls = keep_list

            # === 工具调用参数为空检测：所有 tool_call 的 arguments 都为空时，视为无效响应，重试 ===
            # 当模型因上下文过长/截断等原因未能正确生成工具参数时，arguments 为 {} 或 None，
            # 此时执行工具必然失败（缺少必填参数），浪费一轮迭代。
            # 正确做法：移除已添加的无效 assistant 消息，注入提示让 LLM 重新生成有效参数后重试。
            if tool_calls:
                _all_empty_args = True
                for tc in tool_calls:
                    _args = (
                        tc.arguments
                        if hasattr(tc, "arguments")
                        else (
                            json.loads(tc.get("function", {}).get("arguments", "{}"))
                            if isinstance(tc.get("function", {}).get("arguments"), str)
                            else tc.get("function", {}).get("arguments") or {}
                        )
                    )
                    if isinstance(_args, dict) and len(_args) > 0:
                        _all_empty_args = False
                        break
                if _all_empty_args:
                    # 移除刚添加的无效 assistant 消息（含空参数 tool_calls）
                    _msgs = getattr(history, "_messages", [])
                    if _msgs and _msgs[-1].get("role") == "assistant":
                        _msgs.pop()
                    # 注入提示让 LLM 重新生成有效参数
                    _empty_args_hint = (
                        "⚠️ 你上次的工具调用参数为空，请重新生成完整的工具调用参数。"
                        "确保每个工具调用的 arguments 包含所有必填字段的有效值。"
                    )
                    history.add_user(_empty_args_hint)
                    self._save_history(history)
                    logger.warning(
                        f"⚡ [ToolEngine] 事务#{task_id} 检测到所有工具调用参数为空"
                        f"({len(tool_calls)}个tool_call)，跳过执行并重试。"
                    )
                    get_output_manager().write_output(
                        task_id=task_id,
                        content=f"{ANSI_COLORS.get('bright_yellow')}⚠️ 工具调用参数为空，重试中...{ANSI_COLORS.get('reset')}\n",
                        source="system",
                    )
                    self._iteration += 1
                    continue

            # 8. 并行执行工具（执行前已创建快照）
            # 获取事务的 nest_name 和 workdir（用于远程备份）
            task_nest = self._task.get("nest") if self._task else None
            task_workdir = self._task.get("workdir") if self._task else None
            if not task_workdir:
                task_workdir = str(get_working_directory())

            # 创建工具调用前的备份快照（同步方法，通过 asyncio.to_thread 避免阻塞事件循环）
            # 工具执行前捕获当前 checklist（回滚时需要恢复到此版本，不能在工具执行后读取）
            pre_tool_checklist = ""
            try:
                from peng.task.manager import TaskManager as _TM_pre

                _tm_pre = _TM_pre()
                _task_pre = _tm_pre.db.get_task(task_id)
                pre_tool_checklist = _task_pre.get("checklist", "") or "" if _task_pre else ""
            except Exception:
                pass

            # 🔧 事务#2064：工具批次（备份快照+并行执行）全程紧急监控。
            # 真空期事故实锤：#2081在E:/aigc/Strata大仓库快照耗时156秒
            # （tool_call事件01:05:10 → bash监控01:07:46才启动），期间用户
            # 感叹号info无任何机制响应——bash内嵌监控在execute内才创建，
            # 管不到快照段。本监控与整个工具批次并行，检测到紧急中断/
            # 控制信号立刻cancel工具批次（to_thread的git快照线程自然跑完
            # 不受影响；工具内同步写段原子不受cancel劈裂），跳到迭代边界
            # 消费处理。
            # 🔧 事务#2087：快照总预算——远程大仓库快照链（git批量快照+外部
            # 文件备份串行堆叠）可达分钟级（#2081实锤152秒：tool_call 02:02:19
            # → bash监控02:05:11才启动），bash 的 timeout 从 execute() 才开始
            # 计时，被快照静默绕过。超预算跳过快照直接执行工具（0=禁用预算）
            from peng.config.loader import get_snapshot_budget_seconds

            _snap_budget = get_snapshot_budget_seconds()
            # 快照提示共享状态（watchdog 写"进行中"，batch 写"已完成"）：
            # 🔧 事务#2088四续：出现"进行中"必须有对应"已完成"收尾，且带
            # 耗时数字让用户能判断慢在哪（快照本身 vs 网络波动）
            _snap_flags: dict = {"notified": False, "t0": time.monotonic()}
            _snap_needed = any(
                ToolBackupManager.should_backup(
                    tc.name if hasattr(tc, "name") else tc.get("function", {}).get("name", "")
                )
                for tc in tool_calls
            )

            async def _run_tool_batch() -> tuple[list, list]:
                """快照+并行执行工具（返回 rollback_metas, tool_exec_results）

                🔧 事务#2087：快照段受 _snap_budget 总预算约束——超时放弃等待
                剩余快照（to_thread 的 git 线程后台自然完成，至多多一个无害
                备份点），未快照的工具 rollback meta 置 None（无回滚链接，
                降级安全：回滚可用本轮迭代开始快照）。
                """
                rollback_metas_inner: list[dict | None] = []

                async def _snapshots() -> None:
                    for tc in tool_calls:
                        tool_name_sn = (
                            tc.name
                            if hasattr(tc, "name")
                            else tc.get("function", {}).get("name", "")
                        )
                        tool_args_sn = (
                            tc.arguments
                            if hasattr(tc, "arguments")
                            else json.loads(tc.get("function", {}).get("arguments", "{}"))
                        )
                        if ToolBackupManager.should_backup(tool_name_sn):
                            try:
                                meta = await asyncio.to_thread(
                                    ToolBackupManager.create_snapshot,
                                    task_id=task_id,
                                    tool_name=tool_name_sn,
                                    tool_args=tool_args_sn,
                                    workdir=task_workdir,
                                    nest_name=task_nest,
                                )
                                rollback_metas_inner.append(meta)
                            except asyncio.CancelledError:
                                raise
                            except Exception as e:
                                logger.debug(f"创建快照失败({tool_name_sn}): {e}")
                                rollback_metas_inner.append(None)
                        else:
                            rollback_metas_inner.append(None)

                if _snap_budget > 0:
                    try:
                        await asyncio.wait_for(_snapshots(), timeout=_snap_budget)
                    except asyncio.TimeoutError:
                        _snap_flags["timeout"] = True
                        _skipped = len(tool_calls) - len(rollback_metas_inner)
                        rollback_metas_inner.extend([None] * _skipped)
                        _warn = (
                            f"⚠️ 备份快照超过 {_snap_budget}s 预算已跳过"
                            f"（{_skipped} 个工具无快照），直接执行工具。"
                            f"回滚请使用本轮迭代开始快照。\n"
                        )
                        get_output_manager().write_output(
                            task_id=task_id,
                            content=f"{ANSI_COLORS.get('bright_yellow')}{_warn}{ANSI_COLORS.get('reset')}",
                            source="system",
                        )
                        logger.warning(
                            f"⚡ [ToolEngine] 事务#{task_id}快照超预算{_snap_budget}s，跳过剩余快照"
                        )
                else:
                    await _snapshots()

                # 🔧 事务#2088六续：快照段结束。竞态修复——watchdog 1s 节流
                # 可能在快照完成后才写"进行中"（#2104 快照10.x秒完成，
                # batch 检查时 notified=False 不写"已完成"，watchdog 11s 才
                # 写"进行中"→ 永远没有收尾）。此处快照结束时自主判断：
                # 已通知过 或 距批次开始>10s（watchdog 可能晚写）都算慢快照
                _snap_flags["snap_done"] = True
                _snap_elapsed = time.monotonic() - _snap_flags["t0"]
                if not _snap_flags.get("timeout") and (
                    _snap_flags["notified"] or _snap_elapsed > 10
                ):
                    _snap_flags["notified"] = True  # 阻止 watchdog 迟到补写"进行中"
                    _n_snaps = sum(1 for m in rollback_metas_inner if m)
                    _snap_done_msg = (
                        f"✅ 备份快照已完成"
                        f"（耗时 {_snap_elapsed:.1f}s，"
                        f"{_n_snaps}/{len(tool_calls)} 个快照），开始执行工具...\n"
                    )
                    get_output_manager().write_output(
                        task_id=task_id,
                        content=f"{ANSI_COLORS.get('bright_green')}{_snap_done_msg}{ANSI_COLORS.get('reset')}",
                        source="system",
                    )

                results = await self._execute_tools_parallel(tool_calls)
                return rollback_metas_inner, results

            async def _urgent_watchdog() -> str | None:
                """工具批次期间的紧急中断看门狗（1秒节流，与思考流检测同源）

                🔧 事务#2087：兼任批次心跳保活（30s节流）+慢快照一次性提示——
                快照/工具执行期间 executor 无 chunk 流（_execute_llm 只在收到
                chunk 时更新心跳），长快照静默期心跳停摆有超时误杀风险；同时
                >10s 的快照向输出写提示，消除"工具已调用却毫无动静"的困惑。
                """
                _batch_start = _snap_flags["t0"]
                _last_hb = _batch_start
                while True:
                    await asyncio.sleep(1)
                    _now = time.monotonic()
                    # 心跳保活：覆盖快照+工具执行全程静默期
                    if _now - _last_hb >= 30:
                        try:
                            self.task_manager.update_heartbeat(task_id)
                        except Exception:
                            pass
                        _last_hb = _now
                    # 慢快照提示（一次性，>10s 且本批次确有快照需求；
                    # 快照段已结束时不再写"进行中"——收尾"已完成"由
                    # _run_tool_batch 在快照结束时自主判断写入（竞态修复））
                    if (
                        not _snap_flags["notified"]
                        and not _snap_flags.get("snap_done")
                        and _snap_needed
                        and _now - _batch_start > 10
                    ):
                        _snap_flags["notified"] = True
                        _slow_msg = (
                            f"⏳ 备份快照进行中（已 {int(_now - _batch_start)}s），"
                            f"快照完成后工具才会执行...\n"
                        )
                        get_output_manager().write_output(
                            task_id=task_id,
                            content=f"{ANSI_COLORS.get('bright_yellow')}{_slow_msg}{ANSI_COLORS.get('reset')}",
                            source="system",
                        )
                    if self._has_pending_interrupts():
                        return "CONTROL"
                    reason = self._has_urgent_interrupts()
                    if reason:
                        return reason

            _batch_task = asyncio.ensure_future(_run_tool_batch())
            _watchdog_task = asyncio.ensure_future(_urgent_watchdog())
            _wd_done, _wd_pending = await asyncio.wait(
                [_batch_task, _watchdog_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            _wd_reason = None
            if _watchdog_task in _wd_done:
                try:
                    _wd_reason = _watchdog_task.result()
                except (asyncio.CancelledError, Exception):
                    pass
            if _wd_reason is not None:
                # 紧急中断/控制信号：cancel工具批次，注入提示，跳迭代消费
                for _t in (_batch_task, _watchdog_task):
                    _t.cancel()
                for _t in (_batch_task, _watchdog_task):
                    try:
                        await _t
                    except (asyncio.CancelledError, Exception):
                        pass
                if _wd_reason == "CONTROL":
                    logger.info(
                        f"⚡ [ToolEngine] 事务#{task_id}工具批次期间检测到控制信号，"
                        f"终止快照/工具执行跳迭代处理"
                    )
                    _wd_msg = "⚠️ 检测到事务控制信号，工具执行已终止。请立刻处理。"
                else:
                    logger.info(
                        f"⚡ [ToolEngine] 事务#{task_id}工具批次期间检测到紧急中断，"
                        f"终止快照/工具执行立刻处理：{_wd_reason}"
                    )
                    _wd_msg = (
                        f"⛔ 紧急中断：{_wd_reason}\n"
                        f"（工具批次已强制终止。该指示以感叹号标记=最高优先级，"
                        f"立刻停止当前行为模式，按上述指示从根本上改变处理方向，"
                        f"禁止继续重复同类操作）"
                    )
                get_output_manager().write_output(
                    task_id=task_id,
                    content=f"{ANSI_COLORS.get('bright_yellow')}{_wd_msg}{ANSI_COLORS.get('reset')}\n",
                    source="system",
                )
                history.add_user(_wd_msg)
                self._save_history(history)
                self._iteration += 1
                continue
            # 正常路径：看门狗cancel，取批次结果
            _watchdog_task.cancel()
            try:
                await _watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            try:
                rollback_metas, tool_exec_results = _batch_task.result()
            except (asyncio.CancelledError, Exception) as _batch_err:
                # 批次异常（非看门狗触发）：维持原异常语义
                raise _batch_err

            # === 🔧 事务#2064：工具调用死循环检测（保守：连续15轮完全同签名） ===
            # 实测#2077事故：模型对同一4行脚本连写31版（r17_go30.py→r17_go31.py…），
            # 每轮文件名递增参数不同但模式完全相同，打断4次仍继续——思考流/
            # content死循环检测管不到工具调用层。签名规范化：路径中数字序列
            # 归一（r17_go30→r17_go<N>）。⚠️阈值必须保守（15轮）：正常批量
            # 文件处理/等待外部结果的轮询也会产生同签名，只在极端重复时
            # 注入【提示性】干预（不跳迭代不打断，由模型自行判断）。
            def _tool_sig(tc_item) -> str:
                """工具调用签名：名称+参数规范化JSON（数字序列→#N，截断300字符）"""
                _name = (
                    tc_item.name
                    if hasattr(tc_item, "name")
                    else tc_item.get("function", {}).get("name", "")
                )
                _args = (
                    tc_item.arguments
                    if hasattr(tc_item, "arguments")
                    else tc_item.get("function", {}).get("arguments", "{}")
                )
                _sig = (
                    f"{_name}:{json.dumps(_args, ensure_ascii=False, default=str, sort_keys=True)}"
                )
                return re.sub(r"\d+", "#N", _sig)[:300]

            _batch_sig = "|".join(sorted(_tool_sig(_tc) for _tc in tool_calls))[:600]
            _loop_count = 0
            if self._tool_batch_sigs and self._tool_batch_sigs[-1] == _batch_sig:
                self._tool_batch_sig_repeat += 1
                _loop_count = self._tool_batch_sig_repeat
            else:
                self._tool_batch_sig_repeat = 0
            self._tool_batch_sigs.append(_batch_sig)
            if len(self._tool_batch_sigs) > 16:
                self._tool_batch_sigs = self._tool_batch_sigs[-16:]
            if _loop_count >= 14:
                self._tool_batch_sig_repeat = 0  # 提示后重置，避免每轮重复注入
                _tl_intervention = (
                    f"ℹ️ 提示：已连续{_loop_count + 1}轮执行完全相同的工具调用组合"
                    f"（参数仅数字/版本号变化）。若你在等待外部结果（daemon/编译/测试）"
                    f"属正常可忽略；若你在反复尝试同类操作且始终无进展，请立刻停止——"
                    f"该方法已被证明无效，换一个根本不同的处理路径，或用report汇报"
                    f"当前状态与卡点。\n"
                )
                logger.warning(
                    f"⚡ [ToolEngine] 事务#{task_id}工具调用高度重复"
                    f"(连续{_loop_count + 1}轮同签名)，注入提示"
                )
                get_output_manager().write_output(
                    task_id=task_id,
                    content=f"{ANSI_COLORS.get('bright_yellow')}{_tl_intervention}{ANSI_COLORS.get('reset')}\n",
                    source="system",
                )
                history.add_user(_tl_intervention)
                self._save_history(history)

            # === 思考过载检测：有效编辑工具调用后重置计数器并恢复思考模式 ===
            _EDIT_TOOLS = frozenset({"write", "edit", "line_edit"})
            if any(
                (tc.name if hasattr(tc, "name") else tc.get("function", {}).get("name", ""))
                in _EDIT_TOOLS
                for tc in tool_calls
            ):
                if self._thinking_overload_bytes > 0 or self._force_thinking_disabled:
                    self._thinking_overload_bytes = 0
                    if self._force_thinking_disabled:
                        self._force_thinking_disabled = False
                        _restore_msg = (
                            f"\n🧠✅ [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                            f"事务#{task_id}检测到有效编辑工作(write/edit/line_edit)，"
                            f"已恢复思考模式。\n"
                        )
                        logger.info(f"[ToolEngine] {_restore_msg.strip()}")
                        get_output_manager().write_output(
                            task_id=task_id,
                            content=f"{ANSI_COLORS.get('bright_green')}{_restore_msg}{ANSI_COLORS.get('reset')}",
                            source="system",
                        )

            # 工具执行后检查变更：无变化的工具不生成回退链接
            for idx, meta in enumerate(rollback_metas):
                if meta is None:
                    continue
                try:
                    rollback_metas[idx] = await asyncio.to_thread(
                        ToolBackupManager.check_post_tool_changes,
                        meta,
                        task_workdir,
                        task_nest,
                    )
                except Exception as e:
                    logger.debug(f"检查工具变更失败(idx={idx}): {e}")

                # 有变更时备份 history context 和 checklist（独立的本地操作，与文件备份无关）
                # 注意：checklist 必须使用工具执行前捕获的 pre_tool_checklist，
                # 不能从 DB 重新读取（因为同批次的 checklist 工具可能已更新 DB）
                if rollback_metas[idx] is not None:
                    try:
                        from peng.service.tool_backup import HistoryBackupManager

                        history_context = json.dumps(
                            history.to_context(
                                thinking_type=self.llm.thinking_type,
                                preserve_thinking=self._provider_preserve_thinking(),
                            ),
                            ensure_ascii=False,
                        )

                        history_commit = await asyncio.to_thread(
                            HistoryBackupManager.backup,
                            task_id,
                            history_context,
                            f"{rollback_metas[idx].get('tool_name', 'unknown')} @ {rollback_metas[idx].get('timestamp', '')}",
                            pre_tool_checklist,
                        )
                        if history_commit:
                            rollback_metas[idx]["history_commit"] = history_commit
                    except Exception as e:
                        logger.debug(f"History备份失败(idx={idx}): {e}")

            hook_block = False

            hook_block = False
            # 9. 处理工具结果并更新历史
            for idx, (tc, result) in enumerate(zip(tool_calls, tool_exec_results, strict=True)):
                # 提取工具名称和参数
                tool_name = (
                    tc.name
                    if hasattr(tc, "name")
                    else tc.get("function", {}).get("name", "unknown")
                )

                # 处理 DualOutput（分离 display 和 summary）
                display_content = None
                summary_content = result
                result_attachments = None
                if isinstance(result, DualOutput):
                    display_content = result.display
                    summary_content = result.summary
                    result_attachments = result.attachments

                # 获取对应的回退元数据
                rollback_meta = rollback_metas[idx] if idx < len(rollback_metas) else None

                yield ToolResultEvent(
                    call=tc,
                    result=summary_content,
                    display=display_content if display_content else summary_content,
                    injections=[],  # 注入现在在 LLM 调用前完成
                    rollback_meta=rollback_meta,
                )

                # 更新历史：添加工具结果
                # 多模态附件（read工具返回的图片/PDF等）：当前provider支持vision
                # 或存在可用的vision provider时，将附件作为 content part 列表注入
                # history（防止模型只看到"图片 读取成功"摘要而凭空编造图片内容）；
                # 后续chat_stream检测到请求携带附件part会自动切换到vision provider
                # 发送（发送成功后strip_sent_attachments清除，只发送一次）。
                # 完全没有vision provider时附件丢弃（保留纯文本摘要，避免history
                # 堆积大体积base64）
                tool_call_id = tc.id if hasattr(tc, "id") else str(id(tc))
                history_content: str | list = summary_content
                if result_attachments and (
                    self._is_multimodal_provider() or self._has_vision_provider_available()
                ):
                    parts: list[dict] = [{"type": "text", "text": str(summary_content)}]
                    for att in result_attachments:
                        if isinstance(att, dict) and att.get("url"):
                            att_type = att.get("type", "file")
                            mime = att.get("mime", "")
                            if mime.startswith("image/"):
                                parts.append(
                                    {"type": "image_url", "image_url": {"url": att["url"]}}
                                )
                            elif mime.startswith("video/"):
                                parts.append(
                                    {"type": "video_url", "video_url": {"url": att["url"]}}
                                )
                            elif mime.startswith("audio/"):
                                b64 = att["url"].split("base64,", 1)[-1]
                                fmt = (
                                    att["url"].split("data:audio/", 1)[-1].split(";", 1)[0]
                                    if "data:audio/" in att["url"]
                                    else "mp3"
                                )
                                parts.append(
                                    {
                                        "type": "input_audio",
                                        "input_audio": {"data": b64, "format": fmt},
                                    }
                                )
                            elif mime == "application/pdf":
                                parts.append({"type": "file", "file": {"url": att["url"]}})
                            else:
                                logger.debug(
                                    f"[ToolEngine] 跳过未知类型附件: type={att_type}, mime={mime}"
                                )
                    if len(parts) > 1:
                        history_content = parts
                        logger.info(
                            f"[ToolEngine] 工具 {tool_name} 结果含 {len(parts) - 1} 个多模态附件，已注入LLM上下文"
                        )
                # 🔧 事务#2114：超长工具结果（超过provider级drop_hint_threshold，
                # 默认1024字节）末尾追加带唯一4位数字id的drop清理提示，
                # 模型确认结果毫无用处时可用drop工具清理以节省上下文空间
                history_content = self._append_drop_hint(history_content)
                history.add_tool_result(tool_call_id, tool_name, history_content)

                tool_args = (
                    tc.arguments
                    if hasattr(tc, "arguments")
                    else json.loads(tc.get("function", {}).get("arguments", "{}"))
                )

                # === 钩子系统 v3.0: After 工具调用 (tools: after) ===
                if hook_manager:
                    from peng.hooks.types import ToolMetadata

                    tool_metadata = ToolMetadata(name=tool_name, args=tool_args, result=result)
                    metadata = {
                        "history": history,
                        "tool": tool_metadata,
                        "task": self._task,
                    }
                    results = await hook_manager.execute_after_hooks(
                        message_id=message_id,
                        task_id=task_id,
                        category="tools",
                        name=tool_name,
                        metadata=metadata,
                    )
                    history = metadata["history"]
                    for action, reason in results:
                        if action == "block":
                            msg_content = (
                                f"🪝  {tool_name}钩子在工具调用后阻止了流程继续 , 原因: {reason}"
                            )
                            logger.debug(msg_content)
                            history.add_user(msg_content)
                            hook_block = True
            # 对于不支持thinking持久化的模型（如GLM），提醒模型按行号回顾输出文件中的思考内容
            # 注意：必须在所有 tool 结果添加到 history 之后执行，否则 add_user 会插入到
            # assistant(tool_calls) 和 tool 结果之间，破坏 API 对话格式
            if thinking and not getattr(self.llm, "provider_config", {}).get(
                "thinking_persistence", True
            ):
                self._remind_thinking_in_output(history)
            # 跨迭代死循环检测：比较本轮与最近几轮的工具调用签名+返回结果指纹
            # 🔧 修复BUG：使用 tool_exec_results（工具执行结果），而非 results（可能被hook覆盖）
            self._check_cross_iteration_loop(
                tool_calls, content, thinking, history, task_id, tool_results=tool_exec_results
            )
            self._iteration += 1
            # LLM发出了工具调用，说明在正常工作，重置连续block计数器
            if tool_calls:
                self._consecutive_blocks = 0
                # 每 N 轮有tool_call的迭代注入PUA（防止长对话中模型"忘记"自身能力定位）
                self._tool_call_iter_count += 1
                pua_threshold = max(3, load_config().llm.pua_threshold)
                if (
                    self._tool_call_iter_count > 1
                    and self._tool_call_iter_count % pua_threshold == 0
                ):
                    history.add_user(self._prompts.pua_text)
            # 安全限制：最多 max_iterations 次迭代（防止无限循环，正常情况不会触发）
            if self._iteration >= self.max_iterations * 0.8:  # 提前20%警告，给用户提示
                logger.warning(f"[ToolEngine] 将达到安全限制 {self.max_iterations} 次迭代")
                if history:
                    history.add_user(
                        self._prompts.iteration_limit_warning.format(
                            iteration=self._iteration,
                            max_iterations=self.max_iterations,
                        )
                    )

            has_completion_signal = False
            # 5. 检查完成信号（即使有工具调用也检查）
            for signal in _COMPLETION_SIGNALS:
                if signal in content:
                    logger.debug(f"[ToolEngine] 检测到完成信号: {signal}")
                    has_completion_signal = True
                    break

            exhausted = False
            if self._iteration >= self.max_iterations:
                msg = self._prompts.iteration_exhausted.format(max_iterations=self.max_iterations)
                logger.warning(msg)
                history.add_user(msg)
                exhausted = True

            should_complete = (
                exhausted or has_completion_signal or hook_block or not tool_calls or should_quit
            )

            # 空响应处理机制：当content为空且无tool_calls时，可能是上下文过长导致模型返回空响应
            if (
                should_complete
                and not exhausted
                and not has_completion_signal
                and not hook_block
                and not should_quit
                and not tool_calls
                and not content.strip()
            ):
                est_tokens = history.get_token_count()

                # 移除本轮空assistant消息，避免空消息累积
                messages = getattr(history, "_messages", [])
                if (
                    messages
                    and messages[-1].get("role") == "assistant"
                    and not messages[-1].get("tool_calls")
                ):
                    messages.pop()

                self._consecutive_empty += 1

                if self._consecutive_empty <= 2:
                    # tokens未超限但空响应：可能是一时问题，直接重试（不结束事务）
                    warn_msg = (
                        f"⚠️ [toolengine] 事务#{task_id} 空响应(tokens={est_tokens})，"
                        f"直接重试（{self._consecutive_empty}/2）。"
                    )
                    logger.warning(warn_msg)
                    get_output_manager().write_output(
                        task_id=task_id,
                        content=f"{ANSI_COLORS.get('bright_yellow')}{warn_msg}{ANSI_COLORS.get('reset')}\n",
                        source="system",
                    )
                    should_complete = False
                elif (
                    self._consecutive_empty <= 5 and est_tokens > effective_compact_threshold * 0.5
                ):
                    # tokens超限：执行compact后重试
                    ratio = 1 - (self._consecutive_empty) * 0.16
                    ratio = max(0.36, ratio)
                    orig, after_compact = history.compact(effective_compact_threshold * ratio, 0.6)
                    if orig > after_compact:
                        history_list = []  # 跳过增量异常检测
                        # compact 后历史被压缩，nest/workdir 信息可能丢失，重置跟踪变量以便重新注入
                        self._last_injected_nest = None
                        self._last_injected_workdir = None
                    warn_msg = (
                        f"⚠️ [toolengine] 事务#{task_id} 空响应(tokens={est_tokens})，"
                        f"compact: {orig} → {after_compact}，继续重试"
                        f"（{self._consecutive_empty}/5）"
                    )
                    logger.warning(warn_msg)
                    _sys_err = (
                        f"{ANSI_COLORS.get('bright_yellow')}{warn_msg}{ANSI_COLORS.get('reset')}\n"
                    )
                    if self._should_report_system_error(_sys_err):
                        get_output_manager().write_output(
                            task_id=task_id,
                            content=_sys_err,
                            source="system",
                        )
                    should_complete = False
                else:
                    # 清空上下文重启事务处理
                    # （🔧 事务#2084 Fix3：经熔断器防死循环——反复空响应restart
                    # 无效时15分钟内第3次起改为pause，避免死循环烧钱）
                    warn_msg = (
                        f"⚠️ [toolengine] 事务#{task_id} 空响应(tokens={est_tokens})，"
                        f"compact处理无效，清空上下文重启事务处理"
                    )
                    logger.warning(warn_msg)
                    get_output_manager().write_output(
                        task_id=task_id,
                        content=f"{ANSI_COLORS.get('bright_yellow')}{warn_msg}{ANSI_COLORS.get('reset')}\n",
                        source="system",
                    )
                    self._engine_restart_with_breaker(
                        task_id, detail=f"空响应compact无效(tokens={est_tokens})"
                    )
                    should_complete = True
            elif content.strip() or tool_calls:
                # 有有效内容或工具调用，重置空响应计数
                self._consecutive_empty = 0

            # 完成信号的钩子判断（如果工具结果钩子没有提前结束）
            if should_complete and not should_quit:
                # === 钩子系统 v3.0: Before 完成 (flow: complete) ===
                from peng.hooks.types import FlowMetadata

                flow_metadata = FlowMetadata(name="complete", args={"response": content})
                metadata = {
                    "history": history,
                    "flow": flow_metadata,
                    "task": self._task,
                }

                results = await hook_manager.execute_before_hooks(
                    message_id=message_id,
                    task_id=task_id,
                    category="flow",
                    name="complete",
                    metadata=metadata,
                )

                history = metadata["history"]
                for action, reason in results:
                    if action == "block" and reason:
                        # 钩子阻止大模型调用结束
                        self._consecutive_blocks += 1
                        if self._consecutive_blocks >= self._max_consecutive_blocks:
                            # LLM连续多次不处理block信息，回滚history改变上下文环境并注入引导指令
                            # 每多block一次，多回滚3条（assistant+user），让LLM看到不同的历史末尾
                            rollback_rounds = (
                                self._consecutive_blocks - self._max_consecutive_blocks + 5
                            )
                            rollback_count = rollback_rounds * 3
                            messages = getattr(history, "_messages", [])
                            # 移除本轮空assistant
                            if not content.strip() and not tool_calls:
                                if messages and messages[-1].get("role") == "assistant":
                                    messages.pop()
                                    rollback_count -= 1
                            # 回滚更多history（之前的空assistant+hint对），改变LLM看到的上下文
                            # 文件写入/编辑的tool_call和tool消息停止回滚（有实际副作用），其他可回滚
                            rolled = 0
                            while rollback_count > 0 and len(messages) > 10:
                                messages.pop()
                                rollback_count -= 1
                                rolled += 1
                            logger.warning(
                                f"[ToolEngine] 事务#{task_id} 连续block {self._consecutive_blocks} 次"
                                f"，回滚了 {rolled} 条history并注入引导指令"
                            )
                            # 注入引导指令
                            history.add_assistant(f"🪝 我需要确认，{reason}")
                        else:
                            # 检查history末尾最近的user消息是否已有相同hint，避免重复追加导致死循环膨胀
                            messages = getattr(history, "_messages", [])
                            already_has_hint = False
                            for msg in reversed(messages[-3:]):
                                if msg.get("role") == "user":
                                    if reason.strip() in str(msg.get("content", "")):
                                        already_has_hint = True
                                    break
                            if not already_has_hint:
                                history.add_assistant(f"🪝 我需要确认，{reason}")
                            info = f"⚠️ [ToolEngine] 事务#{task_id} 钩子阻止大模型结束({self._consecutive_blocks}/{self._max_consecutive_blocks}): {reason}"
                            logger.info(info)
                        should_complete = False
                        break
            if should_complete:
                self._save_history(history)
                # 无钩子提示，正常结束（使用钩子可能修改过的 response）
                yield CompleteEvent(content=content, iterations=self._iteration)
                return
            # 继续下一轮迭代

    async def _execute_tools_parallel(self, tool_calls: list[Any]) -> list[str | DualOutput]:
        """
        并行执行多个工具调用

        Args:
            tool_calls: 工具调用列表

        Returns:
            执行结果列表（与输入顺序一致，可能是字符串或 DualOutput）

        Raises:
            ToolError: 如果bash工具执行失败
        """
        if not tool_calls:
            return []

        tasks = [self._execute_tool(tc) for tc in tool_calls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 处理异常结果
        final_results: list[str | DualOutput] = []
        for result in results:
            if isinstance(result, Exception):
                # 所有异常都返回错误消息给大模型
                final_results.append(json.dumps({"error": str(result)}, ensure_ascii=False))
            else:
                final_results.append(result)

        return final_results

    def _check_line_edit_read_required(self, tool_args: dict) -> str | None:
        """检查 line_edit 操作前是否已 read 了同一文件的相关行号区域

        扫描对话历史中的 read 工具调用，找到对同一 filePath 的 read 操作，
        检查其读取的行号范围是否覆盖 line_edit 的操作行号范围。

        Args:
            tool_args: line_edit 工具的参数字典

        Returns:
            None: 检查通过（已 read 且覆盖）
            str: 错误提示信息（检查未通过时）
        """
        file_path = tool_args.get("filePath", "")
        operation = tool_args.get("operation", "")

        # 🔧 事务#2083：filePath 缺失时先给出根因明确的报错（区分"参数缺失"与
        # "未先read"），避免误报"未检测到read调用"误导大模型反复无效重试
        # （事务#2080事故：3次line_edit均漏传filePath，被误导为read范围/
        # 路径格式问题，浪费3轮重试后才改用edit工具脱困）
        if not file_path or not str(file_path).strip():
            return (
                "❌ line_edit 拒绝执行：缺少必填参数 filePath（要编辑文件的绝对路径）。"
                "请检查本次工具调用参数，补上 filePath 后重试；"
                "这不是 read 未覆盖问题，无需重复 read。"
            )

        # 计算 line_edit 操作影响的行号范围
        start_line = None
        end_line = None
        if operation in ("delete", "replace"):
            start_line = tool_args.get("startLine")
            end_line = tool_args.get("endLine")
        elif operation == "insert":
            line = tool_args.get("line")
            if line is not None:
                start_line = int(line)
                end_line = int(line)

        if start_line is not None:
            start_line = int(start_line)
        if end_line is not None:
            end_line = int(end_line)

        # 扫描历史消息，查找对同一文件的 read 工具调用
        messages = getattr(self._history, "_messages", [])
        read_ranges: list[tuple[int, int]] = []  # [(read_start, read_end), ...]

        for msg in messages:
            # assistant 消息中包含 tool_calls
            if msg.get("role") != "assistant":
                continue
            tool_calls = msg.get("tool_calls", [])
            for tc in tool_calls:
                func = tc.get("function", {})
                tc_name = func.get("name", "")
                if tc_name != "read":
                    continue
                # 解析 read 的参数
                args_str = func.get("arguments", "{}")
                try:
                    if isinstance(args_str, str):
                        args = json.loads(args_str)
                    else:
                        args = args_str
                except (json.JSONDecodeError, TypeError):
                    continue
                read_path = args.get("filePath", "")
                if read_path != file_path:
                    continue
                # read 的行号范围
                read_offset = int(args.get("offset", 1))
                read_limit = int(args.get("limit", 2000))
                read_start = read_offset
                read_end = read_offset + read_limit - 1
                read_ranges.append((read_start, read_end))

        if not read_ranges:
            return (
                f"❌ line_edit 拒绝执行：未检测到对文件 {file_path} 的 read 调用。"
                f"请先使用 read 工具读取该文件的相关行号区域，再使用 line_edit。"
            )

        # 检查 read 的行号范围是否覆盖 line_edit 的操作行号范围
        if start_line is not None and end_line is not None:
            covered = False
            for r_start, r_end in read_ranges:
                if r_start <= start_line and r_end >= end_line:
                    covered = True
                    break
            if not covered:
                return (
                    f"❌ line_edit 拒绝执行：read 读取的行号范围未覆盖 line_edit 操作区域"
                    f"(操作行号 {start_line}-{end_line})。"
                    f"请先使用 read 工具读取该文件的行号区域 "
                    f"(至少覆盖第 {start_line}-{end_line} 行)，再使用 line_edit。"
                )

        return None

    async def _execute_tool(self, tool_call: Any) -> str | DualOutput:
        """
        执行单个工具调用

        Args:
            tool_call: 工具调用对象

        Returns:
            执行结果（字符串或 DualOutput，保留 display 用于 diff 显示）
        """
        try:
            # 阶段9：暂停检测（工具调用前）
            if self._task_id is not None:
                try:
                    from peng.webui.websocket import get_pause_signal

                    pause_signal = get_pause_signal(self._task_id)
                    if pause_signal == "paused":
                        logger.debug(f"[ToolEngine] 检测到暂停信号，事务 #{self._task_id} 即将暂停")

                        # 保存关键上下文到事务数据
                        pause_context = {
                            "message_id": self._message_id,
                            "task_id": self._task_id,
                            "paused_at": datetime.now().isoformat(),
                            "current_iteration": self._iteration,
                        }

                        # 添加到全局暂停列表
                        from peng.webui.websocket import add_paused_task

                        add_paused_task(self._task_id, pause_context)

                        # 抛出暂停异常
                        from peng.task.task_types import TaskExecutionError

                        raise TaskExecutionError(f"事务 #{self._task_id} 已暂停")

                except ImportError:
                    pass  # webui模块不可用（命令行模式）

            # 提取工具名称和参数
            if hasattr(tool_call, "name"):
                name = tool_call.name
                arguments = tool_call.arguments
            else:
                # 字典格式
                func = tool_call.get("function", {})
                name = func.get("name", "")
                arguments = json.loads(func.get("arguments", "{}"))

            # line_edit 前置检查：必须先 read 同一文件的相关行号区域
            if name == "line_edit" and self._history:
                error_msg = self._check_line_edit_read_required(arguments)
                if error_msg:
                    return DualOutput(summary=error_msg, display=error_msg)

            result = await self.tools.execute(name, arguments)

            # 保留 DualOutput 对象，让上层处理 display
            if isinstance(result, DualOutput):
                return result
            if isinstance(result, str):
                return result
            return json.dumps(result, ensure_ascii=False, indent=2)

        except Exception as e:
            # 所有异常都返回错误消息给大模型，而不是抛出异常
            # 这样大模型可以知道工具执行失败并继续处理
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    # 工具类型映射常量（避免每次调用创建字典）
    _TOOL_TYPE_NAMES: dict[str, str] = {
        "read": "📃 读取文件",
        "write": "📝 写入文件",
        "edit": "✏️ 编辑文件",
        "line_edit": "✏️ 按行编辑",
        "glob": "🔍 文件搜索",
        "grep": "🔎 内容搜索",
        "bash": "🚩 执行命令",
        "cmd": "🚩 执行命令",
        "web_search": "🌐 网络搜索",
        "web_fetch": "📄 获取网页",
    }

    # 这些工具的参数太冗长，工具调用结果记录时只显示工具类型+文件路径
    # （line_edit 的结果 display 已含文件路径+diff，头部不再重复参数）
    _TOOL_RESULT_NO_ARGS: frozenset[str] = frozenset(
        {"write", "edit", "line_edit", "checklist", "report", "message"}
    )

    # 上述工具中包含文件路径参数的工具
    _TOOL_RESULT_HAS_PATH: frozenset[str] = frozenset({"write", "edit", "line_edit"})

    def _format_tool_call_info(self, tool_name: str, arguments: dict) -> str:
        """格式化工具调用信息（始终显示完整参数）

        write/edit/line_edit 工具的文件内容参数按扩展名做语法高亮、以
        人类易读的多行格式显示；report/checklist 的 markdown 内容参数
        同样做高亮易读显示（详见 peng.common.code_display）；其余工具
        保持单行 JSON 格式。
        """

        tool_type = self._TOOL_TYPE_NAMES.get(tool_name, f"🛠️ {tool_name}")

        if isinstance(arguments, dict):
            # write/edit/line_edit：文件内容人类易读+语法高亮显示
            if tool_name in ("write", "edit", "line_edit"):
                try:
                    # purpose 显示：📌 目的: xxx（缺失时容错不显示）
                    raw_purpose = arguments.get("purpose")
                    purpose_text = (
                        f"📌 目的: {raw_purpose.strip()} "
                        if isinstance(raw_purpose, str) and raw_purpose.strip()
                        else ""
                    )

                    from peng.common.code_display import (
                        format_edit_display,
                        format_line_edit_display,
                        format_write_display,
                    )

                    file_path = arguments.get("filePath", "") or ""
                    if tool_name == "write":
                        content = arguments.get("content", "")
                        if file_path and isinstance(content, str):
                            return f"{tool_type} {purpose_text}{format_write_display(file_path, content)}"
                    elif tool_name == "edit":
                        old_string = arguments.get("oldString", "")
                        new_string = arguments.get("newString", "")
                        if (
                            file_path
                            and isinstance(old_string, str)
                            and isinstance(new_string, str)
                        ):
                            return (
                                f"{tool_type} {purpose_text}"
                                f"{format_edit_display(file_path, old_string, new_string)}"
                            )
                    else:  # line_edit
                        if file_path:
                            return (
                                f"{tool_type} {purpose_text}"
                                f"{format_line_edit_display(file_path, arguments)}"
                            )
                except Exception as e:
                    # 格式化失败不影响主流程，回退到通用 JSON 显示
                    logger.debug(f"write/edit 参数易读格式化失败，回退JSON显示: {e}")

            # report/checklist：markdown 内容人类易读+高亮显示
            elif tool_name in ("report", "checklist"):
                try:
                    from peng.common.code_display import format_markdown_display

                    content = arguments.get("summary") or arguments.get("checklist_content") or ""
                    if isinstance(content, str) and content.strip():
                        return f"{tool_type} {format_markdown_display(content)}"
                except Exception as e:
                    logger.debug(f"report/checklist 参数易读格式化失败，回退JSON显示: {e}")

        # 显示完整参数
        try:
            args_json = json.dumps(arguments, ensure_ascii=False, indent=2)
            # 移除换行，保持在一行
            args_json = args_json.replace("\n", "\\n").replace("  ", " ")
            return f"{tool_type} {args_json}"
        except Exception:
            # JSON序列化失败，使用字符串表示
            return f"{tool_type} {str(arguments)}"

    def _format_tool_result_info(self, tool_name: str, arguments: dict) -> str:
        """格式化工具调用结果信息

        🔧 事务#1819：结果行只显示关键参数摘要，不再全量回显参数JSON ——
        调用参数已在 tool_call 行完整显示过（agent.py ToolCallEvent），
        tool_result 行再回显一遍造成每次调用参数出现两遍，是事务#1818
        输出冗余混乱的主要来源之一。此处仅提取定位性关键参数（路径/
        模式/查询词/命令摘要）帮助快速识别是哪次调用。
        """

        tool_type = self._TOOL_TYPE_NAMES.get(tool_name, f"🛠️ {tool_name}")

        # 参数冗长的工具：只显示工具类型（+文件路径）
        if tool_name in self._TOOL_RESULT_NO_ARGS:
            if tool_name in self._TOOL_RESULT_HAS_PATH:
                file_path = arguments.get("filePath", "")
                if file_path:
                    return f"{tool_type} {file_path}"
            return tool_type

        # 🔧 事务#1819：其余工具提取定位性关键参数摘要（截断防长内容）
        _KEY_PARAMS = ("filePath", "path", "pattern", "query", "url", "args")
        parts = []
        for key in _KEY_PARAMS:
            val = arguments.get(key)
            if isinstance(val, str) and val.strip():
                v = val.strip().replace("\n", " ")
                if len(v) > 80:
                    v = v[:80] + "…"
                parts.append(v)
                break  # 只取第一个命中的关键参数，保持结果行简短
        if tool_name in ("bash", "cmd"):
            cmd = arguments.get("command", "")
            if isinstance(cmd, str) and cmd.strip():
                c = " ".join(cmd.split())
                parts.insert(0, c[:80] + ("…" if len(c) > 80 else ""))
        if parts:
            return f"{tool_type} {parts[0]}"
        return tool_type

    def task_stopped(self):
        task_id = self._task_id
        if task_id and self.task_manager:
            task = self.task_manager.db.get_task(task_id)
            if not task:
                return True
            status = task.get("status")
            return status not in ("running", "accepting")
        return True

    def duplicated_process(self):
        import os

        if not self._task_id:
            return True

        current = os.getpid()
        # 每次从数据库重新读取 process_id，不使用缓存的 self._task
        # 否则当新进程启动并覆盖了 process_id 后，旧进程的缓存不会更新，
        # 导致 current == pid 永远为 True，无法检测到重复进程
        task = self.task_manager.db.get_task(self._task_id)
        if not task:
            return True

        pid = task.get("process_id")
        if pid is None or pid == 0:
            # process_id 被清空（事务已完成/失败/被取消），当前进程应退出
            text = f"🚩 事务 #{self._task_id} 的 process_id 已被清空，当前进程#{current}应退出🟥\n"
            text = f"{ANSI_COLORS.get('bright_yellow')}{text}{ANSI_COLORS.get('reset')}"
            logger.info(text)
            return True
        if current != pid:
            text = f"🚩 事务 #{self._task_id} 的进程#{current}异常，数据记录进程为#{pid}，可能原因：事务被重启，立刻结束旧进程#{current}🟥\n"
            text = f"{ANSI_COLORS.get('bright_yellow')}{text}{ANSI_COLORS.get('reset')}"
            logger.info(text)
            return True
        return False

    async def _aclose_llm_stream(self, stream) -> None:
        """🔧 事务#2178：显式关闭chat_stream异步生成器（幂等清理）

        根因：紧急中断/无效输出检测break跳出async for时，Python不会自动关闭
        被消费的异步生成器——chat_stream的finally（关httpx连接、停prefill追踪、
        释放信号量）直到下一轮chat_stream重赋值触发GC才被执行。窗口期内被打断
        的请求在服务端继续生成往没人读的socket吐字，僵尸请求占着全局信号量槽位
        和GPU KV/批处理槽，多事务共享server时挤爆KV池→前缀缓存被驱逐→
        其他请求从头prefill；writer阻塞→SSE停滞240s→切换重试/心跳杀进程卡死。

        GeneratorExit/CancelledError路径同理：本生成器被外部关闭时，
        局部变量引用的嵌套生成器不会被自动关闭，需显式aclose。
        已自然耗尽的生成器aclose为no-op，可安全重复调用。
        """
        if stream is None:
            return
        try:
            await stream.aclose()
        except asyncio.CancelledError:
            # 清理路径尽力而为：取消期间aclose可能再次被取消，不掩盖原始流程
            logger.debug(f"[ToolEngine] 事务#{self._task_id}关闭LLM流生成器时被取消(忽略)")
        except Exception as e:
            logger.debug(f"[ToolEngine] 关闭LLM流生成器失败(忽略): {e}")

    def exit_on_unexpected_status(self):
        # 监测父进程是否存在（如果父进程被杀死，子进程也应该退出）
        import os

        pid = os.getpid()

        if self.task_stopped():
            text = f"🚩 事务 #{self._task_id} 已被停止或被删除，立刻结束处理进程#{pid}🟥\n"
            text = f"{ANSI_COLORS.get('bright_yellow')}{text}{ANSI_COLORS.get('reset')}"
            logger.info(text)
            # 事务进入终态时清理nest端agent-browser浏览器进程
            self._cleanup_browser_if_terminal()
            sys.exit(1)

        if os.getppid() == 1:  # 父进程已结束
            text = f"🚩 事务 #{self._task_id} 的父进程已结束，立刻结束处理进程#{pid}🟥\n"
            text = f"{ANSI_COLORS.get('bright_yellow')}{text}{ANSI_COLORS.get('reset')}"
            logger.info(text)
            sys.exit(1)

        if self.duplicated_process():
            sys.exit(1)

    def _cleanup_browser_if_terminal(self):
        """事务进程退出前，检查事务是否处于终态（completed/cancelled/failed），如果是则清理浏览器

        paused 状态不清理（恢复后可能继续使用浏览器）。
        使用 fire-and-forget 后台线程异步清理，不阻塞进程退出。
        """
        try:
            task_id = self._task_id
            if not task_id:
                return
            task = self.task_manager.db.get_task(task_id)
            if not task:
                return
            status = task.get("status", "")
            # 只有终态才清理浏览器，paused不清理
            if status in ("completed", "cancelled", "failed"):
                nest_name = task.get("nest")
                if nest_name:
                    logger.info(
                        f"[ToolEngine] 事务#{task_id} 终态={status}，清理 nest '{nest_name}' 的 agent-browser"
                    )
                    import threading

                    def _do_cleanup(nest: str):
                        try:
                            import asyncio
                            from peng.nest.client import bash

                            async def _async():
                                try:
                                    await bash(
                                        "agent-browser close --all", timeout=10000, nest_name=nest
                                    )
                                except Exception:
                                    pass

                            asyncio.run(_async())
                        except Exception:
                            pass

                    # fire-and-forget 后台线程，不阻塞进程退出
                    thread = threading.Thread(target=_do_cleanup, args=(nest_name,), daemon=True)
                    thread.start()
        except Exception as e:
            logger.debug(f"检查事务终态清理浏览器失败: {e}")

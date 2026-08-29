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
from peng.task.output_manager import get_output_manager
from peng.tools.base import DualOutput, Tool, ToolError
from peng.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# 完成信号常量
_COMPLETION_SIGNALS = frozenset({"ANALYSIS_DONE", "TASK_DONE", "ACCEPTANCE_DONE", "DONE"})

# 退化循环检测安全验证行：用于检测pattern是否过于宽泛（会误报正常思考行）
# 例如未转义的字符类 [STOP LOOP] 会匹配任意S/T/O/P/空格/L字符，导致几乎每行都命中
_SAFE_THINKING_LINE = "This is a normal thinking line about the current task."


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

    def size(self):
        return len(self._messages) + 1

    def add_system(self, content: str) -> None:
        """添加系统消息"""
        if not self._system_message:
            self._system_message = {"role": "system", "content": content}
        else:
            self._system_message["content"] += "\n" + content

    def mark_iteration_start(self) -> None:
        """标记新迭代开始，防止跨迭代合并user消息破坏上下文前缀缓存"""
        self._user_merge_floor = len(self._messages)

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

    def to_api_format(self, thinking_type: str | None = None, vision: bool = False) -> list[dict]:
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

        输出规则：
        1. 纯 thinking 消息（_thinking_only=True）：
           - thinking_type="thinking" 时，如果下一条是带 tool_calls 的 assistant，则合并
           - thinking_type != "thinking" 时：跳过（GLM 等不复用 thinking）
           - 否则输出为 {"role": "assistant", "content": "", "reasoning_content": "..."}
        2. 带 reasoning_content 的 assistant + tool_calls 消息：直接输出
        3. 普通 assistant 消息：直接输出
        4. user 消息 content 为多模态列表时：按 vision 参数保留或降级
        """
        result = []
        if self._system_message:
            result.append(self._system_message)

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
                if thinking_type != "thinking":
                    # 非 thinking 模式：跳过纯 thinking 消息
                    # 实测 GLM5.2 等不会复用转换的 thinking 内容，总是重新思考
                    i += 1
                    continue
                # thinking_type == "thinking"：检查是否可以合并到下一个 assistant 消息
                next_msg = self._messages[i + 1] if i + 1 < len(self._messages) else None
                if (
                    next_msg
                    and next_msg.get("role") == "assistant"
                    and "tool_calls" in next_msg
                    and not next_msg.get(self._THINKING_ONLY)
                    and "reasoning_content" not in next_msg
                ):
                    # 合并到下一条带 tool_calls 的 assistant 消息
                    # 先清理前一条 assistant 的未响应 tool_calls
                    _flush_pending_cleanup()
                    merged = _clean_output(next_msg)
                    merged["reasoning_content"] = msg.get("reasoning_content", "")
                    result.append(merged)
                    pending_tc_idx = len(result) - 1
                    i += 2
                    continue
                else:
                    # 独立输出
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
                if thinking_type == "thinking":
                    out = _clean_output(msg)
                    result.append(out)
                else:
                    # 非 thinking 模式：去掉 reasoning_content
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

            # DeepSeek thinking mode 要求：带 tool_calls 的 assistant 必须有 reasoning_content
            if (
                thinking_type == "thinking"
                and msg.get("role") == "assistant"
                and "tool_calls" in msg
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
                    # 回溯合并：将 reasoning_content 合并到当前 tool_calls 消息
                    out["reasoning_content"] = result[-1].pop("reasoning_content", "")
                    result.pop()  # 移除独立的 thinking 消息
                else:
                    # 无前置 thinking 可合并（如模型直接输出 tool_calls 无 thinking）
                    # 添加空 reasoning_content 满足 DeepSeek API 要求
                    out["reasoning_content"] = ""

            result.append(out)
            if msg.get("role") == "assistant" and "tool_calls" in msg:
                pending_tc_idx = len(result) - 1
            i += 1

        # 尾部清理：最后一条如果是带 tool_calls 的 assistant
        _flush_pending_cleanup()

        return result

    def to_context(self, thinking_type: str | None = None, vision: bool = True) -> list | dict:
        """转换为可持久化的context格式（包含元数据）

        Args:
            thinking_type: 当前 provider 的 thinking 处理类型
            vision: 持久化默认保留多模态内容（True），上下文恢复后再按
                    当前 provider 决定是否降级

        Returns:
            新格式 dict: {"messages": [...], "_prompt_tokens": N, "_total_tokens_consumed": N}
        """
        return {
            "messages": self.to_api_format(thinking_type=thinking_type, vision=vision),
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

    def strip_sent_attachments(self) -> int:
        """清除history中已发送的多模态附件part（只发送一次机制）

        多模态附件（base64图片/视频/音频/PDF）体积大且多数provider的视觉token
        不享受缓存折扣，留在history中会导致后续每轮LLM调用重复发送。
        在LLM调用成功完成后调用本方法，将已随本次请求发送的附件part移除：

        1. text part保留，追加附件清除说明（同步替换原"已直接注入"提示，
           避免清除附件后误导模型以为仍能看到附件）
        2. 只剩单个text part时content简化为str（与普通消息格式一致）
        3. 纯附件消息content替换为清除说明（防御性，正常总有text part）

        Returns:
            清除的附件part总数（0表示无附件被清除）
        """
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
            text = (text + "\n" if text else "") + self._ATTACHMENT_REMOVED_NOTICE
            msg["content"] = text
        return removed

    # 多模态附件 part 的估算 token 数（qwen-vl 图像约数百token，
    # base64字符长度/3.5 会严重高估，按每附件固定值估算）
    _MULTIMODAL_PART_ESTIMATED_TOKENS = 1000

    def estimate_tokens(self, begin=0, end=-1) -> int:
        """估算 token 数量（基于实际 API payload 大小）

        使用消息的 JSON 序列化大小来估算，确保与 API 发送内容一致。
        估算规则：JSON 字符长度 / 3.5（混合中英文的平均 token 比率）
        多模态 content part（image_url/video_url/input_audio/file）按固定
        附件 token 数估算（provider 按视觉token计费，非文本字符数）。
        """
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
            content = msg.get("content") if isinstance(msg, dict) else None
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
                msg_str = json.dumps(msg, ensure_ascii=False)
            except (TypeError, ValueError):
                msg_str = str(msg)
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

    def get_token_count(self, begin=0, end=-1, estimate=True) -> int:
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
        """
        if self._prompt_tokens > 0 or not estimate:
            return self._prompt_tokens
        return self.estimate_tokens(begin, end)

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

        # 思考过载检测：跨迭代累积thinking字节数，超阈值且无有效编辑工作时强制关闭思考模式
        self._thinking_overload_bytes: int = 0  # 累积thinking字节数
        self._force_thinking_disabled: bool = False  # 是否强制关闭思考模式（仅影响当前事务）

        # thinking回顾扫描游标：上次已消费的输出文件行号（提醒时从游标后扫描thinking行，
        # run_stream 每轮开始重置为当前文件行数以跳过历史内容）
        self._thinking_scan_line: int = 0

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

                # 纯数字token视为"其他内容"打断重复链（#1688修复，替代#1653二轮的skip）：
                # 数学/代码文本中数字常量会隔在同名变量之间（如#1688实测误判
                # "√drive) + 6000×drive³? drive=0.033"中drive连续3次、中间夹6000），
                # 跳过不重置→误判复读→正常思考被丢弃。数字token重置计数后：
                # 数字常量自然重复（"2/(0.58*ext)*(2/2.2)"中"2"多次）不会累积
                # （重置后count重新从1开始），真正的字母/中文复读仍可检出。
                if token.isdigit():
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
        # 检测如"好的好的好的好的好的好的"这类短词组复读模式
        if not detected and threshold >= 4:
            import re as _re

            # 只检查buf尾部足够长的区域（threshold*4个CJK字符 + 余量）
            tail_len = min(len(buf), threshold * 6)
            tail = buf[-tail_len:] if len(buf) > tail_len else buf

            # 匹配：同一2-4字CJK序列连续出现threshold次（无分隔或仅标点分隔）
            pattern = _re.compile(
                r"([\u4e00-\u9fff\u3400-\u4dbf]{2,4})\1{" + str(threshold - 1) + r"}"
            )
            if pattern.search(tail):
                detected = True

        # --- 更新状态 ---
        if detected:
            state.clear()  # 检测到后重置全部状态（调用方会break停止流式处理）
        else:
            state["last"] = last
            state["count"] = count
            state["buf"] = pending_tail  # ""=无pending，非空=等待下轮补全英文单词

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
        4. 重复次数≥threshold 且 unit含字母或中文（排除纯符号/纯数字误报）→ 检出。

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
            # unit必须含字母或中文（排除纯符号/纯数字/纯空白）
            if not any(c.isalpha() for c in unit):
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
                content=f"\n{ANSI_COLORS.get('bright_red')}{fail_msg}{ANSI_COLORS.get('reset')}\n",
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
            content=f"\n{ANSI_COLORS.get('bright_red')}{hint}{ANSI_COLORS.get('reset')}\n",
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
        检测事务是否有待处理的中断信号：
        1. 新的未读回复消息
        2. 未读的info指示
        3. 未处理的控制信号（cancel/pause/resume/restart）

        Returns:
            True 表示有中断信号，应该停止当前思考跳过本次迭代
        """
        task_id = self._task_id
        message_id = self._message_id
        if not task_id:
            return False

        # 1. 检查新的未读回复消息（仅根事务）
        if not self.task_manager.get_parent_id(task_id):
            try:
                processing_messages = self._get_processing_messages(message_id)
                if processing_messages:
                    logger.info(
                        f"⚡ [ToolEngine] 事务#{task_id}检测到新回复消息，停止思考跳过本次迭代"
                    )
                    return True
            except Exception as e:
                logger.debug(f"[ToolEngine] 检查新回复消息失败: {e}")

        # 2. 检查未读info
        try:
            unread_items = self.task_manager.db.get_task_info(task_id, mode="unread")
            if unread_items:
                logger.info(f"⚡ [ToolEngine] 事务#{task_id}检测到未读info，停止思考跳过本次迭代")
                return True
        except Exception as e:
            logger.debug(f"[ToolEngine] 检查未读info失败: {e}")

        # 3. 检查未处理的控制信号
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

    def _check_workdir_conflicts(self):
        """检测并通知 nest+workdir 冲突的运行中事务

        在事务 start/restart/resume 时调用。
        查找与本事务 nest 和 workdir 都相同的其他**根**运行中事务，
        对每个冲突事务注入 info 提示；本事务自身的警告保存到
        self._workdir_conflict_warnings，由迭代循环内 _get_task_info
        中已验证安全的 history.add_user 点注入（不打断当前thinking/操作，
        由迭代自然处理）。

        冲突判定：nest 非空 且 workdir 非空，且两者都完全相同。
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
                if t_nest == my_nest and t_workdir == my_workdir:
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
                f"请特别注意避免编辑同一文件，避免使用git reset/checkout/revert等危险操作，多用git diff核对变更，切勿覆盖对方有效代码！"
            )
            self._workdir_conflict_warnings.append(f"\n{self_info}\n")

            # 向每个冲突事务注入 info
            for t in conflict_tasks:
                tid = t["id"]
                other_info = (
                    f"⚠️ 冲突警告：检测到新启动的事务#{my_task_id} 与本事务使用相同的 nest({my_nest}) 和 workdir({my_workdir})。"
                    f"请特别注意避免编辑同一文件，避免使用git reset/checkout/revert等危险操作，多用git diff核对变更，切勿覆盖对方有效代码！"
                )
                try:
                    self.task_manager.db.add_task_info(
                        task_id=tid, info_content=other_info, dedup=True
                    )
                except Exception as e:
                    logger.debug(f"[ToolEngine] 向冲突事务#{tid} 注入info失败: {e}")

        except Exception as e:
            logger.warning(f"[ToolEngine] 事务#{self._task_id} 检测workdir冲突失败: {e}")

    # 云端LLM API域名后缀（这些provider无/flush_cache端点，且flush概念不适用）
    _CLOUD_API_DOMAINS = (
        "open.bigmodel.cn",
        "api.deepseek.com",
        "api.openai.com",
        "openrouter.ai",
        "dashscope.aliyuncs.com",
        "api.moonshot.cn",
        "api.siliconflow.cn",
    )
    # 进程级flush防抖：{base_url: 上次flush时间戳}。
    # 空响应→事务结束→(timer)重启新进程，引擎实例级计数/防抖会失效，
    # 必须进程级防抖才能覆盖"重启风暴"场景（如事务#2每5分钟空转重启）。
    _FLUSH_DEBOUNCE: dict[str, float] = {}
    _FLUSH_DEBOUNCE_SECONDS = 1800.0  # 同一base_url 30分钟内最多flush一次

    async def _flush_local_llm_cache(self, task_id: int | None) -> None:
        """空响应时清除本地sglang/vLLM类推理服务的KV缓存（方案C自愈机制）。

        根因（2026-08-18 事务#1573分析）：sglang MambaRadixCache的MAMBA_KV_FALLBACK路径，
        当prefix的mamba state被tombstone/驱逐后，复用full-attention KV但mamba层从0重放，
        混合注意力状态不一致 → 模型对特定前缀确定性输出垃圾（标签碎片/复读工具定义）→ peng端
        表现为空响应（且timer重启后新进程仍命中同一损坏前缀，形成空转循环）。
        POST /flush_cache 清除含损坏前缀的radix缓存即可恢复。

        触发权衡：空响应本身代价（整轮事务白跑+重启开销）远大于flush代价（其他并发
        事务下一轮多付一次prefill），故首次空响应即触发；进程级按base_url 30分钟防抖，
        避免多事务/重启风暴下反复flush拖垮整体缓存命中率。
        安全边界：仅对私有部署（base_url host非云端API域名）发请求；
        flush失败仅记录告警，绝不影响主流程。
        """
        try:
            prov_cfg = getattr(self.llm, "provider_config", None) or {}
            base_url = (prov_cfg.get("base_url") or "").strip().rstrip("/")
            provider = getattr(self.llm, "provider", None) or "unknown"
            if not base_url:
                logger.debug(
                    f"[ToolEngine] 事务#{task_id} 空响应flush跳过: provider={provider} 无base_url"
                )
                return
            # 域名白名单校验：云端API不支持flush且不应触碰
            host = httpx.URL(base_url).host or ""
            if any(host == d or host.endswith("." + d) for d in self._CLOUD_API_DOMAINS):
                logger.debug(
                    f"[ToolEngine] 事务#{task_id} 空响应flush跳过: provider={provider} 为云端API({host})"
                )
                return
            # 进程级防抖：同一base_url 30分钟内只flush一次
            now = time.time()
            last = self._FLUSH_DEBOUNCE.get(base_url, 0.0)
            if now - last < self._FLUSH_DEBOUNCE_SECONDS:
                logger.debug(
                    f"[ToolEngine] 事务#{task_id} 空响应flush防抖跳过: {base_url} "
                    f"距上次flush仅{now - last:.0f}s < {self._FLUSH_DEBOUNCE_SECONDS:.0f}s"
                )
                return
            self._FLUSH_DEBOUNCE[base_url] = now
            # sglang/vLLM的flush_cache在服务根路径（POST /flush_cache），而OpenAI兼容
            # base_url通常带版本前缀（如 http://wsl:30000/v1），需剥离 /v1 等后缀再拼接
            root_url = base_url
            for suffix in ("/v1", "/v1/chat"):
                if root_url.lower().endswith(suffix):
                    root_url = root_url[: -len(suffix)]
                    break
            flush_url = f"{root_url}/flush_cache"
            logger.warning(
                f"⚠️ [ToolEngine] 事务#{task_id} 空响应，尝试清除本地推理服务缓存: "
                f"POST {flush_url} (provider={provider}, host={host})"
            )
            timeout = httpx.Timeout(connect=3.0, read=10.0, write=3.0, pool=3.0)
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                resp = await client.post(flush_url)
                body = (resp.text or "")[:100]
            if resp.status_code == 200:
                logger.info(f"✅ [ToolEngine] 事务#{task_id} 推理服务缓存清除成功: {body}")
                get_output_manager().write_output(
                    task_id=task_id,
                    content=(
                        f"\n{ANSI_COLORS.get('bright_yellow')}"
                        f"⚠️ 空响应已触发推理服务缓存清除(flush_cache)，"
                        f"下一轮LLM调用将以冷缓存重新prefill。"
                        f"{ANSI_COLORS.get('reset')}\n"
                    ),
                    source="system",
                )
            elif resp.status_code == 400:
                # sglang语义：有pending/running请求时拒绝flush——无需处理，
                # 损坏场景（空响应）必然无本事务在跑的请求，400极可能是其他事务正在推理
                logger.info(
                    f"[ToolEngine] 事务#{task_id} flush_cache被服务端拒绝(有运行中请求，跳过): {body}"
                )
            else:
                logger.warning(
                    f"[ToolEngine] 事务#{task_id} flush_cache返回 HTTP {resp.status_code}: {body}"
                )
        except Exception as e:
            logger.warning(f"[ToolEngine] 事务#{task_id} 空响应flush_cache失败(忽略): {e}")

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
                        content=f"\n📜 {ANSI_COLORS.get('gray')} 待处理消息#{reply.id} [{reply.timestamp}] {sender} ：{content}{ANSI_COLORS.get('reset')}\n",
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
                    content=f"\n{ANSI_COLORS.get('bright_red')}{warning.strip()}{ANSI_COLORS.get('reset')}\n",
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

        # 提取内容
        contents = [item["content"] for item in deduped_items]
        info_text = "\n".join(contents)
        info_text = info_text.replace("\\n", "\n")

        # 标记所有未读info为已读（包括被截断的，避免下次重复）
        success = db.set_task_info_status(self._task_id, mode="all", read=True)
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
            history.add_user(f"\n关于本事务#{self._task_id}的最新指示：{info_text}\n")
        logger.debug(
            f"[ToolEngine] Info字段处理: {len(items_to_mark)}个未读info（去重后{len(deduped_items)}个）已标记为已读"
        )
        get_output_manager().write_output(
            task_id=self._task_id,
            source="task",
            content=f"\n🏷️  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {ANSI_COLORS.get('bold')}{ANSI_COLORS.get('bright_blue')}{get_master_name()} ：{ANSI_COLORS.get('bright_green')}{info_text}{ANSI_COLORS.get('reset')}\n",
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
                fetch_result = cfg.fetch_providers(content)
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
                    }.get(status, status)
                    notes.append(f"@file={ref} 无法注入: {reason}")

            if loaded:
                if notes:
                    parts[0]["text"] = content + "\n[多模态注入提示]\n" + "\n".join(notes)
                msg["content"] = parts
                get_output_manager().write_output(
                    task_id=self._task_id,
                    source="task",
                    content=f"\n🖼️ [多模态] 已注入 {loaded} 个多模态文件到上下文\n",
                )
            elif notes:
                # 全部失败：保留文本并追加提示（模型可自行用read工具处理）
                msg["content"] = content + "\n[多模态注入提示]\n" + "\n".join(notes)

        self._mm_scan_cursor = cursor

    def _save_history(self, history):
        try:
            context = json.dumps(
                history.to_context(thinking_type=self.llm.thinking_type),
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
                history.to_context(thinking_type=self.llm.thinking_type),
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

            meta = await asyncio.to_thread(
                ToolBackupManager.create_iteration_snapshot,
                task_id=task_id,
                history_context=history_context,
                checklist=checklist,
                iteration=iteration,
                workdir=task_workdir,
                nest_name=task_nest,
            )
            if not meta:
                logger.debug(f"迭代备份未生成(task#{task_id}, iter#{iteration})")
                return

            # 备份成功：更新指纹基线（全部失败时不更新，下一轮自动重试）
            self._last_backup_fingerprint = fingerprint

            display = ToolBackupManager.generate_iteration_backup_display(meta)
            if display:
                get_output_manager().write_output(
                    task_id=task_id,
                    content=f"\n{ANSI_COLORS.get('bright_cyan', '')}{display}{ANSI_COLORS.get('reset', '')}\n",
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
        current = history.get_token_count(estimate=False)
        task = self.task_manager.get_task(self._task_id)
        progress = task.get("progress", 0) or 0
        if current > threshold:
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
            if self._should_report_system_error(content):
                history.add_user(content)
                get_output_manager().write_output(
                    task_id=self._task_id,
                    content=content,
                    source="system",
                )

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
        # 有tool_call的迭代计数（用于PUA注入间隔判断）
        self._tool_call_iter_count: int = 0
        # nest+workdir 冲突警告列表：_check_workdir_conflicts 检测并保存，
        # 由迭代循环内 _get_task_info 的 history.add_user 注入（不打断，迭代自然处理）
        self._workdir_conflict_warnings: list[str] = []
        # 跟踪上次注入的 nest/workdir，避免每次迭代重复注入
        self._last_injected_nest: str | None = None
        self._last_injected_workdir: str | None = None
        # 多模态注入游标：记录已扫描过的 history 消息索引（run_stream 重入时重置）
        self._mm_scan_cursor: int = 0
        # thinking回顾扫描游标：重置为当前输出文件行数，跳过历史迭代内容，
        # 本轮提醒只扫描游标之后的 thinking 行（_remind_thinking_in_output 消费后推进）
        self._thinking_scan_line: int = self._count_output_lines(task_id)
        # 检测 nest+workdir 冲突并向冲突事务发送 info 提示（start/restart/resume 时均触发）
        self._check_workdir_conflicts()
        if len(self._history._messages) == 0:
            # 如果history为空（fresh start），获取消息上下文和事务内容
            self._get_task_full_context(self._history)
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
            _vision_provider = self._is_multimodal_provider()
            current_messages = history.to_api_format(
                thinking_type=self.llm.thinking_type, vision=_vision_provider
            )
            if history.size() > 5:
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
                            content=f"\n{ANSI_COLORS.get('bright_red')}{warn_msg}{ANSI_COLORS.get('reset')}\n",
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
            while not llm_finish:
                try:
                    thinking = ""
                    content = ""
                    tool_calls: list[Any] = []
                    chunk_count = 0
                    # 纯符号计数在重试时不重置：跨重试累积，尽早检测垃圾流
                    # （重试时模型可能继续输出符号垃圾，重置计数会推迟检测）

                    # 重试时 provider 可能已切换（thinking_type/vision 变化），需重建消息
                    if retry > 0:
                        current_messages = history.to_api_format(
                            thinking_type=self.llm.thinking_type,
                            vision=self._is_multimodal_provider(),
                        )

                    # 思考过载检测：强制关闭思考模式时传入 disabled
                    # 注意：仅 auto 模式允许干预（auto=自动管理思考模式，发现过度思考时禁用/降级）
                    # 固定模式（enabled/low/high/max/disabled）尊重用户显式配置，不做强制干预
                    effective_thinking_override = None
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
                            # thinking阶段：每个chunk都检测info/控制信号中断，及时响应
                            if self._has_pending_interrupts():
                                self.exit_on_unexpected_status()
                                logger.info(
                                    f"⚡ [ToolEngine] 事务#{task_id}thinking阶段检测到中断信号(info/控制)，停止思考保存进度"
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

                        # 5. 中断检测：每隔32个chunk检查一次是否有新消息/info/控制信号
                        # 避免频繁查询数据库，同时在thinking阶段能及时响应
                        if chunk_count % 32 == 0 and self._has_pending_interrupts():
                            self.exit_on_unexpected_status()
                            logger.info(
                                f"⚡ [ToolEngine] 事务#{task_id}检测到中断信号，停止思考跳过本次迭代"
                            )
                            should_skip_iteration = True
                            break
                    llm_finish = True
                except asyncio.CancelledError as e:
                    text = f"⚠️  事务#{task_id} [ToolEngine]检测到中断，立刻结束大模型处理{e}。\n"
                    logger.info(text)
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
                        # 检测400内容审核/敏感内容错误：特殊处理
                        # 这类错误不应冷却或切换provider，而是重试1次后删除user输入继续
                        _content_filter_keywords = [
                            "不安全或敏感内容",
                            "敏感内容",
                            "内容审核",
                            "内容可能包含不安全",
                            "content_filter",
                            "sensitive content",
                            "安全审核",
                            "违规内容",
                        ]
                        if status_code == 400 and any(
                            kw.lower() in err_msg.lower() for kw in _content_filter_keywords
                        ):
                            is_content_filtered = True
                            logger.warning(
                                f"{icon} {self.llm.provider} 事务#{task_id}大模型API内容审核拦截(400)。"
                                f"不冷却不切换，重试1次后删除user输入。详情: {err_msg[:200]}\n"
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
                    if status_code == 429 and is_rate_limited:
                        _max_retry = 30
                    elif status_code == 429 and not is_rate_limited:
                        _max_retry = 2  # 配额耗尽：快速切换
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
                        _sys_err = f"\n❌  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]{ANSI_COLORS.get('bright_red')}大模型API调用失败({detail})，已重试{retry}次，跳出重试循环。{ANSI_COLORS.get('reset')}\n"
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
                    # 网络瞬时错误（ReadError/ReadTimeout/ConnectError/RemoteProtocolError等）
                    # 这些是网络层临时故障，不切换provider，同provider重试
                    retry += 1
                    is_rate_limited = False
                    is_content_filtered = False
                    is_transient_error = True  # 标记瞬时错误，阻止provider切换
                    icon = self.llm.config.get_icon(self.llm.provider)
                    err_str = str(e).strip()
                    if err_str:
                        self.llm.error = f"{type(e).__name__}: {err_str}"
                    else:
                        self.llm.error = f"{type(e).__name__}"
                    logger.info(
                        f"{icon} {self.llm.provider} 事务#{task_id}大模型网络瞬时错误"
                        f"({type(e).__name__})，不切换provider，0.5秒后同provider重试(第{retry}次)。\n"
                        f"堆栈: {traceback.format_exc()}"
                    )
                    if retry >= 5:
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
                    # 网络瞬时错误：内层重试耗尽，短暂等待后回到外层循环重试同一provider
                    logger.info(
                        f"🌐 [ToolEngine] 事务#{task_id} 网络瞬时错误重试耗尽，"
                        f"5秒后继续重试同一provider={self.llm.provider}"
                    )
                    await asyncio.sleep(5)
                    continue  # 回到外层 while True 循环，重试同一provider（未被标记失败）
                if is_content_filtered:
                    # 400内容审核：重试1次已失败，删除最后一条user消息后继续重试
                    # 安全限制：最多连续删除2次user消息，防止无限循环
                    if self._content_filter_deletions < 2:
                        self._content_filter_deletions += 1
                        messages = getattr(history, "_messages", [])
                        for i in range(len(messages) - 1, -1, -1):
                            if messages[i].get("role") == "user":
                                deleted_msg = messages.pop(i)
                                logger.warning(
                                    f"🛡️ [ToolEngine] 事务#{task_id} 内容审核拦截，"
                                    f"删除最后一条user消息({len(deleted_msg.get('content', ''))}字符)后继续重试。"
                                    f"删除次数: {self._content_filter_deletions}/2"
                                )
                                break
                        self._save_history(history)
                        _sys_err = (
                            f"\n{ANSI_COLORS.get('bright_yellow')}"
                            f"🛡️ 内容审核拦截，已删除最近的用户输入并重试"
                            f"(第{self._content_filter_deletions}次)..."
                            f"{ANSI_COLORS.get('reset')}\n"
                        )
                        get_output_manager().write_output(
                            task_id=task_id,
                            content=_sys_err,
                            source="system",
                        )
                        continue  # 回到外层 while True 循环，重试（已删除user消息）
                    else:
                        logger.error(
                            f"🛡️ [ToolEngine] 事务#{task_id} 内容审核拦截，"
                            f"已连续删除{self._content_filter_deletions}次user消息仍失败，放弃。"
                        )
                        # 落入下方provider冷却等待逻辑
                # 根据provider冷却时间动态计算等待时间
                wait_time = self.llm.config.get_min_cooldown_remaining()
                # 取冷却剩余时间+5秒余量，至少30秒，最多300秒
                wait_time = max(30, min(int(wait_time) + 5, 300))
                logger.warning(
                    f"⚠️ [ToolEngine] 事务#{task_id} 所有provider重试均失败，"
                    f"等待{wait_time}秒后重试..."
                )
                _sys_err = (
                    f"\n⏳  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]{ANSI_COLORS.get('bright_yellow')}"
                    f"所有大模型provider均不可用，等待{wait_time}秒后重试..."
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
                        content=f"\n{ANSI_COLORS.get('bright_red')}{intervention}{ANSI_COLORS.get('reset')}\n",
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
                        content=f"\n{ANSI_COLORS.get('bright_red')}{intervention}{ANSI_COLORS.get('reset')}\n",
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
                        content=f"\n{ANSI_COLORS.get('bright_red')}{intervention}{ANSI_COLORS.get('reset')}\n",
                        source="system",
                    )
                elif wait_hit_count >= self.llm.config.overthinking_threshold:
                    intervention = self._prompts.overthinking_warning
                    get_output_manager().write_output(
                        task_id=task_id,
                        content=f"\n{ANSI_COLORS.get('bright_yellow')}{intervention}{ANSI_COLORS.get('reset')}\n",
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
            # 6. 更新历史：添加助手消息
            # 当有 tool_calls 时，将 reasoning_content 合并到同一个 assistant 消息中
            # 避免产生两个连续 assistant 消息（DeepSeek 不支持）
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
                        content=f"\n{ANSI_COLORS.get('bright_yellow')}⚠️ 工具调用参数为空，重试中...{ANSI_COLORS.get('reset')}\n",
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

            rollback_metas: list[dict | None] = []
            for tc in tool_calls:
                tool_name_sn = (
                    tc.name if hasattr(tc, "name") else tc.get("function", {}).get("name", "")
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
                        rollback_metas.append(meta)
                    except Exception as e:
                        logger.debug(f"创建快照失败({tool_name_sn}): {e}")
                        rollback_metas.append(None)
                else:
                    rollback_metas.append(None)

            tool_exec_results = await self._execute_tools_parallel(tool_calls)

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
                            history.to_context(thinking_type=self.llm.thinking_type),
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
                # 多模态附件（read工具返回的图片/PDF等）：vision provider时
                # 将附件作为 content part 列表传给 LLM（防止模型只看到
                # "图片 读取成功"摘要而凭空编造图片内容）；
                # 非vision provider时附件丢弃（保留纯文本摘要）
                tool_call_id = tc.id if hasattr(tc, "id") else str(id(tc))
                history_content: str | list = summary_content
                if result_attachments and self._is_multimodal_provider():
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

                # sglang缓存损坏自愈（方案C）：MambaRadixCache的MAMBA_KV_FALLBACK路径在mamba state被
                # tombstone/驱逐后复用full-attention KV但mamba层重放，混合注意力状态不一致导致模型对
                # 特定前缀确定性输出垃圾（表现为空响应，且timer重启后新进程仍命中同一损坏前缀形成空转
                # 循环）。flush_cache清除含损坏前缀的radix缓存即可恢复。
                # 触发：首次空响应即触发（flush代价远小于整轮空转+重启）；
                # 防抖在 _flush_local_llm_cache 内按base_url进程级30分钟一次（覆盖重启风暴）；
                # 仅私有部署生效（云端API域名跳过）；flush失败仅告警不影响原空响应处理流程。
                asyncio.create_task(self._flush_local_llm_cache(task_id))

                if self._consecutive_empty <= 2:
                    # tokens未超限但空响应：可能是一时问题，直接重试（不结束事务）
                    warn_msg = (
                        f"⚠️ [toolengine] 事务#{task_id} 空响应(tokens={est_tokens})，"
                        f"直接重试（{self._consecutive_empty}/2）。"
                    )
                    logger.warning(warn_msg)
                    get_output_manager().write_output(
                        task_id=task_id,
                        content=f"\n{ANSI_COLORS.get('bright_yellow')}{warn_msg}{ANSI_COLORS.get('reset')}\n",
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
                    _sys_err = f"\n{ANSI_COLORS.get('bright_yellow')}{warn_msg}{ANSI_COLORS.get('reset')}\n"
                    if self._should_report_system_error(_sys_err):
                        get_output_manager().write_output(
                            task_id=task_id,
                            content=_sys_err,
                            source="system",
                        )
                    should_complete = False
                else:
                    # 清空上下文重启事务处理
                    warn_msg = (
                        f"⚠️ [toolengine] 事务#{task_id} 空响应(tokens={est_tokens})，"
                        f"compact处理无效，清空上下文重启事务处理"
                    )
                    logger.warning(warn_msg)
                    get_output_manager().write_output(
                        task_id=task_id,
                        content=f"\n{ANSI_COLORS.get('bright_yellow')}{warn_msg}{ANSI_COLORS.get('reset')}\n",
                        source="system",
                    )
                    self.task_manager.db.insert_task_control(task_id, "restart")
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
                    from peng.common.code_display import (
                        format_edit_display,
                        format_line_edit_display,
                        format_write_display,
                    )

                    file_path = arguments.get("filePath", "") or ""
                    if tool_name == "write":
                        content = arguments.get("content", "")
                        if file_path and isinstance(content, str):
                            return (
                                f"{tool_type} {format_write_display(file_path, content)}"
                            )
                    elif tool_name == "edit":
                        old_string = arguments.get("oldString", "")
                        new_string = arguments.get("newString", "")
                        if (
                            file_path
                            and isinstance(old_string, str)
                            and isinstance(new_string, str)
                        ):
                            return (
                                f"{tool_type} "
                                f"{format_edit_display(file_path, old_string, new_string)}"
                            )
                    else:  # line_edit
                        if file_path:
                            return (
                                f"{tool_type} "
                                f"{format_line_edit_display(file_path, arguments)}"
                            )
                except Exception as e:
                    # 格式化失败不影响主流程，回退到通用 JSON 显示
                    logger.debug(f"write/edit 参数易读格式化失败，回退JSON显示: {e}")

            # report/checklist：markdown 内容人类易读+高亮显示
            elif tool_name in ("report", "checklist"):
                try:
                    from peng.common.code_display import format_markdown_display

                    content = (
                        arguments.get("summary")
                        or arguments.get("checklist_content")
                        or ""
                    )
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
        """格式化工具调用结果信息（对冗长参数的工具只显示工具类型+文件路径）"""

        tool_type = self._TOOL_TYPE_NAMES.get(tool_name, f"🛠️ {tool_name}")

        # 参数冗长的工具：只显示工具类型+文件路径
        if tool_name in self._TOOL_RESULT_NO_ARGS:
            if tool_name in self._TOOL_RESULT_HAS_PATH:
                file_path = arguments.get("filePath", "")
                if file_path:
                    return f"{tool_type} {file_path}"
            return tool_type

        # 其他工具：显示完整参数
        try:
            args_json = json.dumps(arguments, ensure_ascii=False, indent=2)
            args_json = args_json.replace("\n", "\\n").replace("  ", " ")
            return f"{tool_type} {args_json}"
        except Exception:
            return f"{tool_type} {str(arguments)}"

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

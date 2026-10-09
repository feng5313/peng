#!/usr/bin/env python3
"""事务#2002：DeepSeek 普通 assistant 消息缺 reasoning_content 导致 400 死循环修复

背景（事务#1997事故）：
- glm 额度耗尽 → 切 s-qwen3.8（报上下文超限）→ glm 冷却中 → 选中 deepseek-flash
- → HTTP 400 "The `reasoning_content` in the thinking mode must be passed back
  to the API." → 换 provider 无法自愈（历史中毒）→ 全 provider 失败死循环
  （20:08~20:19 连续 7 轮）

根因（实机 API 验证）：
DeepSeek 带 tools + thinking 的请求要求**所有** assistant 消息都携带
reasoning_content（空串 "" 可接受，缺失则 400）。官方文档（Tool Calls）：
"for requests carrying the tools parameter, the reasoning_content must be fully
passed back to the API in all subsequent requests — even for turns where the
model did not perform a tool call. If your code does not correctly pass back
reasoning_content, the API will return a 400 error."

peng 旧代码 to_api_format 只为**带 tool_calls** 的 assistant 消息补空串
reasoning_content，遗漏了**无 tool_calls 的普通 assistant 消息**（典型来源：
report 钩子注入的 "🪝 我需要确认…" 消息、模型无思考的最终回答）。事务#1997
的 history 中恰有 1 条这样的消息（钩子注入）→ 400 死循环。

实机验证（deepseek-flash，真实 key）：
- 消息序列含 tool_calls(有rc) + plain assistant(缺rc) → 400
- tool_calls 消息缺 rc → 400
- 所有 assistant 都有 rc（空串）→ 200
- #1997 真实 321 消息 payload 修复前 400 / 修复后 200

修复：to_api_format 中，thinking_type=="thinking" 时**所有**缺失
reasoning_content 的 assistant 消息都补空串（不限于带 tool_calls 者）。

覆盖：plain assistant 补空串 / tool_calls assistant 补空串 /
有前置 thinking 回溯合并 / 非 thinking 模式不补。
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from peng.llm.tool_engine import MessageHistory


def _assistant_msgs(out: list[dict]) -> list[dict]:
    return [m for m in out if m.get("role") == "assistant"]


def _find(out: list[dict], content_prefix: str) -> dict:
    for m in out:
        if m.get("role") == "assistant" and str(m.get("content", "")).startswith(content_prefix):
            return m
    raise AssertionError(f"未找到 content 以 {content_prefix!r} 开头的 assistant 消息")


# ==================== 核心修复：所有 assistant 消息补空串 ====================


def test_plain_assistant_gets_empty_reasoning_in_thinking_mode():
    """普通 assistant（无 tool_calls）缺 reasoning_content 时补空串（核心修复）

    复现事务#1997：report 钩子注入的普通 assistant 消息无 reasoning_content
    → DeepSeek 400。修复后应补空串。
    """
    h = MessageHistory()
    h.add_system("sys")
    h.add_user("任务", merge=False)
    # 模型带 tool_calls 的一轮（reasoning 非空）
    h.add_assistant(
        "",
        tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "report", "arguments": "{}"}}
        ],
        _reasoning="汇报理由",
    )
    h.add_tool_result("c1", "report", "汇报成功", add_timestamp=False)
    # 钩子注入的普通 assistant 消息（无 tool_calls、无 reasoning_content）
    h.add_assistant("🪝 我需要确认，checklist 是否完成？")
    out = h.to_api_format(thinking_type="thinking", vision=False)
    hook_msg = _find(out, "🪝 我需要确认")
    assert hook_msg.get("reasoning_content") == "", (
        f"普通 assistant 消息应补空串 reasoning_content，"
        f"实际: {'缺失' if 'reasoning_content' not in hook_msg else hook_msg['reasoning_content']!r}"
    )


def test_all_assistant_msgs_have_reasoning_in_thinking_mode():
    """thinking 模式下所有 assistant 消息都必须有 reasoning_content（空串可接受）"""
    h = MessageHistory()
    h.add_system("sys")
    h.add_user("任务", merge=False)
    h.add_assistant(
        "",
        tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
        ],
        _reasoning="思考1",
    )
    h.add_tool_result("c1", "read", "r1", add_timestamp=False)
    h.add_assistant("", tool_calls=[
        {"id": "c2", "type": "function", "function": {"name": "read", "arguments": "{}"}}
    ])  # 无 reasoning
    h.add_tool_result("c2", "read", "r2", add_timestamp=False)
    h.add_assistant("最终回答")  # 无 reasoning
    out = h.to_api_format(thinking_type="thinking", vision=False)
    for m in _assistant_msgs(out):
        assert "reasoning_content" in m, f"assistant 消息缺 reasoning_content: {m}"


def test_nonempty_reasoning_preserved():
    """thinking 模式下非空 reasoning_content 必须原样保留"""
    h = MessageHistory()
    h.add_system("sys")
    h.add_user("任务", merge=False)
    h.add_assistant(
        "",
        tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
        ],
        _reasoning="真实思考内容",
    )
    h.add_tool_result("c1", "read", "r", add_timestamp=False)
    out = h.to_api_format(thinking_type="thinking", vision=False)
    tc_msg = [m for m in _assistant_msgs(out) if m.get("tool_calls")][0]
    assert tc_msg.get("reasoning_content") == "真实思考内容"


# ==================== 回溯合并（保留正确行为） ====================


def test_backtrack_merge_thinking_into_tool_calls():
    """独立 thinking 消息 + 后续 tool_calls 消息：应回溯合并 reasoning_content"""
    h = MessageHistory()
    h.add_system("sys")
    h.add_user("任务", merge=False)
    h.add_thinking("独立思考内容", is_deepseek=True)
    h.add_assistant(
        "",
        tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
        ],
    )
    h.add_tool_result("c1", "read", "r", add_timestamp=False)
    out = h.to_api_format(thinking_type="thinking", vision=False)
    tc_msg = [m for m in _assistant_msgs(out) if m.get("tool_calls")][0]
    assert tc_msg.get("reasoning_content") == "独立思考内容", "应回溯合并独立 thinking"
    # 独立 thinking 消息应被合并移除
    thinking_only = [
        m
        for m in _assistant_msgs(out)
        if not m.get("content") and not m.get("tool_calls")
    ]
    assert not thinking_only, "独立 thinking 消息应被合并移除"


# ==================== 非 thinking 模式 ====================


def test_reasoning_not_added_in_non_thinking_mode():
    """非 thinking 模式（如 qwen）不补 reasoning_content，且清除已有的"""
    h = MessageHistory()
    h.add_system("sys")
    h.add_user("任务", merge=False)
    h.add_assistant("", tool_calls=[
        {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
    ], _reasoning="思考")
    h.add_tool_result("c1", "read", "r", add_timestamp=False)
    h.add_assistant("回答")
    for tt in ("chat_template_kwargs", None):
        out = h.to_api_format(thinking_type=tt, vision=False)
        for m in _assistant_msgs(out):
            assert "reasoning_content" not in m, f"tt={tt} 不应有 reasoning_content"


# ==================== 真实 #1997 history 回归 ====================


def test_real_1997_history_all_assistants_have_reasoning():
    """用 #1997 真实 history 回归：thinking 模式下所有 assistant 都有 reasoning_content"""
    ctx_path = ROOT / "data" / "history" / "task#1997" / "context.json"
    if not ctx_path.exists():
        return  # 历史文件不存在则跳过（CI 环境）
    data = json.loads(ctx_path.read_text())
    h = MessageHistory()
    h.from_str(json.dumps(data))
    out = h.to_api_format(thinking_type="thinking", vision=False)
    missing = [i for i, m in enumerate(_assistant_msgs(out)) if "reasoning_content" not in m]
    assert not missing, f"所有 assistant 消息都应有 reasoning_content，缺失 {len(missing)} 条"
    # 该 history 恰有 1 条普通 assistant 消息（钩子注入），必须补空串
    plain = [m for m in _assistant_msgs(out) if not m.get("tool_calls")]
    assert plain, "#1997 history 应含普通 assistant 消息（钩子注入）"
    assert all("reasoning_content" in m for m in plain)

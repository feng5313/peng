#!/usr/bin/env python3
"""内容审核拦截逐级清除策略单元测试

背景（事务#1699死循环事故）：read工具读取图片→附件注入tool结果消息→
LLM调用触发HTTP 400内容审核拦截；旧逻辑只删user消息文本，触发源附件
在tool消息中永远不被清除→无限重试死循环。

覆盖 ToolExecutionEngine._content_filter_next_action 的逐级升级清除：
级1 清多模态附件（含tool消息中的附件）/ 级2 删最后user消息 / 级3+ 成对
裁剪尾部轮次 / 下限保护raise / 无效动作自动跳级 / 计数器升级
"""

import sys
from pathlib import Path

# 项目根目录加入 sys.path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from peng.llm.tool_engine import MessageHistory, ToolExecutionEngine


def make_engine(stage: int = 0) -> ToolExecutionEngine:
    """构造跳过 __init__ 的最小引擎实例（_content_filter_next_action 只依赖计数器）"""
    engine = ToolExecutionEngine.__new__(ToolExecutionEngine)
    engine._content_filter_deletions = stage
    return engine


def _img_part(b64: str = "abc") -> dict:
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}


def make_history_with_tool_attachment() -> MessageHistory:
    """复现事务#1699场景：user任务 → assistant(tool_calls) → tool结果带图片附件"""
    h = MessageHistory()
    h.add_system("你是助手")
    h.add_user("调整鲸鱼尾鳍模型", merge=False)
    h.add_assistant(
        "",
        tool_calls=[
            {"id": "call1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
        ],
    )
    h.add_tool_result(
        "call1",
        "read",
        [{"type": "text", "text": "图片读取成功"}, _img_part()],
        add_timestamp=False,
    )
    return h


def _no_attachment_left(h: MessageHistory) -> bool:
    for m in h._messages:
        c = m.get("content")
        if isinstance(c, list):
            if any(
                isinstance(p, dict) and p.get("type") in MessageHistory._ATTACHMENT_PART_TYPES
                for p in c
            ):
                return False
    return True


# ==================== 级1：清除多模态附件 ====================


def test_stage1_clears_attachments_in_tool_message():
    """级1：清除tool消息中的多模态附件（事务#1699死循环根因场景）"""
    h = make_history_with_tool_attachment()
    engine = make_engine(0)
    desc = engine._content_filter_next_action(h)
    assert "附件" in desc, desc
    assert engine._content_filter_deletions == 1
    # 附件part已被替换为占位说明文本
    tool_msgs = [m for m in h._messages if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    content = tool_msgs[0]["content"]
    assert isinstance(content, str)
    assert "多模态附件" in content
    assert _no_attachment_left(h)


# ==================== 级2：删除最后一条user消息 ====================


def test_stage0_skips_to_user_delete_when_no_attachment():
    """级1无附件可清→自动跳级执行级2删除最后一条user消息"""
    h = MessageHistory()
    h.add_system("sys")
    for i in range(8):
        h.add_user(f"任务片段{i}", merge=False)
        h.add_assistant(f"回复{i}")
    engine = make_engine(0)
    desc = engine._content_filter_next_action(h)
    assert "user" in desc, desc
    assert engine._content_filter_deletions == 2
    assert sum(1 for m in h._messages if m["role"] == "user") == 7
    assert h._messages[-1]["role"] == "assistant"


def test_stage2_deletes_last_user_message():
    """级2：删除最后一条user消息"""
    h = MessageHistory()
    h.add_system("sys")
    for i in range(8):
        h.add_user(f"任务{i}", merge=False)
        h.add_assistant(f"回复{i}")
    engine = make_engine(1)
    desc = engine._content_filter_next_action(h)
    assert "user" in desc, desc
    assert engine._content_filter_deletions == 2
    assert sum(1 for m in h._messages if m["role"] == "user") == 7


# ==================== 级3+：裁剪最后一个完整轮次 ====================


def make_long_history_with_tool_attachment() -> MessageHistory:
    """附件轮次 + 3个补充轮次（共10条消息，高于下限6）"""
    h = make_history_with_tool_attachment()  # user + assistant(tool_calls) + tool = 3条
    for i in range(3):
        h.add_user(f"补充{i}", merge=False)
        h.add_assistant(f"回答{i}")
    return h


def test_stage3_prunes_tail_turn_pair():
    """级3：裁剪最后一个完整轮次（user"补充2"+"回答2"），保持tool_calls配对完整"""
    h = make_long_history_with_tool_attachment()
    engine = make_engine(2)
    desc = engine._content_filter_next_action(h)
    assert "裁剪" in desc and "2" in desc, desc
    assert engine._content_filter_deletions == 3
    roles = [m["role"] for m in h._messages]
    # 尾部轮次(user+assistant)被整体裁掉，附件轮次保留
    assert roles == ["user", "assistant", "tool", "user", "assistant", "user", "assistant"]
    assert h._messages[-1]["content"] == "回答1"


def test_stage3_prunes_last_full_turn_from_plain_tail():
    """级3：尾部是普通user+assistant轮次时整体裁剪（不留孤立user）"""
    h = MessageHistory()
    h.add_system("sys")
    for i in range(8):
        h.add_user(f"任务{i}", merge=False)
        h.add_assistant(f"回复{i}")
    engine = make_engine(2)
    desc = engine._content_filter_next_action(h)
    assert "裁剪" in desc and "2" in desc, desc
    assert engine._content_filter_deletions == 3
    assert h._messages[-1]["content"] == "回复6"  # 最后一个轮次(任务7+回复7)被裁掉


# ==================== 下限保护 ====================


def test_min_messages_raises():
    """下限保护：消息数≤下限仍被拦截时raise RuntimeError，杜绝死循环"""
    h = make_history_with_tool_attachment()  # 仅3条消息
    engine = make_engine(2)
    try:
        engine._content_filter_next_action(h)
        raise AssertionError("应raise RuntimeError")
    except RuntimeError as e:
        assert "停止重试" in str(e)
    assert engine._content_filter_deletions == 2  # 未执行任何动作


def test_progression_until_raise():
    """完整递进：附件→user→轮次→轮次…直到下限raise"""
    h = MessageHistory()
    h.add_system("sys")
    h.add_user("原始任务", merge=False)
    h.add_assistant(
        "",
        tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
        ],
    )
    h.add_tool_result(
        "c1", "read", [{"type": "text", "text": "图"}, _img_part()], add_timestamp=False
    )
    for i in range(3):
        h.add_user(f"补充{i}", merge=False)
        h.add_assistant(f"回答{i}")
    engine = make_engine(0)
    d1 = engine._content_filter_next_action(h)
    assert "附件" in d1, d1
    d2 = engine._content_filter_next_action(h)
    assert "user" in d2, d2
    d3 = engine._content_filter_next_action(h)
    assert "裁剪" in d3, d3
    if len(h._messages) <= engine._CONTENT_FILTER_MIN_MESSAGES:
        try:
            engine._content_filter_next_action(h)
            raise AssertionError("应raise RuntimeError")
        except RuntimeError:
            pass

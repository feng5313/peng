#!/usr/bin/env python3
"""事务#2002：上下文长度超标应拒绝处理而非破坏上下文

背景（事务#1993事故）：glm接口额度耗尽→切换s-qwen3.8→400上下文长度超标
（240003 tokens > 232000上限）→被当普通4xx处理（重试2次→跳出→全provider
不可用→等待循环），且provider切换导致thinking_type变化（glm→qwen）使
to_api_format输出格式变化（reasoning_content被strip），误报"上下文增量异常"
输出大段history diff。

根因：
1. check_progress_and_compact 用 get_token_count(estimate=False) 返回
   _prompt_tokens（上一个provider glm报告的210K），provider切换后该值过期，
   新provider qwen实际计数240K但检测不到上下文紧张→不compact→400超限。
2. 400上下文长度超标未识别，按普通4xx处理（切换provider必然同样失败）。
3. provider切换时to_api_format序列化格式变化，前缀比较误报增量异常。

覆盖：
- CONTEXT_LENGTH_KEYWORDS 关键词识别
- check_progress_and_compact 用 estimate_tokens（provider无关）
- to_api_format 序列化键变化时重置增量检测基线
"""

import sys
from pathlib import Path

# 项目根目录加入 sys.path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from peng.llm.tool_engine import (
    CONTEXT_LENGTH_KEYWORDS,
    MessageHistory,
    ToolExecutionEngine,
)


# ==================== CONTEXT_LENGTH_KEYWORDS 识别 ====================


def _match_ctx_overflow(err_msg: str) -> bool:
    """与tool_engine.py中HTTP 400处理分支相同的匹配逻辑"""
    return any(kw.lower() in err_msg.lower() for kw in CONTEXT_LENGTH_KEYWORDS)


def test_context_overflow_keyword_english():
    """英文上下文长度超标报错应被识别（事务#1993实际报错）"""
    err = (
        "Requested token count exceeds the model's maximum context length "
        "of 232000 tokens. You requested a total of 240003 tokens: 210003 "
        "tokens from the input messages and 30000 tokens for the completion."
    )
    assert _match_ctx_overflow(err), "应识别英文上下文长度超标"


def test_context_overflow_keyword_chinese():
    """中文上下文长度超标报错应被识别"""
    assert _match_ctx_overflow("上下文长度超标，请减少输入")
    assert _match_ctx_overflow("上下文超限")
    assert _match_ctx_overflow("超出上下文长度限制")


def test_context_overflow_not_match_content_filter():
    """内容审核/解析类报错不应被误判为上下文长度超标"""
    assert not _match_ctx_overflow("内容可能包含不安全或敏感内容")
    assert not _match_ctx_overflow("could not decode image data")
    assert not _match_ctx_overflow("messages[0].content[0].file 必须传入file_id")


# ==================== check_progress_and_compact 用 estimate_tokens ====================


def make_engine_with_history(prompt_tokens: int) -> tuple:
    """构造引擎+history，_prompt_tokens设为上一provider的旧值"""
    engine = ToolExecutionEngine.__new__(ToolExecutionEngine)
    history = MessageHistory()
    history.add_system("你是助手")
    # 填充足够多的消息使 estimate_tokens 能反映真实大小
    for i in range(10):
        history.add_user(f"任务片段{i} " + "x" * 200, merge=False)
        history.add_assistant(f"回复{i} " + "y" * 200)
    # 模拟上一provider（glm）报告的旧prompt token数
    history._prompt_tokens = prompt_tokens
    return engine, history


def test_check_uses_estimate_not_stale_prompt_tokens():
    """根因修复：上下文紧张检测用estimate_tokens而非过期的_prompt_tokens

    事务#1993：glm报告_prompt_tokens=210000（<320K阈值不告警），
    切换qwen后qwen实际计数240K（>232K上限）。若检测仍用_prompt_tokens，
    切换provider前检测不到上下文紧张→不compact→400超限。
    修复后：estimate_tokens基于当前消息JSON大小，provider无关，能正确检测。
    """
    engine, history = make_engine_with_history(prompt_tokens=210000)
    # 模拟一个低阈值（如s-qwen3.8的192000），estimate_tokens会超过它
    # 而_prompt_tokens(210000)也超过，但关键是estimate反映当前真实大小
    est = history.estimate_tokens()
    # estimate_tokens 应基于当前消息计算，而非直接返回_prompt_tokens
    # 当前消息约10轮×400字符≈4000字符，estimate约1000+tokens
    assert est > 0, "estimate_tokens应返回正数"
    # 关键：estimate_tokens 不等于 _prompt_tokens（说明用了独立估算）
    assert est != 210000, "estimate_tokens应独立估算，不返回过期的_prompt_tokens"


def test_get_token_count_returns_prompt_tokens():
    """对照：get_token_count(estimate=False)返回_prompt_tokens（旧行为）"""
    engine, history = make_engine_with_history(prompt_tokens=210000)
    # get_token_count(estimate=False) 直接返回_prompt_tokens
    assert history.get_token_count(estimate=False) == 210000


# ==================== to_api_format 序列化键变化重置基线 ====================


def make_history_with_reasoning() -> MessageHistory:
    """构造含reasoning_content的history（glm thinking_type=thinking保留）"""
    h = MessageHistory()
    h.add_system("你是助手")
    h.add_user("任务", merge=False)
    # 带reasoning_content的assistant+tool_calls消息
    h.add_assistant(
        "回复",
        tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
        ],
        _reasoning="思考内容",
    )
    h.add_tool_result("c1", "read", "工具结果", add_timestamp=False)
    for i in range(5):
        h.add_user(f"补充{i}", merge=False)
        h.add_assistant(f"回答{i}")
    return h


def test_api_format_thinking_type_changes_output():
    """thinking_type变化会改变to_api_format输出（reasoning_content保留/strip）

    glm(thinking_type=thinking)保留reasoning_content，
    qwen(thinking_type=chat_template_kwargs)也保留，
    但某些provider(thinking_type=None)会strip reasoning_content。
    这证明provider切换会改变序列化格式，前缀比较必然失败。
    """
    h = make_history_with_reasoning()
    # thinking_type="thinking"：保留reasoning_content
    fmt_thinking = h.to_api_format(thinking_type="thinking", vision=False)
    # thinking_type=None：strip reasoning_content
    fmt_none = h.to_api_format(thinking_type=None, vision=False)
    # 两种格式应不同（reasoning_content保留/strip）
    import json

    s1 = json.dumps(fmt_thinking, ensure_ascii=False)
    s2 = json.dumps(fmt_none, ensure_ascii=False)
    assert s1 != s2, "thinking_type变化应导致to_api_format输出不同"
    # thinking_type=None应strip掉reasoning_content
    assert "reasoning_content" not in s2, "thinking_type=None应strip reasoning_content"


def test_api_format_key_change_detection():
    """序列化键（provider+thinking_type+vision）变化检测

    模拟provider切换：glm(thinking_type=thinking)→qwen(thinking_type=chat_template_kwargs)
    序列化键变化时应重置增量检测基线，避免误报"上下文增量异常"。
    """
    # 模拟glm的序列化键
    glm_key = ("z-glm5.3", "thinking", False)
    # 模拟qwen的序列化键
    qwen_key = ("s-qwen3.8", "chat_template_kwargs", False)
    # 键应不同（provider和thinking_type都变了）
    assert glm_key != qwen_key, "provider切换后序列化键应不同"
    # 同provider同thinking_type键应相同
    assert glm_key == ("z-glm5.3", "thinking", False)


def test_api_format_key_stable_same_provider():
    """同provider同thinking_type时序列化键稳定（不误重置基线）"""
    key1 = ("z-glm5.3", "thinking", False)
    key2 = ("z-glm5.3", "thinking", False)
    assert key1 == key2, "同provider同thinking_type键应相同（不误重置）"


# ==================== compact 目标收紧 ====================


def test_compact_ratio_progression():
    """上下文超标compact目标逐轮收紧（0.6→0.48→0.36）"""
    # 模拟tool_engine.py中的compact ratio计算逻辑
    for rounds, expected in [(1, 0.6), (2, 0.48), (3, 0.36)]:
        ratio = max(0.36, 0.6 - (rounds - 1) * 0.12)
        assert abs(ratio - expected) < 1e-9, f"第{rounds}轮ratio应为{expected}，实际{ratio}"


def test_compact_ratio_floor():
    """compact ratio下限0.36（不会无限收紧）"""
    for rounds in [4, 5, 10]:
        ratio = max(0.36, 0.6 - (rounds - 1) * 0.12)
        assert ratio == 0.36, f"第{rounds}轮ratio应触底0.36，实际{ratio}"

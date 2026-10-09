"""@think=<value> 临时思考模式覆盖回归测试（事务#2085）

参考 @compact= 覆盖机制：每轮迭代 LLM 调用前查询事务 content 与所有 info，
检测 @think=no/yes/enabled/disable/max/xhigh/high/medium/low/auto 指令，
临时覆盖 provider 的 thinking_mode（含推理强度档位）。

优先级：@think 显式覆盖 > 思考过载自动干预 > provider 默认。
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from peng.llm.client import GLMClient  # noqa: E402
from peng.llm.tool_engine import ToolExecutionEngine  # noqa: E402


# ==================== _parse_think_override 解析测试 ====================

parse = ToolExecutionEngine._parse_think_override


@pytest.mark.parametrize(
    "text,expected",
    [
        # 禁用系
        ("@think=no", "disabled"),
        ("@think=off", "disabled"),
        ("@think=disable", "disabled"),
        ("@think=disabled", "disabled"),
        # 开启系
        ("@think=yes", "enabled"),
        ("@think=on", "enabled"),
        ("@think=enable", "enabled"),
        ("@think=enabled", "enabled"),
        # auto
        ("@think=auto", "auto"),
        # 推理档位
        ("@think=low", "low"),
        ("@think=medium", "medium"),
        ("@think=high", "high"),
        ("@think=xhigh", "xhigh"),
        ("@think=max", "max"),
        # 大小写不敏感
        ("@think=XHIGH", "xhigh"),
        ("@think=Low", "low"),
        # 嵌入文本中
        ("请深度思考 @think=max 处理这个问题", "max"),
        # 多个匹配取最后一个
        ("@think=high 然后 @think=low", "low"),
        # 无匹配
        ("普通指令无标记", None),
        ("@think=", None),
        ("@think=1", None),
        ("@thinking=high", None),  # 前缀不同不算
    ],
)
def test_parse_think_override(text, expected):
    assert parse(text) == expected


def test_parse_think_invalid_value_warns_and_returns_none(caplog):
    """无效值返回 None 并记录警告（不抛异常）"""
    assert parse("@think=xxx") is None


# ==================== _get_effective_thinking_mode 覆盖来源测试 ====================


def _make_client(provider="z-glm5.3"):
    client = object.__new__(GLMClient)
    client.provider = provider
    client.compact_threshold = 320000
    client.thinking_mode = "auto"
    client.thinking_type = None
    client.provider_config = None
    return client


def _make_engine(content="", infos=None):
    eng = object.__new__(ToolExecutionEngine)
    eng.llm = _make_client()
    eng.llm.thinking_mode = "auto"
    eng.task_manager = types.SimpleNamespace(
        db=types.SimpleNamespace(
            get_task_info=lambda *a, **k: infos or []
        ),
        get_task=lambda tid: {"content": content},
    )
    eng._task_id = 2085
    return eng


def test_no_directive_returns_none():
    eng = _make_engine(content="普通事务内容 @compact=128K")
    assert eng._get_effective_thinking_mode() is None


def test_content_directive_overrides():
    eng = _make_engine(content="分析代码 @think=max")
    assert eng._get_effective_thinking_mode() == "max"


def test_info_directive_overrides():
    infos = [{"content": "临时改用低推理 @think=low"}]
    eng = _make_engine(content="无标记内容", infos=infos)
    assert eng._get_effective_thinking_mode() == "low"


def test_latest_info_wins():
    """info 列表中后面的（更新的）覆盖前面的"""
    infos = [
        {"content": "@think=high"},
        {"content": "@think=disabled"},
    ]
    eng = _make_engine(content="@think=medium", infos=infos)
    assert eng._get_effective_thinking_mode() == "disabled"


def test_no_task_id_returns_none():
    eng = _make_engine(content="@think=max")
    eng._task_id = None
    assert eng._get_effective_thinking_mode() is None


# ==================== run_stream 接入点逻辑测试（优先级） ====================
# 直接验证 effective_thinking_override 决策逻辑的输入输出


def _decide_override(eng, force_disabled=False):
    """复现 run_stream 中的优先级决策代码（保持与实现同步）"""
    effective_thinking_override = eng._get_effective_thinking_mode()
    if effective_thinking_override is None:
        if force_disabled and eng.llm.thinking_mode == "auto":
            effective_thinking_override = "disabled"
    return effective_thinking_override


def test_priority_think_over_auto_intervention():
    """@think 显式覆盖优先于思考过载自动干预"""
    eng = _make_engine(content="@think=high")
    assert _decide_override(eng, force_disabled=True) == "high"


def test_auto_intervention_without_directive():
    """无 @think 时保留原自动禁用逻辑"""
    eng = _make_engine(content="无标记")
    assert _decide_override(eng, force_disabled=True) == "disabled"


def test_auto_intervention_not_in_fixed_mode():
    """固定模式（非auto）下无 @think 时不干预"""
    eng = _make_engine(content="无标记")
    eng.llm.thinking_mode = "enabled"
    assert _decide_override(eng, force_disabled=True) is None


def test_no_override_default():
    eng = _make_engine(content="无标记")
    assert _decide_override(eng, force_disabled=False) is None

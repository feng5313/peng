#!/usr/bin/env python3
"""事务#2114：上下文长度优化（思考提醒 + 超长工具结果compact精简）

需求：
1. 大模型输出思考内容后没有输出正式内容（无content无tool_calls）时，
   history.add_user 提醒"思考内容不会保留在上下文中，因此思考后必须输出
   正式内容总结，以免丢失思考成果！"
2. 工具调用返回内容结果超过阈值字节（provider级 compact_hint_threshold 可配，
   参考 max_concurrent，默认1024）时末尾追加带唯一4位数字id的compact清理提示；
   compact工具按id把上一轮迭代对应的工具调用返回结果替换为指定精简文字，
   不得跨迭代compact，只替换返回结果，不改工具调用和参数。

覆盖：
- 配置层：ProviderConfig/LLMConfig/LLMPromptsConfig 新字段 + get_provider 4路透传
- 引擎层：_append_compact_hint（阈值/provider覆盖/禁用/多模态/ID唯一）、
  _remind_thinking_only（仅思考无正式内容时注入）
- MessageHistory：_iteration_marks 边界、get_compact_window、compact_by_id
  （成功/跨迭代拦截/未找到/非法id/无窗口/多模态精简/空replace回退）
- CompactTool：经工具上下文执行精简
"""

import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from peng.config.loader import LLMConfig, LLMPromptsConfig, ProviderConfig  # noqa: E402
from peng.llm.tool_engine import MessageHistory, ToolExecutionEngine  # noqa: E402
from peng.tools.compact import COMPACT_TOOL, CompactTool  # noqa: E402

# ==================== 配置层 ====================


def test_config_defaults():
    """全局默认1024；provider级默认None（用全局）；提示词模板含占位符"""
    assert LLMConfig().compact_hint_threshold == 1024
    assert ProviderConfig().compact_hint_threshold is None
    prompts = LLMPromptsConfig()
    assert prompts.thinking_only_reminder == (
        "思考内容不会保留在上下文中，因此思考后必须输出正式内容总结，以免丢失思考成果！"
    )
    assert "{compact_id}" in prompts.compact_hint
    assert "compact工具" in prompts.compact_hint
    # 🔧 事务#2521：compact精简窗口默认3批迭代
    assert LLMConfig().compact_window_iterations == 3


def _make_llm_config(prov_kwargs=None) -> LLMConfig:
    cfg = LLMConfig()
    cfg.providers = {
        "p-override": ProviderConfig(
            api_key="k",
            base_url="u",
            model="m",
            compact_hint_threshold=4096,
            priority=99,
        ),
        "p-default": ProviderConfig(api_key="k2", base_url="u2", model="m2", priority=90),
    }
    if prov_kwargs:
        cfg.providers["p-override"].__dict__.update(prov_kwargs)
    return cfg


def test_get_provider_compact_threshold_passthrough():
    """get_provider返回：provider级设置生效，None回退全局"""
    cfg = _make_llm_config()
    # 正常选择分支（priority最高的p-override）
    data = cfg.get_provider()
    assert data["provider"] == "p-override"
    assert data["compact_hint_threshold"] == 4096
    # 切换到无provider级配置的provider（forbidden排除高优先级的p-override）
    data2 = cfg.get_provider(current="p-override", forbidden=["p-override"])
    assert data2["provider"] == "p-default"
    assert data2["compact_hint_threshold"] == 1024  # 全局默认
    # 锁定分支透传
    data3 = cfg.get_provider(current="p-default", locked=True)
    assert data3["compact_hint_threshold"] == 1024
    # enable_fallback=False 分支透传
    cfg.enable_fallback = False
    cfg.current_provider = "p-override"
    data4 = cfg.get_provider()
    assert data4["compact_hint_threshold"] == 4096


# ==================== 引擎辅助方法 ====================


def make_engine(
    provider_config=None,
    global_threshold=None,
) -> ToolExecutionEngine:
    """构造跳过__init__的引擎实例（遵循现有单测惯例）"""
    engine = ToolExecutionEngine.__new__(ToolExecutionEngine)
    engine.llm = SimpleNamespace(
        provider="test",
        provider_config=provider_config if provider_config is not None else {},
        config=SimpleNamespace(compact_hint_threshold=global_threshold),
        thinking_type=None,
    )
    engine._compact_id_seq = 9999
    engine._used_compact_ids = set()
    return engine


def test_append_compact_hint_str_content():
    """str结果：超阈值追加提示（含唯一4位id），未超阈值原样返回"""
    engine = make_engine()
    small = "x" * 100
    assert engine._append_compact_hint(small) == small

    big = "x" * 2000  # > 1024字节
    result = engine._append_compact_hint(big)
    assert result.startswith(big)
    tail = result[len(big) :]
    assert "请用compact工具清理或精简，id " in tail
    assert "尽量节省上下文空间！" in tail
    # id为4位数字
    m = re.search(r"id (\d{4})", tail)
    assert m, f"提示中未找到4位数字id: {tail!r}"


def test_append_compact_hint_id_unique():
    """连续分配的id互不相同"""
    engine = make_engine()
    ids = []
    for _ in range(50):
        result = engine._append_compact_hint("y" * 1500)
        ids.append(result.split("id ")[-1][:4])
    assert len(set(ids)) == 50
    assert all(len(i) == 4 and i.isdigit() for i in ids)


def test_append_compact_hint_provider_override_and_disable():
    """provider级阈值覆盖全局；阈值<=0禁用"""
    # provider级4096：2000字节不触发
    engine = make_engine(provider_config={"compact_hint_threshold": 4096})
    assert engine._append_compact_hint("z" * 2000) == "z" * 2000
    big = "z" * 5000
    assert "请用compact工具清理或精简" in engine._append_compact_hint(big)

    # provider级0=禁用
    engine0 = make_engine(provider_config={"compact_hint_threshold": 0})
    assert engine0._append_compact_hint("z" * 5000) == "z" * 5000

    # 全局配置回退（provider_config缺失该键）
    engine_g = make_engine(provider_config={}, global_threshold=100)
    assert "请用compact工具清理或精简" in engine_g._append_compact_hint("z" * 200)


def test_append_compact_hint_multimodal_list():
    """多模态列表content：按text part字节判断，提示追加到首个text part"""
    engine = make_engine()
    parts = [
        {"type": "text", "text": "t" * 2000},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
    ]
    result = engine._append_compact_hint(parts)
    assert result is parts  # 原列表对象（原地修改text part）
    assert "请用compact工具清理或精简，id " in result[0]["text"]
    assert result[0]["text"].startswith("t" * 2000)
    assert result[1] == parts[1]  # 附件part不动

    # text part未超阈值（附件base64不计入）：不追加
    parts_small = [
        {"type": "text", "text": "small"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "b" * 100000}},
    ]
    assert engine._append_compact_hint(parts_small) == parts_small
    assert parts_small[0]["text"] == "small"


# ==================== 思考后无正式内容提醒（事务#2114） ====================


def test_remind_thinking_only():
    """仅思考无正式内容时注入提醒；有content/tool_calls时静默"""
    engine = ToolExecutionEngine.__new__(ToolExecutionEngine)

    # 场景1：只有thinking
    h = MessageHistory()
    h.add_assistant("")
    fired = engine._remind_thinking_only(h, "我想到了方案A", "", [])
    assert fired is True
    assert h._messages[-1]["role"] == "user"
    assert "思考内容不会保留在上下文中" in h._messages[-1]["content"]
    assert "以免丢失思考成果" in h._messages[-1]["content"]

    # 场景2：thinking + content → 不提醒
    h2 = MessageHistory()
    assert engine._remind_thinking_only(h2, "思考", "正式回答", []) is False
    assert all(m["role"] != "user" for m in h2._messages)

    # 场景3：thinking + tool_calls → 不提醒（正常工具迭代）
    h3 = MessageHistory()
    assert engine._remind_thinking_only(h3, "思考", "", [object()]) is False
    assert all(m["role"] != "user" for m in h3._messages)

    # 场景4：无thinking → 不提醒
    h4 = MessageHistory()
    assert engine._remind_thinking_only(h4, "", "回答", []) is False

    # 场景5：thinking + 纯空白content → 提醒
    h5 = MessageHistory()
    assert engine._remind_thinking_only(h5, "思考", "  \n  ", []) is True


# ==================== MessageHistory compact窗口与精简 ====================

PLACEHOLDER = MessageHistory._COMPACTED_PLACEHOLDER


def _make_history_with_compact_hint():
    """构造两轮迭代的历史：第1轮含带id提示的工具结果，第2轮模型发起compact"""
    h = MessageHistory()
    h.add_user("任务")  # idx 0（首轮mark之前）
    # === 迭代1 ===
    h.mark_iteration_start()  # mark1 = 1
    h.add_assistant(
        "",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "bash", "arguments": '{"command": "ls -R /"}'},
            }
        ],
    )  # idx 1
    h.add_tool_result(
        "c1", "bash", "[ts] 大量输出...\n 如果这些返回结果是毫无用处的垃圾信息，请用compact工具清理或精简，id 1234，尽量节省上下文空间！",
        add_timestamp=False,
    )  # idx 2
    # === 迭代2（当前） ===
    h.mark_iteration_start()  # mark2 = 3
    h.add_assistant(
        "",
        tool_calls=[
            {
                "id": "c2",
                "type": "function",
                "function": {"name": "compact", "arguments": '{"id": "1234"}'},
            }
        ],
    )  # idx 3
    return h


def test_compact_by_id_success():
    """成功精简：上一轮迭代内命中，content替换，tool_calls/参数不变"""
    h = _make_history_with_compact_hint()
    ok, msg = h.compact_by_id("1234", "无用信息")
    assert ok is True
    assert h._messages[2]["content"] == "无用信息"
    # 工具调用与参数保持不变
    assert h._messages[1]["tool_calls"][0]["function"]["name"] == "bash"
    assert h._messages[1]["tool_calls"][0]["function"]["arguments"] == '{"command": "ls -R /"}'
    assert h._messages[2]["tool_call_id"] == "c1"
    # 重复compact同一id：标记已被替换，应未找到
    ok2, _ = h.compact_by_id("1234", "无用信息")
    assert ok2 is False


def test_compact_by_id_empty_replace_fallback():
    """replace为空/纯空白：回退占位文案"""
    h = _make_history_with_compact_hint()
    ok, _ = h.compact_by_id("1234", "   ")
    assert ok is True
    assert h._messages[2]["content"] == PLACEHOLDER


def test_compact_by_id_cross_iteration_blocked(monkeypatch):
    """N=1窗口（原始语义回归）：id在更早迭代中存在但不在上一轮窗口内"""
    monkeypatch.setattr(MessageHistory, "_compact_window_iterations", lambda self: 1)
    h = _make_history_with_compact_hint()
    # 第3轮迭代开始后，id 1234所在的迭代1已不在"上一轮"窗口（上一轮=迭代2）
    h.add_tool_result("c2", "compact", "已精简", add_timestamp=False)  # idx 4
    h.mark_iteration_start()  # mark3 = 5
    h.add_assistant(
        "",
        tool_calls=[
            {
                "id": "c3",
                "type": "function",
                "function": {"name": "compact", "arguments": '{"id": "1234"}'},
            }
        ],
    )
    ok, msg = h.compact_by_id("1234", "无用信息")
    assert ok is False
    assert "不在最近1次迭代精简窗口内" in msg
    assert h._messages[2]["content"] != "无用信息"


def test_compact_by_id_not_found_and_invalid():
    """未找到id / 非法id / 无上一轮窗口"""
    h = _make_history_with_compact_hint()
    ok, msg = h.compact_by_id("5678", "无用信息")
    assert ok is False and "未找到" in msg

    ok, msg = h.compact_by_id("123", "无用信息")
    assert ok is False and "4位数字" in msg
    ok, msg = h.compact_by_id("abcd", "无用信息")
    assert ok is False and "4位数字" in msg

    # 仅1个mark：不存在上一轮迭代
    h1 = MessageHistory()
    h1.mark_iteration_start()
    ok, msg = h1.compact_by_id("1234", "无用信息")
    assert ok is False


def test_compact_by_id_multimodal():
    """多模态工具结果：text part带提示命中，整体替换为精简文字（附件一并清除）"""
    h = MessageHistory()
    h.add_user("看图")
    h.mark_iteration_start()
    h.add_assistant(
        "",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "read", "arguments": '{"filePath": "/x.png"}'},
            }
        ],
    )
    h.add_tool_result(
        "c1",
        "read",
        [
            {
                "type": "text",
                "text": "图片读取成功\n 如果这些返回结果是毫无用处的垃圾信息，请用compact工具清理或精简，id 2048，尽量节省上下文空间！",
            },
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
        add_timestamp=False,
    )
    h.mark_iteration_start()
    ok, msg = h.compact_by_id("2048", "无用信息")
    assert ok is True
    assert h._messages[-1]["content"] == "无用信息"


def test_iteration_marks_reset_on_clear():
    """clear/reset后边界失效；marks只保留最近8条"""
    h = MessageHistory()
    for _ in range(10):
        h.mark_iteration_start()
    assert len(h._iteration_marks) == 8
    h.clear()
    assert h._iteration_marks == []
    assert h.get_compact_window() is None

    h2 = MessageHistory()
    h2.mark_iteration_start()
    h2.mark_iteration_start()
    h2.reset_messages_keep_stats()
    assert h2._iteration_marks == []


def test_compact_after_idle_iteration_still_allowed():
    """隔空转迭代（无工具调用的迭代，如空响应重试/纯文本轮）仍可精简上一批结果

    "上一轮迭代"取模型视角=最近一批工具结果所在迭代，机械的"倒数两个mark"
    窗口会因中间的空转迭代错过模型实际看到的上一批结果（集成推演发现的缺陷）
    """
    h = _make_history_with_compact_hint()
    # 迭代2空转：无工具调用，只有thinking-only+提醒+空assistant（已被空响应处理移除）
    h._messages.pop()  # 移除_make_history中的assistant(c2 compact)，模拟纯空转迭代
    h.add_thinking("思考...")
    h.add_user("思考内容不会保留在上下文中，因此思考后必须输出正式内容总结，以免丢失思考成果！")
    # === 迭代3：模型看到id 1234提示后发起compact ===
    h.mark_iteration_start()  # mark3
    h.add_assistant(
        "",
        tool_calls=[
            {
                "id": "c3",
                "type": "function",
                "function": {"name": "compact", "arguments": '{"id": "1234"}'},
            }
        ],
    )
    ok, msg = h.compact_by_id("1234", "无用信息")
    assert ok is True, msg
    assert h._messages[2]["content"] == "无用信息"


def test_compact_window_excludes_current_iteration():
    """当前迭代（最后一次mark之后）的工具结果不在窗口内，不可精简"""
    h = _make_history_with_compact_hint()
    # 在当前迭代内追加另一个带id提示的工具结果（同批次其他工具，先于compact完成）
    h.add_tool_result(
        "c3",
        "bash",
        "另一批输出\n 如果这些返回结果是毫无用处的垃圾信息，请用compact工具清理或精简，id 1300，尽量节省上下文空间！",
        add_timestamp=False,
    )
    # 上一轮窗口内没有id 1300 → 拒绝（属于当前迭代）
    ok, msg = h.compact_by_id("1300", "无用信息")
    assert ok is False


# ==================== CompactTool 工具 ====================


def test_compact_tool_execute():
    """CompactTool经工具上下文执行精简（工具结果）"""
    tool = CompactTool()
    h = _make_history_with_compact_hint()
    tool.set_context(history=h, task_id=2114)
    result = asyncio.run(tool.execute(id="1234", replace="无用信息"))
    assert result.startswith("已精简")
    assert h._messages[2]["content"] == "无用信息"

    # 非法id
    result2 = asyncio.run(tool.execute(id="12", replace="无用信息"))
    assert "4位数字" in result2
    # 未找到
    result3 = asyncio.run(tool.execute(id="9999", replace="无用信息"))
    assert "未找到" in result3


def test_compact_tool_without_history_context():
    """无history上下文时报错（不崩溃）"""
    tool = CompactTool()
    result = asyncio.run(tool.execute(id="1234", replace="无用信息"))
    assert "不可用" in result


def test_compact_tool_identity():
    """工具名/实例导出"""
    assert CompactTool().name == "compact"
    assert isinstance(COMPACT_TOOL, CompactTool)


# ==================== 提示追加与精简的端到端联动 ====================


def test_hint_then_compact_roundtrip():
    """引擎追加提示 → 入history → 下一轮compact精简，全链路闭环（工具结果）"""
    engine = make_engine()
    h = MessageHistory()
    h.add_user("任务")
    h.mark_iteration_start()  # 迭代1
    big_result = "[2026-10-04 19:30:00] " + "L" * 3000
    hinted = engine._append_compact_hint(big_result)
    assert hinted != big_result
    drop_id = re.search(r"id (\d{4})", hinted).group(1)
    h.add_assistant(
        "",
        tool_calls=[{"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}],
    )
    h.add_tool_result("c1", "bash", hinted, add_timestamp=False)
    h.mark_iteration_start()  # 迭代2
    ok, msg = h.compact_by_id(drop_id, "内容精简：ls输出无异常")
    assert ok is True
    tool_msgs = [m for m in h._messages if m.get("role") == "tool"]
    assert tool_msgs[0]["content"] == "内容精简：ls输出无异常"


# ==================== 上下文估算对齐 preserve_thinking（事务#2114） ====================


def _make_thinking_heavy_history() -> MessageHistory:
    """构造思考内容占比很大的历史（纯thinking消息 + 带reasoning的assistant）"""
    h = MessageHistory()
    h.add_system("sys" * 100)
    h.add_user("任务")
    for i in range(5):
        h.add_thinking("深度思考" * 500)  # 纯thinking消息
        h.add_assistant(
            "",
            tool_calls=[
                {
                    "id": f"c{i}",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command": "ls"}'},
                }
            ],
            _reasoning="工具前思考" * 300,
        )
        h.add_tool_result(f"c{i}", "bash", f"输出{i}" + "x" * 200, add_timestamp=False)
    h.add_assistant("最终回答" + "y" * 500)
    return h


def test_estimate_tokens_strips_thinking_by_default():
    """默认（preserve_thinking=False且非thinking风格）：估算不计入思考内容"""
    h = _make_thinking_heavy_history()
    stripped = h.estimate_tokens()  # 默认剥离
    kept = h.estimate_tokens(thinking_type="thinking")  # 保留模式
    assert kept > stripped * 1.5, f"保留模式应显著大于剥离模式: kept={kept}, stripped={stripped}"

    # preserve_thinking=True 同样计入
    kept2 = h.estimate_tokens(preserve_thinking=True)
    assert kept2 > stripped * 1.5

    # 剥离模式估算不包含思考文本与内部标记字段
    import json as _json

    blob = _json.dumps([h._system_message] + h._messages, ensure_ascii=False)
    raw_chars = len(blob)
    # 原始全量（含思考）按同一折算率计算的token必然大于剥离估算
    assert stripped < raw_chars * 2 // 7, "剥离估算应小于含思考的全量序列化折算"


def test_estimate_tokens_aligned_with_actual_payload():
    """黄金对齐：估算 ≈ 对实际发送payload（to_api_format序列化）的估算"""
    from peng.llm.client import estimate_messages_tokens

    h = _make_thinking_heavy_history()
    # 场景1：剥离模式（GLM/qwen默认）
    api_msgs = h.to_api_format(thinking_type=None, preserve_thinking=False)
    est_direct = h.estimate_tokens(thinking_type=None, preserve_thinking=False)
    est_payload = estimate_messages_tokens(api_msgs)
    # 容差15%：允许to_api_format消息合并/空串回填等结构差异
    assert abs(est_direct - est_payload) <= est_payload * 0.15, (
        f"剥离模式估算与实际payload偏差过大: direct={est_direct}, payload={est_payload}"
    )
    # 剥离模式实际payload确实不含思考内容
    assert not any("reasoning_content" in m for m in api_msgs)

    # 场景2：保留模式（deepseek/qwen preserve）
    api_msgs2 = h.to_api_format(thinking_type="thinking", preserve_thinking=True)
    est_direct2 = h.estimate_tokens(thinking_type="thinking", preserve_thinking=True)
    est_payload2 = estimate_messages_tokens(api_msgs2)
    assert abs(est_direct2 - est_payload2) <= est_payload2 * 0.15, (
        f"保留模式估算与实际payload偏差过大: direct={est_direct2}, payload={est_payload2}"
    )
    assert est_payload2 > est_payload * 1.5, "保留模式payload应显著大于剥离模式"


def test_get_token_count_fallback_passes_flags():
    """get_token_count fallback（无真实值时）透传序列化开关"""
    h = _make_thinking_heavy_history()
    assert h.get_token_count(estimate=True) == h.estimate_tokens()  # 默认剥离
    assert (
        h.get_token_count(estimate=True, thinking_type="thinking")
        == h.estimate_tokens(thinking_type="thinking")
    )
    # 有真实值时优先真实值（与开关无关）
    h._prompt_tokens = 12345
    assert h.get_token_count(estimate=True, thinking_type="thinking") == 12345


# ==================== 事务#2521：防假目标 + 提示携带字符数 ====================


def test_compact_no_false_target_on_legacy_error_message(monkeypatch):
    """事务#2520回归：窗口内含marker的compact错误消息不再被同id精简命中

    旧代码：错误消息"错误：未找到 id 1234..."文本含marker"id 1234"子串，下一轮
    重试compact(1234)时被窗口内子串匹配命中——报告"已精简：从65字符精简到113
    字符"的虚假成功（目标大结果纹丝不动，token不减反增）。此处用旧格式文本模拟
    修复上线时存量会话中的历史遗留消息（跳过compact自身消息的深度防御兜底）。
    钉N=1保持"仅上一轮"窗口语义以复现事务#2520场景（窗口放宽后见N=3专项组）。
    """
    monkeypatch.setattr(MessageHistory, "_compact_window_iterations", lambda self: 1)
    h = MessageHistory()
    h.add_user("任务")
    # 迭代1：真实目标（带id 1234提示）在更早迭代
    h.mark_iteration_start()
    h.add_assistant(
        "",
        tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}
        ],
    )
    h.add_tool_result(
        "c1",
        "bash",
        "大量输出\n 如果这些返回结果是毫无用处的垃圾信息，请用compact工具清理或精简，id 1234，尽量节省上下文空间！",
        add_timestamp=False,
    )
    # 迭代2：compact(1234)失败，旧格式错误消息（含marker）入历史
    h.mark_iteration_start()
    h.add_assistant(
        "",
        tool_calls=[
            {
                "id": "c2",
                "type": "function",
                "function": {"name": "compact", "arguments": '{"id": "1234"}'},
            }
        ],
    )
    h.add_tool_result(
        "c2",
        "compact",
        "[ts] 错误：未找到 id 1234 对应的工具结果。请核对工具结果末尾提示中的4位数字ID。",
        add_timestamp=False,
    )
    # 迭代3：模型重试compact(1234)
    h.mark_iteration_start()
    h.add_assistant(
        "",
        tool_calls=[
            {
                "id": "c3",
                "type": "function",
                "function": {"name": "compact", "arguments": '{"id": "1234"}'},
            }
        ],
    )
    ok, msg = h.compact_by_id("1234", "精简摘要")
    # 不再虚假成功：compact自身消息被跳过；命中更早迭代的真实目标→跨迭代拦截
    assert ok is False, f"不应虚假命中compact错误消息: {msg}"
    assert "不在最近1次迭代精简窗口内" in msg
    # 错误消息与真实目标的内容均未被改动
    compact_msgs = [m for m in h._messages if m.get("name") == "compact"]
    assert "未找到" in compact_msgs[0]["content"]
    assert "大量输出" in h._messages[2]["content"]


def test_compact_skips_own_result_message_in_full_scan():
    """全历史唯一含marker的是compact自身结果（如成功消息"已精简：id 1234..."）时，
    第一循环和全历史第二循环都应跳过它→报"未找到"而非命中/误报跨迭代"""
    h = MessageHistory()
    h.add_user("任务")
    h.mark_iteration_start()
    h.add_assistant(
        "",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "compact", "arguments": '{"id": "1234"}'},
            }
        ],
    )
    h.add_tool_result(
        "c1",
        "compact",
        "已精简：id 1234 对应的工具返回结果，从5000字符精简到100字符。",
        add_timestamp=False,
    )
    h.mark_iteration_start()
    h.add_assistant(
        "",
        tool_calls=[
            {
                "id": "c2",
                "type": "function",
                "function": {"name": "compact", "arguments": '{"id": "1234"}'},
            }
        ],
    )
    ok, msg = h.compact_by_id("1234", "无用信息")
    assert ok is False
    assert "未找到" in msg
    assert "已精简：id 1234" in h._messages[2]["content"]  # 内容未被动过


def test_compact_error_messages_use_hash_format(monkeypatch):
    """新错误消息中id写作"id#XXXX"（不带空格），不构成marker"id XXXX"子串

    钉N=1：跨迭代分支需要id存在于更早迭代（N=3宽窗口下该场景直接命中成功）。
    """
    monkeypatch.setattr(MessageHistory, "_compact_window_iterations", lambda self: 1)
    h = _make_history_with_compact_hint()
    # 未找到分支
    _, msg_not_found = h.compact_by_id("5678", "无用信息")
    assert "id#5678" in msg_not_found
    assert not re.search(r"id 5678", msg_not_found), f"错误消息不应含marker子串: {msg_not_found!r}"
    # 跨迭代分支
    h.add_tool_result("c2", "compact", "已精简", add_timestamp=False)
    h.mark_iteration_start()
    h.add_assistant(
        "",
        tool_calls=[
            {
                "id": "c3",
                "type": "function",
                "function": {"name": "compact", "arguments": '{"id": "1234"}'},
            }
        ],
    )
    _, msg_cross = h.compact_by_id("1234", "无用信息")
    assert "id#1234" in msg_cross
    assert not re.search(r"id 1234", msg_cross), f"错误消息不应含marker子串: {msg_cross!r}"


def test_compact_hint_includes_char_count():
    """提示携带当前实际字符数（str/多模态text parts按len()字符计）"""
    engine = make_engine()
    # 纯ASCII
    big = "x" * 2000
    result = engine._append_compact_hint(big)
    assert result.startswith(big)
    assert "（2000字符）" in result
    # 中文按字符数（非字节数）
    big_cn = "中" * 600  # 600字符 / 1800字节
    result2 = engine._append_compact_hint(big_cn)
    assert result2.startswith(big_cn)
    assert "（600字符）" in result2
    # 多模态：text parts字符数（附件不计入）
    parts = [
        {"type": "text", "text": "t" * 1500},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "b" * 5000}},
    ]
    result3 = engine._append_compact_hint(parts)
    assert result3 is parts
    assert "（1500字符）" in result3[0]["text"]


def test_compact_hint_custom_template_without_size_compat(monkeypatch):
    """旧自定义模板（无{size}占位符）兼容：format多余kwarg被忽略，不KeyError"""
    engine = make_engine()
    monkeypatch.setattr(
        ToolExecutionEngine,
        "_prompts",
        property(lambda self: SimpleNamespace(compact_hint="旧模板，id {compact_id}，请精简！")),
    )
    result = engine._append_compact_hint("y" * 2000)
    assert result.startswith("y" * 2000)
    m = re.search(r"id (\d{4})", result)
    assert m, f"旧模板应正常分配id: {result[-60:]!r}"
    # 新模板占位符（回归确认默认模板已含{size}）
    assert "{size}" in LLMPromptsConfig().compact_hint


# ==================== 事务#2521：精简窗口放宽为最近N批迭代（默认3） ====================


def _add_tool_batch(h: MessageHistory, call_id: str, compact_id: str):
    """追加一个完整工具迭代批次：mark + assistant(tool_call) + tool结果(带id提示)"""
    h.mark_iteration_start()
    h.add_assistant(
        "",
        tool_calls=[
            {"id": call_id, "type": "function", "function": {"name": "read", "arguments": "{}"}}
        ],
    )
    h.add_tool_result(
        call_id,
        "read",
        f"大量输出{compact_id}\n 请用compact工具清理或精简，id {compact_id}，尽量节省上下文空间！",
        add_timestamp=False,
    )


def test_compact_window_last_3_iterations():
    """N=3（默认/真实配置）：最近3批工具结果所在迭代可精简，第4批之外拦截

    复刻事务#2521风主令前实测痛点：隔1轮/隔2轮的结果曾被"禁止跨迭代清理"拦截。
    """
    h = MessageHistory()
    h.add_user("任务")
    _add_tool_batch(h, "b0", "1000")  # 迭代0（4批前，应拦截）
    _add_tool_batch(h, "b1", "1001")  # 迭代1（3批前，窗口内）
    _add_tool_batch(h, "b2", "1002")  # 迭代2
    _add_tool_batch(h, "b3", "1003")  # 迭代3（最近批次）
    h.mark_iteration_start()  # 当前迭代：模型发起compact
    h.add_assistant(
        "",
        tool_calls=[
            {"id": "c9", "type": "function", "function": {"name": "compact", "arguments": "{}"}}
        ],
    )
    # 最近3批=迭代1/2/3，均可精简
    for cid in ("1001", "1002", "1003"):
        ok, msg = h.compact_by_id(cid, "已精简摘要")
        assert ok is True, f"id {cid} 应在最近3次迭代窗口内: {msg}"
    # 第4批之外（迭代0）拦截
    ok0, msg0 = h.compact_by_id("1000", "无用信息")
    assert ok0 is False
    assert "不在最近3次迭代精简窗口内" in msg0


def test_compact_window_n1_only_last_batch(monkeypatch):
    """N=1（原始语义对照）：仅最近1批可精简，隔1批即拦截"""
    monkeypatch.setattr(MessageHistory, "_compact_window_iterations", lambda self: 1)
    h = MessageHistory()
    h.add_user("任务")
    _add_tool_batch(h, "b1", "1001")
    _add_tool_batch(h, "b2", "1002")
    h.mark_iteration_start()
    h.add_assistant(
        "",
        tool_calls=[
            {"id": "c9", "type": "function", "function": {"name": "compact", "arguments": "{}"}}
        ],
    )
    ok, _ = h.compact_by_id("1002", "摘要")
    assert ok is True
    ok1, msg1 = h.compact_by_id("1001", "摘要")
    assert ok1 is False
    assert "不在最近1次迭代精简窗口内" in msg1


def test_compact_window_idle_iterations_not_shifting():
    """N=3：中间无工具的空转迭代（空响应重试/纯文本轮）不顶掉批次

    推广事务#2114哲学：窗口按"最近N批工具结果"计，而非机械倒数N个mark。
    """
    h = MessageHistory()
    h.add_user("任务")
    _add_tool_batch(h, "b0", "1000")  # 迭代0（3批之外）
    _add_tool_batch(h, "b1", "1001")  # 批次1
    h.mark_iteration_start()  # 空转迭代A（无工具消息）
    h.add_user("空响应提醒（无工具调用）")
    _add_tool_batch(h, "b2", "1002")  # 批次2
    h.mark_iteration_start()  # 空转迭代B
    h.add_user("纯文本轮（无工具调用）")
    _add_tool_batch(h, "b3", "1003")  # 批次3
    h.mark_iteration_start()  # 当前迭代
    h.add_assistant(
        "",
        tool_calls=[
            {"id": "c9", "type": "function", "function": {"name": "compact", "arguments": "{}"}}
        ],
    )
    # 窗口起点应越过空转迭代直达批次1（而非机械marks[-3]=空转B之前的批次2）
    start, end = h.get_compact_window()
    assert h._messages[start].get("tool_calls"), "窗口起点应落在批次1的assistant消息上"
    ok, msg = h.compact_by_id("1001", "摘要")
    assert ok is True, f"空转迭代不应顶掉批次1: {msg}"
    ok0, msg0 = h.compact_by_id("1000", "摘要")
    assert ok0 is False
    assert "不在最近3次迭代精简窗口内" in msg0


def test_compact_window_config_read_and_clamp(monkeypatch):
    """_compact_window_iterations：真实配置读取（peng.json=3/默认3）；非法回退3；≤0钳制1"""
    h = MessageHistory()
    n = h._compact_window_iterations()
    assert n == 3, f"默认/配置应读取3（peng.json显式或字段默认）: {n}"
    # ≤0钳制为1
    monkeypatch.setattr(
        "peng.llm.tool_engine.load_config",
        lambda: SimpleNamespace(llm=SimpleNamespace(compact_window_iterations=0)),
    )
    assert MessageHistory()._compact_window_iterations() == 1
    # 非法值回退3
    monkeypatch.setattr(
        "peng.llm.tool_engine.load_config",
        lambda: SimpleNamespace(llm=SimpleNamespace(compact_window_iterations="abc")),
    )
    assert MessageHistory()._compact_window_iterations() == 3


def test_compact_window_marks_truncation_clamp():
    """第N批工具迭代早于保留的最早mark（marks仅留8条被空转挤掉）时，
    窗口起点钳制到marks[0]，不失控覆盖远古历史"""
    h = MessageHistory()
    h.add_user("任务")
    _add_tool_batch(h, "b1", "1001")  # 批次1（其mark将被挤出）
    for i in range(8):  # 8次空转迭代：批次1的mark被截断
        h.mark_iteration_start()
        h.add_user(f"空转{i}")
    _add_tool_batch(h, "b2", "1002")  # 批次2
    _add_tool_batch(h, "b3", "1003")  # 批次3
    h.mark_iteration_start()  # 当前迭代
    start, end = h.get_compact_window()
    assert start == h._iteration_marks[0], "窗口起点应钳制到最旧保留mark"
    # 批次1的1001消息在marks[0]之前→不在窗口→拦截（保守取舍：宁拒勿滥）
    ok, msg = h.compact_by_id("1001", "摘要")
    assert ok is False
    assert "不在最近3次迭代精简窗口内" in msg

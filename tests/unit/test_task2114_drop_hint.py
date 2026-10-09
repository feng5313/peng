#!/usr/bin/env python3
"""事务#2114：上下文长度优化（思考提醒 + 超长工具结果drop清理）

需求：
1. 大模型输出思考内容后没有输出正式内容（无content无tool_calls）时，
   history.add_user 提醒"思考内容不会保留在上下文中，因此思考后必须输出
   正式内容总结，以免丢失思考成果！"
2. 工具调用返回内容结果超过阈值字节（provider级 drop_hint_threshold 可配，
   参考 max_concurrent，默认1024）时末尾追加带唯一4位数字id的drop清理提示；
   drop工具按id把上一轮迭代对应的工具调用返回结果替换为
   "垃圾信息，已被drop工具清理。"，不得跨迭代drop，只替换返回结果，
   不改工具调用和参数。

覆盖：
- 配置层：ProviderConfig/LLMConfig/LLMPromptsConfig 新字段 + get_provider 4路透传
- 引擎层：_append_drop_hint（阈值/provider覆盖/禁用/多模态/ID唯一）、
  _remind_thinking_only（仅思考无正式内容时注入）
- MessageHistory：_iteration_marks 边界、get_drop_window、drop_tool_result
  （成功/跨迭代拦截/未找到/非法id/无窗口/多模态清理）
- DropTool：经工具上下文执行清理
"""

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from peng.config.loader import LLMConfig, LLMPromptsConfig, ProviderConfig  # noqa: E402
from peng.llm.tool_engine import MessageHistory, ToolExecutionEngine  # noqa: E402
from peng.tools.drop import DropTool  # noqa: E402

# ==================== 配置层 ====================


def test_config_defaults():
    """全局默认1024；provider级默认None（用全局）；提示词模板含占位符"""
    assert LLMConfig().drop_hint_threshold == 1024
    assert ProviderConfig().drop_hint_threshold is None
    prompts = LLMPromptsConfig()
    assert prompts.thinking_only_reminder == (
        "思考内容不会保留在上下文中，因此思考后必须输出正式内容总结，以免丢失思考成果！"
    )
    assert "{drop_id}" in prompts.drop_hint
    assert "drop工具清理" in prompts.drop_hint


def _make_llm_config(prov_kwargs=None) -> LLMConfig:
    cfg = LLMConfig()
    cfg.providers = {
        "p-override": ProviderConfig(
            api_key="k", base_url="u", model="m", drop_hint_threshold=4096, priority=99
        ),
        "p-default": ProviderConfig(api_key="k2", base_url="u2", model="m2", priority=90),
    }
    if prov_kwargs:
        cfg.providers["p-override"].__dict__.update(prov_kwargs)
    return cfg


def test_get_provider_drop_hint_threshold_passthrough():
    """get_provider返回：provider级设置生效，None回退全局"""
    cfg = _make_llm_config()
    # 正常选择分支（priority最高的p-override）
    data = cfg.get_provider()
    assert data["provider"] == "p-override"
    assert data["drop_hint_threshold"] == 4096
    # 切换到无provider级配置的provider（forbidden排除高优先级的p-override）
    data2 = cfg.get_provider(current="p-override", forbidden=["p-override"])
    assert data2["provider"] == "p-default"
    assert data2["drop_hint_threshold"] == 1024  # 全局默认
    # 锁定分支透传
    data3 = cfg.get_provider(current="p-default", locked=True)
    assert data3["drop_hint_threshold"] == 1024
    # enable_fallback=False 分支透传
    cfg.enable_fallback = False
    cfg.current_provider = "p-override"
    data4 = cfg.get_provider()
    assert data4["drop_hint_threshold"] == 4096


# ==================== 引擎辅助方法 ====================


def make_engine(provider_config=None, global_threshold=None) -> ToolExecutionEngine:
    """构造跳过__init__的引擎实例（遵循现有单测惯例）"""
    engine = ToolExecutionEngine.__new__(ToolExecutionEngine)
    engine.llm = SimpleNamespace(
        provider="test",
        provider_config=provider_config if provider_config is not None else {},
        config=SimpleNamespace(drop_hint_threshold=global_threshold),
    )
    engine._drop_id_seq = 9999
    engine._used_drop_ids = set()
    return engine


def test_append_drop_hint_str_content():
    """str结果：超阈值追加提示（含唯一4位id），未超阈值原样返回"""
    engine = make_engine()
    small = "x" * 100
    assert engine._append_drop_hint(small) == small

    big = "x" * 2000  # > 1024字节
    result = engine._append_drop_hint(big)
    assert result.startswith(big)
    tail = result[len(big) :]
    assert "请用drop工具清理，id " in tail
    assert "尽量节省上下文空间！" in tail
    # id为4位数字
    import re

    m = re.search(r"id (\d{4})", tail)
    assert m, f"提示中未找到4位数字id: {tail!r}"


def test_append_drop_hint_id_unique():
    """连续分配的id互不相同"""
    engine = make_engine()
    ids = []
    for _ in range(50):
        result = engine._append_drop_hint("y" * 1500)
        ids.append(result.split("id ")[-1][:4])
    assert len(set(ids)) == 50
    assert all(len(i) == 4 and i.isdigit() for i in ids)


def test_append_drop_hint_provider_override_and_disable():
    """provider级阈值覆盖全局；阈值<=0禁用"""
    # provider级4096：2000字节不触发
    engine = make_engine(provider_config={"drop_hint_threshold": 4096})
    assert engine._append_drop_hint("z" * 2000) == "z" * 2000
    big = "z" * 5000
    assert "请用drop工具清理" in engine._append_drop_hint(big)

    # provider级0=禁用
    engine0 = make_engine(provider_config={"drop_hint_threshold": 0})
    assert engine0._append_drop_hint("z" * 5000) == "z" * 5000

    # 全局配置回退（provider_config缺失该键）
    engine_g = make_engine(provider_config={}, global_threshold=100)
    assert "请用drop工具清理" in engine_g._append_drop_hint("z" * 200)


def test_append_drop_hint_multimodal_list():
    """多模态列表content：按text part字节判断，提示追加到首个text part"""
    engine = make_engine()
    parts = [
        {"type": "text", "text": "t" * 2000},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
    ]
    result = engine._append_drop_hint(parts)
    assert result is parts  # 原列表对象（原地修改text part）
    assert "请用drop工具清理，id " in result[0]["text"]
    assert result[0]["text"].startswith("t" * 2000)
    assert result[1] == parts[1]  # 附件part不动

    # text part未超阈值（附件base64不计入）：不追加
    parts_small = [
        {"type": "text", "text": "small"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "b" * 100000}},
    ]
    assert engine._append_drop_hint(parts_small) == parts_small
    assert parts_small[0]["text"] == "small"


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


# ==================== MessageHistory drop窗口与清理 ====================


def _make_history_with_drop_hint():
    """构造两轮迭代的历史：第1轮含带id提示的工具结果，第2轮模型发起drop"""
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
        "c1", "bash", "[ts] 大量输出...\n 如果这些返回结果是毫无用处的垃圾信息，请用drop工具清理，id 1234，尽量节省上下文空间！",
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
                "function": {"name": "drop", "arguments": '{"id": "1234"}'},
            }
        ],
    )  # idx 3
    return h


def test_drop_tool_result_success():
    """成功清理：上一轮迭代内命中，content替换，tool_calls/参数不变"""
    h = _make_history_with_drop_hint()
    ok, msg = h.drop_tool_result("1234")
    assert ok is True
    assert h._messages[2]["content"] == "垃圾信息，已被drop工具清理。"
    # 工具调用与参数保持不变
    assert h._messages[1]["tool_calls"][0]["function"]["name"] == "bash"
    assert h._messages[1]["tool_calls"][0]["function"]["arguments"] == '{"command": "ls -R /"}'
    assert h._messages[2]["tool_call_id"] == "c1"
    # 重复drop同一id：标记已被替换，应未找到
    ok2, _ = h.drop_tool_result("1234")
    assert ok2 is False


def test_drop_tool_result_cross_iteration_blocked():
    """跨迭代拦截：id在更早迭代中存在但不在上一轮窗口内"""
    h = _make_history_with_drop_hint()
    # 第3轮迭代开始后，id 1234所在的迭代1已不在"上一轮"窗口（上一轮=迭代2）
    h.add_tool_result("c2", "drop", "已清理", add_timestamp=False)  # idx 4
    h.mark_iteration_start()  # mark3 = 5
    h.add_assistant(
        "",
        tool_calls=[
            {"id": "c3", "type": "function", "function": {"name": "drop", "arguments": '{"id": "1234"}'}}
        ],
    )
    ok, msg = h.drop_tool_result("1234")
    assert ok is False
    assert "不属于上一轮迭代" in msg
    assert "垃圾信息，已被drop工具清理。" not in h._messages[2]["content"]


def test_drop_tool_result_not_found_and_invalid():
    """未找到id / 非法id / 无上一轮窗口"""
    h = _make_history_with_drop_hint()
    ok, msg = h.drop_tool_result("5678")
    assert ok is False and "未找到" in msg

    ok, msg = h.drop_tool_result("123")
    assert ok is False and "4位数字" in msg
    ok, msg = h.drop_tool_result("abcd")
    assert ok is False and "4位数字" in msg

    # 仅1个mark：不存在上一轮迭代
    h1 = MessageHistory()
    h1.mark_iteration_start()
    ok, msg = h1.drop_tool_result("1234")
    assert ok is False


def test_drop_tool_result_multimodal():
    """多模态工具结果：text part带提示命中，整体替换为占位文案（附件一并清除）"""
    h = MessageHistory()
    h.add_user("看图")
    h.mark_iteration_start()
    h.add_assistant(
        "",
        tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"filePath": "/x.png"}'}}
        ],
    )
    h.add_tool_result(
        "c1",
        "read",
        [
            {"type": "text", "text": "图片读取成功\n 如果这些返回结果是毫无用处的垃圾信息，请用drop工具清理，id 2048，尽量节省上下文空间！"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
        add_timestamp=False,
    )
    h.mark_iteration_start()
    ok, msg = h.drop_tool_result("2048")
    assert ok is True
    assert h._messages[-1]["content"] == "垃圾信息，已被drop工具清理。"


def test_iteration_marks_reset_on_clear():
    """clear/reset后边界失效；marks只保留最近8条"""
    h = MessageHistory()
    for _ in range(10):
        h.mark_iteration_start()
    assert len(h._iteration_marks) == 8
    h.clear()
    assert h._iteration_marks == []
    assert h.get_drop_window() is None

    h2 = MessageHistory()
    h2.mark_iteration_start()
    h2.mark_iteration_start()
    h2.reset_messages_keep_stats()
    assert h2._iteration_marks == []


def test_drop_after_idle_iteration_still_allowed():
    """隔空转迭代（无工具调用的迭代，如空响应重试/纯文本轮）仍可清理上一批结果

    "上一轮迭代"取模型视角=最近一批工具结果所在迭代，机械的"倒数两个mark"
    窗口会因中间的空转迭代错过模型实际看到的上一批结果（集成推演发现的缺陷）
    """
    h = _make_history_with_drop_hint()
    # 迭代2空转：无工具调用，只有thinking-only+提醒+空assistant（已被空响应处理移除）
    h._messages.pop()  # 移除_make_history中的assistant(c2 drop)，模拟纯空转迭代
    h.add_thinking("思考...")
    h.add_user("思考内容不会保留在上下文中，因此思考后必须输出正式内容总结，以免丢失思考成果！")
    # === 迭代3：模型看到id 1234提示后发起drop ===
    h.mark_iteration_start()  # mark3
    h.add_assistant(
        "",
        tool_calls=[
            {"id": "c3", "type": "function", "function": {"name": "drop", "arguments": '{"id": "1234"}'}}
        ],
    )
    ok, msg = h.drop_tool_result("1234")
    assert ok is True, msg
    assert h._messages[2]["content"] == "垃圾信息，已被drop工具清理。"


def test_drop_window_excludes_current_iteration():
    """当前迭代（最后一次mark之后）的工具结果不在窗口内，不可清理"""
    h = _make_history_with_drop_hint()
    # 在当前迭代内追加另一个带id提示的工具结果（同批次其他工具，先于drop完成）
    h.add_tool_result(
        "c3", "bash", "另一批输出\n 如果这些返回结果是毫无用处的垃圾信息，请用drop工具清理，id 1300，尽量节省上下文空间！",
        add_timestamp=False,
    )
    # 上一轮窗口内没有id 1300 → 拒绝（属于当前迭代）
    ok, msg = h.drop_tool_result("1300")
    assert ok is False


# ==================== DropTool 工具 ====================


def test_drop_tool_execute():
    """DropTool经工具上下文执行清理"""
    tool = DropTool()
    h = _make_history_with_drop_hint()
    tool.set_context(history=h, task_id=2114)
    result = __import__("asyncio").run(tool.execute(id="1234"))
    assert result.startswith("已清理")
    assert h._messages[2]["content"] == "垃圾信息，已被drop工具清理。"

    # 非法id
    result2 = __import__("asyncio").run(tool.execute(id="12"))
    assert "4位数字" in result2
    # 未找到
    result3 = __import__("asyncio").run(tool.execute(id="9999"))
    assert "未找到" in result3


def test_drop_tool_without_history_context():
    """无history上下文时报错（不崩溃）"""
    tool = DropTool()
    result = __import__("asyncio").run(tool.execute(id="1234"))
    assert "不可用" in result


# ==================== 提示追加与清理的端到端联动 ====================


def test_hint_then_drop_roundtrip():
    """引擎追加提示 → 入history → 下一轮drop清理，全链路闭环"""
    engine = make_engine()
    h = MessageHistory()
    h.add_user("任务")
    h.mark_iteration_start()  # 迭代1
    big_result = "[2026-10-04 19:30:00] " + "L" * 3000
    hinted = engine._append_drop_hint(big_result)
    assert hinted != big_result
    import re

    drop_id = re.search(r"id (\d{4})", hinted).group(1)
    h.add_assistant(
        "",
        tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}
        ],
    )
    h.add_tool_result("c1", "bash", hinted, add_timestamp=False)
    h.mark_iteration_start()  # 迭代2
    ok, msg = h.drop_tool_result(drop_id)
    assert ok is True
    tool_msgs = [m for m in h._messages if m.get("role") == "tool"]
    assert tool_msgs[0]["content"] == "垃圾信息，已被drop工具清理。"


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

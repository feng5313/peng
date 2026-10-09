#!/usr/bin/env python3
"""事务#2111：qwen 系列 provider 支持 preserve_thinking 配置（默认关闭省token）

需求：config/peng.json 的 provider 配置支持可选 preserve_thinking，
默认 False（序列化请求/history持久化时剥离 reasoning_content 节省 token）。

实机验证（epyc:8080 swift-1.5 qwen 后端，curl 对照实验）：
- assistant 消息（content+reasoning_content 合并形式）→ 后端把 reasoning_content
  渲染回 prompt 并计费：prompt_tokens 53→153，模型能读到思考内容
  （思考中列出10种水果，问"提到几种水果"正确答"10"；剥离时答"0"）
- 独立空 content 的 thinking 消息 → HTTP 200 但模型读不到（无效形式）
  → 保留模式必须用合并形式输出

实现：
- ProviderConfig 新增 preserve_thinking: bool = False；
  get_provider() 4 处返回字典透传该字段
- MessageHistory.to_api_format(to_context) 新增 preserve_thinking 参数：
  keep_reasoning = thinking_type=="thinking" or preserve_thinking
  - thinking-only 消息：非保留模式跳过；保留模式合并进相邻 assistant 消息
  - assistant 的 reasoning_content：保留模式输出，非保留模式剥离
  - DeepSeek 空串回填逻辑保持仅 "thinking" 风格（qwen 后端无此要求）
- ToolEngine 请求构建/重试重建/_save_history/迭代备份/工具回滚备份均按
  当前 provider_config.preserve_thinking 序列化；_fmt_key 纳入该标志
"""

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from peng.config.loader import LLMConfig, ProviderConfig  # noqa: E402
from peng.llm.tool_engine import MessageHistory, ToolExecutionEngine  # noqa: E402


def _make_history() -> MessageHistory:
    """构造含 thinking / tool_calls / 普通 assistant 的典型历史"""
    h = MessageHistory()
    h.add_system("sys")
    h.add_user("任务", merge=False)
    # 第1轮：thinking + tool_calls（存储时已合并，add_assistant(_reasoning=...)）
    h.add_assistant(
        "",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "read", "arguments": '{"filePath": "/tmp/a"}'},
            }
        ],
        _reasoning="思考读取文件",
    )
    h.add_tool_result("c1", "read", "文件内容A", add_timestamp=False)
    # 第2轮：thinking-only + 普通 assistant（run循环分离存储路径）
    h.add_thinking("思考第二轮答案")
    h.add_assistant("这是最终回答")
    h.add_user("追问", merge=False)
    return h


def _assistant_msgs(out: list[dict]) -> list[dict]:
    return [m for m in out if m.get("role") == "assistant"]


# ==================== 默认关闭：剥离思考内容（省token） ====================


def test_qwen_default_strips_thinking():
    """qwen thinking_type 默认（preserve_thinking=False）剥离 reasoning_content"""
    h = _make_history()
    out = h.to_api_format(thinking_type="chat_template_kwargs", vision=False)
    assistants = _assistant_msgs(out)
    assert len(assistants) == 2  # thinking-only 被跳过，仅2条assistant
    for m in assistants:
        assert "reasoning_content" not in m, f"默认模式不应保留: {m}"
    # tool_calls 消息保留 tool_calls
    assert assistants[0]["tool_calls"][0]["id"] == "c1"
    # 普通回答保留 content
    assert assistants[1]["content"] == "这是最终回答"


def test_to_api_format_default_param_strips():
    """不传 preserve_thinking（默认 False）时与显式 False 一致"""
    h = _make_history()
    out_default = h.to_api_format(thinking_type="qwen")
    out_false = h.to_api_format(thinking_type="qwen", preserve_thinking=False)
    assert out_default == out_false
    assert all("reasoning_content" not in m for m in _assistant_msgs(out_default))


def test_glm_still_strips_with_explicit_false():
    """glm 风格显式 preserve_thinking=False 保持剥离"""
    h = _make_history()
    out = h.to_api_format(thinking_type="glm", preserve_thinking=False)
    assert all("reasoning_content" not in m for m in _assistant_msgs(out))


# ==================== 开启保留：qwen 复用上轮思考 ====================


def test_qwen_preserve_keeps_reasoning_content():
    """preserve_thinking=True：assistant 的 reasoning_content 保留"""
    h = _make_history()
    out = h.to_api_format(
        thinking_type="chat_template_kwargs", vision=False, preserve_thinking=True
    )
    assistants = _assistant_msgs(out)
    # thinking-only 合并进相邻 assistant：c1(tool_calls+rc) + 最终回答(merged rc)
    assert len(assistants) == 2
    assert assistants[0]["reasoning_content"] == "思考读取文件"
    assert assistants[0]["tool_calls"][0]["id"] == "c1"
    # 分离存储的 thinking-only 与普通 assistant 合并为一条消息（后端实测
    # 独立空content的thinking消息读不到，合并形式有效）
    assert assistants[1]["content"] == "这是最终回答"
    assert assistants[1]["reasoning_content"] == "思考第二轮答案"
    assert "tool_calls" not in assistants[1]


def test_qwen_preserve_standalone_thinking_tail():
    """preserve_thinking=True：末尾无相邻 assistant 的 thinking-only 独立输出
    （content:"" + reasoning_content，后端200可接受）"""
    h = MessageHistory()
    h.add_system("sys")
    h.add_user("任务", merge=False)
    h.add_thinking("被打断的思考")
    out = h.to_api_format(
        thinking_type="chat_template_kwargs", vision=False, preserve_thinking=True
    )
    thinking_msgs = [m for m in out if m.get("reasoning_content")]
    assert len(thinking_msgs) == 1
    assert thinking_msgs[0]["role"] == "assistant"
    assert thinking_msgs[0]["content"] == ""
    assert thinking_msgs[0]["reasoning_content"] == "被打断的思考"


def test_qwen_preserve_no_empty_reasoning_backfill():
    """qwen+preserve 不触发 DeepSeek 式空串回填（普通assistant保持无rc字段）

    后端无 DeepSeek 的"所有assistant必须带rc"要求，注入空串反而可能干扰模板。
    """
    h = MessageHistory()
    h.add_system("sys")
    h.add_user("任务", merge=False)
    h.add_assistant("无思考的直接回答")  # 钩子注入/无思考回复
    out = h.to_api_format(
        thinking_type="chat_template_kwargs", vision=False, preserve_thinking=True
    )
    m = _assistant_msgs(out)[0]
    assert "reasoning_content" not in m


def test_preserve_round_trip_restore():
    """保留模式输出 → to_context 持久化 → from_str 恢复不丢失 thinking"""
    h = _make_history()
    import json

    ctx = h.to_context(thinking_type="chat_template_kwargs", preserve_thinking=True)
    h2 = MessageHistory()
    assert h2.from_str(json.dumps(ctx, ensure_ascii=False))
    out2 = h2.to_api_format(
        thinking_type="chat_template_kwargs", preserve_thinking=True
    )
    out1 = h.to_api_format(thinking_type="chat_template_kwargs", preserve_thinking=True)
    assert out1 == out2


# ==================== DeepSeek "thinking" 回归（行为不变） ====================


def test_deepseek_thinking_mode_unchanged():
    """thinking_type="thinking" 时无论 preserve 与否都保留 rc + 空串回填"""
    h = _make_history()
    out = h.to_api_format(thinking_type="thinking", vision=False)
    assistants = _assistant_msgs(out)
    # DeepSeek：全量回填（含回溯合并），thinking 不丢
    rc_values = [m.get("reasoning_content", "<missing>") for m in assistants]
    assert "思考读取文件" in rc_values
    assert all(m.get("reasoning_content") is not None for m in assistants), rc_values
    # preserve_thinking=True 不改变 "thinking" 风格输出
    out_p = h.to_api_format(thinking_type="thinking", vision=False, preserve_thinking=True)
    assert out_p == out


# ==================== 配置层：字段解析与 get_provider 透传 ====================


def _make_llm_config() -> LLMConfig:
    cfg = LLMConfig()
    cfg.providers = {
        "s-qwen3.8": ProviderConfig(
            api_key="k",
            base_url="http://a",
            model="qwen",
            icon="🐥",
            enable=True,
            priority=93,
            preserve_thinking=True,
        ),
        "z-glm5.3": ProviderConfig(
            api_key="k", base_url="http://b", model="glm", icon="🦊", enable=True, priority=96
        ),
        "deepseek-pro": ProviderConfig(
            api_key="k",
            base_url="http://c",
            model="ds",
            icon="🐋",
            enable=True,
            priority=88,
            thinking_type="thinking",
        ),
    }
    return cfg


def test_provider_config_default_false():
    p = ProviderConfig(api_key="k", base_url="http://a", model="qwen")
    assert p.preserve_thinking is False


def test_get_provider_locked_path_returns_flag():
    cfg = _make_llm_config()
    r = cfg.get_provider(current="s-qwen3.8", favorites=["s-qwen3.8"], locked=True)
    assert r["provider"] == "s-qwen3.8" and r["preserve_thinking"] is True


def test_get_provider_current_reuse_path_returns_flag():
    cfg = _make_llm_config()
    r = cfg.get_provider(current="z-glm5.3")
    assert r["provider"] == "z-glm5.3" and r["preserve_thinking"] is False


def test_get_provider_switch_path_returns_flag():
    cfg = _make_llm_config()
    cfg.current_provider = "s-qwen3.8"
    r = cfg.get_provider(current=None, failed=True)
    # priority最高且启用的是 z-glm5.3（96）
    assert r["provider"] == "z-glm5.3" and r["preserve_thinking"] is False


def test_get_provider_no_fallback_path_returns_flag():
    cfg = _make_llm_config()
    cfg.enable_fallback = False
    cfg.current_provider = "deepseek-pro"
    r = cfg.get_provider(current="deepseek-pro")
    assert r["provider"] == "deepseek-pro" and r["preserve_thinking"] is False


# ==================== ToolEngine 辅助：provider_config 读取 ====================


def test_tool_engine_provider_preserve_thinking_helper():
    """_provider_preserve_thinking 正确读取 provider_config（缺失安全回退False）"""
    te = ToolExecutionEngine.__new__(ToolExecutionEngine)  # 跳过重量级__init__
    # 1. qwen provider 开启保留
    te.llm = SimpleNamespace(provider_config={"provider": "s-qwen3.8", "preserve_thinking": True})
    assert te._provider_preserve_thinking() is True
    # 2. 默认配置（字段缺失 → False）
    te.llm = SimpleNamespace(provider_config={"provider": "z-glm5.3"})
    assert te._provider_preserve_thinking() is False
    # 3. provider_config 尚未初始化（预热前）
    te.llm = SimpleNamespace()
    assert te._provider_preserve_thinking() is False


if __name__ == "__main__":
    fails = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"✅ {_name}")
            except AssertionError as e:
                fails += 1
                print(f"❌ {_name}: {e}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"💥 {_name}: {type(e).__name__}: {e}")
    sys.exit(1 if fails else 0)

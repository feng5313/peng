"""provider临时变量预热回归测试（事务#1699/#1734）

背景：事务重启后新进程首轮迭代 check_progress_and_compact 发生在首次LLM请求之前，
client 的 provider 专属临时变量（compact_threshold等）还是 __init__ 填入的全局默认值，
导致配置了 512000 的 z-glm5.3-flash 事务在 325K 就被全局 320K 误判"上下文紧张"
要求 checklist+restart，无限循环重启。

修复架构：run_stream 首轮迭代前调用 GLMClient.refresh_provider_vars()，按当前有效
provider（get_provider 实时按优先级/启用/冷却判定，而非上一次使用的记录）更新
临时变量，与 chat_stream 连上 provider 后的更新保持同一套字段。
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from peng.llm.client import GLMClient  # noqa: E402
from peng.llm.tool_engine import ToolExecutionEngine  # noqa: E402


FLASH_DATA = {
    "provider": "z-glm5.3-flash",
    "compact_threshold": 512000,
    "thinking_mode": "auto",
    "thinking_type": "thinking",
}


class FakeLLMConfig:
    """模拟 LLMConfig.get_provider：实时返回当前有效 provider 配置"""

    def __init__(self, data):
        self._data = data
        self.calls = []

    def get_provider(self, current=None, **kwargs):
        self.calls.append(current)
        return self._data


@pytest.fixture
def fake_llm_config(monkeypatch):
    """patch client 模块的 load_config，返回可控的 FakeLLMConfig"""
    holder = {}

    def _install(data):
        cfg = FakeLLMConfig(data)
        holder["cfg"] = cfg
        monkeypatch.setattr(
            "peng.llm.client.load_config",
            lambda: types.SimpleNamespace(llm=cfg),
        )
        return cfg

    return _install


def _make_client(provider=None, compact_threshold=320000):
    client = object.__new__(GLMClient)
    client.provider = provider
    client.compact_threshold = compact_threshold
    client.thinking_mode = "auto"
    client.thinking_type = None
    client.provider_config = None
    return client


def test_refresh_updates_vars_from_current_valid_provider(fake_llm_config):
    """预热按当前有效provider更新全部临时变量（而非上一次使用的记录/全局默认值）"""
    fake_llm_config(FLASH_DATA)
    client = _make_client(provider=None, compact_threshold=320000)
    client.refresh_provider_vars()
    assert client.provider == "z-glm5.3-flash"
    assert client.compact_threshold == 512000
    assert client.thinking_type == "thinking"
    assert client.provider_config == FLASH_DATA


def test_refresh_no_available_provider_keeps_old_vars(fake_llm_config):
    """无可用provider时不抛异常、保留原值"""
    fake_llm_config(None)
    client = _make_client(provider=None, compact_threshold=320000)
    client.refresh_provider_vars()
    assert client.provider is None
    assert client.compact_threshold == 320000


def test_refresh_get_provider_called_with_current(fake_llm_config):
    """get_provider 以 current=当前provider 调用（保持粘性，failed逻辑不变）"""
    cfg = fake_llm_config(FLASH_DATA)
    client = _make_client(provider="z-glm5.3-flash")
    client.refresh_provider_vars()
    assert cfg.calls == ["z-glm5.3-flash"]


def _make_engine(llm):
    eng = object.__new__(ToolExecutionEngine)
    eng.llm = llm
    eng.task_manager = types.SimpleNamespace(
        db=types.SimpleNamespace(get_task_info=lambda *a, **k: []),
        get_task=lambda tid: {"content": "", "provider": "z-glm5.3-flash"},
    )
    eng._task_id = 1699
    return eng


def test_effective_threshold_uses_warmed_client_value(fake_llm_config):
    """首轮迭代 _get_effective_compact_threshold 读取预热后的client阈值"""
    fake_llm_config(FLASH_DATA)
    llm = _make_client(provider=None, compact_threshold=320000)
    llm.refresh_provider_vars()  # run_stream 首轮检查前的预热
    eng = _make_engine(llm)
    assert eng._get_effective_compact_threshold() == 512000


def test_effective_threshold_without_warmup_falls_back_global(fake_llm_config):
    """未预热时保持原行为：读client当前值（全局默认320000）"""
    fake_llm_config(FLASH_DATA)
    llm = _make_client(provider=None, compact_threshold=320000)
    eng = _make_engine(llm)
    assert eng._get_effective_compact_threshold() == 320000


# ==================== 事务#2061：上下文紧张检测取值回归测试 ====================


def _make_check_engine(monkeypatch=None, threshold=320000, task_id=2060, progress=90):
    """构造可直接调用 check_progress_and_compact 的引擎

    monkeypatch 隔离 get_output_manager，避免测试写真实事务输出文件。
    llm.config 为动态 property（load_config），_error_dedup_rounds 经
    getattr(config, 'error_dedup_rounds', 5) 读取真实配置默认值5。
    """
    if monkeypatch is not None:
        fake_om = types.SimpleNamespace(
            write_output=lambda **kw: None,
        )
        monkeypatch.setattr(
            "peng.llm.tool_engine.get_output_manager", lambda: fake_om
        )
    eng = object.__new__(ToolExecutionEngine)
    eng.llm = _make_client(provider="z-glm5.3", compact_threshold=threshold)
    eng.task_manager = types.SimpleNamespace(
        db=types.SimpleNamespace(get_task_info=lambda *a, **k: []),
        get_task=lambda tid: {"content": "", "progress": progress},
    )
    eng._task_id = task_id
    eng._recent_system_errors = []
    # 🔧 事务#2061续：上下文紧张节流游标（绕过__init__构造需手动补）
    eng._iteration = 0
    eng._last_context_tight_iter = -1_000_000
    return eng


def _assert_injected(h, before_size: int, expect: bool):
    """断言提示是否注入：add_user默认merge=True，上一条是user时合并（size不变），
    否则新增消息（size+1）。统一按最后一条消息内容判断。"""
    import re

    last = h._messages[-1]["content"]
    if isinstance(last, list):  # 多模态part
        last = "".join(p.get("text", "") for p in last if isinstance(p, dict))
    plain = re.sub(r"\x1b\[[0-9;]*m", "", str(last))
    has = "上下文紧张" in plain
    assert has == expect, (
        f"注入状态={has}(期望{expect})，最后消息: {plain[:80]}"
    )


def test_check_compact_uses_real_api_tokens_over_low_estimate(monkeypatch):
    """事务#2061核心回归：API真实值超阈值而估算偏低时必须触发提示

    实测数据（task#2060）：真实prompt=393829，估算≈288K（中文3.5字符/token
    低估27%，且不含tools schema），阈值320K。修复前纯估算永不触发；
    修复后取max(真实,估算)触发。
    """
    from peng.llm.tool_engine import MessageHistory

    h = MessageHistory()
    for i in range(10):
        h.add_user(f"中文内容{i} " + "测" * 500, merge=False)  # 估算低于阈值
    # 模拟当前provider最近一次成功响应报告的真实值（直接设_prompt_tokens，
    # 与test_task2002同款方式；含tools schema与真实分词）
    h._prompt_tokens = 393829

    eng = _make_check_engine(monkeypatch)
    eng.check_progress_and_compact(320000, h)
    _assert_injected(h, None, True)
    import re

    plain = re.sub(r"\x1b\[[0-9;]*m", "", str(h._messages[-1]["content"]))
    assert "384K" in plain  # 393829//1024=384K
    assert "312K" in plain  # 320000//1024=312K


def test_check_compact_estimate_fallback_when_real_stale(monkeypatch):
    """事务#2002场景保留：provider切换后真实值过期（旧provider低报），
    估算兜底检测——修复前后均应触发"""
    from peng.llm.tool_engine import MessageHistory

    h = MessageHistory()
    # 估算超320K需约1.12M字符（JSON字符/3.5），20条×6万字符
    for i in range(20):
        h.add_user(f"切换兜底{i} " + "算" * 60000, merge=False)  # 估算将超过阈值
    h._prompt_tokens = 100000  # 旧provider低报

    eng = _make_check_engine(monkeypatch)
    eng.check_progress_and_compact(320000, h)
    _assert_injected(h, None, True)


def test_check_compact_no_trigger_below_threshold(monkeypatch):
    """两者均低于阈值时不误报"""
    from peng.llm.tool_engine import MessageHistory

    h = MessageHistory()
    h.add_user("小上下文", merge=False)
    h._prompt_tokens = 5000

    eng = _make_check_engine(monkeypatch)
    eng.check_progress_and_compact(320000, h)
    _assert_injected(h, None, False)


def test_check_compact_first_call_zero_real_uses_estimate(monkeypatch):
    """首次调用（_prompt_tokens=0）纯估算兜底"""
    from peng.llm.tool_engine import MessageHistory

    h = MessageHistory()
    # 估算超320K需约1.12M字符，20条×6万字符
    for i in range(20):
        h.add_user(f"首次调用估算兜底{i} " + "估" * 60000, merge=False)

    eng = _make_check_engine(monkeypatch)
    eng.check_progress_and_compact(320000, h)
    _assert_injected(h, None, True)


def test_check_compact_throttle_no_dedup_deadlock(monkeypatch):
    """🔧 事务#2061续：迭代节流替代报错去重——持续超标时每3轮催促一次，
    绝不永久静默（task#2077事故：_should_report_system_error跳过时不追加
    签名，单一签名永久卡死窗口，14:22首轮注入后560K上下文3小时零提示）"""
    from peng.llm.tool_engine import MessageHistory

    h = MessageHistory()
    h.add_user("初始消息", merge=False)
    h._prompt_tokens = 560000  # 持续超标状态

    eng = _make_check_engine(monkeypatch)
    injected_rounds = []
    for round_no in range(1, 8):  # 模拟7轮迭代
        eng._iteration = round_no
        h._messages = [{"role": "user", "content": "初始消息"}]  # 重置便于断言
        eng.check_progress_and_compact(320000, h)
        last = str(h._messages[-1].get("content", ""))
        injected_rounds.append("上下文紧张" in last)

    # 第1轮必注入；第2、3轮节流跳过；第4轮再注入；第5、6轮跳过；第7轮注入
    assert injected_rounds == [
        True, False, False, True, False, False, True
    ], f"节流模式错误: {injected_rounds}"


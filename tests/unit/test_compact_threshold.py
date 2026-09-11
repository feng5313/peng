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

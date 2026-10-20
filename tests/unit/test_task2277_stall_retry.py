"""事务#2277：SSE流停滞重试策略——同provider重新请求，3分钟窗口占比过半才切换

根因：tool_engine 捕获 StreamStallError 后无条件允许切换provider（注释声称
"立即切换provider"），且停滞检测阈值 timeout_read 配置为240s——provider服务
卡住时要白等240秒才报错，然后还直接切走了provider。

需求：
1. SSE流停滞30秒（timeout_read=30）就重新请求，同provider重试不切换；
2. 3分钟滚动窗口内停滞累计时长占比过半（>=90s）才判定provider不可用，
   切换provider并清空窗口重新计时。

修复：_register_stall_event 登记 (monotonic时间戳, 停滞时长) 事件并按窗口
占比判定；StreamStallError 分支据此设置 is_transient_error（True→chat_stream
failed=False→get_provider"没有出错就不切换"复用当前provider）。
"""

import sys

import pytest

sys.path.insert(0, "src")

import peng.llm.tool_engine as te  # noqa: E402
from peng.llm.tool_engine import ToolExecutionEngine  # noqa: E402


def _bare_engine():
    return object.__new__(ToolExecutionEngine)


@pytest.fixture()
def fake_clock(monkeypatch):
    """可控单调时钟：_register_stall_event 内部调用 time.monotonic"""
    state = {"now": 1000.0}

    def monotonic():
        return state["now"]

    monkeypatch.setattr(te.time, "monotonic", monotonic)
    return state


class TestRegisterStallEvent:
    def test_single_stall_no_switch(self, fake_clock):
        """单次停滞30s：同provider重新请求，不切换"""
        events: list = []
        switched, total = _bare_engine()._register_stall_event(events, 30.0)
        assert switched is False
        assert total == pytest.approx(30.0)
        assert len(events) == 1

    def test_two_stalls_below_threshold_no_switch(self, fake_clock):
        """连续两次停滞(30+30=60s < 90s)：仍不切换provider"""
        events: list = []
        engine = _bare_engine()
        assert engine._register_stall_event(events, 30.0)[0] is False
        fake_clock["now"] += 31.0
        switched, total = engine._register_stall_event(events, 30.0)
        assert switched is False
        assert total == pytest.approx(60.0)

    def test_three_stalls_switch_and_window_reset(self, fake_clock):
        """连续三次停滞(90s>=90s占比过半)：切换provider，且窗口清空重新计时"""
        events: list = []
        engine = _bare_engine()
        assert engine._register_stall_event(events, 30.0)[0] is False
        fake_clock["now"] += 31.0
        assert engine._register_stall_event(events, 30.0)[0] is False
        fake_clock["now"] += 31.0
        switched, total = engine._register_stall_event(events, 30.0)
        assert switched is True, "窗口累计90s应占比过半触发切换"
        assert total == pytest.approx(90.0)
        assert events == [], "切换后窗口必须清空重新计时"
        # 清空后重新累计：两次停滞不再切换
        fake_clock["now"] += 31.0
        assert engine._register_stall_event(events, 30.0)[0] is False
        fake_clock["now"] += 31.0
        assert engine._register_stall_event(events, 30.0)[0] is False

    def test_old_events_slid_out_of_window(self, fake_clock):
        """窗口滑出：180s前的旧停滞被清理，不计入累计（孤立偶发停滞不切换）"""
        events: list = []
        engine = _bare_engine()
        engine._register_stall_event(events, 30.0)
        fake_clock["now"] += 200.0  # 第1次事件滑出3分钟窗口
        switched, total = engine._register_stall_event(events, 30.0)
        assert switched is False
        assert total == pytest.approx(30.0), "窗口外旧事件不应计入"
        assert len(events) == 1

    def test_isolated_stalls_never_switch(self, fake_clock):
        """偶发停滞间隔>90s：窗口内始终只有1~2次，永不切换（核心需求场景）"""
        events: list = []
        engine = _bare_engine()
        for _ in range(10):
            switched, _ = engine._register_stall_event(events, 30.0)
            assert switched is False, "偶发停滞不应切换provider"
            fake_clock["now"] += 95.0  # 上一次事件即将滑出窗口

    def test_none_timeout_treated_as_zero(self, fake_clock):
        """timeout缺失防御：按0s计，不误触发切换"""
        events: list = []
        switched, total = _bare_engine()._register_stall_event(events, None)
        assert switched is False
        assert total == 0.0

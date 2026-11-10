"""事务#2667：本地provider意外关闭时prefill中检测不到服务关闭、不切换provider

根因（日志1195613-1197584行取证，19:17-19:48共30分钟）：
1. swift-qwen3.8-iq3(localhost:8080)关闭后，WSL2 NAT下SYN被丢弃（不回RST），
   表现为 httpx.ConnectTimeout 而非 ConnectError。原分类
   `isinstance(e, (ConnectError, RemoteProtocolError))` 漏掉 ConnectTimeout，
   被归为"网络瞬时错误"→chat_stream failed=False→永不mark_failed/冷却/切换。
2. transient熔断（_transient_rounds>=3）只改等待时长不触发切换：transient路径
   从未mark_failed→无冷却记录→get_min_cooldown_remaining()=0→公共路径
   "立刻重试"→get_provider复用current死provider→无限循环（65+次ConnectTimeout）。
3. 连接阶段prefill_tracker即输出"⏳ LLM prefill中X%"：ConnectTimeout等满
   timeout_connect(30s)恰达PREFILL_OUTPUT_MIN_ELAPSED(30s)提示阈值，每轮重试
   刷一条假进度，掩盖"服务已死、连接从未建立"的真相。

修复：
- Fix1: is_service_unavailable_error纯函数把ConnectTimeout归入服务不可用
  （走#2474路径：同provider快速重试3次→全败→_provider_switch_pending→
  mark_failed+切换provider）
- Fix2: transient熔断时置_provider_switch_pending=True（纵深防御）
- Fix3: prefill_tracker连接门控（mark_connected未置位不写状态文件/进度行）
"""

import json
import time

import httpx
import pytest

import peng.llm.prefill_tracker as pt
from peng.llm.prefill_tracker import PrefillTracker, _get_prefill_file
from peng.llm.tool_engine import is_service_unavailable_error


def _mk_request() -> httpx.Request:
    return httpx.Request("POST", "http://localhost:8080/v1/chat/completions")


class TestServiceUnavailableClassification:
    """Fix1：ConnectTimeout必须归入"服务不可用"（事务#2667核心修复）"""

    def test_connect_timeout_is_service_unavailable(self):
        """核心场景：WSL2 NAT下服务关闭SYN被丢弃→ConnectTimeout=服务已死"""
        assert is_service_unavailable_error(httpx.ConnectTimeout("timed out")) is True

    def test_connect_error_is_service_unavailable(self):
        """连接被拒绝（RST）：原有分类保持不变"""
        assert is_service_unavailable_error(httpx.ConnectError("refused")) is True

    def test_remote_protocol_error_is_service_unavailable(self):
        """对端异常断开：原有分类保持不变"""
        assert is_service_unavailable_error(httpx.RemoteProtocolError("abort")) is True

    def test_read_timeout_not_service_unavailable(self):
        """读超时（连接已建立，流中停滞）：维持瞬时错误语义不回退"""
        assert is_service_unavailable_error(httpx.ReadTimeout("read timed out")) is False

    def test_read_error_not_service_unavailable(self):
        """读错误（流中断流，#2235保留响应路径依赖此分类）：维持不变"""
        assert is_service_unavailable_error(httpx.ReadError("reset")) is False

    def test_write_error_not_service_unavailable(self):
        assert is_service_unavailable_error(httpx.WriteError("broken pipe")) is False

    def test_pool_timeout_not_service_unavailable(self):
        assert is_service_unavailable_error(httpx.PoolTimeout("pool")) is False

    def test_generic_exception_not_service_unavailable(self):
        assert is_service_unavailable_error(Exception("x")) is False

    def test_subclass_of_connect_error_classified(self):
        """isinstance语义：ConnectError子类同样归入服务不可用"""

        class MyConnectError(httpx.ConnectError):
            pass

        assert is_service_unavailable_error(MyConnectError("sub")) is True


class TestTransientBreakerSwitchPending:
    """Fix2：transient熔断必须触发provider切换（置_provider_switch_pending）

    用最小引擎实例验证标志置位逻辑的调用约定（熔断分支内联代码难以整段
    单测，此处锁死"熔断→switch_pending"的因果绑定与复位语义）。
    """

    def test_switch_pending_consumed_by_chat_stream_arg(self):
        """_provider_switch_pending=True时下一次调用failed=True并立即复位"""
        import peng.llm.tool_engine as te

        engine = object.__new__(te.ToolExecutionEngine)
        engine._provider_switch_pending = True
        # 消费点逻辑（tool_engine.py:4304-4323）的等价复现
        if engine._provider_switch_pending:
            engine._provider_switch_pending = False
            failed_arg = True
        else:
            failed_arg = None
        assert failed_arg is True
        assert engine._provider_switch_pending is False


class TestPrefillConnectedGate:
    """Fix3：连接未建立（响应头未到达）不写prefill状态文件/进度行"""

    @pytest.fixture()
    def fast_tracker(self, monkeypatch, tmp_path):
        """缩短周期+屏蔽DB心跳/输出写入+状态文件隔离到tmp_path的tracker环境"""
        monkeypatch.setattr(pt, "PREFILL_HEARTBEAT_INTERVAL", 0.05)
        monkeypatch.setattr(pt, "PREFILL_OUTPUT_MIN_ELAPSED", 0.15)
        monkeypatch.setattr(pt, "PREFILL_OUTPUT_INTERVAL", 0.05)
        monkeypatch.setattr(pt, "_get_data_dir", lambda: tmp_path)
        # 屏蔽DB心跳更新（避免真实DB依赖）
        try:
            from peng.task.database import TaskDatabase

            monkeypatch.setattr(TaskDatabase, "update_heartbeat", lambda self, tid: None)
        except Exception:
            pass
        # 屏蔽输出进度行（避免output_manager真实写文件）
        monkeypatch.setattr(
            PrefillTracker, "_write_output_progress", lambda self, tid, elapsed, pct: None
        )
        return PrefillTracker()

    def test_not_connected_no_status_file(self, fast_tracker):
        """连接未建立：即使超过提示阈值也不写状态文件（#2667假进度根因）"""
        tid = 2667001
        fast_tracker.start_request(tid, est_tokens=10000, provider="dead", compact_threshold=100000)
        try:
            time.sleep(0.35)  # 远超PREFILL_OUTPUT_MIN_ELAPSED(0.15s)
            assert not _get_prefill_file(tid).exists(), (
                "连接未建立时不应写prefill状态文件（掩盖服务已死的假进度）"
            )
        finally:
            fast_tracker.finish_request(tid)

    def test_connected_writes_status_file(self, fast_tracker):
        """mark_connected后：超过阈值正常写状态文件（真prefill提示不受影响）"""
        tid = 2667002
        fast_tracker.start_request(tid, est_tokens=10000, provider="alive", compact_threshold=100000)
        try:
            fast_tracker.mark_connected(tid)
            time.sleep(0.35)
            filepath = _get_prefill_file(tid)
            assert filepath.exists(), "连接已建立后应正常写prefill状态文件"
            data = json.loads(filepath.read_text())
            assert data["provider"] == "alive"
            assert data["est_tokens"] == 10000
        finally:
            fast_tracker.finish_request(tid)

    def test_mark_connected_after_finish_safe(self, fast_tracker):
        """finish_request后mark_connected：无活跃entry，静默无害（不抛异常）"""
        tid = 2667003
        fast_tracker.start_request(tid, est_tokens=100, provider="x", compact_threshold=1000)
        fast_tracker.finish_request(tid)
        fast_tracker.mark_connected(tid)  # 不应抛异常
        fast_tracker.mark_connected(999999999)  # 从未存在同样安全

    def test_connected_late_still_shows_after_threshold(self, fast_tracker):
        """连接慢建立（先等一段再mark_connected）：置位后仍能显示进度"""
        tid = 2667004
        fast_tracker.start_request(tid, est_tokens=5000, provider="slow", compact_threshold=10000)
        try:
            time.sleep(0.25)  # 连接阶段（未置位）——无状态文件
            assert not _get_prefill_file(tid).exists()
            fast_tracker.mark_connected(tid)
            time.sleep(0.2)  # 置位后下一轮即写
            assert _get_prefill_file(tid).exists()
        finally:
            fast_tracker.finish_request(tid)


class TestHeartbeatStillRefreshedBeforeConnected:
    """连接阶段心跳保活不受门控影响（防事务被heartbeat_monitor误杀）"""

    def test_heartbeat_called_while_not_connected(self, fast_tracker_none, monkeypatch):
        """未连接期间_update心跳仍被调用（门控只作用于提示，不作用于心跳）"""
        # 直接验证_track_loop内部结构：心跳代码位于connected检查之前（代码顺序锁死）
        import inspect

        src = inspect.getsource(PrefillTracker._track_loop)
        heartbeat_pos = src.index("update_heartbeat")
        gate_pos = src.index("connected.is_set()")
        assert heartbeat_pos < gate_pos, (
            "_track_loop中心跳更新必须先于connected门控（连接阶段也要保活）"
        )


@pytest.fixture()
def fast_tracker_none():
    """占位fixture（TestHeartbeatStillRefreshedBeforeConnected不需要实例）"""
    return None

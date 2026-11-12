"""跨进程LLM限流闸门回归测试（事务#2689，消息#12863）

需求现场：glm 系列 provider 明明没有到额度限制，几个事务卡死无输出。
实测取证：智谱返回 HTTP 429 {"code":"1302","message":"您的账户已达到速率限制，
请您控制请求频率"}，响应头无 retry-after/x-ratelimit（独立curl单独一次请求也429）
——账户级**速率限制**而非配额耗尽。

旧实现的活锁自锁：每个事务（独立 runner 子进程）在429临时限流路径各自每0.5秒
"抢流量"重试同一账户，被拒请求同样消耗账户RPM窗口 ⇒ 每分钟数百请求全落在注定
被拒的试探上，谁都无法拿到成功响应；且该路径只写 logger.warning、不写事务输出
⇒ UI 毫无动静（风主视角的"卡死无输出"）。

覆盖：
- is_rate_limit_message：速率限制关键词判定（中文/英文），英文"Rate limit exceeded"
  不再被配额关键词("exceeded"/"limit reached")误判为额度耗尽
- rate_domain_key：同api_key+base_url归组为同一限流域（同账户共享RPM池）
- record_throttle：指数退避窗口/间隔增长、服务端声明等待优先、封顶、next_slot不早于窗口
- claim_send_slot：多事务串行排队（不再并发抢流量）、未限流时不排队、窗口内不放行
- record_success：被接受即清零退避状态恢复全速
- escalate：累计节流时长达阈值 → 调用方升级硬阻塞
- mark_domain_blocked：同域provider一并硬阻塞（切换到同账户的flash必然再次429）
- 跨进程共享：多子进程并发领取槽位，验证排队间隔真实生效（旧实现在此处活锁）
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from peng.config.loader import (  # noqa: E402
    AllProvidersBlockedError,
    LLMConfig,
    ProviderConfig,
)
from peng.llm.rate_gate import (  # noqa: E402
    RATE_GATE_BASE_INTERVAL_SECONDS,
    RATE_GATE_BASE_WINDOW_SECONDS,
    RATE_GATE_ESCALATE_AFTER_SECONDS,
    RATE_GATE_MAX_INTERVAL_SECONDS,
    RATE_GATE_MAX_WINDOW_SECONDS,
    RATE_GATE_STALE_SECONDS,
    RateGate,
    rate_domain_key,
)
from peng.llm.tool_engine import is_rate_limit_message  # noqa: E402

# ============ 429分类：速率限制 vs 配额耗尽（事务#2689根因之一） ============


class TestRateLimitClassification:
    """速率限制必须优先于配额关键词判定，否则明明没额度却被全局硬阻塞"""

    def test_zhipu_real_429_message(self):
        """现场原文：智谱 code 1302 账户速率限制"""
        msg = '{"error":{"code":"1302","message":"您的账户已达到速率限制，请您控制请求频率"}}'
        assert is_rate_limit_message(msg), "智谱速率限制文案必须识别为限流"

    def test_english_rate_limit_exceeded_not_quota(self):
        """旧实现缺陷：'exceeded'/'limit reached'命中配额关键词 → 误判额度耗尽"""
        msg = "Rate limit exceeded: too many requests per minute"
        assert is_rate_limit_message(msg)
        # tool_engine 的判定语义复算（not rate_limit and any(quota_kw)）
        quota_keywords = ["exceeded", "limit reached", "quota", "exhausted", "额度", "上限"]
        low = msg.lower()
        assert any(k in low for k in quota_keywords), "旧实现会命中配额关键词"
        assert not (not is_rate_limit_message(msg) and any(k in low for k in quota_keywords))

    def test_true_quota_exhaustion_still_quota(self):
        """真配额耗尽（带恢复时刻）不得被判成短期限流"""
        msg = "已达到 5 小时的使用上限。您的限额将在 2026-11-10 18:31:28 重置"
        assert not is_rate_limit_message(msg)

    def test_openai_insufficient_quota(self):
        msg = "You exceeded your current quota, please check your plan and billing details"
        assert not is_rate_limit_message(msg)

    def test_empty_message_safe(self):
        assert not is_rate_limit_message("")
        assert not is_rate_limit_message(None)  # type: ignore[arg-type]


# ============ 限流域归组：同账户共享RPM池 ============


class TestRateDomainKey:
    def test_same_account_same_domain(self):
        """z系 glm-5.3 与 glm-5.3-flash 共用同一api_key ⇒ 必须共享一个闸门"""
        a = rate_domain_key({"base_url": "https://open.bigmodel.cn/api/coding/paas/v4", "api_key": "K"})
        b = rate_domain_key(
            {"base_url": "https://open.bigmodel.cn/api/coding/paas/v4", "api_key": "K"}
        )
        assert a == b and a

    def test_different_account_different_domain(self):
        a = rate_domain_key({"base_url": "http://a", "api_key": "k1"})
        b = rate_domain_key({"base_url": "http://a", "api_key": "k2"})
        c = rate_domain_key({"base_url": "http://b", "api_key": "k1"})
        assert len({a, b, c}) == 3

    def test_missing_credentials_falls_back_to_provider(self):
        assert rate_domain_key({"provider": "z-glm5.3"})
        assert rate_domain_key({"provider": "other"}) != rate_domain_key({"provider": "z-glm5.3"})
        assert rate_domain_key(None) == ""
        assert rate_domain_key({}) == ""


# ============ 闸门退避状态机 ============


@pytest.fixture()
def gate(tmp_path: Path) -> RateGate:
    return RateGate(data_dir=tmp_path)


class TestRecordThrottle:
    def test_first_throttle_sets_window_and_interval(self, gate: RateGate):
        st = gate.record_throttle("d1", reason="429速率限制", now=1000.0)
        assert st["count"] == 1
        assert st["until"] == pytest.approx(1000.0 + RATE_GATE_BASE_WINDOW_SECONDS, abs=1)
        assert st["interval"] == pytest.approx(RATE_GATE_BASE_INTERVAL_SECONDS, abs=1)
        # 窗口内不得允许任何请求穿过（旧实现0.5秒抢流量正因此被拒）
        assert st["until"] > 1000.0

    def test_exponential_backoff_growth_and_cap(self, gate: RateGate):
        now = 1000.0
        wins = []
        ints = []
        t = now
        for _ in range(8):
            st = gate.record_throttle("d1", declared_wait=None, now=t)
            wins.append(st["until"] - t)
            ints.append(st["interval"])
            t = st["until"]
        assert wins[0] < wins[1] < wins[2]
        assert wins[-1] <= RATE_GATE_MAX_WINDOW_SECONDS
        assert ints[-1] <= RATE_GATE_MAX_INTERVAL_SECONDS

    def test_declared_wait_authoritative(self, gate: RateGate):
        st = gate.record_throttle("d1", declared_wait=45.0, now=1000.0)
        assert st["until"] == pytest.approx(1045.0, abs=1)
        assert st["wait"] == pytest.approx(45.0, abs=1)

    def test_escalate_after_cumulative_duration(self, gate: RateGate):
        now = 1000.0
        st = gate.record_throttle("d1", now=now)
        assert st["escalate"] is False
        st = gate.record_throttle("d1", now=now + RATE_GATE_ESCALATE_AFTER_SECONDS)
        assert st["escalate"] is True, "累计节流时长达阈值必须升级（不再无限死磕）"
        assert st["throttled_total"] >= RATE_GATE_ESCALATE_AFTER_SECONDS

    def test_success_clears_backoff(self, gate: RateGate):
        gate.record_throttle("d1", now=1000.0)
        gate.record_throttle("d1", now=1003.0)
        assert gate.throttle_state("d1", now=1004.0)["count"] == 2
        gate.record_success("d1", now=1005.0)
        st = gate.throttle_state("d1", now=1006.0)
        assert st["count"] == 0 and st["wait"] == 0.0 and st["interval"] == 0.0
        # 恢复全速：下一次领取立即放行
        slot, wait = gate.claim_send_slot("d1", now=1007.0)
        assert wait == 0.0

    def test_stale_state_self_heals(self, gate: RateGate):
        gate.record_throttle("d1", now=1000.0)
        st = gate.throttle_state("d1", now=1000.0 + RATE_GATE_STALE_SECONDS + 1)
        assert st["count"] == 0 and st["until"] == 0.0, "陈旧节流状态必须自愈失效"


class TestClaimSendSlot:
    def test_not_throttled_does_not_queue(self, gate: RateGate):
        """未限流时不排队（并发仍由 llm/concurrency.py 槽位约束）"""
        for _ in range(5):
            _slot, wait = gate.claim_send_slot("d1", now=1000.0)
            assert wait == 0.0

    def test_serial_queue_within_window(self, gate: RateGate):
        """限流期间多事务被排成单列按间隔逐个试探——旧实现各自并发抢流量"""
        gate.record_throttle("d1", now=1000.0)  # interval=1s, window至1003
        slots = []
        for _ in range(4):
            slot, wait = gate.claim_send_slot("d1", now=1002.0)
            slots.append(slot)
        assert slots == sorted(slots)
        # 首个槽位在窗口边界(1003)，之后按间隔1s串行
        assert slots[0] == pytest.approx(1003.0, abs=1)
        assert slots[1] - slots[0] >= 0.9
        assert slots[3] - slots[0] >= 2.9

    def test_snapshot_lists_domains(self, gate: RateGate):
        gate.record_throttle("dA", now=1000.0)
        gate.record_throttle("dB", now=1000.0)
        snap = gate.snapshot(now=1001.0)
        assert set(snap) == {"dA", "dB"}
        assert snap["dA"]["count"] == 1 and snap["dB"]["count"] == 1

    def test_state_file_is_valid_json(self, gate: RateGate, tmp_path: Path):
        gate.record_throttle("d1", reason="429速率限制", now=1000.0)
        data = json.loads((tmp_path / "rate_gate.json").read_text(encoding="utf-8"))
        assert data["domains"]["d1"]["reason"] == "429速率限制"


# ============ 跨进程共享（多子进程，旧实现在此处活锁） ============

_CHILD = """
import os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(os.environ['ROOT']) / 'src'))
from peng.llm.rate_gate import RateGate
gate = RateGate(data_dir=Path(os.environ['DIR']))
now = float(os.environ['NOW'])
gate.record_throttle(os.environ['DOMAIN'], now=now)
slot, wait = gate.claim_send_slot(os.environ['DOMAIN'], now=now + 0.5)
print(f'{slot:.3f} {wait:.3f}')
"""


class TestCrossProcessGate:
    def test_processes_are_serialized(self, tmp_path: Path):
        """三个进程同域领取发送槽位：必须按间隔串行（旧实现各自0.5秒并发撞墙）"""
        now = time.time()
        procs = []
        for _ in range(3):
            env = dict(os.environ)
            env.update(
                {
                    "ROOT": str(Path(__file__).resolve().parents[2]),
                    "DIR": str(tmp_path),
                    "DOMAIN": "shared-account",
                    "NOW": f"{now:.3f}",
                }
            )
            procs.append(
                subprocess.Popen(
                    [sys.executable, "-c", _CHILD], env=env, stdout=subprocess.PIPE, text=True
                )
            )
        out = []
        for p in procs:
            stdout, _stderr = p.communicate(timeout=30)
            out.append(stdout.strip())
        assert all(out), f"子进程输出缺失: {out}"
        slots = [float(line.split()[0]) for line in out]
        assert max(slots) - min(slots) >= RATE_GATE_BASE_INTERVAL_SECONDS * 2, (
            f"跨进程未串行排队（间隔不足）: {slots}"
        )
        # 同进程多次领取也必须推进队列（不共享私有状态）
        g = RateGate(data_dir=tmp_path)
        s1, _ = g.claim_send_slot("shared-account", now=now + 0.5)
        s2, _ = g.claim_send_slot("shared-account", now=now + 0.5)
        assert s2 > s1

    def test_async_acquire_waits_to_slot(self, tmp_path: Path):
        """chat_stream发请求前排队：等待到槽位时刻，且能被控制信号打断"""
        import asyncio

        g = RateGate(data_dir=tmp_path)
        # 用真实时钟注入节流窗口（acquire按真实时间排队）：3秒静默窗口
        g.record_throttle("d1", now=time.time())

        async def _run():
            loop = asyncio.get_event_loop()
            start = loop.time()
            waited = await g.acquire("d1", task_id=99, notice_cb=None)
            return loop.time() - start, waited

        elapsed, waited = asyncio.run(_run())
        assert waited > 0.0, "限流期间必须排队等待而非立即撞墙"
        assert elapsed >= 1.0, f"未真正等待到槽位（只等了{elapsed:.2f}s）"

    def test_acquire_interruptible(self, tmp_path: Path):
        """等待期间检测到控制信号/终态立刻放弃等待（紧急info/cancel可打断）"""
        import asyncio

        g = RateGate(data_dir=tmp_path)
        g.record_throttle("d1", now=time.time())

        async def _run():
            start = asyncio.get_event_loop().time()
            await g.acquire("d1", task_id=99, interrupt_cb=lambda: True)
            return asyncio.get_event_loop().time() - start

        elapsed = asyncio.run(_run())
        assert elapsed < 2.0, f"中断未生效，等待了{elapsed:.1f}s"


# ============ 同域provider一并硬阻塞（切换到同账户flash必然再次429） ============


def _make_cfg(tmp_path: Path) -> LLMConfig:
    cfg = LLMConfig()
    cfg.providers = {
        "z-glm5.3": ProviderConfig(
            api_key="k1", base_url="https://open.bigmodel.cn/x", model="glm", enable=True, priority=98
        ),
        "z-glm5.3-flash": ProviderConfig(
            api_key="k1", base_url="https://open.bigmodel.cn/x", model="flash", enable=True, priority=96
        ),
        "swift-qwen3.8": ProviderConfig(
            api_key="k2", base_url="http://localhost:8080/v1", model="qwen", enable=True, priority=90
        ),
    }
    cfg._failed_providers_path = tmp_path / "failed_providers.json"
    return cfg


# ============ chat_stream端到端：限流期间真实排队（旧实现立即撞墙） ============


class _FakeResp:
    """假429响应（流式接口：__enter__/status_code/read/request）"""

    def __init__(self, status_code: int, body: bytes, request: object) -> None:
        self.status_code = status_code
        self._body = body
        self.request = request
        self.headers = {}

    def read(self) -> bytes:
        return self._body

    def iter_lines(self):  # pragma: no cover - 429路径不会走到
        yield b""


class _FakeCM:
    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp

    def __enter__(self) -> _FakeResp:
        return self._resp

    def __exit__(self, *exc) -> bool:
        return False


class _FakeTaskMgr:
    def get_task(self, task_id=None):
        return {"content": "", "info": "[]"}

    def update_heartbeat(self, task_id):
        pass


class _FakeMsgMgr:
    def get_task_manager(self):
        return _FakeTaskMgr()


class TestChatStreamGateIntegration:
    """限流时第二次请求必须等待到闸门槽位，而不是立即再撞墙（旧实现活锁）"""

    def _make_client(self, monkeypatch, tmp_path: Path, attempts: list):
        import httpx

        from peng.llm import rate_gate as rg_mod
        from peng.llm.client import GLMClient

        cfg = LLMConfig()
        cfg.providers = {
            "z-glm5.3": ProviderConfig(
                api_key="k1", base_url="https://api.test", model="glm", enable=True, priority=98
            ),
            "other-acct": ProviderConfig(
                api_key="k2", base_url="http://localhost:1/v1", model="q", enable=True, priority=50
            ),
        }
        cfg.max_concurrent = 0  # 关闭并发槽位，避免测试写真实槽位文件
        cfg._failed_providers_path = tmp_path / "failed_providers.json"
        monkeypatch.setattr(GLMClient, "config", property(lambda self: cfg))

        gate = RateGate(data_dir=tmp_path)
        monkeypatch.setattr(rg_mod, "get_rate_gate", lambda: gate)

        def fake_stream(self, method, url, **kwargs):
            attempts.append(time.time())
            return _FakeCM(
                _FakeResp(
                    429,
                    '{"error":{"code":"1302","message":"您的账户已达到速率限制"}}'.encode(),
                    object(),
                )
            )

        monkeypatch.setattr(httpx.Client, "stream", fake_stream)
        return GLMClient(), gate

    def test_second_request_waits_for_slot(self, monkeypatch, tmp_path: Path):
        import httpx


        attempts: list = []
        client, gate = self._make_client(monkeypatch, tmp_path, attempts)

        async def _call():
            stream = client.chat_stream(
                messages=[{"role": "user", "content": "hi"}],
                message_manager=_FakeMsgMgr(),
                task_id=None,
            )
            async for _chunk in stream:
                pass  # 429在响应头阶段抛出，无chunk

        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(_call())  # 第一次：立即请求 → 被限流（旧实现现场）
        assert len(attempts) == 1
        assert client.provider == "z-glm5.3"

        st = gate.record_throttle(rate_domain_key(client.provider_config), reason="速率限制")
        assert st["count"] == 1 and st["interval"] >= RATE_GATE_BASE_INTERVAL_SECONDS

        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(_call())  # 第二次：必须排队等待闸门槽位，不再立即撞墙
        assert len(attempts) == 2
        gap = attempts[1] - attempts[0]
        assert gap >= RATE_GATE_BASE_INTERVAL_SECONDS * 0.8, (
            f"限流期间未排队（两次请求间隔仅{gap:.2f}s，旧实现0.5秒高频抢流量）"
        )

    def test_success_clears_gate_and_restores_full_speed(self, monkeypatch, tmp_path: Path):
        """响应被接受 ⇒ 闸门清零，后续请求不再排队（避免限流恢复后仍被拖慢）"""
        import httpx


        attempts: list = []
        client, gate = self._make_client(monkeypatch, tmp_path, attempts)
        domain = rate_domain_key({"base_url": "https://api.test", "api_key": "k1"})
        gate.record_throttle(domain, now=time.time() - 10)  # 窗口已过但退避计数在

        # 200流式响应（手工喂一行SSE后结束）
        def fake_stream(self, method, url, **kwargs):
            attempts.append(time.time())
            resp = _FakeResp(200, b"", object())
            resp.iter_lines = lambda: iter(
                ['data: {"choices":[{"delta":{"content":"ok"}}]}', "data: [DONE]"]
            )
            return _FakeCM(resp)

        monkeypatch.setattr(httpx.Client, "stream", fake_stream)

        async def _call():
            chunks = []
            stream = client.chat_stream(
                messages=[{"role": "user", "content": "hi"}],
                message_manager=_FakeMsgMgr(),
                task_id=None,
            )
            async for chunk in stream:
                chunks.append(chunk)
            return chunks

        chunks = asyncio.run(_call())
        assert chunks, "正常流应返回chunk"
        st = gate.throttle_state(domain)
        assert st["count"] == 0, "响应被接受后必须清零退避状态"
        t0 = time.time()
        asyncio.run(_call())
        assert time.time() - t0 < 1.0, "清零后应恢复全速（不再排队）"

    def test_rate_notice_written_to_task_output(self, monkeypatch, tmp_path: Path):
        """限流排队提示写入事务输出（修复"卡死无输出"观感）"""
        from peng.llm.client import GLMClient

        written: list = []

        class _FakeOut:
            def write_output(self, task_id=None, source=None, content=None):
                written.append((task_id, source, content))

        monkeypatch.setattr(
            "peng.task.output_manager.get_output_manager", lambda: _FakeOut()
        )
        client = GLMClient()
        client._notify_rate_wait(77, 12.0, "您的账户已达到速率限制")
        assert written and written[0][0] == 77
        assert "速率限制" in written[0][2] and written[0][1] == "system"


class TestDomainBlocked:
    def test_same_domain_all_marked(self, tmp_path: Path):
        cfg = _make_cfg(tmp_path)
        until = time.time() + 60
        marked = cfg.mark_domain_blocked("z-glm5.3", until, "速率限制持续180s")
        assert set(marked) == {"z-glm5.3", "z-glm5.3-flash"}, "同账户provider必须一并阻塞"
        assert "swift-qwen3.8" not in marked, "不同账户不受影响"
        assert cfg.blocked_remaining("z-glm5.3-flash") > 0

    def test_selection_skips_whole_domain(self, tmp_path: Path):
        """整域阻塞后选择器只能选其他账户provider（避免切到flash再次429）"""
        cfg = _make_cfg(tmp_path)
        cfg.mark_domain_blocked("z-glm5.3", time.time() + 60, "速率限制")
        pd = cfg.get_provider(current="z-glm5.3", failed=True, task_id=1)
        assert pd["provider"] == "swift-qwen3.8"

    def test_all_domains_blocked_raises(self, tmp_path: Path):
        cfg = _make_cfg(tmp_path)
        cfg.mark_domain_blocked("z-glm5.3", time.time() + 60, "速率限制")
        cfg.mark_domain_blocked("swift-qwen3.8", time.time() + 60, "速率限制")
        with pytest.raises(AllProvidersBlockedError):
            cfg.get_provider(current="z-glm5.3", failed=True, task_id=1)

    def test_persisted_across_processes(self, tmp_path: Path):
        """域阻塞落盘：新进程（restart/resume的事务）继承，不重复撞墙"""
        cfg = _make_cfg(tmp_path)
        until = time.time() + 60
        cfg.mark_domain_blocked("z-glm5.3", until, "速率限制持续180s")
        fresh = _make_cfg(tmp_path)
        assert fresh.blocked_remaining("z-glm5.3-flash") > 0

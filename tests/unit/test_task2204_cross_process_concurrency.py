#!/usr/bin/env python3
"""事务#2204：LLM max_concurrent 跨进程并发限制测试

背景（根因）：
``llm.max_concurrent`` / ``llm.providers.<name>.max_concurrent`` 此前用**进程内**
``asyncio.Semaphore`` 实现（client.py）。但每个事务都在独立子进程中执行
（TaskManager._execute_task_process → subprocess.Popen(runner.py) → PengAgent()
→ GLMClient()），信号量进程私有：跨事务完全不共享，且单进程内 LLM 请求本就
串行 ⇒ 该配置对真实 provider 并发数毫无约束（用户实测抱怨"毫无效果"）。

修复：``peng/llm/concurrency.py`` 用"文件锁槽位"实现**跨进程**信号量
（filelock → flock/msvcrt，锁随进程退出由内核自动释放）。

本测试锁死以下语义：
1. 单进程内多协程并发受 limit 约束
2. 跨进程（真实子进程）并发受 limit 约束，且不限并发时确实并发
3. 多约束组合为"全有或全无"（全局 + provider 同时满足才放行）
4. 退出上下文立即释放槽位，可立刻重新获取
5. 空约束（不限并发）直接放行
6. 等待中的任务被取消不泄漏槽位
7. client 并发约束构造：全局大/小模型 + provider 级组合
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from peng.config.loader import LLMConfig, ProviderConfig  # noqa: E402
from peng.llm.client import GLMClient  # noqa: E402
from peng.llm.concurrency import (  # noqa: E402
    acquire_llm_slots,
    acquire_llm_slots_handle,
    normalize_limits,
    sanitize_key,
    slot_file,
    slots_root,
)


@pytest.fixture
def slots_dir(tmp_path: Path) -> Path:
    """每个测试独立的槽位目录（避免真实 data/llm_slots 干扰）"""
    d = tmp_path / "llm_slots"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ==================== 1. 基础工具函数 ====================


class TestHelpers:
    def test_normalize_limits(self):
        assert normalize_limits(None) == []
        assert normalize_limits([]) == []
        assert normalize_limits([("a", 3)]) == [("a", 3)]
        # None / 0 / 负数 = 不限并发，被过滤
        assert normalize_limits([("a", None), ("b", 0), ("c", -2), ("d", 1)]) == [("d", 1)]
        # 空 key 过滤
        assert normalize_limits([("", 3)]) == []

    def test_sanitize_key_safe_and_distinct(self):
        safe = sanitize_key("provider:swift-qwen3.8-iq3")
        assert all(c.isalnum() or c in "_.-" for c in safe), safe
        # 不同原始 key 规范化后必须不同（否则不同 provider 会共享槽位）
        assert sanitize_key("a/b") != sanitize_key("a_b")
        assert sanitize_key("provider:x") != sanitize_key("provider:y")
        # 长 key 截断后仍不碰撞
        assert sanitize_key("x" * 200) != sanitize_key("x" * 199 + "y")
        # 可重现（同一 key 每次相同）
        assert sanitize_key("k1") == sanitize_key("k1")

    def test_slot_file_paths(self, slots_dir: Path):
        p0 = slot_file("provider:p", 0, slots_dir)
        p1 = slot_file("provider:p", 1, slots_dir)
        assert p0 != p1 and p0.parent == slots_dir
        # 默认目录在项目 data/llm_slots 下（跨进程共享的唯一事实源）
        assert slots_root().name == "llm_slots"


# ==================== 2. 单进程内并发受约束 ====================


class TestSingleProcess:
    @pytest.mark.asyncio
    async def test_limit_serializes_coroutines(self, slots_dir: Path):
        """limit=1：4 个协程必须串行（最大并发=1）"""
        hold = 0.1
        state = {"cur": 0, "max": 0}

        async def worker():
            async with acquire_llm_slots([("k", 1)], slots_dir=slots_dir):
                state["cur"] += 1
                state["max"] = max(state["max"], state["cur"])
                await asyncio.sleep(hold)
                state["cur"] -= 1

        t0 = time.monotonic()
        await asyncio.gather(*[worker() for _ in range(4)])
        elapsed = time.monotonic() - t0

        assert state["max"] == 1, f"单进程内并发被突破: max={state['max']}"
        # 串行 4 次，至少 3*hold（留出轮询抖动余量）
        assert elapsed >= hold * 3, f"未串行化: elapsed={elapsed:.2f}"

    @pytest.mark.asyncio
    async def test_limit_two_allows_two(self, slots_dir: Path):
        """limit=2：4 个协程最多 2 并发（两波完成）"""
        hold = 0.15
        state = {"cur": 0, "max": 0}

        async def worker():
            async with acquire_llm_slots([("k2", 2)], slots_dir=slots_dir):
                state["cur"] += 1
                state["max"] = max(state["max"], state["cur"])
                await asyncio.sleep(hold)
                state["cur"] -= 1

        t0 = time.monotonic()
        await asyncio.gather(*[worker() for _ in range(4)])
        elapsed = time.monotonic() - t0

        assert state["max"] == 2, f"并发上限错误: max={state['max']}"
        assert elapsed >= hold * 1.5, f"未分批: elapsed={elapsed:.2f}"

    @pytest.mark.asyncio
    async def test_all_or_nothing_multi_key(self, slots_dir: Path):
        """多约束全有或全无：keyA 被占满时，(keyA,keyB) 必须等待"""
        hold = 0.3
        holder_in = asyncio.Event()

        async def holder():
            async with acquire_llm_slots([("keyA", 1)], slots_dir=slots_dir):
                holder_in.set()
                await asyncio.sleep(hold)

        async def combo():
            async with acquire_llm_slots([("keyA", 1), ("keyB", 1)], slots_dir=slots_dir):
                return time.monotonic()

        t0 = time.monotonic()
        holder_task = asyncio.create_task(holder())
        await holder_in.wait()
        got = await asyncio.gather(combo(), holder_task)
        waited = got[0] - t0

        assert waited >= hold * 0.8, f"未等待被占用的 keyA: waited={waited:.2f}"
        # keyB 未被泄漏占用：组合释放后仍可单独获取
        async with acquire_llm_slots([("keyB", 1)], slots_dir=slots_dir):
            pass

    @pytest.mark.asyncio
    async def test_release_on_exit_allows_immediate_reacquire(self, slots_dir: Path):
        """退出上下文即释放：可立即重新获取（不残留）"""
        async with acquire_llm_slots([("r", 1)], slots_dir=slots_dir):
            pass
        t0 = time.monotonic()
        async with acquire_llm_slots([("r", 1)], slots_dir=slots_dir):
            pass
        assert time.monotonic() - t0 < 0.5, "槽位未在退出时释放"

    @pytest.mark.asyncio
    async def test_release_on_exception(self, slots_dir: Path):
        """异常退出上下文也要释放槽位"""
        with pytest.raises(RuntimeError):
            async with acquire_llm_slots([("e", 1)], slots_dir=slots_dir):
                raise RuntimeError("boom")
        async with acquire_llm_slots([("e", 1)], slots_dir=slots_dir):
            pass

    @pytest.mark.asyncio
    async def test_no_limits_passes_through(self, slots_dir: Path):
        """空约束（不限并发）直接放行，不等待"""
        t0 = time.monotonic()
        async with acquire_llm_slots(None, slots_dir=slots_dir):
            pass
        async with acquire_llm_slots([], slots_dir=slots_dir):
            pass
        async with acquire_llm_slots([("x", 0)], slots_dir=slots_dir):
            pass
        assert time.monotonic() - t0 < 0.2

    @pytest.mark.asyncio
    async def test_cancel_while_waiting_does_not_leak(self, slots_dir: Path):
        """等待中被取消：不持有任何槽位（后续可正常获取）"""
        async with acquire_llm_slots([("c", 1)], slots_dir=slots_dir):
            async def waiter():
                async with acquire_llm_slots([("c", 1)], slots_dir=slots_dir):
                    pass

            task = asyncio.create_task(waiter())
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        # 释放后仍可获取（无泄漏的僵尸槽位）
        async with acquire_llm_slots([("c", 1)], slots_dir=slots_dir):
            pass

    @pytest.mark.asyncio
    async def test_heartbeat_and_notice_callbacks(self, slots_dir: Path):
        """等待期间会定期刷新心跳、并在超阈值时打印一次提示"""
        import peng.llm.concurrency as conc

        hb_calls = {"n": 0}
        notices: list[tuple[float, str]] = []

        def hb():
            hb_calls["n"] += 1

        def notice(waited: float, desc: str):
            notices.append((waited, desc))

        # 缩短阈值，避免测试变慢
        old_notice = conc.WAIT_NOTICE_SECONDS
        old_hb = conc.WAIT_HEARTBEAT_INTERVAL
        conc.WAIT_NOTICE_SECONDS = 0.05
        conc.WAIT_HEARTBEAT_INTERVAL = 0.05
        try:
            async with acquire_llm_slots([("hb", 1)], slots_dir=slots_dir):
                async def waiter():
                    async with acquire_llm_slots(
                        [("hb", 1)],
                        slots_dir=slots_dir,
                        task_id=123,
                        heartbeat_cb=hb,
                        notice_cb=notice,
                    ):
                        pass

                task = asyncio.create_task(waiter())
                await asyncio.sleep(0.4)
        finally:
            conc.WAIT_NOTICE_SECONDS = old_notice
            conc.WAIT_HEARTBEAT_INTERVAL = old_hb
        await task

        assert hb_calls["n"] >= 1, "等待期间未刷新心跳"
        assert len(notices) == 1, f"等待提示应只打印一次: {notices}"
        assert notices[0][1] == "hb×1"


# ==================== 3. 跨进程并发受约束（真实子进程） ====================

_WORKER_SRC = r"""
import asyncio, os, sys, time
sys.path.insert(0, sys.argv[5])
from peng.llm.concurrency import acquire_llm_slots

slots_dir, key, limit, hold, log_path = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4]), sys.argv[6]

def log(tag):
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{tag} {time.time():.6f} {os.getpid()}\n")

async def main():
    async with acquire_llm_slots([(key, limit)], slots_dir=slots_dir):
        log("ENTER")
        await asyncio.sleep(hold)
        log("EXIT")

asyncio.run(main())
"""


def _run_workers(
    slots_dir: Path, key: str, limit: int, hold: float, n: int, src_root: Path
) -> dict:
    """并发启动 n 个真实子进程各获取一次槽位并持有一段时间

    Returns:
        {"elapsed": 总耗时秒, "max_conc": 日志重构的最大并发数,
         "enters": 进入次数, "enter_span": 首个/末个进入时刻之差}
    """
    log_path = slots_dir / f"events_{key.replace(':', '_')}_{limit}.log"
    if log_path.exists():
        log_path.unlink()
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _WORKER_SRC,
                str(slots_dir),
                key,
                str(limit),
                str(hold),
                str(src_root),
                str(log_path),
            ]
        )
        for _ in range(n)
    ]
    t0 = time.monotonic()
    for p in procs:
        assert p.wait(timeout=60) == 0
    elapsed = time.monotonic() - t0

    enters_ts: list[float] = []
    events: list[tuple[float, int]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        tag, ts, _pid = line.split()
        events.append((float(ts), 1 if tag == "ENTER" else -1))
        if tag == "ENTER":
            enters_ts.append(float(ts))
    events.sort(key=lambda e: e[0])
    cur = mx = 0
    for _, delta in events:
        cur += delta
        mx = max(mx, cur)
    return {
        "elapsed": elapsed,
        "max_conc": mx,
        "enters": len(enters_ts),
        # 子进程启动耗时（解释器启动 ~0.3s）不参与衡量，用"进入时刻跨度"判断排队
        "enter_span": (max(enters_ts) - min(enters_ts)) if enters_ts else 0.0,
    }


class TestCrossProcess:
    """跨进程限制：N 个子进程并发打同一 provider，在途请求数不得超过 limit"""

    def test_limit_enforced_across_processes(self, slots_dir: Path):
        src_root = Path(__file__).resolve().parents[2] / "src"
        hold = 0.5
        # 3 个子进程、limit=2 ⇒ 第 3 个必须等上一波释放（进入时刻跨度 ≥ 一个持有周期）
        res = _run_workers(slots_dir, "provider:x", 2, hold, 3, src_root)
        assert res["enters"] == 3
        assert res["max_conc"] <= 2, f"跨进程并发被突破: max={res['max_conc']}"
        assert res["enter_span"] >= hold * 0.8, f"未按 limit 排队: span={res['enter_span']:.2f}"

    def test_unlimited_spawns_concurrently(self, slots_dir: Path):
        """对照：limit 足够大时 3 个子进程同时进入（证明上面的排队确由限制导致）"""
        src_root = Path(__file__).resolve().parents[2] / "src"
        hold = 0.5
        res = _run_workers(slots_dir, "provider:y", 5, hold, 3, src_root)
        assert res["enters"] == 3
        assert res["max_conc"] == 3, f"应同时并发: max={res['max_conc']}"
        assert res["enter_span"] < hold * 0.5, f"进入时刻过于分散: span={res['enter_span']:.2f}"


# ==================== 4. client 并发约束构造 ====================


class _SpecStub:
    """只暴露 config 的轻量桩（复用 GLMClient._get_concurrency_specs）"""

    _get_concurrency_specs = GLMClient._get_concurrency_specs

    def __init__(self, cfg: LLMConfig):
        self.config = cfg


def _make_cfg() -> LLMConfig:
    cfg = LLMConfig()
    cfg.max_concurrent = 8
    cfg.small_model_max_concurrent = 200
    cfg.small_model = "sm"
    cfg.providers = {
        "p-limited": ProviderConfig(api_key="k", base_url="b", model="m", small_model="sm", max_concurrent=3),
        "p-unlimited": ProviderConfig(api_key="k", base_url="b", model="m", small_model="sm"),
        "p-small-model": ProviderConfig(
            api_key="k", base_url="b", model="sm", small_model="sm"  # model == small_model
        ),
    }
    return cfg


class TestConcurrencySpecs:
    def test_global_plus_provider(self):
        stub = _SpecStub(_make_cfg())
        assert stub._get_concurrency_specs("m", "sm", "p-limited") == [
            ("global:llm", 8),
            ("provider:p-limited", 3),
        ]

    def test_provider_without_limit(self):
        stub = _SpecStub(_make_cfg())
        assert stub._get_concurrency_specs("m", "sm", "p-unlimited") == [("global:llm", 8)]

    def test_small_model_uses_small_model_limit(self):
        stub = _SpecStub(_make_cfg())
        assert stub._get_concurrency_specs("sm", "sm", "p-unlimited") == [
            ("global:small_model", 200)
        ]

    def test_no_provider(self):
        stub = _SpecStub(_make_cfg())
        assert stub._get_concurrency_specs("m", "sm", None) == [("global:llm", 8)]

    def test_zero_limit_means_unlimited(self):
        cfg = _make_cfg()
        cfg.max_concurrent = 0
        cfg.providers["p-limited"].max_concurrent = 0
        stub = _SpecStub(cfg)
        assert stub._get_concurrency_specs("m", "sm", "p-limited") == []


# ==================== 5. 端到端：真实 chat_stream + mock LLM 服务 ====================

#: 端到端 worker：重定向槽位目录到临时目录后，走真实 GLMClient.chat_stream 请求
#: mock LLM 服务（HTTP SSE），用于验证并发槽位确实包裹了真实请求全过程
_E2E_WORKER_SRC = r'''
import asyncio
import sys
from pathlib import Path

port, slots_dir, src_root = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, src_root)

import peng.llm.concurrency as conc

# 测试隔离：跨进程槽位目录重定向到临时目录，绝不干扰真实运行的 peng 实例
conc.slots_root = lambda slots_dir=None: Path(SLOTS_DIR_PLACEHOLDER)

from peng.config.loader import LLMConfig, ProviderConfig
from peng.llm.client import GLMClient

CFG = LLMConfig()
CFG.max_concurrent = 2
CFG.small_model_max_concurrent = 2
CFG.small_model = "m"
CFG.current_provider = "mock"
CFG.providers = {
    "mock": ProviderConfig(
        api_key="k",
        base_url=f"http://127.0.0.1:{port}/v1",
        model="m",
        small_model="m",
        max_concurrent=2,
        enable=True,
        priority=99,
    )
}


class _Client(GLMClient):
    @property
    def config(self):
        return CFG


class _TaskManager:
    def get_task(self, task_id=None, **kw):
        return {"id": task_id, "content": "hello", "info": "[]"}

    def update_heartbeat(self, task_id):
        return True


class _MessageManager:
    def get_task_manager(self):
        return _TaskManager()


async def main():
    client = _Client()
    parts = []
    async for chunk in client.chat_stream(
        messages=[{"role": "user", "content": "hi"}],
        task_id=None,
        message_manager=_MessageManager(),
    ):
        if chunk.content:
            parts.append(chunk.content)
    text = "".join(parts)
    if "hi" not in text:
        sys.stderr.write(f"BAD RESPONSE: {text!r}\n")
        return 1
    print(f"OK {text}")
    return 0


sys.exit(asyncio.run(main()))
'''


class _MockLLMServer:
    """最小 OpenAI 兼容 SSE 服务：统计同时在途请求数（max_concurrent）"""

    def __init__(self, hold: float):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.hold = hold
        self.lock = threading.Lock()
        self.current = 0
        self.max_concurrent = 0
        self.requests = 0
        srv = self

        body = (
            b'data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
            b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":5,"completion_tokens":1,"total_tokens":6}}\n\n'
            b"data: [DONE]\n\n"
        )

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                with srv.lock:
                    srv.current += 1
                    srv.requests += 1
                    srv.max_concurrent = max(srv.max_concurrent, srv.current)
                try:
                    time.sleep(srv.hold)  # 模拟长推理（在途请求窗口）
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                finally:
                    with srv.lock:
                        srv.current -= 1

            def log_message(self, *args):  # 静默
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestEndToEndChatStream:
    """真实 chat_stream 全链路：多进程并发请求时 provider 在途数受 max_concurrent 约束"""

    def test_provider_limit_wraps_real_request(self, tmp_path: Path, slots_dir: Path):
        src_root = Path(__file__).resolve().parents[2] / "src"
        worker = tmp_path / "e2e_worker.py"
        worker.write_text(
            _E2E_WORKER_SRC.replace("SLOTS_DIR_PLACEHOLDER", repr(str(slots_dir))),
            encoding="utf-8",
        )
        server = _MockLLMServer(hold=0.8)  # 窗口拉长，保证 3 进程必然重叠
        try:
            procs = [
                subprocess.Popen(
                    [sys.executable, str(worker), str(server.port), str(slots_dir), str(src_root)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for _ in range(3)
            ]
            outs = []
            for p in procs:
                out, err = p.communicate(timeout=120)
                outs.append((p.returncode, out.decode(), err.decode()))
        finally:
            server.stop()

        for code, out, err in outs:
            assert code == 0, f"worker 失败: rc={code}\nstdout={out}\nstderr={err}"
        assert server.requests == 3
        assert server.max_concurrent <= 2, (
            f"provider 在途请求数突破 max_concurrent=2: {server.max_concurrent}"
        )
        # 上界被真正触发（若限制失效，3 个请求会同时在途 ⇒ max_concurrent=3）
        assert server.max_concurrent == 2, (
            f"未能观测到并发达到上限（可能进程启动过于错开）: {server.max_concurrent}"
        )


# ==================== 6. 显式句柄 API（chat_stream 的 try/finally 用法） ====================


class TestSlotHandle:
    """acquire_llm_slots_handle：显式获取 + 同步释放（client 的 try/finally 用法）"""

    @pytest.mark.asyncio
    async def test_handle_release_sync_and_idempotent(self, slots_dir: Path):
        handle = await acquire_llm_slots_handle([("h", 1)], slots_dir=slots_dir)
        assert handle.acquired is True
        # 第二个获取者必须排队等待（句柄持有中）→ 释放后才返回
        async def second():
            h = await acquire_llm_slots_handle([("h", 1)], slots_dir=slots_dir)
            h.release()
            return time.monotonic()

        t0 = time.monotonic()
        task = asyncio.create_task(second())
        await asyncio.sleep(0.3)
        assert not task.done(), "槽位已被占用，第二个获取者不应立即返回"
        handle.release()  # 释放后等待者才能进入
        await task
        assert time.monotonic() - t0 >= 0.25
        handle.release()  # 幂等
        handle2 = await acquire_llm_slots_handle([("h", 1)], slots_dir=slots_dir)
        assert handle2.acquired is True
        handle2.release()

    @pytest.mark.asyncio
    async def test_handle_no_limits(self, slots_dir: Path):
        handle = await acquire_llm_slots_handle(None, slots_dir=slots_dir)
        assert handle.acquired is False
        handle.release()  # 安全 no-op

    @pytest.mark.asyncio
    async def test_generator_close_releases_slot(self, slots_dir: Path):
        """模拟 chat_stream：槽位句柄在生成器 finally 中同步释放

        覆盖 tool_engine 的 break + aclose 场景（GeneratorExit 展开路径）：
        槽位必须立刻释放，否则并发槽位泄漏会导致其他事务永久排队。
        """
        released = {"n": 0}

        async def fake_chat_stream():
            handle = await acquire_llm_slots_handle([("gen", 1)], slots_dir=slots_dir)
            try:
                for i in range(10):
                    yield i
            finally:
                handle.release()
                released["n"] += 1

        stream = fake_chat_stream()
        async for chunk in stream:
            if chunk == 2:
                break
        await stream.aclose()
        assert released["n"] == 1

        # 槽位已释放：可立即再次获取
        handle = await acquire_llm_slots_handle([("gen", 1)], slots_dir=slots_dir)
        assert handle.acquired is True
        handle.release()

    @pytest.mark.asyncio
    async def test_generator_exception_releases_slot(self, slots_dir: Path):
        """生成器内部抛异常时槽位也要释放"""
        async def fake_chat_stream():
            handle = await acquire_llm_slots_handle([("gex", 1)], slots_dir=slots_dir)
            try:
                yield 1
                raise RuntimeError("boom")
            finally:
                handle.release()

        stream = fake_chat_stream()
        with pytest.raises(RuntimeError):
            async for _ in stream:
                pass
        handle = await acquire_llm_slots_handle([("gex", 1)], slots_dir=slots_dir)
        assert handle.acquired is True
        handle.release()

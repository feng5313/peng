"""事务#2178：紧急info打断后必须显式关闭LLM流生成器（僵尸请求根治）

根因：tool_engine 检测到感叹号紧急info后 break 跳出 async for，Python 不会
自动关闭被消费的 chat_stream 异步生成器——client 侧 finally（关 httpx 连接、
停 prefill 追踪、释放信号量）直到下一轮 chat_stream 重赋值 GC 时才执行。
窗口期内被打断的请求在服务端继续生成往没人读的 socket 吐字，僵尸请求占着
信号量槽位和 GPU KV/批处理槽 → 多事务共享 server 时 KV 池被挤爆 → 前缀缓存
被驱逐 → 其他请求从头 prefill；writer 阻塞 → SSE 停滞 240s → "完全卡死"。

修复：tool_engine._aclose_llm_stream 在所有 break/GeneratorExit/取消出口显式
aclose；client.py chat_stream 的 httpx client 局部化并由生成器 finally 关闭。
"""

import asyncio
import sys

import pytest

sys.path.insert(0, "src")

from peng.llm.tool_engine import ToolExecutionEngine  # noqa: E402


def _bare_engine():
    e = object.__new__(ToolExecutionEngine)
    e._task_id = 999
    return e


class TestAcloseLlmStream:
    @pytest.mark.asyncio
    async def test_break_path_aclose_triggers_finally_immediately(self):
        """核心语义锁死：消费循环 break 后，aclose 必须立刻触发生成器 finally"""
        finally_ran = {"value": False}
        sem = asyncio.Semaphore(1)

        async def fake_stream():
            async with sem:  # 模拟 chat_stream 的信号量+finally 结构
                try:
                    for i in range(10):
                        yield i
                finally:
                    finally_ran["value"] = True

        stream = fake_stream()
        async for chunk in stream:
            if chunk == 2:
                break

        engine = _bare_engine()
        await engine._aclose_llm_stream(stream)

        assert finally_ran["value"], "break后显式aclose必须立即执行生成器finally"
        # 信号量已释放：下一次获取不应阻塞
        assert sem.locked() is False or True  # aclose后finally执行→with退出→槽位归还
        await asyncio.wait_for(sem.acquire(), timeout=0.5)
        sem.release()

    @pytest.mark.asyncio
    async def test_without_aclose_finally_does_not_run(self):
        """对照组锁死问题本身：不 aclose 时 finally 不会执行（僵尸语义）"""
        finally_ran = {"value": False}

        async def fake_stream():
            try:
                for i in range(10):
                    yield i
            finally:
                finally_ran["value"] = True

        stream = fake_stream()
        async for chunk in stream:
            if chunk == 2:
                break
        # 引用仍被局部变量持有，GC不会回收，finally未执行
        assert not finally_ran["value"], "不aclose时finally不应执行（问题复现锁死）"
        await stream.aclose()
        assert finally_ran["value"]

    @pytest.mark.asyncio
    async def test_aclose_none_is_noop(self):
        engine = _bare_engine()
        await engine._aclose_llm_stream(None)

    @pytest.mark.asyncio
    async def test_aclose_exhausted_stream_is_noop(self):
        """自然跑完的流，aclose 幂等 no-op"""
        finally_ran = {"value": False}

        async def fake_stream():
            try:
                yield 1
            finally:
                finally_ran["value"] = True

        stream = fake_stream()
        async for _ in stream:
            pass
        assert finally_ran["value"]  # 自然耗尽时finally已执行
        finally_ran["value"] = False
        engine = _bare_engine()
        await engine._aclose_llm_stream(stream)  # 不抛异常即可

    @pytest.mark.asyncio
    async def test_aclose_swallows_errors(self):
        """aclose 内部异常不向外传播（清理路径尽力而为）"""

        class Boom:
            async def aclose(self):
                raise RuntimeError("boom")

        engine = _bare_engine()
        await engine._aclose_llm_stream(Boom())


class TestClientPerCallCleanup:
    """client.py：chat_stream 的 httpx client 必须局部化并由生成器 finally 关闭"""

    def test_chat_stream_no_shared_client_mutation(self):
        import inspect

        import peng.llm.client as client_mod

        src = inspect.getsource(client_mod.GLMClient.chat_stream)
        # 本次调用专属 client（不得再赋给 self._client，防并发覆盖/泄漏）
        # 🔧 事务#2289：AsyncClient→同步Client（SSE读线程化），#2178的专属
        # client+finally关闭语义不变
        assert "_call_client = httpx.Client(" in src
        assert "self._client = httpx.AsyncClient" not in src
        assert "_call_client.stream" in src
        # finally 中关闭专属 client
        assert "_call_client.close()" in src

    def test_close_method_guarded(self):
        """close() 不再裸用 self._client（实例级client可能从未创建）"""
        import inspect

        import peng.llm.client as client_mod

        src = inspect.getsource(client_mod.GLMClient.close)
        assert "getattr(self" in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

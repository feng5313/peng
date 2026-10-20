"""事务#2289：断流保留响应熔断（#2235保留路径的无限空转修复）单元测试

背景（#2288事故）：swift-qwen3.8服务故障期，每轮流在thinking+content输出后、
tool_calls传输前断开（ReadError）。#2235保留路径保留半截content（工具调用
前导文本，32-89字符）走正常收尾 → 无tool_calls → content非空绕过空响应熔断
（重置_consecutive_empty）+ 保留路径重置_transient_rounds绕过transient熔断
→ 钩子block防结束 → 无限迭代烧钱（11:02-11:10实测65.76元）。

修复设计：
1. _stream_kept_breaker_wait(rounds)：退避计算纯函数（30s线性退避封顶300s）
2. 保留路径 _stream_kept_rounds += 1（流被中断非完整成功，不重置）
3. 流完整成功重置区 _stream_kept_rounds = 0（保留路径break跳过该区）
4. 迭代循环开头 >= _STREAM_KEPT_BREAKER_THRESHOLD 转限频等待（中断可打断）
5. 保留响应且无tool_calls时向history注入补发提示（模型直接补发，不重新长思考）

run_stream为巨型async生成器（依赖llm_client/DB/output等），与库内既有实践
一致（#2277对stall重试的处理）：退避决策提取为纯函数直接单测；run_stream内
联集成点用源码断言防回归。
"""

import inspect

import pytest

import peng.llm.tool_engine as te
from peng.llm.tool_engine import (
    _STREAM_KEPT_BREAKER_THRESHOLD,
    _STREAM_KEPT_WAIT_MAX_SECONDS,
    _stream_kept_breaker_wait,
)


# ==================== 退避计算纯函数 ====================
class TestStreamKeptBreakerWait:
    def test_below_threshold_no_wait(self):
        """未达阈值：不等待（偶发断流，保留响应后正常继续）"""
        assert _stream_kept_breaker_wait(0) == 0
        assert _stream_kept_breaker_wait(1) == 0
        assert _stream_kept_breaker_wait(2) == 0

    def test_at_threshold_30s(self):
        """首次达到阈值：等待30秒（限频起点）"""
        assert _stream_kept_breaker_wait(3) == 30

    def test_linear_backoff(self):
        """超限后线性退避：30s×超限轮数"""
        assert _stream_kept_breaker_wait(4) == 60
        assert _stream_kept_breaker_wait(5) == 90
        assert _stream_kept_breaker_wait(6) == 120

    def test_capped_at_max(self):
        """退避封顶300秒（第12轮起 30×10=300）"""
        assert _stream_kept_breaker_wait(12) == 300
        assert _stream_kept_breaker_wait(50) == 300
        assert _stream_kept_breaker_wait(1000) == 300

    def test_threshold_constant(self):
        """熔断阈值=3：连续3轮保留即限频（容忍2轮偶发断流）"""
        assert _STREAM_KEPT_BREAKER_THRESHOLD == 3

    def test_wait_max_constant(self):
        """等待上限=300秒"""
        assert _STREAM_KEPT_WAIT_MAX_SECONDS == 300

    def test_negative_rounds_safe(self):
        """防御：负数轮数（不应出现）不等待不报错"""
        assert _stream_kept_breaker_wait(-1) == 0


# ==================== run_stream内联集成点源码断言（防回归） ====================
def _run_stream_source() -> str:
    return inspect.getsource(te.ToolExecutionEngine.run_stream)


class TestRunStreamIntegrationPoints:
    """断言修复的5个集成点存在于run_stream源码的关键位置"""

    def test_keep_path_increments_counter(self):
        """保留路径（#2235）必须+1计数且不再只重置transient"""
        src = _run_stream_source()
        # 保留路径特征：content非空保留 → _stream_kept_rounds += 1 → break
        assert "self._stream_kept_rounds += 1" in src
        # +1 必须位于保留break之前（同一分支内）
        keep_br = src.index("断流保留响应轮数+1")
        br = src.index("llm_finish = True", keep_br)
        assert br > keep_br

    def test_keep_path_sets_interrupted_flag(self):
        """保留路径必须设置 stream_interrupted_kept 标志供收尾注入提示"""
        src = _run_stream_source()
        assert "stream_interrupted_kept = True" in src
        assert "stream_interrupted_kept = False" in src  # 迭代级初始化

    def test_success_path_resets_counter(self):
        """流完整成功重置区必须重置 _stream_kept_rounds = 0"""
        src = _run_stream_source()
        assert "self._stream_kept_rounds = 0" in src

    def test_iteration_start_breaker_check(self):
        """迭代循环开头必须有熔断检查（mark_iteration_start之后）"""
        src = _run_stream_source()
        check = src.index("_stream_kept_rounds >= _STREAM_KEPT_BREAKER_THRESHOLD")
        mark = src.index("history.mark_iteration_start()")
        assert check > mark  # 熔断检查在迭代边界标记之后

    def test_breaker_uses_pure_wait_function(self):
        """熔断等待时长使用提取的纯函数（与单测覆盖的实现一致）"""
        src = _run_stream_source()
        assert "_stream_kept_breaker_wait(self._stream_kept_rounds)" in src

    def test_breaker_wait_interruptible(self):
        """限频等待期间必须响应紧急中断/控制信号（#2064语义）"""
        src = _run_stream_source()
        breaker = src.index("_stream_kept_rounds >= _STREAM_KEPT_BREAKER_THRESHOLD")
        # 该分支之后的等待循环内含中断检测
        after = src[breaker:]
        assert "_has_pending_interrupts()" in after
        assert "_has_urgent_interrupts()" in after

    def test_resend_hint_injection(self):
        """保留响应且无tool_calls时注入补发提示（告知模型直接补发）"""
        src = _run_stream_source()
        assert "if stream_interrupted_kept and not tool_calls:" in src
        assert "无需重新长篇思考" in src
        assert "重新发起所需的工具调用" in src


if __name__ == "__main__":
    sys = pytest
    sys.main([__file__, "-v"])

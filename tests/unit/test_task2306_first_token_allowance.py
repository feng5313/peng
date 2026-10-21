"""事务#2306：首token预算——长上下文冷prefill不得被30秒停滞阈值误杀

根因（事务#2304死亡螺旋，实测取证）：
- client固定stall阈值=timeout_read=30s同时管辖"首个chunk到达前"的等待；
- 智谱z-glm5.3对~130K级冷缓存大请求真实TTFT落在30-40s区间（现场socket取证
  ：请求体554KB已上传并ACK，服务端约33s后才回首块——恰好越过30s阈值，
  客户端在答案送达前3秒杀掉请求）；
- 小/热请求实测TTFT 1.2-9.9s（78K冷=4.7s、103K冷+工具=8.2/8.8s）；
- stall→重试→切换provider→序列化键变化→KV缓存全量失效→更慢的冷prefill
  →必超时→熔断冷却30s（>缓存TTL）→永远冷启动。

修复三件套（单一可信源在prefill_tracker，client消费）：
1. first_token_allowance_seconds：首chunk前窗口按prefill估算动态放宽；
2. effective_stall_timeout：仅"首个chunk前"用预算，首chunk后维持原30s语义
   （#2277/#2289不回退）；
3. call_read_timeout：socket层read超时与应用层窗口同口径（#2304的
   07:16-07:20三次"ReadTimeout断流但content非空"即31s socket超时抢先炸）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from peng.llm.client import call_read_timeout  # noqa: E402
from peng.llm.prefill_tracker import (  # noqa: E402
    FIRST_TOKEN_ALLOWANCE_CAP_SECONDS,
    effective_stall_timeout,
    first_token_allowance_seconds,
)

FLOOR = 30.0  # timeout_read默认30s：短上下文行为必须与此完全一致


class TestFirstTokenAllowance:
    def test_short_context_keeps_floor(self):
        """回归锁死：短上下文（<~25K/320K）预算=原阈值，行为零变化"""
        assert first_token_allowance_seconds(1000, 320000, FLOOR) == pytest.approx(FLOOR)
        # 中段起步（25.6K/320K）：14.4*2+15=43.8 > floor，开始动态放宽
        assert first_token_allowance_seconds(25 * 1024, 320000, FLOOR) == pytest.approx(43.8)

    def test_zero_and_invalid_guards(self):
        assert first_token_allowance_seconds(0, 320000, FLOOR) == pytest.approx(FLOOR)
        assert first_token_allowance_seconds(-5, 320000, FLOOR) == pytest.approx(FLOOR)
        assert first_token_allowance_seconds(140000, 0, FLOOR) == pytest.approx(FLOOR)

    def test_mid_range_scales(self):
        """中段按比例放大：80K/320K → (0.25*180)*2+15 = 105s"""
        assert first_token_allowance_seconds(80 * 1000, 320000, FLOOR) == pytest.approx(105.0)

    def test_full_context_capped(self):
        """满载封顶：必须低于僵尸watchdog(360s)且等于cap常量"""
        assert first_token_allowance_seconds(320000, 320000, FLOOR) == pytest.approx(
            FIRST_TOKEN_ALLOWANCE_CAP_SECONDS
        )

    def test_task2304_scenario_survives_measured_ttft(self):
        """#2304现场：137K/320K（含工具~143K等效）预算必须远大于实测TTFT 33s"""
        allowance = first_token_allowance_seconds(137757, 320000, FLOOR)
        assert allowance >= 60.0, "死亡螺旋触发点是TTFT≈33s刚超30s阈值，预算须留足余量"
        assert allowance == pytest.approx(169.0, abs=5)

    def test_monotonic_non_decreasing(self):
        prev = 0.0
        for est in (0, 10_000, 40_000, 80_000, 128_000, 160_000, 240_000, 320_000):
            a = first_token_allowance_seconds(est, 320000, FLOOR)
            assert a >= prev
            prev = a


class TestEffectiveStallTimeout:
    def test_before_first_chunk_uses_allowance(self):
        assert effective_stall_timeout(0, 30.0, 169.0) == pytest.approx(169.0)

    def test_after_first_chunk_keeps_original_threshold(self):
        """首chunk后语义不回退（#2277/#2289）"""
        assert effective_stall_timeout(1, 30.0, 169.0) == pytest.approx(30.0)
        assert effective_stall_timeout(100, 30.0, 169.0) == pytest.approx(30.0)


class TestCallReadTimeout:
    def test_short_context_preserves_old_socket_behavior(self):
        """短上下文：预算=阈值 → read仍为timeout_read+1（原行为）"""
        assert call_read_timeout(30.0, 30.0) == pytest.approx(31.0)

    def test_socket_layer_never_fires_before_app_window(self):
        """长上下文：socket层不得先于应用层窗口炸（#2304三次ReadTimeout断流源头）"""
        assert call_read_timeout(30.0, 169.0) == pytest.approx(170.0)


class TestTask2304DeathSpiralContract:
    def test_end_to_end_decision(self):
        """完整决策链锁定（#2304实测数字）：

        est≈137K/compact_threshold=320K → 预算~169s；实测provider对该形态
        请求TTFT≈33s——旧逻辑30s判死，新逻辑在预算内静候答案。
        """
        allowance = first_token_allowance_seconds(137757, 320000, FLOOR)
        measured_ttft = 33.0
        window = effective_stall_timeout(0, FLOOR, allowance)
        assert window > measured_ttft, "实测TTFT必须落在容忍窗口内，否则死亡螺旋复发"
        # socket层同口径：不会在应用窗口之前抢先ReadTimeout
        assert call_read_timeout(FLOOR, allowance) > measured_ttft
        # 首chunk之后（流已开始后的真实停滞）仍按30s快速止损
        assert effective_stall_timeout(1, FLOOR, allowance) == pytest.approx(FLOOR)

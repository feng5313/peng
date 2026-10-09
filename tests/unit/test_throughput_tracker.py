#!/usr/bin/env python3
"""事务#1941：ThroughputTracker 吞吐率算法测试

验证核心场景：
1. 匀速流式：显示速率接近真实生成速率
2. burst 免疫：服务器缓冲涌出数千 token 时速率不虚高（修复前实测尖峰 5663 tok/s）
3. usage 校准收敛：多次 record_usage 后钳制上限收紧
4. burst 后自然回落：积分速率随时间指数回落到真值
5. 窗口空闲自愈：跨请求长间隙自动重置积分窗口
6. end_request 清零：无活跃请求时不显示（消除幽灵残留）
7. 首chunk冷启动：MIN_ELAPSED 防抖
"""
import sys

sys.path.insert(0, "src")

import peng.llm.throughput_tracker as tt
from peng.llm.throughput_tracker import ThroughputTracker


class FakeClock:
    """虚拟时钟：控制 time.time() 便于确定性测试"""

    def __init__(self):
        self.v = 1000.0

    def set(self, v):
        self.v = v

    def advance(self, dt):
        self.v += dt


def make_tracker(clock):
    """构造使用虚拟时钟的 tracker（重置单例，保证测试隔离）"""
    # 全模块 monkeypatch time（tracker 内部全部通过 tt.time.time 取时）
    tt.time = type("M", (), {"time": staticmethod(lambda: clock.v)})()
    ThroughputTracker._instance = None  # 重置单例，避免跨测试状态泄漏
    tr = ThroughputTracker()
    return tr


def test_uniform_stream():
    """匀速流：60 tok/s（120字符/s），显示速率应接近 60"""
    clock = FakeClock()
    tr = make_tracker(clock)
    tr.start_request("glm")
    # 模拟30秒，每0.1秒一个chunk（12字符=6 tokens）
    for i in range(300):
        clock.advance(0.1)
        tr.record("glm", 12)
    rate = tr._provider_state["glm"]["rate"]
    assert 50 <= rate <= 75, f"匀速流速率异常: {rate}"
    print(f"PASS 匀速流: rate={rate:.1f} (期望~60)")


def test_burst_immunity():
    """burst 免疫：30s 匀速后 1s 内涌出 8000 tokens（旧算法实测尖峰 5663）"""
    clock = FakeClock()
    tr = make_tracker(clock)
    tr.start_request("glm")
    # 先建立校准：前一个请求 60 tok/s（60秒生成3600 tokens）
    tr.record_usage("glm", 3600, 60.0)
    clock.advance(2.0)  # 工具执行间隙
    tr.start_request("glm")
    # 匀速 10 秒：60 tok/s
    for i in range(100):
        clock.advance(0.1)
        tr.record("glm", 12)
    r_before = tr._provider_state["glm"]["rate"]
    assert 50 <= r_before <= 75, f"burst前速率异常: {r_before}"
    # burst：1秒内涌出 8000 tokens（16000字符，模拟服务器缓冲的thinking内容）
    for i in range(160):
        tr.record("glm", 100)  # 0时间间隔内高频到达
    clock.advance(1.0)
    tr.record("glm", 100)
    r_burst = tr._provider_state["glm"]["rate"]
    # cap = max(150, 2.5×60) = 150
    assert r_burst <= 160, f"burst尖峰未被钳制: {r_burst} (期望≤150)"
    print(f"PASS burst免疫: burst前={r_before:.1f}, burst后={r_burst:.1f} (期望≤150)")


def test_burst_natural_decay():
    """burst 后自然回落：继续匀速，速率在数秒内回到真值附近"""
    clock = FakeClock()
    tr = make_tracker(clock)
    tr.record_usage("glm", 3600, 60.0)
    tr.start_request("glm")
    for i in range(100):
        clock.advance(0.1)
        tr.record("glm", 12)
    for i in range(160):
        tr.record("glm", 100)
    # burst 后继续匀速 60 tok/s，观察回落
    rates = []
    for i in range(100):
        clock.advance(0.1)
        tr.record("glm", 12)
        if i % 10 == 9:
            rates.append(tr._provider_state["glm"]["rate"])
    # 10秒后应接近 60~150 区间且回落趋势
    assert rates[-1] < rates[0], "burst后速率未回落"
    assert rates[-1] <= 160, f"10秒后仍未回落: {rates[-1]}"
    print(f"PASS burst回落: {rates[0]:.1f} → {rates[-1]:.1f} (10秒内)")


def test_calibration_convergence():
    """usage 校准：多次 record_usage 后 cap 收紧到 2.5×校准值"""
    clock = FakeClock()
    tr = make_tracker(clock)
    # 5次请求，每次 60 tok/s（60s 生成 3600 tokens），间隔 30s
    for _ in range(5):
        tr.record_usage("glm", 3600, 60.0)
        clock.advance(30.0)
    state = tr._provider_state["glm"]
    cal = state["calibrated"]
    assert 50 <= cal <= 70, f"校准速率未收敛: {cal}"
    cap = tr._calc_cap(state)
    assert cap == 150.0, f"cap计算错误: {cap}"
    print(f"PASS 校准收敛: calibrated={cal:.1f}, cap={cap}")


def test_cold_start_hard_cap():
    """冷启动（无校准）：burst 被硬上限 800 钳制"""
    clock = FakeClock()
    tr = make_tracker(clock)
    tr.start_request("glm")
    # 0.1秒内涌出巨量数据（5000 tokens）
    for i in range(100):
        tr.record("glm", 100)
    clock.advance(0.1)
    tr.record("glm", 100)
    r = tr._provider_state["glm"]["rate"]
    assert r <= 810, f"冷启动burst未钳制: {r}"
    assert r > 0
    print(f"PASS 冷启动硬上限: rate={r:.1f} (≤800)")


def test_window_idle_reset():
    """窗口空闲自愈：请求间隙>10s 后新数据不继承旧窗口累计"""
    clock = FakeClock()
    tr = make_tracker(clock)
    # 请求A：1秒内 burst 3000 tokens
    for i in range(60):
        tr.record("glm", 100)
    # 长间隙 15s（工具执行，无 start/end 配对——自愈场景）
    clock.advance(15.0)
    # 请求B：匀速数据到达
    for i in range(20):
        clock.advance(0.1)
        tr.record("glm", 12)
    r = tr._provider_state["glm"]["rate"]
    # 请求B 窗口：240 tokens / ~2s = 120 tok/s 左右（无旧窗口污染则不会是 1500+）
    assert r <= 200, f"窗口未自愈，旧累计泄漏: {r}"
    print(f"PASS 窗口自愈: 请求B速率={r:.1f} (无旧窗口污染)")


def test_end_request_zero():
    """end_request：active 归零时 rate 清 0 并写 0 值文件"""
    import json
    import tempfile
    from pathlib import Path

    clock = FakeClock()
    tr = make_tracker(clock)
    # 重定向数据目录到临时目录，避免污染
    with tempfile.TemporaryDirectory() as td:
        tt._get_data_dir = lambda: Path(td)
        tr.start_request("glm")
        for i in range(50):
            clock.advance(0.1)
            tr.record("glm", 12)
        assert tr._provider_state["glm"]["rate"] > 0
        tr.end_request("glm")
        assert tr._provider_state["glm"]["rate"] == 0.0, "end后rate未清0"
        # 文件应写入0值
        files = list(Path(td).glob("throughput_glm_*.json"))
        assert len(files) == 1, f"文件数异常: {files}"
        data = json.loads(files[0].read_text())
        assert data["throughput"] == 0, f"文件未写0: {data}"
        # 多次 end（不配对）不产生负计数
        tr.end_request("glm")
        tr.end_request("glm")
        assert tr._provider_state["glm"]["active_requests"] == 0
        print("PASS end_request清零: rate=0, 文件已写0值, 计数防负")


def test_concurrent_requests():
    """并发请求：两流交错，速率=总吞吐（进程级语义）"""
    clock = FakeClock()
    tr = make_tracker(clock)
    tr.start_request("glm")
    tr.start_request("glm")  # 并发第二请求
    # 两流各 60 tok/s 交错，总 120 tok/s
    for i in range(100):
        clock.advance(0.1)
        tr.record("glm", 12)  # 流A
        tr.record("glm", 12)  # 流B
    r = tr._provider_state["glm"]["rate"]
    assert 100 <= r <= 145, f"并发总速率异常: {r}"
    # 一个结束不清零（另一个还活跃）
    tr.end_request("glm")
    assert tr._provider_state["glm"]["rate"] > 0, "并发中单请求结束误清零"
    tr.end_request("glm")
    assert tr._provider_state["glm"]["rate"] == 0.0
    print(f"PASS 并发请求: 总速率={r:.1f} (期望~120), 单结束不清零")


def test_min_elapsed_damping():
    """冷启动初期防抖：首chunk后极短时间内不产生巨大比值"""
    clock = FakeClock()
    tr = make_tracker(clock)
    tr.start_request("glm")
    # 首chunk：50 tokens 在 0.01s 内到达
    clock.advance(0.01)
    tr.record("glm", 100)
    r = tr._provider_state["glm"]["rate"]
    # 50 tokens / max(0.01, 1.0) = 50，不是 5000
    assert r <= 810 and r >= 40, f"初期防抖失效: {r}"
    print(f"PASS 初期防抖: rate={r:.1f} (50 tokens/0.01s → ~50)")


def test_snapshot_merge_and_filter_zero():
    """get_snapshot：多进程求和 + 0值过滤"""
    import tempfile
    from pathlib import Path

    clock = FakeClock()
    tr = make_tracker(clock)
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        tt._get_data_dir = lambda: d
        now = clock.v
        # 进程1：100 tok/s；进程2：50 tok/s；进程3：0（空闲）
        for pid, tps in [(111, 100.0), (222, 50.0), (333, 0.0)]:
            (d / f"throughput_glm_{pid}.json").write_text(
                __import__("json").dumps({"timestamp": now, "throughput": tps, "pid": pid})
            )
        # 过期文件
        (d / "throughput_glm_999.json").write_text(
            __import__("json").dumps({"timestamp": now - 999, "throughput": 500.0, "pid": 999})
        )
        snap = tr.get_snapshot()
        assert snap["throughputs"]["glm"] == 150.0, f"求和错误: {snap}"
        print(f"PASS snapshot求和: {snap['throughputs']}")


def test_replay_real_burst():
    """真实生产尖峰场景回放：复现22:24:47的5663尖峰时序"""
    clock = FakeClock()
    tr = make_tracker(clock)
    tr.record_usage("glm", 3716, 60.0)  # 前一请求校准（真实场景completion=3716）
    clock.advance(3.0)
    tr.start_request("glm")
    # 匀速40s
    for i in range(400):
        clock.advance(0.1)
        tr.record("glm", 12)
    # burst: 11300字符(5663 tokens) 在1s窗口内到达（旧算法产生5663.5尖峰）
    peak = 0
    for i in range(113):
        tr.record("glm", 100)
        peak = max(peak, tr._provider_state["glm"]["rate"])
    clock.advance(1.0)
    for i in range(113):
        tr.record("glm", 100)
    peak = max(peak, tr._provider_state["glm"]["rate"])
    assert peak <= 160, f"生产尖峰场景未修复: peak={peak} (旧算法5663)"
    print(f"PASS 生产尖峰回放: peak={peak:.1f} (旧算法同场景=5663.5)")


if __name__ == "__main__":
    test_uniform_stream()
    test_burst_immunity()
    test_burst_natural_decay()
    test_calibration_convergence()
    test_cold_start_hard_cap()
    test_window_idle_reset()
    test_end_request_zero()
    test_concurrent_requests()
    test_min_elapsed_damping()
    test_snapshot_merge_and_filter_zero()
    test_replay_real_burst()
    print("\n✅ 全部 11 项测试通过")

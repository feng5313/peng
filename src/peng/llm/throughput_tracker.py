"""
Provider 吞吐率追踪器（跨进程方案）

实时统计每个 Provider 的**模型生成速率**（tokens/s），用于前端显示和动画帧率调节。

算法：指数加权速率估计 + usage 校准钳制（事务#1941 重构）

背景（旧算法缺陷）：旧实现按"区间采样"计算 interval_rate = accumulated/dt 并用 EWMA 平滑。
GLM 等 thinking 模型的输出到达客户端并非匀速——思考停顿/服务器缓冲后会在 ~1s 内
涌出数千 token（burst）。burst 的 token 挂在极短的 dt 采样窗口上导致 interval_rate
虚高至数千 tok/s（实测生产环境单进程尖峰 5663/4771 tok/s），EWMA(τ=3s) 又让尖峰
残留数秒。这不是真实生成速率，而是被缓冲扭曲的"接收速率"。

新算法（指数加权速率估计器，O(1) 增量）：
1. weighted = weighted·e^(-Δt/τ) + tokens（RATE_TAU=2.5s）
2. rate = weighted / min(τ, max(自首chunk起的墙钟时间, MIN_ELAPSED))
   - 请求初期分母用实际时长 → 无冷启动欠冲（首秒即接近真实速率）
   - 稳态分母固定 τ → 匀速流的无偏估计（rate ≈ 真实速率）
   - burst 涌入 N tokens → raw_rate 瞬间 N/τ，被钳制；随后按 e^(-t/τ)
     连续衰减，约 3~4τ（8~10s）回落到真实速率
3. usage 校准钳制：每个请求结束时 record_usage() 记录精确速率
   （completion_tokens/duration），跨请求维护校准速率 EWMA(τ=90s)；
   流式估计钳制到 cap = max(MIN_CAP, CALIB_FACTOR×校准速率)，
   冷启动（无校准数据）时使用硬上限 HARD_CAP。
   - 效果：glm 校准 ~60 tok/s → burst 显示 ≤150，~8 秒回落到真值
4. 请求结束（active 归零）时速率清 0：无活跃请求不显示，消除幽灵残留

跨进程共享：
- runner 进程通过 record()/record_usage() 写入速率到文件
- 每个 runner 子进程写独立文件（含PID），避免多进程互相覆盖
- webui 进程通过 get_snapshot() 读取同 provider 的所有进程文件并求和
- 数据文件：data/throughput_{provider}_{pid}.json
"""

from __future__ import annotations

import atexit
import json
import logging
import math
import os
import re
import threading
import time
from pathlib import Path

from peng.common.paths import chown_to_project_owner, ensure_dir_with_owner

logger = logging.getLogger(__name__)

# ============ 核心算法参数 ============

# 字符到 token 的估算比例
# LLM tokenization 规则：中文约 1.5 字符/token，英文约 4 字符/token
# 混合文本取约 2 字符/token 作为合理估算
CHARS_PER_TOKEN = 2.0

# 指数加权时间常数（秒）：速率估计器的滑动窗口尺度
# rate = 指数加权和 / min(τ, max(自首chunk起的墙钟时间, 1s))
# - τ 越小平滑越弱但 burst 回落越快；τ=2.5s 时 burst 尖峰 ~8 秒衰减完毕
# - 匀速流稳态：rate = 真实速率（指数加权窗口的无偏估计）
RATE_TAU = 2.5

# 归一化分母下限（秒）：防止请求刚开始时（首 chunk 后极短时间内）比值剧烈抖动
MIN_ELAPSED = 1.0

# 校准速率 EWMA 时间常数（秒）：跨请求平滑真实 usage 速率
# τ=90s 意味着几十个请求后收敛到该 provider 的典型速率
CALIB_EWMA_TAU = 90.0

# 流式估计钳制下限（tokens/s）：保证较慢模型不被钳得过低
MIN_CAP = 150.0

# 流式估计钳制系数：cap = max(MIN_CAP, CALIB_FACTOR × 校准速率)
# 校准速率是真实生成速率，burst 估计值不应显著超过它的若干倍
CALIB_FACTOR = 2.5

# 冷启动硬上限（tokens/s）：尚无校准数据时的绝对上限
# 覆盖本地 vLLM/sglang 等高速后端（可达数百 tok/s），同时压制 burst 尖峰
HARD_CAP = 800.0

# 窗口空闲重置间隔（秒）：record() 间隔超过此值时重置指数加权状态。
# （指数衰减本身在 4τ≈10s 后残留<2%，此处显式重置保证状态干净）
WINDOW_RESET_GAP = 10.0

# ============ 文件管理参数 ============

# 跨进程共享数据文件前缀
_THROUGHPUT_PREFIX = "throughput_"

# 数据过期时间（秒）- provider文件超过此时间视为不活跃
DATA_EXPIRY = 120.0

# 文件写入间隔（秒），避免频繁IO
FILE_WRITE_INTERVAL = 1.0

# 清理间隔（秒）- 定期清理过期文件
CLEANUP_INTERVAL = 30.0

# ============ 动画参数 ============

# 吞吐率阈值（tokens/s）
LOW_THROUGHPUT = 5.0
HIGH_THROUGHPUT = 50.0


def _get_data_dir() -> Path:
    """获取数据目录"""
    try:
        from peng.config import get_project_root
        return get_project_root() / "data"
    except Exception:
        return Path("data")


def _get_throughput_file(provider: str, pid: int = None) -> Path:
    """获取某个provider的吞吐率数据文件路径

    Args:
        provider: Provider 名称
        pid: 进程ID，如果指定则生成含PID的文件名（每个进程独立文件）
    """
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", provider)
    if pid is not None:
        return _get_data_dir() / f"throughput_{safe_name}_{pid}.json"
    return _get_data_dir() / f"throughput_{safe_name}.json"


def _get_all_provider_files(data_dir: Path = None) -> list[Path]:
    """获取所有provider的吞吐率文件"""
    if data_dir is None:
        data_dir = _get_data_dir()
    if not data_dir.exists():
        return []
    return list(data_dir.glob(f"{_THROUGHPUT_PREFIX}*.json"))


def _estimate_tokens(char_count: int) -> float:
    """将字符数估算为 token 数

    基于主流 LLM tokenizer 的统计规律：
    - 纯英文：约 4 字符/token
    - 纯中文：约 1.5 字符/token
    - 混合文本：约 2 字符/token（保守估计）
    """
    return char_count / CHARS_PER_TOKEN


def _calc_animation_duration(throughput: float) -> float:
    """计算 CSS animation-duration

    帧率范围 0.1 ~ 2.0 fps，对应 duration 0.1s ~ 2.0s
    吞吐率越高帧率越高（动画越快）
    """
    if throughput <= LOW_THROUGHPUT:
        fps = 0.1
    elif throughput >= HIGH_THROUGHPUT:
        fps = 2.0
    else:
        ratio = (throughput - LOW_THROUGHPUT) / (HIGH_THROUGHPUT - LOW_THROUGHPUT)
        fps = 0.1 + ratio * 1.9  # 0.1 ~ 2.0

    duration = 1.0 / fps if fps > 0 else 2.0
    return round(max(0.1, min(2.0, duration)), 2)


class ThroughputTracker:
    """
    Provider 吞吐率追踪器（请求级积分速率 + usage 校准钳制）

    算法原理：
    1. start_request()/end_request() 维护活跃请求计数，界定积分窗口
    2. record() 累计输出字符并计算请求级积分速率（burst 免疫）：
       rate = 窗口累计tokens / max(自首chunk起的墙钟时间, MIN_ELAPSED)
    3. rate 钳制到 cap = max(MIN_CAP, CALIB_FACTOR×校准速率)（或 HARD_CAP）
    4. record_usage() 用真实 usage（completion_tokens/duration）更新校准速率

    跨进程通过文件共享数据：
    - record(): 在 runner 进程中调用，记录 SSE chunk 字符数
    - record_usage(): 在 runner 进程中请求结束时调用，校准速率
    - get_snapshot(): 在 webui 进程中调用，读取最新吞吐率数据
    - cleanup(): 清理过期文件和无效进程文件
    - 进程退出时自动清理本进程的文件
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self._data_lock = threading.Lock()
        # 每个 provider 的状态：
        #   rate: 当前显示速率（tokens/s，钳制后）
        #   active_requests: 活跃请求数（0 表示空闲，速率清 0）
        #   weighted: 指数加权和 Σ tokens_i·e^(-(now-t_i)/RATE_TAU)（O(1)增量维护）
        #   window_start: 活跃周期首 chunk 时刻（None 表示尚未有数据）
        #   last_record_time: 上次 record() 时刻（加权衰减与空闲重置用）
        #   calibrated: usage 校准速率 EWMA（tokens/s，None 表示尚无校准）
        #   last_calib_time: 上次校准更新时刻（计算 EWMA 的 dt 用）
        self._provider_state: dict[str, dict] = {}
        # 上次写入文件的时间（每个provider独立）
        self._last_file_write: dict[str, float] = {}
        # 当前进程PID
        self._pid = os.getpid()
        # 上次清理时间
        self._last_cleanup = 0.0

    def _get_state(self, provider: str, now: float) -> dict:
        """获取或创建 provider 状态（须持有 self._data_lock）"""
        state = self._provider_state.get(provider)
        if state is None:
            state = {
                "rate": 0.0,
                "active_requests": 0,
                "weighted": 0.0,
                "window_start": None,
                "calibrated": None,
                "last_calib_time": None,
                "last_record_time": None,
            }

            self._provider_state[provider] = state
        return state

    def _calc_cap(self, state: dict) -> float:
        """计算流式估计的钳制上限（须持有 self._data_lock）"""
        calibrated = state.get("calibrated")
        if calibrated is None or calibrated <= 0:
            return HARD_CAP
        return max(MIN_CAP, CALIB_FACTOR * calibrated)

    def start_request(self, provider: str) -> None:
        """标记一个请求开始（活跃计数 +1，首个请求开启速率窗口）

        Args:
            provider: Provider 名称
        """
        if not provider:
            return
        now = time.time()
        with self._data_lock:
            state = self._get_state(provider, now)
            state["active_requests"] += 1
            if state["active_requests"] == 1:
                # 新的活跃周期：重置速率窗口
                state["weighted"] = 0.0
                state["window_start"] = None
                state["last_record_time"] = None

    def end_request(self, provider: str) -> None:
        """标记一个请求结束（活跃计数 -1，归零时速率清 0 并立即写文件）

        在 runner 请求的 finally 中调用（正常/异常/中断路径均覆盖）。

        Args:
            provider: Provider 名称
        """
        if not provider:
            return
        write_zero = False
        with self._data_lock:
            state = self._provider_state.get(provider)
            if state is None:
                return
            state["active_requests"] = max(0, state["active_requests"] - 1)
            if state["active_requests"] == 0:
                # 无活跃请求：显示速率清 0（消除空闲幽灵残留）
                state["rate"] = 0.0
                state["weighted"] = 0.0
                state["window_start"] = None
                write_zero = True

        if write_zero:
            # 写入 0 值文件，让 webui 立即停止显示该进程的速率
            try:
                self._write_to_file(provider)
            except Exception as e:
                logger.debug(f"[ThroughputTracker] end_request后写入文件失败: {e}")

    def record_usage(self, provider: str, completion_tokens: int, duration_seconds: float) -> None:
        """记录真实usage数据（在 runner 进程流式响应结束时调用）

        用精确速率（completion_tokens/duration）更新跨请求校准 EWMA，
        并将当前显示速率设置为该精确值。

        Args:
            provider: Provider 名称
            completion_tokens: 模型实际生成的token数
            duration_seconds: 接口响应时间（秒）
        """
        if not provider or completion_tokens <= 0 or duration_seconds <= 0:
            return

        now = time.time()
        with self._data_lock:
            state = self._get_state(provider, now)
            tps = completion_tokens / duration_seconds

            # 跨请求校准 EWMA：τ=90s（按墙钟时间衰减）
            prev = state.get("calibrated")
            last_t = state.get("last_calib_time")
            if prev is None or last_t is None:
                state["calibrated"] = tps
            else:
                dt = max(now - last_t, 0.001)
                alpha = 1.0 - math.exp(-dt / CALIB_EWMA_TAU)
                state["calibrated"] = alpha * tps + (1.0 - alpha) * prev
            state["last_calib_time"] = now

            # 真实 usage 速率直接作为显示值（不钳制，它是精确测量值）
            state["rate"] = round(tps, 1)
            logger.debug(
                f"[ThroughputTracker] record_usage: provider={provider}, "
                f"completion_tokens={completion_tokens}, duration={duration_seconds:.2f}s, "
                f"tps={tps:.1f}, calibrated={state['calibrated']:.1f}"
            )

        # 立即写入文件（跨进程共享）
        try:
            self._write_to_file(provider)
        except Exception as e:
            logger.debug(f"[ThroughputTracker] record_usage后写入文件失败: {e}")

    def record(self, provider: str, char_count: int) -> None:
        """
        记录一次输出（在 runner 进程中调用）

        指数加权速率估计（O(1) 增量，等价于指数加权滑动窗口）：
        1. weighted = weighted·e^(-Δt/τ) + tokens（增量衰减）
        2. rate = weighted / min(τ, max(自首chunk的墙钟时间, MIN_ELAPSED))
           - 请求初期（elapsed<τ）分母用实际时长 → 无冷启动欠冲
           - 之后分母固定为 τ → burst 后指数快速回落
        3. rate 钳制到 cap = max(MIN_CAP, CALIB_FACTOR×校准速率)（或 HARD_CAP）

        burst 免疫原理：服务器缓冲涌出的 N tokens 使 raw_rate 瞬间达 N/τ，
        被钳制到 cap；随后按 e^(-t/τ) 连续衰减，约 3~4τ 回落到真实速率。

        Args:
            provider: Provider 名称
            char_count: 本次输出的字符数
        """
        if char_count <= 0 or not provider:
            return

        now = time.time()
        tokens = _estimate_tokens(char_count)

        with self._data_lock:
            state = self._get_state(provider, now)

            # 窗口空闲自愈：距上次 record 超过 WINDOW_RESET_GAP 视为新请求开始。
            # 即使 start/end_request 未严格配对（异常路径/旧版调用方），
            # 请求间隙的旧加权残留（已衰减至~0）也被显式清零。
            last_rec = state.get("last_record_time")
            if last_rec is not None and now - last_rec > WINDOW_RESET_GAP:
                state["weighted"] = 0.0
                state["window_start"] = None

            # 指数加权增量更新（dt 钳非负，防时钟回拨导致权重放大）
            if last_rec is not None and state["weighted"] > 0:
                dt = max(now - last_rec, 0.0)
                decay = math.exp(-dt / RATE_TAU)
                state["weighted"] = state["weighted"] * decay + tokens
            else:
                state["weighted"] = tokens
            state["last_record_time"] = now
            if state["window_start"] is None:
                state["window_start"] = now

            # 自适应归一化：初期用实际时长，稳态用 τ 封顶
            elapsed = max(now - state["window_start"], 0.0)
            norm = min(RATE_TAU, max(elapsed, MIN_ELAPSED))
            rate = state["weighted"] / norm
            rate = min(rate, self._calc_cap(state))
            state["rate"] = rate

        # 定期写入文件（跨进程共享）
        if now - self._last_file_write.get(provider, 0) >= FILE_WRITE_INTERVAL:
            self._write_to_file(provider)
            self._last_file_write[provider] = now

        # 定期清理过期文件
        if now - self._last_cleanup >= CLEANUP_INTERVAL:
            self.cleanup()
            self._last_cleanup = now

    def _write_to_file(self, provider: str) -> None:
        """将单个 provider 的吞吐率写入文件

        写入格式: {
            "timestamp": 当前时间戳,
            "throughput": 当前显示速率（tokens/s，空闲时为 0）,
            "pid": 进程ID
        }

        注意：rate 为 0 时也写入（覆盖旧值），用于 end_request 后立即
        清除该进程在 webui 端的显示（get_snapshot 会过滤掉 0 值）。
        """
        try:
            with self._data_lock:
                state = self._provider_state.get(provider)
                if not state:
                    return
                tps = round(state["rate"], 1)

            data = {
                "timestamp": time.time(),
                "throughput": tps,
                "pid": self._pid,
            }

            filepath = _get_throughput_file(provider, pid=self._pid)
            ensure_dir_with_owner(filepath.parent)

            # 原子写入：先写临时文件，再rename
            tmp_path = filepath.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(str(tmp_path), str(filepath))
            chown_to_project_owner(filepath)

        except Exception as e:
            logger.debug(f"[ThroughputTracker] 写入文件失败(provider={provider}): {e}")

    def get_snapshot(self) -> dict:
        """
        读取所有provider的最新吞吐率快照（在 webui 进程中调用）

        合并同 provider 的所有进程文件：直接求和各进程的吞吐率。

        Returns:
            {"throughputs": {provider: tps}, "frame_rates": {provider: duration}}
            只返回未过期的provider数据（DATA_EXPIRY 秒内有更新的）
        """
        try:
            now = time.time()
            files = _get_all_provider_files()

            # 按 provider 分组收集所有进程的吞吐率
            provider_rates: dict[str, list[float]] = {}

            for filepath in files:
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    # 检查是否过期
                    ts = data.get("timestamp", 0)
                    if now - ts > DATA_EXPIRY:
                        continue

                    # 从文件名提取 provider 名称
                    filename = filepath.stem
                    if filename.startswith(_THROUGHPUT_PREFIX):
                        remainder = filename[len(_THROUGHPUT_PREFIX):]
                        parts = remainder.rsplit("_", 1)
                        if len(parts) == 2 and parts[1].isdigit():
                            provider = parts[0]
                        else:
                            provider = remainder
                    else:
                        continue

                    tps = data.get("throughput", 0)
                    if tps <= 0:
                        continue

                    if provider not in provider_rates:
                        provider_rates[provider] = []
                    provider_rates[provider].append(tps)

                except Exception:
                    continue

            if not provider_rates:
                return {}

            # 合并：对每个 provider，求和所有进程的吞吐率
            throughputs = {}
            frame_rates = {}
            for provider, rates in provider_rates.items():
                total_tps = round(sum(rates), 1)
                throughputs[provider] = total_tps
                frame_rates[provider] = _calc_animation_duration(total_tps)

            return {
                "timestamp": now,
                "throughputs": throughputs,
                "frame_rates": frame_rates,
            }

        except Exception as e:
            logger.debug(f"[ThroughputTracker] 读取快照失败: {e}")
            return {}

    def cleanup(self) -> None:
        """清理过期文件和无效进程文件"""
        try:
            now = time.time()
            data_dir = _get_data_dir()
            if not data_dir.exists():
                return

            for filepath in data_dir.glob(f"{_THROUGHPUT_PREFIX}*.json"):
                try:
                    # 快速检查文件修改时间，避免频繁读文件
                    if now - filepath.stat().st_mtime > DATA_EXPIRY:
                        filepath.unlink()
                        logger.debug(f"[ThroughputTracker] 已删除过期文件: {filepath.name}")
                        continue

                    # 检查文件中的pid是否有效
                    filename = filepath.stem
                    if filename.startswith(_THROUGHPUT_PREFIX):
                        remainder = filename[len(_THROUGHPUT_PREFIX):]
                        parts = remainder.rsplit("_", 1)
                        if len(parts) == 2 and parts[1].isdigit():
                            pid = int(parts[1])
                            from peng.common.platform import process_exists
                            if not process_exists(pid):
                                filepath.unlink()
                                logger.debug(
                                    f"[ThroughputTracker] 已删除无效进程文件: {filepath.name} (pid={pid})"
                                )
                except Exception:
                    continue

        except Exception as e:
            logger.debug(f"[ThroughputTracker] 清理文件失败: {e}")


# 全局单例
_throughput_tracker: ThroughputTracker | None = None


def _cleanup_on_exit() -> None:
    """进程退出时清理本进程的吞吐率文件"""
    try:
        tracker = get_throughput_tracker()
        pid = tracker._pid
        data_dir = _get_data_dir()
        if data_dir.exists():
            for filepath in data_dir.glob(f"{_THROUGHPUT_PREFIX}*_{pid}.json"):
                try:
                    filepath.unlink()
                    logger.debug(f"[ThroughputTracker] 进程退出，已删除文件: {filepath.name}")
                except Exception:
                    pass
    except Exception:
        pass


# 注册进程退出清理
atexit.register(_cleanup_on_exit)


def get_throughput_tracker() -> ThroughputTracker:
    """获取全局吞吐率追踪器"""
    global _throughput_tracker
    if _throughput_tracker is None:
        _throughput_tracker = ThroughputTracker()
    return _throughput_tracker

"""
Provider 吞吐率追踪器（跨进程方案）

实时统计每个 Provider 的输出吞吐率（tokens/s），用于前端显示和动画帧率调节。

核心算法：累计计数 + 周期采样 + EWMA 平滑
- 与 Linux 内核网络流量统计原理一致：
  1. 持续累计接收的 token 数（类似网络驱动的字节计数器）
  2. 每隔 CALC_INTERVAL 秒采样一次，计算区间速率 = 累计 token / 采样间隔
  3. 用 EWMA 平滑区间速率，消除抖动
- 比逐 chunk 计算瞬时速率更准确、噪声更低、收敛更快

跨进程共享：
- runner 进程通过 record() 写入 EWMA 吞吐率到文件
- 每个 runner 子进程写独立文件（含PID），避免多进程互相覆盖
- webui 进程通过 get_snapshot() 读取同 provider 的所有进程文件并合并
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

# 速率计算采样间隔（秒）
# 每隔这么多秒，计算一次 "累计token数/时间间隔" 作为区间速率
CALC_INTERVAL = 1.0

# EWMA 时间常数（秒）：平滑区间速率的抖动
# τ 越大越平滑但响应越慢，τ=3 意味着约 9 秒达到 95% 稳态
EWMA_TIME_CONSTANT = 3.0

# 字符到 token 的估算比例
# LLM tokenization 规则：中文约 1.5 字符/token，英文约 4 字符/token
# 混合文本取约 2 字符/token 作为合理估算
CHARS_PER_TOKEN = 2.0

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
    Provider 吞吐率追踪器（累计计数 + 周期采样 + EWMA）

    算法原理（与 Linux 内核网络流量统计一致）：
    1. record() 持续累计 token 数（类似驱动的字节计数器）
    2. 每 CALC_INTERVAL 秒采样一次：区间速率 = 累计 token / 间隔时间
    3. 用 EWMA 平滑区间速率，消除采样间的抖动
    4. 首次采样直接使用区间速率（不从0平滑），确保快速响应

    跨进程通过文件共享数据：
    - record(): 在 runner 进程中调用，记录 SSE chunk 字符数
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
        #   rate: 当前 EWMA 平滑后的吞吐率（tokens/s）
        #   accumulated_tokens: 自上次采样以来累计的 token 数
        #   last_calc_time: 上次采样（计算区间速率）的时间戳
        #   first_sample: 是否还在等待首次采样（首次不做EWMA平滑）
        self._provider_state: dict[str, dict] = {}
        # 上次写入文件的时间（每个provider独立）
        self._last_file_write: dict[str, float] = {}
        # 当前进程PID
        self._pid = os.getpid()
        # 上次清理时间
        self._last_cleanup = 0.0

    def start_request(self, provider: str) -> None:
        """标记一个请求开始（用于计算真实响应时间）

        Args:
            provider: Provider 名称
        """
        if not provider:
            return
        now = time.time()
        with self._data_lock:
            state = self._provider_state.get(provider)
            if state:
                state["response_start_time"] = now

    def record_usage(self, provider: str, completion_tokens: int, duration_seconds: float) -> None:
        """记录真实usage数据（在 runner 进程流式响应结束时调用）

        Args:
            provider: Provider 名称
            completion_tokens: 模型实际生成的token数
            duration_seconds: 接口响应时间（秒）
        """
        if not provider or completion_tokens <= 0 or duration_seconds <= 0:
            return

        with self._data_lock:
            # 如果该provider已有状态，更新为最新请求的数据
            state = self._provider_state.get(provider)
            if state:
                # 记录真实吞吐率（completion_tokens / 响应时间）
                tps = completion_tokens / duration_seconds
                # 直接设置当前吞吐率（不经过EWMA平滑，因为是单次请求的精确值）
                state["rate"] = round(tps, 1)
                # 🔧 事务#1891：重置累计器，避免下一个请求的区间速率计算包含残留token
                # 问题：record() 持续累加 accumulated_tokens，record_usage() 直接覆盖 rate
                # 但不重置累计器，导致下一个请求的 interval_rate = (残留token + 新token) / dt 虚高
                state["accumulated_tokens"] = 0.0
                state["last_calc_time"] = time.time()
                state["first_sample"] = True  # 下一个请求首次采样直接使用区间速率
                logger.debug(
                    f"[ThroughputTracker] record_usage: provider={provider}, "
                    f"completion_tokens={completion_tokens}, duration={duration_seconds:.2f}s, tps={tps:.1f}"
                )
            else:
                # 首次使用该provider，创建状态
                self._provider_state[provider] = {
                    "rate": round(completion_tokens / duration_seconds, 1),
                    "accumulated_tokens": 0.0,
                    "last_calc_time": time.time(),
                    "first_sample": True,
                }

        # 立即写入文件（跨进程共享）
        try:
            self._write_to_file(provider)
        except Exception as e:
            logger.debug(f"[ThroughputTracker] record_usage后写入文件失败: {e}")

    def record(self, provider: str, char_count: int) -> None:
        """
        记录一次输出（在 runner 进程中调用）

        累计 token 数，达到采样间隔时计算区间速率并用 EWMA 平滑。

        Args:
            provider: Provider 名称
            char_count: 本次输出的字符数
        """
        if char_count <= 0 or not provider:
            return

        now = time.time()
        tokens = _estimate_tokens(char_count)

        with self._data_lock:
            if provider not in self._provider_state:
                self._provider_state[provider] = {
                    "rate": 0.0,
                    "accumulated_tokens": 0.0,
                    "last_calc_time": now,
                    "first_sample": True,
                }

            state = self._provider_state[provider]
            state["accumulated_tokens"] += tokens

            # 达到采样间隔，计算区间速率
            dt = now - state["last_calc_time"]
            if dt >= CALC_INTERVAL:
                interval_rate = state["accumulated_tokens"] / dt

                if state["first_sample"]:
                    # 首次采样直接使用区间速率，不从0平滑
                    state["rate"] = interval_rate
                    state["first_sample"] = False
                else:
                    # EWMA 平滑：rate_new = α × interval_rate + (1 - α) × rate_old
                    alpha = 1.0 - math.exp(-dt / EWMA_TIME_CONSTANT)
                    state["rate"] = alpha * interval_rate + (1.0 - alpha) * state["rate"]

                # 重置累计器
                state["accumulated_tokens"] = 0.0
                state["last_calc_time"] = now

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
            "throughput": EWMA 吞吐率（tokens/s）,
            "pid": 进程ID
        }
        """
        try:
            with self._data_lock:
                state = self._provider_state.get(provider)
                if not state:
                    return
                # 优先使用已计算的 rate；如果还没采样过，用累计数/时间估算
                tps = state["rate"]
                if tps <= 0:
                    dt = time.time() - state["last_calc_time"]
                    if dt > 0.01 and state["accumulated_tokens"] > 0:
                        tps = state["accumulated_tokens"] / dt
                    else:
                        return  # 确实没有数据
                tps = round(tps, 1)

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

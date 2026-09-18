"""
LLM 请求 prefill 阶段追踪器（跨进程方案）

本地部署大模型（如 s-qwen3.8）长上下文 prefill 时间较长，SSE 流在首个
chunk 到达前没有任何数据返回。tool_engine 的 update_heartbeat 只在 chunk
到达时调用，导致 prefill 期间事务心跳停更，被 heartbeat_monitor 判定
"进程存活但连续2次心跳超时"而强制恢复（事务#1819/#1864 根因）。

本模块在 LLM 请求发出前启动追踪：
1. 后台守护线程每 PREFILL_HEARTBEAT_INTERVAL 秒更新事务 heartbeat_at
   （防止心跳超时误杀，从 prefill 开始就持续刷新）
2. prefill 超过 PREFILL_OUTPUT_MIN_ELAPSED 秒后，写 data/prefill_{task_id}.json
   状态文件（跨进程共享，供 webui/CLI 读取显示 prefill 百分比）
3. prefill 超过 PREFILL_OUTPUT_MIN_ELAPSED 秒后，每 PREFILL_OUTPUT_INTERVAL 秒
   向事务输出文件写入进度行（CLI watch 直接可见），并按估算 token 数与已耗时
   计算 prefill 百分比

提示阈值：prefill 未超过 PREFILL_OUTPUT_MIN_ELAPSED（默认10秒）不产生任何提示，
短 prefill（云端 API 通常数秒内返回首 chunk）完全无感知，避免每次请求都出现
"已等待0秒"之类提示刷屏（事务#1866）。

prefill 百分比估算原理：
- 请求发出时估算输入 token 数 est_tokens（estimate_messages_tokens）
- provider 的 compact_threshold 代表其上下文能力上限（token）
- prefill 耗时与输入 token 数近似线性（KV cache 构建成本 ∝ token 数）
- 百分比 = min(99, elapsed / (est_tokens / compact_threshold * REF_PREFILL_SECONDS) * 100)
  其中 REF_PREFILL_SECONDS 是"填满 compact_threshold 上下文所需参考 prefill 秒数"，
  默认 120s（本地大模型长上下文 prefill 经验值）。百分比封顶 99%（首个 chunk
  到达即 finish，真实完成由流结束确认）。

跨进程共享：
- runner 子进程：start_request() 启动后台线程写文件+更新DB心跳
- webui 进程：get_snapshot() 读取所有未过期 prefill 文件并广播前端
- CLI watch：read_status() 直接读文件显示百分比
- 数据文件：data/prefill_{task_id}.json
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from peng.common.paths import chown_to_project_owner, ensure_dir_with_owner

logger = logging.getLogger(__name__)

# ============ 核心参数 ============

# 心跳更新间隔（秒）：远小于默认心跳超时300s，确保prefill期间心跳持续刷新
PREFILL_HEARTBEAT_INTERVAL = 10.0

# 输出文件进度行写入间隔（秒）：CLI watch可见的进度提示（低频避免刷屏）
PREFILL_OUTPUT_INTERVAL = 60.0

# prefill提示最小耗时（秒）：prefill超过该时长才对外提示（写状态文件+输出
# 进度行）。短prefill（云端API通常数秒内返回首chunk）无需打扰，避免每次
# 请求都出现"已等待0秒"提示刷屏（事务#1866）
PREFILL_OUTPUT_MIN_ELAPSED = 30.0

# "填满 compact_threshold 上下文所需参考 prefill 秒数"（本地大模型经验值）
REF_PREFILL_SECONDS = 180.0

# 数据过期时间（秒）- 文件超过此时间无更新视为残留（进程崩溃清理用）
DATA_EXPIRY = 600.0

# ============ 文件管理 ============

_PREFILL_PREFIX = "prefill_"


def _get_data_dir() -> Path:
    """获取数据目录"""
    try:
        from peng.config import get_project_root

        return get_project_root() / "data"
    except Exception:
        return Path("data")


def _get_prefill_file(task_id: int) -> Path:
    """获取某个事务的prefill状态文件路径"""
    return _get_data_dir() / f"{_PREFILL_PREFIX}{task_id}.json"


def _get_all_prefill_files(data_dir: Path | None = None) -> list[Path]:
    """获取所有prefill状态文件"""
    if data_dir is None:
        data_dir = _get_data_dir()
    if not data_dir.exists():
        return []
    return list(data_dir.glob(f"{_PREFILL_PREFIX}*.json"))


def _estimate_prefill_percent(elapsed: float, est_tokens: int, compact_threshold: int) -> int:
    """估算prefill完成百分比（封顶99%）

    Args:
        elapsed: 已耗时（秒）
        est_tokens: 估算输入token数
        compact_threshold: provider上下文能力上限（token）

    Returns:
        0-99的整数百分比
    """
    if est_tokens <= 0 or compact_threshold <= 0:
        return 0
    # 填满compact_threshold上下文需要REF_PREFILL_SECONDS，按比例缩放
    expected_seconds = (est_tokens / compact_threshold) * REF_PREFILL_SECONDS
    if expected_seconds <= 0:
        return 0
    percent = elapsed / expected_seconds * 100.0
    return max(0, min(99, int(percent)))


class PrefillTracker:
    """LLM请求prefill阶段追踪器（单例，线程安全）

    - start_request(): runner进程在LLM请求发出前调用，启动后台追踪线程
    - finish_request(): runner进程在首个chunk到达时调用，停止追踪线程
    - get_snapshot(): webui进程读取所有活跃prefill状态并广播前端
    - read_status(): CLI watch直接读取指定事务的prefill状态
    """

    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self._lock = threading.Lock()
        # task_id -> {"thread": threading.Thread, "stop_event": threading.Event}
        self._active: dict[int, dict] = {}

    # ==================== runner进程侧（写） ====================

    def start_request(
        self,
        task_id: int | None,
        est_tokens: int,
        provider: str | None = None,
        compact_threshold: int | None = None,
    ) -> None:
        """标记一个LLM请求开始prefill阶段（runner进程调用）

        Args:
            task_id: 事务ID（None时不追踪）
            est_tokens: 估算输入token数（estimate_messages_tokens结果）
            provider: provider名称（用于显示）
            compact_threshold: provider上下文能力上限token数（用于百分比估算）
        """
        if not task_id:
            return

        with self._lock:
            # 同一事务已有活跃追踪时不重复启动（重试场景由finish_request清理）
            existing = self._active.get(task_id)
            if existing and existing["thread"].is_alive():
                return

            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._track_loop,
                args=(task_id, est_tokens, provider, compact_threshold, stop_event),
                daemon=True,
                name=f"prefill-tracker-{task_id}",
            )
            self._active[task_id] = {"thread": thread, "stop_event": stop_event}

            # 注意：不立即写状态文件/输出提示——prefill超过 PREFILL_OUTPUT_MIN_ELAPSED
            # 秒才开始提示（事务#1866：立即提示导致每次请求都出现"已等待0秒"，刷屏）。
            # 心跳保护不受影响：_track_loop 从第5秒起持续刷新事务心跳。

            thread.start()

    def finish_request(self, task_id: int | None) -> None:
        """标记prefill阶段结束（首个chunk到达时调用）

        Args:
            task_id: 事务ID
        """
        if not task_id:
            return

        with self._lock:
            entry = self._active.pop(task_id, None)

        if entry:
            entry["stop_event"].set()

        # 删除状态文件（webui/CLI下次读取即消失，前端自动移除prefill提示）
        try:
            filepath = _get_prefill_file(task_id)
            if filepath.exists():
                filepath.unlink()
        except Exception as e:
            logger.debug(f"[PrefillTracker] 删除状态文件失败(task_id={task_id}): {e}")

    def _track_loop(
        self,
        task_id: int,
        est_tokens: int,
        provider: str | None,
        compact_threshold: int | None,
        stop_event: threading.Event,
    ) -> None:
        """后台追踪线程：定期更新心跳+写状态文件+写输出进度行"""
        start_time = time.time()
        last_output_write = 0.0  # 0.0表示尚未写过输出进度行（首次达标即写）

        while not stop_event.is_set():
            stop_event.wait(PREFILL_HEARTBEAT_INTERVAL)
            if stop_event.is_set():
                break

            now = time.time()
            elapsed = now - start_time

            # 1. 更新事务心跳（防止heartbeat_monitor超时误杀）——核心功能，
            #    从prefill开始就持续刷新，与提示阈值无关
            try:
                from peng.task.database import TaskDatabase

                TaskDatabase().update_heartbeat(task_id)
            except Exception as e:
                logger.debug(f"[PrefillTracker] 更新心跳失败(task_id={task_id}): {e}")

            # 2. prefill未超过PREFILL_OUTPUT_MIN_ELAPSED秒不提示（短请求无感知，
            #    避免"已等待0秒"刷屏）——状态文件与输出进度行同阈值
            if elapsed < PREFILL_OUTPUT_MIN_ELAPSED:
                continue

            # 3. 写状态文件（webui/CLI读取显示百分比）
            percent = _estimate_prefill_percent(elapsed, est_tokens, compact_threshold or 0)
            self._write_status_file(
                task_id, est_tokens, provider, compact_threshold, elapsed=elapsed, percent=percent
            )

            # 4. 定期写输出文件进度行（CLI watch直接可见）——低频避免刷屏
            if now - last_output_write >= PREFILL_OUTPUT_INTERVAL:
                last_output_write = now
                self._write_output_progress(task_id, elapsed, percent)

    def _write_status_file(
        self,
        task_id: int,
        est_tokens: int,
        provider: str | None,
        compact_threshold: int | None,
        elapsed: float,
        percent: int,
    ) -> None:
        """将prefill状态写入跨进程共享文件（原子写入）

        写入格式:
        {
            "task_id": 事务ID,
            "timestamp": 当前时间戳,
            "start_time": prefill开始时间戳,
            "elapsed": 已耗时秒,
            "percent": 估算百分比(0-99),
            "est_tokens": 估算输入token数,
            "provider": provider名称,
            "compact_threshold": provider上下文上限token,
            "pid": 写入进程ID
        }
        """
        try:
            data = {
                "task_id": task_id,
                "timestamp": time.time(),
                "start_time": time.time() - elapsed,
                "elapsed": round(elapsed, 1),
                "percent": percent,
                "est_tokens": est_tokens,
                "provider": provider or "",
                "compact_threshold": compact_threshold or 0,
                "pid": os.getpid(),
            }

            filepath = _get_prefill_file(task_id)
            ensure_dir_with_owner(filepath.parent)

            # 原子写入：先写临时文件，再rename
            tmp_path = filepath.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(str(tmp_path), str(filepath))
            chown_to_project_owner(filepath)

        except Exception as e:
            logger.debug(f"[PrefillTracker] 写入状态文件失败(task_id={task_id}): {e}")

    def _write_output_progress(self, task_id: int, elapsed: float, percent: int) -> None:
        """向事务输出文件写入prefill进度行（CLI watch直接可见）"""
        try:
            from peng.task.output_manager import get_output_manager

            get_output_manager().write_output(
                task_id=task_id,
                source="system",
                content=(
                    f"⏳ LLM prefill中 {percent}%（已等待{int(elapsed)}秒，"
                    f"长上下文填充需要时间，首个输出即将开始）\n"
                ),
            )
        except Exception as e:
            logger.debug(f"[PrefillTracker] 写输出进度失败(task_id={task_id}): {e}")

    # ==================== webui/CLI进程侧（读） ====================

    def read_status(self, task_id: int) -> dict | None:
        """读取指定事务的prefill状态（CLI watch调用）

        Args:
            task_id: 事务ID

        Returns:
            状态字典；不存在、已过期或json损坏返回 None
        """
        try:
            filepath = _get_prefill_file(task_id)
            if not filepath.exists():
                return None
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 过期数据视为无效（写入进程崩溃残留）
            if time.time() - data.get("timestamp", 0) > DATA_EXPIRY:
                try:
                    filepath.unlink()
                except Exception:
                    pass
                return None
            return data
        except Exception as e:
            logger.debug(f"[PrefillTracker] 读取状态失败(task_id={task_id}): {e}")
            return None

    def get_snapshot(self) -> dict[int, dict]:
        """读取所有活跃事务的prefill状态快照（webui进程调用）

        合并读取所有未过期的prefill文件，按task_id分组返回。

        Returns:
            {task_id: {"percent": int, "elapsed": float, "provider": str, ...}}
            无活跃prefill时返回空dict
        """
        try:
            now = time.time()
            result: dict[int, dict] = {}
            for filepath in _get_all_prefill_files():
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    # 过期数据跳过（残留清理在runner进程atexit+过期删除处理）
                    if now - data.get("timestamp", 0) > DATA_EXPIRY:
                        continue
                    task_id = data.get("task_id")
                    if not task_id:
                        continue
                    result[int(task_id)] = {
                        "percent": int(data.get("percent", 0)),
                        "elapsed": float(data.get("elapsed", 0)),
                        "provider": data.get("provider", ""),
                        "est_tokens": int(data.get("est_tokens", 0)),
                    }
                except Exception:
                    continue
            return result
        except Exception as e:
            logger.debug(f"[PrefillTracker] 读取快照失败: {e}")
            return {}

    def cleanup(self) -> None:
        """清理过期残留文件（写入进程崩溃未删除的）"""
        try:
            now = time.time()
            data_dir = _get_data_dir()
            if not data_dir.exists():
                return
            for filepath in data_dir.glob(f"{_PREFILL_PREFIX}*.json"):
                try:
                    if now - filepath.stat().st_mtime > DATA_EXPIRY:
                        filepath.unlink()
                        logger.debug(f"[PrefillTracker] 已删除过期文件: {filepath.name}")
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"[PrefillTracker] 清理文件失败: {e}")


# 全局单例访问
_prefill_tracker: PrefillTracker | None = None


def get_prefill_tracker() -> PrefillTracker:
    """获取全局prefill追踪器单例"""
    global _prefill_tracker
    if _prefill_tracker is None:
        _prefill_tracker = PrefillTracker()
    return _prefill_tracker

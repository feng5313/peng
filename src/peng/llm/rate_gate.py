"""跨进程 LLM 限流闸门（事务#2689，消息#12863）

需求现场
--------
风主：glm 系列 provider 明明没有到额度限制，几个事务卡死无输出。
实测取证（logs/peng.log + 独立 curl）：智谱返回
``HTTP 429 {"code":"1302","message":"您的账户已达到速率限制，请您控制请求频率"}``，
响应头**没有** retry-after / x-ratelimit-reset —— 这是账户级 RPM 节流（速率限制），
不是配额耗尽。

旧实现的致命缺陷（活锁自锁）
--------------------------
每个事务在独立 runner 子进程中运行，429 临时限流走 tool_engine 的
"不冷却不切换，0.5秒后重试抢流量"路径：内层 ``_max_retry=30`` 固定 sleep(0.5)，
外层 ``if is_rate_limited: sleep(0.5); continue`` 无限重来。三个事务各自每 0.5 秒
向**同一账户**（z-glm5.3 与 z-glm5.3-flash 共用同一 api_key）发一次请求，被拒的
请求同样消耗账户的 RPM 窗口 ⇒ 每分钟数百个请求全部落在"注定被拒"的试探上，
谁都无法拿到成功响应 —— 事务永远卡死，且该路径只写 logger.warning、不写事务
输出文件，UI 上毫无动静（风主视角的"卡死无输出"）。

本模块方案：账户域共享的确定性节流闸门
--------------------------------------
1. **限流域（rate domain）** = ``sha1(base_url + api_key)``。同账户的不同模型
   （glm-5.3 / glm-5.3-flash）共享一个域 —— 它们共享同一个 RPM 池，分开计数
   必然各自打满账户配额。无 api_key/base_url 时退化为 provider 名。
2. **跨进程共享状态** ``data/rate_gate.json``（flock 独占锁 + 稳定 inode 锁文件，
   沿用 llm/concurrency.py 的关键教训：filelock unlink 换 inode 会破坏互斥）。
3. **请求排队（令牌间隔）**：发请求前在锁内领取一个"发送时刻槽位"——
   ``slot = max(now, throttled_until, next_slot)``，并推进 ``next_slot = slot + interval``。
   于是限流期间所有事务被排成单列、按 interval 逐个试探（不再并发抢流量），
   未限流时 ``interval=0`` ⇒ 不排队（并发仍由 llm/concurrency.py 的槽位约束）。
4. **指数退避**：连续被拒次数越多，静默窗口（throttled_until）与请求间隔越大；
   服务端声明了等待时长（retry-after/文案恢复时刻）则直接采用声明值。
5. **成功即清零**：收到响应首个 chunk 即视为配额可用（429 在响应头阶段抛出），
   清零退避状态恢复全速。
6. **持续限流升级**：累计节流时长超过阈值时返回 ``escalate=True``——调用方据此
   把该域 provider 持久化硬阻塞并切换其他账户 provider，绝不无限死磕。
7. **等待可见 + 可打断**：等待超过阈值向事务输出打印一次提示（含预计恢复时刻），
   等待期间刷新心跳；调用方可传入 interrupt_cb（紧急 info/控制信号）提前跳出。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ============ 核心参数 ============

#: 共享状态文件名（<project_root>/data/rate_gate.json）与锁文件后缀
GATE_FILE_NAME = "rate_gate.json"
_LOCK_SUFFIX = ".lock"

#: 首次被节流后的请求排队间隔（秒）——之后按连续被拒次数指数增长
RATE_GATE_BASE_INTERVAL_SECONDS = 1.0
#: 排队间隔上限（秒）：再高频也抢不到流量，上限防无限静默
RATE_GATE_MAX_INTERVAL_SECONDS = 30.0

#: 首次被节流的静默窗口（秒）。云端速率限制多为分钟级滑窗，起步3秒即可
#: 显著削减被拒请求数（旧实现0.5秒高频重试是活锁根因）
RATE_GATE_BASE_WINDOW_SECONDS = 3.0
#: 静默窗口上限（秒）
RATE_GATE_MAX_WINDOW_SECONDS = 120.0

#: 累计节流时长超过此值（秒）→ escalate=True，调用方升级硬阻塞/切换其他账户
RATE_GATE_ESCALATE_AFTER_SECONDS = 180.0

#: 等待超过该时长向事务输出打印一次可见提示（秒）
RATE_GATE_NOTICE_SECONDS = 5.0
#: 等待期间刷新事务心跳的间隔（秒），远小于默认心跳超时（300s）
RATE_GATE_HEARTBEAT_INTERVAL = 30.0

#: 状态陈旧度：超过此时长（秒）无新事件的节流状态视为失效（自愈，防陈旧阻塞）
RATE_GATE_STALE_SECONDS = 600.0

# 文件锁被占用/权限错误的 errno（视为"他人正在写"，重试而非绕过闸门）
_BUSY_ERRNOS = {11, 13}  # EAGAIN/EWOULDBLOCK, EACCES
_LOCK_RETRY_SECONDS = 2.0
_LOCK_RETRY_INTERVAL = 0.02

#: 单次写入的进程内节流（避免同进程高频写盘）
_WRITE_MIN_INTERVAL_SECONDS = 0.05


def _flock_exclusive(fd: int) -> None:
    if os.name == "nt":  # pragma: cover Windows
        import msvcrt

        _msvcrt: Any = msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _funlock(fd: int) -> None:
    if os.name == "nt":  # pragma: cover Windows
        import msvcrt

        _msvcrt: Any = msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class _FileLock:
    """flock 独占锁（锁文件永不删除 ⇒ inode 稳定，跨进程互斥可靠）"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        deadline = time.time() + _LOCK_RETRY_SECONDS
        while True:
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o666)
            try:
                _flock_exclusive(fd)
                self._fd = fd
                return True
            except OSError as e:
                os.close(fd)
                if e.errno not in _BUSY_ERRNOS:
                    logger.debug(f"[RateGate] 文件锁异常(忽略): {self.path}: {e}")
                    return False
                if time.time() >= deadline:
                    return False
                time.sleep(_LOCK_RETRY_INTERVAL)

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            _funlock(self._fd)
        except Exception:
            pass
        os.close(self._fd)
        self._fd = None


def rate_domain_key(provider_data: dict[str, Any] | None) -> str:
    """限流域 key：同 api_key+base_url 的 provider 共享一个闸门（同账户共享RPM）

    provider_data 需含 base_url/api_key（chat_stream 的 get_provider 返回值）。
    缺失时退化为 provider 名（各自独立闸门）。
    """
    if not provider_data:
        return ""
    base = str(provider_data.get("base_url") or "")
    key = str(provider_data.get("api_key") or "")
    if not base and not key:
        return str(provider_data.get("provider") or "")
    raw = f"{base}|{key}"
    return f"{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]}"


class RateGate:
    """跨进程限流闸门：按限流域记录节流窗口与请求排队槽位"""

    _instance: RateGate | None = None

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self._pid = os.getpid()
        self._data_dir_override = Path(data_dir) if data_dir else None
        self._last_write = 0.0

    # ---------------- 路径 ----------------

    def _data_dir(self) -> Path:
        if self._data_dir_override is not None:
            return self._data_dir_override
        try:
            from peng.config import get_project_root

            return get_project_root() / "data"
        except Exception:  # pragma: no cover - 极端环境兜底
            return Path("data")

    def gate_path(self) -> Path:
        return self._data_dir() / GATE_FILE_NAME

    def lock_path(self) -> Path:
        return self._data_dir() / (GATE_FILE_NAME + _LOCK_SUFFIX)

    # ---------------- 状态读写（flock保护，原子替换） ----------------

    def _read_state(self) -> dict[str, Any]:
        path = self.gate_path()
        try:
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict) and isinstance(loaded.get("domains"), dict):
                    return loaded
        except Exception as e:
            logger.debug(f"[RateGate] 状态读取失败(视为空): {e}")
        return {"domains": {}}

    def _write_state(self, data: dict[str, Any]) -> None:
        path = self.gate_path()
        try:
            tmp = path.with_name(path.name + f".tmp{self._pid}")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(str(tmp), str(path))
            try:
                from peng.common.paths import chown_to_project_owner

                chown_to_project_owner(path)
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"[RateGate] 状态写入失败(忽略): {e}")

    def _domain_entry(self, data: dict[str, Any], domain: str, now: float) -> dict[str, Any]:
        """取出域节流状态；陈旧状态（超过STALE无新事件）视为已失效并清除"""
        domains = data.setdefault("domains", {})
        entry = domains.get(domain) or {}
        last = float(entry.get("updated_at", 0.0) or 0.0)
        if entry and now - last > RATE_GATE_STALE_SECONDS:
            domains[domain] = {}
            return {}
        return entry

    def _with_lock(self, mutate: Callable[[dict[str, Any]], Any]) -> Any:
        """锁内读-改-写。返回 mutate 的结果；拿不到锁时返回 None（不静默绕过）"""
        lock = _FileLock(self.lock_path())
        if not lock.acquire():
            logger.debug(f"[RateGate] 获取闸门锁失败(跳过本次更新): {domain_hint(self)}")
            return None
        try:
            data = self._read_state()
            result = mutate(data)
            self._write_state(data)
            return result
        except Exception as e:
            logger.debug(f"[RateGate] 闸门状态更新失败(忽略): {e}")
            return None
        finally:
            lock.release()

    # ---------------- 记录 ----------------

    def record_throttle(
        self, domain: str, reason: str = "", declared_wait: float | None = None, now: float | None = None
    ) -> dict[str, Any]:
        """登记一次被节流（429速率限制），返回最新闸门状态

        Args:
            domain: 限流域 key
            reason: 原因文案（诊断/UI展示）
            declared_wait: 服务端声明的等待秒数（retry-after/恢复时刻），有则直接采用
            now: 当前时间（测试可注入）

        Returns:
            {"interval": 请求排队间隔, "wait": 距静默窗口结束的秒数,
             "count": 连续被拒次数, "until": 窗口截止epoch,
             "throttled_total": 累计节流时长, "escalate": 是否应升级}
        """
        if not domain:
            return {"interval": 0.0, "wait": 0.0, "count": 0, "until": 0.0,
                    "throttled_total": 0.0, "escalate": False}
        now = now if now is not None else time.time()

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            entry = self._domain_entry(data, domain, now)
            count = int(entry.get("count", 0) or 0) + 1
            first_at = float(entry.get("first_throttle_at", 0.0) or 0.0) or now
            step = count - 1
            window = min(RATE_GATE_BASE_WINDOW_SECONDS * (2**step), RATE_GATE_MAX_WINDOW_SECONDS)
            if declared_wait and declared_wait > 0:
                # 服务端声明优先（权威），但不超过上限，避免一次死等数小时
                window = min(max(declared_wait, 1.0), RATE_GATE_MAX_WINDOW_SECONDS)
            interval = min(RATE_GATE_BASE_INTERVAL_SECONDS * (2**step), RATE_GATE_MAX_INTERVAL_SECONDS)
            until = now + window
            entry.update(
                {
                    "count": count,
                    "first_throttle_at": first_at,
                    "throttled_until": until,
                    "interval": interval,
                    # 排队槽位起点不得早于窗口：窗口内不允许任何请求穿过
                    "next_slot": max(float(entry.get("next_slot", 0.0) or 0.0), until),
                    "reason": reason[:200],
                    "updated_at": now,
                }
            )
            data["domains"][domain] = entry
            total = now - first_at
            return {
                "interval": interval,
                "wait": max(0.0, until - now),
                "count": count,
                "until": until,
                "throttled_total": total,
                "escalate": total >= RATE_GATE_ESCALATE_AFTER_SECONDS,
            }

        result = self._with_lock(_mutate)
        if result is None:  # 锁不可用：保守按基础间隔退避（不放行高频试探）
            return {
                "interval": RATE_GATE_BASE_INTERVAL_SECONDS,
                "wait": RATE_GATE_BASE_WINDOW_SECONDS,
                "count": 0,
                "until": now + RATE_GATE_BASE_WINDOW_SECONDS,
                "throttled_total": 0.0,
                "escalate": False,
            }
        return result

    def record_success(self, domain: str, now: float | None = None) -> None:
        """请求被服务端接受（拿到响应）⇒ 配额可用，清零退避状态恢复全速"""
        if not domain:
            return
        now = now if now is not None else time.time()

        def _mutate(data: dict[str, Any]) -> None:
            data["domains"][domain] = {"count": 0, "updated_at": now}

        self._with_lock(_mutate)

    # ---------------- 领取发送槽位 ----------------

    def claim_send_slot(self, domain: str, now: float | None = None) -> tuple[float, float]:
        """领取本域的"发送时刻"槽位：返回 (slot_epoch, 需等待秒数)

        锁内推进 next_slot ⇒ 跨进程串行排队（限流期间按 interval 逐个试探）。
        未限流（interval=0）时 slot=now ⇒ 不排队。
        """
        if not domain:
            return (now or time.time(), 0.0)
        now = now if now is not None else time.time()

        def _mutate(data: dict[str, Any]) -> tuple[float, float]:
            entry = self._domain_entry(data, domain, now)
            interval = float(entry.get("interval", 0.0) or 0.0)
            until = float(entry.get("throttled_until", 0.0) or 0.0)
            next_slot = float(entry.get("next_slot", 0.0) or 0.0)
            slot = max(now, until, next_slot)
            if interval > 0:
                entry["next_slot"] = slot + interval
                entry["updated_at"] = now
                data["domains"][domain] = entry
            return (slot, max(0.0, slot - now))

        got = self._with_lock(_mutate)
        if got is None:  # 锁不可用：不阻塞放行（并发另有槽位约束）
            return (now, 0.0)
        return got

    def throttle_state(self, domain: str, now: float | None = None) -> dict[str, Any]:
        """只读查询节流状态（诊断/提示文案用）"""
        now = now if now is not None else time.time()
        entry = self._domain_entry(self._read_state(), domain, now)
        until = float(entry.get("throttled_until", 0.0) or 0.0)
        return {
            "count": int(entry.get("count", 0) or 0),
            "interval": float(entry.get("interval", 0.0) or 0.0),
            "until": until,
            "wait": max(0.0, until - now),
            "reason": str(entry.get("reason", "") or ""),
        }

    def snapshot(self, now: float | None = None) -> dict[str, dict[str, Any]]:
        """全部域的节流状态快照（诊断/UI）"""
        now = now if now is not None else time.time()
        data = self._read_state()
        out: dict[str, dict[str, Any]] = {}
        for domain in data.get("domains", {}):
            out[domain] = self.throttle_state(domain, now)
        return out

    # ---------------- 异步等待 ----------------

    async def acquire(
        self,
        domain: str,
        *,
        task_id: int | None = None,
        heartbeat_cb: Callable[[], None] | None = None,
        notice_cb: Callable[[float, str], None] | None = None,
        interrupt_cb: Callable[[], bool] | None = None,
    ) -> float:
        """发请求前领取发送槽位并等待到槽位时刻（限流期间排队）

        Returns:
            实际等待秒数（0=立即可发）
        """
        slot, wait = self.claim_send_slot(domain)
        if wait <= 0:
            return 0.0
        st = self.throttle_state(domain)
        logger.info(
            f"[RateGate] 事务#{task_id}限流域排队等待{wait:.1f}s"
            f"(连续被拒{st['count']}次，窗口至{time.strftime('%H:%M:%S', time.localtime(st['until']))})"
        )
        start = time.monotonic()
        last_heartbeat = start
        noticed = False
        deadline = slot
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            if interrupt_cb is not None:
                try:
                    if interrupt_cb():
                        logger.info(f"[RateGate] 事务#{task_id}限流等待被中断（提前放行）")
                        break
                except Exception as e:
                    logger.debug(f"[RateGate] 中断检测失败(忽略): {e}")
            now_mono = time.monotonic()
            if heartbeat_cb is not None and now_mono - last_heartbeat >= RATE_GATE_HEARTBEAT_INTERVAL:
                last_heartbeat = now_mono
                try:
                    await asyncio.to_thread(heartbeat_cb)
                except Exception as e:
                    logger.debug(f"[RateGate] 心跳刷新失败(忽略): {e}")
            if not noticed and notice_cb is not None and now_mono - start >= RATE_GATE_NOTICE_SECONDS:
                noticed = True
                try:
                    notice_cb(remaining, st.get("reason", ""))
                except Exception as e:
                    logger.debug(f"[RateGate] 等待提示失败(忽略): {e}")
            await asyncio.sleep(min(1.0, remaining) + random.uniform(0.0, 0.25))
        return time.monotonic() - start


def domain_hint(gate: RateGate) -> str:
    """日志用：当前闸门状态概览（锁失败时辅助定位）"""
    try:
        snap = gate.snapshot()
        return " | ".join(
            f"{d}(拒{n['count']}次,余{n['wait']:.0f}s)" for d, n in sorted(snap.items())
        ) or "无节流域"
    except Exception:
        return "?"


def get_rate_gate(data_dir: Path | str | None = None) -> RateGate:
    """全局闸门单例（同一安装目录内所有 runner 子进程共享状态文件）"""
    global _gate_instance
    if _gate_instance is None or (data_dir is not None and _gate_instance._data_dir_override != Path(data_dir)):
        _gate_instance = RateGate(data_dir)
    return _gate_instance


_gate_instance: RateGate | None = None

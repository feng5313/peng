"""
Provider 分配账本与同优先级轮换选择器（事务#2307）

需求（消息#10863）
----------------
优先级相同的多个 provider 应该**轮流使用**：restart/start 新启动的事务优先选择
**比较空闲**的 provider；resume 继续运行的事务默认**不换** provider（除非不可用）。

为什么必须跨进程
----------------
peng 的每个事务都在独立子进程中执行（runner.py → executor → PengAgent →
GLMClient），配置对象 ``LLMConfig.current_provider`` 与各 client 的
``self.provider`` 都是**进程私有**状态。旧实现在同优先级 tier 内永远取
shifts 的第一个（dict 插入顺序），因此多个同优先级 provider 时新事务全部
压到同一个最"忙"的 provider 上，其余同优先级 provider 空转。

设计：每进程私有账本 + 读方聚合（无共享写争用）
--------------------------------------------
- 每个进程只写自己的账本文件 ``data/provider_balance/p{pid}.json``
  （与 throughput_tracker 同构：写方单一 ⇒ 无需文件锁、无互相覆盖）；
- 选择器聚合所有进程的账本，得到每个 provider 的：
  - ``active``：活跃分配数（= 该 provider 上活跃事务数）
  - ``last_assigned``：最近一次**被分配**的时刻（LRU 轮转依据）
- 排序键（同 priority tier 内）：``(active 升序, last_assigned 升序, 名称)``
  - 从未分配过的 provider ``last_assigned=0`` 排最前（新部署时自然轮流铺开）
  - LRU 让"都空闲"时的选择自动轮转，等价于 round-robin
- 租约 TTL：分配条目 ``updated_at`` 超过 ``ASSIGN_TTL`` 秒视为失效（事务
  归档/结束不再刷新，自动掉出账本）；进程死亡（``process_exists`` False）
  时整个文件忽略；进程退出时 ``atexit`` 删除自己的文件。

与"粘性 provider"的关系
----------------------
resume（keep_context=True）保留 DB ``task.provider`` 作为粘性记录，进程重启后
首轮选择直接复用它（``get_provider`` 的"没有出错就不切换"原则）；只有
restart/start（清空上下文）才重新按本模块的空闲度/轮换规则分配。本模块只在
**自动选择**路径记录分配，不覆盖用户显式 @provider 意图。
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ============ 核心参数 ============

#: 分配租约存活时间（秒）：超过则条目失效。事务运行期每轮 chat_stream 都会
#: 刷新（touch），归档/结束的事务停止刷新后自动掉出账本；进程崩溃时账本文件
#: 由 atexit（正常退出）或删除检测兜底。
ASSIGN_TTL = 600.0

#: 账本文件最大存活时间（秒）：mtime 超过此值直接删除（进程死亡后的兜底清理）
FILE_TTL = 1800.0

#: 聚合快照缓存（秒）：避免每轮迭代高频读盘（同进程多次选择复用）
SNAPSHOT_CACHE_TTL = 2.0

#: 本进程账本落盘最小间隔（秒）：节流高频写（同进程内字典即时生效，不受影响）
MIN_WRITE_INTERVAL = 1.0

_DIR_NAME = "provider_balance"
_FILE_PREFIX = "p"


def _get_data_dir() -> Path:
    """账本目录（<project_root>/data/provider_balance）"""
    try:
        from peng.config import get_project_root

        return get_project_root() / "data" / _DIR_NAME
    except Exception:
        return Path("data") / _DIR_NAME


def _sanitize_pid(pid: int) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]", "_", str(pid))


class ProviderBalance:
    """Provider 分配账本（同优先级轮换 + 空闲度排序）

    用法::

        balance = get_provider_balance()
        ranked = balance.rank(candidates, task_id=task_id)   # tier内按空闲度排序
        balance.record(provider, task_id)                    # 自动分配命中后登记
        balance.release(task_id)                             # 事务结束时释放
    """

    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> ProviderBalance:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self._lock = threading.Lock()
        self._pid = os.getpid()
        # 本进程账本（内存真值，落盘供其他进程读取）
        # task_id(str) -> {"provider": str, "assigned_at": float, "updated_at": float}
        self._self_entries: dict[str, dict[str, Any]] = {}
        self._last_write = 0.0
        # 聚合快照缓存：{"built_at": float, "others": [...]}
        self._cache: dict[str, Any] | None = None

    # ---------------- 写入（仅本进程） ----------------

    def _self_file(self) -> Path:
        return _get_data_dir() / f"{_FILE_PREFIX}{_sanitize_pid(self._pid)}.json"

    def record(self, provider: str, task_id: int | None = None) -> None:
        """登记/刷新本进程的分配（自动选择命中时调用）

        - 新登记：``assigned_at=now``（LRU 时间戳，决定同 tier 内的轮转顺序）
        - 已登记：只刷新 ``updated_at``（续租），不移动 ``assigned_at``
          （否则最忙的 provider 会因为频繁续租而永远排最前）
        """
        if not provider:
            return
        key = str(task_id) if task_id is not None else f"pid{self._pid}"
        now = time.time()
        with self._lock:
            entry = self._self_entries.get(key)
            if entry is None or entry.get("provider") != provider:
                self._self_entries[key] = {
                    "provider": provider,
                    "assigned_at": now,
                    "updated_at": now,
                }
            else:
                entry["updated_at"] = now
            should_write = (now - self._last_write) >= MIN_WRITE_INTERVAL
            if should_write:
                self._last_write = now
        if should_write:
            self._dump_self()

    def release(self, task_id: int | None = None) -> None:
        """释放本进程的某个分配（事务结束/归档时调用）"""
        key = str(task_id) if task_id is not None else f"pid{self._pid}"
        with self._lock:
            if self._self_entries.pop(key, None) is None:
                return
        self._dump_self(force=True)

    def _dump_self(self, force: bool = False) -> None:
        """原子写本进程账本文件（tmp + os.replace）"""
        try:
            with self._lock:
                data = {
                    "pid": self._pid,
                    "updated_at": time.time(),
                    "assignments": dict(self._self_entries),
                }
            if not force and not data["assignments"]:
                return
            from pathlib import Path

            data_dir = _get_data_dir()
            if not data_dir.exists():
                try:
                    from peng.common.paths import ensure_dir_with_owner

                    ensure_dir_with_owner(data_dir)
                except Exception:
                    data_dir.mkdir(parents=True, exist_ok=True)
            filepath = Path(data_dir) / f"{_FILE_PREFIX}{_sanitize_pid(self._pid)}.json"
            tmp = filepath.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(str(tmp), str(filepath))
            try:
                from peng.common.paths import chown_to_project_owner

                chown_to_project_owner(filepath)
            except Exception:
                pass
        except Exception as e:  # pragma: no cover - 账本失败不影响主流程
            logger.debug(f"[ProviderBalance] 写入账本失败: {e}")

    # ---------------- 聚合读取 ----------------

    def _iter_other_files(self) -> Iterator[Path]:
        try:
            data_dir = _get_data_dir()
            if not data_dir.exists():
                return
            for filepath in data_dir.glob(f"{_FILE_PREFIX}*.json"):
                if filepath.name == f"{_FILE_PREFIX}{_sanitize_pid(self._pid)}.json":
                    continue  # 本进程用内存真值
                yield filepath
        except Exception as e:  # pragma: no cover
            logger.debug(f"[ProviderBalance] 扫描账本目录失败: {e}")

    def _aggregate(self, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        """聚合所有进程的账本 → {provider: {"active": int, "last_assigned": float}}

        缓存只覆盖**其他进程文件**部分（IO 昂贵）；本进程账本（内存真值）每次
        实时合并——否则同进程连续分配时看不到刚登记的条目，轮转不生效。
        """
        now = time.time()
        cache = self._cache
        if not (use_cache and cache and now - cache.get("built_at", 0) < SNAPSHOT_CACHE_TTL):
            cache = {"built_at": now, "others": self._scan_other_entries()}
            self._cache = cache

        load: dict[str, dict[str, Any]] = {}

        def _add(provider: str, assigned_at: float, updated_at: float) -> None:
            if not provider or now - updated_at > ASSIGN_TTL:
                return
            slot = load.setdefault(provider, {"active": 0, "last_assigned": 0.0})
            slot["active"] += 1
            if assigned_at > slot["last_assigned"]:
                slot["last_assigned"] = float(assigned_at)

        # 其他进程（缓存的条目，按当前时间重新过滤TTL）
        for provider, assigned_at, updated_at in cache["others"]:
            _add(provider, assigned_at, updated_at)

        # 本进程（内存真值，实时）
        with self._lock:
            self_snapshot = [dict(v) for v in self._self_entries.values()]
        for entry in self_snapshot:
            _add(entry.get("provider", ""), entry.get("assigned_at", 0), entry.get("updated_at", 0))

        return load

    def _scan_other_entries(self) -> list[tuple[str, float, float]]:
        """扫描其他进程账本文件 → [(provider, assigned_at, updated_at), ...]"""
        from pathlib import Path

        from peng.common.platform import process_exists

        now = time.time()
        entries: list[tuple[str, float, float]] = []
        for filepath in self._iter_other_files():
            try:
                p = Path(filepath)
                if now - p.stat().st_mtime > FILE_TTL:
                    self._unlink_quietly(p)
                    continue
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    continue
                pid = data.get("pid")
                if pid and not process_exists(int(pid)):
                    self._unlink_quietly(p)
                    continue
                assignments = data.get("assignments") or {}
                if not isinstance(assignments, dict):
                    continue
                for entry in assignments.values():
                    if isinstance(entry, dict):
                        entries.append(
                            (
                                entry.get("provider", ""),
                                float(entry.get("assigned_at", 0) or 0),
                                float(entry.get("updated_at", 0) or 0),
                            )
                        )
            except Exception:
                continue
        return entries

    @staticmethod
    def _unlink_quietly(path: Path) -> None:
        try:
            path.unlink()
        except Exception:
            pass

    def get_load(self, provider: str) -> dict[str, Any]:
        """单个 provider 的负载视图 {"active": int, "last_assigned": float}"""
        return self._aggregate().get(provider, {"active": 0, "last_assigned": 0.0})

    # ---------------- 排序 ----------------

    def rank(self, candidates: list[str], task_id: int | None = None) -> list[str]:
        """同优先级 tier 内按空闲度排序（调用方已按 priority 分好组）

        排序键：活跃分配数升序 → 最近分配时刻升序（LRU，从未分配=0最优先）
        → 名称字典序（稳定 tie-break）。

        Args:
            candidates: 同一优先级 tier 内的 provider 名称列表
            task_id: 本事务 id。若本进程已把某 provider 分配给自己，
                该 provider 排最前（同事务重选时保持稳定，避免来回抖动）
        """
        if not candidates:
            return []
        if len(candidates) == 1:
            return list(candidates)

        load = self._aggregate()
        mine: str | None = None
        if task_id is not None:
            with self._lock:
                entry = self._self_entries.get(str(task_id))
            if entry:
                mine = entry.get("provider")

        def key(name: str) -> tuple[int, int, float, str]:
            info = load.get(name, {"active": 0, "last_assigned": 0.0})
            return (0 if name == mine else 1, info["active"], info["last_assigned"], name)

        return sorted(candidates, key=key)

    # ---------------- 清理 ----------------

    def cleanup(self) -> None:
        """删除过期/死进程账本文件（供周期性清理调用）"""
        from pathlib import Path

        now = time.time()
        from peng.common.platform import process_exists

        for filepath in self._iter_other_files():
            try:
                p = Path(filepath)
                pid_str = p.stem[len(_FILE_PREFIX) :]
                if not pid_str.isdigit():
                    continue
                if not process_exists(int(pid_str)):
                    self._unlink_quietly(p)
                    continue
                if now - p.stat().st_mtime > FILE_TTL:
                    self._unlink_quietly(p)
            except Exception:
                continue

    def snapshot_summary(self) -> dict[str, dict[str, Any]]:
        """调试/展示用：{provider: {"active": n, "last_assigned": ts}}（只含非零项）"""
        return {k: v for k, v in self._aggregate(use_cache=False).items() if v["active"] > 0}


# ============ 全局单例 ============

_balance: ProviderBalance | None = None


def _cleanup_on_exit() -> None:
    """进程退出时删除本进程账本文件"""
    try:
        balance = get_provider_balance()
        with balance._lock:
            entries = dict(balance._self_entries)
        if not entries:
            return
        path = balance._self_file()
        if path.exists():
            path.unlink()
            logger.debug(f"[ProviderBalance] 进程退出，已删除账本: {path.name}")
    except Exception:
        pass


def get_provider_balance() -> ProviderBalance:
    """获取全局分配账本单例"""
    global _balance
    if _balance is None:
        _balance = ProviderBalance()
        atexit.register(_cleanup_on_exit)
    return _balance

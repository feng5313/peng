"""
Provider 空闲度账本与同优先级轮换选择器（事务#2307，重构版）

需求（消息#10863，风主强调）
--------------------------
优先级相同的多个 provider 应该**轮流使用**：restart/start 新启动的事务，**优先
选择比较空闲的provider**；resume 继续运行的事务默认不换 provider，除非不可用。

为什么必须查 DB（风主指示，根本性方案）
------------------------------------
每个事务在独立 runner 子进程运行，各进程的配置/客户端状态私有。第一版用
"每进程私有账本文件+读方聚合"，实测暴露致命缺陷：**只有自动选择路径会登记**，
显式 @provider 强制指定、锁定、粘性（resume）路径都不登记 → 账本常年为空 →
空闲度永远 0:0 → 退化为字典序固定选第一个 → 所有事务挤在同一个 provider。

DB ``tasks`` 表才是跨进程天然真值：``chat_stream`` 每轮都把实际使用的 provider
写回 ``task.provider``，事务状态（running/accepting）由 DB 权威记录——**任何
路径**（强制指定/锁定/粘性/自动）选定 provider 后都会体现在 DB 里。因此空闲度
主信号 = DB 聚合各 provider 上的活跃事务数（风主场景实测：iq3=2、wen=1 →
新事务应选 wen）。

LRU 轮换指针（同空闲度时轮流）
-----------------------------
活跃数相同时必须真正"轮流"，因此需要跨进程共享的最近分配时刻：单一共享账本
``data/provider_balance.json``，由 flock 独占锁保护（锁文件 inode 稳定永不删除，
沿用 llm/concurrency.py 的关键教训——filelock unlink 换 inode 会破坏互斥）。
记录时机：**新分配**（restart/start/首次自动选择）才推进 LRU；粘性复用不推进
（它不改变归属）。

排序键（同 priority tier 内；tier 间仍按 priority 降序）
------------------------------------------------------
``(DB活跃事务数↑, last_assigned↑[从未分配=0最优先], 名称)``

DB 不可用时降级为纯 LRU 账本排序（保持轮换语义）。

历史教训（实测风主场景后修订）：曾尝试"扣除自己+已分配排最前"的稳定性优化，
结果 current=iq3(2-1=1) 与 wen(1) 打平退化为名字序 → 新事务永远选 iq3，与需求
"优先选择比较空闲的provider"直接冲突。稳定性本就由 get_provider"没有出错就不
切换"原则在 rank 之前保证（rank 只在真正需要新分配/切换时介入），故排序键必须
是纯原始负载。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ============ 核心参数 ============

#: DB 活跃事务数聚合缓存（秒）：provider 选择每事务每迭代都可能发生，
#: 缓存窗口内复用，避免高频查询打DB（本地PG单查询<1ms，窗口小即可）
DB_CACHE_TTL = 3.0

#: LRU 账本读取缓存（秒），同上
LEDGER_CACHE_TTL = 2.0

#: 跨进程文件锁获取重试
LOCK_RETRY_SECONDS = 1.0
LOCK_RETRY_INTERVAL = 0.02

_LEDGER_NAME = "provider_balance.json"
_LOCK_SUFFIX = ".lock"

#: 参与"活跃负载"统计的事务状态（paused/归档的事务不消耗provider算力）
ACTIVE_STATUSES = ("running", "accepting")


def _get_data_dir() -> Path:
    """数据目录（<project_root>/data）"""
    try:
        from peng.config import get_project_root

        return get_project_root() / "data"
    except Exception:
        return Path("data")


def _ledger_path() -> Path:
    return _get_data_dir() / _LEDGER_NAME


def _lock_path() -> Path:
    return _get_data_dir() / (_LEDGER_NAME + _LOCK_SUFFIX)


# ============ 跨进程文件锁（稳定inode，参考 llm/concurrency.py） ============

if os.name == "nt":  # pragma: no cover - Windows
    import msvcrt

    # msvcrt锁定API只存在于Windows平台typeshed桩（其他平台空桩），显式按Any使用
    _msvcrt: Any = msvcrt

    def _flock_exclusive(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)

    def _funlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)

else:

    def _flock_exclusive(fd: int) -> None:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _funlock(fd: int) -> None:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class ProviderBalance:
    """Provider 空闲度账本（DB活跃聚合 + 共享LRU轮换）

    用法::

        balance = get_provider_balance()
        ranked = balance.rank(candidates, task_id=tid, current=cur)
        balance.record_assignment(provider, task_id=tid)   # 新分配时推进LRU
    """

    _instance = None

    def __init__(self) -> None:
        self._pid = os.getpid()
        self._db_cache: dict[str, int] | None = None
        self._db_cache_built = 0.0
        self._ledger_cache: dict[str, Any] | None = None
        self._ledger_cache_built = 0.0

    # ---------------- 跨进程锁 ----------------

    class _FileLock:
        """flock 独占锁（锁文件永不删除 ⇒ inode 稳定，互斥语义可靠）"""

        def __init__(self, path: Path) -> None:
            self.path = path
            self._fd: int | None = None

        def acquire(self) -> bool:
            deadline = time.time() + LOCK_RETRY_SECONDS
            while True:
                fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o666)
                try:
                    _flock_exclusive(fd)
                    self._fd = fd
                    return True
                except OSError as e:
                    os.close(fd)
                    if e.errno not in (getattr(__import__("errno"), "EWOULDBLOCK", 11), 13):
                        logger.debug(f"[ProviderBalance] 文件锁异常: {self.path}: {e}")
                        return False
                    if time.time() >= deadline:
                        return False
                    time.sleep(LOCK_RETRY_INTERVAL)

        def release(self) -> None:
            if self._fd is None:
                return
            try:
                _funlock(self._fd)
            except Exception:
                pass
            os.close(self._fd)
            self._fd = None

    # ---------------- DB 活跃事务数（跨进程真值，主信号） ----------------

    def active_counts(self, use_cache: bool = True) -> dict[str, int]:
        """各 provider 上的活跃事务数（DB tasks 聚合）

        ``chat_stream`` 每轮把实际 provider 写回 task.provider，状态由DB权威记录，
        因此该聚合天然涵盖强制指定/锁定/粘性/自动选择**所有路径**（第一版账本
        只覆盖自动选择路径，正是"完全无效"的根因）。

        Returns:
            {provider: 活跃事务数}；DB 不可用时返回 {}（降级为纯LRU轮换）
        """
        now = time.time()
        if use_cache and self._db_cache is not None and now - self._db_cache_built < DB_CACHE_TTL:
            return self._db_cache

        counts: dict[str, int] = {}
        try:
            from peng.task.database import TaskDatabase

            placeholders = ", ".join(["%s"] * len(ACTIVE_STATUSES))
            # 不用ORM/自研适配层，直接聚合（列名来自固定白名单，无注入面）
            with TaskDatabase()._get_connection() as conn:
                cursor = conn.execute(
                    f"SELECT provider, COUNT(*) AS n FROM tasks "
                    f"WHERE status IN ({placeholders}) AND provider <> '' "
                    f"GROUP BY provider",
                    tuple(ACTIVE_STATUSES),
                )
                for raw in cursor.fetchall():
                    row = dict(raw)  # 项目DB层返回RealDictRow（与既有查询风格一致）
                    name = row.get("provider")
                    if name:
                        counts[str(name)] = int(row.get("n") or 0)
        except Exception as e:
            logger.debug(f"[ProviderBalance] DB活跃事务聚合失败(降级纯LRU): {e}")
            counts = {}

        self._db_cache = counts
        self._db_cache_built = now
        return counts

    # ---------------- LRU 共享账本（同空闲度时的轮换依据） ----------------

    def _load_ledger(self, use_cache: bool = True) -> dict[str, Any]:
        """读取共享 LRU 账本 {providers: {name: {"last_assigned": ts}}}"""
        now = time.time()
        if (
            use_cache
            and self._ledger_cache is not None
            and now - self._ledger_cache_built < LEDGER_CACHE_TTL
        ):
            return self._ledger_cache

        data: dict[str, Any] = {"providers": {}}
        path = _ledger_path()
        try:
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict) and isinstance(loaded.get("providers"), dict):
                    data = loaded
        except Exception as e:
            logger.debug(f"[ProviderBalance] 账本读取失败(忽略): {e}")

        self._ledger_cache = data
        self._ledger_cache_built = now
        return data

    def last_assigned(self, provider: str) -> float:
        """该 provider 最近一次被新分配的时刻（0=从未，LRU最优先）"""
        entry = self._load_ledger().get("providers", {}).get(provider) or {}
        return float(entry.get("last_assigned", 0) or 0)

    def record_assignment(self, provider: str, task_id: int | None = None) -> None:
        """新分配命中时推进该 provider 的 LRU 时刻（跨进程共享，flock保护）

        只对"新分配"调用（restart/start/首次自动选择）；粘性复用不推进——它不
        改变归属，推进会让刚被 resume 的 provider 永远排到 tier 末尾。
        """
        if not provider:
            return
        lock = self._FileLock(_lock_path())
        if not lock.acquire():
            logger.debug("[ProviderBalance] 获取账本锁失败，跳过LRU推进")
            return
        try:
            data = self._load_ledger(use_cache=False)
            providers = data.setdefault("providers", {})
            entry = providers.setdefault(provider, {})
            entry["last_assigned"] = time.time()
            if task_id is not None:
                entry.setdefault("by", {})[str(task_id)] = self._pid
            self._write_ledger(data)
        except Exception as e:
            logger.debug(f"[ProviderBalance] LRU推进失败(忽略): {e}")
        finally:
            lock.release()

    def _write_ledger(self, data: dict[str, Any]) -> None:
        """原子写共享账本（tmp + os.replace，同目录同 inode）"""
        path = _ledger_path()
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
            self._ledger_cache = data
            self._ledger_cache_built = time.time()
        except Exception as e:
            logger.debug(f"[ProviderBalance] 账本写入失败(忽略): {e}")

    # ---------------- 排序 ----------------

    def rank(self, candidates: list[str], task_id: int | None = None) -> list[str]:
        """同优先级 tier 内排序：DB活跃事务数↑ → LRU↑（从未=0最优先）→ 名称

        Args:
            candidates: 同一优先级 tier 的 provider（调用方已分组）
            task_id: 本事务 id（记录进账本 by，供审计）
        """
        if not candidates:
            return []
        if len(candidates) == 1:
            return list(candidates)

        counts = self.active_counts()

        def key(name: str) -> tuple[int, float, str]:
            # 需求"优先选择比较空闲的provider"=直接按DB活跃数排（实测教训：曾加
            # "扣除自己"优化，结果风主场景下 current=iq3(2-1=1) 与 wen(1) 打平退
            # 化名字序永远选iq3——风主骂对了。稳定性由get_provider"没出错不切换"
            # 原则在rank之前保证，rank只在真正需要换/新分配时介入，无需扣除。）
            return (counts.get(name, 0), self.last_assigned(name), name)

        ranked = sorted(candidates, key=key)
        if len(set(key(c) for c in candidates)) > 1:
            logger.info(
                "[ProviderBalance] 同优先级轮换排序: "
                + " | ".join(f"{c}(活跃{counts.get(c, 0)})" for c in ranked)
            )
        return ranked

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """展示/诊断用：{provider: {"active": db计数, "last_assigned": ts}}"""
        counts = self.active_counts(use_cache=False)
        ledger = self._load_ledger(use_cache=False).get("providers", {})
        result: dict[str, dict[str, Any]] = {}
        for name in set(counts) | set(ledger):
            result[name] = {
                "active": counts.get(name, 0),
                "last_assigned": float((ledger.get(name) or {}).get("last_assigned", 0) or 0),
            }
        return result


# ============ 全局单例 ============

_balance: ProviderBalance | None = None


def get_provider_balance() -> ProviderBalance:
    """获取全局空闲度账本单例"""
    global _balance
    if _balance is None:
        _balance = ProviderBalance()
    return _balance

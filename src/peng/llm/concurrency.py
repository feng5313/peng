"""
LLM 请求跨进程并发限制器（事务#2204）

问题背景
--------
``llm.max_concurrent``（全局）与 ``llm.providers.<name>.max_concurrent``（provider 级）
此前用**进程内** ``asyncio.Semaphore`` 实现（client.py）。但 peng 的每个事务都在
独立子进程中执行（``TaskManager._execute_task_process`` → ``subprocess.Popen(runner.py)``
→ ``run_task_process`` → ``PengAgent()`` → ``GLMClient()``），信号量因此是进程私有的：

1. 跨事务完全不共享 —— N 个事务 = N 个独立信号量，provider 真实并发数不受任何约束；
2. 单进程内 tool_engine 本就串行调用 ``chat_stream``（同一时刻最多 1 个在途请求），
   进程内信号量也从不真正生效。

净效果：``max_concurrent`` 对真实的 provider 并发数**毫无约束**（用户实测抱怨
"毫无效果"），本地部署的大模型（如 swift-qwen3.8）会被多事务同时打爆。

本模块方案：持久文件锁槽位 = 跨进程信号量
----------------------------------------
- 每个 ``(key, limit)`` 对应 ``limit`` 个槽位锁文件
  ``<project_root>/data/llm_slots/<sanitized_key>.<i>.lock``（i = 0..limit-1）。
- 对槽位锁文件加 OS 独占锁（POSIX: ``fcntl.flock`` / Windows: ``msvcrt.locking``），
  加锁成功即持有该槽位。OS 文件锁随进程退出（正常退出/异常/被 kill -9）由内核
  自动释放，因此崩溃不会留下"僵尸槽位"永久占用。
- 组合多个约束（全局并发 + provider 并发）时采用**全有或全无**：逐个非阻塞尝试，
  任一失败立即释放已获得的槽位并退避重试。全程不做阻塞式等待 ⇒ 天然无死锁
  （不会出现 hold-and-wait 循环）。
- 等待（排队）期间定期刷新事务心跳，避免长队导致心跳超时误杀；等待超过阈值时
  通过回调向事务输出打印一次提示（可见、可诊断）。

为什么不用 ``filelock`` 库（重要：踩过的坑）
----------------------------------------
第一版用 ``filelock.FileLock`` 实现，端到端测试实测**并发上限被突破**（3 进程同时
进入 limit=2 的临界区）。根因：filelock 的 Unix 实现 ``_release()`` 在放锁时
**unlink 删除锁文件**，而"同一路径"在删除后重新创建会得到**新的 inode**：

    P1 open(路径) → inode X → flock(X) 成功 → 持有
    P1 release → unlink(路径) [inode X 已无名字] → flock 解锁 → close
    P2 open(O_CREAT, 路径) → 得到**新 inode Y** → flock(Y) 成功 → 持有

只要有任何一方还持有"无名 inode X"（例如本模块的**部分获取回滚**会释放并删除已获得的
槽位文件，而另一进程此刻正好 open 到新 inode），就会出现"同一槽位被两个进程同时持有"
⇒ 并发限制失效。实测证据：同一路径 ``provider_mock.1.lock`` 上两个进程分别持有
inode 3539843 / 3539844，且各自 ``is_locked=True``。

因此本模块自行实现槽位锁：**锁文件只创建、永不删除**（inode 稳定），
放锁只做 ``flock(LOCK_UN)`` + ``close``。不变量：同一路径永远是同一个 inode，
于是 flock 的互斥语义 = 槽位互斥语义。

同进程并发：每次尝试都新 ``os.open`` 得到独立 fd，flock 在独立 fd 之间互斥
（同进程不同 fd 亦然）。因此同进程多个并发请求会各占**不同**槽位，
同一槽位不会被重复计数。

跨进程共享目录：``<project_root>/data/llm_slots/``（所有 runner 子进程 + 服务进程
共用同一安装目录）。锁文件数量固定（等于各 key 的 limit），创建后长期保留。
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
import os
import random
import re
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from peng.common.paths import chown_to_project_owner, ensure_dir_with_owner

logger = logging.getLogger(__name__)

#: 槽位锁文件所在子目录（相对 project_root/data）
SLOTS_SUBDIR = "llm_slots"

#: 锁文件权限（显式 0666，避免服务以 root 运行时普通用户进程无法打开）
_SLOT_FILE_MODE = 0o666

#: 轮询间隔（秒）：首次获取是立即尝试，失败后按此间隔（含抖动）重试。
#: 不使用退避放大，保证长等待的事务也能以同样频率抢到刚释放的槽位（公平性）
POLL_INTERVAL = 0.1
#: 轮询间隔抖动比例（0~1）：多进程同步轮询易产生惊群，加入随机抖动打散
POLL_JITTER = 0.5
#: 单次尝试最多探测的槽位数：limit 很大时（如小模型 200）避免一次轮询打开
#: 上百个文件。随机起点+多次轮询保证最终能找到空闲槽位
PROBE_MAX = 8

#: 等待提示阈值（秒）：等待超过该时长向事务输出打印一次提示（写事务输出文件）
WAIT_NOTICE_SECONDS = 5.0
#: 等待期间刷新事务心跳的间隔（秒）。远小于默认心跳超时（300s）
WAIT_HEARTBEAT_INTERVAL = 30.0
#: 等待期间 WARNING 日志间隔（秒）
WAIT_LOG_INTERVAL = 60.0

#: 被占用（非致命）的锁错误码：POSIX EAGAIN/EWOULDBLOCK/EACCES，Windows EACCES/EDEADLK
_BUSY_ERRNOS = {errno.EAGAIN, errno.EACCES, errno.EDEADLK}
if hasattr(errno, "EWOULDBLOCK"):
    _BUSY_ERRNOS.add(errno.EWOULDBLOCK)

# 类型别名：并发约束列表 [(key, limit), ...]
LimitSpecs = Sequence[tuple[str, int]]


def slots_root(slots_dir: Path | str | None = None) -> Path:
    """获取槽位锁文件根目录（默认 <project_root>/data/llm_slots）"""
    if slots_dir is not None:
        return Path(slots_dir)
    from peng.config import get_project_root

    return get_project_root() / "data" / SLOTS_SUBDIR


def sanitize_key(raw: str) -> str:
    """把资源 key 规范化为安全的文件名片段（保留可读性 + 短哈希防碰撞）

    不同 key 规范化后必须仍然互不相同（否则不同 provider 会错误共享槽位），
    因此追加原始 key 的 sha1 前 8 位。
    """
    safe = re.sub(r"[^0-9A-Za-z_.-]", "_", raw)[:80]
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}"


def slot_file(key: str, index: int, slots_dir: Path | str | None = None) -> Path:
    """获取某个槽位的锁文件路径"""
    return slots_root(slots_dir) / f"{sanitize_key(key)}.{index}.lock"


def describe_limits(limits: LimitSpecs) -> str:
    """把并发约束列表格式化为可读描述（日志/输出提示用）"""
    return ", ".join(f"{key}×{limit}" for key, limit in limits)


def normalize_limits(specs: LimitSpecs | None) -> list[tuple[str, int]]:
    """过滤/规范化并发约束：只保留 limit > 0 的项（None/0/负数 = 不限并发）"""
    if not specs:
        return []
    result: list[tuple[str, int]] = []
    for key, limit in specs:
        if not key:
            continue
        try:
            limit_int = int(limit)
        except (TypeError, ValueError):
            continue
        if limit_int > 0:
            result.append((str(key), limit_int))
    return result


class SlotLock:
    """单个槽位的文件锁（跨进程互斥，锁文件持久不删除）

    语义与 ``fcntl.flock``/``msvcrt.locking`` 一致：锁随持有 fd 关闭或进程退出
    自动释放；**锁文件本身永不删除**（删除会让同一路径换 inode，破坏互斥）。
    """

    __slots__ = ("path", "_fd")

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    @property
    def locked(self) -> bool:
        """当前是否持有该槽位"""
        return self._fd is not None

    def try_acquire(self) -> bool:
        """非阻塞尝试加锁；被其他持有者占用时返回 False

        Raises:
            OSError: 安装级故障（磁盘满/权限错误等），不静默绕过并发限制
        """
        if self._fd is not None:  # 已持有：直接成功（幂等）
            return True
        created = not self.path.exists()
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, _SLOT_FILE_MODE)
        try:
            if created:
                # 显式权限：umask/服务以 root 运行时创建的文件需可被普通用户进程打开
                try:
                    os.fchmod(fd, _SLOT_FILE_MODE)
                except OSError:  # pragma: no cover - 非致命
                    pass
                chown_to_project_owner(self.path)
            _flock_exclusive(fd)
        except OSError as e:
            os.close(fd)
            if e.errno in _BUSY_ERRNOS:
                return False
            raise
        self._fd = fd
        return True

    def release(self) -> None:
        """释放槽位（锁文件保留，下次同路径仍是同一个 inode）"""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _funlock(fd)
        except OSError as e:  # pragma: no cover - 解锁失败不应影响主流程
            logger.warning(f"[LLM并发] 解锁槽位失败(忽略): {self.path}: {e}")
        finally:
            try:
                os.close(fd)
            except OSError:  # pragma: no cover
                pass


if os.name == "nt":  # pragma: no cover - Windows

    import msvcrt

    # msvcrt 的锁定 API 只存在于 Windows 平台的 typeshed 桩中（其他平台是空桩），
    # 显式按 Any 使用，避免跨平台类型检查对平台专属分支误报
    _msvcrt: Any = msvcrt

    def _flock_exclusive(fd: int) -> None:
        """Windows：对文件首字节加非阻塞独占锁（msvcrt.locking）

        锁随 fd 关闭或进程退出自动释放；被占用时抛 OSError(EACCES/EDEADLK)。
        """
        os.lseek(fd, 0, os.SEEK_SET)
        _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)

    def _funlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)

else:

    def _flock_exclusive(fd: int) -> None:
        """POSIX：flock 非阻塞独占锁（锁随 fd 关闭/进程退出自动释放）"""
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _funlock(fd: int) -> None:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _try_acquire_one(
    key: str, limit: int, slots_dir: Path | str | None
) -> SlotLock | None:
    """非阻塞尝试获取 key 的任一空闲槽位，全部占用则返回 None

    随机起点遍历槽位（最多 PROBE_MAX 个，避免 limit 很大时的天文级探测开销），
    降低多进程"总是抢 0 号槽"造成的无谓冲突。
    仅把"锁被占用"视为获取失败；其他异常（磁盘/权限等安装级故障）向上抛出，
    避免静默绕过并发限制。
    """
    probes = min(limit, PROBE_MAX)
    start = random.randrange(limit)
    for offset in range(probes):
        idx = (start + offset) % limit
        lock = SlotLock(slot_file(key, idx, slots_dir))
        if lock.try_acquire():
            return lock
    return None


class _HeldSlots:
    """一次成功获取的全部槽位（幂等释放）"""

    __slots__ = ("_locks",)

    def __init__(self, locks: list[SlotLock]) -> None:
        self._locks = locks

    def release(self) -> None:
        locks, self._locks = self._locks, []
        for lock in locks:
            lock.release()


class LlmSlotHandle:
    """已获取的 LLM 并发槽位句柄

    ``release()`` 是**同步**操作（flock 解锁 + close，无需 await），因此可以直接
    放在 ``try/finally`` 的 finally 中释放——包括异步生成器被 aclose（GeneratorExit）
    展开 finally 的场景（此时执行 await 会触发 "asynchronous generator is already
    running" 类问题，同步释放则安全）。
    """

    __slots__ = ("_held",)

    def __init__(self, held: _HeldSlots | None) -> None:
        self._held = held

    @property
    def acquired(self) -> bool:
        """是否持有槽位（空约束=不限并发时为 False）"""
        return self._held is not None

    def release(self) -> None:
        """释放槽位（幂等，可重复调用）"""
        held, self._held = self._held, None
        if held is not None:
            held.release()


def _try_acquire_all(limits: LimitSpecs, slots_dir: Path | str | None) -> _HeldSlots | None:
    """一次性非阻塞获取全部约束的槽位；任一失败则回滚全部并返回 None

    按 key 排序保证获取顺序一致（便于排查），且全程非阻塞，不存在死锁。
    """
    locks: list[SlotLock] = []
    try:
        for key, limit in sorted(limits, key=lambda item: item[0]):
            lock = _try_acquire_one(key, limit, slots_dir)
            if lock is None:
                _HeldSlots(locks).release()
                return None
            locks.append(lock)
        return _HeldSlots(locks)
    except BaseException:
        _HeldSlots(locks).release()
        raise


async def acquire_llm_slots_handle(
    specs: LimitSpecs | None,
    *,
    task_id: int | None = None,
    heartbeat_cb: Callable[[], None] | None = None,
    notice_cb: Callable[[float, str], None] | None = None,
    slots_dir: Path | str | None = None,
) -> LlmSlotHandle:
    """跨进程获取 LLM 并发槽位，返回可**同步释放**的句柄

    适用于"获取点与释放点不在同一缩进层级"的场景（chat_stream 的请求块位于
    函数体顶层缩进，无法直接包进 async with；且释放点可能处于 GeneratorExit
    展开路径，此时同步释放比 async with 更安全）。

    Args:
        specs: 并发约束列表 [(key, limit), ...]；空/None 表示不限制（直接放行）
        task_id: 事务ID（仅用于日志与提示）
        heartbeat_cb: 等待期间定期调用的心跳刷新回调（同步函数，在线程池中执行）
        notice_cb: 等待超过 WAIT_NOTICE_SECONDS 时调用一次的回调 (已等待秒数, 约束描述)
        slots_dir: 槽位目录覆盖（测试用）

    Returns:
        LlmSlotHandle（无论是否限制并发都可安全 release()）

    Raises:
        OSError: 槽位文件无法建立/加锁等安装级故障（不静默绕过限制）
    """
    limits = normalize_limits(specs)
    if not limits:
        return LlmSlotHandle(None)

    root = slots_root(slots_dir)
    ensure_dir_with_owner(root)

    desc = describe_limits(limits)
    held = _try_acquire_all(limits, slots_dir)

    if held is None:
        wait_start = time.monotonic()
        last_heartbeat = wait_start
        last_log = wait_start
        noticed = False
        logger.info(f"[LLM并发] 事务#{task_id} 等待并发槽位({desc}) ...")
        while True:
            await asyncio.sleep(POLL_INTERVAL + random.uniform(0.0, POLL_INTERVAL * POLL_JITTER))
            held = _try_acquire_all(limits, slots_dir)
            if held is not None:
                break
            now = time.monotonic()
            waited = now - wait_start
            # 等待期间刷新心跳，防止长队导致心跳超时误杀事务
            if heartbeat_cb is not None and now - last_heartbeat >= WAIT_HEARTBEAT_INTERVAL:
                last_heartbeat = now
                try:
                    await asyncio.to_thread(heartbeat_cb)
                except Exception as e:
                    logger.debug(f"[LLM并发] 等待期间刷新心跳失败(忽略): {e}")
            # 等待超过阈值：向事务输出打印一次提示（用户可见）
            if not noticed and notice_cb is not None and waited >= WAIT_NOTICE_SECONDS:
                noticed = True
                try:
                    notice_cb(waited, desc)
                except Exception as e:
                    logger.debug(f"[LLM并发] 等待提示回调失败(忽略): {e}")
            # 低频 WARNING 日志（便于诊断长时间排队）
            if now - last_log >= WAIT_LOG_INTERVAL:
                last_log = now
                logger.warning(f"[LLM并发] 事务#{task_id} 已等待并发槽位 {waited:.0f}s ({desc})")
        logger.info(
            f"[LLM并发] 事务#{task_id} 等待 {time.monotonic() - wait_start:.1f}s 后获得并发槽位({desc})"
        )

    return LlmSlotHandle(held)


@asynccontextmanager
async def acquire_llm_slots(
    specs: LimitSpecs | None,
    *,
    task_id: int | None = None,
    heartbeat_cb: Callable[[], None] | None = None,
    notice_cb: Callable[[float, str], None] | None = None,
    slots_dir: Path | str | None = None,
) -> AsyncIterator[None]:
    """跨进程获取 LLM 并发槽位（异步上下文管理器，等同 acquire_llm_slots_handle）
    """
    handle = await acquire_llm_slots_handle(
        specs,
        task_id=task_id,
        heartbeat_cb=heartbeat_cb,
        notice_cb=notice_cb,
        slots_dir=slots_dir,
    )
    try:
        yield
    finally:
        handle.release()


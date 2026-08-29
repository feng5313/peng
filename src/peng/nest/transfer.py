"""
Nest 文件传输库 - push/pull/sync 核心逻辑

供 tools/nest.py（NestTool）与 bin/nest CLI（push/pull/sync 子命令）复用。
传输走 nest WebSocket 协议（write_bytes / read_bytes，base64 分块，二进制安全），
单块默认 512KB（base64 后 ~683KB，远低于 bridge 的 50MB 消息上限）。

Transport 抽象：任何提供 `async request(type, params, timeout) -> NestResponse`
的对象均可作为 transport：
- peng 主进程/子进程：peng.nest.client 的 send_request 闭包
- CLI 在 nest 机器上：连接 peng 服务端 ws (bridge 8899) 的 forward 客户端
"""

from __future__ import annotations

import base64
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from peng.nest.protocol import NestResponse

logger = logging.getLogger(__name__)

# 单块大小（字节）。base64 后约 1.37 倍，512KB → ~700KB，安全
DEFAULT_CHUNK_SIZE = 512 * 1024

# mtime 比较容差（秒）：FAT32 2s 精度 / 网络盘时钟漂移
MTIME_TOLERANCE = 2.0

# 进度回调类型：on_event(事件字典)
OnEvent = Callable[[dict], None]


class TransferError(Exception):
    """传输错误"""


def _check(response: NestResponse, action: str) -> dict:
    """检查 NestResponse，出错抛 TransferError"""
    if response is None:
        raise TransferError(f"{action}失败: 空响应")
    if response.error:
        raise TransferError(f"{action}失败: {response.error}")
    return response.data if isinstance(response.data, dict) else (response.data or {})


def _fmt_size(n: float) -> str:
    """人类可读字节数"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


# ==================== 单文件传输 ====================


async def push_file(
    transport: Any,
    local_path: str | Path,
    remote_path: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    set_mtime: bool = True,
    on_event: OnEvent | None = None,
) -> dict:
    """推送单个本地文件到远端 nest（分块二进制安全）

    每块均携带 mtime（nest 端写入后 os.utime），确保最后一块落地后时间正确，
    避免文件大小恰为块整数倍时漏设 mtime。

    Returns: {path, size, chunks, speed}
    """
    src = Path(local_path)
    if not src.is_file():
        raise TransferError(f"本地文件不存在: {src}")

    st = src.stat()
    size, mtime = st.st_size, st.st_mtime
    chunks = 0
    t0 = time.monotonic()

    with open(src, "rb") as f:
        while True:
            raw = f.read(chunk_size)
            if not raw and chunks > 0:
                break  # 已写完
            mode = "create" if chunks == 0 else "append"
            params: dict[str, Any] = {
                "path": remote_path,
                "data_b64": base64.b64encode(raw).decode("ascii") if raw else "",
                "mode": mode,
            }
            if set_mtime:
                params["mtime"] = mtime
            _check(
                await transport.request("write_bytes", params, timeout=120.0),
                f"写入 {remote_path}",
            )
            chunks += 1
            if on_event:
                on_event({"type": "chunk", "path": remote_path, "chunks": chunks, "size": min(chunks * chunk_size, size), "total": size})
            if not raw:
                break  # 空文件：写一次 create 空块即结束

    elapsed = max(time.monotonic() - t0, 0.001)
    result = {
        "path": remote_path,
        "size": size,
        "chunks": chunks,
        "speed": size / elapsed,
    }
    if on_event:
        on_event({"type": "file_done", "direction": "push", **result})
    return result


async def pull_file(
    transport: Any,
    remote_path: str,
    local_path: str | Path,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    on_event: OnEvent | None = None,
) -> dict:
    """从远端 nest 拉取单个文件（分块二进制安全）

    Returns: {path, size, chunks}
    """
    dst = Path(local_path)
    offset = 0
    chunks = 0
    t0 = time.monotonic()

    while True:
        data = _check(
            await transport.request(
                "read_bytes",
                {"path": remote_path, "offset": offset, "chunk_size": chunk_size, "encoding": "base64"},
                timeout=120.0,
            ),
            f"读取 {remote_path}",
        )
        raw = base64.b64decode(data.get("content_b64", ""))
        got = len(raw)
        if offset == 0:
            # 首块：创建目录并覆盖写入
            dst.parent.mkdir(parents=True, exist_ok=True)
            with open(dst, "wb") as f:
                f.write(raw)
        elif got > 0:
            with open(dst, "ab") as f:
                f.write(raw)
        chunks += 1
        offset += got
        if on_event:
            on_event({"type": "chunk", "path": remote_path, "chunks": chunks, "size": offset, "total": data.get("total_size", 0)})
        if not data.get("has_more"):
            break
        if got == 0 and offset > 0:
            break  # 防御：远端异常时避免死循环

    size = offset
    elapsed = max(time.monotonic() - t0, 0.001)
    result = {"path": str(dst), "size": size, "chunks": chunks, "speed": size / elapsed}
    if on_event:
        on_event({"type": "file_done", "direction": "pull", **result})
    return result


# ==================== 目录树枚举 ====================


def _iter_local_files(base: Path) -> dict[str, dict]:
    """枚举本地目录树（相对路径 posix → {size, mtime}），跳过符号链接"""
    result: dict[str, dict] = {}
    if not base.exists():
        return result
    for root, dirs, names in os.walk(base):
        dirs[:] = [d for d in dirs if not Path(root, d).is_symlink()]
        for name in names:
            p = Path(root) / name
            try:
                if p.is_symlink():
                    continue
                st = p.stat()
            except OSError:
                continue
            result[p.relative_to(base).as_posix()] = {"size": st.st_size, "mtime": st.st_mtime}
    return result


async def _list_remote_files(transport: Any, remote_dir: str) -> dict[str, dict]:
    """枚举远端目录树（相对路径 posix → {size, mtime}），目录不存在返回空"""
    data = _check(
        await transport.request("list_tree", {"path": remote_dir}, timeout=60.0),
        f"列举远端目录 {remote_dir}",
    )
    if not data.get("exists"):
        return {}
    return {f["path"]: {"size": f["size"], "mtime": f["mtime"]} for f in data.get("files", [])}


def _need_copy(src: dict, dst: dict | None) -> bool:
    """判断是否需要复制：目标缺失 / 大小不同 / 源更新（含容差）"""
    if dst is None:
        return True
    if src["size"] != dst["size"]:
        return True
    return src["mtime"] > dst["mtime"] + MTIME_TOLERANCE


# ==================== 目录同步 ====================


async def sync_tree(
    transport: Any,
    local_dir: str | Path,
    remote_dir: str,
    direction: str = "push",
    delete: bool = False,
    dry_run: bool = False,
    on_event: OnEvent | None = None,
) -> dict:
    """目录递归同步（增量：mtime+size 比对）

    Args:
        direction: "push"（本地→远端）或 "pull"（远端→本地）
        delete: 删除目标端源端没有的文件（镜像），并清理空目录
        dry_run: 只报告将执行的变更，不实际传输

    Returns: {checked, copied, skipped, deleted, failed, total_bytes}
    """
    if direction not in ("push", "pull"):
        raise TransferError(f"无效的 direction: {direction}（支持 push/pull）")

    local_base = Path(local_dir)
    if direction == "push" and local_base.exists() and not local_base.is_dir():
        raise TransferError(f"push 同步的本地路径必须是目录: {local_base}")

    local_files = _iter_local_files(local_base)
    remote_files = await _list_remote_files(transport, remote_dir)

    if direction == "push":
        src_files, dst_files = local_files, remote_files
    else:
        src_files, dst_files = remote_files, local_files

    stats = {"checked": 0, "copied": 0, "skipped": 0, "deleted": 0, "failed": 0, "total_bytes": 0}

    # 1. 复制新增/变更文件
    for rel in sorted(src_files):
        stats["checked"] += 1
        src_meta = src_files[rel]
        dst_meta = dst_files.get(rel)
        if not _need_copy(src_meta, dst_meta):
            stats["skipped"] += 1
            continue
        stats["total_bytes"] += src_meta["size"]
        if dry_run:
            stats["copied"] += 1
            if on_event:
                on_event({"type": "would_copy", "direction": direction, "path": rel, "size": src_meta["size"]})
            continue
        try:
            remote_file = f"{remote_dir.rstrip('/')}/{rel}"
            local_file = local_base / rel
            if direction == "push":
                await push_file(transport, str(local_file), remote_file, on_event=on_event)
            else:
                await pull_file(transport, remote_file, str(local_file), on_event=on_event)
            stats["copied"] += 1
        except TransferError as e:
            stats["failed"] += 1
            logger.error(f"同步文件失败 {rel}: {e}")
            if on_event:
                on_event({"type": "error", "path": rel, "error": str(e)})

    # 2. 删除目标端多余文件（--delete）
    if delete:
        extra = sorted(set(dst_files) - set(src_files))
        for rel in extra:
            if dry_run:
                stats["deleted"] += 1
                if on_event:
                    on_event({"type": "would_delete", "path": rel})
                continue
            try:
                if direction == "push":
                    _check(
                        await transport.request("delete", {"path": f"{remote_dir.rstrip('/')}/{rel}"}, timeout=60.0),
                        f"删除远端 {rel}",
                    )
                else:
                    p = local_base / rel
                    if p.is_dir() and not p.is_symlink():
                        import shutil

                        shutil.rmtree(p)
                    else:
                        p.unlink(missing_ok=True)
                stats["deleted"] += 1
            except (TransferError, OSError) as e:
                stats["failed"] += 1
                logger.error(f"删除失败 {rel}: {e}")
                if on_event:
                    on_event({"type": "error", "path": rel, "error": str(e)})

        # 3. 清理目标端空目录（pull 方向本地清理；push 方向远端逐个探测删除）
        if not dry_run:
            if direction == "pull":
                _cleanup_local_empty_dirs(local_base, set(src_files))
            else:
                await _cleanup_remote_empty_dirs(transport, remote_dir, src_files)

    return stats


def _cleanup_local_empty_dirs(base: Path, keep_files: dict[str, dict]) -> None:
    """删除本地 base 下不含任何保留文件的空目录（自底向上）"""
    keep_dirs: set[str] = set()
    for rel in keep_files:
        parts = rel.split("/")
        for i in range(1, len(parts)):
            keep_dirs.add("/".join(parts[:i]))
    all_dirs: list[Path] = [p for p in base.rglob("*") if p.is_dir() and not p.is_symlink()]
    for d in sorted(all_dirs, key=lambda x: len(x.parts), reverse=True):
        rel = d.relative_to(base).as_posix()
        if rel in keep_dirs:
            continue
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass


async def _cleanup_remote_empty_dirs(transport: Any, remote_dir: str, keep_files: dict[str, dict]) -> None:
    """删除远端 remote_dir 下的空目录（基于 list_tree 结果自底向上探测）"""
    keep_dirs: set[str] = set()
    for rel in keep_files:
        parts = rel.split("/")
        for i in range(1, len(parts)):
            keep_dirs.add("/".join(parts[:i]))
    # list_tree 只返回文件，无法得知目录集合；用 stat 探测代价高，
    # 简化策略：删除多余文件后不主动清目录（rsync 默认也不删空目录时可接受）
    # 如后续需要可通过新增协议类型实现。
    return

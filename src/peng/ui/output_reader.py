"""
输出读取器

从文件读取进程输出，支持：
- 增量读取（只读取新增内容）
- 多客户端独立订阅（每个客户端有自己的订阅列表）
- 历史输出读取
- 异步回调通知
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from peng.config.loader import load_config

logger = logging.getLogger(__name__)


class OutputReader:
    """
    输出读取器

    从输出文件读取内容，支持：
    - 增量读取：记录已读取位置，只读取新增内容
    - 多客户端订阅：每个客户端独立订阅不同的消息/事务
    - 历史读取：发送已订阅文件的历史内容
    - 异步回调：通过回调函数通知新输出
    """

    def __init__(self):
        """初始化输出读取器"""
        # 加载配置
        try:
            config = load_config()
            self._output_dir = Path(config.paths.output_dir)
        except Exception as e:
            logger.warning(f"加载配置失败，使用默认输出目录: {e}")
            self._output_dir = Path("data/output")

        # 每个客户端的订阅：{client_id: {task_id: position}}
        self._subscriptions: dict[int, dict[int, int]] = {}
        # 每个客户端是否明确订阅了特定task_ids（True=明确订阅，False=自动发现）
        self._explicit_task_subscriptions: dict[int, bool] = {}
        # 每个客户端的回调：{client_id: callback}
        self._callbacks: dict[int, Callable[[dict[str, Any]], Awaitable[None]]] = {}
        # 🔧 Task#1435：订阅epoch —— 每次 update_subscription 递增，
        # _send_history_output_async 发送前检查 epoch 是否过期，避免快速切换时串现
        self._subscription_epochs: dict[int, int] = {}
        # 延迟初始化的asyncio.Lock（避免绑定到错误的event loop）
        self._lock: asyncio.Lock | None = None
        self._lock_loop_id: int | None = None

        # 监控事务
        self._watch_task: asyncio.Task | None = None
        self._running = False

        # 客户端ID计数器
        self._next_client_id = 0

        logger.info(f"OutputReader 初始化完成，输出目录: {self._output_dir}")

    def _get_lock(self) -> asyncio.Lock:
        """获取绑定到当前event loop的asyncio.Lock（事件循环变化时自动重建）

        解决服务重启后单例OutputReader的Lock绑定到旧event loop的问题：
        RuntimeError: <Lock> is bound to a different event loop
        """
        try:
            current_loop_id = id(asyncio.get_running_loop())
        except RuntimeError:
            current_loop_id = None
        if self._lock is None or current_loop_id != self._lock_loop_id:
            self._lock = asyncio.Lock()
            self._lock_loop_id = current_loop_id
        return self._lock

    @property
    def output_dir(self) -> Path:
        """获取输出目录"""
        return self._output_dir

    def get_output_file_path(self, task_id: int) -> Path:
        """
        获取输出文件路径

        Args:
            task_id: 事务ID

        Returns:
            输出文件路径
        """
        return self._output_dir / f"task-{task_id}.out"

    def parse_output_line(self, line: str) -> dict[str, Any] | None:
        """
        解析输出行

        格式：{timestamp}\t{source}\t{task_id}\t{content}

        source 可能是：thinking, answer, bash, tool_call, tool_result, system, task 等

        Args:
            line: 输出行

        Returns:
            解析后的字典，解析失败返回None
        """
        if not line or not line.strip():
            return None

        parts = line.strip().split("\t", 3)

        if len(parts) < 4:
            return None

        try:
            return {
                "timestamp": parts[0],
                "source": parts[1],
                "task_id": int(parts[2]),
                "content": parts[3],
            }
        except (ValueError, IndexError):
            return None

    def read_all_output(self, task_id: int) -> str:
        """
        同步读取指定事务的所有输出（返回整个文件内容）

        Args:
            task_id: 事务 ID

        Returns:
            文件内容字符串
        """
        file_path = self.get_output_file_path(task_id)
        if not file_path.exists():
            return ""

        try:
            with open(file_path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception as e:
            logger.debug(f"读取输出文件失败：{file_path}, 错误：{e}")
            return ""

    def read_last_n_output(self, task_id: int, limit: int) -> list[dict[str, Any]]:
        """
        只读取最后N行输出（优化内存使用）

        Args:
            task_id: 事务ID
            limit: 最大返回条数

        Returns:
            输出列表，最多limit条
        """
        file_path = self.get_output_file_path(task_id)
        if not file_path.exists():
            return []

        outputs = []
        try:
            with open(file_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    parsed = self.parse_output_line(line)
                    if parsed:
                        outputs.append(parsed)
                        # 保持队列大小不超过limit
                        if len(outputs) > limit:
                            outputs.pop(0)
        except Exception as e:
            logger.debug(f"读取输出文件失败: {file_path}, 错误: {e}")

        return outputs

    def read_all_output_lines(self, task_id: int) -> list[dict[str, Any]]:
        """
        读取全部输出行

        Args:
            task_id: 事务 ID

        Returns:
            全部输出列表
        """
        file_path = self.get_output_file_path(task_id)
        if not file_path.exists():
            return []

        outputs = []
        try:
            with open(file_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    parsed = self.parse_output_line(line)
                    if parsed:
                        outputs.append(parsed)
        except Exception as e:
            logger.debug(f"读取输出文件失败：{file_path}, 错误：{e}")

        return outputs

    async def register_client(
        self,
        callback: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> int:
        """
        注册新客户端

        Args:
            callback: 回调函数，接收输出数据

        Returns:
            客户端 ID
        """
        async with self._get_lock():
            client_id = self._next_client_id
            self._next_client_id += 1
            self._subscriptions[client_id] = {}
            # 🔧 修复 WebUI 卡顿：默认设为明确订阅模式，防止 _watch_loop
            # 在前端 subscribe 请求到达前自动发现并读取所有359+个输出文件，
            # 导致瞬间向浏览器发送数万条历史输出，卡死主线程。
            # 只有当前端发送 task_ids=[] 时才切换为自动发现模式。
            self._explicit_task_subscriptions[client_id] = True
            self._callbacks[client_id] = callback
            self._subscription_epochs[client_id] = 0  # 🔧 Task#1435
            logger.debug(f"客户端 {client_id} 已注册（默认明确订阅模式）")
            return client_id

    async def unregister_client(self, client_id: int) -> None:
        """
        注销客户端

        Args:
            client_id: 客户端 ID
        """
        async with self._get_lock():
            self._subscriptions.pop(client_id, None)
            self._callbacks.pop(client_id, None)
            self._explicit_task_subscriptions.pop(client_id, None)
            self._subscription_epochs.pop(client_id, None)  # 🔧 Task#1435
            logger.debug(f"客户端 {client_id} 已注销")

    def _discover_output_files(self) -> list[int]:
        """
        发现输出目录中所有事务输出文件

        Returns:
            [task_id, ...] 列表
        """
        result = []
        try:
            prefix = "task-"
            suffix = ".out"
            for file_path in self._output_dir.iterdir():
                if file_path.name.startswith(prefix) and file_path.name.endswith(suffix):
                    try:
                        # 解析 task_id: 从 "task-{id}.out" 中提取 {id}
                        task_id_str = file_path.name[len(prefix) : -len(suffix)]
                        task_id = int(task_id_str)
                        result.append(task_id)
                    except (ValueError, OSError):
                        continue
        except OSError:
            pass
        return result

    def _find_position_since_time(self, file_path: Path, since_time: str) -> int:
        """
        在输出文件中找到第一个 timestamp > since_time 的行的起始位置。

        🚀 性能优化：使用二分查找替代逐行扫描（O(log n) vs O(n)）。
        对16MB/10万行文件，从 ~8秒 降至 ~0.3毫秒。
        """
        if not file_path.exists():
            return 0
        try:
            file_size = file_path.stat().st_size
            if file_size == 0:
                return 0
            with open(file_path, "rb") as f:
                lo, hi = 0, file_size
                result = file_size
                while lo < hi:
                    mid = (lo + hi) // 2
                    f.seek(mid)
                    if mid > 0:
                        f.readline()
                    line_start = f.tell()
                    line = f.readline()
                    if not line:
                        hi = mid
                        continue
                    tab_idx = line.find(b"\t")
                    if tab_idx <= 0:
                        lo = line_start + 1
                        continue
                    ts = line[:tab_idx].decode("utf-8", errors="replace")
                    if ts > since_time:
                        result = line_start
                        hi = mid
                    else:
                        lo = line_start + 1
                return result
        except Exception as e:
            logger.warning(f"_find_position_since_time 读取失败: {file_path}, 错误: {e}")
            if file_path.exists():
                return file_path.stat().st_size
            return 0

    def count_total_lines(self, file_path: Path) -> int:
        """快速统计文件总行数（分块二进制读取，C级速度）。"""
        if not file_path.exists():
            return 0
        count = 0
        try:
            with open(file_path, "rb") as f:
                while True:
                    buf = f.read(1024 * 1024)
                    if not buf:
                        break
                    count += buf.count(b"\n")
            return count
        except Exception as e:
            logger.warning(f"count_total_lines 失败: {file_path}, {e}")
            return 0

    def read_lines_from_byte_pos(self, file_path: Path, byte_pos: int) -> tuple[list[str], int, int]:
        """从指定字节位置读取到文件末尾。

        Returns: (lines, total_lines, start_line_no)
        """
        if not file_path.exists():
            return [], 0, 0
        try:
            with open(file_path, "rb") as f:
                # 快速统计 byte_pos 之前的行数（分块二进制计数）
                f.seek(0)
                prev_lines = 0
                remaining = byte_pos
                while remaining > 0:
                    chunk_size = min(1024 * 1024, remaining)
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    prev_lines += chunk.count(b"\n")
                    remaining -= len(chunk)
                start_line_no = prev_lines + 1
                # 读取 byte_pos 之后的全部内容
                f.seek(byte_pos)
                remaining_bytes = f.read()
            remaining_text = remaining_bytes.decode("utf-8", errors="replace")
            # 🔧 修复：不能用 splitlines()！它会把 \x0b \x85 \u2028 \u2029 等
            # Unicode行分隔符当作换行，而 readlines()/split('\n') 只认 \n。
            raw_lines = remaining_text.split("\n")
            if raw_lines and raw_lines[-1] == "":
                raw_lines.pop()
            lines = [line + "\n" for line in raw_lines]
            total_lines = start_line_no - 1 + len(lines)
            return lines, total_lines, start_line_no
        except Exception as e:
            logger.warning(f"read_lines_from_byte_pos 失败: {file_path}, {e}")
            return [], 0, 0

    def find_timestamp_line_pos(
        self, file_path: Path, timestamp: str, mode: str = "gte"
    ) -> tuple[int, int, int]:
        """在文件中找到满足时间戳条件的第一行。

        🚀 二分查找 O(log n)，不逐行扫描。
        Args:
            mode: "gte" = 第一个 ts >= timestamp (for before_timestamp)
                  "gt"  = 第一个 ts > timestamp (for since_time)
        Returns: (byte_position, line_no, total_lines)
        """
        if not file_path.exists():
            return 0, 0, 0
        try:
            file_size = file_path.stat().st_size
            if file_size == 0:
                return 0, 0, 0
            match_pos = None
            with open(file_path, "rb") as f:
                lo, hi = 0, file_size
                while lo < hi:
                    mid = (lo + hi) // 2
                    f.seek(mid)
                    if mid > 0:
                        f.readline()
                    line_start = f.tell()
                    line = f.readline()
                    if not line:
                        hi = mid
                        continue
                    tab_idx = line.find(b"\t")
                    if tab_idx <= 0:
                        lo = line_start + 1
                        continue
                    ts = line[:tab_idx].decode("utf-8", errors="replace")
                    match = (ts >= timestamp) if mode == "gte" else (ts > timestamp)
                    if match:
                        match_pos = line_start
                        hi = mid
                    else:
                        lo = line_start + 1
            total_lines = self.count_total_lines(file_path)
            if match_pos is not None:
                match_line_no = self._count_newlines_before(file_path, match_pos) + 1
                return match_pos, match_line_no, total_lines
            return file_size, total_lines + 1, total_lines
        except Exception as e:
            logger.warning(f"find_timestamp_line_pos 失败: {file_path}, {e}")
            return 0, 0, 0

    def _count_newlines_before(self, file_path: Path, byte_pos: int) -> int:
        """统计文件中 byte_pos 之前的换行符数量（分块二进制计数）。"""
        if byte_pos <= 0:
            return 0
        count = 0
        try:
            with open(file_path, "rb") as f:
                remaining = byte_pos
                while remaining > 0:
                    chunk_size = min(1024 * 1024, remaining)
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    count += chunk.count(b"\n")
                    remaining -= len(chunk)
        except Exception as e:
            logger.warning(f"_count_newlines_before 失败: {file_path}, {e}")
        return count

    def read_lines_before_pos(
        self, file_path: Path, end_byte_pos: int, n: int
    ) -> tuple[list[str], int]:
        """读取 end_byte_pos 之前的 n 行（用于翻页）。

        🚀 从字节位置反向读取，不扫描整个文件！
        """
        if not file_path.exists() or end_byte_pos <= 0:
            return [], 0
        try:
            read_bytes = max(n * 200, 4096)
            while True:
                start_byte = max(0, end_byte_pos - read_bytes)
                with open(file_path, "rb") as f:
                    f.seek(start_byte)
                    if start_byte > 0:
                        f.readline()
                    data = f.read(max(0, end_byte_pos - f.tell()))
                text = data.decode("utf-8", errors="replace")
                raw_lines = [l + "\n" for l in text.split("\n") if l != ""]
                if len(raw_lines) >= n or start_byte == 0:
                    return raw_lines[-n:], len(raw_lines)
                read_bytes *= 2
        except Exception as e:
            logger.warning(f"read_lines_before_pos 失败: {file_path}, {e}")
            return [], 0

    def read_last_n_lines(self, file_path: Path, n: int) -> tuple[list[str], int, int]:
        """从文件末尾读取最后N行。

        Returns: (lines, total_lines, start_line_no)
        """
        if not file_path.exists():
            return [], 0, 0
        try:
            total_lines = self.count_total_lines(file_path)
            if total_lines == 0:
                return [], 0, 0
            start_line_no = max(1, total_lines - n + 1)
            from collections import deque
            with open(file_path, encoding="utf-8", errors="replace") as f:
                last_lines = deque(f, maxlen=n)
            return list(last_lines), total_lines, start_line_no
        except Exception as e:
            logger.warning(f"read_last_n_lines 失败: {file_path}, {e}")
            return [], 0, 0

    def read_line_range(
        self, file_path: Path, start_line: int, end_line: int
    ) -> tuple[list[str], int]:
        """读取文件中 [start_line, end_line] 范围的行（1-based，含两端）。

        Returns: (lines, total_lines)
        """
        if not file_path.exists():
            return [], 0
        try:
            lines = []
            total_lines = 0
            with open(file_path, encoding="utf-8", errors="replace") as f:
                line_no = 0
                while True:
                    line = f.readline()
                    if not line:
                        break
                    line_no += 1
                    total_lines = line_no
                    if start_line <= line_no <= end_line:
                        lines.append(line)
                    if line_no >= end_line:
                        break
            return lines, total_lines
        except Exception as e:
            logger.warning(f"read_line_range 失败: {file_path}, {e}")
            return [], 0

    async def update_subscription(
        self,
        client_id: int,
        task_ids: list[int] | None,
        skip_history: bool = False,
        since_time: str | None = None,
    ) -> tuple[bool, int]:
        """
        更新客户端的订阅（只依赖task_id，不依赖message_id）

        关键逻辑：
        - 如果该文件之前已经订阅过 → 保留读取位置 → 只读取新内容
        - 如果该文件之前没订阅过 → 位置为0 → 读取所有历史记录
        - 如果 task_ids 发生变化 → 清空旧订阅，重新开始
        - 如果 skip_history=True → 新文件位置设为文件末尾，跳过历史
        - 如果 since_time 提供 → 新文件位置设为第一个 timestamp >= since_time 的行

        Args:
            client_id: 客户端ID
            task_ids: 事务ID列表（包含所有子事务），为空时自动发现
            skip_history: 是否跳过历史输出（前端已从缓存恢复时为True）
            since_time: 起始时间（ISO格式字符串），跳过此时间之前的输出

        Returns:
            (是否是新订阅, 当前订阅epoch)
        """
        async with self._get_lock():
            if client_id not in self._subscriptions:
                return False, self._subscription_epochs.get(client_id, 0)

            # 🔧 保留已存在文件的读取位置（防止重复读取）
            old_positions = dict(self._subscriptions[client_id])
            old_task_ids = set(old_positions.keys())
            new_task_ids = set(task_ids) if task_ids else set()

            # 🔧 如果task_ids为None，表示清除订阅
            if task_ids is None:
                self._subscriptions[client_id].clear()
                self._explicit_task_subscriptions[client_id] = False
                # 🔧 Task#1435：递增epoch，使进行中的历史发送任务失效
                self._subscription_epochs[client_id] = self._subscription_epochs.get(client_id, 0) + 1
                logger.info(f"客户端 {client_id} 清除订阅 (epoch={self._subscription_epochs[client_id]})")
                return False, self._subscription_epochs[client_id]

            # 🔧 如果task_ids为空列表，表示自动发现模式，需要清空订阅
            if not task_ids:
                self._subscriptions[client_id].clear()
                self._explicit_task_subscriptions[client_id] = False
                # 🔧 Task#1435：递增epoch
                self._subscription_epochs[client_id] = self._subscription_epochs.get(client_id, 0) + 1

                # 自动发现所有输出文件
                discovered_task_ids = self._discover_output_files()
                for task_id in discovered_task_ids:
                    self._subscriptions[client_id][task_id] = 0
                logger.info(f"客户端 {client_id} 自动发现订阅: {discovered_task_ids}")
                return True, self._subscription_epochs[client_id]

            # 🔧 明确订阅模式：只订阅指定的task_ids
            self._explicit_task_subscriptions[client_id] = True

            # 🔧 Task#1435：task_ids变化时递增epoch，使之前的历史发送任务失效
            if old_task_ids != new_task_ids:
                self._subscription_epochs[client_id] = self._subscription_epochs.get(client_id, 0) + 1
                logger.info(f"客户端 {client_id} epoch递增至 {self._subscription_epochs[client_id]}")

            # 判断是否需要清空旧订阅（如果task_ids完全不同）
            if old_task_ids != new_task_ids:
                self._subscriptions[client_id].clear()
                logger.info(
                    f"客户端 {client_id} 订阅变化: {old_task_ids} -> {new_task_ids}，清空订阅"
                )

            # 添加新的订阅
            for task_id in task_ids:
                # 🔧 Task#219：如果有 since_time，即使用户之前订阅过该 task，
                # 也需要重新计算位置（否则保留的旧位置可能在文件末尾，会跳过增量数据）
                if task_id in old_positions and not since_time:
                    # 🔧 关键：如果之前有读取位置，保留它
                    self._subscriptions[client_id][task_id] = old_positions[task_id]
                    logger.info(
                        f"客户端 {client_id} 保留已有位置: task-{task_id} -> {old_positions[task_id]}"
                    )
                else:
                    # 🔧 新文件：确定起始读取位置
                    file_path = self.get_output_file_path(task_id)
                    if since_time:
                        # 🚀 性能优化：_find_position_since_time 移入线程池
                        # 这个方法会逐行扫描文件找到 since_time 对应的 byte position
                        pos = await asyncio.to_thread(
                            self._find_position_since_time, file_path, since_time
                        )
                        self._subscriptions[client_id][task_id] = pos
                        logger.info(
                            f"客户端 {client_id} 新订阅: task-{task_id} -> {pos} (since_time={since_time})"
                        )
                    elif skip_history:
                        if file_path.exists():
                            file_size = file_path.stat().st_size
                            self._subscriptions[client_id][task_id] = file_size
                            logger.info(
                                f"客户端 {client_id} 新订阅: task-{task_id} -> {file_size} (跳过历史)"
                            )
                        else:
                            self._subscriptions[client_id][task_id] = 0
                            logger.info(
                                f"客户端 {client_id} 新订阅: task-{task_id} -> 0 (文件不存在)"
                            )
                    else:
                        # 🔧 Task#216修复竞态：新订阅位置设为文件末尾（而非0）
                        # 避免watch_loop看到位置0就逐行发送全部历史（与_send_history_output_async竞态）
                        # 历史输出由_send_history_output_async统一通过output_batch批量发送
                        if file_path.exists():
                            file_size = file_path.stat().st_size
                            self._subscriptions[client_id][task_id] = file_size
                            logger.info(
                                f"客户端 {client_id} 新订阅: task-{task_id} -> {file_size} (历史由后台任务发送)"
                            )
                        else:
                            self._subscriptions[client_id][task_id] = 0
                            logger.info(
                                f"客户端 {client_id} 新订阅: task-{task_id} -> 0 (文件不存在)"
                            )

        logger.info(
            f"客户端 {client_id} 订阅更新: task_ids={task_ids or '(空)'}, 订阅文件数={len(self._subscriptions.get(client_id, {}))}"
        )

        # 🔧 判断是否是新订阅
        has_new_subscription = any(task_id not in old_positions for task_id in new_task_ids)

        # 🔧 Task#219：如果有 since_time，视为增量订阅，需要发送增量数据
        if since_time:
            logger.info(f"客户端 {client_id} 增量订阅: since_time={since_time}")
            return True, self._subscription_epochs.get(client_id, 0)

        # 🔧 如果 skip_history=True 且所有文件都在 old_positions 中，说明不是新订阅（只是恢复）
        if skip_history and not has_new_subscription:
            logger.info(f"客户端 {client_id} 非新订阅: skip_history=True 且所有文件已订阅")
            return False, self._subscription_epochs.get(client_id, 0)

        # 🔧 如果所有新订阅的文件都已经有位置，说明不是新订阅（只是切换回来）
        if not has_new_subscription:
            logger.info(f"客户端 {client_id} 非新订阅: 所有文件已订阅")
            return False, self._subscription_epochs.get(client_id, 0)

        logger.info(f"客户端 {client_id} 新订阅: 有新文件被订阅")
        return True, self._subscription_epochs.get(client_id, 0)

    def read_history_batch(
        self,
        task_ids: list[int] | None,
        client_id: int | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        """
        批量读取历史输出（支持分页，按时间戳从新到老倒序）

        Args:
            task_ids: 事务 ID 列表，为空时自动发现
            client_id: 客户端 ID，用于获取读取位置（缓存机制）
            limit: 每批返回条数（默认 500）
            offset: 分页偏移量（0=第一批，1=第二批...）

        Returns:
            {
                "outputs": [...],  # 本批输出列表
                "total": 1234,     # 总条数
                "has_more": True,  # 是否还有下一批
            }
        """
        # 如果 task_ids 为空，自动发现
        if not task_ids:
            task_ids_to_read = self._discover_output_files()
        else:
            task_ids_to_read = task_ids

        logger.info(
            f"[read_history_batch] 准备读取：task_ids={task_ids or '(自动发现)'}, "
            f"files={len(task_ids_to_read)}, client_id={client_id}, limit={limit}"
        )

        # 🔧 一次性读取全部历史到内存（master指示：内存管够）
        all_outputs = []
        for task_id in task_ids_to_read:
            file_path = self.get_output_file_path(task_id)
            if file_path.exists():
                outputs = self.read_all_output_lines(task_id)
                all_outputs.extend(outputs)

        # 🔧 保持文件原始顺序，不做排序（文件本身就是按时间顺序写入的）

        total = len(all_outputs)
        logger.info(f"[read_history_batch] 读取完成：总共 {total} 条，limit={limit}")

        # 🔧 删除offset逻辑：返回全部数据，由调用方分批发送
        batch_outputs = all_outputs

        logger.info(f"[read_history_batch] 返回全部数据：总共{len(batch_outputs)}条")

        # 更新订阅位置到文件末尾（避免重复发送历史）
        if client_id is not None and task_ids_to_read:
            if client_id in self._subscriptions:
                for task_id in task_ids_to_read:
                    file_path = self.get_output_file_path(task_id)
                    if file_path.exists():
                        # 记录文件末尾位置，下次只读取新内容
                        file_size = file_path.stat().st_size
                        self._subscriptions[client_id][task_id] = file_size
                        logger.debug(
                            f"[read_history_batch] 更新订阅位置：task-{task_id} -> {file_size}"
                        )

        # 🔧 master指示：删除 has_more 逻辑，时间戳是唯一标准
        return {
            "outputs": batch_outputs,
            "total": total,
        }

    async def read_history(
        self,
        task_ids: list[int] | None,
        callback: Callable[[dict[str, Any]], Awaitable[None]],
        client_id: int | None = None,
        limit: int = 500,
    ) -> None:
        """
        读取历史输出并通过回调发送（限制发送数量，避免性能问题）

        Args:
            task_ids: 事务ID列表，为空时自动发现
            callback: 回调函数，接收每行输出
            client_id: 客户端ID，用于获取读取位置（缓存机制）
            limit: 最大发送条数（默认500，从配置读取）
        """
        # 如果 task_ids 为空，自动发现
        if not task_ids:
            task_ids_to_read = self._discover_output_files()
        else:
            task_ids_to_read = task_ids

        logger.info(
            f"[read_history] 准备读取: task_ids={task_ids or '(自动发现)'}, "
            f"files={len(task_ids_to_read)}, client_id={client_id}, limit={limit}"
        )

        # 🚀 优化：使用read_last_n_output只读取最后N条，避免读取全部文件
        all_outputs = []
        for task_id in task_ids_to_read:
            # 每个事务读取limit*2条，排序后取limit条
            outputs = self.read_last_n_output(task_id, limit * 2)
            all_outputs.extend(outputs)

        # 按时间戳排序
        all_outputs.sort(key=lambda x: x.get("timestamp", ""))

        # 只保留最后limit条
        all_outputs = all_outputs[-limit:]

        logger.info(f"[read_history] 读取完成: 总共 {len(all_outputs)} 条（限制={limit}）")

        # 通过回调发送
        sent_count = 0
        for output in all_outputs:
            try:
                await callback(output)
                sent_count += 1
            except Exception as e:
                logger.error(f"[read_history] 回调历史输出失败: {e}")
                raise

        logger.info(f"[read_history] 发送完成: {sent_count} 条")

        # 更新订阅位置到文件末尾（避免重复发送历史）
        if client_id is not None and task_ids_to_read:
            async with self._get_lock():
                if client_id in self._subscriptions:
                    for task_id in task_ids_to_read:
                        file_path = self.get_output_file_path(task_id)
                        if file_path.exists():
                            # 记录文件末尾位置，下次只读取新内容
                            file_size = file_path.stat().st_size
                            self._subscriptions[client_id][task_id] = file_size
                            logger.debug(
                                f"[read_history] 更新订阅位置: task-{task_id} -> {file_size}"
                            )

    async def _watch_loop(self) -> None:
        """
        监控循环：检查所有订阅的文件是否有新内容，并发送给对应客户端
        同时定期重新扫描输出文件（发现新事务）
        """
        logger.info("🔍 输出文件监控循环已启动")

        while self._running:
            try:
                # 获取所有需要检查的文件及其订阅者
                async with self._get_lock():
                    # 🔧 只有"非明确订阅模式"的客户端才自动发现新文件
                    for client_id in self._subscriptions.keys():
                        is_explicit = self._explicit_task_subscriptions.get(client_id, False)

                        # 只有"非明确订阅模式"（自动发现模式）才扫描新文件
                        if not is_explicit:
                            current_task_ids = set(self._subscriptions[client_id].keys())
                            discovered_task_ids = self._discover_output_files()
                            new_files = []
                            for task_id in discovered_task_ids:
                                if task_id not in current_task_ids:
                                    # 新发现的文件，从位置 0 开始
                                    self._subscriptions[client_id][task_id] = 0
                                    new_files.append(task_id)
                            if new_files:
                                logger.info(
                                    f"客户端 {client_id} (自动发现模式) 动态发现 {len(new_files)} 个新输出文件: {new_files}"
                                )
                        else:
                            # "明确订阅模式"：不自动发现新文件，只订阅前端明确指定的 task_ids
                            logger.debug(
                                f"客户端 {client_id} (明确订阅模式) 跳过自动发现，只订阅前端指定的 tasks"
                            )

                    # 收集所有订阅：{task_id: {client_id: position}}
                    all_subs: dict[int, dict[int, int]] = {}
                    for client_id, subs in self._subscriptions.items():
                        for task_id, pos in subs.items():
                            if task_id not in all_subs:
                                all_subs[task_id] = {}
                            all_subs[task_id][client_id] = pos
                    callbacks = dict(self._callbacks)

                # 检查每个文件
                for task_id, client_positions in all_subs.items():
                    file_path = self.get_output_file_path(task_id)

                    if not file_path.exists():
                        continue

                    try:
                        file_size = file_path.stat().st_size
                    except OSError:
                        continue

                    # 找出最小位置（有新内容的最小起点）
                    min_pos = min(client_positions.values())
                    if file_size <= min_pos:
                        logger.debug(
                            f"文件 {file_path.name} 无新内容: size={file_size}, min_pos={min_pos}"
                        )
                        continue

                    # 读取新内容（在线程池中执行，避免阻塞事件循环）
                    try:
                        def _read_new_lines(fp: Path, seek_pos: int) -> tuple[list[tuple[int, dict[str, Any]]], int]:
                            lines = []
                            with open(fp, "rb") as f:
                                # 🚀 性能优化：分块二进制计数 seek_pos 之前的行数（C级速度）
                                f.seek(0)
                                prev_line_count = 0
                                remaining = seek_pos
                                while remaining > 0:
                                    chunk_size = min(1024 * 1024, remaining)
                                    chunk = f.read(chunk_size)
                                    if not chunk:
                                        break
                                    prev_line_count += chunk.count(b"\n")
                                    remaining -= len(chunk)
                                # 定位到 seek_pos，读取后续新内容
                                f.seek(seek_pos)
                                remaining_bytes = f.read()
                            # 解析新行（🔧 用 split('\n') 而非 splitlines，避免
                            # \x0b 等Unicode行分隔符导致错误分行）
                            remaining_text = remaining_bytes.decode("utf-8", errors="replace")
                            raw_split = remaining_text.split("\n")
                            if raw_split and raw_split[-1] == "":
                                raw_split.pop()
                            line_no = prev_line_count
                            offset = seek_pos
                            for raw_line in raw_split:
                                line_pos = offset
                                offset += len(raw_line.encode("utf-8")) + 1
                                line_no += 1
                                parsed = self.parse_output_line(raw_line + "\n")
                                if parsed:
                                    parsed["fileLineNo"] = line_no
                                    lines.append((line_pos, parsed))
                            return lines, offset

                        new_lines, max_pos = await asyncio.to_thread(_read_new_lines, file_path, min_pos)

                        for line_pos, parsed in new_lines:
                            for client_id, last_pos in client_positions.items():
                                # 只发送给还没读到这行的客户端
                                # 修改条件：使用 <= 而不是 <，因为客户端可能正好在行首位置
                                if last_pos <= line_pos:
                                    callback = callbacks.get(client_id)
                                    if callback:
                                        try:
                                            await callback(parsed)
                                        except Exception as e:
                                            logger.error(
                                                f"回调新输出失败: {e}\n{traceback.format_exc()}"
                                            )
                                    else:
                                        logger.warning(f"⚠️ 客户端 {client_id} 没有callback")
                                else:
                                    logger.debug(
                                        f"客户端 {client_id} last_pos={last_pos} > line_pos={line_pos}, 跳过发送"
                                    )

                        # 更新所有订阅者的位置
                        async with self._get_lock():
                            for client_id in client_positions:
                                if client_id in self._subscriptions:
                                    if task_id in self._subscriptions[client_id]:
                                        self._subscriptions[client_id][task_id] = max_pos
                                    else:
                                        logger.warning(
                                            f"⚠️ task-{task_id} 不在客户端 {client_id} 订阅中，现有task_ids: {list(self._subscriptions[client_id].keys())}"
                                        )
                                else:
                                    logger.warning(f"⚠️ 客户端 {client_id} 不在 _subscriptions 中")

                    except Exception as e:
                        logger.error(
                            f"读取新输出失败: {file_path}, 错误: {e}\n{traceback.format_exc()}"
                        )

                # 🚀 性能优化：监控循环间隔从0.3s增加到0.5s，降低CPU占用
                await asyncio.sleep(0.5)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"监控循环异常: {e}\n{traceback.format_exc()}")
                await asyncio.sleep(1)

        logger.info("🔍 输出文件监控循环已停止")

    async def start_watching(self) -> None:
        """启动监控事务"""
        if self._running:
            logger.warning("监控事务已在运行")
            return

        self._running = True
        self._watch_task = asyncio.create_task(self._watch_loop())
        logger.info("✅ 输出文件监控事务已启动")

    def is_subscription_stale(self, client_id: int, epoch: int) -> bool:
        """
        🔧 Task#1435：检查订阅是否已过期（客户端是否已切换到其他事务）

        Args:
            client_id: 客户端ID
            epoch: 调用 update_subscription 时捕获的 epoch 值

        Returns:
            True 如果 epoch 已过期（客户端已切换到其他事务）
        """
        current_epoch = self._subscription_epochs.get(client_id, 0)
        return current_epoch != epoch

    async def update_read_position(self, client_id: int, task_id: int, position: int) -> None:
        """
        更新单个文件的读取位置

        Args:
            client_id: 客户端ID
            task_id: 事务ID
            position: 新的读取位置（字节偏移）
        """
        async with self._get_lock():
            if client_id not in self._subscriptions:
                logger.warning(f"⚠️ 客户端 {client_id} 不在 _subscriptions 中")
                return

            self._subscriptions[client_id][task_id] = position
            logger.debug(f"更新读取位置: 客户端 {client_id}, task-{task_id} -> {position}")

    async def reset_positions(self, task_ids: list[int]) -> None:
        """
        重置指定事务的订阅位置（清空输出后调用）

        当输出文件被删除时，需要重置所有客户端对该文件的订阅位置为0，
        这样重新订阅后能从头开始读取新输出。

        Args:
            task_ids: 事务ID列表
        """
        async with self._get_lock():
            reset_count = 0
            for client_id in list(self._subscriptions.keys()):
                for task_id in task_ids:
                    if task_id in self._subscriptions[client_id]:
                        old_position = self._subscriptions[client_id][task_id]
                        self._subscriptions[client_id][task_id] = 0
                        reset_count += 1
                        logger.info(
                            f"重置读取位置: 客户端 {client_id}, task-{task_id} -> 0 (原位置: {old_position})"
                        )

            logger.info(f"已重置 {reset_count} 个订阅位置: task_ids={task_ids}")

    async def stop_watching(self) -> None:
        """停止监控事务"""
        self._running = False
        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
            self._watch_task = None
        logger.info("✅ 输出文件监控事务已停止")

    async def delete_output(self, task_ids: list[int]) -> int:
        """删除输出文件（仅用于手动清理，如 /clear 命令）"""
        deleted_count = 0
        for task_id in task_ids:
            file_path = self.get_output_file_path(task_id)
            if file_path.exists():
                try:
                    file_path.unlink()
                    deleted_count += 1
                    logger.info(f"已删除输出文件: {file_path}")
                except Exception as e:
                    logger.warning(f"删除输出文件失败: {file_path}, 错误: {e}")

        # 重置订阅位置，确保清空后重新订阅能从头开始读取
        await self.reset_positions(task_ids)
        return deleted_count


# 全局单例实例
_output_reader: OutputReader | None = None


def get_output_reader() -> OutputReader:
    """
    获取全局输出读取器实例

    Returns:
        OutputReader实例
    """
    global _output_reader
    if _output_reader is None:
        _output_reader = OutputReader()
    return _output_reader

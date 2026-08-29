"""
输出管理器

负责管理所有输出（进程输出、大模型输出、工具调用信息）的写入和存储。

功能：
1. 统一输出存储：所有输出写入结构化文件
2. 文件格式规范：类似CSV，每行包含时间戳、来源、事务ID、输出内容
3. 行缓冲机制：缓存输出直到遇到换行符
4. 线程安全：使用线程锁保护文件写入
5. 自动flush：确保文件内容最新
6. 支持多种输出类型：进程输出、大模型输出、工具调用、工具结果
"""

from __future__ import annotations

import logging
import os
import re
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from peng.common.colors import colorize, get_colors
from peng.common.paths import chown_to_project_owner, ensure_dir_with_owner
from peng.config import get_agent_name, get_master_name
from peng.config.loader import load_config

logger = logging.getLogger(__name__)
_COLORS = get_colors()


class OutputManager:
    """
    进程输出管理器（单例模式）

    所有进程的输出统一写入文件，支持：
    - 行缓冲：缓存输出直到遇到换行符
    - 线程安全：使用锁保护文件写入
    - 自动flush：确保文件内容最新
    - 文件格式：{timestamp}\t{source}\t{lineNo}\t{content}（🔧 Task#1725：第3字段为行号）
    - 文件命名：task-{task_id}.out
    - 旧格式（第3字段为task_id）在再次写入时自动迁移为行号格式
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        """单例模式"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """初始化输出管理器"""
        if hasattr(self, "_initialized"):
            return

        self._initialized = True
        self._write_lock = threading.Lock()

        # 缓冲区：{task_id: buffered_content}
        self._buffers: dict[int, str] = {}

        # 跟踪每个 task_id 的上一个 source，用于检测 source 变更
        # 当 source 变化时，需要先 flush 缓冲区，避免不同 source 的内容混在同一批行中
        self._last_source: dict[int, str] = {}

        # 跟踪每个 task_id 已连续写入的空行数，用于限制连续空行不超过1个
        self._consecutive_empty: dict[int, int] = {}

        # 🔧 Task#1725：行号计数器（行号=物理行位置，O(1)分配）
        self._next_line_no: dict[int, int] = {}
        # 行号计数器初始化时/最近的文件大小（检测外部清空 → 计数器重置）
        self._line_no_file_size: dict[int, int] = {}
        # 已确认是行号格式的文件（避免重复检测）
        self._line_no_format_ok: set[int] = set()

        # 加载配置
        try:
            config = load_config()
            self._output_dir = Path(config.paths.output_dir)
        except Exception as e:
            logger.warning(f"加载配置失败，使用默认输出目录: {e}\n{traceback.format_exc()}")
            self._output_dir = Path("data/output")

        # 确保输出目录存在
        self._ensure_output_dir()

        logger.info(f"OutputManager 初始化完成，输出目录: {self._output_dir}")

    def _ensure_output_dir(self) -> None:
        """确保输出目录存在"""
        try:
            ensure_dir_with_owner(self._output_dir)
        except Exception as e:
            logger.error(f"创建输出目录失败: {e}\n{traceback.format_exc()}")

    def get_output_file_path(self, task_id: int) -> Path:
        """
        获取输出文件路径

        Args:
            task_id: 事务ID

        Returns:
            输出文件路径
        """
        filename = f"task-{task_id}.out"
        return self._output_dir / filename

    def write_output(
        self,
        task_id: Any,
        source: str,
        content: str,
    ) -> None:
        """
        写入输出（行缓冲）

        Args:
            task_id: 事务ID
            source: 来源（task, tool, llm, system等，llm会被替换为thinking或answer）
            content: 输出内容
        """
        if not content:
            return

        buffer_key = int(task_id)

        with self._write_lock:
            # 检查 source 是否变化，如果变化则先 flush 缓冲区
            # 这样可以避免不同 source 的内容混在同一批行中被错误标记
            last_source = self._last_source.get(buffer_key)
            if last_source is not None and last_source != source:
                # source 变化了，将缓冲区中所有内容用旧 source 刷新
                if buffer_key in self._buffers and self._buffers[buffer_key]:
                    remaining = self._buffers[buffer_key]
                    lines_to_flush = []
                    while "\n" in remaining:
                        line_end = remaining.index("\n") + 1
                        line = remaining[:line_end]
                        lines_to_flush.append(line.rstrip("\n"))
                        remaining = remaining[line_end:]
                    if lines_to_flush:
                        self._write_lines(buffer_key, last_source, lines_to_flush)
                    # 未完成的行也用旧 source 写出（避免混入新 source）
                    if remaining.strip():
                        self._write_lines(buffer_key, last_source, [remaining])
                    self._buffers[buffer_key] = ""
                elif buffer_key not in self._buffers:
                    self._buffers[buffer_key] = ""

            # 更新 last_source
            self._last_source[buffer_key] = source

            # 将连续换行符压缩为单个换行符，减少输出中的空行
            content = re.sub(r"\n{2,}", "\n", content)

            # 添加到缓冲区
            if buffer_key not in self._buffers:
                self._buffers[buffer_key] = ""

            self._buffers[buffer_key] += content

            # 检查是否有完整的行（以\n结尾）
            buffered = self._buffers[buffer_key]

            # 找到所有完整的行
            lines_to_write = []
            remaining = buffered

            while "\n" in remaining:
                line_end = remaining.index("\n") + 1
                line = remaining[:line_end]
                lines_to_write.append(line.rstrip("\n"))
                remaining = remaining[line_end:]

            # 更新缓冲区（保留不完整的行）
            self._buffers[buffer_key] = remaining

            # 写入完整的行
            if lines_to_write:
                self._write_lines(task_id, source, lines_to_write)

    def flush_output(self, task_id: int, source: str = "task") -> None:
        """
        刷新缓冲区，将剩余内容写入文件

        Args:
            task_id: 事务ID
            source: 来源（默认为task）
        """
        buffer_key = task_id

        with self._write_lock:
            if buffer_key not in self._buffers:
                return

            remaining = self._buffers[buffer_key]
            if remaining:
                # 写入剩余内容
                self._write_lines(task_id, source, [remaining])
                self._buffers[buffer_key] = ""

    # ==================== 🔧 Task#1725：行号格式 ====================

    def _detect_line_no_format(self, file_path: Path) -> bool:
        """检测文件是否已是行号格式（第3字段为行号）。

        判定：前2行的第3字段分别为 '1'、'2'（行号从1连续）。
        旧格式第3字段是task_id（恒定值，不可能连续为1、2），至少需要2行才能可靠区分。
        空文件/不足2行时：单行看第3字段是否=='1'；全空视为新格式（无需迁移）。
        """
        try:
            if not file_path.exists() or file_path.stat().st_size == 0:
                return True
            with open(file_path, encoding="utf-8", errors="replace") as f:
                first = f.readline()
                if not first:
                    return True
                parts = first.rstrip("\n").split("\t")
                if len(parts) < 4 or parts[2] != "1":
                    return False
                second = f.readline()
                if not second:
                    return True  # 仅1行且第3字段==1 → 新格式
                parts = second.rstrip("\n").split("\t")
                return len(parts) >= 4 and parts[2] == "2"
        except Exception as e:
            logger.warning(f"行号格式检测失败: {file_path}, {e}")
            return False

    def _migrate_to_line_no_format(self, task_id: int, file_path: Path) -> int:
        """旧格式（第3字段task_id）原地迁移为行号格式（第3字段=物理行号）。

        Returns: 迁移后的总行数（作为行号计数器初始值）
        写端独占写时才调用（此时本进程是该文件唯一写者），rename 原子替换。
        """
        total = 0
        tmp_path = file_path.with_suffix(".out.migrating")
        try:
            with open(file_path, encoding="utf-8", errors="replace") as fin, \
                 open(tmp_path, "w", encoding="utf-8") as fout:
                for line in fin:
                    total += 1
                    raw = line.rstrip("\n")
                    parts = raw.split("\t")
                    if len(parts) >= 4:
                        new_raw = parts[0] + "\t" + parts[1] + "\t" + str(total) + "\t" + "\t".join(parts[3:])
                    else:
                        new_raw = raw  # 异常行原样保留
                    fout.write(new_raw + "\n")
            os.replace(tmp_path, file_path)
            logger.info(f"[Task#1725] 输出文件已迁移为行号格式: task={task_id}, {total}行")
            return total
        except Exception as e:
            logger.error(f"行号格式迁移失败: {file_path}, {e}\n{traceback.format_exc()}")
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            return 0

    def _ensure_line_no_ready(self, task_id: int, file_path: Path) -> None:
        """确保行号计数器就绪（在 _write_lock 保护下调用）。

        - 首次写该文件：检测格式；旧格式先迁移；计数器=总行数+1
        - 后续：检测文件被外部清空（size<记录值）→ 计数器重置
        """
        # 已初始化：检查外部清空
        if task_id in self._next_line_no:
            try:
                size = file_path.stat().st_size if file_path.exists() else 0
                if size < self._line_no_file_size.get(task_id, 0):
                    # 文件被清空/截断 → 重置计数器从1开始
                    self._next_line_no[task_id] = 1
                    self._line_no_file_size[task_id] = 0
                    self._line_no_format_ok.discard(task_id)
            except OSError:
                pass
            return

        # 首次：格式检测 + 迁移 + 计数器初始化
        total = 0
        if file_path.exists() and file_path.stat().st_size > 0:
            if not self._detect_line_no_format(file_path):
                total = self._migrate_to_line_no_format(task_id, file_path)
                if total == 0:
                    # 迁移失败 → 按当前实际行数初始化，避免行号错乱
                    total = self._count_lines_fallback(file_path)
            else:
                total = self._count_lines_fallback(file_path)
            self._line_no_format_ok.add(task_id)
        self._next_line_no[task_id] = total + 1
        try:
            self._line_no_file_size[task_id] = file_path.stat().st_size if file_path.exists() else 0
        except OSError:
            self._line_no_file_size[task_id] = 0

    def _count_lines_fallback(self, file_path: Path) -> int:
        """行数统计兜底（分块计数）。"""
        count = 0
        try:
            with open(file_path, "rb") as f:
                while True:
                    buf = f.read(1024 * 1024)
                    if not buf:
                        break
                    count += buf.count(b"\n")
        except Exception as e:
            logger.warning(f"行数统计失败: {file_path}, {e}")
        return count

    def _write_lines(
        self,
        task_id: int,
        source: str,
        lines: list[str],
    ) -> None:
        """
        写入多行输出到文件（内部方法，不线程安全）

        Args:
            task_id: 事务ID
            source: 来源（llm会被替换为thinking或answer）
            lines: 行列表（不含换行符）
        """
        if not lines:
            return

        # 过滤连续空行：最多保留1个连续空行
        filtered_lines: list[str] = []
        consecutive = self._consecutive_empty.get(int(task_id), 0)
        for line in lines:
            if not line.strip():
                consecutive += 1
                if consecutive > 1:
                    continue  # 跳过超出限制的连续空行
            else:
                consecutive = 0
            filtered_lines.append(line)
        self._consecutive_empty[int(task_id)] = consecutive

        if not filtered_lines:
            return

        file_path = self.get_output_file_path(task_id)

        try:
            # 🔧 Task#1725：确保行号计数器就绪（含旧格式自动迁移）
            self._ensure_line_no_ready(int(task_id), file_path)

            # 追加模式打开文件
            need_chown = not file_path.exists()
            with open(file_path, "a", encoding="utf-8") as f:
                for line in filtered_lines:
                    # 不跳过空行，而是将空行写入为空格占位符
                    if not line.strip():
                        line = " "  # 保留空格占位符
                    # 格式：{timestamp}\t{source}\t{lineNo}\t{content}
                    # 🔧 Task#1725：第3字段为行号（=物理行位置，O(1)分配）
                    timestamp = datetime.now().isoformat()
                    if source in ["thinking", "answer"]:
                        color = (
                            _COLORS.agent("thinking")
                            if source == "thinking"
                            else _COLORS.models("big_model")
                        )
                        line = colorize(line, color)
                    line_no = self._next_line_no[int(task_id)]
                    self._next_line_no[int(task_id)] = line_no + 1
                    formatted_line = f"{timestamp}\t{source}\t{line_no}\t{line}\n"

                    # 中文标点保持原样输出，不再转换为英文标点
                    f.write(formatted_line)

                # 立即flush确保文件内容最新
                f.flush()

            # 🔧 Task#1725：记录写入后大小（外部清空检测基准）
            try:
                self._line_no_file_size[int(task_id)] = file_path.stat().st_size
            except OSError:
                pass

            # 新建的文件需要设置 owner（root 运行时文件会归 root 所有）
            if need_chown:
                chown_to_project_owner(file_path)

        except Exception as e:
            logger.error(f"写入输出文件失败: {file_path}\n错误: {e}\n{traceback.format_exc()}")

    def clear_buffer(self, task_id: int) -> None:
        """
        清除指定事务的缓冲区（不写入文件）

        Args:
            task_id: 事务ID
        """
        buffer_key = task_id

        with self._write_lock:
            if buffer_key in self._buffers:
                del self._buffers[buffer_key]
            if buffer_key in self._last_source:
                del self._last_source[buffer_key]
            if buffer_key in self._consecutive_empty:
                del self._consecutive_empty[buffer_key]

    def flush_all_buffers(self, task_id: int) -> None:
        """
        刷新指定事务的所有输出缓冲区

        Args:
            task_id: 事务ID
        """
        # 刷新普通输出缓冲区
        self.flush_output(task_id, "task")

    def write_tool_call(
        self,
        task_id: int,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_call_id: str = "",
    ) -> None:
        """
        写入工具调用信息

        Args:
            task_id: 事务ID
            tool_name: 工具名称
            tool_args: 工具参数
            tool_call_id: 工具调用ID
        """
        import json

        try:
            args_str = json.dumps(tool_args, ensure_ascii=False)
        except Exception:
            args_str = str(tool_args)

        content = f"🔧 {tool_name}"
        if tool_call_id:
            content += f" (id={tool_call_id})"
        content += f" args={args_str}"

        # 确保以换行符结尾
        if not content.endswith("\n"):
            content += "\n"
        self.write_output(task_id, "tool_call", content)

    def record_conversation(
        self,
        task_id: int,
        sender: str,
        content: str,
    ) -> None:
        """
        记录对话消息（用户消息或大模型回复）

        Args:
            task_id: 事务ID
            sender: 发送者（user/assistant）
            content: 消息内容
        """
        if not content:
            return

        # 将多行消息分割为行
        lines = content.split("\n")

        # 第一行加上发送者前缀
        if lines:
            master_name = get_master_name()
            agent_name = get_agent_name()
            sender_name = master_name if sender == "user" else agent_name
            lines[0] = f"{sender_name}：{lines[0]}"

        # 写入所有行
        for line in lines:
            # 不跳过空行，而是将空行写入为空格占位符
            if not line.strip():
                line = " "

            # 确保以换行符结尾
            line_with_newline = line + "\n"
            self.write_output(task_id, "conversation", line_with_newline)


# 全局单例实例
_output_manager: OutputManager | None = None


def get_output_manager() -> OutputManager:
    """
    获取全局输出管理器实例

    Returns:
        OutputManager实例
    """
    global _output_manager
    if _output_manager is None:
        _output_manager = OutputManager()
    return _output_manager

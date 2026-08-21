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
    - 文件格式：{timestamp}\t{source}\t{task_id}\t{content}
    - 文件命名：task-{task_id}.out
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
            # 追加模式打开文件
            need_chown = not file_path.exists()
            with open(file_path, "a", encoding="utf-8") as f:
                for line in filtered_lines:
                    # 不跳过空行，而是将空行写入为空格占位符
                    if not line.strip():
                        line = " "  # 保留空格占位符
                    # 格式：{timestamp}\t{source}\t{task_id}\t{content}
                    timestamp = datetime.now().isoformat()
                    if source in ["thinking", "answer"]:
                        color = (
                            _COLORS.agent("thinking")
                            if source == "thinking"
                            else _COLORS.models("big_model")
                        )
                        line = colorize(line, color)
                    formatted_line = f"{timestamp}\t{source}\t{task_id}\t{line}\n"

                    # 中文标点保持原样输出，不再转换为英文标点
                    f.write(formatted_line)

                # 立即flush确保文件内容最新
                f.flush()

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

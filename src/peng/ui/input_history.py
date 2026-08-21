"""
输入历史管理器

支持无限 Undo/Redo 功能，避免递归锁死，支持持久化存储。
单一无限历史栈：所有字符变化都追加，undo 可以回溯到所有历史。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from peng.common.paths import chown_to_project_owner, ensure_dir_with_owner

_logger = logging.getLogger(__name__)


@dataclass
class HistoryEntry:
    """历史记录条目"""

    text: str
    cursor_position: int
    timestamp: float = field(default_factory=lambda: 0.0)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "text": self.text,
            "cursor_position": self.cursor_position,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HistoryEntry:
        """从字典创建"""
        return cls(
            text=data["text"],
            cursor_position=data["cursor_position"],
            timestamp=data.get("timestamp", 0.0),
        )


class InputHistory:
    """
    输入历史管理器

    提供无限 Undo/Redo 功能，支持持久化存储。
    
    单一无限历史栈设计：
    - 所有字符变化都追加到 _history_stack
    - _current_index 指向当前位置
    - undo：index 向前移动
    - redo：index 向后移动
    - 新输入：截断 index 之后的历史，追加新状态
    """

    def __init__(
        self,
        history_file: Path | None = None,
        max_size: int | None = None,
    ):
        """
        初始化输入历史管理器

        Args:
            history_file: 历史记录文件路径（可选）
            max_size: 最大历史数量（None=无限）
        """
        self.history_file = history_file
        self.max_size = max_size

        # 单一历史栈
        self._history_stack: list[HistoryEntry] = []
        # 当前位置索引（-1 表示没有历史）
        self._current_index: int = -1

        # 递归锁死防护
        self._is_undoing = False

        # 当前状态（用于比较）
        self._current_text: str = ""
        self._current_cursor: int = 0

    def push(self, text: str, cursor_position: int) -> None:
        """
        记录变更到历史（字符级变化记录，实时持久化）

        Args:
            text: 当前文本
            cursor_position: 光标位置

        注意：
        - 如果 _is_undoing 为 True，不记录（避免递归）
        - 如果文本与上次相同，不记录
        - 🔧 每次字符变化都会实时追加到持久化文件
        """
        if self._is_undoing:
            _logger.debug(f"[push] 跳过记录（正在 undo/redo）: text={text[:20]}")
            return

        # 如果文本与上次相同，不记录
        if text == self._current_text and cursor_position == self._current_cursor:
            _logger.debug(f"[push] 跳过记录（文本未变）: text={text[:20]}")
            return

        # 更新当前状态
        self._current_text = text
        self._current_cursor = cursor_position

        # 创建历史记录条目
        entry = HistoryEntry(
            text=text,
            cursor_position=cursor_position,
            timestamp=time.time(),
        )

        _logger.debug(f"[push] 记录历史：text={text[:20]}, cursor={cursor_position}")

        # 🔧 不截断！直接追加新状态到历史栈
        self._history_stack.append(entry)
        self._current_index = len(self._history_stack) - 1

        # 限制历史大小（只移除最旧的）
        if self.max_size and len(self._history_stack) > self.max_size:
            self._history_stack.pop(0)
            self._current_index = len(self._history_stack) - 1

        # 🔧 实时持久化
        self._save_entry(entry)

    def undo(self) -> tuple[str, int] | None:
        """
        撤销到上一个状态

        Returns:
            (text, cursor_position) 或 None（如果没有历史）
        """
        if self._current_index < 0:
            _logger.debug("[undo] 没有历史记录")
            return None

        # 标记正在执行 undo
        self._is_undoing = True
        try:
            if self._current_index > 0:
                # 向前移动索引
                self._current_index -= 1
                entry = self._history_stack[self._current_index]
                self._current_text = entry.text
                self._current_cursor = entry.cursor_position
                _logger.debug(f"[undo] 撤销到 index={self._current_index}: text={entry.text[:20]}")
                return entry.text, entry.cursor_position
            else:
                # 已经到达最开始（空状态）
                self._current_index = -1
                self._current_text = ""
                self._current_cursor = 0
                _logger.debug("[undo] 已到达最开始")
                return "", 0
        finally:
            # 清除标记
            self._is_undoing = False

    def redo(self) -> tuple[str, int] | None:
        """
        重做被撤销的操作

        Returns:
            (text, cursor_position) 或 None（如果没有重做历史）
        """
        if self._current_index >= len(self._history_stack) - 1:
            _logger.debug("[redo] 没有重做历史")
            return None

        # 标记正在执行 redo
        self._is_undoing = True
        try:
            # 向后移动索引
            self._current_index += 1
            entry = self._history_stack[self._current_index]
            self._current_text = entry.text
            self._current_cursor = entry.cursor_position
            _logger.debug(f"[redo] 重做到 index={self._current_index}: text={entry.text[:20]}")
            return entry.text, entry.cursor_position
        finally:
            # 清除标记
            self._is_undoing = False

    def _save_entry(self, entry: HistoryEntry) -> None:
        """保存单个条目到文件（追加模式）"""
        if not self.history_file:
            return

        try:
            # 确保目录存在
            ensure_dir_with_owner(self.history_file.parent)

            # 追加到文件
            line = json.dumps(entry.to_dict(), ensure_ascii=False)
            with open(self.history_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            chown_to_project_owner(self.history_file)

            _logger.debug(f"[_save_entry] 追加历史到 {self.history_file}")
        except Exception as e:
            _logger.error(f"[_save_entry] 保存失败：{e}")

    def save(self) -> None:
        """
        保存历史记录到磁盘（追加模式）
        
        注意：现在 push 时已实时保存，此方法主要用于兼容性
        """
        if not self.history_file:
            _logger.debug("[save] 没有设置历史文件")
            return

        _logger.info(f"[save] 历史已实时保存到 {self.history_file}")

    def load(self) -> bool:
        """
        从磁盘加载历史记录（按行读取所有历史）

        Returns:
            是否成功加载
        """
        if not self.history_file or not self.history_file.exists():
            _logger.debug(f"[load] 历史文件不存在：{self.history_file}")
            return False

        try:
            # 🔧 按行读取所有历史记录
            with open(self.history_file, "r", encoding="utf-8") as f:
                lines = f.readlines()

            # 解析每一行作为一个历史条目
            self._history_stack = []
            for line in lines:
                line = line.strip()
                if line:
                    try:
                        data = json.loads(line)
                        entry = HistoryEntry.from_dict(data)
                        self._history_stack.append(entry)
                    except json.JSONDecodeError as e:
                        _logger.warning(f"[load] 跳过无效行：{e}")
                        continue

            # 恢复当前状态（使用栈顶状态）
            if self._history_stack:
                self._current_index = len(self._history_stack) - 1
                self._current_text = self._history_stack[-1].text
                self._current_cursor = self._history_stack[-1].cursor_position
            else:
                self._current_index = -1
                self._current_text = ""
                self._current_cursor = 0

            _logger.info(f"[load] 已加载 {len(self._history_stack)} 条历史从 {self.history_file}")
            return True
        except Exception as e:
            _logger.error(f"[load] 加载失败：{e}")
            return False

    def clear(self) -> None:
        """清空历史记录"""
        self._history_stack.clear()
        self._current_index = -1
        self._current_text = ""
        self._current_cursor = 0
        _logger.debug("[clear] 已清空历史")

    @property
    def can_undo(self) -> bool:
        """是否可以撤销"""
        return self._current_index >= 0

    @property
    def can_redo(self) -> bool:
        """是否可以重做"""
        return self._current_index < len(self._history_stack) - 1

    @property
    def undo_count(self) -> int:
        """历史数量"""
        return self._current_index + 1

    @property
    def redo_count(self) -> int:
        """重做数量"""
        return len(self._history_stack) - self._current_index - 1


# 全局历史管理器实例（可选）
_global_history: InputHistory | None = None


def get_input_history() -> InputHistory | None:
    """获取全局输入历史管理器"""
    return _global_history


def setup_input_history(history_file: Path | None = None, max_size: int | None = None) -> InputHistory:
    """
    设置全局输入历史管理器

    Args:
        history_file: 历史记录文件路径
        max_size: 最大历史数量

    Returns:
        输入历史管理器实例
    """
    global _global_history

    _global_history = InputHistory(history_file=history_file, max_size=max_size)
    return _global_history


def cleanup_input_history() -> None:
    """清理全局输入历史管理器"""
    global _global_history

    if _global_history:
        _global_history.save()
        _global_history = None

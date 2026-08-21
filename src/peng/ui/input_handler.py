"""
键盘事件处理模块

需求 §4.8-4.9: 交互命令和快捷键

提供 Esc 和 Ctrl+C 的处理：
- 快速按2次 Esc：暂停当前消息链
- 按1次 Ctrl+C：暂停当前消息链
- 连续再按1次 Ctrl+C：取消当前消息链并退出
"""

from __future__ import annotations

import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto

from rich.console import Console

console = Console()


class InterruptAction(Enum):
    """中断动作"""

    NONE = auto()
    PAUSE = auto()
    CANCEL = auto()


@dataclass
class InputHandlerConfig:
    """输入处理器配置"""

    esc_double_press_threshold: float = 0.5  # 双击 Esc 阈值（秒）
    ctrl_c_double_press_threshold: float = 1.0  # 双击 Ctrl+C 阈值（秒）


class InputHandler:
    """
    键盘输入处理器

    处理 Esc 和 Ctrl+C 的暂停/取消逻辑。
    """

    def __init__(
        self,
        on_pause: Callable[[], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
        on_resume: Callable[[], None] | None = None,
        config: InputHandlerConfig | None = None,
    ):
        """
        初始化输入处理器

        Args:
            on_pause: 暂停回调
            on_cancel: 取消回调
            on_resume: 恢复回调
            config: 配置
        """
        self.on_pause = on_pause
        self.on_cancel = on_cancel
        self.on_resume = on_resume
        self.config = config or InputHandlerConfig()

        # 状态追踪
        self._last_esc_time: float = 0
        self._last_ctrl_c_time: float = 0
        self._is_paused: bool = False
        self._ctrl_c_count: int = 0

        # 原始信号处理器
        self._original_sigint_handler = None

    def install(self) -> None:
        """安装信号处理器"""
        self._original_sigint_handler = signal.signal(signal.SIGINT, self._handle_sigint)

    def uninstall(self) -> None:
        """恢复原始信号处理器"""
        if self._original_sigint_handler:
            signal.signal(signal.SIGINT, self._original_sigint_handler)
            self._original_sigint_handler = None

    def _handle_sigint(self, signum, frame) -> None:
        """处理 Ctrl+C 信号"""
        now = time.time()

        if self._is_paused:
            # 已暂停状态下按 Ctrl+C
            if now - self._last_ctrl_c_time < self.config.ctrl_c_double_press_threshold:
                # 连续第二次 Ctrl+C：取消
                console.print("\n[red]取消当前消息链[/red]")
                self._is_paused = False
                self._ctrl_c_count = 0
                if self.on_cancel:
                    self.on_cancel()
                # 恢复原始处理器并重新触发
                self.uninstall()
                # 如果有原始处理器，调用它
                if self._original_sigint_handler:
                    signal.signal(signal.SIGINT, self._original_sigint_handler)
                    raise KeyboardInterrupt
                sys.exit(1)
            else:
                # 第一次 Ctrl+C（暂停状态）：提示
                self._ctrl_c_count += 1
                console.print("\n[yellow]再按一次 Ctrl+C 取消并退出[/yellow]")
        else:
            # 未暂停状态下按 Ctrl+C：暂停
            self._is_paused = True
            self._last_ctrl_c_time = now
            self._ctrl_c_count = 1
            console.print("\n[yellow]消息链已暂停，输入消息继续或再按 Ctrl+C 取消[/yellow]")
            if self.on_pause:
                self.on_pause()

    def handle_esc(self) -> InterruptAction:
        """
        处理 Esc 键

        Returns:
            触发的动作
        """
        now = time.time()

        if now - self._last_esc_time < self.config.esc_double_press_threshold:
            # 双击 Esc：暂停
            self._last_esc_time = 0
            self._is_paused = True
            console.print("\n[yellow]消息链已暂停，输入消息继续[/yellow]")
            if self.on_pause:
                self.on_pause()
            return InterruptAction.PAUSE
        # 第一次 Esc：记录时间
        self._last_esc_time = now
        return InterruptAction.NONE

    def resume(self) -> bool:
        """
        恢复暂停的消息链

        Returns:
            是否成功恢复
        """
        if self._is_paused:
            self._is_paused = False
            self._ctrl_c_count = 0
            console.print("[green]继续处理消息链[/green]")
            if self.on_resume:
                self.on_resume()
            return True
        return False

    @property
    def is_paused(self) -> bool:
        """是否处于暂停状态"""
        return self._is_paused

    def check_input_resume(self, user_input: str) -> bool:
        """
        检查用户输入是否表示恢复

        Args:
            user_input: 用户输入

        Returns:
            是否恢复
        """
        if self._is_paused and user_input.strip():
            # 暂停状态下，任何非空输入都表示恢复
            return self.resume()
        return False


# 全局输入处理器实例
_global_handler: InputHandler | None = None


def get_input_handler() -> InputHandler | None:
    """获取全局输入处理器"""
    return _global_handler


def setup_input_handler(
    on_pause: Callable[[], None] | None = None,
    on_cancel: Callable[[], None] | None = None,
    on_resume: Callable[[], None] | None = None,
    config: InputHandlerConfig | None = None,
) -> InputHandler:
    """
    设置全局输入处理器

    Args:
        on_pause: 暂停回调
        on_cancel: 取消回调
        on_resume: 恢复回调
        config: 配置

    Returns:
        输入处理器实例
    """
    global _global_handler

    _global_handler = InputHandler(
        on_pause=on_pause,
        on_cancel=on_cancel,
        on_resume=on_resume,
        config=config,
    )
    _global_handler.install()
    return _global_handler


def cleanup_input_handler() -> None:
    """清理全局输入处理器"""
    global _global_handler

    if _global_handler:
        _global_handler.uninstall()
        _global_handler = None

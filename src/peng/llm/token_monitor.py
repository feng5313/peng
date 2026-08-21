"""
Token 优化提醒

基于 estimate_tokens 添加阈值提醒，超过阈值时发出警告。
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)


class TokenWarningLevel(Enum):
    """Token 警告级别"""

    NORMAL = "normal"  # 正常
    WARNING = "warning"  # 警告（接近阈值）
    CRITICAL = "critical"  # 严重（超过阈值）


@dataclass
class TokenStatus:
    """Token 状态"""

    current_tokens: int
    max_tokens: int
    warning_threshold: float  # 百分比，如 0.7 = 70%
    critical_threshold: float  # 百分比，如 0.9 = 90%
    level: TokenWarningLevel
    usage_percent: float
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def is_warning(self) -> bool:
        """是否处于警告状态"""
        return self.level in (TokenWarningLevel.WARNING, TokenWarningLevel.CRITICAL)

    @property
    def remaining_tokens(self) -> int:
        """剩余可用 tokens"""
        return max(0, self.max_tokens - self.current_tokens)

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "current_tokens": self.current_tokens,
            "max_tokens": self.max_tokens,
            "usage_percent": f"{self.usage_percent:.1f}%",
            "level": self.level.value,
            "remaining_tokens": self.remaining_tokens,
        }


class TokenMonitor:
    """
    Token 监控器

    监控 token 使用量，在超过阈值时发出警告。
    """

    def __init__(
        self,
        max_tokens: int = 128000,
        warning_threshold: float = 0.7,
        critical_threshold: float = 0.9,
        on_warning: Callable[[TokenStatus], None] | None = None,
        on_critical: Callable[[TokenStatus], None] | None = None,
    ):
        """
        初始化 Token 监控器

        Args:
            max_tokens: 最大 token 数量
            warning_threshold: 警告阈值（百分比）
            critical_threshold: 严重阈值（百分比）
            on_warning: 警告回调
            on_critical: 严重回调
        """
        self.max_tokens = max_tokens
        self.warning_threshold = warning_threshold
        self.critical_threshold = critical_threshold
        self.on_warning = on_warning
        self.on_critical = on_critical
        self._last_status: TokenStatus | None = None
        self._last_level = TokenWarningLevel.NORMAL

    def check(self, current_tokens: int) -> TokenStatus:
        """
        检查 token 使用情况

        Args:
            current_tokens: 当前使用的 token 数量

        Returns:
            TokenStatus: Token 状态
        """
        usage_percent = current_tokens / self.max_tokens if self.max_tokens > 0 else 0

        # 确定警告级别
        if usage_percent >= self.critical_threshold:
            level = TokenWarningLevel.CRITICAL
        elif usage_percent >= self.warning_threshold:
            level = TokenWarningLevel.WARNING
        else:
            level = TokenWarningLevel.NORMAL

        status = TokenStatus(
            current_tokens=current_tokens,
            max_tokens=self.max_tokens,
            warning_threshold=self.warning_threshold,
            critical_threshold=self.critical_threshold,
            level=level,
            usage_percent=usage_percent * 100,
        )

        # 触发回调（仅当级别变化时）
        self._trigger_callbacks(status)
        self._last_status = status
        self._last_level = level

        return status

    def _trigger_callbacks(self, status: TokenStatus) -> None:
        """触发回调"""
        # 严重级别
        if status.level == TokenWarningLevel.CRITICAL:
            if self._last_level != TokenWarningLevel.CRITICAL:
                logger.warning(
                    f"Token 使用量严重超标: {status.current_tokens}/{status.max_tokens} "
                    f"({status.usage_percent:.1f}%)"
                )
                if self.on_critical:
                    self.on_critical(status)

        # 警告级别
        elif status.level == TokenWarningLevel.WARNING:
            if self._last_level == TokenWarningLevel.NORMAL:
                logger.warning(
                    f"Token 使用量接近阈值: {status.current_tokens}/{status.max_tokens} "
                    f"({status.usage_percent:.1f}%)"
                )
                if self.on_warning:
                    self.on_warning(status)

    @property
    def last_status(self) -> TokenStatus | None:
        """获取最后一次状态"""
        return self._last_status

    def reset(self) -> None:
        """重置监控器状态"""
        self._last_status = None
        self._last_level = TokenWarningLevel.NORMAL


def create_default_monitor(
    max_tokens: int = 128000,
) -> TokenMonitor:
    """
    创建默认的 Token 监控器

    Args:
        max_tokens: 最大 token 数量

    Returns:
        TokenMonitor 实例
    """

    def on_warning(status: TokenStatus) -> None:
        print(
            f"\n⚠️  Token 警告: 已使用 {status.usage_percent:.1f}% "
            f"({status.current_tokens:,} tokens)"
        )
        print(f"    剩余 {status.remaining_tokens:,} tokens，建议精简对话历史")

    def on_critical(status: TokenStatus) -> None:
        print(
            f"\n🚨 Token 严重警告: 已使用 {status.usage_percent:.1f}% "
            f"({status.current_tokens:,} tokens)"
        )
        print(f"    剩余仅 {status.remaining_tokens:,} tokens，请立即精简对话历史！")

    return TokenMonitor(
        max_tokens=max_tokens,
        warning_threshold=0.7,
        critical_threshold=0.9,
        on_warning=on_warning,
        on_critical=on_critical,
    )


# 全局监控器实例（可被覆盖）
_global_monitor: TokenMonitor | None = None


def get_token_monitor() -> TokenMonitor:
    """获取全局 Token 监控器"""
    global _global_monitor
    if _global_monitor is None:
        _global_monitor = create_default_monitor()
    return _global_monitor


def set_token_monitor(monitor: TokenMonitor) -> None:
    """设置全局 Token 监控器"""
    global _global_monitor
    _global_monitor = monitor

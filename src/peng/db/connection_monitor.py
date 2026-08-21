#!/usr/bin/env python3
"""
PostgreSQL连接池监控和自动清理模块

功能：
1. 监控连接池状态
2. 自动清理无效连接
3. 定期输出连接池状态信息
4. 自动告警机制
5. 连接泄漏检测
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger(__name__)

# 全局变量：监控线程
_monitor_thread: threading.Thread | None = None
_monitor_running = False
_monitor_interval = 60  # 监控间隔（秒）
_monitor_enabled = False

# 连接池引用
_pool: "ThreadedConnectionPool | None" = None

# 连接泄漏检测
_connection_leak_detector: "ConnectionLeakDetector | None" = None

# 告警管理器
_alert_manager: "AlertManager | None" = None


@dataclass
class ConnectionPoolStatus:
    """连接池状态"""

    total_connections: int = 0
    active_connections: int = 0
    idle_connections: int = 0
    idle_in_transaction_connections: int = 0
    max_connections: int = 0
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "total_connections": self.total_connections,
            "active_connections": self.active_connections,
            "idle_connections": self.idle_connections,
            "idle_in_transaction_connections": self.idle_in_transaction_connections,
            "max_connections": self.max_connections,
            "utilization": self.total_connections / self.max_connections
            if self.max_connections > 0
            else 0,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class Alert:
    """告警"""

    level: str  # info, warning, error, critical
    title: str
    message: str
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "level": self.level,
            "title": self.title,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
        }


class AlertManager:
    """告警管理器"""

    def __init__(self, max_alerts: int = 100):
        """
        初始化告警管理器

        Args:
            max_alerts: 最大告警数量
        """
        self.max_alerts = max_alerts
        self.alerts: list[Alert] = []
        self.alert_lock = threading.Lock()

        # 告警阈值配置
        self.thresholds = {
            "connection_count_warning": 0.7,  # 连接数超过70%告警
            "connection_count_critical": 0.9,  # 连接数超过90%严重告警
            "idle_in_transaction_warning": 1,  # 超过1个阻塞连接告警
            "idle_in_transaction_critical": 5,  # 超过5个阻塞连接严重告警
        }

    def add_alert(self, alert: Alert) -> None:
        """
        添加告警

        Args:
            alert: 告警
        """
        with self.alert_lock:
            self.alerts.append(alert)
            if len(self.alerts) > self.max_alerts:
                self.alerts.pop(0)

        # 输出告警日志
        if alert.level == "critical":
            logger.critical(f"🚨 [CRITICAL] {alert.title}: {alert.message}")
        elif alert.level == "error":
            logger.error(f"🔴 [ERROR] {alert.title}: {alert.message}")
        elif alert.level == "warning":
            logger.warning(f"🟡 [WARNING] {alert.title}: {alert.message}")
        else:
            logger.info(f"🔵 [INFO] {alert.title}: {alert.message}")

    def get_alerts(
        self,
        level: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Alert]:
        """
        获取告警列表

        Args:
            level: 告警级别过滤
            since: 起始时间
            limit: 最大数量

        Returns:
            告警列表
        """
        with self.alert_lock:
            alerts = self.alerts.copy()

        if level:
            alerts = [a for a in alerts if a.level == level]

        if since:
            alerts = [a for a in alerts if a.timestamp >= since]

        if limit:
            alerts = alerts[:limit]

        return alerts

    def clear_alerts(self, before: datetime | None = None) -> int:
        """
        清除告警

        Args:
            before: 清除此时间之前的告警

        Returns:
            清除的告警数量
        """
        with self.alert_lock:
            if before is None:
                count = len(self.alerts)
                self.alerts.clear()
                return count

            original_count = len(self.alerts)
            self.alerts = [a for a in self.alerts if a.timestamp >= before]
            return original_count - len(self.alerts)


class ConnectionLeakDetector:
    """连接泄漏检测器"""

    def __init__(self, window_size: int = 10, threshold: float = 0.5):
        """
        初始化连接泄漏检测器

        Args:
            window_size: 检测窗口大小（次）
            threshold: 泄漏阈值（0.5表示连接数增长超过50%）
        """
        self.window_size = window_size
        self.threshold = threshold
        self.history: list[ConnectionPoolStatus] = []
        self.history_lock = threading.Lock()

    def add_status(self, status: ConnectionPoolStatus) -> None:
        """
        添加连接池状态

        Args:
            status: 连接池状态
        """
        with self.history_lock:
            self.history.append(status)
            if len(self.history) > self.window_size:
                self.history.pop(0)

    def detect_leak(self) -> list[Alert]:
        """
        检测连接泄漏

        Returns:
            告警列表
        """
        alerts = []

        with self.history_lock:
            if len(self.history) < 2:
                return alerts

            # 检查连接数是否持续增长
            first = self.history[0]
            last = self.history[-1]

            if first.total_connections > 0:
                growth_rate = (
                    last.total_connections - first.total_connections
                ) / first.total_connections

                if growth_rate > self.threshold:
                    alerts.append(
                        Alert(
                            level="warning",
                            title="检测到连接泄漏",
                            message=f"连接数从 {first.total_connections} 增长到 {last.total_connections} "
                            f"（增长 {growth_rate * 100:.1f}%），可能存在连接泄漏",
                            metadata={
                                "first_status": first.to_dict(),
                                "last_status": last.to_dict(),
                                "growth_rate": growth_rate,
                            },
                        )
                    )

            # 检查 idle in transaction 连接是否持续增长
            first_idle_in_transaction = first.idle_in_transaction_connections
            last_idle_in_transaction = last.idle_in_transaction_connections

            if last_idle_in_transaction > 0 and first_idle_in_transaction > 0:
                idle_in_transaction_growth = (
                    last_idle_in_transaction - first_idle_in_transaction
                ) / first_idle_in_transaction

                if idle_in_transaction_growth > 0:
                    alerts.append(
                        Alert(
                            level="critical",
                            title="检测到阻塞连接持续增长",
                            message=f"idle in transaction 连接从 {first_idle_in_transaction} 增长到 "
                            f"{last_idle_in_transaction}（增长 {idle_in_transaction_growth * 100:.1f}%），"
                            "可能存在连接泄漏或长时间运行的事务",
                            metadata={
                                "first_idle_in_transaction": first_idle_in_transaction,
                                "last_idle_in_transaction": last_idle_in_transaction,
                                "idle_in_transaction_growth": idle_in_transaction_growth,
                            },
                        )
                    )

        return alerts


def set_pool(pool: "ThreadedConnectionPool | None") -> None:
    """设置要监控的连接池"""
    global _pool
    _pool = pool


def enable_monitor(interval: int = 60) -> None:
    """
    启用连接池监控

    Args:
        interval: 监控间隔（秒）
    """
    global _monitor_enabled, _monitor_interval, _monitor_thread, _monitor_running

    _monitor_enabled = True
    _monitor_interval = interval

    # 初始化告警管理器
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager()

    # 初始化连接泄漏检测器
    global _connection_leak_detector
    if _connection_leak_detector is None:
        _connection_leak_detector = ConnectionLeakDetector()

    if _monitor_thread is None or not _monitor_thread.is_alive():
        _monitor_running = True
        _monitor_thread = threading.Thread(
            target=_monitor_loop, daemon=True, name="ConnectionPoolMonitor"
        )
        _monitor_thread.start()
        logger.info(f"✓ 连接池监控已启动，间隔 {interval} 秒")


def disable_monitor() -> None:
    """禁用连接池监控"""
    global _monitor_enabled, _monitor_running

    _monitor_enabled = False
    _monitor_running = False

    if _monitor_thread and _monitor_thread.is_alive():
        _monitor_thread.join(timeout=5)
        logger.info("✓ 连接池监控已停止")


def _monitor_loop() -> None:
    """监控线程的主循环"""
    global _monitor_running

    while _monitor_running:
        try:
            status = _check_and_report_pool_status()

            # 检测连接泄漏
            if status and _connection_leak_detector:
                _connection_leak_detector.add_status(status)
                alerts = _connection_leak_detector.detect_leak()
                for alert in alerts:
                    _add_alert(alert)

            time.sleep(_monitor_interval)
        except Exception as e:
            logger.error(f"❌ 连接池监控出错: {e}", exc_info=True)


def _check_and_report_pool_status() -> ConnectionPoolStatus | None:
    """检查并报告连接池状态"""
    global _pool

    if _pool is None:
        return None

    try:
        # 获取连接池配置
        from peng.db.connection import get_config

        config = get_config()

        # 通过查询 pg_stat_activity 来获取连接信息
        import psycopg2

        conn = psycopg2.connect(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            database="postgres",  # 连接到系统数据库
            connect_timeout=10,
        )

        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        state,
                        COUNT(*) as count
                    FROM pg_stat_activity
                    WHERE datname = %s
                    GROUP BY state
                    ORDER BY state
                """,
                    (config.database,),
                )

                state_counts = {}
                for row in cursor.fetchall():
                    state_counts[row["state"]] = row["count"]

                total = sum(state_counts.values())
                active = state_counts.get("active", 0)
                idle = state_counts.get("idle", 0)
                idle_in_transaction = state_counts.get("idle in transaction", 0)

                status = ConnectionPoolStatus(
                    total_connections=total,
                    active_connections=active,
                    idle_connections=idle,
                    idle_in_transaction_connections=idle_in_transaction,
                    max_connections=config.pool_size + config.max_overflow,
                )

                logger.info(
                    f"📊 数据库连接状态: 总计 {total} 个连接, "
                    f"active={active}, idle={idle}, "
                    f"idle in transaction={idle_in_transaction}"
                )

                # 检查连接数是否过多
                if _alert_manager:
                    utilization = (
                        total / status.max_connections if status.max_connections > 0 else 0
                    )

                    if utilization > _alert_manager.thresholds["connection_count_critical"]:
                        _add_alert(
                            Alert(
                                level="critical",
                                title="连接数严重过高",
                                message=f"连接数 {total}/{status.max_connections} "
                                f"({utilization * 100:.1f}%) 超过严重阈值",
                                metadata={
                                    "total_connections": total,
                                    "max_connections": status.max_connections,
                                    "utilization": utilization,
                                },
                            )
                        )
                    elif utilization > _alert_manager.thresholds["connection_count_warning"]:
                        _add_alert(
                            Alert(
                                level="warning",
                                title="连接数过高",
                                message=f"连接数 {total}/{status.max_connections} "
                                f"({utilization * 100:.1f}%) 超过警告阈值",
                                metadata={
                                    "total_connections": total,
                                    "max_connections": status.max_connections,
                                    "utilization": utilization,
                                },
                            )
                        )

                # 检查是否有 idle in transaction 的连接
                if idle_in_transaction > 0:
                    logger.warning(
                        f"⚠️  发现 {idle_in_transaction} 个 idle in transaction 连接，"
                        "可能导致连接泄漏"
                    )

                    if _alert_manager:
                        if (
                            idle_in_transaction
                            > _alert_manager.thresholds["idle_in_transaction_critical"]
                        ):
                            _add_alert(
                                Alert(
                                    level="critical",
                                    title="阻塞连接数量严重",
                                    message=f"发现 {idle_in_transaction} 个阻塞连接 "
                                    f"（idle in transaction），可能导致数据库锁定",
                                    metadata={
                                        "idle_in_transaction_connections": idle_in_transaction,
                                    },
                                )
                            )
                        elif (
                            idle_in_transaction
                            > _alert_manager.thresholds["idle_in_transaction_warning"]
                        ):
                            _add_alert(
                                Alert(
                                    level="warning",
                                    title="发现阻塞连接",
                                    message=f"发现 {idle_in_transaction} 个阻塞连接 "
                                    f"（idle in transaction）",
                                    metadata={
                                        "idle_in_transaction_connections": idle_in_transaction,
                                    },
                                )
                            )

                return status

        finally:
            conn.close()

    except Exception as e:
        logger.error(f"❌ 检查连接池状态失败: {e}", exc_info=True)
        return None


def _add_alert(alert: Alert) -> None:
    """
    添加告警

    Args:
        alert: 告警
    """
    global _alert_manager
    if _alert_manager:
        _alert_manager.add_alert(alert)


def get_pool_status() -> ConnectionPoolStatus | None:
    """
    获取连接池状态

    Returns:
        连接池状态
    """
    return _check_and_report_pool_status()


def cleanup_idle_connections(
    idle_threshold: int = 300,  # 5分钟
    force: bool = False,
) -> int:
    """
    清理空闲连接

    Args:
        idle_threshold: 空闲时间阈值（秒）
        force: 是否强制清理所有空闲连接

    Returns:
        清理的连接数量
    """
    global _pool

    if _pool is None:
        logger.warning("⚠️  连接池未初始化，无法清理")
        return 0

    cleaned = 0

    try:
        from peng.db.connection import get_config

        config = get_config()

        import psycopg2
        from psycopg2.extras import RealDictCursor

        conn = psycopg2.connect(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            database=config.database,
            connect_timeout=10,
        )

        try:
            conn.autocommit = True
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                if force:
                    # 清理所有 idle 连接
                    cursor.execute(
                        """
                        SELECT pid, state, query_start
                        FROM pg_stat_activity
                        WHERE datname = %s
                          AND state = 'idle'
                          AND pid <> pg_backend_pid()
                        """,
                        (config.database,),
                    )
                else:
                    # 清理超过阈值的 idle 连接
                    cursor.execute(
                        """
                        SELECT pid, state, query_start
                        FROM pg_stat_activity
                        WHERE datname = %s
                          AND state = 'idle'
                          AND pid <> pg_backend_pid()
                          AND query_start < now() - interval '%s seconds'
                        """,
                        (config.database, idle_threshold),
                    )

                connections = cursor.fetchall()

                if connections:
                    logger.info(f"🧹 发现 {len(connections)} 个空闲连接，正在清理...")

                    for row in connections:
                        pid = row["pid"]
                        try:
                            cursor.execute("SELECT pg_terminate_backend(%s)", (pid,))
                            cleaned += 1
                            logger.debug(f"  ✅ 已终止连接 PID={pid}")
                        except Exception as e:
                            logger.warning(f"  ⚠️  终止连接失败 PID={pid}: {e}")

                    logger.info(f"✅ 已清理 {cleaned} 个空闲连接")
                else:
                    logger.debug("✅ 没有需要清理的空闲连接")

        finally:
            conn.close()

    except Exception as e:
        logger.error(f"❌ 清理空闲连接失败: {e}", exc_info=True)

    return cleaned


def cleanup_idle_in_transaction_connections() -> int:
    """
    清理 idle in transaction 的连接（阻塞连接）

    Returns:
        清理的连接数量
    """
    global _pool

    if _pool is None:
        logger.warning("⚠️  连接池未初始化，无法清理")
        return 0

    cleaned = 0

    try:
        from peng.db.connection import get_config

        config = get_config()

        import psycopg2
        from psycopg2.extras import RealDictCursor

        conn = psycopg2.connect(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            database=config.database,
            connect_timeout=10,
        )

        try:
            conn.autocommit = True
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT pid, usename, application_name, state, query_start
                    FROM pg_stat_activity
                    WHERE datname = %s
                      AND state = 'idle in transaction'
                      AND pid <> pg_backend_pid()
                    ORDER BY query_start
                """,
                    (config.database,),
                )

                connections = cursor.fetchall()

                if connections:
                    logger.warning(
                        f"⚠️  发现 {len(connections)} 个阻塞连接（idle in transaction），正在清理..."
                    )

                    for row in connections:
                        pid = row["pid"]
                        try:
                            cursor.execute("SELECT pg_terminate_backend(%s)", (pid,))
                            cleaned += 1
                            logger.warning(f"  ✅ 已终止阻塞连接 PID={pid}")
                        except Exception as e:
                            logger.warning(f"  ⚠️  终止连接失败 PID={pid}: {e}")

                    logger.info(f"✅ 已清理 {cleaned} 个阻塞连接")
                else:
                    logger.debug("✅ 没有阻塞连接")

        finally:
            conn.close()

    except Exception as e:
        logger.error(f"❌ 清理阻塞连接失败: {e}", exc_info=True)

    return cleaned


@contextmanager
def monitor_context(interval: int = 60):
    """
    连接池监控上下文管理器

    Args:
        interval: 监控间隔（秒）
    """
    enable_monitor(interval)
    try:
        yield
    finally:
        disable_monitor()


def get_monitor_status() -> dict:
    """
    获取监控状态

    Returns:
        监控状态字典
    """
    global _monitor_enabled, _monitor_interval, _monitor_thread, _monitor_running

    return {
        "enabled": _monitor_enabled,
        "running": _monitor_running,
        "interval": _monitor_interval,
        "thread_alive": _monitor_thread.is_alive() if _monitor_thread else False,
    }


def get_alerts(
    level: str | None = None,
    since: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """
    获取告警列表

    Args:
        level: 告警级别过滤
        since: 起始时间（ISO格式）
        limit: 最大数量

    Returns:
        告警列表
    """
    global _alert_manager
    if not _alert_manager:
        return []

    since_datetime = None
    if since:
        try:
            since_datetime = datetime.fromisoformat(since)
        except ValueError:
            pass

    alerts = _alert_manager.get_alerts(level=level, since=since_datetime, limit=limit)
    return [alert.to_dict() for alert in alerts]


def clear_alerts(before: str | None = None) -> int:
    """
    清除告警

    Args:
        before: 清除此时间之前的告警（ISO格式）

    Returns:
        清除的告警数量
    """
    global _alert_manager
    if not _alert_manager:
        return 0

    before_datetime = None
    if before:
        try:
            before_datetime = datetime.fromisoformat(before)
        except ValueError:
            pass

    return _alert_manager.clear_alerts(before=before_datetime)

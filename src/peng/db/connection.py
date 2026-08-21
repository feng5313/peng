"""
PostgreSQL数据库连接管理模块

提供统一的数据库连接接口，支持连接池管理。
"""

from __future__ import annotations

import functools
import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Generator

import asyncpg
import psycopg2

# Windows: psycopg2 的 libpq C 层可能因 GBK 编码导致 UnicodeDecodeError
# 这个补丁捕获 UnicodeDecodeError 并重试（PG 可能需要时间应用 lc_messages=C 配置）
if sys.platform == "win32":
    os.environ.setdefault("PGCLIENTENCODING", "UTF8")
    os.environ.setdefault("LC_MESSAGES", "C")

    _original_psycopg2_connect = psycopg2.connect

    def _patched_connect(*args, **kwargs):
        """Windows 补丁：捕获 UnicodeDecodeError 并重试"""
        import time as _time

        for _attempt in range(3):
            try:
                return _original_psycopg2_connect(*args, **kwargs)
            except UnicodeDecodeError:
                if _attempt < 2:
                    _time.sleep(1.0)  # 等待 PG 重新加载配置
                else:
                    raise

    psycopg2.connect = _patched_connect
from psycopg2 import pool
from psycopg2.extras import RealDictCursor, RealDictRow

# Windows 中文版：强制 psycopg2 使用 UTF-8 编码，防止 GBK 解码崩溃
# 必须在 psycopg2 导入后立即设置
if sys.platform == "win32":
    psycopg2.extensions.register_type(psycopg2.extensions.UNICODE)
    psycopg2.extensions.register_type(psycopg2.extensions.UNICODEARRAY)

from peng.common import jsonp as json

# 导入PostgreSQL连接封装
from peng.db.pg_adapter import PostgreSQLConnection

logger = logging.getLogger(__name__)


def is_connection_valid(conn: psycopg2.extensions.connection) -> bool:
    """
    检查数据库连接是否仍然有效

    Args:
        conn: psycopg2连接对象

    Returns:
        连接是否有效
    """
    if conn is None or conn.closed:
        return False

    try:
        # 执行简单的查询来测试连接
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return True
    except Exception as e:
        # 这是正常的连接池行为，降级为 DEBUG
        logger.debug(f"数据库连接无效: {e}", exc_info=True)
        return False


class DBConfig:
    """数据库配置"""

    def __init__(self, config_path: str | None = None):
        """
        初始化数据库配置

        Args:
            config_path: 配置文件路径，默认为 config/db.json
        """
        if config_path is None:
            # 默认配置路径
            try:
                from peng.config import get_project_root

                config_path = str(get_project_root() / "config" / "db.json")
            except Exception as e:
                logger.warning(f"无法获取项目根目录，使用默认配置路径：{e}", exc_info=True)
                config_path = "config/db.json"

        config_file = Path(config_path)

        if not config_file.exists():
            raise FileNotFoundError(f"数据库配置文件不存在：{config_file}")

        with open(config_file) as f:
            config = json.load(f)
        logger.info(
            f"[DBConfig] 配置文件: {config_file}, postgresql.port={config.get('postgresql', {}).get('port', 'N/A')}"
        )

        # 支持两种配置格式：
        # 1. 扁平格式：直接读取根目录配置
        # 2. 嵌套格式：从 postgresql/mysql 等数据库类型对象中读取
        db_type = config.get("type", "postgresql")
        db_config = config.get(db_type, {})

        # 优先从数据库类型对象中读取配置，如果不存在则从根目录读取
        self.host = db_config.get("host") or config.get("host", "localhost")
        self.port = db_config.get("port") or config.get("port", 5432)
        # Windows: 支持端口覆盖（当系统排除端口范围包含默认端口时）
        # 优先级: 环境变量 > 配置文件 > 默认值
        _port_override = os.environ.get("PENG_PG_PORT_OVERRIDE")
        if not _port_override:
            # 从持久化文件读取（服务重启后环境变量丢失）
            _override_file = (
                Path(sys.executable).resolve().parent.parent.parent / "config" / "db_port_win"
            )
            logger.debug(
                f"[DBConfig] 读取端口覆盖文件: {_override_file}, exists={_override_file.exists()}"
            )
            if _override_file.exists():
                _port_override = _override_file.read_text().strip()
                logger.debug(f"[DBConfig] 端口覆盖值: {_port_override}")
        if _port_override:
            self.port = int(_port_override)
        logger.debug(f"[DBConfig] 最终端口: {self.port}")
        self.user = db_config.get("user") or config.get("user", "peng")
        self.password = db_config.get("password") or config.get("password", "peng_password")
        self.database = db_config.get("database") or config.get("database", "peng_db")
        self.pool_size = db_config.get("pool_size") or config.get("pool_size", 10)
        self.max_overflow = db_config.get("max_overflow") or config.get("max_overflow", 20)
        self.monitor_enabled = db_config.get("monitor_enabled") or config.get(
            "monitor_enabled", False
        )
        self.monitor_interval = db_config.get("monitor_interval") or config.get(
            "monitor_interval", 60
        )
        self.connect_timeout = db_config.get("connect_timeout") or config.get("connect_timeout", 30)
        self.idle_timeout = db_config.get("idle_timeout") or config.get("idle_timeout", 300)
        # 连接超时配置
        self.connect_timeout = config.get("connect_timeout", 30)  # 连接超时（秒）
        self.idle_timeout = config.get("idle_timeout", 300)  # 空闲超时（秒）

    @property
    def dsn(self) -> str:
        """构建 PostgreSQL DSN"""
        return (
            f"host={self.host} port={self.port} dbname={self.database} "
            f"user={self.user} password={self.password} "
            f"connect_timeout={self.connect_timeout} "
            # 添加 keepalive 参数，防止长时间闲置连接被关闭
            f"keepalives_idle=60 keepalives_interval=10 keepalives_count=6"
        )

    @property
    def connect_kwargs(self) -> dict:
        """构建 PostgreSQL 连接关键字参数（避免 DSN 字符串编码问题）"""
        return {
            "host": self.host,
            "port": int(self.port),
            "dbname": self.database,
            "user": self.user,
            "password": self.password,
            "connect_timeout": int(self.connect_timeout),
            "keepalives_idle": 60,
            "keepalives_interval": 10,
            "keepalives_count": 6,
        }


_pool: pool.ThreadedConnectionPool | None = None
_config: DBConfig | None = None
_pool_pid: int | None = None  # 记录创建连接池的进程ID
_pool_lock: threading.Lock = threading.Lock()  # 保护连接池创建的锁


def _cleanup_pool_on_exit():
    """进程退出时自动关闭连接池，防止连接泄漏"""
    global _pool
    if _pool is not None:
        try:
            _pool.closeall()
        except Exception:
            pass
        _pool = None


import atexit

atexit.register(_cleanup_pool_on_exit)


def get_config() -> DBConfig:
    """获取数据库配置"""
    global _config
    # 如果有端口覆盖且配置已缓存，重新加载
    _port_override = os.environ.get("PENG_PG_PORT_OVERRIDE")
    if not _port_override:
        # 从持久化文件读取
        _override_file = (
            Path(sys.executable).resolve().parent.parent.parent / "config" / "db_port_win"
        )
        if _override_file.exists():
            try:
                _port_override = _override_file.read_text().strip()
            except Exception:
                pass
    if _port_override and _config is not None:
        try:
            override_port = int(_port_override)
            if _config.port != override_port:
                _config = DBConfig()  # 重新加载配置
        except (ValueError, TypeError):
            pass
    if _config is None:
        _config = DBConfig()
    return _config


def get_pool() -> pool.ThreadedConnectionPool:
    """
    获取数据库连接池（单例，线程安全）

    Returns:
        连接池对象
    """
    global _pool, _pool_pid

    current_pid = os.getpid()

    # 检测是否在子进程中（fork 后）
    # 如果当前进程 ID 与创建连接池的进程 ID 不同，说明在子进程中
    if _pool is not None and _pool_pid is not None and current_pid != _pool_pid:
        logger.debug(
            f"检测到进程变化 ({_pool_pid} -> {current_pid})，关闭父进程连接池并创建新连接池"
        )
        try:
            _pool.closeall()
        except Exception as e:
            logger.debug(f"关闭父进程连接池时出错（可忽略）: {e}")
        _pool = None
        _pool_pid = None

    # 使用双重检查锁定模式，避免不必要的锁竞争
    if _pool is None:
        with _pool_lock:
            # 再次检查，防止多个线程同时创建连接池
            if _pool is None:
                config = get_config()

                # 创建连接池（使用关键字参数避免 DSN 字符串编码问题）
                # minconn=1：按需创建连接，避免每个进程预占多个PG连接
                # 远程CLI命令会spawn大量短命子进程，minconn过大会快速耗尽PG连接数
                _pool = pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=config.pool_size,
                    **config.connect_kwargs,
                )
                _pool_pid = current_pid

                # 将超时配置附加到池对象，供 PostgreSQLConnection 使用
                _pool._connect_timeout = config.connect_timeout
                _pool._idle_timeout = config.idle_timeout

                logger.info(
                    f"✓ 数据库连接池已创建：{config.host}:{config.port}/{config.database} (PID: {current_pid})"
                )

                # 如果启用了连接池监控
                if config.monitor_enabled:
                    try:
                        from peng.db.connection_monitor import enable_monitor

                        enable_monitor(interval=config.monitor_interval)
                        logger.info(f"✓ 连接池监控已启用，间隔：{config.monitor_interval}秒")
                    except Exception as e:
                        logger.warning(f"连接池监控启用失败：{e}", exc_info=True)

    return _pool


def get_db_connection() -> PostgreSQLConnection:
    """
    获取数据库连接

    Args:
        无参数

    Returns:
        PostgreSQL连接封装对象

    Example:
        # 方式1：直接使用（需要手动关闭）
        conn = get_db_connection()
        try:
            cursor = conn.execute("SELECT * FROM messages")
            for row in cursor.fetchall():
                print(row)
        finally:
            conn.close()

        # 方式2：使用上下文管理器（推荐）
        with get_db_connection() as conn:
            cursor = conn.execute("SELECT * FROM messages")
            for row in cursor.fetchall():
                print(row)
    """
    pool = get_pool()
    raw_conn = pool.getconn()
    return PostgreSQLConnection(raw_conn, pool=pool)


# ==================== 临时占位符：get_connection 将在后面定义 ====================
# 这样可以确保在 get_connection 使用之前，retry_on_connection_error 已经定义
_get_connection_core = None
get_connection = None


@contextmanager
def get_cursor(
    conn: PostgreSQLConnection | None = None,
    cursor_factory: Any = RealDictCursor,
) -> Generator[psycopg2.extensions.cursor, None, None]:
    """
    获取数据库游标（上下文管理器）

    Args:
        conn: 数据库连接，如果为None则自动获取
        cursor_factory: 游标工厂，默认为RealDictCursor

    Yields:
        数据库游标
    """
    need_close = conn is None

    if need_close:
        pool = get_pool()
        raw_conn = pool.getconn()

        # 检查连接是否有效，如果无效则关闭并重新获取
        if not is_connection_valid(raw_conn):
            # 这是正常的连接池行为，降级为 DEBUG
            logger.debug("从连接池获取的连接已失效，关闭并重新获取")
            try:
                raw_conn.close()  # 直接关闭失效连接，避免putconn时rollback失败
            except Exception as e:
                logger.warning(f"关闭失效连接时出错: {e}", exc_info=True)
            raw_conn = pool.getconn()  # 重新获取

        conn = PostgreSQLConnection(raw_conn, pool=pool)

    cursor = conn.cursor(cursor_factory=cursor_factory)

    try:
        yield cursor
    finally:
        # 关闭游标
        try:
            cursor.close()
        except Exception as e:
            logger.debug(f"关闭游标时出错（可忽略）: {e}")

        # 如果是自动获取的连接，放回池中
        # 注意：通过PostgreSQLConnection.close()来放回，这样可以正确处理连接状态
        if need_close and conn and not conn.closed:
            try:
                conn.close()  # 这会正确清理cursor并放回连接到池
            except Exception as e:
                logger.warning(f"放回连接到池中时出错: {e}", exc_info=True)


def close_pool() -> None:
    """关闭数据库连接池"""
    global _pool, _pool_pid

    if _pool:
        try:
            _pool.closeall()
            logger.info("✓ 数据库连接池已关闭")
        except Exception as e:
            logger.error(f"关闭连接池时出错: {e}", exc_info=True)
        finally:
            _pool = None
            _pool_pid = None


def get_async_dsn() -> str:
    """
    获取asyncpg的DSN格式

    Returns:
        asyncpg DSN字符串
    """
    config = get_config()
    return (
        f"postgresql://{config.user}:{config.password}@"
        f"{config.host}:{config.port}/{config.database}"
    )


async def get_async_connection() -> asyncpg.Connection:
    """
    获取异步数据库连接

    Returns:
        asyncpg连接对象
    """
    dsn = get_async_dsn()
    conn = await asyncpg.connect(dsn)
    return conn


def row_to_dict(row: RealDictRow) -> dict[str, Any]:
    """
    将数据库行转换为字典

    Args:
        row: 数据库行

    Returns:
        字典
    """
    return dict(row)


def retry_on_connection_error(
    max_retries: int = 3,
    retry_delay: float = 0.5,
    backoff_factor: float = 2.0,
) -> Callable:
    """
    数据库连接重试装饰器

    当遇到连接异常时自动重试，最多重试 max_retries 次。

    Args:
        max_retries: 最大重试次数
        retry_delay: 初始重试延迟（秒）
        backoff_factor: 每次重试延迟的倍增因子

    Returns:
        装饰器函数

    Example:
        @retry_on_connection_error(max_retries=3)
        def query_data():
            with get_connection() as conn:
                return conn.execute("SELECT * FROM table").fetchall()
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            current_delay = retry_delay

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except psycopg2.OperationalError as e:
                    error_msg = str(e)
                    # 只重试连接关闭相关的错误
                    is_connection_error = (
                        "closed the connection" in error_msg
                        or "terminating connection" in error_msg
                        or "server closed the connection" in error_msg
                    )

                    if not is_connection_error or attempt == max_retries:
                        # 不是连接错误，或者已达到最大重试次数，直接抛出
                        raise

                    logger.warning(
                        f"数据库连接异常（尝试 {attempt + 1}/{max_retries + 1}）: {e}，"
                        f"{current_delay:.2f}秒后重试..."
                    )

                    # 等待后重试
                    time.sleep(current_delay)
                    current_delay *= backoff_factor
                except Exception as e:
                    # 其他异常不重试，直接抛出
                    raise

        return wrapper

    return decorator


@contextmanager
def _get_connection_core() -> Generator[PostgreSQLConnection, None, None]:
    """
    获取数据库连接的核心实现（不带重试）

    Yields:
        PostgreSQL连接封装对象
    """
    pool = get_pool()
    raw_conn = pool.getconn()

    # 检查连接是否有效，如果无效则关闭并重新获取
    if not is_connection_valid(raw_conn):
        # 这是正常的连接池行为，降级为 DEBUG
        logger.debug("从连接池获取的连接已失效，关闭并重新获取")
        try:
            raw_conn.close()  # 直接关闭失效连接，避免putconn时rollback失败
        except Exception as e:
            logger.warning(f"关闭失效连接时出错: {e}", exc_info=True)
        raw_conn = pool.getconn()  # 重新获取

    # 包装为PostgreSQLConnection
    conn = PostgreSQLConnection(raw_conn, pool=pool)

    try:
        yield conn
    except Exception:
        # 异常时，PostgreSQLConnection.__exit__会自动rollback
        # 我们需要调用__exit__来触发rollback
        exc_type, exc_val, exc_tb = sys.exc_info()
        conn.__exit__(exc_type, exc_val, exc_tb)
        raise
    else:
        # 正常退出，PostgreSQLConnection.__exit__会自动commit
        conn.__exit__(None, None, None)


# 创建带重试的版本
get_connection = retry_on_connection_error(max_retries=3)(_get_connection_core)


def rows_to_list(rows: list[RealDictRow]) -> list[dict[str, Any]]:
    """
    将数据库行列表转换为字典列表

    Args:
        rows: 数据库行列表

    Returns:
        字典列表
    """
    return [dict(row) for row in rows]


# ==================== 数据库维护方法 ====================


def clear_idle_transaction_connections() -> list[int]:
    """
    清理所有 idle in transaction 的连接

    Returns:
        被清理的连接ID列表
    """
    config = get_config()

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
            # 查询所有 idle in transaction 的连接
            cursor.execute(
                """
                SELECT pid FROM pg_stat_activity
                WHERE datname = %s
                  AND state = 'idle in transaction'
                  AND pid <> pg_backend_pid()
                """,
                (config.database,),
            )

            blocking_connections = cursor.fetchall()

            if not blocking_connections:
                return []

            terminated_pids = []
            for row in blocking_connections:
                pid = row["pid"]
                try:
                    cursor.execute("SELECT pg_terminate_backend(%s)", (pid,))
                    terminated_pids.append(pid)
                except Exception as e:
                    print(f"  ⚠️  终止连接 {pid} 失败: {e}")

            return terminated_pids
    finally:
        conn.close()


def terminate_all_other_connections(exclude_pids: list[int] | None = None) -> list[int]:
    """
    终止数据库上除当前后端（和指定PID列表）之外的所有连接。
    用于 clear-all-data 等需要独占数据库访问的场景。

    Args:
        exclude_pids: 额外需要排除的PID列表（不会被终止）

    Returns:
        被终止的连接ID列表
    """
    config = get_config()

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
            # 查询当前后端PID
            cursor.execute("SELECT pg_backend_pid() AS pid")
            my_pid = cursor.fetchone()["pid"]

            # 构建排除列表
            excluded = {my_pid}
            if exclude_pids:
                excluded.update(exclude_pids)

            # 查询所有其他连接（排除后台工作进程和自身）
            cursor.execute(
                """
                SELECT pid, state, application_name, query
                FROM pg_stat_activity
                WHERE datname = %s
                  AND pid <> ALL(%s)
                  AND backend_type = 'client backend'
                """,
                (config.database, list(excluded)),
            )

            connections = cursor.fetchall()

            if not connections:
                return []

            terminated_pids = []
            for row in connections:
                pid = row["pid"]
                try:
                    cursor.execute("SELECT pg_terminate_backend(%s)", (pid,))
                    terminated_pids.append(pid)
                    logger.info(
                        f"已终止数据库连接 pid={pid}, state={row['state']}, "
                        f"app={row['application_name']}, query={row['query'][:80] if row['query'] else ''}"
                    )
                except Exception as e:
                    logger.warning(f"终止连接 pid={pid} 失败: {e}")

            return terminated_pids
    finally:
        conn.close()


def truncate_all_tables() -> int:
    """
    清空所有业务表（DROP 所有表并重建）

    Returns:
        删除的记录总数
    """
    import psycopg2
    from peng.db.init_postgres_db import (
        create_messages_table,
        create_tasks_table,
        create_task_steps_table,
        create_task_dependencies_table,
        create_task_controls_table,
        create_message_controls_table,
        create_audit_logs_table,
        create_schema_version_tables,
        fix_tasks_table_schema,
    )

    total_deleted = 0

    config = get_config()

    # 创建原始连接（不使用连接池，避免清理过程中的问题）
    raw_conn = psycopg2.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=config.database,
    )

    try:
        from psycopg2.extras import RealDictCursor

        cursor = raw_conn.cursor(cursor_factory=RealDictCursor)

        # 获取所有表名（排除系统表）
        cursor.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """
        )
        tables = [row["table_name"] for row in cursor.fetchall()]

        # 统计删除的记录数
        if tables:
            for table in tables:
                cursor.execute(f"SELECT COUNT(*) as count FROM {table}")
                row = cursor.fetchone()
                count_before = row["count"] if row else 0
                total_deleted += count_before

            # 删除所有表（使用 CASCADE 自动处理外键依赖）
            for table in tables:
                cursor.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
                logger.debug(f"已删除表: {table}")

        raw_conn.commit()
        logger.info(f"✓ 已删除 {len(tables)} 个表，{total_deleted} 条记录")

        # 重建所有表
        logger.info("开始重建数据库表...")
        create_messages_table(raw_conn)
        create_tasks_table(raw_conn)
        create_task_steps_table(raw_conn)
        create_task_dependencies_table(raw_conn)
        create_task_controls_table(raw_conn)
        create_message_controls_table(raw_conn)
        create_audit_logs_table(raw_conn)
        create_schema_version_tables(raw_conn)
        fix_tasks_table_schema(raw_conn)
        logger.info("✓ 所有表重建完成")

    except Exception as e:
        raw_conn.rollback()
        logger.error(f"❌ 清空并重建表失败: {e}", exc_info=True)
        raise
    finally:
        raw_conn.close()

    return total_deleted

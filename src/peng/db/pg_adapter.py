"""
PostgreSQL连接封装模块

提供更方便的PostgreSQL接口。
"""

import logging
import time
from typing import Any, Generator

import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

# 哨兵值：用于区分 cursor_factory 参数"未传参"和"显式传 None"
# 当调用者显式传入 cursor_factory=None 时，表示需要 psycopg2 的默认元组游标
_CURSOR_DEFAULT = object()


class PostgreSQLConnection:
    """
    PostgreSQL连接封装，提供更方便的接口
    """

    def __init__(self, conn: psycopg2.extensions.connection, pool=None):
        self._conn = conn
        self._pool = pool  # 连接池（用于上下文管理器）
        self._cursors = []  # 跟踪所有打开的cursor，用于在commit/rollback前关闭
        self._last_used = time.time()  # 记录最后使用时间，用于空闲检测
        self._last_health_check = 0.0  # 上次健康检查时间戳，避免频繁 SELECT 1
        # 连接超时配置（从连接池配置继承，如果有）
        if pool and hasattr(pool, "_connect_timeout"):
            self._connect_timeout = pool._connect_timeout
        else:
            self._connect_timeout = 30  # 默认 30 秒
        if pool and hasattr(pool, "_idle_timeout"):
            self._idle_timeout = pool._idle_timeout
        else:
            self._idle_timeout = 300  # 默认 5 分钟

    @property
    def closed(self) -> bool:
        """返回连接是否已关闭"""
        return self._conn.closed

    def cursor(self, cursor_factory=_CURSOR_DEFAULT) -> "PostgreSQLCursor":
        """创建游标

        Args:
            cursor_factory: 游标工厂类。
                - 不传参（默认）：使用 RealDictCursor（返回字典）
                - 显式传 None：使用 psycopg2 默认游标（返回元组）
                - 传其他类：使用指定的游标工厂
        """
        if cursor_factory is _CURSOR_DEFAULT:
            cursor_factory = RealDictCursor
        # cursor_factory=None 时，传 None 给 psycopg2（使用默认元组游标）

        raw_cursor = self._conn.cursor(cursor_factory=cursor_factory)
        pg_cursor = PostgreSQLCursor(raw_cursor, self)
        self._cursors.append(pg_cursor)
        self._last_used = time.time()  # 更新最后使用时间
        return pg_cursor

    # 健康检查最小间隔（秒）：避免频繁 SELECT 1 消耗数据库资源
    _HEALTH_CHECK_MIN_INTERVAL = 5.0

    def _check_and_reconnect(self) -> bool:
        """
        检查连接是否需要重连（超时、失效等）

        优化：使用 _HEALTH_CHECK_MIN_INTERVAL 节流健康检查频率，
        避免每次 execute() 都执行 SELECT 1。

        Returns:
            连接是否可用（True=可用，False=需要重新获取连接）
        """
        # 检查连接是否已关闭
        if self._conn.closed:
            logger.debug("连接已关闭，需要重连")
            return False

        # 检查是否空闲超时
        idle_time = time.time() - self._last_used
        if idle_time > self._idle_timeout:
            logger.debug(f"连接已空闲 {idle_time:.1f}秒，超过阈值 {self._idle_timeout}秒，需要回收")
            return False

        # 节流：距离上次健康检查不足 _HEALTH_CHECK_MIN_INTERVAL 秒时跳过 SELECT 1
        now = time.time()
        if now - self._last_health_check < self._HEALTH_CHECK_MIN_INTERVAL:
            return True

        # 检查连接是否仍然有效
        try:
            with self._conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            self._last_health_check = now
            return True
        except psycopg2.Error as e:
            logger.debug(f"连接失效检查失败：{e}，需要重连")
            return False

    def _reconnect(self) -> None:
        """
        重新连接（从连接池获取新连接）

        Raises:
            ValueError: 如果没有连接池，无法重连
        """
        if not self._pool:
            raise ValueError("没有连接池，无法重连")

        # 关闭旧连接（从池中移除）
        try:
            self.close()
        except Exception:
            pass

        # 从池中获取新连接
        raw_conn = self._pool.getconn()
        self._conn = raw_conn
        self._cursors = []
        self._last_used = time.time()
        self._last_health_check = time.time()  # 重置健康检查时间戳
        logger.debug("已从连接池获取新连接")

    def execute(
        self, sql: str, params: tuple | None = None, max_retries: int = 3
    ) -> "PostgreSQLCursor":
        """
        执行 SQL（便捷方法，支持自动重试）

        Args:
            sql: SQL 语句
            params: SQL 参数
            max_retries: 最大重试次数（默认 3 次）

        Returns:
            游标对象

        Raises:
            psycopg2.Error: SQL 执行错误
        """
        current_delay = 0.5

        for attempt in range(max_retries):
            try:
                # 执行前检查连接状态，必要时重连
                if not self._check_and_reconnect():
                    self._reconnect()

                cursor = self.cursor()
                if params:
                    cursor.execute(sql, params)
                else:
                    cursor.execute(sql)
                self._last_used = time.time()  # 更新最后使用时间
                return cursor
            except psycopg2.OperationalError as e:
                error_msg = str(e)

                # 只重试连接错误
                is_connection_error = (
                    "closed the connection" in error_msg
                    or "terminating connection" in error_msg
                    or "server closed the connection" in error_msg
                )

                if not is_connection_error:
                    # 不是连接错误，直接抛出
                    logger.error(f"SQL 执行失败：{e}\nSQL: {sql}", exc_info=True)
                    raise

                # 已达到最大重试次数，抛出异常
                if attempt == max_retries - 1:
                    logger.error(
                        f"数据库连接已关闭，已重试{max_retries}次仍然失败：{e}\nSQL: {sql}",
                        exc_info=True,
                    )
                    raise

                # 连接已关闭，需要重试
                logger.warning(
                    f"数据库连接已关闭（尝试 {attempt + 1}/{max_retries}），"
                    f"{current_delay:.2f}秒后重试...\nSQL: {sql}"
                )

                # 等待后重试
                time.sleep(current_delay)
                current_delay *= 2.0

                # 重新连接
                self._reconnect()
                # 重试循环继续
            except psycopg2.Error as e:
                logger.error(f"SQL执行失败: {e}\nSQL: {sql}", exc_info=True)
                raise

    def commit(self, max_retries: int = 3) -> None:
        """
        提交事务（支持自动重试）

        Args:
            max_retries: 最大重试次数（默认 3 次）

        Raises:
            psycopg2.Error: SQL 执行错误
        """
        current_delay = 0.5
        last_exception = None

        for attempt in range(max_retries):
            try:
                # 执行前检查连接状态，必要时重连
                if not self._check_and_reconnect():
                    self._reconnect()

                # 在 commit 前关闭所有未关闭的 cursor，避免 PGRES_TUPLES_OK 错误
                for cursor in self._cursors[:]:  # 使用副本，避免在遍历时修改列表
                    if not cursor.closed:
                        try:
                            cursor.close()
                        except psycopg2.InterfaceError:
                            # psycopg2 连接已关闭，忽略
                            pass
                        except psycopg2.Error as e:
                            # 记录错误，但继续关闭其他 cursor
                            logger.debug(f"关闭 cursor 时出错（可忽略）: {e}")
                        except Exception as e:
                            logger.debug(f"关闭 cursor 时出错（可忽略）: {e}")

                self._cursors.clear()

                if not self._conn.closed:
                    # 只在有活动事务时才 commit，避免 PGRES_TUPLES_OK 错误
                    try:
                        status = self._conn.get_transaction_status()
                        if status == psycopg2.extensions.TRANSACTION_STATUS_INTRANS:
                            try:
                                self._conn.commit()
                            except psycopg2.DatabaseError as e:
                                # 捕获 PGRES_TUPLES_OK 等错误，避免崩溃
                                error_msg = str(e)
                                if "PGRES_TUPLES_OK" in error_msg:
                                    logger.warning(f"commit 失败（连接状态异常）: {e}，忽略此错误")
                                else:
                                    # 其他错误仍然抛出
                                    raise
                        elif status == psycopg2.extensions.TRANSACTION_STATUS_IDLE:
                            logger.debug("没有活动事务，无需 commit")
                        else:
                            # 记录实际状态值
                            status_names = {
                                psycopg2.extensions.TRANSACTION_STATUS_IDLE: "IDLE",
                                psycopg2.extensions.TRANSACTION_STATUS_INTRANS: "INTRANS",
                                psycopg2.extensions.TRANSACTION_STATUS_INERROR: "INERROR",
                                psycopg2.extensions.TRANSACTION_STATUS_UNKNOWN: "UNKNOWN",
                            }
                            status_name = status_names.get(status, f"UNKNOWN({status})")
                            logger.info(f"事务状态异常：{status_name}，尝试 commit")
                            try:
                                self._conn.commit()
                            except psycopg2.DatabaseError as e:
                                error_msg = str(e)
                                if "PGRES_TUPLES_OK" in error_msg:
                                    logger.warning(f"commit 失败（连接状态异常）: {e}，忽略此错误")
                                else:
                                    raise
                    except psycopg2.InterfaceError as e:
                        # 连接已关闭，跳过到重试逻辑
                        raise
                    except psycopg2.Error as e:
                        # 其他 psycopg2 错误，记录并抛出
                        logger.error(f"数据库提交失败：{e}", exc_info=True)
                        raise
                else:
                    # 连接已关闭，抛出异常让重试逻辑处理
                    raise psycopg2.InterfaceError("Connection is closed")

                # 成功执行，退出循环
                self._last_used = time.time()  # 更新最后使用时间
                return

            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                last_exception = e
                error_msg = str(e)

                # 只重试连接错误
                is_connection_error = (
                    "closed the connection" in error_msg
                    or "terminating connection" in error_msg
                    or "server closed the connection" in error_msg
                    or "Connection is closed" in error_msg
                )

                if not is_connection_error:
                    # 不是连接错误，直接抛出
                    logger.error(f"数据库提交失败：{e}", exc_info=True)
                    raise

                # 已达到最大重试次数，抛出异常
                if attempt == max_retries - 1:
                    logger.error(
                        f"数据库连接已关闭，已重试{max_retries}次仍然失败：{e}",
                        exc_info=True,
                    )
                    raise

                # 连接已关闭，需要重试
                logger.warning(
                    f"数据库连接已关闭（尝试 {attempt + 1}/{max_retries}），"
                    f"{current_delay:.2f}秒后重试..."
                )

                # 等待后重试
                time.sleep(current_delay)
                current_delay *= 2.0

                # 重新连接
                self._reconnect()

            except psycopg2.Error as e:
                logger.error(f"数据库提交失败：{e}", exc_info=True)
                raise

        # 如果所有重试都失败，抛出最后一个异常
        if last_exception:
            raise last_exception

    def rollback(self, max_retries: int = 3) -> None:
        """
        回滚事务（支持自动重试）

        Args:
            max_retries: 最大重试次数（默认 3 次）

        Raises:
            psycopg2.Error: SQL 执行错误
        """
        current_delay = 0.5
        last_exception = None

        for attempt in range(max_retries):
            try:
                # 执行前检查连接状态，必要时重连
                if not self._check_and_reconnect():
                    self._reconnect()

                for cursor in self._cursors[:]:
                    if not cursor.closed:
                        try:
                            cursor.close()
                        except Exception:
                            pass

                self._cursors.clear()

                if not self._conn.closed:
                    # 只在有活动事务时才 rollback
                    try:
                        status = self._conn.get_transaction_status()
                        if status == psycopg2.extensions.TRANSACTION_STATUS_INTRANS:
                            try:
                                self._conn.rollback()
                                logger.debug("事务已回滚")
                            except psycopg2.DatabaseError as e:
                                # 捕获 PGRES_TUPLES_OK 等错误，避免崩溃
                                error_msg = str(e)
                                if "PGRES_TUPLES_OK" in error_msg:
                                    logger.warning(
                                        f"rollback 失败（连接状态异常）: {e}，忽略此错误"
                                    )
                                else:
                                    # 其他错误仍然抛出
                                    raise
                        elif status == psycopg2.extensions.TRANSACTION_STATUS_IDLE:
                            logger.debug("没有活动事务，无需 rollback")
                        else:
                            # 记录实际状态值
                            status_names = {
                                psycopg2.extensions.TRANSACTION_STATUS_IDLE: "IDLE",
                                psycopg2.extensions.TRANSACTION_STATUS_INTRANS: "INTRANS",
                                psycopg2.extensions.TRANSACTION_STATUS_INERROR: "INERROR",
                                psycopg2.extensions.TRANSACTION_STATUS_UNKNOWN: "UNKNOWN",
                            }
                            status_name = status_names.get(status, f"UNKNOWN({status})")
                            logger.info(f"事务状态异常：{status_name}，尝试 rollback")
                            try:
                                self._conn.rollback()
                            except psycopg2.DatabaseError as e:
                                error_msg = str(e)
                                if "PGRES_TUPLES_OK" in error_msg:
                                    logger.warning(
                                        f"rollback 失败（连接状态异常）: {e}，忽略此错误"
                                    )
                                else:
                                    raise
                    except psycopg2.InterfaceError as e:
                        # 连接已关闭，跳过到重试逻辑
                        raise
                    except psycopg2.Error as e:
                        # 其他 psycopg2 错误，记录并抛出
                        logger.error(f"数据库回滚失败：{e}", exc_info=True)
                        raise
                else:
                    # 连接已关闭，抛出异常让重试逻辑处理
                    raise psycopg2.InterfaceError("Connection is closed")

                # 成功执行，退出循环
                self._last_used = time.time()  # 更新最后使用时间
                return

            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                last_exception = e
                error_msg = str(e)

                # 只重试连接错误
                is_connection_error = (
                    "closed the connection" in error_msg
                    or "terminating connection" in error_msg
                    or "server closed the connection" in error_msg
                    or "Connection is closed" in error_msg
                )

                if not is_connection_error:
                    # 不是连接错误，直接抛出
                    logger.error(f"数据库回滚失败：{e}", exc_info=True)
                    raise

                # 已达到最大重试次数，抛出异常
                if attempt == max_retries - 1:
                    logger.error(
                        f"数据库连接已关闭，已重试{max_retries}次仍然失败：{e}",
                        exc_info=True,
                    )
                    raise

                # 连接已关闭，需要重试
                logger.warning(
                    f"数据库连接已关闭（尝试 {attempt + 1}/{max_retries}），"
                    f"{current_delay:.2f}秒后重试..."
                )

                # 等待后重试
                time.sleep(current_delay)
                current_delay *= 2.0

                # 重新连接
                self._reconnect()

            except psycopg2.Error as e:
                logger.error(f"数据库回滚失败：{e}", exc_info=True)
                raise

        # 如果所有重试都失败，抛出最后一个异常
        if last_exception:
            raise last_exception

    def close(self) -> None:
        """关闭连接"""
        for cursor in self._cursors:
            if not cursor.closed:
                try:
                    cursor.close()
                except Exception:
                    pass

        self._cursors.clear()

        if self._pool:
            # 如果是从连接池获取的，放回池中
            # 关键：即使连接已关闭/失效，也必须调用 putconn 来从池的 _used 字典中移除
            # 否则连接池会认为该连接仍在使用中，导致连接池耗尽
            if self._conn.closed:
                # 连接已关闭，使用 putconn(close=True) 来正确归还连接池资源
                logger.debug("连接已关闭，从连接池中移除")
                try:
                    self._pool.putconn(self._conn, close=True)
                except Exception as e:
                    logger.debug(f"从连接池移除已关闭连接时出错（可忽略）: {e}")
                return

            try:
                # 检查连接是否仍然有效
                with self._conn.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    result = cursor.fetchone()
                    if result and result[0] == 1:
                        # 连接有效，放回池中
                        self._pool.putconn(self._conn)
                    else:
                        # 结果异常，关闭连接
                        logger.debug("连接返回异常结果，关闭连接")
                        self._conn.close()
                        self._pool.putconn(self._conn, close=True)
            except psycopg2.InterfaceError:
                # 连接已断开，从池中移除
                logger.debug("连接已断开，从连接池中移除")
                try:
                    self._pool.putconn(self._conn, close=True)
                except Exception as e:
                    logger.debug(f"从连接池移除断开连接时出错（可忽略）: {e}")
            except psycopg2.OperationalError as e:
                # 连接操作错误（如服务器关闭连接），从池中移除
                logger.debug(f"连接已失效 ({e})，从连接池中移除")
                try:
                    self._pool.putconn(self._conn, close=True)
                except Exception as e2:
                    logger.debug(f"从连接池移除失效连接时出错（可忽略）: {e2}")
            except psycopg2.ProgrammingError:
                # 编程错误（如"no results to fetch"），关闭连接
                logger.debug("连接编程错误，从连接池中移除")
                try:
                    self._conn.close()
                    self._pool.putconn(self._conn, close=True)
                except Exception as e:
                    logger.debug(f"从连接池移除编程错误连接时出错（可忽略）: {e}")
            except Exception as e:
                # 其他错误，尝试关闭连接并从池中移除
                logger.warning(f"放回连接到池中时出错: {e}，从连接池中移除", exc_info=True)
                try:
                    self._conn.close()
                    self._pool.putconn(self._conn, close=True)
                except Exception as e2:
                    logger.debug(f"从连接池移除连接时出错（可忽略）: {e2}")
        else:
            # 否则直接关闭
            try:
                self._conn.close()
            except Exception:
                pass

    def __enter__(self) -> "PostgreSQLConnection":
        """上下文管理器支持"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """上下文管理器支持"""
        # 无异常时自动提交，有异常时自动回滚
        if exc_type is None:
            # 正常退出，提交事务
            if not self._conn.closed:
                try:
                    self.commit()
                except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                    # 连接已断开，服务器端已自动回滚，忽略错误
                    error_msg = str(e)
                    if "closed the connection" in error_msg or "closed connection" in error_msg:
                        logger.warning(f"连接已断开，无法提交事务（服务器端已自动回滚）: {e}")
                    else:
                        # 其他错误仍然抛出
                        raise
        else:
            # 异常退出，回滚事务
            if not self._conn.closed:
                try:
                    self.rollback()
                except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                    # 连接已断开，服务器端已自动回滚，忽略错误
                    error_msg = str(e)
                    if "closed the connection" in error_msg or "closed connection" in error_msg:
                        logger.warning(f"连接已断开，无法回滚事务（服务器端已自动回滚）: {e}")
                    else:
                        # 其他错误仍然抛出
                        raise
        # 关闭连接
        self.close()


class PostgreSQLCursor:
    """
    PostgreSQL游标封装
    """

    def __init__(self, cursor: psycopg2.extensions.cursor, conn: PostgreSQLConnection):
        self._cursor = cursor
        self._conn = conn
        self.closed = False

    @property
    def rowcount(self) -> int:
        """返回影响的行数"""
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        """返回最后插入的行的ID"""
        try:
            # PostgreSQL 使用 lastval() 获取最后插入的ID
            lastval_cursor = self._conn._conn.cursor()
            lastval_cursor.execute("SELECT lastval()")
            result = lastval_cursor.fetchone()
            lastval_cursor.close()
            if result:
                return result[0]
        except Exception:
            # 如果没有插入或出错，返回None
            pass
        return None

    def execute(
        self, sql: str, params: tuple | None = None, max_retries: int = 3
    ) -> "PostgreSQLCursor":
        """执行SQL（支持自动重试）"""
        import time

        current_delay = 0.5

        for attempt in range(max_retries):
            try:
                if params:
                    self._cursor.execute(sql, params)
                else:
                    self._cursor.execute(sql)
                return self
            except psycopg2.OperationalError as e:
                error_msg = str(e)

                # 只重试连接关闭相关的错误
                is_connection_error = (
                    "closed the connection" in error_msg
                    or "terminating connection" in error_msg
                    or "server closed the connection" in error_msg
                )

                if not is_connection_error:
                    # 不是连接错误，直接抛出
                    logger.error(f"SQL执行失败: {e}\nSQL: {sql}", exc_info=True)
                    raise

                # 已达到最大重试次数，抛出异常
                if attempt == max_retries - 1:
                    logger.error(
                        f"数据库连接已关闭，已重试{max_retries}次仍然失败: {e}\nSQL: {sql}",
                        exc_info=True,
                    )
                    raise

                # 连接已关闭，需要重试
                logger.warning(
                    f"数据库连接已关闭（尝试 {attempt + 1}/{max_retries}），"
                    f"{current_delay:.2f}秒后重试...\nSQL: {sql}"
                )

                # 等待后重试
                time.sleep(current_delay)
                current_delay *= 2.0

                # 关闭当前失效的连接
                try:
                    self._conn.close()
                except Exception as e2:
                    logger.debug(f"关闭失效连接时出错: {e2}")

                # 如果有连接池，重新获取连接并创建新的cursor
                if self._conn._pool:
                    try:
                        # 从连接池获取新连接
                        raw_conn = self._conn._pool.getconn()
                        self._conn._conn = raw_conn
                        # 创建新的cursor（使用 RealDictCursor 作为 cursor_factory）
                        raw_cursor = raw_conn.cursor(cursor_factory=RealDictCursor)
                        # 包装成 PostgreSQLCursor
                        self._cursor = PostgreSQLCursor(raw_cursor, self._conn)
                        # 继续重试循环
                        continue
                    except Exception as e3:
                        logger.error(f"重新获取连接失败: {e3}")
                        raise
                else:
                    # 没有连接池，无法重试
                    raise

            except psycopg2.Error as e:
                logger.error(f"SQL执行失败: {e}\nSQL: {sql}", exc_info=True)
                raise

    def fetchone(self) -> dict | None:
        """获取一行"""
        if self.closed:
            return None
        try:
            return self._cursor.fetchone()
        except psycopg2.ProgrammingError:
            return None

    def fetchall(self) -> list[dict]:
        """获取所有行"""
        if self.closed:
            return []
        try:
            return self._cursor.fetchall()
        except psycopg2.ProgrammingError:
            return []

    def fetchmany(self, size: int = 1) -> list[dict]:
        """获取多行"""
        if self.closed:
            return []
        return self._cursor.fetchmany(size)

    def close(self) -> None:
        """关闭游标"""
        if not self.closed:
            try:
                self._cursor.close()
            except Exception:
                pass
            finally:
                self.closed = True

    def __enter__(self) -> "PostgreSQLCursor":
        """上下文管理器入口"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """上下文管理器出口"""
        self.close()
        return False  # 不抑制异常

    def __iter__(self):
        """迭代支持"""
        return iter(self._cursor)

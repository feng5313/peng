"""
消息管理模块 - 数据库操作

实现消息记录的持久化存储，包括：
- 基础 CRUD 操作
- 状态转换管理
- 回复链管理
- 并发控制（锁和心跳）
- 验收标准管理

从 bin/msg 迁移并扩展，新增字段：
- reply_to: 回复的消息ID
- chain_head_id: 回复链链首消息ID
- chain_context: 回复链完整上下文（JSON）
- caller: 调用来源（chat/fly/msg/inbox_monitor）
- caller_session_id: 调用者会话ID
- is_chain_head: 是否是回复链链首
- is_cancelled: 是否被取消
- timeout: 消息处理超时时间
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.errors

from peng.common import MetaSingleton
from peng.common import jsonp as json
from peng.common.paths import get_working_directory
from peng.config import get_agent_name, get_master_name
from peng.db.connection import get_connection, get_cursor, row_to_dict, rows_to_list

logger = logging.getLogger(__name__)

# Agent 名称
AGENT_NAME = get_agent_name()  # 从配置中获取
MASTER_NAME = get_master_name()

# 日志限流：防止高频重复错误
_last_log_time: dict[str, float] = {}
_log_throttle_interval = 1.0  # 相同错误最少间隔秒数


def _log_with_throttle(level: str, message: str, key: str) -> None:
    """
    限流日志：避免相同错误高频重复输出

    Args:
        level: 日志级别 (debug, info, warning, error)
        message: 日志消息
        key: 用于限流的唯一键（通常是错误类型 + 关键参数）
    """
    now = time.time()
    last_time = _last_log_time.get(key, 0)

    if now - last_time >= _log_throttle_interval:
        _last_log_time[key] = now
        log_func = getattr(logger, level, logger.warning)
        log_func(message)
    else:
        # 静默跳过，不输出日志
        pass


def retry_on_db_error(max_retries: int = 3, delay: float = 0.1):
    """
    数据库操作重试装饰器

    针对常见的数据库I/O错误和锁冲突进行重试。

    Args:
        max_retries: 最大重试次数
        delay: 重试间隔（秒）
    """

    def decorator(func):
        def wrapper(*args, **kwargs):
            import traceback

            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except psycopg2.OperationalError as e:
                    error_msg = str(e).lower()
                    # 对特定的数据库错误重试
                    retryable_errors = [
                        "disk i/o error",
                        "database is locked",
                        "server closed the connection",
                        "connection does not exist",
                        "terminating connection",
                    ]
                    if any(err in error_msg for err in retryable_errors):
                        if attempt < max_retries - 1:
                            logger.warning(
                                f"[DB Retry] {func.__name__} 失败 (尝试 {attempt + 1}/{max_retries}): {e}"
                            )
                            time.sleep(delay * (attempt + 1))  # 指数退避
                            continue
                    # 其他错误或重试次数用尽，直接抛出
                    logger.error(f"[DB Error] {func.__name__} 失败: {e}\n{traceback.format_exc()}")
                    raise

        return wrapper

    return decorator


class MessageDatabase(metaclass=MetaSingleton):
    """
    消息数据库管理器

    负责：
    - 创建和初始化数据库表
    - 消息的 CRUD 操作
    - 状态转换管理
    - 回复链管理
    - 并发控制（锁和心跳）
    """

    # messages 表所有列名（与实际数据库表结构一致）
    _MESSAGE_COLUMNS = [
        "id", "timestamp", "sender", "receiver", "content", "title",
        "workdir", "status", "created_at", "read_at", "archived_at",
        "locked_by", "locked_at", "heartbeat_at", "process_id",
        "reply_to", "chain_head_id", "chain_context", "error",
        "caller", "caller_session_id", "is_chain_head", "is_cancelled",
        "timeout", "is_paused", "collapsed", "is_archived",
        "pending_delete", "nest", "platform_msg_id", "feishu_chat_id",
    ]

    # 需要通知前端的消息字段（这些字段变化时触发 WebSocket 广播）
    _BROADCAST_FIELDS = {
        "status", "reply_to", "content", "nest",
        "workdir", "is_archived", "sender", "receiver",
    }

    # 有效消息状态值（用于 update_messages 验证）
    VALID_STATUSES = {
        "unread", "read", "replied", "processing", "accepting",
        "accepted", "paused", "interrupted", "cancelled", "failed",
        "done", "archived",
    }

    SCHEMA_VERSION = 3  # 升级版本以支持 pending_delete 字段

    @staticmethod
    def _notify_webui(action: str, msg_id: int, details: dict[str, Any] | None = None, source: str = "unknown") -> None:
        """
        通知所有 WebUI 客户端消息数据变更

        通过 WebSocket 广播 message_update 消息，携带变更字段详情，
        前端可直接增量更新 DOM，无需重新请求 /api/messages。

        线程安全设计（与 TaskDatabase._notify_webui 对齐）：
        - 主进程：通过 ws_manager._loop（ConnectionManager 首个连接时捕获的主事件循环）
          使用 asyncio.run_coroutine_threadsafe() 线程安全地调度广播协程。
          该 API 从任何线程（事件循环线程或工作线程）调用都是安全的。
        - 子进程：子进程的 ws_manager 没有活跃连接和 _loop，
          通过 HTTP 请求主进程的内部分发接口触发广播。

        ⚡ 性能优化（Task#1438）：在 CLI 子进程中，peng.webui.websocket 模块尚未被导入，
        直接 import 会拉起 fastapi + lark_oapi 等庞大依赖链（耗时 3+ 秒）。
        通过 sys.modules 检查避免在子进程中触发这个昂贵的导入。

        Args:
            action: 操作类型（created/updated/deleted/archived/unarchived）
            msg_id: 消息ID
            details: 变更字段详情（可选）
            source: 消息来源（webui/cli/api/unknown）
        """
        try:
            import sys

            # ⚡ 性能优化：仅在模块已加载时才访问（主进程），避免子进程中的 3 秒 import 开销
            ws_module = sys.modules.get("peng.webui.websocket")
            if ws_module is not None:
                ws_manager = getattr(ws_module, "manager", None)
                loop = getattr(ws_manager, "_loop", None) if ws_manager else None
                if loop and ws_manager and ws_manager.active_connections:
                    # 主进程：使用存储的事件循环引用，线程安全地调度广播协程
                    broadcast_fn = getattr(ws_module, "broadcast_message_update", None)
                    if broadcast_fn:
                        asyncio.run_coroutine_threadsafe(
                            broadcast_fn(msg_id, action, details or {}, source=source),
                            loop,
                        )
                        return
            # 子进程或 WebUI 未启动：通过 HTTP 请求主进程触发广播
            MessageDatabase._notify_webui_via_http(action, msg_id, details or {}, source=source)
        except Exception as e:
            # 🔧 Task#287：提升日志级别为 warning，确保通知失败可见（之前 debug 在 info 级别下不可见）
            logger.warning(f"[MsgDB] 通知WebUI失败(msg#{msg_id}, {action}): {e}")

    @staticmethod
    def _notify_webui_via_http(action: str, msg_id: int, details: dict[str, Any], source: str = "unknown") -> None:
        """
        子进程中通过 HTTP 请求主进程内部分发接口触发 WebSocket 广播

        消息操作在子进程中运行时，子进程的 WebSocket ConnectionManager 没有活跃连接，
        因此需要通过 HTTP 请求主进程的 /api/internal/broadcast-message 端点来广播。
        """
        try:
            from peng.config.loader import load_config
            import urllib.request
            import urllib.error

            config = load_config()
            host = config.webui.host if hasattr(config, 'webui') and config.webui else "localhost"
            port = config.webui.port if hasattr(config, 'webui') and config.webui else 6688
            # 绑定到 0.0.0.0 时用 localhost 连接
            if host == "0.0.0.0":
                host = "localhost"

            url = f"http://{host}:{port}/api/internal/broadcast-message"
            payload = json.dumps({
                "msg_id": msg_id,
                "action": action,
                "details": details,
                "source": source,
            }).encode("utf-8")

            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status != 200:
                    logger.warning(f"[MsgDB] HTTP广播返回非200: {resp.status}")
        except Exception as e:
            # 🔧 Task#287：提升日志级别为 warning，确保广播失败可见
            logger.warning(f"[MsgDB] HTTP广播失败(msg#{msg_id}, {action}): {e}")

    def _get_connection(self):
        """
        获取配置好的数据库连接

        使用PostgreSQL连接池获取连接

        Returns:
            配置好的数据库连接
        """
        return get_connection()

    # SQL: 创建索引
    CREATE_INDEXES = [
        "CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status)",
        "CREATE INDEX IF NOT EXISTS idx_messages_reply_to ON messages(reply_to)",
        "CREATE INDEX IF NOT EXISTS idx_messages_chain_head ON messages(chain_head_id)",
        "CREATE INDEX IF NOT EXISTS idx_messages_caller ON messages(caller)",
        "CREATE INDEX IF NOT EXISTS idx_messages_caller_session ON messages(caller_session_id)",
        "CREATE INDEX IF NOT EXISTS idx_messages_heartbeat ON messages(heartbeat_at)",
        "CREATE INDEX IF NOT EXISTS idx_messages_locked ON messages(locked_by)",
        "CREATE INDEX IF NOT EXISTS idx_messages_pending_delete ON messages(pending_delete)",
    ]

    # SQL: 创建消息控制表（用于暂停/恢复/重启/取消等控制）
    CREATE_MESSAGE_CONTROLS_TABLE = """
        CREATE TABLE IF NOT EXISTS message_controls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            processed INTEGER DEFAULT 0,

            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
        )
    """

    # SQL: 创建消息控制表索引
    CREATE_MESSAGE_CONTROLS_INDEXES = [
        "CREATE INDEX IF NOT EXISTS idx_message_controls_message ON message_controls(message_id)",
        "CREATE INDEX IF NOT EXISTS idx_message_controls_processed ON message_controls(processed)",
    ]

    def __init__(self):
        """
        初始化数据库
        """
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        """确保表存在"""
        # PostgreSQL表通过init_postgres_db.py创建
        # 这里只验证连接和表是否可用
        from peng.db.connection import get_cursor

        with get_cursor() as cursor:
            # 验证messages表是否存在
            cursor.execute("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'messages'
            """)
            result = cursor.fetchone()
            if not result:
                raise RuntimeError(
                    "messages表不存在，请运行 python src/peng/db/init_postgres_db.py"
                )

    def _execute_query_one(self, query: str, params: tuple | None = None) -> dict[str, Any] | None:
        """
        执行查询并返回单行结果

        Args:
            query: SQL查询语句（使用%s占位符）
            params: 查询参数

        Returns:
            单行字典，没有结果返回None
        """
        with get_cursor() as cursor:
            cursor.execute(query, params)
            row = cursor.fetchone()
            return row_to_dict(row) if row else None

    def _execute_query_all(self, query: str, params: tuple | None = None) -> list[dict[str, Any]]:
        """
        执行查询并返回所有行结果

        Args:
            query: SQL查询语句（使用%s占位符）
            params: 查询参数

        Returns:
            字典列表，没有结果返回空列表
        """
        with get_cursor() as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return rows_to_list(rows)

    # ==================== 消息 CRUD ====================

    @retry_on_db_error(max_retries=3, delay=0.1)
    def create_message(self, **kwargs: Any) -> tuple[int, dict[str, Any] | None]:
        """
        创建消息（通用字典参数形式）

        所有字段通过 kwargs 传入，与 update_messages 风格一致。
        常用字段：content, sender, receiver, workdir, status, timestamp,
        reply_to, caller, caller_session_id, timeout, check_duplicate,
        id, nest, platform_msg_id, feishu_chat_id

        Args:
            **kwargs: 消息字段（content 必填）

        Returns:
            (msg_id, duplicate_msg): 消息ID和重复消息
        """
        # 提取参数
        content = kwargs.get("content", "")
        sender = kwargs.get("sender")
        receiver = kwargs.get("receiver")
        workdir = kwargs.get("workdir")
        status = kwargs.get("status", "unread")
        timestamp = kwargs.get("timestamp")
        reply_to = kwargs.get("reply_to")
        caller = kwargs.get("caller")
        caller_session_id = kwargs.get("caller_session_id")
        timeout = kwargs.get("timeout", 0)
        check_duplicate = kwargs.get("check_duplicate", False)
        id = kwargs.get("id")
        nest = kwargs.get("nest")
        platform_msg_id = kwargs.get("platform_msg_id")
        feishu_chat_id = kwargs.get("feishu_chat_id")
        # 名字映射：将简短名字映射到完整名字（修复P0问题1）
        name_mapping = {
            "peng": AGENT_NAME,
            "鹏": AGENT_NAME,
            "鹏🐣": AGENT_NAME,
            "master": MASTER_NAME,
        }
        if sender in name_mapping:
            sender = name_mapping[sender]
        if receiver in name_mapping:
            receiver = name_mapping[receiver]

        if sender is None and receiver is None:
            sender = AGENT_NAME
            receiver = MASTER_NAME
        elif receiver is None:
            if sender == AGENT_NAME:
                receiver = MASTER_NAME
            else:
                receiver = AGENT_NAME
        elif sender is None:
            if receiver == AGENT_NAME:
                sender = MASTER_NAME
            else:
                sender = AGENT_NAME

        # 从 content 中解析 @nest=<nest> 和 @workdir=<workdir>（优先级大于参数）
        if content:
            nest_match = re.search(r"@nest=(\S+)", content)
            if nest_match:
                parsed_nest = nest_match.group(1)
                # 验证 nest 是否已连接
                try:
                    from peng.nest.bridge import NestBridgeManager

                    manager = NestBridgeManager.get_instance()
                    if manager and manager.get_connection(parsed_nest):
                        nest = parsed_nest
                        logger.info(f"[create_message] 从content解析到有效nest: {nest}")
                    else:
                        available = (
                            [c["name"] for c in manager.list_connections()] if manager else []
                        )
                        logger.warning(
                            f"[create_message] content中指定了@nest={parsed_nest}，"
                            f"但该nest未连接。可用连接: {available or '无'}"
                        )
                except Exception as e:
                    logger.warning(f"[create_message] 验证nest连接失败: {e}")

            workdir_match = re.search(r"@workdir=(\S+)", content)
            if workdir_match:
                parsed_workdir = workdir_match.group(1)
                workdir = parsed_workdir
                logger.info(f"[create_message] 从content解析到workdir: {workdir}")

            reply_match = re.search(r"@reply#(\d+)", content)
            if reply_match:
                parsed_reply_to = int(reply_match.group(1))
                reply_to = parsed_reply_to
                logger.info(f"[create_message] 从content解析到reply_to: {reply_to}")

        if timestamp is None:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        now = datetime.now().isoformat()

        # 确定链首ID和是否为链首
        chain_head_id = None
        is_chain_head = 0

        if reply_to:
            # 回复消息：获取链首
            original = self.get_message(reply_to)
            if original:
                # 如果原消息是链首，则链首是它；否则继承原消息的链首
                if original.get("is_chain_head"):
                    chain_head_id = reply_to
                elif original.get("chain_head_id"):
                    chain_head_id = original.get("chain_head_id")
                else:
                    # 原消息也不是链首且没有 chain_head_id，需要向上查找真正的链首
                    chain_head = self.get_chain_head(reply_to)
                    if chain_head:
                        chain_head_id = chain_head["id"]
                # 回复消息的 nest 和 workdir 必须跟随根消息（链首）
                chain_head_msg = self.get_message(chain_head_id) if chain_head_id else original
                if not workdir:
                    workdir = chain_head_msg.get("workdir") or original.get("workdir")
                    logger.debug(
                        f"[create_message] 从链首消息#{chain_head_id}继承工作目录: {workdir}"
                    )
                if not nest:
                    nest = chain_head_msg.get("nest") or original.get("nest")
                    logger.debug(f"[create_message] 从链首消息#{chain_head_id}继承nest: {nest}")
            else:
                return 0, {
                    "success": False,
                    "error": f"回复消息失败，找不到要回复的消息#{reply_to}。",
                    "is_duplicate": False,
                }
        else:
            # 非回复消息：标记为链首
            is_chain_head = 1
            # 链首消息的 chain_head_id 应该设置为自己的ID
            # 在INSERT后立即UPDATE

        # 自动从 config.webui.nest 获取 nest 值（如果调用方未显式指定）
        if not nest or not workdir:
            try:
                from peng.config.loader import load_config

                config = load_config()
                if not nest:
                    nest = getattr(getattr(config, "webui", None), "nest", "") or ""
                if not workdir:
                    workdir = getattr(getattr(config, "webui", None), "workdir", "") or ""
            except Exception:
                nest = ""
                workdir = ""

        # 检查重复消息

        if receiver == MASTER_NAME:
            check_duplicate = True
        duplicate_msg = None
        if check_duplicate:
            # 新对话（reply_to=None）不检查重复，因为用户明确表示开始新消息
            # 仅在回复链内检查重复（避免同一对话中重复发送相同内容）
            if reply_to:
                # 优先查找待处理或处理中的消息
                duplicate_msg = self.find_similar_message(
                    content, chain_head_id, status_filter=["unread", "done"]
                )
                if duplicate_msg:
                    duplicate_msg["is_duplicate"] = True
                    duplicate_msg["error"] = f"已有重复消息#{duplicate_msg['id']}"
                    return (0, duplicate_msg)

        msg_id = 0
        with self._get_connection() as conn:
            # 根据是否指定 id 使用不同的 INSERT 语句
            if id:
                # 指定 id：使用方法1直接插入指定 id
                cursor = conn.execute(
                    """
                    INSERT INTO messages (
                        id, timestamp, content, status, created_at,
                        reply_to, chain_head_id, caller, caller_session_id,
                        is_chain_head, timeout, sender, receiver, workdir, nest,
                        platform_msg_id, feishu_chat_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        id,
                        timestamp,
                        content,
                        status,
                        now,
                        reply_to,
                        chain_head_id,
                        caller,
                        caller_session_id,
                        is_chain_head,
                        timeout,
                        sender,
                        receiver,
                        workdir,
                        nest,
                        platform_msg_id or "",
                        feishu_chat_id or "",
                    ),
                )
                msg_id = id
                # 🔧 推进 PostgreSQL SERIAL 序列，防止后续自动生成的 id 与显式指定的 id 冲突
                conn.execute(
                    "SELECT setval(pg_get_serial_sequence('messages', 'id'), "
                    "(SELECT COALESCE(MAX(id), 1) FROM messages), true)"
                )
            else:
                # 未指定 id：使用序列自动生成
                cursor = conn.execute(
                    """
                    INSERT INTO messages (
                        timestamp, content, status, created_at,
                        reply_to, chain_head_id, caller, caller_session_id,
                        is_chain_head, timeout, sender, receiver, workdir, nest,
                        platform_msg_id, feishu_chat_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        timestamp,
                        content,
                        status,
                        now,
                        reply_to,
                        chain_head_id,
                        caller,
                        caller_session_id,
                        is_chain_head,
                        timeout,
                        sender,
                        receiver,
                        workdir,
                        nest,
                        platform_msg_id or "",
                        feishu_chat_id or "",
                    ),
                )
                msg_id = cursor.lastrowid

            # 对于链首消息，立即设置 chain_head_id 为自己的ID
            if is_chain_head:
                cursor.execute(
                    "UPDATE messages SET chain_head_id = %s WHERE id = %s",
                    (msg_id, msg_id),
                )
                # 注意：不要手动 commit，连接会在退出 with 块时自动提交

        msg = self.get_message(msg_id)
        logger.info(
            f"[create_message] 消息创建成功: msg_id={msg_id}, from={msg['sender']}, to={msg['receiver']}, reply_to={reply_to}"
        )

        # 🔧 通知 WebUI：新消息创建
        if msg:
            details = {k: v for k, v in msg.items() if k in self._BROADCAST_FIELDS and v is not None}
            # 传递 caller 作为 source，前端据此判断是否自动选中
            source = caller or "unknown"
            self._notify_webui("created", msg_id, details, source=source)

        return (msg_id, msg)

    def find_by_platform_msg_id(self, platform_msg_id: str) -> dict[str, Any] | None:
        """根据飞书消息ID查找对应的消息记录

        Args:
            platform_msg_id: 飞书平台消息ID（如 om_xxx）

        Returns:
            消息记录字典或None
        """
        if not platform_msg_id:
            return None
        try:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "SELECT * FROM messages WHERE platform_msg_id = %s ORDER BY id DESC LIMIT 1",
                    (platform_msg_id,),
                )
                row = cursor.fetchone()
                if row:
                    from peng.db.connection import row_to_dict

                    return row_to_dict(row)
                return None
        except Exception as e:
            logger.error(f"查找platform_msg_id失败: {e}", exc_info=True)
            return None

    @retry_on_db_error(max_retries=3, delay=0.1)
    def get_message(self, msg_id: int) -> dict[str, Any] | None:
        """
        获取指定消息

        Args:
            msg_id: 消息ID

        Returns:
            消息字典，不存在返回 None
        """
        with get_cursor() as cursor:
            cursor.execute("SELECT * FROM messages WHERE id = %s", (msg_id,))
            row = cursor.fetchone()
            return row_to_dict(row) if row else None

    def get_unread(self) -> list[dict[str, Any]]:
        """获取所有未读消息（不包括归档）"""
        return self.get_by_status("unread")

    def get_by_status(
        self,
        status: str,
        include_archived: bool = False,
        order: str = "ASC",
        receiver: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        按状态获取消息

        Args:
            status: 消息状态
            include_archived: 是否包含归档消息（默认 False）
            order: 排序顺序，"ASC" 为从老到新，"DESC" 为从新到老（默认 "ASC"）
            receiver: 接收者过滤（可选）

        Returns:
            消息列表
        """
        try:
            order_clause = "ASC" if order == "ASC" else "DESC"
            with get_cursor() as cursor:
                # 构建查询条件
                conditions = ["status = %s"]
                params = [status]

                if not include_archived:
                    conditions.append("(is_archived = 0 OR is_archived IS NULL)")

                if receiver:
                    conditions.append("receiver = %s")
                    params.append(receiver)

                where_clause = " AND ".join(conditions)
                query = (
                    f"SELECT * FROM messages WHERE {where_clause} ORDER BY timestamp {order_clause}"
                )
                cursor.execute(query, params)
                return rows_to_list(cursor.fetchall())
        except Exception as e:
            import traceback

            logger.error(
                f"❌ get_by_status() - database.py:500: Exception - {e}\n{traceback.format_exc()}"
            )
            _log_with_throttle(
                "warning", f"获取 {status} 状态消息失败：{e}", f"get_status_{status}"
            )
            return []

    def get_by_receiver(
        self,
        receiver: str,
        include_archived: bool = False,
        order: str = "ASC",
    ) -> list[dict[str, Any]]:
        """
        按接收者获取消息

        Args:
            receiver: 接收者名称
            include_archived: 是否包含归档消息（默认 False）
            order: 排序顺序，"ASC" 为从老到新，"DESC" 为从新到老（默认 "ASC"）

        Returns:
            消息列表
        """
        order_clause = "ASC" if order == "ASC" else "DESC"
        with self._get_connection() as conn:
            # 构建查询条件
            conditions = ["receiver = %s"]
            params = [receiver]

            if not include_archived:
                conditions.append("(is_archived = 0 OR is_archived IS NULL)")

            where_clause = " AND ".join(conditions)
            query = f"SELECT * FROM messages WHERE {where_clause} ORDER BY timestamp {order_clause}"
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    @retry_on_db_error(max_retries=3, delay=0.1)
    def get_all(
        self,
        ascending: bool = False,
        include_archived: bool = True,
        receiver: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        获取所有消息

        Args:
            ascending: 是否按从老到新排序
            include_archived: 是否包含归档消息（默认 True）
            receiver: 接收者过滤（可选）

        Returns:
            消息列表
        """
        order = "ASC" if ascending else "DESC"
        with self._get_connection() as conn:
            # 构建查询条件
            conditions = []
            params = []

            if not include_archived:
                conditions.append("(is_archived = 0 OR is_archived IS NULL)")

            if receiver:
                conditions.append("receiver = %s")
                params.append(receiver)

            where_clause = " AND ".join(conditions) if conditions else "1=1"
            query = f"SELECT * FROM messages WHERE {where_clause} ORDER BY timestamp {order}"
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def get_finished(
        self,
        limit: int = 20,
        include_archived: bool = True,
        order: str = "ASC",
        receiver: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        获取已完成的消息（包括归档）

        Args:
            limit: 返回数量限制
            include_archived: 是否包含归档消息（默认 True）
            order: 排序顺序，"ASC" 为从老到新，"DESC" 为从新到老（默认 "ASC"）
            receiver: 接收者过滤（可选）

        Returns:
            消息列表
        """
        order_clause = "ASC" if order == "ASC" else "DESC"
        with self._get_connection() as conn:
            # 构建查询条件
            conditions = ["status = 'done'"]
            params = []

            if not include_archived:
                conditions.append("(is_archived = 0 OR is_archived IS NULL)")
            else:
                conditions.append("is_archived = 1")

            if receiver:
                conditions.append("receiver = %s")
                params.append(receiver)

            # 对于归档的情况，使用 OR 条件
            if include_archived:
                where_clause = "(status = 'done' OR is_archived = 1)"
            else:
                where_clause = " AND ".join(conditions)

            if receiver:
                where_clause += " AND receiver = %s"
                params.append(receiver)

            query = f"""
                SELECT * FROM messages
                WHERE {where_clause}
                ORDER BY timestamp {order_clause}
                LIMIT %s
            """
            params.append(limit)
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def count_by_status(self) -> dict[str, int]:
        """统计各状态消息数量"""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT status, COUNT(*) as count FROM messages GROUP BY status")
            return {row["status"]: row["count"] for row in cursor.fetchall()}

    def count_by_status_list(self, statuses: list[str]) -> dict[str, int]:
        """
        统计指定状态的消息数量

        Args:
            statuses: 状态列表

        Returns:
            状态到数量的映射
        """
        if not statuses:
            return {}

        placeholders = ",".join(["%s"] * len(statuses))
        with self._get_connection() as conn:
            cursor = conn.execute(
                f"SELECT status, COUNT(*) as count FROM messages WHERE status IN ({placeholders}) GROUP BY status",
                tuple(statuses),
            )
            return {row["status"]: row["count"] for row in cursor.fetchall()}

    def get_processing_messages_with_heartbeat(self) -> list[dict[str, Any]]:
        """
        获取所有 processing 状态的消息及其心跳信息

        Returns:
            消息列表，包含 id, process_id, heartbeat_at, locked_by, locked_at
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, process_id, heartbeat_at, locked_by, locked_at
                FROM messages WHERE status = 'processing'
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_tasks_by_message_status(self, message_id: int) -> list[dict[str, Any]]:
        """
        获取消息关联的事务及其状态

        Args:
            message_id: 消息ID

        Returns:
            事务列表，包含 id, status
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, status FROM tasks WHERE message_id = %s
                """,
                (message_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def update_messages_status_batch(
        self,
        msg_ids: list[int],
        status: str,
        extra_fields: dict[str, Any] | None = None,
    ) -> int:
        """
        批量更新消息状态

        Args:
            msg_ids: 消息ID列表
            status: 新状态
            extra_fields: 额外更新的字段

        Returns:
            更新的行数
        """
        kwargs = {"status": status}
        if extra_fields:
            kwargs.update(extra_fields)
        return self.update_messages(msg_ids, **kwargs)

    def get_messages_by_ids(self, msg_ids: list[int]) -> list[dict[str, Any]]:
        """
        批量获取消息

        Args:
            msg_ids: 消息ID列表

        Returns:
            消息列表
        """
        if not msg_ids:
            return []

        placeholders = ",".join(["%s"] * len(msg_ids))
        with self._get_connection() as conn:
            cursor = conn.execute(
                f"SELECT id, sender FROM messages WHERE id IN ({placeholders})",
                tuple(msg_ids),
            )
            return [dict(row) for row in cursor.fetchall()]

    def count_reply_by_message_id(self, message_id: int) -> int:
        """
        统计消息的回复数量

        Args:
            message_id: 消息ID

        Returns:
            回复数量
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) as reply_count FROM messages WHERE reply_to = %s",
                (message_id,),
            )
            row = cursor.fetchone()
            return row["reply_count"] if row else 0

    def get_all_messages(self) -> list[dict[str, Any]]:
        """
        获取所有消息的基本信息

        Returns:
            消息列表，包含 id, status
        """
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT id, status FROM messages")
            return [dict(row) for row in cursor.fetchall()]

    def get_messages_by_status_and_ids(
        self, status: str, msg_ids: list[int]
    ) -> list[dict[str, Any]]:
        """
        按状态和ID列表获取消息

        Args:
            status: 消息状态
            msg_ids: 消息ID列表

        Returns:
            消息列表
        """
        if not msg_ids:
            return []

        placeholders = ",".join(["%s"] * len(msg_ids))
        with self._get_connection() as conn:
            cursor = conn.execute(
                f"SELECT id FROM messages WHERE status = %s AND id IN ({placeholders})",
                [status] + list(msg_ids),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_messages_in_time_window(
        self,
        exclude_id: int,
        receiver: str,
        status: str = "unread",
        start_time: str = None,
        end_time: str = None,
    ) -> list[dict[str, Any]]:
        """
        获取指定时间窗口内的消息

        Args:
            exclude_id: 排除的消息ID
            receiver: 接收者
            status: 消息状态
            start_time: 开始时间（ISO格式）
            end_time: 结束时间（ISO格式）

        Returns:
            消息列表
        """
        conditions = ["id != %s", "receiver = %s", "status = %s"]
        params = [exclude_id, receiver, status]

        if start_time and end_time:
            conditions.append("timestamp BETWEEN %s AND %s")
            params.extend([start_time, end_time])

        with self._get_connection() as conn:
            cursor = conn.execute(
                f"SELECT * FROM messages WHERE {' AND '.join(conditions)} ORDER BY timestamp ASC",
                params,
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_message_by_status_and_sender_not(
        self, status: str, sender: str, limit: int = 1
    ) -> dict[str, Any] | None:
        """
        获取指定状态且发送者不是sender的消息

        Args:
            status: 消息状态
            sender: 排除的发送者
            limit: 返回数量限制

        Returns:
            消息字典
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT id FROM messages
                WHERE status = %s AND sender != %s
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (status, sender, limit),
            )
            rows = cursor.fetchall()
            return dict(rows[0]) if rows else None

    def get_messages_by_status_with_archived(
        self,
        statuses: list[str],
        archive_threshold: str | None = None,
        is_archived: int | None = 0,
    ) -> list[int]:
        """
        获取指定状态的消息（支持归档过滤）

        Args:
            statuses: 状态列表
            archive_threshold: 归档阈值时间（ISO格式）
            is_archived: 归档标记（0未归档，1已归档）

        Returns:
            消息ID列表
        """
        conditions = []
        params = []

        placeholders = ",".join(["%s"] * len(statuses))
        conditions.append(f"status IN ({placeholders})")
        params.extend(statuses)

        if is_archived is not None:
            conditions.append(f"(is_archived = %s OR is_archived IS NULL)")
            params.append(is_archived)

        if archive_threshold:
            conditions.append("(archived_at IS NULL OR archived_at < %s)")
            params.append(archive_threshold)

        where_clause = " AND ".join(conditions)

        with self._get_connection() as conn:
            cursor = conn.execute(
                f"SELECT id FROM messages WHERE {where_clause} ORDER BY id",
                tuple(params),
            )
            return [row["id"] for row in cursor.fetchall()]

    def update_messages_archived(self, msg_ids: list[int]) -> int:
        """
        批量归档消息

        Args:
            msg_ids: 消息ID列表

        Returns:
            更新的行数
        """
        return self.update_messages(
            msg_ids, is_archived=1, archived_at=datetime.now().isoformat()
        )

    def get_unprocessed_message_controls(self) -> list[dict[str, Any]]:
        """
        获取未处理的控制项（按created_at排序）

        Returns:
            控制项列表，包含 id, message_id, action, created_at
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, message_id, action, created_at
                FROM message_controls
                WHERE processed = 0
                ORDER BY created_at ASC
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def mark_message_control_processed(self, control_id: int) -> bool:
        """
        标记消息控制为已处理

        Args:
            control_id: 控制ID

        Returns:
            是否成功
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "UPDATE message_controls SET processed = 1 WHERE id = %s",
                (control_id,),
            )
            return cursor.rowcount > 0

    def insert_message_control(self, message_id: int, action: str) -> int | None:
        """
        插入消息控制记录

        Args:
            message_id: 消息ID
            action: 控制动作

        Returns:
            插入的控制记录ID，失败返回None
        """
        try:
            from datetime import datetime

            with self._get_connection() as conn:
                cursor = conn.execute(
                    """INSERT INTO message_controls (message_id, action, created_at, processed)
                       VALUES (%s, %s, %s, 0)
                       RETURNING id""",
                    (message_id, action, datetime.now().isoformat()),
                )
                row = cursor.fetchone()
                conn.commit()
                return row["id"] if row else None
        except Exception as e:
            import logging

            logger = logging.getLogger(__name__)
            logger.error(f"插入消息控制失败: {e}")
            return None

    def get_paused_message_ids(self) -> list[int]:
        """
        获取所有暂停的消息ID

        Returns:
            消息ID列表
        """
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT id FROM messages WHERE status = 'paused'")
            return [row["id"] for row in cursor.fetchall()]

    def update_message_with_heartbeat(
        self, msg_id: int, heartbeat_at: str, status: str | None = None
    ) -> bool:
        """
        更新消息的心跳时间和可选的状态

        Args:
            msg_id: 消息ID
            heartbeat_at: 心跳时间（ISO格式）
            status: 新状态（可选）

        Returns:
            是否成功
        """
        kwargs = {"heartbeat_at": heartbeat_at}
        if status:
            kwargs["status"] = status
        return self.update_message(msg_id, **kwargs)

    def update_message_process_id(
        self, msg_id: int, process_id: int | None = None, heartbeat_at: str | None = None
    ) -> bool:
        """
        更新消息的进程ID和可选的心跳时间

        Args:
            msg_id: 消息ID
            process_id: 进程ID
            heartbeat_at: 心跳时间（ISO格式，可选）

        Returns:
            是否成功
        """
        kwargs = {"process_id": process_id}
        if heartbeat_at:
            kwargs["heartbeat_at"] = heartbeat_at
        return self.update_messages([msg_id], _where_conditions=["status = 'processing'"], **kwargs) > 0

    def delete_message(self, msg_id: int, force=False) -> bool:
        """删除指定消息"""
        if not force and msg_id <= 1:  # 消息#1为鹏🐣-忠实助手，禁止删除
            import traceback

            logger.error(f"消息#1为鹏🐣-忠实助手，禁止删除 {'\n'.join(traceback.format_stack())}")
            return False

        # 🔧 删除前先获取消息信息（用于通知前端）
        msg_info = self.get_message(msg_id)

        with self._get_connection() as conn:
            cursor = conn.execute("DELETE FROM messages WHERE id = %s", (msg_id,))
            deleted = cursor.rowcount > 0

        # 🔧 通知 WebUI：消息删除
        if deleted:
            details = {}
            if msg_info:
                details["reply_to"] = msg_info.get("reply_to")
                details["chain_head_id"] = msg_info.get("chain_head_id")
            self._notify_webui("deleted", msg_id, details)

        return deleted

    def get_messages_by_reply_to(self, reply_to: int) -> list[dict[str, Any]]:
        """
        获取所有回复指定消息的子消息

        Args:
            reply_to: 被回复的消息 ID

        Returns:
            子消息列表（仅包含 id 字段）
        """
        return self._execute_query_all("SELECT id FROM messages WHERE reply_to = %s", (reply_to,))

    def delete_message_recursive(self, msg_id: int, force=False) -> int:
        """递归删除指定消息及其所有子消息
        
        🔧 Task#1346：批量优化，消除N+1查询
        旧实现逐层递归 get_children + DELETE，N个子消息产生 2N次连接池操作。
        新实现用 PostgreSQL 递归CTE 一次性查出所有后代ID，然后一条SQL批量DELETE。
        """
        if not force and msg_id <= 1:  # 消息#1为鹏🐣-忠实助手，禁止删除
            import traceback

            logger.error(f"消息#1为鹏🐣-忠实助手，禁止删除 {'\n'.join(traceback.format_stack())}")
            return 0

        # 使用递归CTE一次性查出所有后代消息ID（含自身）
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                WITH RECURSIVE descendants AS (
                    -- 起点：自身
                    SELECT id FROM messages WHERE id = %s
                    UNION ALL
                    -- 递归：所有 reply_to 指向已找到消息的消息
                    SELECT m.id FROM messages m
                    INNER JOIN descendants d ON m.reply_to = d.id
                )
                DELETE FROM messages WHERE id IN (SELECT id FROM descendants)
                """,
                (msg_id,),
            )
            deleted_count = cursor.rowcount

        # 🔧 Task#867：只广播一次根消息删除事件
        # 前端收到后会 loadMessages 重新加载，所有被删除的消息都会从列表中消失
        # 不逐条广播，避免通知风暴和前端并发 loadMessages 竞态
        if deleted_count > 0:
            self._notify_webui("deleted", msg_id, {})

        return deleted_count

    def set_pending_delete(self, msg_id: int) -> bool:
        """
        设置消息待删除标记

        Args:
            msg_id: 消息 ID

        Returns:
            是否成功
        """
        return self.update_message(msg_id, pending_delete=1)

    def get_pending_delete_messages(self) -> list[int]:
        """
        获取所有待删除消息 ID

        Returns:
            待删除消息 ID 列表
        """
        rows = self._execute_query_all("SELECT id FROM messages WHERE pending_delete = 1")
        return [row["id"] for row in rows]

    def clear_pending_delete(self, msg_id: int) -> bool:
        """
        清除消息待删除标记（删除失败时恢复）

        Args:
            msg_id: 消息 ID

        Returns:
            是否成功
        """
        return self.update_message(msg_id, pending_delete=0)

    def find_similar_message(
        self, content: str, chain_head_id: int, status_filter: list[str] | None = None
    ) -> dict[str, Any] | None:
        """
        查找内容相似的老消息

        Args:
            content: 要搜索的消息内容
            status_filter: 状态过滤列表（如 ['unread', 'processing']），None 表示不过滤

        Returns:
            相似消息的字典，如果没有找到则返回 None
        """

        # 首先尝试完全匹配
        with self._get_connection() as conn:
            chain_head_id_param = (
                f"AND chain_head_id = %s" if chain_head_id else f"AND chain_head_id <> %s"
            )
            if status_filter:
                placeholders = ",".join(["%s"] * len(status_filter))
                query = f"""
                    SELECT * FROM messages
                    WHERE content = %s
                    {chain_head_id_param}
                    AND status IN ({placeholders})
                    AND receiver = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                """
                params = [content, chain_head_id] + status_filter + [AGENT_NAME]
            else:
                query = """
                    SELECT * FROM messages
                    WHERE content = %s
                    {chain_head_id_param}
                    AND receiver = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                """
                params = [content, chain_head_id, AGENT_NAME]

            cursor = conn.execute(query, params)
            row = cursor.fetchone()

            if row:
                return dict(row)

        return None

    # ==================== 状态转换 ====================

    def _get_current_status(self, msg_id: int) -> str | None:
        """获取消息当前状态"""
        max_retries = 3
        retry_delay = 0.05

        for attempt in range(max_retries):
            try:
                with self._get_connection() as conn:
                    # PostgreSQL不需要PRAGMA busy_timeout
                    cursor = conn.execute("SELECT status FROM messages WHERE id = %s", (msg_id,))
                    row = cursor.fetchone()
                    return row["status"] if row else None
            except psycopg2.OperationalError as e:
                import traceback

                tb = traceback.format_exc()
                error_msg = str(e)
                if "locked" in error_msg.lower() or "busy" in error_msg.lower():
                    if attempt < max_retries - 1:
                        _log_with_throttle(
                            "debug",
                            f"数据库锁竞争 (msg_id={msg_id}, "
                            f"尝试={attempt + 1}/{max_retries}): {e}\n{tb}",
                            f"status_lock_{msg_id}",
                        )
                        time.sleep(retry_delay * (attempt + 1))
                        continue
                _log_with_throttle(
                    "warning",
                    f"获取消息状态失败 (msg_id={msg_id}, 尝试={attempt + 1}/{max_retries}): {e}\n{tb}",
                    f"status_error_{msg_id}",
                )
                return None
            except Exception as e:
                import traceback

                logger.error(
                    f"❌ _get_current_status() - database.py:731: Exception - {e}\n{traceback.format_exc()}"
                )
                _log_with_throttle(
                    "warning",
                    f"获取消息状态失败 (msg_id={msg_id}): {e}",
                    f"status_error_{msg_id}",
                )
                return None

        return None

    @retry_on_db_error(max_retries=3, delay=0.1)
    def _update_status(
        self, msg_id: int, status: str, extra_fields: dict[str, Any] | None = None
    ) -> bool:
        """更新消息状态

        Args:
            msg_id: 消息ID
            status: 新状态
            extra_fields: 额外的更新字段

        Returns:
            是否成功更新
        """
        kwargs = {"status": status}
        if extra_fields:
            kwargs.update(extra_fields)
        return self.update_message(msg_id, **kwargs)

    def update_messages(
        self,
        msg_ids: list[int],
        _where_conditions: list[str] | None = None,
        **kwargs: Any,
    ) -> int:
        """
        批量更新消息

        所有 messages 表字段的 UPDATE 操作必须通过本方法实现。
        除 create_message 外，禁止在 database.py 外部直接执行 SQL 操作 messages 表字段。

        Args:
            msg_ids: 消息ID列表
            _where_conditions: 额外的 WHERE 条件列表（如 ["status = 'processing'"]），
                用于需要原子性条件更新的场景。
                注意：这是内部参数，外部调用者不应使用。
        Returns:
            更新的行数
        """
        if not msg_ids:
            return 0

        if not kwargs:
            return 0

        # 归档状态变更时，自动更新归档时间（如果未显式提供 archived_at）
        if "is_archived" in kwargs and "archived_at" not in kwargs:
            kwargs["archived_at"] = datetime.now().isoformat()

        # 验证状态值
        if "status" in kwargs:
            if kwargs["status"] not in self.VALID_STATUSES:
                logger.warning(f"[update_messages] 无效状态值: {kwargs['status']}")
                return 0

        fields = []
        values = []
        for key, value in kwargs.items():
            # 如果值是 dict 或 list 类型，需要转换为 JSON 字符串
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            fields.append(f"{key} = %s")
            values.append(value)

        placeholders = ",".join(["%s"] * len(msg_ids))

        # 构建完整的 WHERE 子句
        where_parts = [f"id IN ({placeholders})"]
        if _where_conditions:
            where_parts.extend(_where_conditions)
        where_clause = " AND ".join(where_parts)

        with self._get_connection() as conn:
            cursor = conn.execute(
                f"UPDATE messages SET {', '.join(fields)} WHERE {where_clause}",
                values + msg_ids,
            )
            affected = cursor.rowcount

        # 🔧 通知 WebUI：检测关键字段变化，主动广播给前端增量更新
        if affected > 0:
            broadcast_fields = {k: v for k, v in kwargs.items() if k in self._BROADCAST_FIELDS}
            if broadcast_fields:
                for mid in msg_ids:
                    self._notify_webui("updated", mid, broadcast_fields)

        return affected

    def update_message(self, msg_id: int, **kwargs: Any) -> bool:
        """
        更新消息

        Args:
            msg_id: 消息ID
            **kwargs: 要更新的字段

        Returns:
            是否成功
        """
        return self.update_messages([msg_id], **kwargs) > 0

    def update_message_status(self, msg_id: int, new_status: str) -> bool:
        """
        直接更新消息状态（不检查状态转换）

        Args:
            msg_id: 消息ID
            new_status: 新状态

        Returns:
            是否成功
        """
        return self._update_status(msg_id, new_status)

    def mark_read(self, msg_id: int) -> bool:
        """标记消息为已读（只能从 unread 转换）"""
        current = self._get_current_status(msg_id)
        if current != "unread":
            return False
        self._update_status(msg_id, "read", {"read_at": datetime.now().isoformat()})

    def get_reply_count(self, msg_id: int) -> int:
        """
        获取消息的回复数

        Args:
            msg_id: 消息ID

        Returns:
            回复数量
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "SELECT COUNT(*) as count FROM messages WHERE reply_to = %s", (msg_id,)
                )
                row = cursor.fetchone()
                return row["count"] if row else 0
        except Exception as e:
            import logging

            logger = logging.getLogger(__name__)
            logger.error(f"获取回复数失败: {e}")
            return 0

    def get_by_receiver_and_status(
        self, receiver: str, status: str, limit: int = 1
    ) -> list[dict[str, Any]]:
        """
        按接收者和状态获取消息

        Args:
            receiver: 接收者
            status: 状态
            limit: 返回数量

        Returns:
            消息列表
        """
        return self.get_by_status(status=status, receiver=receiver, limit=limit)
        return True

    def mark_replied(self, msg_id: int) -> bool:
        """
        标记消息为已回复

        用于简单消息（无需事务处理）回复后的状态更新。

        Args:
            msg_id: 消息ID

        Returns:
            是否成功
        """
        current = self._get_current_status(msg_id)
        # 允许从 processing 或 interrupted 状态转换（防止竞态条件）
        if current not in ("processing", "interrupted"):
            return False
        self._update_status(msg_id, "replied")
        return True

    def mark_failed(self, msg_id: int, reason: str | None = None) -> bool:
        """
        标记消息为失败

        用于事务执行失败后的状态更新。

        Args:
            msg_id: 消息ID
            reason: 失败原因

        Returns:
            是否成功
        """
        kwargs = {"status": "failed"}
        if reason:
            kwargs["error"] = reason
        return self.update_message(msg_id, **kwargs)

    def mark_read_batch(self, msg_ids: list[int]) -> int:
        """
        批量标记消息为已读

        Args:
            msg_ids: 消息ID列表

        Returns:
            成功标记的数量
        """
        if not msg_ids:
            return 0

        now = datetime.now().isoformat()
        return self.update_messages(
            msg_ids,
            _where_conditions=["status = 'unread'"],
            status="read", read_at=now,
        )

    def mark_processing(
        self,
        msg_id: int,
        auto_reply: bool = True,
        force: bool = False,
    ) -> tuple[bool, str | None, str | None]:
        """
        标记消息为处理中

        Args:
            msg_id: 消息ID
            auto_reply: 是否自动回复消息报告状态
            force: 是否强制转换（忽略状态限制）

        Returns:
            (success, warning, status_msg)
        """
        current = self._get_current_status(msg_id)
        if current is None:
            return (False, None, None)

        if not force and current not in ("unread", "read", "done"):
            return (False, None, None)

        warning = None
        if current == "done":
            warning = "⚠️ 警告：该消息已完成处理，现在重新标记为处理中"
        elif current == "processing":
            warning = "⚠️ 注意：消息已经是处理中状态"

        # 【修复心跳超时bug】标记为processing时立即设置心跳时间
        # 避免事务进程启动期间被心跳监控误判超时
        self._update_status(
            msg_id,
            "processing",
            {"heartbeat_at": datetime.now().isoformat(), "locked_at": datetime.now().isoformat()},
        )

        # 生成状态消息
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status_msg = f"📢 消息 #{msg_id} 状态更新\n\n🔵 开始处理\n\n时间: {timestamp}"

        if auto_reply:
            self._auto_reply_status(msg_id, "🔵 开始处理")

        return (True, warning, status_msg)

    def get_processing_messages_full(self) -> list[dict[str, Any]]:
        """
        获取所有处理中消息的完整信息

        Returns:
            消息列表，包含 id, process_id, locked_by, locked_at, heartbeat_at
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, process_id, locked_by, locked_at, heartbeat_at
                FROM messages WHERE status = 'processing'
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def count_processing_messages(self) -> int:
        """
        统计处理中消息的数量

        Returns:
            处理中消息数量
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """SELECT COUNT(*) as count FROM messages WHERE status = 'processing'"""
            )
            row = cursor.fetchone()
            return row["count"] if row else 0

    def get_message_by_status_and_sender(
        self, status: str, sender: str, limit: int = 1
    ) -> dict[str, Any] | None:
        """
        获取指定状态且发送者不是sender的消息

        Args:
            status: 消息状态
            sender: 排除的发送者
            limit: 返回数量限制

        Returns:
            消息字典
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT id FROM messages
                WHERE status = %s AND sender != %s
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (status, sender, limit),
            )
            rows = cursor.fetchall()
            return dict(rows[0]) if rows else None

    def update_messages_status_by_ids(
        self, msg_ids: list[int], status: str, heartbeat_at: str | None = None
    ) -> int:
        """
        批量更新消息状态

        Args:
            msg_ids: 消息ID列表
            status: 新状态
            heartbeat_at: 心跳时间（可选）

        Returns:
            更新的行数
        """
        kwargs = {"status": status}
        if heartbeat_at:
            kwargs["heartbeat_at"] = heartbeat_at
        return self.update_messages(msg_ids, **kwargs)

    def update_messages_batch_by_status(self, msg_ids: list[int], status: str) -> int:
        """
        批量更新消息状态（简单版本）

        Args:
            msg_ids: 消息ID列表
            status: 新状态

        Returns:
            更新的行数
        """
        return self.update_messages(msg_ids, status=status)

    def mark_done(
        self,
        msg_id: int,
        force: bool = False,
        auto_reply: bool = True,
    ) -> tuple[bool, str | None, str | None]:
        """
        标记消息为完成

        Args:
            msg_id: 消息ID
            force: 是否强制完成（忽略验收检查）
            auto_reply: 是否自动回复消息报告状态

        Returns:
            (success, error, status_msg)
        """
        current = self._get_current_status(msg_id)
        if current != "processing":
            return (False, "消息状态不是processing", None)

        self._update_status(msg_id, "done")

        # 生成状态消息
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status_msg = f"📢 消息 #{msg_id} 状态更新\n\n✅ 处理完成\n\n时间: {timestamp}"

        if auto_reply:
            self._auto_reply_status(msg_id, "✅ 处理完成")

        return (True, None, status_msg)

    def mark_archived(self, msg_id: int) -> bool:
        """
        归档消息（需求 §3.2.6-3.2.8）

        归档是独立标记，不改变消息状态。
        归档的消息：
        - 可被 --search 搜索到
        - 可被 --finished 列出
        - 可通过 ID 获取
        - 不能被 msg --all/--inbox 等方式列出
        """

        if msg_id == 1:
            # 不归档忠实助手消息
            return False
        return self.update_message(msg_id, is_archived=1, archived_at=datetime.now().isoformat())

    def unarchive_message(self, msg_id: int) -> bool:
        """
        取消归档消息

        Args:
            msg_id: 消息ID

        Returns:
            是否成功
        """
        return self.update_message(msg_id, is_archived=0)

    def archive_old_messages(self, days: int = 7) -> int:
        """
        归档超过指定天数的暂停/中断/失败消息（需求 §3.2.8）

        判断依据：archived_at（归档状态最后变更时间）超过指定天数。
        对于从未变更过归档状态的消息（archived_at IS NULL），也视为可归档。

        Args:
            days: 天数阈值

        Returns:
            归档的消息数量
        """
        # 计算截止日期
        cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()

        # 先查询符合条件的消息ID
        rows = self._execute_query_all(
            """
            SELECT id FROM messages
            WHERE is_archived = 0
            AND (
                status IN ('interrupted', 'paused', 'failed')
                OR is_paused = 1
            )
            AND (archived_at IS NULL OR archived_at < %s)
            """,
            (cutoff_date,),
        )
        msg_ids = [row["id"] for row in rows]

        if not msg_ids:
            return 0

        # 通过 update_messages 统一更新
        archived_count = self.update_messages(
            msg_ids,
            is_archived=1,
            archived_at=datetime.now().isoformat(),
        )

        if archived_count > 0:
            logger.info(f"已归档 {archived_count} 条过期消息（超过{days}天）")

        return archived_count

    def _auto_reply_status(self, msg_id: int, status_msg: str) -> bool:
        """自动回复消息状态变更"""
        try:
            original = self.get_message(msg_id)
            if not original:
                return False

            if original.get("receiver") != AGENT_NAME:
                return False

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            original_content = original.get("content", "")

            content = f"""📢 消息 #{msg_id} 状态更新

{status_msg}

时间: {timestamp}

---
**原消息内容:**
{original_content}"""

            with self._get_connection() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO messages (timestamp, content, sender, receiver, status, reply_to)
                    VALUES (%s, %s, %s, %s, 'unread', %s)
                    """,
                    (timestamp, content, AGENT_NAME, get_master_name(), msg_id),
                )
                return cursor.rowcount > 0
        except Exception as e:
            import traceback

            logger.error(
                f"❌ _auto_reply_status() - database.py:1023: 异常 - {e}\n{traceback.format_exc()}"
            )
            return False

    # ==================== 新字段管理 ====================

    def set_timeout(self, msg_id: int, timeout: int) -> bool:
        """设置超时时间"""
        return self.update_message(msg_id, timeout=timeout)

    def set_caller(self, msg_id: int, caller: str) -> bool:
        """设置调用来源"""
        return self.update_message(msg_id, caller=caller)

    def set_cancelled(self, msg_id: int, cancelled: bool = True) -> bool:
        """设置取消标记，同时更新状态"""
        if cancelled:
            return self.update_message(msg_id, is_cancelled=1, status="cancelled")
        else:
            return self.update_message(msg_id, is_cancelled=0)

    def mark_paused(self, msg_id: int, reason: str = "Message processing paused") -> bool:
        """
        标记消息为暂停状态

        当消息处理被管理模块主动暂停时，标记消息为暂停状态。
        与中断不同，暂停的消息可以恢复。

        Args:
            msg_id: 消息 ID
            reason: 暂停原因

        Returns:
            是否成功
        """
        return self.update_message(msg_id, is_paused=1, status="paused")

    def mark_interrupted(self, msg_id: int, reason: str = "Request interrupted by user") -> bool:
        """
        标记消息为中断状态

        当消息处理进程被中断（所有相关进程 ID 失效）时，标记消息为中断状态。

        Args:
            msg_id: 消息 ID
            reason: 中断原因

        Returns:
            是否成功
        """
        return self.update_message(
            msg_id, status="interrupted", is_cancelled=1
        )

    def mark_interrupted_batch(
        self, msg_ids: list[int], reason: str = "Request interrupted by user"
    ) -> int:
        """
        批量标记消息为中断状态

        Args:
            msg_ids: 消息 ID 列表
            reason: 中断原因

        Returns:
            更新的行数
        """
        if not msg_ids:
            return 0

        return self.update_messages(
            msg_ids, status="interrupted", is_cancelled=1
        )

    def check_and_mark_stale_processing_messages(self) -> list[int]:
        """
        检查并标记失效的处理中消息

        检测所有状态为 processing 的消息，如果关联的进程已失效（进程不存在），
        则标记为中断状态。

        Returns:
            被标记为中断的消息 ID 列表
        """
        from concurrent.futures import ThreadPoolExecutor

        # 获取所有处理中的消息
        processing_msgs = self.get_all_processing_messages()

        # 收集需要检查的消息
        msgs_to_check = []
        for msg in processing_msgs:
            process_id = msg.get("process_id")
            locked_by = msg.get("locked_by")
            # 如果没有关联任何进程，跳过（可能是等待锁的消息）
            if process_id or locked_by:
                msgs_to_check.append((msg["id"], process_id or locked_by))

        if not msgs_to_check:
            return []

        # 并行检查进程是否存在
        def check_one(item: tuple[int, int]) -> int | None:
            msg_id, pid = item
            if self._check_process_exists(pid):
                return None
            return msg_id

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(check_one, msgs_to_check))

        # 收集需要中断的消息ID
        interrupted_ids = [r for r in results if r is not None]

        if not interrupted_ids:
            return []

        # 批量标记为中断（优化：N次update → 1次批量update）
        self.mark_interrupted_batch(interrupted_ids)

        return interrupted_ids

    def get_all_processing_messages(self) -> list[dict[str, Any]]:
        """
        获取所有处理中的消息

        Returns:
            处理中的消息列表
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM messages
                WHERE status = 'processing'
                ORDER BY locked_at ASC
                """,
            )
            return [dict(row) for row in cursor.fetchall()]

    def _check_process_exists(self, os_pid: int) -> bool:
        """
        检查进程是否存在

        Args:
            os_pid: 操作系统进程 ID

        Returns:
            进程是否存在
        """

        # None 或无效值表示没有关联进程，不应该被检查
        if os_pid is None:
            return True  # 没有关联进程，不标记为中断

        # 使用跨平台封装检查进程是否存在
        from peng.common.platform import process_exists

        return process_exists(os_pid)

    def check_and_mark_paused_messages(self) -> list[int]:
        """
        检查并标记被暂停的消息

        检测所有被标记为 is_paused=1 的消息，确保它们在 bin/msg 中正确显示为已暂停。

        Returns:
            被暂停的消息 ID 列表
        """
        paused_ids = []

        # 获取所有被暂停的消息
        paused_msgs = self.get_paused_messages()

        for msg in paused_msgs:
            msg_id = msg["id"]
            paused_ids.append(msg_id)

        return paused_ids

    def get_paused_messages(self) -> list[dict[str, Any]]:
        """
        获取所有被暂停的消息

        Returns:
            被暂停的消息列表
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM messages
                WHERE is_paused = 1
                ORDER BY locked_at ASC
                """,
            )
            return [dict(row) for row in cursor.fetchall()]

    # ==================== 回复链管理 ====================

    @retry_on_db_error(max_retries=3, delay=0.1)
    def get_reply_chain(self, msg_id: int) -> list[dict[str, Any]]:
        """
        获取消息的完整回复链

        Args:
            msg_id: 消息ID（可以是链首或链中任意消息）

        Returns:
            回复链上的所有消息列表
        """
        with self._get_connection() as conn:
            # 先获取当前消息
            msg_cursor = conn.execute("SELECT * FROM messages WHERE id = %s", (msg_id,))
            msg = msg_cursor.fetchone()
            if msg is None:
                logger.warning(
                    f"[MessageDatabase] get_reply_chain: 消息 #{msg_id} 不存在，返回空链。"
                )
                return []
            msg = dict(msg)

            # 找到链首ID
            head_id = msg.get("chain_head_id")
            if not head_id:
                if msg.get("is_chain_head"):
                    head_id = msg["id"]
                elif msg.get("reply_to"):
                    # 递归查找链首（在同一个连接中）
                    current_id = msg["reply_to"]
                    for _ in range(999):  # 防止无限循环
                        query = "SELECT * FROM messages WHERE id = %s"
                        parent_cursor = conn.execute(query, (current_id,))
                        parent = parent_cursor.fetchone()
                        if parent is None:
                            break
                        parent = dict(parent)  # 转换为字典
                        if parent["is_chain_head"]:
                            head_id = parent["id"]
                            break
                        if parent["chain_head_id"]:
                            head_id = parent["chain_head_id"]
                            break
                        if parent["reply_to"]:
                            current_id = parent["reply_to"]
                        else:
                            head_id = parent["id"]
                            break
                    else:
                        head_id = msg["id"]
                else:
                    head_id = msg["id"]

            if not head_id:
                head_id = msg["id"]

            # 获取整个回复链
            chain_cursor = conn.execute(
                """
                SELECT DISTINCT * FROM messages
                WHERE id = %s OR chain_head_id = %s
                ORDER BY timestamp ASC
                """,
                (head_id, head_id),
            )
            return [dict(row) for row in chain_cursor.fetchall()]

    def get_chain_head(self, msg_id: int) -> dict[str, Any] | None:
        """
        获取链首消息

        Args:
            msg_id: 消息ID

        Returns:
            链首消息字典
        """
        try:
            query = "SELECT * FROM messages WHERE id = %s"
            msg = self._execute_query_one(query, (msg_id,))
            chain_head_id = msg.get("chain_head_id")
            if chain_head_id:
                return self.get_message(chain_head_id)
            if msg and msg.get("reply_to"):
                # 如果是回复消息，找到链首
                while msg and msg.get("reply_to"):
                    msg = self._execute_query_one(
                        "SELECT * FROM messages WHERE id = %s", (msg["reply_to"],)
                    )
                self.update_message(msg_id, {"chain_head_id": msg["id"]})
            return msg
        except Exception as e:
            logger.exception(f"get_chain_head {msg_id} failed: {e}\n{traceback.format_stack()}")
            return None

    def get_descendants(self, msg_id: int) -> list[dict[str, Any]]:
        """
        获取消息的所有子孙消息（递归获取所有回复）

        Args:
            msg_id: 消息ID

        Returns:
            子孙消息列表（包含所有层级的回复）
        """
        descendants = []

        # 使用递归CTE查询所有子孙消息
        with get_cursor() as cursor:
            cursor.execute(
                """
                WITH RECURSIVE descendants AS (
                    -- 基础查询：获取直接子节点
                    SELECT * FROM messages WHERE reply_to = %s
                    UNION
                    -- 递归查询：获取子节点的子孙
                    SELECT m.* FROM messages m
                    INNER JOIN descendants d ON m.reply_to = d.id
                )
                SELECT * FROM descendants ORDER BY timestamp ASC
            """,
                (msg_id,),
            )
            rows = cursor.fetchall()
            descendants = rows_to_list(rows)

        return descendants

    def build_context_for_message(self, msg_id: int) -> str | None:
        """
        给定任意消息ID，解析其所属消息链的链首，构建并返回完整恢复链上下文。
        同时更新链首消息的 chain_context 字段。

        统一入口：bin/msg context 和 bin/task msg 子命令均调用此方法。

        Args:
            msg_id: 消息ID（可以是链首或链中任意消息）

        Returns:
            上下文文本，如果消息不存在或无法构建返回 None
        """
        msg = self.get_message(msg_id)
        if not msg:
            return None

        # 确定链首消息ID
        if msg.get("is_chain_head") == 1 or msg.get("reply_to") is None:
            chain_head_id = msg_id
        else:
            chain_head_id = msg.get("chain_head_id")
            if not chain_head_id:
                head = self.get_chain_head(msg_id)
                if head:
                    chain_head_id = head["id"]

        if not chain_head_id:
            return None

        return self.build_context_history(chain_head_id)

    def build_context_history(self, chain_head_id: int, colored=True) -> str | None:
        """
        构建消息树的历史记录格式（按时间先后，最新消息在最后）

        用于存储到数据库 chain_context 字段，以及 LLM 处理

        格式示例：
            2025-03-23 10:00:00 #1  master	这是第一条消息
            2025-03-23 10:01:00 #2 → #1 鹏🐣	这是回复消息

        Args:
            chain_head_id: 链首消息ID

        Returns:
            上下文文本，如果链不存在返回 None
        """
        chain = self.get_reply_chain(chain_head_id)
        if not chain:
            return None

        # ANSI 紫色（用于高亮 reply_to ID）
        PURPLE = "\033[95m" if colored else ""
        RESET = "\033[0m" if colored else ""

        lines = []
        for msg in chain:
            timestamp = msg.get("timestamp", "")
            msg_id = msg["id"]
            sender = msg.get("sender", "")
            content = msg.get("content", "")

            # 不截断内容，保留完整消息（用于数据库存储和LLM处理）
            # 构建 reply_to 标识
            reply_to = msg.get("reply_to")
            if reply_to:
                reply_to_part = f"{PURPLE}→ #{reply_to}{RESET} "
            else:
                reply_to_part = " "

            line = f"{timestamp} #{msg_id}{reply_to_part}{sender}\t{content}"
            lines.append(line)
        context = "\n".join(lines)
        if context is None:
            return False

        self.update_message(chain_head_id, chain_context=context)
        return context

    def build_context_tree(self, chain_head_id: int, colored=True) -> str | None:
        """
        构建消息树的树状结构格式（用于 bin/msg trees 和 tree 子命令显示）

        格式示例：
            2025-03-23 10:00:00 #1  master	这是第一条消息
              2025-03-23 10:01:00 #2 → #1 鹏🐣	这是回复消息
                2025-03-23 10:02:00 #3 → #2 master	继续回复

        Args:
            chain_head_id: 链首消息ID

        Returns:
            树状结构文本，如果链不存在返回 None
        """
        chain = self.get_reply_chain(chain_head_id)
        if not chain:
            return None

        # ANSI 紫色（用于高亮 reply_to ID）
        PURPLE = "\033[95m" if colored else ""
        RESET = "\033[0m" if colored else ""

        # 构建消息映射和子节点映射
        msg_map = {msg["id"]: msg for msg in chain}
        children_map: dict[int, list[int]] = {}

        # 找到根消息和构建子节点映射
        root_id = None
        for msg in chain:
            msg_id = msg["id"]
            reply_to = msg.get("reply_to")
            if not reply_to:
                root_id = msg_id
            else:
                if reply_to not in children_map:
                    children_map[reply_to] = []
                children_map[reply_to].append(msg_id)

        if not root_id:
            return None

        # 递归构建树状文本
        def build_node(msg_id: int, depth: int) -> list[str]:
            msg = msg_map.get(msg_id)
            if not msg:
                return []

            indent = "  " * depth
            timestamp = msg.get("timestamp", "")
            sender = msg.get("sender", "")
            content = msg.get("content", "")

            # 截断消息为单行（用于终端显示）
            # 将换行符替换为空格，保留在同一行显示
            content = content.replace("\n", " ").replace("\r", " ")
            # 如果仍然过长，截断到100字符
            if len(content) > 100:
                content = content[:97] + "..."

            # 构建 reply_to 标识
            reply_to = msg.get("reply_to")
            if reply_to:
                reply_to_part = f"{PURPLE}→ #{reply_to}{RESET} "
            else:
                reply_to_part = " "

            line = f"{indent}{timestamp} #{msg_id}{reply_to_part}{sender}	{content}"
            lines = [line]

            # 递归处理子节点（按时间排序）
            children = children_map.get(msg_id, [])
            children_sorted = sorted(children, key=lambda x: msg_map[x].get("timestamp", ""))
            for child_id in children_sorted:
                lines.extend(build_node(child_id, depth + 1))

            return lines

        lines = build_node(root_id, 0)
        return "\n".join(lines)

    def get_chain_head_messages(
        self,
        caller: str | None = None,
        status: str | None = None,
        include_archived: bool = False,
        receiver: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        获取链首消息列表

        Args:
            caller: 调用者过滤
            status: 状态过滤
            include_archived: 是否包含已归档消息
            receiver: 接收者过滤（可选）

        Returns:
            链首消息列表
        """
        conditions = ["is_chain_head = 1"]
        if not include_archived:
            conditions.append("is_archived = 0")
        # 始终过滤掉待删除消息
        conditions.append("(pending_delete = 0 OR pending_delete IS NULL)")
        params: list[Any] = []

        if caller:
            conditions.append("caller = %s")
            params.append(caller)

        if status:
            conditions.append("status = %s")
            params.append(status)

        if receiver:
            conditions.append("receiver = %s")
            params.append(receiver)

        where_clause = " AND ".join(conditions)

        with self._get_connection() as conn:
            cursor = conn.execute(
                f"SELECT * FROM messages WHERE {where_clause} ORDER BY timestamp DESC",
                params,
            )
            return [dict(row) for row in cursor.fetchall()]

    # ==================== 并发控制 ====================

    def lock_message(self, msg_id: int, process_id: int) -> bool:
        """
        原子性地锁定消息

        通过 update_messages 的 _where_conditions 实现原子性：
        - 只有当消息状态为 unread 且未被锁定时才能锁定
        - 锁定后更新状态为 processing

        Args:
            msg_id: 消息ID
            process_id: 进程ID

        Returns:
            True 表示锁定成功，False 表示已被其他进程锁定
        """
        now = datetime.now().isoformat()

        return self.update_messages(
            [msg_id],
            _where_conditions=["status = 'unread'", "locked_by IS NULL"],
            status="processing",
            locked_by=process_id,
            locked_at=now,
            heartbeat_at=now,
            process_id=process_id,
        ) > 0

    def release_lock(self, msg_id: int, process_id: int) -> bool:
        """
        释放消息锁定（清空锁定和心跳信息）

        Args:
            msg_id: 消息ID
            process_id: 进程ID

        Returns:
            True 表示释放成功，False 表示权限不足或消息不存在
        """
        return self.update_messages(
            [msg_id],
            _where_conditions=[f"locked_by = {int(process_id)}"],
            locked_by=None,
            locked_at=None,
            heartbeat_at=None,
            process_id=None,
        ) > 0

    def unlock_message(self, msg_id: int, process_id: int) -> bool:
        """
        解锁消息（release_lock的别名）

        Args:
            msg_id: 消息ID
            process_id: 进程ID

        Returns:
            True 表示解锁成功，False 表示权限不足或消息不存在
        """
        return self.release_lock(msg_id, process_id)

    def lock_unread_message(self, process_id: int | None = None) -> int | None:
        """
        锁定一条未读消息（原子性）

        Args:
            process_id: 进程ID，None 表示服务进程锁定但未分配执行进程

        Returns:
            锁定的消息ID，如果没有可用的消息返回 None
        """
        # 注意：不使用默认值 os.getpid()，因为：
        # 1. 服务进程调用时，应该传入 None（表示未分配执行进程）
        # 2. 事务执行进程调用时，应该传入真实的进程 PID
        # 3. 避免 process_id 混乱导致取消事务时杀死错误的进程

        now = datetime.now().isoformat()

        # 先获取要锁定的消息ID
        row = self._execute_query_one(
            """
            SELECT id FROM messages
            WHERE status = 'unread' AND locked_by IS NULL
            ORDER BY timestamp ASC
            LIMIT 1
            """
        )
        if row is None:
            return None

        msg_id = row["id"]

        # 通过 update_messages 原子性锁定（利用 _where_conditions）
        updated = self.update_messages(
            [msg_id],
            _where_conditions=["locked_by IS NULL"],
            status="processing",
            locked_by=process_id,
            locked_at=now,
            heartbeat_at=now,
            process_id=process_id,
        )

        if updated == 0:
            # 并发冲突，消息已被其他进程锁定
            return None

        return msg_id

    def update_heartbeat(self, msg_id: int, process_id: int) -> bool:
        """
        更新心跳时间戳

        Args:
            msg_id: 消息ID
            process_id: 进程ID

        Returns:
            True 表示更新成功，False 表示权限不足或消息不存在
        """
        return self.update_messages(
            [msg_id],
            _where_conditions=[f"locked_by = {int(process_id)}"],
            heartbeat_at=datetime.now().isoformat(),
        ) > 0

    def get_heartbeat(self) -> dict[str, Any] | None:
        """
        获取最近的心跳记录

        Returns:
            包含 updated_at 字段的心跳记录字典，如果没有心跳记录则返回 None
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT heartbeat_at as updated_at
                FROM messages
                WHERE heartbeat_at IS NOT NULL
                ORDER BY heartbeat_at DESC
                LIMIT 1
                """
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def release_expired_locks(self, timeout_seconds: int = 300) -> int:
        """
        释放过期的锁

        Args:
            timeout_seconds: 超时时间（秒），默认5分钟

        Returns:
            释放的锁数量
        """
        expired_time = (datetime.now() - timedelta(seconds=timeout_seconds)).isoformat()

        # 先查询符合条件的消息ID
        rows = self._execute_query_all(
            """
            SELECT id FROM messages
            WHERE status = 'processing' AND heartbeat_at < %s
            """,
            (expired_time,),
        )
        msg_ids = [row["id"] for row in rows]

        if not msg_ids:
            return 0

        # 通过 update_messages 统一更新
        return self.update_messages(
            msg_ids,
            status="unread",
            locked_by=None,
            locked_at=None,
            heartbeat_at=None,
            process_id=None,
        )

    def get_timed_out_messages(self, timeout_seconds: int = 300) -> list[dict[str, Any]]:
        """
        获取心跳超时的消息列表

        Args:
            timeout_seconds: 超时时间（秒），默认 5 分钟

        Returns:
            超时消息列表
        """
        expired_time = (datetime.now() - timedelta(seconds=timeout_seconds)).isoformat()

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM messages
                WHERE status = 'processing'
                    AND heartbeat_at < %s
                ORDER BY heartbeat_at ASC
                """,
                (expired_time,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_stale_locked_messages(self, timeout_seconds: int = 300) -> list[dict[str, Any]]:
        """
        获取锁超时的消息列表（locked_at 超时）

        Args:
            timeout_seconds: 超时时间（秒），默认 5 分钟

        Returns:
            锁超时消息列表
        """
        expired_time = (datetime.now() - timedelta(seconds=timeout_seconds)).isoformat()

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM messages
                WHERE status = 'processing'
                    AND locked_at < %s
                ORDER BY locked_at ASC
                """,
                (expired_time,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_timeout_based_messages(self) -> list[dict[str, Any]]:
        """
        根据消息自己的 timeout 字段获取超时消息列表

        判断逻辑：status = 'processing' AND timeout > 0 AND locked_at + timeout < now

        Returns:
            超时消息列表
        """
        now = datetime.now().isoformat()

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM messages
                WHERE status = 'processing'
                    AND timeout > 0
                    AND (locked_at + (timeout || ' seconds')::interval) < %s
                ORDER BY locked_at ASC
                """,
                (now,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_by_process_id(self, process_id: int) -> list[dict[str, Any]]:
        """
        根据进程 ID 获取关联的消息

        Args:
            process_id: 进程 ID

        Returns:
            关联的消息列表
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM messages
                WHERE process_id = %s
                ORDER BY timestamp DESC
                """,
                (process_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    # ==================== 维护操作 ====================

    def delete_archived(self) -> int:
        """删除已归档消息（is_archived = 1）"""
        with self._get_connection() as conn:
            cursor = conn.execute("DELETE FROM messages WHERE is_archived = 1")
            return cursor.rowcount

    def cleanup_old_records(self, days: int = 30) -> int:
        """
        清理旧的已完成消息

        Args:
            days: 保留天数

        Returns:
            清理的记录数
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                DELETE FROM messages
                WHERE status IN ('done', 'archived')
                AND archived_at < datetime('now', %s)
                """,
                (f"-{days} days",),
            )
            return cursor.rowcount

    # ==================== 消息折叠功能 ====================

    def set_collapsed(self, msg_id: int, collapsed: bool) -> bool:
        """
        设置消息的折叠状态

        Args:
            msg_id: 消息ID
            collapsed: 是否折叠

        Returns:
            是否成功
        """
        try:
            return self.update_message(msg_id, collapsed=1 if collapsed else 0)
        except Exception as e:
            logger.error(f"设置消息折叠状态失败: {e}\n{traceback.format_exc()}")
            return False

    def get_collapsed(self, msg_id: int) -> bool:
        """
        获取消息的折叠状态

        Args:
            msg_id: 消息ID

        Returns:
            是否折叠（默认False）
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "SELECT collapsed FROM messages WHERE id = %s",
                    (msg_id,),
                )
                row = cursor.fetchone()
                if row:
                    return row["collapsed"] == 1
            return False
        except Exception as e:
            logger.error(f"获取消息折叠状态失败: {e}\n{traceback.format_exc()}")
            return False

    # ==================== 消息控制管理 ====================

    def get_message_controls(self, processed: bool = False) -> list[dict[str, Any]]:
        """
        获取消息控制项

        Args:
            processed: 是否只获取已处理的控制项，默认 False（获取未处理的）

        Returns:
            消息控制项列表
        """
        try:
            with self._get_connection() as conn:
                if processed:
                    cursor = conn.execute(
                        """
                        SELECT id, message_id, action, created_at
                        FROM message_controls
                        WHERE processed = 1
                        ORDER BY created_at ASC
                        """
                    )
                else:
                    cursor = conn.execute(
                        """
                        SELECT id, message_id, action, created_at
                        FROM message_controls
                        WHERE processed = 0
                        ORDER BY created_at ASC
                        """
                    )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"获取消息控制项失败: {e}\n{traceback.format_exc()}")
            return []

    def mark_message_control_processed(self, control_id: int) -> bool:
        """
        标记消息控制为已处理

        Args:
            control_id: 控制项 ID

        Returns:
            是否成功
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "UPDATE message_controls SET processed = 1 WHERE id = %s",
                    (control_id,),
                )
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"标记消息控制为已处理失败: {e}\n{traceback.format_exc()}")
            return False

    # ==================== 批量查询 ====================

    def get_by_ids(self, message_ids: list[int]) -> list[dict[str, Any]]:
        """
        批量获取多个消息

        Args:
            message_ids: 消息 ID 列表

        Returns:
            消息字典列表
        """
        if not message_ids:
            return []

        placeholders = ",".join(["%s"] * len(message_ids))
        with self._get_connection() as conn:
            cursor = conn.execute(
                f"SELECT * FROM messages WHERE id IN ({placeholders})",
                message_ids,
            )
            return [dict(row) for row in cursor.fetchall()]

    @retry_on_db_error(max_retries=3, delay=0.1)
    def get_reply_chains_batch(self, chain_head_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        """
        批量获取多个链首消息的回复链（一次SQL完成）

        替代循环调用 get_reply_chain，解决 N+1 查询问题。

        Args:
            chain_head_ids: 链首消息ID列表

        Returns:
            dict[chain_head_id, 该链的所有消息列表]（按 timestamp 升序）
        """
        if not chain_head_ids:
            return {}

        placeholders = ",".join(["%s"] * len(chain_head_ids))
        # 合并参数：chain_head_ids 出现两次（一次匹配 chain_head_id 列，一次匹配 id 列）
        params = chain_head_ids + chain_head_ids

        with self._get_connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT DISTINCT * FROM messages
                WHERE chain_head_id IN ({placeholders}) OR id IN ({placeholders})
                ORDER BY timestamp ASC
                """,
                params,
            )
            rows = [dict(row) for row in cursor.fetchall()]

        # 按 chain_head_id 分组
        result: dict[int, list[dict[str, Any]]] = {}
        for msg in rows:
            # 确定该消息属于哪条链
            head_id = msg.get("chain_head_id")
            if head_id is None or head_id == 0:
                # chain_head 自身，chain_head_id 可能为空
                head_id = msg["id"]

            if head_id not in result:
                result[head_id] = []
            result[head_id].append(msg)

        return result

    # ==================== 消息移动 ====================

    def move_message(
        self, msg_id: int, target_id: int | None = None, only_message: bool = False
    ) -> dict[str, Any]:
        """
        移动消息到新的父节点（支持拖拽到空隙变成树根）

        移动消息会更新：
        1. 消息#msg_id的 reply_to 字段改为target_id，如果#msg_id 为根消息，清除其 chain_context
        2. 消息#msg_id及其所有子孙的 chain_head_id 改为 target_id 的 chain_head_id
        3. 消息#target_id  的 chain_head要build_context_histor

        Args:
            msg_id: 要移动的消息ID
            target_id: 目标父消息ID，None表示拖拽到空隙变成树根
            only_message: 是否仅移动消息不同步移动关联事务（内部使用）

        Returns:
            {"success": bool, "message": str, "msg_id": int}
        """
        try:
            logger.info(
                f"[move_message] ===== 开始移动消息#{msg_id} -> 目标父消息#{target_id} ====="
            )
            if msg_id == target_id:
                return {
                    "success": False,
                    "message": f"消息#{msg_id}不能作为自己的分支。",
                    "msg_id": msg_id,
                }
            # 系统消息保护：禁止移动系统消息（#1鹏🐣忠实助手）
            if msg_id == 1:
                return {
                    "success": False,
                    "message": f"系统消息#{msg_id}受保护，禁止移动。",
                    "msg_id": msg_id,
                }
            # 1. 获取要移动的消息
            msg = self.get_message(msg_id)
            if not msg:
                logger.error(f"[move_message] 消息#{msg_id}不存在")
                return {"success": False, "message": f"消息#{msg_id}不存在", "msg_id": msg_id}

            # 2. 验证目标父消息存在性（如果不是变成树根）
            descendants = []
            descendant_ids = set()
            original_reply_to = msg.get("reply_to")

            if (
                target_id and target_id != 1
            ):  # 拖拽到鹏🐣-忠实助手（自动生成的第一条消息#1）要变成树根
                if target_id == original_reply_to:
                    return {
                        "success": False,
                        "message": f"目标消息#{target_id}已经是消息#{msg_id}的父消息。",
                        "msg_id": msg_id,
                    }

                target_parent = self.get_message(target_id)
                if not target_parent:
                    logger.error(f"[move_message] 目标父消息#{target_id}不存在")
                    return {
                        "success": False,
                        "message": f"目标父消息#{target_id}不存在",
                        "msg_id": msg_id,
                    }
                descendants = self.get_descendants(msg_id)
                descendant_ids = {d["id"] for d in descendants}
                if target_id in descendant_ids:
                    # 目标为子孙时，只交换content
                    content = msg.get("content")
                    self.update_message(msg_id, content=target_parent.get("content"))
                    self.update_message(target_id, content=content)
                    # Bug #6修复：交换content后，重建链首的chain_context
                    original_chain_head_id = msg.get("chain_head_id")
                    if original_chain_head_id:
                        self.build_context_history(original_chain_head_id)
                    return {
                        "success": True,
                        "message": f"目标消息#{target_id}为消息#{msg_id}的子孙消息，执行交换文本操作。",
                        "msg_id": msg_id,
                    }
                chain_head_id = target_parent.get("chain_head_id")
                # Bug #1/#2修复：记录原链首ID，用于后续更新原消息树
                original_chain_head_id = msg.get("chain_head_id")
                if not original_reply_to:
                    # 根消息变为子消息
                    if not only_message:
                        # 根消息变子消息时，同步移动关联的事务树
                        # 查找根消息关联的根事务，移动到目标消息关联的事务树下
                        from peng.task.database import TaskDatabase

                        task_db = TaskDatabase()
                        root_tasks = task_db.get_root_tasks_by_message(msg_id)
                        if root_tasks:
                            # 查找目标消息链首关联的根事务
                            target_root_tasks = task_db.get_root_tasks_by_message(chain_head_id)
                            for root_task in root_tasks:
                                # 确定目标父事务ID
                                if target_root_tasks:
                                    # 目标消息有根事务，移动到其下
                                    target_parent_task_id = target_root_tasks[0]["id"]
                                else:
                                    # 目标消息没有根事务，放弃移动
                                    return {
                                        "success": False,
                                        "message": f"目标消息#{target_id}尚未开始处理，不能移动该目标下。",
                                        "msg_id": msg_id,
                                    }

                                task_db.move_task(
                                    task_id=root_task["id"],
                                    target_id=target_parent_task_id,
                                    only_task=True,
                                )
                    self.update_message(
                        msg_id,
                        status="done",
                        reply_to=target_id,
                        chain_head_id=chain_head_id,
                        is_chain_head=0,
                        chain_context="",
                    )
                else:
                    self.update_message(
                        msg_id, reply_to=target_id, chain_head_id=chain_head_id, chain_context=""
                    )
                # 更新分支消息chain_head_id和chain_context
                for id in descendant_ids:
                    self.update_message(id, chain_head_id=chain_head_id, chain_context="")
                self.build_context_history(target_id)
                # Bug #1修复：重建原消息树根的chain_context（移走了消息后需要更新）
                if original_chain_head_id and original_chain_head_id != chain_head_id:
                    self.build_context_history(original_chain_head_id)
                return {
                    "success": True,
                    "message": f"消息#{msg_id}变为消息#{target_id}的分支消息。",
                    "msg_id": msg_id,
                }
            elif not original_reply_to:
                # 根消息拖拽到空隙或者鹏🐣-忠实助手，无效操作
                return {"success": False, "message": f"消息#{msg_id}已是根消息。", "msg_id": msg_id}
            else:
                # 拖拽到空隙或者鹏🐣-忠实助手：变成树根
                descendants = self.get_descendants(msg_id)
                descendant_ids = {d["id"] for d in descendants}
                self.update_message(
                    msg_id, is_chain_head=1, reply_to=0, chain_head_id=msg_id, status="unread"
                )
                for id in descendant_ids:
                    self.update_message(id, chain_head_id=msg_id)
                logger.info(
                    f"[move_message] 拖拽到空隙，消息#{msg_id}变成树根，有{len(descendant_ids)}个子孙消息: {descendant_ids}"
                )
                self.build_context_history(msg_id)
                return {"success": True, "message": f"消息#{msg_id}变为根消息。", "msg_id": msg_id}
        except Exception as e:
            logger.error(f"[move_message] ❌ 移动消息失败: {e} {traceback.format_exc()}")
            return {"success": False, "message": f"移动消息失败: {e}", "msg_id": msg_id}

#!/usr/bin/env python3
"""
PostgreSQL数据库初始化脚本

在PostgreSQL中创建所需的表结构
"""

import logging

import psycopg2
from psycopg2.extras import RealDictCursor

from peng.db.connection import get_config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _migrate_add_column(conn, table: str, column: str, col_type: str) -> None:
    """安全地为已有表添加列（如果不存在）"""
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s AND column_name = %s",
            (table, column),
        )
        if not cursor.fetchone():
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            conn.commit()
            logger.info(f"✓ 迁移：为 {table} 添加 {column} 字段")
    except Exception as e:
        conn.rollback()
        logger.warning(f"迁移 {table}.{column} 跳过: {e}")


def create_messages_table(conn):
    """创建消息表"""
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            timestamp TEXT NOT NULL,
            sender TEXT NOT NULL,
            receiver TEXT NOT NULL,
            content TEXT NOT NULL,
            title TEXT DEFAULT '',
            workdir TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'unread',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            read_at TEXT,
            archived_at TEXT,
            locked_by INTEGER,
            locked_at TEXT,
            heartbeat_at TEXT,
            process_id INTEGER,
            reply_to INTEGER,
            chain_head_id INTEGER,
            chain_context TEXT,
            error TEXT,
            caller TEXT,
            caller_session_id TEXT,
            is_chain_head INTEGER DEFAULT 0,
            is_cancelled INTEGER DEFAULT 0,
            timeout INTEGER DEFAULT 0,
            is_paused INTEGER DEFAULT 0,
            collapsed INTEGER DEFAULT 0,
            is_archived INTEGER DEFAULT 0,
            pending_delete INTEGER DEFAULT 0,
            nest TEXT DEFAULT '',
            platform_msg_id TEXT DEFAULT '',
            feishu_chat_id TEXT DEFAULT ''
        )
    """)
    logger.info("✓ 创建messages表")

    # 创建索引
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status)",
        "CREATE INDEX IF NOT EXISTS idx_messages_reply_to ON messages(reply_to)",
        "CREATE INDEX IF NOT EXISTS idx_messages_chain_head ON messages(chain_head_id)",
        "CREATE INDEX IF NOT EXISTS idx_messages_caller ON messages(caller)",
        "CREATE INDEX IF NOT EXISTS idx_messages_caller_session ON messages(caller_session_id)",
        "CREATE INDEX IF NOT EXISTS idx_messages_heartbeat ON messages(heartbeat_at)",
        "CREATE INDEX IF NOT EXISTS idx_messages_locked ON messages(locked_by)",
        "CREATE INDEX IF NOT EXISTS idx_messages_pending_delete ON messages(pending_delete)",
        "CREATE INDEX IF NOT EXISTS idx_messages_receiver ON messages(receiver)",
        "CREATE INDEX IF NOT EXISTS idx_messages_platform_msg_id ON messages(platform_msg_id)",
    ]

    for index_sql in indexes:
        cursor.execute(index_sql)

    logger.info("✓ 创建messages表索引")

    # 迁移：添加 platform_msg_id 和 feishu_chat_id 字段（兼容已有数据库）
    _migrate_add_column(conn, "messages", "platform_msg_id", "TEXT DEFAULT ''")
    _migrate_add_column(conn, "messages", "feishu_chat_id", "TEXT DEFAULT ''")

    conn.commit()


def create_tasks_table(conn):
    """创建事务表"""
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            content TEXT,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            progress INTEGER DEFAULT 0,
            priority INTEGER DEFAULT 2,
            task_type TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            deadline TEXT,
            timer INTEGER DEFAULT 0,
            last_restart_at TEXT,
            message_id INTEGER,
            parent_task_id INTEGER,
            root_id INTEGER,
            depth INTEGER DEFAULT 0,
            path TEXT DEFAULT '',
            caller TEXT,
            caller_session_id TEXT,
            worktree_path TEXT,
            branch_name TEXT,
            worktree_locked INTEGER DEFAULT 0,
            process_id INTEGER,
            error TEXT,
            result TEXT,
            timeout INTEGER DEFAULT 0,
            timeout_started_at TEXT,
            heartbeat_at TEXT,
            heartbeat_timeout INTEGER DEFAULT NULL,  -- NULL表示使用 config monitor.default_heartbeat_timeout
            expertise TEXT,
            analysis_report TEXT,
            subtask_config TEXT,
            tool_calls_log TEXT DEFAULT '',
            tool_results_log TEXT DEFAULT '',
            metadata TEXT DEFAULT '',
            is_archived INTEGER DEFAULT 0,
            archived_at TEXT,
            current_step_index INTEGER DEFAULT 0,
            current_step_name TEXT,
            collapsed INTEGER DEFAULT 0,
            pause_context TEXT,
            prompt TEXT DEFAULT '',
            workdir TEXT DEFAULT '',
            small_model INTEGER DEFAULT 0,
            info TEXT DEFAULT '',
            is_complex INTEGER DEFAULT 0,
            context TEXT DEFAULT '',
            creator TEXT DEFAULT '',
            provider TEXT DEFAULT '',
            nest TEXT DEFAULT '',
            checklist TEXT DEFAULT '',
            report TEXT DEFAULT '',
            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE SET NULL,
            FOREIGN KEY (parent_task_id) REFERENCES tasks(id) ON DELETE SET NULL
        )
    """)
    logger.info("✓ 创建tasks表")

    # 创建索引
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_message ON tasks(message_id)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_root ON tasks(root_id)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_depth ON tasks(depth)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_is_complex ON tasks(is_complex)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_caller ON tasks(caller)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks(deadline)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_heartbeat ON tasks(heartbeat_at)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_worktree ON tasks(worktree_path)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_branch_name ON tasks(branch_name)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_archived_at ON tasks(archived_at)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_timer ON tasks(timer)",
    ]

    for index_sql in indexes:
        cursor.execute(index_sql)

    logger.info("✓ 创建tasks表索引")

    conn.commit()


def create_task_steps_table(conn):
    """创建事务步骤表"""
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_steps (
            id SERIAL PRIMARY KEY,
            task_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            sort_order INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            error TEXT,
            result TEXT,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
    """)
    logger.info("✓ 创建task_steps表")

    # 创建索引
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_steps_task ON task_steps(task_id)")

    conn.commit()


def create_task_dependencies_table(conn):
    """创建事务依赖关系表"""
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_dependencies (
            id SERIAL PRIMARY KEY,
            task_id INTEGER NOT NULL,
            depends_on_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (depends_on_id) REFERENCES tasks(id) ON DELETE CASCADE,
            UNIQUE(task_id, depends_on_id)
        )
    """)
    logger.info("✓ 创建task_dependencies表")

    # 创建索引
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_deps_task ON task_dependencies(task_id)",
        "CREATE INDEX IF NOT EXISTS idx_deps_depends ON task_dependencies(depends_on_id)",
    ]

    for index_sql in indexes:
        cursor.execute(index_sql)

    logger.info("✓ 创建task_dependencies表索引")

    conn.commit()


def create_task_controls_table(conn):
    """创建事务控制表"""
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_controls (
            id SERIAL PRIMARY KEY,
            task_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            processed INTEGER DEFAULT 0,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
    """)
    logger.info("✓ 创建task_controls表")

    # 创建索引
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_task_controls_task ON task_controls(task_id)",
        "CREATE INDEX IF NOT EXISTS idx_task_controls_processed ON task_controls(processed)",
    ]

    for index_sql in indexes:
        cursor.execute(index_sql)

    logger.info("✓ 创建task_controls表索引")

    conn.commit()


def create_message_controls_table(conn):
    """创建消息控制表"""
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS message_controls (
            id SERIAL PRIMARY KEY,
            message_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            processed INTEGER DEFAULT 0,
            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
        )
    """)
    logger.info("✓ 创建message_controls表")

    # 创建索引
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_message_controls_message ON message_controls(message_id)",
        "CREATE INDEX IF NOT EXISTS idx_message_controls_processed ON message_controls(processed)",
    ]

    for index_sql in indexes:
        cursor.execute(index_sql)

    logger.info("✓ 创建message_controls表索引")

    conn.commit()


def create_audit_logs_table(conn):
    """创建审计日志表"""
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id SERIAL PRIMARY KEY,
            task_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            monitor_input TEXT,
            decision_logic TEXT,
            action TEXT,
            action_args TEXT DEFAULT '',
            result TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
    """)
    logger.info("✓ 创建audit_logs表")

    # 创建索引
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_task ON audit_logs(task_id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_timestamp ON audit_logs(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON audit_logs(action)",
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_result ON audit_logs(result)",
    ]

    for index_sql in indexes:
        cursor.execute(index_sql)

    logger.info("✓ 创建audit_logs表索引")

    conn.commit()


def fix_tasks_table_schema(conn):
    """修复tasks表的缺失字段"""
    cursor = conn.cursor()

    try:
        # 检查 archived_at 字段是否存在
        cursor.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'tasks' AND column_name = 'archived_at'
        """)
        if not cursor.fetchone():
            logger.info("添加 archived_at 字段到 tasks 表...")
            cursor.execute("ALTER TABLE tasks ADD COLUMN archived_at TEXT")
            logger.info("✓ archived_at 字段添加成功")
        else:
            logger.info("✓ archived_at 字段已存在")

        # 检查 is_archived 字段是否存在
        cursor.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'tasks' AND column_name = 'is_archived'
        """)
        if not cursor.fetchone():
            logger.info("添加 is_archived 字段到 tasks 表...")
            cursor.execute("ALTER TABLE tasks ADD COLUMN is_archived INTEGER DEFAULT 0")
            logger.info("✓ is_archived 字段添加成功")
        else:
            logger.info("✓ is_archived 字段已存在")

        # 检查 last_restart_at 字段是否存在
        cursor.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'tasks' AND column_name = 'last_restart_at'
        """)
        if not cursor.fetchone():
            logger.info("添加 last_restart_at 字段到 tasks 表...")
            cursor.execute("ALTER TABLE tasks ADD COLUMN last_restart_at TEXT")
            logger.info("✓ last_restart_at 字段添加成功")
        else:
            logger.info("✓ last_restart_at 字段已存在")

        # 创建 archived_at 索引
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_archived_at ON tasks(archived_at)")
            logger.info("✓ archived_at 索引创建成功")
        except Exception as e:
            logger.warning(f"⚠ archived_at 索引创建失败（可能已存在）: {e}")

        # 创建 timer 索引
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_timer ON tasks(timer)")
            logger.info("✓ timer 索引创建成功")
        except Exception as e:
            logger.warning(f"⚠ timer 索引创建失败（可能已存在）: {e}")

        conn.commit()
    except Exception as e:
        logger.error(f"❌ 修复tasks表结构失败: {e}")
        conn.rollback()
        raise


def create_schema_version_tables(conn):
    """创建schema版本表"""
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS message_schema_version (
            version INTEGER PRIMARY KEY
        )
    """)
    logger.info("✓ 创建message_schema_version表")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_schema_version (
            version INTEGER PRIMARY KEY
        )
    """)
    logger.info("✓ 创建task_schema_version表")

    conn.commit()


def main():
    """主函数"""
    config = get_config()

    logger.info(f"连接到PostgreSQL: {config.host}:{config.port}/{config.database}")

    conn = psycopg2.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=config.database,
    )

    try:
        # 创建所有表
        create_messages_table(conn)
        create_tasks_table(conn)
        create_task_steps_table(conn)
        create_task_dependencies_table(conn)
        create_task_controls_table(conn)
        create_message_controls_table(conn)
        create_audit_logs_table(conn)
        create_schema_version_tables(conn)

        # 修复 tasks 表结构（处理已存在的表）
        fix_tasks_table_schema(conn)

        logger.info("✓ 所有表创建成功")

    except Exception as e:
        logger.error(f"❌ 创建表失败: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()

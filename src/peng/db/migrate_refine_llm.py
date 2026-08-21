#!/usr/bin/env python3
"""
数据库迁移脚本 - 添加refine_llm事务所需字段

添加字段：
- is_complex: 是否为复杂事务（布尔值，默认false）
- workdir: 工作目录（用于隔离环境）
"""

import logging
import sys

try:
    import psycopg2

    from peng.db.connection import get_config, get_connection
except ImportError:
    print("警告：PostgreSQL模块未安装，跳过数据库迁移")
    sys.exit(0)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def migrate_database():
    """执行数据库迁移"""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()

            # 添加is_complex字段
            try:
                cursor.execute("""
                    ALTER TABLE tasks
                    ADD COLUMN IF NOT EXISTS is_complex INTEGER DEFAULT 0
                """)
                logger.info("✓ 添加is_complex字段")
            except Exception as e:
                logger.warning(f"添加is_complex字段失败: {e}")

            # 添加workdir字段
            try:
                cursor.execute("""
                    ALTER TABLE tasks
                    ADD COLUMN IF NOT EXISTS workdir TEXT DEFAULT ''
                """)
                logger.info("✓ 添加workdir字段")
            except Exception as e:
                logger.warning(f"添加workdir字段失败: {e}")

            # 添加is_complex索引
            try:
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_tasks_is_complex ON tasks(is_complex)
                """)
                logger.info("✓ 创建idx_tasks_is_complex索引")
            except Exception as e:
                logger.warning(f"创建idx_tasks_is_complex索引失败: {e}")

            conn.commit()
            logger.info("✓ 数据库迁移完成")

    except Exception as e:
        logger.error(f"数据库迁移失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    migrate_database()

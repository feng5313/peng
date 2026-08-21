#!/usr/bin/env python3
"""
数据库初始化脚本

提供统一的数据库初始化接口,包括:
- 数据库连接测试
- 表结构创建
- 迁移脚本执行
- 数据完整性验证
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor

from peng.db.connection import DBConfig

logger = logging.getLogger(__name__)


class DatabaseInitializer:
    """
    数据库初始化器

    负责:
    - 测试数据库连接
    - 创建表结构
    - 执行迁移脚本
    - 验证数据完整性
    """

    def __init__(self, config_path: Optional[str] = None):
        """
        初始化数据库初始化器

        Args:
            config_path: 配置文件路径,默认为None(使用默认配置)
        """
        # 直接使用PostgreSQL配置
        if config_path:
            self.config = DBConfig(config_path)
        else:
            # 获取PostgreSQL配置(需要从配置文件中读取postgresql部分)
            try:
                import json
                from pathlib import Path

                # 尝试读取配置文件
                config_file = Path("config/db.json")
                if config_file.exists():
                    with open(config_file) as f:
                        data = json.load(f)
                    pg_config = data.get("postgresql", {})
                    self.config = type("Config", (), pg_config)()
                else:
                    # 使用默认值
                    self.config = type(
                        "Config",
                        (),
                        {
                            "host": "localhost",
                            "port": 5432,
                            "user": "peng",
                            "password": "peng_password",
                            "database": "peng_db",
                            "pool_size": 10,
                            "max_overflow": 20,
                        },
                    )()
            except Exception as e:
                logger.warning(f"读取配置文件失败: {e}, 使用默认配置")
                self.config = type(
                    "Config",
                    (),
                    {
                        "host": "localhost",
                        "port": 5432,
                        "user": "peng",
                        "password": "peng_password",
                        "database": "peng_db",
                        "pool_size": 10,
                        "max_overflow": 20,
                    },
                )()

        self.migrations_dir = Path(__file__).parent / "migrations"

    def test_connection(self) -> tuple[bool, str]:
        """
        测试数据库连接

        Returns:
            (是否成功, 消息)
        """
        try:
            conn = psycopg2.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database=self.config.database,
            )
            cursor = conn.cursor()
            cursor.execute("SELECT version() as version")
            row = cursor.fetchone()
            version = row["version"] if row else "unknown"
            cursor.close()
            conn.close()

            logger.info(
                f"✓ 数据库连接成功: {self.config.host}:{self.config.port}/{self.config.database}"
            )
            return True, f"连接成功\nPostgreSQL版本: {version}"
        except Exception as e:
            logger.error(f"❌ 数据库连接失败: {e}")
            return False, f"连接失败: {e}"

    def create_tables(self) -> tuple[bool, str]:
        """
        创建所有表结构

        Returns:
            (是否成功, 消息)
        """
        try:
            conn = psycopg2.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database=self.config.database,
            )

            try:
                # 导入表创建函数
                from peng.db.init_postgres_db import (
                    create_message_controls_table, create_messages_table,
                    create_schema_version_tables, create_task_controls_table,
                    create_task_dependencies_table, create_task_steps_table,
                    create_tasks_table)

                # 创建所有核心表
                create_messages_table(conn)
                create_tasks_table(conn)
                create_task_steps_table(conn)
                create_task_dependencies_table(conn)
                create_task_controls_table(conn)
                create_message_controls_table(conn)
                create_schema_version_tables(conn)

                logger.info("✓ 所有核心表创建成功")

                # 检查是否有迁移脚本
                self._execute_migration_scripts(conn)

                conn.commit()
                return True, "所有表创建成功"

            except Exception as e:
                conn.rollback()
                raise
            finally:
                conn.close()

        except Exception as e:
            logger.error(f"❌ 创建表失败: {e}")
            return False, f"创建表失败: {e}"

    def _execute_migration_scripts(self, conn) -> None:
        """
        执行迁移脚本

        Args:
            conn: 数据库连接
        """
        if not self.migrations_dir.exists():
            logger.warning(f"迁移脚本目录不存在: {self.migrations_dir}")
            return

        # 获取所有SQL迁移脚本
        sql_files = sorted(self.migrations_dir.glob("*.sql"))

        if not sql_files:
            logger.info("没有找到迁移脚本")
            return

        logger.info(f"找到 {len(sql_files)} 个迁移脚本")

        # 执行每个迁移脚本
        for sql_file in sql_files:
            try:
                with open(sql_file, "r", encoding="utf-8") as f:
                    sql_content = f.read()

                cursor = conn.cursor()
                cursor.execute(sql_content)
                conn.commit()
                cursor.close()

                logger.info(f"✓ 执行迁移脚本: {sql_file.name}")

            except Exception as e:
                logger.warning(f"⚠ 执行迁移脚本失败 {sql_file.name}: {e}")
                conn.rollback()

    def verify_tables(self) -> tuple[bool, str]:
        """
        验证表结构完整性

        Returns:
            (是否成功, 消息)
        """
        try:
            conn = psycopg2.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database=self.config.database,
            )

            try:
                cursor = conn.cursor(cursor_factory=RealDictCursor)

                # 检查核心表是否存在
                required_tables = [
                    "messages",
                    "tasks",
                    "task_steps",
                    "task_dependencies",
                    "task_controls",
                    "message_controls",
                    "message_schema_version",
                    "task_schema_version",
                ]

                cursor.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_name = ANY(%s)
                """,
                    (required_tables,),
                )

                existing_tables = {row["table_name"] for row in cursor.fetchall()}

                missing_tables = set(required_tables) - existing_tables

                if missing_tables:
                    cursor.close()
                    conn.close()
                    return False, f"缺少表: {', '.join(missing_tables)}"

                # 检查students表(可选)
                cursor.execute("""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_name = 'students'
                """)

                if cursor.fetchone():
                    logger.info("✓ students表存在")
                else:
                    logger.info("ℹ students表不存在(可选)")

                cursor.close()
                conn.close()

                logger.info("✓ 表结构验证通过")
                return True, "表结构验证通过"

            except Exception as e:
                conn.close()
                raise

        except Exception as e:
            logger.error(f"❌ 验证表结构失败: {e}")
            return False, f"验证表结构失败: {e}"

    def get_table_info(self) -> dict:
        """
        获取表信息

        Returns:
            表信息字典
        """
        try:
            conn = psycopg2.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database=self.config.database,
            )

            try:
                cursor = conn.cursor(cursor_factory=RealDictCursor)

                # 获取所有表
                cursor.execute("""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                    ORDER BY table_name
                """)

                tables = []
                for row in cursor.fetchall():
                    table_name = row["table_name"]

                    # 获取表的记录数
                    cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
                    row = cursor.fetchone()
                    count = row["count"] if row else 0

                    tables.append(
                        {
                            "name": table_name,
                            "count": count,
                        }
                    )

                cursor.close()
                conn.close()

                return {
                    "database": f"{self.config.host}:{self.config.port}/{self.config.database}",
                    "tables": tables,
                    "total_tables": len(tables),
                }

            except Exception as e:
                conn.close()
                raise

        except Exception as e:
            logger.error(f"❌ 获取表信息失败: {e}")
            return {
                "error": str(e),
                "database": f"{self.config.host}:{self.config.port}/{self.config.database}",
                "tables": [],
                "total_tables": 0,
            }

    def initialize(self, force: bool = False) -> tuple[bool, str]:
        """
        完整初始化数据库

        Args:
            force: 是否强制重新创建表

        Returns:
            (是否成功, 消息)
        """
        messages = []

        print("=======================----------------------")
        # 1. 测试连接
        success, msg = self.test_connection()
        if not success:
            return False, f"数据库连接失败: {msg}"
        messages.append(msg)

        # 2. 验证表结构
        success, msg = self.verify_tables()
        if success and not force:
            messages.append(f"表已存在,跳过创建: {msg}")
            return True, "\n".join(messages)

        # 3. 创建表
        if force or not success:
            success, msg = self.create_tables()
            if not success:
                return False, f"创建表失败: {msg}"
            messages.append(msg)

        # 4. 再次验证
        success, msg = self.verify_tables()
        if not success:
            return False, f"表结构验证失败: {msg}"
        messages.append(msg)

        return True, "\n".join(messages)


def main():
    """主函数: 命令行入口"""
    import argparse

    parser = argparse.ArgumentParser(description="数据库初始化工具")
    parser.add_argument(
        "--config",
        "-c",
        help="配置文件路径",
        default=None,
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="强制重新创建表",
    )
    parser.add_argument(
        "--test",
        "-t",
        action="store_true",
        help="仅测试数据库连接",
    )
    parser.add_argument(
        "--verify",
        "-v",
        action="store_true",
        help="仅验证表结构",
    )
    parser.add_argument(
        "--info",
        "-i",
        action="store_true",
        help="显示表信息",
    )

    args = parser.parse_args()

    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    # 创建初始化器
    initializer = DatabaseInitializer(args.config)

    if args.test:
        # 仅测试连接
        success, msg = initializer.test_connection()
        print(msg)
        return 0 if success else 1

    if args.verify:
        # 仅验证表结构
        success, msg = initializer.verify_tables()
        print(msg)
        return 0 if success else 1

    if args.info:
        # 显示表信息
        info = initializer.get_table_info()
        print(f"数据库: {info['database']}")
        print(f"表数量: {info['total_tables']}")
        print("\n表列表:")
        for table in info.get("tables", []):
            print(f"  - {table['name']}: {table['count']} 条记录")
        return 0

    # 完整初始化
    print("=" * 60)
    print("数据库初始化")
    print("=" * 60)

    success, msg = initializer.initialize(force=args.force)

    print("=" * 60)
    if success:
        print("✓ 数据库初始化成功")
        print("=" * 60)
        print(msg)
        return 0
    else:
        print("❌ 数据库初始化失败")
        print("=" * 60)
        print(msg)
        return 1


if __name__ == "__main__":
    exit(main())

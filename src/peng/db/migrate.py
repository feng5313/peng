#!/usr/bin/env python3
"""
数据库迁移工具

提供数据库迁移脚本管理功能,包括:
- 执行迁移脚本
- 回滚迁移
- 查看迁移历史
- 验证迁移状态
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)


class MigrationTool:
    """
    数据库迁移工具

    负责:
    - 执行迁移脚本
    - 记录迁移历史
    - 支持迁移回滚
    - 查看迁移状态
    """

    def __init__(self, config_path: Optional[str] = None):
        """
        初始化迁移工具

        Args:
            config_path: 配置文件路径,默认为None(使用默认配置)
        """
        # 直接使用PostgreSQL配置
        if config_path:
            import json
            with open(config_path) as f:
                data = json.load(f)
            pg_config = data.get("postgresql", {})
            self.config = type('Config', (), pg_config)()
        else:
            # 尝试读取配置文件
            try:
                import json
                from pathlib import Path
                
                config_file = Path("config/db.json")
                if config_file.exists():
                    with open(config_file) as f:
                        data = json.load(f)
                    pg_config = data.get("postgresql", {})
                    self.config = type('Config', (), pg_config)()
                else:
                    # 使用默认值
                    self.config = type('Config', (), {
                        "host": "localhost",
                        "port": 5432,
                        "user": "peng",
                        "password": "peng_password",
                        "database": "peng_db",
                        "pool_size": 10,
                        "max_overflow": 20
                    })()
            except Exception as e:
                logger.warning(f"读取配置文件失败: {e}, 使用默认配置")
                self.config = type('Config', (), {
                    "host": "localhost",
                    "port": 5432,
                    "user": "peng",
                    "password": "peng_password",
                    "database": "peng_db",
                    "pool_size": 10,
                    "max_overflow": 20
                })()

        self.migrations_dir = Path(__file__).parent / "migrations"
        self._ensure_migrations_table()

    def _get_connection(self):
        """获取数据库连接"""
        return psycopg2.connect(
            host=self.config.host,
            port=self.config.port,
            user=self.config.user,
            password=self.config.password,
            database=self.config.database,
        )

    def _ensure_migrations_table(self):
        """确保迁移历史表存在"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL UNIQUE,
                    version VARCHAR(50) NOT NULL,
                    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    checksum TEXT,
                    description TEXT
                )
            """)

            # 创建索引
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_migrations_name
                ON schema_migrations(name)
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_migrations_version
                ON schema_migrations(version)
            """)

            conn.commit()
            cursor.close()
            conn.close()

            logger.info("✓ 迁移历史表已就绪")

        except Exception as e:
            conn.rollback()
            conn.close()
            logger.error(f"❌ 创建迁移历史表失败: {e}")
            raise

    def _get_applied_migrations(self) -> set[str]:
        """获取已应用的迁移列表"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("SELECT name FROM schema_migrations ORDER BY applied_at")
            rows = cursor.fetchall()
            cursor.close()
            conn.close()
            return {row["name"] for row in rows}
        except Exception as e:
            conn.close()
            logger.error(f"❌ 获取已应用迁移失败: {e}")
            raise

    def _record_migration(self, name: str, version: str, checksum: str, description: str) -> None:
        """记录迁移"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO schema_migrations (name, version, checksum, description)
                VALUES (%s, %s, %s, %s)
            """, (name, version, checksum, description))
            conn.commit()
            cursor.close()
            conn.close()
            logger.info(f"✓ 记录迁移: {name}")
        except Exception as e:
            conn.rollback()
            conn.close()
            raise

    def _remove_migration(self, name: str) -> None:
        """移除迁移记录"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM schema_migrations WHERE name = %s", (name,))
            conn.commit()
            cursor.close()
            conn.close()
            logger.info(f"✓ 移除迁移记录: {name}")
        except Exception as e:
            conn.rollback()
            conn.close()
            raise

    def _calculate_checksum(self, content: str) -> str:
        """计算SQL内容的校验和"""
        import hashlib
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    def get_pending_migrations(self) -> list[dict]:
        """
        获取待执行的迁移

        Returns:
            待执行的迁移列表
        """
        if not self.migrations_dir.exists():
            logger.warning(f"迁移脚本目录不存在: {self.migrations_dir}")
            return []

        applied = self._get_applied_migrations()
        pending = []

        for sql_file in sorted(self.migrations_dir.glob("*.sql")):
            if sql_file.name not in applied:
                with open(sql_file, "r", encoding="utf-8") as f:
                    content = f.read()

                # 提取版本号和描述
                version = sql_file.stem
                description = self._extract_description(content)

                pending.append({
                    "name": sql_file.name,
                    "version": version,
                    "description": description,
                    "path": sql_file,
                })

        return pending

    def _extract_description(self, sql_content: str) -> str:
        """从SQL内容中提取描述"""
        lines = sql_content.split("\n")
        for line in lines:
            if line.strip().startswith("-- 描述:"):
                return line.split(":", 1)[1].strip()
        return "无描述"

    def migrate(self, name: Optional[str] = None) -> tuple[bool, str]:
        """
        执行迁移

        Args:
            name: 迁移文件名,None表示执行所有待执行的迁移

        Returns:
            (是否成功, 消息)
        """
        pending = self.get_pending_migrations()

        if not pending:
            return True, "没有待执行的迁移"

        if name:
            # 执行指定迁移
            migration = next((m for m in pending if m["name"] == name), None)
            if not migration:
                return False, f"找不到迁移: {name}"
            migrations = [migration]
        else:
            # 执行所有待执行的迁移
            migrations = pending

        messages = []
        success_count = 0
        failed_migrations = []

        for migration in migrations:
            try:
                # 读取SQL内容
                with open(migration["path"], "r", encoding="utf-8") as f:
                    sql_content = f.read()

                # 计算校验和
                checksum = self._calculate_checksum(sql_content)

                # 执行SQL
                conn = self._get_connection()
                try:
                    cursor = conn.cursor()
                    cursor.execute(sql_content)
                    conn.commit()
                    cursor.close()
                    conn.close()

                    # 记录迁移
                    self._record_migration(
                        migration["name"],
                        migration["version"],
                        checksum,
                        migration["description"],
                    )

                    success_count += 1
                    messages.append(f"✓ {migration['name']}: {migration['description']}")
                    logger.info(f"✓ 执行迁移成功: {migration['name']}")

                except Exception as e:
                    conn.rollback()
                    conn.close()
                    raise

            except Exception as e:
                failed_migrations.append(migration["name"])
                messages.append(f"❌ {migration['name']}: {e}")
                logger.error(f"❌ 执行迁移失败 {migration['name']}: {e}")

        # 汇总结果
        if failed_migrations:
            result_msg = (
                f"迁移完成: {success_count}/{len(migrations)} 成功\n"
                f"失败的迁移: {', '.join(failed_migrations)}\n"
                f"\n详细信息:\n" + "\n".join(messages)
            )
            return False, result_msg
        else:
            result_msg = (
                f"所有迁移执行成功: {success_count}/{len(migrations)}\n"
                f"\n详细信息:\n" + "\n".join(messages)
            )
            return True, result_msg

    def rollback(self, name: str) -> tuple[bool, str]:
        """
        回滚迁移(删除迁移记录)

        注意: 这不会自动执行回滚SQL,仅删除迁移记录,
        回滚SQL需要手动执行

        Args:
            name: 迁移文件名

        Returns:
            (是否成功, 消息)
        """
        try:
            # 检查迁移是否存在
            applied = self._get_applied_migrations()
            if name not in applied:
                return False, f"迁移未应用: {name}"

            # 删除迁移记录
            self._remove_migration(name)

            return True, f"迁移记录已删除: {name}\n注意: 这不会自动回滚SQL变更"

        except Exception as e:
            logger.error(f"❌ 回滚迁移失败 {name}: {e}")
            return False, f"回滚迁移失败: {e}"

    def status(self) -> dict:
        """
        获取迁移状态

        Returns:
            迁移状态字典
        """
        try:
            conn = self._get_connection()
            try:
                cursor = conn.cursor(cursor_factory=RealDictCursor)

                # 获取已应用的迁移
                cursor.execute("""
                    SELECT * FROM schema_migrations
                    ORDER BY applied_at DESC
                """)
                applied = list(cursor.fetchall())

                # 获取待执行的迁移
                pending = self.get_pending_migrations()

                cursor.close()
                conn.close()

                return {
                    "applied_count": len(applied),
                    "pending_count": len(pending),
                    "applied_migrations": applied,
                    "pending_migrations": pending,
                }

            except Exception as e:
                conn.close()
                raise

        except Exception as e:
            logger.error(f"❌ 获取迁移状态失败: {e}")
            return {
                "error": str(e),
                "applied_count": 0,
                "pending_count": 0,
                "applied_migrations": [],
                "pending_migrations": [],
            }


def main():
    """主函数: 命令行入口"""
    import argparse

    parser = argparse.ArgumentParser(description="数据库迁移工具")
    parser.add_argument(
        "--config",
        "-c",
        help="配置文件路径",
        default=None,
    )
    parser.add_argument(
        "--name",
        "-n",
        help="指定迁移文件名",
        default=None,
    )
    parser.add_argument(
        "--status",
        "-s",
        action="store_true",
        help="查看迁移状态",
    )
    parser.add_argument(
        "--rollback",
        "-r",
        action="store_true",
        help="回滚迁移(删除迁移记录)",
    )

    args = parser.parse_args()

    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    # 创建迁移工具
    tool = MigrationTool(args.config)

    if args.status:
        # 查看迁移状态
        print("=" * 60)
        print("迁移状态")
        print("=" * 60)

        status = tool.status()

        if "error" in status:
            print(f"错误: {status['error']}")
            return 1

        print(f"已应用迁移: {status['applied_count']}")
        print(f"待执行迁移: {status['pending_count']}")
        print()

        if status["applied_migrations"]:
            print("已应用的迁移:")
            for m in status["applied_migrations"]:
                print(f"  - {m['name']} ({m['version']}) - {m.get('description', 'N/A')}")
                print(f"    应用时间: {m['applied_at']}")
            print()

        if status["pending_migrations"]:
            print("待执行的迁移:")
            for m in status["pending_migrations"]:
                print(f"  - {m['name']} ({m['version']}) - {m['description']}")
            print()

        print("=" * 60)
        return 0

    if args.rollback:
        # 回滚迁移
        if not args.name:
            print("错误: 回滚需要指定迁移文件名 (--name)")
            return 1

        print("=" * 60)
        print("回滚迁移")
        print("=" * 60)

        success, msg = tool.rollback(args.name)
        print(msg)

        print("=" * 60)
        return 0 if success else 1

    # 执行迁移
    print("=" * 60)
    print("执行迁移")
    print("=" * 60)

    success, msg = tool.migrate(args.name)

    print("=" * 60)
    if success:
        print("✓ 迁移执行成功")
        print("=" * 60)
        print(msg)
        return 0
    else:
        print("❌ 迁移执行失败")
        print("=" * 60)
        print(msg)
        return 1


if __name__ == "__main__":
    exit(main())

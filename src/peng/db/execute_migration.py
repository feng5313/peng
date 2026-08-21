#!/usr/bin/env python3
"""
执行数据库迁移脚本

用于手动执行数据库迁移，确保tasks表包含归档字段。
"""
import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from peng.db.connection import get_connection
from peng.common import jsonp as json
from datetime import datetime


def check_fields_exist():
    """检查归档字段是否存在"""
    with get_connection() as conn:
        try:
            cursor = conn.cursor()

            # 检查字段是否存在
            cursor.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'tasks' AND column_name IN ('is_archived', 'archived_at')
            """)
            columns = [row[0] for row in cursor.fetchall()]

            cursor.close()

            return 'is_archived' in columns and 'archived_at' in columns
        except Exception as e:
            print(f"❌ 检查字段失败: {e}")
            return False


def add_archived_fields():
    """添加归档字段到tasks表"""
    with get_connection() as conn:
        try:
            cursor = conn.cursor()

            # 添加 is_archived 字段
            print("添加 is_archived 字段...")
            try:
                cursor.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS is_archived INTEGER DEFAULT 0")
                print("✓ is_archived 字段添加成功")
            except Exception as e:
                print(f"⚠ is_archived 字段添加失败（可能已存在）: {e}")

            # 添加 archived_at 字段
            print("添加 archived_at 字段...")
            try:
                cursor.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS archived_at TEXT")
                print("✓ archived_at 字段添加成功")
            except Exception as e:
                print(f"⚠ archived_at 字段添加失败（可能已存在）: {e}")

            # 创建索引
            print("创建索引...")
            try:
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_is_archived ON tasks(is_archived)")
                print("✓ idx_tasks_is_archived 索引创建成功")
            except Exception as e:
                print(f"⚠ 索引创建失败（可能已存在）: {e}")

            try:
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_archived_at ON tasks(archived_at)")
                print("✓ idx_tasks_archived_at 索引创建成功")
            except Exception as e:
                print(f"⚠ 索引创建失败（可能已存在）: {e}")

            conn.commit()
            cursor.close()

            print("\n✅ 数据库迁移完成")
            return True

        except Exception as e:
            conn.rollback()
            print(f"\n❌ 数据库迁移失败: {e}")
            import traceback
            traceback.print_exc()
            return False


def main():
    """主函数"""
    print("=" * 60)
    print("数据库迁移：添加事务归档字段")
    print("=" * 60)
    print()

    # 检查字段是否存在
    print("检查归档字段是否存在...")
    if check_fields_exist():
        print("✓ 归档字段已存在，无需迁移")
        return 0

    print("归档字段不存在，开始迁移...")
    print()

    # 添加字段
    if add_archived_fields():
        return 0
    else:
        return 1


if __name__ == "__main__":
    sys.exit(main())

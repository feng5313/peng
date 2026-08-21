#!/usr/bin/env python3
"""
为其他项目创建PostgreSQL数据库

用法:
    python create_project_db.py <database_name>
"""

import sys
from pathlib import Path

# 设置项目路径
_SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_DIR / "src"))

import psycopg2

from peng.db.connection import get_config


def create_database(db_name: str):
    """
    为项目创建数据库和用户

    Args:
        db_name: 数据库名称
    """
    config = get_config()

    print(f"创建数据库: {db_name}")
    print(f"连接到: {config.host}:{config.port}")
    print("=" * 60)

    try:
        # 连接到postgres数据库（默认数据库）
        conn = psycopg2.connect(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            database="postgres",
        )

        conn.autocommit = True
        cursor = conn.cursor()

        # 检查数据库是否已存在
        cursor.execute(
            """
            SELECT 1 FROM pg_database WHERE datname = %s
        """,
            (db_name,),
        )

        if cursor.fetchone():
            print(f"⚠️  数据库 {db_name} 已存在")
            choice = input("是否删除并重建？(y/N): ").strip().lower()
            if choice == "y":
                # 终止所有连接
                cursor.execute(
                    """
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = %s
                """,
                    (db_name,),
                )

                # 删除数据库
                cursor.execute(f"DROP DATABASE {db_name}")
                print(f"✓ 已删除旧数据库 {db_name}")
            else:
                print("操作已取消")
                return False

        # 创建新数据库
        cursor.execute(f"CREATE DATABASE {db_name}")
        print(f"✓ 已创建数据库 {db_name}")

        # 创建专用用户（可选）
        username = f"{db_name}_user"
        password = f"{db_name}_password"

        try:
            cursor.execute(f"CREATE USER {username} WITH PASSWORD '{password}'")
            print(f"✓ 已创建用户 {username}")
        except psycopg2.errors.DuplicateObject:
            print(f"⚠️  用户 {username} 已存在，跳过创建")

        # 授权
        cursor.execute(f"GRANT ALL PRIVILEGES ON DATABASE {db_name} TO {username}")
        cursor.execute(f"ALTER DATABASE {db_name} OWNER TO {username}")
        print(f"✓ 已授予用户 {username} 所有权限")

        conn.close()

        print("\n" + "=" * 60)
        print("✅ 数据库创建成功")
        print("=" * 60)
        print(f"\n请使用以下配置连接数据库：")
        print(f"{{")
        print(f'  "host": "{config.host}",')
        print(f'  "port": {config.port},')
        print(f'  "user": "{username}",')
        print(f'  "password": "{password}",')
        print(f'  "database": "{db_name}"')
        print(f"}}\n")

        return True

    except Exception as e:
        print(f"❌ 创建数据库失败: {e}")
        import traceback

        traceback.print_exc()
        return False


def list_databases():
    """列出所有数据库"""
    config = get_config()

    print("当前数据库列表:")
    print("=" * 60)

    try:
        conn = psycopg2.connect(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            database="postgres",
        )

        cursor = conn.cursor()
        cursor.execute("""
            SELECT datname, pg_size_pretty(pg_database_size(datname)) as size
            FROM pg_database
            WHERE datistemplate = false
            ORDER BY datname;
        """)

        for row in cursor.fetchall():
            print(f"  {row['datname']:<30} {row['size']}")

        conn.close()

    except Exception as e:
        print(f"❌ 列出数据库失败: {e}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python create_project_db.py <database_name>")
        print("或: python create_project_db.py --list")
        sys.exit(1)

    if sys.argv[1] == "--list":
        list_databases()
    else:
        db_name = sys.argv[1]
        # 验证数据库名（只允许字母、数字、下划线）
        import re

        if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", db_name):
            print(f"❌ 无效的数据库名: {db_name}")
            print("   只允许字母、数字和下划线，且必须以字母开头")
            sys.exit(1)

        create_database(db_name)

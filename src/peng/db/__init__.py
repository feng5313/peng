"""
PostgreSQL数据库模块

包含：
- connection.py: 数据库连接池管理
- postgres_auto_deploy.py: 自动部署PostgreSQL
- init_postgres_db.py: 数据库初始化
- create_project_db.py: 创建项目数据库
"""

from .connection import (DBConfig, close_pool, get_async_connection,
                         get_config, get_connection, get_cursor, row_to_dict,
                         rows_to_list)
from .postgres_auto_deploy import (check_postgresql_available,
                                   ensure_postgresql, ensure_postgresql_fly,
                                   ensure_postgresql_msg,
                                   ensure_postgresql_task)

__all__ = [
    # connection.py
    "DBConfig",
    "close_pool",
    "get_connection",
    "get_cursor",
    "row_to_dict",
    "rows_to_list",
    "get_config",
    "get_async_connection",
    # postgres_auto_deploy.py
    "ensure_postgresql",
    "ensure_postgresql_fly",
    "ensure_postgresql_task",
    "ensure_postgresql_msg",
    "check_postgresql_available",
]

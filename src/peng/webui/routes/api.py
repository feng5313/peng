"""
API 路由（兼容性入口）

本模块已拆分为多个子模块，此处保留 router 和 file_watcher 的导出，
确保 server.py 和其他模块的引用兼容。

子模块列表：
- _common.py: 共享工具函数、常量、Pydantic模型
- file_operations.py: 文件操作（files/*, editor/*, upload/*, FileWatcher）
- messages.py: 消息管理（messages/*）
- tasks.py: 事务管理（tasks/*, broadcast-task）
- workdir_config.py: 工作目录和配置（workdir, config, project-structure）
- terminal.py: 终端管理（terminal/*）
- llm_routes.py: LLM/模型管理（llm/*, models/*, output-history, input-history）
- system.py: 系统命令和调试（command, status, debug-webui/*）
- profile.py: 用户配置（v1/users/me）
- snapshots.py: 快照管理（snapshots/*）
- nest_routes.py: Nest管理和连接池监控（nest/*, connection-pool/*）
- lsp_routes.py: LSP依赖管理（lsp/*）
"""

from __future__ import annotations

# 保留 router 导出（server.py 通过 api.router 注册路由）
from fastapi import APIRouter

router = APIRouter()

# 保留 file_watcher 导出（server.py 通过 api.file_watcher 启动文件监测）
from peng.webui.routes.file_operations import file_watcher

# 保留 _current_workdir 导出（server.py _init_workdir 通过 api._current_workdir 设置）
from peng.webui.routes.workdir_config import _current_workdir

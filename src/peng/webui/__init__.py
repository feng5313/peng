"""
{AGENT_NAME}的 Web UI 模块

实现 docs/requests.md §9 web ui 功能需求

主要组件：
- server.py: FastAPI 服务器
- websocket.py: WebSocket 管理
- routes/: API 路由
- html/: 静态文件（HTML/CSS/JS）
"""

from .server import create_app, run_webui

__all__ = [
    "create_app",
    "run_webui",
]

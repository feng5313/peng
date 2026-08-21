"""
页面路由

提供 HTML 页面服务

需求映射：docs/requests.md §9
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

from peng.config import get_agent_name

logger = logging.getLogger(__name__)

router = APIRouter()

# HTML 目录
HTML_DIR = Path(__file__).parent.parent / "html"

# Agent 名称
AGENT_NAME = get_agent_name()

# 模板引擎（全局单例，使用原始 Jinja2 Environment 避免缓存问题）
try:
    from jinja2 import Environment, FileSystemLoader

    template_env = Environment(
        loader=FileSystemLoader(str(HTML_DIR)),
        autoescape=True,
        auto_reload=True,
    )
except ImportError:
    logger.error("未安装 jinja2，请运行: pip install jinja2")
    raise


@router.get("/", response_class=HTMLResponse, response_model=None)
async def index(request: Request) -> HTMLResponse:
    """主页"""
    agent_name = request.app.state.agent_name
    template = template_env.get_template("index.html")
    html_content = template.render(request=request, agent_name=agent_name, cache_v=int(time.time()))
    response = HTMLResponse(content=html_content)
    # 🔧 Task#327：确保浏览器不缓存index.html，避免JS模块文件更新后用户仍在使用旧代码
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


@router.get("/lsp", response_class=HTMLResponse, response_model=None)
async def lsp_management(request: Request) -> HTMLResponse:
    """LSP语言服务器管理页面"""
    agent_name = request.app.state.agent_name
    template = template_env.get_template("lsp.html")
    html_content = template.render(request=request, agent_name=agent_name)
    return HTMLResponse(content=html_content)


@router.get("/test-tab-close", response_class=HTMLResponse, response_model=None)
async def test_tab_close(request: Request) -> HTMLResponse:
    """测试关闭按钮hover效果"""
    template = template_env.get_template("test-tab-close.html")
    html_content = template.render(request=request)
    return HTMLResponse(content=html_content)


@router.get("/favicon.ico", response_model=None)
async def favicon() -> FileResponse | None:
    """网站图标"""
    favicon_file = HTML_DIR / "static" / "favicon.ico"
    if favicon_file.exists():
        return FileResponse(favicon_file)
    return None


def _get_default_html() -> str:
    """获取默认 HTML 页面（开发阶段使用）"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{AGENT_NAME} Web UI</title>
    <link rel="stylesheet" href="/static/css/style.css">
</head>
<body class="dark-theme">
    <!-- 标题栏 -->
    <header class="header">
        <div class="header-title">
            <span class="logo"><img src="/static/img/favicon.svg" alt="logo" style="height:3.6em;vertical-align:middle;"></span>
            <h1>{AGENT_NAME}  大鹏一日同风起，扶摇直上九万里！</h1>
        </div>
        <div class="header-actions">
            <button id="theme-toggle" class="btn-icon" title="切换主题">
                🌙
            </button>
            <a href="/docs" target="_blank" class="btn-link">API 文档</a>
        </div>
    </header>

    <!-- 主内容区 -->
    <div class="main-container">
        <!-- 左边栏：消息树 -->
        <aside class="sidebar sidebar-left">
            <div class="sidebar-header">
                <h2>📬 消息</h2>
                <button id="new-message-btn" class="btn-small">+ 新消息</button>
            </div>
            <div id="message-tree" class="tree-container">
                <div class="loading">加载中...</div>
            </div>
        </aside>

        <!-- 中间：主界面 -->
        <main class="main-content">
            <!-- 信息显示框 -->
            <div class="output-container">
                <div id="output" class="output">
                    <div class="welcome-message">
                        <p>🪽 欢迎使用{AGENT_NAME}的 Web UI</p>
                        <p>选择左侧消息或创建新消息开始对话</p>
                    </div>
                </div>
            </div>

            <!-- 输入栏 -->
            <div class="input-container">
                <div class="input-options">
                    <select id="model-select" class="model-select">
                        <option value="glm-5">GLM-5</option>
                        <option value="glm-4.7">GLM-4.7</option>
                        <option value="qwen3.5-plus">Qwen3.5-Plus</option>
                    </select>
                    <label class="checkbox-label">
                        <input type="checkbox" id="thinking-toggle">
                        思考模式
                    </label>
                    <label class="checkbox-label">
                        <input type="checkbox" id="search-toggle">
                        搜索增强
                    </label>
                </div>
                <div class="input-row">
                    <textarea id="input" placeholder="输入消息..." rows="3"></textarea>
                    <div class="input-actions">
                        <button id="attach-btn" class="btn-icon" title="上传文件">📎</button>
                        <button id="send-btn" class="btn-primary">发送</button>
                    </div>
                </div>
            </div>
        </main>

        <!-- 右侧栏：事务树 -->
        <aside class="sidebar sidebar-right">
            <div class="sidebar-header">
                <h2>📋 事务</h2>
            </div>
            <div id="task-tree" class="tree-container">
                <div class="loading">加载中...</div>
            </div>
        </aside>
    </div>

    <!-- 状态栏 -->
    <footer class="footer">
        <div class="status-left">
            <span id="status-indicator" class="status-dot running"></span>
            <span id="status-text">服务运行中</span>
        </div>
        <div class="status-center">
            <span id="connection-status">已连接</span>
        </div>
        <div class="status-right">
            <a href="https://github.com" target="_blank">帮助</a>
        </div>
    </footer>

    <script src="/static/js/app.js"></script>
</body>
</html>
"""

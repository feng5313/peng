"""
Web UI 服务器

基于 FastAPI 实现，提供：
- RESTful API（消息、事务、状态）
- WebSocket（实时进程输出）
- 静态文件服务

需求映射：docs/requests.md §9
"""

from __future__ import annotations

from peng.config import get_agent_name, get_path

# 全局 uvicorn server 引用，允许外部触发优雅关闭
_uvicorn_server = None

AGENT_NAME = get_agent_name()

import asyncio  # noqa: E402
import logging  # noqa: E402
import traceback  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from pathlib import Path  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.middleware.gzip import GZipMiddleware  # noqa: E402


# 模块级别初始化日志（确保通过 uvicorn 启动时日志正常工作）
def _setup_logging():
    """初始化日志配置"""
    from peng.config import get_project_root, load_config
    from peng.common.paths import chown_to_project_owner

    root_logger = logging.getLogger()

    # 如果已经有 FileHandler，跳过
    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler):
            return

    # 设置根日志器级别为 INFO
    root_logger.setLevel(logging.INFO)

    # 从配置读取日志路径
    log_file = get_path("log_file")

    log_file.parent.mkdir(parents=True, exist_ok=True)
    chown_to_project_owner(log_file.parent)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    chown_to_project_owner(log_file)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s")
    )
    root_logger.addHandler(file_handler)


_setup_logging()

logger = logging.getLogger(__name__)

# 静态文件和模板目录
HTML_DIR = Path(__file__).parent / "html"
STATIC_DIR = HTML_DIR / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """应用生命周期管理"""
    from peng.ui.output_reader import get_output_reader
    from peng.webui.websocket import terminal_session_manager
    from peng.webui.routes.api import file_watcher
    from peng.config.loader import load_config

    output_reader = get_output_reader()

    # 启动时：启动监控事务（回调在 WebSocket 连接时注册）
    await output_reader.start_watching()
    logger.info("✅ 输出文件监控事务已启动")

    # 启动文件变化监测
    file_watcher.start()
    logger.info("✅ 文件变化监测已启动")

    # 初始化Channel消息通道系统（微信/飞书等）
    try:
        peng_config = load_config()
        channels_config = peng_config.channels if hasattr(peng_config, 'channels') else {}
        if not channels_config:
            # 尝试从原始配置字典获取
            try:
                import json
                config_path = peng_config.paths.config_dir if hasattr(peng_config, 'paths') else "config"
                from pathlib import Path
                config_file = Path(config_path) / "peng.json"
                if config_file.exists():
                    with open(config_file, 'r', encoding='utf-8') as f:
                        raw_config = json.load(f)
                    channels_config = raw_config.get("channels", {})
            except Exception:
                channels_config = {}

        if channels_config.get("enabled", False):
            from peng.channels.integration import init_channel_system
            from peng.channels.routes import init_channels as init_channel_routes

            channel_manager = init_channel_system(channels_config)
            init_channel_routes(channel_manager)
            await channel_manager.start_all()
            logger.info("✅ Channel消息通道系统已启动")
        else:
            logger.info("ℹ️ Channel消息通道系统未启用")
    except Exception as e:
        logger.warning(f"⚠️ Channel消息通道系统初始化失败（非致命）: {e}")

    # 🔧 修复：启动定期清理终端会话任务
    try:
        config = load_config()
        session_timeout = config.webui.get("terminal", {}).get("session_timeout", 3600)
    except Exception:
        session_timeout = 3600

    async def cleanup_inactive_sessions_task():
        """定期清理不活跃的终端会话"""
        while True:
            await asyncio.sleep(300)  # 每5分钟清理一次
            terminal_session_manager.cleanup_inactive_sessions(max_age=session_timeout)

    cleanup_task = asyncio.create_task(cleanup_inactive_sessions_task())
    logger.info(
        f"✅ 终端会话定期清理任务已启动（每5分钟清理一次，最大存活时间={session_timeout}秒）"
    )

    # 启动 Provider 吞吐率定期广播任务
    async def throughput_broadcast_task():
        """定期读取跨进程共享的吞吐率数据并广播到前端"""
        from peng.llm.throughput_tracker import get_throughput_tracker
        from peng.webui.websocket import manager
        from peng.nest.bridge import NestBridgeManager

        cleanup_counter = 0  # 每30秒清理一次过期文件
        while True:
            await asyncio.sleep(3.0)  # 🔧 每3秒采样并广播一次（流量计算用EWMA 5秒时间常数平滑）
            try:
                tracker = get_throughput_tracker()
                snapshot = tracker.get_snapshot()
                if not snapshot or not snapshot.get("throughputs"):
                    pass
                throughputs = snapshot.get("throughputs", {})
                frame_rates = snapshot.get("frame_rates", {})
                
                # 🔧 采集 nest 流量统计
                nest_manager = NestBridgeManager.get_instance()
                nest_manager.update_all_traffic_rates()
                nest_traffic = nest_manager.get_traffic_snapshots()
                
                has_data = throughputs or nest_traffic
                if has_data and len(manager.active_connections) > 0:
                    broadcast_data = {
                        "type": "throughput_update",
                        "throughputs": throughputs,
                        "frame_rates": frame_rates,
                        "nest_traffic": nest_traffic,
                    }
                    await manager.broadcast(broadcast_data)
                # 每30秒清理一次过期文件（约10次循环）
                cleanup_counter += 1
                if cleanup_counter >= 10:
                    cleanup_counter = 0
                    tracker.cleanup()
                    logger.debug("[ThroughputBroadcast] 已执行过期文件清理")
            except Exception as e:
                logger.debug(f"吞吐率广播异常（可忽略）: {e}")

    throughput_task = asyncio.create_task(throughput_broadcast_task())
    logger.info("✅ Provider 吞吐率定期广播任务已启动")

    yield

    # 关闭时：停止吞吐率广播任务
    throughput_task.cancel()
    try:
        await throughput_task
    except asyncio.CancelledError:
        pass
    logger.info("✅ Provider 吞吐率定期广播任务已停止")

    # 关闭时：停止Channel消息通道系统
    try:
        from peng.channels.integration import get_channel_manager
        channel_manager = get_channel_manager()
        if channel_manager and channel_manager.is_running:
            await channel_manager.stop_all()
            logger.info("✅ Channel消息通道系统已停止")
    except Exception as e:
        logger.debug(f"Channel停止异常（可忽略）: {e}")

    # 关闭时：停止清理任务
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    logger.info("✅ 终端会话定期清理任务已停止")

    # 关闭时：停止文件变化监测
    file_watcher.stop()
    logger.info("✅ 文件变化监测已停止")

    # 关闭时：停止监控事务
    await output_reader.stop_watching()
    logger.info("✅ 输出文件监控事务已停止")


def create_app() -> FastAPI:
    """创建 FastAPI 应用"""
    app = FastAPI(
        title=f"{AGENT_NAME} Web UI",
        description=f"{AGENT_NAME}的 Web 用户界面",
        version="0.1.0",
        lifespan=_lifespan,
    )

    # 保存到 app.state 以便路由使用
    app.state.agent_name = AGENT_NAME

    # 🔧 性能优化：启用 Gzip 压缩中间件
    # 将 1.1MB 的静态资源压缩到 ~215KB（减少80%传输量）
    # minimum_size=500: 最小压缩阈值，避免压缩已经很小或不可压缩的响应
    # compresslevel=6: 中等压缩级别，平衡压缩率和CPU开销（默认9太慢）
    app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)

    # 挂载静态文件
    # 🔧 Task#327：静态文件禁用缓存（开发期），确保浏览器每次加载最新JS/CSS
    from starlette.middleware.base import BaseHTTPMiddleware
    class NoCacheMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            if request.url.path.startswith("/static/"):
                response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
                response.headers["Pragma"] = "no-cache"
                if "etag" in response.headers:
                    del response.headers["etag"]
                if "last-modified" in response.headers:
                    del response.headers["last-modified"]
            return response
    app.add_middleware(NoCacheMiddleware)
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # 注册路由
    from .routes import api, pages

    # api.py 已拆分为子模块，此处 api.router 为空（仅保留兼容导出）
    # 各子模块分别注册路由
    app.include_router(api.router, prefix="/api")
    app.include_router(pages.router)

    # 子模块路由（从 api.py 拆分出的功能模块）
    from .routes.file_operations import router as file_ops_router
    from .routes.messages import router as messages_router
    from .routes.tasks import router as tasks_router
    from .routes.workdir_config import router as workdir_config_router
    from .routes.terminal import router as terminal_router
    from .routes.llm_routes import router as llm_router
    from .routes.system import router as system_router
    from .routes.profile import router as profile_router
    from .routes.snapshots import router as snapshots_router
    from .routes.nest_routes import router as nest_router
    from .routes.lsp_routes import router as lsp_router

    app.include_router(file_ops_router, prefix="/api")
    app.include_router(messages_router, prefix="/api")
    app.include_router(tasks_router, prefix="/api")
    app.include_router(workdir_config_router, prefix="/api")
    app.include_router(terminal_router, prefix="/api")
    app.include_router(llm_router, prefix="/api")
    app.include_router(system_router, prefix="/api")
    app.include_router(profile_router, prefix="/api")
    app.include_router(snapshots_router, prefix="/api")
    app.include_router(nest_router, prefix="/api")
    app.include_router(lsp_router, prefix="/api")

    # CLI API 路由（为远程模式提供 HTTP API）
    from .routes.msg_cli_api import router as msg_cli_router
    from .routes.task_cli_api import router as task_cli_router

    app.include_router(msg_cli_router, prefix="/api")
    app.include_router(task_cli_router, prefix="/api")

    # Channel 消息通道路由（微信/飞书回调）
    from peng.channels.routes import router as channel_router
    from peng.channels.routes import init_channels as init_channel_routes

    app.include_router(channel_router, prefix="/api")

    # WebSocket 路由
    from .websocket import websocket_endpoint, terminal_websocket_endpoint

    app.add_api_websocket_route("/ws/output", websocket_endpoint)
    app.add_api_websocket_route("/ws/terminal", terminal_websocket_endpoint)

    return app


# 模块级别 app 实例，供 uvicorn 使用
app = create_app()


def _init_workdir() -> None:
    """初始化工作目录（从配置读取并切换）

    同时设置 api._current_workdir 全局变量，确保 /api/config 返回正确的值。
    """
    import os

    try:
        from peng.config.loader import load_config
        from peng.webui.routes import workdir_config

        config = load_config()
        workdir = config.webui.workdir

        if workdir:
            expanded_path = Path(workdir).expanduser().resolve()
            if expanded_path.is_dir():
                os.chdir(str(expanded_path))
                workdir_config._current_workdir = str(expanded_path)
                logger.info(f"WebUI 工作目录初始化：{expanded_path}")
            else:
                logger.warning(f"配置的工作目录不存在：{workdir}")
    except Exception as e:
        logger.warning(f"初始化工作目录失败：{e}\n{traceback.format_exc()}")


async def run_webui(
    host: str = "0.0.0.0", port: int = 6688, shutdown_event: asyncio.Event | None = None
) -> None:
    """
    运行 Web UI 服务器

    Args:
        host: 监听地址
        port: 监听端口
        shutdown_event: 外部提供的关闭事件（用于优雅关闭）
    """
    import asyncio
    import signal
    import socket
    import uvicorn

    from peng.common.platform import kill_port_process

    # 初始化工作目录（从配置读取）
    _init_workdir()

    app = create_app()

    # 启动前检查端口是否被占用
    def _is_port_in_use(port: int) -> bool:
        """检查端口是否被占用"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                # 设置 SO_REUSEADDR 允许立即重用端口（避免 TIME_WAIT 问题）
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", port))
                return False
        except OSError:
            return True

        # 检查端口占用

    if _is_port_in_use(port):
        logger.error(f"❌ 端口 {port} 已被占用，正在尝试自动清理...")
        logger.info(f"💡 提示：使用 './bin/fly kill --port {port}' 可手动释放端口")

        # 自动清理端口
        if kill_port_process(port):
            logger.info(f"✅ 端口 {port} 已自动释放")
            # 等待端口完全释放
            await asyncio.sleep(1)
        else:
            # 清理失败，抛出异常而不是 sys.exit
            # 让上层调用者能够正确处理
            raise RuntimeError(f"端口 {port} 已被占用且无法自动释放")

    # 如果没有提供外部 shutdown_event，创建一个并设置信号处理器
    if shutdown_event is None:
        shutdown_event = asyncio.Event()

        def signal_handler():
            """信号处理器"""
            logger.info("🛑 收到停止信号，设置 shutdown_event...")
            shutdown_event.set()

        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGINT, signal_handler)
        loop.add_signal_handler(signal.SIGTERM, signal_handler)
        logger.info("✅ WebUI 已设置信号处理器 (SIGINT, SIGTERM)")

    # 创建 filter 过滤掉 connection open/closed 日志
    class _ConnectionFilter(logging.Filter):
        """过滤掉 WebSocket connection open/closed 日志"""

        def filter(self, record):
            msg = record.getMessage()
            return msg not in ["connection open", "connection closed", "opening handshake failed"]

    _connection_filter = _ConnectionFilter()

    # 获取根日志器的文件处理器并添加 filter
    root_logger = logging.getLogger()
    file_handler = None
    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler):
            file_handler = handler
            file_handler.addFilter(_connection_filter)
            break

    if not file_handler:
        from peng.config import get_path, get_project_root

        try:
            log_file = get_path("log_file")
        except Exception as e:
            logger.critical(f"❌ 无法获取日志文件路径: {e}")
            import sys

            sys.exit(1)

        log_file.parent.mkdir(parents=True, exist_ok=True)
        chown_to_project_owner(log_file.parent)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        chown_to_project_owner(log_file)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s")
        )
        file_handler.addFilter(_connection_filter)
        root_logger.addHandler(file_handler)

    # 给所有 StreamHandler 添加 filter，防止 connection open/closed 输出到控制台
    for handler in root_logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.addFilter(_connection_filter)

    # websockets 库的日志 propagate 到根日志器，会被 filter 过滤
    for name in [
        "websockets",
        "websockets.server",
        "websockets.legacy",
        "websockets.legacy.server",
    ]:
        ws_logger = logging.getLogger(name)
        ws_logger.setLevel(logging.INFO)
        ws_logger.propagate = True

    # 创建只写文件的 handler
    class _SilentHandler(logging.Handler):
        """静默 handler：只写入文件，不输出到控制台"""

        def emit(self, record):
            file_handler.emit(record)

    _silent_handler = _SilentHandler()
    _silent_handler.setFormatter(file_handler.formatter)
    _silent_handler.addFilter(_connection_filter)

    # 创建过滤器：将 uvicorn 英文启动消息替换为中文
    class _UvicornChineseFilter(logging.Filter):
        """将 uvicorn 的英文启动消息替换为中文"""

        def filter(self, record):
            msg = record.getMessage()

            # 替换 "Uvicorn running on" 为中文
            if "Uvicorn running on" in msg:
                import re

                # 匹配格式: "Uvicorn running on http://host:port (Press CTRL+C to quit)"
                match = re.search(
                    r"Uvicorn running on (http://[^\s]+) \(Press CTRL\+C to quit\)", msg
                )
                if match:
                    url = match.group(1)
                    # 替换为中文，加上 emoji 图标
                    new_msg = f"🪽 {AGENT_NAME}  服务器运行在 {url} (按 CTRL+C 退出)"
                    # 修改 record 的 msg 属性
                    record.msg = new_msg
                    record.args = ()  # 清空参数，避免格式化问题

            # 替换 "Started server process" 为中文
            if "Started server process" in msg:
                import re

                # 匹配格式: "Started server process [PID]"
                match = re.search(r"Started server process \[(\d+)\]", msg)
                if match:
                    pid = match.group(1)
                    # 替换为中文，加上 emoji 图标
                    new_msg = f"🪽 已启动 {AGENT_NAME} 服务器进程 [PID: {pid}]"
                    record.msg = new_msg
                    record.args = ()

            return True

    _uvicorn_chinese_filter = _UvicornChineseFilter()

    # 给根日志器添加中文转换过滤器
    root_logger = logging.getLogger()
    root_logger.addFilter(_uvicorn_chinese_filter)

    # WebSocket/HTTP 协议日志：只写入文件
    for name in ["uvicorn.protocols.websockets", "uvicorn.protocols.http", "uvicorn.access"]:
        proto_logger = logging.getLogger(name)
        proto_logger.handlers = [_silent_handler]
        proto_logger.propagate = False

    # uvicorn 主日志配置：使用根日志器的 handlers（包含文件和控制台）
    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
        log_config=None,  # 禁用 uvicorn 自动日志配置，使用现有配置
        # WebSocket keepalive 配置：增加间隔和超时，降低 ping 频率
        ws_ping_interval=30,  # ping 间隔 30 秒（默认 20 秒）
        ws_ping_timeout=30,  # ping 超时 30 秒（默认 20 秒）
    )

    server = uvicorn.Server(config)

    # 保存 server 引用，允许外部触发优雅关闭
    global _uvicorn_server
    _uvicorn_server = server

    # 直接给 uvicorn 的日志器添加过滤器
    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_logger.addFilter(_uvicorn_chinese_filter)

    # 给 uvicorn.error 也添加过滤器
    uvicorn_error_logger = logging.getLogger("uvicorn.error")
    uvicorn_error_logger.addFilter(_uvicorn_chinese_filter)

    logger.info(f"🌐 Web UI 监听：http://{host}:{port}")

    # 创建后台事务运行服务器
    serve_task = asyncio.create_task(server.serve())

    try:
        # 直接等待服务器事务完成（不等待 shutdown_event）
        await serve_task
        logger.info("✅ WebUI 服务器正常关闭")
    except KeyboardInterrupt:
        # Ctrl+C 信号，确保服务器被关闭
        logger.info("🌐 Web UI 收到 Ctrl+C，正在关闭服务器...")
        server.should_exit = True
        try:
            await serve_task
        except asyncio.CancelledError:
            pass
        raise
    except asyncio.CancelledError:
        # CancelledError 可能来自 uvicorn 关闭
        logger.info("🌐 Web UI 收到取消信号，正在关闭服务器...")
        server.should_exit = True
        try:
            await serve_task
        except asyncio.CancelledError:
            pass
    except Exception as e:
        # 捕获所有其他异常
        logger.error(f"🌐 Web UI 异常: {e}", exc_info=True)
        server.should_exit = True

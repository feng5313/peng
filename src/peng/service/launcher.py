"""
服务启动器

提供服务的健康检查和启动失败时的自动回退功能。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# 默认配置
DEFAULT_PORT = 6688
DEFAULT_SERVICE_NAME = "peng"


class ServiceLauncher:
    """服务启动器"""

    def __init__(self, port: int = DEFAULT_PORT, service_name: str = DEFAULT_SERVICE_NAME):
        """
        初始化启动器

        Args:
            port: 服务端口（默认6688）
            service_name: 服务名称（默认"peng"）
        """
        self.running = False
        self.startup_complete = False
        self.check_interval = 2  # 健康检查间隔（秒）
        self.max_checks = 30  # 最大检查次数（60秒）
        self.port = port
        self.service_name = service_name
        # 启动成功标记文件路径
        from pathlib import Path

        self.success_marker_path = Path(
            f"~/.config/{self.service_name}/startup_success.json"
        ).expanduser()
        # 停止事件（用于信号处理器通知主循环）
        self._shutdown_requested = asyncio.Event()

    def _get_current_git_version(self) -> str:
        """获取当前Git版本"""
        try:
            import subprocess

            result = subprocess.run(
                ["git", "log", "-1", "--format=%H"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return "unknown"

    def _set_success_marker(self):
        """设置启动成功标记"""
        import json
        import os

        self.success_marker_path.parent.mkdir(parents=True, exist_ok=True)

        marker_data = {
            "startup_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "pid": os.getpid(),
            "version": self._get_current_git_version(),
            "port": self.port,
        }

        with open(self.success_marker_path, "w") as f:
            json.dump(marker_data, f, indent=2)

        logger.info(f"✅ 已设置启动成功标记: {self.success_marker_path}")

    def _get_success_marker(self) -> dict:
        """
        获取启动成功标记

        Returns:
            启动成功标记信息字典，如果标记不存在返回None
        """
        import json

        if not self.success_marker_path.exists():
            return None

        try:
            with open(self.success_marker_path, "r") as f:
                marker_data = json.load(f)
            logger.info(f"🔍 检测到启动成功标记: time:{marker_data.get('startup_time')} port:{marker_data.get('port')}")
            return marker_data
        except Exception as e:
            logger.warning(f"读取启动成功标记失败: {e}")
            return None

    def _clear_success_marker(self):
        """清除启动成功标记"""
        if self.success_marker_path.exists():
            self.success_marker_path.unlink()
            logger.info(f"✅ 已清除启动成功标记")

    async def _notify_crash_recovery(self, crash_branch: str, crash_info: dict):
        """
        崩溃恢复后自动发布消息/事务处理崩溃分支

        Args:
            crash_branch: 崩溃分支名称
            crash_info: 崩溃信息
        """
        try:
            from peng.message.manager import MessageManager
            from peng.config import get_agent_name

            agent_name = get_agent_name()

            # 构造消息内容
            message_content = f"""服务崩溃已自动恢复

服务名称：{self.service_name}
崩溃时间：{crash_info.get("timestamp", "未知")}
错误类型：{crash_info.get("exception_type", "未知")}
错误消息：{crash_info.get("error_message", "未知")}
崩溃分支：{crash_branch}

已执行恢复操作：
1. 创建崩溃分支保存导致崩溃的代码：{crash_branch}
2. 自动回退到上一个稳定版本
3. 服务已恢复运行

请处理崩溃分支 {crash_branch} 中的问题：
- 分析崩溃原因
- 修复代码bug
- 测试修复方案
- 修复合并后重新部署

切换到崩溃分支查看代码：
git checkout {crash_branch}
"""

            # 创建消息管理器
            msg_manager = MessageManager()

            # 发送消息给自己（鹏🐣），提醒处理崩溃代码
            msg_id, msg = msg_manager.create_message(
                content=message_content,
                sender=agent_name,
                receiver=agent_name,  # 发送给自己
                caller="launcher",
                caller_session_id=f"crash_recovery_{int(time.time())}",
                timeout=3600,  # 1小时超时
            )

            if msg_id and msg_id > 0:
                logger.info(f"✅ 已发送崩溃恢复通知消息: msg_id={msg_id}")
                logger.info(f"   inbox_monitor会自动创建事务")
            else:
                logger.error(f"❌ 发送崩溃恢复通知消息失败")

        except Exception as e:
            logger.error(f"❌ 崩溃恢复通知失败: {e}", exc_info=True)
            # 不影响服务恢复，只是日志记录错误

    async def _handle_startup_failure_recovery(self):
        """处理启动失败的恢复"""
        logger.warning("⚠️  检测到上一次启动失败，执行崩溃恢复...")

        try:
            from peng.service.git_recovery import safe_rollback_to_stable
            from peng.service.version_manager import get_available_versions, backup_current_version

            versions = get_available_versions(self.service_name)
            logger.info(f"当前可用版本数量: {len(versions)}")

            crash_branch_name = None

            if len(versions) == 0:
                # 第一次崩溃，创建初始版本备份
                logger.info("检测到第一次崩溃，创建初始版本备份")
                success, msg, version_name = backup_current_version(self.service_name)
                if success:
                    logger.info(f"✅ 已创建初始版本: {version_name}")
                else:
                    logger.warning(f"❌ 创建初始版本失败: {msg}")
            else:
                # 已有版本，回退到最后一个稳定版本
                logger.info("检测到有历史版本，执行安全回退...")
                logger.info("将先创建崩溃分支保存改动，然后回退到稳定版本")

                # 构造崩溃信息
                crash_info = {
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "crash_type": "startup_failure",
                    "error_message": "服务启动失败",
                    "exception_type": "StartupFailureError",
                    "port": self.port,
                    "service_name": self.service_name,
                }

                success, msg = safe_rollback_to_stable(crash_info, self.service_name)
                if success:
                    logger.info(f"✅ 安全回退成功: {msg}")
                    logger.info("✅ 已回退到稳定版本，将继续启动")

                    # 提取崩溃分支名称
                    if "分支名称：" in msg:
                        branch_line = [line for line in msg.split("\n") if "分支名称：" in line][0]
                        crash_branch_name = branch_line.split("：")[-1].strip()
                        logger.info(f"崩溃分支名称: {crash_branch_name}")

                        # 自动发布消息/事务处理崩溃分支
                        await self._notify_crash_recovery(crash_branch_name, crash_info)
                else:
                    logger.error(f"❌ 安全回退失败: {msg}")

            # 清除旧的启动成功标记
            self._clear_success_marker()
        except Exception as e:
            logger.error(f"崩溃恢复异常: {e}", exc_info=True)

    async def run_with_health_check(self, service_func):
        """
        运行服务并进行健康检查

        Args:
            service_func: 服务启动函数
        """
        self.running = True

        # 设置信号处理器（跨平台：POSIX用add_signal_handler，Windows用signal.signal）
        loop = asyncio.get_event_loop()
        shutdown_event = asyncio.Event()

        def signal_handler():
            """信号处理器 - 同时触发shutdown_event和BackgroundService的停止"""
            logger.info("🛑 收到停止信号...")
            self.running = False
            shutdown_event.set()
            # 触发停止信号文件，让BackgroundService检测到停止信号
            from peng.ui.service import get_stop_signal_file
            try:
                get_stop_signal_file().touch()
                logger.info("✅ 已创建停止信号文件")
            except Exception as e:
                logger.warning(f"创建停止信号文件失败: {e}")

        # 添加信号处理器（跨平台封装）
        from peng.common.platform import setup_signal_handlers, IS_WINDOWS

        handlers_setup = setup_signal_handlers(loop, signal_handler, (signal.SIGINT, signal.SIGTERM))

        if IS_WINDOWS and not handlers_setup:
            # Windows: add_signal_handler 不可用，使用 signal.signal 代替
            # 注意：signal.signal 只能在线程主线程中设置
            def windows_signal_handler(signum, frame):
                logger.info(f"🛑 收到Windows信号 {signum}...")
                signal_handler()

            try:
                signal.signal(signal.SIGINT, windows_signal_handler)
                signal.signal(signal.SIGTERM, windows_signal_handler)
                logger.info("✅ Windows信号处理器已设置 (signal.signal)")
            except (OSError, ValueError) as e:
                logger.warning(f"设置Windows信号处理器失败: {e}")

        # 检查上次启动是否成功
        success_marker = self._get_success_marker()
        current_version = self._get_current_git_version()

        if "--rollback-on-fail" in sys.argv:
            if success_marker:
                marker_version = success_marker.get("version", "unknown")
                if marker_version == current_version:
                    # 有启动成功标记且版本一致，说明上次启动成功
                    logger.info("✅ 检测到上次启动成功，正常启动")
                    logger.info(f"   标记版本: {marker_version}")
                    logger.info(f"   当前版本: {current_version}")
                else:
                    # 版本不一致，说明有新的代码提交
                    logger.info("📝 检测到新代码提交，将正常启动新版本")
                    logger.info(f"   标记版本: {marker_version}")
                    logger.info(f"   当前版本: {current_version}")
                    # 清除旧标记
                    self._clear_success_marker()
            else:
                # 没有启动成功标记，说明上次启动失败或首次启动
                logger.warning("⚠️  没有检测到启动成功标记")
                logger.info("   说明：上次启动失败 或 首次启动")
                logger.info(f"   当前版本: {current_version}")

                # 检查是否有可恢复的版本
                from peng.service.version_manager import get_available_versions

                versions = get_available_versions(self.service_name)
                if len(versions) > 0:
                    logger.warning(f"🔍 检测到有 {len(versions)} 个可用版本，判断上次启动失败")
                    await self._handle_startup_failure_recovery()
                else:
                    logger.info("📝 没有历史版本，判断为首次启动")

        # 启动服务
        logger.info("🚀 正在启动服务...")
        service_task = asyncio.create_task(service_func())

        # 等待服务启动完成
        try:
            await asyncio.wait_for(self._wait_for_startup(), timeout=60)
            logger.info("✅ 服务启动成功")
            self.startup_complete = True
        except asyncio.TimeoutError:
            logger.error("❌ 服务启动超时")
            return False

        # 等待服务运行：同时监听 service_task 和 shutdown_event
        # 收到停止信号时立刻 cancel 并 raise KeyboardInterrupt，
        # 由 asyncio.run() 内置的 KeyboardInterrupt 处理强制清理所有残留任务
        try:
            shutdown_waiter = asyncio.create_task(shutdown_event.wait())
            done, pending = await asyncio.wait(
                [service_task, shutdown_waiter],
                return_when=asyncio.FIRST_COMPLETED,
            )
            # 清理 shutdown waiter
            shutdown_waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await shutdown_waiter

            if shutdown_event.is_set():
                # 立刻 cancel service_task 并退出进程
                logger.info("🛑 收到停止信号，停止服务...")
                service_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await service_task
                # 恢复信号处理器后直接退出进程
                # （不能依赖 asyncio.run() 的清理——uvicorn 的 serve task
                #   不响应 cancel，会导致 asyncio.run() 卡死）
                self._restore_signal_handlers(loop, handlers_setup, IS_WINDOWS)
                os._exit(0)

            # service_task 正常完成，收集结果
            with contextlib.suppress(asyncio.CancelledError):
                await service_task
            return True
        except Exception as e:
            logger.error(f"❌ 服务运行异常：{e}", exc_info=True)
            return False
        finally:
            # 恢复默认信号处理器，确保后续 Ctrl+C 能正常触发 KeyboardInterrupt
            self._restore_signal_handlers(loop, handlers_setup, IS_WINDOWS)

    @staticmethod
    def _restore_signal_handlers(loop, handlers_setup: bool, is_windows: bool) -> None:
        """恢复默认信号处理器，确保后续 Ctrl+C 能正常触发 KeyboardInterrupt

        Args:
            loop: asyncio 事件循环
            handlers_setup: POSIX 信号处理器是否成功设置
            is_windows: 是否是 Windows 平台
        """
        try:
            if is_windows and not handlers_setup:
                # Windows: 恢复默认 SIGINT 处理器
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                logger.info("✅ 已恢复 Windows 默认信号处理器")
            elif handlers_setup:
                # POSIX: 移除自定义信号处理器，恢复默认行为
                from peng.common.platform import cleanup_signal_handlers
                cleanup_signal_handlers(loop, (signal.SIGINT, signal.SIGTERM))
                logger.info("✅ 已恢复 POSIX 默认信号处理器")
        except Exception as e:
            logger.warning(f"恢复信号处理器失败（非致命）: {e}")

    async def _wait_for_startup(self):
        """等待服务启动完成"""
        for i in range(self.max_checks):
            if not self.running:
                break

            # 检查服务是否已启动
            if self._check_service_ready():
                # 服务启动成功，设置启动成功标记
                self._set_success_marker()
                return True

            logger.info(f"⏳ 等待服务启动... ({i + 1}/{self.max_checks})")
            await asyncio.sleep(self.check_interval)

        return False

    def _check_service_ready(self) -> bool:
        """检查服务是否已就绪"""
        try:
            # 检查端口是否监听
            import socket

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(("127.0.0.1", self.port))
            sock.close()
            return result == 0
        except Exception as e:
            logger.debug(f"健康检查：{e}")
            return False

    async def _handle_startup_failure(self, service_task):
        """处理启动失败"""
        logger.warning("⚠️  服务启动失败")

        # 记录崩溃信息
        try:
            from peng.service.crash_logger import record_crash
            from peng.service.version_manager import get_current_version, generate_version_name

            current_version = get_current_version(self.service_name)
            # 如果没有版本，生成一个临时版本号用于记录崩溃
            if not current_version:
                current_version = generate_version_name()
                logger.info(f"没有当前版本，使用临时版本号: {current_version}")

            # 记录启动失败
            success, msg = record_crash(
                version=current_version,
                crash_type="startup_failure",
                error_message="服务启动超时或启动后运行异常",
                port=self.port,
                service_name=self.service_name,
                additional_info={
                    "timeout_seconds": 60,
                    "max_checks": self.max_checks,
                    "check_interval": self.check_interval,
                    "port": self.port,
                },
            )
            if success:
                logger.info(f"📝 {msg}")
            else:
                logger.warning(f"记录崩溃日志失败：{msg}")
        except Exception as e:
            logger.error(f"记录崩溃信息异常：{e}", exc_info=True)

        # 取消服务事务
        if not service_task.done():
            service_task.cancel()
            try:
                await service_task
            except asyncio.CancelledError:
                pass

        # 停止服务进程
        try:
            from peng.common.platform import kill_port_process

            kill_port_process(self.port)
            logger.info("✅ 已清理端口")
        except Exception as e:
            logger.warning(f"清理端口失败：{e}")

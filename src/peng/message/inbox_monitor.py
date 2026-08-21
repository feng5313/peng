"""
Inbox监控器（简化版）

只检测是否有新消息，如果有则交给消息处理模块处理。
不做其他事（不处理心跳、不管理锁等）。

职责：
- 定时检查是否有新消息
- 发现新消息后调用 MessageManager.handle_new_message 处理
- 定时检查并重启鹏🐣-忠实助手事务（根据配置）
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import traceback
from typing import Any

from peng.config import get_agent_name, load_config, get_project_root
from peng.common.colors import ANSI_COLORS

from .database import MessageDatabase
from .manager import MessageManager

logger = logging.getLogger(__name__)

# Agent 名称
AGENT_NAME = get_agent_name()
project_root_path = get_project_root()


class InboxMonitor:
    """
    Inbox监控器（简化版）

    只负责检测新消息，发现后调用 MessageManager.handle_new_message 处理。
    同时定时检查并重启鹏🐣-忠实助手事务。
    """

    DEFAULT_CHECK_INTERVAL = 1  # 默认1秒检查一次

    def __init__(
        self,
        check_interval: int = DEFAULT_CHECK_INTERVAL,
    ):
        """
        初始化监控器

        注意：PostgreSQL 不需要数据库路径，使用连接池。

        Args:
            check_interval: 检查间隔（秒）
        """
        self.check_interval = check_interval
        self.db = MessageDatabase()
        self.manager = MessageManager()

        self._running = False
        self._task: asyncio.Task[None] | None = None

        # 【优化】记录每个消息的上次处理时间，实现时间窗口去重
        # 格式: {msg_id: 上次处理时间戳}
        self._last_processed: dict[int, float] = {}

        # 鹏🐣-忠实助手定时运行
        config = load_config()
        self.task_manager_check_interval = config.monitor.task_manager_check_interval
        self.task_manager_prompt_name = config.monitor.task_manager_prompt_name
        self.assistant_prompt_name = config.monitor.assistant_prompt_name
        # 延迟导入避免循环依赖
        self._task_db = None
        self._task_manager = None

    async def start(self) -> None:
        """启动监控"""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info(f"📬 Inbox监控已启动 (每{self.check_interval}秒检查一次)")

    async def stop(self) -> None:
        """停止监控"""
        if not self._running:
            return

        self._running = False

        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("📬 Inbox监控已停止")

    @property
    def is_running(self) -> bool:
        """是否正在运行"""
        return self._running

    async def check_manager(self) -> None:
        """鹏🐣-忠实助手定时检查循环"""
        # 延迟导入避免循环依赖
        if self._task_db is None:
            from peng.task.database import TaskDatabase

            self._task_db = TaskDatabase()
        if self._task_manager is None:
            from peng.task.manager import TaskManager

            self._task_manager = TaskManager()

        try:
            # 查找prompt字段为task_manager_prompt_name的事务
            task_id = 1
            task = self._task_manager.get_task(task_id=task_id)
            workdir = str(project_root_path)
            tm = self._task_manager
            if not task:
                from peng.config import get_agent_name, get_master_name

                mm = self.manager
                content = f"我是{get_master_name()}的忠实助手，随时待命..."
                msg_id, _ = mm.db.create_message(
                    content=content,
                    sender=get_agent_name(),
                    receiver=get_agent_name(),
                    caller="inbox_monitor",
                    status="done",
                    id=1,
                )
                if msg_id:
                    task_id = tm.create_task(
                        message_id=msg_id,
                        content=f"{get_master_name()}的忠实助手待命中...",
                        prompt=self.assistant_prompt_name,
                        workdir=workdir,
                        status="completed",
                        id=1,
                    )
                    if task_id:
                        subtask_id = tm.create_task(
                            parent_task_id=task_id,
                            content=f"@timer={self.task_manager_check_interval} 🦉事务总管，系统定时运行，监控所有根事务状态，发现异常立即处理。",
                            prompt=self.task_manager_prompt_name,
                            workdir=workdir,
                            id=2,
                        )
                        if subtask_id:
                            logger.info(
                                f"✅ 已创建鹏🐣-忠实助手事务总管事务 #{task_id} 和子事务 #{subtask_id}"
                            )
            elif not tm.get_task(task_id=2):
                tm.create_task(
                    parent_task_id=1,
                    content=f"@timer={self.task_manager_check_interval} 🦉事务总管，系统定时运行，监控所有根事务状态，发现异常立即处理。",
                    prompt=self.task_manager_prompt_name,
                    workdir=workdir,
                    id=2,
                )
        except Exception as e:
            logger.error(f"鹏🐣-忠实助手事务总管异常: {e}\n{traceback.format_exc()}", exc_info=True)

    async def _monitor_loop(self) -> None:
        """监控循环"""
        while self._running:
            try:
                # 检查/创建 鹏🐣-忠实助手
                await self.check_manager()
                # 检查是否有新消息（发给{AGENT_NAME}的消息，包括回复消息）
                unread_messages = self.db.get_unread()
                if unread_messages:
                    # 【优化】时间窗口去重：过滤掉1分钟内已处理过的消息
                    current_time = time.time()
                    time_window = 60  # 1分钟
                    messages_to_process = []
                    messages_skipped = 0

                    for msg in unread_messages:
                        msg_id = msg.get("id")
                        if msg_id:
                            last_time = self._last_processed.get(msg_id, 0)
                            if current_time - last_time < time_window:
                                messages_skipped += 1
                            else:
                                messages_to_process.append(msg)

                    if messages_to_process:
                        logger.info(
                            f"📬 发现 {len(unread_messages)} 条新消息（{messages_skipped} 条在1分钟内已处理，实际处理 {len(messages_to_process)} 条）"
                        )

                    # 如果所有消息都在时间窗口内，静默跳过（但必须 sleep 避免忙等待）
                    if not messages_to_process:
                        await asyncio.sleep(self.check_interval)
                        continue

                    # 避免并发数过多，每次循环只处理一条消息
                    msg = messages_to_process[0]
                    msg_id = msg.get("id")
                    reply_to = msg.get("reply_to")
                    sender = msg.get("sender")
                    receiver = msg.get("receiver")
                    content = msg.get("content")
                    if receiver == AGENT_NAME:
                        content = f"\n{ANSI_COLORS['blue']}{ANSI_COLORS['bold']}{sender} ：{ANSI_COLORS['green']}{content}{ANSI_COLORS['reset']}\n"
                    else:
                        content = f"\n{ANSI_COLORS['green']}{ANSI_COLORS['bold']}{sender} ：{ANSI_COLORS['blue']}{content}{ANSI_COLORS['reset']}\n"
                    # 把无主回复消息转为根消息
                    if msg_id:
                        try:
                            if reply_to:
                                chain_head = self.manager.get_chain_head(msg_id)
                                # 回复消息：创建控制命令，让心跳监控执行
                                if chain_head:
                                    from peng.task.output_manager import get_output_manager

                                    chain_head_id = chain_head.get("id")
                                    task_id = self.manager.get_task_id(msg_id=chain_head_id)
                                    get_output_manager().write_output(
                                        task_id=task_id,
                                        source=sender,
                                        content=content,
                                    )
                                    if receiver == AGENT_NAME:
                                        result = await self.manager.handle_reply(
                                            msg_id=msg_id,
                                        )
                                        if result.get("success"):
                                            logger.info(f"✅ 回复消息 #{msg_id} 已交给处理模块")
                                        else:
                                            logger.warning(
                                                f"⚠️ 消息 #{msg_id} 回复处理失败: {result.get('error')}"
                                            )
                                    else:
                                        self.manager.update_message_status(msg_id, "read")

                                    # 📱 飞书回复自动转发：鹏🐣的回复消息，如果链首来自飞书，自动通过飞书reply API回复
                                    # 注意：这个逻辑必须独立于 receiver 判断，因为鹏🐣回复消息的 receiver 是master
                                    if sender == AGENT_NAME and chain_head:
                                        await self._forward_to_feishu_if_needed(msg, chain_head)
                                        # 📱 微信回复自动转发：链首来自微信，自动通过iLink sendMessage API回复
                                        await self._forward_to_wechat_if_needed(msg, chain_head)
                                else:
                                    logger.warning(f"⚠️ 无法获取回复消息 #{msg_id} 的链首，删除之")
                                    self.manager.delete_message(msg_id)

                            elif receiver == AGENT_NAME:
                                # 根消息：调用handle_new_message处理
                                result = await self.manager.handle_new_message(
                                    msg_id=msg_id,
                                )
                                if result.get("success"):
                                    logger.warning(f"✅ 新消息 #{msg_id} 已交给处理模块")
                                    from peng.task.output_manager import get_output_manager

                                    task_id = self.manager.get_task_id(msg_id=msg_id)
                                    get_output_manager().write_output(
                                        task_id=task_id,
                                        source=sender,
                                        content=content,
                                    )
                                else:
                                    logger.warning(
                                        f"⚠️ 消息 #{msg_id} 处理失败: {result.get('error')}"
                                    )

                            # 通知前端刷新消息列表（inbox_monitor运行在webui server的async事件循环中）
                            # 跳过webui来源的消息（webui api创建时已广播过，避免双重广播）
                            msg_caller = msg.get("caller")
                            if msg_caller != "webui":
                                try:
                                    from peng.webui.websocket import broadcast_message_update

                                    await broadcast_message_update(
                                        msg_id, "created", {}, source=msg_caller or "inbox_monitor"
                                    )
                                except Exception as e:
                                    logger.debug(f"广播消息更新失败(可忽略): {e}")

                            # 【优化】记录处理时间（无论成功失败）
                            self._last_processed[msg_id] = time.time()
                        except Exception as e:
                            logger.error(f"处理消息 #{msg_id} 异常: {e}\n{traceback.format_exc()}")
                            # 【优化】即使异常也记录时间，避免频繁重试失败消息
                            self._last_processed[msg_id] = time.time()

            except Exception as e:
                logger.error(f"监控循环异常: {e}\n{traceback.format_exc()}", exc_info=True)

            await asyncio.sleep(self.check_interval)

    def check_now(self) -> list[dict[str, Any]]:
        """
        立即检查新消息（同步方法）

        Returns:
            未读消息列表
        """
        return self.db.get_unread()

    def has_unread(self) -> bool:
        """
        检查是否有未读消息

        Returns:
            是否有未读消息
        """
        return len(self.db.get_unread()) > 0

    async def _forward_to_feishu_if_needed(
        self, msg: dict[str, Any], chain_head: dict[str, Any]
    ) -> None:
        """检查是否需要将回复消息转发到飞书

        规则：只对根消息caller来源为feishu:的需要转换回复。
        找到被回复消息的platform_msg_id，通过飞书reply API回复。
        同时检查链首消息是否包含 @get:<file> 或 @getall 指令，发送附件。

        Args:
            msg: 当前回复消息
            chain_head: 链首消息（根消息）
        """
        try:
            chain_head_caller = chain_head.get("caller", "")
            if not chain_head_caller.startswith("feishu:"):
                return  # 非飞书来源，不转发

            # 找到被回复消息的 platform_msg_id
            reply_to_id = msg.get("reply_to")
            if not reply_to_id:
                return

            replied_msg = self.db.get_message(reply_to_id)
            if not replied_msg:
                logger.warning(f"飞书转发：找不到被回复消息#{reply_to_id}")
                return

            feishu_msg_id = replied_msg.get("platform_msg_id", "")
            if not feishu_msg_id:
                logger.warning(f"飞书转发：被回复消息#{reply_to_id}无platform_msg_id")
                return

            # ====== @get/@getall 附件发送 ======
            await self._send_channel_attachments(
                chain_head=chain_head,
                channel_type="feishu",
                target_id=feishu_msg_id,
            )

            # 发送飞书文本回复
            content = msg.get("content", "")
            from peng.channels.integration import send_feishu_reply

            new_feishu_msg_id = await send_feishu_reply(feishu_msg_id, content)
            if new_feishu_msg_id:
                # 将飞书返回的新消息ID存储为回复消息的 platform_msg_id
                # 这样用户在飞书中回复鹏🐣的消息时，能正确映射到消息树
                msg_id = msg.get("id")
                if msg_id:
                    self.db.update_message(msg_id, platform_msg_id=new_feishu_msg_id)
                    logger.info(
                        f"📱 飞书回复转发成功: msg_tree_id={msg_id} → feishu_msg={feishu_msg_id}, "
                        f"new_platform_msg_id={new_feishu_msg_id}"
                    )
                else:
                    logger.info(f"📱 飞书回复转发成功: reply_to_feishu_msg={feishu_msg_id}")
            else:
                logger.warning(
                    f"📱 飞书回复转发失败: msg_tree_id={msg.get('id')} → feishu_msg={feishu_msg_id}"
                )
        except Exception as e:
            logger.error(f"飞书回复转发异常: {e}", exc_info=True)

    async def _forward_to_wechat_if_needed(
        self, msg: dict[str, Any], chain_head: dict[str, Any]
    ) -> None:
        """检查是否需要将回复消息转发到微信

        规则：链首caller来源为 wechat: 的需要转发回复。
        通过 iLink sendMessage API 发送文本回复给对应用户。
        同时检查链首消息是否包含 @get:<file> 或 @getall 指令，发送附件。

        Args:
            msg: 当前回复消息
            chain_head: 链首消息（根消息）
        """
        try:
            chain_head_caller = chain_head.get("caller", "")
            if not chain_head_caller.startswith("wechat:"):
                return  # 非微信来源，不转发

            # 找到被回复消息，获取微信用户ID（从 caller 提取）
            reply_to_id = msg.get("reply_to")
            if not reply_to_id:
                return

            # 获取微信用户ID：从链首 caller 中提取
            wechat_user_id = chain_head_caller[len("wechat:") :]
            if not wechat_user_id:
                logger.warning(f"微信转发：无法从 caller={chain_head_caller} 提取 user_id")
                return

            # ====== @get/@getall 附件发送 ======
            await self._send_channel_attachments(
                chain_head=chain_head,
                channel_type="wechat",
                target_id=wechat_user_id,
            )

            content = msg.get("content", "")
            if not content:
                return

            from peng.channels.integration import send_wechat_reply

            result = await send_wechat_reply(
                to_user_id=wechat_user_id,
                content=content,
            )
            if result:
                logger.info(
                    f"📱 微信回复转发成功: msg_tree_id={msg.get('id')} → wechat_user={wechat_user_id}"
                )
            else:
                logger.warning(
                    f"📱 微信回复转发失败: msg_tree_id={msg.get('id')} → wechat_user={wechat_user_id}"
                )
        except Exception as e:
            logger.error(f"微信回复转发异常: {e}", exc_info=True)

    async def _send_channel_attachments(
        self,
        chain_head: dict[str, Any],
        channel_type: str,
        target_id: str,
    ) -> None:
        """检查消息树中是否包含 @get/@getall 指令，发送附件

        规则：
        1. 扫描消息树中所有消息（不仅是链首），检查是否包含 @get: 或 @getall
        2. 消息树变化（新回复）时重新检测所有有效文件路径
        3. 避免重复发送：检查消息树中是否已有相同附件的已发送记录

        Args:
            chain_head: 链首消息（根消息）
            channel_type: "feishu" 或 "wechat"
            target_id: 飞书消息ID 或 微信用户ID
        """
        try:
            chain_head_id = chain_head.get("id")
            if not chain_head_id:
                return

            # 获取完整回复链
            chain = self.db.get_reply_chain(chain_head_id)
            if not chain:
                return

            # 1. 检查消息树中是否有 @get: 或 @getall 指令
            # 收集所有含 @get:/@getall 的消息的 content
            get_file_paths: list[str] = []  # 来自 @get:<file> 的明确路径
            has_getall = False

            for chain_msg in chain:
                content = chain_msg.get("content", "")
                if not content:
                    continue

                # 检查 @get:<file>
                if "@get:" in content:
                    import re

                    get_pattern = re.compile(r"@get:(\S+)")
                    for match in get_pattern.findall(content):
                        path = match.rstrip(",.;，。；")
                        if path:
                            get_file_paths.append(path)

                # 检查 @getall
                if "@getall" in content:
                    has_getall = True

            # 无任何附件指令
            if not get_file_paths and not has_getall:
                return

            # 2. 收集 @getall 需要的文件路径
            all_file_paths = list(get_file_paths)  # 先放明确的 @get 路径

            if has_getall:
                from peng.channels.integration import _extract_file_paths_from_context

                # 先重建 chain_context 确保包含最新回复
                self.db.build_context_history(chain_head_id)
                # 重新获取 chain_head 以拿到更新的 chain_context
                updated_head = self.db.get_message(chain_head_id)
                chain_context = (updated_head or chain_head).get("chain_context", "")

                # 从 chain_context 提取文件路径
                context_paths = _extract_file_paths_from_context(chain_context)
                all_file_paths.extend(context_paths)

                # 同时直接从回复链的消息内容中提取文件路径（双重保障）
                # 回复链中可能包含截屏、文件等路径信息
                for chain_msg in chain:
                    msg_content = chain_msg.get("content", "")
                    if msg_content:
                        direct_paths = _extract_file_paths_from_context(msg_content)
                        all_file_paths.extend(direct_paths)

            # 去重
            seen = set()
            unique_paths = []
            for p in all_file_paths:
                if p not in seen:
                    seen.add(p)
                    unique_paths.append(p)

            if not unique_paths:
                logger.info("消息树包含 @get/@getall 但未解析出文件路径")
                return

            # 3. 检查已发送的附件（避免重复）
            already_sent = self._get_already_sent_files(chain)
            new_paths = [p for p in unique_paths if p not in already_sent]

            if not new_paths:
                logger.info(
                    f"📎 所有文件均已发送过，跳过（共 {len(unique_paths)} 个，已发送 {len(already_sent)} 个）"
                )
                return

            logger.info(
                f"📎 消息树包含 @get/@getall，需要发送 {len(new_paths)} 个新文件"
                f"（总计 {len(unique_paths)}，已发送 {len(already_sent)}）: {new_paths}"
            )

            # 4. 获取 nest 和 workdir
            nest_name = chain_head.get("nest", "")
            workdir = chain_head.get("workdir", "")

            from peng.channels.integration import (
                nest_download_file,
                send_feishu_attachments,
                send_wechat_attachments,
            )

            # 5. 从 nest 下载文件到本地
            local_paths = []
            for fp in new_paths:
                local_path = await nest_download_file(nest_name or None, fp, workdir or None)
                if local_path:
                    local_paths.append((fp, local_path))
                else:
                    logger.warning(f"📎 文件下载失败，跳过: {fp}")

            if not local_paths:
                logger.warning("所有文件下载失败，无附件可发送")
                return

            # 6. 发送附件
            success_files = []  # 记录成功发送的原始路径
            local_only = [lp for _, lp in local_paths]

            if channel_type == "feishu":
                count = await send_feishu_attachments(target_id, local_only)
                # 记录成功发送的文件（简单处理：全部记录，失败的会在日志中体现）
                if count > 0:
                    success_files = [fp for fp, _ in local_paths]
            elif channel_type == "wechat":
                count = await send_wechat_attachments(target_id, local_only)
                if count > 0:
                    success_files = [fp for fp, _ in local_paths]

            logger.info(f"📎 {channel_type} 附件发送完成: {count}/{len(local_paths)} 成功")

            # 7. 记录已发送的附件到消息树（避免重复发送）
            if success_files:
                self._record_sent_attachments(chain_head_id, success_files)

        except Exception as e:
            logger.error(f"发送通道附件异常: {e}", exc_info=True)

    def _get_already_sent_files(self, chain: list[dict[str, Any]]) -> set[str]:
        """从消息树中获取已发送的附件文件路径集合

        通过检查消息树中已有的「📎 已发送附件」标记消息来判断。

        Args:
            chain: 完整回复链

        Returns:
            已发送的文件路径集合
        """
        sent_files = set()
        for msg in chain:
            content = msg.get("content", "")
            # 检查附件发送记录标记
            if content.startswith("📎 已发送附件:"):
                # 格式: "📎 已发送附件: /path/to/file1.txt, /path/to/file2.pdf"
                paths_str = content[len("📎 已发送附件:") :].strip()
                for p in paths_str.split(", "):
                    p = p.strip()
                    if p:
                        sent_files.add(p)
        return sent_files

    def _record_sent_attachments(self, chain_head_id: int, file_paths: list[str]) -> None:
        """记录已发送的附件到消息树

        在消息树中添加一条系统记录消息，标记已发送的文件。
        使用 status="done" 避免触发 inbox_monitor 处理。

        Args:
            chain_head_id: 链首消息ID
            file_paths: 已发送的文件路径列表
        """
        try:
            from peng.config import get_agent_name

            agent_name = get_agent_name()

            content = "📎 已发送附件: " + ", ".join(file_paths)

            msg_id, _ = self.db.create_message(
                content=content,
                sender=agent_name,
                receiver=agent_name,
                caller="attachment_tracker",
                reply_to=chain_head_id,
                status="done",
            )

            if msg_id > 0:
                logger.info(f"📎 附件发送记录已保存: #{msg_id}, files={file_paths}")
            else:
                logger.warning(f"附件发送记录保存失败: files={file_paths}")
        except Exception as e:
            logger.error(f"记录附件发送信息异常: {e}", exc_info=True)

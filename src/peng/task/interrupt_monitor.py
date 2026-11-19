"""事务中断监控（共享协程）

长时间等待/执行期间（bash 命令进程、wait 工具等待）轮询中断条件，
让风主🍃的指示与控制信号能立刻被响应而不是等工具自然结束：

1. 事务状态变更：不再是 running/accepting（cancel/pause 等系统级控制必须立刻响应）
2. info 注入：紧急（感叹号开头）未读 info；check_all=True 时任何未读 info
3. 根消息新回复：紧急（感叹号开头）processing 回复；check_all=True 时任何新回复

共享原因（peng/task/info.py:is_urgent_content 文档同样强调"不要写两份"）：
- src/peng/tools/bash.py 的 _interrupt_monitor（命令进程中断）
- src/peng/tools/task.py 的 WaitTool（等待中断）

所有同步 DB 查询经 asyncio.to_thread + wait_for(5s) 执行——DB 瞬时卡顿
（锁/网络）不再阻塞事件循环导致监控协程僵死（事务#2064：命令124秒未打断
事故的防御性根治：无论 DB 状态如何，监控循环永不卡死）。
"""

import asyncio
import logging
from typing import Any

from peng.task.info import get_unread_info_items, is_urgent_content, parse_info_to_list_with_status

logger = logging.getLogger(__name__)


async def _db_call(func, *args, task_id: int | None = None) -> Any:
    """同步DB查询线程化+5秒超时，防止卡死监控/事件循环"""
    try:
        return await asyncio.wait_for(asyncio.to_thread(func, *args), timeout=5.0)
    except TimeoutError:
        logger.warning(
            f"[interrupt_monitor] 事务#{task_id}DB查询超时5s(跳过本轮): {func.__name__}"
        )
        return None
    except Exception as e:
        logger.debug(f"[interrupt_monitor] 事务#{task_id}DB查询失败: {e}")
        return None


async def monitor_interrupts(
    task_manager: Any,
    task_id: int | None,
    message_id: int | None,
    check_all: bool = False,
    label: str = "执行",
) -> str | None:
    """中断监控协程：定期检查事务状态/info/根消息回复

    Args:
        task_manager: 事务管理器（None则只检查消息回复）
        task_id: 当前事务ID
        message_id: 当前根消息ID（None则只检查事务状态/info）
        check_all: 是否检查全部中断条件。True=任何未读info/任何新回复都打断
            （等待类操作语义：wait 工具/含sleep的命令，用户在等待时应能随时介入）；
            False=仅事务状态变更 + 感叹号开头的紧急未读info/新回复（事务#1860），
            普通消息/info不打断（与思考流打断策略一致）
        label: 日志中的被监控对象描述（"命令"/"wait等待"）

    Returns:
        中断原因字符串，None表示未中断（协程持续运行直到被cancel）
    """
    _last_known_reply_count = -1
    # 🔧 事务#2064：检测间隔2秒→1秒（打断更及时）
    check_interval = 1
    logger.info(
        f"[interrupt_monitor] 事务#{task_id}{label}中断监控已启动"
        f"(interval={check_interval}s, check_all={check_all})"
    )

    while True:
        await asyncio.sleep(check_interval)

        # 条件1: 事务状态不再是 running/accepting（所有启用监控的操作都检查：
        # cancel/pause等系统级控制必须立即响应，覆盖所有场景）
        task = None
        if task_manager and task_id:
            task = await _db_call(task_manager.db.get_task, task_id, task_id=task_id)
            if task:
                status = task.get("status")
                if status not in ("running", "accepting"):
                    status_cn = task_manager.STATUS_CHINESE.get(status, status)
                    return (
                        f"事务已被{status_cn}（状态: {status}），{label}已终止。"
                        "请立刻处理事务状态变更。"
                    )

            # 条件2: info字段检查（优先级最高——用户紧急指示）
            # check_all（等待类操作，原有bash含sleep命令行为）：任何未读info都打断
            # 否则（事务#1860）：仅感叹号开头的紧急未读info打断
            try:
                if task is None:
                    info_field = "[]"
                else:
                    info_field = task.get("info", "[]") or "[]"
                info_list = parse_info_to_list_with_status(info_field)
                unread_items = get_unread_info_items(info_list)
                if check_all:
                    if unread_items:
                        info_contents = [item["content"] for item in unread_items]
                        info_text = "\n".join(f"  - {c}" for c in info_contents)
                        return f"收到info注入信息，{label}已终止。请立刻处理：\n{info_text}"
                else:
                    for item in unread_items:
                        if is_urgent_content(item.get("content")):
                            content = (item.get("content") or "").replace("\\n", "\n")
                            return (
                                f"收到感叹号开头的紧急info，{label}已终止。请立刻处理：\n"
                                f"  - {content.strip()[:200]}"
                            )
            except Exception as e:
                logger.debug(f"[interrupt_monitor] 检查info字段失败: {e}")

        # 条件3: 根消息有新回复
        if message_id:
            if check_all:
                # 原有行为：回复计数增加即打断
                try:
                    from peng.message.database import MessageDatabase

                    msg_db = MessageDatabase()
                    reply_count = await _db_call(msg_db.get_reply_count, message_id)
                    if (
                        reply_count is not None
                        and _last_known_reply_count >= 0
                        and reply_count > _last_known_reply_count
                    ):
                        return (
                            f"根消息#{message_id}收到新回复（当前{reply_count}条），"
                            f"{label}已终止。请立刻用message工具查看并处理。"
                        )
                    if reply_count is not None:
                        _last_known_reply_count = reply_count
                except Exception as e:
                    logger.debug(f"[interrupt_monitor] 检查消息回复失败: {e}")
            else:
                # 事务#1860：仅感叹号开头的紧急processing回复打断
                # （processing=未处理待注入，与 tool_engine._get_processing_messages
                #  消费语义一致，处理后标记done不会重复触发）
                try:
                    from peng.config import get_agent_name
                    from peng.message.database import MessageDatabase

                    msg_db = MessageDatabase()
                    replies = await _db_call(
                        msg_db.get_processing_replies, message_id, get_agent_name()
                    )
                    for reply in replies or []:
                        if is_urgent_content(reply.get("content")):
                            content = (reply.get("content") or "").replace("\\n", "\n")
                            return (
                                f"根消息#{message_id}收到感叹号开头的紧急消息"
                                f"#{reply.get('id')} [{reply.get('sender')}]，"
                                f"{label}已终止。请立刻处理：{content.strip()[:200]}"
                            )
                except Exception as e:
                    logger.debug(f"[interrupt_monitor] 检查紧急消息回复失败: {e}")

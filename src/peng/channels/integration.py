"""
Channel 集成模块

将Channel消息通道与MessageManager集成，实现：
1. Channel收到的消息 → 写入MessageDatabase → 由inbox_monitor检测处理
2. 飞书来源消息的回复 → 自动通过飞书reply API回复
3. 微信来源消息的回复 → 自动通过iLink sendMessage API回复

消息来源标识（caller字段）：
- 飞书: "feishu:{open_id}"
- 微信: "wechat:{ilink_user_id}"

消息树映射规则：
- 飞书: root_id/parent_id → reply_to（飞书自带回复结构）
- 微信: ref_msg.message_item.msg_id → 通过 platform_msg_id 查找 reply_to（微信引用功能）
  无引用时视为新根消息
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
import uuid
from typing import Any

from peng.channels.base import ChannelMessage, ChannelType
from peng.channels.clawbot_wechat import ClawBotWechatChannel
from peng.channels.feishu import FeishuChannel
from peng.channels.manager import ChannelManager

logger = logging.getLogger(__name__)

__all__ = [
    "init_channel_system",
    "get_channel_manager",
    "handle_channel_message",
    "send_feishu_reply",
    "send_wechat_reply",
    "is_feishu_source",
    "is_wechat_source",
    "channel_message_to_message_record",
]

# 全局ChannelManager实例
_channel_manager: ChannelManager | None = None


def init_channel_system(config: dict[str, Any]) -> ChannelManager:
    """初始化Channel系统

    根据配置创建并注册启用的Channel。
    同时注册消息回调，将Channel消息自动转为消息树消息。

    Args:
        config: peng.json 配置中的 channels 节点

    Returns:
        ChannelManager 实例
    """
    global _channel_manager

    _channel_manager = ChannelManager()

    if not config.get("enabled", False):
        logger.info("Channel系统未启用（channels.enabled=false）")
        return _channel_manager

    # 注册飞书Channel
    feishu_config = config.get("feishu", {})
    if feishu_config.get("enabled", False):
        if feishu_config.get("app_id") and feishu_config.get("app_secret"):
            feishu_channel = FeishuChannel(feishu_config)
            _channel_manager.register_channel(feishu_channel)
            logger.info("✅ 飞书Channel已注册")
        else:
            logger.warning("⚠️ 飞书Channel已启用但缺少 app_id/app_secret 配置")

    # 注册微信Channel (ClawBot iLink)
    wechat_config = config.get("wechat", {})
    if wechat_config.get("enabled", False):
        wechat_channel = ClawBotWechatChannel(wechat_config)
        _channel_manager.register_channel(wechat_channel)
        logger.info("✅ 微信Channel已注册（ClawBot）")

    # 注册消息回调：Channel消息 → 消息树
    _channel_manager.set_message_callback(_on_channel_message)

    logger.info(
        f"Channel系统初始化完成，已注册 {len(_channel_manager._channels)} 个通道"
    )
    return _channel_manager


def get_channel_manager() -> ChannelManager | None:
    """获取全局ChannelManager实例"""
    return _channel_manager


def _on_channel_message(message: ChannelMessage) -> None:
    """ChannelManager的消息回调：将Channel消息转为消息树消息

    由ChannelManager在收到任何Channel消息时调用。
    """
    try:
        handle_channel_message(message)
    except Exception as e:
        logger.error(f"处理Channel消息失败: {e}", exc_info=True)


def _find_windows_nest() -> tuple[str | None, str | None]:
    """查找已连接的 Windows 平台 nest，返回 (nest_name, workdir)

    如果找到 Windows 平台的 nest 连接：
    - nest_name: 该 nest 的 display_key
    - workdir: 固定为 D:\\downloads

    Returns:
        (nest_name, workdir) 或 (None, None)
    """
    try:
        from peng.nest.bridge import NestBridgeManager
        manager = NestBridgeManager.get_instance()
        if not manager:
            return None, None

        for conn_info in manager.list_connections():
            if conn_info.get("platform") == "Windows":
                nest_name = conn_info["display_key"]
                workdir = r"D:\downloads"
                logger.info(
                    f"飞书新消息自动设置 Windows nest: nest={nest_name}, workdir={workdir}"
                )
                return nest_name, workdir
    except Exception as e:
        logger.warning(f"查找 Windows nest 失败: {e}")

    return None, None


def _find_wechat_reply_by_ref_text(
    db: Any, caller: str, ref_text: str
) -> int | None:
    """通过引用文本内容查找被引用的消息

    策略：在 caller 为微信的根消息的 chain_context 中搜索引用文本，
    找到包含该文本的**具体消息**（而非根消息），返回该具体消息的ID。

    chain_context 格式（每行一条消息）：
        2025-03-23 10:00:00 #1  master\t消息内容
        2025-03-23 10:01:00 #2 → #1 鹏🐣\t回复内容

    iLink API 的 ref_msg.message_item 经常不含 msg_id，
    但包含被引用消息的文本摘要（ref_msg.message_item.text_item.text）。

    Args:
        db: MessageDatabase 实例
        caller: 当前消息的 caller（如 wechat:xxx）
        ref_text: 被引用消息的文本摘要（来自 ref_msg_text）

    Returns:
        匹配到的具体消息ID（优先返回被引用的消息，而非根消息），未找到返回 None
    """
    if not ref_text:
        return None
    search_text = ref_text.strip()
    if not search_text:
        return None
    try:
        from peng.db.connection import get_connection, row_to_dict
        with get_connection() as conn:
            # 遍历 caller 为微信的所有根消息（reply_to IS NULL），检查其 chain_context
            cursor = conn.execute(
                "SELECT id, chain_context FROM messages "
                "WHERE caller = %s AND reply_to IS NULL "
                "ORDER BY id DESC",
                (caller,),
            )
            for row in cursor.fetchall():
                d = row_to_dict(row)
                root_id = d["id"]
                chain_context = d.get("chain_context", "") or ""
                if not chain_context or search_text not in chain_context:
                    continue

                # 找到了匹配的 chain_context，现在定位具体是哪条消息
                # 从 chain_context 的最后一行往前搜索，找到包含 ref_text 的最近一条消息
                best_match_id = None
                for line in reversed(chain_context.split("\n")):
                    if not line.strip():
                        continue
                    if search_text in line:
                        # 解析消息ID：格式为 "... #123 ..." 或 "... #123→#456 ..."
                        import re
                        id_match = re.search(r'#(\d+)', line)
                        if id_match:
                            best_match_id = int(id_match.group(1))
                        break

                if best_match_id:
                    logger.info(
                        f"微信引用文本匹配成功: matched_id={best_match_id}, "
                        f"root_id={root_id}, ref_text={search_text[:50]}"
                    )
                    return best_match_id

                # 如果无法从 chain_context 解析具体消息ID，回退到根消息
                logger.info(
                    f"微信引用文本匹配成功(回退到根消息): root_id={root_id}, "
                    f"ref_text={search_text[:50]}"
                )
                return root_id

            logger.debug(
                f"微信引用文本匹配: 所有根消息中未找到匹配, "
                f"ref_text={search_text[:80]}"
            )
    except Exception as e:
        logger.warning(f"微信引用文本匹配失败: {e}")
    return None


def _find_wechat_reply_by_session(
    db: Any, caller: str, current_platform_msg_id: str, session_id: str
) -> int | None:
    """通过 session_id 查找同一会话中的最近消息作为回复目标

    微信 iLink 的 session_id 在同一对话会话中保持不变，
    可以用来关联属于同一对话的消息。

    Args:
        db: MessageDatabase 实例
        caller: 当前消息的 caller（如 wechat:xxx）
        current_platform_msg_id: 当前消息的 platform_msg_id，避免匹配自己
        session_id: 微信会话 ID

    Returns:
        匹配到的消息ID，未找到返回 None
    """
    try:
        from peng.db.connection import get_connection, row_to_dict
        with get_connection() as conn:
            # 优先通过 caller_session_id 列匹配
            cursor = conn.execute(
                "SELECT id FROM messages "
                "WHERE caller = %s AND caller_session_id = %s AND platform_msg_id != %s "
                "ORDER BY id DESC LIMIT 1",
                (caller, session_id, current_platform_msg_id),
            )
            row = cursor.fetchone()
            if row:
                d = row_to_dict(row)
                return d["id"]
    except Exception as e:
        logger.warning(f"微信 session 匹配失败: {e}")
    return None


def _find_wechat_recent_root(db: Any, caller: str) -> int | None:
    """查找同一微信用户最近的根消息作为回复目标

    兜底策略：当 ref_msg_id 和 session_id 都无法匹配时，
    将新消息作为同一用户最近根消息的回复。

    Args:
        db: MessageDatabase 实例
        caller: 当前消息的 caller（如 wechat:xxx）

    Returns:
        匹配到的消息ID，未找到返回 None
    """
    try:
        from peng.db.connection import get_connection, row_to_dict
        with get_connection() as conn:
            # 查找同一 caller 最近的根消息（reply_to IS NULL）
            cursor = conn.execute(
                "SELECT id FROM messages "
                "WHERE caller = %s AND reply_to IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (caller,),
            )
            row = cursor.fetchone()
            if row:
                d = row_to_dict(row)
                return d["id"]
    except Exception as e:
        logger.warning(f"微信用户最近消息匹配失败: {e}")
    return None


def _find_wechat_reply_by_ref_time(
    db: Any, caller: str, ref_create_time_ms: int, tolerance_seconds: int = 10
) -> int | None:
    """通过引用消息的 create_time_ms 查找被引用的消息

    当 iLink API 返回 ref_msg 但不含 msg_id 时（常见于引用图片/文件消息），
    可以通过引用消息的创建时间来匹配。

    Args:
        db: MessageDatabase 实例
        caller: 当前消息的 caller（如 wechat:xxx）
        ref_create_time_ms: 被引用消息的 create_time_ms（iLink ref_msg.message_item.create_time_ms）
        tolerance_seconds: 时间容差（秒），默认10秒

    Returns:
        匹配到的消息ID（根消息），未找到返回 None
    """
    if not ref_create_time_ms:
        return None
    try:
        from peng.db.connection import get_connection, row_to_dict
        from datetime import datetime, timedelta

        # iLink 的 create_time_ms 是毫秒级时间戳
        ref_time = datetime.fromtimestamp(ref_create_time_ms / 1000.0)
        time_start = ref_time - timedelta(seconds=tolerance_seconds)
        time_end = ref_time + timedelta(seconds=tolerance_seconds)
        time_start_str = time_start.strftime("%Y-%m-%d %H:%M:%S")
        time_end_str = time_end.strftime("%Y-%m-%d %H:%M:%S")

        with get_connection() as conn:
            # 查找同一 caller 时间范围内的根消息
            cursor = conn.execute(
                "SELECT id, timestamp FROM messages "
                "WHERE caller = %s AND reply_to IS NULL "
                "AND timestamp >= %s AND timestamp <= %s "
                "ORDER BY id DESC LIMIT 1",
                (caller, time_start_str, time_end_str),
            )
            row = cursor.fetchone()
            if row:
                d = row_to_dict(row)
                logger.info(
                    f"微信引用时间匹配: ref_time={ref_time}, matched_id={d['id']}, "
                    f"msg_time={d.get('timestamp')}"
                )
                return d["id"]
    except Exception as e:
        logger.warning(f"微信引用时间匹配失败: {e}")
    return None
def handle_channel_message(channel_msg: ChannelMessage) -> int | None:
    """将ChannelMessage转为消息树消息

    核心转换逻辑：
    - 飞书新消息（无root_id） → 创建消息树根消息
    - 飞书回复消息（有root_id/parent_id） → 通过root_id查找对应消息树消息，创建回复
    - 微信消息 → 查找同一用户最近未完成的消息，作为回复或新根消息

    Args:
        channel_msg: Channel消息

    Returns:
        创建的消息ID，失败返回None
    """
    from peng.message.database import MessageDatabase
    from peng.config import get_master_name, get_agent_name

    db = MessageDatabase()
    master_name = get_master_name()
    agent_name = get_agent_name()

    # 提取平台消息信息
    platform_msg_id = channel_msg.platform_msg_id
    sender_id = channel_msg.sender_id
    content = channel_msg.content
    is_group = channel_msg.is_group
    raw_data = channel_msg.raw_data or {}

    # 构建caller标识
    if channel_msg.channel_type == ChannelType.WECHAT:
        caller = f"wechat:{sender_id}"
    else:
        caller = f"feishu:{sender_id}"

    reply_to_msg_id = None

    if channel_msg.channel_type == ChannelType.FEISHU:
        # ===== 飞书消息：通过 root_id/parent_id 识别回复 =====
        feishu_chat_id = channel_msg.receiver_id  # chat_id
        root_id = raw_data.get("root_id", "")
        parent_id = raw_data.get("parent_id", "")
        logger.info("飞书回复检测: parent_id=%s, root_id=%s" % (parent_id, root_id))

        # 优先用 parent_id（直接被回复的消息），找不到时回退到 root_id（线程根消息）
        for feishu_reply_id in [parent_id, root_id]:
            if not feishu_reply_id:
                continue
            existing = db.find_by_platform_msg_id(feishu_reply_id)
            if existing:
                reply_to_msg_id = existing["id"]
                logger.info(
                    "飞书回复消息映射: feishu_msg=%s → msg_tree_id=%s" % (feishu_reply_id, reply_to_msg_id)
                )
                break
        else:
            if parent_id or root_id:
                logger.warning(
                    "飞书回复消息的原始消息未找到: parent_id=%s, root_id=%s，作为新消息处理"
                    % (parent_id, root_id)
                )

        # 自动检测 Windows 平台 nest（仅对飞书新消息）
        nest_name = None
        workdir = None
        if not reply_to_msg_id:
            nest_name, workdir = _find_windows_nest()
    else:
        # ===== 微信消息：通过引用功能识别回复 =====
        # 规则：
        # 1. 有 ref_msg_id（iLink 返回了引用信息）→ 精确匹配
        # 2. ref_msg_text 文本匹配 → 通过引用文本内容查找
        # 3. ref_msg 存在但无 msg_id → 通过引用消息的 create_time_ms 时间匹配
        # 4. session_id 匹配 → 同一会话的消息关联
        # 5. 兜底：同一用户最近根消息
        feishu_chat_id = ""
        nest_name = None
        workdir = None

        # 方法1：通过引用消息的 msg_id 来查找回复目标
        # 微信用户引用消息时，item_list[].ref_msg.message_item.msg_id 包含被引用消息的 ID
        ref_msg_id = raw_data.get("ref_msg_id", "")
        if ref_msg_id:
            # 通过被引用消息的 msg_id（存储为 platform_msg_id）查找消息树中的消息
            existing = db.find_by_platform_msg_id(ref_msg_id)
            if existing:
                reply_to_msg_id = existing["id"]
                logger.info(
                    f"微信引用消息映射: ref_msg_id={ref_msg_id} → msg_tree_id={reply_to_msg_id}"
                )
            else:
                logger.warning(
                    f"微信引用消息的原始消息未找到: ref_msg_id={ref_msg_id}，尝试其他匹配"
                )

        # 方法2：ref_msg 存在但没有 msg_id 时，通过引用文本内容匹配
        # iLink API 的 ref_msg.message_item 经常不含 msg_id，但包含被引用消息的文本内容
        ref_msg_text = raw_data.get("ref_msg_text", "")
        if not reply_to_msg_id and ref_msg_text:
            reply_to_msg_id = _find_wechat_reply_by_ref_text(db, caller, ref_msg_text)
            if reply_to_msg_id:
                logger.info(
                    f"微信引用文本匹配: ref_text={ref_msg_text[:50]} → reply_to={reply_to_msg_id}"
                )

        # 方法3：ref_msg 存在但无 msg_id 且无 text（如引用图片消息），通过 create_time_ms 匹配
        has_ref_msg = raw_data.get("has_ref_msg", False)
        ref_msg_create_time_ms = raw_data.get("ref_msg_create_time_ms", 0)
        if not reply_to_msg_id and has_ref_msg and ref_msg_create_time_ms:
            reply_to_msg_id = _find_wechat_reply_by_ref_time(db, caller, ref_msg_create_time_ms)
            if reply_to_msg_id:
                logger.info(
                    f"微信引用时间匹配: ref_create_time_ms={ref_msg_create_time_ms} → reply_to={reply_to_msg_id}"
                )

        # 方法4：如果方法1/2/3失败，通过 session_id 匹配
        # 同一会话(session)中的消息通常属于同一个对话线程
        session_id = raw_data.get("session_id", "")
        if not reply_to_msg_id and session_id:
            reply_to_msg_id = _find_wechat_reply_by_session(db, caller, platform_msg_id, session_id)
            if reply_to_msg_id:
                logger.info(
                    f"微信session匹配: session_id={session_id}, caller={caller} → reply_to={reply_to_msg_id}"
                )

        # 方法5（兜底）：同一用户最近根消息
        # 当以上方法都失败时，将消息关联到同一用户最近的根消息
        if not reply_to_msg_id:
            reply_to_msg_id = _find_wechat_recent_root(db, caller)
            if reply_to_msg_id:
                logger.info(
                    f"微信兜底匹配(最近根消息): caller={caller} → reply_to={reply_to_msg_id}"
                )

        # 最终仍无匹配 → 作为新根消息
        if not reply_to_msg_id:
            logger.info(
                f"微信消息所有匹配方法均失败，作为新根消息: caller={caller}"
            )

    # 微信引用消息转为回复消息时，去除 content 中的 [引用: ...] 前缀
    # 引用关系已通过 reply_to 字段表达，content 中不需要重复
    if reply_to_msg_id and content.startswith("[引用: "):
        # 去除 "[引用: xxx]\n" 前缀，保留实际用户输入的内容
        newline_pos = content.find("]\n")
        if newline_pos != -1:
            content = content[newline_pos + 2:]  # 跳过 "]\n"
            logger.debug(f"微信回复消息已去除引用前缀: content={content[:80]}")

    # 创建消息
    # 微信消息用 session_id 作为 caller_session_id（用于同一会话的消息关联）
    caller_session_id = raw_data.get("session_id", "") if channel_msg.channel_type == ChannelType.WECHAT else None

    msg_id, result = db.create_message(
        content=content,
        sender=master_name,
        receiver=agent_name,
        caller=caller,
        caller_session_id=caller_session_id,
        reply_to=reply_to_msg_id,
        platform_msg_id=platform_msg_id,
        feishu_chat_id=feishu_chat_id,
        nest=nest_name,
        workdir=workdir,
        status="unread",
    )

    if msg_id > 0:
        logger.info(
            f"✅ Channel消息已转为消息树: msg_id={msg_id}, "
            f"platform_msg={platform_msg_id}, reply_to={reply_to_msg_id}, caller={caller}"
        )
    else:
        logger.warning(f"Channel消息转换失败: platform_msg_id={platform_msg_id}")

    return msg_id if msg_id > 0 else None


def is_feishu_source(caller: str | None) -> bool:
    """检查消息的caller是否为飞书来源"""
    return caller is not None and caller.startswith("feishu:")


def is_wechat_source(caller: str | None) -> bool:
    """检查消息的caller是否为微信来源"""
    return caller is not None and caller.startswith("wechat:")


def get_feishu_platform_msg_id(db_msg: dict[str, Any]) -> str | None:
    """从消息树消息中获取飞书platform_msg_id"""
    pid = db_msg.get("platform_msg_id", "")
    return pid if pid else None


def get_wechat_msg_info(db_msg: dict[str, Any]) -> dict[str, str]:
    """从消息树消息中获取微信回复所需的信息

    需要的信息：
    - platform_msg_id: 微信消息的 message_id（用于日志）
    - from_user_id: 微信消息发送者的 ilink_user_id（用于 send_message 的 to_user_id）

    注意：微信 iLink 发送回复需要知道接收者（from_user_id），
    我们从消息的 platform_msg_id 对应的原始消息 raw_data 中获取。
    由于 raw_data 不直接存储在 messages 表中，我们通过 caller 字段提取 user_id。

    Returns:
        {"user_id": "xxx"} 或空 dict
    """
    caller = db_msg.get("caller", "")
    if not caller.startswith("wechat:"):
        return {}

    user_id = caller[len("wechat:"):]
    return {"user_id": user_id}


async def send_feishu_reply(
    feishu_msg_id: str,
    content: str,
) -> str | None:
    """通过飞书reply API回复指定消息

    Args:
        feishu_msg_id: 要回复的飞书消息ID（platform_msg_id）
        content: 回复内容

    Returns:
        回复消息的飞书 message_id，失败返回 None
    """
    if not _channel_manager:
        logger.error("ChannelManager未初始化，无法发送飞书回复")
        return None

    feishu_channel = _channel_manager.get_channel(ChannelType.FEISHU)
    if not feishu_channel or not isinstance(feishu_channel, FeishuChannel):
        logger.error("飞书通道未注册，无法发送回复")
        return None

    if not feishu_channel.is_connected:
        logger.error("飞书通道未连接，无法发送回复")
        return None

    try:
        new_msg_id = await feishu_channel.reply_message(
            message_id=feishu_msg_id,
            content=content,
        )
        if new_msg_id:
            logger.info(f"✅ 飞书回复发送成功: reply_to_feishu_msg={feishu_msg_id}, new_msg_id={new_msg_id}")
        else:
            logger.error(f"❌ 飞书回复发送失败: reply_to_feishu_msg={feishu_msg_id}")
        return new_msg_id
    except Exception as e:
        logger.error(f"飞书回复发送异常: {e}", exc_info=True)
        return None


async def send_wechat_reply(
    to_user_id: str,
    content: str,
    context_token: str = "",
) -> bool:
    """通过微信iLink sendMessage API回复指定用户

    Args:
        to_user_id: 接收者的 ilink user ID
        content: 回复内容
        context_token: 上下文token（可选，用于关联回复）

    Returns:
        True表示回复成功
    """
    if not _channel_manager:
        logger.error("ChannelManager未初始化，无法发送微信回复")
        return False

    wechat_channel = _channel_manager.get_channel(ChannelType.WECHAT)
    if not wechat_channel or not isinstance(wechat_channel, ClawBotWechatChannel):
        logger.error("微信通道未注册，无法发送回复")
        return False

    if not wechat_channel.is_connected:
        logger.error("微信通道未连接，无法发送回复")
        return False

    try:
        # 构建 ChannelMessage 用于发送
        from peng.channels.base import ChannelMessage
        send_msg = ChannelMessage(
            channel_type=ChannelType.WECHAT,
            platform_msg_id="",
            sender_id="",
            sender_name="",
            receiver_id=to_user_id,
            content=content,
            is_group=False,
            group_name="",
            raw_data={
                "from_user_id": to_user_id,
                "context_token": context_token,
            },
        )
        result = await wechat_channel.send_message(send_msg)
        if result:
            logger.info(f"✅ 微信回复发送成功: to_user={to_user_id}")
        else:
            logger.error(f"❌ 微信回复发送失败: to_user={to_user_id}")
        return result
    except Exception as e:
        logger.error(f"微信回复发送异常: {e}", exc_info=True)
        return False


def channel_message_to_message_record(channel_msg: ChannelMessage) -> dict[str, Any]:
    """将ChannelMessage转换为MessageRecord格式的字典（兼容保留）"""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    source = f"[{channel_msg.channel_type.value}]"
    sender = f"{source} {channel_msg.sender_name or channel_msg.sender_id}"

    content = channel_msg.content

    if channel_msg.is_group and channel_msg.group_name:
        content = f"[群:{channel_msg.group_name}] {content}"

    return {
        "timestamp": timestamp,
        "sender": sender,
        "receiver": "鹏🐣",
        "content": content,
        "title": "",
        "status": "unread",
        "caller": f"channel:{channel_msg.channel_type.value}",
        "workdir": "",
    }


# ==================== @get/@getall 附件功能 ====================

# 图片文件扩展名集合
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico",
    ".tiff", ".tif", ".avif", ".heic", ".heif", ".svg",
}


def parse_get_file_paths(
    chain_head_content: str,
    chain_context: str | None = None,
) -> list[str]:
    """从链首消息内容中解析 @get:<file> 和 @getall 指令，提取文件路径列表

    解析规则：
    1. @get:<file> — 提取指定的文件路径，支持多个
    2. @getall — 从 chain_context 中提取所有有效文件路径

    Args:
        chain_head_content: 链首消息的 content
        chain_context: 链首消息的 chain_context（用于 @getall）

    Returns:
        去重后的文件路径列表
    """
    file_paths: list[str] = []

    # 1. 解析 @get:<file> 指令（支持多个）
    # 匹配 @get:/path/to/file 或 @get:relative/path/file.txt
    get_pattern = re.compile(r"@get:(\S+)")
    matches = get_pattern.findall(chain_head_content)
    for match in matches:
        # 去除可能的尾部标点
        path = match.rstrip(",.;，。；")
        if path:
            file_paths.append(path)

    # 2. 解析 @getall 指令
    if "@getall" in chain_head_content:
        context_text = chain_context or ""
        context_paths = _extract_file_paths_from_context(context_text)
        file_paths.extend(context_paths)

    # 去重（保持顺序）
    seen = set()
    unique_paths = []
    for p in file_paths:
        if p not in seen:
            seen.add(p)
            unique_paths.append(p)

    if unique_paths:
        logger.info(f"@get/@getall 解析出文件路径: {unique_paths}")

    return unique_paths


def _extract_file_paths_from_context(context_text: str) -> list[str]:
    """从 chain_context 文本中提取有效的文件路径

    chain_context 格式示例：
        2025-03-23 10:00:00 #1  master\t消息内容
        2025-03-23 10:01:00 #2 → #1 鹏🐣\t回复内容 [附件] file.txt: /path/to/file

    提取规则：
    1. 提取 "[附件] filename: /path/to/file" 格式中的路径
    2. 提取绝对路径（以 / 或盘符开头的带扩展名的路径）
    3. 排除明显不是文件路径的内容

    Args:
        context_text: chain_context 原始文本

    Returns:
        提取出的文件路径列表
    """
    paths = []

    # 规则1: 提取 [附件] filename: /path 格式（飞书/微信附件下载后的标记）
    attach_pattern = re.compile(r"\[附件\]\s+\S+:\s+(\S+)")
    for match in attach_pattern.finditer(context_text):
        path = match.group(1).rstrip(",.;，。；")
        if path:
            paths.append(path)

    # 规则2: 提取绝对路径（以 / 或 X:\ 或 X:/ 开头，带文件扩展名）
    # 排除已有的 [附件] 路径避免重复
    # 排除 Python 模块路径等误匹配（路径必须包含 /home/、/mnt/、/tmp/、/data/ 等真实前缀，
    # 或者 Windows 盘符路径，或者以 / 开头且长度>=10 的路径）
    abs_path_pattern = re.compile(
        r'(?:(?:/[a-zA-Z0-9_./-]+\.[a-zA-Z0-9]+)'
        r'|(?:[a-zA-Z]:[/\\][a-zA-Z0-9_.\\/-]+\.[a-zA-Z0-9]+))'
    )
    # 合法的绝对路径前缀（Linux 常见根目录）
    valid_linux_prefixes = (
        "/home/", "/mnt/", "/tmp/", "/data/", "/var/", "/opt/",
        "/etc/", "/usr/", "/srv/", "/root/", "/Users/",
    )
    for match in abs_path_pattern.finditer(context_text):
        path = match.group(0).rstrip(",.;，。；")
        if not path or path in paths:
            continue
        # 过滤：Linux 绝对路径必须有合法前缀，或 Windows 盘符路径
        if path.startswith("/"):
            if not any(path.startswith(p) for p in valid_linux_prefixes):
                continue  # 跳过像 /peng/nest/cli.py 这样的误匹配
        paths.append(path)

    return paths


def _is_windows_abs_path(path: str) -> bool:
    r"""判断是否为 Windows 绝对路径（盘符开头，如 C:\ D:/ 等）

    在 Linux 上 os.path.isabs() 不识别 Windows 盘符路径，
    需要额外判断，避免将 Windows 绝对路径误当作相对路径与 workdir 拼接。
    """
    if len(path) >= 2 and path[0].isalpha() and path[1] == ':':
        return True
    return False


def resolve_file_path(
    file_path: str,
    workdir: str | None = None,
) -> str:
    """将文件路径解析为绝对路径

    Args:
        file_path: 原始文件路径（可能是绝对或相对路径）
        workdir: 工作目录（用于解析相对路径）

    Returns:
        绝对路径
    """
    # 已经是绝对路径（包含 Linux 绝对路径和 Windows 绝对路径）
    if os.path.isabs(file_path) or _is_windows_abs_path(file_path):
        return file_path

    # 相对路径：基于 workdir 解析
    if workdir:
        return os.path.normpath(os.path.join(workdir, file_path))

    # 无 workdir，基于当前目录
    return os.path.abspath(file_path)


async def nest_download_file(
    nest_name: str | None,
    file_path: str,
    workdir: str | None = None,
) -> str | None:
    """通过 nest 下载远程文件到本地临时目录

    优先检查本地文件是否存在，存在则直接返回本地路径。
    本地不存在时才通过 nest 远程读取。

    Args:
        nest_name: nest 名称（None 则使用默认）
        file_path: 远程文件路径
        workdir: 工作目录（用于解析相对路径）

    Returns:
        本地文件路径，失败返回 None
    """
    # 解析为绝对路径
    resolved_path = resolve_file_path(file_path, workdir)

    # 优先检查本地文件是否存在（截屏等文件通常在 peng 服务器本地）
    if os.path.isfile(resolved_path):
        logger.info(f"📎 文件在本地存在，直接使用: {resolved_path} ({os.path.getsize(resolved_path)} bytes)")
        return resolved_path

    from peng.nest.bridge import NestBridgeManager
    from peng.nest.protocol import NestRequest

    manager = NestBridgeManager.get_instance()
    if not manager:
        logger.error("NestBridgeManager 未初始化")
        return None

    # 获取 nest 连接
    conn = None
    if nest_name:
        conn = manager.get_connection(nest_name)
    if not conn:
        conn = manager.get_default_connection()
    if not conn:
        logger.error(f"无可用的 nest 连接（指定: {nest_name}）")
        return None

    logger.info(f"通过 nest 下载文件: {file_path} → {resolved_path} (nest={conn.display_key})")

    try:
        # 尝试读取文件（对于文本文件和二进制文件，nest 都会返回数据）
        request = NestRequest(
            type="read",
            params={"path": resolved_path, "offset": 1, "limit": 1000000, "_skip_security": True},
        )
        response = await conn.send_request(request, timeout=120.0)

        if response.error:
            logger.error(f"nest 读取文件失败: {response.error}")
            return None

        data = response.data
        if not data:
            logger.error(f"nest 返回空数据: {resolved_path}")
            return None

        # 根据返回类型处理
        file_type = data.get("type", "")

        if file_type == "binary":
            # 二进制文件：base64 编码
            b64_data = data.get("data", "")
            if not b64_data:
                logger.error(f"nest 返回空 binary 数据: {resolved_path}")
                return None
            file_bytes = base64.b64decode(b64_data)

        elif file_type == "file":
            # 文本文件：从 lines 中提取
            lines = data.get("lines", [])
            text_lines = []
            for line in lines:
                if ": " in line:
                    text_lines.append(line.split(": ", 1)[1])
                else:
                    text_lines.append(line)
            file_bytes = "\n".join(text_lines).encode("utf-8")

        else:
            logger.error(f"nest 返回未知类型: {file_type}")
            return None

        # 保存到本地临时文件
        filename = os.path.basename(resolved_path)
        # 清理文件名中的特殊字符
        safe_filename = re.sub(r'[^\w.\-]', '_', filename)
        if not safe_filename:
            safe_filename = f"file_{int(time.time())}"

        tmp_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
            "data", "tmp", "attachments"
        )
        os.makedirs(tmp_dir, exist_ok=True)

        local_path = os.path.join(tmp_dir, safe_filename)
        # 避免文件名冲突
        if os.path.exists(local_path):
            name, ext = os.path.splitext(safe_filename)
            local_path = os.path.join(tmp_dir, f"{name}_{int(time.time())}{ext}")

        with open(local_path, "wb") as f:
            f.write(file_bytes)

        logger.info(f"✅ 文件已从 nest 下载到本地: {local_path} ({len(file_bytes)} bytes)")
        return local_path

    except Exception as e:
        logger.error(f"nest 下载文件异常: {e}", exc_info=True)
        return None


async def send_feishu_attachments(
    feishu_msg_id: str,
    file_paths: list[str],
) -> int:
    """通过飞书发送文件附件

    先上传文件到飞书，再作为回复消息发送。

    Args:
        feishu_msg_id: 要回复的飞书消息ID
        file_paths: 本地文件路径列表

    Returns:
        成功发送的附件数量
    """
    if not _channel_manager:
        logger.error("ChannelManager未初始化，无法发送飞书附件")
        return 0

    feishu_channel = _channel_manager.get_channel(ChannelType.FEISHU)
    if not feishu_channel or not isinstance(feishu_channel, FeishuChannel):
        logger.error("飞书通道未注册，无法发送附件")
        return 0

    if not feishu_channel.is_connected:
        logger.error("飞书通道未连接，无法发送附件")
        return 0

    success_count = 0
    for file_path in file_paths:
        try:
            is_image = feishu_channel._is_image_file(file_path)

            if is_image:
                # 图片：上传图片 → 回复图片消息
                image_key = await feishu_channel.upload_image(file_path)
                if image_key:
                    result = await feishu_channel.reply_image_message(feishu_msg_id, image_key)
                    if result:
                        success_count += 1
                        logger.info(f"📱 飞书图片附件发送成功: {os.path.basename(file_path)}")
                    else:
                        logger.error(f"📱 飞书图片附件发送失败: {os.path.basename(file_path)}")
                else:
                    logger.error(f"飞书图片上传失败: {os.path.basename(file_path)}")
            else:
                # 文件：上传文件 → 回复文件消息
                file_key = await feishu_channel.upload_file(file_path)
                if file_key:
                    result = await feishu_channel.reply_file_message(feishu_msg_id, file_key)
                    if result:
                        success_count += 1
                        logger.info(f"📱 飞书文件附件发送成功: {os.path.basename(file_path)}")
                    else:
                        logger.error(f"📱 飞书文件附件发送失败: {os.path.basename(file_path)}")
                else:
                    logger.error(f"飞书文件上传失败: {os.path.basename(file_path)}")
        except Exception as e:
            logger.error(f"发送飞书附件异常: {file_path}, {e}", exc_info=True)

    return success_count


async def send_wechat_attachments(
    to_user_id: str,
    file_paths: list[str],
) -> int:
    """通过微信发送文件附件

    微信 iLink API 的 sendmessage 只可靠支持 type=1 TEXT 消息。
    策略：
    - 文本文件（.txt/.py/.json等）：将文件内容作为文本消息发送
    - 图片/二进制文件：发送文件路径通知

    Args:
        to_user_id: 微信用户的 ilink user ID
        file_paths: 本地文件路径列表

    Returns:
        成功发送的附件数量
    """
    if not _channel_manager:
        logger.error("ChannelManager未初始化，无法发送微信附件")
        return 0

    wechat_channel = _channel_manager.get_channel(ChannelType.WECHAT)
    if not wechat_channel or not isinstance(wechat_channel, ClawBotWechatChannel):
        logger.error("微信通道未注册，无法发送附件")
        return 0

    if not wechat_channel.is_connected:
        logger.error("微信通道未连接，无法发送附件")
        return 0

    # 文本文件扩展名集合
    text_extensions = {
        ".txt", ".md", ".py", ".js", ".ts", ".json", ".xml", ".html",
        ".css", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf",
        ".sh", ".bat", ".csv", ".log", ".sql", ".c", ".cpp", ".h",
        ".go", ".rs", ".java", ".rb", ".php", ".r", ".tex", ".ini",
    }

    success_count = 0
    for file_path in file_paths:
        try:
            filename = os.path.basename(file_path)
            ext = os.path.splitext(file_path)[1].lower()

            if ext in text_extensions:
                # 文本文件：读取内容作为文本消息发送
                try:
                    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()

                    # 限制长度（微信消息不宜过长）
                    max_len = 3000
                    if len(content) > max_len:
                        content = content[:max_len] + f"\n... (共 {len(content)} 字符，已截断)"

                    text_msg = f"📎 [{filename}]\n{content}"

                    from peng.channels.base import ChannelMessage
                    send_msg = ChannelMessage(
                        channel_type=ChannelType.WECHAT,
                        platform_msg_id="",
                        sender_id="",
                        sender_name="",
                        receiver_id=to_user_id,
                        content=text_msg,
                        is_group=False,
                        group_name="",
                        raw_data={"from_user_id": to_user_id},
                    )
                    result = await wechat_channel.send_message(send_msg)
                    if result:
                        success_count += 1
                        logger.info(f"📱 微信文本附件发送成功: {filename}")
                    else:
                        logger.error(f"📱 微信文本附件发送失败: {filename}")
                except Exception as e:
                    logger.error(f"读取文本文件失败: {file_path}, {e}")

            else:
                # 二进制/图片文件：发送路径通知
                # iLink API 不支持直接发送二进制文件
                file_size = os.path.getsize(file_path) if os.path.isfile(file_path) else 0
                size_str = f"{file_size / 1024:.1f}KB" if file_size < 1024 * 1024 else f"{file_size / 1024 / 1024:.1f}MB"
                text_msg = f"📎 [{filename}] ({size_str}) 微信暂不支持直接发送此类型文件，文件已准备好在服务器上。"

                from peng.channels.base import ChannelMessage
                send_msg = ChannelMessage(
                    channel_type=ChannelType.WECHAT,
                    platform_msg_id="",
                    sender_id="",
                    sender_name="",
                    receiver_id=to_user_id,
                    content=text_msg,
                    is_group=False,
                    group_name="",
                    raw_data={"from_user_id": to_user_id},
                )
                result = await wechat_channel.send_message(send_msg)
                if result:
                    success_count += 1
                    logger.info(f"📱 微信文件通知发送成功: {filename}")
                else:
                    logger.error(f"📱 微信文件通知发送失败: {filename}")

        except Exception as e:
            logger.error(f"发送微信附件异常: {file_path}, {e}", exc_info=True)

    return success_count

"""
消息 CLI 服务模块 - 消息系统的核心业务逻辑

提供 bin/msg CLI 的所有业务逻辑功能，包括：
- 消息发送（master→鹏，鹏→master）
- 消息查询（收件箱、发件箱、所有消息）
- 消息状态管理（已读、处理中、完成）
- 消息归档和清理
- 事务管理接口
- 搜索功能
- 验收管理
- 并发控制（锁、心跳）
- 过期检测（stale/paused）

项目路径获取统一使用：
    from peng.config import get_agent_name, get_project_root

AGENT_NAME = get_agent_name()
"""

from __future__ import annotations

import contextlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from peng.config import get_agent_name, get_master_name, get_project_root
from peng.db.connection import get_connection, get_cursor

AGENT_NAME = get_agent_name()
MASTER_NAME = get_master_name()
from peng.message.database import MessageDatabase  # noqa: E402
from peng.message.manager import MessageManager  # noqa: E402

logger = logging.getLogger(__name__)

# 并行处理线程池大小
_PARALLEL_WORKERS = 8

# ==================== 路径和环境配置 ====================


def _get_db_paths() -> dict[str, Path]:
    """获取数据库路径（PostgreSQL 不需要文件路径）"""
    paths = {}

    try:
        # PostgreSQL 使用连接池，不需要文件路径
        # 保留这个函数用于兼容性，但返回空路径
        from peng.config import get_path

        paths["history_dir"] = get_path("history")
    except Exception as e:
        import traceback

        logger.critical(f"❌ _get_db_paths() 加载配置失败: {e}\n{traceback.format_exc()}")
        import sys

        sys.exit(1)

    return paths


def ensure_dirs() -> None:
    """确保目录存在"""
    paths = _get_db_paths()
    if paths.get("history_dir"):
        paths["history_dir"].mkdir(parents=True, exist_ok=True)


def get_message_database(db_path: Path | None = None) -> MessageDatabase:
    """获取消息数据库实例（PostgreSQL 不需要路径）"""
    # PostgreSQL 使用连接池，不需要传递 db_path
    return MessageDatabase()


def get_message_manager(db_path: Path | None = None) -> MessageManager:
    """获取消息管理器实例（PostgreSQL 不需要路径）"""
    # PostgreSQL 使用连接池，不需要传递 db_path
    return MessageManager()


def get_task_manager():
    """获取事务管理器实例（PostgreSQL 不需要文件路径）"""
    from peng.task.manager import TaskManager

    # PostgreSQL 使用连接池，不需要传递文件路径
    return TaskManager()


# ==================== 消息发送 ====================


def send_message(
    content: str,
    reply_to_id: int = None,
    timeout: int = 0,
    from_: str = None,
    to: str = None,
    nest: str = None,
    workdir: str = None,
) -> dict[str, Any]:
    """
    发送消息（统一接口）

    Args:
        content: 消息内容
        reply_to_id: 回复的消息 ID
        timeout: 超时时间（秒）
        from_: 发送者（CLI调用时的默认值：reply则鹏→master，send则master→鹏）
        to: 接收者（CLI调用时的默认值：reply则master，send则鹏）
        nest: 关联的nest名称（可选）
        workdir: 工作目录（可选）

    Returns:
        结果字典，包含 msg_id, timestamp, content 等
    """
    ensure_dirs()

    # 设置时间戳
    timestamp = datetime.now().strftime("%Y-%m-%d:%H:%M:%S")

    # 使用 MessageManager 创建消息（会自动更新链首上下文）
    msg_manager = get_message_manager()
    msg_id, msg = msg_manager.create_message(
        content=content,
        sender=from_,
        receiver=to,
        caller="msg",
        reply_to=reply_to_id,
        timeout=timeout,
        nest=nest,
        workdir=workdir,
    )
    msg["sucess"] = msg_id > 0
    if not msg_id:
        if not msg.get("error"):
            msg["error"] = "重复消息" if msg["is_duplicate"] else "创建消息失败"
    return msg


# ==================== 消息查询 ====================


def read_outbox(show_all: bool = False) -> dict[str, Any]:
    """
    读取发件箱（鹏发给master的消息）

    Args:
        show_all: 是否显示所有消息（否则只显示未读）

    Returns:
        结果字典，包含 messages 列表和统计信息
    """
    ensure_dirs()
    paths = _get_db_paths()

    # PostgreSQL模式下不检查文件存在
    # 直接连接数据库即可

    db = get_message_database()
    all_messages = db.get_by_receiver(MASTER_NAME)

    if show_all:
        messages = all_messages
        title = "所有反馈"
    else:
        messages = [m for m in all_messages if m.get("status") == "unread"]
        title = "未读反馈"

    if not messages:
        return {"messages": [], "empty": True, "title": title}

    # 自动标记为已读（只有查看未读时才标记）- 批量操作
    if not show_all:
        db.mark_read_batch([msg["id"] for msg in messages])

    return {
        "messages": messages,
        "empty": False,
        "title": title,
        "count": len(messages),
        "auto_marked_read": not show_all,
    }


def read_unread_messages() -> dict[str, Any]:
    """读取未读消息"""
    ensure_dirs()
    paths = _get_db_paths()

    # PostgreSQL模式下不检查文件存在
    # 直接连接数据库即可

    db = get_message_database()
    messages = db.get_unread()

    if not messages:
        return {"messages": [], "empty": True, "title": "暂无新消息"}

    return {
        "messages": messages,
        "empty": False,
        "count": len(messages),
    }


def read_all_messages(ascending: bool = True, order: str = "ASC") -> dict[str, Any]:
    """
    查看所有消息（只显示链首消息，包含完整上下文）

    Args:
        order: 排序顺序，"ASC" 为从老到新，"DESC" 为从新到老（默认 "ASC"）

    Returns:
        消息列表和统计信息，每条消息包含 reply_chain 和 tree 字段
    """
    ensure_dirs()
    paths = _get_db_paths()

    # PostgreSQL模式下不检查文件存在
    # 直接连接数据库即可

    db = get_message_database()
    # 只获取链首消息（is_chain_head=1 或 reply_to IS NULL）
    all_messages = db.get_all(ascending=(order == "ASC"))
    chain_heads = [
        msg for msg in all_messages if msg.get("is_chain_head") == 1 or msg.get("reply_to") is None
    ]

    # 为每个链首获取完整回复链和消息树
    from peng.message.types import MessageTree

    msg_manager = MessageManager()

    for head in chain_heads:
        reply_chain = db.get_reply_chain(head["id"])
        head["reply_chain"] = reply_chain

        # 构建消息树
        message_tree = MessageTree.from_messages(reply_chain)
        if message_tree:
            head["tree"] = message_tree.to_dict()
        else:
            head["tree"] = None

    if not chain_heads:
        return {"messages": [], "empty": True}

    return {
        "messages": chain_heads,
        "empty": False,
        "count": len(chain_heads),
    }


def read_unarchived_messages(order: str = "ASC") -> dict[str, Any]:
    """
    查看所有未归档的根消息树（只显示链首消息，包含完整上下文）

    未归档消息定义：is_archived 为 0 或 NULL 的链首消息。

    Args:
        order: 排序顺序，"ASC" 为从老到新，"DESC" 为从新到老（默认 "ASC"）

    Returns:
        消息列表和统计信息，每条消息包含 reply_chain 和 tree 字段
    """
    ensure_dirs()
    paths = _get_db_paths()

    db = get_message_database()
    # 获取所有链首消息
    all_messages = db.get_all(ascending=(order == "ASC"))
    chain_heads = [
        msg for msg in all_messages if msg.get("is_chain_head") == 1 or msg.get("reply_to") is None
    ]

    # 过滤未归档的消息
    unarchived_heads = [
        msg for msg in chain_heads if not msg.get("is_archived", 0)
    ]

    # 为每个未归档链首获取完整回复链和消息树
    from peng.message.types import MessageTree

    msg_manager = MessageManager()

    for head in unarchived_heads:
        reply_chain = db.get_reply_chain(head["id"])
        head["reply_chain"] = reply_chain

        # 构建消息树
        message_tree = MessageTree.from_messages(reply_chain)
        if message_tree:
            head["tree"] = message_tree.to_dict()
        else:
            head["tree"] = None

    if not unarchived_heads:
        return {"messages": [], "empty": True, "reason": "暂无未归档的消息"}

    return {
        "messages": unarchived_heads,
        "empty": False,
        "count": len(unarchived_heads),
    }


def read_incomplete_messages(order: str = "ASC") -> dict[str, Any]:
    """
    查看所有未完成的消息树（只显示链首消息，包含完整上下文）

    未完成消息定义：
    - status 不是 "done"，并且满足以下任一条件：
      - status 在 ("unread", "read", "processing", "interrupted") 中
      - 被取消（is_cancelled=1）
      - 被暂停（is_paused=1）

    注意：status="done" 的消息永远被认为是已完成的，不管 is_cancelled 和 is_paused 的值

    Args:
        order: 排序顺序，"ASC" 为从老到新，"DESC" 为从新到老（默认 "ASC"）

    Returns:
        消息列表和统计信息，每条消息包含 reply_chain 和 tree 字段
    """
    ensure_dirs()
    paths = _get_db_paths()

    # PostgreSQL模式下不检查文件存在
    # 直接连接数据库即可

    db = get_message_database()
    # 只获取链首消息（is_chain_head=1 或 reply_to IS NULL）
    all_messages = db.get_all(ascending=(order == "ASC"))
    chain_heads = [
        msg for msg in all_messages if msg.get("is_chain_head") == 1 or msg.get("reply_to") is None
    ]

    # 过滤未完成的消息
    incomplete_heads = []
    for msg in chain_heads:
        # 判断是否未完成
        status = msg.get("status", "unread")
        is_cancelled = msg.get("is_cancelled", 0)
        is_paused = msg.get("is_paused", 0)

        # 未完成条件：
        # 1. 状态必须是未完成（不是 "done"）
        # 2. 并且是未读/已读/处理中/中断，或者被取消/暂停
        is_completed = status == "done"
        is_incomplete = (not is_completed) and (
            status in ("unread", "read", "processing", "interrupted") or is_cancelled or is_paused
        )

        if is_incomplete:
            incomplete_heads.append(msg)

    # 为每个未完成链首获取完整回复链和消息树
    from peng.message.types import MessageTree

    msg_manager = MessageManager()

    for head in incomplete_heads:
        reply_chain = db.get_reply_chain(head["id"])
        head["reply_chain"] = reply_chain

        # 构建消息树
        message_tree = MessageTree.from_messages(reply_chain)
        if message_tree:
            head["tree"] = message_tree.to_dict()
        else:
            head["tree"] = None

    if not incomplete_heads:
        return {"messages": [], "empty": True, "reason": "暂无未完成的消息"}

    return {
        "messages": incomplete_heads,
        "empty": False,
        "count": len(incomplete_heads),
    }


def search_messages(keyword: str) -> dict[str, Any]:
    """
    模糊搜索消息（直接过滤 msg -a 的结果）

    Args:
        keyword: 搜索关键词

    Returns:
        匹配的消息列表
    """
    if not keyword:
        return {"messages": [], "empty": True, "error": "请提供搜索关键词"}

    # 直接调用 read_all_messages 获取所有链首（含 reply_chain）
    all_result = read_all_messages(ascending=True)

    if all_result.get("empty"):
        return {
            "messages": [],
            "empty": True,
            "reason": all_result.get("reason", "无消息"),
            "keyword": keyword,
        }

    # 过滤：链首或回复链中包含关键词
    keyword_lower = keyword.lower()
    matched = []
    for msg in all_result["messages"]:
        reply_chain = msg.get("reply_chain", [])
        # 检查链首或任何回复是否包含关键词
        for ctx in reply_chain:
            if keyword_lower in ctx.get("content", "").lower():
                matched.append(msg)
                break

    if not matched:
        return {"messages": [], "empty": True, "keyword": keyword}

    return {
        "messages": matched,
        "empty": False,
        "keyword": keyword,
        "count": len(matched),
    }


def show_status() -> dict[str, Any]:
    """
    显示消息状态统计

    Returns:
        状态统计字典
    """
    ensure_dirs()
    paths = _get_db_paths()

    # PostgreSQL模式下不检查文件存在
    db = get_message_database()

    # 只统计收件箱
    inbox_messages = db.get_by_receiver(AGENT_NAME)
    counts = {}
    for msg in inbox_messages:
        status = msg.get("status", "unread")
        counts[status] = counts.get(status, 0) + 1

    # 统计发件箱数量
    outbox_count = len(db.get_by_receiver(MASTER_NAME))

    return {
        "inbox": counts,
        "outbox_count": outbox_count,
        "empty": False,
    }


def show_finished(limit: int = 20) -> dict[str, Any]:
    """
    显示已完成消息

    Args:
        limit: 显示数量

    Returns:
        已完成消息列表
    """
    ensure_dirs()
    paths = _get_db_paths()

    # PostgreSQL模式下不检查文件存在
    db = get_message_database()
    messages = db.get_finished(limit=limit)

    if not messages:
        return {"messages": [], "empty": True}

    return {
        "messages": messages,
        "empty": False,
        "count": len(messages),
    }


def list_inbox() -> dict[str, Any]:
    """
    显示收件箱（过滤已完成的消息）

    Returns:
        消息列表和统计信息
    """
    ensure_dirs()
    paths = _get_db_paths()

    # PostgreSQL模式下不检查文件存在
    db = get_message_database()
    all_messages = db.get_by_receiver(AGENT_NAME)

    # 过滤：显示未完成的消息 + 被中断的消息 + 被暂停的消息
    messages = [
        m
        for m in all_messages
        if m["status"] in ("unread", "read", "processing")
        or m.get("is_cancelled", 0)
        or m.get("is_paused", 0)
    ]
    done_count = len(
        [
            m
            for m in all_messages
            if m["status"] in ("done", "archived")
            and not m.get("is_cancelled", 0)
            and not m.get("is_paused", 0)
        ]
    )

    if not messages:
        return {
            "messages": [],
            "empty": True,
            "done_count": done_count,
        }

    # 获取消息管理器（用于获取上下文）
    msg_manager = None
    with contextlib.suppress(Exception):
        msg_manager = get_message_manager()

    # 优先获取包含上下文的消息（并行化）
    if msg_manager:

        def _get_context(msg: dict) -> None:
            try:
                ctx_msg = msg_manager.get_message_with_context(msg["id"])
                if ctx_msg:
                    msg["chain_context"] = ctx_msg.get("chain_context")
            except Exception as e:
                import traceback

                logger.error(
                    f"❌ _get_context() - cli_service.py:479: 异常 - {e}\n{traceback.format_exc()}"
                )

        with ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as executor:
            executor.map(_get_context, messages)

    return {
        "messages": messages,
        "empty": False,
        "count": len(messages),
        "done_count": done_count,
    }


def list_unread_inbox() -> dict[str, Any]:
    """只显示未读的收件箱消息"""
    ensure_dirs()
    paths = _get_db_paths()

    # PostgreSQL模式下不检查文件存在
    db = get_message_database()
    messages = db.get_by_status("unread")
    messages = [m for m in messages if m.get("receiver") == AGENT_NAME]

    if not messages:
        return {"messages": [], "empty": True}

    return {
        "messages": messages,
        "empty": False,
        "count": len(messages),
    }


# ==================== 事务管理 ====================
def get_tasks_by_message(message_id: int) -> dict[str, Any]:
    """获取消息关联的事务列表"""
    task_manager = get_task_manager()
    tasks = task_manager.get_tasks_by_message(message_id)

    if not tasks:
        return {"tasks": [], "empty": True}

    return {
        "tasks": tasks,
        "empty": False,
        "count": len(tasks),
    }


def control_message_task(message_id: int, action: str) -> dict[str, Any]:
    """
    控制消息关联的事务

    Args:
        message_id: 消息 ID
        action: 控制动作（pause/cancel/resume/restart/up/down）

    Returns:
        控制结果
    """
    msg_manager = get_message_manager()
    db = get_message_database()
    msg = db.get_message(message_id)

    if not msg:
        return {"success": False, "error": f"消息 #{message_id} 不存在"}

    task_manager = get_task_manager()
    tasks = task_manager.get_tasks_by_message(message_id)

    if not tasks:
        return {"success": False, "error": f"消息 #{message_id} 没有关联的事务"}

    # 将所有控制操作写入控制表（包括up/down）
    try:
        # 写入消息控制表
        msg_db = get_message_database()
        msg_db.insert_message_control(message_id, action)

        # 写入事务控制表
        from peng.task.database import TaskDatabase

        task_db = TaskDatabase()
        for task in tasks:
            task_id = task.get("id")
            task_db.insert_task_control(task_id, action)

        logger.info(
            f"✅ 已将控制操作写入控制表: {action} -> 消息 #{message_id}, {len(tasks)} 个事务"
        )

        return {
            "success": True,
            "message_id": message_id,
            "action": action,
            "task_count": len(tasks),
            "queued": True,  # 标记为已排队
        }

    except Exception as e:
        import traceback

        logger.error(f"❌ 写入控制表失败: {e}\n{traceback.format_exc()}")
        return {
            "success": False,
            "error": f"写入控制表失败: {e}",
        }


def show_processing_messages() -> dict[str, Any]:
    """显示处理中的消息和事务树"""
    db = get_message_database()
    processing_msgs = db.get_by_status("processing")
    to_peng_msgs = [m for m in processing_msgs if m.get("receiver") == AGENT_NAME]

    if not to_peng_msgs:
        return {"messages": [], "empty": True}

    task_manager = get_task_manager()
    msg_manager = get_message_manager()

    def _process_message(msg: dict) -> dict[str, Any] | None:
        """并行处理单条消息"""
        msg_id = msg.get("id")

        # 优先获取包含上下文的消息
        display_msg = msg
        try:
            ctx_msg = msg_manager.get_message_with_context(msg_id)
            if ctx_msg:
                display_msg = ctx_msg
        except Exception as e:
            import traceback

            logger.error(
                f"❌ _process_message() - cli_service.py:794: 异常 - {e}\n{traceback.format_exc()}"
            )

        # 获取关联事务
        tasks = task_manager.get_tasks_by_message(msg_id)

        # 获取回复链
        chain = get_message_database().get_reply_chain(msg_id)

        return {
            "message": display_msg,
            "tasks": tasks,
            "chain": chain.replies if chain else [],
        }

    # 并行处理消息
    messages = []
    with ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as executor:
        results = list(executor.map(_process_message, to_peng_msgs))
        messages = [r for r in results if r is not None]

    return {
        "messages": messages,
        "empty": False,
        "count": len(messages),
    }


def update_message_timeout(message_id: int | None, timeout: int) -> dict[str, Any]:
    """
    更新消息的超时时间

    Args:
        message_id: 消息 ID（None 表示第一条处理中的消息）
        timeout: 超时时间（秒）

    Returns:
        更新结果
    """
    db = get_message_database()

    if message_id is None or message_id == -1:
        processing_msgs = db.get_by_status("processing")
        to_peng_msgs = [m for m in processing_msgs if m.get("receiver") == AGENT_NAME]
        if not to_peng_msgs:
            return {"success": False, "error": "没有处理中的消息"}
        message_id = to_peng_msgs[0]["id"]

    msg = db.get_message(message_id)
    if not msg:
        return {"success": False, "error": f"消息 #{message_id} 不存在"}

    msg_manager = get_message_manager()
    if msg_manager.set_timeout(message_id, timeout):
        timeout_str = f"{timeout}秒" if timeout > 0 else "永不超时"
        return {
            "success": True,
            "msg_id": message_id,
            "timeout": timeout,
            "timeout_str": timeout_str,
        }
    return {"success": False, "error": "无法更新消息超时"}


# ==================== 并发控制 ====================


def lock_unread_message() -> dict[str, Any]:
    """锁定一条未读消息"""
    db = get_message_database()
    msg_id = db.lock_unread_message()

    if msg_id:
        msg = db.get_message(msg_id)
        content = msg.get("content", "") if msg else ""
        return {
            "success": True,
            "msg_id": msg_id,
        }
    return {"success": False, "error": "没有可锁定的未读消息"}


def update_heartbeat(msg_id: int) -> dict[str, Any]:
    """
    更新消息的心跳时间

    注意：不传入 process_id 参数，保持原有 process_id 不变
    这样可以避免 process_id 混乱导致取消事务时杀死错误的进程
    """
    db = get_message_database()
    if db.update_heartbeat(msg_id):
        return {"success": True, "msg_id": msg_id}
    return {"success": False, "error": "消息不存在或未锁定"}


# ==================== 过期检测 ====================


def check_stale_messages() -> dict[str, Any]:
    """检测并标记失效的处理中消息为中断"""
    msg_manager = get_message_manager()
    interrupted_ids = msg_manager.check_and_mark_stale_processing_messages()

    if interrupted_ids:
        return {
            "success": True,
            "interrupted_ids": interrupted_ids,
            "count": len(interrupted_ids),
        }
    return {"success": True, "count": 0, "message": "没有检测到失效的处理中消息"}


def check_paused_messages() -> dict[str, Any]:
    """检测并显示被暂停的消息"""
    msg_manager = get_message_manager()
    paused_ids = msg_manager.check_and_mark_paused_messages()

    if paused_ids:
        return {
            "success": True,
            "paused_ids": paused_ids,
            "count": len(paused_ids),
        }
    return {"success": True, "count": 0, "message": "没有检测到被暂停的消息"}


# ==================== 消息树 ====================


def get_message_tree(message_id: int | None = None) -> dict[str, Any]:
    """
    获取消息树

    Args:
        message_id: 消息ID（None则获取当前处理中的消息树）

    Returns:
        消息树数据，包含树结构
    """
    # PostgreSQL模式下不检查文件存在
    msg_manager = get_message_manager()
    # 获取消息树
    tree = msg_manager.get_message_tree(message_id)

    if tree is None:
        return {"tree": None, "empty": True, "reason": f"消息 #{message_id} 不存在"}

    return {"tree": tree, "empty": False, "message_id": message_id}


def get_message_context(message_id: int) -> dict[str, Any]:
    """
    获取消息上下文

    Args:
        message_id: 消息ID

    Returns:
        消息上下文数据
    """
    ensure_dirs()
    paths = _get_db_paths()

    # PostgreSQL模式下不检查文件存在
    msg_manager = get_message_manager()
    db = get_message_database()

    # 获取消息
    msg = db.get_message(message_id)
    if not msg:
        return {"context": None, "empty": True, "reason": f"消息 #{message_id} 不存在"}

    # 找到消息树根的 chain_context
    # 如果是链首消息，直接使用自己的 chain_context
    # 如果不是链首消息，找到链首消息
    chain_head_id = msg.get("chain_head_id")
    if msg.get("is_chain_head") == 1 or msg.get("reply_to") is None:
        chain_head_id = msg["id"]
    elif not chain_head_id:
        # 没有存储 chain_head_id，通过 reply_to 反向查找
        tree = msg_manager.get_message_tree(message_id)
        if tree:
            chain_head_id = tree.root.message.id
        else:
            return {"context": None, "empty": True, "reason": f"无法获取消息树"}

    if not chain_head_id:
        return {"context": None, "empty": True, "reason": f"无法找到消息树根"}

    # 获取链首消息的 chain_context
    head_msg = db.get_message(chain_head_id)
    if not head_msg:
        return {"context": None, "empty": True, "reason": f"消息 #{chain_head_id} 不存在"}

    context = head_msg.get("chain_context")

    return {
        "context": context,
        "empty": False,
        "message_id": message_id,
        "chain_head_id": chain_head_id,
    }

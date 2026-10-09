#!/usr/bin/env python3
"""
鹏的消息系统 - CLI 命令行接口

master与鹏的通信管道，支持消息状态：未读 → 处理中 → 处理完成

架构说明：
- 本文件为纯 CLI 界面，负责命令行参数解析和结果显示
- 所有消息处理业务逻辑委托给 src/peng/message/cli_service 模块
- 数据库操作委托给 src/peng/message/database 模块
- 显示格式化委托给 src/peng/message/display 模块

子命令结构：
  msg [全局选项] <子命令> [子命令选项]

  子命令：
    send         发送消息给鹏（默认子命令）
    list         列出消息
    show         显示消息详情
    content      消息内容操作（无参数=显示，有内容=添加，-d=清空）
    title        消息标题操作（无参数=显示，有内容=设置，-d=清空）
    reply        回复消息
    delete       删除消息
    status       显示消息状态统计或设置消息状态（status <id> [read|processing|done|failed]）
    pause        暂停消息关联的事务
    cancel       取消消息关联的事务
    restart      重启消息关联的事务
    resume       恢复消息关联的事务
    up           提高消息关联的事务优先级
    down         降低消息关联的事务优先级
    search       搜索消息
    tree         显示消息分支树（从该消息开始）
    full-tree    显示消息完整树（从树根开始）
    context      显示消息上下文
    move         移动消息到新的父节点或变成树根
    workdir      消息工作目录操作（无参数=显示，有内容=设置，-d=清空）
    archive      归档消息（归档后不在 trees 列表中显示）
    unarchive    取消归档消息
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

from peng.common.colors import get_message_colors
from peng.config import get_agent_name, get_master_name, get_project_root
from peng.common.utils import get_text_from_args, fix_markdown_linebreaks
from peng.message import cli_service

PROJECT_ROOT = get_project_root()
AGENT_NAME = get_agent_name()
MASTER_NAME = get_master_name()


class Colors:
    """终端颜色（从统一颜色配置获取）"""

    _colors = get_message_colors()
    RED = _colors.get("ERROR", "")
    GREEN = _colors.get("SUCCESS", "")
    YELLOW = _colors.get("WARNING", "")
    BLUE = _colors.get("INFO", "")
    MAGENTA = _colors.get("HIGHLIGHT", "")
    CYAN = _colors.get("TITLE", "")
    WHITE = _colors.get("CONTENT", "")
    BOLD = _colors.get("BOLD", "")
    DIM = _colors.get("DIM", "")
    RESET = _colors.get("RESET", "")
    TIMESTAMP = _colors.get("TIMESTAMP", "\033[90m")
    # 低饱和度内容颜色（从配置读取RGB格式）
    CONTENT_GREEN = _colors.get("CONTENT_GREEN", "\033[38;2;140;170;140m")
    CONTENT_BLUE = _colors.get("CONTENT_BLUE", "\033[38;2;140;160;190m")
    # 发送者标签颜色（使用原来的亮色）
    LABEL_GREEN = _colors.get("LABEL_GREEN", "\033[92m")
    LABEL_BLUE = _colors.get("LABEL_BLUE", "\033[94m")

    @classmethod
    def highlight(cls, text: str, color: str = None) -> str:
        """高亮文本"""
        if color is None:
            color = cls.CYAN
        return f"{color}{text}{cls.RESET}"

    @staticmethod
    def rgb(r: int, g: int, b: int) -> str:
        """
        生成RGB真彩色ANSI代码

        Args:
            r: 红色分量 (0-255)
            g: 绿色分量 (0-255)
            b: 蓝色分量 (0-255)

        Returns:
            ANSI颜色代码字符串
        """
        return f"\033[38;2;{r};{g};{b}m"

    @staticmethod
    def rgb_bg(r: int, g: int, b: int) -> str:
        """
        生成RGB真彩色背景ANSI代码

        Args:
            r: 红色分量 (0-255)
            g: 绿色分量 (0-255)
            b: 蓝色分量 (0-255)

        Returns:
            ANSI背景颜色代码字符串
        """
        return f"\033[48;2;{r};{g};{b}m"


# ==================== CLI 显示函数 ====================


def _resolve_workdir_from_nest(nest: str) -> str | None:
    """如果指定了 nest 但没有指定 workdir，从 NestBridgeManager 或 peng service API 获取该 nest 的 workdir
    
    Args:
        nest: nest 名称
    
    Returns:
        解析后的 workdir，如果无法解析则返回 None
    """
    if not nest:
        return None
    
    # 方式1：从 NestBridgeManager 获取（peng service 进程内部）
    try:
        from peng.nest.bridge import NestBridgeManager
        bridge = NestBridgeManager.get_instance()
        conn = bridge.get_connection(nest)
        if conn:
            nest_workdir = conn.info.get("workdir", "")
            if nest_workdir:
                return nest_workdir
    except Exception:
        pass
    
    # 方式2：从 peng service HTTP API 获取（独立进程）
    try:
        import json
        import urllib.request
        
        peng_host = os.environ.get("PENG_HOST", "localhost")
        peng_port = int(os.environ.get("PENG_PORT", "6688"))
        url = f"http://{peng_host}:{peng_port}/api/nest/list"
        
        req = urllib.request.Request(url, method="GET")
        req.add_header("Content-Type", "application/json")
        
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            nests = data.get("nests", {})
            
            if nest in nests:
                nest_workdir = nests[nest].get("workdir", "")
                if nest_workdir:
                    return nest_workdir
            
            for key, info in nests.items():
                if info.get("original_name") == nest:
                    nest_workdir = info.get("workdir", "")
                    if nest_workdir:
                        return nest_workdir
    except Exception:
        pass
    
    return None


def format_datetime(dt_str: str | None) -> str:
    """格式化时间"""
    if not dt_str:
        return "-"
    try:
        dt = datetime.fromisoformat(dt_str)
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return dt_str


def format_task_status(status: str) -> str:
    """格式化事务状态为中文"""
    names = {
        "pending": "待执行",
        "running": "执行中",
        "completed": "已完成",
        "failed": "已失败",
        "cancelled": "已取消",
        "paused": "已暂停",
        "interrupted": "已中断",
    }
    colors = {
        "pending": Colors.YELLOW,
        "running": Colors.BLUE,
        "completed": Colors.GREEN,
        "failed": Colors.RED,
        "cancelled": Colors.DIM,
        "paused": Colors.CYAN,
        "interrupted": Colors.MAGENTA,
    }
    name = names.get(status, status)
    color = colors.get(status, Colors.WHITE)
    return f"{color}[{name}]{Colors.RESET}"


def _format_message_id(msg_id: int, task_manager=None, color=None) -> str:
    """
    格式化消息ID，如果有关联事务则显示事务ID和状态

    Args:
        msg_id: 消息ID
        task_manager: 事务管理器（可选）
        color: 消息ID颜色（可选，默认使用 MAGENTA）

    Returns:
        格式化后的消息ID字符串，包含事务信息和状态（如果有）
    """
    # 如果没有指定颜色，默认使用紫色
    if color is None:
        color = Colors.MAGENTA

    # 如果提供了事务管理器，检查是否有关联的事务
    if task_manager is not None:
        try:
            from peng.message.manager import MessageManager

            mm = MessageManager()
            task_id = mm.get_task_id(msg_id)
            if task_id:
                # 获取事务状态
                task = task_manager.get_task(task_id)
                if task:
                    status = task.get("status", "pending")
                    status_str = format_task_status(status)
                    # 🔧 事务#1864：running事务处于prefill阶段时显示进度百分比
                    # （本地大模型长上下文prefill耗时较长，让用户知道在正常填充）
                    prefill_suffix = ""
                    if status == "running":
                        try:
                            from peng.llm.prefill_tracker import get_prefill_tracker

                            ps = get_prefill_tracker().read_status(task_id)
                            if ps:
                                prefill_suffix = (
                                    f" {Colors.YELLOW}⏳prefill {ps.get('percent', 0)}%"
                                    f"({int(ps.get('elapsed', 0))}s){Colors.CYAN}"
                                )
                        except Exception:
                            pass
                    msg_id_str = (
                        f"#{msg_id} {Colors.CYAN}事务#{task_id} {status_str}{prefill_suffix}"
                    )
                    return f"{color}{msg_id_str}{Colors.RESET}"
                else:
                    msg_id_str = f"#{msg_id} {Colors.CYAN}事务#{task_id}"
                    return f"{color}{msg_id_str}{Colors.RESET}"
        except Exception:
            # 如果获取事务信息失败，只显示消息ID
            pass

    # 只显示消息ID（不带事务信息）
    return f"{color}#{msg_id}{Colors.RESET}"


def print_send_result(result: dict, reply_to_id: int = None, timeout: int = 0):
    """打印发送消息结果"""
    if result.get("success") is False:
        print(f"{Colors.RED}❌ {result.get('error', '发送失败')}{Colors.RESET}")
        return

    timeout_str = f", 超时：{timeout}秒" if timeout > 0 else ""
    if reply_to_id:
        msg = (
            f"{Colors.GREEN}✅ 消息已发送给{result['receiver']} [{result['timestamp']}] "
            f"(ID: {result['id']}, 引用 #{reply_to_id}{timeout_str}){Colors.RESET}"
        )
        print(msg)
    else:
        msg = (
            f"{Colors.GREEN}✅ 消息已发送给{result['receiver']} [{result['timestamp']}] "
            f"(ID: {result['id']}{timeout_str}){Colors.RESET}"
        )
        print(msg)
    print(f"内容：{result.get('content', '')}")


def print_outbox_result(result: dict, show_all: bool = False):
    """打印发件箱结果"""
    if result.get("empty"):
        if result.get("reason"):
            print(f"📭 暂无反馈消息（{result['reason']}）")
        elif show_all:
            print("📭 暂无反馈")
        else:
            print("📭 暂无新反馈")
            print("💡 查看所有反馈：msg list --outbox")
        return

    title = f"📤 {AGENT_NAME}的反馈" + (" (所有):" if show_all else " (未读):")
    print(f"{Colors.BOLD}{title}{Colors.RESET}")
    print("=" * 50)

    try:
        from peng.message.manager import MessageManager

        mm = MessageManager()
        task_manager = mm._task_manager
    except Exception:
        task_manager = None

    for msg in result["messages"]:
        # 发件箱中的消息都是鹏发的，使用绿色
        msg_id = _format_message_id(msg["id"], task_manager, Colors.LABEL_GREEN)
        timestamp = Colors.highlight(msg["timestamp"], Colors.CYAN)
        content = msg["content"]
        lines = content.split("\n")

        print(f"{msg_id}. [{timestamp}]")
        for line in lines:
            print(f"   {line}")
        print()

    print("=" * 50)
    print(f"共 {Colors.highlight(str(result['count']), Colors.YELLOW)} 条反馈")

    if result.get("auto_marked_read"):
        print("💡 已自动标记为已读")


def print_unread_result(result: dict):
    """打印未读消息结果"""
    if result.get("empty"):
        if result.get("reason"):
            print(f"📭 暂无消息（{result['reason']}）")
        else:
            print("📭 暂无新消息")
            print("💡 使用 msg list --all 查看所有消息")
            print("💡 使用 msg status 查看处理状态")
        return

    print("📬 未读消息:")
    print("=" * 50)

    for i, msg in enumerate(result["messages"], 1):
        timestamp = Colors.highlight(msg["timestamp"], Colors.CYAN)
        content = Colors.highlight(msg["content"], Colors.WHITE)

        # 未读消息都是master的指示，使用蓝色
        try:
            from peng.message.manager import MessageManager

            mm = MessageManager()
            msg_id = _format_message_id(msg["id"], mm._task_manager, Colors.LABEL_BLUE)
        except Exception:
            msg_id = Colors.highlight(f"#{msg['id']}", Colors.LABEL_BLUE)

        print(f"\n{i}. [{timestamp}]")
        print(f"   {msg_id}")
        print(f"   {content}")

    print("=" * 50)
    print(f"共 {Colors.highlight(str(result['count']), Colors.YELLOW)} 条未读消息")


def print_all_messages_result(result: dict):
    """打印所有消息结果（只显示链首，包含完整上下文，按树状结构显示）"""
    if result.get("empty"):
        if result.get("reason"):
            print(f"📭 暂无消息（{result['reason']}）")
        else:
            print("📭 暂无消息")
        return

    print("📬 消息树列表 (从老到新):")
    print("=" * 50)

    try:
        from peng.message.manager import MessageManager

        mm = MessageManager()
        task_manager = mm._task_manager
    except Exception:
        task_manager = None

    for msg in result["messages"]:
        # 优先使用 tree 字段（消息树），否则使用 reply_chain 字段（向后兼容）
        tree = msg.get("tree")
        reply_chain = msg.get("reply_chain", [])

        if tree and tree.get("root"):
            # 使用消息树显示
            print_message_tree_node(tree["root"], 0, task_manager)
        elif reply_chain and len(reply_chain) > 1:
            # 向后兼容：使用回复链显示
            for i, ctx in enumerate(reply_chain):
                ctx_time = Colors.highlight(ctx.get("timestamp", ""), Colors.TIMESTAMP)
                sender = ctx.get("sender", "")

                # 根据发送者判断颜色：风发的用蓝色，鹏发的用绿色
                if sender == MASTER_NAME:
                    # 风：蓝色
                    sender_display = Colors.highlight(MASTER_NAME, Colors.LABEL_BLUE)
                    content_color = Colors.CONTENT_BLUE
                    msg_id_color = Colors.LABEL_BLUE
                else:
                    # 鹏：绿色
                    sender_display = Colors.highlight(AGENT_NAME, Colors.LABEL_GREEN)
                    content_color = Colors.CONTENT_GREEN
                    msg_id_color = Colors.LABEL_GREEN

                # 格式化消息ID（使用正确的颜色）
                ctx_id = _format_message_id(ctx["id"], task_manager, msg_id_color)

                # 保持完整内容，不截断
                content = ctx.get("content", "")

                # 链首无缩进，回复有缩进
                indent = "" if i == 0 else "  "
                print(
                    f"{indent}{ctx_time} {ctx_id} {sender_display}  {Colors.highlight(content, content_color)}"
                )
        else:
            # 无消息树或回复链：只显示单条消息
            timestamp = Colors.highlight(msg["timestamp"], Colors.TIMESTAMP)
            sender = msg.get("sender", "")
            content = msg.get("content", "")

            # 根据发送者判断颜色：风发的用蓝色，鹏发的用绿色
            if sender == MASTER_NAME:
                # 风：蓝色
                sender_display = Colors.highlight(MASTER_NAME, Colors.LABEL_BLUE)
                content_color = Colors.CONTENT_BLUE
                msg_id_color = Colors.LABEL_BLUE
            else:
                # 鹏：绿色
                sender_display = Colors.highlight(AGENT_NAME, Colors.LABEL_GREEN)
                content_color = Colors.CONTENT_GREEN
                msg_id_color = Colors.LABEL_GREEN

            # 格式化消息ID（使用正确的颜色）
            msg_id = _format_message_id(msg["id"], task_manager, msg_id_color)

            print(
                f"{timestamp} {msg_id} {sender_display}  {Colors.highlight(content, content_color)}"
            )

        print()

    print("=" * 50)
    print(f"共 {Colors.highlight(str(result['count']), Colors.YELLOW)} 条消息链")


def print_message_tree_node(node: dict, depth: int, task_manager=None) -> None:
    """递归打印消息树节点

    Args:
        node: 树节点（包含 message 和 children 字段）
        depth: 当前深度
        task_manager: 事务管理器（可选）
    """
    msg = node["message"]
    ctx_time = Colors.highlight(msg.get("timestamp", ""), Colors.TIMESTAMP)
    sender = msg.get("sender", "")

    # 根据发送者判断颜色：风发的用蓝色，鹏发的用绿色
    if sender == MASTER_NAME:
        # 风：蓝色
        sender_display = Colors.highlight(MASTER_NAME, Colors.LABEL_BLUE)
        content_color = Colors.CONTENT_BLUE
        msg_id_color = Colors.LABEL_BLUE
    else:
        # 鹏：绿色
        sender_display = Colors.highlight(AGENT_NAME, Colors.LABEL_GREEN)
        content_color = Colors.CONTENT_GREEN
        msg_id_color = Colors.LABEL_GREEN

    # 格式化消息ID（使用正确的颜色）
    msg_id = _format_message_id(msg["id"], task_manager, msg_id_color)

    # 截断消息为单行（用于终端显示）
    content = msg.get("content", "")
    # 将换行符替换为空格，保留在同一行显示
    content = content.replace("\n", " ").replace("\r", " ")
    # 如果仍然过长，截断到 100 字符
    if len(content) > 100:
        content = content[:97] + "..."

    # 根据深度缩进
    indent = "  " * depth

    # 如果有 reply_to 字段，显示 -> #reply_to_id（使用紫色）
    reply_to = msg.get("reply_to")
    reply_to_str = f"{Colors.highlight(f' → #{reply_to}', Colors.MAGENTA)}" if reply_to else ""

    print(
        f"{indent}{ctx_time} {msg_id}{reply_to_str} {sender_display}  {Colors.highlight(content, content_color)}"
    )

    # 递归打印子节点
    for child in node.get("children", []):
        print_message_tree_node(child, depth + 1, task_manager)


def print_search_result(result: dict):
    """打印搜索结果（复用 msg list --all 格式，保持完整上下文）"""
    if result.get("empty"):
        if result.get("error"):
            print(f"❌ {result['error']}")
        elif result.get("reason"):
            print(f"📭 暂无消息（{result['reason']}）")
        else:
            print(f"🔍 没有找到包含 '{result.get('keyword', '')}' 的消息")
        return

    keyword = result["keyword"]
    keyword_lower = keyword.lower()

    def highlight_keyword(text: str, content_color: str) -> str:
        """高亮关键词"""
        text_lower = text.lower()
        result_parts = []
        i = 0
        while i < len(text):
            pos = text_lower.find(keyword_lower, i)
            if pos == -1:
                result_parts.append(text[i:])
                break
            result_parts.append(text[i:pos])
            result_parts.append(
                Colors.highlight(text[pos : pos + len(keyword)], Colors.LABEL_GREEN)
            )
            i = pos + len(keyword)
        return Colors.highlight("".join(result_parts), content_color)

    try:
        from peng.message.manager import MessageManager

        mm = MessageManager()
        task_manager = mm._task_manager
    except Exception:
        task_manager = None

    print(f"🔍 搜索结果（关键词：{Colors.highlight(keyword, Colors.LABEL_GREEN)}）:")
    print("=" * 50)

    for msg in result["messages"]:
        reply_chain = msg.get("reply_chain", [])

        if reply_chain and len(reply_chain) > 1:
            for i, ctx in enumerate(reply_chain):
                ctx_time = Colors.highlight(ctx.get("timestamp", ""), Colors.TIMESTAMP)
                sender = ctx.get("sender", "")

                # 根据发送者判断颜色：风发的用蓝色，鹏发的用绿色
                if sender == MASTER_NAME:
                    # 风：蓝色
                    sender_display = Colors.highlight(MASTER_NAME, Colors.LABEL_BLUE)
                    content_color = Colors.CONTENT_BLUE
                    msg_id_color = Colors.LABEL_BLUE
                else:
                    # 鹏：绿色
                    sender_display = Colors.highlight(AGENT_NAME, Colors.LABEL_GREEN)
                    content_color = Colors.CONTENT_GREEN
                    msg_id_color = Colors.LABEL_GREEN

                ctx_id = _format_message_id(ctx["id"], task_manager, msg_id_color)

                # 保持完整内容，不截断
                content = ctx.get("content", "")
                highlighted = highlight_keyword(content, content_color)

                indent = "" if i == 0 else "  "
                print(f"{indent}{ctx_time} {ctx_id} {sender_display}  {highlighted}")
        else:
            timestamp = Colors.highlight(msg["timestamp"], Colors.TIMESTAMP)
            sender = msg.get("sender", "")

            # 根据发送者判断颜色：风发的用蓝色，鹏发的用绿色
            if sender == MASTER_NAME:
                # 风：蓝色
                sender_display = Colors.highlight(MASTER_NAME, Colors.LABEL_BLUE)
                content_color = Colors.CONTENT_BLUE
                msg_id_color = Colors.LABEL_BLUE
            else:
                # 鹏：绿色
                sender_display = Colors.highlight(AGENT_NAME, Colors.LABEL_GREEN)
                content_color = Colors.CONTENT_GREEN
                msg_id_color = Colors.LABEL_GREEN

            # 保持完整内容，不截断
            content = msg.get("content", "")
            highlighted = highlight_keyword(content, content_color)
            msg_id = _format_message_id(msg["id"], task_manager, msg_id_color)
            print(f"{timestamp} {msg_id} {sender_display}  {highlighted}")

        print()

    print("=" * 50)
    print(f"共找到 {Colors.highlight(str(result['count']), Colors.YELLOW)} 条匹配消息链")


def print_status_result(result: dict):
    """打印消息状态统计"""
    if result.get("empty"):
        if result.get("reason"):
            print(f"📭 暂无消息（{result['reason']}）")
        else:
            print("📭 暂无消息")
        return

    print("📊 消息处理状态:")
    print("=" * 50)
    print(f"📥 收件箱（{MASTER_NAME}的指示）:")
    inbox = result.get("inbox", {})
    print(f"   未读：{inbox.get('unread', 0)} 条")
    print(f"   处理中：{inbox.get('processing', 0)} 条")
    print(f"   完成：{inbox.get('done', 0)} 条")
    print(f"📤 发件箱（{AGENT_NAME}的反馈）: {result.get('outbox_count', 0)} 条")
    print("=" * 50)


def print_finished_result(result: dict):
    """打印已完成消息结果"""
    if result.get("empty"):
        print("📭 没有已完成的消息")
        return

    print(f"📋 最近 {len(result['messages'])} 条已完成的消息:")
    print("=" * 50)

    try:
        from peng.message.manager import MessageManager

        mm = MessageManager()
        task_manager = mm._task_manager
    except Exception:
        task_manager = None

    for msg in result["messages"]:
        content_lines = msg["content"].split("\n")
        receiver = msg.get("receiver", "")
        if receiver == AGENT_NAME:
            dir_icon, dir_text = ("📥", f"🌪 → {AGENT_NAME}")
            # 风→鹏：蓝色
            msg_id = _format_message_id(msg["id"], task_manager, Colors.LABEL_BLUE)
        else:
            dir_icon, dir_text = ("📤", f"{AGENT_NAME} → 🌪")
            # 鹏→风：绿色
            msg_id = _format_message_id(msg["id"], task_manager, Colors.LABEL_GREEN)
        status_icon = "✅" if msg["status"] == "done" else "📦"

        timestamp = Colors.highlight(msg["timestamp"], Colors.CYAN)

        print(f"{msg_id}. [{timestamp}] {dir_icon}{dir_text} {status_icon}")
        for line in content_lines[:5]:
            print(f"   {line}")
        if len(content_lines) > 5:
            print(f"   ... (还有 {len(content_lines) - 5} 行)")
        print()

    print("=" * 50)


def print_remove_result(result: dict):
    """打印删除消息结果"""
    if result.get("success"):
        print(f"即将删除消息 ID={result['msg_id']}:")
        deleted_messages = result.get("deleted_messages", 1)
        deleted_tasks = result.get("deleted_tasks", 0)
        msg_text = f"{Colors.GREEN}✅ 已删除 {deleted_messages} 条消息{Colors.RESET}"
        if deleted_tasks > 0:
            msg_text += f" 和 {deleted_tasks} 个事务"
        print(msg_text)
    else:
        print(f"{Colors.RED}❌ {result.get('error', '删除失败')}{Colors.RESET}")


def print_task_query_result(result: dict, message_id: int):
    """打印事务查询结果"""
    if result.get("empty"):
        print(f"消息 #{message_id} 暂无关联事务")
        print(f"{Colors.DIM}提示：复杂消息会自动创建事务{Colors.RESET}")
        return

    print(f"\n{Colors.BOLD}消息 #{message_id} 关联的事务{Colors.RESET}")
    print("=" * 60)

    status_colors = {
        "pending": Colors.YELLOW,
        "running": Colors.BLUE,
        "completed": Colors.GREEN,
        "failed": Colors.RED,
        "cancelled": Colors.DIM,
    }
    priority_names = {0: "CRITICAL", 1: "HIGH", 2: "NORMAL", 3: "LOW"}

    for task in result["tasks"]:
        task_id = task.get("id")
        status = task.get("status", "pending")
        title = task.get("title", "")
        priority = task.get("priority", 2)

        status_color = status_colors.get(status, Colors.WHITE)
        priority_name = priority_names.get(priority, str(priority))

        print(f"  事务 #{task_id} [{status_color}{status}{Colors.RESET}] ({priority_name})")
        print(f"    标题：{title}")

        # 显示子事务
        try:
            task_manager = cli_service.get_task_manager()
            child_tasks = task_manager.list_tasks()
            for child in child_tasks:
                if child.get("parent_task_id") == task_id:
                    child_status = child.get("status", "pending")
                    child_status_color = status_colors.get(child_status, Colors.WHITE)
                    line = (
                        f"    └── #{child['id']} [{child_status_color}"
                        f"{child_status}{Colors.RESET}] {child.get('content', '')}"
                    )
                    print(line)
        except Exception:
            pass

    print(f"\n{Colors.DIM}提示：使用 'task show <事务 ID>' 查看详情{Colors.RESET}")


def print_task_control_result(result: dict, action: str):
    """打印事务控制结果"""
    if result.get("success"):
        action_names = {
            "pause": "暂停",
            "cancel": "取消",
            "resume": "恢复",
            "restart": "重启",
            "up": "提高优先级",
            "down": "降低优先级",
        }
        action_name = action_names.get(action, action)
        msg = (
            f"{Colors.GREEN}✅ 已对消息 #{result['message_id']} 的 "
            f"{result['task_count']} 个事务执行 '{action_name}'{Colors.RESET}"
        )
        print(msg)
    else:
        print(f"{Colors.RED}❌ {result.get('error', '操作失败')}{Colors.RESET}")


def print_processing_result(result: dict):
    """打印处理中消息结果"""
    if result.get("empty"):
        print("没有处理中的消息")
        return

    status_colors = {
        "pending": Colors.YELLOW,
        "running": Colors.BLUE,
        "completed": Colors.GREEN,
        "failed": Colors.RED,
        "cancelled": Colors.DIM,
    }

    try:
        from peng.message.manager import MessageManager

        mm = MessageManager()
        task_manager = mm._task_manager
    except Exception:
        task_manager = None

    print(f"\n{Colors.BOLD}处理中的消息{Colors.RESET}")
    print("=" * 70)

    for item in result["messages"]:
        msg = item["message"]
        tasks = item["tasks"]
        chain = item["chain"]

        msg_id = msg.get("id")
        timeout = msg.get("timeout", 0)
        locked_at = msg.get("locked_at")

        # 根据发送者判断消息ID颜色
        sender = msg.get("sender", "")
        if sender == MASTER_NAME:
            # 风：蓝色
            msg_id_str = _format_message_id(msg_id, task_manager, Colors.LABEL_BLUE)
        else:
            # 鹏：绿色
            msg_id_str = _format_message_id(msg_id, task_manager, Colors.LABEL_GREEN)

        # 计算剩余时间
        remaining = ""
        if timeout > 0 and locked_at:
            try:
                locked_time = datetime.fromisoformat(locked_at)
                elapsed = (datetime.now() - locked_time).total_seconds()
                remaining_sec = max(0, timeout - elapsed)
                remaining = f", 剩余：{int(remaining_sec)}秒"
            except Exception:
                pass

        timeout_str = f"超时：{timeout}秒{remaining}" if timeout > 0 else "超时：永不超时"
        has_context = bool(msg.get("chain_context"))
        context_mark = f" {Colors.DIM}[含上下文]{Colors.RESET}" if has_context else ""

        is_paused = msg.get("is_paused", 0)
        is_cancelled = msg.get("is_cancelled", 0)

        if is_paused:
            line = (
                f"\n{Colors.CYAN}{msg_id_str}{Colors.RESET} "
                f"[{Colors.YELLOW}已暂停{Colors.RESET}] {timeout_str}{context_mark}"
            )
            print(line)
            print(f"  {Colors.YELLOW}[已暂停]{Colors.RESET}")
        elif is_cancelled:
            line = (
                f"\n{Colors.CYAN}{msg_id_str}{Colors.RESET} "
                f"[{Colors.RED}已中断{Colors.RESET}] {timeout_str}{context_mark}"
            )
            print(line)
            print(f"  {Colors.RED}[Request interrupted by user]{Colors.RESET}")
        else:
            line = (
                f"\n{Colors.CYAN}消息 #{msg_id}{Colors.RESET} "
                f"[{Colors.YELLOW}处理中{Colors.RESET}] ({timeout_str}){context_mark}"
            )
            print(line)

        content = msg.get("chain_context") or msg.get("content", "")
        content_lines = content.split("\n")
        for i, line in enumerate(content_lines[:10]):
            print(f"  {'├──' if i < len(content_lines) - 1 else '└──'} {line}")
        if len(content_lines) > 10:
            print(f"  └── ... (还有 {len(content_lines) - 10} 行)")

        # 显示关联事务
        if tasks:
            print("  关联事务:")
            for task in tasks:
                task_id = task.get("id")
                status = task.get("status", "pending")
                title = task.get("title", "")
                progress = task.get("progress", 0)

                status_color = status_colors.get(status, Colors.WHITE)
                line = (
                    f"    └── 事务 #{task_id} [{status_color}{status}"
                    f"{Colors.RESET}] {progress}% {title}"
                )
                print(line)
            print()

        # 显示回复链
        if chain:
            print("  回复链:")
            for chain_msg in chain:
                chain_content = (chain_msg.content or "")[:30] if chain_msg.content else ""
                suffix = "..." if len(chain_msg.content or "") > 30 else ""
                line = f"    └── 回复消息 #{chain_msg.id}: {chain_content}{suffix}"
                print(line)

    print(f"\n{Colors.DIM}共 {len(result['messages'])} 条处理中的消息{Colors.RESET}")


def print_lock_result(result: dict):
    """打印锁定消息结果"""
    if result.get("success"):
        print(f"✅ 已锁定消息 #{result['msg_id']}")
    else:
        print(f"❌ {result.get('error', '锁定失败')}")


def print_heartbeat_result(result: dict):
    """打印心跳更新结果"""
    if result.get("success"):
        print(f"✅ 消息 #{result['msg_id']} 心跳已更新")
    else:
        print(f"❌ {result.get('error', '无法更新心跳')}")


def print_timeout_result(result: dict):
    """打印超时设置结果"""
    if result.get("success"):
        line = (
            f"{Colors.GREEN}✅ 消息 #{result['msg_id']} 超时已设置为 "
            f"{result['timeout_str']}{Colors.RESET}"
        )
        print(line)
    else:
        print(f"{Colors.RED}❌ {result.get('error', '无法更新超时')}{Colors.RESET}")


def print_stale_check_result(result: dict):
    """打印失效消息检测结果"""
    count = result.get("count", 0)
    if count > 0:
        print(f"{Colors.GREEN}✅ 检测到 {count} 个失效的处理中消息，已标记为中断:{Colors.RESET}")
        for mid in result.get("interrupted_ids", []):
            print(f"   - 消息 #{mid}: [Request interrupted by user]")
    else:
        print(
            f"{Colors.YELLOW}⏳ {result.get('message', '没有检测到失效的处理中消息')}{Colors.RESET}"
        )


def print_paused_check_result(result: dict):
    """打印暂停消息检测结果"""
    count = result.get("count", 0)
    if count > 0:
        print(f"{Colors.GREEN}✅ 检测到 {count} 个被暂停的消息:{Colors.RESET}")
        for mid in result.get("paused_ids", []):
            print(f"   - 消息 #{mid}: [已暂停]")
    else:
        print(f"{Colors.YELLOW}⏳ {result.get('message', '没有检测到被暂停的消息')}{Colors.RESET}")


# ==================== 子命令处理函数 ====================


def _read_file_content(file_path: str) -> str:
    """从文件读取内容，用于 --file 参数"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        # 读取后删除临时文件
        try:
            os.remove(file_path)
        except OSError:
            pass
        return content
    except Exception as e:
        print(f"{Colors.RED}❌ 读取文件失败: {e}{Colors.RESET}", file=sys.stderr)
        return ""


def _inject_nest_workdir_defaults(args: list[str]) -> list[str]:
    """对 send 子命令自动注入 --nest 和 --workdir

    与 bin/msg 脚本的注入逻辑保持一致，确保 console_scripts 入口
    （.venv/bin/msg）在远程模式下也正确转发 nest 和 workdir。
    事务#2089：reply 不再注入——回复消息的 nest/workdir 由数据库层
    从链首消息继承，保证消息链上下文一致。

    Args:
        args: 命令行参数列表（不含 --local）

    Returns:
        可能追加了 --nest/--workdir 的参数列表
    """
    _msg_subcommands_needing_defaults = {"send"}
    _msg_all_subcommands = {
        "list", "trees", "show", "content", "title", "nest", "workdir",
        "delete", "status", "pause", "cancel", "restart", "resume",
        "up", "down", "search", "tree", "full-tree", "context", "move",
        "archive", "unarchive",
    }

    _first_arg = args[0] if args else None
    if _first_arg is None:
        return args

    # 判断是否需要注入
    needs_defaults = _first_arg in _msg_subcommands_needing_defaults or (
        not _first_arg.startswith("-")
        and _first_arg not in _msg_all_subcommands
    )
    if not needs_defaults:
        return args

    has_nest = "--nest" in args or "-n" in args
    has_workdir = "--workdir" in args or "-w" in args

    result = list(args)

    # --workdir 始终注入（如果未指定）
    if not has_workdir:
        result.extend(["--workdir", os.getcwd()])

    # --nest 注入：优先使用 get_self_nest_name()
    if not has_nest:
        from peng.cli.remote import get_self_nest_name

        self_nest = get_self_nest_name()
        if self_nest:
            result.extend(["--nest", self_nest])
        else:
            from peng.cli.remote import is_local_service_running, get_default_nest_name, is_default_nest_local

            if is_local_service_running():
                result.extend(["--nest", "local"])
            else:
                default_nest = get_default_nest_name()
                if default_nest and not is_default_nest_local():
                    result.extend(["--nest", default_nest])
                else:
                    result.extend(["--nest", "local"])

    return result


def _detect_local_nest_name() -> str:
    """检测当前 nest 名称

    优先级：
    1. get_self_nest_name()（self 字段或 instances 推断）
    2. 本地服务运行中 → 'local'
    3. 默认 nest 名称
    4. 'local'（最终 fallback）

    确保所有入口点（bin/msg 包装脚本、.venv/bin/msg console_scripts）统一行为。
    """
    from peng.cli.remote import get_self_nest_name, is_local_service_running, get_default_nest_name

    self_nest = get_self_nest_name()
    if self_nest:
        return self_nest
    if is_local_service_running():
        return "local"
    default_nest = get_default_nest_name()
    if default_nest:
        return default_nest
    return "local"


def cmd_send(args):
    """处理 send 子命令"""
    # 优先从 --file 参数读取内容
    file_path = getattr(args, "file", None)
    if file_path:
        message_content = _read_file_content(file_path)
        if not message_content and " ".join(args.message) if args.message else None:
            message_content = " ".join(args.message)
    else:
        message_content = " ".join(args.message) if args.message else None

    if not message_content:
        print(f"{Colors.RED}❌ 请提供消息内容{Colors.RESET}")
        return 1

    reply_id = args.reply if args.reply != -1 else None
    timeout = args.timeout or 0

    from_ = getattr(args, "from", None)
    to = getattr(args, "to", None)
    nest = getattr(args, "nest", None)
    workdir = getattr(args, "workdir", None)

    # 根消息（非回复）：未显式指定时，默认使用执行命令所在 nest 和 os.getcwd()
    if reply_id is None:
        if not workdir:
            workdir = os.getcwd()
        if not nest:
            nest = _detect_local_nest_name()

    # 如果指定了 nest 但没有指定 workdir，从 NestBridgeManager 或 peng service API 获取
    if nest and not workdir:
        workdir = _resolve_workdir_from_nest(nest)

    # 统一使用cli_service.send_message（底层调用db.create_message）
    message_content = get_text_from_args(message_content)
    result = cli_service.send_message(message_content, reply_id, timeout, from_, to, nest, workdir)

    print_send_result(result, reply_id, timeout)
    if result.get("success") is False:
        return 1
    return 0


def cmd_list(args):
    """处理 list 子命令"""
    if args.all:
        result = cli_service.read_all_messages(order=args.order)
        print_all_messages_result(result)
    elif args.outbox:
        result = cli_service.read_outbox(show_all=True)
        print_outbox_result(result, show_all=True)
    elif args.unread:
        result = cli_service.list_unread_inbox()
        print_unread_result(result)
    elif args.finished:
        result = cli_service.show_finished()
        print_finished_result(result)
    elif args.processing:
        result = cli_service.show_processing_messages()
        print_processing_result(result)
    else:
        # 默认显示未归档的根消息树
        result = cli_service.read_unarchived_messages(order=args.order)
        print_all_messages_result(result)
    return 0


def cmd_trees(args):
    """处理 trees 子命令（与 list 等效）"""
    # 完全复用 cmd_list 的逻辑
    if args.all:
        result = cli_service.read_all_messages(order=args.order)
        print_all_messages_result(result)
    elif args.outbox:
        result = cli_service.read_outbox(show_all=True)
        print_outbox_result(result, show_all=True)
    elif args.unread:
        result = cli_service.list_unread_inbox()
        print_unread_result(result)
    elif args.finished:
        result = cli_service.show_finished()
        print_finished_result(result)
    elif args.processing:
        result = cli_service.show_processing_messages()
        print_processing_result(result)
    else:
        # 默认显示未归档的根消息树
        result = cli_service.read_unarchived_messages(order=args.order)
        print_all_messages_result(result)
    return 0


def cmd_content(args):
    """消息内容操作"""
    # 获取消息
    from peng.message.database import MessageDatabase

    db = MessageDatabase()
    msg = db.get_message(args.id)

    if not msg:
        print(f"{Colors.RED}❌ 消息 {args.id} 不存在{Colors.RESET}", file=sys.stderr)
        return 1

    # 检查是否使用 -d --delete 参数清空字段
    delete_flag = getattr(args, "delete", False)
    if delete_flag:
        success = db.update_message(args.id, content="")
        if success:
            print(f"{Colors.GREEN}✅ 消息 {args.id} 内容已清空{Colors.RESET}")
            # 检查是否为根消息（reply_to IS NULL），如果是则同步更新关联的根事务content
            if msg.get("reply_to") is None:
                from peng.task.database import TaskDatabase

                task_db = TaskDatabase()
                # 查找关联的事务
                related_task = task_db.get_by_message(args.id)
                if related_task:
                    # 检查该事务是否为根事务（parent_task_id IS NULL）
                    task_info = related_task[0] if isinstance(related_task, list) else related_task
                    if not task_info.get("parent_task_id"):
                        # 同步更新根事务的content
                        task_success = task_db.update_task(task_info["id"], content="")
                        if task_success:
                            print(
                                f"{Colors.GREEN}  → 已同步清空根事务 #{task_info['id']} 的内容{Colors.RESET}"
                            )
                        else:
                            print(
                                f"{Colors.YELLOW}  ⚠️ 同步清空根事务 #{task_info['id']} 内容失败{Colors.RESET}",
                                file=sys.stderr,
                            )
        else:
            print(f"{Colors.RED}❌ 无法更新消息{Colors.RESET}", file=sys.stderr)
            return 1
        return 0

    # 获取内容参数 - 优先从 --file 参数读取
    file_path = getattr(args, "file", None)
    if file_path:
        content_value = [_read_file_content(file_path)]
    else:
        content_value = getattr(args, "content", None)

    # 判断操作类型
    # 如果没有 content 参数，显示内容
    if content_value is None or (isinstance(content_value, list) and len(content_value) == 0):
        value = msg.get("content", "")
        print()

        if value:
            print(f"{Colors.BLUE}{msg.get('sender')}：{Colors.RESET}{value}")
        else:
            print(f"{Colors.YELLOW}  (无内容){Colors.RESET}")
        return 0

    # 如果 content 参数是空字符串列表 [""]，删除内容
    if isinstance(content_value, list) and len(content_value) == 1 and content_value[0] == "":
        success = db.update_message(args.id, content="")
        if success:
            print(f"{Colors.GREEN}✅ 消息 {args.id} 内容已清空{Colors.RESET}")
            # 检查是否为根消息（reply_to IS NULL），如果是则同步更新关联的根事务content
            if msg.get("reply_to") is None:
                from peng.task.database import TaskDatabase

                task_db = TaskDatabase()
                # 查找关联的事务
                related_task = task_db.get_by_message(args.id)
                if related_task:
                    # 检查该事务是否为根事务（parent_task_id IS NULL）
                    task_info = related_task[0] if isinstance(related_task, list) else related_task
                    if not task_info.get("parent_task_id"):
                        # 同步更新根事务的content
                        task_success = task_db.update_task(task_info["id"], content="")
                        if task_success:
                            print(
                                f"{Colors.GREEN}  → 已同步清空根事务 #{task_info['id']} 的内容{Colors.RESET}"
                            )
                        else:
                            print(
                                f"{Colors.YELLOW}  ⚠️ 同步清空根事务 #{task_info['id']} 内容失败{Colors.RESET}",
                                file=sys.stderr,
                            )
        else:
            print(f"{Colors.RED}❌ 无法更新消息{Colors.RESET}", file=sys.stderr)
            return 1
        return 0

    # 如果 content 参数有内容，设置内容（合并多个参数为单个字符串）
    if isinstance(content_value, list):
        content_string = " ".join(content_value)
    else:
        content_string = content_value
    # 将 \n 转换为真正的换行符
    content_string = get_text_from_args(content_string)
    success = db.update_message(args.id, content=content_string)
    if success:
        print(f"{Colors.GREEN}✅ 消息 {args.id} 内容已更新{Colors.RESET}")
        print(f"  内容：{content_string}")

        # 检查是否为根消息（reply_to IS NULL），如果是则同步更新关联的根事务content
        if msg.get("reply_to") is None:
            from peng.task.database import TaskDatabase

            task_db = TaskDatabase()
            # 查找关联的事务
            related_task = task_db.get_by_message(args.id)
            if related_task:
                # 检查该事务是否为根事务（parent_task_id IS NULL）
                task_info = related_task[0] if isinstance(related_task, list) else related_task
                if not task_info.get("parent_task_id"):
                    # 同步更新根事务的content
                    task_success = task_db.update_task(task_info["id"], content=content_string)
                    if task_success:
                        print(
                            f"{Colors.GREEN}  → 已同步更新根事务 #{task_info['id']} 的内容{Colors.RESET}"
                        )
                    else:
                        print(
                            f"{Colors.YELLOW}  ⚠️ 同步更新根事务 #{task_info['id']} 内容失败{Colors.RESET}",
                            file=sys.stderr,
                        )
    else:
        print(f"{Colors.RED}❌ 无法更新消息{Colors.RESET}", file=sys.stderr)
        return 1

    return 0


def cmd_title(args):
    """消息标题操作"""
    # 获取消息
    from peng.message.database import MessageDatabase

    db = MessageDatabase()
    msg = db.get_message(args.id)

    if not msg:
        print(f"{Colors.RED}❌ 消息 {args.id} 不存在{Colors.RESET}", file=sys.stderr)
        return 1

    # 检查是否使用 -d --delete 参数清空字段
    delete_flag = getattr(args, "delete", False)
    if delete_flag:
        success = db.update_message(args.id, title="")
        if success:
            print(f"{Colors.GREEN}✅ 消息 {args.id} 标题已清空{Colors.RESET}")
        else:
            print(f"{Colors.RED}❌ 无法更新消息{Colors.RESET}", file=sys.stderr)
            return 1
        return 0

    # 获取标题参数 - 优先从 --file 参数读取
    file_path = getattr(args, "file", None)
    if file_path:
        title_value = [_read_file_content(file_path)]
    else:
        title_value = getattr(args, "title", None)

    # 判断操作类型
    # 如果没有 title 参数，显示标题
    if title_value is None or (isinstance(title_value, list) and len(title_value) == 0):
        value = msg.get("title", "")
        print(f"{Colors.CYAN}消息 {args.id} 的标题{Colors.RESET}")
        print()

        if value:
            print(f"  标题：{value}")
        else:
            print(f"{Colors.YELLOW}  (无标题){Colors.RESET}")
        return 0

    # 如果 title 参数是空字符串列表 [""]，删除标题
    if isinstance(title_value, list) and len(title_value) == 1 and title_value[0] == "":
        success = db.update_message(args.id, title="")
        if success:
            print(f"{Colors.GREEN}✅ 消息 {args.id} 标题已清空{Colors.RESET}")
        else:
            print(f"{Colors.RED}❌ 无法更新消息{Colors.RESET}", file=sys.stderr)
            return 1
        return 0

    # 如果 title 参数有内容，设置标题（合并多个参数为单个字符串）
    if isinstance(title_value, list):
        title_string = " ".join(title_value)
    else:
        title_string = title_value

    # 将 \n 转换为真正的换行符
    title_string = title_string.replace("\\n", "\n")

    success = db.update_message(args.id, title=title_string)

    if success:
        print(f"{Colors.GREEN}✅ 消息 {args.id} 标题已更新{Colors.RESET}")
        print(f"  标题：{title_string}")
    else:
        print(f"{Colors.RED}❌ 无法更新消息{Colors.RESET}", file=sys.stderr)
        return 1

    return 0


def cmd_nest(args):
    """消息鹏🐣窝操作"""
    # 获取消息
    from peng.message.database import MessageDatabase

    db = MessageDatabase()
    msg = db.get_message(args.id)

    if not msg:
        print(f"{Colors.RED}❌ 消息 {args.id} 不存在{Colors.RESET}", file=sys.stderr)
        return 1

    # 检查是否使用 -d --delete 参数清空字段
    delete_flag = getattr(args, "delete", False)
    if delete_flag:
        success = db.update_message(args.id, nest="")
        if success:
            print(f"{Colors.GREEN}✅ 消息 {args.id} 鹏🐣窝已清空{Colors.RESET}")
        else:
            print(f"{Colors.RED}❌ 无法更新消息{Colors.RESET}", file=sys.stderr)
            return 1
        return 0

    # 获取nest参数 - 优先从 --file 参数读取
    file_path = getattr(args, "file", None)
    if file_path:
        nest_value = [_read_file_content(file_path)]
    else:
        nest_value = getattr(args, "nest", None)

    # 判断操作类型
    # 如果没有 nest 参数，显示鹏🐣窝
    if nest_value is None or (isinstance(nest_value, list) and len(nest_value) == 0):
        value = msg.get("nest", "")
        print(f"{Colors.CYAN}消息 {args.id} 的鹏🐣窝{Colors.RESET}")
        print()

        if value:
            print(f"  鹏🐣窝：{value}")
        else:
            print(f"{Colors.YELLOW}  (未设置){Colors.RESET}")
        return 0

    # 如果 nest 参数是空字符串列表 [""]，清空鹏🐣窝
    if isinstance(nest_value, list) and len(nest_value) == 1 and nest_value[0] == "":
        success = db.update_message(args.id, nest="")
        if success:
            print(f"{Colors.GREEN}✅ 消息 {args.id} 鹏🐣窝已清空{Colors.RESET}")
        else:
            print(f"{Colors.RED}❌ 无法更新消息{Colors.RESET}", file=sys.stderr)
            return 1
        return 0

    # 如果 nest 参数有内容，设置鹏🐣窝（合并多个参数为单个字符串）
    if isinstance(nest_value, list):
        nest_string = " ".join(nest_value)
    else:
        nest_string = nest_value

    # 将 \n 转换为真正的换行符
    nest_string = nest_string.replace("\\n", "\n")

    success = db.update_message(args.id, nest=nest_string)

    if success:
        print(f"{Colors.GREEN}✅ 消息 {args.id} 鹏🐣窝已更新{Colors.RESET}")
        print(f"  鹏🐣窝：{nest_string}")
    else:
        print(f"{Colors.RED}❌ 无法更新消息{Colors.RESET}", file=sys.stderr)
        return 1

    return 0


def cmd_workdir(args):
    """消息工作目录操作"""
    # 获取消息
    from peng.message.database import MessageDatabase

    db = MessageDatabase()
    msg = db.get_message(args.id)

    if not msg:
        print(f"{Colors.RED}❌ 消息 {args.id} 不存在{Colors.RESET}", file=sys.stderr)
        return 1

    # 检查是否使用 -d --delete 参数清空字段
    delete_flag = getattr(args, "delete", False)
    if delete_flag:
        success = db.update_message(args.id, workdir="")
        if success:
            print(f"{Colors.GREEN}✅ 消息 {args.id} 工作目录已清空{Colors.RESET}")
        else:
            print(f"{Colors.RED}❌ 无法更新消息{Colors.RESET}", file=sys.stderr)
            return 1
        return 0

    # 获取workdir参数 - 优先从 --file 参数读取
    file_path = getattr(args, "file", None)
    if file_path:
        workdir_value = [_read_file_content(file_path)]
    else:
        workdir_value = getattr(args, "workdir", None)

    # 判断操作类型
    # 如果没有 workdir 参数，显示工作目录
    if workdir_value is None or (isinstance(workdir_value, list) and len(workdir_value) == 0):
        value = msg.get("workdir", "")
        print(f"{Colors.CYAN}消息 {args.id} 的工作目录{Colors.RESET}")
        print()

        if value:
            print(f"  工作目录：{value}")
        else:
            print(f"{Colors.YELLOW}  (未设置){Colors.RESET}")
        return 0

    # 如果 workdir 参数是空字符串列表 [""]，清空工作目录
    if isinstance(workdir_value, list) and len(workdir_value) == 1 and workdir_value[0] == "":
        success = db.update_message(args.id, workdir="")
        if success:
            print(f"{Colors.GREEN}✅ 消息 {args.id} 工作目录已清空{Colors.RESET}")
        else:
            print(f"{Colors.RED}❌ 无法更新消息{Colors.RESET}", file=sys.stderr)
            return 1
        return 0

    # 如果 workdir 参数有内容，设置工作目录（合并多个参数为单个字符串）
    if isinstance(workdir_value, list):
        workdir_string = " ".join(workdir_value)
    else:
        workdir_string = workdir_value

    # 将 \n 转换为真正的换行符
    workdir_string = workdir_string.replace("\\n", "\n")

    success = db.update_message(args.id, workdir=workdir_string)

    if success:
        print(f"{Colors.GREEN}✅ 消息 {args.id} 工作目录已更新{Colors.RESET}")
        print(f"  工作目录：{workdir_string}")
    else:
        print(f"{Colors.RED}❌ 无法更新消息{Colors.RESET}", file=sys.stderr)
        return 1

    return 0


def _build_msg_info_table(msg: dict) -> "Table":
    """构建消息基本信息表格"""
    from rich.table import Table
    from rich.text import Text

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="cyan bold", width=12)
    table.add_column()

    table.add_row("ID", str(msg.get("id")))
    table.add_row("发送者", msg.get("sender", ""))
    table.add_row("接收者", msg.get("receiver", ""))

    # 状态
    status = msg.get("status", "unread")
    status_styles = {
        "unread": "yellow",
        "processing": "blue",
        "read": "green",
        "archived": "dim",
    }
    status_names = {
        "unread": "未读",
        "processing": "处理中",
        "read": "已读",
        "archived": "已归档",
    }
    status_style = status_styles.get(status, "")
    status_name = status_names.get(status, status)
    table.add_row("状态", Text(status_name, style=status_style))

    return table


def _build_msg_time_table(msg: dict) -> "Table":
    """构建消息时间表格"""
    from rich.table import Table

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="cyan bold", width=12)
    table.add_column()

    table.add_row("时间戳", msg.get("timestamp", "-"))
    table.add_row("创建", format_datetime(msg.get("created_at")))
    table.add_row("读取", format_datetime(msg.get("read_at")))
    table.add_row("归档", format_datetime(msg.get("archived_at")))

    return table


def _build_msg_concurrency_table(msg: dict) -> "Table":
    """构建消息并发控制表格"""
    from rich.table import Table

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="cyan bold", width=12)
    table.add_column()

    table.add_row("锁定者", str(msg.get("locked_by", "-")))
    table.add_row("锁定时间", format_datetime(msg.get("locked_at")))
    table.add_row("心跳", format_datetime(msg.get("heartbeat_at")))
    table.add_row("进程ID", str(msg.get("process_id", "-")))
    table.add_row("超时", f"{msg.get('timeout', 0)} 秒")

    return table


def _build_msg_chain_table(msg: dict) -> "Table":
    """构建消息回复链表格"""
    from rich.table import Table

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="cyan bold", width=12)
    table.add_column()

    reply_to = msg.get("reply_to")
    table.add_row("回复消息", f"#{reply_to}" if reply_to else "-")
    chain_head = msg.get("chain_head_id")
    table.add_row("链头消息", f"#{chain_head}" if chain_head else "-")
    table.add_row("是否链头", "是" if msg.get("is_chain_head") else "否")

    return table


def _build_msg_workdir_table(msg: dict) -> "Table":
    """构建消息工作目录表格"""
    from rich.table import Table

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="cyan bold", width=12)
    table.add_column()

    table.add_row("鹏🐣 窝", msg.get("nest", "-"))
    table.add_row("工作目录", msg.get("workdir", "-"))

    return table


def _build_msg_control_table(msg: dict) -> "Table":
    """构建消息控制状态表格"""
    from rich.table import Table

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="cyan bold", width=12)
    table.add_column()

    table.add_row("是否取消", "是" if msg.get("is_cancelled") else "否")
    table.add_row("是否暂停", "是" if msg.get("is_paused") else "否")
    table.add_row("是否折叠", "是" if msg.get("collapsed") else "否")
    table.add_row("是否归档", "是" if msg.get("is_archived") else "否")
    table.add_row("待删除", "是" if msg.get("pending_delete") else "否")

    return table


def _build_msg_caller_table(msg: dict) -> "Table":
    """构建消息调用信息表格"""
    from rich.table import Table

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="cyan bold", width=12)
    table.add_column()

    table.add_row("调用来源", msg.get("caller", "-"))
    table.add_row("调用会话", msg.get("caller_session_id", "-"))

    return table


def cmd_show(args):
    """处理 show 子命令（Rich Markdown 格式）"""
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.text import Text
    from rich.theme import Theme

    # 如果指定了 --task，显示消息关联的事务
    if args.task:
        if not args.id:
            print(f"{Colors.RED}❌ 请先指定消息ID{Colors.RESET}")
            return 1
        result = cli_service.get_tasks_by_message(args.id)
        print_task_query_result(result, args.id)
        return 0

    # 默认：显示消息详情
    if not args.id:
        print(f"{Colors.RED}❌ 请指定消息ID{Colors.RESET}")
        print(f"用法: msg show <消息ID> [--task]{Colors.RESET}")
        return 1

    # 获取消息
    db = cli_service.get_message_database()
    msg = db.get_message(args.id)

    if not msg:
        print(f"{Colors.RED}❌ 消息 #{args.id} 不存在{Colors.RESET}")
        return 1

    # 自定义 Markdown 主题：标题用粗体 cyan，code 用粗体 magenta，无背景色
    md_theme = Theme({
        "markdown.heading": "bold cyan",
        "markdown.h1": "bold cyan",
        "markdown.h2": "bold cyan",
        "markdown.h3": "bold cyan",
        "markdown.h4": "bold cyan",
        "markdown.code": "bold magenta",
        "markdown.code_block": "bold magenta",
        "markdown.item.bullet": "bold",
        "markdown.item.number": "bold",
    })
    console = Console(theme=md_theme)

    # ── 标题 ──
    console.print()
    console.print(Text(f"消息 #{args.id}", style="bold cyan"))

    # ── 内容 ──
    content = msg.get("content", "")
    if content:
        console.print()
        if len(content) > 1000:
            console.print(Markdown(fix_markdown_linebreaks(content[:1000] + "\n\n*... (已截断)*")))
        else:
            console.print(Markdown(fix_markdown_linebreaks(content)))

    # ── 基本信息 ──
    console.print()
    console.print(Text("📋 基本信息", style="bold yellow"))
    info_table = _build_msg_info_table(msg)
    console.print(info_table)

    # ── 时间 ──
    console.print()
    console.print(Text("⏰ 时间", style="bold yellow"))
    time_table = _build_msg_time_table(msg)
    console.print(time_table)

    # ── 并发控制 ──
    has_concurrency = msg.get("locked_by") or msg.get("locked_at") or msg.get("heartbeat_at") or msg.get("process_id")
    if has_concurrency or msg.get("timeout", 0) > 0:
        console.print()
        console.print(Text("🔒 并发控制", style="bold yellow"))
        concurrency_table = _build_msg_concurrency_table(msg)
        console.print(concurrency_table)

    # ── 回复链 ──
    console.print()
    console.print(Text("🔗 回复链", style="bold yellow"))
    chain_table = _build_msg_chain_table(msg)
    console.print(chain_table)

    # ── 工作环境 ──
    if msg.get("nest") or msg.get("workdir"):
        console.print()
        console.print(Text("📁 工作环境", style="bold yellow"))
        workdir_table = _build_msg_workdir_table(msg)
        console.print(workdir_table)

    # ── 链上下文 ──
    if msg.get("chain_context"):
        chain_context = msg.get("chain_context", "")
        console.print()
        console.print(Text("📜 链上下文", style="bold yellow"))
        if len(chain_context) > 1000:
            console.print(Markdown(fix_markdown_linebreaks(chain_context[:1000] + "\n\n*... (已截断)*")))
        else:
            console.print(Markdown(fix_markdown_linebreaks(chain_context)))

    # ── 控制状态 ──
    is_cancelled = msg.get("is_cancelled")
    is_paused = msg.get("is_paused")
    collapsed = msg.get("collapsed")
    is_archived = msg.get("is_archived")
    pending_delete = msg.get("pending_delete")
    if is_cancelled or is_paused or collapsed or is_archived or pending_delete:
        console.print()
        console.print(Text("🎛️ 控制状态", style="bold yellow"))
        control_table = _build_msg_control_table(msg)
        console.print(control_table)

    # ── 调用信息 ──
    if msg.get("caller") or msg.get("caller_session_id"):
        console.print()
        console.print(Text("📞 调用信息", style="bold yellow"))
        caller_table = _build_msg_caller_table(msg)
        console.print(caller_table)

    # ── 关联事务 ──
    try:
        from peng.message.manager import MessageManager

        mm = MessageManager()
        task_id = mm.get_task_id(args.id)
        if task_id:
            console.print()
            console.print(Text("📎 关联事务", style="bold yellow"))
            task_manager = mm._task_manager
            if task_manager:
                task = task_manager.get_task(task_id)
                if task:
                    task_status = task.get("status", "pending")
                    status_styles = {
                        "pending": "yellow",
                        "running": "blue",
                        "completed": "green",
                        "failed": "bold red",
                        "cancelled": "dim",
                    }
                    status_names = {
                        "pending": "待执行",
                        "running": "执行中",
                        "completed": "已完成",
                        "failed": "已失败",
                        "cancelled": "已取消",
                    }
                    status_style = status_styles.get(task_status, "")
                    status_name = status_names.get(task_status, task_status)
                    console.print(f"  事务 #{task_id} ", Text(status_name, style=status_style))
                else:
                    console.print(f"  事务 #{task_id}")
    except Exception:
        pass

    console.print()
    return 0


def cmd_reply(args):
    """处理 reply 子命令"""
    # 优先从 --file 参数读取内容
    file_path = getattr(args, "file", None)
    if file_path:
        message_content = _read_file_content(file_path)
        if not message_content and (" ".join(args.message) if args.message else None):
            message_content = " ".join(args.message)
    else:
        message_content = " ".join(args.message) if args.message else None

    if not message_content:
        print(f"{Colors.RED}❌ 请提供消息内容{Colors.RESET}")
        return 1

    if not args.id:
        print(f"{Colors.RED}❌ 请指定要回复的消息 ID{Colors.RESET}")
        return 1

    # 获取发送者和接收者
    # 回复消息的默认值：用户回复鹏，所以from_是master，to是鹏
    from_ = getattr(args, "from", None)
    to = getattr(args, "to", None)
    nest = getattr(args, "nest", None)
    workdir = getattr(args, "workdir", None)

    # 如果指定了 nest 但没有指定 workdir，从 NestBridgeManager 或 peng service API 获取
    if nest and not workdir:
        workdir = _resolve_workdir_from_nest(nest)

    # 统一使用cli_service.send_message（底层调用db.create_message）
    timeout = args.timeout or 0
    message_content = get_text_from_args(message_content)
    msg = cli_service.send_message(message_content, args.id, timeout, from_, to, nest, workdir)
    # 显示结果
    if msg.get("success") is False:
        print(f"{Colors.RED}❌ {msg.get('error', '发送失败')}{Colors.RESET}")
        return 1

    # 获取消息ID
    msg_id = msg.get("id")
    sender_name = msg.get("sender")
    receiver_name = msg.get("receiver")
    message_content = msg.get("content")
    lines = (
        f"\n{Colors.GREEN}✅ 成功回复{receiver_name}  "
        f"[{msg['timestamp']}] (#{msg_id} -> #{args.id}){Colors.RESET}\n"
    )
    print(lines)
    return 0


def cmd_delete(args):
    """处理 delete 子命令"""
    msg_manager = cli_service.get_message_manager()
    force = True if args.force else False
    if not args.id:
        print(f"{Colors.RED}❌ 请指定要删除的消息 ID{Colors.RESET}")
        return 1

    result = msg_manager.delete_message(args.id, force)
    print_remove_result(result)
    return 0 if result.get("success") else 1


def cmd_status(args):
    """处理 status 子命令"""
    from peng.message.database import MessageDatabase

    db = MessageDatabase()

    # 如果提供了消息ID，处理单个消息的状态
    if args.id is not None:
        # 如果提供了状态值，设置消息状态
        if args.status_value is not None:
            # 先验证消息是否存在
            msg = db.get_message(args.id)
            if not msg:
                print(f"{Colors.RED}❌ 消息 #{args.id} 不存在{Colors.RESET}")
                return 1

            # 直接更新状态
            if db.update_message_status(args.id, args.status_value):
                print(
                    f"{Colors.GREEN}✅ 消息 #{args.id} 状态已设置为 {args.status_value}{Colors.RESET}"
                )
                return 0
            else:
                print(f"{Colors.RED}❌ 设置状态失败{Colors.RESET}")
                return 1
        else:
            # 没有状态值，显示消息的当前状态
            msg = db.get_message(args.id)
            if not msg:
                print(f"{Colors.RED}❌ 消息 #{args.id} 不存在{Colors.RESET}")
                return 1

            status = msg.get("status", "unknown")
            status_colors = {
                "unread": Colors.YELLOW,
                "processing": Colors.BLUE,
                "read": Colors.GREEN,
                "done": Colors.GREEN,
                "failed": Colors.RED,
                "archived": Colors.DIM,
            }
            status_names = {
                "unread": "未读",
                "processing": "处理中",
                "read": "已读",
                "done": "完成",
                "failed": "失败",
                "archived": "已归档",
            }
            status_color = status_colors.get(status, Colors.WHITE)
            status_name = status_names.get(status, status)
            print(f"消息 #{args.id} 的状态：{status_color}{status_name}{Colors.RESET}")
            return 0

    # 没有提供消息ID，使用原有逻辑
    if args.check_stale:
        result = cli_service.check_stale_messages()
        print_stale_check_result(result)
    elif args.check_paused:
        result = cli_service.check_paused_messages()
        print_paused_check_result(result)
    else:
        result = cli_service.show_status()
        print_status_result(result)
    return 0


def cmd_pause(args):
    """处理 pause 子命令"""
    if not args.id:
        print(f"{Colors.RED}❌ 请指定消息 ID{Colors.RESET}")
        return 1

    result = cli_service.control_message_task(args.id, "pause")
    print_task_control_result(result, "pause")
    return 0 if result.get("success") else 1


def cmd_cancel(args):
    """处理 cancel 子命令"""
    if not args.id:
        print(f"{Colors.RED}❌ 请指定消息 ID{Colors.RESET}")
        return 1

    result = cli_service.control_message_task(args.id, "cancel")
    print_task_control_result(result, "cancel")
    return 0 if result.get("success") else 1


def cmd_restart(args):
    """处理 restart 子命令"""
    if not args.id:
        print(f"{Colors.RED}❌ 请指定消息 ID{Colors.RESET}")
        return 1

    result = cli_service.control_message_task(args.id, "restart")
    print_task_control_result(result, "restart")
    return 0 if result.get("success") else 1


def cmd_resume(args):
    """处理 resume 子命令"""
    if not args.id:
        print(f"{Colors.RED}❌ 请指定消息 ID{Colors.RESET}")
        return 1

    result = cli_service.control_message_task(args.id, "resume")
    print_task_control_result(result, "resume")
    return 0 if result.get("success") else 1


def cmd_up(args):
    """处理 up 子命令"""
    if not args.id:
        print(f"{Colors.RED}❌ 请指定消息 ID{Colors.RESET}")
        return 1

    result = cli_service.control_message_task(args.id, "up")
    print_task_control_result(result, "up")
    return 0 if result.get("success") else 1


def cmd_down(args):
    """处理 down 子命令"""
    if not args.id:
        print(f"{Colors.RED}❌ 请指定消息 ID{Colors.RESET}")
        return 1

    result = cli_service.control_message_task(args.id, "down")
    print_task_control_result(result, "down")
    return 0 if result.get("success") else 1


def cmd_search(args):
    """处理 search 子命令"""
    if not args.keyword:
        print(f"{Colors.RED}❌ 请提供搜索关键词{Colors.RESET}")
        return 1

    result = cli_service.search_messages(args.keyword)
    print_search_result(result)
    return 0


def cmd_tree(args):
    """处理 tree 子命令 - 显示消息的分支树（从该消息开始的子树）"""
    result = cli_service.get_message_tree(args.id)

    if result.get("empty"):
        reason = result.get("reason", "")
        print(f"{Colors.YELLOW}📭 {reason}{Colors.RESET}")
        return 0

    tree = result.get("tree")
    if not tree:
        print(f"{Colors.YELLOW}📭 无法获取消息树{Colors.RESET}")
        return 0

    # 找到指定消息的节点
    from peng.message.types import MessageTree

    if not isinstance(tree, MessageTree):
        print(f"{Colors.YELLOW}📭 无法解析消息树{Colors.RESET}")
        return 0

    node = tree.find_node(args.id)
    if not node:
        print(f"{Colors.YELLOW}📭 消息 #{args.id} 不在消息树中{Colors.RESET}")
        return 0

    print(f"\n{Colors.BOLD}消息 #{args.id} 的分支树{Colors.RESET}")
    print("=" * 70)

    # 获取事务管理器
    try:
        from peng.message.manager import MessageManager

        mm = MessageManager()
        task_manager = mm._task_manager
    except Exception:
        task_manager = None

    # 打印分支树（从指定节点开始）
    root_dict = node.to_dict()
    print_message_tree_node(root_dict, 0, task_manager)

    return 0


def cmd_full_tree(args):
    """处理 full-tree 子命令 - 显示消息的完整树（从树根开始）"""
    result = cli_service.get_message_tree(args.id)

    if result.get("empty"):
        reason = result.get("reason", "")
        print(f"{Colors.YELLOW}📭 {reason}{Colors.RESET}")
        return 0

    tree = result.get("tree")
    if not tree:
        print(f"{Colors.YELLOW}📭 无法获取消息树{Colors.RESET}")
        return 0

    msg_id = result.get("message_id")
    print(f"\n{Colors.BOLD}消息 #{args.id} 的完整树（树根 #{msg_id}）{Colors.RESET}")
    print("=" * 70)

    # 获取事务管理器
    try:
        from peng.message.manager import MessageManager

        mm = MessageManager()
        task_manager = mm._task_manager
    except Exception:
        task_manager = None

    # 打印完整消息树（从树根开始）
    from peng.message.types import MessageTree

    if isinstance(tree, MessageTree):
        root_dict = tree.root.to_dict()
        print_message_tree_node(root_dict, 0, task_manager)
    else:
        print(f"{Colors.YELLOW}📭 无法解析消息树{Colors.RESET}")

    return 0


def cmd_context(args):
    """处理 context 子命令 - 重新构造上下文并更新数据库"""
    db = cli_service.get_message_database()

    msg = db.get_message(args.id)
    if not msg:
        print(f"{Colors.RED}❌ 消息 #{args.id} 不存在{Colors.RESET}")
        return 1

    context = db.build_context_for_message(args.id)
    if not context:
        print(f"{Colors.RED}❌ 无法构建上下文{Colors.RESET}")
        return 1

    print(context)
    return 0


def cmd_move(args):
    """处理 move 子命令 - 移动消息到新的父节点或变成树根"""
    msg_manager = cli_service.get_message_manager()
    result = msg_manager.move_message(args.id, args.target)

    if result.get("success"):
        print(f"{Colors.GREEN}✅ {result['message']}{Colors.RESET}")
        return 0
    else:
        print(f"{Colors.RED}❌ {result['message']}{Colors.RESET}", file=sys.stderr)
        return 1


def cmd_archive(args):
    """处理 archive 子命令 - 归档消息"""
    from peng.message.database import MessageDatabase

    db = MessageDatabase()
    msg = db.get_message(args.id)
    if not msg:
        print(f"{Colors.RED}❌ 消息 #{args.id} 不存在{Colors.RESET}", file=sys.stderr)
        return 1

    if msg.get("is_archived"):
        print(f"{Colors.YELLOW}消息 #{args.id} 已经是已归档状态{Colors.RESET}")
        return 0

    if db.mark_archived(args.id):
        print(f"{Colors.GREEN}✅ 消息 #{args.id} 已归档{Colors.RESET}")
        return 0
    else:
        print(f"{Colors.RED}❌ 归档消息 #{args.id} 失败{Colors.RESET}", file=sys.stderr)
        return 1


def cmd_unarchive(args):
    """处理 unarchive 子命令 - 取消归档消息"""
    from peng.message.database import MessageDatabase

    db = MessageDatabase()
    msg = db.get_message(args.id)
    if not msg:
        print(f"{Colors.RED}❌ 消息 #{args.id} 不存在{Colors.RESET}", file=sys.stderr)
        return 1

    if not msg.get("is_archived"):
        print(f"{Colors.YELLOW}消息 #{args.id} 当前不是已归档状态{Colors.RESET}")
        return 0

    if db.unarchive_message(args.id):
        print(f"{Colors.GREEN}✅ 消息 #{args.id} 已取消归档{Colors.RESET}")
        return 0
    else:
        print(f"{Colors.RED}❌ 取消归档消息 #{args.id} 失败{Colors.RESET}", file=sys.stderr)
        return 1


# ==================== 参数解析 ====================


def create_parser():
    """创建 argparse 解析器"""
    parser = argparse.ArgumentParser(
        prog="msg",
        description=f"{AGENT_NAME}的消息系统 - {MASTER_NAME}与{AGENT_NAME}的通信管道",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # 创建子命令解析器
    subparsers = parser.add_subparsers(dest="command", help="可用子命令")

    # ========== send 子命令 ==========
    send_parser = subparsers.add_parser("send", help=f"发送消息给{AGENT_NAME}（默认子命令）")
    send_parser.add_argument("message", nargs="*", help="消息内容")
    send_parser.add_argument(
        "-r", "--reply", type=int, nargs="?", const=-1, default=None, help="回复指定消息ID"
    )
    send_parser.add_argument("--timeout", type=int, help="设置超时时间（秒）")
    send_parser.add_argument("--to", type=str, help="指定接收者")
    send_parser.add_argument("--from", type=str, help="指定发送者")
    send_parser.add_argument("--file", type=str, help="从文件读取消息内容")
    send_parser.add_argument("-n", "--nest", type=str, help="指定关联的nest名称")
    send_parser.add_argument("-w", "--workdir", type=str, help="指定工作目录")

    # ========== list 子命令 ==========
    list_parser = subparsers.add_parser("list", help="列出消息")
    list_parser.add_argument("-a", "--all", action="store_true", help="查看所有消息")
    list_parser.add_argument("-i", "--inbox", action="store_true", help="列出收件箱")
    list_parser.add_argument("-o", "--outbox", action="store_true", help="显示发件箱（所有）")
    list_parser.add_argument("-u", "--unread", action="store_true", help="只显示未读的收件箱消息")
    list_parser.add_argument("-f", "--finished", action="store_true", help="显示最近已完成消息")
    list_parser.add_argument("--processing", action="store_true", help="查询所有处理中的消息")
    list_parser.add_argument(
        "--order",
        type=str,
        default="ASC",
        choices=["ASC", "DESC"],
        help="排序顺序：ASC=从老到新（默认），DESC=从新到老",
    )

    # ========== trees 子命令 ==========
    trees_parser = subparsers.add_parser("trees", help="列出消息树（与 list 等效）")
    trees_parser.add_argument("-a", "--all", action="store_true", help="查看所有消息")
    trees_parser.add_argument("-i", "--inbox", action="store_true", help="列出收件箱")
    trees_parser.add_argument("-o", "--outbox", action="store_true", help="显示发件箱（所有）")
    trees_parser.add_argument("-u", "--unread", action="store_true", help="只显示未读的收件箱消息")
    trees_parser.add_argument("-f", "--finished", action="store_true", help="显示最近已完成消息")
    trees_parser.add_argument("--processing", action="store_true", help="查询所有处理中的消息")
    trees_parser.add_argument(
        "--order",
        type=str,
        default="ASC",
        choices=["ASC", "DESC"],
        help="排序顺序：ASC=从老到新（默认），DESC=从新到老",
    )

    # ========== show 子命令 ==========
    show_parser = subparsers.add_parser("show", help="显示消息详情")
    show_parser.add_argument("id", type=int, nargs="?", help="要显示的消息ID")
    show_parser.add_argument("--task", action="store_true", help="显示消息关联的事务")

    # ========== content 子命令 ==========
    content_parser = subparsers.add_parser("content", help="消息内容操作")
    content_parser.add_argument("id", type=int, help="消息ID")
    content_parser.add_argument("content", nargs="*", help="内容（无参数=显示，有内容=添加）")
    content_parser.add_argument("-d", "--delete", action="store_true", help="清空内容")
    content_parser.add_argument("--file", type=str, help="从文件读取内容")
    content_parser.set_defaults(command="content")

    # ========== title 子命令 ==========
    title_parser = subparsers.add_parser("title", help="消息标题操作")
    title_parser.add_argument("id", type=int, help="消息ID")
    title_parser.add_argument("title", nargs="*", help="标题（无参数=显示，有内容=设置）")
    title_parser.add_argument("-d", "--delete", action="store_true", help="清空标题")
    title_parser.add_argument("--file", type=str, help="从文件读取标题")
    title_parser.set_defaults(command="title")

    # ========== nest 子命令 ==========
    nest_parser = subparsers.add_parser("nest", help="消息鹏🐣窝操作")
    nest_parser.add_argument("id", type=int, help="消息ID")
    nest_parser.add_argument("nest", nargs="*", help="鹏🐣窝（无参数=显示，有内容=设置）")
    nest_parser.add_argument("-d", "--delete", action="store_true", help="清空鹏🐣窝")
    nest_parser.add_argument("--file", type=str, help="从文件读取鹏🐣窝")
    nest_parser.set_defaults(command="nest")

    # ========== workdir 子命令 ==========
    workdir_parser = subparsers.add_parser("workdir", help="消息工作目录操作")
    workdir_parser.add_argument("id", type=int, help="消息ID")
    workdir_parser.add_argument("workdir", nargs="*", help="工作目录（无参数=显示，有内容=设置）")
    workdir_parser.add_argument("-d", "--delete", action="store_true", help="清空工作目录")
    workdir_parser.add_argument("--file", type=str, help="从文件读取工作目录")
    workdir_parser.set_defaults(command="workdir")

    # ========== reply 子命令 ==========
    reply_parser = subparsers.add_parser("reply", help="回复消息")
    reply_parser.add_argument("id", type=int, nargs="?", help="要回复的消息ID")
    reply_parser.add_argument("message", nargs="*", help="回复内容")
    reply_parser.add_argument("--from", type=str, help="指定发送者")
    reply_parser.add_argument("--to", type=str, help="指定接收者")
    reply_parser.add_argument("--timeout", type=int, help="设置超时时间（秒）")
    reply_parser.add_argument("--file", type=str, help="从文件读取回复内容")
    reply_parser.add_argument("-n", "--nest", type=str, help="指定关联的nest名称")
    reply_parser.add_argument("-w", "--workdir", type=str, help="指定工作目录")

    # ========== delete 子命令 ==========
    delete_parser = subparsers.add_parser("delete", help="删除消息")
    delete_parser.add_argument("id", type=int, nargs="?", help="要删除的消息ID")
    delete_parser.add_argument("-f", "--force", action="store_true", help="强制删除")

    # ========== status 子命令 ==========
    status_parser = subparsers.add_parser("status", help="显示消息状态统计或设置消息状态")
    status_parser.add_argument("id", type=int, nargs="?", help="消息ID（可选）")
    status_parser.add_argument(
        "status_value",
        type=str,
        nargs="?",
        choices=["read", "processing", "done", "failed"],
        help="设置消息状态（可选，只支持：read/processing/done/failed）",
    )
    status_parser.add_argument(
        "--check-stale",
        action="store_true",
        dest="check_stale",
        help="检测并标记失效的处理中消息为中断",
    )
    status_parser.add_argument(
        "--check-paused", action="store_true", dest="check_paused", help="检测并显示被暂停的消息"
    )

    # ========== pause 子命令 ==========
    pause_parser = subparsers.add_parser("pause", help="暂停消息关联的事务")
    pause_parser.add_argument("id", type=int, help="消息ID")

    # ========== cancel 子命令 ==========
    cancel_parser = subparsers.add_parser("cancel", help="取消消息关联的事务")
    cancel_parser.add_argument("id", type=int, help="消息ID")

    # ========== restart 子命令 ==========
    restart_parser = subparsers.add_parser("restart", help="重启消息关联的事务")
    restart_parser.add_argument("id", type=int, help="消息ID")

    # ========== resume 子命令 ==========
    resume_parser = subparsers.add_parser("resume", help="恢复消息关联的事务")
    resume_parser.add_argument("id", type=int, help="消息ID")

    # ========== up 子命令 ==========
    up_parser = subparsers.add_parser("up", help="提高消息关联的事务优先级")
    up_parser.add_argument("id", type=int, help="消息ID")

    # ========== down 子命令 ==========
    down_parser = subparsers.add_parser("down", help="降低消息关联的事务优先级")
    down_parser.add_argument("id", type=int, help="消息ID")

    # ========== search 子命令 ==========
    search_parser = subparsers.add_parser("search", help="搜索消息")
    search_parser.add_argument("keyword", nargs="?", help="搜索关键词")

    # ========== tree 子命令 ==========
    tree_parser = subparsers.add_parser("tree", help="显示消息分支树（从该消息开始）")
    tree_parser.add_argument("id", type=int, help="消息ID")

    # ========== full-tree 子命令 ==========
    full_tree_parser = subparsers.add_parser("full-tree", help="显示消息完整树（从树根开始）")
    full_tree_parser.add_argument("id", type=int, help="消息ID")

    # ========== context 子命令 ==========
    context_parser = subparsers.add_parser("context", help="显示消息上下文")
    context_parser.add_argument("id", type=int, help="消息ID")

    # ========== move 子命令 ==========
    move_parser = subparsers.add_parser("move", help="移动消息到新的父节点或变成树根")
    move_parser.add_argument("id", type=int, help="要移动的消息ID")
    move_parser.add_argument(
        "target", type=int, nargs="?", default=None, help="目标父消息ID（不指定则变成树根）"
    )

    # ========== archive 子命令 ==========
    archive_parser = subparsers.add_parser("archive", help="归档消息")
    archive_parser.add_argument("id", type=int, help="要归档的消息ID")

    # ========== unarchive 子命令 ==========
    unarchive_parser = subparsers.add_parser("unarchive", help="取消归档消息")
    unarchive_parser.add_argument("id", type=int, help="要取消归档的消息ID")

    return parser


def parse_legacy_args(argv):
    """解析旧版参数格式，实现向后兼容"""
    # 如果第一个参数不是子命令，则尝试解析为旧版格式
    # 旧版格式：msg [消息内容] [-a] [-i] [-o] [-s 关键词] 等等

    # 检查第一个非选项参数是否是子命令
    subcommands = [
        "send",
        "list",
        "trees",
        "show",
        "content",
        "title",
        "nest",
        "workdir",
        "reply",
        "delete",
        "status",
        "pause",
        "cancel",
        "restart",
        "resume",
        "up",
        "down",
        "search",
        "tree",
        "full-tree",
        "context",
        "move",
        "archive",
        "unarchive",
    ]

    for arg in argv:
        if arg.startswith("-"):
            continue
        if arg in subcommands:
            return None  # 使用新格式
        break

    # 转换旧版参数为新版子命令格式
    new_argv = []

    # 检查各种旧版选项并转换
    has_content = False
    content_parts = []

    i = 0
    while i < len(argv):
        arg = argv[i]

        if arg in ["-a", "--all"]:
            new_argv.extend(["list", "--all"])
        elif arg in ["-n", "--nest"]:
            # 保留 -n/--nest 参数及其值
            if i + 1 < len(argv):
                new_argv.extend([arg, argv[i + 1]])
                i += 1
            else:
                new_argv.append(arg)
        elif arg == "-w" or arg == "--workdir":
            # 保留 -w/--workdir 参数及其值
            if i + 1 < len(argv):
                new_argv.extend([arg, argv[i + 1]])
                i += 1
            else:
                new_argv.append(arg)
        elif not arg.startswith("-"):
            # 消息内容
            has_content = True
            content_parts.append(arg)
        else:
            # 未知参数，保留
            new_argv.append(arg)

        i += 1

    # 如果有消息内容且没有子命令，添加 send 子命令
    if has_content and not any(cmd in new_argv for cmd in subcommands):
        new_argv = (
            ["send"] + content_parts + new_argv + ["--from", MASTER_NAME, "--to", AGENT_NAME]
        )
    elif not new_argv:
        # 默认显示发件箱
        new_argv = ["list"]

    return new_argv


# ==================== 主入口 ====================


def main(argv: list[str] | None = None) -> int:
    """msg 命令主入口"""
    # console_scripts 入口（.venv/bin/msg）无参数时：与 bin/msg 无参数行为一致，
    # 标记无参数模式（输出末尾打印 provider 用量/费用页脚，见 peng/cli/_usage_footer.py）
    if argv is None and len(sys.argv) <= 1:
        os.environ["PENG_CLI_NOARGS"] = "1"

    # 远程模式检测：优先本地服务，无本地服务时连接默认 nest 的 host
    # 这确保无论从 bin/msg 还是 .venv/bin/msg（console_scripts）都能正确路由
    _check_args = list(argv if argv is not None else sys.argv[1:])
    if "--local" not in _check_args:
        # 对于 send/reply 子命令，自动注入 --nest 和 --workdir
        # 确保远程模式下转发到主peng时，消息记录正确的nest和workdir
        _check_args = _inject_nest_workdir_defaults(_check_args)
        from peng.cli.remote import should_use_remote
        _remote_result = should_use_remote("msg", _check_args)
        if _remote_result is not None:
            return _remote_result

    # Windows下修复GBK编码无法输出emoji等Unicode字符的问题
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if argv is None:
        argv = sys.argv[1:]
    parser = create_parser()

    # 尝试解析为旧版格式
    new_argv = parse_legacy_args(argv)
    if new_argv:
        argv = new_argv

    args = parser.parse_args(argv)

    # 延迟导入 cli_service
    from peng.message import cli_service

    # 处理子命令
    command = args.command

    if command == "send":
        return cmd_send(args)
    elif command == "list":
        rc = cmd_list(args)
        # console_scripts 入口（.venv/bin/msg）无参数时经 parse_legacy_args 走 list 命令，
        # 与 bin/msg 无参数（trees）同为无参数模式，同样打印 provider 用量/费用页脚
        if os.environ.get("PENG_CLI_NOARGS") == "1":
            from peng.cli._usage_footer import print_provider_usage_footer

            print_provider_usage_footer()
        return rc
    elif command == "trees":
        rc = cmd_trees(args)
        # 无参数模式（PENG_CLI_NOARGS=1，bin/msg 入口或 console_scripts 兜底设置，
        # 远程模式经 cli_command env 转发）在输出末尾打印 provider 用量/费用页脚
        if os.environ.get("PENG_CLI_NOARGS") == "1":
            from peng.cli._usage_footer import print_provider_usage_footer

            print_provider_usage_footer()
        return rc
    elif command == "show":
        return cmd_show(args)
    elif command == "content":
        return cmd_content(args)
    elif command == "title":
        return cmd_title(args)
    elif command == "nest":
        return cmd_nest(args)
    elif command == "workdir":
        return cmd_workdir(args)
    elif command == "reply":
        return cmd_reply(args)
    elif command == "delete":
        return cmd_delete(args)
    elif command == "status":
        return cmd_status(args)
    elif command == "pause":
        return cmd_pause(args)
    elif command == "cancel":
        return cmd_cancel(args)
    elif command == "restart":
        return cmd_restart(args)
    elif command == "resume":
        return cmd_resume(args)
    elif command == "up":
        return cmd_up(args)
    elif command == "down":
        return cmd_down(args)
    elif command == "search":
        return cmd_search(args)
    elif command == "tree":
        return cmd_tree(args)
    elif command == "full-tree":
        return cmd_full_tree(args)
    elif command == "context":
        return cmd_context(args)
    elif command == "move":
        return cmd_move(args)
    elif command == "archive":
        return cmd_archive(args)
    elif command == "unarchive":
        return cmd_unarchive(args)
    else:
        # 默认显示未归档的根消息树
        result = cli_service.read_unarchived_messages(order="ASC")
        print_all_messages_result(result)
        return 0

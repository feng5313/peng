#!/usr/bin/env python3
"""
鹏的事务管理工具

用于查询事务树、消息树和事务控制的命令行工具。

命令:
    task list [OPTIONS]         列出事务
    task show <ID>              显示事务详情
    task tree <ID>              显示事务分支树（从该事务开始）
    task full-tree <ID>         显示事务完整树（从根事务开始）
    task msg <ID>               显示根事务的根消息回复链
    task context <ID>           显示消息上下文
    task pause <ID>             暂停事务（递归暂停子事务）
    task cancel <ID>            取消事务（递归取消子事务）
    task up <ID>                提高优先级（递归提高子事务）
    task down <ID>              降低优先级（递归降低子事务）
    task delete <ID>            递归删除事务及其所有子事务
    task kill <ID>              杀死指定事务的所有后台daemon进程
    task kill -d <daemon_id>    杀死指定daemon id的后台进程（可多个-d）
    task killall                杀死daemon.json中记录的所有后台daemon进程
    task daemon                 显示所有已注册的后台daemon
    task retry <ID>             重试事务
    task recover                恢复中断的事务
    task start <ID>             启动待定事务
    task status <ID> [状态]     显示或设置事务状态（pending/running/completed/failed）
    task new <ID> --small-model 创建使用小模型的子事务
    task small-model <ID>       设置事务使用小模型
    task big-model <ID>         设置事务使用大模型
    task timer <ID> [seconds]   事务定时器操作（无参数=显示，有值=设置，-d=清除）
    task heartbeat <ID> [sec]   事务心跳超时操作（无参数=显示，有值=设置，-d=重置默认）
    task prompt <ID> [prompt]   事务提示词操作（无参数=显示，有内容=设置，-d=清空）
    task description <ID> [desc] 事务描述操作（无参数=显示，有内容=设置，-d=清空）
    task content <ID> [content] 事务内容操作（无参数=显示，有内容=设置，-d=清空）
    task title <ID> [title]     事务标题操作（无参数=显示，有内容=设置，-d=清空）
    task workdir <ID> [dir]     事务工作目录操作（无参数=显示，有内容=设置，-d=清空）
    task nest <ID> [nest]       事务鹏🐣窝操作（无参数=显示，有内容=设置，-d=清空）
    task branch <ID> [name]     事务分支名操作（无参数=显示，有内容=设置，-d=清空）
    task result <ID> [result]   事务结果操作（无参数=显示，有内容=设置，-d=清空）
    task metadata <ID> [meta]  事务元数据操作（无参数=显示，有内容=设置，-d=清空）
    task info <ID> [info]       事务信息操作（无参数=显示，有内容=添加，-d=清空）
    task output <filename>      显示事务输出文件内容
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from peng.common.utils import get_text_from_args, fix_markdown_linebreaks
from peng.common.colors import get_task_colors
from peng.config import get_agent_name, get_master_name, get_project_root
from peng.config.loader import get_default_heartbeat_timeout
from peng.db.connection import get_connection, get_cursor

from peng.task.info import (
    parse_info_to_list_with_status,
    serialize_info_list,
)
from peng.common.daemon_registry import (
    cleanup_dead_daemons,
    kill_all_daemons_sync,
    kill_daemon_by_id_sync,
    kill_task_daemons_sync,
    list_daemons,
    normalize_nest,
)

import logging

logger = logging.getLogger(__name__)
PROJECT_ROOT = get_project_root()
AGENT_NAME = get_agent_name()


def _read_file_content(file_path: str) -> str:
    """从文件读取内容，用于 --file 参数。读取后自动删除临时文件。"""
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
        print(f"❌ 读取文件失败: {e}", file=sys.stderr)
        return ""


class Colors:
    """终端颜色（从统一颜色配置获取）"""

    _colors = get_task_colors()
    RED = _colors.get("ERROR", "")
    # 上下文过长警告专用红色：亮度拉满（RGB 255,100,100）
    CONTEXT_RED = "\033[38;2;255;100;100m"
    GREEN = _colors.get("SUCCESS", "")
    YELLOW = _colors.get("WARNING", "")
    BLUE = _colors.get("INFO", "")
    MAGENTA = _colors.get("HIGHLIGHT", "")
    CYAN = _colors.get("TITLE", "")
    WHITE = _colors.get("CONTENT", "")
    BOLD = _colors.get("BOLD", "")
    DIM = _colors.get("DIM", "")
    RESET = _colors.get("RESET", "")

    @classmethod
    def highlight(cls, text: str, color: str = None) -> str:
        """高亮文本"""
        if color is None:
            color = cls.CYAN
        return f"{color}{text}{cls.RESET}"


def _print_task_not_found_debug_info(task_id: int) -> None:
    """输出事务不存在时的调试信息（包括调用堆栈）"""
    print(f"❌ 错误: 事务 {task_id} 不存在", file=sys.stderr)


# ==================== 回滚超链接ID标记（output 显示 + rollback 子命令） ====================
# 统一实现见 peng.ui.rollback_display：解析行内 OSC8 rollback:id:<task_id>-<seq> 超链接，
# 在行尾追加 [R<seq>] task rollback <task_id> R<seq>（含回滚ID+命令行，橙色，去重）。
# 与 fly msg/reply/info/watch 实时输出共用同一标注，保证回滚信息格式全局一致。

from peng.ui.rollback_display import (  # noqa: E402,F401
    ROLLBACK_ID_URI_RE as _ROLLBACK_ID_URI_RE,
    annotate_rollback_info as _annotate_rollback_ids,
)


def get_task_manager():
    """获取事务管理器"""
    from peng.task.manager import TaskManager

    return TaskManager()


def get_context_tokens(context):
    """计算事务上下文的 token 数量（转发到 database 模块级函数）"""
    from peng.task.database import get_context_tokens as _get

    return _get(context)


def get_context_color(tokens: int, provider: str = "") -> str:
    """根据上下文 token 数返回颜色级别（转发到 database 模块级函数）"""
    from peng.task.database import get_context_color as _get

    return _get(tokens, provider)


def get_context_total_tokens(context):
    """获取事务累计总token消耗（转发到 database 模块级函数）"""
    from peng.task.database import get_context_total_tokens as _get

    return _get(context)


def get_context_cost(context):
    """获取事务累计费用（转发到 database 模块级函数）"""
    from peng.task.database import get_context_cost as _get

    return _get(context)


def get_context_usage_detail(context):
    """获取事务分项token用量（转发到 database 模块级函数）"""
    from peng.task.database import get_context_usage_detail as _get

    return _get(context)


def _get_cost_html_color(cost: float) -> str:
    """根据费用返回RGB渐变HTML颜色码（绿→橙红线性插值）。"""
    if cost <= 0:
        return "#3fb950"
    lo = 0.1  # 0.1元 → 绿
    hi = 10.0  # 10元 → 橙红
    ratio = (cost - lo) / (hi - lo) if cost > lo else 0.0
    ratio = min(max(ratio, 0.0), 1.0)
    r = int(63 + (255 - 63) * ratio)
    g = int(185 + (100 - 185) * ratio)
    b = int(80 + (60 - 80) * ratio)
    return f"rgb({r},{g},{b})"


def _get_consumption_html_color(total_tokens: int) -> str:
    """根据总token消耗返回RGB渐变HTML颜色码（蓝→紫线性插值）。

    蓝(<100K) → 紫(>2M) 线性插值。
    """
    if total_tokens <= 0:
        return "#4682ff"
    lo = 100_000  # 100K → 蓝
    hi = 2_000_000  # 2M → 紫
    ratio = (total_tokens - lo) / (hi - lo) if total_tokens > lo else 0.0
    ratio = min(max(ratio, 0.0), 1.0)
    r = int(70 + (200 - 70) * ratio)
    g = int(130 + (100 - 130) * ratio)
    b = 255
    return f"rgb({r},{g},{b})"


def get_message_manager():
    """获取消息管理器"""
    from peng.message.manager import MessageManager

    return MessageManager()


def format_datetime(dt_str: str | None) -> str:
    """格式化时间"""
    if not dt_str:
        return "-"
    try:
        dt = datetime.fromisoformat(dt_str)
        return dt.strftime("%m-%d %H:%M")
    except Exception as e:
        return dt_str


def format_status(status: str) -> str:
    """格式化状态"""
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
    return f"{color}{name}{Colors.RESET}"


def format_task_id(task_id: int, task: dict | None = None) -> str:
    """格式化事务ID，统一显示为"根事务#31"或"子事务#33"

    Args:
        task_id: 事务ID
        task: 事务数据（可选），如果不提供则不显示上级信息

    Returns:
        格式化后的事务ID字符串
    """
    if task is None:
        return str(task_id)

    # 判断是根事务还是子事务
    if task.get("parent_task_id"):
        return f"{Colors.CYAN}子事务#{task_id}{Colors.RESET}"
    else:
        return f"{Colors.CYAN}根事务#{task_id}{Colors.RESET}"


def cmd_children(args):
    """显示子事务列表"""
    tm = get_task_manager()

    # 检查父事务是否存在
    parent = tm.get_task(args.id)
    if not parent:
        _print_task_not_found_debug_info(args.id)
        return 1

    # 获取子事务（直接返回完整事务列表）
    children = tm.get_children(args.id)

    if not children:
        formatted_id = format_task_id(args.id, parent)
        print(f"事务 {formatted_id} 没有子事务")
        return 0

    formatted_id = format_task_id(args.id, parent)
    print(f"\n{Colors.BOLD}事务 {formatted_id} 的子事务{Colors.RESET}")
    print(f"父事务：{parent.get('content', '')}")
    print("-" * 60)

    # 按状态分组统计
    completed = [c for c in children if c.get("status") == "completed"]
    failed = [c for c in children if c.get("status") == "failed"]
    running = [c for c in children if c.get("status") == "running"]
    pending = [c for c in children if c.get("status") == "pending"]

    print(f"\n统计：共 {len(children)} 个子事务")
    print(f"  {Colors.GREEN}已完成：{len(completed)}{Colors.RESET}")
    print(f"  {Colors.BLUE}运行中：{len(running)}{Colors.RESET}")
    print(f"  {Colors.YELLOW}待执行：{len(pending)}{Colors.RESET}")
    if failed:
        print(f"  {Colors.RED}已失败：{len(failed)}{Colors.RESET}")

    print(f"\n{'ID':<30} {'状态':<12} {'优先级':<10} {'内容':<40}")
    print("-" * 90)

    for child in children:
        child_id = format_task_id(child["id"], child)
        status = format_status(child.get("status", "pending"))
        child_content = (child.get("content", "") or "")[:38]
        print(f"{child_id:<30} {status:<20} {child_content:<40}")

    # 检查是否所有子事务都已完成
    if len(completed) == len(children):
        print(f"\n{Colors.GREEN}所有子事务已完成!{Colors.RESET}")
    elif failed:
        print(f"\n{Colors.YELLOW}有子事务失败，请检查{Colors.RESET}")

    return 0


def cmd_subtask(args):
    """子事务操作（显示或创建）"""
    # 先检查 --file 参数，优先从文件读取内容
    file_path = getattr(args, "file", None)
    if file_path:
        content = _read_file_content(file_path)
        if not content and args.content:
            content = " ".join(args.content)
    else:
        content = " ".join(args.content) if args.content else ""

    # 如果既没有content参数也没有--file参数，显示用法
    if not content:
        print(f"错误: 缺少子事务需求描述")
        print(
            f"示例：subtask {args.id} 子事务需求描述 --prompt 提示词名称 --workdir 工作目录 --creator 事务创建者 --small-model  （创建子事务，指定需求描述，指定提示词名称，工作目录，事务创建者，是否使用小模型）"
        )
        print(f"或者使用 --file 参数从文件读取内容：subtask {args.id} --file /path/to/content.txt")
        return 1

    # 有content参数，创建子事务
    tm = get_task_manager()

    # 检查父事务是否存在
    parent = tm.get_task(args.id)
    if not parent:
        print(f"错误: 父事务 {args.id} 不存在", file=sys.stderr)
        _print_task_not_found_debug_info(args.id)
        return 1

    # 自动将父事务标记为复杂事务（事务三：创建子事务时自动设置is_complex=1）
    is_complex = parent.get("is_complex", 0)
    if is_complex == 0:
        # 父事务不是复杂事务，自动标记为复杂事务
        success = tm.db.update_task(args.id, is_complex=1)
        if success:
            print(f"{Colors.GREEN}✅ 父事务 #{args.id} 已自动标记为复杂事务{Colors.RESET}")
            is_complex = 1
        else:
            print(
                f"{Colors.YELLOW}⚠️  警告：无法将父事务 #{args.id} 标记为复杂事务{Colors.RESET}",
                file=sys.stderr,
            )

    # 创建子事务
    try:
        child_id = tm.create_subtask(
            parent_task_id=args.id,
            content=content,
            description=None,
            small_model=args.small_model if hasattr(args, "small_model") else False,
            prompt=args.prompt if hasattr(args, "prompt") else None,
            workdir=args.workdir if hasattr(args, "workdir") else None,
            creator=args.creator if hasattr(args, "creator") else "",
        )
        if not child_id:
            print(f"❌{Colors.RED}创建子事务失败 ！{Colors.RESET}")
            return 1
        print(f"{Colors.GREEN}已创建子事务 #{child_id}{Colors.RESET}")
        print(f"  父事务：#{args.id} - {parent.get('content', '')}")
        print(f"  内容：{content}")
        if hasattr(args, "prompt") and args.prompt:
            print(f"  提示词：{args.prompt}")
        if hasattr(args, "workdir") and args.workdir:
            print(f"  工作目录：{args.workdir}")
        if hasattr(args, "creator") and args.creator:
            print(f"  创建者：{args.creator}")
        if hasattr(args, "small_model") and args.small_model:
            print(f"  使用小模型：{Colors.GREEN}是{Colors.RESET}")
        return 0
    except ValueError as e:
        print(f"{Colors.RED}错误：{e}\n{traceback.format_exc()}{Colors.RESET}")
        return 1
    except Exception as e:
        print(f"{Colors.RED}创建失败：{e}\n{traceback.format_exc()}{Colors.RESET}")
        return 1


def cmd_trees(args):
    """以树状结构显示事务树（默认显示未归档的）"""
    tm = get_task_manager()

    # 检查 --all 参数
    show_all = getattr(args, "all", False)
    now = datetime.now().isoformat()
    deleted_tasks = []  # 记录被自动删除的事务

    # ── 高性能扫描：单次 JOIN 批量获取所有根事务的归档状态 ──
    # 替代原先 list_tasks(SELECT * 含 200MB+ context) + N次 get_message()
    all_root_status = tm.db.get_root_tasks_message_status(include_archived=True)

    for item in all_root_status:
        task_id = item["id"]
        message_id = item.get("message_id")
        msg_exists = item.get("msg_exists", False)
        msg_archived = item.get("msg_is_archived")

        if not message_id or not msg_exists:
            # 根消息为空或不存在，递归删除该事务树（无论是否有--all）
            try:
                deleted_count = tm.db.delete_task_recursive(task_id, force=True)
                deleted_tasks.append((task_id, deleted_count))
            except Exception as e:
                logger.warning("自动删除事务#%d失败: %s", task_id, e)
            continue

        # 根消息已归档且非--all时，自动归档事务
        if not show_all and msg_archived:
            if not item.get("is_archived"):
                tm.db.update_task(task_id, is_archived=1, archived_at=now)

    if deleted_tasks:
        for task_id, count in deleted_tasks:
            logger.info(
                "根事务#%d的根消息为空或无效，已自动删除（共%d个事务）",
                task_id,
                count,
            )

    # 获取要显示的根事务（只取必要字段，排除 context 等大字段）
    _limit = getattr(args, "limit", None)
    if _limit is not None:
        root_tasks = tm.db.list_tasks(
            limit=_limit,
            root_only=True,
            include_archived=show_all,
            order="DESC",
            exclude_fields=["context"],
        )
        root_tasks.reverse()  # 反转为从老到新，保持阅读顺序一致
    else:
        root_tasks = tm.db.list_tasks(
            limit=None,
            root_only=True,
            include_archived=show_all,
            exclude_fields=["context"],
        )

    if not root_tasks:
        print("没有事务" if show_all else "没有未归档的根事务")
        return 0

    # ── 高性能树渲染：批量预加载所有子树数据 ──
    # 单次 SQL 查询所有根事务的子树（含 context），避免递归 N 次 get_task
    root_ids = [r["id"] for r in root_tasks]
    subtree_map = tm.db.get_subtree_batch(root_ids)

    label = "所有根事务树" if show_all else "未归档的根事务树"
    print(f"\n{Colors.BOLD}{label}{Colors.RESET}")
    print("=" * 70)

    for root in root_tasks:
        rid = root["id"]
        subtree_nodes = subtree_map.get(rid, [])
        tree_text = tm.db.build_tree_text_from_batch(rid, subtree_nodes, content_max_len=80)
        if tree_text:
            print(tree_text)

    # 统计信息
    print(f"\n共 {len(root_tasks)} 个{label}")
    return 0


def print_tree_recursive(
    tm,
    task_id: int,
    indent: int = 0,
    is_last: bool = False,
    content_max_len: int = 80,
):
    """递归打印事务树

    Args:
        tm: 事务管理器
        task_id: 事务ID
        indent: 缩进层级
        is_last: 是否为最后一个子节点
        show_priority: 是否显示优先级（默认True）
        content_max_len: 内容最大显示长度，0表示不截断（默认40）
    """
    task = tm.get_task(task_id)
    if not task:
        return

    prefix = "  " * indent
    if indent > 0:
        prefix += "└─ " if is_last else "├─ "

    status = task.get("status")
    content = task.get("content", "") or ""
    if content_max_len > 0:
        content = content[:content_max_len]

    # 构建显示行
    formatted_id = format_task_id(task_id, task)
    # 快速路径：直接解析 JSON 元数据，避免创建 MessageHistory 对象
    from peng.task.database import _fast_context_meta

    meta = _fast_context_meta(task.get("context"))
    tokens = meta.get("prompt_tokens", 0)
    cost_info = ""
    _cost = meta.get("total_cost", 0.0)
    if _cost > 0:
        from peng.task.database import _get_cost_ansi as _gc_ansi

        _cost_c = _gc_ansi(_cost)
        cost_info = f" [{_cost_c}费用{_cost:.2f}元{Colors.RESET}]"
    if tokens > 2000 and status in ["running", "paused"]:
        color = get_context_color(tokens, task.get("provider", ""))
        is_danger = color == "context_red"
        COLORS_MAP = {
            "context_red": Colors.CONTEXT_RED,
            "yellow": Colors.YELLOW,
            "green": Colors.GREEN,
        }
        cc = COLORS_MAP[color]
        comment = f"{cc}{'降智' if is_danger else '安全'}{Colors.RESET}"
        print(
            f"{prefix}{formatted_id} [{format_status(status)}] [当前上下文长度 {tokens // 1000}K ({comment})]{cost_info} {content}"
        )
    else:
        print(f"{prefix}{formatted_id} [{format_status(status)}]{cost_info} {content}")

    # 递归显示子事务
    children = tm.get_children(task_id)
    for i, child in enumerate(children):
        is_last_child = i == len(children) - 1
        # get_children 返回字典列表，需要提取 id
        child_id = child if isinstance(child, int) else child.get("id")
        if child_id:
            print_tree_recursive(tm, child_id, indent + 1, is_last_child, content_max_len)


def _build_info_table(task: dict, tm) -> "Table":
    """构建基本信息表格"""
    from rich.table import Table
    from rich.text import Text

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="cyan bold", width=12)
    table.add_column()

    # 状态用 rich 颜色
    status = task.get("status", "pending")
    status_names = {
        "pending": "待执行",
        "running": "执行中",
        "completed": "已完成",
        "failed": "已失败",
        "cancelled": "已取消",
        "paused": "已暂停",
        "interrupted": "已中断",
    }
    status_colors = {
        "pending": "yellow",
        "running": "blue",
        "completed": "green",
        "failed": "red",
        "cancelled": "dim",
        "paused": "cyan",
        "interrupted": "magenta",
    }
    status_name = status_names.get(status, status)
    status_style = status_colors.get(status, "")
    status_text = Text(status_name, style=status_style)

    progress = task.get("progress", 0)
    progress_style = "green" if progress == 100 else ("yellow" if progress >= 50 else "")
    progress_text = Text(f"{progress}%", style=progress_style)

    table.add_row("ID", str(task.get("id")))
    table.add_row("状态", status_text)
    table.add_row("进度", progress_text)
    table.add_row("已归档", "是" if task.get("is_archived") else "否")
    table.add_row("类型", task.get("task_type", "auto"))
    model_str = "小模型" if task.get("small_model") else "大模型"
    model_style = "cyan" if task.get("small_model") else "green"
    table.add_row("模型", Text(model_str, style=model_style))
    if task.get("creator"):
        table.add_row("创建者", task.get("creator", ""))
    table.add_row("复杂事务", "是" if task.get("is_complex") else "否")

    return table


def _build_time_table(task: dict) -> "Table":
    """构建时间表格"""
    from rich.table import Table

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="cyan bold", width=12)
    table.add_column()

    table.add_row("创建", format_datetime(task.get("created_at")))
    table.add_row("开始", format_datetime(task.get("started_at")))
    table.add_row("完成", format_datetime(task.get("completed_at")))
    table.add_row("截止", format_datetime(task.get("deadline")))
    table.add_row("心跳", format_datetime(task.get("heartbeat_at")))
    heartbeat_timeout = task.get("heartbeat_timeout") or get_default_heartbeat_timeout()
    table.add_row("心跳超时", f"{heartbeat_timeout} 秒")
    table.add_row("超时", f"{task.get('timeout', 0)} 秒")
    table.add_row("定时器", f"{task.get('timer', 0)} 秒")

    return table


def _build_relation_table(task: dict) -> "Table":
    """构建关联表格"""
    from rich.table import Table

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="cyan bold", width=12)
    table.add_column()

    if task.get("message_id"):
        table.add_row("关联消息", f"#{task['message_id']}")
    if task.get("parent_task_id"):
        table.add_row("父事务", f"#{task['parent_task_id']}")
    if task.get("root_id"):
        table.add_row("根事务", f"#{task['root_id']}")
    table.add_row("深度", str(task.get("depth", 0)))
    if task.get("path"):
        table.add_row("路径", task.get("path"))
    if task.get("caller"):
        table.add_row("调用者", task.get("caller", ""))
    if task.get("caller_session_id"):
        table.add_row("调用会话", task.get("caller_session_id", ""))

    return table


def _build_worktree_table(task: dict) -> "Table":
    """构建 Worktree 表格"""
    from rich.table import Table

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="cyan bold", width=12)
    table.add_column()

    if task.get("nest"):
        table.add_row("鹏🐣 窝", task.get("nest", ""))
    if task.get("workdir"):
        table.add_row("工作目录", task.get("workdir", ""))
    if task.get("worktree_path"):
        table.add_row("工作树路径", task.get("worktree_path", ""))
    if task.get("branch_name"):
        table.add_row("分支名", task.get("branch_name", ""))
    table.add_row("已锁定", "是" if task.get("worktree_locked") else "否")

    return table


# ══════════════════════════════════════════════════════════════
#  HTML 格式输出（用于 WebUI 弹窗，支持表格自适应宽度）
# ══════════════════════════════════════════════════════════════

import html as _html_module


def _h(text: str) -> str:
    """HTML escape"""
    return _html_module.escape(str(text), quote=False)


def _h_with_links(text: str, nest: str = None) -> str:
    """HTML escape + 文件路径超链接转换（用于所有文本区域）"""
    text = _h(text)
    return _convert_paths_to_links(text, nest)


def _content_to_text(content: Any) -> str:
    """将消息 content 规范化为纯文本字符串

    context 中消息 content 可能为 OpenAI 多模态格式（list）：
    [{"type": "text", "text": "..."}, {"type": "image_url", "image_url": {...}}, ...]
    （如 read 工具返回图片附件或多模态注入后的内容）。逐项提取可读文本：
    - type=text → 取 text 字段
    - type=image_url / image → "[图片]" 占位符
    - 其他 dict 项 → JSON 序列化（截断防爆屏）
    str 原样返回；None 返回空串；其他类型 str() 兜底。
    """
    import json

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                itype = item.get("type", "")
                if itype == "text":
                    parts.append(str(item.get("text", "")))
                elif itype in ("image_url", "image", "input_image"):
                    parts.append("[图片]")
                elif "text" in item:
                    parts.append(str(item.get("text", "")))
                else:
                    s = json.dumps(item, ensure_ascii=False, default=str)
                    parts.append(s[:200] + ("…" if len(s) > 200 else ""))
            elif isinstance(item, str):
                parts.append(item)
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text", ""))
        return json.dumps(content, ensure_ascii=False, default=str)[:200]
    return str(content)


def _md_to_html(md_text: str, nest: str = None) -> str:
    """简易 Markdown → HTML 转换（支持标题、加粗、代码、列表、表格、硬换行）"""
    import re as _re

    # 防御：非字符串输入（如多模态 list）规范化为文本，保证渲染永不崩溃
    if not isinstance(md_text, str):
        md_text = _content_to_text(md_text)

    lines = md_text.split("\n")
    result = []
    in_ul = False
    in_ol = False
    in_code = False
    in_table = False
    table_rows = []

    def close_lists():
        nonlocal in_ul, in_ol
        if in_ul:
            result.append("</ul>")
            in_ul = False
        if in_ol:
            result.append("</ol>")
            in_ol = False

    def flush_table():
        nonlocal in_table, table_rows
        if table_rows:
            result.append('<table class="md-table">')
            for ri, row in enumerate(table_rows):
                cells = [c.strip() for c in row.split("|")]
                cells = [c for c in cells if c != ""]
                if not cells:
                    continue
                tag = "th" if ri == 0 else "td"
                result.append("<tr>" + "".join(f"<{tag}>{_h(c)}</{tag}>" for c in cells) + "</tr>")
            result.append("</table>")
        table_rows = []
        in_table = False

    for line in lines:
        stripped = line.strip()

        # Code block toggle
        if stripped.startswith("```"):
            if in_code:
                result.append("</code></pre>")
                in_code = False
            else:
                close_lists()
                flush_table()
                result.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            result.append(_h(line))
            continue

        # Markdown table row (contains |)
        if "|" in stripped and stripped.startswith("|"):
            if not in_table:
                # Check if next line is separator
                in_table = True
            # Skip separator row (|---|---|)
            if _re.match(r"^\|[-\s:|]+\|$", stripped):
                table_rows.append("")  # placeholder
                continue
            table_rows.append(stripped)
            continue
        else:
            if in_table:
                flush_table()

        # Empty line
        if not stripped:
            close_lists()
            result.append("")
            continue

        # Headings
        if stripped.startswith("#### "):
            close_lists()
            result.append(f"<h4>{_h(stripped[5:])}</h4>")
        elif stripped.startswith("### "):
            close_lists()
            result.append(f"<h3>{_h(stripped[4:])}</h3>")
        elif stripped.startswith("## "):
            close_lists()
            result.append(f"<h2>{_h(stripped[3:])}</h2>")
        elif stripped.startswith("# "):
            close_lists()
            result.append(f"<h1>{_h(stripped[2:])}</h1>")
        # Unordered list
        elif _re.match(r"^[-*]\s+", stripped):
            if not in_ul:
                close_lists()
                result.append('<ul class="md-list">')
                in_ul = True
            item = _re.sub(r"^[-*]\s+", "", stripped)
            item = _md_inline(item, nest)
            result.append(f"<li>{item}</li>")
        # Ordered list
        elif _re.match(r"^\d+\.\s+", stripped):
            if not in_ol:
                close_lists()
                result.append('<ol class="md-list">')
                in_ol = True
            item = _re.sub(r"^\d+\.\s+", "", stripped)
            item = _md_inline(item, nest)
            result.append(f"<li>{item}</li>")
        else:
            close_lists()
            result.append(f"<p>{_md_inline(stripped, nest)}</p>")

    close_lists()
    flush_table()
    if in_code:
        result.append("</code></pre>")

    return "\n".join(result)


def _convert_paths_to_links(text: str, nest: str = None) -> str:
    """将文件路径转换为HTML超链接（类似xterm中的行为），超链接包含 nest 参数"""
    import re as _re

    # 步骤1：保护URL（先匹配保存，最后替换）
    url_matches = []
    text = _re.sub(
        r"https?:\/\/[a-zA-Z0-9\-._~:/?#\[\]@!$&\'()*+,;=%]+",
        lambda m: _protect_url(m.group(), url_matches),
        text,
    )

    # 步骤2：保护 filePath/path= 参数格式（最优先）
    param_matches = []
    text = _re.sub(
        r'(?:filePath|path)=["\']([^"\']+)["\']',
        lambda m: _protect_param(m.group(1), param_matches),
        text,
    )

    # 步骤2.5：保护 Windows 盘符路径（全平台）
    win_drive_matches = []
    text = _re.sub(
        r'(?<![a-zA-Z0-9])([a-zA-Z]:[/\\][^\s"\'`，。；：！？、（）《》【】「」『』〈〉]{2,})',
        lambda m: _protect_win_drive(m.group(), win_drive_matches),
        text,
    )

    # 步骤3：保护 Unix/Linux 路径（含扩展名优先、无扩展名、家目录）
    unix_matches = []
    # 通用正则：含扩展名的路径优先，无扩展名路径其次，家目录最后
    unix_pattern = (
        r"(?:[~/]?[a-zA-Z0-9_\-\.+]+/)+[a-zA-Z0-9_\-+\u0080-\uffff]+\.[a-zA-Z0-9_\-+]+(?![a-zA-Z0-9_\-+])"
        r"|(?:[~/]?[a-zA-Z0-9_\-\.+]+/)+[a-zA-Z0-9_\-\.+]+/?"
        r"|~\/[^\s]+"
    )
    text = _re.sub(unix_pattern, lambda m: _protect_unix(m.group(), unix_matches), text)

    # 从后往前替换所有占位符为超链接（避免索引变化）——包含 nest 参数！
    all_replacements = []

    # URL → <a href="url">url</a>（保留原始URL，不需要 nest）
    for placeholder, original_url in url_matches:
        all_replacements.append(
            (placeholder, f'<a href="{original_url}" class="file-link">{original_url}</a>')
        )

    # filePath/path= → <a href="editor://path?nest=nestname">path</a>
    for placeholder, original_path in param_matches:
        nest_param = f"?nest={nest}" if nest else ""
        all_replacements.append(
            (
                placeholder,
                f'<a href="editor://{original_path}{nest_param}" class="file-link">{original_path}</a>',
            )
        )

    # Windows盘符路径 → <a href="editor://path?nest=nestname">path</a>
    for placeholder, original_path in win_drive_matches:
        nest_param = f"?nest={nest}" if nest else ""
        all_replacements.append(
            (
                placeholder,
                f'<a href="editor://{original_path}{nest_param}" class="file-link">{original_path}</a>',
            )
        )

    # Unix路径 → <a href="editor://path?nest=nestname">path</a>
    for placeholder, original_path in unix_matches:
        nest_param = f"?nest={nest}" if nest else ""
        all_replacements.append(
            (
                placeholder,
                f'<a href="editor://{original_path}{nest_param}" class="file-link">{original_path}</a>',
            )
        )

    for placeholder, replacement in all_replacements:
        text = text.replace(placeholder, replacement)

    return text


def _protect_url(match_text: str, matches: list) -> str:
    """剥离URL末尾标点符号，生成占位符"""
    import re as _re

    tp = _re.search(r"[.,;:!?)\]]+$", match_text)
    url = match_text[: -len(tp.group())] if tp else match_text
    placeholder = f"\x00URL_PH_{len(matches)}\x00"
    matches.append((placeholder, url))
    return placeholder + (tp.group() if tp else "")


def _protect_param(path: str, matches: list) -> str:
    """生成 filePath/path= 参数的占位符"""
    placeholder = f"\x00PARAM_PH_{len(matches)}\x00"
    matches.append((placeholder, path))
    return placeholder


def _protect_win_drive(match_text: str, matches: list) -> str:
    """生成 Windows 盘符路径的占位符"""
    if match_text.startswith("\x00"):
        return match_text
    cleaned_path = match_text.rstrip(".,;:!?)\\]")  # 简单清理尾部标点
    if not cleaned_path or len(cleaned_path) < 3:
        return match_text
    placeholder = f"\x00WINDRIVE_PH_{len(matches)}\x00"
    matches.append((placeholder, cleaned_path))
    return placeholder


def _protect_unix(match_text: str, matches: list) -> str:
    """生成 Unix/Linux 路径的占位符"""
    if match_text.startswith("\x00"):
        return match_text
    real_path = match_text.rstrip(".,;:!?)\\]")  # 简单清理尾部标点
    if not real_path:
        return match_text
    placeholder = f"\x00UNIX_PH_{len(matches)}\x00"
    matches.append((placeholder, real_path))
    return placeholder


def _md_inline(text: str, nest: str = None) -> str:
    """处理行内 Markdown 格式：加粗、斜体、行内代码、文件路径超链接

    为避免数学表达式中的 *（乘号）被错误解析为斜体标记，
    先用占位符保护 *，待 Markdown 处理完毕后再还原。

    🔧 Task#872：在 HTML 转义后、Markdown 处理前，将文件路径转换为超链接。
    """
    import re as _re

    # Escape HTML first
    text = _h(text)

    # 🔧 Task#872：将文件路径转换为超链接（在 HTML 转义后处理）——包含 nest 参数！
    text = _convert_paths_to_links(text, nest)

    # 用占位符保护 *，防止被误解析为斜体标记
    # 先保护 **（加粗标记），再保护单独的 *
    AST_PH = "\x00AST\x00"
    BOLD_PH = "\x00BLD\x00"
    text = text.replace("**", BOLD_PH)
    text = text.replace("*", AST_PH)
    text = text.replace(BOLD_PH, "**")  # 还原加粗标记

    # Bold: **text**
    text = _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)

    # Italic: *text* — 此时 * 已被占位符保护，不会误匹配乘号
    text = _re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", text)

    # 还原被保护的乘号
    text = text.replace(AST_PH, "*")

    # Inline code: `text` — ⚠️ 行内代码块内不应再转换路径超链接（已做HTML转义）
    text = _re.sub(r"`(.+?)`", r"<code>\1</code>", text)

    return text


class _SafeHtml(str):
    """标记字符串为安全HTML，在 _kv_table_html 中不被转义"""

    pass


def _kv_table_html(rows: list) -> str:
    """生成键值对 HTML 表格（用于基本信息、时间、关联等）"""
    parts = ['<table class="kv-table">']
    for key, val in rows:
        # val 可以是 (text, color_class) 元组
        if isinstance(val, tuple):
            text, cls = val
            val_html = f'<span class="{cls}">{_h(text)}</span>'
        elif isinstance(val, _SafeHtml):
            val_html = val  # 已经是安全HTML，不转义
        else:
            val_html = _h(val)
        parts.append(f"<tr><th>{_h(key)}</th><td>{val_html}</td></tr>")
    parts.append("</table>")
    return "\n".join(parts)


def _section_html(icon_title: str, content_html: str) -> str:
    """生成一个带标题的 section"""
    return f'<div class="task-section"><h3>{_h(icon_title)}</h3>{content_html}</div>'


def _cmd_show_html(args):
    """HTML 格式输出事务详情（用于 WebUI 弹窗）"""
    tm = get_task_manager()
    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    parts = []

    # ── 标题（含消息#id、状态、上下文、消耗信息）──
    formatted_id = format_task_id(task["id"], task)
    clean_id = re.sub(r"\x1b\[[0-9;]*m", "", formatted_id)
    title_spans = [
        f'<span style="font-size:1.5em;color:#f0883e;font-weight:bold;">{_h(clean_id)}</span>'
    ]
    # 消息#id（根事务显示）
    message_id = task.get("message_id")
    if message_id and not task.get("parent_task_id"):
        title_spans.append(
            f'<span style="font-size:1.2em;color:#58a6ff;font-weight:bold;">消息#{message_id}</span>'
        )
    # [状态]
    _status = task.get("status", "pending")
    _status_html_colors = {
        "pending": "#e6edf3",
        "running": "#58a6ff",
        "completed": "#3fb950",
        "failed": "#f85149",
        "cancelled": "#8b949e",
        "paused": "#39c5cf",
        "interrupted": "#ff6b6b",
    }
    _status_names = {
        "pending": "待执行",
        "running": "执行中",
        "completed": "已完成",
        "failed": "已失败",
        "cancelled": "已取消",
        "paused": "已暂停",
        "interrupted": "已中断",
    }
    _sname = _status_names.get(_status, _status)
    _scolor = _status_html_colors.get(_status, "#e6edf3")
    title_spans.append(f'<span style="color:{_scolor};font-weight:bold;">[{_h(_sname)}]</span>')
    # [上下文XXK]（仅 >2000 时显示）
    _tokens = get_context_tokens(task.get("context"))
    if _tokens > 2000:
        _ctx_color_name = get_context_color(_tokens, task.get("provider", ""))
        _ctx_html_colors = {"context_red": "#FF6464", "yellow": "#d29922", "green": "#3fb950"}
        _ctx_html_color = _ctx_html_colors.get(_ctx_color_name, "#3fb950")
        title_spans.append(
            f'<span style="color:{_ctx_html_color};">[上下文{_tokens // 1000}K]</span>'
        )
    # [费用X.XX元 输入(命中X@单价 输入(未命中X@单价 输出X@单价]（仅 >0 时显示）
    _ctx_str = task.get("context")
    _cost = get_context_cost(_ctx_str)
    if _cost > 0:
        _cost_html_color = _get_cost_html_color(_cost)
        _usage = get_context_usage_detail(_ctx_str)
        # 从provider配置获取单价（元/百万tokens）
        _provider_name = task.get("provider", "")
        _price_hit = _price_miss = _price_out = 0
        if _provider_name:
            try:
                from peng.config.loader import load_config

                _cfg = load_config()
                _pcfg = _cfg.llm.get_provider(current=_provider_name, locked=True)
                _price_hit = _pcfg.get("price_input_hit", 0) or 0
                _price_miss = _pcfg.get("price_input_miss", 0) or 0
                _price_out = _pcfg.get("price_output", 0) or 0
            except Exception:
                pass

        def _fmt_m(n):
            return f"{n / 1_000_000:.3f}".rstrip("0").rstrip(".") + "M"

        title_spans.append(
            f'<span style="color:{_cost_html_color};font-weight:bold;">[费用{_cost:.2f}元]</span>'
            f'<span style="font-size:0.8em;color:#8b949e;">[输入命中缓存{_fmt_m(_usage["input_hit"])} ({_price_hit}元/M) 未命中{_fmt_m(_usage["input_miss"])} ({_price_miss}元/M) 输出{_fmt_m(_usage["output"])} ({_price_out}元/M)]</span>'
        )
    parts.append(f'<div class="task-title">{"&nbsp; ".join(title_spans)}</div>')

    # ── 内容 ──
    content_text = task.get("content", "")
    if content_text:
        parts.append(
            f'<div class="task-content">{_md_to_html(content_text, task.get("nest"))}</div>'
        )

    # ── 基本信息 ──
    status = task.get("status", "pending")
    status_names = {
        "pending": "待执行",
        "running": "执行中",
        "completed": "已完成",
        "failed": "已失败",
        "cancelled": "已取消",
        "paused": "已暂停",
        "interrupted": "已中断",
    }
    status_name = status_names.get(status, status)
    progress = task.get("progress", 0)
    model_str = "小模型" if task.get("small_model") else "大模型"

    info_rows = [
        ("ID", str(task.get("id"))),
        ("状态", (status_name, f"task-status status-{status}")),
        ("进度", (f"{progress}%", "task-progress")),
        ("已归档", "是" if task.get("is_archived") else "否"),
        ("类型", task.get("task_type", "auto")),
        ("模型", model_str),
    ]
    if task.get("creator"):
        info_rows.append(("创建者", task.get("creator", "")))
    info_rows.append(("复杂事务", "是" if task.get("is_complex") else "否"))
    parts.append(_section_html("📋 基本信息", _kv_table_html(info_rows)))

    # ── 时间 ──
    time_rows = [
        ("创建", format_datetime(task.get("created_at"))),
        ("开始", format_datetime(task.get("started_at"))),
        ("完成", format_datetime(task.get("completed_at"))),
        ("截止", format_datetime(task.get("deadline"))),
        ("心跳", format_datetime(task.get("heartbeat_at"))),
    ]
    # 心跳超时行：添加设置/重置超链接（参考定时器实现）
    _heartbeat_value = task.get("heartbeat_timeout") or 0
    _default_heartbeat = get_default_heartbeat_timeout()
    _heartbeat_display = _heartbeat_value if _heartbeat_value > 0 else _default_heartbeat
    _heartbeat_is_custom = _heartbeat_value > 0
    _heartbeat_links = f'<a href="javascript:void(0)" onclick="editTaskHeartbeat({task["id"]}, {_heartbeat_value})" style="margin-left:4ch; font-size:0.85em; color:#58a6ff; text-decoration:underline;">设置</a>'
    if _heartbeat_is_custom:
        _heartbeat_links += f' <a href="javascript:void(0)" onclick="resetTaskHeartbeat({task["id"]})" style="margin-left:1ch; font-size:0.85em; color:#f85149; text-decoration:underline;">重置默认</a>'
    _heartbeat_cell = _SafeHtml(f"{_heartbeat_display} 秒{_heartbeat_links}")
    time_rows.append(("心跳超时", _heartbeat_cell))
    time_rows.append(("超时", f"{task.get('timeout', 0)} 秒"))
    # 定时器行：添加设置/清除超链接
    timer_value = task.get("timer", 0)
    timer_links = f'<a href="javascript:void(0)" onclick="editTaskTimer({task["id"]}, {timer_value})" style="margin-left:4ch; font-size:0.85em; color:#58a6ff; text-decoration:underline;">设置</a>'
    if timer_value and timer_value > 0:
        timer_links += f' <a href="javascript:void(0)" onclick="clearTaskTimer({task["id"]})" style="margin-left:1ch; font-size:0.85em; color:#f85149; text-decoration:underline;">清除</a>'
    timer_cell = _SafeHtml(f"{timer_value} 秒{timer_links}")
    time_rows.append(("定时器", timer_cell))
    parts.append(_section_html("⏰ 时间", _kv_table_html(time_rows)))

    # ── 关联 ──
    relation_rows = [
        ("关联消息", str(task.get("message_id", ""))),
        ("根事务", str(task.get("root_task_id") or task.get("id"))),
        ("父事务", str(task.get("parent_task_id") or "")),
        ("深度", str(task.get("depth", 0))),
        ("路径", str(task.get("path", ""))),
    ]
    parts.append(_section_html("🔗 关联", _kv_table_html(relation_rows)))

    # ── 工作环境 ──
    if (
        task.get("worktree_path")
        or task.get("workdir")
        or task.get("branch_name")
        or task.get("nest")
    ):
        wt_rows = []
        _edit_link = lambda field, current_val: _SafeHtml(
            f'{_h_with_links(current_val, task.get("nest"))} <a href="javascript:void(0)" onclick="editTaskField({task["id"]}, \'{field}\')" style="margin-left:4ch; font-size:0.85em; color:#58a6ff; text-decoration:underline;">设置</a>'
        )
        if task.get("nest"):
            wt_rows.append(("鹏🐣窝", _edit_link("nest", task["nest"])))
        if task.get("workdir"):
            wt_rows.append(("工作目录", _edit_link("workdir", task["workdir"])))
        if task.get("worktree_path"):
            wt_rows.append(("Worktree", _h_with_links(task["worktree_path"], task.get("nest"))))
        if task.get("branch_name"):
            wt_rows.append(("分支", _h_with_links(task["branch_name"], task.get("nest"))))
        parts.append(_section_html("📁 工作环境", _kv_table_html(wt_rows)))

    # ── 执行信息 ──
    if task.get("process_id") or task.get("error") or task.get("result"):
        exec_rows = []
        if task.get("process_id"):
            exec_rows.append(("进程ID", str(task["process_id"])))
        if task.get("error"):
            exec_rows.append(
                ("错误", (_h_with_links(task["error"], task.get("nest")), "text-error"))
            )
        if task.get("result"):
            result = task["result"]
            if len(result) > 500:
                exec_rows.append(
                    ("结果", (_h_with_links(result[:500], task.get("nest")) + "... (已截断)"))
                )
            else:
                exec_rows.append(("结果", _h_with_links(result, task.get("nest"))))
        parts.append(_section_html("⚙️ 执行信息", _kv_table_html(exec_rows)))

    # ── 子事务配置 ──
    if task.get("subtask_config"):
        parts.append(
            _section_html(
                "🔧 子事务配置",
                f'<div class="task-text">{_h_with_links(task["subtask_config"], task.get("nest"))}</div>',
            )
        )

    # ── Tool Call 日志 ──
    if task.get("tool_calls_log") and task.get("tool_calls_log") != "[]":
        parts.append(
            _section_html(
                "📝 Tool Call 日志",
                f'<div class="task-text">{_h_with_links(task["tool_calls_log"], task.get("nest"))}</div>',
            )
        )

    # ── Tool Result 日志 ──
    if task.get("tool_results_log") and task.get("tool_results_log") != "[]":
        parts.append(
            _section_html(
                "📝 Tool Result 日志",
                f'<div class="task-text">{_h_with_links(task["tool_results_log"], task.get("nest"))}</div>',
            )
        )

    # ── 描述 ──
    if task.get("description"):
        desc = task["description"]
        if len(desc) > 1000:
            desc = desc[:1000] + "\n\n*... (已截断)*"
        parts.append(_section_html("📝 描述", _md_to_html(desc, task.get("nest"))))

    # ── 提示词 ──
    # 始终显示提示词字段，不论是否为默认值
    prompt = task.get("prompt", "")
    if prompt and len(prompt) > 1000:
        prompt = prompt[:1000] + "... (已截断)"

    # 获取系统提示词（从 prompts.jsonp、AGENTS.md 和 task_prompt）
    def _get_system_prompt():
        """获取系统提示词内容（与 agent._prepare_prompt 实际注入逻辑对齐，事务#2242）"""
        import asyncio

        from peng.common import jsonp as json
        from peng.config import get_path

        # ── 主系统提示词：全局 AGENTS.md（项目根）→ 回退 prompts.jsonp system_prompt ──
        system_prompt = ""
        # 从 prompts.jsonp 读取 system_prompt（使用 jsonp 模块支持注释）
        prompts_path = get_path("prompts_json")
        prompts_dict = {}
        if prompts_path.exists():
            with open(prompts_path, encoding="utf-8") as f:
                prompts_config = json.load(f)
            prompts_dict = prompts_config.get("prompts", prompts_config)
            system_prompt = prompts_dict.get("system_prompt", {}).get("content", "")

        # 加载全局 AGENTS.md（如果存在）
        # 🔧 事务#2242：改为项目根目录基准（与 agent._load_agent_md 的 #2105 修正一致，
        # 消除 CWD 依赖导致的跨工作目录读取污染）
        from peng.config.loader import get_project_root

        project_root = Path(get_project_root())
        agent_md = ""
        for path in [project_root / "AGENTS.md", project_root / ".peng" / "AGENTS.md"]:
            if path.exists():
                agent_md = path.read_text(encoding="utf-8")
                break

        # 主系统提示词二选一（与实际注入一致：AGENTS.md 存在则替代 system_prompt）
        combined = agent_md if agent_md else system_prompt

        # 获取 task_prompt（与 _prepare_prompt 逻辑一致）
        # prompt = task.get("prompt") or "task_prompt"
        # 如果 prompt 在 prompts_dict 中，取其 content；否则直接使用 prompt 值
        prompt_key = task.get("prompt") or "task_prompt"
        if prompt_key in prompts_dict:
            task_prompt_content = prompts_dict[prompt_key].get("content") or prompt_key
        else:
            task_prompt_content = prompt_key

        if task_prompt_content:
            combined += "\n\n---\n\n" + task_prompt_content

        # ── 尾部追加：事务所在 nest 工作目录的 AGENTS.md（如果存在）（事务#2242）──
        try:
            from peng.llm.client import load_task_agents_md

            _task_for_md = {"nest": task.get("nest"), "workdir": task.get("workdir")}
            if _task_for_md.get("workdir"):
                task_agents_md = None
                if (_task_for_md.get("nest") or "local") == "local":
                    # local nest：纯同步本地直读
                    from peng.llm.client import _read_agents_md_local

                    task_agents_md = _read_agents_md_local(_task_for_md["workdir"])
                else:
                    # 远程 nest：无事件循环时 asyncio.run（_cmd_show_html 为同步上下文），
                    # 已在事件循环中（如转发线程内联协程）则跳过远程读取（展示降级）
                    try:
                        asyncio.get_running_loop()
                    except RuntimeError:
                        task_agents_md = asyncio.run(load_task_agents_md(_task_for_md))
                if task_agents_md and task_agents_md != agent_md:
                    combined += "\n\n---\n\n" + task_agents_md
        except Exception as e:
            logger.warning(f"[task show] 读取工作目录AGENTS.md用于展示失败(忽略): {e}")

        return combined

    system_prompt_content = _get_system_prompt()

    # 构建提示词 HTML：任务提示词 + 可点击展开的系统提示词超链接
    prompt_html = ""
    if prompt:
        prompt_html += f'<div class="task-text">{_md_to_html(prompt, task.get("nest"))}</div>'
    else:
        prompt_html += '<div class="task-text" style="color:#8b949e;">(无任务提示词)</div>'

    # 添加系统提示词超链接（可点击展开）
    system_prompt_link = (
        '<div class="task-section" id="system-prompt-section-' + str(task["id"]) + '">'
        '<h3 class="system-prompt-toggle" onclick="toggleSystemPrompt('
        + str(task["id"])
        + ', this)" style="cursor:pointer;text-decoration:underline;color:#58a6ff;">'
        '<span class="toggle-title">🤖 系统提示词</span> <span class="toggle-hint" style="font-size:0.85em;color:#8b949e;">(点击展开)</span>'
        "</h3>"
        '<div class="system-prompt-full" style="display:none">'
        "<style>"
        ".system-prompt-full p, .system-prompt-full div, .system-prompt-full span {"
        "  margin: 0 !important; padding: 0 !important; line-height: 1.2 !important;"
        "}"
        "</style>"
        '<div style="word-break:break-word;">'
        + _md_to_html(system_prompt_content, task.get("nest"))
        + "</div>"
        "</div>"
        "</div>"
    )

    parts.append(_section_html("💡 提示词", prompt_html + system_prompt_link))

    # ── 元数据 ──
    if task.get("metadata") and task.get("metadata") != "{}":
        parts.append(
            _section_html(
                "🏷️ 元数据",
                f'<div class="task-text">{_h_with_links(task["metadata"], task.get("nest"))}</div>',
            )
        )

    # ── 依赖 ──
    deps = tm.get_dependencies(args.id)
    if deps:
        dep_items = []
        for dep_id in deps:
            dep_task = tm.get_task(dep_id)
            if dep_task:
                dep_id_formatted = format_task_id(dep_id, dep_task)
                clean_dep_id = re.sub(r"\x1b\[[0-9;]*m", "", dep_id_formatted)
                status_str = format_status(dep_task.get("status", "pending"))
                clean_status = re.sub(r"\x1b\[[0-9;]*m", "", status_str)
                dep_items.append(
                    f'<li><span class="task-id">{_h(clean_dep_id)}</span> <span class="task-status-text">[{_h(clean_status)}]</span> {_h_with_links(dep_task.get("content", ""), dep_task.get("nest"))}</li>'
                )
        parts.append(_section_html("🔗 依赖", f'<ul class="md-list">{"".join(dep_items)}</ul>'))

    # ── 被依赖 ──
    dependents = tm.get_dependents(args.id)
    if dependents:
        dep_items = []
        for dep_id in dependents:
            dep_task = tm.get_task(dep_id)
            if dep_task:
                dep_id_formatted = format_task_id(dep_id, dep_task)
                clean_dep_id = re.sub(r"\x1b\[[0-9;]*m", "", dep_id_formatted)
                status_str = format_status(dep_task.get("status", "pending"))
                clean_status = re.sub(r"\x1b\[[0-9;]*m", "", status_str)
                dep_items.append(
                    f'<li><span class="task-id">{_h(clean_dep_id)}</span> <span class="task-status-text">[{_h(clean_status)}]</span> {_h_with_links(dep_task.get("content", ""), dep_task.get("nest"))}</li>'
                )
        parts.append(_section_html("🔗 被依赖", f'<ul class="md-list">{"".join(dep_items)}</ul>'))

    # ── 上下文（可点击超链接展开） ──
    tokens = get_context_tokens(task.get("context"))
    if tokens:
        context_str = task.get("context", "{}")
        # 解析 context JSON 获取消息列表
        import json

        try:
            context_data = json.loads(context_str)
            messages = (
                context_data.get("messages", []) if isinstance(context_data, dict) else context_data
            )
        except Exception:
            messages = []
        # 构建对话形式 HTML（markdown 高亮）
        messages_html = ""
        role_icons = {"system": "🤖", "user": "👤", "assistant": "🐣", "tool": "🔧"}
        role_colors = {
            "system": "#58a6ff",
            "user": "#d29922",
            "assistant": "#3fb950",
            "tool": "#bc8cff",
        }
        for i, msg in enumerate(messages):
            role = msg.get("role", "unknown")
            content = _content_to_text(msg.get("content"))
            reasoning = _content_to_text(msg.get("reasoning_content"))
            tool_calls = msg.get("tool_calls")
            icon = role_icons.get(role, "❓")
            color = role_colors.get(role, "#8b949e")
            # 构建内容 HTML
            parts_html = []
            # 1. reasoning_content（灰色，始终显示在前）
            if reasoning:
                parts_html.append(
                    f'<div style="color:#8b949e;font-size:0.9em;">{_md_to_html(reasoning, task.get("nest"))}</div>'
                )
            # 2. content（正常颜色）
            if content:
                parts_html.append(_md_to_html(content, task.get("nest")))
            # 3. tool_calls（紫色，简洁展示工具调用）
            if tool_calls:
                tc_items = []
                for tc in tool_calls:
                    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                    fn_name = _h(fn.get("name", "?"))
                    fn_args = fn.get("arguments", "")
                    try:
                        import json as _json

                        args_parsed = _json.loads(fn_args) if isinstance(fn_args, str) else fn_args
                        args_str = _json.dumps(args_parsed, ensure_ascii=False)
                    except Exception:
                        args_str = str(fn_args)
                    tc_items.append(
                        f'<div style="margin:2px 0;">⚡ <strong>{fn_name}</strong> <code style="color:#bc8cff;font-size:0.85em;">{_h(args_str)}</code></div>'
                    )
                parts_html.append(f'<div style="margin-top:2px;">{"".join(tc_items)}</div>')
            content_html = (
                "".join(parts_html) if parts_html else '<span style="color:#8b949e">(空)</span>'
            )
            # 为每条消息添加删除/截断超链接（system 消息不允许删除）
            if role != "system":
                actions_html = f"""<span style="float:right;font-size:0.8em;">
                    <a href="javascript:void(0)" onclick="deleteContextMessage({task["id"]}, {i})" style="color:#f85149;text-decoration:underline;margin-left:8px;" title="删除本条消息及其关联消息">删除</a>
                    <a href="javascript:void(0)" onclick="truncateContextFrom({task["id"]}, {i})" style="color:#d29922;text-decoration:underline;margin-left:8px;" title="从此处截断所有后续消息（含本条）">截断</a>
                </span>"""
            else:
                actions_html = ""
            messages_html += f'''<div class="context-message" data-msg-index="{i}" style="margin:8px 0;padding:6px;border-left:3px solid {color};background:rgba({color}22,0.1)">
                <div style="color:{color};font-weight:bold;margin-bottom:4px;overflow:hidden;">{icon} {role} <span style="font-size:0.8em;color:#8b949e">[{i + 1}]</span>{actions_html}</div>
                <div class="context-content">{content_html}</div>
            </div>'''
        # 可点击超链接：默认隐藏内容，标题内小字提示，点击后显示全部消息
        # 在完整上下文底部添加折叠超链接
        collapse_html = f"""<div style="text-align:center;margin:12px 0;">
            <span class="context-collapse" onclick="toggleContext({task["id"]}, this)" style="cursor:pointer;text-decoration:underline;color:#58a6ff;">
                ⬆️ 折叠上下文
            </span>
        </div>"""
        parts.append(f"""<div class="task-section" id="context-section-{task["id"]}">
            <h3 class="context-toggle" onclick="toggleContext({task["id"]}, this)" style="cursor:pointer;text-decoration:underline;color:#58a6ff;">
                <span class="toggle-title">📜 当前上下文</span> <span style="font-size:0.85em;color:#8b949e">({tokens // 1000}K tokens, {len(messages)} 条消息)</span> <span class="toggle-hint" style="font-size:0.75em;color:#58a6ff;">(点击展开)</span>
            </h3>
            <div class="context-full" style="display:none">{messages_html}{collapse_html}</div>
        </div>""")

    # ── Info ──
    if task.get("info") and task.get("info") != "[]":
        info_list = parse_info_to_list_with_status(task.get("info"))
        if info_list:
            read_count = sum(1 for item in info_list if item["read"])
            unread_count = len(info_list) - read_count
            # 构建所有 info 的 HTML（已读灰色，未读绿色）——使用路径超链接转换
            all_info_html = ""
            for i, item in enumerate(info_list, 1):
                color = "#8b949e" if item["read"] else "#3fb950"
                unread_link = f'<a href="javascript:void(0)" onclick="markTaskInfoItemUnread({task["id"]}, {i - 1}, {str(item["read"]).lower()})" style="margin-left:1ch; color:#58a6ff; font-size:0.85em; text-decoration:underline;">{"改未读" if item["read"] else "改已读"}</a>'
                delete_link = f'<a href="javascript:void(0)" onclick="deleteTaskInfoItem({task["id"]}, {i - 1})" style="margin-left:1ch; color:#f85149; font-size:0.85em; text-decoration:underline;">删除</a>'
                # 事务#2089：显示发送者标识（旧数据无sender时不显示）
                sender_span = (
                    f'<span style="color:#58a6ff">[来自 {item["sender"]}]</span> '
                    if item.get("sender")
                    else ""
                )
                all_info_html += f'<div class="task-text task-info-item" data-info-index="{i - 1}" style="color:{color}">[{i}] {sender_span}{_h_with_links(item["content"], task.get("nest"))}{unread_link}{delete_link}</div>\n'
            # 默认显示：最后一条已读 + 所有未读（保持原样）——使用路径超链接转换
            default_html = ""
            last_read_idx = None
            last_read_content = None
            for i, item in enumerate(info_list, 1):
                if item["read"]:
                    last_read_idx = i
                    last_read_content = item["content"]
            if last_read_idx:
                default_html += f'<div class="task-text" style="color:#8b949e">📖 最后已读 [{last_read_idx}]: {_h_with_links(last_read_content, task.get("nest"))}</div>'
            if unread_count > 0:
                default_html += f'<div class="task-text" style="color:#3fb950">未读 {unread_count} 条 <a href="javascript:void(0)" onclick="markTaskInfoRead({task["id"]})" style="color:#58a6ff; font-size:0.85em; text-decoration:underline;">标记为已读（不处理）</a></div>'
                for i, item in enumerate(info_list, 1):
                    if not item["read"]:
                        sender_span = (
                            f'<span style="color:#58a6ff">[来自 {item["sender"]}]</span> '
                            if item.get("sender")
                            else ""
                        )
                        default_html += f'<div class="task-text task-info-item" style="color:#3fb950">[{i}] {sender_span}{_h_with_links(item["content"], task.get("nest"))}</div>'
            # 注入信息标题为可点击超链接，点击后替换为全部 info 内容
            parts.append(f"""<div class="task-section" id="info-section-{task["id"]}">
                <h3 class="task-info-toggle" onclick="toggleTaskInfo({task["id"]}, this)" style="cursor:pointer; text-decoration:underline;">
                    <span class="toggle-title">💡 注入信息</span> <span style="font-size:0.85em; color:#8b949e">({read_count}已读/{unread_count}未读)</span><a href="javascript:void(0)" onclick="event.stopPropagation(); clearTaskInfo({task["id"]})" style="margin-left:4ch; font-size:0.85em; color:#f85149; text-decoration:underline;">清除</a>
                </h3>
                <div class="task-info-default">{default_html}</div>
                <div class="task-info-full" style="display:none">{all_info_html}</div>
            </div>""")

    # ── Provider ──
    if task.get("provider"):
        provider_name = task.get("provider", "")
        try:
            from peng.config.loader import get_llm_config

            llm_config = get_llm_config()
            provider_icon = llm_config.get_icon(provider_name) or "🌐"
        except Exception:
            provider_icon = "🌐"
        parts.append(
            _section_html(
                f"{provider_icon} 大模型接口", f'<div class="task-text">{_h(provider_name)}</div>'
            )
        )

    # ── 事项清单 ──
    if task.get("checklist"):
        checklist_html = _md_to_html(task["checklist"], task.get("nest"))
        parts.append(
            f'<div class="task-section">'
            f"<h3>✅ 事项清单"
            f'<a href="javascript:void(0)" onclick="clearTaskChecklist({task["id"]})" '
            f'style="margin-left:4ch; font-size:0.85em; color:#f85149; text-decoration:underline;">清除</a>'
            f"</h3>{checklist_html}</div>"
        )

    # ── 工作汇报 ──
    if task.get("report"):
        parts.append(_section_html("📊 工作汇报", _md_to_html(task["report"], task.get("nest"))))

    print(f'<div class="task-detail-html">{"\n".join(parts)}</div>')
    return 0


def cmd_show(args):
    """显示事务详情"""
    if getattr(args, "format", "ansi") == "html":
        return _cmd_show_html(args)

    # ── 默认 ANSI/Rich 格式 ──
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.table import Table
    from rich.text import Text
    from rich.theme import Theme

    tm = get_task_manager()

    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    # 自定义 Markdown 主题：标题用粗体 cyan，不用默认的紫色下划线
    md_theme = Theme(
        {
            "markdown.heading": "bold cyan",
            "markdown.h1": "bold cyan",
            "markdown.h2": "bold cyan",
            "markdown.h3": "bold cyan",
            "markdown.h4": "bold cyan",
            "markdown.code": "bold magenta",
            "markdown.code_block": "bold magenta",
            "markdown.item.bullet": "bold",
            "markdown.item.number": "bold",
        }
    )
    console = Console(theme=md_theme)

    # ── 标题 ──
    formatted_id = format_task_id(task["id"], task)
    clean_id = re.sub(r"\x1b\[[0-9;]*m", "", formatted_id)
    console.print()
    console.print(Text(clean_id, style="bold cyan"))

    # ── 内容 ──
    content_text = task.get("content", "")
    if content_text:
        console.print(Text(content_text))

    # ── 基本信息 ──
    console.print()
    console.print(Text("📋 基本信息", style="bold yellow"))
    info_table = _build_info_table(task, tm)
    console.print(info_table)

    # ── 时间 ──
    console.print()
    console.print(Text("⏰ 时间", style="bold yellow"))
    time_table = _build_time_table(task)
    console.print(time_table)

    # ── 关联 ──
    console.print()
    console.print(Text("🔗 关联", style="bold yellow"))
    relation_table = _build_relation_table(task)
    console.print(relation_table)

    # ── Git Worktree ──
    if (
        task.get("worktree_path")
        or task.get("workdir")
        or task.get("branch_name")
        or task.get("nest")
    ):
        console.print()
        console.print(Text("📁 工作环境", style="bold yellow"))
        worktree_table = _build_worktree_table(task)
        console.print(worktree_table)

    # ── 执行信息 ──
    if task.get("process_id") or task.get("error") or task.get("result"):
        console.print()
        console.print(Text("⚙️ 执行信息", style="bold yellow"))
        exec_table = Table(show_header=False, box=None, padding=(0, 2))
        exec_table.add_column(style="cyan bold", width=12)
        exec_table.add_column()
        if task.get("process_id"):
            exec_table.add_row("进程ID", str(task.get("process_id")))
        if task.get("error"):
            exec_table.add_row("错误", Text(task["error"], style="bold red"))
        if task.get("result"):
            result = task["result"]
            if len(result) > 500:
                exec_table.add_row("结果", result[:500] + "... (已截断)")
            else:
                exec_table.add_row("结果", result)
        console.print(exec_table)

    # ── 子事务配置 ──
    if task.get("subtask_config"):
        console.print()
        console.print(Text("🔧 子事务配置", style="bold yellow"))
        console.print(f"  {task.get('subtask_config')}")

    # ── Tool Call 日志 ──
    if task.get("tool_calls_log") and task.get("tool_calls_log") != "[]":
        console.print()
        console.print(Text("📝 Tool Call 日志", style="bold yellow"))
        console.print(f"  {task.get('tool_calls_log')}")

    # ── Tool Result 日志 ──
    if task.get("tool_results_log") and task.get("tool_results_log") != "[]":
        console.print()
        console.print(Text("📝 Tool Result 日志", style="bold yellow"))
        console.print(f"  {task.get('tool_results_log')}")

    # ── 描述 ──
    if task.get("description"):
        description = task["description"]
        console.print()
        console.print(Text("📝 描述", style="bold yellow"))
        if len(description) > 1000:
            console.print(
                Markdown(fix_markdown_linebreaks(description[:1000] + "\n\n*... (已截断)*"))
            )
        else:
            console.print(Markdown(fix_markdown_linebreaks(description)))

    # ── 提示词 ──
    if task.get("prompt"):
        prompt = task["prompt"]
        console.print()
        console.print(Text("💡 提示词", style="bold yellow"))
        if len(prompt) > 1000:
            console.print(Text(prompt[:1000] + "... (已截断)"))
        else:
            console.print(Text(prompt))

    # ── 元数据 ──
    if task.get("metadata") and task.get("metadata") != "{}":
        console.print()
        console.print(Text("🏷️ 元数据", style="bold yellow"))
        console.print(f"  {task.get('metadata')}")

    # ── 依赖 ──
    deps = tm.get_dependencies(args.id)
    if deps:
        console.print()
        console.print(Text("🔗 依赖", style="bold yellow"))
        for dep_id in deps:
            dep_task = tm.get_task(dep_id)
            if dep_task:
                dep_id_formatted = format_task_id(dep_id, dep_task)
                clean_dep_id = re.sub(r"\x1b\[[0-9;]*m", "", dep_id_formatted)
                status_str = format_status(dep_task.get("status", "pending"))
                clean_status = re.sub(r"\x1b\[[0-9;]*m", "", status_str)
                console.print(f"  {clean_dep_id} [{clean_status}] {dep_task.get('content', '')}")

    # ── 被依赖 ──
    dependents = tm.get_dependents(args.id)
    if dependents:
        console.print()
        console.print(Text("🔗 被依赖", style="bold yellow"))
        for dep_id in dependents:
            dep_task = tm.get_task(dep_id)
            if dep_task:
                dep_id_formatted = format_task_id(dep_id, dep_task)
                clean_dep_id = re.sub(r"\x1b\[[0-9;]*m", "", dep_id_formatted)
                status_str = format_status(dep_task.get("status", "pending"))
                clean_status = re.sub(r"\x1b\[[0-9;]*m", "", status_str)
                console.print(f"  {clean_dep_id} [{clean_status}] {dep_task.get('content', '')}")

    # ── 上下文长度 ──
    tokens = get_context_tokens(task.get("context"))
    if tokens:
        console.print()
        # Rich Console 不支持自定义颜色名，映射为 hex 值
        _RICH_COLOR_MAP = {"context_red": "#FF6464", "yellow": "yellow", "green": "green"}
        color_name = get_context_color(tokens, task.get("provider", ""))
        rich_color = _RICH_COLOR_MAP.get(color_name, "green")
        style = f"bold {rich_color}"
        console.print(Text(f"📜 当前上下文长度：{tokens // 1000}K", style=style))

    # ── Info ──
    if task.get("info") and task.get("info") != "[]":
        console.print()
        console.print(Text("💡 注入信息", style="bold yellow"))
        info_list = parse_info_to_list_with_status(task.get("info"))
        if info_list:
            read_count = sum(1 for item in info_list if item["read"])
            unread_count = len(info_list) - read_count
            console.print(f"  已读 {read_count} 条")
            # 显示最后一条已读的内容
            last_read_idx = None
            last_read_content = None
            for i, item in enumerate(info_list, 1):
                if item["read"]:
                    last_read_idx = i
                    last_read_content = item["content"]
            if last_read_idx:
                console.print(
                    Text(f"  📖 最后已读 [{last_read_idx}]: {last_read_content}", style="dim")
                )
            if unread_count > 0:
                console.print(Text(f"  未读 {unread_count} 条：", style="bold green"))
                for i, item in enumerate(info_list, 1):
                    if not item["read"]:
                        console.print(Text(f"    [{i}] {item['content']}", style="green"))
        else:
            console.print("  (无注入信息)")

    # ── Provider ──
    if task.get("provider"):
        console.print()
        provider_name = task.get("provider", "")
        try:
            from peng.config.loader import get_llm_config

            llm_config = get_llm_config()
            provider_icon = llm_config.get_icon(provider_name) or "🌐"
        except Exception:
            provider_icon = "🌐"
        console.print(Text(f"{provider_icon} 大模型接口", style="bold yellow"))
        console.print(f"  {provider_name}")

    # ── 事项清单（Markdown 渲染）──
    if task.get("checklist"):
        console.print()
        console.print(Text("✅ 事项清单", style="bold yellow"))
        console.print(Markdown(fix_markdown_linebreaks(task["checklist"])))

    # ── 工作汇报（Markdown 渲染）──
    if task.get("report"):
        console.print()
        console.print(Text("📊 工作汇报", style="bold yellow"))
        console.print(Markdown(fix_markdown_linebreaks(task["report"])))

    console.print()
    return 0


def cmd_tree(args):
    """显示事务分支树（从该事务开始）"""
    tm = get_task_manager()

    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    if task.get("parent_task_id"):
        label = f"子事务 #{args.id}"
    else:
        label = f"根事务 #{args.id}"
    print(f"\n{Colors.BOLD}{label} 的分支树{Colors.RESET}")
    print("-" * 60)

    tree_text = tm.db.get_tree_text(args.id, content_max_len=0)
    if tree_text:
        print(tree_text)

    # 统计信息（使用 database 的 get_descendants 来统计该事务及所有子事务）
    descendants = tm.db.get_descendants(args.id)
    # descendants 不包括事务本身，所以 +1
    print(f"\n共 {len(descendants) + 1} 个事务")
    return 0


def cmd_full_tree(args):
    """显示事务完整树（从根事务开始）"""
    tm = get_task_manager()

    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    # 找到根事务
    root_id = task.get("root_id") or args.id

    print(f"\n{Colors.BOLD}事务 #{args.id} 的完整树（根事务 #{root_id}）{Colors.RESET}")
    print("-" * 60)

    tree_text = tm.db.get_tree_text(root_id, content_max_len=0)
    if tree_text:
        print(tree_text)

    # 统计信息
    subtree = tm.get_subtree(root_id)
    print(f"\n共 {len(subtree)} 个事务")
    return 0


def cmd_msg(args):
    """显示事务#<id>的根事务的根消息的完整恢复链上下文（同 bin/msg context）"""
    tm = get_task_manager()

    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    # 找到根事务
    root_id = task.get("root_id") or args.id

    # 获取根事务
    root_task = tm.get_task(root_id) if root_id != args.id else task
    if not root_task:
        print(f"未找到根事务 #{root_id}")
        return 1

    message_id = root_task.get("message_id")
    if not message_id:
        print(f"根事务 #{root_id} 没有关联消息")
        return 0

    # 构建根消息的完整恢复链上下文（与 bin/msg context 逻辑一致）
    from peng.message.database import MessageDatabase

    db = MessageDatabase()
    context = db.build_context_for_message(message_id)
    if not context:
        print(f"无法构建消息 #{message_id} 的上下文")
        return 1

    print(context)
    return 0


def cmd_context(args):
    """事务上下文操作"""
    return _cmd_field_operation(args, "context", "上下文")


def cmd_pause(args):
    """暂停事务（递归暂停子事务）"""
    tm = get_task_manager()

    # 检查事务是否存在
    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    # 检查事务是否可以暂停
    terminal_states = ("completed", "failed", "cancelled")
    if task["status"] in terminal_states:
        formatted_id = format_task_id(args.id, task)
        print(f"错误: 事务 {formatted_id} 已完成，无法暂停", file=sys.stderr)
        return 1

    # 使用控制表机制
    from datetime import datetime
    from pathlib import Path

    try:
        project_root = get_project_root()
        from peng.config import get_path

        # 获取所有需要暂停的事务（递归）
        all_task_ids = [args.id]

        def collect_recursive(task_id: int):
            children = tm.get_children(task_id)
            for child in children:
                all_task_ids.append(child["id"])
                collect_recursive(child["id"])

        collect_recursive(args.id)

        # 使用TaskDatabase的insert_task_control方法
        for task_id in all_task_ids:
            tm.db.insert_task_control(task_id, "pause")

        formatted_id = format_task_id(args.id, task)
        print(f"事务树 {formatted_id} 暂停控制已加入队列 ({len(all_task_ids)} 个事务)")
        return 0

    except Exception as e:
        import traceback

        print(f"错误: 写入控制表失败: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1


def cmd_cancel(args):
    """取消事务（递归取消子事务）"""
    tm = get_task_manager()

    # 检查事务是否存在
    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    # 使用控制表机制
    from datetime import datetime
    from pathlib import Path

    try:
        project_root = get_project_root()
        from peng.config import get_path

        # 获取所有需要取消的事务（递归）
        all_task_ids = [args.id]

        def collect_recursive(task_id: int):
            children = tm.get_children(task_id)
            for child in children:
                all_task_ids.append(child["id"])
                collect_recursive(child["id"])

        collect_recursive(args.id)

        # 使用TaskDatabase的insert_task_control方法
        for task_id in all_task_ids:
            tm.db.insert_task_control(task_id, "cancel")

        formatted_id = format_task_id(args.id, task)
        print(f"事务树 {formatted_id} 取消控制已加入队列 ({len(all_task_ids)} 个事务)")
        return 0

    except Exception as e:
        import traceback

        print(f"错误: 写入控制表失败: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1


def cmd_up(args):
    """提高优先级（递归提高子事务）"""
    tm = get_task_manager()

    # 检查事务是否存在
    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    # 使用控制表机制
    from datetime import datetime
    from pathlib import Path

    try:
        # 获取所有需要提高优先级的事务（递归）
        all_task_ids = [args.id]

        def collect_recursive(task_id: int):
            children = tm.get_children(task_id)
            for child in children:
                all_task_ids.append(child)
                collect_recursive(child)

        collect_recursive(args.id)

        # 使用TaskDatabase的insert_task_control方法
        for task_id in all_task_ids:
            tm.db.insert_task_control(task_id, "resume")

        formatted_id = format_task_id(args.id, task)
        print(f"事务树 {formatted_id} 提高优先级控制已加入队列 ({len(all_task_ids)} 个事务)")
        return 0

    except Exception as e:
        import traceback

        print(f"错误: 写入控制表失败: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1


def cmd_down(args):
    """降低优先级（递归降低子事务）"""
    tm = get_task_manager()

    # 检查事务是否存在
    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    # 使用控制表机制
    from datetime import datetime
    from pathlib import Path

    try:
        # 获取所有需要降低优先级的事务（递归）
        all_task_ids = [args.id]

        def collect_recursive(task_id: int):
            children = tm.get_children(task_id)
            for child in children:
                all_task_ids.append(child)
                collect_recursive(child)

        collect_recursive(args.id)

        # 使用TaskDatabase的insert_task_control方法
        for task_id in all_task_ids:
            tm.db.insert_task_control(task_id, "kill")

        formatted_id = format_task_id(args.id, task)
        print(f"事务树 {formatted_id} 降低优先级控制已加入队列 ({len(all_task_ids)} 个事务)")
        return 0

    except Exception as e:
        import traceback

        print(f"错误: 写入控制表失败: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1


def cmd_restart(args):
    """重启事务（从步骤1开始）"""
    tm = get_task_manager()

    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    # 使用控制表机制（restart_task 会自动处理运行中事务的暂停）
    from datetime import datetime
    from pathlib import Path

    try:
        project_root = get_project_root()
        from peng.config import get_path

        if args.force:
            tm.db.update_task(args.id, checklist="")
        # 使用TaskDatabase的insert_task_control方法
        tm.db.insert_task_control(args.id, "restart")

        formatted_id = format_task_id(args.id, task)
        print(f"事务 {formatted_id} 重启控制已加入队列")
        return 0

    except Exception as e:
        import traceback

        print(f"错误: 写入控制表失败: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1


def cmd_resume(args):
    """恢复事务（从当前步骤继续）"""
    tm = get_task_manager()

    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    # 使用控制表机制
    from datetime import datetime
    from pathlib import Path

    try:
        # 使用TaskDatabase的insert_task_control方法
        tm.db.insert_task_control(args.id, "resume")
        formatted_id = format_task_id(args.id, task)
        print(f"事务 {formatted_id} 恢复控制已加入队列")
        return 0

    except Exception as e:
        import traceback

        print(f"错误: 写入控制表失败: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1


def cmd_start(args):
    """启动待定事务"""
    tm = get_task_manager()

    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    # 只能启动 pending 状态的事务
    if task["status"] != "pending":
        formatted_id = format_task_id(args.id, task)
        status_text = format_status(task["status"])
        print(
            f"错误: 事务 {formatted_id} 状态为 {status_text}，只有待执行(pending)状态的事务可以启动",
            file=sys.stderr,
        )
        return 1

    # 使用控制表机制
    from datetime import datetime

    try:
        # 使用TaskDatabase的insert_task_control方法
        tm.db.insert_task_control(args.id, "start")

        formatted_id = format_task_id(args.id, task)
        print(f"事务 {formatted_id} 启动控制已加入队列")
        return 0

    except Exception as e:
        import traceback

        print(f"错误: 写入控制表失败: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1


def cmd_chain(args):
    """显示调用链/进程信息"""
    tm = get_task_manager()

    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    formatted_id = format_task_id(args.id, task)
    print(f"\n{Colors.BOLD}事务 {formatted_id} 的调用链{Colors.RESET}")
    print("=" * 70)

    # 显示调用链
    chain = task.get("chain", [])
    if not chain:
        print("暂无调用链信息")
        return 0

    for i, item in enumerate(chain, 1):
        caller = item.get("caller", "unknown")
        session_id = item.get("caller_session_id", "N/A")
        timestamp = item.get("timestamp", "")
        print(f"{i}. 调用者: {caller}")
        print(f"   会话ID: {session_id}")
        print(f"   时间戳: {timestamp}")
        print()

    return 0


def cmd_status(args):
    """处理 status 子命令"""
    tm = get_task_manager()

    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    # 如果提供了状态值，设置事务状态
    if args.status_value is not None:
        current_status = task.get("status")
        success = False
        if not (args.status_value == "completed" and "@endless" in task.get("content")):
            success = tm.update_task_status(args.id, current_status, args.status_value)

        if success:
            formatted_id = format_task_id(args.id, task)
            print(
                f"{Colors.GREEN}✅ 事务 {formatted_id} 状态已设置为 {args.status_value}{Colors.RESET}"
            )
            return 0
        else:
            print(f"{Colors.RED}❌ 无法更新事务状态{Colors.RESET}", file=sys.stderr)
            return 1
    else:
        # 没有状态值，显示事务状态
        status = task.get("status", "unknown")
        status_colors = {
            "pending": Colors.YELLOW,
            "running": Colors.BLUE,
            "completed": Colors.GREEN,
            "failed": Colors.RED,
        }
        status_names = {
            "pending": "待执行",
            "running": "执行中",
            "completed": "已完成",
            "failed": "已失败",
        }
        status_color = status_colors.get(status, Colors.WHITE)
        status_name = status_names.get(status, status)
        formatted_id = format_task_id(args.id, task)
        print(f"事务 {formatted_id} 的状态：{status_color}{status_name}{Colors.RESET}")
        return 0


def cmd_delete(args):
    """递归删除事务及其所有子事务"""
    tm = get_task_manager()

    # 检查事务是否存在
    task = tm.get_task(args.id)
    force = True if args.force else False
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    # 检查事务状态：正在处理中的事务禁止删除
    if not force and task.get("status") == "running":
        formatted_id = format_task_id(args.id, task)
        print(
            f"{Colors.RED}❌ 错误: 事务 {formatted_id} 正在处理中，禁止删除，请先停止事务。{Colors.RESET}",
            file=sys.stderr,
        )
        return 1

    # 递归删除事务
    is_root = not task.get("parent_task_id")
    try:
        deleted_count = tm.db.delete_task_recursive(args.id, force)
        formatted_id = format_task_id(args.id, task)
        msg_note = "（关联消息已一并删除）" if is_root else ""
        print(
            f"{Colors.GREEN}✅ 事务树 {formatted_id} 已删除，共 {deleted_count} 个事务{msg_note}{Colors.RESET}"
        )
        return 0
    except Exception as e:
        import traceback

        print(f"{Colors.RED}❌ 删除失败: {e}{Colors.RESET}", file=sys.stderr)
        traceback.print_exc()
        return 1


def _format_daemon_entry(entry: dict) -> str:
    """格式化daemon条目显示: d3f9a2(PID 12345, nest=epyc)"""
    nest = entry.get("nest")
    if nest:
        return f"{entry.get('id')}(PID {entry.get('pid')}, nest={nest})"
    return f"{entry.get('id')}(PID {entry.get('pid')})"


def _print_daemon_kill_result(result: dict, label: str = "") -> None:
    """打印daemon批量kill结果（pid维度）"""
    killed = result.get("killed", [])
    already_dead = result.get("already_dead", [])
    errors = result.get("errors", [])
    if killed:
        e_list = ", ".join(_format_daemon_entry(e) for e in killed)
        print(f"  {Colors.GREEN}{label}已杀死: [{e_list}]{Colors.RESET}")
    if already_dead:
        e_list = ", ".join(_format_daemon_entry(e) for e in already_dead)
        print(f"  {Colors.DIM}{label}已自行退出: [{e_list}]{Colors.RESET}")
    if errors:
        for err in errors:
            print(
                f"  {Colors.RED}{label}杀死失败: {err.get('id')} (PID {err.get('pid')}) - {err.get('error')}{Colors.RESET}"
            )


def cmd_kill(args):
    """杀死daemon后台进程：按daemon id（-d，可多个）或按事务ID"""
    daemon_ids = list(getattr(args, "daemon_id", None) or [])
    task_id = getattr(args, "id", None)

    if not daemon_ids and task_id is None:
        print(
            f"{Colors.RED}❌ 请指定事务ID（task kill <ID>）或daemon id（task kill -d <id>）{Colors.RESET}",
            file=sys.stderr,
        )
        return 1

    # 先清理已退出的daemon记录
    cleaned = cleanup_dead_daemons()

    if daemon_ids:
        # 按 daemon id 杀死（支持多个 -d）
        total_killed = []
        total_already_dead = []
        total_errors = []
        for daemon_id in daemon_ids:
            result = kill_daemon_by_id_sync(daemon_id)
            total_killed.extend(result["killed"])
            total_already_dead.extend(result["already_dead"])
            total_errors.extend(result["errors"])

        if not total_killed and not total_already_dead and not total_errors:
            print(f"{Colors.YELLOW}⚠️ 指定的daemon均不存在或已无记录{Colors.RESET}")
            if cleaned > 0:
                print(f"{Colors.DIM}已清理 {cleaned} 个已退出daemon的记录{Colors.RESET}")
            return 0

        print(f"{Colors.BOLD}daemon {', '.join(daemon_ids)} 清理结果:{Colors.RESET}")
        if cleaned > 0:
            print(f"  {Colors.DIM}清理 {cleaned} 个已退出daemon记录{Colors.RESET}")
        _print_daemon_kill_result(
            {
                "killed": total_killed,
                "already_dead": total_already_dead,
                "errors": total_errors,
            }
        )
        total = len(total_killed) + len(total_already_dead) + len(total_errors)
        print(
            f"\n共 {total} 个daemon（{len(total_killed)} 杀死, {len(total_already_dead)} 已退出, {len(total_errors)} 失败）"
        )
        return 1 if total_errors else 0

    # 按事务ID杀死该事务的所有daemon
    tm = get_task_manager()
    task = tm.get_task(task_id)
    formatted_id = format_task_id(task_id, task) if task else str(task_id)

    result = kill_task_daemons_sync(task_id)
    killed = result["killed"]
    already_dead = result["already_dead"]
    errors = result["errors"]

    if not killed and not already_dead and not errors:
        print(f"{Colors.YELLOW}⚠️ 事务 {formatted_id} 没有已注册的后台daemon{Colors.RESET}")
        if cleaned > 0:
            print(f"{Colors.DIM}已清理 {cleaned} 个已退出进程的记录{Colors.RESET}")
        return 0

    print(f"{Colors.BOLD}事务 {formatted_id} 后台daemon清理结果:{Colors.RESET}")
    if cleaned > 0:
        print(f"  {Colors.DIM}清理 {cleaned} 个已退出进程记录{Colors.RESET}")
    _print_daemon_kill_result(result)

    total = len(killed) + len(already_dead) + len(errors)
    print(
        f"\n共 {total} 个daemon（{len(killed)} 杀死, {len(already_dead)} 已退出, {len(errors)} 失败）"
    )

    return 1 if errors else 0


def cmd_killall(args):
    """杀死data/daemon.json中记录的所有后台daemon进程"""
    # 先清理已退出的daemon记录
    cleaned = cleanup_dead_daemons()

    # 杀死所有daemon
    result = kill_all_daemons_sync()
    killed = result["killed"]
    already_dead = result["already_dead"]
    errors = result["errors"]

    if not killed and not already_dead and not errors:
        print(f"{Colors.YELLOW}⚠️ 没有已注册的后台daemon{Colors.RESET}")
        if cleaned > 0:
            print(f"{Colors.DIM}已清理 {cleaned} 个已退出daemon的记录{Colors.RESET}")
        return 0

    print(f"{Colors.BOLD}杀死所有后台daemon:{Colors.RESET}")
    if cleaned > 0:
        print(f"  {Colors.DIM}清理 {cleaned} 个已退出daemon记录{Colors.RESET}")
    _print_daemon_kill_result(result)

    total = len(killed) + len(already_dead) + len(errors)
    print(
        f"\n共 {total} 个daemon（{len(killed)} 杀死, {len(already_dead)} 已退出, {len(errors)} 失败）"
    )

    return 1 if errors else 0


def cmd_daemon(args):
    """显示所有已注册的后台daemon（支持按nest过滤）"""
    nest_filter = None
    if getattr(args, "local", False):
        nest_filter = "local"  # 特殊值：只显示本地（nest=None）的daemon
    elif getattr(args, "nest", None):
        if args.nest.lower() == "local":
            nest_filter = "local"
        else:
            nest_filter = args.nest

    cleaned = cleanup_dead_daemons()
    if cleaned > 0:
        print(f"{Colors.DIM}已清理 {cleaned} 个已退出daemon记录{Colors.RESET}")

    all_data = list_daemons()
    if not all_data:
        print(f"{Colors.YELLOW}没有已注册的后台daemon{Colors.RESET}")
        return 0

    # 按nest过滤
    filtered = {}
    for daemon_id, entry in all_data.items():
        entry_nest = entry.get("nest")
        if nest_filter is None:
            filtered[daemon_id] = entry
        elif nest_filter == "local":
            if entry_nest is None:
                filtered[daemon_id] = entry
        elif entry_nest == nest_filter:
            filtered[daemon_id] = entry

    if not filtered:
        if nest_filter:
            print(f"{Colors.YELLOW}没有匹配的后台daemon (nest={nest_filter}){Colors.RESET}")
        else:
            print(f"{Colors.YELLOW}没有已注册的后台daemon{Colors.RESET}")
        return 0

    tm = get_task_manager()
    print(f"\n{Colors.BOLD}已注册的后台daemon{Colors.RESET}")
    print("=" * 60)

    # 按事务分组显示
    task_groups: dict[str, list] = {}
    for daemon_id, entry in filtered.items():
        task_groups.setdefault(str(entry.get("task_id") or ""), []).append(entry)

    for task_key in sorted(task_groups, key=lambda k: int(k) if k.isdigit() else -1):
        entries = task_groups[task_key]
        if task_key:
            task = tm.get_task(int(task_key))
            label = format_task_id(int(task_key), task) if task else f"#{task_key}"
        else:
            label = "(无关联事务)"
        print(f"  事务 {label}:")
        for entry in entries:
            cmd_preview = (entry.get("command") or "")[:50]
            nest_part = f", nest={entry['nest']}" if entry.get("nest") else ""
            log_part = f"\n      日志: {entry['log_file']}" if entry.get("log_file") else ""
            print(f"    {entry['id']}(PID {entry['pid']}{nest_part}) {cmd_preview}{log_part}")

    # nest统计
    nest_stats: dict[str | None, int] = {}
    for entry in filtered.values():
        n = entry.get("nest")
        nest_stats[n] = nest_stats.get(n, 0) + 1

    total = len(filtered)
    parts = []
    for n, count in sorted(nest_stats.items(), key=lambda x: (x[0] is not None, x[0] or "")):
        stat_label = n if n else "本地"
        parts.append(f"{stat_label}: {count}")
    print(f"\n共 {total} 个daemon ({', '.join(parts)})")
    print(f"{Colors.DIM}停止: task kill -d <daemon_id>，或bash/daemon工具 stop <daemon_id>{Colors.RESET}")
    return 0

def cmd_move(args):
    """移动事务到新的父事务下或变成树根"""
    tm = get_task_manager()

    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    formatted_id = format_task_id(args.id, task)

    # 执行移动
    success = tm.db.move_task(
        task_id=args.id,
        target_id=args.target,
    )

    if success:
        target_desc = f"#{args.target}" if args.target else "根"
        print(f"{Colors.GREEN}✅ 事务 {formatted_id} 已移动到 {target_desc}{Colors.RESET}")
        return 0
    else:
        print(
            f"{Colors.RED}❌ 事务 {formatted_id} 移动失败，请检查事务状态（运行中/已归档不可移动）{Colors.RESET}",
            file=sys.stderr,
        )
        return 1


def cmd_small_model(args):
    """设置事务使用小模型"""
    tm = get_task_manager()

    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    success = tm.db.update_task(args.id, small_model=1)

    if success:
        formatted_id = format_task_id(args.id, task)
        print(f"{Colors.GREEN}✅ 事务 {formatted_id} 已设置为使用小模型{Colors.RESET}")
    else:
        print(f"{Colors.RED}❌ 无法更新事务{Colors.RESET}", file=sys.stderr)
        return 1


def cmd_big_model(args):
    """设置事务使用大模型"""
    tm = get_task_manager()

    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    success = tm.db.update_task(args.id, small_model=0)

    if success:
        formatted_id = format_task_id(args.id, task)
        print(f"{Colors.GREEN}✅ 事务 {formatted_id} 已设置为使用大模型{Colors.RESET}")
    else:
        print(f"{Colors.RED}❌ 无法更新事务{Colors.RESET}", file=sys.stderr)
        return 1


def cmd_timer(args):
    """
    事务定时器操作

    timer字段用于设置事务的定时重启功能：
    - timer > 0: 事务完成后，每隔timer秒自动重启一次
    - timer = 0: 不启用定时重启（默认值）

    用法：
    - 无参数：显示当前timer值
    - 有参数：设置timer值（秒），0表示清除定时器
    - -d/--delete：清除定时器
    """
    tm = get_task_manager()

    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    formatted_id = format_task_id(args.id, task)
    timer_value = getattr(args, "seconds", None)
    delete_flag = getattr(args, "delete", False)

    # 如果使用 -d --delete 参数，清除定时器
    if delete_flag:
        success = tm.db.update_task(args.id, timer=0)
        if success:
            print(f"{Colors.GREEN}✅ 事务 {formatted_id} 定时器已清除{Colors.RESET}")
        else:
            print(f"{Colors.RED}❌ 无法更新事务{Colors.RESET}", file=sys.stderr)
            return 1
        return 0

    # 如果没有timer_value参数，显示当前timer值
    if timer_value is None:
        current_timer = task.get("timer", 0)
        if current_timer > 0:
            print(f"{Colors.CYAN}事务 {formatted_id} 的定时器{Colors.RESET}")
            print(f"  定时重启间隔: {current_timer} 秒")
            last_start = task.get("started_at")
            if last_start:
                last_start_time = datetime.fromisoformat(last_start)
                next_start_time = last_start_time.timestamp() + current_timer
                next_start = datetime.fromtimestamp(next_start_time).isoformat()
                print(f"  下次启动时间: {next_start}")
        else:
            print(f"{Colors.YELLOW}事务 {formatted_id} 未启用定时器{Colors.RESET}")
        return 0

    # 如果timer_value参数有内容，设置timer值
    try:
        timer_seconds = int(timer_value)
        if timer_seconds < 0:
            print(f"{Colors.RED}❌ timer值不能为负数{Colors.RESET}", file=sys.stderr)
            return 1

        # 更新timer字段
        # 如果timer设置为0，也清空start_at
        update_kwargs = {"timer": timer_seconds}
        success = tm.db.update_task(args.id, **update_kwargs)

        if success:
            if timer_seconds > 0:
                print(
                    f"{Colors.GREEN}✅ 事务 {formatted_id} 定时器已设置为 {timer_seconds} 秒{Colors.RESET}"
                )
                print(f"{Colors.DIM}事务将每隔 {timer_seconds} 秒自动重启{Colors.RESET}")
                last_start = task.get("started_at")
                if last_start:
                    last_start_time = datetime.fromisoformat(last_start)
                    next_start_time = last_start_time.timestamp() + timer_seconds
                    next_start = datetime.fromtimestamp(next_start_time).isoformat()
                    print(f"下次启动时间: {next_start}")
            else:
                print(f"{Colors.GREEN}✅ 事务 {formatted_id} 定时器已清除{Colors.RESET}")
        else:
            print(f"{Colors.RED}❌ 无法更新事务{Colors.RESET}", file=sys.stderr)
            return 1
    except ValueError:
        print(f"{Colors.RED}❌ timer值必须是整数{Colors.RESET}", file=sys.stderr)
        return 1


def cmd_heartbeat(args):
    """
    事务心跳超时操作

    heartbeat_timeout字段用于设置事务的心跳超时时间：
    - heartbeat_timeout > 0: 事务心跳超时时间（秒）
    - heartbeat_timeout = 0 或 NULL: 使用系统默认心跳超时时间

    用法：
    - 无参数：显示当前心跳超时值
    - 有参数：设置心跳超时值（秒），0表示使用默认值
    - -d/--delete：重置为默认值（设为0表示使用默认）
    """
    tm = get_task_manager()

    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    formatted_id = format_task_id(args.id, task)
    seconds_value = getattr(args, "seconds", None)
    delete_flag = getattr(args, "delete", False)

    # 如果使用 -d --delete 参数，重置为默认值（设为0表示使用默认）
    if delete_flag:
        success = tm.db.update_task(args.id, heartbeat_timeout=0)
        if success:
            default_timeout = get_default_heartbeat_timeout()
            print(
                f"{Colors.GREEN}✅ 事务 {formatted_id} 心跳超时已重置为默认值（{default_timeout} 秒）{Colors.RESET}"
            )
        else:
            print(f"{Colors.RED}❌ 无法更新事务{Colors.RESET}", file=sys.stderr)
            return 1
        return 0

    # 如果没有seconds参数，显示当前心跳超时值
    if seconds_value is None:
        current_timeout = task.get("heartbeat_timeout") or get_default_heartbeat_timeout()
        is_custom = bool(task.get("heartbeat_timeout") and task.get("heartbeat_timeout") > 0)
        print(f"{Colors.CYAN}事务 {formatted_id} 的心跳超时{Colors.RESET}")
        if is_custom:
            print(f"  心跳超时: {current_timeout} 秒{Colors.DIM}（自定义）{Colors.RESET}")
        else:
            print(f"  心跳超时: {current_timeout} 秒{Colors.DIM}（默认）{Colors.RESET}")
        return 0

    # 如果seconds参数有内容，设置heartbeat_timeout值
    try:
        timeout_seconds = int(seconds_value)
        if timeout_seconds < 0:
            print(f"{Colors.RED}❌ 心跳超时值不能为负数{Colors.RESET}", file=sys.stderr)
            return 1

        success = tm.db.update_task(args.id, heartbeat_timeout=timeout_seconds)

        if success:
            if timeout_seconds > 0:
                print(
                    f"{Colors.GREEN}✅ 事务 {formatted_id} 心跳超时已设置为 {timeout_seconds} 秒{Colors.RESET}"
                )
            else:
                default_timeout = get_default_heartbeat_timeout()
                print(
                    f"{Colors.GREEN}✅ 事务 {formatted_id} 心跳超时已重置为默认值（{default_timeout} 秒）{Colors.RESET}"
                )
        else:
            print(f"{Colors.RED}❌ 无法更新事务{Colors.RESET}", file=sys.stderr)
            return 1
    except ValueError:
        print(f"{Colors.RED}❌ 心跳超时值必须是整数{Colors.RESET}", file=sys.stderr)
        return 1


def cmd_info(args):
    """
    事务信息操作（列表格式）

    info 字段存储为 JSON 列表格式：[{"content": "信息1", "read": false}, ...]
    - 无参数：显示展开的 info 列表（包含已读/未读状态）
    - 有参数：追加信息到列表末尾（默认未读）
    - -d/--delete：清空列表
    - -u/--unread：标记所有信息为未读
    - 向后兼容：字符串自动转为带状态格式
    """

    tm = get_task_manager()

    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    formatted_id = format_task_id(args.id, task)

    # 检查是否使用 -d --delete 参数清空字段
    delete_flag = getattr(args, "delete", False)
    if delete_flag:
        success = tm.db.clear_task_info(args.id)
        if success:
            print(f"{Colors.GREEN}✅ 事务 {formatted_id} 注入信息已清空{Colors.RESET}")
        else:
            print(f"{Colors.RED}❌ 无法更新事务{Colors.RESET}", file=sys.stderr)
            return 1
        return 0

    # 检查是否使用 -u --unread / -r --read 参数标记状态
    unread_flag = getattr(args, "unread", False)
    read_flag = getattr(args, "read", False)
    if unread_flag or read_flag:
        success = tm.db.set_task_info_status(args.id, mode="all", read=read_flag)
        if success:
            info_list = tm.db.get_task_info(args.id, mode="all") or []
            print(
                f"{Colors.GREEN}✅ 事务 {formatted_id} 所有信息已标记为{'已' if read_flag else '未'}读{Colors.RESET}"
            )
            print(f"{Colors.DIM}共 {len(info_list)} 条信息{Colors.RESET}")
        else:
            print(f"{Colors.RED}❌ 无法更新事务{Colors.RESET}", file=sys.stderr)
            return 1
        return 0

    # 获取字段值参数 - 优先从 --file 参数读取
    file_path = getattr(args, "file", None)
    if file_path:
        info_value = [_read_file_content(file_path)]
    else:
        info_value = getattr(args, "info", None)

    # 获取当前 info 列表（通过 database 函数）
    info_list_with_status = tm.db.get_task_info(args.id, mode="all") or []

    # 如果没有 info_value 参数，显示字段（包含已读/未读状态）
    if info_value is None or (isinstance(info_value, list) and len(info_value) == 0):
        print(f"{Colors.CYAN}事务 {formatted_id} 的注入信息{Colors.RESET}")
        print()

        if info_list_with_status:
            unread_count = 0
            for i, item in enumerate(info_list_with_status, 1):
                status = (
                    f"{Colors.YELLOW}📭未读{Colors.RESET}"
                    if not item["read"]
                    else f"{Colors.DIM}📭已读{Colors.RESET}"
                )
                # 事务#2089：显示发送者标识（旧数据无sender时不显示）
                sender_part = (
                    f"{Colors.BLUE}[{item['sender']}]{Colors.RESET} " if item.get("sender") else ""
                )
                if not item["read"]:
                    unread_count += 1
                print(f"  [{i}] {status} {sender_part}{item['content']}")
            print(
                f"\n{Colors.DIM}共 {len(info_list_with_status)} 条信息（{unread_count} 条未读）{Colors.RESET}"
            )
        else:
            print(f"{Colors.YELLOW}  (无注入信息){Colors.RESET}")
        return 0

    # 如果 info_value 参数是空字符串列表 [""]，清空字段
    if isinstance(info_value, list) and len(info_value) == 1 and info_value[0] == "":
        success = tm.db.clear_task_info(args.id)
        if success:
            print(f"{Colors.GREEN}✅ 事务 {formatted_id} 注入信息已清空{Colors.RESET}")
        else:
            print(f"{Colors.RED}❌ 无法更新事务{Colors.RESET}", file=sys.stderr)
            return 1
        return 0

    # 如果 info_value 参数有内容，追加到列表末尾（使用新格式，默认未读）
    if isinstance(info_value, list):
        new_info = " ".join(info_value)
    else:
        new_info = info_value

    # 将 \n 转换为真正的换行符
    new_info = get_text_from_args(new_info)

    # 发送者标识（事务#2089）：CLI调用者默认为风主；LLM工具调用时显式传入 事务#N
    sender = getattr(args, "sender", None) or get_master_name()

    # 通过 database 函数添加
    success = tm.db.add_task_info(args.id, new_info, sender=sender)

    if success:
        info_list_with_status = tm.db.get_task_info(args.id, mode="all") or []
        unread_count = sum(1 for item in info_list_with_status if not item["read"])
        print(f"{Colors.GREEN}✅ 事务 {formatted_id} 注入信息已添加（未读，来自 {sender}）{Colors.RESET}")
        print(f"  添加：{new_info}")
        print(
            f"{Colors.DIM}当前共 {len(info_list_with_status)} 条信息（{unread_count} 条未读）{Colors.RESET}"
        )
        # 只有当事务不在running状态时才插入resume控制信号
        # 如果事务已在running，info会在下一个迭代自动被检测到并注入
        # 避免running状态下强制重启导致多进程竞态调用LLM API（引发HTTP 400错误）
        current_status = task.get("status", "")
        if current_status != "running":
            tm.db.insert_task_control(args.id, "resume")
        else:
            print(f"{Colors.DIM}  事务正在运行中，info将在下一次迭代自动注入{Colors.RESET}")
    else:
        print(f"{Colors.RED}❌ 无法更新事务{Colors.RESET}", file=sys.stderr)
        return 1
    return 0


def cmd_prompt(args):
    """事务提示词操作"""
    return _cmd_field_operation(args, "prompt", "提示词")


def cmd_provider(args):
    """事务提示词操作"""
    return _cmd_field_operation(args, "provider", "提示词")


def cmd_description(args):
    """事务描述操作"""
    return _cmd_field_operation(args, "description", "描述")


def cmd_content(args):
    """事务内容操作"""
    return _cmd_field_operation(args, "content", "内容")


def cmd_checklist(args):
    """事务检查清单操作"""
    return _cmd_field_operation(args, "checklist", "检查清单")


def cmd_report(args):
    """事务汇报操作"""
    return _cmd_field_operation(args, "report", "工作汇报")


def cmd_title(args):
    """事务标题操作"""
    return _cmd_field_operation(args, "title", "标题")


def cmd_workdir(args):
    """事务工作目录操作"""
    return _cmd_field_operation(args, "workdir", "工作目录")


def cmd_nest(args):
    """事务鹏🐣窝操作"""
    return _cmd_field_operation(args, "nest", "鹏🐣窝")


def cmd_branch(args):
    """事务分支名操作"""
    # 将 branch_name 参数映射为 branch，供 _cmd_field_operation 使用
    if hasattr(args, "branch_name"):
        setattr(args, "branch", args.branch_name)
    return _cmd_field_operation(args, "branch_name", "分支名")


def cmd_result(args):
    """事务结果操作"""
    return _cmd_field_operation(args, "result", "结果")


def cmd_metadata(args):
    """事务元数据操作"""
    return _cmd_field_operation(args, "metadata", "元数据")


def _cmd_field_operation(args, field_name: str, field_label: str):
    """
    通用的字段操作函数

    Args:
        args: 命令行参数
        field_name: 字段名称（info/prompt/description/content/workdir/nest/branch_name/result/metadata/title/checklist/report）
        field_label: 字段标签（注入信息/提示词/描述/内容/工作目录/鹏🐣窝/分支名/结果/元数据/标题/检查清单/工作汇报）
    """
    tm = get_task_manager()

    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    # 获取字段值参数 - 优先从 --file 参数读取
    file_path = getattr(args, "file", None)
    if file_path:
        file_content = _read_file_content(file_path)
        field_value = [file_content] if file_content else []
    else:
        field_value = getattr(args, field_name, None)

    # 检查是否使用 -d --delete 参数清空字段
    delete_flag = getattr(args, "delete", False)

    # 判断操作类型
    # 如果没有 field_value 参数，显示字段
    if not delete_flag and (
        field_value is None or (isinstance(field_value, list) and len(field_value) == 0)
    ):
        # 显示字段
        value = task.get(field_name, "[]")
        formatted_id = format_task_id(args.id, task)
        print(f"{Colors.CYAN}事务 {formatted_id} 的{field_label}{Colors.RESET}")
        print()

        if value and value != "[]" and value != "null" and value != "":
            print(f"  {field_label}：{value}")
        else:
            print(f"{Colors.YELLOW}  (未设置){Colors.RESET}")
        return 0

    # 如果 field_value 参数是空字符串列表 [""]，清空字段
    if isinstance(field_value, list) and len(field_value) == 1 and field_value[0] == "":
        delete_flag = True

    if delete_flag:
        # 对于 info/prompt/metadata（JSON 格式），清空为 []
        # 对于其他字段（字符串格式），清空为 ""
        if field_name in ["info", "prompt", "metadata"]:
            empty_value = "[]"
        else:
            empty_value = ""

        success = tm.db.update_task(args.id, **{field_name: empty_value})
        if success:
            formatted_id = format_task_id(args.id, task)
            print(f"{Colors.GREEN}✅ 事务 {formatted_id} {field_label}已清空{Colors.RESET}")
        else:
            print(f"{Colors.RED}❌ 无法更新事务{Colors.RESET}", file=sys.stderr)
            return 1
        return 0

    # 如果 field_value 参数有内容，设置字段（合并多个参数为单个字符串）
    if isinstance(field_value, list):
        value_string = " ".join(field_value)
    else:
        value_string = field_value

    # 将 \n 转换为真正的换行符
    value_string = get_text_from_args(value_string)
    success = tm.db.update_task(args.id, **{field_name: value_string})

    if success:
        formatted_id = format_task_id(args.id, task)
        print(f"{Colors.GREEN}✅ 事务 {formatted_id} {field_label}已更新{Colors.RESET}")
        print(f"  {field_label}：{value_string}")

        # 如果是content字段且为根事务（parent_task_id IS NULL），同步更新关联的根消息content
        if field_name == "content" and not task.get("parent_task_id"):
            message_id = task.get("message_id")
            if message_id:
                from peng.message.database import MessageDatabase

                msg_db = MessageDatabase()
                # 获取消息信息检查是否为根消息
                msg = msg_db.get_message(message_id)
                if msg and msg.get("reply_to") is None:
                    # 同步更新根消息的content
                    msg_success = msg_db.update_message(message_id, content=value_string)
                    if msg_success:
                        print(
                            f"{Colors.GREEN}  → 已同步更新根消息 #{message_id} 的内容{Colors.RESET}"
                        )
                    else:
                        print(
                            f"{Colors.YELLOW}  ⚠️ 同步更新根消息 #{message_id} 内容失败{Colors.RESET}",
                            file=sys.stderr,
                        )
    else:
        print(f"{Colors.RED}❌ 无法更新事务{Colors.RESET}", file=sys.stderr)
        return 1

    return 0


def cmd_output(args):
    """显示事务输出文件内容"""
    from peng.config.loader import load_config
    from peng.message.manager import MessageManager

    # 获取事务管理器
    tm = get_task_manager()

    # 获取事务信息
    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1

    task_id = task["id"]
    message_id = task.get("message_id")
    if not message_id:
        print(f"{Colors.YELLOW}⚠️ 事务 #{task_id} 没有关联消息{Colors.RESET}")
        return 1

    # 构建输出文件名：task-<task_id>.out
    filename = f"task-{task_id}.out"

    try:
        config = load_config()
        output_dir = (
            Path(config.paths.output_dir)
            if hasattr(config, "paths")
            else PROJECT_ROOT / "data" / "output"
        )
    except Exception as e:
        output_dir = PROJECT_ROOT / "data" / "output"

    if not output_dir.exists():
        print(f"{Colors.YELLOW}⚠️ 输出目录不存在: {output_dir}{Colors.RESET}")
        return 1

    # 查找输出文件
    filepath = output_dir / filename
    if not filepath.exists():
        print(f"{Colors.RED}❌ 找不到输出文件: {filename}{Colors.RESET}")
        return 1

    # 如果使用 -d --delete 参数，删除输出文件
    if args.delete:
        try:
            filepath.unlink()
            formatted_id = format_task_id(task_id, task)
            print(f"{Colors.GREEN}✅ 事务 {formatted_id} 的输出文件已删除{Colors.RESET}")
            print(f"  文件: {filepath}")
            return 0
        except Exception as e:
            print(f"{Colors.RED}❌ 删除文件失败: {e}{Colors.RESET}")
            return 1

    # 读取文件
    try:
        lines = filepath.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception as e:
        print(f"{Colors.RED}❌ 读取文件失败: {e}{Colors.RESET}")
        return 1

    total_lines = len(lines)

    # 如果只显示行数
    if args.line_count:
        print(f"{Colors.BOLD}文件: {filepath}{Colors.RESET}")
        print(f"{Colors.BOLD}事务: {task_id}, 消息: {message_id}{Colors.RESET}")
        print(f"{Colors.BOLD}总行数: {total_lines}{Colors.RESET}")
        return 0

    # 处理行号范围
    begin_line = args.begin - 1 if args.begin else 0  # 转换为 0-based
    end_line = args.end if args.end else total_lines

    # 处理 head 和 tail
    if args.head:
        begin_line = 0
        end_line = min(args.head, total_lines)
    elif args.tail:
        begin_line = max(0, total_lines - args.tail)
        end_line = total_lines

    # 边界检查
    begin_line = max(0, begin_line)
    end_line = min(total_lines, end_line)

    if begin_line >= end_line:
        print(f"{Colors.YELLOW}⚠️ 无有效内容显示{Colors.RESET}")
        return 0

    # 显示文件信息
    print(f"{Colors.BOLD}文件: {filepath}{Colors.RESET}")
    print(f"{Colors.BOLD}事务: {task_id}{Colors.RESET}")
    print(f"{Colors.BOLD}总行数: {total_lines}{Colors.RESET}")
    if not args.line_count:
        print(f"{Colors.BOLD}显示范围: {begin_line + 1}-{end_line}{Colors.RESET}")

    # 检测显示范围内是否含回滚链接，若有则提示回滚命令用法
    _has_rollback = any(_ROLLBACK_ID_URI_RE.search(lines[i]) for i in range(begin_line, end_line))
    if _has_rollback:
        print(
            f"{Colors.YELLOW}💡 回滚命令: task rollback {task_id} R<ID>"
            f"（<ID> 即行尾 [R<ID>] 标记中的数字，如 R389；行尾同时给出完整回滚命令）{Colors.RESET}"
        )

    # 只显示行数的情况
    if args.line_count:
        return 0

    # 显示表头（根据 verbose 参数决定）
    if args.verbose:
        print("=" * 100)
        print(f"{'行号':<12}  {'时间戳':<20}  {'输出源':<10} {'行号':<8}  内容")
        print("-" * 100)

    # 显示内容（带行号）；含回滚超链接的行在行尾追加 [R<seq>] task rollback <task_id> R<seq>，
    # 同时给出回滚ID与完整回滚命令行，可直接复制执行
    for i in range(begin_line, end_line):
        line_num = i + 1
        line_content = lines[i]

        # 解析行内容（TSV格式）
        parts = line_content.split("\t")
        if len(parts) >= 4:
            timestamp = parts[0]
            output_type = parts[1]
            task_id_display = parts[2]
            content = "\t".join(parts[3:])

            if args.verbose:
                # verbose 模式：显示完整信息（含回滚ID标记）
                print(
                    f"{line_num:<8}  {timestamp:<29}  {output_type:<12}  {task_id_display:<8}  {_annotate_rollback_ids(content)}"
                )
            else:
                # 默认模式：只显示行号和内容（含回滚ID+命令行标记）
                print(f"{line_num:<8}  {_annotate_rollback_ids(content)}")
        else:
            # 无法解析的行，直接显示（仍尝试追加回滚ID+命令行标记）
            print(f"{line_num:<8}  {_annotate_rollback_ids(line_content)}")

    return 0


def _resolve_rollback_id_ref(text: str, context_task_id: int) -> str | None:
    """将用户输入的宽松回滚ID解析为标准 "<task_id>-<seq>" 格式

    支持：
    - "1553-12" / "R1553-12" → 1553-12（完整ID）
    - "12" / "R12"           → <context_task_id>-12（task上下文内seq）
    跨task的完整ID自动纠正到目标task（但命令的task_id参数必须匹配）。
    """
    from peng.service.tool_backup import parse_rollback_id

    t = (text or "").strip()
    parsed = parse_rollback_id(t)
    if parsed is not None:
        task_id, seq = parsed
        return f"{task_id}-{seq}"
    if t and t[0] in "Rr":
        t = t[1:]
    if t.isdigit():
        return f"{context_task_id}-{int(t)}"
    return None


def _format_rollback_meta_summary(meta: dict) -> str:
    """格式化回滚meta摘要（供确认/列表显示）"""
    from peng.service.tool_backup import ToolBackupManager

    tool_name = meta.get("tool_name", "unknown")
    timestamp = (meta.get("timestamp") or "").replace("T", " ")
    backup_type = meta.get("type", "?")
    iteration = meta.get("iteration")
    task_id = meta.get("task_id", "?")
    rollback_id = meta.get("rollback_id") or meta.get("_seq", "?")
    seq = str(rollback_id).split("-", 1)[1] if "-" in str(rollback_id) else str(rollback_id)

    label = f"迭代#{iteration}" if iteration else tool_name
    lines = [
        f"  回滚ID:   {Colors.MAGENTA}R{seq}{Colors.RESET}  （CLI: task rollback {task_id} R{seq}）",
        f"  快照:     {label}  [{timestamp}]",
        f"  备份类型: {backup_type}",
        f"  工作目录: {meta.get('workdir', '?')} (nest: {meta.get('nest_name', 'local')})",
    ]
    changed = meta.get("changed_files", [])
    added = meta.get("added_files", [])
    if changed:
        for f in changed[:10]:
            lines.append(f"  {Colors.RED}- {f}{Colors.RESET}")
        if len(changed) > 10:
            lines.append(f"  {Colors.RED}... 共 {len(changed)} 个修改文件{Colors.RESET}")
    if added:
        for f in added[:10]:
            lines.append(f"  {Colors.RED}+ 删除 {f}{Colors.RESET}")
        if len(added) > 10:
            lines.append(
                f"  {Colors.RED}... 共 {len(added)} 个新增文件（回滚时删除）{Colors.RESET}"
            )
    if not changed and not added:
        lines.append(f"  {Colors.YELLOW}⚠️ 快照时无变更清单（回滚时动态检测实际变更）{Colors.RESET}")
    return "\n".join(lines)


def cmd_rollback(args):
    """按回滚ID回滚事务文件/上下文到指定快照

    回滚ID来源：task output / fly watch 显示中的 [R<seq>] 标记（或 xterm 中
    回滚超链接）。完整ID为 <task_id>-<seq>。

    流程：定位meta → 显示摘要 → 确认 → 暂停运行中事务 → 执行回滚 →
    写回滚记录到 output（含撤销链接和继续链接）。
    """
    from peng.service.tool_backup import (
        ToolBackupManager,
        list_rollback_metas,
        load_rollback_meta_persistent,
    )
    from peng.task.output_manager import get_output_manager

    tm = get_task_manager()
    task = tm.get_task(args.id)
    if not task:
        _print_task_not_found_debug_info(args.id)
        return 1
    formatted_id = format_task_id(args.id, task)

    # --list：列出全部可用回滚ID
    if args.list:
        metas = list_rollback_metas(args.id)
        print(f"\n{Colors.BOLD}事务 {formatted_id} 可用回滚快照{Colors.RESET}")
        print("=" * 70)
        if not metas:
            print(f"{Colors.YELLOW}⚠️ 无持久化回滚快照（旧数据或未生成备份）{Colors.RESET}")
            return 0
        for meta in metas:
            rid = meta.get("rollback_id") or meta.get("_seq", "?")
            seq = str(rid).split("-", 1)[1] if "-" in str(rid) else str(rid)
            tool_name = meta.get("tool_name", "unknown")
            timestamp = (meta.get("timestamp") or "").replace("T", " ")
            iteration = meta.get("iteration")
            label = f"迭代#{iteration}" if iteration else tool_name
            scope = []
            if meta.get("type") in ("git", "non_git"):
                scope.append("文件")
            if meta.get("history_commit"):
                scope.append("history")
            scope_str = "+".join(scope) or "无"
            print(
                f"  {Colors.MAGENTA}[R{seq}]{Colors.RESET} "
                f"R{seq:<8} {label} [{timestamp}] ({scope_str})"
            )
        print(
            f"\n回滚命令: task rollback {args.id} R<ID>（如 task rollback {args.id} R{metas[-1].get('_seq', 1)}）"
        )
        return 0

    # 解析回滚ID
    if not args.rollback_id:
        print(
            f"{Colors.RED}❌ 缺少回滚ID。先运行 task rollback {args.id} --list 查看{Colors.RESET}",
            file=sys.stderr,
        )
        return 1
    rid = _resolve_rollback_id_ref(args.rollback_id, args.id)
    if not rid:
        print(
            f"{Colors.RED}❌ 无效回滚ID: {args.rollback_id}（支持 12 / R12 / {args.id}-12 格式）{Colors.RESET}",
            file=sys.stderr,
        )
        return 1

    # task_id 一致性校验
    rid_task = int(rid.split("-", 1)[0])
    if rid_task != args.id:
        print(
            f"{Colors.RED}❌ 回滚ID {rid} 属于事务#{rid_task}，与目标事务 {formatted_id} 不匹配{Colors.RESET}",
            file=sys.stderr,
        )
        return 1

    meta = load_rollback_meta_persistent(rid)
    if meta is None:
        print(
            f"{Colors.RED}❌ 回滚ID {rid} 不存在。运行 task rollback {args.id} --list 查看可用ID{Colors.RESET}",
            file=sys.stderr,
        )
        return 1
    _raw_seq = str(meta.get("rollback_id") or rid.split("-", 1)[1])
    seq = _raw_seq.split("-", 1)[1] if "-" in _raw_seq else _raw_seq

    # 显示摘要并确认
    print(f"\n{Colors.BOLD}即将回滚事务 {formatted_id} 至快照：{Colors.RESET}")
    print(_format_rollback_meta_summary(meta))
    print(f"\n{Colors.YELLOW}⚠️ 回滚将覆盖当前文件内容，同期非本事务的变更将丢失。{Colors.RESET}")
    if not args.yes:
        try:
            answer = input(f"确认回滚 R{seq}？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer not in ("y", "yes"):
            print("已取消")
            return 0

    # 暂停运行中的事务（与 WebUI /api/tool-rollback 行为一致）
    was_running = False
    if task.get("status") == "running":
        try:
            import asyncio

            asyncio.run(tm.pause_task(args.id, reason=f"CLI rollback {rid}"))
            was_running = True
            print(
                f"{Colors.YELLOW}⏸️ 事务 {formatted_id} 已暂停（回滚后可用 resume 继续）{Colors.RESET}"
            )
        except Exception as e:
            print(f"{Colors.YELLOW}⚠️ 暂停事务失败（继续回滚）: {e}{Colors.RESET}")

    # 执行回滚（同步方法，与 WebUI 的 asyncio.to_thread 等价）
    # 返回4元组：成功标志/消息/undo_meta/是否有实际变化
    print(f"{Colors.CYAN}⏳ 正在执行回滚 {rid} ...{Colors.RESET}")
    success, message, undo_meta, changed = ToolBackupManager.rollback(meta)

    if not success:
        print(f"{Colors.RED}❌ 回滚失败: {message}{Colors.RESET}", file=sys.stderr)
        return 1

    # 🔧 无实际变化：不写回滚记录到output（无回滚链接/撤销链接）；
    # 回滚前暂停了事务则自动恢复运行（与 WebUI 行为一致）
    if not changed:
        print(f"{Colors.YELLOW}ℹ️ {message}{Colors.RESET}")
        if was_running:
            try:
                import asyncio

                resume_ok = asyncio.run(
                    tm.resume_task(args.id, reason="回滚无实际变化，自动恢复运行")
                )
                if resume_ok:
                    print(f"{Colors.GREEN}▶️ 事务 {formatted_id} 已自动恢复运行{Colors.RESET}")
            except Exception as e:
                print(
                    f"{Colors.YELLOW}⚠️ 恢复事务失败（可用 task resume {args.id} 手动恢复）: {e}{Colors.RESET}"
                )
        return 0

    # 写回滚记录到 output（含撤销链接和继续链接），与 WebUI 行为一致
    try:
        record = ToolBackupManager.generate_rollback_record(
            meta, undo_meta, message, was_running=was_running
        )
        get_output_manager().write_output(args.id, "rollback", f"CLI rollback {rid}\n{record}\n")
    except Exception as e:
        print(f"{Colors.YELLOW}⚠️ 写入回滚记录失败: {e}{Colors.RESET}")

    print(f"{Colors.GREEN}✅ 回滚成功: {message}{Colors.RESET}")
    undo_rid = (undo_meta or {}).get("rollback_id")
    if undo_rid:
        undo_seq = str(undo_rid).split("-", 1)[1] if "-" in str(undo_rid) else str(undo_rid)
        print(f"  撤销: task rollback {args.id} R{undo_seq}")
    if was_running:
        print(f"  继续处理: task resume {args.id}")
    return 0


def main():
    """主入口"""
    # 远程模式检测：优先本地服务，无本地服务时连接默认 nest 的 host
    # 这确保无论从 bin/task 还是 .venv/bin/task（console_scripts）都能正确路由
    from peng.cli.remote import should_use_remote

    _remote_result = should_use_remote("task", sys.argv[1:])
    if _remote_result is not None:
        return _remote_result

    # Windows下修复GBK编码无法输出emoji等Unicode字符的问题
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # 检查是否为无参数或只有 --all/-a 的情况
    has_all = "--all" in sys.argv or "-a" in sys.argv
    no_subcommand = True

    # 子命令列表
    subcommands = [
        "trees",
        "children",
        "new",
        "list",
        "show",
        "tree",
        "full-tree",
        "msg",
        "pause",
        "cancel",
        "up",
        "down",
        "retry",
        "recover",
        "restart",
        "resume",
        "start",
        "context",
        "chain",
        "status",
        "delete",
        "kill",
        "killall",
        "daemon",
        "move",
        "small-model",
        "big-model",
        "timer",
        "heartbeat",
        "prompt",
        "description",
        "content",
        "title",
        "workdir",
        "branch",
        "result",
        "metadata",
        "info",
        "output",
        "rollback",
        "checklist",
        "report",
    ]

    # 检查是否有子命令
    for arg in sys.argv[1:]:
        if not arg.startswith("-") and arg in subcommands:
            no_subcommand = False
            break

    # 如果无参数或只有 --all/-a，显示事务列表
    if len(sys.argv) == 1 or (no_subcommand and has_all):
        # 无参数时，显示未完成事务列表（和 bin/task list 一样）
        # 支持 --all/-a 参数
        show_all = has_all

        class ListArgs:
            command = "list"
            all = show_all
            status = None
            limit = None
            # 从配置加载默认 order
            _default_order = "ASC"
            try:
                from peng.config.loader import load_config

                _cfg = load_config()
                _default_order = _cfg.display.order or "ASC"
            except Exception as e:
                pass
            order = _default_order

        rc = cmd_trees(ListArgs())
        # 无参数（len(sys.argv)==1，区别于显式 --all/-a 扩展查询）时，
        # 在输出末尾打印 provider 用量/费用页脚（见 peng/cli/_usage_footer.py）
        if len(sys.argv) == 1:
            from peng.cli._usage_footer import print_provider_usage_footer

            print_provider_usage_footer()
        return rc

    parser = argparse.ArgumentParser(
        description=f"{AGENT_NAME}的事务管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    task                     以树状结构显示所有事务
    task list                列出事务
    task list --status pending  列出待执行事务
    task children 123        显示事务 123 的子事务
    task new 123 -c "内容"  创建子事务
    task show 123            显示事务详情
    task tree 123            显示事务树
    task pause 123           暂停事务（包括子事务）
    task cancel 123          取消事务（包括子事务）
    task up 123              提高优先级（包括子事务）
    task down 123            降低优先级（包括子事务）
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="命令")

    # trees 命令（显示事务树）
    trees_parser = subparsers.add_parser("trees", help="显示事务树（默认显示未完成）")
    trees_parser.add_argument(
        "--all", "-a", action="store_true", help="显示所有事务树（包括已完成的）"
    )
    trees_parser.add_argument("--limit", "-n", type=int, default=None, help="显示数量（默认不限）")
    trees_parser.set_defaults(func=cmd_trees)

    # children 命令
    children_parser = subparsers.add_parser("children", help="显示子事务列表")
    children_parser.add_argument("id", type=int, help="父事务 ID")
    children_parser.set_defaults(func=cmd_children)

    # subtask 命令
    subtask_parser = subparsers.add_parser("subtask", help="子事务操作（无参数=显示，有内容=创建）")
    subtask_parser.add_argument("id", type=int, help="父事务 ID")
    subtask_parser.add_argument("content", nargs="*", help="子事务内容（多个参数会自动拼接）")
    subtask_parser.add_argument("--prompt", "-p", help="子事务提示词")
    subtask_parser.add_argument("--workdir", "-w", help="子事务工作目录")
    subtask_parser.add_argument("--small-model", action="store_true", help="使用小模型")
    subtask_parser.add_argument("--creator", "-c", help="事务创建者")
    subtask_parser.add_argument("--file", type=str, help="从文件读取子事务内容")
    subtask_parser.set_defaults(func=cmd_subtask)

    # list 命令
    list_parser = subparsers.add_parser("list", help="列出事务（默认显示未完成）")
    list_parser.add_argument(
        "--all",
        "-a",
        action="store_true",
        help="显示所有事务（包括已完成的）",
    )
    list_parser.add_argument(
        "--status",
        "-s",
        help="状态过滤 (pending/running/completed/failed/cancelled/paused/interrupted)",
    )
    list_parser.add_argument("--limit", "-n", type=int, default=None, help="显示数量（默认不限）")
    # 从配置加载默认 order
    _default_order = "ASC"
    try:
        from peng.config.loader import load_config

        _cfg = load_config()
        _default_order = _cfg.display.order or "ASC"
    except Exception as e:
        pass
    list_parser.add_argument(
        "--order",
        type=str,
        default=_default_order,
        choices=["ASC", "DESC"],
        help="排序顺序：ASC=从老到新（默认），DESC=从新到老",
    )
    list_parser.set_defaults(func=cmd_trees)

    # show 命令
    show_parser = subparsers.add_parser("show", help="显示事务详情")
    show_parser.add_argument("id", type=int, help="事务ID")
    show_parser.add_argument(
        "--format",
        choices=["ansi", "html"],
        default="ansi",
        help="输出格式（ansi=终端默认，html=语义化HTML用于弹窗）",
    )
    show_parser.set_defaults(func=cmd_show)

    # tree 命令
    tree_parser = subparsers.add_parser("tree", help="显示事务分支树（从该事务开始）")
    tree_parser.add_argument("id", type=int, help="事务ID")
    tree_parser.set_defaults(func=cmd_tree)

    # full-tree 命令
    full_tree_parser = subparsers.add_parser("full-tree", help="显示事务完整树（从根事务开始）")
    full_tree_parser.add_argument("id", type=int, help="事务ID")
    full_tree_parser.set_defaults(func=cmd_full_tree)

    # msg 命令
    msg_parser = subparsers.add_parser("msg", help="显示根事务的消息回复链")
    msg_parser.add_argument("id", type=int, help="事务ID")
    msg_parser.set_defaults(func=cmd_msg)

    # pause 命令
    pause_parser = subparsers.add_parser("pause", help="暂停事务（递归暂停子事务）")
    pause_parser.add_argument("id", type=int, help="事务ID")
    pause_parser.set_defaults(func=cmd_pause)

    # cancel 命令
    cancel_parser = subparsers.add_parser("cancel", help="取消事务（递归取消子事务）")
    cancel_parser.add_argument("id", type=int, help="事务ID")
    cancel_parser.set_defaults(func=cmd_cancel)

    # up 命令
    up_parser = subparsers.add_parser("up", help="提高优先级（递归提高子事务）")
    up_parser.add_argument("id", type=int, help="事务ID")
    up_parser.set_defaults(func=cmd_up)

    # down 命令
    down_parser = subparsers.add_parser("down", help="降低优先级（递归降低子事务）")
    down_parser.add_argument("id", type=int, help="事务ID")
    down_parser.set_defaults(func=cmd_down)

    # restart 命令
    restart_parser = subparsers.add_parser("restart", help="重启事务（从步骤1开始）")
    restart_parser.add_argument("id", type=int, help="事务ID")
    restart_parser.add_argument("-f", "--force", action="store_true", help="强制重新启动")
    restart_parser.set_defaults(func=cmd_restart)

    # resume 命令
    resume_parser = subparsers.add_parser("resume", help="恢复事务（从当前步骤继续）")
    resume_parser.add_argument("id", type=int, help="事务ID")
    resume_parser.set_defaults(func=cmd_resume)

    # start 命令
    start_parser = subparsers.add_parser("start", help="启动待定事务")
    start_parser.add_argument("id", type=int, help="事务ID")
    start_parser.set_defaults(func=cmd_start)

    # context 命令
    context_parser = subparsers.add_parser("context", help="事务上下文操作")
    context_parser.add_argument("id", type=int, help="事务ID")
    context_parser.add_argument("context", nargs="*", help="上下文内容（无参数=显示，有内容=设置）")
    context_parser.add_argument("-d", "--delete", action="store_true", help="清空上下文")
    context_parser.add_argument("--file", type=str, help="从文件读取上下文内容")
    context_parser.set_defaults(func=cmd_context)

    # chain 命令
    chain_parser = subparsers.add_parser("chain", help="显示调用链/进程信息")
    chain_parser.add_argument("id", type=int, help="事务ID")
    chain_parser.set_defaults(func=cmd_chain)

    # status 命令
    status_parser = subparsers.add_parser("status", help="显示或设置事务状态")
    status_parser.add_argument("id", type=int, help="事务ID")
    status_parser.add_argument(
        "status_value",
        type=str,
        nargs="?",
        choices=["pending", "running", "completed", "failed"],
        help="设置事务状态（可选，只支持：pending/running/completed/failed）",
    )
    status_parser.set_defaults(func=cmd_status)

    # delete 命令
    delete_parser = subparsers.add_parser("delete", help="递归删除事务及其所有子事务")
    delete_parser.add_argument("id", type=int, help="事务ID")
    delete_parser.add_argument("-f", "--force", action="store_true", help="强制删除")
    delete_parser.set_defaults(func=cmd_delete)

    # kill 命令
    kill_parser = subparsers.add_parser(
        "kill", help="杀死后台daemon进程（按事务ID或 -d <daemon_id>，从data/daemon.json读取）"
    )
    kill_parser.add_argument("id", type=int, nargs="?", default=None, help="事务ID（与 -d 二选一）")
    kill_parser.add_argument(
        "--daemon-id",
        "-d",
        action="append",
        default=None,
        help="daemon id（daemon工具返回的id，可多次指定杀死多个）",
    )
    kill_parser.set_defaults(func=cmd_kill)

    # killall 命令
    killall_parser = subparsers.add_parser("killall", help="杀死daemon.json中记录的所有后台daemon进程")
    killall_parser.set_defaults(func=cmd_killall)

    # daemon 命令
    daemon_parser = subparsers.add_parser("daemon", help="显示所有已注册的后台daemon")
    daemon_parser.add_argument(
        "--nest", type=str, default=None, help="按nest过滤（local=本地，其他=远程nest名称）"
    )
    daemon_parser.add_argument(
        "--local", action="store_true", help="只显示本地进程（等同于--nest local）"
    )
    daemon_parser.set_defaults(func=cmd_daemon)

    # move 命令
    move_parser = subparsers.add_parser("move", help="移动事务到新的父事务下或变成树根")
    move_parser.add_argument("id", type=int, help="要移动的事务ID")
    move_parser.add_argument(
        "target", type=int, nargs="?", default=None, help="目标父事务ID（不指定则变成树根）"
    )
    move_parser.set_defaults(func=cmd_move)

    # small-model 命令
    small_model_parser = subparsers.add_parser("small-model", help="设置事务使用小模型")
    small_model_parser.add_argument("id", type=int, help="事务ID")
    small_model_parser.set_defaults(func=cmd_small_model)

    # big-model 命令
    big_model_parser = subparsers.add_parser("big-model", help="设置事务使用大模型")
    big_model_parser.add_argument("id", type=int, help="事务ID")
    big_model_parser.set_defaults(func=cmd_big_model)

    # timer 命令
    timer_parser = subparsers.add_parser("timer", help="事务定时器操作（定时重启）")
    timer_parser.add_argument("id", type=int, help="事务ID")
    timer_parser.add_argument(
        "seconds", nargs="?", type=int, help="定时重启间隔（秒），0表示清除定时器"
    )
    timer_parser.add_argument("-d", "--delete", action="store_true", help="清除定时器")
    timer_parser.set_defaults(func=cmd_timer)

    # heartbeat 命令
    heartbeat_parser = subparsers.add_parser(
        "heartbeat", help="事务心跳超时操作（查看和修改心跳超时时间）"
    )
    heartbeat_parser.add_argument("id", type=int, help="事务ID")
    heartbeat_parser.add_argument(
        "seconds", nargs="?", type=int, help="心跳超时时间（秒），0表示使用默认值"
    )
    heartbeat_parser.add_argument("-d", "--delete", action="store_true", help="重置为默认值")
    heartbeat_parser.set_defaults(func=cmd_heartbeat)

    # prompt 命令
    prompt_parser = subparsers.add_parser("prompt", help="事务提示词操作")
    prompt_parser.add_argument("id", type=int, help="事务ID")
    prompt_parser.add_argument("prompt", nargs="*", help="提示词内容（无参数=显示，有内容=设置）")
    prompt_parser.add_argument("-d", "--delete", action="store_true", help="清空提示词")
    prompt_parser.add_argument("--file", type=str, help="从文件读取提示词内容")
    prompt_parser.set_defaults(func=cmd_prompt)

    # provider 命令
    provider_parser = subparsers.add_parser("provider", help="事务提示词操作")
    provider_parser.add_argument("id", type=int, help="事务ID")
    provider_parser.add_argument(
        "provider", nargs="*", help="提示词内容（无参数=显示，有内容=设置）"
    )
    provider_parser.add_argument("-d", "--delete", action="store_true", help="清空提示词")
    provider_parser.add_argument("--file", type=str, help="从文件读取内容")
    provider_parser.set_defaults(func=cmd_provider)

    # description 命令
    description_parser = subparsers.add_parser("description", help="事务描述操作")
    description_parser.add_argument("id", type=int, help="事务ID")
    description_parser.add_argument(
        "description", nargs="*", help="描述内容（无参数=显示，有内容=设置）"
    )
    description_parser.add_argument("-d", "--delete", action="store_true", help="清空描述")
    description_parser.add_argument("--file", type=str, help="从文件读取描述内容")
    description_parser.set_defaults(func=cmd_description)

    # content 命令
    content_parser = subparsers.add_parser("content", help="事务内容操作")
    content_parser.add_argument("id", type=int, help="事务ID")
    content_parser.add_argument("content", nargs="*", help="内容（无参数=显示，有内容=设置）")
    content_parser.add_argument("-d", "--delete", action="store_true", help="清空内容")
    content_parser.add_argument("--file", type=str, help="从文件读取内容")
    content_parser.set_defaults(func=cmd_content)

    # checklist 命令
    checklist_parser = subparsers.add_parser("checklist", help="事务检查清单操作")
    checklist_parser.add_argument("id", type=int, help="事务ID")
    checklist_parser.add_argument(
        "checklist", nargs="*", help="检查清单（无参数=显示，有内容=设置）"
    )
    checklist_parser.add_argument("-d", "--delete", action="store_true", help="清空检查清单")
    checklist_parser.add_argument("--file", type=str, help="从文件读取检查清单")
    checklist_parser.set_defaults(func=cmd_checklist)

    # report 命令
    report_parser = subparsers.add_parser("report", help="事务汇报操作")
    report_parser.add_argument("id", type=int, help="事务ID")
    report_parser.add_argument("report", nargs="*", help="汇报内容（无参数=显示，有内容=设置）")
    report_parser.add_argument("-d", "--delete", action="store_true", help="清空汇报")
    report_parser.add_argument("--file", type=str, help="从文件读取汇报内容")
    report_parser.set_defaults(func=cmd_report)

    # workdir 命令
    workdir_parser = subparsers.add_parser("workdir", help="事务工作目录操作")
    workdir_parser.add_argument("id", type=int, help="事务ID")
    workdir_parser.add_argument("workdir", nargs="*", help="工作目录（无参数=显示，有内容=设置）")
    workdir_parser.add_argument("-d", "--delete", action="store_true", help="清空工作目录")
    workdir_parser.add_argument("--file", type=str, help="从文件读取内容")
    workdir_parser.set_defaults(func=cmd_workdir)

    # nest 命令
    nest_parser = subparsers.add_parser("nest", help="事务鹏🐣窝操作")
    nest_parser.add_argument("id", type=int, help="事务ID")
    nest_parser.add_argument("nest", nargs="*", help="鹏🐣窝（无参数=显示，有内容=设置）")
    nest_parser.add_argument("-d", "--delete", action="store_true", help="清空鹏🐣窝")
    nest_parser.add_argument("--file", type=str, help="从文件读取内容")
    nest_parser.set_defaults(func=cmd_nest)

    # branch 命令
    branch_parser = subparsers.add_parser("branch", help="事务分支名操作")
    branch_parser.add_argument("id", type=int, help="事务ID")
    branch_parser.add_argument("branch_name", nargs="*", help="分支名（无参数=显示，有内容=设置）")
    branch_parser.add_argument("-d", "--delete", action="store_true", help="清空分支名")
    branch_parser.add_argument("--file", type=str, help="从文件读取内容")
    branch_parser.set_defaults(func=cmd_branch)

    # result 命令
    result_parser = subparsers.add_parser("result", help="事务结果操作")
    result_parser.add_argument("id", type=int, help="事务ID")
    result_parser.add_argument("result", nargs="*", help="结果（无参数=显示，有内容=设置）")
    result_parser.add_argument("-d", "--delete", action="store_true", help="清空结果")
    result_parser.add_argument("--file", type=str, help="从文件读取内容")
    result_parser.set_defaults(func=cmd_result)

    # metadata 命令
    metadata_parser = subparsers.add_parser("metadata", help="事务元数据操作")
    metadata_parser.add_argument("id", type=int, help="事务ID")
    metadata_parser.add_argument("metadata", nargs="*", help="元数据（无参数=显示，有内容=设置）")
    metadata_parser.add_argument("-d", "--delete", action="store_true", help="清空元数据")
    metadata_parser.add_argument("--file", type=str, help="从文件读取内容")
    metadata_parser.set_defaults(func=cmd_metadata)

    # title 命令
    title_parser = subparsers.add_parser("title", help="事务标题操作")
    title_parser.add_argument("id", type=int, help="事务ID")
    title_parser.add_argument("title", nargs="*", help="标题内容（无参数=显示，有内容=设置）")
    title_parser.add_argument("-d", "--delete", action="store_true", help="清空标题")
    title_parser.add_argument("--file", type=str, help="从文件读取内容")
    title_parser.set_defaults(func=cmd_title)

    # info 命令
    info_parser = subparsers.add_parser("info", help="事务信息操作")
    info_parser.add_argument("id", type=int, help="事务ID")
    info_parser.add_argument("info", nargs="*", help="信息内容（无参数=显示，有内容=添加）")
    info_parser.add_argument("-d", "--delete", action="store_true", help="清空信息")
    info_parser.add_argument("-u", "--unread", action="store_true", help="标记所有信息为未读")
    info_parser.add_argument("-r", "--read", action="store_true", help="标记所有信息为未读")
    info_parser.add_argument("--file", type=str, help="从文件读取信息内容")
    info_parser.add_argument(
        "--sender",
        type=str,
        default=None,
        help="发送者标识（事务#2089：如 事务#2089；缺省=风主🍃——CLI调用者默认为风主，"
        "LLM工具调用时会显式传入事务#ID）",
    )
    info_parser.set_defaults(func=cmd_info)

    # output 命令
    output_parser = subparsers.add_parser("output", help="显示事务输出文件内容")
    output_parser.add_argument("id", type=int, help="事务ID")
    output_parser.add_argument("--begin", "-b", type=int, help="开始行号（从1开始）")
    output_parser.add_argument("--end", "-e", type=int, help="结束行号")
    output_parser.add_argument("--head", type=int, help="显示前N行")
    output_parser.add_argument("--tail", type=int, help="显示后N行")
    output_parser.add_argument(
        "--line-count", action="store_true", help="只显示总行数，不显示文件内容"
    )
    output_parser.add_argument(
        "-v", "--verbose", action="store_true", help="显示完整信息（时间戳、输出源、消息、事务）"
    )
    output_parser.add_argument("-d", "--delete", action="store_true", help="删除输出文件")
    output_parser.set_defaults(func=cmd_output)

    # rollback 命令（按回滚ID回滚到指定快照）
    rollback_parser = subparsers.add_parser(
        "rollback", help="按回滚ID回滚事务文件/上下文到指定快照（ID见 output 显示的 [R<seq>] 标记）"
    )
    rollback_parser.add_argument("id", type=int, help="事务ID")
    rollback_parser.add_argument(
        "rollback_id",
        type=str,
        nargs="?",
        default=None,
        help="回滚ID（即 output 行尾 [R<ID>] 中的数字），如 12 或 R12",
    )
    rollback_parser.add_argument("--list", "-l", action="store_true", help="列出全部可用回滚ID")
    rollback_parser.add_argument("-y", "--yes", action="store_true", help="跳过确认直接回滚")
    rollback_parser.set_defaults(func=cmd_rollback)

    args = parser.parse_args()

    if hasattr(args, "func"):
        return args.func(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())

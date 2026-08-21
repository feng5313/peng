#!/usr/bin/env python3
"""
数据导出工具 - 导出AI助手项目的消息和事务数据

用法:
    python export_data.py [--messages] [--tasks] [--output FILE] [--format FORMAT]

选项:
    --messages      导出消息数据
    --tasks         导出事务数据
    --output FILE   输出文件路径 (默认: stdout)
    --format FORMAT 输出格式：json, csv, markdown (默认: json)
"""

import argparse
import logging
# 导入PostgreSQL连接器
import sys
import traceback
from pathlib import Path

from peng.common import jsonp as json

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from peng.db.connection import get_db_connection

# 配置日志
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def export_messages(db_path: str, limit: int = 100) -> list[dict]:
    """导出消息数据"""
    conn = get_db_connection(db_path)
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, timestamp, sender, receiver, content, status,
               created_at, read_at, archived_at,
               reply_to, chain_head_id, chain_context,
               caller, caller_session_id,
               is_chain_head, is_cancelled, is_paused, is_archived,
               timeout, locked_by, locked_at, heartbeat_at, process_id
        FROM messages
        ORDER BY id DESC
        LIMIT %s
    """,
        (limit,),
    )

    messages = []
    for row in cursor.fetchall():
        msg = dict(row)
        # 解析JSON字段
        if msg.get("chain_context"):
            try:
                msg["chain_context"] = json.loads(msg["chain_context"])
            except (json.JSONDecodeError, TypeError) as e:
                logger.debug(f"解析chain_context失败，保留原始值: {e}\n{traceback.format_exc()}")
        messages.append(msg)

    conn.close()
    return messages


def export_tasks(db_path: str, limit: int = 100) -> list[dict]:
    """导出事务数据"""
    conn = get_db_connection(db_path)
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, title, content, description,
               status, progress,
               priority, task_type,
               created_at, started_at, completed_at, deadline,
               message_id, parent_task_id, root_id, depth, path,
               caller, caller_session_id,
               worktree_path, branch_name, worktree_locked,
               process_id, error, result,
               timeout, timeout_started_at,
               heartbeat_at, heartbeat_timeout,
               expertise,
               analysis_report, subtask_config,
               metadata, is_archived
        FROM tasks
        ORDER BY id DESC
        LIMIT %s
    """,
        (limit,),
    )

    tasks = []
    for row in cursor.fetchall():
        task = dict(row)
        # 解析JSON字段
        for field in [
            "expertise",
            "analysis_report",
            "subtask_config",
            "metadata",
            "chain_context",
        ]:
            if task.get(field):
                try:
                    task[field] = json.loads(task[field])
                except (json.JSONDecodeError, TypeError) as e:
                    logger.debug(
                        f"解析事务字段{field}失败，保留原始值: {e}\n{traceback.format_exc()}"
                    )
        tasks.append(task)

    conn.close()
    return tasks


def format_output(data: dict, format_type: str) -> str:
    """格式化输出数据"""
    if format_type == "json":
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)

    elif format_type == "csv":
        import csv
        import io

        output = io.StringIO()
        for _key, items in data.items():
            if not items:
                continue
            writer = csv.DictWriter(output, fieldnames=items[0].keys())
            writer.writeheader()
            for item in items:
                # 将复杂类型转换为字符串
                row = {
                    k: json.dumps(v, ensure_ascii=False, default=str)
                    if isinstance(v, (dict, list))
                    else v
                    for k, v in item.items()
                }
                writer.writerow(row)
        return output.getvalue()

    elif format_type == "markdown":
        lines = []
        for key, items in data.items():
            if not items:
                continue
            lines.append(f"## {key.title()}\n")
            # 表头
            if items:
                headers = list(items[0].keys())
                lines.append("| " + " | ".join(headers) + " |")
                lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
                # 数据行
                for item in items[:20]:  # 限制显示行数
                    row = []
                    for h in headers:
                        val = item.get(h, "")
                        if isinstance(val, (dict, list)):
                            val = json.dumps(val, ensure_ascii=False)[:50] + "..."
                        elif isinstance(val, str) and len(val) > 100:
                            val = val[:100] + "..."
                        row.append(str(val).replace("|", "\\|").replace("\n", " "))
                    lines.append("| " + " | ".join(row) + " |")
            lines.append("")
        return "\n".join(lines)

    else:
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def main():
    parser = argparse.ArgumentParser(description="数据导出工具")
    parser.add_argument("--messages", action="store_true", help="导出消息数据")
    parser.add_argument("--tasks", action="store_true", help="导出事务数据")
    parser.add_argument("--output", "-o", type=str, help="输出文件路径")
    parser.add_argument(
        "--format",
        "-f",
        type=str,
        default="json",
        choices=["json", "csv", "markdown"],
        help="输出格式",
    )
    parser.add_argument("--limit", "-l", type=int, default=100, help="限制导出数量")
    parser.add_argument("--data-dir", type=str, default="data", help="数据目录路径")

    args = parser.parse_args()

    # 如果没有指定导出类型，默认导出所有
    if not args.messages and not args.tasks:
        args.messages = True
        args.tasks = True

    # 确定数据目录
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        # 相对于脚本所在目录
        script_dir = Path(__file__).parent.parent
        data_dir = script_dir / "data"

    messages_db = data_dir / "messages.db"
    tasks_db = data_dir / "tasks.db"

    result = {}

    # 导出消息
    if args.messages:
        if messages_db.exists():
            print(f"正在导出消息数据 from {messages_db}...", file=sys.stderr)
            result["messages"] = export_messages(str(messages_db), args.limit)
            print(f"  导出 {len(result['messages'])} 条消息", file=sys.stderr)
        else:
            print(f"警告: 消息数据库不存在: {messages_db}", file=sys.stderr)
            result["messages"] = []

    # 导出事务
    if args.tasks:
        if tasks_db.exists():
            print(f"正在导出事务数据 from {tasks_db}...", file=sys.stderr)
            result["tasks"] = export_tasks(str(tasks_db), args.limit)
            print(f"  导出 {len(result['tasks'])} 个事务", file=sys.stderr)
        else:
            print(f"警告: 事务数据库不存在: {tasks_db}", file=sys.stderr)
            result["tasks"] = []

    # 格式化输出
    output = format_output(result, args.format)

    # 输出到文件或stdout
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"数据已导出到: {output_path}", file=sys.stderr)
    else:
        print(output)

    # 输出摘要
    print("\n=== 导出摘要 ===", file=sys.stderr)
    print(f"消息数量: {len(result.get('messages', []))}", file=sys.stderr)
    print(f"事务数量: {len(result.get('tasks', []))}", file=sys.stderr)
    print(f"输出格式: {args.format}", file=sys.stderr)


if __name__ == "__main__":
    main()

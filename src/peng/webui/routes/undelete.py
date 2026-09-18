"""
撤销删除（undelete）路由模块

为 WebUI 撤销/重做功能提供"删除快照备份 + 恢复"能力。

原理：
- 删除前将消息子树/事务子树（含 tasks/messages 及关联子表）导出为 JSON 快照文件
- 撤销删除时按原 ID 回插（ON CONFLICT DO NOTHING），并推进 PG 序列
- 恢复成功后删除快照文件（redo 删除时会产生新快照）

路由前缀: /api (由 server.py 注册)
- POST /undelete/restore
"""

from __future__ import annotations

import json
import logging
import re
import traceback
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from peng.common.paths import get_temp_directory, ensure_dir_with_owner
from peng.db.connection import get_connection

logger = logging.getLogger(__name__)
router = APIRouter()


# 备份快照目录与保留策略
UNDELETE_DIR = get_temp_directory() / "peng_undelete"
ensure_dir_with_owner(UNDELETE_DIR)
MAX_BACKUP_FILES = 300

# 快照文件名格式（防路径穿越，restore 时校验）
_BACKUP_NAME_RE = re.compile(r"^undel_\d{8}_\d{6}_[0-9a-f]{8}\.json$")

# 恢复顺序：先父表后子表（满足外键依赖）
_RESTORE_TABLE_ORDER = [
    "messages",
    "tasks",
    "task_steps",
    "task_dependencies",
    "task_controls",
    "message_controls",
]


class RestoreRequest(BaseModel):
    backup_file: str


# ==================== 收集（备份） ====================

def _query_rows(conn, sql: str, params: tuple) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, params)
    rows = cursor.fetchall() if cursor else []
    return [dict(r) for r in rows]


def _collect_message_descendant_ids(conn, msg_id: int) -> list[int]:
    """递归CTE收集消息及其所有后代消息ID（含自身）"""
    rows = _query_rows(
        conn,
        """
        WITH RECURSIVE descendants AS (
            SELECT id FROM messages WHERE id = %s
            UNION ALL
            SELECT m.id FROM messages m
            INNER JOIN descendants d ON m.reply_to = d.id
        )
        SELECT id FROM descendants
        """,
        (msg_id,),
    )
    return [r["id"] for r in rows]


def _collect_task_descendants(conn, task_id: int) -> list[int]:
    """收集事务及其所有后代事务ID（含自身）"""
    rows = _query_rows(
        conn,
        """
        WITH RECURSIVE descendants AS (
            SELECT id FROM tasks WHERE id = %s
            UNION ALL
            SELECT t.id FROM tasks t
            INNER JOIN descendants d ON t.parent_task_id = d.id
        )
        SELECT id FROM descendants
        """,
        (task_id,),
    )
    return [r["id"] for r in rows]


def _expand_task_trees(conn, seed_task_ids: list[int]) -> list[int]:
    """给定种子任务ID，扩展为完整任务树（含后代）ID 集合"""
    all_ids: set[int] = set()
    for tid in seed_task_ids:
        if tid in all_ids:
            continue
        for did in _collect_task_descendants(conn, tid):
            all_ids.add(did)
    return sorted(all_ids)


def _select_by_ids(
    conn, table: str, id_column: str, ids: list[int]
) -> list[dict[str, Any]]:
    """按 ID 列表查询整表行"""
    if not ids:
        return []
    placeholders = ",".join(["%s"] * len(ids))
    return _query_rows(
        conn, f"SELECT * FROM {table} WHERE {id_column} IN ({placeholders})", tuple(ids)
    )


def _tasks_by_message_ids(conn, msg_ids: list[int]) -> list[int]:
    """查询关联到指定消息集合的所有任务ID（message_id 继承自链首，含子事务）"""
    if not msg_ids:
        return []
    placeholders = ",".join(["%s"] * len(msg_ids))
    rows = _query_rows(
        conn,
        f"SELECT id FROM tasks WHERE message_id IN ({placeholders})",
        tuple(msg_ids),
    )
    return [r["id"] for r in rows]


def _collect_backup_payload(kind: str, root_id: int) -> dict[str, Any] | None:
    """
    收集删除备份快照数据。

    备份范围（为删除范围的可靠超集）：
    - kind='message'：消息子树（含自身）+ 这些消息关联的全部任务树
    - kind='task'：任务树（含自身）；若为根事务（级联会删关联消息），
      额外包含关联消息子树及其关联的全部任务树

    Args:
        kind: 'message' 或 'task'
        root_id: 消息ID或事务ID

    Returns:
        快照 payload；对象不存在返回 None
    """
    with get_connection() as conn:
        msg_ids: list[int] = []
        seed_task_ids: list[int] = []

        if kind == "message":
            msg_ids = _collect_message_descendant_ids(conn, root_id)
            if not msg_ids:
                return None
            seed_task_ids = _tasks_by_message_ids(conn, msg_ids)
        else:
            task_rows = _query_rows(conn, "SELECT * FROM tasks WHERE id = %s", (root_id,))
            if not task_rows:
                return None
            seed_task_ids = [root_id]
            # 根事务删除时级联删除关联消息（delete_task_recursive 仅根事务删消息）
            if not task_rows[0].get("parent_task_id"):
                message_id = task_rows[0].get("message_id")
                if message_id:
                    msg_ids = _collect_message_descendant_ids(conn, message_id)
                    # 消息子树下挂的其他根任务也一并纳入（防漏）
                    seed_task_ids.extend(_tasks_by_message_ids(conn, msg_ids))

        task_ids = _expand_task_trees(conn, sorted(set(seed_task_ids))) if seed_task_ids else []
        msg_ids = sorted(set(msg_ids))

        messages = _select_by_ids(conn, "messages", "id", msg_ids)
        tasks = _select_by_ids(conn, "tasks", "id", task_ids)
        task_steps = _select_by_ids(conn, "task_steps", "task_id", task_ids)
        task_dependencies = _select_by_ids(conn, "task_dependencies", "task_id", task_ids)
        task_controls = _select_by_ids(conn, "task_controls", "task_id", task_ids)
        message_controls = _select_by_ids(conn, "message_controls", "message_id", msg_ids)

    # 按 id 升序排序，保证恢复时父记录先于子记录插入
    messages.sort(key=lambda r: r["id"])
    tasks.sort(key=lambda r: r["id"])

    return {
        "version": 1,
        "kind": kind,
        "root_id": root_id,
        "created_at": datetime.now().isoformat(),
        "messages": messages,
        "tasks": tasks,
        "task_steps": task_steps,
        "task_dependencies": task_dependencies,
        "task_controls": task_controls,
        "message_controls": message_controls,
    }


def _prune_old_backups() -> None:
    """备份目录超过上限时删除最老的快照文件"""
    try:
        files = sorted(
            (p for p in UNDELETE_DIR.iterdir() if p.is_file() and _BACKUP_NAME_RE.match(p.name)),
            key=lambda p: p.name,  # 文件名含时间戳，字典序即时间序
        )
        excess = len(files) - MAX_BACKUP_FILES
        for p in files[:excess]:
            try:
                p.unlink()
                logger.info(f"[undelete] 清理过期备份: {p.name}")
            except OSError:
                pass
    except Exception as e:
        logger.warning(f"[undelete] 清理备份目录失败: {e}")


def backup_for_delete(kind: str, root_id: int) -> str | None:
    """
    删除前调用：生成备份快照文件。

    Args:
        kind: 'message' 或 'task'
        root_id: 消息ID或事务ID

    Returns:
        快照文件名（非路径）；对象不存在或备份失败返回 None（不阻塞删除，
        但意味着该删除不可撤销）
    """
    try:
        payload = _collect_backup_payload(kind, root_id)
        if not payload:
            return None
        name = f"undel_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.json"
        path = UNDELETE_DIR / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        _prune_old_backups()
        logger.info(
            f"[undelete] 已备份 {kind}#{root_id}: {name} "
            f"(messages={len(payload['messages'])}, tasks={len(payload['tasks'])})"
        )
        return name
    except Exception as e:
        logger.error(f"[undelete] 备份失败（删除仍继续）: {e}\n{traceback.format_exc()}")
        return None


# ==================== 恢复 ====================

def _restore_rows(conn, table: str, rows: list[dict[str, Any]]) -> int:
    """按列名批量回插，id 冲突跳过。返回实际插入行数。"""
    inserted = 0
    for row in rows:
        if not row:
            continue
        cols = list(row.keys())
        placeholders = ",".join(["%s"] * len(cols))
        sql = (
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT (id) DO NOTHING"
        )
        cursor = conn.execute(sql, tuple(row[c] for c in cols))
        if cursor is not None and getattr(cursor, "rowcount", 0):
            inserted += cursor.rowcount
    return inserted


def _do_restore(backup_name: str) -> dict[str, Any]:
    """执行恢复（同步，在线程池中调用）。成功后删除快照文件。"""
    path = UNDELETE_DIR / backup_name
    if not path.is_file():
        raise FileNotFoundError(f"备份快照不存在: {backup_name}")

    payload = json.loads(path.read_text(encoding="utf-8"))

    stats: dict[str, Any] = {}
    with get_connection() as conn:
        for table in _RESTORE_TABLE_ORDER:
            rows = payload.get(table) or []
            stats[table] = _restore_rows(conn, table, rows)

        # 推进序列，防止后续自动生成 id 与恢复的显式 id 冲突
        conn.execute(
            "SELECT setval(pg_get_serial_sequence('messages', 'id'), "
            "(SELECT COALESCE(MAX(id), 1) FROM messages), true)"
        )
        conn.execute(
            "SELECT setval(pg_get_serial_sequence('tasks', 'id'), "
            "(SELECT COALESCE(MAX(id), 1) FROM tasks), true)"
        )

    # 恢复成功后删除快照（redo 删除时会生成新快照）
    try:
        path.unlink()
    except OSError:
        pass

    logger.info(
        f"[undelete] 恢复完成 {backup_name}: "
        f"messages={stats['messages']}, tasks={stats['tasks']}"
    )
    return {
        "restored_messages": stats["messages"],
        "restored_tasks": stats["tasks"],
        "root_kind": payload.get("kind"),
        "root_id": payload.get("root_id"),
        "message_ids": [m["id"] for m in payload.get("messages", [])],
        "task_ids": [t["id"] for t in payload.get("tasks", [])],
    }


@router.post("/undelete/restore")
async def restore_deleted(request: RestoreRequest) -> dict[str, Any]:
    """从备份快照恢复被删除的消息/事务（撤销删除）"""
    import asyncio

    name = (request.backup_file or "").strip()
    if not _BACKUP_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="非法的备份文件名")

    try:
        result = await asyncio.to_thread(_do_restore, name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"[undelete] 恢复失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"恢复失败: {e}") from None

    # 广播恢复事件，其他 webui 实例同步刷新
    from ..websocket import broadcast_message_update, broadcast_task_update

    if result["message_ids"]:
        await broadcast_message_update(
            result["message_ids"][0], "restored", {"ids": result["message_ids"]}, source="webui"
        )
    for tid in result["task_ids"]:
        await broadcast_task_update(tid, "restored", source="webui")

    return {
        "success": True,
        "message": (
            f"已恢复 {result['restored_messages']} 条消息、{result['restored_tasks']} 个事务"
        ),
        **result,
    }

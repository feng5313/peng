"""
测试事务#2802：暂停/取消/继续/重启默认不联动子事务，仅显式 recursive 时联动

测试覆盖：
1. stop_task / cancel_task / pause_task 默认（recursive=False）只操作事务自身
2. recursive=True 时递归操作整棵事务树（running/accepting 的后代）
3. CLI parser：pause/cancel/resume/restart 支持 -r/--recursive 参数
4. WebUI TaskControlRequest.recursive 默认 False
"""
from __future__ import annotations

# 在导入 peng 模块之前 mock psycopg2 及所有子模块，避免环境依赖
import sys
from unittest.mock import MagicMock

_mock_psycopg2 = MagicMock()
_mock_psycopg2.IntegrityError = type("IntegrityError", (Exception,), {})
_mock_psycopg2.errors = MagicMock()
sys.modules.setdefault("psycopg2", _mock_psycopg2)
sys.modules.setdefault("psycopg2.extras", MagicMock())
sys.modules.setdefault("psycopg2.pool", MagicMock())
sys.modules.setdefault("psycopg2.errors", MagicMock())
sys.modules.setdefault("asyncpg", MagicMock())

import pytest
from unittest.mock import patch

from peng.task.manager import TaskManager
from peng.task.database import TaskDatabase
from peng.common.types import MetaSingleton


# ==================== Fixtures ====================


@pytest.fixture(autouse=True)
def _clean_singletons():
    """每个测试前后清理 MetaSingleton 缓存，避免测试间状态污染"""
    saved = dict(MetaSingleton._instances)
    MetaSingleton._instances.clear()
    yield
    MetaSingleton._instances.clear()
    MetaSingleton._instances.update(saved)


@pytest.fixture
def mock_db():
    """创建 mock 的 TaskDatabase 实例，绕过 PostgreSQL 连接"""
    with patch.object(TaskDatabase, "__init__", lambda self: None):
        db = TaskDatabase()
        db.update_tasks = MagicMock(return_value=1)
        db.update_task = MagicMock(return_value=True)
        db.get_descendants = MagicMock(return_value=[])
        return db


@pytest.fixture
def tm(mock_db):
    """创建 mock 的 TaskManager 实例（绕过 __init__，手动注入 db）"""
    with patch.object(TaskManager, "__init__", lambda self: None):
        manager = TaskManager()
        manager.db = mock_db
        manager.message_manager = None
        return manager


def _task(task_id: int, status: str = "running") -> dict:
    return {"id": task_id, "status": status, "message_id": None, "context": ""}


# ==================== stop_task / cancel_task / pause_task ====================


@pytest.mark.asyncio
async def test_cancel_task_default_not_recursive(tm, mock_db):
    """cancel 默认只取消自身，不联动子事务"""
    mock_db.get_task = MagicMock(return_value=_task(1))
    with patch.object(TaskManager, "_cleanup_nest_browser"):
        ok = await tm.cancel_task(1, reason="测试")
    assert ok is True
    mock_db.get_descendants.assert_not_called()
    # update_tasks 只包含事务自身
    args, kwargs = mock_db.update_tasks.call_args
    assert args[0] == [1]


@pytest.mark.asyncio
async def test_cancel_task_recursive(tm, mock_db):
    """cancel 显式 recursive=True 时递归取消 running 子事务"""
    mock_db.get_task = MagicMock(
        side_effect=lambda tid: _task(tid)
    )
    mock_db.get_descendants = MagicMock(
        return_value=[{"id": 2, "status": "running"}, {"id": 3, "status": "paused"}]
    )
    with patch.object(TaskManager, "_cleanup_nest_browser"):
        ok = await tm.cancel_task(1, recursive=True, kill_process=False, reason="测试")
    assert ok is True
    mock_db.get_descendants.assert_called_once_with(1)
    # 只有 running 的子事务2 + 自身1（paused 的3不在列表）
    args, kwargs = mock_db.update_tasks.call_args
    assert sorted(args[0]) == [1, 2]


@pytest.mark.asyncio
async def test_pause_task_default_not_recursive(tm, mock_db):
    """pause 默认只暂停自身，不联动子事务"""
    mock_db.get_task = MagicMock(return_value=_task(1))
    ok = await tm.pause_task(1, reason="测试")
    assert ok is True
    mock_db.get_descendants.assert_not_called()
    args, kwargs = mock_db.update_tasks.call_args
    assert args[0] == [1]


@pytest.mark.asyncio
async def test_pause_task_recursive(tm, mock_db):
    """pause 显式 recursive=True 时递归暂停 running 子事务"""
    mock_db.get_task = MagicMock(side_effect=lambda tid: _task(tid))
    mock_db.get_descendants = MagicMock(
        return_value=[{"id": 2, "status": "accepting"}, {"id": 3, "status": "completed"}]
    )
    ok = await tm.pause_task(1, recursive=True, reason="测试")
    assert ok is True
    args, kwargs = mock_db.update_tasks.call_args
    # accepting 的子事务2参与，completed 的3不参与
    assert sorted(args[0]) == [1, 2]


@pytest.mark.asyncio
async def test_stop_task_keep_context_pause_only_self(tm, mock_db):
    """stop_task(keep_context=True) 默认不联动子事务"""
    mock_db.get_task = MagicMock(return_value=_task(1))
    ok = tm.stop_task(1, keep_context=True, reason="测试")
    assert ok is True
    mock_db.get_descendants.assert_not_called()
    args, kwargs = mock_db.update_tasks.call_args
    assert args[0] == [1]


# ==================== CLI parser ====================


def _build_parser():
    """构建 task CLI parser（导入可能依赖环境，失败则跳过）"""
    from peng.cli._task_impl import build_parser

    return build_parser()


@pytest.mark.parametrize("cmd", ["pause", "cancel", "resume", "restart"])
def test_cli_recursive_flag(cmd):
    """CLI pause/cancel/resume/restart 支持 -r/--recursive，默认关闭"""
    parser = _build_parser()
    # 默认不带 -r
    args = parser.parse_args([cmd, "123"])
    assert args.recursive is False
    # -r 短参数
    args = parser.parse_args([cmd, "123", "-r"])
    assert args.recursive is True
    # --recursive 长参数
    args = parser.parse_args([cmd, "123", "--recursive"])
    assert args.recursive is True


def test_cli_restart_force_and_recursive():
    """CLI restart 同时支持 -f 和 -r"""
    parser = _build_parser()
    args = parser.parse_args(["restart", "123", "-f", "-r"])
    assert args.force is True
    assert args.recursive is True


# ==================== WebUI TaskControlRequest ====================


def test_webui_control_request_recursive_default():
    """WebUI 控制请求 recursive 默认 False"""
    from peng.webui.routes.task_cli_api import TaskControlRequest

    req = TaskControlRequest(action="cancel")
    assert req.recursive is False
    req = TaskControlRequest(action="cancel", recursive=True)
    assert req.recursive is True


# ==================== 阶段2：消息级控制只作用于根事务（消息#14028/14029/14030） ====================
# 根因：子事务创建时 message_id 继承根消息ID，list_tasks(message_id) 会匹配到
# 整棵事务树 → resume_message 启动整棵树。修复：消息级操作一律 root_only=True。


@pytest.fixture
def msg_mgr():
    """构造绕过 __init__ 的 MessageManager，手动注入 mock 依赖"""
    from unittest.mock import AsyncMock
    from peng.message.manager import MessageManager

    with patch.object(MessageManager, "__init__", lambda self: None):
        m = MessageManager()
        m.db = MagicMock()
        m._task_manager = MagicMock()
        m._task_manager.resume_task = AsyncMock(return_value=True)
        m._task_manager.pause_task = AsyncMock(return_value=True)
        m._task_manager.cancel_task = AsyncMock(return_value=True)
        return m


_ROOT_TASK = {"id": 100, "message_id": 55, "parent_task_id": None, "status": "completed"}
_CHILD_TASK = {"id": 101, "message_id": 55, "parent_task_id": 100, "status": "paused"}


@pytest.mark.asyncio
async def test_resume_message_only_root(msg_mgr):
    """收到回复消息只启动根事务：list_tasks 必须带 root_only=True"""
    msg_mgr.db.get_message = MagicMock(
        return_value={"id": 55, "status": "done", "content": ""}
    )
    msg_mgr.db.update_message_status = MagicMock(return_value=True)
    msg_mgr._task_manager.list_tasks = MagicMock(return_value=[_ROOT_TASK])

    ok = await msg_mgr.resume_message(55)
    assert ok is True
    # 关键断言：root_only=True（否则会查到子事务启动整棵树）
    msg_mgr._task_manager.list_tasks.assert_called_once_with(message_id=55, root_only=True)
    # 只 resume 根事务
    msg_mgr._task_manager.resume_task.assert_called_once()


@pytest.mark.asyncio
async def test_pause_message_only_root(msg_mgr):
    """消息暂停只暂停根事务"""
    msg_mgr.db.get_message = MagicMock(
        return_value={"id": 55, "status": "processing", "content": ""}
    )
    msg_mgr.db.update_message_status = MagicMock(return_value=True)
    msg_mgr._task_manager.list_tasks = MagicMock(return_value=[_ROOT_TASK])

    ok = await msg_mgr.pause_message(55)
    assert ok is True
    msg_mgr._task_manager.list_tasks.assert_called_once_with(message_id=55, root_only=True)


@pytest.mark.asyncio
async def test_cancel_message_only_root(msg_mgr):
    """消息取消只取消根事务"""
    msg_mgr.db.get_message = MagicMock(
        return_value={"id": 55, "status": "processing", "content": ""}
    )
    msg_mgr.db.update_message_status = MagicMock(return_value=True)
    msg_mgr._task_manager.list_tasks = MagicMock(return_value=[_ROOT_TASK])

    ok = await msg_mgr.cancel_message(55)
    assert ok is True
    msg_mgr._task_manager.list_tasks.assert_called_once_with(message_id=55, root_only=True)


def test_control_message_task_only_root(monkeypatch):
    """消息级控制按钮（cli_service.control_message_task）只对根事务插控制"""
    import peng.message.cli_service as cli_service

    msg_db = MagicMock()
    task_db = MagicMock()
    monkeypatch.setattr(cli_service, "get_message_manager", lambda: MagicMock())
    monkeypatch.setattr(cli_service, "get_message_database", lambda: msg_db)
    monkeypatch.setattr(cli_service, "get_task_manager", lambda: MagicMock(
        get_tasks_by_message=MagicMock(return_value=[_ROOT_TASK, _CHILD_TASK])
    ))
    monkeypatch.setattr("peng.task.database.TaskDatabase", lambda: task_db)

    msg_db.get_message = MagicMock(return_value={"id": 55, "status": "done"})
    result = cli_service.control_message_task(55, "pause")

    assert result["success"] is True
    # 只对根事务100插控制，子事务101不受影响
    inserted = [call.args[0] for call in task_db.insert_task_control.call_args_list]
    assert inserted == [100]


def test_get_tasks_by_message_ids_list_root_only_sql(mock_db):
    """get_tasks_by_message_ids_list(root_only=True) SQL 含 parent_task_id IS NULL"""
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_conn = MagicMock()
    mock_conn.execute.return_value = mock_cursor
    mock_db._get_connection = MagicMock(
        return_value=MagicMock(
            __enter__=MagicMock(return_value=mock_conn),
            __exit__=MagicMock(return_value=False),
        )
    )

    mock_db.get_tasks_by_message_ids_list([55], root_only=True)
    sql = mock_conn.execute.call_args[0][0]
    assert "parent_task_id IS NULL" in sql

    # 默认不含过滤（兼容既有用途）
    mock_conn.execute.reset_mock()
    mock_db.get_tasks_by_message_ids_list([55])
    sql = mock_conn.execute.call_args[0][0]
    assert "parent_task_id IS NULL" not in sql

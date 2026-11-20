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

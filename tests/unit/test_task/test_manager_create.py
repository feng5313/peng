"""
测试 TaskManager.create_task 和 TaskDatabase.create_task 的核心行为

测试覆盖：
1. 基本创建（root task / subtask）
2. workdir 从消息继承
3. 重复根事务保护
4. 事务树字段自动计算（root_id / depth / path）
5. nest 继承（消息 → 事务 / 父事务 → 子事务）
6. @timer= 内容解析
7. @wait# 依赖解析
8. 类型转换逻辑
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
from datetime import datetime

from peng.task.manager import TaskManager
from peng.task.database import TaskDatabase
from peng.task.task_types import TaskPriority, TaskStatus, TaskType
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
    """
    创建 mock 的 TaskDatabase 实例，绕过 PostgreSQL 连接。

    使用 MagicMock 替代 TaskDatabase 单例，支持对 create_task /
    get_task / list_tasks 等方法的跟踪和断言。
    """
    with patch.object(TaskDatabase, "__init__", lambda self: None):
        db = TaskDatabase()
        # 模拟 cursor 和 connection 上下文管理器
        mock_cursor = MagicMock()
        mock_cursor.lastrowid = 1
        mock_cursor.rowcount = 1
        mock_cursor.fetchone.return_value = None
        mock_cursor.fetchall.return_value = []

        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        db._get_connection = MagicMock(return_value=mock_conn)
        return db


@pytest.fixture
def mock_task_manager(mock_db):
    """
    创建使用 mock db 的 TaskManager 实例。

    TaskManager 是 MetaSingleton，需要绕过已初始化的实例。
    """
    with patch.object(TaskManager, "__init__", lambda self: None):
        mgr = TaskManager()
        mgr.db = mock_db
        mgr.message_manager = None
        return mgr


# ==================== TaskManager.create_task 测试 ====================


class TestTaskManagerCreateTask:
    """TaskManager.create_task() 高级接口测试"""

    def test_create_task_passes_kwargs_to_db(self, mock_task_manager, mock_db):
        """create_task 应该将 kwargs 透传给 db.create_task"""
        mock_db.create_task = MagicMock(return_value=42)

        result = mock_task_manager.create_task(
            title="测试事务",
            content="do something",
            priority=TaskPriority.HIGH,
        )

        assert result == 42
        mock_db.create_task.assert_called_once()
        call_kwargs = mock_db.create_task.call_args[1]
        assert call_kwargs["title"] == "测试事务"
        assert call_kwargs["content"] == "do something"
        assert call_kwargs["priority"] == TaskPriority.HIGH

    def test_create_task_inherits_workdir_from_message(self, mock_task_manager, mock_db):
        """当 workdir 未设置且有 message_id 时，应从消息继承 workdir"""
        mock_db.create_task = MagicMock(return_value=10)

        mock_msg = {"id": 5, "workdir": "/home/user/project"}
        with patch("peng.message.database.MessageDatabase") as MockMsgDB:
            MockMsgDB.return_value.get_message.return_value = mock_msg
            result = mock_task_manager.create_task(
                title="子事务",
                message_id=5,
            )

        assert result == 10
        call_kwargs = mock_db.create_task.call_args[1]
        assert call_kwargs["workdir"] == "/home/user/project"

    def test_create_task_no_workdir_without_message_id(self, mock_task_manager, mock_db):
        """没有 message_id 时不尝试继承 workdir"""
        mock_db.create_task = MagicMock(return_value=11)

        result = mock_task_manager.create_task(title="独立事务")
        assert result == 11
        call_kwargs = mock_db.create_task.call_args[1]
        # workdir 不应被设置
        assert "workdir" not in call_kwargs or call_kwargs.get("workdir") is None

    def test_create_task_workdir_already_set_skips_inheritance(self, mock_task_manager, mock_db):
        """workdir 已设置时不覆盖"""
        mock_db.create_task = MagicMock(return_value=12)

        with patch("peng.message.database.MessageDatabase") as MockMsgDB:
            MockMsgDB.return_value.get_message.return_value = {
                "id": 5, "workdir": "/should/not/be/used"
            }
            result = mock_task_manager.create_task(
                title="事务",
                message_id=5,
                workdir="/my/custom/dir",
            )

        call_kwargs = mock_db.create_task.call_args[1]
        assert call_kwargs["workdir"] == "/my/custom/dir"

    def test_create_task_workdir_inheritance_failure_handled(self, mock_task_manager, mock_db):
        """消息数据库异常时 workdir 继承失败不应阻断创建"""
        mock_db.create_task = MagicMock(return_value=13)

        with patch("peng.message.database.MessageDatabase") as MockMsgDB:
            MockMsgDB.return_value.get_message.side_effect = Exception("DB error")
            result = mock_task_manager.create_task(
                title="事务",
                message_id=99,
            )

        assert result == 13

    def test_create_task_message_has_no_workdir(self, mock_task_manager, mock_db):
        """消息存在但没有 workdir 字段时不应设置 workdir"""
        mock_db.create_task = MagicMock(return_value=14)

        with patch("peng.message.database.MessageDatabase") as MockMsgDB:
            MockMsgDB.return_value.get_message.return_value = {"id": 5, "workdir": ""}
            result = mock_task_manager.create_task(
                title="事务",
                message_id=5,
            )

        assert result == 14
        call_kwargs = mock_db.create_task.call_args[1]
        assert "workdir" not in call_kwargs or not call_kwargs.get("workdir")

    def test_create_task_message_not_found(self, mock_task_manager, mock_db):
        """消息不存在时 workdir 继承不报错"""
        mock_db.create_task = MagicMock(return_value=15)

        with patch("peng.message.database.MessageDatabase") as MockMsgDB:
            MockMsgDB.return_value.get_message.return_value = None
            result = mock_task_manager.create_task(
                title="事务",
                message_id=999,
            )

        assert result == 15


# ==================== TaskDatabase.create_task 测试 ====================


class TestTaskDatabaseCreateTask:
    """TaskDatabase.create_task() 底层逻辑测试"""

    def _setup_mock_conn(self, mock_db, lastrowid=1, existing_tasks=None):
        """辅助方法：设置 mock 连接返回指定 task_id"""
        mock_cursor = MagicMock()
        mock_cursor.lastrowid = lastrowid
        mock_cursor.fetchone.return_value = None
        mock_cursor.fetchall.return_value = []

        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_db._get_connection.return_value = mock_conn

        # mock list_tasks for duplicate root task check
        mock_db.list_tasks = MagicMock(return_value=existing_tasks or [])
        # mock get_task for parent inheritance
        mock_db.get_task = MagicMock(return_value=None)
        # mock _add_wait_dependencies
        mock_db._add_wait_dependencies = MagicMock()

        return mock_conn, mock_cursor

    def test_basic_root_task_creation(self, mock_db):
        """创建根事务：应设置 root_id 和 path"""
        mock_conn, mock_cursor = self._setup_mock_conn(mock_db, lastrowid=100)

        task_id = mock_db.create_task(title="根事务", content="测试")

        assert task_id == 100
        # 应执行至少两次 SQL：INSERT + UPDATE（设置 root_id/path）
        assert mock_conn.execute.call_count >= 2

        # 验证 UPDATE 语句设置了 root_id 和 path
        update_calls = [str(call) for call in mock_conn.execute.call_args_list]
        has_root_update = any(
            "root_id" in str(call) and "path" in str(call)
            for call in update_calls
        )
        assert has_root_update, f"应执行 UPDATE 设置 root_id/path, 实际调用: {update_calls}"

    def test_duplicate_root_task_protection(self, mock_db):
        """同一 message_id 不应创建重复根事务，应返回已有事务ID"""
        existing = [{"id": 50, "title": "已存在"}]
        self._setup_mock_conn(mock_db, existing_tasks=existing)

        task_id = mock_db.create_task(title="重复事务", message_id=5)

        # 应返回已有事务ID
        assert task_id == 50
        mock_db.list_tasks.assert_called_once_with(message_id=5, root_only=True)

    def test_duplicate_protection_only_for_root_tasks(self, mock_db):
        """重复保护只针对根事务（有 parent_task_id 时跳过检查）"""
        mock_conn, mock_cursor = self._setup_mock_conn(mock_db, lastrowid=60)
        # 即使 list_tasks 返回已有事务，有 parent_task_id 时也应正常创建
        mock_db.list_tasks = MagicMock(return_value=[{"id": 50}])

        parent_task = {
            "id": 50, "root_id": 50, "depth": 0, "path": "/50", "nest": "",
        }
        mock_db.get_task = MagicMock(return_value=parent_task)

        task_id = mock_db.create_task(
            title="子事务", message_id=5, parent_task_id=50,
        )
        assert task_id == 60

    def test_subtask_inherits_tree_fields(self, mock_db):
        """子事务应继承父事务的 root_id/depth/path 并更新"""
        mock_conn, mock_cursor = self._setup_mock_conn(mock_db, lastrowid=200)

        parent_task = {
            "id": 100, "root_id": 100, "depth": 0, "path": "/100", "nest": "local",
        }
        mock_db.get_task = MagicMock(return_value=parent_task)

        task_id = mock_db.create_task(title="子事务", parent_task_id=100)

        assert task_id == 200
        mock_db.get_task.assert_called()

    def test_subtask_path_calculation(self, mock_db):
        """子事务的 path 应在 _compute_tree_fields 和 _insert_task_record 中正确计算"""
        mock_conn, mock_cursor = self._setup_mock_conn(mock_db, lastrowid=201)

        parent_task = {
            "id": 100, "root_id": 100, "depth": 1, "path": "/100/150", "nest": "local",
        }
        mock_db.get_task = MagicMock(return_value=parent_task)

        task_id = mock_db.create_task(title="孙事务", parent_task_id=150)

        assert task_id == 201
        # _compute_tree_fields 设置 path = parent_path/parent_task_id = /100/150/150
        # _insert_task_record 再更新为 parent_path/task_id = /100/150/150/201
        update_calls = mock_conn.execute.call_args_list
        # 找到 UPDATE path 的调用
        path_update_found = False
        for call in update_calls:
            args = call[0]
            if len(args) >= 2 and "UPDATE" in str(args[0]) and "path" in str(args[0]):
                params = args[1]
                # correct_path = fields["path"] (="/100/150/150") + "/201" = "/100/150/150/201"
                assert params[0] == "/100/150/150/201"
                assert params[1] == 201
                path_update_found = True
        assert path_update_found, f"应执行 UPDATE path, 实际调用: {update_calls}"

    def test_nest_inherited_from_message(self, mock_db):
        """事务应从关联消息继承 nest"""
        mock_conn, mock_cursor = self._setup_mock_conn(mock_db, lastrowid=300)

        with patch("peng.message.database.MessageDatabase") as MockMsgDB:
            MockMsgDB.return_value.get_message.return_value = {"id": 10, "nest": "win11"}
            task_id = mock_db.create_task(title="从消息继承nest", message_id=10)

        assert task_id == 300

    def test_nest_inherited_from_parent(self, mock_db):
        """子事务应从父事务继承 nest（当消息未指定时）"""
        mock_conn, mock_cursor = self._setup_mock_conn(mock_db, lastrowid=301)

        parent_task = {
            "id": 100, "root_id": 100, "depth": 0, "path": "/100", "nest": "remote-server",
        }
        mock_db.get_task = MagicMock(return_value=parent_task)

        with patch("peng.message.database.MessageDatabase") as MockMsgDB:
            MockMsgDB.return_value.get_message.return_value = None
            task_id = mock_db.create_task(title="继承父nest", parent_task_id=100)

        assert task_id == 301

    def test_subtask_inherits_message_id_from_root(self, mock_db):
        """子事务未指定 message_id 时，应从 root 事务继承"""
        mock_conn, mock_cursor = self._setup_mock_conn(mock_db, lastrowid=400)

        parent_task = {
            "id": 100, "root_id": 50, "depth": 0, "path": "/50/100",
            "nest": "", "message_id": 5,
        }
        root_task = {"id": 50, "message_id": 5}
        mock_db.get_task = MagicMock(side_effect=[parent_task, root_task])

        task_id = mock_db.create_task(title="孙事务", parent_task_id=100)

        assert task_id == 400

    def test_create_task_with_specified_id(self, mock_db):
        """支持指定 id 创建事务"""
        mock_conn, mock_cursor = self._setup_mock_conn(mock_db, lastrowid=999)

        task_id = mock_db.create_task(title="指定ID事务", id=888)

        assert task_id == 888
        # INSERT 应包含 id 列
        insert_call = mock_conn.execute.call_args_list[0]
        sql = insert_call[0][0]
        assert "id" in sql

    def test_timer_parsed_from_content(self, mock_db):
        """content 中的 @timer=600 应被解析"""
        mock_conn, mock_cursor = self._setup_mock_conn(mock_db, lastrowid=500)

        task_id = mock_db.create_task(
            title="定时任务", content="定时重启 @timer=600",
        )

        assert task_id == 500
        # 验证 INSERT 参数中包含 timer 值
        # 🔧 事务#2307：@标记会触发provider自动选择（同优先级轮换查DB活跃数），
        # INSERT不再保证是第0个execute调用 → 按SQL关键字定位INSERT
        insert_call = next(
            c for c in mock_conn.execute.call_args_list if "INSERT" in str(c[0][0]).upper()
        )
        insert_params = insert_call[0][1] if len(insert_call[0]) > 1 else ()
        # timer_int 应为 600（在 insert_values 元组中）
        assert 600 in insert_params

    def test_default_status_is_pending(self, mock_db):
        """未指定 status 时默认为 pending"""
        mock_conn, mock_cursor = self._setup_mock_conn(mock_db, lastrowid=600)

        task_id = mock_db.create_task(title="默认状态")

        insert_call = mock_conn.execute.call_args_list[0]
        insert_params = insert_call[0][1] if len(insert_call[0]) > 1 else ()
        assert "pending" in insert_params


# ==================== @timer= 内容解析测试 ====================


class TestTimerParsing:
    """测试 @timer= 表达式的解析"""

    def test_parse_timer_valid(self):
        """正确的 @timer=600 应返回 600"""
        from peng.task.timer import parse_timer
        assert parse_timer("do something @timer=600") == 600

    def test_parse_timer_no_timer(self):
        """没有 @timer= 应返回 None"""
        from peng.task.timer import parse_timer
        assert parse_timer("普通内容") is None

    def test_parse_timer_zero_ignored(self):
        """@timer=0 应返回 None（无效值）"""
        from peng.task.timer import parse_timer
        assert parse_timer("@timer=0") is None

    def test_parse_timer_none_content(self):
        """None 内容应返回 None"""
        from peng.task.timer import parse_timer
        assert parse_timer(None) is None

    def test_parse_timer_short_content(self):
        """过短的内容应返回 None"""
        from peng.task.timer import parse_timer
        assert parse_timer("abc") is None


# ==================== @wait# 依赖解析测试 ====================


class TestWaitDependencyParsing:
    """测试 @wait# 依赖引用的解析"""

    def test_parse_wait_refs_single(self):
        """单个 @wait#42 应解析出 [42]"""
        db = TaskDatabase.__new__(TaskDatabase)
        result = db._parse_wait_refs("等待 @wait#42 完成")
        assert result == [42]

    def test_parse_wait_refs_multiple(self):
        """多个 @wait# 引用应全部解析"""
        db = TaskDatabase.__new__(TaskDatabase)
        result = db._parse_wait_refs("等待 @wait#10 和 @wait#20")
        assert result == [10, 20]

    def test_parse_wait_refs_duplicate(self):
        """重复引用应去重"""
        db = TaskDatabase.__new__(TaskDatabase)
        result = db._parse_wait_refs("@wait#5 @wait#5")
        assert result == [5]

    def test_parse_wait_refs_empty(self):
        """无引用应返回空列表"""
        db = TaskDatabase.__new__(TaskDatabase)
        result = db._parse_wait_refs("没有依赖")
        assert result == []

    def test_parse_wait_refs_none(self):
        """None 内容应返回空列表"""
        db = TaskDatabase.__new__(TaskDatabase)
        result = db._parse_wait_refs(None)
        assert result == []

    def test_parse_wait_refs_zero_id_ignored(self):
        """@wait#0 不应被包含（id 必须 > 0）"""
        db = TaskDatabase.__new__(TaskDatabase)
        result = db._parse_wait_refs("@wait#0 @wait#5")
        assert result == [5]


# ==================== 类型转换测试 ====================


class TestTypeConversions:
    """测试 create_task 中的类型转换逻辑"""

    def test_priority_enum_to_value(self):
        """TaskPriority 枚举应正确转换为 int"""
        assert TaskPriority.CRITICAL.value == 0
        assert TaskPriority.HIGH.value == 1
        assert TaskPriority.NORMAL.value == 2
        assert TaskPriority.LOW.value == 3

    def test_task_type_enum_to_value(self):
        """TaskType 枚举应正确转换为 str"""
        assert TaskType.AUTO.value == "auto"
        assert TaskType.MESSAGE.value == "message"
        assert TaskType.CLI.value == "cli"

    def test_task_status_values(self):
        """TaskStatus 枚举值应覆盖所有合法状态"""
        valid_statuses = {
            "pending", "running", "accepting", "accepted",
            "paused", "interrupted", "completed", "failed", "cancelled",
        }
        actual = {s.value for s in TaskStatus}
        assert actual == valid_statuses

    def test_priority_comparison(self):
        """优先级数值越小越高"""
        assert TaskPriority.CRITICAL < TaskPriority.HIGH
        assert TaskPriority.HIGH < TaskPriority.NORMAL
        assert TaskPriority.NORMAL < TaskPriority.LOW

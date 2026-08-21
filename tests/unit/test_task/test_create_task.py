"""
测试 TaskDatabase.create_task 的重构

测试策略：
- mock _get_connection / get_task / list_tasks 等数据库操作
- 对每个提取出的子方法编写独立测试
- 对 create_task 整体做集成测试验证行为等价
"""

import json
from unittest.mock import MagicMock, patch, call
from datetime import datetime

import pytest

from peng.task.database import TaskDatabase
from peng.task.task_types import TaskPriority, TaskType


# ==================== Fixtures ====================

@pytest.fixture
def db():
    """创建 TaskDatabase 实例（不触发单例限制，每次新建）"""
    instance = object.__new__(TaskDatabase)
    return instance


@pytest.fixture
def mock_conn():
    """mock 数据库连接"""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.lastrowid = 42  # 默认生成的 task_id
    conn.execute.return_value = cursor
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


def _make_insert_fields(**overrides):
    """构造 _insert_task_record 所需的完整字段字典"""
    base = {
        "title": "",
        "content": "",
        "description": None,
        "status": "pending",
        "priority": 2,
        "task_type": "auto",
        "created_at": datetime.now().isoformat(),
        "message_id": None,
        "parent_task_id": None,
        "caller": None,
        "caller_session_id": None,
        "timeout": 0,
        "deadline": None,
        "metadata": "{}",
        "root_id": None,
        "depth": 0,
        "path": "",
        "worktree_path": None,
        "branch_name": None,
        "worktree_locked": 0,
        "expertise": "",
        "prompt": "",
        "workdir": None,
        "provider": "",
        "small_model": 0,
        "is_complex": 0,
        "info": "",
        "creator": "",
        "timer": 0,
        "nest": None,
        "id": None,
    }
    base.update(overrides)
    return base


# ==================== _extract_create_fields ====================

class TestExtractCreateFields:
    """测试字段提取和类型转换"""

    def test_default_values(self, db):
        """未提供参数时，应使用合理的默认值"""
        fields = db._extract_create_fields({})
        assert fields["title"] == ""
        assert fields["content"] == ""
        assert fields["description"] is None
        assert fields["priority"] == TaskPriority.NORMAL.value
        assert fields["task_type"] == TaskType.AUTO.value
        assert fields["message_id"] is None
        assert fields["parent_task_id"] is None
        assert fields["status"] == "pending"
        assert fields["small_model"] == 0
        assert fields["is_complex"] == 0
        assert fields["timer"] == 0
        assert fields["nest"] is None
        assert fields["workdir"] is None

    def test_explicit_values(self, db):
        """显式提供的参数应正确传递"""
        fields = db._extract_create_fields({
            "title": "测试事务",
            "content": "内容 @timer=300",
            "priority": TaskPriority.HIGH,
            "task_type": TaskType.CLI,
            "message_id": 10,
            "parent_task_id": 5,
            "status": "running",
            "small_model": True,
            "is_complex": True,
            "nest": "win11",
            "workdir": "/tmp/work",
            "creator": "test_user",
        })
        assert fields["title"] == "测试事务"
        assert fields["content"] == "内容 @timer=300"
        assert fields["priority"] == TaskPriority.HIGH.value
        assert fields["task_type"] == TaskType.CLI.value
        assert fields["message_id"] == 10
        assert fields["parent_task_id"] == 5
        assert fields["status"] == "running"
        assert fields["small_model"] == 1
        assert fields["is_complex"] == 1
        assert fields["nest"] == "win11"
        assert fields["workdir"] == "/tmp/work"
        assert fields["creator"] == "test_user"

    def test_priority_int_conversion(self, db):
        """priority 应支持 IntEnum 和 int"""
        fields1 = db._extract_create_fields({"priority": TaskPriority.CRITICAL})
        assert fields1["priority"] == 0

        fields2 = db._extract_create_fields({"priority": 3})
        assert fields2["priority"] == 3

    def test_task_type_str_conversion(self, db):
        """task_type 应支持 Enum 和 str"""
        fields1 = db._extract_create_fields({"task_type": TaskType.MESSAGE})
        assert fields1["task_type"] == "message"

        fields2 = db._extract_create_fields({"task_type": "cli"})
        assert fields2["task_type"] == "cli"

    def test_none_to_none_conversion(self, db):
        """message_id/parent_task_id/root_id 为 None 或 falsy 时应转为 None"""
        fields = db._extract_create_fields({"message_id": None, "parent_task_id": None, "root_id": None})
        assert fields["message_id"] is None
        assert fields["parent_task_id"] is None
        assert fields["root_id"] is None

    def test_zero_treated_as_none(self, db):
        """0 值的 message_id/parent_task_id/root_id 应转为 None"""
        fields = db._extract_create_fields({"message_id": 0, "parent_task_id": 0, "root_id": 0})
        assert fields["message_id"] is None
        assert fields["parent_task_id"] is None
        assert fields["root_id"] is None

    def test_metadata_serialized_to_json(self, db):
        """metadata 应被序列化为 JSON 字符串"""
        meta = {"key": "value", "count": 42}
        fields = db._extract_create_fields({"metadata": meta})
        assert fields["metadata"] == json.dumps(meta, ensure_ascii=False)

    def test_metadata_none_becomes_empty_dict_json(self, db):
        """metadata 为 None 时应序列化为 {}"""
        fields = db._extract_create_fields({"metadata": None})
        assert fields["metadata"] == "{}"

    def test_boolean_fields_converted_to_int(self, db):
        """worktree_locked, small_model, is_complex 应转为 int"""
        fields = db._extract_create_fields({
            "worktree_locked": True, "small_model": True, "is_complex": True,
        })
        assert fields["worktree_locked"] == 1
        assert fields["small_model"] == 1
        assert fields["is_complex"] == 1

    def test_timer_none_becomes_zero(self, db):
        """timer 为 None 时应转为 0"""
        fields = db._extract_create_fields({"timer": None})
        assert fields["timer"] == 0

    def test_created_at_generated(self, db):
        """created_at 应自动生成 ISO 格式时间戳"""
        fields = db._extract_create_fields({})
        assert "created_at" in fields
        assert len(fields["created_at"]) > 10  # 至少有个日期


# ==================== _check_root_task_duplicate ====================

class TestCheckRootTaskDuplicate:
    """测试根事务去重检查"""

    def test_no_message_id_returns_none(self, db):
        """无 message_id 时不应检查去重"""
        result = db._check_root_task_duplicate(None)
        assert result is None

    def test_with_parent_task_returns_none(self, db):
        """有 parent_task_id 时不触发去重检查"""
        result = db._check_root_task_duplicate(10, parent_task_id=5)
        assert result is None

    def test_existing_root_task_returns_id(self, db):
        """已有根事务时应返回已有 task_id"""
        with patch.object(db, 'list_tasks', return_value=[{"id": 99}]):
            result = db._check_root_task_duplicate(10)
        assert result == 99

    def test_no_existing_root_task_returns_none(self, db):
        """无已有根事务时应返回 None"""
        with patch.object(db, 'list_tasks', return_value=[]):
            result = db._check_root_task_duplicate(10)
        assert result is None

    def test_calls_list_tasks_correctly(self, db):
        """应正确调用 list_tasks 查询"""
        with patch.object(db, 'list_tasks', return_value=[]) as mock_list:
            db._check_root_task_duplicate(10)
        mock_list.assert_called_once_with(message_id=10, root_only=True)


# ==================== _compute_tree_fields ====================

class TestComputeTreeFields:
    """测试事务树字段计算（root_id, depth, path）"""

    def test_root_task_no_parent(self, db):
        """无父事务时，应保持原始值"""
        fields = {"root_id": None, "depth": 0, "path": "", "parent_task_id": None}
        result = db._compute_tree_fields(fields, None)
        assert result["root_id"] is None
        assert result["depth"] == 0
        assert result["path"] == ""

    def test_child_task_inherits_from_parent(self, db):
        """子事务应从父事务继承 root_id，计算 depth 和 path"""
        parent = {"id": 10, "root_id": 5, "depth": 1, "path": "/5/10"}
        fields = {
            "root_id": None,
            "depth": 0,
            "path": "",
            "parent_task_id": 10,
        }
        result = db._compute_tree_fields(fields, parent)
        assert result["root_id"] == 5
        assert result["depth"] == 2
        # path = parent_path + "/" + parent_task_id = "/5/10/10"
        assert result["path"] == "/5/10/10"

    def test_child_task_explicit_root_id_preserved(self, db):
        """显式指定的 root_id 不应被覆盖"""
        parent = {"id": 10, "root_id": 5, "depth": 1, "path": "/5/10"}
        fields = {
            "root_id": 7,
            "depth": 0,
            "path": "",
            "parent_task_id": 10,
        }
        result = db._compute_tree_fields(fields, parent)
        assert result["root_id"] == 7  # 保留显式值

    def test_child_task_explicit_depth_preserved(self, db):
        """显式指定的 depth 不应被覆盖"""
        parent = {"id": 10, "root_id": 5, "depth": 1, "path": "/5/10"}
        fields = {
            "root_id": None,
            "depth": 3,
            "path": "",
            "parent_task_id": 10,
        }
        result = db._compute_tree_fields(fields, parent)
        assert result["depth"] == 3  # 保留显式值

    def test_child_task_explicit_path_preserved(self, db):
        """显式指定的 path 不应被覆盖"""
        parent = {"id": 10, "root_id": 5, "depth": 1, "path": "/5/10"}
        fields = {
            "root_id": None,
            "depth": 0,
            "path": "/custom/path",
            "parent_task_id": 10,
        }
        result = db._compute_tree_fields(fields, parent)
        assert result["path"] == "/custom/path"  # 保留显式值

    def test_parent_root_id_fallback_to_id(self, db):
        """父事务无 root_id 时应使用父事务 id 作为 root_id"""
        parent = {"id": 10, "root_id": None, "depth": 0, "path": ""}
        fields = {
            "root_id": None,
            "depth": 0,
            "path": "",
            "parent_task_id": 10,
        }
        result = db._compute_tree_fields(fields, parent)
        assert result["root_id"] == 10

    def test_parent_path_empty_generates_simple_path(self, db):
        """父事务 path 为空时，path 为 /parent_task_id"""
        parent = {"id": 10, "root_id": 5, "depth": 0, "path": ""}
        fields = {
            "root_id": None,
            "depth": 0,
            "path": "",
            "parent_task_id": 10,
        }
        result = db._compute_tree_fields(fields, parent)
        # parent_path 为空 → path = "/parent_task_id" = "/10"
        assert result["path"] == "/10"


# ==================== _inherit_nest_and_workdir ====================

class TestInheritNestAndWorkdir:
    """测试 nest 和 workdir 的继承逻辑"""

    def test_already_set_no_inheritance(self, db):
        """已有 nest 和 workdir 时不应覆盖"""
        fields = {"nest": "win11", "workdir": "/tmp/work", "message_id": None, "parent_task_id": None}
        result = db._inherit_nest_and_workdir(fields, None)
        assert result["nest"] == "win11"
        assert result["workdir"] == "/tmp/work"

    @patch("peng.config.loader.load_config")
    def test_inherit_from_config(self, mock_load_config, db):
        """未指定 nest/workdir 时应从 config 继承"""
        mock_config = MagicMock()
        mock_config.webui.nest = "default_nest"
        mock_config.webui.workdir = "/default/work"
        mock_load_config.return_value = mock_config
        fields = {"nest": None, "workdir": None, "message_id": None, "parent_task_id": None}
        result = db._inherit_nest_and_workdir(fields, None)
        assert result["nest"] == "default_nest"
        assert result["workdir"] == "/default/work"

    def test_inherit_nest_from_message(self, db):
        """应从关联消息继承 nest"""
        mock_msg = {"nest": "msg_nest"}
        with patch("peng.message.database.MessageDatabase") as MockMsgDB:
            mock_msg_db = MagicMock()
            mock_msg_db.get_message.return_value = mock_msg
            MockMsgDB.return_value = mock_msg_db

            fields = {"nest": None, "workdir": "/tmp", "message_id": 10, "parent_task_id": None}
            result = db._inherit_nest_and_workdir(fields, None)
            assert result["nest"] == "msg_nest"

    def test_inherit_nest_from_parent(self, db):
        """应从父事务继承 nest"""
        parent = {"id": 5, "nest": "parent_nest"}
        fields = {"nest": None, "workdir": "/tmp", "message_id": None, "parent_task_id": 5}
        result = db._inherit_nest_and_workdir(fields, parent)
        assert result["nest"] == "parent_nest"

    def test_message_nest_priority_over_parent(self, db):
        """消息的 nest 应优先于父事务的 nest"""
        mock_msg = {"nest": "msg_nest"}
        with patch("peng.message.database.MessageDatabase") as MockMsgDB:
            mock_msg_db = MagicMock()
            mock_msg_db.get_message.return_value = mock_msg
            MockMsgDB.return_value = mock_msg_db

            parent = {"id": 5, "nest": "parent_nest"}
            fields = {"nest": None, "workdir": "/tmp", "message_id": 10, "parent_task_id": 5}
            result = db._inherit_nest_and_workdir(fields, parent)
            assert result["nest"] == "msg_nest"  # 消息优先


# ==================== _parse_provider_from_content ====================

class TestParseProviderFromContent:
    """测试从 content 解析 provider"""

    @patch("peng.config.loader.load_config")
    def test_no_provider(self, mock_load_config, db):
        """content 中无 @provider 时返回空字符串"""
        mock_config = MagicMock()
        mock_config.llm.fetch_providers.return_value = []
        mock_load_config.return_value = mock_config
        result = db._parse_provider_from_content("普通内容")
        assert result == ""

    @patch("peng.config.loader.load_config")
    def test_with_provider(self, mock_load_config, db):
        """content 中有 @provider 时应返回第一个 provider"""
        mock_config = MagicMock()
        mock_config.llm.fetch_providers.return_value = ["deepseek", "openai"]
        mock_load_config.return_value = mock_config
        result = db._parse_provider_from_content("请使用 @deepseek 完成")
        assert result == "deepseek"

    def test_empty_content(self, db):
        """content 为空时返回空字符串（不调用 load_config）"""
        result = db._parse_provider_from_content("")
        assert result == ""


# ==================== _insert_task_record ====================

class TestInsertTaskRecord:
    """测试 SQL 插入逻辑"""

    def test_root_task_insert(self, db, mock_conn):
        """根事务插入后应更新 root_id 和 path"""
        fields = _make_insert_fields(parent_task_id=None, root_id=None, id=None)
        with patch.object(db, '_get_connection', return_value=mock_conn):
            task_id = db._insert_task_record(fields)
        assert task_id == 42  # lastrowid
        # 应执行 UPDATE 设置 root_id 和 path
        update_calls = [c for c in mock_conn.execute.call_args_list if "UPDATE" in str(c)]
        assert len(update_calls) == 1
        assert "root_id" in str(update_calls[0])

    def test_child_task_insert(self, db, mock_conn):
        """子事务插入后应更新 path 为正确路径"""
        fields = _make_insert_fields(
            parent_task_id=10,
            root_id=5,
            depth=2,
            path="/5/10",  # 父事务的 path
            id=None,
        )
        with patch.object(db, '_get_connection', return_value=mock_conn):
            task_id = db._insert_task_record(fields)
        assert task_id == 42
        # 应执行 UPDATE 设置正确的 path
        update_calls = [c for c in mock_conn.execute.call_args_list if "UPDATE" in str(c)]
        assert len(update_calls) == 1
        # path 应为 /5/10/42
        assert "/5/10/42" in str(update_calls[0])

    def test_specified_id_used(self, db, mock_conn):
        """指定 id 时应使用指定的 id"""
        fields = _make_insert_fields(id=100, parent_task_id=None, root_id=None)
        with patch.object(db, '_get_connection', return_value=mock_conn):
            task_id = db._insert_task_record(fields)
        assert task_id == 100


# ==================== create_task 集成测试 ====================

class TestCreateTaskIntegration:
    """测试 create_task 整体流程的行为等价性"""

    def test_duplicate_root_task_returns_existing(self, db, mock_conn):
        """同一消息的重复根事务应返回已有 ID"""
        with patch.object(db, 'list_tasks', return_value=[{"id": 99}]):
            task_id = db.create_task(message_id=10, title="重复")
        assert task_id == 99

    @patch("peng.config.loader.load_config")
    def test_create_simple_root_task(self, mock_load_config, db, mock_conn):
        """创建简单的根事务"""
        mock_config = MagicMock()
        mock_config.llm.fetch_providers.return_value = []
        mock_config.webui.nest = "test_nest"
        mock_config.webui.workdir = "/test/work"
        mock_load_config.return_value = mock_config

        with patch.object(db, '_get_connection', return_value=mock_conn):
            with patch.object(db, 'list_tasks', return_value=[]):
                task_id = db.create_task(title="测试事务", content="内容")

        assert task_id == 42
        # 验证 INSERT 被调用
        insert_calls = [c for c in mock_conn.execute.call_args_list if "INSERT" in str(c)]
        assert len(insert_calls) == 1

    @patch("peng.config.loader.load_config")
    def test_child_task_fetches_parent(self, mock_load_config, db, mock_conn):
        """创建子事务应查询父事务"""
        mock_config = MagicMock()
        mock_config.llm.fetch_providers.return_value = []
        mock_config.webui.nest = ""
        mock_config.webui.workdir = ""
        mock_load_config.return_value = mock_config

        parent = {
            "id": 10, "root_id": 5, "depth": 1, "path": "/5/10",
            "message_id": 3, "nest": "parent_nest",
        }

        with patch.object(db, 'get_task', return_value=parent) as mock_get:
            with patch.object(db, '_get_connection', return_value=mock_conn):
                task_id = db.create_task(
                    title="子事务", content="", parent_task_id=10
                )

        assert task_id == 42
        # 应调用 get_task 获取父事务信息和根事务信息
        # get_task 会按顺序调用：parent_task_id=10, root_id=5（继承message_id）
        calls = mock_get.call_args_list
        assert call(10) in calls  # 查询父事务
        assert call(5) in calls   # 查询根事务（继承message_id）

    @patch("peng.config.loader.load_config")
    def test_create_task_with_all_params(self, mock_load_config, db, mock_conn):
        """创建带完整参数的事务"""
        mock_config = MagicMock()
        mock_config.llm.fetch_providers.return_value = ["deepseek"]
        mock_config.webui.nest = ""
        mock_config.webui.workdir = ""
        mock_load_config.return_value = mock_config

        with patch.object(db, '_get_connection', return_value=mock_conn):
            with patch.object(db, 'list_tasks', return_value=[]):
                task_id = db.create_task(
                    title="完整测试",
                    content="使用 @deepseek 执行 @timer=600",
                    priority=TaskPriority.HIGH,
                    task_type=TaskType.CLI,
                    message_id=10,
                    caller="test",
                    caller_session_id="sess_001",
                    timeout=300,
                    metadata={"key": "val"},
                    creator="user1",
                    prompt="default_prompt",
                    workdir="/work",
                    nest="win11",
                )

        assert task_id == 42

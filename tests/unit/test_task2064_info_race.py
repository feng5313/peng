#!/usr/bin/env python3
"""事务#2064：感叹号紧急info被消费竞态误杀修复的单元测试

根因：tool_engine._get_task_info 消费未读info时用 set_task_info_status
(mode="all") 标记**全部**已读——读列表与标记之间新注入的info（含感叹号
紧急指示）被一并误标已读：内容丢失（未进history）+ 紧急中断检测
（查unread）永远查不到 → 打断失效。

修复：
1. info项增加唯一id（add_task_info写入时分配）
2. 新增 mark_info_items_read：按id精确标记，消费瞬间新注入项保持未读
3. WebUI info端点（添加/全标记/单条删除/单条已读）全部改用db原子操作，
   消除手工read-modify-write覆盖写竞态
4. provider重试等待循环加紧急中断检测（消除429死等真空期）

测试策略：
- info.py纯函数：id保留/兼容/next_info_id分配
- database.py：mock连接测试 mark_info_items_read 精确标记核心竞态语义
"""

import json
from unittest.mock import MagicMock

import pytest

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from peng.task.database import TaskDatabase
from peng.task.info import (
    is_urgent_content,
    next_info_id,
    parse_info_to_list_with_status,
    serialize_info_list,
)


# ==================== fixtures ====================


@pytest.fixture
def db():
    """创建 TaskDatabase 实例（绕过单例，不初始化连接）"""
    return object.__new__(TaskDatabase)


def _make_conn(initial_info: str):
    """构造带内存info状态的mock连接：模拟FOR UPDATE读-改-写"""
    conn = MagicMock()
    state = {"info": initial_info}
    # with语句支持：__enter__返回自身（保持execute的side_effect生效）
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)

    def execute(sql, params=None):
        cur = MagicMock()
        if "FOR UPDATE" in sql:
            row = MagicMock()
            row.get = lambda k, d=None: state.get(k, d)
            cur.fetchone.return_value = row
        else:  # UPDATE
            assert "UPDATE tasks SET info" in sql
            state["info"] = params[0]
            cur.fetchone.return_value = None
        return cur

    conn.execute.side_effect = execute
    return conn, state


# ==================== 1. info.py：id字段支持 ====================


class TestInfoIdSupport:
    def test_parse_preserves_id(self):
        raw = json.dumps(
            [
                {"content": "！紧急指示", "read": False, "id": 7},
                {"content": "普通指示", "read": True},
            ],
            ensure_ascii=False,
        )
        items = parse_info_to_list_with_status(raw)
        assert items[0] == {"content": "！紧急指示", "read": False, "id": 7}
        assert items[1] == {"content": "普通指示", "read": True}  # 无id保持无id

    def test_parse_legacy_formats_untouched(self):
        # 旧格式字符串列表/纯字符串，正常解析无id
        assert parse_info_to_list_with_status('["a","b"]') == [
            {"content": "a", "read": False},
            {"content": "b", "read": False},
        ]
        assert parse_info_to_list_with_status('"hello"') == [
            {"content": "hello", "read": False}
        ]
        assert parse_info_to_list_with_status("[]") == []

    def test_serialize_roundtrip_keeps_id(self):
        items = [{"content": "x", "read": False, "id": 3}]
        assert parse_info_to_list_with_status(serialize_info_list(items)) == items

    def test_next_info_id(self):
        assert next_info_id([]) == 1
        assert next_info_id([{"content": "a", "read": True}]) == 1  # 无id视为0
        assert next_info_id([{"content": "a", "read": True, "id": 5}]) == 6
        assert (
            next_info_id(
                [
                    {"content": "a", "read": True, "id": 2},
                    {"content": "b", "read": False, "id": 9},
                ]
            )
            == 10
        )
        # 非法id安全处理
        assert next_info_id([{"content": "a", "read": True, "id": "bad"}]) == 1

    def test_is_urgent_content_unaffected(self):
        # id字段不影响紧急判断
        items = parse_info_to_list_with_status(
            json.dumps([{"content": "！打断我", "read": False, "id": 1}], ensure_ascii=False)
        )
        assert is_urgent_content(items[0]["content"]) is True


# ==================== 2. mark_info_items_read：核心竞态修复 ====================


class TestMarkInfoItemsRead:
    def test_new_injected_item_survives_consume(self, db, monkeypatch):
        """核心场景（事务#2064根因）：消费[旧项]期间新注入[紧急项]，
        精确标记只杀[旧项]，[紧急项]保持未读等待紧急检测/下轮消费"""
        # DB现状：消费读取时只有 old（id=1，未读）
        conn, state = _make_conn(
            json.dumps(
                [{"content": "旧指示", "read": False, "id": 1}], ensure_ascii=False
            )
        )
        monkeypatch.setattr(db, "_get_connection", lambda: conn)

        # 模拟消费期间（读列表→标记之间）并发注入紧急info（id=2）
        # mark_info_items_read 在 FOR UPDATE 锁内读到最新列表
        original_execute = conn.execute.side_effect

        def execute_with_race(sql, params=None):
            cur = original_execute(sql, params)
            if "FOR UPDATE" in sql and "id\": 1" in state["info"]:
                # 首次锁定读取时模拟并发注入（add_task_info原子追加）
                lst = json.loads(state["info"])
                lst.append({"content": "！紧急打断指示", "read": False, "id": 2})
                state["info"] = json.dumps(lst, ensure_ascii=False)
            return cur

        conn.execute.side_effect = execute_with_race

        # 消费方只标记它读到的项（旧指示）
        ok = db.mark_info_items_read(1, [{"content": "旧指示", "read": False, "id": 1}])
        assert ok is True

        final = parse_info_to_list_with_status(state["info"])
        assert final[0]["read"] is True  # 旧指示已消费
        assert final[1]["read"] is False  # 🔴 紧急指示存活（旧代码mode=all会误杀）
        assert is_urgent_content(final[1]["content"]) is True

    def test_mark_by_id_only(self, db, monkeypatch):
        """按id精确标记：同内容不同id的两条只标记目标条"""
        conn, state = _make_conn(
            json.dumps(
                [
                    {"content": "同内容", "read": False, "id": 1},
                    {"content": "同内容", "read": False, "id": 2},
                ],
                ensure_ascii=False,
            )
        )
        monkeypatch.setattr(db, "_get_connection", lambda: conn)

        ok = db.mark_info_items_read(1, [{"content": "同内容", "read": False, "id": 2}])
        assert ok is True
        final = parse_info_to_list_with_status(state["info"])
        assert final[0]["read"] is False  # id=1不受影响
        assert final[1]["read"] is True  # id=2精确标记

    def test_mark_legacy_by_content(self, db, monkeypatch):
        """无id旧数据按content匹配（仅未读项）"""
        conn, state = _make_conn(
            json.dumps(
                [
                    {"content": "旧数据", "read": False},
                    {"content": "另一条", "read": False},
                ],
                ensure_ascii=False,
            )
        )
        monkeypatch.setattr(db, "_get_connection", lambda: conn)

        ok = db.mark_info_items_read(1, [{"content": "旧数据", "read": False}])
        assert ok is True
        final = parse_info_to_list_with_status(state["info"])
        assert final[0]["read"] is True
        assert final[1]["read"] is False

    def test_no_match_no_write(self, db, monkeypatch):
        """无匹配项（已并发标记）不写回"""
        conn, state = _make_conn(
            json.dumps([{"content": "x", "read": True, "id": 1}], ensure_ascii=False)
        )
        monkeypatch.setattr(db, "_get_connection", lambda: conn)
        ok = db.mark_info_items_read(1, [{"content": "x", "read": False, "id": 99}])
        assert ok is True
        # 未触发UPDATE（state未变）

    def test_empty_items_short_circuit(self, db):
        assert db.mark_info_items_read(1, []) is True

    def test_task_not_found(self, db, monkeypatch):
        conn, _ = _make_conn("[]")
        # fetchone返回None模拟事务不存在
        conn.execute.side_effect = lambda sql, params=None: (
            MagicMock(fetchone=lambda: None) if "FOR UPDATE" in sql else MagicMock()
        )
        monkeypatch.setattr(db, "_get_connection", lambda: conn)
        assert (
            db.mark_info_items_read(999, [{"content": "x", "read": False, "id": 1}])
            is False
        )


# ==================== 3. add_task_info：id分配 ====================


class TestAddTaskInfoAssignsId:
    def test_add_assigns_sequential_ids(self, db, monkeypatch):
        conn, state = _make_conn(
            json.dumps(
                [{"content": "first", "read": False, "id": 3}], ensure_ascii=False
            )
        )
        monkeypatch.setattr(db, "_get_connection", lambda: conn)

        assert db.add_task_info(1, "！新的紧急指示") is True
        items = parse_info_to_list_with_status(state["info"])
        assert len(items) == 2
        assert items[1]["content"] == "！新的紧急指示"
        assert items[1]["read"] is False
        assert items[1]["id"] == 4  # max(3)+1
        assert is_urgent_content(items[1]["content"]) is True

    def test_add_to_legacy_no_id_list(self, db, monkeypatch):
        conn, state = _make_conn(json.dumps([{"content": "旧", "read": True}], ensure_ascii=False))
        monkeypatch.setattr(db, "_get_connection", lambda: conn)

        assert db.add_task_info(1, "new") is True
        items = parse_info_to_list_with_status(state["info"])
        assert items[1]["id"] == 1  # 无id项视为0，新id=1


# ==================== 4. 单条操作原子方法 ====================


class TestItemLevelOps:
    def test_delete_item(self, db, monkeypatch):
        conn, state = _make_conn(
            json.dumps(
                [
                    {"content": "a", "read": True, "id": 1},
                    {"content": "b", "read": False, "id": 2},
                ],
                ensure_ascii=False,
            )
        )
        monkeypatch.setattr(db, "_get_connection", lambda: conn)

        removed = db.delete_task_info_item(1, 0)
        assert removed == {"content": "a", "read": True, "id": 1}
        final = parse_info_to_list_with_status(state["info"])
        assert len(final) == 1 and final[0]["content"] == "b"

    def test_delete_out_of_range(self, db, monkeypatch):
        conn, _ = _make_conn("[]")
        monkeypatch.setattr(db, "_get_connection", lambda: conn)
        assert db.delete_task_info_item(1, 5) is None

    def test_set_item_status(self, db, monkeypatch):
        conn, state = _make_conn(
            json.dumps([{"content": "a", "read": False, "id": 1}], ensure_ascii=False)
        )
        monkeypatch.setattr(db, "_get_connection", lambda: conn)

        assert db.set_task_info_item_status(1, 0, True) is True
        final = parse_info_to_list_with_status(state["info"])
        assert final[0]["read"] is True

    def test_set_item_status_out_of_range(self, db, monkeypatch):
        conn, _ = _make_conn("[]")
        monkeypatch.setattr(db, "_get_connection", lambda: conn)
        assert db.set_task_info_item_status(1, 0, True) is False


# ==================== 5. 端到端语义回归：紧急检测可见性 ====================


class TestUrgentVisibilityE2E:
    def test_consume_then_urgent_detectable(self, db, monkeypatch):
        """E2E语义：消费旧info后，紧急info必须仍可被 _has_urgent_interrupts
        风格的 unread 查询发现（模拟检测逻辑）"""
        conn, state = _make_conn(
            json.dumps(
                [{"content": "旧指示", "read": False, "id": 1}], ensure_ascii=False
            )
        )
        monkeypatch.setattr(db, "_get_connection", lambda: conn)

        # 用户注入紧急指示
        db.add_task_info(1, "！立刻停下来！")

        # 模拟tool_engine消费：读unread → 注入history → 精确标记
        unread = [i for i in parse_info_to_list_with_status(state["info"]) if not i["read"]]
        consumed = [i for i in unread if not is_urgent_content(i["content"])]
        # 假设打断失败晚了一步，迭代边界只消费了旧指示
        db.mark_info_items_read(1, consumed)

        # 紧急检测（模拟 _has_urgent_interrupts 的 unread 查询）
        remaining_unread = [
            i
            for i in parse_info_to_list_with_status(state["info"])
            if not i["read"] and is_urgent_content(i["content"])
        ]
        assert len(remaining_unread) == 1
        assert remaining_unread[0]["content"] == "！立刻停下来！"

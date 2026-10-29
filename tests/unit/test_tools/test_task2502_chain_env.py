"""
事务#2502：message工具彻底禁止发送根消息 + 回复消息nest/workdir自动与根消息（链首）一致

三层防线测试（工具层用例见 test_message_info_sender.py::TestMessageToolNoRootMessage）：
1. CLI注入层 _inject_nest_workdir_defaults / bin/msg：只有 send（含默认子命令）注入
   默认执行环境；reply 不再被误当"默认子命令"注入本机 cwd/self-nest
   （历史数据 223条nest、197条workdir 与链首不一致的根因）
2. bin/msg 与 _msg_impl 的注入集合一致性（防止两处逻辑漂移）
3. 数据库层 create_message：回复消息 nest/workdir 无条件跟随链首
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from peng.cli._msg_impl import _inject_nest_workdir_defaults
from peng.config import get_project_root

# ==================== CLI注入层 ====================


@pytest.fixture(autouse=True)
def _fake_nest_env(monkeypatch):
    """固定 nest 身份探测结果，隔离真实 nest/服务状态"""
    monkeypatch.setattr("peng.cli.remote.get_self_nest_name", lambda: "epyc")
    monkeypatch.setattr("peng.cli.remote.is_local_service_running", lambda: True)
    monkeypatch.setattr("peng.cli.remote.get_default_nest_name", lambda: None)


class TestCliInjection:
    def test_send_injects_default_env(self):
        """send（新任务/链首）注入本机 workdir 与 self nest"""
        args = _inject_nest_workdir_defaults(["send", "帮我截屏"])
        assert args[args.index("--nest") + 1] == "epyc"
        assert args[args.index("--workdir") + 1] == os.getcwd()

    def test_reply_not_injected(self):
        """reply 不注入默认执行环境——由数据库层从链首继承（事务#2502修复）"""
        args = _inject_nest_workdir_defaults(["reply", "9302", "已完成"])
        assert "--nest" not in args and "--workdir" not in args
        assert "-n" not in args and "-w" not in args

    def test_reply_keeps_explicit_env_without_duplicate(self):
        """reply 显式带 --nest 时不得追加第二个（重复注入）"""
        args = _inject_nest_workdir_defaults(["reply", "9302", "x", "--nest", "wen"])
        assert args.count("--nest") == 1

    def test_readonly_subcommands_not_injected(self):
        for sub in ("trees", "show", "context", "search", "status"):
            args = _inject_nest_workdir_defaults([sub, "12"])
            assert "--nest" not in args and "--workdir" not in args, sub

    def test_legacy_default_subcommand_still_injected(self):
        """首参数非已知子命令（旧版 msg <内容> = send）保持注入"""
        args = _inject_nest_workdir_defaults(["帮我截屏"])
        assert "--nest" in args and "--workdir" in args


def _extract_subcmd_items(block: str) -> set[str]:
    """从源码块提取子命令集合：只接受ASCII小写词（滤除注释中的引号词）"""
    return {x for x in re.findall(r'"([^"]+)"', block) if re.fullmatch(r"[a-z][a-z0-9-]*", x)}


class TestBinMsgInjectionConsistency:
    """bin/msg 内联注入逻辑与 _msg_impl 必须一致（两处各自维护集合，防漂移）"""

    def _bin_msg_set(self) -> set[str]:
        path = Path(get_project_root()) / "bin" / "msg"
        src = path.read_text(encoding="utf-8")
        m = re.search(r"_msg_all_subcommands\s*=\s*\{(.*?)\}", src, re.S)
        assert m, "bin/msg 未找到 _msg_all_subcommands"
        return _extract_subcmd_items(m.group(1))

    def _msg_impl_set(self) -> set[str]:
        path = (
            Path(get_project_root()) / "src" / "peng" / "cli" / "_msg_impl.py"
        )
        src = path.read_text(encoding="utf-8")
        m = re.search(
            r"def _inject_nest_workdir_defaults.*?_msg_all_subcommands\s*=\s*\{(.*?)\}",
            src,
            re.S,
        )
        assert m, "_msg_impl 未找到 _msg_all_subcommands"
        return _extract_subcmd_items(m.group(1))

    def test_reply_excluded_from_injection(self):
        for name in (self._bin_msg_set(), self._msg_impl_set()):
            assert "reply" in name, "reply必须在注入排除集合内（链首继承）"

    def test_sets_identical(self):
        assert self._bin_msg_set() == self._msg_impl_set()


class TestSubcmdWhitelistConsistency:
    """事务#2502：message工具子命令白名单须与CLI两处子命令清单一致（防漂移）——
    漏项意味着该子命令会落进"默认子命令send"路径（发送根消息）"""

    def test_whitelist_matches_cli_subcommands(self):
        from peng.tools.message import _MSG_SUBCMDS

        bin_set = TestBinMsgInjectionConsistency()._bin_msg_set() | {"send"}
        assert _MSG_SUBCMDS == bin_set, "白名单须等于 bin/msg 子命令集合+send"

        impl = Path(get_project_root()) / "src" / "peng" / "cli" / "_msg_impl.py"
        src = impl.read_text(encoding="utf-8")
        m = re.search(r"def parse_legacy_args.*?subcommands\s*=\s*\[(.*?)\]", src, re.S)
        assert m, "parse_legacy_args 未找到 subcommands 列表"
        legacy = _extract_subcmd_items(m.group(1))
        assert _MSG_SUBCMDS == legacy, "白名单须等于 parse_legacy_args 的子命令列表"


# ==================== 数据库层：回复消息跟随链首 ====================


class _FakeCursor:
    lastrowid = 4321

    def execute(self, sql, params=None):
        return None


class _FakeConn:
    def __init__(self, sink):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self._sink.append((" ".join(sql.split())[:30], params))
        return _FakeCursor()


# INSERT 参数顺序（不带显式id分支）：
# 0 timestamp,1 content,2 status,3 now,4 reply_to,5 chain_head_id,6 caller,
# 7 caller_session_id,8 is_chain_head,9 timeout,10 sender,11 receiver,
# 12 workdir,13 nest,...
IDX_WORKDIR = 12
IDX_NEST = 13


@pytest.fixture
def db(monkeypatch):
    try:
        from peng.message.database import MessageDatabase

        instance = MessageDatabase()
    except Exception as exc:  # pragma: no cover - 无DB环境跳过
        pytest.skip(f"数据库不可用: {exc}")
    monkeypatch.setattr(instance, "find_similar_message", lambda *a, **k: None)
    monkeypatch.setattr(instance, "_notify_webui", lambda *a, **k: None)
    return instance


def _wire_chain(monkeypatch, db, msgs: dict):
    sink: list = []
    monkeypatch.setattr(db, "get_message", lambda mid: msgs.get(mid))
    monkeypatch.setattr(db, "_get_connection", lambda: _FakeConn(sink))
    return sink


def _insert_params(sink: list) -> tuple:
    inserts = [params for sql, params in sink if params is not None]
    assert inserts, f"未捕获INSERT: {sink}"
    return inserts[0]


class TestReplyFollowsChainHead:
    def test_explicit_env_overridden_by_chain_head(self, monkeypatch, db):
        msgs = {
            9302: {"id": 9302, "nest": "epyc", "workdir": "D:/work/fcc", "is_chain_head": 1},
            9303: {
                "id": 9303,
                "reply_to": 9302,
                "chain_head_id": 9302,
                "nest": "wen",
                "workdir": "/x",
                "is_chain_head": 0,
            },
            4321: {"id": 4321, "sender": "peng", "receiver": "master"},
        }
        sink = _wire_chain(monkeypatch, db, msgs)
        db.create_message(
            content="测试回复内容",
            sender="peng",
            receiver="master",
            reply_to=9303,
            nest="local",
            workdir="/mnt/p/feng/work/peng",
        )
        params = _insert_params(sink)
        assert params[IDX_NEST] == "epyc"
        assert params[IDX_WORKDIR] == "D:/work/fcc"

    def test_reply_directly_to_chain_head(self, monkeypatch, db):
        msgs = {
            9302: {"id": 9302, "nest": "wen", "workdir": "D:/AI/Strata", "is_chain_head": 1},
            4321: {"id": 4321, "sender": "peng", "receiver": "master"},
        }
        sink = _wire_chain(monkeypatch, db, msgs)
        db.create_message(
            content="直接回复链首", sender="peng", receiver="master", reply_to=9302
        )
        params = _insert_params(sink)
        assert params[IDX_NEST] == "wen"
        assert params[IDX_WORKDIR] == "D:/AI/Strata"

    def test_content_at_annotation_loses_to_chain_head(self, monkeypatch, db):
        msgs = {
            9302: {"id": 9302, "nest": "epyc", "workdir": "D:/work/fcc", "is_chain_head": 1},
            4321: {"id": 4321, "sender": "peng", "receiver": "master"},
        }
        sink = _wire_chain(monkeypatch, db, msgs)
        db.create_message(
            content="内容带@workdir=/tmp/xyz与@nest=wen标注",
            sender="peng",
            receiver="master",
            reply_to=9302,
        )
        params = _insert_params(sink)
        assert params[IDX_NEST] == "epyc"
        assert params[IDX_WORKDIR] == "D:/work/fcc"

    def test_empty_chain_head_falls_back_to_parent_message(self, monkeypatch, db):
        msgs = {
            9302: {"id": 9302, "nest": "", "workdir": "", "is_chain_head": 1},
            9303: {
                "id": 9303,
                "reply_to": 9302,
                "chain_head_id": 9302,
                "nest": "orig-nest",
                "workdir": "/orig/wd",
                "is_chain_head": 0,
            },
            4321: {"id": 4321, "sender": "peng", "receiver": "master"},
        }
        sink = _wire_chain(monkeypatch, db, msgs)
        db.create_message(content="回退测试", sender="peng", receiver="master", reply_to=9303)
        params = _insert_params(sink)
        assert params[IDX_NEST] == "orig-nest"
        assert params[IDX_WORKDIR] == "/orig/wd"

    def test_chain_head_creation_keeps_caller_env(self, monkeypatch, db):
        """链首（新任务）消息不受影响：使用调用方/内容解析的环境"""
        msgs = {4321: {"id": 4321, "sender": "peng", "receiver": "master"}}
        sink = _wire_chain(monkeypatch, db, msgs)
        db.create_message(
            content="新任务", sender="master", receiver="peng", nest="epyc", workdir="D:/work/fcc"
        )
        params = _insert_params(sink)
        assert params[IDX_NEST] == "epyc"
        assert params[IDX_WORKDIR] == "D:/work/fcc"

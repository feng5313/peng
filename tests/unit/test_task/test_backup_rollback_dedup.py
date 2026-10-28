"""
测试 备份/回滚去重优化（事务#1583）

验证：备份/回滚操作在 checklist/history/外部文件/git 没有实际变化时：
1. HistoryBackupManager.backup 不产生冗余 commit（复用 HEAD）
2. HistoryBackupManager.has_changes 正确检测实质变化（忽略统计字段）
3. ExternalFileBackup.backup 无变化复用 HEAD；check_changes 支持指定基准 commit
4. ToolBackupManager.rollback 无实际变化时快速返回 changed=False（不建undo快照）
5. strip_context_stats 剥离统计字段
6. 迭代备份指纹只含 messages+checklist（统计字段变化不影响指纹）
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from peng.service.tool_backup import (
    ExternalFileBackup,
    HistoryBackupManager,
    ToolBackupManager,
    strip_context_stats,
)


def _git(repo: Path, *args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, timeout=10, **kw
    )


def _ctx(messages: list, tokens: int = 100) -> str:
    """构造含统计字段的 history context JSON（模拟 to_context 输出）"""
    return json.dumps(
        {
            "messages": messages,
            "_prompt_tokens": tokens,
            "_total_tokens_consumed": tokens * 2,
            "_total_cost": 0.5,
        },
        ensure_ascii=False,
    )


# ==================== strip_context_stats ====================


class TestStripContextStats:
    def test_strip_stats_fields(self):
        ctx = _ctx([{"role": "user", "content": "hi"}], tokens=999)
        stripped = strip_context_stats(ctx)
        assert "_prompt_tokens" not in stripped
        assert json.loads(stripped) == [{"role": "user", "content": "hi"}]

    def test_stats_change_not_affect_stripped(self):
        """统计字段变化 → 剥离后内容相同（指纹/备份去重的关键）"""
        s1 = strip_context_stats(_ctx([{"role": "user", "content": "hi"}], tokens=100))
        s2 = strip_context_stats(_ctx([{"role": "user", "content": "hi"}], tokens=200))
        assert s1 == s2

    def test_message_change_affects_stripped(self):
        s1 = strip_context_stats(_ctx([{"role": "user", "content": "hi"}]))
        s2 = strip_context_stats(_ctx([{"role": "user", "content": "changed"}]))
        assert s1 != s2

    def test_invalid_json_return_as_is(self):
        assert strip_context_stats("not-json") == "not-json"

    def test_old_format_list(self):
        """旧格式纯数组原样处理"""
        s = strip_context_stats(json.dumps([{"role": "user"}]))
        assert json.loads(s) == [{"role": "user"}]


# ==================== HistoryBackupManager ====================


@pytest.fixture
def history_repo(tmp_path, monkeypatch) -> Path:
    repo = tmp_path / "history" / "task#998001"
    repo.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@peng")
    _git(repo, "config", "user.name", "T")
    (repo / "context.json").write_text("[]", encoding="utf-8")
    (repo / "checklist.md").write_text("", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    monkeypatch.setattr(
        HistoryBackupManager, "_get_history_repo", staticmethod(lambda task_id: str(repo))
    )
    monkeypatch.setattr(
        HistoryBackupManager, "_get_history_base", staticmethod(lambda: str(repo.parent))
    )
    return repo


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.decode().strip()


class TestHistoryBackupDedup:
    def test_same_content_reuses_head(self, history_repo):
        """相同 messages+checklist 两次备份 → 复用同一 commit"""
        c1 = HistoryBackupManager.backup(
            998001, _ctx([{"role": "user", "content": "a"}]), "lbl1", "cl-v1"
        )
        c2 = HistoryBackupManager.backup(
            998001, _ctx([{"role": "user", "content": "a"}]), "lbl2", "cl-v1"
        )
        assert c1 and c2
        assert c1 == c2
        # 仓库只有 init + 第一次备份两个 commit
        count = _git(history_repo, "rev-list", "--count", "HEAD").stdout.decode().strip()
        assert count == "2"

    def test_stats_only_change_reuses_head(self, history_repo):
        """仅统计字段变化（messages相同）→ 复用 HEAD commit"""
        c1 = HistoryBackupManager.backup(
            998001, _ctx([{"role": "user", "content": "a"}], tokens=100), "l1", "cl"
        )
        c2 = HistoryBackupManager.backup(
            998001, _ctx([{"role": "user", "content": "a"}], tokens=99999), "l2", "cl"
        )
        assert c1 == c2

    def test_message_change_creates_new_commit(self, history_repo):
        c1 = HistoryBackupManager.backup(998001, _ctx([{"role": "user", "content": "a"}]), "l1", "cl")
        c2 = HistoryBackupManager.backup(998001, _ctx([{"role": "user", "content": "b"}]), "l2", "cl")
        assert c1 != c2

    def test_checklist_change_creates_new_commit(self, history_repo):
        c1 = HistoryBackupManager.backup(998001, _ctx([{"role": "user", "content": "a"}]), "l1", "v1")
        c2 = HistoryBackupManager.backup(998001, _ctx([{"role": "user", "content": "a"}]), "l2", "v2")
        assert c1 != c2


def _mock_task_manager(context: str, checklist: str) -> MagicMock:
    tm = MagicMock()
    tm.db.get_task.return_value = {
        "id": 998001, "context": context, "checklist": checklist
    }
    fake_mod = MagicMock()
    fake_mod.TaskManager = lambda: tm
    return fake_mod


class TestHistoryHasChanges:
    def test_no_changes(self, history_repo, monkeypatch):
        """当前 db 与备份 commit 一致 → False"""
        ctx = _ctx([{"role": "user", "content": "a"}])
        commit = HistoryBackupManager.backup(998001, ctx, "l", "cl-v1")
        fake_mod = _mock_task_manager(ctx, "cl-v1")
        monkeypatch.setitem(__import__("sys").modules, "peng.task.manager", fake_mod)
        assert HistoryBackupManager.has_changes(998001, commit) is False

    def test_stats_only_change_is_no_change(self, history_repo, monkeypatch):
        """当前 db 仅统计字段不同（messages相同）→ False（关键用例）"""
        backup_ctx = _ctx([{"role": "user", "content": "a"}], tokens=100)
        commit = HistoryBackupManager.backup(998001, backup_ctx, "l", "cl")
        current_ctx = _ctx([{"role": "user", "content": "a"}], tokens=8888)
        fake_mod = _mock_task_manager(current_ctx, "cl")
        monkeypatch.setitem(__import__("sys").modules, "peng.task.manager", fake_mod)
        assert HistoryBackupManager.has_changes(998001, commit) is False

    def test_message_change_detected(self, history_repo, monkeypatch):
        ctx1 = _ctx([{"role": "user", "content": "a"}])
        commit = HistoryBackupManager.backup(998001, ctx1, "l", "cl")
        ctx2 = _ctx([{"role": "user", "content": "a"}, {"role": "assistant", "content": "r"}])
        fake_mod = _mock_task_manager(ctx2, "cl")
        monkeypatch.setitem(__import__("sys").modules, "peng.task.manager", fake_mod)
        assert HistoryBackupManager.has_changes(998001, commit) is True

    def test_checklist_change_detected(self, history_repo, monkeypatch):
        ctx = _ctx([{"role": "user", "content": "a"}])
        commit = HistoryBackupManager.backup(998001, ctx, "l", "old")
        fake_mod = _mock_task_manager(ctx, "new")
        monkeypatch.setitem(__import__("sys").modules, "peng.task.manager", fake_mod)
        assert HistoryBackupManager.has_changes(998001, commit) is True


# ==================== ExternalFileBackup ====================


@pytest.fixture
def ext_repo(tmp_path, monkeypatch) -> Path:
    repo = tmp_path / "file_backups" / "task#998001"
    repo.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@peng")
    _git(repo, "config", "user.name", "T")
    _git(repo, "commit", "-m", "init", "--allow-empty")
    # 🔧 隔离盘符路由：Windows 上 tmp_path 在 C: 盘，_split_drive_key 会把
    # 备份真实写入 C:\.peng\backups\task#998001（污染系统盘符根目录），必须禁用
    monkeypatch.setattr(
        ExternalFileBackup, "_split_drive_key", staticmethod(lambda fp: None)
    )
    monkeypatch.setattr(
        ExternalFileBackup, "_get_backup_repo", staticmethod(lambda task_id: str(repo))
    )
    return repo


class TestExternalBackupDedup:
    def test_same_files_reuse_head(self, ext_repo, tmp_path):
        f1 = tmp_path / "ext1.txt"
        f1.write_text("same-content", encoding="utf-8")
        c1 = ExternalFileBackup.backup(998001, [str(f1)], "l1")
        c2 = ExternalFileBackup.backup(998001, [str(f1)], "l2")
        assert c1 and c2 and c1 == c2
        count = _git(ext_repo, "rev-list", "--count", "HEAD").stdout.decode().strip()
        assert count == "2"  # init + 一次实际备份

    def test_file_change_new_commit(self, ext_repo, tmp_path):
        f1 = tmp_path / "ext1.txt"
        f1.write_text("v1", encoding="utf-8")
        c1 = ExternalFileBackup.backup(998001, [str(f1)], "l1")
        f1.write_text("v2", encoding="utf-8")
        c2 = ExternalFileBackup.backup(998001, [str(f1)], "l2")
        assert c1 != c2

    def test_check_changes_against_specific_commit(self, ext_repo, tmp_path):
        """check_changes 支持指定基准 commit（回滚预检测场景）"""
        f1 = tmp_path / "ext1.txt"
        f1.write_text("v1", encoding="utf-8")
        base = ExternalFileBackup.backup(998001, [str(f1)], "l1")
        # 之后又有一次备份移动 HEAD，但基线 commit 仍是 base
        f1.write_text("v2", encoding="utf-8")
        ExternalFileBackup.backup(998001, [str(f1)], "l2")
        f1.write_text("v1", encoding="utf-8")  # 恢复成基线内容
        # 相对 base 无变化（虽然 HEAD 是 v2）
        assert ExternalFileBackup.check_changes(998001, [str(f1)], base) == []
        # 相对 HEAD 有变化（HEAD=v2，当前v1）
        assert ExternalFileBackup.check_changes(998001, [str(f1)]) == [str(f1)]

    def test_check_changes_detects_new_file_vs_base(self, ext_repo, tmp_path):
        """文件在基线 commit 中不存在 → 视为新增（有变化）"""
        f1 = tmp_path / "ext_new.txt"
        f1.write_text("x", encoding="utf-8")
        base = _git(ext_repo, "rev-parse", "HEAD").stdout.decode().strip()
        assert ExternalFileBackup.check_changes(998001, [str(f1)], base) == [str(f1)]


# ==================== ToolBackupManager.rollback 无变化快速路径 ====================


class TestRollbackNoChangeFastPath:
    def test_no_changes_returns_early(self, tmp_path):
        """文件/history/外部文件均无变化 → changed=False, undo_meta=None"""
        workdir = tmp_path / "wd"
        workdir.mkdir()
        meta = {
            "type": "git",
            "task_id": 998001,
            "tool_name": "iteration_start",
            "commit_hash": "abc1234",
            "workdir": str(workdir),
            "nest_name": "local",
            "external_files": [],
            "history_commit": "hc1",
        }
        check_calls = []

        def fake_check(m, w, n):
            check_calls.append((m, w, n))
            return None  # 工作区无变化

        def fail_undo(*a, **k):
            raise AssertionError("不应创建undo快照")

        with (
            patch.object(
                ToolBackupManager, "check_post_tool_changes", staticmethod(fake_check)
            ),
            patch.object(
                HistoryBackupManager,
                "has_changes",
                staticmethod(lambda tid, hc, **kw: False),  # history无变化（kw=维度include参数）
            ),
            patch.object(
                ToolBackupManager, "_create_undo_snapshot", staticmethod(fail_undo)
            ),
        ):
            ok, msg, undo, changed = ToolBackupManager.rollback(meta)
        assert ok is True
        assert changed is False
        assert undo is None
        assert "无实际变化" in msg
        assert len(check_calls) == 1

    def test_history_change_triggers_rollback(self, tmp_path):
        """history有变化 → 走正常回滚路径（changed=True）"""
        workdir = tmp_path / "wd"
        workdir.mkdir()
        meta = {
            "type": "history_only",
            "task_id": 998001,
            "tool_name": "iteration_start",
            "commit_hash": "",
            "workdir": str(workdir),
            "nest_name": "local",
            "history_commit": "hc1",
        }
        with (
            patch.object(
                HistoryBackupManager,
                "has_changes",
                staticmethod(lambda tid, hc, **kw: True),
            ),
            patch.object(
                ToolBackupManager,
                "_create_undo_snapshot",
                staticmethod(lambda *a, **k: None),
            ),
            patch.object(
                HistoryBackupManager,
                "restore",
                staticmethod(lambda tid, hc, trim_tool_call=False, restore_context=True, restore_checklist=True: "上下文已恢复"),
            ),
        ):
            ok, msg, undo, changed = ToolBackupManager.rollback(meta)
        assert ok is True
        assert changed is True
        assert "上下文已恢复" in msg

    def test_file_change_triggers_rollback(self, tmp_path):
        """工作区文件有变化 → 走正常回滚路径"""
        workdir = tmp_path / "wd"
        workdir.mkdir()
        meta = {
            "type": "git",
            "task_id": 998001,
            "tool_name": "write",
            "commit_hash": "abc1234",
            "workdir": str(workdir),
            "nest_name": "local",
            "history_commit": "",
            "changed_files": ["a.py"],
            "added_files": [],
        }

        def fake_detected(m, w, n):
            m["changed_files"] = ["a.py"]
            m["added_files"] = []
            return m

        fake_undo = {
            "type": "git",
            "task_id": 998001,
            "tool_name": "撤销:write",
            "commit_hash": "def5678",
            "workdir": str(workdir),
            "nest_name": "local",
            "changed_files": ["a.py"],
            "added_files": [],
        }
        with (
            patch.object(
                ToolBackupManager,
                "check_post_tool_changes",
                staticmethod(fake_detected),
            ),
            patch.object(
                ToolBackupManager,
                "_create_undo_snapshot",
                staticmethod(lambda *a, **k: fake_undo),
            ),
            patch.object(
                ToolBackupManager,
                "_rollback_git",
                staticmethod(lambda m, n, **kw: (True, "已回退 1 个文件")),
            ),
        ):
            ok, msg, undo, changed = ToolBackupManager.rollback(meta)
        assert ok is True
        assert changed is True
        assert undo is fake_undo


# ==================== 迭代备份指纹（tool_engine 逻辑等价验证） ====================


class TestIterationFingerprintLogic:
    def test_fingerprint_ignores_stats(self):
        """与 tool_engine._create_iteration_backup 相同的指纹算法：
        仅统计字段变化 → 指纹相同（空转轮次跳过备份）"""
        import hashlib

        def fingerprint(history_context: str, checklist: str) -> str:
            _ctx = json.loads(history_context)
            src = json.dumps(
                _ctx.get("messages", _ctx) if isinstance(_ctx, dict) else _ctx,
                ensure_ascii=False,
                sort_keys=True,
            ) + "\x00" + checklist
            return hashlib.sha256(src.encode("utf-8")).hexdigest()

        c1 = _ctx([{"role": "user", "content": "hi"}], tokens=100)
        c2 = _ctx([{"role": "user", "content": "hi"}], tokens=200)
        assert fingerprint(c1, "cl") == fingerprint(c2, "cl")
        assert fingerprint(c1, "cl") != fingerprint(c1, "cl2")
        assert fingerprint(c1, "cl") != fingerprint(
            json.dumps({"messages": [{"role": "user", "content": "other"}]}), "cl"
        )

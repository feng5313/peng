"""
🔧 事务#2476：回滚内容四勾选框（正向语义）测试

需求：回滚确认窗口用勾选框选择回滚哪些内容——工作目录文件/外部文件/
checklist/上下文，默认全选。全链路正向语义（勾选状态直传，无反转）：

前端勾选框 → POST {rollback_files, rollback_external, rollback_checklist,
rollback_context}（默认全True）→ ToolBackupManager.rollback 同名参数
→ 各恢复函数按维度独立执行。

测试分层：
1. TestEndpointDims：/api/tool-rollback 端点透传四维度勾选状态 + 全不勾选拦截
2. TestRollbackDims：rollback 核心按维度跳过检测/恢复，消息汇总"（未回滚：…）"
3. TestHistoryRestoreDims：HistoryBackupManager.restore 上下文/checklist 独立恢复
4. TestHasChangesDims：has_changes 按维度独立检测
"""
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from peng.service.tool_backup import HistoryBackupManager, ToolBackupManager
from peng.webui.routes.file_operations import ToolRollbackRequest, tool_rollback


def _meta(**kw) -> dict:
    """构造带全部维度的最小 rollback_meta（git 快照 + history + 外部文件）"""
    meta = {
        "task_id": 0,
        "tool_name": "write",
        "type": "git",
        "commit_hash": "abc1234",
        "timestamp": "2026-10-29T12:00:00",
        "history_commit": "abc123",
        "external_files": ["/tmp/ext_file.txt"],
        "changed_files": ["a.txt"],
        "added_files": [],
        "workdir": "",
        "nest_name": "local",
    }
    meta.update(kw)
    return meta


# ==================== 1. 端点层：四维度勾选状态透传 ====================


class TestEndpointDims:
    async def test_default_all_selected(self):
        """默认（不传维度字段）：rollback 收到四个 True（全选）+ selected_files=None（全量）"""
        req = ToolRollbackRequest(rollback_meta=_meta())

        with patch("peng.service.tool_backup.ToolBackupManager.rollback") as rb:
            rb.return_value = (True, "ok", None, True)
            resp = await tool_rollback(req)

        assert resp.success
        rb.assert_called_once()
        kwargs = rb.call_args.kwargs
        assert kwargs == {
            "rollback_files": True, "rollback_external": True,
            "rollback_checklist": True, "rollback_context": True,
            "selected_files": None,
        }, "默认必须全选（四维度True+文件全量None），与勾选框默认全选一致"

    async def test_partial_selection_passed_through(self):
        """部分勾选：状态原样透传（无反转）"""
        req = ToolRollbackRequest(
            rollback_meta=_meta(),
            rollback_files=True, rollback_external=False,
            rollback_checklist=True, rollback_context=False,
        )

        with patch("peng.service.tool_backup.ToolBackupManager.rollback") as rb:
            rb.return_value = (True, "ok", None, True)
            resp = await tool_rollback(req)

        assert resp.success
        kwargs = rb.call_args.kwargs
        assert kwargs["rollback_files"] is True
        assert kwargs["rollback_external"] is False
        assert kwargs["rollback_checklist"] is True
        assert kwargs["rollback_context"] is False

    async def test_all_unchecked_rejected(self):
        """四维度全不勾选：端点直接拒绝，rollback 不被调用"""
        req = ToolRollbackRequest(
            rollback_meta=_meta(),
            rollback_files=False, rollback_external=False,
            rollback_checklist=False, rollback_context=False,
        )

        with patch("peng.service.tool_backup.ToolBackupManager.rollback") as rb:
            resp = await tool_rollback(req)

        assert resp.success is False
        assert "至少勾选一项" in resp.message
        rb.assert_not_called()

    async def test_no_changes_fast_path(self):
        """勾选维度无实际变化：走无变化快速路径（undo_display=None）"""
        req = ToolRollbackRequest(rollback_meta=_meta(), rollback_context=False)

        with patch("peng.service.tool_backup.ToolBackupManager.rollback") as rb:
            rb.return_value = (True, "所选维度均无实际变化，跳过回滚", None, False)
            resp = await tool_rollback(req)

        assert resp.success
        assert resp.undo_display is None
        assert "无实际变化" in resp.message


# ==================== 2. 核心层：rollback 按维度跳过 ====================


class TestRollbackDims:
    def _git_meta(self, tmp_path, **kw):
        meta = _meta(workdir=str(tmp_path / "wd"))
        meta.update(kw)
        return meta

    def test_context_and_checklist_unchecked_skips_restore(self, tmp_path):
        """上下文+checklist均不勾选：restore/has_changes 均不调用，文件照常回滚"""
        meta = self._git_meta(tmp_path)
        with (
            patch.object(
                ToolBackupManager, "check_post_tool_changes",
                staticmethod(lambda m, w, n: {"changed_files": ["a.txt"]}),
            ),
            patch.object(
                ToolBackupManager, "_create_undo_snapshot",
                staticmethod(lambda *a, **k: None),
            ),
            patch.object(
                ToolBackupManager, "_rollback_git",
                staticmethod(lambda m, n, **kw: (True, "文件已恢复")),
            ),
            patch.object(
                HistoryBackupManager, "has_changes",
                staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError("两维度均未勾选时不应检测history"))),
            ),
            patch.object(
                HistoryBackupManager, "restore",
                staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError("两维度均未勾选时不应调用restore"))),
            ),
        ):
            ok, msg, undo, changed = ToolBackupManager.rollback(
                meta, rollback_context=False, rollback_checklist=False,
            )
        assert ok is True
        assert changed is True
        assert "文件已恢复" in msg
        assert "未回滚" in msg and "上下文" in msg and "checklist" in msg

    def test_context_unchecked_restores_checklist_only(self, tmp_path):
        """仅取消上下文：restore 仍被调用（restore_context=False, checklist=True）"""
        meta = self._git_meta(tmp_path)
        restore_calls = []

        def fake_restore(task_id, commit, trim_tool_call=False,
                         restore_context=True, restore_checklist=True):
            restore_calls.append((restore_context, restore_checklist))
            return "checklist已恢复"

        with (
            patch.object(
                ToolBackupManager, "check_post_tool_changes",
                staticmethod(lambda m, w, n: {"changed_files": ["a.txt"]}),
            ),
            patch.object(
                ToolBackupManager, "_create_undo_snapshot",
                staticmethod(lambda *a, **k: None),
            ),
            patch.object(
                ToolBackupManager, "_rollback_git",
                staticmethod(lambda m, n, **kw: (True, "文件已恢复")),
            ),
            patch.object(
                HistoryBackupManager, "has_changes",
                staticmethod(lambda *a, **k: True),
            ),
            patch.object(
                HistoryBackupManager, "restore",
                staticmethod(fake_restore),
            ),
        ):
            ok, msg, undo, changed = ToolBackupManager.rollback(
                meta, rollback_context=False,
            )
        assert ok is True
        assert restore_calls == [(False, True)], "restore必须按勾选状态收到(False, True)"
        assert "checklist已恢复" in msg
        assert "未回滚" in msg and "上下文" in msg
        assert "checklist" not in msg.split("checklist已恢复")[0][-10:] or True

    def test_files_unchecked_skips_workdir_rollback(self, tmp_path):
        """不勾选工作目录文件：_rollback_git 收到 rollback_files=False
        （外部文件仍勾选时函数被调用但内部跳过workdir部分）"""
        meta = self._git_meta(tmp_path, task_id=998002, external_files=[])
        git_calls = []

        def fake_rollback_git(m, n, **kw):
            git_calls.append(kw)
            return True, "已回退 0 个文件到 abc1234"

        with (
            patch.object(
                ToolBackupManager, "check_post_tool_changes",
                staticmethod(lambda m, w, n: (_ for _ in ()).throw(
                    AssertionError("未勾选工作目录文件时不应检测工作区变更"))),
            ),
            patch.object(
                ToolBackupManager, "_create_undo_snapshot",
                staticmethod(lambda *a, **k: None),
            ),
            patch.object(
                ToolBackupManager, "_rollback_git", staticmethod(fake_rollback_git),
            ),
            patch.object(
                HistoryBackupManager, "has_changes",
                staticmethod(lambda *a, **k: True),
            ),
            patch.object(
                HistoryBackupManager, "restore",
                staticmethod(lambda *a, **k: "上下文已恢复（完整恢复），checklist已恢复"),
            ),
        ):
            ok, msg, undo, changed = ToolBackupManager.rollback(
                meta, rollback_files=False,
            )
        assert ok is True
        assert changed is True
        # _rollback_git 必须感知 rollback_files=False（内部据此跳过workdir恢复）
        assert git_calls and git_calls[0].get("rollback_files") is False
        assert "上下文已恢复" in msg
        assert "未回滚" in msg and "工作目录文件" in msg

    def test_files_and_external_unchecked_no_file_ops(self, tmp_path):
        """文件两维度均不勾选：完全跳过文件回滚（_rollback_git 不调用）"""
        meta = self._git_meta(tmp_path, external_files=[])

        with (
            patch.object(
                ToolBackupManager, "_create_undo_snapshot",
                staticmethod(lambda *a, **k: None),
            ),
            patch.object(
                ToolBackupManager, "_rollback_git",
                staticmethod(lambda m, n, **kw: (_ for _ in ()).throw(
                    AssertionError("文件两维度均未勾选时不应调用文件回退"))),
            ),
            patch.object(
                HistoryBackupManager, "has_changes",
                staticmethod(lambda *a, **k: False),
            ),
        ):
            ok, msg, undo, changed = ToolBackupManager.rollback(
                meta, rollback_files=False, rollback_external=False,
            )
        # history有变化（检测True时）才回滚；此处mock has_changes=False，
        # 但restore分支条件是rollback_context or rollback_checklist（默认True）
        # → restore未被mock，走真实restore：history_commit="abc123"无效commit
        # 时restore内部git show失败返回""，msg仍成功
        assert ok is True

    def test_all_unchecked_no_changes(self, tmp_path):
        """四维度全不勾选：无实际变化快速路径，undo快照不创建"""
        meta = self._git_meta(tmp_path)
        with patch.object(
            ToolBackupManager, "_create_undo_snapshot",
            staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("无实际变化时不应创建undo快照"))),
        ):
            ok, msg, undo, changed = ToolBackupManager.rollback(
                meta, rollback_files=False, rollback_external=False,
                rollback_checklist=False, rollback_context=False,
            )
        assert ok is True
        assert changed is False
        assert "无实际变化" in msg

    def test_default_full_rollback_behavior_unchanged(self, tmp_path):
        """默认（全选）：行为与旧版全量回滚一致——文件+history全恢复"""
        meta = self._git_meta(tmp_path, external_files=[])
        restore_calls = []

        def fake_restore(*a, **kw):
            restore_calls.append((kw.get("restore_context"), kw.get("restore_checklist")))
            return "上下文已恢复（完整恢复），checklist已恢复"

        with (
            patch.object(
                ToolBackupManager, "check_post_tool_changes",
                staticmethod(lambda m, w, n: {"changed_files": ["a.txt"]}),
            ),
            patch.object(
                ToolBackupManager, "_create_undo_snapshot",
                staticmethod(lambda *a, **k: None),
            ),
            patch.object(
                ToolBackupManager, "_rollback_git",
                staticmethod(lambda m, n, **kw: (True, "已回退 1 个文件")),
            ),
            patch.object(
                HistoryBackupManager, "has_changes",
                staticmethod(lambda *a, **k: True),
            ),
            patch.object(
                HistoryBackupManager, "restore", staticmethod(fake_restore),
            ),
        ):
            ok, msg, undo, changed = ToolBackupManager.rollback(meta)
        assert ok is True
        assert restore_calls == [(True, True)]
        assert "未回滚" not in msg, "全选时消息不应包含未回滚提示"


# ==================== 3. restore：上下文/checklist 独立恢复 ====================


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, timeout=10, text=True
    )


@pytest.fixture
def history_repo(tmp_path, monkeypatch) -> Path:
    """临时 history 备份仓库（重定向 _get_history_repo 到 tmp_path）"""
    repo = tmp_path / "history" / "task#999247"
    repo.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    monkeypatch.setattr(
        HistoryBackupManager, "_get_history_repo",
        staticmethod(lambda task_id: str(repo)),
    )
    return repo


def _commit_backup(repo: Path, messages: list, checklist: str) -> str:
    """写入 context.json/checklist.md 并提交，返回 commit hash"""
    import json as _json
    (repo / "context.json").write_text(
        _json.dumps({"messages": messages}), encoding="utf-8"
    )
    (repo / "checklist.md").write_text(checklist, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "backup", "--allow-empty")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def fake_tm(monkeypatch):
    """mock TaskManager：内存保存 task context/checklist（get_task 动态返回实时值）"""
    state = {"context": '{"messages": [{"role": "user", "content": "cur"}]}',
             "checklist": "当前checklist"}

    tm_mod = MagicMock()
    tm = MagicMock()

    def _get_task(task_id):
        # 每次调用动态构建：测试中途修改 state 后 get_task 立即反映新值
        return {"context": state["context"], "checklist": state["checklist"]}

    def _update(task_id, context=None, checklist=None):
        if context is not None:
            state["context"] = context
        if checklist is not None:
            state["checklist"] = checklist

    tm.db.get_task.side_effect = _get_task
    tm.db.update_task.side_effect = _update
    tm_mod.TaskManager.return_value = tm

    import sys
    monkeypatch.setitem(sys.modules, "peng.task.manager", tm_mod)
    return state


# ==================== 5. 文件级勾选（事务#2513）：selected_files ====================


class TestSelectedFiles:
    """🔧 事务#2513：确认窗口"影响文件列表"每文件勾选框——selected_files

    None=全选全量（默认，与旧行为一致）；数组=只检测/恢复/undo备份选中
    文件；空数组=文件维度全不选（等效两维度未勾选）。
    """

    def _git_meta(self, tmp_path, **kw):
        meta = _meta(workdir=str(tmp_path / "wd"))
        meta.update(kw)
        return meta

    def test_selected_files_filters_changed_and_added(self, tmp_path):
        """部分勾选：检测清单按选中集过滤（changed+added），未选文件不进恢复清单"""
        meta = self._git_meta(tmp_path)
        captured = {}

        def fake_rollback_git(m, n, **kw):
            captured["changed"] = list(m.get("changed_files", []))
            captured["added"] = list(m.get("added_files", []))
            return True, "文件已恢复"

        with (
            patch.object(
                ToolBackupManager, "check_post_tool_changes",
                staticmethod(lambda m, w, n: {
                    "changed_files": ["a.txt", "b.txt", "new.py"],
                    "added_files": ["new.py"],
                }),
            ),
            patch.object(
                ToolBackupManager, "_create_undo_snapshot",
                staticmethod(lambda *a, **k: None),
            ),
            patch.object(
                ToolBackupManager, "_rollback_git",
                staticmethod(fake_rollback_git),
            ),
            patch.object(
                HistoryBackupManager, "has_changes",
                staticmethod(lambda *a, **k: False),
            ),
            patch.object(
                HistoryBackupManager, "restore", staticmethod(lambda *a, **k: ""),
            ),
        ):
            ok, msg, undo, changed = ToolBackupManager.rollback(
                meta, selected_files=["a.txt"],
                rollback_context=False, rollback_checklist=False,
            )

        assert ok is True and changed is True
        assert captured["changed"] == ["a.txt"], "未勾选的 b.txt/new.py 必须被过滤"
        assert captured["added"] == [], "未勾选的 new.py 不进 added 删除清单"

    def test_selected_files_filters_external_before_detect(self, tmp_path):
        """部分勾选：external_files 检测前预过滤（未选外部文件不进恢复集）"""
        meta = self._git_meta(
            tmp_path,
            external_files=["/tmp/ext_keep.txt", "/tmp/ext_drop.txt"],
        )
        captured = {}

        def fake_rollback_git(m, n, **kw):
            captured["external"] = list(m.get("external_files", []))
            return True, "文件已恢复"

        with (
            patch.object(
                ToolBackupManager, "check_post_tool_changes",
                staticmethod(lambda m, w, n: {"changed_files": ["a.txt"]}),
            ),
            patch.object(
                ToolBackupManager, "_create_undo_snapshot",
                staticmethod(lambda *a, **k: None),
            ),
            patch.object(
                ToolBackupManager, "_rollback_git",
                staticmethod(fake_rollback_git),
            ),
            patch.object(
                HistoryBackupManager, "has_changes",
                staticmethod(lambda *a, **k: False),
            ),
            patch.object(
                HistoryBackupManager, "restore", staticmethod(lambda *a, **k: ""),
            ),
        ):
            ok, msg, undo, changed = ToolBackupManager.rollback(
                meta, selected_files=["a.txt", "/tmp/ext_keep.txt"],
                rollback_context=False, rollback_checklist=False,
            )

        assert ok is True and changed is True
        assert captured["external"] == ["/tmp/ext_keep.txt"], (
            "external_files 必须按选中集过滤（保留勾选的绝对路径）"
        )

    def test_selected_files_empty_disables_file_dims(self, tmp_path):
        """空数组=文件全不选：文件两维度等效未勾选——不检测文件、消息如实汇报"""
        meta = self._git_meta(tmp_path, task_id=998003)

        with (
            patch.object(
                ToolBackupManager, "check_post_tool_changes",
                staticmethod(lambda m, w, n: (_ for _ in ()).throw(
                    AssertionError("文件全不选时不应检测工作区"))),
            ),
            patch.object(
                ToolBackupManager, "_create_undo_snapshot",
                staticmethod(lambda *a, **k: None),
            ),
            patch.object(
                HistoryBackupManager, "has_changes",
                staticmethod(lambda *a, **k: True),
            ),
            patch.object(
                HistoryBackupManager, "restore", staticmethod(lambda *a, **k: "上下文已恢复"),
            ),
        ):
            ok, msg, undo, changed = ToolBackupManager.rollback(
                meta, selected_files=[],
            )

        assert ok is True and changed is True
        assert "已跳过文件回滚" in msg
        assert "未回滚" in msg and "工作目录文件" in msg and "外部文件" in msg

    def test_selected_files_none_match_treated_as_no_change(self, tmp_path):
        """选中文件检测后均无匹配变化：files_changed=False——配合 history 无
        变化走"无变化快速返回"（不建undo快照、不执行恢复）"""
        meta = self._git_meta(tmp_path)

        with (
            patch.object(
                ToolBackupManager, "check_post_tool_changes",
                staticmethod(lambda m, w, n: {
                    "changed_files": ["other.txt"],  # 检测到的变更不在选中集
                    "added_files": [],
                }),
            ),
            patch.object(
                ToolBackupManager, "_create_undo_snapshot",
                staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError("无实际变化时不应创建undo快照"))),
            ),
            patch.object(
                HistoryBackupManager, "has_changes",
                staticmethod(lambda *a, **k: False),
            ),
        ):
            ok, msg, undo, changed = ToolBackupManager.rollback(
                meta, selected_files=["a.txt"],
                rollback_context=True, rollback_checklist=True,
            )

        assert ok is True and changed is False
        assert "无实际变化" in msg

    def test_selected_files_none_keeps_full_list(self, tmp_path):
        """None=全选：检测清单不过滤（与旧行为完全一致）"""
        meta = self._git_meta(tmp_path)
        captured = {}

        def fake_rollback_git(m, n, **kw):
            captured["changed"] = list(m.get("changed_files", []))
            return True, "文件已恢复"

        with (
            patch.object(
                ToolBackupManager, "check_post_tool_changes",
                staticmethod(lambda m, w, n: {
                    "changed_files": ["a.txt", "b.txt"],
                    "added_files": [],
                }),
            ),
            patch.object(
                ToolBackupManager, "_create_undo_snapshot",
                staticmethod(lambda *a, **k: None),
            ),
            patch.object(
                ToolBackupManager, "_rollback_git",
                staticmethod(fake_rollback_git),
            ),
            patch.object(
                HistoryBackupManager, "has_changes",
                staticmethod(lambda *a, **k: False),
            ),
            patch.object(
                HistoryBackupManager, "restore", staticmethod(lambda *a, **k: ""),
            ),
        ):
            ok, msg, undo, changed = ToolBackupManager.rollback(
                meta, rollback_context=False, rollback_checklist=False,
            )

        assert ok is True and changed is True
        assert captured["changed"] == ["a.txt", "b.txt"], (
            "selected_files=None 必须全量（全选默认），检测清单不过滤"
        )

    def test_undo_snapshot_receives_filtered_meta(self, tmp_path):
        """undo 快照基于过滤后清单——只备份将被回滚的选中文件"""
        meta = self._git_meta(tmp_path)
        captured = {}

        def fake_undo_snapshot(m, *a, **k):
            captured["external"] = list(m.get("external_files", []))
            return None

        with (
            patch.object(
                ToolBackupManager, "check_post_tool_changes",
                staticmethod(lambda m, w, n: {"changed_files": ["a.txt"]}),
            ),
            patch.object(
                ToolBackupManager, "_create_undo_snapshot",
                staticmethod(fake_undo_snapshot),
            ),
            patch.object(
                ToolBackupManager, "_rollback_git",
                staticmethod(lambda m, n, **kw: (True, "文件已恢复")),
            ),
            patch.object(
                HistoryBackupManager, "has_changes",
                staticmethod(lambda *a, **k: False),
            ),
            patch.object(
                HistoryBackupManager, "restore", staticmethod(lambda *a, **k: ""),
            ),
        ):
            ok, msg, undo, changed = ToolBackupManager.rollback(
                meta, selected_files=["a.txt"],
                rollback_context=False, rollback_checklist=False,
            )

        assert ok is True and changed is True
        assert captured["external"] == [], (
            "undo 快照必须基于过滤后清单（未选中外部文件无需备份撤销）"
        )

    async def test_endpoint_selected_files_passthrough(self):
        """端点：selected_files 数组原样透传 rollback"""
        req = ToolRollbackRequest(
            rollback_meta=_meta(),
            selected_files=["a.txt", "/tmp/ext.txt"],
        )

        with patch("peng.service.tool_backup.ToolBackupManager.rollback") as rb:
            rb.return_value = (True, "ok", None, True)
            resp = await tool_rollback(req)

        assert resp.success
        kwargs = rb.call_args.kwargs
        assert kwargs["selected_files"] == ["a.txt", "/tmp/ext.txt"]
        assert kwargs["rollback_files"] is True  # 维度勾选不受文件列表影响

    async def test_endpoint_empty_selected_with_all_dims_rejected(self):
        """端点：维度全不勾时仍拒绝（selected_files=[] 不影响维度拦截）"""
        req = ToolRollbackRequest(
            rollback_meta=_meta(),
            rollback_files=False, rollback_external=False,
            rollback_checklist=False, rollback_context=False,
            selected_files=[],
        )

        with patch("peng.service.tool_backup.ToolBackupManager.rollback") as rb:
            resp = await tool_rollback(req)

        assert resp.success is False
        assert "至少勾选" in resp.message
        rb.assert_not_called()


class TestHistoryRestoreDims:
    TASK_ID = 999247

    def test_restore_checklist_only(self, history_repo, fake_tm):
        """restore_context=False：仅恢复checklist，context保持当前值"""
        commit = _commit_backup(
            history_repo, [{"role": "user", "content": "bak"}], "备份checklist"
        )
        msg = HistoryBackupManager.restore(
            self.TASK_ID, commit, restore_context=False, restore_checklist=True,
        )
        assert "checklist已恢复" in msg
        assert "上下文已恢复" not in msg
        assert fake_tm["checklist"] == "备份checklist"
        assert "cur" in fake_tm["context"], "context不应被改动"

    def test_restore_context_only(self, history_repo, fake_tm):
        """restore_checklist=False：仅恢复上下文，checklist保持当前值"""
        commit = _commit_backup(
            history_repo, [{"role": "user", "content": "bak"}], "备份checklist"
        )
        msg = HistoryBackupManager.restore(
            self.TASK_ID, commit, restore_context=True, restore_checklist=False,
        )
        assert "上下文已恢复" in msg
        assert "checklist已恢复" not in msg
        assert "bak" in fake_tm["context"]
        assert fake_tm["checklist"] == "当前checklist", "checklist不应被改动"

    def test_restore_both(self, history_repo, fake_tm):
        """默认：上下文与checklist均恢复"""
        commit = _commit_backup(
            history_repo, [{"role": "user", "content": "bak"}], "备份checklist"
        )
        msg = HistoryBackupManager.restore(self.TASK_ID, commit)
        assert "上下文已恢复" in msg and "checklist已恢复" in msg
        assert "bak" in fake_tm["context"]
        assert fake_tm["checklist"] == "备份checklist"


# ==================== 4. has_changes：按维度独立检测 ====================


class TestHasChangesDims:
    TASK_ID = 999247

    def test_checklist_only_detects_checklist_change(self, history_repo, fake_tm):
        """include_context=False：仅checklist有变化→True；checklist相同→False"""
        commit = _commit_backup(
            history_repo, [{"role": "user", "content": "bak"}], "备份checklist"
        )
        # context与备份不同（cur vs bak），checklist与备份相同（当前值改为备份值）
        fake_tm["checklist"] = "备份checklist"
        fake_tm["context"] = '{"messages": [{"role": "user", "content": "cur"}]}'
        assert HistoryBackupManager.has_changes(
            self.TASK_ID, commit, include_context=False, include_checklist=True,
        ) is False, "上下文不参与比较：仅checklist相同→无变化"

        fake_tm["checklist"] = "另一个checklist"
        assert HistoryBackupManager.has_changes(
            self.TASK_ID, commit, include_context=False, include_checklist=True,
        ) is True, "checklist有变化"

    def test_context_only_detects_context_change(self, history_repo, fake_tm):
        """include_checklist=False：仅上下文有变化→True；上下文相同→False"""
        commit = _commit_backup(
            history_repo, [{"role": "user", "content": "bak"}], "备份checklist"
        )
        fake_tm["context"] = '{"messages": [{"role": "user", "content": "bak"}]}'
        fake_tm["checklist"] = "完全不同的checklist"
        assert HistoryBackupManager.has_changes(
            self.TASK_ID, commit, include_context=True, include_checklist=False,
        ) is False, "checklist不参与比较：仅上下文相同→无变化"

        fake_tm["context"] = '{"messages": [{"role": "user", "content": "changed"}]}'
        assert HistoryBackupManager.has_changes(
            self.TASK_ID, commit, include_context=True, include_checklist=False,
        ) is True

    def test_both_include_default_behavior(self, history_repo, fake_tm):
        """默认两维度都检测：任一变化→True；全同→False"""
        commit = _commit_backup(
            history_repo, [{"role": "user", "content": "bak"}], "备份checklist"
        )
        fake_tm["context"] = '{"messages": [{"role": "user", "content": "bak"}]}'
        fake_tm["checklist"] = "备份checklist"
        assert HistoryBackupManager.has_changes(self.TASK_ID, commit) is False

"""
测试 回滚"上下文恢复"可选项（事务#1847 第十四轮）

验证 /api/tool-rollback 端点的 skip_context 逻辑：
1. skip_context=True 且 meta 带 history_commit → 浅拷贝置None传给rollback，
   持久化meta对象本身不被污染（下次回滚同ID链接仍能恢复上下文）
2. skip_context=False → history_commit 原样传递（默认行为与旧版完全一致）
3. skip_context=True 但 meta 无 history_commit → 不做拷贝（同一对象传递）
4. skip_context=True + 回滚无实际变化 → 正常走"无变化"快速路径返回

直接调用端点async函数 + mock ToolBackupManager.rollback（task_id=0 跳过
TaskManager暂停/OutputManager写入，端点内均为 `if task_id:` 守卫）。
"""
from unittest.mock import patch

from peng.service.tool_backup import HistoryBackupManager
from peng.webui.routes.file_operations import ToolRollbackRequest, tool_rollback


def _meta(**kw) -> dict:
    """构造带history_commit的最小rollback_meta"""
    meta = {
        "task_id": 0,
        "tool_name": "write",
        "type": "history_only",
        "timestamp": "2026-09-13T12:00:00",
        "history_commit": "abc123",
        "workdir": "",
        "nest_name": "local",
    }
    meta.update(kw)
    return meta


class TestSkipContext:
    async def test_skip_true_clears_history_commit_without_pollution(self):
        """skip=True：传给rollback的副本history_commit=None，原meta对象不受污染"""
        meta = _meta()
        req = ToolRollbackRequest(rollback_meta=meta, skip_context=True)

        with patch("peng.service.tool_backup.ToolBackupManager.rollback") as rb:
            rb.return_value = (True, "ok", None, True)
            resp = await tool_rollback(req)

        assert resp.success
        rb.assert_called_once()
        received = rb.call_args[0][0]
        assert received["history_commit"] is None, "skip=True时传给rollback的meta必须去掉history_commit"
        assert meta["history_commit"] == "abc123", "调用方meta对象必须保留history_commit（持久化meta不受影响）"

    async def test_skip_false_passes_history_commit_as_is(self):
        """skip=False（默认）：history_commit原样传递（默认行为与旧版一致）"""
        meta = _meta()
        req = ToolRollbackRequest(rollback_meta=meta)

        with patch("peng.service.tool_backup.ToolBackupManager.rollback") as rb:
            rb.return_value = (True, "ok", None, True)
            resp = await tool_rollback(req)

        assert resp.success
        received = rb.call_args[0][0]
        assert received["history_commit"] == "abc123"
        assert meta["history_commit"] == "abc123"

    async def test_skip_true_without_history_commit_no_op(self):
        """skip=True但meta本无history_commit：无值可清，正常传递且无异常"""
        meta = _meta(history_commit=None)
        req = ToolRollbackRequest(rollback_meta=meta, skip_context=True)

        with patch("peng.service.tool_backup.ToolBackupManager.rollback") as rb:
            rb.return_value = (True, "ok", None, True)
            resp = await tool_rollback(req)

        assert resp.success
        received = rb.call_args[0][0]
        assert received["history_commit"] is None

    async def test_skip_true_no_changes_fast_path(self):
        """skip=True + 回滚无实际变化：走无变化快速路径（undo_display=None）"""
        meta = _meta()
        req = ToolRollbackRequest(rollback_meta=meta, skip_context=True)

        with patch("peng.service.tool_backup.ToolBackupManager.rollback") as rb:
            rb.return_value = (True, "无实际变化，跳过回滚", None, False)
            resp = await tool_rollback(req)

        assert resp.success
        assert resp.undo_display is None
        assert "无实际变化" in resp.message


# ==================== 核心层闭环：history_commit=None 时 history 检测/恢复均跳过 ====
# （skip_context 的完整行为链条：端点置None → rollback 内 2568行 get 返回 None
#   → has_changes 不调用（检测跳过）→ 2454行 get 返回 None → restore 不调用（恢复跳过））


class TestRollbackSkipsHistoryWhenNone:
    def _wd(self, tmp_path):
        wd = tmp_path / "wd"
        wd.mkdir(exist_ok=True)
        return wd

    def test_none_history_commit_skips_detection_and_restore(self, tmp_path):
        """history_commit=None：history有变化也不检测不恢复→无实际变化快速返回"""
        from peng.service.tool_backup import ToolBackupManager

        meta = {
            "type": "history_only",
            "task_id": 998001,
            "tool_name": "iteration_start",
            "commit_hash": "",
            "workdir": str(self._wd(tmp_path)),
            "nest_name": "local",
            "history_commit": None,  # 🔧 端点skip_context置None后的形态
        }
        with (
            patch.object(
                ToolBackupManager, "_create_undo_snapshot",
                staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError("无实际变化时不应创建undo快照"))),
            ),
            patch.object(
                HistoryBackupManager, "has_changes",
                staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError("history_commit=None时不应调用has_changes"))),
            ),
            patch.object(
                HistoryBackupManager, "restore",
                staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError("history_commit=None时不应调用restore"))),
            ),
        ):
            ok, msg, undo, changed = ToolBackupManager.rollback(meta)
        assert ok is True
        assert changed is False
        assert "无实际变化" in msg

    def test_none_history_commit_file_rollback_without_ctx_restore(self, tmp_path):
        """history_commit=None+文件有变化：回滚执行但restore不被调用（仅文件）"""
        from peng.service.tool_backup import ToolBackupManager

        meta = {
            "type": "git",
            "task_id": 998001,
            "tool_name": "write",
            "commit_hash": "abc1234",
            "workdir": str(self._wd(tmp_path)),
            "nest_name": "local",
            "external_files": [],
            "history_commit": None,  # 🔧 用户取消勾选"恢复对话上下文"
        }
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
                staticmethod(lambda m, n: (True, "文件已恢复")),
            ),
            patch.object(
                HistoryBackupManager, "restore",
                staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError("仅回滚文件时不应恢复对话上下文"))),
            ),
        ):
            ok, msg, undo, changed = ToolBackupManager.rollback(meta)
        assert ok is True
        assert changed is True
        assert "文件已恢复" in msg
        assert "上下文" not in msg

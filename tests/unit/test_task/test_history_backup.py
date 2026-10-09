"""
测试 HistoryBackupManager.preview_checklist_diff

验证回滚确认窗口的 checklist diff 预览：
- 对比当前 task db checklist 与 history 备份 commit 中 checklist.md 的差异
- diff 方向与文件 diff 一致（-=回滚后移除 +=回滚后恢复）
- 无快照备份 / 无变化 / 异常 等边界场景
"""
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from peng.service.tool_backup import HistoryBackupManager

# 测试用 task_id（避免与真实 data/history/task#N 冲突，通过 mock 重定向到 tmp_path）


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """在指定仓库执行 git 命令"""
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, timeout=10
    )


@pytest.fixture
def history_repo(tmp_path, monkeypatch) -> Path:
    """创建临时 history 备份仓库（重定向 _get_history_repo 到 tmp_path）"""
    repo = tmp_path / "history" / "task#999001"
    repo.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@peng")
    _git(repo, "config", "user.name", "Peng Test")
    (repo / "context.json").write_text("{}", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init", "--allow-empty")

    monkeypatch.setattr(
        HistoryBackupManager, "_get_history_repo", staticmethod(lambda task_id: str(repo))
    )
    return repo


def _commit_checklist(repo: Path, checklist: str) -> str:
    """写入指定 checklist 内容并 commit，返回 commit hash"""
    (repo / "checklist.md").write_text(checklist, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "backup", "--allow-empty")
    result = _git(repo, "rev-parse", "HEAD")
    return result.stdout.decode().strip()


def _mock_task_manager(current_checklist: str) -> MagicMock:
    """构造 mock TaskManager（返回指定当前 checklist）"""
    tm = MagicMock()
    tm.db.get_task.return_value = {"id": 999001, "checklist": current_checklist}
    fake_module = MagicMock()
    fake_module.TaskManager = lambda: tm
    return fake_module


class TestPreviewChecklistDiff:
    """preview_checklist_diff 主流程"""

    def test_no_history_commit_returns_unavailable(self):
        """无 history_commit（工具级快照）→ available=False"""
        result = HistoryBackupManager.preview_checklist_diff(999001, "")
        assert result["available"] is False
        assert result["changed"] is False
        assert result["diff"] == ""
        assert "未包含" in result["message"]

    def test_no_task_id_returns_unavailable(self):
        """无 task_id → available=False"""
        result = HistoryBackupManager.preview_checklist_diff(0, "abc123")
        assert result["available"] is False

    def test_checklist_changed(self, history_repo):
        """checklist 有变化 → changed=True，diff方向正确（+=快照内容）"""
        snapshot = "# 计划\n1. ⌛ 步骤A\n2. ⌛ 步骤B\n"
        current = "# 计划\n1. ✅ 步骤A\n2. ⌛ 步骤B\n3. ⌛ 新增步骤C\n"
        commit = _commit_checklist(history_repo, snapshot)

        with patch.dict(
            "sys.modules", {"peng.task.manager": _mock_task_manager(current)}
        ):
            result = HistoryBackupManager.preview_checklist_diff(
                999001, commit, "迭代#1 快照[2026-08-16 02:00:00]"
            )

        assert result["available"] is True
        assert result["changed"] is True
        assert result["message"] == "ok"
        # diff 头部标注来源
        assert "迭代#1 快照[2026-08-16 02:00:00]" in result["diff"]
        assert "checklist（当前）" in result["diff"]
        # 方向：当前有、快照无 → "-"（回滚后移除）
        assert any(
            line.startswith("-") and "新增步骤C" in line
            for line in result["diff"].split("\n")
        )
        # 方向：快照内容 → "+"（回滚后恢复快照状态）
        assert any(
            line.startswith("+") and "⌛ 步骤A" in line
            for line in result["diff"].split("\n")
        )
        assert any(
            line.startswith("-") and "✅ 步骤A" in line
            for line in result["diff"].split("\n")
        )

    def test_checklist_unchanged(self, history_repo):
        """checklist 完全相同 → changed=False，diff为空"""
        checklist = "# 计划\n1. ✅ 完成\n"
        commit = _commit_checklist(history_repo, checklist)

        with patch.dict(
            "sys.modules", {"peng.task.manager": _mock_task_manager(checklist)}
        ):
            result = HistoryBackupManager.preview_checklist_diff(999001, commit)

        assert result["available"] is True
        assert result["changed"] is False
        assert result["diff"] == ""
        assert "无变化" in result["message"]

    def test_snapshot_empty_vs_current_content(self, history_repo):
        """快照checklist为空、当前有内容 → 回滚将清空checklist（全部为-行）"""
        commit = _commit_checklist(history_repo, "")
        current = "# 计划\n1. ✅ 已完成\n"

        with patch.dict(
            "sys.modules", {"peng.task.manager": _mock_task_manager(current)}
        ):
            result = HistoryBackupManager.preview_checklist_diff(999001, commit)

        assert result["changed"] is True
        assert any(
            line.startswith("-") and "已完成" in line
            for line in result["diff"].split("\n")
        )
        # 快照为空，不应有任何 + 内容行（+++开头的文件头除外）
        assert not any(
            line.startswith("+") and not line.startswith("+++")
            for line in result["diff"].split("\n")
        )

    def test_old_commit_without_checklist_file(self, history_repo):
        """旧 commit 无 checklist.md → 快照视为空字符串（与backup语义一致）"""
        # init commit 未提交 checklist.md，直接用 HEAD（即 init commit）测试
        result = _git(history_repo, "rev-parse", "HEAD")
        commit = result.stdout.decode().strip()

        with patch.dict(
            "sys.modules", {"peng.task.manager": _mock_task_manager("# 当前内容\n")}
        ):
            result = HistoryBackupManager.preview_checklist_diff(999001, commit)

        assert result["available"] is True
        assert result["changed"] is True
        # 当前内容相对空快照 → 全部为 - 行
        assert "-# 当前内容" in result["diff"]

    def test_diff_truncated(self, history_repo):
        """diff超3MB字节 → 截断到完整行并返回未显示字节数"""
        # 构造超过3MB的checklist（每行约30字节，需要约10万行）
        snapshot = "\n".join(f"- 快照行{i:06d} abcdefghijklmnop" for i in range(110000)) + "\n"
        current = "\n".join(f"- 当前行{i:06d} abcdefghijklmnop" for i in range(110000)) + "\n"
        commit = _commit_checklist(history_repo, snapshot)

        with patch.dict(
            "sys.modules", {"peng.task.manager": _mock_task_manager(current)}
        ):
            result = HistoryBackupManager.preview_checklist_diff(999001, commit)

        assert result["changed"] is True
        assert result["truncated"] is True
        assert result["truncated_bytes"] > 0
        # 截断后文本不超过3MB
        from peng.service.tool_backup import _DIFF_PREVIEW_MAX_BYTES
        assert len(result["diff"].encode("utf-8")) <= _DIFF_PREVIEW_MAX_BYTES
        # 截断按完整行对齐：最后一行是完整的diff行（以-/+开头或@@头）
        last_line = result["diff"].split("\n")[-1]
        assert last_line.startswith(("-", "+", "@@")) or last_line == ""

    def test_task_manager_import_failure_treated_as_empty(self, history_repo):
        """TaskManager 导入/读取失败 → 当前checklist按空处理，diff仍可计算"""
        snapshot = "# 快照内容\n"
        commit = _commit_checklist(history_repo, snapshot)

        broken = MagicMock()
        broken.TaskManager = MagicMock(side_effect=RuntimeError("db down"))
        with patch.dict("sys.modules", {"peng.task.manager": broken}):
            result = HistoryBackupManager.preview_checklist_diff(999001, commit)

        assert result["available"] is True
        assert result["changed"] is True
        # 快照内容相对空当前 → 全部为 + 行
        assert "+# 快照内容" in result["diff"]

    def test_invalid_commit_returns_error(self, history_repo):
        """commit不存在 → git show失败，走异常/返回路径"""
        result = HistoryBackupManager.preview_checklist_diff(
            999001, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        )
        # git show 返回非0 → 快照按空处理，而非异常（当前checklist读取失败按空）→ 无变化
        # （返回路径取决于实现：git show失败不抛异常，returncode!=0 → snapshot_cl=""）
        assert result["available"] is True

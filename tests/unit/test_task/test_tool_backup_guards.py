"""🔧 事务#2402：备份/回滚路径护栏测试

背景（task#2332 生产事故）：回滚窗口把 D:/work（workdir D:/work/fcc 的父
目录）与 D:/work/fcc/nul（cmd `2>nul` 的重定向目标，Windows 设备名伪存在）
列为"变更文件（回滚将恢复到快照版本）"。

根因链与对应护栏：
1. _extract_paths_from_command 收集重定向目标 nul / 动词参数 D:/work
   → 提取源头剔除设备名与 workdir 祖先（_is_windows_device_path /
     _is_workdir_ancestor）
2. _filter_unbackupable / _filter_manifest_paths 不拦祖先目录
   → 备份对象/恢复集过滤新增剔除（存量 manifest 死条目读取侧自动失效）
3. check_changes 对目录 git hash-object 必失败 → 恒 append "已变更"
   → 目录直接跳过；hash 失败（权限等）不再误报
4. restore 对 tree 基线 git show rc=0 返回 ls-tree 文本 → open(目录,'wb')
   抛 IsADirectoryError 炸掉整批恢复
   → 目录/设备名/非 blob 基线一律跳过；单文件 OSError 不中断
5. manifest_add 兜底剔除设备名（防死条目永久累积）
"""
import json
import subprocess
from pathlib import Path

import pytest

from peng.service import tool_backup
from peng.service.tool_backup import (
    ExternalFileBackup,
    ToolBackupManager,
    _SnapshotTransientError,
    _is_windows_device_path,
    _is_workdir_ancestor,
    manifest_add,
    manifest_get,
)


def _git(repo: Path, *args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, timeout=10, **kw
    )


def _git_init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")


@pytest.fixture
def default_repo(tmp_path, monkeypatch):
    """默认备份仓库（data/file_backups/task#xxx）→ tmp 隔离"""
    repo = tmp_path / "file_backups" / "task#997001"
    _git_init(repo)
    monkeypatch.setattr(
        ExternalFileBackup, "_get_backup_repo", staticmethod(lambda tid: str(repo))
    )
    return repo


class TestPathPredicates:
    """纯形态判定：零 IO，跨平台一致"""

    @pytest.mark.parametrize("p", [
        "nul", "NUL", "Con", "PRN", "AUX", "com1", "LPT9",
        "D:/work/fcc/nul", "D:\\work\\fcc\\NUL", "x/nul.txt", "COM3.cfg",
    ])
    def test_device_paths(self, p):
        assert _is_windows_device_path(p) is True

    @pytest.mark.parametrize("p", [
        "", "work", "fd.txt", "null_", "console.log", "D:/work/fcc/null2",
        "D:/work", "D:/work/fcc/build/x.cpp",
    ])
    def test_non_device_paths(self, p):
        assert _is_windows_device_path(p) is False

    def test_workdir_ancestor(self):
        wd = "D:/work/fcc"
        assert _is_workdir_ancestor("D:/work", wd) is True       # 父目录
        assert _is_workdir_ancestor("D:/", wd) is True           # 盘根（同为祖先）
        assert _is_workdir_ancestor(wd, wd) is True              # workdir 本身
        assert _is_workdir_ancestor("D:/WORK", wd) is True       # 大小写不敏感
        assert _is_workdir_ancestor("D:\\work", wd) is True      # 反斜杠归一
        # 裸盘符 "D:"（无斜杠）是 drive-relative（D盘当前目录），语义不明，
        # 非绝对路径不参与祖先判定；该形态 token 在提取层走相对 join + 存在
        # 性过滤，不会进入备份列表
        assert _is_workdir_ancestor("D:", wd) is False
        assert _is_workdir_ancestor("D:/other", wd) is False     # 盘内旁支
        assert _is_workdir_ancestor("C:/work", wd) is False      # 跨盘
        assert _is_workdir_ancestor("D:/work/fcc2", wd) is False  # 前缀陷阱
        assert _is_workdir_ancestor("rel/path", wd) is False     # 相对路径

    def test_workdir_ancestor_posix(self):
        wd = "/home/u/proj"
        assert _is_workdir_ancestor("/home/u", wd) is True
        assert _is_workdir_ancestor("/home", wd) is True
        assert _is_workdir_ancestor("/home/u/proj", wd) is True
        assert _is_workdir_ancestor("/home/u/other", wd) is False


class TestFilters:
    """_filter_unbackupable / _filter_manifest_paths / manifest_add"""

    def test_filter_unbackupable_excludes_guards(self):
        kept = ToolBackupManager._filter_unbackupable(
            [
                "D:/work",                       # workdir 祖先（task#2332）
                "D:/work/fcc",                   # workdir 本身
                "E:/x/nul",                      # 设备名
                "E:/some/normal.txt",            # 正常外部文件
            ],
            workdir="D:/work/fcc",
        )
        assert kept == ["E:/some/normal.txt"]

    def test_filter_manifest_paths_with_workdir(self):
        files = [
            "D:/work", "D:/work/fcc", "D:/work/fcc/nul",
            "E:/x/nul.txt", "C:/Users/u/keys.txt", "D:/work/fcc/ok.cpp",
        ]
        kept = ToolBackupManager._filter_manifest_paths(files, "D:/work/fcc")
        # D:/work/fcc/ok.cpp 在 workdir 内：manifest 是恢复集，workdir 内文件
        # 由 git 快照负责——但 _filter_manifest_paths 轻过滤不剔 git/workdir
        # 内文件（迭代快照的 nested git 文件也在 workdir 内）→ 保留
        assert "D:/work" not in kept
        assert "D:/work/fcc" not in kept
        assert "D:/work/fcc/nul" not in kept
        assert "E:/x/nul.txt" not in kept
        assert "C:/Users/u/keys.txt" in kept
        assert "D:/work/fcc/ok.cpp" in kept

    def test_filter_manifest_paths_no_workdir(self):
        # 不提供 workdir 时只剔设备名/系统路径（向后兼容旧调用）
        kept = ToolBackupManager._filter_manifest_paths(
            ["D:/work", "D:/x/nul", "E:/ok.txt"]
        )
        assert kept == ["D:/work", "E:/ok.txt"]

    def test_manifest_add_skips_device(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            tool_backup, "_get_rollback_ids_base", lambda: str(tmp_path / "rid")
        )
        manifest_add(997001, ["E:/a/nul", "E:/a/ok.txt"])
        files = manifest_get(997001)
        assert files == ["E:/a/ok.txt"]


class TestExtractGuards:
    """命令路径提取源头剔除（task#2332：`2>nul` 与动词参数 D:/work）"""

    @pytest.fixture
    def always_exist(self, monkeypatch):
        """模拟 Windows 设备伪存在：所有候选视为存在，验证过滤独立于存在性"""
        def fake(paths, nest_name=None):
            return set(paths)

        monkeypatch.setattr(tool_backup, "_paths_exist_batch", fake)
        return None

    def test_redirect_nul_not_extracted(self, always_exist):
        out = ToolBackupManager._extract_paths_from_command(
            "dir tools\\paycrypt.cpp 2>nul", workdir="D:/work/fcc"
        )
        assert not any(p.endswith("/nul") or p.lower() == "nul" for p in out)

    def test_workdir_ancestor_not_extracted(self, always_exist):
        out = ToolBackupManager._extract_paths_from_command(
            "cp D:/work D:/work/fcc/x.txt && dir D:\\work", workdir="D:/work/fcc"
        )
        assert "D:/work" not in out
        assert "D:\\work" not in out
        assert not any(p.replace("\\", "/") == "D:/work" for p in out)

    def test_normal_paths_still_extracted(self, always_exist):
        out = ToolBackupManager._extract_paths_from_command(
            "rm E:/tmp/scratch/old.txt", workdir="D:/work/fcc"
        )
        # 提取层输出保留命令原样的分隔符形态（反斜杠），归一化后比较
        assert any(
            p.replace("\\", "/") == "E:/tmp/scratch/old.txt" for p in out
        )


class TestLocalCheckRestore:
    """本地 check_changes / restore 的目录与设备名护栏"""

    def _commit_file(self, repo: Path, rel: str, content: str) -> str:
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", f"add {rel}")
        r = _git(repo, "rev-parse", "HEAD")
        return r.stdout.decode().strip()

    def test_check_changes_skips_dir(self, default_repo, tmp_path):
        """盘上路径变成目录后不再恒报'已变更'（task#2332 的 D:/work）"""
        f = tmp_path / "proj" / "a.txt"
        f.parent.mkdir(parents=True)
        f.write_text("v1", encoding="utf-8")
        commit = ExternalFileBackup.backup(997001, [str(f)], "l1")
        assert commit
        # 文件改为目录（模拟 D:/work 场景）
        f.unlink()
        f.mkdir()
        (f / "child.txt").write_text("x", encoding="utf-8")
        changed = ExternalFileBackup.check_changes(997001, [str(f)], commit)
        assert changed == []

    def test_check_changes_real_change_still_detected(self, default_repo, tmp_path):
        """护栏不吞真实变更：内容改了必须报"""
        f = tmp_path / "proj" / "b.txt"
        f.parent.mkdir(parents=True)
        f.write_text("v1", encoding="utf-8")
        commit = ExternalFileBackup.backup(997001, [str(f)], "l1")
        f.write_text("v2", encoding="utf-8")
        changed = ExternalFileBackup.check_changes(997001, [str(f)], commit)
        assert changed == [str(f)]

    def test_restore_skips_dir_and_nonblob(
        self, default_repo, tmp_path, monkeypatch
    ):
        """目标是目录 / 基线是 tree（曾整目录 copytree）→ 跳过且不炸脚本"""
        # 构造含目录 tree 的基线（模拟旧代码 copytree 的 D:/work 备份）
        (default_repo / "d" / "work").mkdir(parents=True)
        (default_repo / "d" / "work" / "inside.txt").write_text(
            "old", encoding="utf-8"
        )
        _git(default_repo, "add", "-A")
        _git(default_repo, "commit", "-qm", "dir baseline")
        base = _git(default_repo, "rev-parse", "HEAD").stdout.decode().strip()

        # 目标路径真实存在为目录
        target_dir = tmp_path / "work"
        target_dir.mkdir()
        # 设备名路径
        dev_path = tmp_path / "proj" / "nul"

        n = ExternalFileBackup._restore_local(
            997001, [str(target_dir), str(dev_path)], base
        )
        assert n == 0
        assert not dev_path.exists()

    def test_restore_normal_file(self, default_repo, tmp_path):
        """正常文件恢复不受影响"""
        f = tmp_path / "proj" / "c.txt"
        f.parent.mkdir(parents=True)
        f.write_text("v1", encoding="utf-8")
        commit = ExternalFileBackup.backup(997001, [str(f)], "l1")
        f.write_text("v2", encoding="utf-8")
        n = ExternalFileBackup._restore_local(997001, [str(f)], commit)
        assert n == 1
        assert f.read_text(encoding="utf-8") == "v1"


class TestSnapshotTransientGuard:
    """🔧 事务#2402：快照临时失败不写粘性探测缓存（#2354 事故回归防护）

    事故：epyc WebSocket 1006 瞬断 70ms 后重连，但快照失败被写入
    _NON_GIT_WD_CACHE（600s TTL），后续 26 个迭代快照全部降级
    history_only（无代码备份），回滚窗口全程不显示项目代码变更。
    """

    def test_transient_error_not_cached(self, monkeypatch):
        monkeypatch.setattr(
            ToolBackupManager, "_NON_GIT_WD_CACHE", {}, raising=False
        )
        calls = []

        def fake_snapshot_git(*a, **kw):
            calls.append(1)
            raise _SnapshotTransientError("nest 'epyc' 未连接")

        monkeypatch.setattr(
            ToolBackupManager, "_snapshot_git", staticmethod(fake_snapshot_git)
        )
        meta = ToolBackupManager._snapshot_git_dispatch(
            "D:/work/fcc", 1, "bash", {}, "m", "t", "epyc", [],
        )
        assert meta is None
        # 关键：临时失败不写缓存——下轮快照会重试而非直接跳过
        assert "D:/work/fcc" not in ToolBackupManager._NON_GIT_WD_CACHE

    def test_real_non_git_cached(self, monkeypatch):
        monkeypatch.setattr(
            ToolBackupManager, "_NON_GIT_WD_CACHE", {}, raising=False
        )

        def fake_snapshot_git(*a, **kw):
            return None  # 真"非git workdir"（rev-parse --git-dir failed）

        monkeypatch.setattr(
            ToolBackupManager, "_snapshot_git", staticmethod(fake_snapshot_git)
        )
        meta = ToolBackupManager._snapshot_git_dispatch(
            "D:/work/plain", 1, "bash", {}, "m", "t", "epyc", [],
        )
        assert meta is None
        # 确定性"非git"仍写缓存（防每轮白跑探测 HTTP）
        assert "D:/work/plain" in ToolBackupManager._NON_GIT_WD_CACHE

    def test_transient_then_retry_succeeds(self, monkeypatch):
        """瞬态失败后下一轮成功：缓存未污染 → 重试后正常返回快照 meta"""
        monkeypatch.setattr(
            ToolBackupManager, "_NON_GIT_WD_CACHE", {}, raising=False
        )
        state = {"n": 0}

        def fake_snapshot_git(*a, **kw):
            state["n"] += 1
            if state["n"] == 1:
                raise _SnapshotTransientError("命令行太长。")
            return {"type": "git", "commit_hash": "abc123"}

        monkeypatch.setattr(
            ToolBackupManager, "_snapshot_git", staticmethod(fake_snapshot_git)
        )
        assert ToolBackupManager._snapshot_git_dispatch(
            "D:/work/fcc", 1, "bash", {}, "m", "t", "epyc", [],
        ) is None
        m2 = ToolBackupManager._snapshot_git_dispatch(
            "D:/work/fcc", 1, "bash", {}, "m", "t", "epyc", [],
        )
        assert m2 == {"type": "git", "commit_hash": "abc123"}
        assert state["n"] == 2  # 第二次真的执行了（没被缓存挡住）

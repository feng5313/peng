"""
测试 备份排除规则（事务#1660）：不备份未修改/无关产物文件

背景：git 快照 `add -A` 会把 .gitignore 忽略的未修改产物（build/、*.dll 等）
全部写入备份对象库，且真实修改后几乎无内容重复，每次快照都新增整套 blob，
浪费大量硬盘空间。

验证：
1. 排除规则纯函数：_is_excluded_dirname/_filter_excluded_paths/_is_abs_path_excluded
2. git 快照路径：build/、build_*/、.gitignore 命中文件不进快照 tree；
   build-only 变更 → 快照去重复用 commit、变更检测返回 None
3. 非 git 路径：增量同步跳过排除目录与 .gitignore 文件；未修改文件不重复制
4. 外部文件备份：排除目录路径跳过
5. 回滚回归：正常修改文件仍可回滚/撤销
"""
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from peng.service.tool_backup import (
    ExternalFileBackup,
    ToolBackupManager,
    _filter_excluded_paths,
    _is_abs_path_excluded,
    _is_excluded_dirname,
    _need_copy,
    _sync_dir_incremental,
)


# ==================== 排除规则纯函数 ====================


class TestExcludeRules:
    def test_dirname_match(self):
        assert _is_excluded_dirname("build")
        assert _is_excluded_dirname("build_x86")
        assert _is_excluded_dirname("build_llvm")
        assert _is_excluded_dirname(".peng")
        assert _is_excluded_dirname(".cache")

    def test_dirname_no_match(self):
        assert not _is_excluded_dirname("src")
        assert not _is_excluded_dirname("builder")  # 前缀但非 build_
        assert not _is_excluded_dirname("mybuild")
        assert not _is_excluded_dirname("rebuild")

    def test_filter_index_paths(self):
        paths = [
            "build/a.o",
            "src/a.c",
            "sub/build/b.o",
            "sub/build_x86/c.o",
            "buildx/d.o",  # 不排除
            "build.c",  # 文件名不参与匹配
        ]
        assert _filter_excluded_paths(paths) == [
            "build/a.o",
            "sub/build/b.o",
            "sub/build_x86/c.o",
        ]

    def test_abs_path_windows_style(self):
        assert _is_abs_path_excluded("D:/work/proj/build/x.dll")
        assert _is_abs_path_excluded("D:\\work\\proj\\build_x86\\x.dll")
        assert not _is_abs_path_excluded("D:/work/proj/src/main.c")
        assert not _is_abs_path_excluded("/a/build.c")


# ==================== git 快照路径 ====================


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_text("*.log\n*.dll\nbuild/\nbuild_*/\n")
    for d in ("src", "build", "build_x86", "sub", "res"):
        (repo / d).mkdir()
    (repo / "sub/build").mkdir()
    (repo / "src/main.c").write_text("int main(){}")
    (repo / "build/art.o").write_text("artifact")
    (repo / "build_x86/art2.o").write_text("artifact2")
    (repo / "sub/build/n.o").write_text("nested")
    (repo / "res/lib.dll").write_text("ignored dll")
    (repo / "run.log").write_text("ignored log")
    (repo / "res/data.txt").write_text("tracked")
    _git(repo, "add", ".gitignore", "src", "res/data.txt")
    _git(repo, "commit", "-m", "init", "--allow-empty")
    return repo


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=15
    )


def _tree_files(repo: Path, commit: str) -> list[str]:
    r = _git(repo, "ls-tree", "-r", "--name-only", commit)
    return r.stdout.split()


class TestGitSnapshotExclusion:
    def test_build_dirs_not_in_snapshot(self, git_repo):
        m = ToolBackupManager.create_snapshot(
            task_id=99101, tool_name="write",
            tool_args={"filePath": "src/main.c", "content": "x"},
            workdir=str(git_repo),
        )
        assert m is not None and m["type"] == "git"
        files = _tree_files(git_repo, m["commit_hash"])
        assert "src/main.c" in files
        assert not any(
            f.startswith(("build/", "build_x86/", "sub/build/")) for f in files
        ), "build 目录泄漏进快照"
        assert "res/lib.dll" not in files and "run.log" not in files

    def test_build_only_change_reuses_commit(self, git_repo):
        m1 = ToolBackupManager.create_snapshot(
            task_id=99102, tool_name="write",
            tool_args={"filePath": "src/main.c", "content": "x"},
            workdir=str(git_repo),
        )
        (git_repo / "build/art.o").write_text("artifact MODIFIED")
        m2 = ToolBackupManager.create_snapshot(
            task_id=99102, tool_name="write",
            tool_args={"filePath": "src/main.c", "content": "y"},
            workdir=str(git_repo),
        )
        assert m2["commit_hash"] == m1["commit_hash"], "build-only 变更应复用commit（去重）"

    def test_src_change_creates_commit(self, git_repo):
        m1 = ToolBackupManager.create_snapshot(
            task_id=99103, tool_name="write",
            tool_args={"filePath": "src/main.c", "content": "x"},
            workdir=str(git_repo),
        )
        (git_repo / "src/main.c").write_text("int main(){return 1;}")
        m2 = ToolBackupManager.create_snapshot(
            task_id=99103, tool_name="write",
            tool_args={"filePath": "src/main.c", "content": "y"},
            workdir=str(git_repo),
        )
        assert m2["commit_hash"] != m1["commit_hash"]
        assert "src/main.c" in _tree_files(git_repo, m2["commit_hash"])

    def test_change_detection_ignores_build(self, git_repo):
        m = ToolBackupManager.create_snapshot(
            task_id=99104, tool_name="write",
            tool_args={"filePath": "src/main.c", "content": "x"},
            workdir=str(git_repo),
        )
        (git_repo / "build/art.o").write_text("artifact CHANGED AGAIN")
        r = ToolBackupManager.check_post_tool_changes(dict(m), str(git_repo))
        assert r is None, "build-only 变更应检测为无变化"

    def test_change_detection_catches_src(self, git_repo):
        m = ToolBackupManager.create_snapshot(
            task_id=99105, tool_name="write",
            tool_args={"filePath": "src/main.c", "content": "x"},
            workdir=str(git_repo),
        )
        (git_repo / "src/main.c").write_text("modified")
        r = ToolBackupManager.check_post_tool_changes(dict(m), str(git_repo))
        assert r is not None and "src/main.c" in r["changed_files"]


# ==================== 非git路径（增量同步） ====================


class TestIncrementalSync:
    def test_skip_build_and_gitignore(self, tmp_path):
        src = tmp_path / "wd"
        dst = tmp_path / "bk"
        src.mkdir()
        dst.mkdir()
        (src / ".gitignore").write_text("*.log\nbuild/\n")
        (src / "src").mkdir()
        (src / "src/a.txt").write_text("a")
        (src / "build").mkdir()
        (src / "build/x.o").write_text("artifact")
        (src / "t.log").write_text("log")
        (src / "keep.txt").write_text("keep")

        copied, removed = _sync_dir_incremental(src, dst, spec=None)
        # .gitignore 由调用方加载后传入；此处验证 build 目录被强制排除
        rels = {str(p.relative_to(dst)).replace(os.sep, "/") for p in dst.rglob("*") if p.is_file()}
        assert "src/a.txt" in rels and "keep.txt" in rels
        assert not any(r.startswith("build/") for r in rels), "build 目录泄漏"

    def test_incremental_no_recopy(self, tmp_path):
        src, dst = tmp_path / "s", tmp_path / "d"
        src.mkdir()
        dst.mkdir()
        (src / "a.txt").write_text("stable")
        _sync_dir_incremental(src, dst, spec=None)
        # 未修改（size/mtime一致）→ 不再复制
        c2, _ = _sync_dir_incremental(src, dst, spec=None)
        assert c2 == 0

    def test_need_copy_semantics(self, tmp_path):
        import shutil

        a, b = tmp_path / "a.txt", tmp_path / "b.txt"
        a.write_text("same")
        shutil.copy2(a, b)  # copy2 保留 mtime → 判定未修改
        assert not _need_copy(a, b)
        b.write_text("changed-longer")
        assert _need_copy(a, b)
        assert _need_copy(a, tmp_path / "nonexistent")

    def test_removed_files_purged(self, tmp_path):
        src, dst = tmp_path / "s", tmp_path / "d"
        src.mkdir()
        dst.mkdir()
        (src / "a.txt").write_text("a")
        _sync_dir_incremental(src, dst, spec=None)
        (dst / "stale.txt").write_text("stale")
        _sync_dir_incremental(src, dst, spec=None)
        assert not (dst / "stale.txt").exists(), "目标端多余文件应被清理"


# ==================== 外部文件备份排除 ====================


class TestExternalFileExclusion:
    def test_build_path_skipped(self, tmp_path):
        with patch("peng.config.loader.load_config") as mock_cfg:
            FakePaths = type("P", (), {"data_dir_path": tmp_path})
            mock_cfg.return_value = type("C", (), {"paths": FakePaths()})()

            normal = tmp_path / "outside/normal.txt"
            normal.parent.mkdir(parents=True)
            normal.write_text("config")
            build_file = tmp_path / "outside/build/obj.o"
            build_file.parent.mkdir(parents=True)
            build_file.write_text("artifact")

            commit = ExternalFileBackup.backup(99201, [str(normal), str(build_file)], "t")
            assert commit, "普通外部文件应正常备份"
            repo = tmp_path / "file_backups/task#99201"
            r = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", "HEAD"],
                cwd=str(repo), capture_output=True, text=True, timeout=10,
            )
            files = r.stdout.split()
            assert any("normal.txt" in f for f in files)
            assert not any("/build/" in f for f in files), "build 路径泄漏进外部备份"


# ==================== 回滚回归 ====================


class TestRollbackRegression:
    def test_normal_rollback_still_works(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / ".gitignore").write_text("build/\n")
        (repo / "src").mkdir()
        (repo / "src/app.py").write_text("v1")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "init")
        m = ToolBackupManager.create_snapshot(
            task_id=99301, tool_name="edit",
            tool_args={"filePath": "src/app.py"},
            workdir=str(repo),
        )
        (repo / "src/app.py").write_text("v2")
        meta = dict(m)
        ToolBackupManager.check_post_tool_changes(meta, str(repo))
        ok, msg, undo, changed = ToolBackupManager.rollback(meta)
        assert ok and changed
        assert (repo / "src/app.py").read_text() == "v1"
        if undo:
            ok2, _, _, _ = ToolBackupManager.rollback(undo)
            assert ok2
            assert (repo / "src/app.py").read_text() == "v2"

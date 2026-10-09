"""
测试非git workdir 快照的 external 统一备份模式（事务#1847 第七轮·初始设计还原）

架构：\.peng\backups 根目录是唯一 git 环境，所有 task 共用；备份文件按绝对
路径映射为仓库子路径（D:\\works\\t.txt → d/works/t.txt），不放 task#id 子目录。
commit 消息含事务id+原路径映射+一键还原命令。绝不扫描/镜像整个 workdir。
"""
import subprocess
from pathlib import Path

import pytest

import peng.service.tool_backup as tb
from peng.service.tool_backup import ExternalFileBackup, ToolBackupManager


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=10)


TASK = 998030


@pytest.fixture
def env(tmp_path, monkeypatch):
    """非git workdir + backups 根仓库（mock 盘符路由到 tmp）"""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    backups_root = tmp_path / "backups"
    backups_root.mkdir()
    (workdir / "src.txt").write_text("v1", encoding="utf-8")

    monkeypatch.setattr(
        ExternalFileBackup,
        "_split_drive_key",
        staticmethod(lambda fp: "D" if "wd" in str(fp) or "wd_ext" in str(fp) else None),
    )
    monkeypatch.setattr(
        ExternalFileBackup, "_repo_path_for_key", staticmethod(lambda tid, key: str(backups_root))
    )
    monkeypatch.setattr(
        ExternalFileBackup, "_get_backup_repo", staticmethod(lambda tid: str(tmp_path / "fb_default"))
    )
    return {"workdir": workdir, "root": backups_root, "tmp": tmp_path}


class TestCreateSnapshotExternal:
    def test_returns_external_meta_and_maps_path(self, env, monkeypatch):
        """非git workdir：快照→type=external，文件入 backups 根仓库 d/<路径>"""
        f = env["workdir"] / "src.txt"
        meta = ToolBackupManager.create_snapshot(
            TASK, "edit", {"filePath": str(f)}, workdir=str(env["workdir"]), nest_name=None,
        )
        assert meta and meta["type"] == "external"
        assert meta["external_files"] == [str(f)]
        assert meta["external_backup_commit"]
        # 初始设计：备份文件 = 盘符小写/完整路径（workdir 内文件同样进备份仓库）
        bak = env["root"] / "d" / Path(*str(f).lstrip("/").split("/"))
        assert bak.is_file() and bak.read_text(encoding="utf-8") == "v1"
        # 备份仓库在根（.git 直接位于 backups 下，无 task#id 子目录）
        assert (env["root"] / ".git").is_dir()
        assert not (env["root"] / f"task#{TASK}").exists()

    def test_no_files_returns_none(self, env):
        """无涉及文件（如 iteration_start args={}）→ None，绝不扫描 workdir"""
        meta = ToolBackupManager.create_snapshot(
            TASK, "iteration_start", {}, workdir=str(env["workdir"]), nest_name=None,
        )
        assert meta is None

    def test_commit_message_spec(self, env):
        """commit 消息：事务id + 原路径->备份路径 + 一键还原命令（cp/copy）"""
        f = env["workdir"] / "src.txt"
        ToolBackupManager.create_snapshot(
            TASK, "edit", {"filePath": str(f)}, workdir=str(env["workdir"]), nest_name=None,
        )
        r = _git(env["root"], "log", "-1", "--pretty=%B")
        msg = r.stdout
        assert f"task#{TASK}" in msg
        assert "-> " in msg and str(f) in msg
        assert "one-key restore (version of this commit):" in msg
        # 🔧 历史版本还原：:/消息前缀 定位本 commit + git checkout + copy
        assert "checkout \":/peng backup task#" in msg
        assert " && copy /y " in msg  # 盘符仓库（Windows）→ copy /y；Linux 默认仓库 → cp


class TestCheckPostToolExternal:
    def test_detect_change_and_none_when_clean(self, env):
        f = env["workdir"] / "src.txt"
        meta = ToolBackupManager.create_snapshot(
            TASK, "edit", {"filePath": str(f)}, workdir=str(env["workdir"]), nest_name=None,
        )
        assert ToolBackupManager.check_post_tool_changes(
            dict(meta), str(env["workdir"]), None
        ) is None
        f.write_text("v2", encoding="utf-8")
        det = ToolBackupManager.check_post_tool_changes(dict(meta), str(env["workdir"]), None)
        assert det is not None and str(f) in det["changed_files"]

    def test_rollback_restores(self, env):
        f = env["workdir"] / "src.txt"
        meta = ToolBackupManager.create_snapshot(
            TASK, "edit", {"filePath": str(f)}, workdir=str(env["workdir"]), nest_name=None,
        )
        f.write_text("v2-modified", encoding="utf-8")
        det = ToolBackupManager.check_post_tool_changes(dict(meta), str(env["workdir"]), None)
        assert det is not None
        ok, msg, _undo, changed = ToolBackupManager.rollback(det)
        assert ok and changed
        assert f.read_text(encoding="utf-8") == "v1"


class TestFilterUnbackupable:
    def test_nested_git_repo_file_skipped(self, env):
        """嵌套 git 仓库内文件（git管理）跳过"""
        import subprocess as sp
        repo = env["workdir"] / "subrepo"
        repo.mkdir()
        sp.run(["git", "init"], cwd=str(repo), capture_output=True)
        f = repo / "a.txt"
        f.write_text("x", encoding="utf-8")
        out = ToolBackupManager._filter_unbackupable([str(f)], str(env["workdir"]))
        assert out == []

    def test_system_paths_skipped(self):
        out = ToolBackupManager._filter_unbackupable(
            [r"C:\Program Files\VS\vcvarsall.bat",
             r"C:\Users\feng\AppData\Local\Temp\fcc_log.txt",
             r"D:\work\t.py"],
            r"D:\work",
        )
        assert out == [r"D:\work\t.py"]

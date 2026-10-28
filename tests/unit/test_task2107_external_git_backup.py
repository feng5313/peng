"""🔧 事务#2107 回归测试：非工作目录 git 管理文件的备份/快照缺陷

缺陷根因：_filter_git_managed（事务#2088）剔除所有位于任何 git 仓库内的
文件，其假设"git 仓库文件由所属事务的 git 分支快照负责回滚"仅当该仓库
**是事务 workdir** 时成立——外部 git 仓库 / workdir 内嵌套子仓库的文件
被剔除后：workdir git 快照不覆盖（外部）或只记 gitlink 无法恢复内容
（嵌套）+ external 备份剔除 = 零备份零回滚！

修复（scope 语义 + manifest 精确恢复集）：
1. scope="workdir"（新备份）：仅剔除 workdir 所属 git 仓库内文件；
   外部/嵌套 git 仓库文件保留进 external 备份
2. scope="all"（旧 meta 兜底）：保持旧行为（全量剔除，防 #2088 误备份
   数据被恢复）
3. meta.backup_policy="workdir_scope" 区分新旧
4. manifest（data/external_manifests/task-<id>.json）：本事务 external
   备份文件的累积精确清单；迭代快照 meta 记录 external_manifest；
   迭代回滚/检测/预览恢复集以 manifest 为准（防共享盘符仓库 ls-tree
   全量恢复混入他 task 文件 → #2080 跨事务覆盖事故）
"""

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from peng.service import tool_backup as tb  # noqa: E402
from peng.service.tool_backup import (  # noqa: E402
    ExternalFileBackup,
    ToolBackupManager,
    manifest_add,
    manifest_get,
)


# ==================== 测试环境构造 ====================


def _git_init(path: Path) -> Path:
    """初始化一个空 git 仓库（仅 .git 存在即可，无需 commit）"""
    path.mkdir(parents=True, exist_ok=True)
    import subprocess

    subprocess.run(
        ["git", "init"], cwd=str(path), capture_output=True, timeout=15, check=True
    )
    return path


@pytest.fixture
def repos(tmp_path):
    """构造：projA(git) + projA/subrepo(嵌套git) + projB(外部git) + plain(非git)"""
    proj_a = _git_init(tmp_path / "projA")
    sub_repo = _git_init(tmp_path / "projA" / "subrepo")
    proj_b = _git_init(tmp_path / "projB")
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()

    fa = proj_a / "a.py"
    fa.write_text("a", encoding="utf-8")
    fb = proj_b / "b.py"
    fb.write_text("b", encoding="utf-8")
    fc = sub_repo / "c.py"
    fc.write_text("c", encoding="utf-8")
    fp_ = plain_dir / "p.py"
    fp_.write_text("p", encoding="utf-8")

    return {
        "root": tmp_path,
        "projA": proj_a,
        "subrepo": sub_repo,
        "projB": proj_b,
        "plain": plain_dir,
        "fa": str(fa),
        "fb": str(fb),
        "fc": str(fc),
        "fp": str(fp_),
    }


@pytest.fixture
def manifest_dir(tmp_path, monkeypatch):
    """manifest 持久化目录重定向到 tmp（隔离 data/rollback_ids 依赖）"""
    fake_base = tmp_path / "rollback_ids"
    fake_base.mkdir()
    monkeypatch.setattr(tb, "_get_rollback_ids_base", lambda: str(fake_base))
    tb._ROLLBACK_IDS_DIR = None  # 清模块级缓存
    yield tmp_path / "external_manifests"
    tb._ROLLBACK_IDS_DIR = None


# ==================== 1. git 根判定辅助 ====================


class TestGitRootHelpers:
    def test_git_root_local(self, repos):
        assert ToolBackupManager._git_root_local(repos["fa"]) == str(repos["projA"])
        assert ToolBackupManager._git_root_local(repos["fc"]) == str(repos["subrepo"])
        assert ToolBackupManager._git_root_local(repos["fb"]) == str(repos["projB"])
        assert ToolBackupManager._git_root_local(repos["fp"]) is None

    def test_git_root_local_win_style_on_linux(self):
        # Linux 服务端无法 stat Windows 盘符路径 → None（保守不过滤）
        if os.name == "nt":
            pytest.skip("Linux 服务端行为")
        assert ToolBackupManager._git_root_local(r"D:\work\x\a.py") is None

    def test_workdir_git_root_local(self, repos):
        got = ToolBackupManager._workdir_git_root_local(str(repos["projA"]))
        assert got is not None
        assert ToolBackupManager._path_key(got) == ToolBackupManager._path_key(
            str(repos["projA"])
        )
        assert ToolBackupManager._workdir_git_root_local(str(repos["plain"])) is None

    def test_path_key_norm(self):
        k1 = ToolBackupManager._path_key(r"D:\Work\FCC")
        k2 = ToolBackupManager._path_key("d:/work/fcc/")
        assert k1 == k2  # Windows 盘符路径大小写+斜杠归一


# ==================== 2. _filter_git_managed scope 语义 ====================


class TestFilterGitManaged:
    def test_scope_all_legacy(self, repos):
        """旧语义：所有 git 仓库内文件剔除（历史 meta 兜底）"""
        out = ToolBackupManager._filter_git_managed(
            [repos["fa"], repos["fb"], repos["fc"], repos["fp"]],
            workdir=str(repos["projA"]), nest_name=None, scope="all",
        )
        assert out == [repos["fp"]]

    def test_scope_workdir_keeps_foreign_repo(self, repos):
        """新语义：仅剔除 workdir 仓库内文件；外部 git 仓库文件保留！"""
        out = ToolBackupManager._filter_git_managed(
            [repos["fa"], repos["fb"], repos["fp"]],
            workdir=str(repos["projA"]), nest_name=None, scope="workdir",
        )
        assert repos["fa"] not in out  # workdir 仓库内 → 剔除（快照负责）
        assert repos["fb"] in out  # 外部 git 仓库 → 保留（核心修复）
        assert repos["fp"] in out  # 非 git → 保留

    def test_scope_workdir_keeps_nested_repo(self, repos):
        """新语义：workdir 内嵌套 git 仓库文件保留（快照只记 gitlink）"""
        out = ToolBackupManager._filter_git_managed(
            [repos["fa"], repos["fc"]],
            workdir=str(repos["projA"]), nest_name=None, scope="workdir",
        )
        assert repos["fa"] not in out
        assert repos["fc"] in out  # 嵌套仓库 → 保留（核心修复）

    def test_scope_workdir_non_git_workdir_keeps_all(self, repos):
        """workdir 非 git 仓库：无仓库快照负责 → 全保留（含嵌套仓库文件）"""
        out = ToolBackupManager._filter_git_managed(
            [repos["fa"], repos["fb"], repos["fc"], repos["fp"]],
            workdir=str(repos["plain"]), nest_name=None, scope="workdir",
        )
        assert sorted(out) == sorted(
            [repos["fa"], repos["fb"], repos["fc"], repos["fp"]]
        )

    def test_scope_workdir_foreign_workdir(self, repos):
        """workdir=projB：projB 文件剔除，projA（现在成了外部仓库）保留"""
        out = ToolBackupManager._filter_git_managed(
            [repos["fa"], repos["fb"]], workdir=str(repos["projB"]),
            nest_name=None, scope="workdir",
        )
        assert repos["fb"] not in out
        assert repos["fa"] in out

    def test_filter_unbackupable_scope_passthrough(self, repos):
        out = ToolBackupManager._filter_unbackupable(
            [repos["fa"], repos["fb"]], workdir=str(repos["projA"]),
            nest_name=None, scope="workdir",
        )
        assert out == [repos["fb"]]
        # 系统路径仍剔除（任何 scope）
        out2 = ToolBackupManager._filter_unbackupable(
            [r"C:\Windows\System32\x.dll", repos["fb"]],
            workdir=str(repos["projA"]), nest_name=None, scope="workdir",
        )
        assert out2 == [repos["fb"]]


# ==================== 3. _meta_scope / manifest 轻过滤 ====================


class TestMetaScopeAndManifestFilter:
    def test_meta_scope(self):
        assert ToolBackupManager._meta_scope({}) == "all"
        assert ToolBackupManager._meta_scope({"backup_policy": "other"}) == "all"
        assert (
            ToolBackupManager._meta_scope({"backup_policy": "workdir_scope"})
            == "workdir"
        )

    def test_filter_manifest_paths(self, repos):
        out = ToolBackupManager._filter_manifest_paths(
            [
                repos["fb"],  # git 仓库文件：保留（manifest 是精确集）
                r"C:\Windows\System32\x.dll",  # 系统路径：剔除
                "D:/",  # 盘根：剔除
                "D:/work/proj/build/out.exe",  # 排除目录：剔除
            ]
        )
        assert out == [repos["fb"]]


# ==================== 4. manifest 持久化 ====================


class TestManifest:
    def test_add_and_get_union(self, manifest_dir):
        manifest_add(991, ["/a/b.txt", "/c/d.txt"])
        manifest_add(991, ["/c/d.txt", "/e/f.txt"])  # 并集去重
        got = manifest_get(991)
        assert sorted(got) == ["/a/b.txt", "/c/d.txt", "/e/f.txt"]
        assert (manifest_dir / "task-991.json").exists()

    def test_get_empty(self, manifest_dir):
        assert manifest_get(999123) == []

    def test_isolated_per_task(self, manifest_dir):
        manifest_add(1, ["/x"])
        manifest_add(2, ["/y"])
        assert manifest_get(1) == ["/x"]
        assert manifest_get(2) == ["/y"]


# ==================== 5. create_snapshot 集成（本地 git workdir） ====================


def _patch_snapshot_git_dispatch(monkeypatch, meta=None, disabled=False):
    """mock git 快照分派（避免真实 git add/commit），返回预置 meta

    disabled=True 时返回 None（模拟非 git workdir）
    """
    calls = {}

    def fake(workdir, task_id, tool_name, tool_args, commit_msg, timestamp,
             nest_name=None, internal_files=None):
        calls["internal_files"] = internal_files
        if disabled:
            return None
        m = {
            "type": "git", "task_id": task_id, "tool_name": tool_name,
            "commit_hash": "deadbeef", "branch_name": f"peng_backups/task#{task_id}",
            "workdir": str(workdir), "nest_name": nest_name or "local",
            "file_paths": [], "timestamp": timestamp, "rollback_cmd": "",
        }
        return meta if meta is not None else m

    monkeypatch.setattr(ToolBackupManager, "_snapshot_git_dispatch", fake)
    return calls


def _patch_ext_backup(monkeypatch):
    """捕获 ExternalFileBackup.backup 的文件列表"""
    captured = {}

    def fake(task_id, file_paths, label="", nest_name=None):
        captured["files"] = list(file_paths)
        return "extcommit0"

    monkeypatch.setattr(ExternalFileBackup, "backup", staticmethod(fake))
    return captured


class TestCreateSnapshot:
    def test_foreign_git_repo_file_backed_up(self, repos, manifest_dir, monkeypatch):
        """核心缺陷修复：workdir 外 git 仓库文件必须进 external 备份"""
        _patch_snapshot_git_dispatch(monkeypatch)
        captured = _patch_ext_backup(monkeypatch)
        meta = ToolBackupManager.create_snapshot(
            task_id=777, tool_name="edit",
            tool_args={"filePath": repos["fb"]},
            workdir=str(repos["projA"]), nest_name=None,
        )
        assert meta is not None
        assert meta["type"] == "git"
        assert meta["backup_policy"] == "workdir_scope"
        # 外部 git 仓库文件进 external 备份（修复前被 _filter_git_managed 剔除）
        assert captured["files"] == [repos["fb"]]
        assert meta["external_files"] == [repos["fb"]]
        assert meta["external_backup_commit"] == "extcommit0"
        # manifest 累积
        assert manifest_get(777) == [repos["fb"]]

    def test_workdir_file_not_in_external(self, repos, manifest_dir, monkeypatch):
        """workdir 仓库内文件不进 external（git 分支快照负责）"""
        calls = _patch_snapshot_git_dispatch(monkeypatch)
        captured = _patch_ext_backup(monkeypatch)
        meta = ToolBackupManager.create_snapshot(
            task_id=778, tool_name="edit",
            tool_args={"filePath": repos["fa"]},
            workdir=str(repos["projA"]), nest_name=None,
        )
        assert meta is not None
        assert "external_files" not in meta
        assert captured.get("files") is None  # 未调用 external 备份
        assert manifest_get(778) == []
        # internal_files 已传给 dispatch（嵌套检测输入）
        assert calls["internal_files"] == [repos["fa"]]

    def test_nested_repo_file_backed_up(self, repos, manifest_dir, monkeypatch):
        """workdir 内嵌套 git 仓库文件进 external 备份（快照只记 gitlink）"""
        _patch_snapshot_git_dispatch(monkeypatch)
        captured = _patch_ext_backup(monkeypatch)
        meta = ToolBackupManager.create_snapshot(
            task_id=779, tool_name="edit",
            tool_args={"filePath": repos["fc"]},
            workdir=str(repos["projA"]), nest_name=None,
        )
        assert meta is not None
        assert captured["files"] == [repos["fc"]]
        assert manifest_get(779) == [repos["fc"]]

    def test_remote_nested_from_dispatch_meta(self, repos, manifest_dir, monkeypatch):
        """远程路径：dispatch 返回的 nested_internal 直接采用（不再二次过滤）"""
        calls = _patch_snapshot_git_dispatch(monkeypatch)
        captured = _patch_ext_backup(monkeypatch)

        # 模拟远程 dispatch 已算好 nested_internal
        def fake(workdir, task_id, tool_name, tool_args, commit_msg, timestamp,
                 nest_name=None, internal_files=None):
            m = {
                "type": "git", "task_id": task_id, "tool_name": tool_name,
                "commit_hash": "cafe", "branch_name": "b",
                "workdir": str(workdir), "nest_name": nest_name or "local",
                "file_paths": [], "timestamp": timestamp, "rollback_cmd": "",
                "nested_internal": (
                    [internal_files[0]] if internal_files else []
                ),
            }
            return m

        monkeypatch.setattr(ToolBackupManager, "_snapshot_git_dispatch", fake)
        meta = ToolBackupManager.create_snapshot(
            task_id=780, tool_name="edit",
            tool_args={"filePath": repos["fc"]},
            workdir=str(repos["projA"]), nest_name="remote_nest",
        )
        assert meta is not None
        assert captured["files"] == [repos["fc"]]
        assert "nested_internal" not in meta  # 已被消费

    def test_non_git_workdir_nested_backed_up(self, repos, manifest_dir, monkeypatch):
        """非 git workdir：嵌套仓库文件也进备份（无仓库快照负责）"""
        # dispatch 返回 None（非 git workdir）
        _patch_snapshot_git_dispatch(monkeypatch, disabled=True)
        captured = _patch_ext_backup(monkeypatch)
        nested_dir = repos["plain"]
        sub = _git_init(nested_dir / "inner_repo")
        f_inner = sub / "i.py"
        f_inner.write_text("i", encoding="utf-8")
        meta = ToolBackupManager.create_snapshot(
            task_id=781, tool_name="edit",
            tool_args={"filePath": str(f_inner)},
            workdir=str(nested_dir), nest_name=None,
        )
        assert meta is not None
        assert meta["type"] == "external"
        assert str(f_inner) in captured["files"]
        assert str(f_inner) in meta["external_files"]
        assert manifest_get(781) == captured["files"]


# ==================== 6. 迭代快照 manifest 记录 ====================


class TestIterationSnapshotManifest:
    def test_meta_records_manifest(self, tmp_path, manifest_dir, monkeypatch):
        manifest_add(881, ["/ext/f.txt", "/ext2/g.txt"])
        git_meta = {
            "type": "git", "task_id": 881, "tool_name": "iteration_start",
            "commit_hash": "aaa", "branch_name": "b", "workdir": str(tmp_path),
            "nest_name": "local", "file_paths": [], "timestamp": "",
            "rollback_cmd": "",
        }
        monkeypatch.setattr(
            ToolBackupManager, "_snapshot_git_dispatch",
            lambda *a, **k: dict(git_meta),
        )
        monkeypatch.setattr(
            ExternalFileBackup, "get_all_heads",
            staticmethod(lambda task_id, nest_name=None: "head1234"),
        )
        monkeypatch.setattr(
            tb.HistoryBackupManager, "backup",
            staticmethod(lambda *a, **k: "histcommit"),
        )
        meta = ToolBackupManager.create_iteration_snapshot(
            task_id=881, history_context="{}", checklist="", iteration=3,
            workdir=str(tmp_path), nest_name=None,
        )
        assert meta is not None
        assert meta["external_head_commit"] == "head1234"
        assert meta["external_manifest"] == ["/ext/f.txt", "/ext2/g.txt"]
        assert meta["backup_policy"] == "workdir_scope"


# ==================== 7. 迭代回滚：manifest 精确恢复集 ====================


class TestIterationRollbackManifest:
    def _meta(self, manifest=None):
        m = {
            "type": "git", "task_id": 882, "tool_name": "iteration_start",
            "commit_hash": "bbb", "branch_name": "b", "workdir": "/wd",
            "nest_name": "local", "file_paths": [], "timestamp": "",
            "rollback_cmd": "", "iteration": 2,
            "external_head_commit": "head5678",
        }
        if manifest is not None:
            m["external_manifest"] = manifest
        return m

    def test_detect_uses_manifest_not_lstree(self, monkeypatch):
        """预检测优先 manifest；不再 ls-tree 全量（防跨事务混入）"""
        lstree = mock.Mock(return_value=["/other/task/file.txt"])
        monkeypatch.setattr(
            ExternalFileBackup, "list_baseline_files", staticmethod(lstree)
        )
        checks = {}

        def fake_check(task_id, files, commit, nest_name=None, strict_baseline=False):
            checks["files"] = list(files)
            return ["/my/ext/f.txt"]

        monkeypatch.setattr(
            ExternalFileBackup, "check_changes", staticmethod(fake_check)
        )
        meta = self._meta(manifest=["/my/ext/f.txt", "/skip/build/x.bin"])
        out = ToolBackupManager._detect_rollback_changes(meta, "/wd", None, True)
        assert out is True
        # 恢复集=manifest 轻过滤（build 剔除），未走 ls-tree
        assert checks["files"] == ["/my/ext/f.txt"]
        lstree.assert_not_called()

    def test_detect_legacy_meta_lstree_fallback(self, monkeypatch):
        """旧 meta（无 manifest）：保持 ls-tree 全量 + 全量 git 剔除兜底"""
        # 工作区检测 mock 为无变化（避免真实 git 命令）
        monkeypatch.setattr(
            ToolBackupManager, "check_post_tool_changes",
            staticmethod(lambda meta, wd, nest: None),
        )
        lstree = mock.Mock(return_value=[])
        monkeypatch.setattr(
            ExternalFileBackup, "list_baseline_files", staticmethod(lstree)
        )
        monkeypatch.setattr(
            ExternalFileBackup, "check_changes",
            staticmethod(lambda *a, **k: []),
        )
        meta = self._meta()  # 无 external_manifest
        out = ToolBackupManager._detect_rollback_changes(meta, "/wd", None, True)
        assert out is False
        lstree.assert_called_once()

    def test_restore_all_with_manifest(self, monkeypatch):
        """restore_all manifest 精确集：不 ls-tree、轻过滤恢复"""
        lstree = mock.Mock(return_value=["/other/task/file.txt"])
        monkeypatch.setattr(
            ExternalFileBackup, "list_baseline_files", staticmethod(lstree)
        )
        restores = {}

        def fake_restore(task_id, files, commit, nest_name=None):
            restores["files"] = list(files)
            return len(files)

        monkeypatch.setattr(ExternalFileBackup, "restore", staticmethod(fake_restore))
        n = ExternalFileBackup.restore_all(
            883, "head999", nest_name=None, workdir="/wd",
            manifest=["/my/a.txt", r"C:\Windows\System32\x.dll", "/my/b.txt"],
        )
        assert n == 2
        assert restores["files"] == ["/my/a.txt", "/my/b.txt"]
        lstree.assert_not_called()

    def test_restore_all_legacy_without_manifest(self, monkeypatch):
        """旧路径（无 manifest）：ls-tree + baseline 过滤，行为不变"""
        lstree = mock.Mock(return_value=[])
        monkeypatch.setattr(
            ExternalFileBackup, "list_baseline_files", staticmethod(lstree)
        )
        monkeypatch.setattr(
            ExternalFileBackup, "restore",
            staticmethod(lambda *a, **k: 0),
        )
        ExternalFileBackup.restore_all(884, "head888", nest_name=None, workdir="/wd")
        lstree.assert_called_once()

    def test_rollback_iter_passes_manifest(self, monkeypatch):
        """rollback() 迭代分支将 meta.external_manifest 传给 restore_all"""
        restore_all = mock.Mock(return_value=1)
        monkeypatch.setattr(
            ExternalFileBackup, "restore_all", staticmethod(restore_all)
        )
        # 预检测：工作区有变化（走快路径）
        monkeypatch.setattr(
            ToolBackupManager, "check_post_tool_changes",
            staticmethod(lambda meta, wd, nest: meta),
        )
        monkeypatch.setattr(
            ToolBackupManager, "_rollback_git",
            staticmethod(lambda meta, nest, **kw: (True, "ok")),
        )
        undo = mock.Mock(return_value=None)
        monkeypatch.setattr(ToolBackupManager, "_create_undo_snapshot", undo)
        meta = self._meta(manifest=["/my/ext/f.txt"])
        meta["changed_files"] = ["/wd/x.py"]
        ok, msg, _undo, changed = ToolBackupManager.rollback(meta)
        assert ok is True
        restore_all.assert_called_once()
        kwargs = restore_all.call_args.kwargs
        assert kwargs.get("manifest") == ["/my/ext/f.txt"]

    def test_preview_uses_manifest(self, monkeypatch):
        """预览优先 manifest（迭代 meta），不再 ls-tree 全量"""
        monkeypatch.setattr(
            ToolBackupManager, "preview_rollback_diff",
            staticmethod(lambda meta: {"success": True, "diff": "", "changed_files": [],
                                       "message": "ok"}),
        )
        lstree = mock.Mock(return_value=[])
        monkeypatch.setattr(
            ExternalFileBackup, "list_baseline_files", staticmethod(lstree)
        )
        monkeypatch.setattr(
            ExternalFileBackup, "check_changes",
            staticmethod(lambda task_id, files, commit, nest_name=None, strict_baseline=False: list(files)),
        )
        meta = self._meta(manifest=["/my/ext/f.txt"])
        result = ToolBackupManager.preview_rollback_diff_with_external(meta)
        assert "/my/ext/f.txt" in result["changed_files"]
        lstree.assert_not_called()


# ==================== 8. 旧 meta 兜底（scope=all，行为不变） ====================


class TestLegacyMetaFallback:
    def test_detect_legacy_external_filter_all(self, repos, monkeypatch):
        """旧工具快照 meta（无 policy）：external_files 全量 git 剔除保持"""
        checks = {}

        def fake_check(task_id, files, commit, nest_name=None, strict_baseline=False):
            checks["files"] = list(files)
            return []

        monkeypatch.setattr(
            ExternalFileBackup, "check_changes", staticmethod(fake_check)
        )
        # 模拟 #2088 误备份：meta 含外部 git 仓库文件
        meta = {
            "type": "git", "task_id": 1, "tool_name": "edit",
            "commit_hash": "h", "branch_name": "b", "workdir": str(repos["projA"]),
            "nest_name": "local", "file_paths": [], "timestamp": "",
            "rollback_cmd": "", "external_files": [repos["fb"], repos["fp"]],
            # 无 backup_policy → 旧语义
        }
        ToolBackupManager._detect_rollback_changes(meta, str(repos["projA"]), None, False)
        # fb 在 git 仓库内 → 旧语义剔除；仅保留非 git 文件
        assert checks["files"] == [repos["fp"]]
        assert meta["external_files"] == [repos["fp"]]  # 写回过滤结果

    def test_detect_new_policy_keeps_git_files(self, repos, monkeypatch):
        """新 meta（workdir_scope）：外部 git 仓库文件保留检测"""
        checks = {}

        def fake_check(task_id, files, commit, nest_name=None, strict_baseline=False):
            checks["files"] = list(files)
            return []

        monkeypatch.setattr(
            ExternalFileBackup, "check_changes", staticmethod(fake_check)
        )
        meta = {
            "type": "git", "task_id": 1, "tool_name": "edit",
            "commit_hash": "h", "branch_name": "b", "workdir": str(repos["projA"]),
            "nest_name": "local", "file_paths": [], "timestamp": "",
            "rollback_cmd": "", "external_files": [repos["fb"], repos["fp"]],
            "backup_policy": "workdir_scope",
        }
        ToolBackupManager._detect_rollback_changes(meta, str(repos["projA"]), None, False)
        assert sorted(checks["files"]) == sorted([repos["fb"], repos["fp"]])

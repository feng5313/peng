"""备份范围护栏回归测试（🔧 事务#2088：epyc 全盘复制事故）

事故根因：bash/cmd 命令 token 解析出盘根（D:\\）或浅目录（D:\\downloads）→
存在性过滤放行 → ExternalFileBackup copytree 无条件整树复制 →
D:\\.peng\\backups 镜像全盘（工作区 502GiB + git 对象库 65GiB）。

护栏分层（纵深防御）：
1. 纯形态层：is_root_volume_path（盘根/卷根）+ dir_backup_allowed_static（目录段数≥2）
2. 提取层：_extract_paths_from_command / _extract_paths_from_script 过滤盘根
3. 入口层：ExternalFileBackup.backup() 剔除盘根
4. 执行层：_backup_local / _remote_backup / _copy_external_file 对目录做
   深度+规模限额（_DIR_BACKUP_MAX_FILES/_DIR_BACKUP_MAX_BYTES）检查
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
    _DIR_BACKUP_MAX_BYTES,
    _DIR_BACKUP_MAX_FILES,
    _path_depth_segments,
    _skip_reason_for_path,
    dir_backup_allowed_static,
    is_root_volume_path,
)


# ==================== 1. 纯形态护栏 ====================


class TestStaticGuardrails:
    @pytest.mark.parametrize(
        "path,depth,is_root",
        [
            ("D:\\", 0, True),
            ("D:/", 0, True),
            ("D:", 0, True),
            ("C:\\", 0, True),
            ("/", 0, True),
            ("//server/share", 0, True),  # UNC share 根=卷根
            ("D:\\work", 1, False),
            ("C:/Users", 1, False),
            ("/home", 1, False),
            ("D:\\work\\x", 2, False),
            ("D:/work/fcc/a.txt", 3, False),
            ("//server/share/dir", 1, False),
        ],
    )
    def test_depth_and_root(self, path, depth, is_root):
        assert _path_depth_segments(path) == depth
        assert is_root_volume_path(path) is is_root

    def test_dir_backup_allowed_static(self):
        assert dir_backup_allowed_static("D:/work") is False  # 一级目录
        assert dir_backup_allowed_static("D:/") is False
        assert dir_backup_allowed_static("D:/work/fcc") is True

    def test_skip_reason(self):
        assert _skip_reason_for_path("D:/", True) is not None
        assert _skip_reason_for_path("D:\\work", True) is not None
        # 一级路径的文件、盘根直接文件：允许（单文件无放大效应）
        assert _skip_reason_for_path("D:\\work", False) is None
        assert _skip_reason_for_path("D:/a.txt", False) is None
        assert _skip_reason_for_path("D:/work/fcc", True) is None

    def test_limits_sane(self):
        assert _DIR_BACKUP_MAX_FILES == 20000
        assert _DIR_BACKUP_MAX_BYTES == 5 * 1024**3


# ==================== 2. 规模限额（本地遍历） ====================


class TestDirBudget:
    def _make_tree(self, root: Path, dirs: int, files_per_dir: int, size: int = 1):
        for d in range(dirs):
            sub = root / f"d{d}"
            sub.mkdir(parents=True, exist_ok=True)
            for f in range(files_per_dir):
                (sub / f"f{f}").write_bytes(b"x" * size)

    def test_small_dir_within_budget(self, tmp_path):
        self._make_tree(tmp_path, dirs=3, files_per_dir=10, size=100)
        assert tb._dir_within_budget_local(str(tmp_path)) is True

    def test_oversized_dir_rejected(self, tmp_path, monkeypatch):
        # 缩小限额避免真实构造 2 万文件（护栏逻辑等价）
        monkeypatch.setattr(tb, "_DIR_BACKUP_MAX_FILES", 5)
        monkeypatch.setattr(tb, "_DIR_BACKUP_MAX_BYTES", 1024)
        self._make_tree(tmp_path, dirs=2, files_per_dir=6, size=10)
        assert tb._dir_within_budget_local(str(tmp_path)) is False

    def test_nonexistent_dir_rejected(self):
        # 遍历失败（不存在/权限）按超限处理（保守跳过）
        assert tb._dir_within_budget_local("/nonexistent/xyz") is False


# ==================== 3. 命令/脚本提取层 ====================


class TestExtractionFilters:
    def test_command_root_paths_filtered(self, monkeypatch):
        monkeypatch.setattr(
            tb, "_paths_exist_batch",
            lambda paths, nest_name=None: set(paths),
        )
        # dir/del 等命令带盘根 token → 不进结果
        out = ToolBackupManager._extract_paths_from_command(
            "del D:\\ /q /s", workdir="D:\\work\\fcc", nest_name=None
        )
        assert out == []
        # 正常文件路径保留
        out = ToolBackupManager._extract_paths_from_command(
            "del D:\\work\\fcc\\a.txt", workdir="D:\\work\\fcc", nest_name=None
        )
        assert out == ["D:\\work\\fcc\\a.txt"]

    def test_script_root_token_filtered(self, tmp_path, monkeypatch):
        # 脚本内含盘根样式 token：存在性通过也不得成为备份对象
        monkeypatch.setattr(
            tb, "_paths_exist_batch",
            lambda paths, nest_name=None: set(paths),
        )
        monkeypatch.setattr(ToolBackupManager, "_read_text_file", staticmethod(
            lambda path, nest_name: "open('D:/work/fcc/x.glsl')\nos.remove('D:\\\\')"
        ))
        out = ToolBackupManager._extract_paths_from_script(
            str(tmp_path / "s.py"), "D:\\work\\fcc", None
        )
        assert all(not is_root_volume_path(p) for p in out)
        assert "D:/work/fcc/x.glsl" in out or "D:\\work\\fcc\\x.glsl" in out


# ==================== 4. 入口层 ====================


class TestBackupEntry:
    def test_backup_drops_root_paths(self, monkeypatch):
        called = {}

        def fake_backup_local(task_id, fps, label=""):
            called["local"] = fps
            return "abc123"

        monkeypatch.setattr(ExternalFileBackup, "_backup_local", staticmethod(fake_backup_local))
        monkeypatch.setattr(ExternalFileBackup, "_remote_backup", staticmethod(
            lambda *a, **k: None
        ))
        res = ExternalFileBackup.backup(
            5, ["D:\\", "D:\\work\\fcc\\a.txt", "C:\\"], "test"
        )
        assert res == "abc123"
        assert called["local"] == ["D:\\work\\fcc\\a.txt"]

    def test_backup_all_root_returns_none(self, monkeypatch):
        monkeypatch.setattr(ExternalFileBackup, "_backup_local", staticmethod(
            lambda *a, **k: pytest.fail("不应到达")
        ))
        assert ExternalFileBackup.backup(5, ["D:\\", "/"], "test") is None


# ==================== 5. 执行层（_backup_local / 远程脚本） ====================


class TestExecutionGuards:
    def test_backup_local_skips_shallow_dir(self, tmp_path, monkeypatch):
        # 一级目录（D:\work 形态）：目录判定 True → 跳过，不建组不复制
        monkeypatch.setattr(os.path, "exists", lambda p: True)
        monkeypatch.setattr(os.path, "isdir", lambda p: str(p).rstrip("\\/") in ("D:\\work",))
        monkeypatch.setattr(os.path, "isfile", lambda p: False)
        copied = []
        monkeypatch.setattr(tb.shutil, "copy2", lambda s, d: copied.append((s, d)))
        monkeypatch.setattr(tb.shutil, "copytree", lambda s, d, **k: copied.append((s, d)))
        monkeypatch.setattr(ExternalFileBackup, "_get_backup_repo_for", staticmethod(
            lambda tid, fp: str(tmp_path / "repo")
        ))
        monkeypatch.setattr(tb, "_is_abs_path_excluded", lambda p: False)
        res = ExternalFileBackup._backup_local(5, ["D:\\work"], "t")
        assert res is None  # 无可备份文件
        assert copied == []

    def test_backup_local_skips_oversized_deep_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(os.path, "exists", lambda p: True)
        monkeypatch.setattr(os.path, "isdir", lambda p: str(p).rstrip("\\/") == "D:\\work\\huge")
        monkeypatch.setattr(os.path, "isfile", lambda p: False)
        monkeypatch.setattr(tb, "_dir_within_budget_local", lambda p: False)
        copied = []
        monkeypatch.setattr(tb.shutil, "copytree", lambda s, d, **k: copied.append((s, d)))
        monkeypatch.setattr(ExternalFileBackup, "_get_backup_repo_for", staticmethod(
            lambda tid, fp: str(tmp_path / "repo")
        ))
        monkeypatch.setattr(tb, "_is_abs_path_excluded", lambda p: False)
        res = ExternalFileBackup._backup_local(5, ["D:\\work\\huge"], "t")
        assert res is None
        assert copied == []

    def test_remote_backup_script_contains_guards(self):
        # 远程脚本自包含护栏：盘根/浅目录/限额逻辑必须内嵌（epyc 真实事故路径）
        import inspect
        src = inspect.getsource(ExternalFileBackup._remote_backup)
        for needle in (
            "seg_count(fp) == 0",
            "seg_count(fp) < 2",
            "dir_within_budget(fp)",
            "skipped.append",
        ):
            assert needle in src, needle
        head = inspect.getsource(ExternalFileBackup._remote_py_head)
        assert "def seg_count" in head
        assert "def dir_within_budget" in head

    def test_remote_py_head_script_compiles(self):
        # 公共头脚本语法必须可编译（防字符串拼接笔误）
        code = ExternalFileBackup._remote_py_head(1)
        compile(code, "<remote_head>", "exec")
        head2 = ExternalFileBackup._remote_py_head(1)
        # 头部独立可执行到函数定义结束（out = {} 之后无顶层副作用）
        exec(head2, {})


# ==================== 7. workdir 内文件误判外部（🔧 斜杠归一化） ====================


class TestWorkdirClassification:
    """Linux 服务端处理 Windows 反斜杠路径的归一化分类（task#2077 事故）"""

    def test_backslash_path_classified_internal(self):
        # workdir=D:/work/fcc（git仓库）+ edit 反斜杠路径 → internal，绝不进 external
        from unittest import mock as _mock

        with _mock.patch.object(ToolBackupManager, "_extract_file_paths") as m1, \
             _mock.patch.object(ToolBackupManager, "_snapshot_git_dispatch") as m2, \
             _mock.patch("peng.service.tool_backup.ExternalFileBackup.backup") as mb:
            m1.return_value = [
                "D:\\work\\fcc\\res\\x.glsl",      # workdir 内（反斜杠）
                "D:/work/fcc/res/y.glsl",           # workdir 内（正斜杠）
                "D:\\downloads\\ai\\s1.html",       # workdir 外
            ]
            m2.return_value = {"type": "git", "commit_hash": "a" * 40}
            mb.return_value = "c1"
            meta = ToolBackupManager.create_snapshot(
                2077, "edit", {"filePath": "D:\\work\\fcc\\res\\x.glsl"},
                "D:/work/fcc", "epyc",
            )
            ext = meta.get("external_files") or []
            assert ext == ["D:/downloads/ai/s1.html"], ext

    def test_boundary_no_false_internal(self):
        # fcc2 不因前缀 fcc 被误判 internal
        from unittest import mock as _mock

        with _mock.patch.object(ToolBackupManager, "_extract_file_paths") as m1, \
             _mock.patch.object(ToolBackupManager, "_snapshot_git_dispatch") as m2, \
             _mock.patch.object(ToolBackupManager, "_filter_unbackupable",
                                side_effect=lambda fps, wd, n, scope="all": fps), \
             _mock.patch("peng.service.tool_backup.ExternalFileBackup.backup") as mb:
            m1.return_value = ["D:\\work\\fcc2\\a.txt"]
            m2.return_value = {"type": "git", "commit_hash": "a" * 40}
            mb.return_value = "c1"
            meta = ToolBackupManager.create_snapshot(
                2077, "edit", {}, "D:/work/fcc", "epyc",
            )
            assert "D:/work/fcc2/a.txt" in (meta.get("external_files") or [])



def test_remote_backup_full_script_compiles(monkeypatch):
    # 构造最小调用路径，捕获生成的完整脚本并编译校验
    captured = {}

    def fake_run(code, timeout=30, nest_name="local"):
        captured["code"] = code
        class R:
            returncode = 0
            stdout = '{"commits": {}}'
            stderr = ""
        return R()

    monkeypatch.setattr(tb, "_run_python_remote", fake_run)
    ExternalFileBackup._remote_backup("epyc", 5, ["D:/work/fcc/a.txt"], "t")
    assert "code" in captured
    compile(captured["code"], "<remote_backup>", "exec")
    # 护栏关键逻辑必须在完整脚本中
    for needle in ("seg_count(fp) == 0", "dir_within_budget(fp)", "'skipped'"):
        assert needle in captured["code"], needle

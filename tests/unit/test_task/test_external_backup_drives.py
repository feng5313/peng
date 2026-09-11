"""
测试 Windows 平台外部文件备份到同盘符根目录 /.peng/backups（事务#1847）

验证 ExternalFileBackup 的多仓库机制：
1. 叶子函数：_split_drive_key（盘符提取）、_repo_key、_repo_path_for_key
2. backup 按文件仓库分组：盘符文件入同盘符仓库，返回 dict {repo_key: commit}；
   纯默认仓库返回 str（100%兼容旧 meta 格式）
3. check_changes / restore 支持 str（旧格式，查默认仓库）/ dict（多仓库基线）/ None
4. get_all_heads / list_baseline_files / restore_all 多仓库全量恢复
5. 旧格式 str 基线数据（全部位于默认 data 仓库）向后兼容
6. dict 基线缺仓库条目 → check_changes 判定新增、restore 跳过

Linux 测试环境通过 mock _split_drive_key（识别路径中的 DRIVE_D/DRIVE_E 标记）
与 _repo_path_for_key（盘符 key 映射到 tmp 目录）端到端模拟 Windows 盘符行为。
"""
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from peng.service.tool_backup import ExternalFileBackup, _strip_drive_rel


def _git(repo: Path, *args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, timeout=10, **kw
    )


def _git_init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@peng")
    _git(repo, "config", "user.name", "T")
    _git(repo, "commit", "-m", "init", "--allow-empty")


# ==================== Windows rel 语义纯字符串单测 ====================
# 🔧 真实测试发现的 Windows 路径语义陷阱：os.path.join(仓库, "D:/x") == "D:/x"
# （含盘符路径被视为绝对路径，dst 恒等于源文件 → copy2(源,源) WinError 32），
# 且 Windows 目录名不能含 ':'（mkdir WinError 123）。盘符仓库 rel 必须去盘符。


class TestStripDriveRel:
    def test_windows_drive_stripped(self):
        assert _strip_drive_rel("D:\\peng\\x.txt") == "peng/x.txt"
        assert _strip_drive_rel("d:/peng/x") == "peng/x"

    def test_unix_path_kept(self):
        assert _strip_drive_rel("/home/user/x") == "home/user/x"

    def test_repo_rel_by_repo_key(self):
        # 盘符仓库：去盘符（Windows 真实场景）
        assert ExternalFileBackup._repo_rel("D:\\peng\\x", "D") == "peng/x"
        # 默认仓库：保留盘符段（Linux peng 的 data 仓库历史数据结构）
        assert ExternalFileBackup._repo_rel("D:\\peng\\x", "") == "D:/peng/x"
        assert ExternalFileBackup._repo_rel("/home/x", "") == "home/x"

    def test_drive_rel_roundtrip(self):
        """盘符仓库 rel 与 list_baseline_files 反推互逆（Windows 真实语义）"""
        rel = ExternalFileBackup._repo_rel("E:\\work\\f.txt", "E")
        assert f"E:/{rel}" == "E:/work/f.txt"


# ==================== 叶子函数单元测试（mock os.name） ====================


class TestSplitDriveKey:
    def test_windows_drive_uppercase(self, monkeypatch):
        monkeypatch.setattr(__import__("os"), "name", "nt")
        assert ExternalFileBackup._split_drive_key("D:\\work\\f.txt") == "D"

    def test_windows_drive_lowercase_normalized(self, monkeypatch):
        monkeypatch.setattr(__import__("os"), "name", "nt")
        assert ExternalFileBackup._split_drive_key("d:/work/f.txt") == "D"

    def test_unix_path_no_drive(self, monkeypatch):
        monkeypatch.setattr(__import__("os"), "name", "nt")
        assert ExternalFileBackup._split_drive_key("/home/user/f.txt") is None

    def test_unc_path_no_drive(self, monkeypatch):
        monkeypatch.setattr(__import__("os"), "name", "nt")
        assert ExternalFileBackup._split_drive_key("\\\\server\\share\\f.txt") is None

    def test_relative_path_no_drive(self, monkeypatch):
        monkeypatch.setattr(__import__("os"), "name", "nt")
        assert ExternalFileBackup._split_drive_key("work/f.txt") is None

    def test_non_windows_platform_none(self, monkeypatch):
        """非 Windows 平台（Linux服务端）一律走默认仓库（远程Windows nest路径不启用盘符仓库）"""
        monkeypatch.setattr(__import__("os"), "name", "posix")
        assert ExternalFileBackup._split_drive_key("D:\\work\\f.txt") is None

    def test_repo_key(self, monkeypatch):
        monkeypatch.setattr(__import__("os"), "name", "nt")
        assert ExternalFileBackup._repo_key("E:\\x") == "E"
        assert ExternalFileBackup._repo_key("/tmp/x") == ""


class TestRepoPathForKey:
    def test_drive_key_path(self, monkeypatch):
        monkeypatch.setattr(__import__("os"), "name", "nt")
        p = ExternalFileBackup._repo_path_for_key(5, "D")
        assert p == "D:\\.peng\\backups\\task#5"

    def test_default_key_uses_backup_repo(self, tmp_path, monkeypatch):
        monkeypatch.setattr(__import__("os"), "name", "nt")
        monkeypatch.setattr(
            ExternalFileBackup, "_get_backup_repo", staticmethod(lambda tid: str(tmp_path / "fb" / f"task#{tid}"))
        )
        assert ExternalFileBackup._repo_path_for_key(7, "") == str(tmp_path / "fb" / "task#7")


# ==================== 端到端多仓库行为（mock 盘符识别） ====================

TASK_ID = 998002


@pytest.fixture
def ext_repo(tmp_path, monkeypatch) -> Path:
    """默认仓库 mock（与 test_backup_rollback_dedup.py 的 ext_repo 等效）"""
    repo = tmp_path / "file_backups" / "task#998001"
    _git_init(repo)
    monkeypatch.setattr(
        ExternalFileBackup, "_get_backup_repo", staticmethod(lambda task_id: str(repo))
    )
    return repo


@pytest.fixture
def drive_env(tmp_path, monkeypatch):
    """模拟 Windows 多盘符环境

    - 路径含 DRIVE_D / DRIVE_E 标记 → 盘符仓库 D / E（映射 tmp/drive_X/task#id）
    - 其他路径 → 默认仓库（tmp/file_backups/task#id，mock _get_backup_repo）
    - _existing_drive_keys 扫描 tmp 下已初始化的盘符仓库
    """
    default_repo = tmp_path / "file_backups" / f"task#{TASK_ID}"
    _git_init(default_repo)

    def fake_split(fp):
        s = str(fp)
        if "DRIVE_D" in s:
            return "D"
        if "DRIVE_E" in s:
            return "E"
        return None

    def fake_path_for_key(tid, key):
        if key:
            return str(tmp_path / f"drive_{key}" / f"task#{tid}")
        return str(default_repo)

    def fake_existing_keys(tid):
        keys = []
        for c in ("D", "E"):
            repo = tmp_path / f"drive_{c}" / f"task#{tid}"
            if (repo / ".git").is_dir():
                keys.append(c)
        return keys

    monkeypatch.setattr(ExternalFileBackup, "_split_drive_key", staticmethod(fake_split))
    monkeypatch.setattr(ExternalFileBackup, "_repo_path_for_key", staticmethod(fake_path_for_key))
    monkeypatch.setattr(
        ExternalFileBackup, "_get_backup_repo", staticmethod(lambda tid: str(default_repo))
    )
    monkeypatch.setattr(
        ExternalFileBackup, "_existing_drive_keys", staticmethod(fake_existing_keys)
    )
    return SimpleNamespace(default_repo=default_repo, tmp_path=tmp_path)


class TestDriveBackup:
    def test_drive_file_backup_returns_dict(self, drive_env, tmp_path):
        """盘符文件 → 备份到同盘符仓库，返回 dict {盘符: commit}"""
        f = tmp_path / "DRIVE_D_proj" / "f.txt"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("v1", encoding="utf-8")

        result = ExternalFileBackup.backup(TASK_ID, [str(f)], "l1")
        assert isinstance(result, dict)
        assert set(result.keys()) == {"D"}
        # 文件镜像位于盘符仓库（不在默认仓库）
        drive_repo = tmp_path / "drive_D" / f"task#{TASK_ID}"
        rel = ExternalFileBackup._to_repo_path(str(f))
        assert (drive_repo / rel).read_text(encoding="utf-8") == "v1"
        assert not (drive_env.default_repo / rel).exists()

    def test_mixed_files_group_to_repos(self, drive_env, tmp_path):
        """盘符D + 盘符E + 默认文件 → 三个仓库各得其所，dict 含三个 key"""
        fd = tmp_path / "DRIVE_D_proj" / "fd.txt"
        fe = tmp_path / "DRIVE_E_proj" / "fe.txt"
        fn = tmp_path / "normal_proj" / "fn.txt"
        for f in (fd, fe, fn):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x", encoding="utf-8")

        result = ExternalFileBackup.backup(TASK_ID, [str(fd), str(fe), str(fn)], "l1")
        assert isinstance(result, dict)
        assert set(result.keys()) == {"", "D", "E"}
        rel_d = ExternalFileBackup._to_repo_path(str(fd))
        rel_n = ExternalFileBackup._to_repo_path(str(fn))
        assert (tmp_path / "drive_D" / f"task#{TASK_ID}" / rel_d).is_file()
        assert (tmp_path / "drive_E" / f"task#{TASK_ID}" / ExternalFileBackup._to_repo_path(str(fe))).is_file()
        assert (drive_env.default_repo / rel_n).is_file()

    def test_default_only_returns_str_compat(self, drive_env, tmp_path):
        """纯默认仓库文件 → 返回 str（100%兼容旧 meta 格式）"""
        fn = tmp_path / "normal_proj" / "fn.txt"
        fn.parent.mkdir(parents=True, exist_ok=True)
        fn.write_text("x", encoding="utf-8")

        result = ExternalFileBackup.backup(TASK_ID, [str(fn)], "l1")
        assert isinstance(result, str) and len(result) == 40  # git commit hash

    def test_drive_dedup_reuse_head(self, drive_env, tmp_path):
        """同盘符文件无变化时复用 HEAD（去重）"""
        f = tmp_path / "DRIVE_D_proj" / "f.txt"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("same", encoding="utf-8")
        c1 = ExternalFileBackup.backup(TASK_ID, [str(f)], "l1")
        c2 = ExternalFileBackup.backup(TASK_ID, [str(f)], "l2")
        assert isinstance(c1, dict) and c1 == c2


class TestDriveCheckRestore:
    def test_check_changes_and_restore_with_dict(self, drive_env, tmp_path):
        """dict 基线：修改后 check_changes 报告、restore 从各自盘符仓库恢复"""
        fd = tmp_path / "DRIVE_D_proj" / "fd.txt"
        fn = tmp_path / "normal_proj" / "fn.txt"
        for f in (fd, fn):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("v1", encoding="utf-8")

        base = ExternalFileBackup.backup(TASK_ID, [str(fd), str(fn)], "l1")
        assert isinstance(base, dict)
        # 无变化
        assert ExternalFileBackup.check_changes(TASK_ID, [str(fd), str(fn)], base) == []
        # 修改两个文件
        fd.write_text("v2-d", encoding="utf-8")
        fn.write_text("v2-n", encoding="utf-8")
        assert set(ExternalFileBackup.check_changes(TASK_ID, [str(fd), str(fn)], base)) == {str(fd), str(fn)}
        # 恢复（各文件走各自仓库）
        assert ExternalFileBackup.restore(TASK_ID, [str(fd), str(fn)], base) == 2
        assert fd.read_text(encoding="utf-8") == "v1"
        assert fn.read_text(encoding="utf-8") == "v1"

    def test_legacy_str_base_reads_default_repo(self, drive_env, tmp_path):
        """旧格式 str 基线：数据全在默认仓库，盘符文件也从默认仓库检测/恢复"""
        fd = tmp_path / "DRIVE_D_proj" / "fd.txt"
        fd.parent.mkdir(parents=True, exist_ok=True)
        fd.write_text("old-v1", encoding="utf-8")
        # 手工将旧数据放入默认仓库（模拟旧版本备份：不分组，全进 data 仓库）
        rel = ExternalFileBackup._to_repo_path(str(fd))
        dst = drive_env.default_repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("old-v1", encoding="utf-8")
        _git(drive_env.default_repo, "add", "-A")
        _git(drive_env.default_repo, "commit", "-m", "legacy")
        legacy_commit = (
            _git(drive_env.default_repo, "rev-parse", "HEAD").stdout.decode().strip()
        )

        # 无变化（相对旧 str 基线，查默认仓库）
        assert ExternalFileBackup.check_changes(TASK_ID, [str(fd)], legacy_commit) == []
        # 修改后：旧基线检测到变化并从默认仓库恢复
        fd.write_text("modified", encoding="utf-8")
        assert ExternalFileBackup.check_changes(TASK_ID, [str(fd)], legacy_commit) == [str(fd)]
        assert ExternalFileBackup.restore(TASK_ID, [str(fd)], legacy_commit) == 1
        assert fd.read_text(encoding="utf-8") == "old-v1"

    def test_dict_missing_key_treated_as_new(self, drive_env, tmp_path):
        """dict 基线缺该盘符仓库条目 → check_changes 判定新增、restore 跳过"""
        fd = tmp_path / "DRIVE_D_proj" / "fd.txt"
        fd.parent.mkdir(parents=True, exist_ok=True)
        fd.write_text("v1", encoding="utf-8")
        # 仅有默认仓库的基线（dict 缺 "D" 条目）
        base = {"" : "0" * 40}
        assert ExternalFileBackup.check_changes(TASK_ID, [str(fd)], base) == [str(fd)]
        assert ExternalFileBackup.restore(TASK_ID, [str(fd)], base) == 0
        assert fd.read_text(encoding="utf-8") == "v1"  # 未被破坏


class TestEnsureRepoRepair:
    """🔧 残缺 .git 自动重建（Windows rmtree 文件锁留下的半初始化状态）"""

    def test_broken_git_dir_rebuilt(self, tmp_path, monkeypatch):
        """仅含 objects/ 的残缺 .git → _ensure_repo 重建为有效仓库"""
        repo = tmp_path / "broken_repo"
        (repo / ".git" / "objects").mkdir(parents=True)  # 模拟 rmtree 部分失败残留
        # 旧逻辑：os.path.exists(.git)=True → 跳过 init → 无效仓库
        result = ExternalFileBackup._ensure_repo(str(repo))
        assert result == str(repo)
        r = subprocess.run(
            ["git", "rev-parse", "--git-dir"], cwd=str(repo),
            capture_output=True, timeout=10,
        )
        assert r.returncode == 0  # 重建后为有效仓库
        r = subprocess.run(
            ["git", "log", "--oneline"], cwd=str(repo),
            capture_output=True, timeout=10,
        )
        assert r.returncode == 0  # init commit 存在

    def test_valid_repo_not_reinit(self, tmp_path):
        """有效仓库 → 不重复 init（HEAD 不变）"""
        repo = tmp_path / "good_repo"
        _git_init(repo)
        head1 = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()
        ExternalFileBackup._ensure_repo(str(repo))
        head2 = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()
        assert head1 == head2

    def test_backup_recovers_from_broken_repo(self, ext_repo, tmp_path):
        """残缺仓库场景下 backup 自动修复并成功（回归 07:42 epyc 真实测试失败）"""
        f1 = tmp_path / "ext1.txt"
        f1.write_text("v1", encoding="utf-8")
        # 先正常备份一次获得有效仓库
        ExternalFileBackup.backup(998001, [str(f1)], "l1")
        # 破坏仓库：清空 .git 只留空 objects（模拟 rmtree 文件锁部分失败）
        import shutil as _sh
        _sh.rmtree(ext_repo / ".git", ignore_errors=True)
        (ext_repo / ".git" / "objects").mkdir(parents=True)
        f1.write_text("v2", encoding="utf-8")
        # backup 应自动重建仓库并成功返回 commit
        commit = ExternalFileBackup.backup(998001, [str(f1)], "l2")
        assert commit  # 非空（str 或 dict）
        # 恢复验证
        f1.write_text("v3", encoding="utf-8")
        assert ExternalFileBackup.check_changes(998001, [str(f1)], commit) == [str(f1)]
        assert ExternalFileBackup.restore(998001, [str(f1)], commit) == 1
        assert f1.read_text(encoding="utf-8") == "v2"


class TestGetAllHeadsAndRestoreAll:
    def test_get_all_heads_multi_repo(self, drive_env, tmp_path):
        fd = tmp_path / "DRIVE_D_proj" / "fd.txt"
        fn = tmp_path / "normal_proj" / "fn.txt"
        for f in (fd, fn):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("v1", encoding="utf-8")
        ExternalFileBackup.backup(TASK_ID, [str(fd), str(fn)], "l1")

        heads = ExternalFileBackup.get_all_heads(TASK_ID)
        assert isinstance(heads, dict)
        assert set(heads.keys()) == {"", "D"}

    def test_get_all_heads_default_only_str(self, drive_env, tmp_path):
        fn = tmp_path / "normal_proj" / "fn.txt"
        fn.parent.mkdir(parents=True, exist_ok=True)
        fn.write_text("v1", encoding="utf-8")
        ExternalFileBackup.backup(TASK_ID, [str(fn)], "l1")

        heads = ExternalFileBackup.get_all_heads(TASK_ID)
        assert isinstance(heads, str) and len(heads) == 40  # 仅默认仓库 → 兼容 str

    def test_list_baseline_and_restore_all(self, drive_env, tmp_path):
        """list_baseline_files 跨仓库列文件；restore_all 全量恢复（含删除场景）

        mock 世界语义：盘符文件（DRIVE_D 标记的 Unix 路径）备份到盘符仓库时
        _strip_drive_rel 无盘符可去（保留完整路径），list_baseline 反推时按
        真实 Windows 语义加回盘符前缀 "D:"。因此 fd 的反推/恢复路径 = "D:"+原路径。
        """
        fd = tmp_path / "DRIVE_D_proj" / "fd.txt"
        fn = tmp_path / "normal_proj" / "fn.txt"
        for f in (fd, fn):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("v1", encoding="utf-8")
        ExternalFileBackup.backup(TASK_ID, [str(fd), str(fn)], "l1")
        heads = ExternalFileBackup.get_all_heads(TASK_ID)

        # 模拟后续破坏：修改 + 删除
        fd.write_text("v2", encoding="utf-8")
        fn.unlink()

        fd_mapped = "D:" + str(fd)  # 盘符仓库反推路径（mock 语义）
        baseline_files = ExternalFileBackup.list_baseline_files(TASK_ID, heads)
        assert set(baseline_files) == {fd_mapped, str(fn)}

        # 回滚预检测：盘符文件在反推路径不存在（判删除）、默认文件被删（判删除）
        assert ExternalFileBackup.check_changes(TASK_ID, baseline_files, heads) == baseline_files
        # 全量恢复：盘符文件恢复到反推路径，默认文件恢复到原路径
        assert ExternalFileBackup.restore_all(TASK_ID, heads) == 2
        assert Path(fd_mapped).read_text(encoding="utf-8") == "v1"
        assert fn.read_text(encoding="utf-8") == "v1"

    def test_restore_all_legacy_str(self, drive_env, tmp_path):
        """旧格式 str 基线 → restore_all 从默认仓库全量恢复"""
        fn = tmp_path / "normal_proj" / "fn.txt"
        fn.parent.mkdir(parents=True, exist_ok=True)
        fn.write_text("v1", encoding="utf-8")
        ExternalFileBackup.backup(TASK_ID, [str(fn)], "l1")
        legacy_commit = (
            _git(drive_env.default_repo, "rev-parse", "HEAD").stdout.decode().strip()
        )

        fn.write_text("v2", encoding="utf-8")
        assert ExternalFileBackup.restore_all(TASK_ID, legacy_commit) == 1
        assert fn.read_text(encoding="utf-8") == "v1"

"""事务#2251 平台交叉路径护栏回归测试

事故根因：task#2247 编排 xin→epyc 传输时把 Windows 盘符路径传给 nest 工具
pull 的 local_path；local_path 在编排器主机（Linux, cwd=项目根）解析，盘符路径
在 POSIX 上是相对路径 → 被静默 mkdir/open 实体化到代码库根目录（垃圾目录 "D:"），
随后被 git add -A 卷入提交。同类：xfer/、exe_parts/ 等运行时中转目录。

护栏（tools/nest.py）：pull/push/sync 两端路径平台交叉 → 明确 ToolError 拒绝；
只拒"盘符路径落在 POSIX 端"这一事故形态（Windows 端 "/x" 为合法路径不误伤）。
配套：.gitignore 追加 xfer/ exe_parts/ zip_parts/ parts2/ [A-Za-z]:/。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from peng.tools.nest import (  # noqa: E402
    ToolError,
    _check_transfer_paths,
    _local_side_platform,
    _path_platform_mismatch,
)


class TestPathPlatformMismatch:
    def test_posix_side_rejects_windows_drive_path(self):
        """事故形态：盘符路径（斜杠/反斜杠两形态）落在 POSIX 端 → 拒绝"""
        assert _path_platform_mismatch("D:/work/fcc/temp/shot.jpg", "Linux") is not None
        assert _path_platform_mismatch("D:\\work\\fcc\\temp\\shot.jpg", "Linux") is not None
        assert _path_platform_mismatch("c:/Tools/x", "Darwin") is not None

    def test_posix_side_accepts_valid_paths(self):
        """POSIX 端合法路径：posix 绝对路径、相对路径（默认 cwd 语义）"""
        assert _path_platform_mismatch("/mnt/p/feng/data/x.dat", "Linux") is None
        assert _path_platform_mismatch("xfer/shot.jpg", "Linux") is None
        assert _path_platform_mismatch("", "Linux") is None

    def test_windows_side_accepts_both_forms(self):
        """Windows 端：盘符路径与 "/x"（nt 视为当前驱动器根）均合法，不误伤"""
        assert _path_platform_mismatch("D:\\work\\temp\\a.exe", "Windows") is None
        assert _path_platform_mismatch("/tmp/x", "Windows") is None

    def test_unknown_platform_no_reject(self):
        """平台未知（空串）不拒绝（避免查询失败误伤既有行为）"""
        assert _path_platform_mismatch("D:/work/x", "") is None


class TestCheckTransferPaths:
    def test_pull_local_path_cross_platform_raises(self):
        """本次事故调用形态：local_path 传了 Windows 盘符路径（本机 Linux）"""
        if _local_side_platform() != "Linux":
            pytest.skip("本机非 Linux，事故场景不匹配")
        with pytest.raises(ToolError, match="平台交叉"):
            _check_transfer_paths("xin", "D:/work/fcc/temp/fcc_hang.dmp", "D:/Temp/fcc_hang.dmp")

    def test_normal_usage_passes(self):
        """task#2247 的正确用法不受影响：本机绝对路径 + 远端盘符路径"""
        if _local_side_platform() != "Linux":
            pytest.skip("本机非 Linux，事故场景不匹配")
        # remote 平台未知（测试无 nest）→ 不拒绝；local 合法 → 通过
        _check_transfer_paths("xin", "/mnt/p/feng/work/peng/exe_parts/paa", "D:\\Temp\\exe_parts\\paa")

    def test_empty_local_path_default_passes(self):
        """local_path 空（默认 cwd 行为）不校验"""
        _check_transfer_paths("xin", "", "D:/Temp/whatever")


class TestGitignoreCoverage:
    def test_runtime_dirs_ignored(self):
        """运行时中转目录必须被 git 忽略（防再被 git add 卷入提交）"""
        import subprocess

        gi = Path(__file__).resolve().parents[2] / ".gitignore"
        text = gi.read_text(encoding="utf-8")
        for token in ("xfer/", "exe_parts/", "zip_parts/", "parts2/", "[A-Za-z]:/"):
            assert token in text, f".gitignore 缺少 {token}"
        # 实际验证 git check-ignore（在仓库内）
        repo = Path(__file__).resolve().parents[2]
        for probe in ("xfer/a.bin", "exe_parts/paa", "zip_parts/zaa", "parts2/p000", "D:/work/fcc/x"):
            r = subprocess.run(
                ["git", "-C", str(repo), "check-ignore", "-q", probe],
                capture_output=True, timeout=10,
            )
            assert r.returncode == 0, f"git 未忽略 {probe}"

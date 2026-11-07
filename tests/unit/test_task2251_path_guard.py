"""事务#2251 平台交叉路径护栏回归测试

事故根因：task#2247 编排 xin→epyc 传输时把 Windows 盘符路径传给 nest 工具
pull 的 local_path；local_path 在编排器主机（Linux, cwd=项目根）解析，盘符路径
在 POSIX 上是相对路径 → 被静默 mkdir/open 实体化到代码库根目录（垃圾目录 "D:"），
随后被 git add -A 卷入提交。同类：xfer/、exe_parts/ 等运行时中转目录。

护栏（tools/nest.py）：pull/push/sync 两端路径平台交叉 → 明确 ToolError 拒绝；
只拒"盘符路径落在 POSIX 端"这一事故形态（Windows 端 "/x" 为合法路径不误伤）。
配套：.gitignore 追加 xfer/ exe_parts/ zip_parts/ parts2/ [A-Za-z]:/。
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from peng.tools.nest import (  # noqa: E402
    NestTool,
    ToolError,
    _check_transfer_paths,
    _final_local_path_check,
    _local_side_platform,
    _path_platform_mismatch,
    _safe_remote_basename,
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
        # 🔧 事务#2609：[A-Za-z]:* 兜住反斜杠整串文件名形态（'C:\Users\...' 无 /）
        for token in ("xfer/", "exe_parts/", "zip_parts/", "parts2/", "[A-Za-z]:/", "[A-Za-z]:*"):
            assert token in text, f".gitignore 缺少 {token}"
        # 实际验证 git check-ignore（在仓库内）
        repo = Path(__file__).resolve().parents[2]
        for probe in (
            "xfer/a.bin", "exe_parts/paa", "zip_parts/zaa", "parts2/p000", "D:/work/fcc/x",
            "C:\\Users\\Xin\\AppData\\Local\\fcc\\fcc.json",
            "C:\\Users\\Xin\\AppData\\Local\\Temp\\fcc_log.txt",
        ):
            r = subprocess.run(
                ["git", "-C", str(repo), "check-ignore", "-q", probe],
                capture_output=True, timeout=10,
            )
            assert r.returncode == 0, f"git 未忽略 {probe}"


class TestTask2609PullDefaultGuard:
    """事务#2609：nest pull 默认/相对 local_path 垃圾实体化根治回归测试

    事故根因：_pull_file 未传 local_path 时用
    os.path.join(os.getcwd(), os.path.basename(remote_path)) 推导默认落地：
    1) POSIX basename 不切分反斜杠 → Windows remote_path 整串成为文件名
       （'C:\\Users\\Xin\\AppData\\Local\\fcc\\fcc.json' 实体化在项目根）
    2) os.getcwd() 是 peng 服务进程 cwd（=项目根）而非事务 workdir
       → workdir 不在本项目的事务也能写进本项目根
    3) #2402 护栏对"空值默认"跳过校验 → 推导路径漏网
    配套：bin/nest CLI（_cli_main.py）同款 Path.name 缺陷一并根治。
    """

    REMOTE_FCC = "C:\\Users\\Xin\\AppData\\Local\\fcc\\fcc.json"

    def test_safe_basename_windows_backslash(self):
        """事故形态：反斜杠整串 Windows 路径 → 只取末段安全文件名"""
        assert _safe_remote_basename(self.REMOTE_FCC) == "fcc.json"
        assert _safe_remote_basename("C:\\Users\\x\\AppData\\Local\\Temp\\fcc_log.txt") == "fcc_log.txt"

    def test_safe_basename_mixed_posix_unc(self):
        assert _safe_remote_basename("D:/work/fcc/temp/shot.jpg") == "shot.jpg"
        assert _safe_remote_basename("/mnt/p/feng/data/x.dat") == "x.dat"
        assert _safe_remote_basename("\\\\server\\share\\dir\\a.exe") == "a.exe"
        assert _safe_remote_basename("C:\\x\\y\\") == "y"  # 尾部分隔符

    def test_safe_basename_rejects_unsafe(self):
        """根路径/盘符裸路径/../. 等推导不出安全名 → 明确拒绝"""
        for p in ("C:", "C:\\", "/", "", "C:\\x\\..", "."):
            with pytest.raises(ToolError):
                _safe_remote_basename(p)

    def test_final_check_rejects_cross(self):
        if _local_side_platform() != "Linux":
            pytest.skip("本机非 Linux，事故场景不匹配")
        with pytest.raises(ToolError, match="平台交叉"):
            _final_local_path_check("D:\\work\\x")

    def test_final_check_accepts_valid(self):
        _final_local_path_check(os.path.join(os.getcwd(), "ok.bin"))

    @staticmethod
    def _tool(workdir: str = ""):
        t = NestTool()
        if workdir:
            t.set_context(workdir=workdir)
        return t

    def test_resolve_default_anchors_workdir(self):
        """pull 默认落地锚定事务 workdir（不再用服务进程 cwd=项目根）"""
        if os.name != "posix":
            pytest.skip("POSIX 路径形态断言")
        out = self._tool("/tmp/wd2609")._resolve_local_path("", self.REMOTE_FCC)
        assert out == "/tmp/wd2609/fcc.json"

    def test_resolve_relative_anchors_workdir(self):
        """显式相对路径锚定事务 workdir（'fcc_xin.json' 事故形态）"""
        out = self._tool("/tmp/wd2609")._resolve_local_path("fcc_xin.json", self.REMOTE_FCC)
        assert out == os.path.join("/tmp/wd2609", "fcc_xin.json")

    def test_resolve_no_context_falls_back_cwd(self, monkeypatch):
        """无 workdir 上下文（CLI/裸调用）回退进程 cwd，但 basename 仍安全"""
        monkeypatch.chdir("/tmp")
        out = self._tool()._resolve_local_path("", self.REMOTE_FCC)
        assert out == os.path.join("/tmp", "fcc.json")

    def test_resolve_disallow_default(self):
        with pytest.raises(ToolError, match="local_path"):
            self._tool("/tmp/wd2609")._resolve_local_path("", self.REMOTE_FCC, allow_default=False)

    def test_pull_file_default_path_integration(self, monkeypatch, tmp_path):
        """pull 未传 local_path：默认落事务 workdir 下安全文件名（非服务cwd、非反斜杠整串）"""
        import asyncio
        from types import SimpleNamespace

        import peng.nest.transfer as transfer_mod

        captured = {}

        class FakeTransport:
            async def request(self, action, payload, timeout=None):
                assert action == "stat"
                return SimpleNamespace(error=None, data={"is_dir": False})

        async def fake_pull_file(transport, remote_path, local_path, **kw):
            captured["remote"], captured["local"] = remote_path, local_path
            return {"path": local_path, "size": 3, "chunks": 1, "speed": 1.0}

        t = self._tool(str(tmp_path))
        monkeypatch.setattr(t, "_make_transport", lambda nest: FakeTransport())
        monkeypatch.setattr(transfer_mod, "pull_file", fake_pull_file)

        result = asyncio.run(t._pull_file({"nest": "xin", "remote_path": self.REMOTE_FCC}))
        assert captured["local"] == str(tmp_path / "fcc.json")
        assert "\\" not in captured["local"].split("/")[-1]
        assert "fcc.json" in result.summary

    def test_pull_dir_default_path_integration(self, monkeypatch, tmp_path):
        """目录 pull 默认落事务 workdir 下远端目录末段名"""
        import asyncio
        from types import SimpleNamespace

        import peng.nest.transfer as transfer_mod

        captured = {}

        class FakeTransport:
            async def request(self, action, payload, timeout=None):
                return SimpleNamespace(error=None, data={"is_dir": True})

        async def fake_sync_tree(transport, local_dir, remote_dir, direction="push", **kw):
            captured["local"] = local_dir
            return {"checked": 0, "copied": 0, "skipped": 0, "deleted": 0, "failed": 0, "total_bytes": 0}

        t = self._tool(str(tmp_path))
        monkeypatch.setattr(t, "_make_transport", lambda nest: FakeTransport())
        monkeypatch.setattr(transfer_mod, "sync_tree", fake_sync_tree)

        asyncio.run(t._pull_file({"nest": "xin", "remote_path": "C:\\Users\\x\\AppData\\Local\\Temp"}))
        assert captured["local"] == str(tmp_path / "Temp")

    def test_cli_safe_basename_shared(self):
        """bin/nest CLI 与 NestTool 共用同一推导实现（规范实现在 transfer 层）"""
        from peng.nest.transfer import safe_remote_basename

        assert safe_remote_basename(self.REMOTE_FCC) == "fcc.json"

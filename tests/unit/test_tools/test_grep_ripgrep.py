"""
ripgrep 集成测试（共享模块 + grep 工具双路径一致性 + nest 服务端）

真实 rg 可用时执行实测；不可用则跳过依赖 rg 的用例。
安装逻辑全部 mock subprocess，不触网。
"""

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from peng.common import ripgrep as rg
from peng.nest.local import LocalExecutor
from peng.tools.base import ToolError
from peng.tools.grep import GrepTool

rg_available = shutil.which("rg") is not None
needs_rg = pytest.mark.skipif(not rg_available, reason="ripgrep not installed")


@pytest.fixture
def fixture_dir(tmp_path: Path) -> Path:
    """构建测试夹具（仅白名单扩展名，保证双后端语义一致）"""
    (tmp_path / "a.txt").write_text("Hello World\nThis is a test\n")
    (tmp_path / "b.py").write_text("import os\nprint Hello\n")
    (tmp_path / "c.md").write_text("# Title\nno match here\n")

    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "d.py").write_text("nested Hello file\n")

    git = tmp_path / ".git"
    git.mkdir()
    (git / "config").write_text("xHello gitignore-test\n")

    pyc = tmp_path / "__pycache__"
    pyc.mkdir()
    (pyc / "e.py").write_text("yHello cache-test\n")

    deep = tmp_path / "x" / "y"
    deep.mkdir(parents=True)
    (deep / ".git").mkdir()
    (deep / ".git" / "f.txt").write_text("zHello nested-git-test\n")

    return tmp_path


def _sorted_lines(text: str) -> list[str]:
    return sorted(ln for ln in text.splitlines() if ln)


class TestDetection:
    def test_finds_via_which(self):
        rg.clear_cache()
        with patch("shutil.which", return_value="/fake/bin/rg"):
            assert rg.find_ripgrep() == "/fake/bin/rg"
        rg.clear_cache()

    def test_finds_via_extra_candidates(self, tmp_path, monkeypatch):
        rg.clear_cache()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        local_bin = tmp_path / ".local/bin"
        local_bin.mkdir(parents=True)
        fake = local_bin / "rg"
        fake.write_text("#!/bin/sh\n")
        with patch("shutil.which", return_value=None):
            assert rg.find_ripgrep() == str(fake)
        rg.clear_cache()

    def test_not_found(self, monkeypatch):
        rg.clear_cache()
        monkeypatch.setattr(Path, "home", lambda: Path("/nonexistent-home"))
        with patch("shutil.which", return_value=None):
            assert rg.find_ripgrep() is None
        rg.clear_cache()


class TestRegexDialect:
    @pytest.mark.parametrize(
        "pattern,expected",
        [
            ("Hello", False),
            ("\\d+\\.\\d+", False),
            ("(?i)hello", False),
            ("(?<=foo)bar", True),
            ("(?=foo)bar", True),
            ("(?!foo)bar", True),
            (r"(a) \1", True),
            ("(?P<name>x)", True),
            ("(?<name>x)", True),
        ],
    )
    def test_needs_pcre2(self, pattern, expected):
        assert rg.needs_pcre2(pattern) == expected

    def test_pcre2_flag_added_when_supported(self):
        with patch.object(rg, "supports_pcre2", return_value=True):
            args = rg.build_args("/usr/bin/rg", "(?<=a)b", ".", None, False, "content")
            assert "--pcre2" in args
        with patch.object(rg, "supports_pcre2", return_value=False):
            args = rg.build_args("/usr/bin/rg", "(?<=a)b", ".", None, False, "content")
            assert "--pcre2" not in args


class TestBuildArgs:
    def test_basic_flags(self):
        args = rg.build_args("rg", "pat", ".", "*.py", True, "content")
        assert "--json" in args
        assert "--ignore-case" in args
        assert "--hidden" in args
        assert args[-4:] == ["-e", "pat", "--", "."]

    def test_glob_order_include_before_exclude(self):
        args = rg.build_args("rg", "pat", ".", "*.py", False, "content")
        glob_idx = [i for i, a in enumerate(args) if a == "--glob"]
        glob_values = [args[i + 1] for i in glob_idx]
        assert glob_values[0] == "*.py"
        # 用户 include 在前，排除在后（实测该顺序生效）；排除用 !name（任意层级）
        assert set(glob_values) >= {"!.git", "!__pycache__", "!node_modules", "!.venv", "!venv"}

    def test_mode_flags(self):
        assert ("-l" in rg.build_args("rg", "p", ".", None, False, "files_with_matches"))
        assert ("--null" in rg.build_args("rg", "p", ".", None, False, "files_with_matches"))
        assert ("-c" in rg.build_args("rg", "p", ".", None, False, "count"))

    def test_resolve_target(self, fixture_dir):
        assert rg.resolve_target(str(fixture_dir)) == (str(fixture_dir), ".")
        f = fixture_dir / "a.txt"
        assert rg.resolve_target(str(f)) == (str(fixture_dir), "a.txt")


@needs_rg
class TestSearchParity:
    """rg 后端与 Python 回退后端结果一致性（夹具仅白名单扩展名）"""

    @pytest.mark.asyncio
    async def test_content_parity(self, fixture_dir):
        rg_res = await rg.asearch("Hello", str(fixture_dir))
        assert rg_res is not None
        paths = {m.path for m in rg_res.matches}
        assert paths == {"a.txt", "b.py", "sub/d.py"}

    @pytest.mark.asyncio
    async def test_ignore_dirs_excluded_at_any_level(self, fixture_dir):
        rg_res = await rg.asearch("Hello", str(fixture_dir))
        all_lines = [m.path for m in rg_res.matches] + rg_res.files_with_matches
        assert not any(".git" in p for p in all_lines)
        assert not any("__pycache__" in p for p in all_lines)
        assert "x/y/.git/f.txt" not in all_lines

    @pytest.mark.asyncio
    async def test_glob_filter(self, fixture_dir):
        rg_res = await rg.asearch("Hello", str(fixture_dir), glob="*.py")
        assert {m.path for m in rg_res.matches} == {"b.py", "sub/d.py"}

    @pytest.mark.asyncio
    async def test_ignore_case(self, fixture_dir):
        rg_res = await rg.asearch("hello", str(fixture_dir), ignore_case=True)
        assert {m.path for m in rg_res.matches} == {"a.txt", "b.py", "sub/d.py"}

    @pytest.mark.asyncio
    async def test_count_exact_totals(self, fixture_dir):
        rg_res = await rg.asearch("Hello", str(fixture_dir))
        assert rg_res.total_matches == 3  # a.txt, b.py, sub/d.py
        assert rg_res.files_count == 3

    @pytest.mark.asyncio
    async def test_files_mode(self, fixture_dir):
        rg_res = await rg.asearch("Hello", str(fixture_dir), output_mode="files_with_matches")
        assert sorted(rg_res.files_with_matches) == ["a.txt", "b.py", "sub/d.py"]
        assert rg_res.files_count == 3

    @pytest.mark.asyncio
    async def test_single_file(self, fixture_dir):
        rg_res = await rg.asearch("Hello", str(fixture_dir / "a.txt"))
        assert [(m.path, m.line) for m in rg_res.matches] == [("a.txt", 1)]

    @pytest.mark.asyncio
    async def test_no_match_empty_not_none(self, fixture_dir):
        rg_res = await rg.asearch("zzz_nothing", str(fixture_dir))
        assert rg_res is not None
        assert rg_res.matches == []

    @pytest.mark.asyncio
    async def test_invalid_regex_fallback_signal_none(self, fixture_dir):
        assert await rg.asearch("[bad(", str(fixture_dir)) is None

    @pytest.mark.asyncio
    async def test_pcre2_passthrough_when_supported(self, fixture_dir):
        (fixture_dir / "g.txt").write_text("fooHello bar\n")
        rg_res = await rg.asearch("(?<=foo)Hello", str(fixture_dir), glob="g.txt")
        assert rg_res is not None
        assert [m.path for m in rg_res.matches] == ["g.txt"]

    @pytest.mark.asyncio
    async def test_non_utf8_line_base64_decode(self, fixture_dir):
        raw = "GBK乱码".encode("gbk") + b" Hello\n"
        (fixture_dir / "gbk.txt").write_bytes(raw)
        rg_res = await rg.asearch("Hello", str(fixture_dir), glob="gbk.txt")
        assert rg_res is not None
        # 解码应与 Python 读取语义一致（utf-8 errors=ignore 丢失非 UTF-8 字节）
        decoded = raw.decode("utf-8", errors="ignore")
        assert rg_res.matches[0].text == decoded

    @pytest.mark.asyncio
    async def test_pcre2_unsupported_falls_back_none(self, fixture_dir):
        with patch.object(rg, "supports_pcre2", return_value=False):
            assert await rg.asearch("(?<=foo)Hello", str(fixture_dir)) is None

    @pytest.mark.asyncio
    async def test_timeout_falls_back_none(self, fixture_dir):
        # 极小超时 → 返回 None（调用方回退 Python 实现）
        assert await rg.asearch("Hello", str(fixture_dir), timeout=0.001) is None


class TestNestPathFormatting:
    """工具端对 nest 响应的格式化（rg 提供精确 total_matches 后统计不再被上限截断）"""

    @pytest.mark.asyncio
    async def test_count_uses_exact_total(self):
        tool = GrepTool()
        tool._context.clear()

        class FakeClient:
            async def grep(self, **kwargs):
                return {
                    "matches": [f"f.py:{i}:x" for i in range(1, 6)],
                    "files_count": 2,
                    "total_matches": 500,
                }

        with patch.object(GrepTool, "_get_nest_client", return_value=FakeClient()):
            out = await tool.execute(pattern="x", path="/anywhere", output_mode="count")
        assert str(out) == "找到 500 处匹配，在 2 个文件中"

    @pytest.mark.asyncio
    async def test_count_legacy_server_fallback(self):
        tool = GrepTool()
        tool._context.clear()

        class FakeClient:
            async def grep(self, **kwargs):
                return {"matches": ["f.py:1:x", "g.py:2:y"], "files_count": 2}

        with patch.object(GrepTool, "_get_nest_client", return_value=FakeClient()):
            out = await tool.execute(pattern="x", path="/anywhere", output_mode="count")
        assert str(out) == "找到 2 处匹配，在 2 个文件中"


class TestFormatOutput:
    def _result(self):
        return rg.RipgrepResult(
            matches=[rg.RipgrepMatch("a.txt", 1, "hello world\n")],
            files_with_matches=["a.txt"],
            files_count=1,
            total_matches=1,
        )

    def test_content_format(self):
        assert rg.format_output(self._result(), "content", 100, 2000) == "a.txt:1:hello world"

    def test_content_line_truncation(self):
        result = rg.RipgrepResult(
            matches=[rg.RipgrepMatch("a.txt", 1, "x" * 50 + "\n")],
            files_with_matches=["a.txt"], files_count=1, total_matches=1,
        )
        out = rg.format_output(result, "content", 100, 10)
        assert out == "a.txt:1:" + "x" * 10 + "...\n\n(1 lines were truncated to 10 chars)"

    def test_content_truncated_hint(self):
        result = rg.RipgrepResult(
            matches=[rg.RipgrepMatch("a.txt", i, "m") for i in range(1, 4)],
            files_with_matches=["a.txt"], files_count=1, total_matches=10,
        )
        out = rg.format_output(result, "content", 3, 2000)
        assert "Results truncated: showing 3 of 10" in out

    def test_files_mode_and_hint(self):
        result = rg.RipgrepResult(
            files_with_matches=["a.txt", "b.txt", "c.txt"], files_count=3, total_matches=3
        )
        assert rg.format_output(result, "files_with_matches", 100, 2000) == "a.txt\nb.txt\nc.txt"
        out = rg.format_output(result, "files_with_matches", 2, 2000)
        assert "... 还有 1 个文件" in out

    def test_count_format(self):
        result = rg.RipgrepResult(files_count=5, total_matches=12)
        assert rg.format_output(result, "count", 100, 2000) == "找到 12 处匹配，在 5 个文件中"

    def test_no_match(self):
        empty = rg.RipgrepResult()
        for mode in ("content", "files_with_matches"):
            assert rg.format_output(empty, mode, 100, 2000) == "未找到匹配"


@needs_rg
class TestGrepToolLocalPathIntegration:
    """GrepTool 本地路径（无 nest）：rg 优先，mock 不可用时回退 Python，输出一致"""

    @pytest.mark.asyncio
    async def test_end_to_end_parity(self, fixture_dir):
        tool = GrepTool()
        tool._context.clear()
        with patch.object(GrepTool, "_get_nest_client", return_value=None):
            out_rg = await tool.execute(pattern="Hello", path=str(fixture_dir))
            with patch.object(rg, "find_ripgrep", return_value=None):
                out_py = await tool.execute(pattern="Hello", path=str(fixture_dir))
        assert _sorted_lines(str(out_rg)) == _sorted_lines(str(out_py))

    @pytest.mark.asyncio
    async def test_end_to_end_count_parity(self, fixture_dir):
        tool = GrepTool()
        tool._context.clear()
        with patch.object(GrepTool, "_get_nest_client", return_value=None):
            out_rg = await tool.execute(
                pattern="Hello", path=str(fixture_dir), output_mode="count"
            )
            with patch.object(rg, "find_ripgrep", return_value=None):
                out_py = await tool.execute(
                    pattern="Hello", path=str(fixture_dir), output_mode="count"
                )
        assert str(out_rg) == str(out_py) == "找到 3 处匹配，在 3 个文件中"

    @pytest.mark.asyncio
    async def test_invalid_regex_raises_toolerror(self, fixture_dir):
        tool = GrepTool()
        tool._context.clear()
        with patch.object(GrepTool, "_get_nest_client", return_value=None):
            with pytest.raises(ToolError) as exc:
                await tool.execute(pattern="[bad(", path=str(fixture_dir))
        assert "无效的正则表达式" in str(exc.value)

    @pytest.mark.asyncio
    async def test_path_not_exists_raises(self, tmp_path):
        tool = GrepTool()
        tool._context.clear()
        with patch.object(GrepTool, "_get_nest_client", return_value=None):
            with pytest.raises(ToolError):
                await tool.execute(pattern="x", path=str(tmp_path / "nope"))


@needs_rg
class TestNestHandlerIntegration:
    """nest 服务端 _handle_grep（实际远程执行路径）：rg 优先 + Python 回退"""

    @pytest.mark.asyncio
    async def test_handler_uses_ripgrep(self, fixture_dir):
        ex = LocalExecutor(security=None)
        resp = await ex._handle_grep({"pattern": "Hello", "path": str(fixture_dir)})
        matches = {m.split(":", 1)[0] for m in resp.data["matches"]}
        assert matches == {"a.txt", "b.py", "sub/d.py"}
        assert resp.data["files_count"] == 3
        assert resp.data["total_matches"] == 3

    @pytest.mark.asyncio
    async def test_handler_fallback_python_parity(self, fixture_dir):
        ex = LocalExecutor(security=None)
        resp_rg = await ex._handle_grep({"pattern": "Hello", "path": str(fixture_dir)})
        with patch.object(rg, "find_ripgrep", return_value=None):
            resp_py = await ex._handle_grep({"pattern": "Hello", "path": str(fixture_dir)})
        assert _sorted_lines("\n".join(resp_rg.data["matches"])) == _sorted_lines(
            "\n".join(resp_py.data["matches"])
        )
        assert resp_rg.data["files_count"] == resp_py.data["files_count"] == 3

    @pytest.mark.asyncio
    async def test_handler_glob_and_case(self, fixture_dir):
        ex = LocalExecutor(security=None)
        resp = await ex._handle_grep(
            {"pattern": "hello", "path": str(fixture_dir), "glob": "*.py", "ignore_case": True}
        )
        matches = {m.split(":", 1)[0] for m in resp.data["matches"]}
        assert matches == {"b.py", "sub/d.py"}

    @pytest.mark.asyncio
    async def test_handler_invalid_regex(self, fixture_dir):
        ex = LocalExecutor(security=None)
        resp = await ex._handle_grep({"pattern": "[bad(", "path": str(fixture_dir)})
        assert "无效的正则表达式" in (resp.error or "")


@needs_rg
class TestEnsureRipgrep:
    def test_idempotent_existing(self, capsys):
        with patch.object(rg, "find_ripgrep", return_value="/usr/bin/rg"):
            assert rg.ensure_ripgrep() == "/usr/bin/rg"
        assert "已安装" in capsys.readouterr().out

    def test_install_via_package_manager(self, capsys):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)

            class P:
                returncode = 0

            return P()

        # 未装 → 包管理器安装成功 → 再检测到
        finds = [None, "/usr/bin/rg"]
        with (
            patch.object(rg, "find_ripgrep", side_effect=finds),
            patch("shutil.which", return_value="/usr/bin/apt-get"),
            patch("subprocess.run", side_effect=fake_run),
            patch.object(rg, "_sudo_prefix", return_value=[]),
        ):
            assert rg.ensure_ripgrep() == "/usr/bin/rg"
        assert any("ripgrep" in " ".join(c) for c in calls)

    def test_install_fallback_release(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # 无包管理器 → 走 GitHub release 下载
        with (
            patch.object(rg, "find_ripgrep", side_effect=[None, str(tmp_path / ".local/bin/rg")]),
            patch("shutil.which", return_value=None),
            patch.object(rg, "install_via_package_manager", return_value=False),
            patch.object(rg, "install_via_release", return_value=True) as m,
        ):
            rg.ensure_ripgrep()
        m.assert_called_once()

    @pytest.mark.asyncio
    async def test_release_download(self, tmp_path, monkeypatch):
        import io
        import tarfile

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # 构造 tar.gz 假包
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            data = b"#!/bin/sh\n"
            info = tarfile.TarInfo("ripgrep-x/rg")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        raw = buf.getvalue()

        class FakeResp:
            def __init__(self, payload: bytes):
                self._payload = payload

            def read(self):
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        responses = iter(
            [
                FakeResp(b'{"tag_name": "v14.1.1"}'),  # GitHub API
                FakeResp(raw),  # 下载
            ]
        )

        def fake_urlopen(req, timeout=None):
            return next(responses)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        monkeypatch.setattr("platform.machine", lambda: "x86_64")

        assert rg.install_via_release() is True
        assert (tmp_path / ".local/bin/rg").exists()

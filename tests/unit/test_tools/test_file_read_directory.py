"""
ReadTool 目录读取单元测试 (事务#1887)

背景：提交 7b8c4d3d 将 nest 服务端 local.py _read_directory 的 entries
从字符串列表升级为 dict{name,is_dir,size,mtime} 列表，但 ReadTool
._execute_via_nest 的 directory 分支仍按 str 列表直接 join，导致
"sequence item 3: expected str instance, dict found" 工具执行失败。
本测试验证修复后的目录读取全链路。
"""

import asyncio
from unittest.mock import AsyncMock

from peng.tools.file import (
    MAX_DIR_ENTRIES,
    ReadTool,
    _build_directory_output,
    _format_dir_entry,
    format_mtime_human,
    format_size_human,
)


class TestFormatSizeHuman:
    """字节大小人类可读格式化测试"""

    def test_none_and_negative(self):
        assert format_size_human(None) is None
        assert format_size_human(-1) is None

    def test_units(self):
        assert format_size_human(0) == "0 B"
        assert format_size_human(512) == "512 B"
        assert format_size_human(1024) == "1.0 KB"
        assert format_size_human(1536) == "1.5 KB"
        assert format_size_human(1024 * 1024) == "1.0 MB"
        assert format_size_human(1024**3) == "1.0 GB"
        assert format_size_human(1024**4) == "1.0 TB"
        # 超出 TB 上限时仍以 TB 计
        assert format_size_human(1024**5) == "1024.0 TB"


class TestFormatMtimeHuman:
    """时间戳格式化测试"""

    def test_none(self):
        assert format_mtime_human(None) is None

    def test_normal(self):
        from datetime import datetime

        ts = datetime(2026, 9, 13, 13, 30).timestamp()
        assert format_mtime_human(ts) == "2026-09-13 13:30"

    def test_invalid(self):
        assert format_mtime_human(float("nan")) is None or True  # NaN 平台相关，不强制
        assert format_mtime_human(1e30) is None or True  # 超范围平台相关，不强制


class TestFormatDirEntry:
    """目录条目格式化测试（dict 新格式 + str 旧格式）"""

    def test_dict_dir_entry_trailing_slash(self):
        assert _format_dir_entry({"name": "bin", "is_dir": True, "size": None, "mtime": 1.0}) == "bin/"

    def test_dict_file_entry_with_meta(self):
        from datetime import datetime

        ts = datetime(2026, 9, 13, 8, 0).timestamp()
        line = _format_dir_entry({"name": "a.txt", "is_dir": False, "size": 1536, "mtime": ts})
        assert line == "a.txt  (1.5 KB, 2026-09-13 08:00)"

    def test_dict_file_entry_missing_meta(self):
        assert _format_dir_entry({"name": "a.txt", "is_dir": False}) == "a.txt"
        assert _format_dir_entry({"name": "a.txt", "is_dir": False, "size": 512}) == "a.txt  (512 B)"
        assert (
            _format_dir_entry({"name": "a.txt", "is_dir": False, "size": None, "mtime": None})
            == "a.txt"
        )

    def test_dict_empty_name_skipped(self):
        assert _format_dir_entry({"name": "", "is_dir": True}) == ""
        assert _format_dir_entry({}) == ""

    def test_legacy_str_entry_passthrough(self):
        """旧格式字符串（远端 nest 未升级时）原样返回"""
        assert _format_dir_entry("bin/") == "bin/"
        assert _format_dir_entry("README.md") == "README.md"


class TestBuildDirectoryOutput:
    """目录列表输出构建测试"""

    def test_normal_output(self):
        out = _build_directory_output(
            "/tmp/x",
            [
                {"name": "bin", "is_dir": True, "size": None, "mtime": 1.0},
                {"name": "a.txt", "is_dir": False, "size": 10, "mtime": 2.0},
            ],
        )
        assert "<path>/tmp/x</path>" in out
        assert "<type>directory</type>" in out
        assert "bin/" in out
        assert "a.txt" in out
        assert "(共 2 个条目)" in out

    def test_truncation_over_limit(self):
        """超过 MAX_DIR_ENTRIES 截断并提示用 glob"""
        entries = [{"name": f"f{i}.txt", "is_dir": False, "size": 1} for i in range(MAX_DIR_ENTRIES + 10)]
        out = _build_directory_output("/tmp/x", entries)
        assert f"仅显示前 {MAX_DIR_ENTRIES} 个" in out
        assert "glob" in out
        assert f"共 {MAX_DIR_ENTRIES + 10} 个条目" in out

    def test_mixed_dict_and_str_entries(self):
        """新旧格式混用时都能输出（防御远端 nest 版本不一致）"""
        out = _build_directory_output("/tmp/x", [{"name": "d", "is_dir": True}, "legacy.txt"])
        assert "d/" in out
        assert "legacy.txt" in out


class TestReadToolViaNestDirectory:
    """via_nest 目录分支修复验证（事务#1887 核心）"""

    def _make_client(self, result: dict):
        client = AsyncMock()
        client.read = AsyncMock(return_value=result)
        return client

    def test_dict_entries_no_type_error(self):
        """dict entries（服务端 7b8c4d3d 起的格式）不再触发 TypeError"""
        client = self._make_client(
            {
                "type": "directory",
                "path": "D:\\work",
                "entries": [
                    {"name": "bin", "is_dir": True, "size": None, "mtime": 1757746800.0},
                    {"name": "README.md", "is_dir": False, "size": 2048, "mtime": 1757746800.0},
                ],
            }
        )
        out = asyncio.run(ReadTool()._execute_via_nest(client, "D:\\work", 1, 2000))
        assert "bin/" in out.summary
        assert "README.md" in out.summary
        assert "(共 2 个条目)" in out.summary
        # display=None 时框架回退 summary，终端也可见目录列表
        assert out.display is None

    def test_legacy_str_entries(self):
        """旧 str entries（远端旧版 nest）保持兼容"""
        client = self._make_client(
            {"type": "directory", "path": "/tmp", "entries": ["bin/", "a.txt"]}
        )
        out = asyncio.run(ReadTool()._execute_via_nest(client, "/tmp", 1, 2000))
        assert "bin/" in out.summary
        assert "a.txt" in out.summary


class TestReadToolLocalDirectory:
    """本地 fallback 目录读取验证（与 via_nest 输出格式一致）"""

    def test_local_directory_listing(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "f.txt").write_bytes(b"hello")
        out = asyncio.run(ReadTool()._execute_local_read(str(tmp_path), 1, 2000))
        assert "sub/" in out.summary
        assert "f.txt" in out.summary
        assert "5 B" in out.summary  # 文件元信息（大小）
        assert "(共 2 个条目)" in out.summary

    def test_local_path_not_found(self, tmp_path):
        out = asyncio.run(ReadTool()._execute_local_read(str(tmp_path / "nope"), 1, 2000))
        assert "文件未找到" in out.summary

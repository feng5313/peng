"""code_display 单元测试：工具调用参数人类易读+语法高亮显示

覆盖：
- format_write_display：路径头/语言识别/高亮/截断/空内容
- format_edit_display：修改前后分区/前缀/截断
- 每行 ANSI 状态自包含（跨行字符串不泄漏）
- 无裸 \t；纯 ANSI 残留空行被清洗
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from peng.common.code_display import (  # noqa: E402
    MAX_DISPLAY_LINES,
    _strip_ansi,
    format_edit_display,
    format_line_edit_display,
    format_markdown_display,
    format_write_display,
    highlight_code,
)

ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")


def _visible(text: str) -> str:
    """去除 ANSI 后的可见文本"""
    return ANSI_RE.sub("", text)


class TestHighlightCode:
    def test_python_language_detected(self):
        lines, lang, omitted = highlight_code("a/b.py", "x = 1\n")
        assert lang == "Python"
        assert omitted == 0
        assert any("x = " in ln for ln in lines)

    def test_unknown_ext_falls_back_text(self):
        lines, lang, omitted = highlight_code("file.xyz123", "hello\n")
        assert lang == "文本"
        assert lines == ["hello"]

    def test_empty_content(self):
        lines, lang, omitted = highlight_code("a.py", "")
        assert lines == []
        assert "空" in lang

    def test_truncation(self):
        content = "\n".join(f"line {i}" for i in range(200))
        lines, lang, omitted = highlight_code("big.js", content)
        assert len(lines) == MAX_DISPLAY_LINES
        assert omitted == 120

    def test_multiline_string_ansi_selfcontained(self):
        """多行字符串：每行行首必须重新携带颜色序列（按行拆分后不丢色）"""
        content = 's = """m1\nm2\nm3"""\nx = 1\n'
        lines, _, _ = highlight_code("s.py", content)
        # 找到多行字符串的延续行（含 m2/m3 的行），必须自带 ANSI 颜色序列
        m2_line = next(ln for ln in lines if "m2" in _visible(ln))
        m3_line = next(ln for ln in lines if "m3" in _visible(ln))
        assert "\x1b[" in m2_line, f"延续行丢失颜色状态: {m2_line!r}"
        assert "\x1b[" in m3_line, f"延续行丢失颜色状态: {m3_line!r}"

    def test_no_bare_tab(self):
        content = "if x:\n\treturn 1\n"
        lines, _, _ = highlight_code("a.py", content)
        for ln in lines:
            assert "\t" not in ln

    def test_ansi_only_lines_cleaned(self):
        """仅含 ANSI 残留的空行应被清洗为纯空行"""
        content = "a = 1\n\n\nb = 2\n"
        lines, _, _ = highlight_code("a.py", content)
        for ln in lines:
            if not _visible(ln).strip():
                assert ln == ""  # 空行必须是纯空串，不能残留 ANSI

    def test_crlf_normalized(self):
        lines, _, _ = highlight_code("a.py", "x = 1\r\ny = 2\r\n")
        assert all("\r" not in ln for ln in lines)


class TestFormatWriteDisplay:
    def test_contains_path_and_meta(self):
        out = format_write_display("src/main.py", "print(1)\n")
        first = out.split("\n")[0]
        assert "src/main.py" in first
        assert "Python" in first

    def test_separator_lines(self):
        out = format_write_display("a.py", "x = 1\n")
        lines = out.split("\n")
        assert _visible(lines[1]).startswith("─")
        assert _visible(lines[-1]).startswith("─")

    def test_empty_file_display(self):
        out = format_write_display("empty.md", "")
        assert "空" in out

    def test_truncation_note(self):
        content = "\n".join(f"v = {i}" for i in range(150))
        out = format_write_display("big.py", content)
        assert "已省略后续 70 行" in out

    def test_multiline_output(self):
        content = "\n".join(f"v = {i}" for i in range(10))
        out = format_write_display("a.py", content)
        # header + sep + 10行 + sep
        assert len(out.split("\n")) == 13


class TestFormatEditDisplay:
    def test_unified_diff_format(self):
        """edit 显示为 unified diff 形式：@@ 定位行 + -/+ 变更行"""
        out = format_edit_display(
            "a.py",
            "def f():\n    return 1\n",
            "def f():\n    return 2\n",
        )
        lines = [_strip_ansi(ln) for ln in out.split("\n")]
        # header
        assert lines[0].startswith("a.py")
        assert "替换" in lines[0]
        # @@ hunk 定位行
        assert any(ln.startswith("@@") for ln in lines)
        # -/+ 变更行
        assert any(ln.startswith("-") and "return 1" in ln for ln in lines)
        assert any(ln.startswith("+") and "return 2" in ln for ln in lines)
        # 上下文行（前导空格）
        assert any(ln.startswith(" ") and "def f():" in ln for ln in lines)

    def test_header_counts(self):
        out = format_edit_display("a.py", "x = 1\ny = 2", "x = 1\ny = 2\nz = 3")
        assert "替换 2 行 → 3 行" in out

    def test_no_diff_when_identical(self):
        out = format_edit_display("a.py", "same\n", "same\n")
        assert "无差异" in out

    def test_pure_addition(self):
        out = format_edit_display("new.txt", "", "hello\nworld\n")
        plain = [_strip_ansi(ln) for ln in out.split("\n")]
        assert any(ln == "+hello" for ln in plain)
        assert any(ln == "+world" for ln in plain)

    def test_total_truncation(self):
        """diff 总行数超限时截断并提示"""
        old = "\n".join(f"old {i}" for i in range(100))
        new = "\n".join(f"new {i}" for i in range(100))
        out = format_edit_display("a.py", old, new)
        assert "已省略后续" in out


class TestFormatLineEditDisplay:
    def test_insert_operation(self):
        out = format_line_edit_display(
            "a.py",
            {"filePath": "a.py", "operation": "insert", "line": 42, "content": "x = 1\n"},
        )
        first = out.split("\n")[0]
        assert "a.py" in first
        assert "插入" in first
        assert "行 42" in first
        assert "插入内容（+）" in out
        assert any(ln.startswith("+ ") for ln in out.split("\n"))

    def test_delete_operation_no_content(self):
        out = format_line_edit_display(
            "a.py",
            {"filePath": "a.py", "operation": "delete", "startLine": 10, "endLine": 12},
        )
        first = out.split("\n")[0]
        assert "删除" in first
        assert "行 10-12" in first
        assert "空内容" not in first  # delete 无 content，不显示语言/空内容标签

    def test_replace_with_range(self):
        out = format_line_edit_display(
            "a.py",
            {
                "filePath": "a.py",
                "operation": "replace",
                "startLine": 5,
                "endLine": 7,
                "content": "x = 1\ny = 2\n",
            },
        )
        first = out.split("\n")[0]
        assert "替换" in first
        assert "行 5-7" in first


class TestFormatMarkdownDisplay:
    def test_header_meta(self):
        out = format_markdown_display("## 标题\n正文\n")
        assert "Markdown" in out.split("\n")[0]
        assert "2 行" in out.split("\n")[0]

    def test_highlight_ansi_present(self):
        out = format_markdown_display("## 标题\n- 列表项\n")
        assert "\x1b[" in out  # 含 ANSI 高亮
        assert "## 标题" in _strip_ansi(out)
        assert "- 列表项" in _strip_ansi(out)

    def test_empty_content(self):
        out = format_markdown_display("")
        assert "空" in out

    def test_truncation(self):
        content = "\n".join(f"- 项目 {i}" for i in range(120))
        out = format_markdown_display(content)
        assert "已省略后续 40 行" in out

    def test_no_bare_tab(self):
        out = format_markdown_display("- a\n\t- b\n")
        assert "\t" not in out

    def test_ansi_only_lines_cleaned(self):
        out = format_markdown_display("a\n\n\nb")
        for ln in out.split("\n"):
            if not _strip_ansi(ln).strip():
                assert ln == ""


class TestAnsiSelfContained:
    def test_every_line_independent(self):
        """每行内容拆出后独立可渲染：无跨行未闭合的颜色状态要求

        pygments TerminalFormatter 每 token 自带重置序列，因此每行
        （除纯空行）都应携带完整闭合的 ANSI 序列。
        """
        content = 'x = """a\nb"""\ny = 2\n'
        lines, _, _ = highlight_code("a.py", content)
        for ln in lines:
            visible = _visible(ln)
            if not visible.strip():
                continue
            # 有可见内容的行不应以未闭合的转义符结尾
            assert not visible.endswith("\x1b"), f"裸转义符: {ln!r}"


class TestStripAnsi:
    def test_strip(self):
        assert _strip_ansi("\x1b[34mabc\x1b[39;49;00m") == "abc"
        assert _strip_ansi("plain") == "plain"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

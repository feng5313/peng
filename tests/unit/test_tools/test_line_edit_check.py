"""
line_edit 重复内容检测测试（#1559）

背景：事务#1558分析确认，大模型长上下文连续行编辑时构造的content参数
常出现边界重复/内部残留/无变化三类错误，工具忠实执行导致文件重复行。
本测试覆盖 peng.common.line_edit_check.detect_line_edit_duplication
以及 LocalExecutor._handle_line_edit / LineEditTool._execute_local 的接入。
"""

import pytest

from peng.common.line_edit_check import detect_line_edit_duplication as d


class TestDetectDuplication:
    """detect_line_edit_duplication 纯函数测试"""

    def test_internal_repetition(self):
        """内部连续重复行（1558事故20:19:08：函数签名写两遍）"""
        content = [
            "static void renderMetadataItemsList(...);",
            "                  MetaListUiState& ui, Layer* ownerLayer) {",
            "                    MetaListUiState& ui, Layer* ownerLayer) {",
        ]
        lines = ["class A {", "  void f();", "};", "void g() {", "int x;", "}"]
        w = d(lines, "replace", 4, 4, None, content)
        assert w is not None and "内容残留" in w

    def test_tail_boundary_duplication(self):
        """末行与编辑区下方紧邻旧行重复（1558事故20:44:49）"""
        lines = ["a", "dbgPrint(1);", "int old;", "editorExitPreviewMode();"]
        content = ["dbgPrint(1);", "int new;", "editorExitPreviewMode();"]
        w = d(lines, "replace", 2, 3, None, content)
        assert w is not None and "重复插入" in w and "第 4 行" in w

    def test_head_boundary_duplication(self):
        """首行与编辑区上方紧邻旧行重复"""
        lines = ["void f() {", "  int x;", "}", "void g() {}"]
        content = ["void f() {", "  int y;", "}"]
        w = d(lines, "replace", 2, 3, None, content)
        assert w is not None and "重复插入" in w and "第 1 行" in w

    def test_no_change_edit(self):
        """新内容与被替换原内容完全相同（行号认知错误）"""
        w = d(["a", "b", "c"], "replace", 2, 2, None, ["b"])
        assert w is not None and "无变化" in w

    def test_trivial_lines_exempt(self):
        """平凡行（空行/{/}）连续出现不告警"""
        lines = ["int f() {", "  return 1;", "}", "", "void g() {}"]
        w = d(lines, "replace", 4, 4, None, ["", "", "}"])
        assert w is None

    def test_normal_edit_no_warning(self):
        """正常编辑无告警"""
        w = d(["a", "b", "c"], "replace", 2, 2, None, ["new_line"])
        assert w is None

    def test_insert_after_tail_dup(self):
        """insert after: 末行与插入点下方行重复"""
        lines = ["x = 1;", "x = 2;", "x = 3;"]
        w = d(lines, "insert", None, None, 1, ["inserted", "x = 2;"], "after")
        assert w is not None and "重复插入" in w

    def test_insert_before_head_dup(self):
        """insert before: 首行与插入点上方行重复"""
        lines = ["x = 1;", "x = 2;", "x = 3;"]
        w = d(lines, "insert", None, None, 2, ["x = 1;", "inserted"], "before")
        assert w is not None and "重复插入" in w and "第 1 行" in w

    def test_insert_after_head_dup(self):
        """insert after: 首行与插入行自身重复"""
        lines = ["x = 1;", "x = 2;", "x = 3;"]
        w = d(lines, "insert", None, None, 2, ["x = 2;", "inserted"], "after")
        assert w is not None and "重复插入" in w and "第 2 行" in w

    def test_insert_boundary_below(self):
        """insert after line=2: 末行与原第3行重复"""
        lines = ["x = 1;", "x = 2;", "x = 3;"]
        w = d(lines, "insert", None, None, 2, ["inserted", "x = 3;"], "after")
        assert w is not None and "重复插入" in w and "第 3 行" in w

    def test_delete_no_detection(self):
        """delete操作不参与检测"""
        w = d(["a", "b", "c"], "delete", 1, 2, None, [])
        assert w is None

    def test_indent_diff_stripped(self):
        """缩进差异按strip比较（识别无变化）"""
        lines = ["int a;", "    int a;", "z"]
        w = d(lines, "replace", 2, 2, None, ["    int a;"])
        assert w is not None and "无变化" in w

    def test_long_repetition_run(self):
        """3行连续重复合并为一次告警"""
        content = ["sig();", "sig();", "sig();", "body;"]
        w = d(["a", "b"], "replace", 1, 1, None, content)
        assert w is not None and "内容残留" in w

    def test_multiple_warnings(self):
        """边界重复+内部残留同时存在时都报告"""
        w = d(["sig();", "old;", "sig();"], "replace", 2, 2, None, ["sig();", "sig();"])
        assert w is not None and "重复插入" in w and "内容残留" in w

    def test_empty_lines_blank_exempt(self):
        """空行连续不告警"""
        w = d(["{", "", "{", "code();"], "replace", 2, 2, None, ["", ""])
        assert w is None


class TestLocalExecutorLineEditWarning:
    """LocalExecutor._handle_line_edit 服务端接入测试"""

    @pytest.fixture
    def executor(self):
        from peng.nest.local import LocalExecutor

        return LocalExecutor()

    @pytest.mark.asyncio
    async def test_replace_tail_dup_warning(self, executor, tmp_path):
        """服务端replace末行重复→响应含warning字段"""
        f = tmp_path / "t.cpp"
        f.write_text("a\nfoo(1);\nbar;\nbaz();\n", encoding="utf-8")
        resp = await executor._handle_line_edit(
            {
                "path": str(f),
                "operation": "replace",
                "start_line": 2,
                "end_line": 3,
                "content": "foo(1);\nnew;\nbaz();",
            }
        )
        assert resp.error is None
        assert resp.data["warning"] is not None and "重复插入" in resp.data["warning"]
        assert "重复插入" in resp.data["description"]
        # 文件仍被忠实编辑（告警不阻断；末行baz()与原第4行重复出现两遍=告警点）
        assert f.read_text(encoding="utf-8") == "a\nfoo(1);\nnew;\nbaz();\nbaz();\n"

    @pytest.mark.asyncio
    async def test_replace_normal_no_warning(self, executor, tmp_path):
        """服务端正常replace无warning"""
        f = tmp_path / "t.cpp"
        f.write_text("a\nb\nc\n", encoding="utf-8")
        resp = await executor._handle_line_edit(
            {
                "path": str(f),
                "operation": "replace",
                "start_line": 2,
                "end_line": 2,
                "content": "brand_new;",
            }
        )
        assert resp.error is None
        assert resp.data.get("warning") is None
        assert f.read_text(encoding="utf-8") == "a\nbrand_new;\nc\n"


class TestLineEditToolLocalWarning:
    """LineEditTool._execute_local 客户端fallback接入测试"""

    @pytest.fixture
    def tool(self):
        from peng.tools.file import LineEditTool

        return LineEditTool()

    @pytest.mark.asyncio
    async def test_local_replace_dup_warning(self, tool, tmp_path):
        """本地fallback replace末行重复→summary含告警"""
        f = tmp_path / "t.cpp"
        f.write_text("a\nfoo(1);\nbar;\nbaz();\n", encoding="utf-8")
        result = await tool._execute_local(
            str(f), "replace", 2, 3, None, "foo(1);\nnew;\nbaz();", "after"
        )
        assert "重复插入" in result.summary
        assert f.read_text(encoding="utf-8") == "a\nfoo(1);\nnew;\nbaz();\nbaz();\n"

    @pytest.mark.asyncio
    async def test_local_replace_internal_dup(self, tool, tmp_path):
        """本地fallback content内部重复→summary含残留告警"""
        f = tmp_path / "t.cpp"
        f.write_text("int main() {\n  old;\n}\n", encoding="utf-8")
        result = await tool._execute_local(
            str(f), "replace", 2, 2, None, "sig();\nsig();", "after"
        )
        assert "内容残留" in result.summary

    @pytest.mark.asyncio
    async def test_local_insert_dup_warning(self, tool, tmp_path):
        """本地fallback insert边界重复→summary含告警"""
        f = tmp_path / "t.cpp"
        f.write_text("x = 1;\nx = 2;\n", encoding="utf-8")
        result = await tool._execute_local(
            str(f), "insert", None, None, 1, "x = 1;\ntail;", "after"
        )
        assert "重复插入" in result.summary

    @pytest.mark.asyncio
    async def test_local_normal_no_warning(self, tool, tmp_path):
        """本地fallback正常编辑无告警"""
        f = tmp_path / "t.cpp"
        f.write_text("a\nb\nc\n", encoding="utf-8")
        result = await tool._execute_local(
            str(f), "replace", 2, 2, None, "brand_new;", "after"
        )
        assert "⚠️" not in result.summary

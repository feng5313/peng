#!/usr/bin/env python3
"""文件工具参数名自动纠错测试（事务#2095）

背景：大模型偶发用 snake_case 参数名调用文件工具
（file_path/old_string/new_string/replace_all/start_line/end_line），
正确名为 camelCase（filePath/oldString/newString/replaceAll/startLine/
endLine）。错误参数被忽略后 filePath 落空，返回"filePath 是必填项"，
白白浪费一轮迭代重试。

修复：file.py 四个工具 execute 入口接入 normalize_arg_names，按工具
声明的 JSON Schema properties 自动纠正参数名（仅纠正未声明但 camelCase
形式已声明的名字，正确名优先，未知参数原样保留）。
"""

import asyncio
import logging

import pytest

from peng.tools.file import (
    EditTool,
    LineEditTool,
    ReadTool,
    WriteTool,
    _snake_to_camel,
    normalize_arg_names,
)


def _run(coro):
    return asyncio.run(coro)


def _props(tool_cls) -> dict:
    return tool_cls().parameters["properties"]


# ============================================================
# _snake_to_camel 基础转换
# ============================================================


class TestSnakeToCamel:
    @pytest.mark.parametrize(
        ("snake", "camel"),
        [
            ("file_path", "filePath"),
            ("old_string", "oldString"),
            ("new_string", "newString"),
            ("replace_all", "replaceAll"),
            ("start_line", "startLine"),
            ("end_line", "endLine"),
            ("offset", "offset"),  # 无下划线原样
            ("limit", "limit"),
            ("content", "content"),
            ("a_b_c", "aBC"),
        ],
    )
    def test_conversion(self, snake, camel):
        assert _snake_to_camel(snake) == camel


# ============================================================
# normalize_arg_names 纯函数测试（用真实工具的参数 schema）
# ============================================================


class TestNormalizeArgNames:
    def test_edit_core_scenario(self):
        """事务#2095核心场景：edit 的 file_path/old_string/new_string 全部纠正"""
        props = _props(EditTool)
        out = normalize_arg_names(
            {
                "file_path": "/tmp/a.py",
                "old_string": "print(1)",
                "new_string": "print(2)",
            },
            props,
        )
        assert out == {
            "filePath": "/tmp/a.py",
            "oldString": "print(1)",
            "newString": "print(2)",
        }

    def test_edit_replace_all(self):
        """replace_all → replaceAll"""
        out = normalize_arg_names(
            {"filePath": "/tmp/a.py", "old_string": "a", "new_string": "b", "replace_all": True},
            _props(EditTool),
        )
        assert out["replaceAll"] is True
        assert "replace_all" not in out

    def test_line_edit_start_end_line(self):
        """line_edit 的 file_path/start_line/end_line → filePath/startLine/endLine"""
        out = normalize_arg_names(
            {
                "file_path": "/tmp/a.py",
                "operation": "delete",
                "start_line": 2,
                "end_line": 3,
            },
            _props(LineEditTool),
        )
        assert out["filePath"] == "/tmp/a.py"
        assert out["startLine"] == 2
        assert out["endLine"] == 3
        assert "file_path" not in out and "start_line" not in out and "end_line" not in out

    def test_read_file_path(self):
        out = normalize_arg_names({"file_path": "/tmp/a.py", "offset": 5}, _props(ReadTool))
        assert out == {"filePath": "/tmp/a.py", "offset": 5}

    def test_valid_names_unchanged(self):
        """全部正确的参数名原样保留（幂等）"""
        src = {
            "filePath": "/tmp/a.py",
            "oldString": "a",
            "newString": "b",
            "replaceAll": False,
            "purpose": "测试",
        }
        assert normalize_arg_names(src, _props(EditTool)) == src

    def test_correct_name_wins(self):
        """同时传正确名和错误名时正确名优先，错误名丢弃不覆盖"""
        out = normalize_arg_names(
            {"filePath": "/correct.py", "file_path": "/wrong.py"}, _props(EditTool)
        )
        assert out == {"filePath": "/correct.py"}

    def test_unknown_params_preserved(self):
        """未声明的未知参数（camel 形式也不在 schema 中）原样保留"""
        out = normalize_arg_names({"filePath": "/a.py", "foo_bar": 1}, _props(EditTool))
        assert out == {"filePath": "/a.py", "foo_bar": 1}

    def test_no_underscore_keys_untouched(self):
        """无下划线的未知参数不参与纠正"""
        out = normalize_arg_names({"filePath": "/a.py", "startline": 3}, _props(LineEditTool))
        assert out["startline"] == 3  # 原样保留（不是 startLine）

    @pytest.mark.parametrize("empty", [{}, {"filePath": "/a.py"}])
    def test_empty_input_noop(self, empty):
        assert normalize_arg_names(empty, _props(ReadTool)) == empty

    def test_empty_properties_noop(self):
        src = {"file_path": "/a.py"}
        assert normalize_arg_names(src, {}) == src

    def test_rename_logged(self, caplog):
        """发生纠正时记录 info 日志，便于观测"""
        with caplog.at_level(logging.INFO, logger="peng.tools.file"):
            normalize_arg_names({"file_path": "/a.py"}, _props(ReadTool))
        assert "已自动纠正" in caplog.text and "file_path→filePath" in caplog.text


# ============================================================
# execute 集成测试：错误参数名端到端自动纠正后成功执行
# （_get_nest_client 置 None + 空 context → is_local_nest=True → 本地路径）
# ============================================================


@pytest.fixture
def no_nest(monkeypatch):
    """屏蔽 nest 客户端，让四个工具走本地 fallback 路径"""

    def _patch(tool_cls):
        monkeypatch.setattr(tool_cls, "_get_nest_client", lambda self: None)

    return _patch


class TestExecuteAutoCorrection:
    def test_edit_with_wrong_names(self, tmp_path, no_nest):
        """edit 全套 snake_case 参数 → 纠正后成功编辑文件（此前报 filePath 必填）"""
        no_nest(EditTool)
        f = tmp_path / "a.txt"
        f.write_text("aaa\nbbb\nccc\n", encoding="utf-8")

        tool = EditTool()
        result = _run(
            tool.execute(
                file_path=str(f),
                old_string="bbb",
                new_string="BBB",
                purpose="验证参数名自动纠正",
            )
        )
        assert "必填" not in result.summary
        assert "成功" in result.summary
        assert f.read_text(encoding="utf-8") == "aaa\nBBB\nccc\n"

    def test_edit_replace_all_wrong_name(self, tmp_path, no_nest):
        """replace_all 纠正后全量替换生效"""
        no_nest(EditTool)
        f = tmp_path / "a.txt"
        f.write_text("x\nx\nx\n", encoding="utf-8")

        result = _run(
            EditTool().execute(
                file_path=str(f),
                old_string="x",
                new_string="y",
                replace_all=True,
                purpose="验证 replace_all 参数名纠正",
            )
        )
        assert "成功" in result.summary
        assert f.read_text(encoding="utf-8") == "y\ny\ny\n"

    def test_read_with_wrong_name(self, tmp_path, no_nest):
        no_nest(ReadTool)
        f = tmp_path / "a.txt"
        f.write_text("hello\n", encoding="utf-8")

        result = _run(ReadTool().execute(file_path=str(f)))
        assert "hello" in result.summary
        assert "文件未找到" not in result.summary

    def test_write_with_wrong_name(self, tmp_path, no_nest):
        no_nest(WriteTool)
        f = tmp_path / "new.txt"

        result = _run(
            WriteTool().execute(
                file_path=str(f), content="line1\n", purpose="验证写入参数名纠正"
            )
        )
        assert "成功" in result.summary
        assert f.read_text(encoding="utf-8") == "line1\n"

    def test_line_edit_with_wrong_names(self, tmp_path, no_nest):
        no_nest(LineEditTool)
        f = tmp_path / "a.txt"
        f.write_text("l1\nl2\nl3\n", encoding="utf-8")

        result = _run(
            LineEditTool().execute(
                file_path=str(f),
                operation="delete",
                start_line=2,
                end_line=3,
                purpose="验证行编辑参数名纠正",
            )
        )
        assert "成功" in result.summary or "删除" in result.summary
        assert f.read_text(encoding="utf-8") == "l1\n"

    def test_correct_names_regression(self, tmp_path, no_nest):
        """回归：正确 camelCase 参数不受影响"""
        no_nest(EditTool)
        f = tmp_path / "a.txt"
        f.write_text("aaa\n", encoding="utf-8")

        result = _run(
            EditTool().execute(
                filePath=str(f),
                oldString="aaa",
                newString="zzz",
                purpose="回归验证正确参数名",
            )
        )
        assert "成功" in result.summary
        assert f.read_text(encoding="utf-8") == "zzz\n"

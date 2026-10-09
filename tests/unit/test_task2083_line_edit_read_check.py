#!/usr/bin/env python3
"""事务#2083：line_edit read前置检查诊断修复的单元测试

背景：事务#2080中大模型3次line_edit调用均漏传必填参数filePath，
tool_engine._check_line_edit_read_required 未先检测参数缺失，误报
"未检测到对文件  的 read 调用"（文件名为空），误导大模型误判为
read范围/路径格式问题浪费3轮重试。修复后filePath缺失时直接报
"缺少必填参数filePath"，与"未先read"两种根因明确区分。
"""

import json
import sys
from pathlib import Path

# 项目根目录加入 sys.path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from peng.llm.tool_engine import ToolExecutionEngine  # noqa: E402


class _FakeHistory:
    """仅提供 _messages 属性的轻量 history 替身"""

    def __init__(self, messages):
        self._messages = messages


def _make_engine(messages=None):
    """绕过重量级 __init__ 构造 ToolEngine，仅注入 _history"""
    engine = object.__new__(ToolExecutionEngine)
    engine._history = _FakeHistory(messages or [])
    return engine


def _read_call(path, offset=1, limit=2000):
    """构造一条 assistant 消息，含 read 工具调用（arguments 为 JSON 字符串，贴近真实）"""
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "function": {
                    "name": "read",
                    "arguments": json.dumps(
                        {"filePath": path, "offset": offset, "limit": limit}
                    ),
                }
            }
        ],
    }


def _replace_args(path=None, start=131, end=131, **extra):
    """构造 line_edit replace 参数；path=None 时完全不含 filePath 键（#2080事故形态）"""
    args = {"operation": "replace", "startLine": start, "endLine": end}
    if path is not None:
        args["filePath"] = path
    args.update(extra)
    return args


# ==================== 修复点：filePath 缺失的显式报错 ====================


def test_missing_filepath_reports_param_error():
    """#2080事故形态：参数中完全无filePath键 → 必须报参数缺失而非read问题"""
    engine = _make_engine([_read_call(r"D:\downloads\ai\s1.html", 126, 10)])
    err = engine._check_line_edit_read_required(_replace_args())
    assert err is not None
    assert "缺少必填参数 filePath" in err
    # 关键：不得出现误导性的"未检测到对文件  的 read 调用"（空文件名）
    assert "未检测到对文件" not in err


def test_empty_filepath_reports_param_error():
    """filePath 为空字符串 → 报参数缺失"""
    engine = _make_engine([_read_call(r"D:\x\a.html", 1, 100)])
    err = engine._check_line_edit_read_required(_replace_args(path=""))
    assert err is not None and "缺少必填参数 filePath" in err


def test_blank_filepath_reports_param_error():
    """filePath 为纯空白 → 报参数缺失"""
    engine = _make_engine([])
    err = engine._check_line_edit_read_required(_replace_args(path="   "))
    assert err is not None and "缺少必填参数 filePath" in err


def test_missing_filepath_priority_over_empty_history():
    """无任何历史消息且filePath缺失 → 参数缺失报错优先"""
    engine = _make_engine([])
    err = engine._check_line_edit_read_required(_replace_args())
    assert err is not None and "缺少必填参数 filePath" in err


# ==================== 既有行为回归：真实 read 检查 ====================


def test_filepath_without_read_reports_path_in_message():
    """有filePath但未read → 报未检测到read调用，且消息中含实际路径（非空）"""
    engine = _make_engine([_read_call(r"D:\other\b.html", 1, 100)])
    err = engine._check_line_edit_read_required(_replace_args(path=r"D:\a\b.html"))
    assert err is not None
    assert "未检测到对文件" in err and r"D:\a\b.html" in err


def test_read_not_covering_range_rejected():
    """read范围(133-144)未覆盖操作行(131) → 拒绝（#2080第一次调用若有路径本应报此错）"""
    engine = _make_engine([_read_call(r"D:\a\b.html", 133, 12)])
    err = engine._check_line_edit_read_required(_replace_args(path=r"D:\a\b.html"))
    assert err is not None and "行号范围未覆盖" in err and "131" in err


def test_read_covering_range_passes():
    """read范围(126-135)覆盖操作行(131) → 放行"""
    engine = _make_engine([_read_call(r"D:\a\b.html", 126, 10)])
    assert engine._check_line_edit_read_required(_replace_args(path=r"D:\a\b.html")) is None


def test_read_covering_via_multiple_ranges():
    """多条read记录，任一覆盖即放行"""
    engine = _make_engine(
        [
            _read_call(r"D:\a\b.html", 500, 50),
            _read_call(r"D:\a\b.html", 100, 40),  # 100-139 覆盖 131
        ]
    )
    assert engine._check_line_edit_read_required(_replace_args(path=r"D:\a\b.html")) is None


def test_insert_line_coverage():
    """insert操作按line单行判定覆盖"""
    engine = _make_engine([_read_call(r"D:\a\b.html", 10, 5)])  # 10-14
    args = {"filePath": r"D:\a\b.html", "operation": "insert", "line": 12, "content": "x"}
    assert engine._check_line_edit_read_required(args) is None
    args_not_covered = dict(args, line=20)
    err = engine._check_line_edit_read_required(args_not_covered)
    assert err is not None and "行号范围未覆盖" in err


def test_delete_without_read_rejected():
    """delete操作同样要求先read（默认参数形态）"""
    engine = _make_engine([])
    err = engine._check_line_edit_read_required(
        {"filePath": r"D:\a\b.html", "operation": "delete", "startLine": 1, "endLine": 3}
    )
    assert err is not None and "未检测到对文件" in err


def test_path_format_mismatch_is_different_file():
    """正/反斜杠路径视为不同文件 → 拒绝（#2080大模型曾怀疑此因，验证既有行为不变）"""
    engine = _make_engine([_read_call("D:/downloads/ai/s1.html", 128, 8)])
    err = engine._check_line_edit_read_required(_replace_args(path=r"D:\downloads\ai\s1.html"))
    assert err is not None and "未检测到对文件" in err


def test_read_arguments_as_dict_also_parsed():
    """read的arguments为dict形态（部分provider）也能解析"""
    engine = _make_engine(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "read",
                            "arguments": {"filePath": r"D:\a\b.html", "offset": 1, "limit": 200},
                        }
                    }
                ],
            }
        ]
    )
    assert engine._check_line_edit_read_required(_replace_args(path=r"D:\a\b.html")) is None


def test_non_read_tool_calls_ignored():
    """历史中edit/bash等其它工具调用不参与read判定"""
    engine = _make_engine(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "edit",
                            "arguments": json.dumps(
                                {"filePath": r"D:\a\b.html", "oldString": "a", "newString": "b"}
                            ),
                        }
                    }
                ],
            }
        ]
    )
    err = engine._check_line_edit_read_required(_replace_args(path=r"D:\a\b.html"))
    assert err is not None and "未检测到对文件" in err

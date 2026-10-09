#!/usr/bin/env python3
"""事务#2083：远程nest _handle_edit 容错匹配修复的单元测试

背景：事务#2080中3次edit因oldString缩进偏差（1个前导空格）被远程
nest的LocalExecutor._handle_edit拒绝——该路径只有一次精确子串匹配，
而本地EditTool fallback路径有9层容错replacer链（replace_content）。
修复：_handle_edit精确匹配失败后回退replace_content容错链。

测试用例最后两个直接复刻事务#2080事故形态（51行块中1行差1空格）。
"""

import sys
from pathlib import Path

import pytest

# 项目根目录加入 sys.path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from peng.nest.local import LocalExecutor  # noqa: E402


@pytest.fixture
def executor():
    return LocalExecutor()


def _make_file(tmp_path, content: str, crlf: bool = False) -> Path:
    f = tmp_path / "t.html"
    if crlf:
        content = content.replace("\n", "\r\n")
    f.write_bytes(content.encode("utf-8"))
    return f


async def _edit(executor, path, old, new, replace_all=False):
    return await executor._handle_edit(
        {
            "path": str(path),
            "oldString": old,
            "newString": new,
            "replace_all": replace_all,
            "_request_id": "test",
        }
    )


# ==================== 修复点：缩进偏差自动修复 ====================


@pytest.mark.asyncio
async def test_indent_mismatch_auto_fixed(executor, tmp_path):
    """事务#2080核心场景：oldString某行多1个前导空格 → 容错链自动修复成功"""
    content = "function a(){\n  let x=1;\n  let y=2;\n}\n"
    f = _make_file(tmp_path, content)
    old = "function a(){\n   let x=1;\n  let y=2;\n}"  # 第2行3空格（实际2）
    new = "function a(){\n  let x=10;\n  let y=2;\n}"
    resp = await _edit(executor, f, old, new)
    assert resp.error is None, f"缩进偏差应被容错链自动修复: {resp.error}"
    assert f.read_text(encoding="utf-8") == "function a(){\n  let x=10;\n  let y=2;\n}\n"


@pytest.mark.asyncio
async def test_indent_mismatch_crlf_auto_fixed(executor, tmp_path):
    """容错 + CRLF协同：文件CRLF、oldString LF且缩进偏差 → 成功且保持CRLF"""
    content = "a(){\r\n  x=1;\r\n  y=2;\r\n}\r\n"
    f = _make_file(tmp_path, content)
    old = "a(){\n   x=1;\n  y=2;\n}"  # LF + 第2行3空格
    new = "a(){\n  x=9;\n  y=2;\n}"
    resp = await _edit(executor, f, old, new)
    assert resp.error is None
    assert f.read_bytes() == "a(){\r\n  x=9;\r\n  y=2;\r\n}\r\n".encode()


@pytest.mark.asyncio
async def test_exact_match_still_works(executor, tmp_path):
    """精确匹配回归：无偏差时走原路径不受影响"""
    f = _make_file(tmp_path, "aaa\nbbb\nccc\n")
    resp = await _edit(executor, f, "bbb", "BBB")
    assert resp.error is None
    assert f.read_text(encoding="utf-8") == "aaa\nBBB\nccc\n"
    assert resp.data["replacements"] == 1


@pytest.mark.asyncio
async def test_crlf_exact_match_preserved(executor, tmp_path):
    """CRLF精确匹配回归：LF的oldString匹配CRLF文件，写回保持CRLF"""
    f = _make_file(tmp_path, "aaa\r\nbbb\r\nccc\r\n")
    resp = await _edit(executor, f, "bbb", "BBB")
    assert resp.error is None
    assert f.read_bytes() == b"aaa\r\nBBB\r\nccc\r\n"


@pytest.mark.asyncio
async def test_truly_not_found_reports_error(executor, tmp_path):
    """真未找到：内容完全不同 → 报错且含line_edit建议"""
    f = _make_file(tmp_path, "aaa\nbbb\n")
    resp = await _edit(executor, f, "zzz\nqqq", "new")
    assert resp.error is not None
    assert "oldString" in resp.error and "line_edit" in resp.error
    assert f.read_text(encoding="utf-8") == "aaa\nbbb\n"  # 文件未被修改


@pytest.mark.asyncio
async def test_multiple_matches_still_rejected(executor, tmp_path):
    """多匹配回归：精确路径多处匹配且未replaceAll → 拒绝"""
    f = _make_file(tmp_path, "x=1\ny\nx=1\nz\n")
    resp = await _edit(executor, f, "x=1", "x=2")
    assert resp.error is not None and "2 处匹配" in resp.error


@pytest.mark.asyncio
async def test_fallback_no_anchor_rejected(executor, tmp_path):
    """容错路径真拒绝：首尾锚点在文件中不存在 → 全链失败报错且文件不变"""
    content = "if(a){\n  computeTotalSum();\n}\n"
    f = _make_file(tmp_path, content)
    old = "function nonexistentHeader(){\n  x();\n}"  # 首行锚不存在
    resp = await _edit(executor, f, old, "new code")
    assert resp.error is not None and "oldString" in resp.error
    assert f.read_text(encoding="utf-8") == content  # 文件未被修改


@pytest.mark.asyncio
async def test_fallback_anchor_locates_block(executor, tmp_path):
    """文档化block_anchor设计行为：首尾锚唯一确定块时中间行内容差异被
    忽略（单候选阈值0.0）——与本地EditTool fallback路径行为完全一致（opencode设计）"""
    content = "if(a){\n  computeTotalSum();\n}\n"
    f = _make_file(tmp_path, content)
    old = "if(a){\n   completelyDifferentCode();\n}"
    resp = await _edit(executor, f, old, "if(a){\n  new();\n}")
    assert resp.error is None  # 首尾锚唯一 → 设计内匹配
    assert f.read_text(encoding="utf-8") == "if(a){\n  new();\n}\n"


@pytest.mark.asyncio
async def test_replace_all_via_exact(executor, tmp_path):
    """replaceAll回归：精确路径全替换"""
    f = _make_file(tmp_path, "x=1\ny\nx=1\n")
    resp = await _edit(executor, f, "x=1", "x=2", replace_all=True)
    assert resp.error is None
    assert f.read_text(encoding="utf-8") == "x=2\ny\nx=2\n"


# ==================== 事务#2080事故真实场景复刻 ====================


@pytest.mark.asyncio
async def test_task2080_scenario_single_line_of_51(executor, tmp_path):
    """复刻事务#2080最后一次失败：51行块中仅第38行差1个前导空格"""
    lines = [f"// line {i} content" + ("  tail" if i % 7 == 0 else "") for i in range(1, 52)]
    file_block = "\n".join(lines)
    content = f"header\n{file_block}\nfooter\n"
    f = _make_file(tmp_path, content)

    old_lines = list(lines)
    old_lines[37] = " " + old_lines[37]  # 第38行多1个前导空格（#2080形态）
    old = "\n".join(old_lines)
    new = "\n".join("REWRITTEN" if i == 37 else ln for i, ln in enumerate(lines))

    resp = await _edit(executor, f, old, new)
    assert resp.error is None, f"51行块单行空格偏差应自动修复: {resp.error}"
    result = f.read_text(encoding="utf-8")
    assert "REWRITTEN" in result
    assert "header\n" in result and "\nfooter\n" in result
    # 未被改动的其他行保持原样
    assert lines[0] in result and lines[50] in result


@pytest.mark.asyncio
async def test_task2080_scenario_two_line_shadow_comment(executor, tmp_path):
    """复刻事务#2080第二次失败（05:37）：2行oldString整体多1层缩进"""
    content = "function render(){\n // 骑行者影子\n const cxr=R*cx0();\n}\n"
    f = _make_file(tmp_path, content)
    old = "  // 骑行者影子\n  const cxr=R*cx0();"  # 2空格（实际1空格）
    new = "  // 骑行者影子\n  const cxr=W*cx0();"
    resp = await _edit(executor, f, old, new)
    assert resp.error is None, f"整体缩进偏移应被容错链修复: {resp.error}"
    result = f.read_text(encoding="utf-8")
    assert "W*cx0()" in result and "R*cx0()" not in result

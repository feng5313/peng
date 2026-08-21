#!/usr/bin/env python3
"""
ansi_tools.py 功能测试
"""

import json

from ansi_tools import get_char_width, get_screen, get_text, is_wide_char


def test_wide_char_detection():
    """测试双宽度字符检测"""
    print("=" * 60)
    print("测试双宽度字符检测")
    print("=" * 60)

    # 单宽度字符
    assert not is_wide_char("a"), "ASCII字符应为单宽度"
    assert not is_wide_char("="), "= 应为单宽度"
    assert not is_wide_char(" "), "空格应为单宽度"

    # 双宽度字符
    assert is_wide_char("中"), "中文应为双宽度"
    assert is_wide_char("你"), "中文应为双宽度"
    assert is_wide_char("。"), "中文标点应为双宽度"
    assert is_wide_char("，"), "中文标点应为双宽度"
    assert is_wide_char("😄"), "emoji应为双宽度"
    assert is_wide_char("🎉"), "emoji应为双宽度"

    # 字符宽度
    assert get_char_width("a") == 1, "ASCII宽度应为1"
    assert get_char_width("中") == 2, "中文宽度应为2"

    print("✓ 双宽度字符检测测试通过")


def test_get_text_simple():
    """测试get_text基本功能"""
    print("\n" + "=" * 60)
    print("测试 get_text 基本功能")
    print("=" * 60)

    # 简单文本
    raw = "Hello World"
    result = get_text(raw)

    print(f"输入: {repr(raw)}")
    print(f"text: {result['text']}")
    print(f"text_lines: {repr(result['text_lines'])}")
    print(f"positions数量: {len(result['positions'])}")
    print(f"colors数量: {len(result['colors'])}")
    print(f"cursor: {result['cursor']}")

    assert result["text"] == "Hello World", f"预期 'Hello World', 得到 '{result['text']}'"
    assert len(result["positions"]) == 11, f"预期11个位置, 得到 {len(result['positions'])}"
    assert len(result["colors"]) == 11, f"预期11个颜色, 得到 {len(result['colors'])}"
    assert "text_lines" in result, "缺少 text_lines 字段"

    print("✓ get_text 基本测试通过")


def test_get_text_with_ansi():
    """测试get_text处理ANSI序列"""
    print("\n" + "=" * 60)
    print("测试 get_text 处理ANSI序列")
    print("=" * 60)

    # 带颜色的文本
    raw = "\x1b[38;5;196mRed\x1b[0m \x1b[38;5;46mGreen\x1b[0m"
    result = get_text(raw)

    print(f"输入: {repr(raw)}")
    print(f"text: {result['text']}")
    print(f"colors前3个: {result['colors'][:3]}")
    print(f"colors后3个: {result['colors'][-3:]}")

    assert result["text"] == "Red Green", f"预期 'Red Green', 得到 '{result['text']}'"
    # 第一个字符'R'应该是红色 (256色196 = #ff0000)
    assert result["colors"][0][1] == "#ff0000", f"预期红色 #ff0000, 得到 {result['colors'][0][1]}"

    print("✓ get_text ANSI处理测试通过")


def test_get_text_with_newlines():
    """测试get_text处理换行"""
    print("\n" + "=" * 60)
    print("测试 get_text 处理换行")
    print("=" * 60)

    # 带换行的文本
    raw = "Line1\nLine2\nLine3"
    result = get_text(raw)

    print(f"输入: {repr(raw)}")
    print(f"text: {result['text']}")
    print(f"text_lines: {repr(result['text_lines'])}")

    # text应该不含换行符
    assert "\n" not in result["text"], f"text不应包含换行符: {result['text']}"
    assert result["text"] == "Line1Line2Line3", f"预期 'Line1Line2Line3', 得到 '{result['text']}'"

    # text_lines应该包含换行符
    assert "\n" in result["text_lines"], f"text_lines应包含换行符: {result['text_lines']}"
    assert result["text_lines"] == "Line1\nLine2\nLine3", (
        f"预期 'Line1\\nLine2\\nLine3', 得到 {repr(result['text_lines'])}"
    )

    print("✓ get_text 换行处理测试通过")


def test_get_text_colored_lines():
    """测试get_text的colored_lines功能"""
    print("\n" + "=" * 60)
    print("测试 get_text colored_lines 功能")
    print("=" * 60)

    from ansi_tools import get_text

    # 带颜色的文本
    raw = "\x1b[31mRed\x1b[0m \x1b[32mGreen\x1b[0m"
    result = get_text(raw)

    print(f"输入: {repr(raw)}")
    print(f"colored_lines: {repr(result['colored_lines'])}")

    # colored_lines应该包含ANSI颜色序列
    assert "\x1b[31m" in result["colored_lines"], "colored_lines应包含红色ANSI序列"
    assert "\x1b[32m" in result["colored_lines"], "colored_lines应包含绿色ANSI序列"

    # 验证colored_lines的渲染结果包含正确的文本
    assert "Red" in result["colored_lines"], "colored_lines应包含'Green'"
    assert "Green" in result["colored_lines"], "colored_lines应包含'Green'"

    print("✓ get_text colored_lines测试通过")


def test_get_text_colored_lines_multiline():
    """测试get_text的colored_lines多行功能"""
    print("\n" + "=" * 60)
    print("测试 get_text colored_lines 多行功能")
    print("=" * 60)

    from ansi_tools import get_text

    # 带颜色和多行的文本
    raw = "\x1b[31mRed\x1b[0m\n\x1b[32mGreen\x1b[0m"
    result = get_text(raw)

    print(f"输入: {repr(raw)}")
    print(f"colored_lines: {repr(result['colored_lines'])}")

    # colored_lines应该包含换行符
    assert "\n" in result["colored_lines"], "colored_lines应包含换行符"

    # 验证每行都有正确的颜色
    lines = result["colored_lines"].split("\n")
    assert "\x1b[31m" in lines[0], "第一行应包含红色"
    assert "\x1b[32m" in lines[1], "第二行应包含绿色"

    print("✓ get_text colored_lines多行测试通过")


def test_get_text_with_cursor_positioning():
    """测试get_text处理光标定位"""
    print("\n" + "=" * 60)
    print("测试 get_text 处理光标定位")
    print("=" * 60)

    # 使用ANSI定位
    raw = "\x1b[1;1HAB\x1b[2;5HCD"
    result = get_text(raw, width=80, height=24)

    print(f"输入: {repr(raw)}")
    print(f"text: {result['text']}")
    print(f"positions: {result['positions']}")
    print(f"text_lines: {repr(result['text_lines'])}")

    # 检查位置
    # A在(0,0), B在(1,0)
    # C在(4,1), D在(5,1)
    assert result["positions"][0] == [0, 0], f"A应在(0,0), 得到 {result['positions'][0]}"
    assert result["positions"][1] == [1, 0], f"B应在(1,0), 得到 {result['positions'][1]}"
    assert result["positions"][2] == [4, 1], f"C应在(4,1), 得到 {result['positions'][2]}"
    assert result["positions"][3] == [5, 1], f"D应在(5,1), 得到 {result['positions'][3]}"

    print("✓ get_text 光标定位测试通过")


def test_get_text_with_chinese():
    """测试get_text处理中文（双宽度字符）"""
    print("\n" + "=" * 60)
    print("测试 get_text 处理中文")
    print("=" * 60)

    raw = "你好World"
    result = get_text(raw)

    print(f"输入: {repr(raw)}")
    print(f"text: {result['text']}")
    print(f"positions: {result['positions']}")

    # 中文'你'在位置(0,0)，'好'在位置(2,0)（因为中文占2列）
    assert result["positions"][0] == [0, 0], "'你'应在(0,0)"
    assert result["positions"][1] == [2, 0], "'好'应在(2,0)"
    assert result["positions"][2] == [4, 0], "'W'应在(4,0)"

    print("✓ get_text 中文处理测试通过")


def test_get_screen_simple():
    """测试get_screen基本功能"""
    print("\n" + "=" * 60)
    print("测试 get_screen 基本功能")
    print("=" * 60)

    raw = "Hello"
    result = get_screen(raw, width=20, height=5)

    print(f"输入: {repr(raw)}")
    print(f"chars[0]: {result['chars'][0][:10]}")
    print(f"cursor: {result['cursor']}")

    # 检查字符矩阵
    assert result["chars"][0][0] == "H", f"chars[0][0]应为'H', 得到 '{result['chars'][0][0]}'"
    assert result["chars"][0][1] == "e", f"chars[0][1]应为'e', 得到 '{result['chars'][0][1]}'"
    assert result["chars"][0][2] == "l", f"chars[0][2]应为'l', 得到 '{result['chars'][0][2]}'"

    print("✓ get_screen 基本测试通过")


def test_get_screen_with_chinese():
    """测试get_screen处理中文（双宽度字符）"""
    print("\n" + "=" * 60)
    print("测试 get_screen 处理中文")
    print("=" * 60)

    raw = "中"
    result = get_screen(raw, width=20, height=5)

    print(f"输入: {repr(raw)}")
    print(f"chars[0][0]: {repr(result['chars'][0][0])}")
    print(f"chars[0][1]: {repr(result['chars'][0][1])}")

    # 中文字符应标记为'l'和'r'
    assert result["chars"][0][0] == "中l", f"chars[0][0]应为'中l', 得到 '{result['chars'][0][0]}'"
    assert result["chars"][0][1] == "中r", f"chars[0][1]应为'中r', 得到 '{result['chars'][0][1]}'"

    print("✓ get_screen 中文处理测试通过")


def test_get_screen_with_colors():
    """测试get_screen处理颜色"""
    print("\n" + "=" * 60)
    print("测试 get_screen 处理颜色")
    print("=" * 60)

    raw = "\x1b[38;5;196mR\x1b[0m"
    result = get_screen(raw, width=20, height=5)

    print(f"输入: {repr(raw)}")
    print(f"chars[0][0]: {repr(result['chars'][0][0])}")
    print(f"colors[0][0]: {result['colors'][0][0]}")

    # 检查颜色 (256色196 = #ff0000)
    assert result["colors"][0][0][1] == "#ff0000", (
        f"前景色应为红色 #ff0000, 得到 {result['colors'][0][0][1]}"
    )

    print("✓ get_screen 颜色处理测试通过")


def test_get_screen_with_cursor_positioning():
    """测试get_screen处理光标定位"""
    print("\n" + "=" * 60)
    print("测试 get_screen 处理光标定位")
    print("=" * 60)

    raw = "\x1b[2;5HX"
    result = get_screen(raw, width=20, height=5)

    print(f"输入: {repr(raw)}")
    print(f"chars[1][4]: {repr(result['chars'][1][4])}")

    # X应该在行2列5（0-indexed: 行1列4）
    assert result["chars"][1][4] == "X", f"chars[1][4]应为'X', 得到 '{result['chars'][1][4]}'"

    print("✓ get_screen 光标定位测试通过")


def test_json_output():
    """测试JSON输出"""
    print("\n" + "=" * 60)
    print("测试 JSON 输出")
    print("=" * 60)

    raw = "\x1b[38;5;46mGreen\x1b[0m"
    result = get_text(raw)

    # 尝试JSON序列化
    json_str = json.dumps(result, ensure_ascii=False, indent=2)
    print(f"JSON输出:\n{json_str}")

    # 验证可以正确序列化和反序列化
    parsed = json.loads(json_str)
    assert parsed["text"] == "Green", "反序列化后text应为'Green'"

    print("✓ JSON输出测试通过")


def run_all_tests():
    """运行所有测试"""
    print("\n" + "=" * 60)
    print("运行所有测试")
    print("=" * 60)

    tests = [
        test_wide_char_detection,
        test_get_text_simple,
        test_get_text_with_ansi,
        test_get_text_with_newlines,
        test_get_text_colored_lines,
        test_get_text_colored_lines_multiline,
        test_get_text_with_cursor_positioning,
        test_get_text_with_chinese,
        test_get_text_with_emoji,
        test_get_text_preserves_empty_lines,
        test_get_text_with_clear_screen,
        test_get_text_with_background_color,
        test_get_screen_simple,
        test_get_screen_with_chinese,
        test_get_screen_with_colors,
        test_get_screen_with_cursor_positioning,
        test_get_screen_with_tab,
        test_get_screen_dimensions,
        test_get_screen_text,
        test_get_screen_text_with_chinese,
        test_get_screen_text_with_spaces,
        test_real_world_sudoku,
        test_json_output,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            import traceback

            print(f"\n✗ {test.__name__} 失败: {e}\n{traceback.format_exc()}")
            failed += 1
        except Exception as e:
            import traceback

            print(f"\n✗ {test.__name__} 出错: {e}\n{traceback.format_exc()}")
            failed += 1

    print("\n" + "=" * 60)
    print(f"测试结果: {passed} 通过, {failed} 失败")
    print("=" * 60)

    return failed == 0


def test_get_text_with_emoji():
    """测试get_text处理emoji"""
    print("\n" + "=" * 60)
    print("测试 get_text 处理emoji")
    print("=" * 60)

    raw = "Hello 😄 World 🎉"
    result = get_text(raw)

    print(f"输入: {repr(raw)}")
    print(f"text: {result['text']}")
    print(f"positions: {result['positions']}")

    # emoji是双宽度字符，位置应该正确
    # H(0,0) e(1,0) l(2,0) l(3,0) o(4,0) 空格(5,0) 😄(6,0) 空格(8,0) W(9,0)...
    assert result["positions"][0] == [0, 0], "'H'应在(0,0)"
    assert result["positions"][6][0] == 6, "emoji应在位置6"

    print("✓ get_text emoji处理测试通过")


def test_get_text_preserves_empty_lines():
    """测试get_text保留中间空行"""
    print("\n" + "=" * 60)
    print("测试 get_text 保留中间空行")
    print("=" * 60)

    raw = "Line1\n\nLine3"
    result = get_text(raw)

    print(f"输入: {repr(raw)}")
    print(f"text_lines: {repr(result['text_lines'])}")

    # 中间的空行应该保留
    assert result["text_lines"] == "Line1\n\nLine3", (
        f"预期 'Line1\\n\\nLine3', 得到 {repr(result['text_lines'])}"
    )

    print("✓ get_text 保留空行测试通过")


def test_get_text_with_clear_screen():
    """测试get_text处理清屏命令"""
    print("\n" + "=" * 60)
    print("测试 get_text 处理清屏命令")
    print("=" * 60)

    # ESC[2J 清屏
    raw = "AB\x1b[2JCD"
    result = get_text(raw)

    print(f"输入: {repr(raw)}")
    print(f"text: {result['text']}")

    # 清屏命令不删除已经记录的文本
    assert "A" in result["text"], "A应该在text中"
    assert "B" in result["text"], "B应该在text中"
    assert "C" in result["text"], "C应该在text中"

    print("✓ get_text 清屏处理测试通过")


def test_get_screen_with_tab():
    """测试get_screen处理制表符"""
    print("\n" + "=" * 60)
    print("测试 get_screen 处理制表符")
    print("=" * 60)

    raw = "A\tB"
    result = get_screen(raw, width=20, height=5)

    print(f"输入: {repr(raw)}")
    print(f"chars[0][:10]: {result['chars'][0][:10]}")

    # 制表符应该转为空格（停在8的倍数位置）
    assert result["chars"][0][0] == "A", "chars[0][0]应为'A'"
    # 位置1-7应该是空格
    for i in range(1, 8):
        assert result["chars"][0][i] == " ", f"chars[0][{i}]应为空格"
    assert result["chars"][0][8] == "B", "chars[0][8]应为'B'"

    print("✓ get_screen 制表符处理测试通过")


def test_get_text_with_background_color():
    """测试get_text处理背景色"""
    print("\n" + "=" * 60)
    print("测试 get_text 处理背景色")
    print("=" * 60)

    # 256色背景: ESC[48;5;Nm
    raw = "\x1b[48;5;21mA\x1b[0m"
    result = get_text(raw)

    print(f"输入: {repr(raw)}")
    print(f"colors[0]: {result['colors'][0]}")

    # 背景21应该是蓝色 #0000ff
    assert result["colors"][0][0] == "#0000ff", f"背景色应为 #0000ff, 得到 {result['colors'][0][0]}"

    print("✓ get_text 背景色处理测试通过")


def test_get_screen_dimensions():
    """测试get_screen维度限制"""
    print("\n" + "=" * 60)
    print("测试 get_screen 维度限制")
    print("=" * 60)

    raw = "Hello World"
    result = get_screen(raw, width=5, height=2)

    print(f"输入: {repr(raw)}")
    print(f"chars长度: {len(result['chars'])}")
    print(f"chars[0]长度: {len(result['chars'][0])}")

    assert len(result["chars"]) == 2, "高度应为2"
    assert len(result["chars"][0]) == 5, "宽度应为5"

    print("✓ get_screen 维度限制测试通过")


def test_real_world_sudoku():
    """测试真实sudoku输出"""
    print("\n" + "=" * 60)
    print("测试真实sudoku输出")
    print("=" * 60)

    # 模拟sudoku的ANSI输出
    raw = "\x1b[?25l\x1b[2J\x1b[H" + "   A B C\n" + "  ┌─┬─┬─┐\n" + "1 │5│3│.│\n"

    result_text = get_text(raw, width=80, height=24)
    get_screen(raw, width=80, height=24)

    print(f"text: {repr(result_text['text'][:50])}")
    print(f"text_lines前3行: {result_text['text_lines'].split(chr(10))[:3]}")

    # 验证文本提取正确
    assert "A" in result_text["text"], "A应该在text中"
    assert "5" in result_text["text"], "5应该在text中"

    print("✓ 真实sudoku输出测试通过")


def test_get_screen_text():
    """测试get_screen_text功能"""
    print("\n" + "=" * 60)
    print("测试 get_screen_text 功能")
    print("=" * 60)

    from ansi_tools import get_screen_text

    # 简单测试: "Hello World Test"
    # H(0) e(1) l(2) l(3) o(4) 空格(5) W(6) o(7) r(8) l(9) d(10) 空格(11) T(12) e(13) s(14) t(15)

    # 测试Hello
    result = get_screen_text("Hello World Test", 2, 0)  # 位置2是'l'
    print(f"位置(2,0)的文本: {repr(result)}")
    assert result == "Hello", f"预期 'Hello', 得到 '{result}'"

    # 测试World
    result = get_screen_text("Hello World Test", 8, 0)  # 位置8是'r'
    print(f"位置(8,0)的文本: {repr(result)}")
    assert result == "World", f"预期 'World', 得到 '{result}'"

    # 测试Test
    result = get_screen_text("Hello World Test", 14, 0)  # 位置14是's'
    print(f"位置(14,0)的文本: {repr(result)}")
    assert result == "Test", f"预期 'Test', 得到 '{result}'"

    print("✓ get_screen_text 基本测试通过")


def test_get_screen_text_with_chinese():
    """测试get_screen_text处理中文"""
    print("\n" + "=" * 60)
    print("测试 get_screen_text 处理中文")
    print("=" * 60)

    from ansi_tools import get_screen_text

    # 添加空格分隔
    raw = "你好 World 测试"
    # 你(0-1) 好(2-3) 空格(4) W(5) o(6) r(7) l(8) d(9) 空格(10) 测(11-12) 试(13-14)

    # 测试英文部分
    result = get_screen_text(raw, 7, 0)  # 'r'
    print(f"位置(7,0)的文本: {repr(result)}")
    assert result == "World", f"预期 'World', 得到 '{result}'"

    # 测试中文部分
    result = get_screen_text(raw, 0, 0)  # '你l'
    print(f"位置(0,0)的文本: {repr(result)}")
    assert result == "你好", f"预期 '你好', 得到 '{result}'"

    # 测试另一个中文部分
    result = get_screen_text(raw, 12, 0)  # '测r'
    print(f"位置(12,0)的文本: {repr(result)}")
    # 如果位置指向双宽度字符的右半部分，跳过它

    print("✓ get_screen_text 中文测试通过")


def test_get_screen_text_with_spaces():
    """测试get_screen_text遇到空格停止"""
    print("\n" + "=" * 60)
    print("测试 get_screen_text 遇到空格停止")
    print("=" * 60)

    from ansi_tools import get_screen_text

    raw = "  ABC  DEF  "

    result = get_screen_text(raw, 3, 0)  # 'B'
    print(f"位置(3,0)的文本: {repr(result)}")
    assert result == "ABC", f"预期 'ABC', 得到 '{result}'"

    result = get_screen_text(raw, 7, 0)  # 'D'
    print(f"位置(7,0)的文本: {repr(result)}")
    assert result == "DEF", f"预期 'DEF', 得到 '{result}'"

    # 空格位置返回空
    result = get_screen_text(raw, 0, 0)
    print(f"位置(0,0)的文本: {repr(result)}")
    assert result == "", f"预期 '', 得到 '{result}'"

    print("✓ get_screen_text 空格测试通过")


if __name__ == "__main__":
    success = run_all_tests()
    exit(0 if success else 1)

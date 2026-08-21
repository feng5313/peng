#!/usr/bin/env python3
"""
ANSI工具集 - 统一的ANSI文本处理工具（v3.0）

核心功能：
1. get_text       - 获取文本及其位置和颜色信息
2. get_screen     - 获取屏幕字符矩阵及颜色信息
3. get_screen_text- 获取指定位置的连续文本
4. pure_text      - 移除所有ANSI序列，返回纯文本 (命令: plain)
5. text_lines     - 基于光标定位渲染屏幕 (命令: lines)
6. colored_lines  - 保留颜色序列 (命令: colored)
7. frame          - 提取动画的最后一帧
8. colors         - 分析颜色使用 (命令: colors)

版本：3.0
更新：
- 新增get_text函数：返回文本、位置、颜色、光标状态
- 新增get_screen函数：返回屏幕字符矩阵、颜色矩阵、光标状态
- 支持256色到#rgb格式转换
- 支持双宽度字符（中文、emoji）处理
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any


# 256色到RGB对照表
def _build_256_color_table() -> dict[int, str]:
    """构建256色到#rgb的映射表"""
    table = {}

    # 0-15: 标准颜色
    standard_colors = [
        "#000000",
        "#800000",
        "#008000",
        "#808000",
        "#000080",
        "#800080",
        "#008080",
        "#c0c0c0",
        "#808080",
        "#ff0000",
        "#00ff00",
        "#ffff00",
        "#0000ff",
        "#ff00ff",
        "#00ffff",
        "#ffffff",
    ]
    for i, color in enumerate(standard_colors):
        table[i] = color

    # 16-231: 6x6x6颜色立方体
    for i in range(16, 232):
        n = i - 16
        r = (n // 36) * 51
        g = ((n % 36) // 6) * 51
        b = (n % 6) * 51
        table[i] = f"#{r:02x}{g:02x}{b:02x}"

    # 232-255: 灰度渐变
    for i in range(232, 256):
        gray = 8 + (i - 232) * 10
        table[i] = f"#{gray:02x}{gray:02x}{gray:02x}"

    return table


COLOR_256_TO_RGB = _build_256_color_table()

# 基本颜色映射 (30-37前景, 40-47背景, 90-97亮前景, 100-107亮背景)
BASIC_COLORS = {
    30: "#000000",
    31: "#800000",
    32: "#008000",
    33: "#808000",
    34: "#000080",
    35: "#800080",
    36: "#008080",
    37: "#c0c0c0",
    40: "#000000",
    41: "#800000",
    42: "#008000",
    43: "#808000",
    44: "#000080",
    45: "#800080",
    46: "#008080",
    47: "#c0c0c0",
    90: "#808080",
    91: "#ff0000",
    92: "#00ff00",
    93: "#ffff00",
    94: "#0000ff",
    95: "#ff00ff",
    96: "#00ffff",
    97: "#ffffff",
    100: "#808080",
    101: "#ff0000",
    102: "#00ff00",
    103: "#ffff00",
    104: "#0000ff",
    105: "#ff00ff",
    106: "#00ffff",
    107: "#ffffff",
}


def is_wide_char(char: str) -> bool:
    """
    判断字符是否为双宽度显示字符

    包括：中文、中文标点、emoji、特殊符号等
    """
    if len(char) == 0:
        return False

    codepoint = ord(char[0])

    # CJK统一汉字范围
    if 0x4E00 <= codepoint <= 0x9FFF:
        return True
    # CJK扩展A
    if 0x3400 <= codepoint <= 0x4DBF:
        return True
    # CJK扩展B-F等
    if 0x20000 <= codepoint <= 0x2CEAF:
        return True
    # CJK兼容汉字
    if 0xF900 <= codepoint <= 0xFAFF:
        return True
    # CJK标点符号
    if 0x3000 <= codepoint <= 0x303F:
        return True
    # 全角ASCII、全角标点
    if 0xFF00 <= codepoint <= 0xFFEF:
        return True
    # Emoji范围
    if 0x1F600 <= codepoint <= 0x1F64F:
        return True
    if 0x1F300 <= codepoint <= 0x1F5FF:
        return True
    if 0x1F680 <= codepoint <= 0x1F6FF:
        return True
    if 0x1F1E0 <= codepoint <= 0x1F1FF:
        return True
    if 0x2600 <= codepoint <= 0x26FF:
        return True
    if 0x2700 <= codepoint <= 0x27BF:
        return True
    # 其他特殊符号
    if 0x2500 <= codepoint <= 0x257F:
        return True  # 制表符
    if 0x2580 <= codepoint <= 0x259F:
        return True  # 方块元素
    if 0x25A0 <= codepoint <= 0x25FF:
        return True  # 几何图形

    # 处理代理对（emoji等）
    if len(char) > 1 and 0xD800 <= codepoint <= 0xDBFF:
        return True

    return False


def get_char_width(char: str) -> int:
    """获取字符显示宽度"""
    if is_wide_char(char):
        return 2
    return 1


@dataclass
class AnsiStyle:
    """ANSI样式状态"""

    fg_color: int | None = None  # 256色前景
    fg_rgb: tuple[int, int, int] | None = None
    fg_basic: int | None = None
    bg_color: int | None = None  # 256色背景
    bg_rgb: tuple[int, int, int] | None = None
    bg_basic: int | None = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    blink: bool = False
    reverse: bool = False
    hidden: bool = False
    strikethrough: bool = False

    def reset(self):
        """重置所有样式"""
        self.fg_color = None
        self.fg_rgb = None
        self.fg_basic = None
        self.bg_color = None
        self.bg_rgb = None
        self.bg_basic = None
        self.bold = False
        self.dim = False
        self.italic = False
        self.underline = False
        self.blink = False
        self.reverse = False
        self.hidden = False
        self.strikethrough = False

    def get_fg_hex(self) -> str:
        """获取前景色的十六进制表示"""
        if self.reverse:
            return self._get_bg_hex_internal()
        return self._get_fg_hex_internal()

    def get_bg_hex(self) -> str:
        """获取背景色的十六进制表示"""
        if self.reverse:
            return self._get_fg_hex_internal()
        return self._get_bg_hex_internal()

    def _get_fg_hex_internal(self) -> str:
        """内部：获取前景色"""
        if self.fg_color is not None:
            return COLOR_256_TO_RGB.get(self.fg_color, "#ffffff")
        if self.fg_rgb is not None:
            return f"#{self.fg_rgb[0]:02x}{self.fg_rgb[1]:02x}{self.fg_rgb[2]:02x}"
        if self.fg_basic is not None:
            return BASIC_COLORS.get(self.fg_basic, "#ffffff")
        return ""  # 默认颜色

    def _get_bg_hex_internal(self) -> str:
        """内部：获取背景色"""
        if self.bg_color is not None:
            return COLOR_256_TO_RGB.get(self.bg_color, "#000000")
        if self.bg_rgb is not None:
            return f"#{self.bg_rgb[0]:02x}{self.bg_rgb[1]:02x}{self.bg_rgb[2]:02x}"
        if self.bg_basic is not None:
            return BASIC_COLORS.get(self.bg_basic, "#000000")
        return ""  # 默认颜色

    def copy(self) -> "AnsiStyle":
        """创建副本"""
        return AnsiStyle(
            fg_color=self.fg_color,
            fg_rgb=self.fg_rgb,
            fg_basic=self.fg_basic,
            bg_color=self.bg_color,
            bg_rgb=self.bg_rgb,
            bg_basic=self.bg_basic,
            bold=self.bold,
            dim=self.dim,
            italic=self.italic,
            underline=self.underline,
            blink=self.blink,
            reverse=self.reverse,
            hidden=self.hidden,
            strikethrough=self.strikethrough,
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "fg": self.get_fg_hex(),
            "bg": self.get_bg_hex(),
            "bold": self.bold,
            "italic": self.italic,
            "underline": self.underline,
        }

    def to_ansi_code(self) -> str:
        """转换为ANSI颜色序列"""
        codes = []

        # 样式
        if self.bold:
            codes.append("1")
        if self.dim:
            codes.append("2")
        if self.italic:
            codes.append("3")
        if self.underline:
            codes.append("4")
        if self.blink:
            codes.append("5")
        if self.reverse:
            codes.append("7")
        if self.hidden:
            codes.append("8")
        if self.strikethrough:
            codes.append("9")

        # 前景色
        if self.fg_basic is not None:
            codes.append(str(self.fg_basic))
        elif self.fg_color is not None:
            codes.append(f"38;5;{self.fg_color}")
        elif self.fg_rgb is not None:
            codes.append(f"38;2;{self.fg_rgb[0]};{self.fg_rgb[1]};{self.fg_rgb[2]}")

        # 背景色
        if self.bg_basic is not None:
            codes.append(str(self.bg_basic))
        elif self.bg_color is not None:
            codes.append(f"48;5;{self.bg_color}")
        elif self.bg_rgb is not None:
            codes.append(f"48;2;{self.bg_rgb[0]};{self.bg_rgb[1]};{self.bg_rgb[2]}")

        if codes:
            return f"\x1b[{';'.join(codes)}m"
        return ""


@dataclass
class TerminalState:
    """终端状态"""

    row: int = 0
    col: int = 0
    style: AnsiStyle = field(default_factory=AnsiStyle)
    saved_row: int = 0
    saved_col: int = 0
    saved_style: AnsiStyle = field(default_factory=AnsiStyle)

    def save_cursor(self):
        self.saved_row = self.row
        self.saved_col = self.col
        self.saved_style = self.style.copy()

    def restore_cursor(self):
        self.row = self.saved_row
        self.col = self.saved_col
        self.style = self.saved_style

    def reset(self):
        self.row = 0
        self.col = 0
        self.style.reset()
        self.saved_row = 0
        self.saved_col = 0
        self.saved_style.reset()

    def to_cursor_dict(self) -> dict[str, Any]:
        """返回光标位置和样式"""
        return {
            "position": [self.col, self.row],
            "style": self.style.to_dict(),
        }


def find_string_terminator(text: str, start: int) -> int:
    """查找字符串结束符"""
    i = start
    while i < len(text):
        if text[i] == "\x07":
            return i
        elif text[i] == "\x1b" and i + 1 < len(text) and text[i + 1] == "\\":
            return i + 1
        i += 1
    return -1


def parse_sgr_params(params: list, style: AnsiStyle) -> AnsiStyle:
    """解析SGR参数"""
    i = 0
    while i < len(params):
        p = params[i]

        if p is None or p == 0:
            style.reset()
        elif p == 1:
            style.bold = True
        elif p == 2:
            style.dim = True
        elif p == 3:
            style.italic = True
        elif p == 4:
            style.underline = True
        elif p == 5 or p == 6:
            style.blink = True
        elif p == 7:
            style.reverse = True
        elif p == 8:
            style.hidden = True
        elif p == 9:
            style.strikethrough = True
        elif p == 22:
            style.bold = False
            style.dim = False
        elif p == 23:
            style.italic = False
        elif p == 24:
            style.underline = False
        elif p == 25:
            style.blink = False
        elif p == 27:
            style.reverse = False
        elif p == 28:
            style.hidden = False
        elif p == 29:
            style.strikethrough = False
        elif 30 <= p <= 37:
            style.fg_basic = p
            style.fg_color = None
            style.fg_rgb = None
        elif 90 <= p <= 97:
            style.fg_basic = p
            style.fg_color = None
            style.fg_rgb = None
        elif p == 38:
            if i + 2 < len(params) and params[i + 1] == 5:
                style.fg_color = params[i + 2]
                style.fg_basic = None
                style.fg_rgb = None
                i += 2
            elif i + 4 < len(params) and params[i + 1] == 2:
                style.fg_rgb = (params[i + 2], params[i + 3], params[i + 4])
                style.fg_basic = None
                style.fg_color = None
                i += 4
        elif 40 <= p <= 47:
            style.bg_basic = p
            style.bg_color = None
            style.bg_rgb = None
        elif 100 <= p <= 107:
            style.bg_basic = p
            style.bg_color = None
            style.bg_rgb = None
        elif p == 48:
            if i + 2 < len(params) and params[i + 1] == 5:
                style.bg_color = params[i + 2]
                style.bg_basic = None
                style.bg_rgb = None
                i += 2
            elif i + 4 < len(params) and params[i + 1] == 2:
                style.bg_rgb = (params[i + 2], params[i + 3], params[i + 4])
                style.bg_basic = None
                style.bg_color = None
                i += 4
        elif p == 39:
            style.fg_basic = None
            style.fg_color = None
            style.fg_rgb = None
        elif p == 49:
            style.bg_basic = None
            style.bg_color = None
            style.bg_rgb = None

        i += 1

    return style


def parse_csi_params(seq: str) -> list:
    """解析CSI参数"""
    if not seq:
        return []

    if seq.startswith("?"):
        seq = seq[1:]

    parts = seq.split(";")
    params = []

    for part in parts:
        part = part.strip()
        if part == "":
            params.append(None)
        else:
            try:
                params.append(int(part))
            except ValueError:
                params.append(None)

    return params


def _parse_ansi_to_screen(
    raw_ansi_text: str, width: int, height: int
) -> tuple[TerminalState, list[list], list]:
    """
    内部函数：解析ANSI文本到屏幕矩阵

    Returns:
        (state, screen_matrix, all_chars) - 终端状态、屏幕矩阵、所有字符列表
        screen_matrix[row][col] = (char, style) 或 None
        all_chars = [(char, col, row, style), ...] - 所有遇到的字符（不受清屏影响）
    """
    screen_matrix = [[None for _ in range(width)] for _ in range(height)]
    all_chars = []  # 记录所有字符，不受清屏影响
    state = TerminalState()
    last_char = " "  # 用于 REP 控制码：记录上一个打印的字符

    i = 0
    n = len(raw_ansi_text)

    while i < n:
        ch = raw_ansi_text[i]

        # 处理ANSI转义序列
        if ch == "\x1b" and i + 1 < n:
            next_ch = raw_ansi_text[i + 1]

            if next_ch == "[":
                # CSI序列
                j = i + 2
                while j < n and not (0x40 <= ord(raw_ansi_text[j]) <= 0x7E):
                    j += 1

                if j >= n:
                    i += 1
                    continue

                seq = raw_ansi_text[i + 2 : j]
                cmd = raw_ansi_text[j]
                params = parse_csi_params(seq)

                # 光标移动命令
                if cmd in ("H", "f"):
                    row_val = params[0] if params and params[0] is not None else 1
                    col_val = params[1] if len(params) > 1 and params[1] is not None else 1
                    state.row = min(max(row_val - 1, 0), height - 1)
                    state.col = min(max(col_val - 1, 0), width - 1)
                elif cmd == "A":
                    mv = params[0] if params and params[0] is not None else 1
                    state.row = max(state.row - mv, 0)
                elif cmd == "B":
                    mv = params[0] if params and params[0] is not None else 1
                    state.row = min(state.row + mv, height - 1)
                elif cmd == "C":
                    mv = params[0] if params and params[0] is not None else 1
                    state.col = min(state.col + mv, width - 1)
                elif cmd == "D":
                    mv = params[0] if params and params[0] is not None else 1
                    state.col = max(state.col - mv, 0)
                elif cmd == "G":
                    mv = params[0] if params and params[0] is not None else 1
                    state.col = min(max(mv - 1, 0), width - 1)
                elif cmd == "d":
                    mv = params[0] if params and params[0] is not None else 1
                    state.row = min(max(mv - 1, 0), height - 1)
                elif cmd == "s":
                    state.save_cursor()
                elif cmd == "u":
                    state.restore_cursor()
                elif cmd == "m":
                    state.style = parse_sgr_params(params, state.style)
                elif cmd == "J":
                    if (params[0] if params else 0) in (2, 3):
                        screen_matrix = [[None for _ in range(width)] for _ in range(height)]
                elif cmd == "K":
                    erase_mode = params[0] if params else 0
                    if erase_mode == 0:
                        for c in range(state.col, width):
                            screen_matrix[state.row][c] = None
                    elif erase_mode == 2:
                        for c in range(width):
                            screen_matrix[state.row][c] = None
                elif cmd == "b":
                    # REP 控制码：重复上一个字符
                    count = params[0] if params and params[0] is not None else 1
                    for _ in range(count):
                        cur_col = state.col
                        cur_row = state.row
                        char_width = get_char_width(last_char)
                        if 0 <= cur_row < height and 0 <= cur_col < width:
                            all_chars.append((last_char, cur_col, cur_row, state.style.copy()))
                            screen_matrix[cur_row][cur_col] = (last_char, state.style.copy())
                            if char_width == 2 and cur_col + 1 < width:
                                screen_matrix[cur_row][cur_col + 1] = (
                                    "__WIDE__",
                                    state.style.copy(),
                                )
                        state.col += char_width
                        if state.col > width:
                            state.col = width

                i = j + 1
                continue

            elif next_ch == "7":
                state.save_cursor()
                i += 2
                continue
            elif next_ch == "8":
                state.restore_cursor()
                i += 2
                continue
            elif next_ch == "c":
                state.reset()
                screen_matrix = [[None for _ in range(width)] for _ in range(height)]
                i += 2
                continue
            elif next_ch == "M":
                state.row = max(state.row - 1, 0)
                i += 2
                continue
            elif next_ch == "D":
                state.row = min(state.row + 1, height - 1)
                i += 2
                continue
            elif next_ch == "E":
                state.row = min(state.row + 1, height - 1)
                state.col = 0
                i += 2
                continue
            elif next_ch in "]P_X^":
                end = find_string_terminator(raw_ansi_text, i + 2)
                i = (end + 1) if end != -1 else i + 2
                continue
            elif next_ch in "()":
                i += 3
                continue
            else:
                i += 2
                continue

        # 处理控制字符
        if ch == "\r":
            state.col = 0
        elif ch == "\n":
            state.row = min(state.row + 1, height - 1)
            state.col = 0
        elif ch == "\t":
            # 制表符用空格填充到屏幕矩阵
            next_col = min(((state.col // 8) + 1) * 8, width)
            while state.col < next_col:
                if 0 <= state.row < height:
                    screen_matrix[state.row][state.col] = (" ", state.style.copy())
                state.col += 1
        elif ch == "\b":
            state.col = max(state.col - 1, 0)
        elif ch.isprintable() or ord(ch) >= 32:
            char_width = get_char_width(ch)
            cur_col = state.col
            cur_row = state.row
            last_char = ch  # 更新最后一个字符，供 REP 控制码使用

            if 0 <= cur_row < height and 0 <= cur_col < width:
                # 记录到字符列表（不受清屏影响）
                all_chars.append((ch, cur_col, cur_row, state.style.copy()))

                # 记录到屏幕矩阵
                screen_matrix[cur_row][cur_col] = (ch, state.style.copy())

                # 双宽度字符标记第二个位置
                if char_width == 2 and cur_col + 1 < width:
                    screen_matrix[cur_row][cur_col + 1] = ("__WIDE__", state.style.copy())

            state.col += char_width
            if state.col > width:
                state.col = width

        i += 1

    return state, screen_matrix, all_chars


def get_text(raw_ansi_text: str, width: int = 80, height: int = 24) -> dict[str, Any]:
    """
    获取文本及其位置和颜色信息

    Args:
        raw_ansi_text: 带ANSI转义符的原始文本
        width: 屏幕宽度
        height: 屏幕高度

    Returns:
        {
            'text': '转换后的纯文本（无换行）',
            'positions': [[x,y], [x,y], ...],  # 每个字符的屏幕位置
            'colors': [['#bg', '#fg'], ...],   # 每个字符的颜色
            'text_lines': '转换后的纯文本（包含换行符，去除行尾空白）',
            'colored_lines': '只保留ANSI颜色转义符的文本（包含换行符，去除行尾空白）',
            'color_set': {'foreground': [...], 'background': [...]},  # 去重后的颜色集合
            'cursor': {'position': [x, y], 'style': {...}}
        }
    """
    state, screen_matrix, all_chars = _parse_ansi_to_screen(raw_ansi_text, width, height)

    # 从all_chars构建text、positions、colors（不受清屏影响）
    text_chars = []
    positions = []
    colors = []
    fg_color_set = set()
    bg_color_set = set()
    for char, col, row, style in all_chars:
        text_chars.append(char)
        positions.append([col, row])
        bg_hex = style.get_bg_hex()
        fg_hex = style.get_fg_hex()
        colors.append([bg_hex, fg_hex])
        if fg_hex:
            fg_color_set.add(fg_hex)
        if bg_hex:
            bg_color_set.add(bg_hex)

    # 构建text_lines和colored_lines（从屏幕矩阵）
    text_lines_list = []
    colored_lines_list = []

    for row in screen_matrix:
        text_parts = []
        colored_parts = []
        prev_style = None

        for item in row:
            if item is not None:
                char, style = item
                if char == "__WIDE__":
                    continue

                text_parts.append(char)
                ansi_code = style.to_ansi_code()
                if prev_style is None or ansi_code != prev_style.to_ansi_code():
                    colored_parts.append(ansi_code)
                colored_parts.append(char)
                prev_style = style
            else:
                text_parts.append(" ")
                if prev_style is not None and prev_style.to_ansi_code() != "":
                    colored_parts.append("\x1b[0m")
                    prev_style = None
                colored_parts.append(" ")

        if prev_style is not None and prev_style.to_ansi_code() != "":
            colored_parts.append("\x1b[0m")

        text_line = "".join(text_parts).rstrip()
        colored_line = "".join(colored_parts).rstrip()
        if colored_line and not colored_line.endswith("\x1b[0m"):
            colored_line += "\x1b[0m"

        text_lines_list.append(text_line)
        colored_lines_list.append(colored_line)

    while len(text_lines_list) > 1 and text_lines_list[-1] == "":
        text_lines_list.pop()
        colored_lines_list.pop()

    return {
        "text": "".join(text_chars),
        "positions": positions,
        "colors": colors,
        "text_lines": "\n".join(text_lines_list),
        "colored_lines": "\n".join(colored_lines_list),
        "color_set": {
            "foreground": sorted(fg_color_set),
            "background": sorted(bg_color_set),
        },
        "cursor": state.to_cursor_dict(),
    }


def get_screen(raw_ansi_text: str, width: int = 80, height: int = 24) -> dict[str, Any]:
    """
    获取屏幕字符矩阵及颜色信息

    Args:
        raw_ansi_text: 带ANSI转义符的原始文本
        width: 屏幕宽度
        height: 屏幕高度

    Returns:
        {
            'chars': [[char, ...], ...],  # 2维数组 chars[y][x]
                - ASCII单宽度: 'c', '='
                - 双宽度字符: '中l', '中r', '😄l', '😄r'
                - 空位: ''
            'colors': [[['#bg','#fg'], ...], ...],  # 2维颜色数组
            'color_set': {'foreground': [...], 'background': [...]},  # 去重后的颜色集合
            'cursor': {'position': [x, y], 'style': {...}}
        }
    """
    state, screen_matrix, _ = _parse_ansi_to_screen(raw_ansi_text, width, height)

    # 转换为输出格式
    chars = [["" for _ in range(width)] for _ in range(height)]
    colors = [[["", ""] for _ in range(width)] for _ in range(height)]
    fg_color_set = set()
    bg_color_set = set()

    for row_idx, row in enumerate(screen_matrix):
        prev_char = ""
        prev_colors = ["", ""]
        for col_idx, item in enumerate(row):
            if item is not None:
                char, style = item
                if char == "__WIDE__":
                    chars[row_idx][col_idx] = prev_char + "r"
                    colors[row_idx][col_idx] = prev_colors
                else:
                    char_width = get_char_width(char)
                    if char_width == 2:
                        chars[row_idx][col_idx] = char + "l"
                    else:
                        chars[row_idx][col_idx] = char
                    bg_hex = style.get_bg_hex()
                    fg_hex = style.get_fg_hex()
                    colors[row_idx][col_idx] = [bg_hex, fg_hex]
                    if fg_hex:
                        fg_color_set.add(fg_hex)
                    if bg_hex:
                        bg_color_set.add(bg_hex)
                    prev_char = char
                    prev_colors = [bg_hex, fg_hex]

    return {
        "chars": chars,
        "colors": colors,
        "color_set": {
            "foreground": sorted(fg_color_set),
            "background": sorted(bg_color_set),
        },
        "cursor": state.to_cursor_dict(),
    }


def get_screen_text(raw_ansi_text: str, x: int, y: int, width: int = 80, height: int = 24) -> str:
    """
    获取屏幕上指定位置左右连续的可见纯文本

    Args:
        raw_ansi_text: 带ANSI转义符的原始文本
        x: 屏幕列位置 (0-indexed)
        y: 屏幕行位置 (0-indexed)
        width: 屏幕宽度
        height: 屏幕高度

    Returns:
        位置(x,y)左右连续的可见纯文本（不含空格和空位）
    """
    # 获取屏幕矩阵
    screen = get_screen(raw_ansi_text, width=width, height=height)
    chars = screen["chars"]

    # 边界检查
    if y < 0 or y >= len(chars) or x < 0 or x >= len(chars[0]):
        return ""

    row = chars[y]

    # 辅助函数：判断是否为双宽度字符标记（长度>1且以l或r结尾）
    def is_wide_left(c):
        return len(c) > 1 and c.endswith("l")

    def is_wide_right(c):
        return len(c) > 1 and c.endswith("r")

    def get_wide_char(c):
        return c[:-1]  # 去除l或r后缀

    # 向左扫描
    left_parts = []
    col = x
    while col >= 0:
        c = row[col]
        if c == "" or c == " ":
            break
        # 处理双宽度字符
        if is_wide_right(c):
            # 这是双宽度字符的右半部分，跳过
            col -= 1
            continue
        if is_wide_left(c):
            left_parts.append(get_wide_char(c))
        else:
            left_parts.append(c)
        col -= 1
    left_parts.reverse()

    # 向右扫描
    right_parts = []
    col = x + 1
    while col < len(row):
        c = row[col]
        if c == "" or c == " ":
            break
        # 处理双宽度字符
        if is_wide_right(c):
            # 这是双宽度字符的右半部分，跳过
            col += 1
            continue
        if is_wide_left(c):
            right_parts.append(get_wide_char(c))
        else:
            right_parts.append(c)
        col += 1

    # 合并结果
    return "".join(left_parts + right_parts)


# ============ 基于新函数的简化功能 ============


def pure_text(text: str) -> str:
    """移除所有ANSI转义序列（基于get_text实现）"""
    return get_text(text)["text"]


def text_lines(text: str, width: int = 80, height: int = 24) -> str:
    """渲染屏幕（基于get_text的text_lines实现）"""
    return get_text(text, width, height)["text_lines"]


def extract_last_frame(text: str) -> str:
    """提取最后一帧"""
    pattern = r"\x1b\[H"
    matches = list(re.finditer(pattern, text))
    if matches:
        return text[matches[-1].start() :]
    return text


def colored_lines(text: str, width: int = 80, height: int = 24) -> str:
    """保留颜色的屏幕处理（基于get_text的colored_lines实现）"""
    return get_text(text, width, height)["colored_lines"]


def colors(text: str, width: int = 80, height: int = 24) -> dict:
    """分析颜色使用（基于get_text的color_set实现）"""
    result = get_text(text, width, height)
    color_set = result["color_set"]

    return {
        "foreground": color_set["foreground"],
        "background": color_set["background"],
        "cursor": result["cursor"],
    }


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="ANSI工具集 v3.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例：
  # 获取文本及位置颜色信息
  python3 ansi_tools.py get_text < input.txt

  # 获取屏幕字符矩阵
  python3 ansi_tools.py get_screen < input.txt

  # 获取指定位置的文本
  python3 ansi_tools.py get_screen_text --x 10 --y 5 < input.txt

  # 其他功能
  python3 ansi_tools.py plain < input.txt
  python3 ansi_tools.py colored < input.txt
  python3 ansi_tools.py frame < input.txt
  python3 ansi_tools.py colors < input.txt
        """,
    )

    parser.add_argument(
        "format",
        choices=[
            "get_text",
            "get_screen",
            "get_screen_text",
            "plain",
            "lines",
            "colored",
            "frame",
            "colors",
        ],
        default="get_text",
        nargs="?",
        help="输出格式",
    )
    parser.add_argument("input_file", nargs="?", help="输入文件")
    parser.add_argument("-o", "--output", help="输出文件")
    parser.add_argument("--width", type=int, default=80, help="屏幕宽度")
    parser.add_argument("--height", type=int, default=24, help="屏幕高度")
    parser.add_argument("--x", type=int, default=0, help="屏幕列位置（用于get_screen_text）")
    parser.add_argument("--y", type=int, default=0, help="屏幕行位置（用于get_screen_text）")
    parser.add_argument("--pretty", action="store_true", help="格式化JSON输出")

    args = parser.parse_args()

    # 读取输入
    if args.input_file:
        with open(args.input_file, encoding="utf-8", errors="replace") as f:
            text = f.read()
    else:
        text = sys.stdin.read()

    # 处理
    indent = 2 if args.pretty else None

    if args.format == "get_text":
        result = get_text(text, args.width, args.height)
        output = json.dumps(result, ensure_ascii=False, indent=indent)
    elif args.format == "get_screen":
        result = get_screen(text, args.width, args.height)
        output = json.dumps(result, ensure_ascii=False, indent=indent)
    elif args.format == "get_screen_text":
        output = get_screen_text(text, args.x, args.y, args.width, args.height)
    elif args.format == "plain":
        output = pure_text(text)
    elif args.format == "lines":
        output = text_lines(text, args.width, args.height)
    elif args.format == "colored":
        output = colored_lines(text, args.width, args.height)
    elif args.format == "frame":
        frame = extract_last_frame(text)
        output = colored_lines(frame, args.width, args.height)
    elif args.format == "colors":
        result = colors(text, args.width, args.height)
        output = json.dumps(result, ensure_ascii=False, indent=indent)
    else:
        output = text

    # 输出
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
    else:
        print(output)


if __name__ == "__main__":
    main()

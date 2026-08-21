#!/usr/bin/env python3
"""
ANSI工具可视化演示

展示彩色文本、图样和屏幕渲染功能
"""

import os
import sys
import time

# 添加技能目录到路径
sys.path.insert(0, os.path.dirname(__file__) if os.path.dirname(__file__) else ".")
from ansi_tools import text_lines


# ANSI 颜色代码
class Colors:
    """ANSI 颜色代码"""

    RESET = "\x1b[0m"
    # 前景色
    BLACK = "\x1b[30m"
    RED = "\x1b[31m"
    GREEN = "\x1b[32m"
    YELLOW = "\x1b[33m"
    BLUE = "\x1b[34m"
    MAGENTA = "\x1b[35m"
    CYAN = "\x1b[36m"
    WHITE = "\x1b[37m"
    # 明亮前景色
    BRIGHT_BLACK = "\x1b[90m"
    BRIGHT_RED = "\x1b[91m"
    BRIGHT_YELLOW = "\x1b[92m"
    BRIGHT_GREEN = "\x1b[93m"
    BRIGHT_BLUE = "\x1b[94m"
    BRIGHT_MAGENTA = "\x1b[95m"
    BRIGHT_CYAN = "\x1b[96m"
    BRIGHT_WHITE = "\x1b[97m"
    # 背景色
    BG_BLACK = "\x1b[40m"
    BG_RED = "\x1b[41m"
    BG_GREEN = "\x1b[42m"
    BG_YELLOW = "\x1b[43m"
    BG_BLUE = "\x1b[44m"
    BG_MAGENTA = "\x1b[45m"
    BG_CYAN = "\x1b[46m"
    BG_WHITE = "\x1b[47m"
    # 样式
    BOLD = "\x1b[1m"
    DIM = "\x1b[2m"
    ITALIC = "\x1b[3m"
    UNDERLINE = "\x1b[4m"
    BLINK = "\x1b[5m"
    REVERSE = "\x1b[7m"
    HIDDEN = "\x1b[8m"
    STRIKETHROUGH = "\x1b[9m"


def demo_colors():
    """演示基本颜色功能"""
    print("\n" + "=" * 70)
    print("【演示1: 基本颜色和样式】")
    print("=" * 70)

    print(
        f"\n{Colors.RED}红色{Colors.GREEN}绿色{Colors.YELLOW}黄色{Colors.BLUE}蓝色{Colors.MAGENTA}品红{Colors.CYAN}青色{Colors.WHITE}白色{Colors.RESET}"
    )

    print(
        f"\n{Colors.BOLD}粗体{Colors.RESET} {Colors.UNDERLINE}下划线{Colors.RESET} {Colors.ITALIC}斜体{Colors.RESET}"
    )

    print(
        f"\n{Colors.BLINK}闪烁{Colors.RESET} {Colors.REVERSE}反色{Colors.RESET} {Colors.STRIKETHROUGH}删除线{Colors.RESET}"
    )

    print(
        f"\n{Colors.BG_BLACK} 黑底{Colors.BG_RED}红底{Colors.BG_GREEN}绿底{Colors.BG_YELLOW}黄底{Colors.RESET}"
    )
    print(
        f"{Colors.BG_BLUE}蓝底{Colors.BG_MAGENTA}品底{Colors.BG_CYAN}青底{Colors.BG_WHITE}白底{Colors.RESET}"
    )


def demo_gradient():
    """演示渐变效果"""
    print("\n" + "=" * 70)
    print("【演示2: 渐变效果】")
    print("=" * 70)

    print("\n256色彩条:")
    for i in range(0, 256, 8):
        print(f"\x1b[38;5;{i}m█", end="")
    print(Colors.RESET)

    print("\n灰度渐变:")
    for i in range(24):
        shade = 232 + i
        print(f"\x1b[38;5;{shade}m▓", end="")
    print(Colors.RESET)


def demo_patterns():
    """演示图案效果"""
    print("\n" + "=" * 70)
    print("【演示3: 图案效果】")
    print("=" * 70)

    print("\n棋盘格:")
    for row in range(4):
        for col in range(8):
            if (row + col) % 2 == 0:
                print(f"{Colors.BG_WHITE}  {Colors.RESET}", end="")
            else:
                print(f"{Colors.BG_BLACK}  {Colors.RESET}", end="")
        print()

    print("\n条纹图案:")
    colors = [
        Colors.BG_RED,
        Colors.BG_YELLOW,
        Colors.BG_GREEN,
        Colors.BG_CYAN,
        Colors.BG_BLUE,
        Colors.BG_MAGENTA,
    ]
    for bg in colors:
        print(f"{bg}  {Colors.RESET}", end="")
    print()

    print("\n波浪效果:")
    waves = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
    for i in range(16):
        wave = waves[i % len(waves)]
        shade = 232 + (i % 24)
        print(f"\x1b[38;5;{shade}m{wave}", end="")
    print(Colors.RESET)


def demo_progress_bars():
    """演示进度条样式"""
    print("\n" + "=" * 70)
    print("【演示4: 进度条样式】")
    print("=" * 70)

    percentages = [25, 50, 75, 100]
    for pct in percentages:
        filled = pct // 5
        empty = 20 - filled
        print(
            f"  {pct:3d}% [{Colors.BG_GREEN}{' ' * filled}{Colors.BG_BLACK}{' ' * empty}{Colors.RESET}]"
        )


def demo_boxes():
    """演示边框样式"""
    print("\n" + "=" * 70)
    print("【演示5: 边框样式】")
    print("=" * 70)

    print("\n单线边框:")
    # 使用英文确保显示宽度一致（中文字符在终端占2宽度）
    box = f"""{Colors.BRIGHT_CYAN}╔═══════════════════════╗{Colors.RESET}
{Colors.BRIGHT_CYAN}║{Colors.WHITE} Sample Text Content   {Colors.BRIGHT_CYAN}║{Colors.RESET}
{Colors.BRIGHT_CYAN}╚═══════════════════════╝{Colors.RESET}
"""
    print(box)
    print("\n纯文本渲染结果:")
    plain = text_lines(box, width=40, height=5)
    print(plain)

    print("\n双线边框:")
    # 使用英文确保显示宽度一致
    box_lines = [
        f"{Colors.BRIGHT_BLUE}╔═══════════════════════╗{Colors.RESET}",
        f"{Colors.BRIGHT_BLUE}║{Colors.WHITE}   ╔═══════════════╗   {Colors.BRIGHT_BLUE}║{Colors.RESET}",
        f"{Colors.BRIGHT_BLUE}║{Colors.WHITE}   ║ Sample Content{Colors.WHITE}║{Colors.BRIGHT_BLUE}   {Colors.BRIGHT_BLUE}║{Colors.RESET}",
        f"{Colors.BRIGHT_BLUE}║{Colors.WHITE}   ╚═══════════════╝   {Colors.BRIGHT_BLUE}║{Colors.RESET}",
        f"{Colors.BRIGHT_BLUE}╚═══════════════════════╝{Colors.RESET}",
    ]
    box = "\n".join(box_lines)
    print(box)
    print("\n纯文本渲染结果:")
    plain = text_lines(box, width=40, height=8)
    print(plain)


def demo_rainbow_text():
    """演示彩虹文字效果"""
    print("\n" + "=" * 70)
    print("【演示6: 彩虹文字】")
    print("=" * 70)

    rainbow = [
        Colors.RED,
        Colors.BRIGHT_YELLOW,
        Colors.GREEN,
        Colors.CYAN,
        Colors.BLUE,
        Colors.MAGENTA,
    ]
    text = "彩虹文字效果演示"

    print(f"\n原始: {text}")
    print("彩虹: ", end="")
    for i, char in enumerate(text):
        color = rainbow[i % len(rainbow)]
        print(f"{color}{char}{Colors.RESET}", end="")
    print()


def demo_tables():
    """演示表格样式"""
    print("\n" + "=" * 70)
    print("【演示7: 表格样式】")
    print("=" * 70)

    # 使用英文确保显示宽度一致
    table_lines = [
        " Name     │ Qty     │ Stat ",
        "──────────┼─────────┼──────",
        f"{Colors.RED}Apple     {Colors.RESET}│ 100     │ {Colors.GREEN}OK{Colors.RESET}   ",
        f"{Colors.YELLOW}Banana    {Colors.RESET}│ 200     │ {Colors.BRIGHT_YELLOW}WARN{Colors.RESET}  ",
        f"{Colors.BLUE}Cherry    {Colors.RESET}│ 300     │ {Colors.BRIGHT_RED}FAIL{Colors.RESET}  ",
    ]
    table = "\n".join(table_lines)
    print(table)
    print("\n纯文本渲染结果:")
    plain = text_lines(table, width=35, height=6)
    print(plain)


def demo_tree():
    """演示树形结构"""
    print("\n" + "=" * 70)
    print("【演示8: 树形结构】")
    print("=" * 70)

    tree = f"""{Colors.GREEN}root/{Colors.RESET}
├──{Colors.BLUE}branch1/{Colors.RESET}
│   ├──{Colors.YELLOW}leaf1/{Colors.RESET}
│   └──{Colors.YELLOW}leaf2/{Colors.RESET}
└──{Colors.BLUE}branch2/{Colors.RESET}
    └──{Colors.YELLOW}leaf3/{Colors.RESET}
"""
    print(tree)


def demo_progress_animation():
    """演示进度条动画"""
    print("\n" + "=" * 70)
    print("【演示9: 进度条动画】")
    print("=" * 70)
    print("\n加载中...")

    for i in range(0, 101, 2):
        filled = i // 2
        empty = 50 - filled
        print(
            f"\r[{Colors.BG_GREEN}{'█' * filled}{Colors.BG_BLACK}{'░' * empty}{Colors.RESET}] {i:3d}%",
            end="",
            flush=True,
        )
        time.sleep(0.03)
    print(Colors.RESET)


def render_comparison_demo():
    """演示屏幕渲染对比 - 原始ANSI vs 渲染结果"""
    print("\n" + "=" * 70)
    print("【演示10: 屏幕渲染对比】")
    print("=" * 70)

    ansi_text = (
        "\x1b[2J\x1b[H"
        "\x1b[1;31m\x1b[7m 主菜单 \x1b[0m\n"
        "\x1b[3;5H\x1b[32m[ ] \x1b[0m\x1b[1;37m1. 新游戏\x1b[0m\n"
        "\x1b[4;5H\x1b[33m[ ] \x1b[0m\x1b[1;37m2. 继续游戏\x1b[0m\n"
        "\x1b[5;5H\x1b[34m[ ] \x1b[0m\x1b[1;37m3. 设置\x1b[0m\n"
        "\x1b[6;5H\x1b[31m[ ] \x1b[0m\x1b[1;37m4. 退出\x1b[0m\n"
        "\x1b[8;5H\x1b[36m按 1-4 选择...\x1b[0m"
    )

    print(f"\n{Colors.YELLOW}原始 ANSI 文本{Colors.RESET}:")
    print("  " + "-" * 70)
    for line in ansi_text.split("\n")[:8]:
        print(f"  {line}")

    print(f"\n{Colors.YELLOW}渲染后结果{Colors.RESET}:")
    print("  " + "-" * 70)

    rendered = text_lines(ansi_text, width=40, height=10)

    for line in rendered.split("\n"):
        if line.strip():
            print(f"  {line}")


def demo_real_ncurses():
    """演示真实的 ncurses 程序输出样式"""
    print("\n" + "=" * 70)
    print("【演示11: 真实 ncurses 程序样式】")
    print("=" * 70)

    # 模拟htop的单帧输出，不使用多次清屏
    htop_lines = [
        "\x1b[2J\x1b[H",  # 初始清屏
        "\x1b[1mCPU: \x1b[0m[\x1b[32m░░░░░░░░░░░░░\x1b[0m] 8%  \x1b[1mLoad: 1.23\x1b[0m\n",
        "\x1b[2mTasks: 123\x1b[0m  \x1b[1mThreads: 456\x1b[0m\n",
        "\n",
        "\x1b[31m\x1b[40m PID    USER      COMMAND  \x1b[0m\n",
        "\x1b[0m 1234  user1     python3\n",
        "\x1b[0m 5678  user2     nvim\n",
        "\x1b[32m\x1b[40m Mem: 2.1G/4.5G  Swap: 256M/512M\x1b[0m\n",
        "\x1b[33m\x1b[40m Uptime: 123:45  LoadAvg: 1.23\x1b[0m",
    ]
    htop_style = "".join(htop_lines)

    print(f"\n{Colors.CYAN}htop 风格输出{Colors.RESET}:")
    print("  " + "-" * 70)

    rendered = text_lines(htop_style, width=40, height=15)
    for line in rendered.split("\n"):
        if line.strip():
            print(f"  {line}")


def demo_status_panel():
    """演示系统状态面板"""
    print("\n" + "=" * 70)
    print("【演示12: 系统状态面板】")
    print("=" * 70)

    panel_lines = [
        "\x1b[2J\x1b[H",
        f"{Colors.BG_CYAN}{Colors.BLACK}┌─────────────────────────────┐{Colors.RESET}\n",
        f"{Colors.BG_CYAN}{Colors.BLACK}│{Colors.YELLOW} [STATUS] System Monitor     {Colors.BG_CYAN}{Colors.BLACK}│{Colors.RESET}\n",
        f"{Colors.BG_CYAN}{Colors.BLACK}├─────────────────────────────┤{Colors.RESET}\n",
        f"{Colors.BG_CYAN}{Colors.BLACK}│{Colors.RESET}                             {Colors.BG_CYAN}{Colors.BLACK}│{Colors.RESET}\n",
        f"{Colors.BG_CYAN}{Colors.BLACK}│{Colors.GREEN} + CPU: 45% STATUS-BAR...    {Colors.BG_CYAN}{Colors.BLACK}│{Colors.RESET}\n",
        f"{Colors.BG_CYAN}{Colors.BLACK}│{Colors.RESET}                             {Colors.BG_CYAN}{Colors.BLACK}│{Colors.RESET}\n",
        f"{Colors.BG_CYAN}{Colors.BLACK}│{Colors.BLUE} + MEM: 62% STATUS-BAR...    {Colors.BG_CYAN}{Colors.BLACK}│{Colors.RESET}\n",
        f"{Colors.BG_CYAN}{Colors.BLACK}│{Colors.RESET}                             {Colors.BG_CYAN}{Colors.BLACK}│{Colors.RESET}\n",
        f"{Colors.BG_CYAN}{Colors.BLACK}│{Colors.YELLOW} + DSK: 23% STATUS-BAR...    {Colors.BG_CYAN}{Colors.BLACK}│{Colors.RESET}\n",
        f"{Colors.BG_CYAN}{Colors.BLACK}│{Colors.RESET}                             {Colors.BG_CYAN}{Colors.BLACK}│{Colors.RESET}\n",
        f"{Colors.BG_CYAN}{Colors.BLACK}└─────────────────────────────┘{Colors.RESET}\n",
    ]
    panel = "".join(panel_lines)

    rendered = text_lines(panel, width=40, height=10)
    for line in rendered.split("\n"):
        if line.strip():
            print(line)


def demo_colorful_text():
    """演示彩色文本"""
    print("\n" + "=" * 70)
    print("【演示13: 彩色文本效果】")
    print("=" * 70)

    examples = [
        (
            f"\n{Colors.BOLD}粗体文本{Colors.RESET} - {Colors.UNDERLINE}下划线{Colors.RESET} - {Colors.ITALIC}斜体{Colors.RESET}"
        ),
        (
            f"\n{Colors.RED}错误 {Colors.RED}严重错误{Colors.RESET}: {Colors.RED}磁盘空间不足{Colors.RESET}"
        ),
        (
            f"\n{Colors.GREEN}成功{Colors.RESET} - {Colors.BRIGHT_GREEN}操作完成{Colors.RESET} - {Colors.BRIGHT_CYAN}状态更新{Colors.RESET}"
        ),
        (
            f"\n{Colors.YELLOW}警告{Colors.RESET} - {Colors.BRIGHT_YELLOW}配置未找到{Colors.RESET} - {Colors.BRIGHT_YELLOW}使用默认值{Colors.RESET}"
        ),
        (
            f"\n{Colors.BLUE}信息{Colors.RESET} - {Colors.BRIGHT_BLUE}正在连接...{Colors.RESET} - {Colors.BRIGHT_BLUE}连接成功{Colors.RESET}"
        ),
        (
            f"\n{Colors.MAGENTA}调试{Colors.RESET} - {Colors.BRIGHT_MAGENTA}请稍候...{Colors.RESET} - {Colors.BRIGHT_MAGENTA}处理完成{Colors.RESET}"
        ),
    ]

    for example in examples:
        print(example)


def show_usage_examples():
    """显示使用示例"""
    print("\n" + "=" * 70)
    print("【使用示例】")
    print("=" * 70)

    usage_text = (
        Colors.CYAN
        + "命令行使用:"
        + Colors.RESET
        + """
  cat ansi_output.txt | python3 ansi_tools.py plain

"""
        + Colors.GREEN
        + "Python API 使用:"
        + Colors.RESET
        + """
  from ansi_tools import pure_text, text_lines

  # 移除 ANSI 序列
  clean = pure_text(raw_text)

  # 渲染屏幕
  screen = text_lines(raw_text, width=80, height=24)

"""
        + Colors.YELLOW
        + "支持的 ANSI 序列:"
        + Colors.RESET
        + """
  - CSI (Control Sequence): ESC [ 参数... 命令
    - 光标移动: ESC[nA (上), ESC[nB (下), ESC[nC (右), ESC[nD (左)
    - 光标定位: ESC[row;colH
    - 清屏: ESC[2J
    - 颜色: ESC[31m (红), ESC[32m (绿), 等等

  - OSC (Operating System): ESC ] ... ST/BEL
    - 窗口标题: ESC ] 0;Title BEL

  - DCS/SOS/PM/APC: ESC P/X/^/_ ... ST

  - 简单转义: ESC M (反向索引), ESC D (索引), 等等

"""
        + Colors.BRIGHT_RED
        + "提示:"
        + Colors.RESET
        + " 渲染功能会处理所有光标定位序列，重建实际的屏幕布局！"
    )
    print(usage_text)


def main():
    """主函数"""
    print("=" * 70)
    print("ANSI工具 - 可视化演示")
    print("=" * 70)
    print("\n展示彩色文本、图样和屏幕渲染功能")
    print()

    demo_colors()
    demo_gradient()
    demo_patterns()
    demo_progress_bars()
    demo_boxes()
    demo_rainbow_text()
    demo_tables()
    demo_tree()
    demo_progress_animation()
    render_comparison_demo()
    demo_real_ncurses()
    demo_status_panel()
    demo_colorful_text()

    show_usage_examples()

    print("\n" + "=" * 70)
    print("演示完成！")
    print("=" * 70)


if __name__ == "__main__":
    main()

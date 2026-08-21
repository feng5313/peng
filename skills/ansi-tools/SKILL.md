---
name: ansi-tools
description: 从包含ANSI转义序列的文本中提取纯文本、渲染终端屏幕布局、保留原始格式、分析颜色。当处理终端输出、ncurses程序屏幕捕获、日志清洗、TUI程序截图或用户提及ANSI/终端/ncurses时使用。
---

## 快速开始

# 使用

```
ansi_tools plain < input.txt
ansi_tools lines < input.txt
ansi_tools colored < input.txt
ansi_tools colors < input.txt
```

## 命令行用法

```bash
# Python版
python3 ansi_tools.py <command> [options] < input.txt

# C++版（更快）
ansi_tools <command> [options] < input.txt
```

### 命令列表

| 命令 | 说明 | 输出格式 |
|------|------|----------|
| `get_text` | 获取完整文本信息 | JSON |
| `get_screen` | 获取屏幕矩阵 | JSON |
| `plain` | 移除ANSI序列 | 纯文本 |
| `lines` | 渲染屏幕布局 | 纯文本 |
| `colored` | 保留颜色输出 | ANSI文本 |
| `colors` | 分析颜色使用 | JSON |

### 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--width=N` | 屏幕宽度 | 80 |
| `--height=N` | 屏幕高度 | 24 |
| `-o FILE` | 输出到文件 | stdout |

### 使用示例

```bash
# 获取完整信息（JSON）
ansi_tools get_text < input.txt

# 移除ANSI序列
ansi_tools plain < input.txt

# 渲染屏幕布局
ansi_tools lines --width=120 --height=40 < input.txt

# 保留颜色输出
ansi_tools colored < input.txt > output.txt

# 分析颜色
ansi_tools colors < input.txt
```

## 返回值详解

### get_text 返回值

```python
{
    'text': 'RedGreen',           # 纯文本（无换行）
    'positions': [[0,0],[1,0],...], # 每个字符的屏幕位置 [x,y]
    'colors': [['','#800000'],...], # 每个字符的 [背景色,前景色]
    'text_lines': 'Red\nGreen',   # 纯文本（有换行）
    'colored_lines': '\x1b[31mRed\x1b[0m\n...',
    'color_set': {
        'foreground': ['#800000', '#008000'],
        'background': ['#000080']
    },
    'cursor': {'position': [5, 1], 'style': {...}}
}
```

### 双宽度字符处理

```python
# 中文、emoji等双宽度字符拆分为左右两格
screen = get_screen(raw_output)
# '中' -> chars[y][x] = '中l', chars[y][x+1] = '中r'

# 判断方法
char = screen['chars'][y][x]
if len(char) > 1 and char.endswith('l'):
    actual_char = char[:-1]  # 去掉'l'得到原字符
```

## 调试技巧

```bash
# 查看ANSI序列
cat -v output.txt | head -20

# 查看颜色文件
cat colored_output.txt

# 分析颜色
ansi_tools colors < output.txt
```

## 相关技能

- **pexpect** - 捕获终端输出，配合ansi-tools处理

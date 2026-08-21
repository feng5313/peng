---
name: pexpect
description: Python 终端自动化库，用于控制交互式命令行程序、捕获 stdout/stderr 输出、自动化测试 CLI 工具。当需要自动化终端程序、捕获 ncurses/TUI 程序屏幕、自动化测试 CLI 或用户提及终端自动化/pexpect 时使用。
---

# pexpect 终端自动化 Skill

pexpect 是 Python 的终端自动化库，用于控制和测试交互式命令行程序。提供完善的 API 封装，支持进程控制、输出捕获、输入发送和模式匹配。Windows版本为 wexpect !

## 功能特性

- **进程控制**: spawn() 创建交互式程序
- **输出捕获**: 读取程序的完整 stdout/stderr（包含 ANSI 序列）
- **输入发送**: send() 系列方法发送文本和按键
- **模式匹配**: expect() 等待特定输出模式
- **窗口设置**: 支持自定义终端窗口尺寸
- **自动化测试**: 支持完整的测试流程
- **ANSI 处理**: 配合 ansi-tools 命令行工具处理输出

## 快速开始

### 安装

```bash
# Debian/Ubuntu
sudo apt install python3-pexpect

# pip 安装（推荐）
source .venv/bin/activate # 激活虚拟环境
pip install pexpect

# 验证安装
python -c "import pexpect; print(pexpect.__version__)"
```

### 基本用法

```python
import pexpect
import time

# 启动程序（必须设置 dimensions！）
child = pexpect.spawn('/usr/bin/nyancat',
                     encoding='utf-8',
                     dimensions=(24, 80))  # 行数，列数

# 等待程序启动
time.sleep(3)

# 读取输出（使用 read_nonblocking）
output = child.read_nonblocking(size=50000, timeout=1)

# 退出程序
child.sendcontrol('c')  # Ctrl+C
child.close()
```

## 核心 API 详解

### 1. spawn() - 创建子进程

**重要：必须设置 dimensions 参数！**

```python
import pexpect

# 推荐用法：同时设置 encoding 和 dimensions
child = pexpect.spawn('command',
                     encoding='utf-8',
                     dimensions=(24, 80))  # (行数，列数)
```

**spawn() 参数说明**:

| 参数         | 类型  | 默认值   | 说明                    |
| ------------ | ----- | -------- | ----------------------- |
| `command`    | str   | -        | 要执行的命令            |
| `args`       | list  | []       | 命令参数列表            |
| `encoding`   | str   | None     | 文本编码（推荐'utf-8'） |
| `timeout`    | int   | 30       | 默认超时时间（秒）      |
| `dimensions` | tuple | (24, 80) | 终端窗口尺寸 (行，列)     |

**常见程序窗口尺寸**:

| 程序类型 | 推荐尺寸 | 说明            |
| -------- | -------- | --------------- |
| 小型 TUI  | 24x80    | nyancat, sudoku |
| 中型 TUI  | 40x120   | htop, nmon      |
| 大型 TUI  | 50x200   | vim, emacs 全屏  |
| 文本编辑 | 30x100   | 舒适的编辑区域  |

### 2. 读取输出 - read vs read_nonblocking

**关键经验：对于动画/持续输出程序，使用 read_nonblocking！**

```python
# 对于静态程序 - read() 可以工作
output = child.read(size=20000)

# 对于动画/持续输出程序 - 必须用 read_nonblocking
try:
    output = child.read_nonblocking(size=100000, timeout=1)
except:
    output = child.before or ''
```

**为什么动画程序需要 read_nonblocking？**

- 动画程序持续输出，read() 会一直等待
- read_nonblocking 可以设置超时，避免阻塞
- 能捕获到指定时刻的完整输出

### 3. send() - 发送输入

```python
# 发送字符串
child.send('hello')
child.sendline('input')  # 自动添加换行

# 发送控制字符（推荐）
child.sendcontrol('c')  # Ctrl+C
child.sendcontrol('d')  # Ctrl+D

# 特殊按键
child.send('\x1b[A')  # 方向键上
child.send('\t')      # Tab
```

### 4. expect() - 等待模式

```python
# 等待字符串
child.expect('Password:')

# 等待多个模式
index = child.expect(['success', 'error', pexpect.TIMEOUT])

# 带超时
try:
    child.expect('ready', timeout=10)
except pexpect.TIMEOUT:
    print("超时")
```

### 5. 程序控制

```python
# 检查状态
if child.isalive():
    print("运行中")

# 优雅退出
child.sendcontrol('c')
time.sleep(0.1)
child.close()

# 强制终止
if child.isalive():
    child.terminate(force=True)
```

## 使用场景

### ansi-tools 命令行工具使用

**所有涉及 ANSI 输出处理的场景都使用 ansi-tools 命令行工具**：

```python
import subprocess
import json

# 基本调用模板
result = subprocess.run(
    ['ansi_tools', '<command>', '--width', '<width>', '--height', '<height>'],
    input=raw_output,
    capture_output=True,
    text=True
)
output = result.stdout  # 或 json.loads(result.stdout)
```

**常用命令**：

| 命令 | 功能 | 输出格式 |
|------|------|---------|
| `get_text` | 获取完整屏幕信息 | JSON（文本、位置、颜色、光标） |
| `get_screen` | 获取屏幕矩阵 | JSON（chars数组、colors数组） |
| `colored` | 保留颜色输出 | 带ANSI颜色代码的文本 |
| `lines` | 获取纯文本（含换行） | 纯文本，保留换行 |
| `plain` | 获取纯文本（不含换行） | 纯文本，无换行 |
| `colors` | 分析颜色使用 | JSON（前景色、背景色、光标） |

### 场景 1: 捕获动画程序帧

```python
import pexpect
import time
import subprocess

def capture_animation(command, target_time=3.0, width=80, height=24):
    """捕获动画程序在指定时间点的帧，返回带颜色的文本"""
    child = pexpect.spawn(command, encoding='utf-8', dimensions=(height, width))
    time.sleep(target_time)

    try:
        raw = child.read_nonblocking(size=100000, timeout=1)
    except:
        raw = child.before or ''

    child.sendcontrol('c')
    child.close()

    # 使用 ansi-tools 保留颜色
    colored_cmd = subprocess.run(
        ['ansi_tools', 'colored', '--width', str(width), '--height', str(height)],
        input=raw, capture_output=True, text=True
    )
    return colored_cmd.stdout

# 使用
colored = capture_animation('/usr/bin/nyancat', target_time=3)
print(colored)  # 直接打印带颜色
```

### 场景 2: 获取完整屏幕信息或指定位置文本

```python
import subprocess
import json

# 获取完整信息（JSON 格式）
result = subprocess.run(
    ['ansi_tools', 'get_text', '--width', '80', '--height', '24'],
    input=raw_output, capture_output=True, text=True
)
data = json.loads(result.stdout)
print(data['text'])           # 纯文本
print(data['text_lines'])     # 带换行的文本
print(data['colored_lines'])  # 带颜色的文本
print(data['color_set'])      # 颜色集合
print(data['cursor'])         # 光标位置

# 提取指定位置文本（从屏幕矩阵获取）
screen_result = subprocess.run(
    ['ansi_tools', 'get_screen', '--width', '80', '--height', '24'],
    input=raw_output, capture_output=True, text=True
)
screen = json.loads(screen_result.stdout)
x, y = 10, 5
width = 20
chars = screen['chars'][y]
text = ''.join(chars[x:x+width])
print(f"位置 ({x},{y}) 的文本：{text}")
```

### 场景 3: 自动化测试 CLI 程序

```python
def test_interactive_program():
    child = pexpect.spawn('myapp', encoding='utf-8', dimensions=(24, 80))

    # 测试启动
    child.expect('Welcome', timeout=5)

    # 测试命令
    child.sendline('status')
    child.expect('Running')

    # 清理
    child.sendline('quit')
    child.close()
```

### 场景 4: 棋盘/网格类游戏自动化

```python
class BoardGameAutoPlayer:
    """棋盘游戏自动化基类"""

    def __init__(self, cmd, width=80, height=24):
        self.child = pexpect.spawn(cmd, encoding='utf-8', dimensions=(height, width))
        self.width = width
        self.height = height
        self.cursor = (0, 0)  # 跟踪当前光标位置（逻辑坐标）
        time.sleep(0.5)

    def read_screen(self):
        """读取屏幕内容"""
        try:
            self.child.read_nonblocking(size=50000, timeout=0.1)
        except:
            pass
        self.child.send('d')  # 刷新
        time.sleep(0.2)
        return self.child.read_nonblocking(size=50000, timeout=1)

    def move_to(self, target_row, target_col):
        """移动到指定格子"""
        cur_row, cur_col = self.cursor

        if target_row > cur_row:
            self.child.send('j' * (target_row - cur_row))
        elif target_row < cur_row:
            self.child.send('k' * (cur_row - target_row))

        if target_col > cur_col:
            self.child.send('l' * (target_col - cur_col))
        elif target_col < cur_col:
            self.child.send('h' * (cur_col - target_col))

        time.sleep(0.01)
        self.cursor = (target_row, target_col)

    def close(self):
        """关闭程序"""
        if self.child and self.child.isalive():
            self.child.send('q')
            time.sleep(0.1)
            self.child.close()
```

### 场景 5: 光标位置跟踪

**关键：光标初始位置不确定，必须先获取位置再计算移动**

```python
import subprocess
import json

def get_cursor_position(child, width=80, height=24):
    """获取光标在屏幕上的位置 [x, y]"""
    # 清空缓冲区
    try:
        child.read_nonblocking(size=50000, timeout=0.1)
    except:
        pass

    # 发送刷新命令（大多数 ncurses 程序用 d 刷新）
    child.send('d')
    time.sleep(0.2)

    # 读取输出
    raw = child.read_nonblocking(size=50000, timeout=1)

    # 解析光标位置
    result = subprocess.run(
        ['ansi_tools', 'get_text', '--width', str(width), '--height', str(height)],
        input=raw, capture_output=True, text=True
    )
    data = json.loads(result.stdout)
    return data['cursor']['position']  # [x, y]

# 使用示例：只在开始时获取一次，之后自己跟踪
start_pos = get_cursor_position(child)  # 只获取一次
cursor = (0, 0)  # 逻辑坐标

for row, col in moves:
    # 自己计算移动
    move_to(row, col)
    cursor = (row, col)

# ❌ 不好的做法：频繁刷新获取位置（刷新会改变位置！）
# for row, col in moves:
#     pos = get_cursor_position(child)  # 刷新会改变位置！
```

## 开发方法论

### 迭代开发策略

**核心原则**：
- 先手动单步操作获得经验，再编写脚本
- 确定固定操作产生固定变化后，再编程加速执行
- 关键点获取屏幕内容验证是否符合预期

**1. 单步调试阶段**

```python
import subprocess

def debug_step(child, action, description, width=80, height=24):
    """单步调试：记录操作前后的屏幕变化"""

    # 记录操作前屏幕
    before = child.read_nonblocking(size=50000, timeout=0.5) if action else ""
    print(f"=== {description} - 操作前 ===")
    before_cmd = subprocess.run(
        ['ansi_tools', 'lines', '--width', str(width), '--height', str(height)],
        input=before, capture_output=True, text=True
    )
    print(before_cmd.stdout[:300])

    # 执行操作
    if action:
        child.send(action)
        time.sleep(0.1)

        # 记录操作后屏幕
        after = child.read_nonblocking(size=50000, timeout=0.5)
        print(f"=== {description} - 操作后 ===")
        after_cmd = subprocess.run(
            ['ansi_tools', 'lines', '--width', str(width), '--height', str(height)],
            input=after, capture_output=True, text=True
        )
        print(after_cmd.stdout[:300])
        return after
    return before
```

**2. 确定固定行为后加速**

```python
DEBUG = True  # 开发时设为 True

if DEBUG:
    # 调试阶段：每步都验证
    debug_step(child, 'j', "向下移动")
else:
    # 生产阶段：批量执行，只在关键点验证
    for action in actions:
        child.send(action)
        time.sleep(0.005)

    # 关键点验证
    final_screen = read_screen(child)
    assert expected_content in final_screen
```

**3. 保存调试屏幕快照**

```python
import subprocess
import os

def save_debug_screen(raw, step_name, debug_dir='/tmp/debug'):
    """保存调试屏幕快照"""
    os.makedirs(debug_dir, exist_ok=True)

    # 保存原始数据
    with open(f'{debug_dir}/{step_name}_raw.txt', 'w') as f:
        f.write(repr(raw))

    # 保存可读文本
    text_cmd = subprocess.run(
        ['ansi_tools', 'lines', '--width', '80', '--height', '24'],
        input=raw, capture_output=True, text=True
    )
    with open(f'{debug_dir}/{step_name}_text.txt', 'w') as f:
        f.write(text_cmd.stdout)

    # 保存带颜色版本
    colored_cmd = subprocess.run(
        ['ansi_tools', 'colored', '--width', '80', '--height', '24'],
        input=raw, capture_output=True, text=True
    )
    with open(f'{debug_dir}/{step_name}_colored.txt', 'w') as f:
        f.write(colored_cmd.stdout)
```

## 最佳实践

### ✅ 推荐做法

1. **始终设置 dimensions**

   ```python
   child = pexpect.spawn('cmd', encoding='utf-8', dimensions=(24, 80))
   ```

2. **动画程序用 read_nonblocking**

   ```python
   output = child.read_nonblocking(size=100000, timeout=1)
   ```

3. **使用 ansi-tools 命令行处理输出**

   ```python
   result = subprocess.run(
       ['ansi_tools', 'colored', '--width', '80', '--height', '24'],
       input=raw_output, capture_output=True, text=True
   )
   colored = result.stdout
   ```

4. **优雅退出**

   ```python
   child.sendcontrol('c')
   time.sleep(0.1)
   child.close()
   ```

5. **批量发送按键提高性能**

   ```python
   # 慢：每个按键后延迟
   for _ in range(8):
       child.send('j')
       time.sleep(0.005)  # 总计 40ms

   # 快：批量发送后统一延迟
   child.send('j' * 8)
   time.sleep(0.04)  # 40ms
   ```

6. **只在开始时获取光标位置，之后自己跟踪**

   ```python
   start_pos = get_cursor_position(child)  # 只获取一次
   cursor = (0, 0)  # 逻辑坐标

   for row, col in moves:
       move_to(row, col)
       cursor = (row, col)
   ```

### ❌ 避免做法

1. **不要忘记 dimensions**

   ```python
   # ❌ 错误
   child = pexpect.spawn('htop')

   # ✅ 正确
   child = pexpect.spawn('htop', dimensions=(40, 120))
   ```

2. **动画程序不要用 read()**

   ```python
   # ❌ 可能阻塞
   output = child.read()

   # ✅ 使用 read_nonblocking
   output = child.read_nonblocking(size=100000, timeout=1)
   ```

3. **不要频繁刷新获取光标位置**

   ```python
   # ❌ 刷新会改变位置！
   for row, col in moves:
       pos = get_cursor_position(child)
   ```

## 调试技巧

### 1. 打印所有输出

```python
import sys
child.logfile_read = sys.stdout
```

### 2. 检查进程状态

```python
print(f"PID: {child.pid}")
print(f"Alive: {child.isalive()}")
print(f"Exit status: {child.exitstatus}")
```

### 3. 查看 ANSI 序列

```bash
# 十六进制
xxd output.txt | head -20

# 转义字符
cat -v output.txt
```

### 4. 分析颜色

```bash
ansi_tools colors < output.txt
```

## 常见问题

### Q: read() 返回空或阻塞

**原因**: 动画/持续输出程序用 read() 会阻塞

**解决**:

```python
output = child.read_nonblocking(size=100000, timeout=1)
```

### Q: ncurses 显示异常

**原因**: 未设置正确的 dimensions

**解决**:

```python
child = pexpect.spawn('program', dimensions=(40, 120))
```

### Q: 光标位置不准确

**原因**: 刷新屏幕会改变光标位置

**解决**: 只在开始时获取一次光标位置，之后自己跟踪移动（参考场景5示例）

## 相关技能

- **ansi-tools** - 处理捕获的 ANSI 输出（命令行工具）
  - `ansi_tools get_text` - 获取完整信息（JSON 格式）
  - `ansi_tools get_screen` - 获取屏幕矩阵（JSON 格式）
  - `ansi_tools colored` - 保留颜色输出
  - `ansi_tools lines` - 获取纯文本（含换行）
  - `ansi_tools colors` - 分析颜色使用

## 参考资源

- pexpect 官方文档：https://pexpect.readthedocs.io/
- ANSI 转义码：https://en.wikipedia.org/wiki/ANSI_escape_code

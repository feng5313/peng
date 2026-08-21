# pexpect 使用示例

实际的代码示例和常见用例。

## 示例 1: 自动化Sudoku游戏

```python
import pexpect
import time
import subprocess

def play_sudoku():
    """自动化sudoku游戏并捕获屏幕"""
    # 1. 启动游戏
    child = pexpect.spawn('/usr/games/sudoku', args=['-cmedium'],
                          encoding='utf-8')

    # 2. 等待游戏加载
    time.sleep(3)

    # 3. 读取初始屏幕
    initial = child.read(size=20000)
    print(f"捕获初始输出: {len(initial)} 字符")

    # 4. 退出游戏
    child.sendline('q')
    child.close()

    # 5. 渲染屏幕（使用 ansi-tools 技能）
    screen = subprocess.run(
        ['python3', '/path/to/ansi_tools.py', 'render'],
        input=initial,
        capture_output=True,
        text=True
    ).stdout

    print("\n=== 游戏屏幕 ===\n")
    print(screen)

play_sudoku()
```

## 示例 2: 批量捕获多个程序

```python
def batch_capture(commands):
    """批量捕获多个程序的输出"""
    results = {}

    for cmd in commands:
        print(f"捕获: {cmd}")
        child = pexpect.spawn(cmd, encoding='utf-8')
        time.sleep(2)

        output = child.read(size=10000)
        results[cmd] = output

        child.sendline('q')
        child.close()

    return results

# 使用
programs = [
    '/usr/games/sudoku -cmedium',
    '/usr/games/sudoku -chard'
]

results = batch_capture(programs)
for cmd, output in results.items():
    print(f"{cmd}: {len(output)} 字符")
```

## 示例 3: 监控程序输出

```python
def monitor_program(command, duration=10):
    """持续监控程序输出"""
    child = pexpect.spawn(command, encoding='utf-8')

    start_time = time.time()
    while time.time() - start_time < duration:
        try:
            output = child.read(size=1000, timeout=1)
            if output:
                print(f"[{time.strftime('%H:%M:%S')}] {output[:100]}")
        except:
            break

    child.sendline('q')
    child.close()
```

## 示例 4: 交互式命令行工具自动化

```python
def interactive_session():
    """与程序进行交互式会话"""
    child = pexpect.spawn('mycli', encoding='utf-8')

    # 发送一系列命令
    commands = [
        ('status', 'Checking status...'),
        ('run', 'Starting...'),
        ('quit', 'Exiting...')
    ]

    for cmd, description in commands:
        print(f"\n{description}")
        child.sendline(cmd)
        time.sleep(0.5)
        response = child.read(size=1000)
        if response:
            print(f"响应: {response[:200]}")

    child.close()
```

## 示例 5: 捕获ncurses程序完整屏幕

```python
import pexpect
import time

def capture_ncurses_screen(command, wait_time=3):
    """捕获ncurses程序的完整屏幕"""
    # 启动程序
    child = pexpect.spawn(command, encoding='utf-8')

    # 等待程序启动和渲染
    time.sleep(wait_time)

    # 读取所有输出
    output = child.read(size=20000)

    # 退出程序
    child.sendline('q')
    child.close()

    return output

# 使用示例
raw = capture_ncurses_screen('/usr/games/sudoku -cmedium')
print(f"捕获 {len(raw)} 字符")

# 配合 ansi-tools 技能渲染
import subprocess
screen = subprocess.run(
    ['python3', '/path/to/ansi_tools.py', 'render'],
    input=raw,
    capture_output=True,
    text=True
).stdout

print(screen)  # 完整的可读屏幕
```

## 示例 6: 处理ANSI格式输出

```python
def capture_and_process(command):
    """捕获并处理ANSI格式的输出"""
    import subprocess

    # 步骤1: pexpect捕获原始输出
    child = pexpect.spawn(command, encoding='utf-8')
    time.sleep(3)
    raw = child.read(size=20000)
    child.sendline('q')
    child.close()

    # 步骤2: 使用 ansi-tools 技能处理ANSI序列
    result = subprocess.run(
        ['python3', '/path/to/ansi_tools.py', 'render'],
        input=raw,
        capture_output=True,
        text=True
    )

    # 步骤3: 返回可读屏幕
    return result.stdout
```

## 相关技能

- **ansi-tools** - 处理捕获的ANSI输出，渲染为可读屏幕

# pexpect 核心 API 参考

详细的 pexpect API 文档和函数签名。

## 1. spawn() - 启动程序

```python
import pexpect

# 基本用法
child = pexpect.spawn('/usr/games/sudoku')

# 带参数
child = pexpect.spawn('/usr/games/sudoku', args=['-cmedium'])

# 带编码
child = pexpect.spawn('/usr/games/sudoku', encoding='utf-8')

# 带timeout
child = pexpect.spawn('/usr/games/sudoku', timeout=30)

# 带环境变量
import os
env = os.environ.copy()
env['MY_VAR'] = 'value'
child = pexpect.spawn('program', env=env)
```

### spawn() 参数说明

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `command` | str | - | 要执行的命令 |
| `args` | list | [] | 命令参数列表 |
| `encoding` | str | None | 文本编码（如'utf-8'） |
| `timeout` | int | 30 | 默认超时时间（秒） |
| `env` | dict | None | 环境变量字典 |

## 2. read() - 读取输出

```python
# 读取指定大小
output = child.read(size=20000)

# 读取所有可用输出
output = child.read()

# 读取到EOF
output = child.read()
child.expect(pexpect.EOF)
```

### read() 参数说明

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `size` | int | -1 | 读取字节数（-1表示全部） |

## 3. send() - 发送输入

```python
# 发送单个字符
child.send('q')

# 发送字符串
child.send('hello world')

# 发送整行（自动添加换行）
child.sendline('input')

# 发送特殊按键
child.send('\x03')  # Ctrl+C
child.send('\x1b[A')  # 方向键上
child.send('\t')     # Tab
```

### send() 系列方法

| 方法 | 说明 |
|------|------|
| `send(s)` | 发送字符串 |
| `sendline(s)` | 发送字符串+换行符 |
| `sendcontrol(char)` | 发送控制字符（如'c'表示Ctrl+C） |
| `sendeof()` | 发送EOF |

## 4. expect() - 等待模式

```python
# 等待字符串
child.expect('Password:')
child.send('mypassword')

# 等待多个模式之一
index = child.expect(['success', 'error', 'timeout'])
if index == 0:
    print("成功")
elif index == 1:
    print("失败")

# 使用正则表达式
child.expect(r'Progress: (\d+)%')
percent = child.match.group(1)
print(f"完成度: {percent}%")

# 带timeout
try:
    child.expect('ready', timeout=10)
except pexpect.TIMEOUT:
    print("超时")
```

### expect() 参数说明

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `pattern` | str/list | - | 要匹配的模式（字符串或正则表达式） |
| `timeout` | int | 30 | 超时时间（秒） |

### expect() 异常

| 异常 | 说明 |
|------|------|
| `TIMEOUT` | 超时未匹配到模式 |
| `EOF` | 程序结束 |
| `Exception` | 其他错误 |

## 5. 程序控制

```python
# 检查程序是否运行
if child.isalive():
    print("程序运行中")

# 等待程序结束
child.wait()
exit_status = child.exitstatus
print(f"退出码: {exit_status}")

# 终止程序
child.terminate(force=True)

# 关闭连接
child.close()
```

### 控制方法

| 方法 | 说明 |
|------|------|
| `isalive()` | 检查进程是否运行 |
| `wait()` | 等待进程结束 |
| `terminate(force=False)` | 终止进程 |
| `close(force=False)` | 关闭连接 |

## 相关资源

- pexpect官方文档: https://pexpect.readthedocs.io/
- pexpect GitHub仓库: https://github.com/pexpect/pexpect

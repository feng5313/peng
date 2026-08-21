# pexpect 最佳实践

推荐的做法和常见错误避免。

## ✅ 推荐做法

### 1. 使用pexpect进行终端自动化

- ✅ 正确的stdout读取能力
- ✅ 简洁的API设计
- ✅ 适合交互式程序自动化

### 2. 设置合理的等待时间

```python
# ncurses程序需要更多时间渲染
time.sleep(3)
```

**原因**: ncurses程序初始化和屏幕渲染需要时间

### 3. 指定encoding参数

```python
child = pexpect.spawn('cmd', encoding='utf-8')
```

**好处**:
- 避免bytes和str混淆
- 简化文本处理
- 减少编码错误

### 4. 使用合适的read大小

```python
# 小输出
output = child.read(size=1000)

# 大输出/完整屏幕
output = child.read(size=50000)
```

**指导**:
- 小程序: 1000-5000
- ncurses程序: 20000-50000
- 大型TUI: 50000+

### 5. 妥善关闭连接

```python
try:
    # 操作...
    pass
finally:
    child.close()
```

**好处**: 确保资源释放，避免僵尸进程

### 6. 配合ANSI渲染器使用

```python
raw = child.read(size=20000)
screen = render_full_screen(raw)  # 使用 ansi-tools 技能
```

**说明**: 捕获的原始输出包含ANSI序列，需要渲染为可读文本

## ❌ 避免做法

### 1. 不要用原始pty期望读取stdout

**问题**:
- `os.read(fd)` 会返回 "Input/output error"
- PTY master fd连接到输入侧，不是输出侧

**解决**: 使用pexpect代替

### 2. 不要忘记等待程序启动

```python
# ❌ 错误
child = pexpect.spawn('cmd')
output = child.read()  # 可能为空

# ✅ 正确
child = pexpect.spawn('cmd')
time.sleep(2)  # 等待启动
output = child.read()
```

### 3. 不要忽略异常

```python
# ✅ 正确处理EOF
try:
    output = child.read(size=10000)
except pexpect.EOF:
    # 正确处理EOF
    break
```

### 4. 不要混淆bytes和str

```python
# ✅ 指定encoding避免混淆
child = pexpect.spawn('cmd', encoding='utf-8')
output = child.read()  # 现在是str
```

## 性能优化建议

### 1. 批量操作

```python
# 一次性发送多个命令
cmds = ['status', 'info', 'quit']
for cmd in cmds:
    child.sendline(cmd)
    time.sleep(0.5)
```

### 2. 合理设置timeout

```python
# 短操作
child.expect('ready', timeout=5)

# 长操作
child.expect('done', timeout=60)
```

### 3. 使用expect而非轮询

```python
# ❌ 低效轮询
while 'result' not in child.read():
    time.sleep(0.1)

# ✅ 高效expect
child.expect('result')
```

## 调试技巧

### 1. 打印调试信息

```python
child = pexpect.spawn('cmd', encoding='utf-8')
child.logfile_read = sys.stdout  # 打印所有输出
```

### 2. 检查进程状态

```python
print(f"PID: {child.pid}")
print(f"Is alive: {child.isalive()}")
print(f"Exit status: {child.exitstatus}")
```

### 3. 捕获完整输出用于调试

```python
output = child.read(size=100000)
with open('/tmp/debug_output.txt', 'w') as f:
    f.write(output)
```

## 相关资源

- pexpect官方文档: https://pexpect.readthedocs.io/

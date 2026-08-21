# pexpect 开发经验

## ncurses/TUI 程序自动化

### 核心要点

1. **必须设置 dimensions**
   ```python
   child = pexpect.spawn('/usr/games/sudoku',
                         encoding='utf-8',
                         dimensions=(24, 80))  # 行, 列
   ```

2. **使用 read_nonblocking 读取输出**
   ```python
   # ncurses 程序持续输出，read() 会阻塞
   try:
       raw = child.read_nonblocking(size=50000, timeout=1)
   except:
       raw = child.before or ''
   ```

3. **按键后需要延迟**
   ```python
   child.send('j')
   time.sleep(0.005)  # 5ms 延迟，让程序响应
   ```

### 光标位置跟踪

ncurses 程序的光标位置是关键信息：

```python
from ansi_tools import get_text

def get_cursor_position(child, width=80, height=24):
    # 发送刷新命令
    child.send('d')
    time.sleep(0.2)

    # 读取输出
    raw = child.read_nonblocking(size=50000, timeout=1)

    # 使用 ansi-tools 解析光标位置
    result = get_text(raw, width, height)
    return result['cursor']['position']  # [x, y]
```

### 棋盘类游戏自动化模式

```python
class BoardGameAutoPlayer:
    def __init__(self, cmd, width=80, height=24):
        self.child = pexpect.spawn(cmd,
                                   encoding='utf-8',
                                   dimensions=(height, width))
        self.width = width
        self.height = height

    def read_screen(self):
        """读取屏幕内容"""
        try:
            self.child.read_nonblocking(size=50000, timeout=0.1)
        except:
            pass
        self.child.send('d')  # 刷新
        time.sleep(0.2)
        return self.child.read_nonblocking(size=50000, timeout=1)

    def get_cursor(self):
        """获取光标位置"""
        raw = self.read_screen()
        result = get_text(raw, self.width, self.height)
        return result['cursor']['position']

    def move_to(self, target_row, target_col):
        """移动到指定位置"""
        current = self.get_cursor()
        # 计算移动...

    def close(self):
        """关闭程序"""
        if self.child and self.child.isalive():
            self.child.send('q')
            time.sleep(0.1)
            self.child.close()
```

## 常见问题与解决

### 1. read() 阻塞
**原因**: ncurses 程序持续输出，read() 等待 EOF

**解决**: 使用 `read_nonblocking(size, timeout)`

### 2. 屏幕内容不完整
**原因**: 读取太快，程序还没输出完

**解决**:
```python
# 先清空缓冲区
try:
    child.read_nonblocking(size=50000, timeout=0.1)
except:
    pass

# 发送刷新命令
child.send('d')
time.sleep(0.2)

# 再读取
raw = child.read_nonblocking(size=50000, timeout=1)
```

### 3. 按键丢失
**原因**: 按键发送太快，程序来不及响应

**解决**:
```python
# 每个按键后延迟
for key in keys:
    child.send(key)
    time.sleep(0.005)

# 或者批量发送后统一延迟
child.send(''.join(keys))
time.sleep(0.05)
```

### 4. 光标位置不准
**原因**: 刷新屏幕改变了光标位置

**解决**:
- 只在开始时获取一次光标位置
- 自己跟踪光标移动，不频繁刷新

## 调试技巧

### 1. 启用日志
```python
import sys
child.logfile_read = sys.stdout  # 打印所有读取内容
child.logfile_send = sys.stdout  # 打印所有发送内容
```

### 2. 保存原始输出
```python
def save_debug_output(raw, filename='/tmp/debug_ansi.txt'):
    with open(filename, 'w') as f:
        f.write(repr(raw))  # 使用 repr 保留转义字符
```

### 3. 检查进程状态
```python
print(f"PID: {child.pid}")
print(f"Alive: {child.isalive()}")
print(f"Exit status: {child.exitstatus}")
```

### 4. 分析 ANSI 序列
```bash
# 十六进制查看
xxd output.txt | head -20

# 显示转义字符
cat -v output.txt
```

## 性能优化

### 1. 减少刷新次数
```python
# 不好的做法：每次移动后刷新
for i in range(9):
    child.send('j')
    pos = get_cursor()  # 每次都刷新

# 好的做法：跟踪位置，只在开始时刷新
start_pos = get_cursor()
for i in range(9):
    child.send('j')
    current_pos = (start_pos[0] + i, start_pos[1])
```

### 2. 批量发送按键
```python
# 不好的做法
for _ in range(8):
    child.send('j')
    time.sleep(0.005)

# 好的做法
child.send('j' * 8)
time.sleep(0.04)
```

### 3. 使用最小延迟
```python
DELAY = 0.005  # 5ms 通常足够
```

## 与 ansi-tools 配合

```python
from ansi_tools import get_text, text_lines, colored_lines

# 获取完整信息
result = get_text(raw, width, height)
text = result['text']              # 纯文本
positions = result['positions']    # 字符位置
colors = result['colors']          # 字符颜色
cursor = result['cursor']          # 光标位置

# 只需要文本
text = text_lines(raw, width, height)

# 保留颜色
colored = colored_lines(raw, width, height)
```

## 相关参考

- pexpect 文档: https://pexpect.readthedocs.io/
- ansi-tools skill: 配合处理 ANSI 输出
- ncurses 程序特点: 持续输出、光标跟踪、刷新机制

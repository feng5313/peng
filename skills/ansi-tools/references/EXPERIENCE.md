# ansi-tools 开发经验

## 版本历史

### v3.1 (2024-02-14)
- 新增 REP 控制码 (`\x1b[#b`) 支持
- 修复 ncurses 程序边框显示异常问题

## 已支持的 ANSI 控制码

### CSI 序列 (已支持)
| 命令 | 功能 | 说明 |
|------|------|------|
| H, f | 光标定位 | `\x1b[row;colH` |
| A | 光标上移 | `\x1b[nA` |
| B | 光标下移 | `\x1b[nB` |
| C | 光标右移 | `\x1b[nC` |
| D | 光标左移 | `\x1b[nD` |
| G | 光标列定位 | `\x1b[colG` |
| d | 光标行定位 | `\x1b[rowd` |
| s | 保存光标 | `\x1b[s` |
| u | 恢复光标 | `\x1b[u` |
| m | SGR 样式 | `\x1b[...m` |
| J | 清屏 | `\x1b[nJ` |
| K | 清行 | `\x1b[nK` |
| **b** | **REP 重复** | `\x1b[nb` 重复前一个字符 n 次 |

### REP 控制码详解

REP (Repeat) 控制码用于重复前一个打印的字符：

```
输入: "+-\x1b[6b"
展开: "+------"
解释: '-' 重复 6 次
```

**实现要点**：
1. 在解析函数中维护 `last_char` 变量
2. 遇到普通可打印字符时更新 `last_char`
3. 遇到 REP 命令时，将 `last_char` 重复指定次数写入屏幕矩阵

```python
# 核心实现
elif cmd == 'b':
    count = params[0] if params and params[0] is not None else 1
    for _ in range(count):
        # 将 last_char 写入当前位置
        # 更新光标位置
```

## 常见问题与解决

### 1. 边框显示异常
**现象**: ncurses 程序的边框显示为 `+-+-+-+` 而非 `+-------+-------+-------+`

**原因**: 程序使用 REP 控制码减少输出数据量，但 ansi-tools 未处理

**解决**: 在 `_parse_ansi_to_screen()` 函数中添加 REP 命令支持

### 2. 字符重复不正确
**注意**: REP 重复的是**前一个打印字符**，不是前一个字节

**示例**:
- `+-\x1b[6b` → `+------` (重复 `-`)
- `A\x1b[3b` → `AAAA` (重复 `A`)

## 使用建议

### 1. 直接使用，无需预处理
```python
from ansi_tools import text_lines, colored_lines

# REP 控制码自动处理
result = text_lines(raw_output, 80, 24)
```

### 2. 检测 REP 控制码
```python
# 检查是否包含 REP 控制码
def has_rep_code(text):
    import re
    return bool(re.search(r'\x1b\[\d+b', text))
```

### 3. 调试 REP 展开
```python
# 对比展开前后
from ansi_tools import text_lines

lines = text_lines(raw, 80, 24).split('\n')
for i, line in enumerate(lines):
    if '+-' in line:
        print(f"边框行 {i}: {line}")
```

## 测试用例

```python
def test_rep_code():
    from ansi_tools import text_lines

    # 测试基本 REP
    raw = '\x1b[H\x1b[J+-\x1b[6b'
    result = text_lines(raw, 80, 24)
    assert result.startswith('+-------')

    # 测试多个 REP
    raw = '\x1b[H\x1b[J+-\x1b[6b+-\x1b[6b+'
    result = text_lines(raw, 80, 24)
    assert result.startswith('+-------+-------+')

    print("REP 控制码测试通过!")
```

## 相关参考

- ANSI REP 控制码: ECMA-48 标准
- ncurses 优化: 使用 REP 减少输出数据量
- 终端仿真: 需要正确处理所有 CSI 序列

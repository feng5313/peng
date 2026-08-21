# bin/test 使用文档

**版本**: 1.0
**创建时间**: 2026-03-14
**用途**: 运行一切命令，保证不死等、不忽略报错

---

## 简介

`bin/test` 是鹏项目的通用测试运行器，专门用于解决测试中的死等和报错被忽略问题。

**核心特性**:
- ✅ 防死等机制（超时自动终止）
- ✅ 实时监控输出
- ✅ 自动检测错误
- ✅ 完整的错误捕获
- ✅ 进程管理（记录PID、支持kill）
- ✅ 日志记录

---

## 使用方法

### 基本语法

```bash
./bin/test <命令> [选项]
```

### 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `command` | 要运行的命令（必须） | - |
| `--timeout` | 超时时间（秒） | 120 |
| `--monitor-interval` | 监控间隔（秒） | 5 |
| `--show-output` | 运行结束后显示完整输出 | false |

---

## 使用示例

### 1. 运行简单命令

```bash
./bin/test "echo '测试'"
```

**输出**:
```
🚀 执行命令: echo '测试'
📝 输出文件: /tmp/test-output.log
📝 PID文件: /tmp/test.pid
⏱️  超时时间: 120秒

✅ 进程已启动，PID: xxxxx
✅ 使用 'kill -9 xxxxx' 终止进程
✅ 输出实时监控中...

📊 进程已结束，退出码: 0
...
```

---

### 2. 运行pytest测试

```bash
./bin/test "pytest tests/unit/" --timeout 300 --monitor-interval 10
```

**说明**:
- 超时时间: 300秒（5分钟）
- 监控间隔: 10秒

---

### 3. 运行fly服务

```bash
./bin/test "bin/fly service" --timeout 600
```

**说明**:
- 运行fly service命令
- 超时时间: 600秒（10分钟）

---

### 4. 测试超时自动终止

```bash
./bin/test "sleep 60" --timeout 5
```

**输出**:
```
...
⏱️  已运行 5 秒...

⚠️  超时！已运行 5 秒
🔪 正在终止进程 (PID: xxxxx)...
✅ 进程已终止
...
```

---

### 5. 运行代码并查看完整输出

```bash
./bin/test "python -c 'print(\"测试\")'" --show-output
```

**输出**:
```
...
======================================================================
📝 完整输出
======================================================================

测试
```

---

## 错误检测

`bin/test` 会自动检测以下错误关键词：

- error, exception, traceback
- failed, failure
- ValueError, TypeError, AttributeError
- KeyError, IndexError, RuntimeError
- OSError, IOError, AssertionError
- ❌, 错误, 异常, 失败

**示例**:

```bash
./bin/test "python -c 'raise ValueError(\"错误\")'"
```

**输出**:
```
🔍 检测到错误: Traceback (most recent call last):
🔍 检测到错误: ValueError:
...
❌ 检测到 X 个错误:
  1. Traceback (most recent call last):
  2. ValueError:
```

---

## 进程管理

### 查看PID

```bash
cat /tmp/test.pid
```

### 终止进程

```bash
kill -9 $(cat /tmp/test.pid)
```

### 查看输出

```bash
tail -100 /tmp/test-output.log
```

---

## 防死等机制

`bin/test` 的防死等机制包括：

1. **超时自动终止**: 超过指定时间自动kill进程
2. **实时监控**: 定期检查进程状态
3. **强制杀死**: 超时后先尝试kill，超时后强制SIGKILL

**示例**:

```bash
# 运行死循环命令，10秒后自动终止
./bin/test "tail -f /dev/null" --timeout 10
```

---

## 完整测试流程

### 1. 快速测试

```bash
./bin/test "pytest tests/unit/core/task/test_types.py" --timeout 60
```

### 2. 完整测试

```bash
./bin/test "pytest tests/unit/" --timeout 600 --monitor-interval 30
```

### 3. 查看结果

```bash
# 查看输出文件
tail -200 /tmp/test-output.log

# 查看PID（如果进程还在运行）
cat /tmp/test.pid

# 终止进程
kill -9 $(cat /tmp/test.pid)
```

---

## 常见问题

### Q1: 为什么使用 `./bin/test` 而不是 `bin/test`？

A: 在大多数系统中，需要使用 `./bin/test` 或 `python bin/test` 来执行脚本。

### Q2: 超时时间设置多少合适？

A: 根据事务类型设置：
- 单个测试: 60秒
- 测试套件: 300-600秒
- 长时间事务: 1800秒（30分钟）或更多

### Q3: 如何监控长时间运行的事务？

A: 使用较短的监控间隔，并定期查看输出文件：

```bash
./bin/test "bin/fly service" --timeout 3600 --monitor-interval 30

# 另一个终端监控输出
tail -f /tmp/test-output.log
```

### Q4: 进程终止失败怎么办？

A: 手动强制杀死进程：

```bash
# 查看进程
ps aux | grep "bin/test"

# 强制杀死
kill -9 <PID>
```

---

## 技术细节

### 实现特性

1. **后台运行**: 使用 `subprocess.Popen` 后台运行
2. **实时监控**: 独立线程读取输出
3. **超时检测**: 定期检查进程状态
4. **错误检测**: 关键词匹配
5. **日志记录**: 写入 `/tmp/test-output.log`
6. **进程管理**: 记录PID到 `/tmp/test.pid`

### 输出文件

- `/tmp/test-output.log` - 完整输出
- `/tmp/test.pid` - 进程PID

---

## 开发说明

### 修改脚本

```bash
# 编辑脚本
vim bin/test

# 测试修改
./bin/test "echo '测试'"

# 提交修改
git add bin/test
git commit -m "fix: 修复bin/test的某个问题"
```

### 添加新特性

1. 在 `bin/test` 中添加新功能
2. 更新本文档
3. 编写测试用例
4. 提交修改

---

## 参考资料

- AGENTS.md - 测试铁律
- docs/refine-llm-test.md - 测试验收文档
- .peng-mem/ISSUE_DISCOVERY_PROCESS.md - 问题发现流程

---

**最后更新**: 2026-03-14

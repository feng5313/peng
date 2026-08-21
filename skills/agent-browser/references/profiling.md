# 性能分析

在浏览器自动化期间捕获 Chrome DevTools 性能分析文件用于性能分析。

**相关文档**: [commands.md](commands.md) 完整命令参考, [SKILL.md](../SKILL.md) 快速入门。

## 目录

- [基本性能分析](#基本性能分析)
- [分析器命令](#分析器命令)
- [类别](#类别)
- [使用场景](#使用场景)
- [输出格式](#输出格式)
- [查看性能分析](#查看性能分析)
- [限制](#限制)

## 基本性能分析

```bash
# 开始性能分析
agent-browser profiler start

# 执行操作
agent-browser navigate https://example.com
agent-browser click "#button"
agent-browser wait 1000

# 停止并保存
agent-browser profiler stop ./trace.json
```

## 分析器命令

```bash
# 使用默认类别开始性能分析
agent-browser profiler start

# 使用自定义追踪类别开始
agent-browser profiler start --categories "devtools.timeline,v8.execute,blink.user_timing"

# 停止性能分析并保存到文件
agent-browser profiler stop ./trace.json
```

## 类别

`--categories` 标志接受逗号分隔的 Chrome 追踪类别列表。默认类别包括：

- `devtools.timeline` -- 标准 DevTools 性能追踪
- `v8.execute` -- 运行 JavaScript 所花费的时间
- `blink` -- 渲染器事件
- `blink.user_timing` -- `performance.mark()` / `performance.measure()` 调用
- `latencyInfo` -- 输入到延迟跟踪
- `renderer.scheduler` -- 事务调度和执行
- `toplevel` -- 广泛的基本事件

还包括几个 `disabled-by-default-*` 类别用于详细的时间线、调用栈和 V8 CPU 性能分析数据。

## 使用场景

### 诊断缓慢的页面加载

```bash
agent-browser profiler start
agent-browser navigate https://app.example.com
agent-browser wait --load networkidle
agent-browser profiler stop ./page-load-profile.json
```

### 分析用户交互

```bash
agent-browser navigate https://app.example.com
agent-browser profiler start
agent-browser click "#submit"
agent-browser wait 2000
agent-browser profiler stop ./interaction-profile.json
```

### CI 性能回归检查

```bash
#!/bin/bash
agent-browser profiler start
agent-browser navigate https://app.example.com
agent-browser wait --load networkidle
agent-browser profiler stop "./profiles/build-${BUILD_ID}.json"
```

## 输出格式

输出是 Chrome Trace Event 格式的 JSON 文件：

```json
{
  "traceEvents": [
    { "cat": "devtools.timeline", "name": "RunTask", "ph": "X", "ts": 12345, "dur": 100, ... },
    ...
  ],
  "metadata": {
    "clock-domain": "LINUX_CLOCK_MONOTONIC"
  }
}
```

`metadata.clock-domain` 字段根据主机平台设置（Linux 或 macOS）。在 Windows 上省略。

## 查看性能分析

在以下任一工具中加载输出 JSON 文件：

- **Chrome DevTools**: Performance 面板 > 加载性能分析 (Ctrl+Shift+I > Performance)
- **Perfetto UI**: https://ui.perfetto.dev/ -- 拖放 JSON 文件
- **Trace Viewer**: 任意 Chromium 浏览器中的 `chrome://tracing`

## 限制

- 仅适用于基于 Chromium 的浏览器（Chrome、Edge）。不支持 Firefox 或 WebKit。
- 性能分析活动时，追踪数据在内存中累积（上限为 500 万事件）。在关注区域结束后及时停止性能分析。
- 停止时的数据收集有 30 秒超时。如果浏览器无响应，停止命令可能会失败。

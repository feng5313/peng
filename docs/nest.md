# Nest（鹏窝）系统设计与使用说明

## 1. 概述

Nest（鹏窝）是鹏的后台控制服务，运行在远程 PC 上，通过 WebSocket 连接到 peng 后端，实现对远程 PC 的文件系统、命令行、窗口管理和键鼠输入的完整控制。

### 核心设计原则

- **Nest 只主动连后端，不监听端口**：nest 是纯 WebSocket client，不暴露任何端口
- **Nest 不直接和 WebUI 通信**：所有通信通过 peng 后端中转
- **多实例支持**：一台 PC 可以运行多个 nest 实例，连接不同的 peng 后端
- **自动重连**：断线后按配置间隔自动重连
- **安全控制**：白名单/黑名单机制保护远程操作安全
- **CLI 远程模式**：bin/task 和 bin/msg 支持通过 WebSocket 连接远程 peng service 执行命令

### 架构图

```
A电脑(nest client) ──WebSocket──> B电脑(peng backend :8899)
C电脑(nest client) ──WebSocket──> B电脑(peng backend :8899)
                                   B电脑(peng tools) 通过 bridge 转发命令到已连接的 nest

A电脑(bin/task, bin/msg) ──WebSocket──> C电脑(peng backend :8899) ──本地执行──> task/msg --local
  （CLI远程模式：读取config/nest.json的default nest配置，连接远程peng service执行命令）
```

---

## 2. 系统架构

### 2.1 组件概览

| 组件 | 文件 | 角色 | 运行位置 |
|------|------|------|---------|
| NestConnector | `src/peng/nest/server.py` | WebSocket 客户端，主动连接 peng 后端 | 远程 PC |
| NestBridgeManager | `src/peng/nest/bridge.py` | WebSocket 服务端，管理来自 nest 的连接 | peng 后端 |
| LocalExecutor | `src/peng/nest/local.py` | 本地执行器，执行 nest 收到的命令 | 远程 PC |
| NestSecurity | `src/peng/nest/security.py` | 安全控制（白名单/黑名单） | 远程 PC |
| protocol | `src/peng/nest/protocol.py` | 消息协议定义（NestRequest/NestResponse） | 双端共用 |
| client.py | `src/peng/nest/client.py` | peng 端发送命令的客户端接口 | peng 后端 |
| cli.py | `src/peng/nest/cli.py` | 命令行界面（install/uninstall/start/stop/default等） | 远程 PC |
| remote.py | `src/peng/cli/remote.py` | CLI 远程模式（bin/task、bin/msg 共享） | 任意 PC |
| bin/nest | `bin/nest` | nest 入口脚本 | 远程 PC |

### 2.2 通信架构

```
                        peng 后端 (B电脑)
                    ┌──────────────────────────┐
                    │  NestBridgeManager       │
                    │  WebSocket Server (:8899) │
                    │         │                │
                    │    bridge.py             │
                    │         │                │
                    │    client.py ←── tools   │
                    │    (send_request)        │
                    └──────┬──────────┬────────┘
                           │          │
              WebSocket    │          │   WebSocket
               (register)  │          │   (register)
                           │          │
              ┌────────────┴──┐  ┌───┴────────────┐
              │  NestConnector│  │  NestConnector  │
              │  (server.py)  │  │  (server.py)    │
              │      │        │  │      │          │
              │ LocalExecutor │  │ LocalExecutor   │
              │ (local.py)    │  │ (local.py)      │
              │    A电脑      │  │    C电脑        │
              └───────────────┘  └────────────────┘
```

### 2.3 请求处理流程

```
peng tools → client.send_request() → NestBridgeManager.get_connection()
  → NestConnection.send_request() → WebSocket → NestConnector
  → LocalExecutor.handle_request() → 本地执行 → 响应原路返回
```

子进程模式（TaskRunner 创建的独立进程）：
```
子进程 tool → client._forward_via_server()
  → WebSocket → localhost:8899 → NestBridgeManager
  → 转发到目标 nest → 响应原路返回
```

---

## 3. 消息协议

### 3.1 请求格式（NestRequest）

```json
{
    "id": "uuid-string",
    "type": "bash|read|write|edit|stat|list_dir|glob|grep|list_windows|screenshot|input|ping|terminal_create|terminal_write|terminal_resize|terminal_close|load_skill|set_workdir|cli_command",
    "params": { ... }
}
```

### 3.2 响应格式（NestResponse）

```json
{
    "id": "uuid-string",
    "type": "result|error|stream",
    "data": ...,
    "error": "...",
    "done": true
}
```

### 3.3 支持的请求类型（19种）

| 类型 | 说明 | 参数 |
|------|------|------|
| `ping` | 心跳检测 | 无 |
| `bash` | 执行 shell 命令 | `command`, `cwd`, `timeout`, `env` |
| `read` | 读取文件/目录 | `path`, `offset`, `limit` |
| `write` | 写入文件 | `path`, `content` |
| `edit` | 精确替换文件内容 | `path`, `old_string`, `new_string`, `replace_all` |
| `stat` | 获取文件信息 | `path` |
| `list_dir` | 列出目录 | `path` |
| `glob` | Glob 模式匹配 | `pattern`, `path` |
| `grep` | 正则搜索 | `pattern`, `path`, `glob`, `ignore_case`, `max_matches` |
| `list_windows` | 列出窗口 | `query` |
| `screenshot` | 截屏 | `window`, `region`, `format` |
| `input` | 鼠标/键盘输入 | `actions`, `window`, `activate` |
| `load_skill` | 加载技能文件 | `path` |
| `terminal_create` | 创建 PTY 终端（仅 Linux） | `shell`, `cols`, `rows`, `env` |
| `terminal_write` | 写入终端 | `session_id`, `data` |
| `terminal_resize` | 调整终端大小 | `session_id`, `cols`, `rows` |
| `terminal_close` | 关闭终端 | `session_id` |
| `set_workdir` | 设置工作目录（host→nest） | `workdir`, `nest_name` |
| `cli_command` | 远程执行 CLI 命令 | `command`, `args` |

### 3.4 注册协议

nest 连接 peng 后端时，先发送注册消息：

```json
{
    "type": "register",
    "name": "local",
    "info": {
        "name": "local",
        "platform": "Windows",
        "hostname": "epyc",
        "pid": 12345,
        "project_root": "D:\\work\\peng",
        "workdir": "D:\\work\\peng",
        "has_desktop": true,
        "session_id": 1
    }
}
```

peng 后端回复：
```json
{
    "type": "registered",
    "name": "local"
}
```

### 3.5 子进程转发协议

子进程连接 peng 后端时，发送转发请求：

```json
{
    "type": "forward_request",
    "request": { "id": "...", "type": "bash", "params": {...} },
    "timeout": 120.0,
    "nest_name": "local"
}
```

### 3.6 CLI 远程执行协议

bin/task 或 bin/msg 通过 WebSocket 连接远程 peng 后端时，发送 CLI 命令请求：

```json
{
    "type": "cli_command",
    "command": "task",
    "args": ["tree", "1"]
}
```

peng 后端收到后，在本地以 `--local` 模式执行对应命令，返回结果：

```json
{
    "type": "result",
    "stdout": "...",
    "stderr": "...",
    "returncode": 0
}
```

---

## 4. 安全控制（NestSecurity）

安全控制在 nest 端的 LocalExecutor 中执行，客户端无需关心安全策略。

### 4.1 规则

| 规则 | 说明 |
|------|------|
| dir_whitelist | 非空时，只允许访问白名单中的目录 |
| dir_blacklist | 禁止访问黑名单中的目录 |
| cmd_whitelist | 非空时，只允许执行白名单中的命令 |
| cmd_blacklist | 禁止执行黑名单中的命令 |
| enable_window_query | 是否允许窗口查询 |
| enable_screenshot | 是否允许截屏 |
| enable_input | 是否允许远程键鼠输入 |

### 4.2 WebUI 文件/终端操作

WebUI 远程文件和终端操作通过 `_skip_security=True` 参数跳过安全检查，允许用户通过文件管理器访问任意目录。

---

## 5. 配置文件

### 5.1 config/nest.json（nest 独立配置）

```json
{
    "default": "epyc",
    "instances": {
        "epyc": {
            "name": "epyc",
            "host": "vnas",
            "port": 8899,
            "reconnect_interval": 5,
            "workdir": "D:\\work\\peng",
            "security": {
                "dir_whitelist": [],
                "dir_blacklist": [],
                "cmd_whitelist": [],
                "cmd_blacklist": [],
                "enable_window_query": true,
                "enable_screenshot": true,
                "enable_input": true
            }
        },
        "vnas": {
            "name": "vnas",
            "host": "epyc",
            "port": 8899,
            "reconnect_interval": 5,
            "workdir": "/root/work/peng"
        }
    }
}
```

| 字段 | 说明 |
|------|------|
| **default** | **默认 nest 实例名称**，用于 CLI 远程模式（bin/task、bin/msg） |
| instances | nest 实例列表，key 为实例名称 |
| instances.\<name\>.name | 显示名称 |
| instances.\<name\>.host | peng 后端地址 |
| instances.\<name\>.port | peng 后端端口（默认 8899） |
| instances.\<name\>.reconnect_interval | 重连间隔（秒，默认 5） |
| instances.\<name\>.workdir | 工作目录（双向同步） |
| instances.\<name\>.security | 安全控制配置 |

### 5.2 config/webui.json（WebUI 配置）

```json
{
    "webui": {
        "workdir": "D:\\work\\peng",
        "nest": "local"
    }
}
```

`config.webui.nest` 与 WebUI 前端 nest 下拉框双向同步。

---

## 6. CLI 远程模式（bin/task、bin/msg）

### 6.1 概述

bin/task 和 bin/msg 支持远程模式，通过 WebSocket 连接到 config/nest.json 中默认 nest 所配置的 peng service，在远程执行命令并返回结果。

这允许从任一台 PC 上运行 `task/msg` 命令操作另一台 PC 上的事务和消息数据。

### 6.2 模式判定流程

```
bin/task 或 bin/msg 启动
  │
  ├── 有 --local 参数？ ──→ 本地模式（直接执行，不连远程）
  │
  ├── config/nest.json 有 default 字段？
  │   └── 无 → 本地模式
  │
  ├── default 对应的实例存在？
  │   └── 不存在 → 本地模式
  │
  ├── host 是 localhost/127.0.0.1？
  │   └── 是 → 本地模式（连接自己无意义）
  │
  ├── WebSocket 连接 host:port（3秒超时）
  │   └── 失败 → 本地模式（静默回退）
  │
  └── 远程模式：发送 cli_command → 接收结果 → 输出
```

### 6.3 核心代码

| 文件 | 说明 |
|------|------|
| `src/peng/cli/remote.py` | 远程模式检测和通信逻辑（`try_remote_mode()`） |
| `bin/task` | 入口脚本，启动时尝试远程模式，失败则回退本地 |
| `bin/msg` | 入口脚本，同上 |
| `src/peng/nest/bridge.py` | `_handle_cli_command()` 处理远程 CLI 执行请求 |

### 6.4 使用示例

```bash
# 远程模式：显示默认 nest 对应的远程 peng service 上的事务树
bin/task tree 1

# 强制本地模式
bin/task --local tree 1

# 远程消息列表
bin/msg list

# 强制本地消息列表
bin/msg --local list
```

### 6.5 注意事项

- 远程模式在 PostgreSQL 初始化之前判定，避免不必要的数据库连接
- `--local` 参数在入口脚本层面提取并移除，不会传递给 argparse
- 连接失败时**静默回退**到本地模式，不输出错误信息
- 远程执行时，peng service 用 `--local` 模式在本地执行命令，避免递归远程

---

## 7. Nest 上下文传递

### 7.1 数据库层

messages 和 tasks 表均有 `nest TEXT DEFAULT ''` 字段，让消息和事务携带 nest 上下文。

### 7.2 数据流

#### 新建消息

```
WebUI 前端 (nest from dropdown) 
  → POST /api/messages {nest: "local"} 
  → MessageManager.create_message(nest="local") 
  → MessageDatabase.send_message(nest="local") 
  → INSERT INTO messages (..., nest) VALUES (..., "local")
```

#### 新建事务

```
TaskDatabase.create_task(message_id=X)
  → 查询 messages.nest → "local"
  → INSERT INTO tasks (..., nest) VALUES (..., "local")
```

继承优先级：显式传入 > 关联消息 > 父事务。

#### 工具执行

```
Task.nest = "local"
  → agent.py set_context(nest=task.get("nest"))
  → ToolRegistry._context["nest"] = "local"
  → BashTool._get_nest_client()
    → client.set_nest_name(get_context("nest"))
    → client.bash(..., nest_name="local")
    → send_request(..., nest_name="local")
    → _get_connection("local")
    → NestBridgeManager.get_connection("local")
    → 发送到正确的远程 nest
```

#### WebUI 选中同步

```
用户点击消息/事务 → selectMessage/selectTask
  → 从 msg.nest/task.nest 读取值
  → _syncNestDropdown(nestName)
  → 更新下拉框选中值并触发 change 事件
  → POST /api/nest/select 同步后端状态
```

### 7.3 初始化

- `BackgroundService.start()` 中调用 `init_nest_clients()` 初始化主进程
- `executor.py` 的 `run_task_process()` 中调用 `init_nest_clients()` 初始化子进程
- 每个子进程有独立的 Python 全局变量空间，必须独立初始化

---

## 8. WebUI Nest 选择器

### 8.1 前端 UI

在 WebUI 工具栏的帮助按钮和工作目录之间显示 nest 下拉框。下拉框始终显示（即使只有 1 个 nest）。

### 8.2 后端 API

| API | 说明 |
|-----|------|
| `GET /api/nest/list` | 列出所有 nest（含连接状态），返回默认 nest |
| `POST /api/nest/select` | 切换当前 nest，同时更新 config.webui.nest |
| `POST /api/nest/set-workdir` | 设置远程 nest 的 workdir（host → nest 方向） |
| `GET /api/get-workdir` | 获取工作目录（支持远程 nest） |
| `POST /api/set-workdir` | 设置工作目录（支持远程 nest） |

### 8.3 数据来源

/api/nest/list 的数据来源于两处：
1. **动态连接**：NestBridgeManager 管理的已连接 nest（有 connected=true）
2. **静态配置**：config 中定义但未连接的 nest（connected=false）

同名 nest 自动加 hostname 后缀区分（如 `local/epyc`）。

---

## 9. Workdir 双向同步

### 9.1 同步机制

```
                      WebUI 工作目录输入框
                            │
                    ┌───────┴───────┐
                    │               │
            nest 下拉框="local"   nest 下拉框="remote"
            (本地模式)            (远程模式)
                    │               │
            本地 config/webui.json   远程 config/nest.json
                    │               │
                    │    ┌──────────┘
                    │    │  两个方向：
                    │    │
              ┌─────┴────┴─────┐
              │                 │
        host → nest         nest → host
    (用户在WebUI修改)    (远程nest配置变化)
              │                 │
    /api/set-workdir      NestConnector
    或                    检测config/nest.json变化
    /api/nest/set-workdir    → 重新注册
              │                 │
    _set_remote_workdir()  _read_workdir_from_config()
    发送set_workdir请求     检测workdir变化
              │                 │
    LocalExecutor           bridge.py 更新
    ._handle_set_workdir()  conn.info["workdir"]
              │
    写入远程
    config/nest.json
```

### 9.2 Host → Nest（用户在 WebUI 修改工作目录）

1. 用户在 WebUI 工作目录输入框修改路径
2. 前端调用 `/api/set-workdir`（或 `/api/nest/set-workdir`）
3. 后端检测到是远程 nest 模式
4. 通过 NestConnection 发送 `set_workdir` 请求到远程 nest
5. 远程 nest 的 LocalExecutor 收到请求，将 workdir 写入 config/nest.json
6. 同时更新本地 conn.info["workdir"] 缓存

### 9.3 Nest → Host（远程 nest 的 workdir 配置变化）

1. NestConnector 每次处理完消息后检查 config/nest.json 的 workdir
2. 如果 workdir 变化，发送新的 register 消息到 peng 后端
3. peng 后端 NestBridgeManager 更新 conn.info["workdir"]
4. WebUI 刷新时通过 /api/nest/list 获取最新的 workdir

---

## 10. Nest CLI 命令

### 10.1 子命令

| 命令 | 说明 |
|------|------|
| `nest install [--name NAME] [--host HOST:PORT]` | 安装并启动 nest 服务 |
| `nest uninstall [--name NAME]` | 停止并卸载 nest 服务 |
| `nest default --name NAME` | 设置默认 nest 实例（写入 config/nest.json 的 default 字段） |
| `nest start` | 启动 nest 服务 |
| `nest stop` | 停止 nest 服务 |
| `nest restart` | 重启 nest 服务 |
| `nest status` | 查看 nest 状态 |
| `nest connect [--name NAME] [--host HOST] [--port PORT]` | 前台连接（调试用） |

### 10.2 default 子命令

`nest default --name <name>` 在 config/nest.json 中设置 `"default": "<name>"` 字段。

default 字段的作用：
- **CLI 远程模式**：bin/task 和 bin/msg 读取 default 配置，连接对应的 peng service
- `nest install` 成功后自动设置 default 为当前安装的实例

```bash
# 设置默认 nest
nest default --name epyc

# 查看当前配置
cat config/nest.json
# { "default": "epyc", "instances": { ... } }
```

### 10.3 多实例支持

- 每个实例使用独立的任务计划/systemd 服务
- 未指定 `--name` 时自动根据 host 命名
- Windows 任务名称: `PengNest-<name>`
- Linux 服务名称: `peng-nest-<name>`

### 10.4 平台差异

| 平台 | 服务管理方式 | 说明 |
|------|-------------|------|
| Windows | schtasks 任务计划程序 | 以用户身份运行（/ru Interactive），可访问桌面 |
| Linux | systemd user 服务 | 注册为 systemd user unit |

#### Windows 特殊说明

- 使用 schtasks 而非 nssm 的原因：nssm 注册的服务以 SYSTEM 身份运行在 Session 0，无法访问桌面（截屏/键鼠输入不可用）
- schtasks `/ru Interactive` 以当前登录用户身份运行，无需密码
- 优先使用 `pythonw.exe` 避免控制台窗口弹出
- 通过 UAC 提权创建 `/rl highest` 任务
- 任务计划使用 `/sc minute /mo 5` 每5分钟重复运行，nest 启动时检测同参数实例避免重复

#### 10.5 异常自重启机制

**peng service 本地 nest 健康监控：**

peng 后端启动本地 nest connect 后，会持续监控其健康状态：
- 每 30 秒检查一次进程存活状态和 WebSocket 连接状态
- 进程退出时自动重启
- 连接断开超过 120 秒（容忍重连时间）后自动重启

**远程 nest 异常自重启：**

| 平台 | 机制 | 说明 |
|------|------|------|
| Windows | schtasks 每5分钟重复运行 | 任务计划每5分钟触发，nest 启动时检测同参数实例 |
| Linux | systemd `Restart=on-failure` + `RestartSec=5` | 进程异常退出时 systemd 自动重启 |

**单实例检测（`_ensure_single_instance`）：**

nest connect 启动时，通过进程检测判断是否有同参数（--name/--host/--port）实例在运行：
- 有且正常 → 当前实例退出（避免重复启动）
- 无或有异常 → 杀掉异常进程，当前实例取代之

---

## 11. fly 命令集成

| 命令 | nest 相关行为 |
|------|--------------|
| `fly install` | 自动安装本地 nest（调用 `get_schtasks_install_commands_windows()` 获取 schtasks 命令） |
| `fly uninstall` | 自动卸载 nest（调用 `stop_service_windows()` 杀进程） |
| `fly start` | 启动所有服务（含 nest） |
| `fly stop` | 停止所有服务（含 nest） |
| `fly restart` | 重启所有服务（未安装则调用 install） |
| `fly status` | 报告 nest 服务状态 |

---

## 12. 工具集成

以下工具通过 nest 远程执行操作：

| 工具 | 文件 | nest 操作 |
|------|------|----------|
| bash | `src/peng/tools/bash.py` | 远程执行命令 |
| read/write/edit | `src/peng/tools/file.py` | 远程文件读写编辑 |
| glob | `src/peng/tools/glob.py` | 远程文件搜索 |
| grep | `src/peng/tools/grep.py` | 远程内容搜索 |
| window | `src/peng/tools/screen.py` | 远程窗口查询 |
| shot | `src/peng/tools/screen.py` | 远程截屏 |
| input | `src/peng/tools/screen.py` | 远程键鼠输入 |

所有工具通过 `_NestClientCompat` 兼容层调用 nest client。工具使用 `get_context('nest')` 获取当前事务关联的 nest 名称，通过 `set_nest_name()` 指定目标 nest。

---

## 13. 文件清单

### 新增文件

| 文件 | 说明 |
|------|------|
| `bin/nest` | nest 入口脚本 |
| `config/nest.json` | nest 独立配置 |
| `src/peng/nest/__init__.py` | 模块初始化 |
| `src/peng/nest/protocol.py` | WebSocket 消息协议（19种请求类型） |
| `src/peng/nest/security.py` | 安全控制（白名单/黑名单） |
| `src/peng/nest/local.py` | 本地执行器（命令/文件/窗口/截屏/输入/终端） |
| `src/peng/nest/server.py` | NestConnector（WebSocket 客户端 + 自动重连） |
| `src/peng/nest/client.py` | peng 端客户端接口 + _NestClientCompat 兼容层 |
| `src/peng/nest/bridge.py` | peng 端 NestBridgeManager + WebSocket 服务器 + CLI 远程执行 |
| `src/peng/nest/cli.py` | CLI 子命令（install/uninstall/start/stop/restart/status/connect/default） |
| `src/peng/nest/_cli_main.py` | CLI 入口 |
| `src/peng/cli/remote.py` | CLI 远程模式（bin/task、bin/msg 共享的远程通信模块） |
| `src/peng/tools/screen.py` | WindowTool + ShotTool + InputTool |

### 修改文件

| 文件 | 修改内容 |
|------|---------|
| `bin/task` | 添加 --local 参数和远程模式检测 |
| `bin/msg` | 添加 --local 参数和远程模式检测 |
| `src/peng/config/loader.py` | 添加 NestConfig + NestEndpointConfig |
| `src/peng/tools/bash.py` | 添加 nest 远程执行路径 |
| `src/peng/tools/file.py` | ReadTool/WriteTool/EditTool 添加 nest 路径 |
| `src/peng/tools/glob.py` | 添加 nest 远程 glob |
| `src/peng/tools/grep.py` | 添加 nest 远程 grep |
| `src/peng/tools/registry.py` | 注册 WindowTool + ShotTool + InputTool |
| `src/peng/ui/fly.py` | fly install/uninstall/start/stop/status 集成 nest |
| `src/peng/ui/service.py` | BackgroundService 启动 nest server（8899端口） |
| `src/peng/task/executor.py` | 子进程初始化 nest forward 模式 |
| `src/peng/db/init_postgres_db.py` | messages/tasks 表添加 nest 字段 |
| `src/peng/webui/html/index.html` | nest 下拉框 |
| `src/peng/webui/html/static/css/style.css` | nest-select 样式 |
| `src/peng/webui/html/static/js/app.js` | nest 选择器 JS + _syncNestDropdown() |
| `src/peng/webui/routes/api.py` | /api/nest/* + /api/set-workdir + 远程文件操作 |

---

## 14. 已知问题和注意事项

### Windows 平台

1. **nssm 服务无法访问桌面**：nssm 注册的服务以 SYSTEM 身份运行在 Session 0，无法截屏/键鼠输入。解决方案：使用 schtasks 以用户身份运行。
2. **GBK 编码问题**：Windows 中文版默认 GBK，subprocess/print 使用 `encoding="utf-8", errors="replace"`，打印使用 `_safe_print()`。
3. **pythonw.exe 后台运行**：使用 `pythonw.exe + DETACHED_PROCESS` 实现无窗口后台运行。
4. **PG 不适合 nssm 服务**：PostgreSQL 以 nssm SYSTEM 服务运行时 fork 的 backend 进程会 DLL 初始化失败（0xC0000142），改用 `pg_ctl start` 管理。

### 通用

5. **子进程需独立初始化**：TaskRunner 通过 subprocess.Popen 创建子进程，每个子进程有独立的全局变量空间，必须在子进程入口处调用 `init_nest_clients()`。
6. **转发请求需重试**：子进程通过 `_forward_via_server()` 转发请求时，可能因短暂连接断开导致失败，内置 2 次重试 + 指数退避。
7. **截屏大消息**：WebSocket max_size 设为 50MB，允许高分辨率截屏图片通过。
8. **CLI 远程模式静默回退**：连接失败时 bin/task 和 bin/msg 不输出错误信息，直接回退本地模式。如需强制本地可使用 `--local` 参数。
9. **default nest 配置**：确保 config/nest.json 中 default 对应的实例 host 在当前网络上可达（DNS 解析正确），否则远程模式不会生效。

---

## 15. 测试验证

### 模块导入测试（8/8 通过）

| 模块 | 结果 |
|------|------|
| peng.nest.protocol | ✅ |
| peng.nest.security | ✅ |
| peng.nest.local | ✅ |
| peng.nest.server | ✅ |
| peng.nest.client | ✅ |
| peng.nest.cli | ✅ |
| peng.tools.screen | ✅ |
| peng.tools._nest_helper | ✅ |

### 协议单元测试（6/6 通过）

| 测试项 | 结果 |
|--------|------|
| NestRequest 创建 + 序列化 roundtrip | ✅ |
| NestResponse.make_result | ✅ |
| NestResponse.make_error | ✅ |
| NestResponse.make_stream | ✅ |
| NestError 异常 | ✅ |
| RequestType 全部19种类型 | ✅ |

### 安全控制测试（10/10 通过）

| 测试项 | 结果 |
|--------|------|
| 默认策略允许所有路径 | ✅ |
| 默认策略允许所有命令 | ✅ |
| dir_whitelist 允许/阻止 | ✅ |
| dir_blacklist 阻止 | ✅ |
| cmd_blacklist 阻止 | ✅ |
| cmd_blacklist 不影响其他命令 | ✅ |
| enable_window_query=False | ✅ |
| enable_screenshot=False | ✅ |
| NestSecurity.from_config | ✅ |
| 服务端安全检查阻止命令 | ✅ |

### 端到端实测（Windows 平台）

| 测试项 | 结果 |
|--------|------|
| WebSocket 连接 | ✅ |
| Ping (platform=Windows, hostname=epyc) | ✅ |
| List Windows (15 windows) | ✅ |
| Screenshot (1920066 bytes, 3840x2160) | ✅ |
| Bash echo | ✅ |
| Write/Read file | ✅ |
| Grep | ✅ |
| Glob | ✅ |
| nest install/uninstall/status | ✅ |
| fly install → nest RUNNING | ✅ |

### CLI 远程模式实测（跨机器）

| 测试项 | 结果 |
|--------|------|
| bin/task tree 1 → 远程 epyc 事务数据 | ✅ |
| bin/msg list → 远程 epyc 消息数据（5条 vs 本地2条） | ✅ |
| bin/task --local tree 1 → 本地 vnas 事务数据 | ✅ |
| bin/msg --local list → 本地 vnas 消息数据 | ✅ |
| host 不可达时静默回退本地模式 | ✅ |
| 无 default 配置时本地模式 | ✅ |

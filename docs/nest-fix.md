# nest-workdir 优化进度

## 事务描述
为 message 和 task 数据表加 nest 字段，让消息和事务携带 nest 上下文，使工具可以通过 get_context('nest') 获取正确的远程连接。

## 需求分解

### 1. 数据库层：messages 和 tasks 表加 nest 字段
- ✅ messages 表添加 `nest TEXT DEFAULT ''` 字段
- ✅ tasks 表添加 `nest TEXT DEFAULT ''` 字段
- ✅ 创建迁移 SQL: `src/peng/db/migrations/add_nest_field.sql`
- ✅ init_postgres_db.py 表定义加 nest

### 2. 配置层：config.webui 加 nest 配置
- ✅ WebUIConfig 添加 `nest: str = ""` 字段
- ✅ config/webui.json 中 webui 节添加 `"nest": "local"`
- ✅ /api/nest/list 用 config.webui.nest 作为默认值
- ✅ /api/nest/select 同时更新 config.webui.nest 文件

### 3. 消息创建层：新消息用 config.webui.nest 填充
- ✅ MessageDatabase.send_message 添加 nest 参数并写入 INSERT
- ✅ MessageManager.create_message 添加 nest 参数透传
- ✅ 回复消息时从被回复消息继承 nest
- ✅ WebUI create_message/reply_message 从 request.nest 传入

### 4. 事务创建层：task/subtask 复制关联消息的 nest 字段
- ✅ TaskDatabase.create_task 添加 nest 参数
- ✅ TaskDatabase.create_task 从关联消息继承 nest（自动查询 MessageDatabase）
- ✅ TaskDatabase.create_task 从父事务继承 nest
- ✅ TaskManager.create_subtask 从父事务继承 nest

### 5. 工具上下文层：set_context 设置 nest 和 workdir
- ✅ agent.py 的 set_context 调用中添加 `nest=task.get("nest")`

### 6. 工具使用层：需要连接 nest 的工具通过 get_context 获取
- ✅ client.py: _NestClientCompat 添加 set_nest_name() 和 _nest_name
- ✅ _forward_via_server 支持 nest_name 参数透传
- ✅ bridge.py: _handle_forward_request 支持 nest_name 指定目标连接
- ✅ bash.py: _get_nest_client 从 get_context("nest") 设置 nest_name
- ✅ file.py: ReadTool/WriteTool/EditTool 的 _get_nest_client 设置 nest_name
- ✅ glob.py: _get_nest_client 设置 nest_name
- ✅ grep.py: _get_nest_client 设置 nest_name
- ✅ screen.py: WindowTool/ShotTool/InputTool 的 _get_nest_client 设置 nest_name

### 7. WebUI 同步层：选中消息/事务时同步 nest 下拉框
- ✅ app.js: 新增 `_syncNestDropdown()` 辅助函数
- ✅ CreateMessageRequest/ReplyMessageRequest 添加 nest 字段
- ✅ selectMessage: 同步 msg.nest 到下拉框
- ✅ selectMessageBranch: 同步 branchMsg.nest 到下拉框
- ✅ selectTask: 同步 task.nest（继承链：子事务>父事务>分支消息>父消息）到下拉框
- ✅ selectMessageHead: 同步 msg.nest 到下拉框

## 数据流

### 新建消息时的 nest 数据流
```
WebUI 前端 (nest from dropdown) 
  → POST /api/messages {nest: "local"} 
  → MessageManager.create_message(nest="local") 
  → MessageDatabase.send_message(nest="local") 
  → INSERT INTO messages (..., nest) VALUES (..., "local")
```

### 新建事务时的 nest 继承流
```
消息创建 → INSERT messages.nest = "local"
TaskDatabase.create_task(message_id=X)
  → 查询 messages.nest → "local"
  → INSERT INTO tasks (..., nest) VALUES (..., "local")
```

### 工具执行时的 nest 传递流
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

### WebUI 选中同步流
```
用户点击消息/事务 → selectMessage/selectTask
  → 从 msg.nest/task.nest 读取值
  → _syncNestDropdown(nestName)
  → 更新下拉框选中值并触发 change 事件
  → POST /api/nest/select 同步后端状态
```

## 问题记录

## 开发心得

1. `_NestClientCompat` 是关键桥接层——旧工具代码用 `get_nest_client()` 获取 client，然后调用 `client.bash()`。通过在 client 上加 `set_nest_name()`，所有工具都能透明地使用正确的远程连接。
2. 数据库字段用 `DEFAULT ''` 而非 `DEFAULT NULL`，避免现有记录需要迁移数据。
3. TaskDatabase 中的 nest 继承优先级：显式传入 > 关联消息 > 父事务。
4. WebUI nest 下拉框同步通过 `_syncNestDropdown()` 统一处理，避免代码重复。
5. `/api/nest/select` 同时更新 config/webui.nest 文件，确保下次刷新页面时保持一致。

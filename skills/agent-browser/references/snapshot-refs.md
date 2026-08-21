# 快照和引用

紧凑的元素引用，大幅减少 AI 代理的上下文使用量。

**相关文档**: [commands.md](commands.md) 完整命令参考, [SKILL.md](../SKILL.md) 快速入门。

## 目录

- [引用如何工作](#引用如何工作)
- [快照命令](#快照命令)
- [使用引用](#使用引用)
- [引用生命周期](#引用生命周期)
- [最佳实践](#最佳实践)
- [引用表示法详情](#引用表示法详情)
- [故障排除](#故障排除)

## 引用如何工作

传统方法：
```
完整 DOM/HTML → AI 解析 → CSS 选择器 → 操作 (~3000-5000 tokens)
```

agent-browser 方法：
```
紧凑快照 → 分配 @refs → 直接交互 (~200-400 tokens)
```

## 快照命令

```bash
# 基本快照 (显示页面结构)
agent-browser snapshot

# 交互快照 (-i 标志) - 推荐
agent-browser snapshot -i
```

### 快照输出格式

```
Page: Example Site - Home
URL: https://example.com

@e1 [header]
  @e2 [nav]
    @e3 [a] "Home"
    @e4 [a] "Products"
    @e5 [a] "About"
  @e6 [button] "Sign In"

@e7 [main]
  @e8 [h1] "Welcome"
  @e9 [form]
    @e10 [input type="email"] placeholder="Email"
    @e11 [input type="password"] placeholder="Password"
    @e12 [button type="submit"] "Log In"

@e13 [footer]
  @e14 [a] "Privacy Policy"
```

## 使用引用

获得引用后，直接交互：

```bash
# 点击 "Sign In" 按钮
agent-browser click @e6

# 填写邮箱输入框
agent-browser fill @e10 "user@example.com"

# 填写密码
agent-browser fill @e11 "password123"

# 提交表单
agent-browser click @e12
```

## 引用生命周期

**重要**: 页面变化时引用失效！

```bash
# 获取初始快照
agent-browser snapshot -i
# @e1 [button] "Next"

# 点击触发页面变化
agent-browser click @e1

# 必须重新快照以获取新引用！
agent-browser snapshot -i
# @e1 [h1] "Page 2"  ← 现在是不同的元素！
```

## 最佳实践

### 1. 交互前始终快照

```bash
# 正确
agent-browser open https://example.com
agent-browser snapshot -i          # 先获取引用
agent-browser click @e1            # 使用引用

# 错误
agent-browser open https://example.com
agent-browser click @e1            # 引用还不存在！
```

### 2. 导航后重新快照

```bash
agent-browser click @e5            # 导航到新页面
agent-browser snapshot -i          # 获取新引用
agent-browser click @e1            # 使用新引用
```

### 3. 动态变化后重新快照

```bash
agent-browser click @e1            # 打开下拉菜单
agent-browser snapshot -i          # 查看下拉菜单项
agent-browser click @e7            # 选择项目
```

### 4. 快照特定区域

对于复杂页面，快照特定区域：

```bash
# 仅快照表单
agent-browser snapshot @e9
```

## 引用表示法详情

```
@e1 [tag type="value"] "text content" placeholder="hint"
│    │   │             │               │
│    │   │             │               └─ 附加属性
│    │   │             └─ 可见文本
│    │   └─ 显示的关键属性
│    └─ HTML 标签名
└─ 唯一引用 ID
```

### 常见模式

```
@e1 [button] "Submit"                    # 带文本的按钮
@e2 [input type="email"]                 # 邮箱输入
@e3 [input type="password"]              # 密码输入
@e4 [a href="/page"] "Link Text"         # 锚点链接
@e5 [select]                             # 下拉菜单
@e6 [textarea] placeholder="Message"     # 文本区域
@e7 [div class="modal"]                  # 容器 (相关时)
@e8 [img alt="Logo"]                     # 图片
@e9 [checkbox] checked                   # 已勾选的复选框
@e10 [radio] selected                    # 已选中的单选按钮
```

## 故障排除

### "Ref not found" 错误

```bash
# 引用可能已变化 - 重新快照
agent-browser snapshot -i
```

### 元素在快照中不可见

```bash
# 向下滚动以显示元素
agent-browser scroll down 1000
agent-browser snapshot -i

# 或等待动态内容
agent-browser wait 1000
agent-browser snapshot -i
```

### 元素太多

```bash
# 快照特定容器
agent-browser snapshot @e5

# 或使用 get text 仅提取内容
agent-browser get text @e5
```

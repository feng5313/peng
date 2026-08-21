# 认证模式

登录流程、会话持久化、OAuth、2FA 和已认证浏览。

**相关文档**: [session-management.md](session-management.md) 状态持久化详情, [SKILL.md](../SKILL.md) 快速入门。

## 目录

- [基本登录流程](#基本登录流程)
- [保存认证状态](#保存认证状态)
- [恢复认证](#恢复认证)
- [OAuth / SSO 流程](#oauth--sso-流程)
- [双因素认证](#双因素认证)
- [HTTP 基础认证](#http-基础认证)
- [基于 Cookie 的认证](#基于-cookie-的认证)
- [令牌刷新处理](#令牌刷新处理)
- [安全最佳实践](#安全最佳实践)

## 基本登录流程

```bash
# 导航到登录页面
agent-browser open https://app.example.com/login
agent-browser wait --load networkidle

# 获取表单元素
agent-browser snapshot -i
# 输出: @e1 [input type="email"], @e2 [input type="password"], @e3 [button] "Sign In"

# 填写凭据
agent-browser fill @e1 "user@example.com"
agent-browser fill @e2 "password123"

# 提交
agent-browser click @e3
agent-browser wait --load networkidle

# 验证登录成功
agent-browser get url  # 应该是仪表板，而非登录页
```

## 保存认证状态

登录后，保存状态供复用：

```bash
# 先登录 (见上文)
agent-browser open https://app.example.com/login
agent-browser snapshot -i
agent-browser fill @e1 "user@example.com"
agent-browser fill @e2 "password123"
agent-browser click @e3
agent-browser wait --url "**/dashboard"

# 保存已认证状态
agent-browser state save ./auth-state.json
```

## 恢复认证

通过加载保存的状态跳过登录：

```bash
# 加载保存的认证状态
agent-browser state load ./auth-state.json

# 直接导航到受保护页面
agent-browser open https://app.example.com/dashboard

# 验证已认证
agent-browser snapshot -i
```

## OAuth / SSO 流程

处理 OAuth 重定向：

```bash
# 启动 OAuth 流程
agent-browser open https://app.example.com/auth/google

# 自动处理重定向
agent-browser wait --url "**/accounts.google.com**"
agent-browser snapshot -i

# 填写 Google 凭据
agent-browser fill @e1 "user@gmail.com"
agent-browser click @e2  # 下一步按钮
agent-browser wait 2000
agent-browser snapshot -i
agent-browser fill @e3 "password"
agent-browser click @e4  # 登录

# 等待重定向回来
agent-browser wait --url "**/app.example.com**"
agent-browser state save ./oauth-state.json
```

## 双因素认证

通过人工干预处理 2FA：

```bash
# 使用凭据登录
agent-browser open https://app.example.com/login --headed  # 显示浏览器
agent-browser snapshot -i
agent-browser fill @e1 "user@example.com"
agent-browser fill @e2 "password123"
agent-browser click @e3

# 等待用户手动完成 2FA
echo "在浏览器窗口中完成 2FA..."
agent-browser wait --url "**/dashboard" --timeout 120000

# 2FA 后保存状态
agent-browser state save ./2fa-state.json
```

## HTTP 基础认证

对于使用 HTTP 基础认证的站点：

```bash
# 导航前设置凭据
agent-browser set credentials username password

# 导航到受保护资源
agent-browser open https://protected.example.com/api
```

## 基于 Cookie 的认证

手动设置认证 cookies：

```bash
# 设置认证 cookie
agent-browser cookies set session_token "abc123xyz"

# 导航到受保护页面
agent-browser open https://app.example.com/dashboard
```

## 令牌刷新处理

对于有过期令牌的会话：

```bash
#!/bin/bash
# 处理令牌刷新的包装器

STATE_FILE="./auth-state.json"

# 尝试加载现有状态
if [[ -f "$STATE_FILE" ]]; then
    agent-browser state load "$STATE_FILE"
    agent-browser open https://app.example.com/dashboard

    # 检查会话是否仍然有效
    URL=$(agent-browser get url)
    if [[ "$URL" == *"/login"* ]]; then
        echo "会话已过期，重新认证..."
        # 执行全新登录
        agent-browser snapshot -i
        agent-browser fill @e1 "$USERNAME"
        agent-browser fill @e2 "$PASSWORD"
        agent-browser click @e3
        agent-browser wait --url "**/dashboard"
        agent-browser state save "$STATE_FILE"
    fi
else
    # 首次登录
    agent-browser open https://app.example.com/login
    # ... 登录流程 ...
fi
```

## 安全最佳实践

1. **永远不要提交状态文件** - 它们包含会话令牌
   ```bash
   echo "*.auth-state.json" >> .gitignore
   ```

2. **使用环境变量存储凭据**
   ```bash
   agent-browser fill @e1 "$APP_USERNAME"
   agent-browser fill @e2 "$APP_PASSWORD"
   ```

3. **自动化后清理**
   ```bash
   agent-browser cookies clear
   rm -f ./auth-state.json
   ```

4. **CI/CD 使用短期会话**
   ```bash
   # CI 中不要持久化状态
   agent-browser open https://app.example.com/login
   # ... 登录并执行操作 ...
   agent-browser close  # 会话结束，不持久化任何内容
   ```

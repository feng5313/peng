---
name: agent-browser
description: AI 代理的浏览器自动化 CLI。当用户需要与网站交互时使用，包括导航页面、填写表单、点击按钮、截图、提取数据、测试 Web 应用或自动化任何浏览器事务。触发词包括"打开网站"、"填写表单"、"点击按钮"、"截图"、"从页面抓取数据"、"测试这个 Web 应用"、"登录网站"、"自动化浏览器操作"或任何需要程序化 Web 交互的事务。
allowed-tools: Bash(agent-browser:*)
---

# agent-browser - 浏览器自动化 CLI

## 环境配置 (必读)

首次使用前必须配置浏览器可执行路径。在 WSL/Linux 环境下，系统 snap chromium 可能不兼容 headless 模式，需要指定 Chrome for Testing 或 Playwright 内置的 Chromium：

```bash
# 方法1: 环境变量 (推荐，全局生效)
export AGENT_BROWSER_EXECUTABLE_PATH=/home/<user>/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome

# 方法2: 配置文件 (项目级持久化)
# 创建 agent-browser.json:
# {"executablePath": "/home/<user>/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome"}
# 使用: AGENT_BROWSER_CONFIG=./agent-browser.json agent-browser open <url>

# 方法3: CLI 标志 (每次命令指定)
agent-browser open --executable-path /path/to/chrome <url>

# 安装 Chrome for Testing (官方推荐):
agent-browser install
```

超时设置 (慢速页面必须调整)：
```bash
export AGENT_BROWSER_DEFAULT_TIMEOUT=30000  # 毫秒，默认 25000，不要超过 30000
```

## 核心工作流

### 多Session模式（默认）

**所有操作默认使用命名session**，支持多个任务并行互不干扰：

```bash
# 使用命名session打开页面（推荐）
agent-browser --session mytask open https://example.com

# 所有后续操作都带上同一session名
agent-browser --session mytask wait --load networkidle
agent-browser --session mytask snapshot -i
agent-browser --session mytask close

# 多session并行（不同任务完全隔离）
agent-browser --session task1 open https://site-a.com
agent-browser --session task2 open https://site-b.com
agent-browser --session task1 snapshot -i    # 不影响 task2
agent-browser session list                   # 查看所有活动session
agent-browser close --all                    # 关闭全部
```

**session命名规范**：用任务/用途命名，如 `--session webui`（调试WebUI）、`--session scrape`（抓取数据）、`--session auth`（认证流程）。

### 获取 WebUI 页面内容

```bash
# 1. 打开页面
agent-browser --session webui open https://example.com

# 2. 等待页面加载完成 (重要!)
agent-browser --session webui wait --load networkidle

# 3. 获取页面内容 (两种方式)
agent-browser --session webui snapshot -i       # 可访问性树 + 元素引用 (推荐，AI 友好)
agent-browser --session webui get text body     # 纯文本内容

# 4. 关闭浏览器
agent-browser --session webui close
```

### 交互式工作流 (表单、按钮等)

```bash
# 1. 打开并等待
agent-browser --session form open https://example.com/form
agent-browser --session form wait --load networkidle

# 2. 获取元素引用
agent-browser --session form snapshot -i
# 输出: @e1 [textbox "Email"], @e2 [textbox "Password"], @e3 [button "Submit"]

# 3. 使用引用交互
agent-browser --session form fill @e1 "user@example.com"
agent-browser --session form fill @e2 "password123"
agent-browser --session form click @e3

# 4. 页面变化后重新快照 (引用会失效!)
agent-browser --session form wait --load networkidle
agent-browser --session form snapshot -i

# 5. 关闭
agent-browser --session form close
```

**引用生命周期规则**: 点击导航链接、提交表单、动态内容加载后，所有引用失效，必须重新 `snapshot`。引用在session内有效，跨session无效。

### 链式调用

不需要中间输出时，用 `&&` 链式执行更高效：
```bash
agent-browser --session s1 open https://example.com && agent-browser --session s1 wait --load networkidle && agent-browser --session s1 snapshot -i
```
需要解析中间输出时，分开执行命令。

## 命令参考

> **约定**：以下命令省略 `--session <name>` 前缀，实际使用时始终带命名session。

### Session 管理
```bash
agent-browser --session <name> open <url>   # 在指定session中打开页面
agent-browser --session <name> close         # 关闭指定session
agent-browser close --all                    # 关闭所有session
agent-browser session list                   # 列出活动session
```

### 导航
```bash
agent-browser open <url>              # 打开页面 (别名: goto, navigate)
agent-browser close                   # 关闭浏览器
agent-browser back / forward / reload # 导航历史
```

### 快照 (获取页面内容)
```bash
agent-browser snapshot                # 完整可访问性树
agent-browser snapshot -i             # 仅交互元素 (推荐)
agent-browser snapshot -c             # 紧凑模式 (去除空结构)
agent-browser snapshot -d 3           # 限制深度 3 层
agent-browser snapshot -s "#main"     # 限定到 CSS 选择器
agent-browser snapshot -i --json      # JSON 输出 (便于程序解析)
```

### 交互 (使用快照中的 @refs)
```bash
agent-browser click @e1               # 点击
agent-browser fill @e2 "text"         # 清空并输入
agent-browser type @e2 "text"         # 追加输入
agent-browser select @e1 "option"     # 下拉选择
agent-browser check @e1               # 勾选
agent-browser press Enter             # 按键
agent-browser scroll down 500         # 滚动
```

### 获取信息
```bash
agent-browser get text @e1            # 元素文本
agent-browser get text body           # 页面全部文本
agent-browser get html @e1            # 元素 HTML
agent-browser get url                 # 当前 URL
agent-browser get title               # 页面标题
agent-browser get count ".item"       # 元素计数
```

### 等待
```bash
agent-browser wait @e1                # 等待元素出现
agent-browser wait --load networkidle # 等待网络空闲 (最常用)
agent-browser wait --url "**/page"    # 等待 URL 匹配
agent-browser wait --text "Welcome"   # 等待文本出现
agent-browser wait 3000               # 等待毫秒数
agent-browser wait --fn "document.readyState === 'complete'"  # JS 条件
```

### 截图与捕获
```bash
agent-browser screenshot              # 截图到临时目录
agent-browser screenshot --full       # 全页面截图
agent-browser screenshot --annotate   # 带元素编号标签 (视觉分析用)
agent-browser screenshot page.png     # 截图到指定路径
agent-browser pdf output.pdf          # 保存为 PDF
```

### 语义定位器 (无需 snapshot)
```bash
agent-browser find text "Sign In" click
agent-browser find label "Email" fill "user@test.com"
agent-browser find role button click --name "Submit"
agent-browser find placeholder "Search" type "query"
```

### JavaScript 执行
```bash
# 简单表达式
agent-browser eval 'document.title'

# 复杂 JS 用 --stdin 避免 shell 转义问题
agent-browser eval --stdin <<'EOF'
JSON.stringify(Array.from(document.querySelectorAll("a")).map(a => a.href))
EOF

# 或用 -b base64 编码
agent-browser eval -b "$(echo -n 'document.title' | base64)"
```

### 下载
```bash
agent-browser download @e1 ./file.pdf      # 点击元素触发下载
agent-browser wait --download ./output.zip # 等待下载完成
```

### 本地文件
```bash
agent-browser --allow-file-access open file:///path/to/document.pdf
agent-browser --allow-file-access open file:///path/to/page.html
```

## 常见模式

### 数据提取
```bash
agent-browser --session scrape open https://example.com/products
agent-browser --session scrape wait --load networkidle
agent-browser --session scrape snapshot -i
agent-browser --session scrape get text @e5               # 特定元素文本
agent-browser --session scrape get text body              # 全部文本
agent-browser --session scrape snapshot -i --json         # JSON 格式便于解析
agent-browser --session scrape close
```

### 表单提交
```bash
agent-browser --session form open https://example.com/signup
agent-browser --session form wait --load networkidle
agent-browser --session form snapshot -i
agent-browser --session form fill @e1 "Jane Doe"
agent-browser --session form fill @e2 "jane@example.com"
agent-browser --session form select @e3 "California"
agent-browser --session form check @e4
agent-browser --session form click @e5
agent-browser --session form wait --load networkidle
agent-browser --session form snapshot -i
agent-browser --session form close
```

### 认证与状态持久化
```bash
# 登录并保存状态
agent-browser --session auth open https://app.example.com/login
agent-browser --session auth wait --load networkidle
agent-browser --session auth snapshot -i
agent-browser --session auth fill @e1 "$USERNAME"
agent-browser --session auth fill @e2 "$PASSWORD"
agent-browser --session auth click @e3
agent-browser --session auth wait --url "**/dashboard"
agent-browser --session auth state save auth.json
agent-browser --session auth close

# 复用状态
agent-browser --session auth state load auth.json
agent-browser --session auth open https://app.example.com/dashboard
```

### 认证保险库 (密码不暴露给 LLM)
```bash
echo "pass" | agent-browser auth save github --url https://github.com/login --username user --password-stdin
agent-browser auth login github
agent-browser auth list
agent-browser auth delete github
```

### 会话持久化 (自动保存/恢复 cookies)
```bash
agent-browser --session-name myapp open https://app.example.com/login
# ... 登录 ...
agent-browser close  # 状态自动保存到 ~/.agent-browser/sessions/

# 下次自动恢复
agent-browser --session-name myapp open https://app.example.com/dashboard
```

### 并行会话
```bash
agent-browser --session s1 open https://site-a.com
agent-browser --session s2 open https://site-b.com
agent-browser --session s1 snapshot -i
agent-browser session list
agent-browser --session s1 close
agent-browser --session s2 close
```

### 连接已有 Chrome
```bash
agent-browser --auto-connect snapshot           # 自动发现
agent-browser --cdp 9222 snapshot               # 指定端口
```

### 差异比较
```bash
agent-browser snapshot -i               # 基准快照
agent-browser click @e2                 # 执行操作
agent-browser diff snapshot             # 比较变更 (+/- 格式)
agent-browser diff screenshot --baseline before.png  # 视觉差异
```

### 会话管理

始终在完成后关闭浏览器，避免进程泄漏：
```bash
agent-browser --session mytask close  # 关闭指定会话
agent-browser close --all             # 关闭所有会话
agent-browser session list            # 查看活动会话
```

如果上一个会话没有正确关闭，先 `agent-browser --session mytask close` 或 `agent-browser close --all` 清理残留守护进程。

## 配置文件

优先级 (低→高): `~/.agent-browser/config.json` < `./agent-browser.json` < 环境变量 < CLI 标志。

```json
{
  "executablePath": "/path/to/chrome",
  "headed": false,
  "proxy": "http://localhost:8080",
  "profile": "./browser-data",
  "args": "--no-sandbox"
}
```

使用 `--config <path>` 或 `AGENT_BROWSER_CONFIG` 加载自定义配置。所有 CLI 选项映射到 camelCase 键。

## 深入文档

| 参考文档 | 使用场景 |
|-----------|-------------|
| [references/commands.md](references/commands.md) | 完整命令参考及所有选项 |
| [references/snapshot-refs.md](references/snapshot-refs.md) | 引用生命周期、失效规则、故障排除 |
| [references/session-management.md](references/session-management.md) | 并行会话、状态持久化、并发抓取 |
| [references/authentication.md](references/authentication.md) | 登录流程、OAuth、2FA 处理、状态复用 |
| [references/proxy-support.md](references/proxy-support.md) | 代理配置、地理测试、轮换代理 |

## 即用模板

| 模板 | 描述 |
|----------|-------------|
| [templates/form-automation.sh](templates/form-automation.sh) | 带验证的表单填写 |
| [templates/authenticated-session.sh](templates/authenticated-session.sh) | 登录一次，复用状态 |
| [templates/capture-workflow.sh](templates/capture-workflow.sh) | 带截图的内容提取 |

## Windows 环境实战经验 (重要!)

### 关键认知

1. **`agent-browser open` 是非阻塞命令**：它启动 daemon 后立刻返回（约 1-3 秒），不会阻塞等待页面加载。shell 超时不代表失败！设置 bash timeout 至少 30000ms。
2. **daemon 是持久后台进程**：`open` 后 daemon 持续运行，后续所有命令（get/snapshot/eval等）都通过 daemon 执行。
3. **`--args "--no-sandbox"` 在 Windows 上经常必须**：Chrome headless 在某些 Windows 环境下需要此参数。
4. **`--cdp <port>` 是万能连接方式**：当 daemon 启动的 Chrome 不稳定时，可手动启动 Chrome 然后通过 CDP 连接。

### Windows 标准流程

```bash
# 步骤0: 首次使用，安装 Chrome for Testing
agent-browser install

# 步骤1: 清理残留（如果之前异常退出）
taskkill /F /IM chrome.exe 2>NUL
agent-browser close --all

# 步骤2: 写配置文件持久化参数（只需一次）
# 创建 ~/.agent-browser/config.json:
# {"args": "--no-sandbox"}
# 这样每次 open 自动带上 --no-sandbox，无需 CLI 指定

# 步骤3: 打开页面（使用命名session）
agent-browser --session webui open http://localhost:6688 --args "--no-sandbox"
# 注意：此命令约 1-3 秒返回，正常行为！

# 步骤4: 立刻执行后续操作（分开调用，不要用 && 链式）
agent-browser --session webui get title
agent-browser --session webui wait --load networkidle
agent-browser --session webui snapshot -i
agent-browser --session webui eval "document.title"
agent-browser --session webui screenshot page.png

# 步骤5: 使用完毕关闭
agent-browser --session webui close
```

### Chrome 启动失败时的应急方案

当 `agent-browser open` 报 "Chrome exited early" 错误时：

```bash
# 方案A: 清理残留进程后重试
taskkill /F /IM chrome.exe 2>NUL
agent-browser close --all
agent-browser open http://localhost:6688 --args "--no-sandbox"

# 方案B: 手动启动 Chrome + CDP 连接（最可靠！）
# 1. 手动启动 Chrome headless，指定调试端口
start "" "C:\Users\feng\.agent-browser\browsers\chrome-148.0.7778.97\chrome.exe" --headless=new --no-sandbox --disable-gpu --remote-debugging-port=9333 --window-size=1280,720 "http://localhost:6688"

# 2. 等待 Chrome 启动
ping -n 3 127.0.0.1 > NUL

# 3. 验证 Chrome 可访问
curl -s http://localhost:9333/json/version

# 4. 通过 CDP 连接 agent-browser（注意：不要先 close --all，会杀掉手动启动的 Chrome！）
agent-browser --cdp 9333 get title
agent-browser --cdp 9333 eval "document.title"
agent-browser --cdp 9333 screenshot output.png

# 5. 用完后关闭
taskkill /F /IM chrome.exe
```

### 调试 DOM/CSS 问题的技巧

```bash
# 获取元素 computed style 链（从目标元素到根节点）
agent-browser --cdp 9333 eval "let el = document.getElementById('my-element'); let chain = []; while(el) { let cs = getComputedStyle(el); chain.push({tag: el.tagName, id: el.id, h: cs.height, w: cs.width, pos: cs.position, display: cs.display}); el = el.parentElement; } JSON.stringify(chain)"

# 获取元素尺寸和位置
agent-browser --cdp 9333 eval "JSON.stringify(document.getElementById('my-element').getBoundingClientRect())"

# 动态注入 CSS 修复测试
agent-browser --cdp 9333 eval "const s = document.createElement('style'); s.textContent = '.my-class { color: red !important; }'; document.head.appendChild(s); 'injected'"

# 强制刷新绕过缓存
agent-browser --cdp 9333 eval "location.reload()"
```

### 常见错误和解决方法

| 错误 | 原因 | 解决方法 |
|------|------|----------|
| `Chrome exited early` | 残留 Chrome 进程或缺少 --no-sandbox | `taskkill /F /IM chrome.exe` + `--args "--no-sandbox"` |
| `daemon already running` | 上次会话未关闭 | `agent-browser close --all` 后重试 |
| 命令超时（30s） | daemon 未启动成功 | 检查 Chrome 是否被杀毒软件阻止，用方案B手动启动 |
| `--args ignored` | daemon 已在运行，参数无法变更 | `agent-browser close --all` 后重新 open |
| eval 报 `Identifier already declared` | JS 上下文中已有同名变量 | 用不同变量名，如 `r2` 代替 `r` |

### 配置文件示例 (Windows)

```json
// ~/.agent-browser/config.json
{
  "args": "--no-sandbox"
}
```

设置后 `agent-browser open <url>` 自动带上 `--no-sandbox`，无需每次指定。

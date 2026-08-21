# 命令参考

agent-browser 所有命令的完整参考。快速入门和常见模式请参见 SKILL.md。

## 导航

```bash
agent-browser open <url>      # 导航到 URL (别名: goto, navigate)
                              # 支持: https://, http://, file://, about:, data://
                              # 如果没有协议则自动添加 https://
agent-browser back            # 后退
agent-browser forward         # 前进
agent-browser reload          # 重新加载页面
agent-browser close           # 关闭浏览器 (别名: quit, exit)
agent-browser connect 9222    # 通过 CDP 端口连接浏览器
```

## 快照 (页面分析)

```bash
agent-browser snapshot            # 完整可访问性树
agent-browser snapshot -i         # 仅交互元素 (推荐)
agent-browser snapshot -c         # 紧凑输出
agent-browser snapshot -d 3       # 限制深度为 3
agent-browser snapshot -s "#main" # 限定到 CSS 选择器
```

## 交互 (使用快照中的 @refs)

```bash
agent-browser click @e1           # 点击
agent-browser click @e1 --new-tab # 点击并在新标签页打开
agent-browser dblclick @e1        # 双击
agent-browser focus @e1           # 聚焦元素
agent-browser fill @e2 "text"     # 清空并输入
agent-browser type @e2 "text"     # 不清空直接输入
agent-browser press Enter         # 按键 (别名: key)
agent-browser press Control+a     # 组合键
agent-browser keydown Shift       # 按住键
agent-browser keyup Shift         # 释放键
agent-browser hover @e1           # 悬停
agent-browser check @e1           # 勾选复选框
agent-browser uncheck @e1         # 取消勾选复选框
agent-browser select @e1 "value"  # 选择下拉选项
agent-browser select @e1 "a" "b"  # 选择多个选项
agent-browser scroll down 500     # 滚动页面 (默认: 向下 300px)
agent-browser scrollintoview @e1  # 滚动元素到可见区域 (别名: scrollinto)
agent-browser drag @e1 @e2        # 拖放
agent-browser upload @e1 file.pdf # 上传文件
```

## 获取信息

```bash
agent-browser get text @e1        # 获取元素文本
agent-browser get html @e1        # 获取 innerHTML
agent-browser get value @e1       # 获取输入值
agent-browser get attr @e1 href   # 获取属性
agent-browser get title           # 获取页面标题
agent-browser get url             # 获取当前 URL
agent-browser get count ".item"   # 计算匹配元素数量
agent-browser get box @e1         # 获取边界框
agent-browser get styles @e1      # 获取计算样式 (字体、颜色、背景等)
```

## 检查状态

```bash
agent-browser is visible @e1      # 检查是否可见
agent-browser is enabled @e1      # 检查是否启用
agent-browser is checked @e1      # 检查是否勾选
```

## 截图和 PDF

```bash
agent-browser screenshot          # 保存到临时目录
agent-browser screenshot path.png # 保存到指定路径
agent-browser screenshot --full   # 全页面
agent-browser pdf output.pdf      # 保存为 PDF
```

## 视频录制

```bash
agent-browser record start ./demo.webm    # 开始录制
agent-browser click @e1                   # 执行操作
agent-browser record stop                 # 停止并保存视频
agent-browser record restart ./take2.webm # 停止当前录制 + 开始新录制
```

## 等待

```bash
agent-browser wait @e1                     # 等待元素
agent-browser wait 2000                    # 等待毫秒数
agent-browser wait --text "Success"        # 等待文本 (或 -t)
agent-browser wait --url "**/dashboard"    # 等待 URL 模式 (或 -u)
agent-browser wait --load networkidle      # 等待网络空闲 (或 -l)
agent-browser wait --fn "window.ready"     # 等待 JS 条件 (或 -f)
```

## 鼠标控制

```bash
agent-browser mouse move 100 200      # 移动鼠标
agent-browser mouse down left         # 按下按钮
agent-browser mouse up left           # 释放按钮
agent-browser mouse wheel 100         # 滚动滚轮
```

## 语义定位器 (引用的替代方案)

```bash
agent-browser find role button click --name "Submit"
agent-browser find text "Sign In" click
agent-browser find text "Sign In" click --exact      # 仅精确匹配
agent-browser find label "Email" fill "user@test.com"
agent-browser find placeholder "Search" type "query"
agent-browser find alt "Logo" click
agent-browser find title "Close" click
agent-browser find testid "submit-btn" click
agent-browser find first ".item" click
agent-browser find last ".item" click
agent-browser find nth 2 "a" hover
```

## 浏览器设置

```bash
agent-browser set viewport 1920 1080          # 设置视口大小
agent-browser set device "iPhone 14"          # 模拟设备
agent-browser set geo 37.7749 -122.4194       # 设置地理位置 (别名: geolocation)
agent-browser set offline on                  # 切换离线模式
agent-browser set headers '{"X-Key":"v"}'     # 额外 HTTP 头
agent-browser set credentials user pass       # HTTP 基础认证 (别名: auth)
agent-browser set media dark                  # 模拟配色方案
agent-browser set media light reduced-motion  # 浅色模式 + 减少动画
```

## Cookies 和存储

```bash
agent-browser cookies                     # 获取所有 cookies
agent-browser cookies set name value      # 设置 cookie
agent-browser cookies clear               # 清除 cookies
agent-browser storage local               # 获取所有 localStorage
agent-browser storage local key           # 获取特定键
agent-browser storage local set k v       # 设置值
agent-browser storage local clear         # 清除所有
```

## 网络

```bash
agent-browser network route <url>              # 拦截请求
agent-browser network route <url> --abort      # 阻止请求
agent-browser network route <url> --body '{}'  # 模拟响应
agent-browser network unroute [url]            # 移除路由
agent-browser network requests                 # 查看跟踪的请求
agent-browser network requests --filter api    # 过滤请求
```

## 标签页和窗口

```bash
agent-browser tab                 # 列出标签页
agent-browser tab new [url]       # 新建标签页
agent-browser tab 2               # 按索引切换标签页
agent-browser tab close           # 关闭当前标签页
agent-browser tab close 2         # 按索引关闭标签页
agent-browser window new          # 新建窗口
```

## 框架

```bash
agent-browser frame "#iframe"     # 切换到 iframe
agent-browser frame main          # 返回主框架
```

## 对话框

```bash
agent-browser dialog accept [text]  # 接受对话框
agent-browser dialog dismiss        # 取消对话框
```

## JavaScript

```bash
agent-browser eval "document.title"          # 仅简单表达式
agent-browser eval -b "<base64>"             # 任意 JavaScript (base64 编码)
agent-browser eval --stdin                   # 从 stdin 读取脚本
```

使用 `-b`/`--base64` 或 `--stdin` 确保可靠执行。带有嵌套引号和特殊字符的 shell 转义容易出错。

```bash
# Base64 编码你的脚本，然后:
agent-browser eval -b "ZG9jdW1lbnQucXVlcnlTZWxlY3RvcignW3NyYyo9Il9uZXh0Il0nKQ=="

# 或使用 stdin 和 heredoc 处理多行脚本:
cat <<'EOF' | agent-browser eval --stdin
const links = document.querySelectorAll('a');
Array.from(links).map(a => a.href);
EOF
```

## 状态管理

```bash
agent-browser state save auth.json    # 保存 cookies、存储、认证状态
agent-browser state load auth.json    # 恢复保存的状态
```

## 全局选项

```bash
agent-browser --session <name> ...    # 独立的浏览器会话
agent-browser --json ...              # JSON 输出便于解析
agent-browser --headed ...            # 显示浏览器窗口 (非无头)
agent-browser --full ...              # 全页面截图 (-f)
agent-browser --cdp <port> ...        # 通过 Chrome DevTools Protocol 连接
agent-browser -p <provider> ...       # 云浏览器提供商 (--provider)
agent-browser --proxy <url> ...       # 使用代理服务器
agent-browser --proxy-bypass <hosts>  # 绕过代理的主机
agent-browser --headers <json> ...    # 限定到 URL 来源的 HTTP 头
agent-browser --executable-path <p>   # 自定义浏览器可执行文件
agent-browser --extension <path> ...  # 加载浏览器扩展 (可重复)
agent-browser --ignore-https-errors   # 忽略 SSL 证书错误
agent-browser --help                  # 显示帮助 (-h)
agent-browser --version               # 显示版本 (-V)
agent-browser <command> --help        # 显示命令的详细帮助
```

## 调试

```bash
agent-browser --headed open example.com   # 显示浏览器窗口
agent-browser --cdp 9222 snapshot         # 通过 CDP 端口连接
agent-browser connect 9222                # 替代方案: connect 命令
agent-browser console                     # 查看控制台消息
agent-browser console --clear             # 清除控制台
agent-browser errors                      # 查看页面错误
agent-browser errors --clear              # 清除错误
agent-browser highlight @e1               # 高亮元素
agent-browser trace start                 # 开始录制追踪
agent-browser trace stop trace.zip        # 停止并保存追踪
agent-browser profiler start              # 启动 Chrome DevTools 性能分析
agent-browser profiler stop trace.json    # 停止并保存性能分析
```

## 环境变量

```bash
AGENT_BROWSER_SESSION="mysession"            # 默认会话名称
AGENT_BROWSER_EXECUTABLE_PATH="/path/chrome" # 自定义浏览器路径
AGENT_BROWSER_EXTENSIONS="/ext1,/ext2"       # 逗号分隔的扩展路径
AGENT_BROWSER_PROVIDER="browserbase"         # 云浏览器提供商
AGENT_BROWSER_STREAM_PORT="9223"             # WebSocket 流端口
AGENT_BROWSER_HOME="/path/to/agent-browser"  # 自定义安装位置
```

# 代理支持

用于地理测试、速率限制规避和企业环境的代理配置。

**相关文档**: [commands.md](commands.md) 全局选项, [SKILL.md](../SKILL.md) 快速入门。

## 目录

- [基本代理配置](#基本代理配置)
- [认证代理](#认证代理)
- [SOCKS 代理](#socks-代理)
- [代理绕过](#代理绕过)
- [常见使用场景](#常见使用场景)
- [验证代理连接](#验证代理连接)
- [故障排除](#故障排除)
- [最佳实践](#最佳实践)

## 基本代理配置

使用 `--proxy` 标志或通过环境变量设置代理：

```bash
# 通过 CLI 标志
agent-browser --proxy "http://proxy.example.com:8080" open https://example.com

# 通过环境变量
export HTTP_PROXY="http://proxy.example.com:8080"
agent-browser open https://example.com

# HTTPS 代理
export HTTPS_PROXY="https://proxy.example.com:8080"
agent-browser open https://example.com

# 两者都设置
export HTTP_PROXY="http://proxy.example.com:8080"
export HTTPS_PROXY="http://proxy.example.com:8080"
agent-browser open https://example.com
```

## 认证代理

对于需要认证的代理：

```bash
# 在 URL 中包含凭据
export HTTP_PROXY="http://username:password@proxy.example.com:8080"
agent-browser open https://example.com
```

## SOCKS 代理

```bash
# SOCKS5 代理
export ALL_PROXY="socks5://proxy.example.com:1080"
agent-browser open https://example.com

# 带认证的 SOCKS5
export ALL_PROXY="socks5://user:pass@proxy.example.com:1080"
agent-browser open https://example.com
```

## 代理绕过

使用 `--proxy-bypass` 或 `NO_PROXY` 跳过特定域名的代理：

```bash
# 通过 CLI 标志
agent-browser --proxy "http://proxy.example.com:8080" --proxy-bypass "localhost,*.internal.com" open https://example.com

# 通过环境变量
export NO_PROXY="localhost,127.0.0.1,.internal.company.com"
agent-browser open https://internal.company.com  # 直连
agent-browser open https://external.com          # 通过代理
```

## 常见使用场景

### 地理位置测试

```bash
#!/bin/bash
# 使用不同地理位置的代理测试站点

PROXIES=(
    "http://us-proxy.example.com:8080"
    "http://eu-proxy.example.com:8080"
    "http://asia-proxy.example.com:8080"
)

for proxy in "${PROXIES[@]}"; do
    export HTTP_PROXY="$proxy"
    export HTTPS_PROXY="$proxy"

    region=$(echo "$proxy" | grep -oP '^\w+-\w+')
    echo "测试地区: $region"

    agent-browser --session "$region" open https://example.com
    agent-browser --session "$region" screenshot "./screenshots/$region.png"
    agent-browser --session "$region" close
done
```

### 抓取时轮换代理

```bash
#!/bin/bash
# 轮换代理列表以避免速率限制

PROXY_LIST=(
    "http://proxy1.example.com:8080"
    "http://proxy2.example.com:8080"
    "http://proxy3.example.com:8080"
)

URLS=(
    "https://site.com/page1"
    "https://site.com/page2"
    "https://site.com/page3"
)

for i in "${!URLS[@]}"; do
    proxy_index=$((i % ${#PROXY_LIST[@]}))
    export HTTP_PROXY="${PROXY_LIST[$proxy_index]}"
    export HTTPS_PROXY="${PROXY_LIST[$proxy_index]}"

    agent-browser open "${URLS[$i]}"
    agent-browser get text body > "output-$i.txt"
    agent-browser close

    sleep 1  # 礼貌延迟
done
```

### 企业网络访问

```bash
#!/bin/bash
# 通过企业代理访问内部站点

export HTTP_PROXY="http://corpproxy.company.com:8080"
export HTTPS_PROXY="http://corpproxy.company.com:8080"
export NO_PROXY="localhost,127.0.0.1,.company.com"

# 外部站点通过代理
agent-browser open https://external-vendor.com

# 内部站点绕过代理
agent-browser open https://intranet.company.com
```

## 验证代理连接

```bash
# 检查你的出口 IP
agent-browser open https://httpbin.org/ip
agent-browser get text body
# 应显示代理的 IP，而非你的真实 IP
```

## 故障排除

### 代理连接失败

```bash
# 先测试代理连通性
curl -x http://proxy.example.com:8080 https://httpbin.org/ip

# 检查代理是否需要认证
export HTTP_PROXY="http://user:pass@proxy.example.com:8080"
```

### 通过代理时的 SSL/TLS 错误

某些代理执行 SSL 检查。如果遇到证书错误：

```bash
# 仅用于测试 - 生产环境不推荐
agent-browser open https://example.com --ignore-https-errors
```

### 性能缓慢

```bash
# 仅在必要时使用代理
export NO_PROXY="*.cdn.com,*.static.com"  # 直接访问 CDN
```

## 最佳实践

1. **使用环境变量** - 不要硬编码代理凭据
2. **适当设置 NO_PROXY** - 避免本地流量通过代理
3. **自动化前测试代理** - 用简单请求验证连通性
4. **优雅处理代理失败** - 为不稳定代理实现重试逻辑
5. **大型抓取事务轮换代理** - 分散负载并避免封禁

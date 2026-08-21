#!/bin/bash
# 模板: 已认证会话工作流
# 目的: 登录一次，保存状态，在后续运行中复用
# 用法: ./authenticated-session.sh <登录-URL> [状态-文件]
#
# 推荐: 使用认证保险库替代此模板:
#   echo "<密码>" | agent-browser auth save myapp --url <登录-URL> --username <用户名> --password-stdin
#   agent-browser auth login myapp
# 认证保险库安全存储凭据，LLM 永远看不到密码。
#
# 环境变量:
#   APP_USERNAME - 登录用户名/邮箱
#   APP_PASSWORD - 登录密码
#
# 两种模式:
#   1. 发现模式 (默认): 显示表单结构以便识别引用
#   2. 登录模式: 更新引用后执行实际登录
#
# 设置步骤:
#   1. 运行一次查看表单结构 (发现模式)
#   2. 更新下方登录流程部分的引用
#   3. 设置 APP_USERNAME 和 APP_PASSWORD
#   4. 删除发现部分

set -euo pipefail

LOGIN_URL="${1:?用法: $0 <登录-URL> [状态-文件]}"
STATE_FILE="${2:-./auth-state.json}"

echo "认证工作流: $LOGIN_URL"

# ================================================================
# 已保存状态: 如果存在有效的已保存状态则跳过登录
# ================================================================
if [[ -f "$STATE_FILE" ]]; then
    echo "从 $STATE_FILE 加载已保存状态..."
    if agent-browser --state "$STATE_FILE" open "$LOGIN_URL" 2>/dev/null; then
        agent-browser wait --load networkidle

        CURRENT_URL=$(agent-browser get url)
        if [[ "$CURRENT_URL" != *"login"* ]] && [[ "$CURRENT_URL" != *"signin"* ]]; then
            echo "会话恢复成功"
            agent-browser snapshot -i
            exit 0
        fi
        echo "会话已过期，执行全新登录..."
        agent-browser close 2>/dev/null || true
    else
        echo "加载状态失败，重新认证..."
    fi
    rm -f "$STATE_FILE"
fi

# ================================================================
# 发现模式: 显示表单结构 (设置完成后删除)
# ================================================================
echo "打开登录页面..."
agent-browser open "$LOGIN_URL"
agent-browser wait --load networkidle

echo ""
echo "登录表单结构:"
echo "---"
agent-browser snapshot -i
echo "---"
echo ""
echo "后续步骤:"
echo "  1. 记录引用: 用户名=@e?, 密码=@e?, 提交=@e?"
echo "  2. 更新下方登录流程部分的引用"
echo "  3. 设置: export APP_USERNAME='...' APP_PASSWORD='...'"
echo "  4. 删除此发现模式部分"
echo ""
agent-browser close
exit 0

# ================================================================
# 登录流程: 发现后取消注释并自定义
# ================================================================
# : "${APP_USERNAME:?设置 APP_USERNAME 环境变量}"
# : "${APP_PASSWORD:?设置 APP_PASSWORD 环境变量}"
#
# agent-browser open "$LOGIN_URL"
# agent-browser wait --load networkidle
# agent-browser snapshot -i
#
# # 填写凭据 (更新引用以匹配你的表单)
# agent-browser fill @e1 "$APP_USERNAME"
# agent-browser fill @e2 "$APP_PASSWORD"
# agent-browser click @e3
# agent-browser wait --load networkidle
#
# # 验证登录成功
# FINAL_URL=$(agent-browser get url)
# if [[ "$FINAL_URL" == *"login"* ]] || [[ "$FINAL_URL" == *"signin"* ]]; then
#     echo "登录失败 - 仍在登录页面"
#     agent-browser screenshot /tmp/login-failed.png
#     agent-browser close
#     exit 1
# fi
#
# # 保存状态供后续运行使用
# echo "保存状态到 $STATE_FILE"
# agent-browser state save "$STATE_FILE"
# echo "登录成功"
# agent-browser snapshot -i

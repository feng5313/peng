"""
{AGENT_NAME}的 UI 模块

提供命令行交互、环境设置、聊天等用户界面相关功能。

模块结构:
- env: 环境设置和变量加载
- args: 命令行参数解析
- commands: 交互命令处理
- chat: 聊天核心逻辑
- fly: fly 启动核心逻辑
- service: 后台服务模式
- output_reader: 输出文件读取器（新架构）
"""

from peng.ui.args import ChatArgs, create_chat_parser
from peng.ui.chat import run_chat
from peng.ui.fly import run_fly
from peng.ui.service import BackgroundService, run_service

__all__ = [
    "BackgroundService",
    "ChatArgs",
    "create_chat_parser",
    "run_chat",
    "run_fly",
    "run_service",
]

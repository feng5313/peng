"""
命令行参数解析模块

提供统一的命令行参数解析功能。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from peng.config import get_agent_name, load_config

AGENT_NAME = get_agent_name()


@dataclass
class ChatArgs:
    """聊天命令行参数"""

    model: str | None = None
    no_stream: bool = False
    config: Path | None = None
    max_iterations: int | None = None  # 从配置或命令行参数获取
    auto: bool = False
    thinking: str | None = None
    continue_last: bool = False
    reply_to: int | None = None
    caller: str = "chat"
    prompt: str | None = None
    nest: str | None = None
    workdir: str | None = None
    remote_host: str | None = None  # 远程 peng service 地址（远程模式时自动设置）
    remote_port: int | None = None  # 远程 peng service 端口（远程模式时自动设置）

    @classmethod
    def from_namespace(cls, ns: argparse.Namespace) -> ChatArgs:
        """从 argparse Namespace 创建"""
        prompt = " ".join(ns.args) if ns.args else None
        return cls(
            model=ns.model,
            no_stream=ns.no_stream,
            config=ns.config,
            max_iterations=ns.max_iterations,
            auto=ns.auto,
            thinking=ns.thinking,
            continue_last=getattr(ns, "continue_last", False),
            reply_to=getattr(ns, "reply_to", None),
            caller=getattr(ns, "caller", "chat"),
            prompt=prompt,
            nest=getattr(ns, "nest", None),
            workdir=getattr(ns, "workdir", None),
            remote_host=getattr(ns, "remote_host", None),
            remote_port=getattr(ns, "remote_port", None),
        )


def create_chat_parser() -> argparse.ArgumentParser:
    """创建聊天命令行参数解析器"""
    # 从配置中读取 max_iterations 默认值
    try:
        config = load_config()
        default_max_iterations = config.max_iterations
    except Exception:
        default_max_iterations = 100  # 回退默认值

    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_path = sys.executable
    py_venv = "虚拟环境" if ".venv" in py_path else "系统Python"

    parser = argparse.ArgumentParser(
        description=f"与{AGENT_NAME}对话\n\n环境: Python {py_version} ({py_venv})\n路径: {py_path}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s                        # 启动交互式对话
  %(prog)s 你好                   # 直接提问（全自动模式）
  %(prog)s -m glm-4-plus          # 指定模型
  %(prog)s --auto                 # 交互模式下启用全自动
  %(prog)s --thinking enabled     # 强制开启思考模式
  %(prog)s --service              # 启动后台服务模式
  %(prog)s --service --webui      # 启动后台服务 + Web UI
        """,
    )

    parser.add_argument(
        "-m",
        "--model",
        default=None,
        help="使用的模型",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="禁用流式输出",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        help="配置文件路径",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=default_max_iterations,
        help=f"最大迭代次数 (默认: {default_max_iterations})",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="启用全自动模式（非交互模式默认启用）",
    )
    parser.add_argument(
        "--thinking",
        choices=["auto", "enabled", "disabled"],
        default=None,
        help="思考模式：auto(自动), enabled(强制开启), disabled(强制关闭)",
    )
    parser.add_argument(
        "--continue",
        action="store_true",
        dest="continue_last",
        help="继续处理最近的消息（交互模式）",
    )
    parser.add_argument(
        "-r",
        "--reply",
        type=int,
        dest="reply_to",
        default=None,
        help="回复指定消息链 ID（非交互模式）",
    )
    parser.add_argument(
        "--caller",
        default="chat",
        help="调用来源（fly/chat），用于消息管理",
    )
    parser.add_argument(
        "--service",
        action="store_true",
        help="后台服务模式：开启消息监控和心跳监控，不主动创建新消息，低消耗值守",
    )
    parser.add_argument(
        "--webui",
        action="store_true",
        help="启动 Web UI（需配合 --service 使用，默认监听 0.0.0.0:6688）",
    )
    parser.add_argument(
        "args",
        nargs="*",
        help="可选的消息内容（非交互模式）",
    )
    parser.add_argument(
        "-n",
        "--nest",
        type=str,
        default=None,
        help="指定关联的nest名称",
    )
    parser.add_argument(
        "-w",
        "--workdir",
        type=str,
        default=None,
        help="指定工作目录",
    )
    parser.add_argument(
        "--remote-host",
        type=str,
        default=None,
        help=argparse.SUPPRESS,  # 内部使用，不显示在帮助中
    )
    parser.add_argument(
        "--remote-port",
        type=int,
        default=None,
        help=argparse.SUPPRESS,  # 内部使用，不显示在帮助中
    )

    return parser


def create_fly_parser() -> argparse.ArgumentParser:
    """创建 fly 命令行参数解析器"""
    # 从配置中读取 max_iterations 默认值
    try:
        config = load_config()
        default_max_iterations = config.max_iterations
    except Exception:
        default_max_iterations = 100  # 回退默认值

    parser = argparse.ArgumentParser(
        prog="peng",
        description=f"{AGENT_NAME} - 大鹏一日同风起，扶摇直上九万里！",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version="%(prog)s 0.1.0",
    )

    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        help="配置文件路径 (默认: config/peng.json)",
    )

    parser.add_argument(
        "--service",
        action="store_true",
        help="后台服务模式：开启消息监控和心跳监控，不主动创建新消息，低消耗值守",
    )

    parser.add_argument(
        "--webui",
        action="store_true",
        help="启动 Web UI（需配合 --service 使用，默认监听 0.0.0.0:6688）",
    )

    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="指定使用的模型",
    )

    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="禁用流式输出",
    )

    parser.add_argument(
        "-t",
        "--thinking",
        type=str,
        choices=["auto", "enabled", "disabled"],
        default="auto",
        help="思考模式：auto(自动), enabled(强制开启), disabled(强制关闭)",
    )

    parser.add_argument(
        "--small-model",
        type=str,
        default=None,
        help="指定小模型名称",
    )

    parser.add_argument(
        "--max-iterations",
        type=int,
        default=default_max_iterations,
        help=f"最大迭代次数 (默认: {default_max_iterations})",
    )

    parser.add_argument(
        "--auto",
        action="store_true",
        help="启用全自动模式（非交互模式默认启用）",
    )

    parser.add_argument(
        "args",
        nargs="*",
        help="可选的消息内容（非交互模式）",
    )

    return parser


# 别名，保持向后兼容
create_parser = create_chat_parser


def parse_args(argv: Sequence[str] | None = None) -> ChatArgs:
    """
    解析命令行参数

    Args:
        argv: 命令行参数（可选，默认使用sys.argv）

    Returns:
        ChatArgs 对象
    """
    parser = create_chat_parser()
    ns = parser.parse_args(argv)
    return ChatArgs.from_namespace(ns)

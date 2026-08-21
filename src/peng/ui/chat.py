"""
聊天核心模块

提供交互式聊天和非交互式单次对话功能。
通过 MessageManager → TaskManager 架构处理消息。

只被 bin/fly 引用，不独立运行。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from peng.config import get_agent_name
from peng.config.loader import get_master_name

AGENT_NAME = get_agent_name()

import asyncio  # noqa: E402
import logging  # noqa: E402
import signal  # noqa: E402
import sys  # noqa: E402
import traceback  # noqa: E402

from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402

from peng.common.colors import colorize, get_colors  # noqa: E402
from peng.config.loader import load_config, get_project_root
from peng.common.paths import ensure_dir_with_owner  # noqa: E402
from peng.ui.args import ChatArgs  # noqa: E402
from peng.ui.message_utils import (
    process_message,
    process_reply,
)

PROJECT_ROOT = get_project_root()
# 全局 Console
console = Console()

# 颜色配置
_COLORS = get_colors()

# 日志
_logger = logging.getLogger(__name__)


# 保留 should_show_output 的向后兼容别名
from peng.ui.message_utils import should_show_output  # noqa: E402


def init_managers():
    """
    初始化管理器：MessageManager → TaskManager

    事务执行由 TaskManager 使用 multiprocessing.Process 管理。
    """
    from peng.message import MessageManager

    # MessageManager 内部会创建 TaskManager
    message_manager = MessageManager()
    task_manager = message_manager.get_task_manager()

    return message_manager, task_manager


def show_chat_help(python_path: str, python_source: str) -> None:
    """显示聊天帮助信息"""
    from peng.ui.args import create_chat_parser

    # 使用 args.py 里的解析器显示帮助
    parser = create_chat_parser()
    parser.print_help()


def print_welcome(config=None):
    """打印欢迎信息"""
    console.print()
    console.print(
        Panel(
            f"[bold cyan]{AGENT_NAME}[/bold cyan] - 对话模式\n\n"
            "[dim]输入消息开始对话，或输入 /help 查看帮助[/dim]",
            title="[bold green]欢迎[/bold green]",
            border_style="grey30",
        )
    )
    # 显示当前模型信息（从配置直接读取）
    try:
        from peng.config.loader import load_config

        config = load_config()
        provider_data = config.llm.get_provider()
        if provider_data:
            console.print(f"[cyan]当前模型：[/cyan] {provider_data.get('provider', '未设置')}")
        else:
            console.print("[cyan]当前模型：[/cyan] 未设置")
    except Exception:
        console.print("[cyan]当前模型：[/cyan] 未设置")
    console.print()


def print_help():
    """打印帮助信息"""
    from rich.panel import Panel

    help_text = """
[bold cyan]快捷键:[/bold cyan]
  ↑/↓          浏览历史记录
  Ctrl+C       暂停/退出
  Ctrl+D       退出
  Ctrl+L       清屏

[bold cyan]命令:[/bold cyan]
  /help        显示帮助
  /exit, /quit 退出对话
  /clear       清屏
  /reset       重置对话
  /think       切换思考模式
  /new         开始新对话
  /continue    继续最近的消息
  /reply <id> <msg> 回复指定消息链
  /msg <args>  转发到 bin/msg
  /task <args> 转发到 bin/task
  /pause       暂停当前消息链处理
  /cancel      取消当前消息链处理
"""
    console.print(Panel(help_text, title="[bold]帮助[/bold]", border_style="blue"))


def run_chat(argv: list[str]) -> int:
    """运行聊天主函数"""
    import asyncio

    from peng.config.loader import is_auto_mode_prompt, load_config
    from peng.ui.args import ChatArgs, create_chat_parser

    # 解析参数
    parser = create_chat_parser()
    args = parser.parse_args(argv)
    chat_args = ChatArgs.from_namespace(args)

    # 检测是否为远程模式
    is_remote = chat_args.remote_host is not None

    try:
        # 加载配置
        config = load_config(chat_args.config)

        # 判断全自动模式
        is_non_interactive = chat_args.prompt is not None
        is_auto = (
            chat_args.auto or is_non_interactive or is_auto_mode_prompt(chat_args.prompt or "")
        )
        if is_auto:
            config.auto_mode.enabled = True

        if chat_args.no_stream:
            config.display.stream = False

        if not is_remote:
            from peng.llm.agent import PengAgent
            agent = PengAgent()

        # 无子命令的非交互模式已删除
        if chat_args.prompt:
            print(f"不支持的命令行参数 ${chat_args.prompt}")
            return 1

        # 交互模式
        if is_remote:
            return asyncio.run(_chat_remote_interactive_async(config, chat_args))
        else:
            return asyncio.run(_chat_interactive_async(agent, config, chat_args))

    except KeyboardInterrupt as e:
        print(f"\n已中断: {e}\n{traceback.format_exc()}")
        return 130
    except Exception as e:
        print(f"错误: {e}\n{traceback.format_exc()}", file=sys.stderr)
        return 1


async def _chat_once_async(agent, config, chat_args: ChatArgs) -> int:
    """单次对话（非交互模式）- 复用 message_utils 的 process_message/process_reply"""
    from peng.ui.session import ChatSession

    # 显示当前模型信息（从配置直接读取）
    try:
        from peng.config.loader import load_config

        cfg = load_config()
        provider_data = cfg.llm.get_provider()
        if provider_data:
            console.print(f"[cyan]当前模型：[/cyan] {provider_data.get('provider', '未设置')}")
    except Exception:
        pass

    # 创建会话（用于 --continue 支持）
    chat_session = ChatSession(caller=chat_args.caller)
    if chat_args.continue_last and chat_session.continue_latest():
        console.print(f"[cyan]继续消息链 #{chat_session.current_chain_head_id}[/cyan]")

    # 处理 -r/--reply 参数：直接回复指定消息链
    if chat_args.reply_to:
        console.print(f"[cyan]回复消息链 #{chat_args.reply_to}[/cyan]")
        return await process_reply(
            msg_id=chat_args.reply_to,
            content=chat_args.prompt,
            caller=chat_args.caller,
            workdir=chat_args.workdir,
            nest=chat_args.nest,
        )

    # 使用 prompt 参数（非交互模式）- 复用 process_message
    return await process_message(
        content=chat_args.prompt,
        caller="fly",  # 非交互模式使用 "fly"，禁用持续对话钩子
        workdir=chat_args.workdir,
        nest=chat_args.nest,
    )


async def _chat_remote_interactive_async(config, chat_args: ChatArgs) -> int:
    """远程交互式对话模式（通过 HTTP API 连接远程 peng service）

    在远端PC上使用：无需本地 PostgreSQL，所有消息操作通过远程 peng service 的 HTTP API 完成。
    """
    from prompt_toolkit import PromptSession
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.styles import Style

    from peng.ui.remote_chat import (
        RemoteChatClient,
        poll_for_remote_task,
        monitor_remote_task_output,
    )

    # 检测 stdin 是否是 TTY
    if not sys.stdin.isatty():
        console.print("[red]远程模式不支持管道输入[/red]")
        return 1

    # 创建远程客户端
    remote_host = chat_args.remote_host
    remote_port = chat_args.remote_port or 6688
    remote_client = RemoteChatClient(
        host=remote_host,
        port=remote_port,
        nest=chat_args.nest,
        workdir=chat_args.workdir,
    )

    # 测试连接
    try:
        remote_client.get_task(0)  # 随意请求，测试连接
    except ConnectionError as e:
        console.print(f"[red]无法连接到远程 peng service ({remote_host}:{remote_port}): {e}[/red]")
        return 1
    except RuntimeError:
        pass  # 404 是正常的（任务不存在）

    print_welcome(config)
    console.print(f"[dim]远程模式: {remote_host}:{remote_port} (nest: {chat_args.nest})[/dim]\n")

    # 历史和输入管理（与本地模式相同）
    try:
        history_file = config.paths.chat_history_file_path
    except Exception:
        history_file = PROJECT_ROOT / "data" / "chat_history.txt"
    ensure_dir_with_owner(history_file.parent)

    from peng.ui.input_history import InputHistory

    input_history_file = Path.home() / ".peng" / "input_history.json"
    input_history = InputHistory(history_file=input_history_file)
    input_history.load()

    kb = __import__("prompt_toolkit.key_binding", fromlist=["KeyBindings"]).KeyBindings()

    @kb.add("c-z")
    def _(event):
        buffer = event.app.current_buffer
        result = input_history.undo()
        if result:
            text, cursor_pos = result
            buffer.text = text
            buffer.cursor_position = cursor_pos

    @kb.add("c-y")
    def _(event):
        buffer = event.app.current_buffer
        result = input_history.redo()
        if result:
            text, cursor_pos = result
            buffer.text = text
            buffer.cursor_position = cursor_pos

    custom_style = Style.from_dict({"prompt": "#5f87ff", "": "#ffffff"})
    command_completer = WordCompleter(
        ["/help", "/exit", "/quit", "/clear", "/reset", "/new", "/continue"],
        ignore_case=True,
    )
    session: PromptSession[str] = PromptSession(
        history=FileHistory(str(history_file)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=command_completer,
        style=custom_style,
        enable_history_search=True,
        multiline=False,
        key_bindings=kb,
    )

    def on_text_changed(buffer):
        if not input_history._is_undoing:
            input_history.push(buffer.text, buffer.cursor_position)
    session.default_buffer.on_text_changed += on_text_changed

    current_chain_head_id: int | None = None
    _interrupt_count = 0

    def handle_sigint(signum, frame):
        nonlocal _interrupt_count
        # 远程模式：暂停当前监控中的任务（通过 Ctrl+C 在 monitor_remote_task_output 中处理）
        _interrupt_count += 1
        if _interrupt_count >= 2:
            print(f"\n{colorize('再见！', 'green')}")
            sys.exit(0)
        print(f"\n{colorize('再按一次 Ctrl+C 退出', 'bright_black')}")

    old_handler = signal.signal(signal.SIGINT, handle_sigint)

    try:
        while True:
            try:
                user_input = await session.prompt_async(
                    f"{get_master_name()} : ", style=custom_style
                )
                user_input = user_input.strip()
                _interrupt_count = 0

                if user_input:
                    input_history.push(user_input, len(user_input))
            except KeyboardInterrupt as e:
                _interrupt_count += 1
                if _interrupt_count >= 2:
                    console.print(f"\n[green]再见！[/green]")
                    sys.exit(0)
                console.print(f"\n[dim]再按一次 Ctrl+C 退出[/dim]")
                continue
            except EOFError:
                continue

            if not user_input:
                continue

            # 处理命令
            if user_input.startswith("/"):
                cmd = user_input.lower()
                if cmd in ("/exit", "/quit", "/q"):
                    console.print(f"[green]{AGENT_NAME} :[/green][bright_blue] 再见！[/bright_blue]")
                    sys.exit(0)
                elif cmd == "/clear":
                    console.clear()
                    print_welcome(config)
                    continue
                elif cmd == "/reset":
                    current_chain_head_id = None
                    console.print("[yellow]已重置对话[/yellow]")
                    continue
                elif cmd == "/help":
                    print_help()
                    continue
                elif cmd == "/new":
                    current_chain_head_id = None
                    console.print("[yellow]已开始新对话[/yellow]")
                    continue
                elif cmd == "/continue" or cmd == "/c":
                    console.print("[dim]远程模式不支持 /continue[/dim]")
                    continue
                else:
                    import os
                    cmd_text = user_input[1:]
                    if cmd_text.startswith("cd "):
                        dir_path = cmd_text[3:]
                        if os.path.isdir(dir_path):
                            os.chdir(dir_path)
                            remote_client.workdir = os.getcwd()
                            console.print(f"[dim]工作目录: {os.getcwd()}[/dim]")
                        else:
                            print(f"目录不存在：{dir_path}")
                    env = os.environ.copy()
                    os.system(cmd_text)
                    continue

            # 发送消息或回复
            try:
                if current_chain_head_id is None:
                    # 创建新消息
                    result = remote_client.send_message(user_input)
                    msg_id = result.get("id")
                    if msg_id and msg_id > 0:
                        print(f"📝 消息 #{msg_id} 已发送", flush=True)
                        current_chain_head_id = msg_id
                    else:
                        error = result.get("error", "未知错误")
                        print(f"❌ 创建消息失败: {error}", file=sys.stderr)
                        continue
                else:
                    # 回复消息
                    result = remote_client.reply_message(current_chain_head_id, user_input)
                    msg_id = result.get("id")
                    if msg_id and msg_id > 0:
                        print(f"📝 回复 #{msg_id} 已发送", flush=True)
                    else:
                        error = result.get("error", "未知错误")
                        print(f"❌ 回复消息失败: {error}", file=sys.stderr)
                        continue

                # 等待任务创建
                poll_result = await poll_for_remote_task(remote_client, msg_id, timeout_seconds=30)
                if poll_result is None:
                    print("❌ 消息未被处理，请检查peng服务是否在运行。", file=sys.stderr)
                    continue

                _, task_id = poll_result
                print(f"🔄 事务 #{task_id} 已开始处理", flush=True)

                # 监控远程任务输出
                await monitor_remote_task_output(remote_client, task_id)

            except asyncio.CancelledError:
                raise  # 让外层处理 Ctrl+C
            except ConnectionError as e:
                console.print(f"[red]连接远程 peng service 失败: {e}[/red]")
                current_chain_head_id = None
            except Exception as e:
                console.print(f"[red]错误: {e}[/red]")
                current_chain_head_id = None
    finally:
        input_history.save()
        signal.signal(signal.SIGINT, old_handler)

    return 0


async def _chat_interactive_async(agent, config, chat_args: ChatArgs) -> int:
    """交互式对话模式"""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.styles import Style

    from peng.ui.session import ChatSession

    message_manager, task_manager = init_managers()

    # 检测 stdin 是否是 TTY
    # 如果不是 TTY（如管道输入），从 stdin 读取内容作为 prompt，使用非交互模式
    if not sys.stdin.isatty():
        try:
            piped_input = sys.stdin.read().strip()
            if piped_input:
                # 使用非交互模式处理管道输入
                # 将管道输入设置为 prompt，调用非交互模式
                chat_args.prompt = piped_input
                return await _chat_once_async(agent, config, chat_args)
            # 没有管道输入，继续进入交互模式
        except Exception as e:
            console.print(f"[red]读取管道输入失败: {e}\n{traceback.format_exc()}[/red]")
            return 0

    print_welcome(config)

    # 🔧 注册全局output_reader客户端（用于整个会话）
    from .output_reader import get_output_reader

    global_output_reader = get_output_reader()

    # 输出队列
    global_output_queue = asyncio.Queue()

    # 回调函数：将输出放入队列
    async def global_output_callback(output_data: dict[str, Any]) -> None:
        await global_output_queue.put(output_data)

    # 注册全局客户端
    global_client_id = await global_output_reader.register_client(global_output_callback)
    _logger.info(f"[chat_interactive] 注册全局客户端: client_id={global_client_id}")

    chat_session = ChatSession(caller=chat_args.caller)
    if chat_args.continue_last and chat_session.continue_latest():
        console.print(f"[cyan]继续消息链 #{chat_session.current_chain_head_id}[/cyan]")

    config = load_config()
    history_file = config.paths.chat_history_file_path
    ensure_dir_with_owner(history_file.parent)

    # 🔧 创建输入历史管理器（用于 undo/redo）
    from peng.ui.input_history import InputHistory

    # 🔧 使用独立的持久化文件存储输入历史（支持跨会话无限undo）
    input_history_file = Path.home() / ".peng" / "input_history.json"
    input_history = InputHistory(history_file=input_history_file)
    # 加载持久化历史（包括之前所有会话的历史）
    input_history.load()

    # 🔧 创建自定义键绑定（Ctrl+Z/Ctrl+Y for undo/redo）
    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()

    @kb.add("c-z")
    def _(event):
        """Ctrl+Z: Undo"""
        buffer = event.app.current_buffer
        result = input_history.undo()
        if result:
            text, cursor_pos = result
            buffer.text = text
            buffer.cursor_position = cursor_pos

    @kb.add("c-y")
    def _(event):
        """Ctrl+Y: Redo"""
        buffer = event.app.current_buffer
        result = input_history.redo()
        if result:
            text, cursor_pos = result
            buffer.text = text
            buffer.cursor_position = cursor_pos

    custom_style = Style.from_dict({"prompt": "#5f87ff", "": "#ffffff"})
    command_completer = WordCompleter(
        [
            "/help",
            "/exit",
            "/quit",
            "/clear",
            "/reset",
            "/think",
            "/new",
            "/continue",
            "/pause",
            "/cancel",
        ],
        ignore_case=True,
    )

    session: PromptSession[str] = PromptSession(
        history=FileHistory(str(history_file)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=command_completer,
        style=custom_style,
        enable_history_search=True,
        multiline=False,
        key_bindings=kb,  # 🔧 使用自定义键绑定
    )

    # 🔧 绑定 on_text_changed 回调（记录字符变更，但 undo/redo 时不记录）
    def on_text_changed(buffer):
        if not input_history._is_undoing:
            input_history.push(buffer.text, buffer.cursor_position)

    session.default_buffer.on_text_changed += on_text_changed

    _interrupt_count = 0

    def handle_sigint(signum, frame):
        nonlocal _interrupt_count
        # 暂停所有运行中的事务
        # 注意：信号处理函数是同步的，不能调用async函数（cancel_task/pause_task），
        # 必须直接调用同步的stop_task方法
        running_tasks = task_manager.list_tasks(status="running")
        if running_tasks:
            for task in running_tasks:
                task_id = task.get("id")
                if task_id:
                    task_manager.stop_task(
                        task_id, recursive=True, keep_context=True, reason="用户中断"
                    )
            chat_session.pause()
            print(f"\n{colorize('⏸️ 已暂停当前对话', 'yellow')}")
        else:
            _interrupt_count += 1
            if _interrupt_count >= 2:
                print(f"\n{colorize('再见！', 'green')}")
                sys.exit(0)
            print(f"\n{colorize('再按一次 Ctrl+C 退出', 'bright_black')}")

    old_handler = signal.signal(signal.SIGINT, handle_sigint)

    try:
        while True:
            try:
                user_input = await session.prompt_async(
                    f"{get_master_name()} : ", style=custom_style
                )
                user_input = user_input.strip()
                _interrupt_count = 0

                # 🔧 记录提交的输入到历史（跨会话无限 undo）
                if user_input:
                    input_history.push(user_input, len(user_input))
            except KeyboardInterrupt as e:
                _interrupt_count += 1
                if _interrupt_count >= 2:
                    console.print(f"\n[green]再见！: {e}\n{traceback.format_exc()}[/green]")
                    sys.exit(0)
                    break
                console.print(f"\n[dim]再按一次 Ctrl+C 退出: {e}\n{traceback.format_exc()}[/dim]")
                continue
            except EOFError as e:
                continue

            if not user_input:
                continue

            # 处理命令
            if user_input.startswith("/"):
                cmd = user_input.lower()
                if cmd in ("/exit", "/quit", "/q"):
                    console.print(
                        f"[green]{get_agent_name()} :[/green][bright_blue] 再见！[/bright_blue]"
                    )
                    sys.exit(0)
                    break
                elif cmd == "/clear":
                    console.clear()
                    print_welcome(config)
                    continue
                elif cmd == "/reset":
                    agent.reset()
                    console.print("[yellow]已重置对话历史[/yellow]")
                    continue
                elif cmd == "/help":
                    print_help()
                    continue
                elif cmd == "/new":
                    # 等待事务功能已废弃，直接重置会话
                    agent.reset()
                    chat_session.start_new_chain()
                    console.print("[yellow]已开始新对话[/yellow]")
                    continue
                elif cmd == "/continue" or cmd == "/c":
                    if chat_session.continue_latest():
                        console.print(
                            f"[cyan]继续消息链 #{chat_session.current_chain_head_id}[/cyan]"
                        )
                    else:
                        console.print("[dim]没有最近的消息[/dim]")
                    continue
                elif cmd.startswith("/reply ") or cmd.startswith("/r "):
                    # /reply <id> <message> - 回复指定消息链
                    parts = user_input.split(maxsplit=2)
                    if len(parts) < 3:
                        console.print("[red]用法: /reply <消息ID> <回复内容>[/red]")
                        continue
                    try:
                        target_id = int(parts[1])
                        user_input = parts[2]
                        # 验证消息存在
                        target_msg = message_manager.get_message(target_id)
                        if not target_msg:
                            console.print(f"[red]消息 #{target_id} 不存在[/red]")
                            continue
                        # 设置当前链为目标消息的链头
                        chat_session.current_chain_head_id = target_id
                    except ValueError as e:
                        console.print(f"[red]消息ID必须是数字: {e}\n{traceback.format_exc()}[/red]")
                    except Exception as e:
                        console.print(f"[red]回复失败: {e}\n{traceback.format_exc()}[/red]")
                elif cmd.startswith("/msg "):
                    # /msg ... - 转发到 bin/msg
                    import subprocess

                    msg_args = user_input[5:].strip()
                    result = subprocess.run(
                        [sys.executable, str(PROJECT_ROOT / "bin" / "msg"), "--local"] + msg_args.split(),
                        capture_output=True,
                        text=True,
                        cwd=str(PROJECT_ROOT),
                        timeout=120,  # 防止垃圾进程卡死项目
                    )
                    console.print(result.stdout)
                    if result.stderr:
                        console.print(f"[red]{result.stderr}[/red]")
                    continue
                elif cmd.startswith("/task "):
                    # /task ... - 转发到 bin/task
                    import subprocess

                    task_args = user_input[6:].strip()
                    result = subprocess.run(
                        [sys.executable, str(PROJECT_ROOT / "bin" / "task"), "--local"] + task_args.split(),
                        capture_output=True,
                        text=True,
                        cwd=str(PROJECT_ROOT),
                        timeout=300,  # 防止垃圾进程卡死项目
                    )
                    console.print(result.stdout)
                    if result.stderr:
                        console.print(f"[red]{result.stderr}[/red]")
                    continue
                elif cmd == "/pause":
                    # /pause - 暂停当前消息链处理
                    if chat_session.current_chain_head_id:
                        # 取消所有运行中的事务
                        running_tasks = task_manager.list_tasks(
                            message_id=chat_session.current_chain_head_id,
                            status="running",
                            root_only=True,
                        )
                        for task in running_tasks:
                            task_id = task.get("id")
                            if task_id:
                                asyncio.run(
                                    task_manager.pause_task(
                                        task_id, recursive=True, reason="用户命令/pause"
                                    )
                                )
                        chat_session.pause()
                        console.print(
                            f"[yellow]⏸️ 已暂停消息链 #{chat_session.current_chain_head_id}[/yellow]"
                        )
                    else:
                        console.print("[dim]没有正在处理的消息链[/dim]")
                    continue
                elif cmd == "/cancel":
                    # /cancel - 取消当前消息链处理
                    if chat_session.current_chain_head_id:
                        # 取消所有运行中的事务
                        running_tasks = task_manager.list_tasks(
                            message_id=chat_session.current_chain_head_id,
                            status="running",
                            root_only=True,
                        )
                        for task in running_tasks:
                            task_id = task.get("id")
                            if task_id:
                                asyncio.run(
                                    task_manager.cancel_task(task_id, reason="用户命令/cancel")
                                )
                        chat_session.cancel()
                        console.print(
                            f"[red]⛔ 已取消消息链 #{chat_session.current_chain_head_id}[/red]"
                        )
                        chat_session.start_new_chain()
                    else:
                        console.print("[dim]没有正在处理的消息链[/dim]")
                    continue
                else:
                    import os

                    cmd = user_input[1:]
                    if cmd.startswith("cd "):
                        dir = cmd[3:]
                        if os.path.isdir(dir):
                            os.chdir(dir)
                        else:
                            print(f"目录不存在：{dir}")
                    env = os.environ.copy()
                    os.system(cmd)
                    continue

            if not chat_session.current_chain_head_id:
                # 交互模式：创建新消息，不等待处理（由peng服务异步处理）
                from peng.message import MessageManager

                msg_manager = MessageManager()
                msg_id, create_result = msg_manager.create_message(
                    content=user_input,
                    sender=get_master_name(),
                    receiver=get_agent_name(),
                    caller=chat_args.caller,
                    check_duplicate=False,
                    workdir=chat_args.workdir,
                    nest=chat_args.nest,
                )

                if msg_id and msg_id > 0:
                    print(f"📝 消息 #{msg_id} 已发送", flush=True)
                    chat_session.current_chain_head_id = msg_id
                else:
                    error = create_result.get("error", "未知错误") if create_result else "未知错误"
                    print(f"❌ 创建消息失败: {error}", file=sys.stderr)
            else:
                await process_reply(
                    chat_session.current_chain_head_id, content=user_input, caller=chat_args.caller,
                    workdir=chat_args.workdir, nest=chat_args.nest,
                )
    finally:
        # 🔧 保存输入历史
        input_history.save()
        signal.signal(signal.SIGINT, old_handler)

    return 0

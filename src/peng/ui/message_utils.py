"""
消息处理公共模块

提供 fly msg/reply 和 fly chat 非交互模式共享的消息处理逻辑：
- 创建消息/回复
- 轮询等待事务
- 监控事务输出

被 fly.py 和 chat.py 共同引用，不独立运行。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import traceback
from typing import Any

_logger = logging.getLogger(__name__)

# 轮询参数
_MSG_POLL_INTERVAL = 0.1  # 轮询间隔（秒）
_MSG_POLL_TIMEOUT = 30.0  # 轮询超时（秒）—— 足够覆盖 InboxMonitor 串行处理排队消息的延迟


def _resolve_workdir_from_nest(nest: str | None, workdir: str | None) -> str | None:
    """如果指定了 nest 但没有指定 workdir，从 NestBridgeManager 获取该 nest 的 workdir
    
    尝试两种方式获取：
    1. 从 NestBridgeManager（当在 peng service 进程内部运行时）
    2. 从 peng service 的 HTTP API（当以独立进程运行时）
    
    Args:
        nest: nest 名称（可选）
        workdir: 已指定的 workdir（可选）
    
    Returns:
        解析后的 workdir，如果无法解析则返回 None
    """
    if not nest or workdir:
        return workdir
    
    # 方式1：从 NestBridgeManager 获取（peng service 进程内部）
    try:
        from peng.nest.bridge import NestBridgeManager
        bridge = NestBridgeManager.get_instance()
        conn = bridge.get_connection(nest)
        if conn:
            nest_workdir = conn.info.get("workdir", "")
            if nest_workdir:
                _logger.info(f"从 NestBridgeManager 获取 nest '{nest}' 的 workdir: {nest_workdir}")
                return nest_workdir
    except Exception as e:
        _logger.debug(f"从 NestBridgeManager 获取 workdir 失败: {e}")
    
    # 方式2：从 peng service HTTP API 获取（独立进程）
    try:
        import urllib.request
        import json
        
        # 获取 peng service 地址
        peng_host = os.environ.get("PENG_HOST", "localhost")
        peng_port = int(os.environ.get("PENG_PORT", "6688"))
        url = f"http://{peng_host}:{peng_port}/api/nest/list"
        
        req = urllib.request.Request(url, method="GET")
        req.add_header("Content-Type", "application/json")
        
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            nests = data.get("nests", {})
            
            # 先精确匹配 display_key
            if nest in nests:
                nest_info = nests[nest]
                nest_workdir = nest_info.get("workdir", "")
                if nest_workdir:
                    _logger.info(f"从 peng service API 获取 nest '{nest}' 的 workdir: {nest_workdir}")
                    return nest_workdir
            
            # 再匹配 original_name
            for key, info in nests.items():
                if info.get("original_name") == nest:
                    nest_workdir = info.get("workdir", "")
                    if nest_workdir:
                        _logger.info(f"从 peng service API 获取 nest '{nest}' (key={key}) 的 workdir: {nest_workdir}")
                        return nest_workdir
    except Exception as e:
        _logger.debug(f"从 peng service API 获取 workdir 失败: {e}")
    
    return None


def should_show_output(
    source: str, disable_list: list = []
) -> tuple[bool, str]:
    """
    判断是否应该显示某个来源的输出。

    始终显示所有来源的输出，不再过滤。

    Args:
        source: 输出来源（thinking, bash, tool_call, tool_result, system, task, answer, llm 等）
        disable_list: 保留参数以向后兼容，默认为空列表（不过滤任何来源）

    Returns:
        (是否显示, 颜色代码)
    """
    # 显示所有来源的输出，不再过滤
    return True, "#888888"


async def _poll_for_task(msg_id: int) -> tuple[int, int] | None:
    """
    轮询等待消息对应的事务被创建并进入处理中状态。

    Args:
        msg_id: 消息ID

    Returns:
        (message_chain_id, task_id) 元组，超时返回 None
    """
    from peng.task.manager import TaskManager
    from peng.message.manager import MessageManager

    task_manager = TaskManager()
    message_manager = MessageManager()

    # 获取消息链首ID
    chain_head = message_manager.get_chain_head(msg_id)
    message_chain_id = chain_head.get("id", msg_id) if chain_head else msg_id

    start_time = time.monotonic()
    while time.monotonic() - start_time < _MSG_POLL_TIMEOUT:
        # 查询消息关联的事务
        tasks = task_manager.list_tasks(message_id=message_chain_id, root_only=True)
        for task in tasks:
            task_id = task.get("id")
            status = task.get("status", "")
            if task_id and status in ("running", "accepting"):
                return (message_chain_id, task_id)
        await asyncio.sleep(_MSG_POLL_INTERVAL)

    return None


async def _monitor_single_task(
    message_chain_id: int,
    task_id: int,
    auto_archive: bool = True,
    timeout_seconds: int = 3600,
    since_time: str | None = None,
    pause_on_cancel: bool = True,
    interactive_info: bool = False,
) -> int:
    """
    监控单个事务的输出并显示到终端。

    Args:
        message_chain_id: 消息链首ID
        task_id: 事务ID
        auto_archive: 是否在完成后自动归档
        timeout_seconds: 超时时间（秒）
        since_time: 起始时间（ISO格式），只显示此时间之后的输出
        pause_on_cancel: Ctrl+C 时是否暂停事务（False 时仅退出监控，不影响事务执行）
        interactive_info: 是否启用交互式信息注入（按回车暂停输出，输入文本后注入事务 info）

    Returns:
        message_chain_id
    """
    from peng.task.manager import TaskManager
    from peng.ui.output_reader import get_output_reader
    from peng.ui.rollback_display import annotate_rollback_info
    from peng.config.loader import get_agent_name

    task_manager = TaskManager()
    message_manager = task_manager.get_message_manager()
    output_reader = get_output_reader()

    # 创建输出队列和回调
    output_queue: asyncio.Queue = asyncio.Queue()

    async def output_callback(output_data: dict) -> None:
        await output_queue.put(output_data)

    client_id = await output_reader.register_client(output_callback)

    # 设置 stdin 后台线程（交互式信息注入模式）
    input_queue: asyncio.Queue | None = None
    if interactive_info and sys.stdin.isatty():
        import threading as _threading

        input_queue = asyncio.Queue()
        _loop = asyncio.get_running_loop()

        def _stdin_reader():
            """后台线程：逐行读取 stdin，通过线程安全的方式推入 asyncio Queue"""
            try:
                for line in sys.stdin:
                    _loop.call_soon_threadsafe(
                        input_queue.put_nowait, line.rstrip("\n\r")
                    )
            except Exception:
                pass

        _stdin_thread = _threading.Thread(target=_stdin_reader, daemon=True)
        _stdin_thread.start()
        print("💡 按回车暂停输出，可输入信息注入事务", flush=True)

    try:
        # 启动监控
        await output_reader.start_watching()

        # 订阅消息和事务（skip_history=True，只监控新输出）
        # 如果提供了 since_time，从该时间开始读取，避免因查询延迟漏掉输出
        await output_reader.update_subscription(
            client_id, [task_id], skip_history=True, since_time=since_time
        )

        # 监控新输出
        timeout = 3
        while True:
            # 交互式信息注入：检查用户是否按了回车
            if input_queue is not None and not input_queue.empty():
                _user_line = input_queue.get_nowait()
                # 用户按了回车，暂停输出，等待输入文本
                print("\n⏸️ 输出已暂停，事务继续运行", flush=True)
                print("⌨️ 输入信息（直接回车恢复输出）: ", end="", flush=True)

                _user_text = ""
                try:
                    _user_text = await asyncio.wait_for(
                        input_queue.get(), timeout=300
                    )
                except TimeoutError:
                    pass

                if _user_text:
                    # 非空文本：注入 info 到事务
                    from peng.common.utils import get_text_from_args

                    _user_text = get_text_from_args(_user_text)
                    try:
                        task_manager.db.add_task_info(task_id, _user_text)
                        print(f"\r✅ 已注入信息: {_user_text}", flush=True)
                        # 事务非 running 状态时插入 resume 控制信号
                        _t = task_manager.get_task(task_id)
                        _cur_status = _t.get("status", "") if _t else ""
                        if _cur_status != "running":
                            task_manager.db.insert_task_control(task_id, "resume")
                            print(
                                f"✅ 事务 #{task_id} 恢复控制已加入队列",
                                flush=True,
                            )
                    except Exception as _e:
                        print(f"\r❌ 注入失败: {_e}", flush=True)
                else:
                    print("\r▶️ 继续输出", flush=True)

                # 补全暂停期间积攒的输出
                while not output_queue.empty():
                    _pending = output_queue.get_nowait()
                    _content = _pending.get("content", "")
                    _source = _pending.get("source", "")
                    _show, _ = should_show_output(_source)
                    if _show:
                        # 回滚信息标注：行内含 OSC8 回滚链接时追加 [R<seq>] task rollback <task_id> R<seq>
                        print(annotate_rollback_info(_content), flush=True)

            try:
                output = await asyncio.wait_for(
                    output_queue.get(), timeout=timeout
                )  # 超时3秒应对output_reader输出延迟
                content = output.get("content", "")
                source = output.get("source", "")
                show, _color = should_show_output(source)
                if show:
                    # 回滚信息标注：行内含 OSC8 回滚链接时追加 [R<seq>] task rollback <task_id> R<seq>
                    print(annotate_rollback_info(content), flush=True)
                if source == get_agent_name():  # 收到回复消息则代表处理结束，
                    timeout = 0.1  # 接收剩余消息内容，然后超时结束
            except TimeoutError:
                # 检查事务是否完成
                task = task_manager.get_task(task_id)
                if task:
                    status = task.get("status", "")
                    if status not in ("running", "accepting"):
                        break
    except asyncio.CancelledError:
        if pause_on_cancel:
            # Ctrl+C 时 asyncio.run() 会取消协程，此处暂停当前监控的事务
            try:
                await task_manager.pause_task(task_id, reason="用户中断 (Ctrl+C)")
                if auto_archive:
                    message_manager.archive_message(message_chain_id)
                print(f"\r⏸️ 事务 #{task_id} 已暂停", flush=True)
            except Exception as e:
                _logger.warning(f"暂停事务 #{task_id} 时出错: {e}", exc_info=True)
                print(f"  ⚠️  暂停事务时出错: {e}", file=sys.stderr)
        else:
            # 仅退出监控，不暂停事务也不归档
            print(f"\r👋 退出监控，事务 #{task_id} 继续执行", flush=True)
        raise  # 重新抛出，让上层处理退出
    finally:
        await output_reader.unregister_client(client_id)
    print()  # 末尾空行

    if auto_archive:
        message_manager.archive_message(message_chain_id)
    return message_chain_id, task_id

async def process_message(
    content: str, caller: str = "fly_message", workdir: str | None = None, nest: str | None = None,
    interactive_info: bool = False,
) -> int:
    """
    非交互模式：发送新消息并监控输出。

    Args:
        content: 消息内容
        caller: 调用来源标识
        workdir: 工作目录（默认为当前目录）
        nest: 关联的nest名称（可选）
        interactive_info: 是否启用交互式信息注入

    Returns:
        退出码
    """
    from peng.message.manager import MessageManager
    from peng.config.loader import get_master_name, get_agent_name

    master_name = get_master_name()
    agent_name = get_agent_name()
    # 如果指定了 nest 但没有指定 workdir，从 NestBridgeManager 获取
    workdir = _resolve_workdir_from_nest(nest, workdir)
    if not workdir:
        workdir = os.getcwd()

    message_manager = MessageManager()

    # 创建新消息
    msg_id, result = message_manager.create_message(
        content=content,
        sender=master_name,
        receiver=agent_name,
        caller=caller,
        workdir=workdir,
        nest=nest,
        check_duplicate=False,
    )

    if msg_id == 0:
        error = result.get("error", "未知错误") if result else "未知错误"
        print(f"❌ 创建消息失败: {error}", file=sys.stderr)
        return 1

    print(f"📝 消息 #{msg_id} 已发送", flush=True)

    # 轮询等待事务
    poll_result = await _poll_for_task(msg_id)
    if poll_result is None:
        print(
            "❌ 消息未被处理，请检查peng服务是否在运行。",
            file=sys.stderr,
        )
        return 1

    message_chain_id, task_id = poll_result
    print(f"🔄 事务 #{task_id} 已开始处理", flush=True)

    # 获取消息创建时间，用于从该时间点开始显示输出
    msg_record = message_manager.get_message(msg_id)
    created_at = msg_record.get("created_at", "") if msg_record else ""

    # 监控事务输出
    return await _monitor_single_task(
        message_chain_id, task_id, auto_archive=True, since_time=created_at,
        interactive_info=interactive_info,
    )


async def process_reply(
    msg_id: int, content: str, caller: str = "fly_reply", workdir: str | None = None, nest: str | None = None,
    interactive_info: bool = False,
) -> int:
    """
    非交互模式：回复消息并监控输出。

    Args:
        msg_id: 要回复的消息ID
        content: 回复内容
        caller: 调用来源标识
        workdir: 工作目录（默认为当前目录）
        nest: 关联的nest名称（可选）
        interactive_info: 是否启用交互式信息注入

    Returns:
        退出码
    """
    from peng.message.manager import MessageManager
    from peng.config.loader import get_master_name, get_agent_name

    message_manager = MessageManager()
    master_name = get_master_name()
    agent_name = get_agent_name()

    if msg_id <= 1:
        recent_messages = message_manager.get_recent_messages(1)
        if recent_messages:
            last = recent_messages[-1]
            sender = last.get("sender")
            last_content = last.get("content")
            msg_id = last.get("chain_head_id") if msg_id == 0 else last.get("id")
            print(f"📜 继续上个话题： {sender} : {last_content}")
        else:
            return await process_message(content, caller)

    # 如果指定了 nest 但没有指定 workdir，从 NestBridgeManager 获取
    workdir = _resolve_workdir_from_nest(nest, workdir)
    if not workdir:
        workdir = os.getcwd()
    # 验证消息存在
    target_msg = message_manager.get_message(msg_id)
    if not target_msg:
        print(f"❌ 消息 #{msg_id} 不存在", file=sys.stderr)
        return 1


    from datetime import datetime
    start_time = datetime.now().isoformat()
    if caller == "fly_continue":
        tm = message_manager.get_task_manager()
        tasks = tm.list_tasks(message_id=msg_id, root_only=True)
        if tasks:
            task = tasks[0]
            os.system(f"task info {task.get("id")} {content}")
    else:
        # 创建回复消息
        new_msg_id, result = message_manager.create_message(
            content=content,
            sender=master_name,
            receiver=agent_name,
            caller=caller,
            reply_to=msg_id,
            workdir=workdir,
            nest=nest,
            check_duplicate=False,
        )

        if new_msg_id == 0:
            error = result.get("error", "未知错误") if result else "未知错误"
            print(f"❌ 回复消息失败: {error}", file=sys.stderr)
            return 1

        print(f"📝 回复 #{new_msg_id} 已发送（回复消息 #{msg_id}）", flush=True)

    # 轮询等待事务
    poll_result = await _poll_for_task(msg_id)
    if poll_result is None:
        print(
            "❌ 消息未被处理，请检查peng服务是否在运行。",
            file=sys.stderr,
        )
        return 1

    message_chain_id, task_id = poll_result
    print(f"🔄 事务 #{task_id} 已开始处理", flush=True)

    # 监控事务输出
    return await _monitor_single_task(
        message_chain_id, task_id, auto_archive=True, since_time=start_time,
        interactive_info=interactive_info,
    )

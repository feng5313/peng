"""
远程聊天客户端

通过 HTTP API 连接远程 peng service，支持：
- 发送消息/回复消息
- 轮询任务状态和输出
- 实时显示任务输出到终端

使用场景：在远端PC（无本地PostgreSQL）上运行 fly chat，
通过远程 peng service 的 HTTP API 进行消息操作。
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
import urllib.error
from typing import Any

logger = logging.getLogger(__name__)


class RemoteChatClient:
    """远程聊天客户端，通过 HTTP API 与 peng service 通信"""

    def __init__(self, host: str, port: int = 6688, nest: str | None = None, workdir: str | None = None):
        """
        初始化远程聊天客户端

        Args:
            host: peng service 地址
            port: peng service HTTP 端口（默认 6688）
            nest: nest 名称（自动注入到每个消息）
            workdir: 工作目录（自动注入到每个消息）
        """
        self.host = host
        self.port = port
        self.nest = nest
        self.workdir = workdir
        self.base_url = f"http://{host}:{port}"

    def _request(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        """发送 HTTP 请求

        Args:
            method: HTTP 方法
            path: API 路径（不含 /api 前缀）
            data: 请求体
            timeout: 超时秒数

        Returns:
            响应 JSON

        Raises:
            ConnectionError: 连接失败
            RuntimeError: 服务端返回错误
        """
        url = f"{self.base_url}/api{path}"
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode("utf-8")
            except Exception:
                pass
            error_detail = ""
            try:
                error_data = json.loads(body_text)
                error_detail = error_data.get("detail", body_text)
            except Exception:
                error_detail = body_text
            raise RuntimeError(f"HTTP {e.code}: {error_detail}") from e
        except urllib.error.URLError as e:
            raise ConnectionError(f"无法连接到 peng service ({self.host}:{self.port}): {e}") from e

    def send_message(self, content: str) -> dict[str, Any]:
        """发送新消息

        Args:
            content: 消息内容

        Returns:
            响应 JSON，包含 id 等
        """
        from peng.config import get_master_name, get_agent_name
        payload: dict[str, Any] = {
            "content": content,
            "from_": get_master_name(),
            "to": get_agent_name(),
        }
        if self.nest:
            payload["nest"] = self.nest
        if self.workdir:
            payload["workdir"] = self.workdir
        return self._request("POST", "/cli/msg/send", data=payload)

    def reply_message(self, msg_id: int, content: str) -> dict[str, Any]:
        """回复消息

        Args:
            msg_id: 要回复的消息ID
            content: 回复内容

        Returns:
            响应 JSON
        """
        from peng.config import get_master_name, get_agent_name
        payload: dict[str, Any] = {
            "content": content,
            "from_": get_master_name(),
            "to": get_agent_name(),
        }
        if self.nest:
            payload["nest"] = self.nest
        if self.workdir:
            payload["workdir"] = self.workdir
        return self._request("POST", f"/cli/msg/{msg_id}/reply", data=payload)

    def get_task(self, task_id: int) -> dict[str, Any]:
        """获取任务信息

        Args:
            task_id: 任务ID

        Returns:
            任务信息 JSON
        """
        return self._request("GET", f"/cli/task/{task_id}")

    def get_task_output(
        self,
        task_id: int,
        begin: int | None = None,
        end: int | None = None,
    ) -> dict[str, Any]:
        """获取任务输出

        Args:
            task_id: 任务ID
            begin: 起始行号
            end: 结束行号

        Returns:
            输出信息 JSON
        """
        params = []
        if begin is not None:
            params.append(f"begin={begin}")
        if end is not None:
            params.append(f"end={end}")
        path = f"/cli/task/{task_id}/output"
        if params:
            path += "?" + "&".join(params)
        return self._request("GET", path)

    def pause_task(self, task_id: int) -> dict[str, Any]:
        """暂停任务

        Args:
            task_id: 任务ID

        Returns:
            操作结果
        """
        return self._request("POST", f"/cli/task/{task_id}/control", data={"action": "pause"})

    def cancel_task(self, task_id: int) -> dict[str, Any]:
        """取消任务

        Args:
            task_id: 任务ID

        Returns:
            操作结果
        """
        return self._request("POST", f"/cli/task/{task_id}/control", data={"action": "cancel"})

    def get_message_tasks(self, msg_id: int) -> dict[str, Any]:
        """获取消息关联的任务列表

        Args:
            msg_id: 消息ID

        Returns:
            任务列表 JSON
        """
        return self._request("GET", f"/cli/msg/{msg_id}/tasks")


async def poll_for_remote_task(
    client: RemoteChatClient,
    msg_id: int,
    timeout_seconds: int = 30,
    poll_interval: float = 1.0,
) -> tuple[int, int] | None:
    """轮询等待远程任务创建或重启

    对于新消息：等待 inbox_monitor 创建任务
    对于回复消息：获取链首消息ID，等待链首任务重启

    Args:
        client: 远程聊天客户端
        msg_id: 消息ID
        timeout_seconds: 超时秒数
        poll_interval: 轮询间隔

    Returns:
        (message_chain_id, task_id) 或 None
    """
    elapsed = 0.0
    while elapsed < timeout_seconds:
        try:
            # 先尝试直接查找此消息关联的任务（新消息场景）
            result = client.get_message_tasks(msg_id)
            tasks = result.get("tasks", [])
            if tasks:
                task = tasks[0]
                task_id = task.get("id")
                chain_id = task.get("message_id", msg_id)
                return chain_id, task_id

            # 如果没有找到，可能是回复消息
            # 获取消息详情以找到链首ID
            msg_detail = client._request("GET", f"/cli/msg/{msg_id}")
            chain_head_id = msg_detail.get("chain_head_id") or msg_detail.get("reply_to")
            if chain_head_id and chain_head_id != msg_id:
                # 查找链首关联的任务
                result = client.get_message_tasks(chain_head_id)
                tasks = result.get("tasks", [])
                if tasks:
                    task = tasks[0]
                    task_id = task.get("id")
                    # 检查任务是否正在运行（handle_reply 会重启链首任务）
                    status = task.get("status", "")
                    if status in ("running", "accepting"):
                        return chain_head_id, task_id
        except Exception as e:
            logger.debug(f"轮询任务失败: {e}")

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    return None


async def monitor_remote_task_output(
    client: RemoteChatClient,
    task_id: int,
    timeout_seconds: int = 3600,
) -> None:
    """监控远程任务输出并显示到终端

    Args:
        client: 远程聊天客户端
        task_id: 任务ID
        timeout_seconds: 最大等待秒数
    """
    from peng.ui.message_utils import should_show_output

    last_line = 0
    elapsed = 0.0
    poll_interval = 0.5
    finished = False

    try:
        while elapsed < timeout_seconds and not finished:
            try:
                # 使用 begin 参数只获取新增行（注意 API 的 begin 是 1-based）
                api_begin = last_line + 1 if last_line > 0 else None
                result = client.get_task_output(task_id, begin=api_begin)
                lines = result.get("lines", [])
                total_lines = result.get("total_lines", 0)

                if lines:
                    for line_data in lines:
                        line_num = line_data.get("line_num", 0)
                        content = line_data.get("content", "")
                        # 解析 TSV 格式: timestamp\tsource\ttask_id\tactual_content
                        parts = content.split("\t", 3)
                        source = parts[1] if len(parts) >= 2 else ""
                        actual_content = parts[3] if len(parts) >= 4 else content

                        show, _color = should_show_output(source)
                        if show:
                            print(actual_content)
                        last_line = line_num

                # 检查任务状态
                try:
                    task_info = client.get_task(task_id)
                    status = task_info.get("status", "")
                    if status not in ("running", "accepting", "pending"):
                        finished = True
                        # 获取剩余输出
                        api_begin = last_line + 1 if last_line > 0 else None
                        result = client.get_task_output(task_id, begin=api_begin)
                        remaining_lines = result.get("lines", [])
                        if remaining_lines:
                            for line_data in remaining_lines:
                                content = line_data.get("content", "")
                                parts = content.split("\t", 3)
                                source = parts[1] if len(parts) >= 2 else ""
                                actual_content = parts[3] if len(parts) >= 4 else content
                                show, _color = should_show_output(source)
                                if show:
                                    print(actual_content)
                        break
                except Exception:
                    pass

            except Exception as e:
                logger.debug(f"获取远程输出失败: {e}")

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
    except asyncio.CancelledError:
        # Ctrl+C: 暂停远程任务
        try:
            client.pause_task(task_id)
            print(f"\r⏸️ 事务 #{task_id} 已暂停", flush=True)
        except Exception as e:
            logger.warning(f"暂停远程任务失败: {e}")
        raise

    print()  # 末尾空行

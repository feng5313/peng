"""
Nest 工具

提供远程窗口查询、截屏、输入和nest管理功能，通过 nest 服务实现。

工具列表：
- WindowTool: 查询程序窗口列表
- ShotTool: 窗口/全屏/指定区域截屏（保存到 nest 端 data/shot 目录）
- InputTool: 远程鼠标/键盘输入
- NestTool: 获取所有nest信息（标明自身）、远程执行命令、传输文件
"""

from __future__ import annotations

import logging
import os
import ntpath
import subprocess
import time
import json
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

from peng.tools.base import DualOutput, Tool, ToolError

logger = logging.getLogger(__name__)


def _local_side_platform() -> str:
    """本端（工具进程所在主机）平台标识，与 nest list 显示口径一致"""
    return "Windows" if os.name == "nt" else "Linux"


def _path_platform_mismatch(path: str, side_platform: str) -> str | None:
    """护栏：路径形态与其解析端平台交叉时返回拒绝原因，一致/不确定返回 None。

    🔧 事务#2251 斩断来源：pull/push/sync 的 local_path 在本机解析、remote_path
    在目标 nest 端解析；跨平台绝对路径传错端时在该端只是**相对路径**，此前被
    静默 mkdir/open 在工作目录根实体化（"D:\\work\\fcc\\x" 在 Linux host 上创建了
    垃圾目录 D:/）。现在明确拒绝并提示正确用法。UNC/无盘符路径不判定（避免误伤）。
    """
    p = str(path).strip()
    if not p:
        return None
    drive, _ = ntpath.splitdrive(p.replace("\\", "/"))
    has_drive = len(drive) == 2 and drive[1] == ":"
    plat = (side_platform or "").lower()
    if plat.startswith("win"):
        # Windows 端：盘符路径与 "/x"（nt 视为当前驱动器根）均合法，不拒绝
        return None
    if plat:
        # POSIX 端（Linux/macOS）：带盘符路径只是相对路径，明确拒绝（事务#2251 事故形态）
        if has_drive and not os.path.isabs(p):
            return f"POSIX 端路径不应是 Windows 盘符路径（会按相对路径静默落在 cwd）: {p}"
    return None


def _check_transfer_paths(nest_name: str, local_path: str, remote_path: str) -> None:
    """校验传输两端的平台交叉路径，不合法抛 ToolError（空值=默认行为，不校验）"""
    if local_path:
        reason = _path_platform_mismatch(local_path, _local_side_platform())
        if reason:
            raise ToolError(
                f"local_path 平台交叉: {reason}。local_path 在本实例主机"
                f"（{_local_side_platform()}）解析，请使用该主机上有效的绝对路径。"
            )
    if remote_path and nest_name:
        try:
            from peng.tools._nest_helper import get_nest_platform

            plat = get_nest_platform(nest_name)
        except Exception:
            plat = ""
        if plat:
            reason = _path_platform_mismatch(remote_path, plat)
            if reason:
                raise ToolError(
                    f"remote_path 平台交叉: {reason}。remote_path 在目标 nest"
                    f"（{nest_name}，平台 {plat}）解析，请使用该平台有效的绝对路径。"
                )


class WindowTool(Tool):
    """窗口查询工具 - 通过 nest 远程查询程序窗口"""

    @property
    def name(self) -> str:
        return "window"

    @property
    def description(self) -> str:
        return """查询远程系统上的程序窗口列表。

通过 nest 服务查询远程操作系统上正在运行的窗口信息。

使用场景：
1. 查看远程系统上运行了哪些程序窗口
2. 获取窗口标题、进程ID等信息用于截屏
3. 根据程序名或窗口标题过滤窗口

参数：
- query: 程序名或窗口标题的过滤条件（可选）"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "程序名或窗口标题的过滤条件（可选，不指定则返回所有窗口）",
                },
            },
            "required": [],
        }

    async def execute(self, **kwargs: Any) -> DualOutput:
        """执行窗口查询"""
        query = kwargs.get("query", "")

        # 获取 nest 客户端
        client = self._get_nest_client()
        if not client:
            raise ToolError("Nest 客户端未配置，无法查询窗口")

        try:
            result = await client.list_windows(query)
            windows = result.get("windows", [])

            if not windows:
                if query:
                    return DualOutput(
                        summary=f"未找到匹配 '{query}' 的窗口",
                        display=f"未找到匹配 '{query}' 的窗口",
                    )
                return DualOutput(
                    summary="当前没有可见窗口",
                    display="当前没有可见窗口",
                )

            # 格式化输出
            lines = [f"找到 {len(windows)} 个窗口：\n"]
            for i, win in enumerate(windows, 1):
                title = win.get("title", "无标题")
                pid = win.get("pid", "?")
                lines.append(f"  {i}. [PID:{pid}] {title}")

            output = "\n".join(lines)
            return DualOutput(summary=output, display=output)

        except Exception as e:
            raise ToolError(f"查询窗口失败: {e}") from e

    def _get_nest_client(self):
        """获取 nest 客户端（从工具上下文获取 nest 名称和 task_id）"""
        from peng.nest.client import get_nest_client

        client = get_nest_client()
        nest_name = self.get_context("nest")
        if nest_name:
            client.set_nest_name(nest_name)
        client.set_task_id(self.get_context("task_id"))
        return client


class ShotTool(Tool):
    """截屏工具 - 通过 nest 远程截取窗口或全屏截图"""

    @property
    def name(self) -> str:
        return "shot"

    @property
    def description(self) -> str:
        return """截取远程系统上的屏幕截图。

通过 nest 服务截取远程操作系统的屏幕截图。

支持以下模式：
1. 全屏截屏：不指定 window 和 region 参数
2. 窗口截屏：指定 window 参数（窗口标题）
3. 区域截屏：指定 region 参数 {"x":0,"y":0,"width":800,"height":600}

参数：
- window: 窗口标题（可选，指定则截取该窗口）
- region: 截取区域，格式 {"x":0,"y":0,"width":800,"height":600}（可选）
- format: 输出格式 "png"（默认）或 "jpg"
- scale: 缩放比例 0-1（可选，默认自动缩放）"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "window": {
                    "type": "string",
                    "description": "窗口标题（可选，截取指定窗口）",
                },
                "region": {
                    "type": "object",
                    "description": "截取区域 {'x':0,'y':0,'width':800,'height':600}",
                    "properties": {
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                    },
                },
                "format": {
                    "type": "string",
                    "description": "输出格式 png（默认）或 jpg",
                    "default": "png",
                },
                "scale": {
                    "type": "number",
                    "description": "缩放比例 0-1（可选）",
                },
            },
            "required": [],
        }

    async def execute(self, **kwargs: Any) -> DualOutput:
        """执行截屏 - 文件保存到 nest 端 data/shot 目录，返回远程路径"""
        window = kwargs.get("window")
        region = kwargs.get("region")
        fmt = kwargs.get("format", "png")

        # 获取 nest 客户端
        client = self._get_nest_client()
        if not client:
            raise ToolError("Nest 客户端未配置，无法截屏")

        try:
            # 生成 nest 端保存路径
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            ext = fmt if fmt in ("png", "jpg", "jpeg", "bmp", "gif", "webp") else "png"
            filename = f"shot_{timestamp}.{ext}"

            # 获取 shot_dir 配置（相对于 nest 端 project_root）
            shot_dir = self._get_shot_dir()
            save_path = os.path.join(shot_dir, filename) if shot_dir else filename

            # 调用 nest 端截屏，指定 save_path 让 nest 端保存文件
            result = await client.screenshot(
                window=window, region=region, fmt=fmt, save_path=save_path
            )

            nest_file_path = result.get("file_path")
            mime = result.get("mime", f"image/{fmt}")

            if not nest_file_path:
                raise ToolError("截屏返回空数据")

            logger.info(f"截图已保存到 nest 端: {nest_file_path}")

            # 描述截屏内容
            if window:
                desc = f"窗口 '{window}' 截屏"
            elif region:
                desc = f"区域截屏 ({region.get('width', '?')}x{region.get('height', '?')}+{region.get('x', 0)},{region.get('y', 0)})"
            else:
                desc = "全屏截屏"

            return DualOutput(
                summary=f"{desc}成功，文件已保存至: {nest_file_path}",
                display=f"📸 {desc}成功，文件: {nest_file_path}",
                attachments=[
                    {
                        "type": "file",
                        "mime": mime,
                        "url": f"nest://{nest_file_path}",
                    }
                ],
            )

        except Exception as e:
            raise ToolError(f"截屏失败: {e}") from e

    def _get_shot_dir(self) -> str:
        """获取 nest 端的 shot 保存目录（相对路径，由 nest 端基于 project_root 解析）"""
        return "data/shot"

    def _get_nest_client(self):
        """获取 nest 客户端（从工具上下文获取 nest 名称和 task_id）"""
        from peng.nest.client import get_nest_client

        client = get_nest_client()
        nest_name = self.get_context("nest")
        if nest_name:
            client.set_nest_name(nest_name)
        client.set_task_id(self.get_context("task_id"))
        return client


class InputTool(Tool):
    """远程输入工具 - 通过 nest 发送鼠标/键盘输入到远程窗口"""

    @property
    def name(self) -> str:
        return "input"

    @property
    def description(self) -> str:
        return """向远程系统上的窗口发送鼠标和键盘输入。

通过 nest 服务向远程操作系统的指定窗口发送输入事件。

鼠标操作：
- mouse_move: 移动鼠标到指定坐标 {"type":"mouse_move","x":100,"y":200}
- mouse_click: 点击鼠标 {"type":"mouse_click","x":100,"y":200,"button":"left","clicks":1}
- mouse_scroll: 滚动鼠标 {"type":"mouse_scroll","x":100,"y":200,"delta":3}

键盘操作：
- key_press: 按下并释放按键 {"type":"key_press","key":"enter"}
- key_down: 按下按键 {"type":"key_down","key":"ctrl"}
- key_up: 释放按键 {"type":"key_up","key":"ctrl"}
- type_text: 输入文本 {"type":"type_text","text":"hello"}

组合键用+连接，如 "ctrl+a", "alt+f4"

参数：
- actions: 动作列表，每个动作是一个字典（必填）
- window: 目标窗口标题，支持模糊匹配（可选，不指定则在当前焦点窗口操作）
- activate: 是否激活目标窗口，默认 false（不激活）"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "description": "输入动作列表",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": [
                                    "mouse_move",
                                    "mouse_click",
                                    "mouse_scroll",
                                    "key_press",
                                    "key_down",
                                    "key_up",
                                    "type_text",
                                ],
                                "description": "动作类型",
                            },
                            "x": {"type": "integer", "description": "X坐标（鼠标操作）"},
                            "y": {"type": "integer", "description": "Y坐标（鼠标操作）"},
                            "button": {
                                "type": "string",
                                "enum": ["left", "middle", "right"],
                                "description": "鼠标按钮（默认left）",
                            },
                            "clicks": {"type": "integer", "description": "点击次数（默认1）"},
                            "delta": {
                                "type": "integer",
                                "description": "滚动量（正=向上，负=向下）",
                            },
                            "key": {"type": "string", "description": "按键名（键盘操作）"},
                            "text": {"type": "string", "description": "要输入的文本（type_text）"},
                        },
                        "required": ["type"],
                    },
                },
                "window": {
                    "type": "string",
                    "description": "目标窗口标题（可选，支持模糊匹配）",
                },
                "activate": {
                    "type": "boolean",
                    "description": "是否激活目标窗口（默认不激活）",
                    "default": False,
                },
            },
            "required": ["actions"],
        }

    async def execute(self, **kwargs: Any) -> DualOutput:
        """执行远程输入"""
        actions = kwargs.get("actions", [])
        window = kwargs.get("window")
        activate = kwargs.get("activate", False)

        if not actions:
            raise ToolError("必须提供至少一个输入动作")

        # 获取 nest 客户端
        client = self._get_nest_client()
        if not client:
            raise ToolError("Nest 客户端未配置，无法发送远程输入")

        try:
            result = await client.send_input(
                actions=actions,
                window=window,
                activate=activate,
            )

            results = result.get("results", [])
            success_count = sum(1 for r in results if r.get("success"))
            fail_count = len(results) - success_count

            lines = [f"远程输入完成: {success_count} 成功"]
            if fail_count:
                lines.append(f"  {fail_count} 失败")
            if window:
                lines.append(f"  目标窗口: {window}")
                if activate:
                    lines.append("  窗口已激活")

            # 列出每个动作结果
            for i, r in enumerate(results, 1):
                status = "OK" if r.get("success") else f"FAIL: {r.get('error', '未知错误')}"
                action_type = r.get("type", "?")
                detail = ""
                if "x" in r and "y" in r:
                    detail = f" ({r['x']},{r['y']})"
                elif "key" in r:
                    detail = f" [{r['key']}]"
                lines.append(f"  {i}. {action_type}{detail}: {status}")

            output = "\n".join(lines)
            return DualOutput(summary=output, display=output)

        except Exception as e:
            raise ToolError(f"远程输入失败: {e}") from e

    def _get_nest_client(self):
        """获取 nest 客户端（从工具上下文获取 nest 名称和 task_id）"""
        from peng.nest.client import get_nest_client

        client = get_nest_client()
        nest_name = self.get_context("nest")
        if nest_name:
            client.set_nest_name(nest_name)
        client.set_task_id(self.get_context("task_id"))
        return client


class NestTool(Tool):
    """Nest管理工具 - 获取所有nest信息、在远程nest执行命令、传输文件"""

    @property
    def name(self) -> str:
        return "nest"

    @property
    def description(self) -> str:
        return """获取所有nest信息、在远程nest上运行命令、与远程nest传输文件。

通过nest服务管理远程nest连接。

操作类型：
1. list: 列出所有nest连接信息（标明哪个是自身nest）
2. exec: 在指定nest上运行bash命令，禁止用nest exec操作自身nest（应直接调用read/edit/write/bash工具）！
3. pull: 从远程nest传输文件到本地nest
4. push: 推送本地文件/目录到远程nest（二进制安全，目录递归）
5. sync: 目录递归同步（增量mtime比对，支持push/pull方向、--delete镜像、dry-run预演）

参数：
- action: 操作类型 list/exec/pull/push/sync（必填）
- nest: 目标nest名称（exec/pull/push/sync时必填）
- command: bash命令（exec时必填）
- remote_path: 远程nest上的文件/目录路径（pull/push/sync时必填，须为目标平台有效的绝对路径）
- local_path: 本地文件/目录路径，在本实例主机解析（pull时可选默认当前工作目录；push/sync时必填；须为本机平台有效的绝对路径）
- direction: 同步方向 push（默认，本地→远端）或 pull（远端→本地），仅sync
- delete: 镜像删除目标端多余文件，仅sync（可选，默认false）
- dry_run: 只预演不实际传输，仅sync（可选，默认false）"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "exec", "pull", "push", "sync"],
                    "description": "操作类型：list=列出所有nest, exec=远程执行命令, pull=拉取文件, push=推送文件, sync=目录递归同步。",
                },
                "nest": {
                    "type": "string",
                    "description": "目标nest的display_key（exec/pull/push/sync时必填）",
                },
                "command": {
                    "type": "string",
                    "description": "要在远程nest上执行的bash命令（exec时必填）",
                },
                "remote_path": {
                    "type": "string",
                    "description": "远程nest上的文件/目录路径（pull/push/sync时必填；须为目标nest平台有效的绝对路径；push目录时为远端目标目录）",
                },
                "local_path": {
                    "type": "string",
                    "description": "本地文件/目录路径，在本实例主机解析（pull时可选默认当前工作目录；push/sync时必填；须为本机平台有效的绝对路径，跨平台路径将被拒绝）",
                },
                "direction": {
                    "type": "string",
                    "enum": ["push", "pull"],
                    "description": "sync同步方向：push=本地→远端（默认），pull=远端→本地",
                },
                "delete": {
                    "type": "boolean",
                    "description": "sync时删除目标端源端没有的文件（镜像模式，默认false）",
                    "default": False,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "sync时只预演变更不实际传输（默认false）",
                    "default": False,
                },
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> DualOutput:
        """执行nest操作"""
        action = kwargs.get("action", "")
        if action == "list":
            return await self._list_nests()
        elif action == "exec":
            return await self._exec_command(kwargs)
        elif action == "pull":
            return await self._pull_file(kwargs)
        elif action == "push":
            return await self._push_file(kwargs)
        elif action == "sync":
            return await self._sync_tree(kwargs)
        else:
            raise ToolError(f"不支持的操作: {action}，支持: list/exec/pull/push/sync")

    def _get_webui_port(self) -> int:
        """获取webui端口"""
        try:
            from peng.config.loader import load_config

            config = load_config()
            if hasattr(config, "webui") and config.webui:
                return config.webui.port
        except Exception:
            pass
        return 6688

    def _api_get(self, path: str) -> dict:
        """通过HTTP GET调用本地webui API"""
        port = self._get_webui_port()
        url = f"http://localhost:{port}{path}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _api_post(self, path: str, data: dict) -> dict:
        """通过HTTP POST调用本地webui API"""
        port = self._get_webui_port()
        url = f"http://localhost:{port}{path}"
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    async def _list_nests(self) -> DualOutput:
        """列出所有nest连接（通过HTTP API）"""
        try:
            result = self._api_get("/api/nest/list")
            nests = result.get("nests", {})
            default_nest = result.get("default_nest", "local")

            if not nests:
                return DualOutput(
                    summary="当前没有已连接的nest",
                    display="当前没有已连接的nest",
                )

            lines = [f"已连接 {len(nests)} 个nest：\n"]
            for key, info in nests.items():
                connected = info.get("connected", False)
                platform = info.get("platform", "?")
                hostname = info.get("hostname", "?")
                workdir = info.get("workdir", "")
                is_self = " ← 自身" if key == default_nest else ""
                status = "🟢" if connected else "🔴"
                lines.append(f"  • {status} {key} ({platform}/{hostname}){is_self}")
                if workdir:
                    lines.append(f"    工作目录: {workdir}")

            output = "\n".join(lines)
            return DualOutput(summary=output, display=output)
        except Exception as e:
            raise ToolError(f"列出nest失败: {e}") from e

    async def _exec_command(self, kwargs: dict) -> DualOutput:
        """在远程nest上执行命令（通过HTTP API）"""
        nest_name = kwargs.get("nest", "")
        command = kwargs.get("command", "")

        if not nest_name:
            raise ToolError("exec操作需要提供nest参数（目标nest名称）")
        if not command:
            raise ToolError("exec操作需要提供command参数（bash命令）")

        # 禁止用 nest exec 操作自身 nest
        try:
            result = self._api_get("/api/nest/list")
            default_nest = result.get("default_nest", "")
            if default_nest and nest_name == default_nest:
                self_nest_info = result.get("nests", {}).get(default_nest, {})
                self_platform = self_nest_info.get("platform", "?")
                raise ToolError(
                    f"禁止用nest exec操作自身（{nest_name}，平台：{self_platform}），应直接调用read/edit/write/bash工具！"
                )
        except ToolError:
            raise
        except Exception:
            pass  # 获取自身nest失败时不阻塞，继续执行

        try:
            params = urllib.parse.urlencode({"nest": nest_name, "command": command, "timeout": 60})
            url = f"/api/nest/exec?{params}"
            resp = self._api_post(url, {})

            if not resp.get("success"):
                raise ToolError(f"远程执行失败: {resp.get('error', 'unknown')}")

            exit_code = resp.get("exit_code", -1)
            stdout = resp.get("stdout", "")
            stderr = resp.get("stderr", "")

            output = f"[{nest_name}]$ {command}\n"
            if stdout:
                output += stdout
            if stderr:
                output += f"\nSTDERR: {stderr}"
            output += f"\n退出码: {exit_code}"

            return DualOutput(summary=output, display=output)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"远程命令执行失败(nest={nest_name}): {e}") from e

    def _make_transport(self, nest_name: str):
        """构建传输 transport（基于 nest client 协议请求）"""
        from peng.nest.client import send_request as _client_send_request

        class _Transport:
            async def request(self, request_type: str, params: dict, timeout: float = 120.0):
                return await _client_send_request(
                    request_type, params, timeout=timeout, nest_name=nest_name
                )

        return _Transport()

    async def _pull_file(self, kwargs: dict) -> DualOutput:
        """从远程nest拉取文件/目录（协议分块传输，二进制安全，目录递归）"""
        from peng.nest.transfer import TransferError, pull_file, sync_tree

        nest_name = kwargs.get("nest", "")
        remote_path = kwargs.get("remote_path", "")
        local_path = kwargs.get("local_path", "")

        if not nest_name:
            raise ToolError("pull操作需要提供nest参数（远程nest名称）")
        if not remote_path:
            raise ToolError("pull操作需要提供remote_path参数（远程文件路径）")
        _check_transfer_paths(nest_name, local_path, remote_path)

        transport = self._make_transport(nest_name)

        # 先 stat 判断远端是文件还是目录
        stat_resp = await transport.request("stat", {"path": remote_path}, timeout=30.0)
        if stat_resp.error:
            raise ToolError(f"远端路径不可访问: {stat_resp.error}")
        is_dir = bool((stat_resp.data or {}).get("is_dir"))

        try:
            lines = []
            if is_dir:
                if not local_path:
                    local_path = os.path.join(os.getcwd(), os.path.basename(remote_path.rstrip("/\\")) or "pull_dir")
                stats = await sync_tree(transport, local_path, remote_path, direction="pull")
                lines.append(f"✅ 目录已从 {nest_name}:{remote_path} 同步到 {local_path}")
                lines.append(
                    f"   检查 {stats['checked']} 个文件: 复制 {stats['copied']}, "
                    f"跳过 {stats['skipped']}, 删除 {stats['deleted']}, 失败 {stats['failed']}"
                )
                if stats["failed"]:
                    raise ToolError(f"目录同步有 {stats['failed']} 个文件失败")
            else:
                if not local_path:
                    local_path = os.path.join(os.getcwd(), os.path.basename(remote_path))
                result = await pull_file(transport, remote_path, local_path)
                lines.append(f"✅ 文件已拉取: {local_path} ({result['size']} 字节)")

            output = "\n".join(lines)
            return DualOutput(summary=output, display=output)
        except TransferError as e:
            raise ToolError(f"拉取失败(nest={nest_name}): {e}") from e

    async def _push_file(self, kwargs: dict) -> DualOutput:
        """推送本地文件/目录到远程nest（协议分块传输，二进制安全，目录递归）"""
        from peng.nest.transfer import TransferError, push_file, sync_tree

        nest_name = kwargs.get("nest", "")
        local_path = kwargs.get("local_path", "")
        remote_path = kwargs.get("remote_path", "")

        if not nest_name:
            raise ToolError("push操作需要提供nest参数（目标nest名称）")
        if not local_path:
            raise ToolError("push操作需要提供local_path参数（本地文件/目录路径）")
        if not remote_path:
            raise ToolError("push操作需要提供remote_path参数（远端目标路径）")
        _check_transfer_paths(nest_name, local_path, remote_path)

        src = Path(local_path).expanduser()
        if not src.exists():
            raise ToolError(f"本地路径不存在: {local_path}")

        transport = self._make_transport(nest_name)

        try:
            lines = []
            if src.is_dir():
                stats = await sync_tree(transport, local_path, remote_path, direction="push")
                lines.append(f"✅ 目录已推送到 {nest_name}:{remote_path}")
                lines.append(
                    f"   检查 {stats['checked']} 个文件: 复制 {stats['copied']}, "
                    f"跳过 {stats['skipped']}, 删除 {stats['deleted']}, 失败 {stats['failed']}"
                )
                if stats["failed"]:
                    raise ToolError(f"目录推送有 {stats['failed']} 个文件失败")
            else:
                result = await push_file(transport, local_path, remote_path)
                lines.append(f"✅ 文件已推送到 {nest_name}:{remote_path} ({result['size']} 字节)")

            output = "\n".join(lines)
            return DualOutput(summary=output, display=output)
        except TransferError as e:
            raise ToolError(f"推送失败(nest={nest_name}): {e}") from e

    async def _sync_tree(self, kwargs: dict) -> DualOutput:
        """目录递归同步（增量mtime比对，支持push/pull方向与--delete）"""
        from peng.nest.transfer import TransferError, sync_tree

        nest_name = kwargs.get("nest", "")
        local_path = kwargs.get("local_path", "")
        remote_path = kwargs.get("remote_path", "")
        direction = kwargs.get("direction", "push")
        delete = bool(kwargs.get("delete", False))
        dry_run = bool(kwargs.get("dry_run", False))

        if not nest_name:
            raise ToolError("sync操作需要提供nest参数（目标nest名称）")
        if not local_path:
            raise ToolError("sync操作需要提供local_path参数（本地目录路径）")
        if not remote_path:
            raise ToolError("sync操作需要提供remote_path参数（远端目录路径）")
        if direction not in ("push", "pull"):
            raise ToolError(f"无效的direction: {direction}（支持 push/pull）")
        _check_transfer_paths(nest_name, local_path, remote_path)

        transport = self._make_transport(nest_name)

        try:
            stats = await sync_tree(
                transport, local_path, remote_path, direction=direction,
                delete=delete, dry_run=dry_run,
            )
            verb = "预演同步" if dry_run else "同步"
            arrow = "→" if direction == "push" else "←"
            lines = [
                f"✅ {verb}完成: {local_path if direction == 'push' else nest_name + ':' + remote_path} "
                f"{arrow} {nest_name + ':' + remote_path if direction == 'push' else local_path}",
                f"   检查 {stats['checked']} 个文件: 复制 {stats['copied']}, "
                f"跳过 {stats['skipped']}, 删除 {stats['deleted']}, 失败 {stats['failed']}",
            ]
            if stats.get("total_bytes"):
                lines.append(f"   传输量: {stats['total_bytes']} 字节")
            output = "\n".join(lines)
            if stats["failed"]:
                output += f"\n⚠️ 有 {stats['failed']} 个失败，详见日志"
            return DualOutput(summary=output, display=output)
        except TransferError as e:
            raise ToolError(f"同步失败(nest={nest_name}): {e}") from e

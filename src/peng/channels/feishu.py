"""
飞书 Channel 实现

基于飞书开放平台API，通过自建应用机器人实现消息收发。

消息接收：WebSocket 长连接（使用 lark-oapi SDK，无需公网IP）
消息发送：RESTful API (POST /open-apis/im/v1/messages)

依赖：
- lark-oapi: 飞书官方Python SDK (pip install lark-oapi)
- httpx: HTTP客户端

配置项（config/peng.json → channels.feishu）：
{
    "enabled": true,
    "app_id": "cli_xxx",
    "app_secret": "xxx",
    "verification_token": "xxx",
    "encrypt_key": "",
    "bot_name": "鹏🐣"
}
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from typing import Any

import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from peng.channels.base import ChannelBase, ChannelMessage, ChannelStatus, ChannelType

logger = logging.getLogger(__name__)

__all__ = ["FeishuChannel"]


class FeishuChannel(ChannelBase):
    """飞书消息通道

    使用飞书开放平台API实现消息收发。
    消息接收采用 WebSocket 长连接方式（飞书推荐），无需公网IP。

    使用流程：
    1. 在飞书开放平台创建自建应用
    2. 开启机器人能力
    3. 配置事件订阅（im.message.receive_v1），选择"使用长连接接收事件"
    4. 获取 App ID 和 App Secret
    5. 将配置填入 config/peng.json
    """

    # 飞书API基础URL
    BASE_URL = "https://open.feishu.cn/open-apis"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(ChannelType.FEISHU, config)

        # 飞书配置
        self._app_id: str = config.get("app_id", "")
        self._app_secret: str = config.get("app_secret", "")
        self._verification_token: str = config.get("verification_token", "")
        self._encrypt_key: str = config.get("encrypt_key", "")
        self._bot_name: str = config.get("bot_name", "鹏🐣")

        # Token管理（用于发送消息的REST API）
        self._tenant_access_token: str = ""
        self._token_expires_at: float = 0.0

        # HTTP客户端（用于发送消息）
        self._http_client: httpx.AsyncClient | None = None

        # WebSocket长连接客户端（用于接收消息）
        self._ws_client: lark.ws.Client | None = None
        self._ws_thread: threading.Thread | None = None

        if not self._app_id or not self._app_secret:
            self._logger.warning(
                "飞书配置缺少 app_id 或 app_secret，通道将不可用。"
                "请在 config/peng.json 的 channels.feishu 中配置。"
            )

    async def start(self) -> None:
        """启动飞书通道"""
        if not self._app_id or not self._app_secret:
            self._logger.error("缺少飞书配置，无法启动")
            self._status = ChannelStatus.ERROR
            return

        self._status = ChannelStatus.CONNECTING
        self._logger.info("正在初始化飞书通道...")

        try:
            # 1. 创建HTTP客户端（用于发送消息）
            self._http_client = httpx.AsyncClient(timeout=30.0)

            # 2. 获取 tenant_access_token 并验证
            await self._refresh_token()
            if not await self._validate_token():
                self._status = ChannelStatus.ERROR
                self._logger.error("❌ 飞书 token 验证失败")
                return

            # 3. 启动 WebSocket 长连接接收消息
            self._start_ws_connection()

            self._status = ChannelStatus.CONNECTED
            self._logger.info("✅ 飞书通道连接成功（WebSocket长连接 + REST API）")
        except Exception as e:
            self._status = ChannelStatus.ERROR
            self._logger.error(f"❌ 飞书通道启动失败: {e}", exc_info=True)

    async def stop(self) -> None:
        """停止飞书通道"""
        # 停止 WebSocket 连接
        self._stop_ws_connection()

        # 关闭 HTTP 客户端
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        self._tenant_access_token = ""
        self._token_expires_at = 0.0
        self._status = ChannelStatus.DISCONNECTED
        self._logger.info("飞书通道已停止")

    async def send_message(
        self,
        receiver_id: str,
        content: str,
        msg_type: str = "text",
        **kwargs: Any,
    ) -> bool:
        """发送飞书消息

        Args:
            receiver_id: 接收者的 open_id 或 chat_id
            content: 消息内容
            msg_type: 消息类型（text/post/image/file等）
            receive_id_type: ID类型（open_id/chat_id/user_id），默认open_id

        Returns:
            True 表示发送成功
        """
        if not self._http_client or not self.is_connected:
            self._logger.error("飞书通道未连接，无法发送消息")
            return False

        # 确保 token 有效
        await self._ensure_token()

        receive_id_type = kwargs.get("receive_id_type", "open_id")

        # 构建消息内容
        if msg_type == "text":
            msg_content = json.dumps({"text": content})
        elif msg_type == "post":
            # 富文本消息
            msg_content = content  # content应该是JSON字符串
        else:
            msg_content = content

        # 发送请求
        url = f"{self.BASE_URL}/im/v1/messages"
        params = {"receive_id_type": receive_id_type}
        headers = {
            "Authorization": f"Bearer {self._tenant_access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "receive_id": receiver_id,
            "msg_type": msg_type,
            "content": msg_content,
        }

        try:
            response = await self._http_client.post(
                url, params=params, headers=headers, json=payload
            )
            result = response.json()

            if response.status_code == 200 and result.get("code") == 0:
                self._logger.info(f"飞书消息发送成功: receiver={receiver_id}")
                return True
            else:
                self._logger.error(
                    f"飞书消息发送失败: status={response.status_code}, "
                    f"code={result.get('code')}, msg={result.get('msg')}"
                )
                # token过期时自动刷新重试
                if result.get("code") == 99991663:
                    await self._refresh_token()
                    return await self.send_message(
                        receiver_id, content, msg_type, **kwargs
                    )
                return False
        except Exception as e:
            self._logger.error(f"飞书消息发送异常: {e}", exc_info=True)
            return False

    async def reply_message(
        self,
        message_id: str,
        content: str,
        msg_type: str = "text",
    ) -> str | None:
        """回复飞书消息（在飞书中体现回复关系）

        使用飞书 reply API，回复的消息在飞书客户端中会显示为对原消息的回复。

        Args:
            message_id: 要回复的消息ID（platform_msg_id）
            content: 回复内容
            msg_type: 消息类型

        Returns:
            回复消息的飞书 message_id，失败返回 None
        """
        if not self._http_client or not self.is_connected:
            self._logger.error("飞书通道未连接，无法回复消息")
            return None

        await self._ensure_token()

        # 构建消息内容
        if msg_type == "text":
            msg_content = json.dumps({"text": content})
        elif msg_type == "post":
            msg_content = content
        else:
            msg_content = content

        url = f"{self.BASE_URL}/im/v1/messages/{message_id}/reply"
        headers = {
            "Authorization": f"Bearer {self._tenant_access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "msg_type": msg_type,
            "content": msg_content,
        }

        try:
            response = await self._http_client.post(
                url, headers=headers, json=payload
            )
            result = response.json()

            if response.status_code == 200 and result.get("code") == 0:
                new_msg_id = result.get("data", {}).get("message_id", "")
                self._logger.info(f"飞书消息回复成功: reply_to={message_id}, new_msg_id={new_msg_id}")
                return new_msg_id or None
            else:
                self._logger.error(
                    f"飞书消息回复失败: status={response.status_code}, "
                    f"code={result.get('code')}, msg={result.get('msg')}"
                )
                if result.get("code") == 99991663:
                    await self._refresh_token()
                    return await self.reply_message(message_id, content, msg_type)
                return None
        except Exception as e:
            self._logger.error(f"飞书消息回复异常: {e}", exc_info=True)
            return None

    async def upload_file(self, file_path: str, file_type: str = "stream") -> str | None:
        """上传文件到飞书，获取 file_key

        飞书上传文件 API: POST /open-apis/im/v1/files
        Content-Type: multipart/form-data

        Args:
            file_path: 本地文件路径
            file_type: 文件类型（stream=普通文件, image=图片）

        Returns:
            file_key 字符串，失败返回 None
        """
        if not self._http_client or not self.is_connected:
            self._logger.error("飞书通道未连接，无法上传文件")
            return None

        await self._ensure_token()

        if not os.path.isfile(file_path):
            self._logger.error(f"文件不存在: {file_path}")
            return None

        try:
            url = f"{self.BASE_URL}/im/v1/files"
            headers = {
                "Authorization": f"Bearer {self._tenant_access_token}",
            }

            filename = os.path.basename(file_path)
            with open(file_path, "rb") as f:
                files = {
                    "file_type": (None, file_type),
                    "file_name": (None, filename),
                    "file": (filename, f),
                }

                response = await self._http_client.post(
                    url, headers=headers, files=files, timeout=120.0
                )

            result = response.json()

            if response.status_code == 200 and result.get("code") == 0:
                file_key = result.get("data", {}).get("file_key", "")
                if file_key:
                    self._logger.info(f"飞书文件上传成功: {filename} → file_key={file_key}")
                    return file_key
                else:
                    self._logger.error(f"飞书文件上传返回空 file_key: {result}")
                    return None
            else:
                self._logger.error(
                    f"飞书文件上传失败: status={response.status_code}, "
                    f"code={result.get('code')}, msg={result.get('msg')}"
                )
                # token 过期重试
                if result.get("code") == 99991663:
                    await self._refresh_token()
                    return await self.upload_file(file_path, file_type)
                return None
        except Exception as e:
            self._logger.error(f"飞书文件上传异常: {e}", exc_info=True)
            return None

    async def upload_image(self, file_path: str, image_type: str = "message") -> str | None:
        """上传图片到飞书，获取 image_key

        飞书上传图片 API: POST /open-apis/im/v1/images
        Content-Type: multipart/form-data

        Args:
            file_path: 本地图片路径
            image_type: 图片类型（message=消息图片）

        Returns:
            image_key 字符串，失败返回 None
        """
        if not self._http_client or not self.is_connected:
            self._logger.error("飞书通道未连接，无法上传图片")
            return None

        await self._ensure_token()

        if not os.path.isfile(file_path):
            self._logger.error(f"图片不存在: {file_path}")
            return None

        try:
            url = f"{self.BASE_URL}/im/v1/images"
            headers = {
                "Authorization": f"Bearer {self._tenant_access_token}",
            }

            filename = os.path.basename(file_path)
            with open(file_path, "rb") as f:
                files = {
                    "image_type": (None, image_type),
                    "image": (filename, f),
                }

                response = await self._http_client.post(
                    url, headers=headers, files=files, timeout=120.0
                )

            result = response.json()

            if response.status_code == 200 and result.get("code") == 0:
                image_key = result.get("data", {}).get("image_key", "")
                if image_key:
                    self._logger.info(f"飞书图片上传成功: {filename} → image_key={image_key}")
                    return image_key
                else:
                    self._logger.error(f"飞书图片上传返回空 image_key: {result}")
                    return None
            else:
                self._logger.error(
                    f"飞书图片上传失败: status={response.status_code}, "
                    f"code={result.get('code')}, msg={result.get('msg')}"
                )
                if result.get("code") == 99991663:
                    await self._refresh_token()
                    return await self.upload_image(file_path, image_type)
                return None
        except Exception as e:
            self._logger.error(f"飞书图片上传异常: {e}", exc_info=True)
            return None

    async def send_file_message(
        self,
        receiver_id: str,
        file_key: str,
        receive_id_type: str = "open_id",
    ) -> bool:
        """发送文件消息

        Args:
            receiver_id: 接收者的 open_id 或 chat_id
            file_key: 上传后获得的 file_key
            receive_id_type: ID 类型

        Returns:
            True 表示发送成功
        """
        if not self._http_client or not self.is_connected:
            self._logger.error("飞书通道未连接，无法发送文件消息")
            return False

        await self._ensure_token()

        url = f"{self.BASE_URL}/im/v1/messages"
        params = {"receive_id_type": receive_id_type}
        headers = {
            "Authorization": f"Bearer {self._tenant_access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "receive_id": receiver_id,
            "msg_type": "file",
            "content": json.dumps({"file_key": file_key}),
        }

        try:
            response = await self._http_client.post(
                url, params=params, headers=headers, json=payload
            )
            result = response.json()

            if response.status_code == 200 and result.get("code") == 0:
                self._logger.info(f"飞书文件消息发送成功: receiver={receiver_id}")
                return True
            else:
                self._logger.error(
                    f"飞书文件消息发送失败: status={response.status_code}, "
                    f"code={result.get('code')}, msg={result.get('msg')}"
                )
                if result.get("code") == 99991663:
                    await self._refresh_token()
                    return await self.send_file_message(receiver_id, file_key, receive_id_type)
                return False
        except Exception as e:
            self._logger.error(f"飞书文件消息发送异常: {e}", exc_info=True)
            return False

    async def send_image_message(
        self,
        receiver_id: str,
        image_key: str,
        receive_id_type: str = "open_id",
    ) -> bool:
        """发送图片消息

        Args:
            receiver_id: 接收者的 open_id 或 chat_id
            image_key: 上传后获得的 image_key
            receive_id_type: ID 类型

        Returns:
            True 表示发送成功
        """
        if not self._http_client or not self.is_connected:
            self._logger.error("飞书通道未连接，无法发送图片消息")
            return False

        await self._ensure_token()

        url = f"{self.BASE_URL}/im/v1/messages"
        params = {"receive_id_type": receive_id_type}
        headers = {
            "Authorization": f"Bearer {self._tenant_access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "receive_id": receiver_id,
            "msg_type": "image",
            "content": json.dumps({"image_key": image_key}),
        }

        try:
            response = await self._http_client.post(
                url, params=params, headers=headers, json=payload
            )
            result = response.json()

            if response.status_code == 200 and result.get("code") == 0:
                self._logger.info(f"飞书图片消息发送成功: receiver={receiver_id}")
                return True
            else:
                self._logger.error(
                    f"飞书图片消息发送失败: status={response.status_code}, "
                    f"code={result.get('code')}, msg={result.get('msg')}"
                )
                if result.get("code") == 99991663:
                    await self._refresh_token()
                    return await self.send_image_message(receiver_id, image_key, receive_id_type)
                return False
        except Exception as e:
            self._logger.error(f"飞书图片消息发送异常: {e}", exc_info=True)
            return False

    async def reply_file_message(self, message_id: str, file_key: str) -> str | None:
        """回复文件消息（在飞书中体现回复关系）

        Args:
            message_id: 要回复的消息ID
            file_key: 上传后获得的 file_key

        Returns:
            回复消息的飞书 message_id，失败返回 None
        """
        if not self._http_client or not self.is_connected:
            self._logger.error("飞书通道未连接，无法回复文件消息")
            return None

        await self._ensure_token()

        url = f"{self.BASE_URL}/im/v1/messages/{message_id}/reply"
        headers = {
            "Authorization": f"Bearer {self._tenant_access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "msg_type": "file",
            "content": json.dumps({"file_key": file_key}),
        }

        try:
            response = await self._http_client.post(
                url, headers=headers, json=payload
            )
            result = response.json()

            if response.status_code == 200 and result.get("code") == 0:
                new_msg_id = result.get("data", {}).get("message_id", "")
                self._logger.info(f"飞书文件回复成功: reply_to={message_id}, new_msg_id={new_msg_id}")
                return new_msg_id or None
            else:
                self._logger.error(
                    f"飞书文件回复失败: status={response.status_code}, "
                    f"code={result.get('code')}, msg={result.get('msg')}"
                )
                if result.get("code") == 99991663:
                    await self._refresh_token()
                    return await self.reply_file_message(message_id, file_key)
                return None
        except Exception as e:
            self._logger.error(f"飞书文件回复异常: {e}", exc_info=True)
            return None

    async def reply_image_message(self, message_id: str, image_key: str) -> str | None:
        """回复图片消息（在飞书中体现回复关系）

        Args:
            message_id: 要回复的消息ID
            image_key: 上传后获得的 image_key

        Returns:
            回复消息的飞书 message_id，失败返回 None
        """
        if not self._http_client or not self.is_connected:
            self._logger.error("飞书通道未连接，无法回复图片消息")
            return None

        await self._ensure_token()

        url = f"{self.BASE_URL}/im/v1/messages/{message_id}/reply"
        headers = {
            "Authorization": f"Bearer {self._tenant_access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "msg_type": "image",
            "content": json.dumps({"image_key": image_key}),
        }

        try:
            response = await self._http_client.post(
                url, headers=headers, json=payload
            )
            result = response.json()

            if response.status_code == 200 and result.get("code") == 0:
                new_msg_id = result.get("data", {}).get("message_id", "")
                self._logger.info(f"飞书图片回复成功: reply_to={message_id}, new_msg_id={new_msg_id}")
                return new_msg_id or None
            else:
                self._logger.error(
                    f"飞书图片回复失败: status={response.status_code}, "
                    f"code={result.get('code')}, msg={result.get('msg')}"
                )
                if result.get("code") == 99991663:
                    await self._refresh_token()
                    return await self.reply_image_message(message_id, image_key)
                return None
        except Exception as e:
            self._logger.error(f"飞书图片回复异常: {e}", exc_info=True)
            return None

    def _is_image_file(self, file_path: str) -> bool:
        """判断文件是否为图片类型"""
        ext = os.path.splitext(file_path)[1].lower()
        image_extensions = {
            ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico",
            ".tiff", ".tif", ".avif", ".heic", ".heif", ".svg",
        }
        return ext in image_extensions

    async def check_connection(self) -> bool:
        """检查飞书连接状态"""
        if not self._http_client:
            return False

        try:
            await self._ensure_token()
            return await self._validate_token()
        except Exception:
            return False

    # ==================== WebSocket 长连接 ====================

    def _start_ws_connection(self) -> None:
        """启动WebSocket长连接（在独立线程中运行）

        lark-oapi SDK的 ws.Client.start() 是同步阻塞的，
        需要在独立线程中运行以避免阻塞asyncio事件循环。
        """
        # 构建事件处理器
        event_handler = (
            lark.EventDispatcherHandler.builder(
                self._encrypt_key,
                self._verification_token,
            )
            .register_p2_im_message_receive_v1(self._on_ws_message_receive)
            .build()
        )

        # 创建 WebSocket 客户端
        self._ws_client = lark.ws.Client(
            app_id=self._app_id,
            app_secret=self._app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
            auto_reconnect=True,
        )

        # 在独立线程中启动（start()是阻塞的，需要独立的事件循环）
        self._ws_thread = threading.Thread(
            target=self._run_ws_client,
            name="feishu-ws",
            daemon=True,
        )
        self._ws_thread.start()
        self._logger.info("飞书 WebSocket 长连接线程已启动")

    def _run_ws_client(self) -> None:
        """在独立线程中运行WebSocket客户端

        SDK的 ws.Client.start() 内部使用模块级别的 loop 变量
        （在模块加载时通过 asyncio.get_event_loop() 获取），
        在已有asyncio事件循环的环境中（如FastAPI），
        该loop已经是主线程的事件循环，调用 run_until_complete 会报错：
        "This event loop is already running"

        解决方案：在新线程中创建新的事件循环，并monkey-patch SDK模块的loop变量。
        """
        try:
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)

            # monkey-patch SDK模块级别的loop变量
            # SDK的ws/client.py在模块加载时获取了主线程的loop
            # 我们需要替换为新线程的loop
            import lark_oapi.ws.client as _ws_client_mod
            _ws_client_mod.loop = new_loop

            if self._ws_client:
                self._ws_client.start()
        except Exception as e:
            self._logger.error(f"飞书WebSocket线程异常: {e}", exc_info=True)

    def _stop_ws_connection(self) -> None:
        """停止WebSocket长连接"""
        # SDK的ws.Client没有提供优雅停止方法，
        # 由于是daemon线程，主线程退出时会自动终止
        self._ws_client = None
        self._ws_thread = None
        self._logger.info("飞书 WebSocket 长连接已停止")

    def _on_ws_message_receive(self, data: P2ImMessageReceiveV1) -> None:
        """WebSocket长连接收到消息的回调

        由 lark-oapi SDK 在 WS 线程中调用。

        Args:
            data: 飞书消息接收事件数据
        """
        try:
            if not data.event or not data.event.message or not data.event.sender:
                return

            message = data.event.message
            sender = data.event.sender

            # 解析消息内容
            msg_type = message.message_type or "text"
            content_str = message.content or "{}"
            try:
                content_json = json.loads(content_str)
            except json.JSONDecodeError:
                content_json = {}

            # 提取文本内容
            text_content = ""
            attachment_paths: list[str] = []  # 下载后的附件本地路径

            if msg_type == "text":
                text_content = content_json.get("text", "")
            elif msg_type == "post":
                text_content = self._extract_post_text(content_json)
            elif msg_type == "image":
                # 图片消息：下载图片
                image_key = content_json.get("image_key", "")
                if image_key:
                    text_content = "[图片]"
                    downloaded = self._download_resource_sync(
                        message.message_id or "", image_key, "image", "jpg"
                    )
                    if downloaded:
                        attachment_paths.append(downloaded)
                else:
                    text_content = "[图片]"
            elif msg_type == "file":
                # 文件消息：下载文件
                file_key = content_json.get("file_key", "")
                file_name = content_json.get("file_name", "unknown")
                if file_key:
                    text_content = f"[文件: {file_name}]"
                    ext = os.path.splitext(file_name)[1].lstrip(".") if file_name else ""
                    downloaded = self._download_resource_sync(
                        message.message_id or "", file_key, "file", ext
                    )
                    if downloaded:
                        attachment_paths.append(downloaded)
                else:
                    text_content = "[文件]"
            else:
                text_content = f"[{msg_type}]"

            if not text_content:
                return

            # 附件信息追加到消息内容
            if attachment_paths:
                text_content += "\n" + "\n".join(
                    f"[附件] {os.path.basename(p)}: {p}" for p in attachment_paths
                )

            # 构建 ChannelMessage
            chat_type = message.chat_type or "p2p"
            is_group = chat_type == "group"

            # 提取 sender_id
            sender_id = ""
            if sender.sender_id:
                sender_id = sender.sender_id.open_id or ""

            channel_msg = ChannelMessage(
                content=text_content,
                sender_id=sender_id,
                sender_name="",
                receiver_id=message.chat_id or "",
                msg_type=msg_type,
                raw_data={
                    "message_id": message.message_id,
                    "chat_id": message.chat_id,
                    "chat_type": chat_type,
                    "message_type": msg_type,
                    "content": content_str,
                    "sender_id": sender_id,
                    "root_id": message.root_id or "",
                    "parent_id": message.parent_id or "",
                },
                channel_type=ChannelType.FEISHU,
                platform_msg_id=message.message_id or "",
                is_group=is_group,
                group_name="",
            )

            self._logger.info(
                f"收到飞书消息(WS): msg_id={message.message_id}, sender={sender_id}, "
                f"group={is_group}, content={text_content[:50]}..."
            )

            # 通知消息回调
            self._notify_message(channel_msg)

        except Exception as e:
            self._logger.error(f"处理飞书WS消息失败: {e}", exc_info=True)

    # ==================== HTTP 回调兼容（保留用于URL验证等） ====================

    def handle_event_callback(self, event_data: dict[str, Any]) -> ChannelMessage | None:
        """处理飞书事件回调（HTTP回调方式，兼容保留）

        当收到飞书事件订阅的HTTP回调时，解析消息并返回ChannelMessage。
        此方法由外部HTTP服务器调用。

        注意：主流程已改用WebSocket长连接，此方法保留用于兼容。

        Args:
            event_data: 飞书事件数据（已解析的JSON）

        Returns:
            ChannelMessage 或 None
        """
        try:
            header = event_data.get("header", {})
            event_type = header.get("event_type", "")

            if event_type != "im.message.receive_v1":
                self._logger.debug(f"忽略非消息事件: {event_type}")
                return None

            event = event_data.get("event", {})
            message = event.get("message", {})
            sender = event.get("sender", {})

            # 解析消息内容
            msg_type = message.get("message_type", "text")
            content_str = message.get("content", "{}")
            try:
                content_json = json.loads(content_str)
            except json.JSONDecodeError:
                content_json = {}

            text_content = ""
            if msg_type == "text":
                text_content = content_json.get("text", "")
            elif msg_type == "post":
                text_content = self._extract_post_text(content_json)
            else:
                text_content = f"[{msg_type}]"

            if not text_content:
                return None

            chat_id = message.get("chat_id", "")
            chat_type = message.get("chat_type", "p2p")
            is_group = chat_type == "group"

            channel_msg = ChannelMessage(
                content=text_content,
                sender_id=sender.get("sender_id", {}).get("open_id", ""),
                sender_name=sender.get("sender_id", {}).get("name", ""),
                receiver_id=chat_id,
                msg_type=msg_type,
                raw_data=event_data,
                channel_type=ChannelType.FEISHU,
                platform_msg_id=message.get("message_id", ""),
                is_group=is_group,
                group_name=message.get("chat_name", ""),
            )

            self._logger.info(
                f"收到飞书消息(HTTP): sender={channel_msg.sender_name or channel_msg.sender_id}, "
                f"group={is_group}, content={text_content[:50]}..."
            )

            self._notify_message(channel_msg)
            return channel_msg

        except Exception as e:
            self._logger.error(f"解析飞书事件失败: {e}", exc_info=True)
            return None

    def verify_url_challenge(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """处理飞书URL验证请求（兼容保留）

        飞书配置HTTP回调事件订阅时会发送验证请求。
        WebSocket长连接模式下不需要此功能，保留用于兼容。

        Args:
            data: 验证请求数据 {"challenge": "xxx", "token": "xxx", "type": "url_verification"}

        Returns:
            响应数据 {"challenge": "xxx"} 或 None
        """
        if data.get("type") == "url_verification":
            token = data.get("token", "")
            if token == self._verification_token:
                return {"challenge": data.get("challenge", "")}
            else:
                self._logger.warning("飞书URL验证token不匹配")
        return None

    def _extract_post_text(self, content_json: dict[str, Any]) -> str:
        """从飞书富文本消息中提取纯文本"""
        text_parts = []
        content = content_json.get("content", {})
        for lang, posts in content.items():
            for post in posts:
                for element in post:
                    if element.get("tag") == "text":
                        text_parts.append(element.get("text", ""))
                    elif element.get("tag") == "at":
                        text_parts.append(element.get("user_name", "@user"))
        return "".join(text_parts)

    async def _refresh_token(self) -> None:
        """刷新 tenant_access_token"""
        if not self._http_client:
            return

        url = f"{self.BASE_URL}/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": self._app_id,
            "app_secret": self._app_secret,
        }

        try:
            response = await self._http_client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            result = response.json()

            if result.get("code") == 0:
                self._tenant_access_token = result.get("tenant_access_token", "")
                expire = result.get("expire", 7200)
                # 提前5分钟刷新
                self._token_expires_at = time.time() + expire - 300
                self._logger.info("飞书 tenant_access_token 刷新成功")
            else:
                self._logger.error(
                    f"飞书 token 获取失败: code={result.get('code')}, "
                    f"msg={result.get('msg')}"
                )
        except Exception as e:
            self._logger.error(f"飞书 token 刷新异常: {e}", exc_info=True)

    async def _ensure_token(self) -> None:
        """确保token有效，过期则刷新"""
        if time.time() >= self._token_expires_at:
            await self._refresh_token()

    async def _validate_token(self) -> bool:
        """验证当前token是否有效"""
        if not self._tenant_access_token or not self._http_client:
            return False

        try:
            # 调用一个简单的API验证token
            url = f"{self.BASE_URL}/bot/v3/info"
            headers = {"Authorization": f"Bearer {self._tenant_access_token}"}
            response = await self._http_client.get(url, headers=headers)
            result = response.json()
            return result.get("code") == 0
        except Exception:
            return False

    def get_status_info(self) -> dict[str, Any]:
        """获取飞书通道状态信息"""
        info = super().get_status_info()
        info["app_id"] = self._app_id[:10] + "..." if self._app_id else ""
        info["bot_name"] = self._bot_name
        info["token_valid"] = time.time() < self._token_expires_at
        info["ws_connected"] = self._ws_client is not None
        info["receive_mode"] = "websocket" if self._ws_client else "none"
        return info

    # ==================== 附件下载 ====================

    def _get_attachment_dir(self) -> str:
        """获取附件保存目录"""
        from peng.config.loader import load_config
        config = load_config()
        attach_dir = os.path.join(str(config.paths.data_dir_path), "feishu_attachments")
        os.makedirs(attach_dir, exist_ok=True)
        return attach_dir

    def _get_token_sync(self) -> str:
        """同步获取tenant_access_token（用于WS线程中的下载）"""
        if time.time() < self._token_expires_at and self._tenant_access_token:
            return self._tenant_access_token
        try:
            import requests
            resp = requests.post(
                f"{self.BASE_URL}/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
                timeout=10,
            )
            result = resp.json()
            if result.get("code") == 0:
                self._tenant_access_token = result.get("tenant_access_token", "")
                expire = result.get("expire", 7200)
                self._token_expires_at = time.time() + expire - 300
                return self._tenant_access_token
        except Exception as e:
            self._logger.error(f"同步获取token失败: {e}")
        return self._tenant_access_token

    def _download_resource_sync(
        self, message_id: str, file_key: str, res_type: str, ext: str
    ) -> str | None:
        """同步下载飞书资源（图片/文件）

        使用飞书统一资源下载API：
        GET /im/v1/messages/{message_id}/resources/{file_key}?type=image|file

        Args:
            message_id: 消息ID
            file_key: 资源key（image_key或file_key）
            res_type: 资源类型（"image"或"file"）
            ext: 文件扩展名

        Returns:
            下载后的本地文件路径，失败返回None
        """
        if not file_key or not message_id:
            return None
        try:
            import requests
            token = self._get_token_sync()
            if not token:
                self._logger.error(f"下载{res_type}失败：无有效token")
                return None

            url = (
                f"{self.BASE_URL}/im/v1/messages/{message_id}"
                f"/resources/{file_key}?type={res_type}"
            )
            headers = {"Authorization": f"Bearer {token}"}
            resp = requests.get(url, headers=headers, timeout=60)

            if resp.status_code != 200:
                self._logger.error(
                    f"下载{res_type}失败: status={resp.status_code}, "
                    f"body={resp.text[:200]}"
                )
                return None

            # 生成文件名
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            short_hash = hashlib.md5(file_key.encode()).hexdigest()[:8]
            ext_str = f".{ext}" if ext else ""
            filename = f"{timestamp}_{short_hash}{ext_str}"
            filepath = os.path.join(self._get_attachment_dir(), filename)

            with open(filepath, "wb") as f:
                f.write(resp.content)

            # 文本文件自动转UTF-8编码（webui要求utf-8）
            text_extensions = {".txt", ".md", ".py", ".js", ".ts", ".json", ".xml", ".html",
                               ".css", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf",
                               ".sh", ".bat", ".csv", ".log", ".sql"}
            if ext_str.lower() in text_extensions:
                self._convert_to_utf8(filepath)

            self._logger.info(
                f"✅ 飞书{res_type}已下载: {filepath} ({len(resp.content)} bytes)"
            )
            return filepath
        except Exception as e:
            self._logger.error(f"下载飞书{res_type}异常: {e}", exc_info=True)
            return None

    @staticmethod
    def _convert_to_utf8(filepath: str) -> None:
        """将文本文件转换为UTF-8编码（如果还不是UTF-8）"""
        try:
            # 先尝试用utf-8读取
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            # 成功说明已经是utf-8，不需要转换
            return
        except UnicodeDecodeError:
            pass

        # 尝试用gbk/gb18030读取（中文常见编码）
        for encoding in ["gb18030", "gbk", "gb2312", "big5", "shift_jis", "latin-1"]:
            try:
                with open(filepath, "r", encoding=encoding) as f:
                    content = f.read()
                # 成功读取，转写为utf-8
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
                return
            except (UnicodeDecodeError, UnicodeEncodeError):
                continue

"""
微信 Channel 实现（基于 Gewechat）

通过 Gewechat 框架的 RESTful API 实现个人微信消息收发。

消息接收：设置回调URL → Gewechat将消息POST到回调地址
消息发送：POST http://{host}:2531/v2/api/{接口名}

前置条件：
1. 部署 Gewechat Docker 服务（需要Linux服务器 + Docker）
   docker pull registry.cn-hangzhou.aliyuncs.com/gewe/gewe:latest
   docker tag registry.cn-hangzhou.aliyuncs.com/gewe/gewe gewe
   docker run -itd -v /root/temp:/root/temp -p 2531:2531 -p 2532:2532 \
       --privileged=true --name=gewe gewe /usr/sbin/init
2. 扫码登录微信
3. 获取 token（POST /v2/api/getToken）

配置项（config/peng.json → channels.wechat）：
{
    "enabled": true,
    "base_url": "http://localhost:2531",
    "download_url": "http://localhost:2532",
    "app_id": "",
    "callback_url": "http://your-server:8080/api/channels/wechat/callback",
    "callback_port": 8080
}
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from peng.channels.base import ChannelBase, ChannelMessage, ChannelStatus, ChannelType

logger = logging.getLogger(__name__)

__all__ = ["WechatChannel"]


class WechatChannel(ChannelBase):
    """微信消息通道（Gewechat适配器）

    使用Gewechat框架的RESTful API实现微信消息收发。

    API调用格式：
    - API地址：http://{host}:2531/v2/api/{接口名}
    - Header：X-GEWE-TOKEN: {token}
    - 文件下载：http://{host}:2532/download/{文件路径}

    主要接口：
    - getToken：获取token
    - getQr：获取登录二维码
    - checkQr：确认登录
    - setCallback：设置消息回调地址
    - getContactList：获取通讯录
    - postText：发送文本消息
    - postImage：发送图片消息
    - postFile：发送文件消息
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(ChannelType.WECHAT, config)

        # Gewechat 配置
        self._base_url: str = config.get("base_url", "http://localhost:2531")
        self._download_url: str = config.get("download_url", "http://localhost:2532")
        self._app_id: str = config.get("app_id", "")
        self._callback_url: str = config.get("callback_url", "")

        # Token管理
        self._token: str = ""

        # HTTP客户端
        self._http_client: httpx.AsyncClient | None = None

        if not self._base_url:
            self._logger.warning(
                "微信Gewechat未配置 base_url，通道将不可用。"
                "请在 config/peng.json 的 channels.wechat 中配置。"
            )

    async def start(self) -> None:
        """启动微信通道"""
        if not self._base_url:
            self._logger.error("缺少Gewechat base_url，无法启动")
            self._status = ChannelStatus.ERROR
            return

        self._status = ChannelStatus.CONNECTING
        self._logger.info("正在初始化微信通道（Gewechat）...")

        try:
            # 创建HTTP客户端
            self._http_client = httpx.AsyncClient(timeout=30.0)

            # 获取token
            await self._get_token()

            if self._token:
                # 设置消息回调地址
                if self._callback_url:
                    await self._set_callback()

                # 检查是否已登录
                if await self._check_online():
                    self._status = ChannelStatus.CONNECTED
                    self._logger.info("✅ 微信通道连接成功（已登录）")
                else:
                    self._status = ChannelStatus.CONNECTED  # 服务可用但需扫码
                    self._logger.warning("⚠️ Gewechat服务已启动，但微信未登录。请使用 get_qr_code() 获取二维码扫码登录。")
            else:
                self._status = ChannelStatus.ERROR
                self._logger.error("❌ Gewechat token 获取失败")

        except Exception as e:
            self._status = ChannelStatus.ERROR
            self._logger.error(f"❌ 微信通道启动失败: {e}", exc_info=True)

    async def stop(self) -> None:
        """停止微信通道"""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        self._token = ""
        self._status = ChannelStatus.DISCONNECTED
        self._logger.info("微信通道已停止")

    async def send_message(
        self,
        receiver_id: str,
        content: str,
        msg_type: str = "text",
        **kwargs: Any,
    ) -> bool:
        """发送微信消息

        Args:
            receiver_id: 接收者的wxid
            content: 消息内容
            msg_type: 消息类型（text/image/file/link等）

        Returns:
            True 表示发送成功
        """
        if not self._http_client or not self._token:
            self._logger.error("微信通道未连接，无法发送消息")
            return False

        try:
            if msg_type == "text":
                return await self._post_text(receiver_id, content)
            elif msg_type == "image":
                image_url = kwargs.get("image_url", "")
                return await self._post_image(receiver_id, image_url)
            elif msg_type == "file":
                file_url = kwargs.get("file_url", "")
                return await self._post_file(receiver_id, file_url)
            elif msg_type == "link":
                return await self._post_link(receiver_id, content, **kwargs)
            else:
                # 默认当文本发送
                return await self._post_text(receiver_id, content)
        except Exception as e:
            self._logger.error(f"微信消息发送异常: {e}", exc_info=True)
            return False

    async def check_connection(self) -> bool:
        """检查微信连接状态"""
        if not self._http_client or not self._token:
            return False
        return await self._check_online()

    async def get_qr_code(self, proxy: str = "") -> str:
        """获取登录二维码

        Args:
            proxy: 代理地址（可选）

        Returns:
            二维码内容（可用于生成二维码图片）
        """
        result = await self._api_call("getQr", {
            "appId": self._app_id,
            "proxy": proxy or "",
        })

        if result and result.get("ret") == 200:
            data = result.get("data", {})
            self._app_id = data.get("appId", self._app_id)
            return data.get("qrUrl", data.get("uuid", ""))

        return ""

    async def check_qr_login(self, uuid: str = "", captch_code: str = "", proxy_ip: str = "") -> dict[str, Any]:
        """确认扫码登录

        Args:
            uuid: getQr返回的uuid
            captch_code: 验证码（同省登录通常不需要）
            proxy_ip: 代理IP

        Returns:
            登录结果
        """
        result = await self._api_call("checkQr", {
            "appId": self._app_id,
            "uuid": uuid,
            "captchCode": captch_code,
            "proxyIp": proxy_ip,
        })

        if result and result.get("ret") == 200:
            self._status = ChannelStatus.CONNECTED
            self._logger.info("✅ 微信扫码登录成功")

        return result or {}

    def handle_callback(self, callback_data: dict[str, Any]) -> ChannelMessage | None:
        """处理Gewechat回调消息

        当Gewechat通过回调URL发送消息时，解析并返回ChannelMessage。

        Gewechat回调数据结构：
        {
            "type": "msg",
            "data": {
                "fromUser": {"wxid": "xxx", "nickname": "xxx"},
                "toUser": {"wxid": "xxx"},
                "content": "消息内容",
                "msgType": 1,  // 1=文本 3=图片 34=语音 47=表情 49=文件等
                "msgId": "xxx",
                "newMsgId": "xxx",
                "createTime": 1234567890,
                "isGroup": false,
                "groupName": "",
                "fromGroupMsgWxid": ""  // 群内发送者wxid
            }
        }

        Args:
            callback_data: 回调数据

        Returns:
            ChannelMessage 或 None
        """
        try:
            msg_type_num = callback_data.get("msgType", 0)
            content = callback_data.get("content", "")

            # 只处理文本消息
            if msg_type_num != 1:
                self._logger.debug(f"忽略非文本消息: msgType={msg_type_num}")
                return None

            # 如果内容是XML格式，提取文本
            if content.startswith("<"):
                content = self._extract_xml_text(content)

            if not content:
                return None

            # 解析发送者
            from_user = callback_data.get("fromUser", {})
            if isinstance(from_user, dict):
                sender_wxid = from_user.get("wxid", "")
                sender_name = from_user.get("nickname", "")
            else:
                sender_wxid = str(from_user)
                sender_name = ""

            to_user = callback_data.get("toUser", {})
            if isinstance(to_user, dict):
                receiver_wxid = to_user.get("wxid", "")
            else:
                receiver_wxid = str(to_user)

            # 群消息处理
            is_group = callback_data.get("isGroup", False)
            group_name = callback_data.get("groupName", "")
            # 群消息中 fromUser 是群wxid，实际发送者在 content 或额外字段
            group_sender = callback_data.get("fromGroupMsgWxid", "")

            if is_group and group_sender:
                sender_wxid = group_sender

            channel_msg = ChannelMessage(
                content=content,
                sender_id=sender_wxid,
                sender_name=sender_name,
                receiver_id=receiver_wxid,
                msg_type="text",
                raw_data=callback_data,
                channel_type=ChannelType.WECHAT,
                platform_msg_id=callback_data.get("newMsgId", callback_data.get("msgId", "")),
                is_group=is_group,
                group_name=group_name,
            )

            self._logger.info(
                f"收到微信消息: sender={sender_name or sender_wxid}, "
                f"group={is_group}, content={content[:50]}..."
            )

            # 通知消息回调
            self._notify_message(channel_msg)

            return channel_msg

        except Exception as e:
            self._logger.error(f"解析微信回调失败: {e}", exc_info=True)
            return None

    # ==================== Gewechat API 封装 ====================

    async def _get_token(self) -> None:
        """获取Gewechat API token"""
        if not self._http_client:
            return

        try:
            url = f"{self._base_url}/v2/api/getToken"
            response = await self._http_client.post(url)
            result = response.json()

            if result.get("ret") == 200:
                self._token = result.get("data", {}).get("token", "")
                if self._token:
                    self._logger.info("Gewechat token 获取成功")
                else:
                    self._logger.error("Gewechat token 为空")
            else:
                self._logger.error(f"Gewechat token 获取失败: {result}")
        except Exception as e:
            self._logger.error(f"Gewechat token 获取异常: {e}", exc_info=True)

    async def _set_callback(self) -> bool:
        """设置消息回调地址"""
        result = await self._api_call("setCallback", {
            "token": self._token,
            "url": self._callback_url,
        })

        if result and result.get("ret") == 200:
            self._logger.info(f"消息回调地址设置成功: {self._callback_url}")
            return True
        else:
            self._logger.warning(f"消息回调地址设置失败: {result}")
            return False

    async def _check_online(self) -> bool:
        """检查微信是否在线"""
        result = await self._api_call("checkOnline", {
            "appId": self._app_id,
        })

        if result and result.get("ret") == 200:
            data = result.get("data", {})
            return data.get("status", "") == "online" if isinstance(data, dict) else True
        return False

    async def _post_text(self, to_wxid: str, content: str) -> bool:
        """发送文本消息"""
        result = await self._api_call("postText", {
            "appId": self._app_id,
            "toUserName": to_wxid,
            "content": content,
        })

        if result and result.get("ret") == 200:
            self._logger.info(f"微信文本消息发送成功: to={to_wxid}")
            return True
        else:
            self._logger.error(f"微信文本消息发送失败: {result}")
            return False

    async def _post_image(self, to_wxid: str, image_url: str) -> bool:
        """发送图片消息"""
        result = await self._api_call("postImage", {
            "appId": self._app_id,
            "toUserName": to_wxid,
            "imgUrl": image_url,
        })

        if result and result.get("ret") == 200:
            self._logger.info(f"微信图片消息发送成功: to={to_wxid}")
            return True
        else:
            self._logger.error(f"微信图片消息发送失败: {result}")
            return False

    async def _post_file(self, to_wxid: str, file_url: str) -> bool:
        """发送文件消息"""
        result = await self._api_call("postFile", {
            "appId": self._app_id,
            "toUserName": to_wxid,
            "fileUrl": file_url,
        })

        return result is not None and result.get("ret") == 200

    async def _post_link(self, to_wxid: str, title: str, **kwargs: Any) -> bool:
        """发送链接消息"""
        result = await self._api_call("postLink", {
            "appId": self._app_id,
            "toUserName": to_wxid,
            "title": title,
            "desc": kwargs.get("desc", ""),
            "linkUrl": kwargs.get("link_url", ""),
            "thumbUrl": kwargs.get("thumb_url", ""),
        })

        return result is not None and result.get("ret") == 200

    async def _api_call(self, api_name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """调用Gewechat API

        Args:
            api_name: 接口名称
            payload: 请求参数

        Returns:
            API响应数据或None
        """
        if not self._http_client:
            return None

        url = f"{self._base_url}/v2/api/{api_name}"
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["X-GEWE-TOKEN"] = self._token

        try:
            response = await self._http_client.post(url, json=payload, headers=headers)
            return response.json()
        except Exception as e:
            self._logger.error(f"Gewechat API调用失败 [{api_name}]: {e}", exc_info=True)
            return None

    @staticmethod
    def _extract_xml_text(xml_content: str) -> str:
        """从XML格式消息中提取纯文本"""
        try:
            # 简单提取 <content><![CDATA[文本]]></content> 中的文本
            import re
            match = re.search(r'<!\[CDATA\[(.*?)\]\]>', xml_content)
            if match:
                return match.group(1)
            return xml_content
        except Exception:
            return xml_content

    def get_status_info(self) -> dict[str, Any]:
        """获取微信通道状态信息"""
        info = super().get_status_info()
        info["base_url"] = self._base_url
        info["app_id"] = self._app_id[:10] + "..." if self._app_id else ""
        info["has_token"] = bool(self._token)
        return info

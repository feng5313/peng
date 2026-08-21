"""
微信 ClawBot (iLink) 通道

直接使用微信官方 ClawBot iLink API 接入微信，无需 OpenClaw Gateway。

iLink API 协议：
1. 扫码登录: POST https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode → 获取二维码
2. 轮询状态: GET https://ilinkai.weixin.qq.com/ilink/bot/get_qrcode_status → 等待扫码确认
3. 收消息: POST {baseurl}/ilink/bot/getupdates (长轮询)
4. 发消息: POST {baseurl}/ilink/bot/sendmessage
5. 通知启动: POST {baseurl}/ilink/bot/msg/notifystart
6. 通知停止: POST {baseurl}/ilink/bot/msg/notifystop

配置示例 (peng.json):
{
  "channels": {
    "wechat": {
      "enabled": true,
      "mode": "clawbot",
      "clawbot": {
        "bot_token": "xxx",
        "base_url": "https://xxx.weixin.qq.com"
      }
    }
  }
}
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
import uuid
from typing import Any

import httpx

from peng.channels.base import (
    ChannelBase,
    ChannelMessage,
    ChannelStatus,
    ChannelType,
)

logger = logging.getLogger(__name__)

# iLink 固定 API 地址
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"

# iLink App 信息 (来自微信官方 ClawBot 插件 package.json)
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = 132097  # 2.4.1 -> (2<<16)|(4<<8)|1 = 132097


class ClawBotWechatChannel(ChannelBase):
    """微信 ClawBot (iLink) 通道

    直接通过 iLink API 与微信通信，支持：
    - 扫码登录（WebUI 触发）
    - 长轮询收消息
    - 发送文本消息
    - 消息类型: 文本、图片、语音、文件、视频
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(ChannelType.WECHAT, config)

        # ClawBot 配置
        clawbot = config.get("clawbot", {})
        self._bot_token: str = clawbot.get("bot_token", "")
        self._base_url: str = clawbot.get("base_url", "")
        self._data_dir: str = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
            "data", "clawbot"
        )
        os.makedirs(self._data_dir, exist_ok=True)

        # 状态
        self._get_updates_buf: str = ""
        self._poll_task: asyncio.Task | None = None
        self._http_client: httpx.AsyncClient | None = None

        # 扫码登录状态
        self._login_session: dict[str, Any] | None = None

        # context_token 缓存（关键！iLink 需要此 token 关联回复消息到会话）
        # key: from_user_id (ilink user ID), value: context_token
        self._context_tokens: dict[str, str] = {}

    @property
    def is_connected(self) -> bool:
        return bool(self._bot_token and self._base_url) and self._status == ChannelStatus.CONNECTED

    def get_status_info(self) -> dict[str, Any]:
        return {
            "channel_type": "wechat",
            "mode": "clawbot",
            "status": self.status.value,
            "is_connected": self.is_connected,
            "has_token": bool(self._bot_token),
            "has_base_url": bool(self._base_url),
        }

    async def check_connection(self) -> bool:
        """检查与微信的连接是否正常"""
        if not self.is_connected:
            return False
        try:
            result = await self._api_post("ilink/bot/getupdates", {
                "get_updates_buf": self._get_updates_buf,
            })
            return result.get("ret", -1) == 0
        except Exception:
            return False

    async def start(self) -> None:
        """启动通道（长轮询收消息）"""
        if not self._bot_token or not self._base_url:
            logger.warning("ClawBot 微信通道缺少 bot_token 或 base_url，请先扫码登录")
            self._status = ChannelStatus.DISCONNECTED
            return

        self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(40.0))
        self._status = ChannelStatus.CONNECTING

        # 通知启动
        try:
            await self._api_post("ilink/bot/msg/notifystart", {})
            logger.info("ClawBot notifyStart 发送成功")
        except Exception as e:
            logger.warning(f"ClawBot notifyStart 失败: {e}")

        # 启动长轮询任务
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._status = ChannelStatus.CONNECTED
        logger.info("✅ ClawBot 微信通道已启动")

    async def stop(self) -> None:
        """停止通道"""
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None

        # 通知停止
        if self._http_client and self._bot_token and self._base_url:
            try:
                await self._api_post("ilink/bot/msg/notifystop", {})
            except Exception as e:
                logger.warning(f"ClawBot notifyStop 失败: {e}")

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        self._status = ChannelStatus.DISCONNECTED
        logger.info("ClawBot 微信通道已停止")

    async def send_message(self, message: ChannelMessage) -> bool:
        """发送文本消息到微信"""
        if not self.is_connected:
            logger.error("ClawBot 微信通道未连接，无法发送消息")
            return False

        # 确定接收者
        to_user_id = message.raw_data.get("from_user_id", "") if message.raw_data else ""
        if not to_user_id:
            logger.error("ClawBot 发送消息缺少 to_user_id")
            return False

        try:
            msg = {
                "message_type": 2,  # BOT
                "message_state": 2,  # FINISH
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": f"peng_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}",
                "item_list": [
                    {
                        "type": 1,  # TEXT
                        "text_item": {"text": message.content},
                    }
                ],
            }

            # 关键：携带 context_token（iLink 需要 context_token 关联回复到会话）
            context_token = message.raw_data.get("context_token", "") if message.raw_data else ""
            if not context_token:
                context_token = self._context_tokens.get(to_user_id, "")
            if context_token:
                msg["context_token"] = context_token
                logger.debug(f"ClawBot sendMessage 携带 context_token: to={to_user_id}")

            result = await self._api_post("ilink/bot/sendmessage", {"msg": msg})

            # 详细记录 API 响应（排查消息发送但不送达问题）
            logger.info(
                f"ClawBot sendMessage API 响应: {json.dumps(result, ensure_ascii=False, default=str)[:300]}"
            )

            # 检查 iLink API 级别错误（HTTP 200 但 API 返回错误码）
            ret = result.get("ret")
            errcode = result.get("errcode")
            is_error = (ret is not None and ret != 0) or (errcode is not None and errcode != 0)
            if is_error:
                errmsg = result.get("errmsg", "")
                logger.error(
                    f"ClawBot sendMessage API 错误: ret={ret}, errcode={errcode}, "
                    f"errmsg={errmsg}, to_user={to_user_id}, response={json.dumps(result, ensure_ascii=False, default=str)[:200]}"
                )
                return False

            logger.info(f"ClawBot 消息已发送给 {to_user_id}")
            return True
        except Exception as e:
            logger.error(f"ClawBot 发送消息失败: {e}", exc_info=True)
            return False

    # ==================== iLink API 方法 ====================

    def _build_headers(self) -> dict[str, str]:
        """构建 iLink API 请求头"""
        uin = base64.b64encode(str(hashlib.sha256(os.urandom(4)).hexdigest()[:8]).encode()).decode()
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {self._bot_token}",
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
            "X-WECHAT-UIN": uin,
        }

    def _build_base_info(self) -> dict[str, Any]:
        return {
            "channel_version": "2.3.1",
            "bot_agent": "Peng/1.0",
        }

    async def _api_post(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        """发送 iLink API POST 请求"""
        if not self._http_client:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(40.0))

        url = f"{self._base_url.rstrip('/')}/{endpoint}"
        body["base_info"] = self._build_base_info()
        data = json.dumps(body, ensure_ascii=False)

        resp = await self._http_client.post(
            url,
            content=data,
            headers=self._build_headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def _api_get(self, url: str) -> dict[str, Any]:
        """发送 iLink API GET 请求"""
        if not self._http_client:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(40.0))

        resp = await self._http_client.get(url, timeout=40.0)
        resp.raise_for_status()
        return resp.json()

    # ==================== 扫码登录 ====================

    async def get_qr_code(self) -> dict[str, Any]:
        """获取扫码登录二维码

        Returns:
            {"success": True, "qr_url": "https://...", "qrcode": "xxx"}
        """
        try:
            url = f"{ILINK_BASE_URL}/ilink/bot/get_bot_qrcode?bot_type=3"
            resp = await self._api_post_raw(url, {"local_token_list": []})
            data = resp.json()

            qr_code = data.get("qrcode", "")
            qr_url = data.get("qrcode_img_content", "")

            # 保存登录会话
            self._login_session = {
                "qrcode": qr_code,
                "qrcode_url": qr_url,
                "started_at": time.time(),
                "current_base_url": ILINK_BASE_URL,
            }

            logger.info(f"ClawBot 二维码获取成功，qr_url 长度={len(qr_url)}")
            return {"success": True, "qr_url": qr_url, "uuid": qr_code}
        except Exception as e:
            logger.error(f"获取 ClawBot 二维码失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def poll_login_status(self, qrcode: str) -> dict[str, Any]:
        """轮询扫码状态

        Returns:
            {"status": "wait|scaned|confirmed|expired", "bot_token": ..., "base_url": ...}
        """
        try:
            current_base = self._login_session.get("current_base_url", ILINK_BASE_URL) if self._login_session else ILINK_BASE_URL
            url = f"{current_base}/ilink/bot/get_qrcode_status?qrcode={qrcode}"

            if not self._http_client:
                self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(40.0))

            resp = await self._http_client.get(url, timeout=40.0)
            resp.raise_for_status()
            data = resp.json()

            status = data.get("status", "wait")
            result: dict[str, Any] = {"status": status}

            if status == "confirmed":
                bot_token = data.get("bot_token", "")
                base_url = data.get("baseurl", "")
                ilink_bot_id = data.get("ilink_bot_id", "")
                ilink_user_id = data.get("ilink_user_id", "")

                # 保存到配置和内存
                self._bot_token = bot_token
                self._base_url = base_url
                self._save_credentials(bot_token, base_url, ilink_bot_id, ilink_user_id)

                result["bot_token"] = bot_token
                result["base_url"] = base_url
                result["ilink_bot_id"] = ilink_bot_id
                result["ilink_user_id"] = ilink_user_id
                logger.info(f"ClawBot 扫码登录成功! bot_id={ilink_bot_id}")

            elif status == "scaned_but_redirect":
                redirect_host = data.get("redirect_host", "")
                if redirect_host:
                    new_base = f"https://{redirect_host}"
                    if self._login_session:
                        self._login_session["current_base_url"] = new_base
                    logger.info(f"ClawBot IDC 重定向: {redirect_host}")

            elif status == "scaned":
                result["message"] = "已扫码，等待确认..."

            elif status == "expired":
                result["message"] = "二维码已过期"

            return result
        except Exception as e:
            logger.error(f"轮询 ClawBot 扫码状态失败: {e}", exc_info=True)
            return {"status": "wait", "error": str(e)}

    async def _api_post_raw(self, url: str, body: dict[str, Any]) -> httpx.Response:
        """发送原始 POST 请求（用于登录阶段，无 token）"""
        if not self._http_client:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(40.0))

        return await self._http_client.post(
            url,
            json=body,
            headers={
                "Content-Type": "application/json",
                "iLink-App-Id": ILINK_APP_ID,
                "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
            },
        )

    def _save_credentials(self, bot_token: str, base_url: str, bot_id: str, user_id: str) -> None:
        """保存登录凭证到配置文件"""
        try:
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
                "config", "peng.json"
            )
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)

            if "channels" not in config:
                config["channels"] = {}
            if "wechat" not in config["channels"]:
                config["channels"]["wechat"] = {}
            config["channels"]["wechat"]["mode"] = "clawbot"
            config["channels"]["wechat"]["clawbot"] = {
                "bot_token": bot_token,
                "base_url": base_url,
                "bot_id": bot_id,
                "user_id": user_id,
            }

            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)

            logger.info("ClawBot 凭证已保存到配置文件")
        except Exception as e:
            logger.error(f"保存 ClawBot 凭证失败: {e}", exc_info=True)

    # ==================== 长轮询收消息 ====================

    async def _poll_loop(self) -> None:
        """长轮询循环：持续调用 getUpdates 获取新消息"""
        logger.info("ClawBot 长轮询启动")
        while True:
            try:
                await self._do_get_updates()
            except asyncio.CancelledError:
                logger.info("ClawBot 长轮询被取消")
                return
            except Exception as e:
                logger.error(f"ClawBot getUpdates 异常: {e}", exc_info=True)
                await asyncio.sleep(3)

    async def _do_get_updates(self) -> None:
        """调用 getUpdates 长轮询"""
        try:
            body = {
                "get_updates_buf": self._get_updates_buf,
            }
            result = await self._api_post("ilink/bot/getupdates", body)

            # OpenClaw monitor.ts 逻辑：ret 不存在（成功时省略）等同于 ret=0
            ret = result.get("ret")
            is_error = (ret is not None and ret != 0) or (result.get("errcode") is not None and result.get("errcode") != 0)
            if is_error:
                errcode = result.get("errcode", 0)
                errmsg = result.get("errmsg", "")
                logger.warning(
                    f"ClawBot getUpdates 返回错误: ret={ret}, errcode={errcode}, errmsg={errmsg}, "
                    f"base_url={self._base_url}, has_token={bool(self._bot_token)}, "
                    f"response_keys={list(result.keys())}"
                )
                if errcode == -14:
                    logger.error("ClawBot session 过期，需要重新扫码登录")
                    self._status = ChannelStatus.DISCONNECTED
                await asyncio.sleep(5)
                return

            # 更新 buffer
            self._get_updates_buf = result.get("get_updates_buf", "")

            # 处理消息
            msgs = result.get("msgs", [])
            for msg in msgs:
                self._handle_inbound_message(msg)

        except httpx.TimeoutException:
            # 长轮询超时是正常的
            pass
        except httpx.HTTPStatusError as e:
            logger.error(f"ClawBot getUpdates HTTP 错误: {e.response.status_code}")
            await asyncio.sleep(5)

    def _handle_inbound_message(self, msg: dict[str, Any]) -> None:
        """处理收到的微信消息

        iLink 消息结构（参考 OpenClaw @tencent-weixin/openclaw-weixin 源码 api/types.ts）：
        - message_type: 1=用户消息, 2=机器人消息
        - item_list: 消息内容列表，每个 item 有 type:
          - 1=TEXT(text_item), 2=IMAGE(image_item), 3=VOICE(voice_item),
            4=FILE(file_item), 5=VIDEO(video_item)
        - 引用消息: item_list[].ref_msg.message_item 包含被引用的消息
          - ref_msg.title: 引用摘要
          - ref_msg.message_item.msg_id: 被引用消息的 message_id
          - ref_msg.message_item.text_item.text: 被引用消息的文本
        """
        msg_type = msg.get("message_type", 0)
        from_user_id = msg.get("from_user_id", "")
        to_user_id = msg.get("to_user_id", "")
        msg_id = msg.get("message_id", 0)
        create_time = msg.get("create_time_ms", 0)
        session_id = msg.get("session_id", "")
        context_token = msg.get("context_token", "")

        # 只处理用户消息 (type=1)
        if msg_type != 1:
            return

        # 调试：打印完整消息结构（用于排查引用消息识别问题）
        logger.info(
            f"ClawBot 原始消息: msg_id={msg_id}, from={from_user_id}, "
            f"keys={sorted(msg.keys())}, "
            f"item_count={len(msg.get('item_list', []))}, "
            f"session_id={session_id}"
        )
        for idx, item in enumerate(msg.get("item_list", [])):
            logger.info(
                f"ClawBot item[{idx}]: type={item.get('type')}, "
                f"has_ref_msg={'ref_msg' in item}, "
                f"keys={sorted(item.keys())}"
            )
            if "ref_msg" in item:
                logger.info(
                    f"ClawBot ref_msg[{idx}]: {json.dumps(item['ref_msg'], ensure_ascii=False, default=str)}"
                )

        # 解析消息内容 + 提取引用信息 + 下载附件
        content_parts = []
        attachment_paths: list[str] = []  # 下载后的附件本地路径
        ref_msg_id = ""  # 被引用消息的 message_id
        ref_msg_text = ""  # 被引用消息的摘要文本
        ref_msg_create_time_ms = 0  # 被引用消息的创建时间（用于图片等无 msg_id 的引用消息匹配）
        ref_msg_type = 0  # 被引用消息的类型（1=文本 2=图片 等）
        has_ref_msg = False  # 是否存在引用消息
        item_list = msg.get("item_list", [])

        for item in item_list:
            item_type = item.get("type", 0)

            if item_type == 1:  # TEXT
                text = item.get("text_item", {}).get("text", "")
                if text:
                    content_parts.append(text)

                # 提取引用消息信息（参考 OpenClaw inbound.ts bodyFromItemList）
                ref_msg = item.get("ref_msg")
                if ref_msg:
                    has_ref_msg = True
                    # 调试：打印完整的 ref_msg 结构
                    logger.info(f"ClawBot ref_msg 原始数据: {json.dumps(ref_msg, ensure_ascii=False, default=str)}")
                    ref_message_item = ref_msg.get("message_item", {})
                    ref_msg_id = str(ref_message_item.get("msg_id", ""))
                    ref_msg_type = ref_message_item.get("type", 0)
                    ref_msg_create_time_ms = ref_message_item.get("create_time_ms", 0)
                    # 注意：title字段未启用，不使用ref_msg.get("title")
                    # ref_msg_text 直接取 message_item.text_item.text（iLink摘要）
                    ref_text = ref_message_item.get("text_item", {}).get("text", "")
                    if ref_text:
                        ref_msg_text = ref_text

            elif item_type == 2:  # IMAGE
                img = item.get("image_item", {})
                # 微信 iLink 图片结构：
                #   aeskey: hex格式的AES-ECB密钥（32字符=128bit）
                #   media.full_url: CDN加密下载URL
                #   media.aes_key: base64格式的密钥
                #   media.encrypt_query_param: 加密参数
                #   mid_size/hd_size: 原始大小
                #   thumb_size: 缩略图大小
                aeskey = img.get("aeskey", "")
                media = img.get("media", {})
                full_url = media.get("full_url", "") if media else ""

                if full_url and aeskey:
                    content_parts.append("[图片]")
                    downloaded = self._download_wechat_image_sync(
                        full_url, aeskey, "image"
                    )
                    if downloaded:
                        attachment_paths.append(downloaded)
                else:
                    content_parts.append("[图片]")
                    logger.debug(
                        f"ClawBot 图片消息无可下载URL，image_item keys={list(img.keys())}"
                    )

                # 图片也可能有引用
                ref_msg = item.get("ref_msg")
                if ref_msg:
                    has_ref_msg = True
                    ref_message_item = ref_msg.get("message_item", {})
                    ref_msg_id = str(ref_message_item.get("msg_id", ""))
                    ref_msg_type = ref_message_item.get("type", 0)
                    ref_msg_create_time_ms = ref_message_item.get("create_time_ms", 0)

            elif item_type == 3:  # VOICE
                voice = item.get("voice_item", {})
                text = voice.get("text", "")
                playtime = voice.get("playtime", 0)
                if text:
                    content_parts.append(f"[语音转文字: {text}]")
                else:
                    content_parts.append(f"[语音: {playtime}ms]")
                # 语音下载：尝试多种URL字段
                voice_url = (voice.get("url") or voice.get("download_url")
                             or voice.get("cdn_url") or "")
                if voice_url:
                    downloaded = self._download_attachment_sync(
                        voice_url, "voice.amr", "voice"
                    )
                    if downloaded:
                        attachment_paths.append(downloaded)

            elif item_type == 4:  # FILE
                f = item.get("file_item", {})
                fname = f.get("file_name", "未知文件")
                content_parts.append(f"[文件: {fname}]")
                # 文件下载：尝试多种URL字段
                file_url = (f.get("url") or f.get("download_url")
                            or f.get("cdn_url") or "")
                if file_url:
                    downloaded = self._download_attachment_sync(
                        file_url, fname, "file"
                    )
                    if downloaded:
                        attachment_paths.append(downloaded)

            elif item_type == 5:  # VIDEO
                video = item.get("video_item", {})
                content_parts.append("[视频]")
                # 视频下载：尝试多种URL字段
                video_url = (video.get("url") or video.get("download_url")
                             or video.get("cdn_url") or "")
                if video_url:
                    downloaded = self._download_attachment_sync(
                        video_url, "video.mp4", "video"
                    )
                    if downloaded:
                        attachment_paths.append(downloaded)

        content = "\n".join(content_parts) if content_parts else "[空消息]"

        # 附件信息追加到消息内容（参考飞书通道附件处理方式）
        if attachment_paths:
            content += "\n" + "\n".join(
                f"[附件] {os.path.basename(p)}: {p}" for p in attachment_paths
            )

        # 如果有引用消息，在内容前加上引用标记（参考 OpenClaw inbound.ts 第189行）
        if ref_msg_text:
            content = f"[引用: {ref_msg_text}]\n{content}"

        logger.info(
            f"ClawBot 收到微信消息: from={from_user_id}, msg_id={msg_id}, "
            f"ref_msg_id={ref_msg_id or '无'}, has_ref_msg={has_ref_msg}, "
            f"ref_msg_type={ref_msg_type}, ref_msg_create_time_ms={ref_msg_create_time_ms}, "
            f"content_len={len(content)}, context_token={'有' if context_token else '无'}"
        )

        # 保存 context_token（关键！iLink 需要 context_token 关联回复到会话）
        if context_token and from_user_id:
            self._context_tokens[from_user_id] = context_token
            logger.debug(f"ClawBot 保存 context_token: user={from_user_id}")

        # 构造 ChannelMessage 并触发回调
        channel_msg = ChannelMessage(
            channel_type=ChannelType.WECHAT,
            platform_msg_id=str(msg_id),
            sender_id=from_user_id,
            sender_name="",
            receiver_id=to_user_id,
            content=content,
            is_group=False,
            group_name="",
            raw_data={
                "from_user_id": from_user_id,
                "to_user_id": to_user_id,
                "session_id": session_id,
                "context_token": context_token,
                "create_time_ms": create_time,
                # 微信引用消息标识（核心！用于消息树 reply_to 映射）
                "ref_msg_id": ref_msg_id,
                "ref_msg_text": ref_msg_text,
                # 引用消息的元数据（当 ref_msg_id 为空时用于辅助匹配）
                "ref_msg_create_time_ms": ref_msg_create_time_ms,
                "ref_msg_type": ref_msg_type,
                "has_ref_msg": has_ref_msg,
                # 下载后的附件本地路径列表
                "attachment_paths": attachment_paths,
            },
        )

        self._notify_message(channel_msg)

    # ==================== 附件下载 ====================

    def _get_attachment_dir(self) -> str:
        """获取附件保存目录"""
        from peng.config.loader import load_config
        config = load_config()
        attach_dir = os.path.join(str(config.paths.data_dir_path), "wechat_attachments")
        os.makedirs(attach_dir, exist_ok=True)
        return attach_dir

    def _download_wechat_image_sync(
        self, cdn_url: str, aeskey_hex: str, res_type: str
    ) -> str | None:
        """同步下载并解密微信图片

        微信 iLink CDN 图片是 AES-ECB 加密的：
        1. 从 image_item.media.full_url 下载加密数据
        2. 用 image_item.aeskey（hex格式，32字符=128bit）作为 AES-ECB key
        3. 解密后即为标准图片文件（JPEG/PNG等）

        Args:
            cdn_url: CDN 下载 URL（image_item.media.full_url）
            aeskey_hex: AES 密钥（hex格式，如 "7f02d821..."）
            res_type: 资源类型描述（用于日志）

        Returns:
            下载解密后的本地文件路径，失败返回 None
        """
        if not cdn_url:
            return None
        try:
            import requests
            resp = requests.get(cdn_url, timeout=120)
            if resp.status_code != 200:
                logger.error(
                    f"下载微信{res_type}失败: status={resp.status_code}"
                )
                return None

            encrypted_data = resp.content

            # AES-ECB 解密
            from Crypto.Cipher import AES
            key = bytes.fromhex(aeskey_hex)
            cipher = AES.new(key, AES.MODE_ECB)
            decrypted_data = cipher.decrypt(encrypted_data)

            # 根据文件头判断图片格式
            ext = ".jpg"  # 默认
            if decrypted_data[:3] == b'\xff\xd8\xff':
                ext = ".jpg"
            elif decrypted_data[:8] == b'\x89PNG\r\n\x1a\n':
                ext = ".png"
            elif decrypted_data[:4] == b'RIFF':
                ext = ".webp"
            elif decrypted_data[:4] == b'GIF8':
                ext = ".gif"

            # 生成文件名并保存
            attach_dir = self._get_attachment_dir()
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            short_hash = hashlib.md5(cdn_url.encode()).hexdigest()[:8]
            safe_name = f"{timestamp}_{short_hash}{ext}"
            filepath = os.path.join(attach_dir, safe_name)

            with open(filepath, "wb") as f:
                f.write(decrypted_data)

            file_size = os.path.getsize(filepath)
            logger.info(
                f"✅ 微信{res_type}已下载解密: {filepath} ({file_size} bytes)"
            )
            return filepath
        except ImportError:
            logger.error(
                "下载微信图片需要 pycryptodome 库，请安装: pip install pycryptodome"
            )
            return None
        except Exception as e:
            logger.error(f"下载微信{res_type}异常: {e}", exc_info=True)
            return None

    def _download_attachment_sync(
        self, url: str, filename: str, res_type: str
    ) -> str | None:
        """同步下载微信附件（图片/文件/语音/视频）

        微信 ClawBot 的附件 URL 通常是 CDN 直链，可直接通过 HTTP GET 下载。

        Args:
            url: 附件下载 URL（CDN 直链）
            filename: 保存的文件名（含扩展名）
            res_type: 资源类型描述（用于日志：image/file/voice/video）

        Returns:
            下载后的本地文件路径，失败返回 None
        """
        if not url:
            return None
        try:
            import requests
            resp = requests.get(url, timeout=120, stream=True)

            if resp.status_code != 200:
                logger.error(
                    f"下载微信{res_type}失败: status={resp.status_code}, "
                    f"body={resp.text[:200]}"
                )
                return None

            # 生成文件名（避免冲突）
            attach_dir = self._get_attachment_dir()
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            short_hash = hashlib.md5(url.encode()).hexdigest()[:8]
            # 确保文件名有扩展名
            _, ext = os.path.splitext(filename)
            if not ext:
                ext = self._guess_extension_from_content_type(
                    resp.headers.get("Content-Type", ""), res_type
                )
            safe_name = f"{timestamp}_{short_hash}{ext}"
            filepath = os.path.join(attach_dir, safe_name)

            # 写入文件
            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            file_size = os.path.getsize(filepath)

            # 文本文件自动转UTF-8编码
            if ext.lower() in {
                ".txt", ".md", ".py", ".js", ".ts", ".json", ".xml", ".html",
                ".css", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf",
                ".sh", ".bat", ".csv", ".log", ".sql",
            }:
                self._convert_to_utf8(filepath)

            logger.info(
                f"✅ 微信{res_type}已下载: {filepath} ({file_size} bytes)"
            )
            return filepath
        except Exception as e:
            logger.error(f"下载微信{res_type}异常: {e}", exc_info=True)
            return None

    @staticmethod
    def _guess_extension_from_content_type(content_type: str, res_type: str) -> str:
        """根据 Content-Type 猜测文件扩展名"""
        ct = content_type.lower().split(";")[0].strip()
        mime_map = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
            "audio/amr": ".amr",
            "audio/mpeg": ".mp3",
            "audio/mp4": ".m4a",
            "video/mp4": ".mp4",
            "application/pdf": ".pdf",
            "application/zip": ".zip",
            "application/octet-stream": ".bin",
        }
        if ct in mime_map:
            return mime_map[ct]
        # 回退：根据资源类型给默认扩展名
        type_default = {
            "image": ".jpg",
            "voice": ".amr",
            "video": ".mp4",
            "file": "",
        }
        return type_default.get(res_type, "")

    @staticmethod
    def _convert_to_utf8(filepath: str) -> None:
        """将文本文件转换为UTF-8编码（如果还不是UTF-8）"""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                f.read()
            return
        except UnicodeDecodeError:
            pass

        for encoding in ["gb18030", "gbk", "gb2312", "big5", "shift_jis", "latin-1"]:
            try:
                with open(filepath, "r", encoding=encoding) as f:
                    content = f.read()
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
                return
            except (UnicodeDecodeError, UnicodeEncodeError):
                continue

    # ==================== Gewechat 兼容接口 ====================

    def handle_callback(self, data: dict[str, Any]) -> None:
        """Gewechat 兼容接口，ClawBot 模式不使用"""
        pass

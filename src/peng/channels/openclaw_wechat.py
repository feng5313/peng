"""
微信 Channel 实现（基于微信官方 ClawBot / iLink API）

直接通过微信官方 iLink API 收发消息，无需安装 OpenClaw。

微信 ClawBot 是微信官方提供的 Bot 接入方式（通过微信「设置→插件」启用）。
iLink API 是腾讯提供的 Bot HTTP API，支持：
- 扫码授权登录（获取 bot_token）
- 长轮询接收消息（getUpdates）
- 发送文本/图片/文件消息（sendMessage）
- 发送正在输入状态（sendTyping）

架构：
  微信 ←(iLink)→ 腾讯 iLink 服务器 ←(HTTP REST)→ 鹏🐣 ILinkWechatChannel

配置项（config/peng.json → channels.wechat）：
{
    "enabled": true,
    "mode": "clawbot",
    "ilink": {
        "token": "扫码授权后获取的 bot_token",
        "base_url": "https://ilinkai.weixin.qq.com"  // 可选，默认值
    }
}
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from peng.channels.base import ChannelBase, ChannelMessage, ChannelStatus, ChannelType

logger = logging.getLogger(__name__)

__all__ = ["ILinkWechatChannel"]

# iLink 固定 API 地址
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
# CDN 基础地址
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
# iLink App ID (from ClawBot 插件 package.json)
ILINK_APP_ID = "bot"
# 长轮询超时
LONG_POLL_TIMEOUT_MS = 35_000
# 普通请求超时
API_TIMEOUT_MS = 15_000
# 凭证存储路径
CREDENTIALS_DIR = Path("data/ilink_wechat")
CREDENTIALS_FILE = CREDENTIALS_DIR / "credentials.json"


class ILinkWechatChannel(ChannelBase):
    """微信消息通道（iLink API / ClawBot）

    直接通过微信官方 iLink HTTP API 实现微信消息收发。

    工作流程：
    1. 从配置/凭证文件加载 bot_token
    2. 长轮询 getUpdates 接收消息
    3. 收到消息后转换为 ChannelMessage 通知回调
    4. 通过 sendMessage 发送回复
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(ChannelType.WECHAT, config)

        # iLink 配置
        ilink_config = config.get("ilink", {})
        self._token: str = ilink_config.get("token", "")
        self._base_url: str = ilink_config.get("base_url", ILINK_BASE_URL)

        # HTTP 客户端
        self._http_client: httpx.AsyncClient | None = None

        # 长轮询状态
        self._get_updates_buf: str = ""
        self._poll_task: asyncio.Task | None = None
        self._running: bool = False

        # 从凭证文件加载 token（如果配置中没有）
        if not self._token:
            self._load_credentials()

        if not self._base_url.endswith("/"):
            self._base_url += "/"

    def _load_credentials(self) -> None:
        """从本地凭证文件加载 bot_token"""
        try:
            if CREDENTIALS_FILE.exists():
                data = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
                self._token = data.get("token", "")
                if data.get("base_url"):
                    self._base_url = data["base_url"]
                if self._token:
                    self._logger.info("从凭证文件加载了 iLink bot_token")
        except Exception as e:
            self._logger.warning(f"加载凭证文件失败: {e}")

    def _save_credentials(self, token: str, base_url: str = "", user_id: str = "") -> None:
        """保存 bot_token 到本地凭证文件"""
        try:
            CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
            data: dict[str, str] = {"token": token}
            if base_url:
                data["base_url"] = base_url
            if user_id:
                data["user_id"] = user_id
            CREDENTIALS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            self._logger.info("iLink bot_token 已保存到凭证文件")
        except Exception as e:
            self._logger.warning(f"保存凭证文件失败: {e}")

    def _build_headers(self, with_auth: bool = True) -> dict[str, str]:
        """构建 iLink API 请求头"""
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": "196617",  # 2.3.1 => (2<<16)|(3<<8)|1 = 131329
            "X-WECHAT-UIN": "",  # 可选
        }
        if with_auth and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
            headers["AuthorizationType"] = "ilink_bot_token"
        return headers

    async def start(self) -> None:
        """启动 iLink 微信通道"""
        if not self._token:
            self._logger.error(
                "❌ 未配置 iLink bot_token。请先执行扫码登录：\n"
                "  python -m peng.channels.openclaw_wechat login\n"
                "或在 config/peng.json 的 channels.wechat.ilink.token 中配置。"
            )
            self._status = ChannelStatus.ERROR
            return

        self._status = ChannelStatus.CONNECTING
        self._logger.info("正在启动 iLink 微信通道...")

        try:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=10.0))

            # 发送 notifyStart
            await self._notify_start()

            self._running = True
            self._status = ChannelStatus.CONNECTED
            self._logger.info("✅ iLink 微信通道已启动")

            # 启动长轮询
            self._poll_task = asyncio.create_task(self._poll_updates())

        except Exception as e:
            self._status = ChannelStatus.ERROR
            self._logger.error(f"❌ iLink 微信通道启动失败: {e}", exc_info=True)

    async def _notify_start(self) -> None:
        """通知 iLink 服务器客户端启动"""
        try:
            resp = await self._http_client.post(  # type: ignore
                f"{self._base_url}ilink/bot/msg/notifystart",
                headers=self._build_headers(),
                json={"base_info": {"channel_version": "2.3.1"}},
                timeout=10.0,
            )
            self._logger.debug(f"notifyStart: status={resp.status_code}")
        except Exception as e:
            self._logger.debug(f"notifyStart 失败（可忽略）: {e}")

    async def _poll_updates(self) -> None:
        """长轮询获取微信消息"""
        self._logger.info("开始长轮询 getUpdates...")

        while self._running:
            try:
                body = {
                    "get_updates_buf": self._get_updates_buf or "",
                    "base_info": {"channel_version": "2.3.1"},
                }

                resp = await self._http_client.post(  # type: ignore
                    f"{self._base_url}ilink/bot/getupdates",
                    headers=self._build_headers(),
                    json=body,
                    timeout=40.0,  # 略大于 longpolling_timeout
                )

                if resp.status_code != 200:
                    self._logger.error(f"getUpdates HTTP 错误: {resp.status_code} {resp.text[:200]}")
                    await asyncio.sleep(5)
                    continue

                data = resp.json()

                # 检查错误
                ret = data.get("ret")
                if ret is not None and ret != 0:
                    errcode = data.get("errcode", "")
                    errmsg = data.get("errmsg", "")
                    self._logger.error(f"getUpdates API 错误: ret={ret} errcode={errcode} errmsg={errmsg}")
                    if errcode == -14:
                        self._logger.error("会话超时，可能需要重新扫码登录")
                        self._status = ChannelStatus.ERROR
                        return
                    await asyncio.sleep(5)
                    continue

                # 更新同步 buffer
                new_buf = data.get("get_updates_buf", "")
                if new_buf:
                    self._get_updates_buf = new_buf

                # 处理消息
                msgs = data.get("msgs", [])
                for msg in msgs:
                    await self._handle_ilink_message(msg)

            except httpx.TimeoutException:
                # 长轮询超时正常，继续
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._logger.error(f"getUpdates 异常: {e}", exc_info=True)
                await asyncio.sleep(3)

        self._logger.info("getUpdates 长轮询已停止")

    async def _handle_ilink_message(self, msg: dict[str, Any]) -> None:
        """处理 iLink 消息

        iLink WeixinMessage 结构：
        {
            "seq": 123,
            "message_id": 456,
            "from_user_id": "wxid_xxx",
            "to_user_id": "bot_id",
            "create_time_ms": 1709000000000,
            "message_type": 1,  // 1=用户消息
            "message_state": 2,  // 2=完成
            "item_list": [
                {"type": 1, "text_item": {"text": "消息内容"}}
            ],
            "context_token": "xxx"
        }
        """
        try:
            # 只处理用户消息 (message_type=1)
            message_type = msg.get("message_type", 0)
            if message_type != 1:
                return

            from_user_id = msg.get("from_user_id", "")
            to_user_id = msg.get("to_user_id", "")
            message_id = msg.get("message_id", 0)
            context_token = msg.get("context_token", "")

            # 提取文本内容
            text = ""
            item_list = msg.get("item_list", [])
            for item in item_list:
                item_type = item.get("type", 0)
                if item_type == 1:  # TEXT
                    text_item = item.get("text_item", {})
                    text += text_item.get("text", "")

            if not text:
                return

            # 保存 context_token 用于回复
            if context_token:
                self._last_context_token = context_token  # type: ignore[attr-defined]
                self._last_from_user_id = from_user_id  # type: ignore[attr-defined]

            # 构建 ChannelMessage
            channel_msg = ChannelMessage(
                content=text,
                sender_id=from_user_id,
                sender_name="",
                receiver_id=to_user_id,
                msg_type="text",
                raw_data=msg,
                channel_type=ChannelType.WECHAT,
                platform_msg_id=str(message_id),
                is_group=False,
                group_name="",
            )

            self._logger.info(
                f"收到 iLink 微信消息: sender={from_user_id}, "
                f"content={text[:50]}..."
            )

            # 通知消息回调
            self._notify_message(channel_msg)

        except Exception as e:
            self._logger.error(f"处理 iLink 消息失败: {e}", exc_info=True)

    async def stop(self) -> None:
        """停止通道"""
        self._running = False

        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        # 发送 notifyStop
        if self._http_client and self._token:
            try:
                await self._http_client.post(
                    f"{self._base_url}ilink/bot/msg/notifystop",
                    headers=self._build_headers(),
                    json={"base_info": {"channel_version": "2.3.1"}},
                    timeout=5.0,
                )
            except Exception:
                pass

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        self._status = ChannelStatus.DISCONNECTED
        self._logger.info("iLink 微信通道已停止")

    async def send_message(
        self,
        receiver_id: str,
        content: str,
        msg_type: str = "text",
        **kwargs: Any,
    ) -> bool:
        """通过 iLink API 发送微信消息

        Args:
            receiver_id: 接收者ID（iLink user ID / wxid）
            content: 消息内容
            msg_type: 消息类型（目前仅支持 text）

        Returns:
            True 表示发送成功
        """
        if not self._http_client or not self._token:
            self._logger.error("iLink 微信通道未启动或未配置 token")
            return False

        try:
            # 构建 sendMessage 请求体
            body = {
                "msg": {
                    "from_user_id": "",  # bot 自己的 ID，可留空
                    "to_user_id": receiver_id,
                    "message_type": 2,  # 2=BOT 消息
                    "message_state": 2,  # 2=FINISH
                    "item_list": [
                        {
                            "type": 1,  # TEXT
                            "text_item": {"text": content},
                        }
                    ],
                },
                "base_info": {"channel_version": "2.3.1"},
            }

            # 如果有 context_token，附上（用于关联回复）
            context_token = getattr(self, "_last_context_token", "")
            if context_token:
                body["msg"]["context_token"] = context_token  # type: ignore[typeddict-item]

            resp = await self._http_client.post(
                f"{self._base_url}ilink/bot/sendmessage",
                headers=self._build_headers(),
                json=body,
                timeout=15.0,
            )

            if resp.status_code == 200:
                self._logger.info(f"iLink 微信消息发送成功: to={receiver_id}")
                return True
            else:
                self._logger.error(
                    f"iLink 微信消息发送失败: HTTP {resp.status_code} {resp.text[:200]}"
                )
                return False

        except Exception as e:
            self._logger.error(f"发送消息异常: {e}", exc_info=True)
            return False

    async def check_connection(self) -> bool:
        """检查连接状态"""
        return self._running and self._status == ChannelStatus.CONNECTED

    def get_status_info(self) -> dict[str, Any]:
        """获取通道状态信息"""
        info = super().get_status_info()
        info["mode"] = "clawbot"
        info["base_url"] = self._base_url
        info["has_token"] = bool(self._token)
        return info


# ============================
# 扫码登录功能（命令行工具）
# ============================


async def qr_login() -> None:
    """执行微信 ClawBot 扫码登录，获取 bot_token。

    使用方法：
        python -m peng.channels.openclaw_wechat login
    """
    import sys
    import secrets

    print("=" * 60)
    print("微信 ClawBot 扫码登录")
    print("=" * 60)

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        # 1. 获取二维码
        print("\n📱 正在获取二维码...")
        try:
            headers = {
                "Content-Type": "application/json",
                "iLink-App-Id": ILINK_APP_ID,
                "iLink-App-ClientVersion": "132097",  # 2.4.1 -> (2<<16)|(4<<8)|1
            }
            resp = await client.post(
                f"{ILINK_BASE_URL}/ilink/bot/get_bot_qrcode?bot_type=3",
                headers=headers,
                json={"local_token_list": []},
                timeout=15.0,
            )

            if resp.status_code != 200:
                print(f"❌ 获取二维码失败: HTTP {resp.status_code}")
                print(resp.text[:500])
                sys.exit(1)

            data = resp.json()
            qrcode = data.get("qrcode", "")
            qrcode_url = data.get("qrcode_img_content", "")

            if not qrcode_url:
                print("❌ 获取二维码失败：服务端未返回二维码链接")
                sys.exit(1)

        except Exception as e:
            print(f"❌ 获取二维码异常: {e}")
            sys.exit(1)

        # 2. 显示二维码链接
        print(f"\n🔗 请用手机微信扫描以下二维码链接：")
        print(f"   {qrcode_url}")
        print(f"\n   或直接在浏览器打开上面的链接查看二维码。")

        # 3. 轮询等待扫码确认
        print("\n⏳ 等待扫码确认...")
        # IDC 重定向时会更新（参考 OpenClaw login-qr.ts activeLogin.currentApiBaseUrl）
        current_base = ILINK_BASE_URL
        deadline = time.time() + 480  # 8 分钟超时
        scanned_printed = False

        while time.time() < deadline:
            try:
                status_url = f"{current_base}/ilink/bot/get_qrcode_status?qrcode={qrcode}"
                status_resp = await client.get(
                    status_url,
                    headers={
                        "iLink-App-Id": ILINK_APP_ID,
                        "iLink-App-ClientVersion": "132097",
                    },
                    timeout=40.0,
                )

                if status_resp.status_code != 200:
                    print(f".", end="", flush=True)
                    await asyncio.sleep(3)
                    continue

                status_data = status_resp.json()
                status = status_data.get("status", "wait")

                if status == "wait":
                    print(".", end="", flush=True)
                elif status == "scaned":
                    if not scanned_printed:
                        print("\n✅ 已扫描，请在手机上确认...")
                        scanned_printed = True
                elif status == "scaned_but_redirect":
                    # 关键！IDC 重定向：切换轮询 host（参考 OpenClaw login-qr.ts 第391-401行）
                    redirect_host = status_data.get("redirect_host", "")
                    if redirect_host:
                        current_base = f"https://{redirect_host}"
                        print(f"\n   (IDC 重定向: 切换到 {redirect_host})")
                    if not scanned_printed:
                        print("\n✅ 已扫描，IDC 重定向中...")
                        scanned_printed = True
                elif status == "confirmed":
                    bot_token = status_data.get("bot_token", "")
                    ilink_bot_id = status_data.get("ilink_bot_id", "")
                    base_url = status_data.get("baseurl", ILINK_BASE_URL)
                    ilink_user_id = status_data.get("ilink_user_id", "")

                    print(f"\n\n🎉 登录成功！")
                    print(f"   bot_id: {ilink_bot_id}")
                    print(f"   user_id: {ilink_user_id}")
                    print(f"   base_url: {base_url}")

                    if bot_token:
                        # 保存凭证
                        CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
                        cred = {
                            "token": bot_token,
                            "bot_id": ilink_bot_id,
                            "base_url": base_url,
                            "user_id": ilink_user_id,
                            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        }
                        CREDENTIALS_FILE.write_text(
                            json.dumps(cred, indent=2, ensure_ascii=False),
                            encoding="utf-8",
                        )
                        print(f"\n   ✅ 凭证已保存到 {CREDENTIALS_FILE}")

                        # 同时更新 peng.json
                        print(f"\n   接下来请更新 config/peng.json：")
                        print(f'   channels.wechat.ilink.token = "{bot_token}"')
                        if base_url != ILINK_BASE_URL:
                            print(f'   channels.wechat.ilink.base_url = "{base_url}"')
                    else:
                        print("   ⚠️ 未获取到 bot_token")

                    return

                elif status == "expired":
                    print("\n⏳ 二维码已过期，请重新运行登录命令")
                    sys.exit(1)
                elif status == "binded_redirect":
                    print("\n✅ 此微信号已绑定过，无需重复登录")
                    sys.exit(0)

            except httpx.TimeoutException:
                # 长轮询超时，继续
                continue
            except Exception as e:
                print(f"\n轮询异常: {e}")
                await asyncio.sleep(3)

        print("\n⏰ 登录超时，请重新运行命令")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        asyncio.run(qr_login())
    else:
        print("用法: python -m peng.channels.openclaw_wechat login")

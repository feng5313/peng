"""
Channel HTTP回调路由

集成到FastAPI WebUI服务器中，处理：
- 飞书事件订阅回调
- 微信Gewechat回调
- 微信ClawBot (iLink) 扫码登录和状态

路由：
- POST /api/channels/feishu/callback - 飞书事件回调
- POST /api/channels/wechat/callback - 微信Gewechat回调
- GET /api/channels/status - 通道状态查询
- GET /api/channels/clawbot/status - ClawBot微信通道状态
- GET /api/channels/clawbot/qrcode - 获取扫码登录二维码
- GET /api/channels/clawbot/poll-login - 轮询扫码状态
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request, Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/channels", tags=["channels"])

# 全局ChannelManager引用（由init_channels设置）
_channel_manager: Any = None


def init_channels(channel_manager: Any) -> None:
    """初始化Channel路由，设置ChannelManager引用"""
    global _channel_manager
    _channel_manager = channel_manager
    logger.info("Channel HTTP回调路由已初始化")


@router.post("/feishu/callback")
async def feishu_callback(request: Request) -> Response:
    """飞书事件订阅回调

    飞书将事件数据POST到此URL。

    处理：
    1. URL验证请求（首次配置时）
    2. 消息接收事件（im.message.receive_v1）
    """
    if not _channel_manager:
        return Response(content="Channel manager not initialized", status_code=503)

    feishu_channel = _channel_manager.get_channel(
        __import__("peng.channels.base", fromlist=["ChannelType"]).ChannelType.FEISHU
    )
    if not feishu_channel:
        return Response(content="Feishu channel not registered", status_code=404)

    try:
        body = await request.json()

        # 处理URL验证请求
        if body.get("type") == "url_verification":
            result = feishu_channel.verify_url_challenge(body)
            if result is not None:
                return Response(
                    content=json.dumps(result),
                    media_type="application/json",
                )
            return Response(content="Verification failed", status_code=403)

        # 处理事件回调
        # 飞书事件结构：{"schema": "2.0", "header": {...}, "event": {...}}
        feishu_channel.handle_event_callback(body)

        return Response(content='{"code": 0}', media_type="application/json")

    except Exception as e:
        logger.error(f"飞书回调处理异常: {e}", exc_info=True)
        return Response(content=str(e), status_code=500)


@router.post("/wechat/callback")
async def wechat_callback(request: Request) -> Response:
    """微信Gewechat回调

    Gewechat将消息数据POST到此URL。
    """
    if not _channel_manager:
        return Response(content="Channel manager not initialized", status_code=503)

    wechat_channel = _channel_manager.get_channel(
        __import__("peng.channels.base", fromlist=["ChannelType"]).ChannelType.WECHAT
    )
    if not wechat_channel:
        return Response(content="Wechat channel not registered", status_code=404)

    try:
        body = await request.json()

        # Gewechat回调数据直接是消息数据
        wechat_channel.handle_callback(body)

        return Response(content='{"ret": 200}', media_type="application/json")

    except Exception as e:
        logger.error(f"微信回调处理异常: {e}", exc_info=True)
        return Response(content=str(e), status_code=500)


@router.get("/status")
async def channels_status() -> dict[str, Any]:
    """查询所有通道状态"""
    if not _channel_manager:
        return {"channels": {}, "error": "Channel manager not initialized"}

    return {
        "channels": _channel_manager.get_all_status(),
        "is_running": _channel_manager.is_running,
        "active_channels": [ct.value for ct in _channel_manager.active_channels],
    }


@router.get("/config")
async def get_channels_config() -> dict[str, Any]:
    """获取当前Channel配置（脱敏，已合并 data/peng.json 覆盖层）"""
    try:
        channels = _load_config().get("channels", {})
        # 脱敏处理
        result = {
            "enabled": channels.get("enabled", False),
            "feishu": {
                "enabled": channels.get("feishu", {}).get("enabled", False),
                "app_id": channels.get("feishu", {}).get("app_id", ""),
                "app_secret": "***已配置***" if channels.get("feishu", {}).get("app_secret") else "",
                "bot_name": channels.get("feishu", {}).get("bot_name", ""),
            },
            "wechat": {
                "enabled": channels.get("wechat", {}).get("enabled", False),
                "clawbot": {
                    "has_bot_token": bool(channels.get("wechat", {}).get("clawbot", {}).get("bot_token", "")),
                    "base_url": channels.get("wechat", {}).get("clawbot", {}).get("base_url", ""),
                },
            },
        }
        return result
    except Exception as e:
        logger.error(f"获取Channel配置失败: {e}", exc_info=True)
        return {"error": str(e)}


def _load_overlay() -> dict:
    """读取 data/peng.json 覆盖层原始内容（不合并基础层，保留用户定制字段）"""
    from peng.common.paths import get_project_root

    data_path = get_project_root() / "data" / "peng.json"
    if data_path.exists():
        try:
            with open(data_path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception as e:
            logger.warning(f"读取 data/peng.json 覆盖层失败: {e}")
    return {}


@router.post("/config/feishu")
async def save_feishu_config(request: Request) -> dict[str, Any]:
    """保存飞书配置并连接（写入 data/peng.json 覆盖层）"""
    try:
        body = await request.json()

        # 读取合并后的配置（config 基础 + data 覆盖层），用于保护已有字段
        old_feishu = _load_config().get("channels", {}).get("feishu", {})

        feishu_config = {
            "enabled": body.get("enabled", True),
            "app_id": body.get("app_id", ""),
            "app_secret": body.get("app_secret", ""),
            "verification_token": old_feishu.get("verification_token", ""),
            "encrypt_key": body.get("encrypt_key", ""),
            "bot_name": body.get("bot_name", "鹏🐣"),
        }

        # app_secret 脱敏值保护：如果前端返回的是脱敏值，保留原值
        new_secret = feishu_config["app_secret"]
        if new_secret.startswith("***") and new_secret.endswith("***"):
            feishu_config["app_secret"] = old_feishu.get("app_secret", "")

        # 只更新 data/peng.json 覆盖层的 channels 节点（保留其他顶层字段）
        overlay = _load_overlay()
        overlay.setdefault("channels", {})
        overlay["channels"]["enabled"] = True
        overlay["channels"]["feishu"] = feishu_config
        _save_config(overlay)

        # 重新初始化通道
        _reinit_channels()

        return {"success": True, "message": "飞书配置已保存"}
    except Exception as e:
        logger.error(f"保存飞书配置失败: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.post("/config/wechat")
async def save_wechat_config(request: Request) -> dict[str, Any]:
    """保存微信配置并连接（写入 data/peng.json 覆盖层）"""
    try:
        body = await request.json()

        # 只更新 data/peng.json 覆盖层的 channels 节点（保留其他顶层字段）
        overlay = _load_overlay()
        overlay.setdefault("channels", {})
        overlay["channels"]["enabled"] = True
        overlay["channels"]["wechat"] = {
            "enabled": body.get("enabled", True),
            "clawbot": body.get("clawbot", {}),
        }
        _save_config(overlay)

        # 重新初始化通道
        _reinit_channels()

        return {"success": True, "message": "微信配置已保存"}
    except Exception as e:
        logger.error(f"保存微信配置失败: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.post("/wechat/qrcode")
async def get_wechat_qrcode() -> dict[str, Any]:
    """获取微信登录二维码"""
    if not _channel_manager:
        return {"error": "Channel manager not initialized"}

    from peng.channels.base import ChannelType
    wechat_channel = _channel_manager.get_channel(ChannelType.WECHAT)
    if not wechat_channel:
        return {"error": "Wechat channel not registered"}

    try:
        qr_url = await wechat_channel.get_qr_code()
        if qr_url:
            return {"success": True, "qr_url": qr_url}
        else:
            return {"success": False, "error": "获取二维码失败"}
    except Exception as e:
        logger.error(f"获取微信二维码失败: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def _reinit_channels() -> None:
    """重新初始化通道系统（配置保存后调用）"""
    global _channel_manager
    if _channel_manager is None:
        return

    import asyncio
    from peng.channels.integration import init_channel_system

    try:
        # 读取合并后的配置（config 基础 + data/peng.json 覆盖层）
        channels_config = _load_config().get("channels", {})

        # 先停止所有通道
        loop = None
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 如果在异步环境中，创建task
                asyncio.create_task(_channel_manager.stop_all())
            else:
                loop.run_until_complete(_channel_manager.stop_all())
        except RuntimeError:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_channel_manager.stop_all())

        # 重新初始化
        _channel_manager = init_channel_system(channels_config)

        # 启动所有通道
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(_channel_manager.start_all())
            else:
                loop.run_until_complete(_channel_manager.start_all())
        except RuntimeError:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_channel_manager.start_all())

        logger.info("✅ Channel系统已重新初始化")
    except Exception as e:
        logger.error(f"重新初始化Channel系统失败: {e}", exc_info=True)


def _load_config() -> dict[str, Any]:
    """加载 peng.json 配置（合并 config 基础 + data 覆盖层）"""
    from peng.config.loader import _load_config_with_overlay
    from peng.common.paths import get_project_root

    project_root = get_project_root()
    return _load_config_with_overlay(
        project_root / "config" / "peng.json",
        project_root / "data" / "peng.json",
    )


def _save_config(config: dict[str, Any]) -> None:
    """保存 peng.json 配置到 data 目录覆盖层"""
    from pathlib import Path
    from peng.config import clear_config_cache
    from peng.common.paths import get_project_root, ensure_dir_with_owner

    project_root = get_project_root()
    data_path = project_root / "data" / "peng.json"
    ensure_dir_with_owner(data_path.parent)
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    clear_config_cache(data_path)


@router.get("/clawbot/status")
async def clawbot_status() -> dict[str, Any]:
    """查询 ClawBot 微信通道状态"""
    if not _channel_manager:
        return {"error": "Channel manager not initialized"}

    from peng.channels.base import ChannelType
    wechat_channel = _channel_manager.get_channel(ChannelType.WECHAT)
    if not wechat_channel:
        return {"error": "Wechat channel not registered"}

    from peng.channels.clawbot_wechat import ClawBotWechatChannel
    if not isinstance(wechat_channel, ClawBotWechatChannel):
        return {"error": "Wechat channel is not in ClawBot mode"}

    return wechat_channel.get_status_info()


@router.get("/clawbot/qrcode")
async def clawbot_qrcode() -> dict[str, Any]:
    """获取 ClawBot 扫码登录二维码
    
    iLink API 用 POST 请求（参考 OpenClaw 源码 login-qr.ts fetchQRCode）
    """
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            # iLink 需要特定的 headers
            ilink_headers = {
                "Content-Type": "application/json",
                "iLink-App-Id": "bot",
                "iLink-App-ClientVersion": "65547",
            }
            resp = await client.post(
                "https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode?bot_type=3",
                content='{"local_token_list":[]}',
                headers=ilink_headers,
            )
            data = resp.json()
            if data.get("ret") != 0:
                return {"success": False, "error": f"iLink API 错误: {data.get('ret_msg', data.get('ret'))}"}
            qr_code = data.get("qrcode", "")
            qr_url = data.get("qrcode_img_content", "")
            if not qr_url:
                return {"success": False, "error": "二维码内容为空"}
            return {"success": True, "qr_url": qr_url, "uuid": qr_code}
    except Exception as e:
        logger.error(f"获取ClawBot二维码失败: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.get("/clawbot/poll-login")
async def clawbot_poll_login(uuid: str = "", redirect_host: str = "") -> dict[str, Any]:
    """轮询 ClawBot 扫码登录状态

    参考 OpenClaw 源码 login-qr.ts pollQRStatus:
    - iLink get_qrcode_status 是长轮询（~35秒）
    - 客户端超时后返回 wait 继续轮询
    - scaned_but_redirect 时服务端返回 redirect_host，后续轮询必须切换到新 host
    - 前端需将 redirect_host 回传（由本接口返回给前端）

    IDC 重定向机制（核心！手机扫码成功但服务端超时的根因）：
    当用户扫码后，iLink 可能返回 scaned_but_redirect + redirect_host，
    表示该用户所在 IDC 不同，需要切换到新 host 继续轮询。
    如果不切换 host，get_qrcode_status 永远不会返回 confirmed。
    """
    if not uuid:
        return {"status": "error", "error": "缺少 uuid 参数"}
    try:
        import httpx
        ilink_headers = {
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": "132097",  # 2.4.1 -> (2<<16)|(4<<8)|1
        }
        # 根据 redirect_host 决定轮询 base URL（参考 OpenClaw login-qr.ts activeLogin.currentApiBaseUrl）
        base_url = f"https://{redirect_host}" if redirect_host else "https://ilinkai.weixin.qq.com"
        poll_url = f"{base_url}/ilink/bot/get_qrcode_status?qrcode={uuid}"

        async with httpx.AsyncClient(timeout=httpx.Timeout(40.0)) as client:
            resp = await client.get(poll_url, headers=ilink_headers)
            data = resp.json()
            status_val = data.get("status", "wait")
            if status_val == "confirmed":
                bot_token = data.get("bot_token", "")
                base_url_val = data.get("baseurl", "")
                ilink_bot_id = data.get("ilink_bot_id", "")
                ilink_user_id = data.get("ilink_user_id", "")
                if bot_token and base_url_val:
                    # 只更新 data/peng.json 覆盖层的 channels.wechat 节点
                    # （保留覆盖层其他顶层字段；同时启用微信通道）
                    overlay = _load_overlay()
                    wechat = overlay.setdefault("channels", {}).setdefault("wechat", {})
                    wechat["enabled"] = True
                    clawbot = wechat.setdefault("clawbot", {})
                    clawbot["bot_token"] = bot_token
                    clawbot["base_url"] = base_url_val
                    clawbot["ilink_bot_id"] = ilink_bot_id
                    clawbot["ilink_user_id"] = ilink_user_id
                    _save_config(overlay)
                    _reinit_channels()
                return {"status": "confirmed", "message": "登录成功", "base_url": base_url_val}
            elif status_val == "scaned":
                return {"status": "scanned", "message": "已扫码，等待确认"}
            elif status_val == "scaned_but_redirect":
                # 关键！IDC 重定向：切换轮询 host（参考 OpenClaw login-qr.ts 第391-401行）
                new_redirect_host = data.get("redirect_host", "")
                logger.info(f"ClawBot IDC 重定向: {redirect_host or 'ilinkai.weixin.qq.com'} -> {new_redirect_host}")
                return {
                    "status": "scanned",
                    "message": "已扫码，IDC 重定向中",
                    "redirect_host": new_redirect_host,
                }
            elif status_val == "binded_redirect":
                return {"status": "scanned", "message": "已绑定过，重定向中"}
            elif status_val == "expired":
                return {"status": "timeout", "message": "二维码已过期"}
            elif status_val == "wait":
                return {"status": "waiting", "message": "等待扫码"}
            else:
                return {"status": "waiting", "message": f"状态: {status_val}", "raw": data}
    except httpx.TimeoutException:
        return {"status": "waiting", "message": "等待扫码..."}
    except Exception as e:
        logger.error(f"轮询ClawBot登录状态失败: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}

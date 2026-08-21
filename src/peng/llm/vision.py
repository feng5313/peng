"""
Vision 大模型调用模块

为 VisionTool 提供专用的大模型异步调用流程。
从 config.llm.providers 中选取支持 vision 且优先级最高的 provider，
构建包含图片的 OpenAI 格式消息，异步调用大模型 API 并返回文本结果。
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import traceback
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from peng.common import jsonp as json
from peng.config import load_config

logger = logging.getLogger(__name__)

# 默认缩小目标：max(长, 宽) <= 768
DEFAULT_VISION_MAX_SIZE = 768


def _get_image_mime_type(image_path: str) -> str:
    """根据文件扩展名返回 MIME 类型"""
    mime_type, _ = mimetypes.guess_type(image_path)
    if mime_type and mime_type.startswith("image/"):
        return mime_type
    # fallback
    ext = Path(image_path).suffix.lower()
    mime_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }
    return mime_map.get(ext, "image/jpeg")


def _normalize_path(image_path: str) -> str:
    """
    规范化图片路径，兼容 Linux 和 Windows 平台。

    处理各种路径格式：
    - Windows 反斜杠: d:\\downloads\\test.jpg -> d:/downloads/test.jpg
    - Windows 正斜杠: d:/downloads/test.jpg -> d:/downloads/test.jpg
    - Linux 路径: /home/user/test.jpg -> /home/user/test.jpg
    - 混合路径: d:/downloads\\test.jpg -> d:/downloads/test.jpg

    Args:
        image_path: 原始路径字符串

    Returns:
        规范化后的路径字符串（正斜杠格式）
    """
    # 处理转义的反斜杠（JSON传输中可能出现）
    # d:\\downloads\\test.jpg -> d:\downloads\test.jpg
    normalized = image_path.replace("\\\\", "\\")

    # 统一转换为正斜杠，兼容跨平台
    # d:\downloads\test.jpg -> d:/downloads/test.jpg
    normalized = normalized.replace("\\", "/")

    # 使用 Path 进行规范化（处理 . 和 .. 等）
    path = Path(normalized)

    # 如果是绝对路径，直接使用
    if path.is_absolute():
        return path.as_posix()

    # 相对路径，转换为正斜杠
    return path.as_posix()


def _load_and_resize_image(
    image_path: str,
    scale: float | None = None,
    max_size: int = DEFAULT_VISION_MAX_SIZE,
) -> tuple[str, str, float]:
    """
    加载图片并按比例缩小，返回 (base64编码, mime_type, 实际缩放比例)

    Args:
        image_path: 图片文件路径（支持正斜杠和反斜杠，自动规范化）
        scale: 缩放比例（0-1），None 时自动计算使 max(长,宽) <= max_size
        max_size: 默认缩小后的最大边长

    Returns:
        (base64_string, mime_type, actual_scale)

    Raises:
        FileNotFoundError: 图片文件不存在
        ValueError: 图片格式不支持或缩放比例无效
    """
    # 路径预处理：统一转换为正斜杠格式，兼容跨平台
    normalized_path = _normalize_path(image_path)
    path = Path(normalized_path)

    if not path.exists():
        # 提供详细的错误信息，包括原始路径和规范化路径
        raise FileNotFoundError(
            f"图片文件不存在: {image_path} (规范化后: {normalized_path})"
        )

    mime_type = _get_image_mime_type(image_path)

    img = Image.open(path)
    orig_w, orig_h = img.size

    # 计算 actual_scale
    if scale is not None:
        if scale <= 0 or scale > 1:
            raise ValueError(f"缩放比例必须在 (0, 1] 范围内，当前值: {scale}")
        actual_scale = scale
    else:
        max_dim = max(orig_w, orig_h)
        if max_dim <= max_size:
            actual_scale = 1.0
        else:
            actual_scale = max_size / max_dim

    # 缩小图片
    new_w = max(1, int(orig_w * actual_scale))
    new_h = max(1, int(orig_h * actual_scale))

    if actual_scale < 1.0:
        # 使用 LANCZOS 高质量缩放
        img = img.resize((new_w, new_h), Image.LANCZOS)

    # 转换为 base64
    buffer = BytesIO()
    # 根据原始格式保存，统一转为 JPEG 以减小体积（除非有透明通道）
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGB")

    img.save(buffer, format="JPEG", quality=85)
    b64_str = base64.b64encode(buffer.getvalue()).decode("utf-8")

    # 更新 mime_type 为 JPEG（因为统一转成了 JPEG）
    actual_mime = "image/jpeg"

    return b64_str, actual_mime, actual_scale


def _build_vision_message(
    prompt: str,
    image_base64: str,
    mime_type: str,
) -> dict[str, Any]:
    """
    构建 OpenAI 格式的 vision 消息

    Args:
        prompt: 提示词文本
        image_base64: 图片的 base64 编码
        mime_type: 图片 MIME 类型

    Returns:
        OpenAI 格式的 user 消息 dict
    """
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{image_base64}",
                },
            },
        ],
    }


def _select_vision_provider() -> dict[str, Any] | None:
    """
    从 config.llm.providers 中选取支持 vision 且优先级最高的 provider

    Returns:
        包含 provider 配置的 dict，格式与 LLMConfig.get_provider() 返回值一致
        如果没有支持 vision 的 provider，返回 None
    """
    config = load_config()
    llm_config = config.llm

    # 遍历所有启用的 provider，筛选支持 vision 的
    vision_providers = []
    for name, prov in llm_config.providers.items():
        if prov.enable and prov.is_vision_enabled():
            vision_providers.append((name, prov.priority))

    if not vision_providers:
        logger.warning("[Vision] 没有支持 vision 的 provider")
        return None

    # 按 priority 降序排序
    vision_providers.sort(key=lambda x: x[1], reverse=True)
    best_name = vision_providers[0][0]

    # 直接从 provider 配置构建返回数据
    # 注意：不能使用 get_provider()，因为它会跳过 vision-only 的 provider！
    prov_config = llm_config.providers.get(best_name)
    if not prov_config:
        logger.warning(f"[Vision] provider {best_name} 配置不存在")
        return None

    provider_data = {
        "provider": best_name,
        "api_key": prov_config.api_key,
        "base_url": prov_config.base_url,
        "model": prov_config.model,
        "small_model": prov_config.small_model,
        "thinking_type": prov_config.thinking_type,
    }
    return provider_data


async def call_vision(
    prompt: str,
    image_path: str,
    scale: float | None = None,
    max_size: int = DEFAULT_VISION_MAX_SIZE,
    enable_thinking: bool = False,
) -> tuple[float, str]:
    """
    异步调用 vision 大模型

    Args:
        prompt: 提示词文本
        image_path: 图片文件路径
        scale: 缩放比例（0-1），None 时自动计算使 max(长,宽) == max_size
        max_size: 默认缩小后的最大边长
        enable_thinking: 是否启用思考模式（默认关闭）

    Returns:
        (actual_scale, result_text) 实际缩放比例和大模型返回的文本

    Raises:
        FileNotFoundError: 图片文件不存在
        ValueError: 没有可用的 vision provider 或参数错误
        RuntimeError: 大模型调用失败
    """
    # 1. 选取 vision provider
    provider_data = _select_vision_provider()
    if not provider_data:
        raise ValueError(
            "没有可用的 vision provider，请在 config/peng.json 中配置 vision=true 的 provider"
        )

    api_key = provider_data["api_key"]
    base_url = provider_data["base_url"]
    model = provider_data["model"]

    logger.info(f"[Vision] 使用 provider: {provider_data['provider']}, model: {model}")

    # 2. 加载并缩小图片
    image_b64, mime_type, actual_scale = _load_and_resize_image(
        image_path, scale=scale, max_size=max_size
    )

    logger.info(
        f"[Vision] 图片已加载并缩小: scale={actual_scale:.3f}, base64_size={len(image_b64)} bytes"
    )

    # 3. 构建消息
    user_msg = _build_vision_message(prompt, image_b64, mime_type)
    messages = [user_msg]

    # 4. 异步调用大模型（非流式，因为我们需要完整结果）
    url = f"{base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,  # vision 任务用低温度确保输出稳定
        "max_tokens": 4096,
        "stream": False,
    }

    # 5. 添加思考模式参数（根据 provider 的 thinking_type 配置决定参数格式）
    thinking_type = provider_data.get("thinking_type")
    if thinking_type == "chat_template_kwargs" or thinking_type == "qwen":
        # qwen 模型默认启用思考模式，需要显式控制
        payload["chat_template_kwargs"] = {"enable_thinking": enable_thinking}
    elif enable_thinking:
        # 默认使用 thinking 参数（OpenAI 格式）
        payload["thinking"] = {"type": "enabled"}
    else:
        # 对于默认启用thinking的模型（如glm-4.6v），显式关闭thinking
        # 确保content字段有内容而非仅在reasoning_content中
        payload["thinking"] = {"type": "disabled"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    timeout_config = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0)

    async with httpx.AsyncClient(timeout=timeout_config, trust_env=False) as client:
        try:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()

            result_data = response.json()
            choices = result_data.get("choices", [])
            if not choices:
                raise RuntimeError(
                    f"大模型返回空结果: {json.dumps(result_data, ensure_ascii=False)}"
                )

            content = choices[0].get("message", {}).get("content", "")
            # 兼容处理：部分模型在thinking模式下content为空，实际内容在reasoning_content中
            if not content:
                reasoning_content = choices[0].get("message", {}).get("reasoning_content", "")
                if reasoning_content:
                    content = reasoning_content
                    logger.info(f"[Vision] 使用 reasoning_content，长度: {len(content)}")
            if not content:
                raise RuntimeError(
                    f"大模型返回空内容: {json.dumps(result_data, ensure_ascii=False)}"
                )

            logger.info(f"[Vision] 调用成功，返回内容长度: {len(content)}")
            return actual_scale, content

        except httpx.HTTPStatusError as e:
            error_detail = e.response.text[:500] if e.response else "无响应内容"
            raise RuntimeError(
                f"Vision API 调用失败: HTTP {e.response.status_code}\n"
                f"响应内容: {error_detail}\n"
                f"provider: {provider_data['provider']}, model: {model}"
            ) from e
        except httpx.TimeoutException as e:
            raise RuntimeError(
                f"Vision API 调用超时: {e}\nprovider: {provider_data['provider']}, model: {model}"
            ) from e
        except Exception as e:
            raise RuntimeError(
                f"Vision API 调用异常: {type(e).__name__}: {e}\n"
                f"Call Stack:\n{traceback.format_exc()}\n"
                f"provider: {provider_data['provider']}, model: {model}"
            ) from e

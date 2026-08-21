"""
VisionTool - 图片视觉识别工具

使用大模型的 vision 能力对图片进行分析识别。
参数为图片路径和提示词，可选参数图片缩小比例。
返回 json 文本格式（实际缩放比例，识别结果字符串）。

支持路径格式：
- 本地路径: /home/user/test.jpg, d:/downloads/test.jpg
- Nest 远程路径: ShotTool 返回的远程文件路径（自动通过 nest 客户端获取）
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
import traceback
from typing import Any

from peng.llm.vision import call_vision, _normalize_path
from peng.tools.base import Tool

logger = logging.getLogger(__name__)

# 标记为 nest 远程路径的前缀
NEST_PATH_PREFIX = "nest://"


async def _fetch_nest_image(image_path: str, nest_name: str | None = None) -> str:
    """
    从 nest 端获取图片文件，保存到本地临时目录，返回本地路径。
    
    nest 端的 read 命令遇到图片文件会自动返回 base64 数据（_read_binary）。
    
    Args:
        image_path: nest 端文件路径
        nest_name: 目标 nest 名称（可选）
    
    Returns:
        本地临时文件路径
    """
    # 去掉 nest:// 前缀
    remote_path = image_path
    if remote_path.startswith(NEST_PATH_PREFIX):
        remote_path = remote_path[len(NEST_PATH_PREFIX):]
    
    from peng.nest.client import send_request
    resp = await send_request("read", {"path": remote_path}, nest_name=nest_name)
    
    if resp.error:
        raise FileNotFoundError(f"无法从 nest 端读取文件 {remote_path}: {resp.error}")
    
    # nest read 对图片文件返回 {"type": "binary", "data": "base64...", "mime": "..."}
    resp_data = resp.data
    if isinstance(resp_data, dict) and resp_data.get("type") == "binary":
        b64_data = resp_data.get("data")
        mime = resp_data.get("mime", "image/png")
    else:
        raise FileNotFoundError(f"nest 端文件 {remote_path} 不是二进制图片文件")
    
    if not b64_data:
        raise FileNotFoundError(f"nest 端文件 {remote_path} 返回空数据")
    
    # 保存到本地临时文件
    image_bytes = base64.b64decode(b64_data)
    ext = os.path.splitext(remote_path)[1] or ".png"
    fd, local_path = tempfile.mkstemp(suffix=ext, prefix="nest_vision_")
    try:
        os.write(fd, image_bytes)
    finally:
        os.close(fd)
    
    logger.info(f"[VisionTool] 从 nest 端获取图片: {remote_path} -> {local_path} ({len(image_bytes)} bytes)")
    return local_path


class VisionTool(Tool):
    """图片视觉识别工具"""

    def __init__(self) -> None:
        super().__init__()

    @property
    def name(self) -> str:
        return "vision"

    @property
    def description(self) -> str:
        return (
            "使用大模型的视觉能力分析图片。"
            "参数："
            "image_path（图片路径）"
            'prompt（简明扼要的提示词，可指定输出json格式例如{"名字":string}），注意Vision大模型非OCR专用，认识各种场景、事物，可识别名人的脸！会解题！'
            "scale（可选，缩放比例0-1，默认自动缩小到max(长,宽)=768）。"
            "enable_thinking（可选，是否启用思考模式，默认false。启用后大模型会先进行推理思考再给出答案，适合复杂分析任务，但响应时间更长）。"
            "返回json格式结果，包含实际缩放比例和识别内容。"
            "支持 jpg/jpeg/png/gif/webp/bmp 格式。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "图片文件路径（绝对路径或 nest 远程路径）",
                },
                "prompt": {
                    "type": "string",
                    "description": ("提示词，应简单明了，过度表述会适得其反！"),
                },
                "scale": {
                    "type": "number",
                    "description": (
                        "图片缩放比例（0-1），例如0.3表示缩小到30%。"
                        "不传则自动计算使 max(长,宽)<=768"
                    ),
                },
                "enable_thinking": {
                    "type": "boolean",
                    "description": (
                        "是否启用思考模式（默认false）。"
                        "启用后大模型会先进行推理思考再给出答案，适合复杂数学题、逻辑推理等需要深度分析的任务，"
                        "但响应时间会更长。"
                    ),
                },
            },
            "required": ["image_path", "prompt"],
        }

    async def execute(self, **kwargs: Any) -> str:
        """
        执行图片视觉识别

        Args:
            image_path: 图片文件路径（支持正斜杠/反斜杠，自动规范化）
            prompt: 提示词
            scale: 缩放比例（可选，支持数字或数字字符串）
            enable_thinking: 是否启用思考模式（可选，默认false）

        Returns:
            json 格式字符串: {"actual_scale": 0.5, "result": "识别结果文本"}
        """
        image_path = kwargs.get("image_path", "")
        prompt = kwargs.get("prompt", "")
        scale = kwargs.get("scale", None)
        enable_thinking = kwargs.get("enable_thinking", False)

        # 参数校验
        if not image_path:
            return '{"error": "image_path 参数不能为空"}'
        if not prompt:
            return '{"error": "prompt 参数不能为空"}'

        # 路径预处理：统一转换为正斜杠格式，兼容跨平台
        original_path = image_path
        image_path = _normalize_path(image_path)
        if original_path != image_path:
            logger.info(f"[VisionTool] 路径规范化: {original_path} -> {image_path}")

        # 从 nest 端获取文件（get_context("nest") 永远有值：local 或远程 nest）
        local_temp_file = None
        nest_name = self.get_context("nest")
        if nest_name:
            try:
                image_path = await _fetch_nest_image(original_path, nest_name)
                local_temp_file = image_path
                logger.info(f"[VisionTool] 从 nest '{nest_name}' 获取: {original_path} -> {image_path}")
            except Exception as e:
                error_msg = str(e).replace('"', '\\"')
                return f'{{"error": "从 nest 端获取图片失败: {error_msg}"}}'

        # scale 类型检查：支持数字或数字字符串
        if scale is not None:
            try:
                if isinstance(scale, str):
                    scale = scale.strip()
                    if not scale:
                        scale = None
                    else:
                        scale = float(scale)
                else:
                    scale = float(scale)

                if scale is not None and (scale <= 0 or scale > 1):
                    return f'{{"error": "scale 必须在 (0, 1] 范围内，当前值: {scale}"}}'
            except (ValueError, TypeError) as e:
                return f'{{"error": "scale 参数格式错误: {e}"}}'

        try:
            actual_scale, result_text = await call_vision(
                prompt=prompt,
                image_path=image_path,
                scale=scale,
                enable_thinking=bool(enable_thinking),
            )

            # 返回 json 格式
            import json

            return json.dumps(
                {
                    "actual_scale": round(actual_scale, 4),
                    "result": result_text,
                },
                ensure_ascii=False,
            )

        except FileNotFoundError as e:
            logger.error(
                f"[VisionTool] 图片文件不存在: {e}\n原始路径: {original_path}\n规范化路径: {image_path}"
            )
            error_msg = str(e).replace('"', '\\"')
            return f'{{"error": "图片文件不存在: {error_msg}"}}'
        except ValueError as e:
            logger.error(
                f"[VisionTool] 参数错误: {e}\n原始路径: {original_path}\n规范化路径: {image_path}"
            )
            error_msg = str(e).replace('"', '\\"')
            return f'{{"error": "{error_msg}"}}'
        except RuntimeError as e:
            logger.error(
                f"[VisionTool] 大模型调用失败: {e}\n原始路径: {original_path}\n规范化路径: {image_path}\n{traceback.format_exc()}"
            )
            error_msg = str(e).replace('"', '\\"')
            return f'{{"error": "大模型调用失败: {error_msg}"}}'
        except Exception as e:
            logger.error(
                f"[VisionTool] 未知错误: {type(e).__name__}: {e}\n原始路径: {original_path}\n规范化路径: {image_path}\n{traceback.format_exc()}"
            )
            error_msg = f"{type(e).__name__}: {e}".replace('"', '\\"')
            return f'{{"error": "未知错误: {error_msg}"}}'
        finally:
            # 清理从 nest 端下载的临时文件
            if local_temp_file and os.path.exists(local_temp_file):
                try:
                    os.unlink(local_temp_file)
                except Exception:
                    pass

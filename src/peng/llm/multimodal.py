"""
多模态消息支持模块

当 provider 本身具备多模态能力（vision=true/yes/on/enabled，如 s-qwen3.8/qwen3-vl）
时，直接在 messages/history 中传递多模态文件内容，让大模型自身处理视觉理解，
而不是通过 vision 工具间接调用。

文件传递方式：
1. webui 附件：消息 content 尾部带 "[附件]
name: path" 块
2. 消息/info 中包含的 "@file=<path>"，path 为事务所在 nest 的 workdir
   相对路径或绝对路径

根据文件扩展名自动识别类型：
- 图片（jpg/jpeg/png/gif/webp/bmp/tiff/avif/ico 等）→ image_url
- 视频（mp4/mov/mkv/avi/webm 等）→ video_url（qwen-vl 格式）
- 音频（mp3/wav/m4a/amr/aac/flac/ogg 等）→ input_audio（qwen 格式）
- PDF → file（data URL）

参考阿里云百炼 qwen-vl OpenAI 兼容格式：
- image: {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}
- video: {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,..."}}
- audio: {"type": "input_audio", "input_audio": {"data": "<b64>", "format": "mp3"}}
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 支持的多模态文件扩展名 → 类型分类
IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif",
     ".avif", ".ico", ".heic", ".heif"}
)
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".wmv"})
AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".m4a", ".amr", ".aac", ".flac", ".ogg", ".opus"})
PDF_EXTENSIONS = frozenset({".pdf"})

# 多模态文件最大字节数（base64前），超过则跳过并提示（避免撑爆上下文）
MAX_MULTIMODAL_FILE_SIZE = 20 * 1024 * 1024

# 🔧 事务#1959：非文本格式（音视频/PDF/无法本地解码的图片透传）最小字节数。
# 图片有PIL全量解码校验、PDF有魔数校验，而音频/视频没有本地解码器，
# 只能靠体积下限判断：<1KB 的音视频/PDF必然是空壳/截断/生成未完成的产物
# （最小有效媒体/文档也远超1KB），直接拒绝防止坏附件污染上下文。
# 单一事实源在 peng.tools.file.MIN_BINARY_ATTACHMENT_BYTES。
from peng.tools.file import MIN_BINARY_ATTACHMENT_BYTES as MIN_MEDIA_FILE_BYTES  # noqa: E402

# @file=<path> 提取正则（路径不含空白）
_FILE_REF_PATTERN = re.compile(r"@file=([^\s\n，。,;；)）\]}】\"']+)")
# webui 附件行提取正则："[附件]" 块内的 "name: path" 行
_ATTACHMENT_LINE_PATTERN = re.compile(r"^([^\s:]+):\s*(\S+)\s*$", re.MULTILINE)


def classify_file(path: str) -> str | None:
    """根据文件扩展名返回多模态类型分类

    Returns:
        "image" | "video" | "audio" | "pdf" | None（非多模态文件）
    """
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in PDF_EXTENSIONS:
        return "pdf"
    return None


def guess_mime(path: str) -> str:
    """猜测文件 MIME 类型（补充 mimetypes 不识别的扩展名）"""
    ext = Path(path).suffix.lower()
    mime_map = {
        ".webp": "image/webp",
        ".avif": "image/avif",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".m4v": "video/x-m4v",
        ".amr": "audio/amr",
        ".m4a": "audio/mp4",
        ".opus": "audio/opus",
    }
    if ext in mime_map:
        return mime_map[ext]
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


def extract_file_refs(text: str) -> list[str]:
    """从文本中提取所有文件引用路径

    支持两种语法：
    1. "@file=<path>" 指令（消息/info内容）
    2. webui 附件块 "[附件]
name: path"（只在 [附件] 标记之后提取）
    """
    refs: list[str] = []
    # 1. @file= 指令（按扩展名过滤：非多模态类型的文件引用静默跳过，
    #    避免文档性提及如 "@file=<path>" 产生噪音提示）
    for r in _FILE_REF_PATTERN.findall(text or ""):
        if classify_file(r) is not None:
            refs.append(r)
    # 2. [附件] 块内的 name: path 行
    if "[附件]" in (text or ""):
        tail = text.split("[附件]", 1)[1]
        for name, path in _ATTACHMENT_LINE_PATTERN.findall(tail):
            if path and classify_file(path):
                refs.append(path)
    # 去重保序
    seen = set()
    result = []
    for r in refs:
        if r not in seen:
            seen.add(r)
            result.append(r)
    return result


# Windows 驱动器路径前缀（D:\ 或 D:/）
_WIN_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")


def is_absolute_any(p: str) -> bool:
    """跨平台绝对路径判断（Linux 上也能识别 Windows 驱动器/UNC 路径）

    Path().is_absolute() 是平台相关的：在 Linux 上 "D:/x/y.jpg" 不算绝对路径，
    会导致 Windows 绝对路径被错误拼接到 Linux workdir 后面。
    """
    return p.startswith("/") or p.startswith("\\\\") or bool(_WIN_DRIVE_PATTERN.match(p))


def resolve_path(raw_path: str, workdir: str | None) -> str:
    """路径规范化（跨平台），相对路径基于 workdir 解析为绝对路径

    兼容 Windows 反斜杠（JSON 传输中的 \\\\ 转义）。
    Windows 驱动器路径（D:\\x、D:/x）和 UNC 路径在任意平台均视为绝对路径。
    """
    normalized = raw_path.replace("\\\\", "\\").replace("\\", "/")
    if not is_absolute_any(normalized) and workdir:
        wd = Path(workdir.replace("\\", "/"))
        normalized = (wd / normalized).as_posix()
    return normalized


async def _read_via_nest(nest_name: str | None, path: str) -> bytes | None:
    """通过 nest 客户端读取文件二进制内容（nest read 对图片/PDF返回 base64 binary）

    Returns:
        文件字节内容；失败返回 None
    """
    try:
        from peng.nest.client import send_request

        resp = await send_request("read", {"path": path}, nest_name=nest_name)
        if resp.error:
            logger.warning(f"[Multimodal] nest 读取文件失败 {path}: {resp.error}")
            return None
        data = resp.data
        if isinstance(data, dict) and data.get("type") == "binary":
            b64 = data.get("data") or ""
            if b64:
                return base64.b64decode(b64)
        logger.warning(f"[Multimodal] nest 读取 {path} 未返回binary数据")
        return None
    except Exception as e:
        logger.warning(f"[Multimodal] nest 读取文件异常 {path}: {e}")
        return None


def _read_local(path: str) -> bytes | None:
    """本地读取文件字节内容"""
    try:
        p = Path(path)
        if not p.is_file():
            return None
        return p.read_bytes()
    except Exception as e:
        logger.warning(f"[Multimodal] 本地读取文件失败 {path}: {e}")
        return None


def build_content_part(
    kind: str, path: str, data: bytes
) -> dict[str, Any] | None:
    """构建 OpenAI/qwen 兼容的多模态 content part

    🔧 事务#1834：图片统一走 normalize_image_bytes 规范化（解码验证 +
    缩放至1M像素 + BMP/TIFF等非白名单格式转PNG）——@file=引用/附件路径
    此前原样 base64 透传，BMP 会被不支持该格式的 provider（GLM）以
    400"图片输入格式/解析错误"拒绝（事务#1822反复报错根因之一）。
    规范化失败（PIL缺插件/数据异常）时降级保留原始数据交provider判断。

    🔧 事务#1959：内容有效性硬校验——空数据（0字节文件）直接返回 None
    （不构建附件part）；PIL 内建格式（png/jpeg/gif/webp/bmp/tiff/ico，
    见 PIL_BUILTIN_IMAGE_MIMES）规范化失败（UnidentifiedImageError）必然
    是空/损坏/非图片数据，同样返回 None 拒绝。仅 heic/avif 等依赖可选
    插件的格式保留原始数据透传（provider端可能支持）。空 data URL 附件
    一旦进入上下文，GLM 报 400 "file必须传入file_id、file_url、
    file_data至少之一" 且无法自愈（事务#1959只能人工回滚救出）。

    Args:
        kind: classify_file 返回的类型（image/video/audio/pdf）
        path: 文件路径（用于 mime 判断）
        data: 文件字节内容

    Returns:
        content part dict；空数据/内建格式损坏/音视频PDF过小/PDF魔数非法返回 None
    """
    # 🔧 事务#1959：空数据（0字节文件）不是有效附件，任何类型一律拒绝
    if not data:
        logger.warning(f"[Multimodal] 文件为空(0字节)，拒绝构建附件part: {path}")
        return None

    mime = guess_mime(path)

    if kind == "image":
        try:
            from peng.tools.file import normalize_image_bytes

            data, mime, _note = normalize_image_bytes(data, mime)
        except Exception as e:
            from peng.tools.file import PIL_BUILTIN_IMAGE_MIMES

            if mime.lower() in PIL_BUILTIN_IMAGE_MIMES:
                # 内建格式无法识别 = 空数据/损坏/非图片，拒绝（0字节png漏洞）
                logger.warning(
                    f"[Multimodal] 图片数据无效(内建格式{mime}无法识别)，拒绝构建附件part: {path}: {e}"
                )
                return None
            # 降级：无法本地解码验证的非内建格式（heic/avif等缺插件），
            # 至少执行非文本格式体积下限（<1KB必然是空壳/截断/未完成）
            if len(data) < MIN_MEDIA_FILE_BYTES:
                logger.warning(
                    f"[Multimodal] 图片文件过小({len(data)}字节<1KB，无法本地验证且疑损坏)，"
                    f"拒绝构建附件part: {path}"
                )
                return None
            logger.warning(f"[Multimodal] 图片规范化失败，保留原始数据 {path}: {e}")
        b64 = base64.b64encode(data).decode("utf-8")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        }

    # 🔧 事务#1959：音频/视频等本地无解码器的非文本格式——体积下限硬校验
    # （<1KB必然是空壳/截断/生成未完成，provider无法处理且可能400）
    if kind in ("video", "audio") and len(data) < MIN_MEDIA_FILE_BYTES:
        logger.warning(
            f"[Multimodal] {kind}文件过小({len(data)}字节<1KB，疑损坏/未完成)，"
            f"拒绝构建附件part: {path}"
        )
        return None

    b64 = base64.b64encode(data).decode("utf-8")
    if kind == "video":
        return {
            "type": "video_url",
            "video_url": {"url": f"data:{mime};base64,{b64}"},
        }
    if kind == "audio":
        # qwen input_audio 格式：data 为裸 base64，format 为音频格式名
        fmt = (Path(path).suffix.lower() or "").lstrip(".") or "mp3"
        return {
            "type": "input_audio",
            "input_audio": {"data": b64, "format": fmt},
        }
    if kind == "pdf":
        # 🔧 事务#1959：PDF 体积下限 + 魔数双重校验（非空已由顶部统一校验）
        if len(data) < MIN_MEDIA_FILE_BYTES:
            logger.warning(
                f"[Multimodal] PDF文件过小({len(data)}字节<1KB，疑损坏/未完成)，"
                f"拒绝构建附件part: {path}"
            )
            return None
        if not data.startswith(b"%PDF"):
            logger.warning(f"[Multimodal] PDF魔数校验失败，拒绝构建附件part: {path}")
            return None
        return {
            "type": "file",
            "file": {"url": f"data:{mime};base64,{b64}"},
        }
    return None


async def load_multimodal_parts(
    raw_path: str,
    workdir: str | None = None,
    nest_name: str | None = None,
) -> tuple[str, dict[str, Any] | None, str]:
    """加载文件并构建多模态 content part

    读取顺序：若 nest_name 存在先尝试 nest 远程读取（含local nest），
    失败则本地读取。

    Returns:
        (绝对路径, content_part 或 None, 状态说明)
        状态: "ok" | "not_found" | "empty" | "invalid" | "unsupported"
              | "too_large" | "error"
        （🔧 事务#1959：empty=0字节文件；invalid=数据损坏/过小/格式校验失败）
    """
    abs_path = resolve_path(raw_path, workdir)
    kind = classify_file(abs_path)
    if kind is None:
        return abs_path, None, "unsupported"

    # 大小预检（仅本地能预检；nest端大小未知，读回后再校验）
    local = Path(abs_path)
    if local.is_file() and local.stat().st_size > MAX_MULTIMODAL_FILE_SIZE:
        return abs_path, None, "too_large"

    data = None
    if nest_name:
        data = await _read_via_nest(nest_name, abs_path)
    if data is None:
        data = _read_local(abs_path)
    if data is None:
        return abs_path, None, "not_found"
    # 🔧 事务#1959：0字节文件单独反馈（生成/截图未完成的典型产物）
    if not data:
        logger.warning(f"[Multimodal] 文件为空(0字节): {abs_path}")
        return abs_path, None, "empty"
    if len(data) > MAX_MULTIMODAL_FILE_SIZE:
        return abs_path, None, "too_large"

    part = build_content_part(kind, abs_path, data)
    if part is None:
        # kind 有效但构建失败 = 数据损坏/过小/校验不通过（非"不支持类型"）
        return abs_path, None, "invalid"
    return abs_path, part, "ok"


# 多模态附件 part 类型集合（OpenAI/qwen 兼容格式的多模态 content part；
# 与 tool_engine.MessageHistory._ATTACHMENT_PART_TYPES 保持一致）
ATTACHMENT_PART_TYPES = frozenset(
    {
        "image_url",
        "video_url",
        "input_audio",
        "input_image",
        "input_video",
        "input_file",
        "file",
        "audio",
    }
)


def has_attachment_part(content: Any) -> bool:
    """content（list 多模态格式）中是否包含附件 part（图片/视频/音频/PDF等）"""
    if not isinstance(content, list):
        return False
    return any(
        isinstance(p, dict) and p.get("type") in ATTACHMENT_PART_TYPES for p in content
    )


def messages_have_attachment_parts(messages: list[dict] | None) -> bool:
    """消息列表（OpenAI格式）中是否存在待发送的多模态附件part"""
    if not messages:
        return False
    for m in messages:
        if isinstance(m, dict) and has_attachment_part(m.get("content")):
            return True
    return False


def strip_multimodal_content(content: Any) -> Any:
    """将多模态 content 降级为纯文本（用于不支持vision的provider）

    list content 时提取所有 text part 拼接，并保留附件路径提示；
    str content 原样返回。
    """
    if not isinstance(content, list):
        return content
    texts = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            texts.append(part.get("text", ""))
    return "\n".join(t for t in texts if t)


# 附件降级为文本时的默认提示（当前provider不支持多模态）
ATTACHMENT_DEMOTED_NOTICE = (
    "[系统提示：本消息原附带的多模态附件（图片/视频/音频/PDF）未能发送："
    "当前大模型provider不支持多模态输入。如需查看附件内容，"
    "请切换到支持vision的provider后重新调用read工具读取原文件。]"
)


def demote_messages_attachments(
    messages: list[dict], notice: str = ATTACHMENT_DEMOTED_NOTICE
) -> list[dict]:
    """将消息列表中所有附件part降级为文本提示（事务#1819）

    发送前闭环保护：选定provider不支持多模态时，把消息中的附件part
    替换为说明文本，防止附件发给不支持vision的provider导致HTTP 400
    （content.type参数非法）并误冷却该provider（事务#1818死循环诱因之一）。

    不修改原列表：含附件的消息构建浅拷贝替换，其余消息原样复用。

    Args:
        messages: OpenAI 格式消息列表
        notice: 降级说明文本

    Returns:
        降级后的消息列表（无附件时返回原列表）
    """
    if not messages:
        return messages
    result: list[dict] = []
    changed = False
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if not has_attachment_part(content):
            result.append(m)
            continue
        texts = [
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        text = "\n".join(t for t in texts if t)
        new_content = (text + "\n" if text else "") + notice
        new_msg = dict(m)
        new_msg["content"] = new_content
        result.append(new_msg)
        changed = True
    return result if changed else messages


# 🔧 事务#1959：附件数据无效（空payload）时替换part的文本提示
INVALID_ATTACHMENT_NOTICE = (
    "[系统提示：原此处的多模态附件（图片/视频/音频/PDF）数据为空(0字节)或无效，"
    "已自动移除以防API请求失败。该文件可能生成/截图未完成或已损坏，"
    "如需其内容请重新生成文件后重新读取。]"
)


def _part_payload_invalid(part: dict[str, Any]) -> bool:
    """判断单个附件 content part 的 payload 是否缺失/为空（事务#1959）

    覆盖 OpenAI/qwen/GLM 等各家的附件 part 形态：
    - image_url/video_url: {"url": "data:...;base64,<b64>" 或 http(s) URL}
    - input_audio: {"data": "<裸b64>", "format": ...}
    - input_image/input_video: {"image_url"/"video_url": ...}（嵌套或平铺）
    - input_file: {"file_url"/"file_data"/"url"/"data": ...}
    - file: {"url"/"file_url"/"file_data": ...} 或平铺 url
    - audio: {"data"/"url": ...}

    判定规则：payload 字符串缺失/空白 → 无效；data URL 的 base64 段为空
    或解码后0字节 → 无效；非法 base64 → 无效。http(s) URL 非空即有效。
    """
    ptype = part.get("type")

    # 已知的 payload 字段名（按优先级）：只认这些键，防止把 format/mime 等
    # 元数据字段（如 input_audio.format='mp3'）误判为有效payload
    _PAYLOAD_KEYS = (
        "url",
        "data",
        "file_url",
        "file_data",
        "b64",
        "b64_json",
        "image_url",
        "video_url",
    )

    def _payload(*candidates: Any) -> str:
        for c in candidates:
            if isinstance(c, str) and c.strip():
                return c.strip()
            if isinstance(c, dict):
                for k in _PAYLOAD_KEYS:
                    v = c.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
                    if isinstance(v, dict):
                        # 再嵌套一层（image_url: {url: ...} 等形态）
                        v2 = v.get("url") or v.get("data")
                        if isinstance(v2, str) and v2.strip():
                            return v2.strip()
        return ""

    if ptype == "image_url":
        payload = _payload(part.get("image_url"), part.get("url"))
    elif ptype == "video_url":
        payload = _payload(part.get("video_url"), part.get("url"))
    elif ptype == "input_audio":
        payload = _payload(part.get("input_audio"), part.get("audio"))
    elif ptype == "input_image":
        payload = _payload(part.get("image_url"), part.get("input_image"))
    elif ptype == "input_video":
        payload = _payload(part.get("video_url"), part.get("input_video"))
    elif ptype == "input_file":
        payload = _payload(part.get("input_file"), part.get("file_url"), part.get("file_data"))
    elif ptype == "file":
        f = part.get("file")
        payload = _payload(f, part.get("file_url"), part.get("file_data"), part.get("url"))
    elif ptype == "audio":
        payload = _payload(part.get("audio"), part.get("data"), part.get("url"))
    else:
        return False

    if not payload:
        return True
    if payload.startswith("data:"):
        if "base64," not in payload:
            return True  # 非base64的data URL（如data:,）无有效载荷
        b64 = payload.split("base64,", 1)[1].strip()
        if not b64:
            return True
        try:
            return len(base64.b64decode(b64)) == 0
        except Exception:
            return True
    return False


def sanitize_messages_attachments(
    messages: list[dict], notice: str = INVALID_ATTACHMENT_NOTICE
) -> tuple[list[dict], int]:
    """清除消息列表中数据为空的附件part，替换为文本提示（事务#1959）

    发送前最后防线：空payload附件（0字节文件透传、历史中毒上下文、
    旧版本入库的坏附件等）一旦发给 provider 必然 400
    （GLM: "file必须传入file_id、file_url、file_data至少之一"）且
    附件无法自动清除，导致事务无限重试只能人工回滚。本函数在发送前
    把这类 part 替换为说明文本，保证请求永不携带空数据附件。

    不修改原列表：含无效附件的消息构建浅拷贝替换，其余消息原样复用。

    Args:
        messages: OpenAI 格式消息列表
        notice: 替换无效附件part的文本提示

    Returns:
        (净化后的消息列表, 移除的无效附件part数；无移除时返回原列表)
    """
    if not messages:
        return messages, 0
    result: list[dict] = []
    removed = 0
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if not has_attachment_part(content):
            result.append(m)
            continue
        new_parts: list[dict] = []
        msg_removed = False
        for p in content:
            if isinstance(p, dict) and p.get("type") in ATTACHMENT_PART_TYPES:
                if _part_payload_invalid(p):
                    removed += 1
                    msg_removed = True
                    continue
            new_parts.append(p)
        if not msg_removed:
            result.append(m)
            continue
        new_msg = dict(m)
        new_msg["content"] = new_parts + [{"type": "text", "text": notice}]
        result.append(new_msg)
    return (result if removed else messages), removed

#!/usr/bin/env python3
"""事务#2119：provider 读不出附件图片（400）——不切换provider、错误返回给大模型

事故链路（事务#2081实际场景）：
  截图脚本把 BMP 数据存成 .png（w_R1.png 魔数 'BM'，520x688）
  → read 工具按**扩展名**推导 mime=image/png，且该 mime 在 provider 白名单内
  → normalize_image_bytes 原样透传 BMP 字节，构造出
     `data:image/png;base64,<BMP字节>`（声明与实际不符）
  → 本地 Strata 引擎按声明解码失败，返回 HTTP 400
     "the image could not be read: cannot read the image C:\\...\\*.img"
  → 该错误不匹配任何内容类关键词 → 按普通 4xx：重试2次 → 切换 provider
     (🐱→🐹→🐥→🐱) → 连续5轮所有 provider 失败 → 等待循环，事务卡死

修复分层（风主指示：这种情况应该返回错误给大模型，而不是一直切换provider）：
  层1 file.py normalize_image_bytes：格式以**实际内容**（PIL识别）为准，
      纠正声明并把非白名单实际格式转码 PNG → 声明与实际永远一致
  层2 tool_engine 关键词：该类错误归入"内容解析失败"（不冷却不切换provider）
  层3 tool_engine 清除附件时用 attachment_rejected_notice 把 provider 报错
      **返回给大模型**（附件从未送达，禁止沿用"已发送"说明诱导臆测图片）
  层4 tool_engine _reencode_history_images：按实际内容判断，修复已中毒历史
      （声明 image/png 实为 BMP 的附件转码救援，保留视觉信息）
"""

import base64
import io
import sys
from pathlib import Path

# 项目根目录加入 sys.path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image

from peng.llm.tool_engine import (
    CONTENT_PARSE_ERROR_KEYWORDS,
    CONTEXT_LENGTH_KEYWORDS,
    MessageHistory,
    ToolExecutionEngine,
)
from peng.tools.file import PIL_FORMAT_TO_MIME, normalize_image_bytes

# 事务#2081 实际报错（原文，含转义后的 Windows 路径）
REAL_ERROR = (
    "HTTP 400 请求参数错误 - the image could not be read: cannot read the image "
    "C:\\Users\\feng\\AppData\\Local\\Temp\\strata-vision-tetj2vq2\\00039f17e0cb9e0e54dac22b61ec77ce.img"
)


# ==================== 测试工具 ====================


def _img_bytes(fmt: str, size=(70, 60), color=(10, 180, 20)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format=fmt)
    return buf.getvalue()


def _data_url(mime: str, data: bytes) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def _sniff(url: str) -> str | None:
    """取出 data URL 的实际图片格式（PIL识别）"""
    _header, _sep, payload = url.partition(";base64,")
    with Image.open(io.BytesIO(base64.b64decode(payload))) as im:
        return PIL_FORMAT_TO_MIME.get((im.format or "").upper())


def _declared(url: str) -> str:
    return url.partition(";base64,")[0].removeprefix("data:").split(";", 1)[0]


class _FakeLLM:
    def __init__(self, error: str = REAL_ERROR):
        self.error = error


def make_engine(stage: int = 0, kind: str = "", error: str = REAL_ERROR):
    """构造跳过 __init__ 的最小引擎实例（只依赖计数器/错误类型/llm.error）"""
    engine = ToolExecutionEngine.__new__(ToolExecutionEngine)
    engine._content_filter_deletions = stage
    engine._content_filter_kind = kind
    engine.llm = _FakeLLM(error)
    return engine


def make_history_with_tool_attachment(url: str) -> MessageHistory:
    """复现事务#2081场景：user任务 → assistant(tool_calls) → tool结果带图片附件"""
    h = MessageHistory()
    h.add_system("你是助手")
    h.add_user("调整状态栏 chip 底边线", merge=False)
    h.add_assistant(
        "",
        tool_calls=[
            {"id": "call1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
        ],
    )
    h.add_tool_result(
        "call1",
        "read",
        [
            {"type": "text", "text": "图片 读取成功"},
            {"type": "image_url", "image_url": {"url": url}},
        ],
        add_timestamp=False,
    )
    return h


def _parts(h: MessageHistory) -> list[dict]:
    out = []
    for m in h._messages:
        c = m.get("content")
        if isinstance(c, list):
            out.extend(p for p in c if isinstance(p, dict))
    return out


# ==================== 层1：格式按实际内容纠正（file.py） ====================


class TestNormalizeByContent:
    """扩展名与实际内容不符时，MIME 必须以内容为准并转码（根治层）"""

    def test_bmp_mislabeled_as_png_is_transcoded(self):
        """BMP 数据存成 .png（事务#2081实测）：纠正声明并转码为真 PNG"""
        bmp = _img_bytes("BMP")
        out, mime, note = normalize_image_bytes(bmp, "image/png")
        assert mime == "image/png", mime
        # 发出的字节必须是真 PNG（否则 provider 按声明解码必然 400）
        assert _sniff(f"data:{mime};base64," + base64.b64encode(out).decode()) == "image/png"
        assert out != bmp
        assert "纠正格式声明" in note and "image/bmp" in note, note
        assert "已转换为PNG通用格式" in note, note

    def test_real_png_passthrough_unchanged(self):
        """真 PNG：原样透传，无纠正/转码说明（避免无谓重编码）"""
        png = _img_bytes("PNG")
        out, mime, note = normalize_image_bytes(png, "image/png")
        assert out == png and mime == "image/png" and note == ""

    def test_real_jpeg_declared_png_is_corrected(self):
        """JPEG 数据被声明为 png：纠正为 image/jpeg 且不转码（属白名单格式）"""
        jpg = _img_bytes("JPEG")
        out, mime, note = normalize_image_bytes(jpg, "image/png")
        assert mime == "image/jpeg", mime
        assert out == jpg, "白名单实际格式无需转码"
        assert "image/png→image/jpeg" in note, note


# ==================== 层2：错误分类（不切换provider） ====================


class TestErrorClassification:
    """事务#2081 的 400 文案必须归入内容解析类，绝不能按普通4xx切换provider"""

    def test_real_error_matches_parse_keywords(self):
        low = REAL_ERROR.lower()
        assert any(kw in low for kw in CONTENT_PARSE_ERROR_KEYWORDS), "未匹配内容解析类关键词"

    def test_real_error_not_context_overflow(self):
        """不得被误判为上下文超标（否则会进入 compact 分支）"""
        low = REAL_ERROR.lower()
        assert not any(kw in low for kw in CONTEXT_LENGTH_KEYWORDS)

    def test_variants_match(self):
        for msg in (
            "the image could not be read",
            "cannot read the image /tmp/x.img",
            "could not read image",
            "failed to read image",
            "unable to read the image",
            "图片无法读取",
            "无法读取图片",
        ):
            assert any(kw in msg.lower() for kw in CONTENT_PARSE_ERROR_KEYWORDS), msg


# ==================== 层3：清附件时把报错返回给大模型 ====================


class TestErrorReturnedToModel:
    """附件被拒时必须把 provider 报错返回给大模型（而非谎称已发送）"""

    def test_rejected_notice_contains_provider_error(self):
        h = make_history_with_tool_attachment(_data_url("image/png", _img_bytes("PNG")))
        engine = make_engine(stage=0, kind="内容解析失败(疑似损坏附件/内容类型不支持)")
        desc = engine._content_filter_next_action(h)
        assert "附件" in desc, desc
        tool_msg = [m for m in h._messages if m["role"] == "tool"][0]
        assert isinstance(tool_msg["content"], str)
        # provider 报错原文必须出现在模型可见的上下文里（错误反馈闭环）
        assert "the image could not be read" in tool_msg["content"]
        assert "尚未看到该附件内容" in tool_msg["content"]
        assert "禁止臆测" in tool_msg["content"]
        # 绝不能出现"已随上一轮请求发送给你"的错误暗示
        assert "已随上一轮请求发送给你" not in tool_msg["content"]
        # 附件确实已被移除
        assert all(p.get("type") != "image_url" for p in _parts(h))

    def test_success_path_notice_unchanged(self):
        """只发送一次机制（成功路径）仍用默认"已发送"说明"""
        h = make_history_with_tool_attachment(_data_url("image/png", _img_bytes("PNG")))
        h.strip_sent_attachments()
        tool_msg = [m for m in h._messages if m["role"] == "tool"][0]
        assert "已随上一轮请求发送给你" in tool_msg["content"]

    def test_content_filter_kind_keeps_default_notice(self):
        """内容审核拦截（附件曾送达）不套用"读不出"文案"""
        h = make_history_with_tool_attachment(_data_url("image/png", _img_bytes("PNG")))
        engine = make_engine(stage=0, kind="内容审核拦截")
        engine._content_filter_next_action(h)
        tool_msg = [m for m in h._messages if m["role"] == "tool"][0]
        assert "已随上一轮请求发送给你" in tool_msg["content"]
        assert "无法被大模型provider" not in tool_msg["content"]


# ==================== 层4：已中毒历史的转码救援 ====================


class TestReencodeRescue:
    """声明 image/png 实为 BMP 的附件：级0按内容转码救援，保留视觉信息"""

    def test_mislabeled_attachment_is_reencoded(self):
        url = _data_url("image/png", _img_bytes("BMP"))
        h = make_history_with_tool_attachment(url)
        engine = make_engine(stage=0, kind="内容解析失败(疑似损坏附件/内容类型不支持)")
        desc = engine._content_filter_next_action(h)
        assert "转码" in desc, desc
        # 附件仍在（视觉信息未丢失），且声明与实际已一致
        img_parts = [p for p in _parts(h) if p.get("type") == "image_url"]
        assert len(img_parts) == 1
        new_url = img_parts[0]["image_url"]["url"]
        assert new_url.startswith("data:image/jpeg;base64,")
        assert _sniff(new_url) == "image/jpeg"

    def test_safe_consistent_attachment_not_reencoded(self):
        """实际为 PNG 且声明 image/png：不转码（不浪费视觉token/重复编码）"""
        url = _data_url("image/png", _img_bytes("PNG"))
        h = make_history_with_tool_attachment(url)
        engine = make_engine(stage=0, kind="内容解析失败(疑似损坏附件/内容类型不支持)")
        desc = engine._content_filter_next_action(h)
        # 无可转附件 → 落入级1清除附件（并带错误反馈文案）
        assert "附件" in desc, desc
        tool_msg = [m for m in h._messages if m["role"] == "tool"][0]
        assert "the image could not be read" in tool_msg["content"]

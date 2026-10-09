#!/usr/bin/env python3
"""事务#1834：多模态图片格式规范化修复的单元测试

背景：事务#1822 反复报"内容解析失败(疑似损坏附件/内容类型不支持)"——
read 工具读取 1400x700 BMP（像素≤1M 原样透传 image/bmp），z-glm5.3-flash
vision API 不支持 BMP，返回 400"图片输入格式/解析错误"，级1清除附件重试
丢失视觉信息后模型再 read 再失败，循环 8 次。

覆盖：
1. file.py：normalize_image_bytes 非白名单格式（BMP/TIFF）转 PNG、
   白名单格式（PNG/JPEG/GIF/WEBP）原样保留、缩放+转码双重、损坏拒绝
2. multimodal.py：build_content_part 图片分支走规范化（@file=/附件路径）
3. tool_engine.py：_reencode_history_images 兜底转码救援（BMP/webp→JPEG、
   损坏跳过、tool消息附件、嵌套/平铺格式）
4. tool_engine.py：_content_filter_next_action 级0转码救援的级别序列
   （救援→级1清附件→级2删user，审核类不走救援）
"""

import base64
import io
import sys
from pathlib import Path

# 项目根目录加入 sys.path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image

from peng.llm.multimodal import build_content_part
from peng.llm.tool_engine import MessageHistory, ToolExecutionEngine
from peng.tools.file import (
    PROVIDER_SAFE_IMAGE_MIMES,
    _build_binary_attachment,
    normalize_image_bytes,
)


# ==================== 测试工具 ====================


def _img_bytes(fmt: str, size=(1400, 700), mode="RGB", color=(100, 150, 200)):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format=fmt)
    return buf.getvalue()


def _data_url(mime: str, data: bytes) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def _bmp_url() -> str:
    return _data_url("image/bmp", _img_bytes("BMP"))


class _FakeHistory:
    """最小 MessageHistory 桩（_content_filter_next_action 依赖面）"""

    def __init__(self, messages):
        self._messages = messages

    def strip_sent_attachments(self, notice=None):
        """简化复刻：附件消息content替换为清除说明文本"""
        n = 0
        for m in self._messages:
            c = m.get("content")
            if not isinstance(c, list):
                continue
            atts = [
                p
                for p in c
                if isinstance(p, dict)
                and p.get("type") in ("image_url", "video_url", "input_audio", "file")
            ]
            if not atts:
                continue
            n += len(atts)
            texts = [
                p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text"
            ]
            m["content"] = "\n".join(t for t in texts if t) + (notice or "[附件已清除]")
        return n

    def attachment_rejected_notice(self, error=""):
        """🔧 事务#2119 新增依赖面：复用真实 MessageHistory 的错误反馈文案"""
        return MessageHistory.attachment_rejected_notice(error)


def _bmp_history() -> _FakeHistory:
    return _FakeHistory(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "图片 读取成功"},
                    {"type": "image_url", "image_url": {"url": _bmp_url()}},
                ],
            },
            {"role": "assistant", "content": "ok"},
        ]
    )


def _make_engine(kind: str = "", deletions: int = 0) -> ToolExecutionEngine:
    engine = object.__new__(ToolExecutionEngine)
    engine._content_filter_kind = kind
    engine._content_filter_deletions = deletions
    engine._CONTENT_FILTER_MIN_MESSAGES = 6
    # 🔧 事务#2119：清附件时的错误反馈文案取自 provider 报错
    engine.llm = type("_L", (), {"error": ""})()
    return engine


_PARSE_KIND = "内容解析失败(疑似损坏附件/内容类型不支持)"


# ==================== 层1：normalize_image_bytes ====================


def test_bmp_to_png_small_image():
    """事故场景：像素≤1M的BMP必须转PNG（此前原样透传导致GLM 400）"""
    raw = _img_bytes("BMP")
    data, mime, note = normalize_image_bytes(raw, "image/bmp")
    assert mime == "image/png"
    assert "PNG" in note and "原图未修改" in note
    img = Image.open(io.BytesIO(data))
    assert img.format == "PNG" and img.size == (1400, 700)
    assert len(data) < len(raw), "截图类BMP转PNG体积应缩小"


def test_whitelist_passthrough():
    """白名单格式（PNG/JPEG）像素未超限时字节级原样保留"""
    for fmt, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg")):
        raw = _img_bytes(fmt, size=(100, 100))
        data, out_mime, note = normalize_image_bytes(raw, mime)
        assert data == raw and out_mime == mime and note == "", (fmt, note)


def test_whitelist_set_contents():
    """白名单=主流provider vision API普遍支持的格式"""
    assert PROVIDER_SAFE_IMAGE_MIMES == {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
    }


def test_tiff_to_png():
    """TIFF非白名单格式同样转PNG"""
    data, mime, note = normalize_image_bytes(_img_bytes("TIFF", size=(80, 60)), "image/tiff")
    assert mime == "image/png" and "PNG" in note


def test_oversized_bmp_downscale_and_convert():
    """超1M像素的非白名单格式：缩放+转码双重处理"""
    raw = _img_bytes("BMP", size=(1400, 1000))  # 1.4M像素
    data, mime, note = normalize_image_bytes(raw, "image/bmp")
    assert mime == "image/png"
    assert "缩放" in note and "PNG" in note
    img = Image.open(io.BytesIO(data))
    assert img.size[0] * img.size[1] <= 1024 * 1024


def test_rgba_tiff_alpha_preserved():
    """带alpha的非白名单格式转PNG须保留透明通道（PNG支持全模式）"""
    raw = _img_bytes("TIFF", size=(64, 64), mode="RGBA", color=(255, 0, 0, 128))
    data, mime, _ = normalize_image_bytes(raw, "image/tiff")
    assert mime == "image/png"
    assert Image.open(io.BytesIO(data)).mode == "RGBA"


def test_corrupt_bmp_still_rejected():
    """损坏BMP（事务#1819行为）：转码前的全量解码验证仍拒绝入库"""
    raw = _img_bytes("BMP")
    out = _build_binary_attachment(
        "x.bmp", base64.b64encode(raw[: len(raw) // 2]).decode(), "image/bmp"
    )
    assert not out.attachments and "失败" in out.summary


def test_build_binary_attachment_bmp_converted():
    """read工具路径：正常BMP附件mime变为image/png，summary带转换说明"""
    out = _build_binary_attachment(
        "ok.bmp", base64.b64encode(_img_bytes("BMP")).decode(), "image/bmp"
    )
    assert out.attachments and len(out.attachments) == 1
    assert out.attachments[0]["mime"] == "image/png"
    assert "读取成功" in out.summary and "PNG" in out.summary
    assert out.attachments[0]["url"].startswith("data:image/png;base64,")


# ==================== 层1：build_content_part ====================


def test_build_content_part_bmp_normalized():
    """@file=/附件路径：BMP数据构建的image_url part须为data:image/png"""
    part = build_content_part("image", "D:/work/temp/dense_fold.bmp", _img_bytes("BMP"))
    assert part["type"] == "image_url"
    url = part["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    img = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert img.format == "PNG" and img.size == (1400, 700)


def test_build_content_part_normalize_failure_fallback():
    """(#1959) 内建格式（bmp）规范化失败=空/损坏数据，拒绝构建（返回None）"""
    part = build_content_part("image", "x.bmp", b"not-an-image")
    assert part is None


def test_build_content_part_plugin_missing_passthrough():
    """(#1959) 缺插件格式（heic）非空≥1KB保留原始数据交provider判断"""
    raw = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 2048
    part = build_content_part("image", "x.heic", raw)
    assert part is not None
    assert part["image_url"]["url"].startswith("data:image/heic;base64,")


# ==================== 层2：_reencode_history_images ====================


def test_reencode_bmp_part_to_jpeg():
    """history中BMP附件转码为JPEG（嵌套image_url格式），保留尺寸"""
    h = _bmp_history()
    engine = _make_engine()
    n = engine._reencode_history_images(h)
    assert n == 1
    url = h._messages[0]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    img = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert img.format == "JPEG" and img.size == (1400, 700) and img.mode == "RGB"


def test_reencode_skips_safe_mimes():
    """jpeg/png附件不重复转码（返回0）"""
    h = _FakeHistory(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "x"},
                    {"type": "image_url", "image_url": {"url": _data_url("image/jpeg", _img_bytes("JPEG", size=(50, 50)))}},
                ],
            }
        ]
    )
    assert _make_engine()._reencode_history_images(h) == 0
    assert h._messages[0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg")


def test_reencode_flat_input_image_webp_white_background():
    """平铺str格式（input_image）+带alpha的webp：白底合成转RGB JPEG"""
    raw = _img_bytes("WEBP", size=(60, 40), mode="RGBA", color=(0, 255, 0, 200))
    h = _FakeHistory(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "x"},
                    {"type": "input_image", "image_url": _data_url("image/webp", raw)},
                ],
            }
        ]
    )
    n = _make_engine()._reencode_history_images(h)
    assert n == 1
    url = h._messages[0]["content"][1]["image_url"]
    assert url.startswith("data:image/jpeg;base64,")
    img = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert img.mode == "RGB" and img.format == "JPEG"


def test_reencode_corrupt_data_skipped():
    """损坏附件转码失败跳过（不计入），留待级1清除"""
    h = _FakeHistory(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "x"},
                    {"type": "image_url", "image_url": {"url": "data:image/bmp;base64,QUJD"}},
                ],
            }
        ]
    )
    assert _make_engine()._reencode_history_images(h) == 0


def test_reencode_tool_message_attachments():
    """tool结果消息中的附件同样被转码（事务#1822附件实际在tool消息）"""
    h = _FakeHistory(
        [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
            {
                "role": "tool",
                "tool_call_id": "t1",
                "content": [
                    {"type": "text", "text": "读取成功"},
                    {"type": "image_url", "image_url": {"url": _bmp_url()}},
                ],
            },
        ]
    )
    n = _make_engine()._reencode_history_images(h)
    assert n == 1
    url = h._messages[1]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")


# ==================== 层2：_content_filter_next_action 级0救援 ====================


def test_filter_parse_kind_first_action_is_reencode():
    """解析类错误首次进入：动作=转码救援（级别编号不递增）"""
    h = _bmp_history()
    engine = _make_engine(kind=_PARSE_KIND)
    act = engine._content_filter_next_action(h)
    assert "转码" in act and "JPEG" in act
    assert engine._content_filter_deletions == 0


def test_filter_rescue_then_level1_cascade():
    """救援后仍失败：无可转附件→级1清附件→级2删user，级别序列稳定"""
    h = _bmp_history()
    engine = _make_engine(kind=_PARSE_KIND)
    a1 = engine._content_filter_next_action(h)
    assert "转码" in a1
    a2 = engine._content_filter_next_action(h)
    assert "清除多模态附件 1 个" in a2 and engine._content_filter_deletions == 1
    a3 = engine._content_filter_next_action(h)
    assert "删除最后一条user消息" in a3 and engine._content_filter_deletions == 2


def test_filter_filter_kind_skips_rescue():
    """内容审核拦截类：不走转码救援（转码与内容审核无关），直接级1"""
    h = _bmp_history()
    engine = _make_engine(kind="内容审核拦截")
    act = engine._content_filter_next_action(h)
    assert "清除多模态附件 1 个" in act and engine._content_filter_deletions == 1


def test_filter_rescue_only_at_deletions_zero():
    """deletions>0（级1已执行过）：不再触发救援，级别序列不回退"""
    h = _bmp_history()
    engine = _make_engine(kind=_PARSE_KIND, deletions=1)
    act = engine._content_filter_next_action(h)
    assert "删除最后一条user消息" in act and engine._content_filter_deletions == 2


def test_filter_parse_kind_no_attachment_goes_level1():
    """解析类错误但无可转附件（jpeg）：直落级1清附件"""
    h = _FakeHistory(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "x"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": _data_url("image/jpeg", _img_bytes("JPEG", size=(50, 50)))
                        },
                    },
                ],
            }
        ]
    )
    engine = _make_engine(kind=_PARSE_KIND)
    act = engine._content_filter_next_action(h)
    assert "清除多模态附件" in act and engine._content_filter_deletions == 1

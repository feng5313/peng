#!/usr/bin/env python3
"""事务#1959：0字节图片附件导致LLM API 400死循环——多层防御修复测试

事故链路（事务#1955实际场景）：
  shotlife.py 生成 0 字节 shot_0.png
  → read 工具 PIL UnidentifiedImageError 走"PIL不识别→保留原始数据"豁免
  → 空 data URL 附件入库（摘要却报"图片 读取成功"）
  → client 检测附件 prefer_vision 切换 z-glm5.3-flash
  → GLM 400 "file必须传入file_id、file_url、file_data至少之一"
  → 错误不匹配内容解析关键词 → 按普通4xx无限重试，只能人工回滚救出

修复分层：
  层1 file.py: 空数据/内建格式损坏/PDF魔数+体积下限 全部拒绝入库
  层2 multimodal.py: build_content_part 同样校验；load_multimodal_parts
      增加 empty/invalid 状态明确反馈
  层3 multimodal.sanitize_messages_attachments: 发送前最后防线，空payload
      附件part替换为文本提示（client.py chat_stream 发送前调用）
  层4 tool_engine: 400关键词补 file_id/file_url/file_data，命中后接入
      既有逐级清除自愈（转码救援→清附件→删user→裁轮次）
  层5 client.py vision切换回退窗口：附件触发的vision provider切换最多
      服务2个迭代，之后强制切回原provider（防滞留flash等轻量模型）
"""

import asyncio
import base64
import io
import sys
from pathlib import Path

# 项目根目录加入 sys.path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import pytest
from PIL import Image

from peng.llm.client import GLMClient, VISION_SWITCH_MAX_ROUNDS
from peng.llm.multimodal import (
    MIN_MEDIA_FILE_BYTES,
    _part_payload_invalid,
    build_content_part,
    load_multimodal_parts,
    sanitize_messages_attachments,
)
from peng.tools.file import (
    MIN_BINARY_ATTACHMENT_BYTES,
    PIL_BUILTIN_IMAGE_MIMES,
    _build_binary_attachment,
)


def _make_png(w: int = 100, h: int = 100, color=(0, 200, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _make_jpeg(w: int = 100, h: int = 100) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 0, 0)).save(buf, format="JPEG", quality=85)
    return buf.getvalue()


# ==================== 层1：read工具附件构建（file.py） ====================


class TestBuildBinaryAttachment:
    """事务#1955核心场景：0字节/损坏附件必须在read入口被拒绝"""

    def test_zero_byte_png_rejected(self):
        """0字节png（空b64）拒绝，不再伪装"读取成功\""""
        out = _build_binary_attachment("shot_0.png", "", "image/png")
        assert not out.attachments
        assert "失败" in out.summary and "为空" in out.summary

    def test_zero_byte_b64_rejected(self):
        """b64编码的空字节串（via_nest读回0字节文件）同样拒绝"""
        out = _build_binary_attachment(
            "shot_1.png", base64.b64encode(b"").decode(), "image/png"
        )
        assert not out.attachments
        assert "为空" in out.summary

    def test_garbage_builtin_png_rejected(self):
        """内建格式（png）PIL不识别=空/损坏/非图片，拒绝（原豁免分支漏洞）"""
        out = _build_binary_attachment(
            "fake.png", base64.b64encode(b"not-an-image").decode(), "image/png"
        )
        assert not out.attachments
        assert "失败" in out.summary and "无效" in out.summary

    def test_garbage_builtin_jpg_rejected(self):
        out = _build_binary_attachment(
            "fake.jpg", base64.b64encode(b"\xff\xd8\xffgarbage").decode(), "image/jpeg"
        )
        assert not out.attachments

    def test_truncated_jpeg_rejected(self):
        """截断JPEG（识别成功但load失败）拒绝（#1819既有行为不回归）"""
        data = _make_jpeg()
        out = _build_binary_attachment(
            "t.jpg", base64.b64encode(data[: len(data) // 2]).decode(), "image/jpeg"
        )
        assert not out.attachments
        assert "失败" in out.summary

    def test_valid_png_ok(self):
        """正常图片不受影响"""
        out = _build_binary_attachment(
            "ok.png", base64.b64encode(_make_png()).decode(), "image/png"
        )
        assert out.attachments and "读取成功" in out.summary

    def test_heic_passthrough_when_large(self):
        """缺插件格式（heic）非空≥1KB保留原始数据（provider端可能支持）"""
        raw = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 2048
        out = _build_binary_attachment(
            "x.heic", base64.b64encode(raw).decode(), "image/heic"
        )
        assert out.attachments
        url = out.attachments[0]["url"]
        assert base64.b64decode(url.split(",", 1)[1]) == raw

    def test_heic_tiny_rejected(self):
        """缺插件格式<1KB（空壳/截断）拒绝"""
        raw = b"\x00\x00\x00\x18ftypheic"
        out = _build_binary_attachment(
            "x.heic", base64.b64encode(raw).decode(), "image/heic"
        )
        assert not out.attachments
        assert "过小" in out.summary

    def test_invalid_b64_rejected(self):
        """非法base64字符串拒绝（不抛异常）"""
        out = _build_binary_attachment("bad.png", "!!!not-base64!!!", "image/png")
        assert not out.attachments
        assert "失败" in out.summary

    def test_pdf_empty_rejected(self):
        out = _build_binary_attachment("a.pdf", "", "application/pdf")
        assert not out.attachments and "为空" in out.summary

    def test_pdf_tiny_rejected(self):
        out = _build_binary_attachment(
            "a.pdf", base64.b64encode(b"%PDF-1.4 x").decode(), "application/pdf"
        )
        assert not out.attachments and "过小" in out.summary

    def test_pdf_bad_magic_rejected(self):
        out = _build_binary_attachment(
            "b.pdf", base64.b64encode(b"P" * 2048).decode(), "application/pdf"
        )
        assert not out.attachments and "无效" in out.summary

    def test_pdf_valid_ok(self):
        pdf = b"%PDF-1.7 some content " + b"x" * 2048
        out = _build_binary_attachment(
            "c.pdf", base64.b64encode(pdf).decode(), "application/pdf"
        )
        assert out.attachments and "读取成功" in out.summary

    def test_constants_consistent(self):
        """file.py与multimodal.py共享同一最小字节常量（单一事实源）"""
        assert MIN_BINARY_ATTACHMENT_BYTES == MIN_MEDIA_FILE_BYTES == 1024
        assert "image/png" in PIL_BUILTIN_IMAGE_MIMES
        assert "image/heic" not in PIL_BUILTIN_IMAGE_MIMES


# ==================== 层2：@file=/附件路径注入（multimodal.py） ====================


class TestBuildContentPart:
    def test_empty_data_all_kinds_rejected(self):
        for kind, path in [
            ("image", "a.png"),
            ("video", "a.mp4"),
            ("audio", "a.mp3"),
            ("pdf", "a.pdf"),
        ]:
            assert build_content_part(kind, path, b"") is None, kind

    def test_video_audio_min_size(self):
        assert build_content_part("video", "v.mp4", b"\x00" * 500) is None
        assert build_content_part("audio", "a.mp3", b"\x00" * 500) is None
        assert build_content_part("video", "v.mp4", b"\x00" * 2048) is not None
        p = build_content_part("audio", "a.mp3", b"\x00" * 2048)
        assert p is not None and p["input_audio"]["data"]

    def test_pdf_magic_and_size(self):
        assert build_content_part("pdf", "a.pdf", b"%PDF " + b"x" * 100) is None
        assert build_content_part("pdf", "a.pdf", b"PDFX" + b"x" * 2048) is None
        assert build_content_part("pdf", "a.pdf", b"%PDF " + b"x" * 2048) is not None

    def test_builtin_image_garbage_none(self):
        assert build_content_part("image", "x.png", b"garbage" * 10) is None
        assert build_content_part("image", "x.bmp", b"garbage" * 10) is None

    def test_valid_image_ok(self):
        part = build_content_part("image", "ok.png", _make_png())
        assert part is not None
        assert part["image_url"]["url"].startswith("data:image/png;base64,")


class TestLoadMultimodalParts:
    """empty/invalid 状态明确反馈（原均为误导性的unsupported）"""

    def test_status_empty_and_invalid_and_ok(self, tmp_path):
        async def run():
            f0 = tmp_path / "z.png"
            f0.write_bytes(b"")  # 0字节
            _, part, st = await load_multimodal_parts(str(f0))
            assert st == "empty" and part is None

            f1 = tmp_path / "z2.png"
            f1.write_bytes(b"garbage" * 10)  # 垃圾数据
            _, part, st = await load_multimodal_parts(str(f1))
            assert st == "invalid" and part is None

            f2 = tmp_path / "ok.png"
            f2.write_bytes(_make_png())
            _, part, st = await load_multimodal_parts(str(f2))
            assert st == "ok" and part is not None

        asyncio.run(run())


# ==================== 层3：发送前最后防线（sanitize） ====================


class TestSanitizeMessagesAttachments:
    def _valid_image_part(self) -> dict:
        b64 = base64.b64encode(_make_png(20, 20)).decode()
        return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}

    def test_empty_data_url_removed_with_notice(self):
        msgs = [
            {
                "role": "tool",
                "content": [
                    {"type": "text", "text": "图片 读取成功: shot_0.png"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,"}},
                ],
            }
        ]
        out, removed = sanitize_messages_attachments(msgs)
        assert removed == 1
        parts = out[0]["content"]
        assert [p["type"] for p in parts] == ["text", "text"]
        assert "数据为空" in parts[-1]["text"]

    def test_valid_parts_kept(self):
        msgs = [
            {
                "role": "tool",
                "content": [
                    {"type": "text", "text": "ok"},
                    self._valid_image_part(),
                ],
            }
        ]
        out, removed = sanitize_messages_attachments(msgs)
        assert removed == 0
        assert out is msgs  # 无移除返回原列表

    def test_original_not_mutated(self):
        msgs = [
            {
                "role": "tool",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,"}},
                ],
            }
        ]
        sanitize_messages_attachments(msgs)
        assert len(msgs[0]["content"]) == 1  # 原列表不变

    def test_all_part_shapes(self):
        """各provider附件part形态的空payload全部识别"""
        invalid_parts = [
            {"type": "image_url", "image_url": {"url": ""}},
            {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,"}},
            {"type": "input_audio", "input_audio": {"data": "", "format": "mp3"}},
            {"type": "input_image", "image_url": ""},
            {"type": "input_file", "input_file": {"file_url": None}},
            {"type": "file", "file": {"url": "data:application/pdf;base64,"}},
            {"type": "file", "file": {}},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,@@@b64@@@"}},
        ]
        for p in invalid_parts:
            assert _part_payload_invalid(p), p

        valid_parts = [
            self._valid_image_part(),
            {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
            {"type": "input_audio", "input_audio": {"data": base64.b64encode(b"a" * 2048).decode(), "format": "mp3"}},
            {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + base64.b64encode(b"v" * 2048).decode()}},
        ]
        for p in valid_parts:
            assert not _part_payload_invalid(p), p

    def test_metadata_not_mistaken_as_payload(self):
        """format/mime等元数据字段不得被误判为有效payload（data缺失仍无效）"""
        assert _part_payload_invalid({"type": "input_audio", "input_audio": {"format": "mp3"}})
        assert not _part_payload_invalid(
            {"type": "input_audio", "input_audio": {"data": base64.b64encode(b"x" * 100).decode(), "format": "mp3"}}
        )

    def test_mixed_messages(self):
        """多条消息混合：无效移除、有效保留、纯文本消息原样复用"""
        msgs = [
            {"role": "system", "content": "sys"},
            {
                "role": "tool",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,"}},
                    self._valid_image_part(),
                ],
            },
            {"role": "user", "content": "hi"},
            {
                "role": "tool",
                "content": [
                    {"type": "input_audio", "input_audio": {"data": "", "format": "wav"}}
                ],
            },
        ]
        out, removed = sanitize_messages_attachments(msgs)
        assert removed == 2
        assert out[0] is msgs[0] and out[2] is msgs[2]
        assert len(out[1]["content"]) == 2  # 有效图 + 提示text
        assert len(out[3]["content"]) == 1


# ==================== 层4：400关键词分类（自愈入口） ====================


def test_glm_empty_file_error_matched():
    """GLM空payload附件400报错必须命中内容解析分类（事务#1955死循环根因）"""
    from peng.llm.tool_engine import CONTENT_PARSE_ERROR_KEYWORDS

    err = "messages[0].content[0].file必须传入file_id、file_url、file_data至少之一"
    low = err.lower()
    assert any(k in low for k in CONTENT_PARSE_ERROR_KEYWORDS)


def test_plain_errors_still_not_matched():
    from peng.llm.tool_engine import CONTENT_PARSE_ERROR_KEYWORDS

    for err in ["thinking 参数非法", "max_tokens out of range", "model not found"]:
        assert not any(k in err.lower() for k in CONTENT_PARSE_ERROR_KEYWORDS), err


# ==================== 层5：vision切换回退窗口（client.py） ====================


def _bare_client() -> GLMClient:
    c = object.__new__(GLMClient)  # 绕过__init__的config/信号量依赖
    c._vision_switch_from = None
    c._vision_switch_used = 0
    return c


class TestVisionSwitchWindow:
    def test_max_rounds_constant(self):
        assert VISION_SWITCH_MAX_ROUNDS == 2

    def test_advance_window_closed_noop(self):
        c = _bare_client()
        assert c._vision_window_advance(False) is None
        assert c._vision_window_advance(True) is None

    def test_advance_returns_original_after_two_non_attach_rounds(self):
        """附件轮不计费；第2个非附件迭代触发强制回切"""
        c = _bare_client()
        c._vision_switch_from = "z-glm5.3"
        c._vision_switch_used = 0
        assert c._vision_window_advance(True) is None  # 附件轮：刷新预算
        assert c._vision_switch_used == 0
        assert c._vision_window_advance(False) is None  # 非附件轮1：仍flash
        assert c._vision_switch_used == 1
        assert c._vision_window_advance(False) == "z-glm5.3"  # 非附件轮2：回切

    def test_advance_continuous_attachments_never_force(self):
        c = _bare_client()
        c._vision_switch_from = "z-glm5.3"
        c._vision_switch_used = 99
        for _ in range(10):
            assert c._vision_window_advance(True) is None
        assert c._vision_switch_used == 0

    def test_on_selected_opens_window_on_attach_switch(self):
        c = _bare_client()
        c._vision_window_on_selected(True, "z-glm5.3", "z-glm5.3-flash")
        assert c._vision_switch_from == "z-glm5.3"
        assert c._vision_switch_used == 0

    def test_on_selected_closes_window_on_return(self):
        c = _bare_client()
        c._vision_switch_from = "z-glm5.3"
        c._vision_window_on_selected(False, "z-glm5.3-flash", "z-glm5.3")
        assert c._vision_switch_from is None

    def test_on_selected_no_prev_no_window(self):
        c = _bare_client()
        c._vision_window_on_selected(True, None, "flash")
        assert c._vision_switch_from is None

    def test_on_selected_open_window_not_overwritten(self):
        """窗口已开时不被新切换覆盖（仍回切最初原provider）"""
        c = _bare_client()
        c._vision_switch_from = "z-glm5.3"
        c._vision_window_on_selected(True, "z-glm5.3-flash", "other-vision")
        assert c._vision_switch_from == "z-glm5.3"

    def test_on_selected_no_switch_no_window(self):
        """prefer_vision但provider未变（本就是vision provider）不开窗"""
        c = _bare_client()
        c._vision_window_on_selected(True, "vision-top", "vision-top")
        assert c._vision_switch_from is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

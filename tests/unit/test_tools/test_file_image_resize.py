"""
ReadTool 图片缩放单元测试 (#1716)

read 工具读取多模态图片时，把分辨率降低到 1M 像素以内（禁止修改原图）。
"""

import base64
import io

from PIL import Image

from peng.tools.file import (
    MAX_IMAGE_PIXELS,
    ReadTool,
    _build_binary_attachment,
    downscale_image_bytes,
)


def _make_jpeg(width: int, height: int) -> bytes:
    """生成指定尺寸的 JPEG 测试图片"""
    img = Image.new("RGB", (width, height), color=(120, 30, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _make_png(width: int, height: int, mode: str = "RGBA") -> bytes:
    """生成指定尺寸和模式的 PNG 测试图片"""
    img = Image.new(mode, (width, height))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestDownscaleImageBytes:
    """downscale_image_bytes 核心缩放逻辑测试"""

    def test_large_jpeg_downscaled(self):
        """超过 1M 像素的 JPEG 被等比缩放到 1M 像素以内"""
        data = _make_jpeg(3000, 2000)  # 6M 像素
        out, mime, changed = downscale_image_bytes(data, "image/jpeg")
        assert changed is True
        assert mime == "image/jpeg"
        with Image.open(io.BytesIO(out)) as img:
            w, h = img.size
            assert w * h <= MAX_IMAGE_PIXELS
            # 等比缩放
            assert abs(w / h - 3000 / 2000) < 0.01

    def test_small_image_unchanged(self):
        """未超过 1M 像素的图片原样返回（零损失）"""
        data = _make_jpeg(800, 600)  # 0.48M 像素
        out, mime, changed = downscale_image_bytes(data, "image/jpeg")
        assert changed is False
        assert out == data
        assert mime == "image/jpeg"

    def test_original_bytes_untouched(self):
        """函数不修改输入字节流（原图保护）"""
        data = _make_jpeg(4000, 3000)
        snapshot = bytes(data)
        downscale_image_bytes(data, "image/jpeg")
        assert data == snapshot

    def test_rgba_png_keeps_alpha(self):
        """RGBA PNG 缩放后保持透明通道"""
        data = _make_png(2000, 2000)  # 4M 像素
        out, mime, changed = downscale_image_bytes(data, "image/png")
        assert changed is True
        assert mime == "image/png"
        with Image.open(io.BytesIO(out)) as img:
            assert img.mode == "RGBA"
            assert img.size[0] * img.size[1] <= MAX_IMAGE_PIXELS

    def test_webp_downscaled(self):
        """WebP 缩放后保持 WebP 格式"""
        img = Image.new("RGB", (2000, 1500))
        buf = io.BytesIO()
        img.save(buf, format="WEBP")
        data = buf.getvalue()
        out, mime, changed = downscale_image_bytes(data, "image/webp")
        assert changed is True
        assert mime == "image/webp"
        with Image.open(io.BytesIO(out)) as out_img:
            assert out_img.size[0] * out_img.size[1] <= MAX_IMAGE_PIXELS

    def test_exif_orientation_fixed_before_resize(self):
        """带 EXIF orientation 的图片先摆正方向再缩放（避免重编码后方向错误）"""
        img = Image.new("RGB", (3000, 2000), color=(255, 0, 0))
        exif = Image.Exif()
        exif[274] = 6  # Orientation: rotate 90° CW
        buf = io.BytesIO()
        img.save(buf, format="JPEG", exif=exif)
        data = buf.getvalue()
        out, _, changed = downscale_image_bytes(data, "image/jpeg")
        assert changed is True
        with Image.open(io.BytesIO(out)) as out_img:
            w, h = out_img.size
            # orientation=6 摆正后为竖图（高 > 宽）
            assert h > w
            assert w * h <= MAX_IMAGE_PIXELS


class TestBuildBinaryAttachment:
    """_build_binary_attachment 附件构建测试"""

    def test_corrupt_image_fallback(self):
        """损坏的图片数据容错：保留原始数据不抛异常"""
        raw = b"not-an-image"
        out = _build_binary_attachment(
            "x.png", base64.b64encode(raw).decode(), "image/png"
        )
        assert out.attachments[0]["mime"] == "image/png"
        url = out.attachments[0]["url"]
        assert base64.b64decode(url.split(",", 1)[1]) == raw

    def test_pdf_passthrough(self):
        """PDF 不做图片缩放，原样透传"""
        pdf = b"%PDF-1.4 fake"
        out = _build_binary_attachment(
            "x.pdf", base64.b64encode(pdf).decode(), "application/pdf"
        )
        assert "PDF 读取成功" in out.summary
        assert out.attachments[0]["mime"] == "application/pdf"

    def test_downscaled_note_in_summary(self):
        """大图缩放后 summary 带提示语"""
        data = _make_jpeg(3000, 2000)
        out = _build_binary_attachment(
            "big.jpg", base64.b64encode(data).decode(), "image/jpeg"
        )
        assert "已缩放" in out.summary
        assert "原图未修改" in out.summary


class TestReadToolLocalImage:
    """ReadTool 本地 fallback 路径图片附件集成测试"""

    async def test_local_read_downscales_image(self, tmp_path):
        """本地读取大图返回缩放后的附件，原图文件不被修改"""
        p = tmp_path / "big.jpg"
        data = _make_jpeg(3200, 2400)  # 7.68M 像素
        p.write_bytes(data)
        tool = ReadTool()
        result = await tool._execute_local_read(str(p), 1, 2000)
        assert result.attachments
        assert "已缩放" in result.summary
        att = result.attachments[0]
        assert att["mime"] == "image/jpeg"
        b64 = att["url"].split(",", 1)[1]
        with Image.open(io.BytesIO(base64.b64decode(b64))) as img:
            assert img.size[0] * img.size[1] <= MAX_IMAGE_PIXELS
        # 磁盘原图字节完全不变
        assert p.read_bytes() == data

    async def test_local_read_small_image_no_downscale(self, tmp_path):
        """本地读取小图不缩放，原样返回"""
        p = tmp_path / "small.png"
        data = _make_png(500, 400, mode="RGB")
        p.write_bytes(data)
        tool = ReadTool()
        result = await tool._execute_local_read(str(p), 1, 2000)
        assert result.attachments
        assert "已缩放" not in result.summary
        assert p.read_bytes() == data

    async def test_local_read_pdf(self, tmp_path):
        """本地读取 PDF 作为附件返回"""
        p = tmp_path / "doc.pdf"
        p.write_bytes(b"%PDF-1.4 test")
        tool = ReadTool()
        result = await tool._execute_local_read(str(p), 1, 2000)
        assert result.attachments[0]["mime"] == "application/pdf"

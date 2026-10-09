"""
ReadTool 图片规范化单元测试 (#1716 缩放 + #1834 格式转码)

read 工具读取多模态图片时：
1. 把分辨率降低到 1M 像素以内（禁止修改原图）(#1716)
2. 非白名单格式（BMP/TIFF等）转码为 provider 通用 PNG (#1834)
   —— GLM 等 provider 不支持 BMP，原样透传触发 400"图片输入格式/解析错误"
"""

import base64
import io

from PIL import Image

from peng.tools.file import (
    MAX_IMAGE_PIXELS,
    PROVIDER_SAFE_IMAGE_MIMES,
    ReadTool,
    _build_binary_attachment,
    normalize_image_bytes,
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


class TestNormalizeImageBytes:
    """normalize_image_bytes 核心规范化逻辑测试"""

    def test_large_jpeg_downscaled(self):
        """超过 1M 像素的 JPEG 被等比缩放到 1M 像素以内"""
        data = _make_jpeg(3000, 2000)  # 6M 像素
        out, mime, note = normalize_image_bytes(data, "image/jpeg")
        assert note, "缩放必须产生转换说明"
        assert mime == "image/jpeg"
        with Image.open(io.BytesIO(out)) as img:
            w, h = img.size
            assert w * h <= MAX_IMAGE_PIXELS
            # 等比缩放
            assert abs(w / h - 3000 / 2000) < 0.01

    def test_small_whitelist_image_unchanged(self):
        """白名单格式未超过 1M 像素的图片原样返回（零损失）"""
        data = _make_jpeg(800, 600)  # 0.48M 像素
        out, mime, note = normalize_image_bytes(data, "image/jpeg")
        assert note == ""
        assert out == data
        assert mime == "image/jpeg"

    def test_small_bmp_converted_to_png(self):
        """(#1834 事故场景) 像素≤1M 的 BMP 必须转码 PNG——GLM 不支持 BMP"""
        img = Image.new("RGB", (1400, 700), color=(200, 50, 50))  # 0.98M 像素
        buf = io.BytesIO()
        img.save(buf, format="BMP")
        data = buf.getvalue()
        out, mime, note = normalize_image_bytes(data, "image/bmp")
        assert mime == "image/png"
        assert "PNG" in note and "原图未修改" in note
        with Image.open(io.BytesIO(out)) as img:
            assert img.format == "PNG"
            assert img.size == (1400, 700)

    def test_tiff_converted_to_png(self):
        """TIFF 非白名单格式转码 PNG"""
        img = Image.new("RGB", (80, 60))
        buf = io.BytesIO()
        img.save(buf, format="TIFF")
        out, mime, note = normalize_image_bytes(buf.getvalue(), "image/tiff")
        assert mime == "image/png" and note

    def test_oversized_bmp_downscale_and_convert(self):
        """超 1M 像素的非白名单格式：缩放+转码双重处理"""
        img = Image.new("RGB", (1400, 1000))  # 1.4M 像素
        buf = io.BytesIO()
        img.save(buf, format="BMP")
        out, mime, note = normalize_image_bytes(buf.getvalue(), "image/bmp")
        assert mime == "image/png"
        assert "缩放" in note and "PNG" in note
        with Image.open(io.BytesIO(out)) as img:
            assert img.size[0] * img.size[1] <= MAX_IMAGE_PIXELS

    def test_original_bytes_untouched(self):
        """函数不修改输入字节流（原图保护）"""
        data = _make_jpeg(4000, 3000)
        snapshot = bytes(data)
        normalize_image_bytes(data, "image/jpeg")
        assert data == snapshot

    def test_rgba_png_keeps_alpha(self):
        """RGBA PNG 缩放后保持透明通道"""
        data = _make_png(2000, 2000)  # 4M 像素
        out, mime, note = normalize_image_bytes(data, "image/png")
        assert note
        assert mime == "image/png"
        with Image.open(io.BytesIO(out)) as img:
            assert img.mode == "RGBA"
            assert img.size[0] * img.size[1] <= MAX_IMAGE_PIXELS

    def test_rgba_tiff_alpha_preserved_after_convert(self):
        """带 alpha 的非白名单格式转 PNG 保留透明通道"""
        data = _make_png(64, 64, mode="RGBA")
        # 以 TIFF mime 送入（模拟 RGBA TIFF）
        buf = io.BytesIO()
        Image.open(io.BytesIO(data)).save(buf, format="TIFF")
        out, mime, _ = normalize_image_bytes(buf.getvalue(), "image/tiff")
        assert mime == "image/png"
        with Image.open(io.BytesIO(out)) as img:
            assert img.mode == "RGBA"

    def test_webp_downscaled(self):
        """WebP 缩放后保持 WebP 格式"""
        img = Image.new("RGB", (2000, 1500))
        buf = io.BytesIO()
        img.save(buf, format="WEBP")
        data = buf.getvalue()
        out, mime, note = normalize_image_bytes(data, "image/webp")
        assert note
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
        out, _, note = normalize_image_bytes(data, "image/jpeg")
        assert note
        with Image.open(io.BytesIO(out)) as out_img:
            w, h = out_img.size
            # orientation=6 摆正后为竖图（高 > 宽）
            assert h > w
            assert w * h <= MAX_IMAGE_PIXELS

    def test_whitelist_contents(self):
        """白名单 = 主流 provider vision API 普遍支持的格式"""
        assert PROVIDER_SAFE_IMAGE_MIMES == {
            "image/jpeg",
            "image/png",
            "image/gif",
            "image/webp",
        }


class TestBuildBinaryAttachment:
    """_build_binary_attachment 附件构建测试"""

    def test_corrupt_image_fallback(self):
        """(#1959) 内建格式（png）PIL不识别=空/损坏/非图片，拒绝入库"""
        raw = b"not-an-image"
        out = _build_binary_attachment(
            "x.png", base64.b64encode(raw).decode(), "image/png"
        )
        assert not out.attachments
        assert "失败" in out.summary

    def test_plugin_missing_image_passthrough(self):
        """(#1959) 缺插件格式（heic）非空≥1KB时保留原始数据透传"""
        raw = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 2048
        out = _build_binary_attachment(
            "x.heic", base64.b64encode(raw).decode(), "image/heic"
        )
        assert out.attachments and out.attachments[0]["mime"] == "image/heic"
        url = out.attachments[0]["url"]
        assert base64.b64decode(url.split(",", 1)[1]) == raw

    def test_truncated_bmp_rejected(self):
        """(#1819) 截断的 BMP（可识别格式但解码失败）拒绝入库"""
        img = Image.new("RGB", (700, 290))
        buf = io.BytesIO()
        img.save(buf, format="BMP")
        raw = buf.getvalue()
        out = _build_binary_attachment(
            "x.bmp", base64.b64encode(raw[: len(raw) // 2]).decode(), "image/bmp"
        )
        assert not out.attachments
        assert "失败" in out.summary

    def test_bmp_attachment_converted(self):
        """(#1834) BMP 附件 mime 转为 image/png，summary 带转换说明"""
        img = Image.new("RGB", (700, 290))
        buf = io.BytesIO()
        img.save(buf, format="BMP")
        out = _build_binary_attachment(
            "ok.bmp", base64.b64encode(buf.getvalue()).decode(), "image/bmp"
        )
        assert out.attachments
        assert out.attachments[0]["mime"] == "image/png"
        assert "读取成功" in out.summary and "PNG" in out.summary

    def test_pdf_passthrough(self):
        """PDF 不做图片规范化，原样透传（#1959：需≥1KB且带%PDF头）"""
        pdf = b"%PDF-1.4 fake content " + b"x" * 2048
        out = _build_binary_attachment(
            "x.pdf", base64.b64encode(pdf).decode(), "application/pdf"
        )
        assert "PDF 读取成功" in out.summary
        assert out.attachments[0]["mime"] == "application/pdf"

    def test_empty_image_rejected(self):
        """(#1959) 0字节图片（空b64）拒绝入库，不再报"读取成功\""""
        out = _build_binary_attachment("shot_0.png", "", "image/png")
        assert not out.attachments
        assert "失败" in out.summary and "为空" in out.summary

    def test_tiny_pdf_rejected(self):
        """(#1959) <1KB 的PDF（空壳/未完成）拒绝入库"""
        out = _build_binary_attachment(
            "x.pdf", base64.b64encode(b"%PDF-1.4 fake").decode(), "application/pdf"
        )
        assert not out.attachments
        assert "失败" in out.summary

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
        """本地读取白名单小图不缩放，原样返回"""
        p = tmp_path / "small.png"
        data = _make_png(500, 400, mode="RGB")
        p.write_bytes(data)
        tool = ReadTool()
        result = await tool._execute_local_read(str(p), 1, 2000)
        assert result.attachments
        assert "已缩放" not in result.summary
        assert p.read_bytes() == data

    async def test_local_read_small_bmp_converted(self, tmp_path):
        """(#1834) 本地读取小 BMP 转码 PNG 后作为附件返回，原图不变"""
        img = Image.new("RGB", (600, 400))
        buf = io.BytesIO()
        img.save(buf, format="BMP")
        data = buf.getvalue()
        p = tmp_path / "shot.bmp"
        p.write_bytes(data)
        tool = ReadTool()
        result = await tool._execute_local_read(str(p), 1, 2000)
        assert result.attachments
        assert result.attachments[0]["mime"] == "image/png"
        # 磁盘原图保持 BMP 不被修改
        assert p.read_bytes() == data

    async def test_local_read_pdf(self, tmp_path):
        """本地读取 PDF 作为附件返回（#1959：需≥1KB且带%PDF头）"""
        p = tmp_path / "doc.pdf"
        p.write_bytes(b"%PDF-1.4 test content " + b"x" * 2048)
        tool = ReadTool()
        result = await tool._execute_local_read(str(p), 1, 2000)
        assert result.attachments[0]["mime"] == "application/pdf"

    async def test_local_read_tiny_pdf_rejected(self, tmp_path):
        """(#1959) 本地读取<1KB的PDF（空壳/未完成）拒绝作为附件"""
        p = tmp_path / "tiny.pdf"
        p.write_bytes(b"%PDF-1.4 test")
        tool = ReadTool()
        result = await tool._execute_local_read(str(p), 1, 2000)
        assert not result.attachments
        assert "失败" in result.summary

    async def test_local_read_empty_png_rejected(self, tmp_path):
        """(#1959) 本地读取0字节png拒绝（事务#1955场景的本地路径变体）"""
        p = tmp_path / "shot_0.png"
        p.write_bytes(b"")
        tool = ReadTool()
        result = await tool._execute_local_read(str(p), 1, 2000)
        assert not result.attachments
        assert "为空" in result.summary

#!/usr/bin/env python3
"""事务#1592：多模态消息支持单元测试

覆盖：
1. multimodal.py：类型识别、@file=/[附件]路径提取、content part 构建、纯文本降级
2. MessageHistory：多模态 user 消息的添加/序列化/API格式/持久化往返/vision门控
"""

import base64
import sys
from pathlib import Path

# 项目根目录加入 sys.path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from peng.llm.multimodal import (
    build_content_part,
    classify_file,
    extract_file_refs,
    guess_mime,
    resolve_path,
    strip_multimodal_content,
)
from peng.llm.tool_engine import MessageHistory


# ==================== classify_file ====================


def test_classify_image():
    for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".avif", ".ico"]:
        assert classify_file(f"photo{ext}") == "image", ext


def test_classify_video():
    for ext in [".mp4", ".mov", ".mkv", ".avi", ".webm"]:
        assert classify_file(f"clip{ext}") == "video", ext


def test_classify_audio():
    for ext in [".mp3", ".wav", ".m4a", ".amr", ".aac", ".flac", ".ogg"]:
        assert classify_file(f"voice{ext}") == "audio", ext


def test_classify_pdf():
    assert classify_file("doc.pdf") == "pdf"


def test_classify_unsupported():
    assert classify_file("code.py") is None
    assert classify_file("data.txt") is None
    assert classify_file("noext") is None


# ==================== guess_mime ====================


def test_guess_mime():
    assert guess_mime("a.jpg") == "image/jpeg"
    assert guess_mime("a.webp") == "image/webp"
    assert guess_mime("a.m4a") == "audio/mp4"
    assert guess_mime("a.mp4") == "video/mp4"


# ==================== extract_file_refs ====================


def test_extract_file_at():
    refs = extract_file_refs("请分析 @file=./shot.png 谢谢")
    assert refs == ["./shot.png"]


def test_extract_file_non_multimodal_filtered():
    # @file= 指向非多模态文件（如 .py）：静默跳过不提取
    assert extract_file_refs("看下 @file=src/main.py 的问题") == []


def test_extract_multiple_refs_dedup():
    refs = extract_file_refs("@file=/a.png 和 @file=/b.jpg 以及 @file=/a.png")
    assert refs == ["/a.png", "/b.jpg"]


def test_extract_attachment_block():
    text = "看这些\n\n[附件]\n截图1: /tmp/aaa.png\n文档: /tmp/doc.pdf\n"
    refs = extract_file_refs(text)
    assert "/tmp/aaa.png" in refs
    assert "/tmp/doc.pdf" in refs


def test_attachment_non_multimodal_ignored():
    # [附件] 中非多模态文件（如 .zip）不提取
    text = "[附件]\n压缩包: /tmp/x.zip\n图片: /tmp/y.png\n"
    refs = extract_file_refs(text)
    assert "/tmp/x.zip" not in refs
    assert "/tmp/y.png" in refs


def test_no_refs():
    assert extract_file_refs("普通文本没有引用") == []


# ==================== resolve_path ====================


def test_resolve_relative_with_workdir():
    assert resolve_path("sub/img.png", "/wd/root") == "/wd/root/sub/img.png"


def test_resolve_absolute_untouched():
    assert resolve_path("/abs/img.png", "/wd") == "/abs/img.png"


def test_resolve_windows_backslash():
    assert resolve_path("d:\\dl\\img.jpg", None) == "d:/dl/img.jpg"


# ==================== build_content_part ====================


def test_build_image_part():
    data = b"\x89PNG fake"
    part = build_content_part("image", "x.png", data)
    assert part["type"] == "image_url"
    b64 = base64.b64encode(data).decode()
    assert part["image_url"]["url"] == f"data:image/png;base64,{b64}"


def test_build_video_part():
    part = build_content_part("video", "x.mp4", b"1234")
    assert part["type"] == "video_url"
    assert part["video_url"]["url"].startswith("data:video/mp4;base64,")


def test_build_audio_part():
    part = build_content_part("audio", "x.mp3", b"abcd")
    assert part["type"] == "input_audio"
    assert part["input_audio"]["format"] == "mp3"
    assert part["input_audio"]["data"] == base64.b64encode(b"abcd").decode()


def test_build_pdf_part():
    part = build_content_part("pdf", "x.pdf", b"%PDF")
    assert part["type"] == "file"
    assert part["file"]["url"].startswith("data:application/pdf;base64,")


# ==================== strip_multimodal_content ====================


def test_strip_multimodal():
    content = [
        {"type": "text", "text": "看看这张图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
    ]
    assert strip_multimodal_content(content) == "看看这张图"


def test_strip_plain_str():
    assert strip_multimodal_content("hello") == "hello"


# ==================== MessageHistory 多模态支持 ====================


def test_add_user_multimodal_not_merged():
    h = MessageHistory()
    h.add_user("普通消息A")
    h.add_user([{"type": "text", "text": "带图"}, {"type": "image_url", "image_url": {"url": "u"}}])
    h.add_user("普通消息B")
    msgs = h._messages
    assert len(msgs) == 3  # 多模态消息独立，不合并
    assert isinstance(msgs[1]["content"], list)


def test_add_user_str_merge_unchanged():
    h = MessageHistory()
    h.mark_iteration_start()
    h.add_user("A")
    h.add_user("B")
    assert len(h._messages) == 1
    assert h._messages[0]["content"] == "A\nB"


def test_to_api_format_vision_keeps_parts():
    h = MessageHistory()
    h.add_user(
        [
            {"type": "text", "text": "分析图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
        ]
    )
    api = h.to_api_format(thinking_type="thinking", vision=True)
    assert isinstance(api[0]["content"], list)  # [0]是system为None时user在首位
    assert api[0]["content"][1]["type"] == "image_url"


def test_to_api_format_non_vision_strips():
    h = MessageHistory()
    h.add_user(
        [
            {"type": "text", "text": "分析图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
        ]
    )
    api = h.to_api_format(thinking_type="thinking", vision=False)
    assert api[0]["content"] == "分析图"


def test_to_context_roundtrip_preserves_multimodal():
    """to_context持久化 + from_str恢复 往返保留多模态 content"""
    h = MessageHistory()
    h.add_system("sys")
    h.add_user(
        [
            {"type": "text", "text": "看看"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
            {"type": "input_audio", "input_audio": {"data": "QQ==", "format": "mp3"}},
        ]
    )
    from peng.common import jsonp as json

    ctx = json.dumps(h.to_context(thinking_type="thinking"), ensure_ascii=False)

    h2 = MessageHistory()
    assert h2.from_str(ctx) is True
    msgs = h2._messages
    assert len(msgs) == 1
    content = msgs[0]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[2]["type"] == "input_audio"


def test_from_api_format_list_content_not_merged():
    h = MessageHistory()
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": "u"}}]},
        {"role": "user", "content": "next"},
    ]
    h.from_api_format(messages)
    assert len(h._messages) == 2
    assert isinstance(h._messages[0]["content"], list)


def test_compact_protects_list_content():
    """compact 只截短 text part，不破坏附件 part 结构"""
    h = MessageHistory()
    h.add_system("sys")
    # 构造超过截断长度的 text
    long_text = "X" * 5000
    parts = [
        {"type": "text", "text": long_text},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
    ]
    h.add_user(parts)
    for i in range(10):
        h.add_user(f"填充消息{i}" * 100)
    h.compact(compact_threshold=100, compact_ratio=0.5)
    mm_msgs = [m for m in h._messages if isinstance(m.get("content"), list)]
    assert mm_msgs, "多模态消息被删除"
    for m in mm_msgs:
        types = [p["type"] for p in m["content"]]
        assert "image_url" in types


# ==================== prefer_vision provider选择 ====================


def _make_llm_config():
    """构造带测试providers的LLMConfig"""
    from peng.config.loader import LLMConfig, ProviderConfig

    cfg = LLMConfig()
    cfg.providers = {
        "plain": ProviderConfig(
            api_key="k", base_url="http://a", model="m1", priority=90, enable=True, vision="disabled"
        ),
        "mm": ProviderConfig(
            api_key="k", base_url="http://b", model="m2", priority=80, enable=True, vision="yes"
        ),
        "vo": ProviderConfig(
            api_key="k", base_url="http://c", model="m3", priority=99, enable=True, vision="only"
        ),
    }
    return cfg


def test_prefer_vision_selects_vision_provider():
    """无current时prefer_vision=True在vision provider中选最优（跳过vision-only）"""
    cfg = _make_llm_config()
    r = cfg.get_provider(current=None, prefer_vision=True)
    assert r["provider"] == "mm"  # vo(99)被跳过（vision-only），plain(90)非vision，mm(80)


def test_prefer_vision_switches_from_non_vision_current():
    """current不支持vision时prefer_vision切换到vision provider"""
    cfg = _make_llm_config()
    r = cfg.get_provider(current="plain", prefer_vision=True)
    assert r["provider"] == "mm"


def test_prefer_vision_keeps_vision_current():
    """current已是vision provider时不切换"""
    cfg = _make_llm_config()
    r = cfg.get_provider(current="mm", prefer_vision=True)
    assert r["provider"] == "mm"


def test_prefer_vision_favorites_take_priority():
    """显式@provider指定（favorites）优先于prefer_vision"""
    cfg = _make_llm_config()
    r = cfg.get_provider(current=None, favorites=["plain"], prefer_vision=True)
    assert r["provider"] == "plain"


def test_prefer_vision_fallback_without_vision_provider():
    """无可用vision provider时回退普通选择（多模态降级文本）"""
    cfg = _make_llm_config()
    del cfg.providers["mm"]
    r = cfg.get_provider(current=None, prefer_vision=True)
    assert r["provider"] == "plain"


def test_no_prefer_vision_unchanged_behavior():
    """prefer_vision=False时行为不变（priority最高可用）"""
    cfg = _make_llm_config()
    r = cfg.get_provider(current=None, prefer_vision=False)
    assert r["provider"] == "plain"  # vo(99)是vision-only被排除，plain(90)最高


# ==================== Windows路径跨平台（#1594修复） ====================


def test_is_absolute_any():
    from peng.llm.multimodal import is_absolute_any

    assert is_absolute_any("D:/downloads/test.jpg")  # Windows正斜杠
    assert is_absolute_any("D:\\downloads\\test.jpg")  # Windows反斜杠
    assert is_absolute_any("/home/x/a.png")  # Linux
    assert is_absolute_any("\\\\server\\share\\a.png")  # UNC
    assert not is_absolute_any("relative/a.png")
    assert not is_absolute_any("./x.jpg")


def test_resolve_windows_path_not_joined_on_linux():
    """Windows绝对路径在Linux上不拼接workdir（#1594根因修复）"""
    assert resolve_path("D:\\downloads\\test.jpg", "/wd/root") == "D:/downloads/test.jpg"
    assert resolve_path("D:/downloads/test.jpg", "/wd/root") == "D:/downloads/test.jpg"
    # 相对路径仍正常拼接
    assert resolve_path("sub/img.png", "/wd/root") == "/wd/root/sub/img.png"


def test_extract_windows_file_ref():
    refs = extract_file_refs("@file=D:\\downloads\\test.jpg describe this")
    assert refs == ["D:\\downloads\\test.jpg"]


# ==================== read工具附件进history（#1594修复） ====================


def test_add_tool_result_multimodal():
    """tool结果支持多模态content列表，时间戳写入text part"""
    h = MessageHistory()
    h._messages.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
        }
    )
    parts = [
        {"type": "text", "text": "图片 读取成功"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}},
    ]
    h.add_tool_result("t1", "read", parts)
    m = h._messages[-1]
    assert m["role"] == "tool"
    assert isinstance(m["content"], list)
    assert "[2026-" in m["content"][0]["text"]  # 时间戳进text part
    assert m["content"][1]["type"] == "image_url"


def test_tool_result_multimodal_vision_gate():
    """tool消息多模态vision门控：vision=True保留，False降级文本"""
    h = MessageHistory()
    h._messages.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
        }
    )
    h.add_tool_result(
        "t1",
        "read",
        [
            {"type": "text", "text": "图片 读取成功"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}},
        ],
        add_timestamp=False,
    )
    api_v = h.to_api_format(thinking_type="thinking", vision=True)
    tool_msgs = [m for m in api_v if m["role"] == "tool"]
    assert isinstance(tool_msgs[0]["content"], list)
    api_nv = h.to_api_format(thinking_type="thinking", vision=False)
    tool_msgs2 = [m for m in api_nv if m["role"] == "tool"]
    assert isinstance(tool_msgs2[0]["content"], str)
    assert "图片 读取成功" in tool_msgs2[0]["content"]


def test_tool_result_multimodal_roundtrip():
    """tool多模态结果持久化往返保留"""
    from peng.common import jsonp as json

    h = MessageHistory()
    h._messages.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
        }
    )
    h.add_tool_result(
        "t1",
        "read",
        [
            {"type": "text", "text": "图片 读取成功"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}},
        ],
        add_timestamp=False,
    )
    ctx = json.dumps(h.to_context(thinking_type="thinking"), ensure_ascii=False)
    h2 = MessageHistory()
    assert h2.from_str(ctx)
    tool_msgs = [m for m in h2._messages if m["role"] == "tool"]
    assert isinstance(tool_msgs[0]["content"], list)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"✅ {name}")
            except Exception as e:
                fails += 1
                print(f"❌ {name}: {e}")
    sys.exit(1 if fails else 0)

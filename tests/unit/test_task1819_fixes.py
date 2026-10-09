#!/usr/bin/env python3
"""事务#1819：输出混乱修复的单元测试

覆盖：
1. file.py：损坏图片（截断BMP等）读取时拒绝入库，正常图片/缩放不受影响
2. multimodal.py：demote_messages_attachments 附件降级闭环
3. tool_engine.py：_format_tool_result_info 关键参数摘要（消除参数重复回显）
4. loader.py：运行期 vision 能力禁用（disable_provider_vision / TTL恢复）
5. tool_engine.py：HTTP 400 内容解析类错误关键词分类
"""

import base64
import io
import sys
import time
from pathlib import Path

# 项目根目录加入 sys.path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image

from peng.llm.multimodal import (
    demote_messages_attachments,
    messages_have_attachment_parts,
)
from peng.tools.file import _build_binary_attachment


# ==================== 修复1：损坏图片校验 ====================


def _make_bmp(w: int = 700, h: int = 290, color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="BMP")
    return buf.getvalue()


def _make_png(w: int = 100, h: int = 100) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (0, 255, 0)).save(buf, format="PNG")
    return buf.getvalue()


def test_truncated_bmp_rejected():
    """截断BMP（事务#1818实际场景）：必须拒绝作为附件，防止污染上下文"""
    data = _make_bmp()
    truncated = data[: len(data) // 2]
    out = _build_binary_attachment(
        "shot.bmp", base64.b64encode(truncated).decode(), "image/bmp"
    )
    assert not out.attachments, "损坏图片不应产生附件"
    assert "失败" in out.summary
    assert "损坏" in out.summary


def test_small_truncated_bmp_rejected():
    """小尺寸截断（像素≤1M不触发缩放路径）：强制全量解码也必须拦截"""
    data = _make_bmp()
    out = _build_binary_attachment(
        "small.bmp", base64.b64encode(data[:200]).decode(), "image/bmp"
    )
    assert not out.attachments
    assert "失败" in out.summary


def test_valid_bmp_ok():
    """正常图片：附件正常返回"""
    out = _build_binary_attachment(
        "ok.bmp", base64.b64encode(_make_bmp()).decode(), "image/bmp"
    )
    assert out.attachments and len(out.attachments) == 1
    assert "读取成功" in out.summary


def test_large_png_downscale_ok():
    """超过1M像素的正常PNG：走缩放路径，功能不受影响"""
    png = _make_png(1400, 1000)  # 1.4M像素
    out = _build_binary_attachment(
        "big.png", base64.b64encode(png).decode(), "image/png"
    )
    assert out.attachments and len(out.attachments) == 1
    assert "缩放" in out.summary


def test_corrupt_png_rejected():
    """截断PNG同样被拦截"""
    png = _make_png()
    out = _build_binary_attachment(
        "bad.png", base64.b64encode(png[: len(png) // 3]).decode(), "image/png"
    )
    assert not out.attachments


# ==================== 修复4a：附件降级闭环 ====================


def _msg_with_image() -> list[dict]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看这张图"},
                {"type": "image_url", "image_url": {"url": "data:image/bmp;base64,AAAA"}},
            ],
        },
        {"role": "assistant", "content": "好的"},
    ]


def test_demote_removes_all_attachment_parts():
    msgs = _msg_with_image()
    assert messages_have_attachment_parts(msgs)
    out = demote_messages_attachments(msgs)
    assert not messages_have_attachment_parts(out), "降级后不应残留附件part"


def test_demote_keeps_text_and_notice():
    msgs = _msg_with_image()
    out = demote_messages_attachments(msgs)
    content = out[0]["content"]
    assert isinstance(content, str)
    assert "看这张图" in content, "原text part应保留"
    assert "不支持多模态" in content, "应包含降级说明"


def test_demote_does_not_mutate_original():
    msgs = _msg_with_image()
    demote_messages_attachments(msgs)
    assert messages_have_attachment_parts(msgs), "原消息列表不应被修改"


def test_demote_noop_without_attachments():
    msgs = [{"role": "user", "content": "纯文本"}]
    out = demote_messages_attachments(msgs)
    assert out is msgs or out[0]["content"] == "纯文本"


# ==================== 修复6：工具结果参数摘要 ====================


class _FakeEngine:
    _TOOL_TYPE_NAMES = {
        "read": "📃 读取文件",
        "grep": "🔎 内容搜索",
        "bash": "🚩 执行命令",
        "glob": "🔍 文件搜索",
    }
    _TOOL_RESULT_NO_ARGS = frozenset(
        {"write", "edit", "line_edit", "checklist", "report", "message"}
    )
    _TOOL_RESULT_HAS_PATH = frozenset({"write", "edit", "line_edit"})


def _get_format_tool_result_info():
    import re

    src = Path(ROOT / "src/peng/llm/tool_engine.py").read_text(encoding="utf-8")
    m = re.search(
        r"    def _format_tool_result_info\(self.*?(?=\n    def )", src, re.DOTALL
    )
    code = "\n".join(
        line[4:] if line.startswith("    ") else line for line in m.group(0).split("\n")
    )
    ns = dict(vars(_FakeEngine))
    exec(compile(code, "<tool_engine>", "exec"), ns)
    return ns["_format_tool_result_info"]


def test_result_info_read_shows_path_only():
    """read 结果行只显示路径，不再全量JSON回显"""
    fn = _get_format_tool_result_info()
    out = fn(_FakeEngine(), "read", {"filePath": "D:/work/x/shot.bmp"})
    assert out == "📃 读取文件 D:/work/x/shot.bmp"
    assert "{" not in out, "不应包含JSON花括号"


def test_result_info_bash_shows_command_digest():
    fn = _get_format_tool_result_info()
    out = fn(_FakeEngine(), "bash", {"command": "cd /w && ls", "timeout": 100})
    assert out == "🚩 执行命令 cd /w && ls"


def test_result_info_long_command_truncated():
    fn = _get_format_tool_result_info()
    out = fn(_FakeEngine(), "bash", {"command": "x" * 200})
    assert len(out) < 120 and out.endswith("…")


def test_result_info_write_still_no_args():
    fn = _get_format_tool_result_info()
    out = fn(_FakeEngine(), "write", {"filePath": "D:/a.py", "content": "x" * 500})
    assert out == "🛠️ write D:/a.py"


def test_result_info_unknown_tool_safe():
    fn = _get_format_tool_result_info()
    out = fn(_FakeEngine(), "foo", {"bar": 1})
    assert out == "🛠️ foo"


# ==================== 修复4b：运行期 vision 禁用 ====================


def test_runtime_vision_disable_cycle():
    from peng.config.loader import (
        _RUNTIME_VISION_DISABLED,
        disable_provider_vision,
        is_provider_vision_runtime_disabled,
    )

    name = f"test-prov-{time.time()}"
    try:
        assert not is_provider_vision_runtime_disabled(name)
        disable_provider_vision(name, ttl_seconds=10)
        assert is_provider_vision_runtime_disabled(name)
    finally:
        _RUNTIME_VISION_DISABLED.pop(name, None)


def test_runtime_vision_disable_expires():
    from peng.config.loader import (
        _RUNTIME_VISION_DISABLED,
        disable_provider_vision,
        is_provider_vision_runtime_disabled,
    )

    name = f"test-prov-exp-{time.time()}"
    try:
        disable_provider_vision(name, ttl_seconds=-1)  # 已过期
        assert not is_provider_vision_runtime_disabled(name), "过期应自动恢复"
        assert name not in _RUNTIME_VISION_DISABLED, "过期标记应被清除"
    finally:
        _RUNTIME_VISION_DISABLED.pop(name, None)


def test_runtime_vision_disable_empty_name_safe():
    from peng.config.loader import disable_provider_vision

    disable_provider_vision("", ttl_seconds=10)  # 不应抛异常


# ==================== 修复2：内容解析错误关键词分类 ====================
# 🔧 事务#1959：改为导入 tool_engine 模块级常量（单一事实源），消除测试
# 副本漂移风险
from peng.llm.tool_engine import (  # noqa: E402
    CONTENT_FILTER_KEYWORDS,
    CONTENT_PARSE_ERROR_KEYWORDS,
)


def _matched(err_msg: str) -> bool:
    low = err_msg.lower()
    return any(k in low for k in CONTENT_PARSE_ERROR_KEYWORDS)


def test_parse_error_truncated_image_matched():
    assert _matched(
        "An exception occurred while loading IMAGE data at index 0: "
        "Error while loading data ImageData(url='data:image/bmp;base64,Qk0...'): "
        "Could not decode image: image file is truncated (2872 bytes not processed)"
    )


def test_parse_error_content_type_matched():
    assert _matched("messages.content.type 参数非法，取值范围 ['text']")


def test_plain_param_error_not_matched():
    assert not _matched("thinking 参数非法")
    assert not _matched("max_tokens out of range")


def test_parse_error_glm_empty_file_matched():
    """(#1959) GLM空payload附件报错必须命中（事务#1955死循环根因之一）"""
    assert _matched(
        "messages[0].content[0].file必须传入file_id、file_url、file_data至少之一"
    )
    assert _matched("HTTP 400 file_url is required for file content")


def test_content_filter_keyword_still_first():
    """内容审核关键词优先于解析错误分类（修复2中 _matched_filter 先判）"""
    msg = "content may contain sensitive content and image_url"
    assert any(k in msg.lower() for k in CONTENT_FILTER_KEYWORDS)

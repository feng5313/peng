"""事务#2658：nest exec 输出编码健壮性测试。

背景：Windows GBK (cp936) 控制台/重定向下（daemon 日志、cmd 管道），
远程命令输出中的 U+FFFD 替换字符（如扫描混合编码文件产生）导致
print 抛 UnicodeEncodeError，且被 _do_exec 的 catch-all except 误报为
"无法连接到 peng webui 服务"并返回退出码 1。

覆盖：
- _safe_write：GBK 流上写入不可编码字符不崩溃、以 \\uXXXX 转义保留
- _ensure_safe_stdio：reconfigure 后 print 不再抛编码异常
- _do_exec：U+FFFD 输出不误报"无法连接"、远程退出码正确透传；
  连接失败路径仍正常报告
- _do_exec_stream：SSE 流式输出同样安全
- LocalExecutor._bash_stream：GBK 输出多编码尝试解码（不再变 U+FFFD）
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shlex
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from peng.nest._cli_main import (
    _do_exec,
    _do_exec_stream,
    _ensure_safe_stdio,
    _safe_write,
)
from peng.nest.local import LocalExecutor, _decode_output


def _gbk_wrapper(buf: io.BytesIO) -> io.TextIOWrapper:
    """构造 GBK 编码文本流，模拟 Windows cp936 控制台/重定向（U+FFFD 不可编码）"""
    return io.TextIOWrapper(buf, encoding="gbk", newline="")


def _mock_http_response(payload: dict | list[bytes]) -> MagicMock:
    """构造 urlopen 返回的 context manager 响应：dict→JSON body；list→SSE 行迭代"""
    resp = MagicMock()
    if isinstance(payload, dict):
        resp.read.return_value = json.dumps(payload).encode("utf-8")
    else:
        resp.__iter__.return_value = iter(payload)
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


class TestSafeWrite:
    def test_plain_chinese_passthrough(self):
        buf = io.BytesIO()
        stream = _gbk_wrapper(buf)
        _safe_write(stream, "中文正常输出\n")
        stream.flush()
        assert buf.getvalue().decode("gbk") == "中文正常输出\n"

    def test_ufffd_escaped_not_crash(self):
        """核心场景：GBK 流遇 U+FFFD 不抛异常，转义保留（修复前 UnicodeEncodeError）"""
        buf = io.BytesIO()
        stream = _gbk_wrapper(buf)
        text = "前段" + ("\ufffd" * 3) + "后段"
        _safe_write(stream, text)
        stream.flush()
        raw = buf.getvalue()
        out = raw.decode("gbk")
        assert "\\ufffd\\ufffd\\ufffd" in out  # 转义形式保留，信息不丢失
        assert "前段" in out and "后段" in out  # 可编码部分原样保留、无重复

    def test_ascii_no_penalty(self):
        buf = io.BytesIO()
        stream = _gbk_wrapper(buf)
        _safe_write(stream, "plain ascii")
        stream.flush()
        assert buf.getvalue() == b"plain ascii"

    def test_stream_without_encoding_attr(self):
        """无 encoding 属性的流（如 io.StringIO）也不崩溃"""
        stream = io.StringIO()
        _safe_write(stream, "a\ufffdb")
        assert stream.getvalue() == "a\ufffdb"


class TestEnsureSafeStdio:
    def test_reconfigure_makes_print_safe(self, monkeypatch):
        buf = io.BytesIO()
        wrapper = _gbk_wrapper(buf)
        monkeypatch.setattr(sys, "stdout", wrapper)
        monkeypatch.setattr(sys, "stderr", wrapper)
        _ensure_safe_stdio()
        print("\ufffd", end="")  # 修复前：UnicodeEncodeError
        wrapper.flush()
        assert b"\\ufffd" in buf.getvalue()

    def test_no_reconfigure_stream_tolerated(self, monkeypatch):
        monkeypatch.setattr(sys, "stdout", io.StringIO())  # 无 reconfigure
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        _ensure_safe_stdio()  # 不应抛异常


class TestDoExecGbk:
    def _patch_gbk_stdio(self, monkeypatch) -> tuple[io.BytesIO, io.BytesIO, io.TextIOWrapper, io.TextIOWrapper]:
        out_buf, err_buf = io.BytesIO(), io.BytesIO()
        out_w, err_w = _gbk_wrapper(out_buf), _gbk_wrapper(err_buf)
        monkeypatch.setattr(sys, "stdout", out_w)
        monkeypatch.setattr(sys, "stderr", err_w)
        return out_buf, err_buf, out_w, err_w

    def test_ufffd_stdout_exit_code_passthrough_no_misreport(self, monkeypatch):
        """复现事务#2658：输出含 U+FFFD（position 1533）→ 不崩溃、不误报、退出码透传"""
        out_buf, err_buf, out_w, err_w = self._patch_gbk_stdio(monkeypatch)
        payload = {
            "success": True,
            "exit_code": 7,
            "stdout": "A" * 1532 + "\ufffd" + "B" * 10,
            "stderr": "",
        }
        with patch("urllib.request.urlopen", return_value=_mock_http_response(payload)):
            rc = _do_exec("xin", "python probe_scan.py", timeout=5)
        out_w.flush()
        err_w.flush()
        out = out_buf.getvalue().decode("gbk")
        err = err_buf.getvalue().decode("gbk")
        assert rc == 7  # 远程退出码透传（修复前 rc=1）
        assert "无法连接" not in err  # 不再误报连接失败（修复前误报）
        assert "\\ufffd" in out  # U+FFFD 以转义保留
        assert "[xin]$ python probe_scan.py" in out
        assert "退出码: 7" in out

    def test_failure_result_reported_on_stderr(self, monkeypatch):
        _, err_buf, _, err_w = self._patch_gbk_stdio(monkeypatch)
        with patch("urllib.request.urlopen", return_value=_mock_http_response({"success": False, "error": "boom"})):
            rc = _do_exec("xin", "cmd", timeout=5)
        err_w.flush()
        assert rc == 1
        assert "远程执行失败: boom" in err_buf.getvalue().decode("gbk")

    def test_connection_error_still_reported(self, monkeypatch):
        """重构后连接失败路径仍正常工作"""
        _, err_buf, _, err_w = self._patch_gbk_stdio(monkeypatch)
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            rc = _do_exec("xin", "echo hi", timeout=2)
        err_w.flush()
        assert rc == 1
        assert "无法连接到 peng webui 服务" in err_buf.getvalue().decode("gbk")


class TestDoExecStreamGbk:
    def test_sse_stdout_with_ufffd(self, monkeypatch):
        out_buf, err_buf = io.BytesIO(), io.BytesIO()
        out_w, err_w = _gbk_wrapper(out_buf), _gbk_wrapper(err_buf)
        monkeypatch.setattr(sys, "stdout", out_w)
        monkeypatch.setattr(sys, "stderr", err_w)
        sse = [
            b"event: stdout\n",
            b'data: "' + ("A" * 1532).encode("ascii") + "\ufffd".encode("utf-8") + b'"\n',
            b"\n",
            b"event: done\n",
            b'data: {"exit_code": 3}\n',
            b"\n",
        ]
        with patch("urllib.request.urlopen", return_value=_mock_http_response(sse)):
            rc = _do_exec_stream("xin", "python x.py", 5, 6688, ["localhost"])
        out_w.flush()
        err_w.flush()
        assert rc == 3
        assert "\\ufffd" in out_buf.getvalue().decode("gbk")
        assert "无法连接" not in err_buf.getvalue().decode("gbk")
        assert "退出码: 3" in out_buf.getvalue().decode("gbk")


class TestBashStreamDecode:
    def test_gbk_and_ascii_lines(self, tmp_path):
        """_bash_stream 多编码行解码：GBK 输出正确解码（修复前变 U+FFFD 乱码）"""
        exe = LocalExecutor(security=None)
        code = (
            "import sys; "
            "sys.stdout.buffer.write('中文GBK输出\\n'.encode('gbk')); "
            "sys.stdout.buffer.write('ascii line\\n'.encode('ascii'))"
        )
        cmd = f"{sys.executable} -c {shlex.quote(code)}"
        it = exe._bash_stream({"_request_id": "t1"}, cmd, tmp_path, os.environ.copy(), 15)

        stdout_text, exit_src = [], None

        async def run():
            nonlocal exit_src
            async for resp in it:
                data = resp.data or {}
                if data.get("source") == "stdout":
                    stdout_text.append(data.get("data", ""))
                elif data.get("source") == "exit":
                    exit_src = data.get("data")

        asyncio.run(run())
        joined = "".join(stdout_text)
        assert "中文GBK输出" in joined  # GBK 行正确解码
        assert "ascii line" in joined
        assert "\ufffd" not in joined  # 不再产生替换字符乱码
        assert exit_src == "0"


class TestDecodeOutput:
    def test_variants(self):
        assert _decode_output(b"") == ""
        assert _decode_output("中文utf8".encode("utf-8")) == "中文utf8"
        assert _decode_output("中文gbk".encode("gbk")) == "中文gbk"
        # 非文本字节 latin-1 兜底不崩溃
        assert _decode_output(b"\xff\xfe\x00bin") == "\xff\xfe\x00bin"

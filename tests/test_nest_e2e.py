#!/usr/bin/env python3
"""
Nest 端到端测试 - 在 Windows 平台上运行
测试截屏、窗口查询等核心功能
"""
import asyncio
import json
import sys
import os
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from peng.nest.client import NestClient
from peng.nest.protocol import RequestType

NEST_HOST = os.environ.get("NEST_HOST", "localhost")
NEST_PORT = int(os.environ.get("NEST_PORT", "8899"))

results = []
output_file = Path(__file__).parent.parent / "logs" / "nest_e2e_test.log"


def log(msg):
    print(msg, flush=True)
    results.append(msg)


def is_ok(resp):
    """检查响应是否成功"""
    return resp.type != "error" and resp.error is None


async def run_tests():
    log(f"=== Nest E2E Test ===")
    log(f"Connecting to ws://{NEST_HOST}:{NEST_PORT} ...")

    client = NestClient(host=NEST_HOST, port=NEST_PORT)

    try:
        await client.connect()
        log("[OK] Connected to nest server")
    except Exception as e:
        log(f"[FAIL] Cannot connect to nest: {e}")
        return False

    all_ok = True

    # Test 1: Ping
    try:
        resp = await client.send_request(RequestType.PING, {})
        if is_ok(resp):
            platform = resp.data.get("platform", "?")
            hostname = resp.data.get("hostname", "?")
            log(f"[OK] Ping: platform={platform}, hostname={hostname}")
        else:
            log(f"[FAIL] Ping: {resp.error}")
            all_ok = False
    except Exception as e:
        log(f"[FAIL] Ping: {e}")
        all_ok = False

    # Test 2: List Windows
    try:
        resp = await client.send_request(RequestType.LIST_WINDOWS, {})
        if is_ok(resp):
            windows = resp.data.get("windows", [])
            log(f"[OK] List Windows: {len(windows)} windows found")
            for w in windows[:30]:
                title = w.get("title", "")
                pid = w.get("pid", "")
                log(f"  PID={pid} title={title}")
        else:
            log(f"[FAIL] List Windows: {resp.error}")
            all_ok = False
    except Exception as e:
        log(f"[FAIL] List Windows: {e}")
        all_ok = False

    # Test 3: Screenshot
    try:
        resp = await client.send_request(RequestType.SCREENSHOT, {})
        if is_ok(resp):
            import base64
            img_b64 = resp.data.get("data", "")
            img_size = resp.data.get("size", 0)
            if img_b64:
                img_bytes = base64.b64decode(img_b64)
                ss_file = Path(__file__).parent.parent / "logs" / "nest_test_screenshot.png"
                ss_file.write_bytes(img_bytes)
                log(f"[OK] Screenshot: {img_size} bytes, saved to {ss_file}")
            else:
                log(f"[FAIL] Screenshot: no data returned, resp.data keys={list(resp.data.keys())}")
                all_ok = False
        else:
            log(f"[FAIL] Screenshot: {resp.error}")
            all_ok = False
    except Exception as e:
        log(f"[FAIL] Screenshot: {e}")
        all_ok = False

    # Test 4: Bash
    try:
        resp = await client.send_request(RequestType.BASH, {
            "command": "echo hello_from_nest",
            "timeout": 10000,
        })
        if is_ok(resp):
            stdout = resp.data.get("stdout", "").strip()
            log(f"[OK] Bash: stdout={stdout}")
        else:
            log(f"[FAIL] Bash: {resp.error}")
            all_ok = False
    except Exception as e:
        log(f"[FAIL] Bash: {e}")
        all_ok = False

    # Test 5: Write + Read file
    try:
        test_file = "D:/work/peng/logs/nest_test_file.txt"
        test_content = f"nest e2e test at {time.strftime('%Y-%m-%d %H:%M:%S')}"

        resp_w = await client.send_request(RequestType.WRITE, {
            "path": test_file,
            "content": test_content,
        })
        if is_ok(resp_w):
            log(f"[OK] Write file: {test_file}")
        else:
            log(f"[FAIL] Write: {resp_w.error}")
            all_ok = False

        resp_r = await client.send_request(RequestType.READ, {
            "path": test_file,
        })
        if is_ok(resp_r):
            content = resp_r.data.get("content", "")
            log(f"[OK] Read file: {content.strip()}")
        else:
            log(f"[FAIL] Read: {resp_r.error}")
            all_ok = False
    except Exception as e:
        log(f"[FAIL] File R/W: {e}")
        all_ok = False

    # Test 6: Glob
    try:
        resp = await client.send_request(RequestType.GLOB, {
            "pattern": "nest",
            "path": "D:/work/peng/bin",
        })
        if is_ok(resp):
            files = resp.data.get("files", [])
            log(f"[OK] Glob: {len(files)} files")
        else:
            log(f"[FAIL] Glob: {resp.error}")
            all_ok = False
    except Exception as e:
        log(f"[FAIL] Glob: {e}")
        all_ok = False

    # Test 7: Grep
    try:
        resp = await client.send_request(RequestType.GREP, {
            "pattern": "Nest",
            "path": "D:/work/peng/bin/nest",
        })
        if is_ok(resp):
            matches = resp.data.get("matches", [])
            log(f"[OK] Grep: {len(matches)} matches")
        else:
            log(f"[FAIL] Grep: {resp.error}")
            all_ok = False
    except Exception as e:
        log(f"[FAIL] Grep: {e}")
        all_ok = False

    await client.disconnect()

    log(f"\n=== Result: {'ALL PASSED' if all_ok else 'SOME FAILED'} ===")
    return all_ok


if __name__ == "__main__":
    try:
        ok = asyncio.run(run_tests())
    except Exception as e:
        import traceback
        log(f"[FATAL] {e}\n{traceback.format_exc()}")
        ok = False

    output_file.parent.mkdir(exist_ok=True)
    output_file.write_text("\n".join(results), encoding="utf-8")
    print(f"\nResults saved to {output_file}")
    sys.exit(0 if ok else 1)

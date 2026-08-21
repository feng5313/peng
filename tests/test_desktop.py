#!/usr/bin/env python3
"""测试桌面访问权限 - 截屏和窗口列表"""
import sys
sys.path.insert(0, "D:/work/peng/src")

log_lines = []

def log(msg):
    print(msg, flush=True)
    log_lines.append(msg)

# Test 1: 截屏
try:
    from PIL import ImageGrab
    img = ImageGrab.grab()
    img.save("D:/work/peng/logs/test_desktop.png")
    log(f"[OK] Screenshot: {img.size}")
except Exception as e:
    log(f"[FAIL] Screenshot: {e}")

# Test 2: 窗口列表
try:
    import ctypes
    from ctypes import wintypes
    EnumWindows = ctypes.windll.user32.EnumWindows
    GetWindowTextW = ctypes.windll.user32.GetWindowTextW
    IsWindowVisible = ctypes.windll.user32.IsWindowVisible
    GetWindowThreadProcessId = ctypes.windll.user32.GetWindowThreadProcessId
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    
    windows = []
    def enum_cb(hwnd, lparam):
        if IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(256)
            GetWindowTextW(hwnd, title, 256)
            pid = wintypes.DWORD()
            GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            t = title.value.strip()
            if t:
                windows.append({"pid": pid.value, "title": t})
        return True
    
    EnumWindows(WNDENUMPROC(enum_cb), 0)
    log(f"[OK] Windows: {len(windows)} found")
    for w in windows[:20]:
        log(f"  PID={w['pid']} {w['title']}")
except Exception as e:
    log(f"[FAIL] Windows: {e}")

# Test 3: 检查当前会话
try:
    import os
    log(f"[INFO] USERNAME={os.environ.get('USERNAME')}")
    log(f"[INFO] SESSIONNAME={os.environ.get('SESSIONNAME')}")
    log(f"[INFO] CLIENTNAME={os.environ.get('CLIENTNAME')}")
except Exception as e:
    log(f"[INFO] env: {e}")

# 保存结果
from pathlib import Path
Path("D:/work/peng/logs/test_desktop.log").write_text("\n".join(log_lines), encoding="utf-8")

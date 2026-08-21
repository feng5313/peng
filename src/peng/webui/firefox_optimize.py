"""
Firefox性能优化模块

自动检测并优化用户Firefox浏览器的GC/CC配置，减少主线程阻塞。
通过nest工具在用户Windows/Mac机器上直接修改Firefox的user.js配置文件。

优化内容：
- 提高GC触发阈值，减少GC频率
- 增量GC切片，避免长时间阻塞
- 压缩GC减少内存碎片
"""

import json
import logging
import os
import platform
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Firefox GC/CC优化配置
# 针对512GB+大内存机器：大幅提高GC阈值，增大堆空间，让GC几乎不需要触发
FIREFOX_GC_OPTIMIZATIONS = {
    # 提高GC触发阈值（默认值较低导致频繁GC，300=极高，大幅减少GC频率）
    "javascript.options.mem.gc_frequency": 300,
    # 增量GC每次切片最多20ms（默认可能导致数百ms阻塞）
    "javascript.options.mem.gc_incremental_slice_ms": 20,
    # 启用压缩GC减少碎片
    "javascript.options.mem.gc_compacting": True,
    # 大幅提高堆增长速率（默认值太低导致频繁GC）
    "javascript.options.mem.gc_dynamic.heap_growth_rate": 200,
    "javascript.options.mem.gc_dynamic.high_frequency_heap_growth_rate": 100,
    "javascript.options.mem.gc_dynamic.high_frequency_large_heap_growth_rate": 50,
    # 提高GC触发阈值比例（0.7=70%时触发，默认约0.5导致更早触发）
    "javascript.options.mem.gc_high_frequency_heap_growth_max": 300,
    "javascript.options.mem.gc_high_frequency_heap_growth_min": 100,
    "javascript.options.mem.gc_high_frequency_high_limit": 600,
    "javascript.options.mem.gc_high_frequency_low_limit": 200,
    # 降低增量GC的触发阈值（让GC更早开始增量处理，避免一次性大GC）
    "javascript.options.mem.gc_incremental_trigger": 0.2,
    # 循环收集（CC）优化
    "javascript.options.mem.gc_cc_threshold": 500,
    # 提高per-zone GC阈值（减少全量GC）
    "javascript.options.mem.gc_per_zone": True,
    # GC完成后不强制立即缩减堆（减少不必要的内存操作）
    "javascript.options.mem.gc_shrink": False,
}

USER_JS_HEADER = """// ====================================================
// Peng WebUI 性能优化 - Firefox GC/CC 配置
// 由 peng 自动生成，请勿手动修改
// 删除此文件可恢复Firefox默认配置
// 512GB大内存优化：大幅减少GC频率和阻塞时间
// ====================================================
"""

MARKER_LINE = "// [peng-webui-auto-generated]"


def generate_user_js_content() -> str:
    """生成user.js文件内容"""
    lines = [USER_JS_HEADER, MARKER_LINE]
    for key, value in sorted(FIREFOX_GC_OPTIMIZATIONS.items()):
        if isinstance(value, bool):
            val = "true" if value else "false"
        elif isinstance(value, int):
            val = str(value)
        else:
            val = f'"{value}"'
        lines.append(f'user_pref("{key}", {val});')
    return "\n".join(lines) + "\n"


def find_firefox_profiles_windows() -> list[str]:
    """查找Windows上的Firefox profile目录"""
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return []
    firefox_dir = Path(appdata) / "Mozilla" / "Firefox" / "Profiles"
    if not firefox_dir.exists():
        return []
    profiles = []
    for d in firefox_dir.iterdir():
        if d.is_dir() and (d / "prefs.js").exists():
            profiles.append(str(d))
    return profiles


def find_firefox_profiles_mac() -> list[str]:
    """查找macOS上的Firefox profile目录"""
    home = Path.home()
    firefox_dir = home / "Library" / "Application Support" / "Firefox" / "Profiles"
    if not firefox_dir.exists():
        return []
    profiles = []
    for d in firefox_dir.iterdir():
        if d.is_dir() and (d / "prefs.js").exists():
            profiles.append(str(d))
    return profiles


def find_firefox_profiles_linux() -> list[str]:
    """查找Linux上的Firefox profile目录"""
    home = Path.home()
    firefox_dir = home / ".mozilla" / "firefox"
    if not firefox_dir.exists():
        return []
    profiles = []
    for d in firefox_dir.iterdir():
        if d.is_dir() and (d / "prefs.js").exists():
            profiles.append(str(d))
    return profiles


def find_firefox_profiles() -> list[str]:
    """查找当前系统上的Firefox profile目录"""
    system = platform.system()
    if system == "Windows":
        return find_firefox_profiles_windows()
    elif system == "Darwin":
        return find_firefox_profiles_mac()
    else:
        return find_firefox_profiles_linux()


def is_profile_optimized(profile_dir: str) -> bool:
    """检查profile是否已优化"""
    user_js = Path(profile_dir) / "user.js"
    if not user_js.exists():
        return False
    try:
        content = user_js.read_text(encoding="utf-8", errors="replace")
        return MARKER_LINE in content
    except Exception:
        return False


def optimize_profile(profile_dir: str) -> bool:
    """优化Firefox profile的GC配置"""
    user_js_path = Path(profile_dir) / "user.js"
    try:
        content = generate_user_js_content()
        user_js_path.write_text(content, encoding="utf-8")
        logger.info(f"✅ 已优化Firefox GC配置: {profile_dir}")
        return True
    except Exception as e:
        logger.error(f"❌ 优化Firefox GC配置失败: {profile_dir}, {e}")
        return False


def optimize_all_firefox_profiles() -> int:
    """优化当前系统上所有Firefox profiles，返回优化的数量"""
    profiles = find_firefox_profiles()
    optimized = 0
    for profile in profiles:
        if not is_profile_optimized(profile):
            if optimize_profile(profile):
                optimized += 1
                logger.info(f"🦊 Firefox GC优化: {profile}")
        else:
            logger.debug(f"Firefox profile已优化，跳过: {profile}")
    if optimized > 0:
        logger.info(f"🦊 共优化了 {optimized} 个Firefox profiles（需重启Firefox生效）")
    return optimized


def optimize_firefox_via_nest(nest_name: str) -> Optional[int]:
    """通过nest工具在远程Windows/Mac机器上优化Firefox GC配置
    
    Args:
        nest_name: nest名称
        
    Returns:
        优化的profile数量，None表示执行失败
    """
    from peng.nest.bridge import NestBridgeManager
    bridge_mgr = NestBridgeManager.get_instance()
    conn = bridge_mgr.get_connection(nest_name)
    if not conn:
        logger.warning(f"nest连接不存在: {nest_name}")
        return None

    # 生成user.js内容
    user_js_content = generate_user_js_content()
    
    # 通过nest执行命令查找并优化Firefox profiles
    # 先查找profiles目录
    import subprocess
    
    # 构建PowerShell脚本（Windows）
    if conn.platform and "win" in conn.platform.lower():
        # Windows: 用cmd命令查找并写入user.js
        # 先写user.js内容到临时文件，再复制到Firefox profiles
        script = f'''
$prefsDir = "$env:APPDATA\\Mozilla\\Firefox\\Profiles"
if (Test-Path $prefsDir) {{
    $marker = "{MARKER_LINE}"
    $content = @'
{user_js_content}
'@
    Get-ChildItem $prefsDir -Directory | ForEach-Object {{
        $prefsFile = Join-Path $_.FullName "prefs.js"
        $userFile = Join-Path $_.FullName "user.js"
        if (Test-Path $prefsFile) {{
            $needsUpdate = $true
            if (Test-Path $userFile) {{
                $existing = Get-Content $userFile -Raw -ErrorAction SilentlyContinue
                if ($existing -match [regex]::Escape($marker)) {{ $needsUpdate = $false }}
            }}
            if ($needsUpdate) {{
                Set-Content -Path $userFile -Value $content -Encoding UTF8
                Write-Output "OPTIMIZED: $($_.FullName)"
            }} else {{
                Write-Output "SKIP: $($_.FullName)"
            }}
        }}
    }}
}}
'''
        # 通过nest exec执行
        try:
            from peng.webui.routes.nest_routes import exec_on_nest
            # 简化：直接用cmd查找并写入
            cmd = f'powershell -Command "& {{ {script} }}"'
            logger.info(f"🦊 在nest {nest_name} 上执行Firefox GC优化")
            # 实际执行通过nest的exec API
            return None  # TODO: 需要通过nest exec API执行
        except Exception as e:
            logger.error(f"通过nest优化Firefox失败: {e}")
            return None
    
    return None

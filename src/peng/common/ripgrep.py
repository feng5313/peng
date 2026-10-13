"""
ripgrep（rg）集成模块

grep 工具优先使用 ripgrep 执行搜索（并行搜索 + 原生 .gitignore 支持，
性能远优于纯 Python 扫描）。本模块提供：

- find_ripgrep()      检测 rg 可执行文件（PATH + 常见安装位置，TTL 缓存）
- search()/asearch()  执行搜索，返回结构化结果；返回 None 表示 rg
                      不可用/无法处理（正则方言差异、超时），调用方回退纯 Python
- format_output()     统一输出格式化（grep 工具与 nest 服务端共用）
- ensure_ripgrep()    自动安装（幂等，供 install.bat / install.sh / fly install）

实现要点（均经实测确认）：
- content 模式用 --json（NDJSON: begin/match/end/summary），路径/行号/内容
  分离，非 UTF-8 行以 base64 bytes 字段给出；文件数以 summary.stats 为准。
- files_with_matches 用 -l --null，count 用 -c --null（NUL 分隔防特殊字符）。
- rg 以搜索根目录为 cwd、目标 "."，保证输出路径相对搜索根（单文件则父目录
  为 cwd、目标为文件名），与 Python 回退路径的 rel_path 语义一致。
- --hidden：对齐 Python 实现（rglob 含隐藏文件）；忽略目录用 -g '!name'
  （任意层级生效）；尊重 .gitignore（对齐 GitIgnoreFilter）。
- 退出码约定：0=有匹配，1=无匹配，2=正则解析错误 → 返回 None 回退。
- Rust regex 不支持 lookaround/反向引用/Python 命名组：能编译 +pcre2 则加
  --pcre2，否则回退 Python re（Python re 支持这些语法）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from peng.common.truncation import truncate_line

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

# 忽略目录（与 GrepTool Python 回退路径的 ignore_dirs 保持一致）
IGNORE_DIRS = (".git", "__pycache__", "node_modules", ".venv", "venv")

# 搜索子进程超时（秒），超时返回 None 由调用方回退
DEFAULT_TIMEOUT = 30.0

# find_ripgrep 结果缓存有效期（秒），确保安装后新终端无需重启即可发现
FIND_CACHE_TTL = 300.0

_RG_EXE = "rg.exe" if IS_WINDOWS else "rg"

# Rust regex 原生不支持、需要 PCRE2 的语法特征（实测 (?<= 默认 rc=2）；
# (?P<name>) 为 Python 命名组写法，Rust 写法是 (?<name>)，也需 PCRE2
_NEEDS_PCRE2 = re.compile(r"\(\?P|\(\?=|\(?!|\(\?<|\(\?<!|\\k<[a-zA-Z_]|\\\d")
# 纯标志组 (?aiLmsux) Rust regex 原生支持，不算 PCRE 特征
_FLAG_GROUP = re.compile(r"\(\?[aiLmsux]+\)", re.IGNORECASE)

_find_cache: tuple[str | None, float] | None = None  # (path|None, monotonic)
_pcre2_cache: dict[str, bool] = {}


# ---------------------------------------------------------------------------
# 检测结果结构
# ---------------------------------------------------------------------------


@dataclass
class RipgrepMatch:
    """单条匹配（path 相对搜索根目录）"""

    path: str
    line: int
    text: str


@dataclass
class RipgrepResult:
    """结构化搜索结果（格式化统一由 format_output 完成）"""

    matches: list[RipgrepMatch] = field(default_factory=list)
    files_with_matches: list[str] = field(default_factory=list)
    files_count: int = 0
    total_matches: int = 0
    source: str = "ripgrep"


def clear_cache() -> None:
    """清空检测缓存（自动安装后调用，强制重新检测）"""
    global _find_cache
    _find_cache = None
    _pcre2_cache.clear()


def find_ripgrep(use_cache: bool = True) -> str | None:
    """查找 rg 可执行文件（PATH + 常见安装位置），带 TTL 缓存"""
    global _find_cache

    if use_cache and _find_cache is not None:
        cached_path, cached_at = _find_cache
        if time.monotonic() - cached_at < FIND_CACHE_TTL:
            if cached_path is None or Path(cached_path).exists():
                return cached_path

    path = shutil.which("rg")
    if not path:
        home = Path.home()
        candidates = [
            home / ".cargo/bin" / _RG_EXE,
            home / ".local/bin" / _RG_EXE,
            home / "scoop/shims" / _RG_EXE,
            Path("/usr/local/bin") / _RG_EXE,
            Path("/opt/homebrew/bin") / _RG_EXE,
        ]
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            candidates.append(Path(local_appdata) / "Microsoft/WinGet/Links" / _RG_EXE)
        for cand in candidates:
            if cand.exists():
                path = str(cand)
                break

    _find_cache = (path, time.monotonic())
    return path


def supports_pcre2(rg_path: str | None = None) -> bool:
    """检测 rg 二进制是否编译了 PCRE2（解析 rg --version features 行）"""
    rg_path = rg_path or find_ripgrep()
    if not rg_path:
        return False
    if rg_path in _pcre2_cache:
        return _pcre2_cache[rg_path]

    ok = False
    try:
        kwargs: dict[str, Any] = {"capture_output": True, "timeout": 5}
        if IS_WINDOWS:
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        out = subprocess.run([rg_path, "--version"], **kwargs)
        for line in _decode(out.stdout).splitlines():
            if line.lower().startswith("features:"):
                ok = "+pcre2" in line.lower()
                break
    except Exception as e:
        logger.debug(f"rg --version 检测失败: {e}")

    _pcre2_cache[rg_path] = ok
    return ok


def needs_pcre2(pattern: str) -> bool:
    """正则是否使用了 Rust regex 原生不支持的语法"""
    probe = _FLAG_GROUP.sub("", pattern)
    return bool(_NEEDS_PCRE2.search(probe))


# ---------------------------------------------------------------------------
# 搜索执行
# ---------------------------------------------------------------------------


def _decode(data: bytes) -> str:
    """解码子进程输出（优先 UTF-8，兼容 Windows 中文编码）"""
    if not data:
        return ""
    for encoding in ("utf-8", "gbk", "cp936"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _norm_rel_path(raw: str) -> str:
    """rg 输出路径相对 cwd（搜索根），去掉 ./ 前缀，与 Python 实现 rel_path 一致"""
    if raw.startswith("./"):
        raw = raw[2:]
    return raw


def _decode_field(obj: dict[str, Any] | None) -> str:
    """解码 JSON 记录的 text/bytes 字段（非 UTF-8 时 rg 给 base64 bytes）"""
    if not obj:
        return ""
    if obj.get("text") is not None:
        return str(obj["text"])
    if obj.get("bytes"):
        try:
            return base64.b64decode(obj["bytes"]).decode("utf-8", errors="ignore")
        except Exception:
            return ""
    return ""


def resolve_target(search_path: str | Path) -> tuple[str, str]:
    """
    计算 (cwd, target)：目录→(目录, ".")；文件→(父目录, 文件名)。
    rg 输出路径相对 cwd，与 Python 回退路径的 rel_path 语义一致。
    """
    p = Path(search_path).expanduser()
    if p.is_file():
        return str(p.parent), p.name
    return str(p), "."


def build_args(
    rg_path: str,
    pattern: str,
    target: str,
    glob: str | None,
    ignore_case: bool,
    output_mode: str,
) -> list[str]:
    """构造 rg 命令行参数（exec 直传，无 shell 引号问题）"""
    args = [rg_path, "--no-messages"]
    if ignore_case:
        args.append("--ignore-case")

    if output_mode == "content":
        args.append("--json")
    elif output_mode == "files_with_matches":
        args.extend(["-l", "--null"])
    elif output_mode == "count":
        args.extend(["-c", "--null"])

    if needs_pcre2(pattern) and supports_pcre2(rg_path):
        args.append("--pcre2")

    # 用户 glob 在前，忽略目录排除在后（实测 include→exclude 顺序生效）
    if glob:
        args.extend(["--glob", glob])
    for d in IGNORE_DIRS:
        args.extend(["--glob", f"!{d}"])
    # 对齐 Python 实现（rglob 包含隐藏文件）
    args.append("--hidden")
    args.extend(["-e", pattern, "--", target])
    return args


def _parse_json_output(stdout: str) -> RipgrepResult:
    """解析 --json NDJSON 输出（begin/match/end/summary）"""
    result = RipgrepResult()
    current_file = ""
    file_seen: dict[str, int] = {}  # path -> matched_lines（end 记录）

    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue

        rtype = rec.get("type")
        data = rec.get("data") or {}

        if rtype == "begin":
            current_file = _norm_rel_path(_decode_field(data.get("path")))
        elif rtype == "match":
            rel = _norm_rel_path(_decode_field(data.get("path"))) or current_file
            if not _filter_ok(rel):  # 双保险：忽略目录兜底校验
                continue
            result.matches.append(
                RipgrepMatch(
                    path=rel,
                    line=int(data.get("line_number") or 0),
                    text=_decode_field(data.get("lines")),
                )
            )
        elif rtype == "end":
            rel = _norm_rel_path(_decode_field(data.get("path"))) or current_file
            stats = data.get("stats") or {}
            matched_lines = int(stats.get("matched_lines") or 0)
            if rel and matched_lines > 0 and _filter_ok(rel):
                file_seen[rel] = matched_lines
            current_file = ""
        elif rtype == "summary":
            stats = data.get("stats") or {}
            result.total_matches = int(stats.get("matched_lines") or 0)
            result.files_count = int(stats.get("searches_with_match") or 0)

    result.files_with_matches = sorted(file_seen)
    if not result.total_matches:
        # summary 缺失兜底（旧版 rg）：按 end 记录累计
        result.total_matches = sum(file_seen.values())
    if not result.files_count:
        result.files_count = len(file_seen)
    return result


def _parse_null_list(stdout: bytes) -> list[str]:
    """解析 -l --null 输出（NUL 分隔路径）"""
    return [
        _norm_rel_path(_decode(p))
        for p in stdout.split(b"\x00")
        if p.strip() and _filter_ok(_norm_rel_path(_decode(p)))
    ]


def _parse_null_counts(stdout: bytes) -> list[tuple[str, int]]:
    """解析 -c --null 输出（每行 path NUL count，行分隔为 \n）"""
    out: list[tuple[str, int]] = []
    for record in stdout.splitlines():
        if not record:
            continue
        parts = record.split(b"\x00")
        if len(parts) < 2:
            continue
        try:
            count = int(_decode(parts[-1]).strip() or 0)
        except ValueError:
            continue
        rel = _norm_rel_path(_decode(b"\x00".join(parts[:-1])))
        if not _filter_ok(rel):  # 双保险：忽略目录兜底校验
            continue
        out.append((rel, count))
    return out


def _result_from_raw(stdout: bytes, output_mode: str) -> RipgrepResult:
    """按输出模式解析原始子进程输出"""
    if output_mode == "content":
        return _parse_json_output(_decode(stdout))

    result = RipgrepResult()
    if output_mode == "files_with_matches":
        paths = sorted(_parse_null_list(stdout))
        result.files_with_matches = paths
        result.files_count = len(paths)
    elif output_mode == "count":
        pairs = _parse_null_counts(stdout)
        result.files_with_matches = sorted(p for p, _ in pairs)
        result.files_count = len(pairs)
        result.total_matches = sum(c for _, c in pairs)
    return result


def _filter_ok(rel_path: str) -> bool:
    """忽略目录兜底校验（glob 已排除，双保险）"""
    return not any(part in IGNORE_DIRS for part in Path(rel_path).parts)


def search(
    pattern: str,
    path: str,
    glob: str | None = None,
    ignore_case: bool = False,
    output_mode: str = "content",
    timeout: float = DEFAULT_TIMEOUT,
) -> RipgrepResult | None:
    """
    同步执行 rg 搜索。

    Returns:
        RipgrepResult，或 None 表示 rg 不可用/无法处理（调用方回退 Python）
    """
    rg_path = find_ripgrep()
    if not rg_path:
        return None

    cwd, target = resolve_target(path)
    if not Path(cwd).exists():
        return None

    args = build_args(rg_path, pattern, target, glob, ignore_case, output_mode)
    try:
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "timeout": timeout,
            "cwd": cwd,
        }
        if IS_WINDOWS:
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        proc = subprocess.run(args, **kwargs)
    except subprocess.TimeoutExpired:
        logger.warning(f"rg 搜索超时（{timeout}s），回退 Python 实现")
        return None
    except (FileNotFoundError, OSError) as e:
        logger.warning(f"rg 执行异常，回退 Python 实现: {e}")
        return None

    if proc.returncode not in (0, 1):
        detail = _decode(proc.stderr).strip()[:200]
        logger.info(f"rg 退出码 {proc.returncode}（{detail}），回退 Python 实现")
        return None

    return _result_from_raw(proc.stdout, output_mode)


async def asearch(
    pattern: str,
    path: str,
    glob: str | None = None,
    ignore_case: bool = False,
    output_mode: str = "content",
    timeout: float = DEFAULT_TIMEOUT,
) -> RipgrepResult | None:
    """异步执行 rg 搜索（grep 工具 / nest 服务端使用），None = 回退 Python"""
    rg_path = find_ripgrep()
    if not rg_path:
        return None

    cwd, target = resolve_target(path)
    if not Path(cwd).exists():
        return None

    args = build_args(rg_path, pattern, target, glob, ignore_case, output_mode)
    kwargs: dict[str, Any] = {"cwd": cwd}
    if IS_WINDOWS:
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
    except (FileNotFoundError, OSError) as e:
        logger.warning(f"rg 启动异常，回退 Python 实现: {e}")
        return None

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        logger.warning(f"rg 搜索超时（{timeout}s），回退 Python 实现")
        return None

    if proc.returncode not in (0, 1):
        detail = _decode(stderr).strip()[:200]
        logger.info(f"rg 退出码 {proc.returncode}（{detail}），回退 Python 实现")
        return None

    return _result_from_raw(stdout or b"", output_mode)


# ---------------------------------------------------------------------------
# 统一输出格式化（grep 工具与 nest 服务端共用，保证两端输出一致）
# ---------------------------------------------------------------------------

NO_MATCH_TEXT = "未找到匹配"


def format_output(
    result: RipgrepResult,
    output_mode: str,
    max_matches: int,
    max_line_length: int,
) -> str:
    """
    将结构化结果格式化为最终输出文本（与 Python 回退路径输出格式一致）。

    content: rel_path:line_num:text（逐行截断 + 截断提示）
    files_with_matches: 文件列表（相对路径）
    count: 汇总统计
    """
    truncated_lines = 0
    lines_out: list[str] = []

    if output_mode == "content":
        for m in result.matches:
            text = m.text.rstrip("\n").rstrip("\r")
            if max_line_length and len(text) > max_line_length:
                text = truncate_line(text, max_line_length)
                truncated_lines += 1
            lines_out.append(f"{m.path}:{m.line}:{text}")
        if not lines_out:
            return NO_MATCH_TEXT
        result_text = "\n".join(lines_out)
    elif output_mode == "files_with_matches":
        if not result.files_with_matches:
            return NO_MATCH_TEXT
        files = result.files_with_matches
        if max_matches and len(files) > max_matches:
            result_text = "\n".join(files[:max_matches])
            result_text += f"\n\n... 还有 {len(files) - max_matches} 个文件"
        else:
            result_text = "\n".join(files)
    else:  # count
        return f"找到 {result.total_matches} 处匹配，在 {result.files_count} 个文件中"

    if output_mode == "content":
        hint_parts = []
        total = max(result.total_matches, len(result.matches))
        if max_matches and total > min(len(result.matches), max_matches):
            shown = min(len(result.matches), max_matches)
            hint_parts.append(
                f"(Results truncated: showing {shown} of {total} "
                f"matches. Consider using a more specific path or pattern.)"
            )
        if truncated_lines > 0:
            hint_parts.append(
                f"({truncated_lines} lines were truncated to {max_line_length} chars)"
            )
        if hint_parts:
            result_text += "\n\n" + "\n".join(hint_parts)
    return result_text


# ---------------------------------------------------------------------------
# 自动安装（install.bat / install.sh / fly install 调用，幂等）
# ---------------------------------------------------------------------------

# GitHub release 资产 target 映射（(system, machine) → asset target）
RELEASE_TARGETS = {
    ("Linux", "x86_64"): "x86_64-unknown-linux-musl",
    ("Linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("Linux", "arm64"): "aarch64-unknown-linux-gnu",
    ("Darwin", "x86_64"): "x86_64-apple-darwin",
    ("Darwin", "arm64"): "aarch64-apple-darwin",
    ("Windows", "x86_64"): "x86_64-pc-windows-msvc",
    ("Windows", "aarch64"): "aarch64-pc-windows-msvc",
}

# 包管理器 → 安装命令（ripgrep 各发行版包名统一）
PKG_COMMANDS = {
    "apt-get": ["install", "-y", "ripgrep"],
    "dnf": ["install", "-y", "ripgrep"],
    "yum": ["install", "-y", "ripgrep"],
    "pacman": ["-S", "--noconfirm", "--needed", "ripgrep"],
    "apk": ["add", "ripgrep"],
    "zypper": ["--non-interactive", "install", "ripgrep"],
    "brew": ["install", "ripgrep"],
}

# Windows 安装器（与 postgres_auto_deploy 同风格：scoop 优先，winget 兜底）
WINDOWS_INSTALLERS = (
    ("scoop", ["install", "ripgrep"]),
    ("winget", ["install", "-e", "--id", "BurntSushi.ripgrep.MSVC",
                "--accept-package-agreements", "--accept-source-agreements",
                "--disable-interactivity"]),
)

# Linux 包管理器检测顺序
LINUX_PKG_ORDER = ("apt-get", "dnf", "yum", "pacman", "zypper", "apk")

# GitHub release 兜底版本（API 不可用时）
FALLBACK_VERSION = "14.1.1"


def _run_install(cmd: list[str], timeout: float = 300.0) -> bool:
    """执行安装命令（输出直打到终端让用户看到进度；与 PG 安装同风格）"""
    print(f"  $ {' '.join(cmd)}")
    sys.stdout.flush()
    try:
        kwargs: dict[str, Any] = {"timeout": timeout}
        if IS_WINDOWS:
            kwargs["creationflags"] = 0x08000000
        result = subprocess.run(cmd, **kwargs)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"  ⚠️ 安装超时（{timeout}s）")
    except (FileNotFoundError, OSError) as e:
        print(f"  ⚠️ 安装异常: {e}")
    sys.stdout.flush()
    return False


def _sudo_prefix() -> list[str]:
    """非 root 时 Linux 包管理需要 sudo（交互式提示密码）"""
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        if shutil.which("sudo"):
            return ["sudo"]
    return []


def install_via_package_manager() -> bool:
    """按检测到的包管理器安装（返回是否执行安装动作）"""
    import platform

    if IS_WINDOWS:
        for name, extra in WINDOWS_INSTALLERS:
            exe = shutil.which(name)
            if not exe:
                continue
            print(f"🚀 使用 {name} 安装 ripgrep...")
            sys.stdout.flush()
            if _run_install([exe] + list(extra), timeout=1800.0):
                return True
            print(f"⚠️ {name} 安装失败")
        return False

    system = platform.system()
    pm = shutil.which("brew") if system == "Darwin" else None
    if pm is None and system != "Darwin":
        for cand in LINUX_PKG_ORDER:
            pm = shutil.which(cand)
            if pm:
                break

    if not pm:
        return False

    name = os.path.basename(pm)
    cmd = _sudo_prefix() + [pm] + PKG_COMMANDS.get(name, [])
    if len(cmd) <= len(_sudo_prefix()) + 1:
        return False
    print(f"🚀 使用 {name} 安装 ripgrep...")
    sys.stdout.flush()
    return _run_install(cmd, timeout=1800.0)


def install_via_release() -> bool:
    """兜底：从 GitHub Releases 下载静态二进制解压到 ~/.local/bin（无管理员权限）"""
    import platform
    import tarfile
    import zipfile

    key = (platform.system(), platform.machine())
    target = RELEASE_TARGETS.get(key)
    if not target:
        logger.info(f"ripgrep 静态二进制无 {key} 构建，跳过自动安装")
        return False

    version = FALLBACK_VERSION
    try:
        import urllib.request

        req = urllib.request.Request(
            "https://api.github.com/repos/BurntSushi/ripgrep/releases/latest",
            headers={"User-Agent": "peng-installer"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            info = json.loads(resp.read())
        version = str(info.get("tag_name", "")).lstrip("v") or FALLBACK_VERSION
    except Exception as e:
        logger.info(f"GitHub API 查询失败，使用兜底版本 {FALLBACK_VERSION}: {e}")

    ext = "zip" if IS_WINDOWS else "tar.gz"
    url = (
        f"https://github.com/BurntSushi/ripgrep/releases/download/"
        f"{version}/ripgrep-{version}-{target}.{ext}"
    )

    # 与 find_ripgrep 候选路径一致（uv 官方安装器也使用该目录，Windows 同理）
    dest_dir = Path.home() / ".local/bin"
    rg_name = "rg.exe" if IS_WINDOWS else "rg"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        print(f"🚀 下载 ripgrep {version}（{target}）...")
        sys.stdout.flush()

        import io
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "peng-installer"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()

        member = None
        if IS_WINDOWS:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                for n in zf.namelist():
                    if os.path.basename(n).lower() == rg_name.lower():
                        member = zf.read(n)
                        break
        else:
            with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
                for m in tf.getmembers():
                    if os.path.basename(m.name) == rg_name and m.isfile():
                        fobj = tf.extractfile(m)
                        member = fobj.read() if fobj else None
                        break
        if member is None:
            print("⚠️ 压缩包中未找到 rg 可执行文件")
            return False

        dest = dest_dir / rg_name
        dest.write_bytes(member)
        if not IS_WINDOWS:
            os.chmod(dest, 0o755)
        print(f"[ OK ] ripgrep 安装到: {dest}")
        if str(dest_dir) not in os.environ.get("PATH", ""):
            print(f"       （如命令不可用请将 {dest_dir} 加入 PATH）")
        sys.stdout.flush()
        return True
    except Exception as e:
        print(f"⚠️ ripgrep 自动下载失败: {e}")
        sys.stdout.flush()
        return False


def ensure_ripgrep() -> str | None:
    """
    确保 ripgrep 可用：已安装直接返回路径（幂等），否则自动安装。

    安装优先级：包管理器（Windows scoop/winget、macOS brew、
    Linux apt/dnf/yum/pacman/zypper/apk）→ GitHub Releases 静态二进制。

    Returns:
        rg 路径（可用），None 表示安装未完成（非致命，grep 自动回退 Python）
    """
    existing = find_ripgrep(use_cache=False)
    if existing:
        print(f"ℹ️ ripgrep 已安装，跳过: {existing}")
        sys.stdout.flush()
        return existing

    print("[ripgrep] 未检测到 rg 命令，开始自动安装...")
    sys.stdout.flush()

    if not install_via_package_manager():
        install_via_release()

    clear_cache()
    found = find_ripgrep(use_cache=False)
    if found:
        print(f"[ OK ] ripgrep 就绪: {found}")
    else:
        print("[WARN] ripgrep 自动安装未完成，grep 将回退内置 Python 搜索（非致命）")
    sys.stdout.flush()
    return found

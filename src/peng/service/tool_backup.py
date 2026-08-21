"""
工具调用备份与回退模块

为文件修改工具调用提供自动备份和一键回退功能：
1. git管理工作目录：使用备份分支 peng_backups/task#id，每次工具调用前用临时索引创建镜像commit
2. 非git工作目录：在备份目录（配置路径或系统temp）初始化git仓库，同步镜像并commit
3. 外部文件（工作目录外）：备份到备份仓库，保留目录结构

设计原则：
- 不干扰用户工作区（git使用临时索引文件）
- 不切换分支
- 备份失败不阻塞工具执行（记录日志，回退链接不显示）
- 支持远程 nest：当事务关联的 nest 不是 local 时，通过 HTTP API 在远程执行

执行模型：
- 全部同步方法（在 tool_engine.py 中通过 asyncio.to_thread 调用）
- 本地：subprocess.run
- 远程 nest：httpx 同步 POST /api/nest/exec（返回 {success, stdout, stderr, exit_code}）
"""

from __future__ import annotations

import base64
import json
import logging
import ntpath
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from peng.common.paths import (
    _is_git_repo,
    chown_to_project_owner,
    ensure_dir_with_owner,
    get_working_directory,
)

logger = logging.getLogger(__name__)

# ==================== 回滚元数据存储池 ====================
# 历史遗留内存池：旧的 rollback:id:<uuid12> 链接在服务未重启时仍可解析；
# 新回滚链接统一使用持久化ID "<task_id>-<seq>"（见 RollbackIdRegistry 一节），
# meta 同时写入此内存池作热缓存（key 即回滚ID）。
# 格式：{meta_id: {"meta": dict, "created_at": float}}
_rollback_meta_pool: dict[str, dict[str, Any]] = {}
_ROLLBACK_META_POOL_MAX = 500  # 最大存储条目数


def _store_rollback_meta(rollback_meta: dict[str, Any]) -> str:
    """将 rollback_meta 存储到内存池，返回短 ID"""
    # 清理过期条目（超过上限时删除最早的）
    if len(_rollback_meta_pool) >= _ROLLBACK_META_POOL_MAX:
        # 按 created_at 排序，删除最早的 10%
        sorted_keys = sorted(
            _rollback_meta_pool.keys(),
            key=lambda k: _rollback_meta_pool[k].get("created_at", 0),
        )
        for k in sorted_keys[: max(1, _ROLLBACK_META_POOL_MAX // 10)]:
            del _rollback_meta_pool[k]

    meta_id = uuid.uuid4().hex[:12]
    _rollback_meta_pool[meta_id] = {
        "meta": rollback_meta,
        "created_at": datetime.now().timestamp(),
    }
    return meta_id


def get_rollback_meta_by_id(meta_id: str) -> dict[str, Any] | None:
    """根据 ID 从内存池获取 rollback_meta"""
    entry = _rollback_meta_pool.get(meta_id)
    if entry:
        return entry["meta"]
    return None


# ==================== 持久化回滚ID注册表 ====================
# 回滚ID格式：<task_id>-<seq>（如 1553-12），在链接生成时分配并持久化，
# 保证"显示的ID"与"使用的ID"完全一致，CLI（bin/task rollback）与 WebUI 共用。
# 存储：
#   data/rollback_ids/task-<id>.json     → {"next_seq": int}
#   data/rollback_ids/task-<id>/<seq>.json → 完整 rollback_meta
_rollback_registry_lock = threading.Lock()
_ROLLBACK_IDS_DIR = None  # 动态获取


def _get_rollback_ids_base() -> str:
    """获取回滚ID持久化根目录（本地 data/rollback_ids）"""
    global _ROLLBACK_IDS_DIR
    if _ROLLBACK_IDS_DIR:
        return _ROLLBACK_IDS_DIR
    try:
        from peng.config.loader import load_config

        base = str(Path(load_config().paths.data_dir_path) / "rollback_ids")
    except Exception:
        base = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "rollback_ids")
    base = os.path.abspath(base)
    try:
        Path(base).mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    _ROLLBACK_IDS_DIR = base
    return base


def allocate_rollback_id(task_id: int) -> str:
    """为事务分配下一个回滚ID（task内自增seq），文件持久化防重启重复

    Returns:
        回滚ID（纯数字 seq 字符串，如 "389"）；持久化失败时退回时间戳低位（尽力而为）
    """
    if not task_id:
        task_id = 0
    with _rollback_registry_lock:
        seq = None
        try:
            state_file = Path(_get_rollback_ids_base()) / f"task-{task_id}.json"
            next_seq = 1
            if state_file.exists():
                try:
                    next_seq = int(json.loads(state_file.read_text(encoding="utf-8")).get("next_seq", 1))
                except Exception:
                    next_seq = 1
            seq = max(1, next_seq)
            state_file.write_text(
                json.dumps({"next_seq": seq + 1}), encoding="utf-8"
            )
        except Exception as e:
            logger.debug(f"回滚ID状态持久化失败(task#{task_id}): {e}")
            # 退回时间戳低位保证唯一性（不持久化，仅在文件系统异常时兜底）
            seq = int(time.time() * 1000) % 1_000_000_000
        return str(seq)


def store_rollback_meta_persistent(rollback_meta: dict[str, Any], rollback_id: str) -> bool:
    """将 rollback_meta 持久化到 data/rollback_ids/task-<id>/<seq>.json

    rollback_id 为纯 seq（如 "389"）；内存热缓存 key 用完整 "<task_id>-<seq>"。
    失败仅记日志。
    """
    try:
        task_id = int(rollback_meta.get("task_id", 0) or 0)
        full_id = f"{task_id}-{rollback_id}"
        task_dir = Path(_get_rollback_ids_base()) / f"task-{task_id}"
        task_dir.mkdir(parents=True, exist_ok=True)
        seq = rollback_id.split("-", 1)[1] if "-" in rollback_id else rollback_id
        meta_file = task_dir / f"{seq}.json"
        meta_file.write_text(
            json.dumps(rollback_meta, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        # 内存热缓存（key=完整ID）：CLI/WebUI 高频读取场景避免磁盘IO
        _rollback_meta_pool[full_id] = {"meta": rollback_meta, "created_at": time.time()}
        return True
    except Exception as e:
        logger.warning(f"回滚meta持久化失败({rollback_id}): {e}")
        return False


def load_rollback_meta_persistent(rollback_id: str) -> dict[str, Any] | None:
    """按回滚ID "<task_id>-<seq>" 加载持久化的 rollback_meta

    优先内存热缓存，其次读文件；均无返回 None。
    """
    entry = _rollback_meta_pool.get(rollback_id)
    if entry:
        return entry["meta"]
    try:
        parts = rollback_id.split("-", 1)
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            return None
        task_id, seq = parts
        meta_file = (
            Path(_get_rollback_ids_base()) / f"task-{task_id}" / f"{seq}.json"
        )
        if not meta_file.exists():
            return None
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        _rollback_meta_pool[rollback_id] = {"meta": meta, "created_at": time.time()}
        return meta
    except Exception as e:
        logger.debug(f"加载回滚meta失败({rollback_id}): {e}")
        return None


def list_rollback_metas(task_id: int) -> list[dict[str, Any]]:
    """列出事务全部持久化回滚meta（按seq升序），供 task rollback --list 使用"""
    result: list[dict[str, Any]] = []
    try:
        task_dir = Path(_get_rollback_ids_base()) / f"task-{task_id}"
        if not task_dir.exists():
            return result
        for f in task_dir.glob("*.json"):
            if not f.stem.isdigit():
                continue
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
                meta["_seq"] = int(f.stem)
                result.append(meta)
            except Exception:
                continue
        result.sort(key=lambda m: m.get("_seq", 0))
    except Exception as e:
        logger.debug(f"列出回滚meta失败(task#{task_id}): {e}")
    return result


def parse_rollback_id(text: str) -> tuple[int, int] | None:
    """宽松解析回滚ID文本为 (task_id, seq)

    支持：12 / R12 / r12 / 1553-12 / R1553-12
    Returns:
        (task_id, seq) 或 None（格式非法）
    """
    if not text:
        return None
    t = text.strip()
    if t and t[0] in "Rr":
        t = t[1:]
    if "-" in t:
        a, _, b = t.partition("-")
        if a.isdigit() and b.isdigit():
            return int(a), int(b)
        return None
    if t.isdigit():
        return None  # 纯数字无法区分task/seq，由调用方结合上下文task_id处理
    return None


def strip_context_stats(history_context: str) -> str:
    """剥离 history context JSON 中的易变统计字段，只保留 messages

    to_context() 输出含 _prompt_tokens/_total_tokens_consumed/_total_cost 等
    统计字段，它们在每次LLM调用后必然变化，与"实际上下文内容变化"无关。
    备份与变更检测统一使用剥离后的内容，避免统计数值变化导致：
    - 无意义的新 commit（context.json 内容实际相同）
    - 回滚预检测误判"history有变化"

    解析失败时原样返回（保守策略：宁可多备份也不丢数据）。
    """
    try:
        data = json.loads(history_context)
        if isinstance(data, dict):
            messages = data.get("messages", [])
            return json.dumps(messages, ensure_ascii=False)
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        return history_context


def _is_abs_cross_platform(path: str) -> bool:
    """跨平台绝对路径检测

    peng 服务可能运行在 Linux 上，但需要处理远程 Windows nest 返回的路径。
    同时检测 Unix 绝对路径（/开头）和 Windows 绝对路径（盘符 X: 或 UNC \\\\）。
    """
    return os.path.isabs(path) or ntpath.isabs(path)

# 需要备份的文件修改工具
FILE_MODIFYING_TOOLS = frozenset({"write", "edit", "line_edit", "bash", "cmd"})

# 备份分支前缀
BACKUP_BRANCH_PREFIX = "peng_backups/task#"

# 临时索引文件名前缀（实际使用时加唯一后缀避免并发冲突）
_TEMP_INDEX_PREFIX = "peng_backup_idx_"

# 回滚diff预览短时缓存（用户反复点击同一回滚链接时跳过 add -A 全量扫描）
# 格式：{cache_key: {"ts": float, "result": dict}}
_preview_diff_cache: dict[str, dict[str, Any]] = {}
_PREVIEW_DIFF_CACHE_TTL = 10.0  # 秒（覆盖"关闭弹窗再点开"的典型间隔，同时保持diff新鲜度）

# 回滚diff预览截断上限（字节）：末尾按完整行对齐，超出部分不传输不渲染
_DIFF_PREVIEW_MAX_BYTES = 3 * 1024 * 1024  # 3MB


def _truncate_diff_by_bytes(
    diff_text: str, max_bytes: int = _DIFF_PREVIEW_MAX_BYTES
) -> tuple[str, bool, int]:
    """按字节截断diff文本（末尾按完整行对齐，不产生半行）

    Args:
        diff_text: 原始diff文本
        max_bytes: 最大字节数（UTF-8编码后计）

    Returns:
        (截断后文本, 是否截断, 未显示字节数)
    """
    raw = diff_text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return diff_text, False, 0

    # 按行累计字节（行内容 + 分隔符\n），只保留完整行
    lines = diff_text.split("\n")
    kept: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        sep = 1 if i < len(lines) - 1 else 0  # 最后一行无换行符
        line_bytes = len(line.encode("utf-8", errors="replace")) + sep
        if used + line_bytes > max_bytes:
            break
        kept.append(line)
        used += line_bytes

    truncated_text = "\n".join(kept)
    remaining = len(raw) - len(truncated_text.encode("utf-8", errors="replace"))
    return truncated_text, True, remaining

# history context 在备份仓库中的存储路径
_HISTORY_FILE = "__history__/context.json"

# 排除同步的目录（注意不排除 __history__）
_EXCLUDE_DIRS = {".peng", ".git", "__pycache__", "node_modules", ".venv", "data"}


# ==================== git 命令执行（支持本地/远程） ====================


def _run_git(
    args: list[str],
    cwd: str | Path,
    env: dict[str, str] | None = None,
    timeout: int = 15,
    nest_name: str | None = None,
) -> subprocess.CompletedProcess:
    """执行git命令（自动选择本地/远程）

    Args:
        args: git命令参数列表
        cwd: 工作目录
        env: 额外环境变量
        timeout: 超时秒数
        nest_name: nest名称，None或"local"表示本地

    Returns:
        CompletedProcess（兼容接口）
    """
    if not nest_name or nest_name == "local":
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        try:
            return subprocess.run(
                ["git"] + args,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                env=full_env,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            return subprocess.CompletedProcess(
                args=["git"] + args, returncode=-1, stdout="", stderr=f"timeout: {e}"
            )
    else:
        return _run_git_remote(args, str(cwd), env, timeout, nest_name)


def _run_git_remote(
    args: list[str],
    cwd: str,
    env: dict[str, str] | None,
    timeout: int,
    nest_name: str,
) -> subprocess.CompletedProcess:
    """在远程nest上执行git命令

    通过 HTTP POST /api/nest/exec 调用远程nest。
    使用Python脚本包装，跨平台兼容（Windows cmd.exe / Linux bash）。
    """
    import httpx

    # 构建 Python 包装脚本（跨平台兼容，不依赖 shell 语法）
    # 环境变量通过 env dict 设置，工作目录通过 cwd 参数设置
    env_items = ", ".join(f"{k!r}: {v!r}" for k, v in (env or {}).items())
    git_args_py = ", ".join(repr(a) for a in args)
    py_code = (
        "import subprocess, os, sys\n"
        f"env = dict(os.environ)\n"
        f"env.update({{{env_items}}})\n"
        f"result = subprocess.run(['git', {git_args_py}], "
        f"cwd={cwd!r}, env=env, capture_output=True, text=True, timeout={timeout})\n"
        "sys.stdout.write(result.stdout)\n"
        "sys.stderr.write(result.stderr)\n"
        "sys.exit(result.returncode)\n"
    )
    # 用 _run_python_remote 的底层逻辑发送
    code_b64 = base64.b64encode(py_code.encode("utf-8")).decode("ascii")
    full_cmd = f"python -c \"import base64;exec(base64.b64decode('{code_b64}').decode('utf-8'))\""

    try:
        port = _get_webui_port()
        api_hosts = _get_api_hosts(port)
        last_error = None
        for host in api_hosts:
            try:
                url = f"http://{host}:{port}/api/nest/exec"
                resp = httpx.post(
                    url,
                    params={"nest": nest_name, "command": full_cmd, "timeout": timeout},
                    json={},
                    timeout=timeout + 10,
                )
                result = resp.json()
                if not result.get("success"):
                    return subprocess.CompletedProcess(
                        args=["git"] + args,
                        returncode=-1,
                        stdout="",
                        stderr=result.get("error", "remote exec failed"),
                    )
                return subprocess.CompletedProcess(
                    args=["git"] + args,
                    returncode=result.get("exit_code", -1),
                    stdout=result.get("stdout", ""),
                    stderr=result.get("stderr", ""),
                )
            except Exception as e:
                last_error = e
                continue
        return subprocess.CompletedProcess(
            args=["git"] + args, returncode=-1, stdout="", stderr=str(last_error)
        )
    except Exception as e:
        return subprocess.CompletedProcess(
            args=["git"] + args, returncode=-1, stdout="", stderr=str(e)
        )


def _run_shell(
    cmd: str,
    cwd: str | None = None,
    timeout: int = 30,
    nest_name: str | None = None,
) -> subprocess.CompletedProcess:
    """执行任意shell命令（支持本地/远程）

    注意：远程nest可能是Windows系统，仅使用此函数执行git命令。
    跨平台文件操作请使用 _run_python_remote。
    """
    if not nest_name or nest_name == "local":
        try:
            return subprocess.run(
                cmd,
                shell=True,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            return subprocess.CompletedProcess(
                args=cmd, returncode=-1, stdout="", stderr=f"timeout: {e}"
            )
    else:
        # 远程：通过 _run_python_remote 执行（跨平台兼容）
        full_cmd = f"cd {shlex.quote(str(cwd))} && {cmd}" if cwd else cmd
        return _run_shell_remote(full_cmd, timeout, nest_name)


def _run_python_remote(
    code: str, timeout: int = 30, nest_name: str = "local"
) -> subprocess.CompletedProcess:
    """在远程nest上执行Python代码（跨平台兼容）

    通过 nest exec API 发送 `python -c "code"` 命令。
    避免Unix/Windows shell命令差异问题。
    """
    import httpx

    # 将Python代码编码为base64避免shell转义问题
    code_b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")
    cmd = f"python -c \"import base64;exec(base64.b64decode('{code_b64}').decode('utf-8'))\""

    try:
        port = _get_webui_port()
        api_hosts = _get_api_hosts(port)
        last_error = None
        for host in api_hosts:
            try:
                url = f"http://{host}:{port}/api/nest/exec"
                resp = httpx.post(
                    url,
                    params={"nest": nest_name, "command": cmd, "timeout": timeout},
                    json={},
                    timeout=timeout + 10,
                )
                result = resp.json()
                if not result.get("success"):
                    return subprocess.CompletedProcess(
                        args=cmd,
                        returncode=-1,
                        stdout="",
                        stderr=result.get("error", "remote exec failed"),
                    )
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=result.get("exit_code", -1),
                    stdout=result.get("stdout", ""),
                    stderr=result.get("stderr", ""),
                )
            except Exception as e:
                last_error = e
                continue
        return subprocess.CompletedProcess(
            args=cmd, returncode=-1, stdout="", stderr=str(last_error)
        )
    except Exception as e:
        return subprocess.CompletedProcess(args=cmd, returncode=-1, stdout="", stderr=str(e))


def _run_shell_remote(
    cmd: str,
    timeout: int,
    nest_name: str,
) -> subprocess.CompletedProcess:
    """在远程nest上执行shell命令"""
    import httpx

    try:
        port = _get_webui_port()
        api_hosts = _get_api_hosts(port)
        last_error = None
        for host in api_hosts:
            try:
                url = f"http://{host}:{port}/api/nest/exec"
                resp = httpx.post(
                    url,
                    params={"nest": nest_name, "command": cmd, "timeout": timeout},
                    json={},
                    timeout=timeout + 10,
                )
                result = resp.json()
                if not result.get("success"):
                    return subprocess.CompletedProcess(
                        args=cmd,
                        returncode=-1,
                        stdout="",
                        stderr=result.get("error", "remote exec failed"),
                    )
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=result.get("exit_code", -1),
                    stdout=result.get("stdout", ""),
                    stderr=result.get("stderr", ""),
                )
            except Exception as e:
                last_error = e
                continue
        return subprocess.CompletedProcess(
            args=cmd, returncode=-1, stdout="", stderr=str(last_error)
        )
    except Exception as e:
        return subprocess.CompletedProcess(args=cmd, returncode=-1, stdout="", stderr=str(e))


def _get_webui_port() -> int:
    """获取webui端口"""
    try:
        from peng.config.loader import load_config

        config = load_config()
        if hasattr(config, "webui") and config.webui:
            return config.webui.port
    except Exception:
        pass
    return 6688


def _get_api_hosts(port: int) -> list[str]:
    """获取可用的API地址列表"""
    api_hosts: list[str] = []
    try:
        project_root = Path(__file__).resolve().parent.parent.parent.parent
        config_path = project_root / "config" / "nest.json"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                nest_config = json.load(f)
            for inst in nest_config.get("instances", {}).values():
                h = inst.get("host", "")
                if ":" in h:
                    h = h.split(":")[0]
                if h and h not in api_hosts:
                    api_hosts.append(h)
    except Exception:
        pass
    if not api_hosts:
        api_hosts = ["localhost"]
    return api_hosts


# ==================== 路径操作辅助（支持本地/远程） ====================


def _path_exists(path: str | Path, nest_name: str | None = None) -> bool:
    """检查路径是否存在（跨平台）"""
    if not nest_name or nest_name == "local":
        return Path(path).exists()
    code = f"import os,sys; sys.exit(0 if os.path.exists({str(path)!r}) else 1)"
    result = _run_python_remote(code, timeout=5, nest_name=nest_name)
    return result.returncode == 0


def _ensure_dir(path: str | Path, nest_name: str | None = None) -> bool:
    """确保目录存在（跨平台）"""
    if not nest_name or nest_name == "local":
        try:
            ensure_dir_with_owner(path)
            return True
        except Exception:
            return False
    code = f"import os; os.makedirs({str(path)!r}, exist_ok=True)"
    result = _run_python_remote(code, timeout=10, nest_name=nest_name)
    return result.returncode == 0


def _get_git_dir(workdir: str | Path, nest_name: str | None = None) -> Path | str | None:
    """获取.git目录的实际路径（处理worktree情况）

    返回 Path（本地）或 str（远程）
    """
    result = _run_git(["rev-parse", "--git-dir"], cwd=str(workdir), nest_name=nest_name)
    if result.returncode != 0:
        return None
    git_dir = result.stdout.strip()
    if not nest_name or nest_name == "local":
        git_path = Path(git_dir)
        if not git_path.is_absolute():
            git_path = Path(workdir) / git_dir
        return git_path.resolve()
    # 远程：返回绝对路径字符串
    # 注意：peng服务可能运行在Linux上，远程nest可能是Windows，
    # os.path.isabs 在Linux上不识别Windows路径（如 D:/work/...），
    # 必须使用 _is_abs_cross_platform 检测
    if not _is_abs_cross_platform(git_dir):
        result2 = _run_git(["rev-parse", "--show-toplevel"], cwd=str(workdir), nest_name=nest_name)
        if result2.returncode == 0:
            toplevel = result2.stdout.strip().replace("\\", "/")
            return f"{toplevel}/{git_dir}"
    return git_dir


def _get_system_temp(nest_name: str | None = None) -> str:
    """获取系统temp目录路径（跨平台）"""
    if not nest_name or nest_name == "local":
        return tempfile.gettempdir()
    code = "import tempfile; print(tempfile.gettempdir())"
    result = _run_python_remote(code, timeout=5, nest_name=nest_name)
    path = result.stdout.strip().split("\n")[-1].strip() if result.returncode == 0 else "/tmp"
    return path or "/tmp"


class ToolBackupManager:
    """工具调用备份管理器（无状态，所有方法可独立调用）

    全部同步方法。在 async 上下文中通过 asyncio.to_thread() 调用。
    """

    @staticmethod
    def should_backup(tool_name: str) -> bool:
        """判断该工具是否需要备份"""
        return tool_name in FILE_MODIFYING_TOOLS

    # ==================== 快照创建（工具调用前） ====================

    @staticmethod
    def create_snapshot(
        task_id: int,
        tool_name: str,
        tool_args: dict[str, Any],
        workdir: str | Path | None = None,
        nest_name: str | None = None,
    ) -> dict[str, Any] | None:
        """工具调用前创建文件快照（在事务的nest上执行）"""
        if not ToolBackupManager.should_backup(tool_name):
            return None

        workdir = workdir or str(get_working_directory())
        workdir = str(workdir)
        timestamp = datetime.now().isoformat(timespec="seconds")

        args_summary = json.dumps(tool_args, ensure_ascii=False)
        if len(args_summary) > 200:
            args_summary = args_summary[:200] + "..."
        commit_msg = f"backup before {tool_name}: {args_summary}\n\nTask#{task_id} @ {timestamp}"

        try:
            # 提取工具涉及的文件路径，区分 workdir 内/外
            all_paths = ToolBackupManager._extract_file_paths(tool_name, tool_args, workdir)
            external_files: list[str] = []
            for fp in all_paths:
                fp_abs = fp if _is_abs_cross_platform(fp) else os.path.join(workdir, fp)
                fp_norm = os.path.normpath(fp_abs)
                wd_norm = os.path.normpath(workdir)
                if not fp_norm.startswith(wd_norm):
                    external_files.append(fp_norm)

            # 🔧 远程nest：先尝试git路径（合并HTTP请求），失败时回退到非git
            if nest_name and nest_name != "local":
                meta = ToolBackupManager._snapshot_git(
                    workdir, task_id, tool_name, tool_args, commit_msg, timestamp, nest_name
                )
                if meta is None:
                    # git快照失败（可能不是git仓库），尝试非git路径
                    meta = ToolBackupManager._snapshot_non_git(
                        workdir, task_id, tool_name, tool_args, commit_msg, timestamp, nest_name
                    )
            elif _is_git_repo_local(workdir, nest_name):
                meta = ToolBackupManager._snapshot_git(
                    workdir, task_id, tool_name, tool_args, commit_msg, timestamp, nest_name
                )
            else:
                meta = ToolBackupManager._snapshot_non_git(
                    workdir, task_id, tool_name, tool_args, commit_msg, timestamp, nest_name
                )

            # 对外部文件（workdir外）做本地备份（与 nest 无关）
            if meta and external_files:
                ext_commit = ExternalFileBackup.backup(
                    task_id, external_files, f"{tool_name} @ {timestamp}"
                )
                meta["external_files"] = external_files
                meta["external_backup_commit"] = ext_commit

            return meta
        except Exception as e:
            logger.warning(f"创建快照失败(task#{task_id}, {tool_name}): {e}", exc_info=True)
            return None

    # ==================== 迭代开始统一快照 ====================

    @staticmethod
    def create_iteration_snapshot(
        task_id: int,
        history_context: str,
        checklist: str,
        iteration: int,
        workdir: str | Path | None = None,
        nest_name: str | None = None,
    ) -> dict[str, Any] | None:
        """每轮迭代开始时创建统一备份快照（复用工具调用备份机制）

        统一备份四类内容：
        1. 工作目录 git 快照：镜像 commit 记录到分支 peng_backups/task#id
           （git 工作目录用临时索引；非 git 工作目录用备份目录中的独立 git 仓库）
        2. history context：备份到 data/history/task#id（HistoryBackupManager）
        3. checklist：与 history 一同备份（checklist.md）
        4. 外部文件基线：data/file_backups/task#id HEAD（ExternalFileBackup），
           回滚时从该 commit 全量恢复 workdir 外文件

        失败不阻塞迭代（返回 None，仅记录日志）。

        Args:
            task_id: 事务ID
            history_context: history context JSON 字符串
            checklist: checklist 内容字符串
            iteration: 当前迭代轮次（从1开始）
            workdir: 工作目录（None 用默认）
            nest_name: nest名称（None/"local" 表示本地）

        Returns:
            统一回滚元数据（含 git 快照 + history commit + 外部文件基线），
            完全失败返回 None；仅 history 成功时也返回 meta（type=history_only）
        """
        if not task_id:
            return None

        workdir = str(workdir or get_working_directory())
        timestamp = datetime.now().isoformat(timespec="seconds")
        commit_msg = (
            f"iteration#{iteration} start backup\n\nTask#{task_id} @ {timestamp}"
        )

        meta: dict[str, Any] | None = None
        try:
            # 1. 工作目录 git 快照（复用 _snapshot_git / _snapshot_non_git）
            try:
                if nest_name and nest_name != "local":
                    # 远程 nest：先 git 后非 git（与 create_snapshot 相同的回退顺序）
                    meta = ToolBackupManager._snapshot_git(
                        workdir, task_id, "iteration_start", {}, commit_msg, timestamp, nest_name
                    )
                    if meta is None:
                        meta = ToolBackupManager._snapshot_non_git(
                            workdir, task_id, "iteration_start", {}, commit_msg, timestamp, nest_name
                        )
                elif _is_git_repo_local(workdir, nest_name):
                    meta = ToolBackupManager._snapshot_git(
                        workdir, task_id, "iteration_start", {}, commit_msg, timestamp, nest_name
                    )
                else:
                    meta = ToolBackupManager._snapshot_non_git(
                        workdir, task_id, "iteration_start", {}, commit_msg, timestamp, nest_name
                    )
            except Exception as e:
                logger.warning(f"迭代快照文件备份失败(task#{task_id}): {e}", exc_info=True)
                meta = None

            # 2. history + checklist 备份（独立本地 git 仓库，失败不影响文件快照）
            history_commit = None
            try:
                history_commit = HistoryBackupManager.backup(
                    task_id,
                    history_context,
                    f"iteration#{iteration} @ {timestamp}",
                    checklist,
                )
            except Exception as e:
                logger.warning(f"迭代快照history备份失败(task#{task_id}): {e}", exc_info=True)

            if meta is None and not history_commit:
                # 文件快照与 history 备份全部失败
                return None

            if meta is None:
                # 仅 history 成功：生成纯上下文恢复 meta
                meta = {
                    "type": "history_only",
                    "task_id": task_id,
                    "tool_name": "iteration_start",
                    "commit_hash": "",
                    "branch_name": "",
                    "workdir": workdir,
                    "nest_name": nest_name or "local",
                    "file_paths": [],
                    "timestamp": timestamp,
                    "rollback_cmd": "",
                }
            else:
                meta["tool_name"] = "iteration_start"

            meta["iteration"] = iteration
            if history_commit:
                meta["history_commit"] = history_commit

            # 3. 外部文件基线：记录 data/file_backups/task#id 的当前 HEAD，
            #    回滚时从该 commit 全量恢复 workdir 外文件（无需迭代开始时同步）
            try:
                ext_repo = ExternalFileBackup._get_backup_repo(task_id)
                result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=ext_repo,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    meta["external_head_commit"] = result.stdout.strip()
            except Exception as e:
                logger.debug(f"迭代快照外部文件基线获取失败(task#{task_id}): {e}")

            logger.info(
                f"迭代快照完成 task#{task_id} iter#{iteration} "
                f"files={meta.get('type', '')} history={'Y' if history_commit else 'N'}"
            )
            return meta
        except Exception as e:
            logger.warning(f"创建迭代快照失败(task#{task_id}): {e}", exc_info=True)
            return None

    @staticmethod
    def generate_iteration_backup_display(meta: dict[str, Any]) -> str:
        """生成迭代开始备份的显示文本（仅"↩️ 回滚"超链接，详细信息作为hover hint）

        链接文本只显示"↩️ 回滚"；迭代号、快照时间戳、备份范围等详细内容
        写入 meta["hint"]，前端 hover 时解析 URI 中的 meta 显示为 tooltip。

        复用 generate_rollback_link 的 OSC8 超链接，点击后走 /api/tool-rollback
        → ToolBackupManager.rollback 完成文件+history+checklist+外部文件恢复。

        Args:
            meta: create_iteration_snapshot 返回的统一回滚元数据

        Returns:
            ANSI 格式超链接文本（可能为空字符串，表示不显示）
        """
        if not meta:
            return ""
        timestamp = meta.get("timestamp", "")
        ts_display = timestamp.replace("T", " ") if timestamp else ""
        iteration = meta.get("iteration", "?")
        # 描述备份覆盖范围（放入hover hint，不占终端行宽）
        scope_parts = []
        backup_type = meta.get("type", "")
        if backup_type in ("git", "non_git"):
            commit = meta.get("commit_hash", "")[:8]
            scope_parts.append(f"git快照[{commit}]" if commit else "git快照")
        if meta.get("history_commit"):
            scope_parts.append("history+checklist")
        scope_str = " + ".join(scope_parts) if scope_parts else "备份"
        meta["hint"] = f"回滚至 迭代#{iteration} 快照[{ts_display}]（{scope_str}）"
        return ToolBackupManager.generate_rollback_link(meta, text="↩️ 回滚")

    # ==================== 变更检查（工具调用后） ====================

    @staticmethod
    def check_post_tool_changes(
        rollback_meta: dict[str, Any] | None,
        workdir: str | Path | None = None,
        nest_name: str | None = None,
    ) -> dict[str, Any] | None:
        """工具调用结束后检查是否有实际变化

        无变化→返回None（不显示回退链接）
        有变化→更新rollback_meta，加入变更文件列表
        """
        if not rollback_meta:
            return None

        workdir = workdir or str(get_working_directory())
        backup_type = rollback_meta.get("type", "")

        try:
            if backup_type == "git":
                return ToolBackupManager._check_changes_git(rollback_meta, str(workdir), nest_name)
            elif backup_type == "non_git":
                return ToolBackupManager._check_changes_non_git(
                    rollback_meta, str(workdir), nest_name
                )
            else:
                return rollback_meta
        except Exception as e:
            logger.warning(f"检查工具变更失败: {e}", exc_info=True)
            return rollback_meta

    @staticmethod
    def _check_changes_git(
        rollback_meta: dict[str, Any], workdir: str, nest_name: str | None
    ) -> dict[str, Any] | None:
        """git场景：比较备份commit与当前工作目录tree的差异"""
        # 🔧 远程nest：使用批量Python脚本，一次HTTP完成全部git操作
        if nest_name and nest_name != "local":
            return ToolBackupManager._check_changes_git_remote_batch(
                rollback_meta, workdir, nest_name
            )

        commit_hash = rollback_meta["commit_hash"]
        git_dir = _get_git_dir(workdir, nest_name)
        if not git_dir:
            return rollback_meta

        temp_index = f"{git_dir}/{_TEMP_INDEX_PREFIX}{uuid.uuid4().hex[:8]}"
        env = {"GIT_INDEX_FILE": temp_index}

        try:
            _run_git(["read-tree", "HEAD"], cwd=workdir, env=env, nest_name=nest_name)
            _run_git(["add", "-A"], cwd=workdir, env=env, nest_name=nest_name)
            result = _run_git(["write-tree"], cwd=workdir, env=env, nest_name=nest_name)
            if result.returncode != 0:
                return rollback_meta
            current_tree = result.stdout.strip()

            result = _run_git(
                ["rev-parse", f"{commit_hash}^{{tree}}"], cwd=workdir, nest_name=nest_name
            )
            if result.returncode != 0:
                return rollback_meta
            backup_tree = result.stdout.strip()

            if current_tree == backup_tree:
                # git tree 无变化，检查外部文件
                external_files = rollback_meta.get("external_files", [])
                if external_files:
                    ext_changed = ExternalFileBackup.check_changes(
                        rollback_meta.get("task_id", 0), external_files
                    )
                    if ext_changed:
                        rollback_meta["changed_files"] = ext_changed
                        rollback_meta["added_files"] = []
                        rollback_meta["rollback_cmd"] = f"恢复 {len(ext_changed)} 个外部文件"
                        logger.info(f"git检查: 外部文件变化 {ext_changed}")
                        return rollback_meta
                logger.debug(f"git检查: 无文件变化(commit={commit_hash[:8]})")
                return None

            result = _run_git(
                ["diff", "--name-status", backup_tree, current_tree],
                cwd=workdir,
                nest_name=nest_name,
            )
            # 解析 name-status 输出: "A\tnew_file\nM\tmodified_file\nD\tdeleted_file"
            changed_files: list[str] = []
            added_files: list[str] = []  # 快照后新增的文件（回退时需删除）
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if not line.strip():
                        continue
                    parts = line.split("\t", 1)
                    status = parts[0]
                    filepath = parts[1] if len(parts) > 1 else ""
                    if status.startswith("A"):
                        # A = 在 current_tree 中新增（快照时不存在）
                        added_files.append(filepath)
                    else:
                        changed_files.append(filepath)

            rollback_meta["changed_files"] = changed_files + added_files
            rollback_meta["added_files"] = added_files  # 回退时需删除这些文件
            rollback_meta["backup_tree"] = backup_tree  # 用于生成 diff 预览
            rollback_meta["current_tree"] = current_tree

            # 检查外部文件变化（合并到变更列表）
            external_files = rollback_meta.get("external_files", [])
            if external_files:
                ext_changed = ExternalFileBackup.check_changes(
                    rollback_meta.get("task_id", 0), external_files
                )
                if ext_changed:
                    rollback_meta["changed_files"] = rollback_meta["changed_files"] + ext_changed

            if changed_files:
                rollback_meta["rollback_cmd"] = (
                    f"git checkout {commit_hash[:8]} -- {' '.join(changed_files)}"
                )
            else:
                rollback_meta["rollback_cmd"] = f"git checkout {commit_hash[:8]} -- ."

            logger.info(
                f"git检查: 检测到{len(changed_files)}个文件变化 "
                f"(commit={commit_hash[:8]}): {changed_files[:5]}"
            )
            return rollback_meta
        finally:
            _cleanup_temp_index(temp_index, nest_name)

    @staticmethod
    def _check_changes_git_remote_batch(
        rollback_meta: dict[str, Any], workdir: str, nest_name: str
    ) -> dict[str, Any] | None:
        """🔧 远程批量git变更检查：一次HTTP调用完成全部git操作

        将原来的8次HTTP请求（rev-parse --git-dir, read-tree, add, write-tree,
        rev-parse commit^{tree}, diff, 清理temp_index×2）合并为1次。
        """
        commit_hash = rollback_meta["commit_hash"]

        py_code = (
            "import subprocess, os, sys, json, uuid\n"
            f"workdir = {workdir!r}\n"
            f"commit_hash = {commit_hash!r}\n"
            f"temp_prefix = {f'peng_backup_idx_'!r}\n"
            f"result_data = {{}}\n"
            "temp_index = None\n"
            "try:\n"
            "    def git(*args, env=None):\n"
            "        full_env = dict(os.environ)\n"
            "        if env: full_env.update(env)\n"
            "        return subprocess.run(['git'] + list(args), cwd=workdir, env=full_env,\n"
            "                              capture_output=True, text=True, timeout=30)\n"
            "    # 🔧 快速路径：先用 git diff HEAD 判断有无工作区变化\n"
            "    # 如果工作区相对HEAD无变化，且备份commit就是HEAD的祖先，则直接判定无变化\n"
            "    r = git('diff', 'HEAD', '--quiet')\n"
            "    workdir_clean = (r.returncode == 0)  # clean = 无变化\n"
            "    if workdir_clean:\n"
            "        # 工作区与HEAD一致，检查备份commit的tree是否也等于HEAD的tree\n"
            "        r = git('rev-parse', 'HEAD^{tree}')\n"
            "        if r.returncode == 0:\n"
            "            head_tree = r.stdout.strip()\n"
            "            r = git('rev-parse', commit_hash + '^{tree}')\n"
            "            if r.returncode == 0 and r.stdout.strip() == head_tree:\n"
            "                result_data['changed'] = False\n"
            "                print(json.dumps(result_data)); sys.exit(0)\n"
            "    # 慢速路径：完整tree比较\n"
            "    # 获取git目录\n"
            "    r = git('rev-parse', '--git-dir')\n"
            "    if r.returncode != 0:\n"
            f"        result_data['error'] = 'rev-parse --git-dir failed'\n"
            "        print(json.dumps(result_data)); sys.exit(0)\n"
            "    git_dir = r.stdout.strip()\n"
            "    if not os.path.isabs(git_dir):\n"
            "        toplevel = git('rev-parse', '--show-toplevel')\n"
            "        if toplevel.returncode == 0:\n"
            "            git_dir = os.path.join(toplevel.stdout.strip().replace(chr(92), '/'), git_dir)\n"
            "    # 创建临时索引，read-tree + add + write-tree\n"
            "    temp_index = os.path.join(git_dir, temp_prefix + uuid.uuid4().hex[:8])\n"
            "    env_idx = {'GIT_INDEX_FILE': temp_index}\n"
            "    git('read-tree', 'HEAD', env=env_idx)\n"
            "    r_add = git('add', '-A', env=env_idx)\n"
            "    # 🔧 修复 worktree 中 submodule .git 路径错误导致 git add -A 失败\n"
            "    if r_add.returncode != 0:\n"
            "        r_gm = git('config', '-f', '.gitmodules', '--get-regexp', 'path')\n"
            "        submod_paths = [ln.split(' ', 1)[1] for ln in r_gm.stdout.strip().split(chr(10)) if ln.strip()]\n"
            "        if submod_paths:\n"
            "            excludes = ['.'] + [f':(exclude){p}' for p in submod_paths]\n"
            "            git('add', '-A', *excludes, env=env_idx)\n"
            "        else:\n"
            "            git('add', '-A', env=env_idx)\n"
            "    r = git('write-tree', env=env_idx)\n"
            "    if r.returncode != 0:\n"
            f"        result_data['error'] = 'write-tree failed'\n"
            "        print(json.dumps(result_data)); sys.exit(0)\n"
            "    current_tree = r.stdout.strip()\n"
            "    # 获取备份tree\n"
            "    r = git('rev-parse', commit_hash + '^{tree}')\n"
            "    if r.returncode != 0:\n"
            f"        result_data['error'] = 'rev-parse commit tree failed'\n"
            "        print(json.dumps(result_data)); sys.exit(0)\n"
            "    backup_tree = r.stdout.strip()\n"
            "    # 比较差异\n"
            "    if current_tree == backup_tree:\n"
            f"        result_data['changed'] = False\n"
            "    else:\n"
            "        r = git('diff', '--name-status', backup_tree, current_tree)\n"
            "        changed_files = []\n"
            "        added_files = []\n"
            "        if r.returncode == 0:\n"
            "            for line in r.stdout.strip().split(chr(10)):\n"
            "                if not line.strip(): continue\n"
            "                parts = line.split(chr(9), 1)\n"
            "                status = parts[0]\n"
            "                filepath = parts[1] if len(parts) > 1 else ''\n"
            "                if status.startswith('A'):\n"
            "                    added_files.append(filepath)\n"
            "                else:\n"
            "                    changed_files.append(filepath)\n"
            f"        result_data['changed'] = True\n"
            f"        result_data['changed_files'] = changed_files\n"
            f"        result_data['added_files'] = added_files\n"
            f"        result_data['backup_tree'] = backup_tree\n"
            f"        result_data['current_tree'] = current_tree\n"
            "except Exception as e:\n"
            f"    result_data['error'] = str(e)\n"
            "finally:\n"
            "    try:\n"
            "        if temp_index and os.path.exists(temp_index): os.unlink(temp_index)\n"
            "    except: pass\n"
            "print(json.dumps(result_data))\n"
        )

        result = _run_python_remote(py_code, timeout=60, nest_name=nest_name)
        if result.returncode != 0:
            logger.warning(f"远程批量变更检查失败({nest_name}): {result.stderr[:200]}")
            return rollback_meta

        try:
            data = json.loads(result.stdout.strip().split("\n")[-1])
        except (json.JSONDecodeError, IndexError):
            logger.warning(f"远程批量变更检查响应解析失败({nest_name}): {result.stdout[:200]}")
            return rollback_meta

        if data.get("error"):
            logger.warning(f"远程批量变更检查git错误({nest_name}): {data['error'][:200]}")
            return rollback_meta

        if not data.get("changed", False):
            # git tree 无变化，检查外部文件
            external_files = rollback_meta.get("external_files", [])
            if external_files:
                ext_changed = ExternalFileBackup.check_changes(
                    rollback_meta.get("task_id", 0), external_files
                )
                if ext_changed:
                    rollback_meta["changed_files"] = ext_changed
                    rollback_meta["added_files"] = []
                    rollback_meta["rollback_cmd"] = f"恢复 {len(ext_changed)} 个外部文件"
                    logger.info(f"git检查: 外部文件变化 {ext_changed}")
                    return rollback_meta
            logger.debug(f"git检查: 无文件变化(commit={commit_hash[:8]})")
            return None

        changed_files = data.get("changed_files", [])
        added_files = data.get("added_files", [])
        rollback_meta["changed_files"] = changed_files + added_files
        rollback_meta["added_files"] = added_files
        # 存储 trees 用于生成 diff 预览
        if "backup_tree" in data:
            rollback_meta["backup_tree"] = data["backup_tree"]
        if "current_tree" in data:
            rollback_meta["current_tree"] = data["current_tree"]

        # 检查外部文件变化（合并到变更列表）
        external_files = rollback_meta.get("external_files", [])
        if external_files:
            ext_changed = ExternalFileBackup.check_changes(
                rollback_meta.get("task_id", 0), external_files
            )
            if ext_changed:
                rollback_meta["changed_files"] = rollback_meta["changed_files"] + ext_changed

        if changed_files:
            rollback_meta["rollback_cmd"] = (
                f"git checkout {commit_hash[:8]} -- {' '.join(changed_files)}"
            )
        else:
            rollback_meta["rollback_cmd"] = f"git checkout {commit_hash[:8]} -- ."

        logger.info(
            f"git检查: 检测到{len(changed_files)}个文件变化 "
            f"(commit={commit_hash[:8]}): {changed_files[:5]}"
        )
        return rollback_meta

    @staticmethod
    def _check_changes_non_git(
        rollback_meta: dict[str, Any], workdir: str, nest_name: str | None
    ) -> dict[str, Any] | None:
        """非git场景：同步工作目录到备份仓库，对比差异"""
        backup_repo = rollback_meta["backup_repo"]
        commit_hash = rollback_meta["commit_hash"]

        if not _path_exists(backup_repo, nest_name):
            return rollback_meta

        try:
            result = _run_git(
                ["rev-parse", f"{commit_hash}^{{tree}}"], cwd=backup_repo, nest_name=nest_name
            )
            if result.returncode != 0:
                return rollback_meta
            backup_tree = result.stdout.strip()

            git_dir = _get_git_dir(backup_repo, nest_name)
            if not git_dir:
                return rollback_meta
            temp_index = f"{git_dir}/{_TEMP_INDEX_PREFIX}{uuid.uuid4().hex[:8]}"
            env = {"GIT_INDEX_FILE": temp_index}

            try:
                ToolBackupManager._sync_to_backup_repo(workdir, backup_repo, nest_name)
                for ext_fp in rollback_meta.get("external_files", []):
                    if _path_exists(ext_fp, nest_name):
                        ToolBackupManager._copy_external_file(ext_fp, backup_repo, nest_name)

                _run_git(["add", "-A"], cwd=backup_repo, env=env, nest_name=nest_name)
                result = _run_git(["write-tree"], cwd=backup_repo, env=env, nest_name=nest_name)
                if result.returncode != 0:
                    return rollback_meta
                current_tree = result.stdout.strip()
            finally:
                _cleanup_temp_index(temp_index, nest_name)

            if current_tree == backup_tree:
                logger.debug(f"非git检查: 无文件变化(commit={commit_hash[:8]})")
                return None

            result = _run_git(
                ["diff", "--name-status", backup_tree, current_tree],
                cwd=backup_repo,
                nest_name=nest_name,
            )
            changed_files: list[str] = []
            added_files: list[str] = []  # 快照后新增的文件（回退时需删除）
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if not line.strip():
                        continue
                    parts = line.split("\t", 1)
                    status = parts[0]
                    filepath = parts[1] if len(parts) > 1 else ""
                    if status.startswith("A"):
                        added_files.append(filepath)
                    else:
                        changed_files.append(filepath)

            rollback_meta["changed_files"] = changed_files + added_files
            rollback_meta["added_files"] = added_files
            rollback_meta["backup_tree"] = backup_tree  # 用于生成 diff 预览
            rollback_meta["current_tree"] = current_tree
            logger.info(
                f"非git检查: 检测到{len(changed_files)}个文件变化 "
                f"(commit={commit_hash[:8]}): {changed_files[:5]}"
            )
            return rollback_meta
        except Exception as e:
            logger.warning(f"非git变更检查失败: {e}", exc_info=True)
            return rollback_meta

    # ==================== git 工作目录快照 ====================

    @staticmethod
    def _snapshot_git(
        workdir: str,
        task_id: int,
        tool_name: str,
        tool_args: dict[str, Any],
        commit_msg: str,
        timestamp: str,
        nest_name: str | None = None,
    ) -> dict[str, Any] | None:
        """git工作目录快照：用临时索引创建镜像commit到备份分支（纯文件操作）"""
        # 🔧 远程nest：使用批量Python脚本，一次HTTP完成全部git操作
        if nest_name and nest_name != "local":
            return ToolBackupManager._snapshot_git_remote_batch(
                workdir, task_id, tool_name, tool_args, commit_msg, timestamp, nest_name
            )

        git_dir = _get_git_dir(workdir, nest_name)
        if not git_dir:
            logger.warning(f"无法获取git目录: {workdir}")
            return None

        branch_name = f"{BACKUP_BRANCH_PREFIX}{task_id}"
        temp_index = f"{git_dir}/{_TEMP_INDEX_PREFIX}{uuid.uuid4().hex[:8]}"
        env = {"GIT_INDEX_FILE": temp_index}

        try:
            _run_git(["read-tree", "HEAD"], cwd=workdir, env=env, nest_name=nest_name)
            _run_git(["add", "-A"], cwd=workdir, env=env, nest_name=nest_name)

            result = _run_git(["write-tree"], cwd=workdir, env=env, nest_name=nest_name)
            if result.returncode != 0:
                logger.warning(f"write-tree失败: {result.stderr}")
                return None
            tree_hash = result.stdout.strip()

            result = _run_git(
                ["rev-parse", "--verify", branch_name], cwd=workdir, nest_name=nest_name
            )
            reused = False
            if result.returncode == 0:
                parent = result.stdout.strip()
                # 🔧 去重优化：工作区tree与备份分支HEAD的tree相同时复用现有commit，
                # 避免无变化时产生仅parent/时间戳不同的冗余commit链
                result = _run_git(
                    ["rev-parse", f"{parent}^{{tree}}"], cwd=workdir, nest_name=nest_name
                )
                if result.returncode == 0 and result.stdout.strip() == tree_hash:
                    logger.debug(f"git快照去重: tree无变化，复用分支commit {parent[:8]}")
                    commit_hash = parent
                    reused = True
            else:
                result = _run_git(["rev-parse", "HEAD"], cwd=workdir, nest_name=nest_name)
                if result.returncode != 0:
                    logger.warning(f"无法获取HEAD: {result.stderr}")
                    return None
                parent = result.stdout.strip()

            if not reused:
                result = _run_git(
                    ["commit-tree", tree_hash, "-p", parent, "-m", commit_msg],
                    cwd=workdir,
                    nest_name=nest_name,
                )
                if result.returncode != 0:
                    logger.warning(f"commit-tree失败: {result.stderr}")
                    return None
                commit_hash = result.stdout.strip()

                _run_git(
                    ["update-ref", f"refs/heads/{branch_name}", commit_hash],
                    cwd=workdir,
                    nest_name=nest_name,
                )

            file_paths = ToolBackupManager._extract_file_paths(tool_name, tool_args, workdir)

            rollback_cmd = (
                f"git checkout {commit_hash[:8]} -- ."
                if not file_paths
                else f"git checkout {commit_hash[:8]} -- {' '.join(file_paths)}"
            )

            return {
                "type": "git",
                "task_id": task_id,
                "tool_name": tool_name,
                "commit_hash": commit_hash,
                "branch_name": branch_name,
                "workdir": str(workdir),
                "nest_name": nest_name or "local",
                "file_paths": file_paths,
                "timestamp": timestamp,
                "rollback_cmd": rollback_cmd,
            }
        finally:
            _cleanup_temp_index(temp_index, nest_name)

    @staticmethod
    def _snapshot_git_remote_batch(
        workdir: str,
        task_id: int,
        tool_name: str,
        tool_args: dict[str, Any],
        commit_msg: str,
        timestamp: str,
        nest_name: str,
    ) -> dict[str, Any] | None:
        """🔧 远程批量git快照：一次HTTP调用完成全部git操作

        将原来的9次HTTP请求（read-tree, add, write-tree, rev-parse×2,
        commit-tree, update-ref, 清理temp_index×2）合并为1次，
        大幅减少网络往返延迟（从~13秒降至~1.5秒）。
        """
        branch_name = f"{BACKUP_BRANCH_PREFIX}{task_id}"
        file_paths = ToolBackupManager._extract_file_paths(tool_name, tool_args, workdir)

        # 构建批量脚本：在远程nest上执行完整快照流程
        py_code = (
            "import subprocess, os, sys, json, uuid\n"
            f"workdir = {workdir!r}\n"
            f"branch_name = {branch_name!r}\n"
            f"commit_msg = {commit_msg!r}\n"
            f"temp_prefix = {f'peng_backup_idx_'!r}\n"
            f"result_data = {{}}\n"
            "try:\n"
            "    def git(*args, env=None):\n"
            "        full_env = dict(os.environ)\n"
            "        if env: full_env.update(env)\n"
            "        return subprocess.run(['git'] + list(args), cwd=workdir, env=full_env,\n"
            "                              capture_output=True, text=True, timeout=30)\n"
            "    # 获取git目录\n"
            "    r = git('rev-parse', '--git-dir')\n"
            "    if r.returncode != 0:\n"
            f"        result_data['error'] = 'rev-parse --git-dir failed: ' + r.stderr\n"
            "        print(json.dumps(result_data)); sys.exit(0)\n"
            "    git_dir = r.stdout.strip()\n"
            "    if not os.path.isabs(git_dir):\n"
            "        toplevel = git('rev-parse', '--show-toplevel')\n"
            "        if toplevel.returncode == 0:\n"
            "            git_dir = os.path.join(toplevel.stdout.strip().replace(chr(92), '/'), git_dir)\n"
            "    # 创建临时索引\n"
            "    temp_index = os.path.join(git_dir, temp_prefix + uuid.uuid4().hex[:8])\n"
            "    env_idx = {'GIT_INDEX_FILE': temp_index}\n"
            "    # read-tree + add + write-tree\n"
            "    git('read-tree', 'HEAD', env=env_idx)\n"
            "    r_add = git('add', '-A', env=env_idx)\n"
            "    # 🔧 修复 worktree 中 submodule .git 路径错误导致 git add -A 失败的问题\n"
            "    # worktree 中 submodule 的 .git 文件使用相对路径（如 ../../.git/modules/xxx），\n"
            "    # 从 worktree 子目录解析时指向不存在的路径，导致 fatal error。\n"
            "    # 解决方案：读取 .gitmodules 获取 submodule 路径，用 pathspec exclude 排除后重试\n"
            "    if r_add.returncode != 0:\n"
            "        r_gm = git('config', '-f', '.gitmodules', '--get-regexp', 'path')\n"
            "        submod_paths = [ln.split(' ', 1)[1] for ln in r_gm.stdout.strip().split(chr(10)) if ln.strip()]\n"
            "        if submod_paths:\n"
            "            excludes = ['.'] + [f':(exclude){p}' for p in submod_paths]\n"
            "            git('add', '-A', *excludes, env=env_idx)\n"
            "        else:\n"
            "            git('add', '-A', env=env_idx)  # 无 .gitmodules，重试原命令\n"
            "    r = git('write-tree', env=env_idx)\n"
            "    if r.returncode != 0:\n"
            f"        result_data['error'] = 'write-tree failed: ' + r.stderr\n"
            "        print(json.dumps(result_data)); sys.exit(0)\n"
            "    tree_hash = r.stdout.strip()\n"
            "    # 获取parent\n"
            "    r = git('rev-parse', '--verify', branch_name)\n"
            "    if r.returncode == 0:\n"
            "        parent = r.stdout.strip()\n"
            "    else:\n"
            "        r = git('rev-parse', 'HEAD')\n"
            "        if r.returncode != 0:\n"
            f"            result_data['error'] = 'rev-parse HEAD failed: ' + r.stderr\n"
            "            print(json.dumps(result_data)); sys.exit(0)\n"
            "        parent = r.stdout.strip()\n"
            "    # 🔧 去重优化：工作区tree与备份分支HEAD的tree相同时复用现有commit\n"
            "    reused = False\n"
            "    r = git('rev-parse', parent + '^{tree}')\n"
            "    if r.returncode == 0 and r.stdout.strip() == tree_hash:\n"
            f"        result_data['reused'] = True\n"
            "        commit_hash = parent\n"
            "        reused = True\n"
            "    if not reused:\n"
            "        # commit-tree\n"
            "        r = git('commit-tree', tree_hash, '-p', parent, '-m', commit_msg)\n"
            "        if r.returncode != 0:\n"
            f"            result_data['error'] = 'commit-tree failed: ' + r.stderr\n"
            "            print(json.dumps(result_data)); sys.exit(0)\n"
            "        commit_hash = r.stdout.strip()\n"
            "        # update-ref\n"
            "        git('update-ref', 'refs/heads/' + branch_name, commit_hash)\n"
            f"    result_data['commit_hash'] = commit_hash\n"
            f"    result_data['branch_name'] = branch_name\n"
            "except Exception as e:\n"
            f"    result_data['error'] = str(e)\n"
            "finally:\n"
            "    try:\n"
            "        if os.path.exists(temp_index): os.unlink(temp_index)\n"
            "    except: pass\n"
            "print(json.dumps(result_data))\n"
        )

        result = _run_python_remote(py_code, timeout=60, nest_name=nest_name)
        if result.returncode != 0:
            logger.warning(f"远程批量快照失败({nest_name}): {result.stderr[:200]}")
            return None

        try:
            data = json.loads(result.stdout.strip().split("\n")[-1])
        except (json.JSONDecodeError, IndexError):
            logger.warning(f"远程批量快照响应解析失败({nest_name}): {result.stdout[:200]}")
            return None

        if data.get("error"):
            logger.warning(f"远程批量快照git错误({nest_name}): {data['error'][:200]}")
            return None

        commit_hash = data.get("commit_hash", "")
        if not commit_hash:
            logger.warning(f"远程批量快照未返回commit_hash({nest_name})")
            return None

        rollback_cmd = (
            f"git checkout {commit_hash[:8]} -- ."
            if not file_paths
            else f"git checkout {commit_hash[:8]} -- {' '.join(file_paths)}"
        )

        return {
            "type": "git",
            "task_id": task_id,
            "tool_name": tool_name,
            "commit_hash": commit_hash,
            "branch_name": branch_name,
            "workdir": str(workdir),
            "nest_name": nest_name,
            "file_paths": file_paths,
            "timestamp": timestamp,
            "rollback_cmd": rollback_cmd,
        }

    # ==================== 非git 工作目录快照 ====================

    @staticmethod
    def _snapshot_non_git(
        workdir: str,
        task_id: int,
        tool_name: str,
        tool_args: dict[str, Any],
        commit_msg: str,
        timestamp: str,
        nest_name: str | None = None,
    ) -> dict[str, Any] | None:
        """非git工作目录快照：同步到备份git仓库（纯文件操作）"""
        backup_repo = ToolBackupManager._ensure_backup_repo(task_id, nest_name)
        if not backup_repo:
            return None

        try:
            all_paths = ToolBackupManager._extract_file_paths(tool_name, tool_args, workdir)
            file_paths: list[str] = []
            external_files: list[str] = []
            for fp in all_paths:
                fp_abs = fp if _is_abs_cross_platform(fp) else os.path.join(workdir, fp)
                fp_norm = os.path.normpath(fp_abs)
                wd_norm = os.path.normpath(workdir)
                if fp_norm.startswith(wd_norm):
                    rel = os.path.relpath(fp_norm, wd_norm)
                    file_paths.append(rel)
                else:
                    external_files.append(fp_norm)

            ToolBackupManager._sync_to_backup_repo(workdir, backup_repo, nest_name)

            for ext_fp in external_files:
                if _path_exists(ext_fp, nest_name):
                    ToolBackupManager._copy_external_file(ext_fp, backup_repo, nest_name)

            _run_git(["add", "-A"], cwd=backup_repo, nest_name=nest_name)
            _run_git(
                ["commit", "-m", commit_msg, "--allow-empty"], cwd=backup_repo, nest_name=nest_name
            )

            result = _run_git(["rev-parse", "HEAD"], cwd=backup_repo, nest_name=nest_name)
            if result.returncode != 0:
                logger.warning(f"无法获取备份仓库HEAD: {result.stderr}")
                return None
            commit_hash = result.stdout.strip()

            return {
                "type": "non_git",
                "task_id": task_id,
                "tool_name": tool_name,
                "commit_hash": commit_hash,
                "backup_repo": str(backup_repo),
                "workdir": str(workdir),
                "nest_name": nest_name or "local",
                "file_paths": file_paths,
                "external_files": external_files,
                "timestamp": timestamp,
                "rollback_cmd": "",  # 在 check_post_tool_changes 中更新
            }
        except Exception as e:
            logger.warning(f"非git快照失败: {e}", exc_info=True)
            return None

    @staticmethod
    def _ensure_backup_repo(task_id: int, nest_name: str | None = None) -> str | None:
        """确保备份git仓库存在并初始化

        位置优先级：
        1. 配置的 backups_dir/task#id（若配置目录在对应 nest 上存在）
        2. 系统临时目录 / .peng / backups / task#id（配置目录不存在或未配置时）

        跨平台说明：
        - 本地 nest：用本地文件系统检查配置目录
        - 远程 nest：通过远程探测检查配置目录是否存在（Linux服务端无法直接
          判断 Windows 远程路径，必须走 _path_exists 远程检查）
        """
        backup_base: str | None = None
        try:
            from peng.config.loader import load_config

            config = load_config()
            configured = config.paths.backups_dir_path
            if configured:
                configured_str = str(configured)
                if not nest_name or nest_name == "local":
                    # 本地：父目录存在即可用（backup_repo 由 _ensure_dir 自动创建）
                    if Path(configured_str).parent.exists():
                        backup_base = configured_str
                else:
                    # 远程 nest：跨平台探测配置目录是否存在（父目录存在即可）
                    if _path_exists(configured_str, nest_name):
                        backup_base = configured_str
                    else:
                        # 配置目录可能指向父目录尚不存在的路径，探测父目录
                        parent = Path(configured_str.replace("\\", "/")).parent
                        if _path_exists(str(parent), nest_name):
                            backup_base = configured_str
        except Exception:
            pass

        if not backup_base:
            # 配置目录不存在（或未配置）→ 使用系统临时目录
            tmp = _get_system_temp(nest_name)
            backup_base = f"{tmp}/.peng/backups"

        backup_repo = f"{backup_base}/task#{task_id}"

        try:
            if not _ensure_dir(backup_repo, nest_name):
                return None

            if not _path_exists(f"{backup_repo}/.git", nest_name):
                result = _run_git(["init"], cwd=backup_repo, nest_name=nest_name)
                if result.returncode != 0:
                    logger.warning(f"git init失败: {result.stderr}")
                    return None
                _run_git(
                    ["config", "user.email", "peng@backup"], cwd=backup_repo, nest_name=nest_name
                )
                _run_git(
                    ["config", "user.name", "Peng Backup"], cwd=backup_repo, nest_name=nest_name
                )
                _run_git(["add", "-A"], cwd=backup_repo, nest_name=nest_name)
                _run_git(
                    ["commit", "-m", f"init backup repo for task#{task_id}", "--allow-empty"],
                    cwd=backup_repo,
                    nest_name=nest_name,
                )

            if not nest_name or nest_name == "local":
                chown_to_project_owner(Path(backup_repo))
            return backup_repo
        except Exception as e:
            logger.warning(f"初始化备份仓库失败: {e}", exc_info=True)
            return None

    # ==================== 文件同步（本地/远程） ====================

    @staticmethod
    def _sync_to_backup_repo(workdir: str, backup_repo: str, nest_name: str | None = None) -> None:
        """同步工作目录文件到备份仓库（先清空备份仓库内容保留.git，再复制）"""
        if not nest_name or nest_name == "local":
            ToolBackupManager._sync_to_backup_repo_local(workdir, backup_repo)
        else:
            ToolBackupManager._sync_to_backup_repo_remote(workdir, backup_repo, nest_name)

    @staticmethod
    def _sync_to_backup_repo_local(workdir: str, backup_repo: str) -> None:
        """本地文件同步"""
        wd = Path(workdir)
        br = Path(backup_repo)
        # 清空备份仓库（保留.git）
        for item in br.iterdir():
            if item.name == ".git":
                continue
            if item.is_dir():
                shutil.rmtree(str(item))
            else:
                item.unlink()
        # 复制工作目录内容
        for item in wd.iterdir():
            if item.name in _EXCLUDE_DIRS:
                continue
            dst = br / item.name
            if item.is_dir():
                shutil.copytree(str(item), str(dst), dirs_exist_ok=True)
            else:
                shutil.copy2(str(item), str(dst))

    @staticmethod
    def _sync_to_backup_repo_remote(workdir: str, backup_repo: str, nest_name: str) -> None:
        """远程文件同步（跨平台，通过Python脚本）

        先清空备份仓库内容（保留.git），再复制工作目录内容。
        """
        excludes = sorted(_EXCLUDE_DIRS)
        code = (
            "import shutil, os\n"
            f"workdir = {workdir!r}\n"
            f"backup_repo = {backup_repo!r}\n"
            f"excludes = {excludes!r}\n"
            "# 清空备份仓库（保留.git）\n"
            "for item in os.listdir(backup_repo):\n"
            "    if item == '.git': continue\n"
            "    p = os.path.join(backup_repo, item)\n"
            "    if os.path.isdir(p): shutil.rmtree(p)\n"
            "    else: os.unlink(p)\n"
            "# 复制工作目录内容\n"
            "for item in os.listdir(workdir):\n"
            "    if item in excludes: continue\n"
            "    src = os.path.join(workdir, item)\n"
            "    dst = os.path.join(backup_repo, item)\n"
            "    if os.path.isdir(src): shutil.copytree(src, dst, dirs_exist_ok=True)\n"
            "    else: shutil.copy2(src, dst)\n"
        )
        _run_python_remote(code, timeout=60, nest_name=nest_name)

    @staticmethod
    def _sync_from_backup_repo(
        backup_repo: str, workdir: str, nest_name: str | None = None
    ) -> None:
        """从备份仓库同步文件到工作目录"""
        if not nest_name or nest_name == "local":
            br = Path(backup_repo)
            wd = Path(workdir)
            for item in br.iterdir():
                if item.name in _EXCLUDE_DIRS or item.name == ".git":
                    continue
                dst = wd / item.name
                if item.is_dir():
                    shutil.copytree(str(item), str(dst), dirs_exist_ok=True)
                else:
                    shutil.copy2(str(item), str(dst))
        else:
            excludes = sorted(_EXCLUDE_DIRS)
            code = (
                "import shutil, os\n"
                f"backup_repo = {backup_repo!r}\n"
                f"workdir = {workdir!r}\n"
                f"excludes = {excludes!r}\n"
                "for item in os.listdir(backup_repo):\n"
                "    if item in excludes or item == '.git': continue\n"
                "    src = os.path.join(backup_repo, item)\n"
                "    dst = os.path.join(workdir, item)\n"
                "    if os.path.isdir(src): shutil.copytree(src, dst, dirs_exist_ok=True)\n"
                "    else: shutil.copy2(src, dst)\n"
            )
            _run_python_remote(code, timeout=60, nest_name=nest_name)

    @staticmethod
    def _copy_external_file(src_path: str, backup_repo: str, nest_name: str | None = None) -> None:
        """复制外部文件到备份仓库的 __external__ 目录"""
        # 计算相对路径（去掉根目录分隔符）
        src_path_clean = src_path.replace("\\", "/")
        rel = src_path_clean.lstrip("/")
        dst = f"{backup_repo}/__external__/{rel}"

        if not nest_name or nest_name == "local":
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst)
        else:
            code = (
                "import shutil, os\n"
                f"src = {src_path!r}\n"
                f"dst = {dst!r}\n"
                "os.makedirs(os.path.dirname(dst), exist_ok=True)\n"
                "if os.path.isfile(src): shutil.copy2(src, dst)\n"
                "elif os.path.isdir(src): shutil.copytree(src, dst, dirs_exist_ok=True)\n"
            )
            _run_python_remote(code, timeout=10, nest_name=nest_name)

    @staticmethod
    def rollback(rollback_meta: dict[str, Any]) -> tuple[bool, str, dict | None, bool]:
        """执行回滚操作

        文件回滚（在 nest 上）和 history 恢复（在本地）完全独立。

        🔧 无变化快速路径：回滚前动态预检测全部恢复对象（工作区tree/外部文件/
        history/checklist），均无实际变化时不创建undo快照、不执行任何恢复操作，
        直接返回 changed=False（调用方据此跳过output记录与回滚链接生成）。

        Returns:
            (成功标志, 消息, undo_meta, 是否发生实际变化)
        """
        backup_type = rollback_meta.get("type", "")
        nest_name = rollback_meta.get("nest_name", "local")
        workdir = rollback_meta.get("workdir", "")
        task_id = rollback_meta.get("task_id", 0)
        tool_name = rollback_meta.get("tool_name", "")
        is_undo = tool_name.startswith("撤销:")
        is_iteration = tool_name == "iteration_start"

        try:
            # 0. 🔧 回滚预检测：动态检测文件/history/checklist/外部文件实际变化，
            #    并将检测到的精确变更清单合并进 rollback_meta
            has_actual_changes = ToolBackupManager._detect_rollback_changes(
                rollback_meta, workdir, nest_name, is_iteration
            )
            if not has_actual_changes:
                logger.info(
                    f"回滚跳过(task#{task_id}): 文件/history/checklist/外部文件均无实际变化"
                )
                return True, "无实际变化，跳过回滚", None, False

            # 1. 回滚前创建 undo 快照（文件+history，用于撤销）
            undo_meta = None
            if not is_undo:
                undo_meta = ToolBackupManager._create_undo_snapshot(
                    rollback_meta, workdir, task_id, nest_name
                )

            # 2. 文件回滚（在事务的 nest 上执行）
            if backup_type == "git":
                success, msg = ToolBackupManager._rollback_git(rollback_meta, nest_name)
            elif backup_type == "non_git":
                success, msg = ToolBackupManager._rollback_non_git(rollback_meta, nest_name)
            elif backup_type == "history_only":
                # 仅 history 备份成功的迭代快照：只恢复上下文，不动文件
                success, msg = True, "无文件快照，跳过文件回滚"
            else:
                return False, f"未知的备份类型: {backup_type}", None

            if success:
                # 3. history 恢复（在本地执行，与 nest 无关）
                history_commit = rollback_meta.get("history_commit")
                if history_commit:
                    ctx_msg = HistoryBackupManager.restore(
                        task_id, history_commit, trim_tool_call=not is_undo and not is_iteration
                    )
                    if ctx_msg:
                        msg += f"，{ctx_msg}"

                # 迭代快照：从外部文件基线 commit 全量恢复 workdir 外文件
                if is_iteration:
                    ext_head = rollback_meta.get("external_head_commit")
                    if ext_head:
                        ext_repo = ExternalFileBackup._get_backup_repo(task_id)
                        ext_restored = ExternalFileBackup.restore_all(
                            task_id, ext_repo, ext_head
                        )
                        if ext_restored > 0:
                            msg += f"，恢复 {ext_restored} 个外部文件"
                        elif ext_restored == 0:
                            logger.debug(
                                f"迭代回滚: 外部文件无变化或无备份(task#{task_id})"
                            )

                # 设置 undo_meta 的文件变更信息
                if undo_meta:
                    undo_meta["changed_files"] = rollback_meta.get("changed_files", [])
                    # 撤销操作不会删除文件（只会恢复），所以 added_files 为空
                    undo_meta["added_files"] = []
                    undo_commit = undo_meta.get("commit_hash", "")[:8]
                    changed = undo_meta["changed_files"]
                    undo_meta["rollback_cmd"] = (
                        f"git checkout {undo_commit} -- {' '.join(changed)}"
                        if backup_type == "git" and changed
                        else f"git checkout {undo_commit} -- ."
                        if backup_type == "git"
                        else f"从备份 {undo_meta.get('backup_repo', '')} 恢复"
                    )

            return success, msg, undo_meta if success else None, success
        except Exception as e:
            logger.error(f"回滚失败: {e}", exc_info=True)
            return False, f"回滚失败: {e}", None, False

    @staticmethod
    def _detect_rollback_changes(
        rollback_meta: dict[str, Any],
        workdir: str,
        nest_name: str | None,
        is_iteration: bool,
    ) -> bool:
        """回滚预检测：检测全部恢复对象是否有实际变化，并填充精确变更清单

        检测项（与回滚实际恢复的对象一一对应）：
        1. 工作区文件（git tree / 备份仓库 tree 对比快照 commit）→
           更新 changed_files/added_files/backup_tree/current_tree
        2. 外部文件：迭代快照对 external_head_commit 基线全量对比；
           工具快照对 external_backup_commit 对比 external_files 列表
        3. history + checklist：HistoryBackupManager.has_changes（剥离统计字段）

        检测失败时保守返回 True（宁可多回滚不可漏恢复）。

        Returns:
            True=至少一项有实际变化（需要执行回滚）；False=全部无变化
        """
        backup_type = rollback_meta.get("type", "")
        task_id = int(rollback_meta.get("task_id", 0) or 0)

        # 1. 工作区文件检测（git/non_git 均支持；检测结果合并进 rollback_meta）
        files_changed = False
        if backup_type in ("git", "non_git"):
            # 🔧 统一动态检测：工具快照与迭代快照均在回滚时检测精确变更清单
            # （链接生成时的旧检测已过时——后续轮次的新变更不在列表中）
            detected = ToolBackupManager.check_post_tool_changes(
                dict(rollback_meta), workdir, nest_name
            )
            if detected is not None:
                # 将检测到的变更合并到 rollback_meta（保留 history_commit 等字段）
                rollback_meta.update(detected)
                files_changed = True
            else:
                # 工作区相对快照无变化：仅保留上下文恢复能力
                rollback_meta["changed_files"] = []
                rollback_meta["added_files"] = []

        # 2. 外部文件检测（对快照记录的基线commit比较，而非HEAD——undo备份会移动HEAD）
        ext_changed_flag = False
        if is_iteration:
            ext_head = rollback_meta.get("external_head_commit")
            if ext_head:
                try:
                    ext_repo = ExternalFileBackup._get_backup_repo(task_id)
                    result = subprocess.run(
                        ["git", "ls-tree", "-r", "--name-only", ext_head],
                        cwd=ext_repo, capture_output=True, text=True, timeout=15,
                    )
                    if result.returncode == 0:
                        rel_paths = [ln.strip() for ln in result.stdout.split("\n") if ln.strip()]
                        abs_paths: list[str] = []
                        for rel in rel_paths:
                            if rel.startswith("__external__/"):
                                rel = rel[len("__external__/"):]
                            abs_path = rel if ntpath.isabs(rel) else "/" + rel
                            abs_paths.append(abs_path)
                        if abs_paths and ExternalFileBackup.check_changes(
                            task_id, abs_paths, ext_head
                        ):
                            ext_changed_flag = True
                except Exception as e:
                    logger.debug(f"外部文件基线检测失败(task#{task_id}): {e}")
        else:
            external_files = rollback_meta.get("external_files", [])
            ext_commit = rollback_meta.get("external_backup_commit")
            if external_files and ExternalFileBackup.check_changes(
                task_id, external_files, ext_commit
            ):
                ext_changed_flag = True

        # 3. history + checklist 检测
        history_changed = False
        history_commit = rollback_meta.get("history_commit")
        if history_commit and task_id:
            history_changed = HistoryBackupManager.has_changes(task_id, history_commit)

        return files_changed or ext_changed_flag or history_changed

    @staticmethod
    def _create_undo_snapshot(
        original_meta: dict[str, Any],
        workdir: str,
        task_id: int,
        nest_name: str | None,
    ) -> dict[str, Any] | None:
        """回滚前创建撤销快照（保存当前文件状态，在 nest 上执行）

        同时备份当前 history（在本地 data/history，通过 HistoryBackupManager）
        和外部文件（在本地 data/file_backups，通过 ExternalFileBackup）。
        """
        if not workdir:
            return None
        try:
            timestamp = datetime.now().isoformat(timespec="seconds")
            commit_msg = f"undo backup (before rollback)\n\nTask#{task_id} @ {timestamp}"

            # 文件快照（在 nest 上）
            if _is_git_repo_local(workdir, nest_name):
                meta = ToolBackupManager._snapshot_git(
                    workdir, task_id, "rollback_undo", {}, commit_msg, timestamp, nest_name
                )
            else:
                meta = ToolBackupManager._snapshot_non_git(
                    workdir, task_id, "rollback_undo", {}, commit_msg, timestamp, nest_name
                )

            if meta:
                meta["tool_name"] = f"撤销:{original_meta.get('tool_name', 'unknown')}"
                meta["timestamp"] = timestamp

                # 外部文件备份（在本地，与 nest 无关）
                original_external = original_meta.get("external_files", [])
                if original_external:
                    undo_ext_commit = ExternalFileBackup.backup(
                        task_id,
                        original_external,
                        f"undo:{original_meta.get('tool_name', 'unknown')} @ {timestamp}",
                    )
                    meta["external_files"] = original_external
                    meta["external_backup_commit"] = undo_ext_commit

                # history + checklist 备份（在本地，与文件备份完全独立）
                if task_id:
                    try:
                        from peng.task.manager import TaskManager

                        tm = TaskManager()
                        task = tm.db.get_task(task_id)
                        if task and task.get("context"):
                            undo_history_commit = HistoryBackupManager.backup(
                                task_id,
                                task["context"],
                                f"undo:{original_meta.get('tool_name', 'unknown')} @ {timestamp}",
                                task.get("checklist", "") or "",
                            )
                            if undo_history_commit:
                                meta["history_commit"] = undo_history_commit
                    except Exception as e:
                        logger.debug(f"undo history备份失败: {e}")

                return meta
        except Exception as e:
            logger.warning(f"创建撤销快照失败: {e}", exc_info=True)
            return None

    @staticmethod
    def _rollback_git(rollback_meta: dict[str, Any], nest_name: str | None) -> tuple[bool, str]:
        """从git备份分支回退

        对于修改的文件：git checkout <commit> -- <files>
        对于新增的文件（快照后创建的）：直接删除
        """
        commit_hash = rollback_meta["commit_hash"]
        workdir = rollback_meta["workdir"]
        # 优先使用 check_post_tool_changes 检测到的实际变更文件
        changed_files = rollback_meta.get("changed_files", [])
        added_files = rollback_meta.get("added_files", [])  # 快照后新增的文件

        # 回退修改/删除的文件（checkout from commit）
        checkout_files = [f for f in changed_files if f not in added_files]

        try:
            if checkout_files:
                result = _run_git(
                    ["checkout", commit_hash, "--"] + checkout_files,
                    cwd=workdir,
                    timeout=30,
                    nest_name=nest_name,
                )
                if result.returncode != 0:
                    # checkout可能失败如果文件在commit tree中不存在（如删除的文件）
                    # 尝试逐个checkout，跳过失败的
                    for fp in checkout_files:
                        _run_git(
                            ["checkout", commit_hash, "--", fp],
                            cwd=workdir,
                            timeout=10,
                            nest_name=nest_name,
                        )

            # 删除快照后新增的文件
            deleted_count = 0
            for fp in added_files:
                fp_full = os.path.join(workdir, fp)
                if _path_exists(fp_full, nest_name):
                    if not nest_name or nest_name == "local":
                        p = Path(fp_full)
                        if p.is_file():
                            p.unlink()
                        elif p.is_dir():
                            shutil.rmtree(str(p))
                    else:
                        code = (
                            "import shutil, os\n"
                            f"p = {fp_full!r}\n"
                            "if os.path.isfile(p): os.unlink(p)\n"
                            "elif os.path.isdir(p): shutil.rmtree(p)\n"
                        )
                        _run_python_remote(code, timeout=10, nest_name=nest_name)
                    deleted_count += 1

            total = len(checkout_files) + deleted_count
            msg = f"已回退 {total} 个文件到 {commit_hash[:8]}"
            if deleted_count:
                msg += f"（含删除 {deleted_count} 个新增文件）"

            # 恢复外部文件（在本地执行，与 nest 无关）
            external_files = rollback_meta.get("external_files", [])
            if external_files:
                task_id = rollback_meta.get("task_id", 0)
                ext_commit = rollback_meta.get("external_backup_commit")
                ext_restored = ExternalFileBackup.restore(task_id, external_files, ext_commit)
                if ext_restored:
                    msg += f"，恢复 {ext_restored} 个外部文件"

            logger.info(f"git回退成功: {msg}")
            return True, msg
        except Exception as e:
            return False, f"git回退异常: {e}"

    @staticmethod
    def _rollback_non_git(rollback_meta: dict[str, Any], nest_name: str | None) -> tuple[bool, str]:
        """从非git备份仓库回退

        changed_files 可能包含 workdir 内文件和 __external__/ 前缀的外部文件，
        需要分离处理。external_files 保存的是原始绝对路径，也需恢复。
        """
        backup_repo = rollback_meta["backup_repo"]
        workdir = rollback_meta["workdir"]
        commit_hash = rollback_meta["commit_hash"]
        all_changed = rollback_meta.get("changed_files", []) or rollback_meta.get("file_paths", [])
        added_files = rollback_meta.get("added_files", [])
        external_files = rollback_meta.get("external_files", [])

        # 分离 changed_files 中的 workdir 文件和外部文件
        wd_changed: list[str] = []
        for fp in all_changed:
            if fp.startswith("__external__/"):
                # 外部文件变更——由 external_files 统一处理，跳过
                continue
            wd_changed.append(fp)

        try:
            # 强制从commit恢复备份仓库工作树文件（因为 _check_changes_non_git 会修改工作树但不commit）
            _run_git(["checkout", commit_hash, "--", "."], cwd=backup_repo, nest_name=nest_name)

            restored_count = 0

            if wd_changed:
                for fp in wd_changed:
                    src = f"{backup_repo}/{fp}"
                    dst = f"{workdir}/{fp}"
                    if _path_exists(src, nest_name):
                        if not nest_name or nest_name == "local":
                            Path(dst).parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(src, dst)
                        else:
                            code = (
                                "import shutil, os\n"
                                f"src = {src!r}\n"
                                f"dst = {dst!r}\n"
                                "os.makedirs(os.path.dirname(dst), exist_ok=True)\n"
                                "shutil.copy2(src, dst)\n"
                            )
                            _run_python_remote(code, timeout=10, nest_name=nest_name)
                        restored_count += 1

            if external_files:
                # 优先从 backup_repo/__external__/ 恢复（nest端，支持远程）
                for ext_fp in external_files:
                    ext_clean = ext_fp.replace("\\", "/")
                    rel = ext_clean.lstrip("/")
                    src = f"{backup_repo}/__external__/{rel}"
                    if _path_exists(src, nest_name):
                        if not nest_name or nest_name == "local":
                            Path(ext_fp).parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(src, ext_fp)
                        else:
                            code = (
                                "import shutil, os\n"
                                f"src = {src!r}\n"
                                f"dst = {ext_fp!r}\n"
                                "os.makedirs(os.path.dirname(dst), exist_ok=True)\n"
                                "shutil.copy2(src, dst)\n"
                            )
                            _run_python_remote(code, timeout=10, nest_name=nest_name)
                        restored_count += 1

                # 补充：从本地 ExternalFileBackup 恢复（处理 undo 场景下
                # backup_repo/__external__/ 为空的情况）
                task_id_val = rollback_meta.get("task_id", 0)
                ext_commit = rollback_meta.get("external_backup_commit")
                if task_id_val and ext_commit:
                    ext_restored = ExternalFileBackup.restore(
                        task_id_val, external_files, ext_commit
                    )
                    if ext_restored:
                        restored_count = max(restored_count, ext_restored)

            if not wd_changed and not external_files and not added_files:
                ToolBackupManager._sync_from_backup_repo(backup_repo, workdir, nest_name)

            # 删除快照后新增的文件（这些文件在快照时不存在）
            deleted_count = 0
            for fp in added_files:
                if fp.startswith("__external__/"):
                    continue
                fp_full = os.path.join(workdir, fp)
                if _path_exists(fp_full, nest_name):
                    if not nest_name or nest_name == "local":
                        p = Path(fp_full)
                        if p.is_file():
                            p.unlink()
                        elif p.is_dir():
                            shutil.rmtree(str(p))
                    else:
                        code = (
                            "import shutil, os\n"
                            f"p = {fp_full!r}\n"
                            "if os.path.isfile(p): os.unlink(p)\n"
                            "elif os.path.isdir(p): shutil.rmtree(p)\n"
                        )
                        _run_python_remote(code, timeout=10, nest_name=nest_name)
                    deleted_count += 1

            total = restored_count + deleted_count
            msg = (
                f"已从备份恢复 {restored_count} 个文件"
                if restored_count > 0
                else f"已从备份恢复工作目录到 {commit_hash[:8]}"
            )
            if deleted_count:
                msg += f"，删除 {deleted_count} 个新增文件"
            logger.info(f"非git回退成功: {msg}")
            return True, msg
        except Exception as e:
            return False, f"非git回退异常: {e}"

    # ==================== 辅助方法 ====================

    @staticmethod
    def _extract_file_paths(
        tool_name: str, tool_args: dict[str, Any], workdir: str | None = None
    ) -> list[str]:
        """从工具参数中提取涉及的文件路径"""
        paths: list[str] = []
        if tool_name in ("write", "edit", "line_edit"):
            fp = tool_args.get("filePath", "")
            if fp:
                paths.append(fp)
        # bash: 无法确定具体文件，返回空列表
        return paths

    # ==================== 显示生成 ====================

    @staticmethod
    @staticmethod
    def generate_rollback_link(rollback_meta: dict[str, Any], text: str = "↩️ 回滚") -> str:
        """生成回滚超链接的ANSI OSC8序列（持久化回滚ID格式）

        🔧 回滚ID方案：链接生成时分配持久化回滚ID（纯 seq，如 "389"，写入
        meta["rollback_id"]）；URI 使用 rollback:id:<task_id>-<seq>（内部定位key），
        完整 meta 持久化到 data/rollback_ids/task-<id>/<seq>.json。
        显示文本保持简洁（不带ID）；回滚ID追加到 meta["hint"]，由 WebUI hover
        tooltip 展示（保留原有回滚信息）。

        Args:
            rollback_meta: 回滚元数据（持久化并分配ID）
            text: 超链接显示文本（默认"↩️ 回滚"，撤销链接用"↩️ 撤销"）
        """
        task_id = int(rollback_meta.get("task_id", 0) or 0)
        seq = str(rollback_meta.get("rollback_id") or allocate_rollback_id(task_id))
        rollback_meta["rollback_id"] = seq
        full_id = f"{task_id}-{seq}"

        # 回滚ID写入 hover hint（保留调用方已设置的回滚信息，如迭代快照范围描述）
        id_line = f"回滚ID: R{seq}（CLI: task rollback {task_id} R{seq}）"
        hint = rollback_meta.get("hint")
        if hint:
            if f"回滚ID: R{seq}" not in hint:
                rollback_meta["hint"] = f"{hint}\n{id_line}"
        else:
            rollback_meta["hint"] = id_line

        # 持久化完整meta（含hint；失败不阻塞链接生成，仅内存缓存可用）
        store_rollback_meta_persistent(rollback_meta, seq)

        uri = f"rollback:id:{full_id}"
        # 追加3个空格作为点击区域padding（绕过xterm OSC8链接鼠标响应区域偏移bug）
        # \x1b[33m=橙色（非粗体）
        return f"\x1b[33m\x1b]8;;{uri}\x07{text}   \x1b]8;;\x07\x1b[0m"


    @staticmethod
    def preview_rollback_diff(rollback_meta: dict[str, Any]) -> dict[str, Any]:
        """预览回滚将导致的文件变更（git diff形式，供确认对话框展示）

        动态计算：临时索引写入当前工作区tree，与快照commit的tree做diff。
        diff方向定义为"回滚后相对当前状态的变化"：
        - "-"行 = 回滚后将被移除的内容（当前存在、快照中不存在）
        - "+"行 = 回滚后将被恢复的内容（快照中存在、当前已改动/删除）

        diff 按 3MB 字节上限截断（末尾按完整行对齐），
        返回 truncated_bytes 提示未显示字节数。

        Args:
            rollback_meta: 回滚元数据（需含 commit_hash/workdir/nest_name）

        Returns:
            {"success": bool, "diff": str(纯文本diff，未着色), "changed_files": [...],
             "truncated": bool, "truncated_bytes": int(未显示字节数), "message": str}
        """
        backup_type = rollback_meta.get("type", "")
        nest_name = rollback_meta.get("nest_name", "local")
        workdir = rollback_meta.get("workdir", "")
        commit_hash = rollback_meta.get("commit_hash", "")

        if backup_type not in ("git", "non_git") or not commit_hash or not workdir:
            return {
                "success": False,
                "diff": "",
                "changed_files": [],
                "truncated": False,
                "message": f"备份类型 {backup_type or '未知'} 暂不支持diff预览"
                if backup_type != "history_only"
                else "仅上下文恢复，无文件变更",
            }

        # 确定diff执行目录：git→workdir本身；non_git→备份仓库（需先同步当前状态）
        if backup_type == "git":
            cwd = workdir
        else:
            cwd = rollback_meta.get("backup_repo", "")

        if not cwd:
            return {"success": False, "diff": "", "changed_files": [], "truncated": False,
                    "message": "缺少diff执行目录"}

        # 🔧 短时结果缓存：用户可能反复点击同一回滚链接查看预览，
        # 工作区无变化时跳过 add -A 全量扫描（大仓库并发构建时可耗时分钟级）。
        # TTL 内命中缓存直接返回；TTL 过期或 key 变化则重新计算。
        cache_key = f"{nest_name}|{backup_type}|{cwd}|{commit_hash}"
        now = time.time()
        cached = _preview_diff_cache.get(cache_key)
        if cached and now - cached["ts"] < _PREVIEW_DIFF_CACHE_TTL:
            return cached["result"]

        # 🔧 远程nest：批量脚本一次HTTP完成（含non_git先同步），
        # 输出base64传输，避免Windows GBK解码git UTF-8输出导致的静默失败
        if nest_name and nest_name != "local":
            result = ToolBackupManager._preview_rollback_diff_remote_batch(
                rollback_meta, backup_type, workdir, cwd, nest_name
            )
            _preview_diff_cache[cache_key] = {"ts": now, "result": result}
            if len(_preview_diff_cache) > 32:
                # 简单容量控制：删除最旧的一半
                for k in sorted(_preview_diff_cache, key=lambda k: _preview_diff_cache[k]["ts"])[:16]:
                    _preview_diff_cache.pop(k, None)
            return result

        try:
            if backup_type == "non_git":
                # 同步当前工作区到备份仓库（不commit），使工作树反映当前状态
                ToolBackupManager._sync_to_backup_repo(workdir, cwd, nest_name)

            git_dir = _get_git_dir(cwd, nest_name)
            if not git_dir:
                return {"success": False, "diff": "", "changed_files": [], "truncated": False,
                        "message": "无法获取git目录"}

            # 快照tree
            result = _run_git(
                ["rev-parse", f"{commit_hash}^{{tree}}"], cwd=cwd, nest_name=nest_name
            )
            if result.returncode != 0:
                return {"success": False, "diff": "", "changed_files": [], "truncated": False,
                        "message": f"快照commit不存在: {result.stderr[:100]}"}
            backup_tree = result.stdout.strip()

            # 当前工作区tree（临时索引）
            temp_index = f"{git_dir}/{_TEMP_INDEX_PREFIX}{uuid.uuid4().hex[:8]}"
            env = {"GIT_INDEX_FILE": temp_index}
            try:
                _run_git(["read-tree", "HEAD"], cwd=cwd, env=env, nest_name=nest_name)
                _run_git(["add", "-A"], cwd=cwd, env=env, nest_name=nest_name)
                result = _run_git(["write-tree"], cwd=cwd, env=env, nest_name=nest_name)
                if result.returncode != 0:
                    return {"success": False, "diff": "", "changed_files": [], "truncated": False,
                            "message": f"write-tree失败: {result.stderr[:100]}"}
                current_tree = result.stdout.strip()
            finally:
                _cleanup_temp_index(temp_index, nest_name)

            if current_tree == backup_tree:
                return {"success": True, "diff": "", "changed_files": [], "truncated": False,
                        "message": "无文件变更"}

            # 变更文件列表（name-status；quotepath=false防止中文文件名八进制转义）
            result = _run_git(
                ["-c", "core.quotepath=false", "diff", "--name-status", backup_tree, current_tree],
                cwd=cwd, nest_name=nest_name
            )
            changed_files: list[str] = []
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if not line.strip():
                        continue
                    parts = line.split("\t", 1)
                    if len(parts) > 1:
                        changed_files.append(parts[1].strip('"'))

            # 完整diff（current_tree→backup_tree = 回滚效果；quotepath=false同上）
            result = _run_git(
                ["-c", "core.quotepath=false", "diff", "--no-color", current_tree, backup_tree],
                cwd=cwd, nest_name=nest_name
            )
            if result.returncode != 0:
                # diff命令失败但tree确实不同：返回文件列表fallback，明确提示而非"ok"
                return {"success": True, "diff": "", "changed_files": changed_files,
                        "truncated": False,
                        "message": f"diff生成失败，共 {len(changed_files)} 个文件变更（见下方列表）"}
            diff_text = result.stdout

            # 按3MB字节截断（末尾按完整行对齐）
            diff_text, truncated, truncated_bytes = _truncate_diff_by_bytes(diff_text)

            result = {
                "success": True,
                "diff": diff_text,
                "changed_files": changed_files,
                "truncated": truncated,
                "truncated_bytes": truncated_bytes,
                "message": "ok",
            }
            _preview_diff_cache[cache_key] = {"ts": now, "result": result}
            return result
        except Exception as e:
            logger.warning(f"回滚diff预览失败: {e}", exc_info=True)
            return {"success": False, "diff": "", "changed_files": [], "truncated": False,
                    "message": f"预览异常: {e}"}

    @staticmethod
    def _preview_rollback_diff_remote_batch(
        rollback_meta: dict[str, Any],
        backup_type: str,
        workdir: str,
        cwd: str,
        nest_name: str,
    ) -> dict[str, Any]:
        """🔧 远程批量回滚diff预览：一次HTTP完成全部操作

        修复两个问题：
        1. 缓慢：原实现9次串行HTTP（每次100-500ms RTT）
        2. 只显示ok：_run_git_remote 的python包装用 text=True，Windows上以
           GBK解码git的UTF-8输出，中文内容触发UnicodeDecodeError → HTTP失败
           → diff静默为空。

        本方法将全部git操作打包为单个Python脚本在远程执行：
        - subprocess 用 bytes 模式捕获，base64编码后JSON返回，彻底避免编码问题
        - non_git 场景先同步工作区到备份仓库（同一脚本内完成）
        - diff失败时返回文件列表+明确错误信息（fallback），不再静默
        - diff 在远程端按3MB字节截断（完整行对齐），只传回有效部分
        """
        commit_hash = rollback_meta.get("commit_hash", "")

        py_code = (
            "import subprocess, os, sys, json, uuid, base64, shutil\n"
            f"workdir = {workdir!r}\n"
            f"cwd = {cwd!r}\n"
            f"commit_hash = {commit_hash!r}\n"
            f"is_non_git = {backup_type == 'non_git'!r}\n"
            f"excludes = {sorted(_EXCLUDE_DIRS)!r}\n"
            f"max_diff_bytes = {_DIFF_PREVIEW_MAX_BYTES!r}\n"
            "result_data = {}\n"
            "temp_index = None\n"
            "try:\n"
            "    def git(*args, env=None):\n"
            "        full_env = dict(os.environ)\n"
            "        if env: full_env.update(env)\n"
            "        return subprocess.run(['git'] + list(args), cwd=cwd, env=full_env,\n"
            "                              capture_output=True, timeout=120)  # bytes模式\n"
            "    # non_git：先同步当前工作区到备份仓库（清空保留.git，再复制）\n"
            "    if is_non_git:\n"
            "        for item in os.listdir(cwd):\n"
            "            if item == '.git': continue\n"
            "            p = os.path.join(cwd, item)\n"
            "            if os.path.isdir(p): shutil.rmtree(p)\n"
            "            else: os.unlink(p)\n"
            "        for item in os.listdir(workdir):\n"
            "            if item in excludes: continue\n"
            "            src = os.path.join(workdir, item)\n"
            "            dst = os.path.join(cwd, item)\n"
            "            if os.path.isdir(src): shutil.copytree(src, dst, dirs_exist_ok=True)\n"
            "            else: shutil.copy2(src, dst)\n"
            "    # 快照tree\n"
            "    r = git('rev-parse', commit_hash + '^{tree}')\n"
            "    if r.returncode != 0:\n"
            "        result_data['error'] = 'rev-parse commit failed: ' + r.stderr.decode('utf-8','replace')[:200]\n"
            "    else:\n"
            "        backup_tree = r.stdout.decode('ascii').strip()\n"
            "        # git目录与临时索引\n"
            "        r = git('rev-parse', '--git-dir')\n"
            "        git_dir = r.stdout.decode('utf-8','replace').strip()\n"
            "        if not os.path.isabs(git_dir):\n"
            "            t = git('rev-parse', '--show-toplevel')\n"
            "            if t.returncode == 0:\n"
            "                git_dir = os.path.join(t.stdout.decode('utf-8','replace').strip().replace(chr(92), '/'), git_dir)\n"
            "        temp_index = os.path.join(git_dir, 'peng_preview_idx_' + uuid.uuid4().hex[:8])\n"
            "        env_idx = {'GIT_INDEX_FILE': temp_index}\n"
            "        git('read-tree', 'HEAD', env=env_idx)\n"
            "        git('add', '-A', env=env_idx)\n"
            "        r = git('write-tree', env=env_idx)\n"
            "        if r.returncode != 0:\n"
            "            result_data['error'] = 'write-tree failed: ' + r.stderr.decode('utf-8','replace')[:200]\n"
            "        else:\n"
            "            current_tree = r.stdout.decode('ascii').strip()\n"
            "            if current_tree == backup_tree:\n"
            "                result_data['no_change'] = True\n"
            "            else:\n"
            "                # 变更文件列表（base64传输防编码问题；quotepath=false防中文文件名转义）\n"
            "                r = git('-c', 'core.quotepath=false', 'diff', '--name-status', backup_tree, current_tree)\n"
            "                result_data['changed_files_b64'] = base64.b64encode(r.stdout).decode('ascii')\n"
            "                # 完整diff（current→backup = 回滚效果）\n"
            "                r = git('-c', 'core.quotepath=false', 'diff', '--no-color', current_tree, backup_tree)\n"
            "                if r.returncode != 0:\n"
            "                    result_data['diff_error'] = r.stderr.decode('utf-8','replace')[:200]\n"
            "                else:\n"
            "                    # 远程端按字节截断（完整行对齐），只传回有效部分\n"
            "                    total = len(r.stdout)\n"
            "                    if total > max_diff_bytes:\n"
            "                        cut = r.stdout.rfind(b'\\n', 0, max_diff_bytes + 1)\n"
            "                        if cut < 0: cut = max_diff_bytes\n"
            "                        result_data['truncated_bytes'] = total - cut\n"
            "                        result_data['diff_b64'] = base64.b64encode(r.stdout[:cut]).decode('ascii')\n"
            "                    else:\n"
            "                        result_data['diff_b64'] = base64.b64encode(r.stdout).decode('ascii')\n"
            "except Exception as e:\n"
            "    result_data['error'] = 'exception: ' + str(e)[:300]\n"
            "finally:\n"
            "    try:\n"
            "        if temp_index and os.path.exists(temp_index): os.unlink(temp_index)\n"
            "    except Exception: pass\n"
            "print(json.dumps(result_data))\n"
        )

        result = _run_python_remote(py_code, timeout=180, nest_name=nest_name)
        if result.returncode != 0:
            logger.warning(f"远程批量diff预览失败({nest_name}): {result.stderr[:200]}")
            return {"success": False, "diff": "", "changed_files": [], "truncated": False,
                    "message": f"远程执行失败: {result.stderr[:150]}"}

        try:
            data = json.loads(result.stdout.strip().split("\n")[-1])
        except (json.JSONDecodeError, IndexError):
            logger.warning(f"远程批量diff预览响应解析失败({nest_name}): {result.stdout[:200]}")
            return {"success": False, "diff": "", "changed_files": [], "truncated": False,
                    "message": "响应解析失败"}

        if data.get("error"):
            logger.warning(f"远程批量diff预览git错误({nest_name}): {data['error'][:200]}")
            return {"success": False, "diff": "", "changed_files": [], "truncated": False,
                    "message": f"git错误: {data['error'][:150]}"}

        if data.get("no_change"):
            return {"success": True, "diff": "", "changed_files": [], "truncated": False,
                    "message": "无文件变更"}

        # 解码变更文件列表
        changed_files: list[str] = []
        cf_b64 = data.get("changed_files_b64", "")
        if cf_b64:
            try:
                cf_text = base64.b64decode(cf_b64).decode("utf-8", "replace")
                for line in cf_text.strip().split("\n"):
                    if not line.strip():
                        continue
                    parts = line.split("\t", 1)
                    if len(parts) > 1:
                        changed_files.append(parts[1])
            except Exception as e:
                logger.debug(f"changed_files解码失败: {e}")

        if data.get("diff_error") or not data.get("diff_b64"):
            # diff失败但tree不同：返回文件列表fallback，明确提示而非"ok"
            return {"success": True, "diff": "", "changed_files": changed_files,
                    "truncated": False,
                    "message": (f"diff生成失败({str(data.get('diff_error', '空输出'))[:100]})，"
                                f"共 {len(changed_files)} 个文件变更（见列表）")}

        try:
            diff_text = base64.b64decode(data["diff_b64"]).decode("utf-8", "replace")
        except Exception as e:
            return {"success": True, "diff": "", "changed_files": changed_files,
                    "truncated": False, "truncated_bytes": 0, "message": f"diff解码失败: {e}"}

        truncated = bool(data.get("truncated_bytes"))
        truncated_bytes = int(data.get("truncated_bytes") or 0)

        return {
            "success": True,
            "diff": diff_text,
            "changed_files": changed_files,
            "truncated": truncated,
            "truncated_bytes": truncated_bytes,
            "message": "ok",
        }


    @staticmethod
    def generate_resume_link(task_id: int) -> str:
        """生成"继续处理"事务超链接的ANSI OSC8序列

        回滚/撤销操作暂停运行中的事务后，输出提示中附带此链接，
        点击后前端调用 /api/tasks/{id}/action resume 恢复事务。

        Args:
            task_id: 事务ID（编码到URI中）
        """
        uri = f"taskresume:{int(task_id)}"
        # 追加3个空格作为点击区域padding（绕过xterm OSC8链接鼠标响应区域偏移bug）
        # \x1b[92m=亮绿色高亮（区别于回滚链接的橙色）
        return f"\x1b[92m\x1b]8;;{uri}\x07▶️ 直接继续处理   \x1b]8;;\x07\x1b[0m"

    @staticmethod
    def generate_rollback_record(
        rollback_meta: dict[str, Any],
        undo_meta: dict[str, Any] | None,
        message: str,
        was_running: bool = False,
    ) -> str:
        """生成回退操作记录文本（写入OutputManager，显示在xterm）

        包含：
        - 回退结果摘要
        - 变更文件列表
        - 暂停提示+"继续处理"超链接（was_running=True时，回滚前事务因回滚操作被暂停）
        - "撤销回退"超链接（如果有undo_meta）

        Args:
            rollback_meta: 原始回退元数据
            undo_meta: 撤销回退用的元数据（None表示无法撤销）
            message: 回退结果消息
            was_running: 回滚前事务是否处于运行状态（是则本次回滚暂停了它）

        Returns:
            ANSI格式文本（写入OutputManager后自动显示在xterm）
        """
        from peng.common.colors import get_colors

        colors = get_colors()
        result_color = colors.agent("result")

        tool_name = rollback_meta.get("tool_name", "")
        is_undo = tool_name.startswith("撤销:")
        title = "↩️ 撤销回滚" if is_undo else "↩️ 工具回滚"

        lines = [
            f"\x1b[1m[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {title}执行\x1b[0m",
            f"  {message}",
        ]

        changed_files = rollback_meta.get("changed_files", [])
        if changed_files:
            display_files = changed_files[:10]
            for f in display_files:
                lines.append(f"  \x1b[90m• {f}\x1b[0m")
            if len(changed_files) > 10:
                lines.append(f"  \x1b[90m• ... 共 {len(changed_files)} 个文件\x1b[0m")

        # 撤销超链接（含快照时间戳，去掉ISO格式中的T分隔符）
        if undo_meta:
            undo_ts = undo_meta.get("timestamp", "").replace("T", " ")
            undo_link = ToolBackupManager.generate_rollback_link(
                undo_meta, text=f"↩️ 撤销至 快照[{undo_ts}]"
            )
            lines.append(f"  {undo_link}")

        # 暂停提示（黄色高亮）+ "直接继续处理"超链接（绿色高亮，点击恢复事务运行），
        # 显示在撤销超链接下面一行
        if was_running and rollback_meta.get("task_id"):
            resume_link = ToolBackupManager.generate_resume_link(
                rollback_meta["task_id"]
            )
            pause_msg = (
                f"\x1b[93m⏸️ 事务#{rollback_meta['task_id']}已暂停，"
                f"请输入新的指示以避免重复输出被回滚内容。\x1b[0m"
            )
            lines.append(f"  {pause_msg} {resume_link}")

        return "\n".join(lines)


# ==================== 模块级辅助函数 ====================


def _is_git_repo_local(workdir: str, nest_name: str | None = None) -> bool:
    """检查目录是否是 git 仓库（支持远程）"""
    if not nest_name or nest_name == "local":
        return _is_git_repo(Path(workdir))
    result = _run_git(
        ["rev-parse", "--is-inside-work-tree"],
        cwd=workdir,
        timeout=5,
        nest_name=nest_name,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _cleanup_temp_index(temp_index: str, nest_name: str | None = None) -> None:
    """清理临时索引文件（跨平台）"""
    if not nest_name or nest_name == "local":
        try:
            os.unlink(temp_index)
        except OSError:
            pass
    else:
        code = f"import os; os.unlink({temp_index!r}) if os.path.exists({temp_index!r}) else None"
        _run_python_remote(code, timeout=5, nest_name=nest_name)


# ==================== History 备份/恢复（纯本地，与文件备份完全独立） ====================

# history 备份目录（固定在本地 peng 服务的 data/history 目录）
_HISTORY_REPO_DIR = None  # 动态获取，见 _get_history_repo


class HistoryBackupManager:
    """History context 备份管理器

    完全独立于文件备份（ToolBackupManager）：
    - 始终在本地（peng服务端）执行，不涉及远程 nest
    - 备份目录固定为 peng 服务的 data/history 目录
    - 每次快照独立 commit，支持多版本
    - 操作对象是 task db 的 context 字段
    """

    @staticmethod
    def _get_history_base() -> str:
        """获取 history 备份根目录（本地 data/history）"""
        try:
            from peng.config.loader import load_config

            config = load_config()
            base = str(config.paths.data_dir_path / "history")
        except Exception:
            base = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "history")
        Path(base).mkdir(parents=True, exist_ok=True)
        return os.path.abspath(base)

    @staticmethod
    def _get_history_repo(task_id: int) -> str:
        """获取/创建 history 备份仓库路径（本地 data/history/task#id）"""
        repo_path = os.path.join(HistoryBackupManager._get_history_base(), f"task#{task_id}")
        Path(repo_path).mkdir(parents=True, exist_ok=True)

        if not os.path.exists(os.path.join(repo_path, ".git")):
            subprocess.run(["git", "init"], cwd=repo_path, capture_output=True, timeout=10)
            subprocess.run(
                ["git", "config", "user.email", "peng@history"], cwd=repo_path, capture_output=True
            )
            subprocess.run(
                ["git", "config", "user.name", "Peng History"], cwd=repo_path, capture_output=True
            )
            Path(os.path.join(repo_path, "context.json")).write_text("{}", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=repo_path, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "init", "--allow-empty"], cwd=repo_path, capture_output=True
            )

        return repo_path

    @staticmethod
    def backup(task_id: int, history_context: str, label: str = "", checklist: str = "") -> str | None:
        """备份 history context 和 checklist 到本地 git 仓库

        🔧 去重优化：
        - context.json 只写入剥离统计字段后的 messages（统计字段每次LLM调用
          必变，写入只会制造无意义commit；restore 时会从当前task保留最新统计）
        - 无实际变化（messages+checklist 与 HEAD 相同）时直接复用 HEAD commit，
          不再强制 --allow-empty 产生冗余commit

        Args:
            task_id: 事务ID
            history_context: history JSON 字符串
            label: 标签（如工具名+时间戳）
            checklist: checklist 内容字符串

        Returns:
            commit hash，失败返回 None
        """
        if not history_context and not checklist:
            return None

        try:
            repo = HistoryBackupManager._get_history_repo(task_id)
            if history_context:
                # 🔧 剥离统计字段：只备份 messages 内容，统计数值变化不产生新commit
                Path(os.path.join(repo, "context.json")).write_text(
                    strip_context_stats(history_context), encoding="utf-8"
                )
            # 始终写入 checklist.md（即使是空字符串），确保回滚时能正确恢复到"无checklist"状态
            # 不删除文件，否则 git show 会找不到文件导致 restore 跳过恢复
            Path(os.path.join(repo, "checklist.md")).write_text(
                checklist or "", encoding="utf-8"
            )

            # 🔧 去重检测：messages+checklist 与 HEAD commit 内容相同时复用 HEAD，
            # 避免空转/重试轮次产生无意义的空 commit（回滚链无差别指向同一状态）
            if HistoryBackupManager._worktree_matches_head(repo, history_context, checklist):
                result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=repo, capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    logger.debug(f"history备份去重: 内容无变化，复用HEAD {result.stdout.strip()[:8]}")
                    return result.stdout.strip()

            subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, timeout=10)

            timestamp = datetime.now().isoformat(timespec="seconds")
            msg = f"history backup: {label}\n\nTask#{task_id} @ {timestamp}"
            result = subprocess.run(
                ["git", "commit", "-m", msg],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=10,
            )

            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                commit_hash = result.stdout.strip()
                logger.debug(f"History备份成功 task#{task_id} commit={commit_hash[:8]}")
                return commit_hash
        except Exception as e:
            logger.warning(f"History备份失败 task#{task_id}: {e}")
        return None

    @staticmethod
    def _worktree_matches_head(
        repo: str, history_context: str, checklist: str
    ) -> bool:
        """检测备份工作树当前内容（已写入待提交的 context.json/checklist.md）
        是否与 HEAD commit 完全一致（用于 backup 去重复用 HEAD）。

        比较 blob hash（git hash-object），与文件在仓库中的路径无关：
        - context.json：与 HEAD:context.json 的 messages 内容比（均剥离统计字段，
          由调用方保证写入时已剥离，此处对传入 history_context 再剥离一次以幂等）
        - checklist.md：与 HEAD:checklist.md 比（旧commit可能无此文件→视为不同）
        """
        try:
            ctx_rel = os.path.join(repo, "context.json")
            cl_rel = os.path.join(repo, "checklist.md")

            if not os.path.exists(ctx_rel) or not os.path.exists(cl_rel):
                return False

            # context.json 工作树内容 vs HEAD blob（均剥离统计字段后比较）
            r = subprocess.run(
                ["git", "hash-object", "--stdin"],
                input=strip_context_stats(history_context),
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return False
            r2 = subprocess.run(
                ["git", "rev-parse", "HEAD:context.json"],
                cwd=repo, capture_output=True, text=True, timeout=5,
            )
            if r2.returncode != 0 or r2.stdout.strip() != r.stdout.strip():
                return False

            # checklist.md 工作树内容 vs HEAD blob
            r = subprocess.run(
                ["git", "hash-object", cl_rel],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return False
            r2 = subprocess.run(
                ["git", "rev-parse", "HEAD:checklist.md"],
                cwd=repo, capture_output=True, text=True, timeout=5,
            )
            if r2.returncode != 0 or r2.stdout.strip() != r.stdout.strip():
                return False
            return True
        except Exception:
            return False

    @staticmethod
    def has_changes(task_id: int, history_commit: str) -> bool:
        """检测当前 task db 的 history/checklist 相对指定备份 commit 是否有实际变化

        回滚预检测使用：messages 与 checklist 均与备份时相同 → 回滚无意义。
        比较时剥离统计字段（_prompt_tokens 等），只看实质内容。

        Returns:
            True=有实质变化（回滚会改变状态）；False=无变化或检测失败时保守返回True
        """
        if not task_id or not history_commit:
            return False
        try:
            repo = HistoryBackupManager._get_history_repo(task_id)

            # 备份 commit 中的 context messages
            r = subprocess.run(
                ["git", "show", f"{history_commit}:context.json"],
                cwd=repo, capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return True  # 无备份内容，视为有变化（保守）
            backup_messages = strip_context_stats(r.stdout)

            # 备份 commit 中的 checklist
            r = subprocess.run(
                ["git", "show", f"{history_commit}:checklist.md"],
                cwd=repo, capture_output=True, text=True, timeout=10,
            )
            backup_checklist = r.stdout if r.returncode == 0 else None

            # 当前 task db 的 history/checklist
            from peng.task.manager import TaskManager

            tm = TaskManager()
            task = tm.db.get_task(task_id)
            if not task:
                return True
            current_messages = (
                strip_context_stats(task.get("context") or "")
                if task.get("context")
                else None
            )
            current_checklist = task.get("checklist", "") or ""

            # messages 比较（备份commit必有context.json；当前无context视为有变化）
            if current_messages is None:
                return True
            if backup_messages != current_messages:
                return True

            # checklist 比较（备份无checklist.md时：当前非空即有变化）
            if backup_checklist is None:
                return bool(current_checklist)
            return backup_checklist != current_checklist
        except Exception as e:
            logger.debug(f"history变更检测失败(task#{task_id}): {e}")
            return True  # 检测异常保守视为有变化

    @staticmethod
    def restore(
        task_id: int,
        history_commit: str,
        trim_tool_call: bool = False,
    ) -> str:
        """从本地 git 仓库恢复 history context 到 task db

        Args:
            task_id: 事务ID
            history_commit: 备份 commit hash
            trim_tool_call: True=删除末尾工具调用+结果

        Returns:
            操作描述消息
        """
        if not task_id or not history_commit:
            return ""

        try:
            repo = HistoryBackupManager._get_history_repo(task_id)

            # 从 commit 读取 history 文件
            result = subprocess.run(
                ["git", "show", f"{history_commit}:context.json"],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.debug(f"History恢复失败: commit {history_commit[:8]} 无 context.json")
                return ""

            history_json = result.stdout
            data = json.loads(history_json)
            messages = data.get("messages", []) if isinstance(data, dict) else data

            if trim_tool_call:
                while messages:
                    last = messages[-1]
                    if last.get("role") == "tool":
                        messages.pop()
                        continue
                    if last.get("role") == "assistant" and "tool_calls" in last:
                        messages.pop()
                        break
                    if (
                        last.get("role") == "assistant"
                        and not last.get("content")
                        and "tool_calls" not in last
                        and not last.get("reasoning_content")
                    ):
                        messages.pop()
                        continue
                    break

            # 读取当前统计信息（token/费用保留最新值）
            from peng.task.manager import TaskManager

            tm = TaskManager()
            task = tm.db.get_task(task_id)
            current_stats = {}
            if task and task.get("context"):
                try:
                    cur_data = json.loads(task["context"])
                    if isinstance(cur_data, dict):
                        for k in (
                            "_prompt_tokens",
                            "_total_tokens_consumed",
                            "_total_cost",
                            "_total_input_hit_tokens",
                            "_total_input_miss_tokens",
                            "_total_output_tokens",
                            "_pricing",
                            "_cost_breakdown",
                        ):
                            if k in cur_data:
                                current_stats[k] = cur_data[k]
                except Exception:
                    pass

            # 合并：备份的messages + 当前统计
            result_data = data if isinstance(data, dict) else {"messages": data}
            result_data["messages"] = messages
            result_data.update(current_stats)
            new_context = json.dumps(result_data, ensure_ascii=False, indent=2)
            tm.db.update_task(task_id, context=new_context)

            # 恢复 checklist
            # 注意：checklist.md 始终存在（backup时即使空字符串也写入文件）
            # 条件仅检查 returncode==0（旧commit可能无此文件），不检查 stdout 是否为空
            checklist_msg = ""
            cl_result = subprocess.run(
                ["git", "show", f"{history_commit}:checklist.md"],
                cwd=repo, capture_output=True, text=True, timeout=10,
            )
            if cl_result.returncode == 0:
                tm.db.update_task(task_id, checklist=cl_result.stdout)
                checklist_msg = "，checklist已恢复"
            else:
                # 旧commit可能没有checklist.md（功能添加前），保留当前值不变
                logger.debug(f"History恢复: commit {history_commit[:8]} 无 checklist.md")

            action = "移除末尾工具调用" if trim_tool_call else "完整恢复"
            logger.info(f"事务#{task_id} History已恢复（{action}）from {history_commit[:8]}")
            return f"上下文已恢复（{action}）{checklist_msg}"

        except Exception as e:
            logger.warning(f"History恢复失败 task#{task_id}: {e}", exc_info=True)
            return ""

    @staticmethod
    def preview_checklist_diff(
        task_id: int,
        history_commit: str,
        snapshot_label: str = "",
    ) -> dict[str, Any]:
        """预览回滚将导致的 checklist 变化（供回滚确认窗口展示）

        对比当前 task db 中的 checklist 与 history 备份 commit 中的快照，
        返回 unified diff。diff 方向与文件 diff 一致（"回滚后相对当前状态的变化"）：
        - "-"行 = 回滚后将被移除的 checklist 内容（当前有、快照无）
        - "+"行 = 回滚后将被恢复的 checklist 内容（快照有、当前无/已改）

        diff 按 3MB 字节上限截断（末尾按完整行对齐），
        返回 truncated_bytes 提示未显示字节数。

        Args:
            task_id: 事务ID
            history_commit: history 备份 commit hash（rollback_meta.history_commit）
            snapshot_label: 快照描述（如 "迭代#3 快照[2026-08-16 02:45:15]"，用于diff头部）

        Returns:
            {"available": bool,  # 是否可计算（有快照备份且能读取）
             "changed": bool,    # checklist 是否有实际变化
             "diff": str,        # unified diff 文本（无变化时为空）
             "message": str}     # 不可用/无变化时的说明
        """
        if not task_id or not history_commit:
            return {
                "available": False,
                "changed": False,
                "diff": "",
                "message": "快照未包含 checklist 备份",
            }

        try:
            repo = HistoryBackupManager._get_history_repo(task_id)

            # 读取快照 checklist（bytes + utf-8 解码，避免 Windows GBK 解码问题；
            # returncode != 0 表示旧 commit 无 checklist.md，此时快照状态视为空字符串，
            # 与 backup "始终写入 checklist.md（即使空）" 的行为一致）
            result = subprocess.run(
                ["git", "show", f"{history_commit}:checklist.md"],
                cwd=repo,
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0:
                snapshot_cl = ""
            else:
                snapshot_cl = result.stdout.decode("utf-8", errors="replace")

            # 读取当前 checklist（DB；事务不存在或读取失败时按空处理，diff 仍可计算）
            current_cl = ""
            try:
                from peng.task.manager import TaskManager

                tm = TaskManager()
                task = tm.db.get_task(task_id)
                if task:
                    current_cl = task.get("checklist", "") or ""
            except Exception as e:
                logger.debug(f"checklist预览: 读取当前checklist失败 task#{task_id}: {e}")

            # 短路：完全相同 → 无变化
            if snapshot_cl == current_cl:
                return {
                    "available": True,
                    "changed": False,
                    "diff": "",
                    "message": "checklist 无变化",
                }

            import difflib

            label = snapshot_label or f"快照[{history_commit[:8]}]"
            diff_lines = list(
                difflib.unified_diff(
                    current_cl.splitlines(keepends=True),
                    snapshot_cl.splitlines(keepends=True),
                    fromfile=f"checklist（当前）",
                    tofile=f"checklist（回滚后 ← {label}）",
                )
            )
            # unified_diff 输出行均带 \n，仅末行可能缺失
            diff_text = "\n".join(line.rstrip("\n") for line in diff_lines)

            # 按3MB字节截断（末尾按完整行对齐）
            diff_text, truncated, truncated_bytes = _truncate_diff_by_bytes(diff_text)

            return {
                "available": True,
                "changed": True,
                "diff": diff_text,
                "truncated": truncated,
                "truncated_bytes": truncated_bytes,
                "message": "ok",
            }
        except Exception as e:
            logger.warning(f"checklist diff预览失败 task#{task_id}: {e}", exc_info=True)
            return {
                "available": False,
                "changed": False,
                "diff": "",
                "message": f"预览异常: {e}",
            }


# ==================== 外部文件备份（workdir外的文件，纯本地） ====================


class ExternalFileBackup:
    """外部文件（工作目录外的文件）备份管理器

    完全独立于文件备份（ToolBackupManager）和 history 备份（HistoryBackupManager）：
    - 始终在本地（peng服务端）执行
    - 备份目录固定为 peng 服务的 data/file_backups/task#id
    - 每次备份使用 git commit，支持多版本
    - 用于 git 场景下工作目录外的文件变更备份
    """

    @staticmethod
    def _get_backup_repo(task_id: int) -> str:
        """获取/创建外部文件备份仓库路径（本地 data/file_backups/task#id）"""
        try:
            from peng.config.loader import load_config

            config = load_config()
            base = str(config.paths.data_dir_path / "file_backups")
        except Exception:
            base = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "file_backups")
        repo_path = os.path.join(base, f"task#{task_id}")
        Path(repo_path).mkdir(parents=True, exist_ok=True)

        if not os.path.exists(os.path.join(repo_path, ".git")):
            subprocess.run(["git", "init"], cwd=repo_path, capture_output=True, timeout=10)
            subprocess.run(
                ["git", "config", "user.email", "peng@files"], cwd=repo_path, capture_output=True
            )
            subprocess.run(
                ["git", "config", "user.name", "Peng Files"], cwd=repo_path, capture_output=True
            )
            subprocess.run(["git", "add", "-A"], cwd=repo_path, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "init", "--allow-empty"], cwd=repo_path, capture_output=True
            )

        return repo_path

    @staticmethod
    def _to_repo_path(file_path: str) -> str:
        """将绝对文件路径转换为备份仓库内的相对路径（去掉根分隔符）"""
        clean = file_path.replace("\\", "/")
        return clean.lstrip("/")

    @staticmethod
    def backup(task_id: int, file_paths: list[str], label: str = "") -> str | None:
        """备份外部文件到本地 git 仓库

        Args:
            task_id: 事务ID
            file_paths: 外部文件绝对路径列表
            label: 备份标签

        Returns:
            commit hash，失败返回 None
        """
        if not file_paths:
            return None

        try:
            repo = ExternalFileBackup._get_backup_repo(task_id)
            backed_up = 0
            for fp in file_paths:
                if not os.path.exists(fp):
                    continue
                rel = ExternalFileBackup._to_repo_path(fp)
                dst = os.path.join(repo, rel)
                Path(os.path.dirname(dst)).mkdir(parents=True, exist_ok=True)
                if os.path.isfile(fp):
                    shutil.copy2(fp, dst)
                elif os.path.isdir(fp):
                    shutil.copytree(fp, dst, dirs_exist_ok=True)
                backed_up += 1

            if backed_up == 0:
                return None

            # 🔧 去重检测：文件内容与 HEAD 完全一致时复用 HEAD commit，
            # 避免无变化时产生冗余空 commit（--allow-empty 已移除）
            changed = ExternalFileBackup.check_changes(task_id, file_paths)
            if not changed:
                result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=repo, capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    logger.debug(
                        f"外部文件备份去重 task#{task_id}: 无变化，复用HEAD {result.stdout.strip()[:8]}"
                    )
                    return result.stdout.strip()

            subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, timeout=10)
            timestamp = datetime.now().isoformat(timespec="seconds")
            msg = f"external files backup: {label}\n\nTask#{task_id} @ {timestamp}"
            subprocess.run(
                ["git", "commit", "-m", msg],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=10,
            )

            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                commit_hash = result.stdout.strip()
                logger.debug(
                    f"外部文件备份成功 task#{task_id} commit={commit_hash[:8]} files={backed_up}"
                )
                return commit_hash
        except Exception as e:
            logger.warning(f"外部文件备份失败 task#{task_id}: {e}")
        return None

    @staticmethod
    def restore_all(task_id: int, repo_path: str, commit_hash: str) -> int:
        """从指定 commit 全量恢复所有外部文件到原始路径

        迭代开始快照记录 ExternalFileBackup 仓库的 HEAD 作为基线，
        回滚时用 git ls-tree 列出该 commit 下全部文件并逐个恢复
        （不修改备份仓库工作树）。

        Args:
            task_id: 事务ID
            repo_path: 备份仓库路径
            commit_hash: 基线 commit hash

        Returns:
            成功恢复的文件数
        """
        if not commit_hash or not repo_path or not os.path.isdir(repo_path):
            return 0

        try:
            # 列出 commit 中所有文件（相对仓库路径）
            result = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", commit_hash],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                logger.warning(f"外部文件全量恢复: ls-tree失败 {result.stderr[:200]}")
                return 0

            rel_paths = [ln.strip() for ln in result.stdout.split("\n") if ln.strip()]
            if not rel_paths:
                return 0

            # 相对路径转绝对路径（备份时去掉了根分隔符）
            abs_paths: list[str] = []
            for rel in rel_paths:
                if rel.startswith("__external__/"):
                    rel = rel[len("__external__/"):]
                # Windows 盘符路径（ntpath.isabs=True）直接使用，Unix 路径补根分隔符
                abs_path = rel if ntpath.isabs(rel) else "/" + rel
                abs_paths.append(abs_path)

            # 该仓库只存 workdir 外部文件，无需过滤 workdir
            restored = ExternalFileBackup.restore(task_id, abs_paths, commit_hash)
            logger.info(f"外部文件全量恢复 task#{task_id}: {restored} 个文件 from {commit_hash[:8]}")
            return restored
        except Exception as e:
            logger.warning(f"外部文件全量恢复失败 task#{task_id}: {e}", exc_info=True)
            return 0

    @staticmethod
    def check_changes(
        task_id: int, file_paths: list[str], commit_hash: str | None = None
    ) -> list[str]:
        """检查外部文件是否有变化

        使用 git hash-object 逐文件比较 blob hash，不修改备份仓库工作树。

        Args:
            task_id: 事务ID
            file_paths: 外部文件绝对路径列表
            commit_hash: 比较基准commit（默认HEAD）。回滚预检测时传入快照记录的
                external_backup_commit/external_head_commit 基线。

        Returns:
            变化的文件路径列表（绝对路径）
        """
        if not file_paths:
            return []

        try:
            repo = ExternalFileBackup._get_backup_repo(task_id)
            base = commit_hash or "HEAD"
            changed: list[str] = []

            for fp in file_paths:
                rel = ExternalFileBackup._to_repo_path(fp)

                if not os.path.exists(fp):
                    # 文件被删除：检查基准commit中是否存在
                    result = subprocess.run(
                        ["git", "cat-file", "-e", f"{base}:{rel}"],
                        cwd=repo,
                        capture_output=True,
                        timeout=5,
                    )
                    if result.returncode == 0:
                        changed.append(fp)
                    continue

                # 计算当前文件的 git blob hash
                result = subprocess.run(
                    ["git", "hash-object", fp],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode != 0:
                    changed.append(fp)  # 无法判断，保守认为有变化
                    continue
                current_hash = result.stdout.strip()

                # 获取基准commit中的 blob hash
                result = subprocess.run(
                    ["git", "rev-parse", f"{base}:{rel}"],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode != 0:
                    # 基准commit中不存在 → 新增文件
                    changed.append(fp)
                elif result.stdout.strip() != current_hash:
                    changed.append(fp)

            return changed

        except Exception as e:
            logger.warning(f"外部文件变更检查失败 task#{task_id}: {e}")
            return []

    @staticmethod
    def restore(task_id: int, file_paths: list[str], commit_hash: str | None = None) -> int:
        """从备份恢复外部文件

        使用 `git show <commit>:<path>` 直接从指定 commit（默认 HEAD）读取文件内容，
        不修改备份仓库工作树，避免 undo 快照覆盖 HEAD 的问题。

        Args:
            task_id: 事务ID
            file_paths: 外部文件绝对路径列表
            commit_hash: 恢复源 commit hash（默认使用 HEAD）

        Returns:
            成功恢复的文件数
        """
        if not file_paths:
            return 0

        try:
            repo = ExternalFileBackup._get_backup_repo(task_id)
            source = commit_hash or "HEAD"
            restored = 0

            for fp in file_paths:
                rel = ExternalFileBackup._to_repo_path(fp)

                # 用 git show 直接从 commit 读取文件内容，不污染工作树
                result = subprocess.run(
                    ["git", "show", f"{source}:{rel}"],
                    cwd=repo,
                    capture_output=True,
                    timeout=10,
                )
                if result.returncode != 0:
                    logger.debug(f"外部文件恢复: {source}:{rel} 不存在或读取失败")
                    continue

                Path(os.path.dirname(fp)).mkdir(parents=True, exist_ok=True)
                with open(fp, "wb") as f:
                    f.write(result.stdout)
                restored += 1

            logger.info(
                f"外部文件恢复 task#{task_id}: {restored} 个文件 "
                f"(from {source[:8] if len(source) > 8 else source})"
            )
            return restored

        except Exception as e:
            logger.warning(f"外部文件恢复失败 task#{task_id}: {e}")
            return 0

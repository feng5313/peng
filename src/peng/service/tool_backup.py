"""
工具调用备份与回退模块

为文件修改工具调用提供自动备份和一键回退功能：
1. git管理工作目录：使用备份分支 peng_backups/task#id，每次工具调用前用临时索引创建镜像commit
2. 非git工作目录：在备份目录（配置路径或系统temp）初始化git仓库，同步镜像并commit
3. 外部文件（工作目录外）：备份到本地git仓库，保留目录结构
   - Windows平台：带盘符的外部文件备份到该文件同盘符根目录 \\.peng\\backups\\task#id
     （如 D:\\work\\f.txt → D:\\.peng\\backups\\task#N），避免跨盘符复制
   - 其他平台/UNC路径：备份到 peng 服务 data/file_backups/task#id

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
import glob
import json
import logging
import ntpath
import os
import re
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


# ==================== 外部文件 manifest（🔧 事务#2107） ====================
# 本事务 external 备份文件的累积清单（data/external_manifests/task-<id>.json）。
#
# 存在意义：迭代快照回滚的恢复范围。盘符备份仓库（X:\.peng\backups）所有
# task 共用，旧逻辑"从基线 commit ls-tree 全量恢复"会混入其他 task 备份的
# 文件 → 跨事务覆盖事故（#2080 覆盖 #2077）。manifest 是本事务显式备份的
# 精确集合，恢复/检测/预览以其为准；仅剔除系统路径/盘根/排除目录，**不再
# 剔除 git 管理文件**——外部 git 仓库/嵌套子仓库文件正是本修复要保护的
# 备份对象（workdir git 快照不覆盖它们）。
#
# 旧 meta（无 external_manifest 字段）回滚时退回旧 ls-tree 全量 + scope="all"
# 全量 git 剔除兜底，行为与历史完全一致。

_MANIFEST_LOCK = threading.Lock()
_MANIFEST_MAX_FILES = 20000  # 清单上限（防异常场景失控膨胀）


def _manifest_file(task_id: int) -> Path:
    """manifest 持久化文件路径（data/external_manifests/task-<id>.json）"""
    base = Path(_get_rollback_ids_base()).parent / "external_manifests"
    return base / f"task-{int(task_id) or 0}.json"


def manifest_add(task_id: int, file_paths: list[str]) -> None:
    """累积记录本事务 external 备份的文件路径（并集；原子写，失败仅日志）

    🔧 事务#2402：兜底剔除 Windows 保留设备名（nul 等——上游过滤漏网时
    也不会写成永久死条目：设备名 check_changes 恒报变更、恢复恒跳过）。
    """
    if not task_id or not file_paths:
        return
    try:
        with _MANIFEST_LOCK:
            mf = _manifest_file(task_id)
            existing: list[str] = []
            if mf.exists():
                try:
                    existing = list(
                        json.loads(mf.read_text(encoding="utf-8")).get("files", [])
                    )
                except Exception:
                    existing = []
            seen = {ToolBackupManager._path_key(p) for p in existing}
            for fp in file_paths:
                if _is_windows_device_path(fp):
                    continue
                k = ToolBackupManager._path_key(fp)
                if k not in seen:
                    seen.add(k)
                    existing.append(fp)
            if len(existing) > _MANIFEST_MAX_FILES:
                existing = existing[-_MANIFEST_MAX_FILES:]
            mf.parent.mkdir(parents=True, exist_ok=True)
            tmp = mf.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(
                json.dumps({"files": existing}, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, mf)
    except Exception as e:
        logger.warning(f"manifest更新失败(task#{task_id}): {e}")


def manifest_get(task_id: int) -> list[str]:
    """读取本事务 external 备份文件清单（无/损坏返回空列表）"""
    if not task_id:
        return []
    try:
        mf = _manifest_file(task_id)
        if not mf.exists():
            return []
        return list(json.loads(mf.read_text(encoding="utf-8")).get("files", []))
    except Exception as e:
        logger.debug(f"manifest读取失败(task#{task_id}): {e}")
        return []


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

# 🔧 事务#2088 快照提速：持久化增量索引前缀（每 task 一份，跨快照复用）
# 根因：read-tree HEAD 产生的索引只含 tree 条目、无 stat 缓存，后续
# add -A/status 视全部文件为"可疑"必须重新 hash——大仓库（fcc res/ 数万
# 文件）每次快照都全量 hash，分钟级卡顿（task#2077 反复"备份快照进行中"）。
# 方案：固定名索引文件持久保留，add -A 依赖 git 自身 stat 缓存增量更新，
# 成本 O(实际变更文件数)；不存在时优先复制真实 .git/index 热启动（含用户
# git 维护的新鲜 stat 缓存），无则 read-tree HEAD 冷启动（首次全量一次）。
_PERSIST_IDX_PREFIX = "peng_backup_idx_task"


def _persist_index_path(git_dir: str | Path, task_id: int) -> str:
    """持久化增量索引路径（git_dir 下按 task 命名，不同事务互不干扰）"""
    return os.path.join(str(git_dir), f"{_PERSIST_IDX_PREFIX}{int(task_id) or 0}")


def _ensure_persist_index(
    idx_path: str, git_dir: str | Path, workdir: str,
    env: dict[str, str], nest_name: str | None = None,
) -> bool:
    """确保持久化增量索引就绪（本地执行）

    1. 已存在 → 直接复用（增量续跑，最快路径）
    2. 缺失 → 复制真实 .git/index 热启动（含 stat 缓存，add -A 只 hash 滞后差异）
    3. 无真实索引 → read-tree HEAD 冷启动（首次 add -A 全量，之后增量）

    Returns:
        True=索引就绪（含已有/复制/重建）；False=git 重建失败（调用方仍可
        尝试 add -A，git 会自行处理空/缺索引场景）
    """
    if os.path.exists(idx_path):
        return True
    real_idx = os.path.join(str(git_dir), "index")
    if os.path.isfile(real_idx):
        try:
            shutil.copy2(real_idx, idx_path)
            return True
        except OSError:
            pass
    r = _run_git(["read-tree", "HEAD"], cwd=workdir, env=env, nest_name=nest_name)
    return r.returncode == 0


def _recover_persist_index(
    idx_path: str, git_dir: str | Path, workdir: str,
    env: dict[str, str], nest_name: str | None = None,
) -> bool:
    """损坏恢复：删除持久索引 → 重建（read-tree HEAD）→ 返回是否成功"""
    try:
        os.unlink(idx_path)
    except OSError:
        pass
    r = _run_git(["read-tree", "HEAD"], cwd=workdir, env=env, nest_name=nest_name)
    return r.returncode == 0

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


# ==================== 备份排除规则（不备份未修改/无关产物文件） ====================
#
# 根因（事务#1660）：git 快照的 `add -A` 不理会任何排除规则，把 .gitignore 忽略的
# 未修改产物（build/、*.dll、*.jpg 等）全部写入备份对象库，且这些文件在真实修改后
# 几乎无内容重复，每次快照都新增整套 blob，导致大量硬盘空间浪费。
#
# 策略：git 快照只跟踪"用户源码"= git 真实跟踪的文件 + 快照后新增的同域文件：
#   1. read-tree HEAD 载入 git 跟踪集（.gitignore 已由用户维护，天然排除产物）
#   2. add -A 增量更新（捕获新增/修改文件）
#   3. 从索引剔除 build/build_*/.peng 等产物目录条目（无论是否修改都不备份）
#   4. write-tree —— 产物目录的既有 blob 已是 loose object 可被 gc 回收，
#      新快照不再为产物文件创建任何新对象
#
# 非快照路径（变更检测/diff预览）必须使用同一剔除规则，保证 tree 比较一致性。
# 剔除方式用 update-index --force-remove 逐条目移除（已实测验证：temp index 下
# 正常工作，diff --cached 可正确报告差异，且对未在索引中的路径无副作用）。
#
# 注意：不能用 `git rm --cached <dir>`/`add exclude pathspec`——排除 pathspec 会使
# add -A 失去全量删除检测能力（实测验证），语义变为"只更新匹配文件"。

# 强制排除的目录名（匹配任意层级，仅匹配目录名本身）
# 🔧 .git：嵌套 git 仓库元数据永不备份（对象库/引用，备份它们=灾难性膨胀）
# 🔧 vcpkg_installed/venv/.venv：依赖安装目录（GB 级产物，同 build 语义）
_FORCE_EXCLUDE_DIR_NAMES = {
    "build", ".peng", ".cache", "__pycache__", "node_modules",
    ".git", "vcpkg_installed", "venv", ".venv",
}

# 强制排除的目录名前缀（如 build_x86、build_llvm）
_FORCE_EXCLUDE_DIR_PREFIXES = ("build_",)


def _is_excluded_dirname(name: str) -> bool:
    """判断目录名是否命中强制排除规则（build/、build_*/等）"""
    return (
        name in _FORCE_EXCLUDE_DIR_NAMES
        or name.startswith(_FORCE_EXCLUDE_DIR_PREFIXES)
    )


def _filter_excluded_paths(paths: list[str]) -> list[str]:
    """过滤掉命中排除规则的索引路径（任意层级目录匹配）

    Args:
        paths: 索引中的路径列表（/ 分隔）

    Returns:
        排除规则命中的路径列表（用于 update-index --force-remove）
    """
    excluded: list[str] = []
    for p in paths:
        parts = p.split("/")
        # 任意一级目录名命中即排除（文件名本身不参与匹配，build.c 不受影响）
        if any(_is_excluded_dirname(part) for part in parts[:-1]):
            excluded.append(p)
    return excluded


def _purge_excluded_from_index(git_args_env: dict, cwd: str, nest_name: str | None) -> int:
    """从临时索引中剔除排除目录条目（本地执行）

    用 git ls-files 列出索引条目，过滤命中排除规则的路径，
    逐批 update-index --force-remove 移除。幂等：路径不在索引中时无副作用。

    Returns:
        移除的条目数（-1 表示失败，调用方可记录日志但不应中断）
    """
    result = _run_git(
        ["ls-files", "-c"], cwd=cwd, env=git_args_env, nest_name=nest_name
    )
    if result.returncode != 0:
        return -1
    entries = [ln for ln in result.stdout.split("\n") if ln.strip()]
    excluded = _filter_excluded_paths(entries)
    if not excluded:
        return 0
    # 分批执行避免命令行长度限制（Windows ~32KB）
    BATCH = 200
    for i in range(0, len(excluded), BATCH):
        batch = excluded[i : i + BATCH]
        _run_git(
            ["update-index", "--force-remove", "--"] + batch,
            cwd=cwd, env=git_args_env, nest_name=nest_name,
        )
    return len(excluded)


def _is_abs_path_excluded(abs_path: str) -> bool:
    """判断绝对（或相对）路径的任意目录层级是否命中强制排除规则"""
    parts = abs_path.replace("\\", "/").split("/")
    # 末段是文件名（或目录名——目录名也参与判断，build 目录本身整个排除）
    return any(_is_excluded_dirname(part) for part in parts)


# ==================== 备份范围护栏（🔧 事务#2088） ====================
# 根因事故：epyc 每盘 \.peng\backups 镜像了全盘（D 盘 502GiB 工作区 + 65GiB
# git 对象库）。触发链：bash/cmd 命令 token 解析出盘根（如 D:\）或浅目录
# （D:\downloads）→ 存在性过滤放行（根/目录都是"存在"的）→
# ExternalFileBackup 的 copytree 无条件复制 → 灾难级规模。
#
# 护栏设计（纵深防御，静态形态判定零 IO，本地/远程脚本共用同一套逻辑）：
# 1. 盘根/文件系统根（段数 0，如 D:\ 或 /）——绝对禁止，任何工具不例外
# 2. 一级目录（段数 1，如 D:\work、C:\Users）——禁止：离根最近的目录天然是
#    巨型容器，目录备份语义 = 递归复制整棵子树
# 3. 深层目录（段数 ≥2，如 D:\work\project）——允许，但备份时做规模限额
#    （文件数/总字节数，scandir 遍历统计，超限短路），防"名字像小目录
#    实为巨型仓库"
# 4. 文件——深度不限（单文件复制无子树放大效应）

# 目录备份规模上限（超出跳过并告警）
_DIR_BACKUP_MAX_FILES = 20000
_DIR_BACKUP_MAX_BYTES = 5 * 1024 * 1024 * 1024  # 5 GiB


def _path_depth_segments(path: str) -> int:
    """路径有效段数：去盘符/去根分隔符后的非空段数（纯形态判定，零IO）

    "D:\\" / "D:/" / "/" → 0；"D:\\work" / "/home" → 1；"D:\\work\\x" → 2
    """
    clean = str(path).replace("\\", "/")
    _, rest = ntpath.splitdrive(clean)
    return len([s for s in rest.split("/") if s])


def is_root_volume_path(path: str) -> bool:
    """是否为盘根/文件系统根（备份绝对禁区）"""
    return _path_depth_segments(path) == 0


def dir_backup_allowed_static(path: str) -> bool:
    """目录对象的静态形态护栏：盘根或一级目录 → False（禁止整树复制）"""
    return _path_depth_segments(path) >= 2


def _dir_within_budget_local(root: str) -> bool:
    """本地目录规模限额检查（scandir 遍历统计，超限短路返回 False）

    遍历失败（权限等）按超限处理（保守跳过备份，宁可少备不爆盘）。
    """
    n_files = 0
    total = 0
    stack = [str(root)]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            if d == str(root):
                return False  # 根目录不可访问：保守拒绝（宁可少备不爆盘）
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    n_files += 1
                    total += e.stat(follow_symlinks=False).st_size
            except OSError:
                continue
        if n_files > _DIR_BACKUP_MAX_FILES or total > _DIR_BACKUP_MAX_BYTES:
            return False
    return True


def _skip_reason_for_path(fp: str, is_dir: bool) -> str | None:
    """静态护栏判定：路径不允许备份时返回原因文案，允许返回 None

    is_dir 由调用方提供（本地/远程脚本各自判定，本函数纯形态零IO）。
    """
    if is_root_volume_path(fp):
        return f"盘根/文件系统根 {fp}"
    if is_dir and not dir_backup_allowed_static(fp):
        return f"一级目录（整树复制规模失控风险） {fp}"
    return None


# 🔧 事务#2402：Windows 保留设备名（basename 不区分大小写，允许带扩展名——
# "NUL.txt" 在 Windows 上同样落入设备语义）。cmd 的 `2>nul` 重定向目标会被
# 命令解析收集为"写入路径"，且设备名 os.path.exists 恒真（伪存在），存在性
# 过滤拦不住——task#2332 的 D:/work/fcc/nul 就这样进了备份 manifest，
# check_changes 恒报变更、回滚窗口永远显示"待恢复"。设备名不是普通文件，
# 从提取源头到恢复全程剔除。
_WINDOWS_DEVICE_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def _is_windows_device_path(path: str) -> bool:
    """路径 basename（去扩展名）是否为 Windows 保留设备名（nul/con/com1…）"""
    base = str(path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if not base:
        return False
    return base.split(".", 1)[0].lower() in _WINDOWS_DEVICE_STEMS


def _is_workdir_ancestor(path: str, workdir: str) -> bool:
    """path 是否为 workdir 本身或其祖先目录（归一化 + 大小写不敏感比较）

    workdir 的祖先必然是目录（如 workdir D:/work/fcc 的父 D:/work），
    绝不能进入 external 单文件备份/变更检测/恢复列表——task#2332 曾把
    D:/work 收进 manifest：check_changes 对目录 git hash-object 必失败
    → 恒报"已变更"；restore 的 git show 对 tree 基线成功 → open(目录,'wb')
    抛 IsADirectoryError 中断整个恢复脚本。workdir 内容由 git 快照负责，
    祖先目录一概不管（不备份、不检测、不恢复）。
    """
    if not path or not workdir:
        return False
    p_raw = str(path).replace("\\", "/")
    w_raw = str(workdir).replace("\\", "/")
    p = os.path.normpath(p_raw)
    w = os.path.normpath(w_raw)
    if not p or not w:
        return False
    # 相对路径无法与绝对 workdir 构成祖先关系（normpath 不解析盘符差异）；
    # 绝对性用原始形态判定（posixpath.normpath("D:/")→"D:" 会丢失斜杠，
    # ntpath.isabs("D:")=False，先 normpath 再判会把盘根误判为相对路径）
    if _is_abs_cross_platform(p_raw) != _is_abs_cross_platform(w_raw):
        return False
    p_l = p.lower().rstrip("/")
    w_l = w.lower().rstrip("/")
    if not p_l or not w_l:
        return False
    return w_l == p_l or w_l.startswith(p_l + "/")


class _SnapshotTransientError(Exception):
    """快照临时失败（网络断连/超时/命令行超限等非确定性错误）

    🔧 事务#2402：与"workdir 非 git 仓库"（确定性，可缓存 600s）区分——
    临时失败绝不写入 _NON_GIT_WD_CACHE，否则一次瞬断（如 epyc WebSocket
    1006 断连 70ms 后重连）会让后续整个 TTL 窗口内所有迭代快照持续降级
    history_only（task#2354 曾因 10:19 一次断连连降 26 个迭代，回滚窗口
    全程无代码变更，文件备份保护裸奔 10 分钟）。
    """


def _strip_drive_rel(file_path: str) -> str:
    """绝对路径 → 备份仓库内相对路径（去根分隔符；Windows 盘符路径去盘符）

    🔧 事务#1847：Windows 目录名不能含 ':'（mkdir 抛 WinError 123），且
    os.path.join/pathlib 拼接含盘符路径（如 "D:/x"）时会视为绝对路径直接
    替换前缀（dst 恒等于源文件路径，copy2(源, 源) 抛 WinError 32）。
    因此 __external__/盘符仓库内的 rel 一律去盘符：
    - "D:\\peng\\x" → "peng/x"；"/home/x" → "home/x"
    """
    clean = str(file_path).replace("\\", "/")
    drive, rest = ntpath.splitdrive(clean)
    if len(drive) == 2 and drive[1] == ":":
        return rest.lstrip("/")
    return clean.lstrip("/")


def _git_repo_valid(repo_path: str) -> bool:
    """验证目录是有效 git 仓库（.git 存在且 rev-parse 成功）

    🔧 事务#1847：`.git` 目录存在 ≠ 仓库有效——Windows 上 rmtree 因文件锁
    部分失败会留下残缺 .git（如仅 objects/），此时 git 操作全部 rc=128。
    """
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        return False
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-dir"], cwd=repo_path,
            capture_output=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def _load_gitignore_spec(root: str) -> Any:
    """加载目录顶层 .gitignore（非git工作目录场景；无文件返回 None）

    使用 pathspec（gitwildmatch 语义与 git 一致）。失败仅记日志返回 None。
    """
    gi = Path(root) / ".gitignore"
    if not gi.is_file():
        return None
    try:
        import pathspec

        return pathspec.PathSpec.from_lines("gitwildmatch", gi.read_text(encoding="utf-8", errors="replace").splitlines())
    except Exception as e:
        logger.debug(f"加载.gitignore失败({gi}): {e}")
        return None


def _iter_backup_files(root: Path) -> list[str]:
    """遍历目录收集相对文件路径（跳过排除目录与 .git，不跟随目录符号链接）

    Returns:
        相对路径列表（POSIX 分隔符，与 git 索引一致）
    """
    files: list[str] = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name == ".git" or _is_excluded_dirname(name):
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
            else:
                files.append(str(Path(entry.path).relative_to(root)).replace(os.sep, "/"))
    return files


def _need_copy(src: str | Path, dst: str | Path) -> bool:
    """判断是否需要复制：目标不存在，或 size/mtime 任一不同（未修改则跳过）"""
    try:
        s, d = os.stat(src), os.stat(dst)
        return s.st_size != d.st_size or abs(s.st_mtime - d.st_mtime) > 1e-6
    except OSError:
        return True  # 目标不存在（或 stat 失败）→ 需要复制


def _sync_dir_incremental(src_root: Path, dst_root: Path, spec: Any = None) -> tuple[int, int]:
    """增量同步目录（本地）：只复制新增/修改文件，删除目标端多余文件

    排除规则：
    - 强制排除目录（build/、build_*/、.peng 等，任意层级）
    - spec（.gitignore PathSpec，仅匹配顶层仓库相对路径，与 git 语义一致）

    Returns:
        (复制文件数, 删除条目数)
    """
    copied, removed = 0, 0

    # 1. 收集源端应同步的文件集
    all_files = _iter_backup_files(src_root)
    keep_files: dict[str, None] = {}
    for rel in all_files:
        if spec is not None and spec.match_file(rel):
            continue  # .gitignore 命中 → 不备份
        keep_files[rel] = None

    # 2. 清理目标端多余文件（源端不存在或被排除的），空目录一并删除
    for rel in _iter_backup_files(dst_root):
        if rel not in keep_files:
            try:
                (dst_root / rel).unlink()
                removed += 1
            except OSError:
                pass
    for dirpath, dirnames, filenames in os.walk(dst_root, topdown=False):
        p = Path(dirpath)
        if p == dst_root or p.name == ".git":
            continue
        if p.name == ".git":
            continue
        try:
            if not any(p.iterdir()):
                p.rmdir()
        except OSError:
            pass

    # 3. 增量复制（仅 size/mtime 不同的文件）
    for rel in keep_files:
        src = src_root / rel
        dst = dst_root / rel
        if _need_copy(src, dst):
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
    return copied, removed

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


def _paths_exist_batch(
    paths: list[str], nest_name: str | None = None
) -> set[str]:
    """批量检查路径存在性（🔧 事务#2088：远程单次HTTP替代逐路径N次HTTP）

    事故：task#2090 bash 执行 python 脚本，_extract_paths_from_script 从脚本
    内容提取出数百个路径样式 token，每个 token 一次 _path_exists HTTP
    （0.35s/次）→ 快照 60s 预算耗尽（"备份快照超预算跳过"）。

    Returns:
        存在的路径集合（本地/远程语义一致；远程失败返回空集，调用方按
        "不存在"处理=不备份，安全方向）
    """
    if not paths:
        return set()
    if not nest_name or nest_name == "local":
        return {p for p in paths if Path(p).exists()}
    py = (
        "import os, json, ntpath\n"
        f"paths = {paths!r}\n"
        "out = []\n"
        "for p in paths:\n"
        "    try:\n"
        "        if ntpath.isabs(p):\n"
        "            ok = os.path.exists(p)\n"
        "        else:\n"
        "            ok = os.path.exists(p.replace('/', os.sep))\n"
        "        if ok: out.append(p)\n"
        "    except OSError:\n"
        "        continue\n"
        "print(json.dumps(out))\n"
    )
    r = _run_python_remote(py, timeout=30, nest_name=nest_name)
    if r.returncode != 0:
        logger.debug(f"批量存在性判定失败({nest_name}): {str(r.stderr)[:150]}")
        return set()
    try:
        return set(json.loads(r.stdout.strip().split("\n")[-1]))
    except (json.JSONDecodeError, IndexError):
        return set()


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

    # 非git workdir 探测缓存（workdir → 判定为非git的时间戳；TTL 内跳过远程探测
    # 避免每次快照白一次HTTP——巨型目录场景每2分钟迭代快照会持续产生探测开销）
    _NON_GIT_WD_CACHE: dict[str, float] = {}
    _NON_GIT_WD_TTL = 600.0

    def _snapshot_git_dispatch(
        workdir: str, task_id: int, tool_name: str, tool_args: dict[str, Any],
        commit_msg: str, timestamp: str, nest_name: str | None,
        internal_files: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """git 快照分派（远程探测结果缓存；None=非git或失败→external 备份路径）

        🔧 事务#2107：internal_files（workdir 内文件）提供时，远程批量脚本
        顺路计算其中嵌套 git 仓库文件（workdir 快照只记 gitlink 无法恢复
        内容，需进 external 备份），放入 meta["nested_internal"]——零额外HTTP。
        """
        if nest_name and nest_name != "local":
            ts = ToolBackupManager._NON_GIT_WD_CACHE.get(workdir, 0)
            if time.time() - ts < ToolBackupManager._NON_GIT_WD_TTL:
                return None
            try:
                meta = ToolBackupManager._snapshot_git(
                    workdir, task_id, tool_name, tool_args, commit_msg, timestamp,
                    nest_name, internal_files,
                )
            except _SnapshotTransientError as e:
                # 🔧 事务#2402：临时失败（网络断连/超时/命令行超限）不写
                # 探测缓存——本次降级 history_only，下一轮快照自动重试；
                # 写缓存会造成一次瞬断后整个 TTL 窗口持续降级（#2354 事故）
                logger.warning(
                    f"git快照临时失败（不缓存，下轮重试）task#{task_id} "
                    f"({nest_name}) {workdir}: {e}"
                )
                return None
            if meta is None:
                ToolBackupManager._NON_GIT_WD_CACHE[workdir] = time.time()
            else:
                ToolBackupManager._NON_GIT_WD_CACHE.pop(workdir, None)
            return meta
        if _is_git_repo_local(workdir, nest_name):
            return ToolBackupManager._snapshot_git(
                workdir, task_id, tool_name, tool_args, commit_msg, timestamp,
                nest_name, internal_files,
            )
        return None

    # 🔧 事务#2107：git 管理过滤 scope 常量
    #   "all"     —— 旧语义（历史 meta 兜底）：剔除所有位于任何 git 仓库内的文件
    #               （#2088 全量防线，防斜杠归一化 bug 时期误备份数据被恢复）
    #   "workdir" —— 新语义（backup_policy=workdir_scope）：仅剔除位于 workdir
    #               所属 git 仓库内的文件（该仓库有 peng_backups/task#id 分支
    #               快照负责回滚）；其他 git 仓库（外部仓库/嵌套子仓库）的文件
    #               必须保留进 external 备份——否则修改它们时零备份零回滚！
    GIT_SCOPE_ALL = "all"
    GIT_SCOPE_WORKDIR = "workdir"

    @staticmethod
    def _path_key(p: str) -> str:
        """路径对比键（正斜杠+去尾斜杠；Windows 盘符路径大小写不敏感）"""
        k = str(p).replace("\\", "/").rstrip("/")
        if ToolBackupManager._win_style(p):
            return k.lower()
        return k

    @staticmethod
    def _git_root_local(file_path: str) -> str | None:
        """文件所在最近 git 仓库根目录（toplevel），非 git 管理返回 None（本地）

        🔧 Linux 服务端无法 stat Windows 盘符路径（dirname 会把整条路径当
        单文件名、向上爬误入 peng 自身 .git）——返回 None（保守不过滤）。
        """
        if ToolBackupManager._win_style(file_path) and os.name != "nt":
            return None
        p = os.path.dirname(os.path.normpath(file_path))
        while True:
            if os.path.isdir(os.path.join(p, ".git")):
                return p
            if os.path.dirname(p) == p:
                return None
            p = os.path.dirname(p)

    @staticmethod
    def _workdir_git_root_local(workdir: str) -> str | None:
        """workdir 所属 git 仓库根（toplevel）；workdir 非 git 仓库返回 None"""
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"], cwd=str(workdir),
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip().replace("\\", "/")
        except Exception:
            pass
        return None

    # 系统目录路径段（备份跳过：Windows 系统文件无备份价值且污染备份仓库；
    # 大小写不敏感，匹配路径中段。不含 /tmp/ /temp/——Linux /tmp 是常用
    # 工作路径，误伤面大）
    _SKIP_PATH_SEGMENTS = (
        "/windows/", "/program files/", "/program files (x86)/", "/appdata/local/temp/",
    )

    @staticmethod
    def _is_system_or_temp_path(file_path: str) -> bool:
        p = os.path.normpath(str(file_path)).replace("\\", "/").lower()
        return any(seg in f"/{p}/" for seg in ToolBackupManager._SKIP_PATH_SEGMENTS)

    @staticmethod
    def _filter_git_managed(
        file_paths: list[str], workdir: str, nest_name: str | None = None,
        scope: str = "all",
    ) -> list[str]:
        """过滤掉 git 仓库管理的文件（🔧 事务#2107：按 scope 分语义）

        历史（事务#2088 全量防线）：剔除所有位于任何 git 仓库内的文件。其
        假设"git 仓库管理的文件由其所属事务的 git 分支快照负责回滚"仅当该
        仓库**是事务 workdir** 时成立——外部 git 仓库/嵌套子仓库的文件被
        剔除后既无 workdir 快照覆盖、又无 external 备份 = 零备份零回滚！
        （#2080 跨事务覆盖事故的真正根因是共享盘符仓库的 ls-tree 全量恢复
        混入他 task 备份的文件，已由迭代回滚的 manifest 精确恢复集修复。）

        scope 语义：
        - "all"（旧 meta 兜底）：剔除所有位于任何 git 仓库内的文件
          （#2088 斜杠归一化 bug 时期误备份数据的恢复防线，保持不变）
        - "workdir"（backup_policy=workdir_scope 新备份）：仅剔除位于
          workdir 所属 git 仓库内的文件（该仓库有 peng_backups/task#id
          分支快照负责回滚）；外部 git 仓库/嵌套子仓库文件保留进 external
          备份（workdir git 快照只记 gitlink 无法恢复其内容，必须备份）

        远程批量单次HTTP。
        """
        if not file_paths:
            return file_paths
        if scope not in (
            ToolBackupManager.GIT_SCOPE_ALL, ToolBackupManager.GIT_SCOPE_WORKDIR,
        ):
            scope = ToolBackupManager.GIT_SCOPE_ALL
        if not nest_name or nest_name == "local":
            if os.name != "nt" and any(
                ToolBackupManager._win_style(fp) for fp in file_paths
            ):
                # Linux 服务端无法 stat Windows 盘符路径（此类场景实际都在
                # 远程 nest，本地分支保守全保留）
                logger.debug("git管理过滤: 本地Linux无法判定Windows路径，保守跳过")
                return file_paths
            if scope == ToolBackupManager.GIT_SCOPE_WORKDIR:
                wd_root = ToolBackupManager._workdir_git_root_local(workdir)
                if wd_root is None:
                    # workdir 非 git 仓库：workdir 内文件走 ExternalFileBackup
                    # 全量备份（type=external 路径），无仓库快照可负责 → 全保留
                    return file_paths
                wd_key = ToolBackupManager._path_key(wd_root)
                managed = set()
                for fp in file_paths:
                    root = ToolBackupManager._git_root_local(fp)
                    if root is not None and ToolBackupManager._path_key(root) == wd_key:
                        managed.add(fp)
            else:
                managed = {
                    fp for fp in file_paths
                    if ToolBackupManager._git_root_local(fp) is not None
                }
        else:
            py = (
                "import os, json, subprocess\n"
                f"files = {file_paths!r}\n"
                f"workdir = {workdir!r}\n"
                f"scope = {scope!r}\n"
                "wd_root = None\n"
                "if scope == 'workdir':\n"
                "    try:\n"
                "        r = subprocess.run(['git', 'rev-parse', '--show-toplevel'],"
                " cwd=workdir, capture_output=True, text=True, timeout=10)\n"
                "        if r.returncode == 0 and r.stdout.strip():\n"
                "            wd_root = r.stdout.strip()\n"
                "    except Exception:\n"
                "        pass\n"
                "def key(s):\n"
                "    return s.replace(chr(92), '/').rstrip('/').lower()\n"
                "def git_root(fp):\n"
                "    p = os.path.dirname(os.path.abspath(fp))\n"
                "    while True:\n"
                "        if os.path.isdir(os.path.join(p, '.git')):\n"
                "            return p\n"
                "        np_ = os.path.dirname(p)\n"
                "        if np_ == p:\n"
                "            return None\n"
                "        p = np_\n"
                "out = []\n"
                "if scope != 'workdir' or wd_root is not None:\n"
                "    for fp in files:\n"
                "        root = git_root(fp)\n"
                "        if root is None:\n"
                "            continue\n"
                "        if scope == 'workdir':\n"
                "            if key(root) == key(wd_root):\n"
                "                out.append(fp)\n"
                "        else:\n"
                "            out.append(fp)\n"
                "print(json.dumps(out))\n"
            )
            r = _run_python_remote(py, timeout=20, nest_name=nest_name)
            managed = set()
            if r.returncode == 0:
                try:
                    managed = set(json.loads(r.stdout.strip().split("\n")[-1]))
                except (json.JSONDecodeError, IndexError):
                    managed = set()
        return [fp for fp in file_paths if fp not in managed]

    @staticmethod
    def _filter_unbackupable(
        file_paths: list[str], workdir: str, nest_name: str | None = None,
        scope: str = "all",
    ) -> list[str]:
        """过滤不可备份对象：git 仓库管理的文件（按 scope）+ 系统目录/临时路径

        初始设计：backups 只备份"工作目录外或非git管理的文件"，且排除临时/
        系统文件（Temp/Program Files/Windows——task#1886 的 cmd 曾把
        vcvarsall.bat、fcc_log.txt 等备份进仓库）。
        scope 语义见 _filter_git_managed（🔧 事务#2107）。
        """
        kept = ToolBackupManager._filter_git_managed(
            file_paths, workdir, nest_name, scope
        )
        # 🔧 事务#2088：盘根路径不参与检测/预览/恢复（历史误备份基线兜底）
        # 🔧 事务#2402：Windows 保留设备名（`2>nul` 的 nul 等）+ workdir
        # 本身/祖先目录（如 workdir D:/work/fcc 的父 D:/work）不进备份——
        # 前者存在性恒真过滤不掉，后者是目录：hash-object 必失败恒报变更，
        # 恢复时 open(目录,'wb') 会炸掉整个恢复脚本
        return [
            fp for fp in kept
            if not ToolBackupManager._is_system_or_temp_path(fp)
            and not is_root_volume_path(fp)
            and not _is_windows_device_path(fp)
            and not _is_workdir_ancestor(fp, workdir)
        ]

    @staticmethod
    def _meta_scope(rollback_meta: dict[str, Any]) -> str:
        """回滚 meta 的 git 过滤 scope：新备份（workdir_scope）按 workdir 仓库
        判定；旧 meta（无标记）保持全量剔除兜底（防#2088误备份数据被恢复）"""
        return (
            ToolBackupManager.GIT_SCOPE_WORKDIR
            if rollback_meta.get("backup_policy") == "workdir_scope"
            else ToolBackupManager.GIT_SCOPE_ALL
        )

    @staticmethod
    def _filter_manifest_paths(
        file_paths: list[str], workdir: str | None = None,
    ) -> list[str]:
        """manifest 恢复集轻过滤：仅剔除系统路径/盘根/强制排除目录

        不剔 git 管理文件——manifest 是本事务显式备份的精确集合，不存在
        跨事务混入问题（🔧 事务#2107）。
        🔧 事务#2402：新增剔除 Windows 保留设备名；提供 workdir 时同步剔除
        workdir 本身/祖先目录（历史 manifest 的死条目——曾被误收的 D:/work
        等一级目录：check_changes 对目录恒报变更、restore open(目录) 会炸，
        读取侧过滤让存量脏数据自动失效，无需逐任务清洗）。
        """
        return [
            fp for fp in file_paths
            if not ToolBackupManager._is_system_or_temp_path(fp)
            and not is_root_volume_path(fp)
            and not _is_abs_path_excluded(fp)
            and not _is_windows_device_path(fp)
            and not (workdir and _is_workdir_ancestor(fp, workdir))
        ]

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
            # 提取工具涉及的文件路径，区分 workdir 内/外（排除目录不进备份）
            # 🔧 bash/cmd 命令解析动词参数+重定向目标+通配符展开（远程经 nest）
            all_paths = ToolBackupManager._extract_file_paths(
                tool_name, tool_args, workdir, nest_name
            )
            external_files: list[str] = []  # workdir 外
            internal_files: list[str] = []  # workdir 内（非git场景也进备份仓库）
            for fp in all_paths:
                fp_abs = fp if _is_abs_cross_platform(fp) else os.path.join(workdir, fp)
                # 🔧 事务#2088：斜杠归一化——Linux 端 os.path.normpath 不转化
                # Windows 反斜杠（"D:\work\fcc\x" vs workdir "D:/work/fcc"
                # startswith 失败），workdir 内文件被误判为"外部"复制进
                # \.peng\backups（git workdir 本不该有任何文件进备份仓库，
                # task#2077 cmd/edit 的 fcc 文件因此被复制）。
                fp_norm = os.path.normpath(fp_abs).replace("\\", "/")
                if _is_abs_path_excluded(fp_norm):
                    continue  # build/、build_*/等排除目录：不备份
                # 🔧 事务#2088：盘根/文件系统根绝对不进备份列表（防全盘复制）
                if is_root_volume_path(fp_norm):
                    logger.warning(f"快照路径过滤（盘根）: {fp_norm}")
                    continue
                wd_norm = os.path.normpath(workdir).replace("\\", "/").rstrip("/")
                # 边界安全：fp==wd 或 fp 以 wd+"/" 开头（防 fcc2 误匹配 fcc）
                if fp_norm == wd_norm or fp_norm.startswith(wd_norm + "/"):
                    internal_files.append(fp_norm)
                else:
                    external_files.append(fp_norm)

            meta: dict[str, Any] | None = None
            # 🔧 git workdir：git 分支快照（临时索引，零复制；探测缓存防白跑；
            # 🔧 事务#2087：status 增量——无变更时 tree 同 HEAD 自然复用/新建空差异commit）
            # 🔧 事务#2107：internal_files 一并传入——远程批量脚本顺路识别其中
            # 嵌套 git 仓库文件（workdir 快照只记 gitlink 无法恢复其内容）
            meta = ToolBackupManager._snapshot_git_dispatch(
                workdir, task_id, tool_name, tool_args, commit_msg, timestamp,
                nest_name, internal_files,
            )

            if meta is not None:
                # git 快照成功：workdir 外文件补进备份仓库
                if external_files:
                    # 🔧 事务#2107：scope=workdir——仅剔除 workdir 所属仓库内
                    # 文件（其分支快照已覆盖）；外部/嵌套 git 仓库文件保留
                    # （workdir 快照不覆盖它们，剔除=零备份！）+ 系统路径/盘根
                    external_files = ToolBackupManager._filter_unbackupable(
                        external_files, workdir, nest_name,
                        scope=ToolBackupManager.GIT_SCOPE_WORKDIR,
                    )
                # 🔧 事务#2107：workdir 内嵌套 git 仓库文件——workdir 快照只记
                # gitlink 无法恢复内容，必须进 external 备份。远程批量脚本已
                # 顺路算出（meta["nested_internal"]）；本地现算（scope=workdir
                # 过滤 internal_files，剩余即嵌套仓库文件+系统路径剔除）
                if internal_files:
                    if "nested_internal" in meta:
                        nested_files = meta.pop("nested_internal")
                    else:
                        nested_files = ToolBackupManager._filter_unbackupable(
                            internal_files, workdir, nest_name,
                            scope=ToolBackupManager.GIT_SCOPE_WORKDIR,
                        )
                    if nested_files:
                        external_files = external_files + [
                            fp for fp in nested_files if fp not in external_files
                        ]
                        logger.debug(
                            f"快照: workdir内嵌套git仓库文件进external备份(task#{task_id}): "
                            f"{nested_files[:5]}"
                        )
                if external_files:
                    ext_commit = ExternalFileBackup.backup(
                        task_id, external_files, f"{tool_name} @ {timestamp}",
                        nest_name=nest_name,
                    )
                    meta["external_files"] = external_files
                    meta["external_backup_commit"] = ext_commit
                    # 🔧 事务#2107：累积 manifest（迭代回滚的精确恢复集）
                    manifest_add(task_id, external_files)
                # 🔧 事务#2107：标记新备份策略（恢复/检测按 workdir scope 过滤）
                meta["backup_policy"] = "workdir_scope"
                return meta

            # 🔧 非git workdir：所有涉及文件（内外）统一进备份仓库（初始设计：
            # \.peng\backups git 环境备份"工作目录外或非git管理的文件"——按绝对
            # 路径映射为仓库子路径）。只备份工具涉及的文件，绝不扫描/镜像整个
            # workdir（巨型目录如 D:\work 的全量基线是灾难）。
            # 🔧 事务#2107：workdir 非自身 git 仓库 → 无仓库快照负责，嵌套 git
            # 仓库文件也必须进备份（旧"由其仓库管理不备份"假设不成立——该
            # 嵌套仓库无本事务快照分支）。系统目录/临时路径仍跳过。
            backup_files = ToolBackupManager._filter_unbackupable(
                external_files + internal_files, workdir, nest_name,
                scope=ToolBackupManager.GIT_SCOPE_WORKDIR,
            )
            if not backup_files:
                return None
            spec = ExternalFileBackup.backup(
                task_id, backup_files, f"{tool_name} @ {timestamp}",
                nest_name=nest_name,
            )
            if not spec:
                return None
            # 🔧 事务#2107：累积 manifest（迭代回滚的精确恢复集）
            manifest_add(task_id, backup_files)
            commit_disp = spec if isinstance(spec, str) else json.dumps(spec)
            return {
                "type": "external",
                "task_id": task_id,
                "tool_name": tool_name,
                "commit_hash": commit_disp[:40],
                "workdir": workdir,
                "nest_name": nest_name or "local",
                "file_paths": [],
                "external_files": backup_files,
                "external_backup_commit": spec,
                "timestamp": timestamp,
                "rollback_cmd": "",
                "backup_policy": "workdir_scope",
            }
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
        4. 外部文件基线：各备份仓库 HEAD（ExternalFileBackup；默认 data 仓库 +
           Windows 盘符 /.peng/backups 仓库），回滚时从这些 commit 全量恢复
           workdir 外文件

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
            # 1. 工作目录快照：git workdir → git 分支快照。
            #    🔧 非git workdir 不做文件快照（初始设计：\.peng\backups 只备份
            #    "工作目录外或非git管理的文件"=工具涉及文件，在 create_snapshot
            #    时精确备份；迭代点全量基线对巨型 workdir（如 D:\work）是灾难）
            try:
                meta = ToolBackupManager._snapshot_git_dispatch(
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

            # 3. 外部文件基线：记录各备份仓库的当前 HEAD（🔧 多仓库：
            #    默认 data 仓库 + Windows 各盘符 \.peng\backups 仓库），
            #    回滚时从这些 commit 恢复 workdir 外文件（无需迭代开始时同步）。
            #    🔧 事务#2107：恢复范围以 manifest（本事务备份文件的精确清单）
            #    为准，不再 ls-tree 全量——共享盘符仓库 HEAD 含其他 task 备份
            #    的文件，全量恢复=跨事务覆盖事故（#2080）。
            #    迭代点也记录当前累积 manifest（本迭代开始时的基线文件集）。
            try:
                ext_heads = ExternalFileBackup.get_all_heads(task_id, nest_name=nest_name)
                if ext_heads:
                    meta["external_head_commit"] = ext_heads
                    # manifest 基线快照（迭代回滚的精确恢复集；截断保护）
                    ext_manifest = manifest_get(task_id)[:_MANIFEST_MAX_FILES]
                    if ext_manifest:
                        meta["external_manifest"] = ext_manifest
                    meta["backup_policy"] = "workdir_scope"
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
            elif backup_type == "external":
                # 🔧 非git workdir 快照：涉及文件统一在备份仓库（初始设计）
                ext_files = rollback_meta.get("external_files", [])
                ext_commit = rollback_meta.get("external_backup_commit")
                changed = ExternalFileBackup.check_changes(
                    int(rollback_meta.get("task_id", 0) or 0), ext_files, ext_commit,
                    nest_name=nest_name, strict_baseline=True,
                )
                if not changed:
                    return None
                rollback_meta["changed_files"] = changed
                rollback_meta["added_files"] = []
                return rollback_meta
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

        # 🔧 事务#2088 持久化增量索引（与快照共用同一索引文件，互利增量：
        # 检测路径的 add -A 同样刷新 stat 缓存，快照随之提速）
        task_id = int(rollback_meta.get("task_id", 0) or 0)
        persist_idx = _persist_index_path(git_dir, task_id)
        env = {"GIT_INDEX_FILE": persist_idx}

        try:
            _ensure_persist_index(persist_idx, git_dir, workdir, env, nest_name)
            r_add = _run_git(
                ["add", "-A"], cwd=workdir, env=env, nest_name=nest_name, timeout=180
            )
            if r_add.returncode != 0:
                _recover_persist_index(persist_idx, git_dir, workdir, env, nest_name)
                _run_git(
                    ["add", "-A"], cwd=workdir, env=env, nest_name=nest_name, timeout=180
                )
            # 🔧 排除产物目录（与快照路径同规则，保证 tree 比较一致性）
            _purge_excluded_from_index(env, workdir, nest_name)
            result = _run_git(
                ["write-tree"], cwd=workdir, env=env, nest_name=nest_name, timeout=60
            )
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
                        rollback_meta.get("task_id", 0), external_files,
                        nest_name=nest_name, strict_baseline=True,
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
                    rollback_meta.get("task_id", 0), external_files,
                    nest_name=nest_name, strict_baseline=True,
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
            # 持久化增量索引保留（与快照共用，增量复用）
            pass

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
            "import subprocess, os, sys, json, uuid, shutil\n"
            f"workdir = {workdir!r}\n"
            f"task_id = {int(rollback_meta.get('task_id', 0) or 0)!r}\n"
            f"commit_hash = {commit_hash!r}\n"
            f"excl_dirs = {sorted(_FORCE_EXCLUDE_DIR_NAMES)!r}\n"
            f"excl_prefixes = {_FORCE_EXCLUDE_DIR_PREFIXES!r}\n"
            f"result_data = {{}}\n"
            "try:\n"
            "    def git(*args, env=None, timeout=30):\n"
            "        full_env = dict(os.environ)\n"
            "        if env: full_env.update(env)\n"
            "        return subprocess.run(['git'] + list(args), cwd=workdir, env=full_env,\n"
            "                              capture_output=True, text=True, timeout=timeout)\n"
            "    def purge_excluded(env_idx):\n"
            "        r = git('ls-files', '-c', env=env_idx)\n"
            "        if r.returncode != 0: return -1\n"
            "        excluded = [p for p in (ln for ln in r.stdout.split(chr(10)) if ln.strip())\n"
            "                    if any(x in excl_dirs or x.startswith(excl_prefixes)\n"
            "                           for x in p.split('/')[:-1])]\n"
            "        for i in range(0, len(excluded), 200):\n"
            "            git('update-index', '--force-remove', '--', *excluded[i:i+200], env=env_idx)\n"
            "        return len(excluded)\n"
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
            # 🔧 事务#2088 持久化增量索引（与快照共用，跨调用增量 O(变更数)）
            "    temp_index = os.path.join(git_dir, 'peng_backup_idx_task' + str(task_id))\n"
            "    env_idx = {'GIT_INDEX_FILE': temp_index}\n"
            "    if not os.path.exists(temp_index):\n"
            "        real_idx = os.path.join(git_dir, 'index')\n"
            "        if os.path.isfile(real_idx):\n"
            "            try: shutil.copy2(real_idx, temp_index)\n"
            "            except OSError: git('read-tree', 'HEAD', env=env_idx)\n"
            "        else:\n"
            "            git('read-tree', 'HEAD', env=env_idx)\n"
            "    r_add = git('add', '-A', env=env_idx, timeout=300)\n"
            "    # 🔧 修复 worktree 中 submodule .git 路径错误导致 git add -A 失败\n"
            "    if r_add.returncode != 0:\n"
            "        r_gm = git('config', '-f', '.gitmodules', '--get-regexp', 'path')\n"
            "        submod_paths = [ln.split(' ', 1)[1] for ln in r_gm.stdout.strip().split(chr(10)) if ln.strip()]\n"
            "        if submod_paths:\n"
            "            excludes = ['.'] + [f':(exclude){p}' for p in submod_paths]\n"
            "            r_add = git('add', '-A', *excludes, env=env_idx, timeout=300)\n"
            "        if r_add.returncode != 0:\n"
            "            try: os.unlink(temp_index)\n"
            "            except OSError: pass\n"
            "            git('read-tree', 'HEAD', env=env_idx)\n"
            "            git('add', '-A', env=env_idx, timeout=300)\n"
            "    # 🔧 排除产物目录（与快照路径同规则，保证 tree 比较一致性）\n"
            "    purge_excluded(env_idx)\n"
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
            "print(json.dumps(result_data))\n"
        )

        # 🔧 持久化增量索引保留（与快照共用）；首次全量放宽超时
        result = _run_python_remote(py_code, timeout=360, nest_name=nest_name)
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
                    rollback_meta.get("task_id", 0), external_files,
                    nest_name=nest_name, strict_baseline=True,
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
                rollback_meta.get("task_id", 0), external_files,
                nest_name=nest_name, strict_baseline=True,
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
        """非git场景变更检测（🔧 worktree 模式：--work-tree 临时索引对比；旧 mirror 模式兼容）"""
        backup_repo = rollback_meta["backup_repo"]
        commit_hash = rollback_meta["commit_hash"]

        if not _path_exists(backup_repo, nest_name):
            return rollback_meta

        # 🔧 worktree 模式：零复制，临时索引 write-tree 对比
        if rollback_meta.get("repo_mode") == "worktree":
            try:
                detection = ToolBackupManager._non_git_check_worktree(
                    workdir, backup_repo, commit_hash, nest_name,
                    task_id=int(rollback_meta.get("task_id", 0) or 0),
                )
                if detection is None:
                    return rollback_meta
                changed_files, added_files = detection
                if not changed_files and not added_files:
                    logger.debug(f"非git检查: 无文件变化(commit={commit_hash[:8]})")
                    return None
                rollback_meta["changed_files"] = changed_files + added_files
                rollback_meta["added_files"] = added_files
                logger.info(
                    f"非git检查: 检测到{len(changed_files)}个文件变化 "
                    f"(commit={commit_hash[:8]}): {changed_files[:5]}"
                )
                return rollback_meta
            except Exception as e:
                logger.warning(f"非gitworktree变更检查失败: {e}", exc_info=True)
                return rollback_meta

        # 旧 mirror 模式（历史 meta）：同步工作目录到备份仓库后对比
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
        internal_files: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """git工作目录快照：用临时索引创建镜像commit到备份分支（纯文件操作）

        🔧 事务#2107：internal_files 仅远程分支使用（批量脚本顺路算嵌套仓库
        文件，meta["nested_internal"]）；本地由 create_snapshot 直接过滤。
        """
        # 🔧 远程nest：使用批量Python脚本，一次HTTP完成全部git操作
        if nest_name and nest_name != "local":
            return ToolBackupManager._snapshot_git_remote_batch(
                workdir, task_id, tool_name, tool_args, commit_msg, timestamp,
                nest_name, internal_files,
            )

        git_dir = _get_git_dir(workdir, nest_name)
        if not git_dir:
            logger.warning(f"无法获取git目录: {workdir}")
            return None

        branch_name = f"{BACKUP_BRANCH_PREFIX}{task_id}"
        # 🔧 事务#2088 增量索引：持久化固定名索引（跨快照复用 stat 缓存），
        # 替代每次重建的临时索引——read-tree HEAD 的索引无 stat 缓存，
        # add -A/status 必须 hash 全部文件，是大仓库快照分钟级卡顿的根因
        persist_idx = _persist_index_path(git_dir, task_id)
        env = {"GIT_INDEX_FILE": persist_idx}

        try:
            # 索引就绪：已有→增量续跑；缺失→复制真实index热启动/read-tree冷启动
            _ensure_persist_index(persist_idx, git_dir, workdir, env, nest_name)
            # add -A 依赖 git stat 缓存增量更新：O(实际变更数)；
            # 首次（冷/热启动）可能全量一次，之后每次仅 hash 变化文件
            r_add = _run_git(
                ["add", "-A"], cwd=workdir, env=env, nest_name=nest_name, timeout=180
            )
            if r_add.returncode != 0:
                # 持久索引损坏（中断残留/版本不兼容）→ 重建后重试一次
                logger.warning(
                    f"增量add失败({workdir})，重建索引重试: {r_add.stderr[:150]}"
                )
                _recover_persist_index(persist_idx, git_dir, workdir, env, nest_name)
                _run_git(
                    ["add", "-A"], cwd=workdir, env=env, nest_name=nest_name, timeout=180
                )
            # 🔧 排除产物目录：build/、build_*/等无论是否修改都不进快照
            # （未修改的 .gitignore 产物文件不再产生 blob 对象）
            _purge_excluded_from_index(env, workdir, nest_name)

            result = _run_git(
                ["write-tree"], cwd=workdir, env=env, nest_name=nest_name, timeout=60
            )
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
            # 持久化增量索引保留（下次快照增量复用；损坏时下次自动重建）
            pass

    @staticmethod
    def _snapshot_git_remote_batch(
        workdir: str,
        task_id: int,
        tool_name: str,
        tool_args: dict[str, Any],
        commit_msg: str,
        timestamp: str,
        nest_name: str,
        internal_files: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """🔧 远程批量git快照：一次HTTP调用完成全部git操作

        将原来的9次HTTP请求（read-tree, add, write-tree, rev-parse×2,
        commit-tree, update-ref, 清理temp_index×2）合并为1次，
        大幅减少网络往返延迟（从~13秒降至~1.5秒）。
        🔧 事务#2107：internal_files 提供时顺路计算嵌套 git 仓库文件
        （meta["nested_internal"]），供 create_snapshot 补进 external 备份。
        """
        branch_name = f"{BACKUP_BRANCH_PREFIX}{task_id}"
        file_paths = ToolBackupManager._extract_file_paths(tool_name, tool_args, workdir)

        # 🔧 事务#2402：internal_files 相对化瘦身——绝对路径（D:/work/fcc/x.cpp
        # 平均数十字符）改为相对 workdir 的短路径，脚本端 join 还原；大事务
        # 涉及文件多时曾撑爆 Windows cmd 8191 字符命令行限制（"命令行太长"
        # →快照失败）。相对化平均缩短 60-80%，配合超长时丢列表重试双保险
        wd_norm_key = os.path.normpath(str(workdir)).replace("\\", "/").rstrip("/").lower()
        rel_files: list[str] = []
        for fp in (internal_files or []):
            fp_key = os.path.normpath(str(fp)).replace("\\", "/")
            if fp_key.lower().startswith(wd_norm_key + "/"):
                rel_files.append(fp_key[len(wd_norm_key) + 1:])
            else:
                rel_files.append(fp_key)  # 意外形态：原样保留（脚本端不 join）

        # 构建批量脚本：在远程nest上执行完整快照流程
        py_code = (
            "import subprocess, os, sys, json, uuid, shutil\n"
            f"workdir = {workdir!r}\n"
            f"task_id = {task_id!r}\n"
            f"branch_name = {branch_name!r}\n"
            f"commit_msg = {commit_msg!r}\n"
            f"_rel_files = {rel_files!r}\n"
            # 相对路径还原为绝对路径（嵌套仓库识别需要向上找 .git）
            "internal_files = [os.path.normpath(os.path.join(workdir, r)) for r in _rel_files]\n"
            f"excl_dirs = {sorted(_FORCE_EXCLUDE_DIR_NAMES)!r}\n"
            f"excl_prefixes = {_FORCE_EXCLUDE_DIR_PREFIXES!r}\n"
            f"result_data = {{}}\n"
            "temp_index = None\n"
            "try:\n"
            "    def git(*args, env=None, timeout=30):\n"
            "        full_env = dict(os.environ)\n"
            "        if env: full_env.update(env)\n"
            "        return subprocess.run(['git'] + list(args), cwd=workdir, env=full_env,\n"
            "                              capture_output=True, text=True, timeout=timeout)\n"
            "    def is_excluded(name):\n"
            "        return name in excl_dirs or name.startswith(excl_prefixes)\n"
            "    def filter_excluded(entries):\n"
            "        out = []\n"
            "        for p in entries:\n"
            "            parts = p.split('/')\n"
            "            if any(is_excluded(x) for x in parts[:-1]): out.append(p)\n"
            "        return out\n"
            "    def purge_excluded(env_idx):\n"
            "        r = git('ls-files', '-c', env=env_idx)\n"
            "        if r.returncode != 0: return -1\n"
            "        entries = [ln for ln in r.stdout.split(chr(10)) if ln.strip()]\n"
            "        excluded = filter_excluded(entries)\n"
            "        for i in range(0, len(excluded), 200):\n"
            "            git('update-index', '--force-remove', '--', *excluded[i:i+200], env=env_idx)\n"
            "        return len(excluded)\n"
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
            # 🔧 事务#2088 持久化增量索引：read-tree HEAD 的索引无 stat 缓存，
            # add -A/status 必须 hash 全部文件——大仓库每次快照分钟级卡顿的
            # 根因。固定名索引跨快照复用（增量 O(变更数)）；缺失时优先复制
            # 真实 .git/index 热启动（含用户 git 维护的 stat 缓存）
            "    temp_index = os.path.join(git_dir, 'peng_backup_idx_task' + str(task_id))\n"
            "    env_idx = {'GIT_INDEX_FILE': temp_index}\n"
            "    if not os.path.exists(temp_index):\n"
            "        real_idx = os.path.join(git_dir, 'index')\n"
            "        if os.path.isfile(real_idx):\n"
            "            try: shutil.copy2(real_idx, temp_index)\n"
            "            except OSError: git('read-tree', 'HEAD', env=env_idx)\n"
            "        else:\n"
            "            git('read-tree', 'HEAD', env=env_idx)\n"
            # add -A 依赖索引 stat 缓存增量更新（首次可能全量一次，之后仅变更文件）
            "    r_add = git('add', '-A', env=env_idx, timeout=300)\n"
            # 🔧 修复 worktree 中 submodule .git 路径错误导致 git add -A 失败的问题
            # （读 .gitmodules pathspec exclude 排除后重试；索引损坏时重建重试）
            "    if r_add.returncode != 0:\n"
            "        r_gm = git('config', '-f', '.gitmodules', '--get-regexp', 'path')\n"
            "        submod_paths = [ln.split(' ', 1)[1] for ln in r_gm.stdout.strip().split(chr(10)) if ln.strip()]\n"
            "        if submod_paths:\n"
            "            excludes = ['.'] + [f':(exclude){p}' for p in submod_paths]\n"
            "            r_add = git('add', '-A', *excludes, env=env_idx, timeout=300)\n"
            "        if r_add.returncode != 0:\n"
            # 持久索引损坏（中断残留/版本不兼容）→ 删除重建 + 全量重试一次
            "            try: os.unlink(temp_index)\n"
            "            except OSError: pass\n"
            "            git('read-tree', 'HEAD', env=env_idx)\n"
            "            git('add', '-A', env=env_idx, timeout=300)\n"
            "    # 🔧 排除产物目录：build/、build_*/等无论是否修改都不进快照\n"
            "    purge_excluded(env_idx)\n"
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
            # 🔧 事务#2107：顺路计算 internal_files 中的嵌套 git 仓库文件
            # （workdir 快照只记 gitlink 无法恢复其内容 → 需进 external 备份；
            #  向上找最近 .git 根，≠ workdir toplevel 即嵌套仓库；零额外HTTP）
            "    if internal_files:\n"
            "        r_tl = git('rev-parse', '--show-toplevel')\n"
            "        if r_tl.returncode == 0 and r_tl.stdout.strip():\n"
            "            tk = r_tl.stdout.strip().replace(chr(92), '/').rstrip('/').lower()\n"
            "            def normkey(p):\n"
            "                return p.replace(chr(92), '/').rstrip('/').lower()\n"
            "            nested = []\n"
            "            for fp in internal_files:\n"
            "                p = os.path.dirname(os.path.normpath(fp.replace('/', os.sep)))\n"
            "                while True:\n"
            "                    if os.path.isdir(os.path.join(p, '.git')):\n"
            "                        if normkey(p) != tk:\n"
            "                            nested.append(fp)\n"
            "                        break\n"
            "                    if normkey(p) == tk:\n"
            "                        break  # 到 workdir 根仍无 .git → 属于 workdir 仓库\n"
            "                    np_ = os.path.dirname(p)\n"
            "                    if np_ == p:\n"
            "                        break\n"
            "                    p = np_\n"
            "            if nested:\n"
            "                result_data['nested_internal'] = nested\n"
            "except Exception as e:\n"
            f"    result_data['error'] = str(e)\n"
            "print(json.dumps(result_data))\n"
        )

        # 🔧 事务#2402：精简版脚本（空 internal_files，嵌套识别降级）——
        # "命令行太长"时用它重试，保住主快照（代码备份/回滚是核心功能）
        py_code_without_nested = py_code.replace(
            f"_rel_files = {rel_files!r}\n", "_rel_files = []\n", 1
        )

        # 🔧 持久化增量索引跨快照复用（不删除）；首次可能全量，timeout 放宽
        # 🔧 事务#2402：①nest 瞬断（WebSocket 1006 断连后亚秒级重连）短重试
        # 一次，避免一次断连废掉整个快照；②"命令行太长"（Windows cmd 8191
        # 字符限制，脚本+internal_files 经 URL 传输）时丢弃 internal_files
        # （嵌套仓库识别降级）重建脚本重试一次，保住主快照（代码备份/回滚）
        result = None
        for attempt, code in enumerate((py_code, py_code_without_nested)):
            if attempt > 0:
                logger.warning(
                    f"远程批量快照重试（精简internal_files）({nest_name})"
                )
            result = _run_python_remote(code, timeout=360, nest_name=nest_name)
            if result.returncode == 0:
                break
            err_txt = (result.stderr or "")[:200]
            if attempt == 0 and (
                "未连接" in err_txt or "关闭" in err_txt or "timeout" in err_txt.lower()
            ):
                # 瞬断重试（一次）
                time.sleep(1.5)
                result = _run_python_remote(code, timeout=360, nest_name=nest_name)
                if result.returncode == 0:
                    break
            if attempt == 0 and "命令行太长" in err_txt:
                continue  # 换精简脚本重试
            break

        if result is None or result.returncode != 0:
            err_txt = (result.stderr if result else "")[:200] if result else ""
            logger.warning(f"远程批量快照失败({nest_name}): {err_txt}")
            # 🔧 事务#2402：网络/执行失败=临时错误，绝不写探测缓存（防一次
            # 瞬断导致 TTL 内所有迭代快照持续降级 history_only，#2354 事故）
            raise _SnapshotTransientError(f"远程执行失败: {err_txt}")

        try:
            data = json.loads(result.stdout.strip().split("\n")[-1])
        except (json.JSONDecodeError, IndexError):
            logger.warning(f"远程批量快照响应解析失败({nest_name}): {result.stdout[:200]}")
            raise _SnapshotTransientError("响应解析失败（远程输出异常）")

        if data.get("error"):
            err = str(data.get("error", ""))[:200]
            if "rev-parse --git-dir failed" in err:
                # workdir 非 git 仓库：正常探测失败（external 备份路径接管），降噪
                logger.debug(f"workdir非git，走备份仓库路径({nest_name}): {workdir}")
                return None
            # 🔧 事务#2402：workdir 是 git 仓库但 git 操作失败（索引损坏/占用/
            # 超时等）——非确定性，不缓存，下轮快照重试
            logger.warning(f"远程批量快照git错误({nest_name}): {err}")
            raise _SnapshotTransientError(f"git操作失败: {err}")

        commit_hash = data.get("commit_hash", "")
        if not commit_hash:
            logger.warning(f"远程批量快照未返回commit_hash({nest_name})")
            raise _SnapshotTransientError("未返回commit_hash")

        rollback_cmd = (
            f"git checkout {commit_hash[:8]} -- ."
            if not file_paths
            else f"git checkout {commit_hash[:8]} -- {' '.join(file_paths)}"
        )

        meta = {
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
        # 🔧 事务#2107：嵌套 git 仓库文件清单（远程顺路算出；键恒存在——
        # 空列表表示"已算且无"，防止 create_snapshot 走本地fallback再发HTTP）
        meta["nested_internal"] = list(data.get("nested_internal") or [])
        return meta

    # ==================== 非git 工作目录快照 ====================

    # ==================== 非git workdir 的 work-tree 快照模式（🔧 事务#1847 第五轮） ====================
    # 备份仓库只存 git 对象库（.git），用 --work-tree 指向真实 workdir 做快照：
    # 不复制任何文件，git 增量对象天然只记录改动/删除（风主要求：只备份改动文件，
    # 不镜像整个工作目录）。远程 nest 打包单脚本一次 HTTP。

    @staticmethod
    def _worktree_exclude_pathspec() -> list[str]:
        """构造强制排除 pathspec（build/、build_*/ 等，任意层级）"""
        specs = ["."]
        for name in sorted(_FORCE_EXCLUDE_DIR_NAMES):
            specs.append(f":(exclude,glob)**/{name}/**")
            specs.append(f":(exclude,glob)**/{name}")
        for p in _FORCE_EXCLUDE_DIR_PREFIXES:
            specs.append(f":(exclude,glob)**/{p}*/**")
        return specs

    @staticmethod
    def _non_git_wt_script_head(backup_repo: str, workdir: str) -> str:
        """远程非git work-tree 操作脚本公共头"""
        specs = ToolBackupManager._worktree_exclude_pathspec()
        return (
            "import subprocess, os, sys, json, uuid, shutil\n"
            f"backup_repo = {backup_repo!r}\n"
            f"workdir = {workdir!r}\n"
            f"specs = {specs!r}\n"
            "GITD = ['--git-dir', os.path.join(backup_repo, '.git'), '--work-tree', workdir]\n"
            "def git(*a, env=None, timeout=120):\n"
            "    e = dict(os.environ)\n"
            "    if env: e.update(env)\n"
            "    r = subprocess.run(['git'] + GITD + list(a), cwd=workdir, env=e,\n"
            "                       capture_output=True, timeout=timeout)\n"
            "    class R: pass\n"
            "    rr = R(); rr.returncode = r.returncode\n"
            "    rr.stdout = r.stdout.decode('utf-8', 'replace')\n"
            "    rr.stderr = r.stderr.decode('utf-8', 'replace')\n"
            "    return rr\n"
            "out = {}\n"
        )

    @staticmethod
    def _non_git_check_worktree(
        workdir: str, backup_repo: str, commit_hash: str,
        nest_name: str | None = None,
        with_trees: bool = False,
        task_id: int = 0,
    ) -> tuple[list[str], list[str]] | tuple[list[str], list[str], str, str] | None:
        """work-tree 变更检测：🔧 持久化增量索引 write-tree 对比快照 commit tree

        索引按 task 持久保留（backup_repo 下），首次 add -A 全量一次，
        之后依赖 git stat 缓存增量更新 O(变更数)。

        Returns:
            (changed_files, added_files) 相对路径列表；
            with_trees=True 时返回 (changed, added, backup_tree, current_tree)；
            None 表示检测失败
        """
        specs = ToolBackupManager._worktree_exclude_pathspec()
        # 🔧 事务#2088 持久化增量索引（按task命名，跨检测/快照增量复用）
        idx = os.path.join(backup_repo, f"peng_wt_idx_task{int(task_id) or 0}")
        try:
            if not nest_name or nest_name == "local":
                git_dir = os.path.join(backup_repo, ".git")
                base = ["--git-dir", git_dir, "--work-tree", workdir]
                env = {"GIT_INDEX_FILE": idx}
                subprocess.run(
                    ["git"] + base + ["add", "-A", "--"] + specs,
                    cwd=workdir, capture_output=True, text=True, timeout=300,
                )
                r = subprocess.run(
                    ["git"] + base + ["write-tree"], cwd=workdir,
                    capture_output=True, text=True, timeout=60,
                )
                if r.returncode != 0:
                    return None
                current_tree = r.stdout.strip()
                r = subprocess.run(
                    ["git"] + base + ["rev-parse", f"{commit_hash}^{{tree}}"], cwd=workdir,
                    capture_output=True, text=True, timeout=30,
                )
                if r.returncode != 0:
                    return None
                backup_tree = r.stdout.strip()
                bt, ct = backup_tree, current_tree
                if current_tree == backup_tree:
                    return ([], [], bt, ct) if with_trees else ([], [])
                r = subprocess.run(
                    ["git"] + base + ["diff", "--name-status", backup_tree, current_tree],
                    cwd=workdir, capture_output=True, text=True, timeout=60,
                )
                raw = r.stdout
            else:
                py = ToolBackupManager._non_git_wt_script_head(backup_repo, workdir) + (
                    f"commit_hash = {commit_hash!r}\n"
                    f"idx_rel = {os.path.basename(idx)!r}\n"
                    "idx = os.path.join(backup_repo, idx_rel)\n"
                    "try:\n"
                    "    git('add', '-A', '--', *specs, env={'GIT_INDEX_FILE': idx}, timeout=300)\n"
                    "    r = git('write-tree', env={'GIT_INDEX_FILE': idx})\n"
                    "    if r.returncode != 0:\n"
                    "        out['error'] = 'write-tree: ' + r.stderr[:200]\n"
                    "    else:\n"
                    "        current = r.stdout.strip()\n"
                    "        r = git('rev-parse', commit_hash + '^{tree}')\n"
                    "        if r.returncode != 0:\n"
                    "            out['error'] = 'rev-parse: ' + r.stderr[:200]\n"
                    "        else:\n"
                    "            backup = r.stdout.strip()\n"
                    "            out['backup_tree'] = backup\n"
                    "            out['current_tree'] = current\n"
                    "            if current == backup:\n"
                    "                out['same'] = True\n"
                    "            else:\n"
                    "                r = git('-c', 'core.quotepath=false', 'diff', '--name-status', backup, current)\n"
                    "                out['diff'] = r.stdout\n"
                    "except Exception as e:\n"
                    "    out['error'] = str(e)\n"
                    "print(json.dumps(out))\n"
                )
                result = _run_python_remote(py, timeout=360, nest_name=nest_name)
                if result.returncode != 0:
                    logger.warning(f"worktree检测远程失败({nest_name}): {str(result.stderr)[:200]}")
                    return None
                try:
                    data = json.loads(result.stdout.strip().split("\n")[-1])
                except (json.JSONDecodeError, IndexError):
                    return None
                if data.get("error"):
                    logger.warning(f"worktree检测git错误({nest_name}): {data['error'][:200]}")
                    return None
                if data.get("same"):
                    return (
                        ([], [], data.get("backup_tree", ""), data.get("current_tree", ""))
                        if with_trees else ([], [])
                    )
                bt = data.get("backup_tree", "")
                ct = data.get("current_tree", "")
                raw = data.get("diff", "")
            changed: list[str] = []
            added: list[str] = []
            for line in raw.split("\n"):
                if not line.strip():
                    continue
                parts = line.split("\t", 1)
                status = parts[0]
                filepath = parts[1] if len(parts) > 1 else ""
                if status.startswith("A"):
                    added.append(filepath)
                else:
                    changed.append(filepath)
            if with_trees:
                return changed, added, bt, ct
            return changed, added
        finally:
            # 🔧 持久化增量索引保留（跨检测增量复用；损坏时下次自动重建）
            pass

    @staticmethod
    def _non_git_rollback_worktree(
        workdir: str, backup_repo: str, commit_hash: str,
        checkout_files: list[str], added_files: list[str],
        nest_name: str | None = None,
    ) -> tuple[int, int]:
        """work-tree 回滚：checkout 恢复改动/删除文件 + 删除快照后新增文件

        Returns:
            (恢复文件数, 删除新增文件数)
        """
        restored = deleted = 0
        checkout_files = [f for f in checkout_files if f not in added_files]
        if not nest_name or nest_name == "local":
            git_dir = os.path.join(backup_repo, ".git")
            base = ["--git-dir", git_dir, "--work-tree", workdir]
            if checkout_files:
                r = subprocess.run(
                    ["git"] + base + ["checkout", commit_hash, "--"] + checkout_files,
                    cwd=workdir, capture_output=True, text=True, timeout=120,
                )
                if r.returncode != 0:
                    for fp in checkout_files:
                        subprocess.run(
                            ["git"] + base + ["checkout", commit_hash, "--", fp],
                            cwd=workdir, capture_output=True, text=True, timeout=30,
                        )
                restored = len(checkout_files)
            for fp in added_files:
                p = Path(workdir) / fp
                try:
                    if p.is_file():
                        p.unlink()
                    elif p.is_dir():
                        shutil.rmtree(str(p))
                    deleted += 1
                except OSError:
                    pass
            return restored, deleted
        # 远程：打包单脚本（bytes 传输 diff 无关，仅命令）
        py = ToolBackupManager._non_git_wt_script_head(backup_repo, workdir) + (
            f"commit_hash = {commit_hash!r}\n"
            f"checkout_files = {checkout_files!r}\n"
            f"added_files = {added_files!r}\n"
            "restored = deleted = 0\n"
            "if checkout_files:\n"
            "    r = git('checkout', commit_hash, '--', *checkout_files)\n"
            "    if r.returncode != 0:\n"
            "        for fp in checkout_files:\n"
            "            git('checkout', commit_hash, '--', fp)\n"
            "    restored = len(checkout_files)\n"
            "import pathlib\n"
            "for fp in added_files:\n"
            "    p = pathlib.Path(workdir) / fp\n"
            "    try:\n"
            "        if p.is_file(): p.unlink()\n"
            "        elif p.is_dir(): shutil.rmtree(str(p))\n"
            "        deleted += 1\n"
            "    except OSError: pass\n"
            "out['restored'] = restored\n"
            "out['deleted'] = deleted\n"
            "print(json.dumps(out))\n"
        )
        result = _run_python_remote(py, timeout=180, nest_name=nest_name)
        if result.returncode != 0:
            logger.warning(f"worktree回滚远程失败({nest_name}): {str(result.stderr)[:200]}")
            return 0, 0
        try:
            data = json.loads(result.stdout.strip().split("\n")[-1])
            return int(data.get("restored", 0)), int(data.get("deleted", 0))
        except (json.JSONDecodeError, IndexError, ValueError):
            return 0, 0


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
        """本地文件同步（增量：只复制新增/修改文件，跳过排除目录与 .gitignore 命中文件）

        🔧 事务#1660：不再"清空+全量复制"。未修改文件 mtime/size 不变直接跳过；
        build/、build_*/等产物目录整个排除；.gitignore 忽略的文件不备份。
        目标端多余文件（源端删除或被排除的）删除，保持镜像语义。
        """
        spec = _load_gitignore_spec(workdir)
        copied, removed = _sync_dir_incremental(Path(workdir), Path(backup_repo), spec)
        logger.debug(f"备份仓库增量同步: copied={copied} removed={removed}")

    @staticmethod
    def _sync_to_backup_repo_remote(workdir: str, backup_repo: str, nest_name: str) -> None:
        """远程文件同步（跨平台，通过Python脚本；增量+排除规则，同本地语义）"""
        code = (
            "import shutil, os\n"
            f"workdir = {workdir!r}\n"
            f"backup_repo = {backup_repo!r}\n"
            f"excl_dirs = {sorted(_FORCE_EXCLUDE_DIR_NAMES)!r}\n"
            f"excl_prefixes = {_FORCE_EXCLUDE_DIR_PREFIXES!r}\n"
            f"use_gitignore = {True!r}\n"
            "def is_excluded(name):\n"
            "    return name in excl_dirs or name.startswith(excl_prefixes)\n"
            "spec = None\n"
            "if use_gitignore:\n"
            "    try:\n"
            "        import pathspec\n"
            "        gi = os.path.join(workdir, '.gitignore')\n"
            "        if os.path.isfile(gi):\n"
            "            spec = pathspec.PathSpec.from_lines('gitwildmatch', open(gi, encoding='utf-8', errors='replace').read().splitlines())\n"
            "    except Exception:\n"
            "        spec = None\n"
            "def iter_files(root):\n"
            "    stack = [root]\n"
            "    out = []\n"
            "    while stack:\n"
            "        d = stack.pop()\n"
            "        try: entries = list(os.scandir(d))\n"
            "        except OSError: continue\n"
            "        for e in entries:\n"
            "            if e.name == '.git' or is_excluded(e.name): continue\n"
            "            if e.is_dir(follow_symlinks=False): stack.append(e.path)\n"
            "            else: out.append(os.path.relpath(e.path, root).replace(os.sep, '/'))\n"
            "    return out\n"
            "def need_copy(src, dst):\n"
            "    try:\n"
            "        s, d = os.stat(src), os.stat(dst)\n"
            "        return s.st_size != d.st_size or abs(s.st_mtime - d.st_mtime) > 1e-6\n"
            "    except OSError:\n"
            "        return True\n"
            "# 1. 源端应同步文件集（排除 .gitignore 命中）\n"
            "keep = {}\n"
            "for rel in iter_files(workdir):\n"
            "    if spec is not None and spec.match_file(rel): continue\n"
            "    keep[rel] = None\n"
            "# 2. 清理目标端多余文件 + 空目录（保留.git）\n"
            "for rel in iter_files(backup_repo):\n"
            "    if rel not in keep:\n"
            "        try: os.unlink(os.path.join(backup_repo, rel))\n"
            "        except OSError: pass\n"
            "for dirpath, dirnames, filenames in os.walk(backup_repo, topdown=False):\n"
            "    if dirpath == backup_repo or os.path.basename(dirpath) == '.git': continue\n"
            "    try:\n"
            "        if not any(os.scandir(dirpath)): os.rmdir(dirpath)\n"
            "    except OSError: pass\n"
            "# 3. 增量复制\n"
            "for rel in keep:\n"
            "    src = os.path.join(workdir, rel)\n"
            "    dst = os.path.join(backup_repo, rel)\n"
            "    if need_copy(src, dst):\n"
            "        os.makedirs(os.path.dirname(dst) or backup_repo, exist_ok=True)\n"
            "        shutil.copy2(src, dst)\n"
        )
        _run_python_remote(code, timeout=120, nest_name=nest_name)

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
        """复制外部文件到备份仓库的 __external__ 目录

        🔧 事务#1660：排除目录（build/等）直接跳过；size/mtime 未变化跳过复制。
        """
        # 排除规则命中（build/、build_*/等任意层级）→ 不备份
        if _is_abs_path_excluded(src_path):
            logger.debug(f"外部文件跳过（排除规则）: {src_path}")
            return
        # 🔧 事务#2088 护栏：盘根禁止；目录需深度≥2 且规模限额内
        if is_root_volume_path(src_path):
            logger.warning(f"外部文件备份跳过（盘根）: {src_path}")
            return
        _is_dir = os.path.isdir(src_path) if (
            not nest_name or nest_name == "local"
        ) else False  # 远程端 isdir 由内嵌脚本判定
        if _is_dir:
            _reason = _skip_reason_for_path(src_path, True)
            if _reason is None and not _dir_within_budget_local(src_path):
                _reason = f"目录超限额 {src_path}"
            if _reason is not None:
                logger.warning(f"外部文件备份跳过: {_reason}")
                return
        # 🔧 计算相对路径：去盘符（Windows 目录名不能含':'，含盘符路径会被
        # os.path.join/pathlib 视为绝对路径导致 dst 恒等于源文件）
        rel = _strip_drive_rel(src_path)
        dst = f"{backup_repo}/__external__/{rel}"

        if not nest_name or nest_name == "local":
            # 未修改（size/mtime 相同）→ 跳过复制
            if not _need_copy(src_path, dst):
                return
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            if os.path.isfile(src_path):
                shutil.copy2(src_path, dst)
            elif os.path.isdir(src_path):
                shutil.copytree(src_path, dst, dirs_exist_ok=True)
        else:
            code = (
                "import shutil, os, ntpath\n"
                f"src = {src_path!r}\n"
                f"dst = {dst!r}\n"
                # 🔧 事务#2088：远程目录护栏（盘根/浅目录/超限），文件不受限
                "if os.path.isdir(src):\n"
                "    _c = str(src).replace(chr(92), '/')\n"
                "    _r = ntpath.splitdrive(_c)[1]\n"
                "    segs = len([s for s in _r.split('/') if s])\n"
                "    ok = segs >= 2\n"
                "    if ok:\n"
                "        n, total, stack = 0, 0, [src]\n"
                "        while stack and ok:\n"
                "            d = stack.pop()\n"
                "            try: es = list(os.scandir(d))\n"
                "            except OSError: continue\n"
                "            for e in es:\n"
                "                try:\n"
                "                    if e.is_dir(follow_symlinks=False): stack.append(e.path)\n"
                "                    elif e.is_file(follow_symlinks=False):\n"
                "                        n += 1; total += e.stat().st_size\n"
                "                except OSError: continue\n"
                "            if n > 20000 or total > 5*1024*1024*1024: ok = False\n"
                "    if not ok: raise SystemExit(0)\n"
                "if os.path.isfile(src):\n"
                "    need = True\n"
                "    if os.path.exists(dst):\n"
                "        try:\n"
                "            s, d = os.stat(src), os.stat(dst)\n"
                "            need = s.st_size != d.st_size or abs(s.st_mtime - d.st_mtime) > 1e-6\n"
                "        except OSError: pass\n"
                "    if need:\n"
                "        os.makedirs(os.path.dirname(dst), exist_ok=True)\n"
                "        shutil.copy2(src, dst)\n"
                "elif os.path.isdir(src): shutil.copytree(src, dst, dirs_exist_ok=True)\n"
            )
            _run_python_remote(code, timeout=30, nest_name=nest_name)

    @staticmethod
    def rollback(
        rollback_meta: dict[str, Any],
        *,
        rollback_files: bool = True,
        rollback_external: bool = True,
        rollback_checklist: bool = True,
        rollback_context: bool = True,
    ) -> tuple[bool, str, dict | None, bool]:
        """执行回滚操作

        文件回滚（在 nest 上）和 history 恢复（在本地）完全独立。

        🔧 回滚内容勾选框（事务#2476）：四个维度独立控制——
        工作目录文件/外部文件/checklist/上下文（与确认窗口勾选框一一对应，
        默认全 True=全选）；未勾选的维度不检测、不恢复。

        🔧 无变化快速路径：回滚前动态预检测所选恢复对象（工作区tree/外部文件/
        history/checklist），均无实际变化时不创建undo快照、不执行任何恢复操作，
        直接返回 changed=False（调用方据此跳过output记录与回滚链接生成）。

        Args:
            rollback_meta: 回滚元数据
            rollback_files: 是否回滚工作目录文件（workdir 内文件）
            rollback_external: 是否回滚外部文件（workdir 外文件）
            rollback_checklist: 是否回滚 checklist
            rollback_context: 是否回滚对话上下文

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

        # 汇总未勾选的维度（消息中如实告知用户）
        scope_labels = {
            "files": "工作目录文件", "external": "外部文件",
            "checklist": "checklist", "context": "上下文",
        }
        unchecked = [
            scope_labels[k] for k, v in (
                ("files", rollback_files), ("external", rollback_external),
                ("checklist", rollback_checklist), ("context", rollback_context),
            ) if not v
        ]
        skipped_msg = f"（未回滚：{'、'.join(unchecked)}）" if unchecked else ""

        try:
            # 0. 🔧 回滚预检测：动态检测勾选维度（文件/history/checklist/外部文件）
            #    实际变化，并将检测到的精确变更清单合并进 rollback_meta
            has_actual_changes = ToolBackupManager._detect_rollback_changes(
                rollback_meta, workdir, nest_name, is_iteration,
                rollback_files=rollback_files, rollback_external=rollback_external,
                rollback_checklist=rollback_checklist, rollback_context=rollback_context,
            )
            if not has_actual_changes:
                logger.info(
                    f"回滚跳过(task#{task_id}): 所选维度均无实际变化{skipped_msg}"
                )
                return True, f"所选维度均无实际变化，跳过回滚{skipped_msg}", None, False

            # 1. 回滚前创建 undo 快照（文件+history，用于撤销；保持全量，
            #    不随勾选变化——保证"撤销"永远可恢复完整状态）
            undo_meta = None
            if not is_undo:
                undo_meta = ToolBackupManager._create_undo_snapshot(
                    rollback_meta, workdir, task_id, nest_name
                )

            # 2. 文件回滚（在事务的 nest 上执行）
            if not rollback_files and not rollback_external:
                success, msg = True, "已跳过文件回滚"
            elif backup_type == "git":
                success, msg = ToolBackupManager._rollback_git(
                    rollback_meta, nest_name,
                    rollback_files=rollback_files, rollback_external=rollback_external,
                )
            elif backup_type == "non_git":
                success, msg = ToolBackupManager._rollback_non_git(
                    rollback_meta, nest_name,
                    rollback_files=rollback_files, rollback_external=rollback_external,
                )
            elif backup_type == "external":
                if not rollback_external:
                    success, msg = True, "已跳过外部文件回滚"
                else:
                    # 🔧 非git workdir 快照：从备份仓库恢复涉及文件（初始设计）
                    ext_files = rollback_meta.get("external_files", [])
                    ext_commit = rollback_meta.get("external_backup_commit")
                    n = ExternalFileBackup.restore(
                        task_id, ext_files, ext_commit, nest_name=nest_name
                    )
                    success, msg = True, f"已从备份仓库恢复 {n} 个文件"
            elif backup_type == "history_only":
                # 仅 history 备份成功的迭代快照：只恢复上下文，不动文件
                success, msg = True, "无文件快照，跳过文件回滚"
            else:
                return False, f"未知的备份类型: {backup_type}", None, True

            if success:
                # 3. history 恢复（在本地执行，与 nest 无关）——
                #    上下文/checklist 两维度均未勾选时整体不恢复
                history_commit = rollback_meta.get("history_commit")
                if history_commit and (rollback_context or rollback_checklist):
                    ctx_msg = HistoryBackupManager.restore(
                        task_id, history_commit,
                        trim_tool_call=not is_undo and not is_iteration,
                        restore_context=rollback_context,
                        restore_checklist=rollback_checklist,
                    )
                    if ctx_msg:
                        msg += f"，{ctx_msg}"

                # 迭代快照：从外部文件基线 commit 恢复 workdir 外文件
                # （🔧 多仓库：str=旧格式默认仓库；dict=各仓库基线；
                #  🔧 事务#2107：有 manifest 时恢复集以其为准——本事务备份的
                #  精确清单，防共享盘符仓库全量恢复混入他 task 文件）
                if is_iteration and rollback_external:
                    ext_head = rollback_meta.get("external_head_commit")
                    if ext_head:
                        ext_restored = ExternalFileBackup.restore_all(
                            task_id, ext_head, nest_name=nest_name,
                            workdir=rollback_meta.get("workdir", ""),
                            manifest=rollback_meta.get("external_manifest"),
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

            return success, msg + skipped_msg, undo_meta if success else None, success
        except Exception as e:
            logger.error(f"回滚失败: {e}", exc_info=True)
            return False, f"回滚失败: {e}", None, False

    @staticmethod
    def _detect_rollback_changes(
        rollback_meta: dict[str, Any],
        workdir: str,
        nest_name: str | None,
        is_iteration: bool,
        *,
        rollback_files: bool = True,
        rollback_external: bool = True,
        rollback_checklist: bool = True,
        rollback_context: bool = True,
    ) -> bool:
        """回滚预检测：检测勾选维度是否有实际变化，并填充精确变更清单

        检测项（与回滚实际恢复的对象一一对应；🔧 未勾选的维度不检测）：
        1. 工作区文件（git tree / 备份仓库 tree 对比快照 commit）→
           更新 changed_files/added_files/backup_tree/current_tree
        2. 外部文件：迭代快照对 external_head_commit 基线全量对比；
           工具快照对 external_backup_commit 对比 external_files 列表
        3. history + checklist：HistoryBackupManager.has_changes（剥离统计字段）

        检测失败时保守返回 True（宁可多回滚不可漏恢复）。

        Returns:
            True=勾选维度至少一项有实际变化（需要执行回滚）；False=全部无变化
        """
        backup_type = rollback_meta.get("type", "")
        task_id = int(rollback_meta.get("task_id", 0) or 0)

        # 1. 工作区文件检测（git/non_git 均支持；检测结果合并进 rollback_meta；
        #    🔧 未勾选时跳过检测——changed_files 保留 meta 原值（仅信息用途））
        files_changed = False
        if rollback_files and backup_type in ("git", "non_git"):
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
        if rollback_external:
            if is_iteration:
                ext_head = rollback_meta.get("external_head_commit")
                if ext_head:
                    try:
                        # 🔧 事务#2107：优先 manifest（本事务备份文件的精确清单），
                        # 轻过滤（不剔 git——外部/嵌套 git 仓库文件是合法备份对象）；
                        # 旧 meta 无 manifest → ls-tree 全量 + 全量 git 剔除兜底
                        ext_manifest = rollback_meta.get("external_manifest")
                        if ext_manifest:
                            abs_paths = ToolBackupManager._filter_manifest_paths(
                                list(ext_manifest), workdir,
                            )
                        else:
                            abs_paths = ExternalFileBackup.list_baseline_files(
                                task_id, ext_head, nest_name=nest_name
                            )
                            abs_paths = ToolBackupManager._filter_baseline_files(
                                abs_paths, workdir, nest_name
                            )
                        if abs_paths and ExternalFileBackup.check_changes(
                            task_id, abs_paths, ext_head, nest_name=nest_name,
                            strict_baseline=True,
                        ):
                            ext_changed_flag = True
                    except Exception as e:
                        logger.debug(f"外部文件基线检测失败(task#{task_id}): {e}")
            else:
                external_files = rollback_meta.get("external_files", [])
                # 🔧 事务#2088：旧 meta 可能含 git 管理文件（斜杠归一化 bug 时期的
                # 误备份）——检测前过滤，防止误报变更/误恢复跨事务文件。
                # 🔧 事务#2107：新 meta（workdir_scope）只剔除 workdir 仓库内文件，
                # 外部/嵌套 git 仓库文件保留检测（它们有本事务 external 备份）
                if external_files:
                    external_files = ToolBackupManager._filter_unbackupable(
                        external_files, workdir, nest_name,
                        scope=ToolBackupManager._meta_scope(rollback_meta),
                    )
                    rollback_meta["external_files"] = external_files
                ext_commit = rollback_meta.get("external_backup_commit")
                if external_files and ExternalFileBackup.check_changes(
                    task_id, external_files, ext_commit, nest_name=nest_name,
                    strict_baseline=True,
                ):
                    ext_changed_flag = True

        # 3. history + checklist 检测（两维度均未勾选时不检测；
        #    has_changes 按维度比较——上下文与 checklist 独立勾选）
        history_changed = False
        history_commit = rollback_meta.get("history_commit")
        if history_commit and task_id and (rollback_context or rollback_checklist):
            history_changed = HistoryBackupManager.has_changes(
                task_id, history_commit,
                include_context=rollback_context,
                include_checklist=rollback_checklist,
            )

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
        和外部文件（通过 ExternalFileBackup；Windows 盘符文件入同盘符
        /.peng/backups 仓库，其余入本地 data/file_backups）。
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
                # 🔧 非git workdir：备份将被回滚覆盖的文件当前版本（external 模式，
                # 进 \.peng\backups 备份仓库；初始设计，绝不全量扫描 workdir）
                undo_files = [
                    f for f in original_meta.get("external_files", [])
                    if _is_abs_cross_platform(f)
                ] or [
                    f for f in original_meta.get("changed_files", [])
                    if _is_abs_cross_platform(f)
                ]
                spec = (
                    ExternalFileBackup.backup(
                        task_id, undo_files, f"undo @ {timestamp}", nest_name=nest_name
                    ) if undo_files else None
                )
                meta = None
                if spec:
                    meta = {
                        "type": "external",
                        "task_id": task_id,
                        "tool_name": "rollback_undo",
                        "commit_hash": (spec if isinstance(spec, str) else json.dumps(spec))[:40],
                        "workdir": workdir,
                        "nest_name": nest_name or "local",
                        "file_paths": [],
                        "external_files": undo_files,
                        "external_backup_commit": spec,
                        "timestamp": timestamp,
                        "rollback_cmd": "",
                    }

            if meta:
                meta["tool_name"] = f"撤销:{original_meta.get('tool_name', 'unknown')}"
                meta["timestamp"] = timestamp

                # 外部文件备份（在本地，与 nest 无关）
                original_external = original_meta.get("external_files", [])
                # 🔧 事务#2107：按原 meta 的 policy 选 scope——旧 meta 保持全量
                # git 剔除（跨事务污染防线）；新 meta（workdir_scope）仅剔除
                # workdir 仓库内文件，外部/嵌套 git 仓库文件进 undo 备份
                original_external = ToolBackupManager._filter_unbackupable(
                    original_external, original_meta.get("workdir", "") or workdir,
                    nest_name, scope=ToolBackupManager._meta_scope(original_meta),
                ) if original_external else original_external
                if original_external:
                    undo_ext_commit = ExternalFileBackup.backup(
                        task_id,
                        original_external,
                        f"undo:{original_meta.get('tool_name', 'unknown')} @ {timestamp}",
                        nest_name=nest_name,
                    )
                    meta["external_files"] = original_external
                    meta["external_backup_commit"] = undo_ext_commit
                    # 🔧 事务#2107：undo 备份同样累积 manifest（撤销撤销的恢复集）
                    manifest_add(task_id, original_external)
                    if original_meta.get("backup_policy") == "workdir_scope":
                        meta["backup_policy"] = "workdir_scope"

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
    def _rollback_git(
        rollback_meta: dict[str, Any],
        nest_name: str | None,
        *,
        rollback_files: bool = True,
        rollback_external: bool = True,
    ) -> tuple[bool, str]:
        """从git备份分支回退

        对于修改的文件：git checkout <commit> -- <files>
        对于新增的文件（快照后创建的）：直接删除

        🔧 勾选框维度控制（事务#2476）：工作目录文件与外部文件独立恢复——
        rollback_files=False 跳过 workdir checkout/删除；rollback_external=False
        跳过外部文件恢复。
        """
        commit_hash = rollback_meta["commit_hash"]
        workdir = rollback_meta["workdir"]
        # 优先使用 check_post_tool_changes 检测到的实际变更文件
        changed_files = rollback_meta.get("changed_files", [])
        added_files = rollback_meta.get("added_files", [])  # 快照后新增的文件

        # 回退修改/删除的文件（checkout from commit）
        checkout_files = [f for f in changed_files if f not in added_files]

        try:
            total = 0
            deleted_count = 0
            if rollback_files:
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
            # （未勾选维度的提示由 rollback() 的 skipped_msg 统一拼接）
            if rollback_external:
                external_files = rollback_meta.get("external_files", [])
                if external_files:
                    task_id = rollback_meta.get("task_id", 0)
                    ext_commit = rollback_meta.get("external_backup_commit")
                    ext_restored = ExternalFileBackup.restore(
                        task_id, external_files, ext_commit, nest_name=nest_name
                    )
                    if ext_restored:
                        msg += f"，恢复 {ext_restored} 个外部文件"

            logger.info(f"git回退成功: {msg}")
            return True, msg
        except Exception as e:
            return False, f"git回退异常: {e}"

    @staticmethod
    def _rollback_non_git(
        rollback_meta: dict[str, Any],
        nest_name: str | None,
        *,
        rollback_files: bool = True,
        rollback_external: bool = True,
    ) -> tuple[bool, str]:
        """从非git备份仓库回退（🔧 worktree 模式：--work-tree checkout 恢复+删除新增；
        旧 mirror 模式兼容）

        changed_files 可能包含 workdir 内文件和 __external__/ 前缀的外部文件，
        需要分离处理。external_files 保存的是原始绝对路径，也需恢复。

        🔧 勾选框维度控制（事务#2476）：rollback_files=False 跳过 workdir 文件
        恢复（worktree checkout/mirror 复制/删除新增/全量同步）；rollback_external=False
        跳过外部文件恢复（__external__/ 复制与 ExternalFileBackup）。
        """
        backup_repo = rollback_meta["backup_repo"]
        workdir = rollback_meta["workdir"]
        commit_hash = rollback_meta["commit_hash"]
        all_changed = rollback_meta.get("changed_files", []) or rollback_meta.get("file_paths", [])
        added_files = rollback_meta.get("added_files", [])
        external_files = rollback_meta.get("external_files", []) if rollback_external else []

        # 分离 changed_files 中的 workdir 文件和外部文件
        wd_changed: list[str] = []
        for fp in all_changed:
            if fp.startswith("__external__/"):
                # 外部文件变更——由 external_files 统一处理，跳过
                continue
            wd_changed.append(fp)

        try:
            # 🔧 worktree 模式：checkout 到 workdir + 删除快照后新增文件（零镜像）
            if rollback_meta.get("repo_mode") == "worktree":
                restored, deleted = (0, 0)
                if rollback_files:
                    restored, deleted = ToolBackupManager._non_git_rollback_worktree(
                        workdir, backup_repo, commit_hash, wd_changed, added_files, nest_name
                    )
                msg = f"已回退 {restored} 个文件到 {commit_hash[:8]}"
                if deleted:
                    msg += f"（含删除 {deleted} 个新增文件）"
                # 外部文件恢复（ExternalFileBackup，rollback 统一入口已处理，
                # 此处补充恢复以覆盖 undo 场景 external_backup_commit）
                task_id_val = rollback_meta.get("task_id", 0)
                ext_commit = rollback_meta.get("external_backup_commit")
                if external_files and task_id_val:
                    ext_restored = ExternalFileBackup.restore(
                        task_id_val, external_files, ext_commit, nest_name=nest_name
                    )
                    if ext_restored:
                        msg += f"，恢复 {ext_restored} 个外部文件"
                logger.info(f"非gitworktree回退成功: {msg}")
                return True, msg

            # 旧 mirror 模式（历史 meta）：从备份仓库工作区复制恢复
            # 强制从commit恢复备份仓库工作树文件（因为 _check_changes_non_git 会修改工作树但不commit）
            _run_git(["checkout", commit_hash, "--", "."], cwd=backup_repo, nest_name=nest_name)

            restored_count = 0

            if rollback_files and wd_changed:
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
                # 🔧 rel 与 _copy_external_file 一致去盘符（Windows 目录名不能含':'）
                for ext_fp in external_files:
                    rel = _strip_drive_rel(ext_fp)
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
                        task_id_val, external_files, ext_commit, nest_name=nest_name
                    )
                    if ext_restored:
                        restored_count = max(restored_count, ext_restored)

            if rollback_files and not wd_changed and not external_files and not added_files:
                ToolBackupManager._sync_from_backup_repo(backup_repo, workdir, nest_name)

            # 删除快照后新增的文件（这些文件在快照时不存在）
            deleted_count = 0
            if rollback_files:
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

    # bash/cmd 命令中会修改/删除文件的动词（basename 匹配）
    _CMD_FILE_VERBS = frozenset({
        "rm", "rmdir", "mv", "cp", "touch", "truncate", "shred", "unlink", "tee",
        "del", "erase", "move", "copy", "ren", "rename", "rd",
    })
    # 命令分段符（; | && || 换行）
    _CMD_SEG_SPLIT = re.compile(r";|\|\||&&|\||\n")
    # 脚本扩展名与解释器动词（🔧 执行脚本的命令需读取脚本内容提取内部路径）
    _SCRIPT_EXTS = (".py", ".js", ".mjs", ".ts", ".sh", ".bash", ".bat", ".cmd", ".ps1")
    _INTERPRETER_VERBS = frozenset({
        "python", "python3", "python.exe", "py", "node", "node.exe", "deno",
        "bash", "sh", "zsh", "pwsh", "powershell", "cmd", "cmd.exe", "wsl",
    })

    @staticmethod
    def _tokens_of(segment: str) -> list[str]:
        """命令段 token 化（保留引号内空格路径；去引号）"""
        raw = re.findall(r'"([^"]*)"|\'([^\']*)\'|(\S+)', segment)
        return [a or b or c for a, b, c in raw]

    @staticmethod
    def _read_text_file(path: str, nest_name: str | None) -> str:
        """读文本文件内容（本地直读/远程单HTTP；失败返回空）"""
        try:
            if not nest_name or nest_name == "local":
                return Path(path).read_text(encoding="utf-8", errors="replace")
            py = (
                "import json\n"
                f"print(json.dumps(open({path!r}, encoding='utf-8', errors='replace').read()))\n"
            )
            r = _run_python_remote(py, timeout=15, nest_name=nest_name)
            if r.returncode == 0:
                return json.loads(r.stdout.strip().split("\n")[-1])
        except Exception as e:
            logger.debug(f"读取脚本内容失败({path}): {e}")
        return ""

    @staticmethod
    def _extract_paths_from_script(
        script_path: str, workdir: str | None, nest_name: str | None,
    ) -> list[str]:
        """读取脚本内容，提取其中涉及的文件路径（🔧 事务#1847 第八轮）

        生产需求：`python a.py` 命令行只有一个脚本参数，脚本内部 open()/删除的
        其他文件必须靠读脚本源码提取。策略：宽进严出——正则提取"路径样式"
        token（带扩展名、引号内或裸词），相对路径以 workdir 与脚本所在目录
        双基准解析，最终存在性过滤兜底误报（os.path 之类的模块名解析后
        不存在，自然被滤掉）。
        """
        content = ToolBackupManager._read_text_file(script_path, nest_name)
        if not content:
            return []
        wd = str(workdir) if workdir else ""
        script_dir = os.path.dirname(script_path)
        pat = re.compile(
            r"[\'\"]((?:[A-Za-z]:)?[\w./\\\- ]+\.\w{1,8})[\'\"]"
            r"|(?<![\'\"\w/\\])((?:[A-Za-z]:)?[\w./\\\-]+\.\w{1,8})"
        )
        # 🔧 事务#2088：先收集全部候选（保序，双base优先序），最后批量
        # 存在性判定（单次HTTP）——旧实现逐token一次HTTP，数百token直接
        # 耗尽快照60s预算（task#2090）
        candidate_bases: list[list[str]] = []  # 每个 token 的按序 base 候选
        seen_tok: set[str] = set()
        for m in pat.finditer(content):
            tok = (m.group(1) or m.group(2) or "").strip()
            if not tok or len(tok) > 260:
                continue
            if tok.startswith(("http:", "https:", "ftp:")):
                continue
            k = tok.lower()
            if k in seen_tok:
                continue
            seen_tok.add(k)
            if _is_abs_cross_platform(tok):
                bases = [ToolBackupManager._join_norm("", tok)]
            else:
                bases = [
                    ToolBackupManager._join_norm(base, tok)
                    for base in (wd, script_dir) if base
                ]
            bases = [
                p for p in bases
                if not _is_abs_path_excluded(p) and not is_root_volume_path(p)
            ]
            if bases:
                candidate_bases.append(bases)
        # 展平去重后单次批量判定
        flat: list[str] = []
        seen_p: set[str] = set()
        for bases in candidate_bases:
            for p in bases:
                if p not in seen_p:
                    seen_p.add(p)
                    flat.append(p)
        existing = _paths_exist_batch(flat, nest_name)
        out: list[str] = []
        for bases in candidate_bases:
            # 保序取第一个存在的 base（与旧 break 语义一致）
            hit = next((p for p in bases if p in existing), None)
            if hit is not None:
                out.append(hit)
        return out

    @staticmethod
    def _win_style(p: str) -> bool:
        return bool(re.match(r"^[A-Za-z]:[\\/]", p))

    @staticmethod
    def _join_norm(base: str, tok: str) -> str:
        """跨平台 join+normpath（Windows 盘符路径用 ntpath，避免 Linux 端
        os.path.join 产生 D:\\x/y 混合斜杠形态）"""
        if base and ToolBackupManager._win_style(base):
            return ntpath.normpath(ntpath.join(base, tok))
        joined = os.path.join(base, tok) if base else tok
        if ToolBackupManager._win_style(joined):
            return ntpath.normpath(joined)
        return os.path.normpath(joined)

    @staticmethod
    def _extract_paths_from_command(
        command: str, workdir: str | None = None, nest_name: str | None = None,
    ) -> list[str]:
        """从 bash/cmd 命令字符串提取将修改/删除的文件路径（🔧 事务#1847 第八轮）

        生产事故：task#1886 用 bash `del D:\\work\\README.md` 删除文件无备份——
        旧实现"bash 无法确定具体文件，返回空列表"。
        规则（保守而全面）：
        - 按 ; | && || 分段，段内 token 化（引号路径保留）
        - 动词（rm/del/mv/move/cp/copy/ren/rename/rmdir/rd/touch/truncate/shred/
          unlink/tee/erase，basename 匹配）后的非选项参数为涉及路径
          （mv/cp/move/copy 的 src 和 dst 都收——dst 原有文件会被覆盖也需备份）
        - 重定向目标（>file、>>file、独立 > 后跟 file）为写入路径
        - 选项过滤：- 开头；Windows 单字母开关 /q /s /f /a（^/[A-Za-z]?$，
          多段路径如 /home/x 不误伤）
        - 相对路径 join(workdir) 解析；含通配符的跳过（无法静态展开）
        - 最终仅保留真实存在的文件/目录（新增文件无需备份原版；删除不存在的
          文件无内容可备）——语义自洽
        """
        if not command:
            return []
        wd = str(workdir) if workdir else ""
        candidates: list[str] = []
        patterns: list[str] = []  # 含通配符的路径模式（命令前 glob 展开为具体文件）

        def resolve(tok: str, to_patterns: bool = True) -> str | None:
            tok = tok.strip().strip('"').strip("'")
            if not tok or not re.search(r"[A-Za-z0-9_.\-\u4e00-\u9fff]", tok):
                return None
            if _is_abs_cross_platform(tok):
                full = ToolBackupManager._join_norm("", tok)
            elif tok.startswith("~") or tok.startswith("%"):
                return None  # 环境变量/家目录展开不了，跳过
            elif wd:
                full = ToolBackupManager._join_norm(wd, tok)
            else:
                return None
            if to_patterns and any(w in full for w in "*?"):
                patterns.append(full)
                return None
            return full

        def is_option(tok: str) -> bool:
            if tok.startswith("-"):
                return True
            return bool(re.match(r"^/[A-Za-z]?$", tok))  # /q /s；/home/x 不误伤

        for seg in ToolBackupManager._CMD_SEG_SPLIT.split(command):
            tokens = ToolBackupManager._tokens_of(seg)
            i, n = 0, len(tokens)
            collecting = False
            while i < n:
                tok = tokens[i]
                base = re.split(r"[\\/]", tok)[-1].lower()
                stripped = tok.lstrip("(")
                base_s = re.split(r"[\\/]", stripped)[-1].lower()
                if base_s in ToolBackupManager._CMD_FILE_VERBS or base in ToolBackupManager._CMD_FILE_VERBS:
                    collecting = True
                    i += 1
                    continue
                if collecting:
                    # 重定向（动词后不再期望；由独立扫描处理）或控制符
                    if tok.startswith(">"):
                        redir = tok.lstrip(">").lstrip("(")
                        r = resolve(redir) if redir else None
                        if r:
                            candidates.append(r)
                        elif not redir and i + 1 < n:
                            r = resolve(tokens[i + 1])
                            if r:
                                candidates.append(r)
                            i += 1
                        collecting = False
                        i += 1
                        continue
                    if is_option(tok):
                        i += 1
                        continue
                    r = resolve(tok)
                    if r:
                        candidates.append(r)
                    i += 1
                    continue
                # 非收集状态：独立重定向目标（echo x > file）
                if tok.startswith(">"):
                    redir = tok.lstrip(">")
                    r = resolve(redir) if redir else None
                    if r:
                        candidates.append(r)
                    elif not redir and i + 1 < n:
                        r = resolve(tokens[i + 1])
                        if r:
                            candidates.append(r)
                        i += 1
                i += 1

        # 🔧 执行脚本检测（事务#1847 第八轮）：解释器（python/node/bash/...）后的
        # 脚本参数，或任何脚本扩展名 token → 读取脚本内容提取内部涉及路径
        # （命令行只有一个脚本参数，脚本内 open()/删除的其他文件靠源码提取）
        script_files: list[str] = []
        for seg in ToolBackupManager._CMD_SEG_SPLIT.split(command):
            tokens = ToolBackupManager._tokens_of(seg)
            expecting_script = False
            for tok in tokens:
                stripped_base = re.split(r"[\\/]", tok.lstrip("("))[-1].lower()
                if (
                    stripped_base in ToolBackupManager._INTERPRETER_VERBS
                    or stripped_base.startswith(("python", "node", "pythonw"))
                ):
                    expecting_script = True
                    continue
                if not tok or is_option(tok):
                    continue
                r = resolve(tok, to_patterns=False)
                if expecting_script:
                    if r and r.lower().endswith(ToolBackupManager._SCRIPT_EXTS):
                        script_files.append(r)
                    expecting_script = False
                elif tok.lower().endswith(ToolBackupManager._SCRIPT_EXTS) and r:
                    script_files.append(r)
        for sf in script_files[:3]:  # 限 3 个脚本，防失控
            try:
                if _path_exists(sf, nest_name):
                    candidates.extend(
                        ToolBackupManager._extract_paths_from_script(sf, wd, nest_name)
                    )
            except Exception:
                continue

        # 🔧 通配符模式在命令执行前展开为实际匹配文件（rm *.txt → 具体文件列表，
        # 配合命令前后对比检测实际变化）
        for pat in patterns:
            try:
                if not nest_name or nest_name == "local":
                    candidates.extend(sorted(glob.glob(pat)))
                else:
                    py = f"import glob, json\nprint(json.dumps(sorted(glob.glob({pat!r}))))\n"
                    r = _run_python_remote(py, timeout=15, nest_name=nest_name)
                    if r.returncode == 0:
                        candidates.extend(json.loads(r.stdout.strip().split("\n")[-1]))
            except Exception as e:
                logger.debug(f"命令通配符展开失败({pat}): {e}")

        # 去重 + 批量存在性过滤（🔧 事务#2088：单次HTTP替代逐路径N次，
        # 仅备份真实存在的对象原版；命令候选通常少量，脚本候选已在上游批量）
        seen: set[str] = set()
        unique: list[str] = []
        for p in candidates:
            k = p.replace("\\", "/").lower()
            if k in seen:
                continue
            seen.add(k)
            if _is_abs_path_excluded(p):
                continue
            # 🔧 事务#2088：盘根不提取（dir D:\ 之类的命令 token，
            # 备份它=备份全盘；workdir 内部路径不受影响——normpath 后不会是根）
            if is_root_volume_path(p):
                continue
            # 🔧 事务#2402：Windows 保留设备名不提取——cmd `2>nul` 的重定向
            # 目标 nul 被 join(workdir) 成 .../nul 后，设备名在 Windows 上
            # os.path.exists 恒真（伪存在），存在性过滤拦不住（task#2332
            # 的 D:/work/fcc/nul 死条目来源）
            if _is_windows_device_path(p):
                continue
            # 🔧 事务#2402：workdir 本身/祖先目录不提取（cp/mv 等动词参数里
            # 的 D:/work 是目录——进 external 后 hash-object 恒失败误报变更，
            # 恢复 open(目录,'wb') 会炸恢复脚本；workdir 内容归 git 快照管）
            if workdir and _is_workdir_ancestor(p, workdir):
                continue
            unique.append(p)
        existing = _paths_exist_batch(unique, nest_name)
        return [p for p in unique if p in existing]

    @staticmethod
    def _extract_file_paths(
        tool_name: str, tool_args: dict[str, Any], workdir: str | None = None,
        nest_name: str | None = None,
    ) -> list[str]:
        """从工具参数中提取涉及的文件路径（含 bash/cmd 命令解析，🔧 第八轮）"""
        paths: list[str] = []
        if tool_name in ("write", "edit", "line_edit"):
            fp = tool_args.get("filePath", "")
            if fp:
                paths.append(fp)
        elif tool_name in ("bash", "cmd"):
            command = str(tool_args.get("command", "") or "")
            paths.extend(
                ToolBackupManager._extract_paths_from_command(command, workdir, nest_name)
            )
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
    def _filter_baseline_files(
        file_paths: list[str], workdir: str, nest_name: str | None = None,
        scope: str = "all",
    ) -> list[str]:
        """过滤基线文件集：git 管理文件（嵌套仓库）+ 系统目录路径不参与
        检测/预览/恢复（🔧 事务#1847 第十轮：旧基线 commit 含历史误备份的
        fcc（git管理）与 Program Files/Temp 文件——回滚窗口数量虚高、回滚
        会错误恢复它们；新备份已过滤，旧基线数据靠此处动态过滤兜底）。
        🔧 事务#2107：scope 语义见 _filter_git_managed（新 meta 用 workdir——
        外部/嵌套 git 仓库文件是合法备份对象，不过滤）"""
        if not file_paths:
            return file_paths
        try:
            return ToolBackupManager._filter_unbackupable(
                file_paths, workdir, nest_name, scope
            )
        except Exception as e:
            logger.debug(f"基线文件过滤失败: {e}")
            return file_paths

    @staticmethod
    def preview_rollback_diff_with_external(rollback_meta: dict[str, Any]) -> dict[str, Any]:
        """回滚预览（含外部/非git管理文件的变更信息，🔧 第九轮）

        webui 回滚确认窗口的完整数据源：在 preview_rollback_diff（workdir 变更）
        基础上，追加"工作目录外或非git管理文件"的变更清单（备份仓库基线对比），
        让用户在确认回滚前看到全部将恢复的对象。
        🔧 事务#2107：迭代快照有 external_manifest 时恢复集以其为准（精确
        清单，轻过滤不剔 git）；工具快照按 meta policy 选过滤 scope。
        """
        result = ToolBackupManager.preview_rollback_diff(rollback_meta)
        if rollback_meta.get("type") == "external":
            return result  # external 分支的 changed_files 已是备份文件变更
        task_id = int(rollback_meta.get("task_id", 0) or 0)
        if not task_id:
            return result
        nest_name = rollback_meta.get("nest_name", "local")
        ext_commit = (
            rollback_meta.get("external_backup_commit")
            or rollback_meta.get("external_head_commit")
        )
        if not ext_commit:
            return result
        # 🔧 history_only（非git workdir 迭代快照）主体返回 success=False
        # （"仅上下文恢复"）——仍需补充外部/非git文件变更信息（生产事故：
        # task#1886 回滚窗口看不到文件变更就是因为这里提前 return）
        if not result.get("success") and rollback_meta.get("type") != "history_only":
            return result
        try:
            ext_manifest = rollback_meta.get("external_manifest")
            if ext_manifest:
                files = ToolBackupManager._filter_manifest_paths(
                    list(ext_manifest), rollback_meta.get("workdir"),
                )
            else:
                files = ExternalFileBackup.list_baseline_files(
                    task_id, ext_commit, nest_name=nest_name
                )
                files = ToolBackupManager._filter_baseline_files(
                    files, rollback_meta.get("workdir", ""), nest_name,
                    scope=ToolBackupManager._meta_scope(rollback_meta),
                )
            if not files:
                return result
            changed = ExternalFileBackup.check_changes(
                task_id, files, ext_commit, nest_name=nest_name,
                strict_baseline=True,
            )
            if changed:
                result["changed_files"] = list(result.get("changed_files") or []) + changed
                # 🔧 事务#2476：外部文件变更独立字段（前端回滚确认窗口勾选框
                # 联动——"外部文件"勾选框控制该区块显隐）
                result["external_changed_files"] = list(changed)
                result["message"] = (
                    (result.get("message") or "")
                    + f"；外部/非git管理文件变更 {len(changed)} 个"
                )
                if rollback_meta.get("type") == "history_only":
                    # 有可展示的文件变更：置 success 让前端渲染变更区块
                    # 🔧 事务#2402：保留"此快照无文件备份"警示——history_only
                    # 快照本身无 workdir 代码快照（曾因 nest 瞬断批量降级），
                    # 明示用户"项目代码不在本快照保护范围"而非笼统"无文件变更"
                    result["success"] = True
                    result["message"] = (
                        f"⚠️ 本快照未含项目代码备份（快照创建时备份暂不可用，"
                        f"仅上下文恢复）；外部/非git管理文件变更 {len(changed)} 个（见列表）"
                    )
        except Exception as e:
            logger.debug(f"回滚预览外部文件变更检测失败(task#{task_id}): {e}")
        return result

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

        # 🔧 external 模式（非git workdir 快照：涉及文件统一在备份仓库，初始设计）
        if backup_type == "external":
            ext_files = rollback_meta.get("external_files", [])
            # 🔧 事务#2088：git 管理文件不参与预览/恢复（旧 meta 误备份兜底）
            # 🔧 事务#2107：新 meta（workdir_scope）仅剔除 workdir 仓库内文件，
            # 外部/嵌套 git 仓库文件保留（它们有本事务备份，必须可见可恢复）
            if ext_files:
                ext_files = ToolBackupManager._filter_unbackupable(
                    ext_files, workdir, nest_name,
                    scope=ToolBackupManager._meta_scope(rollback_meta),
                )
            ext_commit = rollback_meta.get("external_backup_commit")
            changed = ExternalFileBackup.check_changes(
                int(rollback_meta.get("task_id", 0) or 0), ext_files, ext_commit,
                nest_name=nest_name,
            )
            if not changed:
                return {"success": True, "diff": "", "changed_files": [], "truncated": False,
                        "message": "无文件变更"}
            return {"success": True, "diff": "", "changed_files": changed, "truncated": False,
                    "message": f"共 {len(changed)} 个文件变更（见列表）"}

        # 🔧 worktree 模式的 non_git（本地/远程统一走 check_worktree 路径，
        # 不进 mirror 同步的 remote batch）
        if backup_type == "non_git" and rollback_meta.get("repo_mode") == "worktree":
            repo = rollback_meta.get("backup_repo", "")
            det = ToolBackupManager._non_git_check_worktree(
                workdir, repo, commit_hash, nest_name, with_trees=True,
                task_id=int(rollback_meta.get("task_id", 0) or 0),
            )
            if det is None:
                return {"success": False, "diff": "", "changed_files": [], "truncated": False,
                        "message": "变更检测失败"}
            changed, added, _bt, _ct = det
            if not changed and not added:
                return {"success": True, "diff": "", "changed_files": [], "truncated": False,
                        "message": "无文件变更"}
            changed_files = [f for f in (changed + added) if f]
            return {"success": True, "diff": "", "changed_files": changed_files,
                    "truncated": False,
                    "message": f"共 {len(changed_files)} 个文件变更（见列表）"}

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
            if backup_type == "non_git" and rollback_meta.get("repo_mode") == "worktree":
                # 🔧 worktree 模式：零镜像，持久索引对比快照 commit tree
                repo = rollback_meta.get("backup_repo", "")
                det = ToolBackupManager._non_git_check_worktree(
                    workdir, repo, commit_hash, nest_name, with_trees=True,
                    task_id=int(rollback_meta.get("task_id", 0) or 0),
                )
                if det is None:
                    return {"success": False, "diff": "", "changed_files": [], "truncated": False,
                            "message": "变更检测失败"}
                changed, added, backup_tree, current_tree = det
                if not changed and not added:
                    return {"success": True, "diff": "", "changed_files": [], "truncated": False,
                            "message": "无文件变更"}
                changed_files = [f for f in (changed + added) if f]
                diff_text = ""
                if not nest_name or nest_name == "local":
                    base = ["--git-dir", os.path.join(repo, ".git"), "--work-tree", workdir]
                    r = subprocess.run(
                        ["git"] + base + ["-c", "core.quotepath=false", "diff", "--no-color",
                                          current_tree, backup_tree],
                        cwd=workdir, capture_output=True, text=True, timeout=60,
                    )
                    if r.returncode == 0:
                        diff_text = r.stdout
                diff_text, truncated, truncated_bytes = _truncate_diff_by_bytes(diff_text)
                if not diff_text:
                    return {"success": True, "diff": "", "changed_files": changed_files,
                            "truncated": False,
                            "message": f"共 {len(changed_files)} 个文件变更（见列表）"}
                result = {
                    "success": True, "diff": diff_text, "changed_files": changed_files,
                    "truncated": truncated, "truncated_bytes": truncated_bytes, "message": "ok",
                }
                _preview_diff_cache[cache_key] = {"ts": now, "result": result}
                return result

            if backup_type == "non_git":
                # 旧 mirror 模式：同步当前工作区到备份仓库（不commit），使工作树反映当前状态
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

            # 当前工作区tree（🔧 持久化增量索引：与快照共用，增量 O(变更数)）
            task_id_prev = int(rollback_meta.get("task_id", 0) or 0)
            persist_idx = _persist_index_path(git_dir, task_id_prev)
            env = {"GIT_INDEX_FILE": persist_idx}
            try:
                _ensure_persist_index(persist_idx, git_dir, cwd, env, nest_name)
                _run_git(["add", "-A"], cwd=cwd, env=env, nest_name=nest_name, timeout=180)
                # 🔧 排除产物目录（与快照路径同规则）
                _purge_excluded_from_index(env, cwd, nest_name)
                result = _run_git(
                    ["write-tree"], cwd=cwd, env=env, nest_name=nest_name, timeout=60
                )
                if result.returncode != 0:
                    return {"success": False, "diff": "", "changed_files": [], "truncated": False,
                            "message": f"write-tree失败: {result.stderr[:100]}"}
                current_tree = result.stdout.strip()
            finally:
                # 持久索引保留（增量复用）
                pass

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
            f"task_id = {int(rollback_meta.get('task_id', 0) or 0)!r}\n"
            f"commit_hash = {commit_hash!r}\n"
            f"is_non_git = {backup_type == 'non_git'!r}\n"
            f"excludes = {sorted(_EXCLUDE_DIRS)!r}\n"
            f"excl_dirs = {sorted(_FORCE_EXCLUDE_DIR_NAMES)!r}\n"
            f"excl_prefixes = {_FORCE_EXCLUDE_DIR_PREFIXES!r}\n"
            f"max_diff_bytes = {_DIFF_PREVIEW_MAX_BYTES!r}\n"
            "result_data = {}\n"
            "temp_index = None\n"
            "try:\n"
            "    def git(*args, env=None):\n"
            "        full_env = dict(os.environ)\n"
            "        if env: full_env.update(env)\n"
            "        return subprocess.run(['git'] + list(args), cwd=cwd, env=full_env,\n"
            "                              capture_output=True, timeout=120)  # bytes模式\n"
            "    def purge_excluded(env_idx):\n"
            "        r = git('ls-files', '-c', env=env_idx)\n"
            "        if r.returncode != 0: return -1\n"
            "        excluded = [p for p in (ln for ln in r.stdout.decode('utf-8','replace').split(chr(10)) if ln.strip())\n"
            "                    if any(x in excl_dirs or x.startswith(excl_prefixes)\n"
            "                           for x in p.split('/')[:-1])]\n"
            "        for i in range(0, len(excluded), 200):\n"
            "            git('update-index', '--force-remove', '--', *excluded[i:i+200], env=env_idx)\n"
            "        return len(excluded)\n"
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
            "        temp_index = os.path.join(git_dir, 'peng_backup_idx_task' + str(task_id))\n"
            "        env_idx = {'GIT_INDEX_FILE': temp_index}\n"
            # 🔧 事务#2088 持久化增量索引（与快照共用；缺失时热/冷启动）
            "        if not os.path.exists(temp_index):\n"
            "            real_idx = os.path.join(git_dir, 'index')\n"
            "            if os.path.isfile(real_idx):\n"
            "                try: shutil.copy2(real_idx, temp_index)\n"
            "                except OSError: git('read-tree', 'HEAD', env=env_idx)\n"
            "            else:\n"
            "                git('read-tree', 'HEAD', env=env_idx)\n"
            "        git('add', '-A', env=env_idx)\n"
            "        # 🔧 排除产物目录（与快照路径同规则）\n"
            "        purge_excluded(env_idx)\n"
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
        """获取/创建 history 备份仓库路径（本地 data/history/task#id）

        🔧 用 _git_repo_valid 验证（残缺 .git 自动重建，同 ExternalFileBackup._ensure_repo）。
        """
        repo_path = os.path.join(HistoryBackupManager._get_history_base(), f"task#{task_id}")
        Path(repo_path).mkdir(parents=True, exist_ok=True)

        if not _git_repo_valid(repo_path):
            # 残缺/不存在 .git → 清除残留重新初始化
            # （工作区已有的 context.json/checklist.md 保留，init 后 add -A 提交）
            shutil.rmtree(os.path.join(repo_path, ".git"), ignore_errors=True)
            subprocess.run(["git", "init"], cwd=repo_path, capture_output=True, timeout=10)
            subprocess.run(
                ["git", "config", "user.email", "peng@history"], cwd=repo_path, capture_output=True
            )
            subprocess.run(
                ["git", "config", "user.name", "Peng History"], cwd=repo_path, capture_output=True
            )
            ctx = os.path.join(repo_path, "context.json")
            if not os.path.exists(ctx):
                Path(ctx).write_text("{}", encoding="utf-8")
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
    def has_changes(
        task_id: int,
        history_commit: str,
        include_context: bool = True,
        include_checklist: bool = True,
    ) -> bool:
        """检测当前 task db 的 history/checklist 相对指定备份 commit 是否有实际变化

        回滚预检测使用：messages 与 checklist 均与备份时相同 → 回滚无意义。
        比较时剥离统计字段（_prompt_tokens 等），只看实质内容。

        Args:
            task_id: 事务ID
            history_commit: 备份 commit hash
            include_context: 是否检测对话上下文变化（🔧 勾选框维度控制：
                用户取消勾选"上下文"时传 False，跳过 messages 比较）
            include_checklist: 是否检测 checklist 变化（🔧 勾选框维度控制：
                用户取消勾选"checklist"时传 False，跳过 checklist 比较）

        Returns:
            True=有实质变化（回滚会改变状态）；False=无变化或检测失败时保守返回True
        """
        if not task_id or not history_commit:
            return False
        try:
            repo = HistoryBackupManager._get_history_repo(task_id)

            # 备份 commit 中的 context messages
            backup_messages: str | None = None
            if include_context:
                r = subprocess.run(
                    ["git", "show", f"{history_commit}:context.json"],
                    cwd=repo, capture_output=True, text=True, timeout=10,
                )
                if r.returncode != 0:
                    return True  # 无备份内容，视为有变化（保守）
                backup_messages = strip_context_stats(r.stdout)

            # 备份 commit 中的 checklist
            backup_checklist: str | None = None
            if include_checklist:
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

            # messages 比较（备份commit必有context.json；当前无context视为有变化）
            if include_context:
                current_messages = (
                    strip_context_stats(task.get("context") or "")
                    if task.get("context")
                    else None
                )
                if current_messages is None:
                    return True
                if backup_messages != current_messages:
                    return True

            # checklist 比较（备份无checklist.md时：当前非空即有变化）
            if include_checklist:
                current_checklist = task.get("checklist", "") or ""
                if backup_checklist is None:
                    return bool(current_checklist)
                if backup_checklist != current_checklist:
                    return True
            return False
        except Exception as e:
            logger.debug(f"history变更检测失败(task#{task_id}): {e}")
            return True  # 检测异常保守视为有变化

    @staticmethod
    def restore(
        task_id: int,
        history_commit: str,
        trim_tool_call: bool = False,
        restore_context: bool = True,
        restore_checklist: bool = True,
    ) -> str:
        """从本地 git 仓库恢复 history context 到 task db

        Args:
            task_id: 事务ID
            history_commit: 备份 commit hash
            trim_tool_call: True=删除末尾工具调用+结果
            restore_context: 是否恢复对话上下文（🔧 勾选框维度控制：
                用户取消勾选"上下文"时传 False，只恢复 checklist）
            restore_checklist: 是否恢复 checklist（🔧 勾选框维度控制：
                用户取消勾选"checklist"时传 False，只恢复上下文）

        Returns:
            操作描述消息
        """
        if not task_id or not history_commit:
            return ""

        try:
            repo = HistoryBackupManager._get_history_repo(task_id)

            restored_parts: list[str] = []

            # 1. 对话上下文恢复（context.json → task db context）
            if restore_context:
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
                else:
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

                    action = "移除末尾工具调用" if trim_tool_call else "完整恢复"
                    restored_parts.append(f"上下文已恢复（{action}）")
                    logger.info(
                        f"事务#{task_id} History已恢复（{action}）from {history_commit[:8]}"
                    )

            # 2. checklist 恢复（checklist.md → task db checklist）
            # 注意：checklist.md 始终存在（backup时即使空字符串也写入文件）
            # 条件仅检查 returncode==0（旧commit可能无此文件），不检查 stdout 是否为空
            if restore_checklist:
                from peng.task.manager import TaskManager

                tm = TaskManager()
                cl_result = subprocess.run(
                    ["git", "show", f"{history_commit}:checklist.md"],
                    cwd=repo, capture_output=True, text=True, timeout=10,
                )
                if cl_result.returncode == 0:
                    tm.db.update_task(task_id, checklist=cl_result.stdout)
                    restored_parts.append("checklist已恢复")
                else:
                    # 旧commit可能没有checklist.md（功能添加前），保留当前值不变
                    logger.debug(f"History恢复: commit {history_commit[:8]} 无 checklist.md")

            if not restored_parts:
                return ""
            return "，".join(restored_parts)

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
    - 每次备份使用 git commit，支持多版本
    - 用于 git 场景下工作目录外的文件变更备份

    备份仓库位置（🔧 事务#1847：Windows平台避免跨盘符复制）：
    - Windows 平台且文件路径带盘符（如 D:\\work\\f.txt）→
      该文件同盘符根目录 \\.peng\\backups\\task#id（如 D:\\.peng\\backups\\task#5）
    - 其他（非 Windows 平台 / UNC 路径 / 无盘符）→
      peng 服务 data/file_backups/task#id（默认仓库）

    仓库 key 约定：`""` = 默认 data 仓库；`"D"` = 盘符仓库（大写字母）。
    回滚 meta 中的基线 commit 字段（external_backup_commit/external_head_commit）
    兼容两种格式：
    - str（旧格式/仅默认仓库）：该 commit 位于默认 data 仓库，所有文件均查默认仓库
    - dict {repo_key: commit}：各仓库记录各自基线，文件按自己盘符 key 查询
    """

    @staticmethod
    def _win_drive_of(file_path: str) -> str:
        """提取 Windows 盘符字母（大写）——纯路径形态判断，不依赖本地平台

        "D:\\x" / "d:/x" → "D"；Unix 路径 / UNC / 无盘符 → ""
        （远程 Windows nest 返回的路径在 Linux 服务端也能正确识别盘符，
        用于路由到 nest 端盘符仓库执行）
        """
        drive, _ = ntpath.splitdrive(str(file_path).replace("\\", "/"))
        if len(drive) == 2 and drive[1] == ":":
            return drive[0].upper()
        return ""

    @staticmethod
    def _split_drive_key(file_path: str) -> str | None:
        """本地备份场景的盘符提取（peng 本地运行在 Windows 时启用本地盘符仓库）

        仅当 os.name == "nt" 且路径带单盘符时返回盘符大写字母；
        非 Windows 平台 / Unix 路径 / UNC 路径返回 None。
        （peng 主服务在 Linux + 远程 Windows nest 的架构下，盘符文件走
        _win_drive_of + nest_name 的远程路径，不走本地盘符仓库）
        """
        if os.name != "nt":
            return None
        return ExternalFileBackup._win_drive_of(file_path) or None

    @staticmethod
    def _repo_key(file_path: str) -> str:
        """文件对应的本地备份仓库 key：盘符字母（Windows 盘符路径）或 ""（默认仓库）"""
        return ExternalFileBackup._split_drive_key(file_path) or ""

    @staticmethod
    def _repo_path_for_key(task_id: int, key: str) -> str:
        """仓库 key → 备份仓库路径（仅计算路径，不创建目录）

        🔧 初始设计还原（事务#1847 第七轮）：盘符根 \\.peng\\backups 本身是唯一
        git 环境，所有 task 共用；备份文件按绝对路径映射为仓库子路径
        （D:\\works\\t.txt → d/works/t.txt），不放 task#id 子目录。
        key="D" → D:\\.peng\\backups；key="" → 默认 data 仓库（会确保存在）
        """
        if not key:
            return ExternalFileBackup._get_backup_repo(task_id)
        return ntpath.join(f"{key}:\\", ".peng", "backups")

    @staticmethod
    def _get_backup_repo_for(task_id: int, file_path: str) -> str:
        """按文件路径定位备份仓库（确保仓库已初始化且有效）

        Windows 盘符路径 → 同盘符根目录 \\.peng\\backups\\task#id（避免跨盘符复制）；
        其他 → 默认 data 仓库。
        🔧 默认分支显式经过 _ensure_repo（残缺 .git 自动重建）——不能依赖
        _get_backup_repo 的内部实现保证（测试 mock 会整体替换它）。
        """
        key = ExternalFileBackup._split_drive_key(file_path)
        if key:
            return ExternalFileBackup._ensure_repo(
                ExternalFileBackup._repo_path_for_key(task_id, key)
            )
        return ExternalFileBackup._ensure_repo(
            ExternalFileBackup._get_backup_repo(task_id)
        )

    @staticmethod
    def _ensure_repo(repo_path: str) -> str:
        """确保备份仓库目录存在并完成 git 初始化（幂等，自动修复残缺残留）

        🔧 事务#1847 epyc 真实测试发现：Windows 上 rmtree 可能因文件锁部分失败，
        留下残缺 .git（如仅有 objects/）——仅检查 .git 存在性会误判已初始化而
        跳过 git init，导致后续 git 操作全部 rc=128 静默失败（备份返回 None）。
        改用 `git rev-parse --git-dir` 验证仓库有效性，无效时清除残缺 .git 重建。
        """
        Path(repo_path).mkdir(parents=True, exist_ok=True)
        if not _git_repo_valid(repo_path):
            # 残缺/不存在 .git → 清除残留后重新初始化
            # （git init 对已存在的 .git 也会补全结构，rmtree 是双保险）
            shutil.rmtree(os.path.join(repo_path, ".git"), ignore_errors=True)
            r = subprocess.run(
                ["git", "init"], cwd=repo_path, capture_output=True, text=True, timeout=15
            )
            if r.returncode != 0:
                logger.warning(f"备份仓库 git init 失败({repo_path}): {r.stderr[:200]}")
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
    def _resolve_base(
        task_id: int, file_path: str, commit_spec: str | dict[str, str] | None
    ) -> tuple[str, str, str | None]:
        """解析文件对应的 (备份仓库路径, 仓库key, 基线commit)

        - commit_spec=None → (文件自己的仓库, 文件key, "HEAD")（与各自仓库 HEAD 对比）
        - str（旧格式）→ (默认 data 仓库, "", str)——旧数据全部位于默认仓库
        - dict → 文件自己仓库 + 文件key + dict[文件key]；dict 缺该仓库条目 → ("", "", None)
          （无基线，check_changes 视为新增、restore 跳过）

        Returns:
            (repo_path, repo_key, base_commit)；base_commit 为 None 表示无基线
        """
        if commit_spec is None:
            key = ExternalFileBackup._repo_key(file_path)
            return ExternalFileBackup._get_backup_repo_for(task_id, file_path), key, "HEAD"
        if isinstance(commit_spec, dict):
            key = ExternalFileBackup._repo_key(file_path)
            commit = str(commit_spec.get(key) or "")
            if not commit:
                return "", key, None
            return ExternalFileBackup._get_backup_repo_for(task_id, file_path), key, commit
        # 旧格式 str：历史备份全部位于默认 data 仓库
        return ExternalFileBackup._get_backup_repo(task_id), "", str(commit_spec)

    @staticmethod
    def _iter_repo_bases(
        task_id: int, commit_spec: str | dict[str, str] | None
    ) -> list[tuple[str, str, str]]:
        """展开基线 commit 为 [(repo_key, repo_path, commit), ...] 列表（跨仓库）

        - str（旧格式）→ [("", 默认仓库, str)]
        - dict → 各 repo_key 对应仓库（跳过无效条目；盘符仓库不强制创建）
        - None/空 → []
        """
        if not commit_spec:
            return []
        if isinstance(commit_spec, dict):
            out: list[tuple[str, str, str]] = []
            for key, commit in commit_spec.items():
                if not commit:
                    continue
                k = str(key)
                if k:
                    # 盘符仓库：仅计算路径，不存在时由调用方跳过（只读操作不创建）
                    out.append((k, ExternalFileBackup._repo_path_for_key(task_id, k), str(commit)))
                else:
                    out.append(("", ExternalFileBackup._get_backup_repo(task_id), str(commit)))
            return out
        return [("", ExternalFileBackup._get_backup_repo(task_id), str(commit_spec))]

    @staticmethod
    def _existing_drive_keys(task_id: int) -> list[str]:
        """枚举 Windows 本地已存在备份仓库的盘符字母列表（A-Z）"""
        if os.name != "nt":
            return []
        keys: list[str] = []
        for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            repo = ExternalFileBackup._repo_path_for_key(task_id, c)
            if os.path.isdir(os.path.join(repo, ".git")):
                keys.append(c)
        return keys

    # ==================== 远程 nest 端执行（🔧 事务#1847 第四轮） ====================
    # peng 主服务可能在 Linux（如 vnas），事务工作在远程 Windows nest（如 epyc）：
    # 外部文件是 nest 端的 Windows 路径，本地 os.path.exists 恒 False → 备份静默失效。
    # 因此远程 Windows 路径（带盘符）的备份/检测/恢复全部路由到 nest 端执行：
    # 仓库位于 nest 端文件同盘符根 \.peng\backups\task#id（避免跨盘符复制），
    # 每个操作打包为单个 Python 脚本经 nest exec 一次 HTTP 完成。

    @staticmethod
    def _is_remote_nest(nest_name: str | None) -> bool:
        return bool(nest_name and nest_name != "local")

    @staticmethod
    def _remote_py_head(task_id: int) -> str:
        """远程批量脚本公共头（nest 端执行环境：盘符路由/仓库定位/git封装）"""
        return (
            "import subprocess, os, sys, json, shutil\n"
            f"task_id = {task_id!r}\n"
            f"excl_names = {sorted(_FORCE_EXCLUDE_DIR_NAMES)!r}\n"
            f"excl_prefixes = {list(_FORCE_EXCLUDE_DIR_PREFIXES)!r}\n"
            "def git_bytes(repo, *a):\n"
            "    return subprocess.run(['git'] + list(a), cwd=repo, capture_output=True, timeout=60)\n"
            "def git(repo, *a):\n"
            "    r = git_bytes(repo, *a)\n"
            "    class R: pass\n"
            "    rr = R(); rr.returncode = r.returncode\n"
            "    rr.stdout = r.stdout.decode('utf-8', 'replace'); rr.stderr = r.stderr.decode('utf-8', 'replace')\n"
            "    return rr\n"
            "def drive_of(fp):\n"
            "    d = str(fp).replace(chr(92), '/')\n"
            "    return d[0].upper() if len(d) >= 2 and d[1] == ':' else ''\n"
            # 🔧 初始设计：盘符根 \.peng\backups 是唯一 git 环境（无 task#id 子目录）
            "def repo_for(dr):\n"
            "    return dr + ':\\\\.peng\\\\backups'\n"
            # 备份仓库内路径：D:\works\t.txt → d/works/t.txt（盘符小写+完整路径映射）
            "def rel_of(fp):\n"
            "    d = str(fp).replace(chr(92), '/')\n"
            "    return d[0].lower() + '/' + d[2:].lstrip('/') if len(d) >= 2 and d[1] == ':' else d.lstrip('/')\n"
            # 备份内路径 → 源绝对路径（d/works/t.txt → D:\works\t.txt）
            "def abs_of(dr, brel):\n"
            "    return dr + ':\\\\' + brel.split('/', 1)[1].replace('/', chr(92)) if '/' in brel else dr + ':\\\\'\n"
            "def excluded(fp):\n"
            "    parts = str(fp).replace(chr(92), '/').split('/')\n"
            "    return any(x in excl_names or x.startswith(tuple(excl_prefixes)) for x in parts[:-1])\n"
            # 🔧 事务#2088 护栏：段数（去盘符后非空路径段）；盘根=0 绝对禁止，
            # 目录段数<2 禁止整树复制
            "def seg_count(fp):\n"
            "    d = str(fp).replace(chr(92), '/')\n"
            "    if len(d) >= 2 and d[1] == ':': d = d[2:]\n"
            "    return len([s for s in d.split('/') if s])\n"
            # 目录规模限额：scandir 遍历统计（超限短路），防巨型目录整树复制
            f"max_dir_files = {_DIR_BACKUP_MAX_FILES!r}\n"
            f"max_dir_bytes = {_DIR_BACKUP_MAX_BYTES!r}\n"
            "def dir_within_budget(root):\n"
            "    n, total, stack = 0, 0, [root]\n"
            "    while stack:\n"
            "        d = stack.pop()\n"
            "        try: es = list(os.scandir(d))\n"
            "        except OSError: continue\n"
            "        for e in es:\n"
            "            try:\n"
            "                if e.is_dir(follow_symlinks=False): stack.append(e.path)\n"
            "                elif e.is_file(follow_symlinks=False):\n"
            "                    n += 1; total += e.stat().st_size\n"
            "            except OSError: continue\n"
            "        if n > max_dir_files or total > max_dir_bytes: return False\n"
            "    return True\n"
            "def ensure_repo(repo):\n"
            "    os.makedirs(repo, exist_ok=True)\n"
            "    if git(repo, 'rev-parse', '--git-dir').returncode != 0:\n"
            "        shutil.rmtree(os.path.join(repo, '.git'), ignore_errors=True)\n"
            "        git(repo, 'init')\n"
            "        git(repo, 'config', 'user.email', 'peng@files')\n"
            "        git(repo, 'config', 'user.name', 'Peng Files')\n"
            "        git(repo, 'add', '-A')\n"
            "        git(repo, 'commit', '-m', 'init', '--allow-empty')\n"
            "out = {}\n"
        )

    @staticmethod
    def _remote_run(nest_name: str, py_code: str, timeout: int = 120) -> dict | None:
        """执行远程批量脚本并解析 JSON 输出（末行）"""
        result = _run_python_remote(py_code, timeout=timeout, nest_name=nest_name)
        if result.returncode != 0:
            logger.warning(
                f"外部文件备份远程执行失败({nest_name}): rc={result.returncode} "
                f"{str(result.stderr)[:200]}"
            )
            return None
        try:
            return json.loads(result.stdout.strip().split("\n")[-1])
        except (json.JSONDecodeError, IndexError):
            logger.warning(f"外部文件备份远程响应解析失败({nest_name}): {str(result.stdout)[:200]}")
            return None

    @staticmethod
    def _remote_backup(
        nest_name: str, task_id: int, file_paths: list[str], label: str
    ) -> dict[str, str] | None:
        """远程备份：按盘符分组复制+commit（nest端盘符根仓库），单次HTTP"""
        if not file_paths:
            return None
        py = ExternalFileBackup._remote_py_head(task_id) + (
            f"files = {file_paths!r}\n"
            f"label = {label!r}\n"
            "groups = {}\n"
            "skipped = []\n"
            "for fp in files:\n"
            "    if not os.path.exists(fp) or excluded(fp): continue\n"
            # 🔧 事务#2088 护栏：盘根/一级目录禁止，深层目录限额（零容忍全盘复制）
            "    if seg_count(fp) == 0:\n"
            "        skipped.append('root: ' + fp); continue\n"
            "    if os.path.isdir(fp):\n"
            "        if seg_count(fp) < 2:\n"
            "            skipped.append('shallow-dir: ' + fp); continue\n"
            "        if not dir_within_budget(fp):\n"
            "            skipped.append('oversized-dir: ' + fp); continue\n"
            "    dr = drive_of(fp)\n"
            "    if not dr: continue\n"
            "    groups.setdefault(dr, []).append(fp)\n"
            "if skipped:\n"
            "    out['skipped'] = skipped[:50]\n"
            "commits = {}\n"
            "for dr, fps in groups.items():\n"
            "    repo = repo_for(dr)\n"
            "    ensure_repo(repo)\n"
            "    for fp in fps:\n"
            "        dst = os.path.join(repo, rel_of(fp).replace('/', os.sep))\n"
            "        os.makedirs(os.path.dirname(dst), exist_ok=True)\n"
            "        if os.path.isfile(fp): shutil.copy2(fp, dst)\n"
            "        elif os.path.isdir(fp): shutil.copytree(fp, dst, dirs_exist_ok=True)\n"
            "    st = git(repo, 'status', '--porcelain')\n"
            "    if st.stdout.strip():\n"
            "        git(repo, 'add', '-A')\n"
            # 🔧 初始设计：commit 消息含事务id+原路径映射+一键还原命令（:/消息前缀
            # 定位本 commit 历史版本：git checkout ":/前缀" -- rel 取该版本到工作区
            # ——二进制安全，再 copy 到原路径；纯 copy 只能拿最新工作区副本）。
            # 一次 commit 无需 amend（消息引用自身 hash 数学上不可能）。纯 ASCII。
            "        nl = chr(10)\n"
            "        fl = 'peng backup task#' + str(task_id) + ': ' + label\n"
            "        mlines = [fl, '', 'files (origin -> backup):']\n"
            "        rst = []\n"
            "        for fp in fps[:20]:\n"
            "            brel = rel_of(fp)\n"
            "            mlines.append('  ' + fp + ' -> ' + brel)\n"
            "            bak = os.path.join(repo, *brel.split('/'))\n"
            "            rst.append('  git -C ' + repo + ' checkout ' + chr(34) + ':/' + fl + chr(34) + ' -- ' + brel + ' && copy /y ' + chr(34) + bak + chr(34) + ' ' + chr(34) + fp + chr(34))\n"
            "        if len(fps) > 20: mlines.append('  ...total ' + str(len(fps)) + ' files (list truncated)')\n"
            "        mlines.append('')\n"
            "        mlines.append('one-key restore (version of this commit):')\n"
            "        mlines.extend(rst)\n"
            "        git(repo, 'commit', '-m', nl.join(mlines))\n"
            "    r = git(repo, 'rev-parse', 'HEAD')\n"
            "    if r.returncode == 0 and r.stdout.strip(): commits[dr] = r.stdout.strip()\n"
            "out['commits'] = commits\n"
            "print(json.dumps(out))\n"
        )
        data = ExternalFileBackup._remote_run(nest_name, py)
        if data and data.get("skipped"):
            # 🔧 事务#2088：护栏拦截的路径回报日志（盘根/浅目录/超限目录）
            for s in data["skipped"]:
                logger.warning(f"外部文件备份跳过(远程{nest_name}): {s}")
        return (data or {}).get("commits") or None

    @staticmethod
    def _remote_check_changes(
        nest_name: str, task_id: int, file_paths: list[str],
        commit_spec: str | dict[str, str] | None,
        strict_baseline: bool = False,
    ) -> list[str]:
        """远程变更检测：逐文件 hash-object 对比基线（nest端），单次HTTP

        strict_baseline 语义见 check_changes（恢复集模式：无基线跳过）。
        """
        if not file_paths:
            return []
        spec = commit_spec if isinstance(commit_spec, dict) else None
        py = ExternalFileBackup._remote_py_head(task_id) + (
            f"files = {file_paths!r}\n"
            f"spec = {spec!r}\n"
            f"strict = {bool(strict_baseline)!r}\n"
            "changed = []\n"
            "for fp in files:\n"
            # 🔧 事务#2088：盘根路径不参与变更检测（历史误备份数据兜底）
            "    if seg_count(fp) == 0: continue\n"
            "    dr = drive_of(fp)\n"
            "    if not dr: continue\n"
            # 🔧 事务#2402：目录不参与变更检测——git hash-object 对目录必失败
            # （旧逻辑直接 append changed → workdir 父目录 D:/work 曾恒报
            # "已变更"，回滚窗口永远把它列为待恢复文件）
            "    if os.path.isdir(fp): continue\n"
            "    repo = repo_for(dr)\n"
            # 🔧 事务#2402：基线解析——恢复集模式（strict）下本事务 dict 基线
            # 缺该盘符条目 = 该盘备份从未成功（如 task#2354 的 P:\.peng\backups
            # 仓库不存在，32 个 P 盘文件 manifest 照记）→ 跳过，绝不 HEAD
            # 兜底（共享仓库其他任务的 HEAD 当基线 → 恒报"变更"误导）；
            # 默认模式保留旧语义（None/str 基线 → HEAD 兜底，缺键视为新增）
            "    if spec is None:\n"
            "        base = 'HEAD'\n"
            "    else:\n"
            "        base = spec.get(dr)\n"
            "        if not base:\n"
            "            if strict: continue\n"
            "            base = 'HEAD'\n"
            "    rel = rel_of(fp)\n"
            "    if not os.path.exists(fp):\n"
            "        if git(repo, 'cat-file', '-e', base + ':' + rel).returncode == 0:\n"
            "            changed.append(fp)\n"
            "        continue\n"
            "    r = git(repo, 'hash-object', fp)\n"
            "    if r.returncode != 0:\n"
            # 🔧 事务#2402：hash 失败（权限/特殊文件）≠ 已变更——旧逻辑恒
            # append 会把无法比较的对象永久误标为"待恢复"，宁缺勿滥
            "        continue\n"
            "    r2 = git(repo, 'rev-parse', base + ':' + rel)\n"
            "    if r2.returncode != 0:\n"
            # 🔧 事务#2402：基线无此路径键（备份后路径大小写形态漂移——git
            # 仓库键大小写敏感，c:/users/... vs 备份时 C:/Users/...；或该
            # 文件实际未备份成功）——恢复集模式（strict）下恢复同样跳过，
            # 显示与恢复一致不报；默认模式保留旧语义（视为新增）
            "        if strict: continue\n"
            "        changed.append(fp); continue\n"
            "    if r2.stdout.strip() != r.stdout.strip():\n"
            "        changed.append(fp)\n"
            "out['changed'] = changed\n"
            "print(json.dumps(out))\n"
        )
        data = ExternalFileBackup._remote_run(nest_name, py, timeout=60)
        return (data or {}).get("changed", [])

    @staticmethod
    def _remote_restore(
        nest_name: str, task_id: int, file_paths: list[str],
        commit_spec: str | dict[str, str] | None,
    ) -> int:
        """远程恢复：git show 基线内容写回原路径（bytes模式防编码损坏），单次HTTP"""
        if not file_paths:
            return 0
        spec = commit_spec if isinstance(commit_spec, dict) else None
        py = ExternalFileBackup._remote_py_head(task_id) + (
            f"files = {file_paths!r}\n"
            f"spec = {spec!r}\n"
            "restored = 0\n"
            "skipped = 0\n"
            "for fp in files:\n"
            "    dr = drive_of(fp)\n"
            "    if not dr: continue\n"
            # 🔧 事务#2402：目标是目录绝不写回——旧逻辑 git show 对 tree 基线
            # （曾被整目录 copytree 备份的 D:/work 等）返回 ls-tree 文本且 rc=0，
            # 随后 open(目录,'wb') 抛 IsADirectoryError 直接炸掉整个恢复脚本
            "    if os.path.isdir(fp): skipped += 1; continue\n"
            "    repo = repo_for(dr)\n"
            "    base = (spec or {}).get(dr)\n"
            "    if not base: continue\n"
            "    rel = rel_of(fp)\n"
            "    r = git_bytes(repo, 'show', base + ':' + rel)\n"
            "    if r.returncode != 0: continue\n"
            # 🔧 事务#2402：基线对象必须是 blob——tree/commit(gitlink) 的
            # git show 同样 rc=0 但内容非文件字节，写回=制造垃圾文件
            "    t = git(repo, 'cat-file', '-t', base + ':' + rel)\n"
            "    if t.returncode == 0 and t.stdout.strip() != 'blob': skipped += 1; continue\n"
            "    os.makedirs(os.path.dirname(fp), exist_ok=True)\n"
            "    try:\n"
            "        with open(fp, 'wb') as f: f.write(r.stdout)\n"
            "    except OSError:\n"
            # 🔧 事务#2402：单文件失败（占用/权限）不中断整批恢复
            "        skipped += 1; continue\n"
            "    restored += 1\n"
            "out['restored'] = restored\n"
            "out['skipped'] = skipped\n"
            "print(json.dumps(out))\n"
        )
        data = ExternalFileBackup._remote_run(nest_name, py, timeout=90)
        return int((data or {}).get("restored", 0))

    @staticmethod
    def _remote_get_drive_heads(nest_name: str, task_id: int) -> dict[str, str]:
        """远程枚举 nest 端已存在的盘符仓库 HEAD（{盘符: commit}），单次HTTP"""
        py = ExternalFileBackup._remote_py_head(task_id) + (
            "heads = {}\n"
            "for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':\n"
            "    repo = repo_for(c)\n"
            "    if os.path.isdir(os.path.join(repo, '.git')):\n"
            "        r = git(repo, 'rev-parse', 'HEAD')\n"
            "        if r.returncode == 0 and r.stdout.strip(): heads[c] = r.stdout.strip()\n"
            "out['heads'] = heads\n"
            "print(json.dumps(out))\n"
        )
        data = ExternalFileBackup._remote_run(nest_name, py, timeout=60)
        return (data or {}).get("heads", {})

    @staticmethod
    def _remote_list_baseline(
        nest_name: str, task_id: int, commit_spec: dict[str, str]
    ) -> list[str]:
        """远程列出盘符仓库基线中的文件（rel→盘符绝对路径），单次HTTP"""
        remote_spec = {k: v for k, v in commit_spec.items() if k}
        if not remote_spec:
            return []
        py = ExternalFileBackup._remote_py_head(task_id) + (
            f"spec = {remote_spec!r}\n"
            "bl = []\n"
            "for dr, commit in spec.items():\n"
            "    if not dr or not commit: continue\n"
            "    repo = repo_for(dr)\n"
            "    if not os.path.isdir(repo): continue\n"
            "    r = git(repo, '-c', 'core.quotepath=false', 'ls-tree', '-r', '--name-only', commit)\n"
            "    for line in r.stdout.splitlines():\n"
            "        line = line.strip()\n"
            "        if not line: continue\n"
            "        if line.startswith('__external__/'): line = line[len('__external__/'):]\n"
            # 🔧 初始设计：备份内路径 d/works/t.txt → 源绝对路径 D:/works/t.txt
            # （首段盘符字母转大写；其余段为原路径）。旧结构（无盘符段）按
            # 去盘符路径处理，加回盘符
            "        if line[:2] == dr.lower() + '/':\n"
            "            bl.append(dr + ':/' + line[2:])\n"
            "        else:\n"
            "            bl.append(dr + ':/' + line)\n"
            "out['files'] = bl\n"
            "print(json.dumps(out))\n"
        )
        data = ExternalFileBackup._remote_run(nest_name, py, timeout=60)
        return (data or {}).get("files", [])

    @staticmethod
    def _split_local_remote(
        nest_name: str | None, file_paths: list[str]
    ) -> tuple[list[str], list[str]]:
        """按执行环境拆分文件：本地处理的 vs 远程nest处理的

        远程 nest 存在时，Windows 盘符路径（远程端文件）→ 远程；其余 → 本地。
        """
        if not ExternalFileBackup._is_remote_nest(nest_name):
            return list(file_paths), []
        remote_set = {
            fp for fp in file_paths if ExternalFileBackup._win_drive_of(fp)
        }
        local = [fp for fp in file_paths if fp not in remote_set]
        return local, list(remote_set)

    @staticmethod
    def _merge_commits(
        local: str | dict[str, str] | None, remote: dict[str, str] | None
    ) -> str | dict[str, str] | None:
        """合并本地/远程备份结果（仅默认仓库 → str 兼容旧 meta 格式）"""
        if not remote:
            return local
        merged: dict[str, str] = {}
        if isinstance(local, dict):
            merged.update(local)
        elif isinstance(local, str) and local:
            merged[""] = local
        merged.update(remote)
        if set(merged.keys()) == {""}:
            return merged[""]
        return merged

    @staticmethod
    def _get_all_heads_local(task_id: int) -> str | dict[str, str]:
        """获取本地全部外部文件备份仓库的当前 HEAD commit

        覆盖：默认 data 仓库 + Windows 各盘符 \\.peng\\backups\\task#id 仓库（已存在时）。
        仅默认仓库有内容 → 返回 str（兼容旧 meta 格式）；
        涉及盘符仓库 → 返回 {repo_key: commit}。
        """
        heads: dict[str, str] = {}
        # 默认仓库（确保存在）
        try:
            default_repo = ExternalFileBackup._get_backup_repo(task_id)
            r = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=default_repo,
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                heads[""] = r.stdout.strip()
        except Exception as e:
            logger.debug(f"外部文件基线: 默认仓库HEAD获取失败(task#{task_id}): {e}")
        # Windows 盘符仓库（已存在的）
        for key in ExternalFileBackup._existing_drive_keys(task_id):
            try:
                repo = ExternalFileBackup._repo_path_for_key(task_id, key)
                r = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=repo,
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0 and r.stdout.strip():
                    heads[key] = r.stdout.strip()
            except Exception as e:
                logger.debug(f"外部文件基线: 盘符{key}:仓库HEAD获取失败(task#{task_id}): {e}")
        if not heads:
            return ""
        if set(heads.keys()) == {""}:
            return heads[""]
        return heads

    @staticmethod
    def _get_backup_repo(task_id: int) -> str:
        """获取/创建外部文件备份仓库路径（本地 data/file_backups/task#id）

        保持单参数签名（测试与既有代码依赖）。
        """
        try:
            from peng.config.loader import load_config

            config = load_config()
            base = str(config.paths.data_dir_path / "file_backups")
        except Exception:
            base = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "file_backups")
        repo_path = os.path.join(base, f"task#{task_id}")
        return ExternalFileBackup._ensure_repo(repo_path)

    @staticmethod
    def _to_repo_path(file_path: str) -> str:
        """将绝对文件路径转换为备份仓库内的相对路径（去掉根分隔符）

        🔧 仅适用于默认 data 仓库（保留盘符段，与历史数据结构一致，如 D:/x → D:/x）。
        盘符仓库必须用 _repo_rel（去盘符）——Windows 目录名不能含 ':'。
        """
        clean = file_path.replace("\\", "/")
        return clean.lstrip("/")

    @staticmethod
    def _repo_rel(file_path: str, repo_key: str) -> str:
        """文件在其备份仓库内的相对路径（POSIX 分隔，按仓库类型计算）

        - 盘符仓库（repo_key 非空，即 \\.peng\\backups 根仓库）：绝对路径映射为
          仓库子路径——D:\\works\\t.txt → d/works/t.txt（🔧 初始设计，事务#1847
          第七轮：盘符字母小写作首段目录，去冒号，Windows 目录名不能含 ':'；
          os.path.join 也会把含盘符的路径当绝对路径处理——join(仓库, "D:/x")
          == "D:/x" 恒等于源文件路径，导致 copy2(源, 源) 抛 WinError 32）
        - 默认仓库（repo_key=""）：保留完整路径含盘符段（Linux peng 的
          data/file_backups 历史数据结构），如 D:\\peng\\x → D:/peng/x

        Args:
            file_path: 外部文件绝对路径
            repo_key: 备份仓库 key（""=默认；盘符字母）
        """
        if repo_key:
            return f"{repo_key.lower()}/" + _strip_drive_rel(file_path)
        return ExternalFileBackup._to_repo_path(file_path)

    @staticmethod
    def _backup_commit_message(
        task_id: int, file_pairs: list[tuple[str, str]], label: str,
        repo_path: str, win: bool, max_lines: int = 20,
    ) -> str:
        """构造备份 commit 消息（🔧 初始设计，事务#1847 第九轮）

        格式：事务id + 原路径→备份路径映射 + 一键还原命令（从**本 commit 的
        历史版本**还原）。commit hash 在 commit 前不可知且 amend 会改变 hash
        （消息引用自身 hash 数学上不可能）——还原命令用 git 的 `:/<消息前缀>`
        语法按消息首行定位本 commit（首行含事务id+label+时间戳，唯一），
        git checkout ":/前缀" -- <rel> 取该版本到工作区（二进制安全）再 copy
        到原路径；纯 copy 只能拿最新工作区副本，无法还原历史。
        纯 ASCII 结构（中文会在 Windows GBK 控制台/脚本解码时崩溃）。

        Args:
            file_pairs: [(原路径, 备份仓库内rel)] 如
                [(r"D:\\works\\t.txt", "d/works/t.txt")]
            repo_path: 备份仓库根（如 D:\\.peng\\backups）
            win: True → 还原命令用 copy /y（Windows）；False → cp（Linux）
        """
        nl = "\n"
        first_line = f"peng backup task#{task_id}: {label}"
        lines = [first_line, "", "files (origin -> backup):"]
        restores: list[str] = []
        for src, rel in file_pairs[:max_lines]:
            lines.append(f"  {src} -> {rel}")
            bak = os.path.join(repo_path, *rel.split("/")) if "/" in rel else os.path.join(repo_path, rel)
            if win:
                restores.append(
                    f'  git -C "{repo_path}" checkout ":/{first_line}" -- {rel}'
                    f' && copy /y "{bak}" "{src}"'
                )
            else:
                restores.append(
                    f'  git -C "{repo_path}" checkout ":/{first_line}" -- {rel}'
                    f' && cp "{bak}" "{src}"'
                )
        if len(file_pairs) > max_lines:
            lines.append(f"  ...total {len(file_pairs)} files (list truncated)")
        lines.append("")
        lines.append(f"one-key restore (version of this commit):")
        lines.extend(restores)
        return nl.join(lines)

    @staticmethod
    def _backup_local(
        task_id: int, file_paths: list[str], label: str = ""
    ) -> str | dict[str, str] | None:
        """备份外部文件到本地 git 仓库（🔧 按文件仓库分组，本地 Windows 盘符文件入同盘符仓库）

        Args:
            task_id: 事务ID
            file_paths: 外部文件绝对路径列表
            label: 备份标签

        Returns:
            仅默认仓库（data）→ commit hash 字符串（兼容旧 meta 格式）；
            涉及盘符仓库 → {repo_key: commit}（key="" 为默认仓库，"D" 等为盘符仓库）；
            无可备份文件或全部失败 → None
        """
        if not file_paths:
            return None

        try:
            backed_up = 0
            skipped_excluded = 0
            # 按仓库分组复制（repo_key → (repo_path, 该仓库实际备份的文件)）
            groups: dict[str, tuple[str, list[str]]] = {}
            for fp in file_paths:
                if not os.path.exists(fp):
                    continue
                # 🔧 排除规则命中（build/、build_*/等，无论是否修改都不备份）
                if _is_abs_path_excluded(fp):
                    skipped_excluded += 1
                    continue
                # 🔧 事务#2088 护栏：盘根/一级目录禁止整树复制；深层目录限额
                is_dir = os.path.isdir(fp)
                reason = _skip_reason_for_path(fp, is_dir)
                if reason is None and is_dir and not _dir_within_budget_local(fp):
                    reason = (
                        f"目录超限额(>{_DIR_BACKUP_MAX_FILES}文件或"
                        f">{_DIR_BACKUP_MAX_BYTES // 2**30}GiB) {fp}"
                    )
                if reason is not None:
                    logger.warning(f"外部文件备份跳过(task#{task_id}): {reason}")
                    continue
                key = ExternalFileBackup._repo_key(fp)
                if key not in groups:
                    groups[key] = (ExternalFileBackup._get_backup_repo_for(task_id, fp), [])
                # 🔧 按仓库类型计算 rel：盘符仓库 d/works/t.txt（盘符小写+完整路径
                # 映射；Windows 目录名不能含':'，且含盘符路径会让 os.path.join
                # 视为绝对路径导致 dst==源文件）
                rel = ExternalFileBackup._repo_rel(fp, key)
                dst = os.path.join(groups[key][0], *rel.split("/"))
                Path(os.path.dirname(dst)).mkdir(parents=True, exist_ok=True)
                if os.path.isfile(fp):
                    shutil.copy2(fp, dst)
                elif os.path.isdir(fp):
                    shutil.copytree(fp, dst, dirs_exist_ok=True)
                groups[key][1].append((fp, rel))
                backed_up += 1

            if skipped_excluded:
                logger.debug(
                    f"外部文件备份: 跳过 {skipped_excluded} 个排除规则命中的路径(task#{task_id})"
                )
            if backed_up == 0:
                return None

            timestamp = datetime.now().isoformat(timespec="seconds")
            commits: dict[str, str] = {}
            for key, (repo, files) in groups.items():
                # 🔧 去重检测：文件内容与该仓库 HEAD 完全一致时复用 HEAD commit，
                # 避免无变化时产生冗余空 commit（--allow-empty 已移除）
                commit_hash: str | None = None
                if not ExternalFileBackup.check_changes(task_id, [fp for fp, _ in files]):
                    result = subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        cwd=repo, capture_output=True, text=True, timeout=5,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        commit_hash = result.stdout.strip()
                        logger.debug(
                            f"外部文件备份去重 task#{task_id}[{key or 'default'}]: "
                            f"无变化，复用HEAD {commit_hash[:8]}"
                        )
                if not commit_hash:
                    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, timeout=10)
                    # 🔧 初始设计：commit 消息含事务id+原路径映射+一键还原命令
                    # （:/消息前缀 定位本 commit 的历史版本，一次 commit 无需 amend）
                    msg = ExternalFileBackup._backup_commit_message(
                        task_id, files, label, repo, win=bool(key)
                    )
                    subprocess.run(
                        ["git", "commit", "-m", msg],
                        cwd=repo, capture_output=True, text=True, timeout=10,
                    )
                    result = subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        cwd=repo, capture_output=True, text=True, timeout=5,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        commit_hash = result.stdout.strip()
                    else:
                        logger.warning(
                            f"外部文件备份commit后获取HEAD失败 task#{task_id}[{key or 'default'}]: "
                            f"rc={result.returncode} {str(result.stderr)[:150]}"
                        )
                if commit_hash:
                    commits[key] = commit_hash

            if not commits:
                return None
            # 仅默认仓库 → str（100%兼容旧 meta 格式）；涉及盘符仓库 → dict
            if set(commits.keys()) == {""}:
                logger.debug(
                    f"外部文件备份成功 task#{task_id} commit={commits[''][:8]} files={backed_up}"
                )
                return commits[""]
            parts = ", ".join(f"{k or 'default'}:{v[:8]}" for k, v in commits.items())
            logger.debug(
                f"外部文件备份成功 task#{task_id}（{len(commits)} 个仓库）"
                f"files={backed_up} [{parts}]"
            )
            return commits
        except Exception as e:
            logger.warning(f"外部文件备份失败 task#{task_id}: {e}")
        return None

    @staticmethod
    def _list_baseline_local(
        task_id: int, commit_spec: str | dict[str, str] | None
    ) -> list[str]:
        """列出基线 commit 中备份的全部外部文件绝对路径（本地仓库部分）

        遍历基线涉及的每个备份仓库（默认 data 仓库 + Windows 各盘符仓库）
        执行 git ls-tree，将仓库相对路径反推为原始绝对路径。
        仓库不存在或 ls-tree 失败时跳过该仓库（只读操作，不创建仓库）。

        Args:
            task_id: 事务ID
            commit_spec: 基线 commit（str=旧格式默认仓库；dict={repo_key: commit}）

        Returns:
            外部文件绝对路径列表（可能为空）
        """
        abs_paths: list[str] = []
        for repo_key, repo, base in ExternalFileBackup._iter_repo_bases(task_id, commit_spec):
            if not base or not os.path.isdir(repo):
                continue
            try:
                result = subprocess.run(
                    ["git", "ls-tree", "-r", "--name-only", base],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            except Exception as e:
                logger.debug(f"外部文件基线ls-tree异常({repo}): {e}")
                continue
            if result.returncode != 0:
                logger.warning(f"外部文件基线ls-tree失败({repo}): {result.stderr[:200]}")
                continue
            for rel in (ln.strip() for ln in result.stdout.split("\n") if ln.strip()):
                if rel.startswith("__external__/"):
                    rel = rel[len("__external__/"):]
                if repo_key:
                    # 🔧 初始设计：备份内路径 d/works/t.txt → 源绝对路径 D:/works/t.txt
                    # （首段盘符字母转大写）。旧结构（无盘符段）按去盘符路径加回盘符
                    if rel[:2].lower() == f"{repo_key.lower()}/":
                        abs_path = f"{repo_key}:/{rel[2:]}"
                    else:
                        abs_path = f"{repo_key}:/{rel}"
                else:
                    # 默认仓库：rel 可能是含盘符的 Windows 路径或 Unix 路径
                    abs_path = rel if ntpath.isabs(rel) else "/" + rel
                abs_paths.append(abs_path)
        return abs_paths

    @staticmethod
    def _check_changes_local(
        task_id: int,
        file_paths: list[str],
        commit_hash: str | dict[str, str] | None = None,
        strict_baseline: bool = False,
    ) -> list[str]:
        """检查外部文件是否有变化（🔧 按文件定位备份仓库，支持多仓库基线；本地执行）

        使用 git hash-object 逐文件比较 blob hash，不修改备份仓库工作树。

        Args:
            task_id: 事务ID
            file_paths: 外部文件绝对路径列表
            commit_hash: 比较基准commit。None=各文件自己仓库的HEAD（默认）；
                str=旧格式基线（一律查默认 data 仓库——历史数据全部在那里）；
                dict={repo_key: commit} 各仓库基线（缺某仓库条目视为该文件新增）。
                回滚预检测时传入快照记录的 external_backup_commit/
                external_head_commit 基线。

        Returns:
            变化的文件路径列表（绝对路径）
        """
        if not file_paths:
            return []

        try:
            changed: list[str] = []

            for fp in file_paths:
                # 🔧 事务#2088：盘根路径不参与变更检测（防 hash-object 失败误报）
                if is_root_volume_path(fp):
                    continue
                # 🔧 事务#2402：目录/Windows保留设备名不参与检测——目录
                # hash-object 必失败（旧逻辑恒 append changed 误报"待恢复"），
                # 设备名（nul等）os.path.exists 恒真但非普通文件
                if os.path.isdir(fp) or _is_windows_device_path(fp):
                    continue
                repo, repo_key, base = ExternalFileBackup._resolve_base(task_id, fp, commit_hash)
                if base is None:
                    if strict_baseline:
                        # 🔧 事务#2402：恢复集模式（manifest 来源）——基线缺条目
                        # = 备份从未成功（manifest 照记的死条目），恢复同样跳过，
                        # 显示与恢复一致：不能恢复的绝不列为"待恢复变更"
                        # （task#2354 的 P 盘 32 文件误报源头）
                        continue
                    # 默认语义（#2107）：dict 缺仓库条目 → 视为新增文件
                    changed.append(fp)
                    continue
                rel = ExternalFileBackup._repo_rel(fp, repo_key)

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
                    # 🔧 事务#2402：无法 hash（权限/特殊文件）≠ 有变化——
                    # 恒 append 会把无法比较的对象永久误标为"待恢复"
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
                    if strict_baseline:
                        # 🔧 事务#2402：恢复集模式——基线无此路径键（路径大小写
                        # 形态漂移，git 仓库键大小写敏感；或备份实际未成功）→
                        # 恢复同样跳过，不显示
                        continue
                    # 默认语义：基准中不存在 → 新增文件
                    changed.append(fp)
                elif result.stdout.strip() != current_hash:
                    changed.append(fp)

            return changed

        except Exception as e:
            logger.warning(f"外部文件变更检查失败 task#{task_id}: {e}")
            return []

    @staticmethod
    def _restore_local(
        task_id: int,
        file_paths: list[str],
        commit_hash: str | dict[str, str] | None = None,
    ) -> int:
        """从备份恢复外部文件（🔧 按文件定位备份仓库，支持多仓库基线；本地执行）

        使用 `git show <commit>:<path>` 直接从指定 commit（默认 HEAD）读取文件内容，
        不修改备份仓库工作树，避免 undo 快照覆盖 HEAD 的问题。

        Args:
            task_id: 事务ID
            file_paths: 外部文件绝对路径列表
            commit_hash: 恢复源 commit。None=各文件自己仓库的HEAD（默认）；
                str=旧格式基线（一律从默认 data 仓库恢复）；
                dict={repo_key: commit} 各仓库基线（缺条目的文件跳过恢复）。

        Returns:
            成功恢复的文件数
        """
        if not file_paths:
            return 0

        try:
            restored = 0

            for fp in file_paths:
                # 🔧 事务#2402：目标是目录/Windows保留设备名绝不写回——
                # 目录 open(wb) 抛 IsADirectoryError 中断整批恢复；tree 基线
                # （曾整目录 copytree 的 D:/work 等）git show rc=0 返回的
                # 是 ls-tree 文本而非文件字节
                if os.path.isdir(fp) or _is_windows_device_path(fp):
                    logger.warning(f"外部文件恢复: 跳过非普通文件 {fp}")
                    continue
                repo, repo_key, source = ExternalFileBackup._resolve_base(task_id, fp, commit_hash)
                if source is None:
                    continue  # 基线 dict 缺该仓库条目：无备份可恢复
                rel = ExternalFileBackup._repo_rel(fp, repo_key)

                # 🔧 事务#2402：基线对象必须是 blob（tree/gitlink 的 git show
                # 同样成功但内容非文件字节，写回=制造垃圾）
                tcheck = subprocess.run(
                    ["git", "cat-file", "-t", f"{source}:{rel}"],
                    cwd=repo, capture_output=True, text=True, timeout=5,
                )
                if tcheck.returncode == 0 and tcheck.stdout.strip() != "blob":
                    logger.warning(f"外部文件恢复: 基线非blob，跳过 {source}:{rel}")
                    continue

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
                try:
                    with open(fp, "wb") as f:
                        f.write(result.stdout)
                except OSError as e:
                    # 🔧 事务#2402：单文件失败（占用/权限）不中断整批恢复
                    logger.warning(f"外部文件恢复: 写入失败跳过 {fp}: {e}")
                    continue
                restored += 1

            logger.info(
                f"外部文件恢复 task#{task_id}: {restored} 个文件 "
                f"(from {ExternalFileBackup._baseline_desc(commit_hash)})"
            )
            return restored

        except Exception as e:
            logger.warning(f"外部文件恢复失败 task#{task_id}: {e}")
            return 0

    @staticmethod
    def _baseline_desc(commit_spec: str | dict[str, str] | None) -> str:
        """基线 commit 的简短描述（用于日志）"""
        if commit_spec is None:
            return "HEAD"
        if isinstance(commit_spec, dict):
            return ",".join(f"{k or 'default'}:{str(v)[:8]}" for k, v in commit_spec.items())
        return str(commit_spec)[:8]

    # ==================== 公开接口（本地/远程分派） ====================

    @staticmethod
    def backup(
        task_id: int, file_paths: list[str], label: str = "",
        nest_name: str | None = None,
    ) -> str | dict[str, str] | None:
        """备份外部文件（🔧 自动分派本地/远程nest端执行）

        - 远程 nest 的 Windows 盘符路径 → nest 端同盘符根 \\.peng\\backups\\task#id
          （避免跨盘符复制；peng 主服务在 Linux 时远程路径本地不可访问）
        - 其余 → 本地仓库（默认 data / 本地Windows盘符仓库）

        Returns:
            仅默认仓库 → str；涉及盘符仓库 → dict {repo_key: commit}；失败 → None
        """
        if not file_paths:
            return None
        try:
            # 🔧 事务#2088：盘根路径入口兜底剔除（静态零IO；执行层另有
            # 浅目录/规模限额护栏，纵深防御）
            safe_paths = [fp for fp in file_paths if not is_root_volume_path(fp)]
            if len(safe_paths) < len(file_paths):
                dropped = [fp for fp in file_paths if is_root_volume_path(fp)]
                logger.warning(
                    f"外部文件备份入口剔除盘根路径(task#{task_id}): {dropped[:5]}"
                )
            if not safe_paths:
                return None
            local_files, remote_files = ExternalFileBackup._split_local_remote(
                nest_name, safe_paths
            )
            local_res = (
                ExternalFileBackup._backup_local(task_id, local_files, label)
                if local_files else None
            )
            remote_res = (
                ExternalFileBackup._remote_backup(nest_name, task_id, remote_files, label)
                if remote_files else None
            )
            return ExternalFileBackup._merge_commits(local_res, remote_res)
        except Exception as e:
            logger.warning(f"外部文件备份失败 task#{task_id}: {e}")
        return None

    @staticmethod
    def check_changes(
        task_id: int,
        file_paths: list[str],
        commit_hash: str | dict[str, str] | None = None,
        nest_name: str | None = None,
        strict_baseline: bool = False,
    ) -> list[str]:
        """检查外部文件是否有变化（本地+远程nest分派，多仓库基线兼容 str|dict|None）

        🔧 事务#2402：strict_baseline=True 为恢复集模式（files 来自累积
        manifest）——无基线/基线键失配的文件跳过不报（显示与恢复一致，
        恢复时它们同样被跳过）；默认 False 保留 #2107 语义（缺键视为新增）。
        """
        if not file_paths:
            return []
        local_files, remote_files = ExternalFileBackup._split_local_remote(
            nest_name, file_paths
        )
        changed: list[str] = []
        if local_files:
            changed.extend(
                ExternalFileBackup._check_changes_local(
                    task_id, local_files, commit_hash, strict_baseline
                )
            )
        if remote_files:
            changed.extend(
                ExternalFileBackup._remote_check_changes(
                    nest_name, task_id, remote_files, commit_hash, strict_baseline
                )
            )
        return changed

    @staticmethod
    def restore(
        task_id: int,
        file_paths: list[str],
        commit_hash: str | dict[str, str] | None = None,
        nest_name: str | None = None,
    ) -> int:
        """从备份恢复外部文件（本地+远程nest分派，多仓库基线兼容 str|dict|None）"""
        if not file_paths:
            return 0
        local_files, remote_files = ExternalFileBackup._split_local_remote(
            nest_name, file_paths
        )
        restored = 0
        if local_files:
            restored += ExternalFileBackup._restore_local(task_id, local_files, commit_hash)
        if remote_files:
            restored += ExternalFileBackup._remote_restore(
                nest_name, task_id, remote_files, commit_hash
            )
        logger.info(
            f"外部文件恢复 task#{task_id}: {restored} 个文件 "
            f"(from {ExternalFileBackup._baseline_desc(commit_hash)})"
        )
        return restored

    @staticmethod
    def get_all_heads(
        task_id: int, nest_name: str | None = None
    ) -> str | dict[str, str]:
        """获取全部外部文件备份仓库的 HEAD（本地默认/本地盘符 + 远程nest盘符）

        仅默认仓库有内容 → str（兼容旧 meta 格式）；涉及盘符仓库 → dict。
        """
        heads: dict[str, str] = {}
        local = ExternalFileBackup._get_all_heads_local(task_id)
        if isinstance(local, dict):
            heads.update(local)
        elif isinstance(local, str) and local:
            heads[""] = local
        if ExternalFileBackup._is_remote_nest(nest_name):
            try:
                heads.update(
                    ExternalFileBackup._remote_get_drive_heads(nest_name, task_id)
                )
            except Exception as e:
                logger.debug(f"外部文件基线: 远程盘符HEAD获取失败({nest_name}): {e}")
        if not heads:
            return ""
        if set(heads.keys()) == {""}:
            return heads[""]
        return heads

    @staticmethod
    def list_baseline_files(
        task_id: int, commit_spec: str | dict[str, str] | None,
        nest_name: str | None = None,
    ) -> list[str]:
        """列出基线 commit 中备份的全部外部文件绝对路径（本地+远程nest合并）"""
        if not commit_spec:
            return []
        abs_paths: list[str] = []
        # 远程盘符 key 部分远程执行；其余（"" 与本地盘符）本地执行
        if ExternalFileBackup._is_remote_nest(nest_name) and isinstance(commit_spec, dict):
            abs_paths.extend(
                ExternalFileBackup._remote_list_baseline(nest_name, task_id, commit_spec)
            )
            local_spec = {k: v for k, v in commit_spec.items() if not k}
            if not local_spec:
                return abs_paths
            abs_paths.extend(
                ExternalFileBackup._list_baseline_local(task_id, local_spec)
            )
            return abs_paths
        return ExternalFileBackup._list_baseline_local(task_id, commit_spec)

    @staticmethod
    def restore_all(
        task_id: int, commit_spec: str | dict[str, str] | None,
        nest_name: str | None = None,
        workdir: str = "",
        manifest: list[str] | None = None,
    ) -> int:
        """从基线 commit 恢复所有外部文件（本地+远程nest分派）

        🔧 workdir 提供时过滤基线文件集（git管理/系统路径不恢复——旧基线
        含历史误备份数据的兜底）
        🔧 事务#2107：manifest 提供时恢复集以其为准（本事务备份文件的精确
        清单，轻过滤不剔 git）——共享盘符仓库的 ls-tree 全量恢复会混入其他
        task 备份的文件，导致跨事务覆盖事故（#2080）。
        """
        if not commit_spec:
            return 0
        try:
            if manifest:
                # 精确恢复集：轻过滤（系统路径/盘根/排除目录，不剔 git）
                # 🔧 事务#2402：传 workdir 剔除祖先目录死条目（D:/work 等）
                abs_paths = ToolBackupManager._filter_manifest_paths(
                    list(manifest), workdir,
                )
            else:
                abs_paths = ExternalFileBackup.list_baseline_files(
                    task_id, commit_spec, nest_name
                )
                if workdir:
                    abs_paths = ToolBackupManager._filter_baseline_files(
                        abs_paths, workdir, nest_name
                    )
            if not abs_paths:
                return 0
            restored = ExternalFileBackup.restore(
                task_id, abs_paths, commit_spec, nest_name
            )
            logger.info(
                f"外部文件全量恢复 task#{task_id}: {restored} 个文件 "
                f"from {ExternalFileBackup._baseline_desc(commit_spec)}"
                + ("（manifest精确集）" if manifest else "")
            )
            return restored
        except Exception as e:
            logger.warning(f"外部文件全量恢复失败 task#{task_id}: {e}", exc_info=True)
            return 0

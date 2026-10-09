import logging
import shutil
import os
import stat
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def get_project_root() -> Path:
    """获取项目根目录（代理到 peng.config.loader.get_project_root）

    此函数提供统一的导入入口，避免调用方需要知道
    get_project_root 的实际定义位置。
    """
    from peng.config.loader import get_project_root as _get_project_root

    return _get_project_root()


def chown_to_project_owner(path: str | Path) -> None:
    """将文件/目录的 owner 设置为项目目录的所有者（而非 root）

    当 peng 服务以 root 身份运行（如 systemd 服务）时，创建的文件会归 root 所有，
    导致 local nest（以普通用户运行）无法读写。此函数将文件/目录的 owner 改为
    项目目录的所有者，确保 local nest 可以正常访问。

    仅在 Linux/POSIX 且当前进程为 root 时执行 chown，其他情况静默跳过。

    Args:
        path: 文件或目录路径（字符串或 Path 对象）
    """
    from peng.config.loader import get_project_root

    if os.name == "nt":
        return  # Windows 不需要 chown

    try:
        if os.getuid() != 0:
            return  # 非 root 用户，无需 chown
    except (AttributeError, OSError):
        return

    try:
        path_obj = Path(path)
        if not path_obj.exists():
            return

        # 获取项目根目录的 uid/gid 作为目标 owner
        project_root = get_project_root()
        if not project_root.exists():
            return

        dir_stat = project_root.stat()
        target_uid = dir_stat.st_uid
        target_gid = dir_stat.st_gid

        # 检查当前 owner 是否已经正确
        path_stat = path_obj.stat()
        if path_stat.st_uid == target_uid and path_stat.st_gid == target_gid:
            return  # 已经是正确的 owner

        # 如果是目录，设置权限为 rwxrwxr-x (775)，确保同组用户可读写
        # 如果是文件，设置权限为 rw-rw-r-- (664)，确保同组用户可读写
        if path_obj.is_dir():
            os.chmod(str(path_obj), stat.S_IRWXU | stat.S_IRWXG | stat.S_IROTH | stat.S_IXOTH)
        else:
            os.chmod(str(path_obj), stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH)

        os.chown(str(path_obj), target_uid, target_gid)
        logger.debug(f"chown {path} -> uid={target_uid}, gid={target_gid}")

    except (PermissionError, OSError) as e:
        logger.warning(f"chown {path} 失败（非致命）: {e}")


def open_log_with_owner(
    path: str | Path,
    mode: str = "a",
    encoding: str = "utf-8",
    errors: str = "replace",
):
    """打开日志文件，root 时确保（含新建）文件属主为项目属主

    解决的问题：以 root 身份运行 fly/nest 命令时，open() 新建的日志文件归
    root 所有，导致之后以普通用户（如 systemd 服务 User=feng）运行的进程
    无法写入（Permission denied），内置 local nest 启动失败。

    模式与 NestConnector.create_stop_signal 一致：先写文件，再 chown。
    （chown_to_project_owner 对不存在的文件是 no-op，必须在 open 之后调用）

    Args:
        path: 日志文件路径
        mode: 打开模式（默认追加 "a"）
        encoding: 文件编码
        errors: 编码错误处理策略

    Returns:
        已打开的文件对象
    """
    f = open(path, mode, encoding=encoding, errors=errors)
    chown_to_project_owner(path)
    return f


def get_project_owner_info() -> tuple[int, int, str] | None:
    """返回项目根目录属主的 (uid, gid, home)

    仅当当前进程为 root 且项目属主不是 root 时返回有效值（即需要降权的场景），
    其他情况（Windows / 非 root / 项目属主就是 root）返回 None。

    Returns:
        (uid, gid, home) 元组；home 可能为空字符串（无法查询 passwd 时）
    """
    if os.name == "nt":
        return None
    try:
        if os.getuid() != 0:
            return None  # 非 root 无需降权
    except (AttributeError, OSError):
        return None

    project_root = get_project_root()
    try:
        dir_stat = project_root.stat()
    except OSError:
        return None
    uid, gid = dir_stat.st_uid, dir_stat.st_gid
    if uid == 0:
        return None  # 项目属主就是 root，无需降权

    home = ""
    try:
        import pwd

        home = pwd.getpwuid(uid).pw_dir or ""
    except (KeyError, ImportError, OSError):
        pass
    return uid, gid, home


def get_popen_owner_context() -> tuple[dict | None, object]:
    """返回适合传给 subprocess.Popen 的 (env, preexec_fn)，
    使 root 启动的子进程降权为项目属主身份运行

    典型场景：root 运行 fly start 的手动兜底逻辑启动 nest connect 子进程时，
    若直接以 root 启动，子进程创建的一切文件（日志/缓存/__pycache__）都会
    归 root 所有，污染项目目录权限。降权为项目属主后从根源上避免此问题。

    用法:
        env, preexec = get_popen_owner_context()
        proc = subprocess.Popen(cmd, env=env, preexec_fn=preexec, ...)

    Returns:
        (env, preexec_fn) 二元组：
        - 无需降权时为 (None, None)（Popen 按默认行为执行）
        - env 为调整了 HOME 的环境副本（或 None 表示沿用默认）
        - preexec_fn 为降权函数（setgroups/setgid/setuid）
    """
    info = get_project_owner_info()
    if info is None:
        return None, None
    uid, gid, home = info

    def _demote() -> None:
        # fork 后 exec 前在子进程中执行：完整降权（清空补充组 → setgid → setuid）
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)

    env = None
    if home and os.environ.get("HOME") != home:
        env = os.environ.copy()
        env["HOME"] = home
    return env, _demote


def ensure_dir_with_owner(path: str | Path) -> Path:
    """创建目录并确保 owner 为项目目录的所有者

    结合 mkdir 和 chown_to_project_owner 的便捷函数。

    Args:
        path: 要创建的目录路径

    Returns:
        创建的目录 Path 对象
    """
    path_obj = Path(path)
    path_obj.mkdir(parents=True, exist_ok=True)
    chown_to_project_owner(path_obj)
    return path_obj


def set_working_directory(workdir: str | Path) -> bool:
    """设置工作目录并持久化到配置文件

    将工作目录保存到 config/webui.json 的 webui.workdir 字段。
    设置后会清除配置缓存，确保 get_working_directory() 立即获取到新值。

    Args:
        workdir: 工作目录路径（字符串或Path对象）

    Returns:
        True 表示设置成功，False 表示设置失败（路径无效等）

    Raises:
        无，所有异常都会被捕获并记录日志，返回 False
    """
    from peng.webui.routes.workdir_config import _save_workdir_to_config

    try:
        # 转换为 Path 对象并解析
        if isinstance(workdir, str):
            workdir = Path(workdir)

        # 展开用户目录符号并解析为绝对路径
        resolved_path = workdir.expanduser().resolve()

        # 验证路径有效性
        if not resolved_path.exists():
            logger.warning(f"set_working_directory: 路径不存在 - {resolved_path}")
            return False
        if not resolved_path.is_dir():
            logger.warning(f"set_working_directory: 路径不是目录 - {resolved_path}")
            return False

        # 实际切换工作目录
        os.chdir(str(resolved_path))
        logger.info(f"工作目录已切换: {resolved_path}")

        # 调用WebUI的配置保存函数
        return _save_workdir_to_config(str(resolved_path))

    except Exception as e:
        logger.error(f"set_working_directory: 异常 - {e}")
        return False


def get_working_directory() -> Path:
    """获取当前工作目录（pwd）

    优先级：
    1. config.webui.workdir（WebUI设置的工作目录）
    2. 项目根目录（默认）

    Returns:
        工作目录路径
    """
    from peng.config.loader import load_config

    try:
        config = load_config()
        if hasattr(config, "webui") and hasattr(config.webui, "workdir"):
            workdir = config.webui.workdir
            if workdir:
                resolved_path = Path(workdir).expanduser().resolve()
                if resolved_path.is_dir():
                    return resolved_path
    except Exception:
        pass

    # 默认使用当前工作目录
    return Path.cwd()


def _is_git_repo(directory: Path) -> bool:
    """检查目录是否是 git 仓库（普通仓库或 git worktree）

    - 普通仓库：.git 是目录，内含 config 文件
    - git worktree：.git 是文件（内容为 gitdir: 指向主仓库）

    Args:
        directory: 要检查的目录

    Returns:
        True 表示是 git 仓库（含 worktree）
    """
    git_entry = directory / ".git"
    if git_entry.is_dir():
        return (git_entry / "config").is_file()
    # git worktree：.git 是文件
    return git_entry.is_file()


def _ensure_gitignore_entry(git_root: Path, entry: str = ".peng") -> None:
    """确保 .gitignore 中包含指定条目

    Args:
        git_root: git 仓库根目录
        entry: 要添加到 .gitignore 的条目
    """
    gitignore_path = git_root / ".gitignore"
    try:
        if gitignore_path.exists():
            content = gitignore_path.read_text(encoding="utf-8")
            lines = content.splitlines()
            if entry in lines:
                return  # 已存在，无需添加
            # 追加到文件末尾
            if content and not content.endswith("\n"):
                content += "\n"
            content += entry + "\n"
        else:
            content = entry + "\n"
        gitignore_path.write_text(content, encoding="utf-8")
        logger.debug(f"已将 '{entry}' 添加到 {gitignore_path}")
    except Exception as e:
        logger.warning(f"更新 .gitignore 失败（非致命）: {e}")


def get_temp_directory() -> Path:
    """获取临时文件目录（自动选择最佳位置，自动创建）

    临时文件目录用于存储工具返回的大数据文件等需要持久化的临时内容。

    选择规则：
    1. 如果工作目录有 .git/config → 使用工作目录的 .peng 目录，并自动加入 .gitignore
    2. 否则使用 project_root/data/temp（原有默认行为）

    Returns:
        临时文件目录路径（已确保目录存在）
    """
    from peng.config.loader import get_project_root, load_config

    # 优先检查工作目录是否是 git 仓库
    workdir = get_working_directory()
    if _is_git_repo(workdir):
        temp_dir = workdir / ".peng"
        ensure_dir_with_owner(temp_dir)
        _ensure_gitignore_entry(workdir, ".peng")
        return temp_dir

    # 非 git 仓库，使用原有逻辑
    try:
        config = load_config()
        temp_dir = config.paths.temp_dir_path
        if not temp_dir or str(temp_dir) == ".":
            temp_dir = Path("data/temp")
        # 相对路径基于项目根目录解析
        if not temp_dir.is_absolute():
            temp_dir = get_project_root() / temp_dir
    except Exception:
        temp_dir = get_project_root() / "data/temp"
    ensure_dir_with_owner(temp_dir)
    return temp_dir


def is_in_working_directory(file_path: Path, workdir: Path = None) -> bool:
    """判断文件是否在工作目录内

    Args:
        file_path: 文件路径

    Returns:
        True 表示在工作目录内（不需要备份），False 表示在工作目录外（需要备份）

    工作目录外的文件需要备份，但临时目录除外。
    """
    working_dir = workdir or get_working_directory()

    # 将路径规范化为绝对路径
    resolved_file_path = file_path.resolve()
    resolved_working_dir = working_dir.resolve()

    # 检查是否在工作目录内
    return resolved_file_path.is_relative_to(resolved_working_dir)


def is_project_file(file_path: Path) -> bool:
    """判断是否为项目内文件（保护检查）

    Args:
        file_path: 文件路径（已 resolve）

    Returns:
        True 表示项目内文件，False 表示项目外文件
    """
    from peng.config.loader import load_config

    project_root = load_config().paths.project_root_path

    return file_path.is_relative_to(project_root)


def is_temporary_directory(file_path: Path) -> bool:
    """判断文件是否在临时目录中

    Args:
        file_path: 文件路径（已 resolve）

    Returns:
        True 表示在临时目录中（不需要备份），False 表示需要备份

    临时目录包括：
    - 配置的临时目录（config.paths.temp_dir，默认 data/temp）
    - 系统临时目录（/tmp, /var/tmp 等）
    - /var/log
    - ~/work/temp
    """
    # 检查配置的临时目录和系统临时目录
    temp_dirs = [
        get_temp_directory(),  # 配置的临时目录（data/temp）
        Path(tempfile.gettempdir()),  # 系统临时目录
        Path("/var/log"),
        Path.home() / "work" / "temp",
    ]

    # 检查文件是否在任何一个临时目录中
    for temp_dir in temp_dirs:
        if file_path.is_relative_to(temp_dir):
            return True

    return False


def resolve_local_data_path(file_path: str) -> str | None:
    """检查路径是否是 local nest 项目目录的 data/ 子目录路径，如果是则返回本地绝对路径

    确保 data/ 目录下的文件无论当前 nest 是什么，都能从本地项目目录访问。

    支持两种形式：
    1. 相对路径: "data/xxx" 或 "data/subdir/file" 或 "./data/xxx"
    2. 绝对路径: "/mnt/p/feng/work/peng/data/xxx" (只要恰好匹配本地 project_root/data)

    Args:
        file_path: 用户提供的文件路径

    Returns:
        本地绝对路径（如果匹配 data/ 目录），None（不匹配）
    """
    from peng.config.loader import get_project_root

    if not file_path:
        return None

    project_root = get_project_root()
    data_dir = (project_root / "data").resolve()

    path_obj = Path(file_path)

    # 情况1: 绝对路径，检查是否在本地 data/ 目录下
    if path_obj.is_absolute():
        try:
            resolved = path_obj.resolve()
            resolved.relative_to(data_dir)
            return str(resolved)
        except (ValueError, OSError):
            return None

    # 情况2: 相对路径，规范化后检查是否以 data/ 开头
    normalized = file_path.replace("\\", "/").lstrip("./")
    if normalized == "data" or normalized.startswith("data/"):
        resolved = (project_root / normalized).resolve()
        try:
            resolved.relative_to(data_dir)
            return str(resolved)
        except (ValueError, OSError):
            return None

    return None


def is_protected_project_path(file_path: Path | str) -> bool:
    """检查路径是否在项目目录内

    AI 不能通过 tool_call 对自身项目进行任何文件操作（包括读、写、编辑）。

    例外：
    - 配置文件中 allowed_tool_call_dirs 指定的目录允许读写
    - worktree 目录（.worktrees/）允许读写

    Args:
        file_path: 文件路径（Path 或 str）

    Returns:
        True 表示受保护（在项目目录内且不在排除列表中），False 表示可以操作
    """
    from peng.config.loader import load_config

    # 确保是 Path 对象
    if isinstance(file_path, str):
        file_path = Path(file_path)

    project_root = load_config().paths.project_root_path

    # 检查是否在项目目录内
    try:
        file_path.relative_to(project_root)
        # 在项目目录内，检查是否是允许读写的目录
        allowed_dirs = load_config().paths.allowed_tool_call_dirs_paths

        # 检查是否在 worktree 目录内
        worktree_dirs = [
            project_root / ".worktrees",
            project_root / "worktrees",
        ]
        for worktree_dir in worktree_dirs:
            if worktree_dir.exists():
                try:
                    file_path.relative_to(worktree_dir)
                    # 在 worktree 目录内，可以操作
                    return False
                except ValueError:
                    pass

        # 检查是否在配置允许的目录内
        for allowed_dir in allowed_dirs:
            try:
                file_path.relative_to(allowed_dir)
                # 在允许的目录内，可以操作
                return False
            except ValueError:
                pass
        # 不在允许目录内，受保护
        return True
    except ValueError:
        # 不在项目目录内，不受保护
        return False


def git_commit(git_root: Path, file_path: Path, external_path: Path = None) -> None:
    """提交备份文件到git

    Args:
        git_root: 备份根目录
        file_path: 备份文件路径
        external_path: 原文件路径(如果是外部文件)
    """
    try:
        # 添加备份文件到暂存区
        subprocess.run(
            ["git", "add", str(file_path.relative_to(git_root))],
            cwd=git_root,
            capture_output=True,
            check=True,
            timeout=10,  # 防止垃圾进程卡死项目
        )

        # 先提交
        commit_msg = f"backup: {external_path}\n\n"
        commit_msg += f"原文件: {external_path}\n"
        commit_msg += f"备份文件: {file_path}\n"
        commit_msg += f"时间: {datetime.now().isoformat()}"

        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=git_root,
            capture_output=True,
            check=True,
            timeout=10,  # 防止垃圾进程卡死项目
        )

        # 获取当前 commit hash
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H"],
            cwd=git_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,  # 防止垃圾进程卡死项目
        )
        commit_hash = result.stdout.strip()
        commit_hash_short = commit_hash[:8]

        # 计算备份文件相对路径
        backup_relative = file_path.relative_to(git_root)

        # 使用 --amend 修改 commit 信息，添加恢复命令
        amended_msg = commit_msg + f"\n\n"
        amended_msg += f"一键恢复：cd {git_root} && git checkout {commit_hash_short} -- {backup_relative} && cp {backup_relative} {external_path}"

        subprocess.run(
            ["git", "commit", "--amend", "-m", amended_msg],
            cwd=git_root,
            capture_output=True,
            check=True,
            timeout=10,  # 防止垃圾进程卡死项目
        )
    except subprocess.CalledProcessError:
        # 如果文件内容相同，git commit会失败，忽略即可
        pass


def backup_external_file(file_path: Path) -> Path | None:
    """备份工作目录外的文件（保留目录结构，git提交）

    备份规则：
    1. 按相对系统根目录的目录结构备份
       例如：/home/user/.bashrc -> ~/work/backups/home/user/.bashrc
    2. 备份路径固定，不使用时间戳
    3. 完全依赖 git 进行版本管理
    4. 备份目录必须是git仓库（如果不是则初始化）
    5. 备份后自动git add和git commit
    6. 特殊设备文件不备份
    7. 工作目录内文件不备份

    Args:
        file_path: 原文件路径

    Returns:
        备份文件路径，如果文件不存在则返回 None
    """
    from peng.config.loader import load_config

    if not file_path.exists():
        return None

    # 检查是否在工作目录内（在工作目录内或临时目录中不需要备份）
    if is_in_working_directory(file_path):
        return None

    # 特殊设备文件，不应该备份
    special_devices = {
        "/dev/null",
        "/dev/zero",
        "/dev/full",
        "/dev/random",
        "/dev/urandom",
        "/proc/self/fd/0",  # stdin
        "/proc/self/fd/1",  # stdout
        "/proc/self/fd/2",  # stderr
    }

    # 检查是否为特殊设备
    file_path_str = str(file_path)
    if file_path_str in special_devices:
        return None

    backup_root = load_config().paths.backups_dir_path
    backup_root.mkdir(parents=True, exist_ok=True)

    # 按相对根目录的路径结构备份（固定路径，无时间戳）
    try:
        relative_path = file_path.relative_to("/")
    except ValueError:
        # Windows 系统，使用盘符作为根
        relative_path = file_path.relative_to(file_path.anchor)

    # 备份路径：backups/home/user/.bashrc（固定路径）
    backup_path = backup_root / relative_path

    # 创建备份目录
    backup_path.parent.mkdir(parents=True, exist_ok=True)

    # 复制文件
    shutil.copy2(file_path, backup_path)

    # Git add和commit
    git_commit(backup_root, backup_path, file_path)

    return backup_path

#!/usr/bin/env python3
"""
PostgreSQL自动部署模块（支持多种部署方式，跨平台）

优先级（Linux）：
1. PostgreSQL已运行 - 直接使用
2. 使用Docker部署 - 快速、隔离
3. 提供手动安装指引 - 最灵活

优先级（Windows）：
1. PostgreSQL已运行 - 直接使用
2. 查找已安装的PostgreSQL（scoop/winget/手动安装）
3. 未安装时自动通过 scoop → winget 安装（无需管理员权限）
4. 初始化数据集群（initdb）
5. 启动PostgreSQL服务（pg_ctl start）
6. 创建用户和数据库
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Literal

from peng.db.connection import get_config

logger = logging.getLogger(__name__)

# 全局变量：记录是否已执行过自动部署
_auto_deploy_done: bool = False
_container_name: str = "peng-postgres"

# PostgreSQL数据目录（从配置读取，默认值）
_data_root: Path | None = None
_peng_data_dir: Path | None = None


def _get_postgresql_data_dir() -> Path:
    """
    从配置获取 PostgreSQL 数据目录（跨平台）

    优先级：
    1. 配置文件中的 postgresql_data_dir
    2. 配置文件中的 fallback_postgresql_data_dir（如果主目录创建失败）

    Windows 默认路径: LOCALAPPDATA/peng/db/postgresql
    Linux 默认路径: /mnt/wsl/work/db/postgresql

    Returns:
        PostgreSQL 数据目录路径
    """
    global _data_root, _peng_data_dir

    if _data_root is not None:
        return _data_root

    # 根据平台确定默认路径
    if sys.platform == "win32":
        default_data_path = str(
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            / "peng"
            / "db"
            / "postgresql"
        )
    else:
        default_data_path = "/mnt/wsl/work/db/postgresql"

    try:
        # 从配置模型直接读取 postgresql_data_dir（不走 get_path，因为该函数不包含此键）
        from peng.config.loader import load_config

        paths_cfg = load_config().paths
        config_path = paths_cfg.postgresql_data_dir
        fallback_path = paths_cfg.fallback_postgresql_data_dir
        data_root = Path(config_path).expanduser()
        fallback_root = Path(fallback_path).expanduser()
    except Exception as e:
        logger.warning(f"⚠️  无法读取配置中的 PostgreSQL 数据目录: {e}")
        data_root = Path(default_data_path)
        fallback_root = Path.home() / ".db" / "postgresql"

    # 检查目录是否存在，不存在则创建
    try:
        if not data_root.exists():
            logger.info(f"📁 创建 PostgreSQL 数据目录: {data_root}")
            data_root.mkdir(parents=True, exist_ok=True)
        else:
            logger.info(f"✓ PostgreSQL 数据目录已存在: {data_root}")
    except (PermissionError, OSError) as e:
        # 权限不足或其他错误，尝试使用备用目录
        logger.warning(f"⚠️  无法创建 {data_root}（{e}），使用备用目录: {fallback_root}")
        data_root = fallback_root
        try:
            data_root.mkdir(parents=True, exist_ok=True)
            logger.info(f"✓ 已创建备用数据目录: {data_root}")
        except Exception as e:
            logger.error(f"❌ 创建备用数据目录失败: {e}")
            raise
    except Exception as e:
        logger.error(f"❌ 创建 PostgreSQL 数据目录失败: {e}")
        raise

    _data_root = data_root
    _peng_data_dir = data_root / "peng"

    return _data_root


def _check_port_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    """检查指定端口是否可达（socket 级别检测，不依赖 psycopg2）"""
    import socket

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def check_postgresql_available() -> bool:
    """
    检查PostgreSQL是否可用

    Returns:
        True表示可用，False表示不可用
    """
    try:
        config = get_config()

        # 尝试连接
        import psycopg2

        conn = psycopg2.connect(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            database=config.database,
            connect_timeout=5,
        )
        conn.close()

        logger.info(f"✓ PostgreSQL已可用: {config.host}:{config.port}/{config.database}")
        return True

    except psycopg2.OperationalError:
        return False
    except (UnicodeDecodeError, UnicodeEncodeError) as e:
        # Windows: psycopg2 可能因 GBK 编码问题抛出 UnicodeDecodeError
        # 但 PG 可能实际可用，用 socket 检测端口是否可达
        logger.warning(f"psycopg2 编码异常，尝试 socket 检测: {e}")
        return _check_port_reachable(config.host, config.port)
    except Exception as e:
        logger.error(f"检查PostgreSQL时出错: {type(e).__name__}: {e}")
        return False


def check_docker_available() -> bool:
    """
    检查Docker是否可用

    Returns:
        True表示可用，False表示不可用
    """
    try:
        import docker

        docker_client = docker.from_env()
        docker_client.ping()
        logger.info("✓ Docker已可用")
        return True

    except ImportError:
        logger.debug("Docker Python SDK未安装")
        return False
    except Exception as e:
        logger.debug(f"Docker不可用: {e}")
        return False


def cleanup_postgresql_idle_connections() -> None:
    """
    清理PostgreSQL的idle连接

    在启动新服务之前，清理所有idle状态的连接，
    防止PostgreSQL连接数达到上限。
    """
    try:
        config = get_config()

        import psycopg2
        from psycopg2 import sql
        from psycopg2.extras import RealDictCursor

        # 连接到postgres数据库（系统数据库）
        conn = psycopg2.connect(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            database="postgres",  # 连接到系统数据库
            connect_timeout=5,
        )

        conn.autocommit = True
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        # 查询所有idle连接
        cursor.execute(
            """
            SELECT pid, usename, application_name, state, query_start
            FROM pg_stat_activity
            WHERE datname = %s
              AND state = 'idle'
              AND pid <> pg_backend_pid()
              AND query_start < now() - interval '1 minute'
            ORDER BY query_start
        """,
            (config.database,),
        )

        idle_connections = cursor.fetchall()

        if not idle_connections:
            logger.debug("✅ 没有需要清理的idle连接")
            conn.close()
            return

        # 终止idle连接
        terminated_count = 0
        for row in idle_connections:
            pid = row["pid"]
            usename = row["usename"]
            app_name = row["application_name"]

            try:
                cursor.execute("SELECT pg_terminate_backend(%s)", (pid,))
                terminated_count += 1
                logger.debug(f"  已终止连接: PID={pid}, 用户={usename}, 应用={app_name}")
            except Exception as e:
                logger.warning(f"  终止连接失败: PID={pid}, 错误={e}")

        logger.info(f"✅ 已清理 {terminated_count} 个idle连接")

        cursor.close()
        conn.close()

    except UnicodeDecodeError as e:
        logger.warning(f"⚠️  清理idle连接失败（编码问题，可忽略）: {e}")
    except Exception as e:
        logger.warning(f"⚠️  清理idle连接失败（可忽略）: {type(e).__name__}: {e}")


def check_tables_exist() -> bool:
    """
    检查数据库表是否存在

    Returns:
        True表示表存在，False表示表不存在
    """
    try:
        config = get_config()

        import psycopg2

        conn = psycopg2.connect(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            database=config.database,
            connect_timeout=5,
        )

        cursor = conn.cursor()
        cursor.execute("""
            SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                ORDER BY table_name;
        """)
        tables = cursor.fetchall()

        cursor.close()
        conn.close()

        if tables:
            table_names = [t[0] for t in tables]
            logger.info(f"✓ 数据库表已存在: {table_names}")
            return True
        else:
            logger.warning(f"⚠️  数据库表不存在")
            return False

    except Exception as e:
        logger.error(f"❌ 检查数据库表时出错: {e}")
        return False


def deploy_with_docker() -> bool:
    """
    使用Docker自动部署PostgreSQL

    Returns:
        True表示部署成功，False表示失败
    """
    global _container_name, _peng_data_dir

    try:
        import docker

        docker_client = docker.from_env()

        config = get_config()

        # 获取数据目录（会自动检查/创建）
        _data_root = _get_postgresql_data_dir()
        _peng_data_dir = _data_root / "peng"

        logger.info(f"✓ 数据卷: {_data_root}")

        # 不预先创建 _peng_data_dir，让Docker容器自己创建

        # 检查容器是否已存在
        try:
            existing_container = docker_client.containers.get(_container_name)
            if existing_container:
                # 容器已存在，检查是否使用了正确的卷挂载
                if "/var/lib/postgresql" in str(existing_container.attrs["Mounts"]):
                    # 容器配置正确，检查运行状态
                    if existing_container.status == "running":
                        logger.info(f"✓ PostgreSQL容器已在运行: {_container_name}")
                        # 检查并配置 WAL
                        logger.info("🔧 检查 WAL 配置...")
                        if not _configure_wal_in_container(existing_container, config):
                            logger.warning("⚠️  WAL 配置失败，PostgreSQL 将使用默认配置")
                        return True
                    else:
                        # 容器存在但未运行，启动它
                        existing_container.start()
                        logger.info(f"✓ 已启动PostgreSQL容器: {_container_name}")
                        time.sleep(2)  # 等待容器启动
                        # 检查并配置 WAL
                        logger.info("🔧 检查 WAL 配置...")
                        if not _configure_wal_in_container(existing_container, config):
                            logger.warning("⚠️  WAL 配置失败，PostgreSQL 将使用默认配置")
                        return True
                else:
                    # 容器配置不正确，需要重建
                    logger.warning(f"⚠️  PostgreSQL容器配置已更新，需要重建")
                    existing_container.stop()
                    time.sleep(2)
                    existing_container.remove()
                    logger.info(f"✓ 已删除旧容器: {_container_name}")

        except Exception:
            pass  # 容器不存在，继续创建

        # 创建并启动容器（使用卷挂载）
        logger.info(f"🚀 正在使用Docker部署PostgreSQL...")
        logger.info(f"   容器名: {_container_name}")
        logger.info(f"   端口: {config.port}")
        logger.info(f"   数据库: {config.database}")
        logger.info(f"   用户: {config.user}")
        logger.info(f"   数据目录: {_data_root}")
        logger.info(f"   Peng数据: {_peng_data_dir}")

        container = docker_client.containers.run(
            "postgres:14",
            name=_container_name,
            environment={
                "POSTGRES_USER": config.user,
                "POSTGRES_PASSWORD": config.password,
                "POSTGRES_DB": config.database,
                "PGDATA": "/var/lib/postgresql/data",  # 明确指定数据目录
            },
            ports={f"{config.port}": "5432"},
            volumes={
                # 映射PostgreSQL数据目录到容器
                str(_data_root): {"bind": "/var/lib/postgresql", "mode": "rw"}
            },
            detach=True,
            restart_policy={"Name": "unless-stopped"},
        )

        logger.info("✓ PostgreSQL容器已创建并启动")

        # 配置 WAL 功能
        logger.info("🔧 正在配置 WAL 功能...")
        if not _configure_wal_in_container(container, config):
            logger.warning("⚠️  WAL 配置失败，PostgreSQL 将使用默认配置")
        else:
            logger.info("✓ WAL 功能配置成功")

        # 等待PostgreSQL启动
        logger.info("⏳ 等待PostgreSQL启动...")
        max_wait = 30  # 最多等待30秒
        for i in range(max_wait):
            time.sleep(1)
            try:
                import psycopg2

                conn = psycopg2.connect(
                    host=config.host,
                    port=config.port,
                    user=config.user,
                    password=config.password,
                    database=config.database,
                    connect_timeout=2,
                )
                conn.close()
                logger.info(f"✓ PostgreSQL已就绪 (耗时{i + 1}秒)")
                return True

            except Exception:
                if (i + 1) % 5 == 0:
                    logger.debug(f"   等待中... ({i + 1}/{max_wait}秒)")
                continue

        logger.error(f"❌ PostgreSQL启动超时 (超过{max_wait}秒)")
        return False

    except Exception as e:
        logger.error(f"❌ Docker部署PostgreSQL时出错: {e}")
        return False


def _configure_wal_in_container(container, config) -> bool:
    """
    在 Docker 容器内配置 WAL 功能

    Args:
        container: Docker 容器对象
        config: PostgreSQL 配置

    Returns:
        True 表示配置成功，False 表示失败
    """
    try:
        import psycopg2

        # 从 peng.config 读取 WAL 配置
        from peng.config import load_config

        peng_config = load_config()
        wal_config = {}

        if peng_config.db and "postgresql" in peng_config.db:
            postgresql_config = peng_config.db["postgresql"]
            wal_config = postgresql_config.get("wal", {})
            logger.info(f"✓ 从 config/db.json 读取 WAL 配置: {wal_config}")

        # 默认 WAL 配置
        default_wal_settings = {
            "wal_level": "replica",
            "fsync": "on",
            "synchronous_commit": "on",
            "max_wal_size": "1GB",
            "min_wal_size": "80MB",
            "wal_keep_size": "128MB",
        }

        # 合并配置（配置文件覆盖默认值）
        wal_settings = {**default_wal_settings, **wal_config}

        logger.info(f"🔧 使用 WAL 配置: {wal_settings}")

        # 等待容器启动完成
        max_wait = 10
        for i in range(max_wait):
            time.sleep(1)
            try:
                conn = psycopg2.connect(
                    host=config.host,
                    port=config.port,
                    user=config.user,
                    password=config.password,
                    database="postgres",  # 连接到系统数据库
                    connect_timeout=2,
                )
                conn.autocommit = True
                cursor = conn.cursor()
                break
            except Exception:
                if i == max_wait - 1:
                    raise
                continue
        else:
            logger.error("❌ 等待容器启动超时")
            return False

        try:
            # 使用 ALTER SYSTEM 设置 WAL 参数
            for param, value in wal_settings.items():
                cursor.execute(f"ALTER SYSTEM SET {param} = '{value}';")
                logger.debug(f"  设置 {param} = {value}")

            logger.info("✓ WAL 参数已设置，需要重启容器生效")

            # 检查当前 WAL 配置
            cursor.execute("SHOW wal_level;")
            wal_level = cursor.fetchone()[0]
            logger.info(f"  当前 wal_level: {wal_level}")

            cursor.execute("SHOW fsync;")
            fsync = cursor.fetchone()[0]
            logger.info(f"  当前 fsync: {fsync}")

            cursor.execute("SHOW max_wal_size;")
            max_wal_size = cursor.fetchone()[0]
            logger.info(f"  当前 max_wal_size: {max_wal_size}")

            cursor.execute("SHOW min_wal_size;")
            min_wal_size = cursor.fetchone()[0]
            logger.info(f"  当前 min_wal_size: {min_wal_size}")

            cursor.close()
            conn.close()

            # 重启容器使 WAL 配置生效
            logger.info("🔄 重启容器使 WAL 配置生效...")
            container.restart()
            time.sleep(5)  # 等待容器重启（增加等待时间）

            # 验证 WAL 是否生效（带重试）
            logger.info("✓ 验证 WAL 配置...")
            max_retry = 5
            for attempt in range(max_retry):
                try:
                    conn = psycopg2.connect(
                        host=config.host,
                        port=config.port,
                        user=config.user,
                        password=config.password,
                        database=config.database,
                        connect_timeout=5,
                    )
                    conn.autocommit = True
                    cursor = conn.cursor()
                    break
                except Exception as e:
                    if attempt < max_retry - 1:
                        logger.debug(f"  连接失败，重试中... ({attempt + 1}/{max_retry})")
                        time.sleep(2)
                    else:
                        raise

            cursor.execute("SHOW wal_level;")
            new_wal_level = cursor.fetchone()[0]
            logger.info(f"  ✓ wal_level = {new_wal_level}")

            cursor.execute("SHOW fsync;")
            new_fsync = cursor.fetchone()[0]
            logger.info(f"  ✓ fsync = {new_fsync}")

            cursor.execute("SHOW max_wal_size;")
            max_wal_size = cursor.fetchone()[0]
            logger.info(f"  ✓ max_wal_size = {max_wal_size}")

            cursor.execute("SHOW min_wal_size;")
            min_wal_size = cursor.fetchone()[0]
            logger.info(f"  ✓ min_wal_size = {min_wal_size}")

            cursor.close()
            conn.close()

            return True

        except Exception as e:
            logger.error(f"❌ 配置 WAL 时出错: {e}")
            cursor.close()
            conn.close()
            return False

    except Exception as e:
        logger.error(f"❌ 配置 WAL 时出错: {e}")
        return False


# ==================== Windows 原生 PostgreSQL 部署 ====================


def _get_windows_user_home() -> Path | None:
    """
    获取 Windows 用户主目录，兼容服务环境

    在 Windows 服务环境下，Path.home() 返回 C:/Windows/System32/config/systemprofile
    而非用户主目录 C:/Users/feng。

    搜索顺序：
    1. USERPROFILE 环境变量（最可靠）
    2. HOMEPATH 环境变量
    3. HOMEDRIVE + HOMEPATH
    4. 遍历 C:/Users/ 下的用户目录
    5. 从peng配置文件读取自定义路径
    6. Path.home() 作为最后备选

    Returns:
        用户主目录路径，未找到返回 None
    """
    import traceback
    import sys

    def debug_print(msg):
        try:
            print(f"[DEBUG] {msg}")
            sys.stdout.flush()
        except UnicodeEncodeError:
            safe_msg = msg.encode('ascii', errors='replace').decode('ascii')
            print(f"[DEBUG] {safe_msg}")
            sys.stdout.flush()

    # 方式1：USERPROFILE 环境变量（最可靠，即使在服务环境中也可能设置）
    userprofile = os.environ.get("USERPROFILE")
    debug_print(f"USERPROFILE={userprofile}")
    if userprofile:
        home = Path(userprofile)
        if home.exists():
            debug_print(f"✓ 通过 USERPROFILE 找到用户主目录: {home}")
            return home

    # 方式2：HOMEPATH + HOMEDRIVE
    homepath = os.environ.get("HOMEPATH")
    homedrive = os.environ.get("HOMEDRIVE", "C:")
    debug_print(f"HOMEPATH={homepath}, HOMEDRIVE={homedrive}")
    if homepath:
        home = Path(homedrive) / homepath.lstrip("\\/")
        if home.exists():
            debug_print(f"✓ 通过 HOMEPATH 找到用户主目录: {home}")
            return home

    # 方式3：遍历 C:/Users/ 下的用户目录
    users_dir = Path("C:/Users")
    debug_print(f"尝试遍历 C:/Users, exists={users_dir.exists()}")
    if users_dir.exists():
        try:
            entries = list(users_dir.iterdir())
            debug_print(f"C:/Users 下有 {len(entries)} 个条目: {[e.name for e in entries]}")
            # 第一轮：优先找有 scoop/apps/postgresql 的用户（最可能是正确的用户）
            for user_dir in entries:
                if user_dir.is_dir() and not user_dir.name.startswith("$"):
                    if (
                        user_dir
                        / "scoop"
                        / "apps"
                        / "postgresql"
                        / "current"
                        / "bin"
                        / "pg_ctl.exe"
                    ).exists():
                        debug_print(f"✓ 找到有 scoop PG 的用户主目录: {user_dir}")
                        return user_dir
            # 第二轮：找任何有 AppData 的用户
            for user_dir in entries:
                if user_dir.is_dir() and not user_dir.name.startswith("$"):
                    debug_print(f"检查用户目录: {user_dir}")
                    if (user_dir / "AppData").exists():
                        debug_print(f"✓ 遍历找到用户主目录: {user_dir}")
                        return user_dir
        except PermissionError as e:
            debug_print(f"遍历 C:/Users 时权限不足: {e}")
        except Exception as e:
            debug_print(f"遍历 C:/Users 时出错: {type(e).__name__}: {e}")

    # 方式4：从peng配置文件读取自定义路径
    try:
        config = get_config()
        if hasattr(config, "user_home") and config.user_home:
            home = Path(config.user_home)
            if home.exists():
                debug_print(f"✓ 从配置文件找到用户主目录: {home}")
                return home
    except Exception as e:
        debug_print(f"从配置文件读取用户主目录失败: {e}")

    # 方式5：Path.home() 作为最后备选
    home = Path.home()
    debug_print(f"⚠️  使用 Path.home() 作为备选: {home}")
    return home


def _find_pg_bin_on_windows() -> Path | None:
    """
    在 Windows 上查找已安装的 PostgreSQL bin 目录

    搜索顺序：
    1. scoop 安装路径（使用用户主目录）
    2. winget/MSI 默认安装路径（PostgreSQL 14-18）
    3. PATH 环境变量中的 pg_ctl

    Returns:
        pg_ctl.exe 的父目录（bin 目录），未找到返回 None
    """
    import shutil
    import sys

    def debug_print(msg):
        try:
            print(f"[DEBUG] {msg}")
            sys.stdout.flush()
        except UnicodeEncodeError:
            safe_msg = msg.encode('ascii', errors='replace').decode('ascii')
            print(f"[DEBUG] {safe_msg}")
            sys.stdout.flush()

    # 获取用户主目录，兼容服务环境
    user_home = _get_windows_user_home() or Path.home()
    debug_print(f"当前使用的用户主目录: {user_home}")

    # 构建搜索路径列表
    search_paths = []

    # 1. scoop 安装路径（使用用户主目录）
    search_paths.append(user_home / "scoop" / "apps" / "postgresql" / "current" / "bin")

    # 2. 尝试常见用户名路径（兼容SYSTEM账户无法访问用户目录的情况）
    common_users = ["feng", "Administrator", "Default"]
    for username in common_users:
        search_paths.append(Path(f"C:/Users/{username}/scoop/apps/postgresql/current/bin"))

    # 3. winget/MSI 默认安装路径（PostgreSQL 14-18）
    search_paths.extend(
        [
            Path("C:/Program Files/PostgreSQL/18/bin"),
            Path("C:/Program Files/PostgreSQL/17/bin"),
            Path("C:/Program Files/PostgreSQL/16/bin"),
            Path("C:/Program Files/PostgreSQL/15/bin"),
            Path("C:/Program Files/PostgreSQL/14/bin"),
        ]
    )

    # 去重
    seen = set()
    unique_paths = []
    for p in search_paths:
        if p not in seen:
            seen.add(p)
            unique_paths.append(p)

    for path in unique_paths:
        pg_ctl = path / "pg_ctl.exe"
        exists = pg_ctl.exists()
        debug_print(f"检查 {path} -> pg_ctl.exe exists={exists}")
        if exists:
            debug_print(f"✓ 找到 PostgreSQL: {path}")
            return path

    # 尝试从 PATH 环境变量查找
    pg_ctl_in_path = shutil.which("pg_ctl")
    debug_print(f"PATH 中查找 pg_ctl: {pg_ctl_in_path}")
    if pg_ctl_in_path:
        debug_print(f"✓ 通过 PATH 找到 PostgreSQL: {pg_ctl_in_path}")
        return Path(pg_ctl_in_path).parent

    # 尝试从注册表查找
    try:
        import winreg

        try:
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\PostgreSQL\Installations\PostgreSQL"
            )
            for i in range(1024):
                try:
                    value, _, _ = winreg.EnumValue(key, i)
                    if "Directory" in value:
                        install_dir = winreg.QueryValueEx(key, value)[0]
                        pg_bin = Path(install_dir) / "bin"
                        if (pg_bin / "pg_ctl.exe").exists():
                            debug_print(f"✓ 从注册表找到 PostgreSQL: {pg_bin}")
                            return pg_bin
                except OSError:
                    break
            winreg.CloseKey(key)
        except FileNotFoundError:
            pass
    except ImportError:
        pass

    debug_print(f"所有搜索路径均未找到 PostgreSQL")
    return None


def _get_pg_data_dir_on_windows() -> Path:
    """
    获取 Windows 上 PostgreSQL 的数据目录

    在 Windows 服务环境下，LOCALAPPDATA 可能指向 C:/Windows/System32/config/systemprofile/AppData/Local
    而非用户目录 C:/Users/feng/AppData/Local。
    """
    # 优先使用真实用户目录（而非systemprofile）
    user_home = _get_windows_user_home() or Path.home()

    # 如果找到的用户目录是systemprofile，尝试用C:/Users下第一个有AppData的用户
    # 优先选择已有 PG 数据目录的用户
    if "systemprofile" in str(user_home).lower():
        users_dir = Path("C:/Users")
        if users_dir.exists():
            try:
                # 第一轮：找已有 PG 数据目录的用户
                for d in users_dir.iterdir():
                    if d.is_dir() and not d.name.startswith("$") and (d / "AppData").exists():
                        alt_data = d / "AppData" / "Local" / "peng" / "db" / "postgresql" / "data"
                        if alt_data.exists():
                            logger.info(f"找到已有数据目录(跳过systemprofile): {alt_data}")
                            return alt_data
                # 第二轮：找有 scoop 安装 PG 的用户
                for d in users_dir.iterdir():
                    if d.is_dir() and not d.name.startswith("$") and (d / "AppData").exists():
                        scoop_pg = (
                            d / "scoop" / "apps" / "postgresql" / "current" / "bin" / "pg_ctl.exe"
                        )
                        if scoop_pg.exists():
                            logger.info(f"找到 scoop PG 用户(跳过systemprofile): {d}")
                            data_dir = (
                                d / "AppData" / "Local" / "peng" / "db" / "postgresql" / "data"
                            )
                            data_dir.mkdir(parents=True, exist_ok=True)
                            return data_dir
            except Exception:
                pass

    # 尝试从 LOCALAPPDATA 环境变量获取
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        data_dir = Path(local_appdata) / "peng" / "db" / "postgresql" / "data"
        if "systemprofile" not in str(data_dir).lower():
            data_dir.mkdir(parents=True, exist_ok=True)
            return data_dir

    # 使用用户主目录的 AppData/Local
    data_dir = user_home / "AppData" / "Local" / "peng" / "db" / "postgresql" / "data"
    # 如果仍在systemprofile下，强制用C:/Users/下的用户目录
    if "systemprofile" in str(data_dir).lower():
        users_dir = Path("C:/Users")
        if users_dir.exists():
            try:
                for d in users_dir.iterdir():
                    if d.is_dir() and not d.name.startswith("$") and (d / "AppData").exists():
                        data_dir = d / "AppData" / "Local" / "peng" / "db" / "postgresql" / "data"
                        logger.info(f"强制使用用户数据目录(跳过systemprofile): {data_dir}")
                        break
            except Exception:
                pass
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _init_pg_cluster_on_windows(pg_bin: Path, data_dir: Path) -> bool:
    """
    初始化 PostgreSQL 数据集群（Windows）

    Args:
        pg_bin: PostgreSQL bin 目录
        data_dir: 数据目录

    Returns:
        True 表示成功
    """
    pg_ctl = pg_bin / "pg_ctl.exe"
    initdb = pg_bin / "initdb.exe"

    # 使用 initdb 初始化
    if not (data_dir / "PG_VERSION").exists():
        print("🔧 初始化 PostgreSQL 数据集群...")
        sys.stdout.flush()
        result = subprocess.run(
            [str(initdb), "-D", str(data_dir), "-U", "postgres", "-E", "UTF8"],
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            print(f"❌ initdb 失败 (exit code: {result.returncode})")
            sys.stdout.flush()
            return False
        print("✓ initdb 成功")

    # 修改 pg_hba.conf 允许本地连接（先用 trust 模式，创建用户后再改回 md5）
    pg_hba = data_dir / "pg_hba.conf"
    if pg_hba.exists():
        pg_hba.write_text(
            "# TYPE  DATABASE  USER      ADDRESS        METHOD\n"
            "host    all       all       127.0.0.1/32   trust\n"
            "host    all       all       ::1/128         trust\n",
            encoding="utf-8",
        )
        print("✓ pg_hba.conf 已配置（trust 模式）")
        sys.stdout.flush()

    # 配置 postgresql.conf（端口、日志编码等）
    _configure_postgresql_conf(data_dir)

    return True


def _get_excluded_port_ranges() -> list[tuple[int, int]]:
    """
    获取Windows排除端口范围列表（带缓存）

    Returns:
        [(start, end), ...] 排除端口范围列表
    """
    cache_key = "_excluded_port_ranges_cache"
    cached = getattr(_get_excluded_port_ranges, cache_key, None)
    if cached is not None:
        return cached

    ranges = []
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["netsh", "interface", "ipv4", "show", "excludedportrange", "protocol=tcp"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            if result.returncode == 0 and result.stdout:
                for line in result.stdout.split("\n"):
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        try:
                            start_port = int(parts[0])
                            end_port = int(parts[1])
                            ranges.append((start_port, end_port))
                        except (ValueError, IndexError):
                            continue
        except subprocess.TimeoutExpired:
            logger.warning("netsh 查询排除端口超时，使用已知常见范围")
            # Hyper-V/WSL常见排除范围
            ranges = [(5357, 5957), (50000, 50059)]
        except Exception as e:
            logger.warning(f"检查排除端口范围失败: {e}")
            ranges = [(5357, 5957), (50000, 50059)]

    setattr(_get_excluded_port_ranges, cache_key, ranges)
    return ranges


def _is_port_excluded(port: int) -> bool:
    """
    检查端口是否在Windows排除端口范围内（Hyper-V/WSL保留）

    Returns:
        True 表示端口在排除范围内
    """
    if sys.platform != "win32":
        return False
    for start, end in _get_excluded_port_ranges():
        if start <= port <= end:
            return True
    return False


def _find_safe_port(original_port: int) -> int:
    """
    找一个不在Windows排除范围内的安全端口

    从original_port开始向上搜索，找到第一个安全的端口。
    """
    # 常见的安全候选端口（避开Windows排除范围5357-5957）
    candidates = [6000, 6001, 7000, 7001, 8000, 8001, 9000, 9001]
    if original_port not in candidates:
        candidates.insert(0, original_port)

    for port in candidates:
        if not _is_port_excluded(port):
            return port

    # 如果所有候选都不行，从original_port+1开始线性搜索
    for offset in range(1, 100):
        port = original_port + offset
        if not _is_port_excluded(port):
            return port

    return original_port  # 兜底返回原端口


def _configure_postgresql_conf(data_dir: Path) -> bool:
    """
    修改 postgresql.conf 配置文件

    Returns:
        True 表示成功
    """
    # 修改 postgresql.conf 端口
    config = get_config()
    postgresql_conf = data_dir / "postgresql.conf"

    # Windows: 检查配置端口是否在系统排除范围内（Hyper-V/WSL保留端口）
    actual_port = config.port
    if sys.platform == "win32":
        excluded_port = _is_port_excluded(config.port)
        if excluded_port:
            # 找一个安全的端口
            actual_port = _find_safe_port(config.port)
            if actual_port != config.port:
                print(f"⚠️  端口 {config.port} 在Windows排除范围内，自动改为 {actual_port}")
                sys.stdout.flush()
                # 更新 postgresql.conf 中的端口，但不改 db.json
                # db.json 保持 5432 以维持 Linux 兼容性
                # Windows 上 psycopg2 连接时需要用 actual_port
                # 通过环境变量或临时修改连接配置来传递
                os.environ["PENG_PG_PORT_OVERRIDE"] = str(actual_port)

    if postgresql_conf.exists():
        conf_lines = []
        with open(postgresql_conf, "r", encoding="utf-8") as f:
            for line in f:
                # 替换端口配置
                if line.strip().startswith("#port") or line.strip().startswith("port"):
                    conf_lines.append(f"port = {actual_port}\n")
                # 禁用 logging_collector，由 pg_ctl -l 管理日志输出
                # 避免两者同时写 postgresql.log 导致 Windows 文件共享冲突
                elif line.strip().startswith("#logging_collector") or line.strip().startswith(
                    "logging_collector"
                ):
                    conf_lines.append("logging_collector = off\n")
                else:
                    conf_lines.append(line)
        # 确保端口配置存在
        if not any(l.strip().startswith("port =") for l in conf_lines):
            conf_lines.append(f"\nport = {actual_port}\n")
        # 禁用 logging_collector（防止与 pg_ctl -l 的文件共享冲突）
        if not any("logging_collector" in l for l in conf_lines):
            conf_lines.append("logging_collector = off\n")
        # 强制 PostgreSQL 日志使用英文（防止 Windows 中文版 GBK 乱码）
        # 注意：需要替换已有的 lc_messages 行，而非仅在没有时添加
        lc_messages_found = False
        for i, line in enumerate(conf_lines):
            if line.strip().startswith("lc_messages") and not line.strip().startswith("#"):
                conf_lines[i] = "lc_messages = 'C'\n"
                lc_messages_found = True
                break
        if not lc_messages_found:
            conf_lines.append("lc_messages = 'C'\n")
        # 移除之前可能添加的 client_encoding = 'UTF8'（可能导致连接问题）
        for i, line in enumerate(conf_lines):
            if (
                line.strip().startswith("client_encoding")
                and not line.strip().startswith("#")
                and "UTF8" in line
            ):
                conf_lines[i] = (
                    "#client_encoding = sql_ascii\t\t# actually, defaults to database encoding\n"
                )
                break
        # 恢复完成后自动 promote（而非默认的 pause）
        # 避免非正常关机后重启时卡在 PAUSED 状态导致服务无法启动
        if not any(
            "recovery_target_action" in l and not l.strip().startswith("#") for l in conf_lines
        ):
            conf_lines.append("recovery_target_action = 'promote'\n")
        postgresql_conf.write_text("".join(conf_lines), encoding="utf-8")
        print(
            f"✓ postgresql.conf 已配置（port={actual_port}, lc_messages=C, logging_collector=off)"
        )
        sys.stdout.flush()

    return True


def _ensure_pg_nssm_service(pg_bin: Path, data_dir: Path) -> bool:
    """
    用 nssm 将 PostgreSQL 注册为 Windows 系统服务

    当 pg_ctl start 失败（通常是权限不足，如以 SYSTEM 账户运行时无法绑定端口），
    自动用 nssm 注册 PostgreSQL 为系统服务来启动。

    Args:
        pg_bin: PostgreSQL bin 目录
        data_dir: 数据目录

    Returns:
        True 表示成功
    """
    import sys
    import shutil

    # 查找 nssm 路径（PATH中可能找不到，尝试常见路径）
    nssm_path = shutil.which("nssm")
    if not nssm_path:
        # 在SYSTEM账户环境下PATH可能不包含scoop，尝试常见路径
        user_home = _get_windows_user_home() or Path.home()
        for candidate in [
            user_home / "scoop" / "apps" / "nssm" / "current" / "nssm.exe",
            user_home / "scoop" / "shims" / "nssm.exe",
            Path("C:/Users/feng/scoop/apps/nssm/current/nssm.exe"),
            Path("C:/nssm/nssm.exe"),
        ]:
            if candidate.exists():
                nssm_path = str(candidate)
                print(f"✓ 找到 nssm: {nssm_path}")
                sys.stdout.flush()
                break
    if not nssm_path:
        print("⚠️  pg_ctl start 失败，且未找到 nssm，无法注册为系统服务")
        sys.stdout.flush()
        return False

    service_name = "postgresql_peng"
    pg_ctl = pg_bin / "pg_ctl.exe"
    pg_log_dir = data_dir.parent
    pg_log_dir.mkdir(parents=True, exist_ok=True)

    print(f"🔧 尝试用 nssm 注册 PostgreSQL 为系统服务 [{service_name}]...")
    sys.stdout.flush()

    try:
        # 先停止可能已有的同名服务
        subprocess.run(
            [nssm_path, "stop", service_name],
            capture_output=True,
            timeout=10,
        )

        # 删除可能已有的同名服务
        subprocess.run(
            [nssm_path, "remove", service_name, "confirm"],
            capture_output=True,
            timeout=10,
        )

        # 注册新服务
        result = subprocess.run(
            [nssm_path, "install", service_name, str(pg_ctl)],
            capture_output=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            print(f"❌ nssm install 失败: {result.stderr.strip()}")
            sys.stdout.flush()
            return False

        # 配置服务参数
        subprocess.run(
            [nssm_path, "set", service_name, "AppDirectory", str(pg_bin)],
            capture_output=True,
            timeout=10,
        )
        subprocess.run(
            [
                nssm_path,
                "set",
                service_name,
                "AppParameters",
                f'start -D "{data_dir}" -l "{data_dir / "postgresql.log"}"',
            ],
            capture_output=True,
            timeout=10,
        )
        subprocess.run(
            [nssm_path, "set", service_name, "AppStdout", str(pg_log_dir / "pg_stdout.log")],
            capture_output=True,
            timeout=10,
        )
        subprocess.run(
            [nssm_path, "set", service_name, "AppStderr", str(pg_log_dir / "pg_stderr.log")],
            capture_output=True,
            timeout=10,
        )
        subprocess.run(
            [nssm_path, "set", service_name, "AppRotateFiles", "1"],
            capture_output=True,
            timeout=10,
        )
        subprocess.run(
            [nssm_path, "set", service_name, "AppRotateBytes", "10485760"],
            capture_output=True,
            timeout=10,
        )
        # 设置自动启动
        subprocess.run(
            [nssm_path, "set", service_name, "Start", "SERVICE_AUTO_START"],
            capture_output=True,
            timeout=10,
        )
        # 设置 PATH 环境变量包含 PG bin 目录，防止 SYSTEM 账户找不到 DLL 导致 backend 崩溃
        # (0xC0000142 STATUS_DLL_INIT_FAILED)
        # 使用 AppEnvironmentExtra 追加到现有环境变量，不覆盖系统 PATH
        subprocess.run(
            [nssm_path, "set", service_name, "AppEnvironmentExtra", f"PATH={pg_bin};%PATH%"],
            capture_output=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )

        # 启动服务
        result = subprocess.run(
            [nssm_path, "start", service_name],
            capture_output=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            print(f"❌ nssm start 失败: {result.stderr.strip()}")
            sys.stdout.flush()
            return False

        print(f"✅ PostgreSQL 系统服务 [{service_name}] 已启动")
        sys.stdout.flush()
        return True

    except Exception as e:
        print(f"❌ nssm 注册/启动 PostgreSQL 服务异常: {e}")
        sys.stdout.flush()
        return False


def _start_pg_on_windows(pg_bin: Path, data_dir: Path) -> bool:
    """
    启动 PostgreSQL 服务（Windows）

    策略：
    1. 先尝试 pg_ctl start（普通方式）
    2. 如果失败（权限不足等），自动用 nssm 注册为系统服务再启动

    Args:
        pg_bin: PostgreSQL bin 目录
        data_dir: 数据目录

    Returns:
        True 表示成功
    """
    import sys

    pg_ctl = pg_bin / "pg_ctl.exe"
    log_file = data_dir / "postgresql.log"

    # 先清理可能残留的 postmaster.pid（上次异常关闭可能留下）
    postmaster_pid = data_dir / "postmaster.pid"
    if postmaster_pid.exists():
        # 用 pg_ctl status 检查 PostgreSQL 是否真的在运行
        status_result = subprocess.run(
            [str(pg_ctl), "-D", str(data_dir), "status"],
            capture_output=True,
            timeout=5,
            encoding="utf-8",
            errors="replace",
        )
        if status_result.returncode != 0:
            # PostgreSQL 没在运行，postmaster.pid 是残留文件，删除它
            print(f"   清理残留的 postmaster.pid...")
            sys.stdout.flush()
            try:
                postmaster_pid.unlink()
            except Exception:
                pass

    # 不使用 -w 参数：让 pg_ctl start 立即返回，不等待 PostgreSQL 就绪
    # 不使用 capture_output：避免 Windows 管道死锁
    print(f"   执行: {pg_ctl} -D {data_dir} -l {log_file} start")
    sys.stdout.flush()

    proc = subprocess.Popen(
        [str(pg_ctl), "-D", str(data_dir), "-l", str(log_file), "start"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # pg_ctl start 应该很快返回（通常1-3秒）
    try:
        stdout, stderr = proc.communicate(timeout=15)
        returncode = proc.returncode
        stdout_text = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
        stderr_text = stderr.decode("utf-8", errors="replace").strip() if stderr else ""

        if returncode != 0:
            print(f"❌ pg_ctl start 失败 (exit code: {returncode})")
            if stdout_text:
                print(f"   stdout: {stdout_text}")
            if stderr_text:
                print(f"   stderr: {stderr_text}")
            sys.stdout.flush()

            # 读取日志帮助诊断
            _print_pg_log_tail(log_file, 10)

            # pg_ctl 失败，尝试用 nssm 注册为系统服务
            print("⚠️  pg_ctl start 失败，尝试用 nssm 注册为系统服务...")
            sys.stdout.flush()
            if _ensure_pg_nssm_service(pg_bin, data_dir):
                # nssm 服务启动成功，等待就绪
                pass  # 继续下面的等待就绪逻辑
            else:
                return False
    except subprocess.TimeoutExpired:
        # pg_ctl 卡住了（不应该发生，但防御性处理）
        proc.kill()
        proc.wait()
        print("⚠️  pg_ctl start 超时（15秒），尝试继续检查 PostgreSQL...")
        sys.stdout.flush()

    # pg_ctl start 已执行，等待 PostgreSQL 真正就绪（用连接测试代替 pg_ctl -w）
    print("   等待 PostgreSQL 就绪...")
    sys.stdout.flush()

    config = get_config()
    max_wait = 30  # 最多等待30秒
    for i in range(max_wait):
        time.sleep(1)
        try:
            import psycopg2

            # 用 postgres 超级用户测试连接（peng 用户可能还没创建）
            conn = psycopg2.connect(
                host=config.host,
                port=config.port,
                user="postgres",
                database="postgres",
                connect_timeout=2,
            )
            conn.close()
            print(f"✓ PostgreSQL 已就绪 ({i + 1}秒)")
            sys.stdout.flush()
            return True
        except Exception:
            if (i + 1) % 5 == 0:
                print(f"   等待中... ({i + 1}/{max_wait}秒)")
                sys.stdout.flush()

    # 超时，打印诊断信息
    print(f"❌ PostgreSQL 启动超时（{max_wait}秒）")
    _print_pg_log_tail(log_file, 15)
    sys.stdout.flush()
    return False


def _print_pg_log_tail(log_file: Path, num_lines: int = 10) -> None:
    """打印 PostgreSQL 日志末尾的指定行数（自动处理 Windows GBK 编码）"""
    if not log_file.exists():
        return
    try:
        log_content = log_file.read_text(encoding="gbk", errors="replace")
    except Exception:
        try:
            log_content = log_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return
    if log_content.strip():
        last_lines = log_content.strip().split("\n")[-num_lines:]
        print(f"   PostgreSQL 日志（最后{num_lines}行）:")
        for line in last_lines:
            print(f"   {line}")
        sys.stdout.flush()


def _create_user_and_db_on_windows(pg_bin: Path) -> bool:
    """
    创建用户和数据库（Windows）

    Args:
        pg_bin: PostgreSQL bin 目录

    Returns:
        True 表示成功
    """
    config = get_config()
    psql = pg_bin / "psql.exe"

    psql_args = [
        str(psql),
        "-h",
        "127.0.0.1",
        "-p",
        str(config.port),
        "-U",
        "postgres",
        "-d",
        "postgres",
    ]

    # subprocess kwargs：用 utf-8 + errors=replace 避免 Windows GBK 解码崩溃
    run_kwargs = dict(capture_output=True, timeout=10, encoding="utf-8", errors="replace")

    # 创建用户（如果不存在）
    print(f"   创建用户: {config.user}")
    sys.stdout.flush()
    result = subprocess.run(
        psql_args
        + [
            "-c",
            f"DO $do$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{config.user}') THEN CREATE ROLE {config.user} WITH LOGIN PASSWORD '{config.password}'; END IF; END $do$;",
        ],
        **run_kwargs,
    )
    if result.returncode != 0:
        err = result.stderr or f"(exit code: {result.returncode})"
        print(f"   ⚠️ 创建用户失败: {err.strip()}")
        sys.stdout.flush()

    # 创建数据库（如果不存在）- CREATE DATABASE 不能在 DO 块里执行，直接用 psql -c
    print(f"   创建数据库: {config.database}")
    sys.stdout.flush()
    # 先检查数据库是否存在
    result = subprocess.run(
        psql_args
        + ["-t", "-A", "-c", f"SELECT 1 FROM pg_database WHERE datname = '{config.database}'"],
        **run_kwargs,
    )
    db_exists = result.stdout and result.stdout.strip() == "1"
    if not db_exists:
        result = subprocess.run(
            psql_args + ["-c", f"CREATE DATABASE {config.database} OWNER {config.user};"],
            **run_kwargs,
        )
        if result.returncode != 0:
            err = result.stderr or f"(exit code: {result.returncode})"
            print(f"   ⚠️ 创建数据库失败: {err.strip()}")
            sys.stdout.flush()
        else:
            print(f"   ✓ 数据库 {config.database} 已创建")
    else:
        print(f"   ✓ 数据库 {config.database} 已存在")

    # 授予权限
    subprocess.run(
        psql_args + ["-c", f"GRANT ALL PRIVILEGES ON DATABASE {config.database} TO {config.user};"],
        **run_kwargs,
    )

    print("✓ 用户和数据库已创建")
    sys.stdout.flush()

    # 创建用户后，将 pg_hba.conf 从 trust 改为 md5（安全）
    data_dir = _get_pg_data_dir_on_windows()
    pg_hba = data_dir / "pg_hba.conf"
    if pg_hba.exists():
        pg_hba.write_text(
            "# TYPE  DATABASE  USER      ADDRESS        METHOD\n"
            "host    all       all       127.0.0.1/32   md5\n"
            "host    all       all       ::1/128         md5\n",
            encoding="utf-8",
        )
        print("✓ pg_hba.conf 已切换为 md5 认证模式")
        sys.stdout.flush()

        # 重载 PostgreSQL 配置（不需要重启）
        pg_ctl = pg_bin / "pg_ctl.exe"
        subprocess.run(
            [str(pg_ctl), "-D", str(data_dir), "reload"],
            capture_output=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )

    return True


def _try_install_pg_on_windows() -> Path | None:
    """
    尝试在 Windows 上自动安装 PostgreSQL（通过 scoop/winget，无需管理员权限）

    安装过程输出直接打印到终端，让用户看到进度。

    Returns:
        安装后的 bin 目录路径，失败返回 None
    """
    import shutil

    # 注意：不能用 CREATE_NO_WINDOW，否则子进程输出不显示到控制台！
    # 优先级1：scoop（开发者的首选包管理器，无需管理员）
    scoop_path = shutil.which("scoop")
    if scoop_path:
        print(f"🚀 使用 scoop 安装 PostgreSQL（路径: {scoop_path}）...")
        sys.stdout.flush()
        try:
            result = subprocess.run(
                f'"{scoop_path}" install postgresql',
                timeout=120,
                shell=True,
            )
            if result.returncode == 0:
                print("✅ scoop 安装 PostgreSQL 成功")
                pg_bin = _find_pg_bin_on_windows()
                if pg_bin:
                    return pg_bin
                print("⚠️ scoop 安装成功但找不到 pg_ctl，可能需要重启终端")
            else:
                print(f"⚠️ scoop 安装失败 (exit code: {result.returncode})")
        except subprocess.TimeoutExpired:
            print("⚠️ scoop 安装超时（120秒）")
        except (FileNotFoundError, OSError) as e:
            print(f"⚠️ scoop 安装异常: {e}")
    else:
        print("ℹ️ 未找到 scoop，尝试 winget...")

    # 优先级2：winget（Windows 自带包管理器，无需管理员）
    winget_path = shutil.which("winget")
    if winget_path:
        print("🚀 使用 winget 安装 PostgreSQL...")
        sys.stdout.flush()
        try:
            result = subprocess.run(
                "winget install -e --id PostgreSQL.PostgreSQL "
                "--accept-package-agreements --accept-source-agreements "
                "--disable-interactivity",
                timeout=180,
                shell=True,
            )
            if result.returncode == 0:
                print("✅ winget 安装 PostgreSQL 成功")
                pg_bin = _find_pg_bin_on_windows()
                if pg_bin:
                    return pg_bin
                print("⚠️ winget 安装成功但找不到 pg_ctl，可能需要重启终端或刷新 PATH")
            else:
                print(f"⚠️ winget 安装失败 (exit code: {result.returncode})")
        except subprocess.TimeoutExpired:
            print("⚠️ winget 安装超时（180秒）")
        except (FileNotFoundError, OSError) as e:
            print(f"⚠️ winget 安装异常: {e}")
    else:
        print("ℹ️ 未找到 winget")

    return None


def deploy_with_windows_native() -> bool:
    """
    Windows 平台自动部署 PostgreSQL（使用 scoop/winget + pg_ctl，无需管理员权限）

    部署流程：
    1. 查找已安装的 PostgreSQL
    2. 未安装时自动通过 scoop → winget 安装
    3. 初始化数据集群（initdb）
    4. 启动 PostgreSQL（pg_ctl start）
    5. 创建用户和数据库

    Returns:
        True 表示部署成功
    """
    try:
        # Windows: 检测端口排除范围，设置覆盖端口（在get_config之前）
        if not os.environ.get("PENG_PG_PORT_OVERRIDE"):
            try:
                from peng.db.connection import get_config as _get_db_config

                _cfg = _get_db_config()
                if _is_port_excluded(_cfg.port):
                    _safe_port = _find_safe_port(_cfg.port)
                    os.environ["PENG_PG_PORT_OVERRIDE"] = str(_safe_port)
                    # 持久化到文件（服务重启后环境变量丢失）
                    try:
                        from pathlib import Path as _P
                        import sys as _sys

                        _override_file = (
                            _P(_sys.executable).resolve().parent.parent.parent
                            / "config"
                            / "db_port_win"
                        )
                        _override_file.parent.mkdir(parents=True, exist_ok=True)
                        _override_file.write_text(str(_safe_port))
                    except Exception:
                        pass
            except Exception:
                pass

        config = get_config()

        # 第1步：查找已安装的 PostgreSQL
        print("📋 [1/6] 查找已安装的 PostgreSQL...")
        sys.stdout.flush()
        pg_bin = _find_pg_bin_on_windows()

        if not pg_bin:
            # 第2步：尝试自动安装
            print("📦 [2/6] PostgreSQL 未安装，尝试自动安装...")
            sys.stdout.flush()
            pg_bin = _try_install_pg_on_windows()

            if not pg_bin:
                print("❌ 自动安装失败")
                sys.stdout.flush()
                show_manual_install_guide()
                return False
        else:
            print(f"✓ 找到 PostgreSQL: {pg_bin}")

        # 第2.5步：确保 postgresql.conf 配置正确（每次都检查，修复已有的 GBK 编码问题）
        data_dir = _get_pg_data_dir_on_windows()
        if data_dir.exists() and (data_dir / "postgresql.conf").exists():
            _configure_postgresql_conf(data_dir)

        # 第3步：检查 PostgreSQL 是否已经在运行
        print("📋 [3/6] 检查 PostgreSQL 是否已运行...")
        sys.stdout.flush()
        # 先用socket直接检测实际端口是否可达（避免psycopg2依赖和端口不匹配问题）
        import socket as _socket

        for _check_port in [config.port, 6000, 5432]:
            try:
                _sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                _sock.settimeout(2)
                if _sock.connect_ex(("127.0.0.1", _check_port)) == 0:
                    _sock.close()
                    print(f"✅ PostgreSQL 已运行（端口 {_check_port}）")
                    # 更新端口覆盖到实际可达的端口
                    if _check_port != 5432:
                        os.environ["PENG_PG_PORT_OVERRIDE"] = str(_check_port)
                    # 重载配置（使 postgresql.conf 修改生效）
                    try:
                        _pg_ctl = pg_bin / "pg_ctl.exe"
                        subprocess.run(
                            [str(_pg_ctl), "reload", "-D", str(data_dir)],
                            capture_output=True,
                            timeout=5,
                            encoding="utf-8",
                            errors="replace",
                        )
                    except Exception:
                        pass
                    return True
                _sock.close()
            except Exception:
                pass
        # socket检测失败，再用psycopg2检测
        if check_postgresql_available():
            print("✅ PostgreSQL 已运行")
            return True

        # 第4步：初始化数据集群
        print("🔧 [4/6] 初始化数据集群...")
        sys.stdout.flush()
        data_dir = _get_pg_data_dir_on_windows()
        if not _init_pg_cluster_on_windows(pg_bin, data_dir):
            return False

        # 第5步：启动 PostgreSQL（pg_ctl start + 轮询检测就绪）
        print("🚀 [5/6] 启动 PostgreSQL...")
        sys.stdout.flush()
        if not _start_pg_on_windows(pg_bin, data_dir):
            return False

        # 创建用户和数据库
        print("🔧 创建用户和数据库...")
        sys.stdout.flush()
        if not _create_user_and_db_on_windows(pg_bin):
            return False

        print("✅ Windows 原生 PostgreSQL 部署完成！")
        sys.stdout.flush()
        return True
    except Exception as e:
        print(f"❌ Windows 原生 PostgreSQL 部署异常: {e}\n{traceback.format_exc()}")
        sys.stdout.flush()
        show_manual_install_guide()
        return False


def _detect_linux_pkg_manager() -> str | None:
    """
    检测 Linux 包管理器类型

    Returns:
        "apt", "dnf", "pacman", "apk" 或 None
    """
    import shutil

    if shutil.which("apt-get"):
        return "apt"
    if shutil.which("dnf"):
        return "dnf"
    if shutil.which("pacman"):
        return "pacman"
    if shutil.which("apk"):
        return "apk"
    return None


def _find_pg_bin_on_linux() -> Path | None:
    """
    在 Linux 上查找 PostgreSQL bin 目录

    搜索顺序：
    1. /usr/lib/postgresql/*/bin（Debian/Ubuntu 标准路径）
    2. /usr/bin（RHEL/Fedora 通常直接放在这里）
    3. PATH 中的 pg_ctl

    Returns:
        pg_ctl 的父目录（bin 目录），未找到返回 None
    """
    import shutil

    # Debian/Ubuntu: /usr/lib/postgresql/<version>/bin/pg_ctl
    pg_lib = Path("/usr/lib/postgresql")
    if pg_lib.exists():
        # 找到最高版本
        versions = sorted(
            [d.name for d in pg_lib.iterdir() if d.is_dir() and d.name[0].isdigit()],
            key=lambda v: tuple(int(x) for x in v.split(".")),
            reverse=True,
        )
        for ver in versions:
            pg_ctl = pg_lib / ver / "bin" / "pg_ctl"
            if pg_ctl.exists():
                return pg_ctl.parent

    # 检查 /usr/bin/pg_ctl（RHEL/Fedora/Arch）
    pg_ctl = shutil.which("pg_ctl")
    if pg_ctl:
        return Path(pg_ctl).parent

    return None


def _find_pg_data_dir_on_linux() -> Path:
    """
    获取 Linux 上 PostgreSQL 的数据目录

    Returns:
        数据目录路径
    """
    # 优先从配置获取
    try:
        data_dir = _get_postgresql_data_dir()
        if data_dir:
            return data_dir
    except Exception:
        pass

    # Debian/Ubuntu 默认数据目录
    pg_lib = Path("/usr/lib/postgresql")
    if pg_lib.exists():
        versions = sorted(
            [d.name for d in pg_lib.iterdir() if d.is_dir() and d.name[0].isdigit()],
            key=lambda v: tuple(int(x) for x in v.split(".")),
            reverse=True,
        )
        for ver in versions:
            default_data = Path(f"/var/lib/postgresql/{ver}/main")
            if default_data.exists():
                return default_data

    return Path("/var/lib/postgresql/data")


def deploy_with_linux_native() -> bool:
    """
    Linux 平台使用系统包管理器自动部署 PostgreSQL

    部署流程：
    1. 检查 PostgreSQL 是否已安装
    2. 使用包管理器安装（apt-get/dnf/pacman）
    3. 启动 PostgreSQL 服务
    4. 创建用户和数据库

    Returns:
        True 表示部署成功
    """
    try:
        config = get_config()
        pkg_manager = _detect_linux_pkg_manager()

        # 第1步：检查是否已安装
        pg_bin = _find_pg_bin_on_linux()
        if pg_bin:
            print(f"✓ 找到已安装的 PostgreSQL: {pg_bin}")
            sys.stdout.flush()
        else:
            # 第2步：安装 PostgreSQL
            if not pkg_manager:
                print("❌ 未检测到支持的包管理器（apt/dnf/pacman/apk）")
                sys.stdout.flush()
                return False

            print(f"📦 使用 {pkg_manager} 安装 PostgreSQL...")
            sys.stdout.flush()

            install_cmd = None
            if pkg_manager == "apt":
                install_cmd = [
                    "sudo",
                    "apt-get",
                    "install",
                    "-y",
                    "postgresql",
                    "postgresql-contrib",
                ]
            elif pkg_manager == "dnf":
                install_cmd = ["sudo", "dnf", "install", "-y", "postgresql", "postgresql-server"]
            elif pkg_manager == "pacman":
                install_cmd = ["sudo", "pacman", "-S", "--noconfirm", "postgresql"]
            elif pkg_manager == "apk":
                install_cmd = ["sudo", "apk", "add", "postgresql", "postgresql-contrib"]

            if not install_cmd:
                print(f"❌ 不支持的包管理器: {pkg_manager}")
                sys.stdout.flush()
                return False

            print(f"   执行: {' '.join(install_cmd)}")
            sys.stdout.flush()
            result = subprocess.run(
                install_cmd,
                capture_output=True,
                text=True,
                timeout=300,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                print(f"❌ 安装失败:\n{result.stderr or f'(exit code: {result.returncode})'}")
                sys.stdout.flush()
                return False

            print("✓ PostgreSQL 安装成功")
            sys.stdout.flush()

            # 重新查找 pg_bin
            pg_bin = _find_pg_bin_on_linux()
            if not pg_bin:
                print("❌ 安装成功但找不到 pg_ctl")
                sys.stdout.flush()
                return False

        # 第3步：启动 PostgreSQL 服务
        print("🚀 启动 PostgreSQL 服务...")
        sys.stdout.flush()

        # 尝试 systemctl 启动（最常见的方式）
        started = False
        for service_name in ["postgresql", "postgresql@14-main", "postgresql@16-main"]:
            result = subprocess.run(
                ["sudo", "systemctl", "start", service_name],
                capture_output=True,
                text=True,
                timeout=30,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode == 0:
                # 同时启用开机自启
                subprocess.run(
                    ["sudo", "systemctl", "enable", service_name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                print(f"✓ PostgreSQL 服务已启动 ({service_name})")
                sys.stdout.flush()
                started = True
                break

        if not started:
            # 尝试用 pg_ctl 直接启动（无需 systemd）
            pg_ctl = pg_bin / "pg_ctl"
            data_dir = _find_pg_data_dir_on_linux()
            if pg_ctl.exists() and data_dir.exists():
                print(f"   尝试 pg_ctl start: {pg_ctl} -D {data_dir}")
                sys.stdout.flush()
                result = subprocess.run(
                    [
                        str(pg_ctl),
                        "-D",
                        str(data_dir),
                        "-l",
                        str(data_dir / "postgresql.log"),
                        "start",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    encoding="utf-8",
                    errors="replace",
                )
                if result.returncode == 0:
                    print("✓ PostgreSQL 已通过 pg_ctl 启动")
                    sys.stdout.flush()
                    started = True

        if not started:
            print("⚠️ PostgreSQL 服务启动失败，尝试继续配置...")
            sys.stdout.flush()

        # 等待 PostgreSQL 就绪
        print("⏳ 等待 PostgreSQL 就绪...")
        sys.stdout.flush()
        max_wait = 30
        for i in range(max_wait):
            time.sleep(1)
            if check_postgresql_available():
                print(f"✓ PostgreSQL 已就绪 ({i + 1}秒)")
                sys.stdout.flush()
                break
            if (i + 1) % 5 == 0:
                print(f"   等待中... ({i + 1}/{max_wait}秒)")
                sys.stdout.flush()
        else:
            print(f"❌ PostgreSQL 启动超时（{max_wait}秒）")
            sys.stdout.flush()
            return False

        # 第4步：创建用户和数据库
        print("🔧 配置用户和数据库...")
        sys.stdout.flush()
        if not _create_user_and_db_on_linux(config):
            return False

        print("✅ Linux 原生 PostgreSQL 部署完成！")
        sys.stdout.flush()
        return True

    except Exception as e:
        print(f"❌ Linux 原生 PostgreSQL 部署异常: {e}\n{traceback.format_exc()}")
        sys.stdout.flush()
        return False


def _create_user_and_db_on_linux(config) -> bool:
    """
    在 Linux 上创建 PostgreSQL 用户和数据库

    使用 sudo -u postgres psql 来执行 SQL 命令

    Args:
        config: 数据库配置对象

    Returns:
        True 表示成功
    """
    # 查找 psql 路径
    import shutil

    psql = shutil.which("psql")
    if not psql:
        pg_bin = _find_pg_bin_on_linux()
        if pg_bin:
            psql = str(pg_bin / "psql")
    if not psql:
        print("❌ 找不到 psql 命令")
        sys.stdout.flush()
        return False

    run_kwargs = dict(
        capture_output=True,
        timeout=10,
        encoding="utf-8",
        errors="replace",
    )

    base_args = ["sudo", "-u", "postgres", psql, "-t", "-A"]

    # 创建用户（如果不存在）
    print(f"   创建用户: {config.user}")
    sys.stdout.flush()
    result = subprocess.run(
        base_args + ["-c", f"SELECT 1 FROM pg_roles WHERE rolname = '{config.user}'"],
        **run_kwargs,
    )
    user_exists = result.stdout and result.stdout.strip() == "1"
    if not user_exists:
        result = subprocess.run(
            base_args
            + ["-c", f"CREATE ROLE {config.user} WITH LOGIN PASSWORD '{config.password}';"],
            **run_kwargs,
        )
        if result.returncode != 0:
            print(f"   ⚠️ 创建用户失败: {result.stderr.strip()}")
            sys.stdout.flush()
        else:
            print(f"   ✓ 用户 {config.user} 已创建")
            sys.stdout.flush()

    # 创建数据库（如果不存在）
    print(f"   创建数据库: {config.database}")
    sys.stdout.flush()
    result = subprocess.run(
        base_args + ["-c", f"SELECT 1 FROM pg_database WHERE datname = '{config.database}'"],
        **run_kwargs,
    )
    db_exists = result.stdout and result.stdout.strip() == "1"
    if not db_exists:
        result = subprocess.run(
            base_args + ["-c", f"CREATE DATABASE {config.database} OWNER {config.user};"],
            **run_kwargs,
        )
        if result.returncode != 0:
            print(f"   ⚠️ 创建数据库失败: {result.stderr.strip()}")
            sys.stdout.flush()
        else:
            print(f"   ✓ 数据库 {config.database} 已创建")
            sys.stdout.flush()
    else:
        print(f"   ✓ 数据库 {config.database} 已存在")
        sys.stdout.flush()

    # 授予权限
    subprocess.run(
        base_args + ["-c", f"GRANT ALL PRIVILEGES ON DATABASE {config.database} TO {config.user};"],
        **run_kwargs,
    )

    # 配置 pg_hba.conf 允许密码认证（md5）
    _configure_pg_hba_on_linux()

    print("✓ 用户和数据库已配置")
    sys.stdout.flush()
    return True


def _configure_pg_hba_on_linux() -> None:
    """
    配置 Linux 上 PostgreSQL 的 pg_hba.conf，确保允许密码认证
    """
    import glob as glob_mod

    # 常见的 pg_hba.conf 位置
    search_patterns = [
        "/etc/postgresql/*/main/pg_hba.conf",
        "/etc/postgresql/*/pg_hba.conf",
        "/var/lib/pgsql/data/pg_hba.conf",
    ]

    pg_hba_files = []
    for pattern in search_patterns:
        pg_hba_files.extend(glob_mod.glob(pattern))

    for pg_hba_path in pg_hba_files:
        pg_hba = Path(pg_hba_path)
        if not pg_hba.exists():
            continue

        try:
            content = pg_hba.read_text(encoding="utf-8", errors="replace")
            lines = content.split("\n")
            new_lines = []
            modified = False

            # 确保有 md5 认证的 host 行
            has_host_md5 = False
            for line in lines:
                stripped = line.strip()
                # 跳过注释和空行
                if not stripped or stripped.startswith("#"):
                    new_lines.append(line)
                    continue

                parts = stripped.split()
                if len(parts) >= 4 and parts[0] == "host":
                    # 将 peer/ident 改为 md5（对于 TCP 连接）
                    if parts[-1] in ("peer", "ident"):
                        parts[-1] = "md5"
                        new_lines.append("  ".join(parts))
                        modified = True
                        continue
                    if parts[-1] == "md5":
                        has_host_md5 = True

                new_lines.append(line)

            # 如果没有 md5 的 host 行，添加
            if not has_host_md5:
                new_lines.append("host    all    all    127.0.0.1/32    md5")
                new_lines.append("host    all    all    ::1/128         md5")
                modified = True

            if modified:
                pg_hba.write_text("\n".join(new_lines), encoding="utf-8")
                print(f"   ✓ 已更新 {pg_hba_path}")
                sys.stdout.flush()

                # 重载 PostgreSQL 配置
                subprocess.run(
                    ["sudo", "systemctl", "reload", "postgresql"],
                    capture_output=True,
                    timeout=10,
                )

        except PermissionError:
            # 尝试用 sudo 写入
            pass
        except Exception as e:
            logger.debug(f"配置 pg_hba.conf 失败 ({pg_hba_path}): {e}")


def show_manual_install_guide() -> None:
    """显示手动安装PostgreSQL的指引（跨平台）"""
    config = get_config()

    logger.warning("=" * 60)
    logger.warning("⚠️  PostgreSQL自动部署失败，需要手动安装")
    logger.warning("=" * 60)
    logger.warning("")

    if sys.platform == "win32":
        logger.warning("方式1: 使用 scoop（推荐，适合开发者）")
        logger.warning("   scoop install postgresql")
        logger.warning("")
        logger.warning("方式2: 使用 winget（Windows 自带）")
        logger.warning("   winget install -e --id PostgreSQL.PostgreSQL")
        logger.warning("")
        logger.warning("方式3: 手动下载安装")
        logger.warning("   下载地址: https://www.postgresql.org/download/windows/")
        logger.warning(f"   安装时设置密码: {config.password}")
        logger.warning(f"   端口设为: {config.port}")
        logger.warning("")
        logger.warning("安装后，运行以下命令初始化数据库:")
        logger.warning("   fly.exe init-db")
    else:
        logger.warning("方式1: 使用Docker（推荐）")
        logger.warning(f"   docker run -d \\")
        logger.warning(f"     --name peng-postgres \\")
        logger.warning(f"     -e POSTGRES_USER={config.user} \\")
        logger.warning(f"     -e POSTGRES_PASSWORD={config.password} \\")
        logger.warning(f"     -e POSTGRES_DB={config.database} \\")
        logger.warning(f"     -e PGDATA=/var/lib/postgresql/data \\")
        logger.warning(f"     -p {config.port}:5432 \\")
        logger.warning(f"     -v /mnt/wsl/work/db/postgresql:/var/lib/postgresql \\")
        logger.warning(f"     --restart unless-stopped \\")
        logger.warning(f"     postgres:14")
        logger.warning("")
        logger.warning("方式2: 系统包管理器安装")
        logger.warning("   Ubuntu/Debian:")
        logger.warning("     sudo apt-get install postgresql postgresql-contrib")
        logger.warning("   macOS:")
        logger.warning("     brew install postgresql && brew services start postgresql")
        logger.warning("")
        logger.warning("安装后，运行以下命令初始化数据库:")
        logger.warning("   python scripts/init_postgres_db.py")

    logger.warning("")
    logger.warning(f"配置信息:")
    logger.warning(f"   主机: {config.host}")
    logger.warning(f"   端口: {config.port}")
    logger.warning(f"   数据库: {config.database}")
    logger.warning(f"   用户: {config.user}")
    logger.warning("")
    logger.warning("=" * 60)


def init_database() -> bool:
    """
    初始化数据库表结构

    Returns:
        True表示初始化成功，False表示失败
    """
    try:
        from peng.db.connection import get_connection

        logger.info("🔧 正在初始化数据库表...")

        # 检查表是否已存在（使用默认元组游标，避免 RealDictCursor 的 KeyError 问题）
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=None)
            cursor.execute("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                ORDER BY table_name;
            """)
            tables = cursor.fetchall()

        if tables:
            table_names = [t[0] for t in tables]
            logger.info(f"✓ 数据库表已存在: {table_names}")
            return True

        # 运行初始化脚本
        project_root = Path(__file__).resolve().parent.parent.parent.parent
        init_script = project_root / "src" / "peng" / "db" / "init_postgres_db.py"

        if not init_script.exists():
            logger.error(f"❌ 初始化脚本不存在: {init_script}")
            return False

        result = subprocess.run(
            [sys.executable, str(init_script)],
            capture_output=True,
            timeout=300,  # 数据库初始化允许5分钟
            encoding="utf-8",
            errors="replace",
        )

        if result.returncode == 0:
            logger.info("✓ 数据库表初始化成功")
            return True
        else:
            logger.error(
                f"❌ 数据库初始化失败:\n{result.stderr or f'(exit code: {result.returncode})'}"
            )
            return False

    except UnicodeDecodeError as e:
        logger.error(f"❌ 初始化数据库时解码错误: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ 初始化数据库时出错: {type(e).__name__}: {e}")
        return False


def check_postgresql_ready() -> tuple[bool, str]:
    """检查PostgreSQL是否就绪（不自动部署，仅检测连接和表）
    
    Returns:
        (ok, error_msg): ok=True表示就绪，error_msg为空；
                         ok=False表示异常，error_msg包含具体异常信息
    """
    try:
        config = get_config()
    except Exception as e:
        return False, f"读取数据库配置失败: {type(e).__name__}: {e}"

    # 检查连接
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            database=config.database,
            connect_timeout=5,
        )
        conn.close()
    except psycopg2.OperationalError as e:
        return False, f"连接失败: {e}"
    except (UnicodeDecodeError, UnicodeEncodeError):
        # Windows编码问题，尝试socket检测
        if not _check_port_reachable(config.host, config.port):
            return False, f"端口 {config.port} 不可达"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    # 检查表是否存在
    try:
        if not check_tables_exist():
            return False, "数据库表不存在"
    except Exception as e:
        return False, f"检查表失败: {type(e).__name__}: {e}"

    return True, ""


def ensure_postgresql(component_name: Literal["fly", "task", "msg"]) -> bool:
    """
    确保PostgreSQL可用（分层次的自动部署策略）

    Args:
        component_name: 组件名称（fly/task/msg），用于日志记录

    Returns:
        True表示PostgreSQL可用，False表示失败
    """
    global _auto_deploy_done

    # 避免重复执行
    if _auto_deploy_done:
        logger.debug(f"PostgreSQL自动部署已在其他组件中执行，跳过")
        return True

    logger.info(f"📋 [{component_name}] 检查PostgreSQL可用性...")
    cleanup_postgresql_idle_connections()

    # 1. 检查PostgreSQL是否已可用（最高优先级）
    if check_postgresql_available():
        logger.info(f"✓ [{component_name}] PostgreSQL已就绪")

        # 检查表是否存在，如果不存在则初始化
        if not check_tables_exist():
            logger.warning(f"⚠️  [{component_name}] PostgreSQL连接正常但表不存在，初始化表...")
            if not init_database():
                # Windows: psycopg2 编码异常时，PG 端口可达即可认为可用
                config = get_config()
                if _check_port_reachable(config.host, config.port):
                    logger.warning(
                        f"⚠️  [{component_name}] 初始化失败但 PG 端口可达，可能是编码问题，继续启动"
                    )
                else:
                    logger.error(f"❌ [{component_name}] 数据库初始化失败")
                    return False

        return True

    # 2. Windows 平台：使用原生 PostgreSQL（scoop/winget + pg_ctl）
    if sys.platform == "win32":
        logger.warning(f"⚠️  [{component_name}] PostgreSQL不可用，尝试Windows原生部署...")
        if not deploy_with_windows_native():
            logger.error(f"❌ [{component_name}] Windows原生PostgreSQL部署失败")
            show_manual_install_guide()
            return False
    else:
        # Linux/macOS：优先 Docker，备选系统包管理器
        if check_docker_available():
            # 3. Docker 可用：使用 Docker 自动部署
            logger.warning(f"⚠️  [{component_name}] PostgreSQL不可用，尝试使用Docker自动部署...")

            if not deploy_with_docker():
                logger.error(f"❌ [{component_name}] Docker部署失败，尝试系统包管理器安装...")
                if not deploy_with_linux_native():
                    logger.error(f"❌ [{component_name}] PostgreSQL自动部署失败")
                    show_manual_install_guide()
                    return False
        else:
            # Docker 不可用：使用系统包管理器安装
            logger.warning(f"⚠️  [{component_name}] Docker不可用，尝试系统包管理器安装PostgreSQL...")
            if not deploy_with_linux_native():
                logger.error(f"❌ [{component_name}] PostgreSQL自动部署失败")
                show_manual_install_guide()
                return False

    # 4. 初始化数据库（先检查表是否已存在，避免重复初始化和编码问题）
    if check_tables_exist():
        logger.info(f"✅ [{component_name}] 数据库表已存在，跳过初始化")
    elif not init_database():
        # Windows: psycopg2 编码异常时，PG 端口可达即可认为可用
        config = get_config()
        if _check_port_reachable(config.host, config.port):
            logger.warning(
                f"⚠️  [{component_name}] 初始化失败但 PG 端口可达，可能是编码问题，继续启动"
            )
        else:
            logger.error(f"❌ [{component_name}] 数据库初始化失败")
            return False

    # 标记已完成
    _auto_deploy_done = True

    logger.info(f"✅ [{component_name}] PostgreSQL已自动部署并初始化完成")
    return True


def ensure_postgresql_fly() -> bool:
    """fly组件的PostgreSQL确保函数"""
    return ensure_postgresql("fly")


def ensure_postgresql_service() -> bool:
    """
    fly service 专用的 PostgreSQL 检查函数。
    
    ⚠️ 只检查 PostgreSQL 是否可用，不自动部署/启动！
    PostgreSQL 只能由 fly install 安装、fly uninstall 卸载。
    fly service 不应该控制 PostgreSQL 的生命周期。
    
    如果 PG 不可用：
    - 尝试初始化数据库表（如果表不存在）
    - 但绝不自动安装或启动 PostgreSQL
    
    Returns:
        True 表示 PostgreSQL 可用，False 表示不可用
    """
    global _auto_deploy_done

    if _auto_deploy_done:
        logger.debug("PostgreSQL自动部署已在其他组件中执行，跳过")
        return True

    logger.info("📋 [service] 检查PostgreSQL可用性（仅检查，不自动部署）...")
    cleanup_postgresql_idle_connections()

    # 检查 PostgreSQL 是否可用
    if check_postgresql_available():
        logger.info("✓ [service] PostgreSQL已就绪")

        # 检查表是否存在，如果不存在则初始化
        if not check_tables_exist():
            logger.warning("⚠️  [service] PostgreSQL连接正常但表不存在，初始化表...")
            if not init_database():
                config = get_config()
                if _check_port_reachable(config.host, config.port):
                    logger.warning(
                        "⚠️  [service] 初始化失败但 PG 端口可达，可能是编码问题，继续启动"
                    )
                else:
                    logger.error("❌ [service] 数据库初始化失败")
                    return False

        _auto_deploy_done = True
        return True

    # PG 不可用 —— 不自动部署，只报错提示用户运行 fly install
    logger.error(
        "❌ [service] PostgreSQL不可用！"
        "请先运行 'fly install' 安装并启动 PostgreSQL。"
        "fly service 不会自动启动 PostgreSQL。"
    )
    return False


def ensure_postgresql_task() -> bool:
    """task组件的PostgreSQL确保函数"""
    return ensure_postgresql("task")


def ensure_postgresql_msg() -> bool:
    """msg组件的PostgreSQL确保函数"""
    return ensure_postgresql("msg")

# Windows平台兼容性分析报告

> 事务 #80 | 全面分析项目兼容性，确保前后端可以在Windows平台运行

## 总体评估

项目已有较完善的Windows兼容基础（`src/peng/common/platform.py` 提供了跨平台封装），但存在若干关键阻塞问题。

---

## 一、Windows入口点机制（重要发现）

### 1.1 `uv sync` 生成的 `.exe` 入口

`pyproject.toml` 定义了三个入口点：

```toml
[project.scripts]
fly = "peng.ui.fly:run_fly"
msg = "peng.cli.msg:main"
task = "peng.cli.task:main"
```

在Windows上执行 `uv sync` 后，会在 `.venv/Scripts/` 目录生成：
- `fly.exe` → 直接调用 `peng.ui.fly:run_fly()`
- `msg.exe` → 直接调用 `peng.cli.msg:main()`
- `task.exe` → 直接调用 `peng.cli.task:main()`

**这是Windows平台的推荐使用方式**，替代 `bin/fly`、`bin/task`、`bin/msg` 这些Shell脚本。

### 1.2 入口点 vs bin脚本的差异

| 特性 | bin/脚本 | .venv/Scripts/*.exe |
|------|---------|-------------------|
| Windows原生支持 | ❌ 需要 `#!/usr/bin/env python3`，Windows不支持Shebang | ✅ 直接运行 |
| 虚拟环境切换 | `bin/task`、`bin/msg` 有自动切换逻辑 | ✅ uv自动安装到正确环境 |
| `ensure_uv_sync()` | `bin/task`、`bin/msg` 调用 | ❌ `task/main()`、`msg/main()` 中未调用 |
| PostgreSQL自动部署 | `bin/task`、`bin/msg` 调用 `ensure_postgresql_*()` | ✅ `task/main()`、`msg/main()` 中有调用 |
| 日志初始化 | `bin/task`、`bin/msg` 调用 `setup_logging_from_config()` | ✅ `_task_impl/main()`、`_msg_impl/main()` 中有调用 |
| `sys.path` 设置 | `bin/fly` 手动设置 | ✅ uv自动处理 |

### 1.3 各入口函数的初始化完整性

#### `fly.exe` → `peng.ui.fly:run_fly()`
- ✅ `ensure_uv_sync()` — 第512行，仅chat模式下调用
- ✅ `ensure_postgresql_fly()` — 第542-544行
- ✅ `setup_logging_from_config()` — 各命令分支中调用
- ⚠️ 不经过 `bin/fly` 的 `sys.path.insert` — 但uv会处理

#### `task.exe` → `peng.cli.task:main()`
需查看 `src/peng/cli/task.py` 和 `_task_impl.py`：
- ✅ 日志初始化 — `_task_impl.py` main()函数中
- ✅ PostgreSQL部署 — `_task_impl.py` main()函数中
- ❓ `ensure_uv_sync` — 需确认是否在main()中调用

#### `msg.exe` → `peng.cli.msg:main()`
- ✅ 日志初始化 — `_msg_impl.py` main()函数中
- ✅ PostgreSQL部署 — `_msg_impl.py` main()函数中
- ❓ `ensure_uv_sync` — 需确认是否在main()中调用

### 1.4 结论

**Windows平台应使用 `.venv/Scripts/fly.exe`、`.venv/Scripts/task.exe`、`.venv/Scripts/msg.exe`**，这些是uv自动生成的正确入口。但需确保入口函数内部包含完整的初始化逻辑（特别是 `ensure_uv_sync`）。

---

## 二、已完成的Windows兼容工作 ✅

### 2.1 平台检测层 (`src/peng/common/platform.py`)
- ✅ `IS_WINDOWS` / `IS_POSIX` 平台常量
- ✅ `is_wsl()` WSL环境检测
- ✅ `get_venv_python()` 跨平台虚拟环境路径（Scripts/python.exe vs bin/python）
- ✅ `get_venv_bin_dir()` 跨平台bin目录（Scripts vs bin）
- ✅ `get_termination_signals()` 跨平台信号
- ✅ `get_kill_signal()` Windows使用SIGTERM替代SIGKILL
- ✅ `kill_process()` 跨平台进程终止（Windows使用taskkill）
- ✅ `kill_port_process()` 跨平台端口占用清理（Windows使用netstat+taskkill）
- ✅ `process_exists()` 跨平台进程存在检查（Windows使用tasklist）
- ✅ `get_default_shell()` 跨平台默认shell
- ✅ `setup_signal_handlers()` / `cleanup_signal_handlers()` Windows跳过asyncio信号处理
- ✅ `can_use_signal_handler()` Windows返回False
- ✅ `ensure_uv_sync()` 静默运行uv sync

### 2.2 入口脚本 (`bin/task`, `bin/msg`)
- ✅ 跨平台虚拟环境路径检测（`_VENV_PYTHON`）
- ✅ 自动切换到虚拟环境Python
- ⚠️ `bin/fly` 缺少虚拟环境切换逻辑

### 2.3 Bash工具 (`src/peng/tools/bash.py`)
- ✅ Windows/Unix双平台正则模式（del/rmdir vs rm等）
- ✅ 平台检测 `IS_WINDOWS`
- ✅ 平台相关的命令提示

### 2.4 服务管理 (`src/peng/ui/service.py`)
- ✅ `_get_port_from_cmdline()` Windows使用wmic，Unix使用ps
- ✅ `_get_all_service_pids()` Windows跳过pgrep
- ✅ 信号文件机制停止服务（跨平台）

### 2.5 其他兼容组件
- ✅ `uvloop` 条件依赖已正确排除Windows
- ✅ `colorama` 在requirements.txt中标记为 `sys_platform == 'win32'`
- ✅ `prompt_toolkit`、`rich`、`textual` 均有Windows原生支持

---

## 三、关键阻塞问题 ❌

### 3.1 WebSocket终端 — 完全依赖Unix API（P0，最严重）
**文件**: `src/peng/webui/websocket.py` 第60-110行

```python
# PtyProcess 类使用了以下 Unix-only API：
os.fork()           # Windows不支持
os.setsid()         # Windows不支持
fcntl.ioctl()       # Windows不支持
termios.TIOCSWINSZ  # Windows不支持
os.waitpid()        # Windows行为不同
TIOCSCTTY = 0x540E  # Linux特定的ioctl
```

**影响**: WebUI的终端功能在Windows上完全不可用，会导致导入时 `fcntl`/`termios` 模块找不到而崩溃。

**修复建议**: 
- 短期：条件导入，Windows下禁用终端功能（优雅降级）
- 长期：为Windows平台使用 `conpty`（Windows Pseudo Console）或 `winpty` 库替代

### 3.2 进程信号 — SIGKILL/SIGTERM直接使用（P0）
多处直接使用Unix信号，未走 `platform.py` 封装：

| 文件 | 行号 | 问题代码 |
|------|------|---------|
| `ui/service.py` | 622 | `os.kill(pid, sig.SIGTERM)` |
| `ui/service.py` | 637 | `os.kill(pid, sig.SIGKILL)` |
| `ui/service.py` | 713 | `os.kill(pid, sig.SIGTERM)` |
| `ui/service.py` | 725 | `os.kill(pid, sig.SIGKILL)` |
| `ui/service.py` | 833 | `os.kill(current_pid, signal.SIGKILL)` |
| `common/pid_registry.py` | 271 | `os.kill(pid, signal.SIGTERM)` |
| `common/pid_registry.py` | 294 | `os.kill(pid, signal.SIGKILL)` |
| `webui/websocket.py` | 153 | `os.kill(self.pid, signal.SIGKILL)` |
| `webui/websocket.py` | 155 | `os.kill(self.pid, signal.SIGTERM)` |
| `service/launcher.py` | 247-248 | `loop.add_signal_handler(signal.SIGTERM)` |

**影响**: Windows上 `signal.SIGKILL` 不存在，会引发 `AttributeError`。

**修复建议**: 使用 `peng.common.platform.kill_process()` 和 `setup_signal_handlers()` 替代。

### 3.3 进程组操作（P1）
**文件**: `common/platform.py` 第273行、`service/systemd_manager.py` 第122行、`ui/service.py` 第890行

- `os.killpg()` 在Windows上完全不可用
- `start_new_session=True` 在Windows上需用 `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP`

### 3.4 `/proc` 文件系统依赖（P1）

| 文件 | 行号 | 用途 |
|------|------|------|
| `common/platform.py` | 323-335 | 读取 `/proc/{pid}/task/{tid}/children` 获取子进程 |
| `common/pid_registry.py` | 158-173 | 读取 `/proc/{pid}/stat` 检查僵尸进程 |
| `common/platform.py` | 36-43 | `is_wsl()` 读取 `/proc/version` |

**影响**: Windows原生环境无 `/proc` 文件系统。

**修复建议**: Windows使用 `psutil` 库或 `wmic`/`tasklist` 命令替代。

### 3.5 硬编码Unix路径（P1）

| 路径 | 位置 | Windows等价物 |
|------|------|-------------|
| `/tmp/test-output.log` | `bin/test` 第113行 | `%TEMP%\test-output.log` |
| `/tmp/test.pid` | `bin/test` 第114行 | `%TEMP%\test.pid` |
| `/tmp/peng_input_history.json` | `config/loader.py` 第196行 | `%TEMP%\peng_input_history.json` |
| `/mnt/wsl/work/db/postgresql` | `config/peng.json` 第34行 | 环境变量动态解析 |
| `/mnt/wsl/work/db/backups` | `config/peng.json` 第11行 | 环境变量动态解析 |
| `/var/log` | `common/paths.py` 第136行 | Windows Event Log 或 `%APPDATA%` |

**修复建议**: 使用 `tempfile.gettempdir()` 和 `pathlib.Path` + 环境变量动态解析。

### 3.6 systemd依赖 — Windows替代方案（P2）
**文件**: `service/systemd_manager.py`（整个文件）

#### 问题分析
当前 `systemd_manager.py` 依赖 Linux systemd 的核心功能：
- `systemctl --user start/stop/restart/status` 命令
- systemd service 文件格式（`.service`）
- 用户级 systemd 运行时目录（`~/.config/systemd/user/`）
- ExecStart/ExecStop 路径（硬编码 Unix 路径）
- `systemctl --user enable` 实现开机自启

#### 权限需求分析

**核心原则：全过程避免管理员权限，普通用户即可完成全部操作。**

| 操作 | 是否需要管理员权限 | 说明 |
|------|:-----------------:|------|
| **Windows Service 注册/注销** | ✅ 需要 | ❌ 不采用 |
| **Windows Service 启动/停止** | ✅ 需要 | ❌ 不采用 |
| **nssm install/remove** | ✅ 需要 | ❌ 不采用 |
| **pywin32 注册 Service** | ✅ 需要 | ❌ 不采用 |
| **chocolatey install** | ✅ 需要 | ❌ 不采用 |
| **scoop install** | ❌ 不需要 | ✅ 采用 |
| **winget install** | ❌ 不需要 | ✅ 采用 |
| **pg_ctl start** (用户目录) | ❌ 不需要 | ✅ 采用 |
| **fly.exe service** (后台进程) | ❌ 不需要 | ✅ 采用 |
| **Startup 快捷方式** | ❌ 不需要 | ✅ 采用 |
| **修改用户 PATH** | ❌ 不需要 | ✅ 采用（写入 `HKCU\Environment`，用户级） |

#### 推荐方案：后台进程 + Startup 快捷方式（无需管理员权限）

完全不需要管理员权限，通过用户级开机自启实现：

```python
# src/peng/service/windows_startup.py

import os
import sys
from pathlib import Path


def _get_startup_dir() -> Path:
    """获取 Windows Startup 目录"""
    return Path(os.environ.get(
        "APPDATA",
        Path.home() / "AppData" / "Roaming"
    )) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _get_fly_exe_path() -> Path:
    """获取 fly.exe 的绝对路径"""
    return Path(sys.executable).parent / "fly.exe"


def install_startup() -> tuple[bool, str]:
    """
    创建开机自启脚本（用户级，无需管理员权限）
    
    Returns:
        (成功标志, 消息)
    """
    startup_dir = _get_startup_dir()
    fly_exe = _get_fly_exe_path()
    
    if not fly_exe.exists():
        return False, f"fly.exe 不存在: {fly_exe}"
    
    # 创建 VBS 启动脚本（静默后台启动，不弹窗口）
    # VBS 的 WScript.Shell.Run 第2个参数 0 表示隐藏窗口
    vbs_content = f'''Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """{fly_exe}"" service", 0, False
'''
    vbs_path = startup_dir / "peng_fly_service.vbs"
    vbs_path.write_text(vbs_content, encoding="utf-8")
    
    return True, f"✅ 已创建开机自启脚本: {vbs_path}"


def uninstall_startup() -> tuple[bool, str]:
    """
    移除开机自启脚本（无需管理员权限）
    
    Returns:
        (成功标志, 消息)
    """
    vbs_path = _get_startup_dir() / "peng_fly_service.vbs"
    
    if not vbs_path.exists():
        return True, "ℹ️ 开机自启脚本不存在，无需移除"
    
    vbs_path.unlink()
    return True, "✅ 已移除开机自启脚本"


def is_startup_installed() -> bool:
    """检查开机自启是否已安装"""
    return (_get_startup_dir() / "peng_fly_service.vbs").exists()
```

#### PATH 环境变量自动配置（仅 Windows）

将 `.venv\Scripts` 加入 Windows 用户 PATH，这样可以直接在终端中调用 `fly`、`task`、`msg`：

```python
# src/peng/common/path_setup.py（仅 Windows 使用）

import os
import sys
from pathlib import Path


def get_venv_scripts_dir() -> Path:
    """获取虚拟环境的 Scripts 目录"""
    return Path(sys.executable).parent


def is_in_user_path(scripts_dir: Path) -> bool:
    """检查 scripts_dir 是否已在用户 PATH 中"""
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    normalized = str(scripts_dir.resolve()).rstrip(os.sep).lower()
    for d in path_dirs:
        if d.rstrip(os.sep).lower() == normalized:
            return True
    return False


def add_to_user_path(scripts_dir: Path | None = None) -> tuple[bool, str]:
    """
    将 .venv\Scripts 添加到 Windows 用户 PATH
    
    修改注册表 HKCU\Environment，无需管理员权限。
    （与系统 PATH HKLM\...\PATH 不同，用户 PATH 不需要管理员权限）
    
    Args:
        scripts_dir: 要添加的目录，默认为当前 .venv\Scripts
    
    Returns:
        (成功标志, 消息)
    """
    if sys.platform != "win32":
        return False, "仅支持 Windows 平台"
    
    if scripts_dir is None:
        scripts_dir = get_venv_scripts_dir()
    
    if not scripts_dir.exists():
        return False, f"❌ 目录不存在: {scripts_dir}"
    
    if is_in_user_path(scripts_dir):
        return True, f"ℹ️ {scripts_dir} 已在 PATH 中"
    
    try:
        import winreg
        
        # 读取当前用户 PATH
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Environment",
            0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        )
        
        try:
            current_path, _ = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current_path = ""
        
        # 追加新路径
        new_path = f"{current_path};{scripts_dir}" if current_path else str(scripts_dir)
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_path)
        winreg.CloseKey(key)
        
        # 通知其他窗口更新环境变量（广播 WM_SETTINGCHANGE）
        import ctypes
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF,  # HWND_BROADCAST
            0x001A,  # WM_SETTINGCHANGE
            0,
            "Environment",
            0x0002,  # SMTO_ABORTIFHUNG
            5000,    # 5秒超时
            None,
        )
        
        # 同时更新当前进程的 PATH
        os.environ["PATH"] = new_path
        
        return True, f"✅ 已将 {scripts_dir} 添加到用户 PATH\n   请重新打开终端窗口使 PATH 生效"
    
    except Exception as e:
        return False, f"❌ 添加 PATH 失败: {e}"
```

**Linux 平台说明**：Linux 不做 PATH 配置，用户通过 `bin/fly`、`bin/task`、`bin/msg` 执行命令，或手动 source 虚拟环境。

#### 适配策略

1. **`fly install` 命令适配**：
   - Windows: `install_startup()` 创建 Startup 快捷方式（无需管理员）
   - Linux: 继续使用 `systemctl --user`

2. **`fly uninstall` 命令适配**：
   - Windows: `uninstall_startup()` 删除快捷方式（无需管理员）
   - Linux: `systemctl --user disable` + `systemctl --user stop`

3. **`fly start` / `fly stop`**：
   - Windows: 直接启动/终止后台进程（无需管理员，使用 PID 文件）
   - Linux: `systemctl --user start/stop`

4. **`fly service` 后台模式**：
   - Windows: 后台进程 + PID 文件（无需管理员）
   - Linux: 同上

5. **PID 文件位置适配**：
   - Windows: `%TEMP%\peng_fly.pid`
   - Linux: `/run/user/1000/peng_fly.pid`

6. **开机自启效果**：
   - Windows: 用户登录后自动运行 `fly.exe service`（通过 Startup VBS）
   - Linux: 用户登录后 systemd --user 自动启动（通过 `systemctl --user enable`）

---

### 3.7 PostgreSQL自动部署 — Windows适配方案（P1）
**文件**: `db/postgres_auto_deploy.py`

#### 问题分析

当前 `postgres_auto_deploy.py` 的部署优先级：
1. 检查 PostgreSQL 是否已运行 → 直接使用
2. 使用 Docker 部署 → `docker.from_env()` + `containers.run()`
3. 提供手动安装指引 → 目前仅包含 Unix 命令

Windows 上的具体问题：

| 问题 | 位置 | 影响 |
|------|------|------|
| 数据路径硬编码 `/mnt/wsl/work/db/postgresql` | 第54行 | Windows 上路径不存在 |
| Docker 端口映射方向反了 | 第343行 `ports={f"{config.port}": "5432"}` | 应为 `{"5432/tcp": config.port}` |
| 手动安装指引仅含 Unix 命令 | 第546-588行 | Windows 用户无从下手 |
| `_configure_wal_in_container` 使用 Unix 命令 | 第400+行 | `pg_ctl`、`psql` 路径 |
| `psycopg2-binary` 需要编译环境 | `pyproject.toml` 第15行 | Windows 可能缺少编译工具 |

#### Windows 适配详细方案

**核心理念：Windows 上使用 scoop/winget 等命令行工具安装原生 PostgreSQL，不使用 Docker Desktop**

**1. 数据路径动态解析（关键修复）**

修改 `_get_postgresql_data_dir()` 函数：

```python
def _get_postgresql_data_dir() -> Path:
    """跨平台获取 PostgreSQL 数据目录"""
    if sys.platform == "win32":
        # Windows: 使用 %LOCALAPPDATA%\peng\db\postgresql
        # LOCALAPPDATA 适合存放数据库文件（比 APPDATA/ROAMING 更合适，避免网络同步）
        default = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "peng" / "db" / "postgresql"
    else:
        # Linux/macOS: 使用配置值或默认路径
        default = Path("/mnt/wsl/work/db/postgresql")
    
    fallback = Path.home() / ".db" / "postgresql"
    
    # 尝试从配置读取（覆盖默认值）
    try:
        from peng.config import get_path
        config_path = get_path("postgresql_data_dir", str(default))
        data_root = Path(config_path).expanduser()
    except Exception:
        data_root = default
    
    # 尝试创建目录，失败则使用备用路径
    try:
        data_root.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError):
        data_root = fallback
        data_root.mkdir(parents=True, exist_ok=True)
    
    return data_root
```

**2. Windows 原生 PostgreSQL 自动部署（核心新增）**

新增 `deploy_with_windows_native()` 函数，使用 scoop/winget 安装 PostgreSQL：

```python
def _find_psql_on_windows() -> Path | None:
    """在 Windows 上查找已安装的 psql 可执行文件"""
    # 常见安装位置（按优先级）
    search_paths = [
        # scoop 安装路径
        Path.home() / "scoop" / "apps" / "postgresql" / "current" / "bin",
        # winget/MSI 默认安装路径
        Path("C:/Program Files/PostgreSQL/14/bin"),
        Path("C:/Program Files/PostgreSQL/15/bin"),
        Path("C:/Program Files/PostgreSQL/16/bin"),
        Path("C:/Program Files/PostgreSQL/17/bin"),
    ]
    
    for path in search_paths:
        psql = path / "psql.exe"
        if psql.exists():
            return psql
    
    # 尝试从 PATH 环境变量查找
    import shutil
    psql_in_path = shutil.which("psql")
    if psql_in_path:
        return Path(psql_in_path)
    
    return None


def _find_pg_ctl_on_windows() -> Path | None:
    """在 Windows 上查找 pg_ctl 可执行文件"""
    psql = _find_psql_on_windows()
    if psql:
        return psql.parent / "pg_ctl.exe"
    return None


def _get_pg_data_dir_on_windows() -> Path:
    """获取 Windows 上 PostgreSQL 的数据目录"""
    data_dir = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "peng" / "db" / "postgresql" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _init_pg_cluster_on_windows(pg_ctl: Path, data_dir: Path, config) -> bool:
    """初始化 PostgreSQL 数据集群（Windows）"""
    result = subprocess.run(
        [str(pg_ctl), "initdb", "-D", str(data_dir)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        logger.error(f"❌ initdb 失败: {result.stderr}")
        return False
    
    # 修改 pg_hba.conf 允许本地连接（trust/md5）
    pg_hba = data_dir / "pg_hba.conf"
    pg_hba.write_text(
        "# TYPE  DATABASE  USER  ADDRESS      METHOD\n"
        "local   all       all                 trust\n"
        "host    all       all   127.0.0.1/32  md5\n"
        "host    all       all   ::1/128       md5\n"
    )
    
    # 修改 postgresql.conf 端口
    postgresql_conf = data_dir / "postgresql.conf"
    conf_content = postgresql_conf.read_text()
    conf_content = conf_content.replace("#port = 5432", f"port = {config.port}")
    postgresql_conf.write_text(conf_content)
    
    return True


def deploy_with_windows_native() -> bool:
    """Windows 平台自动部署 PostgreSQL（使用 scoop/winget）"""
    config = get_config()
    
    # 第1步：检查 PostgreSQL 是否已安装
    pg_ctl = _find_pg_ctl_on_windows()
    
    if not pg_ctl:
        # 第2步：尝试自动安装
        logger.info("📦 PostgreSQL 未安装，尝试自动安装...")
        
        installed = False
        
        # 优先级1：scoop（开发者的首选包管理器）
        if not installed:
            scoop = shutil.which("scoop")
            if scoop:
                logger.info("🚀 使用 scoop 安装 PostgreSQL...")
                result = subprocess.run(
                    ["scoop", "install", "postgresql"],
                    capture_output=True, text=True, timeout=300,
                )
                if result.returncode == 0:
                    logger.info("✅ scoop 安装 PostgreSQL 成功")
                    installed = True
                else:
                    logger.warning(f"⚠️ scoop 安装失败: {result.stderr}")
        
        # 优先级2：winget（Windows 自带包管理器）
        if not installed:
            winget = shutil.which("winget")
            if winget:
                logger.info("🚀 使用 winget 安装 PostgreSQL...")
                result = subprocess.run(
                    ["winget", "install", "-e", "--id", "PostgreSQL.PostgreSQL", "--accept-package-agreements", "--accept-source-agreements"],
                    capture_output=True, text=True, timeout=600,
                )
                if result.returncode == 0:
                    logger.info("✅ winget 安装 PostgreSQL 成功")
                    installed = True
                else:
                    logger.warning(f"⚠️ winget 安装失败: {result.stderr}")
        
        # 注意：不使用 chocolatey，因为 choco install 需要管理员权限
        
        if not installed:
            logger.error("❌ 自动安装失败，请手动安装 PostgreSQL")
            show_manual_install_guide()
            return False
        
        # 重新查找安装后的路径
        pg_ctl = _find_pg_ctl_on_windows()
        if not pg_ctl:
            logger.error("❌ 安装成功但找不到 pg_ctl，可能需要重启终端或刷新 PATH")
            return False
    
    # 第3步：检查 PostgreSQL 服务是否运行
    data_dir = _get_pg_data_dir_on_windows()
    
    # 尝试连接
    if check_postgresql_available():
        logger.info("✅ PostgreSQL 已运行")
        return True
    
    # 第4步：初始化数据集群（首次安装时需要）
    if not (data_dir / "PG_VERSION").exists():
        logger.info("🔧 初始化 PostgreSQL 数据集群...")
        if not _init_pg_cluster_on_windows(pg_ctl, data_dir, config):
            return False
    
    # 第5步：启动 PostgreSQL
    logger.info("🚀 启动 PostgreSQL...")
    result = subprocess.run(
        [str(pg_ctl), "start", "-D", str(data_dir), "-l", str(data_dir / "postgresql.log")],
        capture_output=True, text=True, timeout=30,
    )
    
    if result.returncode != 0:
        logger.error(f"❌ PostgreSQL 启动失败: {result.stderr}")
        return False
    
    # 第6步：创建用户和数据库
    psql = pg_ctl.parent / "psql.exe"
    # ... 使用 psql 创建用户和数据库
    
    logger.info("✅ PostgreSQL 启动成功")
    return True
```

**3. Windows 手动安装指引补充**

在 `show_manual_install_guide()` 中增加 Windows 部分：

```python
if sys.platform == "win32":
    logger.warning("方式1: 使用 scoop（推荐，适合开发者）")
    logger.warning("   scoop install postgresql")
    logger.warning("")
    logger.warning("方式2: 使用 winget（Windows 自带）")
    logger.warning("   winget install -e --id PostgreSQL.PostgreSQL")
    logger.warning("")
    logger.warning("方式3: 手动下载安装")
    logger.warning(f"   下载地址: https://www.postgresql.org/download/windows/")
    logger.warning(f"   安装时设置密码: {config.password}")
    logger.warning(f"   端口设为: {config.port}")
    logger.warning("")
    logger.warning("安装后，运行以下命令初始化数据库:")
    logger.warning("   fly.exe init-db")
else:
    # 保持现有 Unix 指引
    ...
```

**4. Docker 部署端口映射修复**（保留 Docker 方案用于 WSL/Linux）

```python
# 修复前（端口映射方向错误）：
ports={f"{config.port}": "5432"}

# 修复后（正确的 Docker SDK 端口映射格式）：
ports={"5432/tcp": config.port}
```

**5. psycopg2-binary 在 Windows 上的兼容性**

`psycopg2-binary` 在 Windows 上有预编译 wheel，通常可以直接安装。但如果有问题：
```toml
# pyproject.toml 备选方案
dependencies = [
    "psycopg2-binary>=2.9.0; sys_platform != 'win32'",
    "psycopg2>=2.9.0; sys_platform == 'win32'",
]
```

#### 部署优先级（跨平台）

```
Linux 平台（保持现有）:
1. 检查 PG 连接是否可用 → 直接使用
2. 使用 Docker 自动部署 → docker.from_env()
3. 提供手动安装指引

Windows 平台（新增）:
1. 检查 PG 连接是否可用 → 直接使用
2. 查找已安装的 PostgreSQL（scoop/winget/手动安装）
3. 未安装时自动通过 scoop → winget 安装
4. 初始化数据集群（initdb）
5. 启动 PostgreSQL 服务（pg_ctl start）
6. 创建用户和数据库
```

#### Windows PostgreSQL 安装路径对照表

| 安装方式 | pg_ctl 路径 |
|---------|------------|
| scoop | `~/scoop/apps/postgresql/current/bin/pg_ctl.exe` |
| winget/MSI | `C:\Program Files\PostgreSQL\16\bin\pg_ctl.exe` |

### 3.8 `os.kill(pid, 0)` 进程检查（P1）
**文件**: `ui/service.py:434`、`task/heartbeat_monitor.py:149`、`message/database.py:1963`

`os.kill(pid, 0)` 在Windows上行为与Unix不同：
- Unix: 进程不存在时引发 `ProcessLookupError`
- Windows: 可能引发 `PermissionError` 或 `OSError`，需调整异常处理

**修复建议**: 使用 `peng.common.platform.process_exists()` 替代。

---

## 四、中等优先级问题 ⚠️

### 4.1 `pexpect` 依赖
**文件**: `pyproject.toml` 第36行

`pexpect` 库在Windows上不支持（依赖Unix PTY）。虽然代码中似乎未直接使用，但作为依赖会导致安装警告。
建议：添加条件依赖 `pexpect>=4.9.0; sys_platform != 'win32'`，或改用 `wexpect`（Windows替代）。

### 4.2 Python版本严格锁定
**文件**: `pyproject.toml` 第6行：`requires-python = "==3.12.3"`

这严格锁定到特定Python版本，Windows用户可能安装了不同版本。
建议：改为 `requires-python = ">=3.12,<3.13"` 或更宽松的范围。

### 4.3 `pgrep` 命令依赖
**文件**: `ui/service.py` 第671行、第780行

当前代码已有 `FileNotFoundError` 处理，Windows会静默跳过，但进程查找功能降级。

---

## 五、低优先级问题 💡

### 5.1 `bin/fly` 入口脚本
缺少Windows的虚拟环境切换逻辑（`bin/task` 和 `bin/msg` 已有），但Windows应直接使用 `fly.exe`。

### 5.2 `signal.SIGBREAK`
Windows特有的 `SIGBREAK` 信号已在 `platform.py` 的 `get_termination_signals()` 中使用，但其他模块未使用它。

---

## 六、修复优先级排序

| 优先级 | 问题 | 影响范围 | 修复复杂度 | 修复方式 |
|--------|------|----------|-----------|---------|
| **P0** | WebSocket终端Unix API | WebUI崩溃 | 高 | 条件导入+优雅降级 |
| **P0** | SIGKILL/SIGTERM直接使用 | 服务管理崩溃 | 中 | 统一用platform封装 |
| **P1** | /proc文件系统依赖 | 进程管理失败 | 中 | psutil或wmic替代 |
| **P1** | 硬编码Unix路径 | 配置错误 | 低 | tempfile+环境变量 |
| **P1** | PostgreSQL自动部署 | 数据库不可用 | 中 | scoop/winget自动安装(无需管理员) |
| **P1** | os.kill(pid,0)检查 | 进程检查异常 | 低 | 用process_exists() |
| **P2** | systemd依赖 | 服务安装不可用 | 低 | Startup快捷方式(无需管理员) |
| **P2** | pexpect依赖 | 安装警告 | 低 | 条件依赖 |
| **P3** | Python版本锁定 | 安装困难 | 低 | 放宽版本范围 |
| **P3** | bin/fly入口脚本 | 启动问题 | 低 | Windows用fly.exe |

---

## 七、Windows平台使用指南

### 安装步骤
```powershell
# 1. 安装 uv（Python包管理器）
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. 克隆项目
git clone <repo-url>
cd peng

# 3. 安装依赖（uv自动创建.venv并生成.exe入口）
uv sync

# 4. 首次运行时自动部署 PostgreSQL（通过 scoop/winget）
#    也可提前手动安装：
#    scoop install postgresql
#    或 winget install -e --id PostgreSQL.PostgreSQL

# 5. 启动服务
.venv\Scripts\fly.exe service

# 6. CLI命令
.venv\Scripts\task.exe list
.venv\Scripts\msg.exe list
```

### 注意事项
- Windows平台**必须**使用 `.venv\Scripts\` 下的 `.exe` 文件，不能使用 `bin/` 目录下的Shell脚本
- PostgreSQL 通过 scoop/winget 安装原生版本，**不需要** Docker Desktop，**不需要**管理员权限
- 首次运行时程序会自动检测并安装 PostgreSQL（按 scoop → winget 优先级）
- WebUI终端功能在修复前不可用（需P0修复）

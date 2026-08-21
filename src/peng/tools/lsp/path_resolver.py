"""
LSP服务器路径解析器

不依赖PATH环境变量，从多个位置查找LSP服务器可执行文件
优先级：配置文件路径 > 项目虚拟环境 > 项目LSP目录 > 用户目录 > 系统路径
支持Windows和Linux/macOS平台。
"""

import logging
import os
import sys
from pathlib import Path
from typing import Optional, Dict, List
from enum import Enum

logger = logging.getLogger(__name__)

# 当前平台
_IS_WINDOWS = sys.platform == "win32"


class ServerType(str, Enum):
    """LSP服务器类型"""
    PYLSP = "pylsp"
    CLANGD = "clangd"
    RUST_ANALYZER = "rust-analyzer"
    GOPLS = "gopls"
    TYPESCRIPT_LANGUAGE_SERVER = "typescript-language-server"
    LUA_LANGUAGE_SERVER = "lua-language-server"
    ZLS = "zls"


# 服务器类型到配置属性的映射
SERVER_CONFIG_ATTRS: Dict[ServerType, str] = {
    ServerType.PYLSP: "lsp_pylsp_path_value",
    ServerType.CLANGD: "lsp_clangd_path_value",
    ServerType.RUST_ANALYZER: "lsp_rust_analyzer_path_value",
    ServerType.GOPLS: "lsp_gopls_path_value",
    ServerType.TYPESCRIPT_LANGUAGE_SERVER: "lsp_typescript_language_server_path_value",
    ServerType.LUA_LANGUAGE_SERVER: "lsp_lua_language_server_path_value",
    ServerType.ZLS: "lsp_zls_path_value",
}


class LspPathResolver:
    """
    LSP服务器路径解析器

    从多个位置查找LSP服务器可执行文件，不依赖PATH环境变量
    优先级：配置文件路径 > 项目虚拟环境 > 项目LSP目录 > 用户目录 > 系统路径
    """

    # LSP服务器可执行文件名映射
    SERVER_BINARIES: Dict[ServerType, str] = {
        ServerType.PYLSP: "pylsp",
        ServerType.CLANGD: "clangd",
        ServerType.RUST_ANALYZER: "rust-analyzer",
        ServerType.GOPLS: "gopls",
        ServerType.TYPESCRIPT_LANGUAGE_SERVER: "typescript-language-server",
        ServerType.LUA_LANGUAGE_SERVER: "lua-language-server",
        ServerType.ZLS: "zls",
    }

    # 版本化名称回退（某些系统安装的是 clangd-21 而非 clangd）
    SERVER_BINARY_FALLBACKS: Dict[ServerType, List[str]] = {
        ServerType.CLANGD: ["clangd-21", "clangd-20", "clangd-19", "clangd-18", "clangd-17"],
    }

    def __init__(self, project_root: Optional[Path] = None):
        """
        初始化路径解析器

        Args:
            project_root: 项目根目录（可选，用于查找.venv）
        """
        self._project_root = Path(project_root) if project_root else Path.cwd()
        self._cache: Dict[ServerType, Optional[Path]] = {}
        self._config = None

    def _load_config(self):
        """延迟加载配置"""
        if self._config is None:
            try:
                from peng.config import load_config
                self._config = load_config()
            except Exception as e:
                logger.warning(f"无法加载配置: {e}")
                self._config = None
        return self._config

    def _get_config_path(self, server_type: ServerType) -> Optional[Path]:
        """从配置获取LSP服务器路径"""
        config = self._load_config()
        if not config:
            return None

        attr_name = SERVER_CONFIG_ATTRS.get(server_type)
        if not attr_name:
            return None

        try:
            path = getattr(config.paths, attr_name, None)
            if path and path.exists():
                logger.debug(f"从配置获取 {server_type.value}: {path}")
                return path
        except Exception as e:
            logger.debug(f"获取配置路径失败: {e}")

        return None

    def get_search_paths(self, server_type: ServerType) -> List[Path]:
        """
        获取LSP服务器的搜索路径列表（按优先级排序）

        Args:
            server_type: 服务器类型

        Returns:
            搜索路径列表
        """
        binary_name = self.SERVER_BINARIES[server_type]
        # Windows上可执行文件需要.exe后缀
        if _IS_WINDOWS and not binary_name.endswith(".exe"):
            binary_name += ".exe"
        paths = []

        # 0. 配置文件中指定的路径（最高优先级）
        config_path = self._get_config_path(server_type)
        if config_path:
            paths.append(config_path)

        if _IS_WINDOWS:
            paths.extend(self._get_windows_search_paths(server_type, binary_name))
        else:
            paths.extend(self._get_unix_search_paths(server_type, binary_name))

        return paths

    def _get_windows_search_paths(self, server_type: ServerType, binary_name: str) -> List[Path]:
        """Windows平台的搜索路径"""
        paths = []
        home = Path.home()

        # 1. 项目虚拟环境（.venv/Scripts/）
        venv_path = self._project_root / ".venv" / "Scripts" / binary_name
        if venv_path.exists():
            paths.append(venv_path)

        # 2. 项目LSP目录
        config = self._load_config()
        if config:
            lsp_bin_dir = config.paths.lsp_bin_dir_path
        else:
            lsp_bin_dir = home / ".local" / "share" / "peng" / "lsp_servers" / "bin"
        server_path = lsp_bin_dir / binary_name
        if server_path.exists():
            paths.append(server_path)

        # 3. Scoop shims（Windows包管理器scoop）
        scoop_shims = home / "scoop" / "shims" / binary_name
        if scoop_shims.exists():
            paths.append(scoop_shims)

        # 4. Chocolatey
        choco_path = Path("C:/ProgramData/chocolatey/bin") / binary_name
        if choco_path.exists():
            paths.append(choco_path)

        # 5. 用户目录下的bin
        user_bin = home / "bin" / binary_name
        if user_bin.exists():
            paths.append(user_bin)

        # 6. Python Scripts目录（pip安装的工具）
        python_scripts = [
            Path("C:/Python3/Scripts") / binary_name,
            home / "AppData" / "Local" / "Programs" / "Python" / "Python312" / "Scripts" / binary_name,
            home / "AppData" / "Local" / "Programs" / "Python" / "Python311" / "Scripts" / binary_name,
        ]
        for p in python_scripts:
            if p.exists():
                paths.append(p)

        # 7. 系统PATH中的常见位置
        system_dirs = [
            Path("C:/Windows/System32"),
            Path("C:/tools") / binary_name,
        ]
        for d in system_dirs:
            p = d / binary_name if d.is_dir() else d
            if p.exists():
                paths.append(p)

        # 8. 使用shutil.which从PATH查找（最后的回退）
        import shutil
        which_result = shutil.which(self.SERVER_BINARIES[server_type])
        if which_result:
            p = Path(which_result)
            if p not in paths:
                paths.append(p)

        return paths

    def _get_unix_search_paths(self, server_type: ServerType, binary_name: str) -> List[Path]:
        """Linux/macOS平台的搜索路径"""
        paths = []

        # 0. 对于clangd，优先使用系统安装版本（更稳定、与compile_commands.json兼容更好）
        if server_type == ServerType.CLANGD:
            for sys_path in [Path("/usr/local/bin") / binary_name, Path("/usr/bin") / binary_name]:
                if sys_path.exists():
                    paths.append(sys_path)
                    return paths  # 找到系统clangd直接返回

        # 1. 项目虚拟环境（.venv/bin/）
        venv_path = self._project_root / ".venv" / "bin" / binary_name
        if venv_path.exists():
            paths.append(venv_path)

        # 2. 项目LSP目录（从配置获取，默认 ~/.local/share/peng/lsp_servers/）
        config = self._load_config()
        if config:
            lsp_bin_dir = config.paths.lsp_bin_dir_path
        else:
            lsp_bin_dir = Path.home() / ".local" / "share" / "peng" / "lsp_servers" / "bin"
        server_path = lsp_bin_dir / binary_name
        if server_path.exists():
            paths.append(server_path)

        # 3. 用户目录（~/.local/bin/）
        user_bin = Path.home() / ".local" / "bin" / binary_name
        if user_bin.exists():
            paths.append(user_bin)

        # 4. Go特定路径（~/go/bin/ 或 ~/.local/go/bin/）
        if server_type == ServerType.GOPLS:
            go_paths = [
                Path.home() / "go" / "bin" / binary_name,  # Go默认安装路径
                Path.home() / ".local" / "go" / "bin" / binary_name,
            ]
            for go_bin in go_paths:
                if go_bin.exists():
                    paths.append(go_bin)

        # 5. npm全局路径（多种可能的位置）
        if server_type in [ServerType.TYPESCRIPT_LANGUAGE_SERVER, ServerType.LUA_LANGUAGE_SERVER]:
            npm_paths = [
                Path.home() / ".npm-global" / "bin" / binary_name,
                Path("/mnt/wsl/work/.npm-global/bin") / binary_name,  # WSL特定
                Path.home() / ".local" / "share" / "npm-global" / "bin" / binary_name,
                Path("/usr/local/share/npm-global/bin") / binary_name,
            ]
            for npm_bin in npm_paths:
                if npm_bin.exists():
                    paths.append(npm_bin)

        # 6. 系统标准位置（硬编码）
        system_paths = [
            Path("/usr/local/bin") / binary_name,
            Path("/usr/bin") / binary_name,
            Path("/snap/bin") / binary_name,
            Path("/opt/homebrew/bin") / binary_name,  # macOS ARM
            Path("/usr/local/homebrew/bin") / binary_name,  # macOS Intel
        ]
        for path in system_paths:
            if path.exists():
                paths.append(path)

        # 7. Rust工具链路径
        if server_type == ServerType.RUST_ANALYZER:
            rust_bin = Path.home() / ".cargo" / "bin" / binary_name
            if rust_bin.exists():
                paths.append(rust_bin)

        # 8. Zig路径
        if server_type == ServerType.ZLS:
            zig_bin = Path("/opt/zig") / binary_name
            if zig_bin.exists():
                paths.append(zig_bin)

        return paths

    def find_server(self, server_type: ServerType) -> Optional[Path]:
        """
        查找LSP服务器可执行文件

        Args:
            server_type: 服务器类型

        Returns:
            服务器可执行文件的绝对路径，如果未找到则返回None
        """
        # 检查缓存
        if server_type in self._cache:
            return self._cache[server_type]

        # 按优先级搜索主名称
        search_paths = self.get_search_paths(server_type)
        for path in search_paths:
            if path.exists() and path.is_file() and is_executable(path):
                logger.debug(f"找到 {server_type.value}: {path}")
                self._cache[server_type] = path
                return path

        # 回退：尝试版本化名称（如 clangd-21, clangd-20 等）
        fallbacks = self.SERVER_BINARY_FALLBACKS.get(server_type, [])
        for fallback_name in fallbacks:
            for base_dir in [Path("/usr/bin"), Path("/usr/local/bin"), Path.home() / ".local" / "bin"]:
                fallback_path = base_dir / fallback_name
                if fallback_path.exists() and fallback_path.is_file() and is_executable(fallback_path):
                    logger.info(f"找到版本化 {server_type.value}: {fallback_path}")
                    self._cache[server_type] = fallback_path
                    return fallback_path

        logger.warning(f"未找到 {server_type.value}")
        self._cache[server_type] = None
        return None

    def get_server_command(
        self,
        server_type: ServerType,
        additional_args: Optional[List[str]] = None
    ) -> Optional[List[str]]:
        """
        获取启动LSP服务器的命令

        Args:
            server_type: 服务器类型
            additional_args: 额外的命令行参数

        Returns:
            命令列表，如果未找到服务器则返回None
        """
        server_path = self.find_server(server_type)

        if server_path is None:
            return None

        command = [str(server_path)]
        if additional_args:
            command.extend(additional_args)

        return command

    def clear_cache(self):
        """清除缓存（用于重新检测服务器）"""
        self._cache.clear()


def is_executable(path: Path) -> bool:
    """
    检查文件是否可执行

    Args:
        path: 文件路径

    Returns:
        是否可执行
    """
    if not path.exists() or not path.is_file():
        return False

    # Windows: 只有带已知可执行后缀的文件才算可执行（无后缀的不是Windows可执行文件）
    if _IS_WINDOWS:
        return path.suffix.lower() in (".exe", ".bat", ".cmd", ".ps1", ".com")

    # Unix系统：检查可执行权限
    try:
        return path.stat().st_mode & 0o111 != 0
    except Exception:
        return False


# 全局单例（使用默认项目根目录）
_global_resolver: Optional[LspPathResolver] = None


def get_global_resolver(project_root: Optional[Path] = None) -> LspPathResolver:
    """
    获取全局LSP路径解析器单例

    Args:
        project_root: 项目根目录（首次调用时设置）

    Returns:
        LSP路径解析器实例
    """
    global _global_resolver

    if _global_resolver is None or project_root is not None:
        _global_resolver = LspPathResolver(project_root)

    return _global_resolver
"""
LSP依赖项管理器

管理LSP语言服务器的安装和状态检测
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass, asdict
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, List, Callable, Awaitable
from urllib.parse import urlparse

from peng.config import get_project_root
project_root = get_project_root()

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

from peng.config import get_path

try:
    from peng.tools.lsp.path_resolver import LspPathResolver, ServerType
    HAS_PATH_RESOLVER = True
except ImportError:
    HAS_PATH_RESOLVER = False

logger = logging.getLogger(__name__)


class InstallStatus(str, Enum):
    """安装状态枚举"""
    NOT_CHECKED = "not_checked"  # 未检查
    INSTALLED = "installed"  # 已安装
    NOT_INSTALLED = "not_installed"  # 未安装
    INSTALLING = "installing"  # 安装中
    INSTALL_FAILED = "install_failed"  # 安装失败


@dataclass
class LspDependency:
    """LSP依赖项配置"""
    name: str  # 依赖项名称（用于日志和UI显示）
    server_name: str  # LSP服务器名称
    languages: List[str]  # 支持的语言列表
    install_commands: Dict[str, List[str]]  # 各平台的安装命令 {platform: [command]}
    verify_command: List[str]  # 验证是否已安装的命令
    manual_install_hint: Optional[str] = None  # 手动安装提示（如果自动安装失败）
    status: InstallStatus = InstallStatus.NOT_CHECKED  # 当前状态
    error_message: Optional[str] = None  # 错误信息（如果安装失败）
    toast_shown: bool = False  # 是否已经发送过toast（避免重复提醒）
    install_method: str = "command"  # 安装方式: "command" 或 "binary"


class LspDependencyManager:
    """
    LSP依赖项管理器

    负责管理LSP语言服务器的检测和安装

    安装策略:
    - 优先使用系统已安装的LSP服务器
    - 如果系统未安装，则安装到配置文件指定的目录（默认 ~/.local/share/peng/lsp_servers）
    """

    # 预编译二进制下载URL模板
    BINARY_DOWNLOAD_URLS = {
        "clangd": "https://github.com/clangd/clangd/releases/download/{version}/clangd-linux-{version}.zip",
        "lua-language-server": "https://github.com/LuaLS/lua-language-server/releases/download/{version}/lua-language-server-{version}-linux-x64.tar.gz",
    }

    # 所有LSP依赖项配置
    DEPENDENCIES = [
        LspDependency(
            name="python-lsp-server",
            server_name="python-lsp-server",
            languages=["Python3"],
            install_commands={
                # 使用 uv 环境安装（项目使用 uv 环境）
                "Linux": ["uv", "pip", "install", "python-lsp-server"],
                "Darwin": ["uv", "pip", "install", "python-lsp-server"],
                "Windows": ["uv", "pip", "install", "python-lsp-server"],
            },
            verify_command=["pylsp", "--version"],
            manual_install_hint="使用 uv pip install python-lsp-server 安装",
        ),
        LspDependency(
            name="clangd",
            server_name="clangd",
            languages=["C", "C++"],
            install_commands={
                "Linux": ["sudo", "apt", "install", "-y", "clangd"],
                "Darwin": ["brew", "install", "clangd"],
                "Windows": ["choco", "install", "llvm"],
            },
            verify_command=["clangd", "--version"],
            manual_install_hint="Linux: sudo apt install clangd\n或下载预编译二进制: https://github.com/clangd/clangd/releases",
            install_method="binary",  # 使用预编译二进制安装
        ),
        LspDependency(
            name="rust-analyzer",
            server_name="rust-analyzer",
            languages=["Rust"],
            install_commands={
                "Linux": ["rustup", "component", "add", "rust-analyzer"],
                "Darwin": ["rustup", "component", "add", "rust-analyzer"],
                "Windows": ["rustup", "component", "add", "rust-analyzer"],
            },
            verify_command=["rust-analyzer", "--version"],
        ),
        LspDependency(
            name="gopls",
            server_name="gopls",
            languages=["GoLang"],
            install_commands={
                "Linux": ["go", "install", "golang.org/x/tools/gopls@latest"],
                "Darwin": ["go", "install", "golang.org/x/tools/gopls@latest"],
                "Windows": ["go", "install", "golang.org/x/tools/gopls@latest"],
            },
            verify_command=["gopls", "version"],
        ),
        LspDependency(
            name="typescript-language-server",
            server_name="typescript-language-server",
            languages=["TypeScript", "JavaScript"],
            install_commands={
                "Linux": ["npm", "install", "-g", "typescript-language-server"],
                "Darwin": ["npm", "install", "-g", "typescript-language-server"],
                "Windows": ["npm", "install", "-g", "typescript-language-server"],
            },
            verify_command=["typescript-language-server", "--version"],
        ),
        LspDependency(
            name="zls",
            server_name="zls",
            languages=["Zig"],
            install_commands={
                "Linux": ["sudo", "apt", "install", "-y", "zls"],  # Ubuntu 22.04+
                "Darwin": ["brew", "install", "zls"],
                "Windows": ["winget", "install", "zigtools.zls"],
            },
            verify_command=["zls", "--version"],
            manual_install_hint="Linux: sudo apt install zls (Ubuntu 22.04+)\n或下载: https://github.com/zigtools/zls/releases",
        ),
        LspDependency(
            name="lua-language-server",
            server_name="lua-language-server",
            languages=["Lua"],
            install_commands={
                "Linux": ["npm", "install", "-g", "lua-language-server"],
                "Darwin": ["npm", "install", "-g", "lua-language-server"],
                "Windows": ["npm", "install", "-g", "lua-language-server"],
            },
            verify_command=["lua-language-server", "--version"],
            install_method="binary",
        ),
    ]

    def __init__(self):
        """初始化依赖项管理器"""
        self._dependencies: Dict[str, LspDependency] = {
            dep.name: dep for dep in self.DEPENDENCIES
        }
        self._status_file = get_path("data") / "lsp_dependencies.json"
        # 使用配置文件中的LSP服务器目录
        self._lsp_servers_dir = get_path("lsp_servers")
        self._lsp_bin_dir = get_path("lsp_bin")
        # 确保安装目录存在
        self._lsp_servers_dir.mkdir(parents=True, exist_ok=True)
        self._lsp_bin_dir.mkdir(parents=True, exist_ok=True)
        self._load_status()

    def _load_status(self) -> None:
        """从文件加载依赖项状态"""
        try:
            if self._status_file.exists():
                with open(self._status_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for name, status_data in data.items():
                        if name in self._dependencies:
                            dep = self._dependencies[name]
                            dep.status = InstallStatus(status_data.get("status", InstallStatus.NOT_CHECKED))
                            dep.error_message = status_data.get("error_message")
                            dep.toast_shown = status_data.get("toast_shown", False)
                logger.info(f"✅ 已加载LSP依赖项状态: {self._status_file}")
        except Exception as e:
            logger.warning(f"⚠️  加载LSP依赖项状态失败: {e}")

    def _get_latest_version(self, dependency_name: str) -> Optional[str]:
        """
        获取依赖项的最新版本号

        Args:
            dependency_name: 依赖项名称

        Returns:
            最新版本号，如果获取失败则返回None
        """
        try:
            if dependency_name == "clangd":
                url = "https://api.github.com/repos/clangd/clangd/releases/latest"
            elif dependency_name == "lua-language-server":
                url = "https://api.github.com/repos/LuaLS/lua-language-server/releases/latest"
            else:
                return None

            with urllib.request.urlopen(url, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
                version = data.get("tag_name", "")
                logger.debug(f"获取 {dependency_name} 最新版本: {version}")
                return version
        except Exception as e:
            logger.error(f"获取 {dependency_name} 最新版本失败: {e}")
            return None

    def _download_binary(self, url: str, dest_path: Path) -> bool:
        """
        下载二进制文件

        Args:
            url: 下载URL
            dest_path: 目标路径

        Returns:
            是否下载成功
        """
        try:
            logger.info(f"📥 下载: {url}")
            urllib.request.urlretrieve(url, dest_path)
            logger.info(f"✅ 下载完成: {dest_path}")
            return True
        except Exception as e:
            logger.error(f"❌ 下载失败: {e}")
            return False

    def _extract_binary(self, archive_path: Path, dest_dir: Path) -> bool:
        """
        解压二进制文件

        Args:
            archive_path: 压缩文件路径
            dest_dir: 目标目录

        Returns:
            是否解压成功
        """
        try:
            logger.info(f"📦 解压: {archive_path}")
            if archive_path.suffix == ".gz":
                with tarfile.open(archive_path, "r:gz") as tar:
                    tar.extractall(dest_dir)
            elif archive_path.suffix == ".bz2":
                with tarfile.open(archive_path, "r:bz2") as tar:
                    tar.extractall(dest_dir)
            elif archive_path.suffix == ".zip":
                with zipfile.ZipFile(archive_path, "r") as zip_ref:
                    zip_ref.extractall(dest_dir)
            else:
                logger.error(f"❌ 不支持的压缩格式: {archive_path.suffix}")
                return False

            logger.info(f"✅ 解压完成: {dest_dir}")
            return True
        except Exception as e:
            logger.error(f"❌ 解压失败: {e}")
            return False

    def _ensure_rust_default_toolchain(self) -> None:
        """确保 Rust 有默认工具链，否则 rustup 命令会失败"""
        import subprocess
        try:
            # 检查是否已有默认工具链
            result = subprocess.run(
                ["rustup", "default"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0 or "no default" in result.stderr.lower() or not result.stdout.strip():
                logger.info("🔧 Rust 未设置默认工具链，正在设置 rustup default stable...")
                setup_result = subprocess.run(
                    ["rustup", "default", "stable"],
                    capture_output=True, text=True, timeout=300
                )
                if setup_result.returncode == 0:
                    logger.info(f"✅ Rust 默认工具链设置成功: {setup_result.stdout.strip()}")
                else:
                    logger.warning(f"⚠️ Rust 默认工具链设置失败: {setup_result.stderr.strip()}")
        except FileNotFoundError:
            logger.warning("⚠️ rustup 未安装，跳过默认工具链检查")
        except Exception as e:
            logger.warning(f"⚠️ 检查 Rust 默认工具链时出错: {e}")

    def _install_binary(self, dependency: LspDependency) -> bool:
        """
        安装预编译二进制文件到 ~/.local/share/peng/lsp_servers/bin/

        Args:
            dependency: 依赖项配置

        Returns:
            是否安装成功
        """
        try:
            # 获取最新版本
            version = self._get_latest_version(dependency.name)
            if not version:
                logger.error(f"❌ 无法获取 {dependency.name} 的最新版本")
                return False

            # 构建下载URL
            url_template = self.BINARY_DOWNLOAD_URLS.get(dependency.name)
            if not url_template:
                logger.error(f"❌ {dependency.name} 没有配置二进制下载URL")
                return False

            url = url_template.format(version=version)
            archive_name = Path(urlparse(url).path).name
            archive_path = self._lsp_servers_dir / archive_name

            # 确保lsp_servers目录存在
            lsp_servers_dir = self._lsp_servers_dir
            lsp_servers_dir.mkdir(parents=True, exist_ok=True)

            # 下载
            if not self._download_binary(url, archive_path):
                return False

            # 解压到临时目录
            import tempfile
            with tempfile.TemporaryDirectory() as temp_dir:
                if not self._extract_binary(archive_path, Path(temp_dir)):
                    return False

                # 创建bin目录
                bin_dir = lsp_servers_dir / "bin"
                bin_dir.mkdir(exist_ok=True)

                # 处理clangd
                if dependency.name == "clangd":
                    # 查找解压后的clangd目录（注意：目录名使用下划线，如clangd_21.1.8）
                    extracted_dirs = [d for d in Path(temp_dir).iterdir() if d.is_dir() and d.name.startswith("clangd_")]
                    if not extracted_dirs:
                        logger.error(f"❌ 未找到clangd解压目录，查找目录: {[d.name for d in Path(temp_dir).iterdir() if d.is_dir()]}")
                        return False

                    extracted_dir = extracted_dirs[0]
                    clangd_src = extracted_dir / "bin" / "clangd"
                    if not clangd_src.exists():
                        logger.error(f"❌ 未找到clangd可执行文件: {clangd_src}")
                        logger.error(f"解压目录内容: {list(extracted_dir.rglob('*'))[:10]}")
                        return False

                    # 复制到bin目录
                    import shutil
                    clangd_dst = bin_dir / "clangd"
                    shutil.copy2(clangd_src, clangd_dst)
                    os.chmod(clangd_dst, 0o755)
                    logger.info(f"✅ clangd已安装到: {clangd_dst}")

                # 处理lua-language-server
                elif dependency.name == "lua-language-server":
                    # lua-language-server需要完整的目录结构（包括main.lua等文件）
                    # 解压后文件直接在temp_dir根目录，不在子目录中

                    # 检查必要文件是否存在
                    lua_ls_src = Path(temp_dir) / "bin" / "lua-language-server"
                    main_lua_src = Path(temp_dir) / "main.lua"
                    if not lua_ls_src.exists():
                        logger.error(f"❌ 未找到lua-language-server可执行文件: {lua_ls_src}")
                        logger.error(f"解压目录内容: {list(Path(temp_dir).rglob('*'))[:10]}")
                        return False
                    if not main_lua_src.exists():
                        logger.error(f"❌ 未找到main.lua文件: {main_lua_src}")
                        return False

                    # 创建lua-language-server目录（在lsp_servers下）
                    lua_ls_dir = lsp_servers_dir / "lua-language-server"
                    lua_ls_dir.mkdir(exist_ok=True)

                    # 复制整个解压目录内容到lua-language-server目录
                    import shutil
                    for item in Path(temp_dir).iterdir():
                        dst_item = lua_ls_dir / item.name
                        if item.is_dir():
                            if dst_item.exists():
                                shutil.rmtree(dst_item)
                            shutil.copytree(item, dst_item)
                        else:
                            shutil.copy2(item, dst_item)

                    # 创建符号链接到bin目录
                    lua_ls_link = bin_dir / "lua-language-server"
                    if lua_ls_link.exists():
                        lua_ls_link.unlink()
                    lua_ls_link.symlink_to(lua_ls_dir / "bin" / "lua-language-server")

                    logger.info(f"✅ lua-language-server已安装到: {lua_ls_dir}")

            # 删除压缩文件
            archive_path.unlink()

            return True
        except Exception as e:
            logger.error(f"❌ 安装二进制失败: {e}", exc_info=True)
            return False

    def _save_status(self) -> None:
        """保存依赖项状态到文件"""
        try:
            self._status_file.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            for name, dep in self._dependencies.items():
                data[name] = {
                    "status": dep.status.value,
                    "error_message": dep.error_message,
                    "toast_shown": dep.toast_shown,
                }
            with open(self._status_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.debug(f"💾 已保存LSP依赖项状态: {self._status_file}")
        except Exception as e:
            logger.error(f"❌ 保存LSP依赖项状态失败: {e}")

    def get_platform(self) -> str:
        """
        获取当前平台

        Returns:
            平台名称: Linux, Darwin, Windows
        """
        return platform.system()

    def has_sudo_permission(self) -> bool:
        """
        检查是否有sudo权限（免密或已缓存）

        Returns:
            是否有sudo权限
        """
        if self.get_platform() == "Windows":
            return True  # Windows不使用sudo

        try:
            # 测试sudo权限（使用echo测试）
            result = subprocess.run(
                ["sudo", "-n", "echo", "test"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception as e:
            logger.debug(f"检查sudo权限失败: {e}")
            return False

    def get_install_command(self, dependency: LspDependency) -> Optional[List[str]]:
        """
        获取适合当前环境的安装命令

        如果命令需要sudo但没有sudo权限，则返回None

        Args:
            dependency: 依赖项配置

        Returns:
            安装命令列表，如果无法安装则返回None
        """
        platform_name = self.get_platform()
        install_cmd = dependency.install_commands.get(platform_name)

        if not install_cmd:
            return None

        # 检查是否需要sudo
        if install_cmd[0] == "sudo" and not self.has_sudo_permission():
            logger.warning(f"⚠️  {dependency.name} 需要sudo权限，但当前无法自动安装")
            return None

        return install_cmd

    def is_installed(self, dependency: LspDependency) -> bool:
        """
        检查依赖项是否已安装

        Args:
            dependency: 依赖项配置

        Returns:
            是否已安装
        """
        try:
            # 先尝试直接执行验证命令
            result = subprocess.run(
                dependency.verify_command,
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                logger.debug(f"检查 {dependency.name}: ✅ 已安装（系统）")
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            # 直接执行失败（命令不存在或超时），继续检查bin_path
            logger.debug(f"检查 {dependency.name}: 系统PATH中未找到，检查本地安装")
        except Exception as e:
            logger.debug(f"检查 {dependency.name} 异常: {e}")

        # 检查是否在 ~/.local/share/peng/lsp_servers/bin/ 目录下
        try:
            bin_path = self._lsp_bin_dir / dependency.verify_command[0]
            if bin_path.exists():
                # 尝试使用本地路径执行
                cmd = [str(bin_path)] + dependency.verify_command[1:]
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    logger.debug(f"检查 {dependency.name}: ✅ 已安装（~/.local/share/peng/lsp_servers/bin）")
                    return True
        except Exception as e:
            logger.debug(f"检查 {dependency.name} lsp_servers/bin 失败: {e}")

        # 检查特殊路径
        try:
            # python-lsp-server: 检查项目.venv虚拟环境
            if dependency.name == "python-lsp-server":
                from peng.config import get_project_root
                project_root = get_project_root()
                venv_paths = [
                    project_root / ".venv" / "bin" / "pylsp",  # 当前项目.venv
                ]
                for venv_path in venv_paths:
                    if venv_path.exists():
                        cmd = [str(venv_path)] + dependency.verify_command[1:]
                        result = subprocess.run(cmd, capture_output=True, timeout=5)
                        if result.returncode == 0:
                            logger.debug(f"检查 {dependency.name}: ✅ 已安装（{venv_path}）")
                            return True

            # gopls: 检查~/go/bin
            if dependency.name == "gopls":
                go_bin_path = Path.home() / "go" / "bin" / "gopls"
                if go_bin_path.exists():
                    cmd = [str(go_bin_path)] + dependency.verify_command[1:]
                    result = subprocess.run(cmd, capture_output=True, timeout=5)
                    if result.returncode == 0:
                        logger.debug(f"检查 {dependency.name}: ✅ 已安装（~/go/bin）")
                        return True
        except Exception as e:
            logger.debug(f"检查 {dependency.name} 特殊路径失败: {e}")

        logger.debug(f"检查 {dependency.name}: ❌ 未安装")
        return False

    async def install(
        self,
        dependency: LspDependency,
        progress_callback: Optional[Callable[[str, InstallStatus, Optional[str]], Awaitable[None]]] = None,
    ) -> bool:
        """
        安装依赖项

        Args:
            dependency: 依赖项配置
            progress_callback: 进度回调函数 (name, status, error_message)

        Returns:
            是否安装成功
        """
        # Windows 平台跳过 LSP 安装（需要 rustup/npm/go 等未安装的工具，symlink 需要管理员权限）
        import sys
        if sys.platform == "win32":
            logger.info(f"⏭️ Windows 平台跳过 {dependency.name} 安装")
            dependency.status = InstallStatus.INSTALL_FAILED
            dependency.error_message = "Windows 平台暂不支持自动安装"
            self._save_status()
            return False

        # 更新状态为安装中
        dependency.status = InstallStatus.INSTALLING
        dependency.error_message = None
        self._save_status()

        # 调用进度回调
        if progress_callback:
            await progress_callback(dependency.name, InstallStatus.INSTALLING, None)

        logger.info(f"🔧 开始安装 {dependency.name}...")

        try:
            # 检查是否使用二进制安装方式
            if dependency.install_method == "binary":
                logger.info(f"🔧 使用二进制安装方式: {dependency.name}")
                if self._install_binary(dependency):
                    dependency.status = InstallStatus.INSTALLED
                    dependency.toast_shown = True
                    self._save_status()

                    if progress_callback:
                        await progress_callback(dependency.name, InstallStatus.INSTALLED, None)

                    logger.info(f"✅ {dependency.name} 二进制安装成功")
                    return True
                else:
                    dependency.status = InstallStatus.INSTALL_FAILED
                    dependency.error_message = "二进制安装失败"
                    self._save_status()

                    if progress_callback:
                        await progress_callback(dependency.name, InstallStatus.INSTALL_FAILED, "二进制安装失败")

                    logger.error(f"❌ {dependency.name} 二进制安装失败")
                    return False

            # 获取适合当前环境的安装命令（检查sudo权限）
            install_cmd = self.get_install_command(dependency)

            if not install_cmd:
                # 无法自动安装（可能是缺少sudo权限）
                manual_hint = dependency.manual_install_hint or "请手动安装此依赖项"
                error_msg = f"无法自动安装（可能需要sudo权限）\n{manual_hint}"
                dependency.status = InstallStatus.INSTALL_FAILED
                dependency.error_message = error_msg
                self._save_status()

                if progress_callback:
                    await progress_callback(dependency.name, InstallStatus.INSTALL_FAILED, error_msg)

                logger.error(f"❌ {dependency.name}: {error_msg}")
                return False

            # 执行安装命令（在后台）
            logger.info(f"📦 执行安装命令: {' '.join(install_cmd)}")

            # 对 rust-analyzer，先确保有默认 Rust 工具链
            if dependency.name == "rust-analyzer":
                self._ensure_rust_default_toolchain()

            # 创建子进程执行安装（使用 asyncio.to_thread 避免阻塞事件循环，设置超时）
            import subprocess
            try:
                result = await asyncio.to_thread(
                    subprocess.run, install_cmd,
                    cwd=project_root, text=True, capture_output=True,
                    timeout=300,  # 5分钟超时，避免网络问题导致永久卡死
                )
            except subprocess.TimeoutExpired:
                error_msg = f"安装超时（5分钟）: {' '.join(install_cmd)}"
                dependency.status = InstallStatus.INSTALL_FAILED
                dependency.error_message = error_msg
                self._save_status()
                if progress_callback:
                    await progress_callback(dependency.name, InstallStatus.INSTALL_FAILED, error_msg)
                logger.error(f"❌ {dependency.name}: {error_msg}")
                return False
            if result.returncode == 0:
                # 安装成功
                dependency.status = InstallStatus.INSTALLED
                dependency.toast_shown = True  # 标记已发送toast
                self._save_status()

                if progress_callback:
                    await progress_callback(dependency.name, InstallStatus.INSTALLED, None)

                logger.info(f"✅ {dependency.name} 安装成功")
                return True
            else:
                # 安装失败
                error_msg = (result.stderr if isinstance(result.stderr, str) else result.stderr.decode("utf-8", errors="ignore")).strip() or "安装失败"
                dependency.status = InstallStatus.INSTALL_FAILED
                dependency.error_message = error_msg
                self._save_status()

                if progress_callback:
                    await progress_callback(dependency.name, InstallStatus.INSTALL_FAILED, error_msg)

                logger.error(f"❌ {dependency.name} 安装失败: {error_msg}")
                return False

        except asyncio.TimeoutError:
            error_msg = "安装超时（10分钟）"
            dependency.status = InstallStatus.INSTALL_FAILED
            dependency.error_message = error_msg
            self._save_status()

            if progress_callback:
                await progress_callback(dependency.name, InstallStatus.INSTALL_FAILED, error_msg)

            logger.error(f"❌ {dependency.name}: {error_msg}")
            return False

        except Exception as e:
            error_msg = f"安装异常: {e}"
            dependency.status = InstallStatus.INSTALL_FAILED
            dependency.error_message = error_msg
            self._save_status()

            if progress_callback:
                await progress_callback(dependency.name, InstallStatus.INSTALL_FAILED, error_msg)

            logger.error(f"❌ {dependency.name}: {error_msg}\n{logger.exception('')}")
            return False

    async def check_and_install_all(
        self,
        progress_callback: Optional[Callable[[str, InstallStatus, Optional[str]], Awaitable[None]]] = None,
    ) -> Dict[str, bool]:
        """
        检查并安装所有依赖项

        Args:
            progress_callback: 进度回调函数 (name, status, error_message)

        Returns:
            安装结果字典 {name: success}
        """
        results = {}

        for dependency in self._dependencies.values():
            # 检查是否已安装
            if self.is_installed(dependency):
                dependency.status = InstallStatus.INSTALLED
                self._save_status()

                # 如果之前已经显示过toast，则不再显示（避免重复提醒）
                if dependency.toast_shown:
                    logger.info(f"✅ {dependency.name} 已安装（已提醒过），跳过")
                else:
                    logger.info(f"✅ {dependency.name} 已安装（首次检测），不显示toast")
                    # 首次检测到已安装，标记为已提醒，但不显示toast
                    dependency.toast_shown = True
                    self._save_status()
                results[dependency.name] = True
                continue

            # 未安装，开始安装
            success = await self.install(dependency, progress_callback)
            results[dependency.name] = success

        return results

    def get_dependencies_status(self) -> Dict[str, Dict[str, any]]:
        """
        获取所有依赖项的状态

        Returns:
            状态字典 {name: {status, error_message, ...}}
        """
        return {
            name: {
                "status": dep.status.value,
                "error_message": dep.error_message,
                "languages": dep.languages,
                "server_name": dep.server_name,
            }
            for name, dep in self._dependencies.items()
        }

    def get_dependency(self, name: str) -> Optional[LspDependency]:
        """
        根据名称获取依赖项

        Args:
            name: 依赖项名称

        Returns:
            依赖项配置，如果不存在则返回None
        """
        return self._dependencies.get(name)

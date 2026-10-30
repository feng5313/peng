"""
配置加载器

从 JSON 文件加载配置，支持环境变量替换。
配置优先级：环境变量 > 配置文件 > 默认值

注意：只有大模型配置支持环境变量覆盖，其他配置仅从配置文件读取。
"""

from __future__ import annotations

import logging
import os
import sys
import re
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from peng.common import jsonp as json
from peng.common.paths import chown_to_project_owner, ensure_dir_with_owner

# 大模型相关环境变量（仅这些支持环境变量配置）

logger = logging.getLogger(__name__)
LLM_ENV_VARS = {
    "API_KEY": "",
    "BASE_URL": "",
}

# 全自动模式触发关键词（使用frozenset优化查找）
_AUTO_MODE_KEYWORDS: frozenset[str] = frozenset(
    [
        "全自动",
        "自动执行",
        "无需确认",
        "持续执行",
        "自主完成",
    ]
)

# 环境变量展开预编译正则
_ENV_VAR_EXPAND_PATTERN = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")

# fetch_providers 预编译正则（匹配 @<provider>，支持字母数字和0个或多个emoji图标混合）
_FETCH_PROVIDER_PATTERN = re.compile(
    r"@[a-zA-Z0-9\.\-\U0001F300-\U0001F5FF\U0001F600-\U0001F64F"
    r"\U0001F680-\U0001F6FF\U0001F900-\U0001F9FF\U0001FA00-\U0001FAFF"
    r"\U0001F1E0-\U0001F1FF\U00002600-\U000027BF\U00002300-\U000023FF"
    r"\U0000FE00-\U0000FE0F\U0000200D_]+",
    re.IGNORECASE,
)


@dataclass
class FailureInfo:
    """Provider 失败信息（支持自适应冷却时间）"""

    failure_count: int = 0  # 连续失败次数
    last_failure_time: float = 0.0  # 最后一次失败的时间戳
    cooldown: int = 30  # 当前冷却时间（秒），默认 30 秒，失败加倍，成功重置

    def to_dict(self) -> dict[str, Any]:
        """转换为字典（用于 JSON 序列化）"""
        return {
            "failure_count": self.failure_count,
            "last_failure_time": self.last_failure_time,
            "cooldown": self.cooldown,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FailureInfo:
        """从字典创建（用于 JSON 反序列化）"""
        return cls(
            failure_count=data.get("failure_count", 0),
            last_failure_time=data.get("last_failure_time", 0.0),
            cooldown=data.get("cooldown", 30),
        )


# 项目根目录缓存
_project_root_cache: Path | None = None


def _deep_merge(base: dict, overlay: dict) -> dict:
    """递归合并 overlay 到 base（overlay 覆盖 base 的同名字段）

    对于嵌套 dict，递归合并；对于非 dict 值，overlay 直接覆盖。
    不修改原始 base 和 overlay。

    Args:
        base: 基础字典（如 config/ 目录中的配置）
        overlay: 覆盖字典（如 data/ 目录中的配置）

    Returns:
        合并后的新字典
    """
    import copy

    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _load_json_file(path: Path) -> dict:
    """安全加载 JSON 文件，失败返回空字典"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:
        logger.warning(f"加载 JSON 文件失败 {path}: {e}")
        return {}


def _load_config_with_overlay(config_path: Path, data_path: Path) -> dict:
    """加载配置文件，支持 data 目录覆盖

    读取优先级：data 目录的同名文件覆盖 config 目录的基础配置（deep merge）。

    Args:
        config_path: config 目录中的基础配置文件路径
        data_path: data 目录中的覆盖配置文件路径

    Returns:
        合并后的配置字典（若两个文件都不存在则返回空字典）
    """
    base_data = {}
    if config_path.exists():
        base_data = _load_json_file(config_path)

    overlay_data = {}
    if data_path.exists():
        overlay_data = _load_json_file(data_path)

    if not overlay_data:
        return base_data
    if not base_data:
        return overlay_data

    return _deep_merge(base_data, overlay_data)


def _get_data_overlay_path(config_filename: str) -> Path:
    """获取 config 文件对应的 data 目录覆盖路径

    Args:
        config_filename: 配置文件名（如 "peng.json", "webui.json"）

    Returns:
        data 目录中的同名文件路径
    """
    project_root = _get_project_root()
    return project_root / "data" / config_filename


# 配置缓存：config_path -> (config对象, 主文件mtime, 相关文件mtime字典)
_config_cache: dict[Path, tuple[PengConfig, float, dict[Path, float]]] = {}


def get_project_root() -> Path:
    """获取项目根目录（从当前文件位置自动检测）

    优先级：
    1. 包含 .git 的目录（支持 Git Worktree，每个 worktree 独立）
    2. 包含 pyproject.toml 的目录
    3. 默认 ~/work/peng

    支持 Git Worktree：在 worktree 中运行时，返回当前 worktree 的根目录，
    而不是主仓库根目录。这样每个 worktree 都有独立的配置、日志、数据和虚拟环境。

    Returns:
        Path: 项目根目录路径
    """
    global _project_root_cache
    if _project_root_cache is not None:
        return _project_root_cache

    # 从当前文件位置推断
    current = Path(__file__).resolve()
    for parent in current.parents:
        git_path = parent / ".git"
        if git_path.exists():
            # 无论是普通仓库还是 worktree，都返回包含 .git 的目录
            # worktree 应该使用自己的配置和数据，而不是共享主仓库的
            _project_root_cache = parent
            return _project_root_cache
        if (parent / "pyproject.toml").exists():
            _project_root_cache = parent
            return _project_root_cache

    # 默认使用 ~/work/peng
    _project_root_cache = Path.home() / "work" / "peng"
    return _project_root_cache


# 兼容旧的内部调用
_get_project_root = get_project_root


def find_config_file(name: str = "peng.json") -> Path | None:
    """
    统一的配置文件查找逻辑

    按优先级搜索配置文件：
    1. 工作目录的 .peng/ 子目录
    2. 工作目录的 config/ 子目录
    3. ~/.config/peng/ 目录
    4. 项目根目录的 config/ 子目录

    Args:
        name: 配置文件名（默认 peng.json）

    Returns:
        找到的配置文件路径，如果未找到则返回 None
    """
    project_root = _get_project_root()
    search_paths = [
        project_root / "config" / name,  # 项目根目录 config/
    ]

    for path in search_paths:
        if path.exists():
            return path

    return None


def _get_home() -> Path:
    """获取 HOME 目录"""
    return Path.home()


class PathsConfig(BaseModel):
    """路径配置（从配置文件读取）"""

    # 目录
    project_root: str = ""
    data_dir: str = ""
    shot_dir: str = "data/shot"
    data_json_dir: str = ""
    messages_dir: str = ""
    backups_dir: str = ""
    config_dir: str = ""
    temp_dir: str = ""
    logs_dir: str = ""
    worktrees_dir: str = ".worktrees"
    output_dir: str = ""

    # 数据库（直接是路径字符串）
    history_dir: str = "data/history"
    postgresql_data_dir: str = "/mnt/wsl/work/db/postgresql"

    # 文件路径（直接是路径字符串）
    agents_md: str = "config/AGENTS.md"
    log_file: str = "logs/peng.log"
    chat_history_file: str = "data/history/chat_history"
    changelog_file: str = "~/work/backups/CHANGELOG.md"
    colors_json: str = "config/colors.json"
    prompts_json: str = "config/prompts.jsonp"
    webui_json: str = "config/webui.json"
    logs_json: str = "config/logs.json"
    db_json: str = "config/db.json"
    skill_md: str = "skills/{skill_name}/SKILL.md"
    skill_md_name: str = "SKILL.md"
    failed_providers_json: str = "config/failed_providers.json"
    input_history_json: str = ""  # 空字符串表示使用系统临时目录（跨平台）
    postgresql_data_dir: str = "/mnt/wsl/work/db/postgresql"
    fallback_postgresql_data_dir: str = "~/.db/postgresql"
    allowed_tool_call_dirs: list[str] = Field(default_factory=lambda: ["data/json"])

    # LSP服务器路径配置
    lsp_servers_dir: str = "~/.local/share/peng/lsp_servers"
    lsp_bin_dir: str = "~/.local/share/peng/lsp_servers/bin"
    # 各语言服务器可执行文件路径（空字符串表示自动搜索）
    lsp_pylsp_path: str = ""
    lsp_clangd_path: str = ""
    lsp_rust_analyzer_path: str = ""
    lsp_gopls_path: str = ""
    lsp_typescript_language_server_path: str = ""
    lsp_lua_language_server_path: str = ""
    lsp_zls_path: str = ""

    def __init__(self, **data):
        super().__init__(**data)
        home = _get_home()
        default_root = _get_project_root()

        # 项目根目录：配置文件 > 自动检测
        if not self.project_root:
            self.project_root = str(default_root)

        project_root = Path(self.project_root)

        # 展开路径：~/ 开头相对于 home，相对路径相对于项目根目录
        # 目录路径
        for attr in [
            "data_dir",
            "shot_dir",
            "data_json_dir",
            "messages_dir",
            "config_dir",
            "temp_dir",
            "output_dir",
            "logs_dir",
            "history_dir",
        ]:
            path_str = getattr(self, attr, None)
            if path_str and not path_str.startswith("~/") and not Path(path_str).is_absolute():
                setattr(self, attr, str(project_root / path_str))

        # backups_dir 特殊处理
        if self.backups_dir and self.backups_dir.startswith("~/"):
            self.backups_dir = str(home / self.backups_dir[2:])
        elif self.backups_dir and not Path(self.backups_dir).is_absolute():
            self.backups_dir = str(project_root / self.backups_dir)

        # 跨平台路径回退：如果配置的绝对路径的父目录不存在（如 Linux 路径在 Windows 上），
        # 则回退到系统临时目录下的子目录，避免在错误位置自动创建目录。
        import tempfile as _tempfile

        _tmp_root = Path(_tempfile.gettempdir()) / "peng"

        # 辅助函数：判断路径是否为"硬编码的绝对路径"（在 Linux 上以 / 开头，
        # 或在 Windows 上以盘符开头），且其父目录不存在时应回退。
        def _is_hardcoded_absolute(path_str: str) -> bool:
            """判断是否为硬编码的绝对路径（非 ~/ 展开的）"""
            p = Path(path_str)
            # Unix 绝对路径（以 / 开头）
            if path_str.startswith("/"):
                return True
            # Windows 绝对路径（如 D:\...）
            if p.is_absolute():
                return True
            return False

        # backups_dir 回退
        if self.backups_dir:
            _backups_path = Path(self.backups_dir)
            if _is_hardcoded_absolute(self.backups_dir) and not _backups_path.parent.exists():
                self.backups_dir = str(_tmp_root / "backups")
                logger.debug(
                    f"backups_dir 父目录不存在 ({_backups_path.parent})，"
                    f"回退到系统临时目录: {self.backups_dir}"
                )

        # postgresql_data_dir 回退
        if self.postgresql_data_dir:
            _pg_path = Path(self.postgresql_data_dir)
            if _is_hardcoded_absolute(self.postgresql_data_dir) and not _pg_path.parent.exists():
                self.postgresql_data_dir = str(_tmp_root / "postgresql")
                logger.debug(
                    f"postgresql_data_dir 父目录不存在 ({_pg_path.parent})，"
                    f"回退到系统临时目录: {self.postgresql_data_dir}"
                )

        # changelog_file 回退：检查其父目录是否存在
        if self.changelog_file:
            if self.changelog_file.startswith("~/"):
                _changelog_path = home / self.changelog_file[2:]
            elif not Path(self.changelog_file).is_absolute():
                _changelog_path = project_root / self.changelog_file
            else:
                _changelog_path = Path(self.changelog_file)
            if not _changelog_path.parent.exists():
                self.changelog_file = str(_tmp_root / "CHANGELOG.md")
                logger.debug(
                    f"changelog_file 父目录不存在 ({_changelog_path.parent})，"
                    f"回退到系统临时目录: {self.changelog_file}"
                )

        # 自动创建所有路径中定义的目录
        self._auto_create_directories()

    def _resolve_path(self, path_str: str) -> Path:
        """解析路径字符串为 Path 对象

        支持 ~/ 开头、相对路径、绝对路径

        Args:
            path_str: 路径字符串

        Returns:
            解析后的 Path 对象
        """
        if path_str.startswith("~/"):
            return Path.home() / path_str[2:]
        return Path(path_str)

    def _auto_create_directories(self) -> None:
        """自动创建 paths 中定义的所有目录

        遍历所有路径属性，对于目录路径（非文件路径）自动创建。
        使用 parents=True, exist_ok=True 确保：
        - 可以创建多级目录
        - 目录已存在时不报错
        """
        # 需要创建的目录列表（排除文件路径如 agents_md）
        dir_attrs = [
            "data_dir",
            "shot_dir",
            "data_json_dir",
            "messages_dir",
            "backups_dir",
            "config_dir",
            "temp_dir",
            "logs_dir",
            "output_dir",
            "history_dir",
        ]

        for attr in dir_attrs:
            path_str = getattr(self, attr, None)
            if path_str:
                path = self._resolve_path(path_str)
                # 创建目录，parents=True 支持多级目录，exist_ok=True 避免已存在时报错
                ensure_dir_with_owner(path)

        # 同时创建 allowed_tool_call_dirs 中定义的目录
        for dir_path in self.allowed_tool_call_dirs:
            path = self._resolve_path(dir_path)
            if not path.is_absolute():
                path = self.project_root_path / path
            ensure_dir_with_owner(path)

    @property
    def project_root_path(self) -> Path:
        return Path(self.project_root)

    @property
    def data_dir_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def shot_dir_path(self) -> Path:
        return Path(self.shot_dir)

    @property
    def data_json_dir_path(self) -> Path:
        return Path(self.data_json_dir)

    @property
    def messages_dir_path(self) -> Path:
        return Path(self.messages_dir)

    @property
    def backups_dir_path(self) -> Path:
        return Path(self.backups_dir)

    @property
    def config_dir_path(self) -> Path:
        return Path(self.config_dir)

    @property
    def temp_dir_path(self) -> Path:
        return Path(self.temp_dir)

    @property
    def logs_dir_path(self) -> Path:
        return Path(self.logs_dir)

    @property
    def agents_md_path(self) -> Path:
        return Path(self.agents_md)

    @property
    def worktrees_dir_path(self) -> Path:
        return self.project_root_path / self.worktrees_dir

    @property
    def output_dir_path(self) -> Path:
        return Path(self.output_dir)

    @property
    def allowed_tool_call_dirs_paths(self) -> list[Path]:
        """获取允许 tool_call 读写的目录列表（绝对路径）"""
        project_root = self.project_root_path
        result = []
        for dir_path in self.allowed_tool_call_dirs:
            if dir_path.startswith("~/"):
                result.append(Path.home() / dir_path[2:])
            else:
                result.append(project_root / dir_path)
        return result

    @property
    def lsp_servers_dir_path(self) -> Path:
        """LSP服务器安装目录路径（绝对路径）"""
        path_str = self.lsp_servers_dir
        if path_str.startswith("~/"):
            return Path.home() / path_str[2:]
        elif not Path(path_str).is_absolute():
            return self.project_root_path / path_str
        return Path(path_str)

    @property
    def lsp_bin_dir_path(self) -> Path:
        """LSP服务器bin目录路径（绝对路径）"""
        path_str = self.lsp_bin_dir
        if path_str.startswith("~/"):
            return Path.home() / path_str[2:]
        elif not Path(path_str).is_absolute():
            return self.project_root_path / path_str
        return Path(path_str)

    def _get_lsp_path(self, path_attr: str) -> Optional[Path]:
        """获取LSP服务器路径（内部方法）"""
        path_str = getattr(self, path_attr, "")
        if not path_str:
            return None
        if path_str.startswith("~/"):
            return Path.home() / path_str[2:]
        elif Path(path_str).is_absolute():
            return Path(path_str)
        else:
            return self.project_root_path / path_str

    @property
    def lsp_pylsp_path_value(self) -> Optional[Path]:
        """Python LSP服务器路径（如果配置）"""
        return self._get_lsp_path("lsp_pylsp_path")

    @property
    def lsp_clangd_path_value(self) -> Optional[Path]:
        """Clangd LSP服务器路径（如果配置）"""
        return self._get_lsp_path("lsp_clangd_path")

    @property
    def lsp_rust_analyzer_path_value(self) -> Optional[Path]:
        """Rust Analyzer LSP服务器路径（如果配置）"""
        return self._get_lsp_path("lsp_rust_analyzer_path")

    @property
    def lsp_gopls_path_value(self) -> Optional[Path]:
        """Go LSP服务器路径（如果配置）"""
        return self._get_lsp_path("lsp_gopls_path")

    @property
    def lsp_typescript_language_server_path_value(self) -> Optional[Path]:
        """TypeScript LSP服务器路径（如果配置）"""
        return self._get_lsp_path("lsp_typescript_language_server_path")

    @property
    def lsp_lua_language_server_path_value(self) -> Optional[Path]:
        """Lua LSP服务器路径（如果配置）"""
        return self._get_lsp_path("lsp_lua_language_server_path")

    @property
    def lsp_zls_path_value(self) -> Optional[Path]:
        """Zig LSP服务器路径（如果配置）"""
        return self._get_lsp_path("lsp_zls_path")

    @property
    def history_dir_path(self) -> Path:
        """历史记录目录路径（绝对路径）"""
        path_str = self.__dict__["history_dir"]
        if path_str.startswith("~/"):
            return Path.home() / path_str[2:]
        elif not Path(path_str).is_absolute():
            return self.project_root_path / path_str
        return Path(path_str)

    @property
    def log_file_path(self) -> Path:
        """日志文件路径（绝对路径）"""
        path_str = self.__dict__["log_file"]
        if path_str.startswith("~/"):
            return Path.home() / path_str[2:]
        elif not Path(path_str).is_absolute():
            return self.project_root_path / path_str
        return Path(path_str)

    @property
    def chat_history_file_path(self) -> Path:
        """聊天历史文件路径（绝对路径）"""
        path_str = self.__dict__["chat_history_file"]
        if path_str.startswith("~/"):
            return Path.home() / path_str[2:]
        elif not Path(path_str).is_absolute():
            return self.project_root_path / path_str
        return Path(path_str)

    @property
    def changelog_file_path(self) -> Path:
        """备份变更日志路径（绝对路径）"""
        path_str = self.__dict__["changelog_file"]
        if path_str.startswith("~/"):
            return Path.home() / path_str[2:]
        elif not Path(path_str).is_absolute():
            return self.project_root_path / path_str
        return Path(path_str)

    @property
    def colors_json_path(self) -> Path:
        """颜色配置文件路径（绝对路径）"""
        path_str = self.__dict__["colors_json"]
        if path_str.startswith("~/"):
            return Path.home() / path_str[2:]
        elif not Path(path_str).is_absolute():
            return self.project_root_path / path_str
        return Path(path_str)

    @property
    def prompts_json_path(self) -> Path:
        """Prompts配置文件路径（绝对路径）"""
        path_str = self.__dict__["prompts_json"]
        if path_str.startswith("~/"):
            return Path.home() / path_str[2:]
        elif not Path(path_str).is_absolute():
            return self.project_root_path / path_str
        return Path(path_str)

    @property
    def webui_json_path(self) -> Path:
        """WebUI配置文件路径（绝对路径）"""
        path_str = self.__dict__["webui_json"]
        if path_str.startswith("~/"):
            return Path.home() / path_str[2:]
        elif not Path(path_str).is_absolute():
            return self.project_root_path / path_str
        return Path(path_str)

    @property
    def logs_json_path(self) -> Path:
        """日志配置文件路径（绝对路径）"""
        path_str = self.__dict__["logs_json"]
        if path_str.startswith("~/"):
            return Path.home() / path_str[2:]
        elif not Path(path_str).is_absolute():
            return self.project_root_path / path_str
        return Path(path_str)

    @property
    def failed_providers_json_path(self) -> Path:
        """失败provider记录路径（绝对路径）"""
        path_str = self.__dict__["failed_providers_json"]
        if path_str.startswith("~/"):
            return Path.home() / path_str[2:]
        elif not Path(path_str).is_absolute():
            return self.project_root_path / path_str
        return Path(path_str)

    @property
    def input_history_json_path(self) -> Path:
        """输入历史记录路径（绝对路径，跨平台）"""
        path_str = self.input_history_json
        if not path_str:
            # 未配置时使用配置的临时目录
            from peng.common.paths import get_temp_directory

            return get_temp_directory() / "peng_input_history.json"
        if path_str.startswith("~/"):
            return Path.home() / path_str[2:]
        elif not Path(path_str).is_absolute():
            return self.project_root_path / path_str
        return Path(path_str)

    @property
    def log_file_path(self) -> Path:
        """日志文件路径（绝对路径）"""
        path_str = self.log_file
        if path_str.startswith("~/"):
            return Path.home() / path_str[2:]
        elif not Path(path_str).is_absolute():
            return self.project_root_path / path_str
        return Path(path_str)

    @property
    def chat_history_file_path(self) -> Path:
        """聊天历史文件路径（绝对路径）"""
        path_str = self.chat_history_file
        if path_str.startswith("~/"):
            return Path.home() / path_str[2:]
        elif not Path(path_str).is_absolute():
            return self.project_root_path / path_str
        return Path(path_str)

    @property
    def changelog_file_path(self) -> Path:
        """备份变更日志路径（绝对路径）"""
        path_str = self.changelog_file
        if path_str.startswith("~/"):
            return Path.home() / path_str[2:]
        elif not Path(path_str).is_absolute():
            return self.project_root_path / path_str
        return Path(path_str)

    @property
    def colors_json_path(self) -> Path:
        """颜色配置文件路径（绝对路径）"""
        path_str = self.colors_json
        if path_str.startswith("~/"):
            return Path.home() / path_str[2:]
        elif not Path(path_str).is_absolute():
            return self.project_root_path / path_str
        return Path(path_str)

    @property
    def prompts_json_path(self) -> Path:
        """Prompts配置文件路径（绝对路径）"""
        path_str = self.prompts_json
        if path_str.startswith("~/"):
            return Path.home() / path_str[2:]
        elif not Path(path_str).is_absolute():
            return self.project_root_path / path_str
        return Path(path_str)

    @property
    def webui_json_path(self) -> Path:
        """WebUI配置文件路径（绝对路径）"""
        path_str = self.webui_json
        if path_str.startswith("~/"):
            return Path.home() / path_str[2:]
        elif not Path(path_str).is_absolute():
            return self.project_root_path / path_str
        return Path(path_str)

    @property
    def logs_json_path(self) -> Path:
        """日志配置文件路径（绝对路径）"""
        path_str = self.logs_json
        if path_str.startswith("~/"):
            return Path.home() / path_str[2:]
        elif not Path(path_str).is_absolute():
            return self.project_root_path / path_str
        return Path(path_str)

    @property
    def failed_providers_json_path(self) -> Path:
        """失败provider记录路径（绝对路径）"""
        path_str = self.failed_providers_json
        if path_str.startswith("~/"):
            return Path.home() / path_str[2:]
        elif not Path(path_str).is_absolute():
            return self.project_root_path / path_str
        return Path(path_str)


# ==================== 路径获取函数 ====================


_path_cache: PengConfig | None = None


def get_path(path_name: str, use_test_db: bool = False, **kwargs) -> Path:
    """获取指定路径（直接返回绝对路径）

    Args:
        path_name: 路径名称
                   数据库路径：
                   - "messages": 消息数据库路径
                   - "tasks": 事务数据库路径
                   - "test_messages": 测试消息数据库路径
                   - "test_tasks": 测试事务数据库路径
                   - "test_processes": 测试进程数据库路径

                   目录路径：
                   - "data": 数据目录路径
                   - "logs": 日志目录路径
                   - "config": 配置目录路径
                   - "output": 输出目录路径
                   - "backups": 备份目录路径
                   - "history": 历史记录目录路径
                   - "worktrees": git worktrees 目录路径

                   文件路径：
                   - "log_file": 日志文件路径 (logs/peng.log)
                   - "agents_md": agents.md 文件路径 (config/AGENTS.md)
                   - "chat_history": 聊天历史文件路径 (data/history/chat_history)
                   - "changelog": 备份变更日志路径 (~/work/backups/CHANGELOG.md)
                   - "colors_json": 颜色配置文件路径 (config/colors.json)
                   - "prompts_json": prompts 配置文件路径 (config/prompts.json)
                   - "webui_json": webui 配置文件路径 (config/webui.json)
                   - "logs_json": 日志配置文件路径 (config/logs.json)
                   - "failed_providers": 失败 provider 记录路径 (~/.config/peng/failed_providers.json)
                   - "input_history": 输入历史记录路径 (/tmp/peng_input_history.json)

                   其他：
                   - "skill_md": skill 文件路径，需要 skill_name 参数
        **kwargs: 其他参数（如 skill_name）

    Returns:
        对应的绝对路径 Path 对象

    Raises:
        ValueError: 不支持的路径名称
    """
    # 每次都重新加载配置，确保路径解析正确
    config = load_config()
    project_root = config.paths.project_root_path

    # 构建路径映射（直接使用 @property，已返回绝对路径）
    path_map = {
        # 目录
        "data": config.paths.data_dir_path,
        "logs": config.paths.logs_dir_path,
        "config": config.paths.config_dir_path,
        "output": config.paths.output_dir_path,
        "backups": config.paths.backups_dir_path,
        "history": config.paths.history_dir_path,
        "worktrees": config.paths.worktrees_dir_path,
        "lsp_servers": config.paths.lsp_servers_dir_path,
        "lsp_bin": config.paths.lsp_bin_dir_path,
        # 文件
        "log_file": config.paths.log_file_path,
        "chat_history": config.paths.chat_history_file_path,
        "changelog": config.paths.changelog_file_path,
        "colors_json": config.paths.colors_json_path,
        "prompts_json": config.paths.prompts_json_path,
        "webui_json": config.paths.webui_json_path,
        "logs_json": config.paths.logs_json_path,
        "failed_providers": config.paths.failed_providers_json_path,
        "input_history": config.paths.input_history_json_path,
    }

    # 处理 agents_md（兼容旧代码）
    if path_name == "agents_md":
        return config.paths.agents_md_path

    # 处理 skill_md
    if path_name == "skill_md":
        skill_name = kwargs.get("skill_name")
        if not skill_name:
            raise ValueError("skill_md 需要 skill_name 参数")
        skill_path = config.paths.skill_md.replace("{skill_name}", skill_name)
        if skill_path.startswith("~/"):
            return Path.home() / skill_path[2:]
        elif not Path(skill_path).is_absolute():
            return project_root / skill_path
        return Path(skill_path)

    # 兼容旧的 use_test_db 参数
    if use_test_db and path_name == "messages":
        return config.paths.test_messages_db_path
    if use_test_db and path_name == "tasks":
        return config.paths.test_tasks_db_path

    if path_name not in path_map:
        raise ValueError(f"不支持的路径名称: {path_name}。支持的路径: {list(path_map.keys())}")

    return path_map[path_name]


def _resolve_to_absolute(project_root: Path, path_str: str) -> Path:
    """将相对路径转换为绝对路径

    Args:
        project_root: 项目根目录
        path_str: 路径字符串（可以是相对路径、绝对路径或以 ~ 开头的路径）

    Returns:
        绝对路径 Path 对象
    """
    path = Path(path_str)
    if path_str.startswith("~/"):
        return Path.home() / path_str[2:]
    elif not path.is_absolute():
        return project_root / path
    return path


# ==================== 运行期 vision 能力禁用（事务#1819） ====================
# 背景：provider 配置声明 vision=true 但后端实际不支持多模态（如 vLLM/sglang
# 部署未启用视觉，返回"messages.content.type 参数非法，取值范围 ['text']"），
# 事务#1818中 s-qwen3.8 因此每次收到附件都 400 并被误冷却。运行期禁用标记
# 使 provider 选择与请求构建把该 provider 视为非 vision，实现自适应纠偏，
# 无需人工改配置文件。
# 进程内存标记（不持久化）：重启/配置热重载后自动复位，TTL 到期自动恢复探测。
_RUNTIME_VISION_DISABLED: dict[str, float] = {}  # provider名 → 禁用到期时间戳


def disable_provider_vision(provider_name: str, ttl_seconds: float = 600) -> None:
    """运行期禁用指定 provider 的 vision 能力（TTL 到期自动恢复）

    Args:
        provider_name: provider 名称（空名忽略）
        ttl_seconds: 禁用时长（秒），默认10分钟
    """
    if not provider_name:
        return
    _RUNTIME_VISION_DISABLED[provider_name] = time.time() + ttl_seconds
    logger.warning(
        f"[LLMConfig] 运行期禁用 {provider_name} 的vision能力({ttl_seconds:.0f}秒)："
        f"后端不支持多模态content"
    )


def is_provider_vision_runtime_disabled(provider_name: str) -> bool:
    """检查 provider 的 vision 能力是否被运行期禁用（TTL 过期自动清除标记）"""
    expiry = _RUNTIME_VISION_DISABLED.get(provider_name)
    if expiry is None:
        return False
    if time.time() >= expiry:
        # 禁用到期：清除标记，恢复探测（后端可能已修复）
        _RUNTIME_VISION_DISABLED.pop(provider_name, None)
        logger.info(f"[LLMConfig] {provider_name} vision运行期禁用到期，恢复探测")
        return False
    return True


class ProviderConfig(BaseModel):
    """单个 Provider 的配置

    支持通过 copy 字段继承其他 provider 的配置：
    - copy: 指定要继承的 provider 名称
    - 当前 provider 的其他字段会覆盖被继承的配置
    - 类似 class 继承机制

    示例：
        "z-glm4": {"clone": "z-glm5", "model": "glm-4"}
        表示 z-glm4 除了 model 用 glm-4，其他配置和 z-glm5 一样

    注意：JSON配置文件中使用 "clone" 字段名。
    """

    model_config = {"populate_by_name": True}

    # 合法的思考模式值（全局与 provider 级别共用）
    # auto: 自动（发现过度思考时自动禁思考/降级）
    # enabled/high: 开启思考（high=增强推理）；disabled: 关闭思考
    # 推理档位（从轻到重）: low < medium < high < xhigh < max
    # （GLM 风格支持 low/high/max；Qwen3/vLLM 风格支持 xhigh/medium/low）
    _THINKING_MODES = ("auto", "enabled", "disabled", "low", "medium", "high", "xhigh", "max")

    clone: str | None = None  # 继承其他 provider 的配置名称（类似 class 继承）
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    small_model: str = ""
    max_tokens: int | None = None  # 最大输出token数，None时使用全局配置
    thinking_mode: str = "auto"  # auto, enabled, disabled, low, high, max
    thinking_dead_loop_threshold: int | None = None  # 思考死循环检测阈值，None时使用全局配置
    content_dead_loop_threshold: int | None = None  # 内容死循环检测阈值，None时使用全局配置
    thinking_type: str | None = None  # "thinking" | "chat_template_kwargs" | None（None=自动检测）
    # 推理强度档位（reasoning_effort: low/medium/high/xhigh/max），
    # 适用于不支持禁用思考的模型（如 GLM-5.3）及支持档位的 Qwen3/vLLM 部署（如 s-qwen3.8）
    # None=不设置（使用API默认档位）；仅在 thinking_type="thinking" 风格下生效
    reasoning_effort: str | None = None  # enabled/auto 模式下的推理强度
    # 模型不支持禁用思考（如 GLM-5.3 仅支持 enabled+reasoning_effort）：
    # thinking_mode=disabled 时降级为 enabled + 此档位推理强度（通常配 "low"）
    # None/空=支持真正的禁用（发送 thinking.type=disabled）
    reasoning_disabled_fallback: str | None = (
        None  # 禁思考降级档位（low/medium/high/xhigh/max），None=真禁用
    )
    thinking_persistence: bool = (
        True  # 是否支持思考内容持久化（GLM等不复用history中的reasoning_content，需写入文件）
    )
    # 是否在多轮请求与history持久化中保留 reasoning_content（qwen 系列）
    # 实机验证（事务#2111）：swift/vLLM/llama.cpp 等后端会把 assistant 消息中的
    # reasoning_content 渲染回 prompt（模型可复用上轮思考，prompt_tokens 相应增加）。
    # 默认 False=序列化时剥离思考内容以节省 token；True=保留（并合并进相邻
    # assistant 消息，独立空 content 的 thinking 消息后端读不到）
    preserve_thinking: bool = False
    compact_threshold: int | None = None  # 上下文压缩阈值，None时使用全局compact_threshold
    # 费用配置（元/百万tokens），None时使用全局默认值(0)
    price_input_hit: float | None = None  # 命中缓存的输入价格
    price_input_miss: float | None = None  # 未命中缓存的输入价格
    price_output: float | None = None  # 输出价格
    vision: str = "disabled"  # 是否支持视觉: "true"/"on"/"yes"/"enabled"=支持, "false"/"off"/"no"/"disabled"=不支持, "only"=仅vision不支持普通LLM
    enable: bool = True  # 是否启用该provider
    icon: str = "🤖"  # Provider 图标（emoji机器人）
    priority: int = 50  # Provider 优先级（0-99，默认50，越高优先级越高）
    max_concurrent: int | None = None  # 该provider最大并发数，None时使用全局max_concurrent

    # 🔧 事务#2114：工具结果compact清理提示阈值（字节）。超过此字节数的工具返回结果
    # 末尾追加带唯一4位数字id的compact清理提示，模型确认结果毫无用处时可用compact工具清理。
    # None时使用全局 llm.compact_hint_threshold（默认1024）；0=禁用该provider的提示
    compact_hint_threshold: int | None = None

    # 采样参数覆盖（按模型作者要求，部分模型在不同thinking模式下需要不同采样参数）
    # 格式: {"thinking_on": {"temperature": 0.6, "top_p": 0.95}, "thinking_off": {"temperature": 0.001, "top_p": 1.0}}
    # None时使用全局 temperature
    sampling_params: dict | None = None

    # 重复惩罚参数（防止模型输出重复/死循环内容）
    # None 时使用全局 LLMConfig 中的配置
    # OpenAI API 标准：-2.0 ~ 2.0，正值降低重复 token 的生成概率
    frequency_penalty: float | None = None  # 频率惩罚：已出现token越多惩罚越大
    presence_penalty: float | None = None  # 存在惩罚：只要出现过就惩罚

    # 过思考检测阈值（行首 "wait/等等" 命中次数达到此值触发干预）
    # None 时使用全局 LLMConfig.overthinking_threshold
    overthinking_threshold: int | None = None

    # 思考退化循环检测：正则表达式列表，匹配thinking行时强制停止思考
    # None 时使用全局 LLMConfig.degenerate_loop_patterns
    degenerate_loop_patterns: list[str] | None = None

    # 纯符号无效输出检测阈值：thinking/content中连续N个纯符号字符（无字母无中文）判定无效输出
    # None 时使用全局 LLMConfig.symbol_output_threshold
    symbol_output_threshold: int | None = None

    # 重复词语无效输出检测阈值：thinking/content中连续N个完全相同的英文单词或中文词语判定无效输出
    # None 时使用全局 LLMConfig.repeated_word_threshold
    repeated_word_threshold: int | None = None

    # 重复短语无效输出检测：连续N个完全相同字符串（≤此长度字符，须含字母/中文）判定无效输出
    # 阈值N用thinking/content_dead_loop_threshold；None时使用全局 LLMConfig.repeated_phrase_maxlen
    repeated_phrase_maxlen: int | None = None

    # 每 N 轮迭代注入专家身份提醒（强化模型身份认知，防止长对话中"忘记"自身能力定位）
    # None 时使用全局 LLMConfig.pua_threshold
    pua_threshold: int | None = None

    def is_vision_enabled(self) -> bool:
        return self.vision.lower() in ("true", "on,", "yes", "enabled", "only")

    def is_vision_only(self) -> bool:
        """是否仅支持 vision（不支持普通 LLM 模式）"""
        return self.vision.lower() == "only"

    def model_post_init(self, __context) -> None:
        """校验 thinking 相关配置的合法性"""
        if self.thinking_mode not in self._THINKING_MODES:
            raise ValueError(
                f"thinking_mode 配置错误: '{self.thinking_mode}'，"
                f"合法值: {', '.join(self._THINKING_MODES)}"
            )
        _EFFORTS = ("low", "medium", "high", "xhigh", "max")
        if self.reasoning_effort is not None and self.reasoning_effort not in _EFFORTS:
            raise ValueError(
                f"reasoning_effort 配置错误: '{self.reasoning_effort}'，合法值: {', '.join(_EFFORTS)}"
            )
        if (
            self.reasoning_disabled_fallback is not None
            and self.reasoning_disabled_fallback not in _EFFORTS
        ):
            raise ValueError(
                f"reasoning_disabled_fallback 配置错误: '{self.reasoning_disabled_fallback}'，"
                f"合法值: {', '.join(_EFFORTS)}"
            )


class LLMPromptsConfig(BaseModel):
    """发送给大模型的提示字符串（可通过配置文件实时修改）

    所有发送给大模型的提示文本统一在此配置，
    config文件监控机制（mtime检测）可实时发现变更并立刻启用。
    带有 {变量} 占位符的模板会在使用时通过 .format() 填充。
    """

    # 每N轮迭代注入的专家身份提醒（PUA）
    pua_text: str = (
        "你汇聚了人类所有专业知识的超级专家大模型，你有无限的精力，"
        "因此你对于任何问题都会采用人类目前最先进的方案解决！ 你追求的的必须是最全面最优化的方法，而不是最简单的方法！"
        "你会纵观全局、充分规划从根本上尽善尽美地解决问题，而不是偷懒、掩盖、绕过或敷衍了事！"
        "你会秉承小心谨慎的原则，变更代码前充分搜集信息、论证分析、制定方案、更新完善事项清单checklist，然后坚定不移、按部就班地执行事项清单checklist， 每项变更后充分验证确认正确性。"
    )

    # 精神分裂式思考检测干预（wait/等等 反复推翻自己）
    overthinking_warning: str = (
        "立刻停止反复用“wait/等等”之类固定模式推翻自己！"
        "这是被无脑训练出来的精神分裂式思考，不是真正的分析！"
        "立刻选取当前最确定的方向，调用工具开始实际解决问题—— "
        "即使方向不完全正确，工具返回的信息也能帮你进一步分析。继续空想毫无意义！"
    )

    # 思考死循环干预（同一点反复思考）
    thinking_dead_loop_intervention: str = (
        "停止反复思考同一个问题！果断选取当前最确定的方向，"
        "立刻调用工具开始实际解决问题，这样就算失败，"
        "也可以获得更多关联信息用于进一步分析！"
    )

    # 内容死循环干预（content流中无限重复同一句话）
    content_dead_loop_intervention: str = (
        "⚠️ 你的输出出现了严重的内容重复死循环！"
        "你刚才在输出中无限重复了同一句话。"
        "这可能是模型退化或上下文过长导致的。"
        "请直接跳过重复内容，立刻调用工具执行下一步操作！"
        "如果仍然重复，系统将强制重启事务。"
    )

    # 思考退化循环干预（模型自行检测到degenerate loop但无法退出）
    degenerate_loop_intervention: str = (
        "⚠️ 你已检测到自身陷入退化循环！立即停止当前思考方向！"
        "换用完全不同的策略：直接调用工具获取新信息或执行具体操作。"
        "禁止继续在当前方向上推理！"
    )

    # 纯符号无效输出干预（thinking/content连续输出大量纯符号字符，无字母无中文）
    symbol_output_intervention: str = (
        "⚠️ 你的输出被检测为无效输出（连续输出大量纯符号字符，无任何字母或中文文字）！"
        "该输出已被系统丢弃。请停止输出符号串，"
        "立即用正常的中英文语言思考或回复，并调用工具执行实际操作！"
    )

    # 重复词语无效输出干预（thinking/content连续输出N个完全相同的英文单词或中文词语）
    repeated_word_intervention: str = (
        "⚠️ 你的输出被检测为无效输出（连续重复输出相同的词语）！"
        "最后2行无效输出已被系统丢弃。请停止复读，"
        "立即用正常的中英文语言继续思考或回复，并调用工具执行实际操作！"
    )

    # 死循环干预提示池（随机选取，差异大、具体可操作）
    dead_loop_hints: list[str] = Field(
        default_factory=lambda: [
            "⚠️  你似乎陷入了死循环！执行 git diff 对照 checklist 核对当前实际变更和进展！",
        ]
    )

    # thinking内容截断提示（{truncated_count} 为被截断的行数）
    thinking_truncated: str = "[已截断前 {truncated_count} 行思考内容...]"

    # 上下文紧张提示（{timestamp} {task_id} {current_kb} {threshold_kb} {progress} 为变量）
    context_too_large: str = (
        "⚠️  [{timestamp}] 事务#{task_id}当前上下文紧张({current_kb}K/{threshold_kb}K)。\n"
        "大模型降智严重，且进展缓慢(进度记录 {progress}%)，把已完成事项，需要继续处理的事项，当前上下文内所有对继续处理的事项有用的信息，以及当前进展，相关代码位置和其他新发现的重要信息，记录到 checklist。然后用工具调用“task restart {task_id}”（禁止添加--force）重启本事务。"
    )

    # 即将达到迭代限制警告（{iteration} {max_iterations} 为变量）
    iteration_limit_warning: str = (
        "⚠️  @compact! 将达到系统迭代限制（{iteration}/{max_iterations}），"
        "请尽快结束本轮对话，避免强制中断。"
    )

    # 达到最大迭代强制结束（{max_iterations} 为变量）
    iteration_exhausted: str = " @compact! 达到最大迭代次数 {max_iterations}，强制结束对话循环"

    # 🔧 事务#2114：思考后无正式内容提醒——模型只输出思考内容（无content无tool_calls）
    # 时注入，提醒模型思考内容不会保留在后续上下文中（默认被剥离），必须输出正式内容
    thinking_only_reminder: str = (
        "思考内容不会保留在上下文中，因此思考后必须输出正式内容总结，以免丢失思考成果！"
    )

    # 🔧 事务#2114：超长工具结果的compact清理提示（{compact_id} 为唯一4位数字ID）。
    # 工具返回结果字节数超过 compact_hint_threshold（provider级可配，默认1024）时
    # 追加此提示，模型确认结果毫无用处时调用compact工具清理以节省上下文空间。
    # 🔧 事务#2521：{size} 为该结果的字符数，提示模型权衡精简收益
    compact_hint: str = "\n 返回结果较长（{size}字符），如果无用或可以用总结大幅精简，请用compact工具清理或精简，id {compact_id}，尽量节省上下文空间！"


class LLMConfig(BaseModel):
    """LLM 配置（只通过 providers 配置）"""

    # 当前使用的 provider 短名称
    current_provider: str = ""

    # Provider 轮换列表（当当前 provider 不可用时自动切换）
    shifts: list[str] = Field(default_factory=list)

    # 通用配置（所有 provider 共享）
    thinking_mode: str = (
        "auto"  # 全局思考模式（auto/enabled/disabled/low/high/max），覆盖 provider 级别配置
    )
    thinking_dead_loop_threshold: int = 10  # 全局思考死循环检测阈值
    content_dead_loop_threshold: int = 8  # 全局内容死循环检测阈值（content流中重复行检测）
    temperature: float = 0.5
    max_tokens: int = 80000
    thinking_budget: int = 0  # 全局思考预算，限制推理过程的最大Token数（0=不限制）

    # 重复惩罚参数（防止模型输出重复/死循环内容）
    # OpenAI API 标准：-2.0 ~ 2.0，正值降低重复 token 的生成概率
    frequency_penalty: float = 0.0  # 全局频率惩罚（provider级别可覆盖）
    presence_penalty: float = 0.0  # 全局存在惩罚（provider级别可覆盖）

    compact_threshold: int = 300000
    # 费用配置全局默认值（元/百万tokens）
    price_input_hit: float = 0.0  # 命中缓存的输入价格
    price_input_miss: float = 0.0  # 未命中缓存的输入价格
    price_output: float = 0.0  # 输出价格
    timeout: float = 120.0
    # HTTP 超时配置（细粒度控制）
    timeout_connect: float = 30.0  # 连接超时
    timeout_read: float = 30.0  # 读取超时
    timeout_write: float = 30.0  # 写入超时
    timeout_pool: float = 30.0  # 连接池超时
    # 并发控制
    max_concurrent: int = 5  # 大模型并发数
    small_model_max_concurrent: int = 200  # 小模型并发数
    # 🔧 事务#2114：工具结果compact清理提示阈值（字节，全局默认，provider级可覆盖）。
    # 超过此字节数的工具返回结果末尾追加compact清理提示（见llm.prompts.compact_hint）；
    # 0=禁用
    compact_hint_threshold: int = 1024
    # 🔧 事务#2521：compact可精简窗口=最近N批工具结果所在的迭代（默认3，peng.json可配）。
    # 1=仅上一轮迭代（原始行为）；窗口覆盖模型视角最近N批工具结果所在迭代，
    # 中间无工具的空转迭代不顶掉批次；更早批次禁止精简（保护上下文前缀缓存）
    compact_window_iterations: int = 3
    # 向后兼容：顶层 model 字段（将从 current_provider 获取）
    model: str = ""
    # 向后兼容：顶层 model 字段（将从 current_provider 获取）
    small_model: str = ""

    # 多 Provider 配置（必须配置）
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)

    # 失败 provider 跟踪（provider_name -> FailureInfo）
    # 存储失败次数、时间戳、当前冷却时间，支持自适应冷却
    failed_providers: dict[str, Any] = Field(default_factory=dict)

    # 每 N 轮迭代注入专家身份提醒（默认5轮，provider级别可覆盖）
    pua_threshold: int = 5

    # 默认冷却时间（秒）
    default_cooldown: int = 30  # 默认 30 秒
    max_cooldown: int = 3600  # 最大冷却时间 1 小时

    # 系统报错去重：检测最近N轮是否有类似报错，有则不重复报告（避免上下文被重复报错撑爆）
    error_dedup_rounds: int = 5

    # 精神分裂式思考检测：单次思考中行首 "wait/等等" 命中次数达到此值时触发干预
    overthinking_threshold: int = 3

    # 思考退化循环检测：正则表达式列表，thinking行匹配任一pattern时强制停止思考继续迭代
    # 模型（如s-qwen3.8）能自己检测到退化循环并输出"degenerate <xx> loop"，但无法自行退出
    # 空列表=禁用此检测
    degenerate_loop_patterns: list[str] = Field(
        default_factory=lambda: [r"degenerate\s+\S+\s+loop"]
    )

    # 纯符号无效输出检测：thinking/content中连续输出N个纯符号字符（无字母、无中文）时
    # 判定为无效输出（模型退化产生的符号垃圾流），丢弃本次输出并提醒模型继续迭代。
    # 字符分类：str.isalpha()（含中英文等所有语言字母）、数字、下划线视为有效内容并重置计数。
    # 正常代码/JSON输出（如 "{{{...}}}" 大括号配对）通常几十个符号内必混有字母，
    # 连续500+纯符号在正常输出中几乎不可能出现。0=禁用此检测
    symbol_output_threshold: int = 500

    # 重复词语无效输出检测：thinking/content中连续输出N个完全相同的英文单词或中文词语（含单字）时
    # 判定为无效输出（模型退化产生的复读机模式），丢弃最后2行输出并提醒模型继续迭代。
    # 检测对象：英文单词（如 "the the the the the the"）、中文单字（如 "的的的的的的"）、
    # 中文短词组（如 "好的好的好的好的好的好的"）。0=禁用此检测
    repeated_word_threshold: int = 6

    # 重复短语无效输出检测：thinking/content中连续输出N个完全相同的字符串（英文短语或
    # 中文词语组成，长度2~repeated_phrase_maxlen字符，须含字母或中文）时判定无效输出。
    # 检测如"these are all independent so I'll batch them together now — "×19
    # 这类单词级/行级检测均漏掉的短语级复读（事务#1599漏检场景）。
    # 阈值N：thinking流用thinking_dead_loop_threshold，content流用content_dead_loop_threshold。
    # 0=禁用此检测（256=推荐上限，防止单次比较开销与误报长文本）
    repeated_phrase_maxlen: int = 256

    # 思考过载检测：跨迭代连续思考字节数超过此阈值且未调用write/edit/line_edit时，
    # 强制关闭思考模式（仅影响当前事务，不修改全局配置）。
    # 一旦调用了write/edit/line_edit进行有效工作，重置计数器并恢复思考模式。
    # 单位：字节，0=禁用此功能
    thinking_overload_threshold: int = 81920  # 80K bytes

    # 自动降级开关（当当前 provider 失败时，是否自动切换到下一个 provider）
    enable_fallback: bool = True

    # 同优先级 provider 轮换开关（事务#2307，消息#10863）：
    # True=优先级相同的多个provider按空闲度（跨进程分配账本）+LRU轮换排序，
    # 新启动的事务优先选择比较空闲的provider；False=同tier保持配置顺序（旧行为）。
    # 仅影响"自动选择"路径；显式 @provider / 锁定 / 粘性（resume）不受影响。
    provider_rotation: bool = True

    # 允许锁定provider强制使用未启用的provider（不改变启用状态）
    allow_locked_disabled: bool = True

    # 发送给大模型的提示字符串（可通过配置文件实时修改）
    prompts: LLMPromptsConfig = Field(default_factory=LLMPromptsConfig)

    def _sort_providers_by_priority(
        self, names: list[str], *, balanced: bool = True, task_id: int | None = None
    ) -> list[str]:
        """按priority降序排列，**同priority tier内按空闲度轮换排序**（事务#2307）

        需求（消息#10863）：优先级相同的多个provider应该轮流使用，新启动的事务
        优先选择比较空闲的provider。跨进程空闲度/LRU数据来自
        ``peng.llm.provider_balance``（每进程账本文件聚合，无共享写争用）。

        Args:
            names: 待排序provider名称列表
            balanced: False=纯priority排序（同tier保持传入顺序，旧行为）
            task_id: 本事务id，用于"本进程已分配给自己的provider排最前"

        Returns:
            排序后的provider名称列表（tier间priority降序，tier内空闲优先+LRU）
        """
        if not names:
            return []
        tiers: dict[int, list[str]] = {}
        for name in names:
            prov_config = self.providers.get(name)
            priority = prov_config.priority if prov_config else 0
            tiers.setdefault(priority, []).append(name)
        result: list[str] = []
        for priority in sorted(tiers.keys(), reverse=True):
            tier = tiers[priority]
            if balanced and self.provider_rotation and len(tier) > 1:
                try:
                    from peng.llm.provider_balance import get_provider_balance

                    tier = get_provider_balance().rank(tier, task_id=task_id)
                except Exception as e:  # 账本异常不影响选择（退化为原顺序）
                    logger.debug(f"[LLMConfig] provider轮换排序失败(退化原顺序): {e}")
            result.extend(tier)
        return result

    def get_shifts(
        self, *, balanced: bool = True, task_id: int | None = None
    ) -> list[str]:
        """获取轮换列表（自动从 providers 中获取所有启用的且非vision-only的，按priority降序排序）

        不再需要手动配置 shifts 列表，自动从 llm.providers 中获取所有 enable=true 且 vision!="only" 的 provider。
        排序规则：priority 越高，优先级越高，排在越前面；**同priority的provider
        按空闲度轮换排序**（事务#2307，开关 llm.provider_rotation）。
        """
        enabled_providers = [
            p
            for p, config in self.providers.items()
            if config.enable and not config.is_vision_only()
        ]
        return self._sort_providers_by_priority(
            enabled_providers, balanced=balanced, task_id=task_id
        )

    def _get_enabled_providers_sorted(
        self, *, balanced: bool = True, task_id: int | None = None
    ) -> list[str]:
        """
        获取所有启用的provider列表（排除vision-only），按priority降序排序

        Returns:
            启用的provider名称列表（不含vision-only），priority越高越靠前；
            同priority按空闲度轮换排序（事务#2307）
        """
        enabled_providers = [
            p
            for p, config in self.providers.items()
            if config.enable and not config.is_vision_only()
        ]
        return self._sort_providers_by_priority(
            enabled_providers, balanced=balanced, task_id=task_id
        )

    def _note_auto_assignment(self, provider: str, task_id: int | None) -> str:
        """自动选择命中后推进跨进程LRU账本（事务#2307）

        仅自动选择路径推进 LRU（新分配）；显式 @provider / 锁定 / 粘性（resume）
        的归属不推进——它们不改变归属，且 DB tasks 聚合已把它们计入活跃数
        （chat_stream 每轮把实际 provider 写回 task.provider），空闲度天然完整。
        """
        if provider and task_id is not None and self.provider_rotation:
            try:
                from peng.llm.provider_balance import get_provider_balance

                get_provider_balance().record_assignment(provider, task_id=task_id)
            except Exception as e:  # 账本失败不影响选择
                logger.debug(f"[LLMConfig] provider分配登记失败(忽略): {e}")
        return provider

    def _get_best_available_provider(
        self,
        forbidden: list[str] | None = None,
        prefer_vision: bool = False,
        min_context_tokens: int | None = None,
        task_id: int | None = None,
    ) -> str | None:
        """
        获取当前最佳可用 provider（优先级最高且不在冷却中的）

        从 shifts 列表（已按 priority 降序排列）中找到第一个不在冷却中的 provider。
        如果所有 provider 都在冷却中，返回第一个（priority 最高的）。
        **同优先级 tier 内按空闲度+LRU 轮换排序**（事务#2307，
        开关 llm.provider_rotation），使优先级相同的多个 provider 轮流使用。

        Args:
            forbidden: 禁止使用的provider列表，这些provider会被跳过
            prefer_vision: 优先选择支持多模态的provider（vision=true/yes/on/enabled）。
                事务包含多模态文件引用（@file=/webui附件）时传True：
                先在支持vision的provider中按priority选最优；无可用vision
                provider时回退普通选择（多模态内容会被降级为纯文本）。
                vision-only 的provider不参与（它们不支持普通LLM任务）。
            min_context_tokens: 估算的请求输入token数（事务#1807）。
                上下文能力装不下当前请求的provider被跳过，防止切换后必然400。
            task_id: 事务id（事务#2307）。传入时同tier按空闲度（DB活跃事务数）
                轮换排序并登记分配。

        Returns:
            最佳可用 provider 名称，如果没有可用 provider 则返回 None
        """
        shifts = self.get_shifts(task_id=task_id)
        if not shifts:
            return None

        # 多模态优先：在支持vision的provider中按priority选最优（不含vision-only）
        # 🔧 事务#1819：运行期vision禁用的provider不参与vision筛选
        if prefer_vision:
            vision_shifts = [
                p
                for p in shifts
                if self.providers[p].is_vision_enabled()
                and not self.providers[p].is_vision_only()
                and not is_provider_vision_runtime_disabled(p)
            ]
            if vision_shifts:
                candidates = vision_shifts
            else:
                logger.warning("[LLMConfig] 无可用vision provider，多模态内容将降级为纯文本")
                candidates = shifts
        else:
            candidates = shifts

        # 上下文能力过滤（事务#1807）：装不下当前请求的provider不参与选择
        if min_context_tokens is not None:
            candidates = [p for p in candidates if self._context_capacity_ok(p, min_context_tokens)]
            if not candidates:
                logger.warning(
                    f"[LLMConfig] 所有provider上下文能力均不足"
                    f"(请求输入~{min_context_tokens}tokens)，无能力匹配的provider（事务#1807）"
                )
                return None

        # 将 forbidden 转换为集合以加速查找
        forbidden_set = set(forbidden) if forbidden else set()

        current_time = time.time()
        for candidate in candidates:
            # 跳过被禁止的provider
            if candidate in forbidden_set:
                continue
            failure_info = self._get_failure_info(candidate)
            if not failure_info:
                return self._note_auto_assignment(candidate, task_id)
            # 检查冷却时间是否已过
            if current_time - failure_info.last_failure_time > failure_info.cooldown:
                return self._note_auto_assignment(candidate, task_id)

        # 所有 provider 都在冷却中，选择冷却时间最短的（而非固定返回第一个）
        best_candidate = None
        best_remaining_cooldown = float("inf")
        for candidate in candidates:
            # 跳过被禁止的provider
            if candidate in forbidden_set:
                continue
            failure_info = self._get_failure_info(candidate)
            if failure_info:
                remaining = failure_info.cooldown - (current_time - failure_info.last_failure_time)
                if remaining < best_remaining_cooldown:
                    best_remaining_cooldown = remaining
                    best_candidate = candidate
            else:
                best_candidate = candidate
                break
        logger.warning(
            f"[LLMConfig] 所有 provider 都在冷却中，使用冷却时间最短的: {best_candidate}"
        )
        return self._note_auto_assignment(best_candidate, task_id)

    def get_min_cooldown_remaining(self) -> float:
        """获取所有provider中最短的剩余冷却时间（秒）

        用于计算"所有provider都在冷却中"时需要等待多久。
        如果没有任何provider在冷却中，返回0。

        Returns:
            最短剩余冷却时间（秒），向上取整
        """
        shifts = self.get_shifts()
        if not shifts:
            return 0.0

        current_time = time.time()
        min_remaining = float("inf")
        for candidate in shifts:
            failure_info = self._get_failure_info(candidate)
            if not failure_info:
                return 0.0  # 有provider可用，无需等待
            remaining = failure_info.cooldown - (current_time - failure_info.last_failure_time)
            if remaining < min_remaining:
                min_remaining = remaining

        if min_remaining < 0:
            return 0.0
        return min_remaining

    def _effective_compact_threshold(self, provider: str) -> int:
        """获取provider的有效上下文压缩阈值（provider级优先，None时用全局值）

        兼作该provider上下文处理能力的判据：上下文超过此值的provider不应被切换选中
        （切换过去请求会超限400，且立即触发上下文压缩告警，如事务#1805）。
        """
        prov_config = self.providers.get(provider)
        if prov_config and prov_config.compact_threshold is not None:
            return prov_config.compact_threshold
        return self.compact_threshold

    def _context_capacity_ok(self, provider: str, min_context_tokens: int | None) -> bool:
        """校验provider的上下文能力是否装得下当前请求（事务#1807）

        准据与系统自身阈值语义一致（check_progress_and_compact同款）：
        估算输入token（含安全余量）≤ 有效compact_threshold 即能力足够。
        不额外计入max_tokens输出预留——compact_threshold是用户按模型真实
        上限配置的软阈值，若加输出预留会把正在正常服务、上下文接近阈值的
        provider也误判为不足，反而制造无谓切换（破坏前缀缓存/优先级意图）。
        #1805实况验证：输入243K*1.05=255K > s-qwen3.8的220K即正确跳过。

        Args:
            provider: provider名称
            min_context_tokens: 估算的请求输入token数（None=不校验，保持旧行为）
        """
        if min_context_tokens is None:
            return True
        capacity = self._effective_compact_threshold(provider)
        if min_context_tokens > capacity:
            logger.info(
                f"[LLMConfig] {provider}上下文能力不足: 请求输入~{min_context_tokens}tokens"
                f" > compact_threshold {capacity}，跳过该provider（事务#1807）"
            )
            return False
        return True

    def _get_failure_info(self, provider: str) -> FailureInfo | None:
        """获取 provider 的失败信息（自动转换为 FailureInfo）"""
        if provider not in self.failed_providers:
            return None

        data = self.failed_providers[provider]
        if isinstance(data, FailureInfo):
            return data
        if isinstance(data, dict):
            # 从 JSON 加载的字典，转换为 FailureInfo
            return FailureInfo.from_dict(data)
        return None

    def _set_failure_info(self, provider: str, info: FailureInfo) -> None:
        """设置 provider 的失败信息"""
        self.failed_providers[provider] = info

    def _clean_expired_failures(self) -> None:
        """清理过期的失败记录"""
        current_time = time.time()
        expired = []

        for provider in self.failed_providers:
            info = self._get_failure_info(provider)
            if info and (current_time - info.last_failure_time) > info.cooldown:
                expired.append(provider)

        for provider in expired:
            del self.failed_providers[provider]

    def _record_failure(self, provider: str) -> None:
        """
        记录 provider 失败（自适应冷却时间：失败加倍）

        冷却时间计算：
        - 首次失败：30 秒（default_cooldown）
        - 再次失败：冷却时间 * 2
        - 最大冷却时间：3600 秒（1 小时）
        """
        info = self._get_failure_info(provider)

        if info is None:
            # 首次失败
            info = FailureInfo(
                failure_count=1,
                last_failure_time=time.time(),
                cooldown=self.default_cooldown,
            )
        else:
            # 再次失败，冷却时间加倍
            info.failure_count += 1
            info.last_failure_time = time.time()
            info.cooldown = min(info.cooldown * 2, self.max_cooldown)

        self._set_failure_info(provider, info)

        # 立即持久化失败记录到磁盘（跨进程共享冷却状态）
        self.save_failed_providers()

    def get_icon(self, provider: str) -> str:
        v = self.providers.get(provider)
        if v:
            return v.icon
        return None

    def mark_success(self, provider: str) -> None:
        """
        标记 provider 成功（重置冷却时间）

        成功时：
        - 失败次数清零
        - 冷却时间重置为默认值（30 秒）
        - 保留记录但冷却时间过期后会自动清理
        """
        info = self._get_failure_info(provider)
        if info:
            # 重置为初始状态
            info.failure_count = 0
            info.cooldown = self.default_cooldown
            # 更新最后失败时间，让清理机制尽快移除
            info.last_failure_time = time.time() - info.cooldown - 1
            self._set_failure_info(provider, info)
            # 立即持久化成功记录到磁盘（跨进程共享冷却状态）
            self.save_failed_providers()

    def match_provider(self, provider: str, include_disabled: bool = False) -> str | None:
        """
        模糊匹配 provider

        Args:
            provider: 字符串（如 'glm', 'q', 'zhi'）
            include_disabled: 是否包含未启用的provider（用于@<provider>!锁定场景）

        Returns:
            匹配的 provider 名称，如果未找到则返回 None
        """
        if not provider:
            return None
        provider = provider.lower()
        if include_disabled:
            search_list = list(self.providers.keys())
        else:
            search_list = self._get_enabled_providers_sorted()

        # 首先尝试精确匹配
        for k in search_list:
            if k.lower() == provider:
                return k
        v: ProviderConfig
        # 尝试图标匹配
        for k in search_list:
            v = self.providers.get(k)
            if provider in v.icon or v.icon in provider:
                return k
        # 尝试provider名称匹配
        for k in search_list:
            if provider in k.lower():
                return k
        # 尝试模型名称匹配
        for k in search_list:
            v = self.providers.get(k)
            if provider in v.model.lower():
                return k
        return None

    def fetch_providers(self, content: str) -> tuple[list[str], bool, list[str]] | None:
        """
        从content 字符串识别@<provider>

        支持 @<provider名称> 和 @<emoji图标> 两种格式，
        emoji支持常见 Unicode emoji 范围（U+1F300-U+1FAFF, U+2600-U+27BF 等）。

        支持锁定语法：@<provider>! 表示永不切换provider（即使失败也不切换）
        支持禁止语法：@<provider>X 或 @<provider> X 表示此事务禁止使用此provider（只匹配大写X，小写x无效）

        Returns:
            (providers列表, locked标志, forbidden_providers列表) 或 None
            providers列表有序：带!锁定标记的provider排在最前（get_provider
            锁定时取favorites[0]，保证锁定语义指向用户带!指定的目标），
            其余按content中出现顺序排列
        """
        locked_list: list[str] = []  # 带!锁定标记的provider（按出现顺序）
        normal_list: list[str] = []  # 普通标记的provider（按出现顺序）
        locked = False
        forbidden = set([])
        if content:
            # emoji 匹配范围（覆盖常见 emoji）
            emoji_pat = r"[\U0001F1E0-\U0001F1FF\U0001F300-\U0001F9FF\U00002700-\U000027BF\U0001F600-\U0001F64F\U0001F680-\U0001F6FF\U00002600-\U000026FF\U0000FE00-\U0000FE0F\U0001FA00-\U0001FAFF]"
            # 匹配 @<provider>! 或 @<provider>！（! 支持半角!和全角！）
            # 同时支持 @<provider>X 或 @<provider> X 的禁止语法（只匹配大写X，小写x无效）
            results = re.findall(
                r"@([a-zA-Z0-9\.\-_]+[!\uff01]*|" + emoji_pat + r"+[!\uff01]*)",
                content,
                re.IGNORECASE,
            )
            for r in results:
                c = r
                # 检测锁定标记（兼容半角!和全角！）
                is_locked = c.endswith("!") or c.endswith("\uff01")
                if is_locked:
                    locked = True
                    c = c.rstrip("!\uff01")
                # 带!锁定标记时搜索全部provider（含未启用），否则只搜已启用
                p = self.match_provider(c, include_disabled=is_locked)
                if p:
                    if is_locked and p not in locked_list:
                        locked_list.append(p)
                    elif not is_locked and p not in normal_list and p not in locked_list:
                        normal_list.append(p)
            # 检测禁止语法：@<provider>X 或 @<provider> X（只匹配大写X，小写x无效）
            # 同时支持 @<emoji>X 的禁止语法
            forbidden_results = re.findall(
                r"@([a-zA-Z0-9\.\-_]+\s*X|" + emoji_pat + r"+\s*X)", content
            )
            for r in forbidden_results:
                c = r.rstrip(" X")
                p = self.match_provider(c, include_disabled=True)
                if p:
                    forbidden.add(p)
        providers = locked_list + normal_list
        return (providers, locked, list(forbidden)) if providers or locked or forbidden else None

    def get_provider(
        self,
        current: str | None = None,
        favorites: list[str] | None = None,
        failed: bool = False,
        task_id: int | None = None,
        locked: bool = False,
        forbidden: list[str] | None = None,
        prefer_vision: bool = False,
        min_context_tokens: int | None = None,
    ) -> dict[str, any] | None:
        """
        获取 provider 配置（统一入口）

        Args:
            failed: 是否标记当前 provider 为失败并切换到下一个
            locked: 锁定provider（来自 @<provider>! 语法），即使失败也不切换
            forbidden: 禁止使用的provider列表（来自 @<provider>X 语法），此事务中不使用这些provider
            prefer_vision: 优先使用支持多模态的provider（事务包含@file=/webui附件时True）
            min_context_tokens: 估算的请求输入token数（事务#1807）。传入时上下文
                能力装不下当前请求（输入token > 有效compact_threshold）的provider
                不会被切换选中，防止切换后必然400超限；全部不足时兜底返回能力
                最大的provider走既有400→冷却→上下文紧张→压缩/重启自愈链路。
                锁定(@provider!)与enable_fallback=False场景尊重用户显式意图，仅告警。
        """
        import time

        # provider锁定：即使失败也不切换，直接返回锁定的provider配置
        if locked:
            # 确定锁定的provider名称：优先favorites（用户在事务content中显式@指定的
            # provider，带!锁定标记的排在favorites最前），其次current（client实例
            # 上一轮使用的provider，可能是首轮预热按priority自动选择的记录，
            # 非用户显式意图），最后已保存的current_provider（排除被禁止的）
            forbidden_set = set(forbidden) if forbidden else set()
            locked_provider = None
            if favorites:
                for fav in favorites:
                    if fav not in forbidden_set:
                        matched = self.match_provider(fav, include_disabled=True)
                        if matched and matched not in forbidden_set:
                            locked_provider = matched
                            break
            if not locked_provider and current and current in self.providers:
                if current not in forbidden_set:
                    locked_provider = current
            # 还没有，尝试使用已保存的 current_provider（排除被禁止的）
            if (
                not locked_provider
                and self.current_provider
                and self.current_provider not in forbidden_set
            ):
                locked_provider = self.current_provider

            if locked_provider and locked_provider in self.providers:
                prov_config = self.providers[locked_provider]
                # 检查是否允许使用未启用的provider
                if not prov_config.enable and not self.allow_locked_disabled:
                    logger.warning(
                        f"[LLMConfig] provider已锁定但未启用且allow_locked_disabled=False: {locked_provider}，忽略锁定"
                    )
                else:
                    # 锁定provider强制使用，不改变启用状态
                    self.current_provider = locked_provider
                    logger.info(
                        f"[LLMConfig] provider已锁定: {locked_provider} (enable={prov_config.enable})"
                    )
                    # 上下文能力校验（事务#1807）：锁定是用户显式意图不强切，仅告警
                    if not self._context_capacity_ok(locked_provider, min_context_tokens):
                        logger.warning(
                            f"[LLMConfig] 锁定provider {locked_provider} 上下文能力不足"
                            f"(请求输入~{min_context_tokens}tokens)，仍按锁定使用，"
                            f"请求可能上下文超限400（事务#1807）"
                        )
                    return {
                        "provider": locked_provider,
                        "api_key": prov_config.api_key,
                        "base_url": prov_config.base_url,
                        "model": prov_config.model,
                        "small_model": prov_config.small_model,
                        # 🔧 事务#1819：运行期vision禁用时返回disabled（附件将降级发送）
                        "vision": prov_config.vision
                        if not is_provider_vision_runtime_disabled(locked_provider)
                        else "disabled",
                        "thinking_mode": prov_config.thinking_mode
                        if prov_config.thinking_mode != "auto"
                        else self.thinking_mode,
                        "thinking_dead_loop_threshold": prov_config.thinking_dead_loop_threshold
                        if prov_config.thinking_dead_loop_threshold is not None
                        else self.thinking_dead_loop_threshold,
                        "content_dead_loop_threshold": prov_config.content_dead_loop_threshold
                        if prov_config.content_dead_loop_threshold is not None
                        else self.content_dead_loop_threshold,
                        "thinking_type": prov_config.thinking_type,
                        "reasoning_effort": prov_config.reasoning_effort,
                        "reasoning_disabled_fallback": prov_config.reasoning_disabled_fallback,
                        "thinking_persistence": prov_config.thinking_persistence,
                        "preserve_thinking": prov_config.preserve_thinking,
                        "timeout": self.timeout,
                        "max_tokens": prov_config.max_tokens
                        if prov_config.max_tokens is not None
                        else self.max_tokens,
                        "temperature": self.temperature,
                        "sampling_params": prov_config.sampling_params,
                        "icon": prov_config.icon,
                        "compact_threshold": prov_config.compact_threshold
                        if prov_config.compact_threshold is not None
                        else self.compact_threshold,
                        "compact_hint_threshold": prov_config.compact_hint_threshold
                        if prov_config.compact_hint_threshold is not None
                        else self.compact_hint_threshold,
                        "price_input_hit": prov_config.price_input_hit
                        if prov_config.price_input_hit is not None
                        else self.price_input_hit,
                        "price_input_miss": prov_config.price_input_miss
                        if prov_config.price_input_miss is not None
                        else self.price_input_miss,
                        "price_output": prov_config.price_output
                        if prov_config.price_output is not None
                        else self.price_output,
                        "thinking_budget": self.thinking_budget,
                        "frequency_penalty": prov_config.frequency_penalty
                        if prov_config.frequency_penalty is not None
                        else self.frequency_penalty,
                        "presence_penalty": prov_config.presence_penalty
                        if prov_config.presence_penalty is not None
                        else self.presence_penalty,
                        "overthinking_threshold": prov_config.overthinking_threshold
                        if prov_config.overthinking_threshold is not None
                        else self.overthinking_threshold,
                        "degenerate_loop_patterns": prov_config.degenerate_loop_patterns
                        if prov_config.degenerate_loop_patterns is not None
                        else self.degenerate_loop_patterns,
                        "symbol_output_threshold": prov_config.symbol_output_threshold
                        if prov_config.symbol_output_threshold is not None
                        else self.symbol_output_threshold,
                        "repeated_word_threshold": prov_config.repeated_word_threshold
                        if prov_config.repeated_word_threshold is not None
                        else self.repeated_word_threshold,
                        "repeated_phrase_maxlen": prov_config.repeated_phrase_maxlen
                        if prov_config.repeated_phrase_maxlen is not None
                        else self.repeated_phrase_maxlen,
                        "pua_threshold": prov_config.pua_threshold
                        if prov_config.pua_threshold is not None
                        else self.pua_threshold,
                    }

        forbidden_set = set(forbidden) if forbidden else set()
        if not self.enable_fallback and self.current_provider:
            # 如果 current_provider 被禁止，即使 enable_fallback=False 也要切换
            if self.current_provider in forbidden_set:
                pass  # 进入后面的切换逻辑
            else:
                # 返回完整的 provider 字典
                prov_config = self.providers[self.current_provider]
                # 上下文能力校验（事务#1807）：enable_fallback=False为用户显式
                # 单provider模式不强切，仅告警
                if not self._context_capacity_ok(self.current_provider, min_context_tokens):
                    logger.warning(
                        f"[LLMConfig] enable_fallback=False，provider {self.current_provider} "
                        f"上下文能力不足(请求输入~{min_context_tokens}tokens)，"
                        f"仍按配置使用，请求可能上下文超限400（事务#1807）"
                    )
                return {
                    "provider": self.current_provider,
                    "api_key": prov_config.api_key,
                    "base_url": prov_config.base_url,
                    "model": prov_config.model,
                    "small_model": prov_config.small_model,
                    # 🔧 事务#1819：运行期vision禁用时返回disabled（附件将降级发送）
                    "vision": prov_config.vision
                    if not is_provider_vision_runtime_disabled(self.current_provider)
                    else "disabled",
                    "thinking_mode": prov_config.thinking_mode
                    if prov_config.thinking_mode != "auto"
                    else self.thinking_mode,
                    "thinking_dead_loop_threshold": prov_config.thinking_dead_loop_threshold
                    if prov_config.thinking_dead_loop_threshold is not None
                    else self.thinking_dead_loop_threshold,
                    "content_dead_loop_threshold": prov_config.content_dead_loop_threshold
                    if prov_config.content_dead_loop_threshold is not None
                    else self.content_dead_loop_threshold,
                    "thinking_type": prov_config.thinking_type,
                    "reasoning_effort": prov_config.reasoning_effort,
                    "reasoning_disabled_fallback": prov_config.reasoning_disabled_fallback,
                    "thinking_persistence": prov_config.thinking_persistence,
                    "preserve_thinking": prov_config.preserve_thinking,
                    "timeout": self.timeout,
                    "max_tokens": prov_config.max_tokens
                    if prov_config.max_tokens is not None
                    else self.max_tokens,
                    "temperature": self.temperature,
                    "sampling_params": prov_config.sampling_params,
                    "icon": prov_config.icon,
                    "compact_threshold": prov_config.compact_threshold
                    if prov_config.compact_threshold is not None
                    else self.compact_threshold,
                    "compact_hint_threshold": prov_config.compact_hint_threshold
                    if prov_config.compact_hint_threshold is not None
                    else self.compact_hint_threshold,
                    "price_input_hit": prov_config.price_input_hit
                    if prov_config.price_input_hit is not None
                    else self.price_input_hit,
                    "price_input_miss": prov_config.price_input_miss
                    if prov_config.price_input_miss is not None
                    else self.price_input_miss,
                    "price_output": prov_config.price_output
                    if prov_config.price_output is not None
                    else self.price_output,
                    "thinking_budget": self.thinking_budget,
                    "frequency_penalty": prov_config.frequency_penalty
                    if prov_config.frequency_penalty is not None
                    else self.frequency_penalty,
                    "presence_penalty": prov_config.presence_penalty
                    if prov_config.presence_penalty is not None
                    else self.presence_penalty,
                    "overthinking_threshold": prov_config.overthinking_threshold
                    if prov_config.overthinking_threshold is not None
                    else self.overthinking_threshold,
                    "degenerate_loop_patterns": prov_config.degenerate_loop_patterns
                    if prov_config.degenerate_loop_patterns is not None
                    else self.degenerate_loop_patterns,
                    "symbol_output_threshold": prov_config.symbol_output_threshold
                    if prov_config.symbol_output_threshold is not None
                    else self.symbol_output_threshold,
                    "repeated_word_threshold": prov_config.repeated_word_threshold
                    if prov_config.repeated_word_threshold is not None
                    else self.repeated_word_threshold,
                    "repeated_phrase_maxlen": prov_config.repeated_phrase_maxlen
                    if prov_config.repeated_phrase_maxlen is not None
                    else self.repeated_phrase_maxlen,
                    "pua_threshold": prov_config.pua_threshold
                    if prov_config.pua_threshold is not None
                    else self.pua_threshold,
                }

        # 清理过期的失败记录
        self._clean_expired_failures()

        # 处理失败标记
        if failed:
            # 记录失败（自适应冷却时间）
            if current and current in self.providers:
                self._record_failure(current)

        # 🔧 核心原则：没有出错就不切换！
        # 当 failed=False 且 current provider 存在且可用时，直接返回 current
        # 只有在以下情况才切换：
        # 1. failed=True（当前 provider 报错）
        # 2. current 为空（首次连接）
        # 3. current 不可用（未启用、在冷却中、或被禁止）
        # 4. prefer_vision=True 且 current 不支持多模态（切换到vision provider）
        # 5. favorites（事务content显式@指定的provider）存在且 current 不在其中
        forbidden_set = set(forbidden) if forbidden else set()
        favorites_set = set(favorites) if favorites else set()
        if not failed and current and current in self.providers and current not in forbidden_set:
            prov_config = self.providers[current]
            # 用户在事务content中显式@指定的provider（favorites）优先级最高：
            # current不在favorites中时不直接复用current（current可能只是首轮
            # 预热按priority自动选择的记录，非用户显式意图），落入下方切换逻辑
            if favorites_set and current not in favorites_set:
                logger.info(
                    f"[LLMConfig] 事务显式指定provider {sorted(favorites_set)}，"
                    f"不复用current {current}，切换到指定provider"
                )
            # 多模态优先：current不支持vision时切换到vision provider
            # （显式favorites/@provider指定的优先级高于prefer_vision，不覆盖）
            # 🔧 事务#1819：运行期vision禁用的provider同样视为不支持多模态
            elif (
                prefer_vision
                and not favorites
                and (
                    not prov_config.is_vision_enabled()
                    or prov_config.is_vision_only()
                    or is_provider_vision_runtime_disabled(current)
                )
            ):
                logger.info(
                    f"[LLMConfig] {current}不支持多模态，切换到vision provider处理多模态内容"
                )
            else:
                # 上下文能力校验（事务#1807）：current装不下当前请求时不复用
                # （config热改compact_threshold/上下文跨阈值增长等场景），落入切换逻辑
                if not self._context_capacity_ok(current, min_context_tokens):
                    logger.warning(
                        f"[LLMConfig] {current}上下文能力不足"
                        f"(请求输入~{min_context_tokens}tokens)，不复用current，"
                        f"切换到能力足够的provider（事务#1807）"
                    )
                # 检查 current provider 是否仍然可用（启用且非vision-only）
                elif prov_config.enable and not prov_config.is_vision_only():
                    failure_info = self._get_failure_info(current)
                    if not failure_info or (
                        time.time() - failure_info.last_failure_time > failure_info.cooldown
                    ):
                        # current provider 可用，直接使用它，不切换
                        self.current_provider = current
                        result = {
                            "provider": current,
                            "api_key": prov_config.api_key,
                            "base_url": prov_config.base_url,
                            "model": prov_config.model,
                            "small_model": prov_config.small_model,
                            # 🔧 事务#1819：运行期vision禁用时返回disabled（附件将降级发送）
                            "vision": prov_config.vision
                            if not is_provider_vision_runtime_disabled(current)
                            else "disabled",
                            "thinking_mode": prov_config.thinking_mode
                            if prov_config.thinking_mode != "auto"
                            else self.thinking_mode,
                            "thinking_type": prov_config.thinking_type,
                            "reasoning_effort": prov_config.reasoning_effort,
                            "reasoning_disabled_fallback": prov_config.reasoning_disabled_fallback,
                            "thinking_persistence": prov_config.thinking_persistence,
                            "preserve_thinking": prov_config.preserve_thinking,
                            "timeout": self.timeout,
                            "max_tokens": prov_config.max_tokens
                            if prov_config.max_tokens is not None
                            else self.max_tokens,
                            "temperature": self.temperature,
                            "sampling_params": prov_config.sampling_params,
                            "icon": prov_config.icon,
                            "compact_threshold": prov_config.compact_threshold
                            if prov_config.compact_threshold is not None
                            else self.compact_threshold,
                            "compact_hint_threshold": prov_config.compact_hint_threshold
                            if prov_config.compact_hint_threshold is not None
                            else self.compact_hint_threshold,
                            "price_input_hit": prov_config.price_input_hit
                            if prov_config.price_input_hit is not None
                            else self.price_input_hit,
                            "price_input_miss": prov_config.price_input_miss
                            if prov_config.price_input_miss is not None
                            else self.price_input_miss,
                            "price_output": prov_config.price_output
                            if prov_config.price_output is not None
                            else self.price_output,
                            "thinking_budget": self.thinking_budget,
                            "frequency_penalty": prov_config.frequency_penalty
                            if prov_config.frequency_penalty is not None
                            else self.frequency_penalty,
                            "overthinking_threshold": prov_config.overthinking_threshold
                            if prov_config.overthinking_threshold is not None
                            else self.overthinking_threshold,
                            "degenerate_loop_patterns": prov_config.degenerate_loop_patterns
                            if prov_config.degenerate_loop_patterns is not None
                            else self.degenerate_loop_patterns,
                            "symbol_output_threshold": prov_config.symbol_output_threshold
                            if prov_config.symbol_output_threshold is not None
                            else self.symbol_output_threshold,
                            "repeated_word_threshold": prov_config.repeated_word_threshold
                            if prov_config.repeated_word_threshold is not None
                            else self.repeated_word_threshold,
                            "repeated_phrase_maxlen": prov_config.repeated_phrase_maxlen
                            if prov_config.repeated_phrase_maxlen is not None
                            else self.repeated_phrase_maxlen,
                            "pua_threshold": prov_config.pua_threshold
                            if prov_config.pua_threshold is not None
                            else self.pua_threshold,
                            "presence_penalty": prov_config.presence_penalty
                            if prov_config.presence_penalty is not None
                            else self.presence_penalty,
                        }
                        return result
                    else:
                        logger.warning(f"[LLMConfig] {current}在冷却中，需要切换到其他provider")
                else:
                    if prov_config.is_vision_only():
                        logger.warning(
                            f"[LLMConfig] {current}是vision-only provider，需要切换到其他provider"
                        )
                    else:
                        logger.warning(f"[LLMConfig] {current}未启用，需要切换到其他provider")

        # 以下是需要切换 provider 的逻辑（failed=True 或 current 不可用或需要vision切换）
        # 确定要获取的 provider
        auto_selected = False  # 标记是否通过自动选择确定（已考虑冷却）
        if not favorites:
            # 优先使用 shifts 中第一个不在冷却中的 provider（即 priority 最高的可用 provider）
            # prefer_vision=True 时在vision provider中选最优
            # min_context_tokens过滤掉上下文能力不足的provider（事务#1807）
            target_provider = self._get_best_available_provider(
                forbidden=forbidden,
                prefer_vision=prefer_vision,
                min_context_tokens=min_context_tokens,
                task_id=task_id,
            )
            auto_selected = True
        else:
            # 尝试模糊匹配，排除被禁止的provider
            forbidden_set = set(forbidden) if forbidden else set()
            valid_favorites = [fav for fav in favorites if fav not in forbidden_set]
            if valid_favorites:
                r = random.randint(0, len(valid_favorites) - 1)
                target_provider = self.match_provider(valid_favorites[r])
            else:
                # 所有favorites都被禁止，使用自动选择
                target_provider = self._get_best_available_provider(
                    forbidden=forbidden,
                    min_context_tokens=min_context_tokens,
                    task_id=task_id,
                )
                auto_selected = True

        # 如果未找到匹配的 provider，尝试使用第一个可用的provider
        if not target_provider or target_provider not in self.providers:
            # 尝试使用第一个启用的provider（按priority排序），排除被禁止的
            enabled_providers = self._get_enabled_providers_sorted()
            # 过滤掉被禁止的provider
            if forbidden:
                enabled_providers = [p for p in enabled_providers if p not in set(forbidden)]
            first_provider = enabled_providers[0] if enabled_providers else None
            if first_provider:
                logger.warning(
                    f"[LLMConfig] target_provider无效 ({target_provider})，使用priority最高的provider: {first_provider}"
                )
                target_provider = first_provider
            else:
                logger.error("❌ [LLMConfig] config/peng.json中没有可用的provider配置！")
                sys.exit(1)

        # 检查target_provider是否启用
        original_provider = target_provider  # 保存原始provider名称，用于日志记录
        prov_config_check = self.providers[target_provider]
        if not prov_config_check.enable or prov_config_check.is_vision_only():
            reason = "未启用" if not prov_config_check.enable else "是vision-only provider"
            # 当前provider不可用，尝试切换到下一个启用的provider（按priority排序）
            enabled_providers = self._get_enabled_providers_sorted()
            # 过滤掉被禁止的provider
            if forbidden:
                enabled_providers = [p for p in enabled_providers if p not in set(forbidden)]
            if enabled_providers:
                # 使用priority最高的启用的provider
                target_provider = enabled_providers[0]
                logger.warning(
                    f"[LLMConfig] {original_provider}{reason}，切换到priority最高的provider: {target_provider}"
                )
            else:
                logger.error("❌ [LLMConfig] config/peng.json中没有可用的provider！")
                sys.exit(1)

        # 检查 provider 是否在冷却中（仅对显式指定的 provider 执行，自动选择已在 _get_best_available_provider 中处理）
        if not auto_selected:
            failure_info = self._get_failure_info(target_provider)
            if failure_info:
                current_time = time.time()
                if current_time - failure_info.last_failure_time <= failure_info.cooldown:
                    # 还在冷却中，强制切换到下一个 provider（全部失败也要换一个返回！）
                    fallback = self._get_best_available_provider(
                        forbidden=forbidden,
                        min_context_tokens=min_context_tokens,
                        task_id=task_id,
                    )
                    if fallback and fallback in self.providers:
                        logger.warning(f"[LLMConfig] {target_provider}在冷却中，切换到: {fallback}")
                        target_provider = fallback

        # 检查 target_provider 是否仍在冷却中，如果在则等待冷却结束
        # 这可以防止在所有 provider 都在冷却中时立即返回导致的报错循环
        target_failure_info = self._get_failure_info(target_provider)
        if target_failure_info:
            elapsed = time.time() - target_failure_info.last_failure_time
            if elapsed <= target_failure_info.cooldown:
                wait_time = target_failure_info.cooldown - elapsed
                logger.warning(
                    f"[LLMConfig] {target_provider} 仍在冷却中，等待 {wait_time:.1f} 秒后返回"
                )
                # 等待期间每30秒更新一次心跳，避免心跳超时
                wait_interval = 30
                wait_remaining = wait_time
                while wait_remaining > 0:
                    sleep_time = min(wait_interval, wait_remaining)
                    time.sleep(sleep_time)
                    wait_remaining -= sleep_time
                    # 更新心跳
                    if task_id is not None:
                        try:
                            from peng.task.manager import TaskManager

                            task_manager = TaskManager()
                            task_manager.update_heartbeat(task_id)
                        except Exception as e:
                            logger.debug(f"[LLMConfig] 心跳更新失败: {e}")

        # 上下文能力最终guard（事务#1807）：favorites匹配/target无效时的兜底列表
        # 等路径可能选中能力不足的provider（_get_best_available_provider全被过滤
        # 返回None时，target来自未过滤的_get_enabled_providers_sorted）。
        # 此时改选enabled非vision-only非forbidden中有效compact_threshold最大者；
        # 全部不足时也返回能力最大者——由上层走既有400→冷却→上下文紧张告警→
        # 压缩/重启自愈链路，绝不因能力校验而sys.exit或死等。
        if not self._context_capacity_ok(target_provider, min_context_tokens):
            enabled_providers = self._get_enabled_providers_sorted()
            if forbidden:
                enabled_providers = [p for p in enabled_providers if p not in set(forbidden)]
            if enabled_providers:
                best_capacity = max(
                    enabled_providers, key=lambda p: self._effective_compact_threshold(p)
                )
                logger.warning(
                    f"[LLMConfig] {target_provider}上下文能力不足"
                    f"(请求输入~{min_context_tokens}tokens)，无能力足够的可用provider，"
                    f"兜底使用能力最大的provider: {best_capacity} "
                    f"(compact_threshold={self._effective_compact_threshold(best_capacity)})，"
                    f"可能仍上下文超限，将走压缩/重启自愈链路（事务#1807）"
                )
                target_provider = best_capacity

        # 同步更新 current_provider（跟踪当前实际使用的 provider）
        self.current_provider = target_provider

        # 获取 provider 配置
        prov_config = self.providers[target_provider]

        # 构建返回的 dict（包含所有需要的值）
        result = {
            "provider": target_provider,
            "api_key": prov_config.api_key,
            "base_url": prov_config.base_url,
            "model": prov_config.model,
            "small_model": prov_config.small_model,
            # 🔧 事务#1819：运行期vision禁用时返回disabled（附件将降级发送）
            "vision": prov_config.vision
            if not is_provider_vision_runtime_disabled(target_provider)
            else "disabled",
            "thinking_mode": prov_config.thinking_mode
            if prov_config.thinking_mode != "auto"
            else self.thinking_mode,
            "thinking_type": prov_config.thinking_type,
            "reasoning_effort": prov_config.reasoning_effort,
            "reasoning_disabled_fallback": prov_config.reasoning_disabled_fallback,
            "thinking_persistence": prov_config.thinking_persistence,
            "preserve_thinking": prov_config.preserve_thinking,
            "timeout": self.timeout,
            "max_tokens": prov_config.max_tokens
            if prov_config.max_tokens is not None
            else self.max_tokens,
            "temperature": self.temperature,
            "sampling_params": prov_config.sampling_params,
            "icon": prov_config.icon,
            "compact_threshold": prov_config.compact_threshold
            if prov_config.compact_threshold is not None
            else self.compact_threshold,
            "compact_hint_threshold": prov_config.compact_hint_threshold
            if prov_config.compact_hint_threshold is not None
            else self.compact_hint_threshold,
            "price_input_hit": prov_config.price_input_hit
            if prov_config.price_input_hit is not None
            else self.price_input_hit,
            "price_input_miss": prov_config.price_input_miss
            if prov_config.price_input_miss is not None
            else self.price_input_miss,
            "price_output": prov_config.price_output
            if prov_config.price_output is not None
            else self.price_output,
            "thinking_budget": self.thinking_budget,
            "frequency_penalty": prov_config.frequency_penalty
            if prov_config.frequency_penalty is not None
            else self.frequency_penalty,
            "presence_penalty": prov_config.presence_penalty
            if prov_config.presence_penalty is not None
            else self.presence_penalty,
            "overthinking_threshold": prov_config.overthinking_threshold
            if prov_config.overthinking_threshold is not None
            else self.overthinking_threshold,
            "degenerate_loop_patterns": prov_config.degenerate_loop_patterns
            if prov_config.degenerate_loop_patterns is not None
            else self.degenerate_loop_patterns,
            "symbol_output_threshold": prov_config.symbol_output_threshold
            if prov_config.symbol_output_threshold is not None
            else self.symbol_output_threshold,
            "repeated_word_threshold": prov_config.repeated_word_threshold
            if prov_config.repeated_word_threshold is not None
            else self.repeated_word_threshold,
            "repeated_phrase_maxlen": prov_config.repeated_phrase_maxlen
            if prov_config.repeated_phrase_maxlen is not None
            else self.repeated_phrase_maxlen,
            "pua_threshold": prov_config.pua_threshold
            if prov_config.pua_threshold is not None
            else self.pua_threshold,
        }

        return result  # 永不返回None

    def save_failed_providers(self) -> None:
        """
        保存失败记录到磁盘（合并策略：先读取文件当前内容，与内存记录合并后写回）

        合并规则：对于同一个 provider，取 last_failure_time 较新的记录。
        这样在多进程场景下，各进程的失败记录不会互相覆盖丢失。

        文件格式:
        {
            "zhipu": {"failure_count": 2, "last_failure_time": 1234567890.123, "cooldown": 10},
            "dashscope": {"failure_count": 1, "last_failure_time": 1234567891.456, "cooldown": 5}
        }
        """
        failed_file = getattr(self, "_failed_providers_path", None)
        if not failed_file:
            return
        ensure_dir_with_owner(failed_file.parent)

        # 转换为可序列化的字典
        data = {
            provider: info.to_dict() if isinstance(info, FailureInfo) else info
            for provider, info in self.failed_providers.items()
        }

        # 合并策略：先读取文件当前内容，与内存记录按 last_failure_time 取较新的
        if failed_file.exists():
            try:
                with open(failed_file, encoding="utf-8") as f:
                    file_data = json.load(f)
                if isinstance(file_data, dict):
                    for provider, file_info in file_data.items():
                        if not isinstance(file_info, dict):
                            continue
                        if provider in data:
                            # 两边都有：取 last_failure_time 较新的
                            mem_time = data[provider].get("last_failure_time", 0)
                            file_time = file_info.get("last_failure_time", 0)
                            if file_time > mem_time:
                                data[provider] = file_info
                        else:
                            # 仅文件中有：保留（可能来自其他进程）
                            data[provider] = file_info
            except Exception:
                # 文件读取失败，使用内存中的数据继续写入
                pass

        try:
            with open(failed_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            import traceback

            logger.error(
                f"❌ save_failed_providers() - loader.py:734: 异常 - {e}\n{traceback.format_exc()}"
            )
            # 静默失败，不影响主流程
            pass

    def load_failed_providers(self) -> None:
        """
        从磁盘加载失败记录（路径由 PathsConfig.failed_providers_json 配置）

        启动时调用，恢复之前的失败状态和冷却时间
        """
        failed_file = getattr(self, "_failed_providers_path", None)
        if not failed_file:
            return

        if not failed_file.exists():
            return

        try:
            with open(failed_file, encoding="utf-8") as f:
                data = json.load(f)

            # 转换为 FailureInfo 对象
            self.failed_providers = {
                provider: FailureInfo.from_dict(info_data) for provider, info_data in data.items()
            }
        except Exception as e:
            import traceback

            logger.error(
                f"❌ load_failed_providers() - loader.py:759: 异常 - {e}\n{traceback.format_exc()}"
            )
            # 静默失败，使用空的失败记录
            self.failed_providers = {}


class DisplayConfig(BaseModel):
    """显示配置"""

    stream: bool = True
    color: bool = True
    verbose: bool = False
    username: str = "master"  # 默认用户名
    order: str = "ASC"  # 默认排序顺序：ASC=从老到新，DESC=从新到老


class AutoModeConfig(BaseModel):
    """全自动模式配置"""

    # 是否启用全自动模式
    enabled: bool = False
    # 全自动模式下的行为
    allow_git_commit: bool = True
    allow_modify_external_files: bool = True
    allow_stop_midway: bool = False  # 全自动模式下禁止中途停止
    allow_ask_user: bool = False  # 全自动模式下禁止询问用户
    # Git 管理配置
    auto_git: bool = True  # 全自动模式下自动进行 git 管理
    auto_git_commit: bool = True  # 变更后自动提交
    auto_git_branch: str = "auto"  # auto=根据复杂度自动决定，true=总是创建，false=不创建
    git_commit_prefix: str = "auto"  # 提交信息前缀

    def should_create_branch(self, task_complexity: str = "medium") -> bool:
        """
        判断是否应该创建分支

        Args:
            task_complexity: 事务复杂度 (simple, medium, complex)

        Returns:
            是否创建分支
        """
        if self.auto_git_branch == "true":
            return True
        if self.auto_git_branch == "false":
            return False
        # auto 模式：根据复杂度决定
        # complex 事务创建分支，simple/medium 不创建
        return task_complexity == "complex"


def is_auto_mode_prompt(prompt: str) -> bool:
    """判断提示词是否触发全自动模式

    根据 AGENTS.md 的规则，以下关键词触发全自动：
    - 全自动
    - 自动执行
    - 无需确认
    - 持续执行
    - 自主完成

    Args:
        prompt: 用户输入的提示词

    Returns:
        True 表示应该使用全自动模式
    """
    if not prompt:
        return False

    prompt_lower = prompt.lower()
    return any(kw in prompt_lower for kw in _AUTO_MODE_KEYWORDS)


class MonitorConfig(BaseModel):
    """监控配置"""

    inbox_monitor_interval: float = 1.0  # 支持亚秒级轮询（如0.5秒）
    heartbeat_monitor_interval: float = 1.0
    archive_after_days: int = 3
    auto_start_pending_tasks: bool = False
    default_heartbeat_timeout: int = 600  # 事务默认心跳超时时间（秒）
    # 🔧 事务#2087：备份快照总预算（秒）——工具调用前/迭代开始的快照链
    # （远程大仓库git快照+外部文件备份串行堆叠）超过预算时跳过快照直接
    # 执行工具，防止#2081式152秒静默阻塞（bash timeout从execute才开始
    # 计时，被快照绕过）。0=禁用预算限制（恢复旧行为）
    snapshot_budget_seconds: int = 60
    # 鹏🐣-忠实助手定时重启配置
    task_manager_check_interval: int = 300  # 检查间隔（秒），默认5分钟
    task_manager_prompt_name: str = "task_manager_prompt"  # 鹏🐣-忠实助手的prompt字段值
    assistant_prompt_name: str = "assistant_prompt"  # 鹏🐣-忠实助手的prompt字段值


class ParallelConfig(BaseModel):
    """并行配置"""

    max_concurrent_tasks: int = 10
    llm_max_concurrent: int = 5  # 大模型并发数
    small_model_max_concurrent: int = 200  # 小模型并发数（用于简单事务）


class ProcessConfig(BaseModel):
    """进程管理配置"""

    max_processes: int = 65536  # 最大进程数


class TimeoutConfig(BaseModel):
    """超时配置"""

    tool: float = 30.0
    task: float = 300.0
    bash: float = 120.0


class LLMLogCategoryConfig(BaseModel):
    """LLM 日志分类配置"""

    messages: bool = True
    tools: bool = True
    parameters: bool = True
    full_payload: bool = True


class LLMLogResponseConfig(BaseModel):
    """LLM 响应日志配置"""

    content: bool = True
    tool_calls: bool = True
    thinking: bool = True
    metadata: bool = True
    full_response: bool = True


class LLMLogStreamConfig(BaseModel):
    """LLM 流式响应日志配置"""

    chunks: bool = False
    final_result: bool = True


class LLMLogErrorConfig(BaseModel):
    """LLM 错误日志配置"""

    http_errors: bool = True
    parse_errors: bool = True
    timeout_errors: bool = True
    stack_trace: bool = True


class LLMLogPerformanceConfig(BaseModel):
    """LLM 性能日志配置"""

    latency: bool = True
    token_usage: bool = True
    concurrency_wait: bool = False


class LLMLogDebugConfig(BaseModel):
    """LLM 调试日志配置"""

    headers: bool = False
    response_headers: bool = False
    retries: bool = True


class LLMLogsConfig(BaseModel):
    """LLM 日志配置"""

    request: LLMLogCategoryConfig = Field(default_factory=LLMLogCategoryConfig)
    response: LLMLogResponseConfig = Field(default_factory=LLMLogResponseConfig)
    stream: LLMLogStreamConfig = Field(default_factory=LLMLogStreamConfig)
    error: LLMLogErrorConfig = Field(default_factory=LLMLogErrorConfig)
    performance: LLMLogPerformanceConfig = Field(default_factory=LLMLogPerformanceConfig)
    debug: LLMLogDebugConfig = Field(default_factory=LLMLogDebugConfig)


class TaskLogsConfig(BaseModel):
    """事务管理日志配置"""

    create: bool = True
    status: bool = True
    worktree: bool = True
    child_task: bool = True
    orchestration: bool = True
    git: bool = True


class MessageLogsConfig(BaseModel):
    """消息处理日志配置"""

    inbox: bool = True
    process: bool = True
    chain: bool = True
    context: bool = True
    database: bool = True


class ProcessLogsConfig(BaseModel):
    """进程管理日志配置"""

    create: bool = True
    status: bool = True
    heartbeat: bool = True
    timeout: bool = True
    runtime: bool = True
    callbacks: bool = True


class AgentLogsConfig(BaseModel):
    """Agent 执行日志配置"""

    decision: bool = True
    tool_call: bool = True
    result: bool = True
    thinking: bool = True


class SessionLogsConfig(BaseModel):
    """会话管理日志配置"""

    create: bool = True
    pause_resume: bool = True
    cancel: bool = True
    chain_switch: bool = True


class DatabaseLogsConfig(BaseModel):
    """数据库日志配置"""

    query: bool = True
    save: bool = True
    recovery: bool = True
    migration: bool = True


class GitLogsConfig(BaseModel):
    """Git 操作日志配置"""

    worktree: bool = True
    branch: bool = True
    commit: bool = True
    ignore: bool = True
    status: bool = True


class DebugLogsConfig(BaseModel):
    """调试日志配置"""

    verbose: bool = False
    trace: bool = False
    profiling: bool = False


class LogsConfig(BaseModel):
    """所有日志分类配置"""

    llm: LLMLogsConfig = Field(default_factory=LLMLogsConfig)
    task: TaskLogsConfig = Field(default_factory=TaskLogsConfig)
    message: MessageLogsConfig = Field(default_factory=MessageLogsConfig)
    process: ProcessLogsConfig = Field(default_factory=ProcessLogsConfig)
    agent: AgentLogsConfig = Field(default_factory=AgentLogsConfig)
    session: SessionLogsConfig = Field(default_factory=SessionLogsConfig)
    database: DatabaseLogsConfig = Field(default_factory=DatabaseLogsConfig)
    git: GitLogsConfig = Field(default_factory=GitLogsConfig)
    debug: DebugLogsConfig = Field(default_factory=DebugLogsConfig)


class LoggingConfig(BaseModel):
    """日志配置"""

    level: str = "INFO"
    file: str = "logs/peng.log"
    format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    logs: LogsConfig | None = None
    # 向后兼容：保留 llm_logs 字段
    llm_logs: LLMLogsConfig | None = None

    def model_post_init(self, __context):
        """初始化后处理，确保 logs 字段不为 None"""
        if self.logs is None:
            if self.llm_logs:
                # 从 llm_logs 构建
                self.logs = LogsConfig(llm=self.llm_logs)
            else:
                self.logs = LogsConfig()

    def get_logs_config(self) -> LogsConfig:
        """获取日志配置，支持向后兼容"""
        if self.logs is None:
            # 初始化 logs
            if self.llm_logs:
                self.logs = LogsConfig(llm=self.llm_logs)
            else:
                self.logs = LogsConfig()
        return self.logs


class SkillsConfig(BaseModel):
    """Skills 配置"""

    enabled: bool = True
    directories: list[str] = Field(default_factory=lambda: ["skills", "~/.agents/skills"])
    auto_load: bool = True


class TokenMonitorConfig(BaseModel):
    """Token 监控配置"""

    enabled: bool = True
    max_tokens: int = 128000
    warning_threshold: float = 0.7
    critical_threshold: float = 0.9


class TruncationConfig(BaseModel):
    """工具输出截断配置

    用于防止工具输出过大导致 token 爆炸。
    参考 opencode 的设计：三层防御策略。
    """

    # 通用截断限制
    max_lines: int = 2000  # 最大行数
    max_bytes: int = 51200  # 最大字节数 (50KB)
    max_line_length: int = 2000  # 单行最大长度

    # 工具特定限制
    grep_max_matches: int = 100  # grep 最多显示匹配数
    grep_max_line_length: int = 2000  # grep 单行最大长度

    # 保留天数
    retention_days: int = 7  # 截断输出文件保留天数

    # 工具结果上限（回退值）：优先使用当前 provider 的 compact_threshold，
    # 仅当 _llm_client 不可用时回退到此值
    tool_result_max_bytes: int = 32768  # 32KB

    # 上下文注入配置
    context_inject_interval: int = 50000  # 每 50k token 注入一次最新上下文

    # 提示词配置（从 prompts.json 加载，这里只是默认值）
    hint_template: str = ""  # 截断提示模板
    grep_truncated_hint: str = ""  # grep 截断提示


class TruncationHintsConfig(BaseModel):
    """截断提示词配置"""

    # 通用截断提示
    general: str = (
        "The tool call succeeded but the output was truncated. "
        "Full output saved to: {full_path}\n"
        "Use read(file_path='{full_path}', offset=N, limit=M) to view specific sections, "
        "or grep(pattern='...', path='{full_path}') to search the content."
    )

    # grep 截断提示
    grep_truncated: str = (
        "(Results truncated: showing {shown} of {total} matches. "
        "Consider using a more specific path or pattern.)"
    )

    # 行截断提示
    line_truncated: str = "({count} lines were truncated to {max_length} chars)"


class ModelColorsConfig(BaseModel):
    """模型输出颜色配置"""

    big_model: str = "white"
    small_model: str = "bright_black"


class UIColorsConfig(BaseModel):
    """UI 元素颜色配置"""

    prompt: str = "cyan"
    agent_name: str = "cyan"
    system: str = "yellow"
    error: str = "bright_red"
    success: str = "bright_green"
    warning: str = "yellow"
    dim: str = "bright_black"
    info: str = "cyan"
    goodbye: str = "green"
    title: str = "cyan"


class ToolColorsConfig(BaseModel):
    """工具执行颜色配置"""

    tool_call: str = "bright_magenta"
    tool_result: str = "bright_green"
    tool_error: str = "bright_red"
    tool_executing: str = "bright_magenta"


class AgentColorsConfig(BaseModel):
    """Agent 输出颜色配置"""

    thinking: str = "bright_black"
    tool_call: str = "bright_blue"
    result: str = "bright_green"


class BashColorsConfig(BaseModel):
    """Bash 命令输出颜色配置"""

    error: str = "bright_red"
    success: str = "bright_green"
    info: str = "bright_blue"
    reset: str = "reset"


class WebColorsConfig(BaseModel):
    """Web 工具颜色配置"""

    info: str = "bright_blue"
    reset: str = "reset"


class FileColorsConfig(BaseModel):
    """文件操作颜色配置"""

    error: str = "bright_red"
    success: str = "bright_green"
    warning: str = "bright_yellow"
    info: str = "bright_blue"
    highlight: str = "bright_cyan"
    bold: str = "bold"
    dim: str = "dim"
    reset: str = "reset"


class MessageColorsConfig(BaseModel):
    """消息列表颜色配置"""

    error: str = "bright_red"
    success: str = "bright_green"
    warning: str = "bright_yellow"
    info: str = "bright_blue"
    highlight: str = "bright_magenta"
    title: str = "bright_cyan"
    content: str = "bright_white"
    bold: str = "bold"
    dim: str = "dim"
    reset: str = "reset"


class TaskColorsConfig(BaseModel):
    """事务列表颜色配置"""

    error: str = "bright_red"
    success: str = "bright_green"
    warning: str = "bright_yellow"
    info: str = "bright_blue"
    highlight: str = "bright_magenta"
    title: str = "bright_cyan"
    content: str = "bright_white"
    bold: str = "bold"
    dim: str = "dim"
    reset: str = "reset"


class FlyColorsConfig(BaseModel):
    """Fly 输出颜色配置"""

    error: str = "red"
    success: str = "green"
    warning: str = "yellow"
    info: str = "blue"
    reset: str = "reset"


class XtermThemeConfig(BaseModel):
    """Xterm 终端主题配置"""

    background: str = "#0d1117"
    foreground: str = "#e6edf3"
    cursor: str = "#e6edf3"
    cursorAccent: str = "#0d1117"
    selection: str = "rgba(255, 255, 255, 0.3)"
    selectionForeground: str = "#e6edf3"
    black: str = "#2e3436"
    red: str = "#cc0000"
    green: str = "#4e9a06"
    yellow: str = "#c4a000"
    blue: str = "#3465a4"
    magenta: str = "#75507b"
    cyan: str = "#06989a"
    white: str = "#d3d7cf"
    brightBlack: str = "#555753"
    brightRed: str = "#ef2929"
    brightGreen: str = "#8ae234"
    brightYellow: str = "#fce94f"
    brightBlue: str = "#729fcf"
    brightMagenta: str = "#ad7fa8"
    brightCyan: str = "#34e2e2"
    brightWhite: str = "#eeeeec"
    # 🔧 Task#840：自定义扩展颜色（xterm.js 不认，前端自行使用）
    grey: str = "#6e7681"
    darkGrey: str = "#484f58"


class WebUIColorsConfig(BaseModel):
    """WebUI CSS 变量颜色配置"""

    bg_primary: str = "#0d1117"
    bg_secondary: str = "#161b22"
    bg_tertiary: str = "#21262d"
    bg_hover: str = "#30363d"
    text_primary: str = "#e6edf3"
    text_secondary: str = "#8b949e"
    text_muted: str = "#6e7681"
    border_color: str = "#30363d"
    accent_color: str = "#58a6ff"
    accent_hover: str = "#79b8ff"
    success_color: str = "#3fb950"
    warning_color: str = "#d29922"
    error_color: str = "#f85149"
    link_color: str = "#58a6ff"
    # 事务选中边框颜色
    task_selection_border_color: str = "#308C30"
    task_selection_border_rgba: str = "rgba(60, 140, 60, 0.5)"
    # 消息选中边框颜色
    message_selection_border_color: str = "#1282D6"
    message_selection_border_rgba: str = "rgba(33, 150, 255, 0.4)"
    # 根消息底色
    root_message_bg_color: str = "rgba(48, 128, 192, 0.10)"
    # 根事务底色
    root_task_bg_color: str = "rgba(48, 128, 192, 0.10)"


class WebUIThemeConfig(BaseModel):
    """WebUI 单主题配置"""

    ui: WebUIColorsConfig = Field(default_factory=WebUIColorsConfig)
    xterm: XtermThemeConfig = Field(default_factory=XtermThemeConfig)
    icon_logo: str = "🪽"
    icon_theme_dark: str = "🌙"
    icon_theme_light: str = "☀️"
    icon_attach_file: str = "📂"
    icon_new_message: str = "✨"
    icon_help: str = "?"
    icon_messages_title: str = "📜 消息"
    icon_tasks_title: str = "🎋 事务"
    icon_status_pending: str = "⏳"
    icon_status_running: str = "🥎"
    icon_status_accepting: str = "🔍"
    icon_status_accepted: str = "✅"
    icon_status_processing: str = "⏳"
    icon_status_replied: str = "✔️"
    icon_status_done: str = "✅"
    icon_status_paused: str = "⏸️"
    icon_status_interrupted: str = "⚠️"
    icon_status_failed: str = "❌"
    icon_status_completed: str = "✅"
    icon_status_cancelled: str = "🚫"
    icon_status_unread: str = "📩"
    icon_status_read: str = "📖"
    icon_action_pause: str = "⏸️"
    icon_action_cancel: str = "❌"
    icon_action_resume: str = "▶️"
    icon_action_restart: str = "🔄"
    icon_action_delete: str = "❌"
    icon_action_archive: str = "♻️"
    icon_action_notify: str = "🛎"


class WebUIThemesConfig(BaseModel):
    """WebUI 主题配置（light/dark）"""

    dark: WebUIThemeConfig = Field(default_factory=WebUIThemeConfig)
    light: WebUIThemeConfig = Field(default_factory=WebUIThemeConfig)


class EditorConfig(BaseModel):
    """编辑器配置"""

    emoji_text_mode: bool = False  # emoji文字模式，默认关闭(彩色emoji)
    auto_create_empty_doc: bool = False  # 切换到编辑器视图时是否自动创建空文档，默认否


class WebUIConfig(BaseModel):
    """WebUI 配置"""

    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 6688
    theme: str = "dark"
    # 刷新间隔配置（毫秒）
    messages_refresh_interval: int = (
        15000  # 消息栏刷新间隔，默认15秒（WebSocket增量更新为主，轮询为辅）
    )
    tasks_refresh_interval: int = (
        30000  # 事务栏刷新间隔，默认30秒（WebSocket增量更新为主，轮询为辅）
    )
    # 工作目录
    workdir: str = "~/"
    # 默认 nest（远程连接名）
    nest: str = ""
    jwt_secret_key: str = "${JWT_SECRET_KEY:-change-this-secret-key-in-production}"
    # 输出来源过滤器
    output_source_filters: Dict[str, Any] = Field(default_factory=dict)
    # 内容截断配置
    content_trunc_lines: int = 10  # 内容最大显示行数
    content_trunc_chars: int = 300  # 内容最大显示字符数（不是字节）
    # 字体配置
    font: Dict[str, Any] = Field(
        default_factory=lambda: {
            "family": "JetBrains Mono, Fira Code, Consolas, Monaco, monospace",
            "size": 11,
            "size_xterm": 11,
        }
    )
    # 侧边栏宽度配置（px）
    sidebar_left_width: int = 320
    sidebar_right_width: int = 220
    # 🔧 Task#153：选中的消息/事务ID（刷新页面后恢复选中状态）
    selected_message_id: Optional[int] = None
    selected_task_id: Optional[int] = None
    selected_branch_message_id: Optional[int] = None
    # 🔧 Task#262：大文件编辑器加载配置
    large_file_threshold: int = 3 * 1024 * 1024  # 大文件分块加载阈值（默认3MB）
    large_file_parallel_threshold: int = 6 * 1024 * 1024  # 并发分块加载阈值（默认6MB）
    large_file_parallel_concurrency: int = 4  # 并发分块加载并发数
    large_file_chunk_size: int = 1024 * 1024  # 每个分块大小（默认1MB）
    # 🔧 输入历史记录最大条数（前后端统一限制）
    input_history_limit: int = 500
    # 🔧 Task#228：顶置消息/事务ID列表
    topped_message_ids: list[int] = Field(default_factory=list)
    topped_task_ids: list[int] = Field(default_factory=list)
    # 🔧 临时文件清理配置
    cleanup_temp_days: int = 2  # data/temp、data/shot 中文件保留天数
    logs_max_size_mb: int = 800  # logs 目录最大总大小（MB）
    # 🔧 编辑器配置
    editor: EditorConfig = Field(default_factory=EditorConfig)
    # 🔧 Task#840：xterm 配置
    xterm: Dict[str, Any] = Field(
        default_factory=lambda: {"scrollback": 500, "show_line_numbers": False}
    )


class ColorsConfig(BaseModel):
    """颜色配置"""

    models: ModelColorsConfig = Field(default_factory=ModelColorsConfig)
    ui: UIColorsConfig = Field(default_factory=UIColorsConfig)
    tools: ToolColorsConfig = Field(default_factory=ToolColorsConfig)
    agent: AgentColorsConfig = Field(default_factory=AgentColorsConfig)
    bash: BashColorsConfig = Field(default_factory=BashColorsConfig)
    web: WebColorsConfig = Field(default_factory=WebColorsConfig)
    file: FileColorsConfig = Field(default_factory=FileColorsConfig)
    message: MessageColorsConfig = Field(default_factory=MessageColorsConfig)
    task: TaskColorsConfig = Field(default_factory=TaskColorsConfig)
    fly: FlyColorsConfig = Field(default_factory=FlyColorsConfig)


class NestEndpointConfig(BaseModel):
    """单个 Nest 端点配置"""

    name: str = "本地"
    host: str = "localhost"
    port: int = 8899


class NestConfig(BaseModel):
    """Nest（鹏窝）配置"""

    enabled: bool = True  # 是否启用 nest
    default_nest: str = "local"  # 默认 nest 名称
    nests: dict[str, NestEndpointConfig] = Field(
        default_factory=lambda: {"local": NestEndpointConfig()}
    )
    # 安全配置
    dir_whitelist: list[str] = Field(default_factory=list)  # 目录白名单（空=允许所有）
    dir_blacklist: list[str] = Field(default_factory=list)  # 目录黑名单
    cmd_whitelist: list[str] = Field(default_factory=list)  # 命令白名单（空=允许所有）
    cmd_blacklist: list[str] = Field(default_factory=list)  # 命令黑名单
    # 功能开关
    enable_window_query: bool = True  # 是否允许窗口查询
    enable_screenshot: bool = True  # 是否允许截屏
    # 超时配置
    timeout: float = 120.0  # 请求超时时间（秒）


class ToolsConfig(BaseModel):
    """工具配置

    用于在工具注册表层禁用指定工具：
    - 被禁用的工具不会出现在发给 LLM 的工具定义中（LLM 看不到，不会调用）
    - 即使被显式调用，registry.execute 也会拦截并返回"工具已禁用"错误

    这是比 nest 安全层（enable_screenshot 等）更上层的通用禁用机制，
    可禁用任意内置工具（如 shot / window / input）。
    """

    disabled: list[str] = Field(default_factory=list)  # 需禁用的工具名列表（主名或别名）


class PengConfig(BaseModel):
    """{AGENT_NAME}的主配置"""

    paths: PathsConfig = Field(default_factory=PathsConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    parallel: ParallelConfig = Field(default_factory=ParallelConfig)
    process: ProcessConfig = Field(default_factory=ProcessConfig)
    timeout: TimeoutConfig = Field(default_factory=TimeoutConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    auto_mode: AutoModeConfig = Field(default_factory=AutoModeConfig)
    token_monitor: TokenMonitorConfig = Field(default_factory=TokenMonitorConfig)
    truncation: TruncationConfig = Field(default_factory=TruncationConfig)
    colors: ColorsConfig = Field(default_factory=ColorsConfig)
    webui: WebUIConfig = Field(default_factory=WebUIConfig)
    webui_themes: WebUIThemesConfig = Field(default_factory=WebUIThemesConfig)
    nest: NestConfig = Field(default_factory=NestConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    # 消息通道配置（微信/飞书等，dict 透传，已合并 data/peng.json 覆盖层）
    channels: dict = Field(default_factory=dict)
    db: dict = Field(default_factory=dict)
    max_iterations: int = 100
    agent_name: str = "鹏🐣"
    master_name: str = "master"

    @staticmethod
    def _parse_llm_config(llm_data: dict[str, Any]) -> dict[str, Any]:
        """解析 LLM 配置，处理 providers 嵌套结构，支持 copy 字段继承

        处理流程：
        1. 先将所有 provider 创建为 ProviderConfig 对象
        2. 再处理 copy 继承关系（用被继承 provider 的配置填充缺省字段）
        """
        if not llm_data:
            return {}

        # 复制数据避免修改原数据
        result = dict(llm_data)

        # 处理 providers 配置
        providers_data = result.pop("providers", {})
        if providers_data:
            # 第一步：先创建所有 ProviderConfig（copy 字段保存在对象中）
            providers = {name: ProviderConfig(**config) for name, config in providers_data.items()}
            # 第二步：解析 copy 字段，处理继承关系
            PengConfig._resolve_provider_copy(providers, providers_data)
            result["providers"] = providers

        return result

    @staticmethod
    def _resolve_provider_copy(
        providers: dict[str, ProviderConfig], raw_data: dict[str, dict[str, Any]]
    ) -> None:
        """解析 provider 的 copy 字段，展开继承关系（原地修改 providers 字典）

        处理逻辑：
        1. 对于有 copy 字段的 provider，先复制被继承 provider 的所有配置
        2. 然后用当前 provider 自己显式设置的字段覆盖
        3. 支持链式继承：A copy B, B copy C → A 最终继承 B 和 C 的配置
        4. 检测循环继承并报错

        Args:
            providers: 已创建的 ProviderConfig 字典 {name: ProviderConfig}（会被原地修改）
            raw_data: 原始 providers 配置字典 {name: {field: value, ...}}，
                      用于判断哪些字段是用户显式设置的
        """
        resolved: set[str] = set()

        def resolve_one(name: str, resolving_chain: set[str]) -> None:
            """递归解析单个 provider 的继承链"""
            if name in resolved:
                return

            if name not in providers:
                logger.warning(f"[ProviderConfig] copy 引用了不存在的 provider: {name}")
                return

            # 检测循环继承
            if name in resolving_chain:
                logger.error(
                    f"[ProviderConfig] 检测到循环继承: {' -> '.join(resolving_chain)} -> {name}"
                )
                return

            prov = providers[name]
            clone = prov.clone

            if clone is not None:
                # 先递归解析被继承的 provider（确保它已处理完自己的 clone）
                resolve_one(clone, resolving_chain | {name})

                if clone not in providers:
                    logger.warning(
                        f"[ProviderConfig] {name} 的 clone 目标 {clone} 不存在，跳过继承"
                    )
                    resolved.add(name)
                    return

                # 获取被继承 provider 的完整配置（已解析过 clone 的）
                base_dict = providers[clone].model_dump()
                # 获取当前 provider 原始数据中用户显式设置的字段（排除 clone）
                overrides = dict(raw_data.get(name, {}))
                overrides.pop("clone", None)
                # 基础配置作为底，用户显式设置的字段覆盖
                base_dict.update(overrides)
                # 重新创建 ProviderConfig（clone 置为 None 表示已处理）
                base_dict["clone"] = None
                providers[name] = ProviderConfig(**base_dict)

            resolved.add(name)

        for name in providers:
            resolve_one(name, set())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PengConfig:
        """从字典创建配置"""
        # 解析嵌套的颜色配置
        colors_data = data.get("colors", {})
        models_data = colors_data.get("models", {})
        ui_data = colors_data.get("ui", {})
        tools_data = colors_data.get("tools", {})
        agent_data = colors_data.get("agent", {})
        bash_data = colors_data.get("bash", {})
        web_data = colors_data.get("web", {})
        file_data = colors_data.get("file", {})
        message_data = colors_data.get("message", {})
        task_data = colors_data.get("task", {})
        fly_data = colors_data.get("fly", {})

        # 解析输出配置（支持旧的 debug 格式和新的 output 格式）
        debug_data = data.get("debug", {})

        # 解析 logging 配置（包含 logs 和 llm_logs）
        logging_data = data.get("logging", {})

        # 支持新的 logs 配置结构
        logs_data = logging_data.pop("logs", {})

        # 向后兼容：处理旧的 llm_logs 配置
        llm_logs_data = logging_data.pop("llm_logs", {})

        # 如果 logs_data 为空，从 llm_logs_data 构建
        if not logs_data and llm_logs_data:
            # 判断 llm_logs_data 是完整的 logs.json 内容（包含多个分类）还是仅 LLM 配置
            # 如果包含 "llm", "task", "message" 等多个键，说明是完整的 logs.json
            if (
                isinstance(llm_logs_data, dict)
                and len(llm_logs_data) > 1
                and "llm" in llm_logs_data
            ):
                # 完整的 logs.json 内容，直接使用
                logs_data = llm_logs_data
            else:
                # 仅 LLM 配置，嵌套在 "llm" 键下
                logs_data = {"llm": llm_logs_data}
        elif llm_logs_data and "llm" not in logs_data:
            # 合并 llm_logs 到 logs.llm
            logs_data["llm"] = llm_logs_data

        # 创建 LogsConfig
        logs_config = LogsConfig(**logs_data) if logs_data else LogsConfig()

        # 创建 LoggingConfig
        logging_config = LoggingConfig(**logging_data, logs=logs_config)

        # 解析 webui 配置
        webui_data = data.get("webui", {})

        # 解析 webui_themes 配置
        webui_themes_data = data.get("webui_themes", {})
        webui_themes_config = (
            WebUIThemesConfig.model_validate(webui_themes_data)
            if webui_themes_data
            else WebUIThemesConfig()
        )

        return cls(
            paths=PathsConfig(**data.get("paths", {})),
            monitor=MonitorConfig(**data.get("monitor", {})),
            process=ProcessConfig(**data.get("process", {})),
            llm=LLMConfig(**PengConfig._parse_llm_config(data.get("llm", {}))),
            display=DisplayConfig(**data.get("display", {})),
            parallel=ParallelConfig(**data.get("parallel", {})),
            timeout=TimeoutConfig(**data.get("timeout", {})),
            logging=logging_config,
            skills=SkillsConfig(**data.get("skills", {})),
            auto_mode=AutoModeConfig(**data.get("auto_mode", {})),
            token_monitor=TokenMonitorConfig(**data.get("token_monitor", {})),
            truncation=TruncationConfig(**data.get("truncation", {})),
            colors=ColorsConfig(
                models=ModelColorsConfig(**models_data),
                ui=UIColorsConfig(**ui_data),
                tools=ToolColorsConfig(**tools_data),
                agent=AgentColorsConfig(**agent_data),
                bash=BashColorsConfig(**bash_data),
                web=WebColorsConfig(**web_data),
                file=FileColorsConfig(**file_data),
                message=MessageColorsConfig(**message_data),
                task=TaskColorsConfig(**task_data),
                fly=FlyColorsConfig(**fly_data),
            ),
            webui=WebUIConfig(**webui_data),
            webui_themes=webui_themes_config,
            nest=NestConfig(**data.get("nest", {})),
            tools=ToolsConfig(**data.get("tools", {})),
            channels=data.get("channels", {}),
            db=data.get("db", {}),
            max_iterations=data.get("max_iterations", 100),
            agent_name=data.get("agent_name", "鹏🐣"),
            master_name=data.get("master_name", "master"),
        )


def _expand_env_vars(value: str) -> str:
    """
    展开字符串中的环境变量

    支持两种格式:
    - ${VAR} - 必须存在
    - ${VAR:-default} - 如果不存在则使用默认值
    """

    def replace(match: re.Match) -> str:
        var_name = match.group(1)
        default = match.group(2)

        value = os.environ.get(var_name)
        if value is not None:
            return value
        if default is not None:
            return default
        return ""

    return _ENV_VAR_EXPAND_PATTERN.sub(replace, value)


def _process_config_values(data: Any) -> Any:
    """递归处理配置值，展开环境变量"""
    if isinstance(data, dict):
        return {k: _process_config_values(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_process_config_values(item) for item in data]
    if isinstance(data, str):
        return _expand_env_vars(data)
    return data


def clear_config_cache(config_path: Path | None = None) -> None:
    """
    清除配置缓存

    Args:
        config_path: 配置文件路径（可选，None 表示清除所有缓存）

    Note:
        如果指定的是项目根目录 config 子目录中的文件（如 webui.json、colors.json 等），
        则会清除主配置文件的缓存（因为这些文件会影响主配置的加载结果）。
    """
    global _config_cache

    if config_path is None:
        # 清除所有缓存
        _config_cache.clear()
        logger.debug("配置缓存已清除（所有）")
        return

    config_path = config_path.resolve()
    project_root = _get_project_root()

    # 检查是否是项目根目录 config 或 data 子目录中的文件
    config_dir = project_root / "config"
    data_dir = project_root / "data"
    if config_path.parent == config_dir or config_path.parent == data_dir:
        # 是相关配置文件，清除所有主配置文件的缓存
        _config_cache.clear()
        logger.debug(f"配置缓存已清除（相关配置文件修改: {config_path.name}）")
    elif config_path in _config_cache:
        # 是主配置文件，只清除该文件的缓存
        del _config_cache[config_path]
        logger.debug(f"配置缓存已清除: {config_path}")


def load_config(config_path: Path | None = None) -> PengConfig:
    """
    加载配置文件（带缓存）

    Args:
        config_path: 配置文件路径（可选，默认使用 find_config_file 查找）

    Returns:
        PengConfig: 配置对象

    Note:
        如果配置文件及相关的配置文件（webui.json、colors.json、logs.json）
        均未变化（mtime 未改变），则返回缓存的配置对象；
        否则重新加载配置并更新缓存。
    """
    global _config_cache

    if config_path is None:
        config_path = find_config_file()

    if config_path is None or not config_path.exists():
        # 返回默认配置
        return PengConfig()

    config_path = config_path.resolve()

    # 获取主配置文件的 mtime
    try:
        main_mtime = config_path.stat().st_mtime
    except Exception as e:
        logger.warning(f"无法获取配置文件 mtime: {e}")
        main_mtime = 0

    # 获取相关配置文件的 mtime（config 目录基础文件 + data 目录覆盖文件）
    project_root = _get_project_root()
    related_files = {
        # config 目录基础文件
        project_root / "config" / "webui.json",
        project_root / "config" / "colors.json",
        project_root / "config" / "logs.json",
        project_root / "config" / "prompts.jsonp",
        project_root / "config" / "db.json",
        project_root / "config" / "failed_providers.json",
        # data 目录覆盖文件（仅写入变化部分，mtime 变化时需重新加载）
        project_root / "data" / "peng.json",
        project_root / "data" / "webui.json",
        project_root / "data" / "colors.json",
        project_root / "data" / "logs.json",
        project_root / "data" / "db.json",
    }

    current_related_mtimes = {}
    for file_path in related_files:
        if file_path.exists():
            try:
                current_related_mtimes[file_path.resolve()] = file_path.stat().st_mtime
            except Exception:
                pass

    # 检查缓存
    if config_path in _config_cache:
        cached_config, cached_main_mtime, cached_related_mtimes = _config_cache[config_path]
        if main_mtime == cached_main_mtime:
            # 检查相关文件的 mtime（双向对比：既检测修改/新增，也检测删除——
            # 如 data/peng.json 覆盖层被删除后必须失效缓存，否则继续用旧配置）
            related_unchanged = current_related_mtimes == cached_related_mtimes

            if related_unchanged:
                # 所有文件均未变化，返回缓存
                return cached_config

    # 文件有变化或无缓存，重新加载
    with open(config_path, encoding="utf-8") as f:
        raw_data = json.load(f) or {}

    # 合并 data/peng.json 覆盖层（data 目录优先）
    data_peng_path = _get_data_overlay_path("peng.json")
    if data_peng_path.exists():
        overlay_data = _load_json_file(data_peng_path)
        if overlay_data:
            raw_data = _deep_merge(raw_data, overlay_data)

    # 展开环境变量
    processed_data = _process_config_values(raw_data)

    # 从单独的 colors.json 加载颜色配置
    colors_data = _load_colors_config(config_path)
    if colors_data:
        processed_data["colors"] = colors_data

    # 从 webui.json 加载 WebUI 配置
    webui_config_data = _load_webui_config(config_path)
    if webui_config_data:
        # 合并 webui 基本配置（优先使用 webui.json）
        if "webui" in webui_config_data:
            # 合并配置：webui.json 覆盖 peng.json 中的同名配置
            existing_webui = processed_data.get("webui", {})
            existing_webui.update(webui_config_data["webui"])
            processed_data["webui"] = existing_webui
        # 合并 webui_themes 配置
        if "webui_themes" in webui_config_data:
            processed_data["webui_themes"] = webui_config_data["webui_themes"]

    # 从 logs.json 加载 LLM 日志配置
    llm_logs_data = _load_llm_logs_config(config_path)
    if llm_logs_data:
        # 合并到 logging 配置中
        if "logging" not in processed_data:
            processed_data["logging"] = {}
        processed_data["logging"]["llm_logs"] = llm_logs_data

    # 从 db.json 加载数据库配置
    db_data = _load_db_config(config_path)
    if db_data:
        processed_data["db"] = db_data

    config = PengConfig.from_dict(processed_data)

    # 更新缓存
    _config_cache[config_path] = (config, main_mtime, current_related_mtimes)

    # 注入failed_providers路径到LLMConfig（从PathsConfig统一获取）
    config.llm._failed_providers_path = config.paths.failed_providers_json_path

    # 从磁盘恢复失败provider的冷却状态（跨进程共享）
    # 每个LLM事务在独立进程中运行，必须加载其他进程记录的失败状态
    config.llm.load_failed_providers()

    return config


def get_llm_config() -> Any:
    config = load_config()
    return config.llm


def load_config_with_failures(config_path: Path | None = None) -> PengConfig:
    """
    加载配置文件并恢复失败记录

    Args:
        config_path: 配置文件路径（可选，默认使用 find_config_file 查找）

    Returns:
        PengConfig: 配置对象
    """
    config = load_config(config_path)

    # 加载失败记录
    config.llm.load_failed_providers()

    return config


def _load_colors_config(config_path: Path) -> dict[str, Any]:
    """
    从 colors.json 加载颜色配置（支持 data 目录覆盖）

    Args:
        config_path: 主配置文件路径（用于定位项目根目录）

    Returns:
        颜色配置字典（data/colors.json 覆盖 config/colors.json）
    """
    project_root = _get_project_root()
    return _load_config_with_overlay(
        project_root / "config" / "colors.json",
        project_root / "data" / "colors.json",
    )


def _load_webui_config(config_path: Path) -> dict[str, Any]:
    """
    从 webui.json 加载 WebUI 主题配置（支持 data 目录覆盖）

    Args:
        config_path: 主配置文件路径（用于定位项目根目录）

    Returns:
        WebUI 主题配置字典（data/webui.json 覆盖 config/webui.json）
    """
    project_root = _get_project_root()
    return _load_config_with_overlay(
        project_root / "config" / "webui.json",
        project_root / "data" / "webui.json",
    )


def _load_llm_logs_config(config_path: Path) -> dict[str, Any]:
    """
    从 logs.json 加载 LLM 日志配置（支持 data 目录覆盖）

    Args:
        config_path: 主配置文件路径（用于定位项目根目录）

    Returns:
        LLM 日志配置字典（data/logs.json 覆盖 config/logs.json）
    """
    project_root = _get_project_root()
    return _load_config_with_overlay(
        project_root / "config" / "logs.json",
        project_root / "data" / "logs.json",
    )


def _load_db_config(config_path: Path) -> dict[str, Any]:
    """
    从 db.json 加载数据库配置（支持 data 目录覆盖）

    Args:
        config_path: 主配置文件路径（用于定位项目根目录）

    Returns:
        数据库配置字典（data/db.json 覆盖 config/db.json）
    """
    project_root = _get_project_root()
    return _load_config_with_overlay(
        project_root / "config" / "db.json",
        project_root / "data" / "db.json",
    )


def find_logs_json() -> Path | None:
    """
    查找 logs.json 配置文件

    Returns:
        找到的配置文件路径，如果未找到则返回 None
    """
    project_root = _get_project_root()
    search_paths = [
        Path.cwd() / "config" / "logs.json",
        project_root / "config" / "logs.json",
        Path.home() / ".config" / "peng" / "logs.json",
    ]

    for path in search_paths:
        if path.exists():
            return path

    return None


# ==================== 自动提高文件描述符限制 ====================


def _increase_ulimit() -> None:
    """
    自动提高文件描述符限制到系统允许的最大值（仅 POSIX 平台）

    解决问题：
    - 数据库操作需要大量文件描述符
    - 默认的 ulimit -n 可能太低（如 1024 或 10240）
    - 导致 "unable to open database file" 错误

    直接将 soft limit 提高到 hard limit（系统允许的最大值）。
    Windows 平台无 resource 模块，静默跳过。
    """
    import sys

    if sys.platform == "win32":
        return  # Windows 无 resource 模块，无需调整

    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)

        # 直接将 soft limit 提高到 hard limit
        if soft < hard:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    except Exception as e:
        import traceback

        logger.error(
            f"❌ _increase_ulimit() - loader.py:1718: 异常 - {e}\n{traceback.format_exc()}"
        )
        # 静默失败，不影响程序运行
        pass


# 模块导入时自动执行
_increase_ulimit()


def get_agent_name() -> str:
    """
    获取 Agent 名称

    Returns:
        Agent 名称（从配置中读取，默认为 "鹏🐣"）
    """
    try:
        config = load_config()
        return config.agent_name
    except Exception as e:
        import traceback

        logger.error(f"❌ get_agent_name() - loader.py:1738: 异常 - {e}\n{traceback.format_exc()}")
        # 配置加载失败时返回默认值
        return "鹏🐣"


def get_master_name() -> str:
    """
    获取master名称

    Returns:
        master名称（从配置中读取，默认为 "master"）
    """
    try:
        config = load_config()
        return config.master_name
    except Exception as e:
        import traceback

        logger.error(f"❌ get_master_name() - loader.py:1739: 异常 - {e}\n{traceback.format_exc()}")
        # 配置加载失败时返回默认值
        return "master"


def get_default_heartbeat_timeout() -> int:
    """
    获取事务默认心跳超时时间（秒）

    Returns:
        默认心跳超时（从配置中读取，默认180秒）
    """
    try:
        config = load_config()
        return config.monitor.default_heartbeat_timeout
    except Exception:
        return 320


def get_snapshot_budget_seconds() -> int:
    """获取备份快照总预算（秒，事务#2087）

    工具调用前/迭代开始的快照链超过该预算时跳过快照直接执行工具。
    0 表示禁用预算限制。
    """
    try:
        config = load_config()
        return max(0, int(config.monitor.snapshot_budget_seconds))
    except Exception:
        return 60

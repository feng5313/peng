"""
环境变量加载模块

从 shell rc 文件加载大模型相关的环境变量。
统一的 load_env_from_rc() 函数，消除重复实现。

跨平台支持：Windows 用户需要手动设置环境变量。
"""

from __future__ import annotations

import logging
import os
import re
import sys
import traceback
from pathlib import Path

# 环境变量定义
# 格式：provider_name -> {"api_key": ENV_VAR, "base_url": ENV_VAR}

logger = logging.getLogger(__name__)
PROVIDER_ENV_VARS: dict[str, dict[str, str]] = {
    "zhipu": {
        "api_key": "ZHIPU_AI_API_KEY",
        "base_url": "ZHIPU_AI_BASE_URL",
    },
    "dashscope": {
        "api_key": "DASHSCOPE_API_KEY",
        "base_url": "DASHSCOPE_BASE_URL",
    },
}

# 正则表达式缓存（避免重复编译）
_ENV_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _get_env_pattern(var_name: str) -> re.Pattern[str]:
    """获取或创建环境变量正则模式（带缓存）"""
    if var_name not in _ENV_PATTERN_CACHE:
        pattern = rf'export\s+{re.escape(var_name)}\s*=\s*["\']?([^"\'\s]+)["\']?'
        _ENV_PATTERN_CACHE[var_name] = re.compile(pattern)
    return _ENV_PATTERN_CACHE[var_name]


def _get_shell_rc_files() -> list[Path]:
    """获取当前平台的 Shell RC 文件列表"""
    if sys.platform == "win32":
        # Windows 没有 shell rc 文件
        # 用户需要手动设置环境变量或使用系统环境变量
        return []
    return [
        Path.home() / ".zshrc",
        Path.home() / ".bashrc",
    ]


def load_env_from_rc(providers: list[str] | None = None) -> None:
    """
    从 shell rc 文件加载环境变量（跨平台）

    Args:
        providers: 要加载的 provider 列表，None 表示加载所有支持的 provider

    加载的环境变量:
        - ZHIPU_AI_API_KEY: 智谱 AI API 密钥
        - ZHIPU_AI_BASE_URL: 智谱 AI API 基础 URL（可选）
        - DASHSCOPE_API_KEY: 通义千问 API 密钥
        - DASHSCOPE_BASE_URL: 通义千问 API 基础 URL（可选）

    优先级:
        1. 已存在的环境变量（不覆盖）
        2. ~/.zshrc 中的配置（仅 Unix）
        3. ~/.bashrc 中的配置（仅 Unix）

    注意:
        Windows 用户需要手动设置环境变量或使用系统环境变量。
    """
    if providers is None:
        providers = list(PROVIDER_ENV_VARS.keys())

    for rc_file in _get_shell_rc_files():
        if not rc_file.exists():
            continue

        try:
            content = rc_file.read_text(encoding="utf-8")

            # 为每个 provider 加载环境变量
            for provider_name in providers:
                if provider_name not in PROVIDER_ENV_VARS:
                    continue

                env_vars = PROVIDER_ENV_VARS[provider_name]

                # 加载 API Key
                api_key_var = env_vars["api_key"]
                if not os.environ.get(api_key_var):
                    # 构建正则表达式，匹配 export VAR_NAME="value" 或 export VAR_NAME='value'
                    pattern = _get_env_pattern(api_key_var)
                    match = pattern.search(content)
                    if match:
                        os.environ[api_key_var] = match.group(1)

                # 加载 Base URL
                base_url_var = env_vars["base_url"]
                if not os.environ.get(base_url_var):
                    pattern = _get_env_pattern(base_url_var)
                    match = pattern.search(content)
                    if match:
                        os.environ[base_url_var] = match.group(1)

        except Exception as e:
            logger.error(f"❌ env.py:112: 异常 - {e}\n{traceback.format_exc()}")
            # 静默失败，不影响程序运行
            pass


def get_env_var_for_provider(provider: str, var_type: str) -> str | None:
    """
    获取指定 provider 的环境变量名

    Args:
        provider: provider 名称（如 "zhipu", "dashscope"）
        var_type: 变量类型（"api_key" 或 "base_url"）

    Returns:
        环境变量名，如果 provider 不支持则返回 None

    Examples:
        >>> get_env_var_for_provider("zhipu", "api_key")
        'ZHIPU_AI_API_KEY'
        >>> get_env_var_for_provider("dashscope", "base_url")
        'DASHSCOPE_BASE_URL'
    """
    if provider not in PROVIDER_ENV_VARS:
        return None
    return PROVIDER_ENV_VARS[provider].get(var_type)


def has_any_api_key() -> bool:
    """
    检查是否已设置任意 provider 的 API Key

    Returns:
        True 表示至少有一个 provider 的 API Key 已设置
    """
    for provider_env in PROVIDER_ENV_VARS.values():
        api_key_var = provider_env["api_key"]
        if os.environ.get(api_key_var):
            return True
    return False


def load_env_if_needed(providers: list[str] | None = None) -> bool:
    """
    检查是否需要加载环境变量，如果需要则加载

    Args:
        providers: 要检查的 provider 列表

    Returns:
        True 表示加载了环境变量，False 表示已有环境变量或加载失败
    """
    if has_any_api_key():
        return False  # 已有 API Key，无需加载

    load_env_from_rc(providers)
    return has_any_api_key()  # 返回是否成功加载

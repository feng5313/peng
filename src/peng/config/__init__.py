"""
配置模块

提供配置加载和管理功能。
大模型配置优先级：环境变量 > 配置文件 > 默认值
其他配置仅从配置文件读取。

项目路径获取：
    from peng.config import get_project_root
    root = get_project_root()  # 返回项目根目录 Path 对象

Agent 名称获取：
    from peng.config import get_agent_name
    name = get_agent_name()  # 返回 f"{AGENT_NAME}" 或配置中定义的名称
"""

from peng.config.loader import (LLM_ENV_VARS, AutoModeConfig, LLMConfig,
                                LLMPromptsConfig, ParallelConfig, PathsConfig,
                                PengConfig, ProcessConfig, ProviderConfig,
                                TokenMonitorConfig, TruncationConfig,
                                TruncationHintsConfig, clear_config_cache,
                                get_agent_name, get_master_name,
                                get_llm_config, get_path, get_project_root,
                                is_auto_mode_prompt, load_config,
                                load_config_with_failures)

__all__ = [
    "LLM_ENV_VARS",
    "AutoModeConfig",
    "JSONConfigError",
    "JSONConfigLoader",
    "LLMConfig",
    "LLMPromptsConfig",
    "ParallelConfig",
    "PathsConfig",
    "PengConfig",
    "ProcessConfig",
    "ProviderConfig",
    "TokenMonitorConfig",
    "TruncationConfig",
    "TruncationHintsConfig",
    "clear_config_cache",
    "get_agent_name",
    "get_master_name",
    "get_llm_config",
    "get_path",
    "get_project_root",
    "is_auto_mode_prompt",
    "load_config",
    "load_config_with_failures",
    "load_json_config",
    "save_json_config",
    "update_json_config",
]

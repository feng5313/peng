"""
LLM/模型管理路由模块

提供 LLM Provider 管理、思考模式切换、模型切换、输出历史等功能。

路由前缀: /api (由 server.py 注册)
- /llm/providers, /llm/thinking-mode, /llm/providers/{name}/priority, /llm/providers/{name}/enable
- /models, /models/switch
- /output-history, /input-history
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from peng.common.paths import get_temp_directory, ensure_dir_with_owner
from peng.config import get_path
from peng.webui.routes._common import DEFAULT_OUTPUT_HISTORY_LIMIT, SwitchModelRequest

logger = logging.getLogger(__name__)
router = APIRouter()


# ==================== Pydantic 模型 ====================


class ProviderEnableRequest(BaseModel):
    enable: bool


class ProviderPriorityRequest(BaseModel):
    priority: int


class CreateProviderRequest(BaseModel):
    name: str
    clone: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    small_model: str | None = None
    max_tokens: int | None = None
    thinking_mode: str | None = None
    thinking_type: str | None = None
    compact_threshold: int | None = None
    price_input_hit: float | None = None
    price_input_miss: float | None = None
    price_output: float | None = None
    vision: str | None = None
    enable: bool | None = None
    icon: str | None = None
    priority: int | None = None


class UpdateProviderRequest(BaseModel):
    """更新 provider 配置（所有字段可选，只更新提供的字段）"""

    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    small_model: str | None = None
    max_tokens: int | None = None
    thinking_mode: str | None = None
    thinking_type: str | None = None
    compact_threshold: int | None = None
    price_input_hit: float | None = None
    price_input_miss: float | None = None
    price_output: float | None = None
    vision: str | None = None
    enable: bool | None = None
    icon: str | None = None
    priority: int | None = None
    clone: str | None = None


class ThinkingModeRequest(BaseModel):
    thinking_mode: str


class InputHistoryItem(BaseModel):
    content: str
    timestamp: int


class SaveInputHistoryRequest(BaseModel):
    history: list[InputHistoryItem]


# 输入历史文件路径
try:
    INPUT_HISTORY_FILE = get_path("input_history")
except Exception as e:
    logger.warning(f"获取输入历史文件路径失败: {e}，使用默认路径")
    INPUT_HISTORY_FILE = get_temp_directory() / "peng_input_history.json"


# ==================== peng.json 读写辅助函数（data 目录覆盖层）====================


def _get_peng_json_data_path() -> Path:
    """获取 peng.json 的 data 目录覆盖路径"""
    from peng.common.paths import get_project_root
    return get_project_root() / "data" / "peng.json"


def _load_peng_json_overlay() -> dict:
    """读取 data/peng.json 覆盖层（仅覆盖部分，不合并 config/）"""
    data_path = _get_peng_json_data_path()
    if data_path.exists():
        try:
            with open(data_path, encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            pass
    return {}


def _load_peng_json_merged() -> dict:
    """读取合并后的 peng.json（config 基础 + data 覆盖，deep merge）"""
    from peng.config.loader import _load_config_with_overlay
    from peng.common.paths import get_project_root

    project_root = get_project_root()
    return _load_config_with_overlay(
        project_root / "config" / "peng.json",
        project_root / "data" / "peng.json",
    )


def _save_peng_json_overlay(data: dict) -> None:
    """写入 data/peng.json 覆盖层"""
    from peng.config import clear_config_cache
    from peng.common.paths import ensure_dir_with_owner

    data_path = _get_peng_json_data_path()
    ensure_dir_with_owner(data_path.parent)
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    clear_config_cache(data_path)


# ==================== 路由端点 ====================


@router.get("/llm/providers")
async def get_llm_providers(detail: bool = False) -> dict[str, Any]:
    """获取所有LLM providers及其enable状态"""
    try:
        from peng.config.loader import load_config

        config = load_config()
        providers = []

        # 读取合并后的 peng.json（config + data 覆盖层）获取原始 clone 信息
        merged_config = _load_peng_json_merged()
        raw_providers: dict[str, dict] = merged_config.get("llm", {}).get("providers", {})

        # 🔧 事务#2307：跨进程分配账本快照（同优先级轮换/空闲度），前端可显示
        # 每个provider当前挂了多少活跃事务，验证"轮流使用"是否生效
        try:
            from peng.llm.provider_balance import get_provider_balance

            balance_load = get_provider_balance().snapshot_summary()
        except Exception:
            balance_load = {}

        for name, prov_config in config.llm.providers.items():
            item = {
                "name": name,
                "enable": prov_config.enable,
                "model": prov_config.model,
                "icon": prov_config.icon,
                "priority": prov_config.priority,
                "is_current": name == config.llm.current_provider,
                # 跨进程账本：该provider上的活跃分配数（0=空闲）
                "active_tasks": balance_load.get(name, {}).get("active", 0),
            }
            if detail:
                item.update(
                    {
                        "clone": raw_providers.get(name, {}).get("clone"),
                        "api_key": prov_config.api_key,
                        "base_url": prov_config.base_url,
                        "small_model": prov_config.small_model,
                        "max_tokens": prov_config.max_tokens,
                        "thinking_mode": prov_config.thinking_mode,
                        "thinking_type": prov_config.thinking_type,
                        "compact_threshold": prov_config.compact_threshold,
                        "price_input_hit": prov_config.price_input_hit,
                        "price_input_miss": prov_config.price_input_miss,
                        "price_output": prov_config.price_output,
                        "vision": prov_config.vision,
                    }
                )
            providers.append(item)

        return {"providers": providers, "current_provider": config.llm.current_provider}
    except Exception as e:
        logger.error(f"获取providers失败: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"获取providers失败: {e}")


@router.post("/llm/providers")
async def create_provider(request: CreateProviderRequest) -> dict[str, Any]:
    """创建新的 LLM provider"""
    try:
        from peng.config.loader import load_config

        provider_name = request.name.strip()
        if not provider_name:
            raise HTTPException(status_code=400, detail="Provider名称不能为空")

        config = load_config()
        if provider_name in config.llm.providers:
            raise HTTPException(status_code=409, detail=f"Provider '{provider_name}' 已存在")

        new_provider_config: dict[str, Any] = {}
        clone_source_resolved: dict[str, Any] = {}

        if request.clone:
            if request.clone not in config.llm.providers:
                raise HTTPException(
                    status_code=404, detail=f"Clone源 provider '{request.clone}' 不存在"
                )
            clone_prov = config.llm.providers[request.clone]
            clone_source_resolved = clone_prov.model_dump()
            new_provider_config["clone"] = request.clone

        user_fields = {
            "api_key": request.api_key,
            "base_url": request.base_url,
            "model": request.model,
            "small_model": request.small_model,
            "max_tokens": request.max_tokens,
            "thinking_mode": request.thinking_mode,
            "thinking_type": request.thinking_type,
            "compact_threshold": request.compact_threshold,
            "price_input_hit": request.price_input_hit,
            "price_input_miss": request.price_input_miss,
            "price_output": request.price_output,
            "vision": request.vision,
            "enable": request.enable,
            "icon": request.icon,
            "priority": request.priority,
        }

        if request.clone and clone_source_resolved:
            for field_name, field_value in user_fields.items():
                if field_value is None:
                    continue
                clone_value = clone_source_resolved.get(field_name)
                if field_value == clone_value:
                    continue
                new_provider_config[field_name] = field_value
        else:
            for field_name, field_value in user_fields.items():
                if field_value is not None:
                    new_provider_config[field_name] = field_value

        if not request.clone and "model" not in new_provider_config:
            raise HTTPException(status_code=400, detail="无 clone 时必须指定 model")

        # 写入 data/peng.json 覆盖层
        overlay = _load_peng_json_overlay()
        if "llm" not in overlay:
            overlay["llm"] = {}
        if "providers" not in overlay["llm"]:
            overlay["llm"]["providers"] = {}
        overlay["llm"]["providers"][provider_name] = new_provider_config
        _save_peng_json_overlay(overlay)

        logger.info(f"新 provider '{provider_name}' 已创建: {new_provider_config}")

        return {
            "success": True,
            "provider": provider_name,
            "config": new_provider_config,
            "message": f"Provider '{provider_name}' 已创建",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"创建provider失败: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"创建provider失败: {e}")
@router.put("/llm/thinking-mode")
async def set_thinking_mode(request: ThinkingModeRequest) -> dict[str, Any]:
    """设置全局 thinking_mode"""
    try:
        from peng.config.loader import load_config

        # 合法的思考模式值（支持思考级别）
        _valid_modes = ("auto", "enabled", "disabled", "low", "medium", "high", "xhigh", "max")
        if request.thinking_mode not in _valid_modes:
            raise HTTPException(
                status_code=400, detail=f"无效的thinking_mode: {request.thinking_mode}，合法值: {', '.join(_valid_modes)}"
            )

        config = load_config()
        config.llm.thinking_mode = request.thinking_mode
        # 写入 data/peng.json 覆盖层
        overlay = _load_peng_json_overlay()
        if "llm" not in overlay:
            overlay["llm"] = {}
        overlay["llm"]["thinking_mode"] = request.thinking_mode
        _save_peng_json_overlay(overlay)

        logger.info(f"全局 thinking_mode 已更新为: {request.thinking_mode}")
        return {"success": True, "thinking_mode": request.thinking_mode}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"设置thinking_mode失败: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"设置thinking_mode失败: {e}")


@router.put("/llm/providers/{provider_name}/priority")
async def set_provider_priority(
    provider_name: str, request: ProviderPriorityRequest
) -> dict[str, Any]:
    """设置provider的优先级"""
    try:
        from peng.config.loader import load_config

        if request.priority < 0 or request.priority > 99:
            raise HTTPException(status_code=400, detail="优先级必须在0-99范围内")

        config = load_config()
        if provider_name not in config.llm.providers:
            raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' 不存在")

        # 写入 data/peng.json 覆盖层
        overlay = _load_peng_json_overlay()
        if "llm" not in overlay:
            overlay["llm"] = {}
        if "providers" not in overlay["llm"]:
            overlay["llm"]["providers"] = {}
        if provider_name not in overlay["llm"]["providers"]:
            overlay["llm"]["providers"][provider_name] = {}
        overlay["llm"]["providers"][provider_name]["priority"] = request.priority
        _save_peng_json_overlay(overlay)

        logger.info(f"大模型 {provider_name} priority已更新为: {request.priority}")
        return {
            "success": True,
            "provider": provider_name,
            "priority": request.priority,
            "message": f"大模型 {provider_name} 优先级已设置为 {request.priority}",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"设置provider priority失败: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"设置provider priority失败: {e}")


@router.put("/llm/providers/{provider_name}/enable")
async def set_provider_enable(provider_name: str, request: ProviderEnableRequest) -> dict[str, Any]:
    """设置provider的enable状态"""
    try:
        from peng.config import clear_config_cache, get_project_root
        from peng.config.loader import load_config

        config = load_config()
        if provider_name not in config.llm.providers:
            raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' 不存在")

        # 写入 data/peng.json 覆盖层
        overlay = _load_peng_json_overlay()
        if "llm" not in overlay:
            overlay["llm"] = {}
        if "providers" not in overlay["llm"]:
            overlay["llm"]["providers"] = {}
        if provider_name not in overlay["llm"]["providers"]:
            overlay["llm"]["providers"][provider_name] = {}
        overlay["llm"]["providers"][provider_name]["enable"] = request.enable
        _save_peng_json_overlay(overlay)

        logger.info(f"Provider '{provider_name}' enable状态已更新为: {request.enable}")
        return {
            "success": True,
            "provider": provider_name,
            "enable": request.enable,
            "message": f"Provider '{provider_name}' 已{'启用' if request.enable else '禁用'}",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"设置provider enable状态失败: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"设置provider enable状态失败: {e}")


@router.put("/llm/providers/{provider_name}")
async def update_provider(provider_name: str, request: UpdateProviderRequest) -> dict[str, Any]:
    """更新 provider 配置（只更新提供的字段）"""
    try:
        from peng.config import clear_config_cache, get_project_root
        from peng.config.loader import load_config

        config = load_config()
        if provider_name not in config.llm.providers:
            raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' 不存在")

        # 读取合并后的配置（config + data 覆盖层）用于 clone 比较和验证
        merged_config = _load_peng_json_merged()
        if "llm" not in merged_config or "providers" not in merged_config["llm"]:
            raise HTTPException(status_code=500, detail="配置文件格式错误")

        # 读取 data 覆盖层（只含变化部分）
        overlay = _load_peng_json_overlay()
        if "llm" not in overlay:
            overlay["llm"] = {}
        if "providers" not in overlay["llm"]:
            overlay["llm"]["providers"] = {}

        raw_provider = overlay["llm"]["providers"].setdefault(provider_name, {})

        # 处理 clone：如果有 clone 源，获取其解析后的完整配置用于比较
        clone_source_resolved: dict[str, Any] = {}
        # 确定 clone 源：优先用请求中的 clone，否则用配置中已有的
        clone_name = request.clone if request.clone else raw_provider.get("clone")
        if clone_name == "":
            # 空字符串表示清除继承
            clone_name = None
        if clone_name and clone_name in config.llm.providers:
            clone_source_resolved = config.llm.providers[clone_name].model_dump()

        # 更新 clone 字段
        if request.clone is not None:
            if request.clone == "":
                raw_provider.pop("clone", None)
            else:
                raw_provider["clone"] = request.clone

        # 收集用户提交的字段
        user_fields = {
            "api_key": request.api_key,
            "base_url": request.base_url,
            "model": request.model,
            "small_model": request.small_model,
            "max_tokens": request.max_tokens,
            "thinking_mode": request.thinking_mode,
            "thinking_type": request.thinking_type,
            "compact_threshold": request.compact_threshold,
            "price_input_hit": request.price_input_hit,
            "price_input_miss": request.price_input_miss,
            "price_output": request.price_output,
            "vision": request.vision,
            "enable": request.enable,
            "icon": request.icon,
            "priority": request.priority,
        }

        updated_fields = []
        removed_fields = []

        if clone_source_resolved:
            # 有 clone 源：与 clone 源相同的值不保存
            for field_name, field_value in user_fields.items():
                if field_value is None:
                    continue
                clone_value = clone_source_resolved.get(field_name)
                if field_value == clone_value:
                    # 与 clone 源相同，从覆盖层中移除（不保存）
                    if field_name in raw_provider:
                        del raw_provider[field_name]
                        removed_fields.append(field_name)
                    continue
                raw_provider[field_name] = field_value
                updated_fields.append(field_name)
        else:
            # 无 clone 源：直接保存
            for field_name, field_value in user_fields.items():
                if field_value is not None:
                    raw_provider[field_name] = field_value
                    updated_fields.append(field_name)

        _save_peng_json_overlay(overlay)
        logger.info(
            f"Provider '{provider_name}' 已更新字段: {updated_fields}, 移除与clone相同字段: {removed_fields}"
        )
        return {
            "success": True,
            "provider": provider_name,
            "updated_fields": updated_fields,
            "removed_fields": removed_fields,
            "message": f"Provider '{provider_name}' 已更新",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新provider失败: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"更新provider失败: {e}")


@router.delete("/llm/providers/{provider_name}")
async def delete_provider(provider_name: str) -> dict[str, Any]:
    """删除 provider"""
    try:
        from peng.config import clear_config_cache, get_project_root
        from peng.config.loader import load_config

        config = load_config()
        if provider_name not in config.llm.providers:
            raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' 不存在")

        if config.llm.current_provider == provider_name:
            raise HTTPException(
                status_code=400, detail=f"Provider '{provider_name}' 当前正在使用，无法删除"
            )

        # 删除 provider 需要将整个 providers 列表写入 data 覆盖层
        # 因为 deep merge 会合并 config + data 的 providers 字典，
        # 所以覆盖层的 providers 必须包含完整列表（排除被删除的）
        overlay = _load_peng_json_overlay()
        if "llm" not in overlay:
            overlay["llm"] = {}

        # 读取 config/peng.json 基础的 providers
        from peng.common.paths import get_project_root
        project_root = get_project_root()
        config_peng_path = project_root / "config" / "peng.json"
        base_providers: dict = {}
        if config_peng_path.exists():
            try:
                with open(config_peng_path, encoding="utf-8") as f:
                    base_raw = json.load(f)
                base_providers = dict(base_raw.get("llm", {}).get("providers", {}))
            except Exception:
                pass

        # 合并基础 providers 和已有覆盖层 providers
        all_providers = dict(base_providers)
        all_providers.update(overlay.get("llm", {}).get("providers", {}))

        # 删除指定 provider
        if provider_name in all_providers:
            del all_providers[provider_name]

        overlay["llm"]["providers"] = all_providers
        _save_peng_json_overlay(overlay)

        logger.info(f"Provider '{provider_name}' 已删除")
        return {
            "success": True,
            "provider": provider_name,
            "message": f"Provider '{provider_name}' 已删除",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除provider失败: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"删除provider失败: {e}")


@router.get("/output-history")
async def get_output_history(
    limit: int = DEFAULT_OUTPUT_HISTORY_LIMIT,
    before_timestamp: str | None = None,
    after_timestamp: str | None = None,
    task_id: int | None = None,
    message_id: int | None = None,
) -> dict[str, Any]:
    """获取输出历史（基于时间戳分页）"""
    try:
        if not message_id and task_id:
            from peng.task.manager import TaskManager

            task_manager = TaskManager()
            task = task_manager.get_task(task_id)
            if task:
                message_id = task.get("message_id")

        if message_id:
            from peng.message.manager import MessageManager
            from peng.ui.output_reader import get_output_reader
            from peng.task.manager import TaskManager

            message_manager = MessageManager()
            chain_head = message_manager.get_chain_head(message_id)
            message_chain_id = chain_head.get("id", message_id) if chain_head else message_id

            task_manager = TaskManager()
            tasks = task_manager.list_tasks(message_id=message_id)

            if tasks:
                reader = get_output_reader()
                all_outputs = []
                read_limit = limit * 20

                for task in tasks:
                    tid = task.get("id")
                    if not tid:
                        continue
                    outputs = reader.read_last_n_output(tid, read_limit)
                    all_outputs.extend(outputs)

                all_outputs.sort(key=lambda x: x.get("timestamp", ""))

                if task_id:
                    all_outputs = [o for o in all_outputs if o.get("task_id") == task_id]

                overlap = 2
                total = len(all_outputs)
                if before_timestamp:
                    filtered = [o for o in all_outputs if o.get("timestamp", "") < before_timestamp]
                    page_outputs = filtered[-(limit + overlap) :]
                    has_more = len(filtered) > limit
                    has_next = True
                elif after_timestamp:
                    filtered = [o for o in all_outputs if o.get("timestamp", "") > after_timestamp]
                    page_outputs = filtered[: limit + overlap]
                    has_more = True
                    has_next = len(filtered) > limit
                else:
                    page_outputs = all_outputs[-limit:]
                    has_more = len(all_outputs) > limit
                    has_next = False

                result = []
                for output in page_outputs:
                    source = output.get("source", "")
                    result.append(
                        {
                            "content": output.get("content", ""),
                            "source": source,
                            "taskId": output.get("task_id"),
                            "messageId": output.get("message_id"),
                            "timestamp": output.get("timestamp", ""),
                        }
                    )

                return {
                    "outputs": result,
                    "limit": limit,
                    "total": total,
                    "has_more": has_more,
                    "has_next": has_next,
                }

        return {"outputs": [], "limit": limit, "total": 0, "has_more": False}
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"获取输出历史失败: {e}\n{tb}")
        return {"outputs": [], "limit": limit, "total": 0, "has_more": False}


@router.delete("/output-history")
async def clear_output_history(
    task_id: int | None = None, message_id: int | None = None
) -> dict[str, Any]:
    """清空输出历史"""
    try:
        from peng.ui.output_reader import get_output_reader

        reader = get_output_reader()
        deleted_count = await reader.delete_output(task_ids=[task_id] if task_id else [])
        return {
            "success": True,
            "message": f"已清空 {deleted_count} 个事务的输出",
            "deleted_count": deleted_count,
        }
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"清空输出历史失败: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"清空输出失败: {str(e)}")


@router.get("/models")
async def get_models() -> dict[str, Any]:
    """获取当前模型信息"""
    try:
        from peng.config.loader import load_config

        config = load_config()
        provider_data = config.llm.get_provider()
        return {
            "models": [],
            "current": provider_data.get("model") if provider_data else None,
            "current_provider": config.llm.current_provider,
        }
    except Exception as e:
        logger.error(f"获取模型信息失败: {e}\n{traceback.format_exc()}")
        return {"models": [], "current": None, "current_provider": None}


@router.post("/models/switch")
async def switch_model(request: SwitchModelRequest) -> dict[str, Any]:
    """切换模型（已废弃）"""
    return {
        "success": False,
        "old_model": "",
        "new_model": request.model_name,
        "message": "大模型已采用自动轮选机制，无需手动切换",
    }


def _get_input_history_limit() -> int:
    """从配置获取输入历史最大条数"""
    try:
        from peng.config.loader import load_config
        config = load_config()
        return config.webui.input_history_limit
    except Exception:
        return 500


@router.get("/input-history")
async def get_input_history(limit: int = 0) -> list[InputHistoryItem]:
    """获取输入历史"""
    try:
        actual_limit = limit if limit > 0 else _get_input_history_limit()
        if not INPUT_HISTORY_FILE.exists():
            return []
        with open(INPUT_HISTORY_FILE, encoding="utf-8") as f:
            history = json.load(f)
        if isinstance(history, list) and len(history) > actual_limit:
            return history[-actual_limit:]
        return history if isinstance(history, list) else []
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"获取输入历史失败: {e}\n{tb}")
        return []


@router.post("/input-history")
async def save_input_history(request: SaveInputHistoryRequest) -> dict[str, Any]:
    """保存输入历史"""
    try:
        ensure_dir_with_owner(INPUT_HISTORY_FILE.parent)
        max_limit = _get_input_history_limit()
        if len(request.history) > max_limit:
            history = request.history[-max_limit:]
        else:
            history = request.history

        history_dicts = [{"content": item.content, "timestamp": item.timestamp} for item in history]
        with open(INPUT_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history_dicts, f, ensure_ascii=False, indent=2)
        return {"success": True, "count": len(history)}
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"保存输入历史失败: {e}\n{tb}")
        return {"success": False, "message": f"{e}\n\nTraceback:\n{tb}"}


# ==================== Coding Plan 用量查询 ====================

# 缓存：避免每分钟都请求智谱 API
_coding_plan_cache: dict[str, Any] = {}
_CODING_PLAN_CACHE_TTL = 55  # 秒，略小于前端 60s 轮询
_CODING_PLAN_STALE_MAX = 600  # 秒，请求失败时允许降级返回旧缓存的最长时间（10分钟）
_CODING_PLAN_RETRIES = 2  # 单轮请求失败后的重试次数（总共最多尝试 3 次）
_CODING_PLAN_TIMEOUT = (5, 10)  # (连接超时, 读超时) 秒
_coding_plan_lock = asyncio.Lock()  # 合并发刷新：防止多标签页同时穿透缓存打智谱 API


def _get_coding_plan_api_key() -> str | None:
    """从配置中获取 Coding Plan provider 的 API Key。"""
    try:
        from peng.config.loader import load_config
        config = load_config()
        # 优先找 base_url 包含 "coding" 的 provider
        for name, prov in config.llm.providers.items():
            if "coding" in (prov.base_url or "") and prov.api_key:
                return prov.api_key
        # 回退: 找 bigmodel 的 provider
        for name, prov in config.llm.providers.items():
            if "bigmodel" in (prov.base_url or "") and prov.api_key:
                return prov.api_key
    except Exception:
        pass
    return None


def _generate_jwt(api_key: str) -> str:
    """用智谱 API Key 生成 JWT token。"""
    import jwt as pyjwt
    api_key_id, api_key_secret = api_key.split(".")
    payload = {
        "api_key": api_key_id,
        "exp": int(round(time.time() * 1000)) + 3600000,
        "timestamp": int(round(time.time() * 1000)),
    }
    return pyjwt.encode(
        payload, api_key_secret,
        algorithm="HS256",
        headers={"alg": "HS256", "sign_type": "SIGN"},
    )


def _fetch_coding_plan_raw(api_key: str) -> dict[str, Any] | None:
    """带重试地请求智谱 quota API，返回原始 dict，失败返回 None。"""
    import requests

    token = _generate_jwt(api_key)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    base = "https://open.bigmodel.cn"

    last_err: Exception | None = None
    for attempt in range(_CODING_PLAN_RETRIES + 1):
        if attempt > 0:
            time.sleep(1.5)  # 重试间隔，避开瞬时抖动
        try:
            r = requests.get(
                f"{base}/api/monitor/usage/quota/limit",
                headers=headers,
                timeout=_CODING_PLAN_TIMEOUT,
            )
            if r.status_code == 200 and r.text:
                data = r.json()
                if data.get("success"):
                    return data
                last_err = RuntimeError(f"智谱返回业务失败: {str(data)[:200]}")
            else:
                last_err = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:  # 网络超时/连接失败
            last_err = e
    if last_err:
        logger.warning(f"查询智谱 Coding Plan 用量失败（已重试 {_CODING_PLAN_RETRIES} 次）: {last_err}")
    return None


def _stale_coding_plan_result(now: float) -> dict[str, Any] | None:
    """请求失败时的降级：返回旧缓存数据（stale-if-error），超时太久则返回 None。"""
    ts = _coding_plan_cache.get("ts")
    data = _coding_plan_cache.get("data")
    if not ts or not data:
        return None
    age = now - ts
    if age > _CODING_PLAN_STALE_MAX:
        return None
    result = dict(data)
    result["stale"] = True  # 标记为旧数据，前端可提示"更新延迟"
    return result


@router.get("/llm/coding-plan-usage")
async def get_coding_plan_usage() -> dict[str, Any]:
    """查询智谱 Coding Plan 的当前用量数据。

    返回套餐等级、5h窗口额度、周额度使用率等信息。
    数据缓存 55 秒，避免频繁请求；请求失败自动重试并降级返回旧缓存（stale-if-error）。
    """
    # 检查缓存
    now = time.time()
    if _coding_plan_cache.get("ts") and now - _coding_plan_cache["ts"] < _CODING_PLAN_CACHE_TTL:
        cached = _coding_plan_cache.get("data")
        if cached:
            return cached

    # 合并发刷新：同一时刻多个标签页/客户端只触发一次上游请求
    async with _coding_plan_lock:
        # 双重检查：等锁期间可能已被其他协程刷新
        now = time.time()
        if _coding_plan_cache.get("ts") and now - _coding_plan_cache["ts"] < _CODING_PLAN_CACHE_TTL:
            cached = _coding_plan_cache.get("data")
            if cached:
                return cached

        api_key = _get_coding_plan_api_key()
        if not api_key:
            return {"success": False, "message": "未找到 Coding Plan API Key"}

        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, _fetch_coding_plan_raw, api_key)
        if not raw:
            # 全部重试失败 → 降级返回旧缓存
            stale = _stale_coding_plan_result(now)
            if stale:
                return stale
            return {"success": False, "message": "智谱 API 返回异常"}

        data = raw.get("data", {})
        limits = data.get("limits", [])
        level = data.get("level", "unknown")

        # 提取关键数据
        result: dict[str, Any] = {
            "success": True,
            "level": level,
            "weekly_percentage": None,
            "weekly_reset": None,
            "window_used": None,
            "window_remaining": None,
            "window_percentage": None,
            "window_reset": None,
        }

        for lim in limits:
            if lim.get("type") == "TOKENS_LIMIT":
                result["weekly_percentage"] = lim.get("percentage")
                ts = lim.get("nextResetTime")
                if ts:
                    from datetime import datetime
                    result["weekly_reset"] = datetime.fromtimestamp(ts / 1000).strftime("%H:%M")
            elif lim.get("type") == "TIME_LIMIT":
                result["window_used"] = lim.get("currentValue")
                result["window_remaining"] = lim.get("remaining")
                result["window_percentage"] = lim.get("percentage")
                ts = lim.get("nextResetTime")
                if ts:
                    from datetime import datetime
                    result["window_reset"] = datetime.fromtimestamp(ts / 1000).strftime("%H:%M")

        # 缓存
        _coding_plan_cache["data"] = result
        _coding_plan_cache["ts"] = now
        return result


# ==================== DeepSeek 用量查询 ====================

# 缓存：避免每分钟都请求 DeepSeek API
_deepseek_cache: dict[str, Any] = {}
_DEEPSEEK_CACHE_TTL = 55  # 秒
_DEEPSEEK_STALE_MAX = 600  # 秒，请求失败时允许降级返回旧缓存的最长时间（10分钟）
_DEEPSEEK_RETRIES = 2  # 单轮请求失败后的重试次数
_DEEPSEEK_TIMEOUT = (5, 10)  # (连接超时, 读超时) 秒
_deepseek_lock = asyncio.Lock()  # 合并发刷新


def _get_deepseek_api_key_and_base() -> tuple[str | None, str]:
    """从配置中获取 DeepSeek provider 的 API Key 和 base_url。"""
    try:
        from peng.config.loader import load_config
        config = load_config()
        for name, prov in config.llm.providers.items():
            base = (prov.base_url or "").lower()
            if "deepseek" in base and prov.api_key:
                return prov.api_key, prov.base_url or "https://api.deepseek.com"
    except Exception:
        pass
    return None, "https://api.deepseek.com"


def _get_deepseek_provider_names() -> list[str]:
    """获取所有 DeepSeek provider 的名称列表。"""
    try:
        from peng.config.loader import load_config
        config = load_config()
        names = []
        for name, prov in config.llm.providers.items():
            base = (prov.base_url or "").lower()
            if "deepseek" in base:
                names.append(name)
        return names
    except Exception:
        return []


def _aggregate_deepseek_cost() -> dict[str, float]:
    """计算 DeepSeek 的累计消费和今日消费。

    DeepSeek API 只提供余额（GET /user/balance），没有消费历史 API。
    采用"余额差值追踪法"：
    - 持久化记录充值基线（topped_up_baseline）和首次余额
    - 累计消费 = 充值基线 - 当前余额（当余额上升时说明有充值，更新基线）
    - 今日消费 = 今日基线 - 当前余额

    Returns:
        {"today_cost": float, "total_cost": float}
    """
    from datetime import datetime, timezone, timedelta
    from pathlib import Path

    try:
        from peng.common.paths import get_project_root
        state_file = get_project_root() / "data" / "deepseek_usage_state.json"

        # 北京时间
        bj_tz = timezone(timedelta(hours=8))
        now_bj = datetime.now(bj_tz)
        today_str = now_bj.strftime("%Y-%m-%d")

        # 查询当前余额
        api_key, base_url = _get_deepseek_api_key_and_base()
        if not api_key:
            return {"today_cost": 0.0, "total_cost": 0.0}

        import requests as _requests
        r = _requests.get(
            f"{base_url.rstrip('/')}/user/balance",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if r.status_code != 200:
            return {"today_cost": 0.0, "total_cost": 0.0}

        raw = r.json()
        current_balance = 0.0
        current_topped_up = 0.0
        for info in raw.get("balance_infos", []):
            if info.get("currency") == "CNY":
                current_balance = float(info.get("total_balance", 0))
                current_topped_up = float(info.get("topped_up_balance", 0))
                break

        # 读取持久化状态
        from peng.common import jsonp as json
        state = {}
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
            except Exception:
                state = {}

        total_baseline = state.get("total_baseline")  # 累计消费计算的余额基线
        today_baseline = state.get("today_baseline")  # 今日消费计算的余额基线
        last_date = state.get("last_date")

        # 初始化或余额上升（充值了）→ 更新基线
        if total_baseline is None or current_balance > total_baseline:
            total_baseline = current_balance

        # 每天重置今日基线
        if last_date != today_str:
            today_baseline = current_balance
            last_date = today_str
        elif today_baseline is None or current_balance > today_baseline:
            today_baseline = current_balance

        # 计算消费
        total_cost = round(total_baseline - current_balance, 2)
        today_cost = round(today_baseline - current_balance, 2)

        # 持久化
        state["total_baseline"] = total_baseline
        state["today_baseline"] = today_baseline
        state["last_date"] = last_date
        state["current_balance"] = current_balance
        state["updated_at"] = datetime.now(bj_tz).isoformat()
        try:
            state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        except Exception as e:
            logger.warning(f"保存 DeepSeek 用量状态失败: {e}")

        return {"today_cost": max(today_cost, 0.0), "total_cost": max(total_cost, 0.0)}
    except Exception as e:
        logger.error(f"计算 DeepSeek 费用失败: {e}")
        return {"today_cost": 0.0, "total_cost": 0.0}


def _is_deepseek_peak() -> bool:
    """判断当前是否为 DeepSeek 高峰时段。

    DeepSeek 高峰时段：北京时间每日 9:00-12:00 和 14:00-18:00，其他时间为低谷。
    """
    from datetime import datetime, timezone, timedelta

    bj_tz = timezone(timedelta(hours=8))
    now_bj = datetime.now(bj_tz)
    hour = now_bj.hour
    return (9 <= hour < 12) or (14 <= hour < 18)


def _fetch_deepseek_raw(api_key: str, base_url: str) -> dict[str, Any] | None:
    """带重试地请求 DeepSeek 余额 API，返回原始 dict，失败返回 None。"""
    import requests

    url = f"{base_url.rstrip('/')}/user/balance"
    headers = {"Authorization": f"Bearer {api_key}"}

    last_err: Exception | None = None
    for attempt in range(_DEEPSEEK_RETRIES + 1):
        if attempt > 0:
            time.sleep(1.5)
        try:
            r = requests.get(url, headers=headers, timeout=_DEEPSEEK_TIMEOUT)
            if r.status_code == 200 and r.text:
                return r.json()
            last_err = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            last_err = e
    if last_err:
        logger.warning(f"查询 DeepSeek 余额失败（已重试 {_DEEPSEEK_RETRIES} 次）: {last_err}")
    return None


@router.get("/llm/deepseek-usage")
async def get_deepseek_usage() -> dict[str, Any]:
    """查询 DeepSeek provider 的消耗数据。

    返回余额、今日费用、累计费用、是否高峰时段。
    数据缓存 55 秒；请求失败自动重试并降级返回旧缓存（stale-if-error）。
    """
    # 检查缓存
    now = time.time()
    if _deepseek_cache.get("ts") and now - _deepseek_cache["ts"] < _DEEPSEEK_CACHE_TTL:
        cached = _deepseek_cache.get("data")
        if cached:
            return cached

    async with _deepseek_lock:
        # 双重检查：等锁期间可能已被其他协程刷新
        now = time.time()
        if _deepseek_cache.get("ts") and now - _deepseek_cache["ts"] < _DEEPSEEK_CACHE_TTL:
            cached = _deepseek_cache.get("data")
            if cached:
                return cached

        api_key, base_url = _get_deepseek_api_key_and_base()
        if not api_key:
            return {"success": False, "message": "未找到 DeepSeek provider"}

        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, _fetch_deepseek_raw, api_key, base_url)
        if not raw:
            # 全部重试失败 → 降级返回旧缓存
            cached_ts = _deepseek_cache.get("ts")
            cached = _deepseek_cache.get("data")
            if cached_ts and cached and now - cached_ts <= _DEEPSEEK_STALE_MAX:
                result = dict(cached)
                result["stale"] = True
                return result
            return {"success": False, "message": "DeepSeek API 返回异常"}

        # 提取 CNY 余额
        balance_cny = None
        balance_usd = None
        for info in raw.get("balance_infos", []):
            if info.get("currency") == "CNY":
                balance_cny = float(info.get("total_balance", 0))
            elif info.get("currency") == "USD":
                balance_usd = float(info.get("total_balance", 0))

        # 计算费用（余额差值法，会自动查余额并持久化）
        cost_data = await loop.run_in_executor(None, _aggregate_deepseek_cost)

        # 高峰时段
        is_peak = await loop.run_in_executor(None, _is_deepseek_peak)

        result: dict[str, Any] = {
            "success": True,
            "is_available": raw.get("is_available", True),
            "balance_cny": balance_cny,
            "balance_usd": balance_usd,
            "today_cost": cost_data["today_cost"],
            "total_cost": cost_data["total_cost"],
            "is_peak": is_peak,
        }

        # 缓存
        _deepseek_cache["data"] = result
        _deepseek_cache["ts"] = now
        return result


@router.put("/llm/deepseek-usage/correct")
async def correct_deepseek_total_cost(body: dict[str, Any]) -> dict[str, Any]:
    """手动修正 DeepSeek 累计消费。

    用户点击累计值编辑后，更新 total_baseline 使后续在此基础上继续累加。
    new_baseline = corrected_total_cost + current_balance
    """
    corrected = body.get("total_cost")
    if corrected is None:
        raise HTTPException(status_code=400, detail="缺少 total_cost 参数")

    try:
        corrected = float(corrected)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="total_cost 必须为数字")

    try:
        from pathlib import Path
        from peng.common.paths import get_project_root
        from peng.common import jsonp as json
        from datetime import datetime, timezone, timedelta

        state_file = get_project_root() / "data" / "deepseek_usage_state.json"
        state = {}
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
            except Exception:
                state = {}

        # 查当前余额来计算新基线
        current_balance = state.get("current_balance", 0.0)
        new_baseline = round(corrected + current_balance, 2)

        state["total_baseline"] = new_baseline
        state["updated_at"] = datetime.now(timezone(timedelta(hours=8))).isoformat()
        state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False))

        # 清除缓存使下次请求重新计算
        _deepseek_cache.clear()

        return {"success": True, "total_baseline": new_baseline, "total_cost": corrected}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"修正失败: {e}")

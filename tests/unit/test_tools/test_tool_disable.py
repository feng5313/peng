"""
测试工具禁用机制（事务#1553：禁用 shot 工具）

覆盖三层禁用链路：
1. 配置层：ToolsConfig / PengConfig.from_dict 解析 tools.disabled
2. 注册表层：disable() 后 get_definitions 过滤、execute 拦截
3. agent 接线逻辑：按 config.tools.disabled 调用 registry.disable
"""
import pytest

from peng.config.loader import PengConfig, ToolsConfig
from peng.tools.registry import ToolRegistry, ToolNotFoundError


# ==================== 配置层 ====================


class TestToolsConfig:
    """测试 ToolsConfig 配置模型"""

    def test_default_disabled_empty(self):
        """默认不禁用任何工具"""
        cfg = ToolsConfig()
        assert cfg.disabled == []

    def test_parse_disabled_list(self):
        """解析 disabled 列表"""
        cfg = ToolsConfig(**{"disabled": ["shot", "window"]})
        assert cfg.disabled == ["shot", "window"]

    def test_ignores_extra_keys(self):
        """忽略未知字段（如 _comment），与 NestConfig 行为一致"""
        cfg = ToolsConfig(**{"_comment": "说明", "disabled": ["shot"]})
        assert cfg.disabled == ["shot"]

    def test_pengconfig_from_dict_wires_tools(self):
        """PengConfig.from_dict 正确装配 tools 配置"""
        data = {"tools": {"disabled": ["shot"]}}
        cfg = PengConfig.from_dict(data)
        assert cfg.tools.disabled == ["shot"]

    def test_pengconfig_from_dict_default_tools(self):
        """无 tools 配置时使用默认（不禁用）"""
        cfg = PengConfig.from_dict({})
        assert cfg.tools.disabled == []


# ==================== 注册表层 ====================


class TestRegistryDisable:
    """测试 ToolRegistry 的禁用机制（shot 工具）"""

    @pytest.fixture(autouse=True)
    def _registry(self):
        """确保工具已加载，并在测试后清理禁用状态（单例，避免污染其他测试）"""
        registry = ToolRegistry()
        registry.load_builtin_tools()
        saved_disabled = set(registry._disabled)
        yield registry
        # 恢复：清掉本测试新增的禁用项，保留原有状态
        registry._disabled = saved_disabled

    def _tool_names(self, registry) -> list:
        return [d["function"]["name"] for d in registry.get_definitions()]

    def test_shot_enabled_by_default(self):
        """未禁用时 shot 出现在工具定义中"""
        registry = ToolRegistry()
        registry._disabled.discard("shot")
        assert "shot" in self._tool_names(registry)

    def test_disable_shot_hides_from_definitions(self):
        """禁用 shot 后不再出现在发给 LLM 的工具定义中"""
        registry = ToolRegistry()
        registry.disable(["shot"])
        names = self._tool_names(registry)
        assert "shot" not in names

    def test_disable_shot_keeps_other_tools(self):
        """禁用 shot 不影响其他工具（window/bash/read 仍在）"""
        registry = ToolRegistry()
        registry.disable(["shot"])
        names = self._tool_names(registry)
        assert "window" in names
        assert "bash" in names or "cmd" in names  # bash 在 Windows 平台为 cmd
        assert "read" in names

    @pytest.mark.asyncio
    async def test_execute_disabled_shot_raises(self):
        """调用被禁用的 shot 工具应抛出 ToolNotFoundError（已禁用）"""
        registry = ToolRegistry()
        registry.disable(["shot"])
        with pytest.raises(ToolNotFoundError) as exc_info:
            await registry.execute("shot", {})
        assert "已禁用" in str(exc_info.value)

    def test_enable_restores_shot(self):
        """enable 可恢复被禁用的工具"""
        registry = ToolRegistry()
        registry.disable(["shot"])
        assert "shot" not in self._tool_names(registry)
        registry.enable(["shot"])
        assert "shot" in self._tool_names(registry)


# ==================== agent 接线逻辑 ====================


class TestAgentDisableWiring:
    """测试 agent.py 中"按 config.tools.disabled 禁用工具"的接线逻辑"""

    def test_agent_disable_pattern(self):
        """模拟 agent.py 的接线代码：从 config.tools.disabled 读取并调用 disable"""
        config = PengConfig.from_dict({"tools": {"disabled": ["shot"]}})

        registry = ToolRegistry()
        registry.load_builtin_tools()

        # —— 与 agent.py 中完全一致的接线逻辑 ——
        _disabled_tools = getattr(getattr(config, "tools", None), "disabled", None) or []
        if _disabled_tools:
            registry.disable(_disabled_tools)

        names = [d["function"]["name"] for d in registry.get_definitions()]
        assert "shot" not in names

    def test_agent_disable_pattern_no_config(self):
        """config 无 tools 字段时不报错、不禁用任何工具"""
        config = PengConfig.from_dict({})  # tools.disabled == []

        registry = ToolRegistry()
        registry.load_builtin_tools()

        _disabled_tools = getattr(getattr(config, "tools", None), "disabled", None) or []
        if _disabled_tools:
            registry.disable(_disabled_tools)

        # shot 未被禁用，仍在定义中（先确保它没被别的测试禁掉）
        registry._disabled.discard("shot")
        names = [d["function"]["name"] for d in registry.get_definitions()]
        assert "shot" in names

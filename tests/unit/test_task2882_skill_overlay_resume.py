"""事务#2882回归测试：历史恢复路径必须重建 task级技能 overlay

根因：run_stream 从 task.context 恢复 history 时跳过 _prepare_prompt，而
_task_skills_overlay 只在 _prepare_prompt 内构建，恢复运行的进程里它恒为空 dict
→ tools.set_context(skill_overlay={}) → skill 工具只能命中全局技能，于是系统
提示词 discovery 已列出的工作目录技能（如 fcc 的 skills/debug-server）报"未找到"。
"""

import pytest

import peng.tools.skill as skill_mod
from peng.llm.agent import PengAgent
from peng.tools.skill import SKILL_TOOL


class _Meta:
    def __init__(self, name):
        self.name = name


class _Skill:
    def __init__(self, name, content):
        self.metadata = _Meta(name)
        self.content = content


class _Loader:
    """全局 SkillLoader 替身：只提供 skill 工具用到的 get/list_skills"""

    def __init__(self, names):
        self._names = list(names)

    def list_skills(self):
        return list(self._names)

    def get(self, name, overlay=None):
        if overlay and name in overlay:
            return overlay[name]
        return _Skill(name, "GLOBAL_CONTENT") if name in self._names else None


class _Dummy:
    """PengAgent 技能 overlay 相关逻辑的轻量 self 替身（不触发重量级 __init__）"""

    def __init__(self, overlay=None):
        self._task_skills_overlay = overlay or {}
        self.built = []

    async def _build_task_skills_overlay(self, task):
        self.built.append(task.get("id"))
        return {"debug-server": _Skill("debug-server", "WORKDIR_CONTENT")}


@pytest.fixture
def tool_ctx(monkeypatch):
    """隔离全局 skill loader 与 SKILL_TOOL 上下文（模块级单例，测试间必须还原）"""
    monkeypatch.setattr(skill_mod, "_skill_loader", None)
    SKILL_TOOL._context.clear()
    yield monkeypatch
    SKILL_TOOL._context.clear()


class TestEnsureOverlay:
    async def test_empty_overlay_is_rebuilt(self):
        """恢复路径（overlay为空）必须重建，否则工作目录技能无法解析（#2882根因）"""
        dummy = _Dummy()
        overlay = await PengAgent._ensure_task_skills_overlay(dummy, {"id": 7})
        assert dummy.built == [7]
        assert "debug-server" in overlay

    async def test_existing_overlay_not_rebuilt(self):
        """_prepare_prompt 已构建时不重复收集，避免多余的 nest 请求"""
        dummy = _Dummy(overlay={"a": _Skill("a", "A")})
        overlay = await PengAgent._ensure_task_skills_overlay(dummy, {"id": 7})
        assert dummy.built == []
        assert "a" in overlay


class TestSkillToolResolution:
    def _setup(self, monkeypatch, global_names, overlay):
        loader = _Loader(global_names)
        monkeypatch.setattr(skill_mod, "_skill_loader", loader)
        SKILL_TOOL.set_context(task_id=1, skill_overlay=overlay)

    async def test_workdir_skill_resolved_from_overlay(self, tool_ctx):
        """工作目录技能（overlay）必须可加载，且优先于同名全局技能"""
        self._setup(
            tool_ctx, ["pexpect"], {"debug-server": _Skill("debug-server", "WORKDIR_CONTENT")}
        )
        out = await SKILL_TOOL.execute("debug-server")
        assert "WORKDIR_CONTENT" in out

    async def test_global_skill_still_resolved(self, tool_ctx):
        """无 overlay 时全局技能照常解析（原行为不回归）"""
        self._setup(tool_ctx, ["pexpect"], {})
        out = await SKILL_TOOL.execute("pexpect")
        assert "GLOBAL_CONTENT" in out

    async def test_error_message_lists_overlay_skills(self, tool_ctx):
        """报错清单须同时含全局与工作目录技能，避免误导排查（#2882）"""
        self._setup(tool_ctx, ["pexpect"], {"debug-server": _Skill("debug-server", "W")})
        out = await SKILL_TOOL.execute("nonexistent")
        assert "pexpect" in out
        assert "debug-server" in out

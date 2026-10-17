"""事务#2242回归测试：nest工作目录AGENTS.md追加到系统提示词尾部

需求（消息#10546）：事务启动/重启时，读取 nest远端 工作目录的 AGENTS.md
（如果存在），添加到系统提示词尾部。

变更前（事务#1672）：工作目录AGENTS.md"优先替代"主系统提示词——
有工作目录AGENTS.md时跳过全局AGENTS.md/prompts.jsonp system_prompt。
变更后（事务#2242）：主系统提示词照常构建（全局AGENTS.md→回退
prompts.jsonp system_prompt），工作目录AGENTS.md追加到系统提示词尾部
（task_prompt之后，最靠近对话、注意力优先级最高的位置）。

注入时机：run_stream在context为空/恢复失败（=事务启动/重启清空上下文）
时调用_prepare_prompt重建系统提示词，恢复context时跳过（提示词已在
历史中，不重复注入）——本测试直接验证_prepare_prompt的构成与顺序。
"""

from peng.llm.agent import PengAgent
from peng.llm.tool_engine import MessageHistory

SKILLS_SENTINEL = "SKILLS_PROMPT_SENTINEL_2242"


class _DummyAgent:
    """轻量self替身：不触发PengAgent.__init__的重量级依赖（参照test_task2105风格）

    只提供_prepare_prompt实际访问的成员：
    history / _task_skills_overlay / _load_agent_md /
    _build_task_skills_overlay / _get_skills_discovery_prompt
    """

    def __init__(self, agent_md: str = ""):
        self.history = MessageHistory()
        self._task_skills_overlay = {}
        self._agent_md = agent_md

    def _load_agent_md(self) -> str:
        return self._agent_md

    async def _build_task_skills_overlay(self, task) -> dict:
        return {}

    def _get_skills_discovery_prompt(self, overlay=None) -> str:
        return SKILLS_SENTINEL


async def _run_prepare_prompt(dummy, task) -> str:
    await PengAgent._prepare_prompt(dummy, task)
    return dummy.history.get_system()


class TestWorkdirAgentsMdAppend:
    """工作目录AGENTS.md必须追加到系统提示词尾部（而非替代主系统提示词）"""

    async def test_workdir_agents_md_appended_to_tail(self, tmp_path):
        """工作目录有AGENTS.md：全局在前、skills/task_prompt居中、工作目录内容在尾部"""
        (tmp_path / "AGENTS.md").write_text("WORKDIR_MD_RULES_2242", encoding="utf-8")
        dummy = _DummyAgent(agent_md="GLOBAL_MD_RULES_2242")
        task = {"nest": "local", "workdir": str(tmp_path)}

        system = await _run_prepare_prompt(dummy, task)

        # 全局AGENTS.md不被替代（#1672替代语义→#2242追加语义的核心断言）
        assert "GLOBAL_MD_RULES_2242" in system
        # 工作目录AGENTS.md存在且追加在尾部（add_system按\n拼接，最后一次即最后一行）
        assert "WORKDIR_MD_RULES_2242" in system
        assert system.split("\n")[-1] == "WORKDIR_MD_RULES_2242"
        # 顺序：全局在前，工作目录在后
        assert system.index("GLOBAL_MD_RULES_2242") < system.index("WORKDIR_MD_RULES_2242")
        # skills提示词仍居中（在全局之后、工作目录之前）
        assert system.index(SKILLS_SENTINEL) > system.index("GLOBAL_MD_RULES_2242")
        assert system.index(SKILLS_SENTINEL) < system.index("WORKDIR_MD_RULES_2242")

    async def test_workdir_without_agents_md_no_append(self, tmp_path):
        """工作目录无AGENTS.md：系统提示词照常（全局+skills+task_prompt），无追加段"""
        dummy = _DummyAgent(agent_md="GLOBAL_MD_RULES_2242")
        task = {"nest": "local", "workdir": str(tmp_path)}  # 空目录

        system = await _run_prepare_prompt(dummy, task)

        assert "GLOBAL_MD_RULES_2242" in system
        assert SKILLS_SENTINEL in system
        # 尾部为task_prompt（无工作目录AGENTS.md追加段）
        assert system.split("\n")[-1] not in ("", "WORKDIR_MD_RULES_2242")
        # 全局AGENTS.md缺失时回退prompts.jsonp system_prompt（同一Dummy路径验证回退链）
        dummy2 = _DummyAgent(agent_md="")
        system2 = await _run_prepare_prompt(dummy2, task)
        assert system2 and not system2.startswith("GLOBAL_MD_RULES_2242")

    async def test_same_content_no_duplicate(self, tmp_path):
        """workdir==project_root等场景（两处AGENTS.md内容相同）不重复注入"""
        (tmp_path / "AGENTS.md").write_text("SAME_MD_2242", encoding="utf-8")
        dummy = _DummyAgent(agent_md="SAME_MD_2242")
        task = {"nest": "local", "workdir": str(tmp_path)}

        system = await _run_prepare_prompt(dummy, task)

        assert system.count("SAME_MD_2242") == 1

    async def test_remote_nest_agents_md_appended_to_tail(self, tmp_path, monkeypatch):
        """远程nest：load_task_agents_md返回内容时同样追加到尾部（注入逻辑与nest无关）"""

        async def _fake_load(task):
            assert task.get("nest") == "epyc"
            return "REMOTE_MD_RULES_2242"

        import peng.llm.client as client_mod

        monkeypatch.setattr(client_mod, "load_task_agents_md", _fake_load)
        dummy = _DummyAgent(agent_md="GLOBAL_MD_RULES_2242")
        # workdir在本地不存在：remote路径不走本地直读，完全依赖mocked加载
        task = {"nest": "epyc", "workdir": "/nonexistent/remote/workdir"}

        system = await _run_prepare_prompt(dummy, task)

        assert "GLOBAL_MD_RULES_2242" in system
        assert "REMOTE_MD_RULES_2242" in system
        assert system.split("\n")[-1] == "REMOTE_MD_RULES_2242"

    async def test_remote_nest_load_failure_ignored(self, monkeypatch):
        """远程nest读取失败：静默忽略，系统提示词照常构建，不抛异常"""

        async def _bad_load(task):
            raise RuntimeError("nest down")

        import peng.llm.client as client_mod

        monkeypatch.setattr(client_mod, "load_task_agents_md", _bad_load)
        dummy = _DummyAgent(agent_md="GLOBAL_MD_RULES_2242")
        task = {"nest": "epyc", "workdir": "/nonexistent/remote/workdir"}

        system = await _run_prepare_prompt(dummy, task)  # 不应抛异常

        assert "GLOBAL_MD_RULES_2242" in system
        assert system.split("\n")[-1] != ""  # 尾部为task_prompt等正常内容

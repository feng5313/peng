"""
Skill 加载工具

实现 Agent Skills 标准的 Progressive Disclosure：
- LLM 调用此工具按需加载 skill 的完整内容
- 避免一次性注入所有 skill，节省 token
"""

from typing import Any

from peng.tools.base import Tool

# 全局 SkillLoader 引用（由 Agent 初始化时设置)
_skill_loader = None


def set_skill_loader(loader) -> None:
    """
    设置全局 skill loader

    Args:
        loader: SkillLoader 实例
    """
    global _skill_loader
    _skill_loader = loader


class SkillTool(Tool):
    """
    Skill 加载工具

    让 LLM 可以按需加载某个 skill 的完整内容。
    符合 Agent Skills 标准的 Progressive Disclosure 模式。
    """

    @property
    def name(self) -> str:
        return "skill"

    @property
    def description(self) -> str:
        return """加载指定技能的完整内容。

当你需要了解某个技能的详细使用方法、API、示例时调用此工具。
技能名称可以从"可用技能"列表中获取。

使用场景：
1. 用户提到需要使用某个特定功能（如 pexpect、ansi 处理）
2. 你需要了解如何正确使用某个技能
3. 事务可能涉及某个技能提供的专业能力

参数：
- name: 技能名称（如 'pexpect', 'ansi-tools'）
"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "要加载的技能名称，如 'pexpect', 'ansi-tools'",
                }
            },
            "required": ["name"],
        }

    async def execute(self, name: str) -> str:
        """
        加载指定 skill 的完整内容

        Args:
            name: Skill 名称

        Returns:
            Skill 的完整内容，或错误信息
        """
        if not _skill_loader:
            return "错误：Skill 系统未初始化"

        skill = _skill_loader.get(name)
        if not skill:
            available = ", ".join(_skill_loader.list_skills())
            return f"错误：未找到 skill '{name}'。可用技能：{available}"

        # 返回完整 skill 内容
        return f"# Skill: {skill.metadata.name}\n\n{skill.content}"


# 导出工具实例
SKILL_TOOL = SkillTool()

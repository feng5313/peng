"""
Skills模块

提供Skills加载和管理功能。

使用示例:
    from peng.skills import SkillLoader

    # 初始化加载器，指定技能目录
    loader = SkillLoader(directories=["/path/to/skills"])

    # 加载所有技能
    loader.load_all()

    # 列出所有可用技能
    skills = loader.list_skills()
    print(f"可用技能: {skills}")

    # 获取技能提示词
    prompt = loader.get_skill_prompt("message")
    print(prompt)
"""

from peng.skills.loader import Skill, SkillLoader, SkillMetadata

__all__ = ["Skill", "SkillLoader", "SkillMetadata"]

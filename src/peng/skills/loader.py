"""
Skills加载器

加载和管理技能（Skills）。
遵循 Agent Skills 开放标准 (https://agentskills.io)
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from peng.common import MetaSingleton

logger = logging.getLogger(__name__)


@dataclass
class SkillMetadata:
    """Skill元数据（遵循 Agent Skills 标准）"""

    name: str
    description: str = ""  # 用于 LLM 语义匹配
    version: str = "1.0.0"
    author: str = ""
    enabled: bool = True
    priority: int = 100
    timeout: float = 30.0


@dataclass
class Skill:
    """Skill定义"""

    metadata: SkillMetadata
    content: str  # SKILL.md的内容
    path: Path
    handler: Callable | None = None


class SkillLoader(metaclass=MetaSingleton):
    """
    Skills加载器

    负责发现、加载和管理Skills。
    遵循 Agent Skills 开放标准，通过 description 让 LLM 自主判断匹配。
    """

    # SKILL.md的frontmatter解析模式
    FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)

    def __init__(self, directories: list[str] | None = None):
        """
        初始化Skills加载器

        Args:
            directories: Skills目录列表
        """
        self._directories: list[Path] = []
        self._skills: dict[str, Skill] = {}

        if directories:
            for d in directories:
                self.add_directory(d)

    def add_directory(self, path: str | Path) -> None:
        """
        添加Skills目录

        Args:
            path: 目录路径
        """
        p = Path(path).expanduser().resolve()
        if p.exists() and p.is_dir():
            self._directories.append(p)

    def discover(self) -> list[str]:
        """
        发现所有Skills

        Returns:
            发现的Skill名称列表
        """
        discovered = []
        for directory in self._directories:
            for skill_dir in directory.iterdir():
                if skill_dir.is_dir():
                    from peng.config import load_config

                    config = load_config()
                    skill_md_name = config.paths.skill_md_name
                    skill_file = skill_dir / skill_md_name
                    if skill_file.exists():
                        discovered.append(skill_dir.name)
        return discovered

    def load_all(self) -> dict[str, Skill]:
        """
        加载所有Skills（并行）

        Returns:
            加载的Skills映射
        """
        from concurrent.futures import ThreadPoolExecutor

        names = self.discover()
        if not names:
            return self._skills

        def _load_one(name: str) -> tuple[str, Skill | None]:
            try:
                return (name, self.load(name))
            except Exception as e:
                import traceback

                logger.error(
                    f"❌ _load_one() - loader.py:108: 异常 - {e}\n{traceback.format_exc()}"
                )
                return (name, None)

        # 并行加载多个skills
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(_load_one, names))

        for name, skill in results:
            if skill:
                self._skills[name] = skill

        return self._skills

    def load(self, name: str) -> Skill | None:
        """加载指定Skill（在已注册的 _directories 中查找）"""
        for directory in self._directories:
            skill_dir = directory / name
            if (skill_dir / self._skill_md_name()).exists():
                return self._build_skill_from_dir(skill_dir, name)
        return None

    def _skill_md_name(self) -> str:
        """获取 SKILL.md 文件名（从配置读取，默认 SKILL.md）"""
        from peng.config import load_config
        return load_config().paths.skill_md_name

    def _build_skill_from_dir(self, skill_dir: Path, name: str) -> Skill | None:
        """从指定 skill 目录构建 Skill（读 SKILL.md、frontmatter、handler、scripts/references）"""
        skill_file = skill_dir / self._skill_md_name()
        if not skill_file.exists():
            return None
        files = self.list_skill_files(skill_dir)
        files_str = (
            f"\n相关脚本/文件路径：\n{'\n'.join(files['scripts'] + files['references'])}\n"
            if (files["scripts"] or files["references"]) else ""
        )
        content = skill_file.read_text(encoding="utf-8") + files_str
        metadata = self._parse_frontmatter(content, name)
        handler = self._load_handler(skill_dir)  # 失败返回None，不阻断
        return Skill(metadata=metadata, content=content, path=skill_dir, handler=handler)


    def list_skill_files(self, skill_dir: Path | None = None) -> dict:
        """列出指定 skill 目录下 scripts/references 子目录的文件绝对路径。

        Args:
            skill_dir: skill 目录；None时回退到 loader.py 所在包目录（兼容旧调用）。

        Returns:
            dict: {"scripts": [...], "references": [...], "scripts_import_path": ...}

        修复：旧实现忽略实际 skill_dir，硬编码为 loader.py 位置，导致所有 skill 脚本路径错误。
        """
        import os

        if skill_dir is None:
            # 回退旧行为：loader.py 所在包上级目录（peng/src/peng/tools）
            skill_dir = Path(os.path.dirname(os.path.dirname(__file__)))

        scripts_dir = os.path.join(str(skill_dir), "scripts")
        references_dir = os.path.join(str(skill_dir), "references")

        result = {
            "scripts": [],
            "references": [],
            "scripts_import_path": scripts_dir,
        }

        # 列出 scripts 子目录文件（目录不存在则跳过）
        if os.path.isdir(scripts_dir):
            for item in os.listdir(scripts_dir):
                item_path = os.path.join(scripts_dir, item)
                if os.path.isfile(item_path):
                    result["scripts"].append(os.path.abspath(item_path))

        # 列出 references 子目录文件（目录不存在则跳过）
        if os.path.isdir(references_dir):
            for item in os.listdir(references_dir):
                item_path = os.path.join(references_dir, item)
                if os.path.isfile(item_path):
                    result["references"].append(os.path.abspath(item_path))

        return result

    def discover_in(self, base_dir: Path | str) -> list[str]:
        """仅扫描指定 base_dir 下的 skills（不触碰 self._directories），返回名称列表。

        Args:
            base_dir: skills 根目录（其下每个含 SKILL.md 的子目录为一个 skill）

        Returns:
            发现的 skill 名称列表；base_dir不存在或无skill时返回空列表。扫描异常记录日志并返回空列表。
        """
        base = Path(base_dir).expanduser().resolve() if base_dir else None
        if not base or not base.is_dir():
            return []

        skill_md_name = self._skill_md_name()
        discovered = []
        try:
            for entry in base.iterdir():
                if entry.is_dir() and (entry / skill_md_name).exists():
                    discovered.append(entry.name)
        except Exception as e:
            logger.warning(f"discover_in() - {base} 扫描失败(忽略): {e}")
            return []

        return discovered

    def collect_external_skills(self, base_dirs: list[Path | str]) -> dict[str, Skill]:
        """从外部 skills 源（工作nest/工作目录的 skills/）收集 Skill 映射（纯函数，不修改 self._skills/_directories）。

        SkillLoader 是进程级 MetaSingleton，被同进程多个 Agent 共享；task级技能必须用
        per-agent overlay（独立 dict）承载并作为参数传入 get_discovery_prompt/get，
        绝不能写入共享的 _skills/_directories，否则会把某任务工作目录的技能泄漏给同进程其他 Agent。

        Args:
            base_dirs: skills 根目录列表（每个目录下每个含 SKILL.md 的子目录为一个 skill）

        Returns:
            {skill_name: Skill}：收集到的技能映射（同名后者覆盖前者）。单个源加载失败静默跳过。调用方负责捕获整体异常。
        """
        collected: dict[str, Skill] = {}

        for base in base_dirs:
            base_path = Path(base).expanduser().resolve() if base else None
            if not base_path or not base_path.is_dir():
                continue

            for name in self.discover_in(base_path):
                try:
                    skill = self._build_skill_from_dir(base_path / name, name)
                except Exception as e:
                    logger.warning(f"collect_external_skills() - {base_path / name} 加载失败(忽略): {e}")
                    continue

                if skill is None:
                    continue

                collected[name] = skill

        return collected



    def _parse_frontmatter(self, content: str, default_name: str) -> SkillMetadata:
        """
        解析SKILL.md的frontmatter

        Args:
            content: 文件内容
            default_name: 默认名称

        Returns:
            Skill元数据
        """
        match = self.FRONTMATTER_PATTERN.match(content)
        if not match:
            return SkillMetadata(name=default_name)

        # 简单的YAML frontmatter解析（不依赖pyyaml库）
        frontmatter_text = match.group(1)
        frontmatter = self._parse_simple_yaml(frontmatter_text)

        return SkillMetadata(
            name=frontmatter.get("name", default_name),
            description=frontmatter.get("description", ""),
            version=frontmatter.get("version", "1.0.0"),
            author=frontmatter.get("author", ""),
            enabled=frontmatter.get("enabled", True),
            priority=frontmatter.get("priority", 100),
            timeout=frontmatter.get("timeout", 30.0),
        )

    def _parse_simple_yaml(self, text: str) -> dict:
        """
        简单的YAML解析器（仅支持 key: value 格式）

        Args:
            text: YAML文本

        Returns:
            解析后的字典
        """
        result = {}
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip()
                # 处理引号
                if (value.startswith('"') and value.endswith('"')) or (
                    value.startswith("'") and value.endswith("'")
                ):
                    value = value[1:-1]
                # 处理布尔值
                elif value.lower() == "true":
                    value = True
                elif value.lower() == "false":
                    value = False
                # 处理数字
                elif value.isdigit():
                    value = int(value)
                elif value.replace(".", "", 1).isdigit():
                    value = float(value)
                # 处理列表（简单格式：[a, b, c]）
                elif value.startswith("[") and value.endswith("]"):
                    value = [v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()]
                result[key] = value
        return result

    def _load_handler(self, skill_dir: Path) -> Callable | None:
        """
        加载Skill的Python handler

        Args:
            skill_dir: Skill目录

        Returns:
            Handler函数，失败返回None
        """
        handler_file = skill_dir / "skill.py"
        if not handler_file.exists():
            return None

        try:
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                f"skill_{skill_dir.name}",
                handler_file,
            )
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return getattr(module, "handle", None)
        except Exception as e:
            import traceback

            logger.error(
                f"❌ _load_handler() - loader.py:246: 异常 - {e}\n{traceback.format_exc()}"
            )

        return None

    def get(self, name: str, overlay: dict[str, "Skill"] | None = None) -> Skill | None:
        """获取已加载的Skill（overlay 优先于全局 _skills）

        Args:
            name: Skill名称
            overlay: per-agent 的 task级技能映射（工作nest/工作目录技能）。命中时优先返回。

        Returns:
            Skill实例，不存在返回None。overlay 与 _skills 均无则返回 None。
        """
        if overlay and name in overlay:
            return overlay[name]
        return self._skills.get(name)

    def list_skills(self) -> list[str]:
        """
        列出所有已加载的Skill名称

        Returns:
            Skill名称列表
        """
        return list(self._skills.keys())

    def get_discovery_prompt(self, overlay: dict[str, "Skill"] | None = None) -> str:
        """生成 Discovery 阶段的提示词（只有 descriptions，~100 tokens）

        符合 Agent Skills 标准的 Progressive Disclosure:
        - 只注入 skill 名称和描述列表
        - LLM 根据描述判断需要哪个 skill
        - 通过 skill 工具按需加载完整内容

        Args:
            overlay: per-agent 的 task级技能映射（工作nest/工作目录技能）。同名时覆盖全局技能（更具体上下文优先）。不修改共享 _skills。

        Returns:
            skills 发现提示词（无技能时返回空字符串）
        """
        # 合并：全局 _skills 为基础，overlay（task级）同名覆盖（更具体上下文优先）；不修改共享状态。
        merged = dict(self._skills)
        if overlay:
            merged.update(overlay)

        if not merged:
            return ""

        enabled_skills = [s for s in merged.values() if s.metadata.enabled]
        if not enabled_skills:
            return ""

        lines = ["## 可用技能\n"]
        lines.append("以下是可用的技能。如需使用某个技能，请调用 `skill` 工具加载详细内容：\n")

        for skill in sorted(enabled_skills, key=lambda s: s.metadata.priority):
            desc = skill.metadata.description or "无描述"
            # 限制描述长度
            desc_short = desc[:100] + "..." if len(desc) > 100 else desc
            lines.append(f"- **{skill.metadata.name}**: {desc_short}")

        return "\n".join(lines)


    def get_skill_content(self, name: str) -> str | None:
        """
        获取Skill的完整内容

        Args:
            name: Skill名称

        Returns:
            Skill内容，不存在返回None
        """
        skill = self._skills.get(name)
        if skill:
            return skill.content
        return None

    def get_skill_prompt(self, name: str) -> str | None:
        """
        获取Skill的提示词内容

        这是get_skill_content的别名，用于更语义化的调用。

        Args:
            name: Skill名称

        Returns:
            Skill内容（作为提示词），不存在返回None
        """
        return self.get_skill_content(name)

    def get_skill_scripts_dir(self, name: str) -> Path | None:
        """
        获取 Skill 的 scripts 目录路径

        Args:
            name: Skill 名称

        Returns:
            scripts 目录路径，不存在返回 None
        """
        skill = self._skills.get(name)
        if skill:
            scripts_dir = skill.path / "scripts"
            if scripts_dir.exists() and scripts_dir.is_dir():
                return scripts_dir
        return None

    def get_all_scripts_dirs(self) -> list[Path]:
        """
        获取所有 enabled skill 的 scripts 目录

        Returns:
            scripts 目录列表
        """
        scripts_dirs = []
        for skill in self._skills.values():
            if not skill.metadata.enabled:
                continue
            scripts_dir = skill.path / "scripts"
            if scripts_dir.exists() and scripts_dir.is_dir():
                scripts_dirs.append(scripts_dir)
        return scripts_dirs

    def find_matching(self, task_description: str) -> list[Skill]:
        """
        查找匹配事务的 Skills

        遵循 Claude Code Skills 规范：
        - 基于 description 语义匹配
        - LLM 根据 description 自主判断何时使用 skill
        - 返回所有 enabled 的 skills，按优先级排序

        Args:
            task_description: 事务描述（用于语义匹配）

        Returns:
            匹配的 Skill 列表，按优先级排序
        """
        matching = []
        for skill in self._skills.values():
            if not skill.metadata.enabled:
                continue
            matching.append(skill)

        # 按优先级排序（priority 越小越优先）
        matching.sort(key=lambda s: s.metadata.priority)
        return matching

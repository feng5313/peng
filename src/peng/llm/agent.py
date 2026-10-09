"""
Agent核心

Agent核心负责执行用户指令，协调LLM、工具和消息管理，实现完整的对话循环。
"""

import logging
import time
import traceback
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from peng.common import jsonp as json
from peng.common.colors import colorize, get_colors
from peng.config import get_path, load_config
from peng.hooks.manager import HookManager
from peng.llm.client import GLMClient
from peng.llm.token_monitor import TokenMonitor, TokenStatus
from peng.llm.tool_engine import (
    CompleteEvent,
    ContentEvent,
    MessageHistory,
    ThinkingEvent,
    ToolCallEvent,
    ToolExecutionEngine,
    ToolResultEvent,
)
from peng.skills import SkillLoader
from peng.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# 获取颜色配置
_COLORS = get_colors()


class AgentState(Enum):
    """Agent状态"""

    IDLE = "idle"  # 空闲
    THINKING = "thinking"  # 等待LLM响应
    EXECUTING = "executing"  # 执行工具
    ERROR = "error"  # 错误状态


@dataclass
class ToolResult:
    """工具执行结果"""

    tool_call_id: str  # 工具调用ID
    tool_name: str  # 工具名称
    success: bool  # 是否成功
    output: str | None  # 输出内容
    error: str | None  # 错误信息


@dataclass
class AgentAction:
    """Agent动作（用于回调）"""

    type: str  # "think" | "tool" | "response"
    content: str  # 内容
    tool_name: str | None = None  # 工具名称（type="tool"时）
    tool_args: dict | None = None  # 工具参数（type="tool"时）


class AgentError(Exception):
    """Agent执行错误"""


class MaxIterationsExceededError(AgentError):
    """超过最大迭代次数"""


@dataclass
class Chunk:
    """输出块（包含内容和类型）"""

    content: str  # 内容
    source: str = "answer"


class PengAgent:
    """
    鹏的核心Agent

    实现完整的对话循环:
    1. 添加用户消息到历史
    2. 调用LLM获取响应
    3. 如果有工具调用，执行工具并继续循环
    4. 返回最终响应
    """

    def __init__(
        self,
        message_manager=None,
    ) -> None:
        """
        初始化Agent

        Args:
            llm_client: LLM客户端实例
            tool_registry: 工具注册表实例
            system_prompt: 系统提示词（可选，默认从AGENTS.md加载）
            max_iterations: 最大迭代次数（防止无限循环，None 则从配置读取）
            config: 完整配置对象（可选）
            message_manager: 消息管理器（可选，用于钩子系统）
        """
        from peng.message.manager import MessageManager

        self.message_manager = message_manager or MessageManager()
        self.task_manager = self.message_manager.get_task_manager()

        self.config = load_config()
        self.llm = GLMClient()
        self.history = MessageHistory()
        # 从配置读取 max_iterations，优先级：参数 > 配置
        self.max_iterations = getattr(self.config, "max_iterations", 100)
        self._state = AgentState.IDLE
        self._hook_manager = None

        self.tools = ToolRegistry()
        # 注入 LLM 客户端到工具注册表，让工具可以自主使用模型
        self.tools.set_llm_client(self.llm)
        self.tools.load_builtin_tools()

        # 按配置禁用指定工具（config: tools.disabled，如禁用 shot 截屏工具）
        # 被禁用的工具不会出现在发给 LLM 的工具定义中，且调用时会被 registry.execute 拦截
        _disabled_tools = getattr(getattr(self.config, "tools", None), "disabled", None) or []
        if _disabled_tools:
            self.tools.disable(_disabled_tools)
            logger.info(f"[Agent] 已按配置禁用工具: {_disabled_tools}")

        # 设置消息管理器（用于上下文工具）
        self.tools.set_message_manager(self.message_manager)
        self.tools.set_show_tool_output(True)

        # 创建工具执行引擎
        self._engine = ToolExecutionEngine(
            llm_client=self.llm,
            tool_registry=self.tools,
            max_iterations=self.max_iterations,
            message_manager=message_manager,
        )

        # Token 监控
        self._token_monitor: TokenMonitor | None = None
        if hasattr(self.config, "token_monitor"):
            tm_config = self.config.token_monitor
            if tm_config.enabled:
                self._token_monitor = TokenMonitor(
                    max_tokens=tm_config.max_tokens,
                    warning_threshold=tm_config.warning_threshold,
                    critical_threshold=tm_config.critical_threshold,
                )

        # Skills 加载器
        self._skill_loader: SkillLoader | None = None
        self._skills: dict[str, Any] = {}
        # 🔧 事务#1827：per-agent task级技能 overlay（工作nest/工作目录技能，不修改共享单例）
        self._task_skills_overlay: dict = {}
        if hasattr(self.config, "skills"):
            skills_config = self.config.skills
            if skills_config.enabled:
                self._init_skill_loader(skills_config)

    def _init_skill_loader(self, skills_config: Any) -> None:
        """
        初始化 Skills 加载器

        Args:
            skills_config: Skills 配置对象
        """
        # 获取项目根目录作为相对路径的基准
        project_root = Path.cwd()
        if self.config and hasattr(self.config, "paths"):
            project_root = Path(self.config.paths.project_root)

        # 收集所有 skills 目录
        skill_dirs = []
        for dir_path in skills_config.directories:
            # 展开 ~ 和相对路径
            p = Path(dir_path).expanduser()
            if not p.is_absolute():
                p = project_root / p
            skill_dirs.append(str(p))

        # 创建加载器
        self._skill_loader = SkillLoader(directories=skill_dirs)

        # 自动加载所有 skills（到内存，但不注入 prompt）
        if skills_config.auto_load:
            self._skills = self._skill_loader.load_all()

        # 设置全局 skill_loader 引用，供 skill 工具使用
        from peng.tools.skill import set_skill_loader

        set_skill_loader(self._skill_loader)

    def get_skill(self, name: str) -> Any | None:
        """
        获取已加载的 Skill

        Args:
            name: Skill 名称

        Returns:
            Skill 对象，不存在返回 None
        """
        return self._skills.get(name)

    def get_skill_prompt(self, name: str) -> str | None:
        """
        获取 Skill 的提示词内容

        Args:
            name: Skill 名称

        Returns:
            Skill 提示词内容，不存在返回 None
        """
        if self._skill_loader:
            return self._skill_loader.get_skill_prompt(name)
        return None

    def list_skills(self) -> list[str]:
        """
        列出所有已加载的 Skill 名称

        Returns:
            Skill 名称列表
        """
        if self._skill_loader:
            return self._skill_loader.list_skills()
        return []

    def _get_skills_discovery_prompt(self, overlay: dict | None = None) -> str:
        """
        获取 Skills Discovery 提示词（只有描述列表）

        符合 Agent Skills 标准的 Progressive Disclosure:
        1. Discovery - 此阶段，只注入 descriptions（~100 tokens）
        2. Activation - LLM 调用 skill 工具时加载完整内容
        3. Execution - 已加载的 skill 在后续对话中可用

        Args:
            overlay: per-agent task级技能映射（工作nest/工作目录技能），同名覆盖全局技能。

        Returns:
            skills 发现提示词（如果没有 skills 则返回空字符串）
        """
        if not self._skill_loader:
            return ""

        return self._skill_loader.get_discovery_prompt(overlay)

    async def _build_task_skills_overlay(self, task) -> dict:
        """构建任务所在 nest/工作目录的 task级技能 overlay（per-agent，不修改共享 SkillLoader 单例）（事务#1827）

        local nest：直接本地读取 <workdir>/skills/，经 collect_external_skills 收集；
        远程 nest：经 nest 协议远程读取（list_remote_skill_dirs + read_remote_skill_md，带缓存）。
        返回 {name: Skill} 映射作为 per-agent overlay：discovery 与 skill 工具按此覆盖同名全局技能（更具体上下文优先）。
        SkillLoader 是进程级 MetaSingleton 被同进程多 Agent 共享，绝不能写入其 _skills/_directories，否则泄漏。
        任何失败静默返回空 dict，不阻塞事务（与 AGENTS.md 加载 #1672 语义一致）。
        """
        if not self._skill_loader or not task:
            return {}

        workdir = task.get("workdir")
        if not workdir:
            return {}

        nest_name = task.get("nest") or "local"

        try:
            if nest_name == "local":
                skills_base = Path(workdir) / "skills"
                overlay = self._skill_loader.collect_external_skills([skills_base])
                if overlay:
                    logger.info(f"[Agent] 已收集 local nest 工作目录技能 {list(overlay.keys())} (来源: {skills_base})")
                return overlay

            from peng.llm.client import list_remote_skill_dirs, read_remote_skill_md
            from peng.skills.loader import Skill

            names = await list_remote_skill_dirs(nest_name, workdir)
            overlay = {}
            for name in names:
                content = await read_remote_skill_md(nest_name, workdir, name)
                if not content:
                    continue
                metadata = self._skill_loader._parse_frontmatter(content, name)
                skill = Skill(metadata=metadata, content=content, path=Path(workdir) / "skills" / name)
                overlay[name] = skill
            if overlay:
                logger.info(f"[Agent] 已收集远程nest {nest_name} 工作目录技能 {list(overlay.keys())}")
            return overlay

        except Exception as e:
            logger.warning(f"[Agent] 收集任务技能失败(忽略): {e}")
            return {}

    @property
    def is_auto_mode(self) -> bool:
        """是否处于全自动模式"""
        if self.config and hasattr(self.config, "auto_mode"):
            return self.config.auto_mode.enabled
        return False

    def _load_agent_md(self) -> str:
        """加载默认系统提示"""
        from pathlib import Path

        from peng.config import load_config

        # 尝试从配置获取 AGENTS.md 路径
        config = load_config()
        agent_md = ""
        # 🔧 事务#2105：改为基于项目根目录的绝对路径解析（消除CWD依赖）。
        # 原实现用相对路径 "./AGENTS.md"，依赖进程CWD——而任务runner子进程
        # 会继承peng服务进程CWD（WebUI工作区切换set_workdir→os.chdir会改
        # 变服务进程CWD），导致远程nest事务（workdir在本地不存在，agent.
        # run_stream不chdir）回退时读到其他工作目录（如huizi）的AGENTS.md，
        # 跨工作目录污染系统提示词（@provider!锁定标记随之泄漏）。
        from peng.config import get_project_root

        project_root = Path(get_project_root())
        possible_paths = [
            project_root / "AGENTS.md",
            project_root / ".peng" / "AGENTS.md",
        ]

        for path in possible_paths:
            if path.exists():
                agent_md = path.read_text(encoding="utf-8")
                break

        return agent_md

    @property
    def state(self) -> AgentState:
        """获取当前状态"""
        return self._state

    def reset(self) -> None:
        """重置Agent状态"""
        self.history.clear()
        self._state = AgentState.IDLE

    def get_token_status(self) -> TokenStatus | None:
        """获取当前 token 状态"""
        if self._token_monitor:
            return self._token_monitor.check(self.history.get_token_count())
        return None

    def _get_hook_manager(self):
        """获取钩子管理器 v3.0（延迟初始化）"""
        if self._hook_manager is None:
            try:
                from pathlib import Path

                # 获取项目根目录（peng/ 目录）
                project_root = Path(__file__).parent.parent.parent.parent
                hooks_dir = project_root / "src" / "peng" / "hooks"

                logger.debug(f"[ToolEngine] 钩子目录: {hooks_dir}")

                # 创建钩子管理器
                self._hook_manager = HookManager(hooks_dir=str(hooks_dir))

                logger.debug(
                    f"[ToolEngine] 钩子管理器 v3.0 初始化成功 "
                    f"(加载了 {self._hook_manager.hooks_count} 个钩子)"
                )

            except ImportError as e:
                logger.warning(f"[ToolEngine] 钩子管理器 v3.0 不可用: {e}")
            except Exception as e:
                logger.error(
                    f"[ToolEngine] 初始化钩子管理器 v3.0 失败: {e}",
                    exc_info=True,
                )

        return self._hook_manager

    async def _prepare_prompt(self, task):
        """准备系统提示词和用户提示词

        AGENTS.md 注入优先级（事务#1672）：
        1. 事务所在 nest 工作目录的 AGENTS.md / .peng/AGENTS.md
           （local nest 本地直读；远程 nest 经 nest 协议读取）
        2. 回退旧逻辑：peng 进程 cwd 的 AGENTS.md / .peng/AGENTS.md
        3. 再回退：prompts.jsonp 的 system_prompt
        """

        # 直接读取 config/prompts.jsonp
        prompts_path = get_path("prompts_json")
        with open(prompts_path, encoding="utf-8") as f:
            prompts_config = json.load(f)

        prompts_dict = prompts_config.get("prompts", prompts_config)

        # 🔧 优先读取事务所在 nest 工作目录的 AGENTS.md（事务#1672）
        task_agents_md = None
        try:
            from peng.llm.client import load_task_agents_md

            task_agents_md = await load_task_agents_md(task)
        except Exception as e:
            logger.warning(f"[Agent] 读取事务AGENTS.md失败(回退旧逻辑): {e}")

        if task_agents_md:
            self.history.add_system(task_agents_md)
        else:
            agent_md = self._load_agent_md()
            if agent_md:
                self.history.add_system(agent_md)
            else:
                system_prompt = prompts_dict["system_prompt"]["content"]
                self.history.add_system(system_prompt)

        prompt = task.get("prompt") or "task_prompt"
        if prompt in prompts_dict:
            task_prompt = prompts_dict[prompt].get("content") or prompt
        else:
            task_prompt = prompt

        # 🔧 事务#1827：构建任务所在 nest/工作目录的 task级技能 overlay（per-agent，不修改共享单例），
        # 使 discovery 提示词包含工作目录技能（task级优先于全局config技能）
        self._task_skills_overlay = await self._build_task_skills_overlay(task)

        # 注入 skills discovery 提示词（只有描述列表，节省 token）
        skills_prompt = self._get_skills_discovery_prompt(self._task_skills_overlay)
        self.history.add_system(skills_prompt)
        if task_prompt:
            self.history.add_system(task_prompt)

    async def run_stream(
        self,
        message_id: int | None = None,
        task_id: int | None = None,
        user_prompt: str = "",
        on_action: Callable[[AgentAction], None] | None = None,
    ) -> AsyncGenerator[str | Chunk]:
        """
        流式执行用户指令

        Args:
            user_prompt: 用户输入
            on_action: 动作回调函数
            message_id: 消息ID（用于自动获取消息链上下文）

        Yields:
            响应文本块或 Chunk 对象（包含内容和类型）

        Raises:
            AgentError: 执行失败
        """
        logger.debug(
            f"[Agent.run_stream] 开始: message_id={message_id}, task_id={task_id}, user_prompt长度={len(user_prompt)}"
        )
        self._state = AgentState.THINKING
        task = self.task_manager.get_task(task_id)

        # === 修复：优先从task.context恢复history ===
        context = task.get("context")
        if context and self.history.from_str(context) and self.history.to_api_format():
            logger.debug(
                f"[Agent.run_stream] 从task.context恢复history成功（包含{len(self.history.to_api_format())}条消息）"
            )
            # 恢复成功，跳过_prepare_prompt，保持完整的对话历史
        else:
            # context为空、反序列化失败、或消息为空（重启清空历史但保留统计）时，
            # 需要执行_prepare_prompt重新构建系统提示词
            # 但如果from_str已恢复累计统计（费用、token），需保留这些统计
            logger.debug(f"[Agent.run_stream] task.context不存在或消息为空，执行_prepare_prompt")
            await self._prepare_prompt(task=task)
        # 设置工具上下文
        workdir = task.get("workdir")
        if workdir:
            import os

            if os.path.isdir(workdir):
                os.chdir(workdir)
        self.tools.set_context(
            message_id=message_id,
            task_id=task_id,
            task=task,
            workdir=workdir,
            nest=task.get("nest") if task else None,
            history=self.history,
            config=self.config,
            tool_engine=self._engine,
            task_manager=self.task_manager,
            # 🔧 事务#1827：per-agent task级技能 overlay，供 skill 工具解析工作nest/工作目录技能（同名覆盖全局）
            skill_overlay=self._task_skills_overlay,
        )

        # === 钩子系统 v3.0: Before LLM 调用 (flow: prompt) ===
        self.hook_manager = self._get_hook_manager()
        logger.debug(f" 🪝  钩子管理器状态: {self.hook_manager is not None}")

        # 跟踪是否已经输出过 thinking 内容
        has_thinking_output = False

        # 使用 engine 执行
        logger.debug(
            f"[Agent.run_stream] 准备调用 engine.run_stream, messages数量={len(self.history.to_api_format())}, tools数量={len(self.tools.get_definitions())}"
        )
        async for event in self._engine.run_stream(
            message_id=message_id,
            task_id=task_id,
            tools=self.tools.get_definitions(),
            hook_manager=self.hook_manager,
            history=self.history,
        ):
            if isinstance(event, ThinkingEvent):
                # 处理 thinking 输出（带颜色）
                if on_action:
                    on_action(AgentAction(type="think", content=event.content))
                content = event.content
                yield Chunk(content=content, source="thinking")
                has_thinking_output = True

            elif isinstance(event, ContentEvent):
                # 如果之前输出过 thinking，在正式内容前添加空行
                if has_thinking_output:
                    yield Chunk(content="\n", source="answer")
                    has_thinking_output = False

                # 处理内容输出（带颜色）
                if on_action:
                    on_action(AgentAction(type="response", content=event.content))
                content = event.content
                yield Chunk(content=content, source="answer")

            elif isinstance(event, ToolCallEvent):
                # 格式化工具调用信息
                tc = event.call
                tool_info = self._engine._format_tool_call_info(tc.name, tc.arguments)
                tool_call_color = _COLORS.agent("tool_call")
                prompt = colorize(
                    f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]🔧 工具调用:",
                    tool_call_color,
                )
                # 🔧 事务#1819：去掉开头\n（输出文件中会产生空格占位空行，
                # 是事务#1818输出空行泛滥的来源之一）
                content = f"{prompt} {tool_info}\n"
                yield Chunk(content=content, source="tool_call")

                if on_action:
                    on_action(
                        AgentAction(
                            type="tool",
                            content="",
                            tool_name=tc.name,
                            tool_args=tc.arguments,
                        )
                    )

                # 更新状态
                self._state = AgentState.EXECUTING

            elif isinstance(event, ToolResultEvent):
                tc = event.call
                display = event.display  # DualOutput 的 display 部分（如 diff）
                # 输出工具结果摘要到 stdout
                result_color = _COLORS.agent("result")
                prompt = "📤 命令执行结果" if tc.name in ("bash", "cmd") else "📤 工具调用结果"
                prompt = colorize(
                    f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]{prompt}:", result_color
                )
                prompt += self._engine._format_tool_result_info(tc.name, tc.arguments)

                # 注：工具级"回滚至快照"链接已废弃——每轮迭代开始的统一备份链接
                # （含完整diff预览确认框）已覆盖此功能，避免链接冗余。

                # 🔧 事务#1819：去掉开头\n与结尾多余\n（输出文件中会产生
                # 空格占位空行，是事务#1818输出空行泛滥的来源之一）
                content = f"{prompt}\n {display}\n"
                source = "bash" if tc.name in ("bash", "cmd") else "tool_result"
                yield Chunk(content=content, source=source)

                # Token 监控检查
                if self._token_monitor:
                    status = self._token_monitor.check(self.history.get_token_count())
                    if status.is_warning and on_action:
                        on_action(
                            AgentAction(
                                type="response",
                                content=f"[Token 使用: {status.usage_percent:.1f}%]",
                            )
                        )

                self._state = AgentState.THINKING

            elif isinstance(event, CompleteEvent):
                self._state = AgentState.IDLE
                logger.debug(f"[Agent.run_stream] 收到CompleteEvent")
                return

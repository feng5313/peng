"""
LLM 客户端

负责与大模型 API 交互，提供流式和非流式调用能力。
支持多 Provider 配置，可通过模型短名称切换 model、api_key 和 base_url。
支持 Provider 轮换，当大模型不可用时自动切换到下一个 provider。
大模型配置优先级：环境变量 > 参数 > 配置文件 > 默认值
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import traceback
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from peng.common import jsonp as json
from peng.common.colors import ANSI_COLORS
from peng.common.types import ToolCall
from peng.config import load_config
from peng.config.loader import LLMConfig, ProviderConfig
from peng.common.logging_config import setup_llm_logging, should_log
from peng.llm.concurrency import acquire_llm_slots_handle

logger = logging.getLogger(__name__)
llm_logger = None


class StreamStallError(Exception):
    """SSE流停滞异常：连接已建立但长时间没有新数据返回"""

    def __init__(self, timeout: float, provider: str | None = None):
        self.timeout = timeout
        self.provider = provider
        super().__init__(
            f"SSE流停滞超过{timeout}秒，provider={provider or 'unknown'}，"
            f"连接可能已断开或服务端卡住"
        )


class LLMError(Exception):
    """LLM API 返回错误异常：如上下文长度超限等"""

    def __init__(self, message: str, provider: str | None = None):
        self.provider = provider
        super().__init__(message)


# thinking标签预编译正则（用于_parse_thinking_tags）
_THINKING_TAG_PATTERN = re.compile(r"<think\>(.*?)\</think\>", re.DOTALL)


# 多模态附件part估算token数（与MessageHistory.estimate_tokens保持一致）
_MULTIMODAL_PART_ESTIMATED_TOKENS = 1000

# 🔧 事务#1959：附件触发的vision provider切换最多服务的迭代数（含附件轮
# 本身）。切换后第2个非附件迭代开始强制切回原provider，防止"没有出错就
# 不切换"原则让事务长期滞留在flash等轻量vision模型上。
VISION_SWITCH_MAX_ROUNDS = 2


def estimate_messages_tokens(messages: list[dict] | None) -> int:
    """估算消息列表的输入token数（事务#1807）

    算法与 MessageHistory.estimate_tokens（tool_engine.py）一致：
    JSON序列化字符数 / 3.5（混合中英文平均token比率），多模态content中
    非text part按固定附件token数估算。client不能import tool_engine
    （会循环导入），故此处独立实现，两处算法需保持同步。

    用于chat_stream选择provider时校验目标provider的上下文能力，
    防止切换到装不下当前上下文的provider后必然400超限（事务#1805）。
    """
    if not messages:
        return 0
    total_chars = 0
    part_tokens = 0
    for msg in messages:
        if not msg:
            continue
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            # 多模态消息：text part按字符估算，附件part按固定token估算
            text_chars = 0
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_chars += len(part.get("text", ""))
                else:
                    part_tokens += _MULTIMODAL_PART_ESTIMATED_TOKENS
            total_chars += text_chars + 100  # 100: 消息结构开销
            continue
        try:
            msg_str = json.dumps(msg, ensure_ascii=False)
        except (TypeError, ValueError):
            msg_str = str(msg)
        total_chars += len(msg_str)

    # 混合中英文：平均约 3.5 字符/token；附件按固定 token 数累加
    return total_chars * 2 // 7 + part_tokens


@dataclass
class StreamChunk:
    """流式响应块"""

    content: str | None = None  # 正常输出内容
    thinking: str | None = None  # 思考/推理内容（GLM-5 等模型）
    tool_calls: list[ToolCall] | None = None
    finish_reason: str | None = None
    usage: dict | None = None  # API返回的token用量 {prompt_tokens, completion_tokens, total_tokens}


# ==================== AGENTS.md 加载（事务#1672） ====================

# AGENTS.md 候选文件名（优先级从高到低）
_AGENTS_MD_CANDIDATES = ("AGENTS.md", ".peng/AGENTS.md")

# 远程 nest 读取 AGENTS.md 的超时（秒）：
# nest client 的 _forward_with_retry 对连接类错误会无限重试，
# 这里用 wait_for 限时保护，避免 nest 长时间不可用时阻塞事务启动
_AGENTS_MD_READ_TIMEOUT = 30.0

# (nest_name, workdir) -> (mtime_ns, size, content) 缓存
_agents_md_cache: dict[tuple[str, str], tuple[int, int, str]] = {}


def _read_agents_md_local(workdir: str) -> str | None:
    """在本地文件系统读取 workdir 下的 AGENTS.md（local nest 场景，nest 与 peng 同机）"""
    base = Path(workdir)
    for name in _AGENTS_MD_CANDIDATES:
        path = base / name
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"[LLM Client] 读取本地 {path} 失败(忽略): {e}")
    return None


# nest 端"文件/路径不存在"类业务错误关键词：
# 子进程转发路径（_forward_via_server）会把 nest 业务错误也包装成 NestError
# 抛出（与连接错误同形态），需按关键词识别为"候选文件不存在"→继续尝试下一候选，
# 而非"nest 连接故障"→放弃。主进程路径正常返回 resp.error 不会进入该分支。
_AGENTS_MD_NOT_FOUND_PATTERNS = (
    "文件未找到",
    "文件不存在",
    "路径访问被安全策略拒绝",
    "获取文件信息失败",
)


def _is_agents_md_not_found(exc: Exception) -> bool:
    """判断异常是否为 nest 端文件不存在类业务错误（而非连接层错误）"""
    return any(p in str(exc) for p in _AGENTS_MD_NOT_FOUND_PATTERNS)


async def _read_agents_md_remote(nest_name: str, workdir: str) -> str | None:
    """通过 nest 协议读取远程 nest 工作目录下的 AGENTS.md

    使用 stat+read 组合实现mtime缓存；stat/read 返回业务错误
    （如"文件未找到"）时尝试下一候选；连接层错误经 wait_for 超时保护后放弃。
    """
    from peng.nest.client import send_request
    from peng.nest.protocol import NestError

    for name in _AGENTS_MD_CANDIDATES:
        remote_path = str(Path(workdir) / name)
        try:
            # 1. stat 获取 mtime/size（用于缓存判断）
            stat_resp = await asyncio.wait_for(
                send_request(
                    "stat",
                    {"path": remote_path},
                    timeout=_AGENTS_MD_READ_TIMEOUT,
                    nest_name=nest_name,
                ),
                timeout=_AGENTS_MD_READ_TIMEOUT + 5,
            )
            if stat_resp.error or not isinstance(stat_resp.data, dict):
                continue  # 文件不存在或stat失败 → 尝试下一候选
            # nest stat 返回 mtime（float秒）/size；键归一化为 (mtime_ns, size)
            try:
                mtime_ns = int(float(stat_resp.data.get("mtime", 0) or 0) * 1e9)
            except (TypeError, ValueError):
                mtime_ns = 0
            size = stat_resp.data.get("size", 0) or 0

            # 2. 缓存命中（mtime+size 未变）直接返回缓存内容
            cache_key = (nest_name, workdir)
            cached = _agents_md_cache.get(cache_key)
            if cached and cached[0] == mtime_ns and cached[1] == size:
                logger.debug(f"[LLM Client] AGENTS.md 缓存命中: {nest_name}:{remote_path}")
                return cached[2]

            # 3. read 读取内容（lines 带行号前缀需剥离）
            read_resp = await asyncio.wait_for(
                send_request(
                    "read",
                    {"path": remote_path, "offset": 1, "limit": 2000},
                    timeout=_AGENTS_MD_READ_TIMEOUT,
                    nest_name=nest_name,
                ),
                timeout=_AGENTS_MD_READ_TIMEOUT + 5,
            )
            if read_resp.error or not isinstance(read_resp.data, dict):
                continue
            lines = read_resp.data.get("lines") or []
            # nest _read_file 返回 "N: content" 格式（N=行号，与 offset 起始一致递增），
            # 用确定性前缀匹配剥离行号（split(": ",1) 会误伤以数字开头的内容行）
            content_lines = []
            for i, line in enumerate(lines):
                prefix = f"{i + 1}: "  # 请求 offset=1，前缀即 "1: "、"2: "... 与行号一致递增
                content_lines.append(line[len(prefix) :] if line.startswith(prefix) else line)
            content = "\n".join(content_lines)
            # splitlines 丢失文件末尾换行，与本地 read_text 行为对齐补回，
            # 避免注入后与后续 system 消息（如 skills 提示词）首行粘连
            if read_resp.data.get("end", 0) >= read_resp.data.get("total_lines", 0):
                content += "\n" if content else ""
            if read_resp.data.get("total_lines", 0) > len(lines):
                logger.warning(
                    f"[LLM Client] AGENTS.md 超过2000行已截断: {nest_name}:{remote_path}"
                )
            _agents_md_cache[cache_key] = (mtime_ns, size, content)
            logger.info(
                f"[LLM Client] 已读取远程nest AGENTS.md: {nest_name}:{remote_path} "
                f"({len(content)}字符)"
            )
            return content
        except (NestError, asyncio.TimeoutError) as e:
            if _is_agents_md_not_found(e):
                continue  # nest端文件不存在（子进程转发包装形态）→ 尝试下一候选
            # 连接层错误：AGENTS.md 读取失败不阻塞事务，记录警告后放弃
            logger.warning(
                f"[LLM Client] 读取远程nest AGENTS.md 失败(忽略): {nest_name}:{remote_path}: {e}"
            )
            return None
        except Exception as e:
            logger.warning(
                f"[LLM Client] 读取远程nest AGENTS.md 异常(忽略): {nest_name}:{remote_path}: {e}"
            )
            return None
    return None


async def load_task_agents_md(task: dict | None) -> str | None:
    """读取事务所在 nest 工作目录内的 AGENTS.md 或 .peng/AGENTS.md（事务#1672/#2242）

    - local nest：nest 与 peng 同机，直接本地读取（不走网络，最快且最可靠）
    - 远程 nest：经 nest 协议远程读取（带超时保护与 mtime 缓存）
    - 任一失败静默返回 None（系统提示词不追加该段，不影响事务执行）

    Args:
        task: 事务字典（含 nest、workdir 字段）

    Returns:
        AGENTS.md 内容；不存在或读取失败返回 None
    """
    if not task:
        return None
    workdir = task.get("workdir")
    if not workdir:
        return None
    nest_name = task.get("nest") or "local"

    if nest_name == "local":
        return _read_agents_md_local(workdir)

    try:
        return await _read_agents_md_remote(nest_name, workdir)
    except Exception as e:
        logger.warning(f"[LLM Client] load_task_agents_md 失败(忽略): {e}")
        return None


# ==================== 任务技能远程发现（事务#1827） ====================

# (nest_name, workdir_abs, skill_name) -> (mtime_ns, size, content) 缓存
_skill_md_cache: dict[tuple[str, str, str], tuple[int, int, str]] = {}


def _strip_line_prefix(lines: list[str], offset: int = 1) -> str:
    """剥离 nest read 返回行的行号前缀（"N: content"），按 offset 起始递增"""
    out = []
    for i, line in enumerate(lines):
        prefix = f"{i + offset}: "
        out.append(line[len(prefix) :] if line.startswith(prefix) else line)
    return "\n".join(out)


async def list_remote_skill_dirs(nest_name: str, workdir: str) -> list[str]:
    """通过 nest 协议列出远程 nest 工作目录下 skills/ 子目录名（每个含 SKILL.md 的为一个 skill）

    仅返回目录名列表（不读取内容，内容按需经 read_remote_skill_md 获取）。失败静默返回空列表。
    """
    from peng.nest.client import send_request
    from peng.nest.protocol import NestError

    skills_dir = str(Path(workdir) / "skills")
    try:
        read_resp = await asyncio.wait_for(
            send_request(
                "read",
                {"path": skills_dir, "offset": 1, "limit": 1000},
                timeout=_AGENTS_MD_READ_TIMEOUT,
                nest_name=nest_name,
            ),
            timeout=_AGENTS_MD_READ_TIMEOUT + 5,
        )
        if read_resp.error or not isinstance(read_resp.data, dict):
            return []
        # nest 目录读取返回 {"type":"directory","entries":[{"name","is_dir",...}]}（见 local._read_directory）
        data = read_resp.data
        if data.get("type") != "directory":
            return []
        entries = data.get("entries") or []
        names = [e.get("name") for e in entries if e.get("is_dir") and e.get("name")]
        return [n for n in names if n]
    except (NestError, asyncio.TimeoutError) as e:
        logger.warning(
            f"[LLM Client] 列出远程nest skills目录失败(忽略): {nest_name}:{skills_dir}: {e}"
        )
        return []
    except Exception as e:
        logger.warning(
            f"[LLM Client] 列出远程nest skills目录异常(忽略): {nest_name}:{skills_dir}: {e}"
        )
        return []


async def read_remote_skill_md(nest_name: str, workdir: str, name: str) -> str | None:
    """通过 nest 协议读取远程 nest 工作目录 skills/<name>/SKILL.md 内容（带 mtime+size 缓存）

    stat+read 组合实现缓存；文件不存在/读取失败静默返回 None（不影响事务）。
    """
    from peng.nest.client import send_request
    from peng.nest.protocol import NestError

    skill_md_name = "SKILL.md"
    try:
        from peng.config import load_config

        skill_md_name = load_config().paths.skill_md_name
    except Exception:
        pass

    remote_path = str(Path(workdir) / "skills" / name / skill_md_name)
    try:
        # 1. stat 获取 mtime/size（用于缓存判断）
        stat_resp = await asyncio.wait_for(
            send_request(
                "stat",
                {"path": remote_path},
                timeout=_AGENTS_MD_READ_TIMEOUT,
                nest_name=nest_name,
            ),
            timeout=_AGENTS_MD_READ_TIMEOUT + 5,
        )
        if stat_resp.error or not isinstance(stat_resp.data, dict):
            return None  # 文件不存在或stat失败 → None（调用方跳过）
        try:
            mtime_ns = int(float(stat_resp.data.get("mtime", 0) or 0) * 1e9)
        except (TypeError, ValueError):
            mtime_ns = 0
        size = stat_resp.data.get("size", 0) or 0

        # 2. 缓存命中（mtime+size 未变）直接返回缓存内容
        cache_key = (nest_name, str(Path(workdir).resolve()), name)
        cached = _skill_md_cache.get(cache_key)
        if cached and cached[0] == mtime_ns and cached[1] == size:
            logger.debug(f"[LLM Client] SKILL.md 缓存命中: {nest_name}:{remote_path}")
            return cached[2]

        # 3. read 读取内容（lines 带行号前缀需剥离）
        read_resp = await asyncio.wait_for(
            send_request(
                "read",
                {"path": remote_path, "offset": 1, "limit": 2000},
                timeout=_AGENTS_MD_READ_TIMEOUT,
                nest_name=nest_name,
            ),
            timeout=_AGENTS_MD_READ_TIMEOUT + 5,
        )
        if read_resp.error or not isinstance(read_resp.data, dict):
            return None
        lines = read_resp.data.get("lines") or []
        content = _strip_line_prefix(lines, offset=1)
        if read_resp.data.get("end", 0) >= read_resp.data.get("total_lines", 0):
            content += "\n" if content else ""
        if read_resp.data.get("total_lines", 0) > len(lines):
            logger.warning(f"[LLM Client] SKILL.md 超过2000行已截断: {nest_name}:{remote_path}")
        _skill_md_cache[cache_key] = (mtime_ns, size, content)
        logger.info(
            f"[LLM Client] 已读取远程nest SKILL.md: {nest_name}:{remote_path} ({len(content)}字符)"
        )
        return content
    except (NestError, asyncio.TimeoutError) as e:
        if _is_agents_md_not_found(e):
            return None  # nest端文件不存在 → None（调用方跳过）
        logger.warning(
            f"[LLM Client] 读取远程nest SKILL.md失败(忽略): {nest_name}:{remote_path}: {e}"
        )
        return None
    except Exception as e:
        logger.warning(
            f"[LLM Client] 读取远程nest SKILL.md异常(忽略): {nest_name}:{remote_path}: {e}"
        )
        return None


@dataclass
class LLMResponseStats:
    """LLM 响应统计信息"""

    has_content: bool = False  # 是否有实际内容
    has_thinking: bool = False  # 是否有思考内容
    has_tool_calls: bool = False  # 是否有工具调用
    content_length: int = 0  # 内容长度
    tool_calls_count: int = 0  # 工具调用数量
    finish_reason: str | None = None  # 结束原因

    def is_successful(
        self,
        requested_tools: bool = False,
        requested_thinking: bool = False,
    ) -> bool:
        """
        判断响应是否成功

        Args:
            requested_tools: 是否请求了工具调用
            requested_thinking: 是否请求了思考内容

        Returns:
            是否成功
        """
        # 1. 必须有实际内容或工具调用（不能为空）
        if not self.has_content and not self.has_tool_calls:
            return False

        # 2. 检查 finish_reason
        if self.finish_reason:
            # length: 内容过长被截断 → 失败
            if self.finish_reason == "length":
                return False
            # content_filter: 内容审核失败 → 失败
            if self.finish_reason == "content_filter":
                return False
            # stop/tool_calls: 正常结束 → 继续检查
            # null: 某些模型不返回 finish_reason，也算成功

        # 3. 如果请求了工具，必须有工具调用
        if requested_tools and not self.has_tool_calls:
            return False

        # 4. 思考内容是非强制的（即使请求了也不算失败）
        # if requested_thinking and not self.has_thinking:
        #     logger.debug("请求了思考模式但未返回思考内容")

        return True


class GLMClient:
    """
    LLM 客户端

    支持流式和非流式调用大模型 API。
    支持多 Provider 配置，可通过模型短名称切换 model、api_key 和 base_url。
    大模型配置优先级：环境变量 > 参数 > 配置文件 > 默认值
    """

    @property
    def config(self) -> LLMConfig:
        """动态获取最新的 LLM 配置（每次访问都通过 load_config() 获取，利用 mtime 缓存自动感知配置文件变化）"""
        return load_config().llm

    def __init__(self) -> None:
        """
        初始化 LLM 客户端

        Args:
            config: LLM 客户端配置（可选）
            api_key: API 密钥（可选）
            base_url: API 基础 URL（可选）
            llm_config: LLM 配置（包含多 provider 配置，可选）

        Raises:
            ValueError: API 密钥未设置
        """
        # 当前使用的 provider 名称
        self.provider: str | None = None
        self.error: str = ""
        self.compact_threshold: int = self.config.compact_threshold
        self.thinking_mode: str = "auto"
        self.thinking_type: str | None = None  # 当前 provider 的 thinking 处理类型

        # 系统报错去重：按task_id追踪最近的报错签名，避免重复输出撑爆上下文
        self._recent_error_sigs: dict[int, list[str]] = {}  # task_id -> [signatures]
        self._error_dedup_rounds: int = getattr(self.config, "error_dedup_rounds", 5)

        # 🔧 事务#1959：vision切换回退窗口——附件触发的vision provider切换跟踪
        # _vision_switch_from: 切换前的原provider名（None=窗口关闭）
        # _vision_switch_used: 窗口开启以来的非附件迭代计数
        self._vision_switch_from: str | None = None
        self._vision_switch_used: int = 0

        # 🔧 事务#2204：并发控制改为**跨进程**文件锁槽位信号量
        # （peng/llm/concurrency.py）。旧实现在每个 GLMClient 实例内建
        # asyncio.Semaphore，而每个事务都在独立子进程中运行（各自新建
        # PengAgent/GLMClient），信号量进程私有 ⇒ max_concurrent 对真实
        # provider 并发数毫无约束（用户实测抱怨"毫无效果"）。跨进程槽位
        # 在每次 chat_stream 时按最新配置动态获取，无需实例级缓存。

        # 初始化 LLM 专用日志器
        global llm_logger
        if llm_logger is None:
            try:
                config = load_config()
                # 使用新的日志配置结构，支持向后兼容
                logs_config = config.logging.get_logs_config()
                llm_logger = setup_llm_logging(llm_logs_config=logs_config.llm)
            except Exception as e:
                llm_logger = setup_llm_logging()
                logger.warning(f"加载 LLM 日志配置失败：{e}\n{traceback.format_exc()}")

    def _vision_window_advance(self, prefer_vision: bool) -> str | None:
        """vision切换回退窗口每轮推进（事务#1959）

        窗口开启（附件触发过vision切换）时按迭代计数推进：
        - 本轮仍有附件（prefer_vision）：刷新预算（图片必须发给vision
          provider，不能为回切而丢附件），不强制回切
        - 本轮无附件：计数+1；达到 VISION_SWITCH_MAX_ROUNDS 后返回需强制
          回切的原provider名

        Returns:
            需强制回切的原provider名；None=无需强制回切
        """
        if not self._vision_switch_from:
            return None
        if prefer_vision:
            self._vision_switch_used = 0
            return None
        self._vision_switch_used += 1
        if self._vision_switch_used >= VISION_SWITCH_MAX_ROUNDS:
            logger.info(
                f"[LLM Client] vision切换窗口已满{VISION_SWITCH_MAX_ROUNDS}轮，"
                f"强制切回原provider: {self._vision_switch_from}"
            )
            return self._vision_switch_from
        return None

    def _vision_window_on_selected(
        self, prefer_vision: bool, prev_provider: str | None, selected: str
    ) -> None:
        """vision切换回退窗口开合维护（事务#1959）

        - 附件触发provider切换（prefer_vision且切换发生）且窗口未开：
          记住切换前原provider开窗
        - 选中的provider即原provider（强制回切成功/优先级自然回切）：关窗
        """
        if prefer_vision and selected != prev_provider and not self._vision_switch_from:
            if prev_provider:
                self._vision_switch_from = prev_provider
                self._vision_switch_used = 0
                logger.info(
                    f"[LLM Client] 附件触发vision切换 {prev_provider} → {selected}，"
                    f"{VISION_SWITCH_MAX_ROUNDS}个迭代后将切回原provider"
                )
        elif self._vision_switch_from and selected == self._vision_switch_from:
            self._vision_switch_from = None
            self._vision_switch_used = 0
            logger.info("[LLM Client] 已切回原provider，vision切换窗口关闭")

    # ==================== 大模型并发控制（跨进程，事务#2204）====================

    def _get_concurrency_specs(
        self, model: str | None = None, small_model: str | None = None, provider: str | None = None
    ) -> list[tuple[str, int]]:
        """构造本次请求的跨进程并发约束 [(key, limit), ...]

        约束来源（与旧进程内信号量语义一致，只是改为跨进程生效）：
        1. 全局并发：请求使用小模型（model == provider 的 small_model）时用
           llm.small_model_max_concurrent，否则用 llm.max_concurrent；
        2. provider 并发：该 provider 配置了 max_concurrent（>0）时叠加该约束。

        limit 为 None/<=0 表示不限制（该项不入列）。
        """
        specs: list[tuple[str, int]] = []

        small_model_name = small_model or self.config.small_model
        if model is not None and model == small_model_name:
            global_key, global_limit = "global:small_model", self.config.small_model_max_concurrent
        else:
            global_key, global_limit = "global:llm", self.config.max_concurrent
        if global_limit is not None and int(global_limit) > 0:
            specs.append((global_key, int(global_limit)))

        if provider:
            prov_cfg = self.config.providers.get(provider)
            prov_limit = getattr(prov_cfg, "max_concurrent", None) if prov_cfg else None
            if prov_limit is not None and int(prov_limit) > 0:
                specs.append((f"provider:{provider}", int(prov_limit)))

        return specs

    def _slot_heartbeat(self, task_id: int | None) -> None:
        """等待并发槽位期间的事务心跳刷新（由限制器在线程池中调用，防止误杀）"""
        if not task_id or not self.task_manager:
            return
        self.task_manager.update_heartbeat(task_id)

    def _notify_slot_wait(self, task_id: int | None, waited: float, desc: str) -> None:
        """等待并发槽位超过阈值时向事务输出打印提示（用户可见、便于诊断）"""
        if not task_id:
            return
        try:
            from peng.task.output_manager import get_output_manager

            get_output_manager().write_output(
                task_id=task_id,
                source="system",
                content=(
                    f"⏳ {ANSI_COLORS['blue']} 等待大模型并发槽位 {waited:.0f}s"
                    f"（{desc}）{ANSI_COLORS['reset']}\n"
                ),
            )
        except Exception as e:
            logger.debug(f"[LLM Client] 并发等待提示输出失败(忽略): {e}")

    def _error_signature(self, content: str) -> str:
        """生成报错的标准化签名（用于去重比较）"""
        import re

        sig = re.sub(r"\x1b\[[0-9;]*m", "", content)
        sig = re.sub(r"\d+", "#", sig)
        return sig.strip()

    def _should_report_error(self, content: str, task_id: int | None = None) -> bool:
        """检查报错是否应该被输出（去重检测）

        在最近 error_dedup_rounds 轮内已有类似报错签名时跳过输出。
        按task_id隔离，不同事务的报错互不影响。
        """
        signature = self._error_signature(content)
        tid = task_id if task_id is not None else 0
        recent = self._recent_error_sigs.get(tid, [])
        if signature in recent[-self._error_dedup_rounds :]:
            return False
        recent.append(signature)
        max_keep = self._error_dedup_rounds * 2
        if len(recent) > max_keep:
            recent = recent[-max_keep:]
        self._recent_error_sigs[tid] = recent
        return True

    async def close(self) -> None:
        """关闭实例级长驻客户端（若存在）

        🔧 事务#2178：chat_stream 改用每次调用专属 client 并由自身 finally
        关闭，不再经过本方法；本方法仅保留给实例级 client（若曾创建）。
        """
        client = getattr(self, "_client", None)
        if client is not None:
            await client.aclose()

    def get_system_prompt(self, messages: list[dict]) -> str:
        if messages:
            if "role" in messages[0] and messages[0]["role"] == "system":
                return messages[0]["content"]
        return ""

    def refresh_provider_vars(self, content: str | None = None) -> None:
        """按当前有效 provider 解析配置并更新临时变量（不发起请求）

        __init__ 只填入全局默认值，provider 专属临时变量
        （provider/compact_threshold/thinking_mode/thinking_type/provider_config）
        在 chat_stream 选中 provider 后才同步。事务恢复后首轮迭代的 compact
        检查发生在首次请求之前，需先调用本方法按当前有效 provider
        更新临时变量，与 chat_stream 连上 provider 后的更新保持同一套字段。

        Args:
            content: 事务content（可选）。传入时解析其中的@<provider>标记
                （favorites/locked/forbidden），使预热与 chat_stream 的 provider
                选择保持一致；否则纯按 priority 自动选择（预热结果会污染
                self.provider，导致 chat_stream 锁定语法@<provider>!失效）
        """
        favorites = None
        locked = False
        forbidden = None
        if content:
            fetch_result = self.config.fetch_providers(content)
            if fetch_result:
                favorites, locked, forbidden = fetch_result
        provider_data = self.config.get_provider(
            current=self.provider,
            favorites=favorites,
            locked=locked,
            forbidden=forbidden,
        )
        if not provider_data:
            logger.warning("[LLM Client] 预热provider临时变量失败: 无可用provider")
            return
        provider = provider_data["provider"]
        if provider != self.provider:
            logger.info(
                f"[LLM Client] 预热provider临时变量: {self.provider or 'None'} → {provider} "
                f"(compact_threshold={provider_data['compact_threshold']})"
            )
        self.provider = provider
        self.compact_threshold = provider_data["compact_threshold"]
        self.thinking_mode = provider_data["thinking_mode"]
        self.thinking_type = provider_data.get("thinking_type")
        self.provider_config = provider_data  # 保存完整配置（含 price_* 字段）

    async def chat_stream(
        self,
        message_id: int | None = None,
        task_id: int | None = None,
        messages: list[dict] | None = None,
        message_manager=None,
        tools: list[dict] | None = None,
        thinking_mode: str | None = None,
        thinking_budget: int | None = None,
        retry: int | None = None,
        failed: bool | None = None,
    ) -> AsyncGenerator[StreamChunk]:
        """
        流式调用 LLM

        Args:
            messages: 消息列表（OpenAI 格式）
            tools: 工具定义列表（可选）
            model: 模型名称（可选，默认使用配置的模型）
            thinking_mode: 思考模式（可选：auto, enabled, disabled）
            thinking_budget: 思考预算，限制推理过程的最大Token数（可选）

        Yields:
            StreamChunk: 流式响应块
        """
        from peng.message.manager import MessageManager

        self.message_manager = message_manager if message_manager else MessageManager()
        self.task_manager = self.message_manager.get_task_manager()
        # 每次调用都获取最新配置（不缓存）
        # 不传provider参数时，get_provider会自动选择优先级最高的可用provider

        if self.config:
            task = self.task_manager.get_task(task_id=task_id)
            content = task.get("content")
            fetch_result = self.config.fetch_providers(content + self.get_system_prompt(messages))
            favorite_providers = fetch_result[0] if fetch_result else None
            provider_locked = fetch_result[1] if fetch_result else False
            forbidden_providers = fetch_result[2] if fetch_result else None
            # 多模态检测：事务内容/未读info包含 @file=<path>/[附件] 多模态引用时
            # 优先使用支持vision的provider（显式@provider指定优先级更高）
            prefer_vision = False
            mm_source = content or ""
            if task:
                try:
                    info_raw = task.get("info") or "[]"
                    if info_raw:
                        info_list = json.loads(info_raw) if isinstance(info_raw, str) else info_raw
                        if isinstance(info_list, list):
                            unread = [
                                i.get("content", "")
                                for i in info_list
                                if isinstance(i, dict) and not i.get("read")
                            ]
                            if unread:
                                mm_source += "\n" + "\n".join(unread)
                except Exception as e:
                    logger.debug(f"[LLM Client] info解析失败(忽略): {e}")
            if mm_source:
                try:
                    from peng.llm.multimodal import extract_file_refs

                    prefer_vision = bool(extract_file_refs(mm_source))
                except Exception as e:
                    logger.debug(f"[LLM Client] 多模态引用检测失败(忽略): {e}")
            # 请求消息中已携带多模态附件part（read等工具结果附件，或上下文恢复的
            # 未发送附件）时必须使用vision provider，否则文本provider收到不支持的
            # 消息格式会报400并误触发该provider冷却
            if not prefer_vision:
                try:
                    from peng.llm.multimodal import messages_have_attachment_parts

                    if messages_have_attachment_parts(messages):
                        prefer_vision = True
                        logger.info(
                            "[LLM Client] 检测到请求携带多模态附件，优先选择vision provider"
                        )
                except Exception as e:
                    logger.debug(f"[LLM Client] 附件part检测失败(忽略): {e}")
            # failed 参数优先级：显式传入 > retry推断
            # 对于429限流错误，调用方可传 failed=False 来保持同一provider
            should_mark_failed = failed if failed is not None else (retry and retry > 0)
            # 上下文能力校验（事务#1807）：估算当前请求输入token，禁止切换到
            # 装不下当前上下文的provider（有效compact_threshold不足者），防止
            # 事务#1805式"切换→400超限→重试→再切换"持续严重错误循环
            min_context_tokens = None
            try:
                est = estimate_messages_tokens(messages)
                if est > 0:
                    # 5%余量覆盖tools schema等未计入的请求开销
                    min_context_tokens = int(est * 1.05)
            except Exception as e:
                logger.debug(f"[LLM Client] 请求token估算失败(忽略，不限制provider选择): {e}")
            # 🔧 事务#1959：vision切换回退窗口——附件触发的vision provider切换
            # 最多服务 VISION_SWITCH_MAX_ROUNDS 个迭代，之后必须切回原provider
            # （get_provider"没有出错就不切换"原则会让current一直复用vision
            # provider，事务长期滞留flash等轻量模型）。本轮仍有附件时刷新预算
            # （图片必须发给vision provider，不能为回切而丢附件）。
            _vision_return_target = self._vision_window_advance(prefer_vision)
            prev_provider = self.provider
            # failed标记语义作用于"当前实际provider"，强制回切仅在成功路径
            # 覆盖current（失败路径按优先级重选即可达到回切效果）
            _provider_current = self.provider
            if _vision_return_target and not should_mark_failed:
                _provider_current = _vision_return_target
            provider_data = self.config.get_provider(
                current=_provider_current,
                favorites=favorite_providers,
                failed=should_mark_failed,
                task_id=task_id,
                locked=provider_locked,
                forbidden=forbidden_providers,
                prefer_vision=prefer_vision,
                min_context_tokens=min_context_tokens,
            )
        else:
            provider_data = None
        if provider_data:
            provider = provider_data["provider"]
            try:
                if provider != self.provider:
                    old = "🎲"
                    new = "🎲"
                    if self.provider:
                        p = self.config.providers.get(self.provider)
                        if p:
                            old = p.icon
                    else:
                        self.provider = ""
                    p = self.config.providers.get(provider)
                    if p:
                        new = p.icon
                    from peng.task.output_manager import get_output_manager

                    content = ""
                    is_error_output = False
                    if self.provider and self.error:
                        # 有错误信息：显示"大模型报错"+ 错误详情
                        content = f"💠 {ANSI_COLORS['blue']} 大模型API报错 {ANSI_COLORS['bright_yellow']}{self.error} {ANSI_COLORS['blue']}，已切换： {ANSI_COLORS['reset']}{old} {self.provider} →- {new} {provider} \n"
                        is_error_output = True
                    elif self.provider:
                        # 无错误信息但 provider 发生变化（优先级自动调整/冷却到期等）：显示"切换大模型"
                        content = f"💠 {ANSI_COLORS['blue']} 切换大模型： {ANSI_COLORS['reset']}{old} {self.provider} →- {new} {provider} \n"
                    else:
                        content = f"💠 {ANSI_COLORS.get('blue')} 连接大模型： {ANSI_COLORS.get('reset')} {new} {provider} \n"
                    # 系统报错去重：重复的报错不输出（切换/连接信息不受影响）
                    if not is_error_output or self._should_report_error(content, task_id):
                        get_output_manager().write_output(
                            task_id=task_id, source="system", content=content
                        )
            except Exception as e:
                logger.error(
                    f"[LLM Client] 无法获取大模型provider图标。 {e}\n{traceback.format_exc()}"
                )

            self.provider = provider
            self.compact_threshold = provider_data["compact_threshold"]
            # 🔧 事务#1959：vision切换窗口开合维护
            self._vision_window_on_selected(prefer_vision, prev_provider, provider)
            self.thinking_mode = provider_data["thinking_mode"]
            self.thinking_type = provider_data.get("thinking_type")
            self.provider_config = provider_data  # 保存完整配置（含 price_* 字段）
        else:
            error_msg = "无法获取可用provider配置（所有provider可能都在冷却中或未启用）"
            logger.error(f"[LLM Client] {error_msg}:\nCall Stack:\n{traceback.format_exc()}")
            raise LLMError(error_msg, provider=self.provider or "unknown")
        current_api_key = provider_data["api_key"]
        current_base_url = provider_data["base_url"]
        current_model = provider_data["model"]
        current_small_model = provider_data["small_model"]
        current_thinking_mode = provider_data["thinking_mode"]
        current_temperature = provider_data["temperature"]
        current_max_tokens = provider_data["max_tokens"]
        current_frequency_penalty = provider_data.get("frequency_penalty", 0.0)
        current_presence_penalty = provider_data.get("presence_penalty", 0.0)

        # 🔧 修复: 立即更新事务的provider字段并广播（通过 update_task 统一处理，一次广播含 icon）
        if task_id and self.task_manager:
            try:
                from peng.task.database import TaskDatabase

                # 获取 provider 的 icon 信息（provider_data 在上方已获取，包含 icon 字段）
                provider_icon = provider_data.get("icon", "🤖") if provider_data else "🤖"
                # 通过 _broadcast_extra 将 icon 附带到广播中，避免二次广播竞态导致图标延迟
                TaskDatabase().update_task(
                    task_id,
                    provider=self.provider,
                    _broadcast_extra={"icon": provider_icon},
                )
                logger.debug(f"[LLM Client] 已立即更新事务#{task_id}的provider为: {self.provider}")
            except Exception as e:
                logger.warning(f"[LLM Client] 更新事务provider失败: {e}\n{traceback.format_exc()}")

        # HTTP 客户端
        # 流式请求需要更长的读取超时，因为 LLM 可能需要时间处理
        # 从配置中获取超时时间，支持细粒度控制
        timeout_config = httpx.Timeout(
            connect=self.config.timeout_connect,  # 连接超时
            read=self.config.timeout_read + 1,  # 读取超时
            write=self.config.timeout_write,  # 写入超时
            pool=self.config.timeout_pool,  # 连接池超时
        )
        # 🔧 事务#2178：本次调用专属 client（旧版赋给 self._client，并发调用互相
        # 覆盖引用、生成器退出时无人关闭，每次 LLM 调用泄漏一个连接池）。
        # 局部化后由本生成器的 finally 显式关闭；self._client 保留给 close()。
        _call_client = httpx.AsyncClient(
            timeout=timeout_config,
            headers={
                "Authorization": f"Bearer {current_api_key}",
                "Content-Type": "application/json",
            },
            trust_env=False,  # 禁用系统代理检测，LLM API 的 base_url 已明确配置，无需走代理
        )

        # 获取日志配置
        llm_logs_config = getattr(llm_logger, "llm_logs_config", None) if llm_logger else None

        # 🔧 事务#2204：使用**跨进程**文件锁槽位信号量控制并发
        # （全局 max_concurrent + provider max_concurrent 组合；全有或全无获取）
        effective_model = current_model
        concurrency_specs = self._get_concurrency_specs(
            effective_model, current_small_model, self.provider
        )
        logger.debug(f"[LLM Client] 尝试获取并发槽位: {concurrency_specs}")
        # 槽位必须覆盖"请求发出 → SSE 流式读取结束"的完整窗口：chat_stream 的请求块
        # 位于函数体顶层缩进（见下方 try），无法包进 async with，因此用显式句柄并在
        # 末尾 finally 中**同步**释放（同步释放对 GeneratorExit 展开路径同样安全）。
        _slot_handle = await acquire_llm_slots_handle(
            concurrency_specs,
            task_id=task_id,
            heartbeat_cb=lambda: self._slot_heartbeat(task_id),
            notice_cb=lambda waited, desc: self._notify_slot_wait(task_id, waited, desc),
        )
        try:
            logger.debug("[LLM Client] 已获得并发槽位，准备发送请求")
            url = f"{current_base_url}/chat/completions"

            # 使用传入的 thinking_mode 或配置的值
            effective_thinking = thinking_mode or current_thinking_mode

            # 获取 thinking_type 配置（强制指定 thinking 的处理方式）
            # 从 provider_data 获取最新配置（不缓存）
            current_thinking_type = None
            if provider_data:
                current_thinking_type = provider_data.get("thinking_type")

            logger.debug(
                f"[LLM Client] thinking配置: thinking_mode参数={thinking_mode}, "
                f"provider_thinking_mode={current_thinking_mode}, "
                f"effective_thinking={effective_thinking}, "
                f"thinking_type={current_thinking_type}"
            )

            # 🔧 事务#1819：附件降级闭环 —— 选定的provider不支持多模态时，
            # 把消息中的附件part降级为文本提示再发送。防止附件发给不支持
            # vision的provider导致HTTP 400（content.type参数非法）并误冷却
            # 该provider（事务#1818死循环诱因之一）。vision值已经loader
            # 运行期禁用检查（禁用时为"disabled"）。
            _vision_cfg = str(provider_data.get("vision", "disabled")).lower()
            _provider_vision_ok = _vision_cfg in ("true", "yes", "on", "enabled", "only")
            if not _provider_vision_ok:
                try:
                    from peng.llm.multimodal import (
                        demote_messages_attachments,
                        messages_have_attachment_parts,
                    )

                    if messages_have_attachment_parts(messages):
                        logger.warning(
                            f"[LLM Client] provider {self.provider} 不支持多模态，"
                            f"已将请求中的附件降级为文本提示"
                        )
                        messages = demote_messages_attachments(messages)
                except Exception as e:
                    logger.debug(f"[LLM Client] 附件降级检查失败(忽略): {e}")

            # 🔧 事务#1959：空附件净化最后防线——请求携带附件part时校验
            # payload非空（data URL base64段非空且解码非0字节）。空payload附件
            # （0字节文件透传/历史中毒上下文/旧版本入库坏附件）发给provider
            # 必然400（GLM: file必须传入file_id/file_url/file_data至少之一），
            # 且换provider无法自愈，曾致事务#1955无限重试只能人工回滚。
            try:
                from peng.llm.multimodal import (
                    messages_have_attachment_parts,
                    sanitize_messages_attachments,
                )

                if messages_have_attachment_parts(messages):
                    messages, _invalid_removed = sanitize_messages_attachments(messages)
                    if _invalid_removed:
                        logger.warning(
                            f"[LLM Client] 已移除{_invalid_removed}个空数据附件part"
                            f"(0字节/无效payload)，防止API 400"
                        )
            except Exception as e:
                logger.debug(f"[LLM Client] 空附件净化检查失败(忽略): {e}")

            payload: dict[str, Any] = {
                "model": effective_model,
                "messages": messages,
                "temperature": current_temperature,
                "max_tokens": current_max_tokens,
                "stream": True,
                "stream_options": {
                    "include_usage": True
                },  # 请求API在streaming末尾返回真实token用量
            }

            # 添加重复惩罚参数（防止模型输出重复/死循环内容）
            # 部分模型后端可能不支持这些参数，值为0时不传递以避免不必要的报错
            if current_frequency_penalty and current_frequency_penalty != 0.0:
                payload["frequency_penalty"] = current_frequency_penalty
            if current_presence_penalty and current_presence_penalty != 0.0:
                payload["presence_penalty"] = current_presence_penalty

            # 添加思考模式参数
            # 使用配置文件中的 thinking_type 字段决定参数格式：
            # - thinking_type = "thinking": 使用 thinking: {"type": "enabled"|"disabled"}
            #   并支持 reasoning_effort 推理强度档位
            #   （low/medium/high/xhigh/max，GLM 风格支持 low/high/max，
            #    Qwen3/vLLM 风格如 s-qwen3.8 支持 xhigh/medium/low）
            # - thinking_type = "chat_template_kwargs": 使用 chat_template_kwargs: {"enable_thinking": true|false}
            # - thinking_type = "glm": 使用 thinking 参数（兼容旧配置）
            # - thinking_type = "qwen": 使用 chat_template_kwargs 参数（兼容旧配置）
            # 注意：阿里云API不支持 chat_template_kwargs 参数，应配置为 "thinking" 或 "glm"
            #
            # thinking_mode 支持思考级别：
            # - auto: 不带参数，模型自己决定（过度思考由 tool_engine 干预）
            # - disabled: 禁用思考；若模型不支持禁用（reasoning_disabled_fallback 配置），
            #   降级为 enabled + 该档位推理强度（如 GLM-5.3 只支持 low/high/max 档位）
            # - enabled: 开启思考（标准档，显式配置 reasoning_effort 时应用该档位）
            # - low/medium/high/xhigh/max: 开启思考并设置对应推理强度档位（从轻到重）
            current_reasoning_effort = (
                provider_data.get("reasoning_effort") if provider_data else None
            )
            current_disabled_fallback = (
                provider_data.get("reasoning_disabled_fallback") if provider_data else None
            )
            use_template_kwargs = current_thinking_type in ("chat_template_kwargs", "qwen")
            # 推理强度档位（从轻到重）: low < medium < high < xhigh < max
            # GLM 风格支持 low/high/max；Qwen3.8/vLLM/SGLang 风格支持 xhigh/medium/low
            _EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

            def _apply_effort_via_ctk(payload: dict[str, Any], effort: str) -> None:
                """推理强度档位经 chat_template_kwargs.reasoning_effort 传递。

                事务#1610根因：SGLang/vLLM 后端顶层 reasoning_effort 是
                Literal["none","low","medium","high","max"]（不含 xhigh），直接传
                xhigh 报 HTTP 400；传 max 又被 Qwen3.8 模板拒绝（仅支持
                xhigh/medium/low）。而 serving_chat.py 会把
                chat_template_kwargs.reasoning_effort pop 后直接赋给 request
                （绕过 Literal 校验）再注入模板，xhigh 等档位可正常生效
                （已 curl 实测验证）。
                """
                ctk = payload.setdefault("chat_template_kwargs", {})
                ctk["reasoning_effort"] = effort

            if effective_thinking == "enabled":
                if use_template_kwargs:
                    # 使用 chat_template_kwargs.enable_thinking 参数
                    payload["chat_template_kwargs"] = {"enable_thinking": True}
                    # enabled 模式的推理强度：显式配置 reasoning_effort 时应用
                    if current_reasoning_effort:
                        _apply_effort_via_ctk(payload, current_reasoning_effort)
                else:
                    # 使用 thinking 参数（默认、glm、thinking等）
                    payload["thinking"] = {"type": "enabled"}
                    # enabled 模式的推理强度：显式配置 reasoning_effort 时应用
                    # （"high" 级别映射为配置的 effort，如未配置则用默认档位）
                    if current_reasoning_effort:
                        payload["reasoning_effort"] = current_reasoning_effort
            elif effective_thinking in _EFFORT_LEVELS:
                # 推理档位模式（low/medium/high/xhigh/max）：开启思考并设置对应推理强度
                if use_template_kwargs:
                    # chat_template_kwargs 风格：档位经 ctk.reasoning_effort 传递
                    # （Qwen3.8 等模型 xhigh 档位只有该通路可表达，见上方注释）
                    payload["chat_template_kwargs"] = {"enable_thinking": True}
                    _apply_effort_via_ctk(payload, effective_thinking)
                else:
                    payload["thinking"] = {"type": "enabled"}
                    payload["reasoning_effort"] = effective_thinking
            elif effective_thinking == "disabled":
                if current_disabled_fallback:
                    # 模型不支持真正禁用思考（如 GLM-5.3）：
                    # 降级为最低/配置档位的思考强度，实现"最接近禁用"的效果
                    if use_template_kwargs:
                        payload["chat_template_kwargs"] = {"enable_thinking": True}
                        _apply_effort_via_ctk(payload, current_disabled_fallback)
                    else:
                        payload["thinking"] = {"type": "enabled"}
                        payload["reasoning_effort"] = current_disabled_fallback
                    logger.info(
                        f"[LLM Client] 模型不支持禁用思考，已降级为 "
                        f"enabled + reasoning_effort={current_disabled_fallback}"
                    )
                elif use_template_kwargs:
                    # 使用 chat_template_kwargs.enable_thinking 参数
                    payload["chat_template_kwargs"] = {"enable_thinking": False}
                else:
                    # 使用 thinking 参数（默认、glm、thinking等）
                    payload["thinking"] = {"type": "disabled"}
            elif effective_thinking == "auto":
                # auto 模式不设置 thinking 参数，让模型自己决定；
                # 但显式配置 reasoning_effort 时仍发送（作为该 provider 的默认推理深度，
                # 与 thinking_budget 在 auto 模式下仍生效的行为保持一致），
                # 仅 thinking 风格支持档位（chat_template_kwargs 风格走 ctk.reasoning_effort）
                if current_reasoning_effort:
                    if use_template_kwargs:
                        _apply_effort_via_ctk(payload, current_reasoning_effort)
                    else:
                        payload["thinking"] = {"type": "enabled"}
                        payload["reasoning_effort"] = current_reasoning_effort

            # 🔧 事务#2185：chat_template_kwargs 风格（Qwen3.8 系列）preserve_thinking=true
            # 时显式注入模板变量，确保后端渲染行为与 peng 配置语义一致。
            # 官方依据（Qwen3.8 chat_template.jinja L116）：
            #   {%- if preserve_thinking is undefined or preserve_thinking is true
            #        or loop.index0 > ns.last_query_index %}
            #       渲染 '<think>\n' + message.reasoning_content + '\n</think>'（保留）
            #   {%- else %} 剥离历史轮 thinking（仅保留当前工具循环轮）
            # 思考内容的唯一正确通道是 assistant 消息的 reasoning_content 字段
            # （模板 L112 只读 message.reasoning_content，不解析 content 内嵌 <think> 标签，
            # tool_engine.to_api_format 的序列化格式已正确）。
            # 不显式注入时依赖后端默认值：llama.cpp --reasoning-preserve 默认 enabled
            # （caps.cpp caps_apply_preserve_reasoning 注入 preserve_thinking 模板变量），
            # 但服务器若以 --no-reasoning-preserve 启动则历史思考被剥离，与配置背离；
            # SGLang 则完全依赖请求显式传参。故两种别名都注入：
            #   preserve_thinking=true  → Qwen3.8/vLLM/SGLang 模板直读（SGLang 将
            #                              chat_template_kwargs 全部键作为模板变量注入）
            #   preserve_reasoning=true → llama.cpp caps 机制（chat.cpp 触发
            #                              caps_apply_preserve_reasoning 同步设置
            #                              preserve_thinking/clear_thinking 等四个变量）
            if use_template_kwargs and provider_data and provider_data.get("preserve_thinking"):
                ctk = payload.setdefault("chat_template_kwargs", {})
                ctk["preserve_thinking"] = True
                ctk["preserve_reasoning"] = True
                logger.debug(
                    "[LLM Client] preserve_thinking=true: 已注入 chat_template_kwargs "
                    "{preserve_thinking: true, preserve_reasoning: true}（确保后端保留历史思考渲染）"
                )

            # 应用 provider 级别采样参数覆盖（按模型作者要求）
            # 部分模型（如 Qwen3）在不同 thinking 模式下需要不同的 temperature/top_p
            # 配置示例: {"thinking_on": {"temperature": 0.6, "top_p": 0.95}, "thinking_off": {"temperature": 0.001, "top_p": 1.0}}
            provider_sampling = provider_data.get("sampling_params") if provider_data else None
            if provider_sampling:
                # enabled/auto 模式下模型可能进行思考推理，使用 thinking_on 参数（更安全的采样）
                # disabled 模式下模型不做推理，使用 thinking_off 参数（低温精确）
                mode_key = "thinking_off" if effective_thinking == "disabled" else "thinking_on"
                mode_params = provider_sampling.get(mode_key)
                if mode_params:
                    if "temperature" in mode_params:
                        payload["temperature"] = mode_params["temperature"]
                    if "top_p" in mode_params:
                        payload["top_p"] = mode_params["top_p"]
                    logger.debug(
                        f"[LLM Client] 应用provider采样参数覆盖: mode={mode_key}, params={mode_params}"
                    )

            # 添加思考预算参数（限制推理过程的最大Token数）
            # 支持Qwen3（思考模式）、GLM与Kimi模型
            # 使用全局配置的thinking_budget作为唯一来源（与WebUI同步）
            effective_thinking_budget = getattr(self.config, "thinking_budget", 0)
            if effective_thinking_budget is not None and effective_thinking_budget > 0:
                # 互斥保护：Qwen3.8 等模型不支持 reasoning_effort 与 thinking_budget 同时设置
                # （同时传入会报错，见千问AI平台文档"控制推理深度"）；
                # 已设置 reasoning_effort 档位（顶层或 chat_template_kwargs 内）时跳过
                # thinking_budget，以档位方式控制推理深度
                if "reasoning_effort" in payload or (
                    "reasoning_effort" in (payload.get("chat_template_kwargs") or {})
                ):
                    logger.debug(
                        "[LLM Client] reasoning_effort 已设置，跳过 thinking_budget "
                        "（两者互斥，档位方式控制推理深度）"
                    )
                else:
                    payload["thinking_budget"] = effective_thinking_budget

            # 记录 thinking 相关的 payload 参数
            thinking_payload = {
                k: v
                for k, v in payload.items()
                if "think" in k.lower() or "effort" in k.lower() or "chat_template" in k.lower()
            }
            if thinking_payload:
                logger.debug(f"[LLM Client] thinking相关payload参数: {thinking_payload}")
            else:
                logger.debug(f"[LLM Client] 无thinking相关payload参数（auto模式或无参数）")

            if tools:
                payload["tools"] = tools

            # 🔧 事务#2204：原实现此处另起一个 try 块（缩进8），导致请求块落在并发
            # 槽位的 async with 之外——槽位在 payload 构建后、请求发出前就被释放，
            # 并发限制形同虚设（连进程内限制也失效）。现已合并到上面的 try 中，
            # 使槽位窗口覆盖"请求发出 → SSE 流式读取结束"全过程。
            # 同步更新 HTTP 客户端的 headers（立即生效）
            _call_client.headers["Authorization"] = f"Bearer {current_api_key}"
            logger.debug(f"[LLM Client] 发送POST请求: {url}")
            logger.debug(f"[LLM Client] payload keys: {list(payload.keys())}")
            # 🔧 事务#1864：prefill阶段追踪——请求发出前启动。本地大模型
            # （如s-qwen3.8）长上下文prefill期间SSE流无任何chunk返回，
            # chunk驱动的心跳会停更导致超时误杀。追踪线程定期更新心跳+
            # 写prefill状态文件（webui/CLI显示百分比），首个SSE行到达即停止。
            _prefill_est_tokens = 0
            try:
                _prefill_est_tokens = int(estimate_messages_tokens(messages))
            except Exception:
                pass
            try:
                from peng.llm.prefill_tracker import get_prefill_tracker

                get_prefill_tracker().start_request(
                    task_id=task_id,
                    est_tokens=_prefill_est_tokens,
                    provider=self.provider,
                    compact_threshold=self.compact_threshold,
                )
            except Exception as _pf_err:
                logger.debug(f"[LLM Client] prefill追踪启动失败(忽略): {_pf_err}")
            # 使用手动上下文管理替代 async with，避免 GeneratorExit
            # 传播到 __aexit__ 时触发 "athrow(): asynchronous generator is already running"
            cm = _call_client.stream("POST", url, json=payload)
            response = await cm.__aenter__()
            try:
                # 流式模式下，先检查状态码
                # 如果状态码非2xx，先尝试读取响应体（获取API返回的具体错误信息）
                if response.status_code >= 400:
                    # 尝试读取错误响应体
                    try:
                        error_body = await response.aread()
                        error_text = (
                            error_body.decode("utf-8", errors="ignore")[:1000] if error_body else ""
                        )
                    except Exception:
                        error_text = ""
                    # 记录详细的错误信息到日志
                    logger.warning(
                        f"[LLM Client] API返回HTTP {response.status_code}, "
                        f"provider={self.provider}, model={effective_model}, "
                        f"响应体: {error_text[:500] if error_text else '(空)'}"
                    )
                    # 手动构建异常信息，包含响应体内容
                    error_detail = (
                        error_text if error_text else f"HTTP {response.status_code} (响应体为空)"
                    )
                    raise httpx.HTTPStatusError(
                        error_detail,
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()

                current_content = ""
                current_thinking = ""  # 累积 thinking 内容
                current_tool_calls: dict[int, dict[str, Any]] = {}

                # Thinking 标签缓冲（处理跨 chunk 的标签）
                thinking_buffer = ""
                in_thinking = False
                sse_error_pending = False  # SSE error 事件待处理标记

                line_count = 0
                stall_timeout = self.config.timeout_read
                logger.debug(f"[LLM Client] 开始读取SSE流, stall_timeout={stall_timeout}s")

                # 🔧 记录请求开始时间（用于计算真实响应时间）
                request_start_time = time.time()
                try:
                    from peng.llm.throughput_tracker import get_throughput_tracker

                    get_throughput_tracker().start_request(self.provider)
                except Exception:
                    pass

                # 使用手动迭代 + asyncio.wait_for 实现停滞检测
                line_iterator = response.aiter_lines().__aiter__()
                while True:
                    try:
                        line = await asyncio.wait_for(
                            line_iterator.__anext__(),
                            timeout=stall_timeout,
                        )
                    except asyncio.TimeoutError:
                        # SSE流停滞：连接已建立但长时间没有新数据
                        # 🔧 事务#2277：抛出异常后由tool_engine决定重试策略——
                        # 默认同provider重新请求，仅3分钟窗口内停滞累计占比
                        # 过半（>=90s）才切换provider
                        logger.warning(
                            f"[LLM Client] SSE流停滞超过{stall_timeout}秒, "
                            f"provider={self.provider}, 抛出StreamStallError重新请求"
                        )
                        raise StreamStallError(
                            timeout=stall_timeout,
                            provider=self.provider,
                        )
                    except StopAsyncIteration:
                        # 流正常结束
                        break
                    line_count += 1
                    if line_count == 1:
                        # 🔧 事务#1864：SSE流首个数据行到达，prefill阶段结束，
                        # 停止prefill追踪（心跳恢复正常chunk驱动语义）
                        try:
                            from peng.llm.prefill_tracker import get_prefill_tracker

                            get_prefill_tracker().finish_request(task_id)
                        except Exception:
                            pass
                        logger.debug(f"[LLM Client] 第一行: {line[:200]}")
                    if line_count <= 3 or line_count % 100 == 0:
                        logger.debug(f"[LLM Client] 第{line_count}行: {line[:200]}")
                    if not line:
                        continue

                    # 处理 SSE error 事件（如 LM Studio 返回的 event: error）
                    # 格式：event: error\ndata: {"error":{"message":"..."}}
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                        if event_type == "error":
                            # 下一行应该包含 error data，但我们先标记
                            sse_error_pending = True
                            logger.warning(
                                f"[LLM Client] 收到SSE error事件, provider={self.provider}"
                            )
                        continue

                    if not line.startswith("data: "):
                        continue

                    data = line[6:]  # 移除 "data: " 前缀

                    # 检查是否是 SSE error 的 data 行
                    if sse_error_pending or (data.startswith("{") and '"error"' in data[:50]):
                        try:
                            error_data = json.loads(data)
                            if "error" in error_data:
                                error_msg = error_data["error"]
                                if isinstance(error_msg, dict):
                                    error_msg = error_msg.get("message", str(error_msg))
                                logger.error(
                                    f"[LLM Client] SSE流错误, provider={self.provider}: {error_msg}"
                                )
                                raise LLMError(f"SSE error from {self.provider}: {error_msg}")
                        except json.JSONDecodeError:
                            pass
                        sse_error_pending = False
                        continue
                    if data == "[DONE]":
                        # 处理缓冲区中残留的内容（未闭合的 thinking 标签）
                        if thinking_buffer:
                            if in_thinking:
                                # 未闭合的 thinking 标签，提取内容作为 thinking 输出
                                thinking_start = "༌"
                                if thinking_buffer.startswith(thinking_start):
                                    remaining_thinking = thinking_buffer[len(thinking_start) :]
                                    # 只有当 thinking 内容非空时才输出
                                    if remaining_thinking and remaining_thinking.strip():
                                        yield StreamChunk(thinking=remaining_thinking)
                                    elif remaining_thinking:
                                        # 空的 thinking 内容，跳过不输出
                                        pass
                                else:
                                    # 异常情况，直接作为普通内容输出
                                    yield StreamChunk(content=thinking_buffer)
                        logger.debug(f"[LLM Client] SSE流结束，共读取{line_count}行")
                        break

                    try:
                        chunk_data = json.loads(data)
                    except json.JSONDecodeError as e:
                        logger.debug(f"[LLM Client] JSON解析失败: {data[:100]}, error={e}")
                        continue

                    choices = chunk_data.get("choices", [])
                    if not choices:
                        # stream_options.include_usage=true 时，[DONE]前会发一个 choices=[] 但带 usage 的 chunk
                        usage_data = chunk_data.get("usage")
                        if usage_data:
                            logger.info(
                                f"[LLM Response] streaming usage chunk: "
                                f"prompt_tokens={usage_data.get('prompt_tokens')}, "
                                f"completion_tokens={usage_data.get('completion_tokens')}, "
                                f"total_tokens={usage_data.get('total_tokens')}"
                            )
                            # 修复: yield usage chunk, 让 tool_engine 能接收到真实token用量
                            # sglang/vLLM等后端在 finish_reason chunk 中不包含 usage
                            # 而是在单独的 choices=[] chunk 中返回
                            yield StreamChunk(usage=usage_data)
                        continue

                    delta = choices[0].get("delta", {})
                    finish_reason = choices[0].get("finish_reason")

                    # 处理文本内容
                    content = delta.get("content", "")
                    if content:
                        current_content += content
                        # 记录吞吐率（用于前端动画帧率调节）
                        try:
                            from peng.llm.throughput_tracker import get_throughput_tracker

                            get_throughput_tracker().record(self.provider, len(content))
                        except Exception as _tp_err:
                            logger.debug(f"[ThroughputTracker] record失败: {_tp_err}")
                        try:
                            # 根据 thinking_type 决定如何处理 thinking
                            if current_thinking_type == "glm":
                                # 强制使用 GLM 方式：不解析 标签
                                # content 全部作为普通内容返回
                                yield StreamChunk(content=content)
                            elif current_thinking_type == "qwen":
                                # 强制使用 Qwen 方式：解析 标签
                                thinking_parts, content_parts = self._parse_thinking_tags_streaming(
                                    content, thinking_buffer, in_thinking
                                )
                                thinking_buffer = thinking_parts.get("buffer", "")
                                in_thinking = thinking_parts.get("in_thinking", False)

                                # 如果有思考内容且非空，单独返回
                                thinking_content = thinking_parts.get("content", "")
                                if thinking_content and thinking_content.strip():
                                    current_thinking += thinking_content  # 累积 thinking
                                    yield StreamChunk(thinking=thinking_content)

                                # 返回普通内容
                                if content_parts:
                                    yield StreamChunk(content="".join(content_parts))
                            else:
                                # 自动检测：两种方式都尝试
                                # 先解析 标签（Qwen 方式）
                                thinking_parts, content_parts = self._parse_thinking_tags_streaming(
                                    content, thinking_buffer, in_thinking
                                )
                                thinking_buffer = thinking_parts.get("buffer", "")
                                in_thinking = thinking_parts.get("in_thinking", False)

                                # 如果有思考内容且非空，单独返回
                                thinking_content = thinking_parts.get("content", "")
                                if thinking_content and thinking_content.strip():
                                    current_thinking += thinking_content  # 累积 thinking
                                    yield StreamChunk(thinking=thinking_content)

                                # 返回普通内容
                                if content_parts:
                                    yield StreamChunk(content="".join(content_parts))
                        except GeneratorExit:
                            # 🔧 事务#2178：本次调用专属client由外层finally统一关闭
                            return

                    try:
                        # 处理思考/推理内容（GLM-5 等模型支持）
                        if current_thinking_type != "qwen":
                            # 检查思考/推理字段（不同后端字段名不同）：
                            # - OpenAI/阿里云: reasoning_content
                            # - vLLM: reasoning
                            # - GLM: thinking
                            thinking = (
                                delta.get("reasoning_content", "")
                                or delta.get("reasoning", "")
                                or delta.get("thinking", "")
                            )
                            if thinking:
                                current_thinking += thinking  # 累积 thinking
                                # 记录 thinking token 的吞吐率
                                try:
                                    from peng.llm.throughput_tracker import get_throughput_tracker

                                    get_throughput_tracker().record(self.provider, len(thinking))
                                except Exception:
                                    pass
                                yield StreamChunk(thinking=thinking)
                    except GeneratorExit:
                        # 🔧 事务#2178：本次调用专属client由外层finally统一关闭
                        return

                    # 处理工具调用（流式累加）
                    tool_calls_delta = delta.get("tool_calls", [])
                    if tool_calls_delta:
                        for tc in tool_calls_delta:
                            index = tc.get("index", 0)
                            if index not in current_tool_calls:
                                current_tool_calls[index] = {
                                    "id": tc.get("id", ""),
                                    "type": tc.get("type", "function"),
                                    "function": {"name": "", "arguments": ""},
                                }

                            # 记录 tool_call 各部分的吞吐率
                            tc_len = 0
                            if tc.get("id"):
                                current_tool_calls[index]["id"] = tc["id"]
                                tc_len += len(tc["id"])

                            func = tc.get("function", {})
                            if func.get("name"):
                                current_tool_calls[index]["function"]["name"] = func["name"]
                                tc_len += len(func["name"])
                            if func.get("arguments"):
                                arg_str = func["arguments"]
                                current_tool_calls[index]["function"]["arguments"] += arg_str
                                tc_len += len(arg_str)
                            if tc_len > 0:
                                try:
                                    from peng.llm.throughput_tracker import get_throughput_tracker

                                    get_throughput_tracker().record(self.provider, tc_len)
                                except Exception:
                                    pass

                    # 处理结束
                    if finish_reason:
                        # 提取API返回的真实token用量
                        current_usage = chunk_data.get("usage")

                        # 记录响应统计信息
                        content_length = len(current_content)
                        tool_calls_count = len(current_tool_calls)
                        logger.debug(
                            f"[LLM Response] finish_reason={finish_reason}, "
                            f"content_length={content_length}, tool_calls={tool_calls_count}"
                        )
                        if current_usage:
                            logger.info(
                                f"[LLM Response] usage: prompt_tokens={current_usage.get('prompt_tokens')}, "
                                f"completion_tokens={current_usage.get('completion_tokens')}, "
                                f"total_tokens={current_usage.get('total_tokens')}"
                            )

                        # 记录完整的响应日志（根据配置）
                        if llm_logger:
                            if (
                                should_log("llm", "response.content", llm_logs_config)
                                and current_content
                            ):
                                content_preview = current_content[:1000] + (
                                    "..." if len(current_content) > 1000 else ""
                                )
                                llm_logger.debug(f"[RESPONSE] content: {content_preview}")
                            if (
                                should_log("llm", "response.thinking", llm_logs_config)
                                and current_thinking
                            ):
                                thinking_preview = current_thinking[:2000] + (
                                    "..." if len(current_thinking) > 2000 else ""
                                )
                                llm_logger.debug(f"[RESPONSE] thinking: {thinking_preview}")
                            if (
                                should_log("llm", "response.tool_calls", llm_logs_config)
                                and current_tool_calls
                            ):
                                tool_calls_json = json.dumps(
                                    current_tool_calls, ensure_ascii=False, indent=2
                                )
                                llm_logger.debug(f"[RESPONSE] tool_calls: {tool_calls_json}")
                            if should_log("llm", "response.metadata", llm_logs_config):
                                llm_logger.debug(
                                    f"[RESPONSE] metadata: finish_reason={finish_reason}, "
                                    f"content_length={content_length}, "
                                    f"thinking_length={len(current_thinking)}, "
                                    f"tool_calls={tool_calls_count}"
                                )

                        # 如果有完整的工具调用，返回它们
                        if current_tool_calls:
                            tool_calls = []
                            for idx in sorted(current_tool_calls.keys()):
                                tc_data = current_tool_calls[idx]
                                try:
                                    args = json.loads(tc_data["function"]["arguments"])
                                except json.JSONDecodeError:
                                    args = {}
                                tool_call_obj = ToolCall(
                                    id=tc_data["id"],
                                    name=tc_data["function"]["name"],
                                    arguments=args,
                                )
                                tool_calls.append(tool_call_obj)
                                logger.debug(
                                    f"[LLM Response] tool_call: "
                                    f"{tc_data['function']['name']}({args})"
                                )

                            yield StreamChunk(
                                tool_calls=tool_calls,
                                finish_reason=finish_reason,
                                usage=current_usage,
                            )
                        else:
                            # 如果没有标准tool_call，尝试从content中解析xml格式的tool_call（sglang格式）
                            xml_tool_calls = self._parse_xml_tool_calls(current_content)
                            if xml_tool_calls:
                                logger.debug(
                                    f"[LLM Response] 从content解析到xml格式tool_call: "
                                    f"{len(xml_tool_calls)}个"
                                )
                                yield StreamChunk(
                                    tool_calls=xml_tool_calls,
                                    finish_reason=finish_reason,
                                    usage=current_usage,
                                )
                            else:
                                yield StreamChunk(finish_reason=finish_reason, usage=current_usage)

                    # 🔧 记录真实usage数据（completion_tokens和响应时间）- 在yield之后统一处理，避免遗漏
                    if finish_reason:
                        try:
                            completion_tokens = (
                                current_usage.get("completion_tokens", 0) if current_usage else 0
                            )
                            duration_seconds = (
                                time.time() - request_start_time
                                if "request_start_time" in dir()
                                else 0
                            )
                            from peng.llm.throughput_tracker import get_throughput_tracker

                            get_throughput_tracker().record_usage(
                                self.provider, completion_tokens, duration_seconds
                            )
                            logger.debug(
                                f"[LLM Client] record_usage: provider={self.provider}, "
                                f"completion_tokens={completion_tokens}, duration={duration_seconds:.2f}s"
                            )
                        except Exception as e:
                            logger.debug(f"[ThroughputTracker] record_usage失败: {e}")

                        # 如果有结束标志且内容不以换行符结尾，则添加换行符
                        if current_content and not current_content.endswith("\n"):
                            yield StreamChunk(content="\n")

                # SSE流正常结束，标记provider成功
                if self.provider and self.config:
                    self.config.mark_success(self.provider)
            finally:
                # 🔧 事务#1864：流结束（正常/异常/中断）兜底停止prefill追踪，
                # 防止请求失败后追踪线程残留持续更新心跳掩盖真卡死（幂等操作）
                try:
                    from peng.llm.prefill_tracker import get_prefill_tracker

                    get_prefill_tracker().finish_request(task_id)
                except Exception:
                    pass
                # 🔧 事务#1941：流结束（正常/异常/中断）配对结束吞吐率追踪，
                # 活跃请求归零时速率清0，消除空闲进程的幽灵速率残留
                try:
                    from peng.llm.throughput_tracker import get_throughput_tracker

                    get_throughput_tracker().end_request(self.provider)
                except Exception:
                    pass
                # 手动退出上下文管理器，确保 HTTP 连接被关闭
                # 捕获 RuntimeError：当 GeneratorExit 触发时，Python 会自动清理嵌套的
                # 异步生成器，此时再手动调用 __aexit__ 会因 "athrow(): asynchronous generator is already running" 而失败
                try:
                    await cm.__aexit__(None, None, None)
                except RuntimeError:
                    pass
                # 🔧 事务#2178：关闭本次调用的专属client（连接池）——旧版每次LLM调用
                # new一个AsyncClient却只有GeneratorExit路径才关，正常/break结束都泄漏
                try:
                    await _call_client.aclose()
                except Exception:
                    pass
        except asyncio.CancelledError:
            text = f"⚠️  [llm.client]检测到中断。\n"
            logger.debug(text)
            pass
        except GeneratorExit:
            pass
        except Exception as e:
            raise
        finally:
            # 🔧 事务#2204：释放跨进程并发槽位（正常/异常/中断/取消/生成器关闭
            # 各路径均覆盖）。release() 为同步操作，GeneratorExit 展开路径安全。
            _slot_handle.release()

    def _parse_thinking_tags_streaming(
        self, content: str, buffer: str = "", in_thinking: bool = False
    ) -> tuple[dict[str, Any], list[str]]:
        """
        流式解析 content 中的 <think></think> 标签，支持跨 chunk 处理

        Qwen 等模型会在响应中包含 <think></think> 标签，需要将它们分离出来
        以便分别用不同颜色显示。此方法支持处理被拆分到多个 chunk 中的标签。

        Args:
            content: 当前 chunk 的内容字符串
            buffer: 上一个 chunk 留下的缓冲区内容
            in_thinking: 是否正处于 thinking 标签内

        Returns:
            (thinking_info, content_parts):
                - thinking_info: 包含 thinking 内容和缓冲状态的字典
                - content_parts: 普通内容列表

        示例:
            输入："<think>思考内容</think>正式回答"
            输出：({"content": "思考内容", "buffer": "", "in_thinking": False}, ["正式回答"])

            输入："<think>思考" (未闭合)
            输出：({"content": "", "buffer": "<think>思考", "in_thinking": True}, [])

            输入："<think></think>" (空标签)
            输出：({"content": "", "buffer": "", "in_thinking": False}, [])
        """
        # 标签常量
        thinking_start = "༌"
        thinking_end = "pmat"

        # 合并缓冲区和当前内容
        full_content = buffer + content if buffer else content

        thinking_result = {"content": "", "buffer": "", "in_thinking": in_thinking}
        content_parts = []

        if in_thinking:
            # 已经在 thinking 标签内，buffer 中存放的是之前 chunk 累积的 thinking 内容（不含 ི 标签）
            # 在当前 chunk 中查找结束标签
            end_pos = full_content.find(thinking_end)
            if end_pos != -1:
                # 找到结束标签，full_content[:end_pos] 就是完整的 thinking 内容
                thinking_result["content"] = full_content[:end_pos]
                # 剩余内容继续解析（可能有多个标签或普通内容）
                remaining = full_content[end_pos + len(thinking_end) :]
                thinking_result["in_thinking"] = False
                # 递归处理剩余内容
                if remaining:
                    sub_result, sub_content = self._parse_thinking_tags_streaming(
                        remaining, "", False
                    )
                    if sub_result.get("content"):
                        thinking_result["content"] += sub_result["content"]
                    thinking_result["buffer"] = sub_result["buffer"]
                    thinking_result["in_thinking"] = sub_result["in_thinking"]
                    content_parts.extend(sub_content)
                return thinking_result, content_parts
            # 未找到结束标签，全部内容都是 thinking 的一部分，存入缓冲区
            thinking_result["buffer"] = full_content
            return thinking_result, content_parts
        # 不在 thinking 标签内，查找开始标签
        start_pos = full_content.find(thinking_start)
        if start_pos != -1:
            # 找到开始标签，提取标签前的普通内容
            before_tag = full_content[:start_pos]
            if before_tag:
                content_parts.append(before_tag)

            # 从开始标签之后查找结束标签
            remaining = full_content[start_pos + len(thinking_start) :]
            end_pos = remaining.find(thinking_end)

            if end_pos != -1:
                # 找到完整标签，提取 thinking 内容
                thinking_result["content"] = remaining[:end_pos]
                thinking_result["in_thinking"] = False
                # 递归处理剩余内容（可能有多个标签）
                after_tag = remaining[end_pos + len(thinking_end) :]
                if after_tag:
                    sub_result, sub_content = self._parse_thinking_tags_streaming(
                        after_tag, "", False
                    )
                    if sub_result.get("content"):
                        thinking_result["content"] += sub_result["content"]
                    thinking_result["buffer"] = sub_result["buffer"]
                    thinking_result["in_thinking"] = sub_result["in_thinking"]
                    content_parts.extend(sub_content)
                return thinking_result, content_parts
            # 未找到结束标签，进入 thinking 状态，buffer 存储已累积的 thinking 内容（不含 ི）
            thinking_result["in_thinking"] = True
            thinking_result["buffer"] = remaining
            return thinking_result, content_parts

        # 兼容sglang格式：检查sglang结束标签（sglang可能缺少开始标签）
        # 注意：不依赖sglang开始标签，只检查结束标签
        sglang_end = "</think>"
        sglang_end_pos = full_content.find(sglang_end)
        if sglang_end_pos != -1:
            # 找到sglang结束标签
            # 检查是否有Qwen开始标签（Qwen优先）
            qwen_start_pos = full_content.find(thinking_start)
            if qwen_start_pos == -1 or qwen_start_pos > sglang_end_pos:
                # 没有Qwen开始标签，或者Qwen开始标签在sglang结束标签之后
                # 将sglang结束标签之前的内容当作thinking内容
                thinking_result["content"] = full_content[:sglang_end_pos]
                thinking_result["in_thinking"] = False
                # 递归处理剩余内容
                after_tag = full_content[sglang_end_pos + len(sglang_end) :]
                if after_tag:
                    sub_result, sub_content = self._parse_thinking_tags_streaming(
                        after_tag, "", False
                    )
                    if sub_result.get("content"):
                        thinking_result["content"] += sub_result["content"]
                    thinking_result["buffer"] = sub_result["buffer"]
                    thinking_result["in_thinking"] = sub_result["in_thinking"]
                    content_parts.extend(sub_content)
                return thinking_result, content_parts

        # 没有开始标签，全部是普通内容
        content_parts.append(full_content)
        return thinking_result, content_parts

    def _parse_xml_tool_calls(self, content: str) -> list[ToolCall]:
        """
        解析 content 中的 xml 格式 tool_call（sglang 格式）

        sglang 格式的 tool_call 格式如下：
        <function=read>
        <parameter=filePath>
        /tmp/webui.log
        </parameter>
        </function>

        Args:
            content: 包含 xml 格式 tool_call 的内容字符串

        Returns:
            ToolCall 对象列表
        """
        tool_calls = []

        # 使用正则表达式匹配 <function>...</function> 标签
        pattern = r"<function=([^>]+)>(.*?)</function>"
        for match in re.finditer(pattern, content, re.DOTALL):
            function_name = match.group(1).strip()
            function_body = match.group(2)

            # 解析参数：匹配 <parameter=参数名>参数值</parameter> 格式
            arguments = {}
            param_pattern = r"<parameter=([^>]+)>(.*?)</parameter>"
            for param_match in re.finditer(param_pattern, function_body, re.DOTALL):
                param_name = param_match.group(1).strip()
                param_value = param_match.group(2).strip()
                arguments[param_name] = param_value

            # 创建 ToolCall 对象
            tool_call = ToolCall(
                id=f"sglang_{len(tool_calls)}", name=function_name, arguments=arguments
            )
            tool_calls.append(tool_call)

            logger.debug(
                f"[LLM Client] 从content解析xml格式tool_call: {function_name}({arguments})"
            )

        return tool_calls

    def _parse_thinking_tags(self, content: str) -> tuple[list[str], list[str]]:
        """
        解析 content 中的 <think\\> 标签，分离思考内容和普通内容

        Qwen 等模型会在响应中包含 <think\\> 标签，需要将它们分离出来
        以便分别用不同颜色显示。

        Args:
            content: 原始内容字符串

        Returns:
            (thinking_parts, content_parts): 思考内容列表和普通内容列表

        示例:
            输入："<think\\>思考内容\\</think\\>正式回答"
            输出：(["思考内容"], ["正式回答"])

            输入："开始 <think\\>思考\\</think\\>中间 <think\\>更多思考\\</think\\>结束"
            输出：(["思考", "更多思考"], ["开始 ", "中间 ", "结束"])
        """
        thinking_parts = []
        content_parts = []

        last_end = 0
        for match in _THINKING_TAG_PATTERN.finditer(content):
            # 添加标签前的普通内容
            if match.start() > last_end:
                content_parts.append(content[last_end : match.start()])

            # 添加标签内的思考内容
            thinking_parts.append(match.group(1))

            last_end = match.end()

        # 添加最后一个标签后的普通内容
        if last_end < len(content):
            content_parts.append(content[last_end:])
        return thinking_parts, content_parts

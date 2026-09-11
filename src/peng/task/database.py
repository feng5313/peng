"""
事务管理模块 - 数据库操作

实现事务记录的持久化存储，包括：
- 事务表 (tasks)
- 步骤表 (task_steps)
- 依赖关系表 (task_dependencies)
"""

from __future__ import annotations

import asyncio
import logging
import re
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg2

from peng.common import MetaSingleton
from peng.common import jsonp as json
from peng.db.connection import get_connection

from .task_types import StepStatus, TaskPriority, TaskStatus, TaskType

logger = logging.getLogger(__name__)

from peng.common import MetaSingleton


def get_context_tokens(context: str | None) -> int:
    """计算事务上下文的 token 数量。"""
    if context:
        from peng.llm import MessageHistory

        history = MessageHistory()
        if history.from_str(context):
            return history.get_token_count()
    return 0


def _fast_context_meta(context: str | None) -> dict:
    """快速提取 context JSON 中的元数据（不创建 MessageHistory 对象）。

    直接解析 JSON 获取 _prompt_tokens 和 _total_cost 等顶层字段，
    避免 from_str 中昂贵的 from_api_format + remove_empty_tail 处理。

    Args:
        context: context JSON 字符串

    Returns:
        {"prompt_tokens": int, "total_cost": float, "total_tokens_consumed": int,
         "total_input_hit": int, "total_input_miss": int, "total_output": int}
    """
    if not context:
        return {}
    try:
        data = json.loads(context)
        if isinstance(data, dict):
            return {
                "prompt_tokens": data.get("_prompt_tokens", 0),
                "total_cost": data.get("_total_cost", 0.0),
                "total_tokens_consumed": data.get("_total_tokens_consumed", 0),
                "total_input_hit": data.get("_total_input_hit_tokens", 0),
                "total_input_miss": data.get("_total_input_miss_tokens", 0),
                "total_output": data.get("_total_output_tokens", 0),
            }
    except Exception:
        pass
    return {}


def get_context_total_tokens(context: str | None) -> int:
    """获取事务累计总token消耗。"""
    if context:
        from peng.llm import MessageHistory

        history = MessageHistory()
        if history.from_str(context):
            return history.get_total_tokens_consumed()
    return 0


def get_context_cost(context: str | None) -> float:
    """获取事务累计费用（元）。"""
    if context:
        from peng.llm import MessageHistory

        history = MessageHistory()
        if history.from_str(context):
            return history.get_total_cost()
    return 0.0


def get_context_usage_detail(context: str | None) -> dict:
    """获取事务分项token用量。

    Returns:
        {"input_hit": int, "input_miss": int, "output": int}
    """
    if context:
        from peng.llm import MessageHistory

        history = MessageHistory()
        if history.from_str(context):
            return {
                "input_hit": history.get_input_hit_tokens(),
                "input_miss": history.get_input_miss_tokens(),
                "output": history.get_output_tokens(),
            }
    return {"input_hit": 0, "input_miss": 0, "output": 0}


def _get_cost_ansi(cost: float) -> str:
    """根据费用返回RGB渐变ANSI颜色码。

    绿(<0.1元) → 橙(>10元) 线性插值。

    Args:
        cost: 事务累计费用（元）

    Returns:
        ANSI 24-bit 颜色码字符串
    """
    if cost <= 0:
        return ""
    lo = 0.1   # 0.1元 → 绿
    hi = 10.0  # 10元 → 橙红
    ratio = (cost - lo) / (hi - lo) if cost > lo else 0.0
    ratio = min(max(ratio, 0.0), 1.0)
    # 绿(63,185,80) → 橙红(255,100,60)
    r = int(63 + (255 - 63) * ratio)
    g = int(185 + (100 - 185) * ratio)
    b = int(80 + (60 - 80) * ratio)
    return f"\033[38;2;{r};{g};{b}m"


def _get_cost_html_color(cost: float) -> str:
    """根据费用返回RGB渐变HTML颜色码（绿→橙红线性插值）。"""
    if cost <= 0:
        return "#3fb950"
    lo = 0.1   # 0.1元 → 绿
    hi = 10.0  # 10元 → 橙红
    ratio = (cost - lo) / (hi - lo) if cost > lo else 0.0
    ratio = min(max(ratio, 0.0), 1.0)
    r = int(63 + (255 - 63) * ratio)
    g = int(185 + (100 - 185) * ratio)
    b = int(80 + (60 - 80) * ratio)
    return f"rgb({r},{g},{b})"


def _get_consumption_ansi(total_tokens: int) -> str:
    """根据总token消耗返回RGB渐变ANSI颜色码。

    蓝(<100K) → 紫(>2M) 线性插值。

    Args:
        total_tokens: 事务累计总token消耗

    Returns:
        ANSI 24-bit 颜色码字符串
    """
    if total_tokens <= 0:
        return ""
    # 渐变区间
    lo = 100_000   # 100K → 蓝
    hi = 2_000_000  # 2M → 紫
    ratio = (total_tokens - lo) / (hi - lo) if total_tokens > lo else 0.0
    ratio = min(max(ratio, 0.0), 1.0)
    # 蓝(70,130,255) → 紫(200,100,255)
    r = int(70 + (200 - 70) * ratio)
    g = int(130 + (100 - 130) * ratio)
    b = 255  # 蓝色分量恒定255
    return f"\033[38;2;{r};{g};{b}m"


def _get_compact_threshold(provider: str = "") -> int:
    """获取指定 provider 的上下文压缩阈值（compact_threshold）。

    通过 config.llm.get_provider 获取 provider 配置中的 compact_threshold。

    Args:
        provider: provider 名称（task 的 provider 字段），空则返回全局默认值

    Returns:
        compact_threshold 值，获取失败时返回默认值 300000
    """
    try:
        from peng.config.loader import load_config

        config = load_config()
        if provider:
            provider_data = config.llm.get_provider(current=provider, locked=True)
            if provider_data:
                return provider_data.get("compact_threshold", config.llm.compact_threshold)
        return config.llm.compact_threshold
    except Exception:
        return 300000


def get_context_color(tokens: int, provider: str = "") -> str:
    """根据上下文 token 数返回颜色级别。

    高亮阈值根据 task 的 provider 对应的 compact_threshold 等比例缩放。
    以 128K 为基准：
        - red 阈值 = compact_threshold × (100000 / 128000) ≈ 78.1%
        - yellow 阈值 = compact_threshold × (50000 / 128000) ≈ 39.1%

    颜色说明：
        - "context_red": 上下文过长（降智危险），使用提高亮度降低饱和度的红色
        - "yellow":      上下文偏长（警告）
        - "green":       上下文安全

    Args:
        tokens: 上下文 token 数
        provider: provider 名称，用于获取对应的 compact_threshold

    Returns:
        颜色名称
    """
    compact_threshold = _get_compact_threshold(provider)
    # 以 128K 为基准等比例缩放
    red_threshold = int(compact_threshold * 100000 / 128000)
    yellow_threshold = int(compact_threshold * 50000 / 128000)

    if tokens > red_threshold:
        return "context_red"
    elif tokens > yellow_threshold:
        return "yellow"
    return "green"


class TaskDatabase(metaclass=MetaSingleton):
    """
    事务数据库管理器

    负责：
    - 创建和初始化数据库表
    - 事务的 CRUD 操作
    - 步骤管理
    - 依赖关系管理

    注：事务ID使用AUTOINCREMENT，自动保证唯一性和递增，
    无需额外的计数器机制。
    """

    # 需要通知前端的事务字段（这些字段变化时触发 WebSocket 广播）
    _BROADCAST_FIELDS = {
        "parent_task_id",
        "status",
        "content",
        "provider",
        "nest",
        "workdir",
        "is_archived",
        "collapsed",
        "message_id",
    }

    # tasks 表所有列名（与实际数据库表结构一致，用于 exclude_fields 优化）
    # 注意：修改表结构时需同步更新此列表
    _TASK_COLUMNS = [
        "id",
        "title",
        "content",
        "description",
        "status",
        "progress",
        "priority",
        "task_type",
        "created_at",
        "started_at",
        "completed_at",
        "deadline",
        "timer",
        "last_restart_at",
        "message_id",
        "parent_task_id",
        "root_id",
        "depth",
        "path",
        "caller",
        "caller_session_id",
        "worktree_path",
        "branch_name",
        "worktree_locked",
        "process_id",
        "error",
        "result",
        "timeout",
        "timeout_started_at",
        "heartbeat_at",
        "heartbeat_timeout",
        "expertise",
        "analysis_report",
        "subtask_config",
        "tool_calls_log",
        "tool_results_log",
        "metadata",
        "is_archived",
        "archived_at",
        "current_step_index",
        "current_step_name",
        "collapsed",
        "pause_context",
        "prompt",
        "workdir",
        "small_model",
        "info",
        "is_complex",
        "context",
        "creator",
        "provider",
        "nest",
        "checklist",
        "report",
    ]

    # SQL: 创建事务表
    CREATE_TASKS_TABLE = """
        CREATE TABLE IF NOT EXISTS tasks (
            -- 主键
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            -- 基本信息
            title TEXT NOT NULL,
            content TEXT,
            description TEXT,

            -- 状态和进度
            status TEXT NOT NULL DEFAULT 'pending',
            progress INTEGER DEFAULT 0,

            -- 优先级和类型
            priority INTEGER DEFAULT 2,
            task_type TEXT DEFAULT '',
            is_complex INTEGER DEFAULT 0,

            -- 时间
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            deadline TEXT,

            -- 定时器（定时重启）
            timer INTEGER DEFAULT 0,
            last_restart_at TEXT,

            -- 关联
            message_id INTEGER,
            parent_task_id INTEGER,
            root_id INTEGER,
            depth INTEGER DEFAULT 0,
            path TEXT DEFAULT '',
            caller TEXT,
            caller_session_id TEXT,

            -- Git Worktree
            worktree_path TEXT,
            workdir TEXT,
            branch_name TEXT,
            worktree_locked INTEGER DEFAULT 0,

            -- 执行信息
            process_id INTEGER,
            error TEXT,
            result TEXT,

            -- 超时
            timeout INTEGER DEFAULT 0,
            timeout_started_at TEXT,

            -- 心跳
            heartbeat_at TEXT,
            heartbeat_timeout INTEGER DEFAULT NULL,  -- NULL表示使用 config monitor.default_heartbeat_timeout

            -- 专家指派 JSON（需求 §5.4.4）
            expertise TEXT,

            -- 分析报告 JSON（需求 §5.4.5）
            analysis_report TEXT,

            -- 子事务 JSON（需求 §5.4.3）
            subtask_config TEXT,

            -- Tool Call 日志（本事务自身调用的 tool_call 和参数）
            tool_calls_log TEXT DEFAULT '',

            -- Tool Result 日志（返回给大模型的调用结果）
            tool_results_log TEXT DEFAULT '',

            -- 元数据
            metadata TEXT DEFAULT '',
            info TEXT DEFAULT '',
            context TEXT DEFAULT '',
            creator TEXT DEFAULT '',
            provider TEXT DEFAULT '',

            -- 验收清单和工作汇报（Markdown 文本）
            checklist TEXT DEFAULT '',
            report TEXT DEFAULT '',

            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE SET NULL,
            FOREIGN KEY (parent_task_id) REFERENCES tasks(id) ON DELETE SET NULL
        )
    """

    # SQL: 创建步骤表
    CREATE_STEPS_TABLE = """
        CREATE TABLE IF NOT EXISTS task_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            sort_order INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            error TEXT,
            result TEXT,

            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
    """

    # SQL: 创建依赖关系表
    CREATE_DEPENDENCIES_TABLE = """
        CREATE TABLE IF NOT EXISTS task_dependencies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            depends_on_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,

            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (depends_on_id) REFERENCES tasks(id) ON DELETE CASCADE,
            UNIQUE(task_id, depends_on_id)
        )
    """

    # SQL: 创建事务控制表（用于暂停/恢复/重启/取消等控制）
    CREATE_TASK_CONTROLS_TABLE = """
        CREATE TABLE IF NOT EXISTS task_controls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            processed INTEGER DEFAULT 0,

            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
    """

    # SQL: 创建索引
    CREATE_INDEXES = [
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_message ON tasks(message_id)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_root ON tasks(root_id)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_depth ON tasks(depth)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_caller ON tasks(caller)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks(deadline)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_heartbeat ON tasks(heartbeat_at)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_worktree ON tasks(worktree_path)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_timer ON tasks(timer)",
        "CREATE INDEX IF NOT EXISTS idx_steps_task ON task_steps(task_id)",
        "CREATE INDEX IF NOT EXISTS idx_deps_task ON task_dependencies(task_id)",
        "CREATE INDEX IF NOT EXISTS idx_deps_depends ON task_dependencies(depends_on_id)",
        "CREATE INDEX IF NOT EXISTS idx_task_controls_task ON task_controls(task_id)",
        "CREATE INDEX IF NOT EXISTS idx_task_controls_processed ON task_controls(processed)",
    ]

    def __init__(self):
        """
        初始化数据库

        Args:
            db_path: 数据库路径（从配置读取）
        """
        pass

    # ==================== WebUI 通知 ====================

    @staticmethod
    def _notify_webui(action: str, task_id: int, details: dict[str, Any] | None = None) -> None:
        """
        通知所有 WebUI 客户端事务数据变更

        通过 WebSocket 广播 task_update 消息，携带变更字段详情，
        前端可直接增量更新 DOM，无需重新请求 /api/tasks。

        线程安全设计：
        - 主进程：通过 ws_manager._loop（ConnectionManager 首个连接时捕获的主事件循环）
          使用 asyncio.run_coroutine_threadsafe() 线程安全地调度广播协程。
          该 API 从任何线程（事件循环线程或工作线程）调用都是安全的。
        - 子进程：子进程的 ws_manager 没有活跃连接和 _loop，
          通过 HTTP 请求主进程的内部分发接口触发广播。

        ⚡ 性能优化（Task#1438）：在 CLI 子进程中，peng.webui.websocket 模块尚未被导入，
        直接 import 会拉起 fastapi + lark_oapi 等庞大依赖链（耗时 3+ 秒）。
        通过 sys.modules 检查避免在子进程中触发这个昂贵的导入：
        - 主进程：模块已在启动时导入（sys.modules 中存在），走 WebSocket 快速路径
        - 子进程：模块未导入，直接走 HTTP 通知，跳过 3 秒的 import 开销

        Args:
            action: 操作类型（created/updated/deleted）
            task_id: 事务ID
            details: 变更字段详情（可选）
        """
        try:
            import sys

            # ⚡ 性能优化：仅在模块已加载时才访问（主进程），避免子进程中的 3 秒 import 开销
            ws_module = sys.modules.get("peng.webui.websocket")
            if ws_module is not None:
                ws_manager = getattr(ws_module, "manager", None)
                loop = getattr(ws_manager, "_loop", None) if ws_manager else None
                if loop and ws_manager and ws_manager.active_connections:
                    # 主进程：使用存储的事件循环引用，线程安全地调度广播协程
                    broadcast_fn = getattr(ws_module, "broadcast_task_update", None)
                    if broadcast_fn:
                        asyncio.run_coroutine_threadsafe(
                            broadcast_fn(task_id, action, details or {}),
                            loop,
                        )
                        return
            # 子进程或 WebUI 未启动：通过 HTTP 请求主进程触发广播
            TaskDatabase._notify_webui_via_http(action, task_id, details or {})
        except Exception as e:
            # 通知失败不应阻塞数据库操作，仅记录 debug 日志
            logger.debug(f"[TaskDB] 通知WebUI失败(task#{task_id}, {action}): {e}")

    @staticmethod
    def _notify_webui_via_http(action: str, task_id: int, details: dict[str, Any]) -> None:
        """
        子进程中通过 HTTP 请求主进程内部分发接口触发 WebSocket 广播

        事务在子进程中运行时，子进程的 WebSocket ConnectionManager 没有活跃连接，
        因此需要通过 HTTP 请求主进程的 /api/internal/broadcast-task 端点来广播。
        """
        try:
            from peng.config.loader import load_config
            import urllib.request
            import urllib.error

            config = load_config()
            host = config.webui.host if hasattr(config, "webui") and config.webui else "localhost"
            port = config.webui.port if hasattr(config, "webui") and config.webui else 6688
            # 绑定到 0.0.0.0 时用 localhost 连接
            if host == "0.0.0.0":
                host = "localhost"

            url = f"http://{host}:{port}/api/internal/broadcast-task"
            payload = json.dumps(
                {
                    "task_id": task_id,
                    "action": action,
                    "details": details,
                }
            ).encode("utf-8")

            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status != 200:
                    logger.debug(f"[TaskDB] HTTP广播返回非200: {resp.status}")
        except Exception as e:
            # HTTP广播失败不阻塞，前端会通过轮询兜底
            logger.debug(f"[TaskDB] HTTP广播失败(task#{task_id}, {action}): {e}")

    def _get_connection(self):
        """
        获取PostgreSQL数据库连接

        使用PostgreSQL连接池获取连接

        Returns:
            PostgreSQL连接对象
        """
        return get_connection()

    # ==================== 事务 CRUD ====================

    def create_task(self, **kwargs: Any) -> int:
        """
        创建事务（通用字典参数形式）

        所有字段通过 kwargs 传入，与 update_tasks 风格一致。
        常用字段：title, content, description, priority, task_type, message_id,
        parent_task_id, caller, caller_session_id, timeout, deadline, metadata,
        root_id, depth, path, worktree_path, branch_name, worktree_locked,
        expertise, prompt, workdir, status, small_model, info, creator,
        is_complex, timer, id, nest

        Returns:
            事务 ID
        """
        content = kwargs.get("content", "")
        message_id = kwargs.get("message_id")
        parent_task_id = kwargs.get("parent_task_id")

        # 【保护】确保一个根消息只创建一个根事务
        existing_id = self._check_root_task_duplicate(message_id, parent_task_id)
        if existing_id is not None:
            return existing_id

        # 步骤1：提取并转换所有字段
        fields = self._extract_create_fields(kwargs)

        # 步骤2：解析 provider
        fields["provider"] = self._parse_provider_from_content(content)

        # 步骤3：解析 timer
        timer = kwargs.get("timer", 0)
        if timer is None or timer == 0:
            if content:
                from peng.task.timer import parse_timer

                parsed_timer = parse_timer(content=content)
                if parsed_timer:
                    fields["timer"] = int(parsed_timer)

        # 步骤4：计算事务树字段（需要获取父事务）
        parent = self.get_task(parent_task_id) if parent_task_id else None
        fields = self._compute_tree_fields(fields, parent)
        # 子事务继承事务树根的 message_id
        if not fields["message_id"] and fields["root_id"]:
            root_task = self.get_task(fields["root_id"])
            if root_task:
                fields["message_id"] = root_task.get("message_id")

        # 步骤5：继承 nest 和 workdir
        fields = self._inherit_nest_and_workdir(fields, parent)

        # 步骤6：插入数据库记录
        task_id = self._insert_task_record(fields)

        # 步骤7：添加依赖关系
        self._add_wait_dependencies(task_id, content)

        # 🔧 通知 WebUI：新事务创建
        details = {k: v for k, v in fields.items() if k in self._BROADCAST_FIELDS and v is not None}
        self._notify_webui("created", task_id, details)

        return task_id

    # ==================== create_task 子方法 ====================

    def _extract_create_fields(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """
        从 kwargs 提取并类型转换所有 create_task 所需字段。

        Returns:
            包含所有已转换字段的字典
        """
        # 原始提取
        priority = kwargs.get("priority", TaskPriority.NORMAL)
        task_type = kwargs.get("task_type", TaskType.AUTO)
        message_id = kwargs.get("message_id")
        parent_task_id = kwargs.get("parent_task_id")
        root_id = kwargs.get("root_id")
        depth = kwargs.get("depth", 0)
        worktree_locked = kwargs.get("worktree_locked", False)
        small_model = kwargs.get("small_model", False)
        is_complex = kwargs.get("is_complex", False)
        timer = kwargs.get("timer", 0)
        task_id_specified = kwargs.get("id")
        status = kwargs.get("status", "pending")
        metadata = kwargs.get("metadata")

        # 类型转换（避免 psycopg2 格式化错误）
        priority_value = priority.value if isinstance(priority, TaskPriority) else int(priority)
        type_value = task_type.value if isinstance(task_type, TaskType) else str(task_type)
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
        worktree_locked_int = 1 if worktree_locked else 0
        small_model_int = 1 if small_model else 0
        is_complex_int = 1 if is_complex else 0
        timer_int = int(timer) if timer is not None else 0
        # 心跳超时：未指定（None）或<=0 时使用配置默认值（monitor.default_heartbeat_timeout）
        heartbeat_timeout = kwargs.get("heartbeat_timeout")
        if heartbeat_timeout is None or int(heartbeat_timeout) <= 0:
            from peng.config.loader import get_default_heartbeat_timeout

            heartbeat_timeout = get_default_heartbeat_timeout()
        else:
            heartbeat_timeout = int(heartbeat_timeout)
        message_id = int(message_id) if message_id else None
        parent_task_id = int(parent_task_id) if parent_task_id else None
        root_id = int(root_id) if root_id else None
        depth = int(depth) if depth is not None else 0
        status = status if isinstance(status, str) else status.value

        return {
            "title": kwargs.get("title") or "",
            "content": kwargs.get("content") or "",
            "description": kwargs.get("description"),
            "status": status,
            "priority": priority_value,
            "task_type": type_value,
            "created_at": datetime.now().isoformat(),
            "message_id": message_id,
            "parent_task_id": parent_task_id,
            "caller": kwargs.get("caller"),
            "caller_session_id": kwargs.get("caller_session_id"),
            "timeout": kwargs.get("timeout", 0),
            "deadline": kwargs.get("deadline"),
            "metadata": metadata_json,
            "root_id": root_id,
            "depth": depth,
            "path": kwargs.get("path") or "",
            "worktree_path": kwargs.get("worktree_path"),
            "branch_name": kwargs.get("branch_name"),
            "worktree_locked": worktree_locked_int,
            "expertise": kwargs.get("expertise") or "",
            "prompt": kwargs.get("prompt") or "",
            "workdir": kwargs.get("workdir"),
            "provider": "",  # 后续由 _parse_provider_from_content 填充
            "small_model": small_model_int,
            "is_complex": is_complex_int,
            "info": kwargs.get("info") or "",
            "creator": kwargs.get("creator") or "",
            "timer": timer_int,
            "heartbeat_timeout": heartbeat_timeout,
            "nest": kwargs.get("nest"),
            "id": task_id_specified,
        }

    def _check_root_task_duplicate(
        self, message_id: int | None, parent_task_id: int | None = None
    ) -> int | None:
        """
        检查是否已存在同一消息的根事务（防止重复创建）。

        Args:
            message_id: 关联消息ID
            parent_task_id: 父事务ID（有值时跳过去重检查）

        Returns:
            已存在的根事务ID，或 None（无重复时）
        """
        if parent_task_id or not message_id:
            return None
        existing_tasks = self.list_tasks(message_id=message_id, root_only=True)
        if existing_tasks:
            existing_task_id = existing_tasks[0].get("id")
            logger.info(f"[TaskDB] 消息#{message_id}已存在根事务#{existing_task_id}，跳过创建")
            return existing_task_id
        return None

    def _parse_provider_from_content(self, content: str) -> str:
        """
        从 content 中解析 @provider 标记。

        Args:
            content: 事务内容字符串

        Returns:
            解析到的 provider 名称，未找到则返回空字符串
        """
        if not content:
            return ""
        from peng.config.loader import load_config

        config = load_config()
        fav_providers = config.llm.fetch_providers(content)
        return fav_providers[0] if fav_providers and fav_providers[0] else ""

    def _compute_tree_fields(
        self, fields: dict[str, Any], parent: dict[str, Any] | None
    ) -> dict[str, Any]:
        """
        计算事务树字段（root_id, depth, path）。

        对于子事务：从父事务继承/计算这些字段。
        对于根事务：保持原值（后续在 _insert_task_record 中更新）。

        Args:
            fields: 已提取的字段字典
            parent: 父事务字典（根事务时为 None）

        Returns:
            更新了树字段的 fields 字典
        """
        if not fields.get("parent_task_id") or not parent:
            return fields

        # 从父事务继承/计算
        if not fields.get("root_id"):
            fields["root_id"] = parent.get("root_id") or parent.get("id")
        if fields.get("depth") == 0:
            fields["depth"] = (parent.get("depth") or 0) + 1
        if not fields.get("path"):
            parent_path = parent.get("path") or ""
            parent_task_id = fields["parent_task_id"]
            fields["path"] = (
                f"{parent_path}/{parent_task_id}" if parent_path else f"/{parent_task_id}"
            )
        return fields

    def _inherit_nest_and_workdir(
        self, fields: dict[str, Any], parent: dict[str, Any] | None
    ) -> dict[str, Any]:
        """
        继承 nest 和 workdir（优先级：显式指定 > 消息 > 父事务 > config）。

        Args:
            fields: 已提取的字段字典
            parent: 父事务字典（根事务时为 None）

        Returns:
            更新了 nest/workdir 的 fields 字典
        """
        nest = fields.get("nest")
        workdir = fields.get("workdir")
        message_id = fields.get("message_id")
        parent_task_id = fields.get("parent_task_id")

        # 从关联消息继承 nest（如果未指定）
        if not nest and message_id:
            try:
                from peng.message.database import MessageDatabase

                msg_db = MessageDatabase()
                msg = msg_db.get_message(message_id)
                if msg and msg.get("nest"):
                    nest = msg.get("nest")
                    logger.debug(f"[create_task] 从消息#{message_id}继承nest: {nest}")
            except Exception:
                pass

        # 从父事务继承 nest（如果仍未指定）
        if not nest and parent_task_id and parent:
            if parent.get("nest"):
                nest = parent.get("nest")
                logger.debug(f"[create_task] 从父事务#{parent_task_id}继承nest: {nest}")

        # 从 config.webui 继承（兜底）
        if not nest or not workdir:
            try:
                from peng.config.loader import load_config

                config = load_config()
                if not nest:
                    nest = getattr(getattr(config, "webui", None), "nest", "") or ""
                if not workdir:
                    workdir = getattr(getattr(config, "webui", None), "workdir", "") or ""
            except Exception:
                if not nest:
                    nest = ""
                if not workdir:
                    workdir = ""

        fields["nest"] = nest
        fields["workdir"] = workdir
        return fields

    def _insert_task_record(self, fields: dict[str, Any]) -> int:
        """
        执行 SQL INSERT 插入事务记录，并更新 root_id/path。

        Args:
            fields: 所有已准备好的字段字典

        Returns:
            创建的事务 ID
        """
        task_id_specified = fields.get("id")
        parent_task_id = fields.get("parent_task_id")
        root_id = fields.get("root_id")
        parent_path = fields.get("path", "")  # 注意：这是父事务的 path（不含自己）

        with self._get_connection() as conn:
            # 动态构建 INSERT 语句
            insert_columns = [
                "title",
                "content",
                "description",
                "status",
                "priority",
                "task_type",
                "created_at",
                "message_id",
                "parent_task_id",
                "caller",
                "caller_session_id",
                "timeout",
                "deadline",
                "metadata",
                "root_id",
                "depth",
                "path",
                "worktree_path",
                "branch_name",
                "worktree_locked",
                "expertise",
                "prompt",
                "workdir",
                "provider",
                "small_model",
                "is_complex",
                "info",
                "creator",
                "timer",
                "heartbeat_timeout",
                "nest",
                "checklist",
                "report",
            ]
            insert_values = [
                fields["title"],
                fields["content"],
                fields["description"],
                fields["status"],
                fields["priority"],
                fields["task_type"],
                fields["created_at"],
                fields["message_id"],
                fields["parent_task_id"],
                fields["caller"],
                fields["caller_session_id"],
                fields["timeout"],
                fields["deadline"],
                fields["metadata"],
                fields["root_id"],
                fields["depth"],
                fields["path"],
                fields["worktree_path"],
                fields["branch_name"],
                fields["worktree_locked"],
                fields["expertise"],
                fields["prompt"],
                fields["workdir"],
                fields["provider"],
                fields["small_model"],
                fields["is_complex"],
                fields["info"],
                fields["creator"],
                fields["timer"],
                # None 时插入 NULL（语义=使用配置默认值，心跳监控端 or default 兜底）
                fields.get("heartbeat_timeout"),
                fields["nest"],
                "",
                "",
            ]

            if task_id_specified:
                insert_columns.insert(0, "id")
                insert_values.insert(0, task_id_specified)

            col_names = ", ".join(insert_columns)
            placeholders = ", ".join(["%s"] * len(insert_columns))
            cursor = conn.execute(
                f"INSERT INTO tasks ({col_names}) VALUES ({placeholders})",
                tuple(insert_values),
            )
            task_id = task_id_specified or cursor.lastrowid

            # 🔧 推进 PostgreSQL SERIAL 序列，防止后续自动生成的 id 与显式指定的 id 冲突
            if task_id_specified:
                conn.execute(
                    "SELECT setval(pg_get_serial_sequence('tasks', 'id'), "
                    "(SELECT COALESCE(MAX(id), 1) FROM tasks), true)"
                )

            # 创建事务的后续字段更新（root_id/path 需要在获取 task_id 后才能设置）
            # 注：此处属于创建流程的一部分，不视为独立的字段更新操作
            if not parent_task_id and not root_id:
                conn.execute(
                    "UPDATE tasks SET root_id = %s, path = %s WHERE id = %s",
                    (task_id, f"/{task_id}", task_id),
                )
            elif parent_task_id:
                # 子事务：更新path为正确的路径（使用新创建的事务ID）
                correct_path = f"{parent_path}/{task_id}" if parent_path else f"/{task_id}"
                conn.execute(
                    "UPDATE tasks SET path = %s WHERE id = %s",
                    (correct_path, task_id),
                )

        return task_id

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        """
        获取事务

        Args:
            task_id: 事务ID

        Returns:
            事务字典，不存在返回 None
        """
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM tasks WHERE id = %s", (task_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_tasks_by_ids(self, task_ids: list[int]) -> list[dict[str, Any]]:
        """
        批量获取多个事务（并发优化：减少N次查询为1次）

        Args:
            task_ids: 事务ID列表

        Returns:
            事务字典列表（仅返回存在的事务）
        """
        if not task_ids:
            return []

        with self._get_connection() as conn:
            placeholders = ",".join(["%s"] * len(task_ids))
            cursor = conn.execute(
                f"SELECT * FROM tasks WHERE id IN ({placeholders})",
                task_ids,
            )
            return [dict(row) for row in cursor.fetchall()]

    def update_tasks(
        self,
        task_ids: list[int],
        _where_conditions: list[str] | None = None,
        _broadcast_extra: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> int:
        """
        批量更新事务

        所有 tasks 表字段的 UPDATE 操作必须通过本方法实现。
        除 create_task 外，禁止在 database.py 外部直接执行 SQL 操作 tasks 表字段。

        Args:
            task_ids: 事务ID列表
            _where_conditions: 额外的 WHERE 条件列表（如 ["status = 'pending'"]），
                用于需要原子性条件更新的场景（如 start_task、complete_task）。
                注意：这是内部参数，外部调用者不应使用。
            _broadcast_extra: 广播时额外携带的字段（如 {"icon": "🤖"}），
                不写入数据库但随 WebSocket 广播到前端。
        Returns:
            更新的行数

        Raises:
            ValueError: 如果状态值不合法
        """
        if not task_ids:
            return 0

        if not kwargs:
            return 0

        # 验证状态值
        if "status" in kwargs:
            VALID_STATUSES = {
                "pending",
                "running",
                "completed",
                "failed",
                "interrupted",
                "paused",
                "cancelled",
                "accepting",
            }
            if kwargs["status"] not in VALID_STATUSES:
                return 0

        # 解析 content 中的 @timer=<int> 表达式
        timer = kwargs.get("timer", None)
        content = kwargs.get("content", None)
        if not timer and content:
            from peng.task.timer import parse_timer

            timer = parse_timer(content=content)
            if timer:
                kwargs["timer"] = timer
        provider = kwargs.get("provider", None)
        if not provider and content:
            from peng.config.loader import load_config

            provider = ""
            config = load_config()
            fav_providers = config.llm.fetch_providers(content)
            if fav_providers and fav_providers[0]:
                provider = fav_providers[0][0]
                kwargs["provider"] = provider

        fields = []
        values = []
        for key, value in kwargs.items():
            # 如果值是 dict 或 list 类型，需要转换为 JSON 字符串
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            # 允许None值更新（用于将字段设为NULL）
            fields.append(f"{key} = %s")
            values.append(value)
        placeholders = ",".join(["%s"] * len(task_ids))

        # 构建完整的 WHERE 子句
        where_parts = [f"id IN ({placeholders})"]
        if _where_conditions:
            where_parts.extend(_where_conditions)
        where_clause = " AND ".join(where_parts)

        with self._get_connection() as conn:
            cursor = conn.execute(
                f"UPDATE tasks SET {', '.join(fields)} WHERE {where_clause}",
                values + task_ids,
            )
            affected = cursor.rowcount

        # 🔧 通知 WebUI：检测关键字段变化，主动广播给前端增量更新
        if affected > 0:
            broadcast_fields = {k: v for k, v in kwargs.items() if k in self._BROADCAST_FIELDS}
            if _broadcast_extra:
                broadcast_fields.update(_broadcast_extra)
            if broadcast_fields:
                for tid in task_ids:
                    self._notify_webui("updated", tid, broadcast_fields)

        return affected

    def update_task(self, task_id: int, **kwargs: Any) -> bool:
        """
        更新事务

        Args:
            task_id: 事务ID
            **kwargs: 要更新的字段

        Returns:
            是否成功

        Raises:
            ValueError: 如果状态值不合法
        """
        return self.update_tasks([task_id], **kwargs) > 0

    def move_task(
        self,
        task_id: int,
        target_id: int | None,
        only_task: bool = False,
    ) -> bool:
        """
        移动事务到新的父事务下或变成树根

        Args:
            task_id: 要移动的事务ID
            target_id: 新的父事务ID，为None时变成树根

        Returns:
            是否成功（失败时返回 False 并通过 logger.warning 记录原因）
        """
        # 获取要移动的事务
        task = self.get_task(task_id)
        if not task:
            logger.warning(f"[move_task] 事务 {task_id} 不存在")
            return False

        # 系统事务保护：禁止移动系统事务（#1鹏🐣忠实助手，#2事务总管）
        if task_id <= 2:
            logger.warning(f"[move_task] 系统事务 {task_id} 受保护，禁止移动")
            return False

        # 检查是否尝试移动到自己的子孙事务下（防止循环）
        descendants = self.get_descendants(task_id)
        descendant_ids = [d["id"] for d in descendants]
        if target_id in descendant_ids:
            logger.warning(
                f"[move_task] 不能将事务 {task_id} 移动到其子孙事务 {target_id} 下，这会造成循环引用"
            )
            return False

        # 如果target_id为None，或者为#1忠实助手，#2事务总管，移动到根
        if not target_id or target_id < 3:
            # 更新为根事务
            new_root_id = task_id
            new_depth = 0
            new_path = f"/{task_id}"

            update_fields = {
                "parent_task_id": None,
                "root_id": new_root_id,
                "depth": new_depth,
                "path": new_path,
            }
            # 变成根事务后需要创建独立的根消息
            message_id = self._create_root_message_for_task(task_id)
            update_fields["message_id"] = message_id

            # 更新事务本身
            self.update_task(task_id, **update_fields)
            descendant_update_fields = {
                "root_id": new_root_id,
                "message_id": message_id,
            }
            self.update_tasks(descendant_ids, **descendant_update_fields)
            # 递归更新所有子事务
            self._update_children_paths(task_id, new_root_id, new_depth, new_path)
            logger.info(f"事务 {task_id} 已移动为根事务")
            return True

        # 检查是否尝试移动到自身
        if target_id == task_id:
            logger.warning(f"[move_task] 不能将事务 {task_id} 移动到自身下")
            return False

        # 获取新父事务
        new_parent = self.get_task(target_id)
        if not new_parent:
            logger.warning(f"[move_task] 新父事务 {target_id} 不存在")
            return False

        # Bug #10修复：禁止移动到已归档的事务下
        if new_parent.get("is_archived"):
            logger.warning(f"[move_task] 不能将事务移动到已归档的事务 {target_id} 下")
            return False

        # 计算新的树结构字段
        new_root_id = new_parent.get("root_id") or target_id
        new_depth = (new_parent.get("depth", 0) or 0) + 1
        new_path = f"{new_parent.get('path', '')}/{task_id}"

        # Bug #3修复：获取新事务树根的 message_id，用于同步更新
        new_root_task = self.get_task(new_root_id)
        new_tree_message_id = new_root_task.get("message_id") if new_root_task else None

        # 方案3：根事务变子事务时，记录原 message_id 用于后续处理关联消息
        message_id = task.get("message_id")
        is_root_task = not task.get("parent_task_id")

        # 更新事务本身
        update_fields = {
            "parent_task_id": target_id,
            "root_id": new_root_id,
            "depth": new_depth,
            "path": new_path,
        }
        if new_tree_message_id:
            # 自动继承新事务树根的 message_id
            update_fields["message_id"] = new_tree_message_id

        self.update_task(task_id, **update_fields)

        # 递归更新所有子事务的路径，并同步 message_id
        self._update_children_paths(task_id, new_root_id, new_depth, new_path)
        # Bug #3修复：同步子事务的 message_id
        if new_tree_message_id:
            sync_msg_id = new_tree_message_id
            self._update_children_message_ids(task_id, sync_msg_id)

        # 根事务变子事务时，同步移动关联的消息
        if is_root_task and message_id and not only_task:
            chain_head_id = update_fields.get("message_id")
            from peng.message.database import MessageDatabase

            db = MessageDatabase()
            db.move_message(message_id, target_id=chain_head_id, only_message=True)

        logger.info(
            f"事务 {task_id} 已移动到父事务 {target_id} 下 "
            f"(root_id={new_root_id}, depth={new_depth}, path={new_path})"
        )
        return True

    def _update_children_paths(
        self, parent_id: int, root_id: int, parent_depth: int, parent_path: str
    ) -> None:
        """
        递归更新所有子事务的root_id、depth、path

        Args:
            parent_id: 父事务ID
            root_id: 根事务ID
            parent_depth: 父事务depth
            parent_path: 父事务path
        """
        # 查找所有直接子事务
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT id FROM tasks WHERE parent_task_id = %s",
                (parent_id,),
            )
            children = [row["id"] for row in cursor.fetchall()]

        # 更新每个子事务
        for child_id in children:
            child_depth = parent_depth + 1
            child_path = f"{parent_path}/{child_id}"

            self.update_tasks(
                [child_id],
                root_id=root_id,
                depth=child_depth,
                path=child_path,
            )

            # 递归更新子事务的子事务
            self._update_children_paths(child_id, root_id, child_depth, child_path)

    def _update_children_message_ids(self, parent_id: int, message_id: int) -> None:
        """
        递归更新所有子事务的 message_id（Bug #3修复辅助方法）

        Args:
            parent_id: 父事务ID
            message_id: 要同步的 message_id
        """
        # 查找所有直接子事务
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT id FROM tasks WHERE parent_task_id = %s",
                (parent_id,),
            )
            children = [row["id"] for row in cursor.fetchall()]

        # 更新每个子事务
        for child_id in children:
            self.update_tasks([child_id], message_id=message_id)
            # 递归更新子事务的子事务
            self._update_children_message_ids(child_id, message_id)

    def _create_root_message_for_task(self, task_id: int) -> int | None:
        """
        为变成根事务的事务创建对应的根消息（Bug #12修复辅助方法）

        当分支事务变成根事务时，需要创建一条根消息来承载它，
        保持消息树和事务树的对应关系。

        Args:
            task_id: 事务ID

        Returns:
            创建的消息ID，失败返回None
        """
        task = self.get_task(task_id)
        if not task:
            return 0

        try:
            from peng.message.database import MessageDatabase
            from peng.config import get_agent_name

            msg_db = MessageDatabase()
            agent_name = get_agent_name()

            content = task.get("content") or f"事务#{task_id}（从子事务提升为根事务）"
            workdir = task.get("workdir")

            # 根据事务状态决定消息状态
            task_status = task.get("status", "pending")
            if task_status in ("completed", "cancelled", "failed"):
                msg_status = "done"
            else:
                msg_status = "processing"

            msg_id, msg = msg_db.create_message(
                content=content,
                sender=agent_name,
                receiver=agent_name,
                caller="move_task",
                workdir=workdir,
                status=msg_status,
            )

            if msg_id:
                logger.info(
                    f"[move_task] 为根事务#{task_id}创建根消息#{msg_id}（状态：{msg_status}）"
                )
            return msg_id
        except Exception as e:
            logger.error(
                f"[move_task] 为事务#{task_id}创建根消息失败: {e}\n{traceback.format_exc()}"
            )
            return 0

    def update_tasks_status(self, task_ids: list[int], status: str) -> int:
        """
        批量更新事务状态

        Args:
            task_ids: 事务ID列表
            status: 新状态

        Returns:
            更新的行数

        Raises:
            ValueError: 如果状态值不合法
        """
        return self.update_tasks(task_ids, status=status)

    def delete_task(self, task_id: int, force=False) -> bool:
        """
        删除事务

        Args:
            task_id: 事务ID

        Returns:
            是否成功
        """
        if not force and task_id <= 2:
            import traceback

            logger.error(
                f"事务#1 #2为鹏🐣-忠实助手和事务总管，禁止删除 {'\n'.join(traceback.format_stack())}"
            )
            return False

        # 🔧 删除前先获取事务信息（用于通知前端）
        task_info = self.get_task(task_id)

        with self._get_connection() as conn:
            cursor = conn.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
            deleted = cursor.rowcount > 0

        # 🔧 通知 WebUI：事务删除（含被删事务的 message_id，前端据此清理消息关联）
        if deleted:
            details = {}
            if task_info:
                details["message_id"] = task_info.get("message_id")
                details["parent_task_id"] = task_info.get("parent_task_id")
            self._notify_webui("deleted", task_id, details)

        return deleted

    def delete_task_recursive(
        self, task_id: int, force=False, _delete_message=True
    ) -> int:
        """
        递归删除事务及其所有子事务，同时递归删除对应的根消息

        🔧 Task#1346：批量优化，消除N+1查询
        旧实现逐层递归 SELECT child + delete_task，N个子事务产生 2N+次连接池操作 + N次广播。
        新实现用 get_descendants 一次性获取所有后代ID，然后一条SQL批量DELETE + 只广播一次。

        Args:
            task_id: 事务ID
            _delete_message: 内部参数，是否删除关联消息（仅在顶层调用时为True，
                            递归调用子事务时为False，避免重复删除）

        Returns:
            删除的事务数量
        """
        if not force and task_id <= 2:
            import traceback

            logger.error(
                f"事务#1 #2为鹏🐣-忠实助手和事务总管，禁止删除 {'\n'.join(traceback.format_stack())}"
            )
            return 0

        # 删除前获取事务信息（用于后续删除关联消息）
        task_info = self.get_task(task_id) if _delete_message else None

        # 🔧 Task#1346：先删除关联消息（含广播通知），再删除事务自身
        # 确保前端收到 message_update deleted 时消息已从 DB 删除
        # 🔧 Task#1237：仅根事务（无 parent_task_id）才删除关联消息！
        if _delete_message and task_info and not task_info.get("parent_task_id"):
            message_id = task_info.get("message_id")
            if message_id:
                try:
                    from peng.message.database import MessageDatabase

                    msg_db = MessageDatabase()
                    msg_deleted = msg_db.delete_message_recursive(
                        message_id, force=force
                    )
                    if msg_deleted:
                        logger.info(
                            f"[delete_task_recursive] 事务#{task_id} 关联消息#{message_id} 已删除 {msg_deleted} 条"
                        )
                except Exception as e:
                    logger.error(
                        f"[delete_task_recursive] 删除事务#{task_id} 关联消息#{message_id} 失败: {e}"
                    )

        # 🔧 Task#1346：批量获取所有后代ID（含自身），一条SQL删除
        all_ids = [task_id]
        descendants = self.get_descendants(task_id)
        all_ids.extend(d["id"] for d in descendants)

        # 一条SQL批量DELETE所有事务
        with self._get_connection() as conn:
            placeholders = ",".join(["%s"] * len(all_ids))
            cursor = conn.execute(
                f"DELETE FROM tasks WHERE id IN ({placeholders})",
                all_ids,
            )
            deleted_count = cursor.rowcount

        # 🔧 Task#867/Task#1346：只广播一次根事务删除事件
        # 前端收到后会 loadTasks 全量刷新，所有子事务都会从列表中消失
        # 不逐条广播，避免通知风暴
        if deleted_count > 0:
            self._notify_webui("deleted", task_id, {})

        return deleted_count

    def list_tasks(
        self,
        status: TaskStatus | str | None = None,
        priority: TaskPriority | int | None = None,
        message_id: int | None = None,
        caller: str | None = None,
        limit: int | None = None,
        order: str = "ASC",
        root_only: bool = False,
        include_archived: bool = True,
        exclude_fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        列出事务

        Args:
            status: 状态过滤
            priority: 优先级过滤
            message_id: 消息ID过滤
            caller: 调用者过滤
            limit: 返回数量限制
            exclude_fields: 需要从 SELECT 中排除的字段列表（如 ["context"] 避免传输大字段）
            order: 排序方式（ASC/DESC）
            root_only: 是否只返回根事务（parent_task_id为null或0）
            include_archived: 是否包含已归档事务（根据关联消息的is_archived判断）

        Returns:
            事务列表
        """
        conditions = []
        params: list[Any] = []

        if status is not None:
            status_value = status.value if isinstance(status, TaskStatus) else status
            conditions.append("t.status = %s")
            params.append(status_value)

        if priority is not None:
            priority_value = priority.value if isinstance(priority, TaskPriority) else priority
            conditions.append("t.priority = %s")
            params.append(priority_value)

        if message_id:
            conditions.append("t.message_id = %s")
            params.append(message_id)

        if caller:
            conditions.append("t.caller = %s")
            params.append(caller)

        # 根事务过滤
        if root_only:
            conditions.append("(t.parent_task_id IS NULL OR t.parent_task_id = 0)")

        # 归档过滤（根据事务自己的is_archived字段判断）
        if not include_archived:
            conditions.append("(t.is_archived = 0 OR t.is_archived IS NULL)")

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        order_clause = "ASC" if order == "ASC" else "DESC"
        # 优化：去掉 LEFT JOIN messages（瓶颈#6优化，阶段一#1.6）
        # 性能优化：exclude_fields 支持排除大字段（如 context，单个可达600KB+）
        if exclude_fields:
            # 使用预定义的列名列表，避免每次查询 information_schema
            exclude_set = set(exclude_fields)
            selected_columns = ", ".join(
                f"t.{col}" for col in self._TASK_COLUMNS if col not in exclude_set
            )
            select_clause = f"SELECT {selected_columns} FROM tasks t"
        else:
            select_clause = "SELECT t.* FROM tasks t"
        sql = (
            f"{select_clause} WHERE {where_clause} ORDER BY t.priority, t.created_at {order_clause}"
        )

        if limit is not None:
            sql += f" LIMIT {limit}"

        with self._get_connection() as conn:
            cursor = conn.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]

    def get_root_tasks_message_status(
        self, include_archived: bool = True
    ) -> list[dict[str, Any]]:
        """
        批量获取所有根事务及其关联消息的归档状态（高性能版本）。

        用于 cmd_trees 的自动归档/删除扫描，避免：
        1. SELECT * 拉取巨大的 context 字段（200MB+）
        2. 对每条事务逐个调用 get_message() 的 N+1 查询

        单次 SQL JOIN 获取所有根事务的 id, message_id, is_archived
        以及关联消息的 is_archived 状态。

        Args:
            include_archived: 是否包含已归档事务

        Returns:
            根事务列表，每条包含: id, message_id, is_archived, msg_is_archived, msg_exists
        """
        conditions = ["(t.parent_task_id IS NULL OR t.parent_task_id = 0)"]
        if not include_archived:
            conditions.append("(t.is_archived = 0 OR t.is_archived IS NULL)")
        where_clause = " AND ".join(conditions)

        with self._get_connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT t.id, t.message_id, t.is_archived,
                       m.id AS msg_id,
                       m.is_archived AS msg_is_archived
                FROM tasks t
                LEFT JOIN messages m ON t.message_id = m.id
                WHERE {where_clause}
                ORDER BY t.priority, t.created_at ASC
                """,
            )
            rows = cursor.fetchall()

        result = []
        for row in rows:
            d = dict(row)
            d["msg_exists"] = d.get("msg_id") is not None
            result.append(d)
        return result

    def get_subtree_batch(
        self, root_ids: list[int]
    ) -> dict[int, list[dict[str, Any]]]:
        """
        批量获取多个事务树的全部节点（高性能版本）。

        用于 cmd_trees 显示多个事务树时，避免对每棵树逐个递归查询。
        单次 SQL 查询所有 root_id 下的事务（含 context），
        按 root_id 分组返回。

        Args:
            root_ids: 根事务ID列表

        Returns:
            {root_id: [task_dict, ...]} 字典，按 depth, path 排序
        """
        if not root_ids:
            return {}

        placeholders = ",".join(["%s"] * len(root_ids))
        with self._get_connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT * FROM tasks
                WHERE root_id IN ({placeholders})
                ORDER BY root_id, depth, path
                """,
                tuple(root_ids),
            )
            rows = [dict(row) for row in cursor.fetchall()]

        # 按 root_id 分组
        result: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            rid = row.get("root_id") or row["id"]
            if rid not in result:
                result[rid] = []
            result[rid].append(row)

        # 确保请求的每个 root_id 都有条目（即使只有自己）
        for rid in root_ids:
            if rid not in result:
                # 该事务可能没有 root_id 字段或 root_id 是自己
                # 需要单独查询
                with self._get_connection() as conn:
                    cursor = conn.execute(
                        "SELECT * FROM tasks WHERE id = %s", (rid,)
                    )
                    row = cursor.fetchone()
                    if row:
                        result[rid] = [dict(row)]

        return result

    # ==================== 状态转换 ====================

    def start_task(self, task_id: int) -> bool:
        """开始事务（原子性：仅当状态为 pending 时才更新）"""
        now = datetime.now().isoformat()
        return (
            self.update_tasks(
                [task_id],
                _where_conditions=["status = 'pending'"],
                status="running",
                started_at=now,
                heartbeat_at=now,
            )
            > 0
        )

    def complete_task(self, task_id: int, result: str | None = None) -> bool:
        """完成事务（原子性：仅当状态为 running 时才更新）"""
        now = datetime.now().isoformat()
        return (
            self.update_tasks(
                [task_id],
                _where_conditions=["status = 'running'"],
                status="completed",
                completed_at=now,
                progress=100,
                result=result,
                process_id=None,
            )
            > 0
        )

    def fail_task(self, task_id: int, error: str) -> bool:
        """标记事务失败（原子性：仅当状态为 pending/running 时才更新）"""
        now = datetime.now().isoformat()
        return (
            self.update_tasks(
                [task_id],
                _where_conditions=["status IN ('pending', 'running')"],
                status="failed",
                completed_at=now,
                error=error,
                process_id=None,
            )
            > 0
        )

    # ==================== 步骤管理 ====================

    def add_step(
        self,
        task_id: int,
        name: str,
        order: int = 0,
        description: str | None = None,
    ) -> int:
        """添加步骤"""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO task_steps (task_id, name, sort_order, description, created_at, status)
                VALUES (%s, %s, %s, %s, %s, 'pending')
                """,
                (task_id, name, order, description, now),
            )
            return cursor.lastrowid

    def get_steps(self, task_id: int) -> list[dict[str, Any]]:
        """获取事务的步骤列表"""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM task_steps WHERE task_id = %s ORDER BY sort_order",
                (task_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def update_step_status(
        self, step_id: int, status: StepStatus, error: str | None = None
    ) -> bool:
        """更新步骤状态"""
        now = datetime.now().isoformat()
        status_value = status.value if isinstance(status, StepStatus) else status

        with self._get_connection() as conn:
            if status == StepStatus.RUNNING:
                cursor = conn.execute(
                    "UPDATE task_steps SET status = %s, started_at = %s WHERE id = %s",
                    (status_value, now, step_id),
                )
            elif status in (StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED):
                cursor = conn.execute(
                    "UPDATE task_steps SET status = %s, completed_at = %s, error = %s WHERE id = %s",
                    (status_value, now, error, step_id),
                )
            else:
                cursor = conn.execute(
                    "UPDATE task_steps SET status = %s WHERE id = %s",
                    (status_value, step_id),
                )
            return cursor.rowcount > 0

    # ==================== 依赖管理 ====================

    def add_dependency(self, task_id: int, depends_on_id: int) -> bool:
        """添加依赖关系"""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO task_dependencies (task_id, depends_on_id, created_at)
                    VALUES (%s, %s, %s)
                    """,
                    (task_id, depends_on_id, now),
                )
                return True
            except psycopg2.IntegrityError:
                return False  # 已存在

    def _parse_wait_refs(self, content: str) -> list[int]:
        """
        解析 content 中的 wait 引用

        支持的格式：
        - @事务#id 或 @事务 #id（事务引用）

        Args:
            content: 事务内容字符串

        Returns:
            task_ids 事务ID列表
        """
        task_ids: list[int] = []

        if not content:
            return task_ids

        # 匹配 @事务#id 和 @消息#id（支持 # 前有空格的情况）
        task_pattern = r"@wait#(\d+)"
        for match in re.finditer(task_pattern, content):
            try:
                tid = int(match.group(1))
                if tid > 0 and tid not in task_ids:
                    task_ids.append(tid)
            except (ValueError, IndexError):
                pass
        return task_ids

    def _add_wait_dependencies(self, task_id: int, content: str) -> None:
        """
        解析 content 中的 wait 引用并添加到 task_dependencies 表

        - @事务#id → 直接添加依赖

        Args:
            task_id: 当前事务ID
            content: 事务内容字符串
        """
        task_refs = self._parse_wait_refs(content)

        if not task_refs:
            return

        # 处理 @事务#id 引用
        for ref_task_id in task_refs:
            # 不能依赖自己
            if ref_task_id == task_id:
                continue
            # 检查引用的事务是否存在
            ref_task = self.get_task(ref_task_id)
            if ref_task is None:
                continue
            # 如果引用的事务已经是终态，无需等待
            if ref_task.get("status") == "completed":
                continue
            # 添加依赖
            print(task_id, ref_task_id)
            self.add_dependency(task_id, ref_task_id)

    def get_dependencies(self, task_id: int) -> list[int]:
        """获取事务的依赖（被依赖的事务ID列表）"""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT depends_on_id FROM task_dependencies WHERE task_id = %s",
                (task_id,),
            )
            return [row["depends_on_id"] for row in cursor.fetchall()]

    def get_dependents(self, task_id: int) -> list[int]:
        """获取依赖此事务的事务ID列表"""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT task_id FROM task_dependencies WHERE depends_on_id = %s",
                (task_id,),
            )
            return [row["task_id"] for row in cursor.fetchall()]

    # ==================== 查询 ====================

    def get_by_status(self, status: TaskStatus) -> list[dict[str, Any]]:
        """按状态获取事务"""
        return self.list_tasks(status=status)

    def get_by_message(self, message_id: int) -> list[dict[str, Any]]:
        """按消息ID获取事务"""
        return self.list_tasks(message_id=message_id)

    def get_max_depth(self) -> int | None:
        """
        获取数据库中事务的最大深度

        Returns:
            最大深度，没有事务返回None
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.execute("SELECT MAX(depth) as max_depth FROM tasks")
                row = cursor.fetchone()
                return row["max_depth"] if row and row["max_depth"] is not None else None
        except Exception as e:
            import logging

            logger = logging.getLogger(__name__)
            logger.error(f"获取最大深度失败: {e}")
            return None

    # ==================== 心跳和超时 ====================

    def update_heartbeat(self, task_id: int | None) -> bool:
        """更新心跳"""
        now = datetime.now().isoformat()
        return self.update_tasks([task_id], heartbeat_at=now) > 0

    def get_timed_out_tasks(self, timeout_seconds: int = 300) -> list[dict[str, Any]]:
        """获取超时事务"""
        expired_time = (
            datetime.now() - __import__("datetime").timedelta(seconds=timeout_seconds)
        ).isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'running'
                AND heartbeat_at < %s
                """,
                (expired_time,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_by_process_id(self, process_id: int) -> list[dict[str, Any]]:
        """
        根据进程 ID 获取关联的事务

        Args:
            process_id: 进程 ID

        Returns:
            关联的事务列表
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM tasks
                WHERE process_id = %s
                ORDER BY created_at ASC
                """,
                (process_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_by_message_id(self, message_id: int) -> list[dict[str, Any]]:
        """
        根据消息 ID 获取关联的事务

        Args:
            message_id: 消息 ID

        Returns:
            关联的事务列表
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM tasks
                WHERE message_id = %s
                ORDER BY created_at DESC
                """,
                (message_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_running_tasks_with_heartbeat(self) -> list[dict[str, Any]]:
        """
        获取所有运行中事务及其心跳信息

        Returns:
            事务列表，包含 id, process_id, heartbeat_at, heartbeat_timeout, message_id, title
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, process_id, heartbeat_at, heartbeat_timeout, message_id, title
                FROM tasks WHERE status = 'running'
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_running_tasks(self) -> list[dict[str, Any]]:
        """
        获取所有运行中的事务

        Returns:
            事务列表
        """
        return self.list_tasks(status="running")

    def update_tasks_batch_interrupted(
        self, task_ids: list[int], completed_at: str, error: str = "进程异常中断"
    ) -> int:
        """
        批量更新事务为中断状态

        Args:
            task_ids: 事务ID列表
            completed_at: 完成时间（ISO格式）
            error: 错误信息

        Returns:
            更新的行数
        """
        return self.update_tasks(
            task_ids,
            status="interrupted",
            error=error,
            completed_at=completed_at,
        )

    def update_tasks_batch_failed(
        self, task_ids: list[int], completed_at: str, error: str = "进程心跳超时"
    ) -> int:
        """
        批量更新事务为失败状态

        Args:
            task_ids: 事务ID列表
            completed_at: 完成时间（ISO格式）
            error: 错误信息

        Returns:
            更新的行数
        """
        return self.update_tasks(
            task_ids,
            status="failed",
            error=error,
            completed_at=completed_at,
        )

    def mark_task_completed_if_running(self, task_id: int) -> bool:
        """
        尝试标记事务为已完成（如果状态为running）

        Args:
            task_id: 事务ID

        Returns:
            是否成功更新
        """
        now_iso = datetime.now().isoformat()
        count = self.update_tasks(
            [task_id],
            _where_conditions=["status = 'running'"],
            status="completed",
            completed_at=now_iso,
            heartbeat_at=now_iso,
        )
        if count > 0:
            with self._get_connection() as conn:
                conn.commit()
            return True
        return False

    def get_running_tasks_with_heartbeat_and_details(self) -> list[dict[str, Any]]:
        """
        获取所有运行中事务及其心跳和详细信息

        Returns:
            事务列表，包含 id, process_id, heartbeat_at, heartbeat_timeout, message_id, title, status
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, process_id, heartbeat_at, heartbeat_timeout, message_id, title, status
                FROM tasks WHERE status IN ('running', 'accepting')
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_all_tasks_with_message_id(self, root_only: bool = False) -> list[dict[str, Any]]:
        """
        获取所有有关联消息ID的事务

        Args:
            root_only: 是否只返回根事务（parent_task_id IS NULL）

        Returns:
            事务列表，包含 id, message_id, status, parent_task_id
        """
        with self._get_connection() as conn:
            if root_only:
                cursor = conn.execute(
                    """SELECT id, message_id, status, parent_task_id FROM tasks
                       WHERE message_id IS NOT NULL AND parent_task_id IS NULL"""
                )
            else:
                cursor = conn.execute(
                    """SELECT id, message_id, status, parent_task_id FROM tasks WHERE message_id IS NOT NULL"""
                )
            return [dict(row) for row in cursor.fetchall()]

    def get_tasks_by_message_ids_list(self, message_ids: list[int]) -> list[dict[str, Any]]:
        """
        根据消息ID列表获取事务

        Args:
            message_ids: 消息ID列表

        Returns:
            事务列表，包含 id, status
        """
        if not message_ids:
            return []

        placeholders = ",".join(["%s"] * len(message_ids))
        with self._get_connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT id, status FROM tasks
                WHERE message_id IN ({placeholders})
                """,
                tuple(message_ids),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_tasks_by_message_ids_with_status(self, message_ids: list[int]) -> list[dict[str, Any]]:
        """
        根据消息ID列表获取事务及其状态

        Args:
            message_ids: 消息ID列表

        Returns:
            事务列表，包含 id, status
        """
        if not message_ids:
            return []

        placeholders = ",".join(["%s"] * len(message_ids))
        with self._get_connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT id, status FROM tasks
                WHERE message_id IN ({placeholders})
                """,
                tuple(message_ids),
            )
            return [dict(row) for row in cursor.fetchall()]

    def update_tasks_status_by_message_ids(self, task_ids: list[int], status: str) -> int:
        """
        根据事务ID列表批量更新事务状态

        Args:
            task_ids: 事务ID列表
            status: 新状态

        Returns:
            更新的行数
        """
        return self.update_tasks(task_ids, status=status)

    def get_active_tasks(self) -> list[dict[str, Any]]:
        """
        获取所有运行中的事务

        Returns:
            事务列表，包含 id, title, status, timeout, started_at
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, title, status, timeout, started_at
                FROM tasks
                WHERE status = 'running'
                ORDER BY id
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_tasks_by_message_ids_and_status(
        self, message_ids: list[int], statuses: list[str]
    ) -> list[dict[str, Any]]:
        """
        根据消息ID列表和状态列表获取事务

        Args:
            message_ids: 消息ID列表
            statuses: 状态列表

        Returns:
            事务列表，包含 id, status
        """
        if not message_ids or not statuses:
            return []

        msg_placeholders = ",".join(["%s"] * len(message_ids))
        status_placeholders = ",".join(["%s"] * len(statuses))
        with self._get_connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT id, status FROM tasks
                WHERE message_id IN ({msg_placeholders})
                AND status IN ({status_placeholders})
                """,
                tuple(message_ids + statuses),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_tasks_with_status_by_message_ids(
        self, message_ids: list[int], status: str | None = None
    ) -> list[dict[str, Any]]:
        """
        根据消息ID列表获取事务

        Args:
            message_ids: 消息ID列表
            status: 状态过滤（可选）

        Returns:
            事务列表
        """
        if not message_ids:
            return []

        placeholders = ",".join(["%s"] * len(message_ids))
        if status:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    f"""
                    SELECT id, message_id, status FROM tasks
                    WHERE message_id IN ({placeholders}) AND status = %s
                    """,
                    list(message_ids) + [status],
                )
        else:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    f"""
                    SELECT id, message_id, status FROM tasks
                    WHERE message_id IN ({placeholders})
                    """,
                    tuple(message_ids),
                )
        return [dict(row) for row in cursor.fetchall()]

    def get_tasks_by_ids_basic(self, task_ids: list[int]) -> list[dict[str, Any]]:
        """
        批量获取事务的基本信息

        Args:
            task_ids: 事务ID列表

        Returns:
            事务列表，包含 id, title, status, message_id
        """
        if not task_ids:
            return []

        placeholders = ",".join(["%s"] * len(task_ids))
        with self._get_connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT id, title, status, message_id FROM tasks
                WHERE id IN ({placeholders})
                """,
                tuple(task_ids),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_tasks_by_status_and_message_ids(
        self, status: str, message_ids: list[int]
    ) -> list[dict[str, Any]]:
        """
        获取指定状态和消息ID的事务

        Args:
            status: 事务状态
            message_ids: 消息ID列表

        Returns:
            事务列表
        """
        if not message_ids:
            return []

        placeholders = ",".join(["%s"] * len(message_ids))
        with self._get_connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT id, status FROM tasks
                WHERE status = %s AND message_id IN ({placeholders})
                """,
                [status] + message_ids,
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_unprocessed_task_controls(self) -> list[dict[str, Any]]:
        """
        获取未处理的事务控制项（按created_at排序）

        Returns:
            控制项列表，包含 id, task_id, action, created_at
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, task_id, action, created_at
                FROM task_controls
                WHERE processed = 0
                ORDER BY created_at ASC
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def mark_task_control_processed(self, control_id: int) -> bool:
        """
        标记事务控制为已处理

        Args:
            control_id: 控制ID

        Returns:
            是否成功
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "UPDATE task_controls SET processed = 1 WHERE id = %s",
                (control_id,),
            )
            return cursor.rowcount > 0

    def insert_task_control(self, task_id: int, action: str) -> int | None:
        """
        插入事务控制记录

        Args:
            task_id: 事务ID
            action: 控制动作

        Returns:
            插入的控制记录ID，失败返回None
        """
        try:
            from datetime import datetime

            with self._get_connection() as conn:
                cursor = conn.execute(
                    """INSERT INTO task_controls (task_id, action, created_at, processed)
                       VALUES (%s, %s, %s, 0)
                       RETURNING id""",
                    (task_id, action, datetime.now().isoformat()),
                )
                row = cursor.fetchone()
                conn.commit()
                return row["id"] if row else None
        except Exception as e:
            import logging

            logger = logging.getLogger(__name__)
            logger.error(f"插入事务控制失败: {e}")
            return None

    def get_tasks_by_message_ids_basic(
        self, message_ids: list[int], status: str | None = None
    ) -> list[dict[str, Any]]:
        """
        根据消息ID列表获取事务的基本信息

        Args:
            message_ids: 消息ID列表
            status: 状态过滤（可选）

        Returns:
            事务列表，包含 id, status
        """
        if not message_ids:
            return []

        placeholders = ",".join(["%s"] * len(message_ids))
        if status:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    f"""
                    SELECT id, status FROM tasks
                    WHERE message_id IN ({placeholders}) AND status = %s
                    """,
                    list(message_ids) + [status],
                )
        else:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    f"""
                    SELECT id, status FROM tasks
                    WHERE message_id IN ({placeholders})
                    """,
                    tuple(message_ids),
                )
        return [dict(row) for row in cursor.fetchall()]

    def get_distinct_message_ids_by_status(self, status: str) -> list[int]:
        """
        获取指定状态事务的去重消息ID列表

        Args:
            status: 事务状态

        Returns:
            消息ID列表
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT DISTINCT message_id FROM tasks
                WHERE status = %s AND message_id IS NOT NULL
                """,
                (status,),
            )
            return [row["message_id"] for row in cursor.fetchall() if row["message_id"]]

    # ==================== Git Worktree 支持 ====================

    def set_worktree(self, task_id: int, worktree_path: str, locked: bool = True) -> bool:
        """
        设置事务的 worktree 关联

        Args:
            task_id: 事务 ID
            worktree_path: worktree 路径
            locked: 是否锁定

        Returns:
            是否成功
        """
        locked_int = 1 if locked else 0
        return (
            self.update_tasks(
                [task_id],
                worktree_path=worktree_path,
                worktree_locked=locked_int,
            )
            > 0
        )

    def clear_worktree(self, task_id: int) -> bool:
        """
        清除事务的 worktree 关联

        Args:
            task_id: 事务 ID

        Returns:
            是否成功
        """
        return (
            self.update_tasks(
                [task_id],
                worktree_path=None,
                worktree_locked=0,
            )
            > 0
        )

    def get_by_worktree(self, worktree_path: str) -> list[dict[str, Any]]:
        """
        根据 worktree 路径获取关联的事务

        Args:
            worktree_path: worktree 路径

        Returns:
            关联的事务列表
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM tasks
                WHERE worktree_path = %s
                ORDER BY created_at DESC
                """,
                (worktree_path,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_locked_worktrees(self) -> list[dict[str, Any]]:
        """
        获取所有被锁定的 worktree

        Returns:
            被锁定的 worktree 事务列表
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT DISTINCT worktree_path, id, title, status
                FROM tasks
                WHERE worktree_locked = 1 AND worktree_path IS NOT NULL
                ORDER BY created_at DESC
                """,
            )
            return [dict(row) for row in cursor.fetchall()]

    def release_worktree_lock(self, worktree_path: str) -> bool:
        """
        释放 worktree 锁

        Args:
            worktree_path: worktree 路径

        Returns:
            是否成功
        """
        # 按条件查找关联的 task_ids，再通过 update_tasks 更新
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT id FROM tasks WHERE worktree_path = %s",
                (worktree_path,),
            )
            task_ids = [row["id"] for row in cursor.fetchall()]
        if not task_ids:
            return False
        return self.update_tasks(task_ids, worktree_locked=0) > 0

    def get_free_worktrees(self, all_worktrees: list[str]) -> list[str]:
        """
        获取空闲的 worktree 列表

        Args:
            all_worktrees: 所有 worktree 路径列表

        Returns:
            空闲的 worktree 路径列表
        """
        if not all_worktrees:
            return []

        # 优化：使用单个 SQL 查询获取所有被锁定的 worktree 路径
        # N 次查询 → 1 次查询
        placeholders = ",".join(["%s"] * len(all_worktrees))
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT DISTINCT worktree_path FROM tasks
                WHERE worktree_path IN ({placeholders}) AND worktree_locked = 1
                """,
                all_worktrees,
            )
            locked_paths = {row["worktree_path"] for row in cursor.fetchall()}

        return [w for w in all_worktrees if w not in locked_paths]

    # ==================== Tool Call 日志管理 ====================

    def append_tool_call(self, task_id: int, tool_call: dict[str, Any]) -> bool:
        """
        追加 tool_call 记录到日志

        Args:
            task_id: 事务 ID
            tool_call: tool_call 数据（包含 id, name, arguments）

        Returns:
            是否成功
        """
        task = self.get_task(task_id)
        if not task:
            return False

        # 解析并追加
        try:
            log = (
                json.loads(task.get("tool_calls_log") or "[]") if task.get("tool_calls_log") else []
            )
        except (json.JSONDecodeError, TypeError):
            log = []

        log.append(tool_call)
        return self.update_tasks([task_id], tool_calls_log=json.dumps(log, ensure_ascii=False)) > 0

    def append_tool_result(self, task_id: int, tool_result: dict[str, Any]) -> bool:
        """
        追加 tool_result 记录到日志

        Args:
            task_id: 事务 ID
            tool_result: tool_result 数据（包含 tool_call_id, tool_name, result）

        Returns:
            是否成功
        """
        task = self.get_task(task_id)
        if not task:
            return False

        # 解析并追加
        try:
            log = (
                json.loads(task.get("tool_results_log") or "[]")
                if task.get("tool_results_log")
                else []
            )
        except (json.JSONDecodeError, TypeError):
            log = []

        log.append(tool_result)
        return (
            self.update_tasks([task_id], tool_results_log=json.dumps(log, ensure_ascii=False)) > 0
        )

    def get_tool_logs(self, task_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """
        获取事务的 tool_call 和 tool_result 日志

        Args:
            task_id: 事务 ID

        Returns:
            (tool_calls_log, tool_results_log) 元组
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT tool_calls_log, tool_results_log FROM tasks WHERE id = %s", (task_id,)
            )
            row = cursor.fetchone()
            if not row:
                return [], []

            try:
                tool_calls = json.loads(row["tool_calls_log"]) if row["tool_calls_log"] else []
            except (json.JSONDecodeError, TypeError):
                tool_calls = []

            try:
                tool_results = (
                    json.loads(row["tool_results_log"]) if row["tool_results_log"] else []
                )
            except (json.JSONDecodeError, TypeError):
                tool_results = []

            return tool_calls, tool_results

    # ==================== 子事务管理 ====================
    def get_parent_id(self, task_id: int) -> int:
        task = self.get_task(task_id)
        return 0 if not task else task.get("parent_task_id")

    def get_parent(self, task_id: int) -> list[dict[str, Any]]:
        parent_id = self.get_parent_id(task_id)
        return self.get_task(parent_id) if parent_id > 0 else []

    def get_children(self, parent_id: int) -> list[dict[str, Any]]:
        """
        获取某个事务的所有子事务

        Args:
            parent_id: 父事务 ID

        Returns:
            子事务列表，按优先级和创建时间排序
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM tasks
                WHERE parent_task_id = %s
                ORDER BY priority, created_at ASC
                """,
                (parent_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_children_count(self, parent_id: int) -> int:
        """
        获取某个事务的子事务数量

        Args:
            parent_id: 父事务 ID

        Returns:
            子事务数量
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT COUNT(*) FROM tasks
                WHERE parent_task_id = %s
                """,
                (parent_id,),
            )
            row = cursor.fetchone()
            return row["count"] if row else 0

    def get_subtree(self, root_id: int) -> list[dict[str, Any]]:
        """
        获取整个事务树（某个根节点下的所有事务）

        Args:
            root_id: 事务树根节点 ID

        Returns:
            事务树中所有事务列表，按深度和路径排序
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM tasks
                WHERE root_id = %s
                ORDER BY depth, path
                """,
                (root_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_descendants(
        self, ancestor_id: int, max_depth: int | None = None
    ) -> list[dict[str, Any]]:
        """
        获取某个事务的所有后代事务（递归子事务）

        Args:
            ancestor_id: 祖先事务 ID
            max_depth: 最大深度（相对于祖先的深度），None 表示不限制

        Returns:
            后代事务列表
        """
        # 获取祖先信息（需要在 with 块外部获取，因为 get_task 也使用连接）
        ancestor = self.get_task(ancestor_id)
        if not ancestor:
            return []

        ancestor_depth = ancestor.get("depth", 0)
        ancestor_root_id = ancestor.get("root_id") or ancestor_id

        with self._get_connection() as conn:
            if max_depth is not None:
                target_depth = ancestor_depth + max_depth
                cursor = conn.execute(
                    """
                    SELECT * FROM tasks
                    WHERE root_id = %s AND depth > %s AND depth <= %s AND path LIKE %s
                    ORDER BY depth, path
                    """,
                    (
                        ancestor_root_id,
                        ancestor_depth,
                        target_depth,
                        f"%/{ancestor_id}/%",
                    ),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT * FROM tasks
                    WHERE root_id = %s AND depth > %s AND path LIKE %s
                    ORDER BY depth, path
                    """,
                    (
                        ancestor_root_id,
                        ancestor_depth,
                        f"%/{ancestor_id}/%",
                    ),
                )
            return [dict(row) for row in cursor.fetchall()]

    def get_siblings(self, task_id: int) -> list[dict[str, Any]]:
        """
        获取某个事务的所有兄弟事务（同父的其他子事务）

        Args:
            task_id: 事务 ID

        Returns:
            兄弟事务列表（不包含自身）
        """
        task = self.get_task(task_id)
        if not task or not task.get("parent_task_id"):
            return []

        parent_id = task["parent_task_id"]
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM tasks
                WHERE parent_task_id = %s AND id != %s
                ORDER BY priority, created_at ASC
                """,
                (parent_id, task_id),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_pending_children(self, parent_id: int) -> list[dict[str, Any]]:
        """
        获取某个事务的待执行子事务

        Args:
            parent_id: 父事务 ID

        Returns:
            待执行的子事务列表
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM tasks
                WHERE parent_task_id = %s AND status = 'pending'
                ORDER BY priority, created_at ASC
                """,
                (parent_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_running_children(self, parent_id: int) -> list[dict[str, Any]]:
        """
        获取某个事务正在执行的子事务

        Args:
            parent_id: 父事务 ID

        Returns:
            正在执行的子事务列表
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM tasks
                WHERE parent_task_id = %s AND status = 'running'
                ORDER BY created_at DESC
                """,
                (parent_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_completed_children(self, parent_id: int) -> list[dict[str, Any]]:
        """
        获取某个事务已完成的子事务

        Args:
            parent_id: 父事务 ID

        Returns:
            已完成的子事务列表
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM tasks
                WHERE parent_task_id = %s AND status = 'completed'
                ORDER BY completed_at DESC
                """,
                (parent_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_failed_children(self, parent_id: int) -> list[dict[str, Any]]:
        """
        获取某个事务失败的子事务

        Args:
            parent_id: 父事务 ID

        Returns:
            失败的子事务列表
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM tasks
                WHERE parent_task_id = %s AND status = 'failed'
                ORDER BY completed_at DESC
                """,
                (parent_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def children_all_completed(self, parent_id: int) -> bool:
        """
        检查某个事务的所有子事务是否都已完成

        Args:
            parent_id: 父事务 ID

        Returns:
            True 表示所有子事务都已完成，False 表示还有未完成的子事务
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT COUNT(*) FROM tasks
                WHERE parent_task_id = %s AND status != 'completed'
                """,
                (parent_id,),
            )
            row = cursor.fetchone()
            return (row["count"] if row else 0) == 0

    def get_child_task_ids(self, parent_id: int) -> list[int]:
        """
        获取某个事务的所有子事务 ID 列表

        Args:
            parent_id: 父事务 ID

        Returns:
            子事务 ID 列表
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id FROM tasks
                WHERE parent_task_id = %s
                ORDER BY created_at DESC
                """,
                (parent_id,),
            )
            return [row["id"] for row in cursor.fetchall()]

    # ==================== 进程关联查询 ====================

    def get_max_process_id(self) -> int | None:
        """
        获取事务表中最大的进程ID

        Returns:
            最大的process_id，如果没有则返回None
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT MAX(process_id) FROM tasks
                WHERE process_id IS NOT NULL AND process_id > 0
                """
            )
            result = cursor.fetchone()
            return result["max"] if result and result["max"] is not None else None

    def has_process(self, task_id: int, process_id: int) -> bool:
        """
        判断进程的调用链中是否包含指定事务

        检查 process_id 关联的事务及其祖先链中是否包含 task_id。
        即判断进程 process_id 是否由事务 task_id 或其子事务开启。

        Args:
            task_id: 目标事务 ID
            process_id: 进程 ID

        Returns:
            True 表示进程由该事务或其子事务开启，False 表示无关
        """
        # 查找 process_id 关联的事务
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, parent_task_id FROM tasks
                WHERE process_id = %s
                """,
                (process_id,),
            )
            tasks = cursor.fetchall()

        if not tasks:
            return False

        # 对每个关联的事务，向上遍历祖先链
        for task in tasks:
            current_id = task["id"]
            visited = set()  # 防止循环

            while current_id is not None:
                if current_id in visited:
                    break
                visited.add(current_id)

                # 找到目标事务
                if current_id == task_id:
                    return True

                # 获取父事务 ID
                with self._get_connection() as conn:
                    cursor = conn.execute(
                        "SELECT parent_task_id FROM tasks WHERE id = %s",
                        (current_id,),
                    )
                    row = cursor.fetchone()
                    current_id = row["parent_task_id"] if row else None

        return False

    # ==================== 数据库健康检查 ====================

    def check_integrity(self) -> tuple[bool, str]:
        """
        检查数据库完整性

        Returns:
            (是否健康, 诊断信息)
        """
        try:
            with self._get_connection() as conn:
                # PostgreSQL使用其他方式检查完整性
                cursor = conn.execute("""
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                    LIMIT 1
                """)
                result = cursor.fetchone()
                if result:
                    return True, "PostgreSQL数据库连接正常"
                else:
                    return False, "PostgreSQL数据库访问异常"
        except Exception as e:
            import traceback

            logger.error(
                f"❌ check_integrity() - database.py:1625: Exception - {e}\n{traceback.format_exc()}"
            )
            return False, f"数据库检查失败: {e}"

    def checkpoint_wal(self) -> bool:
        """
        执行 WAL checkpoint，将 WAL 内容合并到主数据库

        Returns:
            是否成功
        """
        try:
            with self._get_connection() as conn:
                # PostgreSQL自动管理WAL checkpoint，无需手动执行
                logger.info("PostgreSQL自动管理WAL")
                return True
        except Exception as e:
            logger.warning(f"WAL checkpoint 执行失败: {e}\n{traceback.format_exc()}")
            return False

    def get_task_by_prompt(self, prompt: str, limit: int = 1) -> list[dict[str, Any]]:
        """
        根据prompt字段查询事务

        Args:
            prompt: 事务提示词
            limit: 返回数量限制（默认返回1个）

        Returns:
            事务列表
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM tasks WHERE prompt = %s ORDER BY created_at DESC LIMIT %s",
                (prompt, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_database_info(self) -> dict[str, Any]:
        """
        获取数据库信息，用于诊断

        Returns:
            数据库信息字典
        """
        info = {}
        try:
            with self._get_connection() as conn:
                # PostgreSQL信息
                cursor = conn.execute("SELECT version()")
                row = cursor.fetchone()
                info["db_version"] = row["version"] if row else "unknown"
                info["journal_mode"] = "WAL (default)"
                info["synchronous"] = "automatic"

                # WAL 文件大小
                wal_path = Path(str(self.db_path) + "-wal")
                if wal_path.exists():
                    info["wal_size_mb"] = wal_path.stat().st_size / (1024 * 1024)
                else:
                    info["wal_size_mb"] = 0

                # 数据库大小
                if self.db_path.exists():
                    info["db_size_mb"] = self.db_path.stat().st_size / (1024 * 1024)
                else:
                    info["db_size_mb"] = 0

                # 事务数量
                cursor = conn.execute("SELECT COUNT(*) as count FROM tasks")
                row = cursor.fetchone()
                info["task_count"] = row["count"] if row else 0

        except Exception as e:
            import traceback

            logger.error(
                f"❌ get_database_info() - database.py:1683: Exception - {e}\n{traceback.format_exc()}"
            )
            info["error"] = str(e)

        return info

    def mark_archived(self, task_id: int) -> bool:
        """
        归档事务（标记单个事务）

        Args:
            task_id: 事务ID

        Returns:
            是否成功
        """
        return (
            self.update_tasks(
                [task_id],
                is_archived=1,
                archived_at=datetime.now().isoformat(),
            )
            > 0
        )

    def unarchive_task(self, task_id: int) -> bool:
        """
        取消归档事务（单个事务）

        Args:
            task_id: 事务ID

        Returns:
            是否成功
        """
        return (
            self.update_tasks(
                [task_id],
                is_archived=0,
                archived_at=None,
            )
            > 0
        )

    def archive_task_tree(self, task_id: int) -> int:
        """
        归档事务树（事务及其所有子事务）

        🔧 Task#1346：批量优化，消除N+1查询
        旧实现递归调用 get_children + mark_archived，N个子事务产生 2N次连接池操作 + N次广播。
        新实现用 get_descendants 一次性获取所有后代ID，然后一条SQL批量UPDATE。

        Args:
            task_id: 事务ID

        Returns:
            归档的事务数量
        """
        # 一次性获取所有后代（含自身）
        all_ids = [task_id]
        descendants = self.get_descendants(task_id)
        all_ids.extend(d["id"] for d in descendants)

        now = datetime.now().isoformat()

        # 批量UPDATE：一条SQL更新所有事务
        with self._get_connection() as conn:
            placeholders = ",".join(["%s"] * len(all_ids))
            cursor = conn.execute(
                f"UPDATE tasks SET is_archived = 1, archived_at = %s WHERE id IN ({placeholders}) AND is_archived = 0",
                [now] + all_ids,
            )
            archived_count = cursor.rowcount

        # 只广播一次（路由层会处理广播，这里不再逐个广播）
        return archived_count

    def unarchive_task_tree(self, task_id: int) -> int:
        """
        取消归档事务树（事务及其所有子事务）

        🔧 Task#1346：批量优化，消除N+1查询

        Args:
            task_id: 事务ID

        Returns:
            取消归档的事务数量
        """
        # 一次性获取所有后代（含自身）
        all_ids = [task_id]
        descendants = self.get_descendants(task_id)
        all_ids.extend(d["id"] for d in descendants)

        # 批量UPDATE：一条SQL更新所有事务
        with self._get_connection() as conn:
            placeholders = ",".join(["%s"] * len(all_ids))
            cursor = conn.execute(
                f"UPDATE tasks SET is_archived = 0, archived_at = NULL WHERE id IN ({placeholders}) AND is_archived = 1",
                all_ids,
            )
            unarchived_count = cursor.rowcount

        return unarchived_count

    def get_root_tasks_by_message(self, message_id: int) -> list[dict[str, Any]]:
        """
        获取消息关联的根事务列表

        Args:
            message_id: 消息ID

        Returns:
            根事务列表（parent_task_id IS NULL的事务）
        """
        return self.list_tasks(message_id=message_id, root_only=True)

    # ==================== Info 字段管理 ====================

    def add_task_info(self, task_id: int, info_content: str, dedup: bool = False) -> bool:
        """
        向事务添加一条info信息（追加到末尾，默认未读）

        使用 SELECT ... FOR UPDATE 在单个数据库事务中原子地执行 read-modify-write，
        防止多进程并发时的 Lost Update 问题。

        Args:
            task_id: 事务ID
            info_content: 信息内容
            dedup: 是否去重（为True时，如果最后一条未读info内容相同则不重复添加）

        Returns:
            是否成功
        """
        from peng.task.info import parse_info_to_list_with_status, serialize_info_list

        with self._get_connection() as conn:
            # SELECT ... FOR UPDATE 锁定行，防止并发修改
            cursor = conn.execute(
                "SELECT info FROM tasks WHERE id = %s FOR UPDATE", (task_id,)
            )
            row = cursor.fetchone()
            if not row:
                return False

            current_info = row.get("info", "[]") or "[]"
            info_list = parse_info_to_list_with_status(current_info)

            # 去重：如果最后一条未读info内容完全相同，则不重复添加
            if dedup:
                for item in reversed(info_list):
                    if not item["read"]:
                        if item["content"] == info_content:
                            logger.debug(f"[DB] add_task_info: 跳过重复info (task_id={task_id})")
                            return True  # 内容相同，跳过
                        break  # 只检查最后一条未读的

            info_list.append({"content": info_content, "read": False})

            # 限制info列表最大数量，避免无限累积（保留最新的50条）
            MAX_INFO_ITEMS = 50
            if len(info_list) > MAX_INFO_ITEMS:
                # 优先保留未读项，然后保留最新的已读项
                unread = [item for item in info_list if not item["read"]]
                read_items = [item for item in info_list if item["read"]]
                if len(unread) >= MAX_INFO_ITEMS:
                    info_list = unread[-MAX_INFO_ITEMS:]
                else:
                    info_list = read_items[-(MAX_INFO_ITEMS - len(unread)) :] + unread
                logger.debug(
                    f"[DB] add_task_info: info列表超过{MAX_INFO_ITEMS}条，已裁剪至最新条目 (task_id={task_id})"
                )

            new_value = serialize_info_list(info_list)
            conn.execute(
                "UPDATE tasks SET info = %s WHERE id = %s", (new_value, task_id)
            )

        # 通知 WebUI
        self._notify_webui("updated", task_id, {"info": new_value})
        return True

    def get_task_info(self, task_id: int, mode: str = "all") -> list[dict] | None:
        """
        获取事务的info列表

        Args:
            task_id: 事务ID
            mode: 获取模式
                - "all": 返回全部info项
                - "read": 仅返回已读info项
                - "unread": 仅返回未读info项
                - 数字字符串(如"1","2"): 返回指定序号的info项（1-based）

        Returns:
            info项列表（每项为 {"content": str, "read": bool}），事务不存在返回 None
        """
        from peng.task.info import parse_info_to_list_with_status

        task = self.get_task(task_id)
        if not task:
            return None

        current_info = task.get("info", "[]") or "[]"
        info_list = parse_info_to_list_with_status(current_info)

        if mode == "all":
            return info_list
        elif mode == "read":
            return [item for item in info_list if item["read"]]
        elif mode == "unread":
            return [item for item in info_list if not item["read"]]
        else:
            # 尝试解析为序号
            try:
                index = int(mode)
                if 1 <= index <= len(info_list):
                    return [info_list[index - 1]]
                return []
            except ValueError:
                return info_list

    def clear_task_info(self, task_id: int) -> bool:
        """
        清空事务的所有info信息

        Args:
            task_id: 事务ID

        Returns:
            是否成功
        """
        return self.update_task(task_id, info="[]")

    def set_task_info_status(self, task_id: int, mode: str = "all", read: bool = True) -> bool:
        """
        设置事务info项的已读/未读状态

        使用 SELECT ... FOR UPDATE 在单个数据库事务中原子地执行 read-modify-write，
        防止多进程并发时的 Lost Update 问题。

        Args:
            task_id: 事务ID
            mode: 设置范围
                - "all": 设置全部info项
                - 数字字符串(如"1","2"): 设置指定序号的info项（1-based）
            read: True=设为已读，False=设为未读

        Returns:
            是否成功
        """
        from peng.task.info import parse_info_to_list_with_status, serialize_info_list

        with self._get_connection() as conn:
            # SELECT ... FOR UPDATE 锁定行，防止并发修改
            cursor = conn.execute(
                "SELECT info FROM tasks WHERE id = %s FOR UPDATE", (task_id,)
            )
            row = cursor.fetchone()
            if not row:
                return False

            current_info = row.get("info", "[]") or "[]"
            info_list = parse_info_to_list_with_status(current_info)
            if not info_list:
                return True  # 没有info项，无需更新

            if mode == "all":
                for item in info_list:
                    item["read"] = read
            else:
                # 尝试解析为序号
                try:
                    index = int(mode)
                    if 1 <= index <= len(info_list):
                        info_list[index - 1]["read"] = read
                    else:
                        return False
                except ValueError:
                    return False

            new_value = serialize_info_list(info_list)
            conn.execute(
                "UPDATE tasks SET info = %s WHERE id = %s", (new_value, task_id)
            )

        # 通知 WebUI
        self._notify_webui("updated", task_id, {"info": new_value})
        return True

    def get_tree_text(self, task_id: int, content_max_len: int = 80, highlight: bool = True) -> str:
        """
        获取事务树的文本表示（用于统一显示事务树）

        递归构建事务树的文本，包含缩进、状态、内容等信息。

        Args:
            task_id: 事务ID（树的根节点）
            content_max_len: 内容最大显示长度，0表示不截断（默认80）
            highlight: 是否使用ANSI颜色高亮（默认True）

        Returns:
            事务树的文本字符串
        """
        # 加载颜色配置（仅在需要高亮时）
        colors = {}
        if highlight:
            from peng.common.colors import ANSI_COLORS

            colors = ANSI_COLORS
        lines: list[str] = []
        self._build_tree_text(task_id, 0, True, content_max_len, lines, colors)
        return "\n".join(lines)

    def _build_tree_text(
        self,
        task_id: int,
        indent: int,
        is_last: bool,
        content_max_len: int,
        lines: list[str],
        colors: dict[str, str] | None = None,
    ) -> None:
        """
        递归构建事务树文本（内部辅助方法）

        Args:
            task_id: 当前事务ID
            indent: 缩进层级
            is_last: 是否为最后一个子节点
            content_max_len: 内容最大显示长度
            lines: 输出行列表（累积）
            colors: 颜色配置字典（为空则不使用高亮）
        """
        task = self.get_task(task_id)
        if not task:
            return

        # 颜色辅助
        R = colors.get("reset", "") if colors else ""

        def c(color_key: str) -> str:
            return colors.get(color_key, "") if colors else ""

        prefix = "  " * indent
        if indent > 0:
            prefix += "└─ " if is_last else "├─ "

        status = task.get("status", "unknown")
        content = task.get("content", "") or ""
        if content_max_len > 0 and content:
            content = content[:content_max_len]

        # 构建显示行：使用"根事务#id"或"子事务#id"格式
        if task.get("parent_task_id"):
            task_label = f"{c('green')}子事务{R}#{task_id}"
        else:
            message_id = task.get("message_id", 0)
            task_label = f"{c('yellow')}根事务{R}#{task_id} {c('bright_blue')}消息{R}#{message_id} {c('bright_cyan')}{'[已归档]' if task.get('is_archived') else ''}{R}"

        # 状态格式化（带颜色）
        status_names = {
            "pending": ("待执行", "bright_white"),
            "running": ("执行中", "blue"),
            "completed": ("已完成", "bright_green"),
            "failed": ("已失败", "red"),
            "cancelled": ("已取消", "cyan"),
            "paused": ("已暂停", "bright_cyan"),
            "interrupted": ("已中断", "bright_red"),
        }
        name, color_key = status_names.get(status, (status, ""))
        status_text = f"{c(color_key)}{name}{R}" if color_key else name

        # 上下文长度（快速路径：直接解析 JSON 元数据）
        context_info = ""
        context = task.get("context")
        if context:
            meta = _fast_context_meta(context)
            tokens = meta.get("prompt_tokens", 0)
            if tokens > 2000:
                ctx_color = c(get_context_color(tokens, task.get("provider", "")))
                context_info = f" [{ctx_color}上下文{tokens // 1000}K{R}]"
            # 费用（绿→橙渐变着色）
            cost = meta.get("total_cost", 0.0)
            if cost > 0:
                cost_color = _get_cost_ansi(cost)
                context_info += f" [{cost_color}费用{cost:.2f}元{R}]"

        line = f"{prefix}{task_label} [{status_text}]{context_info} {content}"
        lines.append(line)

        # 递归显示子事务
        children = self.get_children(task_id)
        for i, child in enumerate(children):
            child_id = child["id"]
            is_last_child = i == len(children) - 1
            self._build_tree_text(
                child_id, indent + 1, is_last_child, content_max_len, lines, colors
            )

    def build_tree_text_from_batch(
        self,
        root_id: int,
        subtree_nodes: list[dict[str, Any]],
        content_max_len: int = 80,
        highlight: bool = True,
    ) -> str:
        """
        从预加载的批量数据构建事务树文本（高性能版本）。

        与 get_tree_text 功能相同，但使用预先批量查询的子树数据，
        避免递归调用 get_task + get_children 产生的 N 次数据库查询。

        Args:
            root_id: 树的根事务ID
            subtree_nodes: 预加载的子树节点列表（含根节点自身），
                           每个节点是完整的 task dict（含 context）
            content_max_len: 内容最大显示长度，0表示不截断
            highlight: 是否使用ANSI颜色高亮

        Returns:
            事务树的文本字符串
        """
        if not subtree_nodes:
            return ""

        # 构建 id → task 映射和 id → children 映射
        task_map: dict[int, dict[str, Any]] = {}
        children_map: dict[int, list[dict[str, Any]]] = {}
        for node in subtree_nodes:
            nid = node["id"]
            task_map[nid] = node
            parent = node.get("parent_task_id")
            if parent and parent != 0:
                if parent not in children_map:
                    children_map[parent] = []
                children_map[parent].append(node)

        # 对 children 按 priority, created_at 排序（与 get_children 一致）
        for parent_id in children_map:
            children_map[parent_id].sort(
                key=lambda t: (t.get("priority", 0), t.get("created_at", ""))
            )

        # 加载颜色
        colors = {}
        if highlight:
            from peng.common.colors import ANSI_COLORS

            colors = ANSI_COLORS

        lines: list[str] = []
        self._build_tree_text_from_map(
            root_id, 0, True, content_max_len, lines, colors, task_map, children_map
        )
        return "\n".join(lines)

    def _build_tree_text_from_map(
        self,
        task_id: int,
        indent: int,
        is_last: bool,
        content_max_len: int,
        lines: list[str],
        colors: dict[str, str] | None,
        task_map: dict[int, dict[str, Any]],
        children_map: dict[int, list[dict[str, Any]]],
    ) -> None:
        """递归构建事务树文本（从预加载的映射数据，无数据库查询）"""
        task = task_map.get(task_id)
        if not task:
            return

        R = colors.get("reset", "") if colors else ""

        def c(color_key: str) -> str:
            return colors.get(color_key, "") if colors else ""

        prefix = "  " * indent
        if indent > 0:
            prefix += "└─ " if is_last else "├─ "

        status = task.get("status", "unknown")
        content = task.get("content", "") or ""
        if content_max_len > 0 and content:
            content = content[:content_max_len]

        if task.get("parent_task_id"):
            task_label = f"{c('green')}子事务{R}#{task_id}"
        else:
            message_id = task.get("message_id", 0)
            task_label = f"{c('yellow')}根事务{R}#{task_id} {c('bright_blue')}消息{R}#{message_id} {c('bright_cyan')}{'[已归档]' if task.get('is_archived') else ''}{R}"

        status_names = {
            "pending": ("待执行", "bright_white"),
            "running": ("执行中", "blue"),
            "completed": ("已完成", "bright_green"),
            "failed": ("已失败", "red"),
            "cancelled": ("已取消", "cyan"),
            "paused": ("已暂停", "bright_cyan"),
            "interrupted": ("已中断", "bright_red"),
        }
        name, color_key = status_names.get(status, (status, ""))
        status_text = f"{c(color_key)}{name}{R}" if color_key else name

        context_info = ""
        context = task.get("context")
        if context:
            # 快速路径：直接解析 JSON 元数据，避免创建 MessageHistory 对象
            meta = _fast_context_meta(context)
            tokens = meta.get("prompt_tokens", 0)
            if tokens > 2000:
                ctx_color = c(get_context_color(tokens, task.get("provider", "")))
                context_info = f" [{ctx_color}上下文{tokens // 1000}K{R}]"
            cost = meta.get("total_cost", 0.0)
            if cost > 0:
                cost_color = _get_cost_ansi(cost)
                context_info += f" [{cost_color}费用{cost:.2f}元{R}]"

        line = f"{prefix}{task_label} [{status_text}]{context_info} {content}"
        lines.append(line)

        children = children_map.get(task_id, [])
        for i, child in enumerate(children):
            is_last_child = i == len(children) - 1
            self._build_tree_text_from_map(
                child["id"],
                indent + 1,
                is_last_child,
                content_max_len,
                lines,
                colors,
                task_map,
                children_map,
            )

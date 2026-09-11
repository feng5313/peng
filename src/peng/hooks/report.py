"""
内置钩子：简单消息回复检查

简单事务如果已经回复用户，则直接结束大模型调用。
"""

import logging

from peng.hooks.types import BaseHook, HookPattern, HookPatterns
from peng.tools.base import REPORT_SUCCESS_SUMMARY

logger = logging.getLogger(__name__)


class ReportHook(BaseHook):
    """消息回复检查钩子"""

    hook_name = "report"
    priority = 10

    hook_patterns = HookPatterns(
        before=[
            # 在流程完成之前检查
            HookPattern(category="flow", pattern=r"^complete$"),
        ],
        after=[
            # 在消息工具调用之后检查
            HookPattern(category="tools", pattern=r"^report$"),
        ],
    )

    def hook_action(
        self, message_id: int, task_id: int, category: str, name: str, metadata: dict
    ) -> tuple[str, str | None]:
        """检查是否工作汇报

        Args:
            message_id: 消息ID
            task_id: 事务ID
            category: 分类（tools/hooks/skills/flow）
            name: 具体名称（工具名/钩子名/技能名/流程名）
            metadata: 元数据字典
                - "history": History（对话历史）
                - "flow": FlowMetadata（流程元数据）

        Returns:
            HookActionType: ("block", reason) 或 ("continue", None)
        """
        try:
            task = metadata.get("task")
            progress = task.get("progress", 0) or 0
            history = metadata.get("history")

            # === tools:report after-hook ===
            # report工具成功后立即强制结束对话，无论history大小
            # 修复：原代码 history.size() > 16 阈值导致短事务（如定时监控任务）
            #       report成功后无法结束，一直保持running状态
            if category == "tools" and name == "report" and history:
                messages = getattr(history, "_messages", [])
                # after-hook触发时，最后一条消息就是刚添加的report工具结果
                if messages:
                    last_msg = messages[-1]
                    if last_msg.get("name") == "report" and "content" in last_msg:
                        content: str = last_msg["content"]
                        # 🔧 修复无限循环：完成标记必须与 tools/task.py ReportTool
                        # 成功时的 summary 精确一致（历史bug：此处旧代码尾带空格
                        # "…成功。 "，而 task.py 的 summary 无尾空格，导致
                        # after-hook 永远匹配失败→report后事务不结束→无限循环）
                        finish_output = self._REPORT_SUCCESS_MARKER
                        done = finish_output in content
                        if done:
                            # 检查是否有未读info，如果有则不结束，继续迭代
                            unread_items = self._get_unread_info(task_id)
                            if unread_items:
                                count = len(unread_items)
                                history.add_user(f"🪝⚠️ 还有 {count} 条未读信息待处理。")
                                logger.info(
                                    f"[report] 事务#{task_id} report成功但检测到{count}条未读info，不结束"
                                )
                                return "continue", None
                            return "block", "🪝✅ 系统检测到工作汇报成功，强制结束事务处理。"

            # === flow:complete before-hook ===
            # 仅在history足够大（>16条）时强制要求使用report工具
            # 短对话（简单问答）无需强制report
            if category == "flow" and name == "complete" and history and history.size() > 16:
                messages = getattr(history, "_messages", [])
                c = 0
                for msg in reversed(messages):  # 从后往前找report工具调用
                    c += 1
                    if c > 3:  # 只找3轮迭代
                        break
                    if (
                        "tool_call_id" in msg
                        and "name" in msg
                        and msg["name"] == "report"
                        and "content" in msg
                        and progress == 100
                    ):
                        # 已汇报，检查是否有未读info
                        unread_items = self._get_unread_info(task_id)
                        if unread_items:
                            count = len(unread_items)
                            hint = f"🪝⚠️ 还有 {count} 条未读信息待处理。"
                            return "block", hint
                        # 无未读info，检查@endless标记
                        task_content = task.get("content", "") if task else ""
                        if "@endless" in task_content:
                            return (
                                "block",
                                "🪝 checklist检查清单是否全部完成？ 是否还有其他没完成的工作？ 你确定已达到完美状态，完全无需迭代测试、分析、优化吗？",
                            )
                        return "continue", None  # 已汇报，可以正常结束
                # 未在最近3条消息中找到report调用
                # 检查是否之前已成功report过，且满足跳过条件
                if progress == 100 and self._can_skip_report(messages, task):
                    logger.info(
                        f"[report] 事务#{task_id} 已成功report过且满足跳过条件，不再强制report"
                    )
                    return "continue", None

                # 阻止结束并提示使用report
                hint = f"🪝 checklist检查清单是否全部完成？ 是否还有其他没完成的工作？ 如果本事务的所有需求都已达成且验证，应该更新checklist，设置progress为100，然后（重新）用report工作汇报工具汇报总结工作来结束本事务处理。"
                return "block", hint

            # 其他情况继续执行
            return "continue", None

        except Exception as e:
            logger.error(f"[report] 检查失败: {e}", exc_info=True)
            return "continue", None  # 出错不阻止

    # report 工具成功时返回的 summary 标记（单一事实源：tools/base.py）
    _REPORT_SUCCESS_MARKER = REPORT_SUCCESS_SUMMARY

    @classmethod
    def _can_skip_report(cls, messages: list[dict], task: dict | None) -> bool:
        """检查是否可以跳过强制report

        前提：已经成功report过。然后满足以下任一条件即可跳过：
        - report后的user信息中不包含"报告"/"report"（即没有新的工作需求要求汇报）
        - report后的user信息中包含"不要报告"/"不要report"/"no report"
        - task content或info中包含"#no_report"

        注意：block reason 通过 add_assistant 注入（非 user 消息），
        所以不会干扰 post_report user 消息的收集。

        Args:
            messages: 对话历史消息列表（history._messages）
            task: 事务信息字典（包含 content, info 等字段）

        Returns:
            是否可以跳过强制report
        """
        if not messages:
            return False

        # 0. s-qwen3.8模型事务必须强制report（提交15c8ee91的设计意图：该模型在
        #    长思考后退化为空响应直接结束、跳过工作汇报），除非有 #no_report 显式标记。
        #    注意：必须放在条件1（report后user消息无关键词→跳过）之前，
        #    否则该模型事务会先命中条件1提前return True，强制report永远不生效。
        if task:
            if "#no_report" in task.get("content", ""):
                return True
            if "#no_report" in task.get("info", ""):
                return True
            if "s-qwen3.8" in task.get("provider", ""):
                return False

        # 1. 查找最后一次成功的report tool结果
        last_report_idx = None
        for i, msg in enumerate(messages):
            if (
                msg.get("tool_call_id")
                and msg.get("name") == "report"
                and cls._REPORT_SUCCESS_MARKER in msg.get("content", "")
            ):
                last_report_idx = i

        if last_report_idx is None:
            return False  # 从未成功report，不能跳过

        # 2. 收集report之后的所有user消息文本（block hint 是 assistant 消息，不会被收集）
        post_report_user_texts = []
        for msg in messages[last_report_idx + 1 :]:
            if msg.get("role") == "user":
                post_report_user_texts.append(msg.get("content", ""))

        combined = "\n".join(post_report_user_texts).lower()

        # 3. 条件1：report后没有user消息，或user消息中不包含"报告"/"report"
        if "报告" not in combined and "report" not in combined:
            return True

        # 4. 条件2：report后的user消息中包含明确的不需要report指令
        skip_keywords = ["不要报告", "不要report", "no report"]
        if any(kw in combined for kw in skip_keywords):
            return True

        # 5. 条件3：task content或info中包含"#no_report"
        #    task dict 已包含 info 字段（JSON字符串），直接检查即可
        #    （s-qwen3.8检查已在函数开头处理）
        if task:
            if "#no_report" in task.get("content", ""):
                return True
            if "#no_report" in task.get("info", ""):
                return True

        return False

    @staticmethod
    def _get_unread_info(task_id: int) -> list[dict]:
        """从数据库获取事务的未读info列表

        直接查询数据库以确保获取最新状态，而非依赖可能过期的缓存task dict。

        Args:
            task_id: 事务ID

        Returns:
            未读info项列表，每项为 {"content": str, "read": bool}
        """
        try:
            from peng.task.database import TaskDatabase

            db = TaskDatabase()
            return db.get_task_info(task_id, mode="unread") or []
        except Exception as e:
            logger.debug(f"[report] 获取未读info失败: {e}")
            return []

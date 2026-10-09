"""事务#1600回归测试：空响应误结束事务 + report跳过策略失效

根因回顾：
1. tool_engine.py 空响应 `<=2` 分支缺少 `should_complete = False`，
   导致s-qwen3.8空响应（长思考退化、content为空、无tool_calls）时
   事务被直接标记"执行成功"结束，"再次尝试"从未生效。
2. hooks/report.py `_can_skip_report` 中 s-qwen3.8 强制report检查
   位于条件1（report后user消息无"报告"/"report"关键词→return True）之后，
   几乎永远执行不到，15c8ee91 的"强制s-qwen3.8事务必须report"设计失效。
"""

import ast

import pytest

from peng.hooks.report import ReportHook


def _mk_report_success_msg(idx: int = 100) -> dict:
    """构造一条成功的report工具结果消息"""
    return {
        "role": "tool",
        "tool_call_id": f"call_{idx}",
        "name": "report",
        "content": "✅  report工具调用成功。 xxx",
    }


class TestCanSkipReportSQwen38:
    """_can_skip_report: s-qwen3.8强制report检查必须前置生效"""

    def test_sqwen38_skip_condition1_hits_first(self):
        """复现#1599：s-qwen3.8 + report后user消息无关键词 → 必须返回False（强制report）

        修复前：条件1在line 173先return True，s-qwen3.8检查（旧line 188）永远不生效，
        事务被错误跳过report直接结束。
        """
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "开始干活"},
            _mk_report_success_msg(),
            {"role": "assistant", "content": "已完成上一阶段"},
        ]
        task = {"content": "调查 鼠标位置 更新到GPU的频率", "provider": "s-qwen3.8", "info": ""}
        assert ReportHook._can_skip_report(messages, task) is False

    def test_sqwen38_never_reported_cannot_skip(self):
        """s-qwen3.8 从未report → False"""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "干活"},
        ]
        task = {"content": "需求", "provider": "s-qwen3.8", "info": ""}
        assert ReportHook._can_skip_report(messages, task) is False

    def test_sqwen38_no_report_marker_still_skips(self):
        """s-qwen3.8 + task content含 #no_report 显式标记 → 仍可跳过（显式标记最高优先）"""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "干活"},
            _mk_report_success_msg(),
        ]
        task = {"content": "需求 #no_report", "provider": "s-qwen3.8", "info": ""}
        assert ReportHook._can_skip_report(messages, task) is True

    def test_sqwen38_info_marker_skips(self):
        """s-qwen3.8 + info含 #no_report → 可跳过"""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "干活"},
            _mk_report_success_msg(),
        ]
        task = {"content": "需求", "provider": "s-qwen3.8", "info": '{"note": "#no_report"}'}
        assert ReportHook._can_skip_report(messages, task) is True

    def test_other_provider_normal_skip_kept(self):
        """非s-qwen3.8 provider：report后user消息无关键词 → 正常跳过（原逻辑保留）"""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "干活"},
            _mk_report_success_msg(),
            {"role": "assistant", "content": "done"},
        ]
        task = {"content": "需求", "provider": "z-glm5.2", "info": ""}
        assert ReportHook._can_skip_report(messages, task) is True

    def test_other_provider_report_keyword_blocks(self):
        """非s-qwen3.8 provider：report后user消息含"报告" → 不跳过（要求重新report）"""
        messages = [
            {"role": "system", "content": "sys"},
            _mk_report_success_msg(),
            {"role": "user", "content": "请汇报报告一下结果"},
        ]
        task = {"content": "需求", "provider": "z-glm5.2", "info": ""}
        assert ReportHook._can_skip_report(messages, task) is False

    def test_never_reported_cannot_skip_any_provider(self):
        """任何provider从未report → 不跳过"""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "干活"},
        ]
        task = {"content": "需求", "provider": "z-glm5.2", "info": ""}
        assert ReportHook._can_skip_report(messages, task) is False

    def test_empty_messages_returns_false(self):
        """空消息列表 → False"""
        task = {"content": "需求", "provider": "s-qwen3.8", "info": ""}
        assert ReportHook._can_skip_report([], task) is False


class TestToolEngineEmptyResponseSource:
    """tool_engine.py 空响应 `<=2` 分支必须设置 should_complete = False（源码级校验）

    完整执行ToolExecutionEngine需要模拟整个LLM流，成本过高；
    此处通过AST静态校验保证该分支不再缺失 should_complete = False
    （根因：提交51026ce0拆分分支时遗漏该语句）。
    """

    def _load_branch(self, source: str):
        tree = ast.parse(source)
        # 定位 execute 方法内的空响应 if 分支
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                # 匹配 if self._consecutive_empty <= 2:
                test = node.test
                if (
                    isinstance(test, ast.Compare)
                    and isinstance(test.left, ast.Attribute)
                    and test.left.attr == "_consecutive_empty"
                    and any(
                        isinstance(c, ast.Constant) and c.value == 2 for c in test.comparators
                    )
                ):
                    target = node
                    break
        assert target is not None, "未找到空响应 <=2 分支"
        return target

    def test_empty_retry_branch_sets_should_complete_false(self):
        """空响应 <=2 分支必须包含 should_complete = False 赋值"""
        source = open("src/peng/llm/tool_engine.py", encoding="utf-8").read()
        branch = self._load_branch(source)
        found = any(
            isinstance(stmt, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "should_complete" for t in stmt.targets
            )
            and isinstance(stmt.value, ast.Constant)
            and stmt.value.value is False
            for stmt in ast.walk(branch)
        )
        assert found, "空响应 <=2 分支缺少 should_complete = False（事务#1600根因#1回归）"

    def test_empty_retry_branch_writes_system_output(self):
        """空响应 <=2 分支应写入系统提示（用户可见）"""
        source = open("src/peng/llm/tool_engine.py", encoding="utf-8").read()
        branch = self._load_branch(source)
        found = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "write_output"
            for node in ast.walk(branch)
        )
        assert found, "空响应 <=2 分支缺少 write_output 系统提示"

    def test_compact_branch_still_sets_should_complete_false(self):
        """compact分支（原有逻辑）仍设置 should_complete = False，未被本次修改破坏"""

        source = open("src/peng/llm/tool_engine.py", encoding="utf-8").read()
        tree = ast.parse(source)
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            # elif 分支的 test 是 BoolOp(and)，需在其中找 _consecutive_empty <= 5 比较
            compares = [
                n
                for n in ast.walk(node.test)
                if isinstance(n, ast.Compare)
                and isinstance(n.left, ast.Attribute)
                and n.left.attr == "_consecutive_empty"
            ]
            has_five = any(
                isinstance(c, ast.Constant) and c.value == 5
                for cmp in compares
                for c in cmp.comparators
            )
            if not has_five:
                continue
            if any(
                isinstance(stmt, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "should_complete" for t in stmt.targets
                )
                and isinstance(stmt.value, ast.Constant)
                and stmt.value.value is False
                for stmt in ast.walk(node)
            ):
                found = True
        assert found, "compact分支缺少 should_complete = False"


# ==================== 事务#1711回归：report成功标记尾空格漂移导致无限循环 ====================

import pytest  # noqa: E402

from peng.tools.base import REPORT_SUCCESS_SUMMARY  # noqa: E402


class _FakeHistory:
    """最小history桩：仅提供钩子用到的 _messages"""

    def __init__(self, messages):
        self._messages = messages


class TestReportSuccessMarkerConsistency:
    """report成功标记必须单一事实源，钩子能精确匹配（否则事务无限循环）"""

    def test_task_summary_uses_shared_constant(self):
        """tools/task.py 的 report 成功 summary 必须引用共享常量，禁止内联硬编码

        历史bug：07-18 提交把 summary 尾空格去掉，而钩子标记尾带空格，
        after-hook 永远匹配失败 → task#2（timer事务总管）report后不结束无限循环。
        """
        from pathlib import Path

        src = (
            Path(__file__).parent.parent.parent / "src" / "peng" / "tools" / "task.py"
        ).read_text(encoding="utf-8")
        assert '"✅  report工具调用成功。"' not in src, (
            "task.py 中禁止内联硬编码 report 成功标记，必须使用 tools.base.REPORT_SUCCESS_SUMMARY"
        )
        assert "REPORT_SUCCESS_SUMMARY" in src

    def test_hook_matches_exact_task_summary(self):
        """钩子标记必须与 task.py 实际返回的 summary 精确匹配（含 históric尾空格场景）"""
        hook = ReportHook()
        assert REPORT_SUCCESS_SUMMARY in hook._REPORT_SUCCESS_MARKER or (
            hook._REPORT_SUCCESS_MARKER == REPORT_SUCCESS_SUMMARY
        ), "钩子标记与 task.py summary 常量不一致"

    def test_hook_action_blocks_on_report_success(self, monkeypatch):
        """report成功（content=共享常量）→ after-hook 必须返回 block（强制结束事务）"""
        hook = ReportHook()
        # 无未读info（staticmethod经类patch后实例调用会绑定self）
        monkeypatch.setattr(ReportHook, "_get_unread_info", staticmethod(lambda tid: []))

        history = _FakeHistory(
            [
                {"role": "user", "content": "干活"},
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "name": "report",
                    "content": REPORT_SUCCESS_SUMMARY,
                },
            ]
        )
        action, reason = hook.hook_action(
            message_id=1,
            task_id=2,
            category="tools",
            name="report",
            metadata={"task": {"id": 2}, "history": history},
        )
        assert action == "block", f"report成功后必须结束事务，实际返回 {action}"
        assert reason and "结束" in reason

    def test_hook_action_continues_with_unread_info(self, monkeypatch):
        """report成功但有未读info → 不结束，提示继续"""
        hook = ReportHook()
        monkeypatch.setattr(ReportHook, "_get_unread_info", staticmethod(lambda tid: [{"id": 1}]))

        history = _FakeHistory(
            [
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "name": "report",
                    "content": REPORT_SUCCESS_SUMMARY,
                }
            ]
        )
        action, reason = hook.hook_action(
            message_id=1,
            task_id=2,
            category="tools",
            name="report",
            metadata={"task": {"id": 2}, "history": history},
        )
        assert action == "continue"

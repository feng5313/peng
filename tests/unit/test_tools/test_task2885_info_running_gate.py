"""
事务#2885：info 工具运行状态门禁测试

需求：严格禁止大模型用 info 工具把信息发给**不在运行状态**的事务
（手动 CLI/WebUI 不受限制，本测试只针对工具层）。

覆盖：
1. InfoTool：running/accepting 放行；paused/completed/cancelled/pending 拒绝且不落 CLI
2. InfoTool：查看（info_string=None）与清空（info_string=""）不受门禁限制
3. TaskTool：info <id> <内容> 同门禁；-d 清空与只读查看不受限
4. 辅助判定：_is_task_running / _lookup_task_status（假 task_manager）
"""

from __future__ import annotations

import pytest

from peng.tools.task import (
    InfoTool,
    TaskTool,
    _info_running_gate_reason,
    _is_task_running,
    _lookup_task_status,
)


class _FakeTaskManager:
    """假 TaskManager：只提供 get_task（门禁只需 status 字段）"""

    def __init__(self, tasks: dict[int, dict]):
        self._tasks = tasks
        self.calls: list[int] = []

    def get_task(self, task_id):
        self.calls.append(task_id)
        return self._tasks.get(task_id)


class _CapturedRun:
    def __init__(self):
        self.cmds: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.cmds.append(list(cmd))
        return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()


def _tm(status: str | None, task_id: int = 3001) -> _FakeTaskManager:
    task = {"id": task_id, "status": status} if status is not None else None
    return _FakeTaskManager({task_id: task} if task else {})


# ==================== 辅助判定 ====================


def test_is_task_running_active_states():
    assert _is_task_running("running")
    assert _is_task_running("accepting")


def test_is_task_running_inactive_states():
    for status in ("pending", "accepted", "paused", "interrupted", "completed", "failed", "cancelled"):
        assert not _is_task_running(status)


def test_is_task_running_unknown_passes():
    """查询失败/事务不存在（status=None）按运行中放行，不因查询异常阻断注入"""
    assert _is_task_running(None)


def test_lookup_task_status_uses_context_task_manager():
    tool = InfoTool()
    tool.set_context(task_manager=_tm("paused"))
    assert _lookup_task_status(tool, 3001) == "paused"


def test_gate_reason_mentions_status_and_alternatives():
    tool = InfoTool()
    tool.set_context(task_manager=_tm("completed"))
    reason = _info_running_gate_reason(tool, 3001)
    assert reason and "completed" in reason and "resume" in reason
    assert _info_running_gate_reason(tool, 3001)  # 幂等，无副作用


# ==================== InfoTool ====================


@pytest.mark.parametrize("status", ["running", "accepting"])
async def test_infotool_send_allowed_when_running(monkeypatch, status):
    tool = InfoTool()
    tool.set_context(task_id=2089, task_manager=_tm(status))
    captured = _CapturedRun()
    monkeypatch.setattr("peng.tools.task.subprocess.run", captured)

    await tool.execute(task_id=3001, info_string="提醒：先 git diff 复核")
    assert len(captured.cmds) == 1
    assert "info" in captured.cmds[0] and "3001" in captured.cmds[0]
    assert "事务#2089" in captured.cmds[0]


@pytest.mark.parametrize(
    "status", ["pending", "paused", "interrupted", "completed", "failed", "cancelled"]
)
async def test_infotool_send_rejected_when_not_running(monkeypatch, status):
    tool = InfoTool()
    tool.set_context(task_id=2089, task_manager=_tm(status))
    captured = _CapturedRun()
    monkeypatch.setattr("peng.tools.task.subprocess.run", captured)

    result = await tool.execute(task_id=3001, info_string="唤醒试探")
    assert captured.cmds == []  # 关键：绝不落到 CLI，因此不会触发 resume 拉起
    assert "🚫" in result.summary
    assert status in result.summary


async def test_infotool_view_not_gated(monkeypatch):
    """查看 info（info_string=None）不受运行状态限制"""
    tool = InfoTool()
    tool.set_context(task_id=2089, task_manager=_tm("completed"))
    captured = _CapturedRun()
    monkeypatch.setattr("peng.tools.task.subprocess.run", captured)

    await tool.execute(task_id=3001)
    assert len(captured.cmds) == 1
    assert "--sender" not in captured.cmds[0]


async def test_infotool_delete_not_gated(monkeypatch):
    """清空 info（info_string=""）不受运行状态限制"""
    tool = InfoTool()
    tool.set_context(task_id=2089, task_manager=_tm("paused"))
    captured = _CapturedRun()
    monkeypatch.setattr("peng.tools.task.subprocess.run", captured)

    result = await tool.execute(task_id=3001, info_string="")
    assert "🚫" not in result.summary
    assert len(captured.cmds) == 1


async def test_infotool_unknown_task_passes_to_cli(monkeypatch):
    """事务查不到时放行给 CLI（由 CLI 统一报「事务不存在」）"""
    tool = InfoTool()
    tool.set_context(task_id=2089, task_manager=_tm(None))
    captured = _CapturedRun()
    monkeypatch.setattr("peng.tools.task.subprocess.run", captured)

    await tool.execute(task_id=3001, info_string="x")
    assert len(captured.cmds) == 1


# ==================== TaskTool info 子命令 ====================


async def test_tasktool_info_send_allowed_when_running(monkeypatch):
    tool = TaskTool()
    tool.set_context(task_id=2089, workdir="/tmp", task_manager=_tm("running"))
    captured = _CapturedRun()
    monkeypatch.setattr("peng.tools.task.subprocess.run", captured)

    result = await tool.execute(args="info 3001 请注意xxx")
    assert "🚫" not in result.summary
    cmd = captured.cmds[0]
    assert cmd[2] == "info" and "3001" in cmd
    assert "--sender" in cmd and cmd[cmd.index("--sender") + 1] == "事务#2089"


@pytest.mark.parametrize("status", ["pending", "paused", "completed", "cancelled"])
async def test_tasktool_info_send_rejected_when_not_running(monkeypatch, status):
    tool = TaskTool()
    tool.set_context(task_id=2089, workdir="/tmp", task_manager=_tm(status))
    captured = _CapturedRun()
    monkeypatch.setattr("peng.tools.task.subprocess.run", captured)

    result = await tool.execute(args="info 3001 请注意xxx")
    assert captured.cmds == []
    assert "🚫" in result.summary and status in result.summary


async def test_tasktool_info_file_option_gated(monkeypatch):
    """显式 --file 传内容也属于发送，非运行事务必须被拒绝（不能绕过门禁）"""
    tool = TaskTool()
    tool.set_context(task_id=2089, workdir="/tmp", task_manager=_tm("paused"))
    captured = _CapturedRun()
    monkeypatch.setattr("peng.tools.task.subprocess.run", captured)

    result = await tool.execute(args="info 3001 --file /tmp/x.txt")
    assert captured.cmds == []
    assert "🚫" in result.summary


async def test_tasktool_info_view_and_delete_not_gated(monkeypatch):
    tool = TaskTool()
    tool.set_context(task_id=2089, workdir="/tmp", task_manager=_tm("completed"))
    captured = _CapturedRun()
    monkeypatch.setattr("peng.tools.task.subprocess.run", captured)

    await tool.execute(args="info 3001")  # 只读查看
    await tool.execute(args="info 3001 -d")  # 清空

    assert len(captured.cmds) == 2
    assert "3001" in captured.cmds[0] and "--file" not in captured.cmds[0]
    assert "-d" in captured.cmds[1] and "3001" in captured.cmds[1]


async def test_tasktool_other_subcommands_not_gated(monkeypatch):
    """门禁只作用于 info 子命令，content/checklist 等其他子命令不受影响"""
    tool = TaskTool()
    tool.set_context(task_id=2089, workdir="/tmp", task_manager=_tm("completed"))
    captured = _CapturedRun()
    monkeypatch.setattr("peng.tools.task.subprocess.run", captured)

    await tool.execute(args="checklist 3001 已完成")
    assert len(captured.cmds) == 1
    assert captured.cmds[0][2] == "checklist"

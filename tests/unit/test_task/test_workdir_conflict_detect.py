#!/usr/bin/env python3
"""事务#1920：nest+workdir 冲突检测路径规范化单元测试

根因：#1873 workdir='D:\\work\\fcc'（反斜杠）与 #1919 workdir='D:/work/fcc'
（正斜杠）语义相同，但 _check_workdir_conflicts 严格字符串相等比较漏检。
修复：_workdir_compare_key 规范化（斜杠方向/normpath/按平台大小写折叠）
+ nest 大小写不敏感比较。

覆盖：
1. _workdir_compare_key：斜杠/尾斜杠/./../大小写/平台语义
2. _check_workdir_conflicts：#1873+#1919 真实场景复现、排除规则、警告/info注入
"""

import sys
from pathlib import Path

# 项目根目录加入 sys.path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from peng.llm.tool_engine import ToolExecutionEngine


# ==================== _workdir_compare_key ====================


def test_backslash_vs_forward_slash_windows():
    """事务#1920 根因场景：反斜杠 vs 正斜杠"""
    a = ToolExecutionEngine._workdir_compare_key(r"D:\work\fcc", "Windows")
    b = ToolExecutionEngine._workdir_compare_key("D:/work/fcc", "Windows")
    assert a == b, f"反斜杠与正斜杠应视为相同目录: {a!r} != {b!r}"


def test_trailing_slash_and_dots():
    """尾斜杠 / 重复斜杠 / . / .. 变体视为相同目录"""
    base = ToolExecutionEngine._workdir_compare_key("D:/work/fcc", "Windows")
    for variant in [
        "D:/work/fcc/",
        "D://work//fcc",
        "D:/work/./fcc",
        "D:/work/fcc/../fcc",
        "D:\\work\\fcc\\",
        "D:/work/fcc/",
    ]:
        key = ToolExecutionEngine._workdir_compare_key(variant, "Windows")
        assert key == base, f"{variant!r} 应规范化为 {base!r}, 实际 {key!r}"


def test_windows_case_insensitive():
    """Windows 平台大小写不敏感"""
    a = ToolExecutionEngine._workdir_compare_key(r"D:\Work\FCC", "Windows")
    b = ToolExecutionEngine._workdir_compare_key("d:/work/fcc", "Windows")
    assert a == b, f"Windows 大小写应折叠: {a!r} != {b!r}"


def test_darwin_case_insensitive():
    """macOS(Darwin) 平台大小写不敏感"""
    a = ToolExecutionEngine._workdir_compare_key("/Users/Feng/Work", "Darwin")
    b = ToolExecutionEngine._workdir_compare_key("/users/feng/work", "Darwin")
    assert a == b


def test_linux_case_sensitive():
    """Linux 平台大小写敏感：不同大小写是不同目录"""
    a = ToolExecutionEngine._workdir_compare_key("/home/feng/Work", "Linux")
    b = ToolExecutionEngine._workdir_compare_key("/home/feng/work", "Linux")
    assert a != b, "Linux 大小写敏感，不应折叠"


def test_linux_slash_normalization():
    """Linux 平台斜杠方向/尾斜杠仍规范化"""
    a = ToolExecutionEngine._workdir_compare_key("/home/feng/work/", "Linux")
    b = ToolExecutionEngine._workdir_compare_key("/home/feng//work", "Linux")
    assert a == b


def test_unknown_platform_drive_letter_folds():
    """平台未知时盘符路径按 Windows 风格折叠大小写"""
    a = ToolExecutionEngine._workdir_compare_key(r"D:\Work\FCC", "")
    b = ToolExecutionEngine._workdir_compare_key("D:/work/fcc", "")
    assert a == b, "未知平台 + 盘符路径应折叠大小写"


def test_unknown_platform_posix_keeps_case():
    """平台未知时非盘符路径保留大小写（避免Linux误报）"""
    a = ToolExecutionEngine._workdir_compare_key("/home/feng/Work", "")
    b = ToolExecutionEngine._workdir_compare_key("/home/feng/work", "")
    assert a != b


def test_unc_paths():
    """UNC 网络路径斜杠方向统一"""
    a = ToolExecutionEngine._workdir_compare_key(r"\\server\share\proj", "Windows")
    b = ToolExecutionEngine._workdir_compare_key("//server/share/proj", "Windows")
    assert a == b, f"UNC 路径应视为相同: {a!r} != {b!r}"


def test_different_directories_differ():
    """不同目录不能误判相同"""
    a = ToolExecutionEngine._workdir_compare_key("D:/work/fcc", "Windows")
    b = ToolExecutionEngine._workdir_compare_key("D:/work/fcc2", "Windows")
    c = ToolExecutionEngine._workdir_compare_key("D:/work", "Windows")
    assert a != b and a != c and b != c


# ==================== _check_workdir_conflicts 集成逻辑 ====================


class _FakeDB:
    def __init__(self, running):
        self._running = running
        self.add_info_calls = []
        self.add_info_senders = []

    def get_running_tasks(self):
        return self._running

    def add_task_info(self, task_id, info_content, sender=None, dedup=False):
        self.add_info_calls.append((task_id, info_content))
        self.add_info_senders.append((task_id, sender))


class _FakeTaskManager:
    def __init__(self, db):
        self.db = db


def _make_engine(task: dict, running: list, platform: str = "Windows"):
    """构造绕过重量级 __init__ 的引擎实例（仅设置被测方法所需属性）"""
    engine = ToolExecutionEngine.__new__(ToolExecutionEngine)
    engine._task = task
    engine._task_id = task["id"]
    engine.task_manager = _FakeTaskManager(_FakeDB(running))
    engine._workdir_conflict_warnings = []
    # 实例属性遮蔽 bound method，避免真实 NestBridge/HTTP 查询
    engine._get_nest_platform = lambda nest: platform
    return engine


def test_real_scenario_1873_vs_1919():
    """复现事务#1920 报障的真实场景：#1919 启动时应检测到 #1873"""
    engine = _make_engine(
        task={"id": 1919, "parent_task_id": None, "nest": "epyc", "workdir": "D:/work/fcc"},
        running=[
            # #1873：同一目录的反斜杠写法（根因现场）
            {"id": 1873, "parent_task_id": None, "nest": "epyc", "workdir": "D:\\work\\fcc"},
            # 无关事务：不同 nest
            {"id": 1900, "parent_task_id": None, "nest": "local", "workdir": "D:/work/fcc"},
            # 无关事务：同 nest 不同目录
            {"id": 1901, "parent_task_id": None, "nest": "epyc", "workdir": "D:/other"},
        ],
        platform="Windows",
    )
    engine._check_workdir_conflicts()

    # 本事务自身警告已保存（供迭代循环 history.add_user 注入）
    assert len(engine._workdir_conflict_warnings) == 1
    warn = engine._workdir_conflict_warnings[0]
    assert "#1873" in warn
    assert "#1900" not in warn and "#1901" not in warn

    # 对方事务 #1873 收到 info 注入
    injected_ids = [tid for tid, _ in engine.task_manager.db.add_info_calls]
    assert injected_ids == [1873]


def test_conflict_case_variants_detected():
    """尾斜杠/大小写/nest大小写变体均检测为冲突"""
    engine = _make_engine(
        task={"id": 101, "parent_task_id": None, "nest": "Epyc", "workdir": "D:/work/fcc/"},
        running=[
            {"id": 102, "parent_task_id": None, "nest": "epyc", "workdir": r"d:\WORK\fcc"},
        ],
        platform="Windows",
    )
    engine._check_workdir_conflicts()
    assert len(engine._workdir_conflict_warnings) == 1
    assert [tid for tid, _ in engine.task_manager.db.add_info_calls] == [102]
    # 事务#2089：冲突警告info记录发送者事务标识
    assert engine.task_manager.db.add_info_senders == [(102, "事务#101")]


def test_linux_platform_case_sensitive_no_conflict():
    """Linux 平台仅大小写不同的 workdir 不算冲突"""
    engine = _make_engine(
        task={"id": 101, "parent_task_id": None, "nest": "local", "workdir": "/home/feng/work"},
        running=[
            {"id": 102, "parent_task_id": None, "nest": "local", "workdir": "/home/feng/Work"},
        ],
        platform="Linux",
    )
    engine._check_workdir_conflicts()
    assert engine._workdir_conflict_warnings == []
    assert engine.task_manager.db.add_info_calls == []


def test_subtask_and_manager_excluded():
    """子事务和事务总管#2排除，空 nest/workdir 跳过"""
    engine = _make_engine(
        task={"id": 101, "parent_task_id": None, "nest": "epyc", "workdir": "D:/work/fcc"},
        running=[
            # 子事务：排除
            {"id": 110, "parent_task_id": 100, "nest": "epyc", "workdir": "D:/work/fcc"},
            # 事务总管：排除
            {"id": 2, "parent_task_id": None, "nest": "epyc", "workdir": "D:/work/fcc"},
            # 空 workdir：跳过
            {"id": 111, "parent_task_id": None, "nest": "epyc", "workdir": ""},
            # 空 nest：跳过
            {"id": 112, "parent_task_id": None, "nest": "", "workdir": "D:/work/fcc"},
            # 自身：排除
            {"id": 101, "parent_task_id": None, "nest": "epyc", "workdir": "D:/work/fcc"},
        ],
        platform="Windows",
    )
    engine._check_workdir_conflicts()
    assert engine._workdir_conflict_warnings == []
    assert engine.task_manager.db.add_info_calls == []


def test_empty_own_workdir_skips():
    """本事务 nest/workdir 为空时不检测"""
    engine = _make_engine(
        task={"id": 101, "parent_task_id": None, "nest": "", "workdir": ""},
        running=[
            {"id": 102, "parent_task_id": None, "nest": "", "workdir": ""},
        ],
    )
    engine._check_workdir_conflicts()
    assert engine._workdir_conflict_warnings == []


def test_identical_strings_still_detected():
    """回归：完全相同的字符串（原逻辑）仍正常检测"""
    engine = _make_engine(
        task={"id": 101, "parent_task_id": None, "nest": "local", "workdir": "/mnt/p/feng/work/peng"},
        running=[
            {"id": 102, "parent_task_id": None, "nest": "local", "workdir": "/mnt/p/feng/work/peng"},
        ],
        platform="Linux",
    )
    engine._check_workdir_conflicts()
    assert len(engine._workdir_conflict_warnings) == 1
    assert [tid for tid, _ in engine.task_manager.db.add_info_calls] == [102]

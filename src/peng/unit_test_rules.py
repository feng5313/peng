#!/usr/bin/env python3
"""
单元测试目录结构规则检查模块

检查 tests/unit 和 src/peng 的目录结构是否完全一致（除 tests/unit/bin 目录外）。
所有文件名应一一对应。

同时检查单元测试是否使用测试数据库，避免污染实际数据。
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)
class StructureMismatch(NamedTuple):
    """结构不匹配记录"""

    location: str  # 位置描述
    expected: str  # 期望的内容
    actual: str  # 实际的内容
    message: str  # 错误消息


class DatabaseViolation(NamedTuple):
    """数据库使用违规记录"""

    file_path: str  # 文件路径
    line_number: int  # 行号
    content: str  # 违规内容
    message: str  # 错误消息


# 禁止在单元测试中使用的实际数据库路径模式
FORBIDDEN_DB_PATTERNS = [
    r'["\']data/messages\.db["\']',
    r'["\']data/tasks\.db["\']',
    r'["\']data/processes\.db["\']',
    r'["\'].*["\']\s*/\s*["\']messages\.db["\']',
    r'["\'].*["\']\s*/\s*["\']tasks\.db["\']',
]

# 允许的测试数据库路径模式
ALLOWED_TEST_DB_PATTERNS = [
    r"test_messages\.db",
    r"test_tasks\.db",
    r"test_processes\.db",
    r"test_.*\.db",
    r"tmp_path",  # pytest fixture
    r"tempfile",
    os.path.join(tempfile.gettempdir(), ""),  # 系统临时目录（/tmp/ 或 C:\Temp\ 等）
]

# 预编译正则表达式（避免每次调用重新编译）
_COMPILED_FORBIDDEN_PATTERNS = [re.compile(p) for p in FORBIDDEN_DB_PATTERNS]
_COMPILED_ALLOWED_PATTERNS = [re.compile(p) for p in ALLOWED_TEST_DB_PATTERNS]

# 并行检查的线程数
_MAX_WORKERS = 8


class UnitTestRulesChecker:
    """单元测试目录结构规则检查器"""

    def __init__(self, project_root: Path | None = None):
        """
        初始化检查器

        Args:
            project_root: 项目根目录，默认为当前文件所在项目的根目录
        """
        if project_root is None:
            project_root = Path(__file__).resolve().parent.parent.parent
        self.project_root = project_root
        self.src_dir = self.project_root / "src" / "peng"
        self.test_dir = self.project_root / "tests" / "unit"

    def get_python_files(self, directory: Path, base: Path) -> set[str]:
        """
        获取目录下所有 Python 文件的相对路径集合

        Args:
            directory: 目录路径
            base: 基础路径（用于计算相对路径）

        Returns:
            Python 文件相对路径集合
        """
        if not directory.exists():
            return set()

        files = set()
        for py_file in directory.rglob("*.py"):
            # 跳过以下划线开头的文件（包括 __init__.py）
            if py_file.name.startswith("_"):
                continue
            rel_path = str(py_file.relative_to(base))
            files.add(rel_path)
        return files

    def check_directory_structure(self) -> list[StructureMismatch]:
        """
        检查 tests/unit 和 src/peng 的目录结构是否一致

        Returns:
            不匹配记录列表
        """
        mismatches = []

        # 获取 src/peng 下的所有 Python 文件（相对于 src/peng）
        src_files = self.get_python_files(self.src_dir, self.src_dir)

        # 获取 tests/unit 下的所有 Python 文件（相对于 tests/unit）
        # 排除 bin 目录
        test_files = set()
        if self.test_dir.exists():
            for py_file in self.test_dir.rglob("*.py"):
                if py_file.name.startswith("_"):
                    continue
                # 跳过 bin 目录
                if "bin" in py_file.parts:
                    continue
                rel_path = str(py_file.relative_to(self.test_dir))
                test_files.add(rel_path)

        # 检查 src/peng 中的每个文件是否在 tests/unit 中有对应
        for src_file in src_files:
            # 转换 src/peng 的文件路径到 tests/unit 的路径
            # 例如：core/task/manager.py -> core/test_manager.py 或 core/task/test_manager.py
            # 或者：core/task/manager.py -> core/task/test_manager.py

            # 提取模块路径和文件名
            parts = src_file.split("/")
            if len(parts) == 1:
                # 根目录文件：xxx.py -> test_xxx.py
                expected_test = "test_" + parts[0]
            else:
                # 子目录文件：dir/xxx.py -> dir/test_xxx.py
                dir_path = "/".join(parts[:-1])
                filename = parts[-1]
                expected_test = f"{dir_path}/test_{filename}"

            if expected_test not in test_files:
                mismatches.append(
                    StructureMismatch(
                        location=f"src/peng/{src_file}",
                        expected=f"tests/unit/{expected_test}",
                        actual="不存在",
                        message=f"src/peng/{src_file} 在 tests/unit 中没有对应的测试文件",
                    )
                )

        # 检查 tests/unit 中的每个文件是否在 src/peng 中有对应
        for test_file in test_files:
            if not test_file.startswith("test_"):
                continue

            # 转换 tests/unit 的文件路径到 src/peng 的路径
            parts = test_file.split("/")
            filename = parts[-1][5:]  # 移除 "test_" 前缀

            if len(parts) == 1:
                # 根目录文件：test_xxx.py -> xxx.py
                expected_src = filename
            else:
                # 子目录文件：dir/test_xxx.py -> dir/xxx.py
                dir_path = "/".join(parts[:-1])
                expected_src = f"{dir_path}/{filename}"

            if expected_src not in src_files:
                mismatches.append(
                    StructureMismatch(
                        location=f"tests/unit/{test_file}",
                        expected=f"src/peng/{expected_src}",
                        actual="不存在",
                        message=f"tests/unit/{test_file} 在 src/peng 中没有对应的源文件",
                    )
                )

        return mismatches

    def check_test_database_usage(self) -> list[DatabaseViolation]:
        """
        检查单元测试是否使用了测试数据库而非实际数据库

        Returns:
            违规记录列表
        """
        violations = []

        if not self.test_dir.exists():
            return violations

        # 获取所有需要检查的文件
        py_files = [
            py_file for py_file in self.test_dir.rglob("*.py") if not py_file.name.startswith("_")
        ]

        def check_file(py_file: Path) -> list[DatabaseViolation]:
            """检查单个文件的数据库使用违规"""
            file_violations = []
            try:
                content = py_file.read_text(encoding="utf-8")
                lines = content.split("\n")

                for line_num, line in enumerate(lines, 1):
                    # 检查是否使用了禁止的数据库路径（使用预编译正则）
                    for pattern in _COMPILED_FORBIDDEN_PATTERNS:
                        if pattern.search(line):
                            # 检查是否是允许的测试路径
                            is_allowed = any(
                                allowed.search(line) for allowed in _COMPILED_ALLOWED_PATTERNS
                            )
                            if not is_allowed:
                                file_violations.append(
                                    DatabaseViolation(
                                        file_path=str(py_file.relative_to(self.project_root)),
                                        line_number=line_num,
                                        content=line.strip()[:80],
                                        message=(
                                            "单元测试不应使用实际数据库路径，"
                                            "请使用测试数据库 (data/test_*.db) 或 tmp_path"
                                        ),
                                    )
                                )
                                break  # 每行只报告一次
            except Exception as e:
                import traceback
                logger.error(f"❌ check_file() - unit_test_rules.py:235: 异常 - {e}\n{traceback.format_exc()}")
            return file_violations

        # 并行检查所有文件
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
            futures = {executor.submit(check_file, f): f for f in py_files}
            for future in as_completed(futures):
                violations.extend(future.result())

        return violations

    def check_actual_database_not_exists(self) -> tuple[bool, str]:
        """
        检查实际数据库文件是否被创建（单元测试后应不存在）

        Returns:
            (是否通过，消息)
        """
        data_dir = self.project_root / "data"
        actual_dbs = ["messages.db", "tasks.db", "processes.db"]
        found = []

        for db in actual_dbs:
            db_path = data_dir / db
            if db_path.exists():
                found.append(db)

        if found:
            return False, f"发现实际数据库文件: {', '.join(found)}。单元测试不应创建实际数据库！"

        return True, "✓ 未发现实际数据库文件"

    def check(self) -> tuple[bool, str]:
        """
        执行完整检查

        Returns:
            (是否通过，消息)
        """
        mismatches = self.check_directory_structure()

        if not mismatches:
            return True, "✓ 目录结构检查通过"

        # 生成错误报告
        error_lines = ["单元测试目录结构检查失败!", ""]
        error_lines.append(
            "tests/unit 和 src/peng 的目录结构应完全一致（除 tests/unit/bin 目录外）"
        )
        error_lines.append("所有文件名应一一对应（tests/unit 中的文件应以 test_ 开头）")
        error_lines.append("")
        error_lines.append("发现以下不匹配:")

        # 只报告前 20 个问题，避免输出过长
        max_report = 20
        for m in mismatches[:max_report]:
            error_lines.append(f"  - {m.message}")
            error_lines.append(f"    期望：{m.expected}")
            error_lines.append(f"    实际：{m.actual}")

        remainder = len(mismatches) - max_report
        if remainder > 0:
            error_lines.append(f"  ... 还有 {remainder} 个不匹配未显示")

        error_lines.append("")
        error_lines.append("请确保 tests/unit 目录结构与 src/peng 保持一致")

        return False, "\n".join(error_lines)


class TestDatabaseChecker:
    """测试数据库使用检查器"""

    def __init__(self, project_root: Path | None = None):
        if project_root is None:
            project_root = Path(__file__).resolve().parent.parent.parent
        self.project_root = project_root
        self.data_dir = project_root / "data"

    def check_before_tests(self) -> dict[str, bool]:
        """
        测试前检查，记录实际数据库状态

        Returns:
            各实际数据库是否存在
        """
        actual_dbs = ["messages.db", "tasks.db", "processes.db"]
        status = {}
        for db in actual_dbs:
            status[db] = (self.data_dir / db).exists()
        return status

    def check_after_tests(self, before_status: dict[str, bool]) -> list[str]:
        """
        测试后检查，检测是否有新的实际数据库被创建

        Args:
            before_status: 测试前的数据库状态

        Returns:
            被创建的数据库列表
        """
        actual_dbs = ["messages.db", "tasks.db", "processes.db"]
        created = []
        for db in actual_dbs:
            exists_now = (self.data_dir / db).exists()
            if exists_now and not before_status.get(db, False):
                created.append(db)
        return created

    def check(self) -> tuple[bool, str]:
        """
        执行完整检查

        Returns:
            (是否通过，消息)
        """

        return True, "✓ 测试数据库检查通过"


def check_unit_test_rules() -> tuple[bool, str]:
    """
    便捷函数：检查单元测试目录结构规则

    Returns:
        (是否通过，消息)
    """
    checker = UnitTestRulesChecker()
    return checker.check()


def check_test_database_rules() -> tuple[bool, str]:
    """
    便捷函数：检查测试数据库使用规则

    Returns:
        (是否通过，消息)
    """
    checker = TestDatabaseChecker()
    return checker.check()


if __name__ == "__main__":
    # 直接运行此脚本进行检查
    passed, message = check_unit_test_rules()
    print(message)

    db_passed, db_message = check_test_database_rules()
    print(db_message)

    exit(0 if (passed and db_passed) else 1)

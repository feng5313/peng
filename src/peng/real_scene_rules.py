#!/usr/bin/env python3
"""
真实场景测试规则检查模块

检查 tests/feature, tests/stress, tests/performance 下的脚本
是否符合真实场景测试规则：
- 只使用 bin/fly, bin/task, bin/msg CLI 工具
- 不使用 pytest
- 不直接操作数据库（通过 peng.core.db 模块操作PostgreSQL）
- 不直接调用 src 目录下代码（分析测试结果除外）
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple


class RuleViolation(NamedTuple):
    """规则违规记录"""

    file: str  # 文件相对路径
    line: int  # 行号
    message: str  # 违规消息


class RealSceneRulesChecker:
    """真实场景测试规则检查器"""

    # 禁止的导入
    FORBIDDEN_IMPORTS = [
        "import pytest",
        "from pytest",
    ]

    def __init__(self, project_root: Path | None = None):
        """
        初始化检查器

        Args:
            project_root: 项目根目录，默认为当前文件所在项目的根目录
        """
        if project_root is None:
            project_root = Path(__file__).resolve().parent.parent.parent
        self.project_root = project_root

    def check_directories(self) -> list[RuleViolation]:
        """
        检查 tests/feature, tests/stress, tests/performance 目录

        Returns:
            违规记录列表
        """
        check_dirs = [
            self.project_root / "tests" / "feature",
            self.project_root / "tests" / "stress",
            self.project_root / "tests" / "performance",
        ]

        violations = []
        for check_dir in check_dirs:
            if not check_dir.exists():
                continue

            for script in check_dir.glob("*.py"):
                if script.name.startswith("_"):
                    continue

                file_violations = self._check_file(script)
                violations.extend(file_violations)

        return violations

    def _check_file(self, script: Path) -> list[RuleViolation]:
        """
        检查单个文件

        Args:
            script: 脚本文件路径

        Returns:
            违规记录列表
        """
        violations = []

        content = script.read_text(encoding="utf-8")

        # 快速检查：如果文件没有禁止的导入，跳过
        has_forbidden = any(forb in content for forb in self.FORBIDDEN_IMPORTS)
        if not has_forbidden:
            return violations

        lines = content.split("\n")
        relative_path = str(script.relative_to(self.project_root))

        for i, line in enumerate(lines, 1):
            stripped = line.strip()

            # 跳过注释
            if stripped.startswith("#"):
                continue

            # 检查禁止的导入
            violations.extend(
                RuleViolation(
                    file=relative_path,
                    line=i,
                    message=f"禁止使用 '{stripped}'",
                )
                for forbidden in self.FORBIDDEN_IMPORTS
                if stripped.startswith(forbidden)
            )

        return violations

    def check(self) -> tuple[bool, str]:
        """
        执行完整检查

        Returns:
            (是否通过，消息)
        """
        violations = self.check_directories()

        if not violations:
            return True, "✓ 规则检查通过"

        # 生成错误报告
        error_lines = ["真实场景测试规则检查失败!", ""]
        error_lines.append("tests/feature, tests/stress, tests/performance 下的脚本应遵守以下规则:")
        error_lines.append("  - 只使用 bin/fly, bin/task, bin/msg CLI 工具")
        error_lines.append("  - 不使用 pytest 导入")
        error_lines.append("  - 不直接操作数据库（通过 peng.core.db 模块操作PostgreSQL）")
        error_lines.append("")
        error_lines.append("发现以下违规导入:")

        # 只报告前 20 个问题，避免输出过长
        max_report = 20
        error_lines.extend(f"  - {v.file}:{v.line}: {v.message}" for v in violations[:max_report])

        remainder = len(violations) - max_report
        if remainder > 0:
            error_lines.append(f"  ... 还有 {remainder} 个问题未显示")

        error_lines.append("")
        error_lines.append("请修正相关测试文件，改为使用 subprocess 调用 CLI 工具进行测试")
        error_lines.append("或者将单元测试移至 tests/unit 目录")

        return False, "\n".join(error_lines)


def check_real_scene_rules() -> tuple[bool, str]:
    """
    便捷函数：检查真实场景测试规则

    Returns:
        (是否通过，消息)
    """
    checker = RealSceneRulesChecker()
    return checker.check()


if __name__ == "__main__":
    # 直接运行此脚本进行检查
    passed, message = check_real_scene_rules()
    print(message)
    exit(0 if passed else 1)

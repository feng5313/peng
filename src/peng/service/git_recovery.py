"""
Git恢复模块

提供Git级别的崩溃恢复功能，包括：
- 创建崩溃分支保存改动
- Git软回退（保留修改在暂存区）
- Git混合回退（保留修改在工作区）
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from typing import Tuple, Optional

logger = logging.getLogger(__name__)


def git_reset_soft(commit_ref: str = "HEAD~1") -> Tuple[bool, str]:
    """
    Git软回退：撤销commit但保留修改在暂存区
    这是"给机会再次修改"的最佳方式

    Args:
        commit_ref: commit引用（默认HEAD~1，回退到上一个commit）

    Returns:
        (成功标志，消息)
    """
    try:
        # 检查是否在git仓库中
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode != 0 or result.stdout.strip() != "true":
            return False, "当前不在git仓库中"

        # 检查是否有未提交的修改
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.stdout.strip():
            return False, "工作区有未提交的修改，请先stash或commit"

        # 获取当前commit信息
        result = subprocess.run(
            ["git", "log", "-1", "--oneline"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode != 0:
            return False, "无法获取当前commit信息"
        current_commit = result.stdout.strip()

        # 执行soft reset
        result = subprocess.run(
            ["git", "reset", "--soft", commit_ref],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode != 0:
            return False, f"Git reset失败：{result.stderr}"

        # 获取新的当前commit
        result = subprocess.run(
            ["git", "log", "-1", "--oneline"],
            capture_output=True,
            text=True,
            timeout=5
        )
        new_commit = result.stdout.strip() if result.returncode == 0 else "未知"

        # 检查暂存区状态
        result = subprocess.run(
            ["git", "diff", "--cached", "--stat"],
            capture_output=True,
            text=True,
            timeout=5
        )
        staged_files = result.stdout.strip() if result.returncode == 0 else ""

        message_lines = [
            f"已执行Git软回退（git reset --soft {commit_ref}）",
            f"回退前：{current_commit}",
            f"回退后：{new_commit}",
            "",
            "所有修改已保留在暂存区（staging area）",
            "可以直接重新commit，或修改后再commit",
        ]
        if staged_files:
            message_lines.append(f"暂存文件：\n{staged_files}")
        else:
            message_lines.append("暂存文件：无")

        message = "\n".join(message_lines)
        logger.info(message.replace("\n", " "))
        return True, message

    except subprocess.TimeoutExpired:
        return False, "操作超时"
    except Exception as e:
        error_msg = f"Git软回退失败：{e}"
        logger.error(error_msg, exc_info=True)
        return False, error_msg


def git_reset_mixed(commit_ref: str = "HEAD~1") -> Tuple[bool, str]:
    """
    Git混合回退：撤销commit，修改保留在工作区（unstaged）

    Args:
        commit_ref: commit引用（默认HEAD~1，回退到上一个commit）

    Returns:
        (成功标志，消息)
    """
    try:
        # 检查是否在git仓库中
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode != 0 or result.stdout.strip() != "true":
            return False, "当前不在git仓库中"

        # 检查是否有未提交的修改
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.stdout.strip():
            return False, "工作区有未提交的修改，请先stash或commit"

        # 获取当前commit信息
        result = subprocess.run(
            ["git", "log", "-1", "--oneline"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode != 0:
            return False, "无法获取当前commit信息"
        current_commit = result.stdout.strip()

        # 执行mixed reset（默认模式，不加参数）
        result = subprocess.run(
            ["git", "reset", commit_ref],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode != 0:
            return False, f"Git reset失败：{result.stderr}"

        # 获取新的当前commit
        result = subprocess.run(
            ["git", "log", "-1", "--oneline"],
            capture_output=True,
            text=True,
            timeout=5
        )
        new_commit = result.stdout.strip() if result.returncode == 0 else "未知"

        # 检查工作区状态
        result = subprocess.run(
            ["git", "diff", "--stat"],
            capture_output=True,
            text=True,
            timeout=5
        )
        changed_files = result.stdout.strip() if result.returncode == 0 else ""

        message_lines = [
            f"已执行Git混合回退（git reset {commit_ref}）",
            f"回退前：{current_commit}",
            f"回退后：{new_commit}",
            "",
            "所有修改已保留在工作区（working directory）",
            "需要重新add后再commit",
        ]
        if changed_files:
            message_lines.append(f"修改文件：\n{changed_files}")
        else:
            message_lines.append("修改文件：无")

        message = "\n".join(message_lines)
        logger.info(message.replace("\n", " "))
        return True, message

    except subprocess.TimeoutExpired:
        return False, "操作超时"
    except Exception as e:
        error_msg = f"Git混合回退失败：{e}"
        logger.error(error_msg, exc_info=True)
        return False, error_msg


def create_crash_branch(crash_info: dict, service_name: str = "peng") -> Tuple[bool, str]:
    """
    创建崩溃分支，保存导致崩溃的所有改动

    Args:
        crash_info: 崩溃信息字典（包含时间戳、错误消息等）
        service_name: 服务名称（默认"peng"）

    Returns:
        (成功标志，消息)
    """
    try:
        # 检查是否在git仓库中
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode != 0 or result.stdout.strip() != "true":
            return False, "当前不在git仓库中"

        # 检查是否有未提交的修改
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if not result.stdout.strip():
            return False, "工作区无未提交的修改，无需创建崩溃分支"

        # 生成分支名称：crash-YYYYMMDD_HHMMSS
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        branch_name = f"crash-{timestamp}"

        # 获取当前分支
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=5
        )
        current_branch = result.stdout.strip() if result.returncode == 0 else "unknown"

        # 创建并切换到崩溃分支
        result = subprocess.run(
            ["git", "checkout", "-b", branch_name],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode != 0:
            return False, f"创建崩溃分支失败：{result.stderr}"

        logger.info(f"已创建崩溃分支：{branch_name}")

        # 添加所有改动
        result = subprocess.run(
            ["git", "add", "-A"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode != 0:
            return False, f"添加文件失败：{result.stderr}"

        # 提交改动
        error_msg = crash_info.get("error_message", "未知错误")
        commit_message = f"崩溃分支：{service_name}服务启动失败\n\n崩溃时间：{crash_info.get('timestamp', '未知')}\n错误类型：{crash_info.get('exception_type', '未知')}\n错误消息：{error_msg}\n原分支：{current_branch}\n\n此分支保存了导致崩溃的所有改动，可用于分析和调试。"

        result = subprocess.run(
            ["git", "commit", "-m", commit_message],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode != 0:
            return False, f"提交改动失败：{result.stderr}"

        logger.info(f"已提交改动到崩溃分支：{branch_name}")

        # 查看改动统计
        result = subprocess.run(
            ["git", "diff", "--stat", "HEAD~1", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5
        )
        diff_stat = result.stdout.strip() if result.returncode == 0 else ""

        message_lines = [
            f"已创建崩溃分支并保存所有改动",
            f"分支名称：{branch_name}",
            f"原分支：{current_branch}",
            f"提交信息：已保存导致崩溃的改动",
            "",
            "所有改动已安全保存在崩溃分支上",
            "可以随时切换到此分支进行分析和调试",
            f"切换命令：git checkout {branch_name}",
        ]
        if diff_stat:
            message_lines.append(f"改动文件：\n{diff_stat}")

        message = "\n".join(message_lines)
        logger.info(message.replace("\n", " "))
        return True, message

    except subprocess.TimeoutExpired:
        return False, "操作超时"
    except Exception as e:
        error_msg = f"创建崩溃分支失败：{e}"
        logger.error(error_msg, exc_info=True)
        return False, error_msg


def restore_from_crash_branch() -> Tuple[bool, str]:
    """
    从崩溃分支恢复改动（如果需要）

    Returns:
        (成功标志，消息)
    """
    try:
        # 查找崩溃分支
        result = subprocess.run(
            ["git", "branch", "-a"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode != 0:
            return False, "获取分支列表失败"

        branches = result.stdout.split("\n")
        crash_branches = [
            b.strip().replace("* ", "").replace("remotes/origin/", "")
            for b in branches if b.strip().startswith("crash-")
        ]

        if not crash_branches:
            return False, "未找到崩溃分支"

        # 选择最新的崩溃分支
        crash_branch = sorted(crash_branches)[-1]

        # 获取当前分支
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=5
        )
        current_branch = result.stdout.strip() if result.returncode == 0 else "unknown"

        # 检查工作区状态
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.stdout.strip():
            return False, "工作区有未提交的修改，请先处理"

        # 切换到崩溃分支
        result = subprocess.run(
            ["git", "checkout", crash_branch],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode != 0:
            return False, f"切换到崩溃分支失败：{result.stderr}"

        message_lines = [
            f"已切换到崩溃分支",
            f"当前分支：{crash_branch}",
            f"原分支：{current_branch}",
            "",
            "现在可以：",
            "1. 分析导致崩溃的代码",
            "2. 修复问题",
            "3. 测试修复",
            f"4. 使用 git merge 将修复合并回 {current_branch}",
            "",
            f"切回原分支：git checkout {current_branch}",
        ]

        message = "\n".join(message_lines)
        logger.info(message.replace("\n", " "))
        return True, message

    except subprocess.TimeoutExpired:
        return False, "操作超时"
    except Exception as e:
        error_msg = f"恢复崩溃分支失败：{e}"
        logger.error(error_msg, exc_info=True)
        return False, error_msg


def safe_rollback_to_stable(crash_info: dict, service_name: str = "peng") -> Tuple[bool, str]:
    """
    安全回退到稳定版本：先创建崩溃分支保存改动，然后回退到上一个稳定版本

    Args:
        crash_info: 崩溃信息字典
        service_name: 服务名称（默认"peng"）

    Returns:
        (成功标志，消息)
    """
    try:
        messages = []

        # 1. 创建崩溃分支保存改动
        success, msg = create_crash_branch(crash_info, service_name)
        if success:
            messages.append(msg)
            logger.info("已创建崩溃分支保存改动")
        else:
            # 如果没有改动或创建失败，跳过这一步
            if "无需创建" in msg:
                logger.info("无未提交修改，跳过创建崩溃分支")
            else:
                messages.append(f"警告：{msg}")

        # 2. 回退到上一个稳定版本
        from peng.service.version_manager import auto_rollback_on_failure
        success, msg = auto_rollback_on_failure()
        if success:
            messages.append(f"\n{msg}")
        else:
            messages.append(f"\n错误：{msg}")

        return True, "\n".join(messages)

    except Exception as e:
        error_msg = f"安全回退失败：{e}"
        logger.error(error_msg, exc_info=True)
        return False, error_msg


def get_git_commit_info(commit_ref: str = "HEAD") -> Optional[dict]:
    """
    获取指定commit的详细信息

    Args:
        commit_ref: commit引用

    Returns:
        commit信息字典，包含hash、author、date、message等
    """
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H|%an|%ad|%s", commit_ref],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode != 0:
            return None

        parts = result.stdout.strip().split("|")
        if len(parts) != 4:
            return None

        return {
            "hash": parts[0],
            "author": parts[1],
            "date": parts[2],
            "message": parts[3]
        }
    except Exception as e:
        logger.error(f"获取commit信息失败：{e}")
        return None
"""
服务失败自动回退模块

实现服务连续失败时的自动回退机制。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from peng.config import get_project_root

logger = logging.getLogger(__name__)

# 失败次数阈值
FAILURE_THRESHOLD = 3


def get_failure_counter_path() -> Path:
    """
    获取失败计数器文件路径

    Returns:
        计数器文件路径
    """
    project_root = get_project_root()
    return project_root / ".failure_count"


def get_failure_count() -> int:
    """
    获取当前失败次数

    Returns:
        失败次数
    """
    counter_file = get_failure_counter_path()
    if not counter_file.exists():
        return 0

    try:
        count_str = counter_file.read_text().strip()
        return int(count_str) if count_str else 0
    except Exception as e:
        logger.error(f"读取失败计数器失败: {e}", exc_info=True)
        return 0


def record_failure() -> int:
    """
    记录一次失败

    Returns:
        当前失败次数
    """
    counter_file = get_failure_counter_path()
    current_count = get_failure_count()
    new_count = current_count + 1

    try:
        counter_file.write_text(str(new_count))
        logger.warning(f"⚠️  服务启动失败（第 {new_count} 次）")
        print(f"⚠️  服务启动失败（第 {new_count} 次）")

        # 检查是否达到阈值
        if new_count >= FAILURE_THRESHOLD:
            logger.error(f"❌ 失败次数达到阈值 {FAILURE_THRESHOLD}，触发自动回退")
            print(f"❌ 失败次数达到阈值 {FAILURE_THRESHOLD}，触发自动回退")
            check_and_rollback()

        return new_count
    except Exception as e:
        logger.error(f"记录失败失败: {e}", exc_info=True)
        return new_count


def reset_failures() -> None:
    """
    重置失败计数器
    """
    counter_file = get_failure_counter_path()
    if counter_file.exists():
        try:
            counter_file.unlink()
            logger.info("✅ 已重置失败计数器")
            print("✅ 已重置失败计数器")
        except Exception as e:
            logger.error(f"重置失败计数器失败: {e}", exc_info=True)


def initialize_failure_counter() -> None:
    """
    初始化失败计数器（服务启动成功时调用，重置计数器）
    """
    try:
        current_count = get_failure_count()
        if current_count > 0:
            logger.info(f"服务启动成功，重置失败计数器（之前: {current_count}）")
            reset_failures()
        else:
            logger.debug("失败计数器已经是0，无需重置")
    except Exception as e:
        logger.error(f"初始化失败计数器失败: {e}", exc_info=True)


def get_current_commit_hash() -> str | None:
    """
    获取当前 commit hash

    Returns:
        commit hash 或 None
    """
    try:
        project_root = get_project_root()
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10,  # 防止垃圾进程卡死项目
        )
        if result.returncode == 0:
            return result.stdout.strip()
        else:
            logger.error(f"获取 commit hash 失败: {result.stderr}")
            return None
    except Exception as e:
        logger.error(f"获取 commit hash 异常: {e}", exc_info=True)
        return None


def git_revert_head() -> bool:
    """
    回退到上一个 commit（git revert HEAD）

    Returns:
        True 成功，False 失败
    """
    try:
        project_root = get_project_root()

        # 先获取当前 commit hash（用于发布修复事务）
        bad_commit = get_current_commit_hash()
        if not bad_commit:
            logger.error("❌ 无法获取当前 commit hash")
            print("❌ 无法获取当前 commit hash")
            return False, "❌ 自动回退失败"

        logger.info(f"🔄 正在回退 commit: {bad_commit[:8]}")
        print(f"🔄 正在回退 commit: {bad_commit[:8]}")

        # 执行 git revert HEAD（不自动提交，使用 --no-commit 选项）
        # 注意：使用 --no-commit 可以让我们检查回退结果后再决定是否提交
        result = subprocess.run(
            ["git", "revert", "--no-commit", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=60,  # git操作允许1分钟
        )

        if result.returncode != 0:
            logger.error(f"❌ git revert 失败: {result.stderr}")
            print(f"❌ git revert 失败: {result.stderr}")
            return False, "❌ 自动回退失败"

        # 提交回退
        result = subprocess.run(
            [
                "git",
                "commit",
                "-m",
                f"auto rollback: revert {bad_commit[:8]} due to service failure",
            ],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=60,  # git操作允许1分钟
        )

        if result.returncode != 0:
            logger.error(f"❌ git commit 失败: {result.stderr}")
            print(f"❌ git commit 失败: {result.stderr}")
            return False, "❌ 自动回退失败"

        logger.info("✅ 已回退到上一个 commit")
        print("✅ 已回退到上一个 commit")

        # 保存坏 commit hash（用于后续发布修复事务）
        bad_commit_file = project_root / ".peng_bad_commit"
        bad_commit_file.write_text(bad_commit)

        return True, "✅ 自动回退成功"

    except Exception as e:
        logger.error(f"❌ git revert 异常: {e}", exc_info=True)
        print(f"❌ git revert 异常: {e}")
        import traceback

        traceback.print_exc()
        return False, "❌ 自动回退失败"


def publish_fix_task(bad_commit: str) -> bool:
    """
    发布修复事务

    Args:
        bad_commit: 导致失败的 commit hash

    Returns:
        True 成功，False 失败
    """
    try:
        project_root = get_project_root()
        msg_cmd = project_root / "bin" / "msg"

        message = f"commit {bad_commit[:8]} 导致bin/fly service启动失败，请修复并提交后运行bin/fly restart重启service"

        logger.info(f"📢 正在发布修复事务: {message}")
        print(f"📢 正在发布修复事务: {message}")

        result = subprocess.run(
            [sys.executable, str(msg_cmd), "--local", message] if sys.platform == "win32" else [str(msg_cmd), "--local", message],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=120,  # bin/msg操作允许2分钟
        )

        if result.returncode != 0:
            logger.error(f"❌ 发布修复事务失败: {result.stderr}")
            print(f"❌ 发布修复事务失败: {result.stderr}")
            return False, "❌ 自动回退失败"

        logger.info("✅ 已发布修复事务")
        print("✅ 已发布修复事务")

        return True, "✅ 自动回退成功"

    except Exception as e:
        logger.error(f"❌ 发布修复事务异常: {e}", exc_info=True)
        print(f"❌ 发布修复事务异常: {e}")
        import traceback

        traceback.print_exc()
        return False, "❌ 自动回退失败"


def check_and_rollback() -> tuple[bool, str]:
    """
    检查并执行回退逻辑

    Returns:
        True 成功，False 失败
    """
    try:
        # 执行 git revert
        if not git_revert_head():
            logger.error("❌ 回退失败，无法继续")
            print("❌ 回退失败，无法继续")
            return False, "❌ 自动回退失败"

        # 重置失败计数器
        reset_failures()

        # 尝试重启服务
        logger.info("🔄 正在重启服务...")
        print("🔄 正在重启服务...")

        if restart_service() == 0:
            logger.info("✅ 服务重启成功")
            print("✅ 服务重启成功")

            # 读取坏 commit hash
            project_root = get_project_root()
            bad_commit_file = project_root / ".peng_bad_commit"

            if bad_commit_file.exists():
                bad_commit = bad_commit_file.read_text().strip()
                bad_commit_file.unlink()

                # 发布修复事务
                if publish_fix_task(bad_commit):
                    logger.info("✅ 自动回退流程完成")
                    print("✅ 自动回退流程完成")
                    return True, "✅ 自动回退成功"
            else:
                logger.warning("⚠️  未找到坏 commit 信息，跳过发布修复事务")
                print("⚠️  未找到坏 commit 信息，跳过发布修复事务")
                return True, "✅ 自动回退成功"
        else:
            logger.error("❌ 服务重启失败")
            print("❌ 服务重启失败")
            return False, "❌ 自动回退失败"

    except Exception as e:
        logger.error(f"❌ 自动回退流程异常: {e}", exc_info=True)
        print(f"❌ 自动回退流程异常: {e}")
        import traceback

        traceback.print_exc()
        return False, "❌ 自动回退失败"

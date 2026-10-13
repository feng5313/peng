"""
Fly 核心逻辑模块

提供 fly 启动脚本的核心功能。
只被 bin/fly 引用。

项目路径获取统一使用：
    from peng.config import get_project_root
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path
from peng.common.logging_config import setup_logging_from_config


class ChineseHelpArgumentParser(argparse.ArgumentParser):
    """自定义 ArgumentParser，使用中文帮助文本"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def format_help(self):
        # 生成默认帮助文本
        help_text = super().format_help()
        # 替换英文 "show this help message and exit" 为中文
        help_text = help_text.replace("show this help message and exit", "显示帮助信息并退出")
        return help_text


from peng.common.platform import ensure_uv_sync, get_venv_python
from peng.common.paths import chown_to_project_owner, ensure_dir_with_owner
from peng.config import get_project_root
from peng.config.env import load_env_from_rc

from peng.ui.message_utils import process_message as _process_message
from peng.ui.message_utils import process_reply as _process_reply


def _setup_global_exception_handler():
    """设置全局异常处理器，确保异常退出时有完整 traceback"""

    def handle_exception(exc_type, exc_value, exc_traceback):
        """全局异常处理钩子"""
        # KeyboardInterrupt 应该被正常处理，不要拦截
        if issubclass(exc_type, KeyboardInterrupt):
            # 重新抛出，让 asyncio.run() 正常捕获并清理
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return

        # 打印完整 traceback 到 stderr
        print("\n" + "=" * 70, file=sys.stderr)
        print("💥 未捕获的异常 (Uncaught Exception)", file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        traceback.print_exception(exc_type, exc_value, exc_traceback, file=sys.stderr)
        print("=" * 70, file=sys.stderr)

    # 安装全局异常处理器
    sys.excepthook = handle_exception


def find_python(project_root: Path) -> tuple[str, str]:
    """查找可用的 Python 解释器（跨平台）"""
    venv_python = get_venv_python(project_root)
    if venv_python.exists():
        return str(venv_python), "uv虚拟环境"
    return sys.executable, "系统Python"


def _split_string_args(argv: list[str]) -> list[str]:
    """
    拆分字符串形式的参数

    例如:
        "service" -> ["service"]
        "service --force" -> ["service", "--force"]
        "chat 你好" -> ["chat", "你好"]

    Args:
        argv: 原始参数列表

    Returns:
        拆分后的参数列表
    """
    if not argv:
        return argv

    result = []

    for arg in argv:
        # 如果参数被引号包裹（单引号或双引号），拆分它
        if (arg.startswith('"') and arg.endswith('"')) or (
            arg.startswith("'") and arg.endswith("'")
        ):
            # 移除引号并拆分
            stripped = arg[1:-1].strip()
            if stripped:
                # 按空格拆分，保留引号内的内容
                import shlex

                try:
                    # 使用 shlex.split 正确处理引号
                    parts = shlex.split(stripped)
                    result.extend(parts)
                except ValueError:
                    # shlex 解析失败，直接按空格拆分
                    result.extend(stripped.split())
        else:
            result.append(arg)

    return result


def _create_arg_parser() -> argparse.ArgumentParser:
    """创建参数解析器"""
    parser = ChineseHelpArgumentParser(
        prog="fly",
        description="鹏 - AI 编程助手",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  fly                       启动交互式聊天
  fly chat                  启动交互式聊天
  fly [message|m] <内容>    发送新消息并查看输出
  fly [reply|r] <id> <内容> 回复消息
  fly [continue|c] <内容>   回复上一个主题
  fly [answer|a] <内容>     回复上一个消息
  fly watch|w|i|info <id> [内容]  监控事务输出（可选注入信息并恢复事务）
  fly service               启动后台服务
  fly service --webui       启动后台服务并启用WebUI
  fly service --force       强制启动（清理端口）
  fly install               安装为系统服务（peng + PostgreSQL）并启动
  fly uninstall             卸载系统服务（peng + PostgreSQL）
  fly start                 启动 peng + nest 服务
  fly stop                  停止 peng + nest 服务
  fly restart               重启 peng + nest 服务
  fly status                查看服务状态（peng + PostgreSQL）
  fly clear-all-data        清空所有数据（数据库、data、logs）
  fly --help                显示帮助信息
        """,
    )

    # 全局参数
    parser.add_argument(
        "--test_db",
        action="store_true",
        help="使用测试数据库 (test_messages.db / test_tasks.db)",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="强制本地模式执行（不连接远程peng service）",
    )
    # 子命令
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # chat 子命令（默认）
    chat_parser = subparsers.add_parser("chat", help="启动交互式聊天（默认）")
    chat_parser.add_argument("-n", "--nest", type=str, help="指定关联的nest名称")
    chat_parser.add_argument("-w", "--workdir", type=str, help="指定工作目录")

    # service 子命令
    service_parser = subparsers.add_parser("service", help="启动后台服务")
    service_parser.add_argument(
        "--webui",
        action="store_true",
        help="启用WebUI",
    )
    service_parser.add_argument(
        "--force",
        action="store_true",
        help="强制启动（先清理端口）",
    )
    service_parser.add_argument(
        "--listen",
        type=str,
        metavar="HOST:PORT",
        help="监听地址（格式: host:port 或 port）",
    )
    service_parser.add_argument(
        "--debug",
        action="store_true",
        help="调试模式（保留启动成功标记，方便调试崩溃问题）",
    )

    # restart 子命令
    restart_parser = subparsers.add_parser("restart", help="重启后台服务（peng + nest）")
    restart_parser.add_argument(
        "--webui",
        action="store_true",
        help="启用WebUI",
    )
    restart_parser.add_argument(
        "--force",
        action="store_true",
        help="强制启动（先清理端口）",
    )
    restart_parser.add_argument(
        "--listen",
        type=str,
        metavar="HOST:PORT",
        help="监听地址（格式: host:port 或 port）",
    )
    # install 子命令

    # stop 子命令
    stop_parser = subparsers.add_parser("stop", help="停止服务（peng + nest）")

    # start 子命令
    start_parser = subparsers.add_parser("start", help="启动服务（peng + nest）")

    # status 子命令
    status_parser = subparsers.add_parser("status", help="查看服务状态（peng + PostgreSQL）")

    install_parser = subparsers.add_parser(
        "install", help="安装为系统服务（peng + PostgreSQL）并自动启动"
    )

    # uninstall 子命令
    uninstall_parser = subparsers.add_parser("uninstall", help="停止并卸载系统服务")

    # msg 子命令（发送新消息，非交互模式）
    msg_parser = subparsers.add_parser("message", help="发送新消息")
    msg_parser.add_argument("content", nargs="*", help="消息内容")
    msg_parser.add_argument("--file", type=str, help="从文件读取消息内容")
    msg_parser.add_argument("-n", "--nest", type=str, help="指定关联的nest名称")
    msg_parser.add_argument("-w", "--workdir", type=str, help="指定工作目录")

    msg_parser = subparsers.add_parser("m", help="发送新消息")
    msg_parser.add_argument("content", nargs="*", help="消息内容")
    msg_parser.add_argument("--file", type=str, help="从文件读取消息内容")
    msg_parser.add_argument("-n", "--nest", type=str, help="指定关联的nest名称")
    msg_parser.add_argument("-w", "--workdir", type=str, help="指定工作目录")

    # reply 子命令（回复消息，非交互模式）
    reply_parser = subparsers.add_parser("reply", help="回复消息")
    reply_parser.add_argument("msg_id", type=int, help="要回复的消息ID")
    reply_parser.add_argument("content", nargs="*", default=["继续"], help="回复内容")
    reply_parser.add_argument("--file", type=str, help="从文件读取回复内容")
    reply_parser.add_argument("-n", "--nest", type=str, help="指定关联的nest名称")
    reply_parser.add_argument("-w", "--workdir", type=str, help="指定工作目录")

    reply_parser = subparsers.add_parser("r", help="回复消息")
    reply_parser.add_argument("msg_id", type=int, help="要回复的消息ID")
    reply_parser.add_argument("content", nargs="*", default=["继续"], help="回复内容")
    reply_parser.add_argument("--file", type=str, help="从文件读取回复内容")
    reply_parser.add_argument("-n", "--nest", type=str, help="指定关联的nest名称")
    reply_parser.add_argument("-w", "--workdir", type=str, help="指定工作目录")

    continue_parser = subparsers.add_parser("continue", help="回复上一个主题（消息链首）")
    continue_parser.add_argument(
        "content", nargs="*", default=["继续"], help="消息内容（默认: 继续）"
    )
    continue_parser.add_argument("--file", type=str, help="从文件读取消息内容")
    continue_parser.add_argument("-n", "--nest", type=str, help="指定关联的nest名称")
    continue_parser.add_argument("-w", "--workdir", type=str, help="指定工作目录")

    continue_parser = subparsers.add_parser("c", help="回复上一个主题（消息链首）")
    continue_parser.add_argument(
        "content", nargs="*", default=["继续"], help="消息内容（默认: 继续）"
    )
    continue_parser.add_argument("--file", type=str, help="从文件读取消息内容")
    continue_parser.add_argument("-n", "--nest", type=str, help="指定关联的nest名称")
    continue_parser.add_argument("-w", "--workdir", type=str, help="指定工作目录")

    answer_parser = subparsers.add_parser("answer", help="回复上一个消息")
    answer_parser.add_argument("content", nargs="*", default=["继续"], help="消息内容")
    answer_parser.add_argument("--file", type=str, help="从文件读取消息内容")
    answer_parser.add_argument("-n", "--nest", type=str, help="指定关联的nest名称")
    answer_parser.add_argument("-w", "--workdir", type=str, help="指定工作目录")

    answer_parser = subparsers.add_parser("a", help="回复上一个消息")
    answer_parser.add_argument("content", nargs="*", default=["继续"], help="消息内容")
    answer_parser.add_argument("--file", type=str, help="从文件读取消息内容")
    answer_parser.add_argument("-n", "--nest", type=str, help="指定关联的nest名称")
    answer_parser.add_argument("-w", "--workdir", type=str, help="指定工作目录")

    # rollback 子命令
    rollback_parser = subparsers.add_parser("rollback", help="回退到上一个版本")
    rollback_parser.add_argument(
        "version", nargs="?", help="指定回退到的版本（不指定则回退到上一个版本）"
    )

    # crash-logs 子命令
    crash_parser = subparsers.add_parser("crash-logs", help="查看崩溃日志列表")
    crash_parser.add_argument("--info", metavar="VERSION", help="查看指定版本的崩溃详情")
    crash_parser.add_argument("--clear", metavar="DAYS", type=int, help="清理多少天前的崩溃日志")

    # clear-all-data 子命令
    clear_data_parser = subparsers.add_parser(
        "clear-all-data", help="清空所有数据（数据库、data、logs目录）"
    )
    clear_data_parser.add_argument(
        "--force",
        action="store_true",
        help="跳过所有确认直接清空（极度危险！）",
    )

    # git-reset-soft 子命令
    git_soft_parser = subparsers.add_parser(
        "git-reset-soft", help="Git软回退（撤销commit但保留修改在暂存区）"
    )
    git_soft_parser.add_argument(
        "commit", nargs="?", default="HEAD~1", help="commit引用（默认HEAD~1，回退到上一个commit）"
    )

    # git-reset-mixed 子命令
    git_mixed_parser = subparsers.add_parser(
        "git-reset-mixed", help="Git混合回退（撤销commit，修改保留在工作区）"
    )
    git_mixed_parser.add_argument(
        "commit", nargs="?", default="HEAD~1", help="commit引用（默认HEAD~1，回退到上一个commit）"
    )

    # git-crash-branch 子命令
    git_crash_parser = subparsers.add_parser(
        "git-crash-branch", help="恢复崩溃分支（切换到崩溃分支分析问题）"
    )

    # git-branch-list 子命令
    git_branch_parser = subparsers.add_parser("git-branch-list", help="列出崩溃分支")

    # watch 子命令（监控事务输出，非交互模式）
    watch_parser = subparsers.add_parser("watch", help="监控事务输出（非交互模式）")
    watch_parser.add_argument("task_id", type=int, help="要监控的事务ID")
    watch_parser.add_argument(
        "content", nargs="*", default=[], help="要注入事务的信息内容（可选，有内容时自动添加info并恢复事务）"
    )

    # watch 简写
    for _alias in ("w", "i", "info"):
        watch_parser = subparsers.add_parser(_alias, help="监控事务输出（watch 简写）")
        watch_parser.add_argument("task_id", type=int, help="要监控的事务ID")
        watch_parser.add_argument(
            "content", nargs="*", default=[], help="要注入事务的信息内容（可选）"
        )

    return parser


def _parse_listen_arg(listen_value: str | None) -> tuple[str | None, int | None]:
    """解析 --listen 参数

    Args:
        listen_value: 监听地址，格式为 "host:port" 或 "port"

    Returns:
        (host, port) 元组
    """
    if not listen_value:
        return None, None

    try:
        if ":" in listen_value:
            host_part, port_part = listen_value.rsplit(":", 1)
            return host_part, int(port_part)
        else:
            # 只有端口号
            return None, int(listen_value)
    except (ValueError, IndexError):
        print(f"❌ 无效的监听地址: {listen_value}", file=sys.stderr)
        return None, None


def _clear_all_data(force: bool) -> int:
    """清空所有数据（数据库、data目录、logs目录）

    Args:
        force: 是否跳过所有确认

    Returns:
        0 成功, 1 失败
    """
    import shutil

    from peng.config import get_path
    from peng.db.connection import get_config, get_connection

    # 第一次确认：危险操作警告
    if not force:
        print("=" * 70)
        print("⚠️  警告：即将执行极其危险的操作！")
        print("=" * 70)
        print("此操作将删除以下内容：")
        print("  1. PostgreSQL 数据库所有业务表数据")
        print("  2. data/ 目录下所有文件和子目录")
        print("  3. logs/ 目录下所有日志文件")
        print()
        print("⚠️  此操作不可逆！所有数据将永久丢失！")
        print("=" * 70)
        print()

        confirm1 = input("确认继续？请输入 'I_CONFIRM_DATA_LOSS'：")
        if confirm1 != "I_CONFIRM_DATA_LOSS":
            print("❌ 操作已取消")
            return 1

        # 第二次确认：具体内容确认
        print()
        print("即将删除：")
        print("  - 所有消息记录")
        print("  - 所有事务记录")
        print("  - 所有输出文件")
        print("  - 所有聊天历史")
        print("  - 所有日志文件")
        print()
        confirm2 = input("最后一次确认？请输入 'DELETE_EVERYTHING'：")
        if confirm2 != "DELETE_EVERYTHING":
            print("❌ 操作已取消")
            return 1

    print()
    print("🚀 开始清空所有数据...")

    total_deleted = 0

    # 0. 杀死所有已注册的后台daemon进程（data/daemon.json中记录的）
    print()
    print("🔪 正在清理后台daemon进程...")
    try:
        import peng.common.daemon_registry as dr

        # 先清理已退出的daemon记录
        dr.cleanup_dead_daemons()

        # 杀死所有daemon
        result = dr.kill_all_daemons_sync()
        total_killed = len(result["killed"]) + len(result["already_dead"])
        total_errors = len(result["errors"])
        if total_killed > 0:
            print(f"  ✅ 已清理 {total_killed} 个后台daemon")
        if total_errors > 0:
            print(f"  ⚠️  {total_errors} 个daemon清理失败", file=sys.stderr)
        if total_killed == 0 and total_errors == 0:
            print(f"  ℹ️  没有已注册的后台daemon")
    except Exception as e:
        print(f"  ⚠️  清理后台daemon失败（可忽略）: {e}", file=sys.stderr)

    # 1. 终止所有其他数据库连接（在清空数据库之前）
    #    Web 服务等进程可能持有表锁，导致 DROP TABLE 被阻塞。
    #    必须终止所有其他连接才能获得 ACCESS EXCLUSIVE 锁。
    print()
    print("🧹 正在终止所有其他数据库连接...")
    try:
        from peng.db.connection import terminate_all_other_connections

        terminated_pids = terminate_all_other_connections()
        if terminated_pids:
            print(f"  ✅ 已终止 {len(terminated_pids)} 个连接")
        else:
            print(f"  ✅ 没有其他活动连接")
    except Exception as e:
        print(f"  ⚠️  终止连接失败（可忽略）: {e}", file=sys.stderr)

    # 2. 清空数据库（删除所有表并重建）
    print()
    print("📊 正在删除并重建数据库表...")
    try:
        from peng.db.connection import truncate_all_tables

        total_deleted = truncate_all_tables()
        if total_deleted > 0:
            print(f"  ✅ 已删除并重建数据库表（原删除 {total_deleted} 条记录）")
        else:
            print(f"  ✅ 已重建数据库表（原无数据）")
    except Exception as e:
        print(f"  ❌ 删除并重建数据库表失败: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1

    # 2. 清空 data 目录
    print()
    print("📁 正在清空 data/ 目录...")
    try:
        data_dir = get_path("data")
        if data_dir.exists():
            # 删除目录下所有内容
            for item in data_dir.iterdir():
                if item.is_dir():
                    shutil.rmtree(item)
                    print(f"  ✅ 已删除目录 {item.name}/")
                elif item.is_file():
                    item.unlink()
                    print(f"  ✅ 已删除文件 {item.name}")
            print(f"  ✅ data/ 目录已清空")
        else:
            print("  ℹ️  data/ 目录不存在")
    except Exception as e:
        print(f"  ❌ 清空 data/ 目录失败: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1

    # 3. 清空 logs 目录
    print()
    print("📝 正在清空 logs/ 目录...")
    try:
        logs_dir = get_path("logs")
        if logs_dir.exists():
            # 只删除 .log 文件，保留目录结构
            deleted = 0
            skipped = 0
            for item in logs_dir.glob("*.log"):
                try:
                    item.unlink()
                    print(f"  ✅ 已删除 {item.name}")
                    deleted += 1
                except PermissionError:
                    # Windows: 文件被占用时无法删除（如正在写入的日志），清空内容代替
                    try:
                        with open(item, "w") as f:
                            f.truncate(0)
                        print(f"  ✅ 已清空 {item.name}（文件被占用，无法删除）")
                        skipped += 1
                    except Exception:
                        print(f"  ⚠️ 跳过 {item.name}（文件被占用）")
                        skipped += 1
            if deleted > 0 or skipped > 0:
                print(f"  ✅ logs/ 目录已处理（删除 {deleted} 个，清空/跳过 {skipped} 个）")
            else:
                print(f"  ℹ️  logs/ 目录为空")
        else:
            print("  ℹ️  logs/ 目录不存在")
    except Exception as e:
        print(f"  ⚠️ 清空 logs/ 目录部分失败: {e}", file=sys.stderr)

    print()
    print("=" * 70)
    print("🎉 所有数据清空完成！")
    print(f"📊 统计：")
    print(f"   - 数据库记录：{total_deleted} 条")
    print(f"   - data/ 目录：已清空")
    print(f"   - logs/ 目录：已清空")
    print("=" * 70)

    return 0


def run_fly(argv: list[str] | None = None) -> int:
    """Fly 主运行函数"""
    if argv is None:
        argv = sys.argv[1:]

    # Windows 中文版：统一 stdout/stderr 编码为 utf-8，防止 GBK 解码崩溃
    if sys.platform == "win32":
        import io

        for name in ("stdout", "stderr"):
            stream = getattr(sys, name)
            if stream and hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8", errors="replace")
                except Exception:
                    try:
                        setattr(
                            sys,
                            name,
                            io.TextIOWrapper(
                                stream.buffer,
                                encoding="utf-8",
                                errors="replace",
                                line_buffering=stream.line_buffering
                                if hasattr(stream, "line_buffering")
                                else True,
                            ),
                        )
                    except Exception:
                        pass

    # 设置全局异常处理器（最早设置，确保所有异常都能捕获）
    _setup_global_exception_handler()

    load_env_from_rc()
    project_root = get_project_root()

    # 确保依赖已同步（uv sync 会自动创建 .venv）
    # 除非使用 --no-uv 参数跳过
    if not os.environ.get("PENG_NO_UV"):
        # 检查是否已经切换过环境（防止死循环）
        if not os.environ.get("PENV_VENV_SWITCHED"):
            ensure_uv_sync(project_root)

            # 检查是否需要切换到venv环境
            venv_python = get_venv_python(project_root)
            if venv_python.exists():
                # 使用 Python 内置方法检测虚拟环境
                # sys.prefix 指向当前虚拟环境，sys.base_prefix 指向系统 Python
                in_venv = sys.prefix != sys.base_prefix

                # 如果当前不在venv环境，使用venv的Python重新执行
                if not in_venv:
                    # 使用venv的Python重新执行当前脚本
                    # 传递所有原始参数，并设置环境变量标记
                    venv_python_str = str(venv_python)
                    print(f"🔄 切换到uv虚拟环境: {venv_python_str}")
                    env = os.environ.copy()
                    env["PENV_VENV_SWITCHED"] = "1"
                    try:
                        # 直接使用 run()，让 Ctrl+C 正常传播
                        result = subprocess.run(
                            [venv_python_str] + sys.argv,
                            cwd=str(project_root),
                            env=env,
                        )
                        sys.exit(result.returncode)
                    except KeyboardInterrupt:
                        # Ctrl+C 时，干净退出
                        print("\n🛑 收到 Ctrl+C，正在停止服务...", flush=True)
                        sys.exit(130)  # 128 + 2 (SIGINT)
    # 不需要数据库的命令，跳过 PostgreSQL 部署
    _no_db_commands = {
        "stop",
        "start",
        "restart",
        "status",
        "install",
        "uninstall",
        "rollback",
        "git-reset-soft",
        "git-reset-mixed",
        "git-crash-branch",
        "git-branch-list",
    }
    # service 命令需要数据库，但不能自动部署/启动 PG（PG 只由 install/uninstall 管理）
    _service_commands = {"service", "--service"}
    _needs_db = True

    # 远程模式拦截：提取 --local 参数，对需要远程执行的子命令转发到远程peng service
    # 注意：fly命令本身是长运行的（需要等事务处理完），cli_command的请求-响应模式不适合
    # 所以将fly子命令转换为对应的msg命令转发（msg send/reply是短时间完成的）
    #
    # 连接优先级（所有入口点统一，包括 bin/fly 和 .venv/bin/fly）：
    # 1. 本地 peng service → 本地模式直接执行
    # 2. config/nest.json 中默认 nest 的 host → 通过 WebSocket 远程执行
    # 3. 两者都不可用 → 报错退出
    _force_local = "--local" in argv
    _clean_argv = [a for a in argv if a != "--local"]
    argv = _clean_argv  # 后续使用去除 --local 的参数

    _first_arg = argv[0] if argv else ""

    # 优先检测本地服务（所有需要服务的命令共用）
    from peng.cli.remote import is_local_service_running
    _local_running = is_local_service_running()

    if _first_arg:
        # 将 fly 子命令映射为 msg 子命令
        _fly_to_msg_args = None
        if _first_arg in ("message", "msg", "m"):
            # fly m <内容> → msg send <内容>
            _fly_to_msg_args = ["send"] + argv[1:]
        elif _first_arg in ("reply", "r"):
            # fly reply <id> <内容> → msg reply <id> <内容>
            _fly_to_msg_args = ["reply"] + argv[1:]
        elif _first_arg in ("continue", "c"):
            # fly continue <内容> → msg reply 0 <内容>
            _fly_to_msg_args = ["reply", "0"] + argv[1:]
        elif _first_arg in ("answer", "a"):
            # fly answer <内容> → msg reply 1 <内容>
            _fly_to_msg_args = ["reply", "1"] + argv[1:]

        if _fly_to_msg_args is not None:
            # 自动注入 --from/--to（master发给鹏）、--nest 和 --workdir（如果用户未指定）
            # 连接优先级：本地服务优先，无本地服务时连接默认 nest 的 host
            from peng.cli.remote import (
                try_remote_mode,
                get_default_nest_name,
                is_default_nest_local,
            )
            from peng.config.loader import get_master_name, get_agent_name

            _has_from = "--from" in _fly_to_msg_args
            _has_to = "--to" in _fly_to_msg_args
            _has_workdir = "--workdir" in _fly_to_msg_args or "-w" in _fly_to_msg_args
            _has_nest = "--nest" in _fly_to_msg_args or "-n" in _fly_to_msg_args
            _nest_auto_injected = False
            # --from/--to: master发给鹏（fly 子命令都是master操作，from=master, to=鹏）
            if not _has_from:
                _fly_to_msg_args.extend(["--from", get_master_name()])
            if not _has_to:
                _fly_to_msg_args.extend(["--to", get_agent_name()])
            # --workdir 始终注入
            if not _has_workdir:
                _fly_to_msg_args.extend(["--workdir", os.getcwd()])

            # --nest 注入：优先使用 get_self_nest_name()（self字段或instances推断）
            if not _has_nest:
                from peng.cli.remote import get_self_nest_name

                _self_nest = get_self_nest_name()
                if _self_nest:
                    _fly_to_msg_args.extend(["--nest", _self_nest])
                    _nest_auto_injected = True
                elif _force_local or _local_running:
                    # 本地模式但无法识别 nest 身份 → 默认 local
                    _fly_to_msg_args.extend(["--nest", "local"])
                    _nest_auto_injected = True
                else:
                    # 无本地服务，检查默认 nest
                    _default_nest = get_default_nest_name()
                    if _default_nest and not is_default_nest_local():
                        # 默认 nest 的 host 是远程机器，使用 nest 名称
                        _fly_to_msg_args.extend(["--nest", _default_nest])
                        _nest_auto_injected = True
                    else:
                        # 无默认 nest，或默认 nest 的 host 是本机 → 用 local
                        _fly_to_msg_args.extend(["--nest", "local"])
                        _nest_auto_injected = True

            if _force_local or _local_running:
                # 本地模式：本地服务运行中 或 强制 --local，直接继续本地执行
                pass
            else:
                # 无本地服务，通过远程模式执行 msg 命令
                _remote_result = try_remote_mode("msg", _fly_to_msg_args, force_local=False)
                if _remote_result is not None:
                    return _remote_result
                # 远程模式也不可用
                # 如果 --nest 是自动注入的非 local 值，改为 local 再试本地
                if _nest_auto_injected:
                    for _i, _arg in enumerate(_fly_to_msg_args):
                        if _arg in ("--nest", "-n") and _i + 1 < len(_fly_to_msg_args):
                            if _fly_to_msg_args[_i + 1] != "local":
                                _fly_to_msg_args[_i + 1] = "local"
                            break
                if not is_local_service_running():
                    print("无本地服务，且无法连接默认nest。", file=sys.stderr)
                    return 1

        # watch/w 子命令：优先本地，无本地服务时通过远程模式转发
        elif _first_arg in ("watch", "w", "i", "info"):
            if not _force_local and not _local_running:
                from peng.cli.remote import try_remote_mode

                # 无本地服务，通过远程模式转发 fly watch（统一转换为 watch 命令名）
                _remote_result = try_remote_mode("fly", ["watch"] + argv[1:], force_local=False)
                if _remote_result is not None:
                    return _remote_result
                # 远程模式也不可用，报错退出
                from peng.cli.remote import get_default_nest_host_port
                _hp = get_default_nest_host_port()
                _host_info = f"{_hp[0]}:{_hp[1]}" if _hp else "无默认nest"
                print(f"无本地服务，且无法连接默认nest({_host_info})。", file=sys.stderr)
                return 1

    # 对于需要 DB 的命令（chat/message/m 等），如果既没有远程 nest 可用，
    # 也没有本地 peng service 运行，直接提示退出，不尝试安装 PostgreSQL
    _needs_service_commands = {
        "chat",
        "message",
        "m",
        "reply",
        "r",
        "continue",
        "c",
        "answer",
        "a",
        "watch",
        "w",
        "i",
        "info",
    }
    _is_needs_service_command = (not argv) or (argv[0] in _needs_service_commands)
    if _is_needs_service_command:
        if _force_local or _local_running:
            # 本地模式：本地服务运行中 或 强制 --local
            pass
        else:
            # 无本地服务，检查是否有可用的远程 nest
            from peng.cli.remote import (
                get_default_nest_name,
                is_default_nest_local,
            )
            _dn = get_default_nest_name()
            _has_remote = bool(_dn and not is_default_nest_local())
            if not _has_remote:
                print("无本地服务，且无法连接默认nest。", file=sys.stderr)
                return 1

    if argv and argv[0] in _no_db_commands:
        _needs_db = False
    elif argv and argv[0].startswith("-"):
        _needs_db = False  # --help 等选项

    # 有远程 nest 可用时（且无本地服务），不需要本地 PostgreSQL（数据库通过 nest 桥接）
    if _needs_db and not _force_local and not _local_running:
        from peng.cli.remote import get_default_nest_name, is_default_nest_local

        _dn = get_default_nest_name()
        if _dn and not is_default_nest_local():
            _needs_db = False

    if _needs_db:
        # 所有命令只检测PostgreSQL可用性，不自动部署
        # 只有 fly install/uninstall 才能安装/卸载 PostgreSQL
        from peng.db.postgres_auto_deploy import check_postgresql_ready

        _pg_ok, _pg_err = check_postgresql_ready()
        if not _pg_ok:
            # service 模式下重试等待 PG 就绪（nssm 重启时 PG 可能还在恢复中）
            if argv and argv[0] == "service":
                import time as _svc_time

                print(f"PostgreSQL 未就绪，等待中... ({_pg_err})", file=sys.stderr)
                _stable = 0
                for _retry in range(60):
                    _svc_time.sleep(2)
                    _pg_ok, _pg_err = check_postgresql_ready()
                    if _pg_ok:
                        _stable += 1
                        if _stable >= 3:
                            break
                    else:
                        _stable = 0
                if _stable < 3:
                    print(f"PostgreSQL 等待超时，建议尝试重新安装。", file=sys.stderr)
                    return 1
            else:
                print(f"PostgreSQL数据库异常，{_pg_err}，建议尝试重新安装。", file=sys.stderr)
                return 1

    # 数据库部署完成后，再处理 --help 参数
    if len(argv) > 0 and argv[0] in ("-h", "--help"):
        parser = _create_arg_parser()
        parser.parse_args(["-h"])
        return 0

    # 创建参数解析器
    parser = _create_arg_parser()

    # 拆分字符串形式的参数
    # 例如："service" -> ["service"], "service --force" -> ["service", "--force"]
    argv = _split_string_args(argv)

    # 向后兼容：检查是否使用旧格式（无子命令）
    # 如果第一个参数不是子命令，则默认为 chat
    old_format_commands = {
        "--service",
        "--stop-service",
        "--restart-service",
    }

    # 定义所有有效的子命令
    valid_commands = {
        "chat",
        "message",
        "m",
        "reply",
        "r",
        "continue",
        "c",
        "answer",
        "a",
        "watch",
        "w",
        "i",
        "info",
        "service",
        "start",
        "stop",
        "restart",
        "status",
        "install",
        "uninstall",
        "rollback",
        "crash-logs",
        "clear-all-data",
        "git-reset-soft",
        "git-reset-mixed",
        "git-crash-branch",
        "git-branch-list",
    }

    # 检查是否是旧格式
    is_old_format = False
    if argv:
        # 检查是否是旧格式的选项（以 -- 开头但不是子命令）
        if argv[0] in old_format_commands or (
            argv[0].startswith("--") and argv[0] not in valid_commands
        ):
            is_old_format = True
        # 检查是否是普通文本（不是子命令，也不是选项）
        elif argv[0] not in valid_commands and not argv[0].startswith("--"):
            # 不再支持通过命令行传递消息内容
            print("❌ 错误：bin/fly 不再接受命令行消息参数", file=sys.stderr)
            print("   请使用交互式模式启动，然后在对话中输入消息", file=sys.stderr)
            print("   示例：", file=sys.stderr)
            print("     fly           # 启动交互式聊天", file=sys.stderr)
            print("     fly chat      # 启动交互式聊天", file=sys.stderr)
            return 1

    # 如果是旧格式，转换为新的子命令格式
    if is_old_format:
        if "--service" in argv:
            # 提取 --service 后的参数
            new_argv = ["service"]
            for arg in argv:
                if arg == "--service":
                    continue
                elif arg in ("--webui", "--force"):
                    new_argv.append(arg)
                elif arg.startswith("--listen"):
                    if arg.startswith("--listen="):
                        new_argv.append(arg)
                    else:
                        # --listen host:port 格式
                        idx = argv.index(arg)
                        if idx + 1 < len(argv):
                            new_argv.extend([arg, argv[idx + 1]])
        elif "--stop-service" in argv:
            new_argv = ["stop"]
        elif "--restart-service" in argv:
            new_argv = ["restart"]
            for arg in argv:
                if arg == "--restart-service":
                    continue
                elif arg in ("--webui", "--force"):
                    new_argv.append(arg)
                elif arg.startswith("--listen"):
                    if arg.startswith("--listen="):
                        new_argv.append(arg)
                    else:
                        # --listen host:port 格式
                        idx = argv.index(arg)
                        if idx + 1 < len(argv):
                            new_argv.extend([arg, argv[idx + 1]])
        else:
            # 默认为 chat（仅用于旧格式的 -- 开头的选项）
            new_argv = ["chat"] + argv
        argv = new_argv

    # 解析参数
    args = parser.parse_args(argv)

    # 添加项目路径
    sys.path.insert(0, str(project_root / "src"))

    # 处理子命令
    command = args.command or "chat"  # 默认为 chat
    import asyncio

    # 对于 chat/message/m/reply/r/continue/c/answer/a 子命令，
    # 自动注入 --nest 和 --workdir（如果用户未指定）
    # 连接优先级：本地服务优先，无本地服务时连接默认 nest 的 host
    _needs_nest_workdir = command in (
        "chat",
        "message",
        "m",
        "reply",
        "r",
        "continue",
        "c",
        "answer",
        "a",
    )
    if _needs_nest_workdir:
        _args_nest = getattr(args, "nest", None)
        _args_workdir = getattr(args, "workdir", None)
        # --workdir 始终注入
        if not _args_workdir:
            setattr(args, "workdir", os.getcwd())
        # --nest 根据本地/远程模式决定值
        _nest_is_local = False
        if not _args_nest:
            if _force_local:
                setattr(args, "nest", "local")
                _nest_is_local = True
            else:
                if _local_running:
                    # 本地服务运行中 → 用 local
                    setattr(args, "nest", "local")
                    _nest_is_local = True
                else:
                    # 无本地服务，检查默认 nest
                    from peng.cli.remote import get_default_nest_name, is_default_nest_local
                    _default_nest = get_default_nest_name()
                    if _default_nest and not is_default_nest_local():
                        # 默认 nest 的 host 是远程机器，使用 nest 名称
                        setattr(args, "nest", _default_nest)
                    else:
                        # 无默认 nest，或默认 nest 的 host 是本机 → 用 local
                        setattr(args, "nest", "local")
                        _nest_is_local = True
        elif _args_nest == "local":
            _nest_is_local = True
        # 如果 nest 为 local，检测本地 peng service 是否运行
        if _nest_is_local and not _local_running:
            print("无本地服务。", file=sys.stderr)
            return 1

    content = ""
    try:
        # .content 可能不存在
        content = " ".join(args.content)
    except:
        pass
    # 如果指定了 --file，从文件读取内容（优先级高于位置参数）
    file_path = getattr(args, "file", None)
    if file_path:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            print(f"❌ 读取文件失败: {e}", file=sys.stderr)
            return 1
    if command == "chat":
        setup_logging_from_config(level="WARNING")
        # 运行聊天
        from peng.ui.chat import run_chat

        # chat 子命令的消息参数
        message_argv = getattr(args, "message", [])
        # 传递 nest 和 workdir 参数给 chat
        _chat_nest = getattr(args, "nest", None)
        _chat_workdir = getattr(args, "workdir", None)
        if _chat_nest:
            message_argv.extend(["--nest", _chat_nest])
        if _chat_workdir:
            message_argv.extend(["--workdir", _chat_workdir])
        # 传递远程模式信息给 chat
        # 当 nest 指向非 localhost 的远程 peng service 时，chat 走远程模式
        # 注意：远程 HTTP API 端口默认 6688（peng service HTTP），不是 nest WebSocket 端口 8899
        if _chat_nest and not _nest_is_local:
            from peng.cli.remote import get_default_nest_host_port

            _hp = get_default_nest_host_port()
            if _hp:
                message_argv.extend(["--remote-host", _hp[0]])
                # HTTP API 端口固定为 6688（peng service FastAPI），不使用 nest WebSocket 端口
                message_argv.extend(["--remote-port", "6688"])
        return run_chat(message_argv)

    elif command in ("watch", "w", "i", "info"):
        setup_logging_from_config(level="WARNING")
        # 非交互模式：监控事务输出
        task_id = args.task_id

        from peng.task.manager import TaskManager
        from peng.ui.message_utils import _monitor_single_task

        task_manager = TaskManager()
        task = task_manager.get_task(task_id)

        if not task:
            print(f"❌ 事务 #{task_id} 不存在", file=sys.stderr)
            return 1

        # 如果有内容参数，先添加 info 并发送 resume 控制信号（参考 cmd_info 实现）
        watch_content = " ".join(getattr(args, "content", []))
        if watch_content:
            from peng.common.utils import get_text_from_args

            watch_content = get_text_from_args(watch_content)

            success = task_manager.db.add_task_info(task_id, watch_content)
            if success:
                print(f"✅ 事务 #{task_id} 注入信息已添加：{watch_content}", flush=True)
                # 只有当事务不在 running 状态时才插入 resume 控制信号
                # 如果事务已在 running，info 会在下一个迭代自动被检测到并注入
                current_status = task.get("status", "")
                if current_status != "running":
                    task_manager.db.insert_task_control(task_id, "resume")
                    print(f"✅ 事务 #{task_id} 恢复控制已加入队列", flush=True)
                else:
                    print(f"  事务正在运行中，info将在下一次迭代自动注入", flush=True)
            else:
                print(f"❌ 无法添加注入信息", file=sys.stderr)
                return 1

        # 获取关联的 message_id
        message_id = task.get("message_id")
        if not message_id:
            print(f"❌ 事务 #{task_id} 没有关联消息", file=sys.stderr)
            return 1

        status = task.get("status", "")
        print(f"🔍 监控事务 #{task_id} (消息 #{message_id}, 状态: {status})", flush=True)

        # 监控事务输出（Ctrl+C 仅退出监控，不暂停事务也不归档）
        try:
            result = asyncio.run(
                _monitor_single_task(
                    message_id, task_id, auto_archive=False, pause_on_cancel=False,
                    interactive_info=True,
                )
            )
            return 0 if result else 1
        except KeyboardInterrupt:
            # Ctrl+C 仅退出监控，事务继续执行（pause_on_cancel=False 时已在 _monitor_single_task 中处理）
            return 0  # 不报错

    elif command in ("message", "msg", "m", "reply", "r", "continue", "c", "answer", "a"):
        setup_logging_from_config(level="WARNING")
        # 非交互模式：发送消息/回复消息
        _nest = getattr(args, "nest", None)
        _workdir = getattr(args, "workdir", None)
        # 本地执行路径：未指定时自动推断 nest 和 workdir
        if not _nest:
            from peng.cli.remote import get_self_nest_name
            _nest = get_self_nest_name() or "local"
        if not _workdir:
            _workdir = os.getcwd()
        if command in ("message", "msg", "m"):
            coro = _process_message(content, workdir=_workdir, nest=_nest, interactive_info=True)
        elif command in ("reply", "r"):
            coro = _process_reply(args.msg_id, content, workdir=_workdir, nest=_nest, interactive_info=True)
        elif command in ("continue", "c"):
            coro = _process_reply(0, content, "fly_continue", workdir=_workdir, nest=_nest, interactive_info=True)
        else:  # answer, a
            coro = _process_reply(1, content, "fly_answer", workdir=_workdir, nest=_nest, interactive_info=True)

        try:
            result = asyncio.run(coro)
            # process_message/process_reply 成功返回 (message_chain_id, task_id)，失败返回 1
            if isinstance(result, tuple):
                return 0 if result[1] else 1
            return 0 if result else 1
        except KeyboardInterrupt:
            # Ctrl+C 已在 message_utils._monitor_single_task 中处理暂停事务
            return 0  # 不报错

    elif command == "service":
        setup_logging_from_config()
        # 后台服务模式
        import asyncio

        from peng.ui.service import _get_all_service_pids, kill_all_services, run_service

        # 解析 --listen 参数
        webui_host, webui_port = _parse_listen_arg(args.listen)

        # 检查是否有僵尸的 fly service 进程
        # 定义阈值：超过 3 个就认为是僵尸进程
        zombie_threshold = 3
        service_pids = _get_all_service_pids()

        if len(service_pids) > zombie_threshold:
            print(f"⚠️  检测到 {len(service_pids)} 个 fly service 进程，可能存在僵尸进程")
            print(f"   （正常情况下应该只有 1 个服务进程）")
            print()

            if args.force:
                # 自动清理
                print("🧹 正在自动清理所有僵尸进程...")
                killed = kill_all_services()
                if killed > 0:
                    print(f"✅ 已清理 {killed} 个僵尸进程")
                print()
            else:
                # 询问用户
                confirm = input("是否清理所有 fly service 进程？(y/N): ")
                if confirm.lower() == "y":
                    print("🧹 正在清理所有 fly service 进程...")
                    killed = kill_all_services()
                    if killed > 0:
                        print(f"✅ 已清理 {killed} 个进程")
                    print()
                else:
                    print("⚠️  跳过清理，继续启动服务...")
                    print()

        # 强制启动时先清理同一端口的旧服务进程
        if args.force:
            from peng.common.platform import kill_port_process
            from peng.ui.service import _kill_all_service_processes

            port_to_clean = webui_port if webui_port else 6688
            killed = _kill_all_service_processes(port_to_clean)
            if killed > 0:
                print(f"✅ 已终止端口 {port_to_clean} 的 {killed} 个旧服务进程")
            # 清理端口
            sys.stdout.flush()
            kill_port_process(port_to_clean)
            print(f"✅ 端口 {port_to_clean} 已清理，正在启动服务...")
            sys.stdout.flush()

        # 使用try-finally确保资源清理
        try:
            return asyncio.run(
                run_service(
                    webui_enabled=args.webui,
                    webui_host=webui_host,
                    webui_port=webui_port,
                    force_start=args.force,
                )
            )
        except KeyboardInterrupt:
            print("\n🛑 收到 Ctrl+C，正在停止服务...")
            # asyncio.run 会自动清理所有事务
            return 0
        except Exception as e:
            print(f"❌ 服务异常: {e}", file=sys.stderr)
            return 1

    elif command == "restart":
        # 复用 fly stop + fly start，不自创逻辑
        rc_stop = run_fly(["stop"])
        if rc_stop != 0:
            print("⚠️ 停止服务遇到问题，继续尝试启动...")
        rc_start = run_fly(["start"])
        return rc_start

    elif command == "start":
        setup_logging_from_config()

        # 清理可能残留的 nest 停止信号文件
        try:
            from peng.nest.server import NestConnector

            NestConnector.remove_stop_signal()
        except Exception:
            pass

        if sys.platform == "win32":
            from peng.service.windows_service import (
                _is_peng_installed,
                start_pg,
                start_peng,
                _is_process_running,
            )

            if not _is_peng_installed():
                print("服务未安装，正在执行 fly install...")
                return run_fly(["install"])

            import time as _time

            # Start PG if needed
            if not _is_process_running("postgres.exe"):
                from peng.db.postgres_auto_deploy import check_postgresql_ready
                from peng.service.windows_service import _is_pg_installed

                if _is_pg_installed():
                    print("[PostgreSQL] 正在启动...")
                    start_pg()
                    # Wait for ready (stable 3 consecutive checks)
                    _stable = 0
                    for _i in range(60):
                        _time.sleep(1)
                        _ok, _err = check_postgresql_ready()
                        if _ok or "表不存在" in _err:
                            _stable += 1
                            if _stable >= 3:
                                print("[PostgreSQL] 已就绪")
                                break
                        else:
                            _stable = 0
                    else:
                        print(f"❌ PostgreSQL 启动超时，请尝试 fly install 重新安装")
                        return 1

            # Start peng if needed
            # ⚠️ 不能按 fly.exe 映像名判断：fly start 自身就是 fly.exe 进程
            #    （自匹配导致永远误判"已在运行"而跳过启动），按 service
            #    命令行判断（排除自身进程链）
            from peng.service.windows_service import _is_peng_service_running

            if not _is_peng_service_running():
                print("🐣 正在启动服务...")
                start_peng()
                for _i in range(30):
                    _time.sleep(1)
                    if _is_peng_service_running():
                        break
            else:
                print("🐣 已在运行")

            # Wait for nest
            import urllib.request
            import json as _json

            sys.stdout.flush()
            for _i in range(15):
                _time.sleep(1)
                try:
                    req = urllib.request.Request("http://localhost:6688/api/nest/list")
                    with urllib.request.urlopen(req, timeout=2) as resp:
                        data = _json.loads(resp.read())
                    if "local" in {
                        k: v for k, v in data.get("nests", {}).items() if v.get("connected")
                    }:
                        print("🪹 本地鹏窝(nest) 已就绪")
                        break
                except Exception:
                    pass
            else:
                print("🪹  ⚠️ local nest 未能在15秒内就绪，请用 fly status 检查")

            return 0
        else:
            # Linux: 启动 peng 服务，然后验证 local nest 可用性
            from peng.service.systemd_manager import (
                start_service as systemd_start_service,
                is_service_installed,
            )

            # 如果服务未安装，直接调用 fly install
            if not is_service_installed():
                print("服务未安装，正在执行 fly install...")
                rc = run_fly(["install"])
                if rc != 0:
                    print("❌ 安装失败")
                    return rc
                return 0  # install 已经启动了所有服务

            # 启动 peng 服务
            success, msg = systemd_start_service()
            print(f"{msg}")
            if not success:
                return 1

            # 等待服务启动，验证 local nest 可用性
            import time as _time

            def _is_local_nest_ready():
                """检查 local nest 是否就绪（子进程运行 或 bridge中已注册）"""
                # 方式1：检查子进程
                try:
                    from peng.nest.cli import _is_nest_process_running
                    if _is_nest_process_running(name="local"):
                        return True
                except Exception:
                    pass
                # 方式2：检查 NestBridgeManager 中的连接
                try:
                    from peng.nest.bridge import NestBridgeManager
                    mgr = NestBridgeManager.get_instance()
                    for conn in mgr._connections:
                        if conn.name == "local" and not conn._closing:
                            return True
                except Exception:
                    pass
                return False

            print("🪹  等待 local nest 启动...")
            sys.stdout.flush()

            for i in range(15):  # 最多等15秒
                _time.sleep(1)
                try:
                    if _is_local_nest_ready():
                        print("🪹  local nest 已启动✅ ")
                        break
                except Exception:
                    pass
                if i % 5 == 4:
                    print(f"🪹  等待中... ({i + 1}秒)")
                    sys.stdout.flush()
            else:
                # 等待超时，local nest 未启动，尝试手动启动
                print("🪹  ⚠️ local nest 未自动启动，尝试手动启动...")
                try:
                    from peng.nest.cli import (
                        _get_python_cmd,
                        _get_nest_script,
                        _kill_all_nest_processes,
                    )
                    import subprocess as _sp

                    # 清理旧进程
                    _kill_all_nest_processes(name="local")
                    _time.sleep(1)

                    project_root = get_project_root()
                    nest_cmd = _get_python_cmd() + [
                        str(_get_nest_script()),
                        "connect",
                        "--name",
                        "local",
                        "--host",
                        "localhost",
                        "--port",
                        "8899",
                        "--skip-single-instance-check",
                        "--builtin-local",
                    ]
                    log_dir = project_root / "logs"
                    ensure_dir_with_owner(log_dir)
                    nest_log = str(log_dir / "nest_connect.log")
                    chown_to_project_owner(nest_log)
                    # 【关键】open 后必须再 chown：文件不存在时上面的 chown 是 no-op，
                    # root 新建的文件会归 root 所有，导致之后 feng 服务的
                    # systemd 进程无法写入、内置 local nest 启动失败(Permission denied)
                    from peng.common.paths import (
                        open_log_with_owner,
                        get_popen_owner_context,
                    )

                    with open_log_with_owner(nest_log) as log_f:
                        # root 运行 fly 时降权为项目属主启动子进程，
                        # 避免子进程创建的文件（日志/缓存/__pycache__）污染项目目录属主
                        _env, _preexec = get_popen_owner_context()
                        proc = _sp.Popen(
                            nest_cmd,
                            stdout=log_f,
                            stderr=_sp.STDOUT,
                            cwd=str(project_root),
                            env=_env,
                            close_fds=True,
                            start_new_session=True,
                            preexec_fn=_preexec,
                        )
                    print(f"🪹  ✅ local nest 已手动启动 (PID: {proc.pid})")
                except Exception as e:
                    print(f"🪹  ❌ 手动启动 local nest 失败: {e}")

            return 0

    elif command == "status":
        setup_logging_from_config()
        if sys.platform == "win32":
            from peng.service.windows_service import get_all_services_status

            status = get_all_services_status()

            state_icons = {
                "RUNNING": "🟢",
                "STOPPED": "🔴",
                "START_PENDING": "🟡",
                "STOP_PENDING": "🟡",
                "PAUSED": "🟠",
            }
            state_labels = {
                "RUNNING": "运行中",
                "STOPPED": "已停止",
                "START_PENDING": "启动中",
                "STOP_PENDING": "停止中",
                "PAUSED": "已暂停",
            }

            # peng 服务
            peng_installed = status["peng_installed"]
            peng_state = status["peng_state"]
            if peng_installed:
                icon = state_icons.get(peng_state or "UNKNOWN", "⚪")
                label = state_labels.get(peng_state or "", peng_state or "未知")
                print(f"  {icon} peng          : {label}")
            else:
                print(f"  ⚪ peng          : 未安装")

            # PostgreSQL 服务
            pg_installed = status["pg_installed"]
            pg_state = status["pg_state"]
            if pg_installed:
                icon = state_icons.get(pg_state or "UNKNOWN", "⚪")
                label = state_labels.get(pg_state or "", pg_state or "未知")
                print(f"  {icon} PostgreSQL    : {label}")
            else:
                print(f"  ⚪ PostgreSQL    : 未安装（将由peng自动管理）")

            # 数据库连接测试
            try:
                from peng.db.connection import get_config
                import socket

                config = get_config()
                port = config.port
                # Windows: 检查端口是否在排除范围内，自动使用安全端口
                if sys.platform == "win32" and not os.environ.get("PENG_PG_PORT_OVERRIDE"):
                    try:
                        from peng.db.postgres_auto_deploy import _is_port_excluded, _find_safe_port

                        if _is_port_excluded(port):
                            port = _find_safe_port(port)
                    except Exception:
                        pass
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3)
                result = sock.connect_ex((config.host, port))
                sock.close()
                if result == 0:
                    print(f"  🟢 数据库连接    : {config.host}:{port} 可达")
                else:
                    print(f"  🔴 数据库连接    : {config.host}:{port} 不可达")
            except Exception as e:
                print(f"  ⚪ 数据库连接    : 测试失败 ({e})")

            # nest 服务（通过 webui API 查询peng服务进程中的连接状态）
            try:
                from peng.nest.cli import _is_nest_process_running

                local_nest_running = _is_nest_process_running(name="local")
                any_nest_running = _is_nest_process_running()

                # 尝试通过 webui API 查询实际的 nest 连接状态
                try:
                    import urllib.request
                    import json as _json

                    req = urllib.request.Request("http://localhost:6688/api/nest/list")
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        api_data = _json.loads(resp.read())
                    connections = {
                        k: v for k, v in api_data.get("nests", {}).items() if v.get("connected")
                    }
                    if connections:
                        print(f"  🟢 nest          : 运行中 ({len(connections)}个连接)")
                        for key, info in connections.items():
                            platform = info.get("platform", "?")
                            hostname = info.get("hostname", "?")
                            pid = info.get("pid", "?")
                            workdir = info.get("workdir", "")
                            workdir_str = f", workdir={workdir}" if workdir else ""
                            print(f"    🪹 {key} ({platform}/{hostname}, pid={pid}{workdir_str})")
                    elif any_nest_running:
                        print(f"  🟢 nest          : 运行中 (暂无已注册连接)")
                    else:
                        print(f"  🔴 nest          : 已停止")
                except Exception:
                    # webui API 不可用，回退到进程检测
                    # 优先显示 local nest 状态（用户最关心的），而非外部 nest 进程
                    icon = "🟢" if local_nest_running else "🔴"
                    state = "运行中" if local_nest_running else "已停止"
                    print(f"  {icon} nest          : {state}")
            except Exception as e:
                print(f"  ⚪ nest          : 检测失败 ({e})")

            print()
        else:
            from peng.service.systemd_manager import get_service_status, check_service_active

            # peng 服务（systemd）
            success, msg = get_service_status()
            if success:
                print(f"  🟢 peng          : 运行中")
            else:
                # 区分已安装但停止 vs 未安装
                installed = check_service_active() is not None
                if installed:
                    print(f"  🔴 peng          : 已停止")
                else:
                    print(f"  ⚪ peng          : 未安装")

            # PostgreSQL 状态
            try:
                import socket as _socket

                pg_host = "localhost"
                pg_port = 5432
                try:
                    from peng.db.connection import get_config as _get_db_config

                    _db_cfg = _get_db_config()
                    pg_port = _db_cfg.port
                    pg_host = _db_cfg.host
                except Exception:
                    pass
                _sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                _sock.settimeout(3)
                if _sock.connect_ex((pg_host, pg_port)) == 0:
                    _sock.close()
                    print(f"  🟢 PostgreSQL    : 运行中")
                else:
                    _sock.close()
                    # socket 检测失败，尝试 pg_isready 作为备选
                    try:
                        result = subprocess.run(
                            ["pg_isready", "-h", "localhost", "-p", "5432"],
                            capture_output=True,
                            encoding="utf-8",
                            errors="replace",
                            timeout=5,
                        )
                        if result.returncode == 0:
                            print("  🟢 PostgreSQL    : 运行中")
                        else:
                            print(f"  🔴 PostgreSQL    : 已停止")
                    except Exception:
                        print("  🔴 PostgreSQL    : 已停止")
            except Exception:
                try:
                    result = subprocess.run(
                        ["pg_isready", "-h", "localhost", "-p", "5432"],
                        capture_output=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=5,
                    )
                    if result.returncode == 0:
                        print("  🟢 PostgreSQL    : 运行中")
                    else:
                        print(f"  🔴 PostgreSQL    : 已停止")
                except Exception:
                    print("  ⚪ PostgreSQL    : 未检测到")

            # 数据库连接测试
            try:
                from peng.db.connection import get_config
                import socket

                config = get_config()
                db_host = config.host
                db_port = config.port
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3)
                result = sock.connect_ex((db_host, db_port))
                sock.close()
                if result == 0:
                    print(f"  🟢 数据库连接    : {db_host}:{db_port} 可达")
                else:
                    print(f"  🔴 数据库连接    : {db_host}:{db_port} 不可达")
            except Exception as e:
                print(f"  ⚪ 数据库连接    : 测试失败 ({e})")

            # nest 服务（通过 webui API 查询peng服务进程中的连接状态）
            try:
                from peng.nest.cli import _is_nest_process_running

                local_nest_running = _is_nest_process_running(name="local")
                any_nest_running = _is_nest_process_running()

                # 尝试通过 webui API 查询实际的 nest 连接状态
                try:
                    import urllib.request
                    import json as _json

                    req = urllib.request.Request("http://localhost:6688/api/nest/list")
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        api_data = _json.loads(resp.read())
                    connections = {
                        k: v for k, v in api_data.get("nests", {}).items() if v.get("connected")
                    }
                    if connections:
                        print(f"  🟢 nest          : 运行中 ({len(connections)}个连接)")
                        for key, info in connections.items():
                            platform = info.get("platform", "?")
                            hostname = info.get("hostname", "?")
                            pid = info.get("pid", "?")
                            workdir = info.get("workdir", "")
                            workdir_str = f", workdir={workdir}" if workdir else ""
                            print(f"    🪹 {key} ({platform}/{hostname}, pid={pid}{workdir_str})")
                    elif any_nest_running:
                        print(f"  🟢 nest          : 运行中 (暂无已注册连接)")
                    else:
                        print(f"  🔴 nest          : 已停止")
                except Exception:
                    # webui API 不可用，回退到进程检测
                    # 优先显示 local nest 状态（用户最关心的），而非外部 nest 进程
                    icon = "🟢" if local_nest_running else "🔴"
                    state = "运行中" if local_nest_running else "已停止"
                    print(f"  {icon} nest          : {state}")
            except Exception as e:
                print(f"  ⚪ nest          : 检测失败 ({e})")

            print()

        return 0

    elif command == "stop":
        setup_logging_from_config()

        # 先写入 nest 停止信号文件，让所有 nest connect 进程主动退出（避免杀不掉 SYSTEM 进程的问题）
        try:
            from peng.nest.server import NestConnector

            NestConnector.create_stop_signal()
        except Exception:
            pass

        # 先停止 peng 服务
        if sys.platform == "win32":
            from peng.service.windows_service import (
                _is_peng_installed,
                stop_peng,
            )

            if _is_peng_installed():
                print("🛑 正在停止🐣服务...")
                stop_peng()

            # 无论是否安装，都强制清理所有 fly service 进程
            from peng.ui.service import kill_all_services

            killed = kill_all_services()
            if killed > 0:
                print(f"🧹 已清理 {killed} 个fly service进程")

            # 清理内置 local nest 子进程
            try:
                from peng.nest.cli import _kill_all_nest_processes

                killed_nest = _kill_all_nest_processes(name="local")
                if killed_nest:
                    print(f"🪹  已清理 {killed_nest} 个内置 local nest 进程")
            except Exception as e:
                print(f"🪹  清理内置 local nest 失败: {e}")

            # 等待 nest connect 进程检测到停止信号文件并主动退出
            import time as _time

            _time.sleep(6)

            # 清理停止信号文件
            try:
                from peng.nest.server import NestConnector

                NestConnector.remove_stop_signal()
            except Exception:
                pass

            # 注意：PostgreSQL 不由 stop/start/restart 管理，只由 install/uninstall 管理

            return 0
        else:
            # Linux: 先通过 systemctl stop 通知 systemd 停止服务
            try:
                from peng.service.systemd_manager import (
                    stop_service as systemd_stop_service,
                    is_service_installed,
                )
                if is_service_installed():
                    success, msg = systemd_stop_service()
                    if success:
                        print(f"{msg}")
                    else:
                        print(f"⚠️ {msg}，将强制清理进程...")
            except Exception as e:
                print(f"⚠️ systemd stop 失败: {e}，将强制清理进程...")

        # 通用停止：清理所有 fly service 进程
        from peng.ui.service import kill_all_services

        # 直接杀死所有fly service进程（包括子进程和安全监控）
        killed = kill_all_services()
        if killed > 0:
            print(f"🧹 已清理 {killed} 个fly service进程")

        # 清理内置 local nest 子进程（peng 服务停止后可能成为孤儿）
        try:
            from peng.nest.cli import _kill_all_nest_processes

            killed_nest = _kill_all_nest_processes(name="local")
            if killed_nest:
                print(f"🪹  已清理 {killed_nest} 个内置 local nest 进程")
        except Exception as e:
            print(f"🪹  清理内置 local nest 失败: {e}")

        # Linux: 杀完进程后，重置 systemd 服务状态防止 Restart=on-failure 自动重启
        if sys.platform != "win32":
            try:
                import subprocess as _sp_reset
                # reset-failed 需要管理员权限（静默失败）
                _sp_reset.run(
                    ["sudo", "systemctl", "reset-failed", "peng.service"],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass

        # 进程已被强制杀死，短暂等待后清理信号文件
        import time as _time
        _time.sleep(1)

        # 清理停止信号文件
        try:
            from peng.nest.server import NestConnector

            NestConnector.remove_stop_signal()
        except Exception:
            pass

        return 0

    elif command == "install":
        setup_logging_from_config()
        if sys.platform == "win32":
            from peng.service.windows_service import (
                _is_peng_installed,
                _is_pg_installed,
                _run_nssm_elevated,
                SERVICE_NAME,
                _get_fly_cmd,
                _cleanup_legacy_registry_autostart,
                _find_pg_ctl,
                _find_pg_data_dir,
                create_peng_vbs,
                create_pg_vbs,
                PENG_TASK_NAME,
                PG_TASK_NAME,
                _find_nssm,
            )

            _cleanup_legacy_registry_autostart()

            # 0. 部署 PostgreSQL（未安装时自动安装：scoop/winget + initdb，无需管理员权限）
            #    与 Linux 分支保持一致：PostgreSQL 由 fly install 负责安装/部署
            try:
                from peng.db.postgres_auto_deploy import (
                    check_postgresql_available,
                    ensure_postgresql_fly,
                )

                if not check_postgresql_available():
                    print("[PostgreSQL] 未检测到可用实例，正在自动部署（scoop/winget）...")
                    sys.stdout.flush()
                    if not ensure_postgresql_fly():
                        print(
                            "[PostgreSQL] ❌ 部署失败，peng 服务可能无法正常工作",
                            file=sys.stderr,
                        )
                    else:
                        print("[PostgreSQL] ✅ 自动部署完成")
                else:
                    print("[PostgreSQL] 已就绪")
            except Exception as e:
                print(f"[PostgreSQL] ⚠️  部署异常（非致命）: {e}")

            # ripgrep 搜索加速（grep 工具检测 rg 存在则优先使用；非致命）
            try:
                from peng.common.ripgrep import ensure_ripgrep

                ensure_ripgrep()
            except Exception as e:
                print(f"[ripgrep] ⚠️  自动安装异常（非致命）: {e}")

            # 1. Create VBS wrappers
            peng_vbs = create_peng_vbs()
            print(f"🐣 VBS wrapper created: {peng_vbs}")

            pg_data_dir = _find_pg_data_dir()
            pg_vbs = None
            if pg_data_dir:
                # Ensure postgresql.conf configured
                try:
                    from peng.db.postgres_auto_deploy import _configure_postgresql_conf

                    _configure_postgresql_conf(Path(pg_data_dir))
                except Exception:
                    pass
                try:
                    pg_vbs = create_pg_vbs(pg_data_dir)
                    print(f"[PostgreSQL] VBS wrapper created: {pg_vbs}")
                except Exception as e:
                    print(f"[PostgreSQL] VBS wrapper 创建失败: {e}")

            # 2. Build UAC commands
            username = os.environ.get("USERNAME", "")
            if not username:
                print("❌ 无法获取用户名")
                return 1

            commands = []

            # Kill old processes
            commands.append("taskkill /F /IM fly.exe 2>NUL")
            commands.append("taskkill /F /IM postgres.exe 2>NUL")
            commands.append("taskkill /F /IM wscript.exe 2>NUL")
            commands.append("timeout /t 2 /nobreak >NUL")

            # Create peng task
            commands.append(f'schtasks /delete /tn "{PENG_TASK_NAME}" /f 2>NUL')
            commands.append(
                f'schtasks /create /tn "{PENG_TASK_NAME}"'
                f' /tr "wscript.exe \\"{peng_vbs}\\""'
                f" /sc onlogon /ru {username} /it /rl LIMITED"
            )

            # Create PG task
            if pg_vbs:
                commands.append(f'schtasks /delete /tn "{PG_TASK_NAME}" /f 2>NUL')
                commands.append(
                    f'schtasks /create /tn "{PG_TASK_NAME}"'
                    f' /tr "wscript.exe \\"{pg_vbs}\\""'
                    f" /sc onlogon /ru {username} /it /rl LIMITED"
                )

            # Start both
            commands.append(f'schtasks /run /tn "{PENG_TASK_NAME}"')
            if pg_vbs:
                commands.append(f'schtasks /run /tn "{PG_TASK_NAME}"')

            cmd_str = " & ".join(commands)
            print(f"\n  安装计划任务: {PENG_TASK_NAME} + {PG_TASK_NAME}")
            print(f"  运行身份: {username} (non-elevated, auto-restart)\n")

            success, msg = _run_nssm_elevated(cmd_str)
            print(f"{msg}")

            # Wait for services to start
            import time
            from peng.db.postgres_auto_deploy import check_postgresql_ready

            print("⏳ 等待服务启动...")
            time.sleep(10)

            # Wait for PG
            if pg_vbs:
                for i in range(30):
                    ok, err = check_postgresql_ready()
                    if ok or "表不存在" in err:
                        print("[PostgreSQL] 已就绪")
                        break
                    time.sleep(2)

            # Wait for peng (port 6688)
            import socket

            for i in range(30):
                s = socket.socket()
                s.settimeout(2)
                r = s.connect_ex(("127.0.0.1", 6688))
                s.close()
                if r == 0:
                    print("🐣 已就绪")
                    break
                time.sleep(2)

            # Firefox GC优化（减少WebUI卡顿）
            try:
                from peng.webui.firefox_optimize import optimize_all_firefox_profiles
                count = optimize_all_firefox_profiles()
                if count > 0:
                    print(f"🦊 Firefox GC优化已应用（{count}个配置文件），重启Firefox生效")
            except Exception as e:
                print(f"🦊 Firefox优化跳过: {e}")

            # Wait for nest
            import urllib.request
            import json

            for i in range(15):
                time.sleep(1)
                try:
                    req = urllib.request.Request("http://localhost:6688/api/nest/list")
                    with urllib.request.urlopen(req, timeout=2) as resp:
                        data = json.loads(resp.read())
                    if "local" in {
                        k: v for k, v in data.get("nests", {}).items() if v.get("connected")
                    }:
                        print("🪹 本地鹏窝(nest) 已就绪")
                        return 0
                except Exception:
                    pass
            return 0 if success else 1
        else:
            # Linux: 安装 systemd 服务
            from peng.service.systemd_manager import install_service as systemd_install_service

            # 先部署 PostgreSQL（fly install 在 _no_db_commands 中，不会自动触发）
            try:
                from peng.db.postgres_auto_deploy import ensure_postgresql_fly

                print("[PostgreSQL] 正在部署...")
                if not ensure_postgresql_fly():
                    print("[PostgreSQL] ❌ 部署失败，peng 服务可能无法正常工作", file=sys.stderr)
            except Exception as e:
                print(f"[PostgreSQL] ⚠️  部署异常（非致命）: {e}")

            # ripgrep 搜索加速（grep 工具检测 rg 存在则优先使用；非致命）
            try:
                from peng.common.ripgrep import ensure_ripgrep

                ensure_ripgrep()
            except Exception as e:
                print(f"[ripgrep] ⚠️  自动安装异常（非致命）: {e}")

            success, msg = systemd_install_service()
            print(msg)

            # Firefox GC优化（减少WebUI卡顿）
            try:
                from peng.webui.firefox_optimize import optimize_all_firefox_profiles
                count = optimize_all_firefox_profiles()
                if count > 0:
                    print(f"🦊 Firefox GC优化已应用（{count}个配置文件），重启Firefox生效")
            except Exception as e:
                print(f"🦊 Firefox优化跳过: {e}")

            # 注意：local nest 不再作为独立服务安装
            # local nest 由 fly service (BackgroundService) 内置启动和监控
            # 当 fly service 启动时会自动启动 local nest，并保证其不可用时立即重启

            return 0 if success else 1

    elif command == "uninstall":
        setup_logging_from_config()

        if sys.platform == "win32":
            from peng.service.windows_service import (
                _is_peng_installed,
                _is_pg_installed,
                _run_nssm_elevated,
                _find_nssm,
                PENG_TASK_NAME,
                PG_TASK_NAME,
                SERVICE_NAME,
            )

            # Stop nest
            try:
                from peng.nest.cli import stop_service_windows, _is_nest_process_running

                if _is_nest_process_running():
                    stop_service_windows()
            except Exception:
                pass

            # Build UAC commands
            commands = [
                f'schtasks /end /tn "{PENG_TASK_NAME}" 2>NUL',
                f'schtasks /end /tn "{PG_TASK_NAME}" 2>NUL',
                "timeout /t 2 /nobreak >NUL",
                "taskkill /F /IM fly.exe 2>NUL",
                "taskkill /F /IM postgres.exe 2>NUL",
                "taskkill /F /IM wscript.exe 2>NUL",
                f'schtasks /delete /tn "{PENG_TASK_NAME}" /f',
                f'schtasks /delete /tn "{PG_TASK_NAME}" /f',
            ]

            cmd_str = " & ".join(commands)
            print("🔐 正在请求管理员权限...")
            success, msg = _run_nssm_elevated(cmd_str)
            if not success:
                print(f"❌ {msg}")
                return 1

            # Wait for cleanup
            import time

            print("⏳ 等待卸载完成...")
            for i in range(15):
                try:
                    r = subprocess.run(
                        ["schtasks", "/query", "/tn", PENG_TASK_NAME],
                        capture_output=True,
                        timeout=3,
                    )
                    peng_gone = r.returncode != 0
                except Exception:
                    peng_gone = True
                if peng_gone:
                    print("✅ 所有服务已卸载")
                    # Stop nest processes
                    try:
                        from peng.nest.cli import _is_nest_process_running, stop_service_windows

                        if _is_nest_process_running():
                            stop_service_windows()
                            print("[ nest ] nest 进程已停止")
                    except Exception:
                        pass
                    return 0
                if i % 5 == 0:
                    sys.stdout.write(".")
                    sys.stdout.flush()
                time.sleep(2)
            print("⚠️ 卸载命令已发送，请用 fly status 确认")
            return 0
        else:
            # Linux: 卸载 systemd 服务
            from peng.service.systemd_manager import uninstall_service as systemd_uninstall_service

            success, msg = systemd_uninstall_service()
            print(msg)

            # 注意：local nest 不再作为独立服务卸载
            # local nest 由 fly service 内置管理，停止 fly service 即停止 local nest

            return 0 if success else 1

    elif command == "rollback":
        setup_logging_from_config()
        # 回退版本
        from peng.service.version_manager import (
            get_available_versions,
            rollback_to_version,
            get_current_version,
        )

        version = args.version

        # 如果没有指定版本，显示可用版本列表
        if not version:
            current_version = get_current_version()
            versions = get_available_versions()

            if not versions:
                print("⚠️  没有可回退的版本")
                return 1

            print("📋 可用版本列表：")
            print()
            for version_name, version_info in versions:
                status = version_info.get("status", "unknown")
                timestamp = version_info.get("timestamp", "")
                current_marker = " <- 当前版本" if version_name == current_version else ""
                print(f"  {version_name}  [{status}]  {timestamp}{current_marker}")
            print()
            print("使用方法：fly rollback <版本名称>")
            print("例如：fly rollback 20260326_203000")
            return 0

        # 执行回退
        success, msg = rollback_to_version(version)
        print(msg)
        return 0 if success else 1

    elif command == "crash-logs":
        setup_logging_from_config()
        # 崩溃日志管理
        from peng.service.crash_logger import (
            get_crash_logs_list,
            get_crash_log_detail,
            print_crash_summary,
            clear_crash_logs,
        )

        # 查看详细崩溃信息
        if args.info:
            crash_log = get_crash_log_detail(args.info)
            if not crash_log:
                print(f"❌ 未找到版本 {args.info} 的崩溃日志")
                return 1

            print()
            print(print_crash_summary(crash_log))
            print()
            print("=" * 80)
            print("完整崩溃日志：")
            print("=" * 80)
            print(json.dumps(crash_log, indent=2, ensure_ascii=False))
            return 0

        # 清理旧崩溃日志
        if args.clear:
            if args.clear <= 0:
                print("❌ 清理天数必须大于0")
                return 1

            count, msg = clear_crash_logs(args.clear)
            print(msg)
            return 0

        # 列出所有崩溃日志
        crash_logs = get_crash_logs_list()
        if not crash_logs:
            print("✅ 没有崩溃日志")
            return 0

        print(f"📋 崩溃日志列表（共 {len(crash_logs)} 条）：")
        print()
        for i, log in enumerate(crash_logs, 1):
            print(f"{i}. {log['file']}")
            print(f"   版本：{log['version']}")
            print(f"   类型：{log['crash_type']}")
            print(f"   时间：{log['timestamp']}")
            print(f"   错误：{log['error_message']}")
            print()

        print("使用方法：")
        print("  fly crash-logs --info <版本>  # 查看详细崩溃信息")
        print("  fly crash-logs --clear <天数>  # 清理多少天前的崩溃日志")
        return 0

    elif command == "git-reset-soft":
        setup_logging_from_config()
        # Git软回退
        from peng.service.git_recovery import git_reset_soft

        success, msg = git_reset_soft(args.commit)
        print(msg)
        return 0 if success else 1

    elif command == "git-reset-mixed":
        setup_logging_from_config()
        # Git混合回退
        from peng.service.git_recovery import git_reset_mixed

        success, msg = git_reset_mixed(args.commit)
        print(msg)
        return 0 if success else 1

    elif command == "git-crash-branch":
        setup_logging_from_config()
        # 恢复崩溃分支
        from peng.service.git_recovery import restore_from_crash_branch

        success, msg = restore_from_crash_branch()
        print(msg)
        return 0 if success else 1

    elif command == "git-branch-list":
        setup_logging_from_config()
        # 列出崩溃分支
        result = subprocess.run(["git", "branch", "-a"], capture_output=True, text=True)
        if result.returncode != 0:
            print("获取分支列表失败")
            return 1

        branches = result.stdout.split("\n")
        crash_branches = [
            b.strip().replace("* ", "").replace("remotes/origin/", "")
            for b in branches
            if b.strip().startswith("crash-") or b.strip().startswith("* crash-")
        ]

        if not crash_branches:
            print("没有崩溃分支")
        else:
            print(f"崩溃分支列表（共 {len(crash_branches)} 个）：")
            for branch in sorted(crash_branches):
                marker = (
                    " * " if branch.startswith("crash-") and "* crash-" in result.stdout else "   "
                )
                print(f"{marker}{branch}")
            print()
            print("使用方法：")
            print("  fly git-crash-branch  # 切换到最新的崩溃分支")
            print("  git checkout <分支名>  # 切换到指定崩溃分支")

        return 0

    elif command == "clear-all-data":
        setup_logging_from_config()
        # 清空所有数据
        return _clear_all_data(args.force)

    else:
        setup_logging_from_config(level="WARNING")
        # 未知命令，默认为 chat
        from peng.ui.chat import run_chat

        return run_chat(argv)

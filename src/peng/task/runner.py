import argparse
import sys
from pathlib import Path

# 添加项目路径到 sys.path
src_path = Path(__file__).parent.parent.parent
sys.path.insert(0, str(src_path))

def main():
    try:
        # 配置日志（避免继承父进程的日志配置）
        import logging
        root_logger = logging.getLogger()
        root_logger.handlers.clear()
        root_logger.setLevel(logging.INFO)

        parser = argparse.ArgumentParser(description="提示词管理工具")
        parser.add_argument("-t", "--task_id", help="task id。", type=int, default=0)
        args = parser.parse_args()

        if args.task_id:
            # Windows 兼容：uv 的 python.exe 是 shim，subprocess.Popen.pid
            # 返回的是 shim 的 PID 而非真实 Python 进程的 PID。
            # 子进程启动后用 os.getpid() 获取真实 PID 并写回数据库。
            import os
            real_pid = os.getpid()
            from peng.db.connection import get_config
            from peng.task.database import TaskDatabase
            # TaskDatabase单例模式，无__init__参数
            db = TaskDatabase()
            db.update_task(args.task_id, process_id=real_pid)
            logging.getLogger(__name__).info(f"[runner] 事务#{args.task_id} 真实PID={real_pid} 已写回数据库")

            from peng.task.executor import run_task_process
            run_task_process(args.task_id)
        else:
            print("需要参数：--task_id <id>")
    except Exception as e:
        # 输出错误到 stderr，让父进程可以看到
        import traceback as tb
        sys.stderr.write(f"事务执行失败: {e}\n")
        sys.stderr.write(f"堆栈信息:\n{tb.format_exc()}\n")
        sys.exit(1)

if __name__ == "__main__":
    sys.exit(main())

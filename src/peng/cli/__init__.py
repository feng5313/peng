"""
CLI 入口模块

提供 msg, task 命令的入口函数。

使用延迟导入避免 bin/task 加载时不必要的 _msg_impl（节省 ~130ms），
以及 bin/msg 加载时不必要的 _task_impl。
"""

__all__ = ["msg_main", "task_main"]


def __getattr__(name: str):
    """模块级延迟导入（PEP 562），仅在访问时加载对应模块。"""
    if name == "msg_main":
        from peng.cli._msg_impl import main as msg_main
        return msg_main
    if name == "task_main":
        from peng.cli._task_impl import main as task_main
        return task_main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

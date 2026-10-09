"""
消息发送者/接收者名称分类辅助（事务#2089）

消息系统中的参与者名称存在多种形态：
- 纯名称：鹏🐣 / 风主🍃（历史消息、WebUI创建）
- 带事务标识：鹏🐣·事务#2089（事务内LLM经 message 工具发送，可追踪来源事务）
- 简短别名：peng / 鹏 / master（db.create_message 的 name_mapping 会归一化）

判断"某名称是否鹏（agent）侧"时**禁止精确比较**（会漏掉带事务标识的形态），
必须使用本模块的 is_agent_side()。历史bug：inbox_monitor 飞书转发用
`sender == AGENT_NAME` 精确比较，事务标识sender被误判为风主侧。
"""

from __future__ import annotations


def is_agent_side(name: str | None) -> bool:
    """判断名称是否属于鹏（agent）侧。

    与 types.py _get_sender_display 的归类规则一致：名称含"鹏"或为
    "peng" 即视为鹏侧（覆盖 鹏🐣 / 鹏🐣·事务#N / peng 等形态）。

    Args:
        name: 发送者/接收者名称

    Returns:
        True 表示鹏（agent）侧
    """
    if not name:
        return False
    return "鹏" in name or name == "peng"


def build_task_sender_tag(task_id: int | None, agent_name: str) -> str:
    """构建带事务标识的发送者名称：鹏🐣·事务#N。

    事务内LLM发送消息时使用，风主可追踪消息来源事务；
    无事务上下文（如服务进程直接发送）时退回纯 agent 名称。

    Args:
        task_id: 调用方事务ID（None则退回纯名称）
        agent_name: agent 名称（如 鹏🐣）

    Returns:
        发送者名称字符串
    """
    if task_id:
        return f"{agent_name}·事务#{task_id}"
    return agent_name

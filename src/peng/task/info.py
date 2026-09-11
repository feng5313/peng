"""
Info 字段处理工具函数

提供 info 字段的解析和状态管理功能，被以下模块共享使用：
- src/peng/llm/tool_engine.py 的 _get_task_info
- src/peng/cli/_task_impl.py 的 cmd_info

master指示：两边调用同样的代码！！不要写两份！
"""

from peng.common import jsonp as json


def parse_info_to_list_with_status(info_field: str) -> list[dict]:
    """
    解析 info 字段为带状态的列表

    支持以下格式：
    - 新格式带状态：[{"content": "信息1", "read": false}, {"content": "信息2", "read": true}]
    - 旧格式字符串列表：["信息1", "信息2"] -> 转为新格式，默认未读
    - 旧格式对象：{"info": ["信息1", "信息2"]} -> 转为新格式，默认未读
    - 纯字符串：直接转为单元素列表，默认未读

    Args:
        info_field: info 字段的原始值

    Returns:
        列表，每个元素为 {"content": str, "read": bool}
    """
    if not info_field or info_field == "[]" or info_field == "null":
        return []

    try:
        parsed = json.loads(info_field)

        if isinstance(parsed, list):
            # 检查是否是新格式（带状态的dict列表）
            if parsed and isinstance(parsed[0], dict) and "content" in parsed[0]:
                # 新格式：[{"content": "xxx", "read": false}, ...]
                return [
                    {"content": str(item.get("content", "")), "read": bool(item.get("read", False))}
                    for item in parsed
                    if item.get("content")
                ]
            else:
                # 旧格式字符串列表：["信息1", "信息2"]
                return [
                    {"content": str(item), "read": False}
                    for item in parsed
                    if item
                ]
        elif isinstance(parsed, dict) and "info" in parsed:
            # 旧格式：{"info": [...]}
            inner = parsed["info"]
            if isinstance(inner, list):
                return [
                    {"content": str(item), "read": False}
                    for item in inner
                    if item
                ]
            elif isinstance(inner, str) and inner:
                return [{"content": inner, "read": False}]
        elif isinstance(parsed, str) and parsed:
            # JSON 解析后是字符串
            return [{"content": parsed, "read": False}]

    except (json.JSONDecodeError, TypeError):
        # 不是合法 JSON，当作纯字符串处理（向后兼容）
        if info_field:
            return [{"content": info_field, "read": False}]

    return []


def get_unread_info_items(info_list_with_status: list[dict]) -> list[dict]:
    """
    从带状态的 info 列表中获取未读项

    Args:
        info_list_with_status: 带状态的 info 列表

    Returns:
        未读项列表
    """
    return [item for item in info_list_with_status if not item["read"]]


def mark_info_as_read(info_list_with_status: list[dict]) -> None:
    """
    将所有未读 info 标记为已读（原地修改）

    Args:
        info_list_with_status: 带状态的 info 列表
    """
    for item in info_list_with_status:
        item["read"] = True


def is_urgent_content(content: str | None) -> bool:
    """
    检测内容是否为紧急指示（以感叹号开头）

    以感叹号（半角 ! 或全角 ！）开头的新回复消息/未读info视为紧急指示：
    - tool_engine 思考流中立刻打断思考（_has_urgent_interrupts）
    - bash 工具运行中立刻终止命令进程（tools/bash.py _interrupt_monitor）
    - 下一轮迭代边界立刻注入处理（_get_unread_reply/_get_task_info）

    被以下模块共享使用（master指示：不要写两份！）：
    - src/peng/llm/tool_engine.py 的 _has_urgent_interrupts
    - src/peng/tools/bash.py 的 _interrupt_monitor

    Args:
        content: 消息/info 内容

    Returns:
        True 表示以感叹号开头（紧急，需打断立刻处理）
    """
    if not content:
        return False
    # 处理存储层的字面 \n 转义与首部空白后，取第一个有效字符判断
    first_char = content.replace("\\n", "\n").lstrip()[:1]
    return first_char in ("!", "！")


def serialize_info_list(info_list_with_status: list[dict]) -> str:
    """
    将带状态的 info 列表序列化为 JSON 字符串

    Args:
        info_list_with_status: 带状态的 info 列表

    Returns:
        JSON 字符串
    """
    return json.dumps(info_list_with_status, ensure_ascii=False)

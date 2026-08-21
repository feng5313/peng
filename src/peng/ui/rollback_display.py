"""回滚超链接显示标注（CLI 纯文本/管道/Terminal 场景）

背景
----
事务输出中的回滚超链接采用 ANSI OSC8 序列：
    \x1b[33m\x1b]8;;rollback:id:<task_id>-<seq>\x07↩️ 回滚   \x1b]8;;\x07\x1b[0m
在 WebUI 中点击可触发回滚、hover 显示回滚ID与命令；但在纯文本/管道/普通终端
（如 ``fly msg/reply/info/watch`` 实时打印、``task output`` 显示）中，OSC8 链接
仅渲染为 "↩️ 回滚" 文本，用户看不到可用的**回滚ID**和**回滚命令行**。

本模块提供统一的行内标注函数：解析行内所有 OSC8 ``rollback:id:<task_id>-<seq>``
超链接，在行尾追加（橙色、去重）：
    [R<seq>] task rollback <task_id> R<seq>
使"回滚信息"同时包含**回滚ID**（R<seq>）与**回滚命令行**
（task rollback <task_id> R<seq>），便于用户直接复制执行。

被以下场景复用，保证各处回滚信息格式一致：
- ``src/peng/ui/message_utils.py``  ``fly msg/reply/info/watch`` 实时输出
- ``src/peng/cli/_task_impl.py``     ``task output`` 行尾标注
"""

from __future__ import annotations

import re

# 匹配 OSC8 回滚超链接 URI：rollback:id:<task_id>-<seq>（持久化ID格式）
# OSC8 格式：ESC ] 8 ; ; <uri> 终止符（BEL=\x07 或 ST=\x1b\）
ROLLBACK_ID_URI_RE = re.compile(r"\x1b\]8;;rollback:id:(\d+-\d+)(?:\x07|\x1b\\)")

# 橙色（非粗体），与 generate_rollback_link 的链接颜色一致
_ORANGE = "\x1b[33m"
_RESET = "\x1b[0m"


def has_rollback_link(line_content: str) -> bool:
    """检测行内是否含 OSC8 回滚超链接。

    先用廉价子串判断短路，避免对绝大多数无链接的行执行正则。
    """
    if "rollback:id:" not in line_content:
        return False
    return bool(ROLLBACK_ID_URI_RE.search(line_content))


def _format_mark(rid: str) -> str:
    """将完整回滚ID "<task_id>-<seq>" 格式化为 "[R<seq>] task rollback <task_id> R<seq>"。"""
    task_id, seq = rid.split("-", 1)
    return f"[R{seq}] task rollback {task_id} R{seq}"


def annotate_rollback_info(line_content: str) -> str:
    """在行尾追加回滚信息标记（橙色），包含**回滚ID**和**回滚命令行**。

    解析行内每个 OSC8 ``rollback:id:<task_id>-<seq>`` 超链接，按出现顺序去重后，
    在行尾追加：``[R<seq>] task rollback <task_id> R<seq>``。

    Args:
        line_content: 原始行内容（可能含 ANSI 序列）。

    Returns:
        追加标记后的行内容；无回滚链接时原样返回。
    """
    if "rollback:id:" not in line_content:
        return line_content

    ids: list[str] = []
    seen: set[str] = set()
    for m in ROLLBACK_ID_URI_RE.finditer(line_content):
        rid = m.group(1)
        if rid not in seen:
            seen.add(rid)
            ids.append(rid)
    if not ids:
        return line_content

    marks = " ".join(_format_mark(rid) for rid in ids)
    return f"{line_content} {_ORANGE}{marks}{_RESET}"


# 向后兼容别名（历史调用方使用 _annotate_rollback_ids 名称）
def _annotate_rollback_ids(line_content: str, line_num: int = 0) -> str:
    """兼容旧签名 ``(line_content, line_num)``；line_num 未使用，保留扩展。"""
    return annotate_rollback_info(line_content)

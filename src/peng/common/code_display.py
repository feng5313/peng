"""
代码内容显示格式化

将工具调用（write/edit）参数中的文件内容格式化为人类易读的多行文本，
并根据文件扩展名使用 pygments 做 ANSI 终端语法高亮。

设计约束（与 output_manager 行拆分机制配合）：
1. 每行 ANSI 状态自包含：pygments TerminalFormatter 的每个 token 都带
   重置序列（\\x1b[39;49;00m），多行字符串等跨行 token 每行行首都会重新
   输出颜色序列，因此按 \\n 拆行后各行颜色独立、不会跨行泄漏。
2. 只含 ANSI 序列残留的空行清洗为纯空行，配合 output_manager 的
   连续空行压缩与空格占位机制。
3. 输出中不含裸 \\t（展开为 4 空格），避免 TSV 行格式与表格显示错位。
4. 行数截断：超长内容只显示前 MAX_DISPLAY_LINES 行并提示省略行数，
   避免刷屏与输出文件膨胀。

显示端兼容性：
- CLI（fly watch/msg/reply/task output）print 透传 ANSI。
- WebUI（websocket → xterm.js）原生渲染 ANSI。
- TerminalFormatter 输出标准 16 色 SGR 序列，所有终端与 xterm.js 均支持。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 单个代码块最大显示行数（超出截断）
MAX_DISPLAY_LINES = 80

# 单个 edit oldString/newString 各自的最大显示行数
MAX_EDIT_SIDES_LINES = 40

_ANSI_RESET = "\x1b[0m"
_ANSI_DIM = "\x1b[90m"  # bright black，用于分隔线与提示文字

# 分隔线宽度（视觉上区分代码块边界）
_SEP_WIDTH = 64


def _strip_ansi(line: str) -> str:
    """去除行内 ANSI SGR 序列（用于检测纯色残留空行）"""
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", line)


def _human_size(num_bytes: int) -> str:
    """字节数人类可读化"""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / 1024 / 1024:.1f} MB"


def _separator() -> str:
    """灰色分隔线"""
    return f"{_ANSI_DIM}{'─' * _SEP_WIDTH}{_ANSI_RESET}"


def _get_lexer(file_path: str):
    """根据文件路径（扩展名）获取 pygments lexer；未识别返回 None"""
    if not file_path:
        return None
    try:
        from pygments.lexers import get_lexer_for_filename
        from pygments.util import ClassNotFound

        import os

        basename = os.path.basename(file_path)
        try:
            return get_lexer_for_filename(basename, stripnl=False, ensurenl=False)
        except ClassNotFound:
            # 尝试用扩展名再试一次（处理带双扩展名等场景）
            _, ext = os.path.splitext(basename)
            if ext:
                return get_lexer_for_filename("x" + ext, stripnl=False, ensurenl=False)
            return None
    except Exception as e:
        logger.debug(f"获取 lexer 失败 ({file_path}): {e}")
        return None


def _highlight_lines(lexer, text: str, max_lines: int) -> tuple[list[str], int]:
    """共用高亮底层：整体高亮后拆行（保留跨行 token 状态），清洗空行

    Returns:
        (清洗后的显示行列表, 省略行数)
    """
    all_lines = text.split("\n")
    # 去掉末尾因尾随换行产生的空串（highlight 会自己补）
    if all_lines and all_lines[-1] == "":
        all_lines = all_lines[:-1]

    total = len(all_lines)
    omitted = max(0, total - max_lines)
    src_text = "\n".join(all_lines[:max_lines])

    if lexer is not None:
        try:
            from pygments import highlight
            from pygments.formatters.terminal import TerminalFormatter

            raw = highlight(src_text, lexer, TerminalFormatter())
            lines = raw.split("\n")
            # highlight 输出末尾总带一个换行产生的空串
            if lines and lines[-1] == "":
                lines = lines[:-1]
        except Exception as e:
            logger.debug(f"语法高亮失败: {e}")
            lines = all_lines[:max_lines]
    else:
        lines = all_lines[:max_lines]

    # 清洗：只含 ANSI 序列残留的行置为纯空行（配合空行压缩机制）
    cleaned: list[str] = []
    for ln in lines:
        if ln and not _strip_ansi(ln).strip():
            cleaned.append("")
        else:
            cleaned.append(ln)

    return cleaned, omitted


def highlight_code(
    file_path: str, content: str, max_lines: int = MAX_DISPLAY_LINES
) -> tuple[list[str], str, int]:
    """
    按文件扩展名对内容做 ANSI 语法高亮。

    Args:
        file_path: 文件路径（用于推断语言）
        content: 文件内容
        max_lines: 最大显示行数，超出部分截断

    Returns:
        (显示行列表, 语言名或"文本", 省略的行数)
    """
    if not content:
        return [], "空内容", 0

    # \r\n 统一为 \n；\t 展开为 4 空格（避免 TSV 行格式与显示错位）
    text = content.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")

    lexer = _get_lexer(file_path)
    lang = "文本"
    if lexer is not None:
        lang = getattr(lexer, "name", None) or "文本"

    lines, omitted = _highlight_lines(lexer, text, max_lines)
    return lines, lang, omitted


def format_markdown_display(content: str, max_lines: int = MAX_DISPLAY_LINES) -> str:
    """
    格式化 markdown 内容为人类易读的 ANSI 高亮多行文本。

    用于 report/checklist 等以 markdown 文本为内容的工具调用参数：
        （Markdown · 42 行 · 1.2 KB）
        ────────────────────────────────────────
        （markdown 高亮内容：标题/粗体/列表/代码块）
        ────────────────────────────────────────

    Returns:
        多行文本（无尾部换行）
    """
    if not content:
        return "（Markdown · 空内容）"

    text = content.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    total = len(text.splitlines())
    size = _human_size(len(content.encode("utf-8")))

    lang = "Markdown"
    try:
        from pygments.lexers.markup import MarkdownLexer

        lexer = MarkdownLexer()
    except Exception as e:
        logger.debug(f"MarkdownLexer 不可用: {e}")
        lexer = None
        lang = "文本"

    lines, omitted = _highlight_lines(lexer, text, max_lines)

    header = f"{lang} · {total} 行 · {size}"
    parts = [header, _separator()]
    parts.extend(lines)
    if omitted:
        parts.append(f"{_ANSI_DIM}… 已省略后续 {omitted} 行{_ANSI_RESET}")
    parts.append(_separator())
    return "\n".join(parts)



def _ellipsis_note(omitted: int) -> str:
    """截断提示行"""
    return f"{_ANSI_DIM}… 已省略后续 {omitted} 行（完整内容以文件为准）{_ANSI_RESET}"


def format_write_display(file_path: str, content: str) -> str:
    """
    格式化 write 工具调用参数为人类易读的多行文本。

    格式：
        /path/to/file.py（Python · 42 行 · 1.2 KB）
        ────────────────────────────────────────
        （语法高亮的代码内容）
        ────────────────────────────────────────

    Returns:
        多行文本（无尾部换行）
    """
    lines, lang, omitted = highlight_code(file_path, content)
    total = len(content.replace("\r\n", "\n").split("\n"))
    if total > 1 and content.replace("\r\n", "\n").endswith("\n"):
        total -= 1
    size = _human_size(len(content.encode("utf-8")))

    header = f"{file_path or '(未指定路径)'}（{lang} · {total} 行 · {size}）"
    parts = [header, _separator()]
    parts.extend(lines)
    if omitted:
        parts.append(_ellipsis_note(omitted))
    parts.append(_separator())
    return "\n".join(parts)


def format_line_edit_display(
    file_path: str,
    arguments: dict,
    max_lines: int = MAX_EDIT_SIDES_LINES,
) -> str:
    """
    格式化 line_edit 工具调用参数为人类易读的多行文本。

    格式（以 insert 为例）：
        /path/to/file.py（Python · insert · 行 42 后 · 3 行）
        ─── 插入内容（+）────────────────────
        + （content 高亮内容）
        ────────────────────────────────────────

    Args:
        file_path: 文件路径
        arguments: line_edit 工具完整参数（filePath/operation/line/content/startLine/endLine）
        max_lines: content 最大显示行数

    Returns:
        多行文本（无尾部换行）
    """
    operation = arguments.get("operation", "?") or "?"
    content = arguments.get("content") or ""
    lines, lang, omitted = highlight_code(file_path, content, max_lines)

    # 位置信息：insert/replace/insert 有 line；delete/replace 有 startLine/endLine
    pos_parts: list[str] = []
    if arguments.get("line") is not None:
        pos_parts.append(f"行 {arguments['line']} 附近")
    if arguments.get("startLine") is not None:
        end_line = arguments.get("endLine")
        pos_parts.append(
            f"行 {arguments['startLine']}-{end_line}" if end_line else f"行 {arguments['startLine']}"
        )

    op_names = {
        "delete": "删除",
        "replace": "替换",
        "insert": "插入",
    }
    op_label = op_names.get(operation, operation)
    pos_label = f" · {' · '.join(pos_parts)}" if pos_parts else ""

    content_count = len(content.replace("\r\n", "\n").split("\n")) if content else 0
    if content:
        header = (
            f"{file_path or '(未指定路径)'}（{lang} · {op_label}{pos_label}"
            f" · {content_count} 行）"
        )
    else:
        # 无 content 的操作（如 delete）：不显示语言与空内容标签
        header = f"{file_path or '(未指定路径)'}（{op_label}{pos_label}）"

    parts = [header]
    if content:
        sep_content = (
            f"{_ANSI_DIM}{'─' * 8} 插入内容（+）{'─' * (_SEP_WIDTH - 16)}{_ANSI_RESET}"
        )
        parts.append(sep_content)
        parts.extend(f"+ {ln}" if ln else "+" for ln in lines)
        if omitted:
            parts.append(_ellipsis_note(omitted))
    parts.append(_separator())
    return "\n".join(parts)


def format_edit_display(file_path: str, old_string: str, new_string: str) -> str:
    """
    格式化 edit 工具调用参数为 unified diff 形式（复用 generate_colored_diff）。

    格式：
        /path/to/file.py（Python · 替换 12 行 → 15 行）
        @@ -10,4 +10,6 @@
         （上下文行，暗淡）
        -（删除行，红色）
        +（新增行，绿色）

    与 write 工具结果（📤 工具调用结果）中的 diff 显示视觉一致。

    Returns:
        多行文本（无尾部换行）
    """
    old_norm = (
        (old_string or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    )
    new_norm = (
        (new_string or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    )

    # 语言识别（header 标注用）
    lexer = _get_lexer(file_path)
    lang = (getattr(lexer, "name", None) or "文本") if lexer else "文本"

    old_count = len(old_norm.splitlines())
    new_count = len(new_norm.splitlines())
    header = f"{file_path or '(未指定路径)'}（{lang} · 替换 {old_count} 行 → {new_count} 行）"

    if old_string == new_string:
        return f"{header}\n{_ANSI_DIM}（oldString 与 newString 相同，无差异）{_ANSI_RESET}"

    # 复用项目现有 unified diff 着色实现（-红 +绿 @@青 上下文暗淡）
    from peng.common.utils import generate_colored_diff

    diff = generate_colored_diff(old_norm, new_norm, file_path or "file")

    # 过滤 ---/+++ 头部行（与 header 中的路径信息重复），保留 @@ hunk 定位行
    diff_lines = [
        ln
        for ln in diff.split("\n")
        if not _strip_ansi(ln).startswith("---") and not _strip_ansi(ln).startswith("+++")
    ]

    # 去掉尾部空行
    while diff_lines and not diff_lines[-1].strip():
        diff_lines.pop()

    parts = [header]
    if not diff_lines:
        parts.append(f"{_ANSI_DIM}（无差异）{_ANSI_RESET}")
        return "\n".join(parts)

    # 行数截断保护（generate_colored_diff 每行独立着色闭合，行级截断安全）
    if len(diff_lines) > MAX_DISPLAY_LINES:
        omitted = len(diff_lines) - MAX_DISPLAY_LINES
        parts.extend(diff_lines[:MAX_DISPLAY_LINES])
        parts.append(_ellipsis_note(omitted))
    else:
        parts.extend(diff_lines)
    return "\n".join(parts)


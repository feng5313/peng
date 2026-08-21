"""
line_edit 重复内容检测

背景(#1558事故分析)：大模型在长上下文/连续行编辑时，构造的 content 参数
常出现两类错误（工具忠实执行导致文件出现重复行）：
1. 边界重复：content 的首/末行与编辑区外紧邻的旧行相同
   （例：replace 2003-2008 的 content 末行 == 原2009行 → 2009行内容出现两遍）
2. 内部重复：大段代码搬迁时 content 内部残留重复行
   （例：函数签名行写两遍）

本模块提供检测函数，在 line_edit 执行成功后返回告警文本（不阻断编辑），
帮助大模型立即发现并修正，避免"修一个重复、造一个新重复"的循环。

被两处共用：
- peng/nest/local.py LocalExecutor._handle_line_edit（nest服务端）
- peng/tools/file.py LineEditTool._execute_local（客户端本地fallback）
"""

from __future__ import annotations

# strip 后长度不超过该值的行视为"平凡行"（空行/大括号/分号等），
# 这类行在源码中天然连续重复出现，告警只会产生噪音
_TRIVIAL_MAX_LEN = 2


def _is_trivial(line: str | None) -> bool:
    """判断是否为平凡行（空行、单独的 { } }; 等）"""
    if line is None:
        return True
    return len(line.strip()) <= _TRIVIAL_MAX_LEN


def _same(a: str | None, b: str | None) -> bool:
    """两行是否相同（strip 后比较，忽略缩进差异；空白差异属正常格式调整）。
    平凡行（空行/{/}/;）天然连续重复，不参与重复判定。"""
    if a is None or b is None:
        return False
    if _is_trivial(a) or _is_trivial(b):
        return False
    return a.strip() == b.strip()


def detect_line_edit_duplication(
    lines_before: list[str],
    operation: str,
    start_line: int | None,
    end_line: int | None,
    insert_line: int | None,
    affected_lines: list[str],
    position: str = "after",
) -> str | None:
    """检测 line_edit 编辑是否疑似产生重复内容。

    编辑已在内存中按参数完成（调用方用编辑前的 lines_before 计算邻接行），
    本函数只读不改，返回告警文本或 None。

    Args:
        lines_before: 编辑前的文件行列表
        operation: "delete" | "replace" | "insert"
        start_line/end_line: replace/delete 的行号（1-based，含）
        insert_line: insert 的目标行号（1-based）
        affected_lines: 新内容行列表（replace/insert；delete 为空）

    Returns:
        告警文本（多行）或 None（无疑似重复）
    """
    warnings: list[str] = []

    if operation == "replace" and start_line and end_line and affected_lines:
        # ── 检测1: 新内容与被替换原内容完全相同（疑似行号认知错误的无意义编辑）──
        old_slice = lines_before[start_line - 1 : end_line]
        if old_slice == affected_lines:
            warnings.append(
                f"⚠️ 疑似无变化编辑：新内容与第 {start_line}-{end_line} 行原内容完全相同。"
                f"若非故意，请核实行号是否正确（可能想编辑的是其他行）。"
            )

        # ── 检测2: 边界重复 ──
        # 首行 vs 编辑区上一行（原 start_line-1）
        if start_line >= 2:
            prev = lines_before[start_line - 2]
            if _same(affected_lines[0], prev):
                warnings.append(
                    f"⚠️ 疑似重复插入：新内容第1行与编辑区上方原第 {start_line - 1} 行相同：\n"
                    f"    {affected_lines[0].strip()[:80]}\n"
                    f"    编辑后该行内容将出现两遍，请检查 content 首行是否多带了旧行。"
                )
        # 末行 vs 编辑区下一行（原 end_line+1）
        if end_line < len(lines_before):
            nxt = lines_before[end_line]  # 0-based: 第 end_line+1 行
            if _same(affected_lines[-1], nxt):
                warnings.append(
                    f"⚠️ 疑似重复插入：新内容末行与编辑区下方原第 {end_line + 1} 行相同：\n"
                    f"    {affected_lines[-1].strip()[:80]}\n"
                    f"    编辑后该行内容将出现两遍，请检查 content 末行是否多带了旧行。"
                )

    elif operation == "insert" and insert_line is not None and affected_lines:
        # insert: position="after"时插到原 insert_line 行后，"before"时插到原 insert_line 行前
        # after: 插入内容首行上方邻行=原第 insert_line 行, 末行下方邻行=原第 insert_line+1 行
        # before: 插入内容首行上方邻行=原第 insert_line-1 行, 末行下方邻行=原第 insert_line 行
        prev_no = insert_line if position != "before" else insert_line - 1
        if prev_no >= 1 and prev_no <= len(lines_before):
            prev = lines_before[prev_no - 1]
            if _same(affected_lines[0], prev):
                warnings.append(
                    f"⚠️ 疑似重复插入：插入内容第1行与插入点上方原第 {prev_no} 行相同：\n"
                    f"    {affected_lines[0].strip()[:80]}\n"
                    f"    编辑后该行内容将出现两遍，请检查 content 是否多带了旧行。"
                )
        nxt_no = insert_line + 1 if position != "before" else insert_line
        if nxt_no >= 1 and nxt_no <= len(lines_before):
            nxt = lines_before[nxt_no - 1]
            if _same(affected_lines[-1], nxt):
                warnings.append(
                    f"⚠️ 疑似重复插入：插入内容末行与插入点下方原第 {nxt_no} 行相同：\n"
                    f"    {affected_lines[-1].strip()[:80]}\n"
                    f"    编辑后该行内容将出现两遍，请检查 content 是否多带了旧行。"
                )

    if operation in ("replace", "insert") and affected_lines:
        # ── 检测3: 新内容内部连续重复行（代码搬迁残留，如函数签名写两遍）──
        _detect_internal_repetition(affected_lines, warnings)

    return "\n".join(warnings) if warnings else None


def _detect_internal_repetition(affected_lines: list[str], warnings: list[str]) -> None:
    """检测新内容内部连续出现的重复非平凡行"""
    dup_runs: list[tuple[int, str]] = []  # (起始行号1-based, 行内容)
    i = 0
    n = len(affected_lines)
    while i < n - 1:
        cur, nxt = affected_lines[i], affected_lines[i + 1]
        if _same(cur, nxt):
            dup_runs.append((i + 1, cur))
            # 跳过整个重复段
            j = i + 1
            while j < n and _same(cur, affected_lines[j]):
                j += 1
            i = j
        else:
            i += 1

    if dup_runs:
        detail = "；".join(
            f"第{k}行起「{text.strip()[:60]}」连续重复" for k, text in dup_runs[:3]
        )
        if len(dup_runs) > 3:
            detail += f"等{len(dup_runs)}处"
        warnings.append(
            f"⚠️ 疑似内容残留：新内容内部存在连续重复行（{detail}）。"
            f"若为代码搬迁请检查是否残留了旧副本。"
        )

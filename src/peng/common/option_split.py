"""
子命令文本的「内容 + 尾部选项」分离工具（事务#1998）

LLM 通过 task/msg 工具、以及 WebUI 的 /task、/msg 命令，都以**单个字符串**
传入子命令参数，形如 "subtask 1 需求描述 --workdir /x --prompt coder"。
执行侧需要把内容经临时文件（bin/task|msg 的 --file）安全传递（避免内容被
argparse 当作选项、避免转义问题），同时把选项原样交给 CLI 解析。
若不分离，选项会被当成内容文本写进临时文件，导致 --workdir/--prompt/--creator/
--small-model 等参数被静默忽略（并污染内容）。

本模块提供该分离逻辑，供 tools/task.py、tools/message.py、webui/routes/system.py
共用，保证三处行为一致。
"""

from __future__ import annotations

import re
import shlex

# 值型选项的「取值形态」校验规则（事务#1998）：
# 选项与内容共用一个纯文本通道，存在天然歧义——正文中可能出现
# "用 --workdir 参数指定子事务的工作目录" 这类文本，若仅按位置判断
# 会把 "--workdir 参数…" 误当成选项，导致内容被截断、workdir 被设为乱码。
# 因此对取值有明确形态的选项做校验（不匹配则整体不作为选项处理）。
#   - 路径类（--workdir/-w/--file）：取值需含路径分隔符/盘符/家目录/相对路径
#   - 提示词类（--prompt/-p）：取值为 ASCII 标识符形态（如 coder、task-master）
_PATH_SHAPE = re.compile(r"[/\\]|^[A-Za-z]:|^~|^\.{1,2}$")
_SLUG_SHAPE = re.compile(r"^[A-Za-z0-9_.\-]+$")

VALUE_SHAPE_RULES: dict[str, re.Pattern[str]] = {
    "--workdir": _PATH_SHAPE,
    "-w": _PATH_SHAPE,
    "--file": _PATH_SHAPE,
    "--prompt": _SLUG_SHAPE,
    "-p": _SLUG_SHAPE,
}


def _value_matches(flag: str, value: str) -> bool:
    """校验取值是否符合该 flag 的形态要求（无规则时一律通过）。"""
    rule = VALUE_SHAPE_RULES.get(flag)
    if rule is None or not value:
        return True
    return rule.search(value) is not None


def _strip_wrapping_quotes(token: str) -> str:
    """去除 token 两端成对的包裹引号（shlex 非 posix 模式会保留引号）。"""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def _match_flag(
    token: str, exact_flags: set[str], attached_flags: set[str] | None = None
) -> str | None:
    """返回 token 命中的 flag，未命中返回 None。

    Args:
        token: 待检测 token
        exact_flags: 精确匹配的 flag 集合（布尔flag与值型flag）
        attached_flags: 允许「--flag=value / -wvalue」粘连形式的 flag 集合
            （仅值型flag允许粘连，布尔flag如 -d/--small-model 不参与）
    """
    if not token.startswith("-") or token == "-":
        return None
    if token in exact_flags:
        return token
    for f in attached_flags or ():
        if f.startswith("--"):
            if token.startswith(f + "="):
                return f
        elif len(f) == 2 and token.startswith(f) and len(token) > len(f):
            return f  # -wvalue 粘连形式
    return None



def split_trailing_options(
    subcmd_args: str,
    value_flags: set[str] | tuple[str, ...] = (),
    bool_flags: set[str] | tuple[str, ...] = (),
) -> tuple[str, list[str]]:
    """从子命令参数文本中分离「内容」与位于尾部的「命令行选项」。

    分离策略为「尾部锚定」反向扫描：从末尾向前，只要 token 属于选项
    （布尔flag、值型flag、--flag=value / -wvalue 粘连形式、以及值型flag的取值
    token）就继续纳入选项块，遇到第一个不符合选项特征的 token 即停止。
    该策略与工具文档约定（选项写在参数末尾）一致，且保证：
      - 内容中间出现的 "--workdir" 等选项样式文本不会被误判（其后仍有普通词）；
      - 内容中的普通 "-" 开头词（如 "修复-auth问题"）不会误判（不在已知flag集合中）；
      - 正文尾部形似选项的文本（如 "用 --workdir 指定子事务的工作目录"）不会误判
        ——靠 VALUE_SHAPE_RULES 对取值做形态校验（路径类flag要求路径形态），
        校验不过则停止扫描、该部分保留在内容中，绝不静默截断内容。

    使用 shlex(posix=False) 分词以保留 Windows 路径反斜杠与引号原样，并通过
    instream.tell() 跟踪 token 结束位置，使内容按原文切片返回（保留多行/换行）。

    Args:
        subcmd_args: 子命令参数部分（已去掉子命令本身），如 "1 需求 --workdir /x"
        value_flags: 需要跟随一个值的选项flag集合，如 {"--workdir", "-w", "--file"}
        bool_flags: 独立出现的布尔选项flag集合，如 {"--small-model", "-d"}

    Returns:
        (content, options)
        - content: 内容字符串（原文切片，两端已strip，可能为空）
        - options: 尾部选项token列表（flag及取值按原顺序，已去除包裹引号）

    Examples:
        >>> split_trailing_options("1 需求 --workdir /x", {"--workdir"})
        ('1 需求', ['--workdir', '/x'])
        >>> split_trailing_options("1 修复-auth问题", {"--workdir"})
        ('1 修复-auth问题', [])
        >>> split_trailing_options("1 用 --workdir 参数指定工作目录", {"--workdir"})
        ('1 用 --workdir 参数指定工作目录', [])
    """
    text = subcmd_args or ""
    if not text:
        return "", []

    value_set = set(value_flags)
    bool_set = set(bool_flags)
    known = value_set | bool_set

    # 非 posix 模式：保留反斜杠（Windows路径）与引号原样；whitespace_split 按空白切分
    lex = shlex.shlex(text, posix=False)
    lex.whitespace_split = True
    lex.commenters = ""
    tokens: list[tuple[str, int]] = []  # (token, token 在原文中的结束位置)
    while True:
        tok = lex.get_token()
        if tok is lex.eof:
            break
        tokens.append((tok, lex.instream.tell()))

    if not tokens:
        return text, []

    # 反向扫描识别尾部选项块。argv中flag紧跟其取值（--workdir /x），反向遍历时
    # 先遇到取值(token i)，再遇到flag(token i-1)，故取值需校验其**左邻**为值型flag，
    # 且取值需符合该flag的形态规则（否则视为正文内容，避免误截断内容）。
    # 注意：flag 匹配只认**未加引号**的 token——被引号包裹的 "--workdir" 是正文文本。
    cut = len(tokens)  # 选项块起始token下标；==len(tokens) 表示未识别到任何尾部选项
    i = len(tokens) - 1
    while i >= 0:
        tok = tokens[i][0]
        if tok.startswith("-"):
            flag = _match_flag(tok, known, value_set)
            if not flag:
                break  # 未知 '-' 开头token（可能是内容的一部分）→ 停止
            # --flag=value / -wvalue 粘连形式：内嵌取值也需通过形态校验
            if flag != tok:
                inline_value = (
                    tok[len(flag) + 1 :] if tok.startswith(flag + "=") else tok[len(flag) :]
                )
                if not _value_matches(flag, _strip_wrapping_quotes(inline_value)):
                    break
            cut = i
            i -= 1
            continue
        # 非 '-' 开头token：仅当其左邻是值型flag时，才视为该flag的取值
        if i - 1 >= 0 and tokens[i - 1][0] in value_set:
            if _value_matches(tokens[i - 1][0], _strip_wrapping_quotes(tok)):
                cut = i - 1
                i -= 2
                continue
        break

    if cut >= len(tokens):
        return text, []

    # 内容取原文切片（保留引号/多行），选项token去除包裹引号后返回
    content = text[: tokens[cut - 1][1]].rstrip() if cut > 0 else ""
    options = [_strip_wrapping_quotes(tok) for tok, _ in tokens[cut:]]
    return content, options


def find_unparsed_option_flags(text: str, flags: set[str] | tuple[str, ...]) -> list[str]:
    """检测文本中出现的、疑似命令行选项但未被识别为尾部选项的 flag。

    用于把「选项写错位置」这类静默失败转化为对调用方的显式提示（事务#1998：
    选项未写在参数末尾时会被当作内容的一部分，导致参数被静默忽略）。

    Args:
        text: 已确认为内容部分的文本
        flags: 已知选项 flag 集合（如 ("--workdir","-w","--small-model")）

    Returns:
        命中的 flag 文本列表（按出现顺序，去重）
    """
    flag_set = set(flags or ())
    if not text or not flag_set:
        return []

    try:
        tokens = shlex.split(text, posix=False)
    except ValueError:
        tokens = text.split()

    found: list[str] = []
    for tok in tokens:
        matched = _match_flag(tok, flag_set, flag_set)
        if matched and tok not in found:
            found.append(tok)
    return found

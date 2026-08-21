import logging
import os
import re

logger = logging.getLogger(__name__)

# shell 配置文件候选列表（按优先级排序）
_SHELL_RC_FILES = [
    os.path.expanduser("~/.zshrc"),
    os.path.expanduser("~/.bashrc"),
]

# export 语句正则：export KEY=VALUE 或 export KEY="VALUE" 或 export KEY='VALUE'
_EXPORT_RE = re.compile(
    r"""^export\s+([A-Za-z_][A-Za-z0-9_]*)=(?:"((?:[^"\\]|\\.)*)"|'([^']*)'|([^#\s]*))"""
)

# 变量引用正则：$VAR 或 ${VAR}
_VAR_REF_RE = re.compile(r"""\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)""")


def _expand_vars(value: str, env: dict[str, str]) -> str:
    """展开字符串中的变量引用（$VAR / ${VAR}），使用 env 中的值"""

    def _replacer(m):
        var_name = m.group(1) or m.group(2)
        if var_name and var_name in env:
            return env[var_name]
        return m.group(0)  # 无法展开则保留原文

    return _VAR_REF_RE.sub(_replacer, value)


def get_shell_env() -> dict[str, str]:
    """
    从 shell 配置文件（~/.zshrc 优先，回退 ~/.bashrc）中自动搜集所有 export 的环境变量。

    解析规则：
    - 提取所有 `export KEY=VALUE` 语句
    - 支持 VALUE 中引用已有变量（$VAR / ${VAR}）
    - 按 shell 文件中的出现顺序逐步展开（模拟 shell 行为）
    - 同名变量以最后出现的为准
    - 跳过无法解析的行（不中断）

    Returns:
        从 shell 配置文件中搜集到的环境变量 dict
    """
    env: dict[str, str] = {}

    for rc_path in _SHELL_RC_FILES:
        if not os.path.isfile(rc_path):
            continue

        try:
            with open(rc_path, "r", encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue

                    m = _EXPORT_RE.match(line)
                    if not m:
                        continue

                    key = m.group(1)
                    # VALUE 可能在 group(2)(双引号)、group(3)(单引号) 或 group(4)(无引号)
                    value = (
                        m.group(2)
                        if m.group(2) is not None
                        else (m.group(3) if m.group(3) is not None else m.group(4))
                    )

                    if value is None:
                        value = ""

                    # 展开 ~ 为 $HOME
                    value = value.replace("~", env.get("HOME", os.environ.get("HOME", "")))

                    # 展开变量引用（使用当前已搜集的 env + 系统环境作为基础）
                    expand_base = {**os.environ, **env}
                    value = _expand_vars(value, expand_base)

                    env[key] = value

            logger.debug(f"[get_shell_env] 从 {rc_path} 搜集到 {len(env)} 个环境变量")
        except Exception as e:
            logger.warning(f"[get_shell_env] 解析 {rc_path} 失败: {e}")

        # 只使用第一个找到的 shell 配置文件
        break

    return env


def get_merged_env() -> dict[str, str]:
    """
    获取合并后的环境变量：系统环境 + shell 配置文件环境（shell 覆盖系统）。

    用于传递给子进程，确保子进程能找到 PATH 中注册的所有工具。

    Returns:
        合并后的完整环境变量 dict
    """
    merged = os.environ.copy()
    shell_env = get_shell_env()
    for key, value in shell_env.items():
        merged[key] = value
    return merged


def get_text_from_args(args: str):
    return_tag = "_$换行转义符$_"
    return args.replace("\\\\n", return_tag).replace("\\n", "\n").replace(return_tag, "\\n")


def fix_markdown_linebreaks(text: str) -> str:
    """预处理文本，确保 Markdown 渲染时保留换行。

    Rich 的 Markdown 渲染器遵循标准 Markdown 规范，
    将连续的非列表行合并为同一个段落（软换行→空格）。
    本函数为每个非空行添加两个尾随空格（Markdown 硬换行），
    确保每行在渲染后仍然独立显示。

    跳过：空行、标题行（#）、代码块内的行。
    """
    lines = text.split("\n")
    result = []
    in_code_block = False
    for line in lines:
        stripped = line.strip()
        # 代码块开关检测
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            result.append(line)
            continue
        # 代码块内不处理
        if in_code_block:
            result.append(line)
            continue
        # 空行不处理
        if not stripped:
            result.append(line)
            continue
        # 标题行不处理（# 开头）
        if stripped.startswith("#"):
            result.append(line)
            continue
        # 已经有两个以上尾随空格的行不处理
        if line.endswith("  "):
            result.append(line)
            continue
        # 其他所有行：添加两个尾随空格（Markdown 硬换行）
        result.append(line + "  ")
    return "\n".join(result)


def generate_colored_diff(old_content: str, new_content: str, file_path: str = "file") -> str:
    from difflib import unified_diff
    from peng.common.colors import get_file_colors

    class Colors:
        """ANSI颜色代码（从统一颜色配置获取）"""

        _colors = get_file_colors()
        RED = _colors.get("ERROR", "")
        GREEN = _colors.get("SUCCESS", "")
        YELLOW = _colors.get("WARNING", "")
        BLUE = _colors.get("INFO", "")
        CYAN = _colors.get("HIGHLIGHT", "")
        BOLD = _colors.get("BOLD", "")
        DIM = _colors.get("DIM", "")
        RESET = _colors.get("RESET", "")

    """生成带颜色的diff输出"""
    old_lines = old_content.splitlines(keepends=False)
    new_lines = new_content.splitlines(keepends=False)

    diff_lines = list(
        unified_diff(
            old_lines,
            new_lines,
            fromfile=f"{file_path}",
            tofile=f"{file_path}",
            lineterm="",
        )
    )

    if not diff_lines:
        return ""

    colored_lines = []
    for line in diff_lines:
        if line.startswith("@@"):
            colored_lines.append(f"{Colors.CYAN}{line}{Colors.RESET}")
        elif line.startswith("---") or line.startswith("+++"):
            colored_lines.append(f"{Colors.YELLOW}{line}{Colors.RESET}")
        elif line.startswith("-") and not line.startswith("---"):
            colored_lines.append(f"{Colors.RED}{line}{Colors.RESET}")
        elif line.startswith("+") and not line.startswith("+++"):
            colored_lines.append(f"{Colors.GREEN}{line}{Colors.RESET}")
        else:
            colored_lines.append(f"{Colors.DIM}{line}{Colors.RESET}")

    return "\n".join(colored_lines)

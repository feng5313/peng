"""
文件工具

完全照搬 opencode 的文件读取/写入/编辑功能。

源码位置：
- packages/opencode/src/tool/edit.ts (668 lines) - EditTool + 9种Replacer
- packages/opencode/src/tool/read.ts (293 lines) - ReadTool
- packages/opencode/src/tool/write.ts (84 lines) - WriteTool

特性：
1. ReadTool - 分批读取、行截断、字节限制、二进制检测、目录读取、图片/PDF支持
2. WriteTool - 检查存在、显示diff
3. EditTool - 多策略智能替换（9种Replacer按优先级尝试）

opencode版权声明：  Copyright (c) 2025 Kujtim Hoxha

"""

import logging
import mimetypes
import re
from collections.abc import Generator
from pathlib import Path
from typing import Any

from peng.common.utils import generate_colored_diff
from peng.common.colors import get_file_colors
from peng.tools.base import DualOutput, Tool, ToolError

logger = logging.getLogger(__name__)


# ============================================================
# 常量配置 - 完全照搬 opencode
# ============================================================

DEFAULT_READ_LIMIT = 2000
MAX_LINE_LENGTH = 2000
MAX_LINE_SUFFIX = f"... (line truncated to {MAX_LINE_LENGTH} chars)"
MAX_BYTES = 80 * 1024
MAX_BYTES_LABEL = f"{MAX_BYTES // 1024} KB"

MAX_DIAGNOSTICS_PER_FILE = 20
MAX_PROJECT_DIAGNOSTICS_FILES = 5

# Levenshtein 相似度阈值 - 完全照搬 opencode
SINGLE_CANDIDATE_SIMILARITY_THRESHOLD = 0.0
MULTIPLE_CANDIDATES_SIMILARITY_THRESHOLD = 0.3


# ============================================================
# 颜色配置
# ============================================================


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


# ============================================================
# 换行符处理工具函数 - 完全照搬 opencode
# ============================================================


def normalize_line_endings(text: str) -> str:
    """
    将所有换行符统一为 \\n
    照搬 opencode: text.replaceAll("\\r\\n", "\\n")
    """
    return text.replace("\r\n", "\n")


def detect_line_ending(text: str) -> str:
    """
    检测文本使用的换行符类型
    照搬 opencode: return text.includes("\\r\\n") ? "\\r\\n" : "\\n"
    """
    return "\r\n" if "\r\n" in text else "\n"


def convert_to_line_ending(text: str, ending: str) -> str:
    """
    将文本的换行符转换为目标换行符
    照搬 opencode: if (ending === "\\n") return text; return text.replaceAll("\\n", "\\r\\n")
    """
    if ending == "\n":
        return text.replace("\r\n", "\n")
    return text.replace("\n", "\r\n")


# ============================================================
# Levenshtein 距离算法 - 完全照搬 opencode
# ============================================================


def levenshtein(a: str, b: str) -> int:
    """
    Levenshtein 距离算法实现
    照搬 opencode edit.ts 中的实现
    """
    # 处理空字符串
    if a == "" or b == "":
        return max(len(a), len(b))

    # 创建矩阵 - 照搬 opencode 的 Array.from 写法
    matrix = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]

    # 初始化第一行和第一列
    for i in range(len(a) + 1):
        matrix[i][0] = i
    for j in range(len(b) + 1):
        matrix[0][j] = j

    # 填充矩阵 - 照搬 opencode 的逻辑
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            matrix[i][j] = min(
                matrix[i - 1][j] + 1,  # 删除
                matrix[i][j - 1] + 1,  # 插入
                matrix[i - 1][j - 1] + cost,  # 替换
            )

    return matrix[len(a)][len(b)]


# ============================================================
# 替换策略 (Replacers) - 完全照搬 opencode
# ============================================================

Replacer = Generator[str, None, None]


def simple_replacer(content: str, find: str) -> Replacer:
    """
    SimpleReplacer - 精确匹配
    照搬 opencode: yield find
    """
    yield find


def line_trimmed_replacer(content: str, find: str) -> Replacer:
    """
    LineTrimmedReplacer - 忽略行首尾空格的匹配
    照搬 opencode edit.ts 实现
    """
    original_lines = content.split("\n")
    search_lines = find.split("\n")

    # 移除末尾空行 - 照搬 opencode: if (searchLines[searchLines.length - 1] === "")
    if search_lines and search_lines[-1] == "":
        search_lines.pop()

    # 照搬 opencode: for (let i = 0; i <= originalLines.length - searchLines.length; i++)
    for i in range(len(original_lines) - len(search_lines) + 1):
        matches = True

        for j in range(len(search_lines)):
            original_trimmed = original_lines[i + j].strip()
            search_trimmed = search_lines[j].strip()

            if original_trimmed != search_trimmed:
                matches = False
                break

        if matches:
            # 计算匹配的起始和结束索引
            match_start_index = 0
            for k in range(i):
                match_start_index += len(original_lines[k]) + 1

            match_end_index = match_start_index
            for k in range(len(search_lines)):
                match_end_index += len(original_lines[i + k])
                if k < len(search_lines) - 1:
                    match_end_index += 1  # Add newline character except for the last line

            yield content[match_start_index:match_end_index]


def block_anchor_replacer(content: str, find: str) -> Replacer:
    """
    BlockAnchorReplacer - 使用首尾行锚点 + Levenshtein 距离的块匹配
    照搬 opencode edit.ts 实现
    """
    original_lines = content.split("\n")
    search_lines = find.split("\n")

    if len(search_lines) < 3:
        return

    # 移除末尾空行
    if search_lines[-1] == "":
        search_lines.pop()

    first_line_search = search_lines[0].strip()
    last_line_search = search_lines[-1].strip()
    search_block_size = len(search_lines)

    # 收集所有候选位置，两个锚点都匹配的位置
    candidates: list[tuple[int, int]] = []
    for i in range(len(original_lines)):
        if original_lines[i].strip() != first_line_search:
            continue

        # 在首行之后查找匹配的尾行
        for j in range(i + 2, len(original_lines)):
            if original_lines[j].strip() == last_line_search:
                candidates.append((i, j))
                break  # 只匹配第一次出现的尾行

    # 如果没有候选，立即返回
    if not candidates:
        return

    # 处理单个候选场景（使用宽松阈值）
    if len(candidates) == 1:
        start_line, end_line = candidates[0]
        actual_block_size = end_line - start_line + 1

        similarity = 0
        lines_to_check = min(search_block_size - 2, actual_block_size - 2)  # 仅检查中间行

        if lines_to_check > 0:
            for j in range(1, min(search_block_size - 1, actual_block_size - 1)):
                original_line = original_lines[start_line + j].strip()
                search_line = search_lines[j].strip()
                max_len = max(len(original_line), len(search_line))
                if max_len == 0:
                    continue
                distance = levenshtein(original_line, search_line)
                similarity += (1 - distance / max_len) / lines_to_check

                # 达到阈值时提前退出
                if similarity >= SINGLE_CANDIDATE_SIMILARITY_THRESHOLD:
                    break
        else:
            # 没有中间行可比较，仅基于锚点接受
            similarity = 1.0

        if similarity >= SINGLE_CANDIDATE_SIMILARITY_THRESHOLD:
            match_start_index = sum(len(original_lines[k]) + 1 for k in range(start_line))
            match_end_index = match_start_index
            for k in range(start_line, end_line + 1):
                match_end_index += len(original_lines[k])
                if k < end_line:
                    match_end_index += 1  # 最后一行外添加换行符
            yield content[match_start_index:match_end_index]
        return

    # 计算多个候选的相似度
    best_match: tuple[int, int] | None = None
    max_similarity = -1

    for start_line, end_line in candidates:
        actual_block_size = end_line - start_line + 1

        similarity = 0
        lines_to_check = min(search_block_size - 2, actual_block_size - 2)  # 仅检查中间行

        if lines_to_check > 0:
            total_similarity = 0
            for j in range(1, min(search_block_size - 1, actual_block_size - 1)):
                original_line = original_lines[start_line + j].strip()
                search_line = search_lines[j].strip()
                max_len = max(len(original_line), len(search_line))
                if max_len == 0:
                    continue
                distance = levenshtein(original_line, search_line)
                total_similarity += 1 - distance / max_len
            similarity = total_similarity / lines_to_check  # 平均相似度
        else:
            # 没有中间行可比较，仅基于锚点接受
            similarity = 1.0

        if similarity > max_similarity:
            max_similarity = similarity
            best_match = (start_line, end_line)

    # 阈值判断
    if max_similarity >= MULTIPLE_CANDIDATES_SIMILARITY_THRESHOLD and best_match:
        start_line, end_line = best_match
        match_start_index = sum(len(original_lines[k]) + 1 for k in range(start_line))
        match_end_index = match_start_index
        for k in range(start_line, end_line + 1):
            match_end_index += len(original_lines[k])
            if k < end_line:
                match_end_index += 1
        yield content[match_start_index:match_end_index]


def whitespace_normalized_replacer(content: str, find: str) -> Replacer:
    """
    WhitespaceNormalizedReplacer - 忽略多余空格的匹配
    照搬 opencode edit.ts 实现
    """

    def normalize_whitespace(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    normalized_find = normalize_whitespace(find)

    # 处理单行匹配
    lines = content.split("\n")
    for line in lines:
        if normalize_whitespace(line) == normalized_find:
            yield line
        else:
            # 仅在整行不匹配时检查子串匹配
            normalized_line = normalize_whitespace(line)
            if normalized_find in normalized_line:
                # 在原始行中查找实际匹配的子串
                words = find.strip().split()
                if words:
                    pattern = r"\s+".join(re.escape(word) for word in words)
                    try:
                        regex = re.compile(pattern)
                        match = regex.search(line)
                        if match:
                            yield match.group(0)
                    except re.error:
                        # 无效的正则表达式模式，跳过
                        pass

    # 处理多行匹配
    find_lines = find.split("\n")
    if len(find_lines) > 1:
        for i in range(len(lines) - len(find_lines) + 1):
            block = lines[i : i + len(find_lines)]
            if normalize_whitespace("\n".join(block)) == normalized_find:
                yield "\n".join(block)


def indentation_flexible_replacer(content: str, find: str) -> Replacer:
    """
    IndentationFlexibleReplacer - 忽略缩进差异的匹配
    照搬 opencode edit.ts 实现
    """

    def remove_indentation(text: str) -> str:
        lines = text.split("\n")
        non_empty_lines = [line for line in lines if line.strip()]
        if not non_empty_lines:
            return text

        min_indent = min(
            len(re.match(r"^(\s*)", line).group(1)) if re.match(r"^(\s*)", line) else 0
            for line in non_empty_lines
        )

        return "\n".join(line if not line.strip() else line[min_indent:] for line in lines)

    normalized_find = remove_indentation(find)
    content_lines = content.split("\n")
    find_lines = find.split("\n")

    for i in range(len(content_lines) - len(find_lines) + 1):
        block = "\n".join(content_lines[i : i + len(find_lines)])
        if remove_indentation(block) == normalized_find:
            yield block


def escape_normalized_replacer(content: str, find: str) -> Replacer:
    """
    EscapeNormalizedReplacer - 处理转义字符的匹配
    照搬 opencode edit.ts 实现
    """

    def unescape_string(s: str) -> str:
        def replace_escape(match: re.Match) -> str:
            captured = match.group(1)
            escape_map = {
                "n": "\n",
                "t": "\t",
                "r": "\r",
                "'": "'",
                '"': '"',
                "`": "`",
                "\\": "\\",
                "\n": "\n",
                "$": "$",
            }
            return escape_map.get(captured, match.group(0))

        return re.sub(r'\\(n|t|r|\'|"|`|\\|\n|\$)', replace_escape, s)

    unescaped_find = unescape_string(find)

    # 使用未转义的查找字符串尝试直接匹配
    if unescaped_find in content:
        yield unescaped_find

    # 同时尝试在内容中查找与未转义查找匹配的转义版本
    lines = content.split("\n")
    find_lines = unescaped_find.split("\n")

    for i in range(len(lines) - len(find_lines) + 1):
        block = "\n".join(lines[i : i + len(find_lines)])
        unescaped_block = unescape_string(block)

        if unescaped_block == unescaped_find:
            yield block


def trimmed_boundary_replacer(content: str, find: str) -> Replacer:
    """
    TrimmedBoundaryReplacer - 尝试修剪版本的匹配
    照搬 opencode edit.ts 实现
    """
    trimmed_find = find.strip()

    if trimmed_find == find:
        # 已经修剪，无需尝试
        return

    # 尝试查找修剪版本
    if trimmed_find in content:
        yield trimmed_find

    # 同时尝试查找修剪内容匹配的块
    lines = content.split("\n")
    find_lines = find.split("\n")

    for i in range(len(lines) - len(find_lines) + 1):
        block = "\n".join(lines[i : i + len(find_lines)])

        if block.strip() == trimmed_find:
            yield block


def context_aware_replacer(content: str, find: str) -> Replacer:
    """
    ContextAwareReplacer - 上下文感知匹配
    照搬 opencode edit.ts 实现
    """
    find_lines = find.split("\n")
    if len(find_lines) < 3:
        # 至少需要 3 行才有意义的上下文
        return

    # 如果存在末尾空行则移除
    if find_lines[-1] == "":
        find_lines.pop()

    content_lines = content.split("\n")

    # 提取首尾行作为上下文锚点
    first_line = find_lines[0].strip()
    last_line = find_lines[-1].strip()

    # 查找以上下文锚点开头和结尾的块
    for i in range(len(content_lines)):
        if content_lines[i].strip() != first_line:
            continue

        # 查找匹配的尾行
        for j in range(i + 2, len(content_lines)):
            if content_lines[j].strip() == last_line:
                # 找到潜在的上下文块
                block_lines = content_lines[i : j + 1]
                block = "\n".join(block_lines)

                # 检查中间内容是否有合理的相似度
                # （简单启发式：修剪后至少 50% 的非空行应匹配）
                if len(block_lines) == len(find_lines):
                    matching_lines = 0
                    total_non_empty_lines = 0

                    for k in range(1, len(block_lines) - 1):
                        block_line = block_lines[k].strip()
                        find_line = find_lines[k].strip()

                        if block_line or find_line:
                            total_non_empty_lines += 1
                            if block_line == find_line:
                                matching_lines += 1

                    if total_non_empty_lines == 0 or matching_lines / total_non_empty_lines >= 0.5:
                        yield block
                        break  # 只匹配第一次出现
                break


def multi_occurrence_replacer(content: str, find: str) -> Replacer:
    """
    MultiOccurrenceReplacer - 多出现处理
    照搬 opencode edit.ts 实现
    此替换器生成所有精确匹配，允许替换函数根据 replaceAll 参数处理多个出现
    """
    start_index = 0

    while True:
        index = content.find(find, start_index)
        if index == -1:
            break

        yield find
        start_index = index + len(find)


# ============================================================
# 核心替换函数 - 完全照搬 opencode
# ============================================================


def replace_content(content: str, oldString: str, newString: str, replace_all: bool = False) -> str:
    """
    使用多策略替换内容

    照搬 opencode edit.ts 的 replace 函数实现
    按优先级顺序尝试各种替换策略
    """
    if oldString == newString:
        raise ToolError("No changes to apply: oldString and newString are identical.")

    not_found = True

    # 按照 opencode 的顺序尝试各种 replacer
    replacers = [
        simple_replacer,
        line_trimmed_replacer,
        block_anchor_replacer,
        whitespace_normalized_replacer,
        indentation_flexible_replacer,
        escape_normalized_replacer,
        trimmed_boundary_replacer,
        context_aware_replacer,
        multi_occurrence_replacer,
    ]

    for replacer in replacers:
        for search in replacer(content, oldString):
            index = content.find(search)
            if index == -1:
                continue
            not_found = False
            if replace_all:
                return content.replace(search, newString)

            last_index = content.rfind(search)
            if index != last_index:
                continue

            return content[:index] + newString + content[index + len(search) :]

    if not_found:
        raise ToolError("无法在文件中找到 oldString。必须完全匹配，包括空白字符、缩进和换行符。")

    raise ToolError("找到多个 oldString 的匹配项。提供更多周围的上下文以使匹配唯一。")


# ============================================================
# Diff 生成工具 - 完全照搬 opencode
# ============================================================


def trim_diff(diff: str) -> str:
    """
    修剪 diff 的公共缩进
    照搬 opencode edit.ts 的 trimDiff 函数实现
    """
    lines = diff.split("\n")
    content_lines = [
        line
        for line in lines
        if (line.startswith("+") or line.startswith("-") or line.startswith(" "))
        and not line.startswith("---")
        and not line.startswith("+++")
    ]

    if not content_lines:
        return diff

    min_indent = float("inf")
    for line in content_lines:
        content = line[1:]
        if content.strip():
            match = re.match(r"^(\s*)", content)
            if match:
                min_indent = min(min_indent, len(match.group(1)))

    if min_indent == float("inf") or min_indent == 0:
        return diff

    trimmed_lines = []
    for line in lines:
        if (
            (line.startswith("+") or line.startswith("-") or line.startswith(" "))
            and not line.startswith("---")
            and not line.startswith("+++")
        ):
            prefix = line[0]
            content = line[1:]
            trimmed_lines.append(prefix + content[int(min_indent) :])
        else:
            trimmed_lines.append(line)

    return "\n".join(trimmed_lines)


def _extract_raw_content(read_resp) -> str:
    """从 nest read 响应中提取原始文本内容（去掉行号前缀）"""
    if not read_resp or not read_resp.data:
        return ""
    lines_list = read_resp.data.get("lines", [])
    return "\n".join(re.sub(r"^\d+: ", "", ln, count=1) for ln in lines_list)


# ============================================================
# 二进制文件检测 - 完全照搬 opencode read.ts
# ============================================================

# 照搬 opencode read.ts 中的扩展名列表
BINARY_EXTENSIONS = {
    ".zip",
    ".tar",
    ".gz",
    ".exe",
    ".dll",
    ".so",
    ".class",
    ".jar",
    ".war",
    ".7z",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".odt",
    ".ods",
    ".odp",
    ".bin",
    ".dat",
    ".obj",
    ".o",
    ".a",
    ".lib",
    ".wasm",
    ".pyc",
    ".pyo",
}


def is_binary_by_extension(file_path: str) -> bool:
    """通过扩展名检测二进制文件 - 照搬 opencode switch case"""
    ext = Path(file_path).suffix.lower()
    return ext in BINARY_EXTENSIONS


def is_binary_by_content(
    file_path: str, file_size: int | None = None, sample_size: int = 4096
) -> bool:
    """
    通过内容检测二进制文件
    照搬 opencode read.ts 中的 isBinaryFile 函数
    """
    # 照搬 opencode: if (fileSize === 0) return false
    if file_size is not None and file_size == 0:
        return False

    try:
        # 照搬 opencode: const sampleSize = Math.min(4096, fileSize)
        actual_sample_size = sample_size if file_size is None else min(sample_size, file_size)

        with open(file_path, "rb") as f:
            sample = f.read(actual_sample_size)

        if not sample:
            return False

        # 检查空字节 - if (bytes[i] === 0) return true
        if b"\x00" in sample:
            return True

        # 计算非打印字符比例
        # 照搬 opencode: if (bytes[i] < 9 || (bytes[i] > 13 && bytes[i] < 32))
        non_printable = sum(1 for b in sample if b < 9 or (b > 13 and b < 32))
        # 如果非打印字符比例 >30%，则认为是二进制文件
        return non_printable / len(sample) > 0.3
    except Exception:
        return False


def is_binary_file(file_path: str, file_size: int | None = None) -> bool:
    """
    综合检测二进制文件
    照搬 opencode read.ts 中的 isBinaryFile 函数

    Args:
        file_path: 文件路径
        file_size: 可选的文件大小，用于优化空文件检测
    """
    if is_binary_by_extension(file_path):
        return True
    return is_binary_by_content(file_path, file_size)


# ============================================================
# MIME 类型检测 - 照搬 opencode
# ============================================================


def get_mime_type(file_path: str) -> str:
    """
    获取文件的 MIME 类型
    照搬 opencode Filesystem.mimeType
    """
    mime_type, _ = mimetypes.guess_type(file_path)
    if mime_type:
        return mime_type
    # 默认返回 application/octet-stream
    return "application/octet-stream"


def is_image_file(file_path: str) -> bool:
    """
    检测是否为图片文件（排除 SVG）
    照搬 opencode: mime.startsWith("image/") && mime !== "image/svg+xml" && mime !== "image/vnd.fastbidsheet"
    """
    mime = get_mime_type(file_path)
    return (
        mime.startswith("image/") and mime != "image/svg+xml" and mime != "image/vnd.fastbidsheet"
    )


def is_pdf_file(file_path: str) -> bool:
    """
    检测是否为 PDF 文件
    照搬 opencode: mime === "application/pdf"
    """
    return get_mime_type(file_path) == "application/pdf"


# ============================================================
# 图片缩放 - read 工具读取多模态图片时降低分辨率（不修改原图）
# ============================================================

# read 附件图片的最大像素数（1M 像素）
MAX_IMAGE_PIXELS = 1024 * 1024
MAX_IMAGE_PIXELS_LABEL = f"{MAX_IMAGE_PIXELS // (1024 * 1024)}M 像素"

# 本地 fallback 时补充 mimetypes 不识别的图片扩展名（与 nest local.py 保持一致）
_EXTRA_IMAGE_EXTENSIONS = {".webp", ".ico", ".tiff", ".tif", ".avif", ".heic", ".heif"}

# 主流大模型 vision API（OpenAI/Anthropic/Gemini/GLM/DeepSeek 等）普遍支持的
# 图片 MIME 白名单。BMP/TIFF/ICO/AVIF/HEIC 等不在其中——GLM 对 BMP 返回
# 400"图片输入格式/解析错误"（事务#1822：1400x700 截图 BMP 像素≤1M 原样
# 透传触发），故非白名单格式进入上下文前必须统一转码 PNG（无损）
PROVIDER_SAFE_IMAGE_MIMES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)


def normalize_image_bytes(data: bytes, mime: str) -> tuple[bytes, str, str]:
    """规范化图片字节流：全量解码验证 + 缩放至 1M 像素内 + 转码为 provider 通用格式。

    三项处理（🔧 事务#1834 新增格式转码）：
    1. 全量解码验证（load）：截断/损坏数据在 load() 时抛异常，由调用方拒绝入库
    2. 等比缩放到 MAX_IMAGE_PIXELS 以内（仅内存中处理，不修改原图）
    3. 格式规范化：mime 不在 PROVIDER_SAFE_IMAGE_MIMES 白名单（BMP/TIFF/ICO
       等 provider 普遍不支持的格式）时转码为 PNG（无损，截图类体积通常更小）；
       像素≤1M 时原样透传非白名单格式会在部分 provider（GLM 不支持 BMP）
       触发 400 图片解析错误，清除附件重试又丢失视觉信息，导致反复报错

    仅在内存中处理，不修改磁盘上的原图文件。

    Args:
        data: 原始图片字节流
        mime: 原始 MIME 类型

    Returns:
        (图片字节流, MIME 类型, 转换说明)。未发生转换时说明为空串。

    Raises:
        PIL.UnidentifiedImageError: PIL 完全不识别的格式（缺解码插件）
        其余解码异常: 数据截断/损坏（调用方应拒绝该附件入库）
    """
    import io

    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as src:
        # 🔧 事务#1819：强制全量解码验证 —— Image.open 是懒加载（只读头部），
        # 截断/损坏文件在缩放或发送前都不会暴露问题，必须在读取时 load()
        # 全量解码，损坏数据一律在此拒绝（配合 _build_binary_attachment）
        src.load()
        # 按 EXIF orientation 摆正像素方向（重编码后 EXIF 会丢失，避免方向错误）
        src = ImageOps.exif_transpose(src) or src
        width, height = src.size
        needs_downscale = width * height > MAX_IMAGE_PIXELS
        if needs_downscale:
            # 等比缩放到 1M 像素以内
            scale = (MAX_IMAGE_PIXELS / (width * height)) ** 0.5
            new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
            img = src.resize(new_size, Image.LANCZOS)
        elif mime not in PROVIDER_SAFE_IMAGE_MIMES:
            # 非白名单格式转码：脱离源文件句柄复制后再编码
            img = src.copy()
        else:
            # 白名单格式且未超像素上限：原样返回
            return data, mime, ""

        # 重新编码。缩放时按原格式重编码（JPEG/WEBP 有损压缩控制体积）；
        # 转码或其余格式统一 PNG（支持全部像素模式，无损）
        if needs_downscale and mime == "image/webp":
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            fmt, save_kwargs, out_mime = "WEBP", {"quality": 85}, "image/webp"
        elif needs_downscale and mime == "image/jpeg":
            if img.mode != "RGB":
                img = img.convert("RGB")
            fmt, save_kwargs, out_mime = "JPEG", {"quality": 85}, "image/jpeg"
        else:
            # PNG 不支持 CMYK 模式，其余模式（1/L/LA/P/RGB/RGBA/I;16等）均可
            if img.mode == "CMYK":
                img = img.convert("RGB")
            fmt, save_kwargs, out_mime = "PNG", {}, "image/png"

    buf = io.BytesIO()
    img.save(buf, format=fmt, **save_kwargs)
    notes = []
    if needs_downscale:
        notes.append(f"已缩放至 ≤{MAX_IMAGE_PIXELS_LABEL}")
    if out_mime != mime:
        short = out_mime.split("/", 1)[1].upper()  # png→PNG jpeg→JPEG
        notes.append(f"已转换为{short}通用格式")
    if notes:
        notes.append("原图未修改")
    return buf.getvalue(), out_mime, f"（{'，'.join(notes)}）"


def _build_binary_attachment(file_path: str, b64_data: str, mime: str) -> DualOutput:
    """
    构建图片/PDF 附件输出。

    图片会缩放到 MAX_IMAGE_PIXELS（1M 像素）以内，并转码为 provider 通用
    格式（PROVIDER_SAFE_IMAGE_MIMES 白名单外的 BMP/TIFF 等统一转 PNG，
    仅内存中处理，不修改原图）。

    🔧 事务#1819：图片完整性校验 —— 读取时用 PIL 全量解码验证（Image.open
    是懒加载，必须 load() 才能暴露截断/损坏），解码失败（如截图脚本写入
    截断的BMP）判定文件损坏，返回错误提示而非把损坏附件放进上下文。
    损坏附件一旦进入上下文，所有 provider 都会 HTTP 400 拒绝，且换
    provider 也无法恢复，曾导致事务#1818无限重试死循环。

    🔧 事务#1834：非白名单格式（BMP/TIFF/ICO等）原样透传会被部分 provider
    （GLM不支持BMP）以400"图片输入格式/解析错误"拒绝，清除附件重试又丢失
    视觉信息导致模型反复read反复失败（事务#1822），故统一转码PNG后入库。

    例外：PIL 完全不识别的格式（UnidentifiedImageError，如无插件时的
    heic/avif）无法验证，保留原始数据交由 vision provider 判断。
    """
    import base64

    kind = "图片" if mime.startswith("image/") else "PDF"
    if kind == "图片":
        try:
            raw = base64.b64decode(b64_data)
            data, mime, note = normalize_image_bytes(raw, mime)
            if note:
                b64_data = base64.b64encode(data).decode("utf-8")
            else:
                note = ""
        except Exception as e:
            from PIL import UnidentifiedImageError

            if isinstance(e, UnidentifiedImageError) or (
                type(e).__name__ == "UnidentifiedImageError"
            ):
                # PIL 不识别该格式（可能缺解码插件）：无法本地验证，
                # 保留原始数据（provider 端可能支持）
                logger.warning(f"read 图片格式无法本地验证(PIL不识别)，保留原始数据: {file_path}: {e}")
                note = ""
            else:
                # 可识别格式但解码失败（截断/损坏）：拒绝入库，防止污染上下文
                logger.warning(f"read 图片解码失败(文件可能损坏)，拒绝作为附件返回: {file_path}: {e}")
                msg = f"{kind} 读取失败: 文件损坏或格式异常（无法解码），请检查源文件或重新生成"
                return DualOutput(
                    summary=msg,
                    display=f"❌ {msg}: {file_path}\n解码错误: {e}",
                )
    else:
        note = ""

    msg = f"{kind} 读取成功{note}"
    data_url = f"data:{mime};base64,{b64_data}"
    return DualOutput(
        summary=msg,
        display=f"📄 {msg}: {file_path}",
        attachments=[{"type": "file", "mime": mime, "url": data_url}],
    )


# ============================================================
# 文件不存在时的建议功能 - 照搬 opencode read.ts
# ============================================================


def find_similar_files(file_path: str, limit: int = 3) -> list[str]:
    """
    查找相似文件名作为建议
    照搬 opencode read.ts 行 53-73
    """
    try:
        dir = Path(file_path).parent
        base = Path(file_path).name

        if not dir.exists():
            return []

        entries = list(dir.iterdir())
        suggestions = []
        for entry in entries:
            entry_name = entry.name.lower()
            base_lower = base.lower()
            if entry_name in base_lower or base_lower in entry_name:
                suggestions.append(str(entry))

        return suggestions[:limit]
    except Exception:
        return []


# ============================================================
# ReadTool - 完全照搬 opencode read.ts
# ============================================================

# 完全照搬 opencode read.txt
READ_DESCRIPTION = """从远程 nest 读取文件或目录。如果路径不存在，则返回错误。

使用方法：
- filePath 参数应该是绝对路径。
- 默认情况下，此工具从文件开头返回最多 2000 行。
- offset 参数是开始读取的行号（从 1 开始）。
- 要读取后面的部分，请使用更大的 offset 再次调用此工具。
- 使用 grep 工具在大型文件或长行文件中查找特定内容。
- 如果不确定正确的文件路径，请使用 glob 工具通过 glob 模式查找文件名。
- 返回的每行内容都带有行号前缀 `<line>: <content>`。例如，如果文件内容为 "foo\\n"，您将收到 "1: foo\\n"。对于目录，条目每行返回一个（不带行号），子目录带有尾部 `/`。
- 超过 2000 个字符的行会被截断。
- 当您知道要读取多个文件时，请并行调用此工具。
- 避免重复的小切片（30 行块）。如果需要更多上下文，请读取更大的窗口。
- 多模态大模型可以用此工具可以读取支持的图片、音频、视频、PDF等文件格式，并将其作为文件附件返回，然后识别。
- 读取图片时自动等比缩放到 1M 像素以内后再返回，并转换为provider通用格式（BMP/TIFF等转PNG；仅在内存中处理，原图不会被修改）。"""


class ReadTool(Tool):
    """读取文件工具 - 完全照搬 opencode read.ts 实现"""

    @property
    def name(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return READ_DESCRIPTION

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "filePath": {
                    "type": "string",
                    "description": "要读取的文件或目录的绝对路径",
                },
                "offset": {
                    "type": "number",
                    "description": "开始读取的行号（从 1 开始）",
                },
                "limit": {
                    "type": "number",
                    "description": f"要读取的最大行数（默认为 {DEFAULT_READ_LIMIT}）",
                },
            },
            "required": ["filePath"],
        }

    async def execute(self, **kwargs: Any) -> DualOutput:
        """执行读取文件 - 通过 nest 远程，local nest 不可用时本地 fallback"""
        file_path: str = kwargs.get("filePath", "")
        offset: int = kwargs.get("offset", 1)
        limit: int = kwargs.get("limit", DEFAULT_READ_LIMIT)

        # 参数验证
        if offset < 1:
            return DualOutput(
                summary="offset 必须大于或等于 1",
                display="❌ offset 必须大于或等于 1",
            )

        # 🔧 data/ 目录文件始终从本地项目目录读取（跨 nest 共享数据）
        from peng.common.paths import resolve_local_data_path

        local_data_path = resolve_local_data_path(file_path)
        if local_data_path:
            return await self._execute_local_read(local_data_path, offset, limit)

        # 通过 nest 远程执行
        client = self._get_nest_client()
        if client:
            try:
                return await self._execute_via_nest(client, file_path, offset, limit)
            except Exception as e:
                # 🔧 local nest 不可用时 fallback 到本地读取
                from peng.nest.protocol import NestError
                from peng.tools._nest_helper import is_local_nest, notify_fallback

                if isinstance(e, NestError) and is_local_nest(self._context):
                    notify_fallback("read", f"读取 {file_path}", self._context)
                    logger.info(f"local nest 不可用({e})，fallback 到本地读取")
                    return await self._execute_local_read(file_path, offset, limit)
                raise

        # 无 client，local nest 直接本地读取
        from peng.tools._nest_helper import is_local_nest, notify_fallback

        if is_local_nest(self._context):
            notify_fallback("read", f"读取 {file_path}", self._context)
            return await self._execute_local_read(file_path, offset, limit)

        return DualOutput(
            summary="读取文件失败: 无可用的 nest 连接，无法读取文件",
            display="❌ 读取文件失败: 无可用的 nest 连接，无法读取文件",
        )

    def _get_nest_client(self):
        """获取 nest 客户端（从工具上下文获取 nest 名称和 task_id）"""
        try:
            from peng.nest.client import get_nest_client

            client = get_nest_client()
            nest_name = self.get_context("nest")
            if nest_name:
                client.set_nest_name(nest_name)
            client.set_task_id(self.get_context("task_id"))
            if client:
                import asyncio

                try:
                    asyncio.get_running_loop().create_task(client.connect())
                except Exception:
                    pass
            return client
        except Exception:
            return None

    async def _execute_via_nest(
        self, client, file_path: str, offset: int, limit: int
    ) -> DualOutput:
        """通过 nest 远程读取文件"""
        from peng.nest.protocol import NestError

        try:
            result = await client.read(file_path, offset, limit)

            if result.get("type") == "directory":
                entries = result.get("entries", [])
                output_parts = [
                    f"<path>{result.get('path', file_path)}</path>",
                    "<type>directory</type>",
                    "<entries>",
                ]
                output_parts.extend(entries)
                output_parts.append(f"\n(共 {len(entries)} 个条目)")
                output_parts.append("</entries>")
                output = "\n".join(output_parts)
                return DualOutput(summary=output, display=output)

            if result.get("type") == "binary":
                # 二进制文件（图片/PDF）；图片缩放到 1M 像素以内（不修改原图）
                data = result.get("data", "")
                mime = result.get("mime", "application/octet-stream")
                return _build_binary_attachment(file_path, data, mime)

            # 文本文件
            lines = result.get("lines", [])
            total_lines = result.get("total_lines", 0)
            start = result.get("start", offset)
            end = result.get("end", offset + len(lines) - 1)

            output_parts = [
                f"<path>{result.get('path', file_path)}</path>",
                "<type>file</type>",
                "<content>",
            ]
            output_parts.extend(lines)

            if end < total_lines:
                output_parts.append(
                    f"\n\n(显示 {total_lines} 行中的第 {start}-{end} 行。使用 offset={end + 1} 继续。)"
                )
            else:
                output_parts.append(f"\n\n(文件结束 - 共 {total_lines} 行)")

            output_parts.append("</content>")
            output = "\n".join(output_parts)
            display_parts = [f"📄 文件读取 {file_path} (行 {start}-{end}, 共 {total_lines} 行):\n"]
            return DualOutput(summary=output, display="\n".join(display_parts))

        except NestError as e:
            return DualOutput(
                summary=f"读取文件失败: {e}",
                display=f"❌ 读取文件失败: {file_path}\n{e}",
            )

    async def _execute_local_read(self, file_path: str, offset: int, limit: int) -> DualOutput:
        """本地读取文件（local nest 不可用时的 fallback）"""
        path = Path(file_path).expanduser().resolve()

        if not path.exists():
            return DualOutput(
                summary=f"文件未找到: {file_path}",
                display=f"❌ 文件未找到: {file_path}",
            )

        if path.is_dir():
            entries = []
            for entry in sorted(path.iterdir(), key=lambda x: x.name):
                suffix = "/" if entry.is_dir() else ""
                entries.append(f"{entry.name}{suffix}")
            output_parts = [
                f"<path>{path}</path>",
                "<type>directory</type>",
                "<entries>",
            ]
            output_parts.extend(entries)
            output_parts.append(f"\n(共 {len(entries)} 个条目)")
            output_parts.append("</entries>")
            output = "\n".join(output_parts)
            return DualOutput(summary=output, display=output)

        # 图片/PDF：作为附件返回（图片缩放到 1M 像素以内，不修改原图）
        mime = get_mime_type(str(path))
        is_image = mime.startswith("image/") or path.suffix.lower() in _EXTRA_IMAGE_EXTENSIONS
        if is_image or is_pdf_file(str(path)):
            import base64

            try:
                raw = path.read_bytes()
            except Exception as e:
                return DualOutput(
                    summary=f"读取文件失败: {e}",
                    display=f"❌ 读取文件失败: {file_path}\n{e}",
                )
            return _build_binary_attachment(file_path, base64.b64encode(raw).decode("utf-8"), mime)

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return DualOutput(
                summary=f"读取文件失败: {e}",
                display=f"❌ 读取文件失败: {file_path}\n{e}",
            )

        all_lines = text.splitlines(keepends=True)
        total_lines = len(all_lines)
        start_idx = offset - 1
        end_idx = min(start_idx + limit, total_lines)
        selected = all_lines[start_idx:end_idx]

        numbered_lines = []
        for i, line in enumerate(selected):
            line_num = start_idx + i + 1
            clean = line.rstrip("\n").rstrip("\r")
            if len(clean) > MAX_LINE_LENGTH:
                clean = clean[:MAX_LINE_LENGTH] + MAX_LINE_SUFFIX
            numbered_lines.append(f"{line_num}: {clean}")

        start = offset
        end = offset + len(selected) - 1

        output_parts = [
            f"<path>{path}</path>",
            "<type>file</type>",
            "<content>",
        ]
        output_parts.extend(numbered_lines)

        if end < total_lines:
            output_parts.append(
                f"\n\n(显示 {total_lines} 行中的第 {start}-{end} 行。使用 offset={end + 1} 继续。)"
            )
        else:
            output_parts.append(f"\n\n(文件结束 - 共 {total_lines} 行)")

        output_parts.append("</content>")
        output = "\n".join(output_parts)
        return DualOutput(
            summary=output,
            display=f"📄 [本地] 文件读取 {file_path} (行 {start}-{end}, 共 {total_lines} 行):\n",
        )


# ============================================================

# 完全照搬 opencode write.txt
WRITE_DESCRIPTION = """向远程 nest 写入文件。

使用方法：
- 如果提供路径处已有现有文件，此工具将覆盖该文件。
- 如果是现有文件，您必须先使用 Read 工具读取文件内容。如果您没有先读取文件，此工具将失败。
- 始终优先编辑代码库中的现有文件。除非明确要求，否则不要写入新文件。
- 绝不要主动创建文档文件（*.md）或 README 文件。仅在用户明确要求时才创建文档文件。
- 仅在用户明确要求时才使用表情符号。除非被要求，否则避免将表情符号写入文件。
- ⚠️ 写入文件必须说明目的（purpose参数），要简明扼要（4-200个字符），否则将被拒绝执行！"""


class WriteTool(Tool):
    """写入文件工具 - 完全照搬 opencode write.ts 实现"""

    @property
    def name(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return WRITE_DESCRIPTION

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "filePath": {
                    "type": "string",
                    "description": "要写入的文件的绝对路径（必须是绝对路径，不能是相对路径）",
                },
                "content": {
                    "type": "string",
                    "description": "要写入文件的内容",
                },
                "purpose": {
                    "type": "string",
                    "description": "写入该文件的目的说明（必填，4-200个字符）。大模型调用write工具时必须说明写入目的，否则将被拒绝。",
                },
            },
            "required": ["filePath", "content", "purpose"],
        }

    async def execute(self, **kwargs: Any) -> DualOutput:
        """执行写入文件 - 通过 nest 远程，local nest 不可用时本地 fallback"""
        file_path: str = kwargs.get("filePath", "")
        content: str = kwargs.get("content", "")

        # 目的说明检查：大模型必须说明写入文件的目的（参考 bash 工具 purpose 校验）
        purpose: str = (kwargs.get("purpose") or "").strip()
        if not purpose or len(purpose) < 4 or len(purpose) > 200:
            raise ToolError(
                "写入被拒绝：必须通过 purpose 参数简明扼要地说明写入该文件的目的（4-200字符）。"
                "例如 purpose='初始化配置文件'。"
            )

        # 🔧 data/ 目录文件始终写入本地项目目录（跨 nest 共享数据）
        from peng.common.paths import resolve_local_data_path

        local_data_path = resolve_local_data_path(file_path)
        if local_data_path:
            return await self._execute_local_write(local_data_path, content)

        # 通过 nest 远程执行
        client = self._get_nest_client()
        if client:
            try:
                return await self._execute_via_nest(client, file_path, content)
            except Exception as e:
                from peng.nest.protocol import NestError
                from peng.tools._nest_helper import is_local_nest, notify_fallback

                if isinstance(e, NestError) and is_local_nest(self._context):
                    notify_fallback("write", f"写入 {file_path}", self._context)
                    logger.info(f"local nest 不可用({e})，fallback 到本地写入")
                    return await self._execute_local_write(file_path, content)
                raise

        # 无 client，local nest 直接本地写入
        from peng.tools._nest_helper import is_local_nest, notify_fallback

        if is_local_nest(self._context):
            notify_fallback("write", f"写入 {file_path}", self._context)
            return await self._execute_local_write(file_path, content)

        return DualOutput(
            summary="写入文件失败: 无可用的 nest 连接，无法写入文件",
            display="❌ 写入文件失败: 无可用的 nest 连接，无法写入文件",
        )

    def _get_nest_client(self):
        """获取 nest 客户端（从工具上下文获取 nest 名称和 task_id）"""
        from peng.nest.client import get_nest_client

        client = get_nest_client()
        nest_name = self.get_context("nest")
        if nest_name:
            client.set_nest_name(nest_name)
        client.set_task_id(self.get_context("task_id"))
        return client

    async def _execute_via_nest(self, client, file_path: str, content: str) -> DualOutput:
        """通过 nest 远程写入文件"""
        from peng.nest.protocol import NestError

        try:
            result = await client.write(file_path, content)

            # 从 nest 响应中获取行数信息和旧内容
            new_total_lines = result.get("new_total_lines", 0) if result else 0
            old_total_lines = result.get("old_total_lines", 0) if result else 0
            is_new_resp = result.get("is_new", True) if result else True

            if is_new_resp:
                summary = f"文件创建成功: {file_path} ({new_total_lines} 行)"
            else:
                summary = f"文件写入成功: {file_path}（{old_total_lines} → {new_total_lines} 行）"

            # 生成 diff（直接使用 nest 响应中的 old_content，无需额外 read）
            display_parts = []
            old_content = result.get("old_content", "") if result else ""
            if is_new_resp:
                display_parts.append(f"\n📄 已创建: {file_path} ({new_total_lines} 行)\n")
            elif old_content and old_content != content:
                diff_output = trim_diff(
                    generate_colored_diff(
                        normalize_line_endings(old_content),
                        normalize_line_endings(content),
                        file_path,
                    )
                )
                if diff_output:
                    display_parts.append(f"\n{diff_output}\n")

            display = "".join(display_parts) if display_parts else summary
            return DualOutput(summary=summary, display=display)
        except NestError as e:
            return DualOutput(
                summary=f"写入文件失败: {e}",
                display=f"❌ 写入文件失败: {file_path}\n{e}",
            )

    async def _execute_local_write(self, file_path: str, content: str) -> DualOutput:
        """本地写入文件（local nest 不可用时的 fallback）"""
        path = Path(file_path).expanduser().resolve()
        is_new = not path.exists()

        try:
            # 读取旧内容用于 diff
            old_content = ""
            if path.exists():
                old_content = path.read_text(encoding="utf-8", errors="replace")

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except Exception as e:
            return DualOutput(
                summary=f"写入文件失败: {e}",
                display=f"❌ 写入文件失败: {file_path}\n{e}",
            )

        old_total_lines = len(old_content.splitlines()) if old_content else 0
        new_total_lines = len(content.splitlines()) if content else 0

        if is_new:
            summary = f"文件创建成功: {file_path} ({new_total_lines} 行)"
        else:
            summary = f"文件写入成功: {file_path}（{old_total_lines} → {new_total_lines} 行）"

        display_parts = []
        if is_new:
            display_parts.append(f"\n📄 [本地] 已创建: {file_path} ({new_total_lines} 行)\n")
        elif old_content and old_content != content:
            diff_output = trim_diff(
                generate_colored_diff(
                    normalize_line_endings(old_content),
                    normalize_line_endings(content),
                    file_path,
                )
            )
            if diff_output:
                display_parts.append(f"\n{diff_output}\n")

        display = "".join(display_parts) if display_parts else summary
        return DualOutput(summary=summary, display=display)


# ============================================================
# EditTool - 完全照搬 opencode edit.ts
# ============================================================

# 完全照搬 opencode edit.txt
EDIT_DESCRIPTION = """在文件中执行精确字符串替换。

使用方法：
- 您必须在编辑之前至少在对话中使用一次 `Read` 工具。如果您尝试在没有读取文件的情况下进行编辑，此工具将报错。
- 当编辑来自 Read 工具输出的文本时，请确保保留精确的缩进（制表符/空格），因为它们出现在行号前缀之后。行号前缀格式为：行号 + 冒号 + 空格（例如 `1: `）。该空格之后的所有内容都是要匹配的实际文件内容。永远不要在 oldString 或 newString 中包含行号前缀的任何部分。
- 始终优先编辑代码库中的现有文件。除非明确要求，否则不要写入新文件。
- 仅在用户明确要求时才使用表情符号。除非被要求，否则避免将表情符号添加到文件中。
- 使用 `replaceAll` 替换和重命名文件中的字符串。如果您想重命名变量，此参数很有用。
- 如果报错oldString匹配问题，可尝试使用line_edit行编辑工具，用行号精确指定编辑范围。
- ⚠️ 编辑文件必须说明目的（purpose参数），要简明扼要（4-200个字符），否则将被拒绝执行！
"""


class EditTool(Tool):
    """编辑文件工具 - 完全照搬 opencode edit.ts 的多策略替换实现"""

    @property
    def name(self) -> str:
        return "edit"

    @property
    def description(self) -> str:
        return EDIT_DESCRIPTION

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "filePath": {
                    "type": "string",
                    "description": "要修改的文件的绝对路径",
                },
                "oldString": {
                    "type": "string",
                    "description": "要替换的文本",
                },
                "newString": {
                    "type": "string",
                    "description": "替换后的文本（必须与 oldString 不同）",
                },
                "replaceAll": {
                    "type": "boolean",
                    "description": "替换所有出现的 oldString（默认为 false）",
                },
                "purpose": {
                    "type": "string",
                    "description": "执行该编辑的目的说明（必填，4-200个字符）。大模型调用edit工具时必须说明编辑目的，否则将被拒绝。",
                },
            },
            "required": ["filePath", "oldString", "newString", "purpose"],
        }

    async def execute(self, **kwargs: Any) -> DualOutput:
        """执行编辑文件 - 通过 nest 远程，local nest 不可用时本地 fallback"""
        file_path: str = kwargs.get("filePath", "")
        oldString: str = kwargs.get("oldString", "")
        newString: str = kwargs.get("newString", "")
        replace_all: bool = kwargs.get("replaceAll", False)

        # 目的说明检查：大模型必须说明编辑文件的目的（参考 bash 工具 purpose 校验）
        purpose: str = (kwargs.get("purpose") or "").strip()
        if not purpose or len(purpose) < 4 or len(purpose) > 200:
            raise ToolError(
                "编辑被拒绝：必须通过 purpose 参数简明扼要地说明本次编辑的目的（4-200字符）。"
                "例如 purpose='修复空指针异常'。"
            )

        # 参数验证
        if not file_path:
            return DualOutput(summary="filePath 是必填项", display="❌ filePath 是必填项")

        if oldString == newString:
            return DualOutput(
                summary="无需更改：oldString 和 newString 相同。",
                display="⚠️ 无需更改：oldString 和 newString 相同。",
            )

        # 🔧 data/ 目录文件始终在本地项目目录编辑（跨 nest 共享数据）
        from peng.common.paths import resolve_local_data_path

        local_data_path = resolve_local_data_path(file_path)
        if local_data_path:
            return await self._execute_local_edit(
                local_data_path, oldString, newString, replace_all
            )

        # 通过 nest 远程执行
        client = self._get_nest_client()
        if client:
            try:
                return await self._execute_via_nest(
                    client, file_path, oldString, newString, replace_all
                )
            except Exception as e:
                from peng.nest.protocol import NestError
                from peng.tools._nest_helper import is_local_nest, notify_fallback

                if isinstance(e, NestError) and is_local_nest(self._context):
                    notify_fallback("edit", f"编辑 {file_path}", self._context)
                    logger.info(f"local nest 不可用({e})，fallback 到本地编辑")
                    return await self._execute_local_edit(
                        file_path, oldString, newString, replace_all
                    )
                raise

        # 无 client，local nest 直接本地编辑
        from peng.tools._nest_helper import is_local_nest, notify_fallback

        if is_local_nest(self._context):
            notify_fallback("edit", f"编辑 {file_path}", self._context)
            return await self._execute_local_edit(file_path, oldString, newString, replace_all)

        return DualOutput(
            summary="编辑文件失败: 无可用的 nest 连接，无法编辑文件",
            display="❌ 编辑文件失败: 无可用的 nest 连接，无法编辑文件",
        )

    def _get_nest_client(self):
        """获取 nest 客户端（从工具上下文获取 nest 名称和 task_id）"""
        from peng.nest.client import get_nest_client

        client = get_nest_client()
        nest_name = self.get_context("nest")
        if nest_name:
            client.set_nest_name(nest_name)
        client.set_task_id(self.get_context("task_id"))
        return client

    async def _execute_via_nest(
        self, client, file_path: str, oldString: str, newString: str, replace_all: bool
    ) -> DualOutput:
        """通过 nest 远程编辑文件"""
        from peng.nest.protocol import NestError

        try:
            result = await client.edit(file_path, oldString, newString, replace_all)

            # 从 nest 响应中获取文件总行数信息
            old_total_lines = result.get("old_total_lines", "?") if result else "?"
            new_total_lines = result.get("new_total_lines", "?") if result else "?"
            summary = f"编辑应用成功（{old_total_lines} → {new_total_lines} 行）。"

            # 生成 diff（用 oldString / newString 直接对比）
            display_parts = []
            if oldString != newString:
                diff = trim_diff(
                    generate_colored_diff(
                        normalize_line_endings(oldString),
                        normalize_line_endings(newString),
                        file_path,
                    )
                )
                if diff:
                    display_parts.append(f"\n{diff}\n")

            # 统计行数变化
            old_lines = oldString.split("\n")
            new_lines = newString.split("\n")
            additions = max(0, len(new_lines) - len(old_lines))
            deletions = max(0, len(old_lines) - len(new_lines))
            display_parts.append(f"\n✅ {summary} (+{additions}/-{deletions} 行)\n")

            display = "".join(display_parts)
            return DualOutput(summary=summary, display=display)
        except NestError as e:
            return DualOutput(summary=str(e), display=f"❌ {file_path}\n{e}")

    async def _execute_local_edit(
        self, file_path: str, oldString: str, newString: str, replace_all: bool
    ) -> DualOutput:
        """本地编辑文件（local nest 不可用时的 fallback）"""
        path = Path(file_path).expanduser().resolve()

        if not path.exists():
            return DualOutput(
                summary=f"文件未找到: {file_path}",
                display=f"❌ 文件未找到: {file_path}",
            )

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return DualOutput(summary=str(e), display=f"❌ {file_path}\n{e}")

        try:
            new_content = replace_content(content, oldString, newString, replace_all)
        except ToolError as e:
            return DualOutput(summary=str(e), display=f"❌ {file_path}\n{e}")

        try:
            path.write_text(new_content, encoding="utf-8")
        except Exception as e:
            return DualOutput(summary=str(e), display=f"❌ {file_path}\n{e}")

        old_total_lines = len(content.splitlines())
        new_total_lines_v = len(new_content.splitlines())
        summary = f"编辑应用成功（{old_total_lines} → {new_total_lines_v} 行）。"
        display_parts = []
        diff = trim_diff(
            generate_colored_diff(
                normalize_line_endings(oldString),
                normalize_line_endings(newString),
                file_path,
            )
        )
        if diff:
            display_parts.append(f"\n{diff}\n")

        old_lines = oldString.split("\n")
        new_lines = newString.split("\n")
        additions = max(0, len(new_lines) - len(old_lines))
        deletions = max(0, len(old_lines) - len(new_lines))
        display_parts.append(f"\n✅ [本地] {summary} (+{additions}/-{deletions} 行)\n")

        display = "".join(display_parts)
        return DualOutput(summary=summary, display=display)


# ============================================================
# LineEditTool - 按行编辑工具
# ============================================================

LINE_EDIT_DESCRIPTION = """按行号编辑文件：删除指定行范围、替换指定行范围、在指定行前/后插入内容， edit工具报错“没找到oldString”时，可用此工具替代。

使用方法：
- operation 参数选择操作类型：
  - "delete": 删除从 startLine 到 endLine（包含）的所有行
  - "replace": 将 startLine 到 endLine（包含）的行替换为 content
  - "insert": 在 line 行的前面或后面插入 content
- delete/replace 操作：需要 startLine 和 endLine 参数（行号从 1 开始，包含两端）
- insert 操作：需要 line 和 content 参数，position 可选 "before" 或 "after"（默认 "after"）
- content 参数中的换行符会自动拆分为多行
- 操作前必须先使用 read 工具读取同一文件的相关行号区域，且 read 的行号范围需覆盖 line_edit 的操作区域。否则将拒绝编辑并提示先 read。
- ⚠️ 重要：每次 line_edit 操作会改变文件行数，后续操作的行号会偏移！
  返回结果中的"编辑后行号预览"显示了编辑点附近的最新行号，务必参考此预览确定下一次操作的行号。
  如果不确定当前行号，请先用 read 工具重新读取文件确认。
- ⚠️ 行编辑必须说明目的（purpose参数），要简明扼要（4-200个字符），否则将被拒绝执行！"""


def _build_local_context(
    lines: list[str],
    operation: str,
    start_line: int | None,
    end_line: int | None,
    insert_line: int | None,
    affected_lines: list[str],
) -> list[str]:
    """生成 line_edit 编辑区域附近的带行号上下文预览。

    返回编辑后文件中编辑点前后各 CONTEXT_PAD 行的 "行号: 内容" 列表，
    让 LLM 在连续编辑时能清楚看到当前实际行号分布。
    """
    CONTEXT_PAD = 5
    new_total = len(lines)

    # 计算编辑影响区域的行号范围（1-based，编辑后的新行号）
    if operation == "delete" and start_line and end_line:
        center = start_line
    elif operation == "replace" and start_line and end_line:
        center = start_line + max(len(affected_lines) - 1, 0) if affected_lines else start_line
    elif operation == "insert" and insert_line is not None:
        center = insert_line if affected_lines else insert_line
    else:
        center = 1

    # 上下文窗口范围（1-based）
    ctx_start = max(1, center - CONTEXT_PAD)
    ctx_end = min(new_total, center + CONTEXT_PAD)
    # 如果替换/插入了多行，确保窗口覆盖整个新内容
    if affected_lines:
        if operation == "replace" and start_line:
            ctx_end = max(
                ctx_end, min(new_total, start_line + len(affected_lines) + CONTEXT_PAD - 1)
            )
        elif operation == "insert" and insert_line is not None:
            ctx_end = max(ctx_end, min(new_total, insert_line + len(affected_lines) + CONTEXT_PAD))

    result = []
    for i in range(ctx_start, ctx_end + 1):
        line_content = lines[i - 1]
        if len(line_content) > 200:
            line_content = line_content[:200] + "..."
        result.append(f"{i}: {line_content}")
    return result


class LineEditTool(Tool):
    """按行编辑文件工具 - 按行号删除/替换/插入"""

    @property
    def name(self) -> str:
        return "line_edit"

    @property
    def description(self) -> str:
        return LINE_EDIT_DESCRIPTION

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "filePath": {
                    "type": "string",
                    "description": "要编辑的文件的绝对路径",
                },
                "operation": {
                    "type": "string",
                    "enum": ["delete", "replace", "insert"],
                    "description": "操作类型：delete=删除行范围, replace=替换行范围, insert=插入内容",
                },
                "startLine": {
                    "type": "number",
                    "description": "起始行号（1-based，包含）。delete/replace 操作必填",
                },
                "endLine": {
                    "type": "number",
                    "description": "结束行号（1-based，包含）。delete/replace 操作必填",
                },
                "line": {
                    "type": "number",
                    "description": "目标行号（1-based）。insert 操作必填",
                },
                "content": {
                    "type": "string",
                    "description": "新内容。replace/insert 操作必填，换行符自动拆分为多行",
                },
                "position": {
                    "type": "string",
                    "enum": ["before", "after"],
                    "description": "插入位置（默认 after）。insert 操作时有效",
                },
                "purpose": {
                    "type": "string",
                    "description": "执行该行编辑的目的说明（必填，4-200个字符）。大模型调用line_edit工具时必须说明编辑目的，否则将被拒绝。",
                },
            },
            "required": ["filePath", "operation", "purpose"],
        }

    async def execute(self, **kwargs: Any) -> DualOutput:
        """执行按行编辑 - 通过 nest 远程，local nest 不可用时本地 fallback"""
        file_path: str = kwargs.get("filePath", "")
        operation: str = kwargs.get("operation", "")
        start_line = kwargs.get("startLine")
        end_line = kwargs.get("endLine")
        line = kwargs.get("line")
        content: str = kwargs.get("content", "")
        position: str = kwargs.get("position", "after")

        # 目的说明检查：大模型必须说明行编辑的目的（参考 bash 工具 purpose 校验）
        purpose: str = (kwargs.get("purpose") or "").strip()
        if not purpose or len(purpose) < 4 or len(purpose) > 200:
            raise ToolError(
                "行编辑被拒绝：必须通过 purpose 参数简明扼要地说明本次编辑的目的（4-200字符）。"
                "例如 purpose='删除冗余日志代码'。"
            )

        # 归一化：LLM 有时用 <br> 代替换行符，统一替换为 \n
        if content:
            content = re.sub(r"<br\s*/?>", "\n", content)

        # 参数验证
        if not file_path:
            return DualOutput(summary="filePath 是必填项", display="❌ filePath 是必填项")

        if operation not in ("delete", "replace", "insert"):
            return DualOutput(
                summary=f"不支持的操作类型: {operation}",
                display=f"❌ 不支持的操作类型: {operation}（支持: delete, replace, insert）",
            )

        # 🔧 data/ 目录文件始终在本地项目目录编辑（跨 nest 共享数据）
        from peng.common.paths import resolve_local_data_path

        local_data_path = resolve_local_data_path(file_path)
        if local_data_path:
            return await self._execute_local(
                local_data_path, operation, start_line, end_line, line, content, position
            )

        # 通过 nest 远程执行
        client = self._get_nest_client()
        if client:
            try:
                return await self._execute_via_nest(
                    client, file_path, operation, start_line, end_line, line, content, position
                )
            except Exception as e:
                from peng.nest.protocol import NestError
                from peng.tools._nest_helper import is_local_nest, notify_fallback

                if isinstance(e, NestError) and is_local_nest(self._context):
                    notify_fallback("line_edit", f"按行编辑 {file_path}", self._context)
                    logger.info(f"local nest 不可用({e})，fallback 到本地按行编辑")
                    return await self._execute_local(
                        file_path, operation, start_line, end_line, line, content, position
                    )
                raise

        # 无 client，local nest 直接本地编辑
        from peng.tools._nest_helper import is_local_nest, notify_fallback

        if is_local_nest(self._context):
            notify_fallback("line_edit", f"按行编辑 {file_path}", self._context)
            return await self._execute_local(
                file_path, operation, start_line, end_line, line, content, position
            )

        return DualOutput(
            summary="按行编辑失败: 无可用的 nest 连接",
            display="❌ 按行编辑失败: 无可用的 nest 连接",
        )

    def _get_nest_client(self):
        """获取 nest 客户端"""
        from peng.nest.client import get_nest_client

        client = get_nest_client()
        nest_name = self.get_context("nest")
        if nest_name:
            client.set_nest_name(nest_name)
        client.set_task_id(self.get_context("task_id"))
        return client

    async def _execute_via_nest(
        self, client, file_path, operation, start_line, end_line, line, content, position
    ) -> DualOutput:
        """通过 nest 远程按行编辑"""
        from peng.nest.protocol import NestError

        try:
            result = await client.line_edit(
                file_path,
                operation,
                start_line=start_line,
                end_line=end_line,
                line=line,
                content=content,
                position=position,
            )
            data = result.data if result and result.data else {}
            desc = data.get("description", f"{operation} 操作完成")
            old_total = data.get("old_total_lines", "?")
            new_total = data.get("new_total_lines", "?")
            dup_warning = data.get("warning")  # #1559: 服务端重复检测告警

            summary = f"{desc}（{old_total} → {new_total} 行）"

            # 直接使用 nest 响应中的 old_content/new_content 生成 diff
            # 无需额外的网络往返，更可靠高效
            display_parts = [f"✅ {summary}\n📄 {file_path}"]
            old_content = data.get("old_content", "")
            new_content = data.get("new_content", "")
            if old_content and new_content and old_content != new_content:
                diff = trim_diff(
                    generate_colored_diff(
                        normalize_line_endings(old_content),
                        normalize_line_endings(new_content),
                        file_path,
                    )
                )
                if diff:
                    display_parts.append(f"\n{diff}\n")

            # 编辑后行号上下文预览（帮助连续编辑时跟踪行号）
            context_preview = data.get("context_preview", [])
            if context_preview:
                display_parts.append("\n📍 编辑后行号预览:\n")
                display_parts.append("\n".join(context_preview))
                display_parts.append("\n")
                # 也将精简的行号预览加入 summary，让 LLM 在上下文中可见
                summary += "\n编辑后行号预览:\n" + "\n".join(context_preview)

            display = "".join(display_parts)
            if dup_warning:
                # #1559: 重复内容告警（description已含告警文本，确保醒目可见）
                display_parts.insert(1, f"\n{dup_warning}\n")
                display = "".join(display_parts)
            return DualOutput(summary=summary, display=display)

        except NestError as e:
            return DualOutput(summary=str(e), display=f"❌ {file_path}\n{e}")

    async def _execute_local(
        self, file_path, operation, start_line, end_line, line, content, position
    ) -> DualOutput:
        """本地按行编辑（local nest 不可用时的 fallback）"""
        path = Path(file_path).expanduser().resolve()

        if not path.exists():
            return DualOutput(
                summary=f"文件未找到: {file_path}",
                display=f"❌ 文件未找到: {file_path}",
            )

        try:
            raw_content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return DualOutput(summary=str(e), display=f"❌ {file_path}\n{e}")

        # 保留原始换行符
        line_ending = "\r\n" if "\r\n" in raw_content else "\n"
        had_trailing_newline = raw_content.endswith(("\n", "\r"))
        lines = raw_content.splitlines()
        old_total = len(lines)
        lines_before_snapshot = list(lines)  # #1559重复检测用（编辑前状态）

        try:
            if operation == "delete":
                if start_line is None or end_line is None:
                    return DualOutput(
                        summary="delete 操作需要 startLine 和 endLine 参数",
                        display="❌ delete 操作需要 startLine 和 endLine 参数",
                    )
                start_line = int(start_line)
                end_line = int(end_line)
                if start_line < 1 or end_line > old_total or start_line > end_line:
                    return DualOutput(
                        summary=f"行号范围无效: startLine={start_line}, endLine={end_line}, 文件共 {old_total} 行",
                        display=f"❌ 行号范围无效: startLine={start_line}, endLine={end_line}, 文件共 {old_total} 行",
                    )
                del lines[start_line - 1 : end_line]
                desc = f"删除第 {start_line}-{end_line} 行（共 {end_line - start_line + 1} 行）"

            elif operation == "replace":
                if start_line is None or end_line is None:
                    return DualOutput(
                        summary="replace 操作需要 startLine 和 endLine 参数",
                        display="❌ replace 操作需要 startLine 和 endLine 参数",
                    )
                start_line = int(start_line)
                end_line = int(end_line)
                if start_line < 1 or end_line > old_total or start_line > end_line:
                    return DualOutput(
                        summary=f"行号范围无效: startLine={start_line}, endLine={end_line}, 文件共 {old_total} 行",
                        display=f"❌ 行号范围无效: startLine={start_line}, endLine={end_line}, 文件共 {old_total} 行",
                    )
                replacement_lines = content.split("\n") if content else []
                lines[start_line - 1 : end_line] = replacement_lines
                desc = f"替换第 {start_line}-{end_line} 行为 {len(replacement_lines)} 行新内容"

            elif operation == "insert":
                if line is None:
                    return DualOutput(
                        summary="insert 操作需要 line 参数",
                        display="❌ insert 操作需要 line 参数",
                    )
                line = int(line)
                if line < 1 or line > old_total:
                    return DualOutput(
                        summary=f"行号无效: line={line}, 文件共 {old_total} 行",
                        display=f"❌ 行号无效: line={line}, 文件共 {old_total} 行",
                    )
                insert_lines = content.split("\n") if content else []
                if position == "before":
                    insert_pos = line - 1
                else:
                    insert_pos = line
                lines[insert_pos:insert_pos] = insert_lines
                pos_label = "前" if position == "before" else "后"
                desc = f"在第 {line} 行{pos_label}插入 {len(insert_lines)} 行"
            else:
                return DualOutput(
                    summary=f"不支持的操作: {operation}",
                    display=f"❌ 不支持的操作: {operation}",
                )
            new_total = len(lines)
            summary = f"{desc}（{old_total} → {new_total} 行）"

            # #1559: 重复内容检测（content边界/内部与旧文件邻接行的疑似重复）
            # 成功编辑后告警不阻断，帮助LLM立即发现参数错误
            dup_warning: str | None = None
            try:
                from peng.common.line_edit_check import detect_line_edit_duplication

                # 本地分支各操作变量名不同(replacement_lines/insert_lines)，统一重建
                _affected = (
                    content.split("\n") if (content and operation in ("replace", "insert")) else []
                )
                dup_warning = detect_line_edit_duplication(
                    lines_before=lines_before_snapshot,
                    operation=operation,
                    start_line=start_line,
                    end_line=end_line,
                    insert_line=line,
                    affected_lines=_affected,
                    position=position,
                )
            except Exception as dup_e:
                logger.debug(f"line_edit 重复检测失败(不影响编辑): {dup_e}")

            # 编辑后行号上下文预览（在写回文件前构建，构建失败也不影响编辑结果）
            try:
                if operation == "delete":
                    affected = []
                elif operation == "replace":
                    affected = content.split("\n") if content else []
                else:  # insert
                    affected = content.split("\n") if content else []
                context_preview = _build_local_context(
                    lines, operation, start_line, end_line, line, affected
                )
            except Exception:
                context_preview = []

            # 写回
            new_raw = line_ending.join(lines)
            if had_trailing_newline and new_raw:
                new_raw += line_ending
            path.write_text(new_raw, encoding="utf-8")

            # 生成 diff（仅 display，不返回给 LLM）
            display_parts = [f"✅ [本地] {summary}\n📄 {file_path}"]
            if raw_content != new_raw:
                diff = trim_diff(
                    generate_colored_diff(
                        normalize_line_endings(raw_content),
                        normalize_line_endings(new_raw),
                        file_path,
                    )
                )
                if diff:
                    display_parts.append(f"\n{diff}\n")

            # 追加编辑后行号上下文预览（如构建成功）
            if context_preview:
                display_parts.append("\n📍 编辑后行号预览:\n")
                display_parts.append("\n".join(context_preview))
                # 也将精简的行号预览加入 summary，让 LLM 在上下文中可见
                summary += "\n编辑后行号预览:\n" + "\n".join(context_preview)

            display = "".join(display_parts)
            if dup_warning:
                # #1559: 重复内容告警（拼入summary让LLM必须看到）
                summary = f"{dup_warning}\n{summary}"
                display_parts.insert(1, f"\n{dup_warning}\n")
                display = "".join(display_parts)
            return DualOutput(summary=summary, display=display)

        except Exception as e:
            return DualOutput(summary=str(e), display=f"❌ {file_path}\n{e}")


# ============================================================
# 导出
# ============================================================

__all__ = [
    "ReadTool",
    "WriteTool",
    "EditTool",
    "LineEditTool",
    "replace_content",
    "trim_diff",
    "levenshtein",
    "normalize_line_endings",
    "detect_line_ending",
    "convert_to_line_ending",
    "is_binary_file",
    "get_mime_type",
    "is_image_file",
    "is_pdf_file",
    "PROVIDER_SAFE_IMAGE_MIMES",
    "normalize_image_bytes",
    "find_similar_files",
    # 替换器
    "simple_replacer",
    "line_trimmed_replacer",
    "block_anchor_replacer",
    "whitespace_normalized_replacer",
    "indentation_flexible_replacer",
    "escape_normalized_replacer",
    "trimmed_boundary_replacer",
    "context_aware_replacer",
    "multi_occurrence_replacer",
]

"""
Grep工具

内容搜索。

限制：
- 最多显示配置的匹配数（默认 100）
- 单行最大长度可配置（默认 2000）
- 超过限制时提示使用更精确的搜索模式
"""

import logging
import re
import traceback
from pathlib import Path
from typing import Any

from peng.tools.base import DualOutput, Tool, ToolError
from peng.common.truncation import get_truncation_config, truncate_line

logger = logging.getLogger(__name__)


def _get_grep_limits() -> tuple[int, int]:
    """获取 grep 限制配置"""
    config = get_truncation_config()
    return (
        config.get("grep_max_matches", 100),
        config.get("grep_max_line_length", 2000),
    )


class GrepTool(Tool):
    """内容搜索工具"""

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return "使用正则表达式搜索文件内容。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "正则表达式模式",
                },
                "path": {
                    "type": "string",
                    "description": "搜索目录或文件（可选，默认当前目录）",
                },
                "glob": {
                    "type": "string",
                    "description": "文件模式过滤（如 *.py）",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": "输出模式（默认content）",
                },
                "-i": {
                    "type": "boolean",
                    "description": "忽略大小写（默认false）",
                },
            },
            "required": ["pattern"],
        }

    async def execute(self, **kwargs: Any) -> DualOutput:
        """执行grep搜索"""
        pattern: str = kwargs.get("pattern", "")
        path: str | None = kwargs.get("path")
        glob_pattern: str | None = kwargs.get("glob")
        output_mode: str = kwargs.get("output_mode", "content")
        ignore_case = kwargs.get("-i", False) or kwargs.get("i", False)

        from peng.common.paths import get_working_directory
        workdir = self.get_context("workdir")
        workdir = Path(workdir) if workdir else get_working_directory()

        # 尝试通过 nest 远程执行（不本地resolve路径，直接传原始path给远程）
        client = self._get_nest_client()
        if client:
            search_path = path or str(workdir)
            return await self._execute_via_nest(
                client, pattern, search_path, glob_pattern, 
                output_mode, ignore_case
            )

        # 本地执行（兼容模式）- 本地才做路径resolve
        search_path = Path(path).expanduser().resolve() if path else workdir
        return await self._execute_local(
            pattern, search_path, glob_pattern, output_mode, ignore_case
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
        self, client, pattern: str, search_path: str,
        glob_pattern: str | None, output_mode: str, ignore_case: bool
    ) -> DualOutput:
        """通过 nest 远程执行 grep"""
        from peng.nest.protocol import NestError
        try:
            result = await client.grep(
                pattern=pattern,
                path=search_path,
                glob=glob_pattern,
                ignore_case=ignore_case,
            )
            matches = result.get("matches", [])
            files_count = result.get("files_count", 0)
            
            if output_mode == "files_with_matches":
                # Extract file paths from matches
                files = set()
                for m in matches:
                    parts = m.split(":", 2)
                    if parts:
                        files.add(parts[0])
                output = "\n".join(sorted(files)) if files else "未找到匹配"
                return DualOutput(summary=output, display=output)
            
            if output_mode == "count":
                # total_matches 为服务端精确总数（rg 路径）；旧服务端缺省时回退 len(matches)
                total = result.get("total_matches") or len(matches)
                output = f"找到 {total} 处匹配，在 {files_count} 个文件中"
                return DualOutput(summary=output, display=output)
            
            if not matches:
                output = "未找到匹配"
                return DualOutput(summary=output, display=output)
            
            output = "\n".join(matches)
            return DualOutput(summary=output, display=output)
        except NestError as e:
            raise ToolError(f"grep 搜索失败: {e}") from e
    
    async def _execute_local(
        self, pattern: str, search_path: Path,
        glob_pattern: str | None, output_mode: str, ignore_case: bool
    ) -> DualOutput:

        if not search_path.exists():
            raise ToolError(f"路径不存在: {search_path}")

        # 从配置获取限制值
        max_matches, max_line_length = _get_grep_limits()

        # 优先 ripgrep（存在则使用之）；rg 不可用/正则方言不兼容/失败时
        # asearch 返回 None，回退下方纯 Python 实现
        from peng.common import ripgrep

        rg_result = await ripgrep.asearch(
            pattern, str(search_path),
            glob=glob_pattern, ignore_case=ignore_case,
            output_mode=output_mode,
        )
        if rg_result is not None:
            output = ripgrep.format_output(
                rg_result, output_mode, max_matches, max_line_length
            )
            return DualOutput(summary=output, display=output)

        # 编译正则表达式
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            import traceback
            raise ToolError(f"无效的正则表达式: {e}") from e

        # 收集文件
        if search_path.is_file():
            files = [search_path]
        else:
            if glob_pattern:
                files = list(search_path.rglob(glob_pattern))
            else:
                files = [f for f in search_path.rglob("*") if f.is_file()]

        # 使用 .gitignore 过滤
        from peng.common.gitignore import GitIgnoreFilter
        gitignore_filter = GitIgnoreFilter(search_path)
        if gitignore_filter.has_gitignore and search_path.is_dir():
            files = gitignore_filter.filter_paths(files)

        # 过滤掉二进制文件和常见忽略目录
        ignore_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv"}
        text_files: list[Path] = []
        for f in files:
            # 跳过忽略目录
            if any(part in ignore_dirs for part in f.parts):
                continue
            # 简单判断是否为文本文件
            if self._is_text_file(f):
                text_files.append(f)

        # 限制文件数量
        text_files = text_files[:500]

        # 并行搜索文件
        from concurrent.futures import ThreadPoolExecutor

        matches: list[str] = []
        files_with_matches: set[Path] = set()
        match_count = 0
        truncated_lines = 0

        def _search_file(file_path: Path) -> tuple[list[str], bool, int, int]:
            """搜索单个文件，返回(matches, has_match, count, truncated)"""
            local_matches = []
            has_match = False
            count = 0
            truncated = 0

            try:
                with open(file_path, encoding="utf-8", errors="ignore") as file_handle:
                    for line_num, line_text in enumerate(file_handle, 1):
                        if regex.search(line_text):
                            has_match = True
                            count += 1
                            if output_mode == "content" and len(local_matches) < max_matches:
                                rel_path = file_path.relative_to(search_path)
                                truncated_line = truncate_line(line_text.rstrip(), max_line_length)
                                if len(line_text.rstrip()) > max_line_length:
                                    truncated += 1
                                local_matches.append(f"{rel_path}:{line_num}:{truncated_line}")
            except Exception as e:
                logger.error(
                    f"❌ _search_file() - grep.py:146: 异常 - {e}\n{traceback.format_exc()}"
                )

            return (local_matches, has_match, count, truncated)

        # 使用线程池并行搜索
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(_search_file, text_files))

        # 合并结果（使用zip正确关联文件和结果）
        for file_path, (local_matches, has_match, count, truncated) in zip(
            text_files, results, strict=True
        ):
            if has_match:
                files_with_matches.add(file_path)
                match_count += count
                truncated_lines += truncated
                if output_mode == "content":
                    matches.extend(local_matches)
                    if len(matches) > max_matches:
                        matches = matches[:max_matches]

        # 格式化输出
        if output_mode == "files_with_matches":
            result = "\n".join(str(f.relative_to(search_path)) for f in files_with_matches)
            # 如果文件列表太长，也截断
            if len(files_with_matches) > max_matches:
                result_lines = result.split("\n")
                result = "\n".join(result_lines[:max_matches])
                result += f"\n\n... 还有 {len(files_with_matches) - max_matches} 个文件"
            return DualOutput(summary=result, display=result)  
        if output_mode == "count":
            result = f"找到 {match_count} 处匹配，在 {len(files_with_matches)} 个文件中"
            return DualOutput(summary=result, display=result)        
        if not matches:
            result = "未找到匹配"
            return DualOutput(summary=result, display=result)
        result = "\n".join(matches)

        # 添加截断提示
        hint_parts = []
        if match_count > max_matches:
            hint_parts.append(
                f"(Results truncated: showing {max_matches} of {match_count} "
                f"matches. Consider using a more specific path or pattern.)"
            )

        if truncated_lines > 0:
            hint_parts.append(
                f"({truncated_lines} lines were truncated to {max_line_length} chars)"
            )

        if hint_parts:
            result += "\n\n" + "\n".join(hint_parts)
        return DualOutput(summary=result, display=result)

    def _is_text_file(self, path: Path) -> bool:
        """判断是否为文本文件"""
        # 常见文本文件扩展名
        text_extensions = {
            ".txt",
            ".md",
            ".py",
            ".js",
            ".ts",
            ".json",
            ".yaml",
            ".yml",
            ".html",
            ".css",
            ".xml",
            ".sh",
            ".bash",
            ".zsh",
            ".fish",
            ".c",
            ".cpp",
            ".h",
            ".hpp",
            ".rs",
            ".go",
            ".java",
            ".kt",
            ".rb",
            ".php",
            ".pl",
            ".lua",
            ".vim",
            ".sql",
            ".toml",
            ".ini",
            ".cfg",
            ".conf",
            ".env",
            ".gitignore",
            ".dockerfile",
            ".makefile",
            ".cmake",
            ".rst",
            ".log",
        }
        return path.suffix.lower() in text_extensions

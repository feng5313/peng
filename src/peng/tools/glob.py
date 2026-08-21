"""
Glob工具

文件模式匹配。
"""

from pathlib import Path
from typing import Any

from peng.tools.base import Tool, ToolError


class GlobTool(Tool):
    """文件模式匹配工具"""

    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return "使用glob模式匹配文件。返回匹配的文件路径列表。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "glob模式（如 **/*.py）",
                },
                "path": {
                    "type": "string",
                    "description": "搜索目录（可选，默认当前目录）",
                },
            },
            "required": ["pattern"],
        }

    async def execute(self, **kwargs: Any) -> str:
        """执行glob匹配"""
        pattern: str = kwargs.get("pattern", "")
        path: str | None = kwargs.get("path")

        from peng.common.paths import get_working_directory
        workdir = self.get_context("workdir")
        workdir = Path(workdir) if workdir else get_working_directory()

        # 尝试通过 nest 远程执行（不本地resolve路径，直接传原始path给远程）
        client = self._get_nest_client()
        if client:
            search_path = path or str(workdir)
            return await self._execute_via_nest(client, pattern, search_path)

        # 本地执行（兼容模式）- 本地才做路径resolve
        search_path = Path(path).expanduser().resolve() if path else workdir
        return await self._execute_local(pattern, search_path)
    
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
    
    async def _execute_via_nest(self, client, pattern: str, search_path: str) -> str:
        """通过 nest 远程执行 glob"""
        from peng.nest.protocol import NestError
        try:
            result = await client.glob(pattern, search_path)
            matches = result.get("matches", [])
            if not matches:
                return "未找到匹配的文件"
            output = "\n".join(matches)
            total = result.get("total", len(matches))
            if total > len(matches):
                output += f"\n\n... 还有 {total - len(matches)} 个结果"
            return output
        except NestError as e:
            raise ToolError(f"glob 匹配失败: {e}") from e
    
    async def _execute_local(self, pattern: str, search_path: Path) -> str:

        if not search_path.exists():
            raise ToolError(f"目录不存在: {search_path}")

        if not search_path.is_dir():
            raise ToolError(f"不是目录: {search_path}")

        try:
            from concurrent.futures import ThreadPoolExecutor
            from peng.common.gitignore import GitIgnoreFilter

            # 收集所有匹配的文件
            matches = list(search_path.rglob(pattern))
            if not matches:
                return "未找到匹配的文件"

            # 使用 .gitignore 过滤
            gitignore_filter = GitIgnoreFilter(search_path)
            if gitignore_filter.has_gitignore:
                matches = gitignore_filter.filter_paths(matches)
                if not matches:
                    return "未找到匹配的文件"

            # 并行获取stat信息
            def _get_mtime(match: Path) -> tuple[Path, float | None]:
                try:
                    return (match, match.stat().st_mtime)
                except OSError:
                    return (match, None)

            with ThreadPoolExecutor(max_workers=8) as executor:
                stat_results = list(executor.map(_get_mtime, matches))

            # 过滤有效的mtime并排序
            matches_with_mtime = [(p, m) for p, m in stat_results if m is not None]

            if not matches_with_mtime:
                return "未找到匹配的文件"

            # 按修改时间排序
            matches_with_mtime.sort(key=lambda x: x[1], reverse=True)

            # 格式化输出
            result: list[str] = []
            for match, _ in matches_with_mtime[:100]:  # 限制结果数量
                try:
                    rel_path = match.relative_to(search_path)
                    result.append(str(rel_path))
                except ValueError:
                    result.append(str(match))

            output = "\n".join(result)
            if len(matches_with_mtime) > 100:
                output += f"\n\n... 还有 {len(matches_with_mtime) - 100} 个结果"

            return output

        except Exception as e:
            import traceback

            raise ToolError(f"glob匹配失败: {e}\n{traceback.format_exc()}") from e

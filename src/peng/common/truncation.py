"""
工具输出截断模块

参考 opencode 的设计，提供工具输出的截断和保存功能。
解决 token 爆炸问题，同时确保数据不丢失。

设计原则：
1. 不丢失数据 - 完整内容保存到文件
2. 可恢复 - 提供文件路径 + Read(offset/limit) 建议
3. 智能提示 - 根据场景给出不同的访问建议
4. 可配置 - 所有限制值和提示词均可配置
"""

import logging
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from peng.common import jsonp as json
from peng.config import load_config

# 模块级缓存

logger = logging.getLogger(__name__)
_config_cache: dict[str, Any] | None = None
_truncation_config_cache: dict[str, Any] | None = None


def _get_truncation_config() -> dict[str, Any]:
    """获取截断配置（从 peng.json，带缓存）"""
    global _truncation_config_cache
    if _truncation_config_cache is not None:
        return _truncation_config_cache

    try:
        config = load_config()
        _truncation_config_cache = {
            "max_lines": getattr(config.truncation, "max_lines", 2000),
            "max_bytes": getattr(config.truncation, "max_bytes", 50 * 1024),
            "max_line_length": getattr(config.truncation, "max_line_length", 2000),
            "retention_days": getattr(config.truncation, "retention_days", 7),
        }
    except Exception as e:
        logger.error(
            f"❌ _get_truncation_config() - truncation.py:71: 异常 - {e}\n{traceback.format_exc()}"
        )
        _truncation_config_cache = {
            "max_lines": 2000,
            "max_bytes": 50 * 1024,
            "max_line_length": 2000,
            "retention_days": 7,
        }
    return _truncation_config_cache
@dataclass
class TruncationResult:
    """截断结果"""

    output: str
    truncated: bool
    full_path: str | None = None
    removed_lines: int = 0
    removed_bytes: int = 0


class Truncation:
    """
    工具输出截断器

    类似 opencode/packages/opencode/src/tool/truncation.ts 的实现
    """

    # 工具输出保存目录
    OUTPUT_DIR: Path | None = None

    # 配置缓存
    _config: dict[str, Any] | None = None

    @classmethod
    def _get_config(cls) -> dict[str, Any]:
        """获取配置（带缓存）"""
        if cls._config is None:
            cls._config = _get_truncation_config()
        return cls._config

    @classmethod
    def _get_output_dir(cls) -> Path:
        """获取输出目录"""
        if cls.OUTPUT_DIR is None:
            from peng.config import get_path

            cls.OUTPUT_DIR = get_path("output")
        cls.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        return cls.OUTPUT_DIR

    @classmethod
    def _generate_filename(cls) -> str:
        """生成唯一文件名"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return f"tool_{timestamp}"

    @classmethod
    def truncate(
        cls,
        text: str,
        max_lines: int | None = None,
        max_bytes: int | None = None,
        direction: str = "head",  # "head" 或 "tail"
    ) -> TruncationResult:
        """
        截断输出内容

        Args:
            text: 原始输出内容
            max_lines: 最大行数（默认从配置读取）
            max_bytes: 最大字节数（默认从配置读取）
            direction: 截断方向，"head" 保留头部，"tail" 保留尾部

        Returns:
            TruncationResult: 截断结果
        """
        config = cls._get_config()
        max_lines = max_lines or config.get("max_lines", 2000)
        max_bytes = max_bytes or config.get("max_bytes", 50 * 1024)

        lines = text.split("\n")
        total_bytes = len(text.encode("utf-8"))

        # 如果不需要截断
        if len(lines) <= max_lines and total_bytes <= max_bytes:
            return TruncationResult(output=text, truncated=False)

        # 执行截断
        out_lines: list[str] = []
        total_size = 0
        hit_bytes = False

        if direction == "head":
            # 保留头部
            for line in lines:
                line_size = len(line.encode("utf-8")) + 1  # +1 for newline
                if len(out_lines) >= max_lines or total_size + line_size > max_bytes:
                    if total_size + line_size > max_bytes:
                        hit_bytes = True
                    break
                out_lines.append(line)
                total_size += line_size
        else:
            # 保留尾部
            for line in reversed(lines):
                line_size = len(line.encode("utf-8")) + 1
                if len(out_lines) >= max_lines or total_size + line_size > max_bytes:
                    if total_size + line_size > max_bytes:
                        hit_bytes = True
                    break
                out_lines.insert(0, line)
                total_size += line_size

        # 计算移除的数量
        removed = total_bytes - total_size if hit_bytes else len(lines) - len(out_lines)

        # 保存完整内容到文件
        full_path = cls._save_full_output(text)

        # 生成提示信息
        preview = "\n".join(out_lines)
        truncated_output = f"\n{preview}\n"

        return TruncationResult(
            output=truncated_output,
            truncated=True,
            full_path=str(full_path),
            removed_lines=len(lines) - len(out_lines) if not hit_bytes else 0,
            removed_bytes=total_bytes - total_size if hit_bytes else 0,
        )

    @classmethod
    def truncate_line(cls, line: str, max_length: int | None = None) -> str:
        """
        截断单行内容

        Args:
            line: 单行内容
            max_length: 最大长度（默认从配置读取）

        Returns:
            截断后的行
        """
        config = cls._get_config()
        max_length = max_length or config.get("max_line_length", 2000)
        if len(line) <= max_length:
            return line
        return line[:max_length] + "..."

    @classmethod
    def _save_full_output(cls, content: str) -> Path:
        """保存完整输出到文件"""
        output_dir = cls._get_output_dir()
        filename = cls._generate_filename()
        filepath = output_dir / filename

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        return filepath

    @classmethod
    def cleanup_old_files(cls) -> int:
        """
        清理过期的输出文件

        Returns:
            清理的文件数量
        """
        import time

        config = cls._get_config()
        retention_days = config.get("retention_days", 7)
        output_dir = cls._get_output_dir()
        cutoff_time = time.time() - retention_days * 24 * 60 * 60
        cleaned = 0

        for file in output_dir.glob("tool_*"):
            if file.stat().st_mtime < cutoff_time:
                file.unlink()
                cleaned += 1

        return cleaned


def truncate_output(
    text: str,
    max_lines: int | None = None,
    max_bytes: int | None = None,
    direction: str = "head",
) -> TruncationResult:
    """
    便捷函数：截断工具输出

    Args:
        text: 原始输出
        max_lines: 最大行数
        max_bytes: 最大字节数
        direction: 截断方向

    Returns:
        TruncationResult
    """
    return Truncation.truncate(text, max_lines, max_bytes, direction)


def truncate_line(line: str, max_length: int | None = None) -> str:
    """
    便捷函数：截断单行

    Args:
        line: 单行内容
        max_length: 最大长度

    Returns:
        截断后的行
    """
    return Truncation.truncate_line(line, max_length)


def get_truncation_config() -> dict[str, Any]:
    """
    获取截断配置

    Returns:
        配置字典
    """
    return Truncation._get_config()

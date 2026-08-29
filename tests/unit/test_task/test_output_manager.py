"""
测试 OutputManager 的 source 变更检测机制

验证不同 source 的内容不会被混在同一批行中。
"""
import os
import tempfile
import pytest
from pathlib import Path

from peng.task.output_manager import OutputManager


@pytest.fixture
def output_manager(tmp_path):
    """创建使用临时目录的 OutputManager 实例"""
    # 需要在单例模式下重置
    manager = OutputManager.__new__(OutputManager)
    manager._initialized = True
    manager._write_lock = manager._lock  # 使用类级别的锁
    manager._buffers = {}
    manager._last_source = {}
    manager._consecutive_empty = {}
    manager._output_dir = tmp_path
    manager._ensure_output_dir()
    return manager


class TestSourceChangeDetection:
    """测试 source 变更时缓冲区的正确刷新"""

    def test_same_source_buffering(self, output_manager, tmp_path):
        """同一 source 的内容应该正常缓冲"""
        output_manager.write_output(1, "answer", "hello ")
        output_manager.write_output(1, "answer", "world\n")
        
        # 读取输出文件
        output_file = tmp_path / "task-1.out"
        lines = output_file.read_text(encoding="utf-8").strip().split("\n")
        
        # 应该有一行 answer
        assert len(lines) == 1
        assert "\tanswer\t" in lines[0]
        assert "hello world" in lines[0]

    def test_source_change_flushes_buffer(self, output_manager, tmp_path):
        """source 变化时应该先 flush 缓冲区（使用旧 source）"""
        # 写入 answer 内容（无换行，留在缓冲区）
        output_manager.write_output(1, "answer", "The user wants me to analyze")
        # 写入 tool_call 内容（带换行，触发刷新）
        output_manager.write_output(1, "tool_call", "\n🔧 工具调用: read file\n")
        
        # 读取输出文件
        output_file = tmp_path / "task-1.out"
        content = output_file.read_text(encoding="utf-8")
        lines = content.strip().split("\n")
        
        # 应该有三行（answer + 空行 + tool_call）
        assert len(lines) == 3
        # 第一行应该是 answer
        assert "\tanswer\t" in lines[0]
        assert "The user wants me to analyze" in lines[0]
        # 第二行是空行（来自 \n 前缀），source 为 tool_call
        assert "\ttool_call\t" in lines[1]
        # 第三行应该是 tool_call
        assert "\ttool_call\t" in lines[2]
        assert "🔧 工具调用" in lines[2]

    def test_source_change_with_incomplete_line(self, output_manager, tmp_path):
        """source 变化时，未完成的行应该用旧 source 写出"""
        # 写入 answer 内容（带一个完整行和一个不完整行）
        output_manager.write_output(1, "answer", "complete line\nincomplete")
        # 写入 tool_call 内容
        output_manager.write_output(1, "tool_call", " part\n")
        
        # 读取输出文件
        output_file = tmp_path / "task-1.out"
        content = output_file.read_text(encoding="utf-8")
        lines = content.strip().split("\n")
        
        # 应该有三行
        assert len(lines) == 3
        # 第一行应该是 answer（完整行）
        assert "\tanswer\t" in lines[0]
        assert "complete line" in lines[0]
        # 第二行应该是 answer（未完成行，用旧 source 写出）
        assert "\tanswer\t" in lines[1]
        assert "incomplete" in lines[1]
        # 第三行应该是 tool_call（新内容）
        assert "\ttool_call\t" in lines[2]
        assert "part" in lines[2]

    def test_multiple_source_changes(self, output_manager, tmp_path):
        """多次 source 变化应该正确处理"""
        output_manager.write_output(1, "answer", "answer text\n")
        output_manager.write_output(1, "tool_call", "🔧 tool call\n")
        output_manager.write_output(1, "bash", "bash result\n")
        
        output_file = tmp_path / "task-1.out"
        content = output_file.read_text(encoding="utf-8")
        lines = content.strip().split("\n")
        
        assert len(lines) == 3
        assert "\tanswer\t" in lines[0]
        assert "\ttool_call\t" in lines[1]
        assert "\tbash\t" in lines[2]

    def test_first_write_no_flush(self, output_manager, tmp_path):
        """第一次写入（无 last_source）不应该触发 flush"""
        output_manager.write_output(1, "answer", "first content\n")
        
        output_file = tmp_path / "task-1.out"
        content = output_file.read_text(encoding="utf-8")
        lines = content.strip().split("\n")
        
        assert len(lines) == 1
        assert "\tanswer\t" in lines[0]

    def test_different_task_ids_independent(self, output_manager, tmp_path):
        """不同 task_id 的缓冲区应该独立"""
        output_manager.write_output(1, "answer", "task1 answer\n")
        output_manager.write_output(2, "tool_call", "task2 tool_call\n")
        output_manager.write_output(1, "tool_call", "task1 tool_call\n")
        
        # task-1 输出
        output_file1 = tmp_path / "task-1.out"
        content1 = output_file1.read_text(encoding="utf-8")
        lines1 = content1.strip().split("\n")
        
        assert len(lines1) == 2
        assert "\tanswer\t" in lines1[0]
        assert "\ttool_call\t" in lines1[1]
        
        # task-2 输出
        output_file2 = tmp_path / "task-2.out"
        content2 = output_file2.read_text(encoding="utf-8")
        lines2 = content2.strip().split("\n")
        
        assert len(lines2) == 1
        assert "\ttool_call\t" in lines2[0]

    def test_clear_buffer_resets_last_source(self, output_manager, tmp_path):
        """clear_buffer 应该重置 last_source"""
        output_manager.write_output(1, "answer", "content\n")
        output_manager.clear_buffer(1)
        
        # 重新写入不同 source，不应该触发 flush（因为没有 last_source）
        output_manager.write_output(1, "tool_call", "tool\n")
        
        output_file = tmp_path / "task-1.out"
        content = output_file.read_text(encoding="utf-8")
        lines = content.strip().split("\n")
        
        assert len(lines) == 2
        assert "\tanswer\t" in lines[0]
        assert "\ttool_call\t" in lines[1]

    def test_simulated_llm_stream(self, output_manager, tmp_path):
        """模拟真实的 LLM 流式输出场景"""
        # 模拟 LLM 先输出内容文本（无换行），然后输出工具调用
        output_manager.write_output(1, "answer", "The user wants me to analyze the vllm configuration")
        output_manager.write_output(1, "tool_call", "\n🔧 工具调用: 📃 读取文件 {\"filePath\": \"/mnt/wsl/work/aigc\"}\n")
        
        output_file = tmp_path / "task-1.out"
        content = output_file.read_text(encoding="utf-8")
        lines = content.strip().split("\n")
        
        # 三行：answer + 空行 + tool_call
        assert len(lines) == 3
        # 第一行：answer source
        assert "\tanswer\t" in lines[0]
        assert "The user wants me to analyze" in lines[0]
        # 第二行：空行（来自 \n 前缀），source 为 tool_call
        assert "\ttool_call\t" in lines[1]
        # 第三行：tool_call source
        assert "\ttool_call\t" in lines[2]
        assert "🔧 工具调用" in lines[2]

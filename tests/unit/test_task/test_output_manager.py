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
    # 🔧 Fix #1751：补齐 Task#1725 行号计数器相关属性（原fixture缺失导致
    # _write_lines → _ensure_line_no_ready AttributeError，8个测试存量失败）
    manager._next_line_no = {}
    manager._line_no_file_size = {}
    manager._line_no_format_ok = set()
    manager._output_dir = tmp_path
    manager._ensure_output_dir()
    return manager


class TestLineNoAlignment:
    """🔧 Fix #1751：写前对齐文件尾 —— 多进程行号计数器漂移修复"""

    def test_fresh_file_line_no_from_1(self, output_manager, tmp_path):
        """新文件行号从1开始连续递增"""
        output_manager.write_output(1, "task", "line1\nline2\nline3\n")
        out = tmp_path / "task-1.out"
        lines = out.read_text(encoding="utf-8").strip().split("\n")
        nos = [int(l.split("\t")[2]) for l in lines]
        assert nos == [1, 2, 3]

    def test_align_to_external_append(self, output_manager, tmp_path):
        """外部进程追加了新行 → 本进程写前行号对齐到文件尾"""
        out = tmp_path / "task-1.out"
        # 模拟另一进程写了3行（行号1-3）
        with open(out, "a", encoding="utf-8") as f:
            for i in range(1, 4):
                f.write(f"2026-09-02T00:00:00\ttask\t{i}\text{i}\n")
        # 本进程写入（无内存计数器 → 首次初始化走尾部对齐）
        output_manager.write_output(1, "task", "mine1\nmine2\n")
        lines = out.read_text(encoding="utf-8").strip().split("\n")
        nos = [int(l.split("\t")[2]) for l in lines]
        # 新行行号应从4开始（对齐文件尾行号3+1）
        assert nos == [1, 2, 3, 4, 5]

    def test_detect_external_append_between_writes(self, output_manager, tmp_path):
        """本进程两次写入之间外部进程追加了行 → 第二次写前按物理行数重新对齐"""
        out = tmp_path / "task-1.out"
        output_manager.write_output(1, "task", "mine1\n")  # 行号1
        # 外部进程追加行号100（模拟漂移的另一进程）
        with open(out, "a", encoding="utf-8") as f:
            f.write("2026-09-02T00:00:00\tsystem\t100\texternal\n")
        output_manager.write_output(1, "task", "mine2\n")  # 对齐物理行数2 → 行号3
        lines = out.read_text(encoding="utf-8").strip().split("\n")
        nos = [int(l.split("\t")[2]) for l in lines]
        assert nos == [1, 100, 3]

    def test_pure_old_format_gradual_migration(self, output_manager, tmp_path):
        """纯旧格式文件（第3字段=task_id恒定）→ 行号按物理行数+1续写（渐进迁移）"""
        out = tmp_path / "task-1.out"
        with open(out, "a", encoding="utf-8") as f:
            for i in range(3):
                f.write("2026-07-28T10:00:00\ttask\t77\told line\n")  # task_id=77恒定
        output_manager.write_output(1, "task", "new line\n")
        lines = out.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 4
        # 新行行号=物理行号4（不是78）
        assert int(lines[3].split("\t")[2]) == 4

    def test_jump_lines_file_align_to_physical(self, output_manager, tmp_path):
        """尾部含跳变行（多进程漂移交错）→ 对齐物理行数（不跟随错误行号）"""
        out = tmp_path / "task-1.out"
        with open(out, "a", encoding="utf-8") as f:
            # 模拟：正常行10、跳变行5000、正常行12（交错漂移）
            f.write("2026-09-02T00:00:00\ttask\t10\ta\n")
            f.write("2026-09-02T00:00:01\tsystem\t5000\tb\n")
            f.write("2026-09-02T00:00:02\ttask\t12\tc\n")
        output_manager.write_output(1, "task", "mine\n")
        lines = out.read_text(encoding="utf-8").strip().split("\n")
        # 物理行数3 → 新行行号4（对齐物理行数，而非尾部错误行号12+1）
        assert int(lines[-1].split("\t")[2]) == 4

    def test_file_truncated_counter_reset(self, output_manager, tmp_path):
        """文件被外部清空 → 计数器重置从1开始"""
        out = tmp_path / "task-1.out"
        output_manager.write_output(1, "task", "line1\nline2\n")  # 行号1-2
        # 外部清空
        out.write_text("", encoding="utf-8")
        output_manager.write_output(1, "task", "fresh\n")
        lines = out.read_text(encoding="utf-8").strip().split("\n")
        assert int(lines[0].split("\t")[2]) == 1


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

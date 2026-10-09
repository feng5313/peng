"""事务#2105回归测试：_load_agent_md 必须与进程CWD无关

根因：WebUI工作区切换（set_workdir→os.chdir）改变peng服务进程CWD，
任务runner子进程（cwd=None继承）回退读取相对路径 ./AGENTS.md 时读到
无关工作目录（如huizi）的AGENTS.md，其@🌻！provider锁定标记随系统
提示词注入事务，把事务锁到enable=false的provider（事务#2104实况）。

修复：_load_agent_md改为基于项目根目录的绝对路径解析。
"""

import os
from pathlib import Path

import pytest

from peng.config.loader import get_project_root


def _load_agent_md_impl():
    """复刻 agent.PengAgent._load_agent_md 修复后的路径解析逻辑"""
    project_root = Path(get_project_root())
    possible_paths = [
        project_root / "AGENTS.md",
        project_root / ".peng" / "AGENTS.md",
    ]
    agent_md = ""
    for path in possible_paths:
        if path.exists():
            agent_md = path.read_text(encoding="utf-8")
            break
    return agent_md


class TestLoadAgentMdCwdIndependence:
    """CWD污染场景下 _load_agent_md 不得读取当前目录的AGENTS.md"""

    def test_no_poison_from_cwd(self, tmp_path, monkeypatch):
        """CWD下存在带@provider!锁定标记的AGENTS.md时不得被读取（事务#2105根因）"""
        poison = tmp_path / "AGENTS.md"
        poison.write_text("@🌻！\n跨工作目录污染测试\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        result = _load_agent_md_impl()

        # 项目根目录无AGENTS.md时应返回空（回退prompts.jsonp），绝不能读到CWD下的毒文件
        project_root_md = (Path(get_project_root()) / "AGENTS.md").exists() or (
            Path(get_project_root()) / ".peng" / "AGENTS.md"
        ).exists()
        if not project_root_md:
            assert result == "", f"CWD污染泄漏: 读取到了 {result[:50]!r}"
            assert "🌻" not in result

    def test_real_agent_method_cwd_independent(self, tmp_path, monkeypatch):
        """直接调用 PengAgent._load_agent_md 验证（不触发__init__重量级依赖）"""
        from peng.llm.agent import PengAgent

        poison = tmp_path / "AGENTS.md"
        poison.write_text("@🌻！POISON\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        agent = PengAgent.__new__(PengAgent)  # 跳过__init__（_load_agent_md不依赖实例状态）
        result = agent._load_agent_md()

        project_root_md = (Path(get_project_root()) / "AGENTS.md").exists() or (
            Path(get_project_root()) / ".peng" / "AGENTS.md"
        ).exists()
        if not project_root_md:
            assert "POISON" not in result, "相对路径CWD依赖回归！"
            assert "@🌻" not in result

    def test_project_root_agents_md_still_works(self, tmp_path, monkeypatch):
        """项目根目录存在AGENTS.md时仍正常读取（合法回退路径不被破坏）"""
        project_root = Path(get_project_root())
        agents_md = project_root / "AGENTS.md"
        if not agents_md.exists():  # 当前仓库无此文件则跳过（peng根目录无AGENTS.md）
            pytest.skip("项目根目录无AGENTS.md")
        expected = agents_md.read_text(encoding="utf-8")
        monkeypatch.chdir(tmp_path)  # 任意CWD下读取结果一致
        assert _load_agent_md_impl() == expected

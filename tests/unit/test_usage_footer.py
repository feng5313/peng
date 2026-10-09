"""
Task#1735: CLI provider 用量/费用页脚模块单元测试

测试 peng/cli/_usage_footer.py 的格式化与显示条件判断逻辑，
与 WebUI 底部状态栏右侧徽章（coding-plan.js / deepseek-usage.js）格式对齐。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from peng.cli._usage_footer import (
    _fmt_coding_plan,
    _fmt_deepseek,
    _is_deepseek_name,
    _is_glm_name,
)


@pytest.fixture
def colors():
    return {
        "RESET": "\033[0m",
        "BOLD": "\033[1m",
        "DIM": "\033[90m",
        "ERROR": "\033[31m",
        "SUCCESS": "\033[32m",
        "WARNING": "\033[33m",
        "INFO": "\033[34m",
    }


# ==================== GLM Coding Plan 格式化 ====================


def test_coding_plan_normal(colors):
    line = _fmt_coding_plan(
        {"success": True, "level": "pro", "weekly_percentage": 4, "weekly_reset": "20:38"},
        colors,
    )
    assert line is not None
    assert "GLM套餐用量" in line
    assert "PRO" in line  # 等级转大写
    assert "4%" in line
    assert "♻️20:38" in line
    assert "⚠️" not in line  # 非 stale 无延迟标记
    assert "░" * 10 in line  # 4% → 进度条 0 格填充


def test_coding_plan_level_uppercase(colors):
    line = _fmt_coding_plan(
        {"success": True, "level": "Pro", "weekly_percentage": 50}, colors
    )
    assert "PRO" in line


def test_coding_plan_stale_and_reset_order(colors):
    """与 webui 一致：⚠️ 在前，♻️ 在后"""
    line = _fmt_coding_plan(
        {
            "success": True,
            "level": "max",
            "weekly_percentage": 85,
            "weekly_reset": "03:00",
            "stale": True,
        },
        colors,
    )
    assert "⚠️ ♻️03:00" in line


def test_coding_plan_stale_only(colors):
    line = _fmt_coding_plan(
        {"success": True, "weekly_percentage": 10, "stale": True}, colors
    )
    assert "⚠️" in line
    assert "♻️" not in line


def test_coding_plan_percentage_boundaries(colors):
    # 100% → 进度条全填充
    line = _fmt_coding_plan({"success": True, "weekly_percentage": 100}, colors)
    assert "█" * 10 in line
    # 95% → 红色档（ERROR）
    line = _fmt_coding_plan({"success": True, "weekly_percentage": 95}, colors)
    assert colors["ERROR"] in line
    # 70% → 黄色档（WARNING，webui 橙）
    line = _fmt_coding_plan({"success": True, "weekly_percentage": 70}, colors)
    assert colors["WARNING"] in line
    # 90% 边界 → 红色档
    line = _fmt_coding_plan({"success": True, "weekly_percentage": 90}, colors)
    assert colors["ERROR"] in line
    # 69% → 绿色档
    line = _fmt_coding_plan({"success": True, "weekly_percentage": 69}, colors)
    assert colors["SUCCESS"] in line


def test_coding_plan_invalid_data(colors):
    # success=False / weekly_percentage=None / 空数据 → 不显示
    assert _fmt_coding_plan({"success": False, "message": "x"}, colors) is None
    assert _fmt_coding_plan({"success": True, "weekly_percentage": None}, colors) is None
    assert _fmt_coding_plan({}, colors) is None


# ==================== DeepSeek 格式化 ====================


def test_deepseek_normal(colors):
    line = _fmt_deepseek(
        {
            "success": True,
            "is_peak": True,
            "balance_cny": 33.86,
            "today_cost": 1.23,
            "total_cost": 376.13,
        },
        colors,
    )
    assert line is not None
    assert "🐋高峰" in line
    assert "余额" in line and "¥33.86" in line
    assert "今日" in line and "¥1.23" in line
    assert "累计" in line and "¥376.13" in line
    assert "⚠️" not in line


def test_deepseek_low_peak(colors):
    line = _fmt_deepseek({"success": True, "is_peak": False}, colors)
    assert "🐋低谷" in line
    assert "🐋高峰" not in line


def test_deepseek_balance_hidden_when_none(colors):
    line = _fmt_deepseek({"success": True, "is_peak": False}, colors)
    assert "余额" not in line


def test_deepseek_zero_costs_hidden(colors):
    """与 webui 一致：今日/累计为 0 时不显示"""
    line = _fmt_deepseek(
        {"success": True, "is_peak": False, "today_cost": 0.0, "total_cost": 0.0},
        colors,
    )
    assert "今日" not in line
    assert "累计" not in line


def test_deepseek_balance_color_tiers(colors):
    # <5 红
    line = _fmt_deepseek({"success": True, "balance_cny": 4.99}, colors)
    assert colors["ERROR"] in line
    # 5~20 黄（webui 橙）
    line = _fmt_deepseek({"success": True, "balance_cny": 19.99}, colors)
    assert colors["WARNING"] in line
    # >=20 绿
    line = _fmt_deepseek({"success": True, "balance_cny": 20.0}, colors)
    assert colors["SUCCESS"] in line


def test_deepseek_stale(colors):
    line = _fmt_deepseek(
        {"success": True, "is_peak": False, "balance_cny": 10.0, "stale": True}, colors
    )
    assert "⚠️" in line


def test_deepseek_invalid_data(colors):
    assert _fmt_deepseek({"success": False, "message": "x"}, colors) is None
    assert _fmt_deepseek({}, colors) is None


# ==================== 显示条件（以激活的 provider 即 current_provider 为准） ====================


def test_is_glm_name():
    assert _is_glm_name("z-glm5")
    assert _is_glm_name("z-glm5.3-flash")
    assert _is_glm_name("glm-4")
    assert _is_glm_name("GLM-4")
    assert _is_glm_name("zhipu-x")
    # 非 GLM 系
    assert not _is_glm_name("deepseek-flash")
    assert not _is_glm_name("s-qwen3.8")
    assert not _is_glm_name("")
    assert not _is_glm_name("deepseek")  # 含 "deepseek" 不含 glm


def test_is_deepseek_name():
    assert _is_deepseek_name("deepseek-pro")
    assert _is_deepseek_name("DeepSeek-Flash")
    # 非 DeepSeek 系
    assert not _is_deepseek_name("z-glm5")
    assert not _is_deepseek_name("s-qwen3.8")
    assert not _is_deepseek_name("")


def test_current_provider_gating(monkeypatch, capsys):
    """只打印激活的（current）provider：deepseek 非激活时不打印其费用行"""
    import peng.cli._usage_footer as uf

    def fake_fetch(path, timeout=10.0):
        if path == "/api/llm/providers":
            return {
                "providers": [
                    {"name": "z-glm5", "enable": True, "is_current": True},
                    {"name": "deepseek-flash", "enable": True, "is_current": False},
                ],
                "current_provider": "z-glm5",
            }
        if path == "/api/llm/coding-plan-usage":
            return {"success": True, "level": "pro", "weekly_percentage": 4}
        if path == "/api/llm/deepseek-usage":
            return {"success": True, "is_peak": False, "balance_cny": 99.0}
        return None

    monkeypatch.setattr(uf, "_fetch_json", fake_fetch)
    uf.print_provider_usage_footer()
    out = capsys.readouterr().out
    assert "GLM套餐用量" in out
    assert "🐋" not in out  # deepseek 非激活 → 不打印
    assert "余额" not in out


def test_current_provider_deepseek_only(monkeypatch, capsys):
    """激活 deepseek 时只打印 deepseek 行"""
    import peng.cli._usage_footer as uf

    def fake_fetch(path, timeout=10.0):
        if path == "/api/llm/providers":
            return {
                "providers": [
                    {"name": "z-glm5", "enable": True, "is_current": False},
                    {"name": "deepseek-flash", "enable": True, "is_current": True},
                ],
                "current_provider": "deepseek-flash",
            }
        if path == "/api/llm/coding-plan-usage":
            return {"success": True, "level": "pro", "weekly_percentage": 4}
        if path == "/api/llm/deepseek-usage":
            return {"success": True, "is_peak": False, "balance_cny": 99.0}
        return None

    monkeypatch.setattr(uf, "_fetch_json", fake_fetch)
    uf.print_provider_usage_footer()
    out = capsys.readouterr().out
    assert "🐋" in out
    assert "GLM套餐用量" not in out


# ==================== 网络容错（不依赖 service） ====================


def test_fetch_json_silent_on_unreachable(monkeypatch):
    """service 不可达时静默返回 None，不抛异常"""
    import peng.cli._usage_footer as uf

    monkeypatch.setenv("PENG_PORT", "1")  # 不可能有人监听的端口
    assert uf._fetch_json("/api/llm/providers", timeout=0.5) is None


def test_print_footer_never_raises(monkeypatch, capsys):
    """页脚任何内部异常都被吞掉，绝不影响主命令"""
    import peng.cli._usage_footer as uf

    def boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(uf, "_fetch_json", boom)
    uf.print_provider_usage_footer()  # 不应抛出
    assert capsys.readouterr().out == ""

#!/usr/bin/env python3
"""事务#1576：纯符号无效输出检测单元测试

验证 _scan_consecutive_symbols 的正确性：
- 纯符号连续500+触发
- 含字母（中英文）重置计数
- 正常代码输出不误报
- 跨chunk累积
- 提前返回（达到阈值后停止扫描）
"""

import sys
from pathlib import Path

# 项目根目录加入 sys.path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from peng.llm.tool_engine import ToolExecutionEngine

T = 500


def scan(text: str, run: int = 0, threshold: int = T) -> int:
    return ToolExecutionEngine._scan_consecutive_symbols(text, run, threshold)


def test_pure_symbols_trigger():
    """纯符号串达到阈值触发"""
    assert scan("!" * T) >= T
    assert scan(")}" * (T // 2)) >= T
    # 中文标点、全角符号也是符号（非字母）
    assert scan("，。；：""''（）【】" * 60) >= T
    print("✅ 纯符号串触发")


def test_letter_resets():
    """字母（任意语言）重置计数"""
    # 前499个符号后出现字母：计数归零，后续符号数不足以触发
    assert scan("!" * (T - 1) + "a") == 0
    # 中文重置
    assert scan("!" * (T - 1) + "中") == 0
    # 日文假名也是字母
    assert scan("!" * (T - 1) + "あ") == 0
    # 数字、下划线不重置（属于符号范畴）
    assert scan("!" * (T - 1) + "1") >= T  # 数字后达到T
    assert scan("!" * (T - 1) + "_") >= T
    print("✅ 字母重置 / 数字下划线累加")


def test_normal_code_no_false_positive():
    """正常代码/JSON输出不误报"""
    code = (
        "def foo(x):\n"
        "    if x > 0:\n"
        "        return {\"key\": [1, 2, 3], \"nested\": {\"a\": True}}\n"
        "    else:\n"
        "        return (None,)\n" * 50
    )
    assert scan(code) < T
    # 深缩进 + 大括号（正常代码极限场景）
    deep = "    " * 100 + "\n" + "{" * 50 + "\n" + "value = 1" + "\n"
    assert scan(deep) < T
    # 中文正常文本
    assert scan("这是正常中文内容。" * 100) < T
    # 混合markdown
    md = "# 标题\n\n- 列表项\n\n`code` **bold** [link](url)\n" * 50
    assert scan(md) < T
    print("✅ 正常代码/文本不误报")


def test_cross_chunk_accumulation():
    """跨chunk累积计数"""
    run = 0
    # 200个符号chunk
    run = scan("." * 200, run)
    assert run == 200
    # 又200个
    run = scan("." * 200, run)
    assert run == 400
    # 再200个：达到600 >= 500，提前返回
    run = scan("." * 200, run)
    assert run >= T
    print("✅ 跨chunk累积")


def test_early_return():
    """达到阈值后提前返回（不遍历剩余字符）"""
    # 500个符号 + 后续字母：应在第500个符号处返回（run恰为500）
    run = scan("!" * T + "abc")
    assert run == T
    print("✅ 提前返回")


def test_threshold_zero_disabled():
    """threshold=0 时函数行为（调用方负责跳过检测）"""
    # run>=threshold(0) 立即触发，调用方用 _symbol_threshold > 0 守卫
    assert scan("a", 0, 0) >= 0
    print("✅ threshold=0由调用方守卫")


def test_empty_text():
    """空文本不改变计数"""
    assert scan("", 123) == 123
    print("✅ 空文本")


def test_config_wiring():
    """配置接线：LLMConfig.symbol_output_threshold 默认值 + provider覆盖"""
    from peng.config.loader import LLMConfig, ProviderConfig, LLMPromptsConfig

    llm_cfg = LLMConfig()
    assert llm_cfg.symbol_output_threshold == 500, llm_cfg.symbol_output_threshold

    # provider级覆盖
    prov = ProviderConfig(symbol_output_threshold=100)
    assert prov.symbol_output_threshold == 100
    prov_none = ProviderConfig()
    assert prov_none.symbol_output_threshold is None

    # 提示词
    prompts = LLMPromptsConfig()
    assert "纯符号" in prompts.symbol_output_intervention
    assert "无效输出" in prompts.symbol_output_intervention
    print("✅ 配置接线")


def test_provider_merge():
    """get_provider 返回的配置包含 symbol_output_threshold"""
    from peng.config.loader import LLMConfig, ProviderConfig

    cfg = LLMConfig(
        current_provider="test",
        providers={
            "test": ProviderConfig(api_key="k", base_url="http://localhost:1", model="m")
        },
    )
    data = cfg.get_provider(current="test")
    assert "symbol_output_threshold" in data, data.keys()
    assert data["symbol_output_threshold"] == 500

    # provider覆盖生效
    cfg2 = LLMConfig(
        current_provider="test2",
        providers={
            "test2": ProviderConfig(
                api_key="k", base_url="http://localhost:1", model="m",
                symbol_output_threshold=77,
            )
        },
    )
    data2 = cfg2.get_provider(current="test2")
    assert data2["symbol_output_threshold"] == 77

    # 禁用(0)
    cfg3 = LLMConfig(
        current_provider="test3",
        providers={
            "test3": ProviderConfig(
                api_key="k", base_url="http://localhost:1", model="m",
                symbol_output_threshold=0,
            )
        },
    )
    data3 = cfg3.get_provider(current="test3")
    assert data3["symbol_output_threshold"] == 0
    print("✅ provider合并逻辑")


def main():
    test_pure_symbols_trigger()
    test_letter_resets()
    test_normal_code_no_false_positive()
    test_cross_chunk_accumulation()
    test_early_return()
    test_threshold_zero_disabled()
    test_empty_text()
    test_config_wiring()
    test_provider_merge()
    print("\n🎉 全部测试通过！")


if __name__ == "__main__":
    main()

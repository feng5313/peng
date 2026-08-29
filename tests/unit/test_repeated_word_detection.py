#!/usr/bin/env python3
"""事务#1653：重复词语无效输出检测误报修复的单元测试

背景（#7826误报场景）：技术分析文本中含大量"字母+数字"标识符序列：
"c0=0-15, c1=16-31, c2=32-47, c3=48-63, c4=64-79, c5"
原 _scan_repeated_words 分词只取连续ASCII字母，数字被当分隔符丢弃，
c0/c1/c2/c3/c4/c5 全部切成 "c" → 6个连续相同token → 误判"重复词语无效
输出" → 正常思考内容被丢弃最后2行。

修复：英文token改为"连续ASCII字母+数字混合序列"（c0/x1/rgb8整体）。
数字变化的标识符（c0,c1,c2...）token各不相同不再误报；
完全相同标识符复读（c0 c0 c0...）仍可检出。
"""

import sys
from pathlib import Path

# 项目根目录加入 sys.path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from peng.llm.tool_engine import ToolExecutionEngine

T = 6  # 默认 repeated_word_threshold

# 原始#7826误报消息文本（截取关键段）
ORIG_7826 = (
    "C++: lightDirX@64, lightDirY@68, lightStr@72, ambientCoef@76, ambientLum@80, "
    "windDir@84?? — no, windDir float[2] at 84-91, windAmp@92, turbAmp@96... "
    "HLSL: float2 at 84 straddles c8/c9 (80-95)? 84+8=92 ≤ 95, no straddle. "
    "float2 can be at .zw? No wait, 84 is c8.w — a float2 must start at .xy or .zw; "
    "84 is the w component → can't fit (84-91 = w of c8 + xy of c9) → straddles "
    "register boundary → HLSL pushes to c9 (96).\n"
    "So C++ natural layout (no pads): windDir@84 (C++ allows any offset), "
    "but HLSL puts it at 96. Mismatch again.\n"
    "Hmm wait c8 is offsets 64-79! Let me recount registers: each register = "
    "16 bytes: c0=0-15, c1=16-31, c2=32-47, c3=48-63, c4=64-79, c5"
)


def scan_words(text: str, state: dict, threshold: int = T) -> bool:
    return ToolExecutionEngine._scan_repeated_words(text, state, threshold)


# ==================== 误报回归（必须False） ====================


def test_t1653_register_names_no_false_positive():
    """#1653核心场景：寄存器名c0-c5数字递增 → 不误报（原版误报）"""
    state = {}
    text = "c0=0-15, c1=16-31, c2=32-47, c3=48-63, c4=64-79, c5"
    assert scan_words(text, state) is False


def test_t1653_orig_message_no_false_positive():
    """#7826原始消息全文（单chunk+流式任意切分） → 不误报"""
    state = {}
    assert scan_words(ORIG_7826, state) is False
    # 流式：任意chunk大小切分
    for step in (5, 7, 10, 23, 64, 500):
        state = {}
        detected = False
        for i in range(0, len(ORIG_7826), step):
            if scan_words(ORIG_7826[i : i + step], state):
                detected = True
                break
        assert detected is False, f"chunk={step} 流式误报"


def test_numbered_vars_no_false_positive():
    """数字编号变量x1..x8 → 不误报"""
    state = {}
    text = "x1=1, x2=2, x3=3, x4=4, x5=5, x6=6, x7=7, x8=8"
    assert scan_words(text, state) is False


def test_offsets_and_registers_no_false_positive():
    """十六进制偏移@64/@68... 与寄存器v0..v7 → 不误报"""
    state = {}
    assert scan_words("@64, @68, @72, @76, @80, @84, @88, @92, @96", state) is False
    state = {}
    assert scan_words("v0 v1 v2 v3 v4 v5 v6 v7", state) is False


def test_repeated_common_words_no_false_positive():
    """正常重复的常见单词（float×5 < 阈值6） → 不误报"""
    state = {}
    assert scan_words("float a; float b; float c; float d; float e;", state) is False


# ==================== 二轮：纯数字token误报（#1653） ====================


def test_math_constant_repetition_no_false_positive():
    """数学算式常量"2"重复6次（隔数学符号） → 不误报（原版误报）

    场景：2/(0.58*ext)*(2/2.2) = (2/2.2)/ —— 字母数字分词后"2"×6，
    纯数字token不参与重复词检测。
    """
    state = {}
    assert scan_words("2/(0.58*ext)*(2/2.2) = (2/2.2)/(", state) is False


def test_full_formula_context_no_false_positive():
    """完整公式上下文（psize推导链） → 不误报"""
    state = {}
    text = (
        "0.58*smkFat*sizeMul*size = 0.58*smkFat*(2/(0.58*smkFat))*(2/2.2)*psize "
        "= 2*(2/2.2)*psize ≈ 1.82*psize"
    )
    assert scan_words(text, state) is False


def test_multiplication_chain_no_false_positive():
    """连乘/rgb值数字序列 → 不误报"""
    state = {}
    assert scan_words("2×2×2×2×2×2×2×2", state) is False
    state = {}
    assert scan_words("rgb(255,255,255) rgb(255,255,255) rgb(255,255,255)", state) is False


def test_digit_repetition_intentionally_excluded():
    """纯数字复读（2 2 2...）明确不检出——设计决策：数字常量自然高频，
    真复读垃圾总是字母/中文主导；短语级检测（unit须含字母）同样排除"""
    state = {}
    assert scan_words("2 2 2 2 2 2 2 2 2 2 ", state) is False
    state = {}
    assert scan_words("100 100 100 100 100 100 100 ", state) is False


def test_second_round_sample_streaming_no_false_positive():
    """二轮完整样本（#1671 diff分析+公式推导）流式任意切分 → 不误报"""
    sample = (
        "Now the t11 branch in vs_particle_mesh (line 721-726) still uses volExtX? "
        "Let me check: t11: `sizeMul = 2.0 / (0.58 * volExtX);` — yes it uses volExtX "
        "directly (no max guard). My t12 version added `max(volExtX, 0.1)` guard — "
        "safe since smkFat ∈ [0.44,1.35].\n"
        "One more consideration — the sizeMul ×(2/2.2) for t12: wait, actually let me "
        "reconsider. The physics now targets szBase*2.2. So a fully-grown t12 particle "
        "= 2.2× emission size. With sizeMul = 2/(0.58*ext)*(2/2.2) = (2/2.2)/("
    )
    for step in (5, 10, 33, 100):
        state = {}
        detected = False
        for i in range(0, len(sample), step):
            if scan_words(sample[i : i + step], state):
                detected = True
                break
        assert detected is False, f"chunk={step} 流式误报"


# ==================== 真复读保持检出（必须True） ====================


def test_pure_word_repetition_detected():
    """纯英文单词复读 → 检出"""
    state = {}
    assert scan_words("the the the the the the the", state) is True


def test_single_letter_repetition_detected():
    """单字母复读 → 检出"""
    state = {}
    assert scan_words("c c c c c c c c", state) is True


def test_alnum_identifier_repetition_detected():
    """字母数字混合标识符复读（修复后新增能力） → 检出"""
    state = {}
    # 末尾带空格终结最后一个token
    assert scan_words("c0 c0 c0 c0 c0 c0 ", state) is True
    state = {}
    assert scan_words("x1 x1 x1 x1 x1 x1\n", state) is True


def test_alnum_repetition_streaming_detected():
    """字母数字标识符复读·流式跨chunk → 检出"""
    full = "c0 c0 c0 c0 c0 c0 c0 "
    for step in (3, 10, 33):
        state = {}
        detected = False
        for i in range(0, len(full), step):
            if scan_words(full[i : i + step], state):
                detected = True
                break
        assert detected is True, f"chunk={step} 流式漏检"


def test_cjk_single_char_repetition_detected():
    """中文单字复读 → 检出"""
    state = {}
    assert scan_words("的的的的的的的的", state) is True


def test_cjk_phrase_repetition_detected():
    """中文词组复读（阶段2正则） → 检出"""
    state = {}
    assert scan_words("好的好的好的好的好的好的", state) is True


def test_symbol_sandwiched_repetition_detected():
    """符号分隔的复读（c++ c++ ...） → 检出"""
    state = {}
    assert scan_words("c++ c++ c++ c++ c++ c++ c++", state) is True


# ==================== 状态与边界 ====================


def test_threshold_zero_disabled():
    """threshold=0 → 禁用"""
    state = {}
    assert scan_words("c " * 100, state, 0) is False


def test_below_threshold_not_detected():
    """5次重复（阈值6） → 不检出"""
    state = {}
    assert scan_words("the the the the the ", state) is False


def test_state_cleared_after_detection():
    """检出后state清空"""
    state = {}
    assert scan_words("c c c c c c ", state) is True
    assert state == {}


def test_pending_tail_carries_across_chunks():
    """chunk边界截断token暂存buf，下轮补全后正确计数"""
    # "regi" + "ster register..." 跨chunk拼接
    state = {}
    chunks = ["regi", "ster register register register register register "]
    detected = False
    for ch in chunks:
        if scan_words(ch, state):
            detected = True
    assert detected is True
    # 纯数字+字母混合token跨chunk
    state = {}
    chunks = ["ab", "c123 abc123 abc123 abc123 abc123 abc123 "]
    detected = False
    for ch in chunks:
        if scan_words(ch, state):
            detected = True
    assert detected is True


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-v"]))

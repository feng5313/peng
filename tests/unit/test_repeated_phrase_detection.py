"""事务#1600补充：重复短语无效输出检测（_scan_repeated_phrases）

背景：#1599漏检场景——s-qwen3.8思考退化时单行内复读同一英文短语：
"these are all independent so I'll batch them together now — " × 19次。
单词不重复（漏过repeated_word）、全部单行（漏过行重复检测），
需要短语级检测兜底。阈值：thinking用thinking_dead_loop_threshold(10)，
content用content_dead_loop_threshold(8)。
"""

from peng.llm.tool_engine import ToolExecutionEngine


PHRASE = "these are all independent so I'll batch them together now — "


class TestScanRepeatedPhrases:
    """_scan_repeated_phrases 核心算法测试"""

    def test_t1599_exact_scenario_thinking(self):
        """精确复现#1599：60字符英文短语×10（thinking阈值）→ 检出"""
        state = {}
        # 单chunk送入10次重复
        text = (PHRASE * 10).rstrip(" —")
        assert ToolExecutionEngine._scan_repeated_phrases(text, state, 10, 256) is True

    def test_t1599_exact_scenario_content(self):
        """#1599场景：同一短语×8（content阈值8）→ 检出"""
        state = {}
        text = (PHRASE * 8).rstrip(" —")
        assert ToolExecutionEngine._scan_repeated_phrases(text, state, 8, 256) is True

    def test_t1599_streaming_chunks(self):
        """#1599流式：短语被任意切分为多个chunk → 跨chunk累积检出"""
        state = {}
        full = PHRASE * 11
        # 模拟流式：每次送13字符（任意切分点，不按短语边界）
        step = 13
        detected_at = None
        for i in range(0, len(full), step):
            chunk = full[i : i + step]
            if ToolExecutionEngine._scan_repeated_phrases(chunk, state, 10, 256):
                detected_at = i
                break
        assert detected_at is not None, "流式切分喂入应能检出"

    def test_below_threshold_not_detected(self):
        """9次重复（阈值10）→ 不检出"""
        state = {}
        text = (PHRASE * 9).rstrip(" —")
        assert ToolExecutionEngine._scan_repeated_phrases(text, state, 10, 256) is False

    def test_chinese_phrase_repetition(self):
        """中文短语复读：'我要检查一下配置' × 10 → 检出"""
        state = {}
        text = "我要检查一下配置" * 10
        assert ToolExecutionEngine._scan_repeated_phrases(text, state, 10, 256) is True

    def test_chinese_phrase_with_separator(self):
        """中文短语带标点分隔：'然后继续处理。' × 10 → 检出（≤8分隔符）"""
        state = {}
        text = "然后继续处理。" * 10
        assert ToolExecutionEngine._scan_repeated_phrases(text, state, 10, 256) is True

    def test_mixed_words_not_phrase_not_detected(self):
        """正常文本（不同句子组成）→ 不检出"""
        state = {}
        text = (
            "Let me check the build system first. "
            "The CMakeLists uses GLOB for sources. "
            "Config structure has llm section. "
            "Now I need to examine settings_ui.cpp. "
            "This is normal thinking without repetition."
        )
        assert ToolExecutionEngine._scan_repeated_phrases(text, state, 10, 256) is False

    def test_normal_repeated_short_words_not_phrase(self):
        """正常少量重复（3次短句）→ 不检出（阈值10保护）"""
        state = {}
        text = "好的我知道了 好的我明白了 好的我知道了"
        assert ToolExecutionEngine._scan_repeated_phrases(text, state, 10, 256) is False

    def test_symbol_only_unit_rejected(self):
        """unit为纯符号（如"— —"）→ 不检出（须含字母/中文）"""
        state = {}
        text = "—" * 40
        assert ToolExecutionEngine._scan_repeated_phrases(text, state, 10, 256) is False

    def test_digit_only_unit_rejected(self):
        """unit为纯数字 → 不检出"""
        state = {}
        text = "123 " * 40
        assert ToolExecutionEngine._scan_repeated_phrases(text, state, 10, 256) is False

    def test_maxlen_zero_disabled(self):
        """maxlen=0 → 禁用，永不检出"""
        state = {}
        text = (PHRASE * 20).rstrip(" —")
        assert ToolExecutionEngine._scan_repeated_phrases(text, state, 10, 0) is False

    def test_unit_longer_than_maxlen_not_detected(self):
        """重复unit超过maxlen → 检不到（截断保护）"""
        state = {}
        long_phrase = ("x" * 300) + " end "
        text = long_phrase * 10
        assert ToolExecutionEngine._scan_repeated_phrases(text, state, 10, 256) is False

    def test_state_cleared_after_detection(self):
        """检出后state被清空（重置）"""
        state = {}
        text = (PHRASE * 10).rstrip(" —")
        assert ToolExecutionEngine._scan_repeated_phrases(text, state, 10, 256) is True
        assert state == {}

    def test_newline_separator_allowed(self):
        """短语间用换行分隔 → 检出（换行属于分隔符）"""
        state = {}
        text = "\n".join(["continue with the plan"] * 10)
        assert ToolExecutionEngine._scan_repeated_phrases(text, state, 10, 256) is True

    def test_long_separator_breaks_chain(self):
        """短语间超过8个分隔符 → 链断裂不检出"""
        state = {}
        sep = "-" * 10
        text = sep.join(["let me think about this"] * 10)
        assert ToolExecutionEngine._scan_repeated_phrases(text, state, 10, 256) is False

    def test_performance_reasonable(self):
        """性能：200字符chunk×200次喂入（40K文本）应在毫秒级完成"""
        import time

        state = {}
        normal = (
            "Investigating the mouse sampling architecture across three modules. "
            "The ripple effect uses direct reads while particles use frame updates. "
        )
        start = time.monotonic()
        for _ in range(200):
            ToolExecutionEngine._scan_repeated_phrases(normal, state, 10, 256)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, f"性能异常: {elapsed:.3f}s"

    def test_exact_tail_anchoring(self):
        """尾部锚定：重复恰好在buf末尾结束 → 检出"""
        state = {}
        text = "prefix here. " + (PHRASE * 10)
        assert ToolExecutionEngine._scan_repeated_phrases(text, state, 10, 256) is True


class TestRepeatedPhraseConfig:
    """配置项加载与合并测试"""

    def test_default_maxlen(self):
        """LLMConfig默认repeated_phrase_maxlen=256"""
        from peng.config.loader import LLMConfig

        cfg = LLMConfig()
        assert cfg.repeated_phrase_maxlen == 256

    def test_provider_config_field(self):
        """ProviderConfig有repeated_phrase_maxlen字段，默认None（继承全局）"""
        from peng.config.loader import ProviderConfig

        pc = ProviderConfig()
        assert pc.repeated_phrase_maxlen is None

    def test_peng_json_has_config(self):
        """config/peng.json llm段含repeated_phrase_maxlen=256"""
        import json

        cfg = json.load(open("config/peng.json", encoding="utf-8"))
        assert cfg["llm"]["repeated_phrase_maxlen"] == 256


class TestT1739CodeOutputNotDetected:
    """#1739回归：C代码/格式串输出的合法重复 → 短语检测不得误报

    风主明确指示：符号字母数字混合的不算单词/短语。unit资格收紧为：
    首字符必须是字母或CJK，且含≥3个连续ASCII字母或含CJK字符。
    """

    @staticmethod
    def _fprintf_snippet() -> str:
        """构造#1739误报场景：bezier导出的fprintf格式串（占位符×6）"""
        frag = ",%.4f"
        return (
            'fprintf(fp, "%s    {\\"i\\":%zu,\\"kind\\":\\"%s\\",\\"bez\\":[",'
            "\n                i ? \",\\n\" : \"\", i, k" + frag * 6 + ");\n"
        )

    def test_fprintf_format_string_not_detected(self):
        """C格式串占位符连续重复 → 不检出（thinking阈值）"""
        state = {}
        text = self._fprintf_snippet()
        assert ToolExecutionEngine._scan_repeated_phrases(text, state, 10, 256) is False

    def test_fprintf_format_string_streaming_not_detected(self):
        """C格式串占位符流式输出 → 不检出"""
        full = self._fprintf_snippet()
        for step in (7, 23, 51):
            state = {}
            detected = False
            for i in range(0, len(full), step):
                if ToolExecutionEngine._scan_repeated_phrases(
                    full[i : i + step], state, 10, 256
                ):
                    detected = True
                    break
            assert detected is False, f"chunk={step} 格式串仍误报"

    def test_symbol_anchored_units_rejected(self):
        """以标点开头的候选unit（符号锚定片段）→ 不检出"""
        state = {}
        text = (",f" * 12)
        assert ToolExecutionEngine._scan_repeated_phrases(text, state, 8, 256) is False
        state = {}
        text = (".4f," * 10)
        assert ToolExecutionEngine._scan_repeated_phrases(text, state, 8, 256) is False

    def test_short_letter_run_units_rejected(self):
        """孤立短字母组合（十六进制对等，无≥3字母核心）→ 不检出"""
        state = {}
        text = (" ff") * 12
        assert ToolExecutionEngine._scan_repeated_phrases(text, state, 8, 256) is False

    def test_real_phrase_repetition_still_detected(self):
        """真正自然语言短语复读 → 仍检出（不因收紧而漏检）"""
        state = {}
        phrase = "let me check the result now"
        assert ToolExecutionEngine._scan_repeated_phrases(
            (phrase + " ") * 10, state, 10, 256
        ) is True

    def test_short_word_phrase_repetition_still_detected(self):
        """含短词的短语复读（风主强调不可漏）→ 仍检出"""
        state = {}
        phrase = "of course of course"
        # "of course"含"course"≥3字母核心，且首字符'o'是字母
        assert ToolExecutionEngine._scan_repeated_phrases(
            (phrase + " ") * 10, state, 10, 256
        ) is True

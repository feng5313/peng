"""事务#2783回归测试：非多模态provider读图片时自动切换vision provider

现场（2026-11-19 11:15:48，事务#2778，日志实证故障链）：
1. 上下文~44万token（@compact指令覆盖614400，超过所有provider的
   compact_threshold=320000）
2. read工具读图片→附件part进入消息→client检测prefer_vision=True
3. get_provider 2623行"z-glm5.3不支持多模态，切换到vision provider"
4. _get_best_available_provider(prefer_vision=True)：vision provider
   （z-glm5.3-flash/swift-qwen3.8-iq3）全部被min_context_tokens上下文
   能力过滤（事务#1807）→"所有provider上下文能力均不足"→返回None
5. **根因**：get_provider三处兜底丢失prefer_vision语义——
   - "target_provider无效"兜底按纯priority选中非vision的z-glm5.3(98最高)
   - "上下文能力最终guard"兜底选"能力最大者"（阈值并列取池首=纯文本主力）
6. client端z-glm5.3非vision→附件降级为文本提示→图片识别静默失效

修复：get_provider三处兜底（target无效/未启用/上下文能力guard）经
_vision_prefer_pool筛选，池内存在可用vision provider时优先选择；
无可用vision时回退原池（附件降级文本，既有warning），服务不中断。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from peng.config.loader import (  # noqa: E402
    LLMConfig,
    ProviderConfig,
    _RUNTIME_VISION_DISABLED,
    disable_provider_vision,
)


def _make_llm_config() -> LLMConfig:
    """复刻事务#2783现场配置（data/peng.json覆盖层真实值）

    priority: z-glm5.3(98,非vision) > flash(96,vision) > swift(95,vision)
    全局compact_threshold=320000，请求464410tokens(443248*1.05)全部能力不足
    """
    cfg = LLMConfig()
    cfg.compact_threshold = 320000
    cfg.providers = {
        "z-glm5.3": ProviderConfig(
            api_key="k", base_url="http://a", model="glm", icon="🦊",
            enable=True, priority=98,
        ),
        "z-glm5.3-flash": ProviderConfig(
            api_key="k", base_url="http://a", model="flash", icon="🐱",
            enable=True, priority=96, vision="yes",
        ),
        "swift-qwen3.8-iq3": ProviderConfig(
            api_key="k", base_url="http://b", model="qwen", icon="🐥",
            enable=True, priority=95, vision="yes", compact_threshold=320000,
        ),
    }
    return cfg


def _get(cfg: LLMConfig, **kwargs):
    kwargs.setdefault("current", "z-glm5.3")
    kwargs.setdefault("min_context_tokens", 464410)
    kwargs.setdefault("wait_for_usable", False)
    return cfg.get_provider(**kwargs)


class TestTask2783VisionFallback:
    """上下文能力过滤兜底必须保持prefer_vision语义"""

    def setup_method(self):
        _RUNTIME_VISION_DISABLED.clear()

    def teardown_method(self):
        _RUNTIME_VISION_DISABLED.clear()

    def test_guard_fallback_prefers_vision(self):
        """现场复现：全部provider能力不足时，prefer_vision=True兜底必选vision provider

        旧代码：2788兜底按纯priority选z-glm5.3(98)→附件降级文本，图片失效
        """
        cfg = _make_llm_config()
        result = _get(cfg, prefer_vision=True)
        assert result["provider"] in ("z-glm5.3-flash", "swift-qwen3.8-iq3"), (
            f"兜底选中了非vision provider {result['provider']}，图片将被降级为文本！"
        )
        assert result["vision"] == "yes"

    def test_guard_fallback_prefers_vision_from_none_current(self):
        """current为空（首次连接）同样生效：兜底必选vision provider"""
        cfg = _make_llm_config()
        result = _get(cfg, current=None, prefer_vision=True)
        assert result["provider"] in ("z-glm5.3-flash", "swift-qwen3.8-iq3")
        assert result["vision"] == "yes"

    def test_guard_fallback_without_vision_pref(self):
        """prefer_vision=False维持旧行为：能力并列时纯priority兜底"""
        cfg = _make_llm_config()
        result = _get(cfg, prefer_vision=False)
        assert result["provider"] == "z-glm5.3"

    def test_guard_fallback_all_vision_disabled(self):
        """vision provider全部运行期禁用时回退原池，服务不中断（附件降级文本）"""
        cfg = _make_llm_config()
        disable_provider_vision("z-glm5.3-flash")
        disable_provider_vision("swift-qwen3.8-iq3")
        result = _get(cfg, prefer_vision=True)
        assert result["provider"] == "z-glm5.3"
        assert result["vision"] != "yes"

    def test_guard_prefers_capable_vision(self):
        """存在能力足够的provider时，优先能力足够的vision provider

        swift配置compact_threshold=640000（能力足够），flash=320000不足：
        prefer_vision=True应选swift而非能力并列不足兜底的池首
        """
        cfg = _make_llm_config()
        cfg.providers["swift-qwen3.8-iq3"].compact_threshold = 640000
        result = _get(cfg, prefer_vision=True)
        assert result["provider"] == "swift-qwen3.8-iq3"


class TestVisionPreferPool:
    """_vision_prefer_pool单元语义（事务#2783）"""

    def setup_method(self):
        _RUNTIME_VISION_DISABLED.clear()

    def teardown_method(self):
        _RUNTIME_VISION_DISABLED.clear()

    def test_no_pref_returns_pool_unchanged(self):
        """prefer_vision=False原样返回"""
        cfg = _make_llm_config()
        pool = ["z-glm5.3", "z-glm5.3-flash", "swift-qwen3.8-iq3"]
        assert cfg._vision_prefer_pool(pool, prefer_vision=False) == pool

    def test_pref_filters_vision(self):
        """prefer_vision=True仅保留可用vision provider（保序）"""
        cfg = _make_llm_config()
        pool = ["z-glm5.3", "z-glm5.3-flash", "swift-qwen3.8-iq3"]
        assert cfg._vision_prefer_pool(pool, prefer_vision=True) == [
            "z-glm5.3-flash",
            "swift-qwen3.8-iq3",
        ]

    def test_pref_excludes_vision_only(self):
        """vision-only provider不参与（不支持普通LLM任务）"""
        cfg = _make_llm_config()
        cfg.providers["vo"] = ProviderConfig(
            api_key="k", base_url="http://v", model="vo", icon="🔍",
            enable=True, priority=99, vision="only",
        )
        pool = ["vo", "z-glm5.3-flash"]
        assert cfg._vision_prefer_pool(pool, prefer_vision=True) == ["z-glm5.3-flash"]

    def test_pref_runtime_disabled_falls_back(self):
        """池内vision provider全被运行期禁用时回退原池"""
        cfg = _make_llm_config()
        disable_provider_vision("z-glm5.3-flash")
        disable_provider_vision("swift-qwen3.8-iq3")
        pool = ["z-glm5.3", "z-glm5.3-flash", "swift-qwen3.8-iq3"]
        assert cfg._vision_prefer_pool(pool, prefer_vision=True) == pool

    def test_pref_no_vision_in_pool_falls_back(self):
        """池内本就无vision provider时回退原池"""
        cfg = _make_llm_config()
        pool = ["z-glm5.3"]
        assert cfg._vision_prefer_pool(pool, prefer_vision=True) == pool

"""provider选择回归测试（事务#1793）

背景：事务content中的 @<provider图标> 自定provider功能失效。
根因链（以事务#1785 @🐥! 为例，🐥=s-qwen3.8，运行时priority z-glm5.3(96)最高）：
1. tool_engine run_stream 首轮预热 refresh_provider_vars() 调用
   get_provider(current=None) 不感知content的@标记 → 按priority选中z-glm5.3，
   污染 self.llm.provider='z-glm5.3'
2. chat_stream → get_provider(current='z-glm5.3', favorites=['s-qwen3.8'],
   locked=True) → 锁定分支 current 优先于 favorites → 锁定z-glm5.3，
   用户显式指定的s-qwen3.8被无视
3. 非"锁定"分支同样存在问题：current有效时直接返回current，favorites被无视

修复：
- loader.get_provider 锁定分支：favorites（用户显式@指定）优先于current
- loader.get_provider 非"锁定"分支：current不在favorites时不直接复用current
- client.refresh_provider_vars 增加 content 参数：预热感知@标记，与chat_stream一致
- loader.fetch_providers：返回有序列表，带!锁定标记的provider排最前
- loader.load_config 缓存：related mtime 双向对比（修复data/peng.json删除检测不到）
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from peng.config.loader import LLMConfig, ProviderConfig  # noqa: E402


def _make_llm_config() -> LLMConfig:
    """构造固定providers的LLMConfig（避免依赖真实config/peng.json）

    priority: z-glm5.3(96) > flash(95) > s-qwen3.8(93)，模拟事务#1785场景
    """
    cfg = LLMConfig()
    cfg.providers = {
        "z-glm5.3": ProviderConfig(
            api_key="k", base_url="http://a", model="glm", icon="🦊", enable=True, priority=96
        ),
        "z-glm5.3-flash": ProviderConfig(
            api_key="k", base_url="http://a", model="flash", icon="🐱", enable=True, priority=95
        ),
        "s-qwen3.8": ProviderConfig(
            api_key="k", base_url="http://b", model="qwen", icon="🐥", enable=True, priority=93
        ),
        "deepseek-pro": ProviderConfig(
            api_key="k", base_url="http://c", model="ds", icon="🐋", enable=False, priority=88
        ),
    }
    return cfg


@pytest.fixture
def llm_cfg():
    return _make_llm_config()


class TestFetchProvidersOrder:
    """fetch_providers：有序列表 + 锁定标记排前"""

    def test_lock_marker_provider_first(self, llm_cfg):
        """@🦊 @🐥! → 带锁定的s-qwen3.8排最前（锁定语义取favorites[0]）"""
        result = llm_cfg.fetch_providers("@🦊 @🐥! 测试内容")
        assert result is not None
        providers, locked, forbidden = result
        assert locked is True
        assert forbidden == []
        assert providers[0] == "s-qwen3.8"
        assert "z-glm5.3" in providers

    def test_single_lock_marker(self, llm_cfg):
        """@🐥! → (['s-qwen3.8'], True, [])（事务#1785原文）"""
        assert llm_cfg.fetch_providers("@🐥! 详细分析本项目，给出优化建议") == (
            ["s-qwen3.8"],
            True,
            [],
        )

    def test_plain_marker_no_lock(self, llm_cfg):
        """@🐥（无!）→ locked=False"""
        providers, locked, _ = llm_cfg.fetch_providers("@🐥 处理任务")
        assert providers == ["s-qwen3.8"]
        assert locked is False

    def test_forbidden_marker(self, llm_cfg):
        """@🐋X → 禁止deepseek-pro"""
        providers, locked, forbidden = llm_cfg.fetch_providers("@🐋X 任务")
        assert forbidden == ["deepseek-pro"]
        assert locked is False


class TestGetProviderLockedPriority:
    """get_provider锁定分支：favorites（用户显式指定）优先于current（预热污染）"""

    def test_locked_favorites_beat_warmed_current(self, llm_cfg):
        """事务#1785复现：预热污染current=z-glm5.3 + @🐥!锁定 → 必须锁定s-qwen3.8"""
        r = llm_cfg.get_provider(current="z-glm5.3", favorites=["s-qwen3.8"], locked=True)
        assert r["provider"] == "s-qwen3.8"

    def test_locked_with_no_current(self, llm_cfg):
        """current=None（无污染）→ favorites匹配（原有行为保持）"""
        r = llm_cfg.get_provider(current=None, favorites=["s-qwen3.8"], locked=True)
        assert r["provider"] == "s-qwen3.8"

    def test_locked_fallback_to_current(self, llm_cfg):
        """locked但无favorites（如@不存在的名字!）→ 回退current（原有行为保持）"""
        r = llm_cfg.get_provider(current="deepseek-pro", locked=True)
        assert r["provider"] == "deepseek-pro"

    def test_locked_favorite_in_forbidden_fallback(self, llm_cfg):
        """favorites被禁止 → 回退current"""
        r = llm_cfg.get_provider(
            current="z-glm5.3", favorites=["s-qwen3.8"], locked=True, forbidden=["s-qwen3.8"]
        )
        assert r["provider"] == "z-glm5.3"


class TestGetProviderNonLockedFavorites:
    """get_provider非锁定分支：current不在favorites时不直接复用current"""

    def test_favorites_switch_from_current(self, llm_cfg):
        """@🐥（无!）：current=z-glm5.3不在favorites → 切换到s-qwen3.8"""
        r = llm_cfg.get_provider(current="z-glm5.3", favorites=["s-qwen3.8"], locked=False)
        assert r["provider"] == "s-qwen3.8"

    def test_current_in_favorites_kept(self, llm_cfg):
        """current已在favorites中 → 保持current不切换（稳定性）"""
        r = llm_cfg.get_provider(current="s-qwen3.8", favorites=["s-qwen3.8"], locked=False)
        assert r["provider"] == "s-qwen3.8"

    def test_no_favorites_keeps_current(self, llm_cfg):
        """无favorites：无错不切换（原有"没有出错就不切换"原则保持）"""
        r = llm_cfg.get_provider(current="z-glm5.3", favorites=None, locked=False)
        assert r["provider"] == "z-glm5.3"


class TestRefreshProviderVarsWarmup:
    """refresh_provider_vars预热感知content的@provider标记"""

    def _make_client(self):
        from peng.llm.client import GLMClient

        client = object.__new__(GLMClient)
        client.provider = None
        client.compact_threshold = 320000
        client.thinking_mode = "auto"
        client.thinking_type = None
        client.provider_config = None
        return client

    def test_warmup_without_content_by_priority(self, llm_cfg, monkeypatch):
        """无content：按priority自动选择（原有行为，事务#1699场景）"""
        import types

        import peng.llm.client as client_mod

        monkeypatch.setattr(
            client_mod, "load_config", lambda: types.SimpleNamespace(llm=llm_cfg)
        )
        client = self._make_client()
        client.refresh_provider_vars()
        assert client.provider == "z-glm5.3"

    def test_warmup_with_locked_content(self, llm_cfg, monkeypatch):
        """带content(@🐥!)：预热选中s-qwen3.8，与chat_stream一致（#1793修复）"""
        import types

        import peng.llm.client as client_mod

        monkeypatch.setattr(
            client_mod, "load_config", lambda: types.SimpleNamespace(llm=llm_cfg)
        )
        client = self._make_client()
        client.refresh_provider_vars(content="@🐥! 详细分析本项目，给出优化建议")
        assert client.provider == "s-qwen3.8"


class TestConfigCacheInvalidation:
    """load_config缓存：data/peng.json覆盖层变化（修改/删除）必须失效缓存"""

    def test_overlay_change_invalidates_cache(self, tmp_path, monkeypatch):
        import peng.config.loader as loader

        root = tmp_path / "proj"
        (root / "config").mkdir(parents=True)
        (root / "data").mkdir()
        main_cfg = {
            "llm": {
                "providers": {
                    "p1": {"api_key": "k", "base_url": "http://a", "model": "m", "priority": 10}
                }
            }
        }
        import json

        (root / "config" / "peng.json").write_text(json.dumps(main_cfg), encoding="utf-8")
        monkeypatch.setattr(loader, "_project_root_cache", root)

        # 基线：无覆盖层
        loader._config_cache.clear()
        cfg = loader.load_config()
        assert cfg.llm.providers["p1"].enable is True

        # 新增覆盖层：p1禁用
        overlay = {"llm": {"providers": {"p1": {"enable": False}}}}
        (root / "data" / "peng.json").write_text(json.dumps(overlay), encoding="utf-8")
        cfg = loader.load_config()
        assert cfg.llm.providers["p1"].enable is False, "覆盖层新增未生效"

        # 删除覆盖层：回到基础配置（双向对比修复点）
        (root / "data" / "peng.json").unlink()
        cfg = loader.load_config()
        assert cfg.llm.providers["p1"].enable is True, "覆盖层删除后缓存未失效"

        loader._config_cache.clear()
        monkeypatch.setattr(loader, "_project_root_cache", None)


# ==================== 事务#1807：禁止切换到上下文能力不足的provider ====================


def _make_llm_config_1807() -> LLMConfig:
    """构造事务#1805复现场景的LLMConfig

    全局compact_threshold=320K，max_tokens=60000：
    - z-glm5.3(96)/z-glm5.3-flash(95)：有效能力320K（provider级None→全局）
    - s-qwen3.8(90)：能力220K（模型真实上限262K，#1805中429切换后400超限者）
    - deepseek-pro(88)：能力320K
    - big-llama(10)：能力500K（验证大能力低优先级provider兜底）
    """
    cfg = LLMConfig()
    cfg.compact_threshold = 320000
    cfg.max_tokens = 60000
    cfg.providers = {
        "z-glm5.3": ProviderConfig(
            api_key="k", base_url="http://a", model="glm", icon="🦊",
            enable=True, priority=96,
        ),
        "z-glm5.3-flash": ProviderConfig(
            api_key="k", base_url="http://a", model="flash", icon="🐱",
            enable=True, priority=95,
        ),
        "s-qwen3.8": ProviderConfig(
            api_key="k", base_url="http://b", model="qwen", icon="🐥",
            enable=True, priority=90, compact_threshold=220000, max_tokens=36000,
        ),
        "deepseek-pro": ProviderConfig(
            api_key="k", base_url="http://c", model="ds", icon="🐋",
            enable=True, priority=88,
        ),
        "big-llama": ProviderConfig(
            api_key="k", base_url="http://d", model="llama", icon="🦙",
            enable=True, priority=10, compact_threshold=500000,
        ),
    }
    return cfg


@pytest.fixture
def llm_cfg_1807():
    return _make_llm_config_1807()


class TestContextCapacityFilter:
    """get_provider：上下文能力过滤（事务#1807）"""

    def test_1805_scenario_switch_skips_insufficient(self, llm_cfg_1807):
        """#1805复现：flash失败(failed=True)且上下文~256K时，
        切换必须跳过s-qwen3.8(220K)，选z-glm5.3(320K)"""
        r = llm_cfg_1807.get_provider(
            current="z-glm5.3-flash", failed=True, min_context_tokens=255850
        )
        assert r is not None
        assert r["provider"] == "z-glm5.3", "切换到了上下文能力不足的provider！"

    def test_insufficient_current_not_reused(self, llm_cfg_1807):
        """current=s-qwen3.8(220K)装不下250K请求时不复用，切换到z-glm5.3"""
        r = llm_cfg_1807.get_provider(
            current="s-qwen3.8", failed=False, min_context_tokens=250000
        )
        assert r is not None
        assert r["provider"] == "z-glm5.3"

    def test_large_context_prefers_big_capacity(self, llm_cfg_1807):
        """400K上下文：普通provider全不足，只有big-llama(500K)能接"""
        r = llm_cfg_1807.get_provider(current="z-glm5.3", failed=True, min_context_tokens=400000)
        assert r is not None
        assert r["provider"] == "big-llama"

    def test_all_insufficient_fallback_largest_capacity(self, llm_cfg_1807):
        """全部provider能力不足(500K请求)时兜底返回能力最大者，不抛异常不退出"""
        r = llm_cfg_1807.get_provider(current="z-glm5.3", failed=True, min_context_tokens=500000)
        assert r is not None
        assert r["provider"] == "big-llama", "兜底应返回compact_threshold最大的provider"

    def test_small_context_no_interference(self, llm_cfg_1807):
        """小上下文：能力过滤不干扰正常选择（复用current/按priority）"""
        r = llm_cfg_1807.get_provider(current="s-qwen3.8", failed=False, min_context_tokens=10000)
        assert r["provider"] == "s-qwen3.8"
        r2 = llm_cfg_1807.get_provider(failed=False, min_context_tokens=10000)
        assert r2["provider"] == "z-glm5.3", "应按priority最高选择"

    def test_none_keeps_legacy_behavior(self, llm_cfg_1807):
        """min_context_tokens=None（默认，如refresh_provider_vars预热）保持旧行为"""
        r = llm_cfg_1807.get_provider(current="s-qwen3.8", failed=False)
        assert r["provider"] == "s-qwen3.8"

    def test_locked_respects_user_intent(self, llm_cfg_1807):
        """@provider!锁定：能力不足也不强切（仅告警），尊重用户显式意图"""
        r = llm_cfg_1807.get_provider(
            current="z-glm5.3", favorites=["s-qwen3.8"], locked=True,
            min_context_tokens=250000,
        )
        assert r["provider"] == "s-qwen3.8"

    def test_input_over_threshold_excludes_and_picks_big(self, llm_cfg_1807):
        """输入350K超过glm系320K阈值：全被跳过，只有big-llama(500K)能接；
        且输入280K(未超320K)时glm系可正常接（不误杀接近阈值的provider）"""
        r = llm_cfg_1807.get_provider(
            current="z-glm5.3-flash", failed=True, min_context_tokens=350000
        )
        assert r["provider"] == "big-llama"
        r2 = llm_cfg_1807.get_provider(
            current="z-glm5.3-flash", failed=True, min_context_tokens=280000
        )
        assert r2["provider"] == "z-glm5.3", "280K未超320K阈值不应误杀glm系"


class TestEstimateMessagesTokens:
    """client.estimate_messages_tokens：请求输入token估算（与MessageHistory算法一致）"""

    def test_empty(self):
        from peng.llm.client import estimate_messages_tokens

        assert estimate_messages_tokens(None) == 0
        assert estimate_messages_tokens([]) == 0

    def test_text_message(self):
        from peng.llm.client import estimate_messages_tokens
        import json as _json

        msgs = [{"role": "user", "content": "你好世界hello"}]
        expected = len(_json.dumps(msgs[0], ensure_ascii=False)) * 2 // 7
        assert estimate_messages_tokens(msgs) == expected
        assert expected > 0

    def test_multimodal_parts(self):
        from peng.llm.client import estimate_messages_tokens

        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看图"},
                    {"type": "image_url", "image_url": {"url": "http://x/a.png"}},
                ],
            }
        ]
        # text 2字符 + 100结构开销，附件part固定1000 token
        assert estimate_messages_tokens(msgs) == (2 + 100) * 2 // 7 + 1000

    def test_chat_stream_margin_covers_tools(self):
        """chat_stream传给get_provider的是est*1.05（5%余量），此处验证量级合理"""
        from peng.llm.client import estimate_messages_tokens

        msgs = [{"role": "user", "content": "x" * 3500}]  # ~1000 tokens
        est = estimate_messages_tokens(msgs)
        assert 900 <= est <= 1100
        assert int(est * 1.05) > est

"""额度耗尽确定性硬阻塞回归测试（事务#2666，消息#12582）

需求：429"已达到 5 小时的使用上限。您的限额将在 T 重置"这类确定性错误，
必须把 provider 的额度耗尽状态与恢复时刻**持久化**，让所有事务（含新启动/
restart/resume）直接跳过该 provider，直到恢复时刻——绝不反复发起注定失败的
请求浪费时间与 token。

覆盖：
- parse_quota_reset_time：响应头（retry-after/retry-after-ms/x-ratelimit-reset/
  anthropic-ratelimit-unified-reset）与中英文文案（完整时刻/仅时刻/时长），
  时区歧义自愈、合理窗口拒绝
- 选择逻辑硬跳过 blocked；全 blocked → AllProvidersBlockedError；current 不复用；
  锁定尊重用户意图（等待到恢复时刻）；enable_fallback=False 同样等待
- 持久化：落盘字段完整、新进程继承、双轨合并（最新声明优先、成功解除不被复活）
- get_min_cooldown_remaining 计入硬阻塞；_clean_expired_failures 保留未到期阻塞
"""

import json
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from peng.config.loader import (  # noqa: E402
    QUOTA_BLOCK_MAX_SECONDS,
    AllProvidersBlockedError,
    LLMConfig,
    ProviderConfig,
    parse_quota_reset_time,
)


def _make_cfg() -> LLMConfig:
    """三个启用provider（模拟风主现场：z系额度类 + 兜底provider）"""
    cfg = LLMConfig()
    cfg.providers = {
        "z-glm5.3": ProviderConfig(
            api_key="k", base_url="http://a", model="glm", icon="🦊", enable=True, priority=96
        ),
        "z-glm5.3-flash": ProviderConfig(
            api_key="k", base_url="http://a", model="flash", icon="🐹", enable=True, priority=95
        ),
        "swift-qwen3.8": ProviderConfig(
            api_key="k", base_url="http://b", model="qwen", icon="🐱", enable=True, priority=90
        ),
    }
    cfg._failed_providers_path = None  # 单测默认不落盘（需要时由fixture注入）
    return cfg


class TestParseQuotaResetTime:
    """恢复时刻解析：响应头优先，文案兜底，异常值拒绝"""

    def test_real_chinese_message_with_datetime(self):
        """风主现场原文：'已达到 5 小时的使用上限。您的限额将在 2026-11-10 18:31:28 重置'"""
        now = time.time()
        future = now + 7200
        msg = (
            "请求过于频繁(限流) - 已达到 5 小时的使用上限。"
            f"您的限额将在 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(future))} 重置。"
        )
        epoch, src = parse_quota_reset_time(msg, None, now=now)
        assert epoch is not None
        assert abs(epoch - future) <= 2, f"解析偏差过大: {epoch} vs {future}"
        assert "文案" in src

    def test_english_resets_at(self):
        now = time.time()
        future = now + 1800
        msg = "You have reached your usage limit. Resets at " + time.strftime(
            "%Y-%m-%d %H:%M:%S", time.gmtime(future)
        )
        epoch, _src = parse_quota_reset_time(msg, None, now=now)
        assert epoch is not None
        # 文案为UTC墙钟时本地解释可能落在过去 → 自愈按UTC换算（与future一致）
        assert abs(epoch - future) <= 2 or abs(epoch - (now + 86400)) <= 2

    def test_time_only_roll_forward(self):
        """仅时刻"将在 23:59:01 重置"：按今天推算（已过则明天）"""
        now = time.time()
        target = time.localtime(now + 3600)
        msg = f"您的限额将在 {time.strftime('%H:%M:%S', target)} 重置"
        epoch, src = parse_quota_reset_time(msg, None, now=now)
        assert epoch is not None
        assert epoch > now and epoch - now < 3600 + 120

    def test_duration_forms(self):
        now = time.time()
        e1, _ = parse_quota_reset_time("quota resets in 2 hours", None, now=now)
        assert e1 == pytest.approx(now + 7200, abs=1)
        e2, _ = parse_quota_reset_time("额度将在 30分钟后重置", None, now=now)
        assert e2 == pytest.approx(now + 1800, abs=1)

    def test_header_retry_after_seconds(self):
        epoch, src = parse_quota_reset_time(None, {"Retry-After": "60"}, now=1_000_000.0)
        assert epoch == 1_000_060.0
        assert "retry-after" in src

    def test_header_retry_after_ms(self):
        epoch, src = parse_quota_reset_time(None, {"retry-after-ms": "30000"}, now=1_000_000.0)
        assert epoch == pytest.approx(1_000_030.0, abs=1)  # 毫秒增量
        assert "retry-after-ms" in src

    def test_header_ratelimit_reset_epoch_vs_delta(self):
        now = 1_760_000_000.0
        e1, _ = parse_quota_reset_time(None, {"x-ratelimit-reset": "1760000900"}, now=now)
        assert e1 == 1_760_000_900.0  # >1e9 视为epoch
        e2, _ = parse_quota_reset_time(None, {"x-ratelimit-reset": "900"}, now=now)
        assert e2 == now + 900  # 小值视为剩余秒数
        e3, _ = parse_quota_reset_time(
            None, {"anthropic-ratelimit-unified-reset": str(now + 600)}, now=now
        )
        assert e3 == now + 600

    def test_header_beats_message(self):
        now = time.time()
        msg = "限额将在 2026-11-11 09:00:00 重置"
        epoch, src = parse_quota_reset_time(msg, {"retry-after": "30"}, now=now)
        assert epoch == pytest.approx(now + 30, abs=1)
        assert "响应头" in src

    def test_no_context_keyword_rejected(self):
        """无恢复语义关键词时不敢建立硬阻塞（避免把无关数值当恢复时刻）"""
        epoch, reason = parse_quota_reset_time("请求过于频繁，稍后重试 1234567890", None, now=time.time())
        assert epoch is None
        assert "关键词" in reason

    def test_absurd_window_rejected(self):
        """超出72h合理窗口（两种时区解释都超界）→ None，回落自适应冷却"""
        now = time.time()
        absurd = now + QUOTA_BLOCK_MAX_SECONDS + 100 * 3600
        epoch, _reason = parse_quota_reset_time(
            f"限额将在 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(absurd))} 重置",
            None,
            now=now,
        )
        assert epoch is None

    def test_utc_wallclock_self_heal(self):
        """文案给的是UTC墙钟（本地解释落在过去）时按UTC换算自愈"""
        now = time.time()
        future = now + 3600
        utc_wall = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(future))
        epoch, _src = parse_quota_reset_time(f"限额将在 {utc_wall} 重置", None, now=now)
        assert epoch is not None
        assert epoch > now, "UTC墙钟文案被误判为过去时刻而丢弃"
        assert epoch <= future + 2, f"UTC解释偏差过大: {epoch} vs {future}"

    def test_epoch_in_message(self):
        now = time.time()
        epoch, src = parse_quota_reset_time(
            f"quota exhausted, resets at {int(now + 1200)}", None, now=now
        )
        assert epoch == pytest.approx(now + 1200, abs=1)


class TestSelectionSkipsBlocked:
    """选择逻辑：硬阻塞provider被硬跳过，全阻塞时抛异常等待"""

    def test_blocked_provider_skipped_in_auto_selection(self):
        cfg = _make_cfg()
        cfg.mark_provider_blocked("z-glm5.3", time.time() + 1200, "额度耗尽(429)")
        best = cfg._get_best_available_provider()
        assert best == "z-glm5.3-flash", "额度耗尽的provider仍被选中！"

    def test_all_blocked_returns_none_and_get_provider_raises(self):
        cfg = _make_cfg()
        until = time.time() + 1800
        for p in cfg.providers:
            cfg.mark_provider_blocked(p, until, "额度耗尽(429)")
        assert cfg._get_best_available_provider() is None
        with pytest.raises(AllProvidersBlockedError) as ei:
            cfg.get_provider(current="z-glm5.3", failed=True)
        assert ei.value.earliest_reset == pytest.approx(until, abs=1)
        assert set(ei.value.blocked) == set(cfg.providers)

    def test_blocked_current_not_reused(self):
        """无出错也不复用额度耗尽的current（复用必然再次429）"""
        cfg = _make_cfg()
        cfg.mark_provider_blocked("z-glm5.3", time.time() + 900, "额度耗尽(429)")
        r = cfg.get_provider(current="z-glm5.3", failed=False)
        assert r["provider"] == "z-glm5.3-flash"

    def test_usable_provider_selected_without_waiting(self):
        """部分provider耗尽：直接选可用者，不等待"""
        cfg = _make_cfg()
        cfg.mark_provider_blocked("z-glm5.3", time.time() + 900, "额度耗尽(429)")
        t0 = time.monotonic()
        r = cfg.get_provider(current="z-glm5.3", failed=True)
        assert r["provider"] == "z-glm5.3-flash"
        assert time.monotonic() - t0 < 1.0, "有可用provider时不应等待"

    def test_locked_provider_waits_until_reset(self, monkeypatch):
        """@provider!锁定：尊重显式意图，但等待到恢复时刻（不发起注定失败请求）"""
        cfg = _make_cfg()
        cfg.mark_provider_blocked("z-glm5.3", time.time() + 600, "额度耗尽(429)")
        slept: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
        # loader 内部用模块级 time 模块，patch time.sleep 生效
        r = cfg.get_provider(current="z-glm5.3", favorites=["z-glm5.3"], locked=True)
        assert r["provider"] == "z-glm5.3"
        assert sum(slept) >= 590, f"锁定provider未等待到恢复时刻: {slept}"

    def test_info_query_never_blocks_caller(self):
        """信息型查询（UI显示/单价/compact阈值/预热）传wait_for_usable=False，
        绝不被额度恢复时刻挂住"""
        cfg = _make_cfg()
        cfg.mark_provider_blocked("z-glm5.3", time.time() + 600, "额度耗尽(429)")
        t0 = time.monotonic()
        r = cfg.get_provider(current="z-glm5.3", locked=True, wait_for_usable=False)
        assert r["provider"] == "z-glm5.3"
        assert time.monotonic() - t0 < 1.0, "信息型查询被额度等待挂住！"
        r2 = cfg.get_provider(wait_for_usable=False)
        assert r2["provider"] == "z-glm5.3-flash"
        assert time.monotonic() - t0 < 2.0

    def test_single_provider_mode_raises_instead_of_doomed_request(self):
        cfg = _make_cfg()
        cfg.enable_fallback = False
        cfg.current_provider = "z-glm5.3"
        cfg.mark_provider_blocked("z-glm5.3", time.time() + 600, "额度耗尽(429)")
        with pytest.raises(AllProvidersBlockedError):
            cfg.get_provider(current="z-glm5.3")

    def test_explicit_favorite_blocked_falls_back_to_usable_by_priority(self):
        """显式@指定(非锁定)的provider耗尽：按priority回退可用provider继续工作，
        既不等待也不发起注定失败的请求（锁定!才尊重"就用它"并等待）"""
        cfg = _make_cfg()
        cfg.mark_provider_blocked("swift-qwen3.8", time.time() + 400, "额度耗尽(429)")
        t0 = time.monotonic()
        r = cfg.get_provider(current="z-glm5.3", favorites=["swift-qwen3.8"], locked=False)
        assert r["provider"] == "z-glm5.3", "应回退到priority最高的可用provider"
        assert time.monotonic() - t0 < 1.0, "有可用provider时不应等待"

    def test_vision_preference_skips_blocked_vision_providers(self):
        """多模态请求：耗尽的vision provider不被选中（避免注定429）"""
        cfg = _make_cfg()
        cfg.providers["z-glm5.3"].vision = "yes"
        cfg.providers["swift-qwen3.8"].vision = "yes"
        cfg.mark_provider_blocked("z-glm5.3", time.time() + 600, "额度耗尽(429)")
        best = cfg._get_best_available_provider(prefer_vision=True)
        assert best == "swift-qwen3.8"

    def test_capacity_guard_prefers_unblocked_or_waits(self, monkeypatch):
        """事务#1807与#2666叠加：能力足够者耗尽时等待其恢复时刻，
        绝不切到能力不足者发起注定400超限的请求"""
        cfg = _make_cfg()
        cfg.compact_threshold = 300000
        cfg.providers["swift-qwen3.8"].compact_threshold = 500000
        cfg.providers["z-glm5.3"].compact_threshold = 200000
        cfg.mark_provider_blocked("swift-qwen3.8", time.time() + 600, "额度耗尽(429)")
        slept: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
        r = cfg.get_provider(current="z-glm5.3", failed=True, min_context_tokens=400000)
        assert r["provider"] == "swift-qwen3.8", "应保留能力足够的provider并等待额度恢复"
        assert sum(slept) >= 590, f"未等待到额度恢复时刻: {slept}"

    def test_capacity_guard_switches_to_unblocked_capable(self):
        """能力足够且未阻塞者存在时直接切换（不等待）"""
        cfg = _make_cfg()
        cfg.compact_threshold = 300000
        cfg.providers["swift-qwen3.8"].compact_threshold = 500000
        cfg.providers["z-glm5.3"].compact_threshold = 200000
        cfg.mark_provider_blocked("z-glm5.3-flash", time.time() + 600, "额度耗尽(429)")
        t0 = time.monotonic()
        r = cfg.get_provider(current="z-glm5.3", failed=True, min_context_tokens=400000)
        assert r["provider"] == "swift-qwen3.8"
        assert time.monotonic() - t0 < 1.0


class TestCooldownAndBlockSemantics:
    """冷却与硬阻塞的合并语义"""

    def test_usable_remaining_merges_cooldown_and_block(self):
        """可用时间 = max(启发式冷却剩余, 确定性硬阻塞剩余)；全局取最早恢复者"""
        cfg = _make_cfg()
        cfg._record_failure("z-glm5.3")  # 启发式冷却30秒
        assert 0 < cfg._usable_remaining("z-glm5.3") <= 30
        cfg.mark_provider_blocked("z-glm5.3", time.time() + 600, "额度耗尽(429)")
        assert cfg._usable_remaining("z-glm5.3") > 590, "硬阻塞剩余未计入可用时间"
        # 其余provider立即可用 → 全局无需等待（本次失败可立刻换provider重试）
        assert cfg.get_min_cooldown_remaining() == 0.0
        # 全部耗尽 → 全局等待 = 最早恢复者的剩余（z-glm5.3 ~600s 最早）
        cfg.mark_provider_blocked("z-glm5.3-flash", time.time() + 1200, "额度耗尽(429)")
        cfg.mark_provider_blocked("swift-qwen3.8", time.time() + 900, "额度耗尽(429)")
        assert 590 <= cfg.get_min_cooldown_remaining() <= 905

    def test_usable_other_provider_yields_zero_wait(self):
        cfg = _make_cfg()
        cfg.mark_provider_blocked("z-glm5.3", time.time() + 600, "额度耗尽(429)")
        assert cfg.get_min_cooldown_remaining() == 0.0, "有可用provider时不应等待"

    def test_clean_expired_keeps_unexpired_block(self):
        cfg = _make_cfg()
        cfg.mark_provider_blocked("z-glm5.3", time.time() + 600, "额度耗尽(429)")
        # 冷却已过（把last_failure_time推老）但硬阻塞未到期 → 记录必须保留
        info = cfg._get_failure_info("z-glm5.3")
        info.last_failure_time = time.time() - 7200
        cfg._set_failure_info("z-glm5.3", info)
        cfg._clean_expired_failures()
        assert "z-glm5.3" in cfg.failed_providers, "未到期额度状态被清除！"

    def test_block_expiry_clears_state_and_provider_reusable(self):
        """恢复时刻到 → 硬阻塞解除；此后按既有启发式冷却语义试探"""
        cfg = _make_cfg()
        cfg.mark_provider_blocked("z-glm5.3", time.time() + 0.05, "额度耗尽(429)")
        time.sleep(0.06)
        assert cfg.blocked_remaining("z-glm5.3") == 0.0
        assert cfg.provider_blocked("z-glm5.3") is None
        # 确定性阻塞解除后仍有短暂启发式冷却（不误判为立即可用）
        assert cfg._get_best_available_provider() != "z-glm5.3"
        info = cfg._get_failure_info("z-glm5.3")
        info.last_failure_time = time.time() - info.cooldown - 1
        cfg._set_failure_info("z-glm5.3", info)
        assert cfg._get_best_available_provider() == "z-glm5.3", (
            "硬阻塞与冷却均解除后应重新可用"
        )

    def test_quota_without_reset_time_still_blocks_conservatively(self):
        """无恢复时刻时额度耗尽仍持久化保守冷却（max_cooldown），避免反复撞墙"""
        cfg = _make_cfg()
        cfg.mark_provider_blocked("z-glm5.3", None, "额度耗尽(429)")
        assert cfg.blocked_remaining("z-glm5.3") == 0.0  # 无硬阻塞时刻
        assert cfg._get_best_available_provider() == "z-glm5.3-flash", "保守冷却应生效"
        info = cfg._get_failure_info("z-glm5.3")
        assert info.cooldown == cfg.max_cooldown

    def test_new_declaration_does_not_shorten_block(self):
        """多个事务先后报错：恢复时刻取更晚者（不缩短既有阻塞）"""
        cfg = _make_cfg()
        t = time.time()
        cfg.mark_provider_blocked("z-glm5.3", t + 1800, "事务A额度耗尽")
        cfg.mark_provider_blocked("z-glm5.3", t + 600, "事务B额度耗尽")
        assert cfg.provider_blocked("z-glm5.3")[0] == pytest.approx(t + 1800, abs=1)


class TestPersistenceAcrossProcesses:
    """【风主紧急指示】额度状态与恢复时刻持久化，新进程继承"""

    @pytest.fixture
    def persist_cfg(self, tmp_path):
        cfg = _make_cfg()
        cfg._failed_providers_path = tmp_path / "failed_providers.json"
        return cfg

    def test_mark_blocked_persists_fields(self, persist_cfg):
        until = time.time() + 900
        persist_cfg.mark_provider_blocked("z-glm5.3", until, "额度耗尽(429)，文案重置时刻")
        data = json.loads(persist_cfg._failed_providers_path.read_text(encoding="utf-8"))
        rec = data["z-glm5.3"]
        assert rec["unavailable_until"] == pytest.approx(until, abs=1)
        assert rec["blocked_updated_at"] > 0
        assert "额度耗尽" in rec["blocked_reason"]

    def test_new_process_inherits_block(self, persist_cfg):
        """新启动/restart的事务：load_failed_providers 后直接跳过额度耗尽provider"""
        persist_cfg.mark_provider_blocked("z-glm5.3", time.time() + 900, "额度耗尽(429)")
        cfg2 = _make_cfg()
        cfg2._failed_providers_path = persist_cfg._failed_providers_path
        cfg2.load_failed_providers()
        assert cfg2.blocked_remaining("z-glm5.3") > 890
        assert cfg2._get_best_available_provider() == "z-glm5.3-flash"

    def test_success_clears_block_for_other_processes(self, persist_cfg):
        """成功请求显式解除额度状态：其他进程按最新声明恢复可用，不被旧记录复活"""
        persist_cfg.mark_provider_blocked("z-glm5.3", time.time() + 900, "额度耗尽(429)")
        other = _make_cfg()
        other._failed_providers_path = persist_cfg._failed_providers_path
        other.load_failed_providers()
        assert other.blocked_remaining("z-glm5.3") > 0
        # 另一进程成功（解除声明写盘）
        other.mark_success("z-glm5.3")
        third = _make_cfg()
        third._failed_providers_path = persist_cfg._failed_providers_path
        third.load_failed_providers()
        assert third.blocked_remaining("z-glm5.3") == 0.0, "解除声明被旧阻塞记录复活"

    def test_merge_keeps_newer_quota_declaration(self, persist_cfg):
        """双轨合并：文件的更晚额度声明胜出，内存更晚的冷却记录不覆盖它"""
        until = time.time() + 1200
        persist_cfg.mark_provider_blocked("z-glm5.3", until, "进程A声明")
        stale = _make_cfg()
        stale._failed_providers_path = persist_cfg._failed_providers_path
        stale.load_failed_providers()
        # 内存里该provider只有"刚失败"的冷却记录（last_failure_time 更新，额度声明陈旧）
        stale._record_failure("z-glm5.3")
        stale.save_failed_providers()
        data = json.loads(persist_cfg._failed_providers_path.read_text(encoding="utf-8"))
        assert data["z-glm5.3"]["unavailable_until"] == pytest.approx(until, abs=1), (
            "陈旧进程把已持久化的额度恢复时刻覆盖掉了"
        )

    def test_lock_file_created_and_stable(self, persist_cfg):
        """锁文件存在且永不删除（inode稳定保证互斥）"""
        persist_cfg.save_failed_providers()
        lock = persist_cfg._failed_providers_path.with_name("failed_providers.json.lock")
        assert lock.exists()
        persist_cfg.save_failed_providers()
        assert lock.exists()

    def test_live_process_sees_sibling_declaration_without_reload(self, persist_cfg):
        """长驻进程不重启也能即时感知其他进程落盘的额度声明（惰性刷新）"""
        persist_cfg.mark_provider_blocked("z-glm5.3", time.time() + 900, "额度耗尽(429)")
        live = _make_cfg()
        live._failed_providers_path = persist_cfg._failed_providers_path
        # 不调用 load_failed_providers：靠 _maybe_reload_failed_providers 感知
        assert live.blocked_remaining("z-glm5.3") > 890, "本进程未感知其他进程的额度声明"
        assert live._get_best_available_provider() == "z-glm5.3-flash"

    def test_sibling_success_clears_local_stale_block(self, persist_cfg):
        """其他进程成功后解除声明更晚 → 本进程陈旧阻塞被解除，不被复活"""
        persist_cfg.mark_provider_blocked("z-glm5.3", time.time() + 900, "额度耗尽(429)")
        live = _make_cfg()
        live._failed_providers_path = persist_cfg._failed_providers_path
        assert live.blocked_remaining("z-glm5.3") > 0
        # 另一进程成功解除（写盘，blocked_updated_at 更新）
        other = _make_cfg()
        other._failed_providers_path = persist_cfg._failed_providers_path
        other.load_failed_providers()
        other.mark_success("z-glm5.3")
        # 本进程强制越过刷新窗口再查
        live._failed_reload_at = 0.0
        assert live.blocked_remaining("z-glm5.3") == 0.0, "陈旧阻塞声明未被最新解除声明覆盖"


class TestToolEngineQuotaStatePersistence:
    """【风主紧急指示】tool_engine 遇额度耗尽必须把状态与恢复时刻持久化"""

    def _make_engine(self, cfg):
        from peng.llm.tool_engine import ToolExecutionEngine

        eng = object.__new__(ToolExecutionEngine)
        eng.llm = types.SimpleNamespace(provider="z-glm5.3", config=cfg)
        return eng

    def test_mark_provider_blocked_persists_until_and_reason(self, tmp_path):
        import json as _json

        cfg = _make_cfg()
        cfg._failed_providers_path = tmp_path / "failed_providers.json"
        eng = self._make_engine(cfg)
        until = time.time() + 1800
        assert eng._mark_provider_blocked(until, "额度耗尽(429)，文案重置时刻") is True
        rec = _json.loads(cfg._failed_providers_path.read_text(encoding="utf-8"))["z-glm5.3"]
        assert rec["unavailable_until"] == pytest.approx(until, abs=1)
        assert "额度耗尽" in rec["blocked_reason"]
        # 其他进程（新启动/restart的事务）继承该状态并跳过该provider
        other = _make_cfg()
        other._failed_providers_path = cfg._failed_providers_path
        other.load_failed_providers()
        assert other.blocked_remaining("z-glm5.3") > 1790
        assert other._get_best_available_provider() == "z-glm5.3-flash"

    def test_mark_without_reset_time_still_persisted(self, tmp_path):
        """无恢复时刻也持久化：保守冷却落盘，避免其他事务反复撞墙"""
        import json as _json

        cfg = _make_cfg()
        cfg._failed_providers_path = tmp_path / "failed_providers.json"
        eng = self._make_engine(cfg)
        eng._mark_provider_blocked(None, "额度耗尽(429)")
        rec = _json.loads(cfg._failed_providers_path.read_text(encoding="utf-8"))["z-glm5.3"]
        assert rec["cooldown"] == cfg.max_cooldown
        assert "额度耗尽" in rec["blocked_reason"]

    def test_mark_blocked_survives_config_errors(self):
        """持久化异常不得击穿请求处理流程（降级返回False，事务继续）"""

        def boom(*_a, **_k):
            raise RuntimeError("持久化失败")

        cfg = types.SimpleNamespace(mark_provider_blocked=boom)
        eng = self._make_engine(cfg)
        assert eng._mark_provider_blocked(time.time() + 100, "额度耗尽(429)") is False

    def test_mark_blocked_no_provider_or_no_config_is_noop(self):
        eng = self._make_engine(_make_cfg())
        eng.llm.provider = ""
        assert eng._mark_provider_blocked(time.time() + 100, "额度耗尽(429)") is False
        eng.llm.provider = "z-glm5.3"
        eng.llm.config = None
        assert eng._mark_provider_blocked(time.time() + 100, "额度耗尽(429)") is False


class TestClientWarmupGraceful:
    """预热路径遇全provider硬阻塞：不发起请求、不抛异常污染事务状态"""

    def test_refresh_provider_vars_survives_all_blocked(self, monkeypatch):
        import types

        import peng.llm.client as client_mod

        cfg = _make_cfg()
        for p in cfg.providers:
            cfg.mark_provider_blocked(p, time.time() + 600, "额度耗尽(429)")
        monkeypatch.setattr(client_mod, "load_config", lambda: types.SimpleNamespace(llm=cfg))
        client = object.__new__(client_mod.GLMClient)
        client.provider = None
        client.compact_threshold = 320000
        client.thinking_mode = "auto"
        client.thinking_type = None
        client.provider_config = None
        client.refresh_provider_vars()  # 不应抛异常
        assert client.provider is None, "预热不应擅自选定额度耗尽的provider"

    @pytest.mark.asyncio
    async def test_chat_stream_raises_blocked_before_any_request(self, monkeypatch):
        """chat_stream：全provider额度耗尽时抛 AllProvidersBlockedError，
        且绝不建立HTTP连接（零注定失败请求）"""
        import types

        import peng.llm.client as client_mod

        cfg = _make_cfg()
        for p in cfg.providers:
            cfg.mark_provider_blocked(p, time.time() + 600, "额度耗尽(429)")

        requested: list = []

        def fake_client(*args, **kwargs):
            requested.append(kwargs)
            raise AssertionError("硬阻塞期间不应创建HTTP client")

        monkeypatch.setattr(client_mod.httpx, "Client", fake_client)
        # GLMClient.config 是动态属性（load_config().llm）→ patch load_config 注入配置
        monkeypatch.setattr(client_mod, "load_config", lambda: types.SimpleNamespace(llm=cfg))

        task_manager = types.SimpleNamespace(
            get_task=lambda task_id=None: {"content": "测试任务"},
            update_heartbeat=lambda tid: True,
        )
        message_manager = types.SimpleNamespace(get_task_manager=lambda: task_manager)

        client = object.__new__(client_mod.GLMClient)
        client.provider = None
        client.error = ""
        client.compact_threshold = 320000
        client.thinking_mode = "auto"
        client.thinking_type = None
        client.provider_config = None
        client._vision_switch_from = None
        client._vision_switch_used = 0
        client._recent_error_sigs = {}
        client._error_dedup_rounds = 5

        stream = client.chat_stream(
            task_id=1,
            messages=[{"role": "user", "content": "你好"}],
            message_manager=message_manager,
            tools=None,
        )
        with pytest.raises(AllProvidersBlockedError) as ei:
            await stream.__anext__()
        assert not requested, "硬阻塞期间仍发起了HTTP请求"
        assert ei.value.earliest_reset > time.time()

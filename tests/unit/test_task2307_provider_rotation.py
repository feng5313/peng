"""事务#2307 回归测试：优先级相同的多个provider轮流使用（消息#10863）

需求（风主原话拆解）：
1. 优先级相同的多个provider应该**轮流使用**；
2. restart/start新启动的事务，**优先选择比较空闲的provider**（空闲度=DB各provider
   上活跃事务数——风主实测场景：iq3有2个、wen有1个（他自己强制指定），下一个
   restart/start新启动的事务应该选wen）；
3. resume继续运行的事务**默认不换provider**，除非不可用。

架构：空闲度主信号=DB tasks聚合（跨进程真值，涵盖强制指定/锁定/粘性/自动所有
路径）；同空闲度时按共享LRU账本轮换（data/provider_balance.json + flock）。
单元测试不依赖真实DB数值——fixture把active_counts桩为{}（纯LRU模式），
DB驱动行为由专门用例注入计数验证。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from peng.config.loader import LLMConfig, ProviderConfig  # noqa: E402
from peng.llm import provider_balance as pb  # noqa: E402


def _cfg_tier_same() -> LLMConfig:
    """三个同优先级provider（轮换/空闲度测试）"""
    cfg = LLMConfig()
    cfg.providers = {
        "p-alpha": ProviderConfig(api_key="k", base_url="http://a", model="m1", priority=90),
        "p-beta": ProviderConfig(api_key="k", base_url="http://a", model="m2", priority=90),
        "p-gamma": ProviderConfig(api_key="k", base_url="http://a", model="m3", priority=90),
    }
    return cfg


@pytest.fixture
def balance(tmp_path, monkeypatch):
    """隔离账本实例：账本目录→tmp_path，DB聚合桩为{}（纯LRU模式，测试确定性）

    provider_balance是模块级单例且loader通过get_provider_balance()取用，必须
    patch 才不读写真实data/目录与真实DB。
    """
    monkeypatch.setattr(pb, "_get_data_dir", lambda: tmp_path)
    instance = pb.ProviderBalance()
    monkeypatch.setattr(instance, "active_counts", lambda use_cache=True: {})
    monkeypatch.setattr(pb, "get_provider_balance", lambda: instance)
    return instance


class TestLedgerLRURotation:
    """纯LRU模式（同空闲度）：新分配推进LRU，实现轮流使用"""

    def test_concurrent_new_tasks_round_robin(self, balance):
        cfg = _cfg_tier_same()
        chosen = [cfg._get_best_available_provider(task_id=tid) for tid in (601, 602, 603)]
        assert len(set(chosen)) == 3, f"同优先级应轮流使用，实际: {chosen}"

    def test_fourth_picks_least_recently_assigned(self, balance):
        """前三个tier成员各分配一个后，第四个事务应拿LRU最早者（实现真轮转）"""
        cfg = _cfg_tier_same()
        first = [cfg._get_best_available_provider(task_id=t) for t in (701, 702, 703)]
        fifth = cfg._get_best_available_provider(task_id=704)
        assert fifth == first[0], f"轮转应回到最早分配的{first[0]}，实际{fifth}"

    def test_record_advances_lru(self, balance):
        balance.record_assignment("p-alpha", task_id=801)
        ranked = balance.rank(["p-alpha", "p-beta"])
        assert ranked[0] == "p-beta", "刚被分配的provider应后移（LRU）"

    def test_no_task_id_no_ledger_record(self, balance):
        """无task_id（UI展示等）不推进账本"""
        cfg = _cfg_tier_same()
        cfg._get_best_available_provider(task_id=None)
        assert balance.snapshot() == {}

    def test_shared_ledger_visible_across_instances(self, balance, monkeypatch, tmp_path):
        """跨进程视角：另一实例（模拟别的runner进程）写的账本立即可见"""
        other = pb.ProviderBalance()
        other._load_ledger = lambda use_cache=True: balance._load_ledger(use_cache=False)
        monkeypatch.setattr(other, "active_counts", lambda use_cache=True: {})
        other.record_assignment("p-alpha", task_id=901)  # 写入共享文件（flock保护）
        # balance实例缓存失效后从文件读到p-alpha已分配 → 排序应把p-alpha排后
        ranked = balance.rank(["p-alpha", "p-beta"], task_id=902)
        assert ranked[0] == "p-beta"


class TestDbDrivenIdlePreference:
    """空闲度主信号=DB活跃事务聚合（风主实测场景驱动）"""

    def test_windmaster_scenario_prefers_idle(self, balance):
        """风主场景：本tier两个provider，一个已被占2个事务、一个0 → 新事务选空闲的"""
        cfg = _cfg_tier_same()
        monkey_counts = {"p-alpha": 2}  # p-alpha已挂2个活跃事务，p-beta/p-gamma空闲
        monkeypatch_balance = balance
        monkeypatch_balance.active_counts = lambda use_cache=True: dict(monkey_counts)
        chosen = cfg._get_best_available_provider(task_id=1001)
        assert chosen in ("p-beta", "p-gamma"), f"应选空闲provider，实际{chosen}"

    def test_fewer_active_wins(self, balance):
        balance.active_counts = lambda use_cache=True: {"p-alpha": 3, "p-beta": 0}
        ranked = balance.rank(["p-alpha", "p-beta"], task_id=1010)
        assert ranked[0] == "p-beta", "活跃事务数少的provider优先（需求：优先选择比较空闲的）"

    def test_raw_db_load_decides_no_deduction(self, tmp_path, monkeypatch):
        """实测教训用例：排序必须用原始DB负载（曾有"扣除自己"优化，风主场景实测
        直接失效——current=iq3(2-1=1)与wen(1)打平退化名字序永远选iq3）"""
        monkeypatch.setattr(pb, "_get_data_dir", lambda: tmp_path)
        b = pb.ProviderBalance()
        b.active_counts = lambda use_cache=True: {"p-beta": 1, "p-alpha": 2}
        # p-beta原始负载1 < p-alpha原始负载2 → p-beta胜（不管调用者"自己"在哪个）
        assert b.rank(["p-alpha", "p-beta"], task_id=1020)[0] == "p-beta"

    def test_degrade_to_lru_when_db_unavailable(self, balance):
        """DB不可用时（active_counts返回{}）降级为纯LRU轮换，不崩"""
        cfg = _cfg_tier_same()
        chosen = [cfg._get_best_available_provider(task_id=t) for t in (1031, 1032)]
        assert chosen[0] != chosen[1], "纯LRU下仍应轮换"

    def test_priority_tier_preserved(self, balance):
        """tier间priority降序优先，轮换只发生在同tier内"""
        cfg = LLMConfig()
        cfg.providers = {
            "hi-a": ProviderConfig(api_key="k", base_url="http://a", model="m", priority=95),
            "hi-b": ProviderConfig(api_key="k", base_url="http://a", model="m", priority=95),
            "lo-c": ProviderConfig(api_key="k", base_url="http://a", model="m", priority=90),
        }
        order = cfg.get_shifts()
        assert set(order[:2]) == {"hi-a", "hi-b"}
        assert order[2] == "lo-c"

    def test_rotation_disabled_keeps_legacy(self, balance):
        cfg = _cfg_tier_same()
        cfg.provider_rotation = False
        balance.record_assignment("p-alpha", task_id=1040)
        assert cfg.get_shifts() == ["p-alpha", "p-beta", "p-gamma"], "开关关闭=旧行为"


class TestGetProviderSticky:
    """get_provider层：resume粘性默认不换（除非不可用），新启动走空闲轮换"""

    def test_resume_sticky_current_kept(self, balance):
        cfg = _cfg_tier_same()
        # p-gamma上挂着别的事务（不空闲），但p-gamma是本事务current → 默认不换
        balance.active_counts = lambda use_cache=True: {"p-gamma": 2}
        r = cfg.get_provider(current="p-gamma", favorites=None, locked=False, task_id=1101)
        assert r["provider"] == "p-gamma", "无错不切换：resume默认不换provider"

    def test_sticky_unavailable_switches_to_idle(self, balance):
        cfg = _cfg_tier_same()
        cfg.providers["p-gamma"].enable = False  # 粘性目标被禁用 → 不可用
        balance.active_counts = lambda use_cache=True: {"p-beta": 2}
        r = cfg.get_provider(current="p-gamma", favorites=None, locked=False, task_id=1102)
        assert r["provider"] in ("p-alpha", "p-gamma") and r["provider"] != "p-gamma"

    def test_explicit_at_provider_beats_rotation(self, balance):
        """content显式@provider用户意图优先于空闲轮换（事务#1793语义不变）"""
        cfg = _cfg_tier_same()
        cfg.providers["p-beta"].icon = "🐱"
        balance.active_counts = lambda use_cache=True: {"p-beta": 5}  # 强制指定再忙也用
        r = cfg.get_provider(current=None, favorites=["p-beta"], locked=False, task_id=1103)
        assert r["provider"] == "p-beta"


class TestManagerStickyLifecycle:
    """manager层：restart/start清粘性（重新均衡分配），resume保留粘性（默认不换）"""

    class FakeDB:
        def __init__(self, task):
            self.task = task
            self.updated_kwargs: dict = {}

        def get_task(self, tid):
            return self.task

        def update_task(self, tid, **kw):
            self.updated_kwargs = kw
            return True

        def get_descendants(self, tid):
            return []

        def _notify_webui_via_http(self, *a, **k):
            pass

        def _parse_provider_from_content(self, content: str) -> str:
            """与TaskDatabase._parse_provider_from_content同逻辑（真实解析@provider）"""
            if not content:
                return ""
            from peng.config.loader import load_config

            fav = load_config().llm.fetch_providers(content)
            return fav[0][0] if fav and fav[0] else ""

    def _make_manager(self, task):
        from peng.task.manager import TaskManager

        tm = object.__new__(TaskManager)  # 跳过MetaSingleton __init__（避免连真实DB）
        tm.db = self.FakeDB(task)
        tm._unarchive_task_and_root_message = lambda tid: None
        tm._sync_task_status_to_message = lambda *a, **k: None
        tm._execute_task_process = lambda tid: 12345
        return tm

    @pytest.mark.asyncio
    async def test_restart_clears_sticky(self):
        """restart（keep_context=False）：provider重置为content显式意图（无则空→重新分配）"""
        tm = self._make_manager(
            {
                "id": 901,
                "content": "干活",
                "status": "completed",
                "context": "",
                "provider": "p-beta",
            }
        )
        await tm.restart_task(901, keep_context=False)
        assert tm.db.updated_kwargs.get("provider") == "", "全新重启应清除粘性provider记录"

    @pytest.mark.asyncio
    async def test_restart_keeps_explicit_at_provider(self):
        """content显式@provider（🐱真实config图标）→ 清除后仍尊重用户意图"""
        tm = self._make_manager(
            {"id": 902, "content": "@🐱 干活", "status": "completed", "context": "", "provider": ""}
        )
        await tm.restart_task(902, keep_context=False)
        from peng.config.loader import load_config

        fav = load_config().llm.fetch_providers("@🐱 干活")
        expected = fav[0][0] if fav and fav[0] else ""
        assert tm.db.updated_kwargs.get("provider") == expected

    @pytest.mark.asyncio
    async def test_resume_keeps_sticky(self):
        """resume（keep_context=True）：update不含provider键→DB粘性记录保留（默认不换）"""
        tm = self._make_manager(
            {"id": 903, "content": "干活", "status": "paused", "context": "", "provider": "p-beta"}
        )
        await tm.restart_task(903, keep_context=True)
        assert "provider" not in tm.db.updated_kwargs, "resume不应清除粘性provider"


class TestWarmupStickyRestore:
    """tool_engine首轮预热：从DB provider恢复粘性（resume默认不换）"""

    class FakeLLM:
        """模拟GLMClient最小面（provider + refresh_provider_vars走真实config逻辑）"""

        def __init__(self, cfg):
            self.provider = None
            self.config = cfg

        def refresh_provider_vars(self, content=None):
            favs = None
            if content:
                res = self.config.fetch_providers(content)
                if res:
                    favs = res[0]
            data = self.config.get_provider(current=self.provider, favorites=favs, locked=False)
            if data:
                self.provider = data["provider"]

    @staticmethod
    def _warmup(llm, db_provider: str, content: str = "") -> str:
        """复刻tool_engine首轮预热路径（事务#2307新增段）：DB粘性provider→client→refresh"""
        if llm.provider is None:
            sticky = db_provider or ""
            if sticky and sticky in llm.config.providers:
                llm.provider = sticky
            llm.refresh_provider_vars(content=content)
        return llm.provider

    def test_resume_keeps_db_provider(self, balance):
        """DB粘性记录存在 → warmup恢复它（即使别的provider更空闲）"""
        cfg = _cfg_tier_same()
        # 其他两个provider各有2个活跃事务，p-alpha是粘性记录（本事务一直用的）
        balance.active_counts = lambda use_cache=True: {"p-beta": 2, "p-gamma": 2}
        llm = self.FakeLLM(cfg)
        assert self._warmup(llm, "p-alpha") == "p-alpha", "resume默认不换provider（粘性）"

    def test_fresh_start_picks_idle(self, balance):
        """DB provider为空（全新start/restart）→ 优先选最空闲的"""
        cfg = _cfg_tier_same()
        balance.active_counts = lambda use_cache=True: {"p-alpha": 3, "p-beta": 2}
        llm = self.FakeLLM(cfg)
        assert self._warmup(llm, "") == "p-gamma", "全新启动应选最空闲的provider"

    def test_sticky_unavailable_reselects_by_idle(self, balance):
        """粘性provider不可用（未启用）→ 落入空闲优先轮换"""
        cfg = _cfg_tier_same()
        cfg.providers["p-alpha"].enable = False
        balance.active_counts = lambda use_cache=True: {"p-beta": 2}
        llm = self.FakeLLM(cfg)
        assert self._warmup(llm, "p-alpha") in ("p-gamma",), "不可用时应换最空闲的provider"

    def test_explicit_marker_beats_sticky_and_idle(self, balance):
        """content显式@provider意图优先（与粘性/轮换都冲突时用户意图胜出）"""
        cfg = _cfg_tier_same()
        cfg.providers["p-beta"].icon = "🐱"
        balance.active_counts = lambda use_cache=True: {"p-beta": 5}
        llm = self.FakeLLM(cfg)
        assert self._warmup(llm, "p-alpha", content="@🐱 干活") == "p-beta"

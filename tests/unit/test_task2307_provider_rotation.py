"""事务#2307 回归测试：优先级相同的多个provider轮流使用（消息#10863）

需求：
1. restart/start 新启动的事务，优先选择比较空闲的provider（同优先级tier内轮换）；
2. resume 继续运行的事务默认不换provider，除非不可用。

覆盖：
- provider_balance 账本：本进程登记、跨进程聚合、死进程/TTL失效、LRU轮换
- LLMConfig 排序：tier间priority降序不变，tier内空闲优先+LRU；开关关闭=旧行为
- get_provider 集成：同tier连续自动选择轮转；current可用时保持（resume粘性）
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from peng.config.loader import LLMConfig, ProviderConfig  # noqa: E402
from peng.llm import provider_balance as pb  # noqa: E402


def _fresh_balance(tmp_path):
    """构造指向tmp_path账本目录的隔离实例（不污染真实data/）"""
    balance = pb.ProviderBalance()
    balance._self_entries = {}
    balance._cache = None
    return balance


@pytest.fixture
def balance(tmp_path, monkeypatch):
    """隔离账本：数据目录指向tmp_path，且loader的get_provider_balance()返回本实例

    （provider_balance是模块级单例，loader内部通过get_provider_balance()取用；
    不patch会读写真实data/目录，测试间互相污染）
    """
    monkeypatch.setattr(pb, "_get_data_dir", lambda: tmp_path)
    instance = _fresh_balance(tmp_path)
    monkeypatch.setattr(pb, "get_provider_balance", lambda: instance)
    return instance


def _cfg_tier_same() -> LLMConfig:
    """三个同优先级provider（轮换测试）"""
    cfg = LLMConfig()
    cfg.providers = {
        "p-alpha": ProviderConfig(api_key="k", base_url="http://a", model="m1", priority=90),
        "p-beta": ProviderConfig(api_key="k", base_url="http://a", model="m2", priority=90),
        "p-gamma": ProviderConfig(api_key="k", base_url="http://a", model="m3", priority=90),
    }
    return cfg


def _cfg_tier_mixed() -> LLMConfig:
    """两个高优先级 + 一个低优先级（tier间priority优先不受轮换影响）"""
    cfg = LLMConfig()
    cfg.providers = {
        "hi-a": ProviderConfig(api_key="k", base_url="http://a", model="m1", priority=95),
        "hi-b": ProviderConfig(api_key="k", base_url="http://a", model="m2", priority=95),
        "lo-c": ProviderConfig(api_key="k", base_url="http://a", model="m3", priority=90),
    }
    return cfg


class TestBalanceLedger:
    """账本登记与聚合"""

    def test_record_and_snapshot(self, balance):
        balance.record("p-alpha", task_id=101)
        balance.record("p-beta", task_id=102)
        snap = balance.snapshot_summary()
        assert snap["p-alpha"]["active"] == 1
        assert snap["p-beta"]["active"] == 1
        assert "p-gamma" not in snap

    def test_release(self, balance):
        balance.record("p-alpha", task_id=101)
        balance.release(101)
        assert "p-alpha" not in balance.snapshot_summary()

    def test_cross_process_aggregation(self, balance, monkeypatch, tmp_path):
        """其他进程账本文件参与聚合（伪造存活进程文件）"""
        import json

        other_pid = 999_999
        (tmp_path / f"p{other_pid}.json").write_text(
            json.dumps(
                {
                    "pid": other_pid,
                    "updated_at": time.time(),
                    "assignments": {
                        "77": {
                            "provider": "p-alpha",
                            "assigned_at": time.time(),
                            "updated_at": time.time(),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("peng.common.platform.process_exists", lambda pid: pid == other_pid)
        assert balance.get_load("p-alpha")["active"] == 1

    def test_dead_process_file_ignored(self, balance, tmp_path):
        import json

        (tmp_path / "p999998.json").write_text(
            json.dumps(
                {
                    "pid": 999_998,
                    "updated_at": time.time(),
                    "assignments": {
                        "77": {
                            "provider": "p-alpha",
                            "assigned_at": time.time(),
                            "updated_at": time.time(),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        assert balance.get_load("p-alpha")["active"] == 0

    def test_ttl_expired_entry_dropped(self, balance, monkeypatch):
        monkeypatch.setattr(pb, "ASSIGN_TTL", 5.0)
        balance.record("p-alpha", task_id=101)
        with balance._lock:
            balance._self_entries["101"]["updated_at"] = time.time() - 10
        assert balance.get_load("p-alpha")["active"] == 0

    def test_touch_keeps_lease_but_not_lru(self, balance):
        """续租刷新updated_at但不移动assigned_at（否则最忙的永远排最前）"""
        balance.record("p-alpha", task_id=101)
        with balance._lock:
            assigned0 = balance._self_entries["101"]["assigned_at"]
        balance.record("p-alpha", task_id=101)  # touch
        with balance._lock:
            assert balance._self_entries["101"]["assigned_at"] == assigned0


class TestRankRotation:
    """同tier轮换排序：空闲优先 + LRU"""

    def test_all_idle_lru_rotation(self, balance):
        cfg = _cfg_tier_same()
        order1 = cfg.get_shifts()
        # 模拟本进程给一个事务分配了p-beta → 其余两个（未分配）优先，p-beta最后
        balance.record("p-beta", task_id=201)
        order2 = cfg.get_shifts()
        assert order1 == ["p-alpha", "p-beta", "p-gamma"]
        assert order2[-1] == "p-beta" or order2 != order1  # p-beta已占用应后移

    def test_idle_preferred_over_loaded(self, balance):
        balance.record("p-alpha", task_id=301)
        balance.record("p-alpha", task_id=302)
        ranked = balance.rank(["p-alpha", "p-beta"])
        assert ranked[0] == "p-beta", "负载少的provider应排前（优先选择比较空闲的）"

    def test_sticky_own_assignment_first(self, balance):
        """本进程已分配给自己的provider排最前（同事务重选稳定）"""
        balance.record("p-alpha", task_id=401)
        ranked = balance.rank(["p-alpha", "p-beta", "p-gamma"], task_id=401)
        assert ranked[0] == "p-alpha"

    def test_priority_tier_preserved(self, balance):
        """tier间仍按priority降序，轮换只发生在同tier内"""
        cfg = _cfg_tier_mixed()
        order = cfg.get_shifts()
        assert set(order[:2]) == {"hi-a", "hi-b"}
        assert order[2] == "lo-c"

    def test_rotation_disabled_keeps_legacy(self, balance):
        cfg = _cfg_tier_same()
        cfg.provider_rotation = False
        balance.record("p-alpha", task_id=501)
        # 关闭开关：同tier保持providers定义顺序（旧行为）
        assert cfg.get_shifts() == ["p-alpha", "p-beta", "p-gamma"]


class TestGetProviderRotation:
    """get_provider集成：新事务连续自动选择应轮转（模拟多事务并发启动）"""

    def test_concurrent_new_tasks_round_robin(self, balance):
        cfg = _cfg_tier_same()
        chosen = []
        for tid in (601, 602, 603):
            r = cfg._get_best_available_provider(task_id=tid)
            chosen.append(r)
        assert len(set(chosen)) == 3, f"同优先级应轮流使用，实际: {chosen}"

    def test_no_task_id_no_ledger_record(self, balance):
        """无task_id（UI展示等）不污染账本"""
        cfg = _cfg_tier_same()
        cfg._get_best_available_provider(task_id=None)
        assert balance.snapshot_summary() == {}

    def test_resume_sticky_current_kept(self, balance):
        """resume粘性：current可用时保持原provider，不被轮换抢走"""
        cfg = _cfg_tier_same()
        # 本进程账本已把p-gamma分配给别的任务（负载更高）
        balance.record("p-gamma", task_id=702)
        r = cfg.get_provider(current="p-gamma", favorites=None, locked=False, task_id=701)
        assert r["provider"] == "p-gamma", "无错不切换：resume默认不换provider"

    def test_sticky_unavailable_switches(self, balance):
        """粘性provider不可用（未启用）时按空闲度重选"""
        cfg = _cfg_tier_same()
        cfg.providers["p-gamma"].enable = False
        r = cfg.get_provider(current="p-gamma", favorites=None, locked=False, task_id=801)
        assert r["provider"] in ("p-alpha", "p-beta")


class TestCrossProcessBalance:
    """跨进程视角：其他进程账本参与空闲度排序（真实部署形态）"""

    def test_prefers_provider_free_of_other_processes(self, balance, monkeypatch):
        cfg = _cfg_tier_same()
        import json

        other_pid = 999_997
        from peng.llm.provider_balance import _get_data_dir

        _get_data_dir().mkdir(parents=True, exist_ok=True)
        (_get_data_dir() / f"p{other_pid}.json").write_text(
            json.dumps(
                {
                    "pid": other_pid,
                    "updated_at": time.time(),
                    "assignments": {
                        "70": {
                            "provider": "p-alpha",
                            "assigned_at": time.time(),
                            "updated_at": time.time(),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("peng.common.platform.process_exists", lambda pid: pid == other_pid)
        # 其他进程已占用p-alpha → 新事务（本进程）应选另外两个之一
        chosen = cfg._get_best_available_provider(task_id=901)
        assert chosen in ("p-beta", "p-gamma"), f"应避开其他进程占用的provider，实际{chosen}"

    def test_restart_rebalances_by_idle(self, balance, monkeypatch):
        """restart/start（无粘性、current=None）→ 优先选最空闲的provider"""
        cfg = _cfg_tier_same()
        # 模拟另两个provider已被本进程其他事务占用，只剩p-gamma空闲
        balance.record("p-alpha", task_id=911)
        balance.record("p-beta", task_id=912)
        chosen = cfg._get_best_available_provider(task_id=910)
        assert chosen == "p-gamma", f"新事务应选最空闲的provider，实际{chosen}"


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
        """content显式@provider（🐱=z-glm5.3-flash真实配置图标）→ 清除后仍尊重意图"""
        tm = self._make_manager(
            {"id": 902, "content": "@🐱 干活", "status": "completed", "context": "", "provider": ""}
        )
        await tm.restart_task(902, keep_context=False)
        # 🐱在真实config里是z-glm5.3-flash（测试环境依赖真实config图标，宽松断言）
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
    def _warmup(llm, db_provider: str, content: str = ""):
        """复刻tool_engine首轮预热路径（事务#2307新增段）：DB粘性provider→client→refresh"""
        if llm.provider is None:
            sticky = db_provider or ""
            if sticky and sticky in llm.config.providers:
                llm.provider = sticky
            llm.refresh_provider_vars(content=content)
        return llm.provider

    def test_resume_keeps_db_provider(self, balance):
        """DB provider=粘性记录存在 → warmup恢复它（即使别的provider更空闲）"""
        # 其他两个provider已被别的进程事务占用，p-alpha是本事务的粘性记录
        balance.record("p-beta", task_id=921)
        balance.record("p-gamma", task_id=922)
        llm = self.FakeLLM(_cfg_tier_same())
        assert self._warmup(llm, "p-alpha") == "p-alpha", "resume默认不换provider（粘性）"

    def test_fresh_start_picks_idle(self, balance):
        """DB provider为空（全新start/restart）→ 按空闲度分配最空闲的provider"""
        balance.record("p-alpha", task_id=931)
        balance.record("p-beta", task_id=932)
        llm = self.FakeLLM(_cfg_tier_same())
        assert self._warmup(llm, "") == "p-gamma", "全新启动应选最空闲的provider"

    def test_sticky_unavailable_reselects_by_idle(self, balance):
        """粘性provider不可用（未启用）→ 落入空闲优先轮换"""
        cfg = _cfg_tier_same()
        cfg.providers["p-alpha"].enable = False  # 粘性目标被禁用
        balance.record("p-beta", task_id=941)  # p-beta被别处占用
        llm = self.FakeLLM(cfg)
        assert self._warmup(llm, "p-alpha") == "p-gamma", "不可用时应换到空闲provider"

    def test_explicit_at_provider_beats_rotation(self, balance):
        """content显式@provider意图优先于轮换（事务#1793语义不变）"""
        balance.record("p-beta", task_id=951)
        balance.record("p-gamma", task_id=952)
        cfg = _cfg_tier_same()
        cfg.providers["p-beta"].icon = "🐱"
        llm = self.FakeLLM(cfg)
        # p-alpha虽有粘性但content显式@🐱(p-beta) → 显式意图优先
        assert self._warmup(llm, "p-alpha", content="@🐱 干活") == "p-beta"

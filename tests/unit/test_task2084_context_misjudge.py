#!/usr/bin/env python3
"""事务#2084：伪上下文超标识别与服务端异常死循环防护

背景（事务#2080事故）：
epyc自研Strata推理服务在引擎未就绪/重载中时返回：
  "HTTP 400 请求参数错误 - prompt (9799 tokens) + max tokens (32000)
   exceeds the context (0); requests are never truncated"
——服务端声称context=0（/v1/models实际报告n_ctx=262144）。

peng侧误判链条（根因）：
1. CONTEXT_LENGTH_KEYWORDS含"max tokens"，错误消息中"max tokens (32000)"
   误匹配 → 判定"上下文长度超标"
2. compact 774→774无效（上下文远小于阈值225000，无压缩空间）
3. 3轮后restart → 历史清空重新请求 → 服务端依然context(0) → 无限死循环
   （实测93次restart，持续20分钟）

修复（三层防御）：
- Fix1: extract_server_context_limit解析服务端声称的context值，<1024判定
  服务端引擎未就绪/状态异常 → 走普通4xx路径（切provider/冷却等待，保活）
- Fix2: compact矛盾判定（orig==after且after+max_tokens<threshold → 数学上
  自相矛盾 → 服务端异常路径，不restart不清历史）
- Fix3: _engine_restart_with_breaker熔断（15分钟内第3次引擎自触发restart
  改为pause+明确诊断，杜绝死循环烧钱）

根因（服务端侧，事务#2081持续调查）：Strata server引擎加载40GB模型期间
（E盘IO被百度网盘吃死时长达14分钟）OpenAI接口返回context(0)。
"""

import sys
from pathlib import Path

# 项目根目录加入 sys.path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from peng.llm.tool_engine import (
    CONTEXT_LENGTH_KEYWORDS,
    SERVER_CONTEXT_LIMIT_SANITY_FLOOR,
    ToolExecutionEngine,
    extract_server_context_limit,
)

# 事务#2080实测原始错误消息（08:47:07，restart清空历史后prompt仅9799 tokens）
ERR_2080_CONTEXT_ZERO = (
    "HTTP 400 请求参数错误 - prompt (9799 tokens) + max tokens (32000) "
    "exceeds the context (0); requests are never truncated"
)
# 事务#2080首次失败错误消息（08:46:56，上下文37516 tokens）
ERR_2080_FIRST = (
    "HTTP 400 请求参数错误 - prompt (37516 tokens) + max tokens (32000) "
    "exceeds the context (0); requests are never truncated"
)


# ==================== Fix1: extract_server_context_limit ====================


def test_parse_strata_context_zero():
    """#2080实际错误：Strata格式context(0)应解析为0（服务端异常铁证）"""
    assert extract_server_context_limit(ERR_2080_CONTEXT_ZERO) == 0
    assert extract_server_context_limit(ERR_2080_FIRST) == 0


def test_parse_strata_normal_context():
    """Strata格式正常值：引擎就绪时context(262144)应正确解析"""
    err = (
        "prompt (37516 tokens) + max tokens (32000) "
        "exceeds the context (262144); requests are never truncated"
    )
    assert extract_server_context_limit(err) == 262144


def test_parse_vllm_openai_format():
    """vllm/OpenAI官方格式：maximum context length is N tokens"""
    err = (
        "This model's maximum context length is 8192 tokens. However, you "
        "requested 10000 tokens in the messages."
    )
    assert extract_server_context_limit(err) == 8192


def test_parse_task1993_format():
    """事务#1993实际报错（glm）：context length of 232000 tokens"""
    err = (
        "Requested token count exceeds the model's maximum context length "
        "of 232000 tokens. You requested a total of 240003 tokens."
    )
    assert extract_server_context_limit(err) == 232000


def test_parse_generic_kv_format():
    """通用格式：context_size: 4096 / context = 32768"""
    assert extract_server_context_limit("Error: context_size: 4096 exceeded") == 4096
    assert extract_server_context_limit("context = 32768 limit reached") == 32768


def test_parse_no_context_value():
    """无数值消息返回None（中文报错/普通错误）"""
    assert extract_server_context_limit("上下文长度超标，请减少输入") is None
    assert extract_server_context_limit("Invalid request payload") is None
    assert extract_server_context_limit("") is None
    assert extract_server_context_limit(None) is None


def test_sanity_floor_value():
    """合理下限1024：任何LLM上下文窗口≥2048，声称值低于1024必为异常"""
    assert SERVER_CONTEXT_LIMIT_SANITY_FLOOR == 1024


# ==================== Fix1: 误判修正端到端判定 ====================


def _final_overflow_judgement(err_msg: str) -> bool:
    """复现tool_engine.py HTTP 400分支的完整判定链（Fix1后）"""
    matched = any(kw.lower() in err_msg.lower() for kw in CONTEXT_LENGTH_KEYWORDS)
    if matched:
        srv_ctx = extract_server_context_limit(err_msg)
        if srv_ctx is not None and srv_ctx < SERVER_CONTEXT_LIMIT_SANITY_FLOOR:
            matched = False  # 服务端引擎未就绪/状态异常，非真实超标
    return matched


def test_2080_error_no_longer_overflow():
    """#2080的context(0)错误（含"max tokens"关键词）不应再判为上下文超标"""
    # 误匹配存在性（回归背景：确实含"max tokens"关键词）
    assert any(
        kw.lower() in ERR_2080_CONTEXT_ZERO.lower() for kw in CONTEXT_LENGTH_KEYWORDS
    )
    # Fix1后最终判定：非overflow（走普通4xx路径，切provider/冷却等待）
    assert not _final_overflow_judgement(ERR_2080_CONTEXT_ZERO)
    assert not _final_overflow_judgement(ERR_2080_FIRST)


def test_genuine_overflow_still_detected():
    """真实超标仍应判为overflow（Fix1不误伤正常自愈链路）"""
    # 事务#1993真实超标（240003 > 232000）
    err_1993 = (
        "Requested token count exceeds the model's maximum context length "
        "of 232000 tokens. You requested a total of 240003 tokens."
    )
    assert _final_overflow_judgement(err_1993)
    # Strata正常context值下的真实超标（prompt 300000 > 262144）
    err_strata_real = (
        "prompt (300000 tokens) + max tokens (32000) "
        "exceeds the context (262144); requests are never truncated"
    )
    assert _final_overflow_judgement(err_strata_real)
    # 中文超标消息（无数值，无法校验，保持原判定）
    assert _final_overflow_judgement("请求失败：上下文长度超标")


def test_tiny_context_below_floor_rejected():
    """声称context=512（<1024下限）也判为服务端异常"""
    err = "prompt (1000 tokens) + max tokens (32000) exceeds the context (512)"
    assert not _final_overflow_judgement(err)


# ==================== Fix2: compact矛盾判定 ====================


def _contradiction(after: int, threshold: int, max_tokens: int, orig: int | None = None) -> bool:
    """复现Fix2矛盾判定：compact无效果且上下文+输出预留远小于已知能力"""
    orig = after if orig is None else orig
    return orig == after and after + max_tokens < threshold


def test_fix2_2080_scenario():
    """#2080场景：compact 774→774，阈值225000 → 矛盾成立 → 服务端异常"""
    assert _contradiction(after=774, threshold=225000, max_tokens=32000, orig=774)
    # 首次失败场景 26739→26739 同样矛盾
    assert _contradiction(after=26739, threshold=225000, max_tokens=32000, orig=26739)


def test_fix2_genuine_overflow_no_contradiction():
    """真超标场景：compact后仍接近/超过阈值 → 不矛盾 → 走既有restart自愈"""
    # 事务#1993：阈值232000，compact后仍240000
    assert not _contradiction(after=240000, threshold=232000, max_tokens=30000, orig=260000)
    # compact有效压缩中（orig>after），不算矛盾（正在朝目标收敛）
    assert not _contradiction(after=100000, threshold=225000, max_tokens=32000, orig=200000)
    # compact后接近阈值（after+max_tokens >= threshold）
    assert not _contradiction(after=200000, threshold=225000, max_tokens=32000, orig=210000)


def test_fix2_max_tokens_uses_global_config():
    """矛盾判定用全局max_tokens（≥provider级32000），方向保守（更难触发）"""
    # 用全局80000：774+80000=80774 < 225000 仍矛盾（#2080场景依然覆盖）
    assert _contradiction(after=774, threshold=225000, max_tokens=80000, orig=774)
    # 边界：after=150000，provider级max_tokens=32000时150000+32000=182000<225000
    # 但用全局80000时150000+80000=230000>=225000 → 不矛盾（保守方向正确）
    assert not _contradiction(after=150000, threshold=225000, max_tokens=80000, orig=150000)


# ==================== Fix3: restart死循环熔断 ====================


class _FakeDB:
    """模拟TaskDatabase：记录insert调用，count按预置返回"""

    def __init__(self, recent_count: int = 0):
        self.recent_count = recent_count
        self.inserted: list[tuple] = []
        self.count_calls: list[tuple] = []

    def count_recent_task_controls(self, task_id, action, minutes=15):
        self.count_calls.append((task_id, action, minutes))
        return self.recent_count

    def insert_task_control(self, task_id, action):
        self.inserted.append((task_id, action))
        return len(self.inserted)


class _FakeTaskManager:
    def __init__(self, db):
        self.db = db


def _make_engine_for_breaker(recent_count: int) -> tuple[ToolExecutionEngine, _FakeDB]:
    """构造带mock db的引擎实例（不跑__init__）"""
    engine = ToolExecutionEngine.__new__(ToolExecutionEngine)
    db = _FakeDB(recent_count)
    engine.task_manager = _FakeTaskManager(db)
    return engine, db


def test_breaker_first_two_restarts_pass():
    """15分钟内第1/2次restart正常放行（正常自愈场景不受影响）"""
    for recent in (0, 1):
        engine, db = _make_engine_for_breaker(recent)
        result = engine._engine_restart_with_breaker(2080, "上下文超标compact无效(774→774)")
        assert result is True, f"recent={recent}时应放行restart"
        assert db.inserted == [(2080, "restart")]


def test_breaker_third_restart_converted_to_pause():
    """15分钟内第3次restart触发熔断：改插入pause（#2080死循环止血点）"""
    engine, db = _make_engine_for_breaker(recent_count=2)
    result = engine._engine_restart_with_breaker(2080, "上下文超标compact无效(774→774)")
    assert result is False, "第3次应熔断"
    assert db.inserted == [(2080, "pause")], "应插入pause而非restart"


def test_breaker_queries_with_15min_window():
    """熔断查询使用15分钟窗口和restart动作过滤"""
    engine, db = _make_engine_for_breaker(recent_count=0)
    engine._engine_restart_with_breaker(2080, "测试")
    assert db.count_calls == [(2080, "restart", 15)]


def test_breaker_db_count_failure_degrades_to_restart():
    """count查询异常时降级为正常restart（不因熔断组件故障阻断自愈）"""
    engine, db = _make_engine_for_breaker(recent_count=0)
    db.count_recent_task_controls = None  # 模拟方法缺失→AttributeError
    result = engine._engine_restart_with_breaker(2080, "测试")
    assert result is True
    assert db.inserted == [(2080, "restart")]


# ==================== database.count_recent_task_controls（SQL逻辑） ====================


def test_count_recent_task_controls_sql():
    """count_recent_task_controls的SQL与参数（psycopg2占位符风格）"""
    from unittest.mock import MagicMock, patch

    from peng.task.database import TaskDatabase

    db = TaskDatabase.__new__(TaskDatabase)  # 不跑__init__（避免连真库）
    fake_cursor = MagicMock()
    fake_cursor.fetchone.return_value = {"n": 2}
    fake_conn = MagicMock()
    fake_conn.__enter__.return_value = fake_conn  # with conn as ctx → 自身
    fake_conn.__exit__.return_value = False
    fake_conn.execute.return_value = fake_cursor

    with patch.object(TaskDatabase, "_get_connection", return_value=fake_conn):
        n = db.count_recent_task_controls(2080, "restart", minutes=15)

    assert n == 2
    sql, params = fake_conn.execute.call_args[0]
    # SQL断言：正确的表、过滤条件与聚合
    assert "COUNT(*)" in sql
    assert "task_controls" in sql
    assert "task_id = %s" in sql
    assert "action = %s" in sql
    assert "created_at >= %s" in sql
    assert params[0] == 2080 and params[1] == "restart"
    # 时间窗口参数为15分钟前的isoformat字符串
    assert isinstance(params[2], str) and "T" in params[2]


def test_count_recent_task_controls_exception_returns_zero():
    """查询异常返回0（降级：由调用方决定行为，不抛异常）"""
    from unittest.mock import patch

    from peng.task.database import TaskDatabase

    db = TaskDatabase.__new__(TaskDatabase)
    with patch.object(
        TaskDatabase,
        "_get_connection",
        side_effect=RuntimeError("db down"),
    ):
        assert db.count_recent_task_controls(2080, "restart") == 0


# ==================== 综合场景推演 ====================


def test_scenario_2080_full_chain():
    """#2080事故全链推演：context(0)错误经三层防御后的行为

    1. Fix1直接识别context(0)<1024 → 非overflow → 普通4xx路径
       （provider切换+冷却等待，事务保活，历史保留）
    2. 假设Fix1未命中（错误格式变化），Fix2矛盾判定兜底：
       compact 774→774 + 774+32000<225000 → 矛盾 → 等待路径
    3. 假设Fix2也未命中，Fix3熔断：第3次restart改pause止血
    """
    # 层1：context(0)直接识别
    assert not _final_overflow_judgement(ERR_2080_CONTEXT_ZERO)
    # 层2：矛盾判定（假设进入overflow分支）
    assert _contradiction(after=774, threshold=225000, max_tokens=32000, orig=774)
    # 层3：熔断（第3次）
    engine, db = _make_engine_for_breaker(recent_count=2)
    assert engine._engine_restart_with_breaker(2080, "上下文超标compact无效") is False
    assert db.inserted == [(2080, "pause")]


def test_scenario_genuine_overflow_unchanged():
    """事务#1993真实超标场景：三层防御均不触发，既有自愈链路不变

    真超标：240003>232000，Fix1识别context=232000（>1024合理值）→overflow；
    Fix2不矛盾（after+max_tokens≥threshold或compact有效）；Fix3前两次restart
    正常放行（restart清空历史后小上下文大概率成功）。
    """
    err_1993 = (
        "Requested token count exceeds the model's maximum context length "
        "of 232000 tokens. You requested a total of 240003 tokens."
    )
    # 层1不触发
    assert _final_overflow_judgement(err_1993)
    # 层2不触发（compact后仍超或正在压缩）
    assert not _contradiction(after=240000, threshold=232000, max_tokens=30000)
    # 层3前两次放行
    engine, db = _make_engine_for_breaker(recent_count=1)
    assert engine._engine_restart_with_breaker(1993, "上下文超标") is True
    assert db.inserted == [(1993, "restart")]

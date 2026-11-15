"""事务#2728回归测试：vision provider切换后不回切原provider的bug

根因：task.content中的@file=/[附件]文本引用**永久存在**，而附件发送成功后
已被strip_sent_attachments清除（只发送一次机制）。旧实现的vision回切窗口
（事务#1959）把"文本引用仍在"误当作"本轮仍有附件"，每轮刷新窗口预算，
导致识别图片后永久滞留vision provider。

修复：拆分双信号——
  prefer_vision        合并信号：驱动首切vision provider与开窗
  prefer_vision_parts  待发送附件part信号：唯一刷新窗口预算的判据；
                        强制回切轮get_provider的prefer_vision值
"""

import types

import pytest

import peng.llm.client as client_mod
from peng.llm.client import GLMClient, LLMError, VISION_SWITCH_MAX_ROUNDS


def _make_recording_cfg(calls: list):
    """构造记录get_provider调用参数的假LLMConfig（get_provider恒返回None
    触发LLMError干净中断，避免进入httpx请求构建）"""

    class _Cfg:
        compact_threshold = 320000

        def fetch_providers(self, content):
            return (None, False, None)

        def get_provider(self, **kwargs):
            calls.append(kwargs)
            return None

    return _Cfg()


def _bare_client() -> GLMClient:
    c = object.__new__(GLMClient)
    c.provider = "vision-flash"
    c.error = ""
    c.compact_threshold = 320000
    c.thinking_mode = "auto"
    c.thinking_type = None
    c.provider_config = None
    c._vision_switch_from = None
    c._vision_switch_used = 0
    c._recent_error_sigs = {}
    c._error_dedup_rounds = 5
    return c


def _mm(text_content: str, messages=None, monkeypatch=None) -> types.SimpleNamespace:
    task_manager = types.SimpleNamespace(
        get_task=lambda task_id=None: {"content": text_content},
        update_heartbeat=lambda tid: True,
    )
    return types.SimpleNamespace(get_task_manager=lambda: task_manager)


async def _one_round_simple(client, text_content, messages):
    """直接跑一轮chat_stream（load_config已被fixture patch）"""
    stream = client.chat_stream(
        task_id=1,
        messages=messages,
        message_manager=_mm(text_content),
        tools=None,
    )
    with pytest.raises(LLMError):
        await stream.__anext__()


class TestVisionWindowReturnWithStaleTextRef:
    """场景：附件已发送清除（无part），content文本引用残留 → 2轮内强制回切"""

    @pytest.fixture
    def patched_client(self, monkeypatch):
        calls: list = []
        cfg = _make_recording_cfg(calls)
        monkeypatch.setattr(
            client_mod, "load_config", lambda: types.SimpleNamespace(llm=cfg)
        )
        client = _bare_client()
        # 窗口已开：此前为识别图片从text-model切到vision-flash
        client._vision_switch_from = "text-model"
        client._vision_switch_used = 0
        return client, calls

    @pytest.mark.asyncio
    async def test_stale_text_ref_no_longer_refreshes_window(self, patched_client):
        """content残留@file=引用但无附件part：第2个非附件迭代强制回切，
        且回切轮get_provider收到prefer_vision=False（旧实现为True，
        导致原provider被判'不支持多模态→切回vision'双重锁死）"""
        client, calls = patched_client
        content = "请看这张图 @file=/tmp/图.png"
        plain_messages = [{"role": "user", "content": "继续处理"}]

        # 轮1：文本引用仍在（text=True），无附件part（parts=False）→计数1
        await _one_round_simple(client, content, plain_messages)
        assert client._vision_switch_used == 1
        assert calls[0]["current"] == "vision-flash"
        assert calls[0]["prefer_vision"] is True  # 非回切轮保持首切能力

        # 轮2：计数2 → 强制回切text-model，且prefer_vision=parts=False
        await _one_round_simple(client, content, plain_messages)
        assert client._vision_switch_used == 2
        assert calls[1]["current"] == "text-model"
        assert calls[1]["prefer_vision"] is False, (
            "强制回切轮必须不受旧文本引用阻碍（事务#2728核心断言）"
        )

    @pytest.mark.asyncio
    async def test_real_attachment_part_still_refreshes_window(self, patched_client):
        """消息携带待发送附件part（如read工具结果/上下文恢复附件）：窗口
        预算必须刷新（图片必须发给vision provider），多轮不强制回切"""
        client, calls = patched_client
        content = "请看这张图 @file=/tmp/图.png"
        mm_messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看图"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            }
        ]
        for i in range(VISION_SWITCH_MAX_ROUNDS + 3):
            await _one_round_simple(client, content, mm_messages)
            assert client._vision_switch_used == 0, f"第{i}轮附件part应刷新窗口预算"
            assert calls[i]["current"] == "vision-flash"  # 无强制回切目标
            assert calls[i]["prefer_vision"] is True

    @pytest.mark.asyncio
    async def test_parts_detected_even_when_text_ref_hits(self, patched_client):
        """parts检测独立于文本引用：text=True时parts也必须被检出（旧实现
        `if not prefer_vision`前缀导致漏检）"""
        client, calls = patched_client
        # content带引用 + 消息带附件part → parts信号生效，窗口刷新
        content = "请看这张图 @file=/tmp/图.png"
        mm_messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看图"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            }
        ]
        client._vision_switch_used = 1  # 已有非附件计数
        await _one_round_simple(client, content, mm_messages)
        assert client._vision_switch_used == 0, "parts=True必须刷新预算"

    @pytest.mark.asyncio
    async def test_first_switch_mechanism_preserved(self, patched_client):
        """首切机制保留：窗口未开 + 文本引用 → prefer_vision=True驱动切换
        （文本引用仍是首切与开窗的合法判据）"""
        client, calls = patched_client
        client._vision_switch_from = None  # 窗口关闭
        client.provider = "text-model"
        content = "请看这张图 @file=/tmp/图.png"
        await _one_round_simple(client, content, [{"role": "user", "content": content}])
        assert calls[0]["prefer_vision"] is True
        assert calls[0]["current"] == "text-model"

"""
Channel 基础架构测试

测试内容：
1. ChannelType 枚举
2. ChannelMessage 数据类
3. ChannelManager 基本操作
4. FeishuChannel 初始化
5. WechatChannel 初始化
6. 配置加载
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from peng.channels.base import ChannelBase, ChannelMessage, ChannelStatus, ChannelType
from peng.channels.manager import ChannelManager
from peng.channels.feishu import FeishuChannel
from peng.channels.wechat import WechatChannel
from peng.channels.integration import (
    channel_message_to_message_record,
    init_channel_system,
)


class TestChannelType:
    """ChannelType 枚举测试"""

    def test_channel_types(self):
        assert ChannelType.WECHAT.value == "wechat"
        assert ChannelType.FEISHU.value == "feishu"

    def test_all_channel_types(self):
        types = list(ChannelType)
        assert len(types) == 2


class TestChannelMessage:
    """ChannelMessage 数据类测试"""

    def test_basic_message(self):
        msg = ChannelMessage(
            content="Hello",
            sender_id="user123",
            channel_type=ChannelType.FEISHU,
        )
        assert msg.content == "Hello"
        assert msg.sender_id == "user123"
        assert msg.channel_type == ChannelType.FEISHU
        assert msg.msg_type == "text"
        assert msg.is_group is False
        assert msg.raw_data == {}

    def test_group_message(self):
        msg = ChannelMessage(
            content="Group Hello",
            sender_id="user123",
            channel_type=ChannelType.WECHAT,
            is_group=True,
            group_name="Test Group",
        )
        assert msg.is_group is True
        assert msg.group_name == "Test Group"

    def test_to_dict(self):
        msg = ChannelMessage(
            content="Test",
            sender_id="wxid_123",
            sender_name="TestUser",
            channel_type=ChannelType.WECHAT,
        )
        d = msg.to_dict()
        assert d["content"] == "Test"
        assert d["sender_id"] == "wxid_123"
        assert d["sender_name"] == "TestUser"
        assert d["channel_type"] == "wechat"


class TestChannelManager:
    """ChannelManager 测试"""

    def test_register_channel(self):
        manager = ChannelManager()
        channel = FeishuChannel({"app_id": "test", "app_secret": "test"})
        manager.register_channel(channel)
        assert ChannelType.FEISHU in manager._channels

    def test_unregister_channel(self):
        manager = ChannelManager()
        channel = FeishuChannel({"app_id": "test", "app_secret": "test"})
        manager.register_channel(channel)
        manager.unregister_channel(ChannelType.FEISHU)
        assert ChannelType.FEISHU not in manager._channels

    def test_get_channel(self):
        manager = ChannelManager()
        channel = FeishuChannel({"app_id": "test", "app_secret": "test"})
        manager.register_channel(channel)
        assert manager.get_channel(ChannelType.FEISHU) is channel
        assert manager.get_channel(ChannelType.WECHAT) is None

    def test_get_all_status(self):
        manager = ChannelManager()
        channel = FeishuChannel({"app_id": "test", "app_secret": "test"})
        manager.register_channel(channel)
        status = manager.get_all_status()
        assert "feishu" in status
        assert status["feishu"]["status"] == "disconnected"

    def test_message_callback(self):
        manager = ChannelManager()
        received_messages = []
        manager.set_message_callback(lambda msg: received_messages.append(msg))

        channel = FeishuChannel({"app_id": "test", "app_secret": "test"})
        manager.register_channel(channel)

        # 模拟消息到达
        test_msg = ChannelMessage(
            content="Test message",
            sender_id="user123",
            channel_type=ChannelType.FEISHU,
        )
        channel._notify_message(test_msg)

        assert len(received_messages) == 1
        assert received_messages[0].content == "Test message"

    def test_active_channels(self):
        manager = ChannelManager()
        assert manager.active_channels == []
        assert manager.is_running is False


class TestFeishuChannel:
    """FeishuChannel 测试"""

    def test_init(self):
        config = {
            "app_id": "cli_test123",
            "app_secret": "secret_test",
            "verification_token": "token_test",
        }
        channel = FeishuChannel(config)
        assert channel.channel_type == ChannelType.FEISHU
        assert channel.status == ChannelStatus.DISCONNECTED

    def test_verify_url_challenge(self):
        config = {
            "app_id": "test",
            "app_secret": "test",
            "verification_token": "correct_token",
        }
        channel = FeishuChannel(config)

        # 正确的验证请求
        result = channel.verify_url_challenge({
            "type": "url_verification",
            "token": "correct_token",
            "challenge": "challenge_abc",
        })
        assert result == {"challenge": "challenge_abc"}

        # 错误的token
        result = channel.verify_url_challenge({
            "type": "url_verification",
            "token": "wrong_token",
            "challenge": "challenge_abc",
        })
        assert result is None

    def test_handle_event_callback_text(self):
        config = {"app_id": "test", "app_secret": "test"}
        channel = FeishuChannel(config)
        received = []
        channel.set_message_callback(lambda msg: received.append(msg))

        event_data = {
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "message": {
                    "message_type": "text",
                    "content": '{"text": "Hello from Feishu"}',
                    "chat_id": "oc_123",
                    "chat_type": "p2p",
                    "message_id": "msg_456",
                },
                "sender": {
                    "sender_id": {"open_id": "ou_789"},
                },
            },
        }

        result = channel.handle_event_callback(event_data)
        assert result is not None
        assert result.content == "Hello from Feishu"
        assert result.sender_id == "ou_789"
        assert result.channel_type == ChannelType.FEISHU
        assert result.is_group is False
        assert len(received) == 1

    def test_handle_event_callback_non_message(self):
        config = {"app_id": "test", "app_secret": "test"}
        channel = FeishuChannel(config)

        result = channel.handle_event_callback({
            "header": {"event_type": "other.event"},
            "event": {},
        })
        assert result is None

    def test_get_status_info(self):
        config = {"app_id": "test_app_id_123", "app_secret": "test"}
        channel = FeishuChannel(config)
        info = channel.get_status_info()
        assert info["channel_type"] == "feishu"
        assert info["status"] == "disconnected"
        assert info["app_id"].endswith("...")  # 截断显示
        assert info["app_id"].startswith("test_app_")

    def test_extract_post_text(self):
        config = {"app_id": "test", "app_secret": "test"}
        channel = FeishuChannel(config)

        content = {
            "content": {
                "zh_cn": [
                    [
                        {"tag": "text", "text": "Hello "},
                        {"tag": "at", "user_name": "User"},
                        {"tag": "text", "text": " World"},
                    ]
                ]
            }
        }
        text = channel._extract_post_text(content)
        assert text == "Hello User World"


class TestWechatChannel:
    """WechatChannel 测试"""

    def test_init(self):
        config = {
            "base_url": "http://localhost:2531",
            "app_id": "wx_test",
        }
        channel = WechatChannel(config)
        assert channel.channel_type == ChannelType.WECHAT
        assert channel.status == ChannelStatus.DISCONNECTED

    def test_handle_callback_text(self):
        config = {"base_url": "http://localhost:2531"}
        channel = WechatChannel(config)
        received = []
        channel.set_message_callback(lambda msg: received.append(msg))

        callback_data = {
            "msgType": 1,
            "content": "Hello from WeChat",
            "fromUser": {"wxid": "wxid_abc", "nickname": "TestUser"},
            "toUser": {"wxid": "wxid_bot"},
            "newMsgId": "msg_123",
            "isGroup": False,
        }

        result = channel.handle_callback(callback_data)
        assert result is not None
        assert result.content == "Hello from WeChat"
        assert result.sender_id == "wxid_abc"
        assert result.sender_name == "TestUser"
        assert result.channel_type == ChannelType.WECHAT
        assert len(received) == 1

    def test_handle_callback_non_text(self):
        config = {"base_url": "http://localhost:2531"}
        channel = WechatChannel(config)

        # 图片消息(msgType=3)应被忽略
        callback_data = {
            "msgType": 3,
            "content": "image_data",
        }
        result = channel.handle_callback(callback_data)
        assert result is None

    def test_handle_callback_group_msg(self):
        config = {"base_url": "http://localhost:2531"}
        channel = WechatChannel(config)

        callback_data = {
            "msgType": 1,
            "content": "Group message",
            "fromUser": {"wxid": "group_123", "nickname": "TestGroup"},
            "toUser": {"wxid": "wxid_bot"},
            "newMsgId": "msg_456",
            "isGroup": True,
            "groupName": "Test Group",
            "fromGroupMsgWxid": "wxid_real_sender",
        }

        result = channel.handle_callback(callback_data)
        assert result is not None
        assert result.is_group is True
        assert result.group_name == "Test Group"
        assert result.sender_id == "wxid_real_sender"

    def test_extract_xml_text(self):
        xml = '<content><![CDATA[Hello XML]]></content>'
        text = WechatChannel._extract_xml_text(xml)
        assert text == "Hello XML"

    def test_get_status_info(self):
        config = {"base_url": "http://localhost:2531"}
        channel = WechatChannel(config)
        info = channel.get_status_info()
        assert info["channel_type"] == "wechat"
        assert info["status"] == "disconnected"
        assert info["base_url"] == "http://localhost:2531"


class TestIntegration:
    """集成函数测试"""

    def test_channel_message_to_message_record(self):
        msg = ChannelMessage(
            content="Test message",
            sender_id="user123",
            sender_name="TestUser",
            channel_type=ChannelType.FEISHU,
            is_group=False,
        )
        record = channel_message_to_message_record(msg)
        assert record["content"] == "Test message"
        assert "[feishu]" in record["sender"]
        assert record["status"] == "unread"

    def test_channel_message_to_message_record_group(self):
        msg = ChannelMessage(
            content="Group test",
            sender_id="user123",
            channel_type=ChannelType.WECHAT,
            is_group=True,
            group_name="MyGroup",
        )
        record = channel_message_to_message_record(msg)
        assert "[群:MyGroup]" in record["content"]

    def test_init_channel_system_disabled(self):
        config = {"enabled": False}
        manager = init_channel_system(config)
        assert manager is not None
        assert len(manager._channels) == 0

    def test_init_channel_system_feishu(self):
        config = {
            "enabled": True,
            "feishu": {
                "enabled": True,
                "app_id": "cli_test",
                "app_secret": "secret_test",
            },
            "wechat": {"enabled": False},
        }
        manager = init_channel_system(config)
        assert ChannelType.FEISHU in manager._channels

    def test_init_channel_system_wechat(self):
        config = {
            "enabled": True,
            "feishu": {"enabled": False},
            "wechat": {
                "enabled": True,
                "base_url": "http://localhost:2531",
            },
        }
        manager = init_channel_system(config)
        assert ChannelType.WECHAT in manager._channels

    def test_init_channel_system_both(self):
        config = {
            "enabled": True,
            "feishu": {
                "enabled": True,
                "app_id": "cli_test",
                "app_secret": "secret_test",
            },
            "wechat": {
                "enabled": True,
                "base_url": "http://localhost:2531",
            },
        }
        manager = init_channel_system(config)
        assert ChannelType.FEISHU in manager._channels
        assert ChannelType.WECHAT in manager._channels

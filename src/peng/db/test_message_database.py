#!/usr/bin/env python3
"""测试MessageDatabase的create_message功能"""

import sys

sys.path.insert(0, "src")

from peng.config import get_agent_name
from peng.message.database import MessageDatabase

print("测试MessageDatabase.create_message()...")

db = MessageDatabase()

# 测试发送消息
agent_name = get_agent_name()
msg_id, duplicate = db.create_message(content="测试消息", sender="测试者", receiver=agent_name)

print(f"返回结果: msg_id={msg_id}, duplicate={duplicate}")

if msg_id is None:
    print("❌ 错误: msg_id为None")
    sys.exit(1)
elif msg_id == 0:
    print("❌ 错误: msg_id为0")
    sys.exit(1)
else:
    print(f"✅ 成功: msg_id={msg_id}")

# 测试获取消息
msg = db.get_message(msg_id)
print(f"获取的消息: {msg['content'] if msg else 'None'}")

print("\n测试完成")

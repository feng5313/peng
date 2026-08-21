-- 迁移：为 messages 和 tasks 表添加 nest 字段
-- 版本：1.0.0
-- 描述：添加 nest 字段，让消息和事务携带 nest 上下文，支持工具通过 get_context('nest') 获取远程连接

-- messages 表添加 nest 字段
ALTER TABLE messages ADD COLUMN IF NOT EXISTS nest TEXT DEFAULT '';

-- tasks 表添加 nest 字段
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS nest TEXT DEFAULT '';

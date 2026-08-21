-- 迁移：为 tasks 表添加归档字段
-- 版本：1.0.0
-- 描述：添加 is_archived 和 archived_at 字段，支持事务归档功能

-- 添加归档标记字段
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS is_archived INTEGER DEFAULT 0;

-- 添加归档时间字段
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS archived_at TEXT;

-- 创建索引以提高查询性能
CREATE INDEX IF NOT EXISTS idx_tasks_is_archived ON tasks(is_archived);
CREATE INDEX IF NOT EXISTS idx_tasks_archived_at ON tasks(archived_at);

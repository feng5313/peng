-- 迁移：为 tasks 表添加 checklist 和 report 字段
-- 版本：1.0.0
-- 描述：添加验收清单和工作汇报字段，均为 Markdown 文本格式

-- 添加验收清单字段（Markdown 文本）
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS checklist TEXT DEFAULT '';

-- 添加工作汇报字段（Markdown 文本）
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS report TEXT DEFAULT '';

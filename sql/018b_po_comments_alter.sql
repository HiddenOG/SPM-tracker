-- 018b_po_comments_alter.sql
-- Run this if po_comments already existed before 018_po_comments.sql was applied.
-- Adds the two new columns safely (no-ops if they already exist).

alter table po_comments
  add column if not exists mentioned_roles text[]   not null default '{}',
  add column if not exists is_system_event boolean  not null default false;

create index if not exists idx_po_comments_mentioned
  on po_comments using gin(mentioned_roles);

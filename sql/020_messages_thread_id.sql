-- 020_messages_thread_id.sql
-- Adds thread_id to messages so replies are grouped into conversations.
-- Existing messages each get their own thread_id (they are standalone).
-- New messages auto-generate a thread_id; replies pass the parent's thread_id.

alter table messages
  add column if not exists thread_id uuid default gen_random_uuid();

-- Backfill: any existing row that has no thread_id (shouldn't happen after the
-- ALTER above sets the default, but keeps it safe for partial migrations).
update messages set thread_id = id where thread_id is null;

create index if not exists idx_messages_thread_id on messages(thread_id, created_at asc);

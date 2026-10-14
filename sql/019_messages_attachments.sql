-- 019_messages_attachments.sql
-- Adds user-uploaded attachment fields to messages.

alter table messages
  add column if not exists attachment_url  text,
  add column if not exists attachment_name text;

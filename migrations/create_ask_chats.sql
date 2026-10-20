-- SPM Ask chat history: every conversation, so anyone can go back to it.
-- Run once in the Supabase SQL Editor.
--
-- Shared, not private: everyone who can use Ask sees every chat, the way the
-- team shares an inbox. Who started each chat is kept, so a list can show it,
-- and only that person or an admin may rename or delete it (the server checks).

create table if not exists ask_chats (
  id               uuid primary key default gen_random_uuid(),
  title            text not null default 'New chat',
  created_by_id    text,
  created_by_name  text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

-- One row per question and per answer, in order. The answer keeps the emails
-- it cited and the steps it took, so an old chat opens looking as it did.
create table if not exists ask_messages (
  id          uuid primary key default gen_random_uuid(),
  chat_id     uuid not null references ask_chats(id) on delete cascade,
  role        text not null check (role in ('user', 'assistant')),
  content     text not null default '',
  sources     jsonb,
  steps       jsonb,
  secs        numeric(8,1),
  asked_by    text,
  created_at  timestamptz not null default now()
);

create index if not exists ask_chats_updated_idx on ask_chats (updated_at desc);
create index if not exists ask_messages_chat_idx on ask_messages (chat_id, created_at);

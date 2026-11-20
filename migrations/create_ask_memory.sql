-- SPM Ask's long-term memory: what it should keep knowing from one chat to
-- the next. Run in the Supabase SQL Editor.
--
-- Ask saves a note when someone tells it something that stays true ("I always
-- want list x 1.35 x 1.13", "Seplat always wants CGI"); it reads a person's
-- own notes and the team's notes at the start of every question. Anyone can
-- see their notes and the team's on the Ask page and delete them there.
create table if not exists ask_memory (
  id          uuid primary key default gen_random_uuid(),
  about       text not null default 'person' check (about in ('person', 'team')),
  user_id     text,                      -- whose note ('person'), or who saved it ('team')
  user_name   text,
  note        text not null,
  chat_id     uuid references ask_chats(id) on delete set null,
  created_at  timestamptz not null default now()
);

create index if not exists ask_memory_user_idx on ask_memory (user_id, created_at desc);
create index if not exists ask_memory_team_idx on ask_memory (about, created_at desc);

alter table ask_memory enable row level security;

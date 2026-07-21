-- 004_users.sql — role-based user accounts
create table if not exists users (
    id            uuid        primary key default gen_random_uuid(),
    username      text        unique not null,
    email         text        unique not null,
    password_hash text        not null,
    role          text        not null check (role in ('admin','procurement','warehouse','expeditor','accounts')),
    full_name     text,
    is_active     boolean     not null default true,
    created_at    timestamptz not null default now(),
    last_login_at timestamptz
);

create index if not exists idx_users_email on users(email);

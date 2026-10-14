-- 011_messages.sql — internal messaging between roles

create table if not exists messages (
    id            uuid        primary key default gen_random_uuid(),
    from_user_id  uuid        references users(id) on delete set null,
    from_name     text        not null,
    to_role       text        not null check (to_role in ('admin','procurement','warehouse','expeditor','accounts')),
    order_id      uuid,
    order_client  text        check (order_client in ('chevron','nlng')),
    subject       text        not null,
    body          text        not null,
    message_type  text        not null default 'general'
                              check (message_type in ('availability_check','stock_response','dispatch_update','general')),
    created_at    timestamptz not null default now()
);

create table if not exists message_reads (
    message_id  uuid not null references messages(id) on delete cascade,
    user_id     uuid not null references users(id)    on delete cascade,
    read_at     timestamptz not null default now(),
    primary key (message_id, user_id)
);

create index if not exists idx_messages_to_role  on messages(to_role, created_at desc);
create index if not exists idx_messages_order_id on messages(order_id);

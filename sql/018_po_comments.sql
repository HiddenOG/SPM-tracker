-- 018_po_comments.sql — per-PO activity comments (team notes + @mentions)
--
-- Replaces the po_comments table that was created directly in Supabase.
-- Adds: mentioned_roles (for @mention notifications) and is_system_event
-- (for pipeline-inserted status change entries).

create table if not exists po_comments (
    id               uuid        primary key default gen_random_uuid(),
    -- exactly one of order_id / nlng_order_id must be set
    order_id         uuid        references orders(id)      on delete cascade,
    nlng_order_id    uuid        references nlng_orders(id) on delete cascade,
    user_id          uuid        references users(id)       on delete set null,
    author_name      text,                          -- snapshot at post time
    author_role      text,                          -- snapshot at post time
    body             text        not null,
    -- roles mentioned via @role in the body, e.g. '{"warehouse","procurement"}'
    mentioned_roles  text[]      not null default '{}',
    -- true for pipeline-inserted events (status changes, email arrivals, etc.)
    is_system_event  boolean     not null default false,
    created_at       timestamptz not null default now(),

    constraint po_comments_one_order check (
        (order_id is not null)::int + (nlng_order_id is not null)::int = 1
    )
);

create index if not exists idx_po_comments_order_id      on po_comments(order_id,      created_at asc);
create index if not exists idx_po_comments_nlng_order_id on po_comments(nlng_order_id, created_at asc);
create index if not exists idx_po_comments_mentioned     on po_comments using gin(mentioned_roles);

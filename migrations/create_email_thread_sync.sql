-- Bookkeeping for the email thread archiver.
--
-- The emails table holds the mail itself. This table only records WHEN each
-- order's Gmail thread was last walked, which is what lets the worker spread
-- the work out: every pass it takes the orders whose threads were synced
-- longest ago, or never, so all 199 are covered in turn instead of all being
-- fetched at once.
--
-- It is deliberately separate from orders and nlng_orders. Those are written by
-- the pipeline and read by the whole app; adding a column to them for the
-- archiver's own housekeeping would put a UI concern in the middle of the
-- order record.

create table if not exists email_thread_sync (
    order_id    text not null,
    order_type  text not null default 'chevron',   -- 'chevron' | 'nlng'
    synced_at   timestamptz,
    email_count integer default 0,
    primary key (order_id, order_type)
);

-- The worker's only query: least-recently-synced first, nulls (never synced)
-- ahead of everything.
create index if not exists email_thread_sync_due_idx
    on email_thread_sync (synced_at asc nulls first);

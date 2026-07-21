-- ============================================================
-- 003_sync_and_parking.sql
-- Turns the listeners into a genuine live system:
--   1. sync_state  — per-folder UID high-water mark (cursor), so each
--      listener processes EVERY new email exactly once instead of
--      re-scanning a fixed [-50:]/[-100:] tail and missing anything
--      older than the window.
--   2. parked_emails — emails that reference a PO whose order row does
--      not exist YET (cross-mailbox ordering race). Instead of being
--      discarded with "no matching order yet", they are parked here and
--      auto-applied the moment the matching order is created.
--
-- Run this in the Supabase SQL editor AFTER 001/002.
-- ============================================================

-- ------------------------------------------------------------
-- SYNC STATE — one row per (account, folder).
-- last_uid is the highest IMAP UID we have fully processed.
-- IMPORTANT: IMAP UIDs are only stable within a folder's UIDVALIDITY.
-- We store uidvalidity too; if the server resets it, we know to
-- re-backfill rather than trust a stale cursor.
-- ------------------------------------------------------------
create table if not exists sync_state (
    id              uuid primary key default gen_random_uuid(),
    account         text not null,        -- e.g. 'yahoo', 'gmail'
    folder          text not null,        -- e.g. 'INBOX', '[Gmail]/All Mail'
    last_uid        bigint not null default 0,
    uidvalidity     bigint,               -- IMAP UIDVALIDITY of the folder
    updated_at      timestamptz default now(),
    unique(account, folder)
);

-- ------------------------------------------------------------
-- PARKED EMAILS — relevant emails whose PO order doesn't exist yet.
-- These are reconciled (applied + deleted) when the order appears.
-- kind tells the reconciler what action to take once matched.
-- ------------------------------------------------------------
create table if not exists parked_emails (
    id              uuid primary key default gen_random_uuid(),
    message_id      text not null,        -- email Message-ID (dedup)
    kind            text not null,        -- 'warehouse_routing' | 'warehouse_reply'
    po_number       text not null,        -- the PO this email refers to
    sender          text,
    subject         text,
    email_date      timestamptz,          -- real send date from the email header
    pdf_path        text,                 -- saved PDF path, if any (routing emails)
    body_text       text,                 -- saved body, for replies needing Claude later
    needs_claude    boolean default false,-- true if a Claude step is still pending
    created_at      timestamptz default now(),
    unique(message_id, kind)
);

create index if not exists idx_parked_po on parked_emails(po_number);
create index if not exists idx_parked_kind on parked_emails(kind);

-- ------------------------------------------------------------
-- Optional flags on orders to record when a Claude-dependent step
-- was deferred (credits empty / extraction failed) so a later pass
-- can backfill it. Safe to run repeatedly.
-- ------------------------------------------------------------
alter table orders add column if not exists pending_ack_extraction boolean default false;
alter table orders add column if not exists pending_stock_extraction boolean default false;

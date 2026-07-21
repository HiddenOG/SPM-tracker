-- ============================================================
-- 004_orders_unique_po.sql
-- Make buyer_po_number unique so duplicate orders are impossible at the
-- database level — the last "accurate on rerun" gap.
--
-- Why: imap_listener currently blind-inserts an order per PO notification.
-- Idempotency relies only on the UID cursor + processed_emails log; if those
-- are reset (or two listeners race), the same notification creates a DUPLICATE
-- order, and find_order_by_po_number() then picks one arbitrarily. A unique
-- constraint enforces one row per PO regardless of code paths, and lets the
-- listener upsert (insert-or-update) cleanly.
--
-- Revisions are distinct: '0060792432' and '0060792432-001' are different
-- strings, so each Chevron PO / change order still gets its own row.
--
-- Run this in the Supabase SQL Editor. There are currently NO duplicates,
-- so it applies cleanly. (If a duplicate ever existed, this would error and
-- you'd dedupe first.)
-- ============================================================

alter table orders
    add constraint orders_buyer_po_number_key unique (buyer_po_number);

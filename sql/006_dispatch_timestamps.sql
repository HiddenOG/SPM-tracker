-- ============================================================
-- Migration 006: dispatch-stage timestamps.
--
-- Three new milestone columns, on both spm_purchase_orders
-- (one per SPM PO / SO) and orders (one per linked Chevron PO):
--
--   so_received_at              — when the Flexitallic SO email landed
--                                 (mirrors spm_purchase_orders.so_acknowledged_at,
--                                  denormalised here for easy per-order querying)
--   flex_dispatch_ready_at      — when Flexitallic says the order is packed &
--                                 ready for dispatch (Penny Latham's email)
--   dispatch_instructions_sent_at — when SPM replies to Flexitallic with
--                                   shipping instructions ("ship to Unicorn")
-- ============================================================

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS so_received_at                timestamptz,
    ADD COLUMN IF NOT EXISTS flex_dispatch_ready_at        timestamptz,
    ADD COLUMN IF NOT EXISTS dispatch_instructions_sent_at timestamptz;

ALTER TABLE spm_purchase_orders
    ADD COLUMN IF NOT EXISTS flex_dispatch_ready_at        timestamptz,
    ADD COLUMN IF NOT EXISTS dispatch_instructions_sent_at timestamptz;

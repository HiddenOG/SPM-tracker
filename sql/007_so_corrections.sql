-- ============================================================
-- Migration 007: track Chevron orders that have been re-raised
-- with a second (or third) SPM PO to Flexitallic.
--
-- This happens in three rare situations:
--   • line_item_error  — wrong part/spec shipped first time
--   • quantity_error   — Flexitallic short-shipped, re-order for balance
--   • excess_order     — SPM authorised to purchase additional stock
--
-- so_correction_count = (total SPM POs for this order) - 1
--   0  → normal single-SO order
--   1  → one correction SO raised (two SPM POs in total)
--   2  → two corrections (three SPM POs), etc.
--
-- so_correction_reason is free text; set manually via Supabase or a
-- future UI — the parser cannot determine the reason automatically.
-- ============================================================

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS so_correction_count  SMALLINT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS so_correction_reason TEXT;      -- 'line_item_error' | 'quantity_error' | 'excess_order'

CREATE INDEX IF NOT EXISTS idx_orders_so_correction
    ON orders(so_correction_count)
    WHERE so_correction_count > 0;

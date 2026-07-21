-- ============================================================
-- Migration 005: capture when SPM forwards a supplier SO to
-- the warehouse (the step after receiving the Flexitallic SO).
-- ============================================================

-- On the SPM PO level (one SO can cover multiple Chevron POs)
ALTER TABLE spm_purchase_orders
    ADD COLUMN IF NOT EXISTS so_sent_to_warehouse_at timestamptz;

-- Denormalised onto individual orders for easy querying / dashboard
ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS so_sent_to_warehouse_at timestamptz;

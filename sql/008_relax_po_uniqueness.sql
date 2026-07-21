-- ============================================================
-- Migration 008: allow a Chevron PO number to appear in more
-- than one orders row when it has been re-raised on a second
-- (or third) SPM PO / Flexitallic SO.
--
-- Reason: Migration 004 added UNIQUE(buyer_po_number) to
-- prevent duplicate notification rows. That protection is still
-- enforced at the application level via the processed_emails log
-- (the imap_listener skips any notification it has already seen).
-- The DB constraint can therefore be dropped without risk, and
-- doing so lets us store a separate orders row per SPM PO when
-- a correction re-raise occurs.
--
-- Scenarios that create a second row:
--   • Flexitallic shipped the wrong line item — a new SPM PO is
--     raised for the correct part.
--   • Quantity short-shipped — new SPM PO for the balance.
--   • SPM authorised to purchase excess stock — new SPM PO.
--
-- In all these cases so_correction_count > 0 (see migration 007)
-- which the dashboard uses to surface the correction badge.
-- ============================================================

ALTER TABLE orders
    DROP CONSTRAINT IF EXISTS orders_buyer_po_number_key;


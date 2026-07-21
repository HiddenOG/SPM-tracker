-- ============================================================
-- Migration 009: full dispatch-to-delivery column set.
--
-- Adds four columns to orders:
--
--   so_number                   — Flexitallic SO denormalised from
--                                 spm_purchase_orders for direct per-order
--                                 querying without a join.  Written by
--                                 process_so_ack() when the SO ack arrives.
--
--   freight_forwarder_received_at — when the freight forwarder (Unicorn /
--                                 other) replies "thanks and noted" confirming
--                                 they have the goods for onward shipment.
--                                 overall_status → so_received
--
--   delivery_requested_at       — when the Nigerian warehouse sends the
--                                 "REQUEST FOR DELIVERY" email to the buyer
--                                 (Chevron/NIGEC) asking them to book receipt.
--                                 overall_status → delivery_requested
--
--   delivered_at                — when the warehouse emails "completely
--                                 delivered" confirming the buyer has taken
--                                 possession of the goods.
--                                 overall_status → delivered
--
-- Also adds freight_forwarder_received_at to spm_purchase_orders so
-- the dispatch picture can be queried at the SPM PO level too.
-- ============================================================

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS so_number                    TEXT,
    ADD COLUMN IF NOT EXISTS freight_forwarder_received_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS delivery_requested_at         TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS delivered_at                  TIMESTAMPTZ;

ALTER TABLE spm_purchase_orders
    ADD COLUMN IF NOT EXISTS freight_forwarder_received_at TIMESTAMPTZ;

-- Index so the dashboard can quickly filter orders with a recorded SO
CREATE INDEX IF NOT EXISTS idx_orders_so_number
    ON orders(so_number)
    WHERE so_number IS NOT NULL;

-- Index for the delivery pipeline (dashboard "delivery requested" chip)
CREATE INDEX IF NOT EXISTS idx_orders_delivery_requested
    ON orders(delivery_requested_at)
    WHERE delivery_requested_at IS NOT NULL;

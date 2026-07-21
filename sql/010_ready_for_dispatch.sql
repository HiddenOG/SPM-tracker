-- 010_ready_for_dispatch.sql
-- Replace freight_forwarder_received_at with two explicit dispatch columns:
--   ready_for_dispatch_at = Penny "arranged Pudsey Transport to collect" email
--   dispatched_at         = shipping company (Unicorn) "Noted" reply

-- Drop the view first — it references freight_forwarder_received_at
DROP VIEW IF EXISTS orders_pipeline;

-- ── orders ────────────────────────────────────────────────────────────
ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS ready_for_dispatch_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS dispatched_at          TIMESTAMPTZ;

-- Migrate any existing freight_forwarder_received_at → dispatched_at
UPDATE orders
SET dispatched_at = freight_forwarder_received_at
WHERE freight_forwarder_received_at IS NOT NULL
  AND dispatched_at IS NULL;

-- Advance so_received status → dispatched
UPDATE orders
SET overall_status = 'dispatched'
WHERE overall_status = 'so_received';

ALTER TABLE orders DROP COLUMN IF EXISTS freight_forwarder_received_at;

-- ── spm_purchase_orders ───────────────────────────────────────────────
ALTER TABLE spm_purchase_orders
    ADD COLUMN IF NOT EXISTS ready_for_dispatch_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS dispatched_at          TIMESTAMPTZ;

UPDATE spm_purchase_orders
SET dispatched_at = freight_forwarder_received_at
WHERE freight_forwarder_received_at IS NOT NULL
  AND dispatched_at IS NULL;

ALTER TABLE spm_purchase_orders DROP COLUMN IF EXISTS freight_forwarder_received_at;

-- ── Recreate orders_pipeline with updated columns ─────────────────────
CREATE VIEW orders_pipeline AS
SELECT
    id,
    buyer_po_number,
    po_amount,
    product_line,
    notification_received_at,
    jde_job_id,
    branch_plant,
    supplier_ref_number,
    pdf_attachment_path,
    order_submitted_on,
    extracted_description,
    required_delivery_date,
    payment_terms,
    po_destination,
    transportation,
    ship_to,
    requestor_name,
    requestor_email,
    extraction_confidence,
    acknowledgment_status,
    acknowledged_at,
    acknowledged_by,
    sent_to_warehouse_at,
    stock_check_completed_at,
    stock_check_raw,
    price_source,
    quoted_price,
    quotation_requested_at,
    quotation_received_at,
    spm_po_number,
    supplier_id,
    supplier_item_number,
    spm_po_drafted_at,
    spm_po_sent_at,
    promised_date,
    overall_status,
    so_number,
    so_received_at,
    so_sent_to_warehouse_at,
    so_correction_count,
    so_correction_reason,
    flex_dispatch_ready_at,
    dispatch_instructions_sent_at,
    ready_for_dispatch_at,
    dispatched_at,
    delivery_requested_at,
    delivered_at,
    buyer_id,
    created_at,
    updated_at
FROM orders;

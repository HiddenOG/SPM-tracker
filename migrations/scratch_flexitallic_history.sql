-- SCRATCH table: historical SPM -> Flexitallic purchase orders and the
-- Flexitallic sales orders that acknowledge them, 2024 to present.
--
-- Built for a one-off question: Flexitallic want to know whether the SPM
-- relationship is still worth their investment, so the boss needs PO values
-- and the gaskets ordered, going back to 2024.
--
-- Deliberately SEPARATE from spm_purchase_orders. That table is written by the
-- live pipeline and only covers 2026; loading three years of history into it
-- would mix reconstructed history with live tracking and could confuse the
-- listeners. Drop this table when the question has been answered.
--
-- One row per Flexitallic sales order. A PO can have several sales orders
-- (Flexitallic splits by despatch, e.g. AIR vs STOCK), so PO totals come from
-- the view below rather than being stored and going stale.
CREATE TABLE IF NOT EXISTS scratch_flexitallic_history (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- the Flexitallic sales order
    so_number           text NOT NULL,
    so_date             timestamptz NOT NULL,
    order_value         numeric(14,2),
    currency            text NOT NULL DEFAULT 'USD',

    -- the SPM purchase order it acknowledges
    spm_po_ref          text,            -- SPM's own sequence number, e.g. '1081'
    spm_po_reference    text,            -- full text, e.g. 'S.P.M.-C.N.L.-1081-0060870148-FLEXITALLIC'

    -- who the goods were ultimately for
    customer            text,            -- CHEVRON / NLNG / MOBIL / SEPLAT / AVEON / CHINA
    customer_po_numbers jsonb,           -- every 006…/4200…/4501… number in the reference

    -- the gaskets
    line_item_count     integer NOT NULL DEFAULT 0,
    line_items          jsonb,           -- item_number, qty, uom, unit_price, extended_price

    -- provenance, so any figure can be traced back to the email it came from
    source_message_id   text UNIQUE,
    source_subject      text,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_scratch_flex_po       ON scratch_flexitallic_history (spm_po_ref);
CREATE INDEX IF NOT EXISTS idx_scratch_flex_customer ON scratch_flexitallic_history (customer);
CREATE INDEX IF NOT EXISTS idx_scratch_flex_date     ON scratch_flexitallic_history (so_date);

-- PO-level totals: one row per SPM PO with its sales orders rolled up.
CREATE OR REPLACE VIEW scratch_flexitallic_po_totals AS
SELECT
    COALESCE(spm_po_ref, spm_po_reference)      AS po_key,
    MIN(spm_po_reference)                       AS po_reference,
    MIN(customer)                               AS customer,
    COUNT(*)                                    AS sales_order_count,
    STRING_AGG(so_number, ', ' ORDER BY so_date) AS sales_orders,
    MIN(so_date)                                AS first_so_date,
    MAX(so_date)                                AS last_so_date,
    currency,
    SUM(order_value)                            AS po_total_value,
    SUM(line_item_count)                        AS total_line_items
FROM scratch_flexitallic_history
GROUP BY COALESCE(spm_po_ref, spm_po_reference), currency;

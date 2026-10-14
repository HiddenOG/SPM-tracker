-- PO-side values and document links for the Flexitallic history.
--
-- The table already held the SALES ORDER value - what Flexitallic acknowledged
-- charging SPM. That is not the same number as the PURCHASE ORDER value, which
-- is what SPM actually ordered: of 164 orders where both are known, exactly one
-- matched. Gaps run both ways (PO 3095 ordered $31,632 but only $2,785 was
-- acknowledged; PO 2082 ordered $1,386 against $17,109 acknowledged), so the
-- two figures have to be shown side by side rather than one standing in for
-- the other.
--
-- po_value is a property of the PURCHASE ORDER, so it repeats across every
-- sales order belonging to that PO. Anything summing it must deduplicate by
-- spm_po_ref first or it will multiply-count POs that Flexitallic split.
ALTER TABLE scratch_flexitallic_history
  ADD COLUMN IF NOT EXISTS po_value      numeric(14,2),
  ADD COLUMN IF NOT EXISTS po_currency   text,
  ADD COLUMN IF NOT EXISTS po_date       timestamptz,
  ADD COLUMN IF NOT EXISTS po_line_items jsonb,
  ADD COLUMN IF NOT EXISTS po_pdf_url    text,
  ADD COLUMN IF NOT EXISTS so_pdf_url    text;

COMMENT ON COLUMN scratch_flexitallic_history.po_value IS
  'Grand total on SPM''s own purchase order PDF. Repeats across every sales '
  'order for that PO - deduplicate by spm_po_ref before summing.';
COMMENT ON COLUMN scratch_flexitallic_history.order_value IS
  'Sales order value: what Flexitallic acknowledged charging SPM.';

-- PO totals view, rebuilt to carry both figures.
--
-- Dropped and recreated rather than CREATE OR REPLACE: replacing a view cannot
-- rename or reorder its existing columns, and this adds so_total_value in the
-- middle of the old column list ("cannot change name of view column
-- total_line_items to so_total_value").
--
-- Note the meaning of po_total_value CHANGES here. In the first version of
-- this view it was SUM(order_value) - the sales order total wearing a PO
-- name, written before it was known that the two figures differ. It now holds
-- the real purchase order total, and the sales order total moves to
-- so_total_value. Anything reading the old column is reading a different
-- number after this migration.
DROP VIEW IF EXISTS scratch_flexitallic_po_totals;

CREATE VIEW scratch_flexitallic_po_totals AS
SELECT
    COALESCE(spm_po_ref, spm_po_reference)       AS po_key,
    MIN(spm_po_reference)                        AS po_reference,
    MIN(customer)                                AS customer,
    COUNT(*)                                     AS sales_order_count,
    STRING_AGG(so_number, ', ' ORDER BY so_date) AS sales_orders,
    MIN(so_date)                                 AS first_so_date,
    MAX(so_date)                                 AS last_so_date,
    currency,
    MAX(po_value)                                AS po_total_value,
    SUM(order_value)                             AS so_total_value,
    SUM(order_value) - MAX(po_value)             AS variance,
    SUM(line_item_count)                         AS total_line_items,
    MAX(po_pdf_url)                              AS po_pdf_url
FROM scratch_flexitallic_history
GROUP BY COALESCE(spm_po_ref, spm_po_reference), currency;

-- Client purchase order values: what Chevron / NLNG / Mobil paid SPM.
--
-- The Flexitallic history holds two figures, and both are SPM's COST:
--   po_value    - SPM's purchase order to Flexitallic
--   order_value - Flexitallic's acknowledgement of it
-- Neither says what the client paid SPM, which is the number the comparison
-- actually needs.
--
-- Stored per CLIENT PO rather than per row, because the two do not line up:
-- one SPM purchase order regularly bundles several client POs (60 of 213 rows
-- do), and the same client PO can appear on more than one row. Keying by the
-- client PO number keeps each value recorded exactly once.
CREATE TABLE IF NOT EXISTS scratch_client_po_values (
    po_number   text PRIMARY KEY,          -- 006…/4200…/4501…
    client      text,                      -- CHEVRON / NLNG / MOBIL
    po_value    numeric(14,2),
    currency    text NOT NULL DEFAULT 'USD',
    po_date     timestamptz,
    description text,
    req_number  text,
    source      text,                      -- 'orders' | 'routing_pdf' | 'ocr'
    pdf_url     text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_scratch_client_po_client ON scratch_client_po_values (client);

COMMENT ON TABLE scratch_client_po_values IS
  'What the end client paid SPM, keyed by their own PO number. Read together '
  'with scratch_flexitallic_history.customer_po_numbers to compare against '
  'what SPM paid Flexitallic.';
COMMENT ON COLUMN scratch_client_po_values.source IS
  '"orders" = taken from the live orders table; "routing_pdf" = read from the '
  'PO PDF on the email forwarding it to the warehouse; "ocr" = that PDF was a '
  'scan and Tesseract read it.';

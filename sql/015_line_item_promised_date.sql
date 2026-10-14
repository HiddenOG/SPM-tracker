-- 015_line_item_promised_date.sql
-- Add promised_date per line item.
--
-- For Chevron POs, the GEP PDF has BOTH "Required Delivery Date" (what Chevron
-- needs) AND "Promised Date" (what SPM committed to in their quotation).  The
-- two columns often match, but not always.  Chevron's OTD scoring uses the
-- Promised Date, not the Required Delivery Date.
--
-- For NLNG, the column is added for future use; NLNG PDFs currently expose
-- only delivery_date (= required date).  If a separate promised date is ever
-- parsed, it will land here.

ALTER TABLE order_line_items
    ADD COLUMN IF NOT EXISTS promised_date date;

ALTER TABLE nlng_order_line_items
    ADD COLUMN IF NOT EXISTS promised_date date;

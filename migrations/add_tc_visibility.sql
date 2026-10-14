-- Per-field show/hide flags for the Terms & Conditions block on a quotation,
-- mirroring the existing markup_visible eye toggle.
--
-- Default TRUE so every existing quotation keeps rendering exactly as it does
-- today — turning a field off is an explicit choice, never a silent change.
ALTER TABLE quotations
  ADD COLUMN IF NOT EXISTS show_lead_time      boolean NOT NULL DEFAULT true,
  ADD COLUMN IF NOT EXISTS show_delivery_dest  boolean NOT NULL DEFAULT true,
  ADD COLUMN IF NOT EXISTS show_country_import boolean NOT NULL DEFAULT true,
  ADD COLUMN IF NOT EXISTS show_shipping_mode  boolean NOT NULL DEFAULT true,
  ADD COLUMN IF NOT EXISTS show_manufacturer   boolean NOT NULL DEFAULT true,
  ADD COLUMN IF NOT EXISTS show_weight         boolean NOT NULL DEFAULT true,
  ADD COLUMN IF NOT EXISTS show_currency       boolean NOT NULL DEFAULT true;

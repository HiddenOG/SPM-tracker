-- A quote's own line-item columns. Run in the Supabase SQL Editor.
--
-- Most quotes never use this: line_columns stays null and the standard
-- columns are used, on the form and the PDF. A quote that needs something
-- unusual saves its own list - order and names - as
--   [{"key": "description", "label": ""}, {"key": "x1", "label": "Lead time"}, ...]
-- (a blank label means the standard name). The values typed into added
-- columns are kept per line in extra: {"x1": "4 weeks"}.

alter table quotations           add column if not exists line_columns jsonb;
alter table quotation_line_items add column if not exists extra        jsonb;

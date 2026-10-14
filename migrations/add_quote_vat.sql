-- VAT on quotations, with its own show/hide eye — same pattern as the markup.
-- Run in the Supabase SQL Editor.
--
-- The eye only hides the LINE on the PDF. A hidden VAT is still in the total,
-- exactly as a hidden markup is: the customer pays it either way, they just do
-- not see it broken out.

alter table quotations add column if not exists vat_pct     numeric(5,2) default 0;
alter table quotations add column if not exists vat_visible boolean      default true;

-- Sheets, tape, braided packing and paste from Flexitallic's 2022 list (pages
-- 6 to 10 and 15) are priced per sheet, roll, coil or tube, not by pipe size
-- and class. These columns hold what each of those prices is for.
-- Run in the Supabase SQL Editor, then reload with scripts/load_price_book.py.

alter table price_book add column if not exists thickness_mm numeric;  -- sheet or tape thickness
alter table price_book add column if not exists width_mm     numeric;  -- tape width; packing section size
alter table price_book add column if not exists length_m     numeric;  -- tape roll or packing coil length
alter table price_book add column if not exists sheet_size   text;     -- '1500x1500' (mm, smaller side first)

-- product now also takes: sheet | tape | packing | paste

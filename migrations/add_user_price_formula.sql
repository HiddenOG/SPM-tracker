-- Each person's own changes to SPM's selling-price formula, saved from the
-- formula chip under Ask's text box. Only the parts they changed are kept,
-- per list: {"flex_2022": {"spm": 1.5}, "spm_2026": {"extra_kg": 0}}.
-- Null means the default formula (scripts/price_book.py).
-- Run in the Supabase SQL Editor.

alter table users add column if not exists price_formula jsonb;

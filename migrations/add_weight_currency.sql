-- Add weight and currency to quotations table
-- Run in Supabase SQL Editor

alter table quotations add column if not exists weight   text;
alter table quotations add column if not exists currency text default 'USD';

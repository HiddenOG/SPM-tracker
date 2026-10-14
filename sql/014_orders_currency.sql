-- 014_orders_currency.sql — add currency column to Chevron orders table
alter table orders add column if not exists po_currency text default 'USD';

-- Migration 003: Add enquiry_number and so_pdf_url to nlng_orders
-- Run in Supabase SQL editor.

ALTER TABLE nlng_orders ADD COLUMN IF NOT EXISTS enquiry_number text;
ALTER TABLE nlng_orders ADD COLUMN IF NOT EXISTS so_pdf_url     text;

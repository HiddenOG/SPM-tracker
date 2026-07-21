-- ============================================================
-- 003_line_item_rdd.sql
-- Per-item Required Delivery Date tracking.
--
-- A single Chevron PO can have line items with DIFFERENT required
-- delivery dates. Collapsing to one order-level date loses that, so we
-- store each item's own RDD here ("the PO repeated per item number, each
-- with its RDD"). The orders table keeps ONE row per PO (so warehouse/ack
-- email matching is unchanged); orders.required_delivery_date remains a
-- summary = the EARLIEST line-item RDD (the binding OTD deadline).
--
-- Run this in the Supabase SQL Editor.
-- ============================================================

alter table order_line_items add column if not exists line_no text;
alter table order_line_items add column if not exists required_delivery_date date;

-- One row per (order, line number). Lets the extractor upsert cleanly
-- instead of duplicating items on re-runs.
create unique index if not exists uq_line_items_order_lineno
    on order_line_items(order_id, line_no);

create index if not exists idx_line_items_rdd
    on order_line_items(required_delivery_date);

-- ============================================================
-- SPM Order Tracking System — Core Schema
-- Run this in the Supabase SQL Editor to set up your database.
-- ============================================================

-- ------------------------------------------------------------
-- BUYERS (Chevron, Aveon, Hillking, etc.)
-- ------------------------------------------------------------
create table if not exists buyers (
    id              uuid primary key default gen_random_uuid(),
    name            text not null unique,        -- e.g. 'Chevron', 'Aveon', 'Hillking'
    notification_email_sender text,               -- e.g. 'Chevron.Notification' — used to auto-detect their PO emails
    created_at      timestamptz default now()
);

-- ------------------------------------------------------------
-- SUPPLIERS (Flexitallic for gaskets, AIV for valves, etc.)
-- ------------------------------------------------------------
create table if not exists suppliers (
    id              uuid primary key default gen_random_uuid(),
    name            text not null unique,        -- e.g. 'Flexitallic', 'AIV'
    product_line    text,                         -- e.g. 'gasket', 'valve', 'lng'
    contact_email   text,
    created_at      timestamptz default now()
);

-- ------------------------------------------------------------
-- PRICE LIST (digitized from the supplier PDF price list)
-- One row per part code per supplier.
-- ------------------------------------------------------------
create table if not exists price_list (
    id              uuid primary key default gen_random_uuid(),
    supplier_id     uuid references suppliers(id),
    part_code       text not null,                -- supplier's own part/material code
    description     text,
    unit_price      numeric(12,2),
    currency        text default 'USD',
    price_list_version text,                      -- e.g. date stamp of the PDF this came from
    updated_at      timestamptz default now(),
    unique(supplier_id, part_code)
);

-- ------------------------------------------------------------
-- ORDERS — one row per Chevron (or other buyer) PO notification.
-- This is created automatically the moment the notification
-- email is detected (Stage 1).
-- ------------------------------------------------------------
create table if not exists orders (
    id                      uuid primary key default gen_random_uuid(),

    -- Identifiers
    buyer_id                uuid references buyers(id),
    buyer_po_number         text not null,          -- e.g. '0061440972'
    jde_job_id              text,                   -- from the notification email, e.g. '63426'
    branch_plant            text,                   -- e.g. '29000000WE'
    supplier_ref_number     text,                   -- buyer's internal supplier id, e.g. '1003023'
    po_amount               numeric(12,2),           -- amount stated in the notification email

    -- Stage 1 — notification received (T0)
    notification_received_at   timestamptz,          -- when the Chevron.Notification email landed
    pdf_attachment_path        text,                 -- where we saved the attached PO PDF

    -- Stage 2 — PDF extraction (auto-filled by Claude)
    extracted_description       text,
    product_line                text,                -- classified: 'gasket' / 'lng' / 'valve' / etc.
    required_delivery_date      date,                 -- RDD from the PO PDF
    extraction_confidence       text,                 -- 'high' / 'low' — flags if a human should double check
    extraction_raw              jsonb,                -- full raw extraction result, for audit/debug

    -- Stage 3 — acknowledgment (T1) — MANUAL, human clicks Chevron's button
    acknowledgment_status        text default 'pending', -- 'pending' / 'acknowledged'
    acknowledged_at              timestamptz,
    acknowledged_by              text,                 -- staff member name

    -- Stage 4 — pricing
    price_source                 text,                 -- 'price_list' / 'quotation_requested' / 'quotation_received'
    quoted_price                 numeric(12,2),
    quotation_requested_at       timestamptz,
    quotation_received_at        timestamptz,

    -- Stage 5 — SPM PO drafted & sent to supplier (T2)
    spm_po_number                text,                 -- internal SPM PO number once created
    supplier_id                  uuid references suppliers(id),
    spm_po_drafted_at            timestamptz,
    spm_po_sent_at                timestamptz,          -- T2 — the real automation milestone

    -- Status / housekeeping
    overall_status                text default 'new',   -- see status values below
    created_at                    timestamptz default now(),
    updated_at                    timestamptz default now()
);

-- overall_status values (informal enum, kept as text for flexibility):
--   new -> pending_acknowledgment -> acknowledged -> pricing -> po_sent
--   -> awaiting_supplier_so -> dispatched -> at_warehouse -> ready_for_dispatch
--   -> booked -> delivered -> waybill_received -> invoiced -> paid -> closed

-- ------------------------------------------------------------
-- ORDER LINE ITEMS — one row per part within a PO.
-- A single buyer PO can contain multiple line items
-- (e.g. 3 different gasket sizes in one PO).
-- ------------------------------------------------------------
create table if not exists order_line_items (
    id                  uuid primary key default gen_random_uuid(),
    order_id            uuid references orders(id) on delete cascade,

    buyer_part_code      text,                -- code as written on the buyer's PO
    supplier_part_code   text,                -- mapped code in supplier's system (Stage 4/5)
    description          text,
    quantity             numeric(12,2),
    unit_price           numeric(12,2),
    line_total           numeric(12,2),

    -- mismatch tracking (the original pain point from our planning)
    qty_ordered_from_supplier   numeric(12,2),
    qty_received_warehouse      numeric(12,2),
    qty_delivered_to_buyer      numeric(12,2),
    mismatch_flag               boolean default false,
    mismatch_notes               text,

    created_at           timestamptz default now()
);

-- ------------------------------------------------------------
-- EMAIL LOG — every email the system has processed.
-- Prevents re-processing the same email twice (cost control)
-- and gives a full audit trail.
-- ------------------------------------------------------------
create table if not exists processed_emails (
    id              uuid primary key default gen_random_uuid(),
    message_id      text unique not null,    -- the email's unique Message-ID header
    sender          text,
    subject         text,
    received_at     timestamptz,
    processed_at    timestamptz default now(),
    matched_order_id uuid references orders(id),
    processing_result text,                   -- 'created_order' / 'no_match' / 'error'
    raw_notes        text
);

-- ------------------------------------------------------------
-- Helpful indexes
-- ------------------------------------------------------------
create index if not exists idx_orders_buyer_po on orders(buyer_po_number);
create index if not exists idx_orders_status on orders(overall_status);
create index if not exists idx_orders_ack_status on orders(acknowledgment_status);
create index if not exists idx_line_items_order on order_line_items(order_id);
create index if not exists idx_processed_emails_message_id on processed_emails(message_id);

-- Migration 002: NLNG orders tables
-- Run in Supabase SQL editor.

CREATE TABLE IF NOT EXISTS nlng_orders (
    id                       uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    po_number                text        NOT NULL,
    variation_number         smallint    NOT NULL DEFAULT 0,
    -- Unique on (po_number, variation_number) so original + revisions coexist
    UNIQUE (po_number, variation_number),

    -- From PDF
    document_date            date,
    required_delivery_date   date,
    delivery_terms           text,          -- e.g. "DDP NLNG CHO PHC WAREHOUSE"
    delivery_address         text,          -- e.g. "FINIMA, BONNY ISLAND 503010"
    net_value                numeric(12,2),
    currency                 text        DEFAULT 'USD',
    contact_name             text,          -- "Queries To" field in PDF
    contact_email            text,

    -- Attachment
    pdf_attachment_path      text,
    pdf_url                  text,

    -- T0: when the email arrived at Yahoo
    notification_received_at timestamptz,

    -- Warehouse routing (Stage 2)
    sent_to_warehouse_at     timestamptz,
    warehouse_routing_raw    text,

    -- Warehouse stock check reply (Stage 3)
    stock_check_completed_at timestamptz,
    stock_check_raw          text,

    -- SPM's own PO to supplier (Stage 4)
    spm_po_number            text,
    spm_po_sent_at           timestamptz,

    -- Supplier SO (Stage 5)
    so_number                text,
    so_received_at           timestamptz,
    promised_date            date,

    -- SO forwarded to warehouse (Stage 6)
    so_sent_to_warehouse_at  timestamptz,

    -- Dispatch pipeline (Stage 7-9)
    flex_dispatch_ready_at   timestamptz,
    dispatch_instructions_sent_at timestamptz,
    ready_for_dispatch_at    timestamptz,
    dispatched_at            timestamptz,
    delivered_at             timestamptz,

    -- Derived
    overall_status           text        NOT NULL DEFAULT 'notification_received',

    created_at               timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS nlng_order_line_items (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    nlng_order_id   uuid        NOT NULL REFERENCES nlng_orders(id) ON DELETE CASCADE,
    item_no         smallint,
    mesc_code       text,                  -- e.g. "8541460821"
    description     text,                  -- e.g. "GASKET:SPW;FLEXITALL IC 350 MM LB"
    quantity        numeric(10,3),
    uom             text,                  -- e.g. "PC"
    unit_price      numeric(12,2),
    net_amount      numeric(12,2),
    int_article_no  text,                  -- "Int. Article No." field
    delivery_date   date,
    created_at      timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS nlng_orders_po_number_idx
    ON nlng_orders (po_number);

CREATE INDEX IF NOT EXISTS nlng_order_line_items_order_idx
    ON nlng_order_line_items (nlng_order_id);

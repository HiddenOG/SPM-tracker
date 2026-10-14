-- SPM Quotations: run in Supabase SQL Editor

create table if not exists quotations (
  id                uuid primary key default gen_random_uuid(),
  quote_number      text unique,
  quote_seq         integer,
  client            text not null check (client in ('chevron','nlng','seplat')),
  status            text not null default 'draft' check (status in ('draft','sent','accepted','rejected')),
  reference_po      text,
  recipient_name    text,
  recipient_dept    text,
  recipient_company text,
  recipient_tel     text,
  recipient_email   text,
  prepared_by       text,
  subject           text,
  quote_date        date,
  validity_days     integer default 30,
  lead_time         text,
  delivery_dest     text,
  country_import    text,
  shipping_mode     text default 'Sea',
  discount_pct      numeric(5,2) default 0,
  shipping_charges  numeric(12,2) default 0,
  sub_total         numeric(14,2) default 0,
  total             numeric(14,2) default 0,
  notes             text,
  sig_name          text,
  created_by        text,
  created_at        timestamptz default now(),
  sent_at           timestamptz
);

create table if not exists quotation_line_items (
  id            uuid primary key default gen_random_uuid(),
  quotation_id  uuid references quotations(id) on delete cascade,
  item_no       integer,
  description   text,
  uom           text default 'EA',
  quantity      numeric(12,4) default 0,
  unit_price    numeric(12,4) default 0,
  tax_rate      numeric(6,4) default 0,
  line_total    numeric(14,2) default 0
);

-- If the table already exists, add the tax_rate column manually:
-- alter table quotation_line_items add column if not exists tax_rate numeric(6,4) default 0;

create index if not exists qli_quotation_id_idx on quotation_line_items(quotation_id);

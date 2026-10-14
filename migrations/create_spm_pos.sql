-- SPM Purchase Orders (the POs Special Piping raises on its own suppliers).
-- Run in the Supabase SQL Editor.
--
-- Deliberately SEPARATE from spm_purchase_orders. That table is written by the
-- email listeners and mirrors POs already sent out; this one is a document
-- generator filled in by hand. Mixing the two would let the form overwrite
-- pipeline data and let the listeners overwrite someone's draft.

create table if not exists spm_pos (
  id               uuid primary key default gen_random_uuid(),
  -- 'S.P.M.-C.N.L.-3131-0061451715-0061451710-FLEXITALLIC'. Not unique: SPM
  -- reuses references, and a number rebuilt after an edit may repeat an old one.
  po_number        text,
  -- The running reference (the 3131 above). One counter for the whole company,
  -- shared with the Chevron and NLNG pipelines — see _next_po_seq() in app.py.
  -- Issued once per order and never reissued, so an edit renumbers the string
  -- around it but never the reference itself.
  po_seq           integer,
  client_po        text,                        -- every client PO on the order, space separated
  client           text,                        -- chevron / nlng / seplat / exxon / total / gca / others
  -- Only for 'others': the one-off customer typed by hand. Its initials become
  -- the client code in the PO number, the same way the quotations do it.
  client_name      text,
  -- Kept for the pipeline; the form has no status box and always writes 'draft'.
  status           text not null default 'draft'
                     check (status in ('draft','issued','cancelled')),
  po_date          date,

  vendor_name      text,
  vendor_role      text,
  vendor_company   text,
  vendor_address   text,
  vendor_email     text,

  deliver_to       text,
  shipping_terms   text,
  shipping_method  text,
  delivery_date    text,                        -- free text: the sheet allows 'TBA', '4 weeks ARO', a date…

  manager          text,
  sales_person     text,
  currency         text default 'USD',
  total            numeric(14,2) default 0,
  notes            text,

  created_by       text,
  created_at       timestamptz default now(),
  issued_at        timestamptz
);

create table if not exists spm_po_line_items (
  id           uuid primary key default gen_random_uuid(),
  spm_po_id    uuid references spm_pos(id) on delete cascade,
  item_no      integer,
  client_po_no text,                             -- per line: one SPM PO can cover several client POs
  quote_ref    text,                             -- per line: the supplier quote each item came from
  description  text,
  quantity     numeric(12,4) default 0,
  unit_price   numeric(12,4) default 0,
  line_total   numeric(14,2) default 0
);

create index if not exists spm_pos_created_idx   on spm_pos(created_at desc);
create index if not exists spm_pos_client_po_idx on spm_pos(client_po);
create index if not exists spm_po_li_parent_idx  on spm_po_line_items(spm_po_id);

-- If the tables were created before the client_name column existed:
-- alter table spm_pos add column if not exists client_name text;

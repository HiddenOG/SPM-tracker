-- Stock register: what is physically on the shelf.
-- Run in the Supabase SQL Editor.
--
-- Deliberately a MANUAL register, not a calculated balance. Stock cannot be
-- computed from the order pipeline because nothing in it records quantities:
-- of 154 warehouse replies on file, none carry a count — they say "Completely
-- delivered" or "PO not in stock" in prose. So the count starts from what
-- someone physically counts, and moves from what someone records.

create sequence if not exists stock_seq_counter start 1;

create table if not exists stock_items (
  id              uuid primary key default gen_random_uuid(),
  -- A short label people can say out loud and write on a shelf tag. The uuid
  -- above is the real key; this is the one that goes in an email.
  --
  -- The number comes from a Postgres sequence rather than "the highest so far,
  -- plus one". Two reasons: counting from the surviving rows hands a deleted
  -- item's number to the next one — and STK-0007 may already be written on a
  -- shelf tag — and two people adding at the same moment would both read the
  -- same highest number and both claim it.
  stock_id        text,                        -- 'STK-0042'
  stock_seq       integer not null default nextval('stock_seq_counter'),
  description     text not null,
  quantity        numeric(14,3) not null default 0,
  uom             text default 'EA',
  unit_cost       numeric(14,4),
  notes           text,
  last_counted_at timestamptz,
  created_by      text,
  created_at      timestamptz default now(),
  updated_at      timestamptz default now()
);

-- Every change to a quantity, so the number on screen can always be explained.
-- Without this someone edits 12 to 8 and nobody knows whether four were sold,
-- damaged, or simply miscounted the first time.
create table if not exists stock_movements (
  id             uuid primary key default gen_random_uuid(),
  stock_item_id  uuid references stock_items(id) on delete cascade,
  change         numeric(14,3) not null,      -- +12, -4
  quantity_after numeric(14,3) not null,
  note           text,
  created_by     text,
  created_at     timestamptz default now()
);

create index if not exists stock_items_seq_idx      on stock_items(stock_seq desc);
create index if not exists stock_items_created_idx  on stock_items(created_at desc);
create index if not exists stock_moves_item_idx     on stock_movements(stock_item_id, created_at desc);

-- If the table was created before the stock_id columns existed:
-- create sequence if not exists stock_seq_counter start 1;
-- alter table stock_items add column if not exists stock_id  text;
-- alter table stock_items add column if not exists stock_seq integer
--   not null default nextval('stock_seq_counter');

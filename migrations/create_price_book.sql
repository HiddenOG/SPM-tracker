-- Gasket price lists and weights, so a client's line item can be priced by
-- looking it up rather than reading tables by eye.
-- Run in the Supabase SQL Editor, then load the lists with
-- scripts/load_price_book.py (it replaces each source whole on every run).

-- One row per priced item: what it is, and what it costs on that list.
create table if not exists price_book (
  id          bigserial primary key,
  source      text not null,      -- 'flex_2022' (Flexitallic list, GBP) | 'spm_2026' (SPM's own, USD)
  page        text,               -- the list's page number, for checking by eye
  product     text not null,      -- spiral_wound | ring_joint | isolation_kit | frg
  variant     text,               -- 'Low emission', 'Low stress', 'Above 24in'
  standard    text,               -- 'ASME B16.20', 'ASME B16.47', 'EN1514'
  series      text,               -- B16.47 series A or B
  style       text,               -- CG, CGI, LEI, LSI | R, RX, BX | Isoflex LT/FS | FRG Sigma/Hot
  winding     text,               -- spiral wound: 316L, 304L
  filler      text,               -- Flexicarb SEL (graphite), Flexicarb - HT
  outer_ring  text,               -- Mild Steel, 316, 304
  inner_ring  text,
  material    text,               -- ring joints: Soft Iron, 316SS
  size_in     numeric,            -- nominal pipe size in inches
  size_dn     integer,            -- EN sizes (DN)
  ring_no     text,               -- R32, RX24, BX152
  rating      text,               -- '150'...'2500', 'PN40'
  price       numeric,            -- null with poa = price on application
  currency    text not null,
  poa         boolean default false,
  note        text,
  loaded_at   timestamptz default now()
);
create index if not exists price_book_lookup on price_book (product, size_in, rating);
create index if not exists price_book_ring on price_book (ring_no);

-- Weight each, in kg, from the weights workbook.
create table if not exists gasket_weights (
  id        bigserial primary key,
  kind      text not null,        -- CGI, CG, CAMPROFILE, ELASTAGRAPH, SOFT_CUT, R_OVAL, R_OCTAG, RX, BX, INSUL_F, INSUL_E
  size_in   numeric,
  size_dn   integer,
  ring_no   text,
  rating    text,
  kg        numeric not null,
  sheet     text
);
create index if not exists gasket_weights_lookup on gasket_weights (kind, size_in, rating);

-- These are SPM's buying prices: with row level security on and no policies,
-- only the server's service key can read them, never the public anon key.
alter table price_book enable row level security;
alter table gasket_weights enable row level security;

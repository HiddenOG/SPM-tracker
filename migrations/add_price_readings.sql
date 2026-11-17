-- Gasket line readings, kept so the same words are only paid for once.
-- scripts/price_book.py reads a client's description into fields with Claude,
-- then saves the result here. id = sha256(rules version | description), and the
-- rules version changes whenever the reader's rules, answer shape or model
-- change, so old readings are simply never matched again (safe to delete any
-- time - the next question re-reads the line).
create table if not exists price_readings (
    id          text primary key,
    description text not null,
    spec        jsonb not null,
    rules       text not null,
    created_at  timestamptz not null default now()
);

alter table price_readings enable row level security;

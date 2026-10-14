-- 016_emails.sql
-- Email story: one row per email, linked to the order(s) it concerns.
-- Powers the "Email Story" drawer in the UI — full timeline of every
-- email related to a PO from arrival through delivery.

CREATE TABLE IF NOT EXISTS emails (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id      text        UNIQUE NOT NULL,       -- RFC Message-ID header (dedup key)
    in_reply_to     text,                              -- In-Reply-To header
    thread_id       text,                              -- root Message-ID (groups a reply chain)
    order_id        uuid        REFERENCES orders(id)      ON DELETE SET NULL,
    nlng_order_id   uuid        REFERENCES nlng_orders(id) ON DELETE SET NULL,
    direction       text        NOT NULL DEFAULT 'in', -- 'in' received by SPM | 'out' sent by SPM
    from_address    text,
    to_address      text,
    subject         text,
    body_text       text,
    received_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS emails_order_id_idx       ON emails(order_id);
CREATE INDEX IF NOT EXISTS emails_nlng_order_id_idx  ON emails(nlng_order_id);
CREATE INDEX IF NOT EXISTS emails_thread_id_idx      ON emails(thread_id);
CREATE INDEX IF NOT EXISTS emails_received_at_idx    ON emails(received_at);

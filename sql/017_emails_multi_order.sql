-- Allow the same email to appear in multiple order stories.
-- Previously unique on message_id alone; now unique per (message_id, order_id, nlng_order_id)
-- so a shared thread email can be linked to every PO that references it.

ALTER TABLE emails DROP CONSTRAINT IF EXISTS emails_message_id_key;

ALTER TABLE emails
    ADD CONSTRAINT emails_message_id_order_unique
    UNIQUE NULLS NOT DISTINCT (message_id, order_id, nlng_order_id);

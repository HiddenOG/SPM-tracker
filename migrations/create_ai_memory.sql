-- Persistent AI summaries per order — enables RAG long-term memory.
-- The unique index on (order_id, order_type) allows upsert on conflict.
CREATE TABLE IF NOT EXISTS ai_memory (
    id          uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    order_id    uuid        NOT NULL,
    order_type  text        NOT NULL DEFAULT 'chevron',
    summary     text        NOT NULL,
    updated_at  timestamptz DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ai_memory_order_unique
    ON ai_memory(order_id, order_type);

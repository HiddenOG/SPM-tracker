-- Chevron PO cancellation / closure.
--
-- Chevron's GEP SMART system emails us when a purchase order it already sent
-- is cancelled ("... is cancelled by <name> on <date>") or closed ("... has
-- been closed"). imap_listener used to detect these and throw them away, so
-- an order cancelled at Chevron kept moving through our pipeline — nine of
-- them were still live, one as far along as ready_for_dispatch.
--
-- Status is derived from milestone timestamps everywhere else in this schema
-- (delivered_at -> delivered, dispatched_at -> dispatched, ...), so a
-- cancellation gets a timestamp column too rather than a bare status string.
-- That keeps sync.derive_status() able to rebuild the correct status from
-- the data alone, which is what makes a cold rebuild reproducible.
ALTER TABLE orders
  ADD COLUMN IF NOT EXISTS cancelled_at       timestamptz,
  ADD COLUMN IF NOT EXISTS cancellation_kind  text,
  ADD COLUMN IF NOT EXISTS cancellation_actor text,
  ADD COLUMN IF NOT EXISTS cancellation_raw   text;

COMMENT ON COLUMN orders.cancelled_at IS
  'When Chevron cancelled/closed this PO. Taken from the notification email''s '
  'own date: the stated date in the body is written inconsistently (7/10/2026 '
  'month-first, 24/04/2025 day-first) and cannot be parsed unambiguously.';
COMMENT ON COLUMN orders.cancellation_kind IS
  '"cancelled" (Chevron cancelled the order) or "closed" (Chevron closed it '
  'out, normally after delivery).';
COMMENT ON COLUMN orders.cancellation_actor IS
  'Person named in the email, e.g. "Elohor Chinakwe". Audit trail only.';
COMMENT ON COLUMN orders.cancellation_raw IS
  'The sentence the decision was read from, kept so a human can verify it.';

CREATE INDEX IF NOT EXISTS idx_orders_cancelled_at
  ON orders (cancelled_at) WHERE cancelled_at IS NOT NULL;

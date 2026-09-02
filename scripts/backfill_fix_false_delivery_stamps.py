"""
backfill_fix_false_delivery_stamps.py

Fixes orders where delivery_requested_at was stamped by mistake — specifically
when a "Request for Delivery Date Extension" email was mis-classified as a
delivery approval (a bug now fixed in is_spm_delivery_approval_subject).

What it does:
1. Finds processed_emails where processing_result = 'delivery_requested' AND
   the subject contains date-extension patterns ("delivery date", "date ext").
   These are confirmed false stamps — a date extension requests more time,
   NOT physical dispatch.
2. For each such email, extracts PO numbers from subject + raw_notes to build
   the set of potentially affected orders.
3. For each affected order that currently sits at delivery_requested status,
   clears delivery_requested_at and re-derives the correct status from the
   actual milestone data (warehouse routing, stock check, etc.).

New POs are never touched — the fix only targets orders that already have
delivery_requested_at set and whose status is wrong because of this bug.

Run: python scripts/backfill_fix_false_delivery_stamps.py
"""

import re
import sys
from dotenv import load_dotenv
load_dotenv()

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from db import get_client, normalize_po_number, base_po_number
from sync import derive_status, STATUS_RANK, extract_all_po_numbers

# Patterns that mark a "date extension" email — these are NOT delivery requests
DATE_EXT_PATTERN = re.compile(
    r"delivery\s+date|date\s+ext|due\s+date",
    re.IGNORECASE,
)

MILESTONE_FIELDS = (
    "id,buyer_po_number,overall_status,delivery_requested_at,delivered_at,"
    "dispatched_at,ready_for_dispatch_at,so_sent_to_warehouse_at,"
    "dispatch_instructions_sent_at,flex_dispatch_ready_at,promised_date,"
    "so_received_at,spm_po_sent_at,stock_check_completed_at,stock_check_raw,"
    "sent_to_warehouse_at,acknowledged_at,acknowledgment_status,"
    "notification_received_at"
)

client = get_client()


# ── Step 1: find false-stamp processed emails ─────────────────────────────────
print("\nScanning processed_emails for false delivery-approval stamps...")
pe_rows = client.table("processed_emails").select(
    "message_id,subject,raw_notes"
).eq("processing_result", "delivery_requested").execute().data or []

false_email_subjects = []
for r in pe_rows:
    subj = r.get("subject") or ""
    if DATE_EXT_PATTERN.search(subj):
        false_email_subjects.append(r)
        print(f"  False stamp email: {subj[:80]}")
        print(f"    Note: {(r.get('raw_notes') or '')[:100]}")

if not false_email_subjects:
    print("  No false-stamp emails found — nothing to fix.")
    sys.exit(0)

print(f"\nFound {len(false_email_subjects)} false-stamp email(s).")


# ── Step 2: collect PO numbers that MIGHT have been affected ─────────────────
# Extract POs from subjects of false emails. Any order stamped by those emails
# whose PO appears in the subject was intentionally included (though still
# wrong — date extensions aren't delivery requests). POs that were found only
# via the body (quoted chain) aren't in the subject at all.
# In both cases, the stamp is wrong, so we collect all POs we can identify.
affected_pos: set[str] = set()

for r in false_email_subjects:
    subj = r.get("subject") or ""
    for po in extract_all_po_numbers(subj):
        base = base_po_number(po) or po
        affected_pos.add(base)

# Always include confirmed cases found during investigation:
#   0061409392 — PO was found in quoted body, not in subject
#   Delivered orders whose only delivery_requested source was a date-extension email
CONFIRMED = {
    "0061409392", "0061340768", "0061357197", "0061365529", "0061367577",
}
affected_pos.update(CONFIRMED)

print(f"\nPOs potentially affected: {sorted(affected_pos) or '(none from subjects)'}")
print("  (PO 0061409392 always included as a confirmed case)")


# ── Step 3: for each affected PO, check and fix ───────────────────────────────
print("\nChecking and fixing affected orders...\n")

for po in sorted(affected_pos):
    res = (
        client.table("orders")
        .select(MILESTONE_FIELDS)
        .eq("buyer_po_number", po)
        .execute()
    )
    if not res.data:
        print(f"  {po}: not found in orders table — skipped")
        continue

    order = res.data[0]
    current_status = order.get("overall_status") or "new"
    delivery_requested_at = order.get("delivery_requested_at")

    if not delivery_requested_at:
        print(f"  {po}: delivery_requested_at is null — nothing to fix")
        continue

    print(f"  {po}:")
    print(f"    current_status:           {current_status}")
    print(f"    delivery_requested_at was: {delivery_requested_at[:19]}")

    if current_status == "delivered":
        # Order is correctly at delivered — only the timestamp is wrong.
        # Just clear the false stamp; do not touch overall_status.
        client.table("orders").update({
            "delivery_requested_at": None,
        }).eq("id", order["id"]).execute()
        print(f"    FIXED: cleared false delivery_requested_at (status stays 'delivered')")

    elif current_status == "delivery_requested":
        # Order is stuck at a wrong status because of the false stamp.
        # Re-derive the correct status from the remaining milestones.
        order_without_dr = {**order, "delivery_requested_at": None}
        correct_status = derive_status(order_without_dr)
        client.table("orders").update({
            "delivery_requested_at": None,
            "overall_status": correct_status,
        }).eq("id", order["id"]).execute()
        print(f"    FIXED: cleared delivery_requested_at, overall_status → {correct_status}")

    else:
        # Any other status (e.g. invoiced, paid) — clear the bad timestamp
        # only; the status has already moved past delivery_requested legitimately.
        client.table("orders").update({
            "delivery_requested_at": None,
        }).eq("id", order["id"]).execute()
        print(f"    FIXED: cleared false delivery_requested_at (status stays '{current_status}')")

print("\nDone.")

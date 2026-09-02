"""
backfill_fix_routing_body.py

Fixes orders where the warehouse routing email was processed by an OLD version of
gmail_ack_listener that stopped when Claude API failed — meaning Part B (the warehouse
stamp) never ran, warehouse_routing_raw was never populated from that email, and
sent_to_warehouse_at may point to an earlier routing email.

How the bug is identified:
  processed_emails.raw_notes contains "Claude extraction failed:" WITHOUT "(non-fatal)".
  The current code always appends "(non-fatal)" — the old format means old code ran.

What the backfill does:
  1. Finds all processed_emails rows matching the old-failure pattern from SPM sender.
  2. For each, re-fetches the original email from Gmail IMAP by Message-ID.
  3. Extracts the body text and email date.
  4. Finds the matching order (tries full PO including suffix, falls back to base PO).
  5. If the email date is >= the order's current sent_to_warehouse_at (or null), applies
     stamp_sent_to_warehouse() to update warehouse_routing_raw and sent_to_warehouse_at.
  6. Updates processed_emails.matched_order_id if it was missing.

New POs are unaffected — current code treats Claude failure as non-fatal and always
reaches Part B. This script is a one-time fix for historically missed routing stamps.

Run: python scripts/backfill_fix_routing_body.py
"""

import email
import re
import sys
from dotenv import load_dotenv

load_dotenv()

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from db import get_client, base_po_number
import sync
from email_utils import get_email_body_text, decode_mime_words
from gmail_ack_listener import (
    connect_to_gmail,
    find_order_by_po_number,
    stamp_sent_to_warehouse,
)
from config import SPM_SENDER

# Old format:     "Claude extraction failed: Error code: 400 ..."
# Current format: "Claude extraction failed (non-fatal): ..."
# The old format is a substring of the new format, so we must exclude the new one.
_OLD_FAIL = re.compile(r"Claude extraction failed:", re.IGNORECASE)
_NEW_FAIL = re.compile(r"Claude extraction failed \(non-fatal\):", re.IGNORECASE)


def _is_old_failure(raw_notes: str) -> bool:
    if not raw_notes:
        return False
    return bool(_OLD_FAIL.search(raw_notes)) and not bool(_NEW_FAIL.search(raw_notes))


def _find_in_imap(imap, message_id: str) -> bytes | None:
    """Search Gmail IMAP for an email by Message-ID. Returns raw RFC822 bytes or None."""
    mid = message_id.strip()
    for search_mid in [mid, mid.strip("<>")]:
        try:
            uids = imap.search(["HEADER", "Message-ID", search_mid])
        except Exception:
            continue
        if uids:
            data = imap.fetch(uids[-1:], ["RFC822"])
            uid = list(data.keys())[0]
            return (data[uid] or {}).get(b"RFC822")
    return None


def _find_order(po_number: str) -> dict | None:
    """
    Look up order by PO number. For change orders (e.g. "0061409392-001") the DB
    row uses the base PO number ("0061409392"), so fall back to the base if the
    exact lookup fails.
    """
    order = find_order_by_po_number(po_number)
    if order:
        return order
    base = base_po_number(po_number)
    if base and base != po_number:
        return find_order_by_po_number(base)
    return None


client = get_client()


# ── Step 1: find affected processed_emails ────────────────────────────────────
print("\nScanning processed_emails for routing emails affected by old-code Claude failure...")

pe_rows = (
    client.table("processed_emails")
    .select("id,message_id,sender,subject,matched_order_id,raw_notes")
    .ilike("raw_notes", "%Claude extraction failed:%")
    .execute()
    .data or []
)

affected = []
for row in pe_rows:
    if not _is_old_failure(row.get("raw_notes") or ""):
        continue
    sender = row.get("sender") or ""
    if SPM_SENDER.lower() not in sender.lower():
        continue
    subject = row.get("subject") or ""
    if not sync.extract_all_po_numbers(subject):
        continue  # not a routing-style subject
    affected.append(row)

if not affected:
    print("  No affected routing emails found — nothing to fix.")
    sys.exit(0)

print(f"  Found {len(affected)} affected routing email(s):")
for r in affected:
    print(f"    {r['subject']!r}  |  {(r.get('raw_notes') or '')[:80]}")


# ── Step 2: re-fetch emails from Gmail IMAP ───────────────────────────────────
print("\nConnecting to Gmail IMAP...")
imap = connect_to_gmail()

recovered = []  # list of dicts: {email_date, body_text, po_numbers, pe_id}
for row in affected:
    mid = row["message_id"]
    raw = _find_in_imap(imap, mid)
    if not raw:
        print(f"  ✗ Message-ID {mid!r} not found in Gmail — skipped")
        continue

    msg = email.message_from_bytes(raw)
    body_text = get_email_body_text(msg).strip()
    email_date = sync.parse_email_date(msg)
    subject = decode_mime_words(msg.get("Subject", "")) or row["subject"]
    po_numbers = sync.extract_all_po_numbers(subject)

    if not po_numbers:
        print(f"  ✗ No PO numbers in subject {subject!r} — skipped")
        continue

    print(f"  ✓ Re-fetched {mid!r}")
    print(f"      subject={subject!r}, date={email_date[:10]}, body={body_text[:80]!r}")
    recovered.append({
        "pe_id": row["id"],
        "pe_matched_order_id": row.get("matched_order_id"),
        "email_date": email_date,
        "body_text": body_text,
        "po_numbers": po_numbers,
    })

imap.logout()

if not recovered:
    print("\nNo emails could be re-fetched. Nothing to update.")
    sys.exit(0)

# Process oldest-to-newest so the most recent routing email wins per order
recovered.sort(key=lambda r: r["email_date"])


# ── Step 3: apply routing stamps ──────────────────────────────────────────────
print(f"\nApplying routing stamps for {len(recovered)} recovered email(s)...")
updated = 0
skipped = 0

for rec in recovered:
    email_date = rec["email_date"]
    body_text = rec["body_text"] or None
    po_numbers = rec["po_numbers"]

    for po in po_numbers:
        order = _find_order(po)
        if not order:
            print(f"  ✗ PO {po}: no matching order in DB — skipped")
            skipped += 1
            continue

        current_stamp = order.get("sent_to_warehouse_at") or ""
        if current_stamp and email_date < current_stamp:
            print(f"  ⏭  PO {po}: email date {email_date[:10]} is older than current stamp "
                  f"{current_stamp[:10]} — skipped")
            skipped += 1
            continue

        stamp_sent_to_warehouse(order["id"], email_date, body_text)
        print(f"  ✓ PO {po}: warehouse_routing_raw updated, sent_to_warehouse_at → {email_date[:10]}")
        updated += 1

        # Patch processed_emails.matched_order_id if the old code missed it
        if not rec["pe_matched_order_id"]:
            client.table("processed_emails").update(
                {"matched_order_id": order["id"]}
            ).eq("id", rec["pe_id"]).execute()

print(f"\nDone. {updated} order(s) updated, {skipped} skipped.")

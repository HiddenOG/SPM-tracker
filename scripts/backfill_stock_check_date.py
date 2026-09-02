"""
backfill_stock_check_date.py — Fix stock_check_completed_at for delivered orders.

The warehouse sends two emails per order:
  1. Stock check reply  ("items in stock / ready")       ← the date we want
  2. Delivery reply     ("items delivered")               ← was wrongly stamped

For all 48 delivered orders where stock_check_completed_at == delivered_at,
this script searches Gmail for the EARLIEST warehouse reply email matching
the PO number and re-stamps stock_check_completed_at with that date.

Safe to re-run — only touches rows where the dates are still identical.

Usage:
    python scripts/backfill_stock_check_date.py            # live run
    python scripts/backfill_stock_check_date.py --dry-run  # preview only
"""

import email
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from imapclient import IMAPClient

from db import get_client
import sync
from config import WAREHOUSE_EMAIL

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

DRY_RUN     = "--dry-run" in sys.argv
FETCH_BATCH = 50


def connect_gmail() -> IMAPClient:
    host = os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com")
    port = int(os.environ.get("GMAIL_IMAP_PORT", 993))
    imap = IMAPClient(host, port=port, use_uid=True, ssl=True)
    imap.login(os.environ["GMAIL_EMAIL"], os.environ["GMAIL_APP_PASSWORD"])
    return imap


def get_bad_orders(db) -> list[dict]:
    """Return delivered orders where stock_check_completed_at == delivered_at."""
    res = db.table("orders").select(
        "id, buyer_po_number, stock_check_completed_at, delivered_at"
    ).not_.is_("delivered_at", "null").not_.is_("stock_check_completed_at", "null").execute()

    bad = [
        o for o in (res.data or [])
        if o["stock_check_completed_at"][:10] == o["delivered_at"][:10]
    ]
    return bad


def find_earliest_warehouse_reply(imap: IMAPClient, po_number: str) -> str | None:
    """
    Search Gmail for ALL warehouse reply emails mentioning this PO number.
    Return the date of the EARLIEST one — that's the stock check, not delivery.
    """
    imap.select_folder("[Gmail]/All Mail")

    # Search by sender + PO number in body
    try:
        uids = imap.search(["FROM", WAREHOUSE_EMAIL, "BODY", po_number])
    except Exception as e:
        print(f"  ⚠️  Gmail search error for {po_number}: {e}")
        return None

    if not uids:
        return None

    # Fetch internal dates for all matching emails, find earliest
    earliest_date = None
    for i in range(0, len(uids), FETCH_BATCH):
        chunk = uids[i:i + FETCH_BATCH]
        try:
            data = imap.fetch(chunk, ["RFC822"])
        except Exception as e:
            print(f"  ⚠️  Fetch error for {po_number}: {e}")
            continue

        for uid in chunk:
            raw = data.get(uid, {}).get(b"RFC822")
            if not raw:
                continue
            msg = email.message_from_bytes(raw)
            date_str = sync.parse_email_date(msg)
            if earliest_date is None or date_str < earliest_date:
                earliest_date = date_str

    return earliest_date


def run():
    db = get_client()

    bad_orders = get_bad_orders(db)
    if not bad_orders:
        print("Nothing to fix — all delivered orders already have correct dates.")
        return

    print(f"Found {len(bad_orders)} delivered order(s) with stock_check_completed_at == delivered_at")
    if DRY_RUN:
        print("  [DRY RUN — no changes will be written]\n")

    imap = connect_gmail()
    try:
        fixed = 0
        not_found = 0

        for o in bad_orders:
            po = o["buyer_po_number"]
            wrong_date = o["stock_check_completed_at"][:10]

            earliest = find_earliest_warehouse_reply(imap, po)

            if not earliest:
                print(f"  ⚠️  {po} — no warehouse emails found in Gmail, skipping")
                not_found += 1
                continue

            earliest_date = earliest[:10]

            if earliest_date == wrong_date:
                print(f"  ⚠️  {po} — earliest warehouse email is same date as delivery ({wrong_date}), skipping")
                not_found += 1
                continue

            print(f"  ✅  {po} — stock_check: {wrong_date} → {earliest_date}  (delivery: {o['delivered_at'][:10]})")

            if not DRY_RUN:
                db.table("orders").update({
                    "stock_check_completed_at": earliest
                }).eq("id", o["id"]).execute()

            fixed += 1

    finally:
        try:
            imap.logout()
        except Exception:
            pass

    action = "would update" if DRY_RUN else "updated"
    print(f"\nDone. {action} {fixed} order(s). {not_found} skipped (no earlier email found).")


if __name__ == "__main__":
    run()

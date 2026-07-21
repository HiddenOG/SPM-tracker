"""
backfill_delivery_requested.py — Stamp delivery_requested_at for orders missing it.

SPM sends "Request for Delivery Approval" emails FROM specialpiping@gmail.com to
NIGEC and/or the warehouse.  The live listener (gmail_ack_listener.py) now catches
these going forward, but historical emails were never processed.

This script searches Gmail All Mail for every SPM-sent email with "Request for
Delivery" in the subject, extracts Chevron PO numbers from subject + body, and
stamps delivery_requested_at for any matched order that doesn't have it yet.

Safe to re-run — uses .is_("delivery_requested_at", "null") guard, so orders
that already have the date from the warehouse-originated "REQUEST FOR DELIVERY"
email are left untouched.

Usage:
    python scripts/backfill_delivery_requested.py            # live run
    python scripts/backfill_delivery_requested.py --dry-run  # preview only
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

from db import get_client, normalize_po_number
import sync
from config import SPM_SENDER

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


def get_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(errors="replace")
    return ""


def find_order(po: str) -> dict | None:
    po = normalize_po_number(po) or po
    res = (
        get_client().table("orders")
        .select("id, buyer_po_number, delivery_requested_at")
        .eq("buyer_po_number", po)
        .order("created_at", desc=False)
        .execute()
    )
    return res.data[0] if res.data else None


def run() -> None:
    db = get_client()
    imap = connect_gmail()

    try:
        imap.select_folder("[Gmail]/All Mail")

        print("Searching Gmail All Mail for SPM delivery approval emails...")
        uids = imap.search(["FROM", SPM_SENDER, "SUBJECT", "Request for Delivery"])
        print(f"Found {len(uids)} matching email(s)\n")

        if not uids:
            print("Nothing to backfill.")
            return

        if DRY_RUN:
            print("  [DRY RUN — no changes will be written]\n")

        stamped          = 0
        skipped_no_order = 0
        skipped_has_date = 0

        for i in range(0, len(uids), FETCH_BATCH):
            chunk = uids[i:i + FETCH_BATCH]
            data  = imap.fetch(chunk, ["RFC822"])

            for uid in chunk:
                raw = data.get(uid, {}).get(b"RFC822")
                if not raw:
                    continue

                msg        = email.message_from_bytes(raw)
                subject    = msg.get("Subject", "")
                body       = get_body(msg)
                email_date = sync.parse_email_date(msg)

                po_numbers = sync.extract_all_po_numbers(subject + " " + body)
                if not po_numbers:
                    continue

                for po in po_numbers:
                    order = find_order(po)
                    if not order:
                        skipped_no_order += 1
                        continue

                    if order.get("delivery_requested_at"):
                        skipped_has_date += 1
                        print(
                            f"  ⚡ {po} — already has "
                            f"delivery_requested_at={order['delivery_requested_at'][:10]}, skipping"
                        )
                        continue

                    print(f"  ✅ {po} — stamping delivery_requested_at={email_date[:10]}")

                    if not DRY_RUN:
                        db.table("orders").update({
                            "delivery_requested_at": email_date,
                        }).eq("id", order["id"]).is_("delivery_requested_at", "null").execute()
                        sync.advance_status(db, order["id"], "delivery_requested")

                    stamped += 1

    finally:
        try:
            imap.logout()
        except Exception:
            pass

    action = "would stamp" if DRY_RUN else "stamped"
    print(
        f"\nDone. {action} {stamped} order(s). "
        f"{skipped_no_order} PO(s) not in DB. "
        f"{skipped_has_date} already had delivery_requested_at."
    )


if __name__ == "__main__":
    run()

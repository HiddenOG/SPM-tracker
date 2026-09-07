"""
backfill_missed_po_typo.py — One-time backfill for the "PURCHASE ORDR" typo bug.

supplier_po_parser.py's IMAP search and subject check both required the exact
substring "PURCHASE ORDER". SPM sent an outgoing PO email with the subject
typo "PURCHASE ORDR" (missing the E) — invisible to both the IMAP-level
search and the Python-level check, so it (and any other similarly-typoed
email) never got picked up. The cursor has since advanced past it because
later, correctly-matched emails pushed the UID watermark forward.

This script re-scans [Gmail]/All Mail for recent SPM "PURCHASE*" emails
(no cursor gating), and processes any that are still missing from
processed_emails using the now-fixed, typo-tolerant matching logic.

Run once from the project root:
    python scripts/backfill_missed_po_typo.py
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from db import get_client
from email_utils import decode_mime_words, is_already_processed
import email as email_mod
import sync
from supplier_po_parser import (
    connect_to_gmail, is_purchase_order_subject, extract_supplier_name,
    process_outgoing_po,
)
from config import SPM_SENDER


def backfill(days_back: int = 10) -> None:
    client_db = get_client()
    imap = connect_to_gmail()

    try:
        folder = "[Gmail]/All Mail"
        imap.select_folder(folder)

        since = (datetime.now() - timedelta(days=days_back)).strftime("%d-%b-%Y")
        print(f"Searching {folder} for SPM 'PURCHASE*' emails since {since} (no cursor gating)...")

        uids = imap.search(["FROM", "specialpiping@gmail.com", "SUBJECT", "PURCHASE", "SINCE", since])
        print(f"Found {len(uids)} candidate emails to check.")

        checked = 0
        recovered = 0

        for uid in uids:
            msg_data = imap.fetch([uid], ["RFC822"])
            raw = msg_data.get(uid)
            if not raw:
                continue
            msg = email_mod.message_from_bytes(raw[b"RFC822"])

            subject = decode_mime_words(msg.get("Subject", ""))
            if not is_purchase_order_subject(subject):
                continue  # not actually a PO email, just contains "PURCHASE" incidentally

            checked += 1
            message_id = msg.get("Message-ID", "") or \
                f"{msg.get('From')}-{msg.get('Date')}-{msg.get('Subject')}"

            if is_already_processed(message_id):
                continue  # already correctly handled — skip

            sender = decode_mime_words(msg.get("From", "")).lower()
            email_date = sync.parse_email_date(msg)

            if not extract_supplier_name(subject):
                print(f"  ⚠️  UID {uid}: '{subject}' — no supplier name found, skipping")
                continue

            print(f"  🔁 Recovering UID {uid}: '{subject}'")
            # process_outgoing_po upserts processed_emails itself on both the
            # success and park paths — nothing extra needed here.
            process_outgoing_po(client_db, msg, message_id, sender, subject, email_date)
            recovered += 1

        print(f"\nDone. Checked {checked} genuine PURCHASE ORDER email(s), recovered {recovered}.")

    finally:
        imap.logout()


if __name__ == "__main__":
    backfill()

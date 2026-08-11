"""
backfill_warehouse_replies.py — Reprocess historical warehouse reply emails.

Scans Gmail All Mail for emails from the warehouse sender and runs them through
the same process_message() pipeline used by the live listener. Already-processed
emails are skipped via the processed_emails table.

Usage:
    python scripts/backfill_warehouse_replies.py            # live run
    python scripts/backfill_warehouse_replies.py --dry-run  # preview only
"""

import os
import sys
import email as emaillib
from dotenv import load_dotenv
from imapclient import IMAPClient

load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))

from db import get_client
from config import WAREHOUSE_EMAIL as WAREHOUSE_SENDER
import warehouse_reply_parser as parser


DRY_RUN = "--dry-run" in sys.argv


def _connect(host, port, email_addr, app_password):
    imap = IMAPClient(host, port=port, use_uid=True, ssl=True)
    imap.login(email_addr, app_password)
    imap.select_folder("[Gmail]/All Mail")
    return imap


def run() -> None:
    host         = os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com")
    port         = int(os.environ.get("GMAIL_IMAP_PORT", 993))
    email_addr   = os.environ["GMAIL_EMAIL"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]

    print("Connecting to Gmail...")
    imap = _connect(host, port, email_addr, app_password)

    print(f"Searching All Mail for emails from {WAREHOUSE_SENDER}...")
    uids = imap.search(["FROM", WAREHOUSE_SENDER])
    print(f"Found {len(uids)} email(s)\n")

    if not uids:
        print("Nothing to backfill.")
        imap.logout()
        return

    client_db = get_client()
    processed = skipped = errors = 0

    BATCH = 100
    for i in range(0, len(uids), BATCH):
        chunk = uids[i: i + BATCH]

        # Reconnect per batch — long runs drop the Gmail socket after a few minutes.
        try:
            imap.noop()
        except Exception:
            print("  ↩ Reconnecting to Gmail...")
            try:
                imap.logout()
            except Exception:
                pass
            imap = _connect(host, port, email_addr, app_password)

        try:
            messages = imap.fetch(chunk, ["RFC822"])
        except Exception as e:
            print(f"  ❌ Fetch failed for batch {i // BATCH + 1}: {e} — reconnecting and retrying...")
            try:
                imap.logout()
            except Exception:
                pass
            imap = _connect(host, port, email_addr, app_password)
            messages = imap.fetch(chunk, ["RFC822"])

        for uid in chunk:
            if uid not in messages:
                continue
            raw_email = messages[uid][b"RFC822"]
            msg = emaillib.message_from_bytes(raw_email)

            message_id = msg.get("Message-ID", "")
            if not message_id:
                message_id = f"{msg.get('From')}-{msg.get('Date')}-{msg.get('Subject')}"

            subject = parser.decode_mime_words(msg.get("Subject", ""))

            # Already processed — skip
            if parser.is_already_processed(message_id):
                skipped += 1
                continue

            print(f"  [{uid}] {subject[:70]}")

            if DRY_RUN:
                print(f"         (dry-run — would process)")
                processed += 1
                continue

            try:
                parser.process_message(client_db, messages[uid])
                processed += 1
            except Exception as e:
                print(f"         ❌ Error: {e}")
                errors += 1

        print(f"  ...batch {i // BATCH + 1} done ({min(i + BATCH, len(uids))}/{len(uids)})")

    try:
        imap.logout()
    except Exception:
        pass
    action = "would process" if DRY_RUN else "processed"
    print(f"\nDone. {action} {processed}, skipped {skipped} already-done, {errors} error(s).")


if __name__ == "__main__":
    run()

"""
backfill_routing_raw.py — One-shot backfill of warehouse_routing_raw.

For every order that has sent_to_warehouse_at set but warehouse_routing_raw
still null, searches Gmail for the routing email (FROM specialpiping TO
warehouse, bare-PO subject) and copies its plain-text body into the DB.

Safe to re-run: only writes to rows still missing the field.

Usage:
    python scripts/backfill_routing_raw.py            # live run
    python scripts/backfill_routing_raw.py --dry-run  # print what would change
"""

import email
import os
import re
import sys
from datetime import datetime, timezone
from email.header import decode_header
from pathlib import Path

from dotenv import load_dotenv
from imapclient import IMAPClient

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
load_dotenv(ROOT / ".env")

from db import get_client
import sync

DRY_RUN = "--dry-run" in sys.argv

# ── helpers ────────────────────────────────────────────────────────────────

def _decode(s):
    if not s:
        return ""
    parts = decode_header(s)
    out = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            out += part.decode(enc or "utf-8", errors="replace")
        else:
            out += part
    return out


def _body_text(msg):
    """Extract plain-text body from a MIME message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                p = part.get_payload(decode=True)
                if p:
                    return p.decode(errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                p = part.get_payload(decode=True)
                if p:
                    return p.decode(errors="replace")
    else:
        p = msg.get_payload(decode=True)
        if p:
            return p.decode(errors="replace")
    return ""


def _connect_gmail():
    host = os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com")
    port = int(os.environ.get("GMAIL_IMAP_PORT", 993))
    imap = IMAPClient(host, port=port, use_uid=True, ssl=True)
    imap.login(os.environ["GMAIL_EMAIL"], os.environ["GMAIL_APP_PASSWORD"])
    imap.select_folder("[Gmail]/All Mail")
    return imap


# ── main ───────────────────────────────────────────────────────────────────

def run():
    db = get_client()

    # Orders that need backfill
    res = (
        db.table("orders")
        .select("id, buyer_po_number, sent_to_warehouse_at")
        .not_.is_("sent_to_warehouse_at", "null")
        .is_("warehouse_routing_raw", "null")
        .execute()
    )
    orders = res.data or []
    print(f"Orders needing backfill: {len(orders)}")
    if not orders:
        print("Nothing to do.")
        return

    # Build lookup: normalized PO → order id
    po_map = {o["buyer_po_number"]: o["id"] for o in orders}

    imap = _connect_gmail()
    try:
        # Search all routing emails from specialpiping to warehouse
        print("Searching Gmail for routing emails...")
        uids = imap.search([
            "FROM", "specialpiping@gmail.com",
            "TO", "spmwarehouse22@gmail.com",
        ])
        print(f"  Found {len(uids)} total routing emails in Gmail.")

        if not uids:
            print("No routing emails found in Gmail.")
            return

        fixed = 0
        skipped_no_match = 0
        skipped_already_set = 0

        # Fetch envelopes first (cheap) to pre-filter by subject.
        # Then for each downloaded email, also scan PDF attachment filenames
        # and content — same approach as the live listener.
        BATCH = 500
        candidate_uids = []
        for i in range(0, len(uids), BATCH):
            chunk = uids[i:i + BATCH]
            envs = imap.fetch(chunk, ["ENVELOPE"])
            for uid in chunk:
                env_data = envs.get(uid, {}).get(b"ENVELOPE")
                if not env_data:
                    continue
                subj_raw = env_data.subject or b""
                subj = subj_raw.decode("utf-8", errors="ignore") if isinstance(subj_raw, bytes) else str(subj_raw)
                po = sync.is_bare_po_subject(subj)
                if po and po in po_map:
                    candidate_uids.append((uid, po))

        print(f"  {len(candidate_uids)} routing emails match POs needing backfill.")

        # Fetch full RFC822 bodies for candidates
        if not candidate_uids:
            print("No candidates found.")
            return

        uid_list = [u for u, _ in candidate_uids]
        uid_to_po = {u: po for u, po in candidate_uids}

        for i in range(0, len(uid_list), 50):
            chunk = uid_list[i:i + 50]
            msgs = imap.fetch(chunk, ["RFC822"])
            for uid in chunk:
                subject_po = uid_to_po[uid]

                msg_data = msgs.get(uid)
                if not msg_data:
                    continue

                msg = email.message_from_bytes(msg_data[b"RFC822"])

                # Merge POs from subject and from PDF attachment (filename + content).
                attachment_pos = sync.chevron_pos_from_attachment(msg)
                all_pos = list(dict.fromkeys([subject_po] + attachment_pos))

                # Try each PO in order; use the first one that has an order row.
                po = next((p for p in all_pos if p in po_map), None)
                if not po:
                    skipped_no_match += 1
                    continue

                order_id = po_map.get(po)
                if not order_id:
                    skipped_no_match += 1
                    continue

                body = _body_text(msg).strip()
                if not body:
                    print(f"  PO {po}: body empty — skipping")
                    continue

                preview = body.replace("\n", " ")[:80]
                print(f"  PO {po}: {preview}…")

                if DRY_RUN:
                    fixed += 1
                    continue

                result = (
                    db.table("orders")
                    .update({"warehouse_routing_raw": body})
                    .eq("id", order_id)
                    .is_("warehouse_routing_raw", "null")
                    .execute()
                )
                if result.data:
                    fixed += 1
                else:
                    skipped_already_set += 1

    finally:
        imap.logout()

    action = "[dry-run] would update" if DRY_RUN else "updated"
    print(f"\nDone. {action} {fixed} orders.")
    if skipped_no_match:
        print(f"  {skipped_no_match} emails had no matching order (PO not in backfill set).")
    if skipped_already_set:
        print(f"  {skipped_already_set} already had routing_raw set (concurrent write).")


if __name__ == "__main__":
    run()

"""
backfill_self_addressed_routing.py - create orders from routing emails SPM
sent to themselves.

is_warehouse_routing_email() used to require the warehouse address on To or
Cc. SPM sometimes sends the same mail to their own address instead - body
addressed "ATTN Warehouse/Mr. Ned", PO PDF attached, warehouse nowhere in the
headers - and those were rejected outright.

Normally that costs nothing, because Chevron's own notification creates the
order anyway. It only bites when Chevron never notified: PO 0061396997 sat in
Gmail from 2 April 2026 with its PDF attached and never became an order at
all, while two sibling POs on the same SPM order came through normally.

The gate now accepts a self-addressed mail when it carries the PDF. This
script applies that to the ones already sitting in the mailbox, whose cursor
has long since moved past them.

    python scripts/backfill_self_addressed_routing.py             # dry run
    python scripts/backfill_self_addressed_routing.py --apply
    python scripts/backfill_self_addressed_routing.py --apply --since=2025-01-01
"""

import os
import re
import sys
import email as email_mod
from pathlib import Path
from datetime import datetime, date

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

load_dotenv()

import sync
from db import get_client
from email_utils import decode_mime_words
from config import SPM_SENDER, WAREHOUSE_EMAIL
from gmail_ack_listener import (
    connect_to_gmail, is_warehouse_routing_email, get_recipients,
    _has_pdf_attachment, process_message,
)


def find_candidates(since: str):
    """Self-addressed routing emails carrying a PO PDF."""
    imap = connect_to_gmail()
    out = []
    try:
        imap.select_folder("[Gmail]/All Mail", readonly=True)
        uids = imap.search(["FROM", SPM_SENDER,
                            "SINCE", datetime.strptime(since, "%Y-%m-%d").date()])
        print(f"  {len(uids)} email(s) from SPM since {since}")

        # Screen on the envelope first so only real candidates are downloaded.
        cand = []
        for i in range(0, len(uids), 300):
            chunk = uids[i:i + 300]
            envs = imap.fetch(chunk, ["ENVELOPE"])
            for uid in chunk:
                env = envs.get(uid, {}).get(b"ENVELOPE")
                if not env or not env.subject:
                    continue
                subj = re.sub(r"\s+", " ",
                              decode_mime_words(env.subject.decode("utf-8", "replace"))).strip()
                if sync.is_bare_po_subject(subj) or sync.is_nlng_po_subject(subj):
                    cand.append((uid, subj))
        print(f"  {len(cand)} with a PO-number subject")

        for uid, subj in cand:
            data = imap.fetch([uid], ["RFC822", "INTERNALDATE"]).get(uid)
            if not data:
                continue
            msg = email_mod.message_from_bytes(data[b"RFC822"])
            recips = get_recipients(msg)
            if any(WAREHOUSE_EMAIL.lower() in r for r in recips):
                continue          # already handled by the normal path
            if not _has_pdf_attachment(msg):
                continue
            out.append({
                "uid": uid, "subject": subj, "msg_data": data,
                "po": sync.is_bare_po_subject(subj) or sync.is_nlng_po_subject(subj),
                "date": data[b"INTERNALDATE"],
                "to": decode_mime_words(msg.get("To", "") or "")[:60],
            })
    finally:
        imap.logout()
    return out


def main() -> None:
    apply = "--apply" in sys.argv
    since = "2026-01-01"
    for arg in sys.argv[1:]:
        if arg.startswith("--since="):
            since = arg.split("=", 1)[1]

    print(f"Self-addressed routing backfill - {'APPLYING' if apply else 'DRY RUN'}, since {since}")
    cands = find_candidates(since)
    print(f"  {len(cands)} self-addressed routing email(s) with a PDF\n")
    if not cands:
        return

    client = get_client()
    pos = sorted({c["po"] for c in cands if c["po"]})
    have = set()
    for i in range(0, len(pos), 50):
        have |= {r["buyer_po_number"] for r in
                 (client.table("orders").select("buyer_po_number")
                  .in_("buyer_po_number", pos[i:i + 50]).execute().data or [])}
    todo = [c for c in cands if c["po"] and c["po"] not in have]
    print(f"  {len(pos)} distinct PO(s); {len(have & set(pos))} already in orders, "
          f"{len(todo)} missing\n")

    for c in todo:
        print(f"   {c['po']}  {c['date']:%Y-%m-%d}  to: {c['to']}")
    if not todo:
        print("   nothing to create")
        return

    if not apply:
        print("\n(dry run - add --apply to create these orders)")
        return

    made = 0
    for c in todo:
        try:
            # Same handler the live listener uses, so a backfilled order is
            # built exactly like one created in the normal flow.
            process_message(client, c["msg_data"])
            if client.table("orders").select("id").eq("buyer_po_number", c["po"]).execute().data:
                made += 1
                print(f"   created {c['po']}")
            else:
                print(f"   {c['po']}: handler ran but no order row appeared")
        except Exception as exc:
            print(f"   {c['po']}: FAILED - {str(exc)[:70]}")
    print(f"\ncreated {made} order(s)")


if __name__ == "__main__":
    main()

"""
backfill_nlng_routing.py — create NLNG orders that only ever appeared as a
warehouse-routing email.

An NLNG order is normally created from NLNG's own PO email, which arrives
from enquiry@specialpipingltd.com. When that email does not arrive, the PO
still reaches us: SPM forwards it to the warehouse ("NEW PO 4200094401")
with the same PDF attached, asking them to confirm stock. Until now the
routing email was parked waiting for a PO email that never came, so no
order was ever created - 4200094401 sat in Gmail from 2026-09-08 with
nothing in nlng_orders to show for it.

gmail_ack_listener now creates the order from the routing email itself.
This script applies that same logic to routing emails already sitting in
the mailbox, whose cursor has long since moved past them.

Run from the project root:
    python scripts/backfill_nlng_routing.py              # dry run
    python scripts/backfill_nlng_routing.py --apply
    python scripts/backfill_nlng_routing.py --apply --since=2025-01-01

Note on the default window: it matches BACKFILL_SINCE (2026-01-01), which is
when NLNG tracking started. Routing emails older than that are real, but
their orders were never meant to be in the system - scanning further back
would manufacture ~95 orders from 2022-2025 that nobody is tracking.
"""

import sys
import email as email_mod
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

load_dotenv()

import sync
from db import get_client
from email_utils import decode_mime_words
from config import SPM_SENDER, WAREHOUSE_EMAIL
from gmail_ack_listener import (
    connect_to_gmail, process_nlng_po_email, _extract_nlng_attachment,
)


def backfill(dry_run: bool = True, since: str | None = None) -> None:
    since_date = (datetime.strptime(since, "%Y-%m-%d").date()
                  if since else sync._backfill_since())
    client_db = get_client()

    have = {r["po_number"] for r in
            (client_db.table("nlng_orders").select("po_number").execute().data or [])}

    imap = connect_to_gmail()
    created = skipped = failed = 0
    try:
        imap.select_folder("[Gmail]/All Mail", readonly=True)
        uids = imap.search([
            "FROM", SPM_SENDER, "TO", WAREHOUSE_EMAIL,
            "SINCE", since_date,
        ])
        print(f"  {len(uids)} SPM->warehouse email(s) since {since_date}")

        # Envelope-first so only genuine candidates get a full download.
        candidates: list[tuple[int, str, str]] = []
        BATCH = 200
        for i in range(0, len(uids), BATCH):
            chunk = uids[i:i + BATCH]
            envs = imap.fetch(chunk, ["ENVELOPE"])
            for uid in chunk:
                env = envs.get(uid, {}).get(b"ENVELOPE")
                if not env or not env.subject:
                    continue
                subj = decode_mime_words(env.subject.decode("utf-8", "replace"))
                po = sync.is_nlng_po_subject(subj)
                if po:
                    candidates.append((uid, po, subj))

        print(f"  {len(candidates)} of them are NLNG PO routing emails")
        todo = [(u, po, s) for u, po, s in candidates if po not in have]
        print(f"  {len(todo)} reference a PO with no order row\n")

        seen: set[str] = set()
        for uid, po, subj in sorted(todo, key=lambda x: x[1]):
            if po in seen:
                continue
            seen.add(po)
            raw = imap.fetch([uid], ["RFC822"]).get(uid)
            if not raw:
                print(f"   {po}: could not fetch message")
                failed += 1
                continue
            msg = email_mod.message_from_bytes(raw[b"RFC822"])
            if not _extract_nlng_attachment(msg):
                print(f"   {po}: no PDF attachment on the routing email - skipped")
                skipped += 1
                continue

            if dry_run:
                print(f"   {po}: would create from {subj[:48]!r}")
                created += 1
                continue

            msg_id = msg.get("Message-ID", "") or \
                f"{msg.get('From')}-{msg.get('Date')}-{msg.get('Subject')}"
            try:
                process_nlng_po_email(
                    client_db, msg, msg_id,
                    decode_mime_words(msg.get("From", "")), subj,
                )
                order = sync.find_nlng_order_by_po(po)
                if order:
                    sync.stamp_nlng_sent_to_warehouse(
                        order["id"], sync.parse_email_date(msg),
                        (msg.get_payload() if isinstance(msg.get_payload(), str) else None),
                    )
                    print(f"   {po}: created + stamped sent_to_warehouse")
                    created += 1
                else:
                    print(f"   {po}: parsed but no order row appeared")
                    failed += 1
            except Exception as exc:
                print(f"   {po}: FAILED - {exc}")
                failed += 1
    finally:
        imap.logout()

    verb = "would create" if dry_run else "created"
    print(f"\n{verb}: {created}   skipped (no PDF): {skipped}   failed: {failed}")


if __name__ == "__main__":
    dry = "--apply" not in sys.argv
    since_arg = None
    for a in sys.argv[1:]:
        if a.startswith("--since="):
            since_arg = a.split("=", 1)[1]
    print(f"NLNG routing backfill - {'DRY RUN' if dry else 'APPLYING'}")
    backfill(dry_run=dry, since=since_arg)

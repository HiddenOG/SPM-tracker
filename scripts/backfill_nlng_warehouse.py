"""
backfill_nlng_warehouse.py — One-shot backfill for NLNG warehouse timeline fields.

For each NLNG order that is missing sent_to_warehouse_at or stock_check_completed_at,
searches Gmail for the matching emails and stamps the fields.

Two passes:
  Pass 1 — Routing emails (FROM specialpiping@gmail.com TO spmwarehouse22@gmail.com)
            Subject: "PO No. 4200XXXXXXX"
            → stamps sent_to_warehouse_at + warehouse_routing_raw on nlng_orders

  Pass 2 — Warehouse reply emails (FROM spmwarehouse22@gmail.com)
            Subject: "PO No. 4200XXXXXXX" (reply thread keeps the subject)
            → stamps stock_check_completed_at + stock_check_raw on nlng_orders

This script does NOT touch the orders table (Chevron) at all.
Safe to re-run: only writes to rows still missing the fields.

Usage:
    python scripts/backfill_nlng_warehouse.py            # live run
    python scripts/backfill_nlng_warehouse.py --dry-run  # print what would change
"""

import email
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from imapclient import IMAPClient
from email.header import decode_header

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
load_dotenv(ROOT / ".env")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from db import get_client
import sync
from config import SPM_SENDER, WAREHOUSE_EMAIL as WAREHOUSE

DRY_RUN = "--dry-run" in sys.argv
FETCH_BATCH   = 50


# ── helpers ──────────────────────────────────────────────────────────────────

def _decode_header(s: str) -> str:
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


def _body_text(msg: email.message.Message) -> str:
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


def _connect_gmail() -> IMAPClient:
    host = os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com")
    port = int(os.environ.get("GMAIL_IMAP_PORT", 993))
    imap = IMAPClient(host, port=port, use_uid=True, ssl=True)
    imap.login(os.environ["GMAIL_EMAIL"], os.environ["GMAIL_APP_PASSWORD"])
    return imap


def _fetch_nlng_orders_needing_backfill(db) -> tuple[dict, dict, dict]:
    """
    Returns three dicts keyed by po_number → {id, ...}:
      needs_routing   — orders missing sent_to_warehouse_at
      needs_reply     — orders missing stock_check_completed_at
      needs_delivered — orders with stock_check_completed_at but missing delivered_at
    """
    res = db.table("nlng_orders").select(
        "id, po_number, sent_to_warehouse_at, stock_check_completed_at, delivered_at"
    ).execute()

    needs_routing:   dict[str, dict] = {}
    needs_reply:     dict[str, dict] = {}
    needs_delivered: dict[str, dict] = {}

    for row in (res.data or []):
        po = row["po_number"]
        if not row.get("sent_to_warehouse_at"):
            needs_routing[po] = row
        if not row.get("stock_check_completed_at"):
            needs_reply[po] = row
        elif not row.get("delivered_at"):
            # Has a stock check but not yet marked delivered — may be "completely delivered"
            needs_delivered[po] = row

    return needs_routing, needs_reply, needs_delivered


# ── pass 1: routing emails (SPM → warehouse) ─────────────────────────────────

def backfill_routing(db, imap: IMAPClient, needs_routing: dict[str, dict]) -> int:
    if not needs_routing:
        print("Pass 1: all NLNG orders already have sent_to_warehouse_at — skipping.")
        return 0

    print(f"Pass 1: {len(needs_routing)} NLNG order(s) need sent_to_warehouse_at.")
    imap.select_folder("[Gmail]/All Mail")

    uids = imap.search(["FROM", SPM_SENDER, "TO", WAREHOUSE])
    print(f"  Found {len(uids)} total SPM→warehouse emails in Gmail.")

    fixed = 0
    for i in range(0, len(uids), FETCH_BATCH):
        chunk = uids[i:i + FETCH_BATCH]
        envs  = imap.fetch(chunk, ["ENVELOPE"])

        # Pre-filter by subject
        candidates = []
        for uid in chunk:
            env = envs.get(uid, {}).get(b"ENVELOPE")
            if not env or not env.subject:
                continue
            subj_raw = env.subject
            subj = subj_raw.decode("utf-8", errors="ignore") if isinstance(subj_raw, bytes) else str(subj_raw)
            po = sync.is_nlng_po_subject(subj)
            if po and po in needs_routing:
                candidates.append((uid, po))

        if not candidates:
            continue

        msgs = imap.fetch([u for u, _ in candidates], ["RFC822"])
        for uid, po in candidates:
            raw = msgs.get(uid)
            if not raw:
                continue
            msg = email.message_from_bytes(raw[b"RFC822"])
            body = _body_text(msg).strip()
            date = sync.parse_email_date(msg)
            subj = _decode_header(msg.get("Subject", ""))

            preview = body.replace("\n", " ")[:80]
            print(f"  PO {po} | routing | {date[:10]} | {preview}…")

            if not DRY_RUN:
                order_id = needs_routing[po]["id"]
                sync.stamp_nlng_sent_to_warehouse(order_id, date, body)

            fixed += 1
            # Use most recent email if multiple exist for same PO
            # (stamp_nlng_sent_to_warehouse only writes if the field is null
            # on first call; subsequent calls to the same PO are no-ops unless
            # we explicitly allow override — here we take the first match)
            needs_routing.pop(po, None)  # remove so duplicates are skipped

    return fixed


# ── pass 2: warehouse reply emails (warehouse → SPM inbox) ───────────────────

def backfill_replies(db, imap: IMAPClient, needs_reply: dict[str, dict]) -> int:
    if not needs_reply:
        print("Pass 2: all NLNG orders already have stock_check_completed_at — skipping.")
        return 0

    print(f"Pass 2: {len(needs_reply)} NLNG order(s) need stock_check_completed_at.")
    imap.select_folder("INBOX")

    uids = imap.search(["FROM", WAREHOUSE])
    print(f"  Found {len(uids)} total warehouse→SPM emails in Gmail INBOX.")

    fixed = 0
    for i in range(0, len(uids), FETCH_BATCH):
        chunk = uids[i:i + FETCH_BATCH]
        envs  = imap.fetch(chunk, ["ENVELOPE"])

        candidates = []
        for uid in chunk:
            env = envs.get(uid, {}).get(b"ENVELOPE")
            if not env or not env.subject:
                continue
            subj_raw = env.subject
            subj = subj_raw.decode("utf-8", errors="ignore") if isinstance(subj_raw, bytes) else str(subj_raw)
            po = sync.is_nlng_po_subject(subj)
            if po and po in needs_reply:
                candidates.append((uid, po))

        if not candidates:
            continue

        msgs = imap.fetch([u for u, _ in candidates], ["RFC822"])
        for uid, po in candidates:
            raw = msgs.get(uid)
            if not raw:
                continue
            msg = email.message_from_bytes(raw[b"RFC822"])
            body = _body_text(msg).strip()
            date = sync.parse_email_date(msg)

            preview = body.replace("\n", " ")[:80]
            print(f"  PO {po} | reply   | {date[:10]} | {preview}…")

            if not DRY_RUN:
                order_id = needs_reply[po]["id"]
                sync.stamp_nlng_stock_check(order_id, date, body)

            fixed += 1
            needs_reply.pop(po, None)

    return fixed


# ── pass 3: delivered_at from "Completely delivered" warehouse emails ─────────

def backfill_delivered(db, imap: IMAPClient, needs_delivered: dict[str, dict]) -> int:
    if not needs_delivered:
        print("Pass 3: all NLNG orders with stock checks already have delivered_at — skipping.")
        return 0

    # Import keyword interpreter without pulling in Claude (we only use keyword matching)
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from warehouse_reply_parser import interpret_reply_simple

    print(f"Pass 3: {len(needs_delivered)} NLNG order(s) missing delivered_at.")
    imap.select_folder("INBOX")

    uids = imap.search(["FROM", WAREHOUSE])
    print(f"  Found {len(uids)} warehouse→SPM emails in Gmail INBOX.")

    fixed = 0
    for i in range(0, len(uids), FETCH_BATCH):
        chunk = uids[i:i + FETCH_BATCH]
        envs  = imap.fetch(chunk, ["ENVELOPE"])

        candidates = []
        for uid in chunk:
            env = envs.get(uid, {}).get(b"ENVELOPE")
            if not env or not env.subject:
                continue
            subj_raw = env.subject
            subj = subj_raw.decode("utf-8", errors="ignore") if isinstance(subj_raw, bytes) else str(subj_raw)
            po = sync.is_nlng_po_subject(subj)
            if po and po in needs_delivered:
                candidates.append((uid, po))

        if not candidates:
            continue

        msgs = imap.fetch([u for u, _ in candidates], ["RFC822"])
        for uid, po in candidates:
            raw = msgs.get(uid)
            if not raw:
                continue
            msg = email.message_from_bytes(raw[b"RFC822"])
            body = _body_text(msg).strip()
            result = interpret_reply_simple(body)
            if not result or result.get("overall_availability") != "fully_delivered":
                continue

            date = sync.parse_email_date(msg)
            print(f"  PO {po} | delivered | {date[:10]} | {body[:60]}…")

            if not DRY_RUN:
                order_id = needs_delivered[po]["id"]
                sync.stamp_nlng_delivered(order_id, date)

            fixed += 1
            needs_delivered.pop(po, None)

    return fixed


# ── main ─────────────────────────────────────────────────────────────────────

def run():
    db = get_client()
    needs_routing, needs_reply, needs_delivered = _fetch_nlng_orders_needing_backfill(db)

    if not needs_routing and not needs_reply and not needs_delivered:
        print("Nothing to backfill — all NLNG orders already have warehouse timestamps.")
        return

    imap = _connect_gmail()
    try:
        r1 = backfill_routing(db, imap, needs_routing)
        r2 = backfill_replies(db, imap, needs_reply)
        r3 = backfill_delivered(db, imap, needs_delivered)
    finally:
        imap.logout()

    action = "[dry-run] would update" if DRY_RUN else "updated"
    print(f"\nDone. {action} {r1} routing, {r2} stock check, {r3} delivered_at.")


if __name__ == "__main__":
    run()

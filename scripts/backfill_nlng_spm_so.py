"""
backfill_nlng_spm_so.py — Backfill spm_po_number, spm_po_sent_at, so_number,
so_received_at, and so_sent_to_warehouse_at for existing NLNG orders.

Three passes:
  Pass 1 — SPM PO sent to Flexitallic
            Gmail All Mail FROM specialpiping SUBJECT FLEXITALLIC
            Subject: "PURCHASE ORDER-S.P.M. - 3071.-NLNG-4200083212- FLEXITALLIC"
            → stamps spm_po_number + spm_po_sent_at  (latest wins)

  Pass 2 — Flexitallic SO received
            Gmail INBOX FROM salesorder@flexitallic.eu
            Subject: "Flexitallic Sales Acknowledgement for SO714770…"
            → stamps so_number + so_received_at  (latest wins)

  Pass 3 — SO forwarded to warehouse
            Gmail All Mail FROM specialpiping TO spmwarehouse22
            Subject: "Fwd: Flexitallic Sales Acknowledgement for SO714770…"
            → stamps so_sent_to_warehouse_at  (latest wins)

Does NOT touch the orders table (Chevron) or any sync_state cursors.
Safe to re-run; the date-guard in each stamp function prevents regressions.

Usage:
    python scripts/backfill_nlng_spm_so.py            # live run
    python scripts/backfill_nlng_spm_so.py --dry-run  # print what would change
"""

import email
import os
import re
import sys
from pathlib import Path
from email.header import decode_header as _dh

from dotenv import load_dotenv
from imapclient import IMAPClient

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
from config import SPM_SENDER, WAREHOUSE_EMAIL as WAREHOUSE, FLEXITALLIC_SENDER as FLEX_SENDER

DRY_RUN = "--dry-run" in sys.argv
FETCH_BATCH      = 50


def _decode(s) -> str:
    if not s:
        return ""
    if isinstance(s, bytes):
        s = s.decode("utf-8", errors="ignore")
    parts = _dh(s)
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
    p = msg.get_payload(decode=True)
    return p.decode(errors="replace") if p else ""



def _connect_gmail() -> IMAPClient:
    host = os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com")
    port = int(os.environ.get("GMAIL_IMAP_PORT", 993))
    imap = IMAPClient(host, port=port, use_uid=True, ssl=True)
    imap.login(os.environ["GMAIL_EMAIL"], os.environ["GMAIL_APP_PASSWORD"])
    return imap


def _spm_po_from_body(body: str) -> str | None:
    """Extract 'S.P.M.-3071' from Flexitallic email body text."""
    m = re.search(r"S\.P\.M\.?\s*[-–.]\s*([\d.]+)", body, re.IGNORECASE)
    if not m:
        return None
    return f"S.P.M.-{m.group(1).rstrip('.')}"


# ── pass 1: SPM PO → Flexitallic ─────────────────────────────────────────────

def backfill_spm_po(db, imap: IMAPClient) -> int:
    print("Pass 1: SPM PO sends to Flexitallic → spm_po_number / spm_po_sent_at")
    imap.select_folder("[Gmail]/All Mail")
    uids = imap.search(["FROM", SPM_SENDER, "SUBJECT", "FLEXITALLIC"])
    print(f"  Found {len(uids)} SPM emails with 'FLEXITALLIC' in subject.")
    fixed = 0
    # Process oldest-first so the date-guard naturally keeps the latest
    for i in range(0, len(uids), FETCH_BATCH):
        chunk = uids[i:i + FETCH_BATCH]
        envs = imap.fetch(chunk, ["ENVELOPE"])
        candidates = []
        for uid in chunk:
            env = envs.get(uid, {}).get(b"ENVELOPE")
            if not env or not env.subject:
                continue
            subj = _decode(env.subject)
            for pair in sync.is_nlng_spm_po_subject(subj):
                candidates.append((uid, pair))
        if not candidates:
            continue
        msgs = imap.fetch([u for u, _ in candidates], ["RFC822"])
        for uid, (spm_po, nlng_po) in candidates:
            raw = msgs.get(uid)
            if not raw:
                continue
            msg = email.message_from_bytes(raw[b"RFC822"])
            date = sync.parse_email_date(msg)
            subj = _decode(msg.get("Subject", ""))
            # PDF attachment filename overrides the SPM PO number when the email
            # is a reply (subject stays the same, attached PDF is revised).
            attachment_spm_po = sync.spm_po_from_attachment(msg)
            if attachment_spm_po and attachment_spm_po != spm_po:
                print(f"  ⚠️  subject has {spm_po} but PDF says {attachment_spm_po} — using PDF")
                spm_po = attachment_spm_po
            # Merge NLNG POs from subject and from attachment filename — one email
            # can reference additional orders only visible in the attached PDF name.
            attachment_nlng_pos = sync.nlng_pos_from_attachment(msg)
            all_nlng_pos = list(dict.fromkeys([nlng_po] + attachment_nlng_pos))
            for po in all_nlng_pos:
                order = sync.find_nlng_order_by_po(po)
                if not order:
                    print(f"  SKIP — NLNG order {po} not in DB")
                    continue
                print(f"  NLNG {po} | spm_po={spm_po} | {date[:10]} | {subj[:60]}")
                if not DRY_RUN:
                    sync.stamp_nlng_spm_po(order["id"], spm_po, date)
                fixed += 1
    return fixed


# ── pass 2: Flexitallic SO received ──────────────────────────────────────────

def backfill_so(db, imap: IMAPClient) -> int:
    """
    Match Flexitallic SO inbox emails to NLNG orders by SPM PO number.

    One Flexitallic email (one SO) can cover multiple NLNG orders that share
    the same SPM PO. For each Flexitallic email, extract the SPM PO from the
    subject, find every NLNG order with that SPM PO, and stamp them all.
    """
    print("Pass 2: Flexitallic SO received_at backfill")

    res = db.table("nlng_orders").select(
        "id, po_number, spm_po_number"
    ).not_.is_("spm_po_number", "null").is_("so_received_at", "null").execute()

    spm_targets: dict[str, list] = {}
    for row in (res.data or []):
        spm_targets.setdefault(row["spm_po_number"], []).append(row)

    if not spm_targets:
        print("  All NLNG orders with spm_po already have so_received_at — skipping.")
        return 0

    total = sum(len(v) for v in spm_targets.values())
    print(f"  {total} order(s) across {len(spm_targets)} SPM PO(s) need so_received_at")

    imap.select_folder("INBOX")
    uids = imap.search(["FROM", FLEX_SENDER])
    print(f"  Found {len(uids)} Flexitallic emails in INBOX.")

    fixed = 0
    for i in range(0, len(uids), FETCH_BATCH):
        if not spm_targets:
            break
        chunk = uids[i:i + FETCH_BATCH]
        envs = imap.fetch(chunk, ["ENVELOPE"])
        candidates = []
        for uid in chunk:
            env = envs.get(uid, {}).get(b"ENVELOPE")
            if not env or not env.subject:
                continue
            subj = _decode(env.subject)
            parsed = sync.is_flex_so_subject(subj)
            if not parsed:
                continue
            so_num, spm_po = parsed
            if spm_po and spm_po in spm_targets:
                candidates.append((uid, so_num, spm_po))
        if not candidates:
            continue
        try:
            date_data = imap.fetch([u for u, _, __ in candidates], ["INTERNALDATE"])
        except Exception as e:
            print(f"  ⚠️  Fetch error chunk {i}: {e}")
            continue
        for uid, so_number, spm_po in candidates:
            rows = spm_targets.pop(spm_po, [])
            if not rows:
                continue
            int_date = date_data.get(uid, {}).get(b"INTERNALDATE")
            date = int_date.isoformat() if (int_date and hasattr(int_date, "isoformat")) else str(int_date or "")
            for row in rows:
                print(f"  NLNG {row['po_number']} | so={so_number} received | {date[:10]}")
                if not DRY_RUN:
                    # Look up earliest despatch_date from already-parsed so_line_items
                    li_res = db.table("so_line_items").select("despatch_date").eq(
                        "so_number", so_number
                    ).not_.is_("despatch_date", "null").order("despatch_date").limit(1).execute()
                    promised = li_res.data[0]["despatch_date"] if li_res.data else None
                    sync.stamp_nlng_so(row["id"], so_number, date, promised_date=promised)
                fixed += 1
    return fixed


# ── pass 3: SO forwarded to warehouse ────────────────────────────────────────

def backfill_so_to_warehouse(db, imap: IMAPClient) -> int:
    print("Pass 3: SO forwarded to warehouse → so_sent_to_warehouse_at")
    imap.select_folder("[Gmail]/All Mail")
    uids = imap.search(["FROM", SPM_SENDER, "TO", WAREHOUSE])
    print(f"  Found {len(uids)} SPM→warehouse emails in All Mail.")
    fixed = 0
    for i in range(0, len(uids), FETCH_BATCH):
        chunk = uids[i:i + FETCH_BATCH]
        envs = imap.fetch(chunk, ["ENVELOPE"])
        candidates = []
        for uid in chunk:
            env = envs.get(uid, {}).get(b"ENVELOPE")
            if not env or not env.subject:
                continue
            subj = _decode(env.subject)
            parsed = sync.is_flex_so_subject(subj)
            if parsed:
                candidates.append((uid, parsed))
        if not candidates:
            continue
        msgs = imap.fetch([u for u, _ in candidates], ["RFC822"])
        for uid, (so_number, spm_po) in candidates:
            raw = msgs.get(uid)
            if not raw:
                continue
            msg = email.message_from_bytes(raw[b"RFC822"])
            date = sync.parse_email_date(msg)
            subj = _decode(msg.get("Subject", ""))
            orders = sync.find_all_nlng_orders_by_spm_po(spm_po) if spm_po else []
            if not orders:
                print(f"  SKIP — no NLNG order for spm_po={spm_po} (SO {so_number})")
                continue
            for order in orders:
                print(f"  NLNG {order['po_number']} | so={so_number} forwarded to warehouse | {date[:10]}")
                if not DRY_RUN:
                    sync.stamp_nlng_so_to_warehouse(order["id"], date, so_number=so_number)
                fixed += 1
    return fixed


# ── main ─────────────────────────────────────────────────────────────────────

def _run_pass(fn, db, *args):
    """Run one backfill pass with its own fresh IMAP connection."""
    imap = _connect_gmail()
    try:
        return fn(db, imap, *args)
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def run():
    db = get_client()
    # Pass 3 runs before Pass 2: Pass 3 writes so_number to the DB, and
    # Pass 2 reads it to match Flexitallic inbox emails by SO number.
    r1 = _run_pass(backfill_spm_po, db)
    r3 = _run_pass(backfill_so_to_warehouse, db)
    r2 = _run_pass(backfill_so, db)

    action = "[dry-run] would update" if DRY_RUN else "updated"
    print(
        f"\nDone. {action}:"
        f"  {r1} spm_po_sent_at,"
        f"  {r3} so_sent_to_warehouse_at,"
        f"  {r2} so_received_at"
    )


if __name__ == "__main__":
    run()

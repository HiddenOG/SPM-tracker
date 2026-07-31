"""
smart_gap_filler.py — One-IMAP-search-per-PO/SO pipeline gap filler.

Algorithm (no Claude API, no per-column IMAP loops):
  1. Load every order that has at least one null pipeline timestamp.
  2. Search Gmail ONCE per unique buyer_po_number (BODY search).
     Search Gmail ONCE per unique so_number (BODY, then SUBJECT fallback).
  3. Download the full RFC822 body for every matched email (up to 40 per PO).
  4. Classify each email by sender + recipients + keywords → maps to columns.
  5. For each order, walk its emails oldest-first; first match per field wins.
  6. Write only null fields to the DB and advance overall_status.

Run:
  python scripts/smart_gap_filler.py                  # all orders
  python scripts/smart_gap_filler.py --po 0061448980  # one PO
  python scripts/smart_gap_filler.py --limit 20       # first N POs
  python scripts/smart_gap_filler.py --dry-run        # show what would change
"""

import os
import re
import sys
import time
import email as email_lib
import argparse
from email.header import decode_header as _dh
from email.utils import parsedate_to_datetime
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from db import get_client
from imapclient import IMAPClient
import sync
from config import SPM_SENDER

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

from config import WAREHOUSE_EMAIL as WAREHOUSE, FLEXITALLIC_SENDER

# Pipeline timestamp fields on the orders table, in pipeline order.
# Value = status string to advance to after filling (None = no status advance).
FIELD_STATUS: dict[str, str | None] = {
    "sent_to_warehouse_at":           "awaiting_warehouse_stock_check",
    "stock_check_completed_at":       "stock_check_complete",
    "spm_po_sent_at":                 "po_sent",
    "spm_po_number":                  None,   # string, not a timestamp
    "so_received_at":                 "supplier_acknowledged",
    "so_number":                      None,   # string, not a timestamp
    "flex_dispatch_ready_at":         "dispatch_packed_awaiting_instruction",
    "dispatch_instructions_sent_at":  "dispatch_instruction_sent",
    "ready_for_dispatch_at":          "ready_for_dispatch",
    "dispatched_at":                  "dispatched",
    "so_sent_to_warehouse_at":        "so_sent_to_warehouse",
    "delivery_requested_at":          "delivery_requested",
    "delivered_at":                   "delivered",
}

# These fields live on spm_purchase_orders too — stamp both tables.
SPM_TABLE_FIELDS = {
    "flex_dispatch_ready_at",
    "dispatch_instructions_sent_at",
    "ready_for_dispatch_at",
    "dispatched_at",
}

SO_RE = re.compile(r"\b(SO\d{5,})\b", re.IGNORECASE)
SPM_PO_RE = re.compile(r"\(([^)]*S\.?\s*P\.?\s*M[^)]+)\)", re.IGNORECASE)


# ─────────────────────────────────────────────
# IMAP helpers
# ─────────────────────────────────────────────

class _IMAP:
    """Thin reconnecting wrapper around IMAPClient."""

    def __init__(self):
        self._c = self._connect()

    def _connect(self):
        c = IMAPClient("imap.gmail.com", ssl=True, timeout=20)
        c.login(os.environ["GMAIL_EMAIL"], os.environ["GMAIL_APP_PASSWORD"])
        c.select_folder("[Gmail]/All Mail", readonly=True)
        return c

    def _retry(self, fn):
        try:
            return fn()
        except Exception as e:
            if any(k in str(e).lower() for k in ("eof", "bye", "reset", "socket", "timed out")):
                print("    [reconnecting…]")
                try: self._c.logout()
                except Exception: pass
                self._c = self._connect()
                return fn()
            raise

    def search(self, criteria):
        return self._retry(lambda: self._c.search(criteria))

    def fetch(self, uids, items):
        return self._retry(lambda: self._c.fetch(uids, items))

    def logout(self):
        try: self._c.logout()
        except Exception: pass


def _decode(s) -> str:
    if not s:
        return ""
    out = ""
    for part, enc in _dh(s):
        if isinstance(part, bytes):
            out += part.decode(enc or "utf-8", errors="replace")
        else:
            out += part
    return out.strip()


def _body_text(msg) -> str:
    """Extract all readable text from a MIME message."""
    parts = msg.walk() if msg.is_multipart() else [msg]
    text = ""
    for part in parts:
        ct = part.get_content_type()
        if ct in ("text/plain", "text/html"):
            raw = part.get_payload(decode=True)
            if raw:
                text += raw.decode(errors="replace") + "\n"
    return text


def _parse_email(raw: bytes, internal_date) -> dict | None:
    """Parse RFC822 bytes into a flat email dict."""
    try:
        msg = email_lib.message_from_bytes(raw)
    except Exception:
        return None

    subject = _decode(msg.get("Subject", ""))
    sender  = _decode(msg.get("From", "")).lower()
    to      = _decode(msg.get("To", "")).lower()
    cc      = _decode(msg.get("Cc", "")).lower()
    mid     = msg.get("Message-ID", "").strip()
    body    = _body_text(msg)

    date_iso = None
    if internal_date:
        try:
            date_iso = internal_date.isoformat()
        except Exception:
            pass
    if not date_iso:
        try:
            date_iso = parsedate_to_datetime(msg.get("Date", "")).isoformat()
        except Exception:
            pass

    return {
        "message_id": mid,
        "from": sender,
        "to_cc": (to + " " + cc).strip(),
        "subject": subject,
        "body": body,
        "date_iso": date_iso or "",
    }


def fetch_emails_for_term(imap: _IMAP, term: str, since, max_emails: int = 40) -> list[dict]:
    """
    Search Gmail for all emails whose BODY contains `term`.
    Falls back to SUBJECT search if BODY finds nothing.
    Returns list of parsed email dicts, sorted oldest-first.
    """
    uids = []
    try:
        uids = imap.search(["BODY", term, "SINCE", since])
        time.sleep(0.2)
    except Exception:
        pass

    if not uids:
        try:
            uids = imap.search(["SUBJECT", term, "SINCE", since])
            time.sleep(0.2)
        except Exception:
            pass

    if not uids:
        return []

    uids = sorted(uids)[-max_emails:]   # newest N to stay within limits
    result = []

    for i in range(0, len(uids), 15):
        batch = uids[i:i+15]
        try:
            msgs = imap.fetch(batch, ["RFC822", "INTERNALDATE"])
            time.sleep(0.25)
            for uid, data in msgs.items():
                raw = data.get(b"RFC822")
                if raw:
                    em = _parse_email(raw, data.get(b"INTERNALDATE"))
                    if em:
                        result.append(em)
        except Exception as e:
            print(f"    fetch error (batch starting {batch[0]}): {e}")

    result.sort(key=lambda e: e["date_iso"])
    return result


# ─────────────────────────────────────────────
# Email classifier
# ─────────────────────────────────────────────

def classify_email(em: dict) -> dict:
    """
    Inspect one email and return {field: value} for every pipeline field
    this email provides evidence for. Values are ISO timestamps except for
    spm_po_number and so_number which are strings.

    Rules are applied in the most specific → least specific order so a single
    email can fill multiple fields (e.g. a warehouse stock-check reply also
    carries the sent_to_warehouse timestamp implicitly — but we only claim what
    we can prove from THIS email).
    """
    sender  = em["from"]
    to_cc   = em["to_cc"]
    subj    = em["subject"]
    subj_l  = subj.lower()
    body    = em["body"]
    body_l  = body.lower()
    date    = em["date_iso"]

    if not date:
        return {}

    updates: dict = {}

    is_spm          = SPM_SENDER in sender
    is_warehouse    = WAREHOUSE in sender
    is_to_warehouse = WAREHOUSE in to_cc
    is_to_flex      = "flexitallic" in to_cc
    is_flex         = "flexitallic.eu" in sender
    is_so_ack       = FLEXITALLIC_SENDER in sender
    is_unicorn      = "unicornsl" in sender or (
                        "unicorn" in sender and "freight" in sender)

    so_match    = SO_RE.search(subj + " " + body)
    so_in_body  = bool(SO_RE.search(body))

    # ── Warehouse → SPM ──────────────────────────────────────────────
    if is_warehouse:
        if "request" in subj_l and ("deliv" in subj_l or "delivery" in subj_l):
            updates["delivery_requested_at"] = date
        elif (    "completely delivered" in body_l
               or "completely deliver"  in body_l
               or "fully delivered"     in body_l
               or "waybill for"         in body_l
               or "waybill item"        in body_l ):
            updates["delivered_at"] = date
        else:
            # Generic warehouse reply = stock check result
            updates["stock_check_completed_at"] = date

    # ── SPM → Flexitallic (check before warehouse — dispatch instructions
    #    emails go To: Flex with warehouse only CC'd) ───────────────────
    elif is_spm and is_to_flex:
        if "purchase order" in subj_l:
            # Outgoing SPM PO to supplier
            updates["spm_po_sent_at"] = date
            m = SPM_PO_RE.search(subj)
            if m:
                updates["spm_po_number"] = re.sub(r"\s+", "", m.group(1))
        else:
            # Reply to Flexitallic that isn't a PO = dispatch instructions
            updates["dispatch_instructions_sent_at"] = date
        if so_match:
            updates["so_number"] = so_match.group(1).upper()

    # ── SPM → warehouse (Flex not a recipient) ───────────────────────
    elif is_spm and is_to_warehouse:
        if so_in_body:
            # Contains an SO number = forwarding SO/dispatch info to warehouse
            updates["so_sent_to_warehouse_at"] = date
            if so_match:
                updates["so_number"] = so_match.group(1).upper()
        else:
            # Bare PO routing = initial stock-check request
            updates["sent_to_warehouse_at"] = date

    # ── Flexitallic SO acknowledgment ────────────────────────────────
    elif is_so_ack:
        updates["so_received_at"] = date
        if so_match:
            updates["so_number"] = so_match.group(1).upper()

    # ── Other Flexitallic emails (Penny) ─────────────────────────────
    elif is_flex:
        if so_match:
            updates["so_number"] = so_match.group(1).upper()

        # "Collection arranged" = transport booked to Unicorn (more specific — check first)
        if (("arrange" in body_l or "collect" in body_l) and
                ("unicorn" in body_l or "transport" in body_l
                 or "pudsey" in body_l or "rtc" in body_l)):
            updates["ready_for_dispatch_at"] = date
        # "Packed & ready" = awaiting dispatch instructions (only if not the above)
        elif "dispatch" in body_l or "packed" in body_l:
            updates["flex_dispatch_ready_at"] = date

    # ── Unicorn / freight forwarder ───────────────────────────────────
    elif is_unicorn:
        if "noted" in body_l or "confirm" in body_l or "received" in body_l:
            updates["dispatched_at"] = date
        if so_match:
            updates["so_number"] = so_match.group(1).upper()

    return updates


# ─────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────

def _stamp_spm_table(db, so_number: str, field: str, value: str) -> None:
    """Also stamp the spm_purchase_orders row for dispatch-stage fields."""
    if not so_number:
        return
    res = (db.table("spm_purchase_orders")
             .select("id")
             .eq("so_number", so_number)
             .execute())
    for row in (res.data or []):
        db.table("spm_purchase_orders").update({field: value}).eq("id", row["id"]).is_(field, "null").execute()


def apply_updates(db, order: dict, updates: dict, dry_run: bool = False) -> list[str]:
    """
    Write each update to the orders row (only if still null) and advance status.
    Returns list of field names that were actually written (empty if nothing changed).
    """
    oid    = order["id"]
    so     = order.get("so_number") or updates.get("so_number")
    written: list[str] = []

    for field, value in updates.items():
        if dry_run:
            print(f"    [dry-run] would set {field} = {str(value)[:60]}")
            written.append(field)
            continue

        result = (
            db.table("orders")
              .update({field: value})
              .eq("id", oid)
              .is_(field, "null")
              .execute()
        )
        if result.data:
            written.append(field)
            status = FIELD_STATUS.get(field)
            if status:
                sync.advance_status(db, oid, status)
            if field in SPM_TABLE_FIELDS:
                _stamp_spm_table(db, so, field, value)

    # When so_number is known but promised_date is still null, fill it from
    # so_dispatch_groups. This covers orders where smart_gap_filler set
    # so_received_at from email content but the PDF-based process_so_received
    # never ran (e.g. listener was down when the SO arrived).
    if not dry_run and so and not order.get("promised_date") and "promised_date" not in updates:
        dg = (db.table("so_dispatch_groups")
                .select("dispatch_date")
                .eq("so_number", so)
                .execute())
        dates = sorted(
            d["dispatch_date"] for d in (dg.data or []) if d.get("dispatch_date")
        )
        if dates:
            result = (db.table("orders")
                        .update({"promised_date": dates[0]})
                        .eq("id", oid)
                        .is_("promised_date", "null")
                        .execute())
            if result.data:
                written.append("promised_date")
                sync.advance_status(db, oid, "supplier_acknowledged")

    return written


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def _null_fields(order: dict) -> set[str]:
    return {f for f in FIELD_STATUS if order.get(f) is None}


def run(args) -> None:
    db    = get_client()
    since = sync._backfill_since()

    # ── Fetch orders with at least one null pipeline field ─────────────────
    query = (db.table("orders")
               .select("id, buyer_po_number, so_number, " + ", ".join(FIELD_STATUS.keys()))
               .not_.is_("buyer_po_number", "null"))
    if args.po:
        query = query.eq("buyer_po_number", args.po)
    res = query.execute()

    orders = [o for o in (res.data or []) if _null_fields(o)]
    if args.limit:
        orders = orders[:args.limit]

    if not orders:
        print("No orders with null pipeline fields found.")
        return

    # ── Build lookup maps ──────────────────────────────────────────────────
    by_po: dict[str, list[dict]] = {}
    for o in orders:
        by_po.setdefault(o["buyer_po_number"], []).append(o)

    unique_sos: set[str] = {
        o["so_number"] for o in orders
        if o.get("so_number")
    }

    print(f"Smart Gap Filler — {len(orders)} order row(s), "
          f"{len(by_po)} unique PO(s), {len(unique_sos)} unique SO(s)")
    print(f"Searching Gmail since {since}  |  dry-run={args.dry_run}\n")

    # ── Phase 1: fetch all related emails (one IMAP search per PO/SO) ─────
    imap = _IMAP()
    po_emails:  dict[str, list[dict]] = {}
    so_emails:  dict[str, list[dict]] = {}

    total_pos  = len(by_po)
    total_sos  = len(unique_sos)

    for idx, po in enumerate(by_po, 1):
        print(f"  [{idx}/{total_pos}] fetching emails for PO {po}…", end=" ", flush=True)
        ems = fetch_emails_for_term(imap, po, since)
        po_emails[po] = ems
        print(f"{len(ems)} found")

    for idx, so in enumerate(unique_sos, 1):
        print(f"  [{idx}/{total_sos}] fetching emails for SO {so}…", end=" ", flush=True)
        ems = fetch_emails_for_term(imap, so, since)
        so_emails[so] = ems
        print(f"{len(ems)} found")

    imap.logout()
    print()

    # ── Phase 2: classify and fill ─────────────────────────────────────────
    change_log: list[dict] = []   # {po, order_id, field, value}

    for po, po_orders in by_po.items():
        for order in po_orders:
            null_flds = _null_fields(order)
            if not null_flds:
                continue

            so = order.get("so_number")
            # Combine PO emails + SO emails, deduplicate by message_id
            seen_ids: set[str] = set()
            all_emails: list[dict] = []
            for em in (po_emails.get(po, []) + so_emails.get(so, [])):
                if em["message_id"] not in seen_ids:
                    seen_ids.add(em["message_id"])
                    all_emails.append(em)
            all_emails.sort(key=lambda e: e["date_iso"])

            if not all_emails:
                continue

            # Classify each email and collect first-match-per-null-field
            updates: dict = {}
            for em in all_emails:
                classified = classify_email(em)
                for field, value in classified.items():
                    if field in null_flds and field not in updates:
                        updates[field] = value

            if not updates:
                continue

            print(f"  {po}  (order {order['id'][:8]}…)")
            for f, v in updates.items():
                print(f"    {f} = {str(v)[:70]}")

            written = apply_updates(db, order, updates, dry_run=args.dry_run)
            for field in written:
                change_log.append({
                    "po": po,
                    "order_id": order["id"][:8],
                    "field": field,
                    "value": str(updates[field])[:60],
                })

    # ── Summary ────────────────────────────────────────────────────────────
    total_orders = len({e["order_id"] for e in change_log})
    print(f"\n{'='*60}")
    if not change_log:
        print("No fields filled — nothing to update.")
    else:
        print(f"Changes written: {len(change_log)} field(s) across {total_orders} order(s)")
        print()
        current_po = None
        for entry in change_log:
            if entry["po"] != current_po:
                current_po = entry["po"]
                print(f"  PO {current_po}  (id …{entry['order_id']})")
            print(f"    + {entry['field']} = {entry['value']}")
    print(f"{'='*60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Smart pipeline gap filler")
    parser.add_argument("--po",      help="Process only this Chevron PO number")
    parser.add_argument("--limit",   type=int, help="Process at most N orders")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be filled without writing to DB")
    run(parser.parse_args())

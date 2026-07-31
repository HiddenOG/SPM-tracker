"""
backfill_nlng_dispatch.py — Backfill dispatch pipeline timestamps for NLNG orders.

Four passes:
  Pass 1 — Flexitallic dispatch-ready ("packed and ready")
            FROM flexitallic.eu BODY <so_number>; requires dispatch/packed keyword
            → stamps flex_dispatch_ready_at

  Pass 2 — SPM dispatch instructions ("Kindly ship to unicorn")
            FROM specialpiping@gmail.com TO flexitallic.eu BODY <so_number>
            → stamps dispatch_instructions_sent_at

  Pass 3 — Penny arranges transport ("collected tomorrow, delivery to Unicorn")
            FROM flexitallic.eu BODY <so_number>; requires transport + arrange keyword
            → stamps ready_for_dispatch_at

  Pass 4 — Freight forwarder ack ("Thanks Penny!")
            FROM unicornsl BODY <so_number>; requires noted/confirm/thanks keyword
            → stamps dispatched_at

Safe to re-run: stamp functions only write when the field is currently NULL.

Usage:
    python scripts/backfill_nlng_dispatch.py            # live run
    python scripts/backfill_nlng_dispatch.py --dry-run
"""

import email as _email_mod
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
from config import SPM_SENDER

DRY_RUN = "--dry-run" in sys.argv
FLEX_DOMAIN = "flexitallic.eu"
SO_RE       = re.compile(r"\bSO\d{5,}\b", re.IGNORECASE)


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




def _all_text(msg) -> str:
    """Concatenate all text/* MIME parts (plain + HTML)."""
    parts = msg.walk() if msg.is_multipart() else [msg]
    out = ""
    for part in parts:
        if part.get_content_type() in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True)
            if payload:
                out += payload.decode(errors="replace")
    return out


def _connect_gmail() -> IMAPClient:
    host = os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com")
    port = int(os.environ.get("GMAIL_IMAP_PORT", 993))
    imap = IMAPClient(host, port=port, use_uid=True, ssl=True)
    imap.login(os.environ["GMAIL_EMAIL"], os.environ["GMAIL_APP_PASSWORD"])
    imap.select_folder("[Gmail]/All Mail")
    return imap


# ── Pass 1: Flexitallic dispatch-ready → flex_dispatch_ready_at ──────────────

def backfill_dispatch_ready(db, imap: IMAPClient) -> int:
    print("Pass 1: Flexitallic dispatch-ready → flex_dispatch_ready_at")

    res = (
        db.table("nlng_orders")
        .select("id, po_number, so_number")
        .not_.is_("so_number", "null")
        .is_("flex_dispatch_ready_at", "null")
        .execute()
    )
    targets = res.data or []
    if not targets:
        print("  All NLNG orders with so_number already have flex_dispatch_ready_at — skipping.")
        return 0

    print(f"  {len(targets)} order(s) need flex_dispatch_ready_at")
    fixed = 0
    seen_uids: set[int] = set()

    for order in targets:
        so = order["so_number"]
        print(f"  Searching for SO {so}...", end=" ", flush=True)

        try:
            uids = imap.search(["FROM", FLEX_DOMAIN, "BODY", so])
        except Exception as e:
            print(f"search error: {e}")
            continue

        # Filter to unseen UIDs only (one SO can appear in multiple emails)
        new_uids = [u for u in uids if u not in seen_uids]
        print(f"{len(uids)} email(s) found ({len(new_uids)} new)")

        if not new_uids:
            continue

        try:
            msgs = imap.fetch(new_uids, ["RFC822", "INTERNALDATE"])
        except Exception as e:
            print(f"    ⚠️  Fetch error: {e}")
            continue

        for uid in new_uids:
            seen_uids.add(uid)
            data = msgs.get(uid)
            if not data or b"RFC822" not in data:
                continue

            msg = _email_mod.message_from_bytes(data[b"RFC822"])
            sender = _decode(msg.get("From", "")).lower()

            # Skip SO-ack emails — we want Penny's dispatch-ready notifications
            if "salesorder@flexitallic.eu" in sender:
                continue

            body = _all_text(msg).lower()
            if "dispatch" not in body and "packed" not in body:
                continue

            internal_date = data.get(b"INTERNALDATE")
            email_date = (
                internal_date.isoformat()
                if (internal_date and hasattr(internal_date, "isoformat"))
                else sync.parse_email_date(msg)
            )
            subj = _decode(msg.get("Subject", ""))

            print(f"    NLNG {order['po_number']} | SO {so} | dispatch ready {email_date[:10]} | {subj[:60]}")
            if not DRY_RUN:
                sync.stamp_nlng_flex_dispatch_ready(order["id"], email_date)
            fixed += 1
            break  # earliest matching email is enough

    return fixed


# ── Pass 2: SPM dispatch instructions → dispatch_instructions_sent_at ─────────

def backfill_dispatch_instructions(db, imap: IMAPClient) -> int:
    print("\nPass 2: SPM dispatch instructions → dispatch_instructions_sent_at")

    res = (
        db.table("nlng_orders")
        .select("id, po_number, so_number")
        .not_.is_("so_number", "null")
        .is_("dispatch_instructions_sent_at", "null")
        .execute()
    )
    targets = res.data or []
    if not targets:
        print("  All NLNG orders with so_number already have dispatch_instructions_sent_at — skipping.")
        return 0

    print(f"  {len(targets)} order(s) need dispatch_instructions_sent_at")
    fixed = 0

    for order in targets:
        so = order["so_number"]
        print(f"  Searching for SO {so}...", end=" ", flush=True)

        try:
            uids = imap.search(["FROM", SPM_SENDER, "TO", FLEX_DOMAIN, "BODY", so])
        except Exception as e:
            print(f"search error: {e}")
            continue

        print(f"{len(uids)} email(s) found")

        if not uids:
            continue

        try:
            msgs = imap.fetch(uids, ["RFC822", "INTERNALDATE"])
        except Exception as e:
            print(f"    ⚠️  Fetch error: {e}")
            continue

        for uid in uids:
            data = msgs.get(uid)
            if not data or b"RFC822" not in data:
                continue

            msg = _email_mod.message_from_bytes(data[b"RFC822"])
            internal_date = data.get(b"INTERNALDATE")
            email_date = (
                internal_date.isoformat()
                if (internal_date and hasattr(internal_date, "isoformat"))
                else sync.parse_email_date(msg)
            )
            subj = _decode(msg.get("Subject", ""))

            print(f"    NLNG {order['po_number']} | SO {so} | dispatch instructions {email_date[:10]} | {subj[:60]}")
            if not DRY_RUN:
                sync.stamp_nlng_dispatch_instructions_sent(order["id"], email_date)
            fixed += 1
            break  # earliest matching email is enough

    return fixed


# ── Pass 3: Penny arranges transport → ready_for_dispatch_at ─────────────────

_TRANSPORT_KW = ("unicorn", "transport", "pudsey", "rtc")
_ARRANGE_KW   = ("arrange", "collect", "dispatch")


def backfill_ready_for_dispatch(db, imap: IMAPClient) -> int:
    print("\nPass 3: Penny arranges transport → ready_for_dispatch_at")

    res = (
        db.table("nlng_orders")
        .select("id, po_number, so_number")
        .not_.is_("so_number", "null")
        .is_("ready_for_dispatch_at", "null")
        .execute()
    )
    targets = res.data or []
    if not targets:
        print("  All NLNG orders with so_number already have ready_for_dispatch_at — skipping.")
        return 0

    print(f"  {len(targets)} order(s) need ready_for_dispatch_at")
    fixed = 0
    seen_uids: set[int] = set()

    for order in targets:
        so = order["so_number"]
        print(f"  Searching for SO {so}...", end=" ", flush=True)

        try:
            uids = imap.search(["FROM", FLEX_DOMAIN, "BODY", so])
        except Exception as e:
            print(f"search error: {e}")
            continue

        new_uids = [u for u in uids if u not in seen_uids]
        print(f"{len(uids)} email(s) found ({len(new_uids)} new)")

        if not new_uids:
            continue

        try:
            msgs = imap.fetch(new_uids, ["RFC822", "INTERNALDATE"])
        except Exception as e:
            print(f"    ⚠️  Fetch error: {e}")
            continue

        for uid in new_uids:
            seen_uids.add(uid)
            data = msgs.get(uid)
            if not data or b"RFC822" not in data:
                continue

            msg = _email_mod.message_from_bytes(data[b"RFC822"])
            sender = _decode(msg.get("From", "")).lower()

            # Skip SO-ack emails from salesorder@
            if "salesorder@flexitallic.eu" in sender:
                continue

            body = _all_text(msg).lower()
            has_transport = any(kw in body for kw in _TRANSPORT_KW)
            has_arrange   = any(kw in body for kw in _ARRANGE_KW)
            if not (has_transport and has_arrange):
                continue

            internal_date = data.get(b"INTERNALDATE")
            email_date = (
                internal_date.isoformat()
                if (internal_date and hasattr(internal_date, "isoformat"))
                else sync.parse_email_date(msg)
            )
            subj = _decode(msg.get("Subject", ""))

            print(f"    NLNG {order['po_number']} | SO {so} | ready for dispatch {email_date[:10]} | {subj[:60]}")
            if not DRY_RUN:
                sync.stamp_nlng_ready_for_dispatch(order["id"], email_date)
            fixed += 1
            break

    return fixed


# ── Pass 4: Freight forwarder ack → dispatched_at ────────────────────────────

_FREIGHT_FORWARDER_DOMAINS = ["unicornsl", "unicorn freight", "airfreight@unicorn"]


def backfill_dispatched(db, imap: IMAPClient) -> int:
    print("\nPass 4: Freight forwarder ack → dispatched_at")

    res = (
        db.table("nlng_orders")
        .select("id, po_number, so_number")
        .not_.is_("so_number", "null")
        .is_("dispatched_at", "null")
        .execute()
    )
    targets = res.data or []
    if not targets:
        print("  All NLNG orders with so_number already have dispatched_at — skipping.")
        return 0

    print(f"  {len(targets)} order(s) need dispatched_at")
    fixed = 0
    seen_uids: set[int] = set()

    for order in targets:
        so = order["so_number"]
        print(f"  Searching for SO {so}...", end=" ", flush=True)

        # Search across all freight forwarder domains
        uids_all: list[int] = []
        for ff in _FREIGHT_FORWARDER_DOMAINS:
            try:
                uids_all.extend(imap.search(["FROM", ff, "BODY", so]))
            except Exception:
                pass
        uids_all = list(dict.fromkeys(uids_all))  # deduplicate, preserve order

        new_uids = [u for u in uids_all if u not in seen_uids]
        print(f"{len(uids_all)} email(s) found ({len(new_uids)} new)")

        if not new_uids:
            continue

        try:
            msgs = imap.fetch(new_uids, ["RFC822", "INTERNALDATE"])
        except Exception as e:
            print(f"    ⚠️  Fetch error: {e}")
            continue

        for uid in new_uids:
            seen_uids.add(uid)
            data = msgs.get(uid)
            if not data or b"RFC822" not in data:
                continue

            msg = _email_mod.message_from_bytes(data[b"RFC822"])
            body = _all_text(msg).lower()
            if "noted" not in body and "confirm" not in body and "thanks" not in body:
                continue

            internal_date = data.get(b"INTERNALDATE")
            email_date = (
                internal_date.isoformat()
                if (internal_date and hasattr(internal_date, "isoformat"))
                else sync.parse_email_date(msg)
            )
            subj = _decode(msg.get("Subject", ""))

            print(f"    NLNG {order['po_number']} | SO {so} | dispatched {email_date[:10]} | {subj[:60]}")
            if not DRY_RUN:
                sync.stamp_nlng_dispatched(order["id"], email_date)
            fixed += 1
            break

    return fixed


# ── main ─────────────────────────────────────────────────────────────────────

def run():
    db = get_client()
    imap = _connect_gmail()
    try:
        r1 = backfill_dispatch_ready(db, imap)
        r2 = backfill_dispatch_instructions(db, imap)
        r3 = backfill_ready_for_dispatch(db, imap)
        r4 = backfill_dispatched(db, imap)
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    action = "[dry-run] would update" if DRY_RUN else "updated"
    print(
        f"\nDone. {action}:"
        f"  {r1} flex_dispatch_ready_at,"
        f"  {r2} dispatch_instructions_sent_at,"
        f"  {r3} ready_for_dispatch_at,"
        f"  {r4} dispatched_at"
    )


if __name__ == "__main__":
    run()

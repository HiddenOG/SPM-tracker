"""
Chevron PO cancellation / closure listener.

Chevron's GEP SMART system sends a notification when a purchase order it
already sent us is cancelled or closed:

    New Order : "PO1 for JDEJobID 63183,..."(0061419934) for $2,440.94 from
    Chevron received on 5/12/2026 (UTC) is cancelled by Elohor Chinakwe on
    7/10/2026 (UTC).

    Order"PO1 for JDEJobID 57860,..." (0061154876) for $ 10,400.00 has been
    closed. You can contact Regina ...

imap_listener.process_message() sees these (it searches ALL of the Yahoo
inbox, unfiltered) but its new-PO gate deliberately DISCARDS them, so an
order cancelled at Chevron kept moving through our pipeline. This module
does the opposite: it recognises exactly these two templates and stamps the
order so its status derives as terminal.

IMPORTANT — the false positive this must not fall for:

    The event "RPRNGN0020803" has been canceled and will no longer be
    available for participation. Comments: This is cancelled to be reraised
    as direct charge...

That is a SOURCING EVENT (an RFQ) being cancelled, not a purchase order. It
contains "is cancelled" and "has been canceled" but carries no PO number.
Requiring a parenthesised Chevron PO number is what separates the two, so
that requirement is load-bearing — do not relax it.
"""
import os
import re
import sys
import email
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

load_dotenv()

import sync
from db import get_client
from email_utils import decode_mime_words, get_email_body_text, is_already_processed

CHEVRON_NOTIFICATION_SENDER = "chevron.notification.prod@gep.com"

# "…(0061419934) for $2,440.94 … is cancelled by Elohor Chinakwe on 7/10/2026"
_CANCELLED_RE = re.compile(r"\bis\s+cancell?ed\b", re.IGNORECASE)
# "Order"PO1 for …" (0061154876) for $ 10,400.00 has been closed."
_CLOSED_RE = re.compile(r"\bhas\s+been\s+closed\b", re.IGNORECASE)

# Who performed it, and the date they state. Both are recorded for the audit
# trail only — never used as the timestamp, see parse_cancellation().
_ACTOR_RE = re.compile(
    r"is\s+cancell?ed\s+by\s+(.{2,60}?)\s+on\s+(\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)


def _readable(text: str) -> str:
    """Strip the HTML the GEP template carries so the audit note is legible.

    cancellation_raw is read by a person checking why an order was cancelled,
    and the raw body arrives full of <p>, <br /> and &nbsp;.
    """
    import html as _htmlmod
    t = re.sub(r"<[^>]+>", " ", text or "")
    t = _htmlmod.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def parse_cancellation(subject: str, body_text: str) -> dict | None:
    """Recognise a Chevron PO cancellation/closure email.

    Returns None for anything else — including sourcing-event cancellations,
    which is the whole reason a PO number is required rather than optional.
    """
    if not body_text:
        return None

    flat = re.sub(r"\s+", " ", body_text)

    is_cancelled = bool(_CANCELLED_RE.search(flat))
    is_closed = bool(_CLOSED_RE.search(flat))
    if not (is_cancelled or is_closed):
        return None

    # The PO number in parentheses is what makes this about an ORDER. A
    # sourcing-event cancellation has no such number and is dropped here.
    m = sync.PO_IN_PARENS_RE.search(flat)
    if not m:
        return None

    raw_po = re.sub(r"\s+", "", m.group(1))
    # Change orders overwrite the base PO row, so "0060834302-001" is the
    # same order as "0060834302" — strip the revision suffix.
    base_po = raw_po.split("-")[0]

    actor = stated_date = None
    am = _ACTOR_RE.search(flat)
    if am:
        actor = am.group(1).strip()
        stated_date = am.group(2).strip()

    # "cancelled" wins over "closed" when a body somehow carries both: it is
    # the more specific statement about why the order stopped.
    kind = "cancelled" if is_cancelled else "closed"

    idx = m.start()
    return {
        "kind": kind,
        "po_number": base_po,
        "raw_po_number": raw_po,
        "actor": actor,
        "stated_date": stated_date,
        "subject": subject,
        "evidence": _readable(flat[max(0, idx - 160): idx + 220]),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Applying a parsed cancellation to the order
# ─────────────────────────────────────────────────────────────────────────────

def _column_exists() -> bool:
    """True once migrations/add_order_cancellation.sql has been applied."""
    try:
        get_client().table("orders").select("cancelled_at").limit(1).execute()
        return True
    except Exception:
        return False


def record_cancellation(client, parsed: dict, email_date, dry_run: bool = False) -> dict:
    """Stamp the order a parsed cancellation refers to.

    The email's OWN date is used as cancelled_at, never the date written in
    the body: Chevron writes those inconsistently ("7/10/2026" month-first,
    "24/04/2025" day-first), so parsing them would silently invent dates.
    The stated string is kept verbatim in cancellation_raw instead.
    """
    po = parsed["po_number"]
    # select("*") rather than naming cancelled_at: this must still run as a
    # dry run BEFORE the migration is applied, and PostgREST errors on a
    # column that does not exist yet.
    rows = (client.table("orders").select("*")
            .eq("buyer_po_number", po).execute().data or [])
    if not rows:
        return {"po": po, "outcome": "no_order_row"}

    order = rows[0]
    if order.get("cancelled_at"):
        return {"po": po, "outcome": "already_recorded", "status": order.get("overall_status")}

    stamp = email_date.isoformat() if hasattr(email_date, "isoformat") else str(email_date)
    note = parsed.get("evidence") or ""
    if parsed.get("actor"):
        note = f"{parsed['kind']} by {parsed['actor']} on {parsed.get('stated_date') or '?'} — {note}"

    update = {
        "cancelled_at": stamp,
        "cancellation_kind": parsed["kind"],
        "cancellation_actor": parsed.get("actor"),
        "cancellation_raw": note[:2000],
        # Written directly rather than via advance_status(): this is the one
        # transition that must be allowed to move an order BACKWARDS, from
        # ready_for_dispatch or anywhere else, to a terminal state.
        "overall_status": "closed" if parsed["kind"] == "closed" else "cancelled",
    }
    if not dry_run:
        client.table("orders").update(update).eq("id", order["id"]).execute()

    return {
        "po": po,
        "outcome": "would_update" if dry_run else "updated",
        "from_status": order.get("overall_status"),
        "to_status": update["overall_status"],
        "kind": parsed["kind"],
        "actor": parsed.get("actor"),
        "at": stamp,
    }


def handle_terminal_email(client, subject: str, body_text: str, email_date,
                          dry_run: bool = False,
                          include_closed: bool = False) -> dict | None:
    """Parse one email and apply it. Returns None if it isn't a PO cancellation.

    "has been closed" notices are ignored by default. Chevron sends those
    for orders we already delivered - closing out a finished PO is normal
    housekeeping, not a problem to fix - so acting on them would rewrite
    healthy "Delivered" rows for no benefit. Opt in with include_closed.
    """
    parsed = parse_cancellation(subject, body_text)
    if not parsed:
        return None
    if parsed["kind"] == "closed" and not include_closed:
        return None
    result = record_cancellation(client, parsed, email_date, dry_run=dry_run)
    result["subject"] = subject
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Mailbox scan
# ─────────────────────────────────────────────────────────────────────────────

_SEARCH_TERMS = ("is cancelled", "is canceled", "has been closed")


def scan_mailbox(dry_run: bool = True, days_back: int | None = None,
                 include_closed: bool = False) -> list[dict]:
    """Scan the Yahoo inbox for cancellation/closure notices and apply them.

    Cancellations arrive on Yahoo, from chevron.notification.prod@gep.com.
    They are NOT on Gmail — Chevron has never sent to that address — so there
    is deliberately no Gmail pass here.
    """
    from datetime import datetime, timedelta
    from imap_listener import connect_to_yahoo

    client = get_client()
    imap = connect_to_yahoo()
    results: list[dict] = []
    try:
        uids: set = set()
        for term in _SEARCH_TERMS:
            query = ["FROM", CHEVRON_NOTIFICATION_SENDER, "BODY", term]
            if days_back:
                query += ["SINCE", (datetime.now() - timedelta(days=days_back)).date()]
            try:
                uids |= set(imap.search(query))
            except Exception as exc:
                print(f"  [warn] search {term!r} failed: {exc}")
        uids = sorted(uids)
        print(f"  {len(uids)} candidate email(s) to inspect")

        BATCH = 15
        for i in range(0, len(uids), BATCH):
            chunk = uids[i:i + BATCH]
            fetched = imap.fetch(chunk, ["RFC822", "INTERNALDATE"])
            for uid in chunk:
                data = fetched.get(uid)
                if not data:
                    continue
                try:
                    msg = email.message_from_bytes(data[b"RFC822"])
                    res = handle_terminal_email(
                        client,
                        decode_mime_words(msg.get("Subject", "") or ""),
                        get_email_body_text(msg),
                        data[b"INTERNALDATE"],
                        dry_run=dry_run,
                        include_closed=include_closed,
                    )
                    if res:
                        results.append(res)
                except Exception as exc:
                    print(f"  [warn] uid {uid} failed: {exc}")
    finally:
        imap.logout()
    return results


def main() -> None:
    dry_run = "--apply" not in sys.argv
    include_closed = "--include-closed" in sys.argv
    days = None
    for arg in sys.argv[1:]:
        if arg.startswith("--days="):
            days = int(arg.split("=", 1)[1])

    if not dry_run and not _column_exists():
        print("Refusing to write: orders.cancelled_at does not exist.")
        print("Apply migrations/add_order_cancellation.sql first.")
        sys.exit(1)

    print(f"Chevron cancellation scan - {'DRY RUN' if dry_run else 'APPLYING'}"
          f"{f', last {days} days' if days else ', full inbox'}"
          f"{', including closed' if include_closed else ', cancellations only'}")
    results = scan_mailbox(dry_run=dry_run, days_back=days,
                           include_closed=include_closed)

    by_outcome: dict[str, list[dict]] = {}
    for r in results:
        by_outcome.setdefault(r["outcome"], []).append(r)

    print(f"\n{len(results)} PO cancellation/closure notice(s) recognised\n")
    for outcome in ("would_update", "updated", "already_recorded", "no_order_row"):
        rows = by_outcome.get(outcome, [])
        if not rows:
            continue
        print(f"-- {outcome} ({len(rows)}) " + "-" * 40)
        for r in rows:
            if outcome in ("would_update", "updated"):
                print(f"   {r['po']}  {r['from_status']} -> {r['to_status']}"
                      f"   ({r['kind']} by {r.get('actor') or '?'}, {str(r['at'])[:10]})")
            else:
                print(f"   {r['po']}")
        print()


if __name__ == "__main__":
    main()

"""
parser_coverage.py — Gmail-driven parser coverage diagnostic.

For each pipeline field that has null values on orders that should have it
filled, this script:
  1. Finds those orders in the DB.
  2. Fetches all related emails from Gmail (by PO number and SO number).
  3. Runs each email through the classifier from smart_gap_filler.
  4. Reports CAUGHT (classifier produced the right field) vs MISSED
     (classifier returned nothing for that field), showing sender + subject
     + first 120 chars of body for every missed email so you can see the
     exact patterns that need fixing.

Run:
  python scripts/parser_coverage.py                         # all fields
  python scripts/parser_coverage.py --field spm_po_number  # one field
  python scripts/parser_coverage.py --po 0061434916        # one PO only
  python scripts/parser_coverage.py --field ready_for_dispatch_at --show-caught
"""

import os
import re
import sys
import argparse
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

# Reuse all IMAP + classify machinery from smart_gap_filler
from smart_gap_filler import (
    _IMAP,
    fetch_emails_for_term,
    classify_email,
    FIELD_STATUS,
    WAREHOUSE,
    SPM_SENDER,
)
from db import get_client
import sync

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# ─────────────────────────────────────────────
# Which field to check, and what earlier field
# proves the order SHOULD already have it.
# ─────────────────────────────────────────────

FIELD_PREREQ: dict[str, str] = {
    "spm_po_sent_at":                "stock_check_completed_at",
    "spm_po_number":                 "stock_check_completed_at",
    "so_received_at":                "spm_po_sent_at",
    "so_number":                     "spm_po_sent_at",
    "so_sent_to_warehouse_at":       "so_received_at",
    "flex_dispatch_ready_at":        "so_received_at",
    "dispatch_instructions_sent_at": "flex_dispatch_ready_at",
    "ready_for_dispatch_at":         "dispatch_instructions_sent_at",
    "dispatched_at":                 "ready_for_dispatch_at",
    "delivery_requested_at":         "dispatched_at",
}

ALL_FIELDS = list(FIELD_PREREQ.keys())

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _sender_domain(sender: str) -> str:
    """Return the @domain part, or full sender if no @."""
    m = re.search(r"@[\w.\-]+", sender)
    return m.group(0) if m else sender[:40]


def _preview(text: str, n: int = 120) -> str:
    clean = " ".join(text.split())
    return clean[:n] + ("…" if len(clean) > n else "")


def _pattern_key(em: dict) -> str:
    """Group missed emails by sender-domain + first 6 words of subject."""
    domain = _sender_domain(em["from"])
    words  = " ".join(em["subject"].split()[:6])
    return f"{domain}  |  {words}"


# ─────────────────────────────────────────────
# Core
# ─────────────────────────────────────────────

def check_field(db, imap: _IMAP, field: str, target_po: str | None,
                show_caught: bool, since) -> None:
    prereq = FIELD_PREREQ[field]

    # Orders where the field is null but the prerequisite is set
    query = (
        db.table("orders")
          .select(f"id, buyer_po_number, so_number, {field}, {prereq}")
          .is_(field, "null")
          .not_.is_(prereq, "null")
    )
    if target_po:
        query = query.eq("buyer_po_number", target_po)
    res = query.execute()
    orders = res.data or []

    if not orders:
        print(f"  [OK] {field} — no orders with null value (prereq={prereq})\n")
        return

    print(f"\n{'='*70}")
    print(f"  FIELD: {field}   ({len(orders)} orders null, prereq={prereq})")
    print(f"{'='*70}")

    # Unique POs and SOs to search
    unique_pos = list({o["buyer_po_number"] for o in orders if o.get("buyer_po_number")})
    unique_sos = list({o["so_number"]       for o in orders if o.get("so_number")})

    # Fetch emails
    po_emails: dict[str, list[dict]] = {}
    so_emails: dict[str, list[dict]] = {}

    print(f"  Fetching {len(unique_pos)} PO search(es) + {len(unique_sos)} SO search(es)…")
    for po in unique_pos:
        ems = fetch_emails_for_term(imap, po, since)
        po_emails[po] = ems

    for so in unique_sos:
        ems = fetch_emails_for_term(imap, so, since)
        so_emails[so] = ems

    # Classify per order
    caught_examples: list[dict]           = []
    missed_by_pattern: dict[str, list]    = defaultdict(list)
    irrelevant_count  = 0
    no_email_orders   = 0

    for order in orders:
        po = order["buyer_po_number"]
        so = order.get("so_number")

        # Combine + deduplicate
        seen: set[str] = set()
        all_emails: list[dict] = []
        for em in (po_emails.get(po, []) + so_emails.get(so, [])):
            if em["message_id"] not in seen:
                seen.add(em["message_id"])
                all_emails.append(em)
        all_emails.sort(key=lambda e: e["date_iso"])

        if not all_emails:
            no_email_orders += 1
            continue

        found_for_order = False
        for em in all_emails:
            classified = classify_email(em)
            if field in classified:
                found_for_order = True
                caught_examples.append({
                    "po": po,
                    "from": em["from"],
                    "subject": em["subject"],
                    "date": em["date_iso"][:10],
                    "value": str(classified[field])[:40],
                })
            elif not classified:
                # Classifier returned nothing — check if email is even relevant
                body_l   = em["body"].lower()
                subj_l   = em["subject"].lower()
                combined = body_l + " " + subj_l
                # Is it plausibly about this pipeline stage?
                if any(k in combined for k in (
                    "dispatch", "collect", "unicorn", "transport", "pudsey", "rtc",
                    "purchase order", "sales order", "acknowledgement", "acknowledgment",
                    "packed", "arrange", "noted", "delivery", "ship", "warehouse",
                    "stock check", "flexitallic",
                )):
                    key = _pattern_key(em)
                    entry = {
                        "po": po,
                        "from": em["from"],
                        "to_cc": em["to_cc"][:80],
                        "subject": em["subject"],
                        "body_preview": _preview(em["body"]),
                        "date": em["date_iso"][:10],
                    }
                    if len(missed_by_pattern[key]) < 3:  # max 3 examples per pattern
                        missed_by_pattern[key].append(entry)
                else:
                    irrelevant_count += 1

    # ── Print results ──────────────────────────────────────────────
    print(f"\n  CAUGHT: {len(caught_examples)} email(s) produce a value for '{field}'")
    if show_caught:
        for ex in caught_examples[:20]:
            print(f"    {ex['date']}  {_sender_domain(ex['from']):<28}  {ex['subject'][:55]}")
            print(f"           → {ex['value']}")
    else:
        # Just show first 5 regardless
        for ex in caught_examples[:5]:
            print(f"    {ex['date']}  {_sender_domain(ex['from']):<28}  {ex['subject'][:55]}")

    if no_email_orders:
        print(f"\n  NO EMAILS FOUND for {no_email_orders} order(s) — PO/SO not in Gmail since {since}")

    if not missed_by_pattern:
        print(f"\n  No plausibly-relevant missed emails found.\n")
        return

    total_missed = sum(len(v) for v in missed_by_pattern.values())
    print(f"\n  MISSED: {total_missed}+ plausibly-relevant email(s) the classifier didn't catch")
    print(f"  (Grouped by sender+subject pattern — up to 3 examples each)\n")

    for pattern, examples in sorted(missed_by_pattern.items(), key=lambda x: -len(x[1])):
        print(f"  ── Pattern: {pattern}")
        for ex in examples:
            print(f"     PO {ex['po']}  {ex['date']}")
            print(f"     FROM:    {ex['from']}")
            print(f"     TO/CC:   {ex['to_cc']}")
            print(f"     SUBJECT: {ex['subject']}")
            print(f"     BODY:    {ex['body_preview']}")
            print()

    if irrelevant_count:
        print(f"  ({irrelevant_count} emails skipped as clearly irrelevant to this pipeline stage)")
    print()


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def run(args) -> None:
    db    = get_client()
    since = sync._backfill_since()

    fields = [args.field] if args.field else ALL_FIELDS

    # Validate
    for f in fields:
        if f not in FIELD_PREREQ:
            print(f"Unknown field: {f}. Valid fields:\n  " + "\n  ".join(ALL_FIELDS))
            sys.exit(1)

    print(f"Parser Coverage Diagnostic")
    print(f"Since: {since}  |  Fields: {', '.join(fields)}")
    print(f"Connecting to Gmail…")

    imap = _IMAP()
    try:
        for field in fields:
            check_field(
                db, imap, field,
                target_po=args.po,
                show_caught=args.show_caught,
                since=since,
            )
    finally:
        imap.logout()

    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parser coverage diagnostic")
    parser.add_argument("--field", help="Check only this pipeline field")
    parser.add_argument("--po",    help="Limit to this Chevron PO number")
    parser.add_argument("--show-caught", action="store_true",
                        help="Also print all caught emails (not just missed)")
    run(parser.parse_args())

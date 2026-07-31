"""
backfill_claude.py — fill in the Claude-dependent fields that were deferred
while the Anthropic API was unavailable (e.g. empty credits).

Background
----------
When credits are empty, the live listeners still do everything that does NOT
need Claude:
  * warehouse-routing emails stamp `sent_to_warehouse_at` and acknowledgment,
    but leave the EXACT acknowledged date null and set pending_ack_extraction.
  * warehouse replies are parked (with their body saved) and the order is
    flagged pending_stock_extraction.

Once you top up credits, run this ONCE to backfill:
  * pending_ack_extraction  -> read the parked routing PDF, extract the real
    'Supplier Acknowledged on' date, stamp acknowledged_at.
  * pending_stock_extraction -> interpret the parked warehouse reply body,
    save the stock-check result.

It is safe to re-run: it only touches orders still flagged pending_*, and it
clears the flag + removes the parked row on success.

Usage:
  python scripts/backfill_claude.py            # process everything pending
  python scripts/backfill_claude.py --dry-run  # show what would be done
"""

import sys
import json
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv

from db import get_client
import sync

# Reuse the EXACT Claude functions the listeners use — no duplicated prompts.
from gmail_ack_listener import extract_ack_info_with_claude
from warehouse_reply_parser import (
    interpret_reply_with_claude,
    save_stock_check_result,
)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

load_dotenv()

DRY_RUN = "--dry-run" in sys.argv

# Statuses considered "further along" than plain acknowledgment — we never
# regress these when stamping the backfilled ack date.
LATER_THAN_ACK = {
    "awaiting_warehouse_stock_check",
    "stock_check_complete",
    "stock_check_needs_review",
}


def _orders_flagged(column: str) -> list[dict]:
    client = get_client()
    res = client.table("orders").select("*").eq(column, True).execute()
    return res.data or []


def _parked_for(po_number: str, kind: str) -> dict | None:
    client = get_client()
    res = (
        client.table("parked_emails")
        .select("*")
        .eq("po_number", po_number)
        .eq("kind", kind)
        .order("email_date", desc=True)
        .execute()
    )
    return res.data[0] if res.data else None


# ------------------------------------------------------------
# 1. Acknowledgment-date backfill (from the parked routing PDF).
# ------------------------------------------------------------
def backfill_ack_dates() -> int:
    orders = _orders_flagged("pending_ack_extraction")
    if not orders:
        print("No orders pending acknowledgment-date extraction.")
        return 0

    print(f"\n=== Acknowledgment-date backfill: {len(orders)} order(s) ===")
    client = get_client()
    done = 0

    for order in orders:
        po = order["buyer_po_number"]
        parked = _parked_for(po, "warehouse_routing")
        pdf_path = (parked or {}).get("pdf_path") or order.get("pdf_attachment_path")

        if not pdf_path or not Path(pdf_path).exists():
            print(f"⚠️  PO {po}: no PDF on disk to extract from "
                  f"({pdf_path!r}) — leaving flagged.")
            continue

        if DRY_RUN:
            print(f"[dry-run] PO {po}: would extract ack date from {pdf_path}")
            continue

        try:
            info = extract_ack_info_with_claude(pdf_path)
        except Exception as e:
            print(f"❌ PO {po}: Claude extraction failed — {e}")
            continue

        ack_date = info.get("supplier_acknowledged_on")
        if not ack_date:
            # PDF genuinely has no acknowledged date — clear the flag so we
            # don't keep retrying a PDF that will never have one.
            client.table("orders").update(
                {"pending_ack_extraction": False}
            ).eq("id", order["id"]).execute()
            print(f"ℹ️  PO {po}: PDF has no 'Supplier Acknowledged on' date — flag cleared.")
            continue

        # Preserve the furthest-along status (don't regress a PO that's
        # already awaiting/at stock check back to 'acknowledged').
        update = {
            "acknowledgment_status": "acknowledged",
            "acknowledged_at": ack_date,
            "acknowledged_by": "GEP export (backfilled)",
            "pending_ack_extraction": False,
        }
        if order.get("overall_status") not in LATER_THAN_ACK:
            update["overall_status"] = "acknowledged"

        client.table("orders").update(update).eq("id", order["id"]).execute()

        # The routing email is fully applied now — drop it from parking.
        if parked:
            sync.delete_parked(parked["id"])

        print(f"✅ PO {po}: acknowledged_at backfilled = {ack_date}")
        done += 1

    return done


# ------------------------------------------------------------
# 2. Stock-check backfill (from the parked warehouse reply body).
# ------------------------------------------------------------
def backfill_stock_checks() -> int:
    orders = _orders_flagged("pending_stock_extraction")
    if not orders:
        print("No orders pending stock-check interpretation.")
        return 0

    print(f"\n=== Stock-check backfill: {len(orders)} order(s) ===")
    client = get_client()
    done = 0

    for order in orders:
        po = order["buyer_po_number"]
        parked = _parked_for(po, "warehouse_reply")

        if not parked or not parked.get("body_text"):
            print(f"⚠️  PO {po}: no parked reply body to interpret — leaving flagged.")
            continue

        if DRY_RUN:
            print(f"[dry-run] PO {po}: would interpret parked reply "
                  f"({len(parked['body_text'])} chars)")
            continue

        try:
            result = interpret_reply_with_claude(parked["body_text"])
        except Exception as e:
            print(f"❌ PO {po}: Claude interpretation failed — {e}")
            continue

        save_stock_check_result(order["id"], result)
        client.table("orders").update(
            {"pending_stock_extraction": False}
        ).eq("id", order["id"]).execute()
        sync.delete_parked(parked["id"])

        flag = "⚠️  NEEDS REVIEW" if result.get("needs_human_review") else "✅"
        print(f"{flag} PO {po}: stock check = {result.get('overall_availability')}")
        done += 1

    return done


def main() -> None:
    if DRY_RUN:
        print("DRY RUN — no changes will be written.\n")

    acks = backfill_ack_dates()
    stocks = backfill_stock_checks()

    print(f"\nDone. Acknowledgment dates backfilled: {acks}. "
          f"Stock checks backfilled: {stocks}.")


if __name__ == "__main__":
    main()

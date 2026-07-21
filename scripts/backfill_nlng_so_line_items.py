"""
backfill_nlng_so_line_items.py — Backfill so_line_items and promised_date for
existing NLNG orders whose SO PDFs are already saved locally.

For every nlng_order that has a so_number and a saved SO PDF:
  1. Parse the PDF with parse_so_pdf() (same function Chevron uses)
  2. Delete + re-insert so_line_items rows for that so_number
  3. Set promised_date = earliest despatch_date from line items (if null)

Safe to re-run; --force overwrites even existing promised_date values.

Usage:
    python scripts/backfill_nlng_so_line_items.py            # live run
    python scripts/backfill_nlng_so_line_items.py --dry-run
    python scripts/backfill_nlng_so_line_items.py --force
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from db import get_client
from supplier_po_parser import parse_so_pdf

SO_DIR = ROOT / "data" / "so_attachments"

DRY_RUN = "--dry-run" in sys.argv
FORCE   = "--force"   in sys.argv


def run():
    db = get_client()

    res = db.table("nlng_orders").select(
        "id, po_number, so_number, promised_date"
    ).not_.is_("so_number", "null").execute()

    rows = res.data or []
    if not rows:
        print("No NLNG orders with SO numbers found.")
        return

    # Deduplicate by so_number — one SO PDF covers all orders sharing that SO
    seen: dict[str, list] = {}
    for row in rows:
        seen.setdefault(row["so_number"], []).append(row)

    done_items = done_dates = 0

    for so_number, orders in seen.items():
        # Find the saved SO PDF
        safe_so = so_number.replace("/", "_")
        candidates = list(SO_DIR.glob(f"{safe_so}*.pdf")) + list(SO_DIR.glob(f"{safe_so}_*.pdf"))
        if not candidates:
            print(f"  ⚠️  {so_number}: no PDF in {SO_DIR} — skipping")
            continue

        pdf_path = str(candidates[0])
        print(f"  {so_number} ({', '.join(o['po_number'] for o in orders)}) — parsing…")

        try:
            pdf_data = parse_so_pdf(pdf_path)
        except Exception as e:
            print(f"    ❌ parse error: {e}")
            continue

        line_items = pdf_data.get("line_items", [])
        dates = sorted(li["despatch_date"] for li in line_items if li.get("despatch_date"))
        promised = dates[0] if dates else None

        print(f"    {len(line_items)} line item(s) | promised: {promised or '—'}")

        if DRY_RUN:
            continue

        # ── SO line items ────────────────────────────────────────────────────
        if line_items:
            db.table("so_line_items").delete().eq("so_number", so_number).execute()
            db.table("so_line_items").insert([{
                "so_number":      so_number,
                "line_no":        li.get("line_no"),
                "item_number":    li.get("item_number"),
                "despatch_date":  li.get("despatch_date"),
                "qty":            li.get("qty"),
                "uom":            li.get("uom"),
                "unit_price":     li.get("unit_price"),
                "extended_price": li.get("extended_price"),
            } for li in line_items]).execute()
            done_items += 1
            print(f"    ✅ so_line_items inserted")

        # ── promised_date ────────────────────────────────────────────────────
        if promised:
            for order in orders:
                if FORCE or not order.get("promised_date"):
                    db.table("nlng_orders").update(
                        {"promised_date": promised}
                    ).eq("id", order["id"]).execute()
            done_dates += 1
            print(f"    ✅ promised_date = {promised}")

    action = "[dry-run] would update" if DRY_RUN else "updated"
    print(f"\nDone. {action} {done_items} SO item sets, {done_dates} promised_date(s).")


if __name__ == "__main__":
    run()

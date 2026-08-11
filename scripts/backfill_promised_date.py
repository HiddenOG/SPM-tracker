"""
backfill_promised_date.py — re-extract Chevron PO PDFs to populate
promised_date on order_line_items (new column from migration 015).

Only uses pdfplumber (no Claude), so it's fast and free.
Safe to re-run: only updates promised_date; does not touch other fields.

Usage:
  python scripts/backfill_promised_date.py
  python scripts/backfill_promised_date.py --dry-run
"""

import os
import sys
import tempfile
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError): pass

load_dotenv()

import sys
sys.path.insert(0, str(Path(__file__).parent))

from db import get_client
from pdf_extractor import extract_pdf_with_pdfplumber

DRY_RUN = "--dry-run" in sys.argv
# Pass --po 0061366168 0061385296 to limit to specific POs
_po_idx = sys.argv.index("--po") if "--po" in sys.argv else None
TARGET_POS = sys.argv[_po_idx + 1:] if _po_idx is not None else []


def fetch_orders(client):
    q = (
        client.table("orders")
        .select("id, buyer_po_number, pdf_url, pdf_attachment_path")
        .or_("pdf_url.not.is.null,pdf_attachment_path.not.is.null")
    )
    if TARGET_POS:
        q = q.in_("buyer_po_number", TARGET_POS)
    return q.execute().data or []


def backfill():
    client = get_client()
    orders = fetch_orders(client)
    print(f"Found {len(orders)} orders with PDFs")

    updated = skipped = failed = 0

    for order in orders:
        po = order["buyer_po_number"]
        pdf_url = order.get("pdf_url")
        pdf_local = order.get("pdf_attachment_path")
        tmp_path = None

        try:
            if pdf_url:
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                    tmp_path = f.name
                urllib.request.urlretrieve(pdf_url, tmp_path)
                pdf_path = tmp_path
            elif pdf_local and Path(pdf_local).exists():
                pdf_path = pdf_local
            else:
                print(f"  ⚠️  PO {po}: no PDF on disk, skipping")
                skipped += 1
                continue

            extraction = extract_pdf_with_pdfplumber(pdf_path)
            line_items = extraction.get("line_items", [])

            items_with_pd = [li for li in line_items if li.get("promised_date")]
            if not items_with_pd:
                print(f"  —  PO {po}: no promised dates found in PDF ({len(line_items)} items)")
                skipped += 1
                continue

            if DRY_RUN:
                for li in items_with_pd:
                    print(f"  [dry] PO {po} line {li['line_no']}: promised_date = {li['promised_date']}")
                updated += 1
                continue

            for li in line_items:
                line_no = li.get("line_no")
                promised = li.get("promised_date")
                if promised is None:
                    continue
                existing = (
                    client.table("order_line_items")
                    .select("id")
                    .eq("order_id", order["id"])
                    .eq("line_no", line_no)
                    .execute()
                )
                if existing.data:
                    client.table("order_line_items").update(
                        {"promised_date": promised}
                    ).eq("id", existing.data[0]["id"]).execute()
                else:
                    # No row yet (synthesised item for cross-page table POs)
                    client.table("order_line_items").insert({
                        "order_id": order["id"],
                        "line_no": line_no,
                        "promised_date": promised,
                        "required_delivery_date": li.get("required_delivery_date"),
                        "description": li.get("description"),
                    }).execute()

            print(f"  ✅ PO {po}: {len(items_with_pd)}/{len(line_items)} items got promised_date")
            updated += 1

        except Exception as e:
            print(f"  ❌ PO {po}: {e}")
            failed += 1
        finally:
            if tmp_path:
                try: os.unlink(tmp_path)
                except OSError: pass

    print(f"\nDone. Updated: {updated}  Skipped: {skipped}  Failed: {failed}")


if __name__ == "__main__":
    if DRY_RUN:
        print("DRY RUN — no DB writes\n")
    backfill()

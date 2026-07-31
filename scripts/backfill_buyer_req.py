"""
backfill_buyer_req.py — Re-extract buyer_name and req_number from all
existing PO PDFs and write them to the orders table.

Run once:  python scripts/backfill_buyer_req.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

from db import get_client
from pdf_extractor import extract_pdf_with_pdfplumber


def run():
    client = get_client()

    res = client.table("orders").select(
        "id, buyer_po_number, pdf_attachment_path"
    ).not_.is_("pdf_attachment_path", "null").execute()

    orders = res.data or []
    print(f"🔍 Found {len(orders)} orders with PDFs.\n")

    updated = skipped = errors = 0

    for o in orders:
        po = o["buyer_po_number"]
        pdf_path = o["pdf_attachment_path"]

        if not pdf_path or not Path(pdf_path).exists():
            print(f"  ⚠️  {po}: PDF not found — {pdf_path}")
            skipped += 1
            continue

        try:
            data = extract_pdf_with_pdfplumber(pdf_path)
            if "error" in data:
                print(f"  ❌ {po}: {data['error']}")
                errors += 1
                continue

            buyer_name = data.get("buyer_name")
            req_number = data.get("req_number")
            buyer_email = data.get("buyer_email")  # extracted but no DB column yet

            update = {}
            if buyer_name:
                update["buyer_name"] = buyer_name
            if req_number:
                update["req_number"] = req_number

            if not update:
                print(f"  —  {po}: nothing found")
                skipped += 1
                continue

            client.table("orders").update(update).eq("id", o["id"]).execute()

            parts = []
            if buyer_name:
                parts.append(f"buyer={buyer_name}")
                if buyer_email:
                    parts[-1] += f" <{buyer_email}>"
            if req_number:
                parts.append(f"req={req_number}")
            print(f"  ✅ {po}: {', '.join(parts)}")
            updated += 1

        except Exception as e:
            print(f"  ❌ {po}: {e}")
            errors += 1

    print(f"\nDone — {updated} updated, {skipped} skipped, {errors} errors.")


if __name__ == "__main__":
    run()

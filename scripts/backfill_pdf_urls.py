"""
backfill_pdf_urls.py — One-time fix for fallback-created orders missing pdf_url.

The original backfill_parked_routing.py saved PDFs to local disk but never
uploaded them to Supabase Storage, so pdf_url and ack_pdf_url are null.
This script finds those orders and uploads their PDFs retroactively.

Safe to re-run — skips orders that already have a pdf_url.
"""

import sys
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
load_dotenv()

from db import get_client
from storage import upload_pdf


def run() -> None:
    client = get_client()

    # Find orders that have a local PDF path but no cloud URL.
    # These are the fallback/backfill-created orders.
    orders = (
        client.table("orders")
        .select("id,buyer_po_number,pdf_attachment_path,pdf_url,ack_pdf_url")
        .is_("pdf_url", "null")
        .not_.is_("pdf_attachment_path", "null")
        .execute()
    ).data or []

    if not orders:
        print("No orders missing pdf_url — nothing to do.")
        return

    print(f"Found {len(orders)} order(s) with local PDF path but no cloud URL.\n")

    for order in orders:
        po      = order["buyer_po_number"]
        path    = order["pdf_attachment_path"]
        oid     = order["id"]

        print(f"─── PO {po} ───────────────────────────────")

        if not path or not Path(path).exists():
            print(f"  ⚠️  Local file not found: {path!r} — cannot upload")
            continue

        try:
            url = upload_pdf(path, "ack", po)
            if not url:
                print(f"  ❌ Upload returned None for {po}")
                continue
        except Exception as e:
            print(f"  ❌ Upload failed: {e}")
            continue

        client.table("orders").update({
            "pdf_url":     url,
            "ack_pdf_url": url,
        }).eq("id", oid).execute()
        print(f"  ✅ pdf_url + ack_pdf_url set: {url}")

    print("\nDone.")


if __name__ == "__main__":
    run()

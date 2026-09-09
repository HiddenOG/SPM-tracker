"""
backfill_nlng_line_items.py — One-shot backfill for NLNG orders that are
missing line items, pdf_url, or enquiry_number.

For every nlng_orders row that has a local pdf_attachment_path on disk:
  1. Re-parse the PDF with parse_nlng_po_pdf()
  2. Insert any missing nlng_order_line_items rows
  3. Upload the PDF to Supabase Storage and write pdf_url if still null
  4. Write contact_name, contact_email, enquiry_number if currently null

Safe to re-run: skips orders whose line items are already populated AND
whose pdf_url is already set (unless --force is passed).

Usage:
    python scripts/backfill_nlng_line_items.py            # live run
    python scripts/backfill_nlng_line_items.py --dry-run  # print only
    python scripts/backfill_nlng_line_items.py --force    # re-parse all
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
from nlng_pdf_parser import parse_nlng_po_pdf

DRY_RUN = "--dry-run" in sys.argv
FORCE   = "--force"   in sys.argv


def run():
    db = get_client()

    # Fetch all NLNG orders with a local PDF path
    res = db.table("nlng_orders").select(
        "id, po_number, variation_number, pdf_attachment_path, pdf_url, "
        "contact_name, contact_email, enquiry_number"
    ).not_.is_("pdf_attachment_path", "null").execute()

    rows = res.data or []
    if not rows:
        print("No NLNG orders with a local PDF path found.")
        return

    # Check which orders already have line items
    ids = [r["id"] for r in rows]
    li_res = db.table("nlng_order_line_items").select("nlng_order_id").in_(
        "nlng_order_id", ids
    ).execute()
    has_items = {li["nlng_order_id"] for li in (li_res.data or [])}

    done_items = done_upload = done_meta = 0

    for row in rows:
        order_id  = row["id"]
        po_number = row["po_number"]
        pdf_path  = Path(row["pdf_attachment_path"])

        needs_items  = FORCE or (order_id not in has_items)
        needs_upload = FORCE or not row.get("pdf_url")
        needs_meta   = FORCE or not row.get("contact_name") or not row.get("enquiry_number")

        if not (needs_items or needs_upload or needs_meta):
            continue

        if not pdf_path.exists():
            print(f"  ⚠️  {po_number}: PDF not on disk ({pdf_path}) — skipping")
            continue

        print(f"  {po_number} — parsing {pdf_path.name} …")

        try:
            fields = parse_nlng_po_pdf(pdf_path.read_bytes())
        except Exception as e:
            print(f"    ❌ parse error: {e}")
            continue

        if fields.get("_parse_error"):
            print(f"    ⚠️  parse warning: {fields['_parse_error']}")

        # ── 1. Line items ────────────────────────────────────────────────────
        if needs_items:
            items = fields.get("line_items", [])
            print(f"    items: {len(items)} found")
            if items and not DRY_RUN:
                db.table("nlng_order_line_items").delete().eq("nlng_order_id", order_id).execute()
                db.table("nlng_order_line_items").insert([{
                    "nlng_order_id": order_id,
                    "item_no":       item.get("item_no"),
                    "mesc_code":     item.get("mesc_code"),
                    "description":   item.get("description"),
                    "quantity":      item.get("quantity"),
                    "uom":           item.get("uom"),
                    "unit_price":    item.get("unit_price"),
                    "net_amount":    item.get("net_amount"),
                    "int_article_no": item.get("int_article_no"),
                    "delivery_date": item.get("delivery_date"),
                } for item in items]).execute()
                done_items += 1

        # ── 2. PDF upload ────────────────────────────────────────────────────
        if needs_upload:
            print(f"    uploading to storage …")
            if not DRY_RUN:
                try:
                    from storage import upload_pdf
                    url = upload_pdf(str(pdf_path), "nlng_po", po_number)
                    if url:
                        db.table("nlng_orders").update({"pdf_url": url}).eq("id", order_id).execute()
                        print(f"    ✅ pdf_url set")
                        done_upload += 1
                    else:
                        print(f"    ⚠️  upload returned no URL")
                except Exception as e:
                    print(f"    ❌ upload failed: {e}")

        # ── 3. Header metadata ───────────────────────────────────────────────
        if needs_meta:
            meta: dict = {}
            existing_enq = row.get("enquiry_number")
            bad_enq = not existing_enq or existing_enq == "0"
            if (not row.get("contact_name") or FORCE) and fields.get("contact_name"):
                meta["contact_name"] = fields["contact_name"]
            if (not row.get("contact_email") or FORCE) and fields.get("contact_email"):
                meta["contact_email"] = fields["contact_email"]
            if (bad_enq or FORCE) and fields.get("enquiry_number"):
                meta["enquiry_number"] = fields["enquiry_number"]
            if meta:
                print(f"    meta update: {list(meta.keys())}")
                if not DRY_RUN:
                    db.table("nlng_orders").update(meta).eq("id", order_id).execute()
                    done_meta += 1

    action = "[dry-run] would update" if DRY_RUN else "updated"
    print(f"\nDone. {action} {done_items} line-item sets, "
          f"{done_upload} PDF uploads, {done_meta} metadata patches.")


if __name__ == "__main__":
    run()

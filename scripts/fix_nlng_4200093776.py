"""
One-off: parse NLNG_4200093776.pdf, fix line items, and correct pdf_attachment_path in DB.
Run after placing the PDF at:  data/po_attachments/NLNG_4200093776.pdf
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

PO_NUMBER = "4200093776"
PDF_PATH  = ROOT / "data" / "po_attachments" / f"NLNG_{PO_NUMBER}.pdf"

if not PDF_PATH.exists():
    print(f"❌ PDF not found at {PDF_PATH}")
    print(f"   Copy NLNG_{PO_NUMBER}.pdf there and re-run.")
    sys.exit(1)

print(f"Parsing {PDF_PATH.name} …")
fields = parse_nlng_po_pdf(PDF_PATH.read_bytes())

if fields.get("_parse_error"):
    print(f"⚠️  Parse warning: {fields['_parse_error']}")

items = fields.get("line_items", [])
print(f"  {len(items)} line item(s) found:")
for it in items:
    print(f"    item {it['item_no']} | mesc={it['mesc_code']} | desc={it['description']!r} | qty={it['quantity']} | net={it['net_amount']}")

if not items:
    print("❌ No items parsed — check the PDF and parser before writing to DB.")
    sys.exit(1)

db = get_client()

# Look up the nlng_orders row
res = db.table("nlng_orders").select("id, pdf_attachment_path").eq("po_number", PO_NUMBER).execute()
rows = res.data or []
if not rows:
    print(f"❌ PO {PO_NUMBER} not found in nlng_orders.")
    sys.exit(1)

order = rows[0]
order_id = order["id"]
print(f"\nOrder ID: {order_id}")
print(f"Old pdf_attachment_path: {order['pdf_attachment_path']}")

# Fix line items
db.table("nlng_order_line_items").delete().eq("nlng_order_id", order_id).execute()
db.table("nlng_order_line_items").insert([{
    "nlng_order_id": order_id,
    "item_no":       it.get("item_no"),
    "mesc_code":     it.get("mesc_code"),
    "description":   it.get("description"),
    "quantity":      it.get("quantity"),
    "uom":           it.get("uom"),
    "unit_price":    it.get("unit_price"),
    "net_amount":    it.get("net_amount"),
    "int_article_no": it.get("int_article_no"),
    "delivery_date": it.get("delivery_date"),
} for it in items]).execute()
print(f"✅ Line items updated ({len(items)} rows)")

# Fix pdf_attachment_path to the current local path
db.table("nlng_orders").update({"pdf_attachment_path": str(PDF_PATH)}).eq("id", order_id).execute()
print(f"✅ pdf_attachment_path updated to: {PDF_PATH}")

# Patch any other parseable fields that might be null
patch = {k: v for k, v in {
    "document_date":          fields.get("document_date"),
    "required_delivery_date": fields.get("required_delivery_date"),
    "net_value":              fields.get("net_value"),
    "currency":               fields.get("currency"),
    "contact_name":           fields.get("contact_name"),
    "contact_email":          fields.get("contact_email"),
    "enquiry_number":         fields.get("enquiry_number"),
}.items() if v is not None}

if patch:
    db.table("nlng_orders").update(patch).eq("id", order_id).execute()
    print(f"✅ Metadata patched: {list(patch.keys())}")

print("\nDone.")

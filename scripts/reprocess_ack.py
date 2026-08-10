"""
reprocess_ack.py — Re-extract acknowledgment info from already-processed PDFs.

Use this whenever:
- acknowledged_at is still null on an order that should be acknowledged
- The extraction prompt has been updated and you want to re-run it
- New fields were added to the schema and you want to backfill them

Run with:  python scripts/reprocess_ack.py
"""

import os
import json
import base64
from pathlib import Path

from dotenv import load_dotenv
import anthropic

from db import get_client

load_dotenv()

ACK_EXTRACTION_PROMPT = """You are reading a Chevron purchase order PDF exported from \
the GEP SMART supplier portal. Extract ALL of the following fields exactly as they appear.

Respond with ONLY valid JSON, no other text, no markdown fences:
{
  "po_number": "<Purchase Order Number — digits only, e.g. 0061440972>",
  "status": "<Status field value, e.g. 'Partner Acknowledged'>",
  "order_submitted_on": "<Order Submitted on date — YYYY-MM-DD format>",
  "supplier_acknowledged_on": "<Supplier Acknowledged on date — YYYY-MM-DD format, THIS IS CRITICAL, look for exact label 'Supplier Acknowledged on'>",
  "payment_terms": "<Payment Terms field value>",
  "po_destination": "<PO Destination field value>",
  "transportation": "<Transportation field value>",
  "requestor_name": "<Requestor Name from Purchaser Information section>",
  "requestor_email": "<Requestor Email from Purchaser Information section>",
  "ship_to": "<Ship To address>",
  "line_items": [
    {
      "line_no": "<Line No.>",
      "description": "<full Description text>",
      "item_number": "<Item Number>",
      "supplier_item_number": "<Supplier Item Number>",
      "quantity": <number>,
      "uom": "<Unit of Measure>",
      "required_delivery_date": "<YYYY-MM-DD>",
      "unit_price": <number or null>,
      "promised_date": "<YYYY-MM-DD or null>",
      "total": <number or null>
    }
  ],
  "confidence": "<high or low>"
}

Critical instructions:
- 'supplier_acknowledged_on' is the date the supplier clicked acknowledge in GEP.
  It appears as a row labeled exactly 'Supplier Acknowledged on' in the order header table.
  Do NOT confuse this with 'Order Submitted on' which is a different field.
- If 'Supplier Acknowledged on' is not present at all, return null — this means the PDF
  is the original unacknowledged version, not the GEP export.
- Extract digits only for po_number, no prefix text.
- All dates must be YYYY-MM-DD format.
"""


def reprocess_pending_acknowledgments():
    client_db = get_client()
    client_ai = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Target any order that is missing its acknowledged_at date but has a
    # saved PDF — NOT just status='pending'. The earlier listener bug flipped
    # orders to status='acknowledged' while leaving acknowledged_at null, so
    # filtering on 'pending' alone would skip exactly the broken orders.
    result = (
        client_db.table("orders")
        .select("*")
        .is_("acknowledged_at", "null")
        .not_.is_("pdf_attachment_path", "null")
        .execute()
    )

    orders = result.data

    if not orders:
        print("No orders pending acknowledgment with a saved PDF.")
        return

    print(f"Found {len(orders)} order(s) to reprocess...\n")

    for order in orders:
        po_number = order["buyer_po_number"]
        pdf_path = order["pdf_attachment_path"]

        if not Path(pdf_path).exists():
            print(f"⚠️  PO {po_number}: PDF not found at {pdf_path}")
            print(f"   Download the acknowledged PDF from Gmail and save it there, then rerun.")
            continue

        print(f"Re-extracting from PO {po_number}...")

        try:
            pdf_bytes = Path(pdf_path).read_bytes()
            pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

            message = client_ai.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2000,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": "application/pdf",
                                    "data": pdf_b64,
                                },
                            },
                            {"type": "text", "text": ACK_EXTRACTION_PROMPT},
                        ],
                    }
                ],
            )

            response_text = message.content[0].text.strip()
            if response_text.startswith("```"):
                response_text = response_text.split("```")[1]
                if response_text.startswith("json"):
                    response_text = response_text[4:]

            extracted = json.loads(response_text.strip())
            ack_date = extracted.get("supplier_acknowledged_on")

            if ack_date:
                # Update the order with all the newly extracted fields
                client_db.table("orders").update(
                    {
                        "acknowledgment_status": "acknowledged",
                        "acknowledged_at": ack_date,
                        "acknowledged_by": "GEP export (reprocessed)",
                        "overall_status": "awaiting_warehouse_stock_check",
                        "extraction_raw": extracted,
                        # Store the extra fields we now extract
                        "extracted_description": extracted.get("status"),
                    }
                ).eq("id", order["id"]).execute()

                # Update line items with the richer data we now have
                line_items = extracted.get("line_items", [])
                for item in line_items:
                    client_db.table("order_line_items").update(
                        {
                            "supplier_part_code": item.get("supplier_item_number"),
                            "unit_price": item.get("unit_price"),
                            "line_total": item.get("total"),
                        }
                    ).eq("order_id", order["id"]).execute()

                print(f"✅ PO {po_number}: acknowledged on {ack_date}")
                print(f"   Payment terms: {extracted.get('payment_terms')}")
                print(f"   Destination: {extracted.get('po_destination')}")
                print(f"   {len(line_items)} line item(s) updated with pricing")

            else:
                print(f"⚠️  PO {po_number}: 'Supplier Acknowledged on' field not found.")
                print(f"   This PDF is likely the original Chevron PO, not the GEP export.")
                print(f"   Download the acknowledged version from Gmail and replace the PDF at:")
                print(f"   {pdf_path}")

        except Exception as e:
            print(f"❌ PO {po_number}: reprocessing failed — {e}")


if __name__ == "__main__":
    reprocess_pending_acknowledgments()
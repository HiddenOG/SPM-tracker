"""
pricing.py — Stage 4: Look up prices for each line item.

What this does:
1. Finds line items that don't have a price yet.
2. Tries to match the part code against the `price_list` table.
3. If found: fills in the price, marks price_source = 'price_list'.
4. If not found: flags the order as needing a quotation from the
   supplier, marks price_source = 'quotation_requested', and prints
   a reminder so a human can email the supplier.

Run this with:  python scripts/pricing.py

Note: This script does NOT email the supplier for you — sending a
quotation request is still a human action for now. It just makes
sure nothing falls through the cracks by flagging it clearly.
"""

from datetime import datetime, timezone

from db import get_client


def get_unpriced_line_items() -> list[dict]:
    """Find line items with no unit_price set yet."""
    client = get_client()
    result = (
        client.table("order_line_items")
        .select("*, orders(buyer_po_number, product_line, supplier_id)")
        .is_("unit_price", "null")
        .execute()
    )
    return result.data


def find_price_in_list(part_code: str) -> dict | None:
    """Look up a part code in the digitized price_list table."""
    if not part_code:
        return None

    client = get_client()
    result = (
        client.table("price_list")
        .select("*")
        .eq("part_code", part_code)
        .execute()
    )
    return result.data[0] if result.data else None


def run_pricing_pass() -> None:
    """Process all line items waiting for a price."""
    line_items = get_unpriced_line_items()

    if not line_items:
        print("No line items waiting for pricing.")
        return

    print(f"Found {len(line_items)} line item(s) needing a price...\n")

    client = get_client()
    needs_quotation = []

    for item in line_items:
        part_code = item.get("buyer_part_code")
        order_info = item.get("orders") or {}
        po_number = order_info.get("buyer_po_number", "unknown")

        match = find_price_in_list(part_code)

        if match:
            line_total = None
            if item.get("quantity") and match.get("unit_price"):
                line_total = float(item["quantity"]) * float(match["unit_price"])

            client.table("order_line_items").update(
                {
                    "unit_price": match["unit_price"],
                    "line_total": line_total,
                }
            ).eq("id", item["id"]).execute()

            print(f"✅ PO {po_number} — part '{part_code}': "
                  f"matched at ${match['unit_price']} from price list")
        else:
            needs_quotation.append((po_number, part_code))

            client.table("orders").update(
                {
                    "price_source": "quotation_requested",
                    "quotation_requested_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("buyer_po_number", po_number).execute()

            print(f"⚠️  PO {po_number} — part '{part_code}': "
                  f"NOT found in price list. Quotation needed from supplier.")

    if needs_quotation:
        print(f"\n📋 {len(needs_quotation)} part(s) need a manual quotation request "
              f"to the supplier. See flagged orders above.")


if __name__ == "__main__":
    run_pricing_pass()

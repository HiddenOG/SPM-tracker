"""
spm_po_draft.py — Stage 5: Draft the SPM purchase order to send to the supplier.

What this does:
1. Finds orders that are acknowledged AND fully priced, but don't
   have an SPM PO drafted yet.
2. Auto-generates an internal SPM PO number.
3. Builds a plain-text draft (PO details, line items, prices) that
   a human reviews and sends manually to the supplier (e.g. Flexitallic).
4. Once you've sent it, run `mark_sent()` to stamp T2 — the moment
   the order officially left SPM's hands and went to the supplier.

Run this with:  python scripts/spm_po_draft.py draft
Then, after you've reviewed and sent it:
    python scripts/spm_po_draft.py sent <buyer_po_number>

This currently outputs plain text drafts to the terminal/file.
Once this is working well, this is the natural place to upgrade to
a formatted PDF or auto-send email — but starting simple on purpose.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

from db import get_client

DRAFTS_DIR = Path(__file__).parent.parent / "data" / "spm_po_drafts"
DRAFTS_DIR.mkdir(parents=True, exist_ok=True)


def generate_spm_po_number(buyer_po_number: str) -> str:
    """
    Simple sequential-ish PO number generator.
    Adjust this to match whatever numbering convention SPM actually uses
    (e.g. 'S.P.M - 2071' style seen in the real records).
    """
    client = get_client()
    result = client.table("orders").select("spm_po_number").not_.is_(
        "spm_po_number", "null"
    ).execute()

    existing_numbers = []
    for row in result.data:
        spm_po = row.get("spm_po_number", "")
        digits = "".join(filter(str.isdigit, spm_po))
        if digits:
            existing_numbers.append(int(digits))

    next_number = max(existing_numbers, default=2070) + 1
    return f"S.P.M - {next_number}"


def get_orders_ready_for_po_draft() -> list[dict]:
    """
    Find orders that are acknowledged, have all line items priced,
    and don't have an SPM PO number yet.
    """
    client = get_client()
    result = (
        client.table("orders")
        .select("*, order_line_items(*)")
        .eq("acknowledgment_status", "acknowledged")
        .is_("spm_po_number", "null")
        .execute()
    )

    ready = []
    for order in result.data:
        line_items = order.get("order_line_items", [])
        if line_items and all(item.get("unit_price") is not None for item in line_items):
            ready.append(order)

    return ready


def build_draft_text(order: dict, spm_po_number: str) -> str:
    """Build a plain-text PO draft ready for human review."""
    line_items = order.get("order_line_items", [])

    lines = [
        f"SPM PURCHASE ORDER — DRAFT (review before sending)",
        f"=" * 60,
        f"SPM PO Number: {spm_po_number}",
        f"Reference Chevron PO: {order['buyer_po_number']}",
        f"Required Delivery Date: {order.get('required_delivery_date') or 'NOT SET — check PO PDF'}",
        f"Product Line: {order.get('product_line') or 'unclassified'}",
        f"",
        f"LINE ITEMS:",
        f"-" * 60,
    ]

    total = 0
    for item in line_items:
        qty = item.get("quantity") or 0
        price = item.get("unit_price") or 0
        line_total = qty * price
        total += line_total
        lines.append(
            f"  Part: {item.get('buyer_part_code')}\n"
            f"  Description: {item.get('description')}\n"
            f"  Qty: {qty}  x  ${price}  =  ${line_total:,.2f}\n"
        )

    lines.append(f"-" * 60)
    lines.append(f"TOTAL: ${total:,.2f}")
    lines.append(f"")
    lines.append(f"⚠️  REVIEW BEFORE SENDING — this is a DRAFT only.")

    return "\n".join(lines)


def run_draft_pass() -> None:
    """Generate drafts for all orders ready for an SPM PO."""
    orders = get_orders_ready_for_po_draft()

    if not orders:
        print("No orders ready for SPM PO drafting (need: acknowledged + all line items priced).")
        return

    print(f"Found {len(orders)} order(s) ready for SPM PO draft...\n")

    client = get_client()

    for order in orders:
        spm_po_number = generate_spm_po_number(order["buyer_po_number"])
        draft_text = build_draft_text(order, spm_po_number)

        draft_path = DRAFTS_DIR / f"{spm_po_number.replace(' ', '_').replace('.', '')}.txt"
        draft_path.write_text(draft_text)

        client.table("orders").update(
            {
                "spm_po_number": spm_po_number,
                "spm_po_drafted_at": datetime.now(timezone.utc).isoformat(),
                "overall_status": "po_drafted",
            }
        ).eq("id", order["id"]).execute()

        print(f"📄 Draft created: {spm_po_number} (ref Chevron PO {order['buyer_po_number']})")
        print(f"   Saved to: {draft_path}")
        print(f"   Review it, then run:")
        print(f"   python scripts/spm_po_draft.py sent {order['buyer_po_number']}\n")


def mark_sent(buyer_po_number: str) -> None:
    """Mark the SPM PO as sent to the supplier — stamps T2."""
    client = get_client()
    result = (
        client.table("orders")
        .update(
            {
                "spm_po_sent_at": datetime.now(timezone.utc).isoformat(),
                "overall_status": "po_sent",
            }
        )
        .eq("buyer_po_number", buyer_po_number)
        .execute()
    )

    if result.data:
        print(f"✅ SPM PO for {buyer_po_number} marked as sent. T2 timestamp recorded.")
    else:
        print(f"⚠️  No order found with buyer PO number {buyer_po_number}.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]

    if command == "draft":
        run_draft_pass()
    elif command == "sent":
        if len(sys.argv) < 3:
            print("Usage: python scripts/spm_po_draft.py sent <buyer_po_number>")
            sys.exit(1)
        mark_sent(sys.argv[2])
    else:
        print(f"Unknown command '{command}'. Use 'draft' or 'sent'.")

"""
acknowledge.py — Stage 3: Track the manual acknowledgment step.

The acknowledgment itself happens by a human clicking the button
inside the actual Chevron email — this script does NOT and CANNOT
do that for you. What it does instead:

1. Lets you list every PO currently waiting on acknowledgment,
   sorted by how long it's been waiting (the alert view).
2. Lets you mark a PO as acknowledged once you've clicked the real
   button in your email, which stamps the T1 timestamp.

Usage:
    python scripts/acknowledge.py list
    python scripts/acknowledge.py mark <buyer_po_number> "<your name>"

Example:
    python scripts/acknowledge.py list
    python scripts/acknowledge.py mark 0061440972 "Simeon Moju"
"""

import sys
from datetime import datetime, timezone

from db import get_client


def list_pending() -> None:
    """Show every order still waiting on acknowledgment, oldest first."""
    client = get_client()
    result = (
        client.table("orders")
        .select("buyer_po_number, po_amount, notification_received_at, product_line")
        .eq("acknowledgment_status", "pending")
        .order("notification_received_at", desc=False)
        .execute()
    )

    if not result.data:
        print("✅ Nothing pending acknowledgment. All clear.")
        return

    print(f"\n{'PO Number':<16} {'Amount':<12} {'Waiting since':<22} {'Hours waiting':<14} Product")
    print("-" * 90)

    now = datetime.now(timezone.utc)
    for row in result.data:
        received = row.get("notification_received_at")
        hours_waiting = "?"
        if received:
            received_dt = datetime.fromisoformat(received.replace("Z", "+00:00"))
            hours_waiting = round((now - received_dt).total_seconds() / 3600, 1)

        flag = "🔴" if isinstance(hours_waiting, (int, float)) and hours_waiting > 24 else "🟡"

        print(
            f"{flag} {row['buyer_po_number']:<14} "
            f"${row.get('po_amount', 0):<11} "
            f"{str(received)[:19]:<22} "
            f"{hours_waiting:<14} "
            f"{row.get('product_line') or 'not yet classified'}"
        )

    print("\n🔴 = waiting over 24 hours — needs attention\n")


def mark_acknowledged(po_number: str, staff_name: str) -> None:
    """Mark a PO as acknowledged — call this AFTER clicking the real button in email."""
    client = get_client()

    result = (
        client.table("orders")
        .update(
            {
                "acknowledgment_status": "acknowledged",
                "acknowledged_at": datetime.now(timezone.utc).isoformat(),
                "acknowledged_by": staff_name,
                "overall_status": "acknowledged",
            }
        )
        .eq("buyer_po_number", po_number)
        .execute()
    )

    if result.data:
        print(f"✅ PO {po_number} marked as acknowledged by {staff_name}.")
    else:
        print(f"⚠️  No order found with PO number {po_number}. Check the number and try again.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]

    if command == "list":
        list_pending()
    elif command == "mark":
        if len(sys.argv) < 4:
            print('Usage: python scripts/acknowledge.py mark <buyer_po_number> "<your name>"')
            sys.exit(1)
        mark_acknowledged(sys.argv[2], sys.argv[3])
    else:
        print(f"Unknown command '{command}'. Use 'list' or 'mark'.")

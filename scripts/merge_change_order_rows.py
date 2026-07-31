"""
merge_change_order_rows.py — one-time migration.

Finds every orders row whose buyer_po_number has a -NNN revision suffix
(e.g. "0061340768-001"), merges its data into the base row
("0061340768"), then deletes the revision row.

Merge rules
-----------
Milestone timestamps : revision value only fills in when the base is null.
                       When both are set, keep the earlier one.
Other fields         : revision value wins (it is the most recent notification)
                       unless base already has a non-null value for fields
                       that should only be set once (notification_received_at,
                       pdf_attachment_path).
overall_status       : re-derived after the merge via sync.derive_status().

Run once:
    python scripts/merge_change_order_rows.py
"""

import sys
import re
sys.path.insert(0, r"c:\Users\Godson\spm-tracker\scripts")
from dotenv import load_dotenv
load_dotenv(r"c:\Users\Godson\spm-tracker\.env")

from db import get_client
import sync

# Timestamp columns — merge rule: earlier non-null wins.
TIMESTAMP_COLS = [
    "notification_received_at",
    "acknowledged_at",
    "sent_to_warehouse_at",
    "stock_check_completed_at",
    "spm_po_sent_at",
    "so_received_at",
    "promised_date",
    "flex_dispatch_ready_at",
    "dispatch_instructions_sent_at",
    "so_sent_to_warehouse_at",
    "ready_for_dispatch_at",
    "dispatched_at",
    "delivery_requested_at",
    "delivered_at",
]

# Other copyable fields — revision wins unless base already has a value
# (except create-only fields below).
PREFER_REVISION_COLS = [
    "jde_job_id", "branch_plant", "supplier_ref_number",
    "po_amount", "po_currency",
    "so_number",
    "warehouse_routing_raw", "stock_check_raw",
    "acknowledgment_status", "acknowledged_by",
]

# Create-only: only copy if base has null
CREATE_ONLY_COLS = [
    "notification_received_at",  # also in TIMESTAMP_COLS; listed here for clarity
    "pdf_attachment_path",
    "pdf_url",
]

REVISION_RE = re.compile(r"^(\d{8,12})-\d{3}$")


def pick_earlier(a, b):
    """Return the earlier of two ISO timestamp strings, ignoring nulls."""
    if not a:
        return b
    if not b:
        return a
    return a if a < b else b


def merge(db, base_row: dict, rev_row: dict) -> dict:
    """Build the update dict to apply to the base row."""
    update = {}

    for col in TIMESTAMP_COLS:
        base_val = base_row.get(col)
        rev_val  = rev_row.get(col)
        merged   = pick_earlier(base_val, rev_val)
        if merged and merged != base_val:
            update[col] = merged

    for col in PREFER_REVISION_COLS:
        if col in TIMESTAMP_COLS:
            continue  # already handled above
        rev_val  = rev_row.get(col)
        base_val = base_row.get(col)
        if rev_val is not None and base_val is None:
            update[col] = rev_val

    for col in CREATE_ONLY_COLS:
        if col in TIMESTAMP_COLS:
            continue
        if base_row.get(col) is None and rev_row.get(col) is not None:
            update[col] = rev_row[col]

    return update


def run():
    db = get_client()

    # Fetch all orders that look like revision rows.
    all_orders = db.table("orders").select("*").execute().data or []
    revision_rows = [r for r in all_orders if REVISION_RE.match(r.get("buyer_po_number", ""))]

    if not revision_rows:
        print("No revision rows found. Nothing to do.")
        return

    print(f"Found {len(revision_rows)} revision row(s) to merge:\n")

    for rev in revision_rows:
        rev_po  = rev["buyer_po_number"]             # e.g. "0061340768-001"
        base_po = REVISION_RE.match(rev_po).group(1) # e.g. "0061340768"

        print(f"  {rev_po} → merging into {base_po}")

        base_res = db.table("orders").select("*").eq("buyer_po_number", base_po).execute()

        if not base_res.data:
            # No base row exists — rename the revision row to the base number.
            print(f"    ⚠  No base row for {base_po} — renaming {rev_po} to {base_po}.")
            db.table("orders").update({"buyer_po_number": base_po}).eq("id", rev["id"]).execute()
            continue

        base = base_res.data[0]
        update = merge(db, base, rev)

        if update:
            # Re-derive status after applying the merged timestamps.
            merged_state = {**base, **update}
            update["overall_status"] = sync.derive_status(merged_state)
            db.table("orders").update(update).eq("id", base["id"]).execute()
            print(f"    ✅ Applied {len(update)} field(s): {list(update.keys())}")
        else:
            print(f"    — Base row already has all data; no fields to copy.")

        # Re-point any processed_emails rows that reference the revision row
        # to the base row, so the FK constraint allows deletion.
        db.table("processed_emails").update({"matched_order_id": base["id"]}) \
            .eq("matched_order_id", rev["id"]).execute()

        # Delete the revision row.
        db.table("orders").delete().eq("id", rev["id"]).execute()
        print(f"    🗑  Deleted revision row {rev_po} (id={rev['id']}).")

        # Normalise any parked emails that were indexed under the old suffix key.
        parked = db.table("parked_emails").select("id,po_number").like("po_number", f"{rev_po}%").execute().data or []
        if parked:
            for p in parked:
                db.table("parked_emails").update({"po_number": base_po}).eq("id", p["id"]).execute()
            print(f"    📬 Re-keyed {len(parked)} parked email(s) from {rev_po} → {base_po}.")

    print("\nDone.")


if __name__ == "__main__":
    run()

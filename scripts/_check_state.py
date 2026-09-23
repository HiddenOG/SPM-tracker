"""Quick DB state check — shows warehouse emails and orders missing key timestamps."""
import sys
sys.path.insert(0, 'scripts')
from db import get_client

db = get_client()

print("\n=== WAREHOUSE PROCESSED EMAILS ===")
res = db.table('processed_emails') \
    .select('message_id,subject,processing_result,raw_notes,received_at') \
    .ilike('sender', '%spmwarehouse22%') \
    .order('received_at', desc=False) \
    .execute()
for r in res.data:
    print(f"  [{r.get('processing_result','?'):<22}] {r.get('subject','')[:80]}")

print("\n=== ORDERS MISSING so_number (have SO ack) ===")
res2 = db.table('orders') \
    .select('buyer_po_number,so_number,overall_status,so_received_at') \
    .is_('so_number', 'null') \
    .not_.is_('so_received_at', 'null') \
    .execute()
for r in res2.data:
    print(f"  PO {r['buyer_po_number']}  status={r['overall_status']}")

print("\n=== ORDERS WITH delivery_requested_at ===")
res3 = db.table('orders') \
    .select('buyer_po_number,overall_status,delivery_requested_at') \
    .not_.is_('delivery_requested_at', 'null') \
    .execute()
if res3.data:
    for r in res3.data:
        print(f"  PO {r['buyer_po_number']}  status={r['overall_status']}")
else:
    print("  (none — column exists but no rows stamped yet)")

print("\n=== STATUS DISTRIBUTION ===")
res4 = db.table('orders').select('overall_status').execute()
from collections import Counter
counts = Counter(r['overall_status'] for r in res4.data)
for status, n in sorted(counts.items(), key=lambda x: -x[1]):
    print(f"  {n:3d}  {status}")

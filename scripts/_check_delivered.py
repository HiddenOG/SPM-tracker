import sys; sys.stdout.reconfigure(encoding="utf-8"); sys.path.insert(0,"scripts")
from db import get_client
db = get_client()
res = db.table("orders").select("buyer_po_number,overall_status,stock_check_completed_at,stock_check_raw").execute()
delivered = [r for r in res.data if isinstance(r.get("stock_check_raw"), dict) and r["stock_check_raw"].get("overall_availability") == "fully_delivered"]
print(f"{len(delivered)} orders with fully_delivered in stock_check_raw:")
for r in delivered:
    print(f"  {r['buyer_po_number']}  status={r['overall_status']}  ts={str(r.get('stock_check_completed_at',''))[:16]}")

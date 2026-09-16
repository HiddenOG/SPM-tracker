"""
convert_client_po_to_usd.py - put every stored client PO value in USD.

A handful of client POs are priced in naira or sterling. Left as-is they read
as enormous next to the dollar figures (one naira PO is 8,207,024) and any
naive sum is wrong by millions. Everything is converted here, at the same live
rate the dashboard uses, so the stored table is single-currency.

The original amount is kept in the description so a converted figure can
always be traced back and re-checked against a different rate.

    python scripts/convert_client_po_to_usd.py            # dry run
    python scripts/convert_client_po_to_usd.py --apply
"""

import sys
import json
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

load_dotenv()

from db import get_client

FALLBACK = {"NGN": 1600.0, "GBP": 0.79}


def live_rates() -> dict:
    """Same source as the dashboard, same fallback if it is unreachable."""
    rates = dict(FALLBACK)
    try:
        with urllib.request.urlopen("https://open.er-api.com/v6/latest/USD", timeout=20) as resp:
            data = json.load(resp)
        for cur in rates:
            if data.get("rates", {}).get(cur):
                rates[cur] = float(data["rates"][cur])
        print(f"live rate: 1 USD = {rates['NGN']:,.2f} NGN / {rates['GBP']:.4f} GBP")
    except Exception as exc:
        print(f"rate fetch failed ({str(exc)[:50]}) - using fallback {rates}")
    return rates


def main() -> None:
    apply = "--apply" in sys.argv
    rates = live_rates()
    client = get_client()

    rows = client.table("scratch_client_po_values").select("*").execute().data or []
    todo = [r for r in rows if (r.get("currency") or "USD") != "USD" and r.get("po_value") is not None]
    print(f"\n{len(rows)} stored, {len(todo)} not in USD\n")
    if not todo:
        print("nothing to convert")
        return

    for r in todo:
        cur = r["currency"]
        rate = rates.get(cur)
        if not rate:
            print(f"   {r['po_number']}: no rate for {cur} - left alone")
            continue
        original = float(r["po_value"])
        converted = original / rate
        note = f"[converted from {cur} {original:,.2f} at {rate:,.4f}] "
        desc = (note + (r.get("description") or ""))[:400]
        print(f"   {r['po_number']}  {cur} {original:>14,.2f}  ->  USD {converted:>12,.2f}")
        if apply:
            client.table("scratch_client_po_values").update({
                "po_value": round(converted, 2),
                "currency": "USD",
                "description": desc,
            }).eq("po_number", r["po_number"]).execute()

    if apply:
        after = client.table("scratch_client_po_values").select("po_value,currency").execute().data or []
        total = sum(float(x["po_value"]) for x in after if x.get("po_value") is not None)
        non_usd = [x for x in after if (x.get("currency") or "USD") != "USD"]
        print(f"\nconverted. {len(after)} rows, {len(non_usd)} still non-USD")
        print(f"client PO total now: ${total:,.2f}")
    else:
        print("\n(dry run - add --apply to write)")


if __name__ == "__main__":
    main()

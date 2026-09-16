"""
backfill_client_po_values.py - what the CLIENT paid SPM.

The Flexitallic history answers "what did SPM pay Flexitallic". The comparison
the business actually wants is against what Chevron, NLNG and Mobil paid SPM,
and that figure is not in that table at all - only the client's PO NUMBER is.

Two sources, in order of trust:

  1. The live `orders` table, where po_amount was already extracted. Covers
     roughly 2026 onward.
  2. The PO PDF attached to the email forwarding that PO to the warehouse.
     SPM has forwarded every one since 2022, so this reaches back through the
     whole period. Read with the app's existing extract_pdf_with_pdfplumber,
     which falls back to OCR on scanned PDFs by itself.

Values are keyed by CLIENT PO NUMBER, not by sales order. One SPM purchase
order often bundles several client POs (60 of 213 rows), so anything that
compares the two has to sum the client side per SPM PO first - comparing a
single client PO against a whole bundled cost reads as a 1400% loss.

    python scripts/backfill_client_po_values.py                # dry run
    python scripts/backfill_client_po_values.py --apply
    python scripts/backfill_client_po_values.py --apply --limit=20
"""

import os
import re
import sys
import json
import email as email_mod
import tempfile
import time
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

load_dotenv()

from db import get_client
from email_utils import decode_mime_words
from config import SPM_SENDER, WAREHOUSE_EMAIL
from supplier_po_parser import connect_to_gmail
import pdfplumber
from pdf_extractor import extract_pdf_with_pdfplumber

# Only used if the live rate service is down. Keep these near current market
# rates - they sat at N1,600 / GBP 0.79 long after the market moved to ~N1,327
# / GBP 0.74, which would have skewed every conversion made during an outage.
_FX_FALLBACK = {"NGN": 1330.0, "GBP": 0.74, "EUR": 0.92}
_FX_RATES = None


def _fx_rates() -> dict:
    """Live USD rates, same source the dashboard uses.

    Values are converted the moment they are read, not in a separate pass
    afterwards. A single unconverted naira PO (7,121,834.52) once sat in the
    table reading as $7.1M and inflated the client total more than threefold,
    because the conversion step was a separate script that had not been run
    since the last backfill.
    """
    global _FX_RATES
    if _FX_RATES is not None:
        return _FX_RATES
    rates = dict(_FX_FALLBACK)
    try:
        import urllib.request
        with urllib.request.urlopen("https://open.er-api.com/v6/latest/USD", timeout=20) as resp:
            data = json.load(resp)
        for cur in rates:
            if data.get("rates", {}).get(cur):
                rates[cur] = float(data["rates"][cur])
        print(f"  rate: 1 USD = {rates['NGN']:,.2f} NGN / {rates['GBP']:.4f} GBP")
    except Exception as exc:
        print(f"  [warn] rate fetch failed ({str(exc)[:40]}) - using fallback")
    _FX_RATES = rates
    return rates


def to_usd(amount, currency: str | None, on=None) -> tuple[float, str]:
    """Return (usd_amount, note) for a value in any supported currency.

    Pass `on` (the PO date) to convert at the rate on that day. This history
    spans three years and the naira moved 13-20% over it, so today's rate
    misstates older POs. Falls back to today's rate - and says so in the note -
    only when no rate for that day can be found.
    """
    cur = (currency or "USD").upper()
    value = float(amount or 0)
    if cur == "USD":
        return round(value, 2), ""
    if on:
        from fx_history import rate_on
        dated = rate_on(cur, on)
        if dated:
            return (round(value / dated, 2),
                    f"[converted from {cur} {value:,.2f} at {dated:,.4f} on {str(on)[:10]}] ")
    rate = _fx_rates().get(cur)
    if not rate:
        return round(value, 2), f"[UNCONVERTED {cur}] "
    return round(value / rate, 2), f"[converted from {cur} {value:,.2f} at {rate:,.4f}, today's rate] "


CLIENT_OF = [("CHEVRON", "006"), ("NLNG", "4200"), ("MOBIL", "4501")]


def _client_for(po: str) -> str | None:
    for name, prefix in CLIENT_OF:
        if po.startswith(prefix):
            return name
    return None


def collect_wanted() -> dict:
    """Every client PO number referenced by the Flexitallic history."""
    client = get_client()
    rows = client.table("scratch_flexitallic_history") \
        .select("customer_po_numbers,so_date").execute().data or []
    wanted: dict[str, str] = {}
    for row in rows:
        nums = row.get("customer_po_numbers") or []
        if isinstance(nums, str):
            try:
                nums = json.loads(nums)
            except Exception:
                nums = []
        for num in nums:
            num = str(num)
            if _client_for(num) and num not in wanted:
                wanted[num] = row.get("so_date") or ""
    return wanted


def from_orders_table(po_numbers: list) -> dict:
    """Values already extracted by the live pipeline."""
    client = get_client()
    out = {}
    for i in range(0, len(po_numbers), 50):
        chunk = po_numbers[i:i + 50]
        rows = (client.table("orders")
                .select("buyer_po_number,po_amount,po_currency,order_submitted_on,"
                        "extracted_description,req_number,pdf_url")
                .not_.is_("po_amount", "null")
                .in_("buyer_po_number", chunk).execute().data or [])
        for r in rows:
            out[r["buyer_po_number"]] = {
                "po_value": float(r["po_amount"]),
                "currency": r.get("po_currency") or "USD",
                "po_date": r.get("order_submitted_on"),
                "description": (r.get("extracted_description") or "")[:400],
                "req_number": r.get("req_number"),
                "pdf_url": r.get("pdf_url"),
                "source": "orders",
            }
    return out


# ExxonMobil purchase orders end with a single unambiguous line:
#   "Total net value excl. tax USD 143.62"
MOBIL_TOTAL_RE = re.compile(
    r"Total\s+net\s+value[^\n]*?\b([A-Z]{3})\s+([\d,]+\.\d{2})", re.IGNORECASE)


SPM_OWN_PO_RE = re.compile(r"ITEM\s*No\.?\s*P\.?O\.?\s*No\.?\s*QUOTE\s*REF", re.IGNORECASE)


def _is_spm_own_po(text: str) -> bool:
    """True if this PDF is SPM's purchase order TO Flexitallic.

    Those name the client's PO number too, so a widened search surfaces them.
    Reading one would file SPM's cost as the client's spend - the exact
    confusion this table exists to separate.
    """
    return bool(SPM_OWN_PO_RE.search(text or ""))


def extract_mobil_po(path: str) -> dict | None:
    """Read an ExxonMobil purchase order PDF."""
    try:
        with pdfplumber.open(path) as pdf:
            text = "\n".join(pg.extract_text() or "" for pg in pdf.pages)
    except Exception:
        return None
    if _is_spm_own_po(text):
        return None
    m = MOBIL_TOTAL_RE.search(text)
    if not m:
        return None
    return {"po_value": float(m.group(2).replace(",", "")),
            "currency": m.group(1).upper(), "source": "routing_pdf",
            "_text": text}


def extract_nlng_po(path: str) -> dict | None:
    """Read an NLNG purchase order PDF with the parser the NLNG pipeline uses."""
    try:
        from nlng_pdf_parser import parse_nlng_po_pdf
        with open(path, "rb") as fh:
            data = parse_nlng_po_pdf(fh.read())
    except Exception:
        return None
    if not data or data.get("net_value") is None:
        return None
    return {"po_value": float(data["net_value"]),
            "currency": data.get("currency") or "USD",
            "description": (data.get("delivery_address") or "")[:200],
            "source": "routing_pdf"}


def extract_chevron_po(path: str) -> dict | None:
    """Read a Chevron GEP purchase order PDF (falls back to OCR internally)."""
    try:
        with pdfplumber.open(path) as pdf:
            raw_text = "\n".join(pg.extract_text() or "" for pg in pdf.pages)
        if _is_spm_own_po(raw_text):
            return None
        data = extract_pdf_with_pdfplumber(path)
    except Exception:
        return None
    if not data or data.get("net_total") is None:
        return None
    return {"po_value": float(data["net_total"]),
            "currency": data.get("currency") or "USD",
            "description": (data.get("description") or "")[:400],
            "req_number": data.get("req_number"),
            "buyer_po_number": data.get("buyer_po_number"),
            "source": "routing_pdf"}


def extract_for(po_number: str, path: str) -> dict | None:
    """Pick the reader that matches whose purchase order this is.

    The three clients issue completely different documents, so one parser
    cannot serve them. Reading a Chevron GEP layout was returning nothing at
    all for the 40 NLNG and 33 Mobil purchase orders.
    """
    if po_number.startswith("4501"):
        return extract_mobil_po(path)
    if po_number.startswith("4200"):
        return extract_nlng_po(path)
    return extract_chevron_po(path)


def _pdfs(msg):
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        payload = part.get_payload(decode=True)
        if payload and payload[:4] == b"%PDF":
            yield payload


def _reconnect(tries: int = 5):
    """Open a Gmail session, waiting longer after each failure.

    This job makes one search per client PO, and Gmail drops or refuses the
    connection partway through. An earlier version reconnected once with no
    backoff; when that immediate retry was also refused the exception escaped
    the handler and killed the run.
    """
    delay = 3
    for attempt in range(tries):
        try:
            imap = connect_to_gmail()
            imap.select_folder("[Gmail]/All Mail", readonly=True)
            return imap
        except Exception as exc:
            if attempt == tries - 1:
                raise
            print(f"  [warn] reconnect failed ({str(exc)[:50]}) - retrying in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)
    return None


def _safe_search(imap, query, tries: int = 3):
    """Search, rebuilding the session if it dies. Returns (uids, imap)."""
    for attempt in range(tries):
        try:
            return imap.search(query), imap
        except Exception as exc:
            if attempt == tries - 1:
                print(f"  [warn] search gave up: {str(exc)[:60]}")
                return [], imap
            try:
                imap.logout()
            except Exception:
                pass
            time.sleep(2)
            try:
                imap = _reconnect()
            except Exception:
                return [], imap
    return [], imap


def from_routing_emails(po_numbers: list, tmpdir: str, save=None) -> dict:
    """Read each client PO's value off the PDF SPM forwarded to the warehouse."""
    imap = _reconnect()
    out: dict[str, dict] = {}
    try:
        for idx, po in enumerate(po_numbers, 1):
            if po.startswith("006"):
                # Warehouse routing first - that forward is the reliable
                # carrier of a Chevron PO PDF - then the whole mailbox,
                # because seven POs turned out to have their document in an
                # ordinary thread and were being missed entirely.
                queries = [["FROM", SPM_SENDER, "TO", WAREHOUSE_EMAIL,
                            "TEXT", po, "SINCE", date(2022, 1, 1)],
                           ["SUBJECT", po, "SINCE", date(2022, 1, 1)],
                           ["TEXT", po, "SINCE", date(2022, 1, 1)]]
            else:
                # NLNG mails its POs in and Mobil's arrive as "ExxonMobil
                # Purchase Order 4501…", neither via the warehouse forward.
                # Subject first - a TEXT search on one Mobil PO returned 213
                # emails, almost all of them replies quoting the number.
                queries = [["SUBJECT", po, "SINCE", date(2022, 1, 1)],
                           ["TEXT", po, "SINCE", date(2022, 1, 1)]]
            # Work through every query until one actually YIELDS A VALUE.
            # Stopping at the first query that merely returned emails was the
            # bug: for Chevron the warehouse-routing search returns replies
            # with no usable PDF, so the broader searches were never tried and
            # POs whose document sits in an ordinary thread stayed unvalued.
            uid_sets = []
            for q in queries:
                found_uids, imap = _safe_search(imap, q)
                if found_uids:
                    uid_sets.append(found_uids)
            uids = []
            for _s in uid_sets:
                for _u in _s:
                    if _u not in uids:
                        uids.append(_u)

            # Scan from BOTH ends of the thread. Mobil's PO arrives late in a
            # long quoting thread, so newest-first was needed there; NLNG's
            # original "PO No. 4200071133" mail is the OLDEST, so newest-only
            # skipped POs a direct test had already proved readable.
            ordered = sorted(uids, reverse=True)
            # 10 from each end: at 6 a diagnostic sweep found eight POs
            # whose document sat just outside the window.
            for uid in (ordered[:14] + ordered[-14:] if len(ordered) > 14 else ordered):
                try:
                    raw = imap.fetch([uid], ["RFC822", "INTERNALDATE"])[uid]
                except Exception:
                    imap = _reconnect()
                    break
                msg = email_mod.message_from_bytes(raw[b"RFC822"])
                for blob in _pdfs(msg):
                    path = os.path.join(tmpdir, f"{po}.pdf")
                    with open(path, "wb") as fh:
                        fh.write(blob)
                    data = extract_for(po, path)
                    if not data:
                        continue
                    # The PDF must actually be this PO, not another one that
                    # happened to ride along in the same thread.
                    if data.get("buyer_po_number") and po not in str(data["buyer_po_number"]):
                        continue
                    if data.get("_text") and po not in data["_text"]:
                        continue      # the PDF in this thread is a different order
                    data.pop("_text", None)
                    out[po] = {
                        "po_value": data["po_value"],
                        "currency": data.get("currency") or "USD",
                        "po_date": raw[b"INTERNALDATE"].isoformat(),
                        "description": (data.get("description") or "")[:400],
                        "req_number": data.get("req_number"),
                        "pdf_url": None,
                        "source": data.get("source") or "routing_pdf",
                    }
                    if save:
                        save(po, out[po])     # commit now, not at the end
                    break
                if po in out:
                    break
            if idx % 20 == 0:
                print(f"     {idx}/{len(po_numbers)} probed ({len(out)} found)", flush=True)
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return out


# Mobil and Seplat send their POs to SPM's Yahoo inbox, not Gmail, so the
# Gmail search above misses most of them - it valued 16 of 31 Mobil POs. A
# direct check found the real document for 8 of the remaining 15 in Yahoo,
# under a subject of the form "ExxonMobil Purchase Order  4501813436 SAPG3P_010"
# or "SEPLAT ENERGY Purchase Order  4501808781 SAPNEP_01".
YAHOO_PO_FOLDERS = ["Inbox", "EXXONMOBIL LPO", "Trash/ExxonMobil Purchase Order", "Archive"]


def from_yahoo(po_numbers: list, save=None) -> dict:
    """Read Mobil/Seplat PO values off the PO documents in the Yahoo inbox.

    Searched by SUBJECT, not TEXT. A text search lands mostly on "open PO
    review" threads that quote dozens of PO numbers and attach something else
    entirely; the purchase order itself is the email named after it.
    """
    from imap_listener import connect_to_yahoo

    out: dict[str, dict] = {}
    tmpdir = tempfile.mkdtemp()
    try:
        yahoo = connect_to_yahoo()
    except Exception as exc:
        print(f"  [warn] Yahoo unavailable, Mobil lookups skipped: {exc}")
        return out
    try:
        available = {f[2] for f in yahoo.list_folders()}
        folders = [f for f in YAHOO_PO_FOLDERS if f in available]
        for po in po_numbers:
            hits = []
            for folder in folders:
                try:
                    yahoo.select_folder(folder, readonly=True)
                    hits += [(folder, uid) for uid in yahoo.search(["SUBJECT", po])]
                except Exception as exc:
                    print(f"  [warn] Yahoo search {folder} for {po}: {str(exc)[:60]}")
            # Newest first: a revised PO supersedes the original, and one of
            # Seplat's re-issues carried no total line at all, so keep trying
            # older copies until one actually yields a value.
            for folder, uid in sorted(hits, key=lambda h: h[1], reverse=True):
                try:
                    yahoo.select_folder(folder, readonly=True)
                    raw = yahoo.fetch([uid], ["RFC822", "INTERNALDATE"])[uid]
                except Exception:
                    continue
                msg = email_mod.message_from_bytes(raw[b"RFC822"])
                for blob in _pdfs(msg):
                    path = os.path.join(tmpdir, f"{po}.pdf")
                    with open(path, "wb") as fh:
                        fh.write(blob)
                    data = extract_mobil_po(path)
                    if not data or po not in data.get("_text", ""):
                        continue       # not this PO's own document
                    out[po] = {
                        "po_value": data["po_value"],
                        "currency": data.get("currency") or "USD",
                        "po_date": raw[b"INTERNALDATE"].isoformat(),
                        "description": decode_mime_words(msg.get("Subject", ""))[:400],
                        "req_number": None,
                        "pdf_url": None,
                        "source": "yahoo_pdf",
                    }
                    if save:
                        save(po, out[po])
                    break
                if po in out:
                    break
    finally:
        try:
            yahoo.logout()
        except Exception:
            pass
    return out


def store_value(client, po: str, rec: dict) -> None:
    """Write one client PO value, converted to USD at the time of storing."""
    usd, note = to_usd(rec.get("po_value"), rec.get("currency"), on=rec.get("po_date"))
    row = {**rec, "po_number": po, "client": _client_for(po),
           "po_value": usd, "currency": "USD",
           "description": (note + (rec.get("description") or ""))[:400]}
    try:
        client.table("scratch_client_po_values").upsert(
            row, on_conflict="po_number").execute()
    except Exception as exc:
        print(f"  [warn] could not store {po}: {str(exc)[:60]}")


def value_missing(po_numbers) -> dict:
    """Look up and store a value for each of these client POs that lacks one.

    The live path: the Flexitallic history listener calls this for the POs on
    each new acknowledgement. Tries the sources in order of trust - the live
    orders table, then the PO document in Gmail, then Yahoo for Mobil/Seplat -
    and never overwrites a value already stored (including hand-entered ones).
    """
    client = get_client()
    wanted = sorted({str(p).upper() for p in po_numbers if _client_for(str(p).upper())})
    if not wanted:
        return {}
    have = {r["po_number"] for r in
            (client.table("scratch_client_po_values").select("po_number")
             .not_.is_("po_value", "null").in_("po_number", wanted).execute().data or [])}
    todo = [p for p in wanted if p not in have]
    if not todo:
        return {}

    found = from_orders_table(todo)
    for po, rec in found.items():
        store_value(client, po, rec)

    save = lambda po, rec: store_value(client, po, rec)
    rest = [p for p in todo if p not in found]
    if rest:
        found.update(from_routing_emails(rest, tempfile.mkdtemp(), save=save))
    mobil = [p for p in rest if p not in found and p.startswith("4501")]
    if mobil:
        found.update(from_yahoo(mobil, save=save))
    return found


def main() -> None:
    apply = "--apply" in sys.argv
    limit = None
    for arg in sys.argv[1:]:
        if arg.startswith("--limit="):
            limit = int(arg.split("=", 1)[1])

    client = get_client()
    if apply:
        try:
            client.table("scratch_client_po_values").select("po_number").limit(1).execute()
        except Exception:
            print("Refusing to write: scratch_client_po_values does not exist.")
            print("Apply migrations/scratch_client_po_values.sql first.")
            sys.exit(1)

    wanted = collect_wanted()
    print(f"Client PO values - {'APPLYING' if apply else 'DRY RUN'}")
    print(f"  {len(wanted)} client PO number(s) referenced by the Flexitallic history")

    known = from_orders_table(sorted(wanted))
    print(f"  {len(known)} already valued in the live orders table")

    already = set()
    if apply:
        for i in range(0, len(wanted), 200):
            batch = sorted(wanted)[i:i + 200]
            already |= {r["po_number"] for r in
                        (client.table("scratch_client_po_values").select("po_number")
                         .not_.is_("po_value", "null")
                         .in_("po_number", batch).execute().data or [])}
        if already:
            print(f"  {len(already)} already stored from an earlier run - skipping those")

    todo = sorted(set(wanted) - set(known) - already)
    if limit:
        todo = todo[:limit]
    print(f"  {len(todo)} to read from warehouse-routing PDFs\n")

    tmpdir = tempfile.mkdtemp()

    def _save(po, rec):
        if apply:
            store_value(client, po, rec)

    found = from_routing_emails(todo, tmpdir, save=_save) if todo else {}
    print(f"\n  recovered {len(found)} of {len(todo)} from Gmail PDFs")

    mobil_left = [po for po in todo if po not in found and po.startswith("4501")]
    if mobil_left:
        from_yahoo_found = from_yahoo(mobil_left, save=_save)
        print(f"  recovered {len(from_yahoo_found)} of {len(mobil_left)} Mobil/Seplat POs from Yahoo")
        found.update(from_yahoo_found)

    combined = {**known, **found}
    for po, rec in combined.items():
        rec["client"] = _client_for(po)

    by_client: dict[str, list] = {}
    for po, rec in combined.items():
        by_client.setdefault(rec["client"] or "?", []).append(rec["po_value"])
    print("\n  client PO value by client:")
    for name, vals in sorted(by_client.items(), key=lambda kv: -sum(kv[1])):
        print(f"     {name:9} {len(vals):4} POs   ${sum(vals):>14,.2f}")
    print(f"\n  TOTAL {len(combined)} POs   ${sum(r['po_value'] for r in combined.values()):,.2f}")
    print(f"  still unvalued: {len(wanted) - len(combined)}")

    if not apply:
        print("\n(dry run - add --apply to write)")
        return

    payload = []
    for po, rec in combined.items():
        usd, note = to_usd(rec.get("po_value"), rec.get("currency"), on=rec.get("po_date"))
        payload.append({**rec, "po_number": po, "po_value": usd, "currency": "USD",
                        "description": (note + (rec.get("description") or ""))[:400]})
    for i in range(0, len(payload), 50):
        client.table("scratch_client_po_values").upsert(
            payload[i:i + 50], on_conflict="po_number").execute()
        print(f"   wrote {min(i + 50, len(payload))}/{len(payload)}")
    print(f"\nstored {len(payload)} client PO value(s)")


if __name__ == "__main__":
    main()

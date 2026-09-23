"""
backfill_flexitallic_history.py - reconstruct SPM->Flexitallic PO history.

Flexitallic asked whether the SPM relationship still justifies their
investment, so we need every SPM purchase order that Flexitallic actually
acknowledged, from 2024 to now, with its value and the gaskets on it.

The source is Flexitallic's own sales acknowledgement email. Each one carries
everything needed:

  Subject: Flexitallic Sales Acknowledgement for SO663748.
           Your PO number - S.P.M.-C.N.L.-1081-006. Total order value 429.72 US$
  PDF:     Remarks: S.P.M.-C.N.L.-1081-0060870148-FLEXITALLIC
           line items with Flexitallic item code, qty, unit price, extended price

The subject is truncated at about 22 characters, so the FULL reference (and
with it the customer's own PO number) is read from the PDF and the subject is
used only as a fallback.

Writes to scratch_flexitallic_history - deliberately NOT to
spm_purchase_orders, which the live pipeline owns and which only covers 2026.

    python scripts/backfill_flexitallic_history.py                # dry run
    python scripts/backfill_flexitallic_history.py --apply
    python scripts/backfill_flexitallic_history.py --apply --since=2024-01-01
    python scripts/backfill_flexitallic_history.py --csv=out.csv  # no DB needed
"""

import os
import re
import sys
import csv
import json
import email as email_mod
import tempfile
from pathlib import Path
from datetime import datetime, date

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

load_dotenv()

import pdfplumber
from db import get_client
from email_utils import decode_mime_words
from supplier_po_parser import connect_to_gmail, parse_so_pdf, extract_total_value

FLEX_SALES = "salesorder@flexitallic.eu"

PDF_TOTAL = re.compile(r"\bTotal:\s*([\d,]+\.\d{2})", re.IGNORECASE)
CURRENCY = re.compile(r"Total order value\s*[\d,.]+\s*(US\$|GBP|EUR)", re.IGNORECASE)
REMARKS = re.compile(r"Remarks:\s*(.+?)\s+Incoterms", re.IGNORECASE)
SUBJ_REF = re.compile(r"Your PO number\s*[-:]\s*(.+?)(?:\.?\s*Total order value)", re.IGNORECASE)
SO_RE = re.compile(r"\bSO\d{5,}\b")
# The customer token can sit anywhere: "S.P.M.-C.N.L.-1081-..." puts it second,
# "S.P.M.-2004-C.N.L.-..." puts the sequence number first.
CUSTOMER = re.compile(
    r"\b(C\.?\s?N\.?\s?L\.?|N\.?\s?L\.?\s?N\.?\s?G\.?|MOBIL|EXXON|SEPLAT|AVEON|CHINA"
    r"|G\.?\s?C\.?\s?A\.?|LNG)\b",
    re.IGNORECASE,
)
# Chevron 006..., NLNG 4200..., ExxonMobil/Seplat 4501... - and Aveon, who
# number theirs AVE126POH00633 / POH01767. Without the Aveon form their rows
# showed no client PO at all, and one showed the Chevron number that happened
# to share the reference.
CUSTPO = re.compile(
    r"\b(006\d{7}|4200\d{5,6}|4501\d{6}|(?:AVE\d{0,3})?POH\d{4,6}(?:-\d+)?"
    r"|GCA\d{6,12})\b",
    re.IGNORECASE)
SPMSEQ = re.compile(
    r"S\.?\s?P\.?\s?M\.?[-\s.]*(?:D\.?M\.?[-\s.]*)?(?:[A-Z.\s]{0,12}?[-\s.]+)?(\d{4})\b",
    re.IGNORECASE,
)

_CUSTOMER_MAP = {
    "CNL": "CHEVRON", "NLNG": "NLNG", "LNG": "NLNG", "MOBIL": "MOBIL",
    "EXXON": "MOBIL", "SEPLAT": "SEPLAT", "AVEON": "AVEON", "CHINA": "CHINA",
    # GCA Energy, first order Sep 2026 (S.P.M.-G.C.A.-3127-GCA4510527136).
    # Without this the row showed no customer and no client PO at all.
    "GCA": "GCA ENERGY",
}


def _norm_customer(raw: str | None) -> str | None:
    if not raw:
        return None
    return _CUSTOMER_MAP.get(re.sub(r"[.\s]", "", raw).upper())


def _pdf_bytes(msg) -> bytes | None:
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        payload = part.get_payload(decode=True)
        if payload and payload[:4] == b"%PDF":
            return payload
    return None


def parse_acknowledgement(msg, email_date, tmpdir: str) -> dict | None:
    """Turn one Flexitallic acknowledgement into a PO/SO row."""
    subject = re.sub(r"\s+", " ", decode_mime_words(msg.get("Subject", "") or ""))
    som = SO_RE.search(subject)
    if not som:
        return None

    po_reference = remarks = spm_ref = None
    line_items: list = []
    pdf_total = None

    raw_pdf = _pdf_bytes(msg)
    if raw_pdf:
        path = os.path.join(tmpdir, f"{abs(hash(subject))}.pdf")
        with open(path, "wb") as fh:
            fh.write(raw_pdf)
        try:
            parsed = parse_so_pdf(path)
            line_items = parsed.get("line_items") or []
            po_reference = parsed.get("po_reference")
            spm_ref = parsed.get("spm_ref")
            with pdfplumber.open(path) as pdf:
                text = "\n".join(pg.extract_text() or "" for pg in pdf.pages)
            rm = REMARKS.search(text)
            remarks = rm.group(1).strip() if rm else None
            totals = PDF_TOTAL.findall(text)
            pdf_total = float(totals[-1].replace(",", "")) if totals else None
        except Exception as exc:
            print(f"  [warn] PDF parse failed for {som.group(0)}: {exc}")

    sm = SUBJ_REF.search(subject)
    subject_ref = sm.group(1).strip() if sm else None

    # Prefer whichever candidate actually looks like a reference: Remarks is a
    # free-text field and sometimes holds a note ("EXXON ORDER", "EXW INDIA").
    candidates = [c for c in (po_reference, remarks, subject_ref)
                  if c and re.search(r"S\.?\s?P\.?\s?M|\d{6}", c)]
    full_reference = max(candidates, key=len) if candidates else (
        subject_ref or remarks or po_reference)

    haystack = " ".join(x for x in (full_reference, subject_ref, po_reference,
                                    remarks, subject) if x)
    if not spm_ref:
        q = SPMSEQ.search(full_reference or "") or SPMSEQ.search(subject_ref or "")
        spm_ref = q.group(1) if q else None
    if not spm_ref:
        # Some references put the sequence number LAST, after the customer POs:
        #   "S.P.M.-C.N.L.-0060949415-0060949123-1089"
        # Strip the customer PO numbers first so their digits cannot be
        # mistaken for the sequence, then take the remaining 4-digit token.
        stripped = CUSTPO.sub(" ", full_reference or subject_ref or "")
        loose = re.findall(r"(?<!\d)(\d{4})(?!\d)", stripped)
        spm_ref = loose[-1] if loose else None

    cu = CUSTOMER.search(haystack)
    cur = CURRENCY.search(subject)
    # The subject states the order value directly; the PDF's own Total is the
    # cross-check. On 202 of 213 orders both were present and agreed exactly.
    value = extract_total_value(subject)
    if value is None:
        value = pdf_total

    message_id = msg.get("Message-ID") or f"{msg.get('From')}-{msg.get('Date')}-{subject}"
    return {
        "so_number": som.group(0),
        "so_date": email_date.isoformat() if hasattr(email_date, "isoformat") else str(email_date),
        "order_value": value,
        "currency": (cur.group(1) if cur else "US$").replace("US$", "USD"),
        "spm_po_ref": spm_ref,
        "spm_po_reference": full_reference,
        "customer": _norm_customer(cu.group(1) if cu else None),
        "customer_po_numbers": sorted(set(CUSTPO.findall(haystack))),
        "line_item_count": len(line_items),
        "line_items": line_items,
        "source_message_id": message_id,
        "source_subject": subject,
        "_value_agrees": (value is not None and pdf_total is not None
                          and abs(value - pdf_total) < 0.02),
        "_has_pdf_total": pdf_total is not None,
    }


def collect(since: str) -> list[dict]:
    """Fetch and parse every Flexitallic acknowledgement since `since`."""
    imap = connect_to_gmail()
    tmpdir = tempfile.mkdtemp()
    rows: dict[str, dict] = {}
    try:
        imap.select_folder("[Gmail]/All Mail", readonly=True)
        uids = imap.search(["FROM", FLEX_SALES, "SINCE",
                            datetime.strptime(since, "%Y-%m-%d").date()])
        print(f"  {len(uids)} Flexitallic acknowledgement email(s) since {since}")
        BATCH = 12
        for i in range(0, len(uids), BATCH):
            chunk = uids[i:i + BATCH]
            fetched = imap.fetch(chunk, ["RFC822", "INTERNALDATE"])
            for uid in chunk:
                data = fetched.get(uid)
                if not data:
                    continue
                msg = email_mod.message_from_bytes(data[b"RFC822"])
                row = parse_acknowledgement(msg, data[b"INTERNALDATE"], tmpdir)
                if not row:
                    continue
                # Flexitallic re-sends a revised acknowledgement under the same
                # SO number; the newest one is the one that counts.
                prev = rows.get(row["so_number"])
                if prev is None or row["so_date"] > prev["so_date"]:
                    rows[row["so_number"]] = row
            if (i // BATCH) % 5 == 0:
                print(f"     {min(i + BATCH, len(uids))}/{len(uids)}", flush=True)
    finally:
        imap.logout()
    return sorted(rows.values(), key=lambda r: r["so_date"])


def report(rows: list[dict]) -> None:
    import collections
    n = len(rows)
    print(f"\n{'=' * 74}\n{n} sales orders parsed\n{'=' * 74}")
    for field, label in [("spm_po_ref", "SPM PO number"),
                         ("spm_po_reference", "full PO reference"),
                         ("customer", "end customer"),
                         ("order_value", "order value")]:
        got = sum(1 for r in rows if r[field])
        print(f"   {label:22} {got:4}/{n}  ({100 * got / n:.0f}%)")
    both = [r for r in rows if r["_has_pdf_total"] and r["order_value"] is not None]
    agree = sum(1 for r in both if r["_value_agrees"])
    print(f"   {'subject value == PDF':22} {agree:4}/{len(both)} of those with both")

    pos = collections.defaultdict(list)
    for r in rows:
        pos[r["spm_po_ref"] or r["spm_po_reference"]].append(r)
    print(f"\n   distinct SPM POs: {len(pos)}  ({sum(1 for v in pos.values() if len(v) > 1)} "
          f"have more than one sales order)")

    print("\n   by year (USD only):")
    per_year = collections.Counter()
    cnt = collections.Counter()
    for r in rows:
        y = r["so_date"][:4]
        cnt[y] += 1
        if r["currency"] == "USD":
            per_year[y] += r["order_value"] or 0
    for y in sorted(cnt):
        print(f"      {y}: {cnt[y]:4} SOs   {per_year[y]:>13,.2f}")

    print("\n   by customer:")
    byc, valc = collections.Counter(), collections.Counter()
    for r in rows:
        k = r["customer"] or "(unknown)"
        byc[k] += 1
        if r["currency"] == "USD":
            valc[k] += r["order_value"] or 0
    for k, c in byc.most_common():
        print(f"      {k:12} {c:4} SOs   {valc[k]:>13,.2f}")

    usd = sum(r["order_value"] or 0 for r in rows if r["currency"] == "USD")
    gbp = sum(r["order_value"] or 0 for r in rows if r["currency"] == "GBP")
    print(f"\n   TOTAL   USD {usd:,.2f}    GBP {gbp:,.2f}")


def write_csv(rows: list[dict], path: str) -> None:
    cols = ["spm_po_ref", "spm_po_reference", "customer", "customer_po_numbers",
            "so_number", "so_date", "order_value", "currency", "line_item_count"]
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([
                r["spm_po_ref"], r["spm_po_reference"], r["customer"],
                ", ".join(r["customer_po_numbers"]), r["so_number"],
                r["so_date"][:10], r["order_value"], r["currency"],
                r["line_item_count"],
            ])
    print(f"\nwrote {path}")


HISTORY = "scratch_flexitallic_history"


def _po_list(values) -> list[str]:
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except Exception:
            values = [values]
    return [str(v) for v in (values or [])]


def merge_row(current: dict, incoming: dict) -> dict:
    """The update that folds an acknowledgement into an existing SO row.

    Three rules, each fixing something the old plain upsert got wrong:

    * Client POs are UNIONED, never replaced. The acknowledgement's reference
      is truncated by Flexitallic's system, while the row may already hold the
      full list recovered from SPM's own purchase-order email - overwriting it
      wiped 116 recovered POs on any re-run.
    * A revised acknowledgement (a different, newer message for the same SO)
      replaces the value and line items, because those are what changed - but
      keeps the ORIGINAL order date, so a December order revised in January
      stays in the year it was placed.
    * Fields already set on the row, such as a corrected customer, are left
      alone; only blanks are filled.

    Returns only the columns that actually change; an empty dict means no-op.
    """
    update: dict = {}

    merged = sorted(set(_po_list(current.get("customer_po_numbers")))
                    | set(_po_list(incoming.get("customer_po_numbers"))))
    if merged != sorted(_po_list(current.get("customer_po_numbers"))):
        update["customer_po_numbers"] = merged

    is_revision = (incoming.get("source_message_id")
                   and incoming["source_message_id"] != current.get("source_message_id")
                   and str(incoming.get("so_date") or "")[:19] > str(current.get("so_date") or "")[:19])
    if is_revision:
        for col in ("order_value", "currency", "line_item_count", "line_items",
                    "source_message_id", "source_subject"):
            if incoming.get(col) is not None and incoming.get(col) != current.get(col):
                update[col] = incoming[col]

    for col in ("spm_po_ref", "spm_po_reference", "customer"):
        if not current.get(col) and incoming.get(col):
            update[col] = incoming[col]
    return update


def upsert_history_row(client, row: dict) -> tuple[str, str | None]:
    """Insert a new sales order or merge into its existing row.

    Keyed on so_number. The table's only unique key is source_message_id, so
    upserting on it gave a revised acknowledgement - a new message for the same
    SO - a second row, and the SO value was counted twice.

    Returns (action, row_id) with action in inserted / updated / unchanged.
    """
    existing = (client.table(HISTORY)
                .select("id,so_date,source_message_id,customer_po_numbers,customer,"
                        "spm_po_ref,spm_po_reference,order_value,currency,"
                        "line_item_count,line_items,source_subject")
                .eq("so_number", row["so_number"]).order("so_date").execute().data or [])
    if not existing:
        payload = {k: row.get(k) for k in (
            "so_number", "so_date", "order_value", "currency", "spm_po_ref",
            "spm_po_reference", "customer", "customer_po_numbers",
            "line_item_count", "line_items", "source_message_id", "source_subject")}
        res = client.table(HISTORY).insert(payload).execute()
        return "inserted", (res.data or [{}])[0].get("id")

    if len(existing) > 1:
        print(f"  [warn] {row['so_number']} has {len(existing)} rows - merging into the earliest")
    current = existing[0]
    update = merge_row(current, row)
    if not update:
        return "unchanged", current["id"]
    client.table(HISTORY).update(update).eq("id", current["id"]).execute()
    return "updated", current["id"]


def load(rows: list[dict]) -> None:
    client = get_client()
    counts: dict[str, int] = {}
    for i, r in enumerate(rows, 1):
        action, _ = upsert_history_row(client, r)
        counts[action] = counts.get(action, 0) + 1
        if i % 50 == 0:
            print(f"   {i}/{len(rows)}")
    print(f"\nscratch_flexitallic_history: {counts}")


def main() -> None:
    apply = "--apply" in sys.argv
    since = "2024-01-01"
    csv_path = None
    for arg in sys.argv[1:]:
        if arg.startswith("--since="):
            since = arg.split("=", 1)[1]
        elif arg.startswith("--csv="):
            csv_path = arg.split("=", 1)[1]

    if apply:
        try:
            get_client().table("scratch_flexitallic_history").select("id").limit(1).execute()
        except Exception:
            print("Refusing to write: scratch_flexitallic_history does not exist.")
            print("Apply migrations/scratch_flexitallic_history.sql first.")
            sys.exit(1)

    print(f"Flexitallic PO/SO history - {'LOADING' if apply else 'DRY RUN'}, since {since}")
    rows = collect(since)
    report(rows)
    if csv_path:
        write_csv(rows, csv_path)
    if apply:
        load(rows)
    elif not csv_path:
        print("\n(dry run - add --apply to load, or --csv=<path> to export)")


if __name__ == "__main__":
    main()

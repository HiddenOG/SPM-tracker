"""
backfill_ack_dates.py — Fill in acknowledged_at for orders where it's null,
WITHOUT the Anthropic API. It locates each PO's GEP acknowledged PDF in Gmail
(the version whose header shows 'Partner Acknowledged' + 'Supplier Acknowledged
on: <date>'), reads the date locally with pdfplumber, and stamps the order.

Why local extraction: the acknowledged date is a fixed labelled field on the
GEP export, so a regex is reliable and free — no LLM call required.

Usage:
    python backfill_ack_dates.py            # dry run — reports only
    python backfill_ack_dates.py --apply    # actually stamp the DB
"""
import sys
import re
import email
from email.header import decode_header
from pathlib import Path

import pdfplumber

from db import get_client
import gmail_ack_listener as g

APPLY = "--apply" in sys.argv
ACK_DIR = Path(__file__).parent.parent / "data" / "ack_attachments"
DATE_RE = re.compile(r"Supplier\s*Acknowledged\s*on\s*:?\s*(\d{1,2}/\d{1,2}/\d{4})", re.I)


def dec(s):
    if not s:
        return ""
    out = ""
    for p, e in decode_header(s):
        out += p.decode(e or "utf-8", "replace") if isinstance(p, bytes) else p
    return out


def iso_date(us_date: str) -> str:
    """Convert M/D/YYYY -> YYYY-MM-DD."""
    m, d, y = us_date.split("/")
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def read_ack_date(pdf_path: str):
    """Return (status, ack_date_iso) from a GEP PDF using pdfplumber."""
    with pdfplumber.open(pdf_path) as pdf:
        txt = pdf.pages[0].extract_text() or ""
    status_m = re.search(r"Status\s*:?\s*([A-Za-z ]+)", txt)
    status = status_m.group(1).strip() if status_m else None
    date_m = DATE_RE.search(txt)
    return status, (iso_date(date_m.group(1)) if date_m else None)


def find_gep_pdf_for_po(imap, po: str):
    """Search Gmail for an email whose PDF attachment is the acknowledged
    GEP export for this PO. Returns (saved_path, ack_date_iso) or (None, None)."""
    ids = imap.search(["SUBJECT", po])
    if not ids:
        return None, None
    msgs = imap.fetch(ids, ["RFC822"])
    for uid, data in msgs.items():
        msg = email.message_from_bytes(data[b"RFC822"])
        if not msg.is_multipart():
            continue
        for part in msg.walk():
            fn = part.get_filename()
            if not fn or not dec(fn).lower().endswith(".pdf"):
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            save_path = ACK_DIR / f"{po}_{dec(fn)}"
            save_path.write_bytes(payload)
            status, ack_date = read_ack_date(str(save_path))
            if ack_date:
                return str(save_path), ack_date
            # not the acknowledged version — clean up and keep looking
            try:
                save_path.unlink()
            except OSError:
                pass
    return None, None


def main():
    client = get_client()
    rows = (
        client.table("orders")
        .select("id, buyer_po_number, acknowledgment_status, overall_status")
        .is_("acknowledged_at", "null")
        .order("buyer_po_number")
        .execute()
        .data
    )
    print(f"Orders with null acknowledged_at: {len(rows)}")
    print(f"Mode: {'APPLY (will write to DB)' if APPLY else 'DRY RUN (report only)'}\n")

    imap = g.connect_to_gmail()
    try:
        for r in rows:
            po = r["buyer_po_number"]
            path, ack_date = find_gep_pdf_for_po(imap, po)
            if ack_date:
                print(f"✅ {po}: found GEP PDF, acknowledged on {ack_date}")
                print(f"     {path}")
                if APPLY:
                    client.table("orders").update(
                        {
                            "acknowledgment_status": "acknowledged",
                            "acknowledged_at": ack_date,
                            "acknowledged_by": "GEP export (backfill, local extract)",
                            "pdf_attachment_path": path,
                        }
                    ).eq("id", r["id"]).execute()
                    print("     -> stamped.")
            else:
                print(f"❌ {po}: no acknowledged GEP PDF found in Gmail "
                      f"(no attachment with a 'Supplier Acknowledged on' date).")
    finally:
        imap.logout()


if __name__ == "__main__":
    main()

"""
backfill_parked_routing.py — Process parked warehouse_routing emails.

Handles the Yahoo-outage case: GEP routing emails arrived in Gmail, were
parked because no order existed yet, and Yahoo never caught up.

For each parked entry:
  1. If the PDF is on local disk  → use it directly with pdfplumber.
  2. If the PDF is missing locally → re-fetch the email from Gmail IMAP
     using the stored Message-ID, save the PDF, then process normally.

Run once:  python scripts/backfill_parked_routing.py
Safe to re-run — skips any order that was already created.
"""

import email
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
load_dotenv()

from db import get_client, base_po_number
from pdf_extractor import extract_pdf_with_pdfplumber, save_extraction_result
from email_utils import decode_mime_words
import sync


# ── Gmail re-fetch helpers ────────────────────────────────────────────────────

def _connect_gmail():
    from imapclient import IMAPClient
    host     = os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com")
    port     = int(os.environ.get("GMAIL_IMAP_PORT", 993))
    addr     = os.environ["GMAIL_EMAIL"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    folder   = os.environ.get("GMAIL_ACK_FOLDER", "[Gmail]/All Mail")
    c = IMAPClient(host, port=port, use_uid=True, ssl=True)
    c.login(addr, password)
    c.select_folder(folder)
    return c


def _fetch_pdf_from_gmail(imap, message_id: str, po_hint: str) -> str | None:
    """Search Gmail for `message_id`, save the first PDF attachment, return path."""
    from gmail_ack_listener import save_and_overwrite_pdf
    try:
        uids = imap.search(["HEADER", "Message-ID", message_id])
        if not uids:
            print(f"  ⚠️  Message-ID {message_id!r} not found in Gmail")
            return None
        data = imap.fetch(uids[:1], ["RFC822"])
        uid  = uids[0]
        if uid not in data:
            return None
        msg = email.message_from_bytes(data[uid][b"RFC822"])
        pdf_path = save_and_overwrite_pdf(msg, po_hint)
        if pdf_path:
            print(f"  📥 PDF re-fetched from Gmail → {Path(pdf_path).name}")
        else:
            print(f"  ⚠️  Email found in Gmail but no PDF attachment")
        return pdf_path
    except Exception as e:
        print(f"  ❌ Gmail re-fetch failed: {e}")
        return None


# ── Main backfill ─────────────────────────────────────────────────────────────

def run() -> None:
    client = get_client()

    parked = (
        client.table("parked_emails")
        .select("*")
        .eq("kind", "warehouse_routing")
        .execute()
    ).data or []

    if not parked:
        print("No parked warehouse_routing emails found — nothing to do.")
        return

    print(f"Found {len(parked)} parked warehouse_routing email(s).\n")

    # Resolve Chevron buyer once
    buyers = client.table("buyers").select("id,notification_email_sender").execute()
    buyer_id = None
    for b in (buyers.data or []):
        s = (b.get("notification_email_sender") or "").lower()
        if "chevron" in s or "cnl" in s:
            buyer_id = b["id"]
            break
    if not buyer_id:
        print("❌ No Chevron buyer found in DB — cannot create orders.")
        return

    # Open Gmail connection once (used only for entries missing local PDFs)
    imap = None

    for entry in parked:
        po_number   = entry["po_number"]
        email_date  = entry.get("email_date")
        pdf_path    = entry.get("pdf_path")
        body_text   = entry.get("body_text")
        message_id  = entry.get("message_id", "")
        base_po     = base_po_number(po_number) or po_number

        print(f"─── PO {po_number} ───────────────────────────────")

        # If order already exists, just reconcile
        existing = (
            client.table("orders").select("id")
            .eq("buyer_po_number", base_po).execute()
        ).data
        if existing:
            order_id = existing[0]["id"]
            print(f"  ℹ️  Order already exists (id={order_id}) — calling reconcile_po()")
            applied = sync.reconcile_po(base_po)
            print(f"  {'✅' if applied else '⚠️ '} reconcile_po applied {applied} parked email(s)")
            continue

        # Resolve the PDF — local disk first, Gmail re-fetch if missing
        if not pdf_path or not Path(pdf_path).exists():
            print(f"  📂 PDF not on local disk — re-fetching from Gmail...")
            # Reconnect if the previous connection was lost (SSL timeout)
            if imap is None:
                try:
                    imap = _connect_gmail()
                    print(f"  🔌 Connected to Gmail IMAP")
                except Exception as e:
                    print(f"  ❌ Could not connect to Gmail: {e}")
                    print(f"  ⏭️  Skipping PO {po_number}")
                    continue
            pdf_path = _fetch_pdf_from_gmail(imap, message_id, base_po)
            if pdf_path is None:
                # Connection may have dropped — reconnect and retry once
                try:
                    imap = _connect_gmail()
                    print(f"  🔄 Reconnected to Gmail IMAP — retrying...")
                    pdf_path = _fetch_pdf_from_gmail(imap, message_id, base_po)
                except Exception as e:
                    print(f"  ❌ Gmail reconnect failed: {e}")
                    imap = None

        if not pdf_path or not Path(pdf_path).exists():
            print(f"  ⏭️  No PDF available — skipping PO {po_number}")
            continue

        # Run pdfplumber
        print(f"  📄 Running pdfplumber on {Path(pdf_path).name} ...")
        plumber = extract_pdf_with_pdfplumber(pdf_path)
        if "error" in plumber:
            print(f"  ❌ pdfplumber error: {plumber['error']} — skipping")
            continue

        notification_received_at = plumber.get("order_submitted_on") or email_date

        # Upload PDF to Supabase Storage so it survives server restarts.
        _pdf_url = None
        try:
            from storage import upload_pdf
            _pdf_url = upload_pdf(pdf_path, "ack", base_po)
            if _pdf_url:
                print(f"  ☁️  PDF uploaded to storage: {_pdf_url}")
        except Exception as _ue:
            print(f"  Warning: PDF storage upload failed for {base_po}: {_ue}")

        insert_row = {k: v for k, v in {
            "buyer_id":                 buyer_id,
            "buyer_po_number":          base_po,
            "notification_received_at": notification_received_at,
            "pdf_attachment_path":      pdf_path,
            "pdf_url":                  _pdf_url,
            "ack_pdf_url":              _pdf_url,
            "acknowledgment_status":    "pending",
            "overall_status":           "pending_acknowledgment",
            "po_amount":                plumber.get("net_total"),
            "po_currency":              plumber.get("currency") or "USD",
        }.items() if v is not None}

        try:
            res      = client.table("orders").insert(insert_row).execute()
            order_id = res.data[0]["id"]
            print(f"  ✅ Order created: {base_po} (id={order_id}), T0={notification_received_at}")
        except Exception as e:
            if "23505" in str(e) or "duplicate key" in str(e).lower():
                again = client.table("orders").select("id").eq("buyer_po_number", base_po).execute()
                if again.data:
                    order_id = again.data[0]["id"]
                    print(f"  ↩️  Race: order was just created (id={order_id})")
                else:
                    print(f"  ❌ Duplicate key but row not found — skipping")
                    continue
            else:
                print(f"  ❌ Insert failed: {e}")
                continue

        # Save all Stage-2 columns + line items immediately
        try:
            save_extraction_result(order_id, plumber)
            n = len(plumber.get("line_items") or [])
            print(f"  📋 Stage-2 fields saved: product={plumber.get('product_line')}, "
                  f"rdd={plumber.get('required_delivery_date')}, {n} line item(s)")
        except Exception as e:
            print(f"  ⚠️  save_extraction_result failed (non-fatal): {e}")

        # Stamp ack date if pdfplumber found one
        ack_date = plumber.get("supplier_acknowledged_on")
        if ack_date:
            client.table("orders").update({
                "acknowledgment_status": "acknowledged",
                "acknowledged_at":       ack_date,
                "acknowledged_by":       "GEP export (backfill, pdfplumber)",
            }).eq("id", order_id).execute()
            sync.advance_status(client, order_id, "acknowledged")
            print(f"  ✅ Ack date stamped: {ack_date} (pdfplumber)")
        else:
            print(f"  ⏳ No ack date in PDF — ack_pdf_extractor will retry async")

        # reconcile_po stamps sent_to_warehouse + deletes park entry
        applied = sync.reconcile_po(base_po)
        if applied:
            print(f"  📤 reconcile_po applied {applied} parked email(s) — sent_to_warehouse stamped")
        else:
            print(f"  ⚠️  reconcile_po found 0 entries — stamping sent_to_warehouse directly")
            upd = {"sent_to_warehouse_at": email_date}
            if body_text:
                upd["warehouse_routing_raw"] = body_text.strip()
            client.table("orders").update(upd).eq("id", order_id).execute()
            sync.advance_status(client, order_id, "awaiting_warehouse_stock_check")

        print(f"  🏁 PO {po_number} fully processed.\n")

    if imap:
        try:
            imap.logout()
        except Exception:
            pass


if __name__ == "__main__":
    run()

"""
gmail_ack_listener.py — Stage 3 (revised): Watch Gmail for the
manually-forwarded GEP acknowledgment PDF and extract the real
acknowledged date automatically.

Real-world flow this matches:
1. Chevron notification PDF arrives in Yahoo (Stage 1 — already built).
   That PDF does NOT contain an acknowledgment date.
2. A human clicks through to the GEP portal and acknowledges the PO there.
   This step is NOT automatable — Chevron requires a real person to do it.
3. The human then manually exports/prints a NEW PDF from GEP — this one
   DOES contain the acknowledged date — and emails it to Gmail by hand.
4. THIS script watches that Gmail inbox, reads the new PDF, pulls out
   the PO number (same number as the original Yahoo PO) and the
   acknowledged date, matches it to the existing order, and stamps T1.

This does NOT replace the human action in step 2/3 — it just means
nobody has to remember to manually update a status afterward. The
acknowledged date comes straight from Chevron's own GEP export, which
is more reliable than a human-remembered timestamp.

Run this with:  python scripts/gmail_ack_listener.py

Note: Gmail needs an "App Password" the same way Yahoo did — generate
one from your Google Account > Security > 2-Step Verification > App passwords.
Regular Gmail login password will NOT work here.
"""

import os
import re
import sys
import time
import json
import base64
import email
from email.header import decode_header
from pathlib import Path
from dotenv import load_dotenv
from imapclient import IMAPClient
import anthropic

from db import get_client, normalize_po_number
import sync
from config import SPM_SENDER, WAREHOUSE_EMAIL, NLNG_PO_SENDER, FLEXITALLIC_SENDER

# Windows' default console codepage (cp1252) can't encode the emoji and
# em-dash characters used in this script's status output, which would crash
# the listener the moment it hits a new email. Force UTF-8 on stdout/stderr.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

load_dotenv()

ACK_PDF_DIR = Path(__file__).parent.parent / "data" / "ack_attachments"
ACK_PDF_DIR.mkdir(parents=True, exist_ok=True)
ACK_EXTRACTION_PROMPT = """You are reading a Chevron purchase order PDF exported from \
the GEP SMART supplier portal. Extract ALL of the following fields exactly as they appear.

Respond with ONLY valid JSON, no other text, no markdown fences:
{
  "po_number": "<Purchase Order Number EXACTLY as shown, INCLUDING any revision suffix like '-001'. A change order shows e.g. '0060792432 - 001' — return it as '0060792432-001'. A normal order is just digits, e.g. '0061440972'.>",
  "status": "<Status field value, e.g. 'Partner Acknowledged'>",
  "order_submitted_on": "<Order Submitted on date — YYYY-MM-DD format>",
  "supplier_acknowledged_on": "<Supplier Acknowledged on date — YYYY-MM-DD format, THIS IS CRITICAL, look for exact label 'Supplier Acknowledged on'>",
  "payment_terms": "<Payment Terms field value>",
  "po_destination": "<PO Destination field value>",
  "transportation": "<Transportation field value>",
  "requestor_name": "<Requestor Name from Purchaser Information section>",
  "requestor_email": "<Requestor Email from Purchaser Information section>",
  "ship_to": "<Ship To address>",
  "line_items": [
    {
      "line_no": "<Line No.>",
      "description": "<full Description text>",
      "item_number": "<Item Number>",
      "supplier_item_number": "<Supplier Item Number>",
      "quantity": <number>,
      "uom": "<Unit of Measure>",
      "required_delivery_date": "<YYYY-MM-DD>",
      "unit_price": <number or null>,
      "promised_date": "<YYYY-MM-DD or null>",
      "total": <number or null>
    }
  ],
  "confidence": "<high or low>"
}

Critical instructions:
- 'supplier_acknowledged_on' is the date the supplier clicked acknowledge in GEP — \
  it appears as a row labeled exactly 'Supplier Acknowledged on' in the order header table. \
  Do NOT confuse this with 'Order Submitted on' which is a different field.
- If 'Supplier Acknowledged on' is not present at all, return null for that field — \
  this means the PDF is the original unacknowledged version, not the GEP export.
- Extract digits only for po_number, no prefix text.
- All dates must be YYYY-MM-DD format.
"""


PO_ATTACHMENTS_DIR = Path(__file__).parent.parent / "data" / "po_attachments"
PO_ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)

SO_ATTACHMENTS_DIR = Path(__file__).parent.parent / "data" / "so_attachments"
SO_ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)


# ── NLNG PDF helpers ─────────────────────────────────────────────────────────

def _extract_nlng_attachment(msg: email.message.Message) -> bytes | None:
    """
    Return PDF bytes from the NLNG PO email attachment.
    Attachments arrive named 'part2.dat' but contain valid PDF bytes.
    Accepts any part whose bytes start with %PDF, or any .dat payload.
    """
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            payload = part.get_payload(decode=True)
            if payload and payload[:4] == b"%PDF":
                return payload
            fname = part.get_filename() or ""
            if fname.lower().endswith(".dat") and payload:
                return payload
    else:
        payload = msg.get_payload(decode=True)
        if payload and payload[:4] == b"%PDF":
            return payload
    return None


def _save_nlng_pdf(pdf_bytes: bytes, po_number: str) -> str:
    safe_po = re.sub(r"[^\w\-]", "_", po_number)
    path = PO_ATTACHMENTS_DIR / f"NLNG_{safe_po}.pdf"
    path.write_bytes(pdf_bytes)
    return str(path)


def _save_so_pdf(msg: email.message.Message, so_number: str) -> str | None:
    """Save the first PDF attachment from a Flexitallic SO email. Returns path or None."""
    for part in msg.walk():
        if part.get_content_type() != "application/pdf":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        safe_so = re.sub(r"[^\w\-]", "_", so_number)
        filename = part.get_filename() or f"SO_{safe_so}.pdf"
        path = SO_ATTACHMENTS_DIR / f"{safe_so}_{filename}"
        path.write_bytes(payload)
        return str(path)
    return None


def process_nlng_po_email(client_db, msg: email.message.Message,
                          message_id: str, sender: str, subject: str) -> None:
    """
    Process an NLNG PO email forwarded from enquiry@specialpipingltd.com into Gmail.
    Extracts the PDF attachment, parses it, upserts nlng_orders + line items.
    """
    from nlng_pdf_parser import parse_nlng_po_pdf

    email_date = sync.parse_email_date(msg)
    pdf_bytes = _extract_nlng_attachment(msg)

    if not pdf_bytes:
        print(f"⚠️  NLNG email has no PDF attachment: {subject}")
        client_db.table("processed_emails").upsert({
            "message_id": message_id, "sender": sender, "subject": subject,
            "processing_result": "nlng_no_pdf",
        }, on_conflict="message_id").execute()
        return

    fields = parse_nlng_po_pdf(pdf_bytes)
    parse_error = fields.get("_parse_error")

    if not fields.get("po_number"):
        m = re.search(r"\b(4200\d{5,6})\b", subject)
        if m:
            fields["po_number"] = m.group(1)

    po_number = fields.get("po_number")
    if not po_number:
        print(f"⚠️  Could not extract NLNG PO number from: {subject}")
        client_db.table("processed_emails").upsert({
            "message_id": message_id, "sender": sender, "subject": subject,
            "processing_result": "nlng_no_po_number",
        }, on_conflict="message_id").execute()
        return

    variation_no = fields.get("variation_number", 0)
    pdf_path = _save_nlng_pdf(pdf_bytes, po_number)

    pdf_url = None
    try:
        from storage import upload_pdf
        pdf_url = upload_pdf(pdf_path, "nlng_po", po_number)
    except Exception as _e:
        print(f"  Warning: NLNG PO storage upload failed for {po_number}: {_e}")

    order_row = {k: v for k, v in {
        "po_number":                po_number,
        "variation_number":         variation_no,
        "document_date":            fields.get("document_date"),
        "notification_received_at": email_date,
        "required_delivery_date":   fields.get("required_delivery_date"),
        "delivery_terms":           fields.get("delivery_terms"),
        "delivery_address":         fields.get("delivery_address"),
        "net_value":                fields.get("net_value"),
        "currency":                 fields.get("currency", "USD"),
        "contact_name":             fields.get("contact_name"),
        "contact_email":            fields.get("contact_email"),
        "enquiry_number":           fields.get("enquiry_number"),
        "pdf_attachment_path":      pdf_path,
        "pdf_url":                  pdf_url,
    }.items() if v is not None}

    try:
        client_db.table("nlng_orders").upsert(
            order_row, on_conflict="po_number,variation_number"
        ).execute()
    except Exception as e:
        print(f"❌ NLNG upsert failed for {po_number}: {e}")
        raise

    order_res = (
        client_db.table("nlng_orders")
        .select("id").eq("po_number", po_number).eq("variation_number", variation_no)
        .execute()
    )
    if not order_res.data:
        print(f"❌ Could not find nlng_orders row after upsert for {po_number}")
        return
    order_id = order_res.data[0]["id"]

    # Advance status (sets notification_received for new orders; won't regress existing ones).
    sync.nlng_advance_status(client_db, order_id)

    # Replay any warehouse emails that arrived before this PO was processed.
    reconciled = sync.reconcile_nlng_po(po_number)
    if reconciled:
        print(f"  ↩️  Reconciled {reconciled} parked email(s) for NLNG {po_number}")

    line_items = fields.get("line_items", [])
    if line_items:
        client_db.table("nlng_order_line_items").delete().eq("nlng_order_id", order_id).execute()
        client_db.table("nlng_order_line_items").insert([{
            "nlng_order_id": order_id,
            "item_no":       item.get("item_no"),
            "mesc_code":     item.get("mesc_code"),
            "description":   item.get("description"),
            "quantity":      item.get("quantity"),
            "uom":           item.get("uom"),
            "unit_price":    item.get("unit_price"),
            "net_amount":    item.get("net_amount"),
            "int_article_no": item.get("int_article_no"),
            "delivery_date": item.get("delivery_date"),
        } for item in line_items]).execute()

    client_db.table("processed_emails").upsert({
        "message_id": message_id, "sender": sender, "subject": subject,
        "processing_result": "nlng_po_created",
        "raw_notes": f"NLNG PO {po_number} v{variation_no}"
                     + (f" | parse_error: {parse_error}" if parse_error else ""),
    }, on_conflict="message_id").execute()

    vlabel = f" (Variation {variation_no})" if variation_no else ""
    print(
        f"✅ NLNG PO {po_number}{vlabel} — "
        f"delivery {fields.get('required_delivery_date') or 'TBD'}, "
        f"${fields.get('net_value') or 0:,.2f} USD — "
        f"{len(line_items)} line item(s)"
        + (f" [parse warning: {parse_error}]" if parse_error else "")
    )


def connect_to_gmail() -> IMAPClient:
    """
    Open an IMAP connection to Gmail.

    IMPORTANT — this listener watches for the warehouse-ROUTING email, which
    SPM *sends* (from specialpiping@gmail.com to the warehouse). Sent mail does
    NOT appear in INBOX — it lives in Sent / All Mail. Searching INBOX here
    silently missed every routing email (the only reason 0061442579's self-CC
    copy showed up at all was that it was also addressed back to the sender).

    '[Gmail]/All Mail' is the one folder that contains BOTH sent and received
    mail, so it catches routing emails (sent) without losing anything.
    """
    host = os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com")
    port = int(os.environ.get("GMAIL_IMAP_PORT", 993))
    email_addr = os.environ["GMAIL_EMAIL"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]
    folder = os.environ.get("GMAIL_ACK_FOLDER", "[Gmail]/All Mail")

    client = IMAPClient(host, port=port, use_uid=True, ssl=True)
    client.login(email_addr, app_password)
    client.select_folder(folder)
    return client


def decode_mime_words(s: str) -> str:
    """Decode email subject/sender headers that may be MIME-encoded."""
    if not s:
        return ""
    decoded_parts = decode_header(s)
    result = ""
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            result += part.decode(encoding or "utf-8", errors="replace")
        else:
            result += part
    return result


def is_already_processed(message_id: str) -> bool:
    """Check if we've already logged this email (by Message-ID header)."""
    client = get_client()
    result = (
        client.table("processed_emails")
        .select("id")
        .eq("message_id", message_id)
        .execute()
    )
    return len(result.data) > 0


def save_and_overwrite_pdf(msg: email.message.Message, po_number: str) -> str | None:
    """
    Save the GEP-acknowledged PDF from the Gmail warehouse email.
    When multiple PDFs are attached, prefer the one whose filename
    contains the PO number — that's always the actual Chevron GEP PDF.
    Falls back to the first PDF found if no name match.
    """
    if not msg.is_multipart():
        return None

    all_pdfs = []
    for part in msg.walk():
        filename = part.get_filename()
        if not filename:
            continue
        filename = decode_mime_words(filename)
        if filename.lower().endswith(".pdf"):
            payload = part.get_payload(decode=True)
            if payload:
                all_pdfs.append((filename, payload))

    if not all_pdfs:
        return None

    # Prefer the PDF whose filename contains the PO number
    chosen_filename, chosen_payload = all_pdfs[0]
    for filename, payload in all_pdfs:
        if po_number in filename:
            chosen_filename = filename
            chosen_payload = payload
            break

    save_path = ACK_PDF_DIR / f"{po_number}_{chosen_filename}"
    save_path.write_bytes(chosen_payload)
    return str(save_path)


def upload_ack_pdf(local_path: str, po_number: str) -> str | None:
    """Upload the ack PDF to Supabase Storage and return the public URL."""
    try:
        from storage import upload_pdf
        return upload_pdf(local_path, "ack", po_number)
    except Exception as e:
        print(f"  Warning: ack storage upload failed for {po_number}: {e}")
        return None


def extract_ack_info_with_claude(pdf_path: str) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    pdf_bytes = Path(pdf_path).read_bytes()
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {"type": "text", "text": ACK_EXTRACTION_PROMPT},
                ],
            }
        ],
    )

    response_text = message.content[0].text.strip()

    if "```" in response_text:
        parts = response_text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                response_text = part
                break

    start = response_text.find("{")
    end = response_text.rfind("}") + 1
    if start != -1 and end > start:
        response_text = response_text[start:end]

    try:
        return json.loads(response_text)
    except json.JSONDecodeError as e:
        print(f"   ⚠️  JSON parse warning: {e}")
        print(f"   Raw response: {response_text[:200]}...")
        return {"po_number": None, "supplier_acknowledged_on": None}


def find_order_by_po_number(po_number: str) -> dict | None:
    """Look up the existing order created back in Stage 1 by PO number.

    Normalises the incoming value first (e.g. Claude may return
    '0060792432 - 001') so it matches the canonical stored form
    ('0060792432-001'), then orders by created_at so duplicates resolve
    deterministically to the earliest row."""
    client = get_client()
    po_number = normalize_po_number(po_number) or po_number
    result = (
        client.table("orders")
        .select("*")
        .eq("buyer_po_number", po_number)
        .order("created_at", desc=False)
        .execute()
    )
    return result.data[0] if result.data else None


def stamp_acknowledgment(order_id: str, acknowledged_date: str, ack_pdf_path: str) -> None:
    """
    Update the order with the real acknowledged date AND update the
    pdf_attachment_path to point to the Gmail version (the GEP export
    with the acknowledged date), replacing the original Yahoo PDF path.
    """
    client = get_client()
    client.table("orders").update(
        {
            "acknowledgment_status": "acknowledged",
            "acknowledged_at": acknowledged_date,
            "acknowledged_by": "GEP export (auto-detected)",
            "pdf_attachment_path": ack_pdf_path,
        }
    ).eq("id", order_id).execute()
    # Monotonic: never regress an order that's already further along.
    sync.advance_status(client, order_id, "acknowledged")


def get_recipients(msg: email.message.Message) -> list[str]:
    """Get all To + Cc recipients as a flat list of lowercase strings."""
    recipients = []
    for header in ("To", "Cc"):
        value = msg.get(header, "")
        if value:
            recipients.append(value.lower())
    return recipients


def is_warehouse_routing_email(msg: email.message.Message, sender: str, subject: str) -> bool:
    """
    Detect the email where SPM forwards a PO to warehouse asking to check stock.
    Pattern: sent BY specialpiping@gmail.com, TO/CC includes the warehouse address,
    subject is either:
      - A bare Chevron PO number (006XXXXXXX) with optional Re:/Fwd: prefix
      - An NLNG PO subject ("PO No. 4200XXXXXXX") with optional Re:/Fwd: prefix
    """
    if SPM_SENDER.lower() not in sender.lower():
        return False

    recipients = get_recipients(msg)
    if not any(WAREHOUSE_EMAIL.lower() in r for r in recipients):
        return False

    return (
        sync.is_bare_po_subject(subject) is not None
        or sync.is_nlng_po_subject(subject) is not None
    )


def is_nlng_so_to_warehouse(msg: email.message.Message, sender: str, subject: str) -> bool:
    """
    Detect SPM forwarding a Flexitallic SO to the warehouse.
    FROM specialpiping, TO warehouse, subject contains Flexitallic SO reference.
    """
    if SPM_SENDER.lower() not in sender.lower():
        return False
    recipients = get_recipients(msg)
    if not any(WAREHOUSE_EMAIL.lower() in r for r in recipients):
        return False
    return sync.is_flex_so_subject(subject) is not None


def is_spm_delivery_approval_subject(subject: str) -> bool:
    """
    Detect SPM-sent 'Request for Delivery Approval' emails.
    These may be addressed to NIGEC, the warehouse, or both — no recipient filter.
    """
    return "request for delivery" in subject.lower()


def process_nlng_spm_po_send(client_db, subject: str, email_date: str,
                              message_id: str, sender: str, msg=None) -> None:
    """
    Process an email where SPM sent a PO to Flexitallic.
    One email may cover multiple NLNG PO numbers (Format B subjects).
    Stamps spm_po_sent_at for each. Latest date always wins (date-guard).
    If msg is provided, the PDF attachment filename is checked first — it
    is more reliable than the subject when the email is part of a reply chain.
    """
    pairs = sync.is_nlng_spm_po_subject(subject)
    if not pairs:
        return
    # Determine effective SPM PO — attachment filename overrides subject for revisions
    attachment_spm_po = sync.spm_po_from_attachment(msg) if msg is not None else None
    base_spm_po = pairs[0][0]
    effective_spm_po = attachment_spm_po if attachment_spm_po else base_spm_po
    if attachment_spm_po and attachment_spm_po != base_spm_po:
        print(f"  ⚠️  subject has {base_spm_po} but PDF says {attachment_spm_po} — using PDF")
    # Collect all NLNG POs: from subject + from attachment filename (handles
    # the case where the PDF covers an order not named in the email subject)
    subject_nlng_pos = [nlng_po for _, nlng_po in pairs]
    attachment_nlng_pos = sync.nlng_pos_from_attachment(msg) if msg is not None else []
    all_nlng_pos = list(dict.fromkeys(subject_nlng_pos + attachment_nlng_pos))
    stamped_ids = []
    missing = []
    for nlng_po in all_nlng_pos:
        order = sync.find_nlng_order_by_po(nlng_po)
        if not order:
            print(f"  ⚠️  SPM PO {effective_spm_po} sent to Flexitallic but NLNG order {nlng_po} not yet in DB")
            missing.append(f"{effective_spm_po}/{nlng_po}")
            continue
        sync.stamp_nlng_spm_po(order["id"], effective_spm_po, email_date)
        stamped_ids.append(order["id"])
        print(f"  📋 NLNG {nlng_po} — SPM PO {effective_spm_po} sent to Flexitallic ({email_date[:10]})")
    result = "nlng_spm_po_stamped" if stamped_ids else "nlng_spm_po_no_order"
    notes = []
    if stamped_ids:
        notes.append(f"stamped {len(stamped_ids)} order(s)")
    if missing:
        notes.append(f"missing: {', '.join(missing)}")
    client_db.table("processed_emails").upsert({
        "message_id": message_id, "sender": sender, "subject": subject,
        "processing_result": result,
        "raw_notes": "; ".join(notes),
    }, on_conflict="message_id").execute()


def _spm_po_from_body(body: str) -> str | None:
    """Extract 'S.P.M.-3071' from Flexitallic email body text."""
    m = re.search(r"S\.P\.M\.?\s*[-–.]\s*([\d.]+)", body, re.IGNORECASE)
    if not m:
        return None
    return f"S.P.M.-{m.group(1).rstrip('.')}"


def process_flexitallic_so(client_db, msg: email.message.Message,
                            subject: str, email_date: str,
                            message_id: str, sender: str,
                            body_text: str | None = None) -> None:
    """
    Process a Flexitallic SO acknowledgement email.
    The SPM PO number is in the email BODY ("Your PO number - S.P.M.-3071.-NLNG-4200."),
    not in the subject header. Falls back to NLNG PO search if SPM PO not found.
    """
    parsed = sync.is_flex_so_subject(subject)
    if not parsed:
        return
    so_number, spm_po = parsed

    # SPM PO not in subject — look in body
    if not spm_po and body_text:
        spm_po = _spm_po_from_body(body_text)

    orders = sync.find_all_nlng_orders_by_spm_po(spm_po) if spm_po else []

    # Last resort: NLNG PO number in body
    if not orders and body_text:
        nlng_m = re.search(r"\b(4200\d{5,6})\b", body_text)
        if nlng_m:
            single = sync.find_nlng_order_by_po(nlng_m.group(1))
            if single:
                orders = [single]

    if not orders:
        # Not NLNG — Chevron or other order; skip silently unless spm_po was found
        if spm_po:
            print(f"  ⚠️  Flexitallic SO {so_number} (spm_po={spm_po}) — no matching NLNG order")
            client_db.table("processed_emails").upsert({
                "message_id": message_id, "sender": sender, "subject": subject,
                "processing_result": "flexitallic_so_no_order",
                "raw_notes": f"SO {so_number}, SPM PO {spm_po}",
            }, on_conflict="message_id").execute()
        return
    # Save, upload, and parse the SO PDF (same as Chevron — parse_so_pdf extracts dispatch dates)
    so_pdf_url = None
    promised_date = None
    try:
        so_pdf_path = _save_so_pdf(msg, so_number)
        if so_pdf_path:
            from storage import upload_pdf
            so_pdf_url = upload_pdf(so_pdf_path, "so", so_number)
            try:
                from supplier_po_parser import parse_so_pdf
                pdf_data = parse_so_pdf(so_pdf_path)
                line_items = pdf_data.get("line_items", [])
                dates = sorted(li["despatch_date"] for li in line_items if li.get("despatch_date"))
                if dates:
                    promised_date = dates[0]
                if line_items:
                    client_db.table("so_line_items").delete().eq("so_number", so_number).execute()
                    client_db.table("so_line_items").insert([{
                        "so_number":      so_number,
                        "line_no":        li.get("line_no"),
                        "item_number":    li.get("item_number"),
                        "despatch_date":  li.get("despatch_date"),
                        "qty":            li.get("qty"),
                        "uom":            li.get("uom"),
                        "unit_price":     li.get("unit_price"),
                        "extended_price": li.get("extended_price"),
                    } for li in line_items]).execute()
            except Exception as _pe:
                print(f"  Warning: SO PDF parse failed for {so_number}: {_pe}")
    except Exception as _e:
        print(f"  Warning: SO PDF save/upload failed for {so_number}: {_e}")

    po_names = ", ".join(o["po_number"] for o in orders)
    for order in orders:
        sync.stamp_nlng_so(order["id"], so_number, email_date, so_pdf_url=so_pdf_url,
                           promised_date=promised_date)
        print(f"  📦 NLNG {order['po_number']} — Flexitallic SO {so_number} received ({email_date[:10]})")
    client_db.table("processed_emails").upsert({
        "message_id": message_id, "sender": sender, "subject": subject,
        "processing_result": "flexitallic_so_stamped",
        "raw_notes": f"SO {so_number} for NLNG {po_names}",
    }, on_conflict="message_id").execute()



def get_email_body_text(msg: email.message.Message) -> str:
    """Extract plain text body from an email message, handling multipart."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(errors="replace")
    return ""


def process_spm_delivery_approval(client_db, msg: email.message.Message,
                                   subject: str, email_date: str,
                                   message_id: str, sender: str) -> None:
    """
    Process an SPM-sent 'Request for Delivery Approval' email.
    Stamps delivery_requested_at (first write only) for every Chevron PO found
    in the subject and body.  Recipients are not checked — emails may go to
    NIGEC, the warehouse, or both.
    """
    body_text = get_email_body_text(msg)
    all_text = subject + " " + body_text
    po_numbers = sync.extract_all_po_numbers(all_text)

    if not po_numbers:
        client_db.table("processed_emails").upsert({
            "message_id": message_id, "sender": sender, "subject": subject,
            "processing_result": "delivery_approval_no_po",
            "raw_notes": "SPM delivery approval but no PO numbers found",
        }, on_conflict="message_id").execute()
        print(f"⚠️  SPM delivery approval with no PO numbers: '{subject[:60]}'")
        return

    stamped = 0
    for po in po_numbers:
        order = find_order_by_po_number(po)
        if not order:
            continue
        get_client().table("orders").update({
            "delivery_requested_at": email_date,
        }).eq("id", order["id"]).is_("delivery_requested_at", "null").execute()
        sync.advance_status(get_client(), order["id"], "delivery_requested")
        stamped += 1

    client_db.table("processed_emails").upsert({
        "message_id": message_id, "sender": sender, "subject": subject,
        "processing_result": "delivery_requested",
        "raw_notes": (
            f"SPM delivery approval: {len(po_numbers)} PO(s) found, "
            f"{stamped} order(s) stamped delivery_requested"
        ),
    }, on_conflict="message_id").execute()
    print(f"📋 SPM delivery approval {email_date[:10]}: "
          f"{stamped}/{len(po_numbers)} PO(s) → delivery_requested")


def stamp_sent_to_warehouse(order_id: str, email_date: str, body_text: str = None) -> None:
    """Stamp when the PO was actually sent to warehouse — use real email date."""
    client = get_client()
    update = {"sent_to_warehouse_at": email_date}
    if body_text:
        update["warehouse_routing_raw"] = body_text.strip()
    client.table("orders").update(update).eq("id", order_id).execute()
    # Monotonic: a re-processed routing email must not regress a later stage.
    sync.advance_status(client, order_id, "awaiting_warehouse_stock_check")

def process_message(client_db, msg_data: dict) -> None:
    """
    Process a single email from Gmail.

    IMPORTANT — the real-world flow (corrected): when SPM acknowledges a PO
    on the GEP portal, they download the GEP replica PDF (which DOES carry the
    'Supplier Acknowledged on' date) and email it to the warehouse. So the
    warehouse-routing email and the acknowledgment PDF are the SAME email,
    not two separate ones.

    This function does BOTH things on a single email when they apply.

    Acknowledgment is stamped BEFORE the warehouse stamp so the final
    overall_status reflects the later stage (awaiting_warehouse_stock_check)
    rather than regressing to 'acknowledged'.
    """
    raw_email = msg_data[b"RFC822"]
    msg = email.message_from_bytes(raw_email)

    message_id = msg.get("Message-ID", "")
    if not message_id:
        message_id = f"{msg.get('From')}-{msg.get('Date')}-{msg.get('Subject')}"

    if is_already_processed(message_id):
        return

    subject = decode_mime_words(msg.get("Subject", ""))
    sender = decode_mime_words(msg.get("From", ""))
    email_date = sync.parse_email_date(msg)

    notes: list[str] = []
    matched_order_id = None
    result = "no_match"

    # ------------------------------------------------------------
    # Is this the warehouse-routing email? Compute ONCE, up front.
    # Because we now search '[Gmail]/All Mail' (to catch SENT routing
    # emails), the result set includes every RFQ, bid and quote SPM has
    # ever sent. Running Claude PDF extraction on all of those would be
    # slow and burn API credits for no reason. The routing email and the
    # GEP ack PDF are the SAME email, so we only do the expensive Claude
    # extraction when this is genuinely a routing email.
    # ------------------------------------------------------------
    is_routing = is_warehouse_routing_email(msg, sender, subject)
    routing_body = get_email_body_text(msg).strip() if is_routing else None
    ack_info = None  # set by Claude in Part A; stays None if no PDF / Claude fails

    # ------------------------------------------------------------
    # Part A — acknowledgment PDF extraction.
    # The GEP replica PDF (with the acknowledged date) is attached to the
    # routing email, so we only attempt Claude extraction for routing emails.
    # ------------------------------------------------------------
    po_match_subject = re.search(r"(\d{8,12})", subject)
    po_hint = po_match_subject.group(1) if po_match_subject else f"unknown_{int(time.time())}"
    pdf_path = save_and_overwrite_pdf(msg, po_hint) if is_routing else None

    if pdf_path:
        try:
            ack_info = extract_ack_info_with_claude(pdf_path)
        except Exception as e:
            ack_info = None
            result = "error"
            notes.append(f"Claude extraction failed: {e}")
            print(f"❌ Failed to extract acknowledgment info from {subject}: {e}")

        if ack_info:
            po_number = ack_info.get("po_number")
            ack_date = ack_info.get("supplier_acknowledged_on")

            if not po_number:
                notes.append("PDF present but no PO number could be extracted from it")
                print(f"⚠️  Could not find a PO number in PDF from email: {subject}")
            else:
                order = find_order_by_po_number(po_number)
                if not order:
                    notes.append(
                        f"PO {po_number} found in PDF but no matching order exists "
                        f"(may not be a GEP ack PDF, or Stage 1 hasn't processed it yet)"
                    )
                    print(f"ℹ️  PO {po_number} in PDF '{subject}' but no matching order yet — skipped.")
                elif not ack_date:
                    matched_order_id = order["id"]
                    result = "skipped"
                    notes.append(
                        f"PO {po_number} matched but PDF has no "
                        f"'Supplier Acknowledged on' date — not the GEP export."
                    )
                    print(f"ℹ️  PO {po_number} PDF has no acknowledged date — skipped (not the GEP export).")
                else:
                    stamp_acknowledgment(order["id"], ack_date, pdf_path)
                    matched_order_id = order["id"]
                    result = "created_order"
                    notes.append(f"Acknowledged on {ack_date}")
                    print(f"✅ PO {po_number} acknowledgment confirmed — acknowledged date: {ack_date}")
                    ack_url = upload_ack_pdf(pdf_path, po_number)
                    if ack_url:
                        from db import get_client as _gc
                        _gc().table("orders").update({"ack_pdf_url": ack_url}).eq("id", order["id"]).execute()

    # ------------------------------------------------------------
    # Part B — warehouse-routing detection.
    # Business rule: if the PO has been sent to warehouse, it has
    # already been acknowledged in the GEP portal. This is the
    # trigger to stamp acknowledgment status even when Claude isn't
    # available to extract the exact acknowledged_at date.
    # Runs AFTER the ack stamp so final status is not regressed.
    # ------------------------------------------------------------
    if is_routing:
        # ── NLNG routing email ────────────────────────────────────────────────
        nlng_po = sync.is_nlng_po_subject(subject)
        if nlng_po:
            nlng_order = sync.find_nlng_order_by_po(nlng_po)
            if nlng_order:
                sync.stamp_nlng_sent_to_warehouse(nlng_order["id"], email_date, routing_body)
                result = "created_order"
                notes.append(f"NLNG warehouse routing stamped for PO {nlng_po}")
                print(f"📤 NLNG PO {nlng_po} sent to warehouse for stock check.")
            else:
                # NLNG PO notification not yet processed — park for later
                sync.park_email(
                    message_id=message_id,
                    kind="nlng_warehouse_routing",
                    po_number=nlng_po,
                    sender=sender,
                    subject=subject,
                    email_date=email_date,
                    pdf_path=None,
                    body_text=routing_body,
                    needs_claude=False,
                )
                result = "parked"
                notes.append(f"NLNG routing parked for PO {nlng_po} (order not yet created)")
                print(f"🅿️  NLNG PO {nlng_po} routing parked — will link when PO email arrives.")

        else:
            # ── Chevron routing email ─────────────────────────────────────────
            # Collect PO numbers from BOTH the subject and the PDF attachment —
            # always merge both sources so nothing is skipped when the PO only
            # appears in one of them.
            subject_pos = sync.extract_all_po_numbers(subject)
            attachment_pos = sync.chevron_pos_from_attachment(msg)
            all_chevron_pos = list(dict.fromkeys(subject_pos + attachment_pos))
            for po_number in all_chevron_pos:
                order = find_order_by_po_number(po_number)
                if order:
                    # Acknowledgment is implied by warehouse routing.
                    # Only skip if already acknowledged by PDF extraction above.
                    if order.get("acknowledgment_status") != "acknowledged":
                        client = get_client()
                        client.table("orders").update(
                            {
                                "acknowledgment_status": "acknowledged",
                                "acknowledged_by": "warehouse routing (auto-detected)",
                                # acknowledged_at left null — requires Claude PDF extraction
                            }
                        ).eq("id", order["id"]).execute()

                    stamp_sent_to_warehouse(order["id"], email_date, routing_body)
                    matched_order_id = order["id"]
                    result = "created_order"
                    notes.append(f"Warehouse routing email — sent_to_warehouse stamped for PO {po_number}")
                    print(f"📤 PO {po_number} sent to warehouse for stock check.")
                else:
                    # Cross-mailbox race: the Yahoo PO notification hasn't been
                    # processed yet. PARK this routing email; reconcile_po() will
                    # apply it the moment Stage 1 creates the order. needs_claude
                    # records that the exact ack date still needs PDF extraction.
                    sync.park_email(
                        message_id=message_id,
                        kind="warehouse_routing",
                        po_number=po_number,
                        sender=sender,
                        subject=subject,
                        email_date=email_date,
                        pdf_path=pdf_path,
                        body_text=routing_body,
                        needs_claude=(ack_info is None),
                    )
                    result = "parked"
                    notes.append(f"Warehouse routing email parked for PO {po_number} (order not created yet)")
                    print(f"🅿️  PO {po_number} routing email parked — will link when its PO notification arrives.")

    # ------------------------------------------------------------
    # Part C — NLNG SO forwarded to warehouse.
    # SPM receives the Flexitallic SO and forwards it to warehouse.
    # Subject is "Fwd: Flexitallic Sales Acknowledgement for SO714770…"
    # Does NOT hit Part B's is_routing check (subject is not a bare PO).
    # ------------------------------------------------------------
    if not is_routing and is_nlng_so_to_warehouse(msg, sender, subject):
        so_result = sync.is_flex_so_subject(subject)
        if so_result:
            so_number, spm_po = so_result
            nlng_orders = sync.find_all_nlng_orders_by_spm_po(spm_po) if spm_po else []
            if nlng_orders:
                for nlng_order in nlng_orders:
                    sync.stamp_nlng_so_to_warehouse(nlng_order["id"], email_date, so_number=so_number)
                    print(f"  📤 NLNG {nlng_order['po_number']} — SO {so_number} forwarded to warehouse ({email_date[:10]})")
                result = "created_order"
                po_names = ", ".join(o["po_number"] for o in nlng_orders)
                notes.append(f"NLNG SO {so_number} forwarded to warehouse for {po_names}")
            else:
                notes.append(f"SO {so_number} to warehouse — no NLNG order found, parked (spm_po={spm_po})")
                print(f"  ⚠️  SO {so_number} forwarded to warehouse but no matching NLNG order")
                if spm_po:
                    sync.park_email(
                        message_id=message_id,
                        kind="nlng_so_to_warehouse",
                        po_number=spm_po,
                        sender=sender,
                        subject=subject,
                        email_date=email_date,
                    )
                    print(f"  🅿️  Parked nlng_so_to_warehouse for spm_po={spm_po}")

    # ------------------------------------------------------------
    # Nothing relevant at all.
    # ------------------------------------------------------------
    if not pdf_path and not notes:
        notes.append("No PDF attachment found, not a warehouse routing email")

    # ------------------------------------------------------------
    # Single processed_emails insert (unique on message_id).
    # ------------------------------------------------------------
    client_db.table("processed_emails").upsert(
        {
            "message_id": message_id,
            "sender": sender,
            "subject": subject,
            "matched_order_id": matched_order_id,
            "processing_result": result,
            "raw_notes": " | ".join(notes) if notes else None,
        }
    , on_conflict="message_id").execute()


def check_inbox_once() -> None:
    """Run a single pass: connect, check for relevant mail, disconnect."""
    print("   Checking Gmail inbox for warehouse routing emails...", end="", flush=True)
    client_db = get_client()
    imap = connect_to_gmail()

    try:
        # Cursor-based: process every routing email newer than our last UID,
        # exactly once. Search is FROM specialpiping TO warehouse in
        # '[Gmail]/All Mail' (routing emails are SENT, never in INBOX).
        # is_warehouse_routing_email() still does the final precise check.
        folder = os.environ.get("GMAIL_ACK_FOLDER", "[Gmail]/All Mail")
        new_ids, uidvalidity = sync.new_uids_since_cursor(
            imap, "gmail", folder,
            ["FROM", "specialpiping@gmail.com", "TO", "spmwarehouse22@gmail.com"],
        )

        if not new_ids:
            print(" (no new routing emails)")
        else:
            print(f" {len(new_ids)} new to check...")
        highest_done = sync.get_cursor("gmail", folder)["last_uid"]

        # Pre-filter by SUBJECT using ONE batched ENVELOPE fetch per chunk.
        # Routing-email subjects are the bare Chevron PO number (006#######),
        # so we only download the heavy RFC822 body for those. The cursor
        # guarantees forward-only progress, so we don't need a per-email
        # processed_emails query during backfill (process_message still
        # dedups by Message-ID as a safety net).
        ENV_BATCH = 500
        for i in range(0, len(new_ids), ENV_BATCH):
            chunk = new_ids[i : i + ENV_BATCH]
            envelopes = imap.fetch(chunk, ["ENVELOPE"])

            for uid in chunk:
                env = envelopes.get(uid, {}).get(b"ENVELOPE")
                subj = ""
                if env and env.subject:
                    subj = env.subject.decode("utf-8", errors="ignore")

                # Cheap subject gate: a routing email's subject is a bare Chevron
                # PO number, an NLNG PO subject, OR a Flexitallic SO forward
                # ("Fwd: Flexitallic Sales Acknowledgement for SO…").
                # is_warehouse_routing_email() and is_nlng_so_to_warehouse() do
                # the final strict checks inside process_message.
                if sync.is_bare_po_subject(subj) or sync.is_nlng_po_subject(subj) or sync.is_flex_so_subject(subj):
                    print(f"   📥 Routing candidate (UID {uid}) — {subj!r}. Fetching...")
                    msg_data = imap.fetch([uid], ["RFC822"])
                    if uid in msg_data:
                        process_message(client_db, msg_data[uid])

                highest_done = max(highest_done, uid)

            # One cursor write per chunk (crash-safe enough, far less DB churn).
            sync.set_cursor("gmail", folder, highest_done, uidvalidity)
            print(f"   ...processed {min(i + ENV_BATCH, len(new_ids))}/{len(new_ids)}")

        # ── NLNG PO arrival emails ────────────────────────────────────────────
        # NLNG POs come from enquiry@specialpipingltd.com (webmail forwards them
        # into Gmail). Subject format: "PO No. 4200083212" or "Fwd: PO No. ..."
        # Uses a separate cursor so it advances independently of routing emails.
        print("   Checking Gmail for NLNG PO arrivals...", end="", flush=True)
        nlng_folder = "[Gmail]/All Mail"
        nlng_new_ids, nlng_uidvalidity = sync.new_uids_since_cursor(
            imap, "gmail_nlng", nlng_folder,
            ["FROM", NLNG_PO_SENDER],
        )

        if not nlng_new_ids:
            print(" (none)")
        else:
            print(f" {len(nlng_new_ids)} new to check...")
            nlng_highest = sync.get_cursor("gmail_nlng", nlng_folder)["last_uid"]

            for i in range(0, len(nlng_new_ids), ENV_BATCH):
                chunk = nlng_new_ids[i : i + ENV_BATCH]
                envelopes = imap.fetch(chunk, ["ENVELOPE"])

                for uid in chunk:
                    env = envelopes.get(uid, {}).get(b"ENVELOPE")
                    subj = ""
                    if env and env.subject:
                        subj = env.subject.decode("utf-8", errors="ignore")

                    # Only download emails whose subject is an NLNG PO subject
                    if sync.is_nlng_po_subject(subj):
                        print(f"   📥 NLNG PO candidate (UID {uid}) — {subj!r}. Fetching...")
                        msg_data = imap.fetch([uid], ["RFC822"])
                        raw = msg_data.get(uid)
                        if raw:
                            raw_email = raw[b"RFC822"]
                            msg_obj = email.message_from_bytes(raw_email)
                            msg_id = msg_obj.get("Message-ID", "")
                            if not msg_id:
                                msg_id = f"{msg_obj.get('From')}-{msg_obj.get('Date')}-{msg_obj.get('Subject')}"
                            sender_val = decode_mime_words(msg_obj.get("From", ""))
                            subj_val = decode_mime_words(msg_obj.get("Subject", ""))
                            # Dedup check
                            dup = (
                                client_db.table("processed_emails")
                                .select("id").eq("message_id", msg_id).execute()
                            )
                            if not dup.data:
                                process_nlng_po_email(client_db, msg_obj, msg_id, sender_val, subj_val)

                    nlng_highest = max(nlng_highest, uid)

                sync.set_cursor("gmail_nlng", nlng_folder, nlng_highest, nlng_uidvalidity)
                print(f"   ...checked {min(i + ENV_BATCH, len(nlng_new_ids))}/{len(nlng_new_ids)} NLNG")

        # ── NLNG SPM PO sends to Flexitallic ─────────────────────────────────
        # Subject: "PURCHASE ORDER-S.P.M. - 3071.-NLNG-4200083212- FLEXITALLIC"
        # FROM specialpiping TO Flexitallic connector (NOT to warehouse).
        # These are SENT emails so they live in [Gmail]/All Mail.
        print("   Checking Gmail for NLNG SPM PO sends to Flexitallic...", end="", flush=True)
        imap.select_folder("[Gmail]/All Mail")
        spm_folder = "[Gmail]/All Mail"
        spm_new_ids, spm_uidvalidity = sync.new_uids_since_cursor(
            imap, "gmail_nlng_flexsend", spm_folder,
            ["FROM", SPM_SENDER, "SUBJECT", "FLEXITALLIC"],
        )
        if not spm_new_ids:
            print(" (none)")
        else:
            print(f" {len(spm_new_ids)} new to check...")
            spm_highest = sync.get_cursor("gmail_nlng_flexsend", spm_folder)["last_uid"]
            for i in range(0, len(spm_new_ids), ENV_BATCH):
                chunk = spm_new_ids[i:i + ENV_BATCH]
                envelopes = imap.fetch(chunk, ["ENVELOPE"])
                for uid in chunk:
                    env = envelopes.get(uid, {}).get(b"ENVELOPE")
                    subj = env.subject.decode("utf-8", errors="ignore") if (env and env.subject) else ""
                    if sync.is_nlng_spm_po_subject(subj):
                        print(f"   📥 SPM→Flexitallic PO (UID {uid}) — {subj!r}. Fetching...")
                        raw_map = imap.fetch([uid], ["RFC822"])
                        raw = raw_map.get(uid)
                        if raw:
                            msg_obj = email.message_from_bytes(raw[b"RFC822"])
                            msg_id = msg_obj.get("Message-ID", "") or \
                                     f"{msg_obj.get('From')}-{msg_obj.get('Date')}-flexsend"
                            if not is_already_processed(msg_id):
                                process_nlng_spm_po_send(
                                    client_db,
                                    decode_mime_words(msg_obj.get("Subject", "")),
                                    sync.parse_email_date(msg_obj),
                                    msg_id,
                                    decode_mime_words(msg_obj.get("From", "")),
                                    msg=msg_obj,
                                )
                    spm_highest = max(spm_highest, uid)
                sync.set_cursor("gmail_nlng_flexsend", spm_folder, spm_highest, spm_uidvalidity)
                print(f"   ...checked {min(i + ENV_BATCH, len(spm_new_ids))}/{len(spm_new_ids)} SPM→Flex")

        # ── SPM delivery approval emails ──────────────────────────────────────
        # FROM specialpiping, subject "Request for Delivery Approval - PO XXXXXXXXXX"
        # Addressed to NIGEC, warehouse, or both — no TO filter used.
        # Lives in Sent / All Mail (outbound email). Stamps delivery_requested_at.
        print("   Checking Gmail for SPM delivery approval emails...", end="", flush=True)
        imap.select_folder("[Gmail]/All Mail")
        deliv_folder = "[Gmail]/All Mail"
        deliv_new_ids, deliv_uidvalidity = sync.new_uids_since_cursor(
            imap, "gmail_delivery_approval", deliv_folder,
            ["FROM", SPM_SENDER, "SUBJECT", "Request for Delivery"],
        )
        if not deliv_new_ids:
            print(" (none)")
        else:
            print(f" {len(deliv_new_ids)} new to check...")
            deliv_highest = sync.get_cursor("gmail_delivery_approval", deliv_folder)["last_uid"]
            for i in range(0, len(deliv_new_ids), ENV_BATCH):
                chunk = deliv_new_ids[i:i + ENV_BATCH]
                envelopes = imap.fetch(chunk, ["ENVELOPE"])
                for uid in chunk:
                    env = envelopes.get(uid, {}).get(b"ENVELOPE")
                    subj = env.subject.decode("utf-8", errors="ignore") if (env and env.subject) else ""
                    if is_spm_delivery_approval_subject(subj):
                        print(f"   📥 SPM delivery approval (UID {uid}) — {subj!r}. Fetching...")
                        raw_map = imap.fetch([uid], ["RFC822"])
                        raw = raw_map.get(uid)
                        if raw:
                            msg_obj = email.message_from_bytes(raw[b"RFC822"])
                            msg_id = msg_obj.get("Message-ID", "") or \
                                     f"{msg_obj.get('From')}-{msg_obj.get('Date')}-delivapproval"
                            if not is_already_processed(msg_id):
                                process_spm_delivery_approval(
                                    client_db,
                                    msg_obj,
                                    decode_mime_words(msg_obj.get("Subject", "")),
                                    sync.parse_email_date(msg_obj),
                                    msg_id,
                                    decode_mime_words(msg_obj.get("From", "")),
                                )
                    deliv_highest = max(deliv_highest, uid)
                sync.set_cursor("gmail_delivery_approval", deliv_folder, deliv_highest, deliv_uidvalidity)
                print(f"   ...checked {min(i + ENV_BATCH, len(deliv_new_ids))}/{len(deliv_new_ids)} delivery approvals")

        # ── Flexitallic SO acknowledgements (Gmail INBOX) ─────────────────────
        # FROM salesorder@flexitallic.eu; subject: "Flexitallic Sales Acknowledgement for SO714770…"
        print("   Checking Gmail for Flexitallic SO acknowledgements...", end="", flush=True)
        imap.select_folder("INBOX")
        flex_folder = "INBOX"
        flex_new_ids, flex_uidvalidity = sync.new_uids_since_cursor(
            imap, "gmail_flexitallic", flex_folder,
            ["FROM", FLEXITALLIC_SENDER],
        )
        if not flex_new_ids:
            print(" (none)")
        else:
            print(f" {len(flex_new_ids)} new to check...")
            flex_highest = sync.get_cursor("gmail_flexitallic", flex_folder)["last_uid"]
            for i in range(0, len(flex_new_ids), ENV_BATCH):
                chunk = flex_new_ids[i:i + ENV_BATCH]
                envelopes = imap.fetch(chunk, ["ENVELOPE"])
                for uid in chunk:
                    env = envelopes.get(uid, {}).get(b"ENVELOPE")
                    subj = env.subject.decode("utf-8", errors="ignore") if (env and env.subject) else ""
                    if sync.is_flex_so_subject(subj):
                        print(f"   📥 Flexitallic SO (UID {uid}) — {subj!r}. Fetching...")
                        raw_map = imap.fetch([uid], ["RFC822"])
                        raw = raw_map.get(uid)
                        if raw:
                            msg_obj = email.message_from_bytes(raw[b"RFC822"])
                            msg_id = msg_obj.get("Message-ID", "") or \
                                     f"{msg_obj.get('From')}-{msg_obj.get('Date')}-flexso"
                            if not is_already_processed(msg_id):
                                process_flexitallic_so(
                                    client_db,
                                    msg_obj,
                                    decode_mime_words(msg_obj.get("Subject", "")),
                                    sync.parse_email_date(msg_obj),
                                    msg_id,
                                    decode_mime_words(msg_obj.get("From", "")),
                                    body_text=get_email_body_text(msg_obj),
                                )
                    flex_highest = max(flex_highest, uid)
                sync.set_cursor("gmail_flexitallic", flex_folder, flex_highest, flex_uidvalidity)
                print(f"   ...checked {min(i + ENV_BATCH, len(flex_new_ids))}/{len(flex_new_ids)} Flex SO")

    finally:
        imap.logout()


def run_forever() -> None:
    """Continuously check the Gmail inbox at a fixed interval, forever."""
    interval = int(os.environ.get("CHECK_INTERVAL_SECONDS", 120))
    print(f"📬 SPM Gmail acknowledgment listener started. Checking every {interval} seconds. "
          f"Press Ctrl+C to stop.")

    while True:
        try:
            check_inbox_once()
        except Exception as e:
            print(f"\n❌ Error during inbox check: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    run_forever()

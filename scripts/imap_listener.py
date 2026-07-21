"""
imap_listener.py — Stage 1: Watch Yahoo Mail for Chevron PO notifications.

What this does:
1. Connects to the Yahoo inbox via IMAP.
2. Checks for new emails since the last check.
3. For each new email, checks if it's a "Chevron.Notification" email
   (or any sender we recognize from the `buyers` table).
4. If it matches and hasn't been processed before:
   - Extracts the PO number, JDE Job ID, branch plant, supplier ref,
     and amount from the email body using simple text parsing.
   - Saves the PDF attachment to disk.
   - Creates a new row in `orders` with status 'pending_acknowledgment'.
   - Logs the email in `processed_emails` so it's never re-processed.
5. Repeats every CHECK_INTERVAL_SECONDS forever (runs in the background).

Run this with:  python scripts/imap_listener.py
"""

import os
import re
import sys
import time
import email
from email.header import decode_header
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from imapclient import IMAPClient

from db import get_client, normalize_po_number
import sync

# Windows' default console codepage (cp1252) can't encode the emoji and
# em-dash characters used in this script's status output, which would crash
# the listener the moment it hits a new PO. Force UTF-8 on stdout/stderr.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

load_dotenv()

ATTACHMENTS_DIR = Path(__file__).parent.parent / "data" / "po_attachments"
ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)


def connect_to_yahoo() -> IMAPClient:
    """Open an IMAP connection to the Yahoo inbox."""
    host = os.environ.get("YAHOO_IMAP_HOST", "imap.mail.yahoo.com")
    port = int(os.environ.get("YAHOO_IMAP_PORT", 993))
    email_addr = os.environ["YAHOO_EMAIL"]
    app_password = os.environ["YAHOO_APP_PASSWORD"]

    client = IMAPClient(host, port=port, use_uid=True, ssl=True)
    client.login(email_addr, app_password)
    client.select_folder("INBOX")
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


def get_known_buyer_senders() -> dict[str, dict]:
    """
    Fetch the list of buyers and their notification sender names
    from the database.
    """
    client = get_client()
    result = client.table("buyers").select("*").execute()
    senders = {}
    for buyer in result.data:
        sender_name = buyer.get("notification_email_sender")
        if sender_name:
            senders[sender_name.lower()] = buyer
    return senders


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


def extract_po_fields(body_text: str, subject: str = "") -> dict:
    """
    Pull the structured fields out of the Chevron notification email body.

    Handles the standard "pending for acknowledgement" template. The
    supplier-ref terminator now matches a quote OR a paren, so quoted
    templates don't leak a trailing " (which became &quot;) into the field.

    PO number capture keeps the revision suffix (e.g. '0060792432-001').
    A change-order notification lists BOTH numbers — 'From (0060792432)' and
    'To (0060792432-001)' — so we must take the 'To' target, not the first
    match. Preference order: the change-order 'To (...)' target, then the
    subject's parenthesised PO, then any parenthesised PO in the body.
    """
    fields = {
        "jde_job_id": None,
        "branch_plant": None,
        "supplier_ref_number": None,
        "buyer_po_number": None,
        "po_amount": None,
    }

    jde_match = re.search(r"JDEJobID\s+(\S+?),", body_text)
    if jde_match:
        fields["jde_job_id"] = jde_match.group(1)

    branch_match = re.search(r"BranchPlant\s+(\S+?),", body_text)
    if branch_match:
        fields["branch_plant"] = branch_match.group(1)

    # Terminate on a quote, single-quote, or opening paren so the quote
    # char is never swallowed into the captured ref.
    supplier_match = re.search(r"Supplier\s+(\S+?)\s*[\"'\(]", body_text)
    if supplier_match:
        ref = supplier_match.group(1)
        ref = ref.replace("&quot;", "").replace('"', "").replace("'", "").strip()
        fields["supplier_ref_number"] = ref or None

    # PO number, with optional "-NNN" revision suffix. Prefer the change-order
    # "To (...)" target, then the subject, then any parenthesised body match.
    po_with_rev = r"\((\d{8,12}(?:\s*-\s*\d{1,3})?)\)"
    po_raw = None
    to_match = re.search(r"To\s*" + po_with_rev, body_text)
    subj_match = re.search(po_with_rev, subject or "")
    body_match = re.search(po_with_rev, body_text)
    if to_match:
        po_raw = to_match.group(1)
    elif subj_match:
        po_raw = subj_match.group(1)
    elif body_match:
        po_raw = body_match.group(1)
    fields["buyer_po_number"] = normalize_po_number(po_raw)

    amount_match = re.search(r"amount\s*\$\s*([\d,]+\.?\d*)", body_text)
    if amount_match:
        fields["po_amount"] = float(amount_match.group(1).replace(",", ""))
    else:
        # Some templates phrase it "for $1,990.48" instead of "amount $ ..."
        alt = re.search(r"for\s*\$\s*([\d,]+\.?\d*)", body_text)
        if alt:
            fields["po_amount"] = float(alt.group(1).replace(",", ""))

    return fields


def get_email_body_text(msg: email.message.Message) -> str:
    """Extract plain text body from an email message, handling multipart."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
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


def save_attachments(msg: email.message.Message, po_number: str) -> str | None:
    """
    Save the PO PDF attachment to disk.
    When multiple PDFs are attached, prefer the one whose filename
    matches the PO number — that's always the actual PO document.
    Falls back to the first PDF found if no name match.
    """
    if not msg.is_multipart():
        return None

    all_pdfs = []

    for part in msg.walk():
        content_disposition = str(part.get("Content-Disposition", ""))
        filename = part.get_filename()

        if filename:
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

    safe_filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", chosen_filename)
    save_path = ATTACHMENTS_DIR / f"{po_number}_{safe_filename}"
    save_path.write_bytes(chosen_payload)
    return str(save_path)


def upload_po_pdf(local_path: str, po_number: str) -> str | None:
    """Upload the PO PDF to Supabase Storage and return the public URL."""
    try:
        from storage import upload_pdf
        return upload_pdf(local_path, "po", po_number)
    except Exception as e:
        print(f"  Warning: storage upload failed for {po_number}: {e}")
        return None


def create_or_update_order(client_db, fields, buyer_id, email_date, pdf_path):
    """
    Idempotent order creation, keyed on buyer_po_number (which now carries any
    '-NNN' revision suffix, so a change order is a DISTINCT row, not an update).

    On a repeat notification for the SAME PO, refresh ONLY the Stage-1
    notification fields — never touch downstream fields (ack, warehouse, SPM PO,
    pricing) or overall_status. Backed by the unique constraint on
    buyer_po_number, with a 23505 fallback for overlapping-run races.

    Returns (order_id, was_created).
    """
    po_number = fields["buyer_po_number"]

    # Stage-1 fields a repeat notification may safely refresh, None-dropped so
    # we never overwrite good data with null. Note: pdf_attachment_path is
    # deliberately create-only — gmail_ack_listener overwrites it with the GEP
    # ack PDF, and a re-notification must not regress it to the Yahoo copy.
    refreshable = {
        "jde_job_id": fields.get("jde_job_id"),
        "branch_plant": fields.get("branch_plant"),
        "supplier_ref_number": fields.get("supplier_ref_number"),
        "po_amount": fields.get("po_amount"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    refreshable = {k: v for k, v in refreshable.items() if v is not None}

    existing = (
        client_db.table("orders").select("id").eq("buyer_po_number", po_number).execute()
    )
    if existing.data:
        order_id = existing.data[0]["id"]
        client_db.table("orders").update(refreshable).eq("id", order_id).execute()
        return order_id, False

    insert_row = {
        "buyer_id": buyer_id,
        "buyer_po_number": po_number,
        "notification_received_at": email_date,   # T0 — set once, never refreshed
        "pdf_attachment_path": pdf_path,           # create-only
        "acknowledgment_status": "pending",
        "overall_status": "pending_acknowledgment",
        **refreshable,
    }
    try:
        res = client_db.table("orders").insert(insert_row).execute()
        return res.data[0]["id"], True
    except Exception as e:
        # Lost a race with an overlapping run — the row now exists. Update it.
        if "23505" in str(e) or "duplicate key" in str(e).lower():
            again = (
                client_db.table("orders").select("id")
                .eq("buyer_po_number", po_number).execute()
            )
            order_id = again.data[0]["id"]
            client_db.table("orders").update(refreshable).eq("id", order_id).execute()
            return order_id, False
        raise


def process_message(client_db, msg_data: dict, known_senders: dict) -> None:
    """Process a single raw email message: check, parse, log, create order."""
    raw_email = msg_data[b"RFC822"]
    msg = email.message_from_bytes(raw_email)

    message_id = msg.get("Message-ID", "")
    if not message_id:
        message_id = f"{msg.get('From')}-{msg.get('Date')}-{msg.get('Subject')}"

    if is_already_processed(message_id):
        return

    sender_raw = decode_mime_words(msg.get("From", ""))
    subject = decode_mime_words(msg.get("Subject", ""))

    matched_buyer = None
    for sender_key, buyer in known_senders.items():
        if sender_key in sender_raw.lower():
            matched_buyer = buyer
            break

    if not matched_buyer:
        client_db.table("processed_emails").upsert(
            {
                "message_id": message_id,
                "sender": sender_raw,
                "subject": subject,
                "processing_result": "no_match",
            }
        , on_conflict="message_id").execute()
        return

    body_text = get_email_body_text(msg)

    # ── GATE: only genuine new-PO notifications create orders ──────
    # Chevron sends several notification templates for the same PO over
    # its life: "pending for acknowledgement" (new — the only one we want),
    # plus "is cancelled" and "has been closed" (terminal states for old
    # POs). The terminal templates use different PO-name formats our field
    # regexes can't parse, and the PO's original PDF isn't attached — so
    # they created empty dead rows. Only proceed for genuine new POs.
    lowered = body_text.lower()
    is_new_po = (
        "pending for acknowledgement" in lowered
        or "pending for acknowledgment" in lowered
    )
    is_terminal = (
        "has been closed" in lowered
        or "is cancelled" in lowered
        or "is canceled" in lowered
    )

    if is_terminal or not is_new_po:
        m = re.search(r"\((\d{8,12})\)", body_text)
        po_for_log = m.group(1) if m else None
        reason = "terminal (cancelled/closed)" if is_terminal else "not a new-PO notification"
        client_db.table("processed_emails").upsert(
            {
                "message_id": message_id,
                "sender": sender_raw,
                "subject": subject,
                "processing_result": "skipped_non_new_po",
                "raw_notes": f"Skipped PO {po_for_log}: {reason}",
            }
        , on_conflict="message_id").execute()
        print(f"⏭️  Skipped {po_for_log or subject[:40]} — {reason}.")
        return

    fields = extract_po_fields(body_text, subject)

    if not fields["buyer_po_number"]:
        client_db.table("processed_emails").upsert(
            {
                "message_id": message_id,
                "sender": sender_raw,
                "subject": subject,
                "processing_result": "error",
                "raw_notes": "Matched buyer but could not extract PO number from body",
            }
        , on_conflict="message_id").execute()
        print(f"⚠️  Could not extract PO number from email: {subject}")
        return

    pdf_path = save_attachments(msg, fields["buyer_po_number"])
    pdf_url  = upload_po_pdf(pdf_path, fields["buyer_po_number"]) if pdf_path else None

    # Use the real email send date, not the current processing time
    email_date = sync.parse_email_date(msg)

    order_id, was_created = create_or_update_order(
        client_db, fields, matched_buyer["id"], email_date, pdf_path
    )

    if pdf_url and order_id:
        client_db.table("orders").update({"pdf_url": pdf_url}).eq("id", order_id).execute()

    client_db.table("processed_emails").upsert(
        {
            "message_id": message_id,
            "sender": sender_raw,
            "subject": subject,
            "matched_order_id": order_id,
            "processing_result": "created_order" if was_created else "duplicate_notification",
        }
    , on_conflict="message_id").execute()

    if was_created:
        print(
            f"✅ New PO detected: {fields['buyer_po_number']} "
            f"(amount ${fields['po_amount']}) — "
            f"email dated {email_date[:10]} — awaiting acknowledgment. "
            f"PDF saved: {pdf_path or 'none found'}"
        )
    else:
        print(
            f"↩️  Repeat notification for {fields['buyer_po_number']} — "
            f"existing order refreshed (no duplicate, downstream data preserved)."
        )

    # Reconcile whether created OR already existing, so parked warehouse/SPM
    # emails still get applied on a repeat.
    applied = sync.reconcile_po(fields["buyer_po_number"])
    if applied:
        print(f"   🔗 Reconciled {applied} parked email(s) for PO {fields['buyer_po_number']}.")


def check_inbox_once() -> None:
    """Run a single pass: connect, check for new mail, process it, disconnect."""
    client_db = get_client()
    known_senders = get_known_buyer_senders()

    if not known_senders:
        print("⚠️  No buyers with notification_email_sender set in the database.")
        return

    imap = connect_to_yahoo()
    try:
        # Cursor-based: only fetch UIDs newer than the last one we processed.
        # On the very first run last_uid=0, so this backfills the whole inbox
        # once; afterwards it only ever sees genuinely new mail. This replaces
        # the old [-50:] tail that silently dropped older PO notifications.
        folder = "INBOX"
        # Yahoo's IMAP SINCE filter silently caps results at ~1000 recent UIDs,
        # missing older messages that genuinely exist in the inbox. Disable it
        # and rely solely on the UID cursor.
        new_ids, uidvalidity = sync.new_uids_since_cursor(
            imap, "yahoo", folder, ["ALL"], use_since=False
        )

        if not new_ids:
            print("No new messages.")
            return

        print(f"Processing {len(new_ids)} new message(s)...")
        # Fetch in batches to keep memory sane on a big first-run backfill.
        BATCH = 50
        highest_done = sync.get_cursor("yahoo", folder)["last_uid"]
        for i in range(0, len(new_ids), BATCH):
            chunk = new_ids[i : i + BATCH]
            messages = imap.fetch(chunk, ["RFC822"])
            for uid in chunk:
                msg_data = messages.get(uid)
                if msg_data:
                    process_message(client_db, msg_data, known_senders)
                # Advance cursor as we go so a crash mid-backfill doesn't
                # force re-processing everything from scratch.
                highest_done = max(highest_done, uid)
                sync.set_cursor("yahoo", folder, highest_done, uidvalidity)

    finally:
        imap.logout()


def run_forever() -> None:
    """Continuously check the inbox at a fixed interval, forever."""
    interval = int(os.environ.get("CHECK_INTERVAL_SECONDS", 120))
    print(
        f"📬 SPM IMAP listener started. Checking every {interval} seconds. "
        f"Press Ctrl+C to stop."
    )

    while True:
        try:
            check_inbox_once()
        except Exception as e:
            print(f"❌ Error during inbox check: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    run_forever()

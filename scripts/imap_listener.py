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
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from imapclient import IMAPClient

from db import get_client, normalize_po_number, base_po_number
import sync
from email_utils import decode_mime_words, get_email_body_text, is_already_processed, safe_filename
from config import IMAP_TIMEOUT

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

    # timeout: without it a half-open socket blocks this thread forever, and
    # worker.py's is_alive() check cannot tell that apart from healthy work.
    client = IMAPClient(host, port=port, use_uid=True, ssl=True, timeout=IMAP_TIMEOUT)
    client.login(email_addr, app_password)
    client.select_folder("INBOX")
    return client


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
        "po_currency": "USD",
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
        fields["po_currency"] = "USD"
    else:
        # Some templates phrase it "for $1,990.48" instead of "amount $ ..."
        alt = re.search(r"for\s*\$\s*([\d,]+\.?\d*)", body_text)
        if alt:
            fields["po_amount"] = float(alt.group(1).replace(",", ""))
            fields["po_currency"] = "USD"
        else:
            # NGN-denominated POs use ₦ or N followed by the amount
            ngn = re.search(r"amount\s*[₦N]\s*([\d,]+\.?\d*)", body_text, re.IGNORECASE)
            if not ngn:
                ngn = re.search(r"[₦N]\s*([\d,]+\.?\d*)", body_text)
            if ngn:
                fields["po_amount"] = float(ngn.group(1).replace(",", ""))
                fields["po_currency"] = "NGN"

    return fields


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

    # Shared helper: strips directory parts, replaces reserved characters,
    # and caps length — an over-long name would otherwise raise OSError and
    # wedge this batch on every future poll (the cursor saves after the loop).
    save_path = ATTACHMENTS_DIR / f"{po_number}_{safe_filename(chosen_filename)}"
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
    Idempotent order creation, keyed on the BASE buyer_po_number (revision
    suffix stripped). A change order notification (e.g. 0061340768-001)
    overwrites the original row (0061340768) rather than creating a new one.

    On any repeat notification for the SAME PO, refresh ONLY the Stage-1
    fields — never touch downstream fields (ack, warehouse, SPM PO, pricing)
    or overall_status. Backed by the unique constraint on buyer_po_number,
    with a 23505 fallback for overlapping-run races.

    Returns (order_id, was_created).
    """
    po_raw = fields["buyer_po_number"]          # may carry -001 suffix
    po_number = base_po_number(po_raw) or po_raw  # always the base
    is_change_order = po_number != po_raw

    # Stage-1 fields a repeat notification may safely refresh, None-dropped so
    # we never overwrite good data with null. Note: pdf_attachment_path is
    # deliberately create-only — gmail_ack_listener overwrites it with the GEP
    # ack PDF, and a re-notification must not regress it to the Yahoo copy.
    refreshable = {
        "jde_job_id": fields.get("jde_job_id"),
        "branch_plant": fields.get("branch_plant"),
        "supplier_ref_number": fields.get("supplier_ref_number"),
        "po_amount": fields.get("po_amount"),
        "po_currency": fields.get("po_currency") or "USD",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    refreshable = {k: v for k, v in refreshable.items() if v is not None}

    existing = (
        client_db.table("orders").select("id").eq("buyer_po_number", po_number).execute()
    )
    if existing.data:
        order_id = existing.data[0]["id"]
        client_db.table("orders").update(refreshable).eq("id", order_id).execute()
        if is_change_order:
            print(f"  ↪ Change order {po_raw} → overwrote base PO {po_number}.")
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

        # A cancellation is not noise to discard - it is the one notification
        # that has to move an order BACKWARDS, out of the live pipeline. These
        # were previously dropped here, which left nine cancelled orders still
        # running, one of them all the way at ready_for_dispatch.
        #
        # Only the cancellation is acted on. "has been closed" arrives for
        # orders we already delivered and needs no correction. The parser also
        # rejects sourcing-event cancellations, which carry the same wording
        # but no PO number - see cancellation_listener.parse_cancellation.
        if is_terminal:
            try:
                from cancellation_listener import handle_terminal_email
                outcome = handle_terminal_email(
                    client_db, subject, body_text, sync.parse_email_date(msg)
                )
                if outcome and outcome.get("outcome") == "updated":
                    client_db.table("processed_emails").upsert(
                        {
                            "message_id": message_id,
                            "sender": sender_raw,
                            "subject": subject,
                            "processing_result": "order_cancelled",
                            "raw_notes": (
                                f"PO {outcome['po']} cancelled at Chevron: "
                                f"{outcome['from_status']} -> {outcome['to_status']}"
                                f" (by {outcome.get('actor') or 'unknown'})"
                            ),
                        }, on_conflict="message_id").execute()
                    print(f"  Cancelled {outcome['po']}: "
                          f"{outcome['from_status']} -> {outcome['to_status']}")
                    return
            except Exception as exc:
                # Never let this stop the email being recorded below; an
                # unrecorded email is reprocessed forever.
                print(f"  [warn] cancellation handling failed: {exc}")

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
        _po_raw  = fields["buyer_po_number"]
        _base_po = base_po_number(_po_raw) or _po_raw
        _upd     = {"pdf_url": pdf_url}
        if _po_raw != _base_po:
            # Change order — new PDF arrived; clear stale extraction so the
            # extractor loop re-derives RDD/dates from the updated document.
            _upd["extraction_raw"]         = None
            _upd["required_delivery_date"] = None
        client_db.table("orders").update(_upd).eq("id", order_id).execute()

    client_db.table("processed_emails").upsert(
        {
            "message_id": message_id,
            "sender": sender_raw,
            "subject": subject,
            "matched_order_id": order_id,
            "processing_result": "created_order" if was_created else "duplicate_notification",
        }
    , on_conflict="message_id").execute()

    sync.upsert_email(
        message_id=message_id,
        order_id=order_id,
        direction="in",
        from_address=sender_raw,
        to_address=decode_mime_words(msg.get("To", "")),
        subject=subject,
        body_text=body_text,
        received_at=email_date,
        in_reply_to=msg.get("In-Reply-To"),
        thread_id=sync.email_thread_id(msg),
    )

    po_raw   = fields["buyer_po_number"]
    base_po  = base_po_number(po_raw) or po_raw

    if was_created:
        print(
            f"✅ New PO detected: {base_po} "
            f"(amount ${fields['po_amount']}) — "
            f"email dated {email_date[:10]} — awaiting acknowledgment. "
            f"PDF saved: {pdf_path or 'none found'}"
        )
    else:
        print(
            f"↩️  Repeat notification for {base_po} — "
            f"existing order refreshed (no duplicate, downstream data preserved)."
        )

    # Reconcile whether created OR already existing, so parked warehouse/SPM
    # emails still get applied on a repeat. Always use the base PO since
    # change orders no longer have their own rows.
    applied = sync.reconcile_po(base_po)
    if applied:
        print(f"   🔗 Reconciled {applied} parked email(s) for PO {base_po}.")


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
            messages = imap.fetch(chunk, ["RFC822", "INTERNALDATE"])
            for uid in chunk:
                msg_data = messages.get(uid)
                if not msg_data:
                    # The server returned nothing for this UID (expunged, or a
                    # transient hiccup). Say so: the cursor is allowed to move
                    # past it below, so without this line the message would be
                    # orphaned silently. rescan_and_catch_up() re-checks by
                    # date rather than cursor and is the backstop here.
                    print(f"⚠️  UID {uid} returned no data — skipped "
                          f"(daily re-scan will re-check it)", flush=True)
                    continue
                # Isolate each message: an unhandled error here would escape
                # before the cursor is saved below, so the next poll would
                # refetch this same message and fail identically — forever.
                try:
                    process_message(client_db, msg_data, known_senders)
                except Exception as exc:
                    sync.record_processing_failure(msg_data, exc, source="imap_listener")
            # Advance the cursor only past UIDs old enough that we're sure
            # the server has fully settled on them (see sync.safe_cursor_uid).
            safe_uid = sync.safe_cursor_uid(messages, chunk)
            if safe_uid is not None:
                highest_done = max(highest_done, safe_uid)
                sync.set_cursor("yahoo", folder, highest_done, uidvalidity)

    finally:
        imap.logout()


_RESCAN_INTERVAL_SECONDS = 24 * 3600  # once a day
_last_rescan_ts: datetime | None = None


def rescan_and_catch_up(days_back: int = 7) -> None:
    """
    Safety net for the cursor path above: ignore the stored cursor and
    re-check the last `days_back` days of INBOX mail. Anything already
    correctly processed is cheap to skip (is_already_processed); anything
    that fell through — a stalled/crashed poll, a brief server hiccup — gets
    processed now instead of staying silently missed forever.
    """
    client_db = get_client()
    known_senders = get_known_buyer_senders()
    if not known_senders:
        return

    imap = connect_to_yahoo()
    try:
        uids = sync.rescan_recent(imap, ["ALL"], days_back=days_back)
        if not uids:
            return
        print(f"🔁 Re-scan: checking {len(uids)} message(s) from the last {days_back} day(s) for anything missed...")
        recovered = 0
        BATCH = 50
        for i in range(0, len(uids), BATCH):
            chunk = uids[i:i + BATCH]
            messages = imap.fetch(chunk, ["RFC822"])
            for uid in chunk:
                msg_data = messages.get(uid)
                if not msg_data:
                    continue
                msg = email.message_from_bytes(msg_data[b"RFC822"])
                message_id = msg.get("Message-ID", "")
                if not message_id:
                    message_id = f"{msg.get('From')}-{msg.get('Date')}-{msg.get('Subject')}"
                if is_already_processed(message_id):
                    continue
                process_message(client_db, msg_data, known_senders)
                # process_message() can silently ignore mail from an unknown
                # sender without writing anything — stamp a placeholder so
                # tomorrow's re-scan doesn't re-fetch the same email again.
                sync.ensure_processed_row(
                    message_id, decode_mime_words(msg.get("From", "")),
                    decode_mime_words(msg.get("Subject", "")),
                )
                recovered += 1
        if recovered:
            print(f"🔁 Re-scan recovered {recovered} previously-missed message(s).")
    finally:
        imap.logout()


def run_forever() -> None:
    """Continuously check the inbox at a fixed interval, forever."""
    interval = int(os.environ.get("CHECK_INTERVAL_SECONDS", 600))
    print(
        f"📬 SPM IMAP listener started. Checking every {interval} seconds. "
        f"Press Ctrl+C to stop."
    )

    global _last_rescan_ts
    while True:
        try:
            check_inbox_once()
            now = datetime.now(timezone.utc)
            if _last_rescan_ts is None or (now - _last_rescan_ts).total_seconds() >= _RESCAN_INTERVAL_SECONDS:
                try:
                    rescan_and_catch_up()
                except Exception as e:
                    print(f"⚠️  Re-scan failed (non-fatal): {e}")
                _last_rescan_ts = now
        except Exception as e:
            print(f"❌ Error during inbox check: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    run_forever()

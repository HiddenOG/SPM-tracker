"""
backfill_change_orders.py — One-time backfill to refresh change-order PDFs.

Searches [Gmail]/All Mail for ALL warehouse routing emails whose subjects are
change orders (e.g. "0061365072-001", "0061365072 - 002").  For each, re-extracts
the attached PDF with pdfplumber and updates the base order row with the latest
delivery date and line items.

Normally is_already_processed() skips emails that were already handled.  This
script bypasses that guard intentionally — the goal is to push fresh extraction
data from change-order PDFs that the old code saved to processed_emails but
never actually re-extracted (it just returned the existing order_id and did
nothing with the new PDF).

Run once from the project root:
    python scripts/backfill_change_orders.py
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
from config import SPM_SENDER, WAREHOUSE_EMAIL
from gmail_ack_listener import connect_to_gmail, save_and_overwrite_pdf


def backfill_change_order_routing_emails() -> None:
    client_db = get_client()
    imap = connect_to_gmail()

    try:
        folder = "[Gmail]/All Mail"
        imap.select_folder(folder)

        print("Searching [Gmail]/All Mail for ALL SPM→warehouse routing emails (no cursor)...")
        uids = imap.search(["FROM", SPM_SENDER, "TO", WAREHOUSE_EMAIL])
        print(f"Found {len(uids)} total routing emails to scan.")

        change_order_count = 0
        updated_count = 0
        skipped_no_order = 0

        BATCH = 200
        for i in range(0, len(uids), BATCH):
            chunk = uids[i : i + BATCH]
            envelopes = imap.fetch(chunk, ["ENVELOPE"])

            for uid in chunk:
                env = envelopes.get(uid, {}).get(b"ENVELOPE")
                if not env or not env.subject:
                    continue
                raw_subj = env.subject
                subj = (
                    raw_subj.decode("utf-8", errors="ignore")
                    if isinstance(raw_subj, bytes) else str(raw_subj)
                )
                subj_dec = decode_mime_words(subj)

                # is_bare_po_subject returns the normalized PO string or None
                po_match = sync.is_bare_po_subject(subj_dec)
                if not po_match:
                    continue  # not a Chevron PO routing email

                base_po = base_po_number(po_match)
                if not base_po or base_po == po_match:
                    continue  # base PO, no revision suffix — not a change order

                # ── change-order routing email ────────────────────────────────
                change_order_count += 1
                print(f"\n  [{change_order_count}] UID {uid}: '{subj_dec}' → base PO {base_po}")

                # Check whether the base order exists at all
                existing = (
                    client_db.table("orders")
                    .select("id,required_delivery_date")
                    .eq("buyer_po_number", base_po)
                    .execute()
                )
                if not existing.data:
                    print(f"    ⚠️  Base order {base_po} not in DB — skipping")
                    skipped_no_order += 1
                    continue

                order_id = existing.data[0]["id"]
                old_rdd  = existing.data[0].get("required_delivery_date")

                # Fetch full email to get the PDF attachment
                msg_data = imap.fetch([uid], ["RFC822"])
                raw = msg_data.get(uid)
                if not raw:
                    print(f"    ⚠️  Could not fetch UID {uid}")
                    continue

                msg = email.message_from_bytes(raw[b"RFC822"])
                pdf_path = save_and_overwrite_pdf(msg, base_po)

                if not pdf_path:
                    print(f"    ⚠️  No PDF attachment — skipping extraction")
                    continue

                # pdfplumber (free, no API credits)
                plumber_data = None
                try:
                    pr = extract_pdf_with_pdfplumber(pdf_path)
                    if "error" not in pr:
                        plumber_data = pr
                    else:
                        print(f"    ⚠️  pdfplumber error: {pr['error']}")
                except Exception as e:
                    print(f"    ⚠️  pdfplumber failed: {e}")

                # Upload revised PDF to storage
                _co_url = None
                try:
                    from storage import upload_pdf
                    _co_url = upload_pdf(pdf_path, "ack", base_po)
                except Exception as e:
                    print(f"    Warning: PDF upload failed: {e}")

                # Clear stale extraction + update PDF path
                upd = {"extraction_raw": None}
                if pdf_path:
                    upd["pdf_attachment_path"] = pdf_path
                if _co_url:
                    upd["ack_pdf_url"] = _co_url
                client_db.table("orders").update(upd).eq("id", order_id).execute()

                # Re-populate Stage-2 fields from the change-order PDF
                if plumber_data:
                    try:
                        save_extraction_result(order_id, plumber_data)
                        new_rdd = plumber_data.get("required_delivery_date") or plumber_data.get("rdd")
                        print(f"    ✅ Re-extracted: RDD {old_rdd} → {new_rdd}")
                        updated_count += 1
                    except Exception as e:
                        print(f"    ⚠️  save_extraction_result failed: {e}")
                else:
                    print(f"    ℹ️  PDF path updated but pdfplumber returned no data")

            print(f"  ...scanned {min(i + BATCH, len(uids))}/{len(uids)}")

        print(
            f"\n{'─'*60}\n"
            f"Backfill complete.\n"
            f"  Change-order routing emails found : {change_order_count}\n"
            f"  Orders updated (PDF + extraction) : {updated_count}\n"
            f"  Skipped (no base order in DB)     : {skipped_no_order}\n"
        )

    finally:
        imap.logout()


if __name__ == "__main__":
    backfill_change_order_routing_emails()

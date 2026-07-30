"""
backfill_nlng_so_pdfs.py — Backfill so_pdf_url for existing NLNG orders.

For every nlng_orders row that has a so_number but no so_pdf_url:
  1. Search Gmail INBOX for the matching Flexitallic SO email
  2. Extract the first PDF attachment
  3. Save it to data/so_attachments/
  4. Upload to Supabase Storage (so/{so_number}/)
  5. Write so_pdf_url back to nlng_orders for every order sharing that SO number

Safe to re-run: skips orders that already have so_pdf_url set (unless --force).

Usage:
    python scripts/backfill_nlng_so_pdfs.py            # live run
    python scripts/backfill_nlng_so_pdfs.py --dry-run  # print only
    python scripts/backfill_nlng_so_pdfs.py --force    # re-upload all
"""

import email
import os
import re
import sys
from pathlib import Path
from email.header import decode_header as _dh
from email.utils import parsedate_to_datetime

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from db import get_client

DRY_RUN = "--dry-run" in sys.argv
FORCE   = "--force"   in sys.argv

from config import FLEXITALLIC_SENDER as FLEX_SENDER
FETCH_BATCH     = 25
SO_ATTACHMENTS_DIR = ROOT / "data" / "so_attachments"
SO_ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)


def _decode(s) -> str:
    if not s:
        return ""
    if isinstance(s, bytes):
        s = s.decode("utf-8", errors="ignore")
    parts = _dh(s)
    out = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            out += part.decode(enc or "utf-8", errors="replace")
        else:
            out += part
    return out


def _connect_gmail():
    from imapclient import IMAPClient
    host = os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com")
    port = int(os.environ.get("GMAIL_IMAP_PORT", 993))
    imap = IMAPClient(host, port=port, use_uid=True, ssl=True)
    imap.login(os.environ["GMAIL_EMAIL"], os.environ["GMAIL_APP_PASSWORD"])
    return imap


def _extract_pdf(msg: email.message.Message) -> bytes | None:
    for part in msg.walk():
        if part.get_content_type() == "application/pdf":
            payload = part.get_payload(decode=True)
            if payload:
                return payload
    # Some Flexitallic emails encode the PDF as application/octet-stream
    for part in msg.walk():
        ct = part.get_content_type()
        fn = part.get_filename() or ""
        if (ct == "application/octet-stream" or fn.lower().endswith(".pdf")):
            payload = part.get_payload(decode=True)
            if payload and payload[:4] == b"%PDF":
                return payload
    return None


def _save_pdf(pdf_bytes: bytes, so_number: str, original_name: str | None = None) -> str:
    safe_so = re.sub(r"[^\w\-]", "_", so_number)
    filename = original_name or f"SO_{safe_so}.pdf"
    path = SO_ATTACHMENTS_DIR / f"{safe_so}_{filename}"
    path.write_bytes(pdf_bytes)
    return str(path)


def _upload(local_path: str, so_number: str) -> str | None:
    try:
        from storage import upload_pdf
        return upload_pdf(local_path, "so", so_number)
    except Exception as e:
        print(f"    ❌ upload failed: {e}")
        return None


def run():
    db = get_client()

    # Fetch orders that need so_pdf_url
    q = db.table("nlng_orders").select("id, po_number, so_number, so_pdf_url")
    if not FORCE:
        q = q.is_("so_pdf_url", "null")
    res = q.not_.is_("so_number", "null").execute()

    rows = res.data or []
    if not rows:
        print("All NLNG orders with SO numbers already have so_pdf_url — nothing to do.")
        return

    # Group by so_number (multiple NLNG orders can share one SO)
    so_map: dict[str, list] = {}
    for row in rows:
        so_map.setdefault(row["so_number"], []).append(row)

    print(f"Need so_pdf_url for {len(rows)} order(s) across {len(so_map)} unique SO number(s).")

    imap = _connect_gmail()
    try:
        imap.select_folder("INBOX")
        uids = imap.search(["FROM", FLEX_SENDER])
        print(f"Found {len(uids)} Flexitallic emails in INBOX.\n")

        done = 0
        remaining = dict(so_map)

        for i in range(0, len(uids), FETCH_BATCH):
            if not remaining:
                break
            chunk = uids[i:i + FETCH_BATCH]
            envs = imap.fetch(chunk, ["ENVELOPE"])

            candidates = []
            for uid in chunk:
                env = envs.get(uid, {}).get(b"ENVELOPE")
                if not env or not env.subject:
                    continue
                subj = _decode(env.subject)
                m = re.search(r"\b(SO\d+)\b", subj, re.IGNORECASE)
                if not m:
                    continue
                so_number = m.group(1).upper()
                if so_number in remaining:
                    candidates.append((uid, so_number))

            if not candidates:
                continue

            msgs = imap.fetch([u for u, _ in candidates], ["RFC822"])

            for uid, so_number in candidates:
                if so_number not in remaining:
                    continue
                raw = msgs.get(uid)
                if not raw:
                    continue
                msg = email.message_from_bytes(raw[b"RFC822"])

                pdf_bytes = _extract_pdf(msg)
                if not pdf_bytes:
                    print(f"  ⚠️  {so_number}: no PDF attachment found in email")
                    continue

                # Find original filename for a clean saved name
                orig_name = None
                for part in msg.walk():
                    fn = part.get_filename()
                    if fn and fn.lower().endswith(".pdf"):
                        orig_name = _decode(fn)
                        break

                orders = remaining.pop(so_number)
                po_names = ", ".join(o["po_number"] for o in orders)
                print(f"  {so_number} ({po_names})")

                if DRY_RUN:
                    print(f"    [dry-run] would save PDF and upload to storage")
                    done += 1
                    continue

                local_path = _save_pdf(pdf_bytes, so_number, orig_name)
                print(f"    saved → {Path(local_path).name}")

                url = _upload(local_path, so_number)
                if not url:
                    continue

                print(f"    ✅ so_pdf_url set")
                for order in orders:
                    db.table("nlng_orders").update({"so_pdf_url": url}).eq("id", order["id"]).execute()
                done += 1

        if remaining:
            print(f"\n⚠️  Could not find Gmail emails for {len(remaining)} SO(s): {', '.join(remaining)}")

    finally:
        try:
            imap.logout()
        except Exception:
            pass

    action = "[dry-run] would update" if DRY_RUN else "updated"
    print(f"\nDone. {action} {done} SO PDF(s).")


if __name__ == "__main__":
    run()

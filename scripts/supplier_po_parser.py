"""
supplier_po_parser.py — Stage 6: Track SPM POs sent to suppliers and their
sales-order acknowledgments.

Two directions, both watching the Gmail account:

  OUTGOING (we → supplier):
    From specialpiping@gmail.com, subject starts with "PURCHASE ORDER ("
    and ends with a supplier name (FLEXITALLIC / AIV).
    → create spm_purchase_orders row + link all bundled Chevron POs
    → stamp sent_to_supplier_at

  INCOMING (supplier → us):
    Subject contains "Sales Acknowledgement" / "SO<digits>" from Flexitallic
    (salesorder@flexitallic.eu or Connor), with a PDF attached.
    → match by spm_po_ref (e.g. "3072")
    → extract SO number + line items from PDF (pdfplumber)
    → stamp so_acknowledged_at

Run:  python scripts/supplier_po_parser.py
"""

import os
import re
import sys
import json
import time
import email
from datetime import datetime
from email.header import decode_header
from pathlib import Path

from dotenv import load_dotenv
from imapclient import IMAPClient
import pdfplumber

import sync
from db import get_client
from config import SPM_SENDER

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

load_dotenv()


# ─────────────────────────────────────────────
# Logging — mirror stdout to a rotating log file
# ─────────────────────────────────────────────

class _Tee:
    """Writes every print() to both the terminal and a timestamped log file.
    Rotates at 5 MB, keeping one backup (.log.1).  Install via sys.stdout = _Tee(path)."""

    _MAX_BYTES = 5 * 1024 * 1024  # 5 MB

    def __init__(self, path: Path):
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "a", encoding="utf-8", buffering=1)
        self._stdout = sys.__stdout__
        self._buf = ""

    def _rotate(self):
        self._file.close()
        backup = self._path.with_suffix(".log.1")
        try:
            if backup.exists():
                backup.unlink()
            self._path.rename(backup)
        except Exception:
            pass
        self._file = open(self._path, "a", encoding="utf-8", buffering=1)

    def write(self, text: str):
        self._stdout.write(text)
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._file.write(f"[{ts}] {line}\n")
        try:
            if self._path.stat().st_size > self._MAX_BYTES:
                self._rotate()
        except Exception:
            pass

    def flush(self):
        self._stdout.flush()
        try:
            self._file.flush()
        except Exception:
            pass

    def close(self):
        if self._buf.strip():
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._file.write(f"[{ts}] {self._buf}\n")
        self._file.close()

    # make it behave like a real file object
    @property
    def encoding(self):
        return self._stdout.encoding

    def isatty(self):
        return False


def _setup_log_tee() -> "_Tee | None":
    log_dir = Path(os.environ.get("LOG_DIR", Path(__file__).parent.parent / "logs"))
    try:
        tee = _Tee(log_dir / "supplier_po_parser.log")
        return tee
    except Exception as e:
        print(f"  ⚠️ Could not open log file: {e}")
        return None


FLEXITALLIC_SENDERS = ["salesorder@flexitallic.eu", "flexitallic"]  # connor too
SUPPLIER_NAMES = ["FLEXITALLIC", "AIV"]
# Freight forwarder senders — Unicorn and equivalents. Matches on substring so
# 'airfreight@unicornsl.co.uk' and 'ops@unicornsl.co.uk' both qualify.
FREIGHT_FORWARDER_SENDERS = ["unicornsl", "unicorn freight", "airfreight@unicorn"]


def is_freight_forwarder(sender: str) -> bool:
    s = sender.lower()
    return any(ff in s for ff in FREIGHT_FORWARDER_SENDERS)

ACK_DIR = os.environ.get(
    "SUPPLIER_SO_DIR",
    r"C:\Users\Godson\spm-tracker\data\supplier_so_attachments",
)
Path(ACK_DIR).mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────
# PARSING HELPERS
# ─────────────────────────────────────────────

def decode_mime_words(s: str) -> str:
    if not s:
        return ""
    out = ""
    for part, enc in decode_header(s):
        if isinstance(part, bytes):
            out += part.decode(enc or "utf-8", errors="replace")
        else:
            out += part
    return out


def normalize_spm_string(s: str) -> str:
    """Strip spaces so 'S.P.M. - C.N.L. - 3072' == 'S.P.M.-C.N.L.-3072'."""
    return re.sub(r"\s+", "", s or "")


def extract_spm_ref(text: str) -> str | None:
    """
    Pull the stable SPM PO ref (e.g. '3072') from any string containing
    an SPM PO reference in any of the known formats:

      Format 1 — COMPANY before REF:
        S.P.M.-C.N.L.-3072-...          (Chevron Nigeria, with hyphen)
        S.P.M.-C.N.L.3069-...           (Chevron Nigeria, no hyphen before ref)
        S.P.M.-AVEON-3041-...            (Aveon)
        S.P.M.-NLNG-3039-...             (NLNG)
        S.P.M.-SEPLAT-2089-...           (Seplat)
        S.P.M.-C.N.L.-ACE EXTRA-3093-... (extra label between company and ref)

      Format 2 — REF immediately after SPM (company comes after):
        S.P.M.-3076.-NLNG-...            (ref before client name)
        S.P.M.-3079.-MOBIL LADOL-...     (ref before client name)

    Format 2 is tried first: if digits follow the SPM prefix directly, that is
    the ref. Format 1 (company word between SPM and ref) is the fallback.

    The leading 'S' is optional (seen dropped in real emails). Dots between
    letters are optional (tolerates the double-dot typo 'C.N.L..-1073'). 1-2
    word segments (letters + dots) cover the company name and any optional label.
    """
    flat = normalize_spm_string(text)
    # Format 2: ref digits immediately after the SPM prefix
    m = re.search(
        r"S?\.*P\.*M\.*[-.]+"  # S.P.M. then one or more separator chars
        r"(\d{3,6})\b",        # ref digits directly (no company word)
        flat, re.IGNORECASE,
    )
    if m:
        return m.group(1)
    # Format 1: one or two word segments (company + optional label) before the ref
    m = re.search(
        r"S?\.*P\.*M\.*-?"                # S.P.M. with optional dots/hyphens
        r"(?:[A-Za-z][A-Za-z.]*-*){1,2}" # 1-2 word segments (company + optional label);
                                          # -* allows zero, one, or two hyphens (e.g. "- -" double-dash format)
        r"(\d{3,6})\b",                   # the numeric SPM ref
        flat, re.IGNORECASE,
    )
    return m.group(1) if m else None

def extract_chevron_pos(text: str) -> list[str]:
    """
    Extract all Chevron PO numbers (006 + 7 digits) from an SPM PO subject.

    Does NOT require a trailing word boundary — some subjects omit the
    separator before the supplier name, producing '0061423954FLEXITALLIC'.
    A trailing \\b would fail there (letter follows the digits), dropping
    the PO. We anchor only on the leading boundary and a fixed 10-digit
    length so we still don't over-match longer digit runs.
    """
    flat = normalize_spm_string(text)
    # Leading boundary (start, hyphen, or paren) + 006 + 7 digits.
    # No trailing \b, so '0061423954FLEXITALLIC' still yields 0061423954.
    # Negative lookahead (?!\d) stops us grabbing 11+ digit runs mid-number.
    matches = re.findall(r"(?:^|[-(\s])(006\d{7})(?!\d)", flat)
    # De-dupe, preserve order
    seen = []
    for m in matches:
        if m not in seen:
            seen.append(m)
    return seen

def extract_supplier_name(text: str) -> str | None:
    up = (text or "").upper()
    for name in SUPPLIER_NAMES:
        if name in up:
            return name
    return None


def extract_so_number(text: str) -> str | None:
    """Find Flexitallic SO number like 'SO715269'."""
    m = re.search(r"\b(SO\d{5,})\b", text or "", re.IGNORECASE)
    return m.group(1).upper() if m else None


def extract_total_value(text: str) -> float | None:
    """Find a total like '3,775.04' near 'order value' or 'Total'."""
    m = re.search(r"([\d,]+\.\d{2})\s*US\$?", text or "")
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


# ─────────────────────────────────────────────
# EMAIL HELPERS
# ─────────────────────────────────────────────

def get_email_body_text(msg) -> str:
    """Return the best plain-text body: text/plain preferred, text/html fallback."""
    html_fallback = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            if ct == "text/plain":
                return payload.decode(errors="replace")
            if ct == "text/html" and not html_fallback:
                html_fallback = payload.decode(errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(errors="replace")
    return html_fallback


def extract_so_numbers_from_all_parts(msg) -> list[str]:
    """
    Extract all SO numbers (e.g. 'SO708848') from every text/* MIME part
    of the email — both plain text and HTML.

    iOS Mail often puts the quoted original only in the text/html part when
    replying, so searching only text/plain misses SO numbers that appear
    exclusively in the quoted section of the HTML body.
    """
    found = set()
    pattern = re.compile(r"\bSO\d{5,}\b", re.IGNORECASE)
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ct = part.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        text = payload.decode(errors="replace")
        for m in pattern.findall(text):
            found.add(m.upper())
    return list(found)


def extract_spm_refs_from_all_parts(msg) -> list[str]:
    """
    Extract all SPM ref numbers (e.g. '3063') from every text/* MIME part.

    Penny's bulk dispatch emails list one SO per line in the format:
      "SO713357 / S.P.M.-C.N.L.-3063-0061388594-FLEXITALLIC"
    The SO number is sometimes mistyped (e.g. 'SO413357' instead of 'SO713357')
    but the S.P.M.-C.N.L.-XXXX ref on the same line is always correct.
    Extracting refs line-by-line provides a reliable fallback lookup path.
    """
    found = set()
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ct = part.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        text = payload.decode(errors="replace")
        for line in text.splitlines():
            ref = extract_spm_ref(line)
            if ref:
                found.add(ref)
    return list(found)


def save_pdf_attachment(msg, spm_ref: str, so_number: str) -> str | None:
    """Save the first PDF attachment to ACK_DIR. Returns path or None."""
    if not msg.is_multipart():
        return None
    for part in msg.walk():
        filename = part.get_filename()
        if filename and filename.lower().endswith(".pdf"):
            filename = decode_mime_words(filename)
            safe_name = f"{spm_ref or 'unknown'}_{so_number or 'SO'}_{filename}"
            safe_name = re.sub(r'[<>:"/\\|?*]', "_", safe_name)
            path = os.path.join(ACK_DIR, safe_name)
            payload = part.get_payload(decode=True)
            if payload:
                with open(path, "wb") as f:
                    f.write(payload)
                return path
    return None


# Results that THIS parser writes — used to detect our own prior work.
# A "no_match" logged by gmail_ack_listener should NOT block re-processing
# here; SO-forward emails pass through that listener unchanged.
_OWN_RESULTS = {
    "spm_po_sent", "so_acknowledged", "duplicate_so",
    "so_sent_to_warehouse", "flex_dispatch_ready",
    "dispatch_instructions_sent", "ready_for_dispatch",
    "dispatched", "freight_forwarder_received",  # legacy compat
    "skipped_irrelevant",
}

def is_already_processed(message_id: str) -> bool:
    result = (
        get_client()
        .table("processed_emails")
        .select("processing_result")
        .eq("message_id", message_id)
        .execute()
    )
    if not result.data:
        return False
    return result.data[0].get("processing_result") in _OWN_RESULTS


# ─────────────────────────────────────────────
# PDF PARSING (SO acknowledgment)
# ─────────────────────────────────────────────

def parse_so_pdf(pdf_path: str) -> dict:
    """
    Extract SO number, PO ref, total, line items (with dispatch dates),
    from a Flexitallic SO PDF.
    """
    result = {
        "so_number": None,
        "spm_ref": None,
        "total": None,
        "line_items": [],
    }
    try:
        with pdfplumber.open(pdf_path) as pdf:
            full_text = ""
            for page in pdf.pages:
                full_text += (page.extract_text() or "") + "\n"
    except Exception as e:
        result["error"] = str(e)
        return result

    result["so_number"] = extract_so_number(full_text)
    result["spm_ref"] = extract_spm_ref(full_text)
    result["total"] = extract_total_value(full_text)

    # Line item rows look like:
    # "C 1 CGI11241256M1Y330A 26/05/26 1.0 EA 466.59 466.59"
    # Some have an item type letter (M) before the date:
    # "C 13 ACPACK M 26/05/26 1.0 EA 150.00 150.00"
    line_pattern = re.compile(
        r"^[A-Z]\s+(\d+)\s+(\S+)\s+(?:[A-Z]\s+)?(\d{2}/\d{2}/\d{2})\s+"
        r"([\d.]+)\s+(\w+)\s+([\d,.]+)\s+([\d,.]+)",
        re.MULTILINE
    )
    for m in line_pattern.finditer(full_text):
        result["line_items"].append({
            "line_no": m.group(1),
            "item_number": m.group(2),
            "despatch_date": parse_flexitallic_date(m.group(3)),
            "qty": float(m.group(4)),
            "uom": m.group(5),
            "unit_price": float(m.group(6).replace(",", "")),
            "extended_price": float(m.group(7).replace(",", "")),
        })

    return result

def parse_flexitallic_date(date_str: str) -> str | None:
    """Flexitallic uses DD/MM/YY. Convert to ISO YYYY-MM-DD."""
    if not date_str:
        return None
    try:
        from datetime import datetime
        return datetime.strptime(date_str.strip(), "%d/%m/%y").strftime("%Y-%m-%d")
    except ValueError:
        return None

def build_dispatch_groups(so_number: str, line_items: list[dict]) -> list[dict]:
    """
    Group line items by dispatch date.

    - If all items share ONE dispatch date → single group, ref = so_number (no suffix)
    - If items have MULTIPLE distinct dates → one group per unique date,
      ref = so_number-01, so_number-02, ... ordered by date ascending.

    Returns list of group dicts ready to insert into so_dispatch_groups.
    """
    # Collect distinct dispatch dates (ignore None)
    dates = sorted({li["despatch_date"] for li in line_items if li.get("despatch_date")})

    groups = []

    if len(dates) <= 1:
        # Single dispatch date (or none) → no suffix
        only_date = dates[0] if dates else None
        members = line_items
        groups.append({
            "dispatch_group_ref": so_number,
            "dispatch_date": only_date,
            "line_numbers": ",".join(li["line_no"] for li in members),
            "item_count": len(members),
            "group_total": round(sum(li.get("extended_price", 0) for li in members), 2),
            "members": members,
        })
    else:
        # Multiple dates → suffix per unique date, ordered by date
        for idx, d in enumerate(dates, start=1):
            members = [li for li in line_items if li.get("despatch_date") == d]
            suffix = f"-{idx:02d}"
            groups.append({
                "dispatch_group_ref": f"{so_number}{suffix}",
                "dispatch_date": d,
                "line_numbers": ",".join(li["line_no"] for li in members),
                "item_count": len(members),
                "group_total": round(sum(li.get("extended_price", 0) for li in members), 2),
                "members": members,
            })

    return groups
# ─────────────────────────────────────────────
# OUTGOING: SPM PO sent to supplier
# ─────────────────────────────────────────────
def _attachment_filenames(msg) -> list[str]:
    """Decoded filenames of all attachments on a message (empty if msg is None)."""
    names = []
    if msg is None:
        return names
    for part in msg.walk():
        fn = part.get_filename()
        if fn:
            names.append(decode_mime_words(fn))
    return names


def process_outgoing_po(client_db, msg, message_id, sender, subject, email_date) -> None:
    """Handle an outgoing PURCHASE ORDER email from SPM to a supplier."""
    spm_ref = extract_spm_ref(subject)

    # A REVISED PO often keeps the SAME subject but adds the new Chevron PO(s)
    # only in the attachment FILENAME (and PDF body), e.g. subject lists
    # '3088 - 0061433723 - 0061436573' while the revised PDF is named
    # '... 3088 - 0061433723 - 0061436573 - 0061440972 - FLEXITALLIC.pdf'.
    # So gather Chevron POs (and the supplier) from the subject AND every
    # attachment filename, then de-dupe preserving order.
    filenames = _attachment_filenames(msg)
    combined = subject + " " + " ".join(filenames)
    chevron_pos = extract_chevron_pos(combined)
    supplier_name = extract_supplier_name(subject) or extract_supplier_name(combined)

    if not spm_ref:
        return  # not a parseable SPM PO, ignore silently

    # ── RELEVANCE GATE ────────────────────────────────────────────
    # Only track this SPM PO if at least one bundled Chevron PO exists
    # as a real order. This discards all the pre-April history whose
    # orders we never captured, without date logic.
    all_orders = (
        client_db.table("orders")
        .select("id, buyer_po_number")
        .order("created_at")
        .execute()
    )
    orders_by_number: dict[str, str] = {}
    for _o in all_orders.data:
        if _o["buyer_po_number"] and _o["buyer_po_number"] not in orders_by_number:
            orders_by_number[_o["buyer_po_number"]] = _o["id"]

    # Pre-check: does ANY bundled PO match (exact or fuzzy)?
    matches = [(po, *match_chevron_po(po, orders_by_number)) for po in chevron_pos]
    has_real_order = any(oid for (_po, oid, _matched) in matches)

    if not has_real_order:
        # Park instead of permanently skip — orders may not exist yet on first run.
        # _recover_spm_pos() will replay this once a matching order is created.
        sync.park_email(
            message_id=message_id,
            kind="spm_po",
            po_number=spm_ref,
            sender=sender,
            subject=subject,
            email_date=email_date,
        )
        print(f"🅿️  Parked SPM PO {spm_ref}: no matching Chevron order yet (will retry)")
        return

    # Store the fullest PO string available — the subject or whichever
    # attachment filename lists the MOST Chevron POs (the revised one).
    best_src = subject
    best_count = len(extract_chevron_pos(subject))
    for fn in filenames:
        cnt = len(extract_chevron_pos(fn))
        if cnt > best_count:
            best_src, best_count = fn, cnt
    full_spm_po = normalize_spm_string(best_src)
    paren = re.search(r"\(([^)]+)\)", best_src)
    spm_po_number = normalize_spm_string(paren.group(1)) if paren else full_spm_po

    supplier_id = None
    if supplier_name:
        sup = (
            client_db.table("suppliers")
            .select("id")
            .ilike("name", f"%{supplier_name[:5]}%")
            .execute()
        )
        if sup.data:
            supplier_id = sup.data[0]["id"]

    existing = (
        client_db.table("spm_purchase_orders")
        .select("id")
        .eq("spm_po_ref", spm_ref)
        .execute()
    )
    if existing.data:
        spm_po_id = existing.data[0]["id"]
        client_db.table("spm_purchase_orders").update({
            "sent_to_supplier_at": email_date,
            "sent_email_message_id": message_id,
            "overall_status": "sent",
        }).eq("id", spm_po_id).execute()
    else:
        inserted = client_db.table("spm_purchase_orders").insert({
            "spm_po_number": spm_po_number,
            "spm_po_ref": spm_ref,
            "supplier_id": supplier_id,
            "supplier_name": supplier_name,
            "sent_to_supplier_at": email_date,
            "sent_email_message_id": message_id,
            "overall_status": "sent",
        }).execute()
        if not inserted.data:
            print(f"  ⚠️ [warn] spm_purchase_orders insert returned no data for SPM {spm_ref} — skipping email")
            return
        spm_po_id = inserted.data[0]["id"]

    linked = 0
    fuzzy_fixes = []
    correction_pos = []  # Chevron POs that got a new order row due to a re-raise

    for po, order_id, matched_po in matches:
        if not order_id:
            continue
        if matched_po != po:
            fuzzy_fixes.append(f"{po}→{matched_po}")

        # ── Already linked on a previous run? ────────────────────────────────
        # Look up by (spm_po_id, buyer_po_number) — this uniquely identifies
        # the active order row for this (SPM PO, Chevron PO) pair.
        current_link = (
            client_db.table("spm_po_chevron_links")
            .select("order_id")
            .eq("spm_po_id", spm_po_id)
            .eq("buyer_po_number", matched_po)
            .execute()
        )
        if current_link.data:
            # Re-run: junction link already exists. Use the row it points to so
            # we can still advance status without creating any duplicates.
            active_order_id = current_link.data[0]["order_id"]

        else:
            # ── New link: check if any OTHER SPM PO already covers this PO ──
            prior_links = (
                client_db.table("spm_po_chevron_links")
                .select("order_id")
                .eq("buyer_po_number", matched_po)
                .execute()
            )
            n_prior = len(prior_links.data)

            if n_prior == 0:
                # Normal path: first SPM PO for this Chevron PO.
                active_order_id = order_id
                try:
                    client_db.table("orders").update({
                        "spm_po_number": spm_po_number,
                        "spm_po_sent_at": email_date,
                        "supplier_id": supplier_id,
                    }).eq("id", active_order_id).execute()
                except Exception:
                    pass

            else:
                # ── RE-RAISE: Chevron PO already on a prior SPM PO. ──────────
                # Create a fresh order row cloned from the earliest (original)
                # notification row so this SO instance tracks independently —
                # it gets its own flex_dispatch_ready_at, dispatch_instructions,
                # stage, etc. without disturbing the first SO's row.
                all_for_po = (
                    client_db.table("orders")
                    .select("id")
                    .eq("buyer_po_number", matched_po)
                    .order("created_at")
                    .execute()
                )
                original_id = all_for_po.data[0]["id"] if all_for_po.data else order_id
                orig = (
                    client_db.table("orders")
                    .select(
                        "buyer_id,buyer_po_number,jde_job_id,branch_plant,"
                        "supplier_ref_number,po_amount,notification_received_at,"
                        "pdf_attachment_path,extracted_description,product_line,"
                        "required_delivery_date,acknowledgment_status,"
                        "acknowledged_at,acknowledged_by"
                    )
                    .eq("id", original_id)
                    .execute()
                )
                orig_data = orig.data[0] if orig.data else {}
                inserted = client_db.table("orders").insert({
                    **orig_data,
                    "spm_po_number": spm_po_number,
                    "spm_po_sent_at": email_date,
                    "supplier_id": supplier_id,
                    "overall_status": "po_sent",
                    "so_correction_count": n_prior,
                }).execute()
                if not inserted.data:
                    print(f"  ⚠️ [warn] re-raise order insert returned no data for {matched_po} — skipping link")
                    continue
                active_order_id = inserted.data[0]["id"]
                correction_pos.append(matched_po)

            # Insert the junction link (original or correction row → this SPM PO).
            try:
                client_db.table("spm_po_chevron_links").insert({
                    "spm_po_id": spm_po_id,
                    "order_id": active_order_id,
                    "buyer_po_number": matched_po,
                }).execute()
                linked += 1
            except Exception:
                pass  # duplicate — junction row already exists

        # Advance status on whatever row is now active for this (SPM PO, Chevron PO) pair.
        try:
            sync.advance_status(client_db, active_order_id, "po_sent")
        except Exception as e:
            print(f"  ⚠️ [warn] advance_status failed for order {active_order_id}: {e}")

    note = f"SPM PO {spm_ref} → {supplier_name}, {linked} Chevron PO(s) linked"
    if fuzzy_fixes:
        note += f" (corrected: {', '.join(fuzzy_fixes)})"
    if correction_pos:
        note += f" (re-raised: {', '.join(correction_pos)})"
    client_db.table("processed_emails").upsert({
        "message_id": message_id, "sender": sender, "subject": subject,
        "processing_result": "spm_po_sent", "raw_notes": note,
    }, on_conflict="message_id").execute()

    print(f"📤 SPM PO {spm_ref} → {supplier_name}: {linked} Chevron PO(s) linked"
          + (f", corrected {len(fuzzy_fixes)} malformed" if fuzzy_fixes else "")
          + (f", ⚠️  {len(correction_pos)} re-raise(s): {', '.join(correction_pos)}" if correction_pos else "")
          + f", sent {email_date[:16]}")

# ─────────────────────────────────────────────
# INCOMING: supplier SO acknowledgment
# ─────────────────────────────────────────────

def process_so_ack(client_db, msg, message_id, sender, subject, email_date) -> None:
    """Handle a Flexitallic sales-order acknowledgment email."""
    so_number = extract_so_number(subject)
    spm_ref = extract_spm_ref(subject)
    total = extract_total_value(subject)

    if not spm_ref:
        # Can't match without a ref. Stay silent — it's irrelevant noise.
        return

    # ── RELEVANCE GATE ────────────────────────────────────────────
    # Only process SO acks for SPM POs we're actually tracking. An SPM PO
    # only exists if its outgoing PO bundled a real (tracked) Chevron order.
    # So if there's no matching SPM PO, this SO is pre-April history → drop.
    spm_po = (
        client_db.table("spm_purchase_orders")
        .select("id, so_number")
        .eq("spm_po_ref", spm_ref)
        .execute()
    )
    if not spm_po.data:
        return  # not tracked → ignore silently

    spm_po_id = spm_po.data[0]["id"]
    existing_so = spm_po.data[0].get("so_number")

    # ── DUPLICATE GUARD (visible) ─────────────────────────────────
    # All Mail contains the original SO ack plus every reply/forward copy,
    # each with a distinct Message-ID, so message_id dedup misses them.
    # If this SO is already recorded for this SPM PO, show it and skip.
    # Require so_number to be non-None — None==None would falsely skip
    # the first real ack when the subject carries no SOxxxxxx token.
    if so_number and existing_so == so_number:
        print(f"🔁 DUPLICATE SO {so_number} for SPM {spm_ref} — already recorded, skipping.")
        client_db.table("processed_emails").upsert({
            "message_id": message_id, "sender": sender, "subject": subject,
            "processing_result": "duplicate_so",
            "raw_notes": f"Duplicate SO {so_number} for SPM {spm_ref}",
        }, on_conflict="message_id").execute()
        return

    # ── Parse the PDF for line items ──────────────────────────────
    pdf_path = save_pdf_attachment(msg, spm_ref, so_number)
    pdf_data = {}
    so_pdf_url = None
    if pdf_path:
        pdf_data = parse_so_pdf(pdf_path)
        so_number = so_number or pdf_data.get("so_number")
        total = total or pdf_data.get("total")
        # Upload SO PDF to Supabase Storage
        if so_number:
            try:
                from storage import upload_pdf
                so_pdf_url = upload_pdf(pdf_path, "so", so_number)
            except Exception as e:
                print(f"  Warning: SO PDF upload failed for {so_number}: {e}")

    # ── Update the SPM PO with SO header info ─────────────────────
    client_db.table("spm_purchase_orders").update({
        "so_number": so_number,
        "so_acknowledged_at": email_date,
        "so_pdf_path": pdf_path,
        "so_raw": {
            "total": total,
            "line_items": pdf_data.get("line_items", []),
            "subject": subject,
        },
        "overall_status": "acknowledged",
    }).eq("id", spm_po_id).execute()

    # ── Build and store dispatch groups ──────────────────────────
    line_items = pdf_data.get("line_items", [])
    groups = build_dispatch_groups(so_number, line_items) if (so_number and line_items) else []

    multi = len(groups) > 1
    for g in groups:
        grp = client_db.table("so_dispatch_groups").insert({
            "spm_po_id": spm_po_id,
            "so_number": so_number,
            "dispatch_group_ref": g["dispatch_group_ref"],
            "dispatch_date": g["dispatch_date"],
            "line_numbers": g["line_numbers"],
            "item_count": g["item_count"],
            "group_total": g["group_total"],
        }).execute()
        if not grp.data:
            print(f"  ⚠️  dispatch group insert returned no data for {g['dispatch_group_ref']} — skipping line items")
            continue
        group_id = grp.data[0]["id"]

        for li in g["members"]:
            client_db.table("so_line_items").insert({
                "spm_po_id": spm_po_id,
                "dispatch_group_id": group_id,
                "so_number": so_number,
                "line_no": li["line_no"],
                "item_number": li["item_number"],
                "despatch_date": li["despatch_date"],
                "qty": li["qty"],
                "uom": li["uom"],
                "unit_price": li["unit_price"],
                "extended_price": li["extended_price"],
            }).execute()

    # ── Stamp linked Chevron orders ──────────────────────────────
    links = (
        client_db.table("spm_po_chevron_links")
        .select("order_id")
        .eq("spm_po_id", spm_po_id)
        .execute()
    )
    stamped = 0
    earliest_dispatch = min(
        (g["dispatch_date"] for g in groups if g["dispatch_date"]),
        default=None,
    )
    for link in links.data:
        if link["order_id"]:
            so_update = {
                "promised_date": earliest_dispatch,
                "so_received_at": email_date,
            }
            if so_number:
                so_update["so_number"] = so_number
            if so_pdf_url:
                so_update["so_pdf_url"] = so_pdf_url
            client_db.table("orders").update(so_update).eq("id", link["order_id"]).execute()
            sync.advance_status(client_db, link["order_id"], "supplier_acknowledged")
            stamped += 1

    client_db.table("processed_emails").upsert({
        "message_id": message_id, "sender": sender, "subject": subject,
        "processing_result": "so_acknowledged",
        "raw_notes": f"SO {so_number} matched SPM {spm_ref}, "
                     f"{len(groups)} dispatch group(s), {stamped} orders stamped",
    }, on_conflict="message_id").execute()

    split_note = f" (SPLIT into {len(groups)} dispatch dates)" if multi else ""
    print(f"✅ SO {so_number} → SPM {spm_ref}: acknowledged, "
          f"{len(line_items)} line item(s){split_note}, "
          f"total {total}, {stamped} Chevron order(s) updated")
    if multi:
        for g in groups:
            print(f"   📦 {g['dispatch_group_ref']}: {g['item_count']} item(s), "
                  f"dispatch {g['dispatch_date']}, lines {g['line_numbers']}")
# ─────────────────────────────────────────────
# HELPERS: look up multiple tracked SPM POs from mixed identifiers
# ─────────────────────────────────────────────

def _find_tracked_spm_pos(client_db, spm_ref=None, so_numbers=None, extra_spm_refs=None) -> list[dict]:
    """
    Return all tracked spm_purchase_orders rows that match any of the given
    identifiers.  Deduplicates by id so a PO found via multiple paths is only
    returned once.

    Lookup priority (all attempted, results merged):
      1. spm_ref from subject
      2. SO numbers from subject or body
      3. extra_spm_refs from body lines — fallback for when Penny's SO number
         has a typo (e.g. 'SO413357' instead of 'SO713357') but the
         S.P.M.-C.N.L.-3063 ref on the same line is correct.
    """
    seen = {}
    if spm_ref:
        res = (
            client_db.table("spm_purchase_orders")
            .select("id, so_number, spm_po_ref")
            .eq("spm_po_ref", spm_ref)
            .execute()
        )
        for row in res.data:
            seen[row["id"]] = row
    for son in (so_numbers or []):
        if not son:
            continue
        res = (
            client_db.table("spm_purchase_orders")
            .select("id, so_number, spm_po_ref")
            .eq("so_number", son.upper())
            .execute()
        )
        for row in res.data:
            seen[row["id"]] = row
    for ref in (extra_spm_refs or []):
        if not ref:
            continue
        res = (
            client_db.table("spm_purchase_orders")
            .select("id, so_number, spm_po_ref")
            .eq("spm_po_ref", ref)
            .execute()
        )
        for row in res.data:
            seen[row["id"]] = row
    return list(seen.values())


def _stamp_orders_for_spm_po(client_db, spm_po_id, field: str, value,
                              status: str = None) -> int:
    """Stamp a single timestamp field on all orders linked to spm_po_id.
    Only writes when the field is currently NULL (first occurrence wins).
    If status is provided, advance overall_status for each order regardless.
    Returns count of rows where the timestamp was newly written."""
    links = (
        client_db.table("spm_po_chevron_links")
        .select("order_id")
        .eq("spm_po_id", spm_po_id)
        .execute()
    )
    stamped = 0
    for link in links.data:
        if link["order_id"]:
            result = (
                client_db.table("orders")
                .update({field: value})
                .eq("id", link["order_id"])
                .is_(field, "null")
                .execute()
            )
            if result.data:
                stamped += 1
            if status:
                sync.advance_status(client_db, link["order_id"], status)
    return stamped


# ─────────────────────────────────────────────
# INCOMING: Flexitallic "packed and ready for dispatch" notification
# ─────────────────────────────────────────────

def process_flex_dispatch_ready(client_db, msg, message_id, sender, subject, email_date,
                                 extra_so_numbers=None, extra_spm_refs=None) -> None:
    """
    Handle a Flexitallic email advising that orders are packed and ready for
    dispatch (e.g. from Penny Latham, platham@flexitallic.eu).

    Two subject formats:
      Single:  "RE: SO717816 / S.P.M.-C.N.L.-ACE EXTRA-3093-..." (SO in subject)
      Bulk:    "RE: SPM, NIGERIA Orders ready for dispatch 03.02.26" (SOs only in body)

    extra_so_numbers: SO numbers parsed from the email body by the router, used
    for bulk emails where the subject carries no SO/SPM reference.

    Stamps flex_dispatch_ready_at on each matching spm_purchase_orders row
    and on each linked Chevron order.
    """
    so_number = extract_so_number(subject)
    spm_ref = extract_spm_ref(subject)
    all_so_numbers = list({so_number} | set(extra_so_numbers or [])) if so_number else list(extra_so_numbers or [])

    if not spm_ref and not all_so_numbers and not extra_spm_refs:
        return

    spm_pos = _find_tracked_spm_pos(client_db, spm_ref=spm_ref, so_numbers=all_so_numbers,
                                     extra_spm_refs=extra_spm_refs)

    total_stamped = 0
    labels = []
    for spm_po in spm_pos:
        spm_po_id = spm_po["id"]
        ref_label = spm_po.get("spm_po_ref") or spm_ref or "?"
        so_label = spm_po.get("so_number") or so_number or "?"
        client_db.table("spm_purchase_orders").update({
            "flex_dispatch_ready_at": email_date,
        }).eq("id", spm_po_id).is_("flex_dispatch_ready_at", "null").execute()
        stamped = _stamp_orders_for_spm_po(
            client_db, spm_po_id, "flex_dispatch_ready_at", email_date,
            "dispatch_packed_awaiting_instruction",
        )
        total_stamped += stamped
        labels.append(f"SO {so_label} (SPM {ref_label})")
        print(f"📦 SO {so_label} (SPM {ref_label}): Flexitallic packed & ready {email_date[:16]}, "
              f"{stamped} order(s) stamped")

    # Also stamp NLNG orders that share any of these SO numbers
    nlng_stamped = 0
    for son in all_so_numbers:
        for nlng_order in sync.find_all_nlng_orders_by_so_number(son):
            sync.stamp_nlng_flex_dispatch_ready(nlng_order["id"], email_date)
            nlng_stamped += 1
            print(f"📦 NLNG {nlng_order['po_number']} | SO {son}: dispatch ready {email_date[:16]}")
    if nlng_stamped:
        total_stamped += nlng_stamped
        print(f"   ({nlng_stamped} NLNG order(s) stamped)")

    client_db.table("processed_emails").upsert({
        "message_id": message_id,
        "sender": sender,
        "subject": subject,
        "processing_result": "flex_dispatch_ready",
        "raw_notes": f"{', '.join(labels)} packed & ready, {total_stamped} order(s) stamped",
    }, on_conflict="message_id").execute()


# ─────────────────────────────────────────────
# OUTGOING: SPM replies to Flexitallic with dispatch instructions
# ─────────────────────────────────────────────

def process_dispatch_instructions(client_db, msg, message_id, sender, subject, email_date,
                                   extra_so_numbers=None, extra_spm_refs=None) -> None:
    """
    Handle SPM's reply to Flexitallic confirming where to ship
    (e.g. "Kindly ship to Unicorn").

    Two subject formats:
      Single:  "RE: SO717816 / S.P.M.-C.N.L.-..." (SO/SPM ref in subject)
      Bulk:    "Re: RE: SPM, NIGERIA Orders ready for dispatch..." (SOs only in quoted body)

    extra_so_numbers: SO numbers parsed from the email body by the router.

    Stamps dispatch_instructions_sent_at on each matching spm_purchase_orders
    row and each linked Chevron order.
    """
    so_number = extract_so_number(subject)
    spm_ref = extract_spm_ref(subject)
    all_so_numbers = list({so_number} | set(extra_so_numbers or [])) if so_number else list(extra_so_numbers or [])

    if not spm_ref and not all_so_numbers and not extra_spm_refs:
        return

    spm_pos = _find_tracked_spm_pos(client_db, spm_ref=spm_ref, so_numbers=all_so_numbers,
                                     extra_spm_refs=extra_spm_refs)

    total_stamped = 0
    labels = []
    for spm_po in spm_pos:
        spm_po_id = spm_po["id"]
        ref_label = spm_po.get("spm_po_ref") or spm_ref or "?"
        so_label = spm_po.get("so_number") or so_number or "?"
        client_db.table("spm_purchase_orders").update({
            "dispatch_instructions_sent_at": email_date,
        }).eq("id", spm_po_id).is_("dispatch_instructions_sent_at", "null").execute()
        stamped = _stamp_orders_for_spm_po(
            client_db, spm_po_id, "dispatch_instructions_sent_at", email_date,
            "dispatch_instruction_sent",
        )
        total_stamped += stamped
        labels.append(f"SO {so_label} (SPM {ref_label})")
        print(f"✈️  SO {so_label} (SPM {ref_label}): dispatch instructions sent {email_date[:16]}, "
              f"{stamped} order(s) stamped")

    # Also stamp NLNG orders that share any of these SO numbers
    nlng_stamped = 0
    for son in all_so_numbers:
        for nlng_order in sync.find_all_nlng_orders_by_so_number(son):
            sync.stamp_nlng_dispatch_instructions_sent(nlng_order["id"], email_date)
            nlng_stamped += 1
            print(f"✈️  NLNG {nlng_order['po_number']} | SO {son}: dispatch instructions sent {email_date[:16]}")
    if nlng_stamped:
        total_stamped += nlng_stamped
        print(f"   ({nlng_stamped} NLNG order(s) stamped)")

    client_db.table("processed_emails").upsert({
        "message_id": message_id,
        "sender": sender,
        "subject": subject,
        "processing_result": "dispatch_instructions_sent",
        "raw_notes": f"{', '.join(labels)} dispatch instructions sent, {total_stamped} order(s) stamped",
    }, on_conflict="message_id").execute()


# ─────────────────────────────────────────────
# INCOMING: SPM forwards the supplier SO to warehouse
# ─────────────────────────────────────────────

def process_so_warehouse_forward(client_db, msg, message_id, sender, subject, email_date) -> None:
    """
    Handle SPM forwarding a Flexitallic SO to the warehouse.

    The forwarded email is FROM specialpiping@gmail.com, has the SO number
    (e.g. 'SO715269') or SPM ref (e.g. 'C.N.L.-3072') in the subject, and
    is NOT a PURCHASE ORDER email. We stamp so_sent_to_warehouse_at on the
    matching spm_purchase_orders row and each linked Chevron order.
    """
    so_number = extract_so_number(subject)
    spm_ref = extract_spm_ref(subject)

    if not spm_ref and not so_number:
        return

    # Locate the tracked SPM PO — prefer ref (stable) over SO number
    spm_po = None
    if spm_ref:
        res = (
            client_db.table("spm_purchase_orders")
            .select("id, so_number, spm_po_ref")
            .eq("spm_po_ref", spm_ref)
            .execute()
        )
        spm_po = res.data[0] if res.data else None
    if not spm_po and so_number:
        res = (
            client_db.table("spm_purchase_orders")
            .select("id, so_number, spm_po_ref")
            .eq("so_number", so_number)
            .execute()
        )
        spm_po = res.data[0] if res.data else None

    if not spm_po:
        return  # not a tracked SPM PO — ignore silently

    spm_po_id = spm_po["id"]
    ref_label = spm_po.get("spm_po_ref") or spm_ref
    so_label = spm_po.get("so_number") or so_number

    client_db.table("spm_purchase_orders").update({
        "so_sent_to_warehouse_at": email_date,
    }).eq("id", spm_po_id).is_("so_sent_to_warehouse_at", "null").execute()

    links = (
        client_db.table("spm_po_chevron_links")
        .select("order_id")
        .eq("spm_po_id", spm_po_id)
        .execute()
    )
    stamped = 0
    for link in links.data:
        if link["order_id"]:
            client_db.table("orders").update({
                "so_sent_to_warehouse_at": email_date,
            }).eq("id", link["order_id"]).is_("so_sent_to_warehouse_at", "null").execute()
            sync.advance_status(client_db, link["order_id"], "so_sent_to_warehouse")
            stamped += 1

    client_db.table("processed_emails").upsert({
        "message_id": message_id,
        "sender": sender,
        "subject": subject,
        "processing_result": "so_sent_to_warehouse",
        "raw_notes": f"SO {so_label} (SPM {ref_label}) forwarded to warehouse, {stamped} order(s) stamped",
    }, on_conflict="message_id").execute()

    print(f"📬 SO {so_label} (SPM {ref_label}): forwarded to warehouse {email_date[:16]}, "
          f"{stamped} order(s) stamped")


# ─────────────────────────────────────────────
# INCOMING: Flexitallic confirms collection arranged for shipping company
# ─────────────────────────────────────────────

def process_ready_for_dispatch(client_db, msg, message_id, sender, subject, email_date,
                                extra_so_numbers=None, extra_spm_refs=None) -> None:
    """
    Handle Penny's email confirming she has arranged collection by a transport
    company (e.g. RTC Transport, Pudsey Transport) to deliver goods to the
    shipping agent (e.g. Unicorn). Comes AFTER dispatch instructions from SPM.

    Example body A: "We have arranged for Pudsey Transport to collect all the
    below orders from us today and deliver them to Unicorn."

    Example body B: "We will arrange for the 2 orders to be sent to Unicorn.
    Goods will be collected from us today by RTC Transport, and delivered to
    you tomorrow."

    Key signals: "arrange" (or "arranged" or "collect") + shipper name
    ("unicorn", "transport", "pudsey", "rtc").

    Stamps ready_for_dispatch_at on matching spm_purchase_orders and linked
    Chevron orders; advances status to 'ready_for_dispatch'.
    """
    so_number = extract_so_number(subject)
    spm_ref = extract_spm_ref(subject)
    all_so_numbers = (
        list({so_number} | set(extra_so_numbers or [])) if so_number
        else list(extra_so_numbers or [])
    )

    if not spm_ref and not all_so_numbers and not extra_spm_refs:
        return

    spm_pos = _find_tracked_spm_pos(
        client_db, spm_ref=spm_ref, so_numbers=all_so_numbers, extra_spm_refs=extra_spm_refs,
    )

    total_stamped = 0
    labels = []
    for spm_po in spm_pos:
        spm_po_id = spm_po["id"]
        ref_label = spm_po.get("spm_po_ref") or spm_ref or "?"
        so_label = spm_po.get("so_number") or so_number or "?"
        client_db.table("spm_purchase_orders").update({
            "ready_for_dispatch_at": email_date,
        }).eq("id", spm_po_id).is_("ready_for_dispatch_at", "null").execute()
        stamped = _stamp_orders_for_spm_po(
            client_db, spm_po_id, "ready_for_dispatch_at", email_date, "ready_for_dispatch",
        )
        total_stamped += stamped
        labels.append(f"SO {so_label} (SPM {ref_label})")
        print(f"🚛 SO {so_label} (SPM {ref_label}): collection arranged, ready for dispatch "
              f"{email_date[:16]}, {stamped} order(s) stamped")

    # Also stamp NLNG orders sharing these SO numbers
    nlng_stamped = 0
    for son in all_so_numbers:
        for nlng_order in sync.find_all_nlng_orders_by_so_number(son):
            sync.stamp_nlng_ready_for_dispatch(nlng_order["id"], email_date)
            nlng_stamped += 1
            print(f"🚛 NLNG {nlng_order['po_number']} | SO {son}: ready for dispatch {email_date[:16]}")
    if nlng_stamped:
        total_stamped += nlng_stamped
        print(f"   ({nlng_stamped} NLNG order(s) stamped)")

    client_db.table("processed_emails").upsert({
        "message_id": message_id,
        "sender": sender,
        "subject": subject,
        "processing_result": "ready_for_dispatch",
        "raw_notes": f"{', '.join(labels)} collection arranged, {total_stamped} order(s) stamped",
    }, on_conflict="message_id").execute()


# ─────────────────────────────────────────────
# INCOMING: shipping company acknowledges goods receipt ("Noted")
# ─────────────────────────────────────────────

def process_dispatched(client_db, msg, message_id, sender, subject, email_date,
                        extra_so_numbers=None, extra_spm_refs=None) -> None:
    """
    Handle the shipping company (e.g. Unicorn) replying 'Noted' after Penny
    confirms goods have been collected and are on their way.

    Stamps dispatched_at on matching spm_purchase_orders and linked Chevron
    orders; advances status to 'dispatched'.
    """
    so_number = extract_so_number(subject)
    spm_ref = extract_spm_ref(subject)
    all_so_numbers = (
        list({so_number} | set(extra_so_numbers or [])) if so_number
        else list(extra_so_numbers or [])
    )

    if not spm_ref and not all_so_numbers and not extra_spm_refs:
        return

    spm_pos = _find_tracked_spm_pos(
        client_db, spm_ref=spm_ref, so_numbers=all_so_numbers, extra_spm_refs=extra_spm_refs,
    )

    total_stamped = 0
    labels = []
    for spm_po in spm_pos:
        spm_po_id = spm_po["id"]
        ref_label = spm_po.get("spm_po_ref") or spm_ref or "?"
        so_label = spm_po.get("so_number") or so_number or "?"
        client_db.table("spm_purchase_orders").update({
            "dispatched_at": email_date,
        }).eq("id", spm_po_id).is_("dispatched_at", "null").execute()
        stamped = _stamp_orders_for_spm_po(
            client_db, spm_po_id, "dispatched_at", email_date, "dispatched",
        )
        total_stamped += stamped
        labels.append(f"SO {so_label} (SPM {ref_label})")
        print(f"🚢 SO {so_label} (SPM {ref_label}): dispatched (shipping company noted) "
              f"{email_date[:16]}, {stamped} order(s) stamped")

    # Also stamp NLNG orders sharing these SO numbers
    nlng_stamped = 0
    for son in all_so_numbers:
        for nlng_order in sync.find_all_nlng_orders_by_so_number(son):
            sync.stamp_nlng_dispatched(nlng_order["id"], email_date)
            nlng_stamped += 1
            print(f"🚢 NLNG {nlng_order['po_number']} | SO {son}: dispatched {email_date[:16]}")
    if nlng_stamped:
        total_stamped += nlng_stamped
        print(f"   ({nlng_stamped} NLNG order(s) stamped)")

    client_db.table("processed_emails").upsert({
        "message_id": message_id,
        "sender": sender,
        "subject": subject,
        "processing_result": "dispatched",
        "raw_notes": f"{', '.join(labels)} dispatched, {total_stamped} order(s) stamped",
    }, on_conflict="message_id").execute()


# ─────────────────────────────────────────────
# ROUTER
# ─────────────────────────────────────────────

def _one_deletion_variants(po: str) -> set[str]:
    """All strings formed by deleting exactly one character from po."""
    return {po[:i] + po[i+1:] for i in range(len(po))}


def match_chevron_po(po: str, orders_by_number: dict) -> tuple[str | None, str]:
    """
    Match a Chevron PO from an email to a real order.

    orders_by_number: {buyer_po_number: order_id} for all orders.

    Returns (order_id, matched_po_number). order_id is None if no match.
    matched_po_number is the REAL po number when fuzzy-matched, else the
    input po (so the link stores the corrected number).
    """
    # 1. Exact match
    if po in orders_by_number:
        return orders_by_number[po], po

    # 2. Base PO maps to a revisioned order. An SPM PO subject/filename lists
    #    the base number '0060792432', but the tracked order is the change
    #    order '0060792432-001'. Only the revision is tracked (the original
    #    isn't), so a base->'{base}-NNN' match is safe and correct.
    for real_po, oid in orders_by_number.items():
        if real_po.startswith(po + "-"):
            return oid, real_po

    # 3. Fuzzy: malformed PO is real PO with one digit dropped.
    #    Only attempt for plausibly-short Chevron numbers (9 digits).
    if len(po) == 9 and po.startswith("006"):
        for real_po, oid in orders_by_number.items():
            if len(real_po) == 10 and po in _one_deletion_variants(real_po):
                return oid, real_po

    # 4. No match
    return None, po

def process_message(client_db, msg_data: dict) -> None:
    raw_email = msg_data[b"RFC822"]
    msg = email.message_from_bytes(raw_email)

    message_id = msg.get("Message-ID", "")
    if not message_id:
        message_id = f"{msg.get('From')}-{msg.get('Date')}-{msg.get('Subject')}"

    if is_already_processed(message_id):
        return

    sender = decode_mime_words(msg.get("From", "")).lower()
    subject = decode_mime_words(msg.get("Subject", ""))
    email_date = sync.parse_email_date(msg)

    # OUTGOING: from SPM, subject is a PURCHASE ORDER to a supplier
    # OUTGOING: from SPM, subject contains a PURCHASE ORDER to a supplier
    # ('in' not startswith, so 'Re:' / '[ELOID:...]' prefixes still match)
    if SPM_SENDER in sender and "PURCHASE ORDER" in subject.upper():
        if extract_supplier_name(subject):
            process_outgoing_po(client_db, msg, message_id, sender, subject, email_date)
            return

    # INCOMING: SO acknowledgment from Flexitallic (subject has "Sales Acknowledgement")
    is_flexitallic = any(fs in sender for fs in FLEXITALLIC_SENDERS)
    # Only the salesorder@ address sends real acks. Penny replies in the same thread
    # (platham@, connorh@) can have "Sales Acknowledgement" in the subject but are
    # dispatch-ready notifications — they must NOT be misrouted to process_so_ack().
    looks_like_ack = (
        "salesorder@flexitallic.eu" in sender.lower()
        and (
            "sales acknowledgement" in subject.lower()
            or "sales acknowledgment" in subject.lower()
        )
    )
    if is_flexitallic and looks_like_ack:
        process_so_ack(client_db, msg, message_id, sender, subject, email_date)
        return

    # INCOMING: Flexitallic emails (not acks) — two distinct email types from Penny:
    #   1. "Arranged transport" — Penny confirms she's arranged collection for delivery
    #      to Unicorn/shipping agent. Body has "arrange"/"collect" + shipper name.
    #      Must be checked BEFORE the packed/dispatch check — it won't contain those words.
    #   2. "Packed and ready" — Penny says goods are packed, awaiting dispatch instructions.
    if is_flexitallic and not looks_like_ack:
        body = get_email_body_text(msg)
        body_lower = body.lower()

        # Type 1: Penny confirmed collection/dispatch to shipping company.
        # "arrange"/"collect" = classic phrasing; "dispatch" + transport name = "we will dispatch to Unicorn today".
        _has_transport = ("unicorn" in body_lower or "transport" in body_lower
                          or "pudsey" in body_lower or "rtc" in body_lower)
        if _has_transport and ("arrange" in body_lower or "collect" in body_lower or "dispatch" in body_lower):
            body_sos = extract_so_numbers_from_all_parts(msg)
            body_refs = extract_spm_refs_from_all_parts(msg)
            if extract_so_number(subject) or extract_spm_ref(subject) or body_sos or body_refs:
                process_ready_for_dispatch(
                    client_db, msg, message_id, sender, subject, email_date,
                    extra_so_numbers=body_sos, extra_spm_refs=body_refs,
                )
                return

        # Type 2: Penny "packed and ready"
        if "dispatch" in body_lower or "packed" in body_lower:
            body_sos = extract_so_numbers_from_all_parts(msg)
            body_refs = extract_spm_refs_from_all_parts(msg)
            if extract_so_number(subject) or extract_spm_ref(subject) or body_sos or body_refs:
                process_flex_dispatch_ready(
                    client_db, msg, message_id, sender, subject, email_date,
                    extra_so_numbers=body_sos, extra_spm_refs=body_refs,
                )
                return

    # OUTGOING: SPM replies to Flexitallic with dispatch instructions,
    # OR SPM forwards the SO to the warehouse.
    # Distinguish by the To: header — if it points at Flexitallic it's dispatch
    # instructions; otherwise it's a warehouse forward.
    if SPM_SENDER in sender and "PURCHASE ORDER" not in subject.upper():
        to_header = decode_mime_words(msg.get("To", "")).lower()
        so_in_subj = extract_so_number(subject)
        spm_in_subj = extract_spm_ref(subject)
        if so_in_subj or spm_in_subj:
            body_sos = extract_so_numbers_from_all_parts(msg)
            body_refs = extract_spm_refs_from_all_parts(msg)
            if "flexitallic" in to_header:
                process_dispatch_instructions(client_db, msg, message_id, sender, subject, email_date,
                                              extra_so_numbers=body_sos, extra_spm_refs=body_refs)
            else:
                process_so_warehouse_forward(client_db, msg, message_id, sender, subject, email_date)
            return
        # Bulk reply to Flexitallic — no SO in subject; scan all MIME parts.
        # iOS Mail puts quoted SO numbers in text/html only. Also extract SPM
        # refs as a fallback for mistyped SO numbers in Penny's emails.
        if "flexitallic" in to_header:
            body_sos = extract_so_numbers_from_all_parts(msg)
            body_refs = extract_spm_refs_from_all_parts(msg)
            if body_sos or body_refs:
                process_dispatch_instructions(
                    client_db, msg, message_id, sender, subject, email_date,
                    extra_so_numbers=body_sos, extra_spm_refs=body_refs,
                )
                return

    # INCOMING: shipping company (e.g. Unicorn) acknowledges goods received — "Noted"
    if is_freight_forwarder(sender):
        body = get_email_body_text(msg)
        if "noted" in body.lower() or "confirm" in body.lower():
            body_sos = extract_so_numbers_from_all_parts(msg)
            body_refs = extract_spm_refs_from_all_parts(msg)
            if extract_so_number(subject) or extract_spm_ref(subject) or body_sos or body_refs:
                process_dispatched(
                    client_db, msg, message_id, sender, subject, email_date,
                    extra_so_numbers=body_sos, extra_spm_refs=body_refs,
                )
                return

    # Not relevant to this parser — ignore silently


# ─────────────────────────────────────────────
# RECOVERY: retry parked / previously-skipped SPM PO emails
# ─────────────────────────────────────────────

def _recover_spm_pos(client_db, imap) -> None:
    """
    Run before each IMAP scan to catch up on emails that were skipped or parked
    because the matching Chevron order didn't exist at processing time.

    Case 1 — legacy skipped_irrelevant entries in processed_emails:
        Created before parking was introduced. Re-fetch the full email from IMAP
        by Message-ID header search, delete the stale processed_emails row, then
        call process_message() so the normal flow handles it (now that orders exist).

    Case 2 — parked spm_po entries in parked_emails:
        Stored by the updated process_outgoing_po(). email_date is available so we
        can replay directly without going back to IMAP.

    Both cases are no-ops when no matching orders exist yet — the email stays
    parked/skipped until a later run where orders are present.
    """
    from datetime import datetime, timezone

    all_orders = (
        client_db.table("orders")
        .select("id, buyer_po_number")
        .order("created_at")
        .execute()
    )
    orders_by_number: dict[str, str] = {}
    for _o in all_orders.data:
        if _o["buyer_po_number"] and _o["buyer_po_number"] not in orders_by_number:
            orders_by_number[_o["buyer_po_number"]] = _o["id"]

    # ── Case 1: legacy skipped_irrelevant rows ────────────────────────────────
    skipped = (
        client_db.table("processed_emails")
        .select("id, message_id, sender, subject")
        .eq("processing_result", "skipped_irrelevant")
        .execute()
    )
    for row in skipped.data:
        subject = row.get("subject") or ""
        if not extract_spm_ref(subject):
            continue  # not an SPM PO email — leave it alone
        chevron_pos = extract_chevron_pos(subject)
        matches = [(po, *match_chevron_po(po, orders_by_number)) for po in chevron_pos]
        if not any(oid for (_, oid, _) in matches):
            continue  # still no matching order
        msg_id = (row["message_id"] or "").strip()
        try:
            # IMAP HEADER search finds the email regardless of where the cursor is
            uids = imap.search(["HEADER", "Message-ID", msg_id.strip("<>")])
            if not uids:
                uids = imap.search(["HEADER", "Message-ID", msg_id])
            if not uids:
                print(f"   ⚠️  Skipped SPM PO email not found in IMAP: {msg_id[:40]}")
                continue
            messages = imap.fetch(uids[:1], ["RFC822"])
            for data in messages.values():
                # Delete stale row first so process_message doesn't see it as processed
                client_db.table("processed_emails").delete().eq("id", row["id"]).execute()
                print(f"   ↩️  Retrying previously-skipped: {subject[:60]}")
                process_message(client_db, data)
        except Exception as exc:
            print(f"   ⚠️  Could not recover skipped email {msg_id[:40]}: {exc}")

    # ── Case 2: parked spm_po entries ────────────────────────────────────────
    parked = (
        client_db.table("parked_emails")
        .select("*")
        .eq("kind", "spm_po")
        .execute()
    )
    for p in parked.data:
        subject = p.get("subject") or ""
        chevron_pos = extract_chevron_pos(subject)
        matches = [(po, *match_chevron_po(po, orders_by_number)) for po in chevron_pos]
        if not any(oid for (_, oid, _) in matches):
            continue  # still no matching order
        email_date = p.get("email_date") or datetime.now(timezone.utc).isoformat()
        msg_id = (p.get("message_id") or "").strip()
        try:
            # Re-fetch the full email so attachment FILENAMES are available on
            # replay — a revised PO adds its new Chevron PO only in the PDF
            # filename, and replaying with msg=None would silently drop it.
            uids = imap.search(["HEADER", "Message-ID", msg_id.strip("<>")]) if msg_id else []
            if not uids and msg_id:
                uids = imap.search(["HEADER", "Message-ID", msg_id])
            if uids:
                data = next(iter(imap.fetch(uids[:1], ["RFC822"]).values()))
                print(f"   ↩️  Replaying parked SPM PO (re-fetched): {subject[:55]}")
                process_message(client_db, data)
            else:
                # Email no longer in IMAP — replay subject-only (best effort).
                # Supplement the subject with filenames of any locally-saved PDFs
                # for this ref so that Chevron POs only in the attachment name are found.
                spm_ref_local = p.get("po_number") or ""
                extra_names = ""
                if spm_ref_local:
                    from pathlib import Path as _Path
                    ack_dir = _Path(ACK_DIR)
                    if ack_dir.is_dir():
                        extra_names = " ".join(f.name for f in ack_dir.glob(f"{spm_ref_local}*.pdf"))
                augmented = f"{subject} {extra_names}".strip()
                print(f"   ↩️  Replaying parked SPM PO (subject-only): {subject[:55]}")
                process_outgoing_po(
                    client_db, None,
                    p["message_id"], p.get("sender") or "", augmented, email_date,
                )
            client_db.table("parked_emails").delete().eq("id", p["id"]).execute()
        except Exception as exc:
            print(f"   ⚠️  Replay failed for parked SPM PO {p.get('po_number')}: {exc}")

    # ── Case 3: existing SPM POs with partially-linked orders ────────────────
    # Two sub-cases handled here:
    #   (a) order didn't exist when the bundle was processed → link row missing entirely
    #   (b) link row exists with buyer_po_number set but order_id=NULL because the
    #       order arrived after the link was inserted
    # In both cases: fill in the order_id on the link and stamp spm_po_sent_at.
    spm_pos = (
        client_db.table("spm_purchase_orders")
        .select("id, spm_po_ref, spm_po_number, sent_to_supplier_at, supplier_id, so_number")
        .execute()
    )
    for spm_po in spm_pos.data:
        spm_po_number = spm_po.get("spm_po_number") or ""
        chevron_pos = extract_chevron_pos(spm_po_number)
        if not chevron_pos:
            continue
        links = (
            client_db.table("spm_po_chevron_links")
            .select("id, buyer_po_number, order_id")
            .eq("spm_po_id", spm_po["id"])
            .execute()
        )
        linked_with_order = {lnk["buyer_po_number"] for lnk in links.data if lnk.get("order_id")}
        null_order_links = {lnk["buyer_po_number"]: lnk["id"] for lnk in links.data if not lnk.get("order_id")}
        for po in chevron_pos:
            if po in linked_with_order:
                continue
            oid, matched_po = match_chevron_po(po, orders_by_number)
            if oid is None:
                continue
            try:
                if matched_po in null_order_links:
                    # Existing placeholder row — fill in the real order_id
                    client_db.table("spm_po_chevron_links").update({
                        "order_id": oid,
                    }).eq("id", null_order_links[matched_po]).execute()
                else:
                    client_db.table("spm_po_chevron_links").insert({
                        "spm_po_id": spm_po["id"],
                        "order_id": oid,
                        "buyer_po_number": matched_po,
                    }).execute()
                order_update = {
                    "spm_po_number": spm_po_number,
                    "spm_po_sent_at": spm_po["sent_to_supplier_at"],
                    "supplier_id": spm_po["supplier_id"],
                }
                target_status = "po_sent"
                # If the SPM PO's SO is already acknowledged, backfill this
                # late-linked order's promised_date + status from its dispatch
                # dates (the SO-ack loop only stamped orders linked at that time).
                if spm_po.get("so_number"):
                    dg = (
                        client_db.table("so_dispatch_groups")
                        .select("dispatch_date")
                        .eq("spm_po_id", spm_po["id"])
                        .execute()
                    )
                    dates = sorted(d["dispatch_date"] for d in dg.data if d.get("dispatch_date"))
                    if dates:
                        order_update["promised_date"] = dates[0]
                        target_status = "supplier_acknowledged"
                client_db.table("orders").update(order_update).eq("id", oid).execute()
                sync.advance_status(client_db, oid, target_status)  # monotonic
                print(f"   🔗 Late-linked {matched_po} → SPM PO {spm_po['spm_po_ref']}")
            except Exception:
                pass  # duplicate link — already there

    # ── Case 4: sent SPM POs whose Flexitallic SO ack was DROPPED ─────────────
    # An SO ack that arrived before its SPM PO existed (e.g. while the PO was
    # skipped by a subject typo) hit the relevance gate `if not spm_po.data:
    # return` and was silently discarded — no parking, no recovery. Now that
    # the SPM PO exists, actively pull its SO ack from Flexitallic.
    soless = (
        client_db.table("spm_purchase_orders")
        .select("id, spm_po_ref")
        .is_("so_number", "null")
        .execute()
    )
    for spo in soless.data:
        ref = spo["spm_po_ref"]
        try:
            uids = imap.search(["FROM", "flexitallic.eu", "SUBJECT", ref])
        except Exception:
            continue
        if not uids:
            continue
        for data in imap.fetch(uids, ["RFC822"]).values():
            if b"RFC822" not in data:
                continue
            m = email.message_from_bytes(data[b"RFC822"])
            subj = decode_mime_words(m.get("Subject", ""))
            low = subj.lower()
            if "acknowledgement" not in low and "acknowledgment" not in low:
                continue
            if extract_spm_ref(subj) != ref:
                continue
            mid = m.get("Message-ID", "")
            if mid and is_already_processed(mid):
                continue
            print(f"   ↩️  Recovering dropped SO ack for SPM {ref}: {subj[:50]}")
            process_message(client_db, data)
            break  # one SO ack per SPM PO


# ─────────────────────────────────────────────
# IMAP LOOP
# ─────────────────────────────────────────────

# Backfills are slow (per-SO BODY searches on All Mail). Run at most once per hour.
_last_backfill_ts: "datetime | None" = None
_BACKFILL_INTERVAL = 3600  # seconds


def connect_to_gmail() -> IMAPClient:
    host = os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com")
    port = int(os.environ.get("GMAIL_IMAP_PORT", 993))
    email_addr = os.environ["GMAIL_EMAIL"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]
    client = IMAPClient(host, port=port, use_uid=True, ssl=True)
    client.socket().settimeout(20)  # prevent silent hangs on slow BODY searches
    client.login(email_addr, app_password)
    client.select_folder("[Gmail]/All Mail")  # need sent + received
    return client


def _backfill_so_received_at(client_db) -> None:
    """
    so_received_at == the SO acknowledgment date.  Stamp it on any linked order
    where it's still NULL — covers orders processed before this column existed.
    No-op on a clean DB (spm_purchase_orders is empty).  Idempotent on re-runs
    (only writes where so_received_at IS NULL).
    """
    spm_pos = (
        client_db.table("spm_purchase_orders")
        .select("id, so_acknowledged_at")
        .not_.is_("so_acknowledged_at", "null")
        .execute()
    )
    backfilled = 0
    for spm_po in spm_pos.data:
        links = (
            client_db.table("spm_po_chevron_links")
            .select("order_id")
            .eq("spm_po_id", spm_po["id"])
            .execute()
        )
        for link in links.data:
            oid = link["order_id"]
            if not oid:
                continue
            order = (
                client_db.table("orders")
                .select("so_received_at")
                .eq("id", oid)
                .execute()
            )
            if order.data and order.data[0].get("so_received_at") is None:
                client_db.table("orders").update({
                    "so_received_at": spm_po["so_acknowledged_at"],
                }).eq("id", oid).execute()
                backfilled += 1
    if backfilled:
        print(f"   🔙 Backfilled so_received_at on {backfilled} order(s)")


def _backfill_missing_dispatch(client_db, imap, max_orders: int = 0) -> None:
    """
    For every SPM PO that has an SO number but still has NULL flex_dispatch_ready_at,
    search Gmail for any email FROM flexitallic.eu whose body contains that SO number.

    This catches dispatch-ready emails that the subject-based IMAP searches missed —
    e.g. Penny replying in an ack thread (subject has "Sales Acknowledgement" from the
    original thread, body has new packing details) or unusual subject formats.

    Each matching email is run through the normal process_message() routing.
    is_already_processed() prevents double-stamping.  NULL guards on the stamp
    functions ensure the earliest dispatch date wins.
    """
    spm_pos = (
        client_db.table("spm_purchase_orders")
        .select("id, spm_po_ref, so_number")
        .not_.is_("so_number", "null")
        .is_("flex_dispatch_ready_at", "null")
        .execute()
    )
    if not spm_pos.data:
        return

    since = sync._backfill_since()
    found_total = 0

    for spm_po in (spm_pos.data[:max_orders] if max_orders else spm_pos.data):
        so_number = spm_po["so_number"]
        try:
            uids = imap.search(["FROM", "flexitallic.eu", "BODY", so_number, "SINCE", since])
        except Exception:
            continue
        if not uids:
            continue

        msgs = imap.fetch(sorted(uids)[:20], ["RFC822", "INTERNALDATE"])
        time.sleep(0.5)
        _epoch = __import__("datetime").datetime.min
        for uid, data in sorted(msgs.items(), key=lambda x: x[1].get(b"INTERNALDATE") or _epoch):
            if b"RFC822" not in data:
                continue
            msg = email.message_from_bytes(data[b"RFC822"])
            message_id = msg.get("Message-ID", f"uid-{uid}").strip()
            sender = decode_mime_words(msg.get("From", "")).lower()
            subject = decode_mime_words(msg.get("Subject", ""))
            internal_date = data.get(b"INTERNALDATE")
            email_date = internal_date.isoformat() if internal_date else None

            # Only act on Flexitallic dispatch-ready emails — not acks from salesorder@
            if "salesorder@flexitallic.eu" in sender:
                continue

            body = get_email_body_text(msg)
            if "dispatch" not in body.lower() and "packed" not in body.lower() and "unicorn" not in body.lower():
                # Also check HTML parts for quoted dispatch keywords
                found_kw = False
                for part in (msg.walk() if msg.is_multipart() else [msg]):
                    ct = part.get_content_type()
                    if ct not in ("text/plain", "text/html"):
                        continue
                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue
                    text = payload.decode(errors="replace").lower()
                    if "dispatch" in text or "packed" in text or "unicorn" in text:
                        found_kw = True
                        break
                if not found_kw:
                    continue

            body_sos = extract_so_numbers_from_all_parts(msg)
            body_refs = extract_spm_refs_from_all_parts(msg)
            if not body_sos and not extract_so_number(subject) and not extract_spm_ref(subject):
                continue

            process_flex_dispatch_ready(
                client_db, msg, message_id, sender, subject, email_date,
                extra_so_numbers=body_sos, extra_spm_refs=body_refs,
            )
            found_total += 1

    if found_total:
        print(f"   [backfill] Processed {found_total} previously-missed dispatch email(s)")


def _backfill_ready_for_dispatch(client_db, imap, max_orders: int = 0) -> None:
    """
    For every SPM PO with an SO number but NULL ready_for_dispatch_at,
    search Gmail for Penny's 'arranged transport/collection' email.
    Body must contain arrange/collect + transport company keywords.
    """
    spm_pos = (
        client_db.table("spm_purchase_orders")
        .select("id, spm_po_ref, so_number")
        .not_.is_("so_number", "null")
        .is_("ready_for_dispatch_at", "null")
        .execute()
    )
    if not spm_pos.data:
        return

    since = sync._backfill_since()
    found_total = 0

    for spm_po in (spm_pos.data[:max_orders] if max_orders else spm_pos.data):
        so_number = spm_po["so_number"]
        try:
            uids = imap.search(["FROM", "flexitallic.eu", "BODY", so_number, "SINCE", since])
        except Exception:
            continue
        if not uids:
            continue

        _epoch = __import__("datetime").datetime.min
        msgs = imap.fetch(sorted(uids)[:20], ["RFC822", "INTERNALDATE"])
        time.sleep(0.5)
        for uid, data in sorted(msgs.items(), key=lambda x: x[1].get(b"INTERNALDATE") or _epoch):
            if b"RFC822" not in data:
                continue
            msg = email.message_from_bytes(data[b"RFC822"])
            message_id = msg.get("Message-ID", f"uid-{uid}").strip()
            sender = decode_mime_words(msg.get("From", "")).lower()
            subject = decode_mime_words(msg.get("Subject", ""))
            internal_date = data.get(b"INTERNALDATE")
            email_date = internal_date.isoformat() if internal_date else None

            if "salesorder@flexitallic.eu" in sender:
                continue

            # Must contain "arrange"/"collect"/"dispatch" + transport company name.
            # Covers both "arranged collection" and "we will dispatch to Unicorn today".
            full_text = ""
            for part in (msg.walk() if msg.is_multipart() else [msg]):
                ct = part.get_content_type()
                if ct in ("text/plain", "text/html"):
                    payload = part.get_payload(decode=True)
                    if payload:
                        full_text += payload.decode(errors="replace").lower()
            has_transport = "unicorn" in full_text or "transport" in full_text or "pudsey" in full_text or "rtc" in full_text
            has_arrange = "arrange" in full_text or "collect" in full_text or "dispatch" in full_text
            if not (has_transport and has_arrange):
                continue

            body_sos = extract_so_numbers_from_all_parts(msg)
            body_refs = extract_spm_refs_from_all_parts(msg)
            process_ready_for_dispatch(
                client_db, msg, message_id, sender, subject, email_date,
                extra_so_numbers=body_sos, extra_spm_refs=body_refs,
            )
            found_total += 1

    if found_total:
        print(f"   [backfill] Processed {found_total} previously-missed ready-for-dispatch email(s)")


def _backfill_dispatched(client_db, imap, max_orders: int = 0) -> None:
    """
    For every SPM PO with an SO number but NULL dispatched_at,
    search Gmail for the shipping company (Unicorn) 'Noted' reply.
    """
    spm_pos = (
        client_db.table("spm_purchase_orders")
        .select("id, spm_po_ref, so_number")
        .not_.is_("so_number", "null")
        .is_("dispatched_at", "null")
        .execute()
    )
    if not spm_pos.data:
        return

    since = sync._backfill_since()
    found_total = 0

    for spm_po in (spm_pos.data[:max_orders] if max_orders else spm_pos.data):
        so_number = spm_po["so_number"]
        try:
            uids = imap.search(["FROM", "unicornsl", "BODY", so_number, "SINCE", since])
            if not uids:
                uids = imap.search(["FROM", "unicorn", "BODY", so_number, "SINCE", since])
        except Exception:
            continue
        if not uids:
            continue

        _epoch = __import__("datetime").datetime.min
        msgs = imap.fetch(sorted(uids)[:20], ["RFC822", "INTERNALDATE"])
        time.sleep(0.5)
        for uid, data in sorted(msgs.items(), key=lambda x: x[1].get(b"INTERNALDATE") or _epoch):
            if b"RFC822" not in data:
                continue
            msg = email.message_from_bytes(data[b"RFC822"])
            message_id = msg.get("Message-ID", f"uid-{uid}").strip()
            sender = decode_mime_words(msg.get("From", "")).lower()
            subject = decode_mime_words(msg.get("Subject", ""))
            internal_date = data.get(b"INTERNALDATE")
            email_date = internal_date.isoformat() if internal_date else None

            if not is_freight_forwarder(sender):
                continue

            # Must contain "noted" or "confirm" somewhere in the email
            found_kw = False
            for part in (msg.walk() if msg.is_multipart() else [msg]):
                ct = part.get_content_type()
                if ct not in ("text/plain", "text/html"):
                    continue
                payload = part.get_payload(decode=True)
                if payload:
                    text = payload.decode(errors="replace").lower()
                    if "noted" in text or "confirm" in text:
                        found_kw = True
                        break
            if not found_kw:
                continue

            body_sos = extract_so_numbers_from_all_parts(msg)
            body_refs = extract_spm_refs_from_all_parts(msg)
            if not body_sos and not extract_so_number(subject) and not extract_spm_ref(subject):
                continue

            process_dispatched(
                client_db, msg, message_id, sender, subject, email_date,
                extra_so_numbers=body_sos, extra_spm_refs=body_refs,
            )
            found_total += 1

    if found_total:
        print(f"   [backfill] Processed {found_total} previously-missed dispatched email(s)")


def check_inbox_once() -> None:
    print("   Checking Gmail for supplier PO/SO emails...", end="", flush=True)
    client_db = get_client()
    imap = connect_to_gmail()
    try:
        folder = "[Gmail]/All Mail"
        account = "gmail_supplier"

        # Two tight subject-filtered searches instead of one broad FROM filter.
        # SPM sends ~4,000 emails since Jan; only ~132 are actual POs. Filtering
        # on subject server-side keeps this fast and relevant.
        #   - Outgoing POs:  FROM specialpiping  + subject "PURCHASE ORDER"
        #   - Incoming acks: FROM flexitallic.eu + subject "Acknowledgement"
        # Outgoing SPM POs to suppliers
        po_terms  = ["FROM", "specialpiping@gmail.com", "SUBJECT", "PURCHASE ORDER"]
        # Incoming SO acks from Flexitallic (salesorder@flexitallic.eu)
        so_terms  = ["FROM", "flexitallic.eu", "SUBJECT", "Acknowledgement"]
        # Flexitallic single-order dispatch-ready emails (SO/SPM ref in subject)
        flex_spm_terms = ["FROM", "flexitallic.eu", "SUBJECT", "S.P.M"]
        # Penny's bulk dispatch-ready emails ("RE: SPM, NIGERIA Orders ready for dispatch...")
        # — subject contains "dispatch" but no "S.P.M"; SO numbers are only in the body.
        flex_dispatch_bulk_terms = ["FROM", "flexitallic.eu", "SUBJECT", "dispatch"]
        # Penny's SO-subject dispatch-ready emails ("RE: SO718143 / SO717051")
        # — subject has SO numbers but no "S.P.M" or "dispatch"; body has "packed".
        flex_so_subject_terms = ["FROM", "flexitallic.eu", "SUBJECT", "SO"]
        # SPM replying to Flexitallic with dispatch instructions (To: @flexitallic.eu)
        dispatch_reply_terms = ["FROM", "specialpiping@gmail.com", "TO", "flexitallic.eu"]
        # SPM forwarding the SO to the warehouse
        wh_so_terms = ["FROM", "specialpiping@gmail.com", "SUBJECT", "Acknowledgement"]
        # Shipping company (Unicorn) "Noted" reply — dispatched confirmation
        freight_fwd_terms = ["FROM", "unicornsl"]

        cursor = sync.get_cursor(account, folder)
        last_uid = cursor["last_uid"]

        # Get UIDVALIDITY once; reset cursor if the folder was rebuilt.
        folder_status = imap.folder_status(folder, [b"UIDVALIDITY"])
        uidvalidity = int(folder_status[b"UIDVALIDITY"])
        if cursor["uidvalidity"] is not None and int(cursor["uidvalidity"]) != uidvalidity:
            last_uid = 0

        since = sync._backfill_since()

        po_ids           = imap.search(po_terms               + ["SINCE", since])
        so_ids           = imap.search(so_terms               + ["SINCE", since])
        flex_spm_ids     = imap.search(flex_spm_terms           + ["SINCE", since])
        flex_bulk_ids    = imap.search(flex_dispatch_bulk_terms + ["SINCE", since])
        flex_so_ids      = imap.search(flex_so_subject_terms   + ["SINCE", since])
        dispatch_ids     = imap.search(dispatch_reply_terms    + ["SINCE", since])
        wh_so_ids        = imap.search(wh_so_terms             + ["SINCE", since])
        freight_fwd_ids  = imap.search(freight_fwd_terms       + ["SINCE", since])

        # PO + SO acks: cursor-gated — these were processed in previous runs.
        cursor_gated = [uid for uid in sorted(set(po_ids) | set(so_ids)) if uid > last_uid]

        # New-workflow searches (flex dispatch-ready, dispatch instructions,
        # SO→warehouse forward, freight forwarder Noted): cursor-gated now that
        # the backfill functions handle all historical catch-up. This prevents
        # re-downloading 1000+ already-processed emails on every run.
        new_workflow = sorted(uid for uid in (set(flex_spm_ids) | set(flex_bulk_ids) | set(flex_so_ids) | set(dispatch_ids) | set(wh_so_ids) | set(freight_fwd_ids)) if uid > last_uid)

        new_ids = sorted(set(cursor_gated) | set(new_workflow))

        if not new_ids:
            print(" (nothing new)")
            return

        print(f" {len(new_ids)} relevant message(s)...")
        highest = last_uid

        FETCH_BATCH = 100
        for i in range(0, len(new_ids), FETCH_BATCH):
            chunk = new_ids[i:i + FETCH_BATCH]
            messages = imap.fetch(chunk, ["RFC822"])
            for uid in chunk:
                if uid in messages:
                    process_message(client_db, messages[uid])
                highest = max(highest, uid)
            sync.set_cursor(account, folder, highest, uidvalidity)
            print(f"   ...processed {min(i + FETCH_BATCH, len(new_ids))}/{len(new_ids)}")

        # Catch-up: runs once per hour, after the main scan so new emails are
        # always processed first.
        global _last_backfill_ts
        now = datetime.now()
        if (_last_backfill_ts is None
                or (now - _last_backfill_ts).total_seconds() >= _BACKFILL_INTERVAL):
            try:
                imap.logout()
            except Exception:
                pass
            imap = connect_to_gmail()
            _backfill_so_received_at(client_db)
            _backfill_missing_dispatch(client_db, imap, max_orders=5)
            _backfill_ready_for_dispatch(client_db, imap, max_orders=5)
            _backfill_dispatched(client_db, imap, max_orders=5)
            _recover_spm_pos(client_db, imap)
            _last_backfill_ts = now
    finally:
        imap.logout()
        
def run_forever() -> None:
    interval = int(os.environ.get("CHECK_INTERVAL_SECONDS", 120))

    tee = _setup_log_tee()
    if tee:
        sys.stdout = tee

    print(f"📦 Supplier PO/SO parser started. Checking every {interval}s. Ctrl+C to stop.")
    if tee:
        print(f"   Logging to: {tee._path}")

    consecutive_errors = 0
    while True:
        try:
            check_inbox_once()
            consecutive_errors = 0
        except KeyboardInterrupt:
            print("\n⏹  Stopped by user.")
            break
        except Exception as e:
            consecutive_errors += 1
            backoff = min(30 * (2 ** (consecutive_errors - 1)), 600)
            is_imap = any(
                kw in repr(e).lower()
                for kw in ("imap", "login", "connect", "socket", "ssl", "overquota", "timeout")
            )
            print(f"\n❌ Error (failure #{consecutive_errors}) [{type(e).__name__}]: {e!r}")
            if consecutive_errors >= 3 and is_imap:
                print(
                    f"⚠️  {consecutive_errors} consecutive IMAP failures — "
                    "check Gmail account / app-password / network connectivity."
                )
            elif consecutive_errors >= 5:
                print(
                    f"⚠️  {consecutive_errors} consecutive failures — "
                    "check logs and environment immediately."
                )
            print(f"   Retrying in {backoff}s...")
            time.sleep(backoff)
            continue
        time.sleep(interval)


if __name__ == "__main__":
    run_forever()

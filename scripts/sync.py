"""
sync.py — shared live-system plumbing used by all three listeners.

Why this exists
---------------
The original listeners each re-scanned a fixed tail of their mailbox
(`[-50:]` / `[-100:]`) every poll. That caused two failures:

  1. Anything older than the window was never processed. With a noisy
     inbox (GEP "Event"/RFQ spam), real PO notifications fell out of the
     50-message Yahoo window and were silently missed.

  2. The two mailboxes are scanned independently, so a Gmail warehouse-
     routing email could be read BEFORE the Yahoo PO-notification that
     creates the order — producing "no matching order yet" and dropping
     the link forever.

This module fixes both, the way real email pipelines do:

  * UID HIGH-WATER MARK (cursor): we remember the highest IMAP UID
    processed per folder in `sync_state`, then fetch only UIDs greater
    than it. Every new email is processed exactly once, no matter how
    many arrive between polls. No fixed window.

  * DEFERRED MATCHING (parking + reconcile): an email that references a
    PO with no order row yet is parked in `parked_emails` instead of
    discarded. Whenever an order is (later) created, `reconcile_po()`
    applies any parked emails for that PO. Mailbox ordering no longer
    matters.

Everything here is Claude-free so it works even with empty API credits.
"""

import io
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

try:
    import pdfplumber as _pdfplumber
except ImportError:
    _pdfplumber = None

from db import get_client, normalize_po_number


# ------------------------------------------------------------
# PO-number extraction — robust against the real-world noise seen
# in production subjects.
#
# Chevron buyer POs are 10 digits beginning with "006" (e.g.
# 0061442579). The naive r"(\d{8,12})" used before grabbed the WRONG
# number in two real cases from the logs:
#   * "BranchPlant 29300000TL ..." -> captured 29300000 (a branch plant)
#   * "...JDEJobID 63437,BranchPlant 29300000TL,Supplier 99999999
#      (0061442579)" -> the real PO is in parentheses at the end.
#
# Strategy:
#   1. Prefer a PO in parentheses (the GEP notification format).
#   2. Otherwise, take the FIRST token matching the Chevron PO shape
#      (006 + 7 digits), which excludes branch plants/supplier refs.
# ------------------------------------------------------------
# The optional "(?:\s*-\s*\d{1,3})?" tail captures a change-order revision
# suffix like "-001" (which may render "0060792432 - 001" with spaces in PDFs).
# Keeping the "006" anchor still excludes branch-plant / supplier-ref numbers.
CHEVRON_PO_RE = re.compile(r"\b(006\d{7}(?:\s*-\s*\d{1,3})?)")
PO_IN_PARENS_RE = re.compile(r"\((006\d{7}(?:\s*-\s*\d{1,3})?)\)")

# A warehouse-ROUTING subject is just the bare Chevron PO number, optionally
# with any number of 'Re:'/'Fwd:'/'Fw:' reply/forward prefixes. This is the
# cheap pre-filter used during backfill so we only download bodies for genuine
# routing candidates — NOT every "PURCHASE ORDER (... 006xxxxxxx ...)" or
# "MTR for SPM PO 006xxxxxxx" email that merely mentions a PO number.
BARE_PO_SUBJECT_RE = re.compile(
    r"^\s*(?:(?:re|fwd?)\s*:\s*)*\(?\s*(006\d{7})\s*\)?\s*$",
    re.IGNORECASE,
)

# ── NLNG PO numbers ──────────────────────────────────────────────────────────
# NLNG SAP generates 9-digit PO numbers starting with 4200 (e.g. 4200083212).
# Warehouse routing emails from SPM have the subject "PO No. 4200083212"
# (the original NLNG subject forwarded verbatim).
NLNG_PO_RE = re.compile(r"\b(4200\d{5,6})\b")

# Matches both the bare number AND "PO No. XXXXXXXXX" with Re:/Fwd: prefixes.
NLNG_PO_SUBJECT_RE = re.compile(
    r"^\s*(?:(?:re|fwd?)\s*:\s*)*(?:PO\s*No\.?\s*)?(4200\d{5,6})\s*$",
    re.IGNORECASE,
)


def is_bare_po_subject(subject: str) -> str | None:
    """
    Return the PO number if the subject is essentially just a PO number,
    else None. Tolerates:
      - leading reply/forward prefixes (Re:, Fwd:, Fw:)
      - a trailing -N line-split suffix (e.g. 0060792432-001)
      - a trailing parenthetical marker (e.g. 0061409670(Recreated))
    """
    if not subject:
        return None
    s = subject.strip()
    # Strip leading Re:/Fwd:/Fw: prefixes (possibly stacked)
    s = re.sub(r"^(?:\s*(?:re|fwd|fw)\s*:\s*)+", "", s, flags=re.IGNORECASE).strip()
    # Match the PO, KEEPING an optional -NNN revision suffix, plus an optional
    # trailing (parenthetical) marker like "(Recreated)".
    m = re.match(r"^(006\d{7}(?:\s*-\s*\d{1,3})?)\s*(?:\([^)]*\))?$", s)
    return normalize_po_number(m.group(1)) if m else None


def is_nlng_po_subject(subject: str) -> str | None:
    """
    Return the NLNG PO number if the subject is an NLNG PO subject, else None.

    Matches:
      - "PO No. 4200083212"          (exact NLNG format from SAP)
      - "Fwd: PO No. 4200083212"     (forwarded)
      - "Re: PO No. 4200083212"      (replied)
      - "4200083212"                 (bare number, if SPM uses it)
    """
    if not subject:
        return None
    m = NLNG_PO_SUBJECT_RE.match(subject.strip())
    return m.group(1) if m else None


# ── NLNG order status pipeline ───────────────────────────────────────────────

NLNG_STATUS_RANK = {
    "notification_received":              0,
    "awaiting_warehouse_stock_check":     1,
    "stock_check_complete":               2,
    "po_sent":                            3,
    "awaiting_supplier_so":               4,
    "supplier_acknowledged":              5,
    "so_sent_to_warehouse":               6,
    "dispatch_packed_awaiting_instruction": 7,
    "dispatch_instruction_sent":          8,
    "ready_for_dispatch":                 9,
    "dispatched":                         10,
    "delivered":                          11,
}


def nlng_derive_status(o: dict) -> str:
    """Furthest pipeline stage justified by NLNG order milestone data."""
    if o.get("delivered_at"):
        return "delivered"
    if o.get("dispatched_at"):
        return "dispatched"
    if o.get("ready_for_dispatch_at"):
        return "ready_for_dispatch"
    if o.get("dispatch_instructions_sent_at"):
        return "dispatch_instruction_sent"
    if o.get("flex_dispatch_ready_at"):
        return "dispatch_packed_awaiting_instruction"
    if o.get("so_sent_to_warehouse_at"):
        return "so_sent_to_warehouse"
    if o.get("promised_date") or o.get("so_received_at"):
        return "supplier_acknowledged"
    if o.get("spm_po_sent_at"):
        return "po_sent"
    if o.get("stock_check_completed_at"):
        return "stock_check_complete"
    if o.get("sent_to_warehouse_at"):
        return "awaiting_warehouse_stock_check"
    return "notification_received"


def nlng_advance_status(client, order_id: str) -> None:
    """Re-derive and write overall_status for an NLNG order if it has moved forward."""
    res = (
        client.table("nlng_orders")
        .select(
            "id, overall_status, sent_to_warehouse_at, stock_check_completed_at,"
            "spm_po_sent_at, so_received_at, promised_date, so_sent_to_warehouse_at,"
            "flex_dispatch_ready_at, dispatch_instructions_sent_at,"
            "ready_for_dispatch_at, dispatched_at, delivered_at"
        )
        .eq("id", order_id)
        .execute()
    )
    if not res.data:
        return
    o = res.data[0]
    new_status = nlng_derive_status(o)
    current_rank = NLNG_STATUS_RANK.get(o.get("overall_status") or "", -1)
    new_rank = NLNG_STATUS_RANK.get(new_status, -1)
    if new_rank > current_rank:
        client.table("nlng_orders").update({"overall_status": new_status}).eq("id", order_id).execute()


def stamp_nlng_sent_to_warehouse(order_id: str, email_date: str, body_text: str | None = None) -> None:
    """Stamp sent_to_warehouse_at on an NLNG order and advance its status."""
    client = get_client()
    update: dict = {"sent_to_warehouse_at": email_date}
    if body_text:
        update["warehouse_routing_raw"] = body_text.strip()
    client.table("nlng_orders").update(update).eq("id", order_id).execute()
    nlng_advance_status(client, order_id)


def stamp_nlng_stock_check(order_id: str, email_date: str,
                           stock_check_data: "str | dict | None" = None) -> None:
    """Stamp stock_check_completed_at on an NLNG order and advance its status.
    stock_check_data may be a raw body string (backfill) or a structured result dict (live).
    """
    client = get_client()
    update: dict = {"stock_check_completed_at": email_date}
    if stock_check_data:
        update["stock_check_raw"] = (
            stock_check_data.strip() if isinstance(stock_check_data, str) else stock_check_data
        )
    client.table("nlng_orders").update(update).eq("id", order_id).execute()
    nlng_advance_status(client, order_id)


def stamp_nlng_delivered(order_id: str, email_date: str) -> None:
    """Stamp delivered_at on an NLNG order (first occurrence wins)."""
    client = get_client()
    client.table("nlng_orders").update({
        "delivered_at": email_date,
    }).eq("id", order_id).is_("delivered_at", "null").execute()
    nlng_advance_status(client, order_id)


def find_nlng_order_by_po(po_number: str) -> dict | None:
    """Return the nlng_orders row for this PO number (any variation), or None."""
    client = get_client()
    res = (
        client.table("nlng_orders")
        .select("id, po_number, variation_number, overall_status")
        .eq("po_number", po_number)
        .order("variation_number", desc=True)   # highest variation = most recent
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


# ── NLNG SPM PO / Flexitallic SO detection ───────────────────────────────────

def spm_po_from_attachment(msg) -> str | None:
    """
    Check the PDF attachment filename(s) of an email for an SPM PO number.
    Returns the formatted PO string (e.g. "S.P.M.-3039") or None.

    The attachment filename is more reliable than the subject when emails are
    sent as replies — the subject stays the same but the attached PO document
    gets the correct revised number.

    Only considers filenames that look like PO documents (contain "NLNG",
    "FLEXITALLIC", or "PURCHASE ORDER") to avoid false matches from other
    attachments that happen to contain "S.P.M" in an unrelated context.
    Extracted number must also be at least 3 digits.
    """
    _PO_DOC_MARKERS = ("NLNG", "FLEXITALLIC", "PURCHASE ORDER")
    for part in msg.walk():
        raw_name = part.get_filename() or ""
        if not raw_name:
            continue
        name = raw_name.decode("utf-8", errors="ignore") if isinstance(raw_name, bytes) else raw_name
        if not name.upper().endswith(".PDF"):
            continue
        name_upper = name.upper()
        if not any(marker in name_upper for marker in _PO_DOC_MARKERS):
            continue
        # Format B in filename: "S.P.M-NLNG-3039"
        m = re.search(r"S\.P\.M\.?\s*-\s*NLNG\s*-\s*(\d{3,})", name, re.IGNORECASE)
        if m:
            return f"S.P.M.-{m.group(1)}"
        # Format A in filename: "S.P.M. - 3039"
        m = re.search(r"S\.P\.M\.?\s*[-–]\s*([\d.]{3,})", name, re.IGNORECASE)
        if m:
            return f"S.P.M.-{m.group(1).rstrip('.')}"
    return None


def nlng_pos_from_attachment(msg) -> list[str]:
    """
    Extract NLNG PO numbers (4200XXXXXX) from PDF attachment filenames AND
    from the PDF text content. Catches orders whose PO number only appears
    inside the attached purchase order document, not in the email subject.
    """
    _PO_DOC_MARKERS = ("NLNG", "FLEXITALLIC", "PURCHASE ORDER")
    found = []
    for part in msg.walk():
        raw_name = part.get_filename() or ""
        if not raw_name:
            continue
        name = raw_name.decode("utf-8", errors="ignore") if isinstance(raw_name, bytes) else raw_name
        if not name.upper().endswith(".PDF"):
            continue
        name_upper = name.upper()
        if not any(marker in name_upper for marker in _PO_DOC_MARKERS):
            continue
        # Scan filename
        found.extend(re.findall(r"(?<!\d)4200\d{6}(?!\d)", name))
        # Scan PDF text content
        if _pdfplumber is not None:
            pdf_bytes = part.get_payload(decode=True)
            if pdf_bytes:
                try:
                    with _pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                        for page in pdf.pages:
                            text = page.extract_text() or ""
                            found.extend(re.findall(r"(?<!\d)4200\d{6}(?!\d)", text))
                except Exception:
                    pass
    return list(dict.fromkeys(found))


def chevron_pos_from_attachment(msg) -> list[str]:
    """
    Extract Chevron PO numbers (006XXXXXXX) from ALL PDF attachment filenames
    and PDF text content. Always used together with subject extraction so
    no relevant PO is skipped when it only appears in the attached document.
    """
    found = []
    for part in msg.walk():
        raw_name = part.get_filename() or ""
        if not raw_name:
            continue
        name = raw_name.decode("utf-8", errors="ignore") if isinstance(raw_name, bytes) else raw_name
        if not name.upper().endswith(".PDF"):
            continue
        # Scan filename
        m = PO_IN_PARENS_RE.search(name) or CHEVRON_PO_RE.search(name)
        if m:
            found.append(normalize_po_number(m.group(1)))
        # Scan PDF text content
        if _pdfplumber is not None:
            pdf_bytes = part.get_payload(decode=True)
            if pdf_bytes:
                try:
                    with _pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                        for page in pdf.pages:
                            text = page.extract_text() or ""
                            for pm in CHEVRON_PO_RE.finditer(text):
                                po = normalize_po_number(pm.group(1))
                                if po:
                                    found.append(po)
                except Exception:
                    pass
    return list(dict.fromkeys(found))


# Subject: "Flexitallic Sales Acknowledgement for SO714770. Your PO number - S.P.M.-3071.-NLNG-4200."
FLEX_SO_SUBJECT_RE = re.compile(
    r"Flexitallic\s+Sales\s+Acknowledgement\s+for\s+(SO\d+)",
    re.IGNORECASE,
)

# Extracts "3071" from "S.P.M. - 3071.-NLNG-4200..."
_FLEX_SPM_NUM_RE = re.compile(r"S\.P\.M\.?\s*[-–]\s*([\d.]+)", re.IGNORECASE)


def _nlng_spm_ref(subject: str) -> str | None:
    """
    Extract the numeric SPM PO reference from any known NLNG PO subject format.

    Tries each format in order, most specific first:
      A / A2  S.P.M. - <ref> (dots, ref BEFORE NLNG)
              "PURCHASE ORDER-S.P.M. - 3046.-NLNG-..."
              "S.P.M. - 3064.-NLNG-..."            (no PURCHASE ORDER prefix)
              "PURCHASE ORDER-S.P.M. - 2016 - NLNG ..."  (space before NLNG)
      B / B2  S.P.M-NLNG-<ref> (dots, NLNG immediately after S.P.M, ref after NLNG)
              "S.P.M-NLNG-3009-4200087186-..."
              "PURCHASE ORDER-S.P.M-NLNG-3051-..."
      C / C2  SPM-NLNG-<ref>  (no dots, legacy)
              "SPM-NLNG-706-4200023626-..."
              "PURCHASE ORDER -SPM-NLNG-706-..."
      D       SPM/NLNG/<ref>  (slash format, legacy)
              "SPM/NLNG/605/4200014036/FLEXITALLIC"
    """
    # A/A2: After "S.P.M." + separator, a digit comes next (not a word like NLNG).
    m = re.search(r"S\.P\.M\.?\s*[-–]\s*(\d[\d.]*)", subject, re.IGNORECASE)
    if m:
        return m.group(1).rstrip(".")

    # B/B2: NLNG immediately follows S.P.M., ref comes after NLNG.
    m = re.search(r"S\.P\.M\.?\s*-\s*NLNG\s*[-/]\s*(\d{3,6})", subject, re.IGNORECASE)
    if m:
        return m.group(1)

    # C/C2: No dots on SPM, hyphen separators.
    m = re.search(r"\bSPM\s*-\s*NLNG\s*-\s*(\d{3,6})", subject, re.IGNORECASE)
    if m:
        return m.group(1)

    # D: Slash separators.
    m = re.search(r"\bSPM\s*/\s*NLNG\s*/\s*(\d{3,6})", subject, re.IGNORECASE)
    if m:
        return m.group(1)

    return None


def is_nlng_spm_po_subject(subject: str) -> list[tuple[str, str]]:
    """
    If this is an SPM PO to Flexitallic for NLNG, return a list of
    (spm_po, nlng_po) pairs — one per NLNG PO bundled in the email.
    Returns [] if no known format matches.

    All 4200xxxxxx numbers in the subject are captured (deduped, order preserved).
    """
    if not subject or "NLNG" not in subject.upper():
        return []

    spm_ref = _nlng_spm_ref(subject)
    if not spm_ref:
        return []

    # 10-digit NLNG SAP PO numbers: 4200 + exactly 6 digits, no adjacent digits.
    nlng_pos = list(dict.fromkeys(re.findall(r"(?<!\d)4200\d{6}(?!\d)", subject)))
    if not nlng_pos:
        return []

    return [(f"S.P.M.-{spm_ref}", po) for po in nlng_pos]


def is_flex_so_subject(subject: str) -> tuple[str, str | None] | None:
    """
    If this is a Flexitallic SO subject (including Fwd: prefixes), return
    (so_number, spm_po_number_or_None).

    Format A: "...SO714770. Your PO number - S.P.M.-3071.-NLNG-4200."
              → ("SO714770", "S.P.M.-3071")
    Format B: "...SO708576. Your PO number - S.P.M-NLNG-3039-420008."
              → ("SO708576", "S.P.M.-3039")
    """
    if not subject:
        return None
    m = FLEX_SO_SUBJECT_RE.search(subject)
    if not m:
        return None
    so_number = m.group(1)
    # Format B first: "S.P.M-NLNG-3039"
    spm_m = re.search(r"S\.P\.M\.?\s*-\s*NLNG\s*-\s*(\d{3,})", subject, re.IGNORECASE)
    if spm_m:
        spm_po = f"S.P.M.-{spm_m.group(1)}"
    else:
        # Format A: "S.P.M. - 3071"
        spm_m = _FLEX_SPM_NUM_RE.search(subject)
        spm_po = f"S.P.M.-{spm_m.group(1).rstrip('.')}" if spm_m else None
    return so_number, spm_po


def find_all_nlng_orders_by_spm_po(spm_po_number: str) -> list[dict]:
    """Return ALL nlng_orders rows whose spm_po_number matches.
    One SPM PO can cover multiple NLNG orders bundled in the same email.
    """
    client = get_client()
    res = (
        client.table("nlng_orders")
        .select("id, po_number, spm_po_number, overall_status")
        .eq("spm_po_number", spm_po_number)
        .order("created_at")
        .execute()
    )
    if res.data:
        return res.data
    # Prefix match — in case the stored value includes a longer suffix
    res = (
        client.table("nlng_orders")
        .select("id, po_number, spm_po_number, overall_status")
        .like("spm_po_number", f"{spm_po_number}%")
        .order("created_at")
        .execute()
    )
    return res.data or []


def find_nlng_order_by_spm_po(spm_po_number: str) -> dict | None:
    """Return the first nlng_orders row whose spm_po_number matches, or None.
    Use find_all_nlng_orders_by_spm_po when one SPM PO may cover multiple orders.
    """
    rows = find_all_nlng_orders_by_spm_po(spm_po_number)
    return rows[0] if rows else None


def stamp_nlng_spm_po(order_id: str, spm_po_number: str, email_date: str) -> None:
    """
    Stamp spm_po_number + spm_po_sent_at on an NLNG order.
    Latest date wins. If dates are equal but the PO number differs (a correction
    from a revised attachment), the new value still overwrites.
    """
    client = get_client()
    existing = client.table("nlng_orders").select("spm_po_number, spm_po_sent_at").eq("id", order_id).execute()
    existing_row = existing.data[0] if existing.data else {}
    existing_date = existing_row.get("spm_po_sent_at") or ""
    existing_spm_po = existing_row.get("spm_po_number") or ""
    if existing_date:
        if email_date < existing_date:
            return  # older email — never overwrite a newer stamp
        if email_date == existing_date and existing_spm_po == spm_po_number:
            return  # identical — nothing to do
    client.table("nlng_orders").update({
        "spm_po_number": spm_po_number,
        "spm_po_sent_at": email_date,
    }).eq("id", order_id).execute()
    nlng_advance_status(client, order_id)


def stamp_nlng_so(order_id: str, so_number: str, email_date: str,
                  so_pdf_url: str | None = None,
                  promised_date: str | None = None) -> None:
    """
    Stamp so_number + so_received_at on an NLNG order.
    Overwrites with the most recent SO if multiple exist for one NLNG PO.
    """
    client = get_client()
    existing = client.table("nlng_orders").select("so_received_at").eq("id", order_id).execute()
    existing_date = (existing.data[0].get("so_received_at") or "") if existing.data else ""
    if existing_date and existing_date >= email_date:
        return
    update: dict = {"so_number": so_number, "so_received_at": email_date}
    if so_pdf_url:
        update["so_pdf_url"] = so_pdf_url
    if promised_date:
        update["promised_date"] = promised_date
    client.table("nlng_orders").update(update).eq("id", order_id).execute()
    nlng_advance_status(client, order_id)


def stamp_nlng_so_to_warehouse(order_id: str, email_date: str,
                               so_number: str | None = None) -> None:
    """
    Stamp so_sent_to_warehouse_at on an NLNG order.
    Overwrites with the most recent forward date.
    If so_number is supplied and the field is not yet set, also writes so_number.
    """
    client = get_client()
    existing = (
        client.table("nlng_orders")
        .select("so_sent_to_warehouse_at, so_number")
        .eq("id", order_id)
        .execute()
    )
    row = existing.data[0] if existing.data else {}
    existing_date = row.get("so_sent_to_warehouse_at") or ""
    if existing_date and existing_date >= email_date:
        return
    update: dict = {"so_sent_to_warehouse_at": email_date}
    if so_number and not row.get("so_number"):
        update["so_number"] = so_number
    client.table("nlng_orders").update(update).eq("id", order_id).execute()
    nlng_advance_status(client, order_id)


def find_nlng_order_by_so_number(so_number: str) -> dict | None:
    """Return the nlng_orders row whose so_number matches, or None."""
    client = get_client()
    res = (
        client.table("nlng_orders")
        .select("id, po_number, so_number, overall_status")
        .eq("so_number", so_number)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def find_all_nlng_orders_by_so_number(so_number: str) -> list[dict]:
    """Return all nlng_orders rows sharing this SO number.
    One SO can cover multiple NLNG POs (e.g. SO708576 covers 4 orders).
    """
    client = get_client()
    res = (
        client.table("nlng_orders")
        .select("id, po_number, so_number, overall_status")
        .eq("so_number", so_number)
        .execute()
    )
    return res.data or []


def stamp_nlng_flex_dispatch_ready(order_id: str, email_date: str) -> None:
    """Stamp flex_dispatch_ready_at on an NLNG order (first occurrence wins)."""
    client = get_client()
    client.table("nlng_orders").update({
        "flex_dispatch_ready_at": email_date,
    }).eq("id", order_id).is_("flex_dispatch_ready_at", "null").execute()
    nlng_advance_status(client, order_id)


def stamp_nlng_dispatch_instructions_sent(order_id: str, email_date: str) -> None:
    """Stamp dispatch_instructions_sent_at on an NLNG order (first occurrence wins)."""
    client = get_client()
    client.table("nlng_orders").update({
        "dispatch_instructions_sent_at": email_date,
    }).eq("id", order_id).is_("dispatch_instructions_sent_at", "null").execute()
    nlng_advance_status(client, order_id)


def stamp_nlng_ready_for_dispatch(order_id: str, email_date: str) -> None:
    """Stamp ready_for_dispatch_at on an NLNG order (first occurrence wins)."""
    client = get_client()
    client.table("nlng_orders").update({
        "ready_for_dispatch_at": email_date,
    }).eq("id", order_id).is_("ready_for_dispatch_at", "null").execute()
    nlng_advance_status(client, order_id)


def stamp_nlng_dispatched(order_id: str, email_date: str) -> None:
    """Stamp dispatched_at on an NLNG order (first occurrence wins)."""
    client = get_client()
    client.table("nlng_orders").update({
        "dispatched_at": email_date,
    }).eq("id", order_id).is_("dispatched_at", "null").execute()
    nlng_advance_status(client, order_id)


def extract_po_number(text: str) -> str | None:
    """Best-effort single Chevron PO number (with revision suffix) from a
    subject or body. Prefer a 'To (...)' change-order target, then any
    parenthesised PO, then the first bare Chevron-shaped token."""
    if not text:
        return None
    to_m = re.search(r"To\s*" + PO_IN_PARENS_RE.pattern, text)
    if to_m:
        return normalize_po_number(to_m.group(1))
    m = PO_IN_PARENS_RE.search(text)
    if m:
        return normalize_po_number(m.group(1))
    m = CHEVRON_PO_RE.search(text)
    if m:
        return normalize_po_number(m.group(1))
    return None


def extract_all_po_numbers(text: str) -> list[str]:
    """All distinct Chevron PO numbers (revision-normalised) in a subject/body."""
    if not text:
        return []
    seen = []
    for m in CHEVRON_PO_RE.finditer(text):
        po = normalize_po_number(m.group(1))
        if po and po not in seen:
            seen.append(po)
    return seen


# ------------------------------------------------------------
# Order status — a MONOTONIC pipeline. overall_status is written by several
# independent parsers; without a rank, a late-processed early-stage event
# (e.g. a warehouse stock-check reconcile) would overwrite a later stage
# (supplier_acknowledged) and regress the order. So we (a) rank the statuses
# and (b) derive the true furthest stage from the milestone data.
# ------------------------------------------------------------
STATUS_RANK = {
    "new": 0,
    "pending_acknowledgment": 1,
    "acknowledged": 2,
    "awaiting_warehouse_stock_check": 3,
    "stock_check_needs_review": 4,
    "stock_check_complete": 5,
    "pricing": 6,
    "po_sent": 7,
    "awaiting_supplier_so": 8,
    "supplier_acknowledged": 9,
    # ── Dispatch pipeline ────────────────────────────────────────
    "dispatch_packed_awaiting_instruction": 10,   # Penny: "packed and ready"
    "dispatch_instruction_sent": 11,              # SPM: "ship to Unicorn"
    "so_sent_to_warehouse": 12,                   # SPM forwards SO to warehouse
    "ready_for_dispatch": 13,                     # Penny: "arranged transport/collection"
    "dispatched": 14,                             # shipping company: "Noted"
    "delivery_requested": 15,                     # warehouse: REQUEST FOR DELIVERY
    "delivered": 16,
    # ── Post-delivery ────────────────────────────────────────────
    "waybill_received": 17,
    "invoiced": 18,
    "paid": 19,
    "closed": 20,
}


def derive_status(o: dict) -> str:
    """The furthest pipeline stage justified by the order's milestone data."""
    if o.get("delivered_at"):
        return "delivered"
    if o.get("delivery_requested_at"):
        return "delivery_requested"
    if o.get("dispatched_at"):
        return "dispatched"
    if o.get("ready_for_dispatch_at"):
        return "ready_for_dispatch"
    if o.get("so_sent_to_warehouse_at"):
        return "so_sent_to_warehouse"
    if o.get("dispatch_instructions_sent_at"):
        return "dispatch_instruction_sent"
    if o.get("flex_dispatch_ready_at"):
        return "dispatch_packed_awaiting_instruction"
    if o.get("promised_date") or o.get("so_received_at"):
        return "supplier_acknowledged"
    if o.get("spm_po_sent_at"):
        return "po_sent"
    if o.get("stock_check_completed_at"):
        raw = o.get("stock_check_raw") or {}
        needs = isinstance(raw, dict) and (
            raw.get("needs_human_review") or raw.get("confidence") == "low"
        )
        return "stock_check_needs_review" if needs else "stock_check_complete"
    if o.get("sent_to_warehouse_at"):
        return "awaiting_warehouse_stock_check"
    if o.get("acknowledged_at") or o.get("acknowledgment_status") == "acknowledged":
        return "acknowledged"
    if o.get("notification_received_at"):
        return "pending_acknowledgment"
    return "new"


def rank(status: str) -> int:
    return STATUS_RANK.get(status or "new", 0)


def advance_status(client, order_id: str, new_status: str) -> None:
    """Set overall_status ONLY if it advances the order (never regress).
    Uses a single UPDATE … WHERE overall_status IN (lower_ranks) to avoid
    a read-then-write race while remaining compatible with PostgREST."""
    lower_statuses = [s for s, r in STATUS_RANK.items() if r < rank(new_status)]
    # NULL-safe: include "new" (rank 0) which is the implicit default for fresh rows.
    # PostgREST's .in_() cannot match NULL; any row with a genuinely-NULL status
    # also needs updating — handle that with a separate pass.
    import os as _os
    _dbg = _os.environ.get("STATUS_TRACE")
    if _dbg:
        cur = client.table("orders").select("overall_status").eq("id", order_id).execute().data
        current = cur[0]["overall_status"] if cur else "new"
        with open(_dbg, "a", encoding="utf-8") as _f:
            _f.write(f"advance {order_id[:8]} {current}(r{rank(current)}) -> "
                     f"{new_status}(r{rank(new_status)})\n")
    if lower_statuses:
        client.table("orders").update({"overall_status": new_status}).eq(
            "id", order_id
        ).in_("overall_status", lower_statuses).execute()
    # Also advance rows where overall_status is NULL (brand-new, unset row).
    client.table("orders").update({"overall_status": new_status}).eq(
        "id", order_id
    ).is_("overall_status", "null").execute()


def mark_processed(client, row: dict) -> None:
    """
    Idempotently record a processed email. Uses upsert on the unique
    message_id so a duplicate-delivered message (Yahoo re-sends the same
    Message-ID) or a full reprocess never crashes the listener with a 23505
    unique-violation on the audit-log insert. Without this, a cold rebuild
    aborts partway through and leaves an inconsistent, half-reprocessed state.
    """
    try:
        client.table("processed_emails").upsert(row, on_conflict="message_id").execute()
    except Exception as e:
        # Log but don't crash — a missing audit record means is_already_processed()
        # returns False next poll, so the email gets reprocessed. We prefer that
        # over letting a transient DB error abort real processing.
        print(f"  ⚠️ [warn] processed_emails write failed ({e}); email may be reprocessed")


def parse_email_date(msg) -> str:
    """Real send date (ISO) from the Date header, falling back to now."""
    date_header = msg.get("Date")
    if date_header:
        try:
            return parsedate_to_datetime(date_header).isoformat()
        except Exception:
            pass
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------
# UID cursor (high-water mark) per (account, folder).
# ------------------------------------------------------------
def get_cursor(account: str, folder: str) -> dict:
    """Return {'last_uid': int, 'uidvalidity': int|None} for a folder."""
    client = get_client()
    res = (
        client.table("sync_state")
        .select("last_uid, uidvalidity")
        .eq("account", account)
        .eq("folder", folder)
        .execute()
    )
    if res.data:
        row = res.data[0]
        return {
            "last_uid": int(row.get("last_uid") or 0),
            "uidvalidity": row.get("uidvalidity"),
        }
    return {"last_uid": 0, "uidvalidity": None}


def set_cursor(account: str, folder: str, last_uid: int, uidvalidity: int | None) -> None:
    """Upsert the cursor for a folder."""
    client = get_client()
    client.table("sync_state").upsert(
        {
            "account": account,
            "folder": folder,
            "last_uid": int(last_uid),
            "uidvalidity": int(uidvalidity) if uidvalidity is not None else None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="account,folder",
    ).execute()


# Earliest email date the system cares about. POs from 2024 / early this year
# are dead — their Chevron notifications will never exist in a fresh database,
# so parking them is pointless clutter and makes the first-run backfill crawl
# through years of history. Default: start of 2026. Override via env
# BACKFILL_SINCE=YYYY-MM-DD.
def _backfill_since() -> datetime:
    import os
    raw = os.environ.get("BACKFILL_SINCE", "2026-01-01")
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return datetime(2026, 1, 1).date()


def _find_start_uid_by_date(imap, all_uids: list, target_date) -> int:
    """
    Binary search over `all_uids` (sorted ascending) to find the first UID
    whose Date header is on or after `target_date`. Returns the UID value,
    or 0 if the inbox predates target_date entirely.

    Only ~log2(N) IMAP fetches regardless of inbox size — safe for 500k UIDs.
    """
    from email.utils import parsedate_to_datetime as _parse

    def _fetch_date(uid):
        try:
            data = imap.fetch([uid], [b"BODY[HEADER.FIELDS (DATE)]"])
            raw = data.get(uid, {}).get(b"BODY[HEADER.FIELDS (DATE)]", b"")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            m = re.search(r"Date:\s*(.+)", raw, re.IGNORECASE)
            if m:
                return _parse(m.group(1).strip()).date()
        except Exception:
            pass
        return None

    lo, hi = 0, len(all_uids) - 1
    result_idx = len(all_uids)  # default: no UID found on/after target

    while lo <= hi:
        mid = (lo + hi) // 2
        d = _fetch_date(all_uids[mid])
        if d is None:
            lo = mid + 1
            continue
        if d >= target_date:
            result_idx = mid
            hi = mid - 1
        else:
            lo = mid + 1

    return all_uids[result_idx] if result_idx < len(all_uids) else 0


def new_uids_since_cursor(
    imap, account: str, folder: str, search_terms: list, use_since: bool = True
) -> tuple[list[int], int]:
    """
    Return the sorted list of UIDs matching `search_terms` that are newer
    than our stored cursor for (account, folder), handling UIDVALIDITY.

    A server-side SINCE filter bounds the search to recent mail only, so the
    first-run backfill doesn't crawl years of dead history. After the cursor
    catches up, SINCE is harmless (cursor does the real work).

    Pass use_since=False to skip the SINCE filter (e.g. Yahoo, whose IMAP
    SINCE implementation only returns the most recent ~1000 UIDs regardless
    of the requested date, silently missing older messages).

    If the server's UIDVALIDITY changed (rare — folder rebuilt), the old
    cursor is meaningless, so we reset to 0 and re-evaluate everything.

    Fresh-start seeding: when use_since=False and last_uid==0 (empty
    sync_state), perform a binary search to find the first UID on/after
    BACKFILL_SINCE, then seed the cursor there before scanning. This makes
    a full database wipe + restart safe and repeatable from January.
    """
    folder_status = imap.folder_status(folder, [b"UIDVALIDITY"])
    uidvalidity = int(folder_status[b"UIDVALIDITY"])

    cursor = get_cursor(account, folder)
    last_uid = cursor["last_uid"]
    if cursor["uidvalidity"] is not None and int(cursor["uidvalidity"]) != uidvalidity:
        # Folder was rebuilt server-side; previous UIDs no longer valid.
        last_uid = 0

    if use_since:
        terms = list(search_terms) + ["SINCE", _backfill_since()]
    else:
        terms = list(search_terms)
    all_ids = imap.search(terms)

    # Fresh Yahoo cursor — binary-search to seed at BACKFILL_SINCE rather than
    # scanning the entire inbox from UID 1.
    if not use_since and last_uid == 0 and all_ids:
        since = _backfill_since()
        print(f"  📅 Fresh Yahoo cursor — binary-searching {len(all_ids):,} UIDs for {since} ...")
        all_sorted = sorted(all_ids)
        start_uid = _find_start_uid_by_date(imap, all_sorted, since)
        if start_uid > 0:
            seed_uid = start_uid - 1
            print(f"  ✅ Seeding cursor at UID {seed_uid} (first {since}+ email is UID {start_uid})")
            set_cursor(account, folder, seed_uid, uidvalidity)
            last_uid = seed_uid
        else:
            print(f"  ⚠️  Could not find a UID on/after {since}; scanning full inbox")

    new_ids = sorted(uid for uid in all_ids if uid > last_uid)
    return new_ids, uidvalidity


# ------------------------------------------------------------
# Parking — store an email whose order doesn't exist yet.
# ------------------------------------------------------------
def park_email(
    *,
    message_id: str,
    kind: str,
    po_number: str,
    sender: str = None,
    subject: str = None,
    email_date: str = None,
    pdf_path: str = None,
    body_text: str = None,
    needs_claude: bool = False,
) -> None:
    """Upsert a parked email (no-op if already parked for this kind)."""
    client = get_client()
    client.table("parked_emails").upsert(
        {
            "message_id": message_id,
            "kind": kind,
            "po_number": po_number,
            "sender": sender,
            "subject": subject,
            "email_date": email_date,
            "pdf_path": pdf_path,
            "body_text": body_text,
            "needs_claude": needs_claude,
        },
        on_conflict="message_id,kind",
    ).execute()


def get_parked_for_po(po_number: str) -> list[dict]:
    client = get_client()
    res = (
        client.table("parked_emails")
        .select("*")
        .eq("po_number", po_number)
        .order("email_date", desc=False)
        .execute()
    )
    return res.data or []


def delete_parked(parked_id: str) -> None:
    client = get_client()
    client.table("parked_emails").delete().eq("id", parked_id).execute()


# ------------------------------------------------------------
# Reconciler — apply any parked emails for a PO once its order exists.
# Called right after an order is created (Stage 1) and can also be run
# standalone to sweep the whole parking lot.
# ------------------------------------------------------------
def reconcile_po(po_number: str) -> int:
    """
    Apply parked routing/reply emails for `po_number`. Returns the number
    of parked emails successfully applied (and removed from parking).

    This is Claude-free: warehouse_routing stamps the timestamps; if the
    routing email still needs Claude for the exact ack date, the order is
    flagged pending_ack_extraction for a later backfill pass.
    """
    client = get_client()
    order_res = (
        client.table("orders")
        .select("id, acknowledgment_status")
        .eq("buyer_po_number", po_number)
        .execute()
    )
    if not order_res.data:
        return 0  # order still doesn't exist; leave parked

    order = order_res.data[0]
    order_id = order["id"]
    applied = 0

    for parked in get_parked_for_po(po_number):
        kind = parked["kind"]

        if kind == "warehouse_routing":
            # Warehouse routing implies acknowledgment + sent-to-warehouse.
            update = {"sent_to_warehouse_at": parked.get("email_date")}
            if order.get("acknowledgment_status") != "acknowledged":
                update["acknowledgment_status"] = "acknowledged"
                update["acknowledged_by"] = "warehouse routing (auto-detected, reconciled)"
            if parked.get("needs_claude"):
                update["pending_ack_extraction"] = True
            if parked.get("body_text"):
                update["warehouse_routing_raw"] = parked["body_text"].strip()
            client.table("orders").update(update).eq("id", order_id).execute()
            advance_status(client, order_id, "awaiting_warehouse_stock_check")
            delete_parked(parked["id"])
            applied += 1

        elif kind == "warehouse_reply":
            # Interpret the reply body NOW (keyword-first, free) so the
            # order's stock_check_raw is populated on reconcile — not just
            # timestamped and left with pending_stock_extraction=True (the
            # old behaviour, which left stock_check_raw permanently null).
            # Lazy import avoids a circular import (that module imports sync).
            from warehouse_reply_parser import interpret_reply

            body = parked.get("body_text") or ""
            result, method = interpret_reply(body)
            availability = result.get("overall_availability")
            update = {"stock_check_completed_at": parked.get("email_date")}

            if availability == "followup":
                # A delivery request/reminder, not a stock check. Don't
                # overwrite status or stock_check_raw; just drop the parked row.
                delete_parked(parked["id"])
                applied += 1
                continue

            if method == "deferred":
                # Claude was needed but unavailable (e.g. no API credits) —
                # typically a partial/mixed reply. Still populate
                # stock_check_raw (never leave it null) with the raw body
                # preserved and a needs-review flag, and KEEP the parked reply
                # + pending flag so a later Claude pass can structure it.
                update["pending_stock_extraction"] = True
                update["stock_check_raw"] = {
                    "overall_availability": "unclear",
                    "needs_human_review": True,
                    "confidence": "low",
                    "summary": "Partial/complex warehouse reply — awaiting AI interpretation",
                    "raw_body": body.strip()[:1000],
                    "interpretation_method": "deferred",
                }
                client.table("orders").update(update).eq("id", order_id).execute()
                advance_status(client, order_id, "stock_check_needs_review")
                applied += 1
                continue

            # Interpreted successfully — fill stock_check_raw and clear the flag.
            update["stock_check_raw"] = result
            if availability == "fully_delivered":
                new_status = "delivered"
                if parked.get("email_date"):
                    update["delivered_at"] = parked["email_date"]
            elif result.get("needs_human_review") or result.get("confidence") == "low":
                new_status = "stock_check_needs_review"
            else:
                new_status = "stock_check_complete"
            update["pending_stock_extraction"] = False
            client.table("orders").update(update).eq("id", order_id).execute()
            advance_status(client, order_id, new_status)
            delete_parked(parked["id"])
            applied += 1

    return applied


def reconcile_all() -> int:
    """Sweep the entire parking lot (e.g. on startup). Returns total applied."""
    client = get_client()
    res = client.table("parked_emails").select("po_number").execute()
    pos = sorted({r["po_number"] for r in (res.data or [])})
    total = 0
    for po in pos:
        total += reconcile_po(po)
    return total


def reconcile_nlng_po(po_number: str) -> int:
    """Apply parked nlng_warehouse_routing / nlng_warehouse_reply emails for an
    NLNG order once it exists. Mirrors reconcile_po() for Chevron."""
    client = get_client()
    order_res = (
        client.table("nlng_orders")
        .select("id")
        .eq("po_number", po_number)
        .execute()
    )
    if not order_res.data:
        return 0
    order_id = order_res.data[0]["id"]
    applied = 0

    for parked in get_parked_for_po(po_number):
        kind = parked["kind"]

        if kind == "nlng_warehouse_routing":
            update: dict = {"sent_to_warehouse_at": parked.get("email_date")}
            if parked.get("body_text"):
                update["warehouse_routing_raw"] = parked["body_text"].strip()
            client.table("nlng_orders").update(update).eq("id", order_id).execute()
            nlng_advance_status(client, order_id)
            delete_parked(parked["id"])
            applied += 1

        elif kind == "nlng_warehouse_reply":
            from warehouse_reply_parser import interpret_reply  # noqa: PLC0415
            body = parked.get("body_text") or ""
            result, _ = interpret_reply(body)
            availability = result.get("overall_availability")
            if availability == "followup":
                delete_parked(parked["id"])
                applied += 1
                continue
            result["raw_body"] = body.strip()
            stamp_nlng_stock_check(order_id, parked.get("email_date"), result)
            if availability == "fully_delivered":
                stamp_nlng_delivered(order_id, parked.get("email_date"))
            delete_parked(parked["id"])
            applied += 1

    return applied

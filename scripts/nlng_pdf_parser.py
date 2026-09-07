"""
nlng_pdf_parser.py — Extract structured fields from an NLNG SAP purchase-order PDF.

NLNG PO PDFs arrive as .dat email attachments (the bytes are valid PDF).
This module accepts raw bytes or a file path and returns a dict of parsed fields
plus a list of line-item dicts.

Usage
-----
    from nlng_pdf_parser import parse_nlng_po_pdf

    with open("PO No. 4200083212.dat", "rb") as f:
        result = parse_nlng_po_pdf(f.read())

    print(result["po_number"])          # "4200083212"
    print(result["required_delivery_date"])  # "2025-08-27"
    print(result["line_items"])         # [{mesc_code, description, ...}, ...]
"""

from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Any

try:
    import pdfplumber
except ImportError:
    pdfplumber = None  # type: ignore


# ── date helpers ─────────────────────────────────────────────────────────────

_DATE_FORMATS = [
    "%d. %B %Y",   # "27. August 2025"
    "%d.%m.%Y",    # "27.08.2025"
    "%Y-%m-%d",    # ISO already
    "%d/%m/%Y",
]


def _parse_date(raw: str) -> str | None:
    """Return ISO YYYY-MM-DD or None."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


# ── amount helpers ────────────────────────────────────────────────────────────

def _parse_amount(raw: str) -> float | None:
    if not raw:
        return None
    cleaned = raw.replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


# ── main parser ──────────────────────────────────────────────────────────────

def parse_nlng_po_pdf(pdf_bytes: bytes) -> dict[str, Any]:
    """
    Parse an NLNG PO PDF from raw bytes.

    Returns a dict with keys:
        po_number, variation_number, document_date, required_delivery_date,
        delivery_terms, delivery_address, net_value, currency,
        contact_name, contact_email, line_items (list of dicts)

    All values are None if not found.  Never raises — returns partial results
    on parse failure so the caller can still create a record.
    """
    if pdfplumber is None:
        raise ImportError("pdfplumber is not installed. Run: pip install pdfplumber")

    result: dict[str, Any] = {
        "po_number": None,
        "variation_number": 0,
        "document_date": None,
        "required_delivery_date": None,
        "delivery_terms": None,
        "delivery_address": None,
        "net_value": None,
        "currency": "USD",
        "contact_name": None,
        "contact_email": None,
        "enquiry_number": None,
        "line_items": [],
    }

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            # Collect all text from all pages
            pages_text = []
            for page in pdf.pages:
                t = page.extract_text(x_tolerance=2, y_tolerance=2)
                if t:
                    pages_text.append(t)
            full_text = "\n".join(pages_text)

            # Only parse header fields from page 1
            page1_text = pages_text[0] if pages_text else ""

            _parse_header(page1_text, full_text, result)
            result["line_items"] = _parse_line_items(page1_text, full_text)

    except Exception as exc:
        result["_parse_error"] = str(exc)

    return result


def _parse_header(page1: str, full_text: str, out: dict) -> None:
    """Fill out[] in-place from page 1 text."""

    # PO Number — appears as "4200083212" prominently at top, and in footer
    # as "PO No. : 4200083212"
    m = re.search(r"PO\s*No\s*\.?\s*:?\s*(42\d{7,8})", page1, re.IGNORECASE)
    if not m:
        # fall back: any 9-10 digit number starting with 42 near top of page
        m = re.search(r"\b(42\d{7,8})\b", page1)
    if m:
        out["po_number"] = m.group(1).strip()

    # Variation No
    m = re.search(r"Variation\s*No[:\s]+(\d+)", page1, re.IGNORECASE)
    if m:
        try:
            out["variation_number"] = int(m.group(1))
        except ValueError:
            pass

    # Document Date — labeled "Document Date:" followed by a date on the same
    # or next line. Pattern: "27. June 2025"
    m = re.search(
        r"Document\s*Date[:\s]+(\d{1,2}[.\s]+\w+ \d{4})",
        page1,
        re.IGNORECASE,
    )
    if m:
        out["document_date"] = _parse_date(m.group(1).strip())

    # Delivery Date — "Delivery Date : 27. August 2025"
    m = re.search(
        r"Delivery\s*Date\s*:?\s*(\d{1,2}[.\s]+\w+\s+\d{4})",
        page1,
        re.IGNORECASE,
    )
    if m:
        out["required_delivery_date"] = _parse_date(m.group(1).strip())

    # Delivery Terms — "Delivery Terms : DDP NLNG CHO PHC WAREHOUSE"
    m = re.search(
        r"Delivery\s*Terms\s*:\s*(.+?)(?:\n|Delivery\s*Date|Shipping)",
        page1,
        re.IGNORECASE,
    )
    if m:
        out["delivery_terms"] = m.group(1).strip()

    # Delivery Address — block after "Delivery Address :"
    m = re.search(
        r"Delivery\s*Address\s*:\s*(.+?)(?=\n\n|\nINVOICING|\nCurrency)",
        page1,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        addr = re.sub(r"\s+", " ", m.group(1)).strip()
        out["delivery_address"] = addr

    # Net Value — "Net Value : 2,348.00"
    m = re.search(r"Net\s*Value\s*:?\s*([\d,]+\.?\d*)", page1, re.IGNORECASE)
    if m:
        out["net_value"] = _parse_amount(m.group(1))

    # Currency
    m = re.search(r"Currency\s*Code\s*:\s*([A-Z]{3})", page1, re.IGNORECASE)
    if m:
        out["currency"] = m.group(1).upper()

    # Enquiry No — layout: "Variation No: Document Date: Enquiry No:\n0 13. Feb 2026 0600025180"
    # The value row has: {variation} {date} {enquiry_no} — enquiry is the last token.
    m = re.search(
        r"Variation\s*No\s*:.*?Enquiry\s*No\s*:[ \t]*\r?\n[ \t]*\S+[ \t]+\d+\.[ \t]+\w+[ \t]+\d{4}[ \t]+(\S+)",
        page1, re.IGNORECASE,
    )
    if m:
        val = m.group(1).strip()
        if val and val != "0":
            out["enquiry_number"] = val

    # Contact — "... Queries To : Theodora OKONKWO\nWARRI THEODORA.OKONKWO@NLNG.COM"
    # Name is at end of the "Queries To :" line; email is anywhere in the following line.
    m = re.search(r"Queries\s*To\s*:\s*(.+)", page1, re.IGNORECASE)
    if m:
        out["contact_name"] = m.group(1).strip()
        # Email is in the text following the name line (within 200 chars)
        rest = page1[m.end():]
        em = re.search(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", rest[:200], re.IGNORECASE)
        if em:
            out["contact_email"] = em.group(0).strip().upper()
    else:
        # Fallback: bare NLNG email anywhere on page
        m = re.search(r"([A-Z0-9._%+\-]+@NLNG\.COM)", page1, re.IGNORECASE)
        if m:
            out["contact_email"] = m.group(1).strip().upper()


# ── line-item parser ──────────────────────────────────────────────────────────

# Two PDF table formats exist:
#
# Old single-line format (MESC is pure digits):
#   1 8541460821 GASKET:SPW;FLEXITALL IC 350 MM LB  50.00 PC 46.96  2,348.00
#
# New multi-line NV-prefix format:
#   1 NV8541363821 GASKET:SPIRAL
#   WOUND;TBA DN350 TBA
#   TBA LB
#   2.00 PC 59.61  119.22

# Matches the start of an item row: item_no, MESC code (optional NV prefix), rest of line
_ITEM_START_RE = re.compile(r"^(\d{1,3})\s+((?:NV)?\d{7,10})\s+(.*)")

# Old format: the rest after the MESC code holds description + qty + uom + price [discount] net
# Optional discount column may have a trailing minus sign (e.g. "16,192.44-")
_REST_QTY_RE = re.compile(
    r"^(.+?)\s+"
    r"(\d[\d,]*\.?\d*)\s+"
    r"([A-Z]{2,4})\s+"
    r"(\d[\d,]*\.?\d*)\s+"
    r"(?:[\d,]*\.?\d*-?\s+)?"
    r"([\d,]+\.?\d*)\s*$"
)

# New multi-line format: qty/uom/price appear on their own line below the description
# Optional discount column may have a trailing minus sign (e.g. "16,192.44-")
_QTY_ONLY_RE = re.compile(
    r"^\s*(\d[\d,]*\.?\d*)\s+"
    r"([A-Z]{2,4})\s+"
    r"(\d[\d,]*\.?\d*)\s+"
    r"(?:[\d,]*\.?\d*-?\s+)?"
    r"([\d,]+\.?\d*)\s*$"
)

# Int. Article No. line that follows an item row
_INT_ARTICLE_RE = re.compile(
    r"Int\.\s*Article\s*No\.\s+(\d+)",
    re.IGNORECASE,
)

# "Delivery Date: 27. August 2025" that appears inline under an item
_ITEM_DELIVERY_RE = re.compile(
    r"Delivery\s*Date:\s*(\d{1,2}[.\s]+\w+\s+\d{4})",
    re.IGNORECASE,
)


def _scan_items_in_text(text: str) -> list[dict]:
    """
    Parse line items from extracted PDF text.
    Handles both old single-line format and new NV-prefix multi-line format
    where description and qty/price appear on separate lines.
    """
    items: list[dict] = []
    current: dict | None = None
    desc_parts: list[str] = []

    def _finalise_current() -> None:
        nonlocal current, desc_parts
        # Incomplete item (no qty line ever found) — discard it
        current = None
        desc_parts = []

    for line_raw in text.split("\n"):
        line = line_raw.strip()
        if not line:
            continue

        # Check for a new item start line
        m_start = _ITEM_START_RE.match(line)
        if m_start:
            _finalise_current()
            rest = m_start.group(3).strip()

            # Try old single-line format first: description + qty all on this line
            m_rest = _REST_QTY_RE.match(rest)
            if m_rest:
                items.append({
                    "item_no":        int(m_start.group(1)),
                    "mesc_code":      m_start.group(2),
                    "description":    m_rest.group(1).strip(),
                    "quantity":       _parse_amount(m_rest.group(2)),
                    "uom":            m_rest.group(3),
                    "unit_price":     _parse_amount(m_rest.group(4)),
                    "net_amount":     _parse_amount(m_rest.group(5)),
                    "int_article_no": None,
                    "delivery_date":  None,
                })
            else:
                # Multi-line format: description continues on following lines
                current = {
                    "item_no":        int(m_start.group(1)),
                    "mesc_code":      m_start.group(2),
                    "description":    "",
                    "quantity":       None,
                    "uom":            None,
                    "unit_price":     None,
                    "net_amount":     None,
                    "int_article_no": None,
                    "delivery_date":  None,
                }
                desc_parts = [rest] if rest else []
            continue

        # Int. Article No. — associate with last saved item or the in-progress one
        m_art = _INT_ARTICLE_RE.search(line)
        if m_art:
            target = items[-1] if (current is None and items) else current
            if target:
                target["int_article_no"] = m_art.group(1)
            continue

        # Per-item delivery date
        m_del = _ITEM_DELIVERY_RE.search(line)
        if m_del:
            target = items[-1] if (current is None and items) else current
            if target:
                target["delivery_date"] = _parse_date(m_del.group(1).strip())
            continue

        if current is None:
            continue

        # Qty/price on its own line (multi-line format)
        m_qty = _QTY_ONLY_RE.match(line)
        if m_qty and current["quantity"] is None:
            current["quantity"]    = _parse_amount(m_qty.group(1))
            current["uom"]         = m_qty.group(2)
            current["unit_price"]  = _parse_amount(m_qty.group(3))
            current["net_amount"]  = _parse_amount(m_qty.group(4))
            current["description"] = " ".join(p for p in desc_parts if p).strip()
            items.append(current)
            current = None
            desc_parts = []
            continue

        # Description continuation line
        desc_parts.append(line)

    return items


def _parse_line_items(page1: str, full_text: str) -> list[dict]:
    """
    Extract line items from the full document text.
    Always scans full_text so items that start on page 2+ are never missed.
    Falls back to a loose pattern if the primary scan finds nothing.
    """
    items = _scan_items_in_text(full_text)
    if not items:
        items = _fallback_parse_items(full_text)
    return items


def _fallback_parse_items(text: str) -> list[dict]:
    """
    Loose fallback: find any MESC code (NV-prefixed or pure 10-digit) and
    capture surrounding data. Skips PO numbers (start 42) and enquiry numbers
    (start 06) which are never MESC codes.
    """
    items = []
    for m in re.finditer(
        r"\b(NV\d{7,10}|\d{10})\b\s+(.{5,80}?)\s+(\d[\d,]*\.?\d+)", text
    ):
        mesc = m.group(1)
        if not mesc.startswith("NV") and (
            mesc.startswith("42") or mesc.startswith("06")
        ):
            continue
        items.append({
            "item_no":        len(items) + 1,
            "mesc_code":      mesc,
            "description":    m.group(2).strip(),
            "quantity":       None,
            "uom":            None,
            "unit_price":     None,
            "net_amount":     _parse_amount(m.group(3)),
            "int_article_no": None,
            "delivery_date":  None,
        })
    return items

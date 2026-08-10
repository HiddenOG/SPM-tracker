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

# Header row looks like:
#   Item MESC Code Description Part Number Quantity UoM Unit Price Discount Net Amount
# Each data row:
#   1 8541460821 GASKET:SPW;FLEXITALL IC 350 MM LB  50.00 PC 46.96  2,348.00

_LINE_ITEM_RE = re.compile(
    r"^(\d+)\s+"                            # item_no
    r"(\d{10})\s+"                          # mesc_code (10 digits)
    r"(.+?)\s+"                             # description (greedy then back off)
    r"(\d[\d,]*\.?\d*)\s+"                  # quantity
    r"([A-Z]{2,4})\s+"                      # uom
    r"(\d[\d,]*\.?\d*)\s+"                  # unit_price
    r"(?:[\d,]*\.?\d*\s+)?"                 # optional discount column
    r"([\d,]+\.?\d*)\s*$",                  # net_amount
    re.MULTILINE,
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


def _parse_line_items(page1: str, full_text: str) -> list[dict]:
    """
    Extract line items from the table on page 1.

    NLNG PDFs render each item row as a single text line (pdfplumber merges
    the columns). We match with _LINE_ITEM_RE then scan the following lines
    for Int. Article No. and per-item Delivery Date.
    """
    items: list[dict] = []

    lines = page1.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = _LINE_ITEM_RE.match(line)
        if m:
            item: dict[str, Any] = {
                "item_no": int(m.group(1)),
                "mesc_code": m.group(2),
                "description": m.group(3).strip(),
                "quantity": _parse_amount(m.group(4)),
                "uom": m.group(5),
                "unit_price": _parse_amount(m.group(6)),
                "net_amount": _parse_amount(m.group(7)),
                "int_article_no": None,
                "delivery_date": None,
            }
            # Look ahead up to 5 lines for Int. Article No. and Delivery Date
            for j in range(i + 1, min(i + 6, len(lines))):
                next_line = lines[j].strip()
                if not next_line:
                    continue
                am = _INT_ARTICLE_RE.search(next_line)
                if am:
                    item["int_article_no"] = am.group(1)
                dm = _ITEM_DELIVERY_RE.search(next_line)
                if dm:
                    item["delivery_date"] = _parse_date(dm.group(1).strip())
                # Stop if we hit the next item row or a section header
                if _LINE_ITEM_RE.match(next_line):
                    break
            items.append(item)
        i += 1

    # Fallback: if the line-item regex found nothing (PDF layout varies),
    # try a looser pattern that just grabs MESC + description + amounts
    if not items:
        items = _fallback_parse_items(page1)

    return items


def _fallback_parse_items(text: str) -> list[dict]:
    """
    Loose fallback: find any 10-digit MESC code and try to capture surrounding
    data. Returns incomplete items rather than nothing.
    """
    items = []
    for m in re.finditer(r"\b(\d{10})\b\s+(.{5,80}?)\s+(\d[\d,]*\.?\d+)", text):
        items.append({
            "item_no": len(items) + 1,
            "mesc_code": m.group(1),
            "description": m.group(2).strip(),
            "quantity": None,
            "uom": None,
            "unit_price": None,
            "net_amount": _parse_amount(m.group(3)),
            "int_article_no": None,
            "delivery_date": None,
        })
    return items

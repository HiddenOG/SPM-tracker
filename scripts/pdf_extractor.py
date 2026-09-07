"""
pdf_extractor.py — Stage 2: Read attached PO PDFs and extract structured data.

Runs continuously in the background, checking every CHECK_INTERVAL_SECONDS
for new orders that have a PDF saved but haven't been extracted yet.
Processes them automatically as they arrive from Stage 1.

Strategy:
  1. Try pdfplumber first (free, fast, regex-based)
  2. Fall back to Claude only if pdfplumber fails or returns low confidence

Run this with:  python scripts/pdf_extractor.py
Press Ctrl+C to stop.
"""

import gc
import os
import json
import base64
import re
import time
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
import anthropic
import pdfplumber

from db import get_client, reset_client


def _tesseract_cmd() -> str:
    """
    Locate the tesseract binary.

    PATH is checked first because the deploy runs Linux, where the Windows
    path below does not exist. These call sites previously hardcoded the
    Windows path with no lookup, so every OCR fallback failed there — silently,
    since the callers swallow the exception — while working fine locally.
    The sibling ack_pdf_extractor.py already did this correctly; this file
    was missed.
    """
    import shutil
    return shutil.which("tesseract") or r"C:\Program Files\Tesseract-OCR\tesseract.exe"

load_dotenv()

_anthropic = None
def _get_claude():
    global _anthropic
    if _anthropic is None:
        _anthropic = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _anthropic

CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", 120))

# ─────────────────────────────────────────────
# PDFPLUMBER EXTRACTION (primary — free)
# ─────────────────────────────────────────────

def parse_gep_date(date_str: str) -> str | None:
    """Convert M/D/YYYY to ISO YYYY-MM-DD."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def extract_rdd_from_words(pdf_path: str) -> str | None:
    """
    Fallback RDD extraction for POs whose enormous line-item descriptions
    stop pdfplumber from forming a table at all (common for valve repair
    kits). The 'Required Delivery Date' value survives only as column-wrapped
    words, so we:
      1. Locate the RDD column's x-band from the header ('Required' →
         'Requisition'), reading it from whichever page carries the header
         (the header and the data row can be on different pages).
      2. Collect the word fragments inside that x-band across ALL pages and
         join them, which reassembles wrapped dates like '6/29/2' + '026'.
      3. Return the earliest parseable date (the binding RDD).
    Returns None if no date can be recovered.
    """
    date_re = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")
    try:
        with pdfplumber.open(pdf_path) as pdf:
            x0 = x1 = None
            for page in pdf.pages:
                words = page.extract_words()
                req = [w for w in words if w["text"] == "Required"]
                if req:
                    x0 = req[0]["x0"]
                    reqn = [w for w in words if w["text"] == "Requisition"]
                    x1 = reqn[0]["x0"] if reqn else x0 + 45
                    break
            if x0 is None:
                return None

            found = []
            for page in pdf.pages:
                words = page.extract_words()
                toks = [
                    w for w in page.extract_words()
                    if (x0 - 2) <= w["x0"] < x1
                    and w["text"] not in ("Required", "Delivery", "Date")
                ]
                toks.sort(key=lambda w: (round(w["top"]), w["x0"]))
                joined = "".join(re.sub(r"\s", "", w["text"]) for w in toks)
                for m in date_re.finditer(joined):
                    iso = parse_gep_date(m.group(0))
                    if iso:
                        found.append(iso)
            return min(found) if found else None
    except Exception:
        return None


def extract_promised_dates_from_words(pdf_path: str) -> list[str]:
    """
    Word-position fallback to collect Promised Dates when pdfplumber's table
    extraction misses the column (e.g. very long descriptions split rows across
    sub-rows, pushing the Promised Date into a continuation row with no
    line_no). Uses the same x-band approach as extract_rdd_from_words.
    Returns a list of ISO date strings, one per date found in column order.
    """
    date_re = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")
    try:
        with pdfplumber.open(pdf_path) as pdf:
            x0 = x1 = None
            for page in pdf.pages:
                words = page.extract_words()
                prom = [w for w in words if w["text"] == "Promised"]
                if prom:
                    x0 = prom[0]["x0"]
                    inco = [w for w in words if w["text"] == "IncoTerm"]
                    x1 = inco[0]["x0"] if inco else x0 + 55
                    break
            if x0 is None:
                return []

            found = []
            for page in pdf.pages:
                toks = [
                    w for w in page.extract_words()
                    if (x0 - 2) <= w["x0"] < x1
                    and w["text"] not in ("Promised", "Date")
                ]
                toks.sort(key=lambda w: (round(w["top"]), w["x0"]))
                joined = "".join(re.sub(r"\s", "", w["text"]) for w in toks)
                for m in date_re.finditer(joined):
                    iso = parse_gep_date(m.group(0))
                    if iso:
                        found.append(iso)
            return found
    except Exception:
        return []


def extract_field(text: str, label: str) -> str | None:
    """
    Extract value after a label, handling pdfplumber's space-stripping.
    Tries both spaced and compressed versions of the label.
    """
    # Compressed version: remove spaces from label
    compressed = label.replace(" ", "")
    # Try compressed label first (pdfplumber output), then normal
    for pattern_label in [compressed, label]:
        pattern = rf"{re.escape(pattern_label)}\s*:?\s*(.+)"
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = match.group(1).strip()
            # Skip if value looks like the next field label (compressed)
            if value and not value.startswith("Net") and len(value) > 0:
                return value
    return None


def classify_product_line(description: str) -> str:
    desc = description.upper()
    if any(w in desc for w in ["GASKET", "SPIRAL WOUND", "RTJ", "SPW", "FLEXITALLIC", "SEALING"]):
        return "gasket"
    if any(w in desc for w in ["VALVE", "GATE", "GLOBE", "CHECK", "BALL", "ACTUATOR"]):
        return "valve"
    if any(w in desc for w in ["LNG", "LIQUEFIED"]):
        return "lng"
    if any(w in desc for w in ["SPACER", "BLIND SPACER", "RING SPACER"]):
        return "spacer"
    if any(w in desc for w in ["PIPE", "FITTING", "FLANGE", "ELBOW"]):
        return "piping"
    if any(w in desc for w in ["CHEMICAL", "FLUID", "OIL", "REFRACTORY", "DRUM"]):
        return "consumable"
    return "other"

def _ocr_words_on_page(pdf_path: str, page_idx: int, scale: float = 2.0) -> list[dict]:
    """
    Render a PDF page via PyMuPDF and return Tesseract word tokens with position.
    Each token: {text, x0, y0, x1, y1}.
    """
    try:
        import fitz
        from PIL import Image
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = _tesseract_cmd()

        doc = fitz.open(pdf_path)
        if page_idx >= len(doc):
            doc.close()
            return []
        page = doc[page_idx]
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        doc.close()

        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        tokens = []
        for i in range(len(data["text"])):
            word = data["text"][i].strip()
            if not word or int(data["conf"][i]) < 20:
                continue
            tokens.append({
                "text":  word,
                "x0":    data["left"][i],
                "y0":    data["top"][i],
                "x1":    data["left"][i] + data["width"][i],
                "y1":    data["top"][i] + data["height"][i],
            })
        return tokens
    except Exception:
        return []


def _ocr_kv_from_page(pdf_path: str, page_idx: int, split_x: int,
                      scale: float = 2.0) -> dict[str, str]:
    """
    OCR a page and build label→value map by splitting each row at split_x.
    Words at x1 <= split_x are labels; words at x0 > split_x are values.
    Rows are grouped by y-proximity (within 12 px at 2x scale).
    """
    tokens = _ocr_words_on_page(pdf_path, page_idx, scale)
    if not tokens:
        return {}

    # Group tokens into rows by y-centre proximity
    rows: list[dict] = []
    for tok in sorted(tokens, key=lambda t: t["y0"]):
        y_c = (tok["y0"] + tok["y1"]) / 2
        placed = False
        for row in rows:
            if abs(y_c - row["y_c"]) < 14:
                row["tokens"].append(tok)
                row["y_c"] = (row["y_c"] * row["count"] + y_c) / (row["count"] + 1)
                row["count"] += 1
                placed = True
                break
        if not placed:
            rows.append({"y_c": y_c, "count": 1, "tokens": [tok]})

    kv: dict[str, str] = {}
    for row in rows:
        left  = sorted([t for t in row["tokens"] if t["x1"] <= split_x], key=lambda t: t["x0"])
        right = sorted([t for t in row["tokens"] if t["x0"] > split_x],  key=lambda t: t["x0"])
        if not left or not right:
            continue
        label = " ".join(t["text"] for t in left).rstrip(":").strip()
        value = " ".join(t["text"] for t in right).strip()
        if label and value:
            kv[label] = value
            # Also store space-free version for looser matching
            kv[label.replace(" ", "")] = value
    return kv


def _ocr_find(kv: dict, *keys: str) -> str | None:
    """Case-insensitive substring match against kv labels."""
    for k in keys:
        for label, value in kv.items():
            if k.lower() in label.lower():
                return value
    return None


def extract_from_ocr(pdf_path: str) -> dict:
    """
    Full extraction for Chrome-printed HTML PDFs where pdfplumber/PyMuPDF
    return zero words due to unreadable CID font encoding.
    Uses Tesseract OCR with word-position column matching.
    Called when the first page has zero pdfplumber words.
    """
    print(f"   🔎 pdfplumber: no extractable text — switching to OCR fallback")
    result: dict = {}
    date_re = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")

    # ── Page 1: header table ──────────────────────────────────────
    # Labels are in the left column (x1 ≤ 360), values in the right (x0 > 360)
    kv1 = _ocr_kv_from_page(pdf_path, 0, split_x=360)

    result["payment_terms"]  = _ocr_find(kv1, "Payment Terms")
    result["po_destination"] = _ocr_find(kv1, "PO Destination", "Destination")
    result["transportation"] = _ocr_find(kv1, "Transportation")

    sub_raw = _ocr_find(kv1, "Order Submitted", "Submitted on")
    if sub_raw:
        m = date_re.search(sub_raw)
        result["order_submitted_on"] = parse_gep_date(m.group(0)) if m else None

    ack_raw = _ocr_find(kv1, "Supplier Acknowledged", "Acknowledged on")
    if ack_raw:
        m = date_re.search(ack_raw)
        result["supplier_acknowledged_on"] = parse_gep_date(m.group(0)) if m else None

    total_raw = _ocr_find(kv1, "Net Total")
    if total_raw:
        m = re.search(r"([\d,]+\.?\d*)", total_raw)
        if m:
            try:
                result["net_total"] = float(m.group(1).replace(",", ""))
            except ValueError:
                pass
    result["currency"] = "NGN" if (total_raw and "NGN" in total_raw.upper()) else "USD"

    # ── Page 6: purchaser info + material items header ────────────
    # Labels at x1 ≤ 490; values at x0 > 490
    kv6 = _ocr_kv_from_page(pdf_path, 5, split_x=490)

    requestor_raw = _ocr_find(kv6, "Requestor Name", "Requestor")
    if requestor_raw:
        parts = [p.strip() for p in requestor_raw.split(",")]
        result["requestor_name"]  = parts[0] if parts else None
        result["requestor_email"] = parts[1] if len(parts) > 1 else None

    result["ship_to"] = _ocr_find(kv6, "Ship To")

    # ── Buyer Contact Details (last page) ─────────────────────────
    # "Buyer Contact Details:Name Telephone Number:(email)" appears in the
    # Additional Terms section. Use image_to_string on the last page — the
    # line is long enough that image_to_string reassembles it correctly.
    try:
        import fitz
        from PIL import Image
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = _tesseract_cmd()
        _doc = fitz.open(pdf_path)
        _last = _doc[len(_doc) - 1]
        _pix  = _last.get_pixmap(matrix=fitz.Matrix(2, 2))
        _img  = Image.frombytes("RGB", [_pix.width, _pix.height], _pix.samples)
        _doc.close()
        _last_txt = pytesseract.image_to_string(_img)
        _bm = re.search(
            r"Buyer\s+Contact\s+Details\s*:?\s*(.+?)\s+Telephone\s+Number",
            _last_txt, re.IGNORECASE,
        )
        if _bm:
            result["buyer_name"] = _bm.group(1).strip()
        _bem = re.search(
            r"Telephone\s+Number[^(]*\(\s*([^\s@)]+@[^\s)]+)\s*\)",
            _last_txt, re.IGNORECASE,
        )
        if _bem:
            result["buyer_email"] = _bem.group(1).strip().replace(" ", "")
    except Exception:
        pass

    # ── Page 7: PO Text description ───────────────────────────────
    # "PO Text" label at x≈87-169; description is to the right at x > 400.
    # Stop at the "QA Codes" label so we don't bleed into the next section.
    toks7 = _ocr_words_on_page(pdf_path, 6)
    po_text_y = None
    qa_codes_y = None
    for tok in toks7:
        if tok["text"] == "Text" and tok["x0"] < 250 and po_text_y is None:
            po_text_y = tok["y0"]
        if tok["text"] == "QA" and tok["x0"] < 200:
            qa_codes_y = tok["y0"]

    if po_text_y is not None:
        desc_cutoff = qa_codes_y if qa_codes_y else po_text_y + 350
        desc_toks = sorted(
            [t for t in toks7 if t["x0"] > 400 and t["y0"] >= po_text_y - 5 and t["y0"] < desc_cutoff],
            key=lambda t: (round(t["y0"] / 20), t["x0"]),
        )
        if desc_toks:
            lines: list[str] = []
            cur_line: list[str] = []
            cur_y = desc_toks[0]["y0"]
            for t in desc_toks:
                if abs(t["y0"] - cur_y) > 18:
                    lines.append(" ".join(cur_line))
                    cur_line = [t["text"]]
                    cur_y = t["y0"]
                else:
                    cur_line.append(t["text"])
            if cur_line:
                lines.append(" ".join(cur_line))
            result["description_summary"] = " ".join(lines).strip()[:500]

    result["product_line"] = classify_product_line(result.get("description_summary") or "")

    # ── Line items: date extraction using column x-bands ─────────
    # GEP tables have narrow date columns. Tesseract often splits "3/20/2026"
    # into "3/20/202" (one sub-row) and "6" (next sub-row). To reassemble:
    #   1. Locate "Required" and "Promised" column header x-positions.
    #   2. Collect every token in that x-band below the header.
    #   3. Concatenate without spaces (no word boundary between fragments).
    #   4. Run the date regex on the concatenated string.
    toks6_all = _ocr_words_on_page(pdf_path, 5, scale=3.0)

    req_x0 = req_x1 = prom_x0 = prom_x1 = hdr_y = None
    for tok in toks6_all:
        if tok["text"] == "Required" and tok["x0"] > 400:
            req_x0 = tok["x0"];  hdr_y = tok["y0"]
        if tok["text"] == "Requisition" and tok["x0"] > 400:
            req_x1 = tok["x0"]
        if tok["text"] == "Promised" and tok["x0"] > 400:
            prom_x0 = tok["x0"]
        if tok["text"] == "IncoTerm" and tok["x0"] > 400:
            prom_x1 = tok["x0"]

    def _col_date(toks, x0, x1, y_min):
        """Concatenate tokens in an x-band below y_min, search for M/D/YYYY."""
        if x0 is None or y_min is None:
            return None
        col = sorted(
            [t for t in toks if x0 - 5 <= t["x0"] < (x1 or x0 + 200) and t["y0"] > y_min],
            key=lambda t: (t["y0"], t["x0"]),
        )
        joined = "".join(t["text"] for t in col)
        m = date_re.search(joined)
        return parse_gep_date(m.group(0)) if m else None

    rdd      = _col_date(toks6_all, req_x0,  req_x1,  hdr_y)
    promised = _col_date(toks6_all, prom_x0, prom_x1, hdr_y) or rdd

    result["required_delivery_date"] = rdd
    result["po_promised_date"]       = promised

    if result.get("description_summary") or rdd:
        result["line_items"] = [{
            "line_no":               "1",
            "description":           result.get("description_summary"),
            "item_number":           None,
            "quantity":              None,
            "required_delivery_date": rdd,
            "promised_date":         promised,
        }]
    else:
        result["line_items"] = []

    result["extraction_method"] = "ocr_fallback"
    result["confidence"] = "high" if result.get("net_total") and result.get("payment_terms") else "low"
    return result


def extract_pdf_with_pdfplumber(pdf_path: str) -> dict:
    """
    Extract fields from a GEP PO PDF using pdfplumber's table extraction.
    Tables give clean cells; we just strip intra-cell newlines.
    Returns dict with same keys as Claude extraction.
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            # Early-exit: Chrome-printed HTML PDFs embed fonts using CID encoding
            # that pdfplumber cannot decode — extract_words() returns []. Detect
            # this on the first page and switch to the OCR-based path immediately.
            if pdf.pages and not pdf.pages[0].extract_words():
                return extract_from_ocr(pdf_path)

            text = ""
            tables = []
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                text += page_text + "\n"
                tables.extend(page.extract_tables())
    except Exception as e:
        return {"error": str(e)}

    if not tables:
        return {"error": "no tables extracted"}

    result = {}
    missing = []

    def clean(cell) -> str | None:
        """Strip intra-cell newlines and surrounding whitespace."""
        if cell is None:
            return None
        return cell.replace("\n", "").strip() or None

    # ── Build a label→value map from the key/value tables ─────────
    # Tables 0-3 and 5 are two-column label/value tables
    kv = {}
    for table in tables:
        for row in table:
            if len(row) >= 2 and row[0]:
                label = clean(row[0])
                value = clean(row[1])
                if label:
                    kv[label.rstrip(":")] = value
                    # Also store a space-stripped version so HTML-to-PDF and
                    # native GEP PDFs both hit the same lookup key.
                    kv[label.rstrip(":").replace(" ", "")] = value

    # ── Core header fields ────────────────────────────────────────
    # Try compressed key (native GEP PDF) then spaced key (ePurchase HTML-to-PDF)
    # then fall back to regex on raw text.
    def _find_ack_date(kv: dict, text: str) -> str | None:
        raw = kv.get("SupplierAcknowledgedon") or kv.get("Supplier Acknowledged on")
        if raw:
            return parse_gep_date(raw)
        m = re.search(r"Supplier\s+Acknowledged\s+on:?\s*(\d{1,2}/\d{1,2}/\d{4})", text, re.IGNORECASE)
        return parse_gep_date(m.group(1)) if m else None

    result["supplier_acknowledged_on"] = _find_ack_date(kv, text)
    result["order_submitted_on"] = parse_gep_date(
        kv.get("OrderSubmittedon") or kv.get("Order Submitted on")
    )

    result["payment_terms"] = kv.get("PaymentTerms")
    if not result["payment_terms"]:
        missing.append("payment_terms")

    result["po_destination"] = kv.get("PODestination")
    if not result["po_destination"]:
        missing.append("po_destination")

    result["transportation"] = kv.get("Transportation")

    # ── Requestor ────────────────────────────────────────────────
    requestor_raw = kv.get("RequestorName/Email/Phonenumber")
    if requestor_raw:
        parts = [p.strip() for p in requestor_raw.split(",")]
        result["requestor_name"] = parts[0] if len(parts) > 0 else None
        result["requestor_email"] = parts[1] if len(parts) > 1 else None
    else:
        result["requestor_name"] = None
        result["requestor_email"] = None
        missing.append("requestor")

    # ── Raw-text fallbacks for HTML-to-PDF format ─────────────────
    # Browser-printed GEP PDFs render table borders as hairlines that
    # pdfplumber doesn't detect, so kv comes back empty. page.extract_text()
    # still captures the text reliably — use regex to fill any gaps.
    if not result.get("payment_terms"):
        m = re.search(
            r"Payment\s*Terms?\s*:?\s*(.+?)(?:\n|Supplier\s+Acknowledged|$)",
            text, re.IGNORECASE,
        )
        if m:
            result["payment_terms"] = m.group(1).strip()
            if "payment_terms" in missing:
                missing.remove("payment_terms")

    if not result.get("po_destination"):
        m = re.search(
            r"PO\s*Destination\s*:?\s*(.+?)(?:\n|Transportation|$)",
            text, re.IGNORECASE,
        )
        if m:
            result["po_destination"] = m.group(1).strip()
            if "po_destination" in missing:
                missing.remove("po_destination")

    if not result.get("transportation"):
        m = re.search(r"\bTransportation\s*:?\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
        if m:
            result["transportation"] = m.group(1).strip()

    if not result.get("order_submitted_on"):
        m = re.search(
            r"Order\s*Submitted\s*[Oo]n\s*:?\s*(\d{1,2}/\d{1,2}/\d{4})",
            text, re.IGNORECASE,
        )
        if m:
            result["order_submitted_on"] = parse_gep_date(m.group(1))

    if not result.get("supplier_acknowledged_on"):
        m = re.search(
            r"Supplier\s*Acknowledged\s*[Oo]n\s*:?\s*(\d{1,2}/\d{1,2}/\d{4})",
            text, re.IGNORECASE,
        )
        if m:
            result["supplier_acknowledged_on"] = parse_gep_date(m.group(1))

    if not result.get("requestor_name"):
        # "Requestor Name / Email / Phone number: Name, email@, phone"
        m = re.search(
            r"Requestor\s*Name\s*/\s*Email\s*/\s*Phone\s*(?:number)?\s*:?\s*([^\n,]+)",
            text, re.IGNORECASE,
        )
        if m:
            result["requestor_name"] = m.group(1).strip()
            if "requestor" in missing:
                missing.remove("requestor")
        m2 = re.search(
            r"Requestor\s*Name\s*/\s*Email\s*/\s*Phone\s*(?:number)?\s*:?\s*[^\n,]+,\s*([^\n,]+@[^\n,]+)",
            text, re.IGNORECASE,
        )
        if m2:
            result["requestor_email"] = m2.group(1).strip()

    # ── Buyer Contact Details ─────────────────────────────────────
    # pdfplumber strips spaces, so the raw text looks like:
    # "BuyerContactDetails:ChikaObijiTelephoneNumber:(chikaobiji@chevron.com)"
    buyer_m = re.search(r'BuyerContactDetails:(.+?)TelephoneNumber', text, re.IGNORECASE)
    if buyer_m:
        raw = buyer_m.group(1).strip()
        result["buyer_name"] = re.sub(r'([a-z])([A-Z])', r'\1 \2', raw).strip()
    else:
        result["buyer_name"] = None

    buyer_email_m = re.search(
        r'BuyerContact.+?(?:Telephone|Phone)[^(]*\(([^@)]+@[^)]+)\)',
        text, re.IGNORECASE
    )
    result["buyer_email"] = buyer_email_m.group(1).strip() if buyer_email_m else None

    # ── Requisition Number ────────────────────────────────────────
    # Two PDF formats in use:
    #   Older GEP format: req number is in the page-1 header as
    #     "Requisition:\nREQ0612726" — readable by PyMuPDF text scan.
    #   Newer GEP SMART format: req number is only in the line-items
    #     table column, which both pdfplumber and PyMuPDF drop.
    # We try the table cell first, then fall back to PyMuPDF header scan.
    req_raw = None

    # Pass 1 — table cell (works for some PDF formats)
    for table in tables:
        req_col = None
        for row in table:
            cells = [clean(c) for c in row]
            for i, c in enumerate(cells):
                if c and "requisition" in c.lower():
                    req_col = i
                    break
            if req_col is not None:
                break
        if req_col is None:
            continue
        for row in table:
            cells = [clean(c) for c in row]
            if not cells or not cells[0] or not cells[0].isdigit():
                continue
            if len(cells) > req_col and cells[req_col]:
                val = cells[req_col].replace("\n", "").strip()
                if val:
                    req_raw = val
                    break
        if req_raw:
            break

    # Pass 2 — PyMuPDF header scan (older GEP format)
    if not req_raw:
        try:
            import fitz
            doc = fitz.open(pdf_path)
            req_re = re.compile(r'Requisition:\s*([A-Z]{2,}\d{4,})', re.IGNORECASE)
            for page in doc:
                m = req_re.search(page.get_text())
                if m:
                    req_raw = m.group(1)
                    break
            doc.close()
        except Exception:
            pass

    # Pass 3 — Tesseract OCR (handles image-based PDFs or unreadable font encodings)
    if not req_raw:
        try:
            import fitz
            from PIL import Image
            import pytesseract
            pytesseract.pytesseract.tesseract_cmd = _tesseract_cmd()
            req_val_re = re.compile(r'^(REQ|RPRNGN)\d{4,}$', re.IGNORECASE)
            doc = fitz.open(pdf_path)
            for page in doc:
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                ocr = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
                # Find x-centre of the "Requisition" column header
                req_col_x0 = req_col_x1 = req_hdr_top = None
                for i, word in enumerate(ocr["text"]):
                    if word.strip().lower() == "requisition" and int(ocr["conf"][i]) > 40:
                        req_col_x0 = ocr["left"][i]
                        req_col_x1 = ocr["left"][i] + ocr["width"][i]
                        req_hdr_top = ocr["top"][i]
                        break
                if req_col_x0 is None:
                    continue
                # Search below the header for a value in that x-band
                for i, word in enumerate(ocr["text"]):
                    w = word.strip()
                    if (not w or int(ocr["conf"][i]) < 40
                            or ocr["top"][i] <= req_hdr_top + 10):
                        continue
                    wx0 = ocr["left"][i]
                    wx1 = wx0 + ocr["width"][i]
                    overlap = min(wx1, req_col_x1) - max(wx0, req_col_x0)
                    if overlap > 0 and req_val_re.match(w):
                        req_raw = w
                        break
                if req_raw:
                    break
            doc.close()
        except Exception:
            pass

    result["req_number"] = req_raw

    # ── Ship To ───────────────────────────────────────────────────
    result["ship_to"] = kv.get("ShipTo")
    if not result.get("ship_to"):
        m = re.search(
            r"Ship\s*To\s*:?\s*(.+?)(?:\n\s*Bill\s*To|\Z)",
            text, re.IGNORECASE | re.DOTALL,
        )
        if m:
            result["ship_to"] = re.sub(r"\s+", " ", m.group(1)).strip()

    # ── Description from PO Text ─────────────────────────────────
    result["description_summary"] = kv.get("POText")
    if not result["description_summary"]:
        # Raw-text fallback: "PO Text  <description>"
        m = re.search(
            r"PO\s*Text\s+(.+?)(?:QA\s*Codes|Item\s*Cross\s*Reference|Incoterm|\Z)",
            text, re.IGNORECASE | re.DOTALL,
        )
        if m:
            desc = re.sub(r"\s+", " ", m.group(1)).strip()
            if desc:
                result["description_summary"] = desc[:500]
    if not result["description_summary"]:
        missing.append("description")

    # ── Product line ──────────────────────────────────────────────
    result["product_line"] = classify_product_line(
        result.get("description_summary") or ""
    )

    # ── Line items from the Material Items table(s) ──────────────
    # pdfplumber frequently splits a multi-line-item PO so that EACH data
    # row lands in its own detached mini-table, separate from the header
    # row. The old logic found the header table, saw no data rows under it,
    # and broke — losing every line item (and the RDD with them).
    #
    # Robust approach: scan EVERY table's rows. Any row whose first cell is
    # a bare line number is a data row. We don't trust header column indices
    # (they don't line up once rows are split); instead we read values by
    # shape — dates are M/D/YYYY, the RDD is the FIRST date in the row
    # (Promised Date is the second), quantity is an N.NN cell, the supplier
    # item code looks like SPMNLxxx, and the description is the longest text.
    result["line_items"] = []
    result["required_delivery_date"] = None

    date_cell_re = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
    qty_cell_re = re.compile(r"^\d+\.\d{2}$")
    code_cell_re = re.compile(r"^SPMNL\w*$", re.IGNORECASE)

    items_by_line: dict[str, dict] = {}
    for table in tables:
        for row in table:
            cells = [clean(c) for c in row]
            if not cells:
                continue
            line_no = cells[0]
            if not line_no or not line_no.isdigit():
                continue

            dates = [parse_gep_date(c) for c in cells if c and date_cell_re.match(c)]
            rdd      = dates[0] if len(dates) > 0 else None  # col 1 = Required Delivery Date
            promised = dates[1] if len(dates) > 1 else None  # col 2 = Promised Date

            quantity = None
            for c in cells:
                if c and qty_cell_re.match(c):
                    quantity = float(c)
                    break

            item_number = next((c for c in cells if c and code_cell_re.match(c)), None)

            description = None
            for c in cells[1:]:
                if not c or date_cell_re.match(c) or qty_cell_re.match(c):
                    continue
                # skip pure price/number/code noise
                if re.match(r"^[\d.,()USD ]+$", c):
                    continue
                # skip IncoTerm Code column values — "D-Delivered,DutyPaid",
                # "F-FreeOnBoard", "E-ExWorks" etc. (letter + dash + 6+ letters/commas,
                # no digits or spaces); require 6+ chars after dash so short
                # item codes like "D-RING" (4 chars) are not accidentally excluded.
                if re.match(r"^[A-Z]-[A-Za-z,]{6,}$", c):
                    continue
                if description is None or len(c) > len(description):
                    description = c

            # A data row may reappear across page repeats; keep the richest copy.
            existing = items_by_line.get(line_no)
            candidate = {
                "line_no": line_no,
                "description": description,
                "item_number": item_number,
                "quantity": quantity,
                "required_delivery_date": rdd,
                "promised_date": promised,
            }
            if existing is None or (rdd and not existing.get("required_delivery_date")):
                items_by_line[line_no] = candidate
            elif promised and not existing.get("promised_date"):
                existing["promised_date"] = promised

    # Gap-fill: pdfplumber sometimes finds most line items but misses a line
    # whose table header landed at the very bottom of one page while the data
    # row is at the top of the next (cross-page table split). Detect sequential
    # gaps in the found line numbers and fill them using the word-position
    # fallback, which collects promised dates in PDF order regardless of page
    # breaks — so fallback[i] matches the i-th slot in the full 1..N sequence.
    if items_by_line:
        min_line = min(int(k) for k in items_by_line)
        max_line = max(int(k) for k in items_by_line)
        # Start from min_line, not 1 — a change order PDF may legitimately
        # begin at line 2+ when only specific lines were revised.
        all_expected = {str(i) for i in range(min_line, max_line + 1)}
        missing_lines = sorted(all_expected - set(items_by_line), key=int)
        if missing_lines:
            gap_pds = extract_promised_dates_from_words(pdf_path)
            full_seq = [str(i) for i in range(1, max_line + 1)]
            rep_rdd = max(
                (v["required_delivery_date"] for v in items_by_line.values()
                 if v.get("required_delivery_date")),
                default=None,
            )
            for ln in missing_lines:
                idx = full_seq.index(ln)
                items_by_line[ln] = {
                    "line_no": ln,
                    "description": None,
                    "item_number": None,
                    "quantity": None,
                    "required_delivery_date": rep_rdd,
                    "promised_date": gap_pds[idx] if idx < len(gap_pds) else None,
                }

    result["line_items"] = [items_by_line[k] for k in sorted(items_by_line, key=int)]

    # Order-level RDD = the LATEST required date across line items — the order
    # is complete only when the last item is delivered. Falls back to None.
    rdds = [li["required_delivery_date"] for li in result["line_items"] if li["required_delivery_date"]]
    result["required_delivery_date"] = max(rdds) if rdds else None

    # Fallback: some POs have descriptions so large that pdfplumber forms no
    # table row, so the loop above finds nothing. Recover the RDD from the
    # raw word positions in that case.
    if not result["required_delivery_date"]:
        result["required_delivery_date"] = extract_rdd_from_words(pdf_path)

    # Promised-date word fallback: pdfplumber sometimes splits rows for long
    # descriptions, pushing the Promised Date cell into a continuation sub-row
    # (no line_no → skipped by the loop). Use x-band word extraction to recover.
    if result["line_items"] and not any(li.get("promised_date") for li in result["line_items"]):
        fallback_pds = extract_promised_dates_from_words(pdf_path)
        if fallback_pds:
            if len(fallback_pds) == len(result["line_items"]):
                for li, pd in zip(result["line_items"], fallback_pds):
                    li["promised_date"] = pd
            else:
                # Different count — assign what we can by position
                for li, pd in zip(result["line_items"], fallback_pds):
                    li["promised_date"] = pd

    # Order-level promised date = LATEST promised date across line items.
    ppds = [li["promised_date"] for li in result["line_items"] if li.get("promised_date")]
    result["po_promised_date"] = max(ppds) if ppds else None

    # Single-item fallback: when table detection produced NO line items,
    # synthesise one row so per-item tracking has something. Two conditions
    # can each independently trigger this:
    #   (a) RDD was recovered from text — the normal case for short descriptions
    #   (b) Word-position fallback found exactly 1 promised date — covers
    #       gasket PDFs where the whole table spans a page break and pdfplumber
    #       can't link the header (page N) to the data row (page N+1); in those
    #       PDFs both the RDD and the promised date live on the data-only page
    #       which has no header context, so extract_rdd_from_words also misses.
    if not result["line_items"]:
        pd_fallback = extract_promised_dates_from_words(pdf_path)
        item_nos = set(re.findall(r"(?m)^\s*(\d{1,2})\s+[A-Z]", text))
        # item_nos regex can false-positive on description text like "1-1/2 IN"
        # wrapping to "2 IN" on a new line. Trust the word-position fallback count
        # instead: if exactly 1 promised date found in the column, it's 1 item.
        is_single = item_nos == {"1"} or len(pd_fallback) == 1
        if is_single and (result["required_delivery_date"] or pd_fallback):
            code_m = re.search(r"\b(SPMNL\w*)\b", text)
            result["line_items"] = [{
                "line_no": "1",
                "description": result.get("description_summary"),
                "item_number": code_m.group(1) if code_m else None,
                "quantity": None,
                "required_delivery_date": result["required_delivery_date"],
                "promised_date": pd_fallback[0] if pd_fallback else None,
            }]

    # Use line item description as fallback summary if PO Text was missing
    if not result.get("description_summary") and result["line_items"]:
        result["description_summary"] = result["line_items"][0].get("description")

    # ── Net total & currency ──────────────────────────────────────
    # kv map often has a "NetTotal" or "Net Total" row from the summary table.
    _total_raw = (
        kv.get("NetTotal")
        or kv.get("Net Total")
        or kv.get("OrderTotal")
        or kv.get("Order Total")
        or kv.get("Total")
    )
    if _total_raw:
        _num = re.sub(r"[^\d.]", "", _total_raw.replace(",", ""))
        try:
            result["net_total"] = float(_num) if _num else None
        except ValueError:
            result["net_total"] = None
    else:
        result["net_total"] = None

    # Raw-text fallback for net_total (HTML-to-PDF format).
    if result["net_total"] is None:
        m = re.search(r"Net\s*Total\s*:?\s*([0-9,]+\.?\d*)", text, re.IGNORECASE)
        if m:
            try:
                result["net_total"] = float(m.group(1).replace(",", ""))
            except ValueError:
                pass

    # Currency: check kv first, then look for explicit USD / NGN / ₦ in text.
    _currency_raw = kv.get("Currency") or kv.get("Curr")
    if _currency_raw and re.search(r"\bNGN\b", _currency_raw, re.IGNORECASE):
        result["currency"] = "NGN"
    elif _currency_raw and re.search(r"\bUSD\b", _currency_raw, re.IGNORECASE):
        result["currency"] = "USD"
    elif "₦" in text or re.search(r"\bNGN\b", text):
        result["currency"] = "NGN"
    else:
        result["currency"] = "USD"

    # ── Confidence ────────────────────────────────────────────────
    result["confidence"] = "low" if len(missing) >= 2 else "high"
    result["extraction_method"] = "pdfplumber"

    return result

def decompress_gep_text(text: str) -> str:
    """
    pdfplumber strips spaces from GEP PDFs. This restores spaces before
    capital letters that follow lowercase letters, fixing most field values.
    e.g. 'Within60daysDuenet' → 'Within 60 days Due net'
    """
    import re
    # Insert space before uppercase that follows lowercase or digit
    text = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', text)
    # Insert space before digit that follows letter
    text = re.sub(r'([a-zA-Z])(\d)', r'\1 \2', text)
    # Insert space before letter that follows digit
    text = re.sub(r'(\d)([a-zA-Z])', r'\1 \2', text)
    return text
# ─────────────────────────────────────────────
# CLAUDE EXTRACTION (fallback — costs credits)
# ─────────────────────────────────────────────

EXTRACTION_PROMPT = """You are reading a Chevron purchase order PDF exported from \
the GEP SMART supplier portal. Extract ALL of the following fields exactly as they appear.

Respond with ONLY valid JSON, no other text, no markdown fences:
{
  "po_number": "<Purchase Order Number — digits only, e.g. 0061440972>",
  "status": "<Status field value, e.g. 'Partner Acknowledged'>",
  "order_submitted_on": "<Order Submitted on date — YYYY-MM-DD format or null>",
  "supplier_acknowledged_on": "<Supplier Acknowledged on date — YYYY-MM-DD format, \
look for exact label 'Supplier Acknowledged on', or null if not present>",
  "payment_terms": "<Payment Terms field value or null>",
  "po_destination": "<PO Destination field value or null>",
  "transportation": "<Transportation field value or null>",
  "requestor_name": "<Requestor Name from Purchaser Information section or null>",
  "requestor_email": "<Requestor Email from Purchaser Information section or null>",
  "buyer_name": "<Buyer name from Buyer Contact Details section (page 4) or null>",
  "buyer_email": "<Buyer email from Buyer Contact Details section (page 4) or null>",
  "req_number": "<Requisition Number from the line items table (e.g. RPRNGN0029859) or null>",
  "ship_to": "<Ship To address or null>",
  "description_summary": "<one short sentence summarizing what is being ordered>",
  "product_line": "<gasket|valve|lng|spacer|piping|consumable|other>",
  "required_delivery_date": "<YYYY-MM-DD format from line items, or null>",
  "line_items": [
    {
      "line_no": "<Line No.>",
      "description": "<full Description text>",
      "item_number": "<Item Number>",
      "supplier_item_number": "<Supplier Item Number or null>",
      "quantity": "<number>",
      "uom": "<Unit of Measure>",
      "required_delivery_date": "<YYYY-MM-DD>",
      "unit_price": "<number or null>",
      "promised_date": "<YYYY-MM-DD or null>",
      "total": "<number or null>"
    }
  ],
  "net_total": "<Net total amount as a plain number (no currency symbol, no commas), e.g. 8625000.00, or null>",
  "currency": "<Currency code on the PO — USD or NGN — default USD if not shown>",
  "confidence": "<high or low>"
}

Classification guidance for product_line:
- gasket: spiral wound gaskets, ring joints, sealing materials, Flexitallic items
- valve: ball valves, gate valves, check valves, actuators
- lng: liquefied natural gas specific equipment
- spacer: spacer rings, RTJ spacers, ring spacers, blind spacers
- piping: pipes, fittings, flanges, elbows
- consumable: chemicals, fluids, oils, refractories
- other: anything that does not clearly fit the above

Critical:
- supplier_acknowledged_on is ONLY present on GEP-exported PDFs after acknowledgment.
  If the label 'Supplier Acknowledged on' does not appear, return null.
- All dates must be YYYY-MM-DD format.
"""


def extract_pdf_with_claude(pdf_path: str) -> dict:
    """Claude fallback — only called when pdfplumber fails or returns low confidence."""
    client = _get_claude()
    pdf_bytes = Path(pdf_path).read_bytes()
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
    del pdf_bytes  # free raw bytes before the network round-trip

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",  # cheapest model — sufficient for structured PDFs
        max_tokens=2000,
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
                    {"type": "text", "text": EXTRACTION_PROMPT},
                ],
            }
        ],
    )

    response_text = message.content[0].text.strip()

    # Strip markdown fences if present
    if "```" in response_text:
        for part in response_text.split("```"):
            part = part.strip().lstrip("json").strip()
            if part.startswith("{"):
                response_text = part
                break

    start = response_text.find("{")
    end = response_text.rfind("}") + 1
    if start != -1 and end > start:
        response_text = response_text[start:end]

    try:
        data = json.loads(response_text)
        data["extraction_method"] = "claude_fallback"
        return data
    except json.JSONDecodeError as e:
        print(f"   ⚠️  Claude JSON parse error: {e}")
        return {}


# ─────────────────────────────────────────────
# EXTRACTION ROUTER
# ─────────────────────────────────────────────

def extract_pdf(pdf_path: str) -> tuple[dict, str]:
    """
    pdfplumber only. We do NOT fall back to Claude for low confidence —
    the old minimal PO PDFs legitimately lack some fields, and partial
    data is still useful. Claude only for genuine errors (and even then
    only if credits exist).
    """
    print(f"   🔍 Trying pdfplumber...")
    result = extract_pdf_with_pdfplumber(pdf_path)

    if "error" in result:
        err = result["error"]
        if "no tables" in err.lower():
            print(f"   ⏭️  Not a GEP PO (no tables) — flagged, skipping.")
            return {"confidence": "not_gep", "extraction_method": "skipped",
                    "skip_reason": err}, "skipped"
        if "eof" in err.lower() or "xref" in err.lower():
            print(f"   ⚠️  Corrupt PDF — flagged.")
            return {"confidence": "corrupt", "extraction_method": "failed",
                    "skip_reason": err}, "failed"
        # other error → flag, don't crash on Claude
        return {"confidence": "error", "extraction_method": "failed",
                "skip_reason": err}, "failed"

    # Accept whatever pdfplumber got — even low confidence
    return result, "pdfplumber"


# ─────────────────────────────────────────────
# DB SAVE
# ─────────────────────────────────────────────

def save_extraction_result(order_id: str, extraction: dict) -> None:
    """Save extraction to orders table and upsert line items.

    NOTE: This reads the po_attachments PDF (pre-acknowledgment version),
    which never contains the 'Supplier Acknowledged on' date. The ack date
    is extracted separately from the ack_attachments PDF by ack_pdf_extractor.
    So we do NOT set acknowledged_at or acknowledgment_status here.
    """
    client = get_client()

    confidence = extraction.get("confidence")

    # Non-GEP or corrupt PDFs — flag for review, don't parse
    if confidence in ("not_gep", "corrupt"):
        client.table("orders").update({
            "extraction_confidence": confidence,
            "extraction_raw": extraction,
        }).eq("id", order_id).execute()
        return

    update = {
        "extracted_description": extraction.get("description_summary"),
        "product_line": extraction.get("product_line"),
        "required_delivery_date": extraction.get("required_delivery_date"),
        "extraction_confidence": extraction.get("confidence", "low"),
        "extraction_raw": extraction,
        "payment_terms": extraction.get("payment_terms"),
        "po_destination": extraction.get("po_destination"),
        "transportation": extraction.get("transportation"),
        "requestor_name": extraction.get("requestor_name"),
        "requestor_email": extraction.get("requestor_email"),
        "buyer_name": extraction.get("buyer_name"),
        "req_number": extraction.get("req_number"),
        "ship_to": extraction.get("ship_to"),
        "order_submitted_on": extraction.get("order_submitted_on"),
        # acknowledged_at intentionally NOT set here — comes from ack PDF
    }

    # Currency is always taken from the PDF (most authoritative source).
    # Amount is only written when the email body didn't already capture one —
    # the email body correctly extracts USD amounts, so we don't overwrite them.
    pdf_currency = extraction.get("currency") or "USD"
    pdf_amount   = extraction.get("net_total")

    existing_row = client.table("orders").select("po_amount,po_currency").eq("id", order_id).execute()
    if existing_row.data:
        stored = existing_row.data[0]
        update["po_currency"] = pdf_currency
        if stored.get("po_amount") is None and pdf_amount is not None:
            update["po_amount"] = pdf_amount

    update = {k: v for k, v in update.items() if v is not None}
    client.table("orders").update(update).eq("id", order_id).execute()

    for item in extraction.get("line_items", []):
        # Dedup on (order_id, line_no) — the stable identity of a line item.
        # buyer_part_code can be null (e.g. spacers) so it's not reliable.
        line_no = item.get("line_no")
        payload = {
            "order_id": order_id,
            "line_no": line_no,
            "buyer_part_code": item.get("item_number"),
            "supplier_part_code": item.get("supplier_item_number"),
            "description": item.get("description"),
            "quantity": item.get("quantity"),
            "unit_price": item.get("unit_price"),
            "line_total": item.get("total"),
            "required_delivery_date": item.get("required_delivery_date"),
            "promised_date": item.get("promised_date"),
        }
        payload = {k: v for k, v in payload.items() if v is not None}

        existing = (
            client.table("order_line_items")
            .select("id")
            .eq("order_id", order_id)
            .eq("line_no", line_no)
            .execute()
        )
        if existing.data:
            client.table("order_line_items").update(payload).eq(
                "id", existing.data[0]["id"]
            ).execute()
        else:
            client.table("order_line_items").insert(payload).execute()

# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────

def get_orders_needing_extraction() -> list[dict]:
    client = get_client()
    result = (
        client.table("orders")
        .select("id, buyer_po_number, pdf_url, pdf_attachment_path")
        .or_("pdf_url.not.is.null,pdf_attachment_path.not.is.null")
        .is_("extraction_raw", "null")
        .execute()
    )
    return result.data


def _download_pdf_to_temp(url: str) -> str:
    """Download a PDF from a URL to a temp file. Returns the temp file path."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_path = tmp.name
    urllib.request.urlretrieve(url, tmp_path)
    return tmp_path


def run_extraction_pass() -> None:
    orders = get_orders_needing_extraction()
    if not orders:
        return

    print(f"\n📄 Found {len(orders)} order(s) needing extraction...")

    for order in orders:
        pdf_url        = order.get("pdf_url")
        pdf_path_local = order.get("pdf_attachment_path")
        po_number      = order["buyer_po_number"]
        tmp_path       = None

        try:
            if pdf_url:
                tmp_path = _download_pdf_to_temp(pdf_url)
                pdf_path = tmp_path
            elif pdf_path_local and Path(pdf_path_local).exists():
                pdf_path = pdf_path_local
            else:
                print(f"⚠️  PO {po_number}: no PDF available (no URL, no local file), skipping.")
                continue

            extraction, method = extract_pdf(pdf_path)

            if not extraction:
                print(f"❌ PO {po_number}: both pdfplumber and Claude failed.")
                continue

            save_extraction_result(order["id"], extraction)

            ack_found = (
                "✅ ack date found"
                if extraction.get("supplier_acknowledged_on")
                else "⏳ no ack date"
            )
            confidence_flag = (
                " ⚠️ LOW CONFIDENCE"
                if extraction.get("confidence") == "low"
                else ""
            )
            print(
                f"✅ PO {po_number} [{method}]: "
                f"'{extraction.get('product_line')}', "
                f"{len(extraction.get('line_items', []))} item(s), "
                f"RDD: {extraction.get('required_delivery_date')}, "
                f"{ack_found}{confidence_flag}"
            )

        except Exception as e:
            print(f"❌ PO {po_number}: extraction failed — {e}")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


def run_forever() -> None:
    print(
        f"📄 SPM PDF extractor started (pdfplumber primary, Claude fallback). "
        f"Checking every {CHECK_INTERVAL_SECONDS}s. Press Ctrl+C to stop."
    )
    while True:
        try:
            run_extraction_pass()
        except Exception as e:
            print(f"❌ Error during extraction pass: {e}")
            # Drop the cached DB client so the next pass gets a fresh connection.
            # Without this a stale socket after a network blip keeps failing
            # even after connectivity is restored.
            reset_client()
        gc.collect()
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
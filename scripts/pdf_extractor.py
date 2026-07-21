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

import os
import json
import base64
import re
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
import anthropic
import pdfplumber

from db import get_client, reset_client

load_dotenv()

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

def extract_pdf_with_pdfplumber(pdf_path: str) -> dict:
    """
    Extract fields from a GEP PO PDF using pdfplumber's table extraction.
    Tables give clean cells; we just strip intra-cell newlines.
    Returns dict with same keys as Claude extraction.
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
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
            pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
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

    # ── Description from PO Text ─────────────────────────────────
    result["description_summary"] = kv.get("POText")
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
            rdd = dates[0] if dates else None  # first date = Required Delivery Date

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
            }
            if existing is None or (rdd and not existing.get("required_delivery_date")):
                items_by_line[line_no] = candidate

    result["line_items"] = [items_by_line[k] for k in sorted(items_by_line, key=int)]

    # Order-level RDD = the earliest required date across line items (the
    # binding deadline for OTD). Falls back to None if no line item had one.
    rdds = [li["required_delivery_date"] for li in result["line_items"] if li["required_delivery_date"]]
    result["required_delivery_date"] = min(rdds) if rdds else None

    # Fallback: some POs have descriptions so large that pdfplumber forms no
    # table row, so the loop above finds nothing. Recover the RDD from the
    # raw word positions in that case.
    if not result["required_delivery_date"]:
        result["required_delivery_date"] = extract_rdd_from_words(pdf_path)

    # Single-item fallback: when table detection produced NO line items but a
    # RDD was recovered, and the PDF text shows exactly one item row ("1 ..."),
    # synthesise that one line item so per-item tracking still has a row. Its
    # RDD is the order RDD (it's the only item). Guarded to fire only for truly
    # single-item POs so a multi-item PO is never collapsed into one.
    if result["required_delivery_date"] and not result["line_items"]:
        item_nos = set(re.findall(r"(?m)^\s*(\d{1,2})\s+[A-Z]", text))
        if item_nos == {"1"}:
            code_m = re.search(r"\b(SPMNL\w*)\b", text)
            result["line_items"] = [{
                "line_no": "1",
                "description": result.get("description_summary"),
                "item_number": code_m.group(1) if code_m else None,
                "quantity": None,  # unreliable in table-less layout; left for pricing/Claude
                "required_delivery_date": result["required_delivery_date"],
            }]

    # Use line item description as fallback summary if PO Text was missing
    if not result.get("description_summary") and result["line_items"]:
        result["description_summary"] = result["line_items"][0].get("description")

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
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    pdf_bytes = Path(pdf_path).read_bytes()
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

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
        .select("*")
        .not_.is_("pdf_attachment_path", "null")
        .is_("extraction_raw", "null")
        .execute()
    )
    return result.data


def run_extraction_pass() -> None:
    orders = get_orders_needing_extraction()
    if not orders:
        return

    print(f"\n📄 Found {len(orders)} order(s) needing extraction...")

    for order in orders:
        pdf_path = order["pdf_attachment_path"]
        po_number = order["buyer_po_number"]

        if not pdf_path or not Path(pdf_path).exists():
            print(f"⚠️  PO {po_number}: PDF not found at {pdf_path}, skipping.")
            continue

        try:
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
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
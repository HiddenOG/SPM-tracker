"""
ack_pdf_extractor.py — Extract 'Supplier Acknowledged on' date from the
ack_attachments PDF (the GEP-exported version downloaded after acknowledgment).

Separate from pdf_extractor.py because:
  - pdf_extractor reads po_attachments (pre-ack, never has the date)
  - this reads ack_attachments (post-ack, has the date)

Matches ack PDFs to orders by:
  1. PO number anywhere in the FILENAME (fast path — catches 'ePurchase_<PO>')
  2. PO number printed INSIDE the PDF (content fallback — catches scans and
     generically-named files where the PO isn't in the filename)

Run:  python scripts/ack_pdf_extractor.py
"""

import os
import re
import sys
import time
import pdfplumber
from pathlib import Path

from dotenv import load_dotenv

from pdf_extractor import extract_pdf_with_pdfplumber
from db import get_client

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

load_dotenv()

ACK_FOLDER = os.environ.get(
    "ACK_ATTACHMENTS_DIR",
    r"C:\Users\Godson\spm-tracker\data\ack_attachments",
)


def _normalize(s: str) -> str:
    """Strip spaces around dashes so '0061365529 - 001' matches '0061365529-001'."""
    return re.sub(r"\s*-\s*", "-", s)


def find_ack_pdf_for_po(po_number: str, all_files: list[str]) -> str | None:
    """
    Find the acknowledged PDF for a PO. Matches if the PO number appears
    ANYWHERE in the filename. Normalises spaces around dashes so that the DB
    value '0061365529-001' matches a file named '0061365529 - 001.pdf'.

    For revision orders (e.g. '0061357197-001'), if no exact match is found,
    falls back to files matching just the base PO number — the attachment from
    the warehouse email is often saved under the base number even when the PDF
    content shows the revision.

    When several files match, prefer the ePurchase export, then shortest name.
    """
    po_norm = _normalize(po_number)
    matches = [f for f in all_files if po_norm in _normalize(f)]

    if not matches and "-" in po_number:
        # Revision fallback: try the base PO number only.
        # For this path, prefer NON-ePurchase files — the ePurchase file in the
        # folder belongs to the base order, while the revision document was saved
        # as a plain attachment (e.g. from the warehouse forwarding email).
        base_po = po_number.split("-")[0].strip()
        base_matches = [f for f in all_files if base_po in _normalize(f)]
        base_matches.sort(key=lambda f: (1 if "epurchase" in f.lower() else 0, len(f)))
        matches = base_matches

    if not matches:
        return None

    if not ("-" in po_number and matches):
        # Normal (exact-match) path: prefer ePurchase
        matches.sort(key=lambda f: (0 if "epurchase" in f.lower() else 1, len(f)))
    return os.path.join(ACK_FOLDER, matches[0])


def _ocr_ack_date(ack_pdf_path: str) -> str | None:
    """
    OCR fallback for image-based PDFs (ePurchase files saved from a browser).
    Renders page 0 at 2x and uses Tesseract word-level bounding boxes to pair
    the 'Supplier Acknowledged on' label with its value, even when the two-column
    table layout puts them in separate text blocks.
    """
    try:
        import fitz
        import pytesseract
        from PIL import Image
        from pdf_extractor import parse_gep_date

        pytesseract.pytesseract.tesseract_cmd = (
            r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        )

        doc = fitz.open(ack_pdf_path)
        page = doc[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

        # Strategy 1: simple regex on plain text (works when label+value are on one line)
        text = pytesseract.image_to_string(img)
        m = re.search(
            r"Supplier\s+Acknowledged\s+on:?\s*(\d{1,2}/\d{1,2}/\d{4})",
            text,
            re.IGNORECASE,
        )
        if m:
            return parse_gep_date(m.group(1))

        # Strategy 2: bounding-box alignment (works when label and value are in
        # separate columns that OCR reads as separate text blocks)
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        words = list(zip(
            data["text"], data["left"], data["top"], data["conf"]
        ))

        date_re = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")

        # Find the Y coordinate of the "Acknowledged" word
        ack_top = None
        for txt, left, top, conf in words:
            if "acknowledged" in txt.lower() and int(conf) > 0:
                ack_top = top
                break

        if ack_top is not None:
            # Look for a date in the same Y band (±25 px) to the right of the label
            for txt, left, top, conf in words:
                if (
                    abs(top - ack_top) < 25
                    and int(conf) > 0
                    and date_re.match(txt.strip())
                    and left > 400  # must be in the right column
                ):
                    return parse_gep_date(txt.strip())

    except Exception as e:
        print(f"   ⚠️  OCR error: {e}")
    return None


def extract_ack_date(ack_pdf_path: str) -> str | None:
    """
    Read the ack PDF and return the Supplier Acknowledged on date (ISO).
    Tries pdfplumber text extraction first; falls back to Tesseract OCR for
    image-based PDFs (ePurchase files saved from a browser have no text layer).
    """
    if not ack_pdf_path or not Path(ack_pdf_path).exists():
        return None

    # Primary: pdfplumber (works on native GEP PDFs)
    result = extract_pdf_with_pdfplumber(ack_pdf_path)
    date = None if "error" in result else result.get("supplier_acknowledged_on")
    if date:
        return date

    # Fallback: OCR (works on ePurchase HTML-to-image PDFs)
    return _ocr_ack_date(ack_pdf_path)


def build_content_index(all_files: list[str], wanted_pos: set[str]) -> dict:
    """
    CONTENT FALLBACK — scan PDFs whose filename did NOT reveal a wanted PO,
    read the PO number printed inside (the 'Purchase Order Number' field, or
    any standalone 006… number), and map it to the file if it carries an
    acknowledged date. Returns {po_number: pdf_path}.

    Only scans files that don't already name a wanted PO, so we don't redo
    the cheap filename matches. Keeps the most relevant (ePurchase) file when
    several map to the same PO.
    """
    index = {}

    # Files that already name a wanted PO are handled by filename matching;
    # only content-scan the rest.
    def names_a_wanted_po(fname: str) -> bool:
        return any(po in fname for po in wanted_pos)

    candidates = [f for f in all_files if not names_a_wanted_po(f)]

    for fname in candidates:
        path = os.path.join(ACK_FOLDER, fname)
        try:
            with pdfplumber.open(path) as pdf:
                text = pdf.pages[0].extract_text() or ""
        except Exception:
            continue  # corrupt/unreadable — skip

        # PO number printed inside the document
        po_m = re.search(r"Purchase\s*Order\s*Number:?\s*(\d{8,12})", text, re.IGNORECASE)
        if not po_m:
            po_m = re.search(r"\b(006\d{7})\b", text)
        if not po_m:
            continue
        po_number = po_m.group(1)

        if po_number not in wanted_pos:
            continue  # not an order we're trying to fill

        # Only useful if this copy actually carries the acknowledged date
        if not re.search(r"Supplier\s*Acknowledged\s*on:?\s*\d", text, re.IGNORECASE):
            continue

        # Prefer an ePurchase copy if we see several for the same PO
        if po_number not in index or "epurchase" in fname.lower():
            index[po_number] = path

    return index


def get_orders_needing_ack_date() -> list[dict]:
    """Orders that are acknowledged (or any order) but missing acknowledged_at."""
    client = get_client()
    result = (
        client.table("orders")
        .select("id, buyer_po_number, acknowledgment_status")
        .is_("acknowledged_at", "null")
        .execute()
    )
    return result.data


def run_pass() -> None:
    orders = get_orders_needing_ack_date()
    if not orders:
        return

    if not os.path.isdir(ACK_FOLDER):
        print(f"❌ Ack folder not found: {ACK_FOLDER}")
        return

    all_files = [f for f in os.listdir(ACK_FOLDER) if f.lower().endswith(".pdf")]
    client = get_client()

    print(f"\n📑 {len(orders)} order(s) missing ack date. Scanning {len(all_files)} ack PDFs...")

    # Content-scan fallback is skipped for large ack folders (>100 files) because
    # most files are image-based PDFs where pdfplumber returns nothing, causing
    # the scan to hang. Filename matching (above) catches all ePurchase files.
    wanted_pos = {o["buyer_po_number"] for o in orders}
    content_index = {}
    if len(all_files) <= 100:
        content_index = build_content_index(all_files, wanted_pos)
        if content_index:
            print(f"   🔎 Content scan matched {len(content_index)} extra PDF(s) by inner PO number.")

    found = 0
    no_pdf = 0
    no_date = 0

    for order in orders:
        po = order["buyer_po_number"]

        # 1. Fast path: PO number in the filename
        ack_pdf = find_ack_pdf_for_po(po, all_files)
        # 2. Fallback: PO number found inside a PDF's content
        if not ack_pdf:
            ack_pdf = content_index.get(po)

        if not ack_pdf:
            no_pdf += 1
            continue

        ack_date = extract_ack_date(ack_pdf)

        if ack_date:
            client.table("orders").update({
                "acknowledged_at": ack_date,
                "acknowledgment_status": "acknowledged",
                "pending_ack_extraction": False,
            }).eq("id", order["id"]).execute()
            found += 1
            print(f"✅ PO {po}: acknowledged_at = {ack_date}")
        else:
            no_date += 1
            # The ack PDF exists but has no date — likely a pre-ack version
            # saved into ack_attachments, or corrupt. Flag for review.
            print(f"⏳ PO {po}: ack PDF found but no date inside")

    print(f"\nSummary: {found} dated, {no_pdf} no PDF on disk, {no_date} PDF but no date")


def run_forever() -> None:
    print("📑 Ack PDF extractor started (pdfplumber, filename + content matching). Press Ctrl+C to stop.")
    while True:
        try:
            run_pass()
        except Exception as e:
            print(f"❌ Error: {e}")
        time.sleep(120)


if __name__ == "__main__":
    run_forever()
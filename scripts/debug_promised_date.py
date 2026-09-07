"""
debug_promised_date.py — detailed extraction trace for specific POs
that are missing promised_date. Shows exactly what pdfplumber finds.
"""
import os, sys, tempfile, urllib.request, re
from pathlib import Path
from dotenv import load_dotenv

for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError): pass

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))

from db import get_client
import pdfplumber
from pdf_extractor import (
    parse_gep_date, extract_pdf_with_pdfplumber,
    extract_promised_dates_from_words
)

TARGET_POS = [
    "0061458131",
]

def debug_po(order, pdf_path):
    po = order["buyer_po_number"]
    print(f"\n{'='*60}")
    print(f"PO {po}")
    print(f"{'='*60}")

    date_cell_re = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")

    # Show raw table rows pdfplumber produces for this PDF
    with pdfplumber.open(pdf_path) as pdf:
        tables = []
        for page in pdf.pages:
            tables.extend(page.extract_tables())

    data_rows = []
    for table in tables:
        for row in table:
            cells = [
                (c.replace("\n", "").strip() if c else None) for c in row
            ]
            if cells and cells[0] and cells[0].isdigit():
                dates = [c for c in cells if c and date_cell_re.match(c)]
                print(f"  Row line_no={cells[0]}  dates_in_row={dates}  total_cells={len(cells)}")
                data_rows.append((cells[0], dates))

    if not data_rows:
        print("  (no data rows with digit line_no found by pdfplumber)")

    # Run full extraction
    result = extract_pdf_with_pdfplumber(pdf_path)
    print(f"\n  Extracted {len(result.get('line_items', []))} line item(s)")
    for li in result.get("line_items", []):
        print(f"    line {li['line_no']}: rdd={li.get('required_delivery_date')}  promised={li.get('promised_date')}")

    # Run word-position fallback directly
    fb = extract_promised_dates_from_words(pdf_path)
    print(f"\n  Word-position fallback returned: {fb}")

    # Show what 'Promised' / 'IncoTerm' x-positions look like
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            words = page.extract_words()
            prom = [w for w in words if w["text"] in ("Promised", "IncoTerm")]
            if prom:
                print(f"\n  Page {i+1} header words (Promised/IncoTerm):")
                for w in prom:
                    print(f"    '{w['text']}' x0={w['x0']:.1f} top={w['top']:.1f}")


def main():
    client = get_client()
    rows = (
        client.table("orders")
        .select("id, buyer_po_number, pdf_url, pdf_attachment_path")
        .in_("buyer_po_number", TARGET_POS)
        .execute().data or []
    )
    index = {r["buyer_po_number"]: r for r in rows}

    for po in TARGET_POS:
        order = index.get(po)
        if not order:
            print(f"\nPO {po}: not found in DB")
            continue

        pdf_url = order.get("pdf_url")
        pdf_local = order.get("pdf_attachment_path")
        tmp_path = None

        try:
            if pdf_url:
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                    tmp_path = f.name
                urllib.request.urlretrieve(pdf_url, tmp_path)
                debug_po(order, tmp_path)
            elif pdf_local and Path(pdf_local).exists():
                debug_po(order, pdf_local)
            else:
                print(f"\nPO {po}: no PDF available")
        except Exception as e:
            print(f"\nPO {po}: download/extraction error — {e}")
        finally:
            if tmp_path:
                try: os.unlink(tmp_path)
                except OSError: pass


if __name__ == "__main__":
    main()

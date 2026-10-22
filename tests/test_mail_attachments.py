"""Ask reads attachments: supplier offers come as PDF or Excel files, so the
email itself often says only "please find attached"."""
import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import mail_attachments as MA


def _pdf(lines) -> bytes:
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 800
    for line in lines:
        c.drawString(50, y, line)
        y -= 18
    c.save()
    return buf.getvalue()


def _xlsx(rows) -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Offer"
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_a_pdf_offer_is_read():
    data = _pdf(["QUOTATION QU-683133-2", "1 GASKET SPIRAL WOUND 36IN 300LB CGI 316 GRAPHITE 2 EA 410.00"])
    a = MA.extract_text(data, "QU_683133_2.pdf", "application/pdf")
    assert a["kind"] == "pdf" and a["pages"] == 1 and not a["cut"]
    assert "QU-683133-2" in a["text"] and "410.00" in a["text"]


def test_an_excel_offer_is_read_row_by_row():
    data = _xlsx([["Item", "Description", "Qty", "Price"],
                  [1, "GASKET SW 36IN 300# CGI", 2, 410.0]])
    a = MA.extract_text(data, "offer.xlsx")
    assert a["kind"] == "xlsx"
    assert "--- sheet Offer ---" in a["text"]
    assert "1 | GASKET SW 36IN 300# CGI | 2 | 410" in a["text"]


def test_long_files_are_cut_and_say_so():
    data = ("x" * 50 + "\n").encode() * 1000
    a = MA.extract_text(data, "notes.txt")
    assert a["cut"] and len(a["text"]) == MA.MAX_CHARS


def test_unreadable_files_explain_themselves():
    with pytest.raises(MA.AttachmentError, match="old-style .xls"):
        MA.extract_text(b"\xd0\xcf\x11\xe0", "offer.xls")
    with pytest.raises(MA.AttachmentError, match="isn't a document"):
        MA.extract_text(b"\x89PNG", "logo.png")
    with pytest.raises(MA.AttachmentError, match="too big"):
        MA.extract_text(b"0" * (MA.MAX_FILE_BYTES + 1), "drawings.pdf")


def test_picking_the_attachment_asked_for():
    names = ["logo.png", "QU_683133_2.pdf", "terms.pdf"]
    assert MA.pick(names, "QU_683133_2.pdf") == 1
    assert MA.pick(names, "qu_683133_2") == 1                 # close enough
    assert MA.pick(["image001.png", "offer.pdf"], "") == 1     # the only document
    with pytest.raises(MA.AttachmentError, match="This email has: logo.png"):
        MA.pick(names, "")                                      # two documents: say which
    with pytest.raises(MA.AttachmentError, match="no attachments"):
        MA.pick([], "x")


def test_ask_opens_an_attachment_and_credits_its_email():
    import ask_agent as A

    class Mail:
        def search(self, q, limit=10):
            return {"total": 1, "results": [{"id": "77", "thread": "7", "date": "22 Oct 2026 15:24",
                                             "from": "Benoit Labre", "to": "spm", "subject": "Offer",
                                             "snippet": "Please find attached", "attachments": ["QU.pdf"]}]}

        def attachment(self, mid, name):
            assert (mid, name) == ("77", "QU.pdf")
            return {"name": "QU.pdf", "kind": "pdf", "text": "36IN 300LB CGI 410.00", "pages": 2, "cut": False}

    refs = A._Refs()
    A.execute("search_email", {"query": "flexitallic"}, refs, mail=Mail())
    text, label, detail = A.execute("read_attachment", {"id": "77", "filename": "QU.pdf"}, refs, mail=Mail())
    assert 'Attachment "QU.pdf" of email [1]' in text and "410.00" in text
    assert label == "Opening “QU.pdf”" and detail == "PDF · 2 pages"
    assert any(t["name"] == "read_attachment" for t in A.EMAIL_TOOLS)

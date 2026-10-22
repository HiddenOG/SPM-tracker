"""
mail_attachments.py — the text inside an email attachment, for SPM Ask.

Suppliers send their prices as files: Flexitallic's offers arrive as PDF or
Excel attachments, so an email's own words often say only "please find
attached". extract_text() turns one downloaded file into plain text Ask can
read. Both mail routes (gmail_api.py and the IMAP code in mail_search.py) use
it, so the text is the same whichever fetched the file.

Read only, and capped: a big file is cut rather than read whole, and a scanned
PDF with no text layer says so instead of returning nothing.
"""
from __future__ import annotations

import io
import os
import re
import zipfile

MAX_FILE_BYTES = 15_000_000     # bigger than this is a drawing or a scan pack, not an offer
MAX_PAGES = 20                  # of a PDF
MAX_ROWS = 400                  # per spreadsheet sheet
MAX_CHARS = 20_000              # of text handed to the model
OCR_PAGES = 3                   # of a scanned PDF - each takes a few seconds


def _ocr_pdf(data: bytes) -> str:
    """Read a scanned PDF's first pages by text recognition (Tesseract), the
    same engine the PO extractor falls back on. Empty if it isn't installed."""
    import shutil
    try:
        import fitz
        import pytesseract
        from PIL import Image
    except ImportError:
        return ""
    pytesseract.pytesseract.tesseract_cmd = (
        shutil.which("tesseract") or r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    pages = []
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        for i in range(min(doc.page_count, OCR_PAGES)):
            pix = doc[i].get_pixmap(matrix=fitz.Matrix(2, 2))
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            pages.append(f"--- page {i + 1} (read from a scan) ---\n" + pytesseract.image_to_string(img).strip())
    except Exception:
        return ""
    return "\n\n".join(pages)


class AttachmentError(Exception):
    """The attachment couldn't be found, fetched or read."""


def _kind(filename: str, mime: str) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    mime = (mime or "").lower()
    if ext == ".pdf" or mime == "application/pdf":
        return "pdf"
    if ext in (".xlsx", ".xlsm") or "spreadsheetml" in mime:
        return "xlsx"
    if ext == ".xls" or mime == "application/vnd.ms-excel":
        return "xls"
    if ext == ".docx" or "wordprocessingml" in mime:
        return "docx"
    if ext in (".csv", ".txt") or mime.startswith("text/"):
        return "text"
    return "other"


def _pdf(data: bytes) -> tuple[str, int]:
    pages = []
    total = 0
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            total = len(pdf.pages)
            for i, page in enumerate(pdf.pages[:MAX_PAGES], start=1):
                # Tables keep their rows together, which matters for price lines.
                parts = []
                for table in page.extract_tables() or []:
                    for row in table:
                        cells = [str(c).replace("\n", " ").strip() for c in row if c not in (None, "")]
                        if cells:
                            parts.append(" | ".join(cells))
                text = page.extract_text() or ""
                body = text if not parts else text + "\n[table rows]\n" + "\n".join(parts)
                pages.append(f"--- page {i} ---\n{body.strip()}")
    except Exception:
        pages = []
    if not "".join(pages).replace("-", "").strip():
        # pdfplumber trips on some files; PyMuPDF reads most of them.
        try:
            import fitz
            doc = fitz.open(stream=data, filetype="pdf")
            total = doc.page_count
            pages = [f"--- page {i} ---\n{doc[i - 1].get_text().strip()}"
                     for i in range(1, min(total, MAX_PAGES) + 1)]
        except Exception as exc:
            raise AttachmentError(f"This PDF couldn't be read: {exc}") from exc
    text = "\n\n".join(pages)
    if not re.sub(r"--- page \d+ ---|\s", "", text):
        # No text layer: a scan. Read it by text recognition instead.
        text = _ocr_pdf(data)
    return text, total


def _xlsx(data: bytes) -> str:
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise AttachmentError(f"This spreadsheet couldn't be read: {exc}") from exc
    out = []
    for ws in wb.worksheets:
        rows = []
        for n, row in enumerate(ws.iter_rows(values_only=True)):
            if n >= MAX_ROWS:
                rows.append(f"(sheet cut at {MAX_ROWS} rows)")
                break
            cells = [str(c).strip() for c in row if c not in (None, "")]
            if cells:
                rows.append(" | ".join(cells))
        if rows:
            out.append(f"--- sheet {ws.title} ---\n" + "\n".join(rows))
    wb.close()
    return "\n\n".join(out)


def _docx(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except Exception as exc:
        raise AttachmentError(f"This Word file couldn't be read: {exc}") from exc
    xml = re.sub(r"</w:p>|</w:tr>", "\n", xml)
    xml = re.sub(r"</w:tc>", " | ", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    import html
    return html.unescape(text)


def extract_text(data: bytes, filename: str, mime: str = "") -> dict:
    """The readable text of one attachment.

    Returns {"name", "kind", "text", "pages" (PDFs), "cut" (bool)}.
    Raises AttachmentError when the file can't be read at all.
    """
    if len(data) > MAX_FILE_BYTES:
        raise AttachmentError(f"{filename} is {len(data) / 1e6:.0f} MB - too big to read here.")
    kind = _kind(filename, mime)
    pages = None
    if kind == "pdf":
        text, pages = _pdf(data)
    elif kind == "xlsx":
        text = _xlsx(data)
    elif kind == "docx":
        text = _docx(data)
    elif kind == "text":
        text = data.decode("utf-8", "replace")
    elif kind == "xls":
        raise AttachmentError(f"{filename} is an old-style .xls spreadsheet, which can't be read here. "
                              "Open it in Excel, or ask the sender for .xlsx or PDF.")
    else:
        raise AttachmentError(f"{filename} isn't a document Ask can read (PDF, Excel, Word or text).")

    text = re.sub(r"[ \t]+", " ", text or "")
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
    if not text.replace("-", "").replace("page", "").strip(" 0123456789\n"):
        text = ("(No text could be taken from this file - it is probably a scanned image. "
                "Open it to read it.)")
    cut = len(text) > MAX_CHARS
    if cut:
        text = text[:MAX_CHARS]
    if pages and pages > MAX_PAGES:
        cut = True
    return {"name": filename, "kind": kind, "text": text, "pages": pages, "cut": cut}


def pick(names: list[str], wanted: str) -> int:
    """Which attachment was asked for: an exact name, else a close one, else
    the only document. Returns its index, or raises naming what's there."""
    if not names:
        raise AttachmentError("That email has no attachments.")
    w = (wanted or "").strip().lower()
    for i, n in enumerate(names):
        if n.lower() == w:
            return i
    if w:
        for i, n in enumerate(names):
            if w in n.lower() or n.lower() in w:
                return i
    docs = [i for i, n in enumerate(names) if _kind(n, "") != "other"]
    if not w and len(docs) == 1:
        return docs[0]
    raise AttachmentError("Say which attachment to open. This email has: " + ", ".join(names))

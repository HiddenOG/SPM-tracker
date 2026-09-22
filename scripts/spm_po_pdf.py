"""
spm_po_pdf.py — Generate the Special Piping Materials purchase order PDF.

This is the document SPM sends to its own suppliers (MetalsDepot, Flexitallic,
Rollstud…). It reproduces the Excel template the office has been copying by
hand — same blocks, same seven line-item columns, same signature footer — and
uses the artwork lifted straight out of that workbook so the printed result is
the document people already recognise:

    data_fl/sig_felix_akarue.png  the manager's signature
    data_fl/sig_ijala_onome.png   the sales person's signature
    data_fl/spm_footer_wave.png   the orange/teal wave along the bottom

The letterhead itself is rebuilt rather than scanned in: the banner inside the
workbook is a photocopy on cream paper and prints grainy, so the header is set
here from the clean logo file and live type.

Signatures are stamped by NAME, not by whoever is logged in: put a different
manager on the order and the line is left blank to be signed by hand, which is
the safe way round. Adding someone means adding their file to _SIGNATURES.

Usage:
    from spm_po_pdf import generate_spm_po_pdf
    pdf_bytes = generate_spm_po_pdf(po_dict, line_items_list)
"""

from __future__ import annotations

import io
import re
from datetime import datetime
from html import escape as _html_escape
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable, Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer,
    Table, TableStyle,
)

ROOT = Path(__file__).parent.parent
ART = ROOT / "data_fl"

# Printed at 17mm. The full-size source is 450KB, which every generated PO
# would otherwise carry; this copy is sized for the page.
LOGO = ART / "spm_logo.png"
FOOTER_WAVE = ART / "spm_footer_wave.png"

# Signature images, keyed by the name as it is typed on the form. Matching is
# on letters only, so "FELIX AKARUE", "Felix Akarue" and "felix  akarue" all
# find the same file; anything unrecognised prints an empty rule to sign.
_SIGNATURES = {
    "felixakarue": ART / "sig_felix_akarue.png",
    "ijalastylesonome": ART / "sig_ijala_onome.png",
}

COMPANY_NAME = "Special Piping Materials (Nig.) Ltd."
COMPANY_TAGLINE = "MANUFACTURING  ·  CONTRACT  ·  PROCUREMENT  ·  SUPPLY SERVICES"
COMPANY_ADDRESS = (
    "KM3, N.P.A. EXPRESSWAY BY THE REFINERY FLY OVER,<br/>"
    "EKPAN - WARRI, DELTA STATE, NIGERIA."
)
COMPANY_CONTACT = (
    "Phone: 08102621418<br/>"
    "Website: www.specialpipingltd.com"
)

PRIMARY = colors.HexColor("#8C1C1C")      # SPM maroon, same as the quotation
DARK = colors.HexColor("#1C2235")
BAND = colors.HexColor("#F1EDED")
RULE = colors.HexColor("#B9B1B1")
HAIR = colors.HexColor("#D8D2D2")

_CURRENCY_SYMBOLS = {"USD": "$", "GBP": "£", "EUR": "€", "NGN": "₦"}

# Helvetica has no naira glyph and ReportLab silently prints "I" in its place,
# so NGN orders need a Unicode face. Same problem, same fix as quotation_pdf.
_UNICODE_FONT = None


def _register_unicode_font() -> None:
    global _UNICODE_FONT
    candidates = [
        ART / "fonts" / "DejaVuSans.ttf",
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
    ]
    for path in candidates:
        try:
            if not Path(path).exists():
                continue
            pdfmetrics.registerFont(TTFont("SPM-PO-Unicode", str(path)))
            _UNICODE_FONT = "SPM-PO-Unicode"
            return
        except Exception:
            pass


_register_unicode_font()


def _fix_naira(text: str) -> str:
    if "₦" not in text or not _UNICODE_FONT:
        return text
    return text.replace("₦", f'<font name="{_UNICODE_FONT}">₦</font>')


def _sty(name: str, **kw) -> ParagraphStyle:
    base = {
        "fontName": "Helvetica",
        "fontSize": 8.5,
        "textColor": DARK,
        "leading": 11.5,
        "spaceAfter": 0,
        "spaceBefore": 0,
    }
    base.update(kw)
    return ParagraphStyle(name, **base)


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    """A Paragraph that degrades to plain text instead of taking the PDF down.

    Every field on this form is free text typed by hand, so a stray '<' in a
    description ('<1/2" NPT') would otherwise raise inside ReportLab's XML
    parser and turn the whole download into a 500.
    """
    try:
        return Paragraph(_fix_naira(text), style)
    except Exception:
        plain = _html_escape(re.sub(r"<[^>]*>", "", str(text or "")))
        try:
            return Paragraph(_fix_naira(plain), style)
        except Exception:
            return Paragraph("", style)


def _esc_lines(text: str) -> str:
    """Escape user text and keep its line breaks.

    For the plain fields — the vendor address, the delivery block, the notes —
    which are typed into ordinary boxes and carry no markup.
    """
    return _html_escape(str(text or "")).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")


def _description(value: str) -> str:
    """A line-item description, ready for ReportLab.

    The description cell is the same rich-text editor as the quotation's, so
    the value can carry bold, italic, underline and colour. quotation_pdf
    already knows how to turn that into Paragraph markup and has been hardened
    against every shape a paste from Word produces — stray colours ReportLab
    rejects, tags left open by a part-selected paste — so it does the work here
    too, rather than a second copy of the same rules drifting away from it.

    Plain text still arrives from older rows and from imports. It takes the
    escaping path instead, which keeps its line breaks.
    """
    text = str(value or "")
    if "<" not in text and "&" not in text:
        return _esc_lines(text)
    try:
        from quotation_pdf import _html_to_rl
        return _html_to_rl(text)
    except Exception:
        return _esc_lines(re.sub(r"<[^>]*>", "", text))


def _fmt_date(value: str) -> str:
    """dd/mm/yyyy, the format the template uses. Unparseable text passes through."""
    if not value:
        return ""
    raw = str(value)[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return str(value)


def _money(value, symbol: str) -> str:
    try:
        return f"{symbol}{float(value or 0):,.2f}"
    except Exception:
        return f"{symbol}0.00"


def _qty(value) -> str:
    """Whole numbers print whole — '3', not '3.0000'."""
    try:
        num = float(value or 0)
    except Exception:
        return str(value or "")
    return str(int(num)) if num == int(num) else f"{num:g}"


def _fit_size(text: str, font: str, max_width: float,
              start: float = 12.0, floor: float = 6.5) -> float:
    """The largest size at which `text` still fits on one line.

    The PO number is one unbroken token — 'S.P.M.-C.N.L.-3131-0061451715-…' —
    and an order covering five client POs runs past eighty characters. Wrapped,
    it breaks mid-number, which is worse than small: '…-0061451710-0' on one
    line and '61448744-…' on the next is a PO number nobody can read back.
    """
    size = start
    try:
        while size > floor and pdfmetrics.stringWidth(text, font, size) > max_width:
            size -= 0.25
    except Exception:
        return floor
    return size


def _signature_for(name: str) -> Path | None:
    key = re.sub(r"[^a-z]", "", str(name or "").lower())
    path = _SIGNATURES.get(key)
    return path if path and path.exists() else None


def _image(path: Path, width: float) -> Image | None:
    """Scale an image to a width, keeping its aspect ratio."""
    try:
        from reportlab.lib.utils import ImageReader
        iw, ih = ImageReader(str(path)).getSize()
        return Image(str(path), width=width, height=width * ih / iw)
    except Exception:
        return None


def generate_spm_po_pdf(po: dict, line_items: list[dict]) -> bytes:
    buf = io.BytesIO()

    page_w, _ = A4
    lm = rm = 14 * mm
    use_w = page_w - lm - rm

    currency = (po.get("currency") or "USD").upper()
    symbol = _CURRENCY_SYMBOLS.get(currency, "$")
    po_number = po.get("po_number") or "—"

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=lm, rightMargin=rm,
        topMargin=12 * mm, bottomMargin=32 * mm,
        title=f"Purchase Order {po_number}",
        author=COMPANY_NAME,
    )

    addr = _sty("addr", fontSize=7.5, leading=10.5, textColor=colors.HexColor("#555555"))
    po_word = _sty("po_word", fontName="Helvetica-Bold", fontSize=17, textColor=PRIMARY,
                   alignment=TA_RIGHT, leading=20)
    meta_lbl = _sty("meta_lbl", fontSize=7.5, textColor=colors.grey, alignment=TA_RIGHT)
    meta_val = _sty("meta_val", fontName="Helvetica-Bold", fontSize=10, textColor=DARK,
                    alignment=TA_RIGHT, leading=13)
    band = _sty("band", fontName="Helvetica-Bold", fontSize=7.5, textColor=PRIMARY)
    key = _sty("key", fontSize=7, textColor=colors.grey, leading=9.5)
    val = _sty("val", fontSize=8.5, textColor=DARK, leading=11.5)
    th = _sty("th", fontName="Helvetica-Bold", fontSize=7.5, textColor=colors.white)
    th_c = _sty("th_c", fontName="Helvetica-Bold", fontSize=7.5, textColor=colors.white,
                alignment=TA_CENTER)
    th_r = _sty("th_r", fontName="Helvetica-Bold", fontSize=7.5, textColor=colors.white,
                alignment=TA_RIGHT)
    td = _sty("td", fontSize=8, textColor=DARK, leading=11)
    td_c = _sty("td_c", fontSize=8, textColor=DARK, alignment=TA_CENTER)
    td_r = _sty("td_r", fontSize=8, textColor=DARK, alignment=TA_RIGHT)
    tot_l = _sty("tot_l", fontName="Helvetica-Bold", fontSize=9.5, textColor=DARK,
                 alignment=TA_RIGHT)
    tot_r = _sty("tot_r", fontName="Helvetica-Bold", fontSize=10.5, textColor=PRIMARY,
                 alignment=TA_RIGHT)
    sign_lbl = _sty("sign_lbl", fontSize=8, textColor=DARK)
    note_sty = _sty("note", fontSize=8, textColor=DARK, leading=11)

    story: list[Any] = []

    # ── 1. Letterhead + PO number ────────────────────────────────────────────
    co_name = _sty("co_name", fontName="Helvetica-Bold", fontSize=14.5, textColor=PRIMARY,
                   leading=17)
    co_tag = _sty("co_tag", fontName="Helvetica-Bold", fontSize=5.8, textColor=DARK,
                  leading=9)

    left: list[Any] = [_p(COMPANY_NAME, co_name), _p(COMPANY_TAGLINE, co_tag),
                       Spacer(1, 2 * mm),
                       _p(COMPANY_ADDRESS, addr), _p(COMPANY_CONTACT, addr)]
    logo = _image(LOGO, 17 * mm) if LOGO.exists() else None
    if logo is not None:
        inner = Table([[logo, left]], colWidths=[19 * mm, use_w * 0.58 - 19 * mm])
        inner.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (0, 0), 0),
            ("RIGHTPADDING", (0, 0), (0, 0), 4),
            ("LEFTPADDING", (1, 0), (1, 0), 0),
            ("RIGHTPADDING", (1, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        left = [inner]

    head = Table(
        [[
            left,
            [_p("PURCHASE ORDER", po_word),
             Spacer(1, 2 * mm),
             _p("DATE", meta_lbl),
             _p(_fmt_date(po.get("po_date")) or "—", meta_val)],
        ]],
        colWidths=[use_w * 0.58, use_w * 0.42],
    )
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(head)
    story.append(HRFlowable(width="100%", thickness=1.4, color=PRIMARY,
                            spaceBefore=3 * mm, spaceAfter=3 * mm))

    # The number spans the page and shrinks to fit rather than wrapping: it is
    # the one string on the sheet that must be readable back character for
    # character, and it is the widest.
    num_w = use_w - 30 * mm
    num_sty = _sty("po_num", fontName="Helvetica-Bold",
                   fontSize=_fit_size(po_number, "Helvetica-Bold", num_w),
                   textColor=DARK, alignment=TA_RIGHT, leading=14)
    po_band = Table(
        [[_p("P.O. NUMBER", key), _p(_html_escape(po_number), num_sty)]],
        colWidths=[27 * mm, use_w - 27 * mm],
    )
    po_band.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BAND),
        ("BOX", (0, 0), (-1, -1), 0.6, RULE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(po_band)
    story.append(Spacer(1, 3.5 * mm))

    # ── 2. Vendor + Deliver to ───────────────────────────────────────────────
    def field(label: str, value: str):
        return [_p(label, key), _p(_esc_lines(value) or "—", val), Spacer(1, 1.2 * mm)]

    vendor_cell: list[Any] = []
    who = " · ".join(x for x in [po.get("vendor_name"), po.get("vendor_role")] if x)
    vendor_cell += field("CONTACT", who)
    vendor_cell += field("COMPANY", po.get("vendor_company") or "")
    vendor_cell += field("ADDRESS", po.get("vendor_address") or "")
    vendor_cell += field("EMAIL", po.get("vendor_email") or "")

    deliver_cell = field("DELIVER TO", po.get("deliver_to") or "")

    parties = Table(
        [[_p("VENDOR", band), _p("DELIVERY", band)],
         [vendor_cell, deliver_cell]],
        colWidths=[use_w * 0.55, use_w * 0.45],
    )
    parties.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
        ("BOX", (0, 0), (-1, -1), 0.6, RULE),
        ("INNERGRID", (0, 0), (-1, -1), 0.6, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(parties)

    # ── 3. Shipping row ──────────────────────────────────────────────────────
    ship = Table(
        [[_p("SHIPPING TERMS", key), _p("SHIPPING METHOD", key), _p("DELIVERY DATE", key)],
         [_p(_html_escape(po.get("shipping_terms") or "—"), val),
          _p(_html_escape(po.get("shipping_method") or "—"), val),
          _p(_html_escape(po.get("delivery_date") or "—"), val)]],
        colWidths=[use_w / 3.0] * 3,
    )
    ship.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.6, RULE),
        ("INNERGRID", (0, 0), (-1, -1), 0.6, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(ship)
    story.append(Spacer(1, 4 * mm))

    # ── 4. Line items ────────────────────────────────────────────────────────
    # Same seven columns as the workbook, in the same order and roughly the
    # same proportions as its column widths.
    col_w = [use_w * f for f in (0.075, 0.13, 0.115, 0.36, 0.06, 0.12, 0.14)]
    rows: list[list[Any]] = [[
        _p("ITEM", th_c), _p("P.O. No.", th), _p("QUOTE REF", th),
        _p("DESCRIPTION", th), _p("QTY", th_c), _p("UNIT PRICE", th_r), _p("TOTAL", th_r),
    ]]

    total = 0.0
    for idx, item in enumerate(line_items or []):
        try:
            line_total = float(item.get("line_total") or 0)
        except Exception:
            line_total = 0.0
        total += line_total
        rows.append([
            _p(str(item.get("item_no") or idx + 1), td_c),
            _p(_html_escape(str(item.get("client_po_no") or "")), td),
            _p(_html_escape(str(item.get("quote_ref") or "")), td),
            _p(_description(item.get("description")), td),
            _p(_qty(item.get("quantity")), td_c),
            _p(_money(item.get("unit_price"), symbol), td_r),
            _p(_money(line_total, symbol), td_r),
        ])

    if len(rows) == 1:
        rows.append([_p("", td)] * 7)

    # splitInRow lets a single row break across pages. Without it, one line
    # item whose description is taller than the page raises LayoutError and
    # the whole download dies with a 500 — and a description is free text, so
    # nothing stops someone pasting a full material specification into one.
    items_tbl = Table(rows, colWidths=col_w, repeatRows=1,
                      splitByRow=1, splitInRow=1)
    items_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.6, RULE),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, HAIR),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, PRIMARY),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FBF9F9")]),
    ]))
    story.append(items_tbl)

    # The sum of the lines wins, always. A document whose TOTAL disagrees with
    # the rows printed directly above it is the kind of thing that turns into
    # an argument with a supplier, and the rows are the part both sides can
    # check. The stored header total only stands in when there are no lines to
    # add up — an order saved before anything was priced.
    if line_items:
        grand = total
    else:
        try:
            grand = float(po.get("total") or 0)
        except (TypeError, ValueError):
            grand = 0.0

    tot_tbl = Table(
        [[_p("TOTAL", tot_l), _p(_money(grand, symbol), tot_r)]],
        colWidths=[sum(col_w[:6]), col_w[6]],
    )
    tot_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BAND),
        ("BOX", (0, 0), (-1, -1), 0.6, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(tot_tbl)

    # ── 5. Notes ─────────────────────────────────────────────────────────────
    if (po.get("notes") or "").strip():
        story.append(Spacer(1, 4 * mm))
        story.append(_p("NOTES", key))
        story.append(_p(_esc_lines(po.get("notes")), note_sty))

    # ── 6. Signatures ────────────────────────────────────────────────────────
    # Kept together so the manager's name never lands on a page of its own.
    story.append(Spacer(1, 7 * mm))

    manager = po.get("manager") or ""
    sales = po.get("sales_person") or ""
    dated = _fmt_date(po.get("po_date"))

    def _sig_image(name: str) -> Image | None:
        """The signature scaled by HEIGHT, so a tall hand and a wide one come
        out the same visual weight and both rules below them line up."""
        path = _signature_for(name)
        if not path:
            return None
        try:
            from reportlab.lib.utils import ImageReader
            iw, ih = ImageReader(str(path)).getSize()
            height = 12 * mm
            width = height * iw / ih
            if width > 46 * mm:                 # a very wide hand is capped instead
                width, height = 46 * mm, 46 * mm * ih / iw
            return Image(str(path), width=width, height=height)
        except Exception:
            return None

    def sign_block(role: str, name: str) -> list[Any]:
        # The signature sits in a fixed-height, bottom-aligned box. Dropping it
        # straight into the cell made the rule under it follow the image height,
        # so the two blocks signed off at different heights on the page.
        stamped = _sig_image(name)
        box = Table([[stamped if stamped is not None else ""]],
                    colWidths=[use_w * 0.46], rowHeights=[13 * mm])
        box.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ]))
        return [
            _p(f"{role}: <b>{_html_escape(name or '—')}</b>", sign_lbl),
            box,
            HRFlowable(width=use_w * 0.42, thickness=0.7, color=RULE,
                       spaceBefore=0, spaceAfter=2),
            _p(f"SIGN &nbsp;&nbsp;·&nbsp;&nbsp; DATE: {_html_escape(dated) or '—'}",
               _sty("sd", fontSize=7.5, textColor=colors.grey)),
        ]

    sign_tbl = Table(
        [[sign_block("MANAGER", manager), sign_block("SALES PERSON", sales)]],
        colWidths=[use_w * 0.5, use_w * 0.5],
    )
    sign_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(KeepTogether(sign_tbl))

    # ── 7. Footer wave ───────────────────────────────────────────────────────
    def _footer(canvas, doc_):
        if not FOOTER_WAVE.exists():
            return
        try:
            from reportlab.lib.utils import ImageReader
            art = ImageReader(str(FOOTER_WAVE))
            iw, ih = art.getSize()
            width = page_w
            canvas.drawImage(art, 0, 0, width=width, height=width * ih / iw,
                             mask="auto")
        except Exception:
            pass

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()

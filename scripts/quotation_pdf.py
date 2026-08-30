"""
quotation_pdf.py — Generate a professional Brobags-style PDF quotation.

Usage:
    from quotation_pdf import generate_quotation_pdf
    pdf_bytes = generate_quotation_pdf(quotation_dict, line_items_list)
"""

from __future__ import annotations
import io
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph,
    Spacer, HRFlowable, Image, KeepTogether,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER, TA_JUSTIFY
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

ROOT = Path(__file__).parent.parent

# Register a Unicode-capable font so characters like ₦ render correctly.
# Falls back to Helvetica if no suitable TTF is found (symbol will be missing).
_FONT      = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"

def _register_unicode_fonts() -> None:
    global _FONT, _FONT_BOLD
    candidates = [
        # Windows
        ("C:/Windows/Fonts/arial.ttf",      "C:/Windows/Fonts/arialbd.ttf"),
        # Linux (Liberation Sans — metrically identical to Arial)
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        # Linux (DejaVu)
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    for reg_path, bold_path in candidates:
        if not Path(reg_path).exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont("SPM-Regular", reg_path))
            _FONT = "SPM-Regular"
            if Path(bold_path).exists():
                pdfmetrics.registerFont(TTFont("SPM-Bold", bold_path))
                _FONT_BOLD = "SPM-Bold"
                pdfmetrics.registerFontFamily("SPM", normal="SPM-Regular", bold="SPM-Bold",
                                              italic="SPM-Regular", boldItalic="SPM-Bold")
            break
        except Exception:
            pass

_register_unicode_fonts()


def _html_to_rl(html: str) -> str:
    """Convert browser-generated bold/colour HTML to ReportLab Paragraph markup.

    Browser produces <b>, <strong>, <span style="color:..."> or <font color="...">.
    ReportLab Paragraph supports <b>, <i>, <font color="...">, <br/>.
    """
    if not html:
        return ""
    # <strong> → <b>, <em> → <i>
    html = re.sub(r"<strong[^>]*>", "<b>", html, flags=re.IGNORECASE)
    html = re.sub(r"</strong>", "</b>", html, flags=re.IGNORECASE)
    html = re.sub(r"<em[^>]*>", "<i>", html, flags=re.IGNORECASE)
    html = re.sub(r"</em>", "</i>", html, flags=re.IGNORECASE)
    # <br> → newline character ReportLab will wrap
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    # <span style="color: #hex"> or <span style="color: rgb(r,g,b)"> → <font color="...">
    # Use a stack so </font> is only emitted when the matching <span> was actually converted;
    # spans with unrecognized/missing colour are dropped (open AND close) to avoid orphan </font>.
    def _convert_spans(src: str) -> str:
        stack: list[bool] = []  # True = emitted <font>, False = span was dropped
        out: list[str] = []
        for part in re.split(r"(</?span[^>]*>)", src, flags=re.IGNORECASE):
            open_m = re.match(r"<span([^>]*)>", part, re.IGNORECASE)
            if open_m:
                attrs = open_m.group(1)
                style_m = re.search(r'style\s*=\s*"([^"]*)"', attrs, re.IGNORECASE)
                font_tag = None
                if style_m:
                    style = style_m.group(1)
                    hx = re.search(r"color\s*:\s*(#[0-9a-fA-F]{3,8})", style, re.IGNORECASE)
                    if hx:
                        font_tag = f'<font color="{hx.group(1)}">'
                    else:
                        rgb = re.search(r"color\s*:\s*rgb\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", style, re.IGNORECASE)
                        if rgb:
                            rv, gv, bv = int(rgb.group(1)), int(rgb.group(2)), int(rgb.group(3))
                            font_tag = f'<font color="#{rv:02x}{gv:02x}{bv:02x}">'
                if font_tag:
                    out.append(font_tag)
                    stack.append(True)
                else:
                    stack.append(False)
            elif re.match(r"</span>", part, re.IGNORECASE):
                if stack and stack.pop():
                    out.append("</font>")
                # else: matching open was dropped — discard this close tag too
            else:
                out.append(part)
        return "".join(out)
    html = _convert_spans(html)
    # Strip any remaining unsupported tags (div, p, etc.)
    html = re.sub(r"<(?!/?\s*(?:b|i|u|font|br)[\s/>])[^>]+>", "", html, flags=re.IGNORECASE)
    return html.strip()
LOGO_PATH = ROOT / "data_fl" / "Gemini_Generated_Image_z6jc3vz6jc3vz6jc.png"

SPM_RED   = colors.HexColor("#8C1C1C")
SPM_DARK  = colors.HexColor("#1C2235")
SPM_GREY  = colors.HexColor("#F4F6FC")
SPM_BDRY  = colors.HexColor("#DDD9D3")
WHITE     = colors.white
BLACK     = colors.black
LGREY     = colors.HexColor("#FAFAFA")


def _fmt_date(s: str) -> str:
    if not s:
        return ""
    try:
        d = datetime.strptime(s[:10], "%Y-%m-%d")
        return f"{d.day} {d.strftime('%B, %Y')}"
    except Exception:
        return s


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(text, style)


def generate_quotation_pdf(quotation: dict, line_items: list[dict]) -> bytes:
    buf = io.BytesIO()

    PAGE_W, PAGE_H = A4
    LM = RM = 15 * mm
    TM = BM = 15 * mm
    USE_W = PAGE_W - LM - RM

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=LM, rightMargin=RM,
        topMargin=TM, bottomMargin=BM,
        title=f"Quotation {quotation.get('quote_number', '')}",
        author="Special Piping Materials (Nig.) Ltd.",
    )

    # ── Styles ────────────────────────────────────────────────────────────────
    def sty(name="body", **kw) -> ParagraphStyle:
        base = {
            "fontName":        _FONT,
            "fontSize":        8.5,
            "textColor":       SPM_DARK,
            "leading":         12,
            "spaceAfter":      0,
            "spaceBefore":     0,
            "leftIndent":      0,
            "rightIndent":     0,
            "firstLineIndent": 0,
        }
        base.update(kw)
        return ParagraphStyle(name, **base)

    co_name = sty("co_name", fontName=_FONT_BOLD, fontSize=12, textColor=SPM_RED, leading=15)
    co_tag  = sty("co_tag",  fontSize=6.5, textColor=colors.grey, letterSpacing=0.4)
    co_addr = sty("co_addr", fontSize=7.5, textColor=colors.HexColor("#666666"), leading=11)
    q_word  = sty("q_word",  fontName=_FONT_BOLD, fontSize=22, textColor=SPM_RED,
                  alignment=TA_RIGHT, leading=26)
    q_num   = sty("q_num",   fontSize=7.5, textColor=colors.grey, alignment=TA_RIGHT)
    m_lbl   = sty("m_lbl",   fontSize=7,   textColor=colors.grey)
    m_val   = sty("m_val",   fontName=_FONT_BOLD, fontSize=8.5, textColor=SPM_DARK)
    b_lbl   = sty("b_lbl",   fontName=_FONT_BOLD, fontSize=7.5, textColor=colors.grey,
                  letterSpacing=0.4, spaceAfter=2)
    b_name  = sty("b_name",  fontName=_FONT_BOLD, fontSize=10, textColor=SPM_DARK, spaceAfter=1)
    b_addr  = sty("b_addr",  fontSize=8.5, textColor=SPM_DARK, leading=13, alignment=TA_JUSTIFY)
    body    = sty("body_",   fontSize=8.5, textColor=SPM_DARK, leading=12,  alignment=TA_JUSTIFY)
    small   = sty("small_",  fontSize=7.5, textColor=colors.HexColor("#777777"), leading=10)
    tc_hd   = sty("tc_hd",   fontName=_FONT_BOLD, fontSize=8.5, textColor=SPM_RED,
                  spaceBefore=2, spaceAfter=3)
    tc_bod  = sty("tc_bod",  fontSize=8, textColor=SPM_DARK, leading=12,   alignment=TA_JUSTIFY)
    foot    = sty("foot_",   fontSize=7.5, textColor=colors.grey, alignment=TA_JUSTIFY,
                  fontName=_FONT)
    aff     = sty("aff_",    fontSize=6.5, textColor=colors.grey, alignment=TA_CENTER)
    th_cell = sty("th_",     fontName=_FONT_BOLD, fontSize=8, textColor=WHITE)
    th_r    = sty("th_r",    fontName=_FONT_BOLD, fontSize=8, textColor=WHITE,
                  alignment=TA_RIGHT)
    th_c    = sty("th_c",    fontName=_FONT_BOLD, fontSize=8, textColor=WHITE,
                  alignment=TA_CENTER)
    td_c    = sty("td_c",    fontSize=8.5, textColor=SPM_DARK, alignment=TA_CENTER)
    td_r    = sty("td_r",    fontSize=8.5, textColor=SPM_DARK, alignment=TA_RIGHT)
    td_s    = sty("td_s",    fontSize=7.5, textColor=colors.grey, alignment=TA_CENTER)
    td_mfr  = sty("td_mfr",  fontSize=7, textColor=colors.grey, leading=10,
                  fontName=_FONT)
    td_desc = sty("td_desc", fontSize=8, textColor=SPM_DARK, leading=11)
    tot_l   = sty("tot_l",   fontSize=9, textColor=SPM_DARK)
    tot_r   = sty("tot_r",   fontSize=9, textColor=SPM_DARK, alignment=TA_RIGHT)
    grand_l = sty("grand_l", fontName=_FONT_BOLD, fontSize=11, textColor=SPM_RED)
    grand_r = sty("grand_r", fontName=_FONT_BOLD, fontSize=11, textColor=SPM_RED,
                  alignment=TA_RIGHT)
    sig_lbl = sty("sig_lbl", fontSize=7.5, textColor=colors.grey)

    story: list[Any] = []

    # ── 1. Header ─────────────────────────────────────────────────────────────
    logo_cell: list[Any] = []
    if LOGO_PATH.exists():
        try:
            logo_cell.append(Image(str(LOGO_PATH), width=28 * mm, height=28 * mm))
        except Exception:
            pass
    logo_cell += [
        _p("Special Piping Materials (Nig.) Ltd.", co_name),
        _p("MANUFACTURING  ·  CONTRACT  ·  PROCUREMENT  ·  SUPPLY SERVICES", co_tag),
        _p(
            "KM3, NPA Expressway By Refinery Flyover, P.O Box 2004, Warri, Delta State, Nigeria<br/>"
            "Tel: +234 8102621418  |  enquiry@specialpipingltd.com  |  www.specialpipingltd.com",
            co_addr,
        ),
    ]

    qn = quotation.get("quote_number") or "—"
    right_parts = [_p("QUOTATION", q_word), _p(f"Our Ref:  {qn}", q_num)]

    hdr_tbl = Table([[logo_cell, right_parts]], colWidths=[USE_W * 0.62, USE_W * 0.38])
    hdr_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN",  (1, 0), (1, 0),   "RIGHT"),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ("TOPPADDING",    (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(hdr_tbl)
    story.append(HRFlowable(width="100%", thickness=1.5, color=SPM_RED,
                            spaceBefore=4 * mm, spaceAfter=4 * mm))

    # ── 2. Meta grid ─────────────────────────────────────────────────────────
    qdate    = _fmt_date(quotation.get("quote_date", "") or "")
    validity = int(quotation.get("validity_days") or 30)
    expiry   = ""
    try:
        d0    = datetime.strptime((quotation.get("quote_date") or "")[:10], "%Y-%m-%d")
        expiry = _fmt_date((d0 + timedelta(days=validity)).strftime("%Y-%m-%d"))
    except Exception:
        pass

    def meta(lbl, val):
        return [_p(lbl.upper(), m_lbl), _p(str(val) if val else "—", m_val)]

    meta_rows = [
        [meta("Quote Date",       qdate),
         meta("Prepared By",      quotation.get("prepared_by", ""))],
        [meta("Expiry Date",      expiry),
         meta("Lead Time",        quotation.get("lead_time", ""))],
        [meta("Reference REQ #",  quotation.get("reference_po", "")),
         meta("Country of Import", quotation.get("country_import", ""))],
        [meta("Mode of Shipping", quotation.get("shipping_mode", "")),
         meta("Offer Validity",   f"{validity} Days")],
    ]
    meta_tbl = Table(meta_rows, colWidths=[USE_W * 0.5, USE_W * 0.5])
    meta_tbl.setStyle(TableStyle([
        ("GRID",          (0, 0), (-1, -1), 0.5, SPM_BDRY),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(meta_tbl)
    story.append(Spacer(1, 5 * mm))

    # ── 3. Bill To ───────────────────────────────────────────────────────────
    story.append(_p("BILL TO", b_lbl))
    if quotation.get("recipient_company"):
        story.append(_p(quotation["recipient_company"], b_name))
    parts = " — ".join(filter(None, [
        quotation.get("recipient_name", ""), quotation.get("recipient_dept", ""),
    ]))
    if parts:
        story.append(_p(parts, b_addr))
    if quotation.get("recipient_email"):
        story.append(_p(quotation["recipient_email"], b_addr))
    if quotation.get("recipient_tel"):
        story.append(_p("Tel: " + quotation["recipient_tel"], b_addr))
    story.append(Spacer(1, 5 * mm))

    # ── 4. Subject ───────────────────────────────────────────────────────────
    if quotation.get("subject"):
        story.append(_p(f"<b>Subject:</b>  {quotation['subject']}", body))
        story.append(Spacer(1, 5 * mm))

    # ── 5. Line items table ──────────────────────────────────────────────────
    currency = quotation.get("currency") or "USD"
    sym      = "£" if currency == "GBP" else "₦" if currency == "NGN" else "$"

    is_nlng = (quotation.get("client") or "").lower() == "nlng"
    pno_w   = 28 * mm if is_nlng else 0
    col_w   = [9*mm, USE_W - 9*mm - pno_w - 16*mm - 16*mm - 24*mm - 24*mm,
               *([pno_w] if is_nlng else []),
               16*mm, 16*mm, 24*mm, 24*mm]

    hrow = [
        _p("#",                     th_cell),
        _p("Item Description",      th_cell),
        *([_p("Product No.",        th_cell)] if is_nlng else []),
        _p("QTY",                   th_c),
        _p("UOM",                   th_c),
        _p(f"Unit Price ({sym})",   th_r),
        _p(f"Total ({sym})",        th_r),
    ]
    rows = [hrow]
    for item in line_items:
        desc_cell: list[Any] = [_p(_html_to_rl(item.get("description") or ""), td_desc)]
        mfr_parts = " | ".join(filter(None, [
            ("Mfr: " + item["manufacturer"]) if item.get("manufacturer") else "",
            ("P/N: " + item["part_number"])  if item.get("part_number")  else "",
        ]))
        if mfr_parts:
            desc_cell.append(_p(mfr_parts, td_mfr))

        qty   = float(item.get("quantity") or 0)
        upx   = float(item.get("unit_price") or 0)
        total = float(item.get("line_total") or qty * upx)
        rows.append([
            _p(str(item.get("item_no") or ""), td_s),
            desc_cell,
            *([_p(str(item.get("product_no") or ""), td_s)] if is_nlng else []),
            _p(f"{qty:,.2f}",   td_c),
            _p(str(item.get("uom") or "EA"), td_c),
            _p(f"{upx:,.2f}",   td_r),
            _p(f"{total:,.2f}", td_r),
        ])

    items_tbl = Table(rows, colWidths=col_w)
    items_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), SPM_RED),
        ("TEXTCOLOR",     (0, 0), (-1, 0), WHITE),
        ("GRID",          (0, 0), (-1, -1), 0.4, colors.HexColor("#E8E8E8")),
        ("LINEBELOW",     (0, 0), (-1, 0), 1, SPM_RED),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [WHITE, LGREY]),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(items_tbl)
    story.append(Spacer(1, 5 * mm))

    # ── 6. Totals + Words + Signature ────────────────────────────────────────
    sub_total      = float(quotation.get("sub_total") or 0)
    disc_pct       = float(quotation.get("discount_pct") or 0)
    disc_amt       = sub_total * disc_pct / 100
    markup_pct     = float(quotation.get("markup_pct") or 0)
    markup_amt     = sub_total * markup_pct / 100
    markup_visible = quotation.get("markup_visible", True)
    shipping       = float(quotation.get("shipping_charges") or 0)
    grand_total    = float(quotation.get("total") or (sub_total - disc_amt + markup_amt + shipping))

    tot_rows: list[Any] = [
        [_p("Sub Total",   tot_l), _p(f"{sym}{sub_total:,.2f}",   tot_r)],
    ]
    if disc_pct:
        tot_rows.append([_p(f"Discount ({disc_pct:.1f}%)", tot_l),
                         _p(f"– {sym}{disc_amt:,.2f}", tot_r)])
    if markup_pct and markup_visible:
        tot_rows.append([_p(f"Markup ({markup_pct:.1f}%)", tot_l),
                         _p(f"+ {sym}{markup_amt:,.2f}", tot_r)])
    if shipping:
        tot_rows.append([_p("Shipping", tot_l), _p(f"{sym}{shipping:,.2f}", tot_r)])
    tot_rows.append([_p(f"TOTAL ({sym})", grand_l), _p(f"{sym}{grand_total:,.2f}", grand_r)])

    tot_tbl = Table(tot_rows, colWidths=[55 * mm, 40 * mm], hAlign="RIGHT")
    tot_tbl.setStyle(TableStyle([
        ("LINEABOVE",     (0, -1), (-1, -1), 1, SPM_RED),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
    ]))
    story.append(tot_tbl)
    story.append(Spacer(1, 5 * mm))

    # ── 7. T&C ───────────────────────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=0.5, color=SPM_BDRY,
                            spaceBefore=2 * mm, spaceAfter=3 * mm))
    story.append(_p("TERMS & CONDITIONS", tc_hd))

    tc_items = [
        ("Delivery",          quotation.get("delivery_dest", "") or "—"),
        ("Lead Time",         quotation.get("lead_time", "") or "—"),
        ("Country of Import", quotation.get("country_import", "") or "—"),
        ("Mode of Shipping",  quotation.get("shipping_mode", "") or "—"),
        ("Manufacturer",      quotation.get("manufacturer", "") or "—"),
        ("Total Weight",      quotation.get("weight", "") or "—"),
        ("Currency",          currency),
        ("Offer Validity",    f"{validity} Days"),
    ]
    tc_pairs = []
    for i in range(0, len(tc_items), 2):
        row = [_p(f"<b>{tc_items[i][0]}:</b>  {tc_items[i][1]}", tc_bod)]
        if i + 1 < len(tc_items):
            row.append(_p(f"<b>{tc_items[i+1][0]}:</b>  {tc_items[i+1][1]}", tc_bod))
        else:
            row.append(_p("", tc_bod))
        tc_pairs.append(row)

    tc_tbl = Table(tc_pairs, colWidths=[USE_W * 0.5, USE_W * 0.5], hAlign="LEFT")
    tc_tbl.setStyle(TableStyle([
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
    ]))
    story.append(tc_tbl)
    story.append(Spacer(1, 5 * mm))

    # ── 8. Notes (below T&C) ─────────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=0.5, color=SPM_BDRY,
                            spaceBefore=2 * mm, spaceAfter=3 * mm))
    story.append(_p(
        "<b>Quote number must appear on purchase order and all correspondence. "
        "Please e-mail purchase orders to SPECIALPIPING at "
        "<font color='#8C1C1C'>enquiry@specialpipingltd.com</font> or "
        "<font color='#8C1C1C'>specialpiping@gmail.com</font></b>",
        body,
    ))
    story.append(Spacer(1, 3 * mm))
    notes = quotation.get("notes") or "Looking forward to your business."
    story.append(_p(f"<b>Notes:</b>  {notes}", body))
    story.append(Spacer(1, 5 * mm))

    # ── 9. Footer ────────────────────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=0.5, color=SPM_BDRY,
                            spaceBefore=2 * mm, spaceAfter=3 * mm))
    story.append(_p(
        "SOLE AGENT / AFFILIATE TO:  FLEXITALLIC  ·  FAMIS  ·  FEDERAL STEEL SUPPLY INC.  ·  "
        "EMU ELECTRICAL MAINTENANCE LTD  ·  CARBTEC SEALING TECHNOLOGIES CO. LTD",
        aff,
    ))

    doc.build(story)
    return buf.getvalue()

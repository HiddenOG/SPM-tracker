"""
quotation_pdf.py — Generate a professional PDF quotation.

Supports two companies:
  "spm"    → Special Piping Materials (Nig.) Ltd.  (dark-red brand)
  "danmag" → Daniel Mag Obontuaren (Nig) Limited   (dark-navy brand)

Usage:
    from quotation_pdf import generate_quotation_pdf
    pdf_bytes = generate_quotation_pdf(quotation_dict, line_items_list)
"""

from __future__ import annotations
import io
import re
from html import escape as _html_escape
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

# ── Unicode font for ₦ symbol ─────────────────────────────────────────────────
_NAIRA_FONT = None

def _register_naira_font() -> None:
    """Register any Unicode TTF so the naira sign has a glyph.

    Helvetica has no U+20A6, and ReportLab silently substitutes "I" - so on an
    image with no fonts installed an NGN quote prints "I40,741,950.00". The
    deploy image is python:3.11-slim, which ships no fonts at all, hence the
    glob over the usual directories on top of the fixed candidates.
    """
    global _NAIRA_FONT
    candidates = [
        ROOT / "data_fl" / "fonts" / "DejaVuSans.ttf",
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
    ]
    for root in (Path("/usr/share/fonts"), Path("/nix/store")):
        if len(candidates) > 40 or not root.exists():
            continue
        try:
            candidates.extend(sorted(root.glob("**/DejaVuSans.ttf"))[:5])
            candidates.extend(sorted(root.glob("**/LiberationSans-Regular.ttf"))[:5])
        except Exception:
            pass
    for path in candidates:
        try:
            if not Path(path).exists():
                continue
            pdfmetrics.registerFont(TTFont("SPM-Unicode", str(path)))
            _NAIRA_FONT = "SPM-Unicode"
            return
        except Exception:
            pass
    print("  [warn] quotation_pdf: no Unicode font found - the naira sign will not render")

_register_naira_font()


def _fix_naira(text: str) -> str:
    if "₦" not in text or not _NAIRA_FONT:
        return text
    return text.replace("₦", f'<font name="{_NAIRA_FONT}">₦</font>')


_RL_COLOURS = set(colors.getAllNamedColors().keys())


def _valid_colour(val: str) -> str | None:
    """Return a colour ReportLab can parse, or None.

    Word and Outlook emit tokens like `windowtext`, `auto` and `currentColor`
    that ReportLab rejects outright, taking the whole PDF down with them.
    """
    v = (val or "").strip()
    if re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})", v):
        return v
    if re.fullmatch(r"rgb\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)", v, re.IGNORECASE):
        return v
    if v.lower() in _RL_COLOURS:
        return v.lower()
    return None


def _balance_tags(src: str) -> str:
    """Close tags left open and drop closers with no opener.

    A part-selected paste ("<b>half a sentence") otherwise raises inside
    ReportLab's XML parser and returns a 500 for the whole download.
    """
    out: list[str] = []
    stack: list[str] = []
    for part in re.split(r"(</?(?:b|i|u|font)(?=[\s/>])[^>]*>)", src, flags=re.IGNORECASE):
        # fullmatch, and the same tag-name boundary the split uses. The split
        # hands back TEXT chunks as well as tags, and a text chunk that merely
        # starts with "<br/>" used to match "<b" here - so every blank line
        # typed right after bold text counted as an unclosed <b>. That added
        # stray </b> closers at the end, ReportLab rejected the paragraph, and
        # the row printed as plain text with its colour, bold and line breaks
        # gone (SPM-00001-TEN, 13 of 22 rows).
        m = re.fullmatch(r"<(/?)(b|i|u|font)(?=[\s/>])[^>]*>", part, re.IGNORECASE)
        if not m:
            out.append(part)
            continue
        tag = m.group(2).lower()
        if m.group(1) != "/":
            stack.append(tag)
            out.append(part)
        elif tag in stack:
            while stack:
                t = stack.pop()
                out.append(f"</{t}>")
                if t == tag:
                    break
        # a closer with no matching opener is dropped
    while stack:
        out.append(f"</{stack.pop()}>")
    return "".join(out)


def _html_to_rl(html: str) -> str:
    """Convert browser bold/colour HTML to ReportLab Paragraph markup."""
    if not html:
        return ""
    html = re.sub(r"<strong[^>]*>", "<b>", html, flags=re.IGNORECASE)
    html = re.sub(r"</strong>", "</b>", html, flags=re.IGNORECASE)
    html = re.sub(r"<em[^>]*>", "<i>", html, flags=re.IGNORECASE)
    html = re.sub(r"</em>", "</i>", html, flags=re.IGNORECASE)

    # Block boundaries must become explicit breaks BEFORE the tags are
    # stripped, otherwise "<p>one</p><p>two</p>" renders as "onetwo".
    # ReportLab honours <br/> only - a bare newline is folded into whitespace.
    html = re.sub(r"</(?:p|div|li|tr|h[1-6]|blockquote)\s*>", "<br/>", html, flags=re.IGNORECASE)
    html = re.sub(r"<br\s*/?>", "<br/>", html, flags=re.IGNORECASE)

    def _convert_spans(src: str) -> str:
        stack: list[bool] = []
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
            else:
                out.append(part)
        return "".join(out)

    html = _convert_spans(html)
    html = re.sub(r"<(?!/?\s*(?:b|i|u|font|br)[\s/>])[^>]+>", "", html, flags=re.IGNORECASE)

    # Text pasted from Word or Outlook arrives carrying its own markup, e.g.
    #   <font face="Calibri, sans-serif" color="#cc0000"><b style="">...</b></font>
    # ReportLab treats `face` as a REGISTERED font name, so a CSS font stack
    # raises "Can't map determine family/bold/italic for calibri, sans-serif".
    # `size` is worse than useless: HTML sizes are a 1-7 scale but ReportLab
    # reads the number as POINTS, so a pasted size="3" renders at 3pt.
    # Keep only a colour ReportLab can actually parse.
    def _clean_font(m: re.Match) -> str:
        attrs = m.group(1) or ""
        colour = re.search(r'color\s*=\s*"([^"]+)"', attrs, re.IGNORECASE)
        keep = ""
        if colour:
            ok = _valid_colour(colour.group(1))
            if ok:
                keep = f' color="{ok}"'
        # A <font> carrying nothing usable would still nest correctly, so emit
        # it bare rather than dropping it and unbalancing the closing tag.
        return f"<font{keep}>"

    html = re.sub(r"<font([^>]*)>", _clean_font, html, flags=re.IGNORECASE)
    html = re.sub(r"<(b|i|u)\s+[^>]*>", r"<\1>", html, flags=re.IGNORECASE)
    html = _balance_tags(html)

    # Tidy the breaks the block conversion introduced.
    html = re.sub(r"(?:\s*<br/>\s*){3,}", "<br/><br/>", html, flags=re.IGNORECASE)
    html = re.sub(r"^(?:\s*<br/>)+|(?:<br/>\s*)+$", "", html, flags=re.IGNORECASE)
    return html.strip()


# ── Shared palette constants ──────────────────────────────────────────────────
WHITE    = colors.white
BLACK    = colors.black
SPM_GREY = colors.HexColor("#F4F6FC")
SPM_BDRY = colors.HexColor("#DDD9D3")
LGREY    = colors.HexColor("#FAFAFA")

# ── Company branding registry ─────────────────────────────────────────────────
_BRANDS: dict[str, dict] = {
    "spm": {
        "name":       "Special Piping Materials (Nig.) Ltd.",
        "tagline":    "MANUFACTURING  ·  CONTRACT  ·  PROCUREMENT  ·  SUPPLY SERVICES",
        "address":    "KM3, NPA Expressway By Refinery Flyover, P.O Box 2004, Warri, Delta State, Nigeria",
        "contact":    "Tel: +234 8102621418  |  enquiry@specialpipingltd.com  |  www.specialpipingltd.com",
        "po_note":    "Please e-mail purchase orders to SPECIALPIPING at "
                      "<font color='{primary}'>enquiry@specialpipingltd.com</font> or "
                      "<font color='{primary}'>specialpiping@gmail.com</font>",
        "logo":       ROOT / "data_fl" / "Gemini_Generated_Image_z6jc3vz6jc3vz6jc.png",
        "primary":    colors.HexColor("#8C1C1C"),
        "dark":       colors.HexColor("#1C2235"),
        "affiliates": "SOLE AGENT / AFFILIATE TO:  FLEXITALLIC  ·  FAMIS  ·  FEDERAL STEEL SUPPLY INC.  ·  "
                      "EMU ELECTRICAL MAINTENANCE LTD  ·  CARBTEC SEALING TECHNOLOGIES CO. LTD",
        "sig":        "",
    },
    "danmag": {
        "name":       "Daniel Mag Obontuaren (Nig) Limited",
        "tagline":    "SALES SUPPLIES AND SERVICES",
        "address":    "No. 40 Ogunu Road, By Fed, Govt College, Warri, Delta State.",
        "contact":    "danielmagltd@gmail.com",
        "po_note":    "Please e-mail purchase orders to DANIEL MAG at "
                      "<font color='{primary}'>danielmagltd@gmail.com</font>",
        "logo":       ROOT / "data_fl" / "Gemini_Generated_Image_7ak2957ak2957ak2.jpg",
        "primary":    colors.HexColor("#1C2878"),
        "dark":       colors.HexColor("#1A1A2E"),
        "affiliates": "",
        "sig":        "Ehis Okojie\nDan Mag Obortuaren Nig. Ltd",
    },
}


def _brand_hex(col) -> str:
    """ReportLab's Color.hexval() returns '0x8c1c1c' - convert to '#8c1c1c'.

    The old code did `int(col.hexval() * 0xFFFFFF)`, but hexval() is a STRING,
    so that built a ~100MB string, raised, and fell back to SPM maroon - which
    is why the Daniel Mag note printed in Special Piping's colour.
    """
    try:
        return "#" + col.hexval()[2:].rjust(6, "0")[-6:]
    except Exception:
        return "#8C1C1C"


def _fmt_date(s: str) -> str:
    if not s:
        return ""
    try:
        d = datetime.strptime(s[:10], "%Y-%m-%d")
        return f"{d.day} {d.strftime('%B, %Y')}"
    except Exception:
        return s


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    """Build a Paragraph, degrading to plain text rather than failing.

    Every free-text field on a quotation (subject, notes, recipient, lead
    time...) is fed straight to ReportLab's XML parser, so a stray "<" that
    looks like a tag once took the whole download down with a 500. Losing the
    styling on one line is always better than losing the PDF.
    """
    try:
        return Paragraph(_fix_naira(text), style)
    except Exception:
        # Keep the line structure even when the styling has to go. Stripping
        # every tag outright joined the lines up - "CO. LTD" followed by a
        # break and "DELIVERY LOCATION" printed as "CO. LTDDELIVERY LOCATION".
        src = str(text or "")
        src = re.sub(r"<br\s*/?>|</(?:p|div|li|tr|h[1-6]|blockquote)\s*>", "\x00", src,
                     flags=re.IGNORECASE)
        plain = _html_escape(re.sub(r"<[^>]*>", "", src))
        plain = re.sub(r"(?:\s*\x00\s*)+", "<br/>", plain).strip()
        plain = re.sub(r"^(?:<br/>)+|(?:<br/>)+$", "", plain)
        return Paragraph(_fix_naira(plain), style)


def generate_quotation_pdf(quotation: dict, line_items: list[dict]) -> bytes:
    buf = io.BytesIO()

    # ── Brand selection ───────────────────────────────────────────────────────
    co_key  = (quotation.get("company") or "spm").lower()
    brand   = _BRANDS.get(co_key, _BRANDS["spm"])

    # Per-field Terms & Conditions eye toggles. A missing flag means shown, so
    # quotes saved before the toggles existed render exactly as they always did.
    def _shown(flag: str) -> bool:
        val = quotation.get(flag)
        return True if val is None else bool(val)

    PRIMARY = brand["primary"]
    DARK    = brand["dark"]
    primary_hex = _brand_hex(PRIMARY)

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
        author=brand["name"],
    )

    # ── Styles ────────────────────────────────────────────────────────────────
    def sty(name="body", **kw) -> ParagraphStyle:
        base = {
            "fontName":        "Helvetica",
            "fontSize":        8.5,
            "textColor":       DARK,
            "leading":         12,
            "spaceAfter":      0,
            "spaceBefore":     0,
            "leftIndent":      0,
            "rightIndent":     0,
            "firstLineIndent": 0,
        }
        base.update(kw)
        return ParagraphStyle(name, **base)

    co_name = sty("co_name", fontName="Helvetica-Bold", fontSize=12, textColor=PRIMARY, leading=15)
    co_tag  = sty("co_tag",  fontSize=6.5, textColor=PRIMARY, letterSpacing=0.4,
                  fontName="Helvetica-Oblique")
    co_addr = sty("co_addr", fontSize=7.5, textColor=colors.HexColor("#666666"), leading=11)
    q_word  = sty("q_word",  fontName="Helvetica-Bold", fontSize=22, textColor=PRIMARY,
                  alignment=TA_RIGHT, leading=26)
    q_num   = sty("q_num",   fontSize=7.5, textColor=colors.grey, alignment=TA_RIGHT)
    m_lbl   = sty("m_lbl",   fontSize=7,   textColor=colors.grey)
    m_val   = sty("m_val",   fontName="Helvetica-Bold", fontSize=8.5, textColor=DARK)
    b_lbl   = sty("b_lbl",   fontName="Helvetica-Bold", fontSize=7.5, textColor=colors.grey,
                  letterSpacing=0.4, spaceAfter=2)
    b_name  = sty("b_name",  fontName="Helvetica-Bold", fontSize=10, textColor=DARK, spaceAfter=1)
    b_addr  = sty("b_addr",  fontSize=8.5, textColor=DARK, leading=13, alignment=TA_JUSTIFY)
    body    = sty("body_",   fontSize=8.5, textColor=DARK, leading=12,  alignment=TA_JUSTIFY)
    small   = sty("small_",  fontSize=7.5, textColor=colors.HexColor("#777777"), leading=10)
    tc_hd   = sty("tc_hd",   fontName="Helvetica-Bold", fontSize=8.5, textColor=PRIMARY,
                  spaceBefore=2, spaceAfter=3)
    tc_bod  = sty("tc_bod",  fontSize=8, textColor=DARK, leading=12, alignment=TA_JUSTIFY)
    foot    = sty("foot_",   fontSize=7.5, textColor=colors.grey, alignment=TA_JUSTIFY,
                  fontName="Helvetica-Oblique")
    aff     = sty("aff_",    fontSize=6.5, textColor=colors.grey, alignment=TA_CENTER)
    th_cell = sty("th_",     fontName="Helvetica-Bold", fontSize=8, textColor=WHITE)
    th_r    = sty("th_r",    fontName="Helvetica-Bold", fontSize=8, textColor=WHITE,
                  alignment=TA_RIGHT)
    th_c    = sty("th_c",    fontName="Helvetica-Bold", fontSize=8, textColor=WHITE,
                  alignment=TA_CENTER)
    td_c    = sty("td_c",    fontSize=8.5, textColor=DARK, alignment=TA_CENTER)
    td_r    = sty("td_r",    fontSize=8.5, textColor=DARK, alignment=TA_RIGHT)
    td_s    = sty("td_s",    fontSize=7.5, textColor=colors.grey, alignment=TA_CENTER)
    td_mfr  = sty("td_mfr",  fontSize=7, textColor=colors.grey, leading=10,
                  fontName="Helvetica-Oblique")
    td_desc = sty("td_desc", fontSize=8, textColor=DARK, leading=11)
    tot_l   = sty("tot_l",   fontSize=9, textColor=DARK)
    tot_r   = sty("tot_r",   fontSize=9, textColor=DARK, alignment=TA_RIGHT)
    grand_l = sty("grand_l", fontName="Helvetica-Bold", fontSize=11, textColor=PRIMARY)
    grand_r = sty("grand_r", fontName="Helvetica-Bold", fontSize=11, textColor=PRIMARY,
                  alignment=TA_RIGHT)
    sig_sty = sty("sig_",    fontSize=8.5, textColor=DARK, leading=13)

    story: list[Any] = []

    # ── 1. Header ─────────────────────────────────────────────────────────────
    logo_path = brand["logo"]
    logo_cell: list[Any] = []
    if Path(logo_path).exists():
        try:
            logo_cell.append(Image(str(logo_path), width=28 * mm, height=28 * mm))
        except Exception:
            pass
    logo_cell += [
        _p(brand["name"],    co_name),
        _p(brand["tagline"], co_tag),
        _p(
            brand["address"] + "<br/>" + brand["contact"],
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
    story.append(HRFlowable(width="100%", thickness=1.5, color=PRIMARY,
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

    # Lead Time, Country of Import and Mode of Shipping appear BOTH here and in
    # the Terms & Conditions block below. Their eye toggles have to gate both
    # places, or hiding one would still leave it on display up here.
    meta_pairs = [
        ("Quote Date",        qdate),
        ("Prepared By",       quotation.get("prepared_by", "")),
        ("Expiry Date",       expiry),
    ]
    if _shown("show_lead_time"):
        meta_pairs.append(("Lead Time", quotation.get("lead_time", "")))
    meta_pairs.append(("Reference REQ #", quotation.get("reference_po", "")))
    if _shown("show_country_import"):
        meta_pairs.append(("Country of Import", quotation.get("country_import", "")))
    if _shown("show_shipping_mode"):
        meta_pairs.append(("Mode of Shipping", quotation.get("shipping_mode", "")))
    meta_pairs.append(("Offer Validity", f"{validity} Days"))

    # Lay out two per row, padding a trailing odd cell so the grid stays square.
    meta_rows = []
    for i in range(0, len(meta_pairs), 2):
        left = meta(*meta_pairs[i])
        right = meta(*meta_pairs[i + 1]) if i + 1 < len(meta_pairs) else [_p("", m_lbl)]
        meta_rows.append([left, right])
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
        ("BACKGROUND",    (0, 0), (-1, 0), PRIMARY),
        ("TEXTCOLOR",     (0, 0), (-1, 0), WHITE),
        ("GRID",          (0, 0), (-1, -1), 0.4, colors.HexColor("#E8E8E8")),
        ("LINEBELOW",     (0, 0), (-1, 0), 1, PRIMARY),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [WHITE, LGREY]),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(items_tbl)
    story.append(Spacer(1, 5 * mm))

    # ── 6. Totals ────────────────────────────────────────────────────────────
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
        ("LINEABOVE",     (0, -1), (-1, -1), 1, PRIMARY),
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

    # Each row is gated by its own eye toggle on the quote form (_shown above).
    tc_items = [
        (label, value) for flag, label, value in [
            ("show_delivery_dest",  "Delivery",          quotation.get("delivery_dest", "") or "—"),
            ("show_lead_time",      "Lead Time",         quotation.get("lead_time", "") or "—"),
            ("show_country_import", "Country of Import", quotation.get("country_import", "") or "—"),
            ("show_shipping_mode",  "Mode of Shipping",  quotation.get("shipping_mode", "") or "—"),
            ("show_manufacturer",   "Manufacturer",      quotation.get("manufacturer", "") or "—"),
            ("show_weight",         "Total Weight",      quotation.get("weight", "") or "—"),
            ("show_currency",       "Currency",          currency),
        ] if _shown(flag)
    ]
    # Offer Validity has no toggle — it is part of the offer's legal standing,
    # not a detail to withhold.
    tc_items.append(("Offer Validity", f"{validity} Days"))
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

    # ── 8. Notes ─────────────────────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=0.5, color=SPM_BDRY,
                            spaceBefore=2 * mm, spaceAfter=3 * mm))

    po_note = brand["po_note"].format(primary=primary_hex)
    story.append(_p(
        f"<b>Quote number must appear on purchase order and all correspondence. "
        f"{po_note}</b>",
        body,
    ))
    story.append(Spacer(1, 3 * mm))
    notes = quotation.get("notes") or "Looking forward to your business."
    story.append(_p(f"<b>Notes:</b>  {notes}", body))
    story.append(Spacer(1, 5 * mm))

    # ── 9. Signature block ───────────────────────────────────────────────────
    sig_text = quotation.get("sig_name") or brand["sig"]
    if sig_text:
        story.append(HRFlowable(width="100%", thickness=0.5, color=SPM_BDRY,
                                spaceBefore=2 * mm, spaceAfter=3 * mm))
        story.append(_p("Sincerely,", sig_sty))
        story.append(Spacer(1, 3 * mm))
        for line in sig_text.split("\n"):
            story.append(_p(f"<b>{line}</b>" if line == sig_text.split("\n")[0] else line, sig_sty))

    # ── 10. Footer ───────────────────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=0.5, color=SPM_BDRY,
                            spaceBefore=2 * mm, spaceAfter=3 * mm))
    if brand["affiliates"]:
        story.append(_p(brand["affiliates"],
                        sty("aff_", fontSize=6.5, textColor=colors.grey, alignment=TA_CENTER)))

    doc.build(story)
    return buf.getvalue()

"""
test_quotation_pdf_markup.py - line-item formatting must survive into the PDF.

The quote editor saves descriptions as browser HTML. If the converted markup is
not valid ReportLab XML, the row silently degrades to plain text, so a failure
here shows up as a quote that "lost its colours and spacing" rather than an
error. The fixtures below are real descriptions from SPM-00001-TEN.
"""
from reportlab.platypus import Paragraph
from reportlab.lib.styles import getSampleStyleSheet

from quotation_pdf import _html_to_rl, _balance_tags, _p, _rich_text

STYLE = getSampleStyleSheet()["Normal"]

# Enter pressed with bold still on leaves <b><br></b>; a bold line ending a
# block leaves </b></div>. Both put a "<br/>" at the start of a text chunk.
EDITED_ROW = (
    'BELT TYPE : WEDGE LENGTH : 17X13X2650MM <div><b><br></b></div>'
    '<div><b>Manufacturer Part No</b> : SPB-2650 </div>'
    '<div><b>Manufacturer :</b> DRB HOLDING CO. LTD</div><div><br></div>'
    '<div><b><font color="#cc0000">DELIVERY LOCATION: FCA UNITED KINGDOM.</font></b></div>'
)
IMPORTED_ROW = (
    'PRESSURE DISC COMES WITH SPINDLE<div><br><div><b> Manufacturer Part No : </b>'
    '1436-202 </div><div><b>Manufacturer : </b>PRES-VAC </div></div>'
)


def _parses(markup: str) -> bool:
    try:
        Paragraph(markup, STYLE)
        return True
    except Exception:
        return False


def test_blank_line_typed_in_bold_keeps_the_row_formatted():
    rl = _html_to_rl(EDITED_ROW)
    assert _parses(rl)
    assert rl.count("<b>") == rl.count("</b>")
    assert '<font color="#cc0000">DELIVERY LOCATION' in rl


def test_imported_row_still_formats():
    assert _parses(_html_to_rl(IMPORTED_ROW))


def test_a_break_is_never_mistaken_for_a_bold_tag():
    assert _balance_tags("<b>x</b><br/>y") == "<b>x</b><br/>y"
    assert _balance_tags("<b><br/></b>") == "<b><br/></b>"


def test_unclosed_bold_is_still_closed():
    assert _balance_tags("<b>half a sentence") == "<b>half a sentence</b>"


def test_plain_text_fallback_keeps_line_breaks():
    # Invalid markup forces the fallback; the lines must not run together.
    para = _p('CO. LTD<div><br></div><div><b>DELIVERY</b></div><', STYLE)
    assert "LTDDELIVERY" not in para.text
    assert "CO. LTD<br/>DELIVERY" in para.text


# ── VAT, with its own eye ────────────────────────────────────────────────────

def _quote(**kw):
    q = {"quote_number": "SPM-00046-CNL", "client": "chevron", "company": "spm",
         "quote_date": "2026-09-25", "currency": "USD", "sub_total": 1000.0,
         "discount_pct": 5, "markup_pct": 10, "markup_visible": True,
         "shipping_charges": 50}
    q.update(kw)
    return q


_ITEMS = [{"item_no": 1, "description": "GASKET", "uom": "EA",
           "quantity": 10, "unit_price": 100, "tax_rate": 0, "line_total": 1000}]


def _lines(pdf):
    import fitz
    text = "".join(p.get_text() for p in fitz.open(stream=pdf, filetype="pdf"))
    return [l.strip() for l in text.splitlines() if l.strip()]


def test_the_vat_line_shows_when_the_eye_is_open():
    from quotation_pdf import generate_quotation_pdf
    pdf = generate_quotation_pdf(_quote(vat_pct=7.5, vat_visible=True), _ITEMS)
    assert any(l.startswith("VAT (7.5") for l in _lines(pdf))


def test_the_vat_line_hides_when_the_eye_is_closed():
    from quotation_pdf import generate_quotation_pdf
    pdf = generate_quotation_pdf(_quote(vat_pct=7.5, vat_visible=False), _ITEMS)
    assert not any(l.startswith("VAT") for l in _lines(pdf))


def test_hiding_the_line_does_not_change_the_total():
    """The eye hides the line, not the charge — same as the markup. A customer
    who cannot see the VAT still pays it."""
    import re
    from quotation_pdf import generate_quotation_pdf

    def grand(visible):
        q = _quote(vat_pct=7.5, vat_visible=visible, total=1178.75)
        text = " ".join(_lines(generate_quotation_pdf(q, _ITEMS)))
        return re.search(r"TOTAL \(\$\)\s*\$?([\d,]+\.\d\d)", text).group(1)

    assert grand(True) == grand(False) == "1,178.75"


def test_no_vat_row_when_the_rate_is_zero():
    from quotation_pdf import generate_quotation_pdf
    pdf = generate_quotation_pdf(_quote(vat_pct=0, vat_visible=True), _ITEMS)
    assert not any(l.startswith("VAT") for l in _lines(pdf))


def test_vat_is_charged_on_the_goods_after_discount_and_markup():
    """1000 - 5% + 10% = 1050; VAT 7.5% of that is 78.75, not 75.00."""
    from quotation_pdf import generate_quotation_pdf
    pdf = generate_quotation_pdf(_quote(vat_pct=7.5, vat_visible=True), _ITEMS)
    assert any("78.75" in l for l in _lines(pdf))


def test_an_older_quote_without_the_columns_still_renders():
    """vat_pct and vat_visible did not exist until the migration."""
    from quotation_pdf import generate_quotation_pdf
    assert generate_quotation_pdf(_quote(), _ITEMS).startswith(b"%PDF")


# ── Customer Notes, now a rich-text field ────────────────────────────────────
# Notes used to be a plain textarea, so the column holds both shapes: editor
# HTML on new quotes and plain text on everything saved before. Both have to
# reach the PDF intact, and neither may be escaped twice.

def test_a_bold_coloured_note_keeps_its_formatting():
    rl = _rich_text('<b>Payment:</b> <font color="#cc0000">70% on order</font>')
    assert _parses(rl)
    assert "<b>Payment:</b>" in rl
    assert '<font color="#cc0000">70% on order</font>' in rl


def test_a_bold_coloured_note_reaches_the_pdf():
    from quotation_pdf import generate_quotation_pdf
    q = _quote(notes='<b>Prices valid 30 days</b><br><i>Ex-works Warri</i>')
    lines = _lines(generate_quotation_pdf(q, _ITEMS))
    assert any("Prices valid 30 days" in l for l in lines)
    assert any("Ex-works Warri" in l for l in lines)


def test_an_old_plain_text_note_keeps_its_line_breaks():
    rl = _rich_text("Lead time: 12 weeks\nFOB Houston")
    assert _parses(rl)
    assert "12 weeks<br/>FOB Houston" in rl


def test_an_ampersand_is_escaped_once_and_only_once():
    """The field is cleaned on save and again on render. "Freight & Duty" must
    print as written, not creep to "&amp;" and then "&amp;amp;"."""
    plain = _rich_text("Freight & Duty included")
    assert plain == "Freight &amp; Duty included"
    assert _rich_text(plain) == plain                      # second pass is a no-op
    assert _parses(plain)


def test_a_note_typed_with_a_stray_angle_bracket_still_renders():
    from quotation_pdf import generate_quotation_pdf
    q = _quote(notes="Tolerance < 0.5mm & no substitutes")
    pdf = generate_quotation_pdf(q, _ITEMS)
    assert pdf.startswith(b"%PDF")
    # It reached the page as a note, not as the "Notes:" label alone.
    assert any("0.5mm" in l for l in _lines(pdf))


def test_an_empty_note_falls_back_to_the_standing_sentence():
    assert _rich_text("", "Looking forward to your business.") \
        == "Looking forward to your business."
    assert _rich_text(None, "Looking forward to your business.") \
        == "Looking forward to your business."
    # An editor left empty reports a bare break, not an empty string.
    assert _rich_text("<br>", "fallback") == "fallback"


def test_the_notes_label_survives_a_formatted_note():
    from quotation_pdf import generate_quotation_pdf
    q = _quote(notes='<b><font color="#0b5">All items FIRM</font></b>')
    assert any(l.startswith("Notes:") for l in _lines(generate_quotation_pdf(q, _ITEMS)))

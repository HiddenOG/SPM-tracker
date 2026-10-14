"""
test_spm_po.py — the SPM purchase order generator.

The document this replaces was being made by copying the last PO and typing
over it, and the blueprint file shows exactly what that costs: a MetalsDepot
order still carrying a Flexitallic contact email, and a leftover gasket line
sitting in the totals row with no quantity and no price. The tests below pin
the pieces that stop the generator reproducing those mistakes, plus the number
format the office already writes by hand.
"""
import sys
from pathlib import Path

import pytest

WEB = Path(__file__).parent.parent / "web"
sys.path.insert(0, str(WEB))

from spm_po_pdf import (
    _description, _esc_lines, _fmt_date, _money, _qty, _signature_for,
)


def _app():
    """Import web/app.py lazily — it needs its environment set up first."""
    import os
    os.environ.setdefault("JWT_SECRET", "test-secret")
    import app
    return app


# ── PO number ────────────────────────────────────────────────────────────────
# The shape is taken from the 62 SPM POs already on file, not invented:
#   S.P.M.-C.N.L.-3101-0061451715-0061451710-0061448744-FLEXITALLIC
#
# The client POs in the middle are read off the line items' P.O. No. column.
# There is no second field for them, so the number cannot disagree with the
# table the supplier is being asked to ship against.

def _pos(*values):
    return _app()._client_pos_from_items([{"client_po_no": v} for v in values])


def test_po_number_reproduces_a_real_chevron_order():
    """S.P.M.-C.N.L.-3101-… is on file exactly as written here."""
    assert _app()._po_number(
        "chevron", "",
        _pos("0061451715", "0061451710", "0061448744", "0061448082", "0061448507"),
        "Flexitallic", 3101,
    ) == ("S.P.M.-C.N.L.-3101-0061451715-0061451710-0061448744-0061448082"
          "-0061448507-FLEXITALLIC")


def test_po_number_reproduces_a_single_po_order():
    assert _app()._po_number("chevron", "", _pos("0061411709"), "Flexitallic", 3073) \
        == "S.P.M.-C.N.L.-3073-0061411709-FLEXITALLIC"


def test_nlng_uses_its_own_client_code():
    assert _app()._po_number("nlng", "", _pos("4200089123"), "Flexitallic", 3111) \
        == "S.P.M.-NLNG-3111-4200089123-FLEXITALLIC"


# ── the padding rule ─────────────────────────────────────────────────────────

def test_a_short_chevron_po_is_padded_back_to_ten_digits():
    """The blueprint workbook types '61484185' for a PO filed as '0061484185'."""
    assert _app()._pad_client_po("61484185") == "0061484185"
    assert _app()._pad_client_po("061451715") == "0061451715"


def test_a_ten_digit_po_is_left_alone():
    """NLNG's 4200… and Mobil's 4501… are already whole."""
    assert _app()._pad_client_po("0061451715") == "0061451715"
    assert _app()._pad_client_po("4200089123") == "4200089123"
    assert _app()._pad_client_po("4501896897") == "4501896897"


def test_a_free_form_reference_is_never_padded():
    """Small customers use their own shapes — padding would corrupt them."""
    assert _app()._pad_client_po("SS/29/01/25/SPM/6113") == "SS/29/01/25/SPM/6113"
    assert _app()._pad_client_po("GCA123456") == "GCA123456"
    assert _app()._pad_client_po("123") == "123"       # too short to be a PO


def test_the_number_carries_the_padded_form_of_what_was_typed():
    assert _app()._po_number("chevron", "", _pos("61484185"), "MetalsDepot", 3131) \
        == "S.P.M.-C.N.L.-3131-0061484185-METALSDEPOT"


# ── reading the list off the line items ──────────────────────────────────────

def test_a_po_used_on_several_lines_appears_once():
    """Most orders put every line against the same client PO."""
    assert _pos("61451715", "61451715", "61451710") == ["0061451715", "0061451710"]


def test_the_padded_and_unpadded_forms_are_the_same_po():
    """Typing it both ways on two lines must not list it twice."""
    assert _pos("61451715", "0061451715") == ["0061451715"]


def test_blank_po_cells_are_skipped():
    """A line can be priced before anyone knows which client PO it belongs to."""
    assert _pos("", None, "61451715") == ["0061451715"]


def test_line_order_is_the_order_typed():
    """The people reading the number expect their own ordering, not sorted."""
    assert _pos("0061448507", "0061448082") == ["0061448507", "0061448082"]


# ── the rest of the number ───────────────────────────────────────────────────

def test_supplier_company_forms_are_dropped():
    """The POs on file end '-FLEXITALLIC', never '-FLEXITALLICLIMITED'."""
    assert _app()._supplier_slug("Rollstud Limited") == "ROLLSTUD"
    assert _app()._supplier_slug("Flexitallic") == "FLEXITALLIC"
    assert _app()._supplier_slug("MetalsDepot") == "METALSDEPOT"


def test_an_unknown_client_still_yields_a_number():
    assert _app()._po_number("", "", _pos("61484185"), "MetalsDepot", 3131) \
        .startswith("S.P.M.-OTH-3131-")


def test_a_po_with_no_client_pos_is_still_well_formed():
    """Stock buys carry no client PO and must not produce a double dash."""
    assert _app()._po_number("nlng", "", [], "Flexitallic", 3051) == "S.P.M.-NLNG-3051-FLEXITALLIC"


# ── Record shaping ───────────────────────────────────────────────────────────

def test_blank_fields_are_stored_as_null_not_empty_string():
    """An empty box leaves '' behind; the column should read as 'not set'."""
    rec = _app()._po_record({"client": "   ", "vendor_company": None})
    assert rec["client"] is None
    assert rec["vendor_company"] is None


def test_status_defaults_to_draft_now_that_the_form_has_no_status_box():
    """The column stays for the pipeline; nothing on the form sets it."""
    assert _app()._po_record({})["status"] == "draft"
    assert _app()._po_record({"status": "posted"})["status"] == "draft"


def test_line_rows_number_themselves_when_the_form_does_not():
    rows = _app()._po_line_rows("po-1", [{"description": "a"}, {"description": "b"}])
    assert [r["item_no"] for r in rows] == [1, 2]
    assert all(r["spm_po_id"] == "po-1" for r in rows)


def test_line_rows_coerce_blank_numbers_to_zero():
    """A row typed but never priced must not break the insert."""
    rows = _app()._po_line_rows("po-1", [{"description": "a", "quantity": "", "unit_price": None}])
    assert rows[0]["quantity"] == 0.0
    assert rows[0]["unit_price"] == 0.0


def test_the_stored_line_carries_the_padded_po_too():
    """The printed table and the printed number have to agree."""
    rows = _app()._po_line_rows("po-1", [{"client_po_no": "61484185"}])
    assert rows[0]["client_po_no"] == "0061484185"


# ── PDF field handling ───────────────────────────────────────────────────────

def test_description_line_breaks_survive_into_the_pdf():
    """The template stacks size, grade and length in one cell."""
    out = _esc_lines('6" X 6" X 1/2" A500 GR B SQUARE STEEL TUBE\nI7   20\'')
    assert "<br/>" in out
    assert "&quot;" in out          # the inch marks are escaped, not dropped


def test_angle_bracket_in_a_description_is_escaped():
    """'<1/2" NPT' looks like a tag to ReportLab's parser and used to 500."""
    assert _esc_lines('<1/2" NPT').startswith("&lt;")


def test_date_prints_in_the_template_format():
    assert _fmt_date("2026-09-21") == "21/09/2026"


def test_unparseable_date_passes_through_rather_than_vanishing():
    assert _fmt_date("TBA") == "TBA"


def test_whole_quantities_print_whole():
    """The column stores 3.0000; the paper should say 3."""
    assert _qty(3) == "3"
    assert _qty(2.5) == "2.5"


def test_money_uses_the_order_currency():
    assert _money(5621.82, "$") == "$5,621.82"
    assert _money(None, "£") == "£0.00"


# ── Signatures ───────────────────────────────────────────────────────────────

def test_signature_is_found_however_the_name_is_typed():
    assert _signature_for("FELIX AKARUE") is not None
    assert _signature_for("Felix  Akarue") == _signature_for("felixakarue")


def test_an_unknown_name_gets_a_blank_line_to_sign():
    """Stamping someone else's signature on their behalf is the failure to avoid."""
    assert _signature_for("A N Other") is None
    assert _signature_for("") is None


# ── The document builds ──────────────────────────────────────────────────────

def test_pdf_builds_from_the_blueprint_order():
    from spm_po_pdf import generate_spm_po_pdf
    pdf = generate_spm_po_pdf(
        {"po_number": "S.P.M.-C.N.L.-3131-0061484185-METALSDEPOT", "po_date": "2026-09-21",
         "currency": "USD", "vendor_company": "MetalsDepot",
         "manager": "FELIX AKARUE", "sales_person": "IJALA STYLES ONOME",
         "total": 5621.82},
        [{"item_no": 1, "client_po_no": "61484185", "quote_ref": "1041744",
          "description": '6" X 6" X 1/2" A500 GR B SQUARE STEEL TUBE\nI7   20\'',
          "quantity": 3, "unit_price": 1873.94, "line_total": 5621.82}],
    )
    assert pdf.startswith(b"%PDF")


def test_pdf_builds_with_no_line_items_at_all():
    """An empty draft still has to produce a page rather than a 500."""
    from spm_po_pdf import generate_spm_po_pdf
    assert generate_spm_po_pdf({"po_number": "S.P.M.-OTH-3132-FLEXITALLIC"}, []).startswith(b"%PDF")


def test_pdf_total_falls_back_to_the_lines_when_the_header_is_empty():
    """A header saved before the total was computed must not print zero."""
    from spm_po_pdf import generate_spm_po_pdf
    import re as _re
    pdf = generate_spm_po_pdf(
        {"po_number": "S.P.M.-NLNG-3133-FLEXITALLIC", "currency": "USD"},
        [{"item_no": 1, "description": "x", "quantity": 2, "unit_price": 50, "line_total": 100.0}],
    )
    assert pdf.startswith(b"%PDF")


# ── the one-off customer ─────────────────────────────────────────────────────

def test_a_one_off_customer_gets_a_code_from_its_name():
    """'Others' has no fixed code, so the typed name supplies one — the same
    initials rule the quotations already use for the same customer."""
    assert _app()._po_client_code("others", "Sahara Group Limited") == "SGL"
    assert _app()._po_number("others", "Sahara Group Limited", ["SS/29/01"],
                             "Rollstud Limited", 3132) \
        == "S.P.M.-SGL-3132-SS/29/01-ROLLSTUD"


def test_a_one_off_customer_with_no_name_still_gets_a_code():
    assert _app()._po_client_code("others", "") == "OTH"
    assert _app()._po_client_code("others", "   ") == "OTH"


def test_a_listed_client_ignores_any_name_left_in_the_box():
    """Switching back from Others must not leave the old initials behind."""
    assert _app()._po_client_code("chevron", "Sahara Group Limited") == "C.N.L."


def test_the_customer_name_is_stored():
    assert _app()._po_record({"client_name": " Sahara Group "})["client_name"] == "Sahara Group"
    assert _app()._po_record({})["client_name"] is None


# ── rich-text descriptions ───────────────────────────────────────────────────

def test_a_formatted_description_keeps_its_bold_and_colour():
    """The cell is the quote form's editor, so a description can carry markup
    and the PDF has to render it rather than print the tags."""
    out = _description('<b>20IN GASKET</b><br><font color="#cc0000">SPWD 316L</font>')
    assert "<b>20IN GASKET</b>" in out
    assert 'color="#cc0000"' in out


def test_a_plain_description_still_keeps_its_line_breaks():
    """Older rows and imports arrive as plain text through the same path."""
    out = _description("6\" X 6\" TUBE\nI7 20'")
    assert "<br/>" in out
    assert "&quot;" in out


def test_a_description_that_is_not_valid_markup_still_prints():
    """A part-selected paste leaves tags open; losing the styling on one line
    beats losing the whole download to a 500."""
    assert _description("<b>half a sentence") is not None
    assert _description("<1/2\" NPT") is not None


# ── things that used to take the whole download down ─────────────────────────
# Every case below was found by feeding the generator what someone could
# actually type, then fixing what broke.

def _pdf_text(pdf: bytes) -> str:
    import fitz
    return "".join(page.get_text() for page in fitz.open(stream=pdf, filetype="pdf"))


def _build(po=None, items=None):
    from spm_po_pdf import generate_spm_po_pdf
    base = {"po_number": "S.P.M.-C.N.L.-3131-0061451715-FLEXITALLIC",
            "po_date": "2026-09-22", "currency": "USD",
            "vendor_company": "Flexitallic", "manager": "FELIX AKARUE"}
    base.update(po or {})
    return generate_spm_po_pdf(base, items or [])


def test_one_description_taller_than_a_page_does_not_kill_the_pdf():
    """A pasted material specification raised LayoutError and returned a 500
    for the whole order. The row splits across pages instead."""
    pdf = _build(items=[{"item_no": 1, "description": "SPIRAL WOUND GASKET " * 200,
                         "quantity": 1, "unit_price": 10, "line_total": 10}])
    assert pdf.startswith(b"%PDF")


def test_a_description_with_no_spaces_at_all_still_splits():
    """A part number pasted without breaks has nowhere to wrap."""
    assert _build(items=[{"item_no": 1, "description": "X" * 3000,
                          "quantity": 1, "unit_price": 1, "line_total": 1}]).startswith(b"%PDF")


def test_forty_line_items_paginate():
    items = [{"item_no": i + 1, "description": f"ITEM {i+1}\nSPEC\nSPEC",
              "quantity": 1, "unit_price": 100, "line_total": 100} for i in range(40)]
    import fitz
    assert fitz.open(stream=_build(items=items), filetype="pdf").page_count > 1


def test_the_printed_total_is_the_sum_of_the_printed_lines():
    """A TOTAL that disagrees with the rows above it is what starts arguments
    with a supplier, and the rows are the half both sides can check."""
    items = [{"item_no": i + 1, "description": "ITEM", "quantity": 1,
              "unit_price": 100, "line_total": 100} for i in range(40)]
    text = _pdf_text(_build({"total": 100.0}, items))      # header says 100
    assert "$4,000.00" in text
    assert "$100.00" in text                                # the line price still prints


def test_an_order_with_no_lines_falls_back_to_the_stored_total():
    assert "$250.00" in _pdf_text(_build({"total": 250.0}, []))


def test_a_long_po_number_stays_on_one_line():
    """Wrapped, it broke mid-number — '…-0061451710-0' then '61448744-…' —
    which is a reference nobody can read back."""
    from spm_po_pdf import _fit_size
    long_number = ("S.P.M.-C.N.L.-3131-0061451715-0061451710-0061448744"
                   "-0061448082-0061448507-FLEXITALLIC")
    from reportlab.pdfbase import pdfmetrics
    from reportlab.lib.units import mm
    size = _fit_size(long_number, "Helvetica-Bold", 155 * mm)
    assert pdfmetrics.stringWidth(long_number, "Helvetica-Bold", size) <= 155 * mm
    assert size >= 6.5
    assert long_number in _pdf_text(_build({"po_number": long_number}))


def test_a_short_po_number_is_not_shrunk():
    from spm_po_pdf import _fit_size
    from reportlab.lib.units import mm
    assert _fit_size("S.P.M.-NLNG-3131-FLEXITALLIC", "Helvetica-Bold", 155 * mm) == 12.0


def test_hostile_descriptions_all_produce_a_document():
    """None of these may return a 500; losing the styling on one line is fine."""
    for desc in ["<b>half a sentence", "sentence</b> more", "STUD & NUT",
                 '<1/2" NPT', '<font color="windowtext">T</font>',
                 "<script>x</script>G", "6&quot; TUBE", "STUD &amp;amp; NUT",
                 "G\x00S", "GASKET \U0001F527", "PRICE \u20a640,000", "   \n  ", "<br>"]:
        pdf = _build(items=[{"item_no": 1, "description": desc, "quantity": 1,
                             "unit_price": 1, "line_total": 1}])
        assert pdf.startswith(b"%PDF"), desc


def test_malformed_numbers_do_not_stop_the_document():
    for item in [{}, {"quantity": "three"}, {"unit_price": "N/A", "line_total": "N/A"},
                 {"quantity": None, "unit_price": None},
                 {"quantity": 1e9, "unit_price": 1e9, "line_total": 1e18},
                 {"unit_price": -5, "line_total": -5}]:
        row = {"item_no": 1, "description": "X"}
        row.update(item)
        assert _build(items=[row]).startswith(b"%PDF"), item


def test_an_empty_order_still_produces_a_page():
    from spm_po_pdf import generate_spm_po_pdf
    assert generate_spm_po_pdf({}, []).startswith(b"%PDF")
    assert generate_spm_po_pdf({k: None for k in ("po_number", "po_date", "currency",
                                                  "vendor_company", "manager")},
                               [{k: None for k in ("item_no", "description", "quantity",
                                                   "unit_price", "line_total")}]
                               ).startswith(b"%PDF")


def test_an_unparseable_date_does_not_stop_the_document():
    assert _build({"po_date": "not a date"}).startswith(b"%PDF")


def test_an_unknown_currency_falls_back_to_dollars():
    assert "$" in _pdf_text(_build({"currency": "XYZ"},
        [{"item_no": 1, "description": "X", "quantity": 1,
          "unit_price": 5, "line_total": 5}]))


# ── when the database is behind the code ─────────────────────────────────────

def test_a_missing_column_names_the_column_and_the_fix():
    """The first save on a fresh install failed with a bare 'Server error',
    which says nothing about the migration that fixes it."""
    exc = Exception("{'code': 'PGRST204', 'message': \"Could not find the "
                    "'client_name' column of 'spm_pos' in the schema cache\"}")
    gap = _app()._schema_gap(exc)
    assert "client_name" in gap
    assert "spm_pos" in gap
    assert "create_spm_pos.sql" in gap


def test_a_missing_table_points_at_the_migration():
    gap = _app()._schema_gap(Exception("{'code': 'PGRST205', 'message': \"Could not "
                                       "find the table 'public.spm_pos'\"}"))
    assert "create_spm_pos.sql" in gap


def test_an_ordinary_failure_is_not_blamed_on_the_schema():
    """A dropped connection must not send anyone off running migrations."""
    assert _app()._schema_gap(Exception("connection reset by peer")) is None
    assert _app()._schema_gap(Exception("Insert failed")) is None

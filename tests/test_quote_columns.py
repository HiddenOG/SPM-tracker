"""
test_quote_columns.py - a quote's own line-item columns.

Rare quotes need a column the standard table doesn't have (Lead time, Brand)
or a heading under another name. The form saves the layout as line_columns
and each line's added values as extra; the PDF draws its table from them.
A standard quote must save and print exactly as before.
"""
import os
import sys
from pathlib import Path

WEB = Path(__file__).parent.parent / "web"
sys.path.insert(0, str(WEB))


def _app():
    os.environ.setdefault("JWT_SECRET", "test-secret")
    import app
    return app


# ── the saved layout ─────────────────────────────────────────────────────────

def test_a_standard_quote_saves_no_layout():
    assert _app()._quote_columns(None) is None
    assert _app()._quote_columns([]) is None


def test_order_and_names_are_kept():
    cols = _app()._quote_columns([
        {"key": "description", "label": "Material"}, {"key": "x1", "label": "Lead time"},
        {"key": "quantity", "label": ""}, {"key": "unit_price", "label": "Rate"}, {"key": "line_total", "label": ""},
    ])
    assert [c["key"] for c in cols] == ["description", "x1", "quantity", "unit_price", "line_total"]
    assert cols[0]["label"] == "Material" and cols[1]["label"] == "Lead time"


def test_the_figures_columns_can_never_be_dropped():
    """Qty x price makes the total. A layout without them - a bad save, an old
    tab - gets them back rather than printing a quote with no prices."""
    keys = [c["key"] for c in _app()._quote_columns([{"key": "x1", "label": "Brand"}])]
    for k in ("description", "quantity", "unit_price", "line_total"):
        assert k in keys


def test_uom_and_product_no_can_be_removed():
    keys = [c["key"] for c in _app()._quote_columns([
        {"key": "description"}, {"key": "quantity"}, {"key": "unit_price"}, {"key": "line_total"}])]
    assert "uom" not in keys and "product_no" not in keys


def test_unknown_keys_duplicates_and_extras_past_four_are_dropped():
    raw = [{"key": "description"}, {"key": "description"}, {"key": "drop table"}, "junk"] \
        + [{"key": f"x{i}", "label": f"C{i}"} for i in range(1, 7)]
    keys = [c["key"] for c in _app()._quote_columns(raw)]
    assert keys.count("description") == 1
    assert "drop table" not in keys
    assert [k for k in keys if k.startswith("x")] == ["x1", "x2", "x3", "x4"]


def test_a_long_name_is_cut_to_forty():
    cols = _app()._quote_columns([{"key": "x1", "label": "L" * 90}])
    assert len(cols[0]["label"]) == 40


# ── the line rows ────────────────────────────────────────────────────────────

_LINE = {"description": "GASKET", "uom": "EA", "quantity": 2, "unit_price": 5, "line_total": 10}


def test_a_standard_quote_sends_no_extra_field():
    """So it still saves on a database that hasn't had the migration."""
    rows = _app()._quote_line_rows("q1", [_LINE, dict(_LINE)])
    assert all("extra" not in r for r in rows)


def test_extra_is_on_every_row_once_any_line_has_one():
    """One insert, one set of keys: a mix would be refused by the database."""
    rows = _app()._quote_line_rows("q1", [dict(_LINE, extra={"x1": "4 weeks"}), dict(_LINE, extra={"x1": ""})])
    assert rows[0]["extra"] == {"x1": "4 weeks"}
    assert "extra" in rows[1] and rows[1]["extra"] == {"x1": ""}


def test_extra_keeps_only_added_column_keys():
    rows = _app()._quote_line_rows("q1", [dict(_LINE, extra={"x1": "A", "unit_price": "999", "x2": "B" * 300})])
    assert set(rows[0]["extra"]) == {"x1", "x2"}
    assert len(rows[0]["extra"]["x2"]) == 200


def test_a_missing_column_says_the_migration_is_needed():
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        _app()._columns_missing(Exception("Could not find the 'line_columns' column of 'quotations' in the schema cache"))
    assert e.value.status_code == 503 and "add_quote_columns.sql" in e.value.detail
    _app()._columns_missing(Exception("connection reset"))   # anything else: left to the normal handler


# ── the PDF ──────────────────────────────────────────────────────────────────

def _quote(**kw):
    q = {"quote_number": "SPM-00106-CNL", "client": "chevron", "company": "spm",
         "quote_date": "2026-10-30", "currency": "USD", "sub_total": 10.0, "total": 10.0}
    q.update(kw)
    return q


def _text(pdf):
    import fitz
    return " ".join(" ".join(p.get_text().split()) for p in fitz.open(stream=pdf, filetype="pdf"))


def test_the_standard_table_is_unchanged():
    from quotation_pdf import generate_quotation_pdf
    t = _text(generate_quotation_pdf(_quote(), [_LINE]))
    assert "Item Description QTY UOM Unit Price ($) Total ($)" in t


def test_added_and_renamed_columns_print_in_their_order():
    from quotation_pdf import generate_quotation_pdf
    q = _quote(line_columns=[
        {"key": "description", "label": "Material"}, {"key": "x1", "label": "Lead time"},
        {"key": "quantity", "label": ""}, {"key": "unit_price", "label": "Rate"}, {"key": "line_total", "label": ""},
    ])
    t = _text(generate_quotation_pdf(q, [dict(_LINE, extra={"x1": "4 wks & ex-works"})]))
    assert "Material Lead time QTY Rate ($) Total ($)" in t
    assert "4 wks & ex-works" in t
    assert "UOM" not in t.split("Material", 1)[1].split("Sub Total")[0]   # UOM was removed


# ── a second price: FCA and DDP side by side ─────────────────────────────────
# The layout from a real customer form: Qty, UOM, FCA unit/extended, DDP unit/extended.

_FCA_DDP = [
    {"key": "description", "label": "Description"}, {"key": "quantity"}, {"key": "uom"},
    {"key": "unit_price", "label": "FCA Unit Price"}, {"key": "line_total", "label": "FCA Extended Total"},
    {"key": "x1", "label": "DDP Unit Price", "kind": "price"},
    {"key": "x2", "label": "DDP Extended Total", "kind": "total", "of": "x1"},
]
_FCA_DDP_LINES = [
    dict(_LINE, item_no=1, quantity=5, unit_price=2500, line_total=12500, extra={"x1": "4350", "x2": "21750.00"}),
    dict(_LINE, item_no=2, quantity=10, unit_price=700, line_total=7000, extra={"x1": "1,500", "x2": "15000.00"}),
]


def test_a_second_price_pair_is_kept():
    cols = _app()._quote_columns(_FCA_DDP)
    assert cols[5]["kind"] == "price" and cols[6] == {"key": "x2", "label": "DDP Extended Total", "kind": "total", "of": "x1"}


def test_half_a_pair_is_just_text():
    cols = _app()._quote_columns([{"key": "x1", "label": "A", "kind": "price"},
                                  {"key": "x2", "label": "B", "kind": "total", "of": "x9"}])
    assert all("kind" not in c for c in cols)


def test_the_customer_example_prints_both_totals():
    from quotation_pdf import generate_quotation_pdf
    t = _text(generate_quotation_pdf(_quote(sub_total=19500, total=19500, line_columns=_FCA_DDP), _FCA_DDP_LINES))
    assert "4,350.00 21,750.00" in t and "1,500.00 15,000.00" in t
    assert "TOTAL ($) $19,500.00" in t
    assert "DDP EXTENDED TOTAL ($) $36,750.00" in t


def test_the_second_total_takes_the_discount_but_not_the_shipping():
    """36,750 less 10% is 33,075. The $100 shipping is on the first total only:
    a delivered price already includes delivery."""
    from quotation_pdf import generate_quotation_pdf
    q = _quote(sub_total=19500, discount_pct=10, shipping_charges=100, total=17650, line_columns=_FCA_DDP)
    t = _text(generate_quotation_pdf(q, _FCA_DDP_LINES))
    assert "DDP EXTENDED TOTAL ($) $33,075.00" in t


def test_a_renamed_total_column_names_the_grand_total():
    from quotation_pdf import generate_quotation_pdf
    t = _text(generate_quotation_pdf(_quote(sub_total=19500, total=19500, line_columns=_FCA_DDP), _FCA_DDP_LINES))
    assert "FCA EXTENDED TOTAL ($) $19,500.00" in t
    assert "Sub Total" in t


def test_an_unrenamed_total_still_reads_total():
    from quotation_pdf import generate_quotation_pdf
    cols = [{"key": "description"}, {"key": "x1", "label": "Brand"}, {"key": "quantity"},
            {"key": "unit_price"}, {"key": "line_total", "label": ""}]
    t = _text(generate_quotation_pdf(_quote(line_columns=cols), [_LINE]))
    assert "TOTAL ($) $10.00" in t and "BRAND" not in t


def test_the_second_total_worked_by_hand():
    """36,750 - 10% = 33,075; + 5% markup of 36,750 (1,837.50) = 34,912.50;
    + 7.5% VAT = 37,530.94. The $100 shipping stays on the first total only."""
    from quotation_pdf import generate_quotation_pdf
    q = _quote(sub_total=19500, discount_pct=10, markup_pct=5, vat_pct=7.5, shipping_charges=100,
               total=20014.38, line_columns=_FCA_DDP)
    t = _text(generate_quotation_pdf(q, _FCA_DDP_LINES))
    assert "FCA EXTENDED TOTAL ($) $20,014.38" in t
    assert "DDP EXTENDED TOTAL ($) $37,530.94" in t


def test_four_added_columns_on_an_nlng_quote_still_fit():
    from quotation_pdf import generate_quotation_pdf
    cols = [{"key": "description"}, {"key": "product_no"}] + [{"key": f"x{i}", "label": f"Col {i}"} for i in range(1, 5)] \
        + [{"key": "uom"}, {"key": "quantity"}, {"key": "unit_price"}, {"key": "line_total"}]
    line = dict(_LINE, product_no="876040", extra={f"x{i}": f"val{i}" for i in range(1, 5)})
    t = _text(generate_quotation_pdf(_quote(client="nlng", line_columns=cols), [line]))
    for i in range(1, 5):
        assert f"Col {i}" in t and f"val{i}" in t
    assert "876040" in t and "10.00" in t

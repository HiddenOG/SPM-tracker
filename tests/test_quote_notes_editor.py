"""
test_quote_notes_editor.py — Customer Notes is the same editor as a description.

Notes was a plain textarea. It is now the same contenteditable the line-item
descriptions use, so bold, italic and colour work there too. There is no JS
runner in this repo, so these tests check the wiring the browser depends on:
the right class on the element (that is what the format bar, the paste cleaner
and the colour buttons key on), and no leftover .value access, which fails
silently on a div and would quietly stop saving the note.
"""
from pathlib import Path

WEB = Path(__file__).parent.parent / "web"


def _html():
    return (WEB / "index.html").read_text(encoding="utf-8")


def _js():
    return (WEB / "script.js").read_text(encoding="utf-8")


def _css():
    return (WEB / "style.css").read_text(encoding="utf-8")


# ── the element ──────────────────────────────────────────────────────────────

def test_notes_is_no_longer_a_textarea():
    assert 'id="qf-notes"' in _html()
    assert '<textarea class="qf-inp" id="qf-notes"' not in _html()


def test_notes_carries_the_class_the_editor_keys_on():
    """The paste cleaner, the selectionchange handler that shows the format bar
    and the bold/italic/colour buttons all find their target with
    closest('.qf-desc-ce'). Without the class the field is just a plain div."""
    block = _html().split('id="qf-notes"')[0][-400:] + _html().split('id="qf-notes"')[1][:400]
    assert "qf-desc-ce" in block
    assert 'contenteditable="true"' in block


def test_notes_has_no_data_idx():
    """data-idx means "write back into a row of the items table". The note is
    not a row; carrying an index would make it overwrite a line item."""
    seg = _html().split('id="qf-notes"')[1][:300]
    assert "data-idx" not in seg


def test_the_placeholder_moved_to_data_ph():
    """:empty::before reads data-ph. A placeholder= attribute does nothing on a
    contenteditable, so the field would just look blank."""
    assert 'data-ph="Looking forward to your business."' in _html()


def test_the_notes_editor_is_sized_in_css():
    css = _css()
    assert ".qf-notes-ce {" in css
    assert ".qf-desc-ce:empty::before" in css          # the placeholder rule


# ── the wiring ───────────────────────────────────────────────────────────────

def test_nothing_still_treats_notes_as_an_input():
    """_setQf sets .value and gv() reads .value. Both are no-ops on a div: the
    note would load blank and save as an empty string with no error anywhere."""
    js = _js()
    assert "_setQf('qf-notes'" not in js
    assert "gv('qf-notes')" not in js


def test_the_setter_and_getter_exist_and_are_used():
    js = _js()
    assert "function _setQfNotes(" in js
    assert "function _getQfNotes(" in js
    assert js.count("_setQfNotes(") >= 3        # the definition, new quote, load
    assert "notes:             _getQfNotes()," in js


def test_the_note_is_cleaned_on_the_way_in_and_out():
    """Same guard as the descriptions: only <b>, <i>, <u>, <font color> and
    <br> may reach the PDF, and entities are decoded before being re-escaped so
    an "&" does not grow an "amp;" on every save."""
    js = _js()
    setter = js.split("function _setQfNotes(")[1].split("\nfunction ")[0]
    getter = js.split("function _getQfNotes(")[1].split("\nfunction ")[0]
    assert "_ceClean(" in setter and "_ceDecode(" in setter
    assert "_ceClean(" in getter


def test_the_write_back_paths_tolerate_a_cell_with_no_index():
    """The note shares the editor with the items table, so the three places
    that sync a cell into _quoteRows/_poRows are reached with it as the target.
    Each must check the index before indexing, or editing a note throws."""
    js = _js()
    for fn in ("function _ceSyncActive(", "document.addEventListener('paste'"):
        body = js.split(fn)[1][:1200]
        assert "isNaN(idx)" in body, fn


def test_the_line_item_editor_was_not_disturbed():
    html = _js()
    assert 'class="qf-inp qf-td-inp qf-desc-ce" contenteditable="true" data-idx=' in html
    assert "description: _ceClean(r.description||'')" in html

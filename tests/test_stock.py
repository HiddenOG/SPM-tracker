"""
test_stock.py — the stock register.

Stock is a MANUAL register, not a calculated balance, and the reason is in the
data: of 154 warehouse replies on file none carry a count. They say "Completely
delivered" or "PO not in stock" in prose. So the number starts from what
somebody physically counts, and every change is logged so the figure on screen
can still be explained a month later.

The Products catalogue that used to sit beside it was removed (Oct 2026):
Inventory is Stock only.
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


# ── the record ───────────────────────────────────────────────────────────────

def test_the_form_keeps_only_what_is_counted():
    """Part code and location were dropped: everything is in Warri, and most
    of what sits on the shelf is a leftover with no catalogue code at all."""
    rec = _app()._stock_record({"description": "GASKET", "part_code": "CGI030030Y00",
                                "location": "Houston"})
    assert set(rec) == {"description", "uom", "unit_cost", "notes"}


def test_the_default_unit_is_each():
    assert _app()._stock_record({"description": "GASKET"})["uom"] == "EA"
    assert _app()._stock_record({"description": "X", "uom": "pc"})["uom"] == "PC"


def test_a_missing_unit_cost_stays_missing():
    """Priced and priced-at-zero are different things on a stock valuation."""
    assert _app()._stock_record({"unit_cost": ""})["unit_cost"] is None
    assert _app()._stock_record({"unit_cost": "abc"})["unit_cost"] is None
    assert _app()._stock_record({"unit_cost": "16.63"})["unit_cost"] == 16.63


def test_blank_notes_are_null_not_empty():
    assert _app()._stock_record({"description": "X", "notes": "  "})["notes"] is None


def test_numbers_that_are_not_numbers_do_not_raise():
    assert _app()._num_or_none("twelve") is None
    assert _app()._num_or_none(None) is None
    assert _app()._num_or_none("12.5") == 12.5


# ── the schema-gap message ───────────────────────────────────────────────────

def test_a_missing_stock_table_names_stock_not_something_else():
    """It once told somebody their spm_pos table was missing when the missing
    table was stock_items — the name was a default, not read from the error."""
    gap = _app()._schema_gap(Exception(
        "{'code': 'PGRST205', 'message': \"Could not find the table "
        "'public.stock_items' in the schema cache\"}"))
    assert "stock_items" in gap
    assert "create_stock.sql" in gap
    assert "spm_pos" not in gap


def test_each_table_points_at_its_own_migration():
    app = _app()
    assert "create_stock.sql" in app._migration_for("stock_movements")
    assert "create_spm_pos.sql" in app._migration_for("spm_po_line_items")
    assert "the migration for it" in app._migration_for("something_unknown")


def test_a_postgres_relation_error_is_read_too():
    gap = _app()._schema_gap(Exception(
        "{'code': '42P01', 'message': 'relation \"stock_movements\" does not exist'}"))
    assert "stock_movements" in gap


def test_an_ordinary_failure_is_not_blamed_on_the_schema():
    assert _app()._schema_gap(Exception("connection reset by peer")) is None


# ── the files the feature ships as ───────────────────────────────────────────

def test_the_feature_is_three_files_of_its_own():
    """Stock lives in inventory.html/css/js, not spread through index.html,
    style.css and script.js — so it can be read and uploaded on its own."""
    for name in ("inventory.html", "inventory.css", "inventory.js"):
        assert (WEB / name).exists(), name


def test_index_holds_the_sidebar_entry_and_nothing_else():
    """The only part that has to live in index.html is the nav row, because it
    has to sit in the right place among the others."""
    index = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'data-p="stock"' in index
    assert 'data-p="products"' not in index        # Products was removed
    assert 'id="page-stock"' not in index          # the page itself is in the fragment
    assert 'id="stock-form-modal"' not in index


def test_the_fragment_carries_the_pages_and_the_modals():
    fragment = (WEB / "inventory.html").read_text(encoding="utf-8")
    for probe in ('id="page-stock"', 'id="stock-form-modal"', 'id="delete-stock-modal"'):
        assert probe in fragment, probe


def test_products_is_gone_everywhere():
    """Removed on purpose: no page, no script, no endpoint left behind."""
    fragment = (WEB / "inventory.html").read_text(encoding="utf-8")
    script = (WEB / "inventory.js").read_text(encoding="utf-8")
    app = (WEB / "app.py").read_text(encoding="utf-8")
    assert 'id="page-products"' not in fragment
    assert "loadProducts" not in script and "_products" not in script
    assert "/api/products" not in app


def test_every_handler_the_fragment_calls_exists_in_the_script():
    """An inline onclick naming a function that was never exported is a dead
    button, and nothing warns you about it."""
    import re
    fragment = (WEB / "inventory.html").read_text(encoding="utf-8")
    script = (WEB / "inventory.js").read_text(encoding="utf-8")
    handlers = set(re.findall(r'\bon(?:click|input|change)="([A-Za-z_$][\w$]*)\(', fragment))
    handlers -= {"clearTimeout"}
    missing = [h for h in sorted(handlers) if f"window.{h} " not in script
               and f"window.{h}=" not in script]
    assert not missing, missing


def test_the_sidebar_group_toggle_is_exported():
    """index.html calls toggleNavGroup, but it is defined in inventory.js."""
    assert "window.toggleNavGroup" in (WEB / "inventory.js").read_text(encoding="utf-8")


def test_the_dropped_fields_are_gone_everywhere():
    """Part code and location came out of the form, so nothing may still be
    reading or writing them — a half-removed field is worse than either."""
    js  = (WEB / "inventory.js").read_text(encoding="utf-8")
    frag = (WEB / "inventory.html").read_text(encoding="utf-8")
    for gone in ("sf-part-code", "sf-location", "x.part_code", "in_stock"):
        assert gone not in js, gone
        assert gone not in frag, gone


def test_every_table_header_matches_the_row_the_script_renders():
    """Part code and location came out of the form but the headers were missed,
    leaving an 8-column header over 6-column rows. Nothing complained — the
    table just rendered wrong. This counts both sides."""
    import re
    fragment = (WEB / "inventory.html").read_text(encoding="utf-8")
    script   = (WEB / "inventory.js").read_text(encoding="utf-8")

    sections = {
        "page-stock": ("STOCK ITEM MODAL", "function (x) {"),
    }
    for section, (next_marker, row_marker) in sections.items():
        a = fragment.index('id="%s"' % section)
        b = fragment.index(next_marker, a)
        block = fragment[a:b]
        headers = re.findall(r"<th[^>]*>.*?</th>", block)
        spans = set(re.findall(r'colspan="(\d+)"', block))

        i = script.index("body.innerHTML = list.slice(start, start + PER_PAGE).map(" + row_marker)
        row = script[i:script.index("}).join('');", i)]
        cells = row.count("<td")

        assert len(headers) == cells, f"{section}: {len(headers)} headers, {cells} cells"
        assert spans == {str(len(headers))}, f"{section}: colspan {spans} for {len(headers)} columns"


def test_the_stock_table_shows_only_what_is_counted():
    import re
    fragment = (WEB / "inventory.html").read_text(encoding="utf-8")
    a = fragment.index('id="page-stock"')
    b = fragment.index("STOCK ITEM MODAL", a)
    labels = [re.sub(r"<[^>]*>", "", h).strip()
              for h in re.findall(r"<th[^>]*>.*?</th>", fragment[a:b])]
    assert labels == ["ID", "Description", "Qty", "Unit", "Value", "Last counted", ""]


# ── the stock id ─────────────────────────────────────────────────────────────

def test_the_id_is_a_label_a_person_can_say_out_loud():
    assert _app()._stock_id(1) == "STK-0001"
    assert _app()._stock_id(42) == "STK-0042"
    assert _app()._stock_id(12345) == "STK-12345"


def test_the_id_is_not_part_of_the_typed_record():
    """It is issued on insert, never sent by the form — otherwise two people
    counting at once could both claim STK-0042."""
    rec = _app()._stock_record({"description": "GASKET", "stock_id": "STK-9999",
                                "stock_seq": 9999})
    assert "stock_id" not in rec and "stock_seq" not in rec


def test_the_number_comes_from_a_database_sequence():
    """Counting from the surviving rows hands a deleted item's number to the
    next one, and STK-0007 may already be written on a shelf tag. The migration
    defaults the column instead, and the app does not set it on insert."""
    sql = (Path(__file__).parent.parent / "migrations" / "create_stock.sql").read_text(encoding="utf-8")
    assert "create sequence if not exists stock_seq_counter" in sql
    assert "default nextval('stock_seq_counter')" in sql

    src = (WEB / "app.py").read_text(encoding="utf-8")
    create = src[src.index("def create_stock_item("):src.index("def _log_stock_move(")]
    assert 'record["stock_seq"]' not in create, "the app must leave the number to the database"
    assert "res.data[0].get(\"stock_seq\")" in create, "and read back what it was given"


def test_there_is_still_a_fallback_when_the_sequence_is_missing():
    """Someone who ran an older copy of the migration should get a working
    page, not a 500 — just without the guarantee."""
    src = (WEB / "app.py").read_text(encoding="utf-8")
    assert "_fallback_stock_seq" in src


def test_the_form_previews_the_id_but_the_server_issues_it():
    js = (WEB / "inventory.js").read_text(encoding="utf-8")
    assert "function nextStockId()" in js          # the preview
    assert "window.onStockDescription" in js       # fires as the description is typed
    frag = (WEB / "inventory.html").read_text(encoding="utf-8")
    assert 'id="sf-stock-id"' in frag
    assert 'readonly' in frag[frag.index('id="sf-stock-id"') - 80:frag.index('id="sf-stock-id"') + 80]


def test_every_css_class_the_pages_use_is_actually_defined():
    """The KPI cards stacked down the page instead of spreading across it
    because the markup used class names that existed nowhere — kpis/kpi-l/
    kpi-v instead of the app's kpi-grid/kpi-lbl/kpi-val. Nothing errors when a
    class is undefined; the element just renders unstyled."""
    import re
    frag = (WEB / "inventory.html").read_text(encoding="utf-8")
    js   = (WEB / "inventory.js").read_text(encoding="utf-8")
    css  = ((WEB / "style.css").read_text(encoding="utf-8")
            + (WEB / "inventory.css").read_text(encoding="utf-8"))

    used = set()
    for group in re.findall(r'class="([^"]*)"', frag):
        used.update(group.split())
    for group in re.findall(r'class=\?"([^"\+]*)', js):
        used.update(group.split())
    used = {c for c in used if re.fullmatch(r"[a-z][a-z0-9-]*", c)}

    missing = sorted(c for c in used if ("." + c) not in css)
    assert not missing, f"used but never defined: {missing}"


def test_the_summary_cards_sit_in_the_grid_that_spreads_them():
    frag = (WEB / "inventory.html").read_text(encoding="utf-8")
    assert 'class="kpi-grid" id="stock-kpis"' in frag


def test_postgres_and_postgrest_word_a_missing_column_differently():
    """PostgREST says "the 'x' column of 'y'"; Postgres says "column y.x does
    not exist". Only the first was read, so a missing stock_seq produced a
    message naming neither the column nor the migration."""
    app = _app()
    postgres  = app._schema_gap(Exception(
        "{'code': '42703', 'message': 'column stock_items.stock_seq does not exist'}"))
    postgrest = app._schema_gap(Exception(
        "{'code': 'PGRST204', 'message': \"Could not find the 'stock_seq' column "
        "of 'stock_items' in the schema cache\"}"))
    for gap in (postgres, postgrest):
        assert "stock_seq" in gap
        assert "stock_items" in gap
        assert "create_stock.sql" in gap


def test_the_migration_carries_the_statements_the_message_points_at():
    """The message tells people to run the alters at the bottom of the file,
    so they had better be there."""
    sql = (Path(__file__).parent.parent / "migrations" / "create_stock.sql").read_text(encoding="utf-8")
    assert "alter table stock_items add column if not exists stock_id" in sql
    assert "alter table stock_items add column if not exists stock_seq" in sql
    assert "create sequence if not exists stock_seq_counter" in sql


def test_the_lists_page_at_the_same_size_as_the_rest_of_the_app():
    """Orders, quotes and delays all show 100. Stock arrived at 20 and the
    purchase orders at 15 for no reason other than that I picked them."""
    import re
    inventory = (WEB / "inventory.js").read_text(encoding="utf-8")
    script    = (WEB / "script.js").read_text(encoding="utf-8")
    sizes = {
        "stock":           re.search(r"var PER_PAGE\s*=\s*(\d+)", inventory).group(1),
        "orders":          re.search(r"^var PER_PAGE\s*=\s*(\d+)", script, re.M).group(1),
        "quotes":          re.search(r"var _QT_PER_PAGE\s*=\s*(\d+)", script).group(1),
        "purchase orders": re.search(r"var _PO_PER_PAGE\s*=\s*(\d+)", script).group(1),
    }
    assert set(sizes.values()) == {"100"}, sizes


def test_the_stock_table_scrolls_like_every_other_table():
    """Same wrappers as the rest of the app, so it inherits the sticky header
    and the max-height that makes it scroll inside its frame."""
    frag = (WEB / "inventory.html").read_text(encoding="utf-8")
    a = frag.index('id="page-stock"')
    section = frag[a:frag.index("STOCK ITEM MODAL", a)]
    assert 'class="tw-wrap"' in section
    assert 'class="tw"' in section
    assert 'class="qt-tbl"' in section


# ── the things that would lose a warehouse count ─────────────────────────────
# Written before putting this in front of people entering real counts. Each one
# is a way the number somebody walked the warehouse to get could have gone.

def test_a_blank_quantity_is_refused_not_silently_zero():
    """"I did not type a number" and "I counted none" are different statements.
    Recording the second when someone meant the first makes a full shelf read
    as empty."""
    import fastapi
    app = _app()
    for bad in (None, "", "twelve", "  "):
        try:
            app._stock_quantity({"quantity": bad})
            assert False, f"{bad!r} was accepted"
        except fastapi.HTTPException as exc:
            assert exc.status_code == 400
            assert "type 0" in exc.detail


def test_zero_is_a_real_count_and_is_accepted():
    assert _app()._stock_quantity({"quantity": 0}) == 0.0
    assert _app()._stock_quantity({"quantity": "0"}) == 0.0


def test_a_slipped_keyboard_is_refused_before_the_database_sees_it():
    """numeric(14,3) rejects it anyway — but after the person has left."""
    import fastapi
    try:
        _app()._stock_quantity({"quantity": 810555666777})
        assert False, "a phone number was accepted as a count"
    except fastapi.HTTPException as exc:
        assert exc.status_code == 400


def test_true_is_not_a_quantity_of_one():
    """float(True) is 1.0, and a stray true becoming a count of 1 is the kind
    of thing nobody ever tracks down."""
    assert _app()._num_or_none(True) is None
    assert _app()._num_or_none(False) is None


def test_nan_and_infinity_never_reach_the_column():
    app = _app()
    assert app._num_or_none(float("nan")) is None
    assert app._num_or_none(float("inf")) is None
    assert app._num_or_none("1e999") is None


def test_a_failed_history_entry_never_takes_the_count_with_it():
    """A recorded count with a gap in its history beats losing the count — but
    the caller is told so the person hears about it now."""
    src = (WEB / "app.py").read_text(encoding="utf-8")
    log = src[src.index("def _log_stock_move("):src.index("@app.put(\"/api/stock/{item_id}\")")]
    assert "return False" in log and "return True" in log
    assert '"logged": logged' in src or '"logged": logged}' in src


def test_a_second_person_cannot_wipe_the_first_persons_count():
    """Both open the item at 12. One saves 8. The other must not be able to
    save 15 over it — and the history entry would have been wrong twice: a
    change measured from a figure that is no longer there."""
    src = (WEB / "app.py").read_text(encoding="utf-8")
    update = src[src.index("def update_stock_item("):src.index("@app.delete(\"/api/stock/{item_id}\")")]
    assert "seen_quantity" in update
    assert "status_code=409" in update
    js = (WEB / "inventory.js").read_text(encoding="utf-8")
    assert "seen_quantity:" in js, "the form must send what it was opened with"


def test_the_warehouse_cannot_destroy_a_count():
    """Deleting takes every count ever recorded against the item. Correcting a
    miscount is an edit, which keeps the trail."""
    src = (WEB / "app.py").read_text(encoding="utf-8")
    assert '_STOCK_DELETE_ROLES = {"admin", "procurement"}' in src
    delete = src[src.index("def delete_stock_item("):]
    assert "status_code=403" in delete[:900]
    js = (WEB / "inventory.js").read_text(encoding="utf-8")
    assert "function canDelete()" in js, "and the button is hidden from them"


def test_closing_the_form_asks_before_throwing_away_a_count():
    """It is typed once, standing at a shelf. The backdrop, Escape and a
    reloaded tab all used to bin it without a word."""
    js = (WEB / "inventory.js").read_text(encoding="utf-8")
    assert "function confirmDiscard()" in js
    assert "function isDirty()" in js
    assert "beforeunload" in js
    assert "if (!force && !confirmDiscard()) return;" in js


def test_a_saved_item_closes_without_being_asked():
    """The guard must not nag on the one path where nothing is at risk."""
    js = (WEB / "inventory.js").read_text(encoding="utf-8")
    assert "closeStockForm(true);" in js


def test_a_label_that_failed_to_write_is_still_readable():
    """stock_id is written a moment after the insert. A row whose second write
    failed has a number but no label, and a dash cannot be quoted on a tag."""
    src = (WEB / "app.py").read_text(encoding="utf-8")
    assert src.count('row["stock_id"] = _stock_id(int(row["stock_seq"]))') == 1
    assert src.count('item["stock_id"] = _stock_id(int(item["stock_seq"]))') == 1


# ── what the hosting costs ───────────────────────────────────────────────────

def test_the_web_service_runs_one_worker_not_three():
    """UvicornWorker is async — one worker serves many requests at once. The
    (2 x cores) + 1 rule that gives 3 is for sync workers and does not apply.
    Three copies of this app measured 270MB against 90MB for one, held every
    minute of every day for a handful of users."""
    root = Path(__file__).parent.parent
    for name in ("Procfile", "render.yaml"):
        text = (root / name).read_text(encoding="utf-8")
        assert "--workers 1 " in text, name
        assert "--workers 3" not in text, name


def test_every_listener_shares_one_interval_and_it_is_not_two_minutes():
    """At 120s, four listeners made ~2,900 IMAP logins a day on a box billed by
    the minute, for mailboxes that get a few orders a week. One of them also
    had the interval hardcoded, so it never moved with the others."""
    import re
    scripts = (Path(__file__).parent.parent / "scripts")
    found = {}
    for name in ("imap_listener", "gmail_ack_listener", "supplier_po_parser",
                 "flexitallic_history_listener", "warehouse_reply_parser",
                 "pdf_extractor", "ack_pdf_extractor"):
        text = (scripts / f"{name}.py").read_text(encoding="utf-8-sig")
        match = re.search(r'CHECK_INTERVAL_SECONDS",\s*(\d+)', text)
        assert match, f"{name} does not read CHECK_INTERVAL_SECONDS"
        found[name] = int(match.group(1))
    assert set(found.values()) == {600}, found

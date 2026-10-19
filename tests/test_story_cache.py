"""
test_story_cache.py — a PO story is paid for once, not every time it is opened.

Opening a story used to cost a live Gmail thread search and a model call, every
single time. The finished story was already sitting in ai_memory, but it was
only ever fed back into the next prompt as background, never served. Of the 25
stories on file when this was written, not one PO had received an email since
its story was written — so every one of them was being paid for twice.

Now a saved story is served as it stands, with the date it was written, and the
reader asks for a new one when they want it.

Two decisions are worth knowing about, because both look like oversights:

  * "Has a new email arrived?" is NOT used to refresh on its own. The emails
    table only holds mail a listener recognised; the story reads the whole
    Gmail thread. A reply nobody parsed would leave that check saying nothing
    had changed, and the story would go quietly stale. The date on screen is
    the honest version.

  * The AI-key check sits after the saved-story branch, so a story already
    written stays readable when no key is configured.
"""
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
WEB = ROOT / "web"
sys.path.insert(0, str(WEB))
os.environ.setdefault("JWT_SECRET", "test-secret")

import pytest


def _app():
    import app
    return app


# ── the fake database ───────────────────────────────────────────────────────

class _Q:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self.rows = list(store.get(name, []))
        self._excl_null = None

    def select(self, *a, **k):
        return self

    def eq(self, f, v):
        self.rows = [r for r in self.rows if str(r.get(f)) == str(v)]
        return self

    @property
    def not_(self):
        self._negate = True
        return self

    def is_(self, f, _null):
        # Only reached via .not_.is_(f, "null") in this code.
        if getattr(self, "_negate", False):
            self.rows = [r for r in self.rows if r.get(f) is not None]
            self._negate = False
        else:
            self.rows = [r for r in self.rows if r.get(f) is None]
        return self

    def order(self, field, desc=False):
        # Postgres defaults: ASC puts NULLs LAST, DESC puts NULLs FIRST. The
        # fake has to match, or the trap this file exists to guard against
        # cannot be reproduced. A first attempt sorted NULLs last on DESC, and
        # the test for the trap passed against the broken code - caught only by
        # mutating the source and finding the suite did not notice.
        nulls = [r for r in self.rows if r.get(field) is None]
        vals = sorted((r for r in self.rows if r.get(field) is not None),
                      key=lambda r: r.get(field), reverse=bool(desc))
        self.rows = (nulls + vals) if desc else (vals + nulls)
        return self

    def limit(self, n):
        self.rows = self.rows[:n]
        return self

    def execute(self):
        class R:
            pass
        r = R()
        r.data = self.rows
        return r


def _db_with(rows, emails=None):
    store = {"ai_memory": rows, "emails": emails or []}
    return type("C", (), {"table": staticmethod(lambda n: _Q(store, n))})()


@pytest.fixture
def saved_story(monkeypatch):
    """One order with a story written on 17 August."""
    app = _app()
    rows = [{"order_id": "o1", "order_type": "chevron",
             "summary": "Flexitallic acknowledged on 3 September.",
             "updated_at": "2026-08-17T11:29:14.563144+00:00"}]
    monkeypatch.setattr(app, "get_client", lambda: _db_with(rows))
    return app


# ── _get_saved_story ────────────────────────────────────────────────────────

def test_a_saved_story_is_found_with_its_date(saved_story):
    text, when = saved_story._get_saved_story("o1", "chevron")
    assert "Flexitallic acknowledged" in text
    assert when.startswith("2026-08-17")


def test_an_order_with_no_story_returns_empty(saved_story):
    assert saved_story._get_saved_story("nope", "chevron") == ("", "")


def test_the_order_type_is_part_of_the_lookup(saved_story):
    """An NLNG order and a Chevron order can share neither story nor id space —
    the table is keyed on both columns."""
    assert saved_story._get_saved_story("o1", "nlng") == ("", "")


def test_a_database_failure_does_not_take_the_story_down(monkeypatch):
    """A saved story is a nicety. If the lookup fails the endpoint must fall
    through to generating one, not return a 500."""
    app = _app()

    def _boom():
        raise Exception("connection reset by peer")

    monkeypatch.setattr(app, "get_client", _boom)
    assert app._get_saved_story("o1", "chevron") == ("", "")


# ── mail arriving after the story was written ───────────────────────────────

def _story_and_mail(monkeypatch, written, mail_dates, order_type="chevron"):
    app = _app()
    col = "nlng_order_id" if order_type == "nlng" else "order_id"
    rows = [{"order_id": "o1", "order_type": order_type,
             "summary": "A story.", "updated_at": written}]
    emails = [{col: "o1", "received_at": d} for d in mail_dates]
    monkeypatch.setattr(app, "get_client", lambda: _db_with(rows, emails))
    return app


def test_mail_newer_than_the_story_withholds_it(monkeypatch):
    """The reader should not be shown a story written before the newest reply
    landed. Withholding it makes the next open regenerate, once."""
    app = _story_and_mail(monkeypatch, "2026-09-01T10:00:00+00:00",
                          ["2026-08-01T09:00:00", "2026-09-20T09:00:00"])
    assert app._get_saved_story("o1", "chevron") == ("", "")


def test_mail_older_than_the_story_serves_it(monkeypatch):
    app = _story_and_mail(monkeypatch, "2026-09-01T10:00:00+00:00",
                          ["2026-08-01T09:00:00", "2026-08-20T09:00:00"])
    text, _ = app._get_saved_story("o1", "chevron")
    assert text == "A story."


def test_an_order_with_no_recorded_mail_still_serves(monkeypatch):
    """No mail on file is not evidence the story is stale — it usually means
    the listeners never recognised anything for this PO."""
    app = _story_and_mail(monkeypatch, "2026-09-01T10:00:00+00:00", [])
    text, _ = app._get_saved_story("o1", "chevron")
    assert text == "A story."


def test_an_undated_email_does_not_disable_the_whole_check(monkeypatch):
    """The bug this is here for: 222 of 682 emails carry no received_at, and
    Postgres sorts NULLs FIRST on a descending order. Without an explicit
    filter the query returned the undated row, read it as "", and the staleness
    check silently never fired — for 21 of the 25 saved stories."""
    app = _story_and_mail(monkeypatch, "2026-09-01T10:00:00+00:00",
                          [None, "2026-09-20T09:00:00"])
    assert app._newest_email_at(app.get_client(), "o1", "chevron") \
        == "2026-09-20T09:00:00"
    assert app._get_saved_story("o1", "chevron") == ("", "")


def test_only_undated_mail_reads_as_no_mail(monkeypatch):
    app = _story_and_mail(monkeypatch, "2026-09-01T10:00:00+00:00", [None, None])
    assert app._newest_email_at(app.get_client(), "o1", "chevron") == ""
    text, _ = app._get_saved_story("o1", "chevron")
    assert text == "A story."


def test_nlng_orders_are_matched_on_their_own_column(monkeypatch):
    """Chevron mail hangs off order_id, NLNG mail off nlng_order_id. Reading
    the wrong column would make every NLNG story look permanently fresh."""
    app = _story_and_mail(monkeypatch, "2026-09-01T10:00:00+00:00",
                          ["2026-09-20T09:00:00"], order_type="nlng")
    assert app._get_saved_story("o1", "nlng") == ("", "")


def test_a_failed_mail_lookup_still_serves_the_story(monkeypatch):
    """A story slightly out of date beats no story, and the date on screen
    still tells the reader how old it is."""
    app = _app()
    rows = [{"order_id": "o1", "order_type": "chevron",
             "summary": "A story.", "updated_at": "2026-09-01T10:00:00+00:00"}]

    class _Broken(_Q):
        def order(self, *a, **k):
            raise Exception("statement timeout")

    def _client():
        store = {"ai_memory": rows, "emails": [{"order_id": "o1", "received_at": "2026-09-20"}]}
        return type("C", (), {"table": staticmethod(
            lambda n: (_Broken if n == "emails" else _Q)(store, n))})()

    monkeypatch.setattr(app, "get_client", _client)
    text, _ = app._get_saved_story("o1", "chevron")
    assert text == "A story."


# ── noticing mail that no listener caught ───────────────────────────────────
# The gap that was put to me plainly: if the listeners miss an email and the
# story reads the emails table, how would it ever know? It would not. The table
# holds a tenth to a quarter of a thread, so a reply nobody parsed left it
# looking unchanged and the saved story kept being served while the email sat
# unread in Gmail. Nothing in the system would have noticed.
#
# So Gmail is asked, for dates only - measured at 4.0s against the 80s the same
# thread takes to fetch in full.

def _gmail_says(monkeypatch, written, live, table_newest=None):
    app = _app()
    rows = [{"order_id": "o1", "order_type": "chevron",
             "summary": "A story.", "updated_at": written}]
    emails = ([{"order_id": "o1", "received_at": table_newest}]
              if table_newest else [])
    monkeypatch.setattr(app, "get_client", lambda: _db_with(rows, emails))
    import email_thread_sync as ets
    monkeypatch.setattr(ets, "latest_gmail_date",
                        lambda po, so="": live)
    return app


def test_gmail_newer_than_the_story_withholds_it(monkeypatch):
    """Even with nothing newer in the table - which is the whole point."""
    app = _gmail_says(monkeypatch, "2026-09-01T10:00:00+00:00",
                      "2026-09-20T10:00:00+00:00")
    assert app._get_saved_story("o1", "chevron", "PO1") == ("", "")


def test_gmail_with_nothing_newer_serves_the_saved_story(monkeypatch):
    app = _gmail_says(monkeypatch, "2026-09-21T10:00:00+00:00",
                      "2026-09-20T10:00:00+00:00")
    text, _ = app._get_saved_story("o1", "chevron", "PO1")
    assert text == "A story."


def test_gmail_being_unreachable_serves_the_story_rather_than_failing(monkeypatch):
    """None means "could not ask", never "nothing new". The story arrives with
    its date on screen and a Regenerate button, which is the honest fallback -
    refusing to show anything would be worse."""
    app = _gmail_says(monkeypatch, "2026-09-01T10:00:00+00:00", None)
    text, _ = app._get_saved_story("o1", "chevron", "PO1")
    assert text == "A story."


def test_a_raising_gmail_check_also_serves_the_story(monkeypatch):
    app = _app()
    rows = [{"order_id": "o1", "order_type": "chevron",
             "summary": "A story.", "updated_at": "2026-09-01T10:00:00+00:00"}]
    monkeypatch.setattr(app, "get_client", lambda: _db_with(rows, []))
    import email_thread_sync as ets

    def _boom(po, so=""):
        raise Exception("imap timeout")

    monkeypatch.setattr(ets, "latest_gmail_date", _boom)
    text, _ = app._get_saved_story("o1", "chevron", "PO1")
    assert text == "A story."


def test_gmail_is_not_asked_when_the_table_already_knows(monkeypatch):
    """The free check runs first. Asking Gmail costs 4 seconds, so there is no
    reason to when the answer is already on file."""
    app = _app()
    rows = [{"order_id": "o1", "order_type": "chevron",
             "summary": "A story.", "updated_at": "2026-09-01T10:00:00+00:00"}]
    emails = [{"order_id": "o1", "received_at": "2026-09-20T10:00:00"}]
    monkeypatch.setattr(app, "get_client", lambda: _db_with(rows, emails))
    import email_thread_sync as ets
    asked = []
    monkeypatch.setattr(ets, "latest_gmail_date",
                        lambda po, so="": asked.append(po) or None)
    assert app._get_saved_story("o1", "chevron", "PO1") == ("", "")
    assert asked == []


def test_gmail_is_not_asked_without_a_po_number(monkeypatch):
    """There is nothing to search for, and the search would match the whole
    mailbox rather than one thread."""
    app = _app()
    rows = [{"order_id": "o1", "order_type": "chevron",
             "summary": "A story.", "updated_at": "2026-09-01T10:00:00+00:00"}]
    monkeypatch.setattr(app, "get_client", lambda: _db_with(rows, []))
    import email_thread_sync as ets
    asked = []
    monkeypatch.setattr(ets, "latest_gmail_date",
                        lambda po, so="": asked.append(po) or "2026-09-20")
    text, _ = app._get_saved_story("o1", "chevron", "")
    assert text == "A story."
    assert asked == []


def test_the_po_number_is_worked_out_before_the_check_runs():
    """It was not, briefly: the check was moved above the line that looks up
    the PO number, which UnboundLocalError'd on every non-fresh request - every
    story open. Caught before it shipped, but only just."""
    src = (WEB / "app.py").read_text(encoding="utf-8")
    body = src.split("def email_summarize")[1].split("\n@app.")[0]
    assert body.index("_get_po_number(order_id, type)") < \
        body.index("_get_saved_story(order_id, type, po_number")
    # And only looked up once.
    assert body.count("_get_po_number(order_id, type)") == 1


def test_it_is_not_the_same_function_that_feeds_the_prompt():
    """_load_ai_memory mixes in sibling-order context because it is building
    prompt input. Serving that to a reader would show them another order's
    status lines dressed up as their story."""
    app = _app()
    assert hasattr(app, "_get_saved_story")
    assert hasattr(app, "_load_ai_memory")
    src = (WEB / "app.py").read_text(encoding="utf-8")
    body = src.split("def _get_saved_story")[1].split("\ndef ")[0]
    assert "SIBLING ORDERS" not in body


# ── what the endpoint does with it ──────────────────────────────────────────

def _endpoint_src():
    src = (WEB / "app.py").read_text(encoding="utf-8")
    return src.split("def email_summarize")[1].split("\n@app.")[0]


def test_the_saved_story_is_served_before_anything_expensive_runs():
    """Order matters: the saved branch has to come before the Gmail search and
    the model call, or the saving is only in the response, not the bill."""
    body = _endpoint_src()
    saved_at = body.index("_get_saved_story")
    assert "_build_email_context" in body
    assert saved_at < body.index("_build_email_context")
    assert saved_at < body.index("_ai_stream")


def test_a_saved_story_survives_a_missing_ai_key():
    """The key check moved below the saved branch. A story already written
    should still open on a box with no key configured."""
    body = _endpoint_src()
    assert body.index("_get_saved_story") < body.index("No AI key configured")


def test_fresh_is_what_bypasses_the_saved_story():
    body = _endpoint_src()
    assert "fresh: int = 0" in body
    assert "if not fresh:" in body


def test_the_date_is_sent_on_every_path():
    """Saved, in-memory cached, and freshly written all have to report a date,
    or the footer shows nothing and the reader cannot judge the age."""
    body = _endpoint_src()
    assert body.count("saved_at") >= 3


def test_a_new_story_reports_its_date_after_the_text():
    """Sent after the stream, not before: the date belongs to the story being
    written now, and the footer should update once it is complete."""
    body = _endpoint_src()
    tail = body.split("for chunk in _ai_stream")[1]
    assert tail.index("saved_at") < tail.index("[DONE]")


# ── the footer the reader actually sees ─────────────────────────────────────

def test_the_footer_and_button_exist_in_the_markup():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="ai-foot"' in html
    assert 'id="ai-foot-age"' in html
    assert 'onclick="regenerateStory()"' in html


def test_the_footer_classes_are_defined_in_css():
    """Class names have been invented here before and the layout broke
    silently, so every one used is checked against the stylesheet."""
    css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
    for cls in (".ai-foot", ".ai-foot-age", ".ai-regen"):
        assert cls + "{" in css.replace(" {", "{"), cls


def test_the_frontend_asks_for_a_fresh_story_and_shows_the_date():
    js = (ROOT / "web" / "script.js").read_text(encoding="utf-8")
    assert "function regenerateStory(" in js
    assert "function _storyAgeLabel(" in js
    assert "fresh ? '&fresh=1' : ''" in js
    assert "parsed.saved_at" in js


def test_the_regenerate_button_is_re_enabled_even_when_the_call_fails():
    """It is disabled on click so one reader cannot fire off several paid
    regenerations. If a failure left it disabled, the only way back would be a
    page reload."""
    js = (ROOT / "web" / "script.js").read_text(encoding="utf-8")
    load = js.split("function loadStorySummary(")[1].split("\nfunction ")[0]
    assert load.count("rb.disabled = false") >= 2

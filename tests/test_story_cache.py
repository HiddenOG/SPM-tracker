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

    def select(self, *a, **k):
        return self

    def eq(self, f, v):
        self.rows = [r for r in self.rows if str(r.get(f)) == str(v)]
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


def _db_with(rows):
    store = {"ai_memory": rows}
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

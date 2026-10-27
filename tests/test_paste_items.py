"""Paste items into a quote: text copied out of a PDF has lost its table, and
/api/quotations/parse-paste has Claude rebuild it as headings and rows. The
quote form then matches the columns itself, as for a pasted email or Word
table. Claude is faked here."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "scripts"))


def _client(monkeypatch, reply=None, stop="end_turn", raises=None):
    os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
    os.environ.setdefault("SUPABASE_KEY", "fake")
    os.environ.setdefault("JWT_SECRET", "test-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    import anthropic
    import app as APP
    from fastapi.testclient import TestClient
    sent = {}

    class Messages:
        def create(self, **kw):
            sent.update(kw)
            if raises:
                raise raises
            return NS(stop_reason=stop, content=[NS(type="text", text=json.dumps(reply or {}))])

    monkeypatch.setattr(anthropic, "Anthropic", lambda: NS(messages=Messages()))
    APP.app.dependency_overrides[APP.verify_jwt] = lambda: {"sub": "u1", "role": "procurement", "name": "T"}
    return APP, TestClient(APP.app), sent


PDF_TEXT = """Material Description Quantity
requested
876040 GASKET:SW;SZ 16IN;CL150;SS TP316L;GPH 10
GASKET:SPIRAL WOUND
Page 1 of 2"""


def test_pdf_text_comes_back_as_a_table(monkeypatch):
    reply = {"headers": ["Material", "Description", "Quantity requested"],
             "rows": [["876040", "GASKET:SW;SZ 16IN;CL150;SS TP316L;GPH\nGASKET:SPIRAL WOUND", "10"],
                      ["", "", ""]]}                      # a blank row is dropped
    APP, c, sent = _client(monkeypatch, reply)
    try:
        r = c.post("/api/quotations/parse-paste", json={"text": PDF_TEXT})
        assert r.status_code == 200
        assert r.json() == {"headers": reply["headers"], "rows": [reply["rows"][0]]}
        # The answer is held to a strict shape, and the text is sent as copied.
        assert sent["output_config"]["format"]["type"] == "json_schema"
        assert sent["output_config"]["format"]["schema"]["required"] == ["headers", "rows"]
        assert PDF_TEXT in sent["messages"][0]["content"]
        assert sent["model"] == APP.PASTE_MODEL
    finally:
        APP.app.dependency_overrides.clear()


def test_the_prompt_never_lets_it_invent_values():
    import app as APP
    for rule in ("Never invent", "Join all the lines", "Leave out anything that isn't an item row"):
        assert rule in APP._PASTE_PROMPT, rule


def test_bad_pastes_are_explained(monkeypatch):
    APP, c, _ = _client(monkeypatch, {"headers": ["A"], "rows": []})
    try:
        assert c.post("/api/quotations/parse-paste", json={"text": "  "}).status_code == 400
        too_long = c.post("/api/quotations/parse-paste", json={"text": "x" * (APP._PASTE_MAX_CHARS + 1)})
        assert too_long.status_code == 400 and "two parts" in too_long.json()["detail"]
        none = c.post("/api/quotations/parse-paste", json={"text": "hello"})
        assert none.status_code == 422 and "No table" in none.json()["detail"]
    finally:
        APP.app.dependency_overrides.clear()


def test_a_refusal_or_a_cut_off_answer_is_said_plainly(monkeypatch):
    for stop, words in (("refusal", "declined"), ("max_tokens", "two parts")):
        APP, c, _ = _client(monkeypatch, {"headers": ["A"], "rows": [["1"]]}, stop=stop)
        try:
            r = c.post("/api/quotations/parse-paste", json={"text": PDF_TEXT})
            assert r.status_code == 422 and words in r.json()["detail"], stop
        finally:
            APP.app.dependency_overrides.clear()


def test_claude_being_down_is_not_a_crash(monkeypatch):
    import anthropic
    import httpx
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    APP, c, _ = _client(monkeypatch, raises=err)
    try:
        r = c.post("/api/quotations/parse-paste", json={"text": PDF_TEXT})
        assert r.status_code == 502 and "Try again" in r.json()["detail"]
    finally:
        APP.app.dependency_overrides.clear()


def test_no_key_says_so(monkeypatch):
    APP, c, _ = _client(monkeypatch, {"headers": ["A"], "rows": [["1"]]})
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    try:
        assert c.post("/api/quotations/parse-paste", json={"text": PDF_TEXT}).status_code == 503
    finally:
        APP.app.dependency_overrides.clear()

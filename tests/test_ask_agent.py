"""
test_ask_agent.py — SPM Ask: the question loop, its budget, and the Gmail reader.

Everything here runs without Gmail or the Anthropic API. The model is a fake
that replays scripted rounds, Gmail is a fake with the shapes measured on the
real mailbox, and the database is an in-memory stand-in.

What matters most, in order:
  * Citations point at the right emails, and only cited emails are listed.
  * Only email TEXT is fetched. The archiver that fetched whole messages got
    the mailbox throttled within a day.
"""
import base64
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("GMAIL_EMAIL", "x@y.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "x")

import pytest

import ask_agent as A
import mail_search as MS


# ── fakes ───────────────────────────────────────────────────────────────────

class FakeDB:
    def __init__(self, spent=0.0, missing=False):
        self.spent, self.missing, self.logged = spent, missing, []

    def table(self, name):
        db = self

        class Q:
            def select(s, *a, **k): return s
            def gte(s, *a, **k): return s

            def insert(s, row):
                db.logged.append(row)
                return s

            def execute(s):
                if db.missing:
                    raise Exception("relation \"ask_log\" does not exist")
                return NS(data=[{"cost_usd": db.spent}] if db.spent else [])
        return Q()


def text_block(t): return NS(type="text", text=t)
def tool_block(i, name, inp): return NS(type="tool_use", id=i, name=name, input=inp)


def round_(blocks, stop, inp=100, out=50, cr=0, cw=0):
    return NS(content=blocks, stop_reason=stop,
              usage=NS(input_tokens=inp, output_tokens=out,
                       cache_read_input_tokens=cr, cache_creation_input_tokens=cw))


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def __iter__(self):
        for b in self.msg.content:
            if b.type == "text":
                # Stream the text in two pieces, like the real API.
                half = len(b.text) // 2
                for piece in (b.text[:half], b.text[half:]):
                    yield NS(type="content_block_delta", delta=NS(type="text_delta", text=piece))

    def get_final_message(self): return self.msg


class FakeClient:
    def __init__(self, rounds):
        self.rounds, self.calls = list(rounds), []
        self.messages = self

    def stream(self, **kw):
        # Snapshot the conversation as it was sent. The agent keeps appending
        # to the same list, so holding the reference would show later turns.
        kw = dict(kw, messages=list(kw["messages"]))
        self.calls.append(kw)
        return FakeStream(self.rounds.pop(0))


class FakeMail:
    def __init__(self, fail=None):
        self.fail, self.calls = fail, []

    def search(self, q, limit=10):
        self.calls.append(("search", q))
        if self.fail == "search":
            raise MS.MailSearchError("Gmail didn't answer: [THROTTLED]")
        return {"total": 157, "results": [
            {"id": "111", "thread": "900", "date": "16 Oct 2026 09:10",
             "from": "Josephine Davou <jdavou@seplatenergy.com>", "to": "SPM <s@x>",
             "subject": "Overdue PO lines", "snippet": "Please confirm delivery dates for 18 lines.",
             "attachments": []},
            {"id": "222", "thread": "901", "date": "19 Oct 2026 16:18",
             "from": "Connor McKelvie <c@flexitallic.eu>", "to": "SPM <s@x>",
             "subject": "RE: SO720339", "snippet": "Ready to ship, awaiting payment of $41120.65.",
             "attachments": ["statement.pdf"]},
        ]}

    def read(self, mid):
        self.calls.append(("read", mid))
        return {"id": mid, "thread": "900", "date": "16 Oct 2026 09:10",
                "from": "Josephine Davou <jdavou@seplatenergy.com>", "to": "SPM", "cc": "",
                "subject": "Overdue PO lines", "body": "Dear SPM, please confirm dates for 18 lines.",
                "quoted_history_trimmed": True, "cut_at_chars": None, "attachments": []}

    def thread(self, tid, limit=20):
        self.calls.append(("thread", tid))
        return {"thread": tid, "size": 2, "shown": 2, "messages": [
            {"id": "111", "date": "16 Oct", "from": "Josephine", "subject": "s", "snippet": "first",
             "attachments": []},
            {"id": "333", "date": "17 Oct", "from": "SPM", "subject": "s", "snippet": "second",
             "attachments": []}]}


class FakeTracker:
    def __init__(self): self.calls = []
    def find_orders(self, q, limit=20): self.calls.append(("find", q)); return "1 match"
    def order_detail(self, po): self.calls.append(("detail", po)); return "PO detail"
    def orders_stuck(self, days=14, client="", limit=40): self.calls.append(("stuck", days, client)); return "stuck list"
    def search_quotes(self, text="", client="", since="", limit=15): self.calls.append(("quotes", text)); return "quotes"
    def stock_on_hand(self, text="", limit=30): self.calls.append(("stock", text)); return "stock"


def run_all(**kw):
    kw.setdefault("mail", FakeMail())
    kw.setdefault("tracker", FakeTracker())
    kw.setdefault("user", {"sub": "u1", "name": "Godson"})
    events = list(A.run(kw.pop("question", "What's the latest on Seplat?"), **kw))
    return events, kw


def answer_of(events):
    text = ""
    for e in events:
        if e["t"] == "text":
            text += e["d"]
        elif e["t"] == "reset":
            text = ""
    return text


# ── the loop ────────────────────────────────────────────────────────────────

def test_search_then_answer_with_a_citation():
    client = FakeClient([
        round_([tool_block("t1", "search_email", {"query": "seplat newer_than:30d"})], "tool_use"),
        round_([text_block("Seplat is chasing 18 lines [1].")], "end_turn")])
    events, kw = run_all(client=client)
    steps = [e for e in events if e["t"] == "step"]
    assert steps[0]["state"] == "run" and steps[-1]["state"] == "done"
    assert "157" in steps[-1]["detail"]
    assert answer_of(events) == "Seplat is chasing 18 lines [1]."
    src = [e for e in events if e["t"] == "sources"][0]["items"]
    assert [s["id"] for s in src] == ["111"]          # only what was cited
    done = events[-1]
    assert done["t"] == "done" and "cost_usd" not in done and "cost_ngn" not in done


def test_uncited_emails_are_not_listed_as_sources():
    client = FakeClient([
        round_([tool_block("t1", "search_email", {"query": "x"})], "tool_use"),
        round_([text_block("I found nothing relevant.")], "end_turn")])
    events, _ = run_all(client=client)
    assert not [e for e in events if e["t"] == "sources"]


def test_text_written_before_a_tool_call_is_withdrawn():
    """The prompt says not to, but if the model narrates before searching, that
    narration must not end up as part of the answer."""
    client = FakeClient([
        round_([text_block("Let me search."), tool_block("t1", "search_email", {"query": "x"})], "tool_use"),
        round_([text_block("Final.")], "end_turn")])
    events, _ = run_all(client=client)
    assert {"t": "reset"} in events
    assert answer_of(events) == "Final."


def test_all_tool_results_go_back_in_one_message():
    client = FakeClient([
        round_([tool_block("a", "search_email", {"query": "x"}),
                tool_block("b", "find_orders", {"query": "0061"})], "tool_use"),
        round_([text_block("ok")], "end_turn")])
    run_all(client=client)
    sent = client.calls[1]["messages"]
    results = sent[-1]["content"]
    assert sent[-1]["role"] == "user"
    assert [r["tool_use_id"] for r in results] == ["a", "b"]


def test_the_assistant_turn_goes_back_exactly_as_received():
    """Including any thinking blocks: the API expects the turn unaltered."""
    thinking = NS(type="thinking", thinking="", signature="sig")
    first = round_([thinking, tool_block("t1", "search_email", {"query": "x"})], "tool_use")
    client = FakeClient([first, round_([text_block("ok")], "end_turn")])
    run_all(client=client)
    assert client.calls[1]["messages"][-2]["content"] is first.content


def test_a_failing_tool_is_reported_and_the_answer_continues():
    client = FakeClient([
        round_([tool_block("t1", "search_email", {"query": "x"})], "tool_use"),
        round_([text_block("Gmail is busy, try later.")], "end_turn")])
    events, _ = run_all(client=client, mail=FakeMail(fail="search"))
    step = [e for e in events if e["t"] == "step"][-1]
    assert step["state"] == "error" and "THROTTLED" in step["detail"]
    result = client.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True
    assert answer_of(events) == "Gmail is busy, try later."


def test_a_refusal_is_said_plainly():
    client = FakeClient([round_([], "refusal")])
    events, _ = run_all(client=client)
    assert any(e["t"] == "error" and "declined" in e["message"] for e in events)


def test_the_loop_stops_at_the_round_limit():
    rounds = [round_([tool_block(f"t{i}", "find_orders", {"query": "x"})], "tool_use")
              for i in range(A.MAX_ROUNDS)]
    client = FakeClient(rounds)
    events, _ = run_all(client=client)
    assert len(client.calls) == A.MAX_ROUNDS
    assert "search limit" in answer_of(events)


def test_tracker_scope_offers_no_email_tools():
    client = FakeClient([round_([text_block("ok")], "end_turn")])
    run_all(client=client, scope="tracker")
    names = {t["name"] for t in client.calls[0]["tools"]}
    assert "search_email" not in names and "find_orders" in names


def test_the_date_goes_in_the_message_so_the_system_prompt_can_be_cached():
    client = FakeClient([round_([text_block("ok")], "end_turn")])
    run_all(client=client)
    call = client.calls[0]
    assert call["system"] == A.SYSTEM
    assert "Today is" in call["messages"][-1]["content"]
    assert "Today is" not in call["system"]
    assert call["cache_control"] == {"type": "ephemeral"}


def test_history_is_text_only_capped_and_starts_with_the_user():
    hist = ([{"role": "assistant", "content": "orphan"}]
            + [{"role": r, "content": "x" * 5000} for r in ["user", "assistant"] * 6]
            + [{"role": "tool", "content": "never"}])
    clean = A._clean_history(hist)
    assert clean[0]["role"] == "user"
    assert len(clean) <= 8
    assert all(len(m["content"]) <= 4000 for m in clean)
    assert all(m["role"] in ("user", "assistant") for m in clean)


def test_the_model_is_haiku_5_5():
    assert A.MODEL == "claude-haiku-5-5"


# ── tools ───────────────────────────────────────────────────────────────────

def test_the_same_email_keeps_its_reference_number():
    refs = A._Refs()
    mail = FakeMail()
    A.execute("search_email", {"query": "x"}, refs, mail=mail)
    text, _l, _d = A.execute("read_email", {"id": "111"}, refs, mail=mail)
    assert text.startswith("[1]")                    # still number 1
    assert len(refs.items) == 2


def test_search_results_carry_ids_and_attachments():
    text, label, detail = A.execute("search_email", {"query": "seplat"}, A._Refs(), mail=FakeMail())
    assert "id 111" in text and "thread 900" in text
    assert "statement.pdf" in text
    assert label.startswith("Searching the inbox")


def test_tracker_tools_reach_the_tracker():
    t = FakeTracker()
    for name, args in [("find_orders", {"query": "0061"}), ("order_detail", {"po_number": "0061"}),
                       ("orders_stuck", {"days": 21, "client": "chevron"}),
                       ("search_quotes", {"text": "gasket"}), ("stock_on_hand", {"text": "bolt"})]:
        A.execute(name, args, A._Refs(), tracker=t)
    assert [c[0] for c in t.calls] == ["find", "detail", "stuck", "quotes", "stock"]


# ── the Gmail reader: real structures from the live mailbox ─────────────────

class BD(tuple):
    """Stands in for imapclient's BodyData."""
    @property
    def is_multipart(self):
        return isinstance(self[0], list)


# Shapes copied from three real Seplat emails, 20 Oct 2026.
MIXED_WITH_DOCX = BD(([BD(([
    BD((b"TEXT", b"PLAIN", (b"CHARSET", b"UTF-8"), None, None, b"7BIT", 140, 3, None, None, None)),
    BD((b"TEXT", b"HTML", (b"CHARSET", b"UTF-8"), None, None, b"QUOTED-PRINTABLE", 737, 15, None, None, None))],
    b"ALTERNATIVE")),
    BD((b"APPLICATION", b"VND.OPENXMLFORMATS-OFFICEDOCUMENT.WORDPROCESSINGML.DOCUMENT",
        (b"NAME", b"SCHEDULE OF RATES.docx"), b"<f>", None, b"BASE64", 24092, None,
        (b"ATTACHMENT", (b"FILENAME", b"SCHEDULE OF RATES.docx")), None))], b"MIXED"))

ALTERNATIVE_ONLY = BD(([
    BD((b"TEXT", b"PLAIN", (b"CHARSET", b"UTF-8"), None, None, b"QUOTED-PRINTABLE", 18246, 365, None, None, None)),
    BD((b"TEXT", b"HTML", (b"CHARSET", b"UTF-8"), None, None, b"QUOTED-PRINTABLE", 158283, 3166, None, None, None))],
    b"ALTERNATIVE"))

HTML_ONLY = BD((b"TEXT", b"HTML", (b"CHARSET", b"UTF-8"), None, None, b"BASE64", 900, 12, None, None, None))


def test_the_plain_text_part_is_found_inside_nested_parts():
    assert MS._text_part(MIXED_WITH_DOCX)[0] == "1.1"
    assert MS._text_part(ALTERNATIVE_ONLY)[0] == "1"
    assert MS._text_part(ALTERNATIVE_ONLY)[1] == "quoted-printable"


def test_a_single_part_message_reads_part_one_and_html_is_flagged():
    path, enc, cs, is_html = MS._text_part(HTML_ONLY)
    assert path == "1" and enc == "base64" and is_html is True


def test_attachments_are_named_but_never_downloaded():
    assert MS._attachments(MIXED_WITH_DOCX) == ["SCHEDULE OF RATES.docx"]
    src = (ROOT / "scripts" / "mail_search.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.split("\n") if not l.lstrip().startswith("#"))
    # The archiver's mistake: whole messages, attachments and all.
    assert '"RFC822"' not in code and "'RFC822'" not in code
    assert "BODY.PEEK[" in code


def test_a_truncated_base64_part_still_decodes():
    raw = base64.b64encode("Packed and ready for collection.".encode())
    assert "Packed and" in MS._decode(raw[:-3], "base64", "utf-8", False)


def test_quoted_printable_and_html_are_turned_into_plain_text():
    qp = b"Dear Ijala,=0AThe goods are =E2=80=9Cready=E2=80=9D."
    assert MS._decode(qp, "quoted-printable", "utf-8", False) == "Dear Ijala,\nThe goods are “ready”."
    html = b"<html><style>p{}</style><p>Hello&nbsp;<b>Felix</b></p><br>Bye</html>"
    out = MS._decode(html, "", "utf-8", True)
    assert "Hello" in out and "Felix" in out and "<" not in out and "p{}" not in out


def test_the_quoted_reply_history_is_trimmed():
    # Long enough to clear the 150-character floor below which a reply is
    # never trimmed - a "Noted." must not be cut down to nothing.
    body = ("Hi Oghenero, we have the below orders ready to ship but are awaiting a payment "
            "of $41120.65 to cover some overdue invoices. We have received the attached "
            "remittance. Please advise when the balance will be made.\n\n"
            "On Mon, 19 Oct 2026, Special Piping wrote:\n> earlier message " + "x" * 500)
    own, trimmed = MS._own_words(body)
    assert trimmed and "$41120.65" in own and "earlier message" not in own


def test_a_short_message_is_not_trimmed_to_nothing():
    own, trimmed = MS._own_words("Noted.\n\nOn Mon, Penny wrote:\n> long")
    assert own.startswith("Noted.")


def test_search_is_two_round_trips():
    """Round trips are the cost: profiled, a no-op took as long as a search."""
    class Counting:
        def __init__(self): self.n = 0

        def search(self, crit):
            self.n += 1
            return [5, 6]

        def fetch(self, uids, items):
            self.n += 1
            out = {}
            for u in uids:
                out[u] = {b"ENVELOPE": NS(subject=b"Hi", from_=[NS(name=b"A", mailbox=b"a", host=b"x.com")],
                                          to=[], cc=[]),
                          b"BODYSTRUCTURE": ALTERNATIVE_ONLY, b"X-GM-MSGID": u * 10,
                          b"X-GM-THRID": 1, b"INTERNALDATE": None,
                          b"BODY[1]<0>": b"Hello there"}
            return out

    c = Counting()
    MS._conn = c
    try:
        res = MS.search("x", 5)
    finally:
        MS._conn = None
        MS._uid_by_msgid.clear()
    assert c.n == 2
    assert [r["snippet"] for r in res["results"]] == ["Hello there", "Hello there"]


# ── the web endpoint ────────────────────────────────────────────────────────

def _client_as(role, monkeypatch):
    os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
    os.environ.setdefault("SUPABASE_KEY", "fake")
    os.environ.setdefault("JWT_SECRET", "test-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    sys.path.insert(0, str(ROOT / "web"))
    import app as APP
    from fastapi.testclient import TestClient
    APP.app.dependency_overrides[APP.verify_jwt] = lambda: {"sub": "u1", "role": role, "name": "T"}

    # No database here: saving the chat fails at once, which the endpoint must
    # survive (tests/test_ask_chats.py covers saving with a fake database).
    def no_db():
        raise RuntimeError("no database in this test")
    monkeypatch.setattr(APP, "get_client", no_db)
    return APP, TestClient(APP.app)


def test_the_endpoint_refuses_the_warehouse(monkeypatch):
    """Ask reads every client's mail and prices. It is for every role except
    the warehouse; hiding the menu item stops nobody calling the endpoint, so
    the server checks too."""
    APP, c = _client_as("warehouse", monkeypatch)
    try:
        assert c.post("/api/ask", json={"question": "hi"}).status_code == 403
        assert c.get("/api/ask/chats").status_code == 403
    finally:
        APP.app.dependency_overrides.clear()


def test_accounts_and_expeditor_may_ask(monkeypatch):
    for role in ("accounts", "expeditor", "procurement", "admin"):
        APP, c = _client_as(role, monkeypatch)
        try:
            assert c.post("/api/ask", json={"question": "  "}).status_code == 400, role   # past the role check
        finally:
            APP.app.dependency_overrides.clear()


def test_the_endpoint_wants_a_sensible_question(monkeypatch):
    APP, c = _client_as("procurement", monkeypatch)
    try:
        assert c.post("/api/ask", json={"question": "  "}).status_code == 400
        assert c.post("/api/ask", json={"question": "x" * 2001}).status_code == 400
    finally:
        APP.app.dependency_overrides.clear()


def test_the_endpoint_streams_the_agents_events(monkeypatch):
    APP, c = _client_as("admin", monkeypatch)
    seen = {}

    def fake_run(question, history, scope, user):
        seen.update(q=question, scope=scope, history=history)
        yield {"t": "text", "d": "Hello"}
        yield {"t": "done", "secs": 1.0, "rounds": 1}

    monkeypatch.setattr(A, "run", fake_run)
    try:
        r = c.post("/api/ask", json={"question": "Latest on Seplat?", "scope": "nonsense",
                                     "history": [{"role": "user", "content": "x"}]})
        assert r.status_code == 200
        assert '"t": "text"' in r.text and "[DONE]" in r.text
        assert seen["scope"] == "both"          # unknown scope falls back
        assert seen["q"] == "Latest on Seplat?"
        # With no database the answer still came, and the page is told it was not saved.
        assert '"t": "saved"' in r.text and '"ok": false' in r.text
    finally:
        APP.app.dependency_overrides.clear()


def test_the_old_menu_hands_ask_to_the_new_app():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'data-p="ask"' in html
    assert "ask.js" not in html and "ask.css" not in html     # the vanilla page is retired
    js = (ROOT / "web" / "script.js").read_text(encoding="utf-8")
    assert "admin:       ['dashboard','ask'" in js
    assert "procurement: ['dashboard','ask'" in js
    assert "var NEW_APP_PAGES = ['dashboard', 'ask', 'orders', 'delays', 'stock', 'messages', 'reports', 'quotes', 'purchase-orders', 'team', 'settings'];" in js
    assert "window.location.href = '/app/' + name;" in js
    # The new app links back as /#page; the old one opens it after login.
    assert "_openLinkedPage();" in js and "function _openLinkedPage()" in js


def test_the_new_app_is_served_under_app(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import web.app as APP
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<div id=root></div>", encoding="utf-8")
    (tmp_path / "assets" / "index-abc.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path.parent / "secret.txt").write_text("no", encoding="utf-8")
    monkeypatch.setattr(APP, "APP_DIST", tmp_path)
    c = TestClient(APP.app)

    for path in ("/app", "/app/", "/app/ask", "/app/orders/123"):   # the router decides
        r = c.get(path)
        assert r.status_code == 200 and "root" in r.text, path
        assert r.headers["cache-control"] == "no-cache"
    r = c.get("/app/assets/index-abc.js")
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    assert c.get("/app/assets/missing.js").status_code == 404
    assert c.get("/app/assets/..%2F..%2Fsecret.txt").status_code == 404


def test_the_new_app_says_when_it_is_not_built(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import web.app as APP
    monkeypatch.setattr(APP, "APP_DIST", tmp_path / "nothing")
    r = TestClient(APP.app).get("/app/ask")
    assert r.status_code == 404 and "npm run build" in r.text


def test_spending_is_not_tracked_on_the_site():
    """Asked for directly: no meter, no per-answer cost, no spend table. The
    monthly cap belongs on the API key in the Anthropic Console."""
    agent = (ROOT / "scripts" / "ask_agent.py").read_text(encoding="utf-8")
    app = (ROOT / "web" / "app.py").read_text(encoding="utf-8")
    page = "".join(p.read_text(encoding="utf-8")
                   for p in (ROOT / "frontend" / "src" / "pages" / "ask").iterdir())
    assert "ask_log" not in agent and "ask_log" not in app
    assert "/api/ask/usage" not in app and "/api/ask/usage" not in page
    assert "meter" not in page and "₦" not in page and "cost" not in page.lower()
    assert not (ROOT / "migrations" / "create_ask_log.sql").exists()


def test_closing_mid_answer_does_not_raise():
    client = FakeClient([
        round_([tool_block("t1", "search_email", {"query": "seplat"})], "tool_use"),
        round_([text_block("Done [1].")], "end_turn")])
    gen = A.run("q", client=client, mail=FakeMail(), tracker=FakeTracker(), user={"sub": "u1"})
    next(gen)
    gen.close()

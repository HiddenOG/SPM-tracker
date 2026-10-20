"""Ask chat history: every exchange is saved as it streams, everyone who can
use Ask sees every chat, and only the person who started a chat (or an admin)
may rename or delete it. Saving must never cost the answer."""
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "scripts"))


class _Res:
    def __init__(self, data):
        self.data = data


class _Query:
    """Just enough of the Supabase query builder for these endpoints."""

    def __init__(self, db, table):
        self.db, self.table, self.filters, self.op, self.payload = db, table, [], "select", None
        self._order, self._desc = None, False

    def select(self, *_a): self.op = "select"; return self
    def insert(self, rows): self.op, self.payload = "insert", rows; return self
    def update(self, row): self.op, self.payload = "update", row; return self
    def delete(self): self.op = "delete"; return self
    def eq(self, k, v): self.filters.append((k, v)); return self
    def limit(self, _n): return self

    def order(self, k, desc=False):
        self._order, self._desc = k, desc
        return self

    def _match(self, row):
        return all(str(row.get(k)) == str(v) for k, v in self.filters)

    def execute(self):
        if self.db.fail:
            raise RuntimeError(self.db.fail)
        rows = self.db.tables.setdefault(self.table, [])
        if self.op == "insert":
            new = self.payload if isinstance(self.payload, list) else [self.payload]
            out = []
            for r in new:
                r = {"id": str(uuid.uuid4()), "created_at": f"2026-10-20T00:{len(rows):02d}:00", "updated_at": "", **r}
                rows.append(r)
                out.append(r)
            return _Res(out)
        hits = [r for r in rows if self._match(r)]
        if self.op == "update":
            for r in hits:
                r.update(self.payload)
            return _Res(hits)
        if self.op == "delete":
            self.db.tables[self.table] = [r for r in rows if not self._match(r)]
            return _Res(hits)
        if self._order:
            hits = sorted(hits, key=lambda r: str(r.get(self._order) or ""), reverse=self._desc)
        return _Res([dict(r) for r in hits])


class FakeDB:
    def __init__(self):
        self.tables, self.fail = {}, None

    def table(self, name):
        return _Query(self, name)


def _client(monkeypatch, role="procurement", sub="u1", name="Precious"):
    os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
    os.environ.setdefault("SUPABASE_KEY", "fake")
    os.environ.setdefault("JWT_SECRET", "test-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    import app as APP
    import ask_agent as A
    from fastapi.testclient import TestClient
    db = FakeDB()
    monkeypatch.setattr(APP, "get_client", lambda: db)
    APP.app.dependency_overrides[APP.verify_jwt] = lambda: {"sub": sub, "role": role, "name": name}

    def fake_run(question, history, scope, user):
        yield {"t": "step", "id": "s1", "label": "Searching the inbox", "state": "run"}
        yield {"t": "text", "d": "Draft that gets thrown away"}
        yield {"t": "reset"}
        yield {"t": "step", "id": "s1", "label": "Searching the inbox", "state": "done", "detail": "3 found", "secs": 1.2}
        yield {"t": "text", "d": "Seplat's PO shipped "}
        yield {"t": "text", "d": "on 14 Oct [1]."}
        yield {"t": "sources", "items": [{"ref": 1, "id": "123", "subject": "Shipped"}]}
        yield {"t": "done", "secs": 4.5, "rounds": 2}
    monkeypatch.setattr(A, "run", fake_run)
    return APP, TestClient(APP.app), db


def _events(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: {")]


def test_a_new_question_starts_a_chat_and_saves_both_sides(monkeypatch):
    APP, c, db = _client(monkeypatch)
    try:
        r = c.post("/api/ask", json={"question": "What's the latest on Seplat?"})
        evs = _events(r.text)
        assert evs[0]["t"] == "chat"                         # the page learns its chat first
        chats = db.tables["ask_chats"]
        assert len(chats) == 1 and chats[0]["id"] == evs[0]["id"]
        assert chats[0]["title"] == "What's the latest on Seplat?"
        assert chats[0]["created_by_name"] == "Precious"
        user_msg, answer = db.tables["ask_messages"]
        assert user_msg["role"] == "user" and user_msg["content"] == "What's the latest on Seplat?"
        assert answer["role"] == "assistant"
        assert answer["content"] == "Seplat's PO shipped on 14 Oct [1]."   # the reset draft is gone
        assert answer["sources"][0]["subject"] == "Shipped"
        assert answer["steps"] == [{"id": "s1", "label": "Searching the inbox", "state": "done", "detail": "3 found", "secs": 1.2}]
        assert answer["secs"] == 4.5
        assert not any(e["t"] == "saved" for e in evs)       # nothing to warn about
    finally:
        APP.app.dependency_overrides.clear()


def test_a_follow_up_goes_into_the_same_chat(monkeypatch):
    APP, c, db = _client(monkeypatch)
    try:
        first = _events(c.post("/api/ask", json={"question": "First?"}).text)[0]["id"]
        again = _events(c.post("/api/ask", json={"question": "And then?", "chat_id": first}).text)[0]["id"]
        assert again == first
        assert len(db.tables["ask_chats"]) == 1
        assert [m["content"] for m in db.tables["ask_messages"] if m["role"] == "user"] == ["First?", "And then?"]
    finally:
        APP.app.dependency_overrides.clear()


MINE, THEIRS, GONE = (str(uuid.uuid4()) for _ in range(3))


def test_a_chat_deleted_meanwhile_starts_a_fresh_one(monkeypatch):
    APP, c, db = _client(monkeypatch)
    try:
        for stale in (GONE, "not-a-chat-id"):              # deleted, or a mangled link
            evs = _events(c.post("/api/ask", json={"question": "Q?", "chat_id": stale}).text)
            assert evs[0]["t"] == "chat" and evs[0]["id"] != stale
            assert not any(e["t"] == "saved" for e in evs)  # and it still saved
    finally:
        APP.app.dependency_overrides.clear()


def test_a_failed_save_never_costs_the_answer(monkeypatch):
    APP, c, db = _client(monkeypatch)
    db.fail = "{'code': 'PGRST205', 'message': \"Could not find the table 'public.ask_chats' in the schema cache\"}"
    try:
        r = c.post("/api/ask", json={"question": "Q?"})
        evs = _events(r.text)
        assert "".join(e.get("d", "") for e in evs if e["t"] == "text").endswith("on 14 Oct [1].")
        saved = [e for e in evs if e["t"] == "saved"]
        assert saved and saved[0]["ok"] is False
        assert "create_ask_chats.sql" in saved[0]["message"]  # says exactly what to run
    finally:
        APP.app.dependency_overrides.clear()


def test_everyone_sees_every_chat_newest_first(monkeypatch):
    APP, c, db = _client(monkeypatch)
    db.tables["ask_chats"] = [
        {"id": THEIRS, "title": "Old", "created_by_id": "u9", "created_by_name": "Rachel", "updated_at": "2026-10-01"},
        {"id": MINE, "title": "New", "created_by_id": "u1", "created_by_name": "Precious", "updated_at": "2026-10-20"},
    ]
    try:
        rows = c.get("/api/ask/chats").json()
        assert [r["id"] for r in rows] == [MINE, THEIRS]     # someone else's chat is listed too
        assert [r["can_edit"] for r in rows] == [True, False]
    finally:
        APP.app.dependency_overrides.clear()


def test_a_chat_opens_with_its_messages_in_order(monkeypatch):
    APP, c, db = _client(monkeypatch)
    try:
        cid = _events(c.post("/api/ask", json={"question": "Q?"}).text)[0]["id"]
        chat = c.get(f"/api/ask/chats/{cid}").json()
        assert [m["role"] for m in chat["messages"]] == ["user", "assistant"]
        assert chat["can_edit"] is True
        assert c.get(f"/api/ask/chats/{GONE}").status_code == 404
        assert c.get("/api/ask/chats/nope").status_code == 404   # a mangled link, not a crash
    finally:
        APP.app.dependency_overrides.clear()


def test_only_the_starter_or_an_admin_renames_or_deletes(monkeypatch):
    APP, c, db = _client(monkeypatch, sub="u1")
    db.tables["ask_chats"] = [{"id": MINE, "title": "x", "created_by_id": "u1"},
                              {"id": THEIRS, "title": "y", "created_by_id": "u2"}]
    db.tables["ask_messages"] = [{"id": "m1", "chat_id": MINE, "role": "user", "content": "q"}]
    try:
        assert c.patch(f"/api/ask/chats/{MINE}", json={"title": "Seplat"}).status_code == 200
        assert db.tables["ask_chats"][0]["title"] == "Seplat"
        assert c.patch(f"/api/ask/chats/{THEIRS}", json={"title": "no"}).status_code == 403
        assert c.delete(f"/api/ask/chats/{THEIRS}").status_code == 403
        assert c.patch(f"/api/ask/chats/{MINE}", json={"title": "  "}).status_code == 400
        assert c.delete("/api/ask/chats/nope").status_code == 404
        assert c.delete(f"/api/ask/chats/{MINE}").status_code == 200
        assert [r["id"] for r in db.tables["ask_chats"]] == [THEIRS]
        assert db.tables["ask_messages"] == []                # its messages went with it
    finally:
        APP.app.dependency_overrides.clear()
    APP, c, db = _client(monkeypatch, role="admin", sub="boss")
    db.tables["ask_chats"] = [{"id": THEIRS, "title": "y", "created_by_id": "u2"}]
    try:
        assert c.delete(f"/api/ask/chats/{THEIRS}").status_code == 200   # an admin may tidy up
    finally:
        APP.app.dependency_overrides.clear()


def test_the_missing_table_is_named_not_the_hints_guess():
    """PostgREST's hint ("Perhaps you meant parked_emails") comes before its
    message in the error text; the message once named the wrong table."""
    import app as APP
    err = ("{'code': 'PGRST205', 'details': None, 'hint': \"Perhaps you meant the table "
           "'public.parked_emails'\", 'message': \"Could not find the table 'public.ask_chats' in the schema cache\"}")
    gap = APP._schema_gap(RuntimeError(err))
    assert "ask_chats" in gap and "create_ask_chats.sql" in gap and "parked_emails" not in gap


def test_long_questions_make_short_titles():
    import app as APP
    t = APP._ask_title("Which Chevron orders have been waiting on dispatch for more than three weeks and why?")
    assert len(t) <= 71 and t.endswith("…") and not t[:-1].endswith(" ")
    assert APP._ask_title("  Short   one ") == "Short one"


def test_the_migration_makes_both_tables():
    sql = (ROOT / "migrations" / "create_ask_chats.sql").read_text(encoding="utf-8")
    assert "create table if not exists ask_chats" in sql
    assert "create table if not exists ask_messages" in sql
    assert "references ask_chats(id) on delete cascade" in sql

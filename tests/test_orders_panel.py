"""The new app's order panel reads an order's email thread from the database,
never from Gmail: opening a story must not cost the mailbox's IMAP allowance."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _client(monkeypatch):
    os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
    os.environ.setdefault("SUPABASE_KEY", "fake")
    os.environ.setdefault("JWT_SECRET", "test-secret")
    sys.path.insert(0, str(ROOT / "web"))
    sys.path.insert(0, str(ROOT / "scripts"))
    import app as APP
    from fastapi.testclient import TestClient
    APP.app.dependency_overrides[APP.verify_jwt] = lambda: {"sub": "u1", "role": "procurement"}
    return APP, TestClient(APP.app)


def test_stored_thread_comes_from_the_table_not_gmail(monkeypatch):
    APP, c = _client(monkeypatch)
    import email_thread_sync as ets
    seen = {}

    def fake_read(order_id, order_type):
        seen.update(id=order_id, type=order_type)
        return [{"message_id": "m1", "subject": "SO722805", "received_at": "2026-10-19T15:00:00"}]

    def no_gmail(*_a, **_k):
        raise AssertionError("went to Gmail")

    monkeypatch.setattr(ets, "read_thread", fake_read)
    monkeypatch.setattr(APP, "_fetch_gmail_emails", no_gmail)
    try:
        r = c.get("/api/emails/stored", params={"order_id": "abc", "type": "nlng"})
        assert r.status_code == 200
        assert r.json()[0]["message_id"] == "m1"
        assert seen == {"id": "abc", "type": "nlng"}
        # Anything that is not "nlng" is a Chevron order.
        c.get("/api/emails/stored", params={"order_id": "abc", "type": "weird"})
        assert seen["type"] == "chevron"
    finally:
        APP.app.dependency_overrides.clear()

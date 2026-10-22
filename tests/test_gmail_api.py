"""The Gmail API is an addition to IMAP, not a replacement: Ask tries the API
first when it is set up and falls back to IMAP whenever it isn't, or fails.
Both must hand Ask the same shape of result."""
import base64
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gmail_api as G
import mail_search as M


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


# A Gmail API message: text and HTML versions, plus a PDF attachment.
MSG = {
    "id": "18c2f1a0b3d4e5f6", "threadId": "18c2f19f00000000", "internalDate": "1760980800000",
    "payload": {
        "headers": [{"name": "From", "value": '"Sheraine Agbale" <SAgbale@seplat.com>'},
                    {"name": "To", "value": "specialpiping@gmail.com, a@x.com, b@x.com, c@x.com"},
                    {"name": "Subject", "value": "=?utf-8?q?PO_4500_=E2=80=93_gaskets?="}],
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "multipart/alternative", "parts": [
                {"mimeType": "text/plain", "headers": [{"name": "Content-Type", "value": "text/plain; charset=UTF-8"}],
                 "body": {"data": _b64("Please find our PO attached.\nThanks")}},
                {"mimeType": "text/html", "body": {"data": _b64("<p>Please find</p>")}}]},
            {"mimeType": "application/pdf", "filename": "PO-4500.pdf",
             "headers": [{"name": "Content-Disposition", "value": "attachment; filename=PO-4500.pdf"}],
             "body": {"attachmentId": "abc", "size": 90000}},
        ],
    },
}


def test_api_results_have_the_same_shape_as_imap(monkeypatch):
    calls = []

    def fake_get(path, params=None):
        calls.append(path)
        if path == "messages":
            return {"messages": [{"id": MSG["id"], "threadId": MSG["threadId"]}], "resultSizeEstimate": 1}
        return MSG
    monkeypatch.setattr(G, "_get", fake_get)

    r = G.search("PO 4500", 5)
    assert r["total"] == 1
    h = r["results"][0]
    assert set(h) == {"id", "thread", "date", "from", "to", "subject", "snippet", "attachments"}
    assert h["id"] == str(int(MSG["id"], 16))            # decimal, like IMAP's X-GM-MSGID
    assert h["from"] == "Sheraine Agbale <SAgbale@seplat.com>"
    assert h["to"].endswith("(+1 more)")
    assert h["subject"] == "PO 4500 – gaskets"
    assert h["snippet"].startswith("Please find our PO attached.")   # the plain part, not the HTML
    assert h["attachments"] == ["PO-4500.pdf"]
    assert h["date"] == "20 Oct 2025 18:20"                # Nigerian time, as IMAP's

    m = G.read(h["id"])
    assert calls[-1] == f"messages/{MSG['id']}"            # decimal back to Gmail's hex id
    assert m["body"].startswith("Please find our PO") and m["cut_at_chars"] is None


def test_ask_uses_imap_when_the_api_is_not_set_up(monkeypatch):
    for k in ("GMAIL_OAUTH_CLIENT_ID", "GMAIL_OAUTH_CLIENT_SECRET", "GMAIL_OAUTH_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(M, "_imap_search", lambda q, n: {"total": 0, "results": [], "via": "imap"})
    assert M.backend() == "imap"
    assert M.search("x")["via"] == "imap"


def test_ask_falls_back_to_imap_when_the_api_fails(monkeypatch):
    for k in ("GMAIL_OAUTH_CLIENT_ID", "GMAIL_OAUTH_CLIENT_SECRET", "GMAIL_OAUTH_REFRESH_TOKEN"):
        monkeypatch.setenv(k, "set")

    def broken(*a):
        raise G.GmailApiError("Google refused the Gmail sign-in (invalid_grant).")
    monkeypatch.setattr(G, "search", broken)
    monkeypatch.setattr(M, "_imap_search", lambda q, n: {"total": 3, "results": [], "via": "imap"})
    assert M.backend() == "gmail_api"
    assert M.search("x")["via"] == "imap"                   # the answer still comes


def test_ask_uses_the_api_when_it_works(monkeypatch):
    for k in ("GMAIL_OAUTH_CLIENT_ID", "GMAIL_OAUTH_CLIENT_SECRET", "GMAIL_OAUTH_REFRESH_TOKEN"):
        monkeypatch.setenv(k, "set")
    monkeypatch.setattr(G, "search", lambda q, n: {"total": 1, "results": [], "via": "api"})
    monkeypatch.setattr(M, "_imap_search", lambda q, n: (_ for _ in ()).throw(AssertionError("IMAP not needed")))
    assert M.search("x")["via"] == "api"


def test_the_workers_still_read_mail_over_imap():
    """Only Ask's search goes through the API; the listeners are unchanged."""
    for f in ("gmail_ack_listener.py", "warehouse_reply_parser.py", "imap_listener.py"):
        src = (ROOT / "scripts" / f).read_text(encoding="utf-8")
        assert "IMAPClient(" in src and "gmail_api" not in src, f


def test_the_google_key_file_is_never_committed():
    assert "google_client.json" in (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "google_client.json" in (ROOT / ".dockerignore").read_text(encoding="utf-8")


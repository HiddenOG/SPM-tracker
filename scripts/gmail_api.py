"""
gmail_api.py — the Gmail API behind SPM Ask, in place of IMAP.

The same three calls as mail_search.py - search(), read(), thread() - with the
same results, so Ask cannot tell which one answered. mail_search uses this one
whenever it is configured and falls back to IMAP if it fails.

Why the API rather than IMAP:
  * No login per connection (IMAP's took 13-28 seconds); a short-lived access
    token comes from a long-lived refresh token in one quick call.
  * Gmail hands back each part already decoded, with the attachments left out,
    so a message is asked for by id and nothing is fetched by part number -
    which is how IMAP failed every search once ("Some messages could not be
    FETCHed": one message lacked a part that had been asked of all of them).
  * Its own quota, apart from IMAP's 2.5GB a day that the listeners share.

Read-only: the token is granted gmail.readonly and nothing else.

Set up once with scripts/gmail_auth.py, which signs in as the mailbox owner
and writes these to .env (copy them to Railway too):
    GMAIL_OAUTH_CLIENT_ID, GMAIL_OAUTH_CLIENT_SECRET, GMAIL_OAUTH_REFRESH_TOKEN

Message and thread ids are given out in decimal, as IMAP's X-GM-MSGID is, so
saved chats and the Gmail links built from them are the same either way. The
API's own ids are the same numbers in hexadecimal.
"""
from __future__ import annotations

import base64
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import getaddresses

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from email_utils import decode_mime_words

API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class GmailApiError(Exception):
    """The Gmail API could not answer: not set up, refused, or unreachable."""


def configured() -> bool:
    return all(os.environ.get(k) for k in
               ("GMAIL_OAUTH_CLIENT_ID", "GMAIL_OAUTH_CLIENT_SECRET", "GMAIL_OAUTH_REFRESH_TOKEN"))


# ── access token ─────────────────────────────────────────────────────────────
# An access token lasts an hour. One is kept and shared between threads, and
# a fresh one fetched a minute before it runs out (or when Gmail says no).

_lock = threading.Lock()
_token: dict = {"value": "", "until": 0.0}
_http = httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))


def _access_token(force: bool = False) -> str:
    with _lock:
        if not force and _token["value"] and time.time() < _token["until"] - 60:
            return _token["value"]
        if not configured():
            raise GmailApiError("The Gmail API isn't set up on this server (run scripts/gmail_auth.py).")
        try:
            r = _http.post(TOKEN_URL, data={
                "grant_type": "refresh_token",
                "client_id": os.environ["GMAIL_OAUTH_CLIENT_ID"],
                "client_secret": os.environ["GMAIL_OAUTH_CLIENT_SECRET"],
                "refresh_token": os.environ["GMAIL_OAUTH_REFRESH_TOKEN"],
            })
        except httpx.HTTPError as exc:
            raise GmailApiError(f"Couldn't reach Google to sign in: {exc}") from exc
        if r.status_code != 200:
            # invalid_grant: the access was revoked, or the app was left in
            # "Testing" (Google ends those grants after 7 days).
            err = (r.json().get("error") if r.headers.get("content-type", "").startswith("application/json") else "") or r.status_code
            raise GmailApiError(f"Google refused the Gmail sign-in ({err}). Run scripts/gmail_auth.py again.")
        body = r.json()
        _token["value"] = body["access_token"]
        _token["until"] = time.time() + int(body.get("expires_in", 3600))
        return _token["value"]


def _get(path: str, params: dict | None = None) -> dict:
    for attempt in (1, 2):
        token = _access_token(force=attempt == 2)
        try:
            r = _http.get(f"{API}/{path}", params=params, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            raise GmailApiError(f"Gmail didn't answer: {exc}") from exc
        if r.status_code == 401 and attempt == 1:
            continue                                  # token expired early: once more with a new one
        if r.status_code == 404:
            raise GmailApiError("Gmail has no such message or thread.")
        if r.status_code != 200:
            try:
                msg = r.json().get("error", {}).get("message", "")
            except Exception:
                msg = ""
            raise GmailApiError(f"Gmail refused ({r.status_code}): {msg or r.text[:200]}")
        return r.json()
    raise GmailApiError("Gmail refused the sign-in.")


# ── reading a message ────────────────────────────────────────────────────────

def _dec(hex_id: str) -> str:
    try:
        return str(int(hex_id, 16))
    except (TypeError, ValueError):
        return str(hex_id or "")


def _hex(dec_id) -> str:
    s = "".join(ch for ch in str(dec_id or "") if ch.isdigit())
    if not s:
        raise GmailApiError("That isn't a message id.")
    return format(int(s), "x")


def _headers(part: dict) -> dict:
    return {h.get("name", "").lower(): h.get("value", "") for h in (part or {}).get("headers", [])}


def _leaves(part: dict):
    """Every leaf part, depth first - the same order IMAP numbers them."""
    kids = part.get("parts") or []
    if kids:
        for k in kids:
            yield from _leaves(k)
    else:
        yield part


def _charset(part: dict) -> str:
    ct = _headers(part).get("content-type", "")
    for bit in ct.split(";")[1:]:
        k, _, v = bit.strip().partition("=")
        if k.lower() == "charset":
            return v.strip().strip('"') or "utf-8"
    return "utf-8"


def _is_file(p: dict) -> bool:
    """An attachment - the same rule as mail_search's _attachments()."""
    mime = (p.get("mimeType") or "").lower()
    name = p.get("filename") or ""
    disp = _headers(p).get("content-disposition", "").lower()
    return bool(name) and (disp.startswith("attachment") or not mime.startswith(("text/", "image/")))


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _text_and_attachments(payload: dict) -> tuple[bytes | None, str, bool, list[str]]:
    """(body bytes, charset, is_html, attachment names). Plain text first,
    HTML only if that is all there is - as mail_search's _text_part()."""
    leaves = list(_leaves(payload or {}))
    files = [p["filename"] for p in leaves if _is_file(p)]
    html = None
    for p in leaves:
        mime = (p.get("mimeType") or "").lower()
        disp = _headers(p).get("content-disposition", "").lower()
        data = (p.get("body") or {}).get("data")
        if p.get("filename") or disp.startswith("attachment") or not data:
            continue
        if mime == "text/plain":
            return _b64(data), _charset(p), False, files
        if mime == "text/html" and html is None:
            html = p
    if html is not None:
        return _b64(html["body"]["data"]), _charset(html), True, files
    return None, "utf-8", False, files


def _addr_list(value: str, limit: int | None = None) -> str:
    pairs = getaddresses([value or ""])
    out = []
    for name, email in pairs:
        name = decode_mime_words(name).strip().strip('"') if name else ""
        if not (name or email):
            continue
        out.append(f"{name} <{email}>" if name and email else (name or email))
    if limit is None:
        return out[0] if out else ""
    more = len(out) - limit
    return ", ".join(out[:limit]) + (f" (+{more} more)" if more > 0 else "")


def _hit(msg: dict, nbytes: int) -> dict:
    """One API message as mail_search's hit dict, with its text (not yet cut)."""
    import mail_search as M
    payload = msg.get("payload") or {}
    h = _headers(payload)
    raw, charset, is_html, files = _text_and_attachments(payload)
    text = M._decode(raw[:nbytes], "", charset, is_html) if raw else ""
    try:
        when = datetime.fromtimestamp(int(msg.get("internalDate", 0)) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        when = None
    return {
        "id": _dec(msg.get("id")),
        "thread": _dec(msg.get("threadId")),
        "date": M._when(when),
        "from": _addr_list(h.get("from", "")),
        "to": _addr_list(h.get("to", ""), 3),
        "cc": _addr_list(h.get("cc", ""), 4),
        "subject": decode_mime_words(h.get("subject", "")),
        "text": text,
        "attachments": files,
    }


def _fetch_many(ids: list[str]) -> dict:
    """Full messages by API id, fetched side by side (each is one quick call)."""
    if not ids:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(ids))) as pool:
        got = list(pool.map(lambda i: _safe_get(f"messages/{i}", {"format": "full"}), ids))
    return {i: m for i, m in zip(ids, got) if m}


def _safe_get(path, params):
    # One unreadable message must not sink the rest of the search.
    try:
        return _get(path, params)
    except GmailApiError as exc:
        print(f"  [warn] gmail_api: {path} skipped: {exc}")
        return None


# ── the public functions (same shape as mail_search's) ──────────────────────

def search(query: str, limit: int = 10) -> dict:
    import mail_search as M
    query = (query or "").strip()
    if not query:
        raise GmailApiError("Empty search.")
    limit = max(1, min(int(limit or 10), M.MAX_HITS))
    listing = _get("messages", {"q": query, "maxResults": limit})
    ids = [m["id"] for m in listing.get("messages", [])]          # newest first
    msgs = _fetch_many(ids)
    results = []
    for i in ids:
        if i not in msgs:
            continue
        h = _hit(msgs[i], M.SNIPPET_BYTES)
        own, _cut = M._own_words(h.pop("text"))
        h["snippet"] = " ".join(own.split())[:M.SNIPPET_CHARS]
        h.pop("cc", None)
        results.append(h)
    # Gmail only estimates the total, but never below what it just returned.
    total = max(int(listing.get("resultSizeEstimate") or 0), len(ids))
    return {"total": total, "results": results, "estimated": True}


def read(message_id: str) -> dict:
    import mail_search as M
    msg = _get(f"messages/{_hex(message_id)}", {"format": "full"})
    h = _hit(msg, M.READ_BYTES)
    body, trimmed = M._own_words(h.pop("text"))
    cut = len(body) > M.READ_CHARS
    h["body"] = (body[:M.READ_CHARS] if cut else body) or "(no readable text in this message)"
    h["quoted_history_trimmed"] = trimmed
    h["cut_at_chars"] = M.READ_CHARS if cut else None
    return h


def thread(thread_id: str, limit: int = 20) -> dict:
    import mail_search as M
    limit = max(1, min(int(limit or 20), 30))
    tid = _hex(thread_id)
    t = _get(f"threads/{tid}", {"format": "full"})
    all_msgs = t.get("messages") or []                             # oldest first
    if not all_msgs:
        raise GmailApiError(f"No thread with id {_dec(tid)}.")
    msgs = []
    for m in all_msgs[-limit:]:
        h = _hit(m, M.SNIPPET_BYTES)
        own, _cut = M._own_words(h.pop("text"))
        h["snippet"] = " ".join(own.split())[:M.SNIPPET_CHARS]
        h.pop("cc", None)
        h.pop("thread", None)
        msgs.append(h)
    return {"thread": _dec(tid), "size": len(all_msgs), "shown": len(msgs), "messages": msgs}


def attachment(message_id: str, filename: str = "") -> dict:
    """The text of one attachment, fetched on its own by Gmail's attachment id."""
    import mail_attachments as MA
    mid = _hex(message_id)
    msg = _get(f"messages/{mid}", {"format": "full"})
    files = [p for p in _leaves(msg.get("payload") or {}) if p.get("filename")]
    try:
        i = MA.pick([p["filename"] for p in files], filename)
    except MA.AttachmentError as exc:
        raise GmailApiError(str(exc)) from exc
    part = files[i]
    body = part.get("body") or {}
    if int(body.get("size") or 0) > MA.MAX_FILE_BYTES:
        raise GmailApiError(f"{part['filename']} is too big to read here.")
    data = body.get("data")
    if not data and body.get("attachmentId"):
        data = _get(f"messages/{mid}/attachments/{body['attachmentId']}").get("data")
    if not data:
        raise GmailApiError(f"Gmail sent no content for {part['filename']}.")
    try:
        return MA.extract_text(_b64(data), part["filename"], part.get("mimeType") or "")
    except MA.AttachmentError as exc:
        raise GmailApiError(str(exc)) from exc


def profile() -> dict:
    """Which mailbox the token opens, and how big it is. For checking set-up."""
    return _get("profile")

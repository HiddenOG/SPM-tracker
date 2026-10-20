"""
mail_search.py — Gmail as the search engine behind SPM Ask.

The inbox holds 294,607 messages, about 870 GB with attachments. Copying that
anywhere would take days, cost a paid database plan, and always lag behind, so
nothing is copied. Gmail already indexes every word; this module asks it.

Measured on the live mailbox: a keyword search across everything takes 10-14
seconds whatever the hit count, and reading the start of 20 matches about 14.
The two things that keep it that fast are both here:

  * One connection, kept open. A fresh login took 13-28 seconds, more than the
    search itself, so the connection is held and reused, behind a lock because
    the web app answers requests on several threads and an IMAP connection is
    not safe to share between them at once.

  * Only the text part is downloaded. A message averages about 3 MB with its
    attachments. BODYSTRUCTURE says where the plain-text part sits - often
    "1.1", inside a multipart/alternative inside a multipart/mixed - and only
    that part is fetched, and only the first few KB of it for a search hit.

Two functions do the work: search() returns hits with a short snippet each,
read() returns one message in full plus the shape of its thread.
"""
from __future__ import annotations

import base64
import html as _html
import os
import quopri
import re
import sys
import threading
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from email_utils import decode_mime_words

# Nigeria is UTC+1 all year, with no daylight saving.
_WAT = timezone(timedelta(hours=1))

SNIPPET_BYTES = 3_000      # of the text part, per search hit
SNIPPET_CHARS = 480        # shown to the model per hit
READ_BYTES    = 80_000     # of the text part, for a full read
READ_CHARS    = 12_000     # shown to the model for a full read
MAX_HITS      = 15


class MailSearchError(Exception):
    """Gmail could not be asked - not configured, unreachable or refused."""


_lock = threading.Lock()
_conn = None


def _open():
    import email_thread_sync as ets
    imap = ets.open_imap()
    if imap is None:
        raise MailSearchError("Gmail isn't configured on this server "
                              "(GMAIL_EMAIL / GMAIL_APP_PASSWORD).")
    # Timezone-aware dates rather than the library's default of converting
    # them to the server's local time, which on Railway is UTC.
    imap.normalise_times = False
    return imap


def _with_imap(fn):
    """Run fn(imap) on the shared connection, reconnecting once if it died."""
    global _conn
    with _lock:
        for attempt in (1, 2):
            try:
                if _conn is None:
                    _conn = _open()
                return fn(_conn)
            except MailSearchError:
                raise
            except Exception as exc:
                # A dropped connection surfaces as any of a dozen socket and
                # imaplib errors. Throw it away and try once on a fresh one.
                try:
                    _conn.logout()
                except Exception:
                    pass
                _conn = None
                # UIDs learned on the old connection are not trusted on a new
                # one: if the folder was rebuilt they would point elsewhere.
                _uid_by_msgid.clear()
                if attempt == 2:
                    raise MailSearchError(f"Gmail didn't answer: {exc}") from exc


# ── reading a message's structure ────────────────────────────────────────────

def _s(v) -> str:
    if v is None:
        return ""
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)


def _params(p) -> dict:
    """BODYSTRUCTURE params arrive flat: (b'CHARSET', b'UTF-8', b'NAME', ...)."""
    out = {}
    if isinstance(p, (list, tuple)):
        for i in range(0, len(p) - 1, 2):
            out[_s(p[i]).lower()] = _s(p[i + 1])
    return out


def _walk(bs, path=""):
    """Yield (path, mime, params, encoding, disposition) for every leaf part.

    A multipart node is ([child, child, ...], subtype, ...). Its children are
    numbered from 1, with a dot between levels - which is also how IMAP names
    them in BODY[...] - and a single-part message's only part is "1".
    """
    if getattr(bs, "is_multipart", False) or (
            isinstance(bs, (list, tuple)) and bs and isinstance(bs[0], list)):
        for i, child in enumerate(bs[0], start=1):
            yield from _walk(child, f"{path}.{i}" if path else str(i))
        return
    mime = f"{_s(bs[0]).lower()}/{_s(bs[1]).lower()}"
    params = _params(bs[2]) if len(bs) > 2 else {}
    enc = _s(bs[5]).lower() if len(bs) > 5 else ""
    disp = ""
    filename = ""
    # Disposition sits at 9 for text parts (which carry a line count at 7) and
    # at 8 for everything else.
    for idx in (8, 9):
        if len(bs) > idx and isinstance(bs[idx], (list, tuple)) and bs[idx]:
            disp = _s(bs[idx][0]).lower()
            dp = _params(bs[idx][1]) if len(bs[idx]) > 1 else {}
            filename = dp.get("filename", "")
            break
    filename = filename or params.get("name", "")
    yield (path or "1", mime, params, enc, disp, filename)


def _text_part(bs):
    """The part to read: plain text first, HTML if that is all there is."""
    best = None
    for path, mime, params, enc, disp, filename in _walk(bs):
        if disp == "attachment" or filename and not mime.startswith("text/"):
            continue
        if mime == "text/plain":
            return path, enc, params.get("charset", "utf-8"), False
        if mime == "text/html" and best is None:
            best = (path, enc, params.get("charset", "utf-8"), True)
    return best


def _attachments(bs) -> list[str]:
    out = []
    for _p, mime, _params_, _enc, disp, filename in _walk(bs):
        if filename and (disp == "attachment" or not mime.startswith(("text/", "image/"))):
            out.append(filename)
    return out


def _decode(raw: bytes, enc: str, charset: str, is_html: bool) -> str:
    """Decode a (possibly truncated) part body to plain text."""
    if enc == "base64":
        # A partial fetch cuts mid-quantum; decode only whole 4-char groups.
        clean = re.sub(rb"[^A-Za-z0-9+/=]", b"", raw)
        clean = clean[: len(clean) - len(clean) % 4]
        try:
            raw = base64.b64decode(clean)
        except Exception:
            raw = b""
    elif enc == "quoted-printable":
        raw = quopri.decodestring(raw)
    try:
        text = raw.decode(charset or "utf-8", "replace")
    except LookupError:
        text = raw.decode("utf-8", "replace")
    if is_html:
        text = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = _html.unescape(text)
    text = text.replace("\r", "")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


# Where a reply's own words stop and the quoted history begins. Cutting there
# keeps a long chain from being read over and over as each reply quotes the
# whole thread beneath it; the earlier messages are their own search hits.
_QUOTE_MARKERS = [
    re.compile(r"\n\s*On .{6,160}?wrote:\s*\n", re.S),
    re.compile(r"\n-{2,}\s*Original Message\s*-{2,}", re.I),
    re.compile(r"\n\s*From:\s.{1,200}\n\s*(Sent|Date):\s", re.I),
    re.compile(r"\n-{5,} Forwarded message -{5,}", re.I),
]


def _own_words(text: str) -> tuple[str, bool]:
    cut = len(text)
    for rx in _QUOTE_MARKERS:
        m = rx.search(text)
        if m and 150 < m.start() < cut:
            cut = m.start()
    return text[:cut].strip(), cut < len(text)


def _when(v) -> str:
    if not isinstance(v, datetime):
        return ""
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(_WAT).strftime("%d %b %Y %H:%M")


def _addr(lst) -> str:
    if not lst:
        return ""
    a = lst[0]
    name = decode_mime_words(_s(a.name)) if a.name else ""
    email = f"{_s(a.mailbox)}@{_s(a.host)}" if a.mailbox else ""
    return f"{name} <{email}>" if name and email else (name or email)


def _addrs(lst, limit=4) -> str:
    out = []
    for a in (lst or [])[:limit]:
        out.append(_addr([a]))
    more = len(lst or []) - limit
    return ", ".join(out) + (f" (+{more} more)" if more > 0 else "")


def _subject(env) -> str:
    return decode_mime_words(_s(env.subject)) if env and env.subject else ""


# Round trips are the cost, not Gmail's work. Profiled from the office: a
# no-op took 12.8 seconds, the same as a full search, so every command costs
# the same whatever it does. Everything below is arranged to make as few as
# possible.
#
# The text part sits at one of three places in nearly every message seen -
# "1" for a single-part message, "1.1" inside multipart/alternative, "1.1.1"
# when that is wrapped again in multipart/mixed. Asking for all three in the
# same FETCH as the structure turns "fetch structure, then fetch text by path"
# into one trip; a message whose text lives anywhere else costs a second.
_LIKELY_PATHS = ("1", "1.1", "1.1.1")

_META = ["ENVELOPE", "BODYSTRUCTURE", "X-GM-MSGID", "X-GM-THRID", "INTERNALDATE"]

# Gmail message id -> UID, learned from every search on this connection. A
# read() of something just found then needs no search of its own, which saved
# a whole round trip. UIDs are stable while the folder's UIDVALIDITY holds,
# and the map is dropped with the connection whenever that is replaced.
_uid_by_msgid: dict = {}


def _body_of(v: dict, path: str):
    for k, body in v.items():
        if isinstance(k, bytes) and k.startswith(f"BODY[{path}]".encode()) and isinstance(body, bytes):
            return body
    return None


def _fetch_with_text(imap, uids: list, nbytes: int) -> dict:
    """Structure, headers and the text part of each message, in one trip.

    Returns {uid: {"meta": {...}, "text": bytes|None, "part": (path, enc,
    charset, is_html)|None}}. Messages whose text part sits outside the usual
    three places get a second, targeted fetch.
    """
    items = _META + [f"BODY.PEEK[{p}]<0.{nbytes}>" for p in _LIKELY_PATHS]
    data = imap.fetch(uids, items)
    out, missing = {}, {}
    for uid, v in data.items():
        tp = _text_part(v.get(b"BODYSTRUCTURE"))
        body = _body_of(v, tp[0]) if tp else None
        out[uid] = {"meta": v, "text": body, "part": tp}
        if tp and body is None:
            missing[uid] = tp[0]
        mid = v.get(b"X-GM-MSGID")
        if mid is not None:
            _uid_by_msgid[str(mid)] = uid
    if missing:
        by_path: dict = {}
        for uid, path in missing.items():
            by_path.setdefault(path, []).append(uid)
        for path, group in by_path.items():
            extra = imap.fetch(group, [f"BODY.PEEK[{path}]<0.{nbytes}>"])
            for uid, v in extra.items():
                out[uid]["text"] = _body_of(v, path)
    return out


# ── the public functions ─────────────────────────────────────────────────────

def _hit(uid, rec, nchars) -> dict:
    v = rec["meta"]
    env = v.get(b"ENVELOPE")
    text = ""
    if rec["text"] is not None and rec["part"]:
        _p, enc, cs, is_html = rec["part"]
        text = _decode(rec["text"], enc, cs, is_html)
    return {
        "id": str(v.get(b"X-GM-MSGID")),
        "thread": str(v.get(b"X-GM-THRID")),
        "date": _when(v.get(b"INTERNALDATE")),
        "from": _addr(env.from_ if env else None),
        "to": _addrs(env.to if env else None, 3),
        "cc": _addrs(env.cc if env else None, 4),
        "subject": _subject(env),
        "text": text,
        "attachments": _attachments(v.get(b"BODYSTRUCTURE")),
    }


def search(query: str, limit: int = 10) -> dict:
    """Search the whole mailbox with Gmail's own syntax. Newest first.

    Two round trips: the search, then one fetch for headers, structure and the
    start of each hit's text. Returns {"total": n, "results": [{"id", "thread",
    "date", "from", "to", "subject", "snippet", "attachments"}...]}. `id` is
    Gmail's message id, stable for the life of the message.
    """
    query = (query or "").strip()
    if not query:
        raise MailSearchError("Empty search.")
    limit = max(1, min(int(limit or 10), MAX_HITS))

    def run(imap):
        uids = imap.search(["X-GM-RAW", query])
        total = len(uids)
        # UIDs in All Mail rise with arrival, so the highest are the newest.
        pick = sorted(uids)[-limit:][::-1]
        if not pick:
            return {"total": 0, "results": []}
        recs = _fetch_with_text(imap, pick, SNIPPET_BYTES)
        results = []
        for uid in pick:
            if uid not in recs:
                continue
            h = _hit(uid, recs[uid], SNIPPET_CHARS)
            own, _cut = _own_words(h.pop("text"))
            h["snippet"] = " ".join(own.split())[:SNIPPET_CHARS]
            h.pop("cc", None)
            results.append(h)
        return {"total": total, "results": results}

    return _with_imap(run)


def read(message_id: str) -> dict:
    """One message in full. One round trip when it came from a search here."""
    mid = re.sub(r"\D", "", str(message_id or ""))
    if not mid:
        raise MailSearchError("That isn't a message id.")

    def run(imap):
        uid = _uid_by_msgid.get(mid)
        if uid is None:
            found = imap.search(["X-GM-MSGID", mid])
            if not found:
                raise MailSearchError(f"No message with id {mid}.")
            uid = found[0]
        recs = _fetch_with_text(imap, [uid], READ_BYTES)
        if uid not in recs:
            # The cached UID no longer points at anything; look it up afresh.
            _uid_by_msgid.pop(mid, None)
            found = imap.search(["X-GM-MSGID", mid])
            if not found:
                raise MailSearchError(f"No message with id {mid}.")
            uid = found[0]
            recs = _fetch_with_text(imap, [uid], READ_BYTES)
        h = _hit(uid, recs[uid], READ_CHARS)
        body, trimmed = _own_words(h.pop("text"))
        cut = len(body) > READ_CHARS
        h["body"] = (body[:READ_CHARS] if cut else body) or "(no readable text in this message)"
        h["quoted_history_trimmed"] = trimmed
        h["cut_at_chars"] = READ_CHARS if cut else None
        return h

    return _with_imap(run)


def thread(thread_id: str, limit: int = 20) -> dict:
    """Every message in a Gmail thread, oldest first, with a snippet each.

    Its own call rather than part of read(), because it costs a search - about
    as much as everything else read() does together - and most questions are
    answered without it.
    """
    tid = re.sub(r"\D", "", str(thread_id or ""))
    if not tid:
        raise MailSearchError("That isn't a thread id.")
    limit = max(1, min(int(limit or 20), 30))

    def run(imap):
        uids = sorted(imap.search(["X-GM-THRID", tid]))
        if not uids:
            raise MailSearchError(f"No thread with id {tid}.")
        pick = uids[-limit:]
        recs = _fetch_with_text(imap, pick, SNIPPET_BYTES)
        msgs = []
        for uid in pick:
            if uid not in recs:
                continue
            h = _hit(uid, recs[uid], SNIPPET_CHARS)
            own, _cut = _own_words(h.pop("text"))
            h["snippet"] = " ".join(own.split())[:SNIPPET_CHARS]
            h.pop("cc", None)
            h.pop("thread", None)
            msgs.append(h)
        return {"thread": tid, "size": len(uids), "shown": len(msgs), "messages": msgs}

    return _with_imap(run)

"""
test_email_thread_sync.py — the email thread archiver.

The PO story is written from an order's whole Gmail conversation, and used to
fetch it live on every telling. The listeners do write to the emails table, but
only for mail they recognise, which measured against a live search came to a
tenth to a quarter of each thread:

    PO 0061340563    2 of 5        PO 0061367679   33 of 126
    PO 0061365256   16 of 145

Too little to write a story from, and too little for "has new mail arrived?" to
be a reliable question. This module walks the whole thread and stores it, on a
worker thread, so the story reads the table instead.

What these tests protect, in order of how badly each would hurt:

  * The fallback. An order the archiver has not reached yet must still get its
    story, from Gmail, or deploying this before the backfill finishes would
    leave orders with no story at all.
  * Thread ordering. Undated mail has to sort FIRST. The story prompt tells the
    model the last email is the most recent, so an undated one at the end would
    be read as the latest development.
  * The NLNG column. Mail stored against the wrong column attaches to nothing.
  * Terminal orders. A delivered order's thread cannot change; re-walking it
    every six hours forever is most of the Gmail traffic for no gain.
"""
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "web"))
os.environ.setdefault("GMAIL_EMAIL", "x@y.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "x")

import pytest

import email_thread_sync as ets


# ── fakes ───────────────────────────────────────────────────────────────────

class _Q:
    def __init__(self, store, name):
        self.store, self.name = store, name
        if name not in store:
            raise Exception("{'code': 'PGRST205', 'message': \"Could not find "
                            "the table 'public.%s' in the schema cache\"}" % name)
        self.rows = list(store[name])
        self.op = "select"
        self.payload = None

    def select(self, *a, **k):
        return self

    def upsert(self, payload, on_conflict=None):
        self.op, self.payload, self.conflict = "upsert", payload, on_conflict
        return self

    def eq(self, f, v):
        self.rows = [r for r in self.rows if str(r.get(f)) == str(v)]
        return self

    def execute(self):
        class R:
            pass
        r = R()
        if self.op == "upsert":
            keys = (self.conflict or "").split(",")
            rows = self.payload if isinstance(self.payload, list) else [self.payload]
            for row in rows:
                match = None
                for existing in self.store[self.name]:
                    if keys and all(existing.get(k) == row.get(k) for k in keys):
                        match = existing
                        break
                if match:
                    match.update(row)
                else:
                    self.store[self.name].append(dict(row))
            r.data = rows
        else:
            r.data = self.rows
        return r


def _client(**tables):
    store = {k: v for k, v in tables.items()}
    return type("C", (), {"table": staticmethod(lambda n: _Q(store, n))})(), store


# ── read_thread ─────────────────────────────────────────────────────────────

def test_undated_mail_sorts_first_not_last(monkeypatch):
    """The ordering bug that would be invisible. The prompt says the LAST email
    below is the most recent; an undated one at the end gets read as the latest
    development and the story says the wrong thing happened last."""
    c, _ = _client(emails=[
        {"order_id": "o1", "received_at": "2026-05-01T00:00:00", "subject": "may"},
        {"order_id": "o1", "received_at": None, "subject": "undated"},
        {"order_id": "o1", "received_at": "2026-01-01T00:00:00", "subject": "jan"},
    ])
    monkeypatch.setattr(ets, "get_client", lambda: c)
    got = [e["subject"] for e in ets.read_thread("o1", "chevron")]
    assert got == ["undated", "jan", "may"]


def test_an_nlng_thread_is_read_from_its_own_column(monkeypatch):
    c, _ = _client(emails=[
        {"nlng_order_id": "n1", "received_at": "2026-01-01", "subject": "nlng mail"},
        {"order_id": "n1", "received_at": "2026-01-02", "subject": "chevron mail"},
    ])
    monkeypatch.setattr(ets, "get_client", lambda: c)
    assert [e["subject"] for e in ets.read_thread("n1", "nlng")] == ["nlng mail"]
    assert [e["subject"] for e in ets.read_thread("n1", "chevron")] == ["chevron mail"]


def test_a_read_failure_is_an_empty_thread_not_a_crash(monkeypatch):
    """An empty thread sends the app to its Gmail fallback, which is right.
    Raising here would take the story down."""
    def _boom():
        raise Exception("statement timeout")
    monkeypatch.setattr(ets, "get_client", _boom)
    assert ets.read_thread("o1", "chevron") == []


# ── fetch_thread ────────────────────────────────────────────────────────────
# Driven through a fake IMAP client with real RFC822 bytes, because the parse
# and the sort are where a thread gets quietly mis-ordered, and every other
# test in this file stubs fetch_thread out. A first mutation run proved that:
# reversing the undated-mail sort here was the one change the suite missed.

def _rfc822(mid, frm, to, subject, body, date=None):
    lines = [f"Message-ID: {mid}", f"From: {frm}", f"To: {to}",
             f"Subject: {subject}"]
    if date:
        lines.append(f"Date: {date}")
    return ("\r\n".join(lines) + "\r\n\r\n" + body).encode()


class _FakeIMAP:
    """Enough of IMAPClient for fetch_thread: a search and an RFC822 fetch."""

    def __init__(self, messages, thrid=42):
        # messages: {uid: rfc822 bytes}
        self.messages = messages
        self.thrid = thrid
        self.logged_out = False

    def search(self, criteria):
        return list(self.messages.keys())

    def fetch(self, uids, parts):
        if "X-GM-THRID" in parts:
            return {u: {b"X-GM-THRID": self.thrid} for u in uids}
        return {u: {b"RFC822": self.messages[u]} for u in uids if u in self.messages}

    def logout(self):
        self.logged_out = True


def test_undated_mail_sorts_first_in_a_fetched_thread():
    """Same rule as read_thread, and it has to hold on the Gmail fallback path
    too — that thread goes straight into the story without passing through the
    table."""
    imap = _FakeIMAP({
        1: _rfc822("<may@x>", "f@x.com", "spm@x.com", "May", "later",
                   "Fri, 01 May 2026 09:00:00 +0000"),
        2: _rfc822("<none@x>", "f@x.com", "spm@x.com", "Undated", "no date"),
        3: _rfc822("<jan@x>", "f@x.com", "spm@x.com", "Jan", "earlier",
                   "Thu, 01 Jan 2026 09:00:00 +0000"),
    })
    got = ets.fetch_thread("PO1", imap=imap)
    assert [e["subject"] for e in got] == ["Undated", "Jan", "May"]


def test_our_own_mail_is_marked_as_outgoing(monkeypatch):
    monkeypatch.setenv("SPM_SENDER", "specialpiping@gmail.com")
    imap = _FakeIMAP({
        1: _rfc822("<a@x>", "Special Piping <specialpiping@gmail.com>",
                   "f@x.com", "Ours", "sent", "Thu, 01 Jan 2026 09:00:00 +0000"),
        2: _rfc822("<b@x>", "flexitallic <f@x.com>", "specialpiping@gmail.com",
                   "Theirs", "received", "Fri, 02 Jan 2026 09:00:00 +0000"),
    })
    got = {e["subject"]: e["direction"] for e in ets.fetch_thread("PO1", imap=imap)}
    assert got == {"Ours": "out", "Theirs": "in"}


def test_the_multi_order_digest_is_left_out():
    """A mail listing every open PO is not correspondence about this one, and
    it would drown the thread. Same exclusion the live fetch always had."""
    imap = _FakeIMAP({
        1: _rfc822("<d@x>", "f@x.com", "spm@x.com",
                   "SPM CNL Purchase Orders 12/10", "digest",
                   "Thu, 01 Jan 2026 09:00:00 +0000"),
        2: _rfc822("<k@x>", "f@x.com", "spm@x.com", "Re: PO 123", "real",
                   "Fri, 02 Jan 2026 09:00:00 +0000"),
    })
    assert [e["subject"] for e in ets.fetch_thread("PO1", imap=imap)] == ["Re: PO 123"]


def test_a_message_with_no_id_never_reaches_the_table():
    """Dropped at the fetch, not just at the store, so it cannot be written
    again on every pass."""
    imap = _FakeIMAP({
        1: _rfc822("", "f@x.com", "spm@x.com", "No id", "x",
                   "Thu, 01 Jan 2026 09:00:00 +0000"),
        2: _rfc822("<ok@x>", "f@x.com", "spm@x.com", "Has id", "x",
                   "Fri, 02 Jan 2026 09:00:00 +0000"),
    })
    assert [e["subject"] for e in ets.fetch_thread("PO1", imap=imap)] == ["Has id"]


def test_the_same_message_twice_is_stored_once():
    """A seed search and a thread search overlap by design, so the same UID can
    arrive from both."""
    body = _rfc822("<same@x>", "f@x.com", "spm@x.com", "Dup", "x",
                   "Thu, 01 Jan 2026 09:00:00 +0000")
    imap = _FakeIMAP({1: body, 2: body})
    assert len(ets.fetch_thread("PO1", imap=imap)) == 1


class _SeedThenThreadIMAP(_FakeIMAP):
    """A seed search that finds less than the thread search does.

    The plain fake returns every message whatever is asked, so dropping the
    thread-widening step made no difference to it - which a mutation run caught:
    removing the widening from fetch_thread passed unnoticed.

    Here the X-GM-RAW search finds only the messages naming the PO, and the
    X-GM-THRID search finds the whole conversation. That is the real shape, and
    the gap between the two is the mail the listeners miss.
    """

    def __init__(self, seed_uids, thread_uids, messages, thrid=42):
        super().__init__(messages, thrid=thrid)
        self.seed_uids, self.thread_uids = seed_uids, thread_uids

    def search(self, criteria):
        flat = " ".join(str(c) for c in criteria)
        if "X-GM-THRID" in flat:
            return list(self.thread_uids)
        return list(self.seed_uids)


def test_a_reply_that_names_no_po_number_is_still_collected():
    """The whole reason the archiver exists. A "Noted, thanks" reply quotes no
    PO number, so the seed search cannot find it - only widening to the thread
    does, and that is precisely the mail no listener recognises either."""
    msgs = {
        1: _rfc822("<names-po@x>", "f@x.com", "s@x.com", "Re: PO 0061365256",
                   "quotes the number", "Thu, 01 Jan 2026 09:00:00 +0000"),
        2: _rfc822("<quotes-nothing@x>", "f@x.com", "s@x.com", "Noted",
                   "Noted, thanks.", "Fri, 02 Jan 2026 09:00:00 +0000"),
        3: _rfc822("<also-nothing@x>", "f@x.com", "s@x.com", "RE:",
                   "Will chase.", "Sat, 03 Jan 2026 09:00:00 +0000"),
    }
    imap = _SeedThenThreadIMAP(seed_uids=[1], thread_uids=[1, 2, 3], messages=msgs)
    got = ets.fetch_thread("0061365256", imap=imap)
    subjects = [e["subject"] for e in got]
    assert len(got) == 3, subjects
    assert "Noted" in subjects
    assert "RE:" in subjects


def test_the_newest_date_comes_from_the_widened_thread_too():
    """Same gap on the cheap check: if the newest message in a thread happens
    to be the one quoting nothing, a seed-only search reports an older date and
    a stale story keeps being served."""
    from datetime import datetime as _dt

    class _Dated(_SeedThenThreadIMAP):
        def __init__(self, dates):
            super().__init__(seed_uids=[1], thread_uids=list(dates),
                             messages={u: b"" for u in dates})
            self.dates = dates

        def fetch(self, uids, parts):
            if "X-GM-THRID" in parts:
                return {u: {b"X-GM-THRID": self.thrid} for u in uids}
            if "INTERNALDATE" in parts:
                return {u: {b"INTERNALDATE": self.dates[u]}
                        for u in uids if u in self.dates}
            return {}

    got = ets.latest_gmail_date("PO1", imap=_Dated({
        1: _dt(2026, 1, 1, tzinfo=timezone.utc),
        2: _dt(2026, 9, 21, tzinfo=timezone.utc),   # newest, quotes nothing
    }))
    assert got.startswith("2026-09-21")


def test_a_passed_in_connection_is_left_open():
    """A pass reuses one login across several orders, so fetch_thread must not
    log out a connection it was handed."""
    imap = _FakeIMAP({1: _rfc822("<a@x>", "f@x.com", "s@x.com", "S", "b",
                                 "Thu, 01 Jan 2026 09:00:00 +0000")})
    ets.fetch_thread("PO1", imap=imap)
    assert imap.logged_out is False


def test_a_thread_matching_nothing_is_an_empty_list():
    imap = _FakeIMAP({})
    assert ets.fetch_thread("PO1", imap=imap) == []


# ── "no mail" is not the same as "could not look" ───────────────────────────
# The bug a live run exposed. A Chevron PO came back with 0 emails after 416
# seconds and was stamped as walked with email_count=0 - so it would not be
# tried again for six hours, and the three messages the listeners had already
# found were all it would ever have. Run fresh, that same search returned 19
# uids in 3.4 seconds: it had not found nothing, it had FAILED, and the failure
# was swallowed into an empty list. Logins to this mailbox were measured at
# 27.9 seconds against what was then a 30 second timeout.

class _FailingSearch(_FakeIMAP):
    def search(self, criteria):
        raise Exception("socket timeout")


class _FailingFetch(_FakeIMAP):
    def fetch(self, uids, parts):
        if "RFC822" in parts:
            raise Exception("connection reset by peer")
        return super().fetch(uids, parts)


def test_a_failed_search_raises_rather_than_looking_empty():
    with pytest.raises(ets.ThreadSyncFailed):
        ets.fetch_thread("PO1", imap=_FailingSearch({1: b"x"}))


def test_a_failed_message_fetch_raises_too():
    body = _rfc822("<a@x>", "f@x.com", "s@x.com", "S", "b",
                   "Thu, 01 Jan 2026 09:00:00 +0000")
    with pytest.raises(ets.ThreadSyncFailed):
        ets.fetch_thread("PO1", imap=_FailingFetch({1: body}))


def test_gmail_not_configured_is_a_failure_not_an_empty_thread(monkeypatch):
    monkeypatch.setattr(ets, "open_imap", lambda: None)
    with pytest.raises(ets.ThreadSyncFailed):
        ets.fetch_thread("PO1")


def test_a_failed_sync_stamps_nothing(monkeypatch):
    """So the order keeps its place at the front of the queue. Stamping it
    would hide a thread that was never read behind a thread with no mail."""
    c, store = _client(email_thread_sync=[])
    monkeypatch.setattr(ets, "get_client", lambda: c)

    def _boom(po, so="", imap=None):
        raise ets.ThreadSyncFailed("search failed")

    monkeypatch.setattr(ets, "fetch_thread", _boom)
    with pytest.raises(ets.ThreadSyncFailed):
        ets.sync_thread("o1", "chevron", "PO1")
    assert store["email_thread_sync"] == []


def test_a_genuinely_empty_thread_is_still_stamped(monkeypatch):
    """The other half of the distinction. The search worked and there is
    nothing to find, so retrying every pass would crowd out orders that do
    have mail."""
    c, store = _client(email_thread_sync=[])
    monkeypatch.setattr(ets, "get_client", lambda: c)
    monkeypatch.setattr(ets, "fetch_thread", lambda po, so="", imap=None: [])
    assert ets.sync_thread("o1", "chevron", "PO1") == 0
    assert store["email_thread_sync"][0]["email_count"] == 0


def test_a_pass_counts_what_it_could_not_read(monkeypatch, capsys):
    """And carries on. One unreadable thread costs that order, not the pass."""
    c, _ = _orders_client([])
    monkeypatch.setattr(ets, "get_client", lambda: c)
    monkeypatch.setattr(ets, "open_imap", lambda: _FakeIMAP({}))

    def _sync(oid, otype, po, so="", imap=None):
        if oid == "o2":
            raise ets.ThreadSyncFailed("socket timeout")
        return 1

    monkeypatch.setattr(ets, "sync_thread", _sync)
    assert ets.run_pass(10) == 2
    out = capsys.readouterr().out
    assert "NOT SYNCED" in out
    assert "keep their place in the queue" in out


def test_the_imap_timeout_clears_the_measured_login_time():
    """Logins were measured at 27.9s. A 30s timeout left no headroom, and the
    timeout surfaced as "this PO has no mail"."""
    assert ets.IMAP_TIMEOUT >= 60


def test_a_missing_date_is_left_empty_never_guessed():
    """The bug this file found. sync.parse_email_date falls back to now(), and
    using it here stamped a dateless email with the moment it was fetched.

    Two consequences, both silent. The story is told in date order and the
    prompt says the last email is the most recent, so that message is read as
    the latest development. And because the cache is invalidated when mail is
    newer than the story, while upsert_email keys on message_id, every sync
    rewrote the date to a fresh now() - so any order holding one dateless
    email regenerated its story on every pass, paying for a Gmail search and a
    model call each time."""
    imap = _FakeIMAP({
        1: _rfc822("<none@x>", "f@x.com", "s@x.com", "No date", "x"),
        2: _rfc822("<bad@x>", "f@x.com", "s@x.com", "Bad date", "x", "not a date"),
    })
    for e in ets.fetch_thread("PO1", imap=imap):
        assert e["received_at"] is None, e["subject"]


def test_a_real_date_is_kept_exactly():
    imap = _FakeIMAP({1: _rfc822("<a@x>", "f@x.com", "s@x.com", "S", "b",
                                 "Thu, 01 Jan 2026 09:00:00 +0000")})
    assert ets.fetch_thread("PO1", imap=imap)[0]["received_at"].startswith("2026-01-01T09:00")


def test_sent_at_never_invents_a_date():
    import email as _e
    assert ets.sent_at(_e.message_from_bytes(b"Subject: x\r\n\r\nb")) is None
    assert ets.sent_at(_e.message_from_bytes(b"Date: rubbish\r\n\r\nb")) is None
    assert ets.sent_at(_e.message_from_bytes(
        b"Date: Thu, 01 Jan 2026 09:00:00 +0000\r\n\r\nb")).startswith("2026-01-01")


def test_the_pipeline_fallback_is_left_alone():
    """sync.parse_email_date keeps its now() fallback - seven pipeline call
    sites rely on it, where the time the mail was handled is a fair answer.
    The archiver has its own rule rather than changing theirs."""
    import sync as _s
    import email as _e
    assert _s.parse_email_date(_e.message_from_bytes(b"Subject: x\r\n\r\nb")) != ""
    # Look for a call, not a mention: the reason it is not used is written out
    # in sent_at's own docstring.
    src = (ROOT / "scripts" / "email_thread_sync.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.split("\n")
                     if "sync.parse_email_date(" in l and not l.lstrip().startswith("#"))
    assert code == "", code


def test_the_body_is_capped():
    imap = _FakeIMAP({1: _rfc822("<a@x>", "f@x.com", "s@x.com", "Big", "x" * 80_000,
                                 "Thu, 01 Jan 2026 09:00:00 +0000")})
    assert len(ets.fetch_thread("PO1", imap=imap)[0]["body_text"]) == 50_000


# ── latest_gmail_date: the cheap "has anything arrived?" question ───────────
# Tested through a fake IMAP for the same reason fetch_thread is: every other
# test stubs it out, and a first mutation run proved the gap - pulling full
# bodies instead of dates, and a failed query claiming "nothing new", both
# passed unnoticed.

class _DatedIMAP(_FakeIMAP):
    """Serves INTERNALDATE, and records which parts were asked for."""

    def __init__(self, dates, thrid=42):
        super().__init__({u: b"" for u in dates}, thrid=thrid)
        self.dates = dates
        self.asked = []

    def fetch(self, uids, parts):
        self.asked.append(tuple(parts))
        if "X-GM-THRID" in parts:
            return {u: {b"X-GM-THRID": self.thrid} for u in uids}
        if "INTERNALDATE" in parts:
            return {u: {b"INTERNALDATE": self.dates[u]}
                    for u in uids if u in self.dates}
        return {}


def test_the_newest_date_in_the_thread_is_returned():
    from datetime import datetime as _dt
    imap = _DatedIMAP({
        1: _dt(2026, 9, 1, 9, 0, tzinfo=timezone.utc),
        2: _dt(2026, 9, 21, 15, 39, 55, tzinfo=timezone.utc),
        3: _dt(2026, 6, 30, 3, 28, tzinfo=timezone.utc),
    })
    got = ets.latest_gmail_date("4200093075", imap=imap)
    assert got.startswith("2026-09-21T15:39:55")


def test_it_asks_for_dates_not_bodies():
    """This is what makes it cheap: 4 seconds against the 80 the same thread
    takes to fetch in full. Asking for RFC822 here would quietly reintroduce
    the whole cost on every story open."""
    from datetime import datetime as _dt
    imap = _DatedIMAP({1: _dt(2026, 9, 1, tzinfo=timezone.utc)})
    ets.latest_gmail_date("PO1", imap=imap)
    asked = [p for parts in imap.asked for p in parts]
    assert "INTERNALDATE" in asked
    assert "RFC822" not in asked


def test_it_widens_to_the_whole_thread():
    """A reply that quotes no PO number is exactly the mail the listeners miss,
    so it must be counted here or the check has the same blind spot."""
    from datetime import datetime as _dt
    imap = _DatedIMAP({1: _dt(2026, 9, 1, tzinfo=timezone.utc)})
    ets.latest_gmail_date("PO1", imap=imap)
    assert any("X-GM-THRID" in parts for parts in imap.asked)


def test_a_failed_search_says_it_does_not_know():
    """None, never a date. A caller reads a date as "nothing newer than this"
    and would serve a stale story for ever."""
    assert ets.latest_gmail_date("PO1", imap=_FailingSearch({1: b"x"})) is None


def test_a_failed_date_fetch_also_says_it_does_not_know():
    class _NoDates(_DatedIMAP):
        def fetch(self, uids, parts):
            self.asked.append(tuple(parts))
            if "X-GM-THRID" in parts:
                return {u: {b"X-GM-THRID": self.thrid} for u in uids}
            raise Exception("connection reset")

    from datetime import datetime as _dt
    assert ets.latest_gmail_date(
        "PO1", imap=_NoDates({1: _dt(2026, 9, 1, tzinfo=timezone.utc)})) is None


def test_a_thread_matching_nothing_says_it_does_not_know():
    assert ets.latest_gmail_date("PO1", imap=_DatedIMAP({})) is None


def test_gmail_not_configured_says_it_does_not_know(monkeypatch):
    monkeypatch.setattr(ets, "open_imap", lambda: None)
    assert ets.latest_gmail_date("PO1") is None


def test_a_naive_date_is_treated_as_utc():
    """IMAP servers vary. A naive datetime compared against a timezone-aware
    story date raises, which would have surfaced as the whole check failing."""
    from datetime import datetime as _dt
    got = ets.latest_gmail_date("PO1", imap=_DatedIMAP({1: _dt(2026, 9, 1, 9, 0)}))
    assert got is not None
    assert got.endswith("+00:00")


def test_a_handed_in_connection_is_not_logged_out():
    from datetime import datetime as _dt
    imap = _DatedIMAP({1: _dt(2026, 9, 1, tzinfo=timezone.utc)})
    ets.latest_gmail_date("PO1", imap=imap)
    assert imap.logged_out is False


# ── store_thread ────────────────────────────────────────────────────────────

_MAIL = [
    {"message_id": "<a@x>", "direction": "in", "from_address": "f@x.com",
     "to_address": "spm@x.com", "subject": "Re: PO", "body_text": "Packed.",
     "received_at": "2026-10-01T09:00:00"},
    {"message_id": "<b@x>", "direction": "out", "from_address": "spm@x.com",
     "to_address": "f@x.com", "subject": "Re: PO", "body_text": "Thanks.",
     "received_at": "2026-10-02T09:00:00"},
]


@pytest.fixture
def captured(monkeypatch):
    seen = []
    monkeypatch.setattr(ets.sync, "upsert_email", lambda **kw: seen.append(kw))
    return seen


def test_every_message_is_stored(captured):
    assert ets.store_thread("o1", "chevron", _MAIL) == 2
    assert [c["message_id"] for c in captured] == ["<a@x>", "<b@x>"]
    assert captured[1]["direction"] == "out"


def test_chevron_mail_goes_on_order_id(captured):
    ets.store_thread("o1", "chevron", _MAIL)
    assert captured[0]["order_id"] == "o1"
    assert "nlng_order_id" not in captured[0]


def test_nlng_mail_goes_on_nlng_order_id(captured):
    """Stored on the wrong column it attaches to nothing, and read_thread looks
    at the matching one — the thread would simply never be found again."""
    ets.store_thread("n1", "nlng", _MAIL)
    assert captured[0]["nlng_order_id"] == "n1"
    assert "order_id" not in captured[0]


def test_a_message_with_no_id_is_skipped(captured):
    """upsert_email keys on message_id for its on-conflict, so a row without
    one would be written again on every pass, for ever."""
    assert ets.store_thread("o1", "chevron", [
        {"message_id": "", "subject": "none"},
        {"message_id": "  ", "subject": "whitespace"},
        _MAIL[0],
    ]) == 1
    assert [c["message_id"] for c in captured] == ["<a@x>"]


def test_one_bad_row_does_not_lose_the_rest(monkeypatch):
    calls = []

    def _flaky(**kw):
        calls.append(kw["message_id"])
        if kw["message_id"] == "<a@x>":
            raise Exception("row too large")

    monkeypatch.setattr(ets.sync, "upsert_email", _flaky)
    assert ets.store_thread("o1", "chevron", _MAIL) == 1
    assert calls == ["<a@x>", "<b@x>"]


# ── sync_thread ─────────────────────────────────────────────────────────────

def test_a_sync_fetches_stores_and_stamps(monkeypatch):
    c, store = _client(email_thread_sync=[])
    monkeypatch.setattr(ets, "get_client", lambda: c)
    monkeypatch.setattr(ets, "fetch_thread", lambda po, so="", imap=None: _MAIL)
    monkeypatch.setattr(ets.sync, "upsert_email", lambda **kw: None)
    assert ets.sync_thread("o1", "chevron", "PO1") == 2
    assert store["email_thread_sync"][0]["email_count"] == 2
    assert store["email_thread_sync"][0]["order_id"] == "o1"


def test_an_order_with_no_po_number_is_not_touched(monkeypatch):
    called = []
    monkeypatch.setattr(ets, "fetch_thread", lambda *a, **k: called.append(1) or [])
    assert ets.sync_thread("o1", "chevron", "") == 0
    assert called == []


def test_an_empty_thread_is_still_stamped(monkeypatch):
    """Otherwise an order whose PO matches nothing in Gmail is retried on every
    single pass, for ever, and crowds out orders that do have mail."""
    c, store = _client(email_thread_sync=[])
    monkeypatch.setattr(ets, "get_client", lambda: c)
    monkeypatch.setattr(ets, "fetch_thread", lambda po, so="", imap=None: [])
    assert ets.sync_thread("o1", "chevron", "PO1") == 0
    assert store["email_thread_sync"][0]["email_count"] == 0


# ── due_orders ──────────────────────────────────────────────────────────────

def _orders_client(stamps, statuses=None):
    statuses = statuses or {}
    orders = [{"id": "o%d" % i, "buyer_po_number": "PO%d" % i, "so_number": "",
               "overall_status": statuses.get("o%d" % i, "stock_check_complete")}
              for i in range(1, 4)]
    return _client(orders=orders, nlng_orders=[], email_thread_sync=stamps)


def test_a_never_synced_order_comes_first(monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    c, _ = _orders_client([{"order_id": "o1", "order_type": "chevron", "synced_at": now}])
    monkeypatch.setattr(ets, "get_client", lambda: c)
    due = ets.due_orders(10)
    ids = [d["order_id"] for d in due]
    assert "o2" in ids and "o3" in ids       # never synced
    assert "o1" not in ids                   # synced just now


def test_the_most_overdue_order_is_taken_first(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    older = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    recent = datetime.now(timezone.utc).isoformat()
    c, _ = _orders_client([
        {"order_id": "o1", "order_type": "chevron", "synced_at": old},
        {"order_id": "o2", "order_type": "chevron", "synced_at": older},
        {"order_id": "o3", "order_type": "chevron", "synced_at": recent},
    ])
    monkeypatch.setattr(ets, "get_client", lambda: c)
    assert [d["order_id"] for d in ets.due_orders(10)] == ["o2", "o1"]


def test_a_delivered_order_is_not_walked_again(monkeypatch):
    """Its thread cannot grow. Re-walking every finished order every six hours
    would eventually be most of the Gmail traffic, for nothing."""
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    c, _ = _orders_client(
        [{"order_id": "o1", "order_type": "chevron", "synced_at": old},
         {"order_id": "o2", "order_type": "chevron", "synced_at": old}],
        statuses={"o1": "delivered", "o2": "cancelled"})
    monkeypatch.setattr(ets, "get_client", lambda: c)
    assert [d["order_id"] for d in ets.due_orders(10)] == ["o3"]


def test_a_delivered_order_still_gets_its_first_walk(monkeypatch):
    """Somebody will look back at a delivered order and want its story."""
    c, _ = _orders_client([], statuses={"o1": "delivered"})
    monkeypatch.setattr(ets, "get_client", lambda: c)
    assert "o1" in [d["order_id"] for d in ets.due_orders(10)]


def test_the_batch_size_is_respected(monkeypatch):
    c, _ = _orders_client([])
    monkeypatch.setattr(ets, "get_client", lambda: c)
    assert len(ets.due_orders(2)) == 2


def test_a_missing_sync_table_names_its_migration(monkeypatch, capsys):
    """Everything is treated as due, which is correct, and the message says
    which file to run rather than leaving somebody to guess."""
    orders = [{"id": "o1", "buyer_po_number": "PO1", "so_number": "",
               "overall_status": "ack"}]
    c, _ = _client(orders=orders, nlng_orders=[])   # no email_thread_sync
    monkeypatch.setattr(ets, "get_client", lambda: c)
    due = ets.due_orders(10)
    assert [d["order_id"] for d in due] == ["o1"]
    assert "create_email_thread_sync.sql" in capsys.readouterr().out


def test_an_order_with_no_po_number_is_never_queued(monkeypatch):
    c, _ = _client(orders=[{"id": "o1", "buyer_po_number": None,
                            "so_number": "", "overall_status": "ack"}],
                   nlng_orders=[], email_thread_sync=[])
    monkeypatch.setattr(ets, "get_client", lambda: c)
    assert ets.due_orders(10) == []


# ── the pass ────────────────────────────────────────────────────────────────

def test_one_failing_thread_does_not_stop_the_pass(monkeypatch):
    """Each pass holds one IMAP connection over several orders. A single bad
    thread has to cost that order, not the other seven."""
    c, _ = _orders_client([])
    monkeypatch.setattr(ets, "get_client", lambda: c)
    monkeypatch.setattr(ets, "open_imap", lambda: object())
    done = []

    def _sync(oid, otype, po, so="", imap=None):
        if oid == "o2":
            raise Exception("thread fetch failed")
        done.append(oid)
        return 1

    monkeypatch.setattr(ets, "sync_thread", _sync)
    assert ets.run_pass(10) == 2
    assert done == ["o1", "o3"]


def test_a_pass_without_gmail_configured_does_nothing(monkeypatch):
    c, _ = _orders_client([])
    monkeypatch.setattr(ets, "get_client", lambda: c)
    monkeypatch.setattr(ets, "open_imap", lambda: None)
    called = []
    monkeypatch.setattr(ets, "sync_thread", lambda *a, **k: called.append(1) or 0)
    assert ets.run_pass(10) == 0
    assert called == []


def test_a_dry_run_writes_nothing(monkeypatch):
    c, _ = _orders_client([])
    monkeypatch.setattr(ets, "get_client", lambda: c)
    called = []
    monkeypatch.setattr(ets, "open_imap", lambda: called.append("login") or object())
    monkeypatch.setattr(ets, "sync_thread", lambda *a, **k: called.append("sync") or 1)
    assert ets.run_pass(10, dry_run=True) == 0
    assert called == []


# ── the sync table treats both order types as first class ───────────────────

def test_the_stamp_always_names_the_order_type(monkeypatch):
    """It must never be left to a column default. The two order tables have
    independent id spaces, so a row defaulting to 'chevron' would stamp an NLNG
    order as a Chevron one; the archiver then looks it up as nlng, finds
    nothing, and re-walks that thread on every pass for ever."""
    c, store = _client(email_thread_sync=[])
    monkeypatch.setattr(ets, "get_client", lambda: c)
    monkeypatch.setattr(ets, "fetch_thread", lambda po, so="", imap=None: _MAIL)
    monkeypatch.setattr(ets.sync, "upsert_email", lambda **kw: None)

    ets.sync_thread("n1", "nlng", "PO1")
    ets.sync_thread("c1", "chevron", "PO2")
    types = {r["order_id"]: r.get("order_type") for r in store["email_thread_sync"]}
    assert types == {"n1": "nlng", "c1": "chevron"}


def test_the_two_order_types_do_not_collide_on_the_same_id(monkeypatch):
    """orders and nlng_orders are separate id spaces. The primary key is the
    pair, so the same id under both types is two rows, not one overwriting the
    other."""
    c, store = _client(email_thread_sync=[])
    monkeypatch.setattr(ets, "get_client", lambda: c)
    monkeypatch.setattr(ets, "fetch_thread", lambda po, so="", imap=None: _MAIL)
    monkeypatch.setattr(ets.sync, "upsert_email", lambda **kw: None)

    ets.sync_thread("same-id", "chevron", "PO1")
    ets.sync_thread("same-id", "nlng", "PO2")
    assert len(store["email_thread_sync"]) == 2


def test_the_migration_constrains_the_order_type():
    """Rather than defaulting it. A missing or misspelled value should fail at
    the insert, not quietly become 'chevron'."""
    raw = (ROOT / "migrations" / "create_email_thread_sync.sql").read_text(encoding="utf-8")
    # Comments only, stripped: the reason the default was removed is written
    # out in the file, so a plain search finds its own explanation.
    sql = "\n".join(l for l in raw.split("\n") if not l.lstrip().startswith("--"))
    assert "check (order_type in ('chevron', 'nlng'))" in sql
    assert "default 'chevron'" not in sql, sql
    assert "primary key (order_id, order_type)" in sql


def test_nlng_orders_are_queued_alongside_chevron(monkeypatch):
    """due_orders walks both tables. Reading only `orders` would leave every
    NLNG thread unsynced for ever, and nothing would say so."""
    c, _ = _client(
        orders=[{"id": "c1", "buyer_po_number": "PO1", "so_number": "",
                 "overall_status": "ack"}],
        nlng_orders=[{"id": "n1", "po_number": "4200093075", "so_number": "SO1",
                      "overall_status": "ready_for_dispatch"}],
        email_thread_sync=[])
    monkeypatch.setattr(ets, "get_client", lambda: c)
    due = {d["order_id"]: d["order_type"] for d in ets.due_orders(10)}
    assert due == {"c1": "chevron", "n1": "nlng"}


def test_the_nlng_po_column_is_read_not_the_chevron_one(monkeypatch):
    """nlng_orders has po_number; orders has buyer_po_number. Reading the wrong
    one yields no PO, and the order is skipped silently."""
    c, _ = _client(
        orders=[],
        nlng_orders=[{"id": "n1", "po_number": "4200093075", "so_number": "SO1",
                      "overall_status": "ack"}],
        email_thread_sync=[])
    monkeypatch.setattr(ets, "get_client", lambda: c)
    due = ets.due_orders(10)
    assert len(due) == 1
    assert due[0]["po_number"] == "4200093075"
    assert due[0]["so_number"] == "SO1"


# ── the worker registration ─────────────────────────────────────────────────

def test_the_worker_runs_it_as_a_thread():
    src = (ROOT / "scripts" / "worker.py").read_text(encoding="utf-8")
    assert "from email_thread_sync" in src
    assert '"email_thread_sync"' in src


def test_it_can_be_run_from_the_command_line_for_a_backfill():
    """The worker covers eight orders a pass; a first backfill of 199 should
    not take a day. Same code either way - the module is the library and both
    entry points call it."""
    assert hasattr(ets, "main")
    src = (ROOT / "scripts" / "email_thread_sync.py").read_text(encoding="utf-8")
    assert "--limit" in src and "--order" in src and "--dry-run" in src


# ── the app's fallback ──────────────────────────────────────────────────────

def _app():
    os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
    os.environ.setdefault("SUPABASE_KEY", "fake")
    os.environ.setdefault("JWT_SECRET", "test-secret")
    import app
    return app


def test_the_app_reads_the_table_before_touching_gmail(monkeypatch):
    app = _app()
    gmail = []
    monkeypatch.setattr(app, "_fetch_gmail_emails",
                        lambda po, so="": gmail.append(po) or [])
    monkeypatch.setattr(ets, "read_thread", lambda oid, ot: _MAIL)
    got = app._thread_for("o1", "chevron", "PO1")
    assert len(got) == 2
    assert gmail == []          # the table answered it


def test_an_unsynced_order_still_gets_its_thread_from_gmail(monkeypatch):
    """The reason this is safe to deploy before the backfill has run. Without
    the fallback, every order the archiver had not reached would have no story
    at all."""
    app = _app()
    monkeypatch.setattr(ets, "read_thread", lambda oid, ot: [])
    monkeypatch.setattr(ets, "store_thread", lambda *a, **k: len(a[2]))
    monkeypatch.setattr(app, "_fetch_gmail_emails", lambda po, so="": _MAIL)
    assert len(app._thread_for("o1", "chevron", "PO1")) == 2


def test_a_gmail_fallback_stores_what_it_fetched(monkeypatch):
    """It has just paid for the fetch, so the next telling should be a read."""
    app = _app()
    stored = []
    monkeypatch.setattr(ets, "read_thread", lambda oid, ot: [])
    monkeypatch.setattr(ets, "store_thread",
                        lambda oid, ot, mail: stored.append((oid, ot, len(mail))))
    monkeypatch.setattr(app, "_fetch_gmail_emails", lambda po, so="": _MAIL)
    app._thread_for("o1", "chevron", "PO1")
    for _ in range(50):          # the store runs on a daemon thread
        if stored:
            break
        import time as _t
        _t.sleep(0.02)
    assert stored == [("o1", "chevron", 2)]


# ── writing a story looks at Gmail; reading a stored thread does not ────────
# The hole that was spotted on review. A story is only ever written when none
# exists, when mail has arrived since the last one, or when somebody presses
# Regenerate - all three mean "go and look". Preferring the table
# unconditionally meant a story rebuilt BECAUSE new mail arrived was built from
# the thread as it stood before that mail, and would say nothing had happened
# while the email sat in Gmail.

def test_a_refresh_goes_to_gmail_even_when_the_table_has_a_thread(monkeypatch):
    app = _app()
    gmail = []
    monkeypatch.setattr(app, "_fetch_gmail_emails",
                        lambda po, so="": gmail.append(po) or _MAIL)
    monkeypatch.setattr(ets, "read_thread", lambda oid, ot: [{"subject": "stale"}])
    monkeypatch.setattr(ets, "store_thread", lambda *a, **k: None)
    got = app._thread_for("o1", "chevron", "PO1", refresh=True)
    assert gmail == ["PO1"]
    assert [e["subject"] for e in got] == ["Re: PO", "Re: PO"]
    assert "stale" not in str(got)


def test_a_refresh_stores_what_it_fetched(monkeypatch):
    """So the follow-up chat, which reads the table, sees the new mail too."""
    app = _app()
    stored = []
    monkeypatch.setattr(app, "_fetch_gmail_emails", lambda po, so="": _MAIL)
    monkeypatch.setattr(ets, "read_thread", lambda oid, ot: [])
    monkeypatch.setattr(ets, "store_thread",
                        lambda oid, ot, mail: stored.append((oid, len(mail))))
    app._thread_for("o1", "chevron", "PO1", refresh=True)
    for _ in range(50):
        if stored:
            break
        import time as _t
        _t.sleep(0.02)
    assert stored == [("o1", 2)]


def test_a_refresh_that_cannot_reach_gmail_uses_what_is_stored(monkeypatch):
    """A thread that is slightly behind beats no story at all, and the date on
    screen still tells the reader how old the story is."""
    app = _app()
    monkeypatch.setattr(app, "_fetch_gmail_emails", lambda po, so="": [])
    monkeypatch.setattr(ets, "read_thread", lambda oid, ot: [{"subject": "stored"}])
    got = app._thread_for("o1", "chevron", "PO1", refresh=True)
    assert [e["subject"] for e in got] == ["stored"]


def test_without_refresh_the_table_still_answers(monkeypatch):
    """The chat asks follow-up questions straight after the story, when the
    table has just been brought up to date, so it should not pay for Gmail
    again on every message."""
    app = _app()
    gmail = []
    monkeypatch.setattr(app, "_fetch_gmail_emails",
                        lambda po, so="": gmail.append(po) or _MAIL)
    monkeypatch.setattr(ets, "read_thread", lambda oid, ot: [{"subject": "stored"}])
    got = app._thread_for("o1", "chevron", "PO1")
    assert gmail == []
    assert [e["subject"] for e in got] == ["stored"]


def test_the_story_refreshes_and_the_chat_does_not():
    """Which call site passes refresh is the whole behaviour, and it is easy to
    get backwards: summarize WRITES a story, chat only asks about one."""
    src = (ROOT / "web" / "app.py").read_text(encoding="utf-8")
    calls = [l for l in src.split("\n")
             if "_build_email_context(" in l
             and not l.lstrip().startswith("def _build_email_context")]
    assert len(calls) == 2
    summarize = [l for l in calls if "order_id, type," in l]
    chat = [l for l in calls if "order_id, order_type," in l]
    assert len(summarize) == 1 and "refresh=True" in summarize[0]
    assert len(chat) == 1 and "refresh=True" not in chat[0]


def test_a_broken_archiver_falls_back_rather_than_failing(monkeypatch):
    app = _app()

    def _boom(oid, ot):
        raise Exception("module exploded")

    monkeypatch.setattr(ets, "read_thread", _boom)
    monkeypatch.setattr(app, "_fetch_gmail_emails", lambda po, so="": _MAIL)
    assert len(app._thread_for("o1", "chevron", "PO1")) == 2


def test_the_story_and_the_chat_both_pass_the_order_through():
    """_build_email_context needs the order id to read the table at all. Both
    callers had to be updated, and the summarize endpoint names the parameter
    `type` while the chat names it `order_type` - passing the wrong one is a
    NameError at request time, not import time."""
    src = (ROOT / "web" / "app.py").read_text(encoding="utf-8")
    # Both call sites sit inside a one-line `def _f_emails(): ...` thread body,
    # so "def" cannot be used to tell a call from the definition.
    calls = [l for l in src.split("\n")
             if "_build_email_context(" in l
             and not l.lstrip().startswith("def _build_email_context")]
    assert len(calls) == 2, calls
    for line in calls:
        assert "order_id" in line
        assert "po_number" in line


# ── the archiver is off unless switched on ──────────────────────────────────

def test_the_archiver_is_off_by_default(monkeypatch):
    """It got the mailbox throttled. Within a day it pulled about 10,000
    messages as full RFC822 with attachments - tens of GB against Gmail's
    ~2.5 GB a day - and the order listeners read the same mailbox, so the
    pipeline slowed with it. Off unless THREAD_SYNC_ENABLED says otherwise."""
    import importlib
    monkeypatch.delenv("THREAD_SYNC_ENABLED", raising=False)
    mod = importlib.reload(ets)
    try:
        assert mod.ENABLED is False
    finally:
        importlib.reload(ets)


def test_switched_off_it_idles_rather_than_returning():
    """worker.py runs each listener in `while True: fn()`, so returning would
    be called straight back and spin."""
    import threading
    calls = []
    done = threading.Event()

    def fake_sleep(secs):
        calls.append(secs)
        if len(calls) >= 3:
            done.set()
            raise SystemExit      # end the idle loop once it has proved it idles

    orig_enabled, orig_sleep = ets.ENABLED, ets.time.sleep
    ets.ENABLED = False
    ets.time.sleep = fake_sleep
    try:
        with pytest.raises(SystemExit):
            ets.run_forever()
    finally:
        ets.ENABLED, ets.time.sleep = orig_enabled, orig_sleep
    # It slept, repeatedly, and never did a pass.
    assert done.is_set()
    assert all(s >= 60 for s in calls)

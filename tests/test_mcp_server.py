"""
test_mcp_server.py — the read-only lookups exposed to Claude Code.

The web app's PO story answers questions about one order, because the server
packs that order and hands it over. Anything spanning the business - "which
orders are stuck?", "what did we quote for gaskets?" - was unanswerable,
because the data was never in the package.

This server closes that gap on the Claude subscription rather than the API:
there is no model in it and no ANTHROPIC_API_KEY, so those questions cost
nothing per answer.

What these tests protect:

  * Read-only. SUPABASE_KEY is the service_role key and bypasses every
    row-level rule, so the absence of any write path is the security boundary,
    not a convention. A test asserts no tool can mutate.
  * Every tool caps its output. Without that, "find orders" on a loose term
    returns the table.
  * Where an order sits is read from the END of the pipeline backwards. Read
    forwards, a gap in the middle - which the auditor fills in later - reports
    an order as stuck at a stage it passed months ago. A live call found
    exactly such a gap on PO 4200093075.
  * Descriptions are flattened. Line items hold the quote editor's innerHTML;
    a live call returned 'Spiral Wound Gasket.<br>NPS -3/4"<' - truncated
    mid-tag, the markup eating the width the description needed.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "fake")

import pytest

mcp_server = pytest.importorskip(
    "mcp_server", reason="the MCP SDK is a local-only tool; see requirements-mcp.txt")
M = mcp_server


# ── fakes ───────────────────────────────────────────────────────────────────

class _Q:
    def __init__(self, store, name):
        self.store, self.name = store, name
        if name not in store:
            raise Exception("{'code': 'PGRST205', 'message': \"Could not find "
                            "the table 'public.%s'\"}" % name)
        self.rows = list(store[name])

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


def _client(**tables):
    store = dict(tables)
    return type("C", (), {"table": staticmethod(lambda n: _Q(store, n))})()


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _order(po, **kw):
    row = {"id": "id-" + po, "buyer_po_number": po, "buyer_name": "CHEVRON",
           "so_number": "", "spm_po_number": "", "overall_status": "po_sent",
           "po_amount": 100, "po_currency": "USD"}
    row.update(kw)
    return row


# ── where an order sits ─────────────────────────────────────────────────────

def test_the_most_recent_stamp_is_what_counts():
    """Not the furthest along a fixed sequence. A gap in the middle - which the
    gap auditor fills in later - must not misreport progress; PO 4200093075 has
    that shape, with the stock check blank but the SPM PO sent and the SO
    received."""
    row = {"sent_to_warehouse_at": _ago(90),
           "stock_check_completed_at": None,          # the gap
           "spm_po_sent_at": _ago(60),
           "so_received_at": _ago(55)}
    label, _, waited = M._where_it_sits(row, M._CHEVRON_STAGES)
    assert label == "supplier acknowledged (SO)"
    assert 54 <= waited <= 56


def test_a_later_stamp_out_of_sequence_still_wins():
    """The bug this replaced. Walking a fixed order backwards reported
    PO 0061378648 as 220 days at "SPM PO sent to supplier" when its delivery
    request had been stamped 74 days earlier, and PO 0061409193 as 171 days at
    "stock check completed" when a delivery request came in 17 days before.
    These stages do not happen in a strict order, so the latest stamp is the
    only honest answer to "has this moved lately?"."""
    row = {"spm_po_sent_at": _ago(220),
           "delivery_requested_at": _ago(74)}
    label, _, waited = M._where_it_sits(row, M._CHEVRON_STAGES)
    assert label == "client requested delivery"
    assert 73 <= waited <= 75


def test_a_delivery_request_is_in_the_stage_list_at_all():
    """It was missing from the first version, inherited from
    _build_order_context which also omits it, and 34 orders carry that
    status - so most of a reported "72 orders stuck" was that one gap."""
    cols = [c for c, _ in M._CHEVRON_STAGES]
    assert "delivery_requested_at" in cols


def test_row_bookkeeping_is_not_mistaken_for_activity():
    """created_at and updated_at say when the ROW was written, not when
    anything happened. PO 0061367679 has created_at 2026-07-06, months after
    its last real event, so counting it would hide a genuinely stale order."""
    cols = [c for c, _ in M._CHEVRON_STAGES]
    assert "created_at" not in cols
    assert "updated_at" not in cols
    row = {"spm_po_sent_at": _ago(235), "created_at": _ago(3),
           "updated_at": _ago(1)}
    _, _, waited = M._where_it_sits(row, M._CHEVRON_STAGES)
    assert 234 <= waited <= 236


def test_an_order_with_nothing_stamped_says_so():
    label, when, waited = M._where_it_sits({}, M._CHEVRON_STAGES)
    assert label == "nothing stamped yet"
    assert when == "—"
    assert waited is None


def test_nlng_has_no_gep_acknowledgement_step():
    """It arrives as a PDF, not through GEP. Including that stage would report
    every NLNG order as missing a step it never has."""
    cols = [c for c, _ in M._NLNG_STAGES]
    assert "acknowledged_at" not in cols
    assert "acknowledged_at" in [c for c, _ in M._CHEVRON_STAGES]
    assert len(M._NLNG_STAGES) == len(M._CHEVRON_STAGES) - 1


# ── orders_stuck ────────────────────────────────────────────────────────────

def test_an_order_still_for_longer_than_asked_is_reported(monkeypatch):
    monkeypatch.setattr(M, "get_client", lambda: _client(
        orders=[_order("0061367679", spm_po_sent_at=_ago(235))], nlng_orders=[]))
    out = M.orders_stuck(days=21)
    assert "0061367679" in out
    assert "SPM PO sent to supplier" in out


def test_a_recently_moved_order_is_not_reported(monkeypatch):
    monkeypatch.setattr(M, "get_client", lambda: _client(
        orders=[_order("0061367679", spm_po_sent_at=_ago(3))], nlng_orders=[]))
    assert "0061367679" not in M.orders_stuck(days=21)


def test_delivered_and_cancelled_orders_are_not_chased(monkeypatch):
    """They are finished. Listing them as stuck is noise that buries the
    orders somebody actually has to act on."""
    monkeypatch.setattr(M, "get_client", lambda: _client(
        orders=[_order("A", overall_status="delivered", delivered_at=_ago(200)),
                _order("B", overall_status="cancelled", cancelled_at=_ago(200)),
                _order("C", spm_po_sent_at=_ago(200))], nlng_orders=[]))
    out = M.orders_stuck(days=21)
    assert "PO C" in out
    assert "PO A" not in out and "PO B" not in out


def test_the_longest_wait_comes_first(monkeypatch):
    monkeypatch.setattr(M, "get_client", lambda: _client(
        orders=[_order("SHORT", spm_po_sent_at=_ago(30)),
                _order("LONG", spm_po_sent_at=_ago(300))], nlng_orders=[]))
    out = M.orders_stuck(days=21)
    assert out.index("LONG") < out.index("SHORT")


def test_the_client_filter_works_both_ways(monkeypatch):
    monkeypatch.setattr(M, "get_client", lambda: _client(
        orders=[_order("CHEV", spm_po_sent_at=_ago(100))],
        nlng_orders=[{"id": "n1", "po_number": "4200092326", "so_number": "",
                      "spm_po_number": "", "overall_status": "dispatched",
                      "dispatched_at": _ago(118)}]))
    assert "4200092326" not in M.orders_stuck(days=21, client="chevron")
    assert "CHEV" not in M.orders_stuck(days=21, client="nlng")
    both = M.orders_stuck(days=21)
    assert "CHEV" in both and "4200092326" in both


def test_nothing_stuck_is_said_in_words_not_an_empty_reply(monkeypatch):
    monkeypatch.setattr(M, "get_client", lambda: _client(orders=[], nlng_orders=[]))
    assert "No" in M.orders_stuck(days=21)


def test_the_reply_is_capped_and_says_it_was(monkeypatch):
    """72 orders were stuck on the live data. Returning all of them every time
    fills a context window for no gain."""
    monkeypatch.setattr(M, "get_client", lambda: _client(
        orders=[_order("PO%02d" % i, spm_po_sent_at=_ago(100 + i))
                for i in range(40)], nlng_orders=[]))
    out = M.orders_stuck(days=21, limit=5)
    assert "40 order(s)" in out
    assert "showing 5" in out
    assert len([l for l in out.split("\n") if " days at: " in l]) == 5


# ── find_orders ─────────────────────────────────────────────────────────────

def test_a_po_is_found_with_or_without_leading_zeros(monkeypatch):
    monkeypatch.setattr(M, "get_client", lambda: _client(
        orders=[_order("0061488314", stock_check_completed_at=_ago(26))],
        nlng_orders=[]))
    for q in ("0061488314", "61488314", "61488"):
        assert "0061488314" in M.find_orders(q), q


def test_an_order_is_found_by_so_or_spm_po_number(monkeypatch):
    monkeypatch.setattr(M, "get_client", lambda: _client(
        orders=[_order("0061488314", so_number="SO718430",
                       spm_po_number="S.P.M.-3095")], nlng_orders=[]))
    assert "0061488314" in M.find_orders("SO718430")
    assert "0061488314" in M.find_orders("3095")


def test_an_empty_query_asks_for_one_rather_than_dumping_the_table(monkeypatch):
    monkeypatch.setattr(M, "get_client", lambda: _client(
        orders=[_order("A"), _order("B")], nlng_orders=[]))
    out = M.find_orders("")
    assert "PO A" not in out and "PO B" not in out


def test_no_match_says_so(monkeypatch):
    monkeypatch.setattr(M, "get_client", lambda: _client(
        orders=[_order("A")], nlng_orders=[]))
    assert "Nothing matches" in M.find_orders("zzzzz")


# ── order_detail ────────────────────────────────────────────────────────────

def test_the_detail_shows_every_stage_ticked_or_not(monkeypatch):
    monkeypatch.setattr(M, "get_client", lambda: _client(
        orders=[_order("0061488314", sent_to_warehouse_at=_ago(60),
                       stock_check_completed_at=None, spm_po_sent_at=_ago(30))],
        nlng_orders=[], ai_memory=[]))
    out = M.order_detail("0061488314")
    assert "[x] sent to warehouse for stock check" in out
    assert "[ ] stock check completed" in out
    assert "[x] SPM PO sent to supplier" in out


def test_the_saved_story_is_reused_not_rewritten(monkeypatch):
    """It was already paid for by the web app. Writing a new one here would
    need the API, which is the cost this server exists to avoid."""
    monkeypatch.setattr(M, "get_client", lambda: _client(
        orders=[_order("0061488314", spm_po_sent_at=_ago(30))],
        nlng_orders=[],
        ai_memory=[{"order_id": "id-0061488314", "order_type": "chevron",
                    "summary": "Flexitallic acknowledged on 3 September.",
                    "updated_at": "2026-09-25T10:00:00+00:00"}]))
    out = M.order_detail("0061488314")
    assert "Flexitallic acknowledged on 3 September." in out
    assert "2026-09-25" in out


def test_an_nlng_po_is_found_in_its_own_table(monkeypatch):
    monkeypatch.setattr(M, "get_client", lambda: _client(
        orders=[], ai_memory=[],
        nlng_orders=[{"id": "n1", "po_number": "4200093075", "so_number": "SO718430",
                      "spm_po_number": "S.P.M.-3095", "contact_name": "Theodora",
                      "currency": "USD", "net_value": 6310.96,
                      "overall_status": "ready_for_dispatch",
                      "ready_for_dispatch_at": _ago(51)}]))
    out = M.order_detail("4200093075")
    assert "NLNG PO 4200093075" in out
    assert "Theodora" in out
    assert "6310.96" in out


def test_an_unknown_po_says_so(monkeypatch):
    monkeypatch.setattr(M, "get_client",
                        lambda: _client(orders=[], nlng_orders=[], ai_memory=[]))
    assert "No order found" in M.order_detail("9999999999")


# ── search_quotes ───────────────────────────────────────────────────────────

def _quotes_client(items, quotes=None):
    return _client(
        quotations=quotes or [
            {"id": "q1", "quote_number": "SPM-00058-SIS", "client": "others",
             "recipient_company": "Siso Integrated Services",
             "subject": "Quotation To Siso", "quote_date": "2026-10-19",
             "currency": "USD", "total": 1766.5, "status": "sent"}],
        quotation_line_items=items)


def test_a_quote_is_found_by_what_is_on_it_not_just_the_title(monkeypatch):
    monkeypatch.setattr(M, "get_client", lambda: _quotes_client([
        {"quotation_id": "q1", "description": "Spiral Wound Gasket CGI",
         "part_number": "", "quantity": 9, "unit_price": 6.38, "uom": "EA"}]))
    assert "SPM-00058-SIS" in M.search_quotes(text="spiral wound")


def test_the_markup_is_stripped_from_what_is_shown(monkeypatch):
    """A live call returned 'Spiral Wound Gasket.<br>NPS -3/4"<' - cut off
    mid-tag, with the markup eating the width."""
    monkeypatch.setattr(M, "get_client", lambda: _quotes_client([
        {"quotation_id": "q1",
         "description": '<font color="#222222">Full Face Gasket</font><br>Size: 1"',
         "part_number": "", "quantity": 1, "unit_price": 22.8, "uom": "EA"}]))
    out = M.search_quotes(text="full face")
    assert "Full Face Gasket Size: 1\"" in out
    assert "<font" not in out and "<br>" not in out


def test_a_search_matches_through_the_markup(monkeypatch):
    """The words are inside tags, so matching the raw HTML would miss a term
    split by a <font> boundary."""
    monkeypatch.setattr(M, "get_client", lambda: _quotes_client([
        {"quotation_id": "q1",
         "description": "<b>SPIRAL</b> <i>WOUND</i> 3/4 IN",
         "part_number": "", "quantity": 3, "unit_price": 29.64, "uom": "EA"}]))
    assert "SPM-00058-SIS" in M.search_quotes(text="spiral wound 3/4")


def test_only_the_matching_items_are_listed(monkeypatch):
    """A 40-line quote should not fill the reply because one line matched."""
    monkeypatch.setattr(M, "get_client", lambda: _quotes_client([
        {"quotation_id": "q1", "description": "Spiral Wound Gasket",
         "part_number": "", "quantity": 1, "unit_price": 1, "uom": "EA"},
        {"quotation_id": "q1", "description": "Hex Bolt M20",
         "part_number": "", "quantity": 2, "unit_price": 2, "uom": "EA"}]))
    out = M.search_quotes(text="gasket")
    assert "Spiral Wound Gasket" in out
    assert "Hex Bolt" not in out


def test_quotes_can_be_narrowed_by_client_and_date(monkeypatch):
    quotes = [
        {"id": "q1", "quote_number": "OLD", "client": "chevron",
         "recipient_company": "CNL", "subject": "s", "quote_date": "2025-01-01",
         "currency": "USD", "total": 1, "status": "sent"},
        {"id": "q2", "quote_number": "NEW", "client": "chevron",
         "recipient_company": "CNL", "subject": "s", "quote_date": "2026-10-01",
         "currency": "USD", "total": 2, "status": "sent"},
        {"id": "q3", "quote_number": "OTHER", "client": "nlng",
         "recipient_company": "NLNG", "subject": "s", "quote_date": "2026-10-02",
         "currency": "USD", "total": 3, "status": "sent"}]
    monkeypatch.setattr(M, "get_client", lambda: _quotes_client([], quotes))
    out = M.search_quotes(client="chevron", since="2026-01-01")
    assert "NEW" in out
    assert "OLD" not in out and "OTHER" not in out


# ── search_mail ─────────────────────────────────────────────────────────────

def test_mail_is_searched_in_subject_and_body(monkeypatch):
    monkeypatch.setattr(M, "get_client", lambda: _client(emails=[
        {"subject": "Orders packed and ready", "from_address": "f@x.com",
         "received_at": "2026-09-21T10:00:00", "direction": "in",
         "body_text": "nothing relevant", "order_id": "o1"},
        {"subject": "Re: PO", "from_address": "f@x.com",
         "received_at": "2026-09-20T10:00:00", "direction": "out",
         "body_text": "they are packed and ready now", "order_id": "o1"}]))
    out = M.search_mail("packed and ready")
    assert "2 message(s)" in out


def test_the_newest_mail_comes_first(monkeypatch):
    monkeypatch.setattr(M, "get_client", lambda: _client(emails=[
        {"subject": "older", "from_address": "f@x", "received_at": "2026-01-01",
         "direction": "in", "body_text": "delay", "order_id": "o1"},
        {"subject": "newer", "from_address": "f@x", "received_at": "2026-09-01",
         "direction": "in", "body_text": "delay", "order_id": "o1"}]))
    out = M.search_mail("delay")
    assert out.index("newer") < out.index("older")


def test_no_mail_found_explains_the_coverage(monkeypatch):
    """Rather than implying the mail does not exist. Before an order has been
    walked, only what a listener recognised is stored, and the reply has to say
    so or an answer gets built on a gap."""
    monkeypatch.setattr(M, "get_client", lambda: _client(emails=[]))
    out = M.search_mail(text="anything")
    assert "email_thread_sync" in out
    assert "refresh_order_mail" in out
    assert "message(s) stored" in out


def test_no_filters_returns_recent_mail_rather_than_refusing(monkeypatch):
    """Changed deliberately. The first version demanded a search word, which
    meant the only questions askable were the ones it had been built for. With
    every filter optional, "what has come in lately" is a question too - and it
    is how you see what the archive actually holds."""
    monkeypatch.setattr(M, "get_client", lambda: _client(emails=[
        {"message_id": "<a@x>", "subject": "Recent", "from_address": "f@x.com",
         "to_address": "s@x.com", "received_at": "2026-10-16T08:00:00",
         "direction": "in", "body_text": "hello", "order_id": "o1"}]))
    out = M.search_mail()
    assert "Recent" in out


def test_filters_combine_with_and(monkeypatch):
    """The point of making them general: any combination is a question. A
    sender who did write, but not in that month, must not match."""
    rows = [
        {"message_id": "<a@x>", "subject": "A", "from_address": "penny@flexitallic.eu",
         "to_address": "spm@x.com", "received_at": "2026-09-18T08:00:00",
         "direction": "in", "body_text": "delay on the forwarder", "order_id": "o1"},
        {"message_id": "<b@x>", "subject": "B", "from_address": "penny@flexitallic.eu",
         "to_address": "spm@x.com", "received_at": "2026-06-01T08:00:00",
         "direction": "in", "body_text": "delay on the forwarder", "order_id": "o1"},
        {"message_id": "<c@x>", "subject": "C", "from_address": "dsv@x.com",
         "to_address": "spm@x.com", "received_at": "2026-09-20T08:00:00",
         "direction": "in", "body_text": "delay on the forwarder", "order_id": "o1"},
    ]
    monkeypatch.setattr(M, "get_client", lambda: _client(emails=rows))
    out = M.search_mail(sender="flexitallic", text="delay",
                        since="2026-09-01", until="2026-09-30")
    assert "subject: A" in out or "] RECV A" in out
    assert "] RECV B" not in out      # right sender, wrong month
    assert "] RECV C" not in out      # right month, wrong sender


def test_until_includes_the_whole_day_given(monkeypatch):
    """until="2026-09-30" has to include the 30th. Comparing a full timestamp
    against a bare date would silently drop that day."""
    monkeypatch.setattr(M, "get_client", lambda: _client(emails=[
        {"message_id": "<a@x>", "subject": "LastDay", "from_address": "f@x",
         "to_address": "s@x", "received_at": "2026-09-30T23:30:00",
         "direction": "in", "body_text": "x", "order_id": "o1"}]))
    assert "LastDay" in M.search_mail(until="2026-09-30")


def test_sent_and_received_can_be_told_apart(monkeypatch):
    monkeypatch.setattr(M, "get_client", lambda: _client(emails=[
        {"message_id": "<a@x>", "subject": "Ours", "from_address": "spm@x",
         "to_address": "f@x", "received_at": "2026-09-01", "direction": "out",
         "body_text": "x", "order_id": "o1"},
        {"message_id": "<b@x>", "subject": "Theirs", "from_address": "f@x",
         "to_address": "spm@x", "received_at": "2026-09-02", "direction": "in",
         "body_text": "x", "order_id": "o1"}]))
    assert "Ours" in M.search_mail(direction="out")
    assert "Theirs" not in M.search_mail(direction="out")
    assert "Theirs" in M.search_mail(direction="in")


def test_a_message_id_is_printed_so_it_can_be_read_in_full(monkeypatch):
    """search_mail returns excerpts; read_message needs the id to go deeper.
    Without the id in the output the two tools cannot be combined."""
    monkeypatch.setattr(M, "get_client", lambda: _client(emails=[
        {"message_id": "<findme@x>", "subject": "S", "from_address": "f@x",
         "to_address": "s@x", "received_at": "2026-09-01", "direction": "in",
         "body_text": "x" * 900, "order_id": "o1"}]))
    out = M.search_mail(text="x")
    assert "<findme@x>" in out
    assert "full_bodies" in out      # and says how to get the whole thing


def test_full_bodies_returns_the_whole_message(monkeypatch):
    monkeypatch.setattr(M, "get_client", lambda: _client(emails=[
        {"message_id": "<a@x>", "subject": "S", "from_address": "f@x",
         "to_address": "s@x", "received_at": "2026-09-01", "direction": "in",
         "body_text": "START " + "y" * 800 + " END", "order_id": "o1"}]))
    brief = M.search_mail(text="START")
    whole = M.search_mail(text="START", full_bodies=True)
    assert "END" not in brief
    assert "END" in whole


# ── reading a thread end to end ─────────────────────────────────────────────

def _thread_client(order_po="4200093075", msgs=None):
    return _client(
        orders=[],
        nlng_orders=[{"id": "n1", "po_number": order_po}],
        emails=msgs or [])


def test_a_thread_reads_oldest_first(monkeypatch):
    """A conversation is read forwards. Newest-first is right for a search
    result and wrong for following what happened."""
    monkeypatch.setattr(M, "get_client", lambda: _thread_client(msgs=[
        {"message_id": "<b@x>", "subject": "second", "from_address": "f@x",
         "to_address": "s@x", "received_at": "2026-09-02", "direction": "in",
         "body_text": "later", "nlng_order_id": "n1"},
        {"message_id": "<a@x>", "subject": "first", "from_address": "f@x",
         "to_address": "s@x", "received_at": "2026-09-01", "direction": "in",
         "body_text": "earlier", "nlng_order_id": "n1"}]))
    out = M.read_mail_thread("4200093075")
    assert out.index("first") < out.index("second")


def test_an_undated_message_is_not_read_as_the_latest(monkeypatch):
    """It is of unknown age. Put last, it reads as the most recent thing that
    happened - the same trap the archiver and the story both guard against."""
    monkeypatch.setattr(M, "get_client", lambda: _thread_client(msgs=[
        {"message_id": "<a@x>", "subject": "dated", "from_address": "f@x",
         "to_address": "s@x", "received_at": "2026-09-01", "direction": "in",
         "body_text": "x", "nlng_order_id": "n1"},
        {"message_id": "<u@x>", "subject": "undated", "from_address": "f@x",
         "to_address": "s@x", "received_at": None, "direction": "in",
         "body_text": "x", "nlng_order_id": "n1"}]))
    out = M.read_mail_thread("4200093075")
    assert out.index("undated") < out.index("dated")


def test_an_oversized_thread_says_what_it_cut(monkeypatch):
    """A 41-message thread ran to 280 lines. Cutting silently would have an
    answer built on a conversation that stopped early without saying so."""
    msgs = [{"message_id": f"<m{i}@x>", "subject": f"msg{i}",
             "from_address": "f@x", "to_address": "s@x",
             "received_at": "2026-09-%02d" % (i + 1), "direction": "in",
             "body_text": "z" * 4000, "nlng_order_id": "n1"}
            for i in range(40)]
    monkeypatch.setattr(M, "get_client", lambda: _thread_client(msgs=msgs))
    out = M.read_mail_thread("4200093075")
    assert "40 message(s)" in out
    assert "showing the first" in out
    assert "since=" in out          # and how to read on


def test_a_thread_with_nothing_stored_points_at_the_refresh(monkeypatch):
    monkeypatch.setattr(M, "get_client", lambda: _thread_client(msgs=[]))
    out = M.read_mail_thread("4200093075")
    assert "refresh_order_mail" in out


def test_reading_one_message_gives_the_whole_body(monkeypatch):
    monkeypatch.setattr(M, "get_client", lambda: _client(emails=[
        {"message_id": "<a@x>", "subject": "S", "from_address": "f@x.com",
         "to_address": "s@x.com", "received_at": "2026-09-01T10:00:00",
         "direction": "out", "body_text": "START " + "y" * 2000 + " END",
         "order_id": "o1"}]))
    out = M.read_message("<a@x>")
    assert "START" in out and "END" in out
    assert "SENT by SPM" in out


def test_an_unknown_message_id_says_so(monkeypatch):
    monkeypatch.setattr(M, "get_client", lambda: _client(emails=[]))
    assert "No stored message" in M.read_message("<nope@x>")


# ── stock_on_hand ───────────────────────────────────────────────────────────

def test_stock_carries_the_date_it_was_counted(monkeypatch):
    """These are hand counts, not a calculated balance. A quantity without the
    date it was taken invites someone to trust a six-month-old figure."""
    monkeypatch.setattr(M, "get_client", lambda: _client(stock_items=[
        {"stock_id": "STK-0005", "description": 'GASKET 1" X 150 CG 316',
         "quantity": 12, "uom": "EA", "unit_cost": 5,
         "last_counted_at": "2026-09-30T10:00:00"}]))
    out = M.stock_on_hand()
    assert "STK-0005" in out
    assert "2026-09-30" in out


def test_stock_can_be_filtered_by_description(monkeypatch):
    monkeypatch.setattr(M, "get_client", lambda: _client(stock_items=[
        {"stock_id": "A", "description": "GASKET", "quantity": 1, "uom": "EA",
         "unit_cost": 1, "last_counted_at": None},
        {"stock_id": "B", "description": "HEX BOLT", "quantity": 2, "uom": "EA",
         "unit_cost": 1, "last_counted_at": None}]))
    out = M.stock_on_hand(text="gasket")
    assert "STK" not in out or "A" in out
    assert "HEX BOLT" not in out


# ── read-only, and that is the security boundary ────────────────────────────

def _server_code() -> str:
    src = (ROOT / "scripts" / "mcp_server.py").read_text(encoding="utf-8")
    # Comments out, and sys.path.insert is not a database call.
    return "\n".join(l for l in src.split("\n")
                     if not l.lstrip().startswith("#")
                     and "sys.path.insert" not in l)


def test_nothing_here_writes_to_the_database_directly():
    """SUPABASE_KEY is the service_role key and bypasses every row-level rule,
    so the write surface is the boundary rather than a convention.

    This file performs no write of its own at all. The one write that happens
    through it - refresh_order_mail appending fetched mail - goes through
    email_thread_sync, which is tested separately and can only reach the emails
    and email_thread_sync tables. Nothing can touch an order, a quotation, a
    line item, a stock count or a user."""
    code = _server_code()
    for forbidden in (".insert(", ".update(", ".upsert(", ".delete(", ".rpc("):
        assert forbidden not in code, forbidden


def test_the_only_table_this_file_names_for_writing_is_none():
    """Every .table(...) call here is followed by .select(...). If a future
    edit chains a mutation onto one, this fails."""
    import re
    code = _server_code()
    for call in re.finditer(r'\.table\("(\w+)"\)(\s*\\?\s*\.?\s*\w*)', code):
        table, nxt = call.group(1), call.group(2)
        assert "select" in nxt or nxt.strip() in ("", "\\"), (table, nxt)


def test_the_one_write_path_is_delegated_and_named(monkeypatch):
    """refresh_order_mail must not grow its own writing code. It calls
    sync_thread, whose writes are bounded and tested in
    test_email_thread_sync.py."""
    src = (ROOT / "scripts" / "mcp_server.py").read_text(encoding="utf-8")
    body = src.split("def refresh_order_mail")[1].split("\n@mcp.tool()")[0]
    assert "ets.sync_thread(" in body
    for forbidden in (".insert(", ".update(", ".upsert(", ".delete("):
        assert forbidden not in body, forbidden


def test_a_gmail_failure_is_reported_not_swallowed(monkeypatch):
    """The silent version of this was a real bug: a failed search reported as
    "0 emails" and stamped the order as walked, so a thread nobody had read
    looked exactly like a thread with nothing in it."""
    import email_thread_sync as ets
    monkeypatch.setattr(M, "get_client", lambda: _client(
        orders=[_order("0061488314")], nlng_orders=[]))
    monkeypatch.setattr(ets, "read_thread", lambda oid, ot: [])

    def _boom(*a, **k):
        raise ets.ThreadSyncFailed("search failed: socket timeout")

    monkeypatch.setattr(ets, "sync_thread", _boom)
    out = M.refresh_order_mail("0061488314")
    assert "could not be read" in out
    assert "try again" in out
    assert "socket timeout" in out


def test_a_refresh_reports_what_it_gained(monkeypatch):
    import email_thread_sync as ets
    monkeypatch.setattr(M, "get_client", lambda: _client(
        orders=[_order("0061488314")], nlng_orders=[]))
    counts = iter([3, 22])      # before, after
    monkeypatch.setattr(ets, "read_thread", lambda oid, ot: [None] * next(counts))
    monkeypatch.setattr(ets, "sync_thread", lambda *a, **k: 22)
    out = M.refresh_order_mail("0061488314")
    assert "3 -> 22" in out
    assert "+19 new" in out


def test_a_refresh_on_an_unknown_po_says_so(monkeypatch):
    monkeypatch.setattr(M, "get_client",
                        lambda: _client(orders=[], nlng_orders=[]))
    assert "No order found" in M.refresh_order_mail("9999999999")


def test_the_refresh_tool_warns_that_it_is_slow():
    """Its own measurements: a 28 second login and a 7 minute thread. Somebody
    reaching for it in a chat should know before they wait."""
    src = (ROOT / "scripts" / "mcp_server.py").read_text(encoding="utf-8")
    doc = src.split("def refresh_order_mail")[1].split('"""')[1]
    assert "SLOW" in doc
    assert "minute" in doc.lower()


def test_there_is_no_free_text_gmail_search():
    """The mailbox holds everything; the emails table holds order
    correspondence. And Gmail caps simultaneous IMAP connections - the worker
    already holds several, and ad-hoc searches competing for them would break
    the pipeline listeners."""
    code = _server_code()
    assert "X-GM-RAW" not in code
    assert "fetch_thread(" not in code     # only sync_thread, which is per-order
    assert "open_imap" not in code


def test_there_is_no_run_any_sql_tool():
    """One such tool would hand the whole database over in a single step and
    make every other limit here decorative."""
    src = (ROOT / "scripts" / "mcp_server.py").read_text(encoding="utf-8")
    low = src.lower()
    for smell in ("def run_sql", "def query(", "def execute_sql", "def raw_query"):
        assert smell not in low, smell


def test_every_tool_that_can_return_many_rows_caps_itself():
    """A question whose honest answer is most of the database should come back
    trimmed and say so. order_detail is exempt because it is bounded by its
    own shape - one PO in, that order out - so a row cap would be noise."""
    src = (ROOT / "scripts" / "mcp_server.py").read_text(encoding="utf-8")
    bodies = src.split("@mcp.tool()")[1:]
    assert len(bodies) == 9
    # Bounded by their own shape: one PO in, one order (or one sync) out.
    bounded_by_shape = {"order_detail", "refresh_order_mail",
                        "read_mail_thread", "read_message"}
    for b in bodies:
        name = b.split("def ", 1)[1].split("(", 1)[0]
        if name in bounded_by_shape:
            assert "limit" not in b.split('"""')[0], name
            continue
        assert "MAX_ROWS" in b, name


def test_it_needs_no_anthropic_key():
    """The whole point: the thinking comes from whatever connects, so on Claude
    Code that is the subscription and these answers cost nothing per question."""
    src = (ROOT / "scripts" / "mcp_server.py").read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY" not in src.replace(
        "no ANTHROPIC_API_KEY", "")           # the docstring may mention it
    assert "import anthropic" not in src


def test_the_sdk_is_in_the_deployed_requirements():
    """This asserted the opposite until the server got its own Railway service.

    The reasoning was that the SDK is a laptop-only tool and has no business in
    the production image. That held while the only client was Claude Code over
    stdio. Once the Procfile gained an `mcp` process, it stopped holding:
    Railway builds ONE image from requirements.txt and shares it across web,
    worker and mcp, so keeping the SDK in a separate file would have left that
    process unable to start.
    """
    reqs = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    lines = [l.strip() for l in reqs.split("\n")
             if l.strip() and not l.strip().startswith("#")]
    assert any(l.lower().startswith("mcp") for l in lines), lines
    # And the separate file is gone, so there is one place to look.
    assert not (ROOT / "requirements-mcp.txt").exists()


def test_the_procfile_runs_it_as_its_own_process():
    """Its own service rather than mounted into the web app: a connector
    lookup can sit for a minute or two on a Gmail fetch, and the site runs on a
    single async worker, so sharing it would make the website slow for everyone
    while one question was answered."""
    proc = (ROOT / "Procfile").read_text(encoding="utf-8")
    mcp_line = [l for l in proc.split("\n") if l.startswith("mcp:")]
    assert len(mcp_line) == 1, proc
    assert "--http" in mcp_line[0]
    assert "mcp_server.py" in mcp_line[0]


def test_http_mode_refuses_to_start_half_configured():
    """Both of these fail from the client end in ways that are hard to
    diagnose - a wrong public URL sends a client to the wrong place to
    authenticate, and a missing secret makes every token unverifiable. Better
    to refuse at boot with a message."""
    src = (ROOT / "scripts" / "mcp_server.py").read_text(encoding="utf-8")
    build = src.split("def _build_server")[1].split("\nmcp = ")[0]
    assert "MCP_PUBLIC_URL" in build
    assert "JWT_SECRET" in build
    assert build.count("raise SystemExit") == 2


# ── who the HTTP server lets in ─────────────────────────────────────────────

def test_a_connector_token_cannot_log_into_the_website():
    """The whole reason a connector token can be long-lived. claude.ai sends a
    FIXED header on every request, so a 7-day login token would need
    re-pasting weekly - and a year-long one would be alarming if it doubled as
    a website login. The audience claim means it does not: the web app's
    decode, which passes no audience, raises on it."""
    import jwt as pyjwt
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    secret = "x" * 32
    connector = pyjwt.encode(
        {"sub": "u1", "role": "admin", "aud": M.MCP_AUDIENCE,
         "exp": _dt.now(_tz.utc) + _td(days=365)}, secret, algorithm="HS256")
    with pytest.raises(pyjwt.InvalidAudienceError):
        pyjwt.decode(connector, secret, algorithms=["HS256"])


def test_a_website_login_token_cannot_reach_the_mcp_server():
    """The separation has to hold both ways, or the long expiry is the only
    thing standing between a leaked login and the whole database."""
    import jwt as pyjwt
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    secret = "x" * 32
    login = pyjwt.encode({"sub": "u1", "role": "admin",
                          "exp": _dt.now(_tz.utc) + _td(days=7)},
                         secret, algorithm="HS256")
    with pytest.raises(pyjwt.MissingRequiredClaimError):
        pyjwt.decode(login, secret, algorithms=["HS256"],
                     audience=M.MCP_AUDIENCE)


def _verify(monkeypatch, token, users=None, raise_db=False):
    import asyncio
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    if raise_db:
        def _boom():
            raise Exception("supabase unreachable")
        monkeypatch.setattr(M, "get_client", _boom)
    else:
        monkeypatch.setattr(M, "get_client", lambda: _client(
            users=users if users is not None else
            [{"id": "u1", "email": "f@spm.com", "role": "admin", "is_active": True}]))
    return asyncio.run(M.SPMTokenVerifier().verify_token(token))


def _tok(**over):
    import jwt as pyjwt
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    claims = {"sub": "u1", "role": "admin", "aud": M.MCP_AUDIENCE,
              "exp": _dt.now(_tz.utc) + _td(days=1)}
    claims.update(over.pop("claims", {}))
    return pyjwt.encode(claims, over.pop("secret", "x" * 32), algorithm="HS256")


def test_a_proper_connector_token_is_accepted(monkeypatch):
    got = _verify(monkeypatch, _tok())
    assert got is not None
    assert got.subject == "u1"
    assert "role:admin" in got.scopes


def test_the_live_role_wins_over_the_one_in_the_token(monkeypatch):
    """So a demotion applies immediately rather than when the token expires -
    and with a year-long token, "when it expires" is no answer at all."""
    got = _verify(monkeypatch, _tok(claims={"role": "admin"}),
                  users=[{"id": "u1", "email": "f@spm.com",
                          "role": "warehouse", "is_active": True}])
    assert "role:warehouse" in got.scopes
    assert "role:admin" not in got.scopes


def test_a_disabled_account_loses_the_connector_too(monkeypatch):
    """How you revoke a token. Without the live lookup, disabling somebody's
    account would leave their connector working for up to a year."""
    assert _verify(monkeypatch, _tok(),
                   users=[{"id": "u1", "email": "f@spm.com",
                           "role": "admin", "is_active": False}]) is None


def test_an_account_that_no_longer_exists_is_refused(monkeypatch):
    assert _verify(monkeypatch, _tok(), users=[]) is None


def test_a_token_signed_with_the_wrong_secret_is_refused(monkeypatch):
    assert _verify(monkeypatch, _tok(secret="y" * 32)) is None


def test_an_expired_token_is_refused(monkeypatch):
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    assert _verify(monkeypatch, _tok(
        claims={"exp": _dt.now(_tz.utc) - _td(days=1)})) is None


def test_a_token_with_no_subject_is_refused(monkeypatch):
    assert _verify(monkeypatch, _tok(claims={"sub": ""})) is None


def test_an_unreachable_database_refuses_rather_than_waving_it_through(monkeypatch):
    """web/app.py lets a valid token through when the database is down, so the
    site stays usable. This does not: a read-only lookup tool is not worth
    opening on an account nobody can verify."""
    assert _verify(monkeypatch, _tok(), raise_db=True) is None


def test_no_secret_configured_refuses_everything(monkeypatch):
    import asyncio
    monkeypatch.setenv("JWT_SECRET", "")
    assert asyncio.run(M.SPMTokenVerifier().verify_token(_tok())) is None


def test_an_empty_token_is_refused(monkeypatch):
    import asyncio
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    assert asyncio.run(M.SPMTokenVerifier().verify_token("")) is None

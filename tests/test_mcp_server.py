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

def test_the_furthest_stage_reached_is_what_counts():
    """Read from the end backwards. An order that has been dispatched has been
    dispatched whether or not every earlier box is ticked - the auditor fills
    those gaps in later, and a live call on PO 4200093075 found one: stock
    check blank, but the SPM PO sent and the SO received."""
    row = {"sent_to_warehouse_at": _ago(90),
           "stock_check_completed_at": None,          # the gap
           "spm_po_sent_at": _ago(60),
           "so_received_at": _ago(55)}
    label, _, waited = M._where_it_sits(row, M._CHEVRON_STAGES)
    assert label == "supplier acknowledged (SO)"
    assert 54 <= waited <= 56


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
    walked by the archiver, only what a listener recognised is here."""
    monkeypatch.setattr(M, "get_client", lambda: _client(emails=[]))
    out = M.search_mail("anything")
    assert "archiver" in out.lower()


def test_an_empty_search_is_refused(monkeypatch):
    monkeypatch.setattr(M, "get_client", lambda: _client(emails=[]))
    assert "search the mail for" in M.search_mail("")


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
    assert len(bodies) == 7
    # Bounded by their own shape: one PO in, one order (or one sync) out.
    bounded_by_shape = {"order_detail", "refresh_order_mail"}
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


def test_the_sdk_is_not_in_the_deployed_requirements():
    """Railway builds requirements.txt. The MCP SDK is a local-only tool and
    would be installed into the production image for nothing."""
    reqs = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "\nmcp" not in reqs and not reqs.startswith("mcp")

"""
test_gap_auditor.py — the auditor's auto-fix writers.

The auditor is the only part of the pipeline that writes to live orders
without a human or an email triggering it. It finds a null timestamp, searches
Gmail, and if a matching email exists it stamps the column itself. That makes
it the most dangerous module in the project: a bug here does not fail loudly,
it silently rewrites order history, and the wrong timestamp on an order is
worse than a missing one because nobody goes looking for it.

Every write it makes is guarded by `.is_(field, "null")` so an existing value
is never overwritten. That guard is the safety property these tests exist to
hold in place — it is one method call, easy to drop in a refactor, and nothing
else in the codebase would notice if it went.

The fake database below honours the guard the way PostgREST does: an update
filtered on `.is_(field, "null")` returns no rows when the column already has
a value, and the auditor keys every decision off that empty result.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
os.environ.setdefault("GMAIL_EMAIL", "x@y.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "x")

import pytest

import gap_auditor as G
import sync


# ── a stand-in for PostgREST ────────────────────────────────────────────────

class _Query:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self.rows = list(store.setdefault(name, []))
        self.op, self.payload = "select", None

    def select(self, *a, **k):
        self.op = "select"
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def eq(self, field, value):
        self.rows = [r for r in self.rows if str(r.get(field)) == str(value)]
        return self

    def is_(self, field, _null):
        # The whole point: only rows whose column is still empty match.
        self.rows = [r for r in self.rows if r.get(field) is None]
        return self

    def execute(self):
        class R:
            pass
        r = R()
        if self.op == "update":
            for row in self.rows:
                row.update(self.payload)
            self.store["_writes"].append((self.name, dict(self.payload), len(self.rows)))
        r.data = self.rows
        return r


class _DB:
    def __init__(self, **tables):
        self.store = {k: v for k, v in tables.items()}
        self.store["_writes"] = []

    def table(self, name):
        return _Query(self.store, name)

    @property
    def writes(self):
        return self.store["_writes"]


@pytest.fixture
def no_status(monkeypatch):
    """Record advance_status calls instead of hitting the real one."""
    calls = []
    monkeypatch.setattr(sync, "advance_status",
                        lambda db, oid, status: calls.append((oid, status)))
    return calls


# ── _classify ───────────────────────────────────────────────────────────────

def test_an_email_that_exists_is_a_parser_miss():
    """A null column plus a matching email means the parser missed it."""
    assert G._classify([{"uid": 1}]) == G.MISS


def test_no_email_is_a_real_null():
    """Nothing to find means the stage genuinely has not happened yet. These
    rows must be left alone — stamping them would invent history."""
    assert G._classify([]) == G.REAL
    assert G._classify(None) == G.REAL


# ── _fix_order_timestamp ────────────────────────────────────────────────────

def test_a_null_timestamp_is_stamped(no_status):
    db = _DB(orders=[{"id": "o1", "buyer_po_number": "0061451715",
                      "so_received_at": None}])
    fixed = G._fix_order_timestamp(db, "0061451715", "so_received_at",
                                   "2026-10-14T09:00:00Z", "so_received")
    assert fixed is True
    assert db.store["orders"][0]["so_received_at"] == "2026-10-14T09:00:00Z"
    assert no_status == [("o1", "so_received")]


def test_a_timestamp_that_is_already_set_is_never_overwritten(no_status):
    """The guard that matters. An order acknowledged on the 3rd must not be
    restamped to the 14th because the auditor found a later email."""
    db = _DB(orders=[{"id": "o1", "buyer_po_number": "0061451715",
                      "so_received_at": "2026-10-03T08:00:00Z"}])
    fixed = G._fix_order_timestamp(db, "0061451715", "so_received_at",
                                   "2026-10-14T09:00:00Z", "so_received")
    assert fixed is False
    assert db.store["orders"][0]["so_received_at"] == "2026-10-03T08:00:00Z"


def test_the_status_does_not_move_when_nothing_was_stamped(no_status):
    """advance_status runs only inside `if result.data`. If it ever moved on an
    empty result, a fully delivered order could be dragged back to an earlier
    stage by an audit run that changed nothing."""
    db = _DB(orders=[{"id": "o1", "buyer_po_number": "P1",
                      "dispatched_at": "2026-09-01T00:00:00Z"}])
    G._fix_order_timestamp(db, "P1", "dispatched_at", "2026-10-14T00:00:00Z", "dispatched")
    assert no_status == []


def test_every_row_sharing_the_po_is_stamped(no_status):
    """One Chevron PO can span several order rows, one per line item. All of
    them reached the same stage at the same time."""
    db = _DB(orders=[{"id": "o1", "buyer_po_number": "P1", "f": None},
                     {"id": "o2", "buyer_po_number": "P1", "f": None},
                     {"id": "o3", "buyer_po_number": "P2", "f": None}])
    assert G._fix_order_timestamp(db, "P1", "f", "T", "s") is True
    assert [r["f"] for r in db.store["orders"]] == ["T", "T", None]
    assert sorted(o for o, _ in no_status) == ["o1", "o2"]


def test_a_mix_of_set_and_null_rows_only_stamps_the_nulls(no_status):
    db = _DB(orders=[{"id": "o1", "buyer_po_number": "P1", "f": "KEEP"},
                     {"id": "o2", "buyer_po_number": "P1", "f": None}])
    assert G._fix_order_timestamp(db, "P1", "f", "T", "s") is True
    assert db.store["orders"][0]["f"] == "KEEP"
    assert db.store["orders"][1]["f"] == "T"
    assert no_status == [("o2", "s")]


def test_an_unknown_po_changes_nothing(no_status):
    db = _DB(orders=[{"id": "o1", "buyer_po_number": "P1", "f": None}])
    assert G._fix_order_timestamp(db, "NOT-A-PO", "f", "T", "s") is False
    assert db.store["orders"][0]["f"] is None
    assert no_status == []


# ── _fix_spm_po_timestamp ───────────────────────────────────────────────────

def _spm_db(**over):
    base = dict(
        spm_purchase_orders=[{"id": "s1", "spm_po_ref": "3101",
                              "so_number": "SO-900", "f": None}],
        spm_po_chevron_links=[{"spm_po_id": "s1", "order_id": "o1"}],
        orders=[{"id": "o1", "f": None}],
    )
    base.update(over)
    return _DB(**base)


def test_the_spm_po_and_its_linked_orders_are_both_stamped(no_status):
    db = _spm_db()
    assert G._fix_spm_po_timestamp(db, "3101", None, "f", "T", "s") is True
    assert db.store["spm_purchase_orders"][0]["f"] == "T"
    assert db.store["orders"][0]["f"] == "T"
    assert no_status == [("o1", "s")]


def test_the_so_number_is_used_when_the_spm_ref_is_missing(no_status):
    """Older rows reached the auditor with no spm_po_ref, only an SO number."""
    db = _spm_db()
    assert G._fix_spm_po_timestamp(db, None, "SO-900", "f", "T", "s") is True
    assert db.store["spm_purchase_orders"][0]["f"] == "T"


def test_neither_reference_matching_is_not_treated_as_a_fix(no_status):
    db = _spm_db()
    assert G._fix_spm_po_timestamp(db, "NOPE", "ALSO-NOPE", "f", "T", "s") is False
    assert db.store["spm_purchase_orders"][0]["f"] is None
    assert no_status == []


def test_an_spm_po_with_no_linked_order_still_counts_as_fixed(no_status):
    """The header row was stamped even though no order hangs off it yet, so the
    auditor must not report it as an unfixed miss and search for it again."""
    db = _spm_db(spm_po_chevron_links=[])
    assert G._fix_spm_po_timestamp(db, "3101", None, "f", "T", "s") is True
    assert db.store["spm_purchase_orders"][0]["f"] == "T"


def test_a_link_with_a_null_order_id_is_skipped(no_status):
    db = _spm_db(spm_po_chevron_links=[{"spm_po_id": "s1", "order_id": None}])
    assert G._fix_spm_po_timestamp(db, "3101", None, "f", "T", "s") is True
    assert no_status == []


def test_an_spm_timestamp_already_set_is_not_overwritten(no_status):
    db = _spm_db(
        spm_purchase_orders=[{"id": "s1", "spm_po_ref": "3101",
                              "so_number": "SO-900", "f": "KEEP"}],
        orders=[{"id": "o1", "f": "KEEP"}])
    assert G._fix_spm_po_timestamp(db, "3101", None, "f", "T", "s") is False
    assert db.store["spm_purchase_orders"][0]["f"] == "KEEP"
    assert db.store["orders"][0]["f"] == "KEEP"


# ── _apply_stock_check_raw ──────────────────────────────────────────────────

def _patch_interpret(monkeypatch, result, method):
    import warehouse_reply_parser as W
    monkeypatch.setattr(W, "interpret_reply", lambda body: (result, method))


def test_a_followup_reply_writes_nothing(monkeypatch, no_status):
    """"Please confirm the part number" is a question, not a stock answer.
    Writing it would mark the stock check done when it has not started."""
    _patch_interpret(monkeypatch, {"overall_availability": "followup"}, "rules")
    db = _DB(orders=[{"id": "o1", "stock_check_raw": None}])
    G._apply_stock_check_raw(db, "o1", "please confirm the part number")
    assert db.store["orders"][0]["stock_check_raw"] is None
    assert db.writes == []
    assert no_status == []


def test_a_deferred_reply_is_flagged_for_review(monkeypatch, no_status):
    _patch_interpret(monkeypatch, {"overall_availability": "partial"}, "deferred")
    db = _DB(orders=[{"id": "o1", "stock_check_raw": None}])
    G._apply_stock_check_raw(db, "o1", "3 of the 5 are on the shelf")
    raw = db.store["orders"][0]["stock_check_raw"]
    assert raw["needs_human_review"] is True
    assert raw["interpretation_method"] == "deferred"
    assert db.store["orders"][0]["pending_stock_extraction"] is True
    assert no_status == [("o1", "stock_check_needs_review")]


def test_a_very_long_warehouse_reply_is_truncated(monkeypatch, no_status):
    """The raw body is stored on the row. A forwarded thread can run to tens of
    thousands of characters, and the column is not the place for it."""
    _patch_interpret(monkeypatch, {"overall_availability": "partial"}, "deferred")
    db = _DB(orders=[{"id": "o1", "stock_check_raw": None}])
    G._apply_stock_check_raw(db, "o1", "x" * 5000)
    assert len(db.store["orders"][0]["stock_check_raw"]["raw_body"]) == 1000


def test_a_clear_reply_completes_the_stock_check(monkeypatch, no_status):
    _patch_interpret(monkeypatch,
                     {"overall_availability": "available", "confidence": "high"}, "rules")
    db = _DB(orders=[{"id": "o1", "stock_check_raw": None}])
    G._apply_stock_check_raw(db, "o1", "all items in stock")
    assert db.store["orders"][0]["pending_stock_extraction"] is False
    assert no_status == [("o1", "stock_check_complete")]


@pytest.mark.parametrize("result", [
    {"overall_availability": "available", "needs_human_review": True},
    {"overall_availability": "available", "confidence": "low"},
])
def test_a_doubtful_reply_goes_to_review_not_complete(monkeypatch, no_status, result):
    """Either flag on its own is enough. Marking a doubtful reply complete hides
    it from the person who needs to read it."""
    _patch_interpret(monkeypatch, result, "rules")
    db = _DB(orders=[{"id": "o1", "stock_check_raw": None}])
    G._apply_stock_check_raw(db, "o1", "most of them I think")
    assert no_status == [("o1", "stock_check_needs_review")]


def test_an_existing_stock_check_is_not_replaced(monkeypatch, no_status):
    _patch_interpret(monkeypatch,
                     {"overall_availability": "available", "confidence": "high"}, "rules")
    db = _DB(orders=[{"id": "o1", "stock_check_raw": {"summary": "counted already"}}])
    G._apply_stock_check_raw(db, "o1", "all items in stock")
    assert db.store["orders"][0]["stock_check_raw"] == {"summary": "counted already"}
    assert no_status == []


# ── _is_ready_for_dispatch_body ─────────────────────────────────────────────

@pytest.mark.parametrize("body", [
    "Collection has been arranged with Unicorn for Friday.",
    "We have arranged RTC to collect on Tuesday.",
    "Transport arranged, collecting tomorrow.",
    "COLLECTION ARRANGED — PUDSEY DEPOT",
])
def test_a_collection_email_is_recognised(body):
    assert G._is_ready_for_dispatch_body(body) is True


@pytest.mark.parametrize("body", [
    "Please arrange payment for invoice 4412.",      # arrange, no shipper
    "Unicorn have updated their rates for 2026.",    # shipper, no arrangement
    "Noted, thanks.",                                 # neither
    "",
])
def test_an_unrelated_email_is_not_mistaken_for_one(body):
    """This decides whether ready_for_dispatch_at gets stamped. A false match
    marks an order ready to ship when no collection exists."""
    assert G._is_ready_for_dispatch_body(body) is False


# ── _decode ─────────────────────────────────────────────────────────────────

def test_a_plain_subject_is_returned_as_is():
    assert G._decode("Purchase Order 0061451715") == "Purchase Order 0061451715"


def test_an_encoded_subject_is_decoded():
    assert "Order" in G._decode("=?utf-8?q?Purchase_Order?=")


def test_an_empty_subject_does_not_raise():
    assert G._decode("") == ""
    assert G._decode(None) == ""


def test_undecodable_bytes_do_not_take_the_audit_down():
    """A malformed header once stopped a whole audit run. errors="replace"
    means one bad subject costs that subject, not the other 400 orders."""
    assert isinstance(G._decode("=?utf-8?q?=FF=FE_bad?="), str)

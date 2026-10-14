"""
test_cancellation.py - Unit tests for the Chevron PO cancellation parser.

parse_cancellation() is pure (no DB calls). The important cases are the ones
it must REJECT: Chevron uses the same "is cancelled" wording for sourcing
events, and acting on those would cancel orders that are perfectly healthy.
"""
import pytest
from cancellation_listener import parse_cancellation
from sync import derive_status, STATUS_RANK, rank

CANCEL = (
    'Dear SIMEON MOJU, New Order : "PO1 for JDEJobID 63183,BranchPlant '
    '29300000TL,Supplier 99999999"(0061419934) for $2,440.94 from Chevron '
    'received on 5/12/2026 (UTC) is cancelled by Elohor Chinakwe on '
    '7/10/2026 (UTC). You can view it in GEP SMART.'
)
CLOSED = (
    'Dear SIMEON MOJU, Order"PO1 for JDEJobID 57860,BranchPlant 29000000WE,'
    'Supplier 99999999" (0061154876) for $ 10,400.00 has been closed. You '
    'can contact Regina for details.'
)
EVENT = (
    'The event "RPRNGN0020803" has been canceled and will no longer be '
    'available for participation. Comments: This is cancelled to be '
    'reraised as direct charge.'
)


def test_cancellation_is_recognised():
    r = parse_cancellation("Chevron Order PO1", CANCEL)
    assert r is not None
    assert r["kind"] == "cancelled"
    assert r["po_number"] == "0061419934"
    assert r["actor"] == "Elohor Chinakwe"
    assert r["stated_date"] == "7/10/2026"


def test_closure_is_recognised_separately():
    r = parse_cancellation("Chevron Order PO1", CLOSED)
    assert r is not None
    assert r["kind"] == "closed"
    assert r["po_number"] == "0061154876"


def test_sourcing_event_cancellation_is_ignored():
    """The false positive that matters: an RFQ event, not a purchase order."""
    assert parse_cancellation("Event cancelled", EVENT) is None


def test_change_order_suffix_maps_to_base_po():
    body = 'Order"PO1 for EGTL" (0060834302-001) for $ 12,241.35 has been closed.'
    r = parse_cancellation("Chevron Order", body)
    assert r["po_number"] == "0060834302"
    assert r["raw_po_number"] == "0060834302-001"


def test_ordinary_new_po_email_is_ignored():
    body = ("This is to notify you that the Order: PO1 for JDEJobID 63183 "
            "(0061419934) received from Chevron of amount $ 2,440.94 is "
            "pending for acknowledgement.")
    assert parse_cancellation("Acknowledge PO1", body) is None


def test_empty_body_is_ignored():
    assert parse_cancellation("subject", "") is None
    assert parse_cancellation("subject", None) is None


def test_cancelled_status_derives_from_timestamp():
    assert derive_status({"cancelled_at": "2026-07-10T00:00:00+00:00"}) == "cancelled"


def test_cancellation_beats_every_other_milestone():
    """A cancelled order stays cancelled even with dispatch/delivery stamps."""
    order = {
        "cancelled_at": "2026-07-10T00:00:00+00:00",
        "notification_received_at": "2026-05-12T00:00:00+00:00",
        "stock_check_completed_at": "2026-06-01T00:00:00+00:00",
        "ready_for_dispatch_at": "2026-07-01T00:00:00+00:00",
        "delivered_at": "2026-07-05T00:00:00+00:00",
    }
    assert derive_status(order) == "cancelled"


def test_closure_kind_derives_as_closed():
    order = {"cancelled_at": "2026-08-06T00:00:00+00:00", "cancellation_kind": "closed"}
    assert derive_status(order) == "closed"


def test_nothing_can_advance_past_cancelled():
    """advance_status only touches rows ranking BELOW the target, and
    'cancelled' outranks everything, so it can never be moved off."""
    top = max(STATUS_RANK.values())
    assert rank("cancelled") == top
    for status in STATUS_RANK:
        lower = [s for s, r in STATUS_RANK.items() if r < rank(status)]
        assert "cancelled" not in lower

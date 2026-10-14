"""
test_derive_status.py — Unit tests for derive_status() in sync.py.

derive_status() is a pure function (no DB calls). It takes an order dict and
returns the string status that matches the furthest completed pipeline stage.
These tests ensure every stage transition is handled correctly.
"""
import pytest
from sync import derive_status

TS = "2026-06-01T09:00:00+00:00"


def test_empty_order_is_new():
    assert derive_status({}) == "new"


def test_notification_received():
    assert derive_status({"notification_received_at": TS}) == "pending_acknowledgment"


def test_acknowledged_via_timestamp():
    assert derive_status({"notification_received_at": TS, "acknowledged_at": TS}) == "acknowledged"


def test_acknowledged_via_status_field():
    assert derive_status({
        "notification_received_at": TS,
        "acknowledgment_status": "acknowledged"
    }) == "acknowledged"


def test_sent_to_warehouse():
    assert derive_status({
        "acknowledged_at": TS,
        "sent_to_warehouse_at": TS,
    }) == "awaiting_warehouse_stock_check"


def test_stock_check_complete():
    assert derive_status({
        "sent_to_warehouse_at": TS,
        "stock_check_completed_at": TS,
        "stock_check_raw": {"overall_availability": "available"},
    }) == "stock_check_complete"


def test_stock_check_needs_review_when_flagged():
    assert derive_status({
        "sent_to_warehouse_at": TS,
        "stock_check_completed_at": TS,
        "stock_check_raw": {"needs_human_review": True},
    }) == "stock_check_needs_review"


def test_stock_check_needs_review_when_low_confidence():
    assert derive_status({
        "sent_to_warehouse_at": TS,
        "stock_check_completed_at": TS,
        "stock_check_raw": {"confidence": "low"},
    }) == "stock_check_needs_review"


def test_po_sent():
    assert derive_status({
        "stock_check_completed_at": TS,
        "spm_po_sent_at": TS,
    }) == "po_sent"


def test_supplier_acknowledged_via_so_received():
    assert derive_status({
        "spm_po_sent_at": TS,
        "so_received_at": TS,
    }) == "supplier_acknowledged"


def test_supplier_acknowledged_via_promised_date():
    assert derive_status({
        "spm_po_sent_at": TS,
        "promised_date": "2026-07-01",
    }) == "supplier_acknowledged"


def test_dispatch_packed_awaiting_instruction():
    assert derive_status({
        "so_received_at": TS,
        "flex_dispatch_ready_at": TS,
    }) == "dispatch_packed_awaiting_instruction"


def test_dispatch_instruction_sent():
    assert derive_status({
        "flex_dispatch_ready_at": TS,
        "dispatch_instructions_sent_at": TS,
    }) == "dispatch_instruction_sent"


def test_so_sent_to_warehouse():
    assert derive_status({
        "dispatch_instructions_sent_at": TS,
        "so_sent_to_warehouse_at": TS,
    }) == "so_sent_to_warehouse"


def test_ready_for_dispatch():
    assert derive_status({
        "so_sent_to_warehouse_at": TS,
        "ready_for_dispatch_at": TS,
    }) == "ready_for_dispatch"


def test_dispatched():
    assert derive_status({
        "ready_for_dispatch_at": TS,
        "dispatched_at": TS,
    }) == "dispatched"


def test_delivery_requested():
    assert derive_status({
        "dispatched_at": TS,
        "delivery_requested_at": TS,
    }) == "delivery_requested"


def test_delivered():
    assert derive_status({
        "delivery_requested_at": TS,
        "delivered_at": TS,
    }) == "delivered"


def test_delivered_wins_over_all_other_fields():
    """delivered_at is the highest stage — it wins no matter what else is set."""
    assert derive_status({
        "notification_received_at": TS,
        "acknowledged_at": TS,
        "sent_to_warehouse_at": TS,
        "stock_check_completed_at": TS,
        "spm_po_sent_at": TS,
        "so_received_at": TS,
        "delivered_at": TS,
    }) == "delivered"


def test_highest_field_wins_not_oldest():
    """Whichever field is furthest in the pipeline wins, regardless of timestamps."""
    assert derive_status({
        "spm_po_sent_at": TS,
        "so_received_at": TS,
        "dispatched_at": TS,   # furthest
    }) == "dispatched"

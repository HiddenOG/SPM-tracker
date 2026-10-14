"""
test_classify_email.py — Unit tests for classify_email() in smart_gap_filler.py.

Each test passes a mock email dict (no IMAP, no DB) and checks what pipeline
fields are returned. These catch regressions in the email-matching rules.
"""
import pytest
from smart_gap_filler import classify_email

DATE = "2026-06-01T09:00:00+00:00"

WAREHOUSE = "spmwarehouse22@gmail.com"
SPM       = "specialpiping@gmail.com"
FLEX_SO   = "salesorder@flexitallic.eu"
FLEX_PENNY = "penny@flexitallic.eu"
UNICORN   = "unicornsl@freight.com"


def _em(frm, to_cc, subject, body, date=DATE):
    return {"from": frm, "to_cc": to_cc, "subject": subject, "body": body,
            "date_iso": date, "message_id": "<test@id>"}


# ── Warehouse → SPM ───────────────────────────────────────────────────────────

class TestWarehouseEmails:
    def test_delivery_request_subject(self):
        em = _em(WAREHOUSE, SPM, "Request for delivery - 0061448980", "Please arrange delivery.")
        r = classify_email(em)
        assert r.get("delivery_requested_at") == DATE
        assert "delivered_at" not in r
        assert "stock_check_completed_at" not in r

    def test_completely_delivered(self):
        em = _em(WAREHOUSE, SPM, "Re: 0061448980", "Items completely delivered to site.")
        r = classify_email(em)
        assert r.get("delivered_at") == DATE
        assert "stock_check_completed_at" not in r

    def test_waybill_for_triggers_delivered(self):
        em = _em(WAREHOUSE, SPM, "Re: 0061448980", "Please find waybill for this delivery.")
        r = classify_email(em)
        assert r.get("delivered_at") == DATE

    def test_waybill_item_triggers_delivered(self):
        em = _em(WAREHOUSE, SPM, "Re: 0061448980", "Waybill item 12345 attached.")
        r = classify_email(em)
        assert r.get("delivered_at") == DATE

    def test_bare_waybill_does_not_trigger_delivered(self):
        """'waybill' alone is too broad — should fall through to stock_check."""
        em = _em(WAREHOUSE, SPM, "Re: 0061448980", "We will send a waybill soon.")
        r = classify_email(em)
        assert "delivered_at" not in r
        assert r.get("stock_check_completed_at") == DATE

    def test_generic_reply_is_stock_check(self):
        em = _em(WAREHOUSE, SPM, "Re: 0061448980", "Stock is available, 10 units in hand.")
        r = classify_email(em)
        assert r.get("stock_check_completed_at") == DATE
        assert "delivered_at" not in r
        assert "delivery_requested_at" not in r


# ── SPM → Warehouse ───────────────────────────────────────────────────────────

class TestSPMToWarehouse:
    def test_with_so_number_gives_so_sent_to_warehouse(self):
        em = _em(SPM, WAREHOUSE, "Re: 0061448980", "Please see SO718602 attached.")
        r = classify_email(em)
        assert r.get("so_sent_to_warehouse_at") == DATE
        assert r.get("so_number") == "SO718602"
        assert "sent_to_warehouse_at" not in r

    def test_without_so_gives_sent_to_warehouse(self):
        em = _em(SPM, WAREHOUSE, "Re: 0061448980", "Please check stock for this order.")
        r = classify_email(em)
        assert r.get("sent_to_warehouse_at") == DATE
        assert "so_sent_to_warehouse_at" not in r


# ── SPM → Flexitallic ─────────────────────────────────────────────────────────

class TestSPMToFlexitallic:
    def test_purchase_order_email(self):
        em = _em(SPM, "purchasing@flexitallic.eu",
                 "Purchase Order (S.P.M.-C.N.L.-3072)",
                 "Please find the purchase order attached.")
        r = classify_email(em)
        assert r.get("spm_po_sent_at") == DATE
        assert r.get("spm_po_number") == "S.P.M.-C.N.L.-3072"

    def test_spm_po_number_is_normalized(self):
        """Spaces inside the PO ref must be stripped."""
        em = _em(SPM, "purchasing@flexitallic.eu",
                 "Purchase Order (S.P.M. - C.N.L. - 3072)",
                 "PO attached.")
        r = classify_email(em)
        assert " " not in (r.get("spm_po_number") or "")

    def test_non_po_to_flex_is_dispatch_instructions(self):
        em = _em(SPM, "penny@flexitallic.eu", "Re: SO718602", "Please ship to Unicorn warehouse.")
        r = classify_email(em)
        assert r.get("dispatch_instructions_sent_at") == DATE
        assert "spm_po_sent_at" not in r


# ── Flexitallic SO Acknowledgement ────────────────────────────────────────────

class TestFlexitallicSOAck:
    def test_so_ack_sets_so_received(self):
        em = _em(FLEX_SO, SPM, "Acknowledgement - SO718602", "Your order SO718602 is confirmed.")
        r = classify_email(em)
        assert r.get("so_received_at") == DATE
        assert r.get("so_number") == "SO718602"

    def test_so_ack_without_so_in_body(self):
        em = _em(FLEX_SO, SPM, "Acknowledgement", "Your order is confirmed.")
        r = classify_email(em)
        assert r.get("so_received_at") == DATE


# ── Flexitallic Penny (other Flexitallic emails) ──────────────────────────────

class TestFlexitallicPenny:
    def test_collection_arranged_gives_ready_for_dispatch(self):
        em = _em(FLEX_PENNY, SPM, "Re: SO718602",
                 "We have arranged collection with Unicorn transport.")
        r = classify_email(em)
        assert r.get("ready_for_dispatch_at") == DATE
        assert "flex_dispatch_ready_at" not in r

    def test_packed_and_ready_gives_flex_dispatch_ready(self):
        em = _em(FLEX_PENNY, SPM, "Re: SO718602",
                 "Your items are packed and ready, awaiting dispatch instructions.")
        r = classify_email(em)
        assert r.get("flex_dispatch_ready_at") == DATE
        assert "ready_for_dispatch_at" not in r

    def test_collection_and_packed_no_double_stamp(self):
        """An email mentioning both 'collect'+'unicorn' AND 'packed' must only
        set ready_for_dispatch_at, not both fields."""
        em = _em(FLEX_PENNY, SPM, "Re: SO718602",
                 "Items are packed and we have collected via Unicorn transport.")
        r = classify_email(em)
        assert r.get("ready_for_dispatch_at") == DATE
        assert "flex_dispatch_ready_at" not in r

    def test_so_number_extracted(self):
        em = _em(FLEX_PENNY, SPM, "Re: SO718602",
                 "Items are packed and ready for SO718602.")
        r = classify_email(em)
        assert r.get("so_number") == "SO718602"


# ── Unicorn / freight forwarder ───────────────────────────────────────────────

class TestUnicorn:
    def test_noted_gives_dispatched(self):
        em = _em(UNICORN, SPM, "Re: SO718602", "Noted, we will collect on Friday.")
        r = classify_email(em)
        assert r.get("dispatched_at") == DATE

    def test_so_number_also_extracted(self):
        em = _em(UNICORN, SPM, "Re: SO718602", "Confirmed, SO718602 noted.")
        r = classify_email(em)
        assert r.get("dispatched_at") == DATE
        assert r.get("so_number") == "SO718602"


# ── Edge cases ────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_unknown_sender_returns_empty(self):
        em = _em("random@company.com", SPM, "Hello", "Random email.")
        r = classify_email(em)
        assert r == {}

    def test_missing_date_returns_empty(self):
        em = _em(WAREHOUSE, SPM, "Re: 0061448980", "Stock available.", date="")
        r = classify_email(em)
        assert r == {}


# ── NLNG warehouse-routing subjects ──────────────────────────────────────────
# SPM forwards an NLNG PO to the warehouse under several wordings. The subject
# must be essentially just the PO reference: anything more descriptive is a
# different email on a different pipeline.

@pytest.mark.parametrize("subject,expected", [
    ("PO No. 4200083212",                    "4200083212"),
    ("Fwd: PO No. 4200093776",               "4200093776"),
    ("NEW PO 4200094401",                    "4200094401"),
    ("NEW PO No. 4200056792",                "4200056792"),
    ("Fwd: NEW PO No. 4200056792",           "4200056792"),
    ("Fwd: NLNG PO 4200055958",              "4200055958"),
    ("Re: PO No. 4200056268'",               "4200056268"),
    ("4200094401",                           "4200094401"),
])
def test_nlng_routing_subjects_recognised(subject, expected):
    from sync import is_nlng_po_subject
    assert is_nlng_po_subject(subject) == expected


@pytest.mark.parametrize("subject", [
    # SPM's own PO to the supplier - belongs to the supplier pipeline, and
    # treating it as warehouse routing would create a duplicate NLNG order.
    "PURCHASE ORDER-S.P.M. - 3123-NLNG-4200094522- FLEXITALLIC",
    "Fwd: PURCHASE ORDER (S.P.M.-N.L.N.G.-1016 -4200055958 - 4200056268 -FLEXITALLIC)",
    "Update on PO 4200094522",
    "Re: FW: COM FOR INCOMPLETE SET OF GASKETS, MESC: 3206000093, PO: 4200091355",
    "Re: Delivery Update/ OVERDUE POs 4200088203 & 4200088564",
    "Fwd: SO691552 / S.P.M.-2031-MOBIL/NLNG-4501887722, 4200089287",
])
def test_non_routing_subjects_rejected(subject):
    from sync import is_nlng_po_subject
    assert is_nlng_po_subject(subject) is None

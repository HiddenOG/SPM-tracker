"""
test_flexitallic_history.py - the rules that keep the Suppliers page honest.

Each test pins a mistake that either happened or nearly shipped:
  - a re-run of the history backfill overwriting recovered client POs
  - a revised acknowledgement moving an order into the wrong year
  - SPM's reused sequence numbers attaching another order's POs to a row
  - consecutive real PO numbers being thrown away as "typos"
"""
from datetime import datetime

from backfill_flexitallic_history import merge_row
from recover_client_pos import drop_typos, is_typo_of, plan_row


def _email(uid, day, pos, customer="NLNG"):
    pos = set(pos)
    return {"uid": uid, "ref": "2065", "date": datetime.fromisoformat(day),
            "subject": "PURCHASE ORDER", "customer": customer,
            "subject_pos": pos, "pdf_pos": set(), "pos": pos}


# ── typo rule ─────────────────────────────────────────────────────────────────

def test_dropped_digit_is_a_typo():
    assert is_typo_of("420003104", "4200083104")
    assert drop_typos({"420003104", "4200083104"}) == {"4200083104"}


def test_consecutive_po_numbers_are_both_real():
    # Buyers number POs in sequence. The first version of the rule discarded
    # these; three such "typos" turned out to exist elsewhere in our data.
    assert not is_typo_of("4200085125", "4200085124")
    assert drop_typos({"4200085124", "4200085125"}) == {"4200085124", "4200085125"}


def test_short_number_kept_when_no_longer_neighbour():
    assert drop_typos({"420076436"}) == {"420076436"}


# ── matching an SPM purchase-order email to a sales order ─────────────────────

ROW = {"so_date": "2025-07-10T09:00:00", "customer": "NLNG",
       "customer_po_numbers": ["4200083104", "4200083124"]}


def test_truncated_list_is_completed_from_the_matching_email():
    full = _email(1, "2025-07-04", ["4200083104", "4200083124", "4200083213", "4200083272"])
    plan = plan_row(ROW, [full])
    assert plan["why"] == "anchored"
    assert plan["new"] == {"4200083213", "4200083272"}


def test_reused_sequence_number_from_another_order_is_rejected():
    # Same ref, different order: none of the row's POs appear in it.
    stranger = _email(2, "2025-07-01", ["0061103559", "0061109151"], customer="CHEVRON")
    plan = plan_row(ROW, [stranger])
    assert plan["chosen"] is None
    assert plan["new"] == set()


def test_email_sent_well_after_the_acknowledgement_is_ignored():
    later = _email(3, "2025-08-30", ["4200083104", "4200083124", "4200099999"])
    assert plan_row(ROW, [later])["chosen"] is None


def test_latest_version_before_the_acknowledgement_wins():
    draft = _email(4, "2025-06-20", ["4200083104", "4200083124", "4200011111"])
    final = _email(5, "2025-07-04", ["4200083104", "4200083124", "4200083213"])
    plan = plan_row(ROW, [draft, final])
    assert plan["chosen"]["uid"] == 5
    assert plan["new"] == {"4200083213"}


def test_typo_on_the_email_is_held_back_not_added():
    email = _email(6, "2025-07-04", ["4200083104", "4200083124", "420003104"])
    plan = plan_row(ROW, [email])
    assert plan["new"] == set()
    assert plan["typos"] == [("420003104", "4200083104")]


def test_row_without_client_pos_is_not_applied_by_default():
    bare = {**ROW, "customer_po_numbers": []}
    only = _email(7, "2025-07-04", ["4200083213"])
    assert plan_row(bare, [only])["why"] == "unanchored-skipped"
    assert plan_row(bare, [only], include_unanchored=True)["new"] == {"4200083213"}


# ── merging an acknowledgement into an existing row ───────────────────────────

CURRENT = {
    "so_date": "2025-12-18T10:00:00", "source_message_id": "<ack-1>",
    "customer_po_numbers": ["4501846645", "4501834359", "4501861396"],
    "customer": "MOBIL", "spm_po_ref": None, "spm_po_reference": "S.P.M.-2046-MOBIL-4501846645",
    "order_value": 1000.0, "currency": "USD", "line_item_count": 3,
    "line_items": [], "source_subject": "ack",
}


def test_rerun_never_removes_recovered_client_pos():
    # The truncated acknowledgement only knows the first PO. The old plain
    # upsert replaced the list with it and wiped the recovered ones.
    again = {**CURRENT, "customer_po_numbers": ["4501846645"]}
    assert "customer_po_numbers" not in merge_row(CURRENT, again)

    extra = {**CURRENT, "customer_po_numbers": ["4501846645", "4501999999"]}
    assert merge_row(CURRENT, extra)["customer_po_numbers"] == sorted(
        CURRENT["customer_po_numbers"] + ["4501999999"])


def test_same_acknowledgement_changes_nothing():
    assert merge_row(CURRENT, dict(CURRENT)) == {}


def test_revised_acknowledgement_updates_value_but_keeps_order_date():
    revised = {**CURRENT, "source_message_id": "<ack-2>", "so_date": "2026-01-06T09:00:00",
               "order_value": 1250.0}
    update = merge_row(CURRENT, revised)
    assert update["order_value"] == 1250.0
    assert update["source_message_id"] == "<ack-2>"
    assert "so_date" not in update          # December order stays in December


def test_older_copy_of_an_acknowledgement_does_not_roll_the_value_back():
    older = {**CURRENT, "source_message_id": "<ack-0>", "so_date": "2025-12-01T09:00:00",
             "order_value": 900.0}
    assert "order_value" not in merge_row(CURRENT, older)


def test_existing_customer_is_not_overwritten_but_blanks_are_filled():
    incoming = {**CURRENT, "customer": "CHEVRON", "spm_po_ref": "2046"}
    update = merge_row(CURRENT, incoming)
    assert "customer" not in update
    assert update["spm_po_ref"] == "2046"

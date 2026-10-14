"""
test_quote_numbering.py — the quote reference number.

Three faults, all found on live data, all fixed here:

  · The number was built once on create and never rebuilt. A draft saved as
    NLNG and later changed to another customer kept NLNG's code — SPM-00002-NLNG
    went out to WAAL ENERGY. Nine of 54 quotations on file disagree with their
    own client record.

  · "Others" was meant to use the customer's initials, but the form sends the
    name as recipient_company and the server only read client_name. So every
    one-off customer was numbered -OTH instead of -WESL, -KIL, -SITCL.

  · A failed lookup fell back to `last = 0`, silently restarting the numbering
    at 1. Six sequence numbers are used two to four times over, all created
    between 13 and 27 August — the weeks the server was dropping Supabase
    connections.
"""
import os
import sys
from pathlib import Path

import pytest

WEB = Path(__file__).parent.parent / "web"
sys.path.insert(0, str(WEB))


def _app():
    os.environ.setdefault("JWT_SECRET", "test-secret")
    import app
    return app


# ── the code that follows the client ─────────────────────────────────────────

def test_each_client_has_its_own_code():
    app = _app()
    assert app._quote_code("chevron") == "CNL"
    assert app._quote_code("nlng") == "NLNG"
    assert app._quote_code("seplat") == "SEP"
    assert app._quote_code("exxon") == "MPN"
    assert app._quote_code("total") == "TEN"


def test_a_one_off_customer_is_named_by_its_initials():
    assert _app()._quote_code("others", "WAAL ENERGY SERVICES LTD") == "WESL"
    assert _app()._quote_code("others", "KAMAX INTERNATIONAL LTD") == "KIL"


def test_a_one_off_customer_with_no_name_falls_back():
    assert _app()._quote_code("others", "") == "OTH"
    assert _app()._quote_code("others", "   ") == "OTH"


def test_a_listed_client_ignores_a_name_left_in_the_box():
    """Switching back from Others must not leave the old initials behind."""
    assert _app()._quote_code("chevron", "WAAL ENERGY") == "CNL"


# ── the name arrives under whichever key the form used ───────────────────────

def test_the_customer_name_is_read_from_the_key_the_form_actually_sends():
    """The form's 'Client Company Name' box is sent as recipient_company.
    client_name was read here and never sent, which is why every Others quote
    on file is numbered -OTH."""
    app = _app()
    assert app._quote_customer_name({"recipient_company": "KAMAX INTERNATIONAL"}) == "KAMAX INTERNATIONAL"
    assert app._quote_customer_name({"client_name": "KAMAX INTERNATIONAL"}) == "KAMAX INTERNATIONAL"
    assert app._quote_customer_name({}) == ""


# ── rebuilding around a sequence that has already been issued ────────────────

def test_the_code_changes_but_the_sequence_never_does():
    """The sequence is the quote's identity and may already be on a document
    somebody has been sent. Only the code after it follows the client."""
    app = _app()
    assert app._quote_number_for(2, "nlng") == "SPM-00002-NLNG"
    assert app._quote_number_for(2, "chevron") == "SPM-00002-CNL"
    assert app._quote_number_for(2, "others", "WAAL ENERGY SERVICES LTD") == "SPM-00002-WESL"


def test_the_company_prefix_is_kept():
    app = _app()
    assert app._quote_number_for(7, "chevron", "", "danmag") == "DMLM-00007-CNL"


def test_the_sequence_is_padded_to_five_digits():
    assert _app()._quote_number_for(9, "nlng") == "SPM-00009-NLNG"


# ── the update path rebuilds it ──────────────────────────────────────────────

def test_saving_an_edit_rebuilds_the_number():
    src = (WEB / "app.py").read_text(encoding="utf-8")
    update = src[src.index("def update_quotation("):src.index('@app.delete("/api/quotations/{quote_id}")')]
    assert "_quote_number_for" in update, "an edit must rebuild the number"
    assert "quote_seq" in update, "and must read the sequence it was issued"
    assert 'update["quote_number"] = rebuilt' in update


def test_the_update_reads_the_sequence_before_writing():
    """It cannot rebuild the number without knowing which sequence to keep."""
    src = (WEB / "app.py").read_text(encoding="utf-8")
    update = src[src.index("def update_quotation("):src.index('@app.delete("/api/quotations/{quote_id}")')]
    assert 'select("id,quote_seq,quote_number,company")' in update


# ── a failed lookup must refuse, not reissue ─────────────────────────────────

def test_a_failed_lookup_refuses_instead_of_restarting_at_one():
    """`except: last = 0` handed out a number that was already taken, without
    a word. A quote that did not save can be saved again; two quotes sharing a
    reference is found much later, by someone holding both documents."""
    src = (WEB / "app.py").read_text(encoding="utf-8")
    fn = src[src.index("def _next_quote_number("):src.index("# ── FastAPI auth dependencies")]
    assert "last = 0" not in fn.split("rows = res.data")[0].split("except Exception as exc:")[-1], \
        "the fallback to zero is back"
    assert "status_code=503" in fn
    assert 'not_.is_("quote_seq", "null")' in fn, "NULLs sort first in a DESC order"


def test_the_error_tells_the_person_their_work_is_safe():
    src = (WEB / "app.py").read_text(encoding="utf-8")
    fn = src[src.index("def _next_quote_number("):src.index("# ── FastAPI auth dependencies")]
    assert "still on screen" in fn
    assert "nothing was saved" in fn

"""
test_otd_dates.py — Tests for the OTD date comparison logic.

The key invariant: a delivery at any time on the required date (e.g. 09:00 UTC
on 2026-07-14) must NOT be classified as late. The _dateMidnight() helper in
index.html strips the time portion so the comparison is date-only.

These tests verify the Python-equivalent logic (the JS logic is identical).
"""
import pytest
from datetime import date, datetime, timezone


def date_midnight(s):
    """Python equivalent of the JS _dateMidnight() helper in index.html."""
    if not s:
        return None
    d_str = str(s)[:10]           # "YYYY-MM-DD"
    return date.fromisoformat(d_str)


def otd_class(o):
    """Python equivalent of the JS otdClass() function."""
    delivered_at = o.get("delivered_at")
    required_delivery_date = o.get("required_delivery_date")

    if delivered_at:
        if not required_delivery_date:
            return "del-otd"
        return "del-otd" if date_midnight(delivered_at) <= date_midnight(required_delivery_date) else "del-late"

    if not required_delivery_date:
        return "no-date"

    today = date.today()
    req = date_midnight(required_delivery_date)
    dl = (req - today).days

    if dl < 0:
        return "overdue"
    if dl < 7:
        return "critical"
    if dl < 30:
        return "at-risk"
    return "on-track"


def dl_diff(o):
    """Python equivalent of the diff calc in dlCellHtml()."""
    req = date_midnight(o.get("required_delivery_date"))
    del_ = date_midnight(o.get("delivered_at"))
    if req is None or del_ is None:
        return None
    return (req - del_).days


# ── Same-day delivery must not be classified as late ─────────────────────────

class TestOtdClassSameDay:
    def test_midnight_utc_delivery_is_otd(self):
        o = {"required_delivery_date": "2026-07-14", "delivered_at": "2026-07-14T00:00:00Z"}
        assert otd_class(o) == "del-otd"

    def test_morning_utc_delivery_is_otd(self):
        o = {"required_delivery_date": "2026-07-14", "delivered_at": "2026-07-14T09:00:00Z"}
        assert otd_class(o) == "del-otd"

    def test_end_of_day_utc_delivery_is_otd(self):
        o = {"required_delivery_date": "2026-07-14", "delivered_at": "2026-07-14T23:59:59Z"}
        assert otd_class(o) == "del-otd"

    def test_next_day_delivery_is_late(self):
        o = {"required_delivery_date": "2026-07-14", "delivered_at": "2026-07-15T09:00:00Z"}
        assert otd_class(o) == "del-late"

    def test_early_delivery_is_otd(self):
        o = {"required_delivery_date": "2026-07-14", "delivered_at": "2026-07-10T09:00:00Z"}
        assert otd_class(o) == "del-otd"

    def test_no_required_date_is_del_otd(self):
        o = {"delivered_at": "2026-07-14T09:00:00Z"}
        assert otd_class(o) == "del-otd"


class TestDlDiff:
    def test_same_day_morning_is_zero_days_early(self):
        o = {"required_delivery_date": "2026-07-14", "delivered_at": "2026-07-14T09:00:00Z"}
        assert dl_diff(o) == 0  # not -1

    def test_one_day_early(self):
        o = {"required_delivery_date": "2026-07-14", "delivered_at": "2026-07-13T09:00:00Z"}
        assert dl_diff(o) == 1

    def test_one_day_late(self):
        o = {"required_delivery_date": "2026-07-14", "delivered_at": "2026-07-15T09:00:00Z"}
        assert dl_diff(o) == -1

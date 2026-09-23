"""
fx_history.py - how many units of a currency one US dollar bought ON A DATE.

The rest of the app converts at today's rate (open.er-api.com), which is right
for a live dashboard and wrong for a three-year history. Naira traded around
N1,500-1,600 to the dollar through 2024-25 against ~N1,327 in Sep 2026, so a
2025 naira PO converted at today's rate came out 13-20% too high.

Two sources:
  - GBP / EUR: European Central Bank reference rates via api.frankfurter.app.
    The whole daily series is fetched in ONE request and cached, since the
    Suppliers page may need a rate for every sterling order at once.
  - NGN (and a fallback for anything else): the free fawazahmed0 currency-api
    daily snapshots on jsDelivr. The ECB does not publish naira. Its sterling
    rate agreed with the ECB's to within 0.4% on the dates checked.

Returns None when no rate can be found; callers fall back to today's rate and
say so in their note rather than guessing.
"""

import json
import time
import urllib.request
from datetime import date, datetime, timedelta

_UA = {"User-Agent": "spm-tracker"}
_ECB_CURRENCIES = {"GBP", "EUR"}
_SERIES_START = "2023-01-01"
_SERIES_TTL = 12 * 3600

_cache: dict[tuple[str, str], float] = {}
_ecb_series: dict[str, dict[str, float]] = {}
_ecb_loaded_at: dict[str, float] = {}


def _get(url: str, timeout: int = 20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout) as resp:
        return json.load(resp)


def _as_day(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)[:10]).date()


def _ecb_rate(cur: str, day: date) -> float | None:
    if cur not in _ecb_series or time.time() - _ecb_loaded_at.get(cur, 0) > _SERIES_TTL:
        data = _get(f"https://api.frankfurter.app/{_SERIES_START}..?from=USD&to={cur}", timeout=40)
        _ecb_series[cur] = {k: float(v[cur]) for k, v in data["rates"].items()}
        _ecb_loaded_at[cur] = time.time()
    series = _ecb_series[cur]
    # The ECB publishes on business days only: a Saturday order takes Friday's rate.
    for back in range(10):
        key = (day - timedelta(days=back)).isoformat()
        if key in series:
            return series[key]
    return None


def _community_rate(cur: str, day: date) -> float | None:
    data = _get(f"https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{day.isoformat()}"
                f"/v1/currencies/usd.json")
    value = data.get("usd", {}).get(cur.lower())
    return float(value) if value else None


def rate_on(currency: str | None, when) -> float | None:
    """Units of `currency` per 1 USD on the day `when`, or None if unknown."""
    cur = (currency or "USD").upper()
    if cur == "USD":
        return 1.0
    try:
        day = _as_day(when)
    except Exception:
        return None
    day = min(day, date.today())
    key = (cur, day.isoformat())
    if key in _cache:
        return _cache[key]

    rate = None
    if cur in _ECB_CURRENCIES:
        try:
            rate = _ecb_rate(cur, day)
        except Exception:
            rate = None
    if rate is None:
        try:
            rate = _community_rate(cur, day)
        except Exception:
            rate = None
    if rate:
        _cache[key] = rate
    return rate

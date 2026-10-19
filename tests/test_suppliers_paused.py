"""The Suppliers page and its listener are paused while Gmail throttles the
mailbox's IMAP access (Oct 2026). These tests keep it that way until someone
switches it back on deliberately."""
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import flexitallic_history_listener as flex  # noqa: E402


def test_the_listener_is_paused_by_default(monkeypatch):
    monkeypatch.delenv("FLEX_HISTORY_ENABLED", raising=False)
    mod = importlib.reload(flex)
    try:
        assert mod.ENABLED is False
    finally:
        importlib.reload(flex)


def test_it_can_be_switched_back_on(monkeypatch):
    monkeypatch.setenv("FLEX_HISTORY_ENABLED", "1")
    mod = importlib.reload(flex)
    try:
        assert mod.ENABLED is True
    finally:
        monkeypatch.delenv("FLEX_HISTORY_ENABLED")
        importlib.reload(flex)


def test_paused_it_idles_and_never_touches_gmail(monkeypatch):
    """worker.py restarts a listener that returns, so it must idle - and must
    not run a check, the 14-day re-read or the full sweep on the way."""
    calls, touched = [], []

    def fake_sleep(secs):
        calls.append(secs)
        if len(calls) >= 3:
            raise SystemExit

    def gmail(*_a, **_k):
        # Recorded, not just raised: the listener's own retry loop catches errors.
        touched.append(True)
        raise AssertionError("touched Gmail while paused")

    monkeypatch.setattr(flex, "ENABLED", False)
    monkeypatch.setattr(flex.time, "sleep", fake_sleep)
    for name in ("check_once", "reconcile", "audit", "_reconnect"):
        monkeypatch.setattr(flex, name, gmail)
    with pytest.raises(SystemExit):
        flex.run_forever()
    assert not touched
    assert len(calls) == 3 and all(s >= 60 for s in calls)


def test_the_suppliers_page_is_hidden_in_both_apps():
    old = (ROOT / "web" / "script.js").read_text(encoding="utf-8")
    start = old.index("var ROLE_PAGES = {")
    roles = old[start:old.index("};", start)]
    assert "'suppliers'" not in roles
    pages = ROOT / "frontend" / "src" / "lib" / "pages.ts"
    if not pages.exists():      # the new app is not in every checkout yet
        return
    new = pages.read_text(encoding="utf-8")
    start = new.index("const ROLE_PAGES")
    assert "'suppliers'" not in new[start:new.index("}", start)]

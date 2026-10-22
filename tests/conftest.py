"""
conftest.py — adds scripts/ to sys.path so test files can import pipeline modules.
Also mocks db.get_client so no live Supabase connection is needed to run tests.
"""
import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make scripts/ importable from any test file
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

# Block real Supabase connections before any script module is imported
import unittest.mock as _mock

_fake_db = _mock.MagicMock()
_patcher = _mock.patch.dict("sys.modules", {"supabase": _mock.MagicMock()})
_patcher.start()

# Ensure SUPABASE_URL/KEY are set so db.py doesn't raise on import
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "fake-key")
os.environ.setdefault("GMAIL_EMAIL", "fake@gmail.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "fake-password")


import pytest


@pytest.fixture(autouse=True)
def _no_real_gmail_api(monkeypatch):
    """Tests never reach the real Gmail API. Once scripts/gmail_auth.py has
    written its token to .env, anything that loads .env would otherwise send
    mail searches to Google instead of to the test's own fakes. A test that
    wants the API route sets these itself (see test_gmail_api.py)."""
    for k in ("GMAIL_OAUTH_CLIENT_ID", "GMAIL_OAUTH_CLIENT_SECRET", "GMAIL_OAUTH_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)

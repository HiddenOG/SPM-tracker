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

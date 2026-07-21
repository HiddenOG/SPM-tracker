"""
db.py — Shared Supabase connection used across all scripts.

Every other script imports `get_client()` from here instead of
creating its own connection. Keeps credentials in one place.
"""

import os
import re
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

_client: Client | None = None


def get_client() -> Client:
    """Return a cached Supabase client, creating it on first use."""
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_KEY"]
        _client = create_client(url, key)
    return _client


def reset_client() -> None:
    """Drop the cached client so the next get_client() call creates a fresh one.
    Call this after a network error so a recovered connection isn't blocked by
    a stale socket.
    """
    global _client
    _client = None


# A Chevron PO number is 8–12 digits, optionally followed by a revision
# suffix like "-001" (a change order). The suffix may render with spaces
# around the dash in PDFs ("0060792432 - 001"), so we tolerate whitespace.
PO_NUMBER_RE = re.compile(r"(\d{8,12})\s*(?:-\s*(\d{1,3}))?")


def normalize_po_number(raw: str | None) -> str | None:
    if not raw:
        return None
    m = PO_NUMBER_RE.search(raw)
    if not m:
        return None
    base, rev = m.group(1), m.group(2)
    return f"{base}-{rev.zfill(3)}" if rev else base

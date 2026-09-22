"""
db.py — Shared Supabase connection used across all scripts.

Every other script imports `get_client()` from here instead of
creating its own connection. Keeps credentials in one place.
"""

import os
import re
import time
import threading

import httpx
from dotenv import load_dotenv
from postgrest import SyncPostgrestClient
from supabase import create_client, Client

try:
    from postgrest.utils import SyncClient as _PostgrestSession
except ImportError:          # library layout changed - a plain client still works
    _PostgrestSession = httpx.Client

load_dotenv()

_client: Client | None = None
_client_lock = threading.Lock()

# Supabase's edge closes pooled connections after a couple of requests, sending
# a graceful GOAWAY (error_code 0).  Over HTTP/2 httpcore cannot see that: its
# has_expired() only checks a keep-alive timer, so the dead connection stays in
# the pool and the next caller handed it dies with
#   <ConnectionTerminated error_code:0, last_stream_id:3>
# or "Server disconnected".  httpx never retries, so that surfaced as a 500 on
# /api/orders and as a failed pass in every worker listener.
#
# Over HTTP/1.1 the same check tests whether an idle socket has become readable,
# which is exactly the server-initiated close, and the pool discards it.  There
# is also no shared HPACK table, which is what the rarer PROTOCOL_ERROR and
# COMPRESSION_ERROR closes were: sync endpoints run in a threadpool and share
# one client.  So we force HTTP/1.1 and retire sockets well before the server
# does.
_LIMITS = httpx.Limits(
    max_connections=20,
    max_keepalive_connections=10,
    keepalive_expiry=20.0,
)

# The window between "server closed it" and "we noticed" is small but never
# zero, so reads get one more line of defence.
_TRANSIENT = (
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.ConnectTimeout,
)


class _ResilientTransport(httpx.HTTPTransport):
    """Retry a dropped connection, but only for requests that are safe to repeat.

    These failures mean no response came back at all.  For a GET that is always
    safe to send again.  For a POST or PATCH we cannot tell whether the write
    landed, so those are raised to the caller rather than risking a duplicate.
    """

    _SAFE = {"GET", "HEAD", "OPTIONS"}
    _ATTEMPTS = 3

    def handle_request(self, request):
        for attempt in range(self._ATTEMPTS):
            try:
                return super().handle_request(request)
            except _TRANSIENT:
                last = attempt == self._ATTEMPTS - 1
                if last or request.method not in self._SAFE:
                    raise
                time.sleep(0.2 * (2 ** attempt))


def _hardened_session(base_url, headers, timeout, verify=True, proxy=None):
    return _PostgrestSession(
        base_url=base_url,
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
        transport=_ResilientTransport(
            verify=verify,
            proxy=proxy,
            http1=True,
            http2=False,
            limits=_LIMITS,
            retries=1,
        ),
    )


# Patched on the class, not on one instance: supabase rebuilds its PostgREST
# client whenever an auth event fires, and a rebuild must not quietly restore
# the HTTP/2 session this exists to remove.
SyncPostgrestClient.create_session = staticmethod(_hardened_session)


def get_client() -> Client:
    """Return a cached Supabase client, creating it on first use."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:      # another thread may have won the race
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


def base_po_number(raw: str | None) -> str | None:
    """Return just the base PO number, stripping any -NNN change-order suffix."""
    if not raw:
        return None
    m = PO_NUMBER_RE.search(raw)
    return m.group(1) if m else None

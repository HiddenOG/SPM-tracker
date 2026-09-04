"""
worker.py — Runs all email parsers simultaneously as background threads.

Railway worker service: python scripts/worker.py

Each parser runs in its own thread with automatic restart on crash.
Staggered starts avoid hammering Gmail with simultaneous IMAP logins.
"""
import os
import sys
import time
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

from gmail_ack_listener    import run_forever as _run_gmail_ack
from warehouse_reply_parser import run_forever as _run_warehouse
from supplier_po_parser     import run_forever as _run_supplier
from imap_listener          import run_forever as _run_imap
from pdf_extractor          import run_forever as _run_pdf
from ack_pdf_extractor      import run_forever as _run_ack_pdf

PARSERS = [
    ("gmail_ack_listener",     _run_gmail_ack),
    ("warehouse_reply_parser", _run_warehouse),
    ("supplier_po_parser",     _run_supplier),
    ("imap_listener",          _run_imap),
    ("pdf_extractor",          _run_pdf),
    ("ack_pdf_extractor",      _run_ack_pdf),
]

RESTART_DELAY = 30  # seconds before restarting a crashed parser


def _run_with_restart(name: str, fn) -> None:
    """Run a parser forever, restarting automatically on any exception."""
    while True:
        try:
            print(f"[worker] ▶  {name} starting", flush=True)
            fn()
        except KeyboardInterrupt:
            print(f"[worker] ⏹  {name} stopped", flush=True)
            break
        except Exception as exc:
            print(f"[worker] ❌  {name} crashed: {exc!r}", flush=True)
            print(f"[worker] ⏳  restarting {name} in {RESTART_DELAY}s…", flush=True)
            time.sleep(RESTART_DELAY)


def main() -> None:
    print("=" * 50, flush=True)
    print("  SPM Tracker — Worker", flush=True)
    print(f"  Starting {len(PARSERS)} parsers (email + PDF)", flush=True)
    print("=" * 50, flush=True)

    threads = []
    for name, fn in PARSERS:
        t = threading.Thread(
            target=_run_with_restart,
            args=(name, fn),
            name=name,
            daemon=True,
        )
        t.start()
        threads.append(t)
        time.sleep(3)  # stagger IMAP logins to avoid rate limits

    # Keep main thread alive — if it exits, all daemon threads die
    try:
        while True:
            # Log which parsers are still alive every 5 minutes
            alive = [t.name for t in threads if t.is_alive()]
            dead  = [t.name for t in threads if not t.is_alive()]
            if dead:
                print(f"[worker] ⚠️  dead threads: {dead}", flush=True)
            time.sleep(300)
    except KeyboardInterrupt:
        print("\n[worker] Stopped.", flush=True)


if __name__ == "__main__":
    main()

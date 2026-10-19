"""
email_thread_sync.py — keep each order's Gmail thread in the emails table.

Why this exists
---------------
The PO story is written from an order's whole Gmail conversation. Until now it
fetched that conversation live on every telling: an IMAP login, a thread search,
and a full RFC822 fetch of every message. On a 145-email thread that is slow
enough to need a "Searching Gmail thread, please wait…" notice.

The listeners do write to the emails table, but only for mail they recognise -
an acknowledgement, a PO, a warehouse reply - because that is all they look
for. Measured against a live search on three real orders:

    PO 0061340563    2 of 5        PO 0061367679   33 of 126
    PO 0061365256   16 of 145

A tenth to a quarter of each thread. Too little to write a story from, and too
little for "has new mail arrived?" to be a reliable question.

So this module walks the whole thread and stores it. The story then reads from
the table, and the table is kept current here rather than at the moment someone
opens a drawer.

Three ways in, one implementation
---------------------------------
    run_forever()          the worker thread, least-recently-synced first
    main()                 the same thing from the command line, for a backfill
    sync_thread(...)       one order, called by the web app when it finds the
                           table empty for an order nobody has synced yet

That last one is what makes this safe to deploy before the backfill has run:
the app reads the table, and falls back to syncing on the spot when a thread is
missing. No order is ever left without a story.

Usage
-----
    python scripts/email_thread_sync.py                # backfill everything
    python scripts/email_thread_sync.py --limit 20     # the 20 most overdue
    python scripts/email_thread_sync.py --order <id>   # one order
    python scripts/email_thread_sync.py --dry-run      # say what it would do
"""
from __future__ import annotations

import argparse
import email as _eml
import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
load_dotenv(os.path.join(ROOT, ".env"))

import sync
from db import get_client
from email_utils import decode_mime_words, get_email_body_text

# A thread is re-walked at most this often. The listeners still stamp the
# pipeline milestones as they always did, so this governs how quickly ordinary
# conversation shows up in a story, not how quickly an order moves.
SYNC_INTERVAL_HOURS = int(os.environ.get("THREAD_SYNC_INTERVAL_HOURS", "6"))

# Orders per pass. Deliberately small: a pass holds one IMAP connection open
# and a long thread is a few hundred full message fetches, so this trades
# coverage speed for staying a well-behaved Gmail client.
BATCH_SIZE = int(os.environ.get("THREAD_SYNC_BATCH", "8"))

CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "600"))

# Subjects that are a digest of many orders rather than correspondence about
# one. Same exclusion the live fetch has always applied.
_SKIP_SUBJECTS = ("SPM CNL Purchase Orders",)

# An order that has been delivered or cancelled is finished, and its thread is
# not going to grow. Those still get their first walk - somebody will look back
# at a delivered order and want its story - but not a re-walk every few hours
# for the rest of the system's life. Without this, most of the Gmail traffic
# would eventually be spent re-reading threads that cannot change.
_TERMINAL_STATUSES = {"delivered", "cancelled"}

# Gmail logins to this mailbox were measured at 27.9 seconds, against what was
# a 30 second timeout. That is close enough that an ordinary login was
# occasionally timing out, and the timeout surfaced as "this PO has no mail".
IMAP_TIMEOUT = int(os.environ.get("THREAD_SYNC_IMAP_TIMEOUT", "90"))


class ThreadSyncFailed(Exception):
    """Gmail could not be asked - as distinct from asked and having no mail.

    The difference decides whether the order is stamped as walked. Stamping a
    failure means not trying again for six hours, and a thread that was never
    read looking exactly like a thread with nothing in it.
    """


# ── Gmail ────────────────────────────────────────────────────────────────────

def open_imap():
    """A logged-in IMAP client, or None when Gmail is not configured.

    Each caller gets its own connection rather than sharing a module global.
    The web app can afford to reuse one because its requests are serialised
    through a single worker; this runs on its own thread beside six other
    listeners, and an IMAPClient is not safe to share between them.
    """
    from imapclient import IMAPClient

    addr = os.environ.get("GMAIL_EMAIL", "")
    pw = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not addr or not pw:
        return None
    imap = IMAPClient("imap.gmail.com", port=993, use_uid=True, ssl=True,
                      timeout=IMAP_TIMEOUT)
    imap.login(addr, pw)
    imap.select_folder("[Gmail]/All Mail", readonly=True)
    return imap


def sent_at(msg) -> str | None:
    """When a message says it was sent, or None. Never a guess.

    Deliberately not sync.parse_email_date, which falls back to now() when the
    Date header is missing or unparseable. That fallback is right where it is
    used - stamping a pipeline milestone needs a time, and the time the mail
    was handled is a fair answer - but it is wrong for an archive, in two ways
    that compound:

      The story is told in date order and the prompt says the LAST email is the
      most recent. A dateless message stamped now() sorts to the end, so it is
      read as the latest development. Backfilling the 222 undated rows already
      on file that way would have the story reporting months-old mail as what
      just happened.

      Worse, it defeats the story cache outright. The cache is invalidated when
      mail on file is newer than the story, and upsert_email keys on
      message_id - so every sync rewrites that message's date to a fresh now(),
      which is always newer than the story, so the story is regenerated on
      every pass. One dateless email in a thread and that order is paying for a
      Gmail search and a model call on every single open: exactly the bill this
      was built to remove.

    None instead, which leaves the column NULL, sorts the message first (an
    unknown date is treated as old, not new), and is skipped by the staleness
    check.
    """
    header = msg.get("Date")
    if not header:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(header).isoformat()
    except Exception:
        return None


def _or_tree(items: list):
    if len(items) == 1:
        return items[0]
    return ["OR", items[0], _or_tree(items[1:])]


def fetch_thread(po_number: str, so_number: str = "", imap=None) -> list[dict]:
    """Every message in the Gmail thread(s) that mention this PO or SO.

    Two searches, not one. The first finds the messages that name the number;
    the second widens to every message sharing their X-GM-THRID, which is how a
    reply that quotes nothing and names nothing still gets picked up.

    A leading-zero variant is searched too: Chevron PO numbers are written both
    ways, and Gmail treats "0061365256" and "61365256" as different terms.
    """
    own = imap is None
    if own:
        imap = open_imap()
        if imap is None:
            raise ThreadSyncFailed("Gmail is not configured "
                                   "(GMAIL_EMAIL / GMAIL_APP_PASSWORD)")
    try:
        stripped = po_number.lstrip("0")
        terms = [f'"{po_number}"']
        if stripped and stripped != po_number:
            terms.append(f'"{stripped}"')
        if so_number:
            terms.append(f'"{so_number}"')

        # A failed search is raised, not swallowed. It used to return an empty
        # list, which sync_thread then recorded as a successful walk that found
        # nothing - stamping the order as done and not trying again for six
        # hours. A live run showed exactly that: a Chevron PO came back with 0
        # emails after 416 seconds, was stamped email_count=0, and the three
        # messages the listeners had already found were the only ones that
        # would ever be there. "No mail" and "could not look" have to be told
        # apart, because only one of them should stop us retrying.
        try:
            seed = set(imap.search(["X-GM-RAW", " OR ".join(terms)]))
        except Exception as exc:
            raise ThreadSyncFailed(f"search failed for {po_number}: {exc}") from exc
        if not seed:
            return []

        thread_data = imap.fetch(list(seed), ["X-GM-THRID"])
        thread_ids = list({v[b"X-GM-THRID"] for v in thread_data.values() if b"X-GM-THRID" in v})

        uids: set[int] = set(seed)
        if thread_ids:
            criteria = [["X-GM-THRID", str(t)] for t in thread_ids]
            try:
                uids.update(imap.search(_or_tree(criteria)))
            except Exception:
                # One oversized OR tree can be rejected where the individual
                # searches are fine, so fall back to asking one at a time.
                for t in thread_ids:
                    try:
                        uids.update(imap.search(["X-GM-THRID", str(t)]))
                    except Exception:
                        pass

        try:
            fetched = imap.fetch(list(uids), ["RFC822"])
        except Exception as exc:
            # Same reasoning as the search above: a thread we could not read is
            # not a thread with nothing in it, and must not be stamped as done.
            raise ThreadSyncFailed(f"fetch failed for {po_number}: {exc}") from exc

        spm_sender = os.environ.get("SPM_SENDER", "specialpiping@gmail.com")
        seen: set[str] = set()
        out: list[dict] = []
        for uid in sorted(uids):
            try:
                raw = (fetched.get(uid) or {}).get(b"RFC822")
                if not raw:
                    continue
                msg = _eml.message_from_bytes(raw)
                subject = decode_mime_words(msg.get("Subject", ""))
                if any(s in subject for s in _SKIP_SUBJECTS):
                    continue
                mid = (msg.get("Message-ID") or "").strip()
                if not mid or mid in seen:
                    # No Message-ID means nothing to key an upsert on, so the
                    # row would be written again on every pass.
                    continue
                seen.add(mid)
                sender = decode_mime_words(msg.get("From", ""))
                out.append({
                    "message_id":   mid,
                    "direction":    "out" if spm_sender.lower() in sender.lower() else "in",
                    "from_address": sender,
                    "to_address":   decode_mime_words(msg.get("To", "")),
                    "subject":      subject,
                    "body_text":    get_email_body_text(msg)[:50_000],
                    "received_at":  sent_at(msg),
                    "in_reply_to":  (msg.get("In-Reply-To") or "").strip() or None,
                })
            except Exception:
                continue

        # Undated mail sorts first, matching what the live fetch has always
        # done. The story prompt tells the model the LAST email is the most
        # recent, so an undated one must not land at the end and be read as
        # the latest development.
        out.sort(key=lambda e: e.get("received_at") or "")
        return out
    finally:
        if own:
            try:
                imap.logout()
            except Exception:
                pass


# ── the table ────────────────────────────────────────────────────────────────

def latest_gmail_date(po_number: str, so_number: str = "", imap=None) -> str | None:
    """When the newest message in this order's Gmail thread arrived, or None.

    The cheap question. fetch_thread pulls every message in full - 80 seconds
    for a 41-message thread, over 7 minutes for 145 - because a story has to be
    written from the words. This only asks the server for dates, so it is a
    search plus a few bytes per message: about 3 seconds on a warm connection.

    It exists because nothing else can answer "has new mail arrived?" honestly.
    The emails table only holds what a listener recognised - measured at a
    tenth to a quarter of a thread - so a reply nobody parsed leaves the table
    looking unchanged, and a saved story would keep being served while the
    email sat unread in Gmail.

    Returns None when Gmail cannot be reached or the thread is not found, which
    callers must read as "do not know" rather than "nothing new".
    """
    own = imap is None
    if own:
        try:
            imap = open_imap()
        except Exception as exc:
            print(f"  ⚠️  latest_gmail_date login: {exc}")
            return None
        if imap is None:
            return None
    try:
        stripped = po_number.lstrip("0")
        terms = [f'"{po_number}"']
        if stripped and stripped != po_number:
            terms.append(f'"{stripped}"')
        if so_number:
            terms.append(f'"{so_number}"')
        try:
            seed = set(imap.search(["X-GM-RAW", " OR ".join(terms)]))
        except Exception as exc:
            print(f"  ⚠️  latest_gmail_date search {po_number}: {exc}")
            return None
        if not seed:
            return None

        # Widen to the whole thread, the same as fetch_thread, so a reply that
        # names nothing is still counted. Without this, the newest message in a
        # thread could be missed precisely because it quoted no PO number.
        uids = set(seed)
        try:
            thread_data = imap.fetch(list(seed), ["X-GM-THRID"])
            tids = list({v[b"X-GM-THRID"] for v in thread_data.values()
                         if b"X-GM-THRID" in v})
            if tids:
                try:
                    uids.update(imap.search(_or_tree(
                        [["X-GM-THRID", str(t)] for t in tids])))
                except Exception:
                    for t in tids:
                        try:
                            uids.update(imap.search(["X-GM-THRID", str(t)]))
                        except Exception:
                            pass
        except Exception:
            pass          # the seed alone is a usable answer

        try:
            # INTERNALDATE only - no RFC822, which is what makes this cheap.
            dated = imap.fetch(list(uids), ["INTERNALDATE"])
        except Exception as exc:
            print(f"  ⚠️  latest_gmail_date dates {po_number}: {exc}")
            return None
        newest = None
        for v in dated.values():
            when = v.get(b"INTERNALDATE")
            if when is None:
                continue
            if newest is None or when > newest:
                newest = when
        if newest is None:
            return None
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        return newest.isoformat()
    finally:
        if own:
            try:
                imap.logout()
            except Exception:
                pass


def store_thread(order_id: str, order_type: str, emails: list[dict]) -> int:
    """Write these emails against this order. Returns how many were written."""
    written = 0
    key = "nlng_order_id" if order_type == "nlng" else "order_id"
    for e in emails:
        mid = (e.get("message_id") or "").strip()
        if not mid:
            continue
        try:
            sync.upsert_email(
                message_id=mid,
                direction=e.get("direction") or "in",
                from_address=e.get("from_address"),
                to_address=e.get("to_address"),
                subject=e.get("subject"),
                body_text=e.get("body_text"),
                received_at=e.get("received_at"),
                in_reply_to=e.get("in_reply_to"),
                **{key: order_id},
            )
            written += 1
        except Exception as exc:
            print(f"  ⚠️  {mid[:40]}: {exc}")
    return written


def read_thread(order_id: str, order_type: str) -> list[dict]:
    """This order's stored thread, oldest first.

    Sorted here rather than in the query because undated mail has to come
    first: Postgres puts NULLs last on an ascending order, which would place an
    undated email at the end of the thread and have it read as the newest.
    """
    key = "nlng_order_id" if order_type == "nlng" else "order_id"
    try:
        res = get_client().table("emails").select(
            "message_id,direction,from_address,to_address,subject,body_text,received_at"
        ).eq(key, order_id).execute()
        rows = res.data or []
    except Exception as exc:
        print(f"  ⚠️  read_thread {order_id}: {exc}")
        return []
    rows.sort(key=lambda e: e.get("received_at") or "")
    return rows


def _stamp(order_id: str, order_type: str, count: int) -> None:
    try:
        get_client().table("email_thread_sync").upsert(
            {
                "order_id":    order_id,
                "order_type":  order_type,
                "synced_at":   datetime.now(timezone.utc).isoformat(),
                "email_count": count,
            },
            on_conflict="order_id,order_type",
        ).execute()
    except Exception as exc:
        # A missed stamp means this order is simply picked up again next pass.
        print(f"  ⚠️  sync stamp {order_id}: {exc}")


def sync_thread(order_id: str, order_type: str, po_number: str,
                so_number: str = "", imap=None) -> int:
    """Walk one order's thread and store it. Returns emails written.

    Raises ThreadSyncFailed when Gmail could not be asked, and stamps nothing
    in that case, so the order comes round again on the next pass. An empty
    thread IS stamped - the search worked and there is genuinely nothing to
    find, and retrying that every pass would crowd out orders that do have
    mail.
    """
    if not po_number:
        return 0
    emails = fetch_thread(po_number, so_number, imap=imap)   # may raise
    if not emails:
        _stamp(order_id, order_type, 0)
        return 0
    written = store_thread(order_id, order_type, emails)
    _stamp(order_id, order_type, written)
    return written


# ── choosing what to sync ────────────────────────────────────────────────────

def due_orders(limit: int = BATCH_SIZE) -> list[dict]:
    """Orders whose threads are most overdue a walk, never-synced ones first.

    Read in full and ranked here rather than through a join, because the sync
    stamps live in their own table and PostgREST cannot left-join across to the
    orders it has no foreign key for. At 199 orders that is one small read.
    """
    db = get_client()
    out: list[dict] = []
    try:
        for table, otype, po_col, so_col in (
            ("orders", "chevron", "buyer_po_number", "so_number"),
            ("nlng_orders", "nlng", "po_number", "so_number"),
        ):
            try:
                res = db.table(table).select(
                    f"id,{po_col},{so_col},overall_status").execute()
            except Exception:
                continue
            for row in res.data or []:
                if not row.get(po_col):
                    continue
                out.append({
                    "order_id":   row["id"],
                    "order_type": otype,
                    "po_number":  row.get(po_col) or "",
                    "so_number":  row.get(so_col) or "",
                    "status":     row.get("overall_status") or "",
                })
    except Exception as exc:
        print(f"  ⚠️  due_orders: {exc}")
        return []

    try:
        stamps = {
            (s["order_id"], s.get("order_type") or "chevron"): s.get("synced_at")
            for s in (get_client().table("email_thread_sync")
                      .select("order_id,order_type,synced_at").execute().data or [])
        }
    except Exception as exc:
        # Without the table nothing has been synced, so everything is due. The
        # web app's own fallback still works, and the message names the file.
        print(f"  ⚠️  email_thread_sync unreadable ({exc}); "
              f"run migrations/create_email_thread_sync.sql")
        stamps = {}

    cutoff = time.time() - SYNC_INTERVAL_HOURS * 3600
    due = []
    for o in out:
        when = stamps.get((o["order_id"], o["order_type"]))
        if not when:
            o["_rank"] = ""          # never synced - sorts ahead of any date
            due.append(o)
            continue
        if o["status"].lower() in _TERMINAL_STATUSES:
            continue                 # walked once already, and it cannot change
        try:
            ts = datetime.fromisoformat(str(when).replace("Z", "+00:00")).timestamp()
        except Exception:
            ts = 0
        if ts < cutoff:
            o["_rank"] = str(when)
            due.append(o)
    due.sort(key=lambda o: o["_rank"])
    return due[:limit]


# ── the worker thread ────────────────────────────────────────────────────────

def run_pass(limit: int = BATCH_SIZE, dry_run: bool = False) -> int:
    """One pass over the most overdue orders. Returns emails written."""
    due = due_orders(limit)
    if not due:
        print("  [thread_sync] nothing due")
        return 0
    print(f"  [thread_sync] {len(due)} order(s) due")
    if dry_run:
        for o in due:
            print(f"     would sync {o['order_type']:7s} PO {o['po_number']}")
        return 0

    imap = None
    try:
        imap = open_imap()
    except Exception as exc:
        print(f"  [thread_sync] Gmail login failed: {exc}")
        return 0
    if imap is None:
        print("  [thread_sync] Gmail not configured - skipping")
        return 0

    total = 0
    failed = 0
    try:
        for o in due:
            try:
                n = sync_thread(o["order_id"], o["order_type"],
                                o["po_number"], o["so_number"], imap=imap)
                total += n
                print(f"     {o['order_type']:7s} PO {o['po_number']}: {n} email(s)")
            except ThreadSyncFailed as exc:
                # Nothing stamped, so this order is first in line next pass.
                failed += 1
                print(f"     {o['order_type']:7s} PO {o['po_number']}: "
                      f"NOT SYNCED - {exc}")
            except Exception as exc:
                # One bad thread must not stop the pass; it comes round again.
                failed += 1
                print(f"     PO {o.get('po_number')}: failed ({exc})")
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    if failed:
        print(f"  [thread_sync] {failed} of {len(due)} could not be read - "
              f"they keep their place in the queue")
    return total


def run_forever() -> None:
    print(f"[thread_sync] started - {BATCH_SIZE} orders per pass, "
          f"re-walk after {SYNC_INTERVAL_HOURS}h, "
          f"every {CHECK_INTERVAL_SECONDS}s", flush=True)
    while True:
        try:
            run_pass()
        except Exception as exc:
            print(f"[thread_sync] pass failed: {exc}", flush=True)
        time.sleep(CHECK_INTERVAL_SECONDS)


# ── command line ─────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Sync Gmail threads into the emails table")
    ap.add_argument("--limit", type=int, default=0,
                    help="how many orders to sync (default: everything due)")
    ap.add_argument("--order", help="sync one order id only")
    ap.add_argument("--type", default="chevron", choices=["chevron", "nlng"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.order:
        table = "nlng_orders" if args.type == "nlng" else "orders"
        po_col = "po_number" if args.type == "nlng" else "buyer_po_number"
        res = get_client().table(table).select(
            f"id,{po_col},so_number").eq("id", args.order).execute()
        if not res.data:
            sys.exit(f"order {args.order} not found in {table}")
        row = res.data[0]
        if args.dry_run:
            print(f"would sync PO {row.get(po_col)}")
            return
        n = sync_thread(args.order, args.type, row.get(po_col) or "",
                        row.get("so_number") or "")
        print(f"stored {n} email(s)")
        return

    # A backfill is the same pass, run until nothing is left to do.
    limit = args.limit or BATCH_SIZE
    total = 0
    while True:
        n = run_pass(limit, dry_run=args.dry_run)
        total += n
        if args.dry_run or args.limit or not due_orders(1):
            break
    print(f"\ndone - {total} email(s) stored")


if __name__ == "__main__":
    main()

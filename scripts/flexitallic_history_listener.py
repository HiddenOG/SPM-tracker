"""
flexitallic_history_listener.py - keep the Suppliers page current, live.

The Flexitallic history and client PO values were built by one-off backfills,
so the page went stale the moment they finished: SO722262 and SO722271 were
acknowledged on 10 Sep, a day after the last run, and never appeared. This
listener does per acknowledgement what those backfills did in bulk:

  1. New Flexitallic sales acknowledgement arrives (cursor-driven, Gmail).
  2. Parse it and merge it into scratch_flexitallic_history, keyed on the SO
     number - a revised acknowledgement updates its row rather than adding a
     second one.
  3. Recover the FULL client PO list from SPM's own purchase-order email,
     because Flexitallic truncates the reference at ~40 characters.
  4. Look up a value for any of those client POs that lacks one: the live
     orders table, the PO document in Gmail, then Yahoo for Mobil/Seplat.

Every 12 hours it also reconciles: re-reads the last 14 days of
acknowledgements (all steps are idempotent, so this only fills gaps the cursor
may have skipped) and retries client POs still without a value - a Chevron PO
document often reaches the mailbox after Flexitallic has acknowledged the SO.

Run under scripts/worker.py; standalone for testing:
    python scripts/flexitallic_history_listener.py --once
"""

import os
import re
import sys
import time
import email as email_mod
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, date

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

load_dotenv()

import sync
from db import get_client
from backfill_flexitallic_history import (
    FLEX_SALES, HISTORY, parse_acknowledgement, upsert_history_row, _po_list,
)
from backfill_flexitallic_po_values import _reconnect, _fetch_with_retry
from backfill_client_po_values import value_missing
from recover_client_pos import emails_for_ref, plan_row, drop_typos, _norm_pos

ACCOUNT = "gmail_flex_history"
FOLDER = "[Gmail]/All Mail"
SEARCH = ["FROM", FLEX_SALES, "SUBJECT", "Sales Acknowledgement"]

RECONCILE_EVERY = timedelta(hours=12)
RECONCILE_LOOKBACK_DAYS = 14

# The weekly audit sweeps the WHOLE history, not just the recent window. It is
# the auditor half of the parser/auditor rule: the cursor can only ever move
# forward, so an acknowledgement it skipped once would otherwise stay missing.
AUDIT_EVERY = timedelta(days=7)
AUDIT_SINCE = date(2024, 1, 1)

_last_reconcile: datetime | None = None
_last_audit: datetime | None = None


def _seed_cursor(imap, uidvalidity: int) -> None:
    """First run only: start just after the newest acknowledgement already loaded.

    Without this the shared cursor helper would start from BACKFILL_SINCE and
    re-process every acknowledgement of the year on the first pass. That would
    be harmless - every step is idempotent - but slow, and it would hide
    whether the listener picks up genuinely new mail.
    """
    if sync.get_cursor(ACCOUNT, FOLDER)["last_uid"]:
        return
    client = get_client()
    loaded = {r["so_number"] for r in client.table(HISTORY).select("so_number").execute().data}
    since = date.today() - timedelta(days=60)
    uids = imap.search(SEARCH + ["SINCE", since])
    envelopes, imap = _fetch_with_retry(imap, uids, ["ENVELOPE"]) if uids else ({}, imap)
    seed = 0
    for uid in sorted(uids):
        env = envelopes.get(uid, {}).get(b"ENVELOPE")
        subject = env.subject.decode("utf-8", "ignore") if env and env.subject else ""
        so = re.search(r"\bSO\d{5,}\b", subject)
        if so and so.group(0) in loaded:
            seed = uid
    sync.set_cursor(ACCOUNT, FOLDER, seed, uidvalidity)
    print(f"   seeded {ACCOUNT} cursor at UID {seed} (newest acknowledgement already loaded)")


def ingest(imap, uid: int, data: dict, tmpdir: str):
    """Steps 2-4 for one acknowledgement. Returns the (possibly new) imap."""
    client = get_client()
    msg = email_mod.message_from_bytes(data[b"RFC822"])
    row = parse_acknowledgement(msg, data[b"INTERNALDATE"], tmpdir)
    if not row:
        return imap

    action, row_id = upsert_history_row(client, row)
    current = (client.table(HISTORY)
               .select("id,so_number,so_date,spm_po_ref,customer,customer_po_numbers")
               .eq("id", row_id).execute().data or [None])[0]
    if not current:
        return imap
    note = f"{current['so_number']} {action}"

    ref = current.get("spm_po_ref")
    if ref:
        candidates, imap = emails_for_ref(imap, ref, tmpdir)
        plan = plan_row(current, candidates)
        have = _norm_pos(current.get("customer_po_numbers"))
        if plan["chosen"] and plan["new"]:
            # Another sales order of the SAME SPM order also matching this email
            # means the new POs could belong to either SO. The bulk recovery
            # held those back for a person to decide; the listener does too.
            siblings = [r for r in (client.table(HISTORY)
                                    .select("so_number,customer_po_numbers")
                                    .eq("spm_po_ref", ref).neq("id", row_id)
                                    .execute().data or [])
                        if _norm_pos(r.get("customer_po_numbers"))
                        and _norm_pos(r.get("customer_po_numbers")) <= plan["chosen"]["pos"]]
            if siblings:
                note += (f", {len(plan['new'])} PO(s) held back - SPM {ref} is shared with "
                         f"{[s['so_number'] for s in siblings]}")
            else:
                full = sorted(drop_typos(have | plan["new"]))
                client.table(HISTORY).update({"customer_po_numbers": full}).eq("id", row_id).execute()
                note += f", +{len(set(full) - have)} client PO(s) from SPM's PO email"
                current["customer_po_numbers"] = full
        elif not plan["chosen"]:
            note += f", PO list not recovered ({plan['why']})"

    valued = value_missing(_po_list(current.get("customer_po_numbers")))
    if valued:
        note += f", valued {len(valued)}"
    print(f"   📦 {note}")
    return imap


def check_once() -> None:
    print("   Checking Gmail for Flexitallic acknowledgements (Suppliers page)...", end="", flush=True)
    imap = _reconnect()
    tmpdir = tempfile.mkdtemp()
    try:
        uidvalidity = int(imap.folder_status(FOLDER, [b"UIDVALIDITY"])[b"UIDVALIDITY"])
        _seed_cursor(imap, uidvalidity)
        new_ids, uidvalidity = sync.new_uids_since_cursor(imap, ACCOUNT, FOLDER, SEARCH)
        if not new_ids:
            # Re-save the cursor unchanged as a heartbeat. Its updated_at is
            # what the Suppliers page shows as "checked N min ago" - without
            # this it would only move when an acknowledgement arrived, so a
            # quiet week would look exactly like a stopped listener.
            sync.set_cursor(ACCOUNT, FOLDER, sync.get_cursor(ACCOUNT, FOLDER)["last_uid"], uidvalidity)
            print(" (none)")
            return
        print(f" {len(new_ids)} new")
        highest = sync.get_cursor(ACCOUNT, FOLDER)["last_uid"]
        for i in range(0, len(new_ids), 5):
            chunk = new_ids[i:i + 5]
            fetched, imap = _fetch_with_retry(imap, chunk, ["RFC822", "INTERNALDATE"])
            for uid in chunk:
                if uid in fetched:
                    imap = ingest(imap, uid, fetched[uid], tmpdir)
            safe = sync.safe_cursor_uid(fetched, chunk)
            if safe is not None:
                highest = max(highest, safe)
                sync.set_cursor(ACCOUNT, FOLDER, highest, uidvalidity)
        # Heartbeat even when every new message was too fresh to move past.
        sync.set_cursor(ACCOUNT, FOLDER, highest, uidvalidity)
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def reconcile() -> None:
    """Re-read recent acknowledgements and retry unvalued client POs."""
    print("   🔁 Suppliers page reconcile: last "
          f"{RECONCILE_LOOKBACK_DAYS} days of acknowledgements + unvalued client POs")
    imap = _reconnect()
    tmpdir = tempfile.mkdtemp()
    try:
        since = date.today() - timedelta(days=RECONCILE_LOOKBACK_DAYS)
        uids = imap.search(SEARCH + ["SINCE", since])
        for i in range(0, len(uids), 5):
            chunk = uids[i:i + 5]
            fetched, imap = _fetch_with_retry(imap, chunk, ["RFC822", "INTERNALDATE"])
            for uid in chunk:
                if uid in fetched:
                    imap = ingest(imap, uid, fetched[uid], tmpdir)
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    rows = get_client().table(HISTORY).select("customer_po_numbers").execute().data or []
    everything = {p for r in rows for p in _po_list(r.get("customer_po_numbers"))}
    found = value_missing(everything)
    print(f"   🔁 reconcile done - {len(found)} previously unvalued client PO(s) now valued")


def audit(fix: bool = True) -> dict[str, dict]:
    """Every Flexitallic acknowledgement since 2024 whose SO is not in the table.

    Envelope-only for the sweep, so three years of acknowledgements cost one
    search and a few small fetches. Returns {so_number: {"uids", "status"}}
    where status is "added", "missing" (fix=False) or "out of scope" - an SO
    first acknowledged before 2024 that only reappears through a later copy.
    """
    client = get_client()
    loaded = {r["so_number"] for r in client.table(HISTORY).select("so_number").execute().data}
    imap = _reconnect()
    tmpdir = tempfile.mkdtemp()
    results: dict[str, dict] = {}
    try:
        uids = imap.search(SEARCH + ["SINCE", AUDIT_SINCE])
        missing: dict[str, list[int]] = {}
        for i in range(0, len(uids), 200):
            chunk = uids[i:i + 200]
            envelopes, imap = _fetch_with_retry(imap, chunk, ["ENVELOPE"])
            for uid in chunk:
                env = envelopes.get(uid, {}).get(b"ENVELOPE")
                subject = env.subject.decode("utf-8", "ignore") if env and env.subject else ""
                so = re.search(r"\bSO\d{5,}\b", subject)
                if so and so.group(0) not in loaded:
                    missing.setdefault(so.group(0), []).append(uid)

        for so, so_uids in sorted(missing.items()):
            # Out of scope if Flexitallic first acknowledged it before 2024.
            earlier = imap.search(["FROM", FLEX_SALES, "SUBJECT", so, "BEFORE", AUDIT_SINCE])
            if earlier:
                results[so] = {"uids": so_uids, "status": "out of scope"}
                continue
            if fix:
                # Earliest first, so the row keeps the original order date and
                # later revisions merge onto it.
                for uid in sorted(so_uids):
                    fetched, imap = _fetch_with_retry(imap, [uid], ["RFC822", "INTERNALDATE"])
                    if uid in fetched:
                        imap = ingest(imap, uid, fetched[uid], tmpdir)
                results[so] = {"uids": so_uids, "status": "added"}
            else:
                results[so] = {"uids": so_uids, "status": "missing"}
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    added = sum(1 for v in results.values() if v["status"] == "added")
    print(f"   🧾 Suppliers audit: {len(uids)} acknowledgements since {AUDIT_SINCE}, "
          f"{len(results)} SO(s) not in the table, {added} added")
    return results


def run_forever() -> None:
    global _last_reconcile, _last_audit
    interval = int(os.environ.get("CHECK_INTERVAL_SECONDS", 600))
    print(f"📦 Flexitallic history listener started. Checking every {interval}s.")
    consecutive_errors = 0
    while True:
        try:
            check_once()
            consecutive_errors = 0
            now = datetime.now()
            if _last_reconcile is None or now - _last_reconcile >= RECONCILE_EVERY:
                try:
                    reconcile()
                except Exception as e:
                    print(f"⚠️  Suppliers reconcile failed (non-fatal): {e!r}")
                _last_reconcile = now
            if _last_audit is None or now - _last_audit >= AUDIT_EVERY:
                try:
                    audit(fix=True)
                except Exception as e:
                    print(f"⚠️  Suppliers audit failed (non-fatal): {e!r}")
                _last_audit = now
        except KeyboardInterrupt:
            print("\n⏹  Stopped by user.")
            break
        except Exception as e:
            consecutive_errors += 1
            backoff = min(30 * (2 ** (consecutive_errors - 1)), 600)
            print(f"\n❌ Flexitallic history listener error #{consecutive_errors}: {e!r}")
            print(f"   Retrying in {backoff}s...")
            time.sleep(backoff)
            continue
        time.sleep(interval)


if __name__ == "__main__":
    if "--audit" in sys.argv:
        audit(fix="--apply" in sys.argv)
    elif "--once" in sys.argv:
        check_once()
        if "--reconcile" in sys.argv:
            reconcile()
    else:
        run_forever()

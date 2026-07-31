"""
backfill_emails.py — One-shot: populate the emails table from processed_emails.

Uses the existing processed_emails table (already in the DB) — no IMAP,
no mail servers, runs in seconds.

Safe to re-run: upsert on message_id silently ignores duplicates.

Run with:  python scripts/backfill_emails.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from db import get_client
from config import SPM_SENDER


def _direction(sender: str | None) -> str:
    if sender and SPM_SENDER.lower() in sender.lower():
        return "out"
    return "in"


def _upsert(row: dict) -> None:
    get_client().table("emails").upsert(row, on_conflict="message_id").execute()


# ── Pass 1: Chevron orders — matched_order_id already set ────────────────────

def backfill_chevron() -> int:
    print("\n── Chevron PO notifications ─────────────────────────────────")
    rows = (
        get_client()
        .table("processed_emails")
        .select("message_id,sender,subject,matched_order_id")
        .not_.is_("matched_order_id", "null")
        .execute()
        .data or []
    )
    print(f"  {len(rows)} rows with matched_order_id")
    written = 0
    for r in rows:
        try:
            _upsert({
                "message_id":   r["message_id"],
                "order_id":     r["matched_order_id"],
                "direction":    _direction(r.get("sender")),
                "from_address": r.get("sender"),
                "subject":      r.get("subject"),
            })
            written += 1
        except Exception as e:
            print(f"  ⚠️  {r.get('message_id','?')[:40]}: {e}")
    print(f"  Written: {written}")
    return written


# ── Pass 2: NLNG PO arrivals ──────────────────────────────────────────────────
# raw_notes = "NLNG PO 4200083212 v0"

def backfill_nlng_arrivals(nlng_by_po: dict) -> int:
    print("\n── NLNG PO arrivals ─────────────────────────────────────────")
    rows = (
        get_client()
        .table("processed_emails")
        .select("message_id,sender,subject,raw_notes")
        .eq("processing_result", "nlng_po_created")
        .execute()
        .data or []
    )
    print(f"  {len(rows)} rows")
    written = 0
    for r in rows:
        notes = r.get("raw_notes") or ""
        m = re.search(r"NLNG PO (\S+)", notes)
        po = m.group(1) if m else None
        oid = nlng_by_po.get(po) if po else None
        if not oid:
            continue
        try:
            _upsert({
                "message_id":   r["message_id"],
                "nlng_order_id": oid,
                "direction":    "in",
                "from_address": r.get("sender"),
                "subject":      r.get("subject"),
            })
            written += 1
        except Exception as e:
            print(f"  ⚠️  {po}: {e}")
    print(f"  Written: {written}")
    return written


# ── Pass 3: SPM → Flexitallic PO sends ───────────────────────────────────────
# raw_notes = "stamped 1 order(s)" — we need to join via spm_po in subject

def backfill_spm_flexitallic(nlng_by_spm: dict, nlng_by_po: dict) -> int:
    print("\n── SPM → Flexitallic PO sends ───────────────────────────────")
    import sync
    rows = (
        get_client()
        .table("processed_emails")
        .select("message_id,sender,subject,raw_notes")
        .eq("processing_result", "nlng_spm_po_stamped")
        .execute()
        .data or []
    )
    print(f"  {len(rows)} rows")
    written = 0
    for r in rows:
        subj = r.get("subject") or ""
        pairs = sync.is_nlng_spm_po_subject(subj)
        oid = None
        if pairs:
            for spm_po, nlng_po in pairs:
                oid = nlng_by_spm.get(spm_po) or nlng_by_po.get(nlng_po)
                if oid:
                    break
        if not oid:
            continue
        try:
            _upsert({
                "message_id":    r["message_id"],
                "nlng_order_id": oid,
                "direction":     "out",
                "from_address":  r.get("sender"),
                "subject":       subj,
            })
            written += 1
        except Exception as e:
            print(f"  ⚠️  {subj[:40]}: {e}")
    print(f"  Written: {written}")
    return written


# ── Pass 4: Flexitallic SO acknowledgements ───────────────────────────────────
# raw_notes = "SO 714770 for NLNG 4200083212"

def backfill_flexitallic_so(nlng_by_po: dict, nlng_by_so: dict) -> int:
    print("\n── Flexitallic SO acknowledgements ──────────────────────────")
    rows = (
        get_client()
        .table("processed_emails")
        .select("message_id,sender,subject,raw_notes")
        .eq("processing_result", "flexitallic_so_stamped")
        .execute()
        .data or []
    )
    print(f"  {len(rows)} rows")
    written = 0
    for r in rows:
        notes = r.get("raw_notes") or ""
        # "SO 714770 for NLNG 4200083212, 4200083213"
        so_m = re.search(r"SO (\S+)", notes)
        nlng_m = re.search(r"NLNG (.+)", notes)
        so_num = so_m.group(1).rstrip(",") if so_m else None
        oid = nlng_by_so.get(so_num) if so_num else None
        if not oid and nlng_m:
            # try first NLNG PO in notes
            first_po = nlng_m.group(1).split(",")[0].strip()
            oid = nlng_by_po.get(first_po)
        if not oid:
            continue
        try:
            _upsert({
                "message_id":    r["message_id"],
                "nlng_order_id": oid,
                "direction":     "in",
                "from_address":  r.get("sender"),
                "subject":       r.get("subject"),
            })
            written += 1
        except Exception as e:
            print(f"  ⚠️  {so_num}: {e}")
    print(f"  Written: {written}")
    return written


# ── Pass 5: SPM delivery approvals ───────────────────────────────────────────

def backfill_delivery_approvals(chevron_by_po: dict) -> int:
    print("\n── SPM delivery approval emails ─────────────────────────────")
    import sync
    rows = (
        get_client()
        .table("processed_emails")
        .select("message_id,sender,subject,raw_notes")
        .eq("processing_result", "delivery_requested")
        .execute()
        .data or []
    )
    print(f"  {len(rows)} rows")
    written = 0
    for r in rows:
        subj = r.get("subject") or ""
        notes = r.get("raw_notes") or ""
        po_numbers = sync.extract_all_po_numbers(subj + " " + notes)
        oid = next((chevron_by_po[po] for po in po_numbers if po in chevron_by_po), None)
        if not oid:
            continue
        try:
            _upsert({
                "message_id":   r["message_id"],
                "order_id":     oid,
                "direction":    "out",
                "from_address": r.get("sender"),
                "subject":      subj,
            })
            written += 1
        except Exception as e:
            print(f"  ⚠️  {subj[:40]}: {e}")
    print(f"  Written: {written}")
    return written


# ── Pass 6: NLNG warehouse routing ────────────────────────────────────────────
# processing_result = 'created_order', raw_notes contains "NLNG warehouse routing"

def backfill_nlng_routing(nlng_by_po: dict) -> int:
    print("\n── NLNG warehouse routing emails ────────────────────────────")
    rows = (
        get_client()
        .table("processed_emails")
        .select("message_id,sender,subject,raw_notes")
        .like("raw_notes", "%NLNG warehouse routing%")
        .execute()
        .data or []
    )
    print(f"  {len(rows)} rows")
    written = 0
    for r in rows:
        notes = r.get("raw_notes") or ""
        m = re.search(r"PO (\S+)", notes)
        po = m.group(1) if m else None
        oid = nlng_by_po.get(po) if po else None
        if not oid:
            continue
        try:
            _upsert({
                "message_id":    r["message_id"],
                "nlng_order_id": oid,
                "direction":     "out",
                "from_address":  r.get("sender"),
                "subject":       r.get("subject"),
            })
            written += 1
        except Exception as e:
            print(f"  ⚠️  {po}: {e}")
    print(f"  Written: {written}")
    return written


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("🔄 Email backfill starting (DB-only, no IMAP)...")

    chevron_by_po = {
        r["buyer_po_number"]: r["id"]
        for r in (get_client().table("orders").select("id,buyer_po_number").execute().data or [])
    }
    nlng_by_po = {
        r["po_number"]: r["id"]
        for r in (get_client().table("nlng_orders").select("id,po_number").execute().data or [])
        if r.get("po_number")
    }
    nlng_by_spm = {
        r["spm_po_number"]: r["id"]
        for r in (
            get_client().table("nlng_orders").select("id,spm_po_number")
            .not_.is_("spm_po_number", "null").execute().data or []
        )
        if r.get("spm_po_number")
    }
    nlng_by_so = {
        r["so_number"]: r["id"]
        for r in (
            get_client().table("nlng_orders").select("id,so_number")
            .not_.is_("so_number", "null").execute().data or []
        )
        if r.get("so_number")
    }

    print(f"  Chevron orders: {len(chevron_by_po)}  |  NLNG: {len(nlng_by_po)}")

    total  = backfill_chevron()
    total += backfill_nlng_arrivals(nlng_by_po)
    total += backfill_spm_flexitallic(nlng_by_spm, nlng_by_po)
    total += backfill_flexitallic_so(nlng_by_po, nlng_by_so)
    total += backfill_delivery_approvals(chevron_by_po)
    total += backfill_nlng_routing(nlng_by_po)

    print(f"\n✅ Done — {total} email record(s) written to emails table.")


if __name__ == "__main__":
    main()

"""
gap_auditor.py — Pipeline null classifier.

For every null timestamp in the pipeline stages, searches Gmail to decide:
  PARSER MISS — a matching email EXISTS but the parser didn't stamp it
  REAL NULL   — no matching email found (genuinely empty, e.g. not reached yet)

When a PARSER MISS is found (email exists but column is null), the auditor
auto-fixes it: stamps the column directly in the DB and advances overall_status
monotonically via sync.advance_status(). REAL NULL rows are left untouched.

Run:  python scripts/gap_auditor.py
      python scripts/gap_auditor.py --stage dispatch_rdy   (one stage)
      python scripts/gap_auditor.py --stage all            (default)

Stages checked:
  sent_to_wh    sent_to_warehouse_at         (did SPM route PO to warehouse for stock check?)
  stock_check   stock_check_completed_at     (did warehouse reply with stock status?)
  sc_raw        stock_check_raw              (timestamp set but body unparsed/null?)
  spm_sent      spm_po_sent_at               (was an SPM PO emailed for this Chevron PO?)
  spm_po_num    spm_po_number                (is the SPM PO number string populated on the order?)
  so_ack        so_received_at               (did Flexitallic send a sales acknowledgement?)
  dispatch_rdy  flex_dispatch_ready_at       (did Flexitallic say "packed & ready"?)
  dispatch_ins  dispatch_instructions_sent_at (did we reply with dispatch instructions?)
  ready_dispatch ready_for_dispatch_at       (did Penny confirm collection arranged?)
  dispatched    dispatched_at               (did the shipping company reply "Noted"?)
  warehouse     so_sent_to_warehouse_at      (did we forward the SO to the warehouse?)
  delivery_req  delivery_requested_at        (did warehouse send REQUEST FOR DELIVERY?)
  delivered     delivered_at                 (did warehouse confirm completely delivered?)
"""

import os
import re
import sys
import time
import email
import argparse
from datetime import datetime, timezone
from email.header import decode_header as _dh

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sync
from db import get_client
from imapclient import IMAPClient
from dotenv import load_dotenv
from config import SPM_SENDER, WAREHOUSE_EMAIL

load_dotenv()


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _decode(s) -> str:
    if not s:
        return ""
    out = ""
    for part, enc in _dh(s):
        if isinstance(part, bytes):
            out += part.decode(enc or "utf-8", errors="replace")
        else:
            out += part
    return out.strip()


class _ReconnectingIMAP:
    """
    Thin wrapper around IMAPClient that silently reconnects and retries
    once when Gmail drops the connection mid-audit (EOF / BYE errors).
    All audit functions receive an instance of this instead of a raw client.
    """

    def __init__(self):
        self._client = self._connect()

    def _connect(self):
        try:
            user = os.environ["GMAIL_EMAIL"]
            password = os.environ["GMAIL_APP_PASSWORD"]
            c = IMAPClient("imap.gmail.com", ssl=True)
            c.login(user, password)
            c.select_folder("[Gmail]/All Mail", readonly=True)
            return c
        except KeyError as e:
            raise RuntimeError(f"Missing env var {e} — check your .env file") from e
        except Exception as e:
            raise RuntimeError(f"IMAP connect failed: {e}") from e

    def _is_dropped(self, exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(kw in msg for kw in ("eof", "bye", "broken pipe", "socket", "reset"))

    def _reconnect(self):
        try:
            self._client.logout()
        except Exception:
            pass
        print("    [reconnecting to Gmail...]")
        self._client = self._connect()

    def search(self, criteria: list) -> list:
        try:
            return self._client.search(criteria)
        except Exception as e:
            if self._is_dropped(e):
                self._reconnect()
                return self._client.search(criteria)
            raise

    def fetch(self, uids, data_items):
        try:
            return self._client.fetch(uids, data_items)
        except Exception as e:
            if self._is_dropped(e):
                self._reconnect()
                return self._client.fetch(uids, data_items)
            raise

    def fetch_safe(self, uids, data_items) -> dict:
        """Like fetch() but returns {} on any non-reconnect error (e.g. OVERQUOTA).
        Use this for body/filtering fetches inside audit stages so a quota event
        doesn't crash the entire auditor run."""
        try:
            return self._client.fetch(uids, data_items)
        except Exception as e:
            if self._is_dropped(e):
                self._reconnect()
                try:
                    return self._client.fetch(uids, data_items)
                except Exception:
                    return {}
            print(f"    IMAP fetch error (non-fatal): {e}")
            return {}

    def logout(self):
        try:
            self._client.logout()
        except Exception:
            pass


def _envelope_summary(imap, uids: list) -> list[dict]:
    """Fetch ENVELOPE for up to 5 UIDs, return list of {date, from, subject}."""
    if not uids:
        return []
    msgs = imap.fetch_safe(uids[:5], ["ENVELOPE"])
    results = []
    for uid, data in msgs.items():
        env = data.get(b"ENVELOPE")
        if not env:
            continue
        subj = _decode(env.subject.decode(errors="replace") if isinstance(env.subject, bytes) else (env.subject or ""))
        dt = env.date  # datetime object from IMAPClient
        date_iso = dt.isoformat() if dt else None
        date_str = str(dt) if dt else "?"
        sender = ""
        if env.from_:
            f = env.from_[0]
            mb = f.mailbox.decode(errors="replace") if f.mailbox else ""
            host = f.host.decode(errors="replace") if f.host else ""
            sender = f"{mb}@{host}"
        results.append({"uid": uid, "date": date_str, "date_iso": date_iso, "from": sender, "subject": subj})
    return results


def _search(imap, criteria: list, label="") -> list:
    try:
        result = imap.search(criteria)
        time.sleep(0.25)   # pace IMAP commands to avoid OVERQUOTA
        return result
    except Exception as e:
        print(f"    IMAP search error ({label}): {e}")
        time.sleep(0.5)    # back off longer after an error
        return []


MISS = "PARSER MISS"
REAL = "REAL NULL"
FIXED = "FIXED"

def _classify(hits):
    return MISS if hits else REAL


def _print_subtotal(r: dict) -> None:
    n_miss = sum(1 for v in r.values() if v["classification"] == MISS)
    n_real = sum(1 for v in r.values() if v["classification"] == REAL)
    n_fixed = sum(1 for v in r.values() if v["classification"] == FIXED)
    print(f"  Subtotal: {n_miss} misses, {n_real} real nulls, {n_fixed} fixed")


def _fetch_warehouse_candidates(imap, uids: list, limit: int = 10) -> tuple:
    """Fetch up to `limit` warehouse UIDs, filter out REQUEST FOR DELIVERY emails.
    Returns (candidate_uids, candidate_bodies, hits) — hits built from the same
    ENVELOPE data already fetched, avoiding a second round-trip."""
    candidate_uids = []
    candidate_bodies = {}
    hits = []
    if not uids:
        return candidate_uids, candidate_bodies, hits
    msgs = imap.fetch_safe(uids[:limit], ["ENVELOPE", "BODY[TEXT]"])
    for uid, data in msgs.items():
        env = data.get(b"ENVELOPE")
        if not env:
            continue
        subj = _decode(env.subject.decode(errors="replace") if isinstance(env.subject, bytes) else (env.subject or ""))
        if "request" in subj.lower() and "deliv" in subj.lower():
            continue
        candidate_uids.append(uid)
        body_raw = data.get(b"BODY[TEXT]") or b""
        candidate_bodies[uid] = body_raw.decode(errors="replace") if isinstance(body_raw, bytes) else str(body_raw)
        if len(hits) < 5:
            dt = env.date
            sender = ""
            if env.from_:
                f = env.from_[0]
                mb = f.mailbox.decode(errors="replace") if f.mailbox else ""
                host = f.host.decode(errors="replace") if f.host else ""
                sender = f"{mb}@{host}"
            hits.append({"uid": uid, "date": str(dt) if dt else "?",
                          "date_iso": dt.isoformat() if dt else None,
                          "from": sender, "subject": subj})
    return candidate_uids, candidate_bodies, hits


def _copy_spm_po_number_from_junction(db, order_id: str):
    """Look up the SPM PO number for an order via the junction table."""
    link = db.table("spm_po_chevron_links").select("spm_po_id").eq("order_id", order_id).execute()
    if not link.data:
        return None
    spm = db.table("spm_purchase_orders").select("spm_po_number").eq("id", link.data[0]["spm_po_id"]).execute()
    return spm.data[0].get("spm_po_number") if spm.data else None


def _print_result(stage, ref, hint, classification, hits):
    if classification == FIXED:
        marker = "[FIXED] "
    elif classification == MISS:
        marker = "[MISS]  "
    else:
        marker = "   "
    print(f"{marker}[{stage}] {ref}")
    print(f"       hint={hint}  ->  {classification}")
    for h in hits:
        print(f"       [{h['date']}] from={h['from']}  subj={h['subject'][:80]}")


# ─────────────────────────────────────────────
# Fix helpers — stamp a column and advance status
# ─────────────────────────────────────────────

def _fix_order_timestamp(db, po: str, field: str, ts: str, status: str) -> bool:
    """Stamp field on every order row for this buyer_po_number where it is still NULL.
    Advances overall_status monotonically. Returns True if at least one row was stamped."""
    rows = db.table("orders").select("id").eq("buyer_po_number", po).execute()
    fixed = False
    for row in rows.data:
        result = (
            db.table("orders")
            .update({field: ts})
            .eq("id", row["id"])
            .is_(field, "null")
            .execute()
        )
        if result.data:
            sync.advance_status(db, row["id"], status)
            fixed = True
    return fixed


def _apply_stock_check_raw(db, order_id: str, body_text: str) -> None:
    """Interpret a warehouse reply body and write stock_check_raw + advance status.
    Mirrors the reconcile_po logic so the auditor populates raw the same way the parser does."""
    from warehouse_reply_parser import interpret_reply
    result, method = interpret_reply(body_text)
    if result.get("overall_availability") == "followup":
        return
    if method == "deferred":
        raw = {
            "overall_availability": "unclear",
            "needs_human_review": True,
            "confidence": "low",
            "summary": "Partial/complex warehouse reply — awaiting AI interpretation",
            "raw_body": body_text.strip()[:1000],
            "interpretation_method": "deferred",
        }
        res = db.table("orders").update({"stock_check_raw": raw, "pending_stock_extraction": True}).eq("id", order_id).is_("stock_check_raw", "null").execute()
        if res.data:
            sync.advance_status(db, order_id, "stock_check_needs_review")
    else:
        new_status = "stock_check_needs_review" if (result.get("needs_human_review") or result.get("confidence") == "low") else "stock_check_complete"
        res = db.table("orders").update({"stock_check_raw": result, "pending_stock_extraction": False}).eq("id", order_id).is_("stock_check_raw", "null").execute()
        if res.data:
            sync.advance_status(db, order_id, new_status)


def _fix_spm_po_timestamp(db, spm_ref, so_number, field: str, ts: str, status: str) -> bool:
    """Stamp field on spm_purchase_orders + all linked order rows.
    Returns True if at least one order was stamped."""
    spm_po = None
    if spm_ref:
        res = db.table("spm_purchase_orders").select("id").eq("spm_po_ref", spm_ref).execute()
        spm_po = res.data[0] if res.data else None
    if not spm_po and so_number:
        res = db.table("spm_purchase_orders").select("id").eq("so_number", so_number).execute()
        spm_po = res.data[0] if res.data else None
    if not spm_po:
        return False
    spm_res = db.table("spm_purchase_orders").update({field: ts}).eq("id", spm_po["id"]).is_(field, "null").execute()
    spm_fixed = bool(spm_res.data)
    links = db.table("spm_po_chevron_links").select("order_id").eq("spm_po_id", spm_po["id"]).execute()
    order_fixed = False
    for link in links.data:
        if link["order_id"]:
            result = (
                db.table("orders")
                .update({field: ts})
                .eq("id", link["order_id"])
                .is_(field, "null")
                .execute()
            )
            if result.data:
                sync.advance_status(db, link["order_id"], status)
                order_fixed = True
    return spm_fixed or order_fixed


# ─────────────────────────────────────────────
# Stage checkers
# ─────────────────────────────────────────────

def audit_sent_to_warehouse(db, imap, since) -> dict:
    """
    sent_to_warehouse_at on orders.
    Evidence: FROM specialpiping@gmail.com, TO spmwarehouse22, SUBJECT/BODY buyer_po_number.
    This is the bare-PO routing email SPM sends to ask the warehouse for a stock check.
    """
    rows = (
        db.table("orders")
        .select("id, buyer_po_number, sent_to_warehouse_at")
        .is_("sent_to_warehouse_at", "null")
        .not_.is_("buyer_po_number", "null")
        .execute()
    )
    results = {}
    for row in rows.data:
        po = row["buyer_po_number"]
        if not po:
            continue
        uids = _search(imap, ["FROM", SPM_SENDER, "TO", WAREHOUSE_EMAIL, "BODY", po, "SINCE", since], label=f"sent_wh/{po}")
        if not uids:
            # Also try subject-only match (bare PO subject emails)
            uids = _search(imap, ["FROM", SPM_SENDER, "TO", WAREHOUSE_EMAIL, "SUBJECT", po, "SINCE", since], label=f"sent_wh_subj/{po}")
        hits = _envelope_summary(imap, uids)
        classification = _classify(hits)
        if classification == MISS and hits and hits[0].get("date_iso"):
            if _fix_order_timestamp(db, po, "sent_to_warehouse_at", hits[0]["date_iso"], "awaiting_warehouse_stock_check"):
                classification = FIXED
        results[po] = {"classification": classification, "hits": hits}
    return results


def audit_stock_check(db, imap, since) -> dict:
    """
    stock_check_completed_at on orders.
    Evidence: FROM spmwarehouse22, BODY buyer_po_number.
    Filters out REQUEST FOR DELIVERY emails (those stamp delivery_requested_at, not this).
    """
    rows = (
        db.table("orders")
        .select("id, buyer_po_number, stock_check_completed_at, sent_to_warehouse_at")
        .is_("stock_check_completed_at", "null")
        .not_.is_("sent_to_warehouse_at", "null")   # only check orders already routed
        .execute()
    )
    results = {}
    for row in rows.data:
        po = row["buyer_po_number"]
        if not po or po in results:
            continue  # skip duplicate PO rows — first iteration already handled all orders for this PO
        uids = _search(imap, ["FROM", WAREHOUSE_EMAIL, "BODY", po, "SINCE", since], label=f"stock/{po}")
        candidate_uids, candidate_bodies, hits = _fetch_warehouse_candidates(imap, uids)
        classification = _classify(hits)
        if classification == MISS and hits and hits[0].get("date_iso"):
            ts = hits[0]["date_iso"]
            uid0 = candidate_uids[0] if candidate_uids else None
            body_text = candidate_bodies.get(uid0, "") if uid0 else ""
            # Pre-check: skip followup emails entirely — don't stamp the timestamp for them
            from warehouse_reply_parser import interpret_reply as _ir
            _result, _method = _ir(body_text) if body_text else ({}, "")
            if _result.get("overall_availability") == "followup":
                results[po] = {"classification": REAL, "hits": hits}
                continue
            orders = db.table("orders").select("id").eq("buyer_po_number", po).execute()
            fixed = False
            for r in orders.data:
                res = db.table("orders").update({"stock_check_completed_at": ts}).eq("id", r["id"]).is_("stock_check_completed_at", "null").execute()
                if res.data:
                    _apply_stock_check_raw(db, r["id"], body_text)
                    fixed = True
            if fixed:
                classification = FIXED
        results[po] = {"classification": classification, "hits": hits}
    return results


def audit_stock_check_raw(db, imap, since) -> dict:
    """
    stock_check_raw on orders.
    Covers orders where stock_check_completed_at is already set but stock_check_raw is still null
    (parser stamped the timestamp but body interpretation failed or was deferred).
    Evidence: FROM spmwarehouse22, BODY buyer_po_number. Fetches body and calls interpret_reply.
    """
    rows = (
        db.table("orders")
        .select("id, buyer_po_number")
        .not_.is_("stock_check_completed_at", "null")
        .is_("stock_check_raw", "null")
        .execute()
    )
    results = {}
    for row in rows.data:
        po = row["buyer_po_number"]
        if not po:
            continue
        uids = _search(imap, ["FROM", WAREHOUSE_EMAIL, "BODY", po, "SINCE", since], label=f"sc_raw/{po}")
        candidate_uids, candidate_bodies, hits = _fetch_warehouse_candidates(imap, uids)
        classification = _classify(hits)
        if classification == MISS and candidate_uids:
            uid0 = candidate_uids[0]
            body_text = candidate_bodies.get(uid0, "")
            if body_text:
                _apply_stock_check_raw(db, row["id"], body_text)
                classification = FIXED
        results[po] = {"classification": classification, "hits": hits}
    return results


def audit_spm_sent(db, imap, since) -> dict:
    """
    spm_po_sent_at on orders.
    Evidence: FROM specialpiping@gmail.com SUBJECT "PURCHASE ORDER" BODY buyer_po_number.
    """
    rows = (
        db.table("orders")
        .select("id, buyer_po_number, spm_po_sent_at")
        .is_("spm_po_sent_at", "null")
        .not_.is_("buyer_po_number", "null")
        .execute()
    )
    results = {}
    for row in rows.data:
        po = row["buyer_po_number"]
        if not po:
            continue
        uids = _search(imap, ["FROM", SPM_SENDER, "SUBJECT", "PURCHASE ORDER", "BODY", po, "SINCE", since], label=f"spm_sent/{po}")
        hits = _envelope_summary(imap, uids)
        classification = _classify(hits)
        if classification == MISS and hits and hits[0].get("date_iso"):
            if _fix_order_timestamp(db, po, "spm_po_sent_at", hits[0]["date_iso"], "po_sent"):
                classification = FIXED
        results[po] = {"classification": classification, "hits": hits}
    return results


def audit_spm_po_number(db, imap, since) -> dict:
    """
    spm_po_number on orders.
    Case A — spm_po_sent_at already set: no IMAP needed, copy from linked spm_purchase_orders row.
    Case B — both null: search for the outgoing PURCHASE ORDER email, then copy from linked row.
    """
    rows = (
        db.table("orders")
        .select("id, buyer_po_number, spm_po_number, spm_po_sent_at")
        .is_("spm_po_number", "null")
        .not_.is_("buyer_po_number", "null")
        .execute()
    )
    results = {}
    for row in rows.data:
        po = row["buyer_po_number"]
        if not po:
            continue

        # Case A: spm_po_sent_at already set — copy number from junction, no IMAP needed
        if row.get("spm_po_sent_at"):
            spm_po_number = _copy_spm_po_number_from_junction(db, row["id"])
            if spm_po_number:
                res = db.table("orders").update({"spm_po_number": spm_po_number}).eq("id", row["id"]).is_("spm_po_number", "null").execute()
                if res.data:
                    sync.advance_status(db, row["id"], "po_sent")
                results[po] = {"classification": FIXED, "hits": [], "note": "copied from spm_purchase_orders (no IMAP)"}
            else:
                results[po] = {"classification": REAL, "hits": [], "note": "no junction link found"}
            continue

        # Case B: both null — search Gmail for the outgoing PO email
        uids = _search(imap, ["FROM", SPM_SENDER, "SUBJECT", "PURCHASE ORDER", "BODY", po, "SINCE", since], label=f"spm_num/{po}")
        hits = _envelope_summary(imap, uids)
        classification = _classify(hits)
        if classification == MISS and hits and hits[0].get("date_iso"):
            ts = hits[0]["date_iso"]
            spm_po_number = _copy_spm_po_number_from_junction(db, row["id"])
            update = {"spm_po_sent_at": ts}
            if spm_po_number:
                update["spm_po_number"] = spm_po_number
            res = db.table("orders").update(update).eq("id", row["id"]).is_("spm_po_number", "null").execute()
            if res.data:
                sync.advance_status(db, row["id"], "po_sent")
                classification = FIXED
        results[po] = {"classification": classification, "hits": hits}
    return results


def audit_so_ack(db, imap, since) -> dict:
    """
    so_received_at on orders.
    Evidence via spm_purchase_orders: FROM salesorder@flexitallic.eu SUBJECT Acknowledgement BODY spm_po_ref.
    """
    spm_pos = (
        db.table("spm_purchase_orders")
        .select("id, spm_po_ref, so_acknowledged_at")
        .is_("so_acknowledged_at", "null")
        .not_.is_("spm_po_ref", "null")
        .execute()
    )
    results = {}
    for spm in spm_pos.data:
        ref = spm["spm_po_ref"]
        if not ref:
            continue
        uids = _search(imap, ["FROM", "salesorder@flexitallic.eu", "SUBJECT", "Acknowledgement", "BODY", ref, "SINCE", since], label=f"so_ack/{ref}")
        hits = _envelope_summary(imap, uids)
        classification = _classify(hits)
        if classification == MISS and hits and hits[0].get("date_iso"):
            ts = hits[0]["date_iso"]
            subject = hits[0].get("subject", "")
            so_m = re.search(r"\bSO\d{5,9}\b", subject)
            so_number = so_m.group(0) if so_m else None
            # so_acknowledged_at lives on spm_purchase_orders; so_received_at on orders
            db.table("spm_purchase_orders").update({"so_acknowledged_at": ts}).eq("id", spm["id"]).is_("so_acknowledged_at", "null").execute()
            links = db.table("spm_po_chevron_links").select("order_id").eq("spm_po_id", spm["id"]).execute()
            any_fixed = False
            for link in links.data:
                if link["order_id"]:
                    patch = {"so_received_at": ts}
                    if so_number:
                        patch["so_number"] = so_number
                    res = db.table("orders").update(patch).eq("id", link["order_id"]).is_("so_received_at", "null").execute()
                    if res.data:
                        sync.advance_status(db, link["order_id"], "supplier_acknowledged")
                        any_fixed = True
                    elif so_number:
                        # so_received_at already set — try to fill missing so_number alone
                        db.table("orders").update({"so_number": so_number}).eq("id", link["order_id"]).is_("so_number", "null").execute()
            if any_fixed:
                classification = FIXED
        results[ref] = {"classification": classification, "hits": hits}

    # Back-fill so_number for orders where so_received_at is set but so_number is still null.
    # The main loop above only covers spm_purchase_orders with so_acknowledged_at IS NULL,
    # so orders that were stamped in an earlier run but missed so_number are not in scope there.
    gaps = db.table("orders").select("id, buyer_po_number").not_.is_("so_received_at", "null").is_("so_number", "null").execute()
    for gap_order in (gaps.data or []):
        po = gap_order.get("buyer_po_number")
        if not po:
            continue
        uids = _search(imap, ["FROM", "salesorder@flexitallic.eu", "SUBJECT", "Acknowledgement", "BODY", po, "SINCE", since], label=f"so_num_gap/{po}")
        if uids:
            hits = _envelope_summary(imap, uids)
            if hits:
                subject = hits[0].get("subject", "")
                so_m = re.search(r"\bSO\d{5,9}\b", subject)
                if so_m:
                    db.table("orders").update({"so_number": so_m.group(0)}).eq("id", gap_order["id"]).is_("so_number", "null").execute()

    return results


def _non_ack_uids_for_so(imap, so: str, since: str, label: str) -> list:
    """Search Flexitallic emails mentioning SO, excluding salesorder@ ack emails.
    Tries BODY first, falls back to SUBJECT."""
    uids = _search(imap, ["FROM", "flexitallic.eu", "BODY", so, "SINCE", since], label=f"{label}/body")
    if not uids:
        uids = _search(imap, ["FROM", "flexitallic.eu", "SUBJECT", so, "SINCE", since], label=f"{label}/subj")
    non_ack = []
    if uids:
        msgs = imap.fetch_safe(uids[:30], ["ENVELOPE"])
        for uid, data in msgs.items():
            env = data.get(b"ENVELOPE")
            if not env:
                continue
            sender_mb = (env.from_[0].mailbox or b"").decode(errors="replace") if env.from_ else ""
            sender_host = (env.from_[0].host or b"").decode(errors="replace") if env.from_ else ""
            if "salesorder@flexitallic.eu" not in f"{sender_mb}@{sender_host}".lower():
                non_ack.append(uid)
    return non_ack


def audit_dispatch_ready(db, imap, since) -> dict:
    """
    flex_dispatch_ready_at on orders (stamped via spm_purchase_orders).
    Evidence: FROM flexitallic.eu (NOT salesorder@), SO number in BODY or SUBJECT.
    Two passes: via spm_purchase_orders, then via orders.so_number directly.
    """
    results = {}

    # ── Pass 1: via spm_purchase_orders ───────────────────────────────────────
    spm_pos = (
        db.table("spm_purchase_orders")
        .select("id, spm_po_ref, so_number, flex_dispatch_ready_at")
        .is_("flex_dispatch_ready_at", "null")
        .not_.is_("so_number", "null")
        .execute()
    )
    for spm in spm_pos.data:
        ref = spm["spm_po_ref"]
        so = spm["so_number"]
        if not so:
            continue
        non_ack_uids = _non_ack_uids_for_so(imap, so, since, f"disp_rdy/{ref}")
        hits = _envelope_summary(imap, non_ack_uids)
        classification = _classify(hits)
        if classification == MISS and hits and hits[0].get("date_iso"):
            if _fix_spm_po_timestamp(db, ref, so, "flex_dispatch_ready_at", hits[0]["date_iso"], "dispatch_packed_awaiting_instruction"):
                classification = FIXED
        results[ref] = {"so": so, "classification": classification, "hits": hits}

    # ── Pass 2: orders with so_number set not caught above ────────────────────
    orders_with_so = (
        db.table("orders")
        .select("id, buyer_po_number, so_number")
        .is_("flex_dispatch_ready_at", "null")
        .not_.is_("so_number", "null")
        .execute()
    )
    seen_so = {v.get("so") for v in results.values() if v.get("so")}
    for row in orders_with_so.data:
        so = row["so_number"]
        po = row["buyer_po_number"]
        if not so or so in seen_so:
            continue
        seen_so.add(so)
        non_ack_uids = _non_ack_uids_for_so(imap, so, since, f"disp_rdy_so/{po}")
        hits = _envelope_summary(imap, non_ack_uids)
        classification = _classify(hits)
        if classification == MISS and hits and hits[0].get("date_iso"):
            if _fix_spm_po_timestamp(db, None, so, "flex_dispatch_ready_at", hits[0]["date_iso"], "dispatch_packed_awaiting_instruction"):
                classification = FIXED
        results[f"SO:{so}"] = {"so": so, "classification": classification, "hits": hits}

    return results


def audit_dispatch_instructions(db, imap, since) -> dict:
    """
    dispatch_instructions_sent_at on orders (stamped via spm_purchase_orders).
    Evidence: FROM specialpiping@gmail.com TO flexitallic.eu, SO in BODY or SUBJECT.
    Two passes: via spm_purchase_orders, then via orders.so_number directly.
    """
    results = {}

    def _search_dispatch_ins(so, label):
        uids = _search(imap, ["FROM", SPM_SENDER, "TO", "flexitallic.eu", "BODY", so, "SINCE", since], label=f"{label}/body")
        if not uids:
            uids = _search(imap, ["FROM", SPM_SENDER, "TO", "flexitallic.eu", "SUBJECT", so, "SINCE", since], label=f"{label}/subj")
        return uids

    # ── Pass 1: via spm_purchase_orders ───────────────────────────────────────
    spm_pos = (
        db.table("spm_purchase_orders")
        .select("id, spm_po_ref, so_number, dispatch_instructions_sent_at")
        .is_("dispatch_instructions_sent_at", "null")
        .not_.is_("so_number", "null")
        .execute()
    )
    for spm in spm_pos.data:
        ref = spm["spm_po_ref"]
        so = spm["so_number"]
        if not so:
            continue
        uids = _search_dispatch_ins(so, f"disp_ins/{ref}")
        hits = _envelope_summary(imap, uids)
        classification = _classify(hits)
        if classification == MISS and hits and hits[0].get("date_iso"):
            if _fix_spm_po_timestamp(db, ref, so, "dispatch_instructions_sent_at", hits[0]["date_iso"], "dispatch_instruction_sent"):
                classification = FIXED
        results[ref] = {"so": so, "classification": classification, "hits": hits}

    # ── Pass 2: orders with so_number set not caught above ────────────────────
    orders_with_so = (
        db.table("orders")
        .select("id, buyer_po_number, so_number")
        .is_("dispatch_instructions_sent_at", "null")
        .not_.is_("so_number", "null")
        .execute()
    )
    seen_so = {v.get("so") for v in results.values() if v.get("so")}
    for row in orders_with_so.data:
        so = row["so_number"]
        po = row["buyer_po_number"]
        if not so or so in seen_so:
            continue
        seen_so.add(so)
        uids = _search_dispatch_ins(so, f"disp_ins_so/{po}")
        hits = _envelope_summary(imap, uids)
        classification = _classify(hits)
        if classification == MISS and hits and hits[0].get("date_iso"):
            if _fix_spm_po_timestamp(db, None, so, "dispatch_instructions_sent_at", hits[0]["date_iso"], "dispatch_instruction_sent"):
                classification = FIXED
        results[f"SO:{so}"] = {"so": so, "classification": classification, "hits": hits}

    return results


def audit_warehouse(db, imap, since) -> dict:
    """
    so_sent_to_warehouse_at on orders.
    Evidence: FROM specialpiping@gmail.com TO spmwarehouse22@gmail.com BODY so_number.
    Looks up the SO number via the linked spm_purchase_orders row.
    """
    rows = (
        db.table("orders")
        .select("id, buyer_po_number, so_sent_to_warehouse_at")
        .is_("so_sent_to_warehouse_at", "null")
        .not_.is_("buyer_po_number", "null")
        .execute()
    )
    # Build buyer_po_number → so_number map via links
    results = {}
    for row in rows.data:
        po = row["buyer_po_number"]
        if not po:
            continue
        # Find linked SPM PO
        link = (
            db.table("spm_po_chevron_links")
            .select("spm_po_id")
            .eq("order_id", row["id"])
            .execute()
        )
        if not link.data:
            results[po] = {"so": None, "classification": REAL, "hits": [], "note": "no SPM PO link"}
            continue
        spm_id = link.data[0]["spm_po_id"]
        spm = db.table("spm_purchase_orders").select("so_number").eq("id", spm_id).execute()
        so = spm.data[0]["so_number"] if spm.data else None
        if not so:
            results[po] = {"so": None, "classification": REAL, "hits": [], "note": "no SO number yet"}
            continue
        uids = _search(imap, ["FROM", SPM_SENDER, "TO", WAREHOUSE_EMAIL, "BODY", so, "SINCE", since], label=f"wh/{po}")
        hits = _envelope_summary(imap, uids)
        classification = _classify(hits)
        if classification == MISS and hits and hits[0].get("date_iso"):
            if _fix_order_timestamp(db, po, "so_sent_to_warehouse_at", hits[0]["date_iso"], "so_sent_to_warehouse"):
                classification = FIXED
        results[po] = {"so": so, "classification": classification, "hits": hits}
    return results


def _is_ready_for_dispatch_body(body_text: str) -> bool:
    """Return True if body looks like Penny's 'collection arranged' email."""
    b = body_text.lower()
    has_arrange = "arrange" in b or "collect" in b
    has_shipper = "unicorn" in b or "transport" in b or "pudsey" in b or "rtc" in b
    return has_arrange and has_shipper


def _search_ready_for_dispatch_uids(imap, so: str, since: str, label: str) -> list:
    """
    Search for Penny's 'collection arranged' email by SO number.
    Tries BODY first (catches emails where SO appears in quoted thread),
    then falls back to SUBJECT (catches emails where SO is only in subject line).
    Excludes salesorder@ ack emails in both passes.
    """
    uids = _search(imap, ["FROM", "flexitallic.eu", "BODY", so, "SINCE", since], label=f"{label}/body")
    if not uids:
        uids = _search(imap, ["FROM", "flexitallic.eu", "SUBJECT", so, "SINCE", since], label=f"{label}/subj")
    return uids


def audit_ready_for_dispatch(db, imap, since) -> dict:
    """
    ready_for_dispatch_at on orders (stamped via spm_purchase_orders).
    Evidence: FROM flexitallic.eu (Penny), SO number in BODY or SUBJECT,
    body contains 'arrange'/'collect' + shipper keyword (unicorn/transport/pudsey/rtc).

    Two search passes per SO number:
      1. Via spm_purchase_orders (primary — covers all linked orders at once)
      2. Via orders.so_number directly (catches orders not yet linked via junction)
    """
    results = {}

    # ── Pass 1: via spm_purchase_orders ───────────────────────────────────────
    spm_pos = (
        db.table("spm_purchase_orders")
        .select("id, spm_po_ref, so_number, ready_for_dispatch_at")
        .is_("ready_for_dispatch_at", "null")
        .not_.is_("so_number", "null")
        .execute()
    )
    for spm in spm_pos.data:
        ref = spm["spm_po_ref"]
        so = spm["so_number"]
        if not so:
            continue
        uids = _search_ready_for_dispatch_uids(imap, so, since, f"ready/{ref}")
        candidate_uids = []
        if uids:
            msgs = imap.fetch_safe(uids[:20], ["ENVELOPE", "BODY[TEXT]"])
            for uid, data in msgs.items():
                env = data.get(b"ENVELOPE")
                if env:
                    sender_mb = (env.from_[0].mailbox or b"").decode(errors="replace") if env.from_ else ""
                    sender_host = (env.from_[0].host or b"").decode(errors="replace") if env.from_ else ""
                    if "salesorder@flexitallic.eu" in f"{sender_mb}@{sender_host}".lower():
                        continue
                body_raw = data.get(b"BODY[TEXT]") or b""
                body_text = body_raw.decode(errors="replace") if isinstance(body_raw, bytes) else str(body_raw)
                if _is_ready_for_dispatch_body(body_text):
                    candidate_uids.append(uid)
        hits = _envelope_summary(imap, candidate_uids)
        classification = _classify(hits)
        if classification == MISS and hits and hits[0].get("date_iso"):
            if _fix_spm_po_timestamp(db, ref, so, "ready_for_dispatch_at", hits[0]["date_iso"], "ready_for_dispatch"):
                classification = FIXED
        results[ref] = {"so": so, "classification": classification, "hits": hits}

    # ── Pass 2: orders with so_number set but no junction / not caught above ──
    orders_with_so = (
        db.table("orders")
        .select("id, buyer_po_number, so_number")
        .is_("ready_for_dispatch_at", "null")
        .not_.is_("so_number", "null")
        .execute()
    )
    seen_so = {v.get("so") for v in results.values() if v.get("so")}
    for row in orders_with_so.data:
        so = row["so_number"]
        po = row["buyer_po_number"]
        if not so or so in seen_so:
            continue
        seen_so.add(so)
        uids = _search_ready_for_dispatch_uids(imap, so, since, f"ready_so/{po}")
        candidate_uids = []
        if uids:
            msgs = imap.fetch_safe(uids[:20], ["ENVELOPE", "BODY[TEXT]"])
            for uid, data in msgs.items():
                env = data.get(b"ENVELOPE")
                if env:
                    sender_mb = (env.from_[0].mailbox or b"").decode(errors="replace") if env.from_ else ""
                    sender_host = (env.from_[0].host or b"").decode(errors="replace") if env.from_ else ""
                    if "salesorder@flexitallic.eu" in f"{sender_mb}@{sender_host}".lower():
                        continue
                body_raw = data.get(b"BODY[TEXT]") or b""
                body_text = body_raw.decode(errors="replace") if isinstance(body_raw, bytes) else str(body_raw)
                if _is_ready_for_dispatch_body(body_text):
                    candidate_uids.append(uid)
        hits = _envelope_summary(imap, candidate_uids)
        classification = _classify(hits)
        if classification == MISS and hits and hits[0].get("date_iso"):
            if _fix_spm_po_timestamp(db, None, so, "ready_for_dispatch_at", hits[0]["date_iso"], "ready_for_dispatch"):
                classification = FIXED
        results[f"SO:{so}"] = {"so": so, "classification": classification, "hits": hits}

    return results


def _unicorn_uids_for_so(imap, so: str, since: str, label: str) -> list:
    """Search Unicorn/shipping-company emails mentioning SO. Tries BODY then SUBJECT."""
    uids = _search(imap, ["FROM", "unicornsl", "BODY", so, "SINCE", since], label=f"{label}/body")
    if not uids:
        uids = _search(imap, ["FROM", "unicorn", "BODY", so, "SINCE", since], label=f"{label}/body2")
    if not uids:
        uids = _search(imap, ["FROM", "unicornsl", "SUBJECT", so, "SINCE", since], label=f"{label}/subj")
    return uids


def audit_dispatched(db, imap, since) -> dict:
    """
    dispatched_at on orders (stamped via spm_purchase_orders).
    Evidence: FROM unicornsl (or similar shipper), SO number in BODY or SUBJECT.
    Two passes: via spm_purchase_orders, then via orders.so_number directly.
    """
    results = {}

    # ── Pass 1: via spm_purchase_orders ───────────────────────────────────────
    spm_pos = (
        db.table("spm_purchase_orders")
        .select("id, spm_po_ref, so_number, dispatched_at")
        .is_("dispatched_at", "null")
        .not_.is_("so_number", "null")
        .execute()
    )
    for spm in spm_pos.data:
        ref = spm["spm_po_ref"]
        so = spm["so_number"]
        if not so:
            continue
        uids = _unicorn_uids_for_so(imap, so, since, f"dispatched/{ref}")
        hits = _envelope_summary(imap, uids)
        classification = _classify(hits)
        if classification == MISS and hits and hits[0].get("date_iso"):
            if _fix_spm_po_timestamp(db, ref, so, "dispatched_at", hits[0]["date_iso"], "dispatched"):
                classification = FIXED
        results[ref] = {"so": so, "classification": classification, "hits": hits}

    # ── Pass 2: orders with so_number set not caught above ────────────────────
    orders_with_so = (
        db.table("orders")
        .select("id, buyer_po_number, so_number")
        .is_("dispatched_at", "null")
        .not_.is_("so_number", "null")
        .execute()
    )
    seen_so = {v.get("so") for v in results.values() if v.get("so")}
    for row in orders_with_so.data:
        so = row["so_number"]
        po = row["buyer_po_number"]
        if not so or so in seen_so:
            continue
        seen_so.add(so)
        uids = _unicorn_uids_for_so(imap, so, since, f"dispatched_so/{po}")
        hits = _envelope_summary(imap, uids)
        classification = _classify(hits)
        if classification == MISS and hits and hits[0].get("date_iso"):
            if _fix_spm_po_timestamp(db, None, so, "dispatched_at", hits[0]["date_iso"], "dispatched"):
                classification = FIXED
        results[f"SO:{so}"] = {"so": so, "classification": classification, "hits": hits}

    return results


def audit_delivery_requested(db, imap, since) -> dict:
    """
    delivery_requested_at on orders.
    Evidence: FROM spmwarehouse22, SUBJECT contains 'request' + 'deliv', BODY buyer_po_number.
    """
    rows = (
        db.table("orders")
        .select("id, buyer_po_number, delivery_requested_at, overall_status")
        .is_("delivery_requested_at", "null")
        .not_.is_("spm_po_sent_at", "null")   # only check orders that reached SPM PO stage
        .execute()
    )
    results = {}
    for row in rows.data:
        po = row["buyer_po_number"]
        if not po:
            continue
        uids = _search(imap, ["FROM", WAREHOUSE_EMAIL, "SUBJECT", "delivery", "BODY", po, "SINCE", since], label=f"deliv_req/{po}")
        hits = _envelope_summary(imap, uids)
        classification = _classify(hits)
        if classification == MISS and hits and hits[0].get("date_iso"):
            if _fix_order_timestamp(db, po, "delivery_requested_at", hits[0]["date_iso"], "delivery_requested"):
                classification = FIXED
        results[po] = {"classification": classification, "hits": hits}
    return results


def audit_delivered(db, imap, since) -> dict:
    """
    delivered_at on orders.
    Evidence: FROM spmwarehouse22, BODY buyer_po_number, BODY 'delivered'/'completely'.
    Only checks orders at delivery_requested or later (ignores ones not yet at that stage).
    """
    rows = (
        db.table("orders")
        .select("id, buyer_po_number, delivered_at, overall_status")
        .is_("delivered_at", "null")
        .in_("overall_status", ["delivery_requested", "dispatched", "ready_for_dispatch", "so_sent_to_warehouse"])
        .execute()
    )
    results = {}
    for row in rows.data:
        po = row["buyer_po_number"]
        if not po or po in results:
            continue  # skip duplicate PO rows
        uids = _search(imap, ["FROM", WAREHOUSE_EMAIL, "BODY", po, "SINCE", since], label=f"delivered/{po}")
        # Filter to emails with delivery-confirmation language (avoid "completely out of stock")
        candidate_uids = []
        if uids:
            msgs = imap.fetch_safe(uids[:10], ["BODY[TEXT]"])
            for uid, data in msgs.items():
                body_raw = data.get(b"BODY[TEXT]") or b""
                body_text = body_raw.decode(errors="replace").lower() if isinstance(body_raw, bytes) else str(body_raw).lower()
                if ("completely delivered" in body_text or "completely deliver" in body_text
                        or "fully delivered" in body_text or "waybill for" in body_text
                        or "waybill item" in body_text):
                    candidate_uids.append(uid)
        hits = _envelope_summary(imap, candidate_uids)
        classification = _classify(hits)
        if classification == MISS and hits and hits[0].get("date_iso"):
            if _fix_order_timestamp(db, po, "delivered_at", hits[0]["date_iso"], "delivered"):
                classification = FIXED
        results[po] = {"classification": classification, "hits": hits}
    return results


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Audit null pipeline timestamps")
    parser.add_argument("--stage", default="all",
                        choices=["all", "sent_to_wh", "stock_check", "sc_raw",
                                 "spm_sent", "spm_po_num", "so_ack", "dispatch_rdy", "dispatch_ins",
                                 "ready_dispatch", "dispatched", "warehouse",
                                 "delivery_req", "delivered"],
                        help="Which stage to audit (default: all)")
    args = parser.parse_args()

    db = get_client()
    since = sync._backfill_since()

    print(f"Gap Auditor - checking nulls since {since}")
    print(f"Stage filter: {args.stage}")
    print()

    imap = _ReconnectingIMAP()
    try:
        totals = {"PARSER MISS": 0, "REAL NULL": 0, "FIXED": 0}

        if args.stage in ("all", "sent_to_wh"):
            print("=" * 60)
            print("STAGE: sent_to_warehouse_at (did SPM route PO to warehouse?)")
            print("=" * 60)
            r = audit_sent_to_warehouse(db, imap, since)
            for po, v in sorted(r.items()):
                _print_result("sent_to_wh", po, f"PO={po}", v["classification"], v["hits"])
                totals[v["classification"]] += 1
            _print_subtotal(r)
            print()

        if args.stage in ("all", "stock_check"):
            print("=" * 60)
            print("STAGE: stock_check_completed_at (did warehouse reply with stock status?)")
            print("=" * 60)
            r = audit_stock_check(db, imap, since)
            for po, v in sorted(r.items()):
                _print_result("stock_check", po, f"PO={po}", v["classification"], v["hits"])
                totals[v["classification"]] += 1
            _print_subtotal(r)
            print()

        if args.stage in ("all", "sc_raw"):
            print("=" * 60)
            print("STAGE: stock_check_raw (timestamp set but body unparsed?)")
            print("=" * 60)
            r = audit_stock_check_raw(db, imap, since)
            for po, v in sorted(r.items()):
                _print_result("sc_raw", po, f"PO={po}", v["classification"], v["hits"])
                totals[v["classification"]] += 1
            _print_subtotal(r)
            print()

        if args.stage in ("all", "spm_sent"):
            print("=" * 60)
            print("STAGE: spm_po_sent_at (was an SPM PO emailed for this Chevron PO?)")
            print("=" * 60)
            r = audit_spm_sent(db, imap, since)
            for po, v in sorted(r.items()):
                _print_result("spm_sent", po, f"PO={po}", v["classification"], v["hits"])
                totals[v["classification"]] += 1
            _print_subtotal(r)
            print()

        if args.stage in ("all", "spm_po_num"):
            print("=" * 60)
            print("STAGE: spm_po_number (is the SPM PO number string populated on the order?)")
            print("=" * 60)
            r = audit_spm_po_number(db, imap, since)
            for po, v in sorted(r.items()):
                note = v.get("note", "")
                _print_result("spm_po_num", po, f"PO={po} {note}".strip(), v["classification"], v["hits"])
                totals[v["classification"]] += 1
            _print_subtotal(r)
            print()

        if args.stage in ("all", "so_ack"):
            print("=" * 60)
            print("STAGE: so_received_at (did Flexitallic send a sales acknowledgement?)")
            print("=" * 60)
            r = audit_so_ack(db, imap, since)
            for ref, v in sorted(r.items()):
                _print_result("so_ack", ref, f"SPM_REF={ref}", v["classification"], v["hits"])
                totals[v["classification"]] += 1
            _print_subtotal(r)
            print()

        if args.stage in ("all", "dispatch_rdy"):
            print("=" * 60)
            print("STAGE: flex_dispatch_ready_at (did Flexitallic say 'packed & ready'?)")
            print("=" * 60)
            r = audit_dispatch_ready(db, imap, since)
            for ref, v in sorted(r.items()):
                _print_result("dispatch_rdy", ref, f"SPM={ref} SO={v.get('so')}", v["classification"], v["hits"])
                totals[v["classification"]] += 1
            _print_subtotal(r)
            print()

        if args.stage in ("all", "dispatch_ins"):
            print("=" * 60)
            print("STAGE: dispatch_instructions_sent_at (did we reply with dispatch instructions?)")
            print("=" * 60)
            r = audit_dispatch_instructions(db, imap, since)
            for ref, v in sorted(r.items()):
                _print_result("dispatch_ins", ref, f"SPM={ref} SO={v.get('so')}", v["classification"], v["hits"])
                totals[v["classification"]] += 1
            _print_subtotal(r)
            print()

        if args.stage in ("all", "ready_dispatch"):
            print("=" * 60)
            print("STAGE: ready_for_dispatch_at (did Penny confirm collection arranged?)")
            print("=" * 60)
            r = audit_ready_for_dispatch(db, imap, since)
            for ref, v in sorted(r.items()):
                _print_result("ready_dispatch", ref, f"SPM={ref} SO={v.get('so')}", v["classification"], v["hits"])
                totals[v["classification"]] += 1
            _print_subtotal(r)
            print()

        if args.stage in ("all", "dispatched"):
            print("=" * 60)
            print("STAGE: dispatched_at (did shipping company reply 'Noted'?)")
            print("=" * 60)
            r = audit_dispatched(db, imap, since)
            for ref, v in sorted(r.items()):
                _print_result("dispatched", ref, f"SPM={ref} SO={v.get('so')}", v["classification"], v["hits"])
                totals[v["classification"]] += 1
            _print_subtotal(r)
            print()

        if args.stage in ("all", "warehouse"):
            print("=" * 60)
            print("STAGE: so_sent_to_warehouse_at (did we forward to warehouse?)")
            print("=" * 60)
            r = audit_warehouse(db, imap, since)
            for po, v in sorted(r.items()):
                note = v.get("note", "")
                _print_result("warehouse", po, f"PO={po} SO={v.get('so')} {note}", v["classification"], v["hits"])
                totals[v["classification"]] += 1
            _print_subtotal(r)
            print()

        if args.stage in ("all", "delivery_req"):
            print("=" * 60)
            print("STAGE: delivery_requested_at (did warehouse send REQUEST FOR DELIVERY?)")
            print("=" * 60)
            r = audit_delivery_requested(db, imap, since)
            for po, v in sorted(r.items()):
                _print_result("delivery_req", po, f"PO={po}", v["classification"], v["hits"])
                totals[v["classification"]] += 1
            _print_subtotal(r)
            print()

        if args.stage in ("all", "delivered"):
            print("=" * 60)
            print("STAGE: delivered_at (did warehouse confirm completely delivered?)")
            print("=" * 60)
            r = audit_delivered(db, imap, since)
            for po, v in sorted(r.items()):
                _print_result("delivered", po, f"PO={po}", v["classification"], v["hits"])
                totals[v["classification"]] += 1
            _print_subtotal(r)
            print()

        print("=" * 60)
        print(f"TOTAL  Parser misses: {totals[MISS]}   Real nulls: {totals[REAL]}   Auto-fixed: {totals[FIXED]}")
        print("=" * 60)
    finally:
        imap.logout()


if __name__ == "__main__":
    main()

"""
warehouse_reply_parser.py — Stage 8: Catch warehouse stock-check replies.

Monitors the Gmail inbox for replies from the warehouse (spmwarehouse22@gmail.com)
and stamps stock_check_completed_at immediately (no Claude needed).

Interpretation strategy:
  1. Keyword matching first (free, handles ~80% of cases)
  2. Claude fallback only for ambiguous/partial replies

Run this with:  python scripts/warehouse_reply_parser.py
Press Ctrl+C to stop.
"""

import gc
import os
import sys
import re
import json
import time
import email
from datetime import datetime
from dotenv import load_dotenv
from imapclient import IMAPClient
import anthropic

from db import get_client, normalize_po_number, base_po_number
import sync
from config import WAREHOUSE_EMAIL as WAREHOUSE_SENDER
from config import IMAP_TIMEOUT
from email_utils import decode_mime_words, get_email_body_text, is_already_processed

# Windows' default console codepage (cp1252) can't encode the emoji and
# em-dash characters used in this script's status output, which would crash
# the parser the moment it hits a new email. Force UTF-8 on stdout/stderr.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

load_dotenv()

_anthropic = None
def _get_claude():
    global _anthropic
    if _anthropic is None:
        _anthropic = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _anthropic


# ─────────────────────────────────────────────
# KEYWORD INTERPRETATION (free — primary)
# ─────────────────────────────────────────────

FOLLOWUP_SIGNALS = [
    "warm reminder",
    "good morning",
    "good day",
    "please a warm",
    "booking",
]
# "request for delivery" is intentionally NOT here — it's handled as its own
# pipeline event (delivery_requested) in _process_delivery_request() below.

def is_followup_email(text: str) -> bool:
    """Returns True if this is a delivery request/reminder, not a stock check."""
    t = text.lower()
    return any(s in t for s in FOLLOWUP_SIGNALS)


# Matches all "not in stock" variants — used to strip them before testing for
# plain "in stock" so the two don't collide (one is a substring of the other).
_OUT_PAT = re.compile(r'not\s+in\s*stock|not\s+instock|out\s+of\s+stock|no\s+stock')

# Same collision, other direction: the delivered/transit checks below are plain
# substring tests, so "not completely delivered" contains "completely delivered"
# and reads as its opposite. This catches a negation appearing shortly before a
# positive indicator. The [^.!?;] window stops it spanning two sentences, so
# "Item 1 not in stock. Completely delivered" is unaffected, and "not in stock"
# never matches because stock wording is deliberately absent from the
# alternatives — that path already works and must keep working.
_NEGATED_POSITIVE_RE = re.compile(
    r"\b(?:not|isn'?t|aren'?t|wasn'?t|hasn'?t|haven'?t|won'?t)\b"
    r"[^.!?;]{0,40}?"
    r"(?:completely\s+deliver|in\s+transit|in\s+chevron|awaiting\s+to\s+be\s+received)",
    re.IGNORECASE,
)

def interpret_reply_simple(body_text: str) -> dict | None:
    """
    Try to interpret a warehouse reply using keyword matching alone.
    Returns a result dict if confident, or None if Claude is needed.
    """
    text = body_text.lower().strip()

    # ── Negated positives — must be tested FIRST ──────────────────
    # The keyword checks below are plain substring tests, so "not completely
    # delivered" contains "completely delivered" and would be read as its own
    # opposite — stamping an order delivered when it explicitly isn't. The
    # PARTIAL_PHRASES guard further down already covers "not completely", but
    # it sits after the delivered/transit returns and so was never reached.
    # Returning None here routes the reply to Claude, which is exactly what
    # that guard was written to do for ambiguous wording.
    if _NEGATED_POSITIVE_RE.search(text):
        return None

    # ── Completely delivered ──────────────────────────────────────
    if any(w in text for w in [
        "completely delivered", "completely deliver",
        "waybill for", "waybill item",
    ]):
        return {
            "overall_availability": "fully_delivered",
            "items_in_stock": None,
            "items_not_in_stock": None,
            "quantity_notes": None,
            "material_spec_notes": None,
            "needs_human_review": False,
            "confidence": "high",
            "summary": "Completely delivered",
            "interpretation_method": "keyword",
        }

    # ── Material in transit / awaiting receipt ────────────────────
    if any(w in text for w in [
        "in chevron awaiting", "awaiting to be received",
        "in transit", "currently in chevron",
    ]):
        return {
            "overall_availability": "in_transit",
            "items_in_stock": None,
            "items_not_in_stock": None,
            "quantity_notes": None,
            "material_spec_notes": None,
            "needs_human_review": False,
            "confidence": "high",
            "summary": "Material in Chevron awaiting receipt",
            "interpretation_method": "keyword",
        }

    # ── Phrases that always mean partial/mixed — send to Claude ───
    PARTIAL_PHRASES = [
        "not completely", "not complete", "partially in stock",
        "some items", "not all items",
    ]
    if any(p in text for p in PARTIAL_PHRASES):
        return None

    # ── Stock keyword detection ────────────────────────────────────
    # Key: "in stock" is a substring of "not in stock". Strip all "not in stock"
    # phrases first, then check what's left for a positive "in stock" signal.
    has_out = bool(_OUT_PAT.search(text))
    text_without_out = _OUT_PAT.sub("", text)
    IN_STOCK_WORDS = [
        "in stock", "instock", "in-stock", "rfd",
        "ready for dispatch", "ready for collection",
    ]
    has_in = any(w in text_without_out for w in IN_STOCK_WORDS)

    # Both "in stock" and "not in stock" present → mixed/partial → Claude
    if has_in and has_out:
        return None

    # ── Fully in stock ────────────────────────────────────────────
    if has_in:
        has_rfd = "rfd" in text
        return {
            "overall_availability": "fully_in_stock",
            "items_in_stock": None,
            "items_not_in_stock": None,
            "quantity_notes": None,
            "material_spec_notes": None,
            "needs_human_review": False,
            "confidence": "high",
            "summary": "PO in stock and ready for dispatch" if has_rfd else "PO in stock",
            "interpretation_method": "keyword",
        }

    # ── Fully out of stock ────────────────────────────────────────
    if has_out:
        return {
            "overall_availability": "fully_out_of_stock",
            "items_in_stock": None,
            "items_not_in_stock": None,
            "quantity_notes": None,
            "material_spec_notes": None,
            "needs_human_review": False,
            "confidence": "high",
            "summary": "PO not in stock",
            "interpretation_method": "keyword",
        }

    # ── Anything else unclear — Claude ───────────────────────────
    return None


# ─────────────────────────────────────────────
# CLAUDE INTERPRETATION (fallback)
# ─────────────────────────────────────────────

STOCK_REPLY_PROMPT = """You are reading an informally-written email exchanged between a \
supplier (Special Piping Materials) and its warehouse team about a purchase order. These \
emails do NOT follow a fixed format — sometimes one line, sometimes referencing item \
numbers, material specs or partial quantities in a sentence.

FIRST work out what kind of email this actually is. Do not assume it is a stock reply.
This is reached as a fallback after simple keyword matching failed, which often means the
email is not the kind that was expected. Saying so is genuinely useful — it is far better
to report the wrong kind than to force an answer that does not fit.

  stock_reply          the warehouse saying what is or is not in stock
  delivery_request     asking for delivery to be scheduled or approved, often listing
                       PO and line items ("REQUEST FOR DELIVERY", "Po is RFD")
  delivery_confirmation someone confirming goods were actually delivered or received
  date_extension       asking to push a required delivery date BACK — the opposite of
                       requesting delivery; never treat this as a delivery request
  followup             a chase or reminder with no new status ("any update?")
  other                anything else

Only fill the stock fields when email_kind is "stock_reply". For every other kind set
overall_availability to "unclear" and leave the stock fields null — a delivery request
says nothing about stock, and guessing at one corrupts the order's status.

Email body:
---
{body}
---

Respond with ONLY valid JSON, no other text, no markdown fences:
{{
  "email_kind": "<one of: stock_reply | delivery_request | delivery_confirmation | \
date_extension | followup | other>",
  "kind_reason": "<one short phrase quoting what in the email decided the kind>",
  "overall_availability": "<one of: fully_in_stock | fully_out_of_stock | partial | \
fully_delivered | in_transit | unclear>",
  "items_in_stock": "<free text describing which items/specs are in stock, or null>",
  "items_not_in_stock": "<free text describing which items/specs are NOT in stock, or null>",
  "quantity_notes": "<any partial quantity detail, e.g. 'item 5 needs 80pcs, only 40pcs available', or null>",
  "material_spec_notes": "<any material/spec detail, e.g. '316 CGI in stock instead of 304', or null>",
  "needs_human_review": <true if any nuance, partial fulfillment, or ambiguity — else false>,
  "confidence": "<high or low>",
  "summary": "<one sentence summary>"
}}
"""


def interpret_reply_with_claude(body_text: str) -> dict:
    """Claude fallback for ambiguous/partial replies."""
    client = _get_claude()

    prompt = STOCK_REPLY_PROMPT.format(body=body_text[:3000])

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",  # cheapest — sufficient for short texts
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text.strip()
    if response_text.startswith("```"):
        response_text = response_text.split("```")[1]
        if response_text.startswith("json"):
            response_text = response_text[4:]

    data = json.loads(response_text.strip())
    data["interpretation_method"] = "claude_fallback"
    return data


def interpret_reply(body_text: str) -> tuple[dict, str]:
    """
    Route to keyword matching first, Claude only if needed.
    Returns (result_dict, method_used).
    """
    # Skip follow-up/reminder emails entirely
    if is_followup_email(body_text):
        return {
            "overall_availability": "followup",
            "items_in_stock": None,
            "items_not_in_stock": None,
            "quantity_notes": None,
            "material_spec_notes": None,
            "needs_human_review": False,
            "confidence": "high",
            "summary": "Delivery request/reminder — not a stock check reply",
            "interpretation_method": "keyword",
        }, "keyword"

    # Try keyword matching first
    result = interpret_reply_simple(body_text)
    if result is not None:
        return result, "keyword"

    # Fall back to Claude
    try:
        result = interpret_reply_with_claude(body_text)
        return result, "claude_fallback"
    except Exception:
        # API unavailable or no credits — store raw body silently, no error message
        return {
            "overall_availability": "unclear",
            "items_in_stock": None,
            "items_not_in_stock": None,
            "quantity_notes": None,
            "material_spec_notes": None,
            "needs_human_review": True,
            "confidence": "low",
            "summary": "Warehouse reply received — see raw body",
            "raw_body": body_text.strip(),
            "interpretation_method": "raw_fallback",
        }, "raw_fallback"


# ─────────────────────────────────────────────
# DB HELPERS
# ─────────────────────────────────────────────

def find_order_by_po_number(po_number: str) -> dict | None:
    # Normalise first, then strip any revision suffix — change orders are
    # stored under their base PO number, not as separate rows.
    client = get_client()
    po_number = base_po_number(normalize_po_number(po_number) or po_number) or po_number
    result = (
        client.table("orders")
        .select("*")
        .eq("buyer_po_number", po_number)
        .order("created_at", desc=False)
        .execute()
    )
    return result.data[0] if result.data else None


def save_stock_check_result(order_id: str, result: dict, email_date: str = None) -> None:
    """Write interpreted stock check result to the order."""
    client = get_client()
    availability = result.get("overall_availability", "unclear")

    # Map availability to overall_status (resolved below for fully_delivered)
    if availability == "followup":
        new_status = None  # not a stock check — status unchanged
    elif result.get("needs_human_review") or result.get("confidence") == "low":
        new_status = "stock_check_needs_review"
    else:
        new_status = "stock_check_complete"

    # Always write the interpreted result to stock_check_raw.
    client.table("orders").update({"stock_check_raw": result}).eq("id", order_id).execute()

    # Stamp delivered_at only when two guards both pass:
    #   1. Pipeline order guard — delivery_requested_at must already be set.
    #      "Completely delivered" from the warehouse can mean items arrived at
    #      the warehouse FROM the supplier, not delivery to the client site.
    #      A prior delivery request proves this is a genuine client delivery.
    #   2. Null guard — never overwrite an existing delivered_at.
    if availability == "fully_delivered" and email_date:
        order_row = client.table("orders").select(
            "delivery_requested_at"
        ).eq("id", order_id).execute()
        has_request = bool(
            order_row.data and order_row.data[0].get("delivery_requested_at")
        )
        if has_request:
            client.table("orders").update({
                "delivered_at": email_date,
            }).eq("id", order_id).is_("delivered_at", "null").execute()
            new_status = "delivered"
        else:
            print(
                f"  ⚠️  warehouse said 'completely delivered' but no delivery "
                f"request on record — treating as stock check, not delivery"
            )
            new_status = "stock_check_complete"

    # Monotonic: a re-interpreted reply must not regress a later stage.
    if new_status:
        sync.advance_status(client, order_id, new_status)


# ─────────────────────────────────────────────
# REQUEST FOR DELIVERY handler (multi-PO)
# ─────────────────────────────────────────────

def _process_delivery_request(client_db, message_id, sender, subject, email_date,
                               body_text) -> None:
    """
    Handle a warehouse 'REQUEST FOR DELIVERY' email that lists multiple Chevron
    PO numbers in the subject and body.  Stamps delivery_requested_at and
    advances each matched order to 'delivery_requested'.
    """
    all_text = subject + " " + body_text
    po_numbers = sync.extract_all_po_numbers(all_text)

    if not po_numbers:
        client_db.table("processed_emails").upsert({
            "message_id": message_id,
            "sender": sender,
            "subject": subject,
            "processing_result": "error",
            "raw_notes": "REQUEST FOR DELIVERY but no Chevron PO numbers found",
        }, on_conflict="message_id").execute()
        print(f"⚠️  REQUEST FOR DELIVERY with no parseable PO numbers: '{subject[:60]}'")
        return

    stamped = 0
    matched_ids = []
    for po in po_numbers:
        order = find_order_by_po_number(po)
        if not order:
            continue
        matched_ids.append(order["id"])
        get_client().table("orders").update({
            "delivery_requested_at": email_date,
        }).eq("id", order["id"]).is_("delivery_requested_at", "null").execute()
        sync.advance_status(get_client(), order["id"], "delivery_requested")
        stamped += 1

    client_db.table("processed_emails").upsert({
        "message_id": message_id,
        "sender": sender,
        "subject": subject,
        "processing_result": "delivery_requested",
        "raw_notes": (
            f"REQUEST FOR DELIVERY: {len(po_numbers)} PO(s) found, "
            f"{stamped} order(s) stamped delivery_requested"
        ),
    }, on_conflict="message_id").execute()

    print(f"📋 REQUEST FOR DELIVERY {email_date[:10]}: "
          f"{stamped}/{len(po_numbers)} PO(s) → delivery_requested")


# ─────────────────────────────────────────────
# MESSAGE PROCESSOR
# ─────────────────────────────────────────────

def process_message(client_db, msg_data: dict) -> None:
    raw_email = msg_data[b"RFC822"]
    msg = email.message_from_bytes(raw_email)

    message_id = msg.get("Message-ID", "")
    if not message_id:
        message_id = f"{msg.get('From')}-{msg.get('Date')}-{msg.get('Subject')}"

    if is_already_processed(message_id):
        return

    sender = decode_mime_words(msg.get("From", ""))
    subject = decode_mime_words(msg.get("Subject", ""))

    if WAREHOUSE_SENDER.lower() not in sender.lower():
        return

    email_date = sync.parse_email_date(msg)
    body_text = get_email_body_text(msg)

    # ── REQUEST FOR DELIVERY — multi-PO bulk email ────────────────
    # Match both "REQUEST FOR DELIVERY" and "REQUESTED FOR DELIVERY".
    # The previous pattern was `requested?`, which reads as "requeste" plus an
    # optional "d" — so it matched REQUESTED but never REQUEST, the form the
    # warehouse actually uses in 23 of 27 real emails. Those fell through to
    # the single-PO stock-check handler below and were silently misfiled as
    # stock replies.
    #
    # The exclusion is essential, not cosmetic: "Request for Delivery Date
    # Extension" asks to push a deadline BACK — the opposite of asking for
    # dispatch. The old typo excluded those by accident (they read "Request",
    # not "Requested"); correcting the typo removes that accidental cover, so
    # the guard has to be explicit. gmail_ack_listener already does this.
    if (re.search(r"request(?:ed)?\s+for\s+delivery", subject, re.IGNORECASE)
            and not re.search(r"delivery\s+(?:due\s+)?date|date\s+ext", subject, re.IGNORECASE)):
        _process_delivery_request(client_db, message_id, sender, subject,
                                   email_date, body_text)
        return

    # Second opinion before treating this as a stock reply.
    # The subject match above is wording-dependent, and when it misses, the
    # email lands in the stock-check handler and is recorded as a stock status
    # it never contained — that is exactly how 0061433089 sat at "stock check"
    # while Chevron had already approved its delivery. Claude is only consulted
    # when keyword matching found nothing, so this costs nothing on the normal
    # path, and it lets the fallback correct the routing instead of inheriting
    # its mistake.
    if interpret_reply_simple(body_text) is None and not is_followup_email(body_text):
        try:
            second = interpret_reply_with_claude(body_text)
        except Exception:
            second = None
        kind = (second or {}).get("email_kind")
        if kind == "delivery_request":
            print(f"   ↩️  '{subject[:48]}' reads as a delivery request "
                  f"({(second or {}).get('kind_reason')}) — routing there, not to stock check")
            _process_delivery_request(client_db, message_id, sender, subject,
                                       email_date, body_text)
            return
        if kind == "date_extension":
            print(f"   ⏭️  '{subject[:48]}' is a delivery-date extension — not a stock reply")
            client_db.table("processed_emails").upsert({
                "message_id": message_id, "sender": sender, "subject": subject,
                "processing_result": "date_extension_request",
                "raw_notes": (second or {}).get("summary"),
            }, on_conflict="message_id").execute()
            return

    # ── Single-PO stock check / delivery reply ────────────────────

    # Check NLNG PO format first (4200XXXXXXX in "PO No. 4200083212")
    nlng_po = sync.is_nlng_po_subject(subject)
    if nlng_po:
        nlng_order = sync.find_nlng_order_by_po(nlng_po)
        if nlng_order:
            result, method = interpret_reply(body_text)
            result["raw_body"] = body_text.strip()
            availability = result.get("overall_availability", "unclear")

            if availability == "followup":
                client_db.table("processed_emails").upsert({
                    "message_id": message_id,
                    "sender": sender,
                    "subject": subject,
                    "processing_result": "followup",
                    "raw_notes": f"NLNG follow-up/reminder for PO {nlng_po} — no status change",
                }, on_conflict="message_id").execute()
                print(f"📧 NLNG PO {nlng_po} [{method}]: follow-up/reminder — skipping.")
                return

            sync.stamp_nlng_stock_check(nlng_order["id"], email_date, result)
            if availability == "fully_delivered":
                sync.stamp_nlng_delivered(nlng_order["id"], email_date)

            proc_result = "delivered" if availability == "fully_delivered" else "processed"
            client_db.table("processed_emails").upsert({
                "message_id": message_id,
                "sender": sender,
                "subject": subject,
                "processing_result": proc_result,
                "raw_notes": json.dumps({
                    "method": method,
                    "availability": availability,
                    "summary": result.get("summary"),
                }),
            }, on_conflict="message_id").execute()
            label = "✅ delivered" if availability == "fully_delivered" else "📦 stock check"
            print(f"{label} NLNG PO {nlng_po} [{method}]: {result.get('summary', '')}")
        else:
            sync.park_email(
                message_id=message_id,
                kind="nlng_warehouse_reply",
                po_number=nlng_po,
                sender=sender,
                subject=subject,
                email_date=email_date,
                body_text=body_text,
                needs_claude=False,
            )
            client_db.table("processed_emails").upsert({
                "message_id": message_id,
                "sender": sender,
                "subject": subject,
                "processing_result": "parked",
                "raw_notes": f"NLNG warehouse reply parked for PO {nlng_po}",
            }, on_conflict="message_id").execute()
            print(f"🅿️  NLNG PO {nlng_po} reply parked — will link when PO notification arrives.")
        return

    po_number = sync.extract_po_number(subject)

    if not po_number:
        client_db.table("processed_emails").upsert({
            "message_id": message_id,
            "sender": sender,
            "subject": subject,
            "processing_result": "error",
            "raw_notes": "Warehouse reply but no Chevron PO number found in subject",
        }, on_conflict="message_id").execute()
        print(f"⚠️  Warehouse reply with no parseable PO number in subject: '{subject}'")
        return

    order = find_order_by_po_number(po_number)

    if not order:
        sync.park_email(
            message_id=message_id,
            kind="warehouse_reply",
            po_number=base_po_number(po_number) or po_number,
            sender=sender,
            subject=subject,
            email_date=email_date,
            body_text=body_text,
            needs_claude=True,
        )
        client_db.table("processed_emails").upsert({
            "message_id": message_id,
            "sender": sender,
            "subject": subject,
            "processing_result": "parked",
            "raw_notes": f"Warehouse reply for PO {po_number} parked (order not created yet)",
        }, on_conflict="message_id").execute()
        print(f"🅿️  PO {po_number} reply parked — will link when its PO notification arrives.")
        return

    # ── Interpret the reply body ──────────────────────────────────
    result, method = interpret_reply(body_text)
    availability = result.get("overall_availability", "unclear")

    if availability == "followup":
        # "requested delivery date" / reminder — not a stock reply, not a RFD email.
        # Log it but don't touch the order's status or stock_check_raw.
        client_db.table("processed_emails").upsert({
            "message_id": message_id,
            "sender": sender,
            "subject": subject,
            "matched_order_id": order["id"],
            "processing_result": "followup",
            "raw_notes": "Warehouse follow-up/reminder — no status change",
        }, on_conflict="message_id").execute()
        print(f"📧 PO {po_number} [{method}]: follow-up/reminder email — skipping.")
        return

    # ── Stamp stock_check_completed_at (first reply only — never overwrite) ──
    get_client().table("orders").update({
        "stock_check_completed_at": email_date,
    }).eq("id", order["id"]).is_("stock_check_completed_at", "null").execute()

    # Always store the raw email body alongside the interpretation so the
    # web app can display it directly when the parsed summary isn't enough.
    result["raw_body"] = body_text.strip()
    save_stock_check_result(order["id"], result, email_date)

    # ── Log to processed_emails ───────────────────────────────────
    client_db.table("processed_emails").upsert({
        "message_id": message_id,
        "sender": sender,
        "subject": subject,
        "matched_order_id": order["id"],
        "processing_result": "delivered" if availability == "fully_delivered" else "processed",
        "raw_notes": json.dumps({
            "method": method,
            "availability": availability,
            "summary": result.get("summary"),
            "needs_review": result.get("needs_human_review"),
        }),
    }, on_conflict="message_id").execute()

    # ── Console output ────────────────────────────────────────────
    review_flag = " ⚠️  NEEDS REVIEW" if result.get("needs_human_review") else ""
    emoji_map = {
        "fully_in_stock":     "✅",
        "fully_out_of_stock": "❌",
        "partial":            "⚠️ ",
        "fully_delivered":    "✅",
        "in_transit":         "🚚",
        "unclear":            "❓",
    }
    emoji = emoji_map.get(availability, "❓")
    suffix = " → DELIVERED" if availability == "fully_delivered" else ""
    print(
        f"{emoji} PO {po_number} [{method}]: "
        f"{result.get('summary', availability)}{review_flag}{suffix}"
    )
    if result.get("quantity_notes"):
        print(f"   Quantity: {result['quantity_notes']}")
    if result.get("material_spec_notes"):
        print(f"   Material: {result['material_spec_notes']}")


# ─────────────────────────────────────────────
# IMAP + MAIN LOOP
# ─────────────────────────────────────────────

def connect_to_gmail() -> IMAPClient:
    host = os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com")
    port = int(os.environ.get("GMAIL_IMAP_PORT", 993))
    email_addr = os.environ["GMAIL_EMAIL"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]

    # timeout: without it a half-open socket blocks this thread forever, and
    # worker.py's is_alive() check cannot tell that apart from healthy work.
    client = IMAPClient(host, port=port, use_uid=True, ssl=True, timeout=IMAP_TIMEOUT)
    client.login(email_addr, app_password)
    client.select_folder("INBOX")
    return client


def check_inbox_once() -> None:
    """Run a single pass: connect, check for relevant mail, disconnect."""
    print("   Checking Gmail inbox for warehouse replies...", end="", flush=True)
    client_db = get_client()
    imap = connect_to_gmail()

    try:
        folder = "INBOX"
        new_ids, uidvalidity = sync.new_uids_since_cursor(
            imap, "gmail", folder, ["FROM", "spmwarehouse22@gmail.com"]
        )

        if not new_ids:
            print(" (no new warehouse replies)")
            return

        print(f" {len(new_ids)} new reply(ies)...")
        highest_done = sync.get_cursor("gmail", folder)["last_uid"]

        FETCH_BATCH = 100
        for i in range(0, len(new_ids), FETCH_BATCH):
            chunk = new_ids[i: i + FETCH_BATCH]
            messages = imap.fetch(chunk, ["RFC822", "INTERNALDATE"])
            for uid in chunk:
                if uid not in messages:
                    print(f"⚠️  UID {uid} returned no data — skipped "
                          f"(daily re-scan will re-check it)", flush=True)
                    continue
                # Isolate each message: an unhandled error here would escape
                # before the cursor is saved below, so the next poll would
                # refetch this same message and fail identically — forever.
                try:
                    process_message(client_db, messages[uid])
                except Exception as exc:
                    sync.record_processing_failure(
                        messages[uid], exc, source="warehouse_reply_parser"
                    )
            # Only advance past UIDs old enough that Gmail's search index has
            # certainly caught up on them — advancing on a fresh batch can
            # carry the cursor past a message the search hasn't returned yet,
            # orphaning it permanently (see sync.safe_cursor_uid).
            safe_uid = sync.safe_cursor_uid(messages, chunk)
            if safe_uid is not None:
                highest_done = max(highest_done, safe_uid)
                sync.set_cursor("gmail", folder, highest_done, uidvalidity)
            print(f"   ...processed {min(i + FETCH_BATCH, len(new_ids))}/{len(new_ids)}")

    finally:
        imap.logout()


_RESCAN_INTERVAL = 24 * 3600  # once a day
_last_rescan_ts = None


def rescan_and_catch_up(days_back: int = 7) -> None:
    """
    Safety net for the cursor path above: ignore the stored cursor entirely and
    re-check the last `days_back` days of warehouse replies. Anything already
    handled is skipped cheaply; anything the cursor raced past — or that Gmail's
    search index hadn't caught up on when the poll ran — is processed now
    instead of staying missed forever.
    """
    client_db = get_client()
    imap = connect_to_gmail()
    try:
        imap.select_folder("INBOX")
        uids = sync.rescan_recent(imap, ["FROM", WAREHOUSE_SENDER], days_back=days_back)
        if not uids:
            return
        print(f"🔁 Re-scan: checking {len(uids)} warehouse reply(ies) from the "
              f"last {days_back} day(s) for anything missed...")
        recovered = 0
        for i in range(0, len(uids), 100):
            chunk = uids[i:i + 100]
            messages = imap.fetch(chunk, ["RFC822"])
            for uid in chunk:
                msg_data = messages.get(uid)
                if not msg_data:
                    continue
                msg = email.message_from_bytes(msg_data[b"RFC822"])
                msg_id = msg.get("Message-ID", "") or \
                    f"{msg.get('From')}-{msg.get('Date')}-{msg.get('Subject')}"
                if is_already_processed(msg_id):
                    continue
                try:
                    process_message(client_db, msg_data)
                except Exception as exc:
                    sync.record_processing_failure(
                        msg_data, exc, source="warehouse_reply_parser rescan"
                    )
                # process_message can return without writing anything for mail
                # it considers irrelevant; stamp a marker so tomorrow's re-scan
                # doesn't re-examine the same message again, and again.
                sync.ensure_processed_row(
                    msg_id,
                    decode_mime_words(msg.get("From", "")),
                    decode_mime_words(msg.get("Subject", "")),
                )
                recovered += 1
        if recovered:
            print(f"🔁 Re-scan recovered {recovered} previously-missed reply(ies).")
    finally:
        imap.logout()


def run_forever() -> None:
    interval = int(os.environ.get("CHECK_INTERVAL_SECONDS", 600))
    print(
        f"📬 SPM warehouse reply parser started "
        f"(keyword primary, Claude fallback). "
        f"Checking every {interval}s. Press Ctrl+C to stop."
    )
    global _last_rescan_ts
    while True:
        try:
            check_inbox_once()
            now = datetime.now()
            if (_last_rescan_ts is None
                    or (now - _last_rescan_ts).total_seconds() >= _RESCAN_INTERVAL):
                try:
                    rescan_and_catch_up()
                except Exception as exc:
                    print(f"⚠️  Re-scan failed (non-fatal): {exc}")
                _last_rescan_ts = now
        except Exception as e:
            print(f"\n❌ Error during inbox check: {e}")
        gc.collect()
        time.sleep(interval)


if __name__ == "__main__":
    run_forever()
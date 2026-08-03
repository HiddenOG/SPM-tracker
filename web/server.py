"""
server.py — Web server for SPMprocure360 dashboard.

Serves the dashboard and proxies /api/orders to Supabase.
Credentials stay in .env (local) or Render env vars — never sent to browser.

Local:
    python web/server.py
    python web/server.py --port 9000

Railway: start command is  python web/server.py
         Railway sets the $PORT env var automatically.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import argparse
import secrets
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import bcrypt
import jwt as pyjwt

# Allow importing from scripts/
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from db import get_client
import sync
from email_utils import get_email_body_text as _get_email_body

WEB_DIR = Path(__file__).parent

# JWT config — JWT_SECRET must be set in production env vars
_JWT_SECRET = os.environ.get("JWT_SECRET", "")
_JWT_ALGO   = "HS256"
_JWT_DAYS   = 7

if not _JWT_SECRET:
    _JWT_SECRET = secrets.token_hex(32)
    print("  ⚠️  JWT_SECRET not set — using ephemeral secret (sessions won't survive restarts)")

# Login rate limiting: max 5 attempts per IP per 15-minute window
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
_RATE_LIMIT_MAX    = 5
_RATE_LIMIT_WINDOW = 900  # seconds

# Gmail email cache: keyed by "po_number:so_number", value is (fetched_at, results).
# Avoids re-opening an IMAP connection when the same story is opened multiple times
# or when the AI summary/chat endpoints re-fetch the same thread the email list just fetched.
_EMAIL_CACHE: dict[str, tuple[float, list]] = {}
_EMAIL_CACHE_TTL = 300  # seconds (5 minutes)

class _GroqRateLimit(Exception):
    """Raised when Groq returns HTTP 429 so callers can surface a clear message."""

# ── SPM business context injected into every AI call ──────────────────────────
# Gives the AI enough background to answer questions intelligently without the
# user having to explain what "GEP", "SO", or "Unicorn" means each time.
_SPM_CONTEXT = """
COMPANY BACKGROUND
==================
You are assisting the team at Special Piping Materials (SPM), a trading and
procurement company based in Nigeria. SPM acts as a middleman: it receives
purchase orders from oil and gas companies, sources the products from
manufacturers/suppliers, and arranges delivery to the buyer's site.

KEY PARTIES
===========
- SPM (specialpiping@gmail.com) — the company you are helping. They manage all
  orders, communicate with buyers and suppliers, and coordinate logistics.
- Chevron Nigeria Limited (CNL) — SPM's main buyer. Chevron sends POs
  electronically via their GEP procurement portal. Chevron PO numbers follow the
  format 0061XXXXXXXX (10-digit numbers starting with 006). A single Chevron PO
  typically covers one order of piping materials (gaskets, flange isolation kits,
  sealing products, etc.).
- NLNG (Nigeria LNG Limited) — SPM's other major buyer. NLNG PO numbers start
  with 4200 (e.g. 4200092856). NLNG POs arrive by email from enquiry@specialpipingltd.com.
- Flexitallic (salesorder@flexitallic.eu, contact: Penny Latham) — SPM's primary
  supplier for gaskets and sealing products. Flexitallic is based in Europe.
  They send a Sales Order (SO) acknowledgment with a PDF once they accept SPM's
  purchase order. Their SO numbers are formatted as SO followed by digits (e.g.
  SO718143).
  IMPORTANT: Flexitallic is NOT SPM's only supplier. SPM works with multiple
  suppliers depending on the product type and availability. Do not assume all
  goods come from Flexitallic.
- The Warehouse (spmwarehouse22@gmail.com) — SPM's storage facility. They
  handle stock checks (do we have it in stock already?) and physical receipt and
  dispatch of goods.
- Unicorn Freight / Air Freight UnicornSL (airfreight@unicornsl.co.uk, contact:
  Sheldon Rebelo) — SPM's freight forwarder. They collect packed goods from
  Flexitallic and ship them to the destination (Chevron's or NLNG's site).
- GEP — Chevron's online procurement portal. SPM must log in and manually
  acknowledge each Chevron PO on the GEP portal. GEP then generates an
  acknowledged PDF which SPM emails to Gmail as proof.

SPM REFERENCE NUMBERS
=====================
When SPM orders goods from Flexitallic on behalf of a Chevron buyer, they
create an internal SPM Purchase Order number in the format:
  S.P.M.-C.N.L.-[REF]-[CHEVRON_PO_1]-[CHEVRON_PO_2]-...
  Example: S.P.M.-C.N.L.-3094-0061412439-0061443994
The REF (e.g. 3094) is SPM's own sequential reference. One SPM PO can bundle
multiple Chevron POs together into a single order to Flexitallic.
For NLNG orders the format is: S.P.M.-NLNG-[REF]-[NLNG_PO]

FULL ORDER PIPELINE — CHEVRON
==============================
Stage 1  | pending_acknowledgment
  Chevron sends a PO notification email to SPM's Yahoo inbox. The system
  automatically creates an order record. SPM must now log into the GEP portal
  and click "Acknowledge" to formally accept the PO.

Stage 2  | acknowledged
  SPM has acknowledged the PO on GEP. GEP generates a stamped PDF. SPM emails
  this PDF to their Gmail inbox. The system detects it and stamps acknowledged_at.

Stage 3  | awaiting_warehouse_stock_check
  SPM emails the warehouse asking: do we have this item in stock? The system
  stamps sent_to_warehouse_at.

Stage 4  | stock_check_complete  (or stock_check_needs_review)
  The warehouse replies with stock availability. If their reply is clear, the
  system auto-interprets it. If ambiguous, it flags for human review.

Stage 5  | po_sent
  SPM creates and emails a Purchase Order to Flexitallic asking them to supply
  the goods. The subject contains the SPM reference number (e.g. S.P.M.-C.N.L.-3094-...).

Stage 6  | supplier_acknowledged  (awaiting_supplier_so while waiting)
  Flexitallic (Penny Latham) sends back a Sales Order (SO) acknowledgment PDF
  confirming they will supply the goods, with a promised delivery date and line
  items. The SO number (e.g. SO718143) is stamped on the order.

Stage 7  | so_sent_to_warehouse
  SPM forwards the Flexitallic SO to the warehouse so they know what to expect.

Stage 8  | dispatch_packed_awaiting_instruction
  Flexitallic emails to say the goods are packed and ready. They are waiting for
  delivery/shipping instructions from SPM.

Stage 9  | dispatch_instruction_sent
  SPM emails Unicorn Freight (Sheldon Rebelo) with collection and shipping
  instructions — where to collect from (Flexitallic's address), where to
  deliver to (the Chevron or NLNG site), and any special requirements.

Stage 10 | ready_for_dispatch
  Unicorn confirms they have received the instructions and goods are booked for
  collection. Flexitallic may also confirm the transport is arranged.

Stage 11 | dispatched
  The goods have been collected/shipped by Unicorn. A waybill or shipping note
  is usually issued.

Stage 12 | delivery_requested
  The warehouse sends a formal "REQUEST FOR DELIVERY" email, confirming goods
  are inbound and requesting final delivery to the end site.

Stage 13 | delivered
  Goods have been delivered to the buyer (Chevron/NLNG site).

Post-delivery: waybill_received → invoiced → paid → closed

FULL ORDER PIPELINE — NLNG
===========================
NLNG orders follow the same stages but without the GEP acknowledgment step
(NLNG sends POs directly by email, no portal login needed). The pipeline is:
notification_received → awaiting_warehouse_stock_check → stock_check_complete
→ po_sent → awaiting_supplier_so → supplier_acknowledged → so_sent_to_warehouse
→ dispatch_packed_awaiting_instruction → dispatch_instruction_sent
→ ready_for_dispatch → dispatched → delivered

COMMON QUESTIONS AND CONTEXT
=============================
- "Console" — Unicorn runs regular airfreight consolidation flights (consoles).
  When Sheldon asks "should I add to today's console or next week's?", he is
  asking whether to include the goods in the next available flight or wait.
  This requires an urgent response from SPM.
- "Promised date" — the delivery date Flexitallic committed to in their SO.
- "Change order" — Chevron sometimes updates a PO (e.g. quantity or price
  change). These arrive as a new notification for the same PO number with a
  suffix like -001. SPM's system overwrites the original record.
- Delays usually happen at: GEP acknowledgment (needs a human to log in),
  stock check interpretation, and freight booking with Unicorn.
- When something is "urgent", it typically means Unicorn needs a booking
  decision or Flexitallic is holding packed goods waiting for shipping instructions.
""".strip()


def _check_rate_limit(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _LOGIN_ATTEMPTS.get(ip, []) if now - t < _RATE_LIMIT_WINDOW]
    _LOGIN_ATTEMPTS[ip] = attempts
    if len(attempts) >= _RATE_LIMIT_MAX:
        return False
    _LOGIN_ATTEMPTS[ip].append(now)
    return True


def _clear_rate_limit(ip: str) -> None:
    _LOGIN_ATTEMPTS.pop(ip, None)


def _make_token(user: dict) -> str:
    payload = {
        "sub":   str(user["id"]),
        "email": user["email"],
        "role":  user["role"],
        "name":  user.get("full_name") or user["email"],
        "exp":   datetime.now(timezone.utc) + timedelta(days=_JWT_DAYS),
    }
    return pyjwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGO)


def _verify_token(token: str) -> dict | None:
    try:
        return pyjwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALGO])
    except pyjwt.PyJWTError:
        return None

NLNG_ORDER_COLS = ",".join([
    "id", "po_number", "variation_number", "document_date",
    "notification_received_at", "required_delivery_date",
    "delivery_terms", "delivery_address", "net_value", "currency",
    "contact_name", "contact_email", "enquiry_number",
    "pdf_attachment_path", "pdf_url",
    "sent_to_warehouse_at", "warehouse_routing_raw",
    "stock_check_completed_at", "stock_check_raw",
    "spm_po_number", "spm_po_sent_at",
    "so_number", "so_received_at", "so_pdf_url", "promised_date",
    "so_sent_to_warehouse_at", "flex_dispatch_ready_at",
    "dispatch_instructions_sent_at", "ready_for_dispatch_at",
    "dispatched_at", "delivered_at",
    "overall_status", "created_at",
    "nlng_order_line_items(item_no,mesc_code,description,quantity,uom,unit_price,net_amount,int_article_no,delivery_date)",
])

ORDER_COLS = ",".join([
    "id", "buyer_po_number", "po_amount", "po_currency", "notification_received_at",
    "order_submitted_on", "extracted_description", "req_number", "buyer_name",
    "pdf_url", "ack_pdf_url", "so_pdf_url",
    "required_delivery_date", "po_destination", "transportation",
    "acknowledgment_status", "acknowledged_at",
    "sent_to_warehouse_at", "stock_check_completed_at", "stock_check_raw",
    "spm_po_number", "spm_po_sent_at", "so_number", "promised_date",
    "warehouse_routing_raw",
    "so_received_at", "so_sent_to_warehouse_at", "flex_dispatch_ready_at",
    "dispatch_instructions_sent_at", "ready_for_dispatch_at", "dispatched_at",
    "delivery_requested_at", "delivered_at", "overall_status", "created_at",
    "order_line_items(line_no,description,quantity,buyer_part_code,required_delivery_date,promised_date)",
])


class _Handler(BaseHTTPRequestHandler):

    # Set per-request by _require_auth
    _current_user: dict | None = None

    def _require_auth(self) -> bool:
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            payload = _verify_token(auth_header[7:])
            if payload:
                self._current_user = payload
                return True
        self._current_user = None
        self._json_error(401, "unauthorized")
        return False

    def _require_admin(self) -> bool:
        if not self._require_auth():
            return False
        if self._current_user.get("role") != "admin":
            self._json_error(403, "admin access required")
            return False
        return True

    def _json_error(self, status: int, message: str) -> None:
        body = json.dumps({"error": message}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_ok(self, data: dict) -> None:
        body = json.dumps(data, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        # Public routes — no auth required
        if path == "/health":
            self._json_ok({"ok": True})
            return
        if path in ("/", "/index.html"):
            self._serve_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path == "/style.css":
            self._serve_file(WEB_DIR / "style.css", "text/css; charset=utf-8")
            return
        if path == "/script.js":
            self._serve_file(WEB_DIR / "script.js", "application/javascript; charset=utf-8")
            return
        if path == "/messages":
            # SAMEORIGIN (not DENY) — this page is embedded as an iframe inside index.html
            try:
                data = (WEB_DIR / "messages.html").read_bytes()
            except FileNotFoundError:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
            self.end_headers()
            self.wfile.write(data)
            return
        if not self._require_auth():
            return
        if path == "/api/orders":
            self._serve_orders()
        elif path == "/api/nlng_orders":
            self._serve_nlng_orders()
        elif path == "/api/so_line_items":
            self._serve_so_line_items()
        elif path == "/api/users":
            self._serve_users()
        elif path == "/api/messages":
            self._serve_messages()
        elif path == "/api/messages/sent":
            self._serve_sent_messages()
        elif path == "/api/messages/unread_count":
            self._serve_unread_count()
        elif path == "/api/emails":
            self._serve_emails()
        elif path == "/api/emails/summarize":
            self._serve_email_summary()
        elif path == "/api/comments":
            self._serve_comments()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/auth/login":
            self._handle_login()
            return
        if not self._require_auth():
            return
        if path == "/api/users":
            self._handle_create_user()
            return
        if path == "/api/messages":
            self._handle_send_message()
            return
        m = re.match(r"^/api/messages/([^/]+)/read$", path)
        if m:
            self._handle_mark_read(m.group(1))
            return
        if path == "/api/emails/chat":
            self._handle_email_chat()
            return
        if path == "/api/comments":
            self._handle_post_comment()
            return
        self.send_response(404)
        self.end_headers()

    def _handle_login(self) -> None:
        ip = self.client_address[0]
        if not _check_rate_limit(ip):
            self._json_error(429, "Too many login attempts — try again in 15 minutes")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            email    = str(body.get("email", "")).strip().lower()
            password = str(body.get("password", ""))
            if not email or not password:
                self._json_error(400, "email and password required")
                return
            result = get_client().table("users").select(
                "id,email,password_hash,role,full_name,is_active"
            ).eq("email", email).execute()
            if not result.data:
                self._json_error(401, "Invalid email or password")
                return
            user = result.data[0]
            if not user.get("is_active"):
                self._json_error(401, "Account is disabled — contact your administrator")
                return
            if not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
                self._json_error(401, "Invalid email or password")
                return
            get_client().table("users").update(
                {"last_login_at": datetime.now(timezone.utc).isoformat()}
            ).eq("id", user["id"]).execute()
            _clear_rate_limit(ip)
            token = _make_token(user)
            self._json_ok({
                "token": token,
                "user": {
                    "email": user["email"],
                    "role":  user["role"],
                    "name":  user.get("full_name") or user["email"],
                },
            })
        except Exception:
            self._json_error(500, "Server error — please try again")

    def _serve_file(self, fpath: Path, content_type: str) -> None:
        try:
            data = fpath.read_bytes()
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.end_headers()
        self.wfile.write(data)

    def _serve_orders(self) -> None:
        try:
            result = (
                get_client()
                .table("orders")
                .select(ORDER_COLS)
                .order("notification_received_at", desc=True)
                .execute()
            )
            orders = result.data or []

            # Embed SO line items directly into each order (mirrors how
            # order_line_items is joined via Supabase FK — so_line_items has
            # no FK to orders so we do it server-side instead).
            so_numbers = list({o["so_number"] for o in orders if o.get("so_number")})
            so_items_map: dict = {}
            if so_numbers:
                li_res = (
                    get_client()
                    .table("so_line_items")
                    .select("so_number,line_no,item_number,despatch_date,qty,uom,unit_price,extended_price")
                    .in_("so_number", so_numbers)
                    .execute()
                )
                for li in (li_res.data or []):
                    sn = li["so_number"]
                    so_items_map.setdefault(sn, []).append(li)
                for sn in so_items_map:
                    so_items_map[sn].sort(key=lambda x: int(x.get("line_no") or 0))
            for o in orders:
                o["so_line_items"] = so_items_map.get(o.get("so_number"), [])

            self._json_ok(orders)
        except Exception as exc:
            print(f"  [error] _serve_orders: {exc}")
            self._json_error(500, "Server error")

    def _serve_nlng_orders(self) -> None:
        try:
            result = (
                get_client()
                .table("nlng_orders")
                .select(NLNG_ORDER_COLS)
                .order("notification_received_at", desc=True)
                .execute()
            )
            orders = result.data or []

            # Embed SO line items by so_number (same pattern as Chevron orders)
            so_numbers = list({o["so_number"] for o in orders if o.get("so_number")})
            so_items_map: dict = {}
            if so_numbers:
                li_res = (
                    get_client()
                    .table("so_line_items")
                    .select("so_number,line_no,item_number,despatch_date,qty,uom,unit_price,extended_price")
                    .in_("so_number", so_numbers)
                    .execute()
                )
                for li in (li_res.data or []):
                    sn = li["so_number"]
                    so_items_map.setdefault(sn, []).append(li)
                for sn in so_items_map:
                    so_items_map[sn].sort(key=lambda x: int(x.get("line_no") or 0))
            for o in orders:
                o["so_line_items"] = so_items_map.get(o.get("so_number"), [])

            self._json_ok(orders)
        except Exception as exc:
            print(f"  [error] _serve_nlng_orders: {exc}")
            self._json_error(500, "Server error")

    def _serve_so_line_items(self) -> None:
        try:
            result = (
                get_client()
                .table("so_line_items")
                .select("so_number,line_no,item_number,despatch_date,qty,uom,unit_price,extended_price")
                .execute()
            )
            self._json_ok(result.data or [])
        except Exception as exc:
            print(f"  [error] _serve_so_line_items: {exc}")
            self._json_error(500, "Server error")

    def do_PATCH(self):
        if not self._require_auth():
            return
        path = self.path.split("?")[0]

        m = re.match(r"^/api/users/([^/]+)$", path)
        if m:
            self._patch_user(m.group(1))
            return

        m = re.match(r"^/api/orders/([^/]+)/req_number$", path)
        if m:
            self._patch_field("orders", m.group(1), "req_number")
            return

        m = re.match(r"^/api/nlng_orders/([^/]+)/enquiry_number$", path)
        if m:
            self._patch_field("nlng_orders", m.group(1), "enquiry_number")
            return

        m = re.match(r"^/api/comments/([^/]+)$", path)
        if m:
            self._patch_comment(m.group(1))
            return

        self.send_response(404)
        self.end_headers()

    def do_DELETE(self):
        if not self._require_auth():
            return
        path = self.path.split("?")[0]
        m = re.match(r"^/api/comments/([^/]+)$", path)
        if m:
            self._delete_comment(m.group(1))
            return
        self.send_response(404)
        self.end_headers()

    def _patch_field(self, table: str, row_id: str, field: str) -> None:
        try:
            raw_len = self.headers.get("Content-Length")
            length = int(raw_len) if raw_len is not None else 0
            body = json.loads(self.rfile.read(length) or b"{}")
            if field not in body:
                err = json.dumps({"error": f"{field} key required"}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return
            value = body[field]
            if value is not None:
                value = str(value).strip() or None

            # IDOR guard: verify the row exists before writing.
            exists = get_client().table(table).select("id").eq("id", row_id).execute()
            if not exists.data:
                err = json.dumps({"error": "not found"}).encode("utf-8")
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return

            result = get_client().table(table).update({field: value}).eq("id", row_id).execute()
            if not result.data:
                err = json.dumps({"error": "update failed — no rows affected"}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return

            out = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        except Exception as exc:
            err = json.dumps({"error": str(exc)}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)

    def _serve_messages(self) -> None:
        try:
            caller_role = self._current_user.get("role", "")
            user_id     = self._current_user.get("sub", "")
            # Admin can preview another role's inbox via ?role= param
            role = caller_role
            if caller_role == "admin":
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                req_role = qs.get("role", [None])[0]
                valid = ("admin", "procurement", "warehouse", "expeditor", "accounts")
                if req_role and req_role in valid:
                    role = req_role
            result  = get_client().table("messages").select("*").eq("to_role", role).order("created_at", desc=True).execute()
            msgs    = result.data or []
            if user_id and msgs:
                read_res = get_client().table("message_reads").select("message_id").eq("user_id", user_id).execute()
                read_ids = {r["message_id"] for r in (read_res.data or [])}
                for m in msgs:
                    m["is_read"] = m["id"] in read_ids
            self._json_ok(msgs)
        except Exception:
            self._json_error(500, "Server error")

    def _serve_sent_messages(self) -> None:
        try:
            user_id = self._current_user.get("sub", "")
            result  = get_client().table("messages").select("*").eq("from_user_id", user_id).order("created_at", desc=True).execute()
            self._json_ok(result.data or [])
        except Exception:
            self._json_error(500, "Server error")

    def _serve_unread_count(self) -> None:
        try:
            caller_role = self._current_user.get("role", "")
            user_id     = self._current_user.get("sub", "")
            role = caller_role
            if caller_role == "admin":
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                req_role = qs.get("role", [None])[0]
                valid = ("admin", "procurement", "warehouse", "expeditor", "accounts")
                if req_role and req_role in valid:
                    role = req_role
            result  = get_client().table("messages").select("id").eq("to_role", role).execute()
            all_ids = {m["id"] for m in (result.data or [])}
            if not all_ids:
                self._json_ok({"count": 0})
                return
            read_res = get_client().table("message_reads").select("message_id").eq("user_id", user_id).execute()
            read_ids = {r["message_id"] for r in (read_res.data or [])}
            self._json_ok({"count": len(all_ids - read_ids)})
        except Exception:
            self._json_error(500, "Server error")

    def _serve_emails(self) -> None:
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            order_id   = qs.get("order_id", [None])[0]
            order_type = qs.get("type",      ["chevron"])[0]
            if not order_id:
                self._json_error(400, "order_id required")
                return

            po_number = None
            so_number = ""
            try:
                if order_type == "nlng":
                    res = get_client().table("nlng_orders").select("po_number,so_number").eq("id", order_id).execute()
                    row = (res.data or [{}])[0]
                    po_number = row.get("po_number")
                    so_number = row.get("so_number") or ""
                else:
                    res = get_client().table("orders").select("buyer_po_number,so_number").eq("id", order_id).execute()
                    row = (res.data or [{}])[0]
                    po_number = row.get("buyer_po_number")
                    so_number = row.get("so_number") or ""
            except Exception:
                pass

            emails = self._fetch_gmail_emails(po_number, so_number) if po_number else []
            self._json_ok(emails)
        except Exception:
            self._json_error(500, "Server error")

    def _fetch_gmail_emails(self, po_number: str, so_number: str = "") -> list:
        """Search Gmail All Mail for this PO number and return emails directly — no DB writes.

        Results are cached for _EMAIL_CACHE_TTL seconds so opening the story and
        then immediately requesting an AI summary only opens one IMAP connection.
        """
        cache_key = f"{po_number}:{so_number}"
        cached = _EMAIL_CACHE.get(cache_key)
        if cached:
            fetched_at, data = cached
            if time.time() - fetched_at < _EMAIL_CACHE_TTL:
                return data

        try:
            import email as _eml
            from email.header import decode_header as _dh
            from imapclient import IMAPClient

            gmail_addr = os.environ.get("GMAIL_EMAIL", "")
            app_pw     = os.environ.get("GMAIL_APP_PASSWORD", "")
            spm_sender = os.environ.get("SPM_SENDER", "specialpiping@gmail.com")
            if not gmail_addr or not app_pw:
                return []

            imap = IMAPClient("imap.gmail.com", port=993, use_uid=True, ssl=True, timeout=20)
            imap.login(gmail_addr, app_pw)
            imap.select_folder("[Gmail]/All Mail", readonly=True)

            # Build a single Gmail search query covering all PO number variants
            # and the SO number (if any). One round-trip replaces what used to be
            # 3-4 separate IMAP searches.
            stripped_po = po_number.lstrip("0")
            terms = [f'"{po_number}"']
            if stripped_po and stripped_po != po_number:
                terms.append(f'"{stripped_po}"')
            if so_number:
                terms.append(f'"{so_number}"')
            raw_query = " OR ".join(terms)

            try:
                seed_uids = set(imap.search(["X-GM-RAW", raw_query]))
            except Exception:
                seed_uids = set()

            if not seed_uids:
                try: imap.logout()
                except Exception: pass
                return []

            # Expand to full threads so replies are included even when the
            # subject drifted. Build a single IMAP OR-tree search instead of
            # one search per thread — N round-trips become 1.
            thread_data = imap.fetch(list(seed_uids), ["X-GM-THRID"])
            thread_ids  = list({v[b"X-GM-THRID"] for v in thread_data.values() if b"X-GM-THRID" in v})

            def _or_tree(items):
                """Fold a list into a nested IMAP OR search tree."""
                if len(items) == 1:
                    return items[0]
                return ["OR", items[0], _or_tree(items[1:])]

            all_uids: set[int] = set(seed_uids)
            if thread_ids:
                criteria = [["X-GM-THRID", str(tid)] for tid in thread_ids]
                try:
                    all_uids.update(imap.search(_or_tree(criteria)))
                except Exception:
                    # fallback: sequential search if the OR tree fails
                    for tid in thread_ids:
                        try:
                            all_uids.update(imap.search(["X-GM-THRID", str(tid)]))
                        except Exception:
                            pass

            def _dec(s) -> str:
                if not s: return ""
                parts = _dh(s)
                out = ""
                for part, enc in parts:
                    out += part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else part
                return out

            # Batch-fetch all emails in one IMAP round-trip (no [:200] cap —
            # the old per-uid loop with [:200] cut off the newest emails).
            try:
                all_data = imap.fetch(list(all_uids), ["RFC822"])
            except Exception:
                all_data = {}

            seen_mids: set[str] = set()
            results = []
            for uid in sorted(all_uids):
                try:
                    raw = (all_data.get(uid) or {}).get(b"RFC822")
                    if not raw:
                        continue
                    msg    = _eml.message_from_bytes(raw)
                    subj   = _dec(msg.get("Subject", ""))
                    if "SPM CNL Purchase Orders" in subj:
                        continue
                    mid    = (msg.get("Message-ID") or "").strip()
                    if mid and mid in seen_mids:
                        continue
                    if mid:
                        seen_mids.add(mid)
                    body   = _get_email_body(msg)
                    sender = _dec(msg.get("From", ""))
                    results.append({
                        "message_id":   mid,
                        "direction":    "out" if spm_sender.lower() in sender.lower() else "in",
                        "from_address": sender,
                        "to_address":   _dec(msg.get("To", "")),
                        "subject":      subj,
                        "body_text":    body[:50_000],
                        "received_at":  sync.parse_email_date(msg),
                    })
                except Exception:
                    continue

            try: imap.logout()
            except Exception: pass

            results.sort(key=lambda e: e["received_at"] or "")
            _EMAIL_CACHE[cache_key] = (time.time(), results)
            return results

        except Exception as e:
            print(f"  Gmail fetch non-fatal: {po_number}: {e}")
            return []

    def _get_po_number(self, order_id: str, order_type: str) -> tuple[str, str]:
        """Returns (po_number, so_number) for building Gmail search queries."""
        try:
            if order_type == "nlng":
                res = get_client().table("nlng_orders").select("po_number,so_number").eq("id", order_id).execute()
                row = (res.data or [{}])[0]
                return row.get("po_number") or "", row.get("so_number") or ""
            else:
                res = get_client().table("orders").select("buyer_po_number,so_number").eq("id", order_id).execute()
                row = (res.data or [{}])[0]
                return row.get("buyer_po_number") or "", row.get("so_number") or ""
        except Exception:
            return "", ""

    def _serve_email_summary(self) -> None:
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            order_id   = qs.get("order_id", [None])[0]
            order_type = qs.get("type",      ["chevron"])[0]
            if not order_id:
                self._json_error(400, "order_id required")
                return
            if not os.environ.get("GROQ_API_KEY"):
                self._json_error(503, "GROQ_API_KEY not configured")
                return
            po_number, so_number = self._get_po_number(order_id, order_type)
            thread_text = self._build_email_context(po_number, so_number)
            # All slow work is done — start streaming now so the browser
            # sees the first token within ~1 second instead of waiting for
            # the full response.
            self._start_sse()
            if not thread_text:
                self._sse_chunk("No emails found for this PO yet.")
                self._sse_done()
                return
            prompt = (
                f"{_SPM_CONTEXT}\n\n"
                "Based on the company background above and the email thread below, "
                "write a SHORT summary (4-6 sentences) covering:\n"
                "- What this PO is about and which buyer it is for\n"
                "- Current pipeline stage and what has happened so far\n"
                "- Any outstanding actions or urgent items that need attention\n\n"
                f"EMAIL THREAD:\n{thread_text}"
            )
            for chunk in self._groq_stream([{"role": "user", "content": prompt}], max_tokens=300, model="llama-3.3-70b-versatile"):
                self._sse_chunk(chunk)
            self._sse_done()
        except Exception as exc:
            print(f"  [error] _serve_email_summary: {exc}")
            try:
                self._sse_done()
            except Exception:
                pass

    def _build_email_context(self, po_number: str, so_number: str = "") -> str:
        """Return a compact text representation of live Gmail emails for a PO.

        Includes ALL emails if the total formatted text fits within CHAR_BUDGET.
        If it exceeds the budget, evenly distributes sample points across the
        full thread so the AI sees the complete chronological spread — not just
        the arbitrary first/middle/last 3.
        """
        BODY_LIMIT  = 600     # chars per email body
        CHAR_BUDGET = 6_000   # total context chars before sampling kicks in

        emails = self._fetch_gmail_emails(po_number, so_number)
        n = len(emails)
        if not n:
            return ""

        def _fmt(e: dict) -> str:
            date = (e.get("received_at") or "unknown")[:16].replace("T", " ")
            dir_ = "SENT" if e.get("direction") == "out" else "RECEIVED"
            frm  = e.get("from_address") or "unknown"
            subj = e.get("subject") or "(no subject)"
            body = (e.get("body_text") or "").strip()[:BODY_LIMIT]
            return f"[{date}] {dir_} | From: {frm} | Subject: {subj}\n{body}"

        formatted = [_fmt(e) for e in emails]
        total_chars = sum(len(f) for f in formatted)

        if total_chars <= CHAR_BUDGET:
            # Full thread fits — give the AI everything
            selected = formatted
        else:
            # Evenly distribute sample points so every part of the thread
            # is represented, not just start/middle/end fixed buckets.
            avg_len = total_chars / n
            target  = max(9, int(CHAR_BUDGET / avg_len))
            target  = min(target, n)
            step    = (n - 1) / (target - 1) if target > 1 else 0
            indices = sorted(set(round(i * step) for i in range(target)))
            selected = [formatted[i] for i in indices]

        return "\n\n---\n\n".join(selected)

    def _groq_call(self, messages: list, max_tokens: int = 400) -> str:
        """Call Groq via curl subprocess. Returns the assistant message text.

        curl is used instead of Python's urllib because Cloudflare blocks
        Python's default User-Agent from the Nigerian IP used in development.
        Railway's IP does not have this issue, but curl keeps both environments
        consistent.
        """
        groq_key = os.environ.get("GROQ_API_KEY", "")
        payload = json.dumps({
            "model":       "llama-3.3-70b-versatile",
            "messages":    messages,
            "max_tokens":  max_tokens,
            "temperature": 0.4,
        })
        proc = subprocess.run(
            ["curl", "-s", "-X", "POST",
             "https://api.groq.com/openai/v1/chat/completions",
             "-H", f"Authorization: Bearer {groq_key}",
             "-H", "Content-Type: application/json",
             "-d", payload],
            capture_output=True, text=True, timeout=25,
        )
        try:
            result = json.loads(proc.stdout)
            return result["choices"][0]["message"]["content"].strip()
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            preview = proc.stdout[:200] if proc.stdout else "(no response)"
            print(f"  [error] Groq call failed: {exc} | response: {preview}")
            raise RuntimeError("AI assistant is temporarily unavailable")

    # ── SSE streaming ──────────────────────────────────────────────────────────

    def _start_sse(self) -> None:
        """Send SSE response headers and leave the connection open for token streaming."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        # Tell nginx/Railway's proxy not to buffer this response — without this
        # the proxy holds all chunks until the stream closes, defeating streaming.
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _sse_chunk(self, text: str) -> None:
        """Write one SSE data event and flush immediately so the browser sees it."""
        event = f"data: {json.dumps({'c': text})}\n\n"
        self.wfile.write(event.encode())
        self.wfile.flush()

    def _sse_done(self) -> None:
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _groq_stream(self, messages: list, max_tokens: int = 400, model: str = "llama-3.1-8b-instant"):
        """Call Groq with stream=True and yield text deltas as they arrive.

        Tries urllib first — this works on Railway (US/EU IP, no Cloudflare block).
        Falls back to curl subprocess for local dev from Nigerian IPs where
        Cloudflare blocks Python's default User-Agent.
        """
        groq_key = os.environ.get("GROQ_API_KEY", "")
        payload_dict = {
            "model":       model,
            "messages":    messages,
            "max_tokens":  max_tokens,
            "temperature": 0.4,
            "stream":      True,
        }
        payload_bytes = json.dumps(payload_dict).encode()

        def _parse_sse_line(line: str):
            if not line.startswith("data: "):
                return None
            data = line[6:]
            if data == "[DONE]":
                return StopIteration
            try:
                chunk = json.loads(data)
                return chunk["choices"][0]["delta"].get("content", "") or None
            except (json.JSONDecodeError, KeyError, IndexError):
                return None

        # ── Attempt 1: urllib (no external dependency) ──────────────────────
        try:
            import urllib.request as _urlreq
            import urllib.error  as _urlerr
            req = _urlreq.Request(
                "https://api.groq.com/openai/v1/chat/completions",
                data=payload_bytes,
                headers={
                    "Authorization":  f"Bearer {groq_key}",
                    "Content-Type":   "application/json",
                },
                method="POST",
            )
            with _urlreq.urlopen(req, timeout=30) as resp:
                buf = b""
                while True:
                    raw = resp.read(512)
                    if not raw:
                        break
                    buf += raw
                    while b"\n" in buf:
                        line_bytes, buf = buf.split(b"\n", 1)
                        result = _parse_sse_line(line_bytes.decode(errors="replace").strip())
                        if result is StopIteration:
                            return
                        if result:
                            yield result
            return  # urllib succeeded — don't fall through to curl

        except _urlerr.HTTPError as exc:
            if exc.code == 429:
                raise _GroqRateLimit("Groq rate limit (429)")
            print(f"  [groq] urllib HTTP {exc.code} — trying curl")
        except Exception as exc:
            print(f"  [groq] urllib failed ({type(exc).__name__}: {exc}) — trying curl")

        # ── Attempt 2: curl subprocess (fallback for Cloudflare-blocked IPs) ─
        proc = subprocess.Popen(
            ["curl", "-s", "-N", "-X", "POST",
             "https://api.groq.com/openai/v1/chat/completions",
             "-H", f"Authorization: Bearer {groq_key}",
             "-H", "Content-Type: application/json",
             "-d", json.dumps(payload_dict)],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            lines = proc.stdout.readlines()
        finally:
            proc.stdout.close()
            proc.wait()

        # Detect error JSON from curl (e.g. 429 body) before treating as SSE
        raw_body = "".join(lines).strip()
        if raw_body.startswith("{"):
            try:
                obj = json.loads(raw_body)
                if "error" in obj:
                    err_type = obj["error"].get("type", "")
                    err_msg  = obj["error"].get("message", "unknown")
                    if "rate_limit" in err_type:
                        raise _GroqRateLimit("Groq rate limit (curl)")
                    raise RuntimeError(f"Groq error: {err_msg}")
            except (json.JSONDecodeError, KeyError):
                pass  # not a JSON error blob — process as SSE below

        for line in lines:
            result = _parse_sse_line(line.strip())
            if result is StopIteration:
                break
            if result:
                yield result

    def _handle_email_chat(self) -> None:
        sse_started = False
        try:
            length  = int(self.headers.get("Content-Length", 0))
            body    = json.loads(self.rfile.read(length) or b"{}")
            order_id   = body.get("order_id", "")
            order_type = body.get("type", "chevron")
            messages   = body.get("messages", [])   # [{role, content}, ...]

            if not order_id or not messages:
                self._json_error(400, "order_id and messages required")
                return

            groq_key = os.environ.get("GROQ_API_KEY", "")
            if not groq_key:
                self._json_error(503, "GROQ_API_KEY not configured")
                return

            # Pull the initial AI summary out of history (first assistant message)
            # and pin it to the system prompt so it survives history trimming.
            summary_ctx = ""
            if messages and messages[0].get("role") == "assistant":
                summary_ctx = "\n\nINITIAL PO SUMMARY:\n" + messages[0]["content"]
                messages = messages[1:]

            # Keep last 10 messages (5 exchanges) to limit token burn per request
            messages = messages[-10:]

            po_number, so_number = self._get_po_number(order_id, order_type)
            thread_text = self._build_email_context(po_number, so_number)
            system_msg  = {
                "role":    "system",
                "content": (
                    f"{_SPM_CONTEXT}\n\n"
                    "You are a procurement assistant for the SPM team. "
                    "Rules:\n"
                    "- Always answer in plain, simple English. No jargon. Short sentences.\n"
                    "- If the question is about this specific order, check the EMAIL THREAD "
                    "below first before answering. Only state facts you can see in the emails.\n"
                    "- If the answer is not in the emails, say so clearly — do not guess.\n"
                    "- Keep replies short. Two or three sentences is usually enough.\n\n"
                    f"EMAIL THREAD:\n{thread_text}"
                    f"{summary_ctx}"
                ),
            }
            self._start_sse()
            sse_started = True
            for chunk in self._groq_stream([system_msg] + messages):
                self._sse_chunk(chunk)
            self._sse_done()
        except _GroqRateLimit:
            try:
                if sse_started:
                    self._sse_chunk("__RATE_LIMIT__")
                    self._sse_done()
            except Exception:
                pass
        except Exception as exc:
            print(f"  [error] _handle_email_chat: {exc}")
            try:
                if sse_started:
                    self._sse_done()
            except Exception:
                pass

    def _handle_send_message(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length) or b"{}")
            to_role = str(body.get("to_role", ""))
            subject = str(body.get("subject", "")).strip()
            msg_body = str(body.get("body", "")).strip()
            valid_roles = ("admin", "procurement", "warehouse", "expeditor", "accounts")
            if to_role not in valid_roles or not subject or not msg_body:
                self._json_error(400, "to_role, subject, and body are required")
                return
            payload = {
                "from_user_id": self._current_user.get("sub"),
                "from_name":    self._current_user.get("name") or self._current_user.get("email", ""),
                "to_role":      to_role,
                "subject":      subject,
                "body":         msg_body,
                "message_type": str(body.get("message_type", "general")),
                "from_role":    self._current_user.get("role") or None,
                "order_id":     body.get("order_id") or None,
                "order_client": body.get("order_client") or None,
                "po_pdf_url":   body.get("po_pdf_url") or None,
            }
            result = get_client().table("messages").insert(payload).execute()
            if not result.data:
                self._json_error(500, "Insert failed")
                return
            self._json_ok({"id": result.data[0]["id"]})
        except Exception:
            self._json_error(500, "Server error")

    def _handle_mark_read(self, message_id: str) -> None:
        try:
            user_id = self._current_user.get("sub", "")
            get_client().table("message_reads").upsert({
                "message_id": message_id,
                "user_id":    user_id,
            }, on_conflict="message_id,user_id").execute()
            self._json_ok({"ok": True})
        except Exception:
            self._json_error(500, "Server error")

    def _serve_users(self) -> None:
        if self._current_user.get("role") != "admin":
            self._json_error(403, "admin access required")
            return
        try:
            result = get_client().table("users").select(
                "id,email,full_name,role,is_active,created_at,last_login_at"
            ).order("created_at").execute()
            self._json_ok(result.data or [])
        except Exception:
            self._json_error(500, "Server error")

    def _handle_create_user(self) -> None:
        if self._current_user.get("role") != "admin":
            self._json_error(403, "admin access required")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length) or b"{}")
            email    = str(body.get("email", "")).strip().lower()
            password = str(body.get("password", ""))
            role     = str(body.get("role", ""))
            name     = str(body.get("full_name", "")).strip()
            valid_roles = ("admin", "procurement", "warehouse", "expeditor", "accounts")
            if not email or not password or role not in valid_roles:
                self._json_error(400, "email, password, and valid role are required")
                return
            pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
            result  = get_client().table("users").insert({
                "email":         email,
                "username":      email,
                "full_name":     name or None,
                "password_hash": pw_hash,
                "role":          role,
                "is_active":     True,
            }).execute()
            if not result.data:
                self._json_error(500, "Insert failed")
                return
            u = result.data[0]
            self._json_ok({"id": u["id"], "email": u["email"], "role": u["role"]})
        except Exception:
            self._json_error(500, "Server error")

    def _patch_user(self, user_id: str) -> None:
        if self._current_user.get("role") != "admin":
            self._json_error(403, "admin access required")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length) or b"{}")
            allowed = {"full_name", "role", "is_active", "password"}
            update: dict = {}
            valid_roles = ("admin", "procurement", "warehouse", "expeditor", "accounts")
            for key in allowed:
                if key not in body:
                    continue
                if key == "role" and body[key] not in valid_roles:
                    self._json_error(400, f"invalid role: {body[key]}")
                    return
                if key == "password":
                    update["password_hash"] = bcrypt.hashpw(
                        str(body[key]).encode(), bcrypt.gensalt(rounds=12)
                    ).decode()
                else:
                    update[key] = body[key]
            if not update:
                self._json_error(400, "nothing to update")
                return
            exists = get_client().table("users").select("id").eq("id", user_id).execute()
            if not exists.data:
                self._json_error(404, "user not found")
                return
            get_client().table("users").update(update).eq("id", user_id).execute()
            self._json_ok({"ok": True})
        except Exception:
            self._json_error(500, "Server error")

    def _serve_comments(self) -> None:
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        order_id   = (qs.get("order_id") or [""])[0].strip()
        order_type = (qs.get("type")     or ["chevron"])[0].strip()
        if not order_id:
            self._json_error(400, "order_id required")
            return
        try:
            col    = "nlng_order_id" if order_type == "nlng" else "order_id"
            result = get_client().table("po_comments").select(
                "id,author_name,author_role,body,created_at"
            ).eq(col, order_id).order("created_at", desc=False).execute()
            self._json_ok(result.data or [])
        except Exception:
            self._json_error(500, "Server error")

    def _handle_post_comment(self) -> None:
        try:
            length     = int(self.headers.get("Content-Length", 0))
            body       = json.loads(self.rfile.read(length) or b"{}")
            order_id   = str(body.get("order_id", "")).strip()
            order_type = str(body.get("type", "chevron")).strip()
            text       = str(body.get("body", "")).strip()
            if not order_id or not text:
                self._json_error(400, "order_id and body required")
                return
            user_id  = self._current_user.get("sub", "")
            user_res = get_client().table("users").select("full_name,role").eq("id", user_id).execute()
            if not user_res.data:
                self._json_error(404, "user not found")
                return
            u           = user_res.data[0]
            author_name = u.get("full_name") or "Unknown"
            author_role = u.get("role") or "user"
            col = "nlng_order_id" if order_type == "nlng" else "order_id"
            row = {col: order_id, "user_id": user_id,
                   "author_name": author_name, "author_role": author_role, "body": text}
            result = get_client().table("po_comments").insert(row).execute()
            self._json_ok(result.data[0] if result.data else {"ok": True})
        except Exception:
            self._json_error(500, "Server error")

    def _patch_comment(self, comment_id: str) -> None:
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length) or b"{}")
            text   = str(body.get("body", "")).strip()
            if not text:
                self._json_error(400, "body required")
                return
            user_id = self._current_user.get("sub", "")
            exists  = get_client().table("po_comments").select("id,user_id").eq("id", comment_id).execute()
            if not exists.data:
                self._json_error(404, "comment not found")
                return
            if exists.data[0].get("user_id") != user_id and self._current_user.get("role") != "admin":
                self._json_error(403, "cannot edit another user's comment")
                return
            get_client().table("po_comments").update({"body": text}).eq("id", comment_id).execute()
            self._json_ok({"ok": True})
        except Exception:
            self._json_error(500, "Server error")

    def _delete_comment(self, comment_id: str) -> None:
        try:
            user_id = self._current_user.get("sub", "")
            exists  = get_client().table("po_comments").select("id,user_id").eq("id", comment_id).execute()
            if not exists.data:
                self._json_error(404, "comment not found")
                return
            if exists.data[0].get("user_id") != user_id and self._current_user.get("role") != "admin":
                self._json_error(403, "cannot delete another user's comment")
                return
            get_client().table("po_comments").delete().eq("id", comment_id).execute()
            self._json_ok({"ok": True})
        except Exception:
            self._json_error(500, "Server error")

    def log_message(self, fmt, *args):  # noqa: A002
        print(f"  [{self.address_string()}] {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="SPMprocure360 server")
    # Railway injects $PORT; locally fall back to $WEB_PORT then 8080
    default_port = int(os.environ.get("PORT") or os.environ.get("WEB_PORT") or 8080)
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args()

    host = "0.0.0.0"   # Railway requires binding to all interfaces, not just localhost
    print(f"SPMprocure360  ->  http://localhost:{args.port}")
    print(f"   Ctrl+C to stop.\n")
    server = HTTPServer((host, args.port), _Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")


if __name__ == "__main__":
    main()

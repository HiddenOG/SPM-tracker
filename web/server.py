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

import base64
import io
import json
import os
import re
import subprocess
import sys
import time
import threading
import urllib.parse
import argparse
import secrets
import string
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

# Department → shared email mapping
DEPT_EMAILS: dict[str, str] = {
    "procurement": "specialpiping@gmail.com",
    "warehouse":   "spmwarehouse22@gmail.com",
    "accounts":    "accounts@specialpipingltd.com",
    "expeditor":   "etsano@specialpipingltd.com",
}

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

# AI summary cache — keyed by "order_id:order_type", avoids re-running Groq on repeat views
_SUMMARY_CACHE: dict[str, tuple[float, str]] = {}
_SUMMARY_CACHE_TTL = 600  # 10 minutes

# Persistent Gmail IMAP connection — reused across requests to avoid SSL handshake overhead
_imap_conn = None

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


_CLIENT_CODES = {
    "chevron": "CNL",
    "nlng":    "NLNG",
    "seplat":  "SEP",
    "exxon":   "MPN",
    "total":   "TEN",
    "others":  "OTH",
}


def _initials_from_name(name: str) -> str:
    words = [w for w in re.sub(r"[^A-Za-z\s]", "", name).split() if w]
    if not words:
        return "OTH"
    return "".join(w[0].upper() for w in words)


def _next_quote_number(client: str, client_name: str = "") -> tuple[str, int]:
    if client.lower() == "others" and client_name.strip():
        code = _initials_from_name(client_name)
    else:
        code = _CLIENT_CODES.get(client.lower(), "OTH")
    try:
        res = (
            get_client().table("quotations")
            .select("id", count="exact")
            .eq("client", client.lower())
            .execute()
        )
        last = res.count or 0
    except Exception:
        last = 0
    seq = int(last) + 1
    return f"SPM-{seq:05d}-{code}", seq


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
        if path == "/reports.js":
            self._serve_file(WEB_DIR / "reports.js", "application/javascript; charset=utf-8")
            return
        if path == "/sw.js":
            self._serve_file(WEB_DIR / "sw.js", "application/javascript; charset=utf-8")
            return
        if path == "/logo.png":
            self._serve_file(ROOT / "data_fl" / "Gemini_Generated_Image_z6jc3vz6jc3vz6jc.png", "image/png")
            return
        if path == "/bg.jpg":
            self._serve_file(ROOT / "data_fl" / "spm4.jpg", "image/jpeg")
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
        elif path == "/api/alerts":
            self._serve_alerts()
        elif path == "/api/quotations":
            self._serve_quotations()
        elif re.match(r"^/api/quotations/[^/]+/pdf$", path):
            m2 = re.match(r"^/api/quotations/([^/]+)/pdf$", path)
            self._serve_quotation_pdf(m2.group(1))
        elif re.match(r"^/api/quotations/[^/]+/export_xlsx$", path):
            m2 = re.match(r"^/api/quotations/([^/]+)/export_xlsx$", path)
            self._serve_quotation_xlsx(m2.group(1))
        elif re.match(r"^/api/quotations/[^/]+$", path):
            m2 = re.match(r"^/api/quotations/([^/]+)$", path)
            self._get_quotation(m2.group(1))
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
        if path == "/api/quotations/parse-enquiry":
            self._parse_enquiry_file()
            return
        m_ip = re.match(r"^/api/quotations/([^/]+)/import_prices$", path)
        if m_ip:
            self._import_line_item_prices(m_ip.group(1))
            return
        if path == "/api/quotations":
            self._create_quotation()
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
            department = str(body.get("department", "")).strip().lower()
            username   = str(body.get("username", "")).strip()
            password   = str(body.get("password", ""))

            if not password:
                self._json_error(400, "password required")
                return

            # Dept users: find by role (multiple users share one dept, not one email)
            # Admin users: find by individual email
            if department in DEPT_EMAILS:
                result = get_client().table("users").select(
                    "id,email,password_hash,role,full_name,is_active"
                ).eq("role", department).execute()
            else:
                email = str(body.get("email", "")).strip().lower()
                if not email:
                    self._json_error(400, "email required for admin login")
                    return
                result = get_client().table("users").select(
                    "id,email,password_hash,role,full_name,is_active"
                ).eq("email", email).execute()

            if not result.data:
                self._json_error(401, "Invalid credentials")
                return

            # Departments share one email; try each user until password matches
            matched: dict | None = None
            for candidate in result.data:
                if not candidate.get("is_active"):
                    continue
                try:
                    if bcrypt.checkpw(password.encode(), candidate["password_hash"].encode()):
                        matched = candidate
                        break
                except Exception:
                    continue

            if not matched:
                self._json_error(401, "Invalid email or password")
                return

            # Save display name if the user has none yet
            update: dict = {"last_login_at": datetime.now(timezone.utc).isoformat()}
            if username and not matched.get("full_name"):
                update["full_name"] = username
                matched["full_name"] = username
            get_client().table("users").update(update).eq("id", matched["id"]).execute()

            _clear_rate_limit(ip)
            display_name = matched.get("full_name") or username or matched.get("email", "")
            matched["full_name"] = display_name
            token = _make_token(matched)
            self._json_ok({
                "token": token,
                "user": {
                    "id":    str(matched["id"]),
                    "email": matched["email"],
                    "role":  matched["role"],
                    "name":  display_name,
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

    @staticmethod
    def _embed_so_line_items(orders: list) -> None:
        """Attach so_line_items list to each order dict in-place."""
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
            self._embed_so_line_items(orders)
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
            self._embed_so_line_items(orders)
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

    def do_PUT(self):
        if not self._require_auth():
            return
        path = self.path.split("?")[0]
        m = re.match(r"^/api/quotations/([^/]+)$", path)
        if m:
            self._update_quotation(m.group(1))
            return
        self.send_response(404)
        self.end_headers()

    def do_DELETE(self):
        if not self._require_auth():
            return
        path = self.path.split("?")[0]
        m = re.match(r"^/api/users/([^/]+)$", path)
        if m:
            self._delete_user(m.group(1))
            return
        m = re.match(r"^/api/comments/([^/]+)$", path)
        if m:
            self._delete_comment(m.group(1))
            return
        m = re.match(r"^/api/quotations/([^/]+)$", path)
        if m:
            self._delete_quotation(m.group(1))
            return
        self.send_response(404)
        self.end_headers()

    def _delete_user(self, user_id: str) -> None:
        if self._current_user.get("role") != "admin":
            self._json_error(403, "admin access required")
            return
        try:
            res = get_client().table("users").select("id,role").eq("id", user_id).execute()
            if not res.data:
                self._json_error(404, "user not found")
                return
            target = res.data[0]
            if target.get("role") == "admin":
                self._json_error(403, "cannot delete admin accounts")
                return
            if str(target["id"]) == str(self._current_user.get("sub", "")):
                self._json_error(403, "cannot delete your own account")
                return
            get_client().table("users").delete().eq("id", user_id).execute()
            self._json_ok({"ok": True})
        except Exception:
            self._json_error(500, "Server error")

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
            print(f"  [error] _patch_field: {exc}")
            self._json_error(500, "Server error")

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

            # Reuse persistent connection to avoid SSL handshake + login overhead
            global _imap_conn
            imap = None
            if _imap_conn is not None:
                try:
                    _imap_conn.noop()
                    imap = _imap_conn
                except Exception:
                    _imap_conn = None
            if imap is None:
                imap = IMAPClient("imap.gmail.com", port=993, use_uid=True, ssl=True, timeout=20)
                imap.login(gmail_addr, app_pw)
                imap.select_folder("[Gmail]/All Mail", readonly=True)
                _imap_conn = imap

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

    def _build_order_context(self, order_id: str, order_type: str) -> str:
        """Fetch structured order data from the DB and format it as readable text for the AI."""
        try:
            def _d(s) -> str:
                return str(s)[:10] if s else "—"

            if order_type == "nlng":
                res = get_client().table("nlng_orders").select(
                    "po_number,variation_number,document_date,net_value,currency,overall_status,"
                    "required_delivery_date,enquiry_number,contact_name,contact_email,"
                    "sent_to_warehouse_at,stock_check_completed_at,"
                    "spm_po_number,spm_po_sent_at,so_number,so_received_at,promised_date,"
                    "dispatch_instructions_sent_at,dispatched_at,delivered_at,"
                    "nlng_order_line_items(item_no,mesc_code,description,quantity,uom,unit_price,net_amount,int_article_no)"
                ).eq("id", order_id).execute()
                if not res.data:
                    return ""
                o = res.data[0]
                cur = o.get("currency") or "USD"

                lines = ["ORDER TYPE: NLNG (Nigeria LNG Limited)",
                         f"PO Number: {o.get('po_number') or '—'}"]
                if o.get("variation_number"):
                    lines.append(f"Variation: {o['variation_number']}")
                lines += [
                    f"Document Date: {_d(o.get('document_date'))}",
                    f"Status: {o.get('overall_status') or '—'}",
                    f"Net Value: {cur} {o.get('net_value') or '—'}",
                    f"Required Delivery: {_d(o.get('required_delivery_date'))}",
                ]
                if o.get("enquiry_number"):
                    lines.append(f"Enquiry#: {o['enquiry_number']}")
                if o.get("contact_name"):
                    lines.append(f"Contact: {o['contact_name']} <{o.get('contact_email') or ''}>")

                lines.append("\nKEY DATES:")
                if o.get("sent_to_warehouse_at"):
                    lines.append(f"  Sent to warehouse: {_d(o['sent_to_warehouse_at'])}")
                if o.get("stock_check_completed_at"):
                    lines.append(f"  Stock check complete: {_d(o['stock_check_completed_at'])}")
                if o.get("spm_po_sent_at"):
                    lines.append(f"  SPM PO sent: {_d(o['spm_po_sent_at'])} ({o.get('spm_po_number') or ''})")
                if o.get("so_number"):
                    lines.append(f"  Supplier SO received: {_d(o.get('so_received_at'))} ({o['so_number']})")
                if o.get("promised_date"):
                    lines.append(f"  Promised dispatch: {_d(o['promised_date'])}")
                if o.get("dispatch_instructions_sent_at"):
                    lines.append(f"  Dispatch instructions sent: {_d(o['dispatch_instructions_sent_at'])}")
                if o.get("dispatched_at"):
                    lines.append(f"  Dispatched: {_d(o['dispatched_at'])}")
                if o.get("delivered_at"):
                    lines.append(f"  Delivered: {_d(o['delivered_at'])}")

                items = sorted(o.get("nlng_order_line_items") or [], key=lambda x: int(x.get("item_no") or 0))
                if items:
                    lines.append(f"\nLINE ITEMS ({len(items)}):")
                    for it in items:
                        mesc = f"MESC {it['mesc_code']}" if it.get("mesc_code") else ""
                        art  = f"Art# {it['int_article_no']}" if it.get("int_article_no") else ""
                        ref  = " | ".join(filter(None, [mesc, art]))
                        desc = it.get("description") or ""
                        qty  = f"Qty {it.get('quantity')} {it.get('uom') or ''}".strip()
                        up   = f"Unit {cur} {float(it['unit_price']):,.2f}" if it.get("unit_price") else ""
                        tot  = f"Total {cur} {float(it['net_amount']):,.2f}" if it.get("net_amount") else ""
                        parts = " | ".join(filter(None, [qty, up, tot]))
                        lines.append(f"  {it.get('item_no','')}. [{ref}] {desc} — {parts}")

            else:  # chevron
                res = get_client().table("orders").select(
                    "buyer_po_number,po_amount,po_currency,overall_status,req_number,buyer_name,"
                    "required_delivery_date,po_destination,acknowledged_at,"
                    "sent_to_warehouse_at,stock_check_completed_at,"
                    "spm_po_number,spm_po_sent_at,so_number,so_received_at,promised_date,"
                    "dispatch_instructions_sent_at,dispatched_at,delivered_at,"
                    "order_line_items(line_no,description,quantity,buyer_part_code,required_delivery_date,promised_date)"
                ).eq("id", order_id).execute()
                if not res.data:
                    return ""
                o = res.data[0]

                lines = ["ORDER TYPE: Chevron Nigeria Limited (CNL)",
                         f"PO Number: {o.get('buyer_po_number') or '—'}",
                         f"Status: {o.get('overall_status') or '—'}",
                         f"PO Value: {o.get('po_currency') or 'USD'} {o.get('po_amount') or '—'}"]
                if o.get("req_number"):
                    lines.append(f"REQ#: {o['req_number']}")
                if o.get("buyer_name"):
                    lines.append(f"Buyer: {o['buyer_name']}")
                if o.get("po_destination"):
                    lines.append(f"Destination: {o['po_destination']}")
                lines.append(f"Required Delivery: {_d(o.get('required_delivery_date'))}")

                lines.append("\nKEY DATES:")
                if o.get("acknowledged_at"):
                    lines.append(f"  Acknowledged on GEP: {_d(o['acknowledged_at'])}")
                if o.get("sent_to_warehouse_at"):
                    lines.append(f"  Sent to warehouse: {_d(o['sent_to_warehouse_at'])}")
                if o.get("stock_check_completed_at"):
                    lines.append(f"  Stock check complete: {_d(o['stock_check_completed_at'])}")
                if o.get("spm_po_sent_at"):
                    lines.append(f"  SPM PO sent: {_d(o['spm_po_sent_at'])} ({o.get('spm_po_number') or ''})")
                if o.get("so_number"):
                    lines.append(f"  Supplier SO received: {_d(o.get('so_received_at'))} ({o['so_number']})")
                if o.get("promised_date"):
                    lines.append(f"  Promised dispatch: {_d(o['promised_date'])}")
                if o.get("dispatch_instructions_sent_at"):
                    lines.append(f"  Dispatch instructions sent: {_d(o['dispatch_instructions_sent_at'])}")
                if o.get("dispatched_at"):
                    lines.append(f"  Dispatched: {_d(o['dispatched_at'])}")
                if o.get("delivered_at"):
                    lines.append(f"  Delivered: {_d(o['delivered_at'])}")

                items = sorted(o.get("order_line_items") or [], key=lambda x: int(x.get("line_no") or 0))
                if items:
                    lines.append(f"\nLINE ITEMS ({len(items)}):")
                    for it in items:
                        desc = it.get("description") or ""
                        qty  = f"Qty {it.get('quantity')}" if it.get("quantity") else ""
                        pn   = f"Part# {it['buyer_part_code']}" if it.get("buyer_part_code") else ""
                        prom = f"Promised {_d(it.get('promised_date'))}" if it.get("promised_date") else ""
                        parts = " | ".join(filter(None, [qty, pn, prom]))
                        lines.append(f"  {it.get('line_no','')}. {desc}" + (f" — {parts}" if parts else ""))

            return "\n".join(lines)
        except Exception as exc:
            print(f"  [warn] _build_order_context: {exc}")
            return ""

    def _load_ai_memory(self, order_id: str, order_type: str) -> str:
        """Return persisted AI knowledge for this order: prior summary + sibling order statuses."""
        try:
            db = get_client()
            parts = []

            mem = db.table("ai_memory").select("summary,updated_at") \
                    .eq("order_id", order_id).eq("order_type", order_type).execute()
            if mem.data:
                row = mem.data[0]
                updated = str(row.get("updated_at", ""))[:10]
                parts.append(f"STORED SUMMARY (last updated {updated}):\n{row['summary']}")

            table  = "nlng_orders" if order_type == "nlng" else "orders"
            po_col = "po_number"   if order_type == "nlng" else "buyer_po_number"
            spm_res = db.table(table).select("spm_po_number").eq("id", order_id).execute()
            if spm_res.data:
                spm_po = spm_res.data[0].get("spm_po_number")
                if spm_po:
                    sib_res = db.table(table) \
                                .select(f"{po_col},overall_status,promised_date") \
                                .eq("spm_po_number", spm_po).neq("id", order_id).execute()
                    if sib_res.data:
                        lines = []
                        for s in sib_res.data:
                            pn   = s.get(po_col) or "?"
                            stat = s.get("overall_status") or "?"
                            prom = str(s.get("promised_date") or "")[:10] or "—"
                            lines.append(f"  - PO {pn}: {stat} | promised {prom}")
                        parts.append(f"SIBLING ORDERS (same SPM PO {spm_po}):\n" + "\n".join(lines))

            return "\n\n".join(parts)
        except Exception as exc:
            print(f"  [warn] _load_ai_memory: {exc}")
            return ""

    def _save_ai_memory(self, order_id: str, order_type: str, summary: str) -> None:
        """Upsert AI-generated summary into ai_memory for long-term recall."""
        try:
            get_client().table("ai_memory").upsert(
                {
                    "order_id":   order_id,
                    "order_type": order_type,
                    "summary":    summary,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                on_conflict="order_id,order_type",
            ).execute()
        except Exception as exc:
            print(f"  [warn] _save_ai_memory: {exc}")

    def _serve_email_summary(self) -> None:
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            order_id   = qs.get("order_id", [None])[0]
            order_type = qs.get("type",      ["chevron"])[0]
            if not order_id:
                self._json_error(400, "order_id required")
                return
            if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("GROQ_API_KEY"):
                self._json_error(503, "No AI key configured (ANTHROPIC_API_KEY or GROQ_API_KEY)")
                return
            # ── Summary cache — skip AI call entirely on repeat views ─────────
            summary_key = f"{order_id}:{order_type}"
            cached_summary = _SUMMARY_CACHE.get(summary_key)
            if cached_summary:
                cached_at, cached_text = cached_summary
                if time.time() - cached_at < _SUMMARY_CACHE_TTL:
                    self._start_sse()
                    self._sse_chunk(cached_text)
                    self._sse_done()
                    return

            # Start SSE immediately — browser gets feedback within milliseconds
            po_number, so_number = self._get_po_number(order_id, order_type)
            self._start_sse()
            self._sse_status("Searching Gmail thread, please wait…")

            # ── Parallel fetch: order record, emails, AI memory ───────────────
            ctx_result    = [None]
            email_result  = [None]
            memory_result = [None]
            def _f_ctx():    ctx_result[0]    = self._build_order_context(order_id, order_type)
            def _f_emails(): email_result[0]  = self._build_email_context(po_number, so_number)
            def _f_memory(): memory_result[0] = self._load_ai_memory(order_id, order_type)
            t1 = threading.Thread(target=_f_ctx,    daemon=True)
            t2 = threading.Thread(target=_f_emails, daemon=True)
            t3 = threading.Thread(target=_f_memory, daemon=True)
            t1.start(); t2.start(); t3.start()
            t1.join();  t2.join();  t3.join()
            order_ctx   = ctx_result[0]   or ""
            thread_text = email_result[0] or ""
            memory_text = memory_result[0] or ""
            self._sse_status("Analysing emails…")
            if not thread_text and not order_ctx:
                self._sse_chunk("No data found for this PO yet.")
                self._sse_done()
                return
            order_section  = f"=== ORDER RECORD ===\n{order_ctx}\n\n" if order_ctx else ""
            thread_section = f"=== EMAIL THREAD ===\n{thread_text}" if thread_text else "=== EMAIL THREAD ===\n(No emails captured yet)"
            memory_section = f"=== PRIOR KNOWLEDGE FROM MEMORY ===\n{memory_text}\n\n" if memory_text else ""
            prompt = (
                f"{_SPM_CONTEXT}\n\n"
                f"{memory_section}"
                f"{order_section}"
                f"{thread_section}\n\n"
                "You have read the entire email thread above from start to finish. Now write a proportional story of what happened — like condensing a long article into 500-600 words maximum.\n"
                "Rules:\n"
                "1. Write in plain flowing paragraphs. No headers, bullets, subheadings, or tables.\n"
                "2. Do NOT repeat what is already in the ORDER RECORD (buyer name, PO value, status, line items, dates). The user can already see those.\n"
                "3. Cover the full arc — beginning, middle, and end. Budget your words before you start writing: early routine exchanges get 1-2 sentences each, problems and decisions get a paragraph, the most recent activity gets the most detail.\n"
                "4. Use real names, dates, and short direct quotes from emails where they make the story clearer.\n"
                "5. End with the current state — what just happened and what is still open or unresolved.\n"
                "6. You must finish within 600 words. Always end with a complete sentence."
            )
            chunks = []
            for chunk in self._ai_stream([{"role": "user", "content": prompt}], max_tokens=1000):
                self._sse_chunk(chunk)
                chunks.append(chunk)
            self._sse_done()
            if chunks:
                full_summary = "".join(chunks)
                _SUMMARY_CACHE[summary_key] = (time.time(), full_summary)
                threading.Thread(
                    target=self._save_ai_memory,
                    args=(order_id, order_type, full_summary),
                    daemon=True,
                ).start()
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
        BODY_LIMIT  = 1_500   # chars per email body
        CHAR_BUDGET = 15_000  # total context chars before sampling kicks in

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

    def _sse_status(self, msg: str) -> None:
        """Send a status/progress event the browser can show while waiting."""
        event = f"data: {json.dumps({'status': msg})}\n\n"
        self.wfile.write(event.encode())
        self.wfile.flush()

    def _ai_stream(self, messages: list, max_tokens: int = 400, system: str = ""):
        """Claude primary, Groq fallback. Yields text chunks via SSE-ready generator."""
        claude_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if claude_key:
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=claude_key)
                kwargs: dict = {
                    "model":      "claude-haiku-4-5-20251001",
                    "max_tokens": max_tokens,
                    "messages":   messages,
                }
                if system:
                    kwargs["system"] = system
                with client.messages.stream(**kwargs) as stream:
                    for text in stream.text_stream:
                        yield text
                return
            except Exception as exc:
                print(f"  [claude] failed ({type(exc).__name__}: {exc}) — trying Groq")

        groq_messages = ([{"role": "system", "content": system}] + messages) if system else messages
        yield from self._groq_stream(groq_messages, max_tokens=max_tokens)

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
                    "User-Agent":     "SPMProcure360/1.0",
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

            if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("GROQ_API_KEY"):
                self._json_error(503, "No AI key configured (ANTHROPIC_API_KEY or GROQ_API_KEY)")
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
            ctx_result    = [None]
            email_result  = [None]
            memory_result = [None]
            def _f_ctx():    ctx_result[0]    = self._build_order_context(order_id, order_type)
            def _f_emails(): email_result[0]  = self._build_email_context(po_number, so_number)
            def _f_memory(): memory_result[0] = self._load_ai_memory(order_id, order_type)
            t1 = threading.Thread(target=_f_ctx,    daemon=True)
            t2 = threading.Thread(target=_f_emails, daemon=True)
            t3 = threading.Thread(target=_f_memory, daemon=True)
            t1.start(); t2.start(); t3.start()
            t1.join();  t2.join();  t3.join()
            order_ctx   = ctx_result[0]   or ""
            thread_text = email_result[0] or ""
            memory_text = memory_result[0] or ""
            order_section  = f"=== ORDER RECORD ===\n{order_ctx}\n\n" if order_ctx else ""
            thread_section = f"=== EMAIL THREAD ===\n{thread_text}" if thread_text else "=== EMAIL THREAD ===\n(No emails captured yet)"
            memory_section = f"\n\n=== PRIOR KNOWLEDGE FROM MEMORY ===\n{memory_text}" if memory_text else ""
            system_msg  = {
                "role":    "system",
                "content": (
                    f"{_SPM_CONTEXT}\n\n"
                    "You are a procurement assistant for the SPM team.\n"
                    "Rules:\n"
                    "- Answer in plain, simple English. No jargon. Short sentences.\n"
                    "- The EMAIL THREAD is the ground truth for what is actually happening — always use it first when answering questions about current state, where goods are, what was said, or what is unresolved.\n"
                    "- The ORDER RECORD is reliable for reference data only: PO numbers, MESC codes, PO value, line item quantities, and key milestone dates stamped by the system.\n"
                    "- If the email trail contradicts the ORDER RECORD status, trust the emails — the DB status is often behind reality.\n"
                    "- Only state facts visible in the EMAIL THREAD or ORDER RECORD. If the answer is not there, say so — do not guess.\n"
                    "- Keep replies short. Two or three sentences is usually enough.\n\n"
                    f"{order_section}"
                    f"{thread_section}"
                    f"{memory_section}"
                    f"{summary_ctx}"
                ),
            }
            self._start_sse()
            sse_started = True
            for chunk in self._ai_stream(messages, system=system_msg["content"]):
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
            name     = str(body.get("full_name", "")).strip()
            password = str(body.get("password", ""))
            role     = str(body.get("role", ""))
            valid_roles = ("admin", "procurement", "warehouse", "expeditor", "accounts")
            if not password or role not in valid_roles:
                self._json_error(400, "password and valid role are required")
                return
            import uuid as _uuid
            uid6     = str(_uuid.uuid4())[:6]
            username = (name + "_" + uid6) if name else (role + "_" + uid6)
            # Dept users get a synthetic unique email; admin needs a real one
            if role in DEPT_EMAILS:
                email = f"{username}@spm.dept"
                # Enforce max 7 active users per department
                existing = get_client().table("users").select("id").eq("role", role).eq("is_active", True).execute()
            else:
                email = str(body.get("email", "")).strip().lower()
                if not email:
                    self._json_error(400, "email required for admin accounts")
                    return
                existing = get_client().table("users").select("id").eq("email", email).eq("is_active", True).execute()
            if len(existing.data or []) >= 7:
                self._json_error(400, "Maximum 7 users per department — deactivate one before adding another")
                return
            pw_hash  = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
            result   = get_client().table("users").insert({
                "email":         email,
                "username":      username,
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
        except Exception as exc:
            print(f"  [error] _handle_create_user: {exc}")
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
                elif key == "role":
                    update["role"] = body[key]
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

    def _serve_alerts(self) -> None:
        CLOSED = {'delivered', 'cancelled', 'closed'}
        try:
            now        = datetime.now(timezone.utc)
            two_h_ago  = (now - timedelta(hours=2)).isoformat()
            today      = now.date()

            SKIP = CLOSED | {"paid", "invoiced", "waybill_received"}

            # Single query per table — Python filters for "new" and "deadline" cases.
            # Use created_at (Supabase auto-timestamp) for "new" detection — NOT
            # notification_received_at, which is the email's Date header and can
            # be hours/days old when the listener processes a backlogged inbox.
            chev_all = (get_client().table("orders")
                        .select("id,buyer_po_number,buyer_name,promised_date,required_delivery_date,"
                                "delivered_at,overall_status,created_at,delivery_requested_at")
                        .execute().data or [])

            nlng_all = (get_client().table("nlng_orders")
                        .select("id,po_number,promised_date,required_delivery_date,"
                                "delivered_at,overall_status,created_at")
                        .execute().data or [])

            chev_new     = [o for o in chev_all if (o.get("created_at") or "") >= two_h_ago]
            chev_delreq  = [o for o in chev_all if (o.get("delivery_requested_at") or "") >= two_h_ago]
            nlng_new     = [o for o in nlng_all if (o.get("created_at") or "") >= two_h_ago]

            critical: list  = []
            new_pos: list   = []
            del_reqs: list  = []

            def _days_left(o: dict) -> int | None:
                d = o.get("promised_date") or o.get("required_delivery_date")
                if not d:
                    return None
                try:
                    return (datetime.fromisoformat(d[:10]).date() - today).days
                except Exception:
                    return None

            for o in chev_all:
                if o.get("delivered_at") or o.get("overall_status") in SKIP:
                    continue
                oid = "chev-" + str(o["id"])
                dl = _days_left(o)
                if dl is not None and dl < 7:
                    critical.append({"id": oid, "po": o.get("buyer_po_number"), "buyer": o.get("buyer_name"), "days_left": dl})

            for o in chev_new:
                oid = "chev-" + str(o["id"])
                new_pos.append({"id": oid, "po": o.get("buyer_po_number"), "buyer": o.get("buyer_name")})

            for o in chev_delreq:
                oid = "chev-" + str(o["id"])
                del_reqs.append({"id": oid, "po": o.get("buyer_po_number")})

            for o in nlng_all:
                if o.get("delivered_at") or o.get("overall_status") in CLOSED:
                    continue
                oid = "nlng-" + str(o["id"])
                dl = _days_left(o)
                if dl is not None and dl < 7:
                    critical.append({"id": oid, "po": o.get("po_number"), "buyer": "NLNG", "days_left": dl})

            for o in nlng_new:
                oid = "nlng-" + str(o["id"])
                new_pos.append({"id": oid, "po": o.get("po_number"), "buyer": "NLNG"})

            self._json_ok({"critical": critical, "new_pos": new_pos, "delivery_requests": del_reqs})
        except Exception:
            self._json_error(500, "Server error")

    # ── Quotation CRUD ─────────────────────────────────────────────────────────

    def _serve_quotations(self) -> None:
        try:
            res = get_client().table("quotations").select("*").order("created_at", desc=True).execute()
            self._json_ok(res.data or [])
        except Exception as exc:
            print(f"  [error] _serve_quotations: {exc}")
            self._json_error(500, "Server error")

    def _get_quotation(self, quote_id: str) -> None:
        try:
            res = get_client().table("quotations").select("*").eq("id", quote_id).execute()
            if not res.data:
                self._json_error(404, "Quotation not found")
                return
            q = res.data[0]
            li = get_client().table("quotation_line_items").select("*").eq("quotation_id", quote_id).order("item_no").execute()
            q["line_items"] = li.data or []
            self._json_ok(q)
        except Exception as exc:
            print(f"  [error] _get_quotation: {exc}")
            self._json_error(500, "Server error")

    def _create_quotation(self) -> None:
        try:
            raw_len = self.headers.get("Content-Length")
            length  = int(raw_len) if raw_len else 0
            body    = json.loads(self.rfile.read(length) or b"{}")
            client      = str(body.get("client") or "chevron").lower()
            client_name = str(body.get("client_name") or "")
            items       = body.get("line_items") or []
            qnum, qseq  = _next_quote_number(client, client_name)
            record = {
                "quote_number":      qnum,
                "quote_seq":         qseq,
                "client":            client,
                "status":            body.get("status") or "draft",
                "reference_po":      body.get("reference_po"),
                "recipient_name":    body.get("recipient_name"),
                "recipient_dept":    body.get("recipient_dept"),
                "recipient_company": body.get("recipient_company"),
                "recipient_tel":     body.get("recipient_tel"),
                "recipient_email":   body.get("recipient_email"),
                "prepared_by":       body.get("prepared_by") or (self._current_user or {}).get("name"),
                "subject":           body.get("subject"),
                "quote_date":        body.get("quote_date"),
                "validity_days":     int(body.get("validity_days") or 30),
                "lead_time":         body.get("lead_time"),
                "delivery_dest":     body.get("delivery_dest"),
                "country_import":    body.get("country_import"),
                "shipping_mode":     body.get("shipping_mode") or "Sea",
                "manufacturer":      body.get("manufacturer"),
                "weight":            body.get("weight"),
                "currency":          body.get("currency") or "USD",
                "discount_pct":      float(body.get("discount_pct") or 0),
                "shipping_charges":  float(body.get("shipping_charges") or 0),
                "sub_total":         float(body.get("sub_total") or 0),
                "total":             float(body.get("total") or 0),
                "notes":             body.get("notes"),
                "sig_name":          body.get("sig_name"),
                "created_by":        (self._current_user or {}).get("name") or (self._current_user or {}).get("email"),
            }
            res = get_client().table("quotations").insert(record).execute()
            if not res.data:
                self._json_error(500, "Insert failed")
                return
            new_q = res.data[0]
            qid   = new_q["id"]
            if items:
                get_client().table("quotation_line_items").insert([{
                    "quotation_id": qid,
                    "item_no":      item.get("item_no") or idx + 1,
                    "product_no":   item.get("product_no") or None,
                    "description":  item.get("description"),
                    "uom":          item.get("uom") or "EA",
                    "quantity":     float(item.get("quantity") or 0),
                    "unit_price":   float(item.get("unit_price") or 0),
                    "tax_rate":     float(item.get("tax_rate") or 0),
                    "line_total":   float(item.get("line_total") or 0),
                } for idx, item in enumerate(items)]).execute()
            self._json_ok({"ok": True, "id": qid, "quote_number": qnum})
        except Exception as exc:
            print(f"  [error] _create_quotation: {exc}")
            self._json_error(500, "Server error")

    def _update_quotation(self, quote_id: str) -> None:
        try:
            raw_len = self.headers.get("Content-Length")
            length  = int(raw_len) if raw_len else 0
            body    = json.loads(self.rfile.read(length) or b"{}")
            exists  = get_client().table("quotations").select("id").eq("id", quote_id).execute()
            if not exists.data:
                self._json_error(404, "Quotation not found")
                return
            items = body.get("line_items") or []
            update = {
                "client":            str(body.get("client") or "chevron").lower(),
                "status":            body.get("status") or "draft",
                "reference_po":      body.get("reference_po"),
                "recipient_name":    body.get("recipient_name"),
                "recipient_dept":    body.get("recipient_dept"),
                "recipient_company": body.get("recipient_company"),
                "recipient_tel":     body.get("recipient_tel"),
                "recipient_email":   body.get("recipient_email"),
                "prepared_by":       body.get("prepared_by"),
                "subject":           body.get("subject"),
                "quote_date":        body.get("quote_date"),
                "validity_days":     int(body.get("validity_days") or 30),
                "lead_time":         body.get("lead_time"),
                "delivery_dest":     body.get("delivery_dest"),
                "country_import":    body.get("country_import"),
                "shipping_mode":     body.get("shipping_mode") or "Sea",
                "manufacturer":      body.get("manufacturer"),
                "weight":            body.get("weight"),
                "currency":          body.get("currency") or "USD",
                "discount_pct":      float(body.get("discount_pct") or 0),
                "shipping_charges":  float(body.get("shipping_charges") or 0),
                "sub_total":         float(body.get("sub_total") or 0),
                "total":             float(body.get("total") or 0),
                "notes":             body.get("notes"),
                "sig_name":          body.get("sig_name"),
            }
            if body.get("status") == "sent":
                update["sent_at"] = datetime.now(timezone.utc).isoformat()
            get_client().table("quotations").update(update).eq("id", quote_id).execute()
            get_client().table("quotation_line_items").delete().eq("quotation_id", quote_id).execute()
            if items:
                get_client().table("quotation_line_items").insert([{
                    "quotation_id": quote_id,
                    "item_no":      item.get("item_no") or idx + 1,
                    "product_no":   item.get("product_no") or None,
                    "description":  item.get("description"),
                    "uom":          item.get("uom") or "EA",
                    "quantity":     float(item.get("quantity") or 0),
                    "unit_price":   float(item.get("unit_price") or 0),
                    "tax_rate":     float(item.get("tax_rate") or 0),
                    "line_total":   float(item.get("line_total") or 0),
                } for idx, item in enumerate(items)]).execute()
            self._json_ok({"ok": True})
        except Exception as exc:
            print(f"  [error] _update_quotation: {exc}")
            self._json_error(500, "Server error")

    def _delete_quotation(self, quote_id: str) -> None:
        try:
            exists = get_client().table("quotations").select("id").eq("id", quote_id).execute()
            if not exists.data:
                self._json_error(404, "Quotation not found")
                return
            get_client().table("quotation_line_items").delete().eq("quotation_id", quote_id).execute()
            get_client().table("quotations").delete().eq("id", quote_id).execute()
            self._json_ok({"ok": True})
        except Exception as exc:
            print(f"  [error] _delete_quotation: {exc}")
            self._json_error(500, "Server error")

    def _import_line_item_prices(self, quote_id: str) -> None:
        """Read a previously-exported .xls (HTML table) or .xlsx file and update
        unit_price / tax_rate / line_total for each matched line item."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw    = self.rfile.read(length)
            body   = json.loads(raw)
            file_b64   = body.get("file_data", "")
            file_bytes = base64.b64decode(file_b64.split(",")[-1])

            # Detect format: real XLSX vs HTML-table XLS
            rows = []
            magic = file_bytes[:4]
            print(f"  [import_prices] file_bytes={len(file_bytes)} magic={magic!r}")
            if magic == b"PK\x03\x04":
                # True XLSX (ZIP-based)
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
                print(f"  [import_prices] sheets={wb.sheetnames}")
                # Find the data sheet — skip known boilerplate sheets
                _skip = {"Instructions", "teehsecirP", "Sheet"}
                ws = None
                for name in wb.sheetnames:
                    if name not in _skip:
                        candidate = wb[name]
                        if candidate.max_row and candidate.max_row > 1:
                            ws = candidate
                            break
                if ws is None:
                    ws = wb.active
                print(f"  [import_prices] using sheet={ws.title} max_row={ws.max_row}")
                for row in ws.iter_rows(values_only=True):
                    rows.append([str(c) if c is not None else "" for c in row])
            else:
                # HTML-table XLS (our client-side export format) — use regex
                import re as _re
                html_text = file_bytes.decode("utf-8", errors="replace")
                _strip_tags = lambda s: _re.sub(r"<[^>]+>", "", s).strip()
                for tr in _re.findall(r"<tr[^>]*>(.*?)</tr>", html_text, _re.IGNORECASE | _re.DOTALL):
                    cells = _re.findall(r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>", tr, _re.IGNORECASE | _re.DOTALL)
                    row = [_strip_tags(c) for c in cells]
                    if any(row):
                        rows.append(row)
                print(f"  [import_prices] HTML parse rows={len(rows)}")

            if len(rows) < 2:
                self._json_error(400, "No data rows found in file")
                return

            # Locate columns by header name
            header = [h.lower() for h in rows[0]]
            print(f"  [import_prices] sheet rows={len(rows)}, headers={rows[0]}")
            idx_no    = next((i for i, h in enumerate(header) if h == "#"), None)
            idx_price = next((i for i, h in enumerate(header) if h.startswith("unit price")), None)
            idx_tax   = next((i for i, h in enumerate(header) if h.startswith("tax")), None)

            if idx_no is None or idx_price is None:
                self._json_error(400, f"Required columns not found — headers found: {rows[0][:8]}")
                return

            # Parse data rows
            updates: list[tuple[int, float, float]] = []
            for row in rows[1:]:
                try:
                    item_no    = int(float(row[idx_no]))
                    unit_price = float(row[idx_price]) if row[idx_price] else 0.0
                    tax_rate   = float(row[idx_tax])   if idx_tax is not None and len(row) > idx_tax and row[idx_tax] else 0.0
                    updates.append((item_no, unit_price, tax_rate))
                except (ValueError, IndexError):
                    continue

            if not updates:
                self._json_error(400, "No valid rows to import")
                return

            # Fetch existing line items (need qty + row id)
            li_res   = get_client().table("quotation_line_items").select("id,item_no,quantity").eq("quotation_id", quote_id).execute()
            existing = {r["item_no"]: r for r in (li_res.data or [])}

            for item_no, unit_price, tax_rate in updates:
                if item_no not in existing:
                    continue
                row_data   = existing[item_no]
                qty        = float(row_data.get("quantity") or 0)
                line_total = round(qty * unit_price * (1 + tax_rate / 100), 2)
                get_client().table("quotation_line_items").update({
                    "unit_price": unit_price,
                    "tax_rate":   tax_rate,
                    "line_total": line_total,
                }).eq("id", row_data["id"]).execute()

            # Recalculate quote-level totals
            li_all    = get_client().table("quotation_line_items").select("line_total").eq("quotation_id", quote_id).execute()
            sub_total = round(sum(r.get("line_total") or 0 for r in (li_all.data or [])), 2)
            q_res     = get_client().table("quotations").select("discount_pct,shipping_charges").eq("id", quote_id).execute()
            q_meta    = q_res.data[0] if q_res.data else {}
            discount  = float(q_meta.get("discount_pct") or 0)
            shipping  = float(q_meta.get("shipping_charges") or 0)
            grand     = round(sub_total * (1 - discount / 100) + shipping, 2)
            get_client().table("quotations").update({"sub_total": sub_total, "total": grand}).eq("id", quote_id).execute()

            # Return refreshed line items
            li_updated = get_client().table("quotation_line_items").select("*").eq("quotation_id", quote_id).order("item_no").execute()
            self._json_ok({"line_items": li_updated.data or [], "sub_total": sub_total, "total": grand})
        except Exception as exc:
            logging.exception("import_line_item_prices error")
            self._json_error(500, "Server error")

    _MAX_UPLOAD = 20 * 1024 * 1024  # 20 MB

    def _parse_enquiry_file(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > self._MAX_UPLOAD:
                self._json_error(413, "File too large — maximum 20 MB")
                return
            body   = json.loads(self.rfile.read(length) or b"{}")
            b64    = body.get("data", "")
            filename = body.get("filename", "").lower()
            if not b64:
                self._json_error(400, "No file data received")
                return
            file_bytes = base64.b64decode(b64)

            if filename.endswith(".pdf"):
                self._json_ok(self._parse_nlng_pdf(file_bytes))
                return

            xlsx_bytes = file_bytes
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)

            # Find the data sheet — skip the boilerplate sheets
            _skip = {"Instructions", "teehsecirP", "Sheet"}
            data_ws = None
            for name in wb.sheetnames:
                if name not in _skip:
                    data_ws = wb[name]
                    break
            if data_ws is None:
                self._json_error(400, "No data sheet found in workbook")
                return

            rows = [tuple(r) for r in data_ws.iter_rows(values_only=True)]

            # ── Title (row 0, col 2) ──────────────────────────────────────────
            title = ""
            if rows and len(rows[0]) > 2 and rows[0][2]:
                title = str(rows[0][2]).strip()

            # Parse title into reference_po and subject
            # Format A: "059300-Aug2026 - REQ0672456-Export_Standby Buoys..."
            # Format B: "059513-Aug2026 - CATALOG-PURCHASE OF ITEMS- MONDAY..."
            # Format C: "059430-Aug2026 - CNL WARRI - FLANGE - REQ0681856 - ..."
            reference_po  = ""
            subject       = ""
            parts = [p.strip() for p in title.split(" - ") if p.strip()]
            bid_number    = parts[0] if parts else ""
            # Look for a REQ number anywhere in the title
            req_match = re.search(r'REQ\d+', title)
            if req_match:
                reference_po = req_match.group(0)          # e.g. "REQ0681856"
                subject      = "QUOTATION FOR " + reference_po
            else:
                # CATALOG or other type — everything after the bid number prefix
                rest = title[len(bid_number):].strip().lstrip("- ").strip()
                reference_po = rest
                subject      = "QUOTATION FOR " + rest if rest else ""

            # ── Find "Column Name:" row → build index map ─────────────────────
            col_map       = {}   # cleaned name → column index
            data_start    = -1
            currency      = "USD"
            for idx, row in enumerate(rows):
                if len(row) > 2 and row[2] == "Column Name:":
                    for j, val in enumerate(row):
                        if val and isinstance(val, str):
                            clean = val.strip().lstrip("*")
                            col_map[clean] = j
                            if "Price per unit" in val:
                                if "NGN" in val:
                                    currency = "NGN"
                                elif "GBP" in val:
                                    currency = "GBP"
                    data_start = idx + 1
                    break

            if data_start < 0:
                self._json_error(400, "Could not find column headers in file")
                return

            intent_col    = col_map.get("Intent to Bid")
            unit_col      = col_map.get("Unit")
            volume_col    = col_map.get("Volume")
            po_text_col   = col_map.get("PO Text")
            item_name_col = col_map.get("Item name")

            # UOM normalisation: "EA : Each" → "EA"
            _uom = {"EA":"EA","EACH":"EA","RL":"ROLL","ROLL":"ROLL","ST":"SET",
                    "SET":"SET","PK":"PACK","PACK":"PACK","MTR":"MTR","METER":"MTR",
                    "GL":"GL","GALLON":"GL","BOX":"BOX"}
            def _norm_uom(raw):
                if not raw:
                    return "EA"
                code = str(raw).split(":")[0].strip().upper()
                return _uom.get(code, code[:4])

            # ── Extract line items ────────────────────────────────────────────
            line_items = []
            for row in rows[data_start:]:
                if not row or not any(row):
                    continue
                # Only process rows where Intent to Bid is "Yes"
                if intent_col is not None and len(row) > intent_col:
                    intent = row[intent_col]
                    if intent not in ("Yes", "No"):
                        continue
                    if intent == "No":
                        continue
                # Description: PO Text preferred, fall back to Item name
                desc = ""
                if po_text_col is not None and len(row) > po_text_col and row[po_text_col]:
                    desc = str(row[po_text_col]).strip()
                if not desc and item_name_col is not None and len(row) > item_name_col and row[item_name_col]:
                    desc = str(row[item_name_col]).strip()
                if not desc:
                    continue
                # Quantity
                qty = 1.0
                if volume_col is not None and len(row) > volume_col and row[volume_col] is not None:
                    try:
                        qty = float(row[volume_col]) or 1.0
                    except (TypeError, ValueError):
                        qty = 1.0
                # UOM
                uom = "EA"
                if unit_col is not None and len(row) > unit_col and row[unit_col]:
                    uom = _norm_uom(row[unit_col])

                line_items.append({
                    "description": desc,
                    "quantity":    qty,
                    "uom":         uom,
                    "unit_price":  0,
                    "tax_rate":    0,
                    "line_total":  0,
                })

            self._json_ok({
                "client":         "chevron",
                "reference_po":   reference_po,
                "subject":        subject,
                "bill_to_company": "Chevron Nigeria Limited",
                "department":     "Procurement",
                "currency":       currency,
                "count":          len(line_items),
                "line_items":     line_items,
            })
        except Exception as exc:
            import traceback; traceback.print_exc()
            print(f"  [error] _parse_enquiry_file: {exc}")
            self._json_error(500, "Could not parse file — check format and try again")

    def _parse_nlng_pdf(self, pdf_bytes: bytes) -> dict:
        """
        Parse an NLNG RFx PDF (SAP SRM format, e.g. BID0600025128.PDF).
        Returns a dict ready for _json_ok with the same shape as the Chevron xlsx parser.

        Extraction strategy:
          - Header fields (RFx number, currency) via regex on the full text.
          - Items via a state machine on extracted lines:
              SEEK_ITEM  → bare integer line   → IN_HEADER
              IN_HEADER  → "Specifications:"   → IN_SPECS
                         → "N UOM" pattern     → capture qty/uom
              IN_SPECS   → bare integer line   → save item, back to IN_HEADER
                         → "Terms and Cond"    → stop
          - Description = all lines in IN_SPECS state joined (the detailed spec text).
          - Quantity / UOM = taken from the "N UOM" line (e.g. "6  PC", "2 EA").
        """
        import pdfplumber

        text_parts: list[str] = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text_parts.append(page.extract_text() or "")
        full_text = "\n".join(text_parts)

        # ── Header fields ────────────────────────────────────────────────
        rfx_match  = re.search(r'RFx\s*number[:\s]+(\d+)', full_text)
        rfx_number = rfx_match.group(1).strip() if rfx_match else ""

        curr_match = re.search(r'RFx\s*currency[:\s]+([A-Z]{3})', full_text)
        currency   = curr_match.group(1) if curr_match else "USD"

        # ── Item state machine ────────────────────────────────────────────
        # pdfplumber keeps each table row on ONE line, e.g.:
        #   "1 8541461341 GASKET:SPW;FLEXITALLIC 16 IN CS 19. October 2026"
        # followed by the quantity on the next line: "6 PC"
        # then "Specifications:" then multi-line spec text.
        items: list[dict] = []
        current: dict | None = None
        state = "SEEK_ITEM"
        in_rfx_section = False

        # Matches item rows: 1-3 digit item no, space, then product no.
        # Product no. may have a letter prefix like "NV" (e.g. NV8541326281).
        # Group 1 = item number, Group 2 = product number.
        _item_row = re.compile(r'^(\d{1,3})\s+((?:[A-Z]{1,3})?\d{4,})')
        # Matches quantity lines: "6 PC", "23 PC", "50 PC", "1 KIT"
        _qty_line = re.compile(r'^([\d.]+)\s+([A-Z]{1,5})$')
        # PDF page-break boilerplate that must not be collected into spec text.
        # These lines appear at the top of every page (NLNG letterhead + table header).
        _spec_noise = re.compile(
            r'^('
            r'Page \d+/\d+'           # "Page 2/8"
            r'|LNG Complex'           # NLNG address line 1
            r'|River State'           # NLNG address line 2
            r'|\+234'                 # NLNG phone number
            r'|Item\s+Product\s+no'   # table column header row
            r'|Quantity\s*$'          # table column sub-header
            r')',
            re.IGNORECASE
        )

        for raw_line in full_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            if "RFx details" in line:
                in_rfx_section = True
                continue

            if not in_rfx_section:
                continue

            # Stop at end of items section
            if "Terms and Conditions" in line or "TERMS AND" in line:
                break

            if state == "SEEK_ITEM":
                m = _item_row.match(line)
                if m:
                    current = {"item_no": int(m.group(1)), "product_no": m.group(2), "qty": 1.0, "uom": "PC", "specs": []}
                    state = "IN_HEADER"

            elif state == "IN_HEADER":
                if re.match(r'^Specifications\s*:?\s*$', line, re.IGNORECASE):
                    state = "IN_SPECS"
                else:
                    m = _qty_line.match(line)
                    if m:
                        current["qty"] = float(m.group(1))
                        current["uom"] = m.group(2)

            elif state == "IN_SPECS":
                # A new item row → save current item, start next
                m = _item_row.match(line)
                if m:
                    if current:
                        items.append(current)
                    current = {"item_no": int(m.group(1)), "product_no": m.group(2), "qty": 1.0, "uom": "PC", "specs": []}
                    state = "IN_HEADER"
                elif _spec_noise.match(line):
                    pass  # skip page-break boilerplate (letterhead, page numbers, table headers)
                else:
                    current["specs"].append(line)

        if current:
            items.append(current)

        # ── Build line items ──────────────────────────────────────────────
        line_items = [
            {
                "product_no":  it["product_no"],
                "description": " ".join(it["specs"]).strip(),
                "quantity":    it["qty"],
                "uom":         it["uom"],
                "unit_price":  0,
                "tax_rate":    0,
                "line_total":  0,
            }
            for it in items
        ]

        return {
            "client":          "nlng",
            "reference_po":    rfx_number,
            "subject":         f"QUOTATION FOR RFx {rfx_number}",
            "bill_to_company": "Nigeria LNG Limited",
            "delivery_dest":   "Your N.L.N.G. Warehouse",
            "currency":        currency,
            "count":           len(line_items),
            "line_items":      line_items,
        }

    def _serve_quotation_xlsx(self, quote_id: str) -> None:
        """Generate a proper XLSX using openpyxl so it survives a save/reload in Excel."""
        try:
            if not self._require_auth():
                return
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment
            res = get_client().table("quotations").select("*").eq("id", quote_id).execute()
            if not res.data:
                self._json_error(404, "Quotation not found")
                return
            q  = res.data[0]
            li = get_client().table("quotation_line_items").select("*").eq("quotation_id", quote_id).order("item_no").execute()
            line_items = li.data or []
            is_nlng  = (q.get("client") or "") == "nlng"
            sym      = "₦" if q.get("currency") == "NGN" else "$"

            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Line Items"

            if is_nlng:
                headers = ["#", "Product No.", "Description", "UOM", "Qty",
                           f"Unit Price ({sym})", "Tax %", f"Total ({sym})"]
            else:
                headers = ["#", "Description", "UOM", "Qty",
                           f"Unit Price ({sym})", "Tax %", f"Total ({sym})"]

            # Header row
            hdr_fill = PatternFill("solid", fgColor="1E3A5F")
            hdr_font = Font(bold=True, color="FFFFFF")
            for col, h in enumerate(headers, 1):
                cell = ws.cell(row=1, column=col, value=h)
                cell.font = hdr_font
                cell.fill = hdr_fill
                cell.alignment = Alignment(horizontal="center")

            # Data rows
            for idx, item in enumerate(line_items):
                row_num = idx + 2
                if is_nlng:
                    values = [
                        item.get("item_no") or idx + 1,
                        item.get("product_no") or "",
                        item.get("description") or "",
                        item.get("uom") or "EA",
                        item.get("quantity") or 0,
                        item.get("unit_price") or 0,
                        item.get("tax_rate") or 0,
                        item.get("line_total") or 0,
                    ]
                else:
                    values = [
                        item.get("item_no") or idx + 1,
                        item.get("description") or "",
                        item.get("uom") or "EA",
                        item.get("quantity") or 0,
                        item.get("unit_price") or 0,
                        item.get("tax_rate") or 0,
                        item.get("line_total") or 0,
                    ]
                for col, v in enumerate(values, 1):
                    ws.cell(row=row_num, column=col, value=v)

            # Column widths
            ws.column_dimensions["A"].width = 5
            ws.column_dimensions["B"].width = 55 if not is_nlng else 18
            ws.column_dimensions["C"].width = 8 if not is_nlng else 55

            buf = io.BytesIO()
            wb.save(buf)
            xlsx_bytes = buf.getvalue()

            ref   = q.get("reference_po") or q.get("quote_number") or quote_id
            fname = "line_items_" + re.sub(r"[^\w\-]", "_", str(ref)) + ".xlsx"

            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(xlsx_bytes)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(xlsx_bytes)
        except Exception as exc:
            print(f"  [error] _serve_quotation_xlsx: {exc}")
            self._json_error(500, "XLSX generation failed")

    def _serve_quotation_pdf(self, quote_id: str) -> None:
        try:
            res = get_client().table("quotations").select("*").eq("id", quote_id).execute()
            if not res.data:
                self._json_error(404, "Quotation not found")
                return
            q  = res.data[0]
            li = get_client().table("quotation_line_items").select("*").eq("quotation_id", quote_id).order("item_no").execute()
            from quotation_pdf import generate_quotation_pdf
            pdf_bytes = generate_quotation_pdf(q, li.data or [])
            raw_name = q.get("subject") or q.get("quote_number") or quote_id
            fname = re.sub(r'[^\w\s\-]', '', raw_name).strip().replace(' ', '_') + ".pdf"
            if not fname or fname == ".pdf":
                fname = f"SPM_Quotation_{quote_id}.pdf"
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(pdf_bytes)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(pdf_bytes)
        except Exception as exc:
            print(f"  [error] _serve_quotation_pdf: {exc}")
            self._json_error(500, "PDF generation failed")

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

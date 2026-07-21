"""
server.py — Web server for SPM Tracker dashboard.

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
import sys
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

WEB_DIR = Path(__file__).parent

# JWT config — JWT_SECRET must be set in production env vars
_JWT_SECRET = os.environ.get("JWT_SECRET", "")
_JWT_ALGO   = "HS256"
_JWT_DAYS   = 7

if not _JWT_SECRET:
    _JWT_SECRET = secrets.token_hex(32)
    print("  ⚠️  JWT_SECRET not set — using ephemeral secret (sessions won't survive restarts)")


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
    "id", "buyer_po_number", "po_amount", "notification_received_at",
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
    "order_line_items(line_no,description,quantity,buyer_part_code,required_delivery_date)",
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
        if not self._require_auth():
            return
        if path == "/api/orders":
            self._serve_orders()
        elif path == "/api/nlng_orders":
            self._serve_nlng_orders()
        elif path == "/api/so_line_items":
            self._serve_so_line_items()
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
        self.send_response(404)
        self.end_headers()

    def _handle_login(self) -> None:
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
            token = _make_token(user)
            self._json_ok({
                "token": token,
                "user": {
                    "email": user["email"],
                    "role":  user["role"],
                    "name":  user.get("full_name") or user["email"],
                },
            })
        except Exception as exc:
            self._json_error(500, str(exc))

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

            payload = json.dumps(orders, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:
            err = json.dumps({"error": str(exc)}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)

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

            payload = json.dumps(orders, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:
            err = json.dumps({"error": str(exc)}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)

    def _serve_so_line_items(self) -> None:
        try:
            result = (
                get_client()
                .table("so_line_items")
                .select("so_number,line_no,item_number,despatch_date,qty,uom,unit_price,extended_price")
                .execute()
            )
            payload = json.dumps(result.data or [], default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:
            err = json.dumps({"error": str(exc)}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)

    def do_PATCH(self):
        if not self._require_auth():
            return
        path = self.path.split("?")[0]

        m = re.match(r"^/api/orders/([^/]+)/req_number$", path)
        if m:
            self._patch_field("orders", m.group(1), "req_number")
            return

        m = re.match(r"^/api/nlng_orders/([^/]+)/enquiry_number$", path)
        if m:
            self._patch_field("nlng_orders", m.group(1), "enquiry_number")
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

    def log_message(self, fmt, *args):  # noqa: A002
        print(f"  [{self.address_string()}] {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="SPM Tracker server")
    # Railway injects $PORT; locally fall back to $WEB_PORT then 8080
    default_port = int(os.environ.get("PORT") or os.environ.get("WEB_PORT") or 8080)
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args()

    host = "0.0.0.0"   # Railway requires binding to all interfaces, not just localhost
    print(f"SPM Tracker  ->  http://localhost:{args.port}")
    print(f"   Ctrl+C to stop.\n")
    server = HTTPServer((host, args.port), _Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")


if __name__ == "__main__":
    main()

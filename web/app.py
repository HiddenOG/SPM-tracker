"""
app.py — FastAPI server for SPMprocure360.

Replaces server.py (BaseHTTPRequestHandler) with FastAPI + Gunicorn.
All business logic is unchanged — only the routing layer changed.

Local dev:
    uvicorn web.app:app --reload --port 8080

Production (Railway):
    gunicorn web.app:app --workers 3 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT
"""

import asyncio
import base64
import io
import json
import os
import re
import secrets
import subprocess
import sys
import time
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt
import jwt as pyjwt
import mimetypes
from fastapi import FastAPI, Depends, HTTPException, Request, Body, Query, UploadFile, File
from fastapi.responses import Response, StreamingResponse, FileResponse

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from db import get_client
import sync
from email_utils import get_email_body_text as _get_email_body

WEB_DIR = Path(__file__).parent

# ── Constants ──────────────────────────────────────────────────────────────────

DEPT_EMAILS: dict[str, str] = {
    "procurement": "specialpiping@gmail.com",
    "warehouse":   "spmwarehouse22@gmail.com",
    "accounts":    "accounts@specialpipingltd.com",
    "expeditor":   "etsano@specialpipingltd.com",
}

_JWT_SECRET = os.environ.get("JWT_SECRET", "")
_JWT_ALGO   = "HS256"
_JWT_DAYS   = 7

if not _JWT_SECRET:
    _JWT_SECRET = secrets.token_hex(32)
    print("  ⚠️  JWT_SECRET not set — using ephemeral secret (sessions won't survive restarts)")

_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
_RATE_LIMIT_MAX    = 5
_RATE_LIMIT_WINDOW = 900

_EMAIL_CACHE: dict[str, tuple[float, list]] = {}
_EMAIL_CACHE_TTL = 300

_SUMMARY_CACHE: dict[str, tuple[float, str]] = {}
_SUMMARY_CACHE_TTL = 600

_imap_conn = None


class _GroqRateLimit(Exception):
    pass


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


# ── Auth helpers ───────────────────────────────────────────────────────────────

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
        # Use max(quote_seq) across ALL clients so numbers are globally unique
        # and never reuse a seq even if a quote is deleted.
        res = get_client().table("quotations").select("quote_seq").order("quote_seq", desc=True).limit(1).execute()
        last = int((res.data or [{}])[0].get("quote_seq") or 0)
    except Exception:
        last = 0
    seq = last + 1
    return f"SPM-{seq:05d}-{code}", seq


# ── FastAPI auth dependencies ──────────────────────────────────────────────────

def verify_jwt(request: Request) -> dict:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        payload = _verify_token(auth_header[7:])
        if payload:
            return payload
    raise HTTPException(status_code=401, detail="unauthorized")


def require_admin(user: dict = Depends(verify_jwt)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin access required")
    return user


# ── Response helpers ───────────────────────────────────────────────────────────

def _ok(data) -> Response:
    return Response(
        content=json.dumps(data, default=str),
        media_type="application/json",
        headers={"Cache-Control": "no-cache"},
    )


def _sse_headers() -> dict:
    return {
        "Cache-Control": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "X-Accel-Buffering": "no",
    }


_FILE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}

# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(docs_url="/docs", redoc_url=None)

# ── SSE Broker ─────────────────────────────────────────────────────────────────
_sse_loop: asyncio.AbstractEventLoop | None = None
_sse_clients: set = set()  # set of asyncio.Queue

@app.on_event("startup")
async def _on_startup():
    global _sse_loop
    _sse_loop = asyncio.get_running_loop()

def _sse_broadcast(event_type: str, payload: dict) -> None:
    if not _sse_loop or not _sse_clients:
        return
    msg = f"event: {event_type}\ndata: {json.dumps(payload, default=str)}\n\n"
    for q in list(_sse_clients):
        try:
            _sse_loop.call_soon_threadsafe(q.put_nowait, msg)
        except Exception:
            pass


# ── Static / public routes ─────────────────────────────────────────────────────

@app.get("/health")
def health():
    return _ok({"ok": True})


@app.get("/")
@app.get("/index.html")
def index():
    return FileResponse(WEB_DIR / "index.html", media_type="text/html; charset=utf-8", headers=_FILE_HEADERS)


@app.get("/style.css")
def style_css():
    return FileResponse(WEB_DIR / "style.css", media_type="text/css; charset=utf-8", headers=_FILE_HEADERS)


@app.get("/script.js")
def script_js():
    return FileResponse(WEB_DIR / "script.js", media_type="application/javascript; charset=utf-8", headers=_FILE_HEADERS)


@app.get("/reports.js")
def reports_js():
    return FileResponse(WEB_DIR / "reports.js", media_type="application/javascript; charset=utf-8", headers=_FILE_HEADERS)


@app.get("/sw.js")
def sw_js():
    return FileResponse(WEB_DIR / "sw.js", media_type="application/javascript; charset=utf-8", headers=_FILE_HEADERS)


@app.get("/logo.png")
def logo():
    return FileResponse(ROOT / "data_fl" / "Gemini_Generated_Image_z6jc3vz6jc3vz6jc.png", media_type="image/png")


@app.get("/bg.jpg")
def bg():
    return FileResponse(ROOT / "data_fl" / "spm4.jpg", media_type="image/jpeg")


@app.get("/messages")
def messages_page():
    fpath = WEB_DIR / "messages.html"
    if not fpath.exists():
        raise HTTPException(status_code=404)
    return FileResponse(fpath, media_type="text/html; charset=utf-8", headers={
        "Cache-Control": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "SAMEORIGIN",  # SAMEORIGIN not DENY — served in iframe inside index.html
        "Referrer-Policy": "strict-origin-when-cross-origin",
    })


# ── Auth ───────────────────────────────────────────────────────────────────────

@app.post("/api/auth/login")
def login(request: Request, body: dict = Body(default={})):
    ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(ip):
        raise HTTPException(status_code=429, detail="Too many login attempts — try again in 15 minutes")
    try:
        department = str(body.get("department", "")).strip().lower()
        username   = str(body.get("username", "")).strip()
        password   = str(body.get("password", ""))

        if not password:
            raise HTTPException(status_code=400, detail="password required")

        if department in DEPT_EMAILS:
            result = get_client().table("users").select(
                "id,email,password_hash,role,full_name,is_active"
            ).eq("role", department).execute()
        else:
            email = str(body.get("email", "")).strip().lower()
            if not email:
                raise HTTPException(status_code=400, detail="email required for admin login")
            result = get_client().table("users").select(
                "id,email,password_hash,role,full_name,is_active"
            ).eq("email", email).execute()

        if not result.data:
            raise HTTPException(status_code=401, detail="Invalid credentials")

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
            raise HTTPException(status_code=401, detail="Invalid email or password")

        update: dict = {"last_login_at": datetime.now(timezone.utc).isoformat()}
        if username and not matched.get("full_name"):
            update["full_name"] = username
            matched["full_name"] = username
        get_client().table("users").update(update).eq("id", matched["id"]).execute()

        _clear_rate_limit(ip)
        display_name = matched.get("full_name") or username or matched.get("email", "")
        matched["full_name"] = display_name
        token = _make_token(matched)
        return _ok({
            "token": token,
            "user": {
                "id":    str(matched["id"]),
                "email": matched["email"],
                "role":  matched["role"],
                "name":  display_name,
            },
        })
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Server error — please try again")


# ── Orders ─────────────────────────────────────────────────────────────────────

def _embed_so_line_items(orders: list) -> None:
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


@app.get("/api/orders")
def get_orders(user: dict = Depends(verify_jwt)):
    try:
        result = (
            get_client().table("orders").select(ORDER_COLS)
            .order("notification_received_at", desc=True).execute()
        )
        orders = result.data or []
        _embed_so_line_items(orders)
        return _ok(orders)
    except Exception as exc:
        print(f"  [error] get_orders: {exc}")
        raise HTTPException(status_code=500, detail="Server error")


@app.get("/api/nlng_orders")
def get_nlng_orders(user: dict = Depends(verify_jwt)):
    try:
        result = (
            get_client().table("nlng_orders").select(NLNG_ORDER_COLS)
            .order("notification_received_at", desc=True).execute()
        )
        orders = result.data or []
        _embed_so_line_items(orders)
        return _ok(orders)
    except Exception as exc:
        print(f"  [error] get_nlng_orders: {exc}")
        raise HTTPException(status_code=500, detail="Server error")


@app.get("/api/so_line_items")
def get_so_line_items(user: dict = Depends(verify_jwt)):
    try:
        result = (
            get_client().table("so_line_items")
            .select("so_number,line_no,item_number,despatch_date,qty,uom,unit_price,extended_price")
            .execute()
        )
        return _ok(result.data or [])
    except Exception as exc:
        print(f"  [error] get_so_line_items: {exc}")
        raise HTTPException(status_code=500, detail="Server error")


# ── Users ──────────────────────────────────────────────────────────────────────

@app.get("/api/users")
def get_users(user: dict = Depends(require_admin)):
    try:
        result = get_client().table("users").select(
            "id,email,full_name,role,is_active,created_at,last_login_at"
        ).order("created_at").execute()
        return _ok(result.data or [])
    except Exception:
        raise HTTPException(status_code=500, detail="Server error")


@app.post("/api/users")
def create_user(body: dict = Body(default={}), user: dict = Depends(require_admin)):
    try:
        import uuid as _uuid
        name     = str(body.get("full_name", "")).strip()
        password = str(body.get("password", ""))
        role     = str(body.get("role", ""))
        valid_roles = ("admin", "procurement", "warehouse", "expeditor", "accounts")
        if not password or role not in valid_roles:
            raise HTTPException(status_code=400, detail="password and valid role are required")
        uid6     = str(_uuid.uuid4())[:6]
        username = (name + "_" + uid6) if name else (role + "_" + uid6)
        if role in DEPT_EMAILS:
            email    = f"{username}@spm.dept"
            existing = get_client().table("users").select("id").eq("role", role).eq("is_active", True).execute()
        else:
            email = str(body.get("email", "")).strip().lower()
            if not email:
                raise HTTPException(status_code=400, detail="email required for admin accounts")
            existing = get_client().table("users").select("id").eq("email", email).eq("is_active", True).execute()
        if len(existing.data or []) >= 7:
            raise HTTPException(status_code=400, detail="Maximum 7 users per department — deactivate one before adding another")
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
        result  = get_client().table("users").insert({
            "email":         email,
            "username":      username,
            "full_name":     name or None,
            "password_hash": pw_hash,
            "role":          role,
            "is_active":     True,
        }).execute()
        if not result.data:
            raise HTTPException(status_code=500, detail="Insert failed")
        u = result.data[0]
        return _ok({"id": u["id"], "email": u["email"], "role": u["role"]})
    except HTTPException:
        raise
    except Exception as exc:
        print(f"  [error] create_user: {exc}")
        raise HTTPException(status_code=500, detail="Server error")


@app.patch("/api/users/{user_id}")
def patch_user(user_id: str, body: dict = Body(default={}), user: dict = Depends(require_admin)):
    try:
        allowed = {"full_name", "role", "is_active", "password"}
        update: dict = {}
        valid_roles = ("admin", "procurement", "warehouse", "expeditor", "accounts")
        for key in allowed:
            if key not in body:
                continue
            if key == "role" and body[key] not in valid_roles:
                raise HTTPException(status_code=400, detail=f"invalid role: {body[key]}")
            if key == "password":
                update["password_hash"] = bcrypt.hashpw(
                    str(body[key]).encode(), bcrypt.gensalt(rounds=12)
                ).decode()
            elif key == "role":
                update["role"] = body[key]
            else:
                update[key] = body[key]
        if not update:
            raise HTTPException(status_code=400, detail="nothing to update")
        exists = get_client().table("users").select("id").eq("id", user_id).execute()
        if not exists.data:
            raise HTTPException(status_code=404, detail="user not found")
        get_client().table("users").update(update).eq("id", user_id).execute()
        return _ok({"ok": True})
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Server error")


@app.delete("/api/users/{user_id}")
def delete_user(user_id: str, user: dict = Depends(require_admin)):
    try:
        res = get_client().table("users").select("id,role").eq("id", user_id).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="user not found")
        target = res.data[0]
        if target.get("role") == "admin":
            raise HTTPException(status_code=403, detail="cannot delete admin accounts")
        if str(target["id"]) == str(user.get("sub", "")):
            raise HTTPException(status_code=403, detail="cannot delete your own account")
        get_client().table("users").delete().eq("id", user_id).execute()
        return _ok({"ok": True})
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Server error")


# ── Messages ───────────────────────────────────────────────────────────────────

@app.get("/api/messages/sent")
def get_sent_messages(user: dict = Depends(verify_jwt)):
    try:
        user_id = user.get("sub", "")
        result  = get_client().table("messages").select("*").eq("from_user_id", user_id).order("created_at", desc=True).execute()
        msgs = result.data or []
        threads: dict = {}
        for m in msgs:
            tid = m.get("thread_id") or m["id"]
            if tid not in threads:
                threads[tid] = []
            threads[tid].append(m)
        thread_list = []
        for tid, tmsg in threads.items():
            latest = tmsg[0]
            thread_list.append({
                "thread_id":      tid,
                "subject":        latest.get("subject") or "",
                "message_count":  len(tmsg),
                "unread_count":   0,
                "latest_at":      latest.get("created_at"),
                "latest_from":    latest.get("from_name"),
                "latest_to_role": latest.get("to_role"),
                "latest_body":    latest.get("body") or "",
            })
        return _ok(thread_list)
    except Exception:
        raise HTTPException(status_code=500, detail="Server error")


@app.get("/api/messages/unread_count")
def get_unread_count(request: Request, user: dict = Depends(verify_jwt)):
    try:
        import urllib.parse
        caller_role = user.get("role", "")
        user_id     = user.get("sub", "")
        role = caller_role
        if caller_role == "admin":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(str(request.url)).query)
            req_role = (qs.get("role") or [None])[0]
            valid = ("admin", "procurement", "warehouse", "expeditor", "accounts")
            if req_role and req_role in valid:
                role = req_role
        result  = get_client().table("messages").select("id").eq("to_role", role).execute()
        all_ids = {m["id"] for m in (result.data or [])}
        if not all_ids:
            return _ok({"count": 0})
        read_res = get_client().table("message_reads").select("message_id").eq("user_id", user_id).execute()
        read_ids = {r["message_id"] for r in (read_res.data or [])}
        return _ok({"count": len(all_ids - read_ids)})
    except Exception:
        raise HTTPException(status_code=500, detail="Server error")


@app.get("/api/messages/thread/{thread_id}")
def get_thread_messages(thread_id: str, user: dict = Depends(verify_jwt)):
    try:
        user_id = user.get("sub", "")
        role    = user.get("role", "")
        result  = get_client().table("messages").select("*").eq("thread_id", thread_id).order("created_at", desc=False).execute()
        msgs = result.data or []
        if user_id and msgs:
            unread_ids = [m["id"] for m in msgs if m.get("to_role") == role]
            for mid in unread_ids:
                try:
                    get_client().table("message_reads").upsert(
                        {"message_id": mid, "user_id": user_id},
                        on_conflict="message_id,user_id"
                    ).execute()
                except Exception:
                    pass
        return _ok(msgs)
    except Exception:
        raise HTTPException(status_code=500, detail="Server error")


@app.get("/api/messages")
def get_messages(request: Request, user: dict = Depends(verify_jwt)):
    try:
        import urllib.parse
        caller_role = user.get("role", "")
        user_id     = user.get("sub", "")
        role = caller_role
        if caller_role == "admin":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(str(request.url)).query)
            req_role = (qs.get("role") or [None])[0]
            valid = ("admin", "procurement", "warehouse", "expeditor", "accounts")
            if req_role and req_role in valid:
                role = req_role
        result = get_client().table("messages").select("*").eq("to_role", role).order("created_at", desc=True).execute()
        msgs = result.data or []
        read_ids: set = set()
        if user_id and msgs:
            read_res = get_client().table("message_reads").select("message_id").eq("user_id", user_id).execute()
            read_ids = {r["message_id"] for r in (read_res.data or [])}
        threads: dict = {}
        for m in msgs:
            tid = m.get("thread_id") or m["id"]
            m["is_read"] = m["id"] in read_ids
            if tid not in threads:
                threads[tid] = []
            threads[tid].append(m)
        thread_list = []
        for tid, tmsg in threads.items():
            latest = tmsg[0]
            unread = sum(1 for x in tmsg if not x["is_read"])
            thread_list.append({
                "thread_id":      tid,
                "subject":        latest.get("subject") or "",
                "message_count":  len(tmsg),
                "unread_count":   unread,
                "latest_at":      latest.get("created_at"),
                "latest_from":    latest.get("from_name"),
                "latest_to_role": latest.get("to_role"),
                "latest_body":    latest.get("body") or "",
            })
        return _ok(thread_list)
    except Exception:
        raise HTTPException(status_code=500, detail="Server error")


@app.post("/api/messages/upload")
async def upload_message_attachment(file: UploadFile = File(...), user: dict = Depends(verify_jwt)):
    try:
        data = await file.read()
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="File too large — maximum is 10 MB")
        filename     = re.sub(r'[^\w\-_\.]', '_', file.filename or "attachment")
        folder_id    = secrets.token_hex(5)
        storage_path = f"msg-attachments/{folder_id}/{filename}"
        ctype        = file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        client       = get_client()
        try:
            client.storage.from_("spm-pdfs").upload(
                path=storage_path, file=data,
                file_options={"content-type": ctype, "upsert": "true"},
            )
        except Exception:
            client.storage.from_("spm-pdfs").update(
                path=storage_path, file=data,
                file_options={"content-type": ctype},
            )
        url = client.storage.from_("spm-pdfs").get_public_url(storage_path)
        return _ok({"url": url, "name": file.filename or filename})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}")


@app.post("/api/messages")
def send_message(body: dict = Body(default={}), user: dict = Depends(verify_jwt)):
    try:
        to_role  = str(body.get("to_role", ""))
        subject  = str(body.get("subject", "")).strip()
        msg_body = str(body.get("body", "")).strip()
        valid_roles = ("admin", "procurement", "warehouse", "expeditor", "accounts")
        if to_role not in valid_roles or not subject or not msg_body:
            raise HTTPException(status_code=400, detail="to_role, subject, and body are required")
        payload = {
            "from_user_id": user.get("sub"),
            "from_name":    user.get("name") or user.get("email", ""),
            "to_role":      to_role,
            "subject":      subject,
            "body":         msg_body,
            "message_type": str(body.get("message_type", "general")),
            "from_role":    user.get("role") or None,
            "order_id":     body.get("order_id") or None,
            "order_client": body.get("order_client") or None,
            "po_pdf_url":      body.get("po_pdf_url") or None,
            "attachment_url":  body.get("attachment_url") or None,
            "attachment_name": body.get("attachment_name") or None,
        }
        if body.get("thread_id"):
            payload["thread_id"] = str(body["thread_id"])
        result = get_client().table("messages").insert(payload).execute()
        if not result.data:
            raise HTTPException(status_code=500, detail="Insert failed")
        return _ok({"id": result.data[0]["id"]})
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Server error")


@app.post("/api/messages/{message_id}/read")
def mark_read(message_id: str, user: dict = Depends(verify_jwt)):
    try:
        user_id = user.get("sub", "")
        get_client().table("message_reads").upsert({
            "message_id": message_id,
            "user_id":    user_id,
        }, on_conflict="message_id,user_id").execute()
        return _ok({"ok": True})
    except Exception:
        raise HTTPException(status_code=500, detail="Server error")


# ── Gmail email viewer ─────────────────────────────────────────────────────────

def _get_po_number(order_id: str, order_type: str) -> tuple[str, str]:
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


def _fetch_gmail_emails(po_number: str, so_number: str = "") -> list:
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

        thread_data = imap.fetch(list(seed_uids), ["X-GM-THRID"])
        thread_ids  = list({v[b"X-GM-THRID"] for v in thread_data.values() if b"X-GM-THRID" in v})

        def _or_tree(items):
            if len(items) == 1:
                return items[0]
            return ["OR", items[0], _or_tree(items[1:])]

        all_uids: set[int] = set(seed_uids)
        if thread_ids:
            criteria = [["X-GM-THRID", str(tid)] for tid in thread_ids]
            try:
                all_uids.update(imap.search(_or_tree(criteria)))
            except Exception:
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
                msg  = _eml.message_from_bytes(raw)
                subj = _dec(msg.get("Subject", ""))
                if "SPM CNL Purchase Orders" in subj:
                    continue
                mid = (msg.get("Message-ID") or "").strip()
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


def _build_email_context(po_number: str, so_number: str = "") -> str:
    BODY_LIMIT  = 1_500
    CHAR_BUDGET = 15_000
    emails = _fetch_gmail_emails(po_number, so_number)
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

    formatted   = [_fmt(e) for e in emails]
    total_chars = sum(len(f) for f in formatted)

    if total_chars <= CHAR_BUDGET:
        selected = formatted
    else:
        avg_len = total_chars / n
        target  = max(9, int(CHAR_BUDGET / avg_len))
        target  = min(target, n)
        step    = (n - 1) / (target - 1) if target > 1 else 0
        indices = sorted(set(round(i * step) for i in range(target)))
        selected = [formatted[i] for i in indices]

    return "\n\n---\n\n".join(selected)


def _build_order_context(order_id: str, order_type: str) -> str:
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
        else:
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
                    desc  = it.get("description") or ""
                    qty   = f"Qty {it.get('quantity')}" if it.get("quantity") else ""
                    pn    = f"Part# {it['buyer_part_code']}" if it.get("buyer_part_code") else ""
                    prom  = f"Promised {_d(it.get('promised_date'))}" if it.get("promised_date") else ""
                    parts = " | ".join(filter(None, [qty, pn, prom]))
                    lines.append(f"  {it.get('line_no','')}. {desc}" + (f" — {parts}" if parts else ""))
        return "\n".join(lines)
    except Exception as exc:
        print(f"  [warn] _build_order_context: {exc}")
        return ""


def _load_ai_memory(order_id: str, order_type: str) -> str:
    try:
        db    = get_client()
        parts = []
        mem   = db.table("ai_memory").select("summary,updated_at") \
                  .eq("order_id", order_id).eq("order_type", order_type).execute()
        if mem.data:
            row     = mem.data[0]
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


def _save_ai_memory(order_id: str, order_type: str, summary: str) -> None:
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


def _ai_stream(messages: list, max_tokens: int = 400, system: str = ""):
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
    yield from _groq_stream(groq_messages, max_tokens=max_tokens)


def _groq_stream(messages: list, max_tokens: int = 400, model: str = "llama-3.1-8b-instant"):
    groq_key     = os.environ.get("GROQ_API_KEY", "")
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

    try:
        import urllib.request as _urlreq
        import urllib.error  as _urlerr
        req = _urlreq.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=payload_bytes,
            headers={
                "Authorization": f"Bearer {groq_key}",
                "Content-Type":  "application/json",
                "User-Agent":    "SPMProcure360/1.0",
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
        return
    except _urlerr.HTTPError as exc:
        if exc.code == 429:
            raise _GroqRateLimit("Groq rate limit (429)")
        print(f"  [groq] urllib HTTP {exc.code} — trying curl")
    except Exception as exc:
        print(f"  [groq] urllib failed ({type(exc).__name__}: {exc}) — trying curl")

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
            pass

    for line in lines:
        result = _parse_sse_line(line.strip())
        if result is StopIteration:
            break
        if result:
            yield result


@app.get("/api/emails")
def get_emails(order_id: str, type: str = "chevron", user: dict = Depends(verify_jwt)):
    try:
        po_number, so_number = _get_po_number(order_id, type)
        emails = _fetch_gmail_emails(po_number, so_number) if po_number else []
        return _ok(emails)
    except Exception:
        raise HTTPException(status_code=500, detail="Server error")


@app.get("/api/emails/summarize")
def email_summarize(order_id: str, type: str = "chevron", user: dict = Depends(verify_jwt)):
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("GROQ_API_KEY"):
        raise HTTPException(status_code=503, detail="No AI key configured (ANTHROPIC_API_KEY or GROQ_API_KEY)")

    summary_key = f"{order_id}:{type}"
    cached = _SUMMARY_CACHE.get(summary_key)
    if cached:
        cached_at, cached_text = cached
        if time.time() - cached_at < _SUMMARY_CACHE_TTL:
            def _cached_gen():
                yield f"data: {json.dumps({'c': cached_text})}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(_cached_gen(), media_type="text/event-stream", headers=_sse_headers())

    po_number, so_number = _get_po_number(order_id, type)

    def _gen():
        try:
            yield f"data: {json.dumps({'status': 'Searching Gmail thread, please wait…'})}\n\n"

            ctx_result    = [None]
            email_result  = [None]
            memory_result = [None]
            def _f_ctx():    ctx_result[0]    = _build_order_context(order_id, type)
            def _f_emails(): email_result[0]  = _build_email_context(po_number, so_number)
            def _f_memory(): memory_result[0] = _load_ai_memory(order_id, type)
            t1 = threading.Thread(target=_f_ctx,    daemon=True)
            t2 = threading.Thread(target=_f_emails, daemon=True)
            t3 = threading.Thread(target=_f_memory, daemon=True)
            t1.start(); t2.start(); t3.start()
            t1.join();  t2.join();  t3.join()

            order_ctx   = ctx_result[0]   or ""
            thread_text = email_result[0] or ""
            memory_text = memory_result[0] or ""

            yield f"data: {json.dumps({'status': 'Analysing emails…'})}\n\n"

            if not thread_text and not order_ctx:
                yield f"data: {json.dumps({'c': 'No data found for this PO yet.'})}\n\n"
                yield "data: [DONE]\n\n"
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
            for chunk in _ai_stream([{"role": "user", "content": prompt}], max_tokens=1000):
                yield f"data: {json.dumps({'c': chunk})}\n\n"
                chunks.append(chunk)
            yield "data: [DONE]\n\n"

            if chunks:
                full_summary = "".join(chunks)
                _SUMMARY_CACHE[summary_key] = (time.time(), full_summary)
                threading.Thread(
                    target=_save_ai_memory,
                    args=(order_id, type, full_summary),
                    daemon=True,
                ).start()

        except Exception as exc:
            print(f"  [error] email_summarize: {exc}")
            try:
                yield "data: [DONE]\n\n"
            except Exception:
                pass

    return StreamingResponse(_gen(), media_type="text/event-stream", headers=_sse_headers())


@app.post("/api/emails/chat")
def email_chat(body: dict = Body(default={}), user: dict = Depends(verify_jwt)):
    order_id   = body.get("order_id", "")
    order_type = body.get("type", "chevron")
    messages   = body.get("messages", [])

    if not order_id or not messages:
        raise HTTPException(status_code=400, detail="order_id and messages required")
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("GROQ_API_KEY"):
        raise HTTPException(status_code=503, detail="No AI key configured (ANTHROPIC_API_KEY or GROQ_API_KEY)")

    summary_ctx = ""
    if messages and messages[0].get("role") == "assistant":
        summary_ctx = "\n\nINITIAL PO SUMMARY:\n" + messages[0]["content"]
        messages = messages[1:]
    messages = messages[-10:]

    po_number, so_number = _get_po_number(order_id, order_type)

    def _gen():
        try:
            ctx_result    = [None]
            email_result  = [None]
            memory_result = [None]
            def _f_ctx():    ctx_result[0]    = _build_order_context(order_id, order_type)
            def _f_emails(): email_result[0]  = _build_email_context(po_number, so_number)
            def _f_memory(): memory_result[0] = _load_ai_memory(order_id, order_type)
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

            system_content = (
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
            )

            for chunk in _ai_stream(messages, system=system_content):
                yield f"data: {json.dumps({'c': chunk})}\n\n"
            yield "data: [DONE]\n\n"

        except _GroqRateLimit:
            yield f"data: {json.dumps({'c': '__RATE_LIMIT__'})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:
            print(f"  [error] email_chat: {exc}")
            yield "data: [DONE]\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream", headers=_sse_headers())


# ── SSE stream ─────────────────────────────────────────────────────────────────

@app.get("/api/sse")
async def sse_stream(token: str = Query(default="")):
    user = _verify_token(token) if token else None
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _sse_clients.add(q)
    async def _gen():
        try:
            yield f"data: {json.dumps({'type': 'connected'})}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25.0)
                    yield msg
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _sse_clients.discard(q)
    return StreamingResponse(_gen(), media_type="text/event-stream", headers=_sse_headers())


# ── Mention notifications ───────────────────────────────────────────────────────

@app.get("/api/notifications/mentions")
def get_mention_notifs(since: str = "", user: dict = Depends(verify_jwt)):
    try:
        user_id  = user.get("sub", "")
        user_res = get_client().table("users").select("role").eq("id", user_id).execute()
        role     = (user_res.data[0].get("role") if user_res.data else None) or "user"
        q = (
            get_client().table("po_comments")
            .select("id,order_id,nlng_order_id,author_name,body,created_at")
            .filter("mentioned_roles", "cs", "{" + role + "}")
        )
        if since:
            q = q.gt("created_at", since)
        result = q.order("created_at", desc=True).limit(20).execute()
        return _ok(result.data or [])
    except Exception:
        raise HTTPException(status_code=500, detail="Server error")


# ── Comments ───────────────────────────────────────────────────────────────────

@app.get("/api/comments")
def get_comments(order_id: str, type: str = "chevron", user: dict = Depends(verify_jwt)):
    try:
        col    = "nlng_order_id" if type == "nlng" else "order_id"
        result = get_client().table("po_comments").select(
            "id,user_id,author_name,author_role,body,mentioned_roles,is_system_event,created_at"
        ).eq(col, order_id).order("created_at", desc=False).execute()
        return _ok(result.data or [])
    except Exception:
        raise HTTPException(status_code=500, detail="Server error")


@app.post("/api/comments")
def post_comment(body: dict = Body(default={}), user: dict = Depends(verify_jwt)):
    try:
        order_id   = str(body.get("order_id", "")).strip()
        order_type = str(body.get("type", "chevron")).strip()
        text       = str(body.get("body", "")).strip()
        if not order_id or not text:
            raise HTTPException(status_code=400, detail="order_id and body required")
        user_id  = user.get("sub", "")
        user_res = get_client().table("users").select("full_name,role").eq("id", user_id).execute()
        if not user_res.data:
            raise HTTPException(status_code=404, detail="user not found")
        u           = user_res.data[0]
        author_name = u.get("full_name") or "Unknown"
        author_role = u.get("role") or "user"
        import re as _re
        _valid_roles = {"admin","procurement","warehouse","expeditor","accounts"}
        mentions = list({m.lower() for m in _re.findall(r'@(\w+)', text) if m.lower() in _valid_roles})
        col  = "nlng_order_id" if order_type == "nlng" else "order_id"
        row  = {col: order_id, "user_id": user_id,
                "author_name": author_name, "author_role": author_role,
                "body": text, "mentioned_roles": mentions, "is_system_event": False}
        result = get_client().table("po_comments").insert(row).execute()
        inserted = result.data[0] if result.data else {"ok": True}
        _sse_broadcast("new_comment", {"order_id": order_id, "order_type": order_type, "comment": inserted})
        return _ok(inserted)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Server error")


@app.patch("/api/comments/{comment_id}")
def patch_comment(comment_id: str, body: dict = Body(default={}), user: dict = Depends(verify_jwt)):
    try:
        text = str(body.get("body", "")).strip()
        if not text:
            raise HTTPException(status_code=400, detail="body required")
        user_id = user.get("sub", "")
        exists  = get_client().table("po_comments").select("id,user_id").eq("id", comment_id).execute()
        if not exists.data:
            raise HTTPException(status_code=404, detail="comment not found")
        if exists.data[0].get("user_id") != user_id and user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="cannot edit another user's comment")
        get_client().table("po_comments").update({"body": text}).eq("id", comment_id).execute()
        return _ok({"ok": True})
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Server error")


@app.delete("/api/comments/{comment_id}")
def delete_comment(comment_id: str, user: dict = Depends(verify_jwt)):
    try:
        user_id = user.get("sub", "")
        exists  = get_client().table("po_comments").select("id,user_id").eq("id", comment_id).execute()
        if not exists.data:
            raise HTTPException(status_code=404, detail="comment not found")
        if exists.data[0].get("user_id") != user_id and user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="cannot delete another user's comment")
        get_client().table("po_comments").delete().eq("id", comment_id).execute()
        return _ok({"ok": True})
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Server error")


# ── Order field patches ────────────────────────────────────────────────────────

def _patch_field_logic(table: str, row_id: str, field: str, body: dict) -> Response:
    try:
        if field not in body:
            raise HTTPException(status_code=400, detail=f"{field} key required")
        value = body[field]
        if value is not None:
            value = str(value).strip() or None
        exists = get_client().table(table).select("id").eq("id", row_id).execute()
        if not exists.data:
            raise HTTPException(status_code=404, detail="not found")
        result = get_client().table(table).update({field: value}).eq("id", row_id).execute()
        if not result.data:
            raise HTTPException(status_code=500, detail="update failed — no rows affected")
        return _ok({"ok": True})
    except HTTPException:
        raise
    except Exception as exc:
        print(f"  [error] _patch_field_logic: {exc}")
        raise HTTPException(status_code=500, detail="Server error")


@app.patch("/api/orders/{order_id}/req_number")
def patch_order_req_number(order_id: str, body: dict = Body(default={}), user: dict = Depends(verify_jwt)):
    return _patch_field_logic("orders", order_id, "req_number", body)


@app.patch("/api/nlng_orders/{order_id}/enquiry_number")
def patch_nlng_enquiry_number(order_id: str, body: dict = Body(default={}), user: dict = Depends(verify_jwt)):
    return _patch_field_logic("nlng_orders", order_id, "enquiry_number", body)


# ── Alerts ─────────────────────────────────────────────────────────────────────

@app.get("/api/alerts")
def get_alerts(user: dict = Depends(verify_jwt)):
    CLOSED = {"delivered", "cancelled", "closed"}
    try:
        now       = datetime.now(timezone.utc)
        two_h_ago = (now - timedelta(hours=2)).isoformat()
        today     = now.date()
        SKIP      = CLOSED | {"paid", "invoiced", "waybill_received"}

        chev_all = (get_client().table("orders")
                    .select("id,buyer_po_number,buyer_name,promised_date,required_delivery_date,"
                            "delivered_at,overall_status,created_at,delivery_requested_at")
                    .execute().data or [])
        nlng_all = (get_client().table("nlng_orders")
                    .select("id,po_number,promised_date,required_delivery_date,"
                            "delivered_at,overall_status,created_at")
                    .execute().data or [])

        chev_new    = [o for o in chev_all if (o.get("created_at") or "") >= two_h_ago]
        chev_delreq = [o for o in chev_all if (o.get("delivery_requested_at") or "") >= two_h_ago]
        nlng_new    = [o for o in nlng_all if (o.get("created_at") or "") >= two_h_ago]

        critical: list = []
        new_pos: list  = []
        del_reqs: list = []

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
            dl = _days_left(o)
            if dl is not None and dl < 7:
                critical.append({"id": "chev-" + str(o["id"]), "po": o.get("buyer_po_number"), "buyer": o.get("buyer_name"), "days_left": dl})

        for o in chev_new:
            new_pos.append({"id": "chev-" + str(o["id"]), "po": o.get("buyer_po_number"), "buyer": o.get("buyer_name")})

        for o in chev_delreq:
            del_reqs.append({"id": "chev-" + str(o["id"]), "po": o.get("buyer_po_number")})

        for o in nlng_all:
            if o.get("delivered_at") or o.get("overall_status") in CLOSED:
                continue
            dl = _days_left(o)
            if dl is not None and dl < 7:
                critical.append({"id": "nlng-" + str(o["id"]), "po": o.get("po_number"), "buyer": "NLNG", "days_left": dl})

        for o in nlng_new:
            new_pos.append({"id": "nlng-" + str(o["id"]), "po": o.get("po_number"), "buyer": "NLNG"})

        return _ok({"critical": critical, "new_pos": new_pos, "delivery_requests": del_reqs})
    except Exception:
        raise HTTPException(status_code=500, detail="Server error")


# ── Quotations ─────────────────────────────────────────────────────────────────

_UOM_MAP = {
    "EA": "EA", "EACH": "EA", "RL": "ROLL", "ROLL": "ROLL",
    "ST": "SET", "SET": "SET", "PK": "PACK", "PACK": "PACK",
    "MTR": "MTR", "METER": "MTR", "GL": "GL", "GALLON": "GL", "BOX": "BOX",
}


def _norm_uom(raw) -> str:
    if not raw:
        return "EA"
    code = str(raw).split(":")[0].strip().upper()
    return _UOM_MAP.get(code, code[:4])


def _parse_sn_desc_xlsx(rows: list, original_filename: str = "") -> dict | None:
    """Parse enquiry files with S/N · DESCRIPTION · UoM · QTY · UNIT PRICE · TOTAL format.

    The spec text lines that follow each item row (after "Specifications:") are joined
    and used as the description. Prices are carried over from the file.
    Returns None if the sheet doesn't match this format.
    """
    if not rows:
        return None

    # Detect by header row: col 0 = "S/N" or "#", col 1 contains "DESC"
    h0 = str(rows[0][0] or "").strip().upper()
    h1 = str(rows[0][1] or "").strip().upper() if len(rows[0]) > 1 else ""
    if h0 not in ("S/N", "#", "SN") or "DESC" not in h1:
        return None

    header = [str(c or "").strip().upper() for c in rows[0]]
    col_sn    = next((i for i, h in enumerate(header) if h in ("S/N", "#", "SN")), None)
    col_desc  = next((i for i, h in enumerate(header) if "DESC" in h), None)
    col_uom   = next((i for i, h in enumerate(header) if "UOM" in h or "UNIT OF" in h), None)
    col_qty   = next((i for i, h in enumerate(header) if "QTY" in h or "QUANTITY" in h), None)
    col_price = next((i for i, h in enumerate(header) if "UNIT PRICE" in h or "UNIT COST" in h), None)
    col_total = next((i for i, h in enumerate(header) if "TOTAL" in h), None)

    if col_sn is None or col_desc is None:
        return None

    currency = "USD"
    if col_price is not None:
        ph = header[col_price]
        if "NGN" in ph or "₦" in ph:
            currency = "NGN"
        elif "GBP" in ph or "£" in ph:
            currency = "GBP"

    client_name = ""
    if original_filename:
        base = original_filename.rsplit(".", 1)[0]
        for suffix in ("QUOTATION", "QUOTE", "RFQ", "ENQUIRY", "INQUIRY", "ORDER"):
            if base.upper().endswith(suffix):
                base = base[:-(len(suffix))].strip(" -_")
                break
        client_name = base.strip()

    line_items = []
    current: dict | None = None
    spec_lines: list[str] = []

    def _flush():
        if current is None:
            return
        spec_text = " ".join(spec_lines).strip()
        desc = spec_text if spec_text else current["short_desc"]
        line_items.append({
            "item_no":     current["sn"],
            "description": desc,
            "uom":         current["uom"],
            "quantity":    current["qty"],
            "unit_price":  current["unit_price"],
            "tax_rate":    0,
            "line_total":  current["line_total"],
        })

    for row in rows[1:]:
        if not row or not any(row):
            continue
        first = row[0]
        # Stop at footer rows
        if isinstance(first, str) and any(kw in first.upper() for kw in ("TOTAL", "VAT", "SUB")):
            break

        # Item row: numeric S/N in col 0
        try:
            sn = int(float(first))
            _flush()
            spec_lines = []

            short_desc = str(row[col_desc] or "").strip() if col_desc is not None and len(row) > col_desc else ""
            uom = "EA"
            if col_uom is not None and len(row) > col_uom and row[col_uom]:
                uom = _norm_uom(row[col_uom])
            qty = 1.0
            if col_qty is not None and len(row) > col_qty and row[col_qty] is not None:
                try:
                    qty = float(row[col_qty]) or 1.0
                except (TypeError, ValueError):
                    pass
            unit_price = 0.0
            if col_price is not None and len(row) > col_price and row[col_price] is not None:
                try:
                    unit_price = float(row[col_price])
                except (TypeError, ValueError):
                    pass
            line_total = round(qty * unit_price, 2)
            if col_total is not None and len(row) > col_total and row[col_total] is not None:
                try:
                    line_total = float(row[col_total])
                except (TypeError, ValueError):
                    pass
            current = {"sn": sn, "short_desc": short_desc, "uom": uom,
                       "qty": qty, "unit_price": unit_price, "line_total": line_total}

        except (TypeError, ValueError):
            # Spec / continuation row — col 0 is None
            if current is None:
                continue
            if col_desc is not None and len(row) > col_desc:
                text = str(row[col_desc] or "").strip()
                if text and text.lower() != "specifications:":
                    spec_lines.append(text)

    _flush()

    subject = f"QUOTATION FOR {client_name}" if client_name else "QUOTATION"
    return {
        "client":          "others",
        "client_name":     client_name,
        "reference_po":    "",
        "subject":         subject,
        "bill_to_company": client_name,
        "currency":        currency,
        "count":           len(line_items),
        "line_items":      line_items,
    }


@app.get("/api/quotations")
def get_quotations(user: dict = Depends(verify_jwt)):
    try:
        res = get_client().table("quotations").select("*").order("created_at", desc=True).execute()
        return _ok(res.data or [])
    except Exception as exc:
        print(f"  [error] get_quotations: {exc}")
        raise HTTPException(status_code=500, detail="Server error")


@app.post("/api/quotations/parse-enquiry")
def parse_enquiry(body: dict = Body(default={}), user: dict = Depends(verify_jwt)):
    try:
        b64              = body.get("data", "")
        original_fname   = body.get("filename", "")
        filename         = original_fname.lower()
        if not b64:
            raise HTTPException(status_code=400, detail="No file data received")
        file_bytes = base64.b64decode(b64)

        if len(file_bytes) > 20 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="File too large — maximum 20 MB")

        if filename.endswith(".pdf"):
            spm = _parse_spm_quotation_pdf(file_bytes)
            if spm is not None:
                return _ok(spm)
            return _ok(_parse_nlng_pdf(file_bytes))

        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
        _skip = {"Instructions", "teehsecirP", "Sheet"}
        data_ws = None
        for name in wb.sheetnames:
            if name not in _skip:
                data_ws = wb[name]
                break
        if data_ws is None:
            raise HTTPException(status_code=400, detail="No data sheet found in workbook")

        rows = [tuple(r) for r in data_ws.iter_rows(values_only=True)]

        # ── Try S/N · DESCRIPTION format first (other companies like SEMIIFE) ──
        sn_result = _parse_sn_desc_xlsx(rows, original_fname)
        if sn_result is not None:
            return _ok(sn_result)

        # ── Chevron GEP format: has a "Column Name:" row ───────────────────────
        title = ""
        if rows and len(rows[0]) > 2 and rows[0][2]:
            title = str(rows[0][2]).strip()

        reference_po = ""
        subject      = ""
        parts      = [p.strip() for p in title.split(" - ") if p.strip()]
        bid_number = parts[0] if parts else ""
        req_match  = re.search(r"REQ\d+", title)
        if req_match:
            reference_po = req_match.group(0)
            subject      = "QUOTATION FOR " + reference_po
        else:
            rest         = title[len(bid_number):].strip().lstrip("- ").strip()
            reference_po = rest
            subject      = "QUOTATION FOR " + rest if rest else ""

        col_map    = {}
        data_start = -1
        currency   = "USD"
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
            raise HTTPException(status_code=400, detail="Could not find column headers in file")

        intent_col    = col_map.get("Intent to Bid")
        unit_col      = col_map.get("Unit")
        volume_col    = col_map.get("Volume")
        po_text_col   = col_map.get("PO Text")
        item_name_col = col_map.get("Item name")

        line_items = []
        for row in rows[data_start:]:
            if not row or not any(row):
                continue
            if intent_col is not None and len(row) > intent_col:
                intent = row[intent_col]
                if intent not in ("Yes", "No"):
                    continue
                if intent == "No":
                    continue
            desc = ""
            if po_text_col is not None and len(row) > po_text_col and row[po_text_col]:
                desc = str(row[po_text_col]).strip()
            if not desc and item_name_col is not None and len(row) > item_name_col and row[item_name_col]:
                desc = str(row[item_name_col]).strip()
            if not desc:
                continue
            qty = 1.0
            if volume_col is not None and len(row) > volume_col and row[volume_col] is not None:
                try:
                    qty = float(row[volume_col]) or 1.0
                except (TypeError, ValueError):
                    qty = 1.0
            uom = "EA"
            if unit_col is not None and len(row) > unit_col and row[unit_col]:
                uom = _norm_uom(row[unit_col])
            line_items.append({
                "description": desc, "quantity": qty, "uom": uom,
                "unit_price": 0, "tax_rate": 0, "line_total": 0,
            })

        return _ok({
            "client":          "chevron",
            "reference_po":    reference_po,
            "subject":         subject,
            "bill_to_company": "Chevron Nigeria Limited",
            "department":      "Procurement",
            "currency":        currency,
            "count":           len(line_items),
            "line_items":      line_items,
        })
    except HTTPException:
        raise
    except Exception as exc:
        import traceback; traceback.print_exc()
        print(f"  [error] parse_enquiry: {exc}")
        raise HTTPException(status_code=500, detail="Could not parse file — check format and try again")


def _parse_spm_quotation_pdf(pdf_bytes: bytes) -> dict | None:
    """
    Parse an SPM-issued quotation PDF (letterhead + terms + line-items table).
    Returns a field dict for the quote form, or None if not an SPM quote.
    """
    import pdfplumber
    from datetime import datetime as _dt

    pages_text: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            pages_text.append(page.extract_text() or "")

    full_text = "\n".join(pages_text)

    # Detect: must look like an SPM outbound quotation
    is_spm = (
        ("Special Piping Materials" in full_text or "S.P.M" in full_text)
        and (
            "QUOTE NUMBER" in full_text
            or "S.P.M.-" in full_text
            or "SPM-0" in full_text                                          # new grid: "Our Ref: SPM-00002-MPN"
            or bool(re.search(r'QUOTATION FOR RFX', full_text, re.IGNORECASE))  # NLNG offer layout
            or bool(re.search(r'^QUOTATION\s*\n', full_text, re.MULTILINE))     # new grid heading
        )
    )
    if not is_spm:
        return None

    result: dict = {}

    # ── Subject ───────────────────────────────────────────────────────────
    m = re.search(r'Subject:\s*(.+)', full_text)
    if m:
        result["subject"] = m.group(1).strip()

    # ── Recipient block (To:\nName\nDept\nCompany\nTel:\nEmail:) ─────────
    to_m = re.search(
        r'To:\s*\n([^\n]+)\n([^\n]+)\n([^\n]+)\nTel:\s*([^\n]+)\nEmail:\s*(\S+)',
        full_text, re.IGNORECASE
    )
    if to_m:
        result["recipient_name"]  = to_m.group(1).strip()
        result["recipient_dept"]  = to_m.group(2).strip()
        result["bill_to_company"] = to_m.group(3).strip()
        result["recipient_tel"]   = to_m.group(4).strip()
        result["recipient_email"] = to_m.group(5).strip()
    else:
        em = re.search(r'Email:\s*(\S+@\S+)', full_text, re.IGNORECASE)
        if em:
            result["recipient_email"] = em.group(1).strip()

    # ── Terms & Conditions ────────────────────────────────────────────────
    m = re.search(r'Manufacturer:\s*(.+)', full_text, re.IGNORECASE)
    if m:
        result["manufacturer"] = m.group(1).strip()

    m = re.search(r'Delivery:\s*(.+)', full_text, re.IGNORECASE)
    if m:
        result["delivery_dest"] = m.group(1).strip()

    m = re.search(r'Lead\s+time:\s*(.+)', full_text, re.IGNORECASE)
    if m:
        result["lead_time"] = m.group(1).strip()

    m = re.search(r'Offer\s+Validity\s*:\s*(\d+)\s*days?', full_text, re.IGNORECASE)
    if m:
        result["validity_days"] = int(m.group(1))

    m = re.search(r'Payment\s+Terms:\s*(.+)', full_text, re.IGNORECASE)
    if m:
        result["payment_terms"] = m.group(1).strip()

    # ── Quote number ──────────────────────────────────────────────────────
    m = re.search(r'QUOTE NUMBER:\s*(S\.P\.M\.[^\n\r]+)', full_text, re.IGNORECASE)
    if m:
        result["original_quote_number"] = m.group(1).strip()
        result["reference_po"] = result["original_quote_number"]

    # ── Quote date ────────────────────────────────────────────────────────
    m = re.search(r'DATE:\s*(\d{1,2}\s+\w+\s+\d{4})', full_text, re.IGNORECASE)
    if m:
        try:
            result["quote_date"] = _dt.strptime(m.group(1).strip(), "%d %B %Y").strftime("%Y-%m-%d")
        except ValueError:
            pass

    # ── Prepared by (FROM: section, two-column layout) ────────────────────
    from_m = re.search(r'FROM:\s+(\w[\w\s\.]+?)(?=Procurement|I\.C\.T|Dept|Km3|Tel:|\n)', full_text, re.IGNORECASE)
    if from_m:
        result["prepared_by"] = from_m.group(1).strip()

    # ── New grid layout: BILL TO block ────────────────────────────────────
    # Pattern: BILL TO\nCompany\nName — Department\nemail@domain
    if not result.get("recipient_email"):
        bt_m = re.search(r'BILL\s+TO\s*\n(.+)\n(.+)\n(\S+@\S+)', full_text, re.IGNORECASE)
        if bt_m:
            result["bill_to_company"] = bt_m.group(1).strip()
            namepart = bt_m.group(2).strip()
            sep = " — " if " — " in namepart else (" - " if " - " in namepart else None)
            if sep:
                parts = namepart.split(sep, 1)
                result["recipient_name"] = parts[0].strip()
                result["recipient_dept"] = parts[1].strip()
            else:
                result["recipient_name"] = namepart
            result["recipient_email"] = bt_m.group(3).strip()

    # ── New grid layout: QUOTE DATE block ─────────────────────────────────
    if not result.get("quote_date"):
        m = re.search(r'QUOTE\s+DATE\s+(\d{1,2}\s+\w+,?\s+\d{4})', full_text, re.IGNORECASE)
        if m:
            try:
                result["quote_date"] = _dt.strptime(
                    m.group(1).strip().replace(",", ""), "%d %B %Y"
                ).strftime("%Y-%m-%d")
            except ValueError:
                pass

    # ── New grid layout: quote number from "Our Ref:" or QUOTATION heading ─
    if not result.get("original_quote_number"):
        m = re.search(r'Our\s+Ref:\s*([A-Z0-9][\w\.\-]+)', full_text, re.IGNORECASE)
        if m:
            result["original_quote_number"] = m.group(1).strip()
        else:
            m2 = re.search(r'^QUOTATION\s+(S\.P\.M\.[^\n]+)', full_text, re.MULTILINE)
            if m2:
                result["original_quote_number"] = m2.group(1).strip()

    # ── New grid layout: PREPARED BY block ───────────────────────────────
    if not result.get("prepared_by"):
        m = re.search(r'PREPARED\s+BY\s+(.+)', full_text, re.IGNORECASE)
        if m:
            result["prepared_by"] = m.group(1).strip()

    # ── New grid layout: REFERENCE REQ # / REFERENCE PO # ────────────────
    if not result.get("reference_po"):
        m = re.search(r'REFERENCE\s+(?:REQ|PO)\s*#\s+([^\n]+)', full_text, re.IGNORECASE)
        if m:
            result["reference_po"] = m.group(1).strip()

    # ── New grid layout: LEAD TIME block (no colon) ───────────────────────
    if not result.get("lead_time"):
        m = re.search(r'LEAD\s+TIME\s+([^\n]+)', full_text, re.IGNORECASE)
        if m:
            result["lead_time"] = m.group(1).strip().rstrip(",")

    # ── New grid layout: OFFER VALIDITY block (no colon) ─────────────────
    if not result.get("validity_days"):
        m = re.search(r'OFFER\s+VALIDITY\s+(\d+)\s*days?', full_text, re.IGNORECASE)
        if m:
            result["validity_days"] = int(m.group(1))

    # ── Client ────────────────────────────────────────────────────────────
    company   = (result.get("bill_to_company") or "").lower()
    subj_low  = (result.get("subject") or "").lower()
    email_low = (result.get("recipient_email") or "").lower()
    full_low  = full_text.lower()
    _lookup   = company + " " + subj_low + " " + email_low
    if "chevron" in _lookup or "cnl" in company:
        result["client"] = "chevron"
    elif "nlng" in _lookup or "nigeria lng" in _lookup:
        result["client"] = "nlng"
    elif "seplat" in _lookup:
        result["client"] = "seplat"
    elif (
        "exxon" in _lookup or "mobil" in _lookup
        or "exxonmobil" in email_low
        or "m.p.n.u" in full_low or "mpnu" in full_low
    ):
        result["client"] = "exxon"
    elif "total" in _lookup:
        result["client"] = "total"
    else:
        result["client"] = "others"

    # ── Currency ──────────────────────────────────────────────────────────
    result["currency"] = "NGN" if ("₦" in full_text or "NGN" in full_text) else (
        "GBP" if ("£" in full_text or "GBP" in full_text) else "USD"
    )

    # ── Line items via text-based state machine ───────────────────────────
    def _num(s: str) -> float:
        try:
            return float(str(s).replace(",", "").strip())
        except (TypeError, ValueError):
            return 0.0

    _UOM_RE = re.compile(r'\b(EA|PC|PCS|SET|ROLL|MTR|GL|BOX|KIT|EACH|LF|Mtr)\b', re.IGNORECASE)

    def _extract_trail(line):
        """Locate the UOM keyword and extract QTY / unit-price / line-total from the
        surrounding digit tokens.  Handles any number format: missing decimals ("2,046"),
        leading-dot blanks (". 34.46"), currency prefixes, extra spaces — all ignored."""
        m = _UOM_RE.search(line)
        if not m:
            return None
        uom      = m.group(1).upper()
        pre_text = line[:m.start()].rstrip()
        post     = re.findall(r'[\d,]+(?:\.\d+)?', line[m.end():])
        if not post:
            return None
        # QTY-first layout when a number ends the text immediately before the UOM
        is_qty_first = bool(pre_text) and pre_text[-1].isdigit()
        if is_qty_first:
            pre_iter = list(re.finditer(r'[\d,]+(?:\.\d+)?', pre_text))
            qty_mi   = pre_iter[-1] if pre_iter else None
            qty      = _num(qty_mi.group()) if qty_mi else 1.0
            up       = _num(post[0])
            lt       = _num(post[1]) if len(post) > 1 else up
            desc_end = qty_mi.start() if qty_mi else m.start()
        else:
            if len(post) < 2:
                return None
            qty      = _num(post[0])
            up       = _num(post[1])
            lt       = _num(post[2]) if len(post) > 2 else up
            desc_end = m.start()
        return {"uom": uom, "qty": qty, "unit_price": up, "line_total": lt, "uom_pos": desc_end}
    _PROD_CODE = re.compile(r'^[A-Z][A-Z0-9]{4,}$')

    # Find where the item table starts — try several header patterns
    hdr_m = (
        re.search(r'ITEM\s+No\s+ITEM\s+DESCRIPTION', full_text, re.IGNORECASE)
        or re.search(r'#\s+Item\s+Description\s+QTY', full_text, re.IGNORECASE)
        or re.search(r'S/N\s+DESCRIPTION\s+UOM', full_text, re.IGNORECASE)
        or re.search(r'Item\s+No\s+Part\s+No', full_text, re.IGNORECASE)
    )
    table_text = full_text[hdr_m.end():] if hdr_m else full_text

    line_items: list[dict] = []
    cur: dict | None = None

    def _flush():
        nonlocal cur
        if cur is None:
            return
        desc_parts = [
            l for l in cur["desc_lines"]
            if l and not re.match(r'Manufacturer\s+is\s+', l, re.IGNORECASE)
        ]
        desc_body = "\n".join(desc_parts)
        # Prepend the product/item code so it appears in the description field
        # of the quote form and the generated PDF (e.g. CHC08200876M1Y13G).
        if cur["product_no"]:
            full_desc = cur["product_no"] + ("\n" + desc_body if desc_body else "")
        else:
            full_desc = desc_body
        line_items.append({
            "item_no":     cur["item_no"],
            "product_no":  cur["product_no"],
            "description": full_desc,
            "uom":         cur["uom"],
            "quantity":    cur["qty"],
            "unit_price":  cur["unit_price"],
            "tax_rate":    0,
            "line_total":  cur["line_total"],
        })
        cur = None

    for raw in table_text.splitlines():
        line = raw.strip()
        if not line:
            continue

        if re.match(r'^(Sub\s+Total|TOTAL)\b', line, re.IGNORECASE) and not re.search(r'DESCRIPTION|ITEM', line, re.IGNORECASE):
            _flush()
            break

        t = _extract_trail(line)

        # New item: bare integer OR integer + space + product-code (A-Z start, 5+ alphanum)
        int_prefix = re.match(r'^(\d{1,2})(?:\s+([A-Z][A-Z0-9]{4,})(.*)|(\s*)$)', line)
        if int_prefix and (int_prefix.group(4) is not None or int_prefix.group(2)):
            _flush()
            item_no = int(int_prefix.group(1))
            pcode   = (int_prefix.group(2) or "").strip()
            rest    = (int_prefix.group(3) or "").strip()

            # If pcode is all-alpha (e.g. GASKET, COMPONENT) OR looks like a size-suffixed
            # description word (e.g. ELBOW12, PIPE16) rather than a real product code,
            # move it back into the description text.
            if pcode and (
                not any(c.isdigit() for c in pcode)  # pure word: PIPE, ELBOW, PLUG
                or re.match(r'^[A-Z]+\d+$', pcode)   # word+size suffix: ELBOW12, PIPE16
            ):
                rest  = (pcode + (" " + rest if rest else "")).strip()
                pcode = ""

            if t:
                mid = line[:t["uom_pos"]].strip()
                mid = re.sub(r'^\d{1,2}\s+', '', mid).strip()
                if pcode and mid.startswith(pcode):
                    mid = mid[len(pcode):].strip()
                cur = {"item_no": item_no, "product_no": pcode,
                       "desc_lines": [mid] if mid else [],
                       "uom": t["uom"], "qty": t["qty"],
                       "unit_price": t["unit_price"], "line_total": t["line_total"],
                       "numbers_done": True}
            else:
                cur = {"item_no": item_no, "product_no": pcode,
                       "desc_lines": [rest] if rest else [],
                       "uom": "EA", "qty": 1.0, "unit_price": 0.0, "line_total": 0.0,
                       "numbers_done": False}
            continue

        # Broad fallback: catches item lines whose first word is too short for int_prefix
        # (PIPE=4 chars, PLUG=4, RED.=3, TEE=3, mixed-case like "Manufacturer") — requires
        # a UOM keyword + numbers on the same line so continuation lines don't trigger this.
        elif t and re.match(r'^(\d{1,2})\s+\S', line):
            _flush()
            item_no = int(re.match(r'^(\d{1,2})', line).group(1))
            mid = line[:t["uom_pos"]].strip()
            mid = re.sub(r'^\d{1,2}\s+', '', mid).strip()
            cur = {"item_no": item_no, "product_no": "",
                   "desc_lines": [mid] if mid else [],
                   "uom": t["uom"], "qty": t["qty"],
                   "unit_price": t["unit_price"], "line_total": t["line_total"],
                   "numbers_done": True}
            continue

        if cur is not None:
            if t and not cur["numbers_done"]:
                cur["uom"]        = t["uom"]
                cur["qty"]        = t["qty"]
                cur["unit_price"] = t["unit_price"]
                cur["line_total"] = t["line_total"]
                cur["numbers_done"] = True
                prefix = line[:t["uom_pos"]].strip()
                if _PROD_CODE.match(prefix) and not cur["product_no"]:
                    cur["product_no"] = prefix
                elif prefix:
                    cur["desc_lines"].append(prefix)
            else:
                if _PROD_CODE.match(line) and not cur["product_no"]:
                    cur["product_no"] = line
                else:
                    cur["desc_lines"].append(line)

    _flush()

    # ── Fallback: pdfplumber table extraction for formats with a Part No. column ──
    # Used when the text state machine gets 0 items (e.g. old CNL format where
    # product codes start with digits like "20033K" and don't match the int_prefix regex)
    if not line_items and re.search(r'Part\s+No', full_text, re.IGNORECASE):
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as _pdf:
            for _page in _pdf.pages:
                for _tbl in (_page.extract_tables() or []):
                    for _row in _tbl:
                        if not _row or len(_row) < 5:
                            continue
                        _cells = [str(c or "").strip() for c in _row]
                        if re.match(r'Item|#|S/N|No', _cells[0], re.IGNORECASE):
                            continue
                        try:
                            _ino = int(_cells[0])
                        except ValueError:
                            continue
                        # Expected columns: Item No | Part No | Description | UOM | QTY | Unit Price | Total
                        line_items.append({
                            "item_no":     _ino,
                            "product_no":  _cells[1] if len(_cells) > 1 else "",
                            "description": _cells[2] if len(_cells) > 2 else "",
                            "uom":         _cells[3] if len(_cells) > 3 else "EA",
                            "quantity":    _num(_cells[4]) if len(_cells) > 4 else 1.0,
                            "unit_price":  _num(_cells[5]) if len(_cells) > 5 else 0.0,
                            "tax_rate":    0,
                            "line_total":  _num(_cells[6]) if len(_cells) > 6 else 0.0,
                        })

    result["line_items"] = line_items
    result["count"]      = len(line_items)

    # ── Grand total ───────────────────────────────────────────────────────
    tot_m = re.search(r'TOTAL\s*(?:\(\$\)|\(USD\)|:)?\s*\$?\s*([\d,]+\.[\d]{2})', full_text, re.IGNORECASE)
    if tot_m:
        result["sub_total"] = float(tot_m.group(1).replace(",", ""))
        result["total"]     = result["sub_total"]

    result["source"] = "spm_quote_pdf"
    print(f"  [parse_spm_quote] client={result.get('client')} items={result['count']} qnum={result.get('original_quote_number')}")
    return result


def _parse_nlng_pdf(pdf_bytes: bytes) -> dict:
    import pdfplumber

    text_parts: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text_parts.append(page.extract_text() or "")
    full_text = "\n".join(text_parts)

    rfx_match  = re.search(r"RFx\s*number[:\s]+(\d+)", full_text)
    rfx_number = rfx_match.group(1).strip() if rfx_match else ""

    curr_match = re.search(r"RFx\s*currency[:\s]+([A-Z]{3})", full_text)
    currency   = curr_match.group(1) if curr_match else "USD"

    items: list[dict] = []
    current: dict | None = None
    state = "SEEK_ITEM"
    in_rfx_section = False

    _item_row = re.compile(r"^(\d{1,3})\s+((?:[A-Z]{1,3})?\d{4,})")
    _qty_line = re.compile(r"^([\d.]+)\s+([A-Z]{1,5})$")
    _spec_noise = re.compile(
        r"^("
        r"Page \d+/\d+"
        r"|LNG Complex"
        r"|River State"
        r"|\+234"
        r"|Item\s+Product\s+no"
        r"|Quantity\s*$"
        r")",
        re.IGNORECASE,
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
        if "Terms and Conditions" in line or "TERMS AND" in line:
            break

        if state == "SEEK_ITEM":
            m = _item_row.match(line)
            if m:
                current = {"item_no": int(m.group(1)), "product_no": m.group(2), "qty": 1.0, "uom": "PC", "specs": []}
                state = "IN_HEADER"
        elif state == "IN_HEADER":
            if re.match(r"^Specifications\s*:?\s*$", line, re.IGNORECASE):
                state = "IN_SPECS"
            else:
                m = _qty_line.match(line)
                if m:
                    current["qty"] = float(m.group(1))
                    current["uom"] = m.group(2)
        elif state == "IN_SPECS":
            m = _item_row.match(line)
            if m:
                if current:
                    items.append(current)
                current = {"item_no": int(m.group(1)), "product_no": m.group(2), "qty": 1.0, "uom": "PC", "specs": []}
                state = "IN_HEADER"
            elif _spec_noise.match(line):
                pass
            else:
                current["specs"].append(line)

    if current:
        items.append(current)

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


@app.post("/api/quotations")
def create_quotation(body: dict = Body(default={}), user: dict = Depends(verify_jwt)):
    try:
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
            "prepared_by":       body.get("prepared_by") or user.get("name"),
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
            "markup_pct":        float(body.get("markup_pct") or 0),
            "markup_visible":    bool(body.get("markup_visible") if body.get("markup_visible") is not None else True),
            "shipping_charges":  float(body.get("shipping_charges") or 0),
            "sub_total":         float(body.get("sub_total") or 0),
            "total":             float(body.get("total") or 0),
            "notes":             body.get("notes"),
            "sig_name":          body.get("sig_name"),
            "created_by":        user.get("name") or user.get("email"),
        }
        res = get_client().table("quotations").insert(record).execute()
        if not res.data:
            raise HTTPException(status_code=500, detail="Insert failed")
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
        return _ok({"ok": True, "id": qid, "quote_number": qnum})
    except HTTPException:
        raise
    except Exception as exc:
        print(f"  [error] create_quotation: {exc}")
        raise HTTPException(status_code=500, detail="Server error")


@app.get("/api/quotations/{quote_id}/pdf")
def get_quotation_pdf(quote_id: str, user: dict = Depends(verify_jwt)):
    try:
        res = get_client().table("quotations").select("*").eq("id", quote_id).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Quotation not found")
        q  = res.data[0]
        li = get_client().table("quotation_line_items").select("*").eq("quotation_id", quote_id).order("item_no").execute()
        from quotation_pdf import generate_quotation_pdf
        pdf_bytes = generate_quotation_pdf(q, li.data or [])
        raw_name  = q.get("subject") or q.get("quote_number") or quote_id
        fname     = re.sub(r"[^\w\s\-]", "", raw_name).strip().replace(" ", "_") + ".pdf"
        if not fname or fname == ".pdf":
            fname = f"SPM_Quotation_{quote_id}.pdf"
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{fname}"',
                "Cache-Control": "no-cache",
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        print(f"  [error] get_quotation_pdf: {exc}")
        raise HTTPException(status_code=500, detail="PDF generation failed")


@app.get("/api/quotations/{quote_id}/export_xlsx")
def get_quotation_xlsx(quote_id: str, user: dict = Depends(verify_jwt)):
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment

        res = get_client().table("quotations").select("*").eq("id", quote_id).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Quotation not found")
        q  = res.data[0]
        li = get_client().table("quotation_line_items").select("*").eq("quotation_id", quote_id).order("item_no").execute()
        line_items = li.data or []
        is_nlng = (q.get("client") or "") == "nlng"
        sym     = "₦" if q.get("currency") == "NGN" else "$"

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Line Items"

        if is_nlng:
            headers = ["#", "Product No.", "Description", "UOM", "Qty",
                       f"Unit Price ({sym})", "Tax %", f"Total ({sym})"]
        else:
            headers = ["#", "Description", "UOM", "Qty",
                       f"Unit Price ({sym})", "Tax %", f"Total ({sym})"]

        hdr_fill = PatternFill("solid", fgColor="1E3A5F")
        hdr_font = Font(bold=True, color="FFFFFF")
        for col, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=h)
            cell.font = hdr_font
            cell.fill = hdr_fill
            cell.alignment = Alignment(horizontal="center")

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

        ws.column_dimensions["A"].width = 5
        ws.column_dimensions["B"].width = 55 if not is_nlng else 18
        ws.column_dimensions["C"].width = 8  if not is_nlng else 55

        buf        = io.BytesIO()
        wb.save(buf)
        xlsx_bytes = buf.getvalue()

        ref   = q.get("reference_po") or q.get("quote_number") or quote_id
        fname = "line_items_" + re.sub(r"[^\w\-]", "_", str(ref)) + ".xlsx"

        return Response(
            content=xlsx_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f'attachment; filename="{fname}"',
                "Cache-Control": "no-cache",
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        print(f"  [error] get_quotation_xlsx: {exc}")
        raise HTTPException(status_code=500, detail="XLSX generation failed")


@app.post("/api/quotations/{quote_id}/import_prices")
def import_prices(quote_id: str, body: dict = Body(default={}), user: dict = Depends(verify_jwt)):
    try:
        file_b64   = body.get("file_data", "")
        file_bytes = base64.b64decode(file_b64.split(",")[-1])

        rows  = []
        magic = file_bytes[:4]
        print(f"  [import_prices] file_bytes={len(file_bytes)} magic={magic!r}")
        if magic == b"PK\x03\x04":
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
            print(f"  [import_prices] sheets={wb.sheetnames}")
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
            import re as _re
            html_text   = file_bytes.decode("utf-8", errors="replace")
            _strip_tags = lambda s: _re.sub(r"<[^>]+>", "", s).strip()
            for tr in _re.findall(r"<tr[^>]*>(.*?)</tr>", html_text, _re.IGNORECASE | _re.DOTALL):
                cells = _re.findall(r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>", tr, _re.IGNORECASE | _re.DOTALL)
                row   = [_strip_tags(c) for c in cells]
                if any(row):
                    rows.append(row)
            print(f"  [import_prices] HTML parse rows={len(rows)}")

        if len(rows) < 2:
            raise HTTPException(status_code=400, detail="No data rows found in file")

        header    = [h.lower() for h in rows[0]]
        print(f"  [import_prices] sheet rows={len(rows)}, headers={rows[0]}")
        idx_no    = next((i for i, h in enumerate(header) if h == "#"), None)
        idx_price = next((i for i, h in enumerate(header) if h.startswith("unit price")), None)
        idx_tax   = next((i for i, h in enumerate(header) if h.startswith("tax")), None)

        if idx_no is None or idx_price is None:
            raise HTTPException(status_code=400, detail=f"Required columns not found — headers found: {rows[0][:8]}")

        updates: list[tuple[int, float, float]] = []
        for row in rows[1:]:
            try:
                item_no    = int(float(row[idx_no]))
                unit_price = float(row[idx_price]) if row[idx_price] else 0.0
                tax_rate   = float(row[idx_tax]) if idx_tax is not None and len(row) > idx_tax and row[idx_tax] else 0.0
                updates.append((item_no, unit_price, tax_rate))
            except (ValueError, IndexError):
                continue

        if not updates:
            raise HTTPException(status_code=400, detail="No valid rows to import")

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

        li_all    = get_client().table("quotation_line_items").select("line_total").eq("quotation_id", quote_id).execute()
        sub_total = round(sum(r.get("line_total") or 0 for r in (li_all.data or [])), 2)
        q_res     = get_client().table("quotations").select("discount_pct,markup_pct,shipping_charges").eq("id", quote_id).execute()
        q_meta    = q_res.data[0] if q_res.data else {}
        discount  = float(q_meta.get("discount_pct") or 0)
        markup    = float(q_meta.get("markup_pct") or 0)
        shipping  = float(q_meta.get("shipping_charges") or 0)
        grand     = round(sub_total - sub_total * discount / 100 + sub_total * markup / 100 + shipping, 2)
        get_client().table("quotations").update({"sub_total": sub_total, "total": grand}).eq("id", quote_id).execute()

        li_updated = get_client().table("quotation_line_items").select("*").eq("quotation_id", quote_id).order("item_no").execute()
        return _ok({"line_items": li_updated.data or [], "sub_total": sub_total, "total": grand})
    except HTTPException:
        raise
    except Exception as exc:
        print(f"  [error] import_prices: {exc}")
        raise HTTPException(status_code=500, detail="Server error")


@app.get("/api/quotations/{quote_id}")
def get_quotation(quote_id: str, user: dict = Depends(verify_jwt)):
    try:
        res = get_client().table("quotations").select("*").eq("id", quote_id).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Quotation not found")
        q  = res.data[0]
        li = get_client().table("quotation_line_items").select("*").eq("quotation_id", quote_id).order("item_no").execute()
        q["line_items"] = li.data or []
        return _ok(q)
    except HTTPException:
        raise
    except Exception as exc:
        print(f"  [error] get_quotation: {exc}")
        raise HTTPException(status_code=500, detail="Server error")


@app.put("/api/quotations/{quote_id}")
def update_quotation(quote_id: str, body: dict = Body(default={}), user: dict = Depends(verify_jwt)):
    try:
        exists = get_client().table("quotations").select("id").eq("id", quote_id).execute()
        if not exists.data:
            raise HTTPException(status_code=404, detail="Quotation not found")
        items  = body.get("line_items") or []
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
            "markup_pct":        float(body.get("markup_pct") or 0),
            "markup_visible":    bool(body.get("markup_visible") if body.get("markup_visible") is not None else True),
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
        return _ok({"ok": True})
    except HTTPException:
        raise
    except Exception as exc:
        print(f"  [error] update_quotation: {exc}")
        raise HTTPException(status_code=500, detail="Server error")


@app.delete("/api/quotations/{quote_id}")
def delete_quotation(quote_id: str, user: dict = Depends(verify_jwt)):
    try:
        exists = get_client().table("quotations").select("id").eq("id", quote_id).execute()
        if not exists.data:
            raise HTTPException(status_code=404, detail="Quotation not found")
        get_client().table("quotation_line_items").delete().eq("quotation_id", quote_id).execute()
        get_client().table("quotations").delete().eq("id", quote_id).execute()
        return _ok({"ok": True})
    except HTTPException:
        raise
    except Exception as exc:
        print(f"  [error] delete_quotation: {exc}")
        raise HTTPException(status_code=500, detail="Server error")

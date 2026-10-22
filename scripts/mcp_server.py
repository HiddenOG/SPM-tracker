"""
mcp_server.py — ask SPMprocure360 questions from Claude Code or Claude Desktop.

What this is for
----------------
The PO story in the web app answers questions about ONE order, because the
server packs that order's record and emails and hands them over. It cannot
answer anything that spans the business - "which orders are stuck?", "what did
we quote Chevron for gaskets?" - because the data for those was never in the
package.

This server closes that gap, and does it on the Claude subscription rather than
the API. There is no model in here and no ANTHROPIC_API_KEY: it is a set of
read-only lookups, and the thinking comes from whatever connects to it. Opened
from Claude Code or Claude Desktop, that is the subscription already paid for,
so these questions cost nothing per answer. The web app's own chat still uses
the API, because a browser user cannot borrow a personal subscription.

Safety
------
SUPABASE_KEY is the service_role key. It bypasses every row-level rule in the
database, so a server holding it could read all 96 quotations, every price and
every customer. What keeps that in bounds:

  * Nothing here can change business data. No tool writes, updates or deletes
    an order, a quotation, a line item, a stock count or a user, and there is
    no delete and no rpc anywhere in the file.

    One exception, stated plainly because it is the only one:
    refresh_order_mail appends the Gmail thread it fetched to the emails table
    for one order, and records that the thread was walked. That is the same
    write email_thread_sync makes on its own schedule, it only ever adds mail
    that already exists in Gmail, and it cannot reach any other table. Every
    other tool is strictly read-only.

  * Narrow. Each tool answers one shape of question. There is no
    "run this SQL" tool, which would hand the whole database over in one step,
    and no free-text Gmail search - the mailbox holds everything, while the
    emails table holds order correspondence.

  * Local. It speaks over stdio to a client on this machine. It opens no port
    and must not be exposed to the internet while it holds that key.

Install and register
--------------------
    pip install -r requirements-mcp.txt
    claude mcp add spm -- <venv python> scripts/mcp_server.py

Then ask, in Claude Code:
    "which chevron orders have been stuck more than 3 weeks?"
    "what did we quote Chevron for spiral wound gaskets this year?"
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, ".env"))

# mcp 2.x: FastMCP was renamed MCPServer and moved. Pinning mcp<2 to keep the
# old import would mean installing a superseded SDK for a brand new file.
from mcp.server.mcpserver import MCPServer

from db import get_client

_INSTRUCTIONS = (
    "Read-only lookups over SPMprocure360: Chevron and NLNG orders, "
    "quotations, stored email threads and counted stock. Nothing here "
    "writes. Every tool caps its own output, so ask a narrow question "
    "rather than expecting a full table."
)

# Which transport is decided here, before the tools are registered, because
# authentication is a constructor argument. Building a second server for HTTP
# and copying the tools onto it is the obvious alternative and the wrong one -
# a tool added to one and not the other would be missing over HTTP and nothing
# would say so.
_HTTP = "--http" in sys.argv or os.environ.get("MCP_TRANSPORT") == "http"


# Connector tokens carry aud="mcp", and that one claim separates them from
# website logins in both directions, with no change to web/app.py:
#
#   a connector token on the website  -> InvalidAudienceError
#   a login token on this server      -> MissingRequiredClaimError
#
# Which is what makes a long-lived connector token reasonable. claude.ai sends
# a FIXED header on every request, so a 7-day login token would need re-pasting
# every week; a year-long one would be alarming if it doubled as a login. It
# does not. A leaked connector token reads through these tools and cannot sign
# in as anybody, and disabling the account still shuts both off.
MCP_AUDIENCE = "mcp"


class SPMTokenVerifier:
    """Accepts a token minted for this server by an active SPM account.

    Deliberately a verifier, not a full OAuth authorization server. The docs
    are clear that claude.ai accepts a fixed credential in a request header, so
    the OAuth dance would be a day's work to replace one pasted string.

    Mirrors verify_jwt in web/app.py, including the live account check: a token
    stays cryptographically valid until it expires, so without looking the user
    up, disabling an account would not actually shut the door.
    """

    async def verify_token(self, token: str):
        from mcp.server.auth.provider import AccessToken

        secret = os.environ.get("JWT_SECRET", "")
        if not secret or not token:
            return None
        try:
            import jwt as pyjwt
            payload = pyjwt.decode(token, secret, algorithms=["HS256"],
                                   audience=MCP_AUDIENCE)
        except Exception:
            # Includes a plain login token, which is missing the audience and
            # is refused here on purpose.
            return None

        user_id = str(payload.get("sub") or "")
        if not user_id:
            return None
        try:
            res = get_client().table("users").select(
                "id,role,is_active,email").eq("id", user_id).execute()
        except Exception:
            # Database unreachable. The web app lets a valid token through here
            # so the site stays usable; this does not. A read-only lookup tool
            # is not worth opening on an unverifiable account.
            print("  [mcp] user lookup failed - refusing the token")
            return None
        if not res.data or not res.data[0].get("is_active"):
            return None

        row = res.data[0]
        return AccessToken(
            token=token,
            client_id=row.get("email") or user_id,
            # The live role, not the one in the token, so a demotion applies
            # without waiting for the token to expire.
            scopes=[f"role:{row.get('role') or 'user'}"],
            expires_at=payload.get("exp"),
            subject=user_id,
        )




def _build_server() -> MCPServer:
    """The server. Authentication is NOT configured through the SDK here.

    It was, via AuthSettings and token_verifier, and that was wrong in a way
    only a real client showed. Those settings make the server advertise OAuth:
    its 401 carries a WWW-Authenticate pointing at
    /.well-known/oauth-protected-resource. claude.ai reads that, concludes the
    server speaks OAuth, and tries to register as a client - which fails,
    because nothing here issues tokens:

        "Couldn't register with SPM's sign-in service. You can try again, or
         add an OAuth Client ID in the connector settings."

    It never offers the request-header option, because the server said it did
    not need one. So the token check moved to plain middleware (see
    _http_app): a 401 with no OAuth hint, which is what a client needs to see
    before it will just send the header it was given.
    """
    return MCPServer(name="spm", instructions=_INSTRUCTIONS)


# Built once, here, so every tool below registers on the one server whichever
# transport is in use.
mcp = _build_server()


# Hard ceiling on every tool. A question whose honest answer is "most of the
# database" should come back trimmed and say so, rather than returning
# thousands of rows into a context window.
MAX_ROWS = 60

# The pipeline, in order, with the column that records each step. Used to work
# out where an order has stopped: the last stamped stage is where it sits.
_CHEVRON_STAGES = [
    ("notification_received_at",      "PO notification received"),
    ("acknowledged_at",               "acknowledged on GEP"),
    ("sent_to_warehouse_at",          "sent to warehouse for stock check"),
    ("stock_check_completed_at",      "stock check completed"),
    ("spm_po_drafted_at",             "SPM PO drafted"),
    ("spm_po_sent_at",                "SPM PO sent to supplier"),
    ("so_received_at",                "supplier acknowledged (SO)"),
    ("so_sent_to_warehouse_at",       "SO forwarded to warehouse"),
    ("flex_dispatch_ready_at",        "supplier says packed and ready"),
    ("dispatch_instructions_sent_at", "dispatch instructions sent"),
    ("ready_for_dispatch_at",         "collection arranged"),
    ("dispatched_at",                 "dispatched"),
    # Client-initiated and easy to miss: it was left out of the first version
    # of this list, which inflated how long 34 orders looked stuck. It does not
    # sit at a fixed point in the sequence - a client can chase delivery at
    # several stages - which is part of why the most recent stamp is what
    # counts here rather than a position in a list.
    ("delivery_requested_at",         "client requested delivery"),
    ("delivered_at",                  "delivered"),
]
# NLNG has no GEP acknowledgement step - the order arrives as a PDF.
_NLNG_STAGES = [s for s in _CHEVRON_STAGES if s[0] != "acknowledged_at"]

_TERMINAL = {"delivered", "cancelled"}


def _d(value) -> str:
    return str(value)[:10] if value else "—"


def _plain(html) -> str:
    """Flatten a rich-text description to plain words.

    Line item descriptions are stored as the quote editor's raw innerHTML, so
    they arrive carrying markup: a live call came back with
    'Flexitallic Style "CGI" Spiral Wound Gasket.<br>NPS -3/4"<' - truncated
    mid-tag, with the tags eating the width the description needed.
    """
    import html as _h
    import re

    text = re.sub(r"<br\s*/?>|</(?:p|div|li|tr|h[1-6])\s*>", " ", str(html or ""),
                  flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return " ".join(_h.unescape(text).replace("\xa0", " ").split())


def _days_since(iso: str) -> int | None:
    try:
        when = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - when).days
    except Exception:
        return None


def _where_it_sits(row: dict, stages: list) -> tuple[str, str, int | None]:
    """The order's most recent activity: what it was, when, and days since.

    The MOST RECENT stamp, not the furthest along a fixed sequence. An earlier
    version walked the stage list backwards and took the last one reached,
    which was wrong in a way that read as authoritative:

        PO 0061378648 was reported as 220 days at "SPM PO sent to supplier",
        when delivery_requested_at was stamped 2026-08-05 - about 74 days.
        PO 0061409193 was reported as 171 days at "stock check completed",
        when its delivery request came in on 2026-10-02, seventeen days before.

    Two causes. delivery_requested_at was missing from the stage list
    altogether, inherited from _build_order_context which also omits it, and 34
    orders carry that status. And a fixed sequence cannot answer "has this
    moved lately?" anyway: these stages do not happen in a strict order, so the
    furthest one reached is not the latest thing that happened.

    created_at and updated_at are deliberately excluded. They are row
    bookkeeping, not activity - PO 0061367679 has created_at 2026-07-06, months
    after its last real event, from when the row was written.
    """
    best = None
    for col, label in stages:
        when = row.get(col)
        if when and (best is None or str(when) > str(best[0])):
            best = (when, label)
    if best is None:
        return "nothing stamped yet", "—", None
    return best[1], _d(best[0]), _days_since(best[0])


# ── orders ───────────────────────────────────────────────────────────────────

@mcp.tool()
def orders_stuck(days: int = 14, client: str = "", limit: int = 25) -> str:
    """Orders that have not moved for a while - what needs chasing.

    An order is counted when the most recent stage it reached was stamped more
    than `days` ago and it is neither delivered nor cancelled.

    Args:
        days: how long without movement counts as stuck. Default 14.
        client: "chevron" or "nlng" to narrow it; blank for both.
        limit: most rows to return.
    """
    db = get_client()
    want = (client or "").strip().lower()
    out: list[tuple[int, str]] = []

    for table, otype, po_col, stages in (
        ("orders", "chevron", "buyer_po_number", _CHEVRON_STAGES),
        ("nlng_orders", "nlng", "po_number", _NLNG_STAGES),
    ):
        if want and want != otype:
            continue
        try:
            rows = db.table(table).select("*").execute().data or []
        except Exception as exc:
            return f"Could not read {table}: {exc}"
        for r in rows:
            if (r.get("overall_status") or "").lower() in _TERMINAL:
                continue
            label, when, waited = _where_it_sits(r, stages)
            if waited is None or waited < days:
                continue
            out.append((waited, "  %-7s PO %-12s  %3d days at: %-34s (since %s)  status=%s"
                        % (otype, r.get(po_col) or "?", waited, label, when,
                           r.get("overall_status") or "?")))

    if not out:
        return f"No {want or 'Chevron or NLNG'} orders have been still for {days}+ days."
    out.sort(reverse=True)
    shown = out[:min(limit, MAX_ROWS)]
    head = f"{len(out)} order(s) still for {days}+ days, longest first"
    if len(shown) < len(out):
        head += f" - showing {len(shown)}"
    return head + ":\n" + "\n".join(line for _, line in shown)


@mcp.tool()
def find_orders(query: str, limit: int = 20) -> str:
    """Find orders by PO number, SO number, SPM PO number or buyer name.

    Partial matches work, and a leading-zero variant is tried too, because
    Chevron PO numbers get written both ways.

    Args:
        query: what to look for.
        limit: most rows to return.
    """
    q = (query or "").strip()
    if not q:
        return "Give me a PO number, SO number or buyer name to look for."
    db = get_client()
    variants = {q, q.lstrip("0")}
    if q.isdigit():
        variants.add(q.zfill(10))

    found: dict[str, str] = {}
    for table, otype, po_col, name_col, stages in (
        ("orders", "chevron", "buyer_po_number", "buyer_name", _CHEVRON_STAGES),
        ("nlng_orders", "nlng", "po_number", "contact_name", _NLNG_STAGES),
    ):
        try:
            rows = db.table(table).select("*").execute().data or []
        except Exception as exc:
            return f"Could not read {table}: {exc}"
        for r in rows:
            haystack = " ".join(str(r.get(c) or "") for c in
                                (po_col, "so_number", "spm_po_number", name_col))
            if not any(v and v.lower() in haystack.lower() for v in variants):
                continue
            label, when, waited = _where_it_sits(r, stages)
            found[r["id"]] = (
                "  %-7s PO %-12s SO %-12s  %s\n"
                "            at: %s (since %s%s)"
                % (otype, r.get(po_col) or "?", r.get("so_number") or "—",
                   r.get("overall_status") or "?", label, when,
                   f", {waited} days" if waited is not None else "")
            )

    if not found:
        return f"Nothing matches {q!r}."
    rows = list(found.values())[:min(limit, MAX_ROWS)]
    return f"{len(found)} match(es) for {q!r}:\n" + "\n".join(rows)


@mcp.tool()
def order_detail(po_number: str) -> str:
    """Everything on one order: every stage date, the value, and its story.

    The story is the summary already written and saved for the web app's story
    drawer, so this reuses work rather than paying to write it again.

    Args:
        po_number: the client PO number, with or without leading zeros.
    """
    po = (po_number or "").strip()
    if not po:
        return "Which PO number?"
    db = get_client()

    for table, otype, po_col, stages in (
        ("orders", "chevron", "buyer_po_number", _CHEVRON_STAGES),
        ("nlng_orders", "nlng", "po_number", _NLNG_STAGES),
    ):
        row = None
        for cand in (po, po.lstrip("0"), po.zfill(10)):
            try:
                res = db.table(table).select("*").eq(po_col, cand).execute()
            except Exception as exc:
                return f"Could not read {table}: {exc}"
            if res.data:
                row = res.data[0]
                break
        if not row:
            continue

        lines = [f"{otype.upper()} PO {row.get(po_col)}   status: {row.get('overall_status') or '?'}"]
        if otype == "chevron":
            lines.append(f"  buyer: {row.get('buyer_name') or '—'}   "
                         f"value: {row.get('po_currency') or ''} {row.get('po_amount') or '—'}")
            if row.get("req_number"):
                lines.append(f"  REQ#: {row['req_number']}")
        else:
            lines.append(f"  contact: {row.get('contact_name') or '—'}   "
                         f"value: {row.get('currency') or ''} {row.get('net_value') or '—'}")
            if row.get("enquiry_number"):
                lines.append(f"  enquiry#: {row['enquiry_number']}")
        lines.append(f"  SPM PO: {row.get('spm_po_number') or '—'}   "
                     f"SO: {row.get('so_number') or '—'}")
        lines.append(f"  required delivery: {_d(row.get('required_delivery_date'))}   "
                     f"promised: {_d(row.get('promised_date'))}")

        lines.append("\n  pipeline:")
        for col, label in stages:
            mark = "x" if row.get(col) else " "
            lines.append(f"    [{mark}] {label:<34} {_d(row.get(col))}")

        label, when, waited = _where_it_sits(row, stages)
        if waited is not None and (row.get("overall_status") or "").lower() not in _TERMINAL:
            lines.append(f"\n  sitting at: {label} for {waited} days")

        try:
            mem = db.table("ai_memory").select("summary,updated_at") \
                    .eq("order_id", row["id"]).eq("order_type", otype).execute()
            if mem.data:
                lines.append(f"\n  story (written {_d(mem.data[0].get('updated_at'))}):")
                lines.append("  " + (mem.data[0].get("summary") or "").strip())
        except Exception:
            pass
        return "\n".join(lines)

    return f"No order found with PO {po!r}."


# ── quotations ───────────────────────────────────────────────────────────────

# Words too common to narrow a search ("gasket FOR 36 x 300").
_STOP = {"a", "an", "and", "the", "for", "of", "with", "to", "in", "on", "x", "by", "or"}


def _flat(s) -> str:
    """Lower case, letters and digits only: "SPIRALWOUND36IN, SS-300#" -> "spiralwound36inss300"."""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _search_words(text) -> list:
    """The words and numbers to look for, flattened the same way."""
    return [w for w in (_flat(t) for t in re.split(r"[^A-Za-z0-9]+", text or ""))
            if w and w not in _STOP]


def _matches(words, *texts) -> bool:
    """Every word present. A number must stand on its own - "36" matches
    "36IN" and '36"' but not "316" - letters may run into anything."""
    if not words:
        return False
    plain = " ".join(_plain(t).lower() for t in texts if t)
    hay = _flat(plain)
    for w in words:
        if w.isdigit():
            if not re.search(rf"(?<![0-9]){w}(?![0-9])", plain):
                return False
        elif w not in hay:
            return False
    return True


def _item_matches(words, it) -> bool:
    return _matches(words, it.get("description"), it.get("part_number"))


@mcp.tool()
def search_quotes(text: str = "", client: str = "", since: str = "",
                  limit: int = 20) -> str:
    """Quotations, searchable by what was quoted.

    `text` is matched against line item descriptions and part numbers as well
    as the quote subject, so "spiral wound gasket" finds quotes containing that
    item rather than only ones with it in the title. Every word and number in
    `text` must appear, but spacing and punctuation are ignored, so
    "spiral wound 36 300" finds "GASKET:SPIRALWOUND36IN,SS300LB".

    Args:
        text: words to look for in items or the subject.
        client: chevron, nlng, exxon, total, seplat, gca, others.
        since: ISO date, e.g. "2026-01-01", to limit how far back.
        limit: most quotes to return.
    """
    db = get_client()
    try:
        quotes = db.table("quotations").select(
            "id,quote_number,client,recipient_company,subject,quote_date,"
            "currency,total,status").execute().data or []
    except Exception as exc:
        return f"Could not read quotations: {exc}"

    want_client = (client or "").strip().lower()
    if want_client:
        quotes = [q for q in quotes if (q.get("client") or "").lower() == want_client]
    if since:
        quotes = [q for q in quotes if str(q.get("quote_date") or "") >= since]

    needle = (text or "").strip().lower()
    words = _search_words(text)
    items_by_quote: dict[str, list] = {}
    if needle:
        try:
            items = db.table("quotation_line_items").select(
                "quotation_id,description,part_number,quantity,unit_price,uom"
            ).execute().data or []
        except Exception as exc:
            return f"Could not read line items: {exc}"
        for it in items:
            items_by_quote.setdefault(it["quotation_id"], []).append(it)

        def _hit(q):
            if _matches(words, q.get("subject")):
                return True
            return any(_item_matches(words, i) for i in items_by_quote.get(q["id"], []))

        quotes = [q for q in quotes if _hit(q)]

    if not quotes:
        bits = [b for b in (repr(text) if text else "", want_client, since) if b]
        return "No quotations match " + (", ".join(bits) or "that") + "."

    quotes.sort(key=lambda q: str(q.get("quote_date") or ""), reverse=True)
    shown = quotes[:min(limit, MAX_ROWS)]
    out = [f"{len(quotes)} quotation(s)" +
           (f" - showing {len(shown)}, newest first" if len(shown) < len(quotes)
            else ", newest first") + ":"]
    for q in shown:
        out.append("  %-22s %-10s %-28s %s %s" % (
            q.get("quote_number") or "?", _d(q.get("quote_date")),
            (q.get("recipient_company") or q.get("client") or "")[:28],
            q.get("currency") or "", q.get("total") or "—"))
        if q.get("subject"):
            out.append(f"        {str(q['subject'])[:120]}")
        # Only the matching items, so a 40-line quote does not fill the reply -
        # but each in full: the size and rating sit at the END of a
        # Flexitallic description, which a short cut used to drop.
        for it in items_by_quote.get(q["id"], []):
            if words and _item_matches(words, it):
                desc = _plain(it.get("description"))
                part = f" [part {it['part_number']}]" if it.get("part_number") else ""
                out.append("        - %s%s · qty %s %s · unit price %s" % (
                    desc[:300], part, it.get("quantity") or "?", it.get("uom") or "",
                    it.get("unit_price") if it.get("unit_price") is not None else "?"))
    return "\n".join(out)


# ── email ────────────────────────────────────────────────────────────────────

# The mail tools below are deliberately general rather than one tool per
# question. An earlier version had a single search that took only a word and
# returned fixed excerpts, which meant only the questions it had been built for
# could be asked. These three - filter, read a whole thread, read one message -
# let any question about the correspondence be answered by combining them.
#
# A character budget rather than only a row count, because message bodies vary
# from one line to forty. Whatever is cut is said out loud, so an answer is
# never built on a thread that was silently truncated.
_MAIL_CHARS = 60_000


def _order_ids_for_po(db, po_number: str) -> list[tuple[str, str]]:
    """[(order_id, order_type)] for a PO, trying leading-zero variants."""
    found = []
    po = (po_number or "").strip()
    if not po:
        return found
    for table, otype, po_col in (("orders", "chevron", "buyer_po_number"),
                                 ("nlng_orders", "nlng", "po_number")):
        for cand in {po, po.lstrip("0"), po.zfill(10)}:
            try:
                res = db.table(table).select("id").eq(po_col, cand).execute()
            except Exception:
                continue
            for r in res.data or []:
                found.append((r["id"], otype))
    return found


def _mail_rows(db):
    return db.table("emails").select(
        "message_id,subject,from_address,to_address,received_at,direction,"
        "body_text,order_id,nlng_order_id").execute().data or []


def _coverage_note(total: int) -> str:
    return (f"({total} message(s) stored. Threads are archived per order by "
            f"email_thread_sync; an order it has not walked yet holds only the "
            f"mail a listener recognised. refresh_order_mail(po) fetches one "
            f"order's thread from Gmail now.)")


@mcp.tool()
def search_mail(text: str = "", sender: str = "", recipient: str = "",
                subject: str = "", since: str = "", until: str = "",
                direction: str = "", po_number: str = "",
                limit: int = 20, full_bodies: bool = False) -> str:
    """Search the stored email. Any combination of filters; all are optional.

    Filters combine with AND. Give none and you get the most recent mail, which
    is a reasonable way to see what the archive holds.

    Args:
        text: words appearing anywhere in the subject or body.
        sender: part of a from address or name, e.g. "flexitallic" or "penny".
        recipient: part of a to address.
        subject: words in the subject only.
        since: ISO date, e.g. "2026-09-01".
        until: ISO date.
        direction: "in" for received, "out" for sent by SPM, blank for both.
        po_number: limit to one order's thread.
        limit: most messages to return.
        full_bodies: true returns whole message bodies rather than an excerpt
            around the match. Use when you need to read what was actually said.
    """
    db = get_client()
    try:
        rows = _mail_rows(db)
    except Exception as exc:
        return f"Could not read emails: {exc}"
    total = len(rows)

    if po_number:
        ids = _order_ids_for_po(db, po_number)
        if not ids:
            return f"No order found with PO {po_number!r}."
        keep = {i for i, _ in ids}
        rows = [r for r in rows
                if r.get("order_id") in keep or r.get("nlng_order_id") in keep]

    low = (text or "").strip().lower()
    if low:
        rows = [r for r in rows
                if low in (r.get("subject") or "").lower()
                or low in (r.get("body_text") or "").lower()]
    if sender:
        s = sender.lower()
        rows = [r for r in rows if s in (r.get("from_address") or "").lower()]
    if recipient:
        t = recipient.lower()
        rows = [r for r in rows if t in (r.get("to_address") or "").lower()]
    if subject:
        sj = subject.lower()
        rows = [r for r in rows if sj in (r.get("subject") or "").lower()]
    if since:
        rows = [r for r in rows if str(r.get("received_at") or "") >= since]
    if until:
        # Inclusive of the whole day given, so until="2026-09-30" includes it.
        rows = [r for r in rows if str(r.get("received_at") or "")[:10] <= until[:10]]
    if direction in ("in", "out"):
        rows = [r for r in rows if (r.get("direction") or "in") == direction]

    if not rows:
        return "Nothing stored matches those filters.\n" + _coverage_note(total)

    rows.sort(key=lambda r: r.get("received_at") or "", reverse=True)
    out, used, shown = [], 0, 0
    for r in rows[:min(limit, MAX_ROWS)]:
        body = " ".join((r.get("body_text") or "").split())
        if full_bodies:
            chunk = body
        elif low and low in body.lower():
            at = body.lower().find(low)
            chunk = "…" + body[max(0, at - 120):at + 260] + "…"
        else:
            chunk = body[:260] + ("…" if len(body) > 260 else "")
        block = (
            "  [%s] %-4s %s\n          from %s\n          to   %s\n          id   %s\n          %s"
            % (_d(r.get("received_at")) if r.get("received_at") else "undated",
               "SENT" if r.get("direction") == "out" else "RECV",
               (r.get("subject") or "(no subject)")[:70],
               (r.get("from_address") or "?")[:70],
               (r.get("to_address") or "?")[:70],
               r.get("message_id") or "?",
               chunk))
        if used + len(block) > _MAIL_CHARS and shown:
            break
        out.append(block)
        used += len(block)
        shown += 1

    head = f"{len(rows)} message(s) match"
    if shown < len(rows):
        head += f" - showing {shown}, newest first (ask for more, or narrow the filters)"
    else:
        head += ", newest first"
    tail = "" if full_bodies else \
        "\n\nBodies are excerpts. Pass full_bodies=true, or use read_mail_thread, to read them in full."
    return head + ":\n" + "\n\n".join(out) + tail


@mcp.tool()
def read_mail_thread(po_number: str, full_bodies: bool = True,
                     since: str = "") -> str:
    """One order's whole conversation, oldest first, for reading end to end.

    This is the tool for "what actually happened on this order" - it returns
    the correspondence itself rather than a summary, so the reasoning is done
    on what people wrote.

    Args:
        po_number: the client PO number, with or without leading zeros.
        full_bodies: whole bodies (default) or first lines only.
        since: ISO date, to read only from a point onwards.
    """
    db = get_client()
    ids = _order_ids_for_po(db, po_number)
    if not ids:
        return f"No order found with PO {po_number!r}."
    keep = {i for i, _ in ids}
    try:
        rows = [r for r in _mail_rows(db)
                if r.get("order_id") in keep or r.get("nlng_order_id") in keep]
    except Exception as exc:
        return f"Could not read emails: {exc}"
    if since:
        rows = [r for r in rows if str(r.get("received_at") or "") >= since]
    if not rows:
        return (f"No mail stored for PO {po_number}. "
                f"Try refresh_order_mail({po_number!r}) to fetch it from Gmail.")

    # Oldest first, undated ahead of dated: an undated message is of unknown
    # age, and putting it last would read as the most recent thing that
    # happened.
    rows.sort(key=lambda r: r.get("received_at") or "")
    out, used, shown = [], 0, 0
    for r in rows:
        body = " ".join((r.get("body_text") or "").split())
        if not full_bodies:
            body = body[:300] + ("…" if len(body) > 300 else "")
        block = "  [%s] %s\n  from %s\n  subject: %s\n  %s" % (
            _d(r.get("received_at")) if r.get("received_at") else "undated",
            "SENT by SPM" if r.get("direction") == "out" else "RECEIVED",
            (r.get("from_address") or "?")[:76],
            (r.get("subject") or "(no subject)")[:76],
            body)
        if used + len(block) > _MAIL_CHARS and shown:
            break
        out.append(block)
        used += len(block)
        shown += 1

    head = f"PO {po_number}: {len(rows)} message(s), oldest first"
    if shown < len(rows):
        head += (f" - showing the first {shown}. The rest were cut to stay "
                 f"within a sensible size; pass since=<date> to read on from "
                 f"where this stops")
    return head + ":\n\n" + "\n\n---\n\n".join(out)


@mcp.tool()
def read_message(message_id: str) -> str:
    """One message in full, by the id that search_mail prints.

    Args:
        message_id: the Message-ID, e.g. "<abc123@mail.gmail.com>".
    """
    mid = (message_id or "").strip()
    if not mid:
        return "Which message id? search_mail prints one for each result."
    db = get_client()
    try:
        res = db.table("emails").select("*").eq("message_id", mid).execute()
    except Exception as exc:
        return f"Could not read emails: {exc}"
    if not res.data:
        return f"No stored message with id {mid!r}."
    r = res.data[0]
    return "\n".join([
        f"date:      {r.get('received_at') or 'undated'}",
        f"direction: {'SENT by SPM' if r.get('direction') == 'out' else 'RECEIVED'}",
        f"from:      {r.get('from_address') or '?'}",
        f"to:        {r.get('to_address') or '?'}",
        f"subject:   {r.get('subject') or '(no subject)'}",
        f"id:        {r.get('message_id')}",
        "",
        (r.get("body_text") or "(empty body)").strip(),
    ])


# ── stock ────────────────────────────────────────────────────────────────────

@mcp.tool()
def refresh_order_mail(po_number: str) -> str:
    """Fetch one order's Gmail thread now, for when the archive looks thin.

    SLOW - expect 30 seconds to several minutes. A login to this mailbox was
    measured at 28 seconds and a 145-message thread took over 7 minutes. Use it
    when search_mail or order_detail looks short on an order, not routinely:
    email_thread_sync walks every order on its own schedule and the table is
    the fast path.

    Bounded to one order on purpose. There is no free-text Gmail search here,
    because the mailbox holds everything while the emails table holds order
    correspondence, and because Gmail caps simultaneous IMAP connections - the
    worker already holds several, and ad-hoc searches competing for them would
    break the pipeline listeners, which matter more than a lookup.

    Args:
        po_number: the client PO number, with or without leading zeros.
    """
    po = (po_number or "").strip()
    if not po:
        return "Which PO number?"
    try:
        import email_thread_sync as ets
    except Exception as exc:
        return f"The archiver is unavailable: {exc}"

    db = get_client()
    for table, otype, po_col in (("orders", "chevron", "buyer_po_number"),
                                 ("nlng_orders", "nlng", "po_number")):
        row = None
        for cand in (po, po.lstrip("0"), po.zfill(10)):
            try:
                res = db.table(table).select(f"id,{po_col},so_number").eq(po_col, cand).execute()
            except Exception as exc:
                return f"Could not read {table}: {exc}"
            if res.data:
                row = res.data[0]
                break
        if not row:
            continue

        before = len(ets.read_thread(row["id"], otype))
        try:
            written = ets.sync_thread(row["id"], otype, row.get(po_col) or "",
                                      row.get("so_number") or "")
        except ets.ThreadSyncFailed as exc:
            # Said plainly, because the silent version of this was a real bug:
            # a failed search once reported as "0 emails" and stamped the order
            # as walked, so a thread nobody had read looked like a thread with
            # nothing in it.
            return (f"Gmail could not be read for PO {row.get(po_col)}: {exc}\n"
                    f"Nothing was stamped, so the archiver will try again. "
                    f"{before} message(s) already stored.")
        except Exception as exc:
            return f"Sync failed for PO {row.get(po_col)}: {exc}"

        after = len(ets.read_thread(row["id"], otype))
        return (f"{otype} PO {row.get(po_col)}: fetched {written} message(s) "
                f"from Gmail.\nStored for this order: {before} -> {after} "
                f"(+{after - before} new).\n"
                f"search_mail and order_detail will now see them.")

    return f"No order found with PO {po!r}."


@mcp.tool()
def stock_on_hand(text: str = "", limit: int = 30) -> str:
    """What the warehouse has counted, optionally filtered by description.

    These are hand counts, not a calculated balance, so each line carries the
    date it was last counted.

    Args:
        text: words to look for in the description.
        limit: most items to return.
    """
    db = get_client()
    try:
        rows = db.table("stock_items").select(
            "stock_id,description,quantity,uom,unit_cost,last_counted_at"
        ).execute().data or []
    except Exception as exc:
        return f"Could not read stock: {exc}"

    needle = (text or "").strip().lower()
    if needle:
        rows = [r for r in rows if needle in (r.get("description") or "").lower()]
    if not rows:
        return f"No stock item matches {text!r}." if needle else "No stock counted yet."

    rows.sort(key=lambda r: (r.get("description") or "").lower())
    shown = rows[:min(limit, MAX_ROWS)]
    out = [f"{len(rows)} item(s)" +
           (f" - showing {len(shown)}" if len(shown) < len(rows) else "") + ":"]
    for r in shown:
        out.append("  %-12s %-50s %8s %-5s  counted %s" % (
            r.get("stock_id") or "?",
            _plain(r.get("description"))[:50],
            r.get("quantity") if r.get("quantity") is not None else "?",
            r.get("uom") or "", _d(r.get("last_counted_at"))))
    return "\n".join(out)


# ── running it over HTTP, for a browser client ───────────────────────────────
#
# Over stdio there is nothing to authenticate: the client is a program on this
# machine, started by the person already logged into it. Over HTTP there is,
# because the address is public and SUPABASE_KEY reads everything.
#
# Rather than invent a shared password, this accepts the SAME token the web app
# issues when somebody signs in to SPMprocure360. So access follows the
# accounts that already exist: an account that is disabled loses the server too,
# and nobody gets in who could not already log in. There is no second set of
# credentials to manage or leak.


def _http_app():
    """The MCP app with a bearer-token gate in front of it.

    Deliberately hand-rolled rather than the SDK's auth, because the SDK's
    turns the server into an OAuth resource server and a client then tries to
    register with an authorization server that does not exist. All that is
    wanted here is: read one header, verify it, refuse plainly.

    The 401 carries no WWW-Authenticate pointing anywhere. That absence is the
    point - it leaves a client with nothing to negotiate, so it sends the fixed
    header it was configured with instead of starting a sign-in dance.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    base = os.environ.get("MCP_PUBLIC_URL", "").rstrip("/")
    if not base:
        raise SystemExit(
            "MCP_PUBLIC_URL must be the service's public https address, e.g.\n"
            "  https://bubbly-miracle-production-8a59.up.railway.app")
    if not os.environ.get("JWT_SECRET"):
        raise SystemExit(
            "JWT_SECRET must be set, and must match the value the WEB service "
            "uses - connector tokens are signed with it, and a different value "
            "rejects every token with no clue as to why.")
    # Checked here because get_client() only reads these when first used. A
    # new Railway service inherits no variables from the others, and without
    # these the service booted, answered /health, and then refused every token
    # - the account lookup failed and looked exactly like a wrong secret.
    missing = [v for v in ("SUPABASE_URL", "SUPABASE_KEY") if not os.environ.get(v)]
    if missing:
        raise SystemExit(
            f"{' and '.join(missing)} must be set on this service. A Railway "
            f"service does not inherit variables from the web or worker "
            f"services - copy them across. Without them every token is refused, "
            f"because the account it belongs to cannot be looked up.")

    app = mcp.streamable_http_app(host="0.0.0.0")
    verifier = SPMTokenVerifier()

    class _RequireToken(BaseHTTPMiddleware):
        # Starlette takes middleware through add_middleware; the
        # @app.middleware("http") decorator is FastAPI's, and this app is a
        # plain Starlette one.
        async def dispatch(self, request, call_next):
            # The health path answers without a credential, so a platform can
            # check the service is alive without holding one.
            if request.url.path in ("/", "/health"):
                return JSONResponse({"ok": True, "service": "spm-mcp"})

            header = request.headers.get("Authorization", "")
            token = header[7:].strip() if header.lower().startswith("bearer ") else ""
            if not token or await verifier.verify_token(token) is None:
                return JSONResponse(
                    {"error": "unauthorized",
                     "detail": "Send Authorization: Bearer <token>. Mint one "
                               "with scripts/mcp_server.py --token <email>."},
                    status_code=401)
            return await call_next(request)

    app.add_middleware(_RequireToken)
    return app


def _mint_token(email: str, days: int = 365) -> None:
    """Print a connector token for one SPM account.

        python scripts/mcp_server.py --token someone@example.com
        python scripts/mcp_server.py --token someone@example.com --days 90

    Paste the result into the connector's request headers on claude.ai as
    `Authorization: Bearer <token>`.
    """
    import jwt as pyjwt

    secret = os.environ.get("JWT_SECRET", "")
    if not secret:
        raise SystemExit("JWT_SECRET is not set - it must match the web app's, "
                         "or the token this prints will not verify.")
    try:
        res = get_client().table("users").select(
            "id,email,role,is_active").eq("email", email).execute()
    except Exception as exc:
        raise SystemExit(f"Could not read users: {exc}")
    if not res.data:
        raise SystemExit(f"No account with email {email!r}.")
    user = res.data[0]
    if not user.get("is_active"):
        raise SystemExit(f"{email} is not an active account.")

    token = pyjwt.encode(
        {
            "sub": user["id"],
            "role": user.get("role") or "user",
            "aud": MCP_AUDIENCE,
            "exp": datetime.now(timezone.utc) + timedelta(days=days),
        },
        secret,
        algorithm="HS256",
    )
    print()
    print(f"Connector token for {user['email']} ({user.get('role')}), "
          f"valid {days} days.")
    print()
    print(token)
    print()
    print("On claude.ai: Settings -> Connectors -> Add custom connector,")
    print("  URL     : <your MCP_PUBLIC_URL>/mcp")
    print("  Headers : Authorization: Bearer <the token above>")
    print()
    print("It reads through the tools in this file and cannot sign in to the")
    print("website - the website refuses it. To revoke, disable the account.")


if __name__ == "__main__":
    if "--token" in sys.argv:
        i = sys.argv.index("--token")
        if i + 1 >= len(sys.argv):
            raise SystemExit("usage: --token <email> [--days N]")
        days = 365
        if "--days" in sys.argv:
            days = int(sys.argv[sys.argv.index("--days") + 1])
        _mint_token(sys.argv[i + 1], days)
        sys.exit(0)

    # stdio stays the default, so Claude Code keeps working unchanged. The
    # server itself was already built for the right transport at import time,
    # with the tools registered on it either way - there is no second instance
    # to keep in step.
    if _HTTP:
        import uvicorn
        port = int(os.environ.get("PORT", "8081"))
        print(f"[mcp] http on :{port}, bearer token required", flush=True)
        # Served directly rather than through mcp.run(), so the token check
        # wraps the app and no OAuth is advertised.
        uvicorn.run(_http_app(), host="0.0.0.0", port=port, log_level="info")
    else:
        mcp.run()

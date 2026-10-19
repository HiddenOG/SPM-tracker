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
every customer. Three rules keep that in bounds:

  * Read-only. Nothing here writes, updates or deletes. There is no code path
    to a mutation, not a disabled one.
  * Narrow. Each tool answers one shape of question. There is no
    "run this SQL" tool, which would hand the whole database over in one step.
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

mcp = MCPServer(
    name="spm",
    instructions=(
        "Read-only lookups over SPMprocure360: Chevron and NLNG orders, "
        "quotations, stored email threads and counted stock. Nothing here "
        "writes. Every tool caps its own output, so ask a narrow question "
        "rather than expecting a full table."
    ),
)

# Hard ceiling on every tool. A question whose honest answer is "most of the
# database" should come back trimmed and say so, rather than returning
# thousands of rows into a context window.
MAX_ROWS = 60

# The pipeline, in order, with the column that records each step. Used to work
# out where an order has stopped: the last stamped stage is where it sits.
_CHEVRON_STAGES = [
    ("acknowledged_at",               "acknowledged on GEP"),
    ("sent_to_warehouse_at",          "sent to warehouse for stock check"),
    ("stock_check_completed_at",      "stock check completed"),
    ("spm_po_sent_at",                "SPM PO sent to supplier"),
    ("so_received_at",                "supplier acknowledged (SO)"),
    ("flex_dispatch_ready_at",        "supplier says packed and ready"),
    ("dispatch_instructions_sent_at", "dispatch instructions sent"),
    ("ready_for_dispatch_at",         "collection arranged"),
    ("dispatched_at",                 "dispatched"),
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
    """The furthest stage this order has reached, when, and days since.

    Read from the end backwards so a gap in the middle does not misreport
    progress - the auditor fills those in later, and an order that has been
    dispatched has been dispatched whether or not every earlier box is ticked.
    """
    for col, label in reversed(stages):
        if row.get(col):
            return label, _d(row[col]), _days_since(row[col])
    return "nothing stamped yet", "—", None


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

@mcp.tool()
def search_quotes(text: str = "", client: str = "", since: str = "",
                  limit: int = 20) -> str:
    """Quotations, searchable by what was quoted.

    `text` is matched against line item descriptions and part numbers as well
    as the quote subject, so "spiral wound gasket" finds quotes containing that
    item rather than only ones with it in the title.

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
            if needle in (q.get("subject") or "").lower():
                return True
            return any(needle in (_plain(i.get("description")) + " " +
                                  str(i.get("part_number") or "")).lower()
                       for i in items_by_quote.get(q["id"], []))

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
            out.append(f"        {str(q['subject'])[:70]}")
        # Only the matching items, so a 40-line quote does not fill the reply.
        for it in items_by_quote.get(q["id"], []):
            desc = _plain(it.get("description"))
            if needle and needle in (desc + " " + str(it.get("part_number") or "")).lower():
                out.append("        - %-58s %s x %s" % (
                    desc[:58], it.get("quantity") or "?", it.get("unit_price") or "?"))
    return "\n".join(out)


# ── email ────────────────────────────────────────────────────────────────────

@mcp.tool()
def search_mail(text: str, limit: int = 15) -> str:
    """Search the stored email threads by subject and body.

    Coverage depends on what the archiver has walked. email_thread_sync stores
    each order's whole Gmail thread as stories are told and on its own
    schedule, so this gets more complete over time; before an order has been
    walked, only the mail a listener recognised is here.

    Args:
        text: words to look for.
        limit: most messages to return.
    """
    needle = (text or "").strip()
    if not needle:
        return "What should I search the mail for?"
    db = get_client()
    try:
        rows = db.table("emails").select(
            "subject,from_address,received_at,direction,body_text,"
            "order_id,nlng_order_id").execute().data or []
    except Exception as exc:
        return f"Could not read emails: {exc}"

    low = needle.lower()
    hits = [r for r in rows
            if low in (r.get("subject") or "").lower()
            or low in (r.get("body_text") or "").lower()]
    if not hits:
        return (f"No stored mail mentions {needle!r}. "
                f"({len(rows)} message(s) on file - an order the archiver has "
                f"not walked yet will only have the mail a listener recognised.)")

    # Undated first, matching how a thread is ordered everywhere else.
    hits.sort(key=lambda r: r.get("received_at") or "", reverse=True)
    shown = hits[:min(limit, MAX_ROWS)]
    out = [f"{len(hits)} message(s) mention {needle!r}" +
           (f" - showing {len(shown)}, newest first" if len(shown) < len(hits)
            else ", newest first") + ":"]
    for r in shown:
        out.append("  [%s] %-4s %s" % (
            _d(r.get("received_at")) if r.get("received_at") else "undated",
            "SENT" if r.get("direction") == "out" else "RECV",
            (r.get("subject") or "(no subject)")[:62]))
        out.append("        from %s" % (r.get("from_address") or "?")[:62])
        body = " ".join((r.get("body_text") or "").split())
        at = body.lower().find(low)
        if at >= 0:
            out.append("        …%s…" % body[max(0, at - 70):at + 110])
    return "\n".join(out)


# ── stock ────────────────────────────────────────────────────────────────────

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


if __name__ == "__main__":
    mcp.run()

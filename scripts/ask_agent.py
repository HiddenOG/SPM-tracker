"""
ask_agent.py — SPM Ask: answer a question from the inbox and the tracker.

The model is given tools rather than documents. It searches the mailbox the
way a person searches the web - Gmail's own index does the work - reads the
few messages that matter, checks the tracker where orders are involved, and
answers with numbered references to the emails it used.

Why it is built the way it is
-----------------------------
Model: Claude Haiku 5.5. Measured on real mail, a typical question reads about
30,000 tokens across its rounds; at $0.10 per million input tokens that is
under half a cent, so the $25 monthly budget covers several thousand
questions. The website's older calls used Haiku 4.5, which costs ten times
as much.

Spending is not tracked here. The monthly cap belongs on the API key itself,
in the Anthropic Console's spend limits, where it stops every caller and not
only this page.

Gmail: only the text of an email is ever downloaded, never attachments - see
mail_search.py. An earlier archiver here downloaded whole messages and got the
mailbox throttled within a day.

Events
------
run() yields plain dicts the web endpoint forwards as server-sent events:

    {"t": "step", "id", "label", "state": "run"|"done"|"error", "detail", "secs"}
    {"t": "text", "d": "..."}          a piece of the answer as it is written
    {"t": "reset"}                     discard streamed text (a tool round)
    {"t": "sources", "items": [...]}   the emails the answer cites
    {"t": "done", "secs", "rounds"}
    {"t": "error", "message"}
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

# Sonnet, not Haiku: Haiku answered gasket questions without knowing a
# Flexitallic CG from a CGI or reading "16 SS" as 316. A question costs a few
# US cents on Sonnet against a fraction of one on Haiku; ASK_MODEL overrides.
MODEL = os.environ.get("ASK_MODEL", "claude-sonnet-5-5")
EFFORT = os.environ.get("ASK_EFFORT", "medium")
MAX_ROUNDS = 8
# Thinking counts against this, so it has to leave room for the answer.
MAX_TOKENS = 16000
# If a request is ever declined by a safety check, the API re-runs it on a
# fallback model inside the same call instead of failing the question.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


_WAT = timezone(timedelta(hours=1))

SYSTEM = """You are SPM Ask, the assistant inside SPMprocure360 for Special Piping Materials (Nig.) Ltd in Warri, Nigeria. SPM supplies piping materials - gaskets, flanges, bolts, valves - to oil and gas clients such as Chevron, NLNG, Seplat, Mobil/ExxonMobil and TotalEnergies, mostly sourced from Flexitallic in the UK.

You answer staff questions from two sources:
- The company inbox (specialpiping@gmail.com, about 295,000 emails), through search_email, read_email and read_thread. search_email takes Gmail search syntax: plain words, "exact phrases", from:, to:, subject:, after:2026/09/01, before:, newer_than:30d, has:attachment, OR, and -word to exclude.
- The SPMprocure360 tracker, through the order, quote and stock tools. The tracker only holds Chevron and NLNG orders; every other client's orders are found in email.

The trade's shorthand - read requests with it, and don't ask what these mean:
- Spiral wound gasket styles (Flexitallic names): CG = winding with an outer centring (guide) ring, usually carbon steel; CGI = CG plus an inner ring; R = winding only; RIR = winding with an inner ring only. "SS outer ring" means the outer ring is stainless instead of carbon steel.
- Materials: SS = stainless steel; 316, 316L and 304 are grades. Staff often drop the 3, so "16 SS" or "SS16" means 316 SS. Fillers: graphite (flexible graphite), PTFE, mica or Thermiculite (Flexitallic's mica-based filler). CS = carbon steel.
- Size is the pipe size in inches, written 36", 36 IN, 36IN or NPS 36. Pressure class is written 300LB, 300#, CL300 or class 300 (150, 300, 600, 900, 1500, 2500). Sizes above 24" follow ASME B16.47 Series A or B, so a 36" gasket needs the series too.
- Ring joints (RTJ) are numbered R, RX or BX plus a number (R18, RX24).
- "A quote from Flexitallic" or "Flexitallic's offer" is Flexitallic's price to SPM. It comes from Flexitallic's sales staff by email, usually as a PDF or Excel attachment. "Our quote" or an SPM quote (SPM-00xx-...) is SPM's price to a client, in the quotations tool. A Flexitallic "sales acknowledgement" or SO comes after SPM orders.
- If one term is still unclear, say how you read it and carry on rather than stopping to ask.

How to work:
- For items, search with a few key words and numbers, not the whole description: search_quotes("spiral wound 36 300"). Spacing and punctuation don't matter there. In email, try the ways the size is written (36" OR 36IN OR "36 in") and from:flexitallic.
- Search first, then read the one to four emails that matter before answering.
- Prices, offers and POs are usually in attachments, not the email text ("please find attached"). Search and read results list each email's attachments; open the relevant one with read_attachment before saying a price isn't there. Search results show only the start of each email.
- For "latest", "status" or "update" questions, search recent mail first (newer_than:30d or newer_than:60d) and widen only if nothing useful comes back.
- Do not write anything before or between tool calls. Write only the final answer.
- If the emails don't settle the question, say what you found and what is missing. Never guess a date, an amount or a PO number.

How to answer:
- The first sentence is the direct answer.
- Then short paragraphs, or lines starting with "- " for lists. Plain language. No headings or tables. A list line may start with a short **bold** label, like "**QU-677580 (Seplat):**"; no other bold.
- Cite emails by their reference number in square brackets, like [2], straight after the fact it supports.
- Write PO numbers, SO numbers, amounts and dates exactly as the source has them."""


# ── tools ────────────────────────────────────────────────────────────────────

EMAIL_TOOLS = [
    {
        "name": "search_email",
        "description": "Search the whole company inbox with Gmail search syntax. Returns the newest matches first, each with a reference number, date, sender, subject and the first lines of the email. Use read_email to see a whole message.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": 'Gmail search, e.g. seplat newer_than:30d, from:flexitallic "ready to ship", subject:"SO720482"'},
                "max_results": {"type": "integer", "description": "How many matches to show, 1-15. Default 10."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_email",
        "description": "Read one email in full, by the id shown in search results. Quoted earlier messages in the reply chain are trimmed; find those with search_email or read_thread.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "The email id from search results."}},
            "required": ["id"],
        },
    },
    {
        "name": "read_attachment",
        "description": "Read the text inside one attachment of an email - PDF, Excel (.xlsx), Word (.docx) or text. Use it when the attachment holds the answer, such as a supplier's price offer or a PO. Takes the email id and the file name exactly as listed under 'attachments' in search or read results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The email id."},
                "filename": {"type": "string", "description": "The attachment's file name."},
            },
            "required": ["id", "filename"],
        },
    },
    {
        "name": "read_thread",
        "description": "List every email in a conversation, oldest first, with the start of each. Use when the order of events matters. Takes the thread id from a search result.",
        "input_schema": {
            "type": "object",
            "properties": {"thread_id": {"type": "string"}},
            "required": ["thread_id"],
        },
    },
]

TRACKER_TOOLS = [
    {
        "name": "find_orders",
        "description": "Find Chevron or NLNG orders in the tracker by PO number, SO number, SPM PO number or buyer name. Partial numbers work.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "order_detail",
        "description": "Everything the tracker holds on one Chevron or NLNG order: every pipeline date, value, SO and SPM PO numbers.",
        "input_schema": {"type": "object", "properties": {"po_number": {"type": "string"}}, "required": ["po_number"]},
    },
    {
        "name": "orders_stuck",
        "description": "Chevron/NLNG orders whose latest activity is older than a number of days, longest first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Default 14."},
                "client": {"type": "string", "enum": ["", "chevron", "nlng"]},
            },
        },
    },
    {
        "name": "search_quotes",
        "description": "SPM's quotations, searched by what was quoted (line item descriptions and part numbers) and the subject.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "client": {"type": "string", "description": "chevron, nlng, exxon, total, seplat, gca or others"},
                "since": {"type": "string", "description": "ISO date, e.g. 2026-01-01"},
            },
        },
    },
    {
        "name": "stock_on_hand",
        "description": "What the warehouse has counted, filtered by description words.",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
    },
]

SCOPES = {
    "both": EMAIL_TOOLS + TRACKER_TOOLS,
    "mail": EMAIL_TOOLS,
    "tracker": TRACKER_TOOLS,
}


class _Refs:
    """Numbers each email the first time a tool shows it, for [n] citations."""

    def __init__(self):
        self.by_id: dict = {}
        self.items: list = []

    def ref(self, h: dict) -> int:
        mid = h.get("id")
        if mid in self.by_id:
            n = self.by_id[mid]
            # Keep the fullest snippet seen for it.
            if h.get("snippet") and not self.items[n - 1].get("snippet"):
                self.items[n - 1]["snippet"] = h["snippet"]
            return n
        n = len(self.items) + 1
        self.by_id[mid] = n
        self.items.append({
            "ref": n, "id": mid, "date": h.get("date", ""), "from": h.get("from", ""),
            "subject": h.get("subject", ""), "snippet": (h.get("snippet") or "")[:220],
        })
        return n


def _short_from(s: str) -> str:
    m = re.match(r'\s*"?([^"<]+?)"?\s*<', s or "")
    return (m.group(1) if m else (s or "")).strip()


def _q(s: str, n=60) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def execute(name: str, args: dict, refs: _Refs, mail=None, tracker=None):
    """Run one tool. Returns (text for the model, step label, step detail)."""
    args = args or {}
    if name in ("search_email", "read_email", "read_thread", "read_attachment"):
        if mail is None:
            import mail_search as mail
    else:
        if tracker is None:
            import mcp_server as tracker

    if name == "search_email":
        query = str(args.get("query") or "")
        label = f"Searching the inbox for “{_q(query)}”"
        res = mail.search(query, int(args.get("max_results") or 10))
        if not res["results"]:
            return (f'No emails match {query!r}. Try fewer or different words, '
                    f'or a wider date range.', label, "No matches")
        lines = [f'{res["total"]} emails match {query!r}; newest {len(res["results"])} shown:']
        for h in res["results"]:
            n = refs.ref(h)
            att = f' · attachments: {", ".join(h["attachments"])[:160]}' if h.get("attachments") else ""
            lines.append(f'[{n}] {h["date"]} · from {h["from"]} · to {h["to"]}\n'
                         f'    subject: {h["subject"]}{att}\n'
                         f'    id {h["id"]} · thread {h["thread"]}\n'
                         f'    {h["snippet"]}')
        detail = f'{res["total"]:,} match' + ("" if res["total"] == 1 else "es")
        return "\n".join(lines), label, detail

    if name == "read_email":
        m = mail.read(str(args.get("id") or ""))
        n = refs.ref({**m, "snippet": " ".join(m["body"].split())[:220]})
        label = f"Reading “{_q(m['subject'], 50)}”"
        notes = []
        if m.get("quoted_history_trimmed"):
            notes.append("quoted earlier messages trimmed")
        if m.get("cut_at_chars"):
            notes.append(f"cut at {m['cut_at_chars']} characters")
        att = f'attachments: {", ".join(m["attachments"])}\n' if m.get("attachments") else ""
        text = (f'[{n}] {m["date"]}\nfrom: {m["from"]}\nto: {m["to"]}\n'
                + (f'cc: {m["cc"]}\n' if m.get("cc") else "")
                + f'subject: {m["subject"]}\n{att}thread {m["thread"]}'
                + (f' ({"; ".join(notes)})' if notes else "") + f'\n\n{m["body"]}')
        return text, label, f'{_short_from(m["from"])} · {m["date"][:11]}'

    if name == "read_attachment":
        eid = str(args.get("id") or "")
        a = mail.attachment(eid, str(args.get("filename") or ""))
        ref = refs.by_id.get(eid)                 # the email's [n], if it was shown
        of = f" of email [{ref}]" if ref else ""
        pages = f" · {a['pages']} pages" if a.get("pages") else ""
        note = " (only the start is shown - the file is longer)" if a.get("cut") else ""
        return (f'Attachment "{a["name"]}"{of}{note}:\n\n{a["text"]}',
                f"Opening “{_q(a['name'], 50)}”", f'{a["kind"].upper()}{pages}')

    if name == "read_thread":
        t = mail.thread(str(args.get("thread_id") or ""))
        label = "Reading the whole conversation"
        lines = [f'Thread of {t["size"]} emails; ' + (f'last {t["shown"]} shown, ' if t["shown"] < t["size"] else "")
                 + "oldest first:"]
        for h in t["messages"]:
            n = refs.ref(h)
            lines.append(f'[{n}] {h["date"]} · from {h["from"]} · id {h["id"]}\n    {h["snippet"]}')
        return "\n".join(lines), label, f'{t["size"]} emails'

    if name == "find_orders":
        q = str(args.get("query") or "")
        return tracker.find_orders(q, limit=20), f"Looking up “{_q(q, 40)}” in the tracker", "Tracker"
    if name == "order_detail":
        po = str(args.get("po_number") or "")
        return tracker.order_detail(po), f"Opening PO {_q(po, 20)} in the tracker", "Tracker"
    if name == "orders_stuck":
        days = int(args.get("days") or 14)
        return (tracker.orders_stuck(days=days, client=str(args.get("client") or ""), limit=40),
                f"Checking orders with no movement in {days}+ days", "Tracker")
    if name == "search_quotes":
        return (tracker.search_quotes(text=str(args.get("text") or ""),
                                      client=str(args.get("client") or ""),
                                      since=str(args.get("since") or ""), limit=15),
                "Searching quotations", "Tracker")
    if name == "stock_on_hand":
        return (tracker.stock_on_hand(text=str(args.get("text") or ""), limit=30),
                "Checking counted stock", "Tracker")
    return f"Unknown tool {name}.", f"Unknown tool {name}", "Not run"


# ── the loop ─────────────────────────────────────────────────────────────────

def _clean_history(history) -> list:
    """Earlier turns, as plain text only, last eight, each capped.

    Only the words of earlier questions and answers are sent back - not the
    emails read to produce them. Re-sending those would grow every question by
    the cost of all the ones before it.
    """
    out = []
    for m in (history or [])[-8:]:
        role = m.get("role")
        text = str(m.get("content") or "").strip()
        if role in ("user", "assistant") and text:
            out.append({"role": role, "content": text[:4000]})
    # The API needs alternating turns starting with the user.
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def run(question: str, history=None, scope: str = "both", user=None,
        client=None, mail=None, tracker=None):
    """Answer one question. Yields event dicts; see the module docstring."""
    started = time.time()
    tools = SCOPES.get(scope, SCOPES["both"])
    refs = _Refs()
    rounds = 0
    answer = ""
    ok = False

    if client is None:
        import anthropic
        client = anthropic.Anthropic()

    now = datetime.now(_WAT)
    messages = _clean_history(history) + [{
        "role": "user",
        # The date goes in the message, not the system prompt, so the system
        # prompt stays byte-identical and cacheable from one question to the next.
        "content": f"(Today is {now:%A %d %b %Y}, Nigeria time.)\n\n{question.strip()}",
    }]

    step_no = 0
    try:
        for rnd in range(1, MAX_ROUNDS + 1):
            streamed = ""
            # Automatic caching marks the last block of each request, so the
            # next round reads the growing conversation from cache rather than
            # paying for it again.
            with client.beta.messages.stream(
                model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM,
                tools=tools, messages=messages,
                cache_control={"type": "ephemeral"},
                output_config={"effort": EFFORT},
                betas=[FALLBACK_BETA], fallbacks="default",
            ) as stream:
                for event in stream:
                    if (getattr(event, "type", "") == "content_block_delta"
                            and getattr(event.delta, "type", "") == "text_delta"):
                        streamed += event.delta.text
                        yield {"t": "text", "d": event.delta.text}
                final = stream.get_final_message()

            rounds += 1
            # The whole response goes back, thinking blocks and all - the API
            # expects the assistant turn exactly as it produced it.
            messages.append({"role": "assistant", "content": final.content})

            if final.stop_reason == "refusal":
                yield {"t": "reset"}
                yield {"t": "error", "message": "Claude declined to answer this one."}
                break

            if final.stop_reason != "tool_use":
                answer = streamed
                if final.stop_reason == "max_tokens":
                    yield {"t": "text", "d": "\n\n(The answer was cut short.)"}
                ok = True
                break

            if streamed:
                yield {"t": "reset"}

            results = []
            for block in final.content:
                if getattr(block, "type", "") != "tool_use":
                    continue
                step_no += 1
                sid = f"s{step_no}"
                t0 = time.time()
                yield {"t": "step", "id": sid, "label": _pending_label(block.name, block.input),
                       "state": "run"}
                try:
                    text, label, detail = execute(block.name, block.input, refs, mail, tracker)
                    yield {"t": "step", "id": sid, "label": label, "state": "done",
                           "detail": detail, "secs": round(time.time() - t0, 1)}
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": text})
                except Exception as exc:
                    msg = str(exc) or type(exc).__name__
                    yield {"t": "step", "id": sid, "label": _pending_label(block.name, block.input),
                           "state": "error", "detail": msg[:160],
                           "secs": round(time.time() - t0, 1)}
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": f"That failed: {msg}", "is_error": True})
            # Every tool result for the round in one message.
            messages.append({"role": "user", "content": results})
        else:
            yield {"t": "text", "d": "I couldn't finish within the search limit. Try a narrower question."}

        cited = sorted({int(n) for n in re.findall(r"\[(\d{1,2})\]", answer)})
        items = [r for r in refs.items if r["ref"] in cited] if cited else []
        if items:
            yield {"t": "sources", "items": items}

        if ok:
            yield {"t": "done", "secs": round(time.time() - started, 1), "rounds": rounds}
    except Exception as exc:
        yield {"t": "error", "message": f"Something went wrong: {str(exc)[:200]}"}


def _pending_label(name: str, args) -> str:
    args = args or {}
    if name == "search_email":
        return f"Searching the inbox for “{_q(str(args.get('query') or ''))}”"
    if name == "read_email":
        return "Reading an email"
    if name == "read_thread":
        return "Reading the whole conversation"
    if name == "read_attachment":
        return f"Opening “{_q(str(args.get('filename') or 'an attachment'), 50)}”"
    if name in ("find_orders", "order_detail"):
        return "Checking the tracker"
    if name == "orders_stuck":
        return "Checking orders that haven't moved"
    if name == "search_quotes":
        return "Searching quotations"
    if name == "stock_on_hand":
        return "Checking counted stock"
    return name


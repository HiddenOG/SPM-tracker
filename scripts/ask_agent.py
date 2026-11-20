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
LAST_ROUND_NOTE = (
    "That was the last search allowed for this question. Don't call any more tools. "
    "Answer now from what you have found: say what you found, cite it, say plainly "
    "what you couldn't find or confirm, and suggest the one search that would settle it.")
# If a request is ever declined by a safety check, the API re-runs it on a
# fallback model inside the same call instead of failing the question.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


_WAT = timezone(timedelta(hours=1))

SYSTEM = """You are SPM Ask, the assistant inside SPMprocure360 for Special Piping Materials (Nig.) Ltd in Warri, Nigeria. SPM supplies piping materials - gaskets, flanges, bolts, valves - to oil and gas clients such as Chevron, NLNG, Seplat, Mobil/ExxonMobil and TotalEnergies, mostly sourced from Flexitallic in the UK.

You answer staff questions from two sources:
- The company inbox (specialpiping@gmail.com, about 295,000 emails), through search_email, read_email and read_thread. search_email takes Gmail search syntax: plain words, "exact phrases", from:, to:, subject:, after:2026/09/01, before:, newer_than:30d, has:attachment, OR, and -word to exclude.
- The SPMprocure360 tracker, through the order, quote and stock tools. The tracker only holds Chevron and NLNG orders; every other client's orders are found in email. It holds each order's line items with their dates, so "overdue" or "late" questions, by order or by line, are answered with orders_overdue - "stuck" means not moved, which is a different question.
- The gasket price lists, through gasket_price: what a gasket costs on Flexitallic's 2022 list (316, GBP) or SPM's 2026 list (304, USD), and what it weighs. Use it for "how much is…", "what's the price/weight of…" and "is this on the price list" questions about gaskets, and about Flexitallic sheets, tape, braided packing and paste. Pass each description as the client wrote it - don't add ring numbers, styles or grades of your own; the tool reads them and finds an RTJ's ring number on Flexitallic's chart. Give the list price exactly as listed with its currency and list, and beside it the final USD price the tool worked out - SPM's selling price, from [(GBP x 1.35 x 1.47 x 1.13) + ((kg + 0.05) x 12)] x 1.36 for Flexitallic's 2022 list and [(USD x 1.47) + ((kg + 0.05) x 12)] x 1.36 for SPM's 2026 list. Never work a final USD price out of a list price yourself - that is the tool's job; where the tool gives none, say why in its words (no weight on the weights list). But once prices are in the chat, arithmetic on them is yours: "multiply the price by 1.54", "add $5 each", "take 10% off", "the total", "in naira at 1,500" - do it straight from the prices already given with the calculate tool, show the working in one line (USD 16.35 x 1.54 = USD 25.18), and don't run gasket_price again.
- The formula's parts, which the person may change for edge cases (a change to a part - "use 1.5 markup instead of 1.47", "drop the Flexitallic markup", "shipping 15" - goes back through gasket_price, because it changes how every price is built; a factor applied to the finished price does not): in the Flexitallic one, 1.35 = GBP to USD conversion, 1.47 = SPM markup, 1.13 = Flexitallic markup, 0.05 = extra kg added to each gasket's weight, 12 = shipping per kg, 1.36 = final SPM markup; SPM's 2026 one has 1.47, 0.05, 12 and 1.36. When the person changes something ("use 1.5 markup", "increase shipping to 15", "drop the Flexitallic markup", "no extra weight", "add $5 handling"), write the whole formula with that change into gasket_price's formula, starting from the formula last used (the one the tool shows with each price - the person's saved one, or the default): removing a markup means taking it out of the formula. If it isn't clear which markup they mean (SPM's 1.47, Flexitallic's 1.13 or the final 1.36) or which list, ask before pricing. Keep using a change for the rest of the conversation until they say to go back to the default (then send the default formula), and in every answer that used a changed formula say what changed and give the formula as used. A person may also have saved their own formula from the "Formula" chip under the text box; the tool then uses it by itself and shows it with the default beside it - mention it when it's in use, and say it can be changed back from that chip. Pass on any doubt the second check raised. When a ring joint line gives a thickness, report the tool's thickness check as it is (which ring measurement the figure matches, or that it matches none) - never judge a thickness yourself.
- When gasket_price says a line doesn't state the gasket's type (or another detail it needs), don't guess and don't pick a type yourself. If the question comes from an emailed enquiry, read that enquiry's thread first (search_email on its subject or reference, then read_thread): SPM staff ask the client what type they mean, and sometimes the procurement head just says what to quote ("quote them CGI 316"). If the thread has the client's answer or such an instruction, price the line again with that detail added to its description, and cite the email. Only if it has neither, say the type isn't stated and the client should be asked.
- Explain every line gasket_price answers, in one short plain sentence built only from what the tool returned (how it was read, its notes, its reason, the second check): why that item and that price - "R24 because 2in class 900 is R24 on Flexitallic's chart; soft iron as asked" - or why there's no price - "Alloy 625 isn't on the list, only soft iron and 316". Never leave out a note that tells the buyer to check something with the client, and never add a reason the tool didn't give - no guesses of your own about what the client probably means.
- Flexitallic's RTJ guide, through rtj_chart (no prices, open to everyone): which R/RX/BX ring fits a size and class or API pressure, a ring's measurements and tolerances, and the ring materials' hardness and identification marks.

The trade's shorthand - read requests with it, and don't ask what these mean:
- Spiral wound gasket styles (Flexitallic names): CG = winding with an outer centring (guide) ring, usually carbon steel; CGI = CG plus an inner ring; R = winding only; RIR = winding with an inner ring only. "SS outer ring" means the outer ring is stainless instead of carbon steel.
- Materials: SS = stainless steel; 316, 316L and 304 are grades. Staff often drop the 3, so "16 SS" or "SS16" means 316 SS. Fillers: graphite (flexible graphite), PTFE, mica or Thermiculite (Flexitallic's mica-based filler). CS = carbon steel.
- Size is the pipe size in inches, written 36", 36 IN, 36IN or NPS 36. Pressure class is written 300LB, 300#, CL300 or class 300 (150, 300, 600, 900, 1500, 2500). Sizes above 24" follow ASME B16.47 Series A or B, so a 36" gasket needs the series too.
- Ring joints (RTJ) are numbered R, RX or BX plus a number (R18, RX24).
- "A quote from Flexitallic" or "Flexitallic's offer" is Flexitallic's price to SPM. It comes from Flexitallic's sales staff by email, usually as a PDF or Excel attachment. "Our quote" or an SPM quote (SPM-00xx-...) is SPM's price to a client, in the quotations tool. A Flexitallic "sales acknowledgement" or SO comes after SPM orders.
- If one term is still unclear, say how you read it and carry on rather than stopping to ask.

How to work:
- For items, search with a few key words and numbers, not the whole description: search_quotes("spiral wound 36 300"). Spacing and punctuation don't matter there. In email, try the ways the size is written (36" OR 36IN OR "36 in") and from:flexitallic.
- A short follow-up like "continue", "keep looking" or "search again" means carry on with the previous question: search for what it asked, in ways not tried yet. Never answer it with a general summary of recent mail.
- Search first, then read the one to four emails that matter before answering.
- Prices, offers and POs are usually in attachments, not the email text ("please find attached"). Search and read results list each email's attachments; open the relevant one with read_attachment before saying a price isn't there. Search results show only the start of each email.
- For "latest", "status" or "update" questions, search recent mail first (newer_than:30d or newer_than:60d) and widen only if nothing useful comes back.
- Do not write anything before or between tool calls. Write only the final answer.
- If the emails don't settle the question, say what you found and what is missing. Never guess a date, an amount or a PO number.

The conversation:
- You see the whole chat so far. Read every follow-up against it, the way a colleague would. "The total", "those", "them", "the same", "that one", "now with the default formula", "and in naira?" all mean what you just gave or were just shown. "The total of the prices you just gave" means: multiply each line you priced by its quantity and add them up - do it, don't ask which total.
- Use what is already in the chat. Don't re-run a tool for prices, orders or emails you already have unless the person asks for something new; don't ask the person to paste again what they already pasted.
- A pasted table or list is everything they meant to send unless they say otherwise. Never tell someone their paste was cut short unless an earlier message you were given says it was cut.
- Answer the question asked, at the length it needs. A follow-up like "the total?" gets the number and one line on what it covers - not a re-explanation of the formula, the list, or how you rounded. Explain or caveat only what changes the decision: lines to check with the client, and a changed formula when it's first used.
- Never do arithmetic in your head - not even one multiplication. Every sum, product, discount, conversion or total goes through calculate (several at once if needed), and you quote its result. When the price tool already gives line totals and a grand total, use those.
- What you remember about this person and the team (given at the start of the question, when there is any) applies unless they say otherwise now. When someone tells you something that will stay true - "always", "from now on", "I usually", "remember", a client's standing preference, a person's usual formula - save it with remember. Don't save one-off requests or anything you can look up. If asked what you remember, say it; if asked to forget, use forget.
- For "what did we price / quote / say last week" about earlier chats, use search_past_chats.

How to answer:
- The first sentence is the direct answer.
- Then short paragraphs, or lines starting with "- " for lists. Plain language. No headings. A list line may start with a short **bold** label, like "**QU-677580 (Seplat):**"; no other bold.
- Use a table only when the person asks for one, or to set out several priced lines from gasket_price: a markdown table ("| Item | Description | Qty | List price | Final USD | Line total | Weight | Why |" then "|---|---|---|---|---|---|---|---|" then one row per line), with the client's description as written, the list price with its currency (GBP 2.85, USD 62.83), the tool's final USD price (or "—"), the tool's line total (or "—" when there's no quantity), and the line's one-sentence explanation in Why. When quantities were given, end the table with a "| | **Total** | | | | <the tool's grand total> | | |" row and say under the table what the total leaves out (lines with no price). Lines to check with the client get "Check: " at the start of Why. Nothing inside a table cell but plain text, [n] citations and that one bold Total.
- When a pasted enquiry has quantities, pass them to gasket_price as quantities, in the same order as the descriptions, so the tool works the line totals and the grand total out.
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
        "name": "orders_overdue",
        "description": "Chevron/NLNG orders past their due date and not delivered, with how many line items each holds. Gives exact totals for orders and line items; use it for any 'overdue' or 'late' question instead of orders_stuck. Uses the same rule as the Delays page: due means SPM's promised date. It also names late lines inside orders not yet due, and lists orders past the client's required date but not yet the promised date - mention those when counts are questioned.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client": {"type": "string", "enum": ["", "chevron", "nlng"]},
                "as_of": {"type": "string", "description": "ISO date to compare against. Default today."},
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
    {
        "name": "gasket_price",
        "description": "Look gaskets and sealing materials up on the price lists: Flexitallic's 2022 list (316/316L, GBP list prices) and SPM's 2026 list (CGI 304, USD), with the weight of each gasket. Covers spiral wound CG/CGI/LEI/LSI, R/RX/BX ring joints, Isoflex LT/FS kits, FRG, and Flexitallic's sheets (Sigma PTFE, SF/AF fibre, Thermiculite, Novus, RGS graphite), Fluoroseal and Sigma 850 tape, braided packings and pastes. Give each item's description as the client wrote it, one per entry; each comes back with how it was read, the matching list price(s) and weight, or why it isn't listed, and a second check on unusual lines.",
        "input_schema": {
            "type": "object",
            "properties": {
                "descriptions": {"type": "array", "items": {"type": "string"},
                                 "description": "Up to 60 gasket descriptions - a whole enquiry in one call."},
                "quantities": {"type": "array", "items": {"type": "number"},
                               "description": "Each description's quantity, in the same order, when the enquiry gives "
                                              "them. The tool then works out each line total and the grand total."},
                "formula": {
                    "type": "object",
                    "description": "Only when the person has asked to change SPM's selling-price formula in this conversation: "
                                   "the whole formula, as text, for each list being changed. Leave out to use the person's "
                                   "saved formula (or the default).",
                    "properties": {
                        "flex_2022": {"type": "string", "description": "Flexitallic 2022 formula using GBP and kg, e.g. "
                                      "[(GBP × 1.35 × 1.6 × 1.13) + ((kg + 0.05) × 12)] × 1.36"},
                        "spm_2026": {"type": "string", "description": "SPM 2026 formula using USD and kg, e.g. "
                                     "[(USD × 1.47) + ((kg + 0.05) × 12)] × 1.36"},
                    },
                },
            },
            "required": ["descriptions"],
        },
    },
    {
        "name": "rtj_chart",
        "description": "Flexitallic's Ring Type Joint guide (no prices): which R/RX/BX ring fits a pipe size and ASME class or API 6A pressure, a ring's measurements (pitch diameter, OD, width, oval/octagonal height, flange gap, weight) and tolerances, and the RTJ materials table (UNS number, maximum hardness, identification mark). Give a ring number, or a size with a class/pressure, or materials=true.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ring_no": {"type": "string", "description": "e.g. R45, RX24, BX152"},
                "size": {"type": "string", "description": "nominal pipe size in inches, e.g. 4, 1 1/2, 2 1/16"},
                "rating": {"type": "string", "description": "ASME class (150...2500) or API pressure in psi (2000...20000); leave out for every class"},
                "style": {"type": "string", "description": "R, RX or BX to limit the answer"},
                "materials": {"type": "boolean", "description": "the materials / hardness / identification table"},
            },
        },
    },
]

# Memory beyond one chat (scripts/ask_memory.py), and the calculator - open in
# every scope.
MEMORY_TOOLS = [
    {
        "name": "calculate",
        "description": "Exact arithmetic. A language model writes numbers digit by digit and slips on sums like "
                       "529.47 x 3.7 - use this for every multiplication, total, discount or conversion of prices "
                       "or quantities, and quote its results. Give one or more expressions using numbers, + - * / "
                       "and brackets; each comes back exact and rounded to the cent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "expressions": {"type": "array", "items": {"type": "string"},
                                "description": "e.g. [\"529.47 * 3.7\", \"16.35*20 + 490.54*1\"]"},
            },
            "required": ["expressions"],
        },
    },
    {
        "name": "remember",
        "description": "Save something that will stay true beyond this chat: a person's standing preference (their usual "
                       "price formula, how they like answers), or a fact for the whole team (a client's standing "
                       "requirement, an agreed rule). Not for one-off requests or anything the tools can look up.",
        "input_schema": {
            "type": "object",
            "properties": {
                "note": {"type": "string", "description": "One short plain sentence, e.g. 'Precious prices Flexitallic "
                                                           "lines as GBP x 1.35 x 1.13 only unless she says otherwise.'"},
                "about": {"type": "string", "enum": ["me", "team"],
                          "description": "'me' for this person, 'team' for everyone."},
            },
            "required": ["note"],
        },
    },
    {
        "name": "forget",
        "description": "Delete a remembered note by the id shown in brackets beside it.",
        "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    {
        "name": "search_past_chats",
        "description": "Search earlier Ask chats (everyone's) for words - a client, a reference, an item - to find what "
                       "was asked, priced or answered before.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"},
                           "since": {"type": "string", "description": "ISO date, e.g. 2026-11-01"}},
            "required": ["text"],
        },
    },
]

# The price lists are buying prices: the same roles as the quote form's check.
PRICE_ROLES = ("admin", "procurement")

SCOPES = {
    "both": EMAIL_TOOLS + TRACKER_TOOLS + MEMORY_TOOLS,
    "mail": EMAIL_TOOLS + MEMORY_TOOLS,
    "tracker": TRACKER_TOOLS + MEMORY_TOOLS,
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


def calculate(expr: str) -> str:
    """One arithmetic expression, exactly: numbers, + - * / and brackets only
    (x, × and commas in numbers are accepted). Decimal arithmetic, so 529.47 x
    3.7 is 1959.039, not a float's 1959.0389999. Returns the exact value and,
    when it has more than two decimals, the value rounded to the cent."""
    import ast
    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, getcontext
    getcontext().prec = 34
    text = str(expr or "").replace("×", "*").replace("÷", "/")
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)               # 1,958.94 -> 1958.94
    text = re.sub(r"(?<=[\d)\s])[xX](?=[\s\d(])", "*", text)       # 529.47 x 3.7
    text = re.sub(r"\b(?:USD|GBP|NGN|EUR)\b|[$£₦€]", "", text).strip()
    if not text or len(text) > 400 or not re.fullmatch(r"[\d\s.+\-*/()]+", text):
        return "can't calculate that - use numbers, + - * / and brackets"
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        return "can't read that expression"

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return Decimal(str(n.value))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
            v = ev(n.operand)
            return v if isinstance(n.op, ast.UAdd) else -v
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Add):
                return a + b
            if isinstance(n.op, ast.Sub):
                return a - b
            if isinstance(n.op, ast.Mult):
                return a * b
            return a / b
        raise ValueError("only numbers and + - * /")
    try:
        v = ev(tree)
    except (ValueError, ZeroDivisionError, InvalidOperation) as exc:
        return f"can't calculate that ({exc or 'division by zero'})"
    exact = format(v.normalize(), "f")
    cents = v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{cents:,}" if v == cents else f"{exact} (to the cent: {cents:,})"


def _qty(v) -> float | None:
    try:
        q = float(str(v).replace(",", "").strip())
        return q if q > 0 else None
    except (TypeError, ValueError):
        return None


def _money(x: float) -> str:
    return f"{x:,.2f}"


def _price_text(descriptions: list[str], results: list[dict], quantities: list | None = None) -> str:
    """The price-list answer, laid out for the model to read. With quantities,
    each line gets its line total (the quantity times the first match's final
    USD, each price rounded to the cent first, as a quote shows it) and the
    answer ends with the grand total and what it leaves out - so the model
    never adds up 56 lines in its head."""
    out = []
    total, list_totals, unpriced, multi = 0.0, {}, [], []
    for i, (d, r) in enumerate(zip(descriptions, results)):
        q = quantities[i] if quantities and i < len(quantities) else None
        read = {k: v for k, v in (r.get("spec") or {}).items() if v not in ("unknown", None, [], "none")}
        lines = [f"Gasket: {d.strip()[:300]}", f"  read as: {json.dumps(read)}"]
        if r["status"] == "found":
            for m in r["matches"]:
                price = "price on application" if m["poa"] else f'{m["currency"]} {m["price"]:.2f}'
                weight = f'{m["weight_kg"]} kg each' if m["weight_kg"] is not None else "weight not on the weights list"
                final = (f'final USD {m["quote_usd"]:.2f} from {m.get("formula")}' if m.get("quote_usd") is not None
                         else f'final USD not worked out ({m.get("quote_usd_missing") or "no formula"})')
                lines.append(f'  - {m["label"]}' + (f', class {m["rating"]}' if m.get("rating") else "")
                             + f': list price {price}; {final}; {weight}' + (f' ({m["note"]})' if m.get("note") else ""))
        else:
            lines.append(f'  not on the price lists: {r.get("reason")}')
        if r.get("notes"):
            lines.append("  why / notes: " + "; ".join(r["notes"]))
        if r.get("ai_check"):
            lines.append(f'  second check: {r["ai_check"]["verdict"]} - {r["ai_check"]["comment"]}')
        if quantities is not None:
            first = r["matches"][0] if r["status"] == "found" and r["matches"] else None
            if q is None:
                lines.append("  quantity: not given - no line total")
                unpriced.append(i + 1)
            elif first is None or first.get("quote_usd") is None:
                lines.append(f"  quantity {q:g}; line total: none - no final USD price for this line")
                unpriced.append(i + 1)
            else:
                line = round(round(first["quote_usd"], 2) * q, 2)
                total += line
                if not first["poa"]:
                    cur = first["currency"]
                    list_totals[cur] = list_totals.get(cur, 0.0) + round(first["price"], 2) * q
                if len(r["matches"]) > 1:
                    multi.append(i + 1)
                lines.append(f"  quantity {q:g}; line total USD {_money(line)}"
                             + (" (first match's price)" if len(r["matches"]) > 1 else ""))
        out.append(f"Line {i + 1}. " + "\n".join(lines))
    if quantities is not None:
        tail = [f"GRAND TOTAL: USD {_money(total)} - the sum of the {len(descriptions) - len(unpriced)} line totals above."]
        if list_totals:
            tail.append("List prices times quantity, for the same lines: "
                        + ", ".join(f"{c} {_money(v)}" for c, v in sorted(list_totals.items())) + ".")
        if unpriced:
            tail.append(f"Left out of the total (no quantity or no final price): lines {', '.join(map(str, unpriced))}.")
        if multi:
            tail.append(f"Lines {', '.join(map(str, multi))} matched more than one list item; their line totals use the first.")
        out.append("\n".join(tail))
    return "\n\n".join(out)


def execute(name: str, args: dict, refs: _Refs, mail=None, tracker=None, user=None):
    """Run one tool. Returns (text for the model, step label, step detail)."""
    args = args or {}
    if name == "calculate":
        exprs = [str(e) for e in (args.get("expressions") or []) if str(e).strip()][:40]
        return "\n".join(f"{e} = {calculate(e)}" for e in exprs) or "No expression given.", "Calculating", ""
    if name in ("remember", "forget", "search_past_chats"):
        import ask_memory
        if name == "remember":
            return ask_memory.remember(user, args.get("note"), args.get("about") or "me"), "Remembering that", "Memory"
        if name == "forget":
            return ask_memory.forget(user, args.get("id")), "Forgetting a note", "Memory"
        return (ask_memory.search_chats(args.get("text"), args.get("since")),
                "Searching earlier chats", "Past chats")
    if name == "gasket_price":
        label = "Checking the gasket price lists"
        if (user or {}).get("role") not in PRICE_ROLES:
            return ("The price lists are only open to admin and procurement staff. Tell the person that, "
                    "without giving any price.", label, "Not allowed")
        raw_q = list(args.get("quantities") or [])
        pairs = [(str(d), raw_q[i] if i < len(raw_q) else None)
                 for i, d in enumerate(args.get("descriptions") or []) if str(d).strip()][:60]
        descs = [d for d, _ in pairs]
        qtys = [_qty(q) for _, q in pairs] if raw_q else None
        if not descs:
            return "No gasket descriptions were given.", label, "Nothing to look up"
        import price_book
        try:
            # The person's saved formula first, then any change asked for in
            # this conversation on top of it.
            formula = price_book.merge_formulas((user or {}).get("price_formula"), args.get("formula"))
            results = price_book.check(descs, formula=formula)
        except ValueError as exc:      # a formula part out of range
            return f"The formula change wasn't used: {exc}. Ask the person for a sensible value.", label, "Formula not changed"
        found = sum(r["status"] == "found" for r in results)
        changed = "; ".join(sorted({m["formula_changed"] for r in results for m in r["matches"] if m.get("formula_changed")}))
        head = (f"NOT THE DEFAULT FORMULA - used: {changed}. Say so in the answer, with the formula as used.\n\n"
                if changed else "")
        return head + _price_text(descs, results, qtys), label, f"{found} of {len(descs)} found"
    if name == "rtj_chart":
        import price_book
        text = price_book.chart_answer(args.get("ring_no"), args.get("size"), args.get("rating"),
                                       args.get("style"), bool(args.get("materials")))
        return text, "Checking Flexitallic's RTJ chart", ""
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
        # The Gmail API only estimates the total; IMAP counts it exactly.
        about = "about " if res.get("estimated") else ""
        lines = [f'{about}{res["total"]} emails match {query!r}; newest {len(res["results"])} shown:']
        for h in res["results"]:
            n = refs.ref(h)
            att = f' · attachments: {", ".join(h["attachments"])[:160]}' if h.get("attachments") else ""
            lines.append(f'[{n}] {h["date"]} · from {h["from"]} · to {h["to"]}\n'
                         f'    subject: {h["subject"]}{att}\n'
                         f'    id {h["id"]} · thread {h["thread"]}\n'
                         f'    {h["snippet"]}')
        detail = ("~" if about else "") + f'{res["total"]:,} match' + ("" if res["total"] == 1 else "es")
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
    if name == "orders_overdue":
        return (tracker.orders_overdue(client=str(args.get("client") or ""),
                                       as_of=str(args.get("as_of") or ""), limit=60),
                "Checking overdue orders", "Tracker")
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

# One earlier message is sent back whole up to this - the same as the longest
# question allowed (web/app.py ASK_MAX_CHARS), so a pasted RFQ and the priced
# table that answered it both survive into the follow-up.
HISTORY_MSG_CHARS = 20_000
# The whole chat goes back, like any chat assistant, up to this many
# characters (about 30,000 tokens) - filled newest first; an older message
# that doesn't fit is cut, and says so. The automatic cache makes a follow-up
# re-read it at a tenth of the price.
HISTORY_CHARS = 120_000


def _cut_note(dropped: int) -> str:
    return (f"\n\n[This earlier message was cut here for length - {dropped:,} more characters "
            f"are not shown. It did not end here: don't say it stops at this point. If its "
            f"missing part matters, say you can only see the start and ask for it again.]")


def _clean_history(history) -> list:
    """Earlier turns, as plain text only - the whole chat, within a budget.

    Only the words of earlier questions and answers are sent back - not the
    emails read to produce them. Re-sending those would grow every question by
    the cost of all the ones before it.

    Each message used to be cut to 4,000 characters. A 56-line RFQ pasted in,
    and the 56-row price table that answered it, are 8-10,000 each: the
    follow-up "what's the total?" saw half of both, and Ask told the person
    their paste stopped at item 29. Now messages go back whole, newest first,
    until the budget is spent; anything cut is marked as cut.
    """
    msgs = []
    for m in (history or []):
        role = m.get("role")
        text = str(m.get("content") or "").strip()
        if role in ("user", "assistant") and text:
            msgs.append({"role": role, "content": text})
    out, left = [], HISTORY_CHARS
    for m in reversed(msgs):
        text = m["content"]
        room = min(HISTORY_MSG_CHARS, left)
        if room < 500:
            break                                   # nothing useful fits any more
        if len(text) > room:
            text = text[:room] + _cut_note(len(text) - room)
        out.insert(0, {"role": m["role"], "content": text})
        left -= min(len(m["content"]), room)
    # The API needs alternating turns starting with the user. A question whose
    # answer never came (stopped, or failed to save) leaves two user turns in
    # a row: fold them into one so neither is lost.
    merged: list = []
    for m in out:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1] = {"role": m["role"], "content": merged[-1]["content"] + "\n\n" + m["content"]}
        else:
            merged.append(m)
    while merged and merged[0]["role"] != "user":
        merged.pop(0)
    # The new question is added as a user turn after this: end on the assistant.
    if merged and merged[-1]["role"] == "user":
        merged.append({"role": "assistant", "content": "(No answer was given to this question.)"})
    return merged


def run(question: str, history=None, scope: str = "both", user=None,
        client=None, mail=None, tracker=None, recall=None):
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
    # What Ask remembers about this person and the team, from earlier chats.
    # Like the date, it goes in the message, not the system prompt.
    remembered = ""
    if user is not None and recall is not False:
        try:
            import ask_memory
            remembered = ask_memory.recall_text(user)
        except Exception as exc:
            print(f"  [warn] ask memory not loaded: {exc}")
    messages = _clean_history(history) + [{
        "role": "user",
        # The date goes in the message, not the system prompt, so the system
        # prompt stays byte-identical and cacheable from one question to the next.
        "content": f"(Today is {now:%A %d %b %Y}, Nigeria time.)\n\n"
                   + (f"({remembered})\n\n" if remembered else "") + question.strip(),
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
                # The last round may not search again: it has to answer.
                tool_choice={"type": "none" if rnd == MAX_ROUNDS else "auto"},
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
                    text, label, detail = execute(block.name, block.input, refs, mail, tracker, user)
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
            if rnd == MAX_ROUNDS - 1:
                # Out of searches: answer from what was found rather than
                # throwing it all away. (It once ended with only "couldn't
                # finish" after finding the right thread.)
                results.append({"type": "text", "text": LAST_ROUND_NOTE})
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
    if name == "orders_overdue":
        return "Checking overdue orders"
    if name == "search_quotes":
        return "Searching quotations"
    if name == "stock_on_hand":
        return "Checking counted stock"
    if name == "gasket_price":
        return "Checking the gasket price lists"
    if name == "rtj_chart":
        return "Checking Flexitallic's RTJ chart"
    return name


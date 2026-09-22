"""
export_suppliers_xlsx.py - the Suppliers (Flexitallic) page as an Excel report.

Built for sending to management, so it mirrors the page rather than dumping
rows: the same five summary cards, the same three charts, and the table - twice,
sorted highest to lowest by client PO value and by sales order value.

Every figure is computed the way web/script.js computes it, so the workbook and
the page agree:
  - client PO values are counted ONCE per client PO number, because one PO can
    sit on several sales orders (47 do)
  - sterling sales orders are converted at the rate on their own SO date
  - customers are grouped as Chevron, NLNG, Mobil, Seplat, Aveon and Other

    python scripts/export_suppliers_xlsx.py
    python scripts/export_suppliers_xlsx.py --out=path/to/report.xlsx
"""

import os
import re
import sys
import json
from pathlib import Path
from datetime import datetime, date, timezone
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

load_dotenv()

from openpyxl import Workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont
from openpyxl.chart import BarChart, DoughnutChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.chart.series import DataPoint
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from db import get_client
from fx_history import rate_on

# ── Page palette (web/style.css, light theme) ─────────────────────────────────
MAROON, INK, T2, T3 = "8C1C1C", "0F1A2E", "4A6080", "8A9DB8"
PAGE_BG, CARD_BG, SOFT = "F4F6FC", "FFFFFF", "EDF1F8"
BORDER = "D9DFEA"
CUST_COLOURS = {"CHEVRON": MAROON, "NLNG": "144FA0", "MOBIL": "9E5C18",
                "SEPLAT": "1A7840", "AVEON": "6B4FA0", "Other": T3}
NAMED = ["CHEVRON", "NLNG", "MOBIL", "SEPLAT", "AVEON"]

MONEY = '"$"#,##0.00'
MONEY0 = '"$"#,##0'
FONT = "Calibri"
thin = Side(style="thin", color=BORDER)
BOX = Border(left=thin, right=thin, top=thin, bottom=thin)


# ── Data, computed exactly as the page computes it ────────────────────────────

def _po_list(v) -> list[str]:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return [v]
    return [str(x) for x in (v or [])]


def _group(customer: str | None) -> str:
    return customer if customer in NAMED else "Other"


def _short(n: float) -> str:
    """_fxShort from script.js: $2.65M / $873K / $412."""
    n = float(n or 0)
    if n >= 1e6:
        return f"${n / 1e6:.2f}M"
    if n >= 1e3:
        return f"${round(n / 1e3):,}K"
    return f"${n:,.0f}"


def load() -> dict:
    client = get_client()
    rows, start = [], 0
    while True:
        page = (client.table("scratch_flexitallic_history")
                .select("so_number,so_date,order_value,currency,spm_po_ref,spm_po_reference,"
                        "customer,customer_po_numbers,line_item_count,line_items,"
                        "po_pdf_url,so_pdf_url")
                .order("so_date").range(start, start + 999).execute().data or [])
        rows.extend(page)
        if len(page) < 1000:
            break
        start += 1000

    values = {r["po_number"]: float(r["po_value"])
              for r in client.table("scratch_client_po_values").select("po_number,po_value").execute().data
              if r.get("po_value") is not None}

    on_rows = defaultdict(list)
    for r in rows:
        for n in _po_list(r.get("customer_po_numbers")):
            on_rows[n].append(r["so_number"])

    for r in rows:
        cur = (r.get("currency") or "USD").upper()
        raw = float(r.get("order_value") or 0)
        if cur == "USD":
            r["_val"], r["_factor"] = raw, 1.0
        else:
            rate = rate_on(cur, r.get("so_date"))
            r["_factor"] = (1 / rate) if rate else 1.0
            r["_val"] = raw * r["_factor"]
        r["_year"] = str(r.get("so_date") or "")[:4]
        r["_cust"] = _group(r.get("customer"))
        r["_po"] = r.get("spm_po_ref") or r.get("spm_po_reference") or "—"
        cps = _po_list(r.get("customer_po_numbers"))
        got = [values[n] for n in cps if n in values]
        r["_client_val"] = sum(got) if got else None
        r["_shared"] = {n: [s for s in on_rows[n] if s != r["so_number"]]
                        for n in cps if len(on_rows[n]) > 1}
        li = r.get("line_items")
        r["line_items"] = json.loads(li) if isinstance(li, str) else (li or [])

    # The "counted once" allocation lives in table() instead, because it is
    # done per sheet: within a year, a client PO shared with another year still
    # counts in this one.

    status = (client.table("sync_state").select("updated_at")
              .eq("account", "gmail_flex_history").execute().data or [])
    return {"rows": rows, "values": values,
            "last_checked": status[0]["updated_at"] if status else None}


def client_total(rows, values) -> tuple[float, int, int]:
    """(total, distinct client POs, how many valued) - each PO counted once."""
    seen, total, valued = set(), 0.0, 0
    for r in rows:
        for n in _po_list(r.get("customer_po_numbers")):
            if n in seen:
                continue
            seen.add(n)
            if n in values:
                total += values[n]
                valued += 1
    return total, len(seen), valued


# ── Styling helpers ───────────────────────────────────────────────────────────

def _fill(hex_):
    return PatternFill("solid", fgColor=hex_)


def _paint(ws, rng, colour):
    for row in ws[rng]:
        for c in row:
            c.fill = _fill(colour)


def _card(ws, col, top, label, big, note, active=False):
    """A summary card three columns wide, like .kpi on the page."""
    c1, c3 = get_column_letter(col), get_column_letter(col + 2)
    for r in range(top, top + 4):
        for cc in range(col, col + 3):
            cell = ws.cell(row=r, column=cc)
            cell.fill = _fill(CARD_BG)
    for r in range(top, top + 4):
        ws.cell(row=r, column=col).border = Border(left=Side(style="medium" if active else "thin",
                                                           color=MAROON if active else BORDER))
        ws.cell(row=r, column=col + 2).border = Border(right=thin)
    for cc in range(col, col + 3):
        ws.cell(row=top, column=cc).border = Border(top=thin, left=ws.cell(row=top, column=cc).border.left,
                                                    right=ws.cell(row=top, column=cc).border.right)
        ws.cell(row=top + 3, column=cc).border = Border(bottom=thin, left=ws.cell(row=top + 3, column=cc).border.left,
                                                        right=ws.cell(row=top + 3, column=cc).border.right)
    for r, (text, font) in enumerate([
        (label.upper(), Font(name=FONT, size=9, bold=True, color=T3)),
        (big, Font(name=FONT, size=20, bold=True, color=MAROON if active else INK)),
        (note, Font(name=FONT, size=9, color=T2)),
    ]):
        rr = top + r if r < 2 else top + 2
        ws.merge_cells(f"{c1}{rr}:{c3}{rr}" if r != 2 else f"{c1}{top + 2}:{c3}{top + 3}")
        cell = ws[f"{c1}{rr}"]
        cell.value, cell.font = text, font
        cell.alignment = Alignment(horizontal="left", vertical="top" if r == 2 else "center",
                                   wrap_text=True, indent=1)


# ── Workbook ──────────────────────────────────────────────────────────────────

def build(data: dict, out_path: str) -> str:
    rows, values = data["rows"], data["values"]
    wb = Workbook()

    ctotal, cpos, cvalued = client_total(rows, values)
    so_total = sum(r["_val"] for r in rows)
    lines = sum(int(r.get("line_item_count") or 0) for r in rows)
    newest = max((r.get("so_date") or "" for r in rows), default="")

    # ---------------------------------------------------------------- data sheet
    cd = wb.active
    cd.title = "Chart data"

    years = sorted({r["_year"] for r in rows if r["_year"]})
    cd["A1"], cd["B1"], cd["C1"] = "Year", "Client PO value", "Sales order value"
    for i, y in enumerate(years, 2):
        yr = [r for r in rows if r["_year"] == y]
        cd.cell(row=i, column=1, value=y)
        cd.cell(row=i, column=2, value=round(client_total(yr, values)[0], 2)).number_format = MONEY0
        cd.cell(row=i, column=3, value=round(sum(r["_val"] for r in yr), 2)).number_format = MONEY0
    y_end = len(years) + 1

    groups = [g for g in NAMED + ["Other"] if any(r["_cust"] == g for r in rows)]
    so_by = {g: sum(r["_val"] for r in rows if r["_cust"] == g) for g in groups}
    po_by = {g: client_total([r for r in rows if r["_cust"] == g], values)[0] for g in groups}
    so_order = sorted(groups, key=lambda g: -so_by[g])
    po_order = sorted(groups, key=lambda g: -po_by[g])
    base = y_end + 3
    cd.cell(row=base, column=1, value="Customer (by SO value)")
    cd.cell(row=base, column=2, value="Sales order value")
    for i, g in enumerate(so_order, base + 1):
        cd.cell(row=i, column=1, value=g)
        cd.cell(row=i, column=2, value=round(so_by[g], 2)).number_format = MONEY0
    cd.cell(row=base, column=4, value="Customer (by client PO value)")
    cd.cell(row=base, column=5, value="Client PO value")
    for i, g in enumerate(po_order, base + 1):
        cd.cell(row=i, column=4, value=g)
        cd.cell(row=i, column=5, value=round(po_by[g], 2)).number_format = MONEY0
    c_end = base + len(groups)

    mix, mix_qty = defaultdict(float), defaultdict(float)
    for r in rows:
        for it in r["line_items"]:
            m = re.match(r"^([A-Z]+)", str(it.get("item_number") or "").upper())
            k = m.group(1) if m else "—"
            price = it.get("extended_price") if it.get("extended_price") is not None else it.get("total")
            mix[k] += float(price or 0) * r["_factor"]
            mix_qty[k] += float(it.get("qty") or 0)
    top = sorted(mix, key=lambda k: -mix[k])[:12]
    mbase = c_end + 3
    cd.cell(row=mbase, column=1, value="Flexitallic item code")
    cd.cell(row=mbase, column=2, value="Sales order value")
    cd.cell(row=mbase, column=3, value="Pieces")
    # Reversed so a horizontal bar chart draws the largest at the top.
    for i, k in enumerate(reversed(top), mbase + 1):
        cd.cell(row=i, column=1, value=k)
        cd.cell(row=i, column=2, value=round(mix[k], 2)).number_format = MONEY0
        cd.cell(row=i, column=3, value=round(mix_qty[k]))
    m_end = mbase + len(top)
    for ref in (f"A1:C1", f"A{base}:E{base}", f"A{mbase}:C{mbase}"):
        for row in cd[ref]:
            for c in row:
                if c.value:
                    c.font = Font(name=FONT, bold=True, color=T2)
    for col, w in zip("ABCDE", (28, 18, 18, 30, 18)):
        cd.column_dimensions[col].width = w

    # ------------------------------------------------------------- summary sheet
    ws = wb.create_sheet("Summary", 0)
    ws.sheet_view.showGridLines = False
    for col in range(1, 18):
        ws.column_dimensions[get_column_letter(col)].width = 10.5
    ws.column_dimensions["A"].width = 2
    _paint(ws, "A1:Q72", PAGE_BG)

    ws["B2"] = "Flexitallic"
    ws["B2"].font = Font(name=FONT, size=24, bold=True, color=INK)
    checked = ""
    if data["last_checked"]:
        dt = datetime.fromisoformat(data["last_checked"].replace("Z", "+00:00"))
        checked = f" · checked for new sales orders {dt.astimezone(timezone.utc):%d %b %Y %H:%M} UTC"
    ws["B3"] = ("Purchase orders and sales orders, 2024 to date"
                + checked + (f" · newest SO {newest[:10]}" if newest else ""))
    ws["B3"].font = Font(name=FONT, size=10, color=T3)
    ws["B4"] = f"Prepared {date.today():%d %B %Y} by SPM Tracker · all figures in USD"
    ws["B4"].font = Font(name=FONT, size=9, italic=True, color=T3)

    _card(ws, 2, 6, "Client PO value", _short(ctotal),
          f"${ctotal:,.0f} · what the client paid SPM"
          + (f" · {cpos - cvalued} of {cpos} not valued yet" if cvalued < cpos else ""))
    _card(ws, 5, 6, "SO value", _short(so_total), f"${so_total:,.0f} · what Flexitallic charged", active=True)
    _card(ws, 8, 6, "Client POs", f"{cpos}",
          f"{cvalued} of them carry a value" if cvalued < cpos else "all valued")
    _card(ws, 11, 6, "Sales orders", f"{len(rows)}", "acknowledged by Flexitallic")
    _card(ws, 14, 6, "Gasket lines", f"{lines:,}", "items across all orders")

    ws.merge_cells("B11:P14")
    note = ws["B11"]
    note.value = (
        "How to read these numbers.  Client PO value is the whole value of each client purchase "
        "order, counted once even when it spans several sales orders. SO value is what Flexitallic "
        "charged SPM. The two are not a like-for-like margin: some client POs also cover items SPM "
        "sourced elsewhere (one $100,511 Chevron PO is mainly pipe against a $968 Flexitallic order), "
        "and some sales orders include stock SPM bought for itself. On orders where one client PO maps "
        "to one sales order, clients typically paid about 2.1× Flexitallic's price.")
    note.font = Font(name=FONT, size=9.5, color=T2)
    note.alignment = Alignment(wrap_text=True, vertical="top", indent=1)
    _paint(ws, "B11:P14", SOFT)

    def _card_title(cell, text):
        ws[cell] = text
        ws[cell].font = Font(name=FONT, size=11, bold=True, color=INK)

    def _axes(chart):
        chart.x_axis.delete = False
        chart.y_axis.delete = False
        chart.legend.position = "b" if chart.legend else None

    _card_title("B16", "Order value by year")
    yc = BarChart()
    yc.type, yc.grouping, yc.overlap = "col", "clustered", -10
    yc.add_data(Reference(cd, min_col=2, max_col=3, min_row=1, max_row=y_end), titles_from_data=True)
    yc.set_categories(Reference(cd, min_col=1, min_row=2, max_row=y_end))
    yc.series[0].graphicalProperties.solidFill = T2
    yc.series[1].graphicalProperties.solidFill = MAROON
    for s in yc.series:
        s.graphicalProperties.line.noFill = True
    yc.dataLabels = DataLabelList()
    yc.dataLabels.showVal = True
    yc.y_axis.numFmt = MONEY0
    yc.y_axis.majorGridlines.spPr = None
    yc.height, yc.width = 7.2, 15.5
    _axes(yc)
    ws.add_chart(yc, "B17")

    def _donut(title, anchor, col_lbl, col_val, order):
        dc = DoughnutChart(holeSize=62)
        dc.add_data(Reference(cd, min_col=col_val, min_row=base, max_row=c_end), titles_from_data=True)
        dc.set_categories(Reference(cd, min_col=col_lbl, min_row=base + 1, max_row=c_end))
        for i, g in enumerate(order):
            pt = DataPoint(idx=i)
            pt.graphicalProperties.solidFill = CUST_COLOURS.get(g, T3)
            pt.graphicalProperties.line.solidFill = CARD_BG
            dc.series[0].dPt.append(pt)
        dc.dataLabels = DataLabelList()
        dc.dataLabels.showPercent = True
        dc.title = title
        dc.legend.position = "r"
        dc.height, dc.width = 7.2, 10.5
        ws.add_chart(dc, anchor)

    _card_title("J16", "Value by end customer")
    _donut("Sales order value", "J17", 1, 2, so_order)
    _donut("Client PO value", "J33", 4, 5, po_order)

    _card_title("B32", "Gasket mix by Flexitallic item code")
    mc = BarChart()
    mc.type = "bar"
    mc.add_data(Reference(cd, min_col=2, min_row=mbase, max_row=m_end), titles_from_data=True)
    mc.set_categories(Reference(cd, min_col=1, min_row=mbase + 1, max_row=m_end))
    mc.series[0].graphicalProperties.solidFill = MAROON
    mc.series[0].graphicalProperties.line.noFill = True
    mc.dataLabels = DataLabelList()
    mc.dataLabels.showVal = True
    mc.legend = None
    mc.x_axis.delete = False
    mc.y_axis.delete = False
    mc.y_axis.numFmt = MONEY0
    mc.height, mc.width = 9.5, 15.5
    ws.add_chart(mc, "B33")

    ws["B53"] = ("Sheets: one per year (2024, 2025, 2026), each with its own totals at the top, then "
                 "'By client PO value' and 'By SO value' covering all three years. Every table is sorted "
                 "largest first and has filter arrows on Year and Client; the totals follow the filter.")
    ws["B53"].font = Font(name=FONT, size=9, italic=True, color=T3)

    # --------------------------------------------------------------- table sheets
    def table(title, key, subset=None, heading=None):
        """One table sheet. `subset` defaults to every sales order.

        The "counted once" column is allocated WITHIN the subset: on a year
        sheet a client PO that also touches another year is still counted in
        this year, which is how the page's by-year chart reads too. Across the
        whole book that means a PO spanning 2024 and 2025 appears in both year
        totals - as it should, since it was worked in both.
        """
        table_rows = rows if subset is None else subset
        in_set = {r["so_number"] for r in table_rows}
        seen_home = {}
        for r in sorted(table_rows, key=lambda r: (str(r.get("so_date") or ""), r["so_number"])):
            for n in _po_list(r.get("customer_po_numbers")):
                if n in values and n not in seen_home:
                    seen_home[n] = r["so_number"]
        once, counted_elsewhere = {}, {}
        for r in table_rows:
            mine = [n for n in _po_list(r.get("customer_po_numbers"))
                    if seen_home.get(n) == r["so_number"]]
            once[r["so_number"]] = round(sum(values[n] for n in mine), 2) if mine else None
            counted_elsewhere[r["so_number"]] = {
                n: seen_home[n] for n in _po_list(r.get("customer_po_numbers"))
                if n in values and seen_home.get(n) != r["so_number"]}

        t = wb.create_sheet(title)
        t.sheet_view.showGridLines = False
        cols = [
            # header,                          width, numeric
            ("SPM PO",                           10, False),
            ("Year",                              8, False),
            ("Client",                           13, False),
            ("Client PO",                        38, False),
            ("Flexitallic SO",                   13, False),
            ("SO date",                          11, False),
            ("Client PO value",                  16, True),
            ("Client PO value (counted once)",   17, True),
            ("SO value",                         15, True),
            ("Items",                             8, True),
            ("PO PDF",                            8, False),
            ("SO PDF",                            8, False),
            ("Note",                             55, False),
        ]
        col = {h: i for i, (h, _, _) in enumerate(cols, 1)}
        L = lambda h: get_column_letter(col[h])

        t["A1"] = heading or f"Flexitallic sales orders — {title.lower()}, highest first"
        t["A1"].font = Font(name=FONT, size=14, bold=True, color=INK)
        t["A2"] = ("Use the filter arrows on Year and Client to narrow the list; the totals row "
                   "recalculates for whatever is showing. 'Client PO value' is every client PO on the "
                   "row summed, so it repeats when a PO spans several sales orders - add up "
                   "'counted once' instead."
                   + (" A client PO also worked in another year is counted in this year too, so the "
                      "2024, 2025 and 2026 sheets together come to slightly more than the all-years "
                      "total." if subset is not None else ""))
        t["A2"].font = Font(name=FONT, size=9, color=T3)

        hdr = 6
        first, last = hdr + 1, hdr + len(table_rows)

        # Totals ABOVE the table, driven by SUBTOTAL so they follow the filters
        # (109 = sum of visible rows, 103 = count of visible non-empty cells).
        # Label spans up to (not into) the SO column, which holds the count.
        t.merge_cells(f"A4:{L('Client PO')}4")
        t["A4"] = "Totals for the rows shown"
        t["A4"].font = Font(name=FONT, size=10, bold=True, color=INK)
        t["A4"].alignment = Alignment(horizontal="right", vertical="center")
        totals = [
            ("Flexitallic SO", f"=SUBTOTAL(103,{L('Flexitallic SO')}{first}:{L('Flexitallic SO')}{last})",
             "0", "sales orders"),
            ("Client PO value (counted once)",
             f"=SUBTOTAL(109,{L('Client PO value (counted once)')}{first}:{L('Client PO value (counted once)')}{last})",
             MONEY, "client PO value, each PO once"),
            ("SO value", f"=SUBTOTAL(109,{L('SO value')}{first}:{L('SO value')}{last})", MONEY, "SO value"),
            ("Items", f"=SUBTOTAL(109,{L('Items')}{first}:{L('Items')}{last})", "#,##0", "gasket lines"),
        ]
        for h, formula, fmt, caption in totals:
            c = t.cell(row=4, column=col[h], value=formula)
            c.number_format = fmt
            c.font = Font(name=FONT, size=12, bold=True, color=MAROON)
            c.alignment = Alignment(horizontal="right", vertical="center")
            c.border = Border(bottom=Side(style="medium", color=MAROON))
            cap = t.cell(row=5, column=col[h], value=caption)
            cap.font = Font(name=FONT, size=8, color=T3)
            cap.alignment = Alignment(horizontal="right", vertical="top", wrap_text=True)
        t.row_dimensions[4].height = 22
        t.row_dimensions[5].height = 24

        for i, (h, w, num) in enumerate(cols, 1):
            c = t.cell(row=hdr, column=i, value=h)
            c.font = Font(name=FONT, size=10, bold=True, color="FFFFFF")
            c.fill = _fill(MAROON)
            c.alignment = Alignment(horizontal="right" if num else "left", vertical="center",
                                    wrap_text=True, indent=0 if num else 1)
            t.column_dimensions[get_column_letter(i)].width = w
        t.row_dimensions[hdr].height = 32

        ordered = sorted(table_rows, key=lambda r: (r[key] is None, -(r[key] or 0)))
        for n, r in enumerate(ordered, first):
            zebra = CARD_BG if (n - first) % 2 == 0 else "F8FAFD"
            notes = []
            elsewhere = counted_elsewhere[r["so_number"]]
            if elsewhere:
                where = sorted(set(elsewhere.values()))
                notes.append(f"{', '.join(sorted(elsewhere))} counted once, on {', '.join(where)}")
            if (r.get("currency") or "USD").upper() != "USD":
                notes.append(f"Priced in {r['currency']} {float(r['order_value']):,.2f}, "
                             f"converted at the SO-date rate")
            values_in_row = {
                "SPM PO": r["_po"],
                "Year": int(r["_year"]) if r["_year"].isdigit() else None,
                "Client": (r.get("customer") or "UNKNOWN").upper(),
                "Client PO": ", ".join(_po_list(r.get("customer_po_numbers"))),
                "Flexitallic SO": r["so_number"],
                "SO date": datetime.fromisoformat(str(r["so_date"])[:10]) if r.get("so_date") else None,
                "Client PO value": None if r["_client_val"] is None else round(r["_client_val"], 2),
                "Client PO value (counted once)": once[r["so_number"]],
                "SO value": round(r["_val"], 2),
                "Items": int(r.get("line_item_count") or 0),
                "PO PDF": None, "SO PDF": None,
                "Note": "; ".join(notes) or None,
            }
            for h, _, num in cols:
                c = t.cell(row=n, column=col[h], value=values_in_row[h])
                c.font = Font(name=FONT, size=10, color=INK)
                c.fill = _fill(zebra)
                c.border = Border(bottom=Side(style="thin", color="EEF1F6"))
                c.alignment = Alignment(vertical="top", horizontal="right" if num else "left",
                                        wrap_text=h in ("Client PO", "Note"),
                                        indent=0 if num else 1)
            # Plain text so the filter list reads CHEVRON, NLNG... - the colour
            # carries the page's customer grouping without polluting the value.
            t.cell(row=n, column=col["Client"]).font = Font(
                name=FONT, size=10, bold=True, color=CUST_COLOURS.get(r["_cust"], T3))
            t.cell(row=n, column=col["Year"]).alignment = Alignment(horizontal="left", vertical="top", indent=1)
            t.cell(row=n, column=col["SO date"]).number_format = "dd mmm yy"
            for h in ("Client PO value", "Client PO value (counted once)", "SO value"):
                t.cell(row=n, column=col[h]).number_format = MONEY
            t.cell(row=n, column=col["Client PO value (counted once)"]).font = Font(name=FONT, size=10, color=T2)
            sort_col = "Client PO value" if key == "_client_val" else "SO value"
            if values_in_row[sort_col] is not None:
                t.cell(row=n, column=col[sort_col]).font = Font(name=FONT, size=10, bold=True, color=INK)
            for h, url in (("PO PDF", r.get("po_pdf_url")), ("SO PDF", r.get("so_pdf_url"))):
                if url:
                    c = t.cell(row=n, column=col[h], value="Open")
                    c.hyperlink = url
                    c.font = Font(name=FONT, size=10, color=MAROON, underline="single")

        t.auto_filter.ref = f"A{hdr}:{get_column_letter(len(cols))}{last}"
        t.freeze_panes = f"A{first}"
        t.page_setup.orientation = "landscape"
        t.page_setup.fitToWidth = 1
        t.print_title_rows = f"{hdr}:{hdr}"

    for year in sorted({r["_year"] for r in rows if r["_year"].isdigit()}):
        year_rows = [r for r in rows if r["_year"] == year]
        table(year, "_val", subset=year_rows,
              heading=f"Flexitallic sales orders — {year}, largest first")
    table("By client PO value", "_client_val")
    table("By SO value", "_val")
    # Summary first, the two tables next, the chart source data last - the
    # reader opens straight onto the page view.
    wb.move_sheet("Chart data", offset=len(wb.sheetnames) - 1 - wb.sheetnames.index("Chart data"))

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    wb.save(out_path)
    return out_path


def main() -> None:
    out = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--out=")), None)
    if not out:
        root = Path(__file__).parent.parent
        out = str(root / "exports" / f"Flexitallic_Supplier_Report_{date.today():%Y-%m-%d}.xlsx")
    data = load()
    path = build(data, out)
    ctotal, cpos, cvalued = client_total(data["rows"], data["values"])
    print(f"Wrote {path}")
    print(f"  {len(data['rows'])} sales orders · SO value ${sum(r['_val'] for r in data['rows']):,.2f} "
          f"· client PO value ${ctotal:,.2f} ({cvalued}/{cpos} valued)")


if __name__ == "__main__":
    main()

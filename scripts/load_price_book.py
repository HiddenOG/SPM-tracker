"""
load_price_book.py — read the gasket price lists and weight list into the database.

    python scripts/load_price_book.py --flex "2022 Standard Price List Master Export  - A.pdf"
                                      --spm  "2026 REV 1 SPM PRICE LIST CGI 304-SEL 304304 .pdf"
                                      --weights "GASKET WEIGHTS.xls"  [--dry-run]
    python scripts/load_price_book.py --rtj "Ring_Type_Joints_(RTJ) Gasket Criteria.pdf"

Every gasket on a list becomes one row: what it is (style, materials, size,
class) and its price, so a client's line item can be looked up by what it
describes rather than by reading tables by eye. The files are read, never
stored: SPM's prices live in the database, not in the code.

Run it again when a new list arrives - each source is replaced as a whole, so
a price Flexitallic dropped doesn't linger.

Run on a PC, not on the server: reading the old .xls weights file needs
`pip install xlrd`, which the web app itself doesn't use.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PRICE = re.compile(r"^(?:[£$][\d,]+\.\d{2}|N/A|POA)$")


def size_in(raw: str) -> float | None:
    """'1/2"', '1 1/2"', '1.1/4"', '0.75', '26"' -> inches as a number."""
    s = raw.replace('"', "").strip()
    m = re.fullmatch(r"(\d+)[ .](\d+/\d+)", s)          # 1 1/2 or 1.1/4
    if m:
        return float(int(m.group(1)) + Fraction(m.group(2)))
    try:
        return float(Fraction(s)) if "/" in s else float(s)
    except ValueError:
        return None


def money(tok: str) -> tuple[float | None, bool]:
    """A price cell: (value, price-on-application). N/A gives (None, False)."""
    if tok == "POA":
        return None, True
    if tok == "N/A":
        return None, False
    return float(tok.lstrip("£$").replace(",", "")), False


def _pages(path: str) -> list[str]:
    """Each page's text, laid out as printed. PyMuPDF, not pdfplumber: pdfplumber
    closes up the gaps in these tables, so 1 1/2" reads as 11/2" - five and a half."""
    import fitz
    with fitz.open(path) as doc:
        return [p.get_text("text", sort=True) for p in doc]


def _field(text: str, name: str) -> str | None:
    m = re.search(rf"{name}:[ \t]*([^\n]+)", text)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


def _ratings(text: str) -> list[str]:
    """The class header: '150lb 300lb ...' or 'PN10 PN16 ...'."""
    for line in text.splitlines():
        toks = [t for t in line.split() if t not in ("Pipe", "Size")]
        if len(toks) >= 4 and all(re.fullmatch(r"\d+lb|PN\d+", t) for t in toks):
            return toks
    return []


def _page_no(text: str) -> str:
    m = re.search(r"Page No\.?\s*(\w+)", text)
    return m.group(1) if m else ""


# ── Flexitallic 2022 list (GBP) ──────────────────────────────────────────────

SW_TITLES = {
    "STANDARD SPIRAL WOUND - LOW EMISSION": "Low emission",
    "STANDARD SPIRAL WOUND - LOW STRESS": "Low stress",
    'STANDARD SPIRAL WOUND - ABOVE 24"': "Above 24in",
    "STANDARD SPIRAL WOUND": "",
}


def _spiral_wound(text: str, page: str) -> list[dict]:
    title = next((t for t in SW_TITLES if t in text), None)
    if title is None:
        return []
    standard = "EN1514" if "EN1514" in text else ("ASME B16.47" if "B16.47" in text else "ASME B16.20")
    base = {
        "source": "flex_2022", "page": page, "product": "spiral_wound", "variant": SW_TITLES[title] or None,
        "standard": standard, "style": _field(text, "Style"), "winding": _field(text, "Winding"),
        "filler": _field(text, "Filler"), "outer_ring": _field(text, "Outer Ring"),
        "inner_ring": _field(text, "Inner Ring"), "currency": "GBP",
    }
    ratings = _ratings(text)
    rows = []
    for line in text.splitlines():
        toks = line.split()
        prices = [t for t in toks if PRICE.match(t)]
        if len(prices) < 2:
            continue
        lead = " ".join(toks[: len(toks) - len(prices)])
        if standard == "EN1514":
            if not lead.isdigit():
                continue
            size = {"size_dn": int(lead)}
        else:
            si = size_in(lead)
            if si is None:
                continue
            size = {"size_in": si}
        for i, tok in enumerate(prices):
            if i >= len(ratings):
                break
            price, poa = money(tok)
            if price is None and not poa:
                continue
            series = None
            if standard == "ASME B16.47":
                series = "A" if i < 4 else "B"
            rows.append({**base, **size, "series": series, "rating": ratings[i].replace("lb", ""),
                         "price": price, "poa": poa})
    return rows


def _rtj(text: str, page: str) -> list[dict]:
    rows = []
    base = {"source": "flex_2022", "page": page, "product": "ring_joint", "currency": "GBP"}
    for m in re.finditer(r"\b((?:RX|BX|R)\d+)\s+(\S+)\s+(\S+)", text):
        ring, si, ss = m.groups()
        if not (PRICE.match(si) and PRICE.match(ss)):
            continue
        style = re.match(r"[A-Z]+", ring).group(0)
        for material, tok in (("Soft Iron", si), ("316SS", ss)):
            price, poa = money(tok)
            if price is None and not poa:
                continue
            rows.append({**base, "style": style, "ring_no": ring, "material": material, "price": price, "poa": poa})
    # A one-off on page 4: "R55 SOFT IRON_$74.34 (YES DOLLAR NOT POUNDS)".
    m = re.search(r"R55 SOFT IRON_\$([\d.]+)", text)
    if m:
        rows.append({**base, "style": "R", "ring_no": "R55", "material": "Soft Iron", "price": float(m.group(1)),
                     "poa": False, "currency": "USD", "note": "Written on the list in dollars"})
    return rows


def _size_grid(text: str, page: str, product: str, style: str, note: str, din_col: bool) -> list[dict]:
    """Isoflex and FRG pages: a size and six class prices per line."""
    rows = []
    ratings = ["150", "300", "600", "900", "1500", "2500"]
    for line in text.splitlines():
        toks = line.split()
        prices = [t for t in toks if PRICE.match(t)]
        if len(prices) != 6:
            continue
        lead = toks[: len(toks) - 6]
        if din_col and len(lead) == 2:
            lead = lead[:1]
        si = size_in(" ".join(lead))
        if si is None:
            continue
        for r, tok in zip(ratings, prices):
            price, poa = money(tok)
            if price is None and not poa:
                continue
            rows.append({"source": "flex_2022", "page": page, "product": product, "style": style, "size_in": si,
                         "rating": r, "price": price, "poa": poa, "currency": "GBP", "note": note})
    return rows


# ── Sheets, tape, packing and paste (pages 6 to 10 and 15) ───────────────────
# Priced per sheet, roll, coil or tube rather than by pipe size and class:
# each row carries its grade (style), thickness, sheet size, width and length.

NUM = re.compile(r"^\d+(?:\.\d+)?(?:mm|m)?$")


def _n(tok: str) -> float:
    return float(re.sub(r"[a-z]+$", "", tok))


def _item(page: str, product: str, style: str, tok: str, **kw) -> dict | None:
    price, poa = money(tok)
    if price is None and not poa:
        return None
    return {"source": "flex_2022", "page": page, "product": product, "style": style, "price": price, "poa": poa,
            "currency": "GBP", **{k: v for k, v in kw.items() if v is not None}}


def _grid(text: str, page: str, product: str, cols: list[tuple[str, str | None, str | None]],
          lead: int = 1) -> list[dict]:
    """Rows of a thickness (and for packing, a coil length) then one price per
    column. cols: (grade, sheet size, note) for each price column."""
    rows = []
    for line in text.splitlines():
        toks = line.split()
        if len(toks) != lead + len(cols) or not all(NUM.match(t) for t in toks[:lead]) \
                or not all(PRICE.match(t) for t in toks[lead:]):
            continue
        for (style, sheet, note), tok in zip(cols, toks[lead:]):
            if product == "packing":
                kw = {"width_mm": _n(toks[0]), "length_m": _n(toks[1])}
            else:
                kw = {"thickness_mm": _n(toks[0]), "sheet_size": sheet}
            r = _item(page, product, style, tok, note=note, **kw)
            if r:
                rows.append(r)
    return rows


def _cells(text: str, label: str) -> list[str]:
    """A header row's cells after its label, split where the gaps are wide."""
    for line in text.splitlines():
        if line.strip().startswith(label):
            return re.split(r"\s{2,}", line.strip()[len(label):].strip())
    return []


def _sheet_mm(heading: str) -> str:
    """'(1500mm x 1500mm)', '1.5mx 2m', '1m x 1m' -> '1500x1500' (always the
    smaller side first)."""
    sides = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*mm?", heading)[:2]]
    mm = sorted(int(s if s > 10 else s * 1000) for s in sides)
    return f"{mm[0]}x{mm[1]}"


def _sigma(text: str, page: str) -> list[dict]:
    sheets, _, tape = text.partition("Fluoroseal Tape")
    rows = []
    for part in re.split(r"Prices per sheet", sheets)[1:]:
        size = _sheet_mm(part.split("\n", 1)[0])
        grades = next((re.findall(r"\b(\d{3})\b", l) for l in part.splitlines() if l.strip().startswith("Thickness")), [])
        colours = _cells(part, "mm")
        cols = [(f"Sigma {g}", size, f"{colours[i]} PTFE" if i < len(colours) else None) for i, g in enumerate(grades)]
        rows += _grid(part, page, "sheet", cols)
    for line in tape.splitlines():
        m = re.fullmatch(r"\s*(\d+)mm\s+([\d.]+)mm\s+(\d+)m\s+(\S+)\s*", line)
        if m:
            r = _item(page, "tape", "Fluoroseal", m.group(4), width_mm=float(m.group(1)),
                      thickness_mm=float(m.group(2)), length_m=float(m.group(3)), note="Expanded PTFE joint sealant tape")
            if r:
                rows.append(r)
    return rows


def _sigma_850(text: str, page: str) -> list[dict]:
    """Two tables side by side: thickness, width, length, price - twice per line."""
    rows = []
    for line in text.splitlines():
        toks = line.split()
        i = 0
        while i + 3 < len(toks):
            t, w, l, p = toks[i:i + 4]
            if NUM.match(t) and NUM.match(w) and NUM.match(l) and PRICE.match(p):
                r = _item(page, "tape", "Sigma 850", p, thickness_mm=_n(t), width_mm=_n(w), length_m=_n(l),
                          note="PTFE tape; EU origin, FDA compliant")
                if r:
                    rows.append(r)
                i += 4
            else:
                i += 1
    return rows


AF_MIN_ORDER = ("Minimum order 36 linear metres (24 sheets of 1.5m x 1.5m, 18 of 1.5m x 2m); "
                "SF2800 and SF3300 1.5m x 1.5m are stocked and exempt")


def _af(text: str, page: str) -> list[dict]:
    rows = []
    for part in re.split(r"Price per sheet:?", text)[1:]:
        part = part.split("Minimum sheet order")[0]
        size = _sheet_mm(part.split("\n", 1)[0])
        grades = next((re.findall(r"\b((?:SF|AF)\d{4})\b", l) for l in part.splitlines()
                       if re.search(r"\bSF\d{4}\b", l)), [])
        comp, colour = _cells(part, "Composition"), _cells(part, "Colour")
        cols = []
        for i, g in enumerate(grades):
            note = ", ".join(x for x in (comp[i] if i < len(comp) else "", colour[i] if i < len(colour) else "") if x)
            if not (size == "1500x1500" and g in ("SF2800", "SF3300")):
                note += "; " + AF_MIN_ORDER
            cols.append((g, size, note))
        rows += _grid(part, page, "sheet", cols)
    return rows


def _thermiculite(text: str, page: str) -> list[dict]:
    t815, _, rest = text.partition("THERMICULITE 715")
    t715, _, paste = rest.partition("THERMICULITE PASTE")
    note = "316 reinforced"
    rows = _grid(t815, page, "sheet", [("Thermiculite 815", "1000x1000", note), ("Thermiculite 815", "1500x1500", note)])
    rows += _grid(t715, page, "sheet", [("Thermiculite 715", "1500x1500", None)])
    m = re.search(r"Price per (\S+) tube\s+(\S+)", paste)
    if m:
        rows.append(_item(page, "paste", "Thermiculite paste", m.group(2), variant=f"{m.group(1)} tube"))
    return rows


NOVUS_NOTE = ("Anti-stick or graphite finish +£3.00/m² (0.5-1.5mm), +£6.00/m² above 1.5mm; "
              "metallic +£7.50/m² (0.75mm minimum)")


def _novus(text: str, page: str) -> list[dict]:
    fibre, _, graphite = text.partition("Graphite & High Temperature")
    grades = re.findall(r"NOVUS (\d+)", fibre)
    colour, filler, finish = _cells(fibre, "Colour"), _cells(fibre, "Filler"), _cells(fibre, "Finish")
    cols = []
    for i, g in enumerate(grades):
        bits = [x[i] for x in (colour, filler) if i < len(x)]
        if i < len(finish) and finish[i] != "None":
            bits.append("anti-stick finish" if finish[i] == "A/S" else finish[i])
        cols.append((f"Novus {g}", "1500x1500", ", ".join(bits) + "; " + NOVUS_NOTE))
    rows = _grid(fibre, page, "sheet", cols)
    rows += _grid(graphite, page, "sheet", [("Novus TI", "1000x1000", "Graphite, 316 tanged"),
                                            ("Novus TI", "1500x1500", "Graphite, 316 tanged"),
                                            ("Novus FI", "1000x1000", "Graphite, 316 reinforced"),
                                            ("Novus FI", "1500x1500", "Graphite, 316 reinforced")])
    return rows


RGS_GRADES = [("RGS 1", "Graphite 98%, nickel reinforced"), ("RGS 3", "Graphite 98%, 316 tanged"),
              ("RGS 4", "Graphite 98%, 316 reinforced"), ("RGS HP", "Graphite 98%, high purity")]


def _rgs(text: str, page: str) -> list[dict]:
    first, _, second = text.partition("Prices for 1.5m x 1.5m")
    return (_grid(first, page, "sheet", [(g, "1000x1000", n) for g, n in RGS_GRADES])
            + _grid(second, page, "sheet", [(g, "1500x1500", n) for g, n in RGS_GRADES]))


def _packing(text: str, page: str) -> list[dict]:
    rows = []
    for part in text.split("Size (mm)")[1:]:
        names = re.findall(r"(?:Flex|Therm) \w+", part.split("\n", 1)[0])
        rows += _grid(part, page, "packing", [(n, None, "Braided packing, price per coil") for n in names], lead=2)
    return rows


def _gts(text: str, page: str) -> list[dict]:
    m = re.search(r"GTS Paste\s+(\S+)\s+tube\s+(\S+)", text)
    return [_item(page, "paste", "GTS paste", m.group(2), variant=f"{m.group(1)} tube",
                  note="Bolt lubricant")] if m else []


def read_flex(path: str) -> list[dict]:
    rows: list[dict] = []
    for text in _pages(path):
        page = _page_no(text)
        if "CONTENTS" in text:
            continue
        if "SIGMA REINFORCED PTFE SHEET" in text:
            rows += _sigma(text, page)
        elif "Sigma 850 Tape" in text:
            rows += _sigma_850(text, page)
        elif "AF SHEET SEALING" in text:
            rows += _af(text, page)
        elif "THERMICULITE 815" in text:
            rows += _thermiculite(text, page)
        elif "Non Asbestos Fibre Jointing" in text:
            rows += _novus(text, page)
        elif "GRAPHITE LAMINATES" in text:
            rows += _rgs(text, page)
        elif "BRAIDED PACKINGS" in text:
            rows += _packing(text, page)
        elif "GTS Paste" in text:
            rows += _gts(text, page)
        elif "SPIRAL WOUND" in text:
            rows += _spiral_wound(text, page)
        elif "RING TYPE JOINTS" in text:
            rows += _rtj(text, page)
        elif "ISOFLEX LT" in text:
            rows += _size_grid(text, page, "isolation_kit", "Isoflex LT",
                               "Set: G10 base ring with Corriculite facing, G10 sleeves and washers, zinc plated CS washers", False)
        elif "ISOFLEX FS" in text:
            rows += _size_grid(text, page, "isolation_kit", "Isoflex FS",
                               "Set: 316L Flexpro cores, polyimide film, Corriculite facings, G10 rings and sleeves, mica faced Flexpro washers", False)
        elif "FRG SIGMA" in text:
            rows += _size_grid(text, page, "frg", "FRG Sigma", "316 core, Sigma 500 facing, Sigma 606 inner ring", True)
        elif "FRG HOT" in text:
            rows += _size_grid(text, page, "frg", "FRG Hot", "316 core, Thermiculite facing, Sigma 606 inner ring", True)
    return rows


# ── SPM 2026 list (USD) ──────────────────────────────────────────────────────

def read_spm(path: str) -> list[dict]:
    text = "\n".join(_pages(path))
    m = re.search(r"Validity\s+(\d{2}/\d{2}/\d{4})", text)
    valid = m.group(1) if m else None
    base = {"source": "spm_2026", "page": "1", "product": "spiral_wound", "standard": "ASME B16.20", "style": "CGI",
            "winding": "304L", "filler": "Flexicarb SEL", "outer_ring": "304", "inner_ring": "304", "currency": "USD",
            "note": f"SPM price, valid to {valid}" if valid else "SPM price"}
    ratings = ["150", "300", "600", "900", "1500", "2500"]
    rows = []
    for line in text.splitlines():
        toks = line.split()
        if len(toks) != 7 or not all(PRICE.match(t) for t in toks[1:]):
            continue
        si = size_in(toks[0])
        if si is None:
            continue
        for r, tok in zip(ratings, toks[1:]):
            price, poa = money(tok)
            if price is not None or poa:
                rows.append({**base, "size_in": si, "rating": r, "price": price, "poa": poa})
    return rows


# ── Weights (kg each) ────────────────────────────────────────────────────────

# sheet -> kind; spiral wound sheets are by size and class, ring joints by ring number.
SIZE_SHEETS = {"SG-IR Weights_CGI": "CGI", "SG Weights_CG": "CG", "M18L or ZG Weights": "CAMPROFILE",
               "Elastagraph": "ELASTAGRAPH", "Soft Cut Weights": "SOFT_CUT"}
DIN_SHEETS = {"DIN SWG SG-IR": "CGI", "DIN SWG CG": "CG"}


def read_weights(path: str) -> list[dict]:
    import xlrd
    wb = xlrd.open_workbook(path)
    out: list[dict] = []
    cell = lambda sh, r, c: str(sh.cell_value(r, c)).strip()
    for sh in wb.sheets():
        rows = [[cell(sh, r, c) for c in range(sh.ncols)] for r in range(sh.nrows)]
        hdr = next((i for i, r in enumerate(rows) if r and r[0].upper() in ("SIZE", "RING NO.")), None)
        if sh.name in SIZE_SHEETS or sh.name in DIN_SHEETS:
            if hdr is None:
                continue
            ratings = [re.sub(r"[^\dPN]", "", h.upper()) for h in rows[hdr][1:]]
            for r in rows[hdr + 1:]:
                if not r or not r[0]:
                    continue
                for rating, v in zip(ratings, r[1:]):
                    if not v or not rating:
                        continue
                    row = {"kind": SIZE_SHEETS.get(sh.name) or DIN_SHEETS[sh.name], "rating": rating, "kg": float(v),
                           "sheet": sh.name}
                    if sh.name in DIN_SHEETS:
                        row["size_dn"] = int(r[0].upper().replace("DN", ""))
                    else:
                        si = size_in(r[0])
                        if si is None:
                            continue
                        row["size_in"] = si
                    out.append(row)
        elif sh.name in ("RX-BX RTJ Weights", "R RTJ Weights"):
            oval_octag = sh.name.startswith("R RTJ")
            for r in rows:
                for i, v in enumerate(r):
                    if not re.fullmatch(r"(RX|BX|R)\d+", v or ""):
                        continue
                    style = re.match(r"[A-Z]+", v).group(0)
                    if oval_octag:
                        for j, shape in ((1, "OVAL"), (2, "OCTAG")):
                            if i + j < len(r) and r[i + j]:
                                out.append({"kind": f"R_{shape}", "ring_no": v, "kg": float(r[i + j]), "sheet": sh.name})
                    elif i + 1 < len(r) and r[i + 1]:
                        out.append({"kind": style, "ring_no": v, "kg": float(r[i + 1]), "sheet": sh.name})
        elif sh.name == "Insulation set weights":
            cls = next((r for r in rows if r and not r[0] and r[1:2] and r[1].replace(".0", "").isdigit()), None)
            fe = next((r for r in rows if r and not r[0] and r[1:2] and r[1] in ("F", "E")), None)
            start = next((i for i, r in enumerate(rows) if r and r[0] == "Size"), None)
            if not (cls and fe and start is not None):
                continue
            for r in rows[start + 1:]:
                si = size_in(r[0]) if r and r[0] else None
                if si is None:
                    continue
                for c in range(1, len(r)):
                    if r[c]:
                        out.append({"kind": f"INSUL_{fe[c]}", "size_in": si, "rating": cls[c].replace(".0", ""),
                                    "kg": float(r[c]), "sheet": sh.name})
    return out


# ── Flexitallic's Ring Type Joint guide: every table in it ───────────────────
# A client writes "RTJ, 10 IN, CL2500"; the price list is by ring number (R55).
# The guide's tables join the two, and give each ring's measurements:
#   Style R  (pages 6-7)  ring by ASME class and API pressure, P, A, B, H, gap, weights
#   Style RX (page 11)    ring by API pressure, P, OD, A, H, weight
#   Style BX (page 14)    ring by API pressure, OD, H, A, hole D, weight
#   Materials (page 4)    UNS number, maximum hardness, identification mark
# The mm tables are read; the inch tables (pages 8-9, 15) are the same rings.
# Each row is split where the gaps are wide: "1 1/4" has one space inside it,
# the columns two or more between them.
RTJ_CHART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rtj_ring_chart.json")

# The tolerances printed beside each table (inches), kept with the chart.
RTJ_TOLERANCES = {
    "R": "A (width) ±0.008in; B, H (height) ±0.020in; P (pitch diameter) ±0.007in; 23° angle ±1/2°",
    "RX": "A (width) +0.008/-0in; H (height) +0.008/-0in; OD +0.020/-0in; 23° angle ±1/2°",
    "BX": "A (width) +0.008/-0in; H (height) +0.008/-0in; OD +0/-0.005in; hole D ±0.02in; 23° angle ±1/4°",
}
DASH = {"–", "-", "—"}


def _mm(cell: str) -> float | None:
    return None if cell in DASH else float(cell)


def _pipe(cell: str) -> tuple[float | None, str | None]:
    """A size cell: '2 1/16**' (2000 psi only), '(2 9/16)' (3000 only), '4 1/16 x 4 1/4'."""
    if cell in DASH:
        return None, None
    only = "2000" if cell.endswith("**") else ("3000" if cell.startswith("(") else None)
    s = cell.strip("()*")
    s = s.split(" x ")[0]
    return size_in(s), only


def _ring_rows(text: str, prefix: str) -> list[tuple[str, str, list[str]]]:
    """(ring number, its footnote marks, the other cells) for each table row."""
    out = []
    for line in text.splitlines():
        cells = re.split(r"\s{2,}", line.strip().lstrip("* ").strip())
        m = re.fullmatch(rf"({prefix}\d+)([*†]*)", cells[0])
        if m and len(cells) > 5:
            out.append((m.group(1), m.group(2), cells[1:]))
    return out


def read_ring_chart(path: str) -> dict:
    """{"by_size": [{ring_no, rating, size_in, only?}], "rings": {ring_no: {...mm...}},
        "materials": [...], "tolerances": {...}}"""
    pages = _pages(path)
    by_size: list[dict] = []
    rings: dict[str, dict] = {}
    materials: list[dict] = []
    problems: list[str] = []

    def size_entry(ring, rating, cell, only=None):
        size, api_only = _pipe(cell)
        if size is None:
            return
        if api_only and api_only != rating:
            return
        e = {"ring_no": ring, "rating": rating, "size_in": size}
        if only:
            e["only"] = only
        by_size.append(e)

    for text in pages:
        if "Dimensions in mm" not in text:
            continue
        if "STYLE RX" in text:
            for ring, marks, c in _ring_rows(text, "RX"):
                if len(c) == 7:          # the last row prints no weight at all
                    c = c + ["–"]
                if len(c) != 8:
                    problems.append(f"{ring}: {c}")
                    continue
                seg = "segmented flanges (dual completions)" if "†" in marks else None
                for rating, cell in zip(("2000", "3000", "5000"), c[:3]):
                    size_entry(ring, rating, cell, seg)
                key = ring + ("†" if seg else "")
                rings[key] = {"style": "RX", "pitch_mm": _mm(c[3]), "od_mm": _mm(c[4]), "width_mm": _mm(c[5]),
                              "height_mm": _mm(c[6]), "weight_lb": _mm(c[7]), **({"only": seg} if seg else {})}
        elif "STYLE BX" in text:
            for ring, marks, c in _ring_rows(text, "BX"):
                if len(c) != 9:
                    problems.append(f"{ring}: {c}")
                    continue
                for rating, cell in zip(("5000", "10000", "15000", "20000"), c[:4]):
                    size_entry(ring, rating, cell)
                rings[ring] = {"style": "BX", "od_mm": _mm(c[4]), "height_mm": _mm(c[5]), "width_mm": _mm(c[6]),
                               "hole_mm": _mm(c[7]), "weight_lb": _mm(c[8])}
            # Sizes given only in the footnotes under the BX table.
            for m in re.finditer(r"(BX\d+) is suitable for ([\d /]+?) Nominal Pipe Size ([\d,]+)(?: and ([\d,]+))? psi", text):
                for p in (m.group(3), m.group(4)):
                    if p:
                        by_size.append({"ring_no": m.group(1), "rating": p.replace(",", ""), "size_in": size_in(m.group(2).strip())})
        elif "STYLE R" in text:
            for ring, marks, c in _ring_rows(text, "R"):
                if len(c) not in (12, 14):
                    problems.append(f"{ring}: {c}")
                    continue
                lapped = "lapped flanges" if "†" in marks else None
                # ASME B16.5 / BS / MSS classes, then API 6A 2000/3000 and 5000 psi.
                for ratings, cell in zip((("150",), ("300", "600"), ("900",), ("1500",), ("2500",),
                                          ("2000", "3000"), ("5000",)), c[:7]):
                    for rating in ratings:
                        size_entry(ring, rating, cell, lapped)
                rings[ring] = {"style": "R", "api_6a": "*" in marks, "pitch_mm": _mm(c[7]), "width_mm": _mm(c[8]),
                               "oval_height_mm": _mm(c[9]), "oct_height_mm": _mm(c[10]), "flange_gap_mm": _mm(c[11]),
                               **({"weight_oval_lb": _mm(c[12]), "weight_oct_lb": _mm(c[13])} if len(c) == 14 else {}),
                               **({"only": lapped} if lapped else {})}
    for text in pages:
        if "MAXIMUM HARDNESS" not in text:
            continue
        body = text.split("ROCKWELL", 1)[1].split("Measured with", 1)[0]
        for line in body.splitlines()[1:]:          # [0] is the rest of the header line
            c = re.split(r"\s{2,}", line.strip())
            if not c[0] or c[0].startswith("*"):
                continue
            m = {"material": c[0]}
            rest = c[1:]
            if rest and re.fullmatch(r"[A-Z]\d{5}", rest[0]):
                m["uns"] = rest.pop(0)
            for key, val in zip(("brinell_max", "rockwell_b_max"), [x for x in rest if x.isdigit()]):
                m[key] = int(val)
            ident = [x for x in rest if not x.isdigit()]
            if ident:
                m["identification"] = ident[0]
            materials.append(m)
    if problems:
        print("Rows not read (check by eye):\n  " + "\n  ".join(problems))
    return {"by_size": by_size, "rings": rings, "materials": materials, "tolerances": RTJ_TOLERANCES,
            "notes": ["Hardness: Brinell measured with a 3000 kg load (soft iron 500 kg); Rockwell B with 100 kg and a 1/16in ball.",
                      "Soft iron and low carbon steel rings are zinc plated, at most 8 µm."]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--flex")
    ap.add_argument("--spm")
    ap.add_argument("--weights")
    ap.add_argument("--rtj", help="Flexitallic's Ring Type Joint guide: writes scripts/rtj_ring_chart.json")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.rtj:
        # Published catalogue data, not prices: kept in the code, not the database.
        chart = read_ring_chart(a.rtj)
        print(f"{len(chart['by_size'])} size/class entries, {len(chart['rings'])} rings with measurements, "
              f"{len(chart['materials'])} materials")
        if not a.dry_run:
            with open(RTJ_CHART, "w", encoding="utf-8") as f:
                json.dump(chart, f, indent=0)

    if not (a.flex and a.spm and a.weights):
        if not a.rtj:
            ap.error("give --flex, --spm and --weights together (and/or --rtj)")
        return
    prices = read_flex(a.flex) + read_spm(a.spm)
    weights = read_weights(a.weights)
    by = {}
    for r in prices:
        by[(r["source"], r["product"], r.get("style"), r.get("page"))] = by.get((r["source"], r["product"], r.get("style"), r.get("page")), 0) + 1
    for k, n in sorted(by.items(), key=lambda x: str(x)):
        print(f"  {k}: {n}")
    print(f"{len(prices)} prices, {len(weights)} weights")
    if a.dry_run:
        return

    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    from db import get_client
    db = get_client()
    for src in sorted({r["source"] for r in prices}):
        db.table("price_book").delete().eq("source", src).execute()
        batch = [r for r in prices if r["source"] == src]
        for i in range(0, len(batch), 500):
            db.table("price_book").insert(batch[i:i + 500]).execute()
    db.table("gasket_weights").delete().neq("kind", "").execute()
    for i in range(0, len(weights), 500):
        db.table("gasket_weights").insert(weights[i:i + 500]).execute()
    print("Loaded.")


if __name__ == "__main__":
    main()

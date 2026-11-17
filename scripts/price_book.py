"""
price_book.py — price a client's gasket line from the price lists.

Two steps, kept apart on purpose:

  1. read  - the AI turns each description, in whatever shape the client wrote
             it ("GASKET:SW;SZ 8IN;CL150;SS TP316L;GPH", "8" 150LB SPIRAL WOUND
             ... 316L SS WINDING, GRAPHITE FILLER"), into a fixed set of fields:
             type, size, class, material grade, filler, rings, ring number.
  2. look  - plain code finds those fields in the price_book and gasket_weights
             tables (loaded by load_price_book.py).

A price or weight therefore only ever comes from the lists. The AI never gives
a number; at worst it misreads a field, and the fields it read are shown with
every answer so a person can see why a line matched or didn't. Edge cases get a
second AI look (step 3, verify) that confirms the match or raises a doubt.

Which list answers depends on the grade asked for: 304 is on SPM's 2026 list
(USD), 316/316L on Flexitallic's 2022 list (GBP). A line that doesn't say gets
both, each labelled.
"""
from __future__ import annotations

import json
import os
import time

from db import get_client

READ_MODEL = os.environ.get("PRICE_READ_MODEL", "claude-opus-5-5")

# ── step 1: read the description ─────────────────────────────────────────────

_ENUM = lambda *v: {"type": "string", "enum": list(v)}

# Sheets, tape, packing and paste, by the names Flexitallic's list prices them
# under (pages 6-10 and 15). The reader picks one of these, so a lookup is an
# exact match on the name, never a fuzzy one.
MATERIAL_NAMES = {
    "sheet": ["Sigma 500", "Sigma 511", "Sigma 533", "Sigma 588", "Sigma 600", "Sigma 800",
              "SF2800", "SF3300", "SF5001", "SF4300", "AF2100", "Thermiculite 815", "Thermiculite 715",
              "Novus 10", "Novus 30", "Novus 34", "Novus 45", "Novus 49", "Novus TI", "Novus FI",
              "RGS 1", "RGS 3", "RGS 4", "RGS HP"],
    "tape": ["Fluoroseal", "Sigma 850"],
    "packing": ["Flex 305", "Flex 306", "Flex 774", "Flex 26D", "Flex 45", "Flex 304", "Flex 26L", "Therm 894",
                "Flex 713L", "Flex 803", "Flex 1065", "Flex 900", "Flex 50"],
    "paste": ["Thermiculite paste", "GTS paste"],
}
_MATERIALS = [n for names in MATERIAL_NAMES.values() for n in names]

_SPEC = {
    "type": "object",
    "properties": {
        "kind": _ENUM("spiral_wound", "ring_joint", "isolation_kit", "frg", "sheet", "tape", "packing", "paste",
                      "unspecified", "other"),
        "style": _ENUM("CG", "CGI", "LEI", "LSI", "unknown"),
        "size_in": {"type": ["number", "null"]},
        "size_dn": {"type": ["integer", "null"]},
        "ratings": {"type": "array", "items": {"type": "string"}},
        "grade": _ENUM("304", "316", "304_or_316", "other", "unknown"),
        "filler": _ENUM("graphite", "ptfe", "other", "unknown"),
        "outer_ring": _ENUM("carbon_steel", "304", "316", "other", "none", "unknown"),
        "inner_ring": _ENUM("304", "316", "carbon_steel", "other", "none", "unknown"),
        "b1647_series": _ENUM("A", "B", "unknown"),
        "ring_no": {"type": ["string", "null"]},
        "rtj_material": _ENUM("soft_iron", "316", "304", "other", "unknown"),
        "rtj_shape": _ENUM("oval", "octagonal", "unknown"),
        "rtj_style": _ENUM("R", "RX", "BX", "unknown"),
        "kit_type": _ENUM("FS", "LT", "other", "unknown"),
        "kit_facing": _ENUM("raised_face", "full_face", "ring_joint", "unknown"),
        "frg_type": _ENUM("sigma", "hot", "unknown"),
        "product_name": _ENUM(*_MATERIALS, "other", "unknown"),
        "thickness_mm": {"type": ["number", "null"]},
        "width_mm": {"type": ["number", "null"]},
        "length_m": {"type": ["number", "null"]},
        "sheet_size": {"type": ["string", "null"]},
    },
    "required": ["kind", "style", "size_in", "size_dn", "ratings", "grade", "filler", "outer_ring", "inner_ring",
                 "b1647_series", "ring_no", "rtj_material", "rtj_shape", "rtj_style", "kit_type", "kit_facing",
                 "frg_type", "product_name", "thickness_mm", "width_mm", "length_m", "sheet_size"],
    "additionalProperties": False,
}
_SCHEMA = {
    "type": "object",
    "properties": {"lines": {"type": "array", "items": _SPEC}},
    "required": ["lines"],
    "additionalProperties": False,
}

_PROMPT = """Each line below is a gasket (or other item) from a client's request for quotation. Read each one into the fields of the schema - one entry per line, in the same order. Copy what the line says; never guess a value it doesn't give - use "unknown" or null.

- kind: spiral_wound (SW, SPW, SWG, spiral wound, Flexitallic CG/CGI), ring_joint (RTJ, ring joint, R-/RX-/BX- numbers), isolation_kit (insulation/isolation gasket kit, Isoflex), frg (FRG, faced/reinforced graphite ring gasket), sheet (gasket sheet / jointing sheet to cut from: Sigma, SF/AF fibre sheet, Thermiculite sheet, Novus, RGS graphite laminate), tape (Fluoroseal or Sigma 850 PTFE tape), packing (braided gland packing: Flex 305 ... Therm 894), paste (Thermiculite paste, GTS paste), unspecified (the line asks for a gasket by size and class but never says what kind, e.g. '4" x 150# FLEXITALLIC GASKET'), or other (cut gaskets - including ones cut from a named sheet like Sigma 511 - soft cut, EPDM, camprofile/kammprofile, jacketed, bolts, anything else).
- product_name (sheet, tape, packing, paste - and a gasket cut from one of these sheets): the product as Flexitallic names it, from the list in the schema. "Sigma 500 sheet" = "Sigma 500"; "Novus T.I." = "Novus TI"; "Thermiculite 894 packing" = "Therm 894"; "Flexitallic 305 packing" = "Flex 305". "Flex 304" is a packing name, never a stainless grade. "other" for a product not in the list, "unknown" if no product is named.
- thickness_mm: sheet or tape thickness in mm (1/16" = 1.5, 1/8" = 3). For a ring joint, the thickness or height in mm the line gives ("17.5 MM THICK" = 17.5); null if it gives none. width_mm: tape width, or a packing's square section size in mm ("10mm sq" = 10, 3/8" = 9.5). length_m: tape or coil length in metres. sheet_size: sheet size in mm, smaller side first, as "1500x1500", "1000x1000", "1500x2000", "1750x1750", "2000x2000" (1.5m x 1.5m = "1500x1500").
- style (spiral wound): when the line names a style ("STYLE: CG", "TYPE CGI", part number CG), copy that style exactly, even if other words in the line seem to disagree - the lookup checks the two against each other. Only when it names none: CGI when it has an inner ring (inner ring, IR, SG-IR, "IR/OR"), CG when it has only an outer/centering ring, LEI for low emission, LSI for low stress. "unknown" if the line doesn't say.
- size_in: nominal pipe size in inches as a number (1/2" = 0.5, 1-1/2" = 1.5, 3/4IN = 0.75, "SZ 8IN" = 8). An isolation kit written "3X1/4IN" is 3 inches with a 1/4 inch thickness: size 3. size_dn: the DN number when the size is given as DN (DN150 = 150). Leave both null when only diameters in mm are given.
- ratings: pressure classes as plain numbers, "150", "300", ... ("CL300/600" gives ["300","600"]; "300#" or "300LB" gives ["300"]; PN classes as "PN40"). API 6A pressures as the psi number: "5000 PSI", "API 5000", "5M" give ["5000"]; "10M" gives ["10000"].
- grade: the stainless grade of the winding (spiral wound) or of the ring (ring joint): "304" for 304/304L/TP304, "316" for 316/316L/TP316L, "304_or_316" when the line accepts either ("304/316 SS WINDINGS", "304 OR 316"), "other" for Monel, Inconel/N06625, 347, carbon steel windings and so on.
- filler: graphite (graphite, GPH, GRAF, flexicarb, FG, exfoliated graphite), ptfe, other (mica, ceramic, non-asbestos), unknown.
- outer_ring / inner_ring: carbon_steel (CS, carbon steel, mild steel), 304, 316, other, none, unknown. Client codes often list the materials in order winding - filler - thickness - outer ring - inner ring, so two ring materials after the winding and filler are the outer ring then the inner ring: in "316SS-FLEX GRAPH S-WND 1/8" CS 316 SS" the winding is 316, the filler graphite, the outer ring CS and the inner ring 316 - a CGI. "CS/316", "CS & 316 SS" and "OR CS IR 316" read the same way. Only say "none" when the line says there is no such ring.
- b1647_series: A or B when stated for sizes above 24".
- ring_no: the ring number in its standard form, "R32", "RX24", "BX152" ("R-54" = "R54", "API R14" = "R14").
- rtj_material: soft_iron, 316, 304, other (4-6CR/0.5MOLY, Inconel, ...), unknown. A ring described only as zinc or cadmium plated (no other material) is soft_iron - those are the plated ones; the flange material (ASTM A105) is not the ring's. rtj_shape: oval / octagonal. rtj_style: R, RX or BX when the line says so (or from the ring number), else unknown.
- kit_type: FS or LT for Isoflex kits; other for any other make or model of kit (IsoPro, VCS, Pikotek, ...); unknown when no model is named. kit_facing: the flange facing the kit is for.
- frg_type: sigma or hot for FRG.

The lines:
"""


# Each description's reading, remembered, so the same words always price the
# same way. Without it a borderline line could read as CGI one time and CG the
# next, and two answers to the same enquiry disagreed.
_READ_CACHE: dict[str, dict] = {}
_READ_CACHE_MAX = 5000


def _read_key(d: str) -> str:
    return " ".join(d.split()).upper()[:1500]


def read_lines(descriptions: list[str]) -> list[dict]:
    """Each description as its fields. Raises RuntimeError with a plain message on failure."""
    todo = [d for d in dict.fromkeys(_read_key(d) for d in descriptions) if d not in _READ_CACHE]
    if todo:
        for key, spec in zip(todo, _read_fresh(todo)):
            if len(_READ_CACHE) >= _READ_CACHE_MAX:
                _READ_CACHE.clear()
            _READ_CACHE[key] = spec
    return [dict(_READ_CACHE[_read_key(d)]) for d in descriptions]


def _read_fresh(descriptions: list[str]) -> list[dict]:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("No ANTHROPIC_API_KEY is configured.")
    import anthropic
    numbered = "\n".join(f"{i + 1}. {d.strip()[:1500]}" for i, d in enumerate(descriptions))
    try:
        res = anthropic.Anthropic().messages.create(
            model=READ_MODEL, max_tokens=16000,
            # Medium, not low: on lines that contradict themselves ("STYLE: CG"
            # beside an inner ring material) low effort read them differently
            # from one run to the next.
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[{"role": "user", "content": _PROMPT + numbered}],
        )
    except anthropic.APIError as exc:
        print(f"  [error] price_book.read_lines: {exc}")
        raise RuntimeError("The description reader didn't answer. Try again in a moment.")
    if res.stop_reason == "refusal":
        raise RuntimeError("The description reader declined these lines.")
    if res.stop_reason == "max_tokens":
        raise RuntimeError("Too many lines to read at once - check them in two parts.")
    try:
        lines = json.loads(next(b.text for b in res.content if b.type == "text"))["lines"]
    except Exception:
        raise RuntimeError("The description reader's answer couldn't be read. Try again.")
    if len(lines) != len(descriptions):
        raise RuntimeError("The description reader lost track of the lines. Try again.")
    return lines


# ── the tables, cached ───────────────────────────────────────────────────────

_CACHE: dict = {"at": 0.0, "prices": [], "weights": []}
_TTL = 600.0


def _tables() -> tuple[list[dict], list[dict]]:
    if time.time() - _CACHE["at"] > _TTL or not _CACHE["prices"]:
        db = get_client()

        def every(table: str) -> list[dict]:
            rows, start = [], 0
            while True:
                page = db.table(table).select("*").range(start, start + 999).execute().data or []
                rows += page
                if len(page) < 1000:
                    return rows
                start += 1000

        _CACHE.update(at=time.time(), prices=every("price_book"), weights=every("gasket_weights"))
    return _CACHE["prices"], _CACHE["weights"]


def _num(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ── step 2: look it up ───────────────────────────────────────────────────────

def _weight(weights: list[dict], kind: str, *, size_in=None, size_dn=None, rating=None, ring_no=None) -> float | None:
    for w in weights:
        if w["kind"] != kind:
            continue
        if ring_no is not None:
            if (w.get("ring_no") or "").upper() == ring_no:
                return _num(w["kg"])
            continue
        if rating is not None and str(w.get("rating")) != str(rating):
            continue
        if size_in is not None and _num(w.get("size_in")) == size_in:
            return _num(w["kg"])
        if size_dn is not None and w.get("size_dn") == size_dn:
            return _num(w["kg"])
    return None


def _label(r: dict) -> str:
    src = "SPM 2026 list" if r["source"] == "spm_2026" else f"Flexitallic 2022 list, page {r.get('page')}"
    if r["product"] == "spiral_wound":
        rings = f"{r.get('outer_ring')} outer" + (f", {r.get('inner_ring')} inner" if r.get("inner_ring") else "")
        what = f"{r.get('style')} {r.get('winding')} + {r.get('filler')}, {rings}"
        if r.get("series"):
            what += f", B16.47 series {r['series']}"
        if r.get("variant"):
            what = f"{r['variant']} {what}"
    elif r["product"] == "ring_joint":
        what = f"{r.get('ring_no')} {r.get('material')}"
    elif r["product"] in MATERIAL_NAMES:
        what = _material_label(r)
    else:
        what = r.get("style") or r["product"]
    return f"{what} - {src}"


def _material_label(r: dict) -> str:
    """'SF2800 sheet 1.5 mm, 1500x1500 mm, per sheet', 'Flex 305 packing 10 mm, coil of 8 m'."""
    g = lambda k: _num(r.get(k))
    p = r["product"]
    if p == "sheet":
        return f"{r['style']} sheet {g('thickness_mm'):g} mm, {r.get('sheet_size')} mm, per sheet"
    if p == "tape":
        return f"{r['style']} tape {g('width_mm'):g} mm wide x {g('thickness_mm'):g} mm, roll of {g('length_m'):g} m"
    if p == "packing":
        return f"{r['style']} packing {g('width_mm'):g} mm, coil of {g('length_m'):g} m"
    return f"{r['style']}, {r.get('variant') or 'each'}"


def _hit(r: dict, weight: float | None, note: str | None = None, factor: float = 1.0) -> dict:
    price = _num(r.get("price"))
    return {
        "source": r["source"], "page": r.get("page"), "label": _label(r), "rating": r.get("rating"),
        "price": round(price * factor, 2) if price is not None else None, "currency": r["currency"],
        "poa": bool(r.get("poa")), "weight_kg": weight, "note": "; ".join(x for x in (r.get("note"), note) if x) or None,
    }


_RTJ_CHART: dict | None = None


def _chart() -> dict:
    global _RTJ_CHART
    if _RTJ_CHART is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rtj_ring_chart.json")
        try:
            with open(path, encoding="utf-8") as f:
                _RTJ_CHART = json.load(f)
        except OSError:
            _RTJ_CHART = {}
        for key, empty in (("by_size", []), ("rings", {}), ("materials", []), ("tolerances", {})):
            _RTJ_CHART.setdefault(key, empty)
    return _RTJ_CHART


# ASME B16.5 classes; any other number on the chart is an API 6A pressure in psi.
ASME_CLASSES = {"150", "300", "400", "600", "900", "1500", "2500"}


def _pressure(rating: str) -> str:
    return f"class {rating}" if rating in ASME_CLASSES else f"{rating} psi (API 6A)"


def ring_for(size: float, rating: str, style: str | None = None) -> list[str]:
    """The ring number(s) for a pipe size and class or API pressure, from
    Flexitallic's RTJ chart, optionally only one style (R, RX, BX). Rings for
    lapped or segmented flanges only (R30, RX...†) answer only where nothing
    else does."""
    hits = [r for r in _chart()["by_size"] if _num(r["size_in"]) == size and r["rating"] == rating
            and (not style or r["ring_no"].rstrip("0123456789") == style)]
    usual = [r["ring_no"] for r in hits if not r.get("only")]
    return list(dict.fromkeys(usual or [r["ring_no"] for r in hits]))


def _ring_entry(ring: str) -> dict:
    """A ring's row on the chart. Rings printed only for segmented flanges
    (RX25†) are kept under their mark."""
    rings = _chart()["rings"]
    return rings.get(ring) or rings.get(ring + "†") or {}


def ring_dims(ring: str) -> str:
    """A ring's measurements from the chart, as a short phrase, or ''."""
    d = _ring_entry(ring)
    parts = [f"pitch dia {d['pitch_mm']:g} mm" if d.get("pitch_mm") else "",
             f"OD {d['od_mm']:g} mm" if d.get("od_mm") else "",
             f"{d['width_mm']:g} mm wide" if d.get("width_mm") else "",
             f"{d['oval_height_mm']:g} mm high oval" if d.get("oval_height_mm") else "",
             f"{d['oct_height_mm']:g} mm high octagonal" if d.get("oct_height_mm") else "",
             f"{d['height_mm']:g} mm high" if d.get("height_mm") else "",
             f"pressure hole {d['hole_mm']:g} mm" if d.get("hole_mm") else "",
             "lapped flanges only" if d.get("only") == "lapped flanges" else ""]
    return ", ".join(p for p in parts if p)


def _size_arg(v) -> float | None:
    """'2 1/16', '2-1/16', '1.5', 4 -> inches."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        pass
    from fractions import Fraction
    s = str(v).replace('"', "").replace("in", "").replace("-", " ").strip()
    try:
        return float(sum(Fraction(p) for p in s.split()))
    except (ValueError, ZeroDivisionError):
        return None


def chart_answer(ring_no: str | None = None, size=None, rating: str | None = None,
                 style: str | None = None, materials: bool = False) -> str:
    """Flexitallic's RTJ guide, as plain text for Ask: a ring's sizes and
    measurements, the ring(s) for a size and class or API pressure, or the
    materials table. No prices."""
    c = _chart()
    out: list[str] = []
    rings: list[str] = []
    if ring_no:
        rings = [ring_no.upper().replace("-", "").replace(" ", "")]
    size_in = _size_arg(size)
    if size_in is not None:
        ratings = [str(rating).upper().replace("LB", "").replace("#", "").replace("PSI", "").strip()] if rating else \
            sorted({e["rating"] for e in c["by_size"]}, key=int)
        st = style.upper() if style and style.upper() in ("R", "RX", "BX") else None
        for r in ratings:
            found = ring_for(size_in, r, st)
            if found:
                out.append(f'{size_in:g}" {_pressure(r)}: ' + ", ".join(found))
                rings += found
            elif rating:
                out.append(f'{size_in:g}" {_pressure(r)}: no {st or "R, RX or BX"} ring on the chart')
    for ring in dict.fromkeys(rings):
        d = _ring_entry(ring)
        if not d:
            out.append(f"{ring}: not in Flexitallic's RTJ guide.")
            continue
        uses = [e for e in c["by_size"] if e["ring_no"] == ring]
        fits = ", ".join(f'{e["size_in"]:g}" {_pressure(e["rating"])}' + (f' ({e["only"]} only)' if e.get("only") else "")
                         for e in uses) or "no standard pipe size listed"
        weights = []
        if d.get("weight_oval_lb"):
            weights.append(f"oval {d['weight_oval_lb']:g} lb")
        if d.get("weight_oct_lb"):
            weights.append(f"octagonal {d['weight_oct_lb']:g} lb")
        if d.get("weight_lb"):
            weights.append(f"{d['weight_lb']:g} lb")
        extra = [f"flange gap made up {d['flange_gap_mm']:g} mm" if d.get("flange_gap_mm") else "",
                 "API 6A ring" if d.get("api_6a") else "", ("weight " + ", ".join(weights)) if weights else ""]
        out.append(f"{ring} (Style {d['style']}): fits {fits}. {ring_dims(ring)}"
                   + "".join(f"; {x}" for x in extra if x) + f". Tolerances: {c['tolerances'].get(d['style'], '')}")
    if materials or not out:
        for m in c["materials"]:
            bits = [f"UNS {m['uns']}" if m.get("uns") else "",
                    f"max {m['brinell_max']} Brinell" if m.get("brinell_max") else "",
                    f"{m['rockwell_b_max']} Rockwell B" if m.get("rockwell_b_max") else "",
                    f"marked {m['identification']}" if m.get("identification") else ""]
            out.append(f"{m['material']}: " + ", ".join(b for b in bits if b))
        out += c.get("notes") or []
    return "\n".join(out) or "Nothing on Flexitallic's RTJ chart for that."


def thickness_check(ring: str, mm: float, shape: str | None = None) -> str:
    """A client's "17.5 MM THICK" against the ring's measurements on the chart.
    Clients sometimes give the oval height on an octagonal ring, or the width;
    say which measurement the figure is, so nobody has to guess."""
    d = _ring_entry(ring)
    if not d:
        return f"{mm:g} mm: {ring} has no measurements on Flexitallic's RTJ chart"
    names = {"oct_height_mm": "octagonal height", "oval_height_mm": "oval height", "height_mm": "height",
             "width_mm": "width"}
    order = ["oct_height_mm", "oval_height_mm"] if shape != "oval" else ["oval_height_mm", "oct_height_mm"]
    order += ["height_mm", "width_mm"]
    hits = [k for k in order if d.get(k) and abs(d[k] - mm) <= 0.25]
    heights = ", ".join(f"{names[k]} {d[k]:g} mm" for k in order[:3] if d.get(k))
    if not hits:
        dims = ", ".join(f"{names[k]} {d[k]:g} mm" for k in order if d.get(k))
        return f"Thickness {mm:g} mm doesn't match {ring} on the chart ({dims}) - check with the client"
    k = hits[0]
    stated = {"octagonal": "oct_height_mm", "oval": "oval_height_mm"}.get(shape or "")
    if k == "width_mm":
        return f"Thickness {mm:g} mm is {ring}'s width ({d[k]:g} mm), not its height ({heights}) - check with the client"
    if stated and k != stated and d.get(stated):
        return (f"Thickness {mm:g} mm is {ring}'s {names[k]} ({d[k]:g} mm), but the line says {shape}: "
                f"{ring}'s {names[stated]} is {d[stated]:g} mm - check with the client")
    return f"Thickness {mm:g} mm = {ring}'s {names[k]} ({d[k]:g} mm) on the chart ✓"


# The rings as the lists name them, against the reader's names.
_RING = {"mild steel": "carbon_steel", "316": "316", "304": "304"}
_RING_NAME = {"carbon_steel": "carbon steel", "316": "316", "304": "304"}


def lookup(spec: dict) -> dict:
    """The matching list prices and weights for one read line."""
    prices, weights = _tables()
    kind = spec.get("kind")
    size = _num(spec.get("size_in"))
    dn = spec.get("size_dn")
    ratings = [str(r).upper().replace("LB", "").replace("#", "").strip() for r in spec.get("ratings") or []]
    hits: list[dict] = []
    notes: list[str] = []

    if kind == "spiral_wound":
        if spec.get("grade") == "other":
            return {"status": "not_listed", "matches": [], "reason": "The winding isn't 304 or 316 stainless - not on the lists."}
        if spec.get("filler") == "other":
            return {"status": "not_listed", "matches": [], "reason": "The filler isn't graphite or PTFE - not on the lists."}
        if size is None and dn is None:
            return {"status": "not_listed", "matches": [], "reason": "No nominal size given (only diameters?) - check by hand."}
        if not ratings:
            return {"status": "not_listed", "matches": [], "reason": "No pressure class given."}
        style = spec.get("style")
        if style == "CG" and spec.get("inner_ring") in ("304", "316", "carbon_steel"):
            # "STYLE: CG" beside an inner ring material: a CG has no inner ring,
            # so the line contradicts itself. The ring it names wins - and says so.
            style = "CGI"
            notes.append(f"Line says CG but gives a {_RING_NAME[spec['inner_ring']]} inner ring - read as CGI; "
                         "check with the client")
        if style == "unknown":
            style = "CG" if spec.get("inner_ring") == "none" else "CGI"
            notes.append(f"Style not stated - read as {style}" + ("" if style == "CG" else " (with inner ring)"))
        grades = [spec["grade"]] if spec.get("grade") in ("304", "316") else ["304", "316"]
        if spec.get("grade") == "304_or_316":
            notes.append("Client accepts 304 or 316 - both shown: 304 from SPM's list, 316 from Flexitallic's")
        elif len(grades) == 2:
            notes.append("Grade not stated - both 304 (SPM list) and 316 (Flexitallic list) shown")
        ptfe = spec.get("filler") == "ptfe"
        for rating in ratings:
            for grade in grades:
                pool = []
                if grade == "304":
                    if style == "CGI" and size is not None and not ptfe:
                        pool = [r for r in prices if r["source"] == "spm_2026" and _num(r.get("size_in")) == size
                                and r.get("rating") == rating]
                else:
                    if dn is not None:
                        page = "2c"
                    elif size is not None and size > 24:
                        page = "2d"
                    elif style == "CG":
                        page = "1"
                    elif style == "LEI":
                        page = "2e"
                    elif style == "LSI":
                        page = "2f"
                    else:
                        page = "2a" if spec.get("outer_ring") == "316" else "2"
                    pool = [r for r in prices if r["source"] == "flex_2022" and r.get("page") == page
                            and r.get("rating") == rating
                            and (_num(r.get("size_in")) == size if dn is None else r.get("size_dn") == dn)]
                    if page == "2d" and spec.get("b1647_series") in ("A", "B"):
                        pool = [r for r in pool if r.get("series") == spec["b1647_series"]]
                for r in pool:
                    # A listed item whose rings differ from the ones asked for is
                    # only the nearest match: say so, which also sends the line
                    # to the second look.
                    for side in ("outer_ring", "inner_ring"):
                        asked = spec.get(side)
                        listed = _RING.get(str(r.get(side) or "").lower())
                        if asked in ("carbon_steel", "304", "316") and listed and asked != listed:
                            n = (f"{side.replace('_', ' ').capitalize()} asked {_RING_NAME[asked]}, "
                                 f"the listed item has {_RING_NAME[listed]}")
                            if n not in notes:
                                notes.append(n)
                    wkind = "CG" if style == "CG" else "CGI"
                    w = _weight(weights, wkind, size_in=size, size_dn=dn, rating=rating)
                    if ptfe and r.get("page") == "2":
                        hits.append(_hit(r, w, "PTFE filler: list price + 20%", 1.2))
                    elif ptfe:
                        continue
                    else:
                        hits.append(_hit(r, w))
        if ptfe and not hits:
            return {"status": "not_listed", "matches": [], "reason": "PTFE filler is only priced for standard CGI 316L (page 2)."}
        if "304" in grades and hits and not any(h["source"] == "spm_2026" for h in hits):
            # 304 was asked for (or allowed) but SPM's list had nothing: say why
            # rather than leave "both shown" over a single 316 price.
            why = ("prices CGI only" if style != "CGI" else
                   "has no PTFE filler" if ptfe else
                   "prices by nominal inch size only" if size is None else
                   "doesn't have that size and class")
            notes.append(f"No 304 price - SPM's 304 list {why}")

    elif kind == "ring_joint":
        ring = (spec.get("ring_no") or "").upper().replace("-", "").replace(" ", "")
        if ring:
            rings = [(ring, None)]
            # A ring number written beside a size and class: check the two agree
            # on the chart - "1-13/16in 10000# BX152" is a typo for BX151.
            if size is not None and ratings:
                style = "".join(c for c in ring if c.isalpha())
                for r in ratings:
                    chart = ring_for(size, r, style)
                    if chart and ring not in chart:
                        notes.append(f"Client wrote {ring}, but Flexitallic's RTJ chart gives {' or '.join(chart)} "
                                     f"for {size:g}\" {_pressure(r)} - check with the client")
        else:
            # Clients write size and class; the list is by ring number. Flexitallic's
            # RTJ chart joins the two (load_price_book.py --rtj).
            if size is None or not ratings:
                return {"status": "not_listed", "matches": [], "reason": "No ring number, size or class given."}
            style = spec.get("rtj_style") if spec.get("rtj_style") in ("R", "RX", "BX") else None
            rings = [(n, r) for r in ratings for n in ring_for(size, r, style)]
            if not rings:
                return {"status": "not_listed", "matches": [],
                        "reason": f"No {style or 'R, RX or BX'} ring for {size:g}\" "
                                  f"{' / '.join(_pressure(r) for r in ratings)} on Flexitallic's RTJ chart."}
            notes += [f"{n} is the ring for {size:g}\" {_pressure(r)} on Flexitallic's RTJ chart"
                      + (f" ({ring_dims(n)})" if ring_dims(n) else "") for n, r in rings]
        # A ring number that isn't on the chart isn't a standard ring at all
        # (RX58): that, not its material, is why it can't be priced.
        unknown = [n for n, _ in rings if not _ring_entry(n)]
        if unknown and len(unknown) == len(rings) and _chart()["rings"]:
            return {"status": "not_listed", "matches": [], "notes": notes,
                    "reason": f"{', '.join(unknown)} isn't a standard ring on Flexitallic's RTJ chart - "
                              "ask the client for a drawing or dimensions."}
        mat = spec.get("rtj_material")
        if mat in ("304", "other"):
            return {"status": "not_listed", "matches": [], "notes": notes,
                    "reason": "Ring material isn't soft iron or 316 - not on the list."}
        want = {"soft_iron": ["Soft Iron"], "316": ["316SS"]}.get(mat, ["Soft Iron", "316SS"])
        if mat == "unknown":
            notes.append("Ring material not stated - soft iron and 316 both shown")
        missing = []
        t = _num(spec.get("thickness_mm"))
        if t:
            shape = spec.get("rtj_shape") if spec.get("rtj_shape") in ("oval", "octagonal") else None
            notes += list(dict.fromkeys(thickness_check(ring, t, shape) for ring, _ in rings))
        for ring, rating in rings:
            style = "".join(c for c in ring if c.isalpha())
            if style == "R":
                shape = "OCTAG" if spec.get("rtj_shape") == "octagonal" else "OVAL"
                if spec.get("rtj_shape") == "unknown" and "Shape not stated - weight shown for oval" not in notes:
                    notes.append("Shape not stated - weight shown for oval")
                wk = f"R_{shape}"
            else:
                wk = style
            w = _weight(weights, wk, ring_no=ring)
            same_ring = [r for r in prices if (r.get("ring_no") or "").upper() == ring]
            found = [_hit({**r, "rating": rating or r.get("rating")}, w) for r in same_ring if r.get("material") in want]
            hits += found
            if not found:
                have = ", ".join(sorted({r.get("material") or "?" for r in same_ring}))
                missing.append(f"{ring} is on the list only in {have}, not in {' or '.join(want)}." if same_ring
                               else f"{ring} isn't on the price list.")
        if missing and not hits:
            return {"status": "not_listed", "matches": [], "notes": notes, "reason": " ".join(missing)}
        notes += missing

    elif kind in ("isolation_kit", "frg"):
        if kind == "isolation_kit" and spec.get("kit_type") == "other":
            return {"status": "not_listed", "matches": [],
                    "reason": "Not an Isoflex kit - only Isoflex LT and FS kits are on the list."}
        if size is None or not ratings:
            return {"status": "not_listed", "matches": [], "reason": "Size or class not given."}
        if kind == "isolation_kit":
            styles = {"FS": ["Isoflex FS"], "LT": ["Isoflex LT"]}.get(spec.get("kit_type"), ["Isoflex FS", "Isoflex LT"])
            if spec.get("kit_type") == "unknown":
                notes.append("Kit type not stated - Isoflex FS and LT both shown")
            wk = "INSUL_E" if spec.get("kit_facing") == "full_face" else "INSUL_F"
        else:
            styles = {"sigma": ["FRG Sigma"], "hot": ["FRG Hot"]}.get(spec.get("frg_type"), ["FRG Sigma", "FRG Hot"])
            wk = None
        for rating in ratings:
            w = _weight(weights, wk, size_in=size, rating=rating) if wk else None
            hits += [_hit(r, w) for r in prices if r.get("style") in styles and _num(r.get("size_in")) == size
                     and r.get("rating") == rating]

    elif kind in MATERIAL_NAMES:
        name = spec.get("product_name")
        if name not in MATERIAL_NAMES[kind]:
            return {"status": "not_listed", "matches": [],
                    "reason": f"No {kind} product named that Flexitallic's list prices - it has "
                              + ", ".join(MATERIAL_NAMES[kind]) + "."}
        rows = [r for r in prices if r["product"] == kind and r.get("style") == name]
        # Narrow by each measurement the client gave. One they left out keeps
        # every listed option, said in a note; one that matches nothing says
        # what the list does have.
        for field, unit, label in (("thickness_mm", " mm", "thickness"), ("width_mm", " mm", "width" if kind == "tape" else "size"),
                                   ("length_m", " m", "length"), ("sheet_size", " mm", "sheet size")):
            listed = sorted({r.get(field) for r in rows if r.get(field) is not None},
                            key=lambda v: (_num(v) is None, _num(v) or 0, str(v)))
            if not listed:
                continue
            show = lambda v: f"{_num(v):g}" if _num(v) is not None else str(v)
            want = spec.get(field)
            if want in (None, ""):
                if len(listed) > 1:
                    notes.append(f"{label.capitalize()} not stated - every {label} listed is shown")
                continue
            same = [r for r in rows if (_num(r.get(field)) == _num(want) if _num(want) is not None
                                        else str(r.get(field)).replace(" ", "") == str(want).replace(" ", ""))]
            if not same:
                return {"status": "not_listed", "matches": [], "notes": notes,
                        "reason": f"{name} isn't listed in {show(want)}{unit} {label}; the list has "
                                  + ", ".join(show(v) for v in listed) + f"{unit}."}
            rows = same
        hits = [_hit(r, None) for r in rows]

    elif kind == "unspecified":
        return {"status": "not_listed", "matches": [],
                "reason": "The line doesn't say what kind of gasket (spiral wound, ring joint, flat...) - ask the client "
                          "before pricing."}

    elif spec.get("product_name") in MATERIAL_NAMES["sheet"]:
        # A gasket cut from a listed sheet: the list prices the sheet, not the
        # cut gasket - give the sheet's price as the starting point.
        name = spec["product_name"]
        t = _num(spec.get("thickness_mm"))
        rows = [r for r in prices if r["product"] == "sheet" and r.get("style") == name
                and (t is None or _num(r.get("thickness_mm")) == t)]
        ref = "; ".join(f"{_material_label(r)} {r['currency']} {_num(r['price']):.2f}" if r.get("price") is not None
                        else f"{_material_label(r)} price on application" for r in rows[:4])
        return {"status": "not_listed", "matches": [],
                "reason": f"A gasket cut from {name} - the list prices the sheet, not cut gaskets."
                          + (f" Sheet: {ref}." if ref else f" {name} isn't listed in that thickness.")}

    else:
        return {"status": "not_listed", "matches": [],
                "reason": "Not a gasket, sheet, tape, packing or paste on the lists (spiral wound, ring joint, Isoflex, FRG, "
                          "Sigma/SF/Thermiculite/Novus/RGS sheet, Fluoroseal/Sigma 850 tape, braided packing, paste)."}

    if not hits:
        return {"status": "not_listed", "matches": [], "reason": "That size and class aren't on the lists.", "notes": notes}
    return {"status": "found", "matches": hits, "notes": notes}


# ── step 3: a second look at the edge cases ──────────────────────────────────
# Most lines are plain - one size, one class, one grade - and the lookup is
# exact. A line is an edge case when the lookup had to assume something, found
# more than one row, or found no row for what is a gasket. For those, the AI
# looks again: the client's words beside what was read and matched. It can
# confirm or raise a doubt; it never changes a price or picks a row itself.

_VERIFY_SCHEMA = {
    "type": "object",
    "properties": {"checks": {"type": "array", "items": {
        "type": "object",
        "properties": {"line": {"type": "integer"}, "verdict": _ENUM("ok", "doubt"), "comment": {"type": "string"}},
        "required": ["line", "verdict", "comment"], "additionalProperties": False}}},
    "required": ["checks"],
    "additionalProperties": False,
}

_VERIFY_PROMPT = """These gasket lines from a client's request were read into fields and matched against Flexitallic's 2022 price list (316/316L gaskets, sheets, tape, packing, paste) and SPM's 2026 list (304 spiral wounds). For each line, compare the client's own words with what was read and matched, and say whether the match is right.

- verdict "ok": the read fields and the matched item(s) fit what the client asked for (or, when nothing matched, the item really isn't a listed spiral wound CG/CGI/LEI/LSI, R/RX/BX ring joint, Isoflex kit, FRG gasket, sheet, tape, braided packing or paste in that size, class or thickness).
- verdict "doubt": something doesn't fit - a field misread (size, class, grade, filler, rings, ring number), a match for a different item than asked, an assumption the client's words contradict, or an item that is on the lists but wasn't found.
- comment: one short plain sentence a buyer can act on, e.g. "Client asks for a 316 outer ring, so page 2a applies, not page 2." For ok, say briefly why it fits.
- Each line says what sizes, classes or ring numbers the lists hold for that kind of gasket. Judge whether something is on the lists only from that, never from what sizes are usual in the trade.

The lines:
"""


def _coverage(kind: str | None) -> str:
    """What the lists hold for one kind of gasket, so the second look judges a
    'not listed' from the lists themselves rather than from what sounds usual."""
    prices, _ = _tables()
    rows = [r for r in prices if r["product"] == kind]
    if not rows:
        return "nothing"
    if kind in MATERIAL_NAMES:
        field, unit = ("width_mm", "mm") if kind == "packing" else ("thickness_mm", "mm")
        parts = []
        for name in MATERIAL_NAMES[kind]:
            vals = sorted({_num(r.get(field)) for r in rows if r.get("style") == name and r.get(field) is not None})
            sizes = sorted({r["sheet_size"] for r in rows if r.get("style") == name and r.get("sheet_size")})
            parts.append(name + (f" {'/'.join(f'{v:g}' for v in vals)} {unit}" if vals else "")
                         + (f" in {', '.join(sizes)} mm sheets" if sizes else ""))
        return "; ".join(parts)
    if kind == "ring_joint":
        return "ring numbers " + ", ".join(sorted({r["ring_no"] for r in rows if r.get("ring_no")},
                                                    key=lambda x: (x.rstrip("0123456789"), int("0" + "".join(c for c in x if c.isdigit())))))
    sizes = sorted({_num(r["size_in"]) for r in rows if r.get("size_in") is not None})
    dns = sorted({r["size_dn"] for r in rows if r.get("size_dn")})
    classes = sorted({str(r["rating"]) for r in rows if r.get("rating")}, key=lambda c: (c.startswith("PN"), int("".join(ch for ch in c if ch.isdigit()) or 0)))
    out = "sizes " + ", ".join(f'{s:g}"' for s in sizes) + "; classes " + ", ".join(classes)
    return out + (f"; EN sizes DN{', DN'.join(map(str, dns))}" if dns else "")


def _edge(r: dict) -> bool:
    if r.get("notes"):
        return True
    if r["status"] == "found":
        return len({(m["source"], m.get("page"), m["label"]) for m in r["matches"]}) > 1
    return (r.get("spec") or {}).get("kind") != "other"


def verify(descriptions: list[str], results: list[dict]) -> None:
    """Add an `ai_check` to each edge-case result. Quietly does nothing on failure:
    it is a second opinion, and the lookup's own answer still stands without it."""
    todo = [i for i, r in enumerate(results) if _edge(r)]
    if not todo or not os.environ.get("ANTHROPIC_API_KEY"):
        return
    blocks = []
    for n, i in enumerate(todo, 1):
        r = results[i]
        read = {k: v for k, v in (r.get("spec") or {}).items() if v not in ("unknown", None, [], "none")}
        matched = "; ".join(f'{m["label"]}' + (f' (class {m["rating"]})' if m.get("rating") else "") for m in r["matches"]) \
            or f'nothing - {r.get("reason") or "not found"}'
        kind = (r.get("spec") or {}).get("kind")
        held = f"\n   The lists hold, for this kind: {_coverage(kind)}" if kind != "other" else ""
        blocks.append(f"{n}. Client wrote: {descriptions[i].strip()[:1500]}\n   Read as: {json.dumps(read)}\n"
                      f"   Matched: {matched}\n   Assumptions: {'; '.join(r.get('notes') or []) or 'none'}{held}")
    try:
        import anthropic
        res = anthropic.Anthropic().messages.create(
            model=READ_MODEL, max_tokens=16000,
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": _VERIFY_SCHEMA}},
            messages=[{"role": "user", "content": _VERIFY_PROMPT + "\n\n".join(blocks)}],
        )
        if res.stop_reason in ("refusal", "max_tokens"):
            return
        checks = json.loads(next(b.text for b in res.content if b.type == "text"))["checks"]
    except Exception as exc:
        print(f"  [warn] price_book.verify: {exc}")
        return
    for c in checks:
        n = int(c.get("line") or 0)
        if 1 <= n <= len(todo):
            results[todo[n - 1]]["ai_check"] = {"verdict": c["verdict"], "comment": str(c.get("comment") or "")}


def check(descriptions: list[str], second_look: bool = True) -> list[dict]:
    """Read and look up each description; one result per line, in order."""
    specs = read_lines(descriptions)
    results = [{"spec": s, **lookup(s)} for s in specs]
    if second_look:
        verify(descriptions, results)
    return results

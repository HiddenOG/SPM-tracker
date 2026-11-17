"""
test_price_book.py - looking a read gasket line up in the price lists.

The AI only reads a description into fields; these tests start from those
fields, so they check the rules that decide which list, page and row answer:
304 from SPM's list, 316 from Flexitallic's, both when the grade isn't given.
"""
import time

import price_book as pb
from load_price_book import size_in


def _sw(source, page, size, rating, price, currency="GBP", **kw):
    return {"source": source, "page": page, "product": "spiral_wound", "style": kw.pop("style", "CGI"),
            "winding": "316L" if source == "flex_2022" else "304L", "filler": "Flexicarb SEL",
            "outer_ring": kw.pop("outer_ring", "Mild Steel"), "inner_ring": "316", "size_in": size,
            "rating": rating, "price": price, "currency": currency, "poa": False, **kw}


PRICES = [
    _sw("flex_2022", "2", 8, "150", 6.98),
    _sw("flex_2022", "2", 2, "300", 1.95), _sw("flex_2022", "2", 2, "600", 1.95),
    _sw("flex_2022", "2a", 3, "150", 4.59, outer_ring="316"),
    _sw("flex_2022", "1", 8, "150", 4.04, style="CG"),
    _sw("flex_2022", "2d", 30, "300", 87.39, series="A"), _sw("flex_2022", "2d", 30, "300", 74.15, series="B"),
    _sw("spm_2026", "1", 8, "150", 27.17, currency="USD"),
    {"source": "flex_2022", "page": "4", "product": "ring_joint", "style": "R", "ring_no": "R32",
     "material": "316SS", "price": 8.98, "currency": "GBP", "poa": False},
    {"source": "flex_2022", "page": "4", "product": "ring_joint", "style": "R", "ring_no": "R32",
     "material": "Soft Iron", "price": 4.29, "currency": "GBP", "poa": False},
    {"source": "flex_2022", "page": "14", "product": "isolation_kit", "style": "Isoflex FS", "size_in": 0.75,
     "rating": "1500", "price": 121.62, "currency": "GBP", "poa": False},
]
WEIGHTS = [
    {"kind": "CGI", "size_in": 8, "rating": "150", "kg": 0.46},
    {"kind": "CG", "size_in": 8, "rating": "150", "kg": 0.35},
    {"kind": "R_OVAL", "ring_no": "R32", "kg": 0.66},
    {"kind": "R_OCTAG", "ring_no": "R32", "kg": 0.65},
    {"kind": "INSUL_F", "size_in": 0.75, "rating": "1500", "kg": 0.21},
]


def setup_module():
    pb._CACHE.update(at=time.time() + 10**9, prices=PRICES, weights=WEIGHTS)


def spec(**kw):
    base = {"kind": "spiral_wound", "style": "unknown", "size_in": None, "size_dn": None, "ratings": [],
            "grade": "unknown", "filler": "graphite", "outer_ring": "unknown", "inner_ring": "unknown",
            "b1647_series": "unknown", "ring_no": None, "rtj_material": "unknown", "rtj_shape": "unknown",
            "rtj_style": "unknown", "kit_type": "unknown", "kit_facing": "unknown", "frg_type": "unknown",
            "product_name": "unknown", "thickness_mm": None, "width_mm": None, "length_m": None, "sheet_size": None}
    return {**base, **kw}


def prices(r):
    return [(m["currency"], m["price"]) for m in r["matches"]]


def test_final_usd_price_from_the_2022_formula():
    # [(2.85 x 1.35 x 1.47 x 1.13) + ((0.4 + 0.05) x 12)] x 1.36 = 16.04
    assert pb.quote_usd(2.85, 0.4) == 16.04
    assert pb.quote_usd(2.85, None) is None
    r = pb.lookup(spec(size_in=8, ratings=["150"], grade="316", style="CGI"))
    assert r["matches"][0]["quote_usd"] == pb.quote_usd(6.98, 0.46)
    # SPM's 2026 list: [(62.83 x 1.47) + ((1.46 + 0.05) x 12)] x 1.36 = 150.25
    assert pb.quote_usd(62.83, 1.46, pb.SPM_2026_USD) == 150.25
    spm = pb.lookup(spec(size_in=8, ratings=["150"], grade="304", style="CGI"))["matches"][0]
    assert spm["quote_usd"] == pb.quote_usd(27.17, 0.46, pb.SPM_2026_USD)


def test_the_default_formulas_as_text_give_the_same_prices():
    assert pb.formula_usd(pb.DEFAULT_FORMULAS["flex_2022"], 2.85, 0.4) == 16.04
    assert pb.formula_usd(pb.DEFAULT_FORMULAS["spm_2026"], 62.83, 1.46) == 150.25
    assert pb.formula_usd(pb.DEFAULT_FORMULAS["flex_2022"], 2.85, None) is None      # needs the weight


def test_a_typed_formula_reads_like_a_spreadsheet():
    f = "[(GBP x 1.35 * 1.6 × 1.13) + ((kg+0.05)*15)] x 1.36"
    assert pb.formula_usd(f, 2.85, 0.4) == round((2.85 * 1.35 * 1.6 * 1.13 + 0.45 * 15) * 1.36, 2)
    assert pb.formula_usd("GBP * 3", 10, None) == 30          # no kg in it: no weight needed
    import pytest
    for bad, why in (("(GBP*2", "bracket"), ("foo*2", "foo"), ("kg*12", "list price"), ("GBP / 0", "zero"),
                     ("GBP * 2 +", "complete"), ("__import__('os')", "")):
        with pytest.raises(ValueError, match=why):
            pb.check_formula(bad, "flex_2022")


def test_a_formula_change_lasts_one_question(monkeypatch):
    monkeypatch.setattr(pb, "read_lines", lambda ds: [spec(size_in=8, ratings=["150"], grade="316", style="CGI") for _ in ds])
    default = pb.check(["8in 150 316 CGI"], second_look=False)[0]["matches"][0]
    assert default["quote_usd"] == pb.quote_usd(6.98, 0.46) and default["formula_changed"] is None
    mine = "[(GBP × 1.35 × 1.47) + ((kg + 0.05) × 15)] × 1.36"          # no Flexitallic markup, 15 per kg
    m = pb.check(["8in 150 316 CGI"], second_look=False, formula={"flex_2022": mine})[0]["matches"][0]
    assert m["quote_usd"] == round((6.98 * 1.35 * 1.47 + 0.51 * 15) * 1.36, 2)
    assert m["formula"] == mine and "default" in m["formula_changed"]
    assert pb.check(["8in 150 316 CGI"], second_look=False)[0]["matches"][0]["quote_usd"] == default["quote_usd"]


def test_a_saved_formula_and_a_conversation_change_stack():
    saved = {"flex_2022": "GBP * 4", "spm_2026": "USD * 2"}
    # The conversation changes the Flexitallic one back to the default: that undoes the saved one.
    assert pb.merge_formulas(saved, {"flex_2022": pb.DEFAULT_FORMULAS["flex_2022"]}) == {"spm_2026": "USD * 2"}
    assert pb.merge_formulas(saved, None) == saved and pb.merge_formulas(None, None) == {}


def test_ask_uses_the_persons_saved_formula(monkeypatch):
    import ask_agent
    seen = {}
    monkeypatch.setattr(pb, "check", lambda d, formula=None, **kw: seen.update(formula=formula) or [])
    ask_agent.execute("gasket_price", {"descriptions": ["8in 150 316"], "formula": {"flex_2022": "GBP * 3"}},
                      ask_agent._Refs(), user={"role": "admin", "price_formula": {"flex_2022": "GBP * 2", "spm_2026": "USD * 2"}})
    assert seen["formula"] == {"flex_2022": "GBP * 3", "spm_2026": "USD * 2"}


def test_316_comes_from_flexitallic_with_its_weight():
    r = pb.lookup(spec(size_in=8, ratings=["150"], grade="316", style="CGI"))
    assert prices(r) == [("GBP", 6.98)] and r["matches"][0]["weight_kg"] == 0.46


def test_304_comes_from_the_spm_list():
    r = pb.lookup(spec(size_in=8, ratings=["150"], grade="304", style="CGI"))
    assert prices(r) == [("USD", 27.17)]


def test_no_grade_shows_both_lists():
    r = pb.lookup(spec(size_in=8, ratings=["150"]))
    assert sorted(prices(r)) == [("GBP", 6.98), ("USD", 27.17)]
    assert any("both" in n for n in r["notes"])


def test_316_outer_ring_goes_to_page_2a():
    r = pb.lookup(spec(size_in=3, ratings=["150"], grade="316", style="CGI", outer_ring="316"))
    assert prices(r) == [("GBP", 4.59)] and r["matches"][0]["page"] == "2a"


def test_cg_uses_page_1_and_cg_weights():
    r = pb.lookup(spec(size_in=8, ratings=["150"], grade="316", style="CG"))
    assert prices(r) == [("GBP", 4.04)] and r["matches"][0]["weight_kg"] == 0.35


def test_two_classes_give_two_rows():
    r = pb.lookup(spec(size_in=2, ratings=["300", "600"], grade="316", style="CGI"))
    assert [m["rating"] for m in r["matches"]] == ["300", "600"]


def test_above_24_inches_keeps_to_the_series_asked():
    assert prices(pb.lookup(spec(size_in=30, ratings=["300"], grade="316", b1647_series="B"))) == [("GBP", 74.15)]
    assert len(pb.lookup(spec(size_in=30, ratings=["300"], grade="316"))["matches"]) == 2


def test_ptfe_filler_is_list_price_plus_20_percent():
    r = pb.lookup(spec(size_in=8, ratings=["150"], grade="316", style="CGI", filler="ptfe"))
    assert prices(r) == [("GBP", 8.38)]


def test_other_alloys_are_not_listed():
    r = pb.lookup(spec(size_in=8, ratings=["150"], grade="other"))
    assert r["status"] == "not_listed" and not r["matches"]


def test_a_size_and_class_not_on_the_list():
    assert pb.lookup(spec(size_in=8, ratings=["900"], grade="316"))["status"] == "not_listed"


def test_ring_joint_by_number_material_and_shape():
    r = pb.lookup(spec(kind="ring_joint", ring_no="R-32", rtj_material="316", rtj_shape="octagonal"))
    assert prices(r) == [("GBP", 8.98)] and r["matches"][0]["weight_kg"] == 0.65
    both = pb.lookup(spec(kind="ring_joint", ring_no="R32"))
    assert sorted(prices(both)) == [("GBP", 4.29), ("GBP", 8.98)]


def test_isoflex_kit():
    r = pb.lookup(spec(kind="isolation_kit", size_in=0.75, ratings=["1500"], kit_type="FS"))
    assert prices(r) == [("GBP", 121.62)] and r["matches"][0]["weight_kg"] == 0.21


def test_other_items_are_not_listed():
    assert pb.lookup(spec(kind="other", size_in=24, ratings=["150"]))["status"] == "not_listed"


def test_sizes_read_from_the_lists():
    assert size_in('1 1/2"') == 1.5
    assert size_in('1.1/4"') == 1.25
    assert size_in("0.75") == 0.75
    assert size_in('26"') == 26.0


# ── which lines get the second AI look ───────────────────────────────────────

def test_plain_single_matches_skip_the_second_look():
    r = {"spec": spec(), **pb.lookup(spec(size_in=8, ratings=["150"], grade="316", style="CGI"))}
    assert not pb._edge(r)


def test_assumptions_several_matches_and_missing_gaskets_get_it():
    assumed = {"spec": spec(), **pb.lookup(spec(size_in=8, ratings=["150"], grade="316"))}
    both_lists = {"spec": spec(), **pb.lookup(spec(size_in=8, ratings=["150"], style="CGI"))}
    missing = {"spec": spec(), **pb.lookup(spec(size_in=8, ratings=["900"], grade="316", style="CGI"))}
    flat = {"spec": spec(kind="other"), **pb.lookup(spec(kind="other"))}
    assert pb._edge(assumed) and pb._edge(both_lists) and pb._edge(missing)
    assert not pb._edge(flat)


def test_ask_gives_prices_only_to_admin_and_procurement():
    import ask_agent
    text, _, detail = ask_agent.execute("gasket_price", {"descriptions": ["8in 150 316 SW"]},
                                        ask_agent._Refs(), user={"role": "warehouse"})
    assert detail == "Not allowed" and "£" not in text


def test_different_rings_from_the_listed_item_are_noted():
    r = pb.lookup(spec(size_in=8, ratings=["150"], grade="304", style="CGI", outer_ring="316", inner_ring="316"))
    assert prices(r) == [("USD", 27.17)]
    assert any("Outer ring asked 316" in n for n in r["notes"]) and pb._edge({"spec": spec(), **r})


def test_a_ring_listed_only_in_another_material_says_so():
    r = pb.lookup(spec(kind="ring_joint", ring_no="R32", rtj_material="316"))
    assert r["status"] == "found"
    pb._CACHE["prices"].append({"source": "flex_2022", "page": "4", "product": "ring_joint", "style": "R",
                                "ring_no": "R55", "material": "Soft Iron", "price": 74.34, "currency": "USD", "poa": False})
    r = pb.lookup(spec(kind="ring_joint", ring_no="R55", rtj_material="316"))
    assert r["status"] == "not_listed" and "only in Soft Iron" in r["reason"]


def test_the_same_description_reads_the_same_every_time(monkeypatch):
    calls = []
    monkeypatch.setattr(pb, "_read_fresh", lambda ds: calls.append(list(ds)) or [{"kind": "other"} for _ in ds])
    monkeypatch.setattr(pb, "_saved_readings", lambda keys: {})
    monkeypatch.setattr(pb, "_save_readings", lambda r: None)
    pb._READ_CACHE.clear()
    pb.read_lines(["gasket  8in  cl150", "flat gasket"])
    pb.read_lines(["GASKET 8IN CL150"])
    assert calls == [["GASKET 8IN CL150", "FLAT GASKET"]]


class _ReadingsDb:
    """Just enough of the Supabase client for the price_readings table."""
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail, self.upserts = dict(rows or {}), fail, []
        self._ids = None

    def table(self, name):
        assert name == "price_readings"
        return self

    def select(self, cols):
        return self

    def in_(self, col, ids):
        self._ids = ids
        return self

    def upsert(self, rows, on_conflict=None):
        self.upserts += rows
        self._ids = None
        return self

    def execute(self):
        if self.fail:
            raise RuntimeError("table missing")
        data = [{"id": i, "spec": self.rows[i]} for i in (self._ids or []) if i in self.rows]
        return type("R", (), {"data": data})()


def test_a_saved_reading_is_not_paid_for_again(monkeypatch):
    calls = []
    db = _ReadingsDb({pb._reading_id("R24 SOFT IRON"): {"kind": "ring_joint", "ring_no": "R24"}})
    monkeypatch.setattr(pb, "get_client", lambda: db)
    monkeypatch.setattr(pb, "_read_fresh", lambda ds: calls.append(list(ds)) or [{"kind": "other"} for _ in ds])
    pb._READ_CACHE.clear()
    out = pb.read_lines(["r24  soft iron", "flat gasket"])
    # Only the unseen line goes to the reader, and its reading is saved.
    assert calls == [["FLAT GASKET"]]
    assert out == [{"kind": "ring_joint", "ring_no": "R24"}, {"kind": "other"}]
    assert [(u["id"], u["description"], u["spec"]) for u in db.upserts] == \
        [(pb._reading_id("FLAT GASKET"), "FLAT GASKET", {"kind": "other"})]
    assert db.upserts[0]["rules"] == pb._rules_version()


def test_without_the_readings_table_lines_are_still_read(monkeypatch):
    monkeypatch.setattr(pb, "get_client", lambda: _ReadingsDb(fail=True))
    monkeypatch.setattr(pb, "_read_fresh", lambda ds: [{"kind": "other"} for _ in ds])
    pb._READ_CACHE.clear()
    assert pb.read_lines(["flat gasket"]) == [{"kind": "other"}]


def test_new_reader_rules_or_model_mean_a_fresh_reading(monkeypatch):
    before = pb._reading_id("FLAT GASKET")
    monkeypatch.setattr(pb, "READ_MODEL", pb.READ_MODEL + "-other")
    after_model = pb._reading_id("FLAT GASKET")
    monkeypatch.setattr(pb, "_PROMPT", pb._PROMPT + "\nA new rule.")
    assert len({before, after_model, pb._reading_id("FLAT GASKET")}) == 3


# ── RTJ lines given by size and class ────────────────────────────────────────

def test_ring_numbers_come_from_flexitallics_chart():
    # These are from the real chart file (scripts/rtj_ring_chart.json).
    assert pb.ring_for(10, "2500") == ["R55"]
    assert pb.ring_for(2, "2500") == ["R26"]
    assert pb.ring_for(1.25, "300") == ["R18"] and pb.ring_for(1, "2500") == ["R18"]
    assert pb.ring_for(3, "600") == ["R31"]          # R30 is for lapped flanges only
    assert pb.ring_for(3.5, "900") == []


def test_an_rtj_by_size_and_class_is_priced_by_its_ring_number():
    r = pb.lookup(spec(kind="ring_joint", size_in=3, ratings=["2500"], rtj_material="316", rtj_shape="octagonal"))
    assert prices(r) == [("GBP", 8.98)] and r["matches"][0]["weight_kg"] == 0.65
    assert any('R32 is the ring for 3" class 2500' in n for n in r["notes"])


def test_a_stated_thickness_is_checked_against_the_ring():
    assert "= R32's octagonal height (17.5 mm)" in pb.thickness_check("R32", 17.5, "octagonal")
    oval = pb.thickness_check("R32", 19.1, "octagonal")
    assert "R32's oval height (19.1 mm), but the line says octagonal" in oval and "17.5 mm" in oval
    assert "is R55's width (28.58 mm), not its height" in pb.thickness_check("R55", 28.6, "octagonal")
    assert "doesn't match R13" in pb.thickness_check("R13", 16.0, "octagonal")
    r = pb.lookup(spec(kind="ring_joint", size_in=3, ratings=["2500"], rtj_material="316", rtj_shape="octagonal",
                       thickness_mm=17.5))
    assert any("R32's octagonal height" in n for n in r["notes"])


def test_a_written_ring_number_is_checked_against_size_and_class():
    # Seplat wrote "1-13/16in 10000# BX152"; the chart's ring for that is BX151.
    r = pb.lookup(spec(kind="ring_joint", ring_no="BX152", size_in=1.8125, ratings=["10000"], rtj_material="other"))
    assert any("Client wrote BX152" in n and "BX151" in n for n in r["notes"])
    r = pb.lookup(spec(kind="ring_joint", ring_no="R32", size_in=3, ratings=["2500"], rtj_material="316"))
    assert not any("Client wrote" in n for n in r.get("notes") or [])


def test_a_ring_number_not_on_the_chart_is_not_standard():
    r = pb.lookup(spec(kind="ring_joint", ring_no="RX58", size_in=12, ratings=["1500"], rtj_material="other"))
    assert r["status"] == "not_listed" and "isn't a standard ring" in r["reason"]
    assert pb._ring_entry("RX25")          # printed only as RX25† (segmented flanges) - still standard


def test_304_or_316_shows_both_lists_and_says_why_one_is_missing():
    r = pb.lookup(spec(size_in=8, ratings=["150"], grade="304_or_316", style="CGI"))
    assert sorted(prices(r)) == [("GBP", 6.98), ("USD", 27.17)] and any("accepts 304 or 316" in n for n in r["notes"])
    cg = pb.lookup(spec(size_in=8, ratings=["150"], grade="304_or_316", style="CG"))
    assert prices(cg) == [("GBP", 4.04)] and any("No 304 price - SPM's 304 list prices CGI only" in n for n in cg["notes"])


def test_a_gasket_of_no_stated_kind_asks_the_client():
    r = pb.lookup(spec(kind="unspecified", size_in=4, ratings=["150"]))
    assert r["status"] == "not_listed" and "what kind of gasket" in r["reason"]


def test_a_gasket_cut_from_a_listed_sheet_gives_the_sheet_price(monkeypatch):
    monkeypatch.setitem(pb._CACHE, "prices", PRICES + MATERIALS)
    r = pb.lookup(spec(kind="other", product_name="SF2800", thickness_mm=1.5, size_in=3))
    assert r["status"] == "not_listed" and "cut from SF2800" in r["reason"] and "GBP 51.79" in r["reason"]


def test_other_isolation_kits_are_not_isoflex():
    r = pb.lookup(spec(kind="isolation_kit", kit_type="other", ratings=["150"]))
    assert r["status"] == "not_listed" and "Isoflex" in r["reason"]


def test_api_pressures_and_styles_on_the_chart():
    assert pb.ring_for(2.0625, "5000", "RX") == ["RX24"]
    assert pb.ring_for(13.625, "10000", "BX") == ["BX159"]
    assert pb.ring_for(26.75, "2000") == ["BX167"]                  # from the footnote under the BX table
    assert pb.ring_for(16.75, "2000", "R") == ["R65"]               # 16 3/4** - 2000 psi only
    assert pb.ring_for(16.75, "3000", "R") == []
    assert pb.ring_for(16, "3000", "R") == ["R66"]                  # (16) - 3000 psi only
    assert pb.ring_for(2.0625, "5000", "RX") == ["RX24"]            # RX24 not the segmented-flange RX20


def test_an_api_rtj_by_size_names_its_pressure():
    r = pb.lookup(spec(kind="ring_joint", size_in=13.625, ratings=["10000"], rtj_style="BX", rtj_material="316"))
    assert r["status"] == "not_listed" and any("BX159 is the ring for 13.625\" 10000 psi" in n for n in r["notes"])


def test_the_chart_answers_ring_sizes_measurements_and_materials():
    r45 = pb.chart_answer("R45")
    assert "pitch dia 211.14 mm" in r45 and '6" class 300' in r45 and "API 6A ring" in r45
    assert "BX152" in pb.chart_answer(size="2 1/16", rating="10000")
    mats = pb.chart_answer(materials=True)
    assert "Soft Iron: max 90 Brinell, 56 Rockwell B, marked D" in mats and "UNS S31600" in mats


def test_the_rtj_chart_in_ask_is_open_to_every_role():
    import ask_agent
    text, _, _ = ask_agent.execute("rtj_chart", {"ring_no": "R32"}, ask_agent._Refs(), user={"role": "warehouse"})
    assert "R32 (Style R)" in text and "£" not in text


# ── sheets, tape, packing and paste ──────────────────────────────────────────

def _mat(product, style, price, **kw):
    return {"source": "flex_2022", "page": "7", "product": product, "style": style, "price": price,
            "currency": "GBP", "poa": False, **kw}


MATERIALS = [
    _mat("sheet", "SF2800", 32.38, thickness_mm=1.0, sheet_size="1500x1500"),
    _mat("sheet", "SF2800", 47.49, thickness_mm=1.0, sheet_size="1500x2000"),
    _mat("sheet", "SF2800", 51.79, thickness_mm=1.5, sheet_size="1500x1500"),
    _mat("packing", "Flex 305", 46.59, width_mm=10.0, length_m=8.0),
    _mat("tape", "Fluoroseal", 31.03, width_mm=10.0, thickness_mm=3.0, length_m=10.0),
    _mat("paste", "GTS paste", 60.19, variant="125mg tube"),
]


def mspec(kind, name, **kw):
    return spec(kind=kind, product_name=name, **kw)


def test_a_sheet_by_grade_thickness_and_size(monkeypatch):
    monkeypatch.setitem(pb._CACHE, "prices", PRICES + MATERIALS)
    r = pb.lookup(mspec("sheet", "SF2800", thickness_mm=1, sheet_size="1500x2000"))
    assert prices(r) == [("GBP", 47.49)] and "per sheet" in r["matches"][0]["label"]
    # Size not given: both sheet sizes, and the note says so.
    r = pb.lookup(mspec("sheet", "SF2800", thickness_mm=1))
    assert len(r["matches"]) == 2 and any("Sheet size not stated" in n for n in r["notes"])
    # A thickness the list doesn't have says which it does.
    r = pb.lookup(mspec("sheet", "SF2800", thickness_mm=5))
    assert r["status"] == "not_listed" and "1, 1.5 mm" in r["reason"]


def test_packing_tape_and_paste(monkeypatch):
    monkeypatch.setitem(pb._CACHE, "prices", PRICES + MATERIALS)
    assert prices(pb.lookup(mspec("packing", "Flex 305", width_mm=10))) == [("GBP", 46.59)]
    assert prices(pb.lookup(mspec("tape", "Fluoroseal", width_mm=10))) == [("GBP", 31.03)]
    assert prices(pb.lookup(mspec("paste", "GTS paste"))) == [("GBP", 60.19)]
    assert pb.lookup(mspec("sheet", "other", thickness_mm=1))["status"] == "not_listed"


def test_the_reader_and_lists_name_materials_alike():
    # Every name the reader may pick is one the lookup knows, and no name twice.
    assert len(pb._MATERIALS) == len(set(pb._MATERIALS))
    assert set(pb._SPEC["properties"]["product_name"]["enum"]) == set(pb._MATERIALS) | {"other", "unknown"}


def test_reading_sheet_tape_and_packing_tables():
    import load_price_book as L
    tape = "  2  10  10  £174.69     6  10  10  £360.74\n  6  35  10  N/A\n"
    assert [(r["thickness_mm"], r["width_mm"], r["price"]) for r in L._sigma_850(tape, "6a")] == \
        [(2.0, 10.0, 174.69), (6.0, 10.0, 360.74)]
    pack = "Size (mm)    (m)   Flex 305   Therm 894\n   3.00   20   £10.88   £214.56\n  11.00  8  £58.62  N/A\n"
    rows = L._packing(pack, "10")
    assert [(r["style"], r["width_mm"], r["length_m"], r["price"]) for r in rows] == \
        [("Flex 305", 3.0, 20.0, 10.88), ("Therm 894", 3.0, 20.0, 214.56), ("Flex 305", 11.0, 8.0, 58.62)]
    assert L._sheet_mm("(1500mm x 1500mm)") == "1500x1500" and L._sheet_mm(":1.5mx 2m") == "1500x2000"

"""Storage rules for every seeded medicine, taken from its FDA prescribing information (openFDA drug-label API).

Rule for every text field: words inside double quotes, or a whole quote with no double quotes, are copied
word-for-word from the label (tests/test_labels.py checks them against the saved label text in data/labels/).
Anything LIFELOG decides itself is written outside the quotes or in `notes`, and assumptions are flagged.
Run scripts/verify_labels.py to re-download the labels and see whether any manufacturer has changed one."""
import json
import re
from pathlib import Path

LABELS = Path(__file__).resolve().parent.parent / "data" / "labels"
ASSUME = "Not stated on label (LIFELOG conservative default: 8 h above the highest labeled limit, doubling per 10°C)"
ROOM = "Room-temp allowance"


def _band(label, lo, hi, hours, quote, assumption=False):
    b = {"label": label, "min_c": lo, "max_c": hi, "budget_hours": hours, "quote": quote}
    if assumption:
        b["budget_is_assumption"] = True   # the label allows the range but gives no time limit
    return b


def _model(name, form, tmin, tmax, tq, bands, *, freeze_quote, cold_ok=True, freeze=True, above=8.0,
           above_quote=ASSUME, above_assumed=True, visual=(), discard=(), notes="", in_use=0):
    return {
        "product_name": name, "form": form,
        "target_min_c": tmin, "target_max_c": tmax, "target_quote": tq,
        "freeze_discard": freeze, "freeze_c": 0.0, "freeze_quote": freeze_quote,
        "cold_ok": cold_ok, "bands": bands,
        "above_limit_budget_hours": above, "above_limit_is_assumption": above_assumed, "above_limit_quote": above_quote,
        "in_use_days": in_use, "visual_checks": list(visual), "discard_rules": list(discard), "notes": notes,
    }


def _no_time(quote, limit):
    return f'Label: "{quote}" No time limit is given; LIFELOG assumes 8 h above {limit}°C, doubling per 10°C.'


PRODUCTS = {
    # ---------------------------------------------------------------- insulins
    "insulin": _model(
        "Lantus SoloStar pen (insulin glargine)", "prefilled pen", 2, 8,
        "Store unused LANTUS in a refrigerator between 36°F and 46°F (2°C and 8°C).",
        [_band(ROOM, 8, 30, 28 * 24, 'Storage table: "Not in-use (unopened)" "Room Temperature" "(up to 86°F [30°C])" · '
                                     '"3 mL single-patient-use SoloStar prefilled pen Until expiration date 28 days 28 days '
                                     'Room temperature only (Do not refrigerate)"')],
        freeze_quote="Do not freeze. Discard LANTUS if it has been frozen.",
        discard=["Protect LANTUS from direct heat and light."],
        notes="The storage table gives 28 days at room temperature for an unopened pen and 28 days for an in-use pen, "
              "which the table says to keep at room temperature only (do not refrigerate).", in_use=28),
    "novolog": _model(
        "NovoLog FlexPen (insulin aspart)", "prefilled pen", 2, 8,
        "Store unused NOVOLOG in a refrigerator between 2°C to 8°C (36°F to 46°F).",
        [_band(ROOM, 8, 30, 28 * 24, 'Table 9: "Not in-use (unopened) Room Temperature (up to 30°C [86°F])" · '
                                     '"3 mL single-patient-use FlexPen 28 days Until expiration date 28 days (Do not refrigerate)"')],
        freeze_quote="Do not freeze NOVOLOG and do not use NOVOLOG if it has been frozen.",
        discard=["Do not expose NOVOLOG to excessive heat or light.",
                 "Instruct patients to discard insulin exposed to temperatures higher than 37°C (98.6°F)."], in_use=28),
    "tresiba": _model(
        "Tresiba FlexTouch (insulin degludec)", "prefilled pen", 2, 8,
        "Store unused TRESIBA in a refrigerator (36°F to 46°F [2°C to 8°C]).",
        [_band(ROOM, 8, 30, 56 * 24, 'Table 18: "Room Temperature (up to 86°F [30°C])" · '
                                     '"3 mL single-patient-use TRESIBA U-100 FlexTouch Until expiration date 56 days (8 weeks)"')],
        freeze_quote="Do not freeze. Do not use TRESIBA if it has been frozen.",
        discard=["Do not store in the freezer or directly adjacent to the refrigerator cooling element."], in_use=56),
    "humalog": _model(
        "Humalog KwikPen (insulin lispro)", "prefilled pen", 2, 8,
        'Storage table: "Not In-Use (Unopened) Refrigerated (36° to 46°F [2° to 8°C])"',
        [_band(ROOM, 8, 30, 28 * 24, 'Storage table: "Not In-Use (Unopened) Room Temperature (Up to 86°F [30°C])" · '
                                     '"3 mL single-patient-use Humalog KwikPen 28 days Until expiration date 28 days '
                                     'Room temperature only (Do not refrigerate)"')],
        freeze_quote="Do not freeze and do not use if it has been frozen.",
        discard=["Protect from direct heat and light.",
                 "When stored at room temperature, HUMALOG U-100 and U-200 can only be used for a total of 28 days, "
                 "including both not in-use (unopened) and in-use (opened) storage time.",
                 "Change the HUMALOG U-100 in the reservoir at least every 7 days, or according to the pump user manual, "
                 "whichever is shorter, or after exposure to temperatures that exceed 98.6°F (37°C)."], in_use=28),
    "toujeo": _model(
        "Toujeo SoloStar pen, in use (insulin glargine U-300)", "prefilled pen", 2, 8,
        'Storage table: "Not in-use (unopened) Refrigerated 36°F–46°F (2°C–8°C)"',
        [_band(ROOM, 8, 30, 56 * 24, 'Storage table: "Room temperature only (Do not refrigerate) up to 86°F (30°C)" · '
                                     '"1.5 mL SoloStar single-patient-use prefilled pen Until expiration date 56 days"')],
        freeze_quote="TOUJEO SoloStar or TOUJEO Max SoloStar prefilled pen should not be stored in the freezer and should "
                     "not be allowed to freeze. Discard TOUJEO prefilled pen if it has been frozen.",
        discard=["Protect TOUJEO SoloStar/TOUJEO Max SoloStar from direct heat and light.",
                 "To prevent degradation, always store the prefilled pens with the cap on during in-use period."],
        notes="The 56 days are for a pen in use (opened). The table gives no room-temperature allowance for unopened pens.",
        in_use=56),
    # ---------------------------------------------------------------- GLP-1 / GIP
    "glp1": _model(
        "Mounjaro single-dose pen (tirzepatide)", "single-dose pen", 2, 8,
        "Store MOUNJARO single-dose pen and single-dose vial in a refrigerator at 2°C to 8°C (36°F to 46°F).",
        [_band(ROOM, 8, 30, 21 * 24, "If needed, each single-dose pen or single-dose vial can be stored unrefrigerated at "
                                    "temperatures not to exceed 30°C (86°F) for up to a total of 21 days.")],
        freeze_quote="Do not freeze MOUNJARO. Do not use MOUNJARO if frozen.",
        discard=["Discard the single-dose pen or single-dose vial after a total of 21 days at room temperature.",
                 "Protect MOUNJARO from heat and light."]),
    "zepbound": _model(
        "Zepbound single-dose pen (tirzepatide)", "single-dose pen", 2, 8,
        "Store ZEPBOUND single-dose pen and single-dose vial in a refrigerator at 2°C to 8°C (36°F to 46°F).",
        [_band(ROOM, 8, 30, 21 * 24, "If needed, each single-dose pen or single-dose vial can be stored unrefrigerated at "
                                    "temperatures not to exceed 30°C (86°F) for up to a total of 21 days.")],
        freeze_quote="Do not freeze ZEPBOUND. Do not use ZEPBOUND if frozen.",
        discard=["Discard single-dose pen and single-dose vial after a total of 21 days at room temperature.",
                 "Protect ZEPBOUND from heat and light."]),
    "trulicity": _model(
        "Trulicity pen (dulaglutide)", "single-dose pen", 2, 8,
        "Store TRULICITY in the refrigerator at 36°F to 46°F (2°C to 8°C).",
        [_band(ROOM, 8, 30, 14 * 24, "If needed, each single-dose pen can be kept at room temperature, not to exceed "
                                    "86°F (30°C) for a total of 14 days.")],
        freeze_quote="Do not freeze TRULICITY. Do not use TRULICITY if it has been frozen.",
        discard=["Protect TRULICITY from light."]),
    "victoza": _model(
        "Victoza pen, in use (liraglutide)", "multi-dose pen", 2, 8,
        "Prior to first use, VICTOZA should be stored in a refrigerator between 36°F to 46°F (2°C to 8°C).",
        [_band(ROOM, 8, 30, 30 * 24, "After first use of the VICTOZA pen, the pen can be stored for 30 days at controlled "
                                    "room temperature 59°F to 86°F (15°C to 30°C) or in a refrigerator 36°F to 46°F (2°C to 8°C).")],
        freeze_quote="Do not freeze VICTOZA and do not use VICTOZA if it has been frozen.",
        discard=["Protect VICTOZA from excessive heat and sunlight.",
                 "You can use your Victoza pen for up to 30 days after you use it the first time."],
        notes="The label gives 15–30°C; LIFELOG applies the same allowance to 8–15°C. The 30 days after first use apply "
              "even in the fridge; LIFELOG's budget only tracks heat.", in_use=30),
    # ---------------------------------------------------------------- biologics
    "biologic": _model(
        "Enbrel SureClick (etanercept)", "autoinjector", 2, 8,
        "Enbrel should be refrigerated at 36°F to 46°F (2°C to 8°C) in the original carton to protect from light or physical damage.",
        [_band(ROOM, 8, 25, 30 * 24, "storage of individual single-dose prefilled syringes, SureClick autoinjectors, single-dose vials, "
                                    "or Enbrel Mini cartridges at room temperature at 68°F to 77°F (20°C to 25°C) for a maximum "
                                    "single period of 30 days is permissible, with protection from light and sources of heat.")],
        freeze_quote="DO NOT FREEZE.", above_quote=_no_time("Do not store Enbrel in extreme heat or cold.", 25),
        discard=["it should not be placed back into the refrigerator.",
                 "If not used within 30 days at room temperature, the single-dose prefilled syringe, SureClick autoinjector, "
                 "single-dose vial, or Enbrel Mini cartridge should be discarded.", "DO NOT SHAKE."],
        notes="The label gives 20–25°C; LIFELOG applies the same allowance to 8–20°C."),
    "humira": _model(
        "Humira Pen (adalimumab)", "single-dose pen", 2, 8,
        "HUMIRA must be refrigerated at 36°F to 46°F (2°C to 8°C).",
        [_band(ROOM, 8, 25, 14 * 24, "If needed, for example when traveling, HUMIRA may be stored at room temperature up to a "
                                    "maximum of 77°F (25°C) for a period of up to 14 days, with protection from light.")],
        freeze_quote="DO NOT FREEZE. Do not use if frozen even if it has been thawed.",
        above_quote=_no_time("Do not store HUMIRA in extreme heat or cold.", 25),
        discard=["HUMIRA should be discarded if not used within the 14-day period.",
                 "Record the date when HUMIRA is first removed from the refrigerator in the spaces provided on the carton and dose tray."]),
    "dupixent": _model(
        "Dupixent pre-filled pen (dupilumab)", "single-dose pen", 2, 8,
        "Store refrigerated at 2°C to 8°C (36°F to 46°F) in the original carton to protect from light.",
        [_band(ROOM, 8, 25, 14 * 24, "If necessary, DUPIXENT may be kept at room temperature up to 25°C (77°F) for a maximum of 14 days.")],
        freeze_quote="Do NOT freeze.", above_quote=_no_time("Do not store above 25°C (77°F).", 25),
        discard=["After removal from the refrigerator, DUPIXENT must be used within 14 days or discarded.",
                 "Do not expose DUPIXENT to heat or direct sunlight.", "Do NOT shake."]),
    "stelara": _model(
        "Stelara prefilled syringe (ustekinumab)", "prefilled syringe", 2, 8,
        "Store STELARA vials and prefilled syringes refrigerated between 2 °C to 8 °C (36 °F to 46 °F).",
        [_band(ROOM, 8, 30, 30 * 24, "If needed, individual prefilled syringes may be stored at room temperature up to 30 °C (86 °F) "
                                    "for a maximum single period of up to 30 days in the original carton to protect from light.")],
        freeze_quote="Do not freeze.",
        discard=["Once a syringe has been stored at room temperature, do not return to the refrigerator.",
                 "Discard the syringe if not used within 30 days at room temperature storage.", "Do not shake."]),
    # ---------------------------------------------------------------- cholesterol, migraine, bone
    "repatha": _model(
        "Repatha SureClick (evolocumab)", "autoinjector", 2, 8,
        "Store refrigerated at 2°C to 8°C (36°F to 46°F) in the original carton to protect from light.",
        [_band(ROOM, 8, 25, 30 * 24, "may also be kept at room temperature at 68°F to 77°F (20°C to 25°C) in the original carton for 30 days.")],
        freeze_quote="Do not freeze.", discard=["If not used within the 30 days, discard REPATHA.", "Do not shake."],
        notes="The label gives 20–25°C; LIFELOG applies the same allowance to 8–20°C."),
    "praluent": _model(
        "Praluent pen (alirocumab)", "single-dose pen", 2, 8,
        "Store in a refrigerator at 36°F to 46°F (2°C to 8°C) in the original carton to protect from light.",
        [_band(ROOM, 8, 25, 30 * 24, "PRALUENT may be kept at room temperature up to 77°F (25°C) in the original carton for 30 days.")],
        freeze_quote="Do not freeze.", discard=["If not used within the 30 days, discard PRALUENT.", "Do not shake."]),
    "aimovig": _model(
        "Aimovig SureClick (erenumab)", "autoinjector", 2, 8,
        "Store refrigerated at 2°C to 8°C (36°F to 46°F) in the original carton to protect from light until time of use.",
        [_band(ROOM, 8, 25, 7 * 24, "If removed from the refrigerator, AIMOVIG should be kept at room temperature (up to 25°C [77°F]) "
                                   "in the original carton and must be used within 7 days.")],
        freeze_quote="Do not freeze.",
        discard=["Throw away AIMOVIG that has been left at room temperature for more than 7 days.", "Do not shake."]),
    "emgality": _model(
        "Emgality pen (galcanezumab)", "single-dose pen", 2, 8,
        "Store refrigerated at 2°C to 8°C (36°F to 46°F) in the original carton to protect EMGALITY from light until use.",
        [_band(ROOM, 8, 30, 7 * 24, "EMGALITY may be stored out of refrigeration in the original carton at temperatures up to "
                                   "30°C (86°F) for up to 7 days.")],
        freeze_quote="Do not freeze.",
        discard=["Once stored out of refrigeration, do not place back in the refrigerator.",
                 "If these conditions are exceeded, EMGALITY must be discarded.", "Do not shake."]),
    "prolia": _model(
        "Prolia prefilled syringe (denosumab)", "prefilled syringe", 2, 8,
        "Store Prolia refrigerated at 2°C to 8°C (36°F to 46°F) in the original carton to protect from light.",
        [_band(ROOM, 8, 25, 30 * 24, "Once removed from the refrigerator, Prolia must not be exposed to temperatures above 25°C (77°F) "
                                    "and must be used within 30 days.")],
        freeze_quote="Do not freeze.",
        discard=["Discard Prolia if not used within the 30 days.", "Protect Prolia from direct light and heat.",
                 "Avoid vigorous shaking of Prolia."]),
    "forteo": _model(
        "Forteo pen (teriparatide)", "multi-dose pen", 2, 8,
        "Store FORTEO under refrigeration at 2° to 8°C (36° to 46°F) at all times except when administering the product.",
        [], freeze_quote="Do not freeze. Do not use FORTEO if it has been frozen.",
        above_quote=_no_time("When using FORTEO, minimize the time out of the refrigerator; deliver the dose immediately "
                             "following removal from the refrigerator.", 8),
        discard=["Throw away the device 28 days after first use.",
                 "Recap the delivery device (pen) when not in use to protect the cartridge from physical damage and light."],
        notes="The label allows no room-temperature storage at all, so any time above 8°C uses budget.", in_use=28),
    # ---------------------------------------------------------------- room-temperature emergency medicines
    "epi": _model(
        "EpiPen auto-injector (epinephrine)", "auto-injector", 20, 25,
        "Store at 20°C to 25°C (68°F to 77°F); excursions permitted to 15°C to 30°C (59°F to 86°F) [See USP Controlled Room Temperature].",
        [_band("Cool excursion", 15, 20, 90 * 24, "excursions permitted to 15°C to 30°C (59°F to 86°F)", assumption=True),
         _band("Warm excursion", 25, 30, 90 * 24, "excursions permitted to 15°C to 30°C (59°F to 86°F)", assumption=True)],
        freeze_quote='Not stated on label (it says: "Do not refrigerate.")', freeze=False, cold_ok=False,
        visual=["Before using, check to make sure the solution in the auto-injector is clear and colorless.",
                "Replace the auto-injector if the solution is discolored (pinkish or brown color), cloudy, or contains particle."],
        discard=["Epinephrine is light sensitive and should be stored in the carrier tube provided to protect it from light.",
                 "Do not refrigerate."],
        notes="The label permits excursions without a time limit; LIFELOG assumes 90 days of cumulative excursion."),
    "gvoke": _model(
        "Gvoke HypoPen (glucagon)", "auto-injector", 20, 25,
        "Store GVOKE HypoPen, GVOKE PFS, and GVOKE Kit (these three presentations are referred to as GVOKE in this labeling), "
        "and GVOKE VialDx at 20°C to 25°C (68°F to 77°F); excursions permitted between 15°C and 30°C (59°F and 86°F).",
        [_band("Cool excursion", 15, 20, 90 * 24, "excursions permitted between 15°C and 30°C (59°F and 86°F)", assumption=True),
         _band("Warm excursion", 25, 30, 90 * 24, "excursions permitted between 15°C and 30°C (59°F and 86°F)", assumption=True)],
        freeze_quote="Do not refrigerate or freeze.", cold_ok=False,
        discard=["Do not expose to extreme temperatures.",
                 "Store the GVOKE HypoPen and GVOKE PFS in the original sealed foil pouch until time of use"],
        notes="The label permits excursions without a time limit; LIFELOG assumes 90 days of cumulative excursion. "
              "The label says not to freeze but gives no discard rule; LIFELOG treats freezing as the end of its use."),
    # ---------------------------------------------------------------- eye drops
    "xalatan": _model(
        "Xalatan eye drops, opened (latanoprost)", "eye drop bottle", 2, 8,
        "Store unopened bottle(s) under refrigeration at 2°C to 8°C (36°F to 46°F).",
        [_band(ROOM, 8, 25, 6 * 7 * 24, "Once a bottle is opened for use, it may be stored at room temperature up to 25°C (77°F) for 6 weeks."),
         _band("Shipping allowance", 25, 40, 8 * 24, "During shipment to the patient, the bottle may be maintained at temperatures "
                                                    "up to 40°C (104°F) for a period not exceeding 8 days.")],
        freeze_quote="Not stated on label", freeze=False, discard=["Protect from light."], in_use=42),
}

BRANDS = {"insulin": "LANTUS", "novolog": "NOVOLOG", "tresiba": "TRESIBA", "humalog": "HUMALOG", "toujeo": "TOUJEO",
          "glp1": "MOUNJARO", "zepbound": "ZEPBOUND", "trulicity": "TRULICITY", "victoza": "VICTOZA", "biologic": "ENBREL",
          "humira": "HUMIRA", "dupixent": "DUPIXENT", "stelara": "STELARA", "repatha": "REPATHA", "praluent": "PRALUENT",
          "aimovig": "AIMOVIG", "emgality": "EMGALITY", "prolia": "PROLIA", "forteo": "FORTEO", "epi": "EpiPen",
          "gvoke": "GVOKE", "xalatan": "XALATAN"}
SHORT = {"insulin": "Lantus pen", "novolog": "NovoLog FlexPen", "tresiba": "Tresiba pen", "humalog": "Humalog KwikPen",
         "toujeo": "Toujeo pen", "glp1": "Mounjaro pen", "zepbound": "Zepbound pen", "trulicity": "Trulicity pen",
         "victoza": "Victoza pen", "biologic": "Enbrel SureClick", "humira": "Humira Pen", "dupixent": "Dupixent pen",
         "stelara": "Stelara syringe", "repatha": "Repatha SureClick", "praluent": "Praluent pen", "aimovig": "Aimovig SureClick",
         "emgality": "Emgality pen", "prolia": "Prolia syringe", "forteo": "Forteo pen", "epi": "EpiPen", "gvoke": "Gvoke HypoPen",
         "xalatan": "Xalatan eye drops"}
ROOM_TEMP = {"epi", "gvoke"}   # stored at room temperature, never in the fridge


def label(key: str) -> dict:
    return json.loads((LABELS / f"{key}.json").read_text(encoding="utf-8"))


def _source(key: str) -> dict:
    lab = label(key)
    eff, sid = lab["effective_time"], lab["set_id"]
    return {"label": f"{BRANDS[key]} prescribing information, FDA label version {eff[:4]}-{eff[4:6]}-{eff[6:]}",
            "url": f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={sid}", "set_id": sid, "effective": eff}


SOURCES = {k: _source(k) for k in PRODUCTS}


# ---------------------------------------------------------------- checking quotes against label text
def norm(s: str) -> str:
    s = (s or "").replace("º", "°").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace("�", "-").replace("®", "").replace("™", "")
    return re.sub(r"\s+", " ", s).strip().lower()


def quoted_parts(q: str) -> list[str]:
    """The parts of a quote that must appear word-for-word in the label."""
    if not q:
        return []
    parts = re.findall(r'"([^"]+)"', q)
    if parts:
        return parts
    if q.startswith("Not stated"):
        return []
    return [q]


def model_quotes(m: dict) -> list[tuple[str, str]]:
    out = [("storage", m.get("target_quote", "")), ("freezing", m.get("freeze_quote", "")),
           ("above limit", m.get("above_limit_quote", ""))]
    out += [(f"band: {b['label']}", b.get("quote", "")) for b in m.get("bands", [])]
    out += [("visual check", v) for v in m.get("visual_checks", [])]
    out += [("discard rule", d) for d in m.get("discard_rules", [])]
    return out


def _deg(x: float) -> str:
    return f"{x:g}"


def check_model(m: dict, label_text: str) -> dict:
    """Which quotes appear word-for-word in the label text, and whether each band's numbers (top temperature and
    number of days) appear in its own quote. Used by the tests for seeded medicines and when adding a medicine."""
    text = norm(label_text)
    verified, unverified = [], []
    for kind, q in model_quotes(m):
        for part in quoted_parts(q):
            (verified if norm(part).rstrip(".") in text else unverified).append({"field": kind, "quote": part})
    numbers = []
    for b in m.get("bands", []):
        if b.get("budget_is_assumption"):
            continue
        q = norm(b.get("quote", ""))
        hi = _deg(b["max_c"])
        if not re.search(rf"(?<![\d.]){re.escape(hi)}\s*°\s*c", q):
            numbers.append(f"{b['label']}: {hi}°C is not in its quote")
        days = b["budget_hours"] / 24
        weeks = days / 7
        if not (re.search(rf"(?<![\d.]){days:g}[ -]day", q) or (weeks == int(weeks) and re.search(rf"(?<![\d.]){weeks:g} weeks", q))):
            numbers.append(f"{b['label']}: {days:g} days is not in its quote")
    return {"verified": verified, "unverified": unverified, "number_issues": numbers,
            "all_verified": not unverified and not numbers}


def label_text(key: str) -> str:
    return " ".join(label(key)["fields"].values())

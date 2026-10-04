"""Answers without Gemini. Every AI feature has a deterministic fallback built from the user's own data and the
label's verbatim text, so buttons keep working when Gemini is out of quota, slow or switched off. Each result
carries ai=False so the UI can say how it was produced."""
import re

VERDICT = {"USE": "USE", "USE_SOON": "USE_SOON", "CHECK": "ASK_PHARMACIST", "ASK_PHARMACIST": "ASK_PHARMACIST",
           "DO_NOT_USE": "DO_NOT_USE"}


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


# ------------------------------------------------------------------ "Is it safe to use?"
def advise(d: dict) -> dict:
    s, st, m = d["state"], d["status"], d["item"]["model"]
    lines = [st["why"], f"It has {_pct(s['remaining'])} of its life budget left."]
    real = [b for b in d["burners"] if b["zone"] != "Labeled storage" and b["burn"] >= 0.005]
    if real:
        b = real[0]
        lines.append(f"Most of what it used came from \"{b['zone']}\" on {b['started']:%b %d} "
                     f"({b['minutes'] / 60:.1f} h, peak {b['peak_temp']:.1f}°C).")
    if d["worst_case"] < s["remaining"] - 0.01:
        lines.append(f"Because of missing sensor data it could be as low as {_pct(d['worst_case'])}.")
    actions = [p["text"] for p in d["precautions"] if p["level"] != "info"][:3] or ["Keep storing it the way you are."]
    quotes = [m["target_quote"]] + [b["quote"] for b in m.get("bands", [])[:1]]
    if st["code"] in ("DO_NOT_USE", "ASK_PHARMACIST", "CHECK"):
        actions.append("Show this page to your pharmacist before the next dose.")
    return {"verdict": VERDICT.get(st["code"], "ASK_PHARMACIST"), "headline": st["label"],
            "explanation": " ".join(lines), "actions": actions, "cited_quotes": quotes, "ai": False}


# ------------------------------------------------------------------ refill / replacement letter
def refill_letter(ctx: dict) -> str:
    rows = ctx.get("exposure_timeline") or []
    timeline = "\n".join(f"  - {e['started']:%b %d %Y, %I:%M %p}: {e['zone']}, {e['minutes'] / 60:.1f} h, "
                         f"peak {e['peak_temp']:.1f}°C, {e['burn'] * 100:.1f}% of its stability budget"
                         for e in rows[:8]) or "  - (no exposure outside labeled storage on record)"
    rules = ctx.get("label_rules") or {}
    quotes = "\n".join(f'  "{q}"' for q in [rules.get("target_quote")] + [b["quote"] for b in rules.get("bands") or []] if q)
    outage = ctx.get("outage")
    out_line = f"\nThis happened during a power outage ({outage['name']}, from {outage['started_at']:%b %d %Y}).\n" if outage else ""
    receipt = f"\nVerification: {ctx['receipt_url']} (exposure receipt {ctx['receipt_code']})\n" if ctx.get("receipt_code") else ""
    return f"""[Date]

To: [Pharmacist / Insurance plan name]
Re: Replacement or early refill request for {ctx['product']}
Patient: [Name], [Date of birth], Member ID [ID], Rx # [number]

I am requesting a replacement of my {ctx['product']} because it was exposed to temperatures outside its labeled
storage conditions. A temperature sensor recorded the following (LIFELOG exposure log):

{timeline}
{out_line}
The product label states:
{quotes}

Based on these records, an estimated {ctx['remaining_budget_pct']}% of its stability allowance remains.
{receipt}
I would be grateful if you could approve a replacement or early refill. I can provide the full exposure log
(CSV) on request.

Sincerely,
[Name]
[Phone]
"""


# ------------------------------------------------------------------ outage dispatch plan
def rescue_plan(at_risk: list[dict], outage: dict) -> str:
    if not at_risk:
        return "Nobody's medicine is predicted to run out before power is restored. No dispatch needed."
    lines = [f"## Dispatch plan: {outage.get('name', 'power outage')}", ""]
    for i, p in enumerate(at_risk, 1):
        ref = p["refuges"][0] if p["refuges"] else None
        go = f"{ref['name']} ({ref['miles']} mi)" if ref else "the nearest place with power"
        hl = p["hours_left"] or 0
        lines += [f"{i}. **{p['name']}**: {p['medicine']} lasts about {hl:.1f} h, power back in {p['hours_to_restore']:.0f} h. Go to {go}.",
                  f"   SMS: \"Hi {p['name']}, the power is out and your {p['medicine']} will get too warm in about "
                  f"{max(1, round(hl))} h. Please take it to {go}. Reply if you need a ride.\"", ""]
    lines.append(f"**Coordinator summary:** {len(at_risk)} medicine(s) run out before power returns. "
                 "Contact them in this order; the first ones have the least time.")
    return "\n".join(lines)


# ------------------------------------------------------------------ porch heat brief
def porch_brief(stats: list[dict]) -> str:
    if not stats:
        return "No delivery data yet."
    worst = sorted(stats, key=lambda r: -float(r["avg_hot_hours"]))[:3]
    best = min(stats, key=lambda r: float(r["avg_hot_hours"]))
    lines = ["## Mail-order heat brief", "",
             "Deliveries that sat longest in mailboxes above 30°C (estimated):", ""]
    lines += [f"- **{r['zip']} {r['name']}, {r['carrier']}**: {r['avg_hot_hours']} h on average; "
              f"{r['pct_over_2h']}% of deliveries over 2 h; peak {r['peak_mailbox_c']}°C" for r in worst]
    lines += ["", f"Lowest exposure: {best['zip']} with {best['carrier']} ({best['avg_hot_hours']} h).", "",
              "**Recommendations**", "1. Ship temperature-sensitive orders with insulated packaging and cold packs from June to September.",
              f"2. Ask {worst[0]['carrier']} to deliver to {worst[0]['zip']} before 10am, or offer locker pickup.",
              "3. Text patients when a package is delivered so it is brought inside quickly."]
    return "\n".join(lines)


# ------------------------------------------------------------------ voice / free-text report
SETTINGS = [  # keyword -> (setting, temperature C); the first match wins
    (r"freez|frozen", "freezer", -5.0), (r"car|truck|dashboard|parked", "parked car", 45.0),
    (r"sun|beach|outside|outdoor|porch|mailbox", "outdoors in the sun", 35.0),
    (r"bag|purse|backpack|pocket|on me|body", "carried on body", 31.0),
    (r"counter|kitchen|room|table|desk|left out", "room", 23.0), (r"fridge|refrigerat", "fridge", 5.0),
]


def parse_report(text: str) -> dict:
    """'I left it in the car for about two hours' -> one exposure event, without AI."""
    t = text.lower()
    words = {"half an": 0.5, "a couple of": 2, "an": 1, "a": 1, "one": 1, "two": 2, "three": 3, "four": 4,
             "five": 5, "six": 6, "eight": 8, "ten": 10, "twelve": 12}
    m = re.search(r"(\d+(?:\.\d+)?|half an|a couple of|an|a|one|two|three|four|five|six|eight|ten|twelve)\s*"
                  r"(hours?|hrs?|h\b|minutes?|mins?)", t)
    if m:
        n = float(m.group(1)) if m.group(1)[0].isdigit() else words[m.group(1)]
        minutes = n * 60 if m.group(2).startswith("h") else n
    else:
        minutes = 60.0
    setting, temp = "room", 23.0
    for pat, s_, c in SETTINGS:
        if re.search(pat, t):
            setting, temp = s_, c
            break
    ago = re.search(r"(\d+)\s*(hours?|hrs?)\s*ago", t)
    start_ago = float(ago.group(1)) * 60 if ago else minutes
    return {"transcript": text, "item_hint": "", "ai": False,
            "events": [{"minutes_ago_start": start_ago, "duration_minutes": minutes, "estimated_temp_c": temp,
                        "setting": setting, "summary": f"About {minutes / 60:.1f} h, {setting} (~{temp:.0f}°C)"}]}


# ------------------------------------------------------------------ label text -> stability model
def parse_label_text(name: str, text: str) -> dict:
    """Rule-based reading of an FDA storage section, used when Gemini is unavailable. Every number is taken from
    the same sentence it is quoted from, and every quote is a sentence copied from the label, so the add-medicine
    check (medicines.check_model) can confirm both. Anything it can't find is left out and flagged."""
    t = re.sub(r"\s+", " ", text.replace("º", "°").replace("\ufffd", "-"))
    sentences = [x.strip() for x in re.split(r"(?<=[.;])\s+(?=[A-Z*•])", t) if x.strip()]
    C = r"(-?\d+(?:\.\d+)?)\s*°\s*C"

    def celsius(sent):
        vals = [float(v) for v in re.findall(C, sent)]
        vals += [float(v) for v in re.findall(r"(-?\d+(?:\.\d+)?)\s*°\s*(?:to|and|-)\s*\d+(?:\.\d+)?\s*°\s*C", sent)]
        return vals

    def days_in(sent):
        m = re.search(r"(\d+)\s*(?:-|\s)?days?\b", sent, re.I)
        if m:
            return float(m.group(1)) * 24
        w = re.search(r"(\d+)\s*weeks?\b", sent, re.I)
        return float(w.group(1)) * 168 if w else None

    RANGE = r"(-?\d+(?:\.\d+)?)\s*°?\s*C?\s*(?:to|and|-)\s*(-?\d+(?:\.\d+)?)\s*°\s*C"
    target = (next((s_ for s_ in sentences if re.search(r"refrigerat", s_, re.I) and re.search(RANGE, s_)), None)
              or next((s_ for s_ in sentences if re.search(r"\bstore", s_, re.I) and re.search(RANGE, s_)), None))
    if target:   # the first Celsius range after "refrigerat" / "store" in that sentence
        at = (re.search(r"refrigerat", target, re.I) or re.search(r"\bstore", target, re.I)).start()
        ranges = list(re.finditer(RANGE, target))
        r_ = next((x for x in ranges if x.start() >= at), ranges[0])
        tmin, tmax = sorted((float(r_.group(1)), float(r_.group(2))))
    else:
        tmin, tmax = 2.0, 8.0
    bands = []
    for s_ in sentences:
        m_ = re.search(r"room temperature|unrefrigerated|out of refrigeration|removed from the refrigerator|can be kept|can be stored for|may be kept", s_, re.I)
        if not m_:
            continue
        near = s_[m_.start():m_.start() + 220]   # tables flatten into one long "sentence": read next to the phrase
        hi = max(celsius(near), default=None)
        hours = days_in(near) or days_in(s_)
        if hi is not None and hi > tmax and hours:
            bands.append({"label": "Room-temp allowance", "min_c": tmax, "max_c": hi, "budget_hours": hours, "quote": s_})
            break
    pick = lambda pat: [s_ for s_ in sentences if re.search(pat, s_, re.I) and s_ != target][:4]
    freeze_q = next(iter(pick(r"\bfreez|\bfrozen")), None)
    in_use = re.search(r"(\d+)\s*days after (?:first use|opening|first opening|initial use)", t, re.I)
    return {
        "product_name": name, "form": "unknown", "target_min_c": tmin, "target_max_c": tmax,
        "target_quote": target or "Not stated on label (LIFELOG could not find the storage temperature: enter it from the box)",
        "freeze_discard": bool(re.search(r"do not freeze|not be frozen|if (it has been )?frozen|never be frozen|or freeze\b", t, re.I)),
        "freeze_c": 0.0, "freeze_quote": freeze_q or "Not stated on label",
        "cold_ok": not re.search(r"do not refrigerate", t, re.I), "bands": bands,
        "above_limit_budget_hours": 8.0, "above_limit_is_assumption": True,
        "above_limit_quote": "Not stated on label (LIFELOG conservative default: 8 h above the highest labeled limit, doubling per 10°C)",
        "in_use_days": float(in_use.group(1)) if in_use else 0.0,
        "visual_checks": pick(r"clear and colorless|discolou?red|cloudy|particles"),
        "discard_rules": pick(r"\bdiscard|throw away|do not shake|protect .{0,40}from (?:direct )?(?:light|heat)"),
        "notes": "Read by LIFELOG's rule-based parser because Gemini was unavailable. Check every number against the label text."
                 + ("" if target else " The storage temperature was not found; 2–8°C is only a placeholder."),
        "ai": False,
    }
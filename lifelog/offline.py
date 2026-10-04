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
    """Rule-based reading of an FDA storage section. Catches the common phrasings ('refrigerator at 2°C to 8°C',
    'not to exceed 30°C ... for 14 days', 'Do not freeze'); anything it can't read is flagged for the person."""
    t = text.replace("º", "°")
    sentences = re.split(r"(?<=[.;])\s+", t)

    def find(pat):
        for s in sentences:
            if re.search(pat, s, re.I):
                return s.strip()
        return None

    rng = re.search(r"(-?\d+(?:\.\d+)?)\s*°\s*C\s*(?:to|and|-|–)\s*(-?\d+(?:\.\d+)?)\s*°\s*C", t)
    tmin, tmax = (float(rng.group(1)), float(rng.group(2))) if rng else (2.0, 8.0)
    target_q = find(r"refrigerat|store") or "Not found in the label text"
    bands = []
    room = re.search(r"(?:not to exceed|up to|below|under|at temperatures? (?:not exceeding|up to))\s*"
                     r"(?:\d+\s*°\s*F\s*[(\[]\s*)?(\d+(?:\.\d+)?)\s*°\s*C", t, re.I)
    days = re.search(r"(\d+)\s*days|(\d+)\s*weeks", t, re.I)
    if room and float(room.group(1)) > tmax:
        hours = (int(days.group(1)) * 24) if days and days.group(1) else (int(days.group(2)) * 168 if days else 0)
        if hours:
            bands.append({"label": "Room-temp allowance", "min_c": tmax, "max_c": float(room.group(1)),
                          "budget_hours": float(hours), "quote": find(r"room temperature|unrefrigerated|not to exceed|up to") or ""})
    freeze = bool(re.search(r"do not freeze|not be frozen|if (it has been )?frozen", t, re.I))
    in_use = re.search(r"(\d+)\s*days after (?:first use|opening|first opening)", t, re.I)
    return {
        "product_name": name, "form": "unknown", "target_min_c": tmin, "target_max_c": tmax, "target_quote": target_q,
        "freeze_discard": freeze, "freeze_c": 0.0, "freeze_quote": find(r"freez") or "Not stated on label",
        "cold_ok": True, "bands": bands, "above_limit_budget_hours": 8.0, "above_limit_is_assumption": True,
        "above_limit_quote": "Not stated on label (LIFELOG conservative default: 8 h above the highest labeled limit, doubling per 10°C)",
        "in_use_days": float(in_use.group(1)) if in_use else 0.0, "visual_checks": [], "discard_rules": [],
        "notes": "Read by LIFELOG's rule-based parser because Gemini was unavailable. Check every number against the label text.",
        "ai": False,
    }

"""Life-budget math in Python. Must stay in lockstep with sql/functions.sql (burn_rate, zone_of)."""
import math
from datetime import datetime, timedelta

BUCKET_H = 5 / 60


def _bounds(m: dict) -> tuple[float, float]:
    lo, hi = m["target_min_c"], m["target_max_c"]
    for b in m.get("bands", []):
        lo, hi = min(lo, b["min_c"]), max(hi, b["max_c"])
    return lo, hi


def burn_rate(t: float, m: dict) -> float:
    """Fraction of the life budget consumed per hour at temperature t."""
    if m.get("freeze_discard") and t <= m["freeze_c"]:
        return math.inf
    if m["target_min_c"] <= t <= m["target_max_c"]:
        return 0.0
    for b in m.get("bands", []):
        if b["min_c"] <= t <= b["max_c"]:
            return 1.0 / b["budget_hours"]
    lo, hi = _bounds(m)
    if t > hi:
        return 2.0 ** ((t - hi) / 10.0) / m["above_limit_budget_hours"]
    if m.get("cold_ok", True):
        return 0.0
    return 1.0 / m["above_limit_budget_hours"]


def zone_of(avg_t: float, min_t: float, m: dict) -> str:
    if m.get("freeze_discard") and min_t <= m["freeze_c"]:
        return "Frozen"
    if m["target_min_c"] <= avg_t <= m["target_max_c"]:
        return "Labeled storage"
    for b in m.get("bands", []):
        if b["min_c"] <= avg_t <= b["max_c"]:
            return b["label"]
    lo, hi = _bounds(m)
    if avg_t > hi:
        return "Above labeled limit"
    return "Labeled storage" if m.get("cold_ok", True) else "Too cold"


def step_burn(t: float, m: dict, hours: float, min_t: float | None = None) -> float:
    if m.get("freeze_discard") and (t if min_t is None else min_t) <= m["freeze_c"]:
        return 1.0
    return min(1.0, burn_rate(t, m) * hours)


def hours_left(remaining: float, t: float, m: dict) -> float | None:
    """Hours until the budget is exhausted at constant temperature t. None = indefinitely."""
    if remaining <= 0:
        return 0.0
    r = burn_rate(t, m)
    if r == 0:
        return None
    return 0.0 if math.isinf(r) else remaining / r


FRIDGE_TAU_H = 6.0  # closed fridge without power: e-folding time toward room temperature (assumption)


def outage_hours_left(remaining: float, current_t: float | None, indoor_t: float, m: dict,
                      horizon_h: float = 72) -> float | None:
    """Hours until the budget is exhausted during an outage. Items currently colder than the room
    (in a fridge) warm toward indoor temperature by Newton cooling; others sit at indoor temp."""
    if remaining <= 0:
        return 0.0
    cur = indoor_t if current_t is None else current_t
    tau = FRIDGE_TAU_H if cur < indoor_t - 5 else 1e-9
    t, step = 0.0, 5 / 60
    while t < horizon_h:
        temp = indoor_t - (indoor_t - cur) * math.exp(-t / tau)
        remaining -= step_burn(temp, m, step)
        t += step
        if remaining <= 0:
            return t
    return None


def project(remaining: float, temps: list[float], m: dict, step_h: float) -> list[float]:
    """Remaining budget after each step of a temperature trajectory."""
    out = []
    for t in temps:
        remaining = max(0.0, remaining - step_burn(t, m, step_h))
        out.append(remaining)
    return out


def whatif(remaining: float, current_t: float, m: dict, horizon_h: float = 12) -> list[dict]:
    """Counterfactual interventions: what does each action leave after `horizon_h` hours?"""
    target_mid = (m["target_min_c"] + m["target_max_c"]) / 2
    options = [
        ("Leave it where it is", current_t),
        ("Put it back in its usual storage", target_mid),
        ("Put it in a cooler with an ice pack", 10.0),
        ("Bring it indoors with air conditioning", 22.0),
        ("Carry it on your body", 31.0),
    ]
    seen, out = set(), []
    for name, t in options:
        if (name != options[0][0]) and round(t, 1) in seen:
            continue
        seen.add(round(t, 1))
        after = max(0.0, remaining - min(1.0, burn_rate(t, m) * horizon_h))
        out.append({
            "option": name, "temp_c": round(t, 1),
            "remaining_after": after,
            "hours_left": hours_left(remaining, t, m),
        })
    base = out[0]["remaining_after"]
    for o in out:
        o["preserved_vs_now"] = o["remaining_after"] - base
    return sorted(out, key=lambda o: -o["remaining_after"])


# ------------------------------------------------------------------ exposure fingerprints
BIN_EDGES = [0, 2, 8, 15, 25, 30, 35, 40, 50]  # 10 bins: <=0, (0,2], ... , >50


def fingerprint(temps: list[float]) -> list[float]:
    """Product-independent, time-shift-invariant 12-dim signature of an exposure history:
    fraction of time in each temperature bin + volatility + peak."""
    if not temps:
        return [0.0] * 12
    bins = [0] * 10
    for t in temps:
        i = 0
        while i < len(BIN_EDGES) and t > BIN_EDGES[i]:
            i += 1
        bins[i] += 1
    n = len(temps)
    frac = [b / n for b in bins]
    diffs = [abs(a - b) for a, b in zip(temps, temps[1:])] or [0.0]
    volatility = min(1.0, (sum(diffs) / len(diffs)) / 5.0)
    peak = min(1.0, max(temps) / 60.0) if max(temps) > 0 else 0.0
    return frac + [volatility, peak]


def vector_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def daterange(start: datetime, end: datetime, step: timedelta):
    t = start
    while t < end:
        yield t
        t += step


# ------------------------------------------------------------------ accuracy helpers
def exact_budget_used(readings: list[tuple], m: dict, started_at=None) -> float:
    """Independent Python recomputation from raw (ts, temp) readings: mean per-reading rate per
    5-minute bucket, same rule as the SQL continuous aggregate. Used to cross-check SQL."""
    buckets: dict = {}
    for ts, t in readings:
        if started_at and ts < started_at:
            continue
        key = ts.replace(minute=ts.minute - ts.minute % 5, second=0, microsecond=0)
        buckets.setdefault(key, []).append(t)
    used = 0.0
    for temps in buckets.values():
        if m.get("freeze_discard") and min(temps) <= m["freeze_c"]:
            used += 1.0
            continue
        rate = sum(min(burn_rate(t, m), 1e6) for t in temps) / len(temps)
        used += min(1.0, rate * BUCKET_H)
    return min(1.0, used)


GAP_ASSUMED_ROOM_C = 25.0


def gap_worst_case(remaining: float, gaps: list[dict], m: dict) -> float:
    """Pessimistic budget if every data gap was spent at the hotter of its last known temperature
    and a warm room (25C). The true value lies between this and `remaining`."""
    low = remaining
    for g in gaps:
        t = max(g.get("last_temp") or GAP_ASSUMED_ROOM_C, GAP_ASSUMED_ROOM_C)
        low -= step_burn(t, m, g["hours"])
    return max(0.0, low)


# ------------------------------------------------------------------ sensor validation
VALID_RANGE_C = (-40.0, 85.0)
SPIKE_C_PER_MIN = 15.0


def validate_readings(readings: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (accepted, rejected, flagged). Rejects impossible or non-numeric values and duplicate
    timestamps; flags physically implausible jumps (kept, but marked for review)."""
    accepted, rejected, flagged, seen = [], [], [], set()
    prev = None
    for r in sorted(readings, key=lambda r: r["ts"]):
        t = r.get("temp_c")
        if not isinstance(t, (int, float)) or t != t or not (VALID_RANGE_C[0] <= t <= VALID_RANGE_C[1]):
            rejected.append({**r, "reason": "out of physical range"})
            continue
        if r["ts"] in seen:
            rejected.append({**r, "reason": "duplicate timestamp"})
            continue
        seen.add(r["ts"])
        if prev is not None:
            minutes = max((r["ts"] - prev["ts"]).total_seconds() / 60, 1 / 60)
            if abs(t - prev["temp_c"]) / max(minutes, 1.0) > SPIKE_C_PER_MIN:
                flagged.append({**r, "reason": f"jump of {t - prev['temp_c']:+.1f}C in {minutes:.1f} min"})
        accepted.append(r)
        prev = r
    return accepted, rejected, flagged


# ------------------------------------------------------------------ plain-language status
STATUS_TEXT = {
    "DO_NOT_USE": "Do not use",
    "CHECK": "Check on it",
    "ASK_PHARMACIST": "Check with your pharmacist",
    "USE_SOON": "OK to use, act soon",
    "USE": "Safe to use",
}


def dates_info(opened_at, expires_on, in_use_days, now) -> dict:
    """Calendar limits that apply regardless of temperature: the printed expiry date and the label's
    in-use period after opening ("discard 28 days after opening")."""
    out = {"opened_at": opened_at, "expires_on": expires_on, "in_use_days": in_use_days or 0,
           "in_use_until": None, "use_by": None, "use_by_reason": None, "expired": False, "in_use_over": False}
    limits = []
    if expires_on is not None:
        exp = datetime.combine(expires_on, datetime.min.time(), tzinfo=now.tzinfo) + timedelta(days=1)
        out["expired"] = exp <= now
        limits.append((exp, "expiration date", expires_on.isoformat()))
    if opened_at is not None and (in_use_days or 0) > 0:
        until = opened_at + timedelta(days=round(in_use_days))
        out["in_use_until"] = until
        out["in_use_over"] = until <= now
        limits.append((until, f"{round(in_use_days)} days after opening", until.date().isoformat()))
    if limits:
        out["use_by"], out["use_by_reason"], out["use_by_date"] = min(limits, key=lambda x: x[0])
        out["days_left"] = (out["use_by"] - now).total_seconds() / 86400
    return out


def status_of(state: dict, worst_case: float, zone: str, dates: dict | None = None) -> dict:
    """Rule-based traffic light that needs no AI: what a person should do, in one line."""
    if dates and dates.get("expired"):
        return {"code": "DO_NOT_USE", "label": STATUS_TEXT["DO_NOT_USE"], "why": "It is past its printed expiration date."}
    if dates and dates.get("in_use_over"):
        return {"code": "DO_NOT_USE", "label": STATUS_TEXT["DO_NOT_USE"],
                "why": f"Its in-use period is over: the label allows {round(dates['in_use_days'])} days after opening."}
    r = state["remaining"]
    silent_h = (state.get("stale_minutes") or 0) / 60
    if zone == "Frozen" or r <= 0:
        code, why = "DO_NOT_USE", "It froze or used its whole life budget." if r <= 0 else "It froze. The label says not to use frozen medicine."
    elif r < 0.2 or (worst_case < 0.2 and r - worst_case > 0.05):
        code, why = "ASK_PHARMACIST", "Most of its life budget is used, or a data gap makes it uncertain."
    elif silent_h >= 1:
        code, why = "CHECK", f"No readings for {silent_h:.0f} h, so what happened since is unknown. Check the sensor and where the medicine is."
    elif r < 0.5 or zone == "Above labeled limit":
        code, why = "USE_SOON", "It is using budget faster than normal." if zone == "Above labeled limit" else "More than half its life budget is used."
    else:
        code, why = "USE", "Stored within its label's limits."
    return {"code": code, "label": STATUS_TEXT[code], "why": why}

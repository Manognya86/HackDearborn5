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
        ("Back into labeled storage", target_mid),
        ("Insulated cooler with ice pack", 10.0),
        ("Indoors with air conditioning", 22.0),
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

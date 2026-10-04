"""Per-medicine temperature forecasting: a small model trained for each item on its own history in Tiger Data.

Candidates (fit by least squares, plain Python, no numpy):
  persistence          tomorrow looks like right now                        (baseline)
  seasonal             the item's own hour-of-day profile                   (a fridge cycle, a backpack's day)
  seasonal_ar          profile + AR(1): today's deviation fades at rate phi
  seasonal_ar_weather  profile + AR(1) + beta x outdoor-temperature anomaly (things you carry follow the weather)

Each candidate is backtested on the item's own last 48 hours (rolling origins every 12 h, 24-hour horizon);
the most accurate one is kept. The chosen model forecasts the next 24 hours; Monte Carlo paths through the
life-budget engine turn that into P(above the label's limit), P(freezing) and the expected budget left.
Trained models and forecasts are stored in Tiger Data (table ml_models)."""
import json
import math
import random
from datetime import datetime, timedelta, timezone

from . import db, engine, weather

HORIZON_H = 24
HISTORY_DAYS = 14
DETROIT = timezone(timedelta(hours=-4))   # demo locale (EDT); hour-of-day seasonality uses local time
MODELS = ["persistence", "seasonal", "seasonal_ar", "seasonal_ar_weather"]
LABELS = {
    "persistence": "Same as now (baseline)",
    "seasonal": "Daily pattern",
    "seasonal_ar": "Daily pattern + short-term memory",
    "seasonal_ar_weather": "Daily pattern + memory + outdoor weather",
}
_weather_cache: dict = {}


# ------------------------------------------------------------------ data
def hourly_series(item_id: int) -> list[tuple[datetime, float]]:
    """Hourly mean temperature from the 5-minute continuous aggregate (no raw scan)."""
    rows = db.query("""SELECT time_bucket('1 hour', bucket) AS h, avg(avg_temp) AS t FROM readings_5m
                       WHERE item_id = %s AND bucket >= now() - %s * INTERVAL '1 day'
                       GROUP BY 1 ORDER BY 1""", (item_id, HISTORY_DAYS))
    return [(r["h"], r["t"]) for r in rows if r["t"] is not None]


def outdoor(lat: float, lon: float) -> dict[datetime, float]:
    """Past 14 days + next 2 days of hourly outdoor temperature (Open-Meteo), cached for an hour."""
    import time
    key = (round(lat, 2), round(lon, 2))
    hit = _weather_cache.get(key)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    try:
        import httpx
        r = httpx.get(weather.FORECAST, params={"latitude": lat, "longitude": lon, "hourly": "temperature_2m",
                                                "timezone": "UTC", "past_days": HISTORY_DAYS, "forecast_days": 2}, timeout=15)
        r.raise_for_status()
        h = r.json()["hourly"]
        data = {datetime.fromisoformat(t).replace(tzinfo=timezone.utc): v
                for t, v in zip(h["time"], h["temperature_2m"]) if v is not None}
    except Exception:
        data = {}
    _weather_cache[key] = (time.time(), data)
    return data


def _hod(t: datetime) -> int:
    return t.astimezone(DETROIT).hour


# ------------------------------------------------------------------ fitting
def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _robust_sigma(xs) -> float:
    """1.4826 x median absolute deviation: the spread of normal hours, not inflated by a rare hot-car spike."""
    if len(xs) < 3:
        return 0.5
    s = sorted(xs)
    med = s[len(s) // 2]
    dev = sorted(abs(x - med) for x in xs)
    return max(0.05, 1.4826 * dev[len(dev) // 2])


def _ols_slope(x, y):
    mx, my = _mean(x), _mean(y)
    sxx = sum((a - mx) ** 2 for a in x)
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / sxx if sxx > 1e-9 else 0.0


def fit(series, kind: str, wx: dict) -> dict:
    """Train one candidate on a series of (hour, temp)."""
    temps = [t for _, t in series]
    prof = {}
    for h, t in series:
        prof.setdefault(_hod(h), []).append(t)
    overall = _mean(temps)
    profile = {k: _mean(v) for k, v in prof.items()}
    p = {"kind": kind, "profile": {str(k): profile.get(k, overall) for k in range(24)}, "phi": 0.0, "beta": 0.0,
         "w_profile": {}, "sigma": 0.5}
    mu = lambda h: p["profile"][str(_hod(h))]
    resid = [t - mu(h) for h, t in series]
    if kind == "seasonal_ar_weather" and wx:
        wprof = {}
        for h, _ in series:
            if h in wx:
                wprof.setdefault(_hod(h), []).append(wx[h])
        p["w_profile"] = {str(k): _mean(v) for k, v in wprof.items()}
        pairs = [(wx[h] - p["w_profile"].get(str(_hod(h)), wx[h]), r) for (h, _), r in zip(series, resid) if h in wx]
        if len(pairs) > 24:
            p["beta"] = max(-1.0, min(1.0, _ols_slope([a for a, _ in pairs], [b for _, b in pairs])))
            resid = [r - p["beta"] * (wx[h] - p["w_profile"].get(str(_hod(h)), wx[h])) if h in wx else r
                     for (h, _), r in zip(series, resid)]
    if kind in ("seasonal_ar", "seasonal_ar_weather") and len(resid) > 3:
        p["phi"] = max(0.0, min(0.98, _ols_slope(resid[:-1], resid[1:])))
        innov = [b - p["phi"] * a for a, b in zip(resid[:-1], resid[1:])]
    elif kind == "persistence":
        innov = [b - a for a, b in zip(temps[:-1], temps[1:])]     # hour-to-hour change
    else:
        innov = resid
    p["sigma"] = _robust_sigma(innov)
    return p


def predict(p: dict, last_h: datetime, last_t: float, steps: int, wx: dict) -> list[float]:
    """Point forecast for the next `steps` hours after (last_h, last_t)."""
    if p["kind"] == "persistence":
        return [last_t] * steps
    mu = lambda h: p["profile"][str(_hod(h))]

    def w_anom(h):
        if not p["beta"] or h not in wx:
            return 0.0
        return wx[h] - p["w_profile"].get(str(_hod(h)), wx[h])
    r0 = last_t - mu(last_h) - p["beta"] * w_anom(last_h)
    out = []
    for k in range(1, steps + 1):
        h = last_h + timedelta(hours=k)
        r = (p["phi"] ** k) * r0 if p["kind"] != "seasonal" else 0.0
        out.append(mu(h) + p["beta"] * w_anom(h) + r)
    return out


# ------------------------------------------------------------------ model selection by backtest
def backtest(series, wx: dict) -> dict:
    """Rolling-origin backtest on the last 48 hours: train on everything before each origin, forecast 24 h."""
    if len(series) < 72:
        return {}
    errors = {k: [] for k in MODELS}
    n = len(series)
    for origin in (n - 48, n - 36, n - 24):
        train, test = series[:origin], series[origin:origin + HORIZON_H]
        if len(train) < 48 or not test:
            continue
        for kind in MODELS:
            if kind == "seasonal_ar_weather" and not wx:
                continue
            p = fit(train, kind, wx)
            pred = predict(p, train[-1][0], train[-1][1], len(test), wx)
            errors[kind] += [abs(a - t) for a, (_, t) in zip(pred, test)]
    return {k: _mean(v) for k, v in errors.items() if v}


# ------------------------------------------------------------------ probabilistic forecast
def simulate(p: dict, point: list[float], remaining: float, m: dict, paths: int = 300, seed: int = 7) -> dict:
    """Monte Carlo: AR(1)-correlated noise around the point forecast, each path run through the life budget."""
    rng = random.Random(seed)
    hi = max([m["target_max_c"]] + [b["max_c"] for b in m.get("bands", [])])
    exceed = freeze = above = 0
    finals, bands_lo, bands_hi = [], [], []
    by_step = [[] for _ in point]
    for _ in range(paths):
        e, rem, crossed, froze, peak = 0.0, remaining, False, False, -1e9
        for k, mu in enumerate(point):
            e = p["phi"] * e + rng.gauss(0, p["sigma"])
            t = mu + e
            by_step[k].append(t)
            crossed |= t > m["target_max_c"]
            peak = max(peak, t)
            froze |= bool(m.get("freeze_discard")) and t <= m["freeze_c"]
            rem = max(0.0, rem - engine.step_burn(t, m, 1.0))
        exceed += crossed
        freeze += froze
        above += peak > hi
        finals.append(rem)
    for s in by_step:
        s.sort()
        bands_lo.append(s[int(0.1 * len(s))])
        bands_hi.append(s[int(0.9 * len(s)) - 1])
    finals.sort()
    return {"p_above_storage": exceed / paths, "p_freeze": freeze / paths,
            "p_above_label_limit": above / paths,
            "budget_median": finals[len(finals) // 2], "budget_p10": finals[int(0.1 * len(finals))],
            "lo": bands_lo, "hi": bands_hi}


# ------------------------------------------------------------------ train + store
def train(item_id: int) -> dict:
    item = db.one("""SELECT i.id, i.nickname, p.model, u.lat, u.lon FROM items i JOIN products p ON p.id = i.product_id
                     JOIN users u ON u.id = i.user_id WHERE i.id = %s""", (item_id,))
    if not item:
        return {"error": "unknown item"}
    series = hourly_series(item_id)
    if len(series) < 72:
        return {"item_id": item_id, "status": "not_enough_data", "hours": len(series),
                "message": "The model needs at least 3 days of readings for this medicine."}
    wx = outdoor(item["lat"], item["lon"])
    scores = backtest(series, wx)
    best = min(scores, key=lambda k: (round(scores[k], 3), MODELS.index(k)))   # ties go to the simpler model
    p = fit(series, best, wx)
    last_h, last_t = series[-1]
    point = predict(p, last_h, last_t, HORIZON_H, wx)
    from . import services
    s = services.summary(item_id, item["model"])
    mc = simulate(p, point, s["remaining"], item["model"])
    base = scores.get("persistence")
    out = {
        "item_id": item_id, "status": "ok", "model": best, "model_label": LABELS[best],
        "params": {"phi": round(p["phi"], 3), "beta": round(p["beta"], 3), "sigma": round(p["sigma"], 3),
                   "profile": {k: round(v, 2) for k, v in p["profile"].items()}},
        "backtest_mae": {k: round(v, 3) for k, v in scores.items()},
        "skill_vs_baseline": round(1 - scores[best] / base, 3) if base else None,
        "trained_on_hours": len(series), "uses_weather": bool(p["beta"]),
        "forecast": [{"t": (last_h + timedelta(hours=k + 1)).isoformat(), "temp": round(v, 2),
                      "lo": round(mc["lo"][k], 2), "hi": round(mc["hi"][k], 2)} for k, v in enumerate(point)],
        "risk": {k: round(v, 3) for k, v in mc.items() if k not in ("lo", "hi")},
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }
    db.execute("""INSERT INTO ml_models (item_id, model, params, metrics, forecast, risk, trained_at)
                  VALUES (%s, %s, %s, %s, %s, %s, now())
                  ON CONFLICT (item_id) DO UPDATE SET model = EXCLUDED.model, params = EXCLUDED.params,
                    metrics = EXCLUDED.metrics, forecast = EXCLUDED.forecast, risk = EXCLUDED.risk, trained_at = now()""",
               (item_id, best, json.dumps(out["params"]),
                json.dumps({"backtest_mae": out["backtest_mae"], "skill_vs_baseline": out["skill_vs_baseline"],
                            "trained_on_hours": out["trained_on_hours"], "uses_weather": out["uses_weather"]}),
                json.dumps(out["forecast"]), json.dumps(out["risk"])))
    return out


def get(item_id: int, max_age_min: int = 60) -> dict:
    """Stored model if fresh enough, otherwise retrain."""
    row = db.one("SELECT *, now() - trained_at AS age FROM ml_models WHERE item_id = %s", (item_id,))
    if row and row["age"] < timedelta(minutes=max_age_min):
        m = row["metrics"]
        return {"item_id": item_id, "status": "ok", "model": row["model"], "model_label": LABELS.get(row["model"], row["model"]),
                "params": row["params"], "backtest_mae": m["backtest_mae"], "skill_vs_baseline": m["skill_vs_baseline"],
                "trained_on_hours": m["trained_on_hours"], "uses_weather": m["uses_weather"], "forecast": row["forecast"],
                "risk": row["risk"], "trained_at": row["trained_at"].isoformat(), "cached": True}
    return train(item_id)


def train_all() -> list[dict]:
    return [{"item_id": r["id"], **{k: v for k, v in train(r["id"]).items() if k in ("status", "model", "skill_vs_baseline")}}
            for r in db.query("SELECT id FROM items ORDER BY id")]

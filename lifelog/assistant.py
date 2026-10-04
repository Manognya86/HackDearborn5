"""'Ask LIFELOG': Gemini answers questions by calling functions that query Tiger Data."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from google.genai import types

from . import config, db, engine, gem, services


def _clean(x):
    return json.loads(json.dumps(x, default=str))


def _me() -> int:
    return db.me()


SEVERITY = {"DO_NOT_USE": 0, "ASK_PHARMACIST": 1, "CHECK": 2, "USE_SOON": 3, "USE": 4}


def _pct(x):
    return None if x is None else round(x * 100, 1)


def _r(x, n=1):
    return None if x is None else round(x, n)


def _trend(f: dict | None) -> dict | None:
    if not f:
        return None
    return {"trend": f.get("trend"), "slope_c_per_hour": _r(f.get("slope_c_per_h"), 2),
            "minutes_until_it_leaves_labeled_range": f.get("minutes_to_limit"),
            "minutes_until_freezing": f.get("minutes_to_freeze"),
            "hours_left_if_trend_continues": _r(f.get("hours_left")),
            "forecast_model": f.get("model")}


def _dates(d: dict) -> dict:
    return {"use_by_date": d.get("use_by_date"), "use_by_reason": d.get("use_by_reason"),
            "days_until_use_by": _r(d.get("days_left")),
            "expired": d.get("expired"), "in_use_period_over": d.get("in_use_over")}


def list_my_medicines() -> list[dict]:
    """List the user's medicines, most urgent first. For each: id, nickname, product, overall status
    (status_label and status_reason combine life budget, dates and sensor health), budget_left_percent,
    worst_case_budget_left_percent (if a sensor gap hid an exposure), current temperature (°C), storage zone,
    hours left at the current temperature, minutes since the sensor last reported, live temperature trend
    and forecast, and calendar limits (expiry / in-use period)."""
    rows = []
    for i in services.list_items(_me()):
        rows.append({
            "id": i["id"], "nickname": i["nickname"], "product": i["product_name"],
            "status_label": i["status"]["label"], "status_reason": i["status"]["why"],
            "budget_left_percent": _pct(i["remaining"]), "worst_case_budget_left_percent": _pct(i["worst_case"]),
            "current_temp_c": _r(i["current_temp"]), "zone": i["zone"],
            "hours_left_at_current_temp": _r(i["hours_left"]),
            "sensor_quiet_minutes": round(i["stale_minutes"] or 0), "trend": _trend(i.get("forecast")),
            "dates": _dates(i["dates"]), "_rank": (SEVERITY.get(i["status"]["code"], 9), i["remaining"])})
    rows.sort(key=lambda r: r.pop("_rank"))
    return _clean(rows)


def get_medicine_details(item_id: int) -> dict:
    """Full status of one medicine: overall status, life budget (percent), what consumed it (episodes, largest
    first), data gaps, live trend forecast, budget used in the last hour, what-if options for the next 12 hours,
    exposure statistics (mean kinetic temperature), calendar limits, open alerts, similar histories and the
    label's own storage text."""
    d = services.item_detail(item_id)
    if not d:
        return {"error": "unknown item"}
    m = d["item"]["model"]
    return _clean({
        "nickname": d["item"]["nickname"], "product": d["item"]["product_name"],
        "status_label": d["status"]["label"], "status_reason": d["status"]["why"],
        "labeled_storage_c": [m["target_min_c"], m["target_max_c"]],
        "budget_left_percent": _pct(d["state"]["remaining"]), "worst_case_budget_left_percent": _pct(d["worst_case"]),
        "current_temp_c": _r(d["state"]["current_temp"]), "zone": d["state"]["zone"],
        "hours_left_at_current_temp": _r(d["state"]["hours_left"]),
        "sensor_quiet_minutes": round(d["state"]["stale_minutes"] or 0),
        "budget_used_last_hour_percent": _pct(d["burn_last_hour"]), "trend": _trend(d["forecast"]),
        "what_used_the_budget": [{"zone": b["zone"], "started": b["started"], "minutes": b["minutes"],
                                  "peak_temp_c": _r(b["peak_temp"]), "budget_used_percent": _pct(b["burn"])}
                                 for b in d["burners"]],
        "data_gaps": d["gaps"],
        "what_if_next_12h": [{"option": w["option"], "temp_c": w["temp_c"],
                              "budget_left_after_percent": _pct(w["remaining_after"])} for w in d["whatif"]],
        "stats": d["stats"], "dates": _dates(d["dates"]), "open_alerts": [a["message"] for a in d["alerts"]],
        "similar_patterns": d["patterns"],
        "label": {k: m.get(k) for k in ("target_quote", "bands", "freeze_quote", "above_limit_quote",
                                         "above_limit_is_assumption", "in_use_days", "visual_checks", "discard_rules")}})


def get_exposure_calendar(item_id: int, days: int = 30) -> list[dict]:
    """Day-by-day exposure for the last `days` days (from Tiger's daily rollup): average, min and max °C and the
    percent of the life budget used that day."""
    return _clean([{"day": r["day"], "avg_temp_c": _r(r["avg_temp"]), "min_temp_c": _r(r["min_temp"]),
                    "max_temp_c": _r(r["max_temp"]), "budget_used_percent": _pct(r["burn"])}
                   for r in services.history(item_id, max(1, min(int(days), 90)))])


def simulate_exposure(item_id: int, temp_c: float, hours: float) -> dict:
    """Predict the life budget left if the medicine is kept at temp_c Celsius for the given hours."""
    d = services.item_detail(item_id)
    if not d:
        return {"error": "unknown item"}
    m, rem = d["item"]["model"], d["state"]["remaining"]
    after = engine.project(rem, [temp_c] * max(1, int(hours * 12)), m, 1 / 12)[-1]
    return {"remaining_now": rem, "remaining_after": after, "zone": engine.zone_of(temp_c, temp_c, m),
            "hours_left_at_that_temp": engine.hours_left(rem, temp_c, m)}


def get_outage_situation() -> dict:
    """Active power outages affecting the user: their medicines' hours of life left versus hours until
    power is restored, and the nearest refuges that still have power."""
    with db.system():
        r = services.rescue()
    me = [p for p in r["people"] if p["user_id"] == db.me()]
    return _clean({"active_outages": [f["properties"] for f in r["outages"]["features"]], "my_medicines": me,
                   "neighbors_at_risk": sum(1 for p in r["people"] if p["at_risk"])})


def get_alerts() -> list[dict]:
    """Open alerts for the user's medicines (frozen, above limit, low budget, sensor silent, outage, expired,
    in-use period over or ending, warming trend, freeze risk)."""
    return _clean([{k: a[k] for k in ("nickname", "kind", "severity", "message", "created_at")}
                   for a in services.alerts(mine_only=True)])


def get_doses(item_id: int) -> list[dict]:
    """Doses the user logged for one medicine, newest first: when it was taken, the medicine's status and life
    budget left at that moment, and whether today's data (e.g. a late sensor upload) changes that answer.
    Use this for questions like 'was the dose I took last Tuesday still good?'."""
    return _clean([{"taken_at": d["taken_at"], "status_when_taken": engine.STATUS_TEXT.get(d["status_at_dose"], d["status_at_dose"]),
                    "budget_left_percent_when_taken": _pct(d["budget_at_dose"]),
                    "temp_c_when_taken": _r(d["temp_at_dose"]),
                    "status_rechecked_now": engine.STATUS_TEXT.get(d["status_now"], d["status_now"]),
                    "budget_left_percent_rechecked_now": _pct(d["budget_now"]),
                    "changed_since_logged": d["changed"], "note": d["note"]}
                   for d in services.doses(item_id, 30)])


TOOLS = [list_my_medicines, get_medicine_details, get_exposure_calendar, simulate_exposure, get_outage_situation,
         get_alerts, get_doses]


def _system(lang: str | None) -> str:
    now = datetime.now(ZoneInfo("America/Detroit"))
    return f"""You are LIFELOG, an assistant that helps a patient understand the remaining stability ("life budget")
of their temperature-sensitive medicines. It is now {now:%A %B %d, %Y, %I:%M %p} in Dearborn, Michigan.
Rules:
- Always call tools first; every number you give must come from a tool result. Never estimate or re-round.
- Budgets are already percentages (budget_left_percent); temperatures are °C. Give the number and what it means.
- status_label is the overall verdict (it combines life budget, expiry / in-use dates and sensor health). For
  "which is worst / most urgent", use the order list_my_medicines returns (most urgent first) and say why.
- Mention a live trend when it is warming or cooling, and the minutes until it leaves its labeled range if given.
- If sensor_quiet_minutes is over 20, say the exposure since then is unknown and give the worst case.
- Quote the label text when it matters. If a tool returns an error, say so; never invent a medicine.
- Answer in under 150 words and finish with the one thing to do next.
{gem.SAFETY}{gem._lang(lang)}"""


def ask(question: str, history: list[dict] | None = None, lang: str | None = None) -> dict:
    contents = []
    for turn in (history or [])[-8:]:
        contents.append(types.Content(role="user" if turn.get("role") == "user" else "model",
                                      parts=[types.Part(text=str(turn.get("text", "")))]))
    contents.append(types.Content(role="user", parts=[types.Part(text=question)]))
    try:
        resp = gem.generate(
            model=config.GEMINI_MODEL, contents=contents,
            config=types.GenerateContentConfig(system_instruction=_system(lang), tools=TOOLS, temperature=0.1),
        )
    except gem.GeminiUnavailable as e:
        return offline_answer(str(e))
    calls = []
    for c in resp.automatic_function_calling_history or []:
        for p in c.parts or []:
            if p.function_call:
                calls.append({"name": p.function_call.name, "args": dict(p.function_call.args or {})})
    return {"answer": resp.text or "", "tool_calls": calls, "model": gem.last_model(), "offline": False}


def offline_answer(reason: str) -> dict:
    """When every Gemini model is unavailable, answer straight from Tiger: the most urgent medicine and why,
    the rest in order, and open alerts. Clearly labeled as not written by Gemini."""
    meds = list_my_medicines()
    why = reason.split(" Budgets,")[0]
    lines = [f"_Gemini is unavailable right now, so this is a summary straight from your data, without AI. {why}_", ""]
    if meds:
        w = meds[0]
        low = w["worst_case_budget_left_percent"]
        text = (f"**Most urgent: {w['nickname']}**: {w['status_label']}. {w['status_reason']} "
                f"Budget left {w['budget_left_percent']}%")
        if low is not None and w["budget_left_percent"] is not None and low < w["budget_left_percent"] - 1:
            text += f" (could be as low as {low}%)"
        text += f", now {w['current_temp_c']}°C ({w['zone'].lower()})." if w["current_temp_c"] is not None else "."
        lines.append(text)
        t = w.get("trend") or {}
        if t.get("trend") in ("warming", "cooling"):
            eta = t.get("minutes_until_it_leaves_labeled_range")
            lines.append(f"It's {t['trend']} {abs(t['slope_c_per_hour'])}°C/h"
                         + (f" and leaves its labeled range in about {eta} min." if eta else "."))
        lines.append("")
        for m in meds[1:]:
            lines.append(f"- {m['nickname']}: {m['status_label']}, {m['budget_left_percent']}% budget left")
    alerts = get_alerts()
    if alerts:
        lines += ["", "**Open alerts:**"] + [f"- {a['message']}" for a in alerts[:5]]
    return {"answer": "\n".join(lines),
            "tool_calls": [{"name": "list_my_medicines", "args": {}}, {"name": "get_alerts", "args": {}}],
            "model": None, "offline": True}

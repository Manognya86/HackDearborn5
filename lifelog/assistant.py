"""'Ask LIFELOG': Gemini answers questions by calling functions that query Tiger Data."""
import json

from google.genai import types

from . import config, db, engine, gem, services


def _clean(x):
    return json.loads(json.dumps(x, default=str))


def _me() -> int:
    return db.one("SELECT id FROM users WHERE is_me")["id"]


def list_my_medicines() -> list[dict]:
    """List the user's tracked medicines with id, nickname, product, life budget left (0-1),
    current temperature in Celsius, storage zone and hours left at the current temperature."""
    return _clean([{k: i[k] for k in ("id", "nickname", "product_name", "remaining", "current_temp", "zone",
                                      "hours_left", "stale_minutes")} for i in services.list_items(_me())])


def get_medicine_details(item_id: int) -> dict:
    """Full status of one medicine: life budget, what consumed it (episodes), data gaps,
    exposure statistics (mean kinetic temperature), open alerts, similar exposure patterns and label quotes."""
    d = services.item_detail(item_id)
    if not d:
        return {"error": "unknown item"}
    m = d["item"]["model"]
    return _clean({"nickname": d["item"]["nickname"], "state": d["state"], "budget_consumers": d["burners"],
                   "gaps": d["gaps"], "stats": d["stats"], "zones": d["zones"], "alerts": d["alerts"],
                   "similar_patterns": d["patterns"],
                   "label": {k: m.get(k) for k in ("target_quote", "bands", "freeze_quote", "above_limit_quote",
                                                    "above_limit_is_assumption", "visual_checks", "discard_rules")}})


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
    r = services.rescue()
    me = [p for p in r["people"] if db.one("SELECT is_me FROM users WHERE id = %s", (p["user_id"],))["is_me"]]
    return _clean({"active_outages": [f["properties"] for f in r["outages"]["features"]], "my_medicines": me,
                   "neighbors_at_risk": sum(1 for p in r["people"] if p["at_risk"])})


def get_alerts() -> list[dict]:
    """Open alerts for the user's medicines (frozen, above limit, low budget, sensor silent, outage)."""
    return _clean([{k: a[k] for k in ("nickname", "kind", "severity", "message", "created_at")}
                   for a in services.alerts(mine_only=True)])


TOOLS = [list_my_medicines, get_medicine_details, simulate_exposure, get_outage_situation, get_alerts]

SYSTEM = f"""You are LIFELOG, an assistant that helps a patient understand the remaining stability
("life budget") of their temperature-sensitive medicines. Always call tools to get facts; never guess numbers.
Quote the label text when it matters. Give short, concrete answers with the numbers. {gem.SAFETY}"""


def ask(question: str, history: list[dict] | None = None, lang: str | None = None) -> dict:
    contents = []
    for turn in (history or [])[-8:]:
        contents.append(types.Content(role=turn["role"], parts=[types.Part(text=turn["text"])]))
    contents.append(types.Content(role="user", parts=[types.Part(text=question)]))
    resp = gem.generate(
        model=config.GEMINI_MODEL, contents=contents,
        config=types.GenerateContentConfig(system_instruction=SYSTEM + gem._lang(lang), tools=TOOLS, temperature=0.2),
    )
    calls = []
    for c in resp.automatic_function_calling_history or []:
        for p in c.parts or []:
            if p.function_call:
                calls.append({"name": p.function_call.name, "args": dict(p.function_call.args or {})})
    return {"answer": resp.text or "", "tool_calls": calls}

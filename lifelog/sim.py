"""Live sensor simulator: streams one reading per medicine every few seconds so the dashboard,
the real-time continuous aggregate and the alert job all move during a demo."""
import random
import threading
from datetime import datetime, timezone

from . import db

TICK_S = 5
_rng = random.Random()
_state: dict = {"running": False, "ticks": 0, "rows": 0, "thread": None, "stop": None}


def _items() -> list[dict]:
    return db.query("""
        SELECT i.id, p.model,
               (SELECT r.temp_c FROM readings r WHERE r.item_id = i.id ORDER BY r.ts DESC LIMIT 1) AS last_t,
               (SELECT max(r.ts) FROM readings r WHERE r.item_id = i.id) AS last_ts,
               (SELECT o.indoor_temp_c FROM outages o JOIN users u ON ST_Contains(o.area, u.geom)
                 WHERE o.active AND u.id = i.user_id LIMIT 1) AS outage_t
        FROM items i JOIN products p ON p.id = i.product_id""")


def _next_temp(it: dict) -> float:
    m = it["model"]
    home = 4.5 if m["target_max_c"] <= 10 else 22.0
    last = it["last_t"] if it["last_t"] is not None else home
    if it["outage_t"] is not None:
        goal, tau = it["outage_t"], 40.0   # fridge without power, demo-accelerated warming
    else:
        goal, tau = home, 6.0              # back where it belongs (e.g. out of the hot car)
    return round(last + (goal - last) / tau + _rng.gauss(0, 0.15), 2)


def _loop(stop: threading.Event) -> None:
    while not stop.is_set():
        now = datetime.now(timezone.utc)
        rows = []
        for it in _items():
            if it["last_ts"] is None or (now - it["last_ts"]).total_seconds() > 1800:
                continue  # a silent sensor stays silent until its buffered data arrives
            rows.append((now, it["id"], _next_temp(it), "sensor"))
        if rows:
            with db.conn() as c, c.cursor() as cur:
                with cur.copy("COPY readings (ts, item_id, temp_c, source) FROM STDIN") as cp:
                    for r in rows:
                        cp.write_row(r)
        _state["ticks"] += 1
        _state["rows"] += len(rows)
        stop.wait(TICK_S)


def start() -> dict:
    if not _state["running"]:
        stop = threading.Event()
        t = threading.Thread(target=_loop, args=(stop,), daemon=True)
        _state.update(running=True, stop=stop, thread=t)
        t.start()
    return status()


def stop() -> dict:
    if _state["running"]:
        _state["stop"].set()
        _state["running"] = False
    return status()


def status() -> dict:
    return {"running": _state["running"], "ticks": _state["ticks"], "rows": _state["rows"], "tick_seconds": TICK_S}

"""Real-time plumbing. Postgres LISTEN/NOTIFY -> in-process broker -> Server-Sent Events.

Triggers in sql/realtime.sql publish on channel 'lifelog' whenever readings land (one event per
INSERT/COPY statement) and whenever an alert opens, changes or resolves."""
import json
import queue
import threading
import time

import psycopg

from . import config

CHANNEL = "lifelog"
_subscribers: set[queue.Queue] = set()
_lock = threading.Lock()
_started = threading.Event()
_alert_wake = threading.Event()
_listening = threading.Event()          # set while the LISTEN connection is up
_threads: dict[str, threading.Thread] = {}


def publish(event: dict) -> None:
    with _lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(event)
        except queue.Full:
            pass


def subscribe() -> queue.Queue:
    start()
    q: queue.Queue = queue.Queue(maxsize=200)
    with _lock:
        _subscribers.add(q)
    return q


def unsubscribe(q: queue.Queue) -> None:
    with _lock:
        _subscribers.discard(q)


def subscriber_count() -> int:
    return len(_subscribers)


def _listen_forever() -> None:
    while True:
        try:
            with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
                conn.execute(f"LISTEN {CHANNEL}")
                _listening.set()
                for n in conn.notifies():
                    try:
                        publish(json.loads(n.payload))
                    except ValueError:
                        publish({"type": "raw", "payload": n.payload})
        except Exception:  # connection dropped: back off and reconnect
            _listening.clear()
            time.sleep(3)


def status() -> dict:
    t = _threads.get("listen")
    return {"listen_thread_alive": bool(t and t.is_alive()), "listen_connected": _listening.is_set(),
            "subscribers": subscriber_count()}


def _alert_loop() -> None:
    """Re-evaluates alert conditions shortly after new data arrives (debounced), on top of the
    scheduled check_alerts job, so alerts appear within seconds instead of up to a minute."""
    while True:
        _alert_wake.wait()
        time.sleep(1.5)
        _alert_wake.clear()
        try:
            with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
                conn.execute("CALL check_alerts()")
        except Exception:
            time.sleep(3)


def alerts_soon() -> None:
    start()
    _alert_wake.set()


def start() -> None:
    if _started.is_set() or not config.DATABASE_URL:
        return
    _started.set()
    _threads["listen"] = threading.Thread(target=_listen_forever, daemon=True, name="pg-listen")
    _threads["alerts"] = threading.Thread(target=_alert_loop, daemon=True, name="alert-check")
    for t in _threads.values():
        t.start()

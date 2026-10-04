import atexit
import time
from contextlib import contextmanager
from contextvars import ContextVar

import psycopg

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        if not config.DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set in .env")
        _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=8,
                               kwargs={"row_factory": dict_row}, open=True)
        atexit.register(_pool.close)
    return _pool


# ------------------------------------------------------------------ who is asking (row-level security)
_user: ContextVar[int | None] = ContextVar("lifelog_user", default=None)
_system: ContextVar[bool] = ContextVar("lifelog_system", default=False)


def set_user(user_id: int | None):
    return _user.set(user_id)


def reset_user(token) -> None:
    _user.reset(token)


@contextmanager
def system():
    """Run as the table owner: background jobs and the explicitly shared community features."""
    tok = _system.set(True)
    try:
        yield
    finally:
        _system.reset(tok)


def current_user() -> int | None:
    return _user.get()


def me() -> int:
    """The signed-in user; background jobs and tests fall back to the demo patient."""
    uid = _user.get()
    if uid:
        return uid
    with system():
        row = one("SELECT id FROM users WHERE is_me ORDER BY id LIMIT 1")
    return row["id"] if row else 0


_rls: bool | None = None


def rls_available() -> bool:
    """Can this database user switch to the restricted role? (Hosted databases may not allow creating it.)"""
    global _rls
    if _rls is None:
        try:
            with pool().connection() as c:
                c.execute("SET LOCAL ROLE lifelog_app")
                c.rollback()
            _rls = True
        except Exception:  # noqa: BLE001
            _rls = False
    return _rls


@contextmanager
def conn(autocommit: bool = False):
    with pool().connection() as c:
        uid = _user.get()
        if autocommit:
            c.autocommit = True
        elif uid and not _system.get() and rls_available():
            # for this transaction only: become the restricted role and say who we are; policies do the rest
            c.execute("SELECT set_config('role', 'lifelog_app', true), set_config('lifelog.user_id', %s, true)",
                      (str(uid),))
        yield c
        if autocommit:
            c.autocommit = False


def query(sql: str, params=None) -> list[dict]:
    with conn() as c, c.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []


def one(sql: str, params=None) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params=None) -> None:
    with conn() as c:
        c.execute(sql, params)


def call_refresh(c, sql: str, params=None, attempts: int = 8) -> None:
    """CALL refresh_continuous_aggregate(...), retried while Tiger's own refresh policy holds the same
    window ("could not refresh continuous aggregate ... due to a concurrent refresh"). Autocommit only."""
    for k in range(attempts):
        try:
            c.execute(sql, params)
            return
        except psycopg.errors.LockNotAvailable:
            if k == attempts - 1:
                raise
            time.sleep(min(10.0, 1.5 * (k + 1)))


def refresh_readings(start, end) -> None:
    """Targeted continuous-aggregate refresh, widened to whole 5-minute buckets so the bucket
    containing `end` is re-materialized too. Must run outside a transaction."""
    with conn(autocommit=True) as c:
        call_refresh(c, """CALL refresh_continuous_aggregate('readings_5m',
                        time_bucket('5 minutes', %s::timestamptz),
                        time_bucket('5 minutes', %s::timestamptz) + INTERVAL '5 minutes')""", (start, end))
        try:  # the daily rollup sits on top of readings_5m and must follow it
            call_refresh(c, """CALL refresh_continuous_aggregate('readings_1d',
                            time_bucket('1 day', %s::timestamptz),
                            time_bucket('1 day', %s::timestamptz) + INTERVAL '1 day')""", (start, end))
        except Exception:
            pass

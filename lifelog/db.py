import atexit
import time
from contextlib import contextmanager

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


@contextmanager
def conn(autocommit: bool = False):
    with pool().connection() as c:
        if autocommit:
            c.autocommit = True
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

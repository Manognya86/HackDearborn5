import atexit
from contextlib import contextmanager

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


def refresh_readings(start, end) -> None:
    """Targeted continuous-aggregate refresh, widened to whole 5-minute buckets so the bucket
    containing `end` is re-materialized too. Must run outside a transaction."""
    with conn(autocommit=True) as c:
        c.execute("""CALL refresh_continuous_aggregate('readings_5m',
                        time_bucket('5 minutes', %s::timestamptz),
                        time_bucket('5 minutes', %s::timestamptz) + INTERVAL '5 minutes')""", (start, end))

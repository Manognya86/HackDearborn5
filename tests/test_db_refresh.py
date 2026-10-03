"""Aggregate refreshes survive a concurrent refresh by Tiger's own policy job (LockNotAvailable). No database needed."""
from unittest import mock

import psycopg
import pytest

from lifelog import db


def test_refresh_retries_while_the_policy_holds_the_lock(monkeypatch):
    monkeypatch.setattr(db.time, "sleep", lambda s: None)
    c = mock.Mock()
    c.execute.side_effect = [psycopg.errors.LockNotAvailable("concurrent refresh"),
                             psycopg.errors.LockNotAvailable("again"), None]
    db.call_refresh(c, "CALL refresh_continuous_aggregate('readings_5m', NULL, NULL)")
    assert c.execute.call_count == 3


def test_refresh_gives_up_after_the_last_attempt(monkeypatch):
    monkeypatch.setattr(db.time, "sleep", lambda s: None)
    c = mock.Mock()
    c.execute.side_effect = psycopg.errors.LockNotAvailable("busy")
    with pytest.raises(psycopg.errors.LockNotAvailable):
        db.call_refresh(c, "CALL x()", attempts=3)
    assert c.execute.call_count == 3

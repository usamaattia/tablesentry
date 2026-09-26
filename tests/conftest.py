from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text

from tablesentry.config import parse_config
from tablesentry.history import History
from tablesentry.warehouse import Warehouse

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
PG_URL = os.environ.get(
    "TABLESENTRY_TEST_POSTGRES_URL",
    "postgresql+psycopg://tubepulse:tubepulse@localhost/sentry_test",
)


def _duckdb(tmp_path):
    return f"duckdb:///{tmp_path / 'wh.duckdb'}"


def _postgres(tmp_path):
    try:
        eng = create_engine(PG_URL)
        with eng.begin() as c:
            c.execute(text("drop schema if exists sentry cascade"))
        eng.dispose()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not available: {exc}")
    return PG_URL


@pytest.fixture(params=["duckdb", "postgres"])
def warehouse_url(request, tmp_path):
    """Every warehouse test runs against DuckDB and a real Postgres."""
    return {"duckdb": _duckdb, "postgres": _postgres}[request.param](tmp_path)


@pytest.fixture
def wh(warehouse_url):
    w = Warehouse(warehouse_url)
    w.exec = lambda sql: _exec(w, sql)
    w.exec("create schema if not exists sentry")
    w.exec(
        "create table sentry.orders (order_id integer, customer_id integer, "
        "total numeric(10,2), updated_at timestamp)"
    )
    yield w
    w.close()


def _exec(w: Warehouse, sql: str) -> None:
    with w.engine.begin() as c:
        c.execute(text(sql))


def insert_orders(w: Warehouse, n: int, *, nulls: int = 0, updated_at: str = "2026-09-20 11:00:00",
                  start: int = 0, negative: int = 0) -> None:  # fmt: skip
    rows = []
    for i in range(n):
        cust = "null" if i < nulls else str(1000 + i)
        total = -5 if i < negative else 10
        rows.append(f"({start + i}, {cust}, {total}, timestamp '{updated_at}')")
    for k in range(0, len(rows), 500):
        w.exec("insert into sentry.orders values " + ", ".join(rows[k : k + 500]))


@pytest.fixture
def history():
    h = History(":memory:")
    yield h
    h.close()


def make_config(url: str, state, checks: dict, **extra):
    raw = {
        "connections": {"wh": url},
        "state": str(state),
        "tables": [{"name": "sentry.orders", "checks": checks}],
        "alerts": [{"type": "console"}],
        **extra,
    }
    return parse_config(raw)

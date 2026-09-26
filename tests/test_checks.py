"""Every check, against both DuckDB and a real Postgres."""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import NOW, insert_orders

from tablesentry import checks
from tablesentry.checks import Context
from tablesentry.config import (
    AnomalySettings,
    CustomCheck,
    FreshnessCheck,
    NullCheck,
    RowCountCheck,
    SchemaCheck,
    TableConfig,
)
from tablesentry.models import Status


def ctx(wh, history, now=NOW, run="r1", **anomaly):
    history.db.execute(
        "insert or ignore into runs values (?, ?, ?, ?)",
        (run, now.isoformat(), now.isoformat(), "pass"),
    )
    return Context(wh, history, run, now, "UTC", AnomalySettings(**anomaly))


def table(**checks_):
    return TableConfig(name="sentry.orders", connection="wh", **checks_)


# -- freshness -----------------------------------------------------------------


@pytest.mark.parametrize(
    "updated_at,status",
    [("2026-09-20 11:00:00", Status.PASS),     # 1h old
     ("2026-09-20 02:00:00", Status.WARN),     # 10h old: past warn_after 6h
     ("2026-09-18 12:00:00", Status.FAIL),     # 48h old: past max_age 24h
     ("2026-09-20 18:00:00", Status.WARN)],    # 6h in the future: timezone problem
)  # fmt: skip
def test_freshness(wh, history, updated_at, status):
    insert_orders(wh, 5, updated_at=updated_at)
    t = table(freshness=FreshnessCheck("updated_at", timedelta(hours=24), timedelta(hours=6)))
    r = checks.freshness(ctx(wh, history), t)
    assert r.status is status, r.message


def test_freshness_on_empty_table_fails(wh, history):
    t = table(freshness=FreshnessCheck("updated_at", timedelta(hours=1)))
    r = checks.freshness(ctx(wh, history), t)
    assert r.status is Status.FAIL and "no rows" in r.message


def test_where_filter_is_applied(wh, history):
    insert_orders(wh, 5, updated_at="2026-09-20 11:00:00")
    insert_orders(wh, 5, updated_at="2026-09-10 11:00:00", start=100)
    t = table(
        freshness=FreshnessCheck("updated_at", timedelta(hours=24)),
        where="updated_at < timestamp '2026-09-15 00:00:00'",
    )
    assert checks.freshness(ctx(wh, history), t).status is Status.FAIL


# -- row count -----------------------------------------------------------------


def test_row_count_thresholds(wh, history):
    insert_orders(wh, 50)
    assert (
        checks.row_count(ctx(wh, history), table(row_count=RowCountCheck(min=10))).status
        is Status.PASS
    )
    r = checks.row_count(
        ctx(wh, history, run="r2"), table(row_count=RowCountCheck(min=100, severity="warn"))
    )
    assert r.status is Status.WARN and "below the minimum" in r.message


def test_row_count_change_vs_recent_median(wh, history):
    t = table(row_count=RowCountCheck(max_change_pct=30))
    for i, run in enumerate(["a", "b", "c"]):
        wh.exec("delete from sentry.orders")
        insert_orders(wh, 100)
        assert (
            checks.row_count(ctx(wh, history, NOW + timedelta(hours=i), run), t).status
            is Status.PASS
        )
    wh.exec("delete from sentry.orders")
    insert_orders(wh, 40)
    r = checks.row_count(ctx(wh, history, NOW + timedelta(hours=5), "d"), t)
    assert r.status is Status.FAIL and "-60.0%" in r.message and r.details["baseline"] == 100


def test_row_count_anomaly_learns_then_flags(wh, history):
    t = table(row_count=RowCountCheck(anomaly="fail"))
    for i, n in enumerate([100, 104, 98, 101, 99, 103, 100]):
        wh.exec("delete from sentry.orders")
        insert_orders(wh, n)
        r = checks.row_count(ctx(wh, history, NOW + timedelta(hours=i), f"r{i}", min_history=5), t)
        assert r.status is Status.PASS
    wh.exec("delete from sentry.orders")
    insert_orders(wh, 20)
    r = checks.row_count(ctx(wh, history, NOW + timedelta(hours=9), "spike", min_history=5), t)
    assert r.status is Status.FAIL and "unusual" in r.message


# -- nulls ---------------------------------------------------------------------


def test_null_rate_threshold_and_single_scan(wh, history):
    insert_orders(wh, 200, nulls=30)  # 15% null customer_id, 0% null order_id
    t = table(nulls=[NullCheck("customer_id", max_pct=10), NullCheck("order_id", max_pct=0)])
    by_col = {r.details["column"]: r for r in checks.nulls(ctx(wh, history), t)}
    assert by_col["customer_id"].status is Status.FAIL and by_col["customer_id"].observed == 15.0
    assert by_col["order_id"].status is Status.PASS


def test_null_spike_detected_from_history(wh, history):
    t = table(nulls=[NullCheck("customer_id", anomaly="warn")])
    for i in range(7):
        wh.exec("delete from sentry.orders")
        insert_orders(wh, 200, nulls=2)  # 1%
        checks.nulls(ctx(wh, history, NOW + timedelta(hours=i), f"r{i}"), t)
    wh.exec("delete from sentry.orders")
    insert_orders(wh, 200, nulls=20)  # 10%
    (r,) = checks.nulls(ctx(wh, history, NOW + timedelta(hours=8), "spike"), t)
    assert r.status is Status.WARN and "unusual" in r.message


# -- schema --------------------------------------------------------------------


def test_schema_drift(wh, history):
    t = table(schema=SchemaCheck())
    first = checks.schema(ctx(wh, history), t)
    assert first.status is Status.PASS and "baseline" in first.message
    assert checks.schema(ctx(wh, history, run="r2"), t).message.startswith("unchanged")

    wh.exec("alter table sentry.orders add column coupon varchar(20)")
    wh.exec("alter table sentry.orders drop column total")
    r = checks.schema(ctx(wh, history, run="r3"), t)
    assert r.status is Status.FAIL
    assert r.details["added"] == ["coupon"] and r.details["removed"] == ["total"]
    # the change becomes the new baseline: reported once
    assert checks.schema(ctx(wh, history, run="r4"), t).status is Status.PASS


def test_schema_type_change_and_allow_added(wh, history):
    t = table(schema=SchemaCheck(allow_added=True))
    checks.schema(ctx(wh, history), t)
    wh.exec("alter table sentry.orders add column note varchar(10)")
    assert checks.schema(ctx(wh, history, run="r2"), t).status is Status.PASS  # allowed
    wh.exec("alter table sentry.orders alter column customer_id type bigint")
    r = checks.schema(ctx(wh, history, run="r3"), t)
    assert r.status is Status.FAIL and r.details["retyped"][0].startswith(
        "customer_id INTEGER -> BIGINT"
    )


# -- custom --------------------------------------------------------------------


def test_custom_sql(wh, history):
    insert_orders(wh, 20, negative=3)
    t = table(custom=[
        CustomCheck("negatives", "select count(*) from {table} where total < 0", equals=0),
        CustomCheck("enough", "select count(*) from {table}", min=10, severity="warn"),
        CustomCheck("broken", "select count(*) from {table} where no_such_column = 1", max=0),
    ])  # fmt: skip
    res = {r.check_id.split(":")[-1]: r for r in checks.custom(ctx(wh, history), t)}
    assert res["negatives"].status is Status.FAIL and res["negatives"].observed == 3
    assert res["enough"].status is Status.PASS
    assert res["broken"].status is Status.ERROR and "query failed" in res["broken"].message


def test_missing_table_gives_errors_not_a_crash(wh, history):
    t = TableConfig(
        name="sentry.nope",
        connection="wh",
        row_count=RowCountCheck(min=1),
        nulls=[NullCheck("a", max_pct=1)],
    )
    results = checks.run_table(ctx(wh, history), t)
    assert {r.check_id for r in results} == {"sentry.nope:row_count", "sentry.nope:nulls:a"}
    assert all(r.status is Status.ERROR for r in results)

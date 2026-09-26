"""Three weeks of a small shop's warehouse, with five scripted incidents.

    python examples/demo.py

Builds a DuckDB warehouse, loads one day at a time, and runs tablesentry after each load
with the clock set to that evening. Watch it learn what normal looks like, then catch:

    day  9  three orders with a negative total           -> custom check fails (fixed day 10)
    day 12  upstream bug: 18% of orders lose customer_id -> null threshold + anomaly
    day 15  a new column appears in the staging table    -> schema drift
    day 17  a partial load, 12% of the usual volume      -> volume anomaly + change limit
    day 19  the nightly load does not run                -> freshness fails

Alerts go to the console (swap in Slack in demo.yml). An HTML report is written at the end.
"""

from __future__ import annotations

import os
import random
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

from tablesentry.config import load_config
from tablesentry.report import write as write_report
from tablesentry.runner import run

HERE = Path(__file__).parent
DB = HERE / "demo_shop.duckdb"
START = datetime(2026, 9, 1, tzinfo=timezone.utc)
DAYS = int(os.environ.get("DEMO_DAYS", 21))


def load_day(con: duckdb.DuckDBPyConnection, day: int, rng: random.Random) -> None:
    if day == 19:
        return  # the scheduler fell over: nothing loaded
    date = START + timedelta(days=day - 1)
    n = int(rng.gauss(1000, 40) * (1.1 if date.weekday() >= 5 else 1.0))
    if day == 17:
        n = int(n * 0.12)
    null_rate = 0.18 if day == 12 else 0.01
    if day == 10:  # someone fixes the bad rows from day 9
        con.execute("update analytics.orders set total = abs(total) where total < 0")
    if day == 15:
        con.execute("alter table staging.orders_daily add column coupon_code varchar")
    con.execute("delete from staging.orders_daily")
    start_id = con.execute("select coalesce(max(order_id), 0) from analytics.orders").fetchone()[0]
    rows = []
    for i in range(n):
        total = round(rng.lognormvariate(3.5, 0.6), 2)
        if day == 9 and i < 3:
            total = -total
        customer = None if rng.random() < null_rate else rng.randint(1, 5000)
        ts = date + timedelta(hours=6 + 16 * i / n)
        rows.append((start_id + i + 1, customer, total, "paid", ts.replace(tzinfo=None)))
    cols = "order_id, customer_id, total, status, updated_at"
    con.executemany(f"insert into staging.orders_daily ({cols}) values (?, ?, ?, ?, ?)", rows)
    con.execute(f"insert into analytics.orders select {cols} from staging.orders_daily")


def main() -> None:
    DB.unlink(missing_ok=True)
    state = HERE / ".tablesentry"
    shutil.rmtree(state, ignore_errors=True)
    con = duckdb.connect(str(DB))
    con.execute("create schema staging; create schema analytics")
    ddl = (
        "(order_id bigint, customer_id bigint, total decimal(10,2), status varchar, "
        "updated_at timestamp)"
    )
    con.execute(f"create table staging.orders_daily {ddl}")
    con.execute(f"create table analytics.orders {ddl}")
    con.close()

    cfg = load_config(HERE / "demo.yml")
    rng = random.Random(7)
    for day in range(1, DAYS + 1):
        con = duckdb.connect(str(DB))
        load_day(con, day, rng)
        con.close()
        evening = START + timedelta(days=day - 1, hours=23)
        if day in (1, 9, 12, 15, 17, 19):
            print()
        report = run(cfg, now=evening)  # alerts print via the console channel
        c = report.counts()
        if not report.events:
            print(f"day {day:2}  {c['pass']} passed, {c['warn'] + c['fail']} problems, no alerts")

    out = write_report(cfg.state_path, HERE / "demo_report.html", "Demo shop: data quality")
    print(f"\nHTML report: {out}")


if __name__ == "__main__":
    main()

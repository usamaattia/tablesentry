"""Run history in a local SQLite file (no server needed).

Stores every check result, the metrics behind them (row counts, null rates, ...) for
anomaly detection, the last known schema of each table for drift detection, and per-check
alert state so the same problem is not announced on every run.

For scheduled CI runs, persist the file between runs (see the GitHub Action in README).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import CheckResult

SCHEMA = """
create table if not exists runs (
    run_id text primary key,
    started_at text not null,
    finished_at text,
    status text
);
create table if not exists results (
    run_id text not null,
    check_id text not null,
    table_name text not null,
    kind text not null,
    status text not null,
    message text,
    observed text,
    expected text,
    details text,
    primary key (run_id, check_id)
);
create table if not exists metrics (
    check_id text not null,
    metric text not null,
    value real not null,
    measured_at text not null,
    run_id text not null
);
create index if not exists metrics_lookup on metrics (check_id, metric, measured_at);
create table if not exists schema_snapshots (
    table_key text primary key,
    columns text not null,
    captured_at text not null
);
create table if not exists alert_state (
    check_id text primary key,
    status text not null,
    since text not null,
    last_notified_at text,
    last_notified_status text
);
"""


@dataclass
class AlertState:
    check_id: str
    status: str
    since: datetime
    last_notified_at: datetime | None
    last_notified_status: str | None


def _ts(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class History:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # -- runs ---------------------------------------------------------------

    def start_run(self, now: datetime) -> str:
        run_id = uuid.uuid4().hex[:12]
        self.db.execute("insert into runs (run_id, started_at) values (?, ?)", (run_id, _ts(now)))
        return run_id

    def finish_run(self, run_id: str, now: datetime, status: str) -> None:
        self.db.execute(
            "update runs set finished_at = ?, status = ? where run_id = ?",
            (_ts(now), status, run_id),
        )
        self.db.commit()

    def record(self, run_id: str, r: CheckResult) -> None:
        self.db.execute(
            "insert or replace into results values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                r.check_id,
                r.table,
                r.kind,
                r.status.value,
                r.message,
                json.dumps(r.observed, default=str),
                json.dumps(r.expected, default=str),
                json.dumps(r.details, default=str),
            ),
        )

    def runs(self, limit: int = 30) -> list[dict]:
        cur = self.db.execute(
            "select run_id, started_at, status from runs where finished_at is not null "
            "order by started_at desc limit ?",
            (limit,),
        )
        return [{"run_id": a, "started_at": b, "status": c} for a, b, c in cur]

    def results(self, run_id: str) -> list[dict]:
        cur = self.db.execute(
            "select check_id, table_name, kind, status, message, observed, expected "
            "from results where run_id = ? order by table_name, check_id",
            (run_id,),
        )
        cols = ["check_id", "table", "kind", "status", "message", "observed", "expected"]
        return [dict(zip(cols, row, strict=True)) for row in cur]

    def status_history(self, check_id: str, limit: int = 30) -> list[tuple[str, str]]:
        cur = self.db.execute(
            "select r.started_at, x.status from results x join runs r using (run_id) "
            "where x.check_id = ? order by r.started_at desc limit ?",
            (check_id, limit),
        )
        return list(reversed(cur.fetchall()))

    # -- metrics ------------------------------------------------------------

    def add_metric(self, run_id: str, check_id: str, metric: str, value: float, now: datetime):
        self.db.execute(
            "insert into metrics values (?, ?, ?, ?, ?)",
            (check_id, metric, float(value), _ts(now), run_id),
        )

    def metric_history(self, check_id: str, metric: str, limit: int) -> list[float]:
        """Most recent values first, excluding anything recorded by the current run."""
        cur = self.db.execute(
            "select value from metrics where check_id = ? and metric = ? "
            "and run_id not in (select run_id from runs where finished_at is null) "
            "order by measured_at desc limit ?",
            (check_id, metric, limit),
        )
        return [row[0] for row in cur]

    def metric_series(self, check_id: str, metric: str, limit: int = 60) -> list[tuple[str, float]]:
        cur = self.db.execute(
            "select measured_at, value from metrics where check_id = ? and metric = ? "
            "order by measured_at desc limit ?",
            (check_id, metric, limit),
        )
        return list(reversed(cur.fetchall()))

    # -- schema snapshots -----------------------------------------------------

    def get_schema(self, table_key: str) -> list[dict] | None:
        row = self.db.execute(
            "select columns from schema_snapshots where table_key = ?", (table_key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def set_schema(self, table_key: str, columns: list[dict], now: datetime) -> None:
        self.db.execute(
            "insert or replace into schema_snapshots values (?, ?, ?)",
            (table_key, json.dumps(columns), _ts(now)),
        )

    # -- alert state ----------------------------------------------------------

    def get_alert_state(self, check_id: str) -> AlertState | None:
        row = self.db.execute(
            "select check_id, status, since, last_notified_at, last_notified_status "
            "from alert_state where check_id = ?",
            (check_id,),
        ).fetchone()
        if not row:
            return None
        return AlertState(row[0], row[1], _dt(row[2]), _dt(row[3]), row[4])

    def set_alert_state(self, s: AlertState) -> None:
        self.db.execute(
            "insert or replace into alert_state values (?, ?, ?, ?, ?)",
            (s.check_id, s.status, _ts(s.since), _ts(s.last_notified_at), s.last_notified_status),
        )

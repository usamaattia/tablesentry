"""The checks. Each takes a table's config and returns CheckResults.

Check ids are stable (``<table>:<kind>[:<name>]``) so history and alert state follow a
check across runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from . import anomaly
from .config import AnomalySettings, TableConfig
from .history import History
from .models import CheckResult, Status
from .warehouse import Warehouse, to_utc

BASELINE_RUNS = 7  # row_count.max_change_pct compares with the median of this many runs


@dataclass
class Context:
    wh: Warehouse
    history: History
    run_id: str
    now: datetime
    tz: str
    anomaly: AnomalySettings


def _worst(statuses: list[Status]) -> Status:
    return max(statuses, key=lambda s: s.rank, default=Status.PASS)


def _sev(severity: str) -> Status:
    return Status.FAIL if severity == "fail" else Status.WARN


def _where(t: TableConfig) -> str:
    return f" where {t.where}" if t.where else ""


def _fmt_age(td: timedelta) -> str:
    s = int(td.total_seconds())
    if s < 0:
        return f"{_fmt_age(-td)} in the future"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


def _anomaly_check(
    ctx: Context,
    check_id: str,
    metric: str,
    value: float,
    severity: str | None,
    min_scale: float,
    unit: str = "",
) -> tuple[Status, str | None, dict[str, Any]]:
    if severity is None:
        return Status.PASS, None, {}
    s = ctx.anomaly
    hist = ctx.history.metric_history(check_id, metric, s.window)
    v = anomaly.detect(value, hist, s.sensitivity, s.min_history, min_scale)
    if v is None:
        return Status.PASS, None, {"anomaly": f"learning ({len(hist)}/{s.min_history} runs)"}
    info = {"anomaly_score": round(v.score, 2), "baseline_median": v.median}
    if v.is_anomaly:
        return _sev(severity), f"unusual value: {v.describe(unit)}", info
    return Status.PASS, None, info


# ---------------------------------------------------------------------------


def freshness(ctx: Context, t: TableConfig) -> CheckResult:
    f = t.freshness
    check_id = f"{t.name}:freshness"
    col = ctx.wh.quote_column(f.column)
    raw = ctx.wh.scalar(f"select max({col}) from {ctx.wh.quote_table(t.name)}{_where(t)}")
    latest = to_utc(raw, ctx.tz)
    expected = f"< {_fmt_age(f.max_age)}"
    if latest is None:
        return CheckResult(
            check_id,
            t.name,
            "freshness",
            Status.FAIL,
            f"no rows with a {f.column}: cannot tell when data last arrived",
            None,
            expected,
        )
    age = ctx.now - latest
    ctx.history.add_metric(ctx.run_id, check_id, "age_hours", age.total_seconds() / 3600, ctx.now)
    details = {"latest": latest.isoformat()}
    if age < -timedelta(minutes=5):
        return CheckResult(
            check_id,
            t.name,
            "freshness",
            Status.WARN,
            f"latest {f.column} is {_fmt_age(age)}: check the timezone setting",
            _fmt_age(age),
            expected,
            details,
        )
    if age > f.max_age:
        status = Status.FAIL
    elif f.warn_after and age > f.warn_after:
        status = Status.WARN
    else:
        status = Status.PASS
    msg = f"last {f.column} was {_fmt_age(age)} ago"
    if status is not Status.PASS:
        limit = f.max_age if status is Status.FAIL else f.warn_after
        msg += f" (limit {_fmt_age(limit)})"
    return CheckResult(check_id, t.name, "freshness", status, msg, _fmt_age(age), expected, details)


def row_count(ctx: Context, t: TableConfig) -> CheckResult:
    rc = t.row_count
    check_id = f"{t.name}:row_count"
    n = int(ctx.wh.scalar(f"select count(*) from {ctx.wh.quote_table(t.name)}{_where(t)}"))
    statuses, reasons, details = [], [], {}

    if rc.min is not None and n < rc.min:
        statuses.append(_sev(rc.severity))
        reasons.append(f"{n:,} rows, below the minimum of {rc.min:,.0f}")
    if rc.max is not None and n > rc.max:
        statuses.append(_sev(rc.severity))
        reasons.append(f"{n:,} rows, above the maximum of {rc.max:,.0f}")

    # Compare with the median of recent runs, not just the last one, so the day after an
    # incident (volume bouncing back to normal) is not flagged as a second incident.
    recent = ctx.history.metric_history(check_id, "row_count", BASELINE_RUNS)
    if recent:
        base = median(recent)
        change = (n - base) / base * 100 if base else (0.0 if n == 0 else 100.0)
        details["change_pct"] = round(change, 2)
        details["baseline"] = base
        if rc.max_change_pct is not None and abs(change) > rc.max_change_pct:
            statuses.append(_sev(rc.severity))
            reasons.append(f"{change:+.1f}% vs the recent median ({base:,.0f} -> {n:,})")

    s, why, info = _anomaly_check(
        ctx, check_id, "row_count", n, rc.anomaly, min_scale=max(1.0, 0.025 * n), unit=" rows"
    )
    details.update(info)
    if why:
        statuses.append(s)
        reasons.append(f"{n:,} rows is an {why}")

    ctx.history.add_metric(ctx.run_id, check_id, "row_count", n, ctx.now)
    status = _worst(statuses)
    msg = "; ".join(reasons) if reasons else f"{n:,} rows"
    return CheckResult(check_id, t.name, "row_count", status, msg, n, None, details)


def nulls(ctx: Context, t: TableConfig) -> list[CheckResult]:
    # One scan for every column: count(*) and count(col) for each.
    cols = [ctx.wh.quote_column(c.column) for c in t.nulls]
    select = ", ".join(["count(*)"] + [f"count({c})" for c in cols])
    row = ctx.wh.row(f"select {select} from {ctx.wh.quote_table(t.name)}{_where(t)}")
    total = int(row[0])
    out = []
    for c, non_null in zip(t.nulls, row[1:], strict=True):
        check_id = f"{t.name}:nulls:{c.column}"
        pct = 0.0 if total == 0 else (total - int(non_null)) / total * 100
        statuses, reasons = [], []
        if c.max_pct is not None and pct > c.max_pct:
            statuses.append(_sev(c.severity))
            reasons.append(f"{pct:.2f}% null, above the limit of {c.max_pct:g}%")
        s, why, info = _anomaly_check(
            ctx, check_id, "null_pct", pct, c.anomaly, min_scale=0.5, unit="%"
        )
        if why:
            statuses.append(s)
            reasons.append(f"{pct:.2f}% null is an {why}")
        ctx.history.add_metric(ctx.run_id, check_id, "null_pct", pct, ctx.now)
        out.append(
            CheckResult(
                check_id,
                t.name,
                "nulls",
                _worst(statuses),
                "; ".join(reasons) or f"{pct:.2f}% null in {c.column}",
                round(pct, 4),
                f"<= {c.max_pct:g}%" if c.max_pct is not None else None,
                {"column": c.column, "rows": total, **info},
            )
        )
    return out


def schema(ctx: Context, t: TableConfig) -> CheckResult:
    check_id = f"{t.name}:schema"
    table_key = f"{t.connection}:{t.name}"
    current = [{"name": c.name, "type": c.type} for c in ctx.wh.columns(t.name)]
    before = ctx.history.get_schema(table_key)
    ctx.history.set_schema(table_key, current, ctx.now)
    if before is None:
        return CheckResult(
            check_id,
            t.name,
            "schema",
            Status.PASS,
            f"baseline recorded ({len(current)} columns)",
            len(current),
        )

    old = {c["name"].lower(): c for c in before}
    new = {c["name"].lower(): c for c in current}
    removed = [old[k]["name"] for k in old if k not in new]
    added = [new[k]["name"] for k in new if k not in old]
    retyped = [
        f"{new[k]['name']} {old[k]['type']} -> {new[k]['type']}"
        for k in new
        if k in old and old[k]["type"] != new[k]["type"]
    ]
    changes, statuses = [], []
    if removed:
        changes.append("removed " + ", ".join(removed))
        statuses.append(_sev(t.schema.severity))
    if retyped:
        changes.append("type changed " + ", ".join(retyped))
        statuses.append(_sev(t.schema.severity))
    if added:
        changes.append("added " + ", ".join(added))
        if not t.schema.allow_added:
            statuses.append(_sev(t.schema.severity))
    details = {"removed": removed, "added": added, "retyped": retyped}
    if not changes:
        return CheckResult(
            check_id,
            t.name,
            "schema",
            Status.PASS,
            f"unchanged ({len(current)} columns)",
            len(current),
            None,
            details,
        )
    return CheckResult(
        check_id,
        t.name,
        "schema",
        _worst(statuses),
        "schema changed: " + "; ".join(changes),
        len(current),
        None,
        details,
    )


def custom(ctx: Context, t: TableConfig) -> list[CheckResult]:
    out = []
    for c in t.custom:
        check_id = f"{t.name}:custom:{c.name}"
        sql = c.sql.replace("{table}", ctx.wh.quote_table(t.name))
        try:
            value = ctx.wh.scalar(sql)
            value = float(value) if value is not None else None
        except Exception as exc:  # noqa: BLE001 - report any SQL failure as an error result
            out.append(
                CheckResult(
                    check_id,
                    t.name,
                    "custom",
                    Status.ERROR,
                    f"{c.name}: query failed: {_short(exc)}",
                )
            )
            continue
        problems = []
        if value is None:
            problems.append("query returned no value")
        else:
            if c.equals is not None and value != c.equals:
                problems.append(f"expected {c.equals:g}")
            if c.min is not None and value < c.min:
                problems.append(f"expected at least {c.min:g}")
            if c.max is not None and value > c.max:
                problems.append(f"expected at most {c.max:g}")
            ctx.history.add_metric(ctx.run_id, check_id, "value", value, ctx.now)
        shown = "null" if value is None else f"{value:g}"
        status = _sev(c.severity) if problems else Status.PASS
        msg = f"{c.name} = {shown}" + (f" ({'; '.join(problems)})" if problems else "")
        expected = {"equals": c.equals, "min": c.min, "max": c.max}
        out.append(
            CheckResult(
                check_id,
                t.name,
                "custom",
                status,
                msg,
                value,
                {k: v for k, v in expected.items() if v is not None},
            )
        )
    return out


def _short(exc: Exception) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return text[:300]


def run_table(ctx: Context, t: TableConfig) -> list[CheckResult]:
    """Run every configured check on one table. A failure in one check never stops the rest."""
    plan = []
    if t.schema:
        plan.append(("schema", lambda: [schema(ctx, t)]))
    if t.freshness:
        plan.append(("freshness", lambda: [freshness(ctx, t)]))
    if t.row_count:
        plan.append(("row_count", lambda: [row_count(ctx, t)]))
    if t.nulls:
        plan.append(("nulls", lambda: nulls(ctx, t)))
    if t.custom:
        plan.append(("custom", lambda: custom(ctx, t)))

    results: list[CheckResult] = []
    for kind, fn in plan:
        try:
            results.extend(fn())
        except Exception as exc:  # noqa: BLE001
            ids = (
                [f"{t.name}:nulls:{c.column}" for c in t.nulls]
                if kind == "nulls"
                else [f"{t.name}:{kind}"]
            )
            for check_id in ids:
                results.append(
                    CheckResult(
                        check_id, t.name, kind, Status.ERROR, f"check could not run: {_short(exc)}"
                    )
                )
    return results

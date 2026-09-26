"""tablesentry command line.

tablesentry init                 write a starter tablesentry.yml
tablesentry validate             check the config and that every table is reachable
tablesentry run                  run checks, record history, send alerts
tablesentry history              recent runs and each check's status over time
tablesentry report -o out.html   HTML report of the latest run
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

TEMPLATE = """\
# tablesentry configuration. Docs: https://github.com/OWNER/tablesentry
version: 1

connections:
  # Any SQLAlchemy URL. Keep secrets in environment variables.
  warehouse: "${WAREHOUSE_URL:-duckdb:///warehouse.duckdb}"

# How to read timestamps that have no timezone
timezone: UTC

# Where run history lives (needed for anomaly detection and schema drift)
state: .tablesentry/state.db

tables:
  - name: analytics.orders
    checks:
      freshness:
        column: updated_at
        warn_after: 6h
        max_age: 26h
      row_count:
        min: 1
        max_change_pct: 50     # vs the median of the last 7 runs
        anomaly: true          # learn normal volume, warn on outliers
      nulls:
        customer_id: {max_pct: 5, anomaly: true}
        order_id: {max_pct: 0}
      schema: {}               # alert when columns are removed, added or retyped
      custom:
        - name: no_negative_totals
          sql: "select count(*) from {table} where total < 0"
          equals: 0

alerts:
  - type: console
  # - type: slack
  #   webhook_url: ${SLACK_WEBHOOK_URL}
  #   on: [fail, error]
  # - type: email
  #   smtp_host: smtp.example.com
  #   username: ${SMTP_USER}
  #   password: ${SMTP_PASSWORD}
  #   from: alerts@example.com
  #   to: [data-team@example.com]

policy:
  repeat_after: 24h        # re-alert for problems that are still open after this long
  notify_resolved: true
"""

EXIT_ON = {"never": 99, "warn": 1, "fail": 2, "error": 3}
SYMBOL = {"pass": "✓", "warn": "!", "fail": "✗", "error": "E"}


def _load(path: str):
    from .config import ConfigError, load_config

    try:
        return load_config(path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        sys.exit(2)


def cmd_init(args) -> int:
    path = Path(args.config)
    if path.exists() and not args.force:
        print(f"{path} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    path.write_text(TEMPLATE)
    print(f"wrote {path}. Edit the connection and tables, then run `tablesentry validate`.")
    return 0


def cmd_validate(args) -> int:
    from .warehouse import Warehouse

    cfg = _load(args.config)
    ok = True
    for name, url in cfg.connections.items():
        try:
            wh = Warehouse(url)
            wh.scalar("select 1")
            print(f"✓ connection {name}")
        except Exception as exc:  # noqa: BLE001
            print(f"✗ connection {name}: {exc}")
            ok = False
            continue
        for t in cfg.tables:
            if t.connection != name:
                continue
            try:
                cols = {c.name.lower() for c in wh.columns(t.name)}
                wanted = [c.column for c in t.nulls] + ([t.freshness.column] if t.freshness else [])
                missing = [c for c in wanted if c.lower() not in cols]
                if missing:
                    print(f"✗ {t.name}: column(s) not found: {', '.join(missing)}")
                    ok = False
                else:
                    print(f"✓ {t.name} ({len(cols)} columns)")
            except Exception as exc:  # noqa: BLE001
                print(f"✗ {t.name}: {exc}")
                ok = False
        wh.close()
    print(
        f"{len(cfg.tables)} tables, {len(cfg.alerts)} alert destination(s), "
        f"state at {cfg.state_path}"
    )
    return 0 if ok else 1


def cmd_run(args) -> int:
    from .report import write
    from .runner import run

    cfg = _load(args.config)
    now = datetime.fromisoformat(args.as_of) if args.as_of else None
    if now and now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    report = run(cfg, now=now, tables=args.table, notify=not args.no_alerts)

    if not args.quiet:
        width = max((len(r.check_id) for r in report.results), default=20)
        for r in report.results:
            print(f"{SYMBOL[r.status.value]} {r.status.value:5} {r.check_id:<{width}}  {r.message}")
        c = report.counts()
        print(
            f"\n{len(report.results)} checks: {c['pass']} passed, {c['warn']} warnings, "
            f"{c['fail']} failed, {c['error']} errors · {len(report.events)} alert(s)"
        )
    if args.json:
        Path(args.json).write_text(json.dumps(report.to_dict(), indent=2, default=str))
    if args.report:
        write(cfg.state_path, args.report)
    for err in report.delivery_errors:
        print(f"alert delivery failed: {err}", file=sys.stderr)
    return 1 if report.status.rank >= EXIT_ON[args.fail_on] else 0


def cmd_history(args) -> int:
    from .history import History

    cfg = _load(args.config)
    h = History(cfg.state_path)
    runs = h.runs(limit=args.limit)
    if not runs:
        print("no runs recorded yet")
        return 0
    print(f"last {len(runs)} runs, newest first:")
    for r in runs:
        print(f"  {r['started_at'][:16]}  {r['run_id']}  {r['status']}")
    print("\nper check (oldest -> newest):")
    for res in h.results(runs[0]["run_id"]):
        if args.table and res["table"] not in args.table:
            continue
        strip = "".join(SYMBOL[s] for _, s in h.status_history(res["check_id"], args.limit))
        print(f"  {strip:>{args.limit}}  {res['check_id']}")
    h.close()
    return 0


def cmd_report(args) -> int:
    from .report import write

    cfg = _load(args.config)
    print(f"wrote {write(cfg.state_path, args.output, args.title)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="tablesentry",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--version", action="version", version=f"tablesentry {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.add_argument("-c", "--config", default="tablesentry.yml")
        p.set_defaults(fn=fn)
        return p

    p = add("init", cmd_init, "write a starter config")
    p.add_argument("--force", action="store_true")
    add("validate", cmd_validate, "check config, connections, tables and columns")

    p = add("run", cmd_run, "run checks and send alerts")
    p.add_argument("-t", "--table", action="append", help="only this table (repeatable)")
    p.add_argument("--no-alerts", action="store_true", help="record results but send nothing")
    p.add_argument("--json", metavar="PATH", help="also write results as JSON")
    p.add_argument("--report", metavar="PATH", help="also write an HTML report")
    p.add_argument(
        "--fail-on",
        choices=list(EXIT_ON),
        default="fail",
        help="exit non-zero at this status or worse (default: fail)",
    )
    p.add_argument("--as-of", help=argparse.SUPPRESS)  # fixed clock, for demos and tests
    p.add_argument("-q", "--quiet", action="store_true")

    p = add("history", cmd_history, "show recent runs")
    p.add_argument("-t", "--table", action="append")
    p.add_argument("--limit", type=int, default=20)

    p = add("report", cmd_report, "write an HTML report of the latest run")
    p.add_argument("-o", "--output", default="tablesentry-report.html")
    p.add_argument("--title", default="Data quality report")

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

"""Run every check in a config, record history, and send alerts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import alerts as alerting
from .checks import Context, run_table
from .config import Config
from .history import History
from .models import CheckResult, Status
from .warehouse import Warehouse


@dataclass
class RunReport:
    run_id: str
    started_at: datetime
    results: list[CheckResult]
    events: list[alerting.AlertEvent] = field(default_factory=list)
    delivery_errors: list[str] = field(default_factory=list)

    @property
    def status(self) -> Status:
        return max((r.status for r in self.results), key=lambda s: s.rank, default=Status.PASS)

    def counts(self) -> dict[str, int]:
        out = {s.value: 0 for s in Status}
        for r in self.results:
            out[r.status.value] += 1
        return out

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "status": self.status.value,
            "counts": self.counts(),
            "results": [r.to_dict() for r in self.results],
            "alerts": [e.to_dict() for e in self.events],
            "delivery_errors": self.delivery_errors,
        }


def run(
    config: Config,
    now: datetime | None = None,
    tables: list[str] | None = None,
    notify: bool = True,
    warehouses: dict[str, Warehouse] | None = None,
) -> RunReport:
    now = now or datetime.now(timezone.utc)
    history = History(config.state_path)
    owned = warehouses is None
    warehouses = warehouses or {}
    try:
        run_id = history.start_run(now)
        results: list[CheckResult] = []
        for t in config.tables:
            if tables and t.name not in tables:
                continue
            if t.connection not in warehouses:
                warehouses[t.connection] = Warehouse(config.connections[t.connection])
            ctx = Context(
                warehouses[t.connection], history, run_id, now, config.timezone, config.anomaly
            )
            results.extend(run_table(ctx, t))

        for r in results:
            history.record(run_id, r)
        report = RunReport(run_id, now, results)
        # notify=False mutes delivery but still records the run and alert state, so
        # muted problems (e.g. while building a first baseline) are not re-sent later.
        report.events = alerting.evaluate(results, history, config.policy, now)
        history.finish_run(run_id, now, report.status.value)
        if notify and report.events:
            label = f"Run {run_id} at {now:%Y-%m-%d %H:%M} UTC"
            report.delivery_errors = alerting.dispatch(report.events, config.alerts, label)
        return report
    finally:
        history.close()
        if owned:
            for wh in warehouses.values():
                wh.close()

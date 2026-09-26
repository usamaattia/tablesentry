"""Result types shared by checks, the runner, alerting and reports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Status(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    ERROR = "error"  # the check itself could not run (bad SQL, table missing, ...)

    @property
    def rank(self) -> int:
        return {"pass": 0, "warn": 1, "fail": 2, "error": 3}[self.value]

    @property
    def is_problem(self) -> bool:
        return self is not Status.PASS


@dataclass
class CheckResult:
    check_id: str  # stable id, e.g. "marts.fct_orders:freshness"
    table: str
    kind: str
    status: Status
    message: str
    observed: Any = None
    expected: Any = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

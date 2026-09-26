"""Load and validate ``tablesentry.yml``.

Errors point at the exact key that is wrong (``tables[1].checks.freshness.max_age``), and
``${ENV_VAR}`` / ``${ENV_VAR:-default}`` references are expanded so secrets stay out of the
file.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

SEVERITIES = ("warn", "fail")
ALERT_TYPES = ("console", "slack", "email", "webhook")


class ConfigError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Small parsers
# ---------------------------------------------------------------------------

_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*([smhdw])")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_duration(value: Any, where: str) -> timedelta:
    """'90s', '30m', '26h', '2d', '1w', '1h30m' -> timedelta."""
    if isinstance(value, int | float):
        return timedelta(seconds=value)
    text = str(value).strip().lower().replace(" ", "")
    parts = _DURATION.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise ConfigError(f"{where}: invalid duration {value!r} (use e.g. 30m, 26h, 2d)")
    total = timedelta()
    for number, unit in parts:
        total += timedelta(**{_UNITS[unit]: float(number)})
    return total


_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


def expand_env(obj: Any, where: str = "config") -> Any:
    if isinstance(obj, dict):
        return {k: expand_env(v, f"{where}.{k}") for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env(v, f"{where}[{i}]") for i, v in enumerate(obj)]
    if isinstance(obj, str):

        def sub(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(f"{where}: environment variable ${{{name}}} is not set")

        return _ENV.sub(sub, obj)
    return obj


def _severity(value: Any, where: str, default: str = "fail") -> str:
    value = default if value is None else str(value)
    if value not in SEVERITIES:
        raise ConfigError(f"{where}: severity must be one of {SEVERITIES}, got {value!r}")
    return value


def _anomaly(value: Any, where: str) -> str | None:
    """anomaly: true -> 'warn'; 'warn' / 'fail'; false / missing -> None."""
    if value in (None, False):
        return None
    if value is True:
        return "warn"
    return _severity(value, where)


def _number(block: dict, key: str, where: str) -> float | None:
    if key not in block or block[key] is None:
        return None
    try:
        return float(block[key])
    except (TypeError, ValueError):
        raise ConfigError(f"{where}.{key}: expected a number, got {block[key]!r}") from None


def _check_keys(block: dict, allowed: set[str], where: str) -> None:
    unknown = set(block) - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)}; allowed: {sorted(allowed)}")


# ---------------------------------------------------------------------------
# Config objects
# ---------------------------------------------------------------------------


@dataclass
class AnomalySettings:
    sensitivity: float = 4.0  # robust z-score threshold
    min_history: int = 7  # observations needed before anomaly checks start
    window: int = 30  # how many past observations to compare against


@dataclass
class FreshnessCheck:
    column: str
    max_age: timedelta
    warn_after: timedelta | None = None


@dataclass
class RowCountCheck:
    min: float | None = None
    max: float | None = None
    max_change_pct: float | None = None  # vs the median of the last 7 runs
    anomaly: str | None = None
    severity: str = "fail"


@dataclass
class NullCheck:
    column: str
    max_pct: float | None = None
    anomaly: str | None = None
    severity: str = "fail"


@dataclass
class SchemaCheck:
    allow_added: bool = False
    severity: str = "fail"


@dataclass
class CustomCheck:
    name: str
    sql: str
    min: float | None = None
    max: float | None = None
    equals: float | None = None
    severity: str = "fail"


@dataclass
class TableConfig:
    name: str
    connection: str
    where: str | None = None
    freshness: FreshnessCheck | None = None
    row_count: RowCountCheck | None = None
    nulls: list[NullCheck] = field(default_factory=list)
    schema: SchemaCheck | None = None
    custom: list[CustomCheck] = field(default_factory=list)


@dataclass
class AlertConfig:
    type: str
    on: list[str]
    options: dict[str, Any]


@dataclass
class Policy:
    repeat_after: timedelta = timedelta(hours=24)
    notify_resolved: bool = True


@dataclass
class Config:
    connections: dict[str, str]
    tables: list[TableConfig]
    alerts: list[AlertConfig]
    policy: Policy
    anomaly: AnomalySettings
    state_path: Path
    timezone: str = "UTC"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_table(raw: dict, i: int, connections: dict[str, str], default_conn: str | None):
    where = f"tables[{i}]"
    if not isinstance(raw, dict) or "name" not in raw:
        raise ConfigError(f"{where}: each table needs a 'name'")
    _check_keys(raw, {"name", "connection", "where", "checks"}, where)
    conn = raw.get("connection", default_conn)
    if conn not in connections:
        raise ConfigError(f"{where}.connection: unknown connection {conn!r}")
    t = TableConfig(name=str(raw["name"]), connection=conn, where=raw.get("where"))
    checks = raw.get("checks") or {}
    _check_keys(checks, {"freshness", "row_count", "nulls", "schema", "custom"}, f"{where}.checks")
    w = f"{where}.checks"

    if (b := checks.get("freshness")) is not None:
        _check_keys(b, {"column", "max_age", "warn_after"}, f"{w}.freshness")
        if "column" not in b or "max_age" not in b:
            raise ConfigError(f"{w}.freshness: needs 'column' and 'max_age'")
        t.freshness = FreshnessCheck(
            column=b["column"],
            max_age=parse_duration(b["max_age"], f"{w}.freshness.max_age"),
            warn_after=parse_duration(b["warn_after"], f"{w}.freshness.warn_after")
            if b.get("warn_after")
            else None,
        )

    if (b := checks.get("row_count")) is not None:
        b = b or {}
        _check_keys(b, {"min", "max", "max_change_pct", "anomaly", "severity"}, f"{w}.row_count")
        t.row_count = RowCountCheck(
            min=_number(b, "min", f"{w}.row_count"),
            max=_number(b, "max", f"{w}.row_count"),
            max_change_pct=_number(b, "max_change_pct", f"{w}.row_count"),
            anomaly=_anomaly(b.get("anomaly"), f"{w}.row_count.anomaly"),
            severity=_severity(b.get("severity"), f"{w}.row_count.severity"),
        )

    for col, b in (checks.get("nulls") or {}).items():
        b = b or {}
        cw = f"{w}.nulls.{col}"
        _check_keys(b, {"max_pct", "anomaly", "severity"}, cw)
        t.nulls.append(
            NullCheck(
                column=col,
                max_pct=_number(b, "max_pct", cw),
                anomaly=_anomaly(b.get("anomaly"), f"{cw}.anomaly"),
                severity=_severity(b.get("severity"), f"{cw}.severity"),
            )
        )

    if (b := checks.get("schema")) is not None:
        b = b or {}
        _check_keys(b, {"allow_added", "severity"}, f"{w}.schema")
        t.schema = SchemaCheck(
            allow_added=bool(b.get("allow_added", False)),
            severity=_severity(b.get("severity"), f"{w}.schema.severity"),
        )

    for j, b in enumerate(checks.get("custom") or []):
        cw = f"{w}.custom[{j}]"
        _check_keys(b, {"name", "sql", "min", "max", "equals", "severity"}, cw)
        if "name" not in b or "sql" not in b:
            raise ConfigError(f"{cw}: needs 'name' and 'sql'")
        c = CustomCheck(
            name=b["name"],
            sql=b["sql"],
            min=_number(b, "min", cw),
            max=_number(b, "max", cw),
            equals=_number(b, "equals", cw),
            severity=_severity(b.get("severity"), f"{cw}.severity"),
        )
        if c.min is None and c.max is None and c.equals is None:
            raise ConfigError(f"{cw}: give at least one of min, max, equals")
        t.custom.append(c)

    if not any([t.freshness, t.row_count, t.nulls, t.schema, t.custom]):
        raise ConfigError(f"{where}: no checks configured")
    return t


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path} (create one with `tablesentry init`)")
    raw = yaml.safe_load(path.read_text()) or {}
    return parse_config(raw, base_dir=path.parent)


def parse_config(raw: dict, base_dir: Path = Path(".")) -> Config:
    raw = expand_env(raw)
    _check_keys(
        raw,
        {"version", "state", "timezone", "connections", "defaults", "tables", "alerts", "policy"},
        "config",
    )
    connections = raw.get("connections") or {}
    if not connections:
        raise ConfigError("config.connections: define at least one connection")
    connections = {k: str(v) for k, v in connections.items()}

    defaults = raw.get("defaults") or {}
    _check_keys(defaults, {"connection", "anomaly"}, "config.defaults")
    default_conn = defaults.get("connection") or (
        next(iter(connections)) if len(connections) == 1 else None
    )
    a = defaults.get("anomaly") or {}
    _check_keys(a, {"sensitivity", "min_history", "window"}, "config.defaults.anomaly")
    anomaly = AnomalySettings(
        sensitivity=float(a.get("sensitivity", 4.0)),
        min_history=int(a.get("min_history", 7)),
        window=int(a.get("window", 30)),
    )

    tables_raw = raw.get("tables") or []
    if not tables_raw:
        raise ConfigError("config.tables: list at least one table to monitor")
    tables = [_parse_table(t, i, connections, default_conn) for i, t in enumerate(tables_raw)]
    names = [t.name for t in tables]
    if len(set(names)) != len(names):
        raise ConfigError("config.tables: each table may only be listed once")

    alerts = []
    for i, b in enumerate(raw.get("alerts") or [{"type": "console"}]):
        where = f"alerts[{i}]"
        kind = b.get("type")
        if kind not in ALERT_TYPES:
            raise ConfigError(f"{where}.type: must be one of {ALERT_TYPES}, got {kind!r}")
        on = b.get("on") or ["fail", "error"]
        bad = set(on) - {"warn", "fail", "error"}
        if bad:
            raise ConfigError(f"{where}.on: unknown statuses {sorted(bad)}")
        options = {k: v for k, v in b.items() if k not in ("type", "on")}
        required = {
            "slack": ["webhook_url"],
            "webhook": ["url"],
            "email": ["smtp_host", "from", "to"],
        }.get(kind, [])
        for key in required:
            if not options.get(key):
                raise ConfigError(f"{where}.{key}: required for {kind} alerts")
        alerts.append(AlertConfig(type=kind, on=list(on), options=options))

    p = raw.get("policy") or {}
    _check_keys(p, {"repeat_after", "notify_resolved"}, "config.policy")
    policy = Policy(
        repeat_after=parse_duration(p.get("repeat_after", "24h"), "config.policy.repeat_after"),
        notify_resolved=bool(p.get("notify_resolved", True)),
    )

    state = Path(raw.get("state") or ".tablesentry/state.db")
    if not state.is_absolute():
        state = base_dir / state
    return Config(
        connections=connections,
        tables=tables,
        alerts=alerts,
        policy=policy,
        anomaly=anomaly,
        state_path=state,
        timezone=str(raw.get("timezone", "UTC")),
    )

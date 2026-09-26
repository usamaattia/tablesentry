from datetime import timedelta

import pytest
import yaml

from tablesentry.cli import TEMPLATE
from tablesentry.config import ConfigError, parse_config, parse_duration


@pytest.mark.parametrize(
    "text,expected",
    [
        ("90s", timedelta(seconds=90)),
        ("30m", timedelta(minutes=30)),
        ("26h", timedelta(hours=26)),
        ("2d", timedelta(days=2)),
        ("1w", timedelta(weeks=1)),
        ("1h30m", timedelta(minutes=90)),
        (45, timedelta(seconds=45)),
    ],
)
def test_parse_duration(text, expected):
    assert parse_duration(text, "x") == expected


@pytest.mark.parametrize("bad", ["", "abc", "10", "5 years", "1h 2x"])
def test_bad_durations_name_the_key(bad):
    with pytest.raises(ConfigError, match="policy.repeat_after"):
        parse_duration(bad, "policy.repeat_after")


def test_starter_template_is_valid(monkeypatch):
    monkeypatch.delenv("WAREHOUSE_URL", raising=False)
    cfg = parse_config(yaml.safe_load(TEMPLATE))
    t = cfg.tables[0]
    assert cfg.connections["warehouse"] == "duckdb:///warehouse.duckdb"  # ${VAR:-default}
    assert t.freshness.max_age == timedelta(hours=26)
    assert t.row_count.anomaly == "warn"  # anomaly: true -> warn
    assert [n.column for n in t.nulls] == ["customer_id", "order_id"]
    assert t.custom[0].equals == 0


def test_env_vars_are_expanded_and_missing_ones_reported(monkeypatch):
    raw = {
        "connections": {"wh": "${DB_URL}"},
        "tables": [{"name": "t", "checks": {"schema": {}}}],
        "alerts": [{"type": "slack", "webhook_url": "${HOOK}"}],
    }
    monkeypatch.setenv("DB_URL", "sqlite://")
    monkeypatch.delenv("HOOK", raising=False)
    with pytest.raises(
        ConfigError, match=r"alerts\[0\].webhook_url: environment variable \$\{HOOK\}"
    ):
        parse_config(raw)
    monkeypatch.setenv("HOOK", "https://hooks.example/1")
    cfg = parse_config(raw)
    assert cfg.connections["wh"] == "sqlite://"
    assert cfg.alerts[0].options["webhook_url"] == "https://hooks.example/1"
    assert cfg.alerts[0].on == ["fail", "error"]


@pytest.mark.parametrize(
    "tables,message",
    [
        (
            [{"name": "t", "checks": {"freshnes": {}}}],
            r"tables\[0\].checks: unknown key\(s\) \['freshnes'\]",
        ),
        ([{"name": "t", "checks": {"freshness": {"column": "x"}}}], "needs 'column' and 'max_age'"),
        (
            [{"name": "t", "checks": {"row_count": {"min": "lots"}}}],
            r"row_count.min: expected a number",
        ),
        ([{"name": "t", "checks": {"row_count": {"severity": "page"}}}], "severity must be one of"),
        (
            [{"name": "t", "checks": {"custom": [{"name": "c", "sql": "select 1"}]}}],
            "at least one of min, max, equals",
        ),
        ([{"name": "t", "checks": {}}], "no checks configured"),
        (
            [{"name": "t", "connection": "nope", "checks": {"schema": {}}}],
            "unknown connection 'nope'",
        ),
        (
            [{"name": "t", "checks": {"schema": {}}}, {"name": "t", "checks": {"schema": {}}}],
            "only be listed once",
        ),
    ],
)
def test_validation_errors_point_at_the_problem(tables, message):
    with pytest.raises(ConfigError, match=message):
        parse_config({"connections": {"wh": "sqlite://"}, "tables": tables})


def test_alert_validation():
    base = {"connections": {"wh": "sqlite://"}, "tables": [{"name": "t", "checks": {"schema": {}}}]}
    with pytest.raises(ConfigError, match="must be one of"):
        parse_config({**base, "alerts": [{"type": "pager"}]})
    with pytest.raises(ConfigError, match=r"email.*|alerts\[0\].smtp_host"):
        parse_config({**base, "alerts": [{"type": "email", "to": ["a@b.c"]}]})
    with pytest.raises(ConfigError, match="unknown statuses"):
        parse_config({**base, "alerts": [{"type": "console", "on": ["panic"]}]})

"""End-to-end through the command line, on a DuckDB file."""

import json

import duckdb
import pytest

from tablesentry.cli import main


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    con = duckdb.connect("wh.duckdb")
    con.execute("create schema shop")
    con.execute(
        "create table shop.orders as select range as id, "
        "case when range % 10 = 0 then null else range end as customer_id, "
        "timestamp '2026-09-20 10:00:00' as updated_at from range(100)"
    )
    con.close()
    (tmp_path / "tablesentry.yml").write_text("""
connections:
  wh: duckdb:///wh.duckdb
tables:
  - name: shop.orders
    checks:
      freshness: {column: updated_at, max_age: 24h}
      row_count: {min: 50}
      nulls:
        customer_id: {max_pct: 5}
      schema: {}
alerts:
  - type: console
""")
    return tmp_path


def test_init_writes_a_template(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert "tables:" in (tmp_path / "tablesentry.yml").read_text()
    assert main(["init"]) == 1  # refuses to overwrite


def test_validate(project, capsys):
    assert main(["validate"]) == 0
    assert "✓ shop.orders (3 columns)" in capsys.readouterr().out


def test_validate_reports_missing_columns(project, capsys):
    cfg = project / "tablesentry.yml"
    cfg.write_text(cfg.read_text().replace("customer_id: {max_pct: 5}", "cust: {max_pct: 5}"))
    assert main(["validate"]) == 1
    assert "column(s) not found: cust" in capsys.readouterr().out


def test_run_outputs_and_exit_codes(project, capsys):
    args = ["run", "--as-of", "2026-09-20T12:00:00", "--json", "out.json", "--report", "r.html"]
    assert main(args) == 1  # 10% nulls > 5% limit
    out = capsys.readouterr().out
    assert "✗ fail  shop.orders:nulls:customer_id" in out
    assert "1 alert(s)" in out
    data = json.loads((project / "out.json").read_text())
    assert data["status"] == "fail" and data["counts"]["pass"] == 3
    assert "shop.orders" in (project / "r.html").read_text()

    # --fail-on error: failed checks no longer fail the build
    assert main(["run", "--as-of", "2026-09-20T13:00:00", "--fail-on", "error", "-q"]) == 0


def test_config_errors_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tablesentry.yml").write_text("connections: {wh: 'sqlite://'}\ntables: []\n")
    with pytest.raises(SystemExit) as e:
        main(["run"])
    assert e.value.code == 2
    assert "list at least one table" in capsys.readouterr().err


def test_history(project, capsys):
    for hour in (12, 13, 14):
        main(["run", "--as-of", f"2026-09-20T{hour}:00:00", "-q", "--no-alerts"])
    capsys.readouterr()
    assert main(["history"]) == 0
    out = capsys.readouterr().out
    assert "last 3 runs" in out and "✗✗✗  shop.orders:nulls:customer_id" in out

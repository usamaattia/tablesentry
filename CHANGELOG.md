# Changelog

## 0.1.0 (unreleased)

First release.

- Checks: freshness (warn and fail thresholds), row count (limits, change vs recent
  median, anomaly), null rate per column (limit, anomaly), schema drift (removed, added,
  retyped columns), custom SQL assertions.
- Robust anomaly detection (median / MAD) that learns from run history.
- Alerts to Slack, email, any webhook or the console, with de-duplication, re-alert
  after a configurable period, escalation and "resolved" notifications.
- Works with any SQLAlchemy database; tested on DuckDB and PostgreSQL.
- CLI: `init`, `validate`, `run`, `history`, `report`; JSON output and HTML report.
- GitHub Action for scheduled checks with history cached between runs.

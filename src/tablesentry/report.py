"""A self-contained HTML report of the latest run, with history, from the state file.

No JavaScript and no external assets, so it works as a CI artifact, an email attachment
or a GitHub Pages site.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

from .history import History

METRIC_FOR_KIND = {
    "row_count": ("row_count", ""),
    "nulls": ("null_pct", "%"),
    "freshness": ("age_hours", "h"),
    "custom": ("value", ""),
}
COLORS = {
    "pass": "var(--pass)",
    "warn": "var(--warn)",
    "fail": "var(--fail)",
    "error": "var(--error)",
}


def _sparkline(values: list[float], w: int = 140, h: int = 28) -> str:
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    step = w / (len(values) - 1)
    pts = " ".join(
        f"{i * step:.1f},{h - 3 - (v - lo) / span * (h - 6):.1f}" for i, v in enumerate(values)
    )
    last_x, last_y = pts.split(" ")[-1].split(",")
    return (
        f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="trend">'
        f'<polyline fill="none" stroke="var(--accent)" stroke-width="1.5" points="{pts}"/>'
        f'<circle cx="{last_x}" cy="{last_y}" r="2.5" fill="var(--accent)"/></svg>'
    )


def _strip(statuses: list[str]) -> str:
    cells = "".join(
        f'<span class="cell" style="background:{COLORS.get(s, "var(--muted)")}" title="{s}"></span>'
        for s in statuses[-30:]
    )
    return f'<span class="strip">{cells}</span>'


def render(state_path: str | Path, title: str = "Data quality report") -> str:
    h = History(state_path)
    try:
        runs = h.runs(limit=1)
        if not runs:
            return "<p>No runs recorded yet.</p>"
        run = runs[0]
        results = h.results(run["run_id"])
        counts: dict[str, int] = {}
        for r in results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        rows = []
        order = {"error": 0, "fail": 1, "warn": 2, "pass": 3}
        for r in sorted(results, key=lambda r: (order[r["status"]], r["table"], r["check_id"])):
            metric, unit = METRIC_FOR_KIND.get(r["kind"], (None, ""))
            series = [v for _, v in h.metric_series(r["check_id"], metric)] if metric else []
            strip = [s for _, s in h.status_history(r["check_id"])]
            name = r["check_id"].split(":", 1)[1].replace(":", " · ")
            last = f"{series[-1]:,.4g}{unit}" if series else ""
            rows.append(
                f"<tr><td><span class='pill {r['status']}'>{r['status']}</span></td>"
                f"<td><b>{escape(r['table'])}</b><br><span class='muted'>{escape(name)}</span></td>"
                f"<td>{escape(r['message'] or '')}</td>"
                f"<td class='spark'>{_sparkline(series)}<span class='muted'>{last}</span></td>"
                f"<td>{_strip(strip)}</td></tr>"
            )
        summary = " · ".join(
            f"<span class='pill {k}'>{v} {k}</span>"
            for k, v in sorted(counts.items(), key=lambda kv: order[kv[0]])
        )
    finally:
        h.close()

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>
:root {{ --bg:#f7f7f5; --panel:#fff; --ink:#1c1e21; --muted:#6b7079; --line:#e5e4e0;
  --pass:#2f9e6b; --warn:#d99a1e; --fail:#d9483b; --error:#8e44ad; --accent:#3a6fd8; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#121417; --panel:#1a1d21; --ink:#e9e7e2;
  --muted:#9aa0a8; --line:#2b2f35; --accent:#7ea4ff; }} }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:14px/1.5 system-ui, sans-serif; }}
.wrap {{ max-width:1100px; margin:0 auto; padding:28px 16px; }}
h1 {{ margin:0 0 4px; font-size:24px; }} .muted {{ color:var(--muted); font-size:12px; }}
.summary {{ margin:14px 0 20px; }}
.pill {{ display:inline-block; padding:1px 9px; border-radius:99px; font-size:12px; font-weight:600;
  color:#fff; }}
.pill.pass {{ background:var(--pass); }} .pill.warn {{ background:var(--warn); }}
.pill.fail {{ background:var(--fail); }} .pill.error {{ background:var(--error); }}
.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; }}
th, td {{ padding:10px 12px; border-bottom:1px solid var(--line); text-align:left; vertical-align:middle; }}
th {{ font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); font-weight:600; }}
td.spark {{ white-space:nowrap; }} td.spark svg {{ vertical-align:middle; margin-right:6px; }}
.strip {{ display:inline-flex; gap:2px; }} .cell {{ width:6px; height:16px; border-radius:1px; }}
</style></head><body><div class="wrap">
<h1>{escape(title)}</h1>
<div class="muted">Run {escape(run["run_id"])} at {escape(run["started_at"][:16].replace("T", " "))} UTC</div>
<div class="summary">{summary}</div>
<div class="panel"><table>
<tr><th>Status</th><th>Check</th><th>Detail</th><th>Trend</th><th>Last 30 runs</th></tr>
{"".join(rows)}
</table></div>
<p class="muted">Generated by tablesentry.</p>
</div></body></html>"""


def write(state_path: str | Path, out: str | Path, title: str = "Data quality report") -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(state_path, title))
    return out

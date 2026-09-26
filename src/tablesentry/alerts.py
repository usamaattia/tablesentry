"""Decide *when* to alert, then deliver to Slack, email, a webhook or the console.

Alert policy (per check):
    pass -> problem          "new"       notify
    problem -> other problem "changed"   notify (e.g. warn escalated to fail)
    problem -> same problem  "ongoing"   notify again only after policy.repeat_after
    problem -> pass          "resolved"  notify if policy.notify_resolved
This keeps a failing check from paging someone on every run of a 15-minute schedule.
"""

from __future__ import annotations

import json
import logging
import smtplib
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from html import escape

from .config import AlertConfig, Policy
from .history import AlertState, History
from .models import CheckResult

log = logging.getLogger(__name__)

ICONS = {
    "fail": ":red_circle:",
    "error": ":boom:",
    "warn": ":large_yellow_circle:",
    "resolved": ":white_check_mark:",
}


@dataclass
class AlertEvent:
    kind: str  # new | changed | ongoing | resolved
    result: CheckResult
    previous_status: str | None = None
    since: datetime | None = None

    @property
    def status_for_routing(self) -> str:
        """Which status decides who hears about it (resolved -> the status it recovered from)."""
        return self.previous_status if self.kind == "resolved" else self.result.status.value

    def title(self) -> str:
        r = self.result
        if self.kind == "resolved":
            return f"RESOLVED {r.check_id}"
        return f"{r.status.value.upper()} {r.check_id}"

    def to_dict(self) -> dict:
        return {
            "event": self.kind,
            "previous_status": self.previous_status,
            "since": self.since.isoformat() if self.since else None,
            **self.result.to_dict(),
        }


def evaluate(
    results: list[CheckResult],
    history: History,
    policy: Policy,
    now: datetime,
    persist: bool = True,
) -> list[AlertEvent]:
    events = []
    save = history.set_alert_state if persist else (lambda state: None)
    for r in results:
        prev = history.get_alert_state(r.check_id)
        status = r.status.value
        if prev is None:
            state = AlertState(r.check_id, status, now, None, None)
            if r.status.is_problem:
                events.append(AlertEvent("new", r, None, now))
                state.last_notified_at, state.last_notified_status = now, status
            save(state)
            continue

        was_problem = prev.status != "pass"
        if not r.status.is_problem:
            if was_problem and policy.notify_resolved and prev.last_notified_at:
                events.append(AlertEvent("resolved", r, prev.status, prev.since))
            save(AlertState(r.check_id, status, now if was_problem else prev.since, None, None))
            continue

        if not was_problem:
            events.append(AlertEvent("new", r, prev.status, now))
            save(AlertState(r.check_id, status, now, now, status))
        elif prev.status != status:
            events.append(AlertEvent("changed", r, prev.status, prev.since))
            save(AlertState(r.check_id, status, prev.since, now, status))
        elif prev.last_notified_at is None or now - prev.last_notified_at >= policy.repeat_after:
            events.append(AlertEvent("ongoing", r, prev.status, prev.since))
            save(AlertState(r.check_id, status, prev.since, now, status))
        else:
            save(prev)  # still failing, already told people recently
    return events


# ---------------------------------------------------------------------------
# Notifiers
# ---------------------------------------------------------------------------


def _post_json(url: str, payload: dict, timeout: float = 10) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - user-configured URL
        if resp.status >= 300:
            raise RuntimeError(f"HTTP {resp.status}")


def _summary(events: list[AlertEvent]) -> str:
    counts: dict[str, int] = {}
    for e in events:
        key = "resolved" if e.kind == "resolved" else "problem"
        counts[key] = counts.get(key, 0) + 1
    parts = []
    if counts.get("problem"):
        n = counts["problem"]
        parts.append(f"{n} problem{'s' if n != 1 else ''}")
    if counts.get("resolved"):
        parts.append(f"{counts['resolved']} resolved")
    return ", ".join(parts)


def slack_payload(events: list[AlertEvent], run_label: str) -> dict:
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"tablesentry: {_summary(events)}"},
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": run_label}]},
    ]
    for e in events[:45]:  # Slack allows 50 blocks per message
        r = e.result
        icon = ICONS["resolved"] if e.kind == "resolved" else ICONS[r.status.value]
        label = {
            "new": "",
            "changed": f" (was {e.previous_status})",
            "ongoing": " (still failing)",
            "resolved": "",
        }[e.kind]
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{icon} *{r.table}* · `{r.kind}`{label}\n{r.message}"[:2900],
                },
            }
        )
    if len(events) > 45:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"...and {len(events) - 45} more"}],
            }
        )
    return {"text": f"tablesentry: {_summary(events)}", "blocks": blocks}


def _email(events: list[AlertEvent], opts: dict, run_label: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = f"[tablesentry] {_summary(events)}"
    msg["From"] = opts["from"]
    to = opts["to"] if isinstance(opts["to"], list) else [opts["to"]]
    msg["To"] = ", ".join(to)
    lines = [run_label, ""] + [f"- {e.title()}: {e.result.message}" for e in events]
    msg.set_content("\n".join(lines))
    rows = "".join(
        f"<tr><td>{escape('resolved' if e.kind == 'resolved' else e.result.status.value)}</td>"
        f"<td>{escape(e.result.table)}</td><td>{escape(e.result.kind)}</td>"
        f"<td>{escape(e.result.message)}</td></tr>"
        for e in events
    )
    msg.add_alternative(
        f"<p>{escape(run_label)}</p><table border='1' cellpadding='4' cellspacing='0'>"
        f"<tr><th>Status</th><th>Table</th><th>Check</th><th>Detail</th></tr>{rows}</table>",
        subtype="html",
    )
    port = int(opts.get("smtp_port", 587))
    cls = smtplib.SMTP_SSL if opts.get("ssl") or port == 465 else smtplib.SMTP
    with cls(opts["smtp_host"], port, timeout=15) as smtp:
        if cls is smtplib.SMTP and opts.get("starttls", True):
            smtp.starttls()
        if opts.get("username"):
            smtp.login(opts["username"], opts.get("password", ""))
        smtp.send_message(msg)


def _console(events: list[AlertEvent], run_label: str) -> None:
    print(f"\n{run_label}: {_summary(events)}")
    for e in events:
        print(f"  {e.title():60} {e.result.message}")


def dispatch(events: list[AlertEvent], alerts: list[AlertConfig], run_label: str) -> list[str]:
    """Send events to every configured destination. Returns delivery errors (never raises)."""
    errors = []
    for a in alerts:
        mine = [e for e in events if e.status_for_routing in a.on]
        if not mine:
            continue
        try:
            if a.type == "console":
                _console(mine, run_label)
            elif a.type == "slack":
                _post_json(a.options["webhook_url"], slack_payload(mine, run_label))
            elif a.type == "webhook":
                _post_json(
                    a.options["url"], {"run": run_label, "events": [e.to_dict() for e in mine]}
                )
            elif a.type == "email":
                _email(mine, a.options, run_label)
        except Exception as exc:  # noqa: BLE001 - a broken channel must not break the run
            log.error("could not deliver %s alert: %s", a.type, exc)
            errors.append(f"{a.type}: {exc}")
    return errors

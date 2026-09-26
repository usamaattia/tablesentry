import json
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from conftest import NOW

from tablesentry import alerts
from tablesentry.config import AlertConfig, Policy
from tablesentry.models import CheckResult, Status


def res(status: Status, check="t:row_count", msg="msg") -> CheckResult:
    return CheckResult(check, "t", "row_count", status, msg)


def run_sequence(history, statuses, policy=None, step=timedelta(hours=1)):
    policy = policy or Policy()
    out = []
    for i, s in enumerate(statuses):
        events = alerts.evaluate([res(s)], history, policy, NOW + i * step)
        out.append([e.kind for e in events])
    return out


def test_alert_lifecycle(history):
    P, W, F = Status.PASS, Status.WARN, Status.FAIL
    assert run_sequence(history, [P, P, F, F, F, W, P, P]) == [
        [],
        [],
        ["new"],
        [],
        [],
        ["changed"],
        ["resolved"],
        [],
    ]


def test_first_run_problem_is_new(history):
    assert run_sequence(history, [Status.FAIL]) == [["new"]]


def test_repeat_after(history):
    policy = Policy(repeat_after=timedelta(hours=3))
    kinds = run_sequence(history, [Status.FAIL] * 7, policy)
    assert kinds == [["new"], [], [], ["ongoing"], [], [], ["ongoing"]]


def test_resolved_can_be_switched_off(history):
    policy = Policy(notify_resolved=False)
    assert run_sequence(history, [Status.FAIL, Status.PASS], policy) == [["new"], []]


def test_routing_by_status(capsys):
    events = [
        alerts.AlertEvent("new", res(Status.WARN, "a:x")),
        alerts.AlertEvent("new", res(Status.FAIL, "b:x")),
        alerts.AlertEvent("resolved", res(Status.PASS, "c:x"), previous_status="fail"),
    ]
    errors = alerts.dispatch(events, [AlertConfig("console", ["fail"], {})], "Run 1")
    out = capsys.readouterr().out
    assert errors == [] and "b:x" in out and "RESOLVED c:x" in out and "a:x" not in out


@pytest.fixture
def webhook_server():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append(json.loads(body))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/hook", received
    server.shutdown()


def test_slack_message(webhook_server):
    url, received = webhook_server
    events = [
        alerts.AlertEvent("new", res(Status.FAIL, "shop.orders:row_count", "40 rows, below 100")),
        alerts.AlertEvent("resolved", res(Status.PASS, "shop.orders:nulls:x"), "warn"),
    ]
    assert (
        alerts.dispatch(
            events, [AlertConfig("slack", ["fail", "warn"], {"webhook_url": url})], "Run 9"
        )
        == []
    )
    (msg,) = received
    assert msg["text"] == "tablesentry: 1 problem, 1 resolved"
    sections = [b["text"]["text"] for b in msg["blocks"] if b["type"] == "section"]
    assert ":red_circle:" in sections[0] and "40 rows, below 100" in sections[0]
    assert ":white_check_mark:" in sections[1]


def test_generic_webhook(webhook_server):
    url, received = webhook_server
    alerts.dispatch(
        [alerts.AlertEvent("new", res(Status.ERROR))],
        [AlertConfig("webhook", ["error"], {"url": url})],
        "Run 2",
    )
    assert received[0]["events"][0]["status"] == "error"
    assert received[0]["events"][0]["event"] == "new"


def test_email(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["server"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def starttls(self):
            sent["tls"] = True

        def login(self, user, pw):
            sent["login"] = user

        def send_message(self, msg):
            sent["msg"] = msg

    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    opts = {
        "smtp_host": "smtp.test",
        "from": "dq@test",
        "to": ["team@test"],
        "username": "u",
        "password": "p",
    }
    alerts.dispatch(
        [alerts.AlertEvent("new", res(Status.FAIL))],
        [AlertConfig("email", ["fail"], opts)],
        "Run 3",
    )
    assert sent["server"] == ("smtp.test", 587) and sent["tls"] and sent["login"] == "u"
    assert sent["msg"]["Subject"] == "[tablesentry] 1 problem"
    assert sent["msg"]["To"] == "team@test"


def test_broken_channel_does_not_raise():
    errors = alerts.dispatch(
        [alerts.AlertEvent("new", res(Status.FAIL))],
        [AlertConfig("webhook", ["fail"], {"url": "http://127.0.0.1:9/nothing"})],
        "Run",
    )
    assert len(errors) == 1 and errors[0].startswith("webhook:")

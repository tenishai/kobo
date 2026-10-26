"""The Activity section: the gateway's audit rows for one agent, shaped for
the table, and a plain sentence when the gateway cannot be asked."""

from __future__ import annotations

from kobo_api.clients import ClientError
from tests.test_web_learnings import (  # noqa: F401
    AGENT_NAME,
    _make_client,
    _seed_agent,
    config,
    session_factory,
)


class FakeAudit:
    def __init__(self, rows, *, fail: str | None = None):
        self.rows, self._fail, self.asked = rows, fail, []

    def audit(self, *, persona: str, since=None, limit=200):
        self.asked.append((persona, limit))
        if self._fail:
            raise ClientError(self._fail)
        return self.rows


ROWS = [
    {"ts": 1700000000.0, "action": "access_allow", "persona": AGENT_NAME, "agent": f"{AGENT_NAME}-primus",
     "detail": {"kind": "call", "handler": "aws", "tool": "aws.call", "args": "action=DescribeInstances service=ec2", "outcome": "ok", "ms": 412}},
    {"ts": 1699999000.0, "action": "access_deny", "persona": AGENT_NAME, "agent": None,
     "detail": {"handler": "github", "tool": "github.read_file", "required_scope": "github:read"}},
    {"ts": 1699998000.0, "action": "grant", "persona": AGENT_NAME, "agent": None, "detail": {}},
]


def test_activity_shapes_calls_and_refusals_for_the_table(session_factory, config):  # noqa: F811
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    fake = FakeAudit(ROWS)
    client.app.state.clients.liminal_admin = fake
    body = client.get(f"/api/agents/{AGENT_NAME}/activity?days=30").json()
    assert body["available"] is True and fake.asked == [(AGENT_NAME, 500)]
    assert [r["outcome"] for r in body["rows"]] == ["ok", "denied"], "grants are not activity"
    call, deny = body["rows"]
    assert call == {"ts": 1700000000.0, "handler": "aws", "tool": "aws.call", "outcome": "ok", "args": "action=DescribeInstances service=ec2", "ms": 412, "detail": ""}
    assert deny["tool"] == "github.read_file" and deny["detail"] == "needs github:read" and deny["ms"] is None


def test_activity_says_why_when_the_gateway_cannot_be_asked(session_factory, config):  # noqa: F811
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    body = client.get(f"/api/agents/{AGENT_NAME}/activity").json()
    assert body["available"] is False and "admin plane" in body["note"] and body["rows"] == []
    client.app.state.clients.liminal_admin = FakeAudit([], fail="gateway admin plane unreachable: ConnectError")
    body = client.get(f"/api/agents/{AGENT_NAME}/activity").json()
    assert body["available"] is False and "unreachable" in body["note"]

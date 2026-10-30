"""Wire shapes the room sends to other products.

The requests kobo's real clients put on the wire to gojira, cabal, sero,
GitHub and Slack, and the task envelopes an agent reads: the one place the
fake and the real client must agree. Folded on 2026-10-29 from
test_clients.py, test_sero.py, test_github_app.py and test_slack_setup.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kobo_api.clients import (
    CabalClient,
    ChatMessage,
    ClientError,
    GhCLI,
    HttpxGojiraClient,
    HttpxSlackClient,
    _build_system_task_envelope,
    _build_task_envelope,
    _build_teach_envelope,
    _post_task,
)
from kobo_api.github_app import InstallationTokenProvider
from kobo_api.pr_broker import PR_PERMISSIONS, mint_scoped_token
from kobo_api.sero import HttpxSeroClient
from kobo_api.slack_setup import render_manifest

# --- from test_clients.py ---


class RecordingRunner:
    """Test double for CommandRunner: records exact argv (and, per Task 3,
    the exact `env` overlay) for every call, and replays canned (returncode,
    stdout, stderr) results in order (or reuses a single configured result
    for every call). Raises ClientError on rc != 0, exactly like the real
    CommandRunner, so real client branching logic (e.g. ensure_gsa's
    describe-then-create) exercises for real against it.
    """

    def __init__(self, results: list[tuple[int, str, str]] | None = None):
        self._results = list(results) if results is not None else [(0, "", "")]
        self.calls: list[list[str]] = []
        # Parallel to `calls`: the `env` kwarg each call was made with (None
        # when the caller passed none) — GhCLI's token_provider wiring is
        # the only thing exercising this today.
        self.envs: list[dict[str, str] | None] = []

    def run(
        self,
        args: list[str],
        stdin: str | None = None,
        *,
        redact_args: bool = False,
        env: dict[str, str] | None = None,
    ) -> str:
        self.calls.append(list(args))
        self.envs.append(env)
        if len(self._results) > 1:
            rc, out, err = self._results.pop(0)
        else:
            rc, out, err = self._results[0]
        if rc != 0:
            raise ClientError(
                f"{' '.join(args)} failed (rc={rc}): {err or out}",
                returncode=rc,
                stdout=out,
                stderr=err,
            )
        return out


SLACK_TOKENS_TEMPLATE = """\
# Slack app-configuration credentials for P2c (sandbox workspace).
# GITIGNORED (*.local.yaml) — never commit.
# Access token expires ~12h from issue; rotate BOTH via tooling.tokens.rotate.
slack_app_config_token: {config_token}
slack_app_config_refresh_token: {refresh_token}
"""


def _write_slack_tokens_file(tmp_path: Path, *, config_token: str, refresh_token: str) -> Path:
    path = tmp_path / "slack.local.yaml"
    path.write_text(
        SLACK_TOKENS_TEMPLATE.format(config_token=config_token, refresh_token=refresh_token)
    )
    return path


def test_repo_access_as_an_app_trusts_the_installations_grant_not_push():
    """Wire shape: Repo access as an app trusts the installations grant not push.

    GitHub answers an App's repository read with `permissions` all False,
    `push` and `pull` alike, even on a repository the App created (a client org,
    2026-09-25: nine of them). Reading `push` refused every repository. As an
    App the verdict is the installation's own contents grant."""
    runner = RecordingRunner(
        results=[
            (
                0,
                (
                    '{"default_branch":"main","full_name":"acme/clio",'
                    '"permissions":{"admin":false,"pull":false,"push":false}}'
                ),
                "",
            )
        ]
    )
    client = GhCLI(
        runner=runner, token_provider=lambda: "ghs_x", installation_writes=lambda: True
    )

    access = client.repo_access("acme/clio")

    assert access.visible is True
    assert access.can_write is True
    assert access.default_branch == "main"


def test_gojira_create_app_request_shape_and_auth_header():
    """Wire shape: Gojira create app request shape and auth header."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(201, json={"id": 7})

    client = HttpxGojiraClient(
        "http://gojira.local", token="secret-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    app_id = client.create_app(
        name="clio", repo_full_name="acme/agent-clio", cluster="dev", namespace="clio"
    )

    assert app_id == 7
    assert captured["method"] == "POST"
    assert captured["url"] == "http://gojira.local/api/v1/apps"
    assert captured["json"] == {
        "name": "clio",
        "repo_full_name": "acme/agent-clio",
        "workload_class": "agent",
        "environments": [{"name": "dev", "cluster": "dev", "namespace": "clio"}],
        "auto_merge_setup_pr": True,
    }
    assert captured["auth"] == "Bearer secret-token"


def test_gojira_create_app_carries_the_workload_class_it_is_given():
    """Wire shape: Gojira create app carries the workload class it is given.

    Connected agents (design 2026-09-21): a team's own code may have no
    HTTP port at all, and the platform's probing is chosen by this field --
    so it is an argument now, not a constant. The default stays "agent",
    which is what every composed agent has always sent."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 7})

    client = HttpxGojiraClient(
        "http://gojira.local", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    client.create_app(
        name="clio",
        repo_full_name="acme/clio",
        cluster="dev",
        namespace="clio",
        workload_class="agent-headless",
    )

    assert captured["json"]["workload_class"] == "agent-headless"


def test_gojira_delete_app_request_shape_default_delete_namespaces_true():
    """Wire shape: Gojira delete app request shape default delete namespaces true."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(202, json={"app": "clio", "rows_deleted": True})

    client = HttpxGojiraClient(
        "http://gojira.local", token="secret-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.delete_app(7)

    assert captured["method"] == "DELETE"
    assert captured["url"] == "http://gojira.local/api/v1/apps/7"
    assert captured["json"] == {"delete_namespaces": True}
    assert captured["auth"] == "Bearer secret-token"
    assert result == {"app": "clio", "rows_deleted": True}


def test_build_task_envelope_shape_multi_turn():
    # Two user turns plus one agent reply in between -> the LAST message
    # (the current turn) lands in status.message; the two PRIOR turns land
    # in history, same order, each mapped to the verified message shape.
    """Wire shape: Build task envelope shape multi turn."""
    messages: list[ChatMessage] = [
        {"role": "user", "text": "hi"},
        {"role": "agent", "text": "hello yourself"},
        {"role": "user", "text": "how are you?"},
    ]
    envelope = _build_task_envelope(messages)
    assert envelope["status"] == {
        "state": "submitted",
        "message": {
            "kind": "message",
            "role": "user",
            "parts": [{"kind": "text", "text": "how are you?"}],
        },
    }
    assert envelope["history"] == [
        {"kind": "message", "role": "user", "parts": [{"kind": "text", "text": "hi"}]},
        {"kind": "message", "role": "agent", "parts": [{"kind": "text", "text": "hello yourself"}]},
    ]


def test_relay_envelope_context_id_is_the_key_the_switchboard_reads():
    """Wire shape: Relay envelope context id is the key the switchboard reads.

    Contract pin. The switchboard's guard reads exactly "contextId".
    daedalus copies this key onto colleague hops (tools/colleagues.py).
    If this key is ever renamed, the loop guard silently stops working
    and no other test notices."""
    env = _build_task_envelope([{"role": "user", "text": "hi"}], context_id="ctx-1")
    assert "contextId" in env, "the guard reads contextId; any other spelling is inert"
    assert env["contextId"] == "ctx-1"


def test_build_teach_envelope_is_a_marked_single_message_task():
    """Wire shape: Build teach envelope is a marked single message task."""
    envelope = _build_teach_envelope("Taught in chat.\n\nA fact.\n", "chat-20260820-a.md")
    assert envelope["kind"] == "task"
    assert envelope["status"]["state"] == "submitted"
    assert envelope["status"]["message"]["parts"][0]["text"] == "Taught in chat.\n\nA fact.\n"
    assert envelope["history"] == []
    assert envelope["metadata"] == {
        "system_task": "teach",
        "filename": "chat-20260820-a.md",
    }


def test_system_task_envelope_carries_the_marker_and_extra_metadata():
    """Wire shape: System task envelope carries the marker and extra metadata."""
    env = _build_system_task_envelope("knowledge", "body text", metadata={"filename": "a.md"})
    assert env["metadata"] == {"system_task": "knowledge", "filename": "a.md"}
    assert env["status"]["message"]["parts"][0]["text"] == "body text"


def test_post_task_joins_all_text_parts_of_a_multi_part_reply():
    """Wire shape: Post task joins all text parts of a multi part reply.

    The join-all-parts contract (daedalus #13 review): a multi-part reply
    arrives whole, non-text parts are skipped, order kept. parts[0] alone
    would have silently dropped everything after the first."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": {
                    "message": {
                        "parts": [
                            {"kind": "text", "text": "first half"},
                            {"kind": "file", "uri": "s3://irrelevant"},
                            {"kind": "text", "text": "second half"},
                        ]
                    }
                }
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    reply = _post_task(client, 54321, [{"role": "user", "text": "hello?"}])
    assert reply == "first half\nsecond half"


def test_slack_create_app_request_shape_and_auth_header(tmp_path):
    """Wire shape: Slack create app request shape and auth header."""
    tokens_path = _write_slack_tokens_file(
        tmp_path,
        config_token="xoxe.xoxp-1-fake-config-token",
        refresh_token="xoxe-1-fake-refresh-token",
    )
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["manifest"] = parse_qs(request.content.decode())["manifest"][0]
        return httpx.Response(200, json={"ok": True, "app_id": "A0FAKE"})

    client = HttpxSlackClient(
        tokens_path, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    manifest = {"display_information": {"name": "clio"}}
    app_id = client.create_app(manifest)

    assert app_id == "A0FAKE"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://slack.com/api/apps.manifest.create"
    assert captured["auth"] == "Bearer xoxe.xoxp-1-fake-config-token"
    assert captured["manifest"] == json.dumps(manifest)


def test_cabal_graduate_request_shape_body_and_auth_header():
    """Wire shape: Cabal graduate request shape body and auth header."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={})

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.graduate(
        "clio",
        endpoint="http://agent-clio.default.svc.cluster.local",
        audience="clio",
        card={"name": "clio", "team": "acme"},
    )

    assert captured["method"] == "POST"
    assert captured["url"] == "http://cabal.local/admin/persona/clio/graduate"
    assert captured["json"] == {
        "endpoint": "http://agent-clio.default.svc.cluster.local",
        "audience": "clio",
        "agent_card_json": {"name": "clio", "team": "acme"},
    }
    assert captured["auth"] == "Bearer op-token"


def test_cabal_relay_request_shape_body_and_auth_header():
    """Wire shape: Cabal relay request shape body and auth header."""
    captured = {}
    envelope = {"kind": "task", "id": "chat-abc123", "history": []}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200, json={"status": {"message": {"parts": [{"text": "hi"}]}}}
        )

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.relay("clio", envelope)

    assert captured["method"] == "POST"
    assert captured["url"] == "http://cabal.local/a2a/tasks"
    assert captured["json"] == {"target": "clio", "task": envelope}
    assert captured["auth"] == "Bearer op-token"
    assert result == {"status": {"message": {"parts": [{"text": "hi"}]}}}


def test_cabal_mint_colleague_request_shape_body_and_auth_header():
    """Wire shape: Cabal mint colleague request shape body and auth header."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"access_token": "fresh-colleague-token"})

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    token = client.mint_colleague("clio")

    assert captured["method"] == "POST"
    assert captured["url"] == "http://cabal.local/admin/token/mint-service"
    assert captured["json"] == {
        "sub": "agent:clio",
        "scope": ["colleague"],
        "aud": "cabal-core",
        "ttl_days": 365,
    }
    assert captured["auth"] == "Bearer op-token"
    assert token == "fresh-colleague-token"


def test_cabal_put_dispatch_pairs_request_shape_body_and_auth_header():
    """Wire shape: Cabal put dispatch pairs request shape body and auth header."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"count": 2})

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    count = client.put_dispatch_pairs({("agent:b", "c"), ("agent:a", "b")})

    assert captured["method"] == "PUT"
    assert captured["url"] == "http://cabal.local/admin/dispatch-pairs"
    # Sorted for determinism -- a set's own iteration order must never leak
    # into the request body two calls with the same pairs could otherwise
    # send in a different order.
    assert captured["json"] == {"pairs": [["agent:a", "b"], ["agent:b", "c"]]}
    assert captured["auth"] == "Bearer op-token"
    assert count == 2


def test_cabal_list_conversations_passes_target_and_caller_when_given():
    """Wire shape: Cabal list conversations passes target and caller when given.

    The two halves of participant scoping (cabal #128): `target` for the
    conversations an agent answered, `caller` (verbatim `agent:<name>`) for
    the ones it started. Sent only when given, like `limit`."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"conversations": []})

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.list_conversations(limit=200, target="ward")
    assert captured["params"] == {"limit": "200", "target": "ward"}

    client.list_conversations(limit=200, caller="agent:ward")
    assert captured["params"] == {"limit": "200", "caller": "agent:ward"}


# --- from test_sero.py ---


URL = "http://secrets.example"


AUDIENCE = "http://secrets.example"


NAMESPACE = "platform"


EMAIL = "someone@example.com"


AGENT = "clio"


# Never a real credential: a placeholder standing in for one, so these tests
# can prove it appears in exactly one place and in no error text.
SECRET_VALUE = "not-a-real-token-value"


def _client(handler, *, token: str = "id-token") -> HttpxSeroClient:
    return HttpxSeroClient(
        URL,
        AUDIENCE,
        NAMESPACE,
        token_provider=lambda: token,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_put_secret_request_shape_and_auth_header():
    """Wire shape: Put secret request shape and auth header."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "applied": True,
                "action": "create",
                "namespace": NAMESPACE,
                "path": f"apps/{AGENT}/cabal",
                "env": "dev",
                "written_keys": ["CABAL_COLLEAGUE_TOKEN"],
                "removed_keys": [],
            },
        )

    _client(handler, token="an-id-token").put_secret(
        EMAIL, f"apps/{AGENT}/cabal", {"CABAL_COLLEAGUE_TOKEN": SECRET_VALUE}
    )

    assert captured["method"] == "POST"
    assert captured["url"] == f"{URL}/api/service/secrets"
    assert captured["json"] == {
        "acting_email": EMAIL,
        "namespace": NAMESPACE,
        "path": f"apps/{AGENT}/cabal",
        "values": {"CABAL_COLLEAGUE_TOKEN": SECRET_VALUE},
        "remove_keys": [],
        "apply": True,
    }
    assert captured["auth"] == "Bearer an-id-token"


def test_ensure_synced_request_shape_and_auth_header():
    """Wire shape: Ensure synced request shape and auth header."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json=[
                {
                    "surface": "cluster",
                    "action": "create",
                    "target": f"ExternalSecret/{AGENT}/{AGENT}-cabal",
                }
            ],
        )

    _client(handler, token="an-id-token").ensure_synced(
        EMAIL, AGENT, f"apps/{AGENT}/cabal", AGENT, f"{AGENT}-cabal"
    )

    assert captured["method"] == "POST"
    assert captured["url"] == f"{URL}/api/service/sync"
    assert captured["json"] == {
        "acting_email": EMAIL,
        "namespace": NAMESPACE,
        "app": AGENT,
        "path": f"apps/{AGENT}/cabal",
        "k8s_namespace": AGENT,
        "name": f"{AGENT}-cabal",
        "target_name": f"{AGENT}-cabal",
        "refresh_interval": "1m",
        "apply": True,
    }
    assert captured["auth"] == "Bearer an-id-token"


# --- from test_github_app.py ---


APP_ID = "app-123"


INSTALLATION_ID = "installation-456"


def _generate_rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _write_pem(tmp_path, private_key, name: str = "app.pem"):
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / name
    path.write_bytes(pem)
    return path


def _iso_expiry(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _recording_transport(captured: list, responses):
    """A MockTransport handler that appends every request to `captured` and
    replays `responses[0]`, `responses[1]`, ... in order (repeating the last
    for any call past the end)."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        idx = min(len(captured) - 1, len(responses) - 1)
        return responses[idx]

    return handler


def _provider_with_mint(tmp_path, body: dict, fixed_now: float = 1_700_000_000.0):
    private_key, _ = _generate_rsa_keypair()
    key_path = _write_pem(tmp_path, private_key)
    body = {"token": "ghs_abc123", "expires_at": _iso_expiry(fixed_now + 3600), **body}
    captured: list[httpx.Request] = []
    http_client = httpx.Client(
        transport=httpx.MockTransport(
            _recording_transport(captured, [httpx.Response(201, json=body)])
        )
    )
    provider = InstallationTokenProvider(
        APP_ID, INSTALLATION_ID, key_path, http_client=http_client, now=lambda: fixed_now
    )
    return provider, captured


def test_can_write_contents_reads_the_grant_github_returns_with_the_token(tmp_path):
    """Wire shape: Can write contents reads the grant github returns with the token.

    The App's contents permission is per installation and GitHub states it
    in the mint body. That is the source, not a repository's `permissions`,
    which GitHub fills for a user and leaves all-False for an App."""
    provider, captured = _provider_with_mint(
        tmp_path, {"permissions": {"contents": "write", "metadata": "read"}}
    )
    assert provider.can_write_contents() is True
    assert len(captured) == 1  # the check minted once; a second read is cached
    assert provider.can_write_contents() is True
    assert len(captured) == 1


# --- from test_slack_setup.py ---


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """wait_active (direct, and via wire_slack) runs a poll loop — never let
    it sleep for real, same discipline as test_steps_wire.py's own fixture."""
    import kobo_api.slack_setup as slack_setup_module

    monkeypatch.setattr(slack_setup_module, "_sleep", lambda seconds: None)


def test_render_manifest_scopes_byte_equal_to_constraint():
    """Wire shape: Render manifest scopes byte equal to constraint."""
    manifest = render_manifest("echo", "A friendly test agent.")
    assert manifest["oauth_config"]["scopes"]["bot"] == [
        "app_mentions:read",
        "channels:history",
        "groups:history",
        "groups:read",
        "channels:read",
        "im:history",
        "im:read",
        "im:write",
        "mpim:history",
        "mpim:read",
        "chat:write",
        "files:read",
        "reactions:read",
        "reactions:write",
    ]


def test_render_manifest_settings_and_events_exact():
    """Wire shape: Render manifest settings and events exact."""
    manifest = render_manifest("echo", "A friendly test agent.")
    assert manifest["settings"] == {
        "event_subscriptions": {
            # Tagged-only in channels (2026-08-31 ruling): no channel
            # message subscriptions -- mentions are the only channel
            # delivery. DMs and group DMs keep full delivery.
            "bot_events": [
                "app_mention",
                "message.im",
                "message.mpim",
            ]
        },
        "interactivity": {"is_enabled": False},
        "org_deploy_enabled": False,
        "socket_mode_enabled": True,
        "token_rotation_enabled": False,
    }


def test_render_manifest_thread_and_channel_modes_subscribe_channel_messages():
    """Wire shape: Render manifest thread and channel modes subscribe channel messages."""
    for mode in ("thread", "channel"):
        events = render_manifest("echo", "x", wake=mode)["settings"]["event_subscriptions"]["bot_events"]
        assert events == [
            "app_mention",
            "message.channels",
            "message.groups",
            "message.im",
            "message.mpim",
        ]
    # Scopes never move with the mode: no reinstall, only a re-applied manifest.
    assert (
        render_manifest("echo", "x", wake="channel")["oauth_config"]
        == render_manifest("echo", "x")["oauth_config"]
    )


# --- from test_pr_broker.py ---


class _FakeHttp:
    """Records the mint POST and answers like GitHub's 201."""

    def __init__(self, status=201):
        self.status = status
        self.sent = None

    def post(self, url, headers=None, json=None):
        self.sent = {"url": url, "json": json}
        request = httpx.Request("POST", url)
        return httpx.Response(
            self.status,
            request=request,
            json={"token": "ghs_scoped", "expires_at": "2026-08-27T23:00:00Z"},
        )


def _rsa_key_pem(tmp_path):
    # PyJWT needs a real RSA key to sign the App JWT; generate a throwaway.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    p = tmp_path / "k.pem"
    p.write_bytes(pem)
    return p


def test_mint_pins_repositories_and_pr_permissions(tmp_path):
    """Wire shape: Mint pins repositories and pr permissions."""
    fake = _FakeHttp()
    out = mint_scoped_token(
        app_id="1",
        installation_id="2",
        private_key_path=_rsa_key_pem(tmp_path),
        repositories=["acme/services-clip", "acme/sero"],
        http_client=fake,
        now=lambda: 1_000_000,
    )
    assert fake.sent["json"]["repositories"] == ["services-clip", "sero"]  # bare names
    assert fake.sent["json"]["permissions"] == PR_PERMISSIONS
    assert out["token"] == "ghs_scoped"
    assert out["repositories"] == ["acme/services-clip", "acme/sero"]


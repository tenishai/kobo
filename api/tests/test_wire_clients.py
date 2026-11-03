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

from kobo_api import (
    admin_integrations,
    admin_store,
    model_providers,
    room_models,
    self_hosted,
)
from kobo_api.clients import (
    CabalClient,
    ChatMessage,
    ClientError,
    Clients,
    GhCLI,
    HttpxGojiraClient,
    HttpxSlackClient,
    _build_system_task_envelope,
    _build_task_envelope,
    _build_teach_envelope,
    _post_task,
)
from kobo_api.config import Config, ModelChoice
from kobo_api.github_app import InstallationTokenProvider
from kobo_api.liminal_admin import (
    HttpxLiminalAdminClient,
    TargetNotFound,
    targets_unsupported,
)
from kobo_api.sero import HttpxSeroClient
from kobo_api.slack_setup import render_manifest
from tests.fakes import FakeBab, FakeKubectl, FakeModelsGateway

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


def test_slack_oauth_access_sends_the_client_pair_in_the_form_and_no_bearer(tmp_path):
    """Wire shape: oauth.v2.access (issue #371) carries the client pair, the
    code and the redirect in the form body and no config token."""
    tokens_path = _write_slack_tokens_file(
        tmp_path, config_token="xoxe.xoxp-1-fake-config-token", refresh_token="xoxe-1-fake-refresh-token"
    )
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["form"] = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        return httpx.Response(200, json={"ok": True, "access_token": "xoxb-fake", "team": {"name": "Fake"}})

    client = HttpxSlackClient(tokens_path, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    answer = client.oauth_access(
        client_id="fake-client-id", client_secret="fake-client-secret", code="fake-code",
        redirect_uri="https://room.example/slack/oauth/callback",
    )

    assert answer["access_token"] == "xoxb-fake"
    assert captured["url"] == "https://slack.com/api/oauth.v2.access"
    assert captured["auth"] is None
    assert captured["form"] == {"client_id": "fake-client-id", "client_secret": "fake-client-secret",
                                "code": "fake-code", "redirect_uri": "https://room.example/slack/oauth/callback"}


def test_slack_update_app_request_shape_and_auth_header(tmp_path):
    """Wire shape: apps.manifest.update (issue #371) carries the app id and
    the manifest as JSON text under the config token."""
    tokens_path = _write_slack_tokens_file(
        tmp_path, config_token="xoxe.xoxp-1-fake-config-token", refresh_token="xoxe-1-fake-refresh-token"
    )
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["form"] = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        return httpx.Response(200, json={"ok": True})

    client = HttpxSlackClient(tokens_path, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    client.update_app("A0FAKE", {"display_information": {"name": "clio"}})

    assert captured["url"] == "https://slack.com/api/apps.manifest.update"
    assert captured["auth"] == "Bearer xoxe.xoxp-1-fake-config-token"
    assert captured["form"] == {"app_id": "A0FAKE", "manifest": json.dumps({"display_information": {"name": "clio"}})}


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
        "chat:write.customize",
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


def test_gateway_targets_go_out_as_the_stanza_and_a_plain_404_is_an_older_admin_plane():
    """Wire shape (gateway admin plane, liminal#222): a target is PUT as the stanza's keys with the
    room's bearer token; the gateway's 409/422 sentence is the error raised; FastAPI's plain
    `Not Found` for a route the gateway lacks is told apart from a 404 about the target, so the
    room can fall back to the pull request on one and call the target gone on the other."""
    captured = {}
    answers = {"PUT": httpx.Response(200, json={"name": "acme-tool", "status": "ready"})}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"], captured["url"] = request.method, str(request.url)
        captured["json"] = json.loads(request.content) if request.content else None
        captured["auth"] = request.headers.get("authorization")
        return answers[request.method]

    client = HttpxLiminalAdminClient("http://liminal-admin.local", lambda: "room-token",
                                     client=httpx.Client(transport=httpx.MockTransport(handler)))
    spec = {"transport": "http", "address": "https://acme.example", "trust": "untrusted", "auth_method": "bearer", "bearer_token_env": "INTEGRATION_ACME_TOOL_TOKEN"}
    assert client.put_target("acme-tool", spec) == {"name": "acme-tool", "status": "ready"}
    assert (captured["method"], captured["url"], captured["json"], captured["auth"]) == (
        "PUT", "http://liminal-admin.local/admin/targets/acme-tool", spec, "Bearer room-token")

    answers["PUT"] = httpx.Response(409, json={"detail": "target 'acme-tool' is owned by the gateway's config file"})
    with pytest.raises(ClientError, match="owned by the gateway's config file") as refused:
        client.put_target("acme-tool", spec)
    assert not targets_unsupported(refused.value)

    answers["PUT"] = httpx.Response(404, json={"detail": "Not Found"})
    with pytest.raises(ClientError) as older:
        client.put_target("acme-tool", spec)
    assert targets_unsupported(older.value)

    answers["DELETE"] = httpx.Response(404, json={"detail": "no live target 'acme-tool'"})
    with pytest.raises(TargetNotFound):
        client.delete_target("acme-tool")
    answers["DELETE"] = httpx.Response(200, json={"removed": "acme-tool"})
    assert client.delete_target("acme-tool") == {"removed": "acme-tool"}
    assert captured["url"] == "http://liminal-admin.local/admin/targets/acme-tool"


# --- the self-hosted models probe and its gateway registration (kobo#390) ----


class SelfHostedServer:
    """An OpenAI-compatible endpoint as `httpx.MockTransport`: `/v1/models`
    lists what it serves, `/v1/chat/completions` answers for the models in
    `alive` and refuses the rest with the server's own sentence. Records
    every request so a test can read the bodies and headers it was sent."""

    def __init__(self, *, alive: set[str], listed: list[str] | None = None, rejects_options: set[str] | None = None):
        self.alive = set(alive)
        self.listed = listed if listed is not None else sorted(alive)
        # Models that answer only a plain body: an engine whose tokenizer refuses
        # `chat_template_kwargs` (Mistral's, live 2026-11-02) says so with a 400.
        self.rejects_options = set(rejects_options or ())
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/v1/models") and request.method == "GET":
            return httpx.Response(200, json={"object": "list", "data": [{"id": m, "object": "model"} for m in self.listed]})
        if request.url.path.endswith("/v1/chat/completions"):
            body = json.loads(request.content)
            if body["model"] in self.rejects_options and "chat_template_kwargs" in body:
                return httpx.Response(400, json={"error": {"message": "chat_template is not supported for Mistral tokenizers.", "type": "BadRequestError", "code": 400}})
            if body["model"] in self.alive:
                return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "hi"}}]})
            return httpx.Response(404, json={"error": {"message": f"model {body['model']!r} does not exist", "type": "NotFoundError"}})
        return httpx.Response(404, json={"error": {"message": "no such route"}})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def chat_bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith("/chat/completions")]


def _self_hosted_room(tmp_path):
    config = Config(workdir=tmp_path / "work", models_gateway_url="http://models-gateway.models.svc", room_namespace="kobo")
    gateway = FakeModelsGateway()
    kubectl = FakeKubectl()
    clients = Clients(github=None, gojira=None, cloud=None, agent=None, kubectl=kubectl, models_gateway=gateway)
    return config, clients, gateway, kubectl


def test_self_hosted_connect_probes_every_model_and_registers_only_the_answering_ones(session_factory, tmp_path, monkeypatch):
    """Wire shape the model gateway reads (kobo#390): connecting a Self-hosted
    models row asks the endpoint `GET /v1/models`, then one short chat
    completion per named model carrying the row's options; the model that
    fails is marked on the row with the server's sentence and never reaches
    the gateway, the ones that answer are registered as `openai/<name>` at
    the endpoint's `/v1` with the options as `extra_body`."""
    server = SelfHostedServer(alive={"qwen3-32b", "llama-3-70b", "codestral-22b"}, rejects_options={"codestral-22b"},
                              listed=["qwen3-32b", "llama-3-70b", "old-model", "codestral-22b"])
    monkeypatch.setattr(self_hosted, "http_client", server.client)
    config, clients, gateway, _ = _self_hosted_room(tmp_path)
    session = session_factory()

    row = admin_integrations.connect(
        session, clients, config, vendor="self-hosted", name="Lab GPU pool", base_access="read", credential="", address="",
        trust="untrusted", actor="ops@example.com", auth="self_hosted_endpoint",
        identity={"base_url": "http://10.0.0.5", "models": "qwen3-32b = Qwen3 32B\nllama-3-70b\nold-model\ncodestral-22b",
                  "options": "chat_template_kwargs: {enable_thinking: false}"},
        secrets={},
    )
    session.commit()

    # The probe: the listing, then one completion per model with the options, eight tokens; the
    # model that refuses the options is asked once more without them.
    assert [(r.method, r.url.path) for r in server.requests] == [
        ("GET", "/v1/models"), ("POST", "/v1/chat/completions"), ("POST", "/v1/chat/completions"), ("POST", "/v1/chat/completions"),
        ("POST", "/v1/chat/completions"), ("POST", "/v1/chat/completions")]
    assert [b["model"] for b in server.chat_bodies()] == ["qwen3-32b", "llama-3-70b", "old-model", "codestral-22b", "codestral-22b"]
    assert all(b["max_tokens"] == 8 for b in server.chat_bodies())
    assert [b.get("chat_template_kwargs") for b in server.chat_bodies()] == [{"enable_thinking": False}] * 4 + [None]
    assert "authorization" not in server.requests[0].headers
    # The row: connected, with the verdict per model and the stale entry named.
    assert row.state == admin_store.STATE_APPLIED
    assert row.spec["base_url"] == "http://10.0.0.5/v1"
    assert [(m["name"], m["label"], m["ok"]) for m in row.spec["models"]] == [
        ("qwen3-32b", "Qwen3 32B", True), ("llama-3-70b", "llama-3-70b", True), ("old-model", "old-model", False),
        ("codestral-22b", "codestral-22b", True)]
    assert row.spec["models"][2]["reason"] == "HTTP 404: model 'old-model' does not exist"
    assert row.spec["models"][3]["without_options"] is True and row.spec["models"][3]["reason"] is None
    assert "3 of 4 models answer; `old-model` failed: HTTP 404: model 'old-model' does not exist" in row.state_detail
    assert row.credential_ref is None
    # The gateway: the answering models only, under the row's name, with the options where LiteLLM
    # takes them, and none for the model that refused them.
    assert set(gateway.models) == {"lab-gpu-pool:qwen3-32b", "lab-gpu-pool:llama-3-70b", "lab-gpu-pool:codestral-22b"}
    assert "extra_body" not in gateway.models["lab-gpu-pool:codestral-22b"]["litellm_params"]
    qwen = gateway.models["lab-gpu-pool:qwen3-32b"]
    assert qwen["model_name"] == "lab-gpu-pool-qwen3-32b"
    assert qwen["litellm_params"] == {
        "model": "openai/qwen3-32b", "api_base": "http://10.0.0.5/v1", "api_key": "no-auth",
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    assert qwen["model_info"] == {"provider": "self-hosted", "integration": "lab-gpu-pool", "label": "Qwen3 32B", "id": "lab-gpu-pool:qwen3-32b"}
    # An agent's model id names its gateway model by itself.
    assert model_providers.gateway_model_for([], "self-hosted/lab-gpu-pool/qwen3-32b") == "lab-gpu-pool-qwen3-32b"
    session.close()


def test_self_hosted_connect_refuses_when_nothing_answers_and_writes_nothing(session_factory, tmp_path, monkeypatch):
    """Nothing is written for an endpoint that does not answer, or whose
    named models all fail (kobo#390): the connect is refused with the
    server's sentence and no row, no Secret and no gateway model exist."""
    server = SelfHostedServer(alive=set(), listed=["something-else"])
    monkeypatch.setattr(self_hosted, "http_client", server.client)
    config, clients, gateway, kubectl = _self_hosted_room(tmp_path)
    session = session_factory()

    with pytest.raises(admin_integrations.ConnectRefused, match="none of the models answered at 10.0.0.5"):
        admin_integrations.connect(
            session, clients, config, vendor="self-hosted", name="Lab", base_access="read", credential="", address="",
            trust="untrusted", actor="ops@example.com", auth="self_hosted_endpoint",
            identity={"base_url": "http://10.0.0.5", "models": "gone-model"}, secrets={"api_key": "sk-lab-secret"},
        )

    assert admin_store.list_integrations(session) == []
    assert kubectl.secrets_applied == [] and gateway.models == {}
    session.close()


def test_check_models_reprobes_and_flips_a_models_state_on_the_row_and_the_gateway(session_factory, tmp_path, monkeypatch):
    """Check models (kobo#390) runs the connect probe again: a model that
    stopped answering is marked failed and comes off the gateway, one that
    came back is marked ok and goes on; a row whose models all stopped
    reads Failed and serves nothing."""
    server = SelfHostedServer(alive={"qwen3-32b"}, listed=["qwen3-32b", "old-model"])
    monkeypatch.setattr(self_hosted, "http_client", server.client)
    config, clients, gateway, _ = _self_hosted_room(tmp_path)
    session = session_factory()
    admin_integrations.connect(
        session, clients, config, vendor="self-hosted", name="Lab", base_access="read", credential="", address="",
        trust="untrusted", actor="ops@example.com", auth="self_hosted_endpoint",
        identity={"base_url": "https://models.example.internal/v1", "models": "qwen3-32b\nold-model"}, secrets={},
    )
    session.commit()
    assert set(gateway.models) == {"lab:qwen3-32b"}
    # An admin offers the second model too (kobo#405); the gateway follows the probe from here.
    room_models.apply_changes(session, config, clients, [{"id": "self-hosted/lab/old-model", "enabled": True}],
                              bab=None, actor="ops@example.com")
    session.commit()

    server.alive = {"old-model"}
    out = admin_integrations.check_models(session, clients, config, integration_id="lab", actor="ops@example.com")
    session.commit()
    assert (out["answering"], out["total"], out["state"]) == (1, 2, "applied")
    assert [(m["name"], m["ok"]) for m in out["models"]] == [("qwen3-32b", False), ("old-model", True)]
    assert set(gateway.models) == {"lab:old-model"}
    row = session.get(admin_integrations.AdminIntegration, "lab")
    assert [(m["name"], m["ok"]) for m in row.spec["models"]] == [("qwen3-32b", False), ("old-model", True)]

    server.alive = set()
    gone = admin_integrations.check_models(session, clients, config, integration_id="lab", actor="ops@example.com")
    session.commit()
    assert gone["state"] == "failed" and gateway.models == {}
    session.close()


# --- Bedrock through an AWS account (kobo#390) --------------------------------

BEDROCK_ROLE = "arn:aws:iam::123456789012:role/KoboModels-Bedrock"
BEDROCK_HAIKU = "bedrock/anthropic.claude-3-5-haiku-20241022-v1:0"


def _bedrock_room(tmp_path):
    config = Config(
        workdir=tmp_path / "work", models_gateway_url="http://models-gateway.models.svc", room_namespace="kobo",
        models=[
            ModelChoice(id=BEDROCK_HAIKU, label="Claude Cheap on AWS (Haiku)", family="Claude", tier="fast"),
            ModelChoice(id="bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0", label="Claude Balanced on AWS (Sonnet)", family="Claude", tier="balanced"),
            ModelChoice(id="vertex_ai/claude-sonnet-4-5", label="Claude Affordable Sonnet", family="Claude", tier="balanced"),
        ],
    )
    gateway, bab = FakeModelsGateway(), FakeBab(integrations=[])
    clients = Clients(github=None, gojira=None, cloud=None, agent=None, kubectl=FakeKubectl(), bab=bab, models_gateway=gateway)
    return config, clients, gateway, bab


def _connect_aws(session, clients, config, *, name, auth, identity):
    return admin_integrations.connect(
        session, clients, config, vendor="aws", name=name, base_access="read", credential="", address="",
        trust="untrusted", actor="ops@example.com", auth=auth, identity=identity, secrets={},
    )


def test_an_aws_account_with_bedrock_ticked_registers_the_orgs_bedrock_entries_on_the_gateway_as_its_role(session_factory, tmp_path):
    """Wire shape the model gateway reads (kobo#390): an `aws_role` row connected
    with the Bedrock box ticked carries `models = {provider, region, role_arn}`
    and registers every `bedrock/` entry of the org's list on the gateway as a
    deployment the gateway serves by assuming that role with its own identity
    token (region, role, token path, session name; no key anywhere). Entries of
    another provider are not touched, and an organization's hub row never
    carries the tick."""
    config, clients, gateway, bab = _bedrock_room(tmp_path)
    session = session_factory()

    row = _connect_aws(session, clients, config, name="acme-models", auth="aws_role",
                       identity={"role_arn": BEDROCK_ROLE, "models_provider": "bedrock", "models_region": "us-west-2"})
    session.commit()

    assert row.state == admin_store.STATE_APPLIED, row.state_detail
    assert row.spec["models"] == {"provider": "bedrock", "region": "us-west-2", "role_arn": BEDROCK_ROLE}
    assert row.spec["identity"] == {"role_arn": BEDROCK_ROLE, "audience": "sts.amazonaws.com"}, "the tick lives in models, not identity"
    assert "Bedrock models in us-west-2" in row.state_detail and "not yet checked with AWS" in row.state_detail
    # The permission service still holds the role as before: the tick changes nothing there.
    assert bab.probes == [(BEDROCK_ROLE, "sts.amazonaws.com")]
    (integ,) = bab.integrations("x")
    assert integ["spec"] == {"role_arn": BEDROCK_ROLE, "audience": "sts.amazonaws.com", "name": "acme-models"}
    # The gateway: one deployment per bedrock entry, as the role; the Vertex entry has no provider.
    assert sorted(gateway.models) == sorted([
        f"{row.id}:bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0", f"{row.id}:{BEDROCK_HAIKU}"])
    haiku = gateway.models[f"{row.id}:{BEDROCK_HAIKU}"]
    assert haiku["model_name"] == "claude-cheap-on-aws-haiku"
    assert haiku["litellm_params"] == {
        "model": BEDROCK_HAIKU, "aws_region_name": "us-west-2", "aws_role_name": BEDROCK_ROLE,
        "aws_web_identity_token": "oidc/google/sts.amazonaws.com", "aws_session_name": "kobo-gateway",
    }
    assert haiku["model_info"]["provider"] == "bedrock" and haiku["model_info"]["integration"] == row.id

    # An organization's hub may not carry it, and an account without the tick serves nothing.
    with pytest.raises(admin_integrations.ConnectRefused, match="per account"):
        _connect_aws(session, clients, config, name="acme-org", auth="aws_hub",
                     identity={"hub_role_arn": "arn:aws:iam::999999999999:role/KoboAccess-Hub", "models_provider": "bedrock"})
    with pytest.raises(admin_integrations.ConnectRefused, match="not an AWS region"):
        _connect_aws(session, clients, config, name="acme-bad", auth="aws_role",
                     identity={"role_arn": BEDROCK_ROLE, "models_provider": "bedrock", "models_region": "US East"})
    plain = _connect_aws(session, clients, config, name="acme-audit", auth="aws_role",
                         identity={"role_arn": "arn:aws:iam::123456789012:role/KoboAccess-Role"})
    session.commit()
    assert "models" not in plain.spec and len(gateway.models) == 2

    # Disconnect takes exactly its models off the gateway.
    admin_integrations.disconnect(session, clients, config, integration_id=row.id, actor="ops@example.com")
    session.commit()
    assert gateway.models == {}


def test_bab_discover_waits_longer_than_an_ordinary_call():
    """Check accounts probes every account of the organization (two role hops each, eight
    at a time), which takes longer than the client's fifteen seconds on an organization
    of twenty accounts; the first Check accounts on dev came back ReadTimeout while the
    service was still working (2026-11-01). The discover call carries its own timeout."""
    from kobo_api.bab import DISCOVER_TIMEOUT_S, HttpxBabClient

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"accounts": []})

    bab = HttpxBabClient("http://bab.local", "tok", client=httpx.Client(transport=httpx.MockTransport(handler)))
    bab.discover_accounts("aws", "kyle@example.com", role_name="KoboAccess-Role")
    assert seen["path"] == "/integrations/aws/discover"
    assert seen["timeout"]["read"] == DISCOVER_TIMEOUT_S
    assert DISCOVER_TIMEOUT_S >= 60

    bab.org_accounts("aws", "kyle@example.com")
    assert seen["timeout"]["read"] < DISCOVER_TIMEOUT_S  # the ordinary call keeps the client's own timeout

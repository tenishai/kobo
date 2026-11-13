"""An agent that runs its team's own code gets what an agent the room builds
gets (2026-11-13): storage that outlives a restart at connect, a gateway
identity when the room has a gateway, and a room that follows its own merged
pull requests through instead of leaving a team's build red."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from kobo_api import connected_wiring
from kobo_api.clients import Clients
from kobo_api.kinds import KIND_CONNECTED, RUN_MODE_HTTP
from kobo_api.models import Agent, AgentStatus
from kobo_api.provisioner import Ctx
from kobo_api.steps import CONNECTED_WIRE_BRANCH, ConnectedWireStep
from tests.fakes import (
    FakeBab,
    FakeCabal,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeModelsGateway,
)

NAME = "clio"
REPO = "example-org/team-chatbot"
PR_URL = "https://github.com/example-org/team-chatbot/pull/4"
GATEWAY = {
    "cabal_url": "http://switchboard.internal",
    "agent_endpoint_template": "http://agent-{name}.internal",
    "liminal_url": "http://gateway.internal/mcp",
    "models_gateway_url": "http://models.internal/",
}


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    import kobo_api.steps as steps_module

    monkeypatch.setattr(steps_module, "_sleep", lambda seconds: None)


@pytest.fixture
def gateway_config(config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    return config.model_copy(update=GATEWAY)


def _room(session_factory, config, *, github=None, status=AgentStatus.PROVISIONING, bab=None):
    session = session_factory()
    agent = Agent(name=NAME, persona="connected", model_id="vertex_ai/claude-sonnet-4-5", skills=[],
                  kind=KIND_CONNECTED, run_mode=RUN_MODE_HTTP, secret_names=[], repo_full_name=REPO,
                  gojira_app_id=1, status=status, created_by="owner@example.com")
    session.add(agent)
    session.commit()
    gojira = FakeGojira(activate_after=1)
    gojira.create_app(name=NAME, repo_full_name=REPO, cluster=config.gojira_cluster, namespace=NAME)
    clients = Clients(github=github if github is not None else FakeGitHub(pr_url=PR_URL), gojira=gojira, cloud=None,
                      agent=None, kubectl=FakeKubectl(), cabal=FakeCabal(), models_gateway=FakeModelsGateway())
    clients.bab = bab
    workdir = config.workdir / NAME
    workdir.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(agent=agent, session=session, config=config, clients=clients, workdir=workdir)
    app = SimpleNamespace(state=SimpleNamespace(clients=clients, config=config, session_factory=session_factory))
    return SimpleNamespace(session=session, agent=agent, clients=clients, github=clients.github, gojira=gojira, ctx=ctx, app=app)


def _proposed(github) -> dict:
    return yaml.safe_load(github.pushed_file("gojira.yaml"))


def test_connect_gives_a_connected_agent_storage_a_pass_and_a_models_key(session_factory, gateway_config):
    """Wire shape: the request a connect opens mounts a volume, names the two Secrets and the
    three addresses, and both Secrets exist before the request does."""
    room = _room(session_factory, gateway_config)

    ConnectedWireStep().run(room.ctx)

    data = _proposed(room.github)
    assert data["data_mount"] == {"pvc": f"agent-{NAME}-data", "path": "/data", "fs_group": 10001}
    assert data["secrets_from"] == [f"{NAME}-cabal", f"{NAME}-models"]
    assert {k: v for k, v in data["env"].items() if k.startswith("KOBO_")} == {
        "KOBO_DATA_DIR": "/data",
        "KOBO_SWITCHBOARD_URL": "http://switchboard.internal",
        "KOBO_GATEWAY_URL": "http://gateway.internal/mcp",
        "KOBO_MODELS_URL": "http://models.internal/v1",
    }
    claim = yaml.safe_load(room.github.pushed_file("k8s/base/kobo-storage.yaml"))
    assert (claim["kind"], claim["metadata"]) == ("PersistentVolumeClaim", {"name": f"agent-{NAME}-data", "namespace": NAME})
    assert "kobo-storage.yaml" in yaml.safe_load(room.github.pushed_file("k8s/base/kustomization.yaml"))["resources"]
    assert room.clients.cabal.mint_colleague_calls == [NAME]
    assert set(room.clients.kubectl.secrets) == {(NAME, f"{NAME}-cabal"), (NAME, f"{NAME}-models")}
    body = room.github.open_pr_calls[0]["body"]
    assert "KOBO_DATA_DIR" in body and "KOBO_GATEWAY_URL" in body and "KOBO_MODELS_URL" in body


def test_a_room_with_no_gateway_wires_storage_and_nothing_of_the_gateway(session_factory, config):
    """Gate: the gateway is an add-on. A room without one mints nothing and names no Secret
    that would never arrive (a pod waits forever on a Secret that does not exist)."""
    room = _room(session_factory, config)

    ConnectedWireStep().run(room.ctx)

    data = _proposed(room.github)
    assert data["data_mount"]["pvc"] == f"agent-{NAME}-data" and data["env"]["KOBO_DATA_DIR"] == "/data"
    assert "secrets_from" not in data
    assert not [k for k in data["env"] if k in ("KOBO_SWITCHBOARD_URL", "KOBO_GATEWAY_URL", "KOBO_MODELS_URL")]
    assert room.clients.cabal.mint_colleague_calls == [] and room.clients.kubectl.secrets == {}


def test_storage_a_team_already_declared_is_kept(session_factory, config):
    """Data integrity: a team's own mount is theirs. The room adds no second claim and does
    not point its variable at a path it did not make."""
    tree = {
        "gojira.yaml": f"app: {NAME}\nclass: agent\ndata_mount: {{pvc: ours, path: /var/ours}}\n",
        "k8s/base/kustomization.yaml": "resources:\n  - deployment.yaml\n",
    }
    room = _room(session_factory, config, github=FakeGitHub(pr_url=PR_URL, clone_tree=tree))

    ConnectedWireStep().run(room.ctx)

    data = _proposed(room.github)
    assert data["data_mount"] == {"pvc": "ours", "path": "/var/ours"}
    assert "KOBO_DATA_DIR" not in data.get("env", {})
    assert "kobo-storage.yaml" not in yaml.safe_load(room.github.pushed_file("k8s/base/kustomization.yaml"))["resources"]


def test_a_retry_that_finds_its_request_open_does_not_take_the_pass_back(session_factory, gateway_config):
    """Gate: minting revokes the pass before it. A Retry on a request a team has not merged
    yet must not do that to an agent that is already running on the first one."""
    room = _room(session_factory, gateway_config, github=FakeGitHub(pr_url=PR_URL, merge_pr_results=[False]))

    for _ in range(2):
        with pytest.raises(RuntimeError, match="Merge this pull request"):
            ConnectedWireStep().run(room.ctx)

    assert len(room.github.open_pr_calls) == 1
    assert room.clients.cabal.mint_colleague_calls == [NAME]
    assert len([c for c in room.clients.models_gateway.calls if c.startswith("mint_key:")]) == 1


SAMPLE = (
    "app: clio\nclass: agent\nenvironments:\n  - name: dev\n    cluster: example-cluster\n"
    "    namespace: clio\nservice_account: agent-clio\n"
)
# What the deploy platform's own build prints for SAMPLE (its one-liner, run 2026-11-13).
SAMPLE_FINGERPRINT = "c1461ef7ec5790de3060cac1fc7c16bf517dcbfce214dbe1f3cc77ba1ee48e51"


def _workflow(stamp: str) -> str:
    return f"name: ci\nenv:\n  IMAGE: example\n  GOJIRA_CONTRACT: {stamp}\njobs: {{}}\n"


def test_the_fingerprint_is_the_one_the_teams_build_computes():
    """Wire shape: the room and the build must agree on what "changed" means, or the room
    asks for a regeneration nobody needs, or never asks for one a red build is waiting on."""
    assert connected_wiring.contract_fingerprint(SAMPLE) == SAMPLE_FINGERPRINT
    reordered = "service_account: agent-clio   # ours\n" + SAMPLE.replace("service_account: agent-clio\n", "")
    assert connected_wiring.contract_fingerprint(reordered) == SAMPLE_FINGERPRINT


def test_a_merged_request_is_followed_through_once_per_version_of_the_file():
    """Gate (found 2026-11-13): a team merges the room's request and their build refuses to
    deploy until the platform regenerates. The room asks, once; it asks again only when the
    file changes again, and never when the two agree or one could not be read."""
    gojira = FakeGojira()
    gojira.create_app(name=NAME, repo_full_name=REPO, cluster="example-cluster", namespace=NAME)
    asked: dict[str, str] = {}
    follow = lambda files: connected_wiring.follow_through(
        FakeGitHub(repo_files=files), gojira, repo=REPO, gojira_app_id=1, asked=asked)
    workflow = ".github/workflows/gojira-ci.yaml"

    assert follow({"gojira.yaml": SAMPLE, workflow: _workflow(SAMPLE_FINGERPRINT)}) is False
    assert follow({"gojira.yaml": SAMPLE}) is False
    merged = SAMPLE + "secrets_from: [clio-env]\n"
    assert follow({"gojira.yaml": merged, workflow: _workflow(SAMPLE_FINGERPRINT)}) is True
    assert follow({"gojira.yaml": merged, workflow: _workflow(SAMPLE_FINGERPRINT)}) is False
    assert gojira.reprovision_calls == [1]
    assert follow({"gojira.yaml": merged + "env: {A: b}\n", workflow: _workflow(SAMPLE_FINGERPRINT)}) is True
    assert gojira.reprovision_calls == [1, 1]


def _holding(agent: str = NAME) -> FakeBab:
    bab = FakeBab(integrations=[])
    bab._integrations.append({"integration": "fleet-audit", "vendor": "aws", "ceiling": ["aws_cloudtrail_read"],
                              "created_at": "2026-10-29T12:00:00", "updated_at": "2026-10-29T12:00:00"})
    bab._grants[1] = {"id": 1, "agent": agent, "integration": "fleet-audit", "permission": "aws_cloudtrail_read",
                      "state": "active"}
    return bab


def test_an_agent_connected_earlier_is_offered_the_wiring_when_it_first_holds_a_permission(session_factory, gateway_config):
    """Gate: an agent connected before this holds a permission it cannot use. The pass offers
    the wiring once, as a request the TEAM merges (the room never merges a later request of
    its own), with the pass delivered first; an agent holding nothing is left alone."""
    idle = _room(session_factory, gateway_config, status=AgentStatus.READY, bab=FakeBab(integrations=[]))
    connected_wiring.keep(idle.app)
    assert idle.github.open_pr_calls == [] and idle.clients.cabal.mint_colleague_calls == []
    idle.session.delete(idle.agent)
    idle.session.commit()

    room = _room(session_factory, gateway_config, status=AgentStatus.READY, bab=_holding())
    connected_wiring.keep(room.app)
    connected_wiring.keep(room.app)

    assert [(c["repo"], c["branch"]) for c in room.github.open_pr_calls] == [(REPO, CONNECTED_WIRE_BRANCH)]
    assert room.github.merge_pr_calls == []
    assert f"{NAME}-cabal" in _proposed(room.github)["secrets_from"]
    assert room.clients.cabal.mint_colleague_calls == [NAME]
    with session_factory() as session:
        assert session.query(Agent).filter_by(name=NAME).one().pass_minted_at is not None


def test_the_access_tab_is_told_where_the_wiring_stands(session_factory, gateway_config, config):
    """Wire shape: owed until a request is open, waiting while it is, wired once the default
    branch carries the pass and both addresses; and nothing at all in a room with no gateway."""
    room = _room(session_factory, gateway_config)
    wired = yaml.safe_dump({"app": NAME, "secrets_from": [f"{NAME}-cabal"], "env": {
        "KOBO_SWITCHBOARD_URL": "http://switchboard.internal", "KOBO_GATEWAY_URL": "http://gateway.internal/mcp"}})

    state = lambda github, cfg=gateway_config: connected_wiring.state(github, room.agent, cfg)
    assert state(FakeGitHub(repo_files={"gojira.yaml": SAMPLE})) == {"state": "owed", "pull_request": None}
    waiting = FakeGitHub(repo_files={"gojira.yaml": SAMPLE}, open_prs={f"{REPO}#{CONNECTED_WIRE_BRANCH}": PR_URL})
    assert state(waiting) == {"state": "waiting", "pull_request": PR_URL}
    assert state(FakeGitHub(repo_files={"gojira.yaml": wired})) == {"state": "wired", "pull_request": None}
    assert state(FakeGitHub(repo_files={"gojira.yaml": SAMPLE}), config) is None


def test_the_example_client_trades_the_pass_and_keeps_the_token_fresh(monkeypatch):
    """Wire shape: the file a team copies sends the pass to the switchboard only, as a bearer
    on a bodiless POST to /auth/liminal-token, reuses the token until a minute before it runs
    out, and trades again after `forget` (a 401) or when the expiry cannot be read."""
    import importlib.util
    import io
    import json
    from datetime import UTC, datetime, timedelta
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "docs" / "examples" / "gateway_token.py"
    spec = importlib.util.spec_from_file_location("gateway_token_example", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    seen = []
    answers = [
        {"access_token": "short-1", "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat()},
        {"access_token": "short-2", "expires_at": "not a time"},
        {"access_token": "short-3", "expires_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat()},
        {"access_token": "short-4", "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat()},
    ]

    def urlopen(request, timeout):
        seen.append((request.full_url, request.get_method(), request.get_header("Authorization"), request.data))
        return io.BytesIO(json.dumps(answers[len(seen) - 1]).encode())

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("KOBO_SWITCHBOARD_URL", "http://switchboard.internal/")
    monkeypatch.setenv("CABAL_COLLEAGUE_TOKEN", "the-pass")

    token = module.GatewayToken()
    assert token.headers() == {"Authorization": "Bearer short-1"} and token.get() == "short-1"
    assert seen == [("http://switchboard.internal/auth/liminal-token", "POST", "Bearer the-pass", None)]
    token.forget()
    assert token.get() == "short-2"  # after a 401
    assert token.get() == "short-3"  # an unreadable expiry is already past
    assert token.get() == "short-4"  # thirty seconds left is inside the minute of slack
    assert token.get() == "short-4" and len(seen) == 4

    monkeypatch.delenv("CABAL_COLLEAGUE_TOKEN")
    token.forget()
    with pytest.raises(RuntimeError, match="wiring has not reached"):
        token.get()

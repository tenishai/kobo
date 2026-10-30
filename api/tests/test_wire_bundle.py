"""Wire shapes the room writes into an agent's repository.

The daedalus bundle (agent.yaml against the vendored schema, the tools and
auth blocks, persona, roster, card), the Dockerfile's runtime pin, the
gojira.yaml contract and the kustomization registration the deploy
platform reads. Folded on 2026-10-29 from test_composer.py,
test_steps_wire.py and test_colleague_mode.py.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from kobo_api.clients import Clients
from kobo_api.composer import (
    _VENDOR_SCHEMA_PATH,
    BundleSpec,
    ComposeError,
    compose,
    validate_agent_yaml,
)
from kobo_api.models import Agent
from kobo_api.provisioner import Ctx
from kobo_api.steps import TECHNE_FS_GROUP, StagedSkill, WireStep
from tests.fakes import (
    GENERATED_KUSTOMIZATION_RESOURCES,
    FakeCabal,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
)

# --- from test_composer.py ---


_RUNTIME_IMAGE = "registry.example/agents/daedalus-runtime:latest"


def _make_spec(
    skills: list[StagedSkill] | None = None,
    *,
    name: str = "sam",
    team: str = "shared",
    group: str | None = None,
    purpose_tags: list[str] | None = None,
    capability: str | None = None,
    colleagues: list[dict] | None = None,
    cabal_url: str | None = None,
    memory_replica_bucket: str = "",
    broker_url: str = "",
    liminal_url: str = "",
    archetype: str | None = None,
) -> BundleSpec:
    return BundleSpec(
        name=name,
        persona_body=f"You are {name}, a helpful assistant.\n",
        model_id="vertex_ai/claude-sonnet-4-5",
        skills=skills or [],
        runtime_image=_RUNTIME_IMAGE,
        vertex_project="example-model-project",
        vertex_location="us-east5",
        team=team,
        group=group,
        purpose_tags=purpose_tags if purpose_tags is not None else [],
        capability=capability,
        colleagues=colleagues if colleagues is not None else [],
        cabal_url=cabal_url,
        memory_replica_bucket=memory_replica_bucket,
        broker_url=broker_url,
        liminal_url=liminal_url,
        archetype=archetype,
    )


def test_compose_agent_yaml_parses_with_expected_transport_and_gate(tmp_path):
    """Wire shape: Compose agent yaml parses with expected transport and gate."""
    dest = tmp_path / "out"

    compose(_make_spec(), dest)

    data = yaml.safe_load((dest / "bundle" / "agent.yaml").read_text())
    assert data["transports"] == [{"kind": "a2a", "host": "0.0.0.0", "port": 8080}]
    assert data["tools"] == {"provider": "none", "allow": []}
    assert data["approvals"]["gates"] == [{"match": "*", "require": "plan_approval"}]
    # the placeholder is a literal string pre-validation: not yet substituted
    assert data["model"]["id"] == "${AGENT_MODEL:-vertex_ai/claude-sonnet-4-5}"
    # Truncated-answers fix: the runtime's own default is 8192 -- the room's
    # composed default must match it, not the old 1024 that cut long or
    # thinking-heavy answers off mid-sentence.
    assert data["model"]["max_tokens"] == 8192


def test_compose_agent_yaml_carries_auth_block_when_cabal_url_configured(tmp_path):
    """Wire shape: Compose agent yaml carries auth block when cabal url configured.

    Note: since Task 3/B3, `cabal_url` configured also swaps the `tools:`
    line to the colleagues provider's config block (see the "tools block"
    section below), and since the "fix round: the approval gates" amendment
    it also composes the approvals `gates:` list empty (see the "approvals
    gates" section further below) — this test's pinned text carries both, so
    this single byte-for-byte assertion still proves every OTHER line is
    untouched by any of the three features."""
    dest = tmp_path / "out"

    compose(_make_spec(cabal_url="http://cabal.example.com"), dest)

    text = (dest / "bundle" / "agent.yaml").read_text()
    assert text == (
        "name: sam\n"
        "card: agent-card.json\n"
        "persona: persona.md\n"
        "model: {id: '${AGENT_MODEL:-vertex_ai/claude-sonnet-4-5}', max_tokens: 8192}\n"
        "transports:\n"
        '  - {kind: a2a, host: 0.0.0.0, port: 8080, auth: {jwks_url: '
        '"http://cabal.example.com/.well-known/jwks.json", issuer: "cabal-core", '
        'audience: "sam"}}\n'
        'tools: {provider: colleagues, allow: [colleagues.list_colleagues, '
        'colleagues.ask_colleague, colleagues.remember], colleagues: {url: "http://cabal.example.com", '
        'token_env: CABAL_COLLEAGUE_TOKEN, self: "sam"}}\n'
        "memory: {personal: techne, home: /techne, revision: operator}\n"
        "checkpoints: sqlite\n"
        "approvals:\n"
        "  gates: []\n"
        "  escalation: {route: cli}\n"
    )


def test_compose_agent_yaml_auth_audience_is_the_agent_name(tmp_path):
    """Wire shape: Compose agent yaml auth audience is the agent name."""
    dest = tmp_path / "out"
    spec = BundleSpec(
        name="rae",
        persona_body="You are rae, a helpful assistant.\n",
        model_id="vertex_ai/claude-sonnet-4-5",
        skills=[],
        runtime_image=_RUNTIME_IMAGE,
        vertex_project="example-model-project",
        vertex_location="us-east5",
        cabal_url="http://cabal.example.com",
    )

    compose(spec, dest)

    data = yaml.safe_load((dest / "bundle" / "agent.yaml").read_text())
    auth = data["transports"][0]["auth"]
    assert auth["audience"] == "rae"
    assert auth["issuer"] == "cabal-core"


def test_compose_agent_yaml_with_auth_block_still_validates(tmp_path):
    """Wire shape: Compose agent yaml with auth block still validates.

    The vendored schema's TransportSpec allows additional properties, so
    the new `auth` key doesn't trip schema validation."""
    dest = tmp_path / "out"

    compose(_make_spec(cabal_url="http://cabal.example.com"), dest)

    validate_agent_yaml(dest / "bundle")  # must not raise


def test_compose_agent_yaml_carries_colleagues_tools_block_when_cabal_url_configured(tmp_path):
    """Wire shape: Compose agent yaml carries colleagues tools block when cabal url configured."""
    dest = tmp_path / "out"

    compose(_make_spec(cabal_url="http://cabal.example.com"), dest)

    data = yaml.safe_load((dest / "bundle" / "agent.yaml").read_text())
    assert data["tools"] == {
        "provider": "colleagues",
        "allow": ["colleagues.list_colleagues", "colleagues.ask_colleague", "colleagues.remember"],
        "colleagues": {
            "url": "http://cabal.example.com",
            "token_env": "CABAL_COLLEAGUE_TOKEN",
            "self": "sam",
        },
    }


def test_compose_persona_written_verbatim(tmp_path):
    """Wire shape: Compose persona written verbatim."""
    dest = tmp_path / "out"
    spec = _make_spec()

    compose(spec, dest)

    assert (dest / "bundle" / "persona.md").read_text() == spec.persona_body


def test_compose_roster_is_the_list_that_counts_over_the_live_tool(tmp_path):
    """Wire shape: Compose roster is the list that counts over the live tool.

    Wire contract with the runtime's prompt (issue #274): the roster is
    who the agent may talk to, and the live list_colleagues read may name
    agents it cannot reach; the copy must never tell the agent the live
    tool wins, or it will name destroyed or unpaired agents as colleagues."""
    dest = tmp_path / "out"
    spec = _make_spec(colleagues=[
        {"name": "ward", "team": "infra-engineering", "purpose_tags": [], "capability": "x"},
    ])
    compose(spec, dest)
    text = (dest / "bundle" / "knowledge" / "colleagues.md").read_text()
    assert "who you may talk to" in text
    assert "list_colleagues" in text and "may list agents you cannot reach" in text
    assert "always answers live" not in text


def test_compose_dockerfile_matches_runtime_image(tmp_path):
    """Wire shape: Compose dockerfile matches runtime image."""
    dest = tmp_path / "out"

    compose(_make_spec(), dest)

    assert (dest / "Dockerfile").read_text() == (
        "# Agent image: the shared runtime plus this agent's bundle.\n"
        f"FROM {_RUNTIME_IMAGE}\n"
        "COPY bundle/ /bundle\n"
    )


def test_validate_agent_yaml_raises_compose_error_on_corrupted_transports(tmp_path):
    """Wire shape: Validate agent yaml raises compose error on corrupted transports."""
    dest = tmp_path / "out"
    compose(_make_spec(), dest)

    agent_yaml_path = dest / "bundle" / "agent.yaml"
    data = yaml.safe_load(agent_yaml_path.read_text())
    data["transports"] = "not-a-list"  # schema requires an array
    agent_yaml_path.write_text(yaml.safe_dump(data, sort_keys=False))

    with pytest.raises(ComposeError):
        validate_agent_yaml(dest / "bundle")


def test_bundle_loads_with_real_daedalus_if_available(tmp_path):
    """Wire shape: Bundle loads with real daedalus if available."""
    daedalus = pytest.importorskip("daedalus")
    from daedalus.bundle import Bundle

    dest = tmp_path / "out"
    compose(_make_spec(), dest)

    bundle = Bundle.load(dest / "bundle", env={})

    assert bundle.spec.name == "sam"
    assert bundle.spec.model.id == "vertex_ai/claude-sonnet-4-5"
    assert bundle.spec.transports[0].kind == "a2a"
    assert bundle.spec.transports[0].options() == {"host": "0.0.0.0", "port": 8080}
    del daedalus  # only imported to prove importorskip's module handle works


def test_tools_block_gains_github_sidecar_when_broker_url_set(tmp_path):
    """Wire shape: Tools block gains github sidecar when broker url set."""
    dest = tmp_path / "out"
    compose(
        _make_spec(
            name="kestrel",
            cabal_url="http://cabal.example.com",
            broker_url="http://kobo.example.com",
        ),
        dest,
    )
    text = (dest / "bundle" / "agent.yaml").read_text()
    # The full explicit github allow: the one write plus the read quartet
    # (2026-08-28). Named one by one, never a glob -- the allow list is law.
    for tool in (
        "github.open_pr",
        "github.list_prs",
        "github.get_pr",
        "github.list_releases",
        "github.read_file",
    ):
        assert tool in text
    assert 'broker_url: "http://kobo.example.com"' in text
    assert "token_env: CABAL_COLLEAGUE_TOKEN" in text
    assert 'self: "kestrel"' in text


def test_liminal_block_composes_when_both_coordinates_exist():
    """Wire shape: Liminal block composes when both coordinates exist."""
    from kobo_api.composer import _render_agent_yaml

    spec = _make_spec(cabal_url="http://cabal.test", broker_url="http://kobo.test",
                      liminal_url="http://liminal.test")
    import yaml as _yaml

    data = _yaml.safe_load(_render_agent_yaml(spec))
    tools = data["tools"]
    assert tools["liminal"] == {
        "url": "http://liminal.test",
        "token_env": "CABAL_COLLEAGUE_TOKEN",
        "exchange_url": "http://cabal.test",
    }
    # A birthright, never a grant: no gateway tool is allow-listed.
    assert not any(a.startswith(("scratchpad.", "atlassian.")) for a in tools["allow"])


def test_vendored_schema_is_the_current_copy():
    """Wire shape: Vendored schema is the current copy.

    The vendored daedalus schema is the v0.5.x copy, not the 0.4.0.dev0 one
    kobo carried until 2026-10-18 (issue #152). The newer schema adds the
    `engine` block and the GitHub, CloudTrail, Jira and Slack-read tool specs,
    allows `${VAR}` placeholders, and no longer requires a skill `id`; a stale
    copy would make kobo reject bundles daedalus accepts."""
    schema = json.loads(_VENDOR_SCHEMA_PATH.read_text())
    assert "engine" in schema["properties"]
    for spec in ("EngineSpec", "GitHubSpec", "CloudTrailSpec", "JiraSpec", "SlackReadSpec"):
        assert spec in schema["$defs"], spec


# --- from test_colleague_mode.py ---


ROSTER = [
    {"name": "ada", "team": "sre", "purpose_tags": ["gcp"], "capability": "Knows the GCP estate."}
]


def _spec(*, switchboard: bool = True, **overrides) -> BundleSpec:
    base = {
        "name": "clio",
        "persona_body": "You are clio.\n",
        "model_id": "vertex_ai/claude-sonnet-4-5",
        "skills": [],
        "runtime_image": "registry.example/agents/daedalus-runtime:v1",
        "vertex_project": "p",
        "vertex_location": "us-east5",
    }
    if switchboard:
        base.update(
            cabal_url="https://cabal.example",
            broker_url="http://kobo.svc",
            liminal_url="https://liminal.example",
            colleagues=ROSTER,
            group="g",
            team="t",
            purpose_tags=["a"],
            capability="x",
        )
    base.update(overrides)
    return BundleSpec(**base)


def _compose(tmp_path: Path, spec: BundleSpec) -> Path:
    compose(spec, tmp_path)
    return tmp_path / "bundle"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_default_mode_composes_the_same_bytes_as_before_the_setting(tmp_path):
    """Wire shape: Default mode composes the same bytes as before the setting.

    Acceptance for issue #131: `default` is today's output byte for byte.
    The digests were taken from origin/main 753a46f's composer for these
    exact specs, and retaken on 2026-10-18 when the composer stopped emitting
    `approvals.ceiling.write: false` (a deliberate change to the default
    bundle, agora discussion #55 item 7), and again on 2026-10-29 when the
    roster stopped telling the agent the live list_colleagues read wins
    (issue #274). Nothing in the colleague-mode work may change them."""
    on = _compose(tmp_path / "on", _spec(colleague_mode="default"))
    off = _compose(tmp_path / "off", _spec(switchboard=False, colleague_mode="default"))

    assert {
        "on/agent.yaml": _sha(on / "agent.yaml"),
        "on/colleagues.md": _sha(on / "knowledge" / "colleagues.md"),
        "off/agent.yaml": _sha(off / "agent.yaml"),
        "off/colleagues.md": _sha(off / "knowledge" / "colleagues.md"),
    } == {
        "on/agent.yaml": "1c04bd2c2a0164a145707c665b82ca445ed0ac11f47bf35cf32f971088ec07b5",
        "on/colleagues.md": "d06ee4e0a42fb564e458e3a119460880b0538257a4c92bf6283122ed27544f8d",
        "off/agent.yaml": "aef18cec1a2ce13072097b7c108ed8abb91114087ecd50d26f8b46e4476be5e6",
        "off/colleagues.md": "06be61b3e459caffaf6439d577616128f9f4d42dbc76e315b0411a348d722d86",
    }


# --- from test_steps_wire.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


MODEL_ID = "vertex_ai/claude-sonnet-4-5"


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """WireStep (via _wait_active) and BuildStep both run poll loops; never
    let either sleep for real."""
    import kobo_api.steps as steps_module

    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)

    fake_sleep.calls = sleep_calls
    monkeypatch.setattr(steps_module, "_sleep", fake_sleep)


def _make_agent(
    session_factory,
    *,
    name: str = AGENT_NAME,
    persona: str = PERSONA,
    model_id: str = MODEL_ID,
    repo_full_name: str | None = None,
    gojira_app_id: int | None = None,
    created_by: str | None = None,
):
    session = session_factory()
    agent = Agent(
        name=name,
        persona=persona,
        model_id=model_id,
        skills=[],
        repo_full_name=repo_full_name,
        gojira_app_id=gojira_app_id,
        created_by=created_by,
    )
    session.add(agent)
    session.commit()
    return session, agent


def _make_ctx(session, agent, config, clients) -> Ctx:
    workdir = config.workdir / agent.name
    workdir.mkdir(parents=True, exist_ok=True)
    return Ctx(agent=agent, session=session, config=config, clients=clients, workdir=workdir)


def _clients(*, github=None, gojira=None, kubectl=None, cabal=None, sero=None) -> Clients:
    return Clients(
        github=github, gojira=gojira, cloud=None, agent=None, kubectl=kubectl,
        cabal=cabal, sero=sero,
    )


def _repo_full_name(config) -> str:
    return f"{config.github_org}/agent-{AGENT_NAME}"


WIRE_CABAL_ENDPOINT_TEMPLATE = "http://agent-{name}.default.svc.cluster.local"


def _wire_cabal_enabled_config(config):
    return config.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": WIRE_CABAL_ENDPOINT_TEMPLATE,
        }
    )


def test_wire_step_registers_both_new_manifests_in_kustomization(session_factory, config):
    """Wire shape: Wire step registers both new manifests in kustomization.

    The P0 gotcha, encoded: writing serviceaccount.yaml and pvc.yaml is not
    enough on its own — kustomization.yaml's *resources* list must also gain
    exactly those two new entries, on top of whatever gojira already
    generated there."""
    repo = _repo_full_name(config)
    session, agent = _make_agent(
        session_factory, repo_full_name=repo, gojira_app_id=1
    )
    github = FakeGitHub()
    gojira = FakeGojira(activate_after=1)
    # Pre-seed gojira's fake so app_id 1 exists and can reach active on poll.
    gojira.create_app(name=AGENT_NAME, repo_full_name=repo, cluster=config.gojira_cluster, namespace=AGENT_NAME)
    ctx = _make_ctx(session, agent, config, _clients(github=github, gojira=gojira))

    WireStep().run(ctx)

    kustomization_path = ctx.workdir / "wire" / "k8s" / "base" / "kustomization.yaml"
    data = yaml.safe_load(kustomization_path.read_text())
    assert data["resources"] == [*GENERATED_KUSTOMIZATION_RESOURCES, "serviceaccount.yaml", "pvc.yaml"]


def test_wire_step_edits_gojira_yaml_contract_with_exact_fs_group(session_factory, config):
    """Wire shape: Wire step edits gojira yaml contract with exact fs group."""
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=1)
    github = FakeGitHub()
    gojira = FakeGojira(activate_after=1)
    gojira.create_app(name=AGENT_NAME, repo_full_name=repo, cluster=config.gojira_cluster, namespace=AGENT_NAME)
    ctx = _make_ctx(session, agent, config, _clients(github=github, gojira=gojira))

    WireStep().run(ctx)

    gojira_yaml = yaml.safe_load((ctx.workdir / "wire" / "gojira.yaml").read_text())
    assert gojira_yaml["service_account"] == f"agent-{AGENT_NAME}"
    assert gojira_yaml["env"] == {
        "VERTEXAI_PROJECT": config.vertex_project,
        "VERTEXAI_LOCATION": config.vertex_location,
    }
    assert gojira_yaml["data_mount"] == {
        "pvc": f"agent-{AGENT_NAME}-techne",
        "path": "/techne",
        "fs_group": 10001,
    }
    assert TECHNE_FS_GROUP == 10001
    # gojira.yaml's pre-existing app/class/environments keys survive the edit
    assert gojira_yaml["app"] == AGENT_NAME
    assert gojira_yaml["class"] == "agent"
    assert gojira_yaml["environments"][0]["namespace"] == AGENT_NAME


def test_wire_step_enabled_appends_agent_cabal_to_secrets_from(session_factory, config, monkeypatch):
    """Wire shape: Wire step enabled appends agent cabal to secrets from."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    repo = _repo_full_name(config)
    cfg = _wire_cabal_enabled_config(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=1)
    github = FakeGitHub()
    gojira = FakeGojira(activate_after=1)
    gojira.create_app(name=AGENT_NAME, repo_full_name=repo, cluster=cfg.gojira_cluster, namespace=AGENT_NAME)
    kubectl = FakeKubectl()
    cabal = FakeCabal()
    ctx = _make_ctx(
        session, agent, cfg, _clients(github=github, gojira=gojira, kubectl=kubectl, cabal=cabal)
    )

    WireStep().run(ctx)

    gojira_yaml = yaml.safe_load((ctx.workdir / "wire" / "gojira.yaml").read_text())
    assert gojira_yaml["secrets_from"] == [f"{AGENT_NAME}-cabal"]


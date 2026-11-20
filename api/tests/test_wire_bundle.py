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

from kobo_api.clients import ClientError, Clients
from kobo_api.composer import (
    _VENDOR_SCHEMA_PATH,
    SLACK_READ_TOOLS,
    BundleSpec,
    ComposeError,
    _splice_tools_section,
    compose,
    update_agent_yaml_slack_read,
    validate_agent_yaml,
)
from kobo_api.model_providers import MODELS_SECRET_PURPOSE, gateway_model_name
from kobo_api.models import Agent
from kobo_api.provisioner import Ctx
from kobo_api.steps import TECHNE_FS_GROUP, StagedSkill, WireStep
from tests.fakes import (
    GENERATED_KUSTOMIZATION_RESOURCES,
    HOUSE_RULES,
    HOUSE_WHAT_THEY_DO,
    FakeCabal,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeModelsGateway,
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
        liminal_url=liminal_url,
        archetype=archetype,
    )


def test_compose_agent_yaml_parses_with_expected_transport_and_gate(tmp_path):
    """Wire shape: Compose agent yaml parses with expected transport and gate."""
    dest = tmp_path / "out"

    compose(_make_spec(), dest)

    data = yaml.safe_load((dest / "bundle" / "agent.yaml").read_text())
    assert data["transports"] == [{"kind": "a2a", "host": "0.0.0.0", "port": 8080}]
    # The Slack tools are allowed from birth; they exist only once the agent
    # has a Slack app (issue #484).
    assert data["tools"] == {"provider": "none", "allow": ["slack.*"]}
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
        'colleagues.ask_colleague, colleagues.remember, slack.*], colleagues: {url: "http://cabal.example.com", '
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
        "allow": ["colleagues.list_colleagues", "colleagues.ask_colleague", "colleagues.remember", "slack.*"],
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


def test_tools_block_composes_no_github_sidecar(tmp_path):
    """Wire shape (kobo#365): the pull request broker retired, so a composed
    bundle carries no `github` block and no `github.` allow entry; GitHub reaches
    an agent as gateway tools its permission's lane admits after birth."""
    dest = tmp_path / "out"
    compose(_make_spec(name="kestrel", cabal_url="http://cabal.example.com", liminal_url="http://liminal.example.com"), dest)
    text = (dest / "bundle" / "agent.yaml").read_text()
    tools = yaml.safe_load(text)["tools"]
    assert "github" not in tools and "broker_url" not in text
    assert not any(str(t).startswith("github") for t in tools["allow"])


def test_liminal_block_composes_when_both_coordinates_exist():
    """Wire shape: Liminal block composes when both coordinates exist."""
    from kobo_api.composer import _render_agent_yaml

    spec = _make_spec(cabal_url="http://cabal.test", liminal_url="http://liminal.test")
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
    (issue #274), and once more on 2026-11-01 when the github broker sidecar
    left the bundle (kobo#365), and on 2026-11-14 when the allow list gained
    `slack.*` (issue #484). Nothing in the colleague-mode work may change them."""
    on = _compose(tmp_path / "on", _spec(colleague_mode="default"))
    off = _compose(tmp_path / "off", _spec(switchboard=False, colleague_mode="default"))

    assert {
        "on/agent.yaml": _sha(on / "agent.yaml"),
        "on/colleagues.md": _sha(on / "knowledge" / "colleagues.md"),
        "off/agent.yaml": _sha(off / "agent.yaml"),
        "off/colleagues.md": _sha(off / "knowledge" / "colleagues.md"),
    } == {
        "on/agent.yaml": "c18699c54eff619769d419d28cae469ba60004bd747f57212ef74701def69932",
        "on/colleagues.md": "d06ee4e0a42fb564e458e3a119460880b0538257a4c92bf6283122ed27544f8d",
        "off/agent.yaml": "49506d5ef07c485517abe98c526589df10fbb96e11b1f35298a71b6d1b4a7801",
        "off/colleagues.md": "06be61b3e459caffaf6439d577616128f9f4d42dbc76e315b0411a348d722d86",
    }


def test_the_slack_reading_block_goes_in_and_comes_out_without_moving_anything_else(tmp_path):
    """Wire shape: the reading switch edits one key of the tools block. A
    channel list somebody wrote by hand is kept; an allow list that does not
    already admit the two reading tools gains them by name and loses them
    again; asking for what the file already says changes nothing."""
    bundle = _compose(tmp_path / "on", _spec())
    agent_yaml = bundle / "agent.yaml"
    before = yaml.safe_load(agent_yaml.read_text())

    assert update_agent_yaml_slack_read(bundle, on=False) is False
    assert update_agent_yaml_slack_read(bundle, on=True) is True
    assert update_agent_yaml_slack_read(bundle, on=True) is False
    after = yaml.safe_load(agent_yaml.read_text())
    assert after == {**before, "tools": {**before["tools"], "slack_read": {"channels": []}}}

    # A list written by hand is the owner's narrowing; the switch keeps it.
    narrowed = {**after["tools"], "slack_read": {"channels": ["C0123456789"]}}
    agent_yaml.write_text(agent_yaml.read_text().replace("channels: []", "channels: [C0123456789]"))
    assert yaml.safe_load(agent_yaml.read_text())["tools"] == narrowed
    assert update_agent_yaml_slack_read(bundle, on=True) is False

    assert update_agent_yaml_slack_read(bundle, on=False) is True
    assert yaml.safe_load(agent_yaml.read_text()) == before

    # An allow list with no `slack.*`: the two tools are named, then unnamed.
    allow = [entry for entry in before["tools"]["allow"] if entry != "slack.*"]
    _splice_tools_section(agent_yaml, agent_yaml.read_text(), {**before["tools"], "allow": allow})
    assert update_agent_yaml_slack_read(bundle, on=True) is True
    assert yaml.safe_load(agent_yaml.read_text())["tools"]["allow"] == [*allow, *SLACK_READ_TOOLS]
    assert update_agent_yaml_slack_read(bundle, on=False) is True
    assert yaml.safe_load(agent_yaml.read_text())["tools"] == {**before["tools"], "allow": allow}

    # A tools section written out by hand, with a blank line inside it, is
    # replaced whole: nothing of it is left behind under the new line.
    by_hand = "name: clio\ntools:\n  provider: none\n  allow:\n  - slack.*\n\n  slack_read:\n    channels: [C0123456789]\ntransports: []\n"
    agent_yaml.write_text(by_hand)
    assert update_agent_yaml_slack_read(bundle, on=False) is True
    assert yaml.safe_load(agent_yaml.read_text()) == {
        "name": "clio", "tools": {"provider": "none", "allow": ["slack.*"]}, "transports": [],
    }
    # One the room cannot span (a comment at the margin, mid-section) is
    # refused and left exactly as it was, never written half-replaced.
    broken_up = by_hand.replace("\n\n  slack_read", "\n# reading\n  slack_read")
    agent_yaml.write_text(broken_up)
    with pytest.raises(ClientError):
        update_agent_yaml_slack_read(bundle, on=False)
    assert agent_yaml.read_text() == broken_up


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


def _clients(*, github=None, gojira=None, kubectl=None, cabal=None, sero=None, models_gateway=None) -> Clients:
    return Clients(
        github=github, gojira=gojira, cloud=None, agent=None, kubectl=kubectl,
        cabal=cabal, sero=sero, models_gateway=models_gateway,
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


def test_wire_step_puts_the_allow_entries_of_held_lanes_into_the_bundle(session_factory, config):
    """Wire shape (kobo#365 lanes at birth): a permission the agent already holds when it
    is wired (Read on its own repository, granted by the Access step) reaches the bundle
    here, in the wiring commit, so the runtime offers the gateway tools without a second
    commit and rebuild; an agent that holds nothing keeps its allow list as composed."""
    from tests.fakes import FakeBab

    repo = _repo_full_name(config)
    agent_yaml = ("name: x\ntransports:\n  - {kind: a2a, host: 0.0.0.0, port: 8080}\n"
                  "tools: {provider: colleagues, allow: [colleagues.list_colleagues]}\n")
    connection = {"integration": "github", "vendor": "github", "kind": "connection", "name": "GitHub",
                  "ceiling": ["github_repos_read"], "spec": {"installation_id": "1"}}
    child = {"integration": f"github-{AGENT_NAME}", "vendor": "github", "kind": "account", "parent": "github",
             "ceiling": ["github_repos_read"], "spec": {"repositories": [repo]}}
    bab = FakeBab(integrations=[connection, child])
    bab.decide([bab.request(AGENT_NAME, f"github-{AGENT_NAME}", ["github_repos_read"], None, "room")["id"]], "approve", None, "room")

    for holds, name in ((True, AGENT_NAME), (False, "ivy")):
        own_repo = repo if holds else f"{config.github_org}/agent-ivy"
        session, agent = _make_agent(session_factory, name=name, repo_full_name=own_repo, gojira_app_id=1)
        github = FakeGitHub(extra_clone_files={"bundle/agent.yaml": agent_yaml})
        gojira = FakeGojira(activate_after=1)
        gojira.create_app(name=name, repo_full_name=own_repo, cluster=config.gojira_cluster, namespace=name)
        clients = _clients(github=github, gojira=gojira)
        clients.bab = bab if holds else FakeBab(integrations=[connection])
        ctx = _make_ctx(session, agent, config, clients)

        WireStep().run(ctx)

        allow = yaml.safe_load((ctx.workdir / "wire" / "bundle" / "agent.yaml").read_text())["tools"]["allow"]
        assert allow == (["colleagues.list_colleagues", "github-mcp.*"] if holds else ["colleagues.list_colleagues"])


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



# --- the model gateway (kobo#390) -------------------------------------------------

MODELS_GATEWAY_URL = "http://models-gateway.models.svc"


def test_compose_writes_the_gateway_model_block_when_the_gateway_is_on(tmp_path):
    """Wire shape (kobo#390): an agent born with the gateway on thinks through
    it: an OpenAI-compatible id under the gateway's name for the model, the
    gateway's `/v1`, and the key's env name; no `${AGENT_MODEL}` placeholder,
    and the bundle still validates against the runtime's schema."""
    spec = _make_spec()
    spec.models_gateway_url, spec.gateway_model = MODELS_GATEWAY_URL, "claude-affordable-sonnet"

    compose(spec, tmp_path / "out")

    validate_agent_yaml(tmp_path / "out" / "bundle")
    assert yaml.safe_load((tmp_path / "out" / "bundle" / "agent.yaml").read_text())["model"] == {
        "id": "openai/claude-affordable-sonnet", "max_tokens": 8192,
        "api_base": f"{MODELS_GATEWAY_URL}/v1", "api_key_env": "MODELS_GATEWAY_KEY",
    }


def test_wire_step_delivers_the_gateway_key_and_names_its_secret(session_factory, config):
    """Wire shape (kobo#390): with the gateway on, WireStep mints the agent's
    key for every model the connected providers serve, delivers it as the
    Secret `<agent>-models` under `MODELS_GATEWAY_KEY` before the push, and
    gojira.yaml's `secrets_from` names that Secret. The key value reaches
    the Secret and nothing else the step writes."""
    from kobo_api import admin_store

    cfg = config.model_copy(update={"models_gateway_url": MODELS_GATEWAY_URL})
    repo = _repo_full_name(cfg)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=1, created_by="owner@example.com")
    admin_store.put_integration(
        session, vendor="gcp", name="Google Cloud example", base_access="read", integration_id="gcp-example",
        spec={"kind": "account", "project": "example-model-project", "models": {"provider": "vertex_ai", "location": "us-east5"}},
        credential_ref=None, actor="ops@example.com",
    )
    admin_store.set_integration_state(session, "gcp-example", state=admin_store.STATE_APPLIED, detail="connected")
    session.commit()
    github, gojira, kubectl, gateway = FakeGitHub(), FakeGojira(activate_after=1), FakeKubectl(), FakeModelsGateway()
    gojira.create_app(name=AGENT_NAME, repo_full_name=repo, cluster=cfg.gojira_cluster, namespace=AGENT_NAME)
    ctx = _make_ctx(session, agent, cfg, _clients(github=github, gojira=gojira, kubectl=kubectl, models_gateway=gateway))

    WireStep().run(ctx)

    gojira_yaml = yaml.safe_load((ctx.workdir / "wire" / "gojira.yaml").read_text())
    assert gojira_yaml["secrets_from"] == [f"{AGENT_NAME}-{MODELS_SECRET_PURPOSE}"]
    # A gateway agent's model access is the key; the two Vertex variables are the direct path's.
    assert not {"VERTEXAI_PROJECT", "VERTEXAI_LOCATION"} & set(gojira_yaml.get("env") or {})
    [minted] = gateway.keys[f"agent:{AGENT_NAME}"]
    assert minted["models"] == [gateway_model_name(m) for m in cfg.models]
    assert kubectl.secrets[(AGENT_NAME, f"{AGENT_NAME}-models")] == {"MODELS_GATEWAY_KEY": minted["key"]}
    assert minted["key"] not in (ctx.workdir / "wire" / "gojira.yaml").read_text()


# --- guardrails (issue #524 step 2) ---


GUARDRAILS_REPO = "acme-corp/agent-gate"
_COMPLETED_RELAY = {"status": {"state": "completed", "message": {"parts": [{"text": "ok"}]}}}


def _guardrails_room(tmp_path, monkeypatch, *, relay_error=None):
    """A room with the switchboard wired and one ready composed agent, `gate`."""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from kobo_api.config import Config
    from kobo_api.main import create_app
    from kobo_api.models import AgentStatus, Base

    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()
    session.add(Agent(name="gate", persona="helpful-generalist", model_id="m", skills=[],
                      status=AgentStatus.READY, repo_full_name=GUARDRAILS_REPO))
    session.commit()
    session.close()
    github = FakeGitHub(existing_repos={GUARDRAILS_REPO})
    cabal = FakeCabal(relay_result=_COMPLETED_RELAY, relay_error=relay_error)
    config = Config(workdir=tmp_path / "work", cabal_url="http://cabal.internal",
                    agent_endpoint_template=WIRE_CABAL_ENDPOINT_TEMPLATE, guardrails=True)
    clients = Clients(github=github, gojira=FakeGojira(), cloud=None, agent=None,
                      kubectl=FakeKubectl(), cabal=cabal)
    return TestClient(create_app(session_factory, config=config, clients=clients)), github, cabal


RULES = ["Never change access without an approved ticket.", "Answer only in the request thread."]


def test_a_guardrails_save_relays_and_archives_the_same_bytes(tmp_path, monkeypatch):
    """Wire shape (the runtime's staleness protocol, daedalus.guardrails): the file the room
    archives is byte for byte the text it relayed. The runtime keeps a live write's sha and
    compares the bundle's against it; different bytes would read as a newer edit made in
    the repository, and the agent would drop the list it was just given."""
    client, github, cabal = _guardrails_room(tmp_path, monkeypatch)

    response = client.put(
        "/api/agents/gate/guardrails", json={"rules": [{"text": rule} for rule in RULES]}
    )

    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "instant"
    [call] = cabal.relay_calls
    envelope = call["task_envelope"]
    assert envelope["metadata"] == {"system_task": "guardrails"}
    relayed = envelope["status"]["message"]["parts"][0]["text"]
    assert github.pushed_file("bundle/guardrails.md") == relayed
    assert relayed.endswith("- Never change access without an approved ticket.\n"
                            "- Answer only in the request thread.\n")
    assert github.pushes[-1][1].endswith("[skip ci]")


PEOPLE = [
    {"name": "Ada Lovelace", "email": "ada@example.com"},
    {"name": "Alan Turing", "email": "Alan@Example.com"},
]


def test_a_rule_names_its_people_on_its_own_line(tmp_path, monkeypatch):
    """Wire shape (daedalus's rules intro and its Slack sender line, `(From Name <email>.)`):
    the people a rule is about ride on the rule's own line, after its words, as
    `(These people: Name <email>; ...)`. The runtime puts the line in front of the model
    as written and names each message's sender the same way, so the model matches the
    two by email. The panel reads the words and the people apart, and the line whole."""
    client, github, _ = _guardrails_room(tmp_path, monkeypatch)

    response = client.put("/api/agents/gate/guardrails", json={"rules": [
        {"text": "Only answer these people.", "people": PEOPLE},
        {"text": RULES[1]},
    ]})

    assert response.status_code == 200, response.text
    line = ("Only answer these people. (These people: Ada Lovelace <ada@example.com>; "
            "Alan Turing <Alan@Example.com>)")
    assert github.pushed_file("bundle/guardrails.md").endswith(f"- {line}\n- {RULES[1]}\n")
    first, second = response.json()["rules"]
    assert first["text"] == "Only answer these people." and first["line"] == line
    assert first["people"] == PEOPLE
    assert second["people"] == [] and second["line"] == RULES[1]


def test_an_assistant_keeps_a_rules_people_by_sending_its_line(tmp_path, monkeypatch):
    """Wire shape (`kobo_set_guardrails` takes plain lines): a rule's `line`, people
    included, sent back as its text keeps the people. People sent both ways at once are
    refused, and so is an address that isn't one; nothing is written either time. A line
    from the repository whose ending only looks like a list of people reads, and saves,
    as written."""
    from kobo_api.guardrails import render_file

    client, github, _ = _guardrails_room(tmp_path, monkeypatch)
    line = "Only answer these people. (These people: Ada Lovelace <ada@example.com>)"

    kept = client.put("/api/agents/gate/guardrails", json={"rules": [{"text": line}]})
    pushes = len(github.pushes)
    twice = client.put("/api/agents/gate/guardrails", json={"rules": [
        {"text": line, "people": [PEOPLE[1]]},
    ]})
    not_an_email = client.put("/api/agents/gate/guardrails", json={"rules": [
        {"text": "Only answer them.", "people": [{"name": "Ada", "email": "ada"}]},
    ]})
    crowd = [{"name": "A" * 60, "email": f"person{i}@example.com"} for i in range(20)]
    too_long = client.put("/api/agents/gate/guardrails", json={"rules": [
        {"text": "Only answer these people.", "people": crowd},
    ]})
    written = "Keep it short (These people: the on-call team)"
    github._repo_files = {"bundle/guardrails.md": render_file([written])}
    read = client.get("/api/agents/gate/guardrails").json()["rules"]
    resaved = client.put("/api/agents/gate/guardrails", json={"rules": [
        {"text": rule["text"], "people": rule["people"]} for rule in read
    ]})

    assert kept.status_code == 200, kept.text
    assert kept.json()["rules"][0]["people"] == [{"name": "Ada Lovelace", "email": "ada@example.com"}]
    assert twice.status_code == 422 and twice.json() == {"error": "a rule names each list once"}
    assert not_an_email.status_code == 422
    assert not_an_email.json() == {"error": "ada isn't an email address"}
    assert too_long.status_code == 422
    assert too_long.json()["error"].startswith("a rule and its people are at most 1,000 characters")
    assert len(github.pushes) == pushes + 1  # the resave; nothing for the three refusals
    assert read[0]["text"] == written and read[0]["people"] == []
    assert resaved.status_code == 200, resaved.text
    assert github.pushed_file("bundle/guardrails.md").endswith(f"- {written}\n")


def test_a_rule_names_people_emoji_channels_and_repositories_on_its_line(tmp_path, monkeypatch):
    """Wire shape (daedalus's rules intro and sender line, which names the channel too):
    a rule's lists ride on its own line in one order, people, emoji, channels,
    repositories, each at most once, written the way the runtime names them: emoji as
    Slack's reaction events do (thumbsup is +1), channels by name with a #, repositories
    as owner/name. The panel reads each list apart and the line whole; an assistant
    sending the line back keeps them all."""
    client, github, _ = _guardrails_room(tmp_path, monkeypatch)

    saved = client.put("/api/agents/gate/guardrails", json={"rules": [{
        "text": "Act only on these.",
        "people": [PEOPLE[0]],
        "emoji": [":thumbsup:", "ticket"],
        "channels": ["#SRE-Requests", "ops"],
        "repositories": ["acme/api"],
    }]})
    line = ("Act only on these. (These people: Ada Lovelace <ada@example.com>) "
            "(These emoji: :+1:, :ticket:) (These channels: #sre-requests, #ops) "
            "(These repositories: acme/api)")
    resent = client.put("/api/agents/gate/guardrails", json={"rules": [{"text": line}]})
    bad = [
        client.put("/api/agents/gate/guardrails", json={"rules": [{"text": "X.", **body}]}).json()
        for body in ({"emoji": ["not an emoji"]}, {"channels": ["sre requests"]},
                     {"repositories": ["acme"]})
    ]

    assert saved.status_code == 200, saved.text
    assert github.pushed_file("bundle/guardrails.md").endswith(f"- {line}\n")
    rule = saved.json()["rules"][0]
    assert rule["text"] == "Act only on these." and rule["line"] == line
    assert rule["emoji"] == ["+1", "ticket"]
    assert rule["channels"] == ["sre-requests", "ops"]
    assert rule["repositories"] == ["acme/api"]
    assert resent.status_code == 200 and resent.json()["rules"][0]["line"] == line
    assert [body["error"].split(";")[0] for body in bad] == [
        "not an emoji isn't an emoji name",
        "sre requests isn't a Slack channel name",
        "acme isn't a repository",
    ]


def test_changing_a_rules_people_keeps_where_it_came_from(tmp_path, monkeypatch):
    """Wire shape (an assistant sends lines, no ids): a rule whose people change keeps
    its row, so it still says where it came from and how often it broke, and the
    panel's "keeps breaking" note does not reset; the person who changed it is
    recorded as its editor."""
    client, _, _ = _guardrails_room(tmp_path, monkeypatch)
    words = "Only answer these people."
    saved = client.put("/api/agents/gate/guardrails", json={"rules": [
        {"text": words, "people": [PEOPLE[0]], "source_kind": "persona",
         "source_ref": "helpful-generalist"},
    ]}).json()["rules"][0]
    client.post(f"/api/agents/gate/guardrails/{saved['id']}/broken")

    changed = client.put("/api/agents/gate/guardrails", json={"rules": [
        {"text": f"{words} (These people: Ada Lovelace <ada@example.com>; Alan Turing <alan@example.com>)"},
    ]}).json()["rules"][0]

    assert changed["id"] == saved["id"]
    assert changed["source"] == {"kind": "persona", "ref": "helpful-generalist"}
    assert changed["breaks"] == 1 and changed["edited_by"]
    assert [p["email"] for p in changed["people"]] == ["ada@example.com", "alan@example.com"]


def test_without_a_relay_the_commit_is_the_delivery(tmp_path, monkeypatch):
    """Wire shape: an agent the switchboard cannot reach (or a runtime from before
    guardrails) gets the list by an ordinary commit, which keeps CI so the next image
    carries it; the archive marker would leave it on the old list."""
    client, github, _ = _guardrails_room(tmp_path, monkeypatch, relay_error="unreachable")

    response = client.put(
        "/api/agents/gate/guardrails", json={"rules": [{"text": rule} for rule in RULES]}
    )

    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "archive"
    assert "- Answer only in the request thread.\n" in github.pushed_file("bundle/guardrails.md")
    assert "[skip ci]" not in github.pushes[-1][1]


def test_the_room_never_writes_a_list_the_runtime_would_cut(tmp_path, monkeypatch):
    """Wire shape (daedalus.guardrails RULES_MAX_CHARS): the runtime cuts a list past
    4,000 characters from the bottom; the room refuses one instead, so what a person
    saved is what the agent follows. Nothing is relayed or pushed."""
    client, github, cabal = _guardrails_room(tmp_path, monkeypatch)
    rule = "Check the approval record before every change, and say which record you used"

    response = client.put(
        "/api/agents/gate/guardrails",
        json={"rules": [{"text": f"{rule} ({i})."} for i in range(60)]},
    )

    assert response.status_code == 422
    assert "4,000-character limit" in response.json()["error"]
    assert cabal.relay_calls == [] and github.pushes == []


def test_a_room_whose_runtime_does_not_read_guardrails_saves_none(tmp_path, monkeypatch):
    """Wire shape (issue #524, the rollout gate): with `guardrails` off the room's runtime
    predates the file, so a saved list would be followed by nobody. The list reads as not
    enabled and a save is refused, with nothing relayed or pushed."""
    client, github, cabal = _guardrails_room(tmp_path, monkeypatch)
    client.app.state.config = client.app.state.config.model_copy(update={"guardrails": False})

    read = client.get("/api/agents/gate/guardrails").json()
    saved = client.put("/api/agents/gate/guardrails", json={"rules": [{"text": RULES[0]}]})

    assert read["enabled"] is False and read["can_edit"] is False
    assert saved.status_code == 409 and "doesn't read guardrails" in saved.json()["error"]
    assert cabal.relay_calls == [] and github.pushes == []


def _born(session_factory, config, fake_storage, *, guardrails: bool):
    """Create agent `clio` (persona helpful-generalist, ten rules) through RepoStep and
    return its composed bundle directory and the room's session."""
    from kobo_api.steps import RepoStep

    config = config.model_copy(update={"guardrails": guardrails})
    session, agent = _make_agent(session_factory)
    clients = Clients(github=FakeGitHub(), gojira=FakeGojira(), cloud=None, agent=None,
                      kubectl=FakeKubectl(), storage=fake_storage)
    ctx = _make_ctx(session, agent, config, clients)
    RepoStep().run(ctx)
    return ctx.workdir / "repo" / "bundle", session


def test_a_new_agents_persona_rules_start_its_guardrails(session_factory, config, fake_storage):
    """Wire shape (what the room writes at birth, issue #524): with guardrails on, the
    persona's rules move into `bundle/guardrails.md`, each recorded as from that persona,
    and persona.md keeps who the agent is and what it does with no Rules section, so a
    rule lives in one place."""
    from kobo_api.guardrails import parse_rules
    from kobo_api.models import GuardrailRule

    bundle, session = _born(session_factory, config, fake_storage, guardrails=True)

    rules = parse_rules((bundle / "guardrails.md").read_text())
    persona = (bundle / "persona.md").read_text()
    assert len(rules) == 10
    assert "## Rules" not in persona and "## What you do" in persona
    rows = session.query(GuardrailRule).filter_by(agent_name=AGENT_NAME).all()
    assert sorted(row.text for row in rows) == sorted(rules)
    assert {(row.source_kind, row.source_ref) for row in rows} == {("persona", PERSONA)}


def test_without_guardrails_a_new_agent_keeps_its_persona_rules(session_factory, config, fake_storage):
    """Wire shape (the rollout gate, issue #524): a room whose runtime does not read
    guardrails composes exactly as before, rules in the persona and no guardrails file,
    because that runtime would follow none of them from the file."""
    bundle, _ = _born(session_factory, config, fake_storage, guardrails=False)

    assert "## Rules" in (bundle / "persona.md").read_text()
    assert not (bundle / "guardrails.md").exists()


@pytest.mark.parametrize("has_list", [True, False], ids=["with-guardrails", "from-before-guardrails"])
def test_a_persona_swap_leaves_an_agents_rules_where_they_live(tmp_path, monkeypatch, fake_storage, has_list):
    """Wire shape (issue #524): swapping the persona of an agent whose rules live in its
    guardrails writes persona.md without the new persona's Rules section and leaves the
    list alone; an agent from before guardrails, with no list yet, keeps the persona's
    rules, which would otherwise be lost."""
    from kobo_api.guardrails import render_file
    from tests.conftest import PERSONAS_BUCKET

    client, github, _ = _guardrails_room(tmp_path, monkeypatch)
    client.app.state.config = client.app.state.config.model_copy(
        update={"personas_bucket": PERSONAS_BUCKET}
    )
    client.app.state.clients.storage = fake_storage
    if has_list:
        github._repo_files = {"bundle/guardrails.md": render_file(["Answer in the thread."])}

    response = client.put("/api/agents/gate/persona", json={"persona": "identity-admin"})

    assert response.status_code == 200, response.text
    persona = github.pushed_file("bundle/persona.md")
    assert ("## Rules" in persona) is not has_list
    assert "## What you do" in persona
    assert len(github.pushes) == 1  # the persona file alone; the list is not rewritten


# --- One agent's own persona, read and edited in place (2026-11-20) ---------


def _house_persona(agent_name: str, *, rules: list[str]) -> str:
    """An agent's persona.md as the room writes it at birth: the catalog render with
    the agent's name where `{name}` was (`resolve_persona_body`)."""
    from kobo_api.personas import PersonaFields, render_persona

    fields = PersonaFields(
        name="alert scribe",
        who_they_are="a careful keeper of the team's alerts",
        what_they_do=HOUSE_WHAT_THEY_DO,
        rules=rules,
    )
    return render_persona(fields).replace("{name}", agent_name)


@pytest.mark.parametrize("has_list", [True, False], ids=["with-guardrails", "from-before-guardrails"])
def test_an_agents_persona_reads_back_from_its_own_file(tmp_path, monkeypatch, has_list):
    """Wire shape (2026-11-20): the Persona section shows the persona the agent runs,
    read apart from its own persona.md, whose opening line names the agent. Its rules
    are said to live in the persona only while the agent has no guardrails file."""
    from kobo_api.guardrails import render_file

    client, github, _ = _guardrails_room(tmp_path, monkeypatch)
    github._repo_files = {"bundle/persona.md": _house_persona("gate", rules=list(HOUSE_RULES))}
    if has_list:
        github._repo_files["bundle/guardrails.md"] = render_file(["Answer in the thread."])

    body = client.get("/api/agents/gate/persona").json()

    assert body["persona"] == "helpful-generalist" and body["house_shape"] is True
    assert body["who_they_are"] == "a careful keeper of the team's alerts"
    assert body["what_they_do"] == HOUSE_WHAT_THEY_DO.strip("\n")
    assert body["rules"] == list(HOUSE_RULES) and body["text"] is None
    assert body["rules_in_persona"] is not has_list
    assert body["can_edit"] is True


def test_an_edit_rewrites_this_agents_persona_and_keeps_its_rules_where_they_live(tmp_path, monkeypatch):
    """Wire shape (2026-11-20): Edit persona writes the agent's own persona.md the way
    the room writes it at birth, in one commit that rebuilds the agent. An agent whose
    rules live in its Guardrails gains no Rules section; the edit cannot add rules to it
    either, and a refused edit writes nothing."""
    from kobo_api.guardrails import render_file

    client, github, _ = _guardrails_room(tmp_path, monkeypatch)
    github._repo_files = {
        "bundle/persona.md": _house_persona("gate", rules=[]),
        "bundle/guardrails.md": render_file(["Answer in the thread."]),
    }
    new_what = HOUSE_WHAT_THEY_DO + "\nTriage pages from the payments API first."

    refused = client.put(
        "/api/agents/gate/persona/text",
        json={"who_they_are": "a triager", "what_they_do": new_what, "rules": ["Never guess."]},
    )
    assert refused.status_code == 422 and "Guardrails" in refused.json()["error"]
    assert github.pushes == []

    response = client.put(
        "/api/agents/gate/persona/text",
        json={"who_they_are": "You are a calm triager of payments alerts.", "what_they_do": new_what},
    )

    assert response.status_code == 200, response.text
    assert response.json()["persona"] == "helpful-generalist" and response.json()["sha"]
    persona = github.pushed_file("bundle/persona.md")
    assert persona.splitlines()[0] == "You are gate, a calm triager of payments alerts."
    assert "Triage pages from the payments API first." in persona and "## Rules" not in persona
    assert [message for _, message in github.pushes] == ["kobo: edit persona"]


def test_an_agent_from_before_guardrails_keeps_and_edits_its_persona_rules(tmp_path, monkeypatch):
    """Wire shape (2026-11-20): an agent with no guardrails file keeps its rules in its
    persona, so an edit that leaves them out keeps them as they are, and one that sends
    them replaces them. Neither runs into the catalog's three-rule floor: these are
    this agent's rules."""
    client, github, _ = _guardrails_room(tmp_path, monkeypatch)
    github._repo_files = {"bundle/persona.md": _house_persona("gate", rules=list(HOUSE_RULES))}

    kept = client.put(
        "/api/agents/gate/persona/text",
        json={"who_they_are": "a triager", "what_they_do": HOUSE_WHAT_THEY_DO},
    )
    assert kept.status_code == 200, kept.text
    assert github.pushed_file("bundle/persona.md").endswith(
        "## Rules\n\n" + "".join(f"- {rule}\n" for rule in HOUSE_RULES)
    )

    github._repo_files = {"bundle/persona.md": github.pushed_file("bundle/persona.md")}
    replaced = client.put(
        "/api/agents/gate/persona/text",
        json={"who_they_are": "a triager", "what_they_do": HOUSE_WHAT_THEY_DO, "rules": ["Never guess."]},
    )
    assert replaced.status_code == 200, replaced.text
    assert github.pushed_file("bundle/persona.md").endswith("## Rules\n\n- Never guess.\n")


def test_a_persona_not_in_the_house_shape_is_read_and_edited_as_one_text(tmp_path, monkeypatch):
    """Wire shape (2026-11-20): a persona the room did not write in its shape (an older
    built-in's plain prose) has no parts to show, so it is read and edited whole, and
    an edit in parts is refused rather than writing over text the form never showed.
    The whole text is saved as given."""
    client, github, _ = _guardrails_room(tmp_path, monkeypatch)
    prose = "You are gate, a helpful assistant. Be concise.\n\nAnswer from what you know.\n"
    github._repo_files = {"bundle/persona.md": prose}

    body = client.get("/api/agents/gate/persona").json()
    assert body["house_shape"] is False and body["text"] == prose
    assert body["who_they_are"] is None and body["rules"] == []

    in_parts = client.put(
        "/api/agents/gate/persona/text",
        json={"who_they_are": "a helper", "what_they_do": HOUSE_WHAT_THEY_DO},
    )
    assert in_parts.status_code == 422 and "one text" in in_parts.json()["error"]
    assert github.pushes == []

    whole = client.put(
        "/api/agents/gate/persona/text", json={"text": prose + "Be brief.\n"}
    )
    assert whole.status_code == 200, whole.text
    assert github.pushed_file("bundle/persona.md") == prose + "Be brief.\n"


def test_a_hand_edited_house_persona_is_edited_whole_and_loses_nothing(tmp_path, monkeypatch):
    """Wire shape (2026-11-20 review): parts are offered only when editing them loses
    nothing. A persona in the house shape that someone edited by hand (a line under
    the opening, a rule in another list style, a paragraph after the rules) would be
    cut down to what the form reads, so it comes back as one text, and saving that
    text writes it back byte for byte."""
    client, github, _ = _guardrails_room(tmp_path, monkeypatch)
    born = _house_persona("gate", rules=["Never guess."])
    lines = born.splitlines()
    lines.insert(1, "Answer the payments team first.")
    edited = "\n".join(lines) + "\n* Say what you checked.\n\nWhen unsure, ask in the thread.\n"
    github._repo_files = {"bundle/persona.md": edited}

    body = client.get("/api/agents/gate/persona").json()
    assert body["house_shape"] is False and body["text"] == edited

    saved = client.put("/api/agents/gate/persona/text", json={"text": body["text"]})
    assert saved.status_code == 200, saved.text
    assert github.pushed_file("bundle/persona.md") == edited


def test_the_floor_and_a_pasted_opening_on_an_edit_in_parts(tmp_path, monkeypatch):
    """Wire shape (2026-11-20): the floor on what-they-do holds for an edit in parts,
    and a who-line pasted as the section shows it ("You are gate, ...") does not
    write the agent's name twice, in any case, with or without the comma."""
    client, github, _ = _guardrails_room(tmp_path, monkeypatch)
    github._repo_files = {"bundle/persona.md": _house_persona("gate", rules=[])}

    thin = client.put(
        "/api/agents/gate/persona/text",
        json={"who_they_are": "a helper", "what_they_do": "Helps with alerts."},
    )
    assert thin.status_code == 422 and "too short to work from" in thin.json()["error"]

    for pasted in ("You are gate, a calm triager.", "you are GATE a calm triager"):
        saved = client.put(
            "/api/agents/gate/persona/text",
            json={"who_they_are": pasted, "what_they_do": HOUSE_WHAT_THEY_DO},
        )
        assert saved.status_code == 200, saved.text
        assert github.pushed_file("bundle/persona.md").splitlines()[0] == (
            "You are gate, a calm triager."
        )


def test_a_persona_edit_from_a_stale_copy_is_refused(tmp_path, monkeypatch):
    """Wire shape (2026-11-20 review): a save names the version it was read at. When
    the file moved since (another edit, a swap, a hand edit), the save is refused and
    nothing is written, so nobody's change is lost; the version read now saves."""
    client, github, _ = _guardrails_room(tmp_path, monkeypatch)
    github._repo_files = {"bundle/persona.md": _house_persona("gate", rules=[])}
    opened = client.get("/api/agents/gate/persona").json()["sha"]
    github._repo_files = {"bundle/persona.md": _house_persona("gate", rules=["Never guess."])}

    change = {"who_they_are": "a triager", "what_they_do": HOUSE_WHAT_THEY_DO}
    stale = client.put("/api/agents/gate/persona/text", json={**change, "base_sha": opened})

    assert stale.status_code == 409
    assert stale.json()["error"].startswith("the persona changed since you opened it")
    assert github.pushes == []
    current = client.get("/api/agents/gate/persona").json()["sha"]
    fresh = client.put("/api/agents/gate/persona/text", json={**change, "base_sha": current})
    assert fresh.status_code == 200, fresh.text


def test_a_persona_reads_without_a_rules_box_when_the_guardrails_file_cannot_be_read(
    tmp_path, monkeypatch
):
    """Wire shape (2026-11-20 review): reading the persona does not fail because the
    guardrails file could not be read; it says the rules are not the persona's, so no
    rules box is offered that could put a rule in two places. The edit itself still
    refuses until it can tell."""
    from kobo_api.clients import ClientError as RoomClientError
    from kobo_api.guardrails import GUARDRAILS_PATH

    client, github, _ = _guardrails_room(tmp_path, monkeypatch)
    github._repo_files = {"bundle/persona.md": _house_persona("gate", rules=[])}
    read_file = github.read_file

    def guardrails_unreadable(repo, path, ref=None):
        if path == GUARDRAILS_PATH:
            raise RoomClientError("down")
        return read_file(repo, path, ref)

    github.read_file = guardrails_unreadable

    body = client.get("/api/agents/gate/persona")
    assert body.status_code == 200, body.text
    assert body.json()["rules_in_persona"] is False and body.json()["house_shape"] is True
    edit = client.put(
        "/api/agents/gate/persona/text",
        json={"who_they_are": "a triager", "what_they_do": HOUSE_WHAT_THEY_DO},
    )
    assert edit.status_code == 502 and github.pushes == []

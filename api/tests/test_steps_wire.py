"""Tests for kobo_api.steps: WireStep, BuildStep, DeployStep.

WireStep is the highest-stakes step in the pipeline: it encodes the P0 live
E2E finding that gojira's generator *preserves* kustomization.yaml entries
rather than discovering files on disk, so an app-authored manifest that isn't
also registered there is silently never applied. Every happy-path test here
therefore asserts on the *parsed* kustomization.yaml resources list, not just
that the manifest files exist — that's the assertion that would have caught
the P0 defect (a missing ServiceAccount) before it ever reached a cluster.

Same no-real-sleep discipline as test_steps.py: `_sleep` is monkeypatched to a
no-op so BuildStep's 900s/10s poll (90 iterations at worst) and WireStep's
reused onboarding poll never actually block.
"""

from __future__ import annotations

import pytest
import yaml

from kobo_api.clients import CabalError, ClientError, Clients
from kobo_api.config import Config, ModelChoice
from kobo_api.models import Agent
from kobo_api.provisioner import Ctx
from kobo_api.steps import (
    BUILD_MAX_HEAD_ADVANCES,
    BUILD_POLL_INTERVAL_S,
    BUILD_POLL_TIMEOUT_S,
    TECHNE_FS_GROUP,
    BuildStep,
    DeployStep,
    WireStep,
)
from tests.fakes import (
    GENERATED_KUSTOMIZATION_RESOURCES,
    FakeCabal,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeLadon,
)

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


def _clients(*, github=None, gojira=None, kubectl=None, cabal=None, ladon=None) -> Clients:
    return Clients(
        github=github, gojira=gojira, cloud=None, agent=None, kubectl=kubectl,
        cabal=cabal, ladon=ladon,
    )


def _repo_full_name(config) -> str:
    return f"{config.github_org}/agent-{AGENT_NAME}"


# --- WireStep -------------------------------------------------------------------


def test_wire_step_registers_both_new_manifests_in_kustomization(session_factory, config):
    """The P0 gotcha, encoded: writing serviceaccount.yaml and pvc.yaml is not
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


def test_wire_step_writes_both_manifests_to_disk(session_factory, config):
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=1)
    github = FakeGitHub()
    gojira = FakeGojira(activate_after=1)
    gojira.create_app(name=AGENT_NAME, repo_full_name=repo, cluster=config.gojira_cluster, namespace=AGENT_NAME)
    ctx = _make_ctx(session, agent, config, _clients(github=github, gojira=gojira))

    WireStep().run(ctx)

    base = ctx.workdir / "wire" / "k8s" / "base"
    sa_text = (base / "serviceaccount.yaml").read_text()
    sa = yaml.safe_load(sa_text)
    assert sa["kind"] == "ServiceAccount"
    assert sa["metadata"]["name"] == f"agent-{AGENT_NAME}"
    assert sa["metadata"]["namespace"] == AGENT_NAME
    expected_gsa = f"agent-{AGENT_NAME}@{config.gcp_project}.iam.gserviceaccount.com"
    assert sa["metadata"]["annotations"]["iam.gke.io/gcp-service-account"] == expected_gsa

    pvc = yaml.safe_load((base / "pvc.yaml").read_text())
    assert pvc == {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": f"agent-{AGENT_NAME}-techne", "namespace": AGENT_NAME},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "1Gi"}},
        },
    }


def test_wire_step_edits_gojira_yaml_contract_with_exact_fs_group(session_factory, config):
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


def test_wire_step_gojira_yaml_uses_model_specific_vertex_location(session_factory, tmp_path):
    """Gemini-3.1-Pro-Preview arc: the VERTEXAI_LOCATION env `_update_gojira_
    yaml` writes is what actually reaches the deployed pod, so it must honor
    a model's own `location` override the same way RepoStep's BundleSpec
    does — a regional-only env var here would still 404 against a
    global-only preview even though the composed bundle carries the right
    value."""
    gemini_model_id = "vertex_ai/gemini-3.1-pro-preview"
    config = Config(
        workdir=tmp_path / "work",
        vertex_location="us-east5",
        models=[
            ModelChoice(id=MODEL_ID, label="Claude Sonnet 4.5"),
            ModelChoice(id=gemini_model_id, label="Gemini 3.1 Pro Preview", location="global"),
        ],
    )
    repo = _repo_full_name(config)
    session, agent = _make_agent(
        session_factory, model_id=gemini_model_id, repo_full_name=repo, gojira_app_id=1
    )
    github = FakeGitHub()
    gojira = FakeGojira(activate_after=1)
    gojira.create_app(
        name=AGENT_NAME, repo_full_name=repo, cluster=config.gojira_cluster, namespace=AGENT_NAME
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github, gojira=gojira))

    WireStep().run(ctx)

    gojira_yaml = yaml.safe_load((ctx.workdir / "wire" / "gojira.yaml").read_text())
    assert gojira_yaml["env"] == {
        "VERTEXAI_PROJECT": config.vertex_project,
        "VERTEXAI_LOCATION": "global",
    }


def test_wire_step_gojira_yaml_keeps_org_default_for_model_without_location(
    session_factory, config
):
    """Companion default case: a model with no `location` override (the
    fixture's default MODEL_ID, and every Claude partner model) still wires
    `config.vertex_location` into gojira.yaml, unchanged from before this
    field existed."""
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=1)
    github = FakeGitHub()
    gojira = FakeGojira(activate_after=1)
    gojira.create_app(
        name=AGENT_NAME, repo_full_name=repo, cluster=config.gojira_cluster, namespace=AGENT_NAME
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github, gojira=gojira))

    WireStep().run(ctx)

    gojira_yaml = yaml.safe_load((ctx.workdir / "wire" / "gojira.yaml").read_text())
    assert gojira_yaml["env"]["VERTEXAI_LOCATION"] == config.vertex_location


def test_wire_step_reprovisions_and_waits_active_when_push_changed_something(session_factory, config):
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=1)
    github = FakeGitHub()  # default commit_push_results=[True]
    gojira = FakeGojira(activate_after=2)
    gojira.create_app(name=AGENT_NAME, repo_full_name=repo, cluster=config.gojira_cluster, namespace=AGENT_NAME)
    ctx = _make_ctx(session, agent, config, _clients(github=github, gojira=gojira))

    detail = WireStep().run(ctx)

    assert gojira.reprovision_calls == [1]
    assert gojira.calls.count("app_status:1") == 2
    assert "active" in detail


def test_wire_step_second_run_with_no_changes_skips_reprovision(session_factory, config):
    """Self-review's central question: a second WireStep run whose commit_push
    reports nothing changed must NOT call reprovision again — but it must
    still poll gojira's status to confirm the app is active before returning,
    since a prior run may have crashed after reprovision() but before its own
    _wait_active completed."""
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=1)
    github = FakeGitHub(commit_push_results=[True, False])
    gojira = FakeGojira(activate_after=1)
    gojira.create_app(name=AGENT_NAME, repo_full_name=repo, cluster=config.gojira_cluster, namespace=AGENT_NAME)
    ctx = _make_ctx(session, agent, config, _clients(github=github, gojira=gojira))

    WireStep().run(ctx)  # first run: pushes real changes
    assert gojira.reprovision_calls == [1]
    status_polls_after_first_run = gojira.calls.count("app_status:1")

    detail = WireStep().run(ctx)  # second run: nothing changed

    assert gojira.reprovision_calls == [1]  # unchanged — no second reprovision
    assert gojira.calls.count("app_status:1") > status_polls_after_first_run
    assert "no" in detail.lower()


def test_wire_step_no_change_run_raises_when_app_actually_failed(session_factory, config):
    """Crash-window regression for Fix 1: if a prior run's reprovision() never
    reached active (e.g. the process crashed before its own _wait_active
    completed) and gojira actually landed on failed, a resumed WireStep run
    that finds nothing left to push must still surface that failure — not
    silently mark the step done."""
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=1)
    github = FakeGitHub(commit_push_results=[False])
    gojira = FakeGojira(
        activate_after=1,
        end_status="failed",
        failed_steps=[{"name": "apply", "status": "failed", "error": "boom"}],
    )
    gojira.create_app(name=AGENT_NAME, repo_full_name=repo, cluster=config.gojira_cluster, namespace=AGENT_NAME)
    ctx = _make_ctx(session, agent, config, _clients(github=github, gojira=gojira))

    with pytest.raises(RuntimeError, match="apply"):
        WireStep().run(ctx)


def test_wire_step_raises_when_gojira_never_reaches_active(session_factory, config):
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=1)
    github = FakeGitHub()
    gojira = FakeGojira(activate_after=1, end_status="failed", failed_steps=[
        {"name": "apply", "status": "failed", "error": "boom"},
    ])
    gojira.create_app(name=AGENT_NAME, repo_full_name=repo, cluster=config.gojira_cluster, namespace=AGENT_NAME)
    ctx = _make_ctx(session, agent, config, _clients(github=github, gojira=gojira))

    with pytest.raises(RuntimeError, match="apply"):
        WireStep().run(ctx)


def test_wire_step_rmtrees_stale_clone_dir_before_recloning(session_factory, config):
    """Retry safety, mirroring RepoStep's compose_dir rmtree: a stale prior
    clone must not make this run fail or silently mix content."""
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=1)
    github = FakeGitHub()
    gojira = FakeGojira(activate_after=1)
    gojira.create_app(name=AGENT_NAME, repo_full_name=repo, cluster=config.gojira_cluster, namespace=AGENT_NAME)
    ctx = _make_ctx(session, agent, config, _clients(github=github, gojira=gojira))

    stale_dir = ctx.workdir / "wire" / "k8s" / "base"
    stale_dir.mkdir(parents=True)
    (stale_dir / "leftover.yaml").write_text("stale: true\n")

    WireStep().run(ctx)

    assert not (stale_dir / "leftover.yaml").exists()
    assert (stale_dir / "serviceaccount.yaml").is_file()


# --- WireStep colleague pass (colleagues switchboard plan, Task 3/B3) -----------
#
# Same two-gate discipline HelloStep/DirectoryStep already use for the
# switchboard: `cabal_enabled(ctx.config)` gates the gojira.yaml `secrets_from`
# append (folded into `_update_gojira_yaml`'s own edit — no second reprovision);
# `cabal_enabled(ctx.config) AND ctx.clients.cabal is not None` gates the actual
# `deliver_pass` call (needs a real client to mint against). Disabled (the
# shared `config` fixture, which carries neither org key) must leave every part
# of this addition a no-op — byte-identical to before this feature existed.

WIRE_CABAL_ENDPOINT_TEMPLATE = "http://agent-{name}.default.svc.cluster.local"


def _wire_cabal_enabled_config(config):
    return config.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": WIRE_CABAL_ENDPOINT_TEMPLATE,
        }
    )


def test_wire_step_disabled_switchboard_leaves_gojira_yaml_secrets_from_absent(
    session_factory, config
):
    """Pin: the shared `config` fixture has no switchboard org keys —
    `secrets_from` must not even appear in gojira.yaml, exactly as before
    this feature existed."""
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=1)
    github = FakeGitHub()
    gojira = FakeGojira(activate_after=1)
    gojira.create_app(name=AGENT_NAME, repo_full_name=repo, cluster=config.gojira_cluster, namespace=AGENT_NAME)
    ctx = _make_ctx(session, agent, config, _clients(github=github, gojira=gojira))

    WireStep().run(ctx)

    gojira_yaml = yaml.safe_load((ctx.workdir / "wire" / "gojira.yaml").read_text())
    assert "secrets_from" not in gojira_yaml


def test_wire_step_disabled_switchboard_never_touches_kubectl_or_cabal(
    session_factory, config
):
    """A cabal client and kubectl fake are both present on `Clients`, but the
    switchboard isn't configured — WireStep must not touch either."""
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=1)
    github = FakeGitHub()
    gojira = FakeGojira(activate_after=1)
    gojira.create_app(name=AGENT_NAME, repo_full_name=repo, cluster=config.gojira_cluster, namespace=AGENT_NAME)
    kubectl = FakeKubectl()
    cabal = FakeCabal()
    ctx = _make_ctx(
        session, agent, config, _clients(github=github, gojira=gojira, kubectl=kubectl, cabal=cabal)
    )

    WireStep().run(ctx)

    assert kubectl.secrets_applied == []
    assert cabal.mint_colleague_calls == []


def test_wire_step_enabled_appends_agent_cabal_to_secrets_from(session_factory, config, monkeypatch):
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


def test_wire_step_enabled_preserves_pre_existing_secrets_from_entries(session_factory, config, monkeypatch):
    """The append is add-only: an already-slack-wired repo's `secrets_from`
    entry must survive, with the colleague-pass entry appended after it —
    never a wholesale replace."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    repo = _repo_full_name(config)
    cfg = _wire_cabal_enabled_config(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=1)
    github = FakeGitHub(gojira_secrets_from=[f"{AGENT_NAME}-slack"])
    gojira = FakeGojira(activate_after=1)
    gojira.create_app(name=AGENT_NAME, repo_full_name=repo, cluster=cfg.gojira_cluster, namespace=AGENT_NAME)
    kubectl = FakeKubectl()
    cabal = FakeCabal()
    ctx = _make_ctx(
        session, agent, cfg, _clients(github=github, gojira=gojira, kubectl=kubectl, cabal=cabal)
    )

    WireStep().run(ctx)

    gojira_yaml = yaml.safe_load((ctx.workdir / "wire" / "gojira.yaml").read_text())
    assert gojira_yaml["secrets_from"] == [f"{AGENT_NAME}-slack", f"{AGENT_NAME}-cabal"]


def test_wire_step_enabled_delivers_the_colleague_pass_secret(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    repo = _repo_full_name(config)
    cfg = _wire_cabal_enabled_config(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=1)
    github = FakeGitHub()
    gojira = FakeGojira(activate_after=1)
    gojira.create_app(name=AGENT_NAME, repo_full_name=repo, cluster=cfg.gojira_cluster, namespace=AGENT_NAME)
    kubectl = FakeKubectl()
    cabal = FakeCabal(mint_colleague_result="minted-colleague-token")
    ctx = _make_ctx(
        session, agent, cfg, _clients(github=github, gojira=gojira, kubectl=kubectl, cabal=cabal)
    )

    WireStep().run(ctx)

    assert cabal.mint_colleague_calls == [AGENT_NAME]
    assert len(kubectl.secrets_applied) == 1
    namespace, name, literals = kubectl.secrets_applied[0]
    assert namespace == AGENT_NAME
    assert name == f"{AGENT_NAME}-cabal"
    assert literals == {"CABAL_COLLEAGUE_TOKEN": "minted-colleague-token"}


def test_wire_step_enabled_but_cabal_client_missing_skips_delivery_without_crashing(
    session_factory, config, monkeypatch
):
    """`cabal_enabled(config)` true but `ctx.clients.cabal` is None (wired
    without a cabal client despite the org config being set) must not
    crash — mirrors HelloStep's/DirectoryStep's own fallback discipline.
    The gojira.yaml secrets_from entry is still appended (its gate is
    `cabal_enabled(config)` alone), but nothing is actually delivered since
    there's no client to mint against."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    repo = _repo_full_name(config)
    cfg = _wire_cabal_enabled_config(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=1)
    github = FakeGitHub()
    gojira = FakeGojira(activate_after=1)
    gojira.create_app(name=AGENT_NAME, repo_full_name=repo, cluster=cfg.gojira_cluster, namespace=AGENT_NAME)
    kubectl = FakeKubectl()
    ctx = _make_ctx(
        session, agent, cfg, _clients(github=github, gojira=gojira, kubectl=kubectl, cabal=None)
    )

    WireStep().run(ctx)  # must not raise

    assert kubectl.secrets_applied == []


def test_wire_step_ignores_cabal_client_when_switchboard_not_enabled(session_factory, config):
    """A cabal client present on `Clients` but `cabal_enabled(config)` false
    (no org keys configured) must leave secrets_from untouched and never
    deliver anything — the client's mere presence is not the signal."""
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=1)
    github = FakeGitHub()
    gojira = FakeGojira(activate_after=1)
    gojira.create_app(name=AGENT_NAME, repo_full_name=repo, cluster=config.gojira_cluster, namespace=AGENT_NAME)
    kubectl = FakeKubectl()
    cabal = FakeCabal()
    ctx = _make_ctx(
        session, agent, config, _clients(github=github, gojira=gojira, kubectl=kubectl, cabal=cabal)
    )

    WireStep().run(ctx)

    gojira_yaml = yaml.safe_load((ctx.workdir / "wire" / "gojira.yaml").read_text())
    assert "secrets_from" not in gojira_yaml
    assert cabal.mint_colleague_calls == []
    assert kubectl.secrets_applied == []


def test_wire_step_mint_failure_propagates_before_the_push(session_factory, config, monkeypatch):
    """Proves the ordering the brief requires (deliver_pass BEFORE the push):
    if minting fails, the failure must propagate before `commit_push` is
    ever called — never a push that gets ahead of a colleague pass that
    doesn't exist yet."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    repo = _repo_full_name(config)
    cfg = _wire_cabal_enabled_config(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=1)
    github = FakeGitHub()
    gojira = FakeGojira(activate_after=1)
    gojira.create_app(name=AGENT_NAME, repo_full_name=repo, cluster=cfg.gojira_cluster, namespace=AGENT_NAME)
    kubectl = FakeKubectl()
    cabal = FakeCabal(mint_colleague_error="switchboard unreachable")
    ctx = _make_ctx(
        session, agent, cfg, _clients(github=github, gojira=gojira, kubectl=kubectl, cabal=cabal)
    )

    with pytest.raises(CabalError):
        WireStep().run(ctx)

    assert not any(call.startswith("commit_push:") for call in github.calls)


def test_wire_step_delivers_the_colleague_pass_through_the_secrets_platform(
    session_factory, config, monkeypatch
):
    """With the secrets platform's machine lane wired (`clients.ladon`), the
    colleague pass goes to the platform under the agent's own path and comes
    back as the SAME `{name}-cabal` Secret -- so this step's gojira.yaml
    edit and the pod behind it are unchanged, and nothing is written into
    the namespace directly."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    repo = _repo_full_name(config)
    cfg = _wire_cabal_enabled_config(config)
    session, agent = _make_agent(
        session_factory, repo_full_name=repo, gojira_app_id=1,
        created_by="someone@example.com",
    )
    github = FakeGitHub()
    gojira = FakeGojira(activate_after=1)
    gojira.create_app(name=AGENT_NAME, repo_full_name=repo, cluster=cfg.gojira_cluster, namespace=AGENT_NAME)
    kubectl = FakeKubectl()
    cabal = FakeCabal(mint_colleague_result="minted-token-value")
    ladon = FakeLadon()
    ctx = _make_ctx(
        session, agent, cfg,
        _clients(github=github, gojira=gojira, kubectl=kubectl, cabal=cabal, ladon=ladon),
    )

    WireStep().run(ctx)

    assert ladon.puts == [
        (
            "someone@example.com",
            f"apps/{AGENT_NAME}/cabal",
            {"CABAL_COLLEAGUE_TOKEN": "minted-token-value"},
        )
    ]
    assert ladon.syncs == [
        (
            "someone@example.com",
            AGENT_NAME,
            f"apps/{AGENT_NAME}/cabal",
            AGENT_NAME,
            f"{AGENT_NAME}-cabal",
        )
    ]
    assert kubectl.secrets_applied == []
    # The contract in the repo is untouched: the same secrets_from entry
    # the direct write produced.
    gojira_yaml = yaml.safe_load((ctx.workdir / "wire" / "gojira.yaml").read_text())
    assert f"{AGENT_NAME}-cabal" in gojira_yaml["secrets_from"]


def test_wire_step_secrets_platform_failure_propagates_before_the_push(
    session_factory, config, monkeypatch
):
    """A lane failure fails this step exactly the way a failed direct write
    does -- before the push, and with no fallback to writing the Secret
    itself (two writers for one Secret name is two sources of truth)."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    repo = _repo_full_name(config)
    cfg = _wire_cabal_enabled_config(config)
    session, agent = _make_agent(
        session_factory, repo_full_name=repo, gojira_app_id=1,
        created_by="someone@example.com",
    )
    github = FakeGitHub()
    gojira = FakeGojira(activate_after=1)
    gojira.create_app(name=AGENT_NAME, repo_full_name=repo, cluster=cfg.gojira_cluster, namespace=AGENT_NAME)
    kubectl = FakeKubectl()
    ctx = _make_ctx(
        session, agent, cfg,
        _clients(
            github=github, gojira=gojira, kubectl=kubectl, cabal=FakeCabal(),
            ladon=FakeLadon(put_error="the secrets platform is unavailable right now"),
        ),
    )

    with pytest.raises(ClientError, match="unavailable"):
        WireStep().run(ctx)

    assert not any(call.startswith("commit_push:") for call in github.calls)
    assert kubectl.secrets_applied == []


def test_wire_step_secret_delivery_failure_propagates_before_the_push(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    repo = _repo_full_name(config)
    cfg = _wire_cabal_enabled_config(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=1)
    github = FakeGitHub()
    gojira = FakeGojira(activate_after=1)
    gojira.create_app(name=AGENT_NAME, repo_full_name=repo, cluster=cfg.gojira_cluster, namespace=AGENT_NAME)
    kubectl = FakeKubectl(apply_secret_error="kubectl apply failed")
    cabal = FakeCabal()
    ctx = _make_ctx(
        session, agent, cfg, _clients(github=github, gojira=gojira, kubectl=kubectl, cabal=cabal)
    )

    with pytest.raises(ClientError):
        WireStep().run(ctx)

    assert not any(call.startswith("commit_push:") for call in github.calls)


def test_register_resources_is_idempotent_when_entries_already_present(tmp_path):
    """Direct unit test of the kustomization-registration helper: re-running
    against a file that already has one (or both) of the new entries must not
    produce duplicates."""
    from kobo_api.steps import _register_resources

    kustomization_path = tmp_path / "kustomization.yaml"
    kustomization_path.write_text(
        yaml.safe_dump(
            {"resources": ["deployment.yaml", "service.yaml", "serviceaccount.yaml"]},
            sort_keys=False,
        )
    )

    _register_resources(kustomization_path, ["serviceaccount.yaml", "pvc.yaml"])
    _register_resources(kustomization_path, ["serviceaccount.yaml", "pvc.yaml"])  # run twice

    data = yaml.safe_load(kustomization_path.read_text())
    assert data["resources"] == ["deployment.yaml", "service.yaml", "serviceaccount.yaml", "pvc.yaml"]
    assert data["resources"].count("serviceaccount.yaml") == 1
    assert data["resources"].count("pvc.yaml") == 1


def test_register_resources_preserves_apiversion_and_kind(tmp_path):
    """_register_resources parses and re-dumps the *whole* kustomization file,
    not just its resources list — apiVersion/kind must round-trip untouched
    alongside the newly-appended entries."""
    from kobo_api.steps import _register_resources

    kustomization_path = tmp_path / "kustomization.yaml"
    kustomization_path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "kustomize.config.k8s.io/v1beta1",
                "kind": "Kustomization",
                "resources": ["deployment.yaml"],
            },
            sort_keys=False,
        )
    )

    _register_resources(kustomization_path, ["serviceaccount.yaml"])

    data = yaml.safe_load(kustomization_path.read_text())
    assert data["apiVersion"] == "kustomize.config.k8s.io/v1beta1"
    assert data["kind"] == "Kustomization"
    assert data["resources"] == ["deployment.yaml", "serviceaccount.yaml"]


# --- BuildStep --------------------------------------------------------------------


def test_build_step_happy_path_returns_success_detail_with_run_id(session_factory, config):
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    github = FakeGitHub(
        head_sha="deadbeef",
        runs_by_sha={
            "deadbeef": [
                [{"id": 42, "status": "in_progress", "conclusion": None}],
                [{"id": 42, "status": "completed", "conclusion": "success"}],
            ]
        },
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    detail = BuildStep().run(ctx)

    assert "42" in detail
    assert "deadbeef" in detail
    assert "rerun" not in detail.lower()
    assert github.calls.count(f"runs_for_commit:{repo}:deadbeef") == 2
    # Adapted for Fix 2: head_sha is now re-resolved on every poll (to
    # follow an advancing head), not resolved once at entry — so it's
    # called once per iteration (2, matching the 2 runs_for_commit calls
    # above), not once total. This fixed-head fake returns the same value
    # every time, so the extra reads don't change the outcome here.
    assert github.calls.count(f"head_sha:{repo}") == 2


def test_build_step_re_resolves_head_each_poll_to_follow_advancing_commit(session_factory, config):
    """BuildStep re-resolves the head sha on every poll (not just once at
    entry) so it can follow a commit that advances mid-step. With a constant
    head, this re-resolution means BuildStep polls exactly that sha
    repeatedly — the same stale-run immunity as before (a completed,
    successful run under some *other* commit is never queried), just via a
    mechanism that also handles an advancing head correctly."""
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    github = FakeGitHub(
        head_sha="new-sha",
        runs_by_sha={
            "stale-sha": [[{"id": 1, "status": "completed", "conclusion": "success"}]],
            "new-sha": [
                [],  # registration lag: nothing for the new commit yet
                [{"id": 7, "status": "completed", "conclusion": "success"}],
            ],
        },
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    detail = BuildStep().run(ctx)

    assert "7" in detail
    # The stale-sha run is never even queried, let alone reported.
    assert github.calls.count(f"runs_for_commit:{repo}:stale-sha") == 0
    assert github.calls.count(f"runs_for_commit:{repo}:new-sha") == 2


def test_build_step_empty_runs_keeps_polling_registration_lag(session_factory, config):
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    github = FakeGitHub(
        head_sha="deadbeef",
        runs_by_sha={
            "deadbeef": [
                [],
                [],
                [{"id": 5, "status": "completed", "conclusion": "success"}],
            ]
        },
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    detail = BuildStep().run(ctx)

    assert "5" in detail
    assert github.calls.count(f"runs_for_commit:{repo}:deadbeef") == 3


def test_build_step_failure_triggers_one_automatic_rerun_and_self_heals(session_factory, config):
    """The field-observed WIF-propagation defect: a completed failure on the
    first CI run against this commit is retried automatically, once, and a
    subsequent success is reported as a self-heal rather than raising —
    Retry no longer needs an operator to `gh run rerun` by hand."""
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    github = FakeGitHub(
        head_sha="deadbeef",
        runs_by_sha={
            "deadbeef": [[{"id": 42, "status": "completed", "conclusion": "failure"}]]
        },
        rerun_result={"status": "completed", "conclusion": "success"},
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    detail = BuildStep().run(ctx)

    assert "42" in detail
    # Adapted for Fix 1: the success detail now names which conclusion the
    # rerun healed ("healed a failure run") instead of the old generic
    # "(after automatic rerun)" phrasing — the phrasing change is the point
    # of the fix (see the cancelled-run test below for the case that
    # motivated it), so this no longer contains the substring "rerun".
    assert "healed a failure run" in detail.lower()
    assert github.rerun_calls == [(repo, 42)]


def test_build_step_heals_a_cancelled_run_not_just_a_failed_one(session_factory, config):
    """Fix 1: BuildStep's heal-on-completed-non-success behavior isn't
    special-cased to "failure" — a live incident saw GitHub's concurrency
    pending-queue rule CANCEL the newer of two pending runs in a group
    outright (rather than queueing it), and that must self-heal exactly the
    same way a completed failure does. The success detail names the
    conclusion it healed."""
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    github = FakeGitHub(
        head_sha="deadbeef",
        runs_by_sha={
            "deadbeef": [[{"id": 42, "status": "completed", "conclusion": "cancelled"}]]
        },
        rerun_result={"status": "completed", "conclusion": "success"},
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    detail = BuildStep().run(ctx)

    assert "42" in detail
    assert "healed a cancelled run" in detail.lower()
    assert github.rerun_calls == [(repo, 42)]


def test_build_step_ignores_a_stale_post_rerun_read_within_the_grace_window(
    session_factory, config
):
    """P1c review fold-in: gh's own read-after-write lag can echo the
    PRE-rerun completed/non-success payload back on the very next poll after
    `rerun()` fires. Pre-fix, BuildStep's `rerun_attempted` flag was already
    set by then, so that stale echo would be misread as a genuine second
    failure and raise immediately — before the rerun ever had a chance to
    actually run. `rerun_followup` models the real sequence: one more poll
    of the stale cancelled payload, then queued (gh has registered the
    rerun), then the healed success."""
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    github = FakeGitHub(
        head_sha="deadbeef",
        runs_by_sha={
            "deadbeef": [[{"id": 42, "status": "completed", "conclusion": "cancelled"}]]
        },
        rerun_followup=[
            {"status": "completed", "conclusion": "cancelled"},  # stale echo
            {"status": "queued", "conclusion": None},
            {"status": "completed", "conclusion": "success"},
        ],
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    detail = BuildStep().run(ctx)

    assert "42" in detail
    assert "healed a cancelled run" in detail.lower()
    # Only the one rerun this whole invocation, despite the stale payload
    # reappearing right after it — that repeat is grace, not a second
    # failure needing (or getting) another rerun.
    assert github.rerun_calls == [(repo, 42)]


def test_build_step_follows_an_advancing_head_and_notes_it(session_factory, config):
    """Fix 2: head_sha is re-resolved on every poll rather than once at
    entry, so a head that advances mid-step (the platform's auto-merged
    setup PR landing) is followed rather than left pinned to a stale read.
    `head_sha_sequence` models two polls still seeing the pre-merge commit
    before the third sees the merge commit as the new head."""
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    github = FakeGitHub(
        head_sha_sequence=["sha1", "sha1", "sha2"],
        runs_by_sha={
            "sha1": [[], []],
            "sha2": [[{"id": 9, "status": "completed", "conclusion": "success"}]],
        },
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    detail = BuildStep().run(ctx)

    assert "9" in detail
    assert "sha2" in detail
    assert "head advanced" in detail.lower()
    assert github.calls.count(f"runs_for_commit:{repo}:sha1") == 2
    assert github.calls.count(f"runs_for_commit:{repo}:sha2") == 1


def test_build_step_does_not_settle_for_a_stale_sha_success_once_head_has_advanced(
    session_factory, config
):
    """Regression test for the live incident described at module level: the
    platform's auto-merged setup PR landed while BuildStep was mid-poll, and
    a single stale head_sha read matched the WIRE commit's already-completed
    success while the *real* head had moved on to the merge commit — whose
    own CI run had been CANCELLED by GitHub's concurrency pending-queue
    rule. Shipped a stale tree that DeployStep's verification then correctly
    rejected two steps later.

    Sequenced so the pre-fix code (a single head_sha resolve at entry) would
    poll only sha1-wire and see its success on that commit's *second* poll —
    i.e. it would have passed on the stale success. The fixed code must
    instead notice, on its own second poll, that head has already advanced
    to sha2-merge, switch to watching it from then on (never re-querying
    sha1-wire again, so it never even sees that stale success), and heal
    sha2-merge's cancelled run.
    """
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    github = FakeGitHub(
        head_sha_sequence=["sha1-wire", "sha2-merge", "sha2-merge"],
        runs_by_sha={
            "sha1-wire": [
                [{"id": 1, "status": "in_progress", "conclusion": None}],
                [{"id": 1, "status": "completed", "conclusion": "success"}],
            ],
            "sha2-merge": [
                [{"id": 2, "status": "completed", "conclusion": "cancelled"}],
            ],
        },
        rerun_result={"status": "completed", "conclusion": "success"},
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    detail = BuildStep().run(ctx)

    assert "2" in detail
    assert "sha2-merge" in detail
    assert "healed a cancelled run" in detail.lower()
    assert "head advanced" in detail.lower()
    # sha1-wire is only ever polled once (the in-progress poll) — its own
    # eventual success (poll 2, never reached) is never queried once the
    # head has moved on, structurally the same "never mistaken for this
    # commit" guarantee BuildStep already had per-invocation, now extended
    # across an advancing head.
    assert github.calls.count(f"runs_for_commit:{repo}:sha1-wire") == 1
    assert github.rerun_calls == [(repo, 2)]


def test_build_step_raises_on_oscillating_head_exceeding_max_advances(session_factory, config):
    """Fix 4: an oscillating head (flapping between multiple shas) that exceeds
    BUILD_MAX_HEAD_ADVANCES is a sign of unexpected concurrent pushes and
    must raise a bounded error rather than looping indefinitely healing each
    new sha in turn. The pipeline's pushes-at-most-twice invariant means a
    sequence like sha1→sha2→sha1→sha2 is never expected and indicates a
    structural problem — the error names the observed shas."""
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    # Construct a sequence that flaps between two shas more than
    # BUILD_MAX_HEAD_ADVANCES times: sha1→sha2→sha1→sha2→sha1→sha2
    # (with runs returning empty for each to force more polls).
    flap_sequence = []
    for _ in range(3):  # 3 flips = 6 head_sha calls covering both shas
        flap_sequence.extend(["sha1", "sha2"])
    github = FakeGitHub(
        head_sha_sequence=flap_sequence,
        runs_by_sha={
            "sha1": [[], [], []],
            "sha2": [[], [], []],
        },
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    with pytest.raises(RuntimeError) as exc_info:
        BuildStep().run(ctx)

    message = str(exc_info.value)
    assert "more than" in message
    assert str(BUILD_MAX_HEAD_ADVANCES) in message
    assert "sha1" in message and "sha2" in message


def test_build_step_second_failure_after_rerun_raises_with_run_id(session_factory, config):
    """A second completed failure against the same commit, after the one
    automatic rerun, is a real failure — surfaced by raising rather than
    rerunning indefinitely.

    Adapted for the post-rerun stale-read grace fold-in: this fake's
    held-forever completed/failure payload now also has to burn through
    `BUILD_RERUN_STALE_GRACE_POLLS` grace polls (each still returning that
    same payload) before it's trusted as genuine and raises — it still
    does, just a few polls later, well within the reset budget. The
    assertions (raises naming "42", exactly one rerun call) are unchanged.
    """
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    github = FakeGitHub(
        head_sha="deadbeef",
        runs_by_sha={
            "deadbeef": [[{"id": 42, "status": "completed", "conclusion": "failure"}]]
        },
        rerun_result={"status": "completed", "conclusion": "failure"},
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    with pytest.raises(RuntimeError, match="42"):
        BuildStep().run(ctx)

    # Exactly one rerun attempted this invocation — not one per poll.
    assert github.rerun_calls == [(repo, 42)]


def test_build_step_times_out_after_max_polls(session_factory, config):
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    github = FakeGitHub(
        head_sha="deadbeef",
        runs_by_sha={
            "deadbeef": [[{"id": 1, "status": "in_progress", "conclusion": None}]]
        },
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    with pytest.raises(RuntimeError, match="timed out"):
        BuildStep().run(ctx)

    expected_polls = BUILD_POLL_TIMEOUT_S // BUILD_POLL_INTERVAL_S
    assert github.calls.count(f"runs_for_commit:{repo}:deadbeef") == expected_polls


def test_build_step_waits_longer_than_the_workflows_own_rollout_wait():
    """The generated workflow waits up to 600s for the rollout after a build
    of a minute or two. A room that gives up at the same 600s always times
    out FIRST on a rollout that never finishes, and reports a run still
    "in_progress" instead of the step that is actually failing. The wait
    here has to outlast the workflow's, so the run concludes while the room
    is still watching and the card can name what failed.

    The 600 is the deploy platform's, not this repo's: its workflow
    template's `kubectl rollout status ... --timeout=600s` (gojira,
    `generator/templates/workflows/_deploy.j2`). If that number moves, this
    one has to move with it, and this test is where that is written down.
    """
    platform_rollout_wait_s = 600
    assert BUILD_POLL_TIMEOUT_S > platform_rollout_wait_s


DEPLOY_JOB_RUNNING = [
    {
        "name": "build-and-deploy-dev",
        "status": "in_progress",
        "conclusion": None,
        "steps": [
            {"name": "Build and push image", "status": "completed", "conclusion": "success"},
            {"name": "Deploy to dev", "status": "in_progress", "conclusion": None},
        ],
    }
]

DEPLOY_JOB_FAILED = [
    {
        "name": "build-and-deploy-dev",
        "status": "completed",
        "conclusion": "failure",
        "steps": [
            {"name": "Build and push image", "status": "completed", "conclusion": "success"},
            {"name": "Deploy to dev", "status": "completed", "conclusion": "failure"},
            {"name": "Post Run actions/checkout@v4", "status": "completed", "conclusion": "success"},
        ],
    }
]


def test_build_step_timeout_names_the_job_and_step_still_running(session_factory, config):
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    github = FakeGitHub(
        head_sha="deadbeef",
        runs_by_sha={"deadbeef": [[{"id": 1, "status": "in_progress", "conclusion": None}]]},
        jobs_by_run={1: DEPLOY_JOB_RUNNING},
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    with pytest.raises(RuntimeError) as excinfo:
        BuildStep().run(ctx)

    message = str(excinfo.value)
    assert "timed out" in message
    assert "run 1" in message
    assert "build-and-deploy-dev" in message
    assert "Deploy to dev" in message
    assert "still running" in message
    # Not the raw dict dump the card used to show.
    assert "{'id'" not in message
    assert github.calls.count(f"run_jobs:{repo}:1") == 1


def test_build_step_timeout_still_says_so_when_the_jobs_cannot_be_read(session_factory, config):
    """A gh failure carries a command line and whatever gh printed (URLs,
    auth hints). That goes to the log; the card only learns that the jobs
    could not be read, and still learns that the wait ended."""
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    github = FakeGitHub(
        head_sha="deadbeef",
        runs_by_sha={"deadbeef": [[{"id": 1, "status": "in_progress", "conclusion": None}]]},
        run_jobs_error=ClientError("gh run view 1 failed (rc=1): https://api.github.invalid/x"),
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    with pytest.raises(RuntimeError) as excinfo:
        BuildStep().run(ctx)

    message = str(excinfo.value)
    assert "timed out" in message
    assert "run 1" in message
    assert "in_progress" in message
    assert "could not be read" in message
    assert "api.github.invalid" not in message
    assert "rc=1" not in message


def test_build_step_timeout_with_no_run_yet_says_none_registered(session_factory, config):
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    github = FakeGitHub(head_sha="deadbeef", runs_by_sha={"deadbeef": [[]]})
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    with pytest.raises(RuntimeError, match="no CI run has registered"):
        BuildStep().run(ctx)


def test_build_step_second_failure_names_the_step_that_failed(session_factory, config):
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    github = FakeGitHub(
        head_sha="deadbeef",
        runs_by_sha={"deadbeef": [[{"id": 42, "status": "completed", "conclusion": "failure"}]]},
        rerun_result={"status": "completed", "conclusion": "failure"},
        jobs_by_run={42: DEPLOY_JOB_FAILED},
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    with pytest.raises(RuntimeError) as excinfo:
        BuildStep().run(ctx)

    message = str(excinfo.value)
    assert "run 42" in message
    assert "'failure'" in message
    assert "build-and-deploy-dev" in message
    assert "Deploy to dev" in message
    assert "failed at step" in message
    assert "automatic rerun" in message


def test_build_step_timeout_names_the_running_step_over_an_earlier_tolerated_failure(
    session_factory, config
):
    """A job may absorb an earlier step's failure (continue-on-error) and keep
    going. While the run is still in progress, the step still running is
    where it stands; the tolerated failure is history."""
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    job = [
        {
            "name": "build-and-deploy-dev",
            "status": "in_progress",
            "conclusion": None,
            "steps": [
                {"name": "Lint (advisory)", "status": "completed", "conclusion": "failure"},
                {"name": "Deploy to dev", "status": "in_progress", "conclusion": None},
            ],
        }
    ]
    github = FakeGitHub(
        head_sha="deadbeef",
        runs_by_sha={"deadbeef": [[{"id": 1, "status": "in_progress", "conclusion": None}]]},
        jobs_by_run={1: job},
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    with pytest.raises(RuntimeError) as excinfo:
        BuildStep().run(ctx)

    message = str(excinfo.value)
    assert "is still running step 'Deploy to dev'" in message
    assert "Lint (advisory)" not in message


def test_build_step_second_failure_names_a_cancelled_step_too(session_factory, config):
    """The heal fires on ANY non-success conclusion, so the second-failure
    message is reached for a cancelled or timed-out run as well. Its steps
    end 'cancelled', never 'failure', and the card still has to name one."""
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    cancelled_job = [
        {
            "name": "build-and-deploy-dev",
            "status": "completed",
            "conclusion": "cancelled",
            "steps": [
                {"name": "Build and push image", "status": "completed", "conclusion": "success"},
                {"name": "Deploy to dev", "status": "completed", "conclusion": "cancelled"},
                {"name": "Complete job", "status": "completed", "conclusion": "skipped"},
            ],
        }
    ]
    github = FakeGitHub(
        head_sha="deadbeef",
        runs_by_sha={"deadbeef": [[{"id": 7, "status": "completed", "conclusion": "cancelled"}]]},
        rerun_result={"status": "completed", "conclusion": "cancelled"},
        jobs_by_run={7: cancelled_job},
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    with pytest.raises(RuntimeError) as excinfo:
        BuildStep().run(ctx)

    message = str(excinfo.value)
    assert "run 7" in message
    assert "build-and-deploy-dev" in message
    assert "Deploy to dev" in message
    assert "ended 'cancelled' at step" in message


def test_build_step_second_failure_names_a_job_that_failed_before_any_step(
    session_factory, config
):
    """A runner that fails before the steps start: the job concludes
    'failure' and every step is skipped. There is no failing step to name,
    and the message says that instead of falling back to the bare verdict."""
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    job_without_steps = [
        {
            "name": "build-and-deploy-dev",
            "status": "completed",
            "conclusion": "failure",
            "steps": [
                {"name": "Set up job", "status": "completed", "conclusion": "skipped"},
            ],
        }
    ]
    github = FakeGitHub(
        head_sha="deadbeef",
        runs_by_sha={"deadbeef": [[{"id": 8, "status": "completed", "conclusion": "failure"}]]},
        rerun_result={"status": "completed", "conclusion": "failure"},
        jobs_by_run={8: job_without_steps},
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    with pytest.raises(RuntimeError) as excinfo:
        BuildStep().run(ctx)

    message = str(excinfo.value)
    assert "build-and-deploy-dev" in message
    assert "concluded 'failure' with no failing step" in message


def test_build_step_rerun_resets_poll_budget_so_a_late_failure_does_not_starve_the_heal(
    session_factory, config
):
    """The self-heal rerun must get its own poll budget: a first build that
    fails late — with only a handful of polls left in the *original* budget
    — must not starve the healed rerun of the polls it actually needs.
    Constructed so the healed run needs strictly more polls than would be
    left without the reset; this only passes if BuildStep resets the budget
    once when the rerun fires (bounded: it can only happen once per
    invocation, so worst case is 2x BUILD_POLL_TIMEOUT_S, never unbounded).
    """
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)

    max_polls = BUILD_POLL_TIMEOUT_S // BUILD_POLL_INTERVAL_S
    fail_at_poll = max_polls - 3  # only 3 polls left in the original budget
    extra_polls_needed = 10  # the healed run needs MORE than those 3 polls
    assert extra_polls_needed > (max_polls - fail_at_poll)  # the point of this test

    first_build_polls = [
        [{"id": 1, "status": "in_progress", "conclusion": None}]
        for _ in range(fail_at_poll - 1)
    ] + [[{"id": 1, "status": "completed", "conclusion": "failure"}]]

    github = FakeGitHub(head_sha="deadbeef", runs_by_sha={"deadbeef": first_build_polls})

    # FakeGitHub.rerun() (tests/fakes.py) always swaps straight to a
    # single-entry "final result" sequence — it can't natively express a
    # healed run that takes several more polls to land. Wrap it so the
    # healed run needs `extra_polls_needed` polls of its own before
    # succeeding, exactly the shape that would time out without the reset.
    real_rerun = github.rerun

    def _rerun_with_slow_healed_build(repo_name, run_id):
        real_rerun(repo_name, run_id)
        healed_polls = [
            [{"id": run_id, "status": "in_progress", "conclusion": None}]
            for _ in range(extra_polls_needed - 1)
        ] + [[{"id": run_id, "status": "completed", "conclusion": "success"}]]
        github._runs_by_sha["deadbeef"] = healed_polls
        github._run_index_by_sha["deadbeef"] = 0

    github.rerun = _rerun_with_slow_healed_build

    ctx = _make_ctx(session, agent, config, _clients(github=github))

    detail = BuildStep().run(ctx)

    assert "1" in detail
    # Adapted for Fix 1 (see the note on the failure-heals test above): the
    # success detail names the healed conclusion instead of saying "rerun".
    assert "healed a failure run" in detail.lower()
    assert github.rerun_calls == [(repo, 1)]


# --- DeployStep -------------------------------------------------------------------


def test_deploy_step_happy_path_calls_rollout_ok_with_namespace_and_deploy(session_factory, config):
    session, agent = _make_agent(session_factory)
    kubectl = FakeKubectl(rollout_result=True)
    ctx = _make_ctx(session, agent, config, _clients(kubectl=kubectl))

    detail = DeployStep().run(ctx)

    assert kubectl.calls == [(AGENT_NAME, AGENT_NAME)]
    assert AGENT_NAME in detail


def test_deploy_step_raises_on_rollout_failure(session_factory, config):
    session, agent = _make_agent(session_factory)
    kubectl = FakeKubectl(rollout_result=False)
    ctx = _make_ctx(session, agent, config, _clients(kubectl=kubectl))

    with pytest.raises(RuntimeError):
        DeployStep().run(ctx)


def test_deploy_step_notes_live_spec_verified_in_detail(session_factory, config):
    session, agent = _make_agent(session_factory)
    kubectl = FakeKubectl(rollout_result=True)
    ctx = _make_ctx(session, agent, config, _clients(kubectl=kubectl))

    detail = DeployStep().run(ctx)

    assert "live spec verified" in detail


def test_deploy_step_raises_naming_only_the_missing_var_on_partial_env_miss(
    session_factory, config
):
    """One of the two wired vars is present, the other absent — the error
    must name exactly the missing one, not both."""
    session, agent = _make_agent(session_factory)
    kubectl = FakeKubectl(
        rollout_result=True,
        env_names={(AGENT_NAME, AGENT_NAME): ["VERTEXAI_PROJECT"]},
    )
    ctx = _make_ctx(session, agent, config, _clients(kubectl=kubectl))

    with pytest.raises(RuntimeError) as exc_info:
        DeployStep().run(ctx)

    message = str(exc_info.value)
    # The `missing` list's repr names only VERTEXAI_LOCATION.
    assert "env ['VERTEXAI_LOCATION']" in message
    # VERTEXAI_PROJECT still appears as part of the live-env-names diagnostic
    # (it IS present live) — just never inside the "missing" list itself.
    assert "VERTEXAI_PROJECT" in message


def test_deploy_step_raises_with_remediation_hint_when_live_env_missing(session_factory, config):
    session, agent = _make_agent(session_factory)
    # Live Deployment is missing the wired model env entirely (e.g. a
    # raced regeneration-PR apply landed the pre-wire tree last).
    kubectl = FakeKubectl(
        rollout_result=True,
        env_names={(AGENT_NAME, AGENT_NAME): []},
    )
    ctx = _make_ctx(session, agent, config, _clients(kubectl=kubectl))

    with pytest.raises(RuntimeError) as exc_info:
        DeployStep().run(ctx)

    message = str(exc_info.value)
    assert "VERTEXAI_PROJECT" in message
    assert "VERTEXAI_LOCATION" in message
    assert "concurrent CI apply" in message
    assert "retry after the latest run completes" in message


def test_deploy_step_raises_naming_got_and_expected_on_wrong_service_account(
    session_factory, config
):
    session, agent = _make_agent(session_factory)
    kubectl = FakeKubectl(
        rollout_result=True,
        service_accounts={(AGENT_NAME, AGENT_NAME): "default"},
    )
    ctx = _make_ctx(session, agent, config, _clients(kubectl=kubectl))

    with pytest.raises(RuntimeError) as exc_info:
        DeployStep().run(ctx)

    message = str(exc_info.value)
    assert "default" in message
    assert f"agent-{AGENT_NAME}" in message
    assert "concurrent CI apply" in message


def test_a_rerun_of_the_wiring_keeps_an_owners_own_environment_variables(
    session_factory, config
):
    """The Secrets and environment panel writes into exactly the mapping
    this step edits (design 2026-09-22). Assigning over it, which is what
    this step used to do for a composed agent, meant any later reprovision
    -- a retry, a runtime re-pin, a resumed pipeline -- silently wiped an
    owner's own variables. The two model keys still win on their own
    names, which is why an owner is refused those at the panel."""
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=1)
    github = FakeGitHub(
        extra_clone_files={
            "gojira.yaml": (
                "app: clio\n"
                "env:\n"
                "  API_BASE: https://example.test\n"
                "  VERTEXAI_LOCATION: somewhere-stale\n"
            )
        }
    )
    gojira = FakeGojira(activate_after=1)
    gojira.create_app(
        name=AGENT_NAME,
        repo_full_name=repo,
        cluster=config.gojira_cluster,
        namespace=AGENT_NAME,
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github, gojira=gojira))

    WireStep().run(ctx)

    written = yaml.safe_load((ctx.workdir / "wire" / "gojira.yaml").read_text())
    assert written["env"]["API_BASE"] == "https://example.test"
    # The room's own keys are the room's, whatever the file said.
    assert written["env"]["VERTEXAI_PROJECT"] == config.vertex_project
    assert written["env"]["VERTEXAI_LOCATION"] == config.vertex_location

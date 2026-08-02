"""Tests for cortex_api.steps: WireStep, BuildStep, DeployStep.

WireStep is the highest-stakes step in the pipeline: it encodes the P0 live
E2E finding that gojira's generator *preserves* kustomization.yaml entries
rather than discovering files on disk, so an app-authored manifest that isn't
also registered there is silently never applied. Every happy-path test here
therefore asserts on the *parsed* kustomization.yaml resources list, not just
that the manifest files exist — that's the assertion that would have caught
the P0 defect (a missing ServiceAccount) before it ever reached a cluster.

Same no-real-sleep discipline as test_steps.py: `_sleep` is monkeypatched to a
no-op so BuildStep's 600s/10s poll (60 iterations at worst) and WireStep's
reused onboarding poll never actually block.
"""

from __future__ import annotations

import pytest
import yaml

from cortex_api.clients import Clients
from cortex_api.models import Agent
from cortex_api.provisioner import Ctx
from cortex_api.steps import (
    BUILD_POLL_INTERVAL_S,
    BUILD_POLL_TIMEOUT_S,
    TECHNE_FS_GROUP,
    BuildStep,
    DeployStep,
    WireStep,
)
from tests.fakes import (
    GENERATED_KUSTOMIZATION_RESOURCES,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
)

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
MODEL_ID = "vertex_ai/claude-sonnet-4-5"


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """WireStep (via _wait_active) and BuildStep both run poll loops; never
    let either sleep for real."""
    import cortex_api.steps as steps_module

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
):
    session = session_factory()
    agent = Agent(
        name=name,
        persona=persona,
        model_id=model_id,
        skills=[],
        repo_full_name=repo_full_name,
        gojira_app_id=gojira_app_id,
    )
    session.add(agent)
    session.commit()
    return session, agent


def _make_ctx(session, agent, config, clients) -> Ctx:
    workdir = config.workdir / agent.name
    workdir.mkdir(parents=True, exist_ok=True)
    return Ctx(agent=agent, session=session, config=config, clients=clients, workdir=workdir)


def _clients(*, github=None, gojira=None, kubectl=None) -> Clients:
    return Clients(github=github, gojira=gojira, cloud=None, agent=None, kubectl=kubectl)


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
    reports nothing changed must NOT call reprovision again."""
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=1)
    github = FakeGitHub(commit_push_results=[True, False])
    gojira = FakeGojira(activate_after=1)
    gojira.create_app(name=AGENT_NAME, repo_full_name=repo, cluster=config.gojira_cluster, namespace=AGENT_NAME)
    ctx = _make_ctx(session, agent, config, _clients(github=github, gojira=gojira))

    WireStep().run(ctx)  # first run: pushes real changes
    assert gojira.reprovision_calls == [1]

    detail = WireStep().run(ctx)  # second run: nothing changed

    assert gojira.reprovision_calls == [1]  # unchanged — no second reprovision
    assert "no" in detail.lower()


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


def test_register_resources_is_idempotent_when_entries_already_present(tmp_path):
    """Direct unit test of the kustomization-registration helper: re-running
    against a file that already has one (or both) of the new entries must not
    produce duplicates."""
    from cortex_api.steps import _register_resources

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


# --- BuildStep --------------------------------------------------------------------


def test_build_step_happy_path_returns_success_detail_with_run_id(session_factory, config):
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    github = FakeGitHub(
        run_sequence=[
            {"id": 42, "status": "in_progress", "conclusion": None},
            {"id": 42, "status": "completed", "conclusion": "success"},
        ]
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    detail = BuildStep().run(ctx)

    assert "42" in detail
    assert github.calls.count(f"latest_run:{repo}") == 2


def test_build_step_failure_conclusion_raises_with_run_id_in_message(session_factory, config):
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    github = FakeGitHub(
        run_sequence=[{"id": 99, "status": "completed", "conclusion": "failure"}]
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    with pytest.raises(RuntimeError, match="99"):
        BuildStep().run(ctx)


def test_build_step_times_out_after_max_polls(session_factory, config):
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    github = FakeGitHub(
        run_sequence=[{"id": 1, "status": "in_progress", "conclusion": None}]
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    with pytest.raises(RuntimeError, match="timed out"):
        BuildStep().run(ctx)

    expected_polls = BUILD_POLL_TIMEOUT_S // BUILD_POLL_INTERVAL_S
    assert github.calls.count(f"latest_run:{repo}") == expected_polls


def test_build_step_sleeps_at_the_configured_interval(session_factory, config, monkeypatch):
    import cortex_api.steps as steps_module

    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    github = FakeGitHub(
        run_sequence=[
            {"id": 1, "status": "in_progress", "conclusion": None},
            {"id": 1, "status": "in_progress", "conclusion": None},
            {"id": 1, "status": "completed", "conclusion": "success"},
        ]
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    sleep_calls = []
    monkeypatch.setattr(steps_module, "_sleep", lambda s: sleep_calls.append(s))

    BuildStep().run(ctx)

    assert sleep_calls == [BUILD_POLL_INTERVAL_S, BUILD_POLL_INTERVAL_S]


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

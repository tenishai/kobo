"""Tests for the connected pipeline (connected-agents design 2026-09-21):
the shorter step list a team's own repo runs through, and the four shared
steps that read the agent's kind before they act.

The one rule every test here exists to hold: the room never pushes to, and
never creates, a connected repo. `FakeGitHub.calls` is a flat audit trail of
every method it was asked for, so "the room did not do that" is an assertion
about that list, not an inference from what did happen.

Same no-real-sleep discipline as test_steps_wire.py / test_api_agents.py:
`kobo_api.steps._sleep` is monkeypatched to a no-op so the gojira and CI poll
loops never actually block. The end-to-end test at the bottom sleeps for real
between HTTP-free status polls, waiting on the pipeline's own daemon thread.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest
import yaml

from kobo_api.clients import ClientError, Clients
from kobo_api.kinds import (
    KIND_COMPOSED,
    KIND_CONNECTED,
    RUN_MODE_HEADLESS,
    RUN_MODE_HTTP,
)
from kobo_api.models import Agent, StepStatus
from kobo_api.provisioner import (
    CONNECTED_PIPELINE,
    PIPELINE,
    STEP_LABELS,
    Ctx,
    pipeline_for,
    run_pipeline,
)
from kobo_api.steps import (
    BUILD_MAX_HEAD_ADVANCES,
    AccessStep,
    AvatarStep,
    ConnectedWireStep,
    ConnectStep,
    OnboardStep,
)
from tests.fakes import (
    GENERATED_KUSTOMIZATION_RESOURCES,
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeImage,
    FakeKubectl,
)

AGENT_NAME = "clio"
REPO_NAME = "team-chatbot"
MODEL_ID = "vertex_ai/claude-sonnet-4-5"
WIRE_BRANCH = "kobo/wire"
WIRE_TITLE = "kobo: service account and model access"
WIRE_PR_URL = "https://github.com/example-org/team-chatbot/pull/4"

CONNECTED_STEP_NAMES = [
    "connect", "avatar", "onboard", "access", "wire", "build", "deploy",
]

POLL_DEADLINE_S = 5
POLL_INTERVAL_S = 0.02


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    import kobo_api.steps as steps_module

    monkeypatch.setattr(steps_module, "_sleep", lambda seconds: None)


def _repo(config) -> str:
    return f"{config.github_org}/{REPO_NAME}"


def _make_agent(
    session_factory,
    config,
    *,
    kind: str = KIND_CONNECTED,
    run_mode: str | None = RUN_MODE_HTTP,
    secret_names: list[str] | None = None,
    repo_full_name: str | None = None,
    gojira_app_id: int | None = None,
    purpose_tags: str | None = None,
):
    session = session_factory()
    agent = Agent(
        name=AGENT_NAME,
        persona="connected",
        model_id=MODEL_ID,
        skills=[],
        kind=kind,
        run_mode=run_mode,
        secret_names=secret_names or [],
        repo_full_name=repo_full_name if repo_full_name is not None else _repo(config),
        gojira_app_id=gojira_app_id,
        purpose_tags=purpose_tags,
    )
    session.add(agent)
    session.commit()
    return session, agent


def _make_ctx(session, agent, config, clients) -> Ctx:
    workdir = config.workdir / agent.name
    workdir.mkdir(parents=True, exist_ok=True)
    return Ctx(agent=agent, session=session, config=config, clients=clients, workdir=workdir)


def _clients(*, github=None, gojira=None, cloud=None, kubectl=None, image=None, agent=None):
    return Clients(
        github=github,
        gojira=gojira,
        cloud=cloud,
        agent=agent,
        kubectl=kubectl,
        image=image,
    )


def _seeded_gojira(config, repo, *, activate_after: int = 1) -> FakeGojira:
    """A FakeGojira that already holds app id 1 for `repo`, so a step which
    only waits (never onboards) has something to poll."""
    gojira = FakeGojira(activate_after=activate_after)
    gojira.create_app(
        name=AGENT_NAME,
        repo_full_name=repo,
        cluster=config.gojira_cluster,
        namespace=AGENT_NAME,
    )
    return gojira


@dataclass
class _Wiring:
    """Everything a ConnectedWireStep test needs, named rather than
    unpacked positionally -- each test reads two or three of these five."""

    session: object
    agent: Agent
    github: FakeGitHub
    gojira: FakeGojira
    ctx: Ctx


def _repo_mutations(github: FakeGitHub) -> list[str]:
    """Every recorded call that would have written to the team's repo or
    changed its identity. The connected pipeline's whole promise is that
    this list stays empty."""
    forbidden = ("create_repo_from_dir:", "commit_push:", "rename_repo:", "archive_repo:")
    return [call for call in github.calls if call.startswith(forbidden)]


# --- the step list and its labels ------------------------------------------------


def test_pipeline_for_picks_the_list_by_kind(session_factory, config):
    session, connected = _make_agent(session_factory, config)
    composed = Agent(name="atlas", persona="helpful-generalist", model_id=MODEL_ID)

    assert pipeline_for(connected) is CONNECTED_PIPELINE
    assert pipeline_for(composed) is PIPELINE
    session.close()


def test_the_connected_list_connects_wires_by_pull_request_and_stops_at_deploy():
    """No directory listing and no first contact: a connected agent has no
    switchboard identity to register and nothing the room can say hello to."""
    assert [step.name for step in CONNECTED_PIPELINE] == CONNECTED_STEP_NAMES


def test_step_labels_cover_every_name_in_both_lists():
    """`STEP_LABELS` is what the checklist renders from, and the detail route
    falls back to the raw step name when a name is missing from it."""
    for step in (*PIPELINE, *CONNECTED_PIPELINE):
        assert STEP_LABELS[step.name] == step.label


def test_the_two_wire_steps_share_a_name_and_a_label():
    """Same job done politely, so the checklist vocabulary stays small."""
    wire = {step.name: step for step in PIPELINE}["wire"]
    connected_wire = {step.name: step for step in CONNECTED_PIPELINE}["wire"]

    assert connected_wire.name == wire.name == "wire"
    assert connected_wire.label == wire.label == "Deploy wiring"


def test_the_connect_step_is_labelled_for_the_checklist():
    assert ConnectStep().name == "connect"
    assert ConnectStep().label == "Repository connected"


# --- ConnectStep -----------------------------------------------------------------


def test_connect_step_records_the_repo_and_creates_nothing(session_factory, config):
    repo = _repo(config)
    session, agent = _make_agent(session_factory, config)
    github = FakeGitHub(existing_repos={repo})
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    note = ConnectStep().run(ctx)

    assert repo in note
    assert _repo_mutations(github) == []
    assert github.created_from_dir == []


def test_connect_step_refuses_a_repo_outside_the_rooms_organisation(session_factory, config):
    session, agent = _make_agent(
        session_factory, config, repo_full_name="somewhere-else/team-chatbot"
    )
    github = FakeGitHub(existing_repos={"somewhere-else/team-chatbot"})
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    with pytest.raises(RuntimeError) as exc_info:
        ConnectStep().run(ctx)

    message = str(exc_info.value)
    assert config.github_org in message
    assert "—" not in message
    assert github.calls == []


def test_connect_step_refuses_an_agent_with_no_repository_recorded(session_factory, config):
    """Its own sentence rather than the out-of-organisation one: an empty
    value is not a repository somewhere else, it is a row that never got
    one, and telling somebody their blank is in the wrong organisation
    helps nobody."""
    session, agent = _make_agent(session_factory, config, repo_full_name="")
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    with pytest.raises(RuntimeError) as exc_info:
        ConnectStep().run(ctx)

    message = str(exc_info.value)
    assert message == "no repository is recorded for this agent. Connect it again"
    assert github.calls == []


@pytest.mark.parametrize(
    "repo",
    [
        "example-org/team/chatbot",
        "example-org",
        "example-org/",
        # Characters that address something other than the repository once
        # this value reaches a `gh api` path or a URL.
        "example-org/foo?x=1",
        "example-org/foo#",
        "example-org/..",
        "example-org/ x",
    ],
)
def test_connect_step_refuses_anything_that_is_not_one_owner_and_one_name(
    session_factory, config, repo
):
    """`owner/a/b` passes an owner check and then travels into every path
    the room builds from it. One slash, both halves a real name, or
    nothing -- the same rule the scan applies before the row exists."""
    session, agent = _make_agent(session_factory, config, repo_full_name=repo)
    github = FakeGitHub(existing_repos={repo})
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    with pytest.raises(RuntimeError) as exc_info:
        ConnectStep().run(ctx)

    message = str(exc_info.value)
    assert "does not look like a repository name" in message
    assert "—" not in message
    assert github.calls == []


def test_connect_step_reads_the_organisation_the_way_github_does(
    session_factory, config
):
    """GitHub resolves an owner case-insensitively. Failing a provision
    over the capitalisation of an organisation the scan already accepted
    would be the room disagreeing with itself."""
    repo = f"{config.github_org.title()}/{REPO_NAME}"
    session, agent = _make_agent(session_factory, config, repo_full_name=repo)
    github = FakeGitHub(existing_repos={repo})
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    assert ConnectStep().run(ctx) == f"connected {repo}"


def test_connect_step_refuses_a_repo_it_cannot_see(session_factory, config):
    session, agent = _make_agent(session_factory, config)
    github = FakeGitHub(existing_repos=set())
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    with pytest.raises(RuntimeError) as exc_info:
        ConnectStep().run(ctx)

    message = str(exc_info.value)
    assert _repo(config) in message
    assert "—" not in message
    assert _repo_mutations(github) == []


# --- AvatarStep ------------------------------------------------------------------


def test_avatar_step_for_a_connected_agent_keeps_the_picture_in_the_room(
    session_factory, config
):
    """The picture is the room's own record of the agent, not a file in the
    team's repo: generated and stored, never committed anywhere."""
    session, agent = _make_agent(session_factory, config, purpose_tags='["billing"]')
    github = FakeGitHub(existing_repos={_repo(config)})
    ctx = _make_ctx(session, agent, config, _clients(github=github, image=FakeImage()))

    AvatarStep().run(ctx)

    assert agent.avatar_png is not None
    assert github.calls == []


def test_avatar_step_for_a_connected_agent_asks_for_a_picture_of_its_name_and_purpose(
    session_factory, config
):
    """A connected agent has no persona row to describe, so the prompt is
    built from what the room does know about it."""
    session, agent = _make_agent(session_factory, config, purpose_tags='["billing"]')
    image = FakeImage()
    ctx = _make_ctx(session, agent, config, _clients(github=FakeGitHub(), image=image))

    AvatarStep().run(ctx)

    prompt = image.prompts[-1]
    assert AGENT_NAME in prompt
    assert "billing" in prompt


def test_avatar_step_for_a_composed_agent_still_commits_the_picture(
    session_factory, config, fake_storage
):
    """The composed path is untouched: the repo is the room's own, and the
    bundle is where the picture lives."""
    session = session_factory()
    agent = Agent(
        name="atlas",
        persona="helpful-generalist",
        model_id=MODEL_ID,
        skills=[],
        repo_full_name=f"{config.github_org}/agent-atlas",
    )
    session.add(agent)
    session.commit()
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github, image=FakeImage()))

    AvatarStep().run(ctx)

    assert any(call.startswith("commit_push:") for call in github.calls)


# --- OnboardStep -----------------------------------------------------------------


def _onboard_workload_class(session, agent, config) -> str:
    gojira = FakeGojira(activate_after=1)
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira, github=FakeGitHub()))
    OnboardStep().run(ctx)
    return gojira.create_app_calls[-1]["workload_class"]


def _onboard_create_call(session, agent, config) -> dict:
    gojira = FakeGojira(activate_after=1)
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira, github=FakeGitHub()))
    OnboardStep().run(ctx)
    return gojira.create_app_calls[-1]


def test_onboard_step_never_asks_the_platform_to_merge_a_connected_repos_setup_pr(
    session_factory, config
):
    """A connected repository is a team's own; its owner merges the wiring
    (Ember's review, 2026-09-29). The room's waiver of that review belongs
    only to repositories the room authored. The platform refuses the flag
    for hand-built repositories anyway; the room does not ask for it."""
    session, agent = _make_agent(session_factory, config, kind=KIND_CONNECTED)

    assert _onboard_create_call(session, agent, config)["auto_merge_setup_pr"] is False


def test_onboard_step_still_merges_a_composed_repos_setup_pr_on_its_own(
    session_factory, config
):
    session, agent = _make_agent(session_factory, config, kind=KIND_COMPOSED, run_mode=None)

    assert _onboard_create_call(session, agent, config)["auto_merge_setup_pr"] is True


def test_onboard_step_asks_for_the_ordinary_class_when_the_agent_serves_http(
    session_factory, config
):
    session, agent = _make_agent(session_factory, config, run_mode=RUN_MODE_HTTP)

    assert _onboard_workload_class(session, agent, config) == "agent"


def test_onboard_step_asks_for_the_headless_class_when_the_agent_has_no_port(
    session_factory, config
):
    headless_config = config.model_copy(update={"connected_headless_class": "agent-headless"})
    session, agent = _make_agent(session_factory, headless_config, run_mode=RUN_MODE_HEADLESS)

    assert _onboard_workload_class(session, agent, headless_config) == "agent-headless"


def test_onboard_step_refuses_a_headless_agent_when_the_room_has_no_class_for_one(
    session_factory, config
):
    """Absent configuration means the feature is off, and the step says so in
    the same words the route does rather than handing the platform a blank."""
    session, agent = _make_agent(session_factory, config, run_mode=RUN_MODE_HEADLESS)
    gojira = FakeGojira(activate_after=1)
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira, github=FakeGitHub()))

    with pytest.raises(RuntimeError) as exc_info:
        OnboardStep().run(ctx)

    assert str(exc_info.value) == "agents with no HTTP port are not available in this room yet"
    assert gojira.calls == []


def test_onboard_step_for_a_composed_agent_still_asks_for_the_agent_class(
    session_factory, config
):
    session = session_factory()
    agent = Agent(
        name="atlas",
        persona="helpful-generalist",
        model_id=MODEL_ID,
        skills=[],
        repo_full_name=f"{config.github_org}/agent-atlas",
    )
    session.add(agent)
    session.commit()

    assert _onboard_workload_class(session, agent, config) == "agent"


# --- AccessStep ------------------------------------------------------------------


def test_access_step_for_a_connected_agent_grants_identity_and_model_access(
    session_factory, config
):
    session, agent = _make_agent(session_factory, config)
    cloud = FakeCloud()
    ctx = _make_ctx(session, agent, config, _clients(cloud=cloud))

    AccessStep().run(ctx)

    email = f"agent-{AGENT_NAME}@{config.gcp_project}.iam.gserviceaccount.com"
    assert f"ensure_gsa:agent-{AGENT_NAME}" in cloud.calls
    assert f"bind_workload_identity:{email}:{AGENT_NAME}/agent-{AGENT_NAME}" in cloud.calls
    assert f"add_to_group:{email}:{config.model_group}" in cloud.calls


def test_access_step_gives_a_connected_agent_no_room_memory_prefix(session_factory, config):
    """The room does not replicate a connected agent's memory: it does not
    know that the team's own code keeps any."""
    memory_config = config.model_copy(update={"memory_replica_bucket": "agent-memory"})
    session, agent = _make_agent(session_factory, memory_config)
    cloud = FakeCloud()
    ctx = _make_ctx(session, agent, memory_config, _clients(cloud=cloud))

    AccessStep().run(ctx)

    assert not any(call.startswith("grant_bucket_prefix_write:") for call in cloud.calls)


def test_access_step_for_a_composed_agent_still_grants_its_memory_prefix(
    session_factory, config
):
    memory_config = config.model_copy(update={"memory_replica_bucket": "agent-memory"})
    session, agent = _make_agent(session_factory, memory_config, kind=KIND_COMPOSED, run_mode=None)
    cloud = FakeCloud()
    ctx = _make_ctx(session, agent, memory_config, _clients(cloud=cloud))

    AccessStep().run(ctx)

    assert any(call.startswith("grant_bucket_prefix_write:") for call in cloud.calls)


# --- ConnectedWireStep -----------------------------------------------------------


def _wire_ctx(session_factory, config, *, github=None, gojira=None, **agent_kwargs):
    repo = _repo(config)
    session, agent = _make_agent(session_factory, config, gojira_app_id=1, **agent_kwargs)
    github = github if github is not None else FakeGitHub(pr_url=WIRE_PR_URL)
    gojira = gojira if gojira is not None else _seeded_gojira(config, repo)
    return _Wiring(
        session=session,
        agent=agent,
        github=github,
        gojira=gojira,
        ctx=_make_ctx(session, agent, config, _clients(github=github, gojira=gojira)),
    )


def test_connected_wire_step_registers_the_service_account_and_nothing_else(
    session_factory, config
):
    """The same registration gotcha WireStep encodes: a manifest the
    kustomization does not name is silently never applied. There is no
    memory volume here, so there is no pvc.yaml to register either."""
    wiring = _wire_ctx(session_factory, config)

    ConnectedWireStep().run(wiring.ctx)

    base = wiring.ctx.workdir / "wire" / "k8s" / "base"
    kustomization = yaml.safe_load((base / "kustomization.yaml").read_text())
    assert kustomization["resources"] == [
        *GENERATED_KUSTOMIZATION_RESOURCES,
        "serviceaccount.yaml",
    ]
    assert not (base / "pvc.yaml").exists()

    service_account = yaml.safe_load((base / "serviceaccount.yaml").read_text())
    assert service_account["metadata"]["name"] == f"agent-{AGENT_NAME}"
    assert service_account["metadata"]["annotations"]["iam.gke.io/gcp-service-account"] == (
        f"agent-{AGENT_NAME}@{config.gcp_project}.iam.gserviceaccount.com"
    )


def test_connected_wire_step_sets_the_service_account_and_model_env_only(
    session_factory, config
):
    wiring = _wire_ctx(session_factory, config)

    ConnectedWireStep().run(wiring.ctx)

    data = yaml.safe_load((wiring.ctx.workdir / "wire" / "gojira.yaml").read_text())
    assert data["service_account"] == f"agent-{AGENT_NAME}"
    assert data["env"] == {
        "VERTEXAI_PROJECT": config.vertex_project,
        "VERTEXAI_LOCATION": config.vertex_location,
    }
    assert "data_mount" not in data
    assert data.get("secrets_from", []) == []


def _gojira_yaml_with(**extra) -> str:
    """A team's own gojira.yaml, as a hand-written file would look."""
    data = {"app": REPO_NAME, "class": "agent", **extra}
    return yaml.safe_dump(data, sort_keys=False)


def test_connected_wire_step_keeps_a_teams_own_env_and_only_adds_the_model_keys(
    session_factory, config
):
    """The room owns a composed agent's gojira.yaml and writes `env`
    wholesale. It does not own this one: assigning over `env` here would
    silently delete the settings the team's own code runs on."""
    github = FakeGitHub(
        pr_url=WIRE_PR_URL,
        extra_clone_files={
            "gojira.yaml": _gojira_yaml_with(
                env={"LOG_LEVEL": "debug", "FEATURE_X": "on"}
            )
        },
    )
    wiring = _wire_ctx(session_factory, config, github=github)

    ConnectedWireStep().run(wiring.ctx)

    data = yaml.safe_load((wiring.ctx.workdir / "wire" / "gojira.yaml").read_text())
    assert data["env"] == {
        "LOG_LEVEL": "debug",
        "FEATURE_X": "on",
        "VERTEXAI_PROJECT": config.vertex_project,
        "VERTEXAI_LOCATION": config.vertex_location,
    }


def test_connected_wire_step_refuses_to_take_over_a_service_account_in_gojira_yaml(
    session_factory, config
):
    """A repository that already names its own service account is telling
    the room something. Overwriting it would move the agent onto an
    identity its owner did not choose, silently."""
    github = FakeGitHub(
        pr_url=WIRE_PR_URL,
        extra_clone_files={
            "gojira.yaml": _gojira_yaml_with(service_account="team-runner")
        },
    )
    wiring = _wire_ctx(session_factory, config, github=github)

    with pytest.raises(RuntimeError) as exc_info:
        ConnectedWireStep().run(wiring.ctx)

    assert str(exc_info.value) == (
        "this repository already sets its own service account. Remove it from "
        "gojira.yaml and k8s/base/serviceaccount.yaml, or ask an operator, "
        "then press Retry"
    )
    assert wiring.github.open_pr_calls == []
    assert _repo_mutations(wiring.github) == []


def test_connected_wire_step_refuses_to_overwrite_a_service_account_manifest(
    session_factory, config
):
    github = FakeGitHub(
        pr_url=WIRE_PR_URL,
        extra_clone_files={
            "k8s/base/serviceaccount.yaml": (
                "apiVersion: v1\nkind: ServiceAccount\n"
                "metadata: {name: team-runner, namespace: team}\n"
            )
        },
    )
    wiring = _wire_ctx(session_factory, config, github=github)

    with pytest.raises(RuntimeError) as exc_info:
        ConnectedWireStep().run(wiring.ctx)

    assert "already sets its own service account" in str(exc_info.value)
    assert wiring.github.open_pr_calls == []


def test_connected_wire_step_accepts_its_own_manifest_from_a_prior_attempt(
    session_factory, config
):
    """The takeover check must not fire on the room's OWN wiring: byte-equal
    content is a retry, not a team's choice."""
    from kobo_api.steps import SERVICEACCOUNT_YAML_TEMPLATE

    sa_email = f"agent-{AGENT_NAME}@{config.gcp_project}.iam.gserviceaccount.com"
    github = FakeGitHub(
        pr_url=WIRE_PR_URL,
        extra_clone_files={
            "k8s/base/serviceaccount.yaml": SERVICEACCOUNT_YAML_TEMPLATE.format(
                name=AGENT_NAME, gsa_email=sa_email
            ),
            "gojira.yaml": _gojira_yaml_with(service_account=f"agent-{AGENT_NAME}"),
        },
    )
    wiring = _wire_ctx(session_factory, config, github=github)

    note = ConnectedWireStep().run(wiring.ctx)

    assert "active" in note


def test_connected_wire_step_refuses_a_leftover_branch_with_no_pull_request(
    session_factory, config
):
    """A closed-unmerged pull request leaves its branch behind, and pushing
    that branch again fails non fast-forward with git's own stderr, which
    no Retry can clear. Say what is in the way instead."""
    repo = _repo(config)
    github = FakeGitHub(pr_url=WIRE_PR_URL, existing_branches={f"{repo}#{WIRE_BRANCH}"})
    wiring = _wire_ctx(session_factory, config, github=github)

    with pytest.raises(RuntimeError) as exc_info:
        ConnectedWireStep().run(wiring.ctx)

    assert str(exc_info.value) == (
        f"the branch {WIRE_BRANCH} is already in this repository. Delete it, "
        "or open a pull request from it, then press Retry"
    )
    assert wiring.github.open_pr_calls == []


def test_connected_wire_step_uses_an_open_pull_request_over_the_branch_check(
    session_factory, config
):
    """The branch existing is expected when its pull request is open: that
    is the retry path, and it merges rather than complaining."""
    repo = _repo(config)
    existing = "https://github.com/example-org/team-chatbot/pull/9"
    github = FakeGitHub(
        pr_url=WIRE_PR_URL,
        open_prs={f"{repo}#{WIRE_BRANCH}": existing},
        existing_branches={f"{repo}#{WIRE_BRANCH}"},
    )
    wiring = _wire_ctx(session_factory, config, github=github)

    ConnectedWireStep().run(wiring.ctx)

    assert wiring.github.merge_pr_calls == [(repo, existing)]


@pytest.mark.parametrize(
    "broken",
    ["clone_error", "find_pr", "branch_exists", "open_pr", "merge_pr", "reprovision"],
)
def test_connected_wire_step_never_puts_a_clients_own_error_text_on_the_step(
    session_factory, config, broken
):
    """Whatever this step raises lands verbatim on `AgentStep.error`, which
    any viewer of the agent can read. A `gh` or platform error carries a
    command line, a URL and sometimes a token; none of that belongs there."""
    secret = "ghs_notatoken_but_shaped_like_one"
    github = FakeGitHub(
        pr_url=WIRE_PR_URL,
        clone_error=secret if broken == "clone_error" else None,
    )
    wiring = _wire_ctx(session_factory, config, github=github)

    def _boom(*args, **kwargs):
        raise ClientError(secret)

    if broken in {"find_pr", "branch_exists", "open_pr", "merge_pr"}:
        setattr(wiring.github, broken, _boom)
    if broken == "reprovision":
        wiring.gojira.reprovision = _boom

    with pytest.raises(RuntimeError) as exc_info:
        ConnectedWireStep().run(wiring.ctx)

    message = str(exc_info.value)
    assert secret not in message
    assert "—" not in message
    assert message


def test_connected_wire_step_pull_request_body_names_the_files_it_touches(
    session_factory, config
):
    """An owner reviewing this has to be able to tell, without reading the
    diff, exactly what it changes and that their gojira.yaml comes back
    reformatted."""
    wiring = _wire_ctx(session_factory, config)

    ConnectedWireStep().run(wiring.ctx)

    body = wiring.github.open_pr_calls[0]["body"]
    assert "k8s/base/serviceaccount.yaml" in body
    assert "k8s/base/kustomization.yaml" in body
    assert "gojira.yaml" in body
    assert "comments" in body
    assert "—" not in body


def test_connected_wire_step_carries_the_owners_own_secret_names(session_factory, config):
    """The room never holds these; it only tells the platform which Secrets
    the owner will create in the agent's namespace."""
    wiring = _wire_ctx(session_factory, config, secret_names=["clio-api", "clio-db"])

    ConnectedWireStep().run(wiring.ctx)

    data = yaml.safe_load((wiring.ctx.workdir / "wire" / "gojira.yaml").read_text())
    assert data["secrets_from"] == ["clio-api", "clio-db"]


def test_connected_wire_step_proposes_the_change_and_never_pushes_it(session_factory, config):
    wiring = _wire_ctx(session_factory, config)

    ConnectedWireStep().run(wiring.ctx)

    assert wiring.github.open_pr_calls[0]["repo"] == _repo(config)
    assert wiring.github.open_pr_calls[0]["branch"] == WIRE_BRANCH
    assert wiring.github.open_pr_calls[0]["title"] == WIRE_TITLE
    assert wiring.github.merge_pr_calls == [(_repo(config), WIRE_PR_URL)]
    assert _repo_mutations(wiring.github) == []


def test_connected_wire_step_reprovisions_once_the_pull_request_is_merged(
    session_factory, config
):
    wiring = _wire_ctx(session_factory, config)

    note = ConnectedWireStep().run(wiring.ctx)

    assert wiring.gojira.reprovision_calls == [1]
    assert "active" in note


def test_connected_wire_step_asks_a_person_to_merge_when_github_refuses(
    session_factory, config
):
    """A protected branch is the owner's own opt-out, not a room failure:
    the step says exactly what to do and the pipeline resumes from here."""
    github = FakeGitHub(pr_url=WIRE_PR_URL, merge_pr_results=[False])
    wiring = _wire_ctx(session_factory, config, github=github)

    with pytest.raises(RuntimeError) as exc_info:
        ConnectedWireStep().run(wiring.ctx)

    assert str(exc_info.value) == f"Merge this pull request, then press Retry: {WIRE_PR_URL}"
    assert wiring.gojira.reprovision_calls == []
    assert _repo_mutations(github) == []


def test_connected_wire_step_retry_reuses_an_open_pull_request(session_factory, config):
    """Opening a second pull request from the same branch is how this step
    would fail on every retry; finding the open one is how it does not."""
    repo = _repo(config)
    existing = "https://github.com/example-org/team-chatbot/pull/9"
    github = FakeGitHub(pr_url=WIRE_PR_URL, open_prs={f"{repo}#{WIRE_BRANCH}": existing})
    wiring = _wire_ctx(session_factory, config, github=github)

    ConnectedWireStep().run(wiring.ctx)

    assert wiring.github.open_pr_calls == []
    assert wiring.github.merge_pr_calls == [(repo, existing)]


def test_connected_wire_step_sees_nothing_to_change_once_the_wiring_landed(
    session_factory, config
):
    """A person merged the pull request by hand between attempts: the default
    branch already carries the wiring, so there is nothing to propose and the
    step goes straight on to the platform."""
    github = FakeGitHub(pr_url=WIRE_PR_URL, track_pushed_content=True)
    wiring = _wire_ctx(session_factory, config, github=github)
    ConnectedWireStep().run(wiring.ctx)

    second_ctx = _make_ctx(
        wiring.session,
        wiring.agent,
        config,
        _clients(github=wiring.github, gojira=wiring.gojira),
    )
    wiring.github.open_pr_calls.clear()
    wiring.github.merge_pr_calls.clear()
    note = ConnectedWireStep().run(second_ctx)

    assert wiring.github.open_pr_calls == []
    assert wiring.github.merge_pr_calls == []
    assert wiring.gojira.reprovision_calls == [1, 1]
    assert "active" in note


def test_connected_wire_step_sees_nothing_to_change_in_the_generators_own_text(
    session_factory, config
):
    """The wiring is on the default branch as the deploy platform renders it
    after a regeneration: header comments, two-space list indentation, quoted
    values. Same meaning, different bytes. The step compares meaning: a
    re-dump here proposed a pull request of pure reformatting, the platform
    regenerated against the merge, and the two took turns forever
    (tenishai/kobo#123 item 11, agent-gordon 2026-10-08 and 2026-10-13)."""
    from kobo_api.config import vertex_location_for
    from kobo_api.steps import SERVICEACCOUNT_YAML_TEMPLATE

    name = AGENT_NAME
    gsa_email = f"agent-{name}@{config.gcp_project}.iam.gserviceaccount.com"
    tree = {
        "gojira.yaml": (
            "# Gojira app spec — this file is the contract between your repo and Gojira.\n"
            "# Change the class or add overrides here; Gojira regenerates the manifests\n"
            "# when this file changes.\n"
            f"app: {name}\n"
            "class: agent\n"
            "environments:\n"
            "  - name: dev\n"
            "    cluster: example-cluster\n"
            f"    namespace: {name}\n"
            f"service_account: agent-{name}\n"
            "env:\n"
            f'  VERTEXAI_LOCATION: "{vertex_location_for(config, MODEL_ID)}"\n'
            f'  VERTEXAI_PROJECT: "{config.vertex_project}"\n'
            "# overrides:                  # needs a justification — see docs\n"
        ),
        "k8s/base/kustomization.yaml": (
            "# Generated by Gojira.\n"
            "apiVersion: kustomize.config.k8s.io/v1beta1\n"
            "kind: Kustomization\n"
            "resources:\n"
            "  - deployment.yaml\n"
            "  - podmonitoring.yaml\n"
            "  - rules.yaml\n"
            "  - service.yaml\n"
            "  # app-authored resources, preserved by Gojira:\n"
            "  - serviceaccount.yaml\n"
        ),
        "k8s/base/serviceaccount.yaml": SERVICEACCOUNT_YAML_TEMPLATE.format(
            name=name, gsa_email=gsa_email
        ),
    }
    github = FakeGitHub(pr_url=WIRE_PR_URL, clone_tree=tree)
    wiring = _wire_ctx(session_factory, config, github=github)

    note = ConnectedWireStep().run(wiring.ctx)

    assert github.open_pr_calls == []
    assert github.merge_pr_calls == []
    assert wiring.gojira.reprovision_calls == [1]
    assert "active" in note
    clone = wiring.ctx.workdir / "wire"
    for rel_path, text in tree.items():
        assert (clone / rel_path).read_text() == text, rel_path


# --- BuildStep -------------------------------------------------------------------


def test_build_step_follows_a_connected_repos_moving_head(session_factory, config):
    """Other people push to a team's own repo, so a head that keeps moving is
    ordinary here. The watch restarts on each new head; the poll budget, not
    an advance count, is what bounds the step."""
    from kobo_api.steps import BuildStep

    advances = BUILD_MAX_HEAD_ADVANCES + 3
    shas = [f"sha{i}" for i in range(advances + 1)]
    session, agent = _make_agent(session_factory, config, gojira_app_id=1)
    github = FakeGitHub(
        head_sha_sequence=shas,
        runs_by_sha={
            shas[-1]: [[{"id": 5, "status": "completed", "conclusion": "success"}]],
        },
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    note = BuildStep().run(ctx)

    assert shas[-1] in note
    assert _repo_mutations(github) == []


def test_build_step_still_refuses_a_composed_repo_that_keeps_moving(session_factory, config):
    """The composed invariant is unchanged: the room owns those repos, so a
    head that advances more than the setup pull request explains is a real
    anomaly."""
    from kobo_api.steps import BuildStep

    advances = BUILD_MAX_HEAD_ADVANCES + 2
    shas = [f"sha{i}" for i in range(advances + 1)]
    session, agent = _make_agent(
        session_factory, config, kind=KIND_COMPOSED, run_mode=None, gojira_app_id=1
    )
    github = FakeGitHub(head_sha_sequence=shas)
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    with pytest.raises(RuntimeError) as exc_info:
        BuildStep().run(ctx)

    assert "head advanced" in str(exc_info.value)


# --- the engine's READY hooks ----------------------------------------------------


def _record_ready_hooks(monkeypatch) -> dict:
    import kobo_api.provisioner as provisioner_module

    seen = {"pairs": 0, "rosters": 0}
    monkeypatch.setattr(
        provisioner_module,
        "sync_dispatch_pairs_best_effort",
        lambda *a, **k: seen.__setitem__("pairs", seen["pairs"] + 1),
    )
    monkeypatch.setattr(
        provisioner_module,
        "refresh_colleague_rosters",
        lambda *a, **k: seen.__setitem__("rosters", seen["rosters"] + 1),
    )
    return seen


class _NoopStep:
    name = "connect"
    label = "Repository connected"

    def run(self, ctx: Ctx) -> str:
        return "ok"


def test_a_connected_agent_going_ready_touches_no_colleague_machinery(
    session_factory, config, monkeypatch
):
    """A connected agent has no switchboard identity and no colleagues, so
    neither hook has anything true to say about it."""
    from kobo_api.provisioner import _run_pipeline_body

    seen = _record_ready_hooks(monkeypatch)
    session, agent = _make_agent(session_factory, config)
    agent_id = agent.id
    session.close()

    _run_pipeline_body(
        session_factory,
        agent_id,
        config=config,
        clients=_clients(),
        steps=[_NoopStep()],
    )

    assert seen == {"pairs": 0, "rosters": 0}


def test_a_composed_agent_going_ready_still_runs_both_hooks(
    session_factory, config, monkeypatch
):
    from kobo_api.provisioner import _run_pipeline_body

    seen = _record_ready_hooks(monkeypatch)
    session, agent = _make_agent(
        session_factory, config, kind=KIND_COMPOSED, run_mode=None
    )
    agent_id = agent.id
    session.close()

    _run_pipeline_body(
        session_factory,
        agent_id,
        config=config,
        clients=_clients(),
        steps=[_NoopStep()],
    )

    assert seen == {"pairs": 1, "rosters": 1}


# --- end to end ------------------------------------------------------------------


def _poll_until(session_factory, name, statuses, deadline_s=POLL_DEADLINE_S):
    deadline = time.monotonic() + deadline_s
    status = None
    while time.monotonic() < deadline:
        session = session_factory()
        try:
            agent = session.query(Agent).filter_by(name=name).one()
            status = agent.status
            error = agent.error
            steps = [(step.name, step.status, step.error) for step in agent.steps]
        finally:
            session.close()
        if status in statuses:
            return steps, error
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"agent {name!r} did not reach {statuses} within {deadline_s}s")


def test_a_connected_agent_provisions_to_ready_without_the_room_touching_its_repo(
    session_factory, config
):
    repo = _repo(config)
    session, agent = _make_agent(session_factory, config)
    agent_id = agent.id
    session.close()

    github = FakeGitHub(existing_repos={repo})
    clients = _clients(
        github=github,
        gojira=FakeGojira(activate_after=1),
        cloud=FakeCloud(),
        kubectl=FakeKubectl(),
        agent=FakeAgent(reply="hello yourself, human"),
    )

    thread = run_pipeline(
        session_factory,
        agent_id,
        config=config,
        clients=clients,
        steps=pipeline_for(agent),
    )
    thread.join(timeout=POLL_DEADLINE_S)

    steps, error = _poll_until(session_factory, AGENT_NAME, {"ready", "failed"})
    assert error is None, f"steps: {steps}"
    assert [name for name, _, _ in steps] == CONNECTED_STEP_NAMES
    # The avatar is best effort and this wiring carries no image backend, so
    # it skips with its fixed reason; every other step must have run.
    by_name = {name: (status, step_error) for name, status, step_error in steps}
    assert by_name["avatar"] == (
        StepStatus.SKIPPED, "picture generation is not available",
    )
    assert all(
        status == StepStatus.DONE for name, status, _ in steps if name != "avatar"
    ), f"steps: {steps}"
    assert _repo_mutations(github) == []
    assert github.created_from_dir == []


# --- awaiting_pr: a person merges a connected repo's setup PR -------------------

PR_URL = "https://github.com/example-org/team-chatbot/pull/12"


def test_wait_active_parks_a_connected_agent_on_awaiting_pr_with_the_link(
    session_factory, config, monkeypatch
):
    """The platform opened the setup pull request; the team merges it. That is
    the design, not a failure, and not a poll budget's call (Ember's review,
    2026-09-29). The step parks with the link a person needs."""
    from kobo_api.steps import StepWaiting, _wait_active

    monkeypatch.setattr("kobo_api.steps._sleep", lambda s: None)
    session, agent = _make_agent(session_factory, config, kind=KIND_CONNECTED)
    gojira = FakeGojira(status_sequence=["awaiting_pr"], setup_pr_url=PR_URL)
    gojira.seed_app(1)
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira, github=FakeGitHub()))

    with pytest.raises(StepWaiting, match="merge the setup pull request") as excinfo:
        _wait_active(ctx, 1)
    assert PR_URL in str(excinfo.value)
    assert gojira.calls.count("app_status:1") == 1  # decided on the first read, no budget spent


def test_wait_active_keeps_polling_a_composed_agent_through_awaiting_pr(
    session_factory, config, monkeypatch
):
    """The room merges its own repositories' setup PR, so for a composed
    agent `awaiting_pr` is a moment on the way to `active`."""
    from kobo_api.steps import _wait_active

    monkeypatch.setattr("kobo_api.steps._sleep", lambda s: None)
    session, agent = _make_agent(session_factory, config, kind=KIND_COMPOSED, run_mode=None)
    gojira = FakeGojira(status_sequence=["awaiting_pr", "awaiting_pr", "active"], setup_pr_url=PR_URL)
    gojira.seed_app(1, status="pending")
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira, github=FakeGitHub()))

    assert _wait_active(ctx, 1) == "gojira app 1 active"
    assert gojira.calls.count("app_status:1") == 3


def test_onboard_step_parks_a_connected_agent_when_the_platform_opens_the_pr(
    session_factory, config, monkeypatch
):
    from kobo_api.steps import StepWaiting

    monkeypatch.setattr("kobo_api.steps._sleep", lambda s: None)
    session, agent = _make_agent(session_factory, config, kind=KIND_CONNECTED)
    gojira = FakeGojira(activate_after=1, end_status="awaiting_pr", setup_pr_url=PR_URL)
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira, github=FakeGitHub()))

    with pytest.raises(StepWaiting) as excinfo:
        OnboardStep().run(ctx)
    assert PR_URL in str(excinfo.value)
    assert agent.gojira_app_id == 1  # recorded, so the resume finds the same app

"""What the room can honestly say about a repository before it connects it.

The scan is the only thing standing between "a team pasted a repository
name" and a pipeline that will deploy whatever is in it, and it is read-only
by construction: four GitHub reads, one database question, and no clone. Its
two lists are not interchangeable. A blocker is something the room cannot
work around, so Connect stays disabled; a warning is something true about
the repository that its owner should know and may still choose, so it never
blocks. Every message is one plain sentence a person can act on, because
the same text is what the connect route hands back on a refusal.
"""

from __future__ import annotations

import pytest

from kobo_api.config import Config
from kobo_api.connect_scan import (
    ADDS,
    ALREADY_DEPLOYED,
    BASE_NOT_PINNED,
    BASE_OUTSIDE_REGISTRY,
    BRANCH_PROTECTED,
    ENV_FILE_COMMITTED,
    HEADLESS_UNAVAILABLE_CODE,
    LOOKS_COMPOSED,
    NAME_TAKEN,
    NO_DOCKERFILE,
    NO_PORT_EXPOSED,
    REPO_NOT_IN_ORG,
    REPO_NOT_VISIBLE,
    REPO_NOT_WRITABLE,
    scan_repo,
)
from kobo_api.kinds import RUN_MODE_HEADLESS, RUN_MODE_HTTP
from kobo_api.models import Agent, AgentStatus
from kobo_api.steps import HEADLESS_UNAVAILABLE
from tests.fakes import FAKE_CONNECTED_DOCKERFILE, FakeGitHub, FakeGojira

ORG = "example-org"
REPO = f"{ORG}/team-chatbot"
NAME = "clio"


@pytest.fixture
def config(tmp_path):
    return Config(workdir=tmp_path / "work", github_org=ORG)


def _scan(session_factory, config, github, *, repo=REPO, name=NAME, run_mode=RUN_MODE_HTTP):
    session = session_factory()
    try:
        return scan_repo(
            github,
            FakeGojira(),
            session,
            config,
            repo=repo,
            name=name,
            run_mode=run_mode,
        )
    finally:
        session.close()


def _codes(findings):
    return [finding.code for finding in findings]


def _take(session_factory, name):
    session = session_factory()
    try:
        session.add(
            Agent(name=name, persona="helper", team="shared", model_id="m", skills=[],
                  status=AgentStatus.READY)
        )
        session.commit()
    finally:
        session.close()


def test_a_repository_the_room_can_work_with_has_nothing_to_say(session_factory, config):
    github = FakeGitHub(existing_repos={REPO})

    report = _scan(session_factory, config, github)

    assert report.blockers == []
    assert report.warnings == []


def test_the_report_always_ends_with_what_will_be_added(session_factory, config):
    """Three lines, the same three every time: the report's promise is that
    nothing else is ever written to a team's repository."""
    report = _scan(session_factory, config, FakeGitHub(existing_repos={REPO}))

    assert report.adds == ADDS
    assert report.adds == [
        "gojira.yaml",
        "k8s/ (including k8s/base/serviceaccount.yaml)",
        ".github/workflows/ (one CI workflow)",
    ]


# --- Blockers -------------------------------------------------------------------


def test_a_repository_outside_the_org_is_refused_without_a_single_read(
    session_factory, config
):
    """The room holds one GitHub App installation and cannot reach outside
    it, so there is nothing to ask GitHub about."""
    github = FakeGitHub()

    report = _scan(session_factory, config, github, repo="elsewhere/team-chatbot")

    assert _codes(report.blockers) == [REPO_NOT_IN_ORG]
    assert github.calls == []


@pytest.mark.parametrize("repo", ["team-chatbot", f"{ORG}/a/b", f"{ORG}/", "/x"])
def test_a_name_that_is_not_owner_slash_repository_is_refused_the_same_way(
    session_factory, config, repo
):
    report = _scan(session_factory, config, FakeGitHub(), repo=repo)

    assert _codes(report.blockers) == [REPO_NOT_IN_ORG]


def test_a_repository_the_room_cannot_see_stops_the_scan_there(session_factory, config):
    """One read, one answer. Nothing else can be asked about a repository
    the App cannot see, and a 404 never distinguishes "no such repository"
    from "nobody granted the room access", so the message names both."""
    github = FakeGitHub()

    report = _scan(session_factory, config, github)

    assert _codes(report.blockers) == [REPO_NOT_VISIBLE]
    assert github.calls == [f"repo_access:{REPO}"]
    assert "could not be reached" in report.blockers[0].message


def test_a_repository_the_room_cannot_write_to_is_still_read(session_factory, config):
    """Read-only blocks the connect, but the rest of the report is worth
    having: an owner fixing access wants to know what else to fix."""
    github = FakeGitHub(
        existing_repos={REPO}, can_write=False, root_entries=["README.md"]
    )

    report = _scan(session_factory, config, github)

    assert _codes(report.blockers) == [REPO_NOT_WRITABLE, NO_DOCKERFILE]


def test_no_dockerfile_at_the_root_is_a_blocker(session_factory, config):
    github = FakeGitHub(existing_repos={REPO}, root_entries=["README.md", "src"])

    report = _scan(session_factory, config, github)

    assert _codes(report.blockers) == [NO_DOCKERFILE]


def test_a_gojira_yaml_naming_a_different_app_is_a_blocker(session_factory, config):
    """One repository is one agent. A repository already set up to deploy
    as something else is already somebody's, under another name."""
    github = FakeGitHub(
        existing_repos={REPO},
        root_entries=["Dockerfile", "gojira.yaml"],
        repo_files={
            "Dockerfile": FAKE_CONNECTED_DOCKERFILE,
            "gojira.yaml": "app: other-thing\n",
        },
    )

    report = _scan(session_factory, config, github)

    assert _codes(report.blockers) == [ALREADY_DEPLOYED]
    assert "other-thing" in report.blockers[0].message


def test_a_gojira_yaml_naming_this_agent_is_not_a_blocker(session_factory, config):
    """A retried connect, or an owner who prepared the file themselves. The
    wiring step updates it in place."""
    github = FakeGitHub(
        existing_repos={REPO},
        root_entries=["Dockerfile", "gojira.yaml"],
        repo_files={
            "Dockerfile": FAKE_CONNECTED_DOCKERFILE,
            "gojira.yaml": f"app: {NAME}\n",
        },
    )

    report = _scan(session_factory, config, github)

    assert report.blockers == []


def test_a_gojira_yaml_the_room_cannot_read_names_no_app(session_factory, config):
    """Unparseable YAML says nothing about which app this repository is,
    and the room does not invent a blocker out of a file it cannot read."""
    github = FakeGitHub(
        existing_repos={REPO},
        root_entries=["Dockerfile", "gojira.yaml"],
        repo_files={
            "Dockerfile": FAKE_CONNECTED_DOCKERFILE,
            "gojira.yaml": "app: [unclosed\n",
        },
    )

    report = _scan(session_factory, config, github)

    assert report.blockers == []


def test_a_name_already_in_the_room_is_a_blocker(session_factory, config):
    _take(session_factory, NAME)

    report = _scan(session_factory, config, FakeGitHub(existing_repos={REPO}))

    assert _codes(report.blockers) == [NAME_TAKEN]


def test_headless_is_a_blocker_until_the_room_has_a_class_for_it(
    session_factory, config
):
    report = _scan(
        session_factory,
        config,
        FakeGitHub(existing_repos={REPO}),
        run_mode=RUN_MODE_HEADLESS,
    )

    assert _codes(report.blockers) == [HEADLESS_UNAVAILABLE_CODE]
    # The same sentence the step raises, word for word: it is the same
    # fact, and nobody should have to work out that it is.
    assert report.blockers[0].message == HEADLESS_UNAVAILABLE


def test_headless_is_fine_once_the_deploy_platform_names_a_class(
    session_factory, tmp_path
):
    config = Config(
        workdir=tmp_path / "work",
        github_org=ORG,
        connected_headless_class="agent-headless",
    )

    report = _scan(
        session_factory,
        config,
        FakeGitHub(existing_repos={REPO}),
        run_mode=RUN_MODE_HEADLESS,
    )

    assert report.blockers == []


# --- Warnings -------------------------------------------------------------------


def test_a_base_image_that_is_not_pinned_by_digest_warns(session_factory, config):
    github = FakeGitHub(
        existing_repos={REPO},
        repo_files={"Dockerfile": "FROM registry.example/agents/base:latest\nEXPOSE 8080\n"},
    )

    report = _scan(session_factory, config, github)

    assert report.blockers == []
    assert _codes(report.warnings) == [BASE_NOT_PINNED]


def test_a_base_image_from_another_registry_warns(session_factory, config):
    github = FakeGitHub(
        existing_repos={REPO},
        repo_files={
            "Dockerfile": (
                "FROM docker.io/library/python@sha256:"
                + "2" * 64
                + "\nEXPOSE 8080\n"
            )
        },
    )

    report = _scan(session_factory, config, github)

    assert _codes(report.warnings) == [BASE_OUTSIDE_REGISTRY]


def test_the_final_stage_is_the_base_that_is_judged(session_factory, config):
    """A multi-stage build's earlier stages never run in the cluster. The
    stage that produces the image is the one worth warning about, and a
    FROM that names an earlier stage is not a base image at all."""
    github = FakeGitHub(
        existing_repos={REPO},
        repo_files={
            "Dockerfile": (
                "FROM docker.io/library/golang:1.22 AS builder\n"
                "RUN go build\n"
                "FROM registry.example/agents/base@sha256:" + "1" * 64 + "\n"
                "COPY --from=builder /out /app\n"
                "EXPOSE 8080\n"
            )
        },
    )

    report = _scan(session_factory, config, github)

    assert report.warnings == []


def test_a_final_from_that_names_an_earlier_stage_looks_further_back(
    session_factory, config
):
    github = FakeGitHub(
        existing_repos={REPO},
        repo_files={
            "Dockerfile": (
                "FROM registry.example/agents/base:latest AS app\n"
                "EXPOSE 8080\n"
                "FROM app\n"
                "CMD [\"/app/serve\"]\n"
            )
        },
    )

    report = _scan(session_factory, config, github)

    assert _codes(report.warnings) == [BASE_NOT_PINNED]


def test_a_committed_env_file_warns(session_factory, config):
    github = FakeGitHub(
        existing_repos={REPO}, root_entries=["Dockerfile", ".env"]
    )

    report = _scan(session_factory, config, github)

    assert _codes(report.warnings) == [ENV_FILE_COMMITTED]


def test_a_protected_default_branch_warns(session_factory, config):
    github = FakeGitHub(existing_repos={REPO}, protected_branches={"main"})

    report = _scan(session_factory, config, github)

    assert _codes(report.warnings) == [BRANCH_PROTECTED]
    assert "main" in report.warnings[0].message


def test_a_bundle_directory_warns_that_this_looks_composed(session_factory, config):
    github = FakeGitHub(
        existing_repos={REPO}, root_entries=["Dockerfile", "bundle"]
    )

    report = _scan(session_factory, config, github)

    assert _codes(report.warnings) == [LOOKS_COMPOSED]


def test_nothing_exposing_the_port_warns_when_it_was_said_to_serve_http(
    session_factory, config
):
    github = FakeGitHub(
        existing_repos={REPO},
        repo_files={"Dockerfile": "FROM registry.example/agents/base@sha256:" + "1" * 64 + "\n"},
    )

    report = _scan(session_factory, config, github)

    assert _codes(report.warnings) == [NO_PORT_EXPOSED]


def test_nothing_exposing_the_port_is_not_worth_saying_for_a_headless_agent(
    session_factory, tmp_path
):
    config = Config(
        workdir=tmp_path / "work",
        github_org=ORG,
        connected_headless_class="agent-headless",
    )
    github = FakeGitHub(
        existing_repos={REPO},
        repo_files={"Dockerfile": "FROM registry.example/agents/base@sha256:" + "1" * 64 + "\n"},
    )

    report = _scan(session_factory, config, github, run_mode=RUN_MODE_HEADLESS)

    assert report.warnings == []


def test_warnings_never_block(session_factory, config):
    github = FakeGitHub(
        existing_repos={REPO},
        root_entries=["Dockerfile", ".env", "bundle"],
        repo_files={"Dockerfile": "FROM docker.io/library/python:3.12\n"},
        protected_branches={"main"},
    )

    report = _scan(session_factory, config, github)

    assert report.blockers == []
    assert set(_codes(report.warnings)) == {
        BASE_NOT_PINNED,
        BASE_OUTSIDE_REGISTRY,
        ENV_FILE_COMMITTED,
        BRANCH_PROTECTED,
        LOOKS_COMPOSED,
        NO_PORT_EXPOSED,
    }


# --- What it costs --------------------------------------------------------------


def test_a_whole_scan_reads_github_five_times(session_factory, config):
    """The repository, its root, its Dockerfile, its gojira.yaml, its
    default branch. A form that scans on every keystroke would otherwise
    spend a team's whole API budget, so the count is pinned here rather
    than left to whoever adds the next finding.
    """
    github = FakeGitHub(
        existing_repos={REPO},
        root_entries=["Dockerfile", "gojira.yaml"],
        repo_files={
            "Dockerfile": FAKE_CONNECTED_DOCKERFILE,
            "gojira.yaml": f"app: {NAME}\n",
        },
    )

    _scan(session_factory, config, github)

    assert github.calls == [
        f"repo_access:{REPO}",
        f"list_root:{REPO}",
        f"read_file:{REPO}:Dockerfile",
        f"read_file:{REPO}:gojira.yaml",
        f"branch_protected:{REPO}:main",
    ]
    assert len(github.calls) <= 6


def test_the_scan_never_clones_and_never_writes(session_factory, config):
    github = FakeGitHub(existing_repos={REPO})

    _scan(session_factory, config, github)

    assert github.cloned == []
    assert github.pushes == []
    assert github.open_pr_calls == []
    assert github.created_from_dir == []

"""Tests for kobo_api.destroy: the six-step teardown ledger, the mirror of
kobo_api.provisioner's PIPELINE/run_pipeline.

Two layers, same split as test_steps.py/test_engine.py:

- Step-level: each destroy step called directly against a `DestroyCtx` built
  from a persisted `Agent` (mirrors test_steps.py's `_make_agent`/`_make_ctx`
  idiom) and the platform-client fakes (tests/fakes.py) — no threading
  involved, exact return-value/side-effect assertions.
- Engine-level: `run_destroy` fired as a real (fast, fake-backed) daemon
  thread and joined, exercising the full ledger against `session_factory`
  (mirrors test_engine.py's `run_pipeline` idiom) — resume-from-failure,
  abort-remaining-on-failure, and the row/step rows actually disappearing
  once every step is done.
"""

from __future__ import annotations

import json

import pytest

from kobo_api import destroy as destroy_module
from kobo_api.clients import ClientError, Clients
from kobo_api.destroy import (
    DESTROY_STEPS,
    DestroyCtx,
    _destroy_access,
    _destroy_directory,
    _destroy_forget,
    _destroy_platform,
    _destroy_repo,
    _destroy_secrets,
    _destroy_slack,
    _is_already_archived,
    _is_repo_not_found,
    run_destroy,
)
from kobo_api.kinds import KIND_COMPOSED, KIND_CONNECTED
from kobo_api.models import A2AInvite, Agent, AgentStatus, AgentStep, StepStatus
from tests.fakes import (
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeLadon,
    FakeSlack,
)

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
MODEL_ID = "vertex_ai/claude-sonnet-4-5"
OWNER = "acme-corp"
REPO = f"{OWNER}/agent-{AGENT_NAME}"
# The person the secrets platform records a removal against: an agent's
# own creator, which is the only name the destroy ledger has to give it.
OWNER_EMAIL = "owner@example.com"

JOIN_TIMEOUT = 5


# --- shared helpers -----------------------------------------------------------


def _make_ctx_agent(
    session_factory,
    *,
    name: str = AGENT_NAME,
    gojira_app_id: int | None = None,
    slack_app_id: str | None = None,
    slack_app_self_service: bool = False,
    repo_full_name: str | None = None,
    status=AgentStatus.READY,
    kind: str = KIND_COMPOSED,
):
    """Persist an Agent and return (session, agent) still open (expire_on_
    commit=False), for step-level tests that build a DestroyCtx directly —
    mirrors test_steps.py's _make_agent idiom exactly."""
    session = session_factory()
    agent = Agent(
        name=name,
        persona=PERSONA,
        model_id=MODEL_ID,
        status=status,
        gojira_app_id=gojira_app_id,
        slack_app_id=slack_app_id,
        slack_app_self_service=slack_app_self_service,
        repo_full_name=repo_full_name,
        kind=kind,
    )
    session.add(agent)
    session.commit()
    return session, agent


def _ctx(
    session,
    agent,
    config,
    *,
    github=None,
    gojira=None,
    slack=None,
    cloud=None,
    cabal=None,
    ladon=None,
    acting_email=None,
) -> DestroyCtx:
    # `cloud` defaults to a fresh, unseeded FakeCloud rather than None: since
    # destroy-access (unlike the other steps) has no `agent.<field>` gate and
    # so always runs, every step-level test that isn't specifically about
    # destroy-access still needs a working cloud client to reach the step
    # after it. Tests that care about destroy-access's own behavior pass
    # their own FakeCloud explicitly.
    clients = Clients(
        github=github,
        gojira=gojira,
        cloud=cloud if cloud is not None else FakeCloud(),
        agent=None,
        kubectl=None,
        slack=slack,
        cabal=cabal,
        ladon=ladon,
    )
    return DestroyCtx(
        agent=agent,
        session=session,
        config=config,
        clients=clients,
        acting_email=acting_email,
    )


def _seed_agent(session_factory, *, name: str = AGENT_NAME, **kwargs) -> int:
    """Persist an Agent, close the session, and return only its id — for
    engine-level (run_destroy) tests, mirrors test_engine.py's _seed_agent."""
    session = session_factory()
    agent = Agent(name=name, persona=PERSONA, model_id=MODEL_ID, **kwargs)
    session.add(agent)
    session.commit()
    agent_id = agent.id
    session.close()
    return agent_id


def _clients(
    *, github=None, gojira=None, slack=None, cloud=None, cabal=None, ladon=None
) -> Clients:
    # Same default-to-a-working-FakeCloud rationale as `_ctx` above, for the
    # engine-level (run_destroy) tests.
    return Clients(
        github=github,
        gojira=gojira,
        cloud=cloud if cloud is not None else FakeCloud(),
        agent=None,
        kubectl=None,
        slack=slack,
        cabal=cabal,
        ladon=ladon,
    )


# --- DESTROY_STEPS shape --------------------------------------------------------


def test_destroy_steps_order_is_fixed():
    assert DESTROY_STEPS == (
        "destroy-secrets",
        "destroy-platform",
        "destroy-access",
        "destroy-slack",
        "destroy-repo",
        "destroy-directory",
        "destroy-forget",
    )


# --- _is_repo_not_found / _is_already_archived (pure helpers) ------------------


def test_is_repo_not_found_matches_the_gh_cli_prefix():
    assert _is_repo_not_found(ClientError("repo not found: acme/agent-clio")) is True


def test_is_repo_not_found_false_for_other_messages():
    assert _is_repo_not_found(ClientError("some other failure")) is False
    assert _is_repo_not_found(ClientError("repo not found somewhere in the middle")) is False


def test_is_already_archived_true_for_real_github_403_message():
    exc = ClientError(
        "gh api ... failed (rc=1): gh: Repository was archived so is read-only. (HTTP 403)"
    )
    assert _is_already_archived(exc) is True


def test_is_already_archived_is_case_insensitive():
    exc = ClientError("gh: REPOSITORY WAS ARCHIVED SO IS READ-ONLY. (HTTP 403)")
    assert _is_already_archived(exc) is True


def test_is_already_archived_false_without_403_marker():
    exc = ClientError("gh: Repository was archived so is read-only. (HTTP 404)")
    assert _is_already_archived(exc) is False


def test_is_already_archived_false_without_archived_substring():
    exc = ClientError("gh: some other 403 (HTTP 403)")
    assert _is_already_archived(exc) is False


# --- _destroy_platform ----------------------------------------------------------


def test_destroy_platform_no_app_id_is_done_without_calling_gojira(session_factory, config):
    session, agent = _make_ctx_agent(session_factory, gojira_app_id=None)
    fake_gojira = FakeGojira()
    ctx = _ctx(session, agent, config, gojira=fake_gojira)

    detail = _destroy_platform(ctx)

    assert detail == "no platform app to tear down"
    assert fake_gojira.delete_calls == []
    session.close()


def test_destroy_platform_already_gone_is_done(session_factory, config):
    session, agent = _make_ctx_agent(session_factory, gojira_app_id=7)
    fake_gojira = FakeGojira(delete_already_gone=True)
    ctx = _ctx(session, agent, config, gojira=fake_gojira)

    detail = _destroy_platform(ctx)

    assert detail == "already gone"
    assert fake_gojira.delete_calls == [(7, True)]
    session.close()


def test_destroy_platform_summary_is_json_dumped(session_factory, config):
    session, agent = _make_ctx_agent(session_factory, gojira_app_id=7)
    summary = {"app": AGENT_NAME, "namespaces_deleted": ["dev"], "rows_deleted": True}
    fake_gojira = FakeGojira(delete_summary=summary)
    ctx = _ctx(session, agent, config, gojira=fake_gojira)

    detail = _destroy_platform(ctx)

    assert json.loads(detail) == summary
    session.close()


def test_destroy_platform_client_error_propagates(session_factory, config):
    session, agent = _make_ctx_agent(session_factory, gojira_app_id=7)
    fake_gojira = FakeGojira(delete_error="platform unreachable")
    ctx = _ctx(session, agent, config, gojira=fake_gojira)

    with pytest.raises(ClientError, match="platform unreachable"):
        _destroy_platform(ctx)
    session.close()


def test_destroy_platform_identity_failures_are_logged_as_warning(session_factory, config, caplog):
    # Final-review Finding B: gojira's teardown summary can report partial,
    # unretryable GCP identity failures (a leaked GSA/binding/image) once
    # gojira's own rows are gone. The step still succeeds (best-effort is
    # gojira's contract), but those failures must not disappear into a
    # log.info json.dumps with nothing calling them out.
    session, agent = _make_ctx_agent(session_factory, name="clio", gojira_app_id=7)
    summary = {
        "app": AGENT_NAME,
        "namespaces_deleted": ["dev"],
        "namespace_failures": [],
        "identity": {
            "bindings_removed": 1,
            "gsa_deleted": False,
            "images_deleted": 0,
            "failures": [
                "gsa delete failed: still in use",
                "image cleanup failed: permission denied",
            ],
        },
        "rows_deleted": True,
    }
    fake_gojira = FakeGojira(delete_summary=summary)
    ctx = _ctx(session, agent, config, gojira=fake_gojira)

    with caplog.at_level("WARNING"):
        detail = _destroy_platform(ctx)

    # The step still succeeds, and the returned detail is unchanged.
    assert json.loads(detail) == summary

    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("clio" in message for message in warnings)
    assert any("gsa delete failed: still in use" in message for message in warnings)
    assert any("image cleanup failed: permission denied" in message for message in warnings)
    session.close()


def test_destroy_platform_no_identity_failures_no_warning(session_factory, config, caplog):
    session, agent = _make_ctx_agent(session_factory, gojira_app_id=7)
    fake_gojira = FakeGojira()  # canned summary's identity.failures is []
    ctx = _ctx(session, agent, config, gojira=fake_gojira)

    with caplog.at_level("WARNING"):
        _destroy_platform(ctx)

    assert not any(r.levelname == "WARNING" for r in caplog.records)
    session.close()


# --- _destroy_access --------------------------------------------------------------


def test_destroy_access_removes_membership_then_gsa_in_order(session_factory, config):
    session, agent = _make_ctx_agent(session_factory)
    email = f"agent-{AGENT_NAME}@{config.gcp_project}.iam.gserviceaccount.com"
    cloud = FakeCloud(project=config.gcp_project)
    cloud.gsas.add(email)
    cloud.group_members.add((email, config.model_group))
    ctx = _ctx(session, agent, config, cloud=cloud)

    detail = _destroy_access(ctx)

    assert detail == "membership removed: True, gsa deleted: True"
    # Order pinned: membership removal happens before the GSA delete (a
    # deleted GSA's ghost membership would be unremovable by email).
    remove_index = cloud.calls.index(f"remove_from_group:{email}:{config.model_group}")
    delete_index = cloud.calls.index(f"delete_gsa:{email}")
    assert remove_index < delete_index
    assert email not in cloud.gsas
    assert (email, config.model_group) not in cloud.group_members
    session.close()


def test_destroy_access_absent_membership_proceeds_to_gsa(session_factory, config):
    """Membership already gone (never granted, or a prior partial attempt
    already removed it) doesn't stop the step - it proceeds straight to
    deleting the GSA."""
    session, agent = _make_ctx_agent(session_factory)
    email = f"agent-{AGENT_NAME}@{config.gcp_project}.iam.gserviceaccount.com"
    cloud = FakeCloud(project=config.gcp_project)
    cloud.gsas.add(email)  # GSA still present, membership is not
    ctx = _ctx(session, agent, config, cloud=cloud)

    detail = _destroy_access(ctx)

    assert detail == "membership removed: False, gsa deleted: True"
    assert email not in cloud.gsas
    session.close()


def test_destroy_access_absent_gsa_is_done(session_factory, config):
    """The GSA is already gone (a prior attempt got all the way through the
    delete but crashed before its row committed DONE) - tolerated as done,
    not a failure, even though membership still needed removing."""
    session, agent = _make_ctx_agent(session_factory)
    email = f"agent-{AGENT_NAME}@{config.gcp_project}.iam.gserviceaccount.com"
    cloud = FakeCloud(project=config.gcp_project)
    cloud.group_members.add((email, config.model_group))  # membership present, GSA already gone
    ctx = _ctx(session, agent, config, cloud=cloud)

    detail = _destroy_access(ctx)

    assert detail == "membership removed: True, gsa deleted: False"
    assert (email, config.model_group) not in cloud.group_members
    session.close()


def test_destroy_access_real_failure_propagates(session_factory, config):
    session, agent = _make_ctx_agent(session_factory)
    cloud = FakeCloud(project=config.gcp_project, remove_group_error="group service outage")
    ctx = _ctx(session, agent, config, cloud=cloud)

    with pytest.raises(ClientError, match="group service outage"):
        _destroy_access(ctx)
    session.close()


# --- _destroy_slack ---------------------------------------------------------------


def test_destroy_slack_no_app_id_is_done_without_calling_slack(session_factory, config):
    session, agent = _make_ctx_agent(session_factory, slack_app_id=None)
    fake_slack = FakeSlack()
    ctx = _ctx(session, agent, config, slack=fake_slack)

    detail = _destroy_slack(ctx)

    assert detail == "no slack app to remove"
    assert fake_slack.delete_calls == []
    session.close()


def test_destroy_slack_true_is_done(session_factory, config):
    session, agent = _make_ctx_agent(session_factory, slack_app_id="A0FAKE")
    fake_slack = FakeSlack(delete_result=True)
    ctx = _ctx(session, agent, config, slack=fake_slack)

    detail = _destroy_slack(ctx)

    assert detail == "slack app deleted"
    assert fake_slack.delete_calls == ["A0FAKE"]
    session.close()


def test_destroy_slack_false_is_already_gone(session_factory, config):
    session, agent = _make_ctx_agent(session_factory, slack_app_id="A0FAKE")
    fake_slack = FakeSlack(delete_result=False)
    ctx = _ctx(session, agent, config, slack=fake_slack)

    detail = _destroy_slack(ctx)

    assert detail == "already gone"
    session.close()


def test_destroy_slack_client_error_propagates(session_factory, config):
    session, agent = _make_ctx_agent(session_factory, slack_app_id="A0FAKE")
    fake_slack = FakeSlack(delete_error="slack unreachable")
    ctx = _ctx(session, agent, config, slack=fake_slack)

    with pytest.raises(ClientError, match="slack unreachable"):
        _destroy_slack(ctx)
    session.close()


def test_destroy_slack_self_service_app_skips_delete_and_notes_provenance(
    session_factory, config
):
    """A self-service app (Task 1's `slack_app_self_service`) was never
    created by kobo's own config-token app — this room has no business
    deleting something it doesn't own. The step must not call
    `delete_app` at all; it completes with the exact provenance note
    (Global Constraints: verbatim, period-separated, no em dash)."""
    session, agent = _make_ctx_agent(
        session_factory, slack_app_id="A0FAKE", slack_app_self_service=True
    )
    fake_slack = FakeSlack()
    ctx = _ctx(session, agent, config, slack=fake_slack)

    detail = _destroy_slack(ctx)

    assert detail == "The Slack app belongs to its creator. Remove it in Slack."
    assert fake_slack.delete_calls == []
    session.close()


def test_destroy_slack_legacy_app_still_deletes_when_self_service_false(
    session_factory, config
):
    """Regression: a room-created (legacy, config-token) app has
    `slack_app_self_service=False` — the byte-identical delete_app path
    from before this change."""
    session, agent = _make_ctx_agent(
        session_factory, slack_app_id="A0FAKE", slack_app_self_service=False
    )
    fake_slack = FakeSlack(delete_result=True)
    ctx = _ctx(session, agent, config, slack=fake_slack)

    detail = _destroy_slack(ctx)

    assert detail == "slack app deleted"
    assert fake_slack.delete_calls == ["A0FAKE"]
    session.close()


def test_destroy_slack_self_service_no_app_id_keeps_no_id_tolerance(
    session_factory, config
):
    """A self-service agent that never got as far as connecting a Slack
    app has no `slack_app_id` either — the existing no-id-done tolerance
    is unchanged by the self-service flag."""
    session, agent = _make_ctx_agent(
        session_factory, slack_app_id=None, slack_app_self_service=True
    )
    fake_slack = FakeSlack()
    ctx = _ctx(session, agent, config, slack=fake_slack)

    detail = _destroy_slack(ctx)

    assert detail == "no slack app to remove"
    assert fake_slack.delete_calls == []
    session.close()


# --- _destroy_repo -----------------------------------------------------------------


def test_destroy_repo_leaves_a_connected_agents_repository_alone(session_factory, config):
    """The one branch that is the difference between a clean destroy and
    archiving a team's repository. This step does NOT self-neutralise the
    way destroy-slack does: `repo_full_name` is set for a connected agent
    (the connect route records it), so without this it would happily rename
    and archive somebody else's repo."""
    session, agent = _make_ctx_agent(
        session_factory, repo_full_name=f"{OWNER}/team-chatbot", kind=KIND_CONNECTED
    )
    fake_github = FakeGitHub(existing_repos={f"{OWNER}/team-chatbot"})
    ctx = _ctx(session, agent, config, github=fake_github)

    detail = _destroy_repo(ctx)

    assert detail == "the repository belongs to its team and is left as it is"
    assert fake_github.calls == []
    assert fake_github.renamed == {}
    assert fake_github.archived == set()
    session.close()


@pytest.mark.parametrize("kind", ["something-else", "adopted", ""])
def test_destroy_repo_archives_only_for_an_explicitly_composed_agent(
    session_factory, config, kind
):
    """`is_connected` fails open to composed, which everywhere else is the
    safe direction and here is the destructive one. A kind this code does
    not recognise -- a future one, a bad write, a half-finished migration --
    must not be read as permission to archive somebody's repository."""
    session, agent = _make_ctx_agent(
        session_factory, repo_full_name=f"{OWNER}/team-chatbot", kind=kind
    )
    fake_github = FakeGitHub(existing_repos={f"{OWNER}/team-chatbot"})
    ctx = _ctx(session, agent, config, github=fake_github)

    detail = _destroy_repo(ctx)

    assert detail == "the repository belongs to its team and is left as it is"
    assert fake_github.calls == []
    session.close()


def test_destroy_repo_still_archives_a_repository_the_room_owns(session_factory, config):
    """The composed side of that same branch, pinned here so the connected
    one can never be widened into "never archive anything"."""
    session, agent = _make_ctx_agent(
        session_factory, repo_full_name=REPO, kind=KIND_COMPOSED
    )
    fake_github = FakeGitHub(existing_repos={REPO})
    ctx = _ctx(session, agent, config, github=fake_github)

    target = f"{OWNER}/archived-agent-{AGENT_NAME}-{agent.id}"
    detail = _destroy_repo(ctx)

    assert detail == f"archived as {target}"
    assert fake_github.renamed[REPO] == target
    assert target in fake_github.archived
    session.close()


def test_destroy_repo_no_repo_full_name_is_done_without_calling_github(session_factory, config):
    session, agent = _make_ctx_agent(session_factory, repo_full_name=None)
    fake_github = FakeGitHub()
    ctx = _ctx(session, agent, config, github=fake_github)

    detail = _destroy_repo(ctx)

    assert detail == "no repo to archive"
    assert fake_github.rename_calls == []
    assert fake_github.archive_calls == []
    session.close()


def test_destroy_repo_happy_path_renames_then_archives(session_factory, config):
    session, agent = _make_ctx_agent(session_factory, repo_full_name=REPO)
    fake_github = FakeGitHub(existing_repos={REPO})
    ctx = _ctx(session, agent, config, github=fake_github)

    target = f"{OWNER}/archived-agent-{AGENT_NAME}-{agent.id}"
    detail = _destroy_repo(ctx)

    assert detail == f"archived as {target}"
    assert fake_github.renamed[REPO] == target
    assert target in fake_github.archived
    session.close()


def test_destroy_repo_ambiguous_404_corroborated_by_archived_name_proceeds_to_archive(
    session_factory, config
):
    """gh renders a permission-denied PATCH as a literal 404, byte-identical
    to a missing repo — so a bare 404 renaming the CURRENT name must never
    alone be trusted as already-gone. Here the current repo is genuinely
    absent (renamed away by a prior attempt) and the archived target name
    already exists: corroborated, so the step proceeds straight to
    archiving the target rather than failing."""
    session, agent = _make_ctx_agent(session_factory, repo_full_name=REPO)
    target = f"{OWNER}/archived-agent-{AGENT_NAME}-{agent.id}"
    fake_github = FakeGitHub(existing_repos={target})
    ctx = _ctx(session, agent, config, github=fake_github)

    detail = _destroy_repo(ctx)

    assert detail == f"archived as {target}"
    assert fake_github.rename_calls == [(REPO, f"archived-agent-{AGENT_NAME}-{agent.id}")]
    assert target in fake_github.archived
    session.close()


def test_destroy_repo_both_names_404_fails_with_plain_reason(session_factory, config):
    """Neither the current name nor the archived target exists — never
    guessed as success on this ambiguity; the step fails with a plain
    reason instead."""
    session, agent = _make_ctx_agent(session_factory, repo_full_name=REPO)
    fake_github = FakeGitHub(existing_repos=set())
    ctx = _ctx(session, agent, config, github=fake_github)

    with pytest.raises(RuntimeError) as excinfo:
        _destroy_repo(ctx)

    message = str(excinfo.value)
    assert "—" not in message and "--" not in message  # no em dash, plain copy
    assert "old or archived name" in message
    session.close()


def test_destroy_repo_already_archived_403_on_archive_is_done(session_factory, config):
    """The renamed target already exists AND is already archived (a prior
    attempt got all the way through archiving but crashed before its row
    committed DONE) — the archive PATCH 403s with GitHub's real
    already-archived message; recognized and treated as done, not a
    failure."""
    session, agent = _make_ctx_agent(session_factory, repo_full_name=REPO)
    fake_github = FakeGitHub(
        existing_repos={REPO},
        archive_error="Repository was archived so is read-only. (HTTP 403)",
    )
    ctx = _ctx(session, agent, config, github=fake_github)

    target = f"{OWNER}/archived-agent-{AGENT_NAME}-{agent.id}"
    detail = _destroy_repo(ctx)

    assert detail == f"archived as {target}"
    session.close()


def test_destroy_repo_non_404_rename_failure_propagates_without_corroboration(
    session_factory, config
):
    session, agent = _make_ctx_agent(session_factory, repo_full_name=REPO)
    fake_github = FakeGitHub(existing_repos={REPO}, rename_error="permission denied outright")

    ctx = _ctx(session, agent, config, github=fake_github)

    with pytest.raises(ClientError, match="permission denied outright"):
        _destroy_repo(ctx)
    # Never even checked repo_exists — a non-404 rename failure is a real
    # failure, not an already-gone ambiguity to corroborate.
    assert "repo_exists:" not in fake_github.calls
    session.close()


def test_destroy_repo_non_403_archive_failure_propagates(session_factory, config):
    session, agent = _make_ctx_agent(session_factory, repo_full_name=REPO)
    fake_github = FakeGitHub(existing_repos={REPO}, archive_error="some real archive failure")
    ctx = _ctx(session, agent, config, github=fake_github)

    with pytest.raises(ClientError, match="some real archive failure"):
        _destroy_repo(ctx)
    session.close()


# --- _destroy_directory (colleagues switchboard plan, Task 2) ---------------------


def _cabal_enabled_config(config):
    """`config` plus the two org keys `cabal_enabled` needs — the caller
    must also monkeypatch CABAL_OPERATOR_TOKEN, the third leg, before this
    reads as enabled."""
    return config.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": "http://agent-{name}.default.svc.cluster.local",
        }
    )


def test_destroy_directory_says_a_connected_agent_was_never_listed(
    session_factory, config, monkeypatch
):
    """A connected agent has no switchboard identity to retire: an agent
    connected from the start never had one (its pipeline has no directory
    step), and a converted one had its entry retired at the moment it was
    converted. The note says what is true of both, rather than claiming
    the converted one was never listed."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    session, agent = _make_ctx_agent(session_factory, kind=KIND_CONNECTED)
    fake_cabal = FakeCabal()
    ctx = _ctx(session, agent, cfg, cabal=fake_cabal)

    detail = _destroy_directory(ctx)

    assert detail == "no directory entry to retire"
    assert fake_cabal.absorb_calls == []
    session.close()


def test_destroy_directory_disabled_is_done_with_note_and_never_calls_cabal(
    session_factory, config
):
    """No switchboard configured (the shared `config` fixture carries
    neither org key) — completes with the fixed note rather than raising,
    and never touches the cabal client at all."""
    session, agent = _make_ctx_agent(session_factory)
    fake_cabal = FakeCabal()
    ctx = _ctx(session, agent, config, cabal=fake_cabal)

    detail = _destroy_directory(ctx)

    assert detail == "The directory entry stays until the switchboard returns."
    assert fake_cabal.absorb_calls == []
    session.close()


def test_destroy_directory_enabled_absorbs_the_agent_by_name(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    session, agent = _make_ctx_agent(session_factory)
    fake_cabal = FakeCabal()
    ctx = _ctx(session, agent, cfg, cabal=fake_cabal)

    detail = _destroy_directory(ctx)

    assert detail == "removed from the directory"
    assert fake_cabal.absorb_calls == [agent.name]
    session.close()


def test_destroy_directory_unreachable_is_done_with_note_not_failed(
    session_factory, config, monkeypatch
):
    """Configured but failing (a CabalError from the switchboard itself) —
    still completes with the same note rather than propagating, unlike
    every other destroy step: a directory hiccup must never hold an
    otherwise-clean teardown hostage."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    session, agent = _make_ctx_agent(session_factory)
    fake_cabal = FakeCabal(absorb_error="switchboard unreachable")
    ctx = _ctx(session, agent, cfg, cabal=fake_cabal)

    detail = _destroy_directory(ctx)

    assert detail == "The directory entry stays until the switchboard returns."
    session.close()


def test_destroy_directory_any_failure_reads_as_unreachable(session_factory, config, monkeypatch):
    """Not only CabalError - a raw connection failure (never wrapped by
    CabalClient's own graduate/absorb, which only ever inspect a response
    that DID come back) must read the same "unreachable" way, never as a
    step failure."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    session, agent = _make_ctx_agent(session_factory)
    fake_cabal = FakeCabal()

    def _raise_connection_error(name):
        raise ConnectionError("connection refused")

    fake_cabal.absorb = _raise_connection_error
    ctx = _ctx(session, agent, cfg, cabal=fake_cabal)

    detail = _destroy_directory(ctx)

    assert detail == "The directory entry stays until the switchboard returns."
    session.close()


# --- _destroy_forget ---------------------------------------------------------------


def test_destroy_forget_deletes_step_rows_and_the_agent_row(session_factory, config):
    session, agent = _make_ctx_agent(session_factory)
    session.add(AgentStep(agent_id=agent.id, name="destroy-platform", status=StepStatus.DONE))
    session.add(AgentStep(agent_id=agent.id, name="destroy-forget", status=StepStatus.RUNNING))
    session.commit()
    agent_id = agent.id

    ctx = _ctx(session, agent, config)
    detail = _destroy_forget(ctx)

    assert detail == "forgotten"
    assert session.get(Agent, agent_id) is None
    assert session.query(AgentStep).filter_by(agent_id=agent_id).count() == 0
    session.close()


def test_destroy_forget_deletes_invites_in_both_directions(session_factory, config):
    """Destroy's forget step owns row deletion; colleague invites naming the
    departing agent are rows it used to leave behind. All three surviving
    production rows referenced destroyed agents."""
    session, agent = _make_ctx_agent(session_factory)
    departing_to_other = A2AInvite(from_agent=agent.name, to_agent="other")
    other_to_departing = A2AInvite(from_agent="other", to_agent=agent.name)
    other_to_unrelated = A2AInvite(from_agent="other", to_agent="unrelated")
    session.add_all([departing_to_other, other_to_departing, other_to_unrelated])
    session.commit()
    unrelated_id = other_to_unrelated.id

    ctx = _ctx(session, agent, config)
    detail = _destroy_forget(ctx)

    assert detail == "forgotten"
    remaining = session.query(A2AInvite).all()
    remaining_ids = {row.id for row in remaining}
    assert unrelated_id in remaining_ids
    assert len(remaining) == 1
    session.close()


# --- run_destroy: engine-level (real thread, real session_factory) -------------


def test_run_destroy_happy_path_deletes_agent_and_all_steps(session_factory, config):
    agent_id = _seed_agent(
        session_factory,
        status=AgentStatus.READY,
        gojira_app_id=7,
        slack_app_id="A0FAKE",
        repo_full_name=REPO,
    )
    fake_github = FakeGitHub(existing_repos={REPO})
    fake_gojira = FakeGojira()
    fake_slack = FakeSlack()
    fake_cloud = FakeCloud(project=config.gcp_project)
    email = f"agent-{AGENT_NAME}@{config.gcp_project}.iam.gserviceaccount.com"
    fake_cloud.gsas.add(email)
    fake_cloud.group_members.add((email, config.model_group))

    thread = run_destroy(
        session_factory,
        agent_id,
        _clients(github=fake_github, gojira=fake_gojira, slack=fake_slack, cloud=fake_cloud),
        config,
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    assert fake_gojira.delete_calls == [(7, True)]
    assert fake_slack.delete_calls == ["A0FAKE"]
    assert fake_github.rename_calls == [(REPO, f"archived-agent-{AGENT_NAME}-{agent_id}")]
    # destroy-access ran too — the GSA and its group membership are both gone.
    assert email not in fake_cloud.gsas
    assert (email, config.model_group) not in fake_cloud.group_members

    session = session_factory()
    assert session.get(Agent, agent_id) is None
    assert session.query(AgentStep).filter_by(agent_id=agent_id).count() == 0
    session.close()


def test_run_destroy_of_a_connected_agent_tears_down_everything_but_the_repository(
    session_factory, config, monkeypatch
):
    """Engine-level: the whole ledger runs to forget, the row is gone, and
    the team still has its repository and its directory untouched. Platform,
    namespace and model access come down exactly as for a composed agent."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    repo = f"{OWNER}/team-chatbot"
    agent_id = _seed_agent(
        session_factory,
        status=AgentStatus.READY,
        gojira_app_id=7,
        repo_full_name=repo,
        kind=KIND_CONNECTED,
    )
    fake_github = FakeGitHub(existing_repos={repo})
    fake_gojira = FakeGojira()
    fake_cabal = FakeCabal()
    fake_cloud = FakeCloud(project=cfg.gcp_project)
    email = f"agent-{AGENT_NAME}@{cfg.gcp_project}.iam.gserviceaccount.com"
    fake_cloud.gsas.add(email)
    fake_cloud.group_members.add((email, cfg.model_group))

    thread = run_destroy(
        session_factory,
        agent_id,
        _clients(github=fake_github, gojira=fake_gojira, cloud=fake_cloud, cabal=fake_cabal),
        cfg,
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    assert fake_gojira.delete_calls == [(7, True)]
    assert email not in fake_cloud.gsas
    assert (email, cfg.model_group) not in fake_cloud.group_members
    assert fake_github.calls == []
    assert fake_cabal.absorb_calls == []

    session = session_factory()
    assert session.get(Agent, agent_id) is None
    assert session.query(AgentStep).filter_by(agent_id=agent_id).count() == 0
    session.close()


def test_run_destroy_happy_path_retires_the_directory_listing_when_cabal_enabled(
    session_factory, config, monkeypatch
):
    """Engine-level: when the switchboard is configured, a full destroy run
    absorbs the agent from the directory too, right before destroy-forget
    deletes the row this stage still reads from."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    agent_id = _seed_agent(session_factory, status=AgentStatus.READY)
    fake_cabal = FakeCabal()

    thread = run_destroy(session_factory, agent_id, _clients(cabal=fake_cabal), cfg)
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    assert fake_cabal.absorb_calls == [AGENT_NAME]

    session = session_factory()
    assert session.get(Agent, agent_id) is None
    session.close()


# --- dispatch-pairs sync after destroy-forget (Task C4) ------------------------


def test_run_destroy_syncs_dispatch_pairs_after_forget_when_cabal_enabled(
    session_factory, config, monkeypatch
):
    """Engine-level: destroying an agent pushes the freshly-recomputed
    allowed_pairs set (the destroyed agent already gone from it) to the
    switchboard, right after destroy-forget deletes its row -- the two
    survivors' own same-group pair is what should be left."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    agent_id = _seed_agent(session_factory, status=AgentStatus.READY, group_name="growth")
    _seed_agent(session_factory, name="b", status=AgentStatus.READY, group_name="growth")
    _seed_agent(session_factory, name="c", status=AgentStatus.READY, group_name="growth")
    fake_cabal = FakeCabal()

    thread = run_destroy(session_factory, agent_id, _clients(cabal=fake_cabal), cfg)
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    assert fake_cabal.put_dispatch_pairs_calls == [
        sorted({("agent:b", "c"), ("agent:c", "b")})
    ]


def test_run_destroy_does_not_sync_dispatch_pairs_when_cabal_disabled(
    session_factory, config
):
    agent_id = _seed_agent(session_factory, status=AgentStatus.READY, group_name="growth")
    fake_cabal = FakeCabal()

    thread = run_destroy(session_factory, agent_id, _clients(cabal=fake_cabal), config)
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    assert fake_cabal.put_dispatch_pairs_calls == []


def test_run_destroy_completes_even_when_dispatch_pairs_sync_fails(
    session_factory, config, monkeypatch
):
    """Fail-soft: a CabalError from put_dispatch_pairs must never leave the
    agent's row behind or crash the destroy thread -- destroy-forget's own
    row deletion already committed by the time this sync runs."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    agent_id = _seed_agent(session_factory, status=AgentStatus.READY)
    fake_cabal = FakeCabal(put_dispatch_pairs_error="switchboard unreachable")

    thread = run_destroy(session_factory, agent_id, _clients(cabal=fake_cabal), cfg)
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    session = session_factory()
    assert session.get(Agent, agent_id) is None
    session.close()


# --- roster refresh after destroy-forget (fast-lane + discovery arc, Task 6) --


def test_run_destroy_redelivers_roster_without_cross_group_delivery(
    session_factory, config, monkeypatch
):
    """Global Constraints' load-bearing negative test, at the destroy-engine
    level: a room with agents in two groups must not cross-deliver. A
    same-group survivor and an unrelated-group survivor both exist; once
    the agent is destroyed, only the same-group survivor's colleagues.md is
    redelivered, and the redelivered roster no longer names the departed
    agent."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    agent_id = _seed_agent(session_factory, status=AgentStatus.READY, group_name="growth")
    _seed_agent(session_factory, name="same-group", status=AgentStatus.READY, group_name="growth")
    _seed_agent(session_factory, name="other-group", status=AgentStatus.READY, group_name="orbit")
    fake_cabal = FakeCabal(
        relay_result={"status": {"state": "completed", "message": {"parts": [{"text": "ok"}]}}}
    )

    thread = run_destroy(session_factory, agent_id, _clients(cabal=fake_cabal), cfg)
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    delivered_to = [
        call["name"]
        for call in fake_cabal.relay_calls
        if call["task_envelope"]["metadata"].get("filename") == "colleagues.md"
    ]
    assert "same-group" in delivered_to
    assert "other-group" not in delivered_to

    same_group_call = next(call for call in fake_cabal.relay_calls if call["name"] == "same-group")
    delivered_text = same_group_call["task_envelope"]["status"]["message"]["parts"][0]["text"]
    assert AGENT_NAME not in delivered_text


def test_run_destroy_does_not_redeliver_roster_when_cabal_disabled(session_factory, config):
    agent_id = _seed_agent(session_factory, status=AgentStatus.READY, group_name="growth")
    _seed_agent(session_factory, name="same-group", status=AgentStatus.READY, group_name="growth")
    fake_cabal = FakeCabal()

    thread = run_destroy(session_factory, agent_id, _clients(cabal=fake_cabal), config)
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    assert fake_cabal.relay_calls == []


def test_run_destroy_roster_refresh_failure_does_not_block_forget(
    session_factory, config, monkeypatch
):
    """Best-effort, no rebuild fallback (Global Constraints): a relay
    failure during the post-destroy roster refresh is logged and swallowed
    -- destroy-forget's own row deletion has already committed by the time
    this refresh runs, and a switchboard hiccup here must never undo it or
    crash the destroy thread."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    agent_id = _seed_agent(session_factory, status=AgentStatus.READY, group_name="growth")
    _seed_agent(session_factory, name="same-group", status=AgentStatus.READY, group_name="growth")
    fake_cabal = FakeCabal(relay_error="switchboard unreachable")

    thread = run_destroy(session_factory, agent_id, _clients(cabal=fake_cabal), cfg)
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    session = session_factory()
    assert session.get(Agent, agent_id) is None
    session.close()


def test_run_destroy_self_service_slack_records_note_and_completes_step(
    session_factory, config, caplog
):
    """Engine-level: a self-service app's destroy-slack step must reach
    DONE with zero Slack calls, and the provenance note must reach the
    ledger's logs the exact same way every other step's detail string
    does — `_run_destroy_body`'s own `log.info("destroy step %r done for
    agent %s: %s", ...)`, never something `_destroy_slack` logs itself.
    The repo step is made to fail (no repo of either name exists) so the
    run aborts before `destroy-forget` — leaving destroy-slack's own row
    inspectable as DONE rather than deleted along with everything else."""
    agent_id = _seed_agent(
        session_factory,
        status=AgentStatus.READY,
        gojira_app_id=None,
        slack_app_id="A0FAKE",
        slack_app_self_service=True,
        repo_full_name=REPO,
    )
    fake_slack = FakeSlack()
    fake_github = FakeGitHub(existing_repos=set())  # both names 404 -> real failure

    with caplog.at_level("INFO"):
        thread = run_destroy(
            session_factory, agent_id, _clients(github=fake_github, slack=fake_slack), config
        )
        thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    assert fake_slack.delete_calls == []

    session = session_factory()
    agent = session.get(Agent, agent_id)
    rows = {s.name: s.status for s in agent.steps}
    assert rows["destroy-slack"] == StepStatus.DONE
    session.close()

    assert any(
        "The Slack app belongs to its creator. Remove it in Slack." in message
        for message in caplog.messages
    )


def test_run_destroy_step_failure_leaves_agent_destroy_failed_and_resumable(
    session_factory, config
):
    agent_id = _seed_agent(
        session_factory,
        status=AgentStatus.READY,
        gojira_app_id=None,
        slack_app_id="A0FAKE",
        repo_full_name=REPO,
    )
    fake_slack = FakeSlack(delete_error="slack outage")

    thread = run_destroy(
        session_factory, agent_id, _clients(github=FakeGitHub(existing_repos={REPO}), slack=fake_slack), config
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    session = session_factory()
    agent = session.get(Agent, agent_id)
    assert agent is not None  # the row survives a failed run
    assert agent.status == AgentStatus.DESTROY_FAILED
    assert agent.error == "slack app deletion failed: slack outage"

    rows = {s.name: s for s in agent.steps}
    assert rows["destroy-platform"].status == StepStatus.DONE
    # destroy-access ran too (it has no gate of its own — always attempted)
    # and succeeded against the default FakeCloud before slack ever failed.
    assert rows["destroy-access"].status == StepStatus.DONE
    assert rows["destroy-slack"].status == StepStatus.FAILED
    assert rows["destroy-slack"].error == "slack app deletion failed: slack outage"
    # destroy-repo/destroy-forget never actually ran (aborted) — the repo
    # step's own side effects never fired.
    assert rows["destroy-repo"].status == StepStatus.FAILED
    assert rows["destroy-repo"].error == "aborted: destroy-slack"
    assert rows["destroy-forget"].status == StepStatus.FAILED
    assert rows["destroy-forget"].error == "aborted: destroy-slack"
    session.close()


def test_run_destroy_resume_skips_done_platform_and_retries_failed_slack(
    session_factory, config
):
    """The brief's exact resume scenario: fail slack once, re-run — platform
    is skipped (not retried), slack is retried and this time succeeds, and
    the run continues on through repo/forget."""
    agent_id = _seed_agent(
        session_factory,
        status=AgentStatus.READY,
        gojira_app_id=7,
        slack_app_id="A0FAKE",
        repo_full_name=REPO,
    )
    fake_gojira = FakeGojira()
    fake_github = FakeGitHub(existing_repos={REPO})
    fake_cloud = FakeCloud(project=config.gcp_project)
    failing_slack = FakeSlack(delete_error="slack outage")

    thread = run_destroy(
        session_factory,
        agent_id,
        _clients(github=fake_github, gojira=fake_gojira, slack=failing_slack, cloud=fake_cloud),
        config,
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False
    assert fake_gojira.delete_calls == [(7, True)]

    session = session_factory()
    agent = session.get(Agent, agent_id)
    assert agent.status == AgentStatus.DESTROY_FAILED
    rows = {s.name: s.status for s in agent.steps}
    assert rows["destroy-platform"] == StepStatus.DONE
    assert rows["destroy-access"] == StepStatus.DONE
    assert rows["destroy-slack"] == StepStatus.FAILED
    session.close()

    working_slack = FakeSlack()
    thread2 = run_destroy(
        session_factory,
        agent_id,
        _clients(github=fake_github, gojira=fake_gojira, slack=working_slack, cloud=fake_cloud),
        config,
    )
    thread2.join(timeout=JOIN_TIMEOUT)
    assert thread2.is_alive() is False

    # Platform and access were never retried (still exactly one call each
    # across both runs) — their rows were already DONE.
    assert fake_gojira.delete_calls == [(7, True)]
    email = f"agent-{AGENT_NAME}@{config.gcp_project}.iam.gserviceaccount.com"
    assert fake_cloud.calls == [
        f"remove_from_group:{email}:{config.model_group}",
        f"delete_gsa:{email}",
    ]
    assert working_slack.delete_calls == ["A0FAKE"]

    session = session_factory()
    assert session.get(Agent, agent_id) is None
    assert session.query(AgentStep).filter_by(agent_id=agent_id).count() == 0
    session.close()


def test_run_destroy_forget_only_runs_once_platform_slack_repo_are_all_done(
    session_factory, config
):
    """A step-1 (platform) failure must leave the agent row and every step
    row intact — destroy-forget's own delete logic must never have
    executed."""
    agent_id = _seed_agent(
        session_factory,
        status=AgentStatus.READY,
        gojira_app_id=7,
        slack_app_id=None,
        repo_full_name=None,
    )
    fake_gojira = FakeGojira(delete_error="platform outage")

    thread = run_destroy(session_factory, agent_id, _clients(gojira=fake_gojira), config)
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    session = session_factory()
    agent = session.get(Agent, agent_id)
    assert agent is not None
    assert agent.status == AgentStatus.DESTROY_FAILED
    rows = {s.name: s.status for s in agent.steps}
    assert rows["destroy-platform"] == StepStatus.FAILED
    assert rows["destroy-access"] == StepStatus.FAILED
    assert rows["destroy-slack"] == StepStatus.FAILED
    assert rows["destroy-repo"] == StepStatus.FAILED
    assert rows["destroy-forget"] == StepStatus.FAILED
    session.close()


def test_run_destroy_already_gone_matrix_for_a_never_fully_provisioned_agent(
    session_factory, config
):
    """An agent that FAILED early in provisioning (no platform app, no
    slack app, no repo at all) is still destroyable: every step is
    trivially done, and the row disappears. destroy-access has no such
    "never got that far" field to check (see its docstring) — it always
    attempts the teardown, tolerating an unseeded FakeCloud's False/False as
    already-done."""
    agent_id = _seed_agent(
        session_factory,
        status=AgentStatus.FAILED,
        gojira_app_id=None,
        slack_app_id=None,
        repo_full_name=None,
    )
    fake_gojira = FakeGojira()
    fake_slack = FakeSlack()
    fake_github = FakeGitHub()
    fake_cloud = FakeCloud(project=config.gcp_project)

    thread = run_destroy(
        session_factory,
        agent_id,
        _clients(github=fake_github, gojira=fake_gojira, slack=fake_slack, cloud=fake_cloud),
        config,
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    assert fake_gojira.delete_calls == []
    assert fake_slack.delete_calls == []
    assert fake_github.rename_calls == []
    email = f"agent-{AGENT_NAME}@{config.gcp_project}.iam.gserviceaccount.com"
    assert fake_cloud.calls == [
        f"remove_from_group:{email}:{config.model_group}",
        f"delete_gsa:{email}",
    ]

    session = session_factory()
    assert session.get(Agent, agent_id) is None
    session.close()


def test_run_destroy_unknown_agent_id_completes_without_raising(session_factory, config):
    thread = run_destroy(session_factory, 999_999, _clients(), config)
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    session = session_factory()
    assert session.query(Agent).count() == 0
    session.close()


# --- run_destroy: per-agent in-flight guard (review Finding 1) -----------------


def test_run_destroy_second_call_while_in_flight_does_not_spawn_a_duplicate_thread(
    session_factory, config
):
    """Simulates a destroy already in flight for this agent_id by seeding
    the module-level in-flight registry directly (no mocking of run_destroy
    or the route itself) — a second run_destroy call for the SAME agent_id
    must return None and must never touch the platform clients at all,
    i.e. no duplicate thread ever launches against the same AgentStep rows."""
    agent_id = _seed_agent(session_factory, status=AgentStatus.READY, gojira_app_id=7)
    fake_gojira = FakeGojira()

    destroy_module._destroying_agent_ids.add(agent_id)
    try:
        result = run_destroy(session_factory, agent_id, _clients(gojira=fake_gojira), config)
    finally:
        destroy_module._destroying_agent_ids.discard(agent_id)

    assert result is None
    assert fake_gojira.delete_calls == []


def test_run_destroy_releases_the_registry_slot_when_the_run_finishes(
    session_factory, config
):
    """The in-flight slot registered at launch is released (in a `finally`)
    once the run's thread body finishes — a subsequent run_destroy call for
    the same agent_id is then free to launch again rather than being
    permanently blocked by a stale registry entry."""
    agent_id = _seed_agent(session_factory, status=AgentStatus.READY, gojira_app_id=7)
    fake_gojira = FakeGojira()

    thread = run_destroy(session_factory, agent_id, _clients(gojira=fake_gojira), config)
    assert thread is not None
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False
    assert agent_id not in destroy_module._destroying_agent_ids

    # A second call for the same agent_id launches again (rather than being
    # silently skipped by a stale registry entry) — it just finds no agent
    # row left (already forgotten by the first run) and completes cleanly,
    # same as the existing "unknown agent id" tolerance.
    thread2 = run_destroy(session_factory, agent_id, _clients(gojira=fake_gojira), config)
    assert thread2 is not None
    thread2.join(timeout=JOIN_TIMEOUT)
    assert thread2.is_alive() is False
    assert agent_id not in destroy_module._destroying_agent_ids


# --- run_destroy: resume-matrix gaps (review Finding 2) ------------------------


def test_run_destroy_resume_skips_done_platform_and_slack_and_retries_failed_repo(
    session_factory, config
):
    """Mirrors test_run_destroy_resume_skips_done_platform_and_retries_failed_
    slack's exact idiom, but the failure is at destroy-repo instead of
    destroy-slack: platform and slack both succeed on the first run, repo
    fails; a second run skips platform AND slack entirely (their fakes see
    no second call across both runs) and retries only repo."""
    agent_id = _seed_agent(
        session_factory,
        status=AgentStatus.READY,
        gojira_app_id=7,
        slack_app_id="A0FAKE",
        repo_full_name=REPO,
    )
    fake_gojira = FakeGojira()
    fake_slack = FakeSlack()
    fake_cloud = FakeCloud(project=config.gcp_project)
    failing_github = FakeGitHub(existing_repos={REPO}, rename_error="repo service outage")

    thread = run_destroy(
        session_factory,
        agent_id,
        _clients(github=failing_github, gojira=fake_gojira, slack=fake_slack, cloud=fake_cloud),
        config,
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False
    assert fake_gojira.delete_calls == [(7, True)]
    assert fake_slack.delete_calls == ["A0FAKE"]

    session = session_factory()
    agent = session.get(Agent, agent_id)
    assert agent.status == AgentStatus.DESTROY_FAILED
    rows = {s.name: s.status for s in agent.steps}
    assert rows["destroy-platform"] == StepStatus.DONE
    assert rows["destroy-access"] == StepStatus.DONE
    assert rows["destroy-slack"] == StepStatus.DONE
    assert rows["destroy-repo"] == StepStatus.FAILED
    session.close()

    working_github = FakeGitHub(existing_repos={REPO})
    thread2 = run_destroy(
        session_factory,
        agent_id,
        _clients(github=working_github, gojira=fake_gojira, slack=fake_slack, cloud=fake_cloud),
        config,
    )
    thread2.join(timeout=JOIN_TIMEOUT)
    assert thread2.is_alive() is False

    # Platform, access, and slack were never retried — still exactly one
    # call each across BOTH runs — their rows were already DONE.
    assert fake_gojira.delete_calls == [(7, True)]
    email = f"agent-{AGENT_NAME}@{config.gcp_project}.iam.gserviceaccount.com"
    assert fake_cloud.calls == [
        f"remove_from_group:{email}:{config.model_group}",
        f"delete_gsa:{email}",
    ]
    assert fake_slack.delete_calls == ["A0FAKE"]
    assert working_github.rename_calls == [(REPO, f"archived-agent-{AGENT_NAME}-{agent_id}")]

    session = session_factory()
    assert session.get(Agent, agent_id) is None
    assert session.query(AgentStep).filter_by(agent_id=agent_id).count() == 0
    session.close()


def test_run_destroy_resume_retries_failed_platform_and_continues_to_forget(
    session_factory, config
):
    """Mirrors the brief's resume scenario at the OTHER end of the pipeline:
    a failure at the very FIRST step (destroy-platform) aborts slack/repo/
    forget on the first run; a second run with a working platform client
    retries platform and, on success, continues through every remaining
    step all the way to a fully forgotten agent (row gone)."""
    agent_id = _seed_agent(
        session_factory,
        status=AgentStatus.READY,
        gojira_app_id=7,
        slack_app_id="A0FAKE",
        repo_full_name=REPO,
    )
    failing_gojira = FakeGojira(delete_error="platform outage")
    fake_github = FakeGitHub(existing_repos={REPO})
    fake_slack = FakeSlack()
    fake_cloud = FakeCloud(project=config.gcp_project)

    thread = run_destroy(
        session_factory,
        agent_id,
        _clients(github=fake_github, gojira=failing_gojira, slack=fake_slack, cloud=fake_cloud),
        config,
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False
    assert failing_gojira.delete_calls == [(7, True)]
    # Aborted — access/slack/repo's own side effects never fired.
    assert fake_cloud.calls == []
    assert fake_slack.delete_calls == []
    assert fake_github.rename_calls == []

    session = session_factory()
    agent = session.get(Agent, agent_id)
    assert agent is not None
    assert agent.status == AgentStatus.DESTROY_FAILED
    rows = {s.name: s.status for s in agent.steps}
    assert rows["destroy-platform"] == StepStatus.FAILED
    assert rows["destroy-access"] == StepStatus.FAILED
    assert rows["destroy-slack"] == StepStatus.FAILED
    assert rows["destroy-repo"] == StepStatus.FAILED
    assert rows["destroy-forget"] == StepStatus.FAILED
    session.close()

    working_gojira = FakeGojira()
    thread2 = run_destroy(
        session_factory,
        agent_id,
        _clients(github=fake_github, gojira=working_gojira, slack=fake_slack, cloud=fake_cloud),
        config,
    )
    thread2.join(timeout=JOIN_TIMEOUT)
    assert thread2.is_alive() is False

    assert working_gojira.delete_calls == [(7, True)]
    # destroy-access is retried on resume (its row was FAILED, not DONE) and
    # succeeds this time against the same (still-unseeded) FakeCloud.
    email = f"agent-{AGENT_NAME}@{config.gcp_project}.iam.gserviceaccount.com"
    assert fake_cloud.calls == [
        f"remove_from_group:{email}:{config.model_group}",
        f"delete_gsa:{email}",
    ]
    assert fake_slack.delete_calls == ["A0FAKE"]
    assert fake_github.rename_calls == [(REPO, f"archived-agent-{AGENT_NAME}-{agent_id}")]

    session = session_factory()
    assert session.get(Agent, agent_id) is None
    assert session.query(AgentStep).filter_by(agent_id=agent_id).count() == 0
    session.close()


def test_run_destroy_access_failure_marks_step_and_resume_retries_access_skipping_platform(
    session_factory, config
):
    """CRITICAL resume case (destruction-completeness design): destroy-access
    fails on the first run, aborting slack/repo/forget; a second run must
    NOT re-run destroy-platform (its fake sees no second call across both
    runs) — only destroy-access is retried, and once it works the run
    completes all the way through to a fully forgotten agent."""
    agent_id = _seed_agent(
        session_factory,
        status=AgentStatus.READY,
        gojira_app_id=7,
        slack_app_id="A0FAKE",
        repo_full_name=REPO,
    )
    fake_gojira = FakeGojira()
    fake_github = FakeGitHub(existing_repos={REPO})
    fake_slack = FakeSlack()
    failing_cloud = FakeCloud(project=config.gcp_project, remove_group_error="group service outage")

    thread = run_destroy(
        session_factory,
        agent_id,
        _clients(github=fake_github, gojira=fake_gojira, slack=fake_slack, cloud=failing_cloud),
        config,
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False
    assert fake_gojira.delete_calls == [(7, True)]
    # Aborted — slack/repo's own side effects never fired.
    assert fake_slack.delete_calls == []
    assert fake_github.rename_calls == []

    session = session_factory()
    agent = session.get(Agent, agent_id)
    assert agent is not None
    assert agent.status == AgentStatus.DESTROY_FAILED
    assert agent.error == "group service outage"
    rows = {s.name: s.status for s in agent.steps}
    assert rows["destroy-platform"] == StepStatus.DONE
    assert rows["destroy-access"] == StepStatus.FAILED
    assert rows["destroy-slack"] == StepStatus.FAILED
    assert rows["destroy-repo"] == StepStatus.FAILED
    assert rows["destroy-forget"] == StepStatus.FAILED
    session.close()

    working_cloud = FakeCloud(project=config.gcp_project)
    thread2 = run_destroy(
        session_factory,
        agent_id,
        _clients(github=fake_github, gojira=fake_gojira, slack=fake_slack, cloud=working_cloud),
        config,
    )
    thread2.join(timeout=JOIN_TIMEOUT)
    assert thread2.is_alive() is False

    # Platform was never retried — still exactly one delete_app call across
    # both runs — its row was already DONE. Only destroy-access retries,
    # against the fresh working cloud client.
    assert fake_gojira.delete_calls == [(7, True)]
    email = f"agent-{AGENT_NAME}@{config.gcp_project}.iam.gserviceaccount.com"
    assert working_cloud.calls == [
        f"remove_from_group:{email}:{config.model_group}",
        f"delete_gsa:{email}",
    ]
    assert fake_slack.delete_calls == ["A0FAKE"]
    assert fake_github.rename_calls == [(REPO, f"archived-agent-{AGENT_NAME}-{agent_id}")]

    session = session_factory()
    assert session.get(Agent, agent_id) is None
    assert session.query(AgentStep).filter_by(agent_id=agent_id).count() == 0
    session.close()


# --- run_destroy: a failed launch releases its slot (fix round 2) -------------


def test_run_destroy_releases_the_registry_slot_when_the_thread_fails_to_launch(
    session_factory, config, monkeypatch
):
    """Before the fix, `run_destroy` added `agent_id` to `_destroying_agent_
    ids` BEFORE constructing/starting the `threading.Thread` — if `.start()`
    (or construction) raised, the exception propagated with the slot still
    registered (the thread's own `finally`-release in
    `_run_destroy_body_and_release` never ran, since the thread's body never
    ran). Every later `run_destroy` call for that agent_id would then see it
    already in the registry and return `None` forever — a permanent brick.

    Patches `threading.Thread` in the destroy module's own namespace (the
    real registry path, not a mock of `run_destroy` itself) so `.start()`
    raises like a real "can't start new thread" failure would."""
    agent_id = _seed_agent(session_factory, status=AgentStatus.READY, gojira_app_id=7)
    fake_gojira = FakeGojira()

    class _ThreadThatFailsToStart:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(destroy_module.threading, "Thread", _ThreadThatFailsToStart)

    with pytest.raises(RuntimeError, match="can't start new thread"):
        run_destroy(session_factory, agent_id, _clients(gojira=fake_gojira), config)

    # The slot must not remain claimed by a launch that never actually
    # started a thread — otherwise every later call for this agent_id is
    # silently swallowed (returns None) forever.
    assert agent_id not in destroy_module._destroying_agent_ids

    # Un-patch and confirm a subsequent call for the SAME agent_id actually
    # launches and completes for real.
    monkeypatch.undo()

    thread2 = run_destroy(session_factory, agent_id, _clients(gojira=fake_gojira), config)
    assert thread2 is not None
    thread2.join(timeout=JOIN_TIMEOUT)
    assert thread2.is_alive() is False
    assert agent_id not in destroy_module._destroying_agent_ids
    assert fake_gojira.delete_calls == [(7, True)]

    session = session_factory()
    assert session.get(Agent, agent_id) is None
    session.close()


# --- destroy-secrets ----------------------------------------------------------
#
# The step that closes the gap the secrets platform integration documented:
# secrets written through the platform used to stay behind when the agent
# was destroyed, which is a live credential at a path nobody is watching.


def _ladon_holding_everything(name: str = AGENT_NAME) -> FakeLadon:
    ladon = FakeLadon()
    for purpose in ("cabal", "slack", "env"):
        ladon.stored[f"apps/{name}/{purpose}"] = {"A_KEY": "not-a-real-value"}
    return ladon


def test_destroy_secrets_removes_all_three_paths_and_their_cluster_objects(
    session_factory, config
):
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL
    ladon = _ladon_holding_everything()

    detail = _destroy_secrets(_ctx(session, agent, config, ladon=ladon))

    assert [path for _, path in ladon.deletes] == [
        f"apps/{AGENT_NAME}/cabal",
        f"apps/{AGENT_NAME}/slack",
        f"apps/{AGENT_NAME}/env",
    ]
    assert [name for *_, name in ladon.unsyncs] == [
        f"{AGENT_NAME}-cabal",
        f"{AGENT_NAME}-slack",
        f"{AGENT_NAME}-env",
    ]
    assert ladon.stored == {}
    assert "removed" in detail
    session.close()


def test_destroy_secrets_takes_the_identity_down_after_the_three_paths(
    session_factory, config
):
    """The store, service account, role and policy the platform made for
    this agent go last: the platform refuses to remove an identity while
    an ExternalSecret still reads through it, so the three objects go
    first, and the identity once nothing is left that needs it."""
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL
    order: list[str] = []

    class _Ordered(FakeLadon):
        def unsync(self, acting_email, app, k8s_namespace, name):
            order.append(f"unsync:{name}")
            super().unsync(acting_email, app, k8s_namespace, name)

        def delete_secret(self, acting_email, path):
            order.append(f"delete:{path}")
            super().delete_secret(acting_email, path)

        def offboard(self, acting_email, app, k8s_namespace):
            order.append("offboard")
            super().offboard(acting_email, app, k8s_namespace)

    ladon = _Ordered()
    for purpose in ("cabal", "slack", "env"):
        ladon.stored[f"apps/{AGENT_NAME}/{purpose}"] = {"A_KEY": "not-a-real-value"}

    detail = _destroy_secrets(_ctx(session, agent, config, ladon=ladon))

    assert ladon.offboards == [(OWNER_EMAIL, AGENT_NAME, AGENT_NAME)]
    assert order[-1] == "offboard"
    assert all(step != "offboard" for step in order[:-1])
    assert detail == "removed: cabal, slack, env, identity"
    session.close()


def test_destroy_secrets_leaves_the_identity_when_a_path_could_not_go(
    session_factory, config
):
    """An identity taken down while an ExternalSecret it serves is still
    there is exactly the dangling object the platform refuses; the room
    does not ask for it, and the raised message says how far it got."""
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL
    ladon = FakeLadon(unsync_error="the platform refused")

    with pytest.raises(RuntimeError, match="could not remove cabal, slack, env"):
        _destroy_secrets(_ctx(session, agent, config, ladon=ladon))

    assert ladon.offboards == []
    session.close()


def test_destroy_secrets_that_cannot_take_the_identity_down_fails_the_step(
    session_factory, config
):
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL
    ladon = _ladon_holding_everything()
    ladon._offboard_error = "the platform refused"

    with pytest.raises(RuntimeError) as excinfo:
        _destroy_secrets(_ctx(session, agent, config, ladon=ladon))

    message = str(excinfo.value)
    assert "could not remove identity" in message
    assert "removed: cabal, slack, env" in message
    assert ladon.stored == {}
    session.close()


def test_destroy_secrets_takes_the_cluster_object_away_before_the_path(
    session_factory, config
):
    # The other way round leaves the cluster reconciling a path that is
    # gone on purpose, and the Kubernetes Secret it owns still sitting in
    # the namespace.
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL

    order: list[str] = []

    class _Ordered(FakeLadon):
        def unsync(self, acting_email, app, k8s_namespace, name):
            order.append(f"unsync:{name}")
            super().unsync(acting_email, app, k8s_namespace, name)

        def delete_secret(self, acting_email, path):
            order.append(f"delete:{path}")
            super().delete_secret(acting_email, path)

    _destroy_secrets(_ctx(session, agent, config, ladon=_Ordered()))

    assert order[0] == f"unsync:{AGENT_NAME}-cabal"
    assert order[1] == f"delete:apps/{AGENT_NAME}/cabal"
    session.close()


def test_destroy_secrets_records_the_removal_against_the_agents_creator(
    session_factory, config
):
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = f"  {OWNER_EMAIL} "
    ladon = _ladon_holding_everything()

    _destroy_secrets(_ctx(session, agent, config, ladon=ladon))

    assert {email for email, _ in ladon.deletes} == {OWNER_EMAIL}
    assert {call[0] for call in ladon.unsyncs} == {OWNER_EMAIL}
    session.close()


def test_destroy_secrets_run_twice_is_the_same_answer(session_factory, config):
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL
    ladon = _ladon_holding_everything()

    first = _destroy_secrets(_ctx(session, agent, config, ladon=ladon))
    second = _destroy_secrets(_ctx(session, agent, config, ladon=ladon))

    assert first == second
    # The second run really did run: the lane's already-gone answer is a
    # success, so every call is made again and the step reports the same
    # three purposes rather than short-circuiting on state it kept.
    assert [path for _, path in ladon.deletes] == [
        f"apps/{AGENT_NAME}/cabal",
        f"apps/{AGENT_NAME}/slack",
        f"apps/{AGENT_NAME}/env",
    ] * 2
    assert len(ladon.unsyncs) == 6
    assert ladon.stored == {}
    session.close()


def test_destroy_secrets_skips_a_room_with_no_secrets_platform(session_factory, config):
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL

    detail = _destroy_secrets(_ctx(session, agent, config, ladon=None))

    assert "no secrets platform" in detail
    session.close()


def test_destroy_secrets_leaves_the_paths_when_nobody_is_on_record(
    session_factory, config
):
    # The platform records every removal against a person. Deleting
    # credentials under nobody's name is worse than leaving them for an
    # operator, and this is a note rather than a failure so an otherwise
    # clean destroy is not held hostage to it.
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = None
    ladon = _ladon_holding_everything()

    detail = _destroy_secrets(_ctx(session, agent, config, ladon=ladon))

    assert "no person on record" in detail
    assert ladon.deletes == []
    assert ladon.unsyncs == []
    session.close()


def test_destroy_secrets_note_never_carries_an_em_dash(session_factory, config):
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = None

    assert "—" not in _destroy_secrets(_ctx(session, agent, config, ladon=None))
    assert "—" not in _destroy_secrets(
        _ctx(session, agent, config, ladon=FakeLadon())
    )
    session.close()


def test_destroy_secrets_that_the_lane_refuses_fails_the_step(session_factory, config):
    # A refusal is not "already gone" (the lane answers 200 for that), so
    # it fails the ledger at this step rather than marking a credential
    # removed that is still live. The run resumes here.
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL
    ladon = FakeLadon(delete_error="that namespace is not yours")

    with pytest.raises(RuntimeError) as exc:
        _destroy_secrets(_ctx(session, agent, config, ladon=ladon))

    assert "could not remove cabal, slack, env" in str(exc.value)
    session.close()


def test_a_full_destroy_run_removes_the_stored_secrets_too(session_factory, config):
    ladon = _ladon_holding_everything()
    agent_id = _seed_agent(
        session_factory,
        status=AgentStatus.READY,
        gojira_app_id=None,
        repo_full_name=None,
        created_by=OWNER_EMAIL,
    )

    thread = run_destroy(session_factory, agent_id, _clients(ladon=ladon), config)
    assert thread is not None
    thread.join(timeout=JOIN_TIMEOUT)

    assert ladon.stored == {}
    assert len(ladon.unsyncs) == 3


def test_destroy_secrets_records_the_removal_against_the_operator_who_asked(
    session_factory, config
):
    # The platform records every removal against a person, and the person
    # who pressed destroy is that person. `created_by` may have left the
    # company; the operator doing this is here.
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL
    ladon = _ladon_holding_everything()

    _destroy_secrets(
        _ctx(session, agent, config, ladon=ladon, acting_email="ops@example.com")
    )

    assert {email for email, _ in ladon.deletes} == {"ops@example.com"}
    assert {call[0] for call in ladon.unsyncs} == {"ops@example.com"}
    session.close()


def test_destroy_secrets_falls_back_to_the_creator_when_no_caller_is_named(
    session_factory, config
):
    # A resume has no request behind it: the ledger retried itself, so
    # there is no verified caller to name and the agent's own creator is
    # the honest fallback.
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL
    ladon = _ladon_holding_everything()

    _destroy_secrets(_ctx(session, agent, config, ladon=ladon, acting_email=None))

    assert {email for email, _ in ladon.deletes} == {OWNER_EMAIL}
    session.close()


def test_destroy_secrets_attempts_all_three_even_when_one_refuses(
    session_factory, config
):
    # A lane that refuses one path is no reason to leave the other two
    # live. The step still fails, so the ledger can be resumed, and its
    # message says how far the run got.
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL

    class _RefusesSlack(FakeLadon):
        def delete_secret(self, acting_email, path):
            if path.endswith("/slack"):
                raise ClientError("that namespace is not yours")
            super().delete_secret(acting_email, path)

    ladon = _RefusesSlack()
    for purpose in ("cabal", "slack", "env"):
        ladon.stored[f"apps/{AGENT_NAME}/{purpose}"] = {"A_KEY": "not-a-real-value"}

    with pytest.raises(RuntimeError) as exc:
        _destroy_secrets(_ctx(session, agent, config, ladon=ladon))

    assert "slack" in str(exc.value)
    assert "cabal" in str(exc.value)
    assert "env" in str(exc.value)
    # The two that could go, went.
    assert f"apps/{AGENT_NAME}/cabal" not in ladon.stored
    assert f"apps/{AGENT_NAME}/env" not in ladon.stored
    session.close()


def test_destroy_secrets_does_not_wedge_on_a_namespace_that_is_already_gone(
    session_factory, config
):
    # The ledger removes secrets before it deletes the namespace, so this
    # should not arise -- but a resumed run, or a namespace an operator
    # removed by hand, must not stop a teardown. The lane answers a gone
    # namespace with "there is nothing here", which is a success.
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL
    ladon = FakeLadon(missing_k8s_namespaces={AGENT_NAME})
    for purpose in ("cabal", "slack", "env"):
        ladon.stored[f"apps/{AGENT_NAME}/{purpose}"] = {"A_KEY": "not-a-real-value"}

    detail = _destroy_secrets(_ctx(session, agent, config, ladon=ladon))

    assert "removed" in detail
    assert ladon.unsyncs == []
    assert len(ladon.absent_unsyncs) == 3
    # The KV paths still go: the namespace being gone says nothing about
    # what the platform is holding.
    assert ladon.stored == {}
    session.close()


def test_the_secrets_step_runs_before_the_platform_takes_the_namespace_away(
    session_factory, config
):
    # The order is the whole point: removing an ExternalSecret is a write
    # into the agent's namespace, and destroy-platform is what deletes
    # that namespace.
    assert DESTROY_STEPS.index("destroy-secrets") < DESTROY_STEPS.index(
        "destroy-platform"
    )


def test_a_full_destroy_run_names_the_operator_who_asked(session_factory, config):
    ladon = _ladon_holding_everything()
    agent_id = _seed_agent(
        session_factory,
        status=AgentStatus.READY,
        gojira_app_id=None,
        repo_full_name=None,
        created_by=OWNER_EMAIL,
    )

    thread = run_destroy(
        session_factory,
        agent_id,
        _clients(ladon=ladon),
        config,
        acting_email="ops@example.com",
    )
    assert thread is not None
    thread.join(timeout=JOIN_TIMEOUT)

    assert {email for email, _ in ladon.deletes} == {"ops@example.com"}


def test_destroy_secrets_counts_an_unexpected_failure_and_keeps_going(
    session_factory, config
):
    # Not only ClientError: a connection reset arriving as something else
    # would otherwise abort the two purposes after it, which is the exact
    # failure this collection exists to prevent.
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL

    class _SurprisesOnCabal(FakeLadon):
        def unsync(self, acting_email, app, k8s_namespace, name):
            if name.endswith("-cabal"):
                raise RuntimeError("something nobody wrapped")
            super().unsync(acting_email, app, k8s_namespace, name)

    ladon = _SurprisesOnCabal()
    for purpose in ("cabal", "slack", "env"):
        ladon.stored[f"apps/{AGENT_NAME}/{purpose}"] = {"A_KEY": "not-a-real-value"}

    with pytest.raises(RuntimeError) as exc:
        _destroy_secrets(_ctx(session, agent, config, ladon=ladon))

    assert "cabal" in str(exc.value)
    assert "slack, env" in str(exc.value)
    assert f"apps/{AGENT_NAME}/slack" not in ladon.stored
    assert f"apps/{AGENT_NAME}/env" not in ladon.stored
    session.close()


def test_an_object_recreated_under_the_same_name_fails_only_that_purpose(
    session_factory, config
):
    # `recreated` means what the lane deleted is gone and something else
    # now stands at the name. That is a failure for this purpose, because
    # a live ExternalSecret nobody here knows about was left behind -- and
    # it is not a reason to stop removing the other two.
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL

    class _Recreates(FakeLadon):
        def unsync(self, acting_email, app, k8s_namespace, name):
            if name.endswith("-slack"):
                raise ClientError(f"something else now stands at {name}")
            super().unsync(acting_email, app, k8s_namespace, name)

    ladon = _Recreates()
    for purpose in ("cabal", "slack", "env"):
        ladon.stored[f"apps/{AGENT_NAME}/{purpose}"] = {"A_KEY": "not-a-real-value"}

    with pytest.raises(RuntimeError) as exc:
        _destroy_secrets(_ctx(session, agent, config, ladon=ladon))

    assert "could not remove slack" in str(exc.value)
    assert "removed: cabal, env" in str(exc.value)
    session.close()


def test_destroy_secrets_never_logs_what_the_lane_said(session_factory, config, caplog):
    # A step's error string lands on AgentStep.error, which any viewer of
    # the agent can read, and the platform's own sentence can quote the
    # request it refused.
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL
    ladon = FakeLadon(delete_error="refused the body not-a-real-value")

    with caplog.at_level("WARNING"), pytest.raises(RuntimeError):
        _destroy_secrets(_ctx(session, agent, config, ladon=ladon))

    assert "not-a-real-value" not in caplog.text
    assert "refused the body" not in caplog.text
    assert "ClientError" in caplog.text
    session.close()

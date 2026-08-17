"""Tests for kobo_api.destroy: the four-step teardown ledger, the mirror of
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
    _destroy_forget,
    _destroy_platform,
    _destroy_repo,
    _destroy_slack,
    _is_already_archived,
    _is_repo_not_found,
    run_destroy,
)
from kobo_api.models import Agent, AgentStatus, AgentStep, StepStatus
from tests.fakes import FakeGitHub, FakeGojira, FakeSlack

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
MODEL_ID = "vertex_ai/claude-sonnet-4-5"
OWNER = "acme-corp"
REPO = f"{OWNER}/agent-{AGENT_NAME}"

JOIN_TIMEOUT = 5


# --- shared helpers -----------------------------------------------------------


def _make_ctx_agent(
    session_factory,
    *,
    name: str = AGENT_NAME,
    gojira_app_id: int | None = None,
    slack_app_id: str | None = None,
    repo_full_name: str | None = None,
    status=AgentStatus.READY,
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
        repo_full_name=repo_full_name,
    )
    session.add(agent)
    session.commit()
    return session, agent


def _ctx(session, agent, config, *, github=None, gojira=None, slack=None) -> DestroyCtx:
    clients = Clients(
        github=github, gojira=gojira, cloud=None, agent=None, kubectl=None, slack=slack
    )
    return DestroyCtx(agent=agent, session=session, config=config, clients=clients)


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


def _clients(*, github=None, gojira=None, slack=None) -> Clients:
    return Clients(
        github=github, gojira=gojira, cloud=None, agent=None, kubectl=None, slack=slack
    )


# --- DESTROY_STEPS shape --------------------------------------------------------


def test_destroy_steps_order_is_fixed():
    assert DESTROY_STEPS == ("destroy-platform", "destroy-slack", "destroy-repo", "destroy-forget")


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


# --- _destroy_repo -----------------------------------------------------------------


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

    thread = run_destroy(
        session_factory, agent_id, _clients(github=fake_github, gojira=fake_gojira, slack=fake_slack), config
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    assert fake_gojira.delete_calls == [(7, True)]
    assert fake_slack.delete_calls == ["A0FAKE"]
    assert fake_github.rename_calls == [(REPO, f"archived-agent-{AGENT_NAME}-{agent_id}")]

    session = session_factory()
    assert session.get(Agent, agent_id) is None
    assert session.query(AgentStep).filter_by(agent_id=agent_id).count() == 0
    session.close()


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
    failing_slack = FakeSlack(delete_error="slack outage")

    thread = run_destroy(
        session_factory,
        agent_id,
        _clients(github=fake_github, gojira=fake_gojira, slack=failing_slack),
        config,
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False
    assert fake_gojira.delete_calls == [(7, True)]

    session = session_factory()
    agent = session.get(Agent, agent_id)
    assert agent.status == AgentStatus.DESTROY_FAILED
    session.close()

    working_slack = FakeSlack()
    thread2 = run_destroy(
        session_factory,
        agent_id,
        _clients(github=fake_github, gojira=fake_gojira, slack=working_slack),
        config,
    )
    thread2.join(timeout=JOIN_TIMEOUT)
    assert thread2.is_alive() is False

    # Platform was never retried (still exactly one delete_app call across
    # both runs) — its row was already DONE.
    assert fake_gojira.delete_calls == [(7, True)]
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
    assert rows["destroy-slack"] == StepStatus.FAILED
    assert rows["destroy-repo"] == StepStatus.FAILED
    assert rows["destroy-forget"] == StepStatus.FAILED
    session.close()


def test_run_destroy_already_gone_matrix_for_a_never_fully_provisioned_agent(
    session_factory, config
):
    """An agent that FAILED early in provisioning (no platform app, no
    slack app, no repo at all) is still destroyable: every step is
    trivially done, and the row disappears."""
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

    thread = run_destroy(
        session_factory,
        agent_id,
        _clients(github=fake_github, gojira=fake_gojira, slack=fake_slack),
        config,
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    assert fake_gojira.delete_calls == []
    assert fake_slack.delete_calls == []
    assert fake_github.rename_calls == []

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
    failing_github = FakeGitHub(existing_repos={REPO}, rename_error="repo service outage")

    thread = run_destroy(
        session_factory,
        agent_id,
        _clients(github=failing_github, gojira=fake_gojira, slack=fake_slack),
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
    assert rows["destroy-slack"] == StepStatus.DONE
    assert rows["destroy-repo"] == StepStatus.FAILED
    session.close()

    working_github = FakeGitHub(existing_repos={REPO})
    thread2 = run_destroy(
        session_factory,
        agent_id,
        _clients(github=working_github, gojira=fake_gojira, slack=fake_slack),
        config,
    )
    thread2.join(timeout=JOIN_TIMEOUT)
    assert thread2.is_alive() is False

    # Platform and slack were never retried — still exactly one call each
    # across BOTH runs — their rows were already DONE.
    assert fake_gojira.delete_calls == [(7, True)]
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

    thread = run_destroy(
        session_factory,
        agent_id,
        _clients(github=fake_github, gojira=failing_gojira, slack=fake_slack),
        config,
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False
    assert failing_gojira.delete_calls == [(7, True)]
    # Aborted — slack/repo's own side effects never fired.
    assert fake_slack.delete_calls == []
    assert fake_github.rename_calls == []

    session = session_factory()
    agent = session.get(Agent, agent_id)
    assert agent is not None
    assert agent.status == AgentStatus.DESTROY_FAILED
    rows = {s.name: s.status for s in agent.steps}
    assert rows["destroy-platform"] == StepStatus.FAILED
    assert rows["destroy-slack"] == StepStatus.FAILED
    assert rows["destroy-repo"] == StepStatus.FAILED
    assert rows["destroy-forget"] == StepStatus.FAILED
    session.close()

    working_gojira = FakeGojira()
    thread2 = run_destroy(
        session_factory,
        agent_id,
        _clients(github=fake_github, gojira=working_gojira, slack=fake_slack),
        config,
    )
    thread2.join(timeout=JOIN_TIMEOUT)
    assert thread2.is_alive() is False

    assert working_gojira.delete_calls == [(7, True)]
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

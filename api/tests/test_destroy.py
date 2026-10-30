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

import importlib.util
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api import destroy as destroy_module
from kobo_api.agent_env import ENV_BRANCH
from kobo_api.clients import ClientError, Clients, GhCLI
from kobo_api.config import Config
from kobo_api.destroy import (
    DESTROY_STEPS,
    DIRECTORY_RETIRE_MISSED_ERROR,
    DestroyCtx,
    _destroy_access,
    _destroy_directory,
    _destroy_forget,
    _destroy_platform,
    _destroy_repo,
    _destroy_secrets,
    _destroy_slack,
    run_destroy,
)
from kobo_api.directory import CLUSTER_SILENT_NOTE, sweep_directory
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER
from kobo_api.kinds import KIND_COMPOSED, KIND_CONNECTED, KIND_DIRECTORY, RUN_MODE_HTTP
from kobo_api.main import create_app
from kobo_api.mcp import rate_limit
from kobo_api.models import (
    A2AInvite,
    Agent,
    AgentStatus,
    AgentStep,
    AgentUserGrant,
    Base,
    RepoAccess,
    StepStatus,
    Thread,
    ThreadParticipant,
)
from kobo_api.personas import seed_default_personas
from kobo_api.sero import HttpxSeroClient
from tests.fakes import (
    FakeAgent,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeImage,
    FakeKubectl,
    FakeSero,
    FakeSlack,
    FakeStorage,
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
    sero=None,
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
        sero=sero,
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
    *, github=None, gojira=None, slack=None, cloud=None, cabal=None, sero=None
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
        sero=sero,
    )


# --- DESTROY_STEPS shape --------------------------------------------------------


# --- _is_repo_not_found / _is_already_archived (pure helpers) ------------------


# --- _destroy_platform ----------------------------------------------------------


# --- _destroy_access --------------------------------------------------------------


# --- _destroy_slack ---------------------------------------------------------------


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


def test_destroy_directory_fails_when_the_switchboard_keeps_the_entry(
    session_factory, config, monkeypatch
):
    """Destruction safety (issue #291): a retire that did not land fails
    the step with the fixed sentence, never the switchboard's own text."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    session, agent = _make_ctx_agent(session_factory)
    ctx = _ctx(session, agent, cfg, cabal=FakeCabal(absorb_error="switchboard unreachable"))

    with pytest.raises(RuntimeError, match=DIRECTORY_RETIRE_MISSED_ERROR):
        _destroy_directory(ctx)
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


def test_destroy_forget_deletes_every_row_keyed_by_the_agents_name(session_factory, config):
    """Data integrity (issue #291): a visibility grant and a thread seat
    left behind would attach themselves to the next agent to take this
    name."""
    session, agent = _make_ctx_agent(session_factory)
    thread = Thread(initiator_email=OWNER_EMAIL)
    session.add(thread)
    session.flush()
    session.add(ThreadParticipant(thread_id=thread.id, agent_name=agent.name))
    session.add(AgentUserGrant(agent_name=agent.name, email=OWNER_EMAIL))
    session.commit()

    _destroy_forget(_ctx(session, agent, config))

    assert session.query(ThreadParticipant).count() == 0
    assert session.query(AgentUserGrant).count() == 0
    session.close()


def test_destroy_forget_names_what_the_switchboard_did_not_take(
    session_factory, config, monkeypatch
):
    """Destruction safety (issue #291): a dispatch-pairs push that fails
    after the row is gone is written into the step's note, not swallowed."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    session, agent = _make_ctx_agent(session_factory)
    ctx = _ctx(session, agent, cfg, cabal=FakeCabal(put_dispatch_pairs_error="down"))

    detail = _destroy_forget(ctx)

    assert detail.startswith("forgotten; still to catch up: ")
    assert "allowed pairs" in detail
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


def test_run_destroy_lands_in_destroy_failed_when_the_switchboard_keeps_the_entry_and_retries(
    session_factory, config, monkeypatch
):
    """Destruction safety (issue #291): the row survives with the directory
    step failed and the sentence on the agent; Destroy again finishes it
    once the switchboard answers."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    agent_id = _seed_agent(session_factory, status=AgentStatus.READY)

    thread = run_destroy(
        session_factory, agent_id, _clients(cabal=FakeCabal(absorb_error="refused")), cfg
    )
    thread.join(timeout=JOIN_TIMEOUT)

    session = session_factory()
    agent = session.get(Agent, agent_id)
    assert agent.status == AgentStatus.DESTROY_FAILED
    assert agent.error == DIRECTORY_RETIRE_MISSED_ERROR
    rows = {s.name: s for s in agent.steps}
    assert rows["destroy-directory"].status == StepStatus.FAILED
    assert rows["destroy-forget"].error == "aborted: destroy-directory"
    session.close()

    thread2 = run_destroy(session_factory, agent_id, _clients(cabal=FakeCabal()), cfg)
    thread2.join(timeout=JOIN_TIMEOUT)

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


def _sero_holding_everything(name: str = AGENT_NAME) -> FakeSero:
    sero = FakeSero()
    for purpose in ("cabal", "slack", "env"):
        sero.stored[f"apps/{name}/{purpose}"] = {"A_KEY": "not-a-real-value"}
    return sero


def test_destroy_secrets_removes_all_three_paths_and_their_cluster_objects(
    session_factory, config
):
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL
    sero = _sero_holding_everything()

    detail = _destroy_secrets(_ctx(session, agent, config, sero=sero))

    assert [path for _, path in sero.deletes] == [
        f"apps/{AGENT_NAME}/cabal",
        f"apps/{AGENT_NAME}/slack",
        f"apps/{AGENT_NAME}/env",
    ]
    assert [name for *_, name in sero.unsyncs] == [
        f"{AGENT_NAME}-cabal",
        f"{AGENT_NAME}-slack",
        f"{AGENT_NAME}-env",
    ]
    assert sero.stored == {}
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

    class _Ordered(FakeSero):
        def unsync(self, acting_email, app, k8s_namespace, name):
            order.append(f"unsync:{name}")
            super().unsync(acting_email, app, k8s_namespace, name)

        def delete_secret(self, acting_email, path):
            order.append(f"delete:{path}")
            super().delete_secret(acting_email, path)

        def offboard(self, acting_email, app, k8s_namespace):
            order.append("offboard")
            super().offboard(acting_email, app, k8s_namespace)

    sero = _Ordered()
    for purpose in ("cabal", "slack", "env"):
        sero.stored[f"apps/{AGENT_NAME}/{purpose}"] = {"A_KEY": "not-a-real-value"}

    detail = _destroy_secrets(_ctx(session, agent, config, sero=sero))

    assert sero.offboards == [(OWNER_EMAIL, AGENT_NAME, AGENT_NAME)]
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
    sero = FakeSero(unsync_error="the platform refused")

    with pytest.raises(RuntimeError, match="could not remove cabal, slack, env"):
        _destroy_secrets(_ctx(session, agent, config, sero=sero))

    assert sero.offboards == []
    session.close()


def test_destroy_secrets_that_cannot_take_the_identity_down_fails_the_step(
    session_factory, config
):
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL
    sero = _sero_holding_everything()
    sero._offboard_error = "the platform refused"

    with pytest.raises(RuntimeError) as excinfo:
        _destroy_secrets(_ctx(session, agent, config, sero=sero))

    message = str(excinfo.value)
    assert "could not remove identity" in message
    assert "removed: cabal, slack, env" in message
    assert sero.stored == {}
    session.close()


def test_destroy_secrets_revokes_the_agents_colleague_passes(
    session_factory, config, monkeypatch
):
    """The colleague pass is the one credential a destroy could not take
    back (destroy-orphans audit #1, 2026-09-21): a destroyed agent's token
    still cleared the switchboard. The switchboard now refuses every token
    the subject was issued before now, and this is where the room asks."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL
    cabal = FakeCabal()

    detail = _destroy_secrets(_ctx(session, agent, cfg, sero=_sero_holding_everything(), cabal=cabal))

    assert cabal.revoke_colleague_calls == [AGENT_NAME]
    assert detail == "removed: cabal, slack, env, identity, passes"
    session.close()


def test_destroy_secrets_revokes_the_passes_even_with_no_secrets_platform(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    session, agent = _make_ctx_agent(session_factory)
    cabal = FakeCabal()

    detail = _destroy_secrets(_ctx(session, agent, cfg, cabal=cabal))

    assert cabal.revoke_colleague_calls == [AGENT_NAME]
    assert "passes" in detail
    session.close()


def test_destroy_secrets_that_cannot_revoke_the_passes_fails_the_step(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL
    cabal = FakeCabal(revoke_colleague_error="switchboard down")

    with pytest.raises(RuntimeError, match="could not remove passes"):
        _destroy_secrets(_ctx(session, agent, cfg, sero=_sero_holding_everything(), cabal=cabal))
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

    class _Ordered(FakeSero):
        def unsync(self, acting_email, app, k8s_namespace, name):
            order.append(f"unsync:{name}")
            super().unsync(acting_email, app, k8s_namespace, name)

        def delete_secret(self, acting_email, path):
            order.append(f"delete:{path}")
            super().delete_secret(acting_email, path)

    _destroy_secrets(_ctx(session, agent, config, sero=_Ordered()))

    assert order[0] == f"unsync:{AGENT_NAME}-cabal"
    assert order[1] == f"delete:apps/{AGENT_NAME}/cabal"
    session.close()


def test_destroy_secrets_run_twice_is_the_same_answer(session_factory, config):
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL
    sero = _sero_holding_everything()

    first = _destroy_secrets(_ctx(session, agent, config, sero=sero))
    second = _destroy_secrets(_ctx(session, agent, config, sero=sero))

    assert first == second
    # The second run really did run: the lane's already-gone answer is a
    # success, so every call is made again and the step reports the same
    # three purposes rather than short-circuiting on state it kept.
    assert [path for _, path in sero.deletes] == [
        f"apps/{AGENT_NAME}/cabal",
        f"apps/{AGENT_NAME}/slack",
        f"apps/{AGENT_NAME}/env",
    ] * 2
    assert len(sero.unsyncs) == 6
    assert sero.stored == {}
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
    sero = _sero_holding_everything()

    detail = _destroy_secrets(_ctx(session, agent, config, sero=sero))

    assert "no person on record" in detail
    assert sero.deletes == []
    assert sero.unsyncs == []
    session.close()


def test_destroy_secrets_note_never_carries_an_em_dash(session_factory, config):
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = None

    assert "—" not in _destroy_secrets(_ctx(session, agent, config, sero=None))
    assert "—" not in _destroy_secrets(
        _ctx(session, agent, config, sero=FakeSero())
    )
    session.close()


def test_destroy_secrets_that_the_lane_refuses_fails_the_step(session_factory, config):
    # A refusal is not "already gone" (the lane answers 200 for that), so
    # it fails the ledger at this step rather than marking a credential
    # removed that is still live. The run resumes here.
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL
    sero = FakeSero(delete_error="that namespace is not yours")

    with pytest.raises(RuntimeError) as exc:
        _destroy_secrets(_ctx(session, agent, config, sero=sero))

    assert "could not remove cabal, slack, env" in str(exc.value)
    session.close()


def test_a_full_destroy_run_removes_the_stored_secrets_too(session_factory, config):
    sero = _sero_holding_everything()
    agent_id = _seed_agent(
        session_factory,
        status=AgentStatus.READY,
        gojira_app_id=None,
        repo_full_name=None,
        created_by=OWNER_EMAIL,
    )

    thread = run_destroy(session_factory, agent_id, _clients(sero=sero), config)
    assert thread is not None
    thread.join(timeout=JOIN_TIMEOUT)

    assert sero.stored == {}
    assert len(sero.unsyncs) == 3


def test_destroy_secrets_records_the_removal_against_the_operator_who_asked(
    session_factory, config
):
    # The platform records every removal against a person, and the person
    # who pressed destroy is that person. `created_by` may have left the
    # company; the operator doing this is here.
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL
    sero = _sero_holding_everything()

    _destroy_secrets(
        _ctx(session, agent, config, sero=sero, acting_email="ops@example.com")
    )

    assert {email for email, _ in sero.deletes} == {"ops@example.com"}
    assert {call[0] for call in sero.unsyncs} == {"ops@example.com"}
    session.close()


def test_destroy_secrets_attempts_all_three_even_when_one_refuses(
    session_factory, config
):
    # A lane that refuses one path is no reason to leave the other two
    # live. The step still fails, so the ledger can be resumed, and its
    # message says how far the run got.
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL

    class _RefusesSlack(FakeSero):
        def delete_secret(self, acting_email, path):
            if path.endswith("/slack"):
                raise ClientError("that namespace is not yours")
            super().delete_secret(acting_email, path)

    sero = _RefusesSlack()
    for purpose in ("cabal", "slack", "env"):
        sero.stored[f"apps/{AGENT_NAME}/{purpose}"] = {"A_KEY": "not-a-real-value"}

    with pytest.raises(RuntimeError) as exc:
        _destroy_secrets(_ctx(session, agent, config, sero=sero))

    assert "slack" in str(exc.value)
    assert "cabal" in str(exc.value)
    assert "env" in str(exc.value)
    # The two that could go, went.
    assert f"apps/{AGENT_NAME}/cabal" not in sero.stored
    assert f"apps/{AGENT_NAME}/env" not in sero.stored
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
    sero = FakeSero(missing_k8s_namespaces={AGENT_NAME})
    for purpose in ("cabal", "slack", "env"):
        sero.stored[f"apps/{AGENT_NAME}/{purpose}"] = {"A_KEY": "not-a-real-value"}

    detail = _destroy_secrets(_ctx(session, agent, config, sero=sero))

    assert "removed" in detail
    assert sero.unsyncs == []
    assert len(sero.absent_unsyncs) == 3
    # The KV paths still go: the namespace being gone says nothing about
    # what the platform is holding.
    assert sero.stored == {}
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


def test_an_object_recreated_under_the_same_name_fails_only_that_purpose(
    session_factory, config
):
    # `recreated` means what the lane deleted is gone and something else
    # now stands at the name. That is a failure for this purpose, because
    # a live ExternalSecret nobody here knows about was left behind -- and
    # it is not a reason to stop removing the other two.
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL

    class _Recreates(FakeSero):
        def unsync(self, acting_email, app, k8s_namespace, name):
            if name.endswith("-slack"):
                raise ClientError(f"something else now stands at {name}")
            super().unsync(acting_email, app, k8s_namespace, name)

    sero = _Recreates()
    for purpose in ("cabal", "slack", "env"):
        sero.stored[f"apps/{AGENT_NAME}/{purpose}"] = {"A_KEY": "not-a-real-value"}

    with pytest.raises(RuntimeError) as exc:
        _destroy_secrets(_ctx(session, agent, config, sero=sero))

    assert "could not remove slack" in str(exc.value)
    assert "removed: cabal, env" in str(exc.value)
    session.close()


def test_destroy_secrets_never_logs_what_the_lane_said(session_factory, config, caplog):
    # A step's error string lands on AgentStep.error, which any viewer of
    # the agent can read, and the platform's own sentence can quote the
    # request it refused.
    session, agent = _make_ctx_agent(session_factory)
    agent.created_by = OWNER_EMAIL
    sero = FakeSero(delete_error="refused the body not-a-real-value")

    with caplog.at_level("WARNING"), pytest.raises(RuntimeError):
        _destroy_secrets(_ctx(session, agent, config, sero=sero))

    assert "not-a-real-value" not in caplog.text
    assert "refused the body" not in caplog.text
    assert "ClientError" in caplog.text
    session.close()


# --- _destroy_access without a model group (2026-10-01) -------------------------------


# --- namespace ownership (issue #193) -------------------------------------------

# A namespace made long before any agent row in these tests: naive UTC, as
# `KubectlClient.namespace_created_at` answers.
OLD_NAMESPACE = datetime(2020, 1, 1, tzinfo=UTC).replace(tzinfo=None)


def test_destroy_deletes_nothing_in_a_namespace_older_than_the_agent(session_factory, config):
    """Guards destruction safety (issue #193): an agent named after a namespace
    that was there first must not delete it, or the secrets and identity under
    the same name."""
    agent_id = _seed_agent(
        session_factory, status=AgentStatus.READY, gojira_app_id=7, created_by=OWNER_EMAIL
    )
    fake_gojira = FakeGojira()
    fake_sero = FakeSero()
    clients = _clients(gojira=fake_gojira, sero=fake_sero)
    clients.kubectl = FakeKubectl(namespaces={AGENT_NAME: OLD_NAMESPACE})

    thread = run_destroy(session_factory, agent_id, clients, config)
    thread.join(timeout=JOIN_TIMEOUT)

    assert fake_gojira.delete_calls == []
    assert (fake_sero.unsyncs, fake_sero.deletes, fake_sero.offboards) == ([], [], [])
    session = session_factory()
    assert session.get(Agent, agent_id).status == AgentStatus.DESTROY_FAILED
    session.close()


def test_destroy_proceeds_in_a_namespace_made_after_the_agent(session_factory, config):
    """Guards destruction safety (issue #193): the agent's own namespace, made
    by the pipeline after its row, is still torn down."""
    session, agent = _make_ctx_agent(session_factory, gojira_app_id=7)
    fake_gojira = FakeGojira()
    ctx = _ctx(session, agent, config, gojira=fake_gojira)
    made = agent.created_at.replace(tzinfo=None) + timedelta(minutes=5)
    ctx.clients.kubectl = FakeKubectl(namespaces={AGENT_NAME: made})

    _destroy_platform(ctx)

    assert fake_gojira.delete_calls == [(7, True)]


def test_destroy_secrets_still_revokes_the_pass_when_the_namespace_is_not_ours(
    session_factory, config, monkeypatch
):
    """Guards destruction safety (issue #193): a refused namespace keeps the
    stored secrets, but the room's own colleague pass is still revoked and the
    step still fails."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    session, agent = _make_ctx_agent(session_factory)
    fake_sero = FakeSero()
    fake_cabal = FakeCabal()
    ctx = _ctx(
        session, agent, _cabal_enabled_config(config),
        sero=fake_sero, cabal=fake_cabal, acting_email=OWNER_EMAIL,
    )
    ctx.clients.kubectl = FakeKubectl(namespaces={AGENT_NAME: OLD_NAMESPACE})

    with pytest.raises(RuntimeError, match="namespace not ours"):
        _destroy_secrets(ctx)

    assert fake_cabal.revoke_colleague_calls == [AGENT_NAME]
    assert (fake_sero.unsyncs, fake_sero.deletes, fake_sero.offboards) == ([], [], [])


def test_destroy_platform_refuses_an_old_namespace_on_resume(session_factory, config):
    """Guards destruction safety (issue #193): a resume whose destroy-secrets
    is already DONE still must not have gojira delete a namespace that was
    there before the agent."""
    agent_id = _seed_agent(session_factory, status=AgentStatus.DESTROY_FAILED, gojira_app_id=7)
    session = session_factory()
    session.add(AgentStep(agent_id=agent_id, name="destroy-secrets", status=StepStatus.DONE))
    session.commit()
    session.close()
    fake_gojira = FakeGojira()
    clients = _clients(gojira=fake_gojira)
    clients.kubectl = FakeKubectl(namespaces={AGENT_NAME: OLD_NAMESPACE})

    thread = run_destroy(session_factory, agent_id, clients, config)
    thread.join(timeout=JOIN_TIMEOUT)

    assert fake_gojira.delete_calls == []
    session = session_factory()
    step = session.query(AgentStep).filter_by(agent_id=agent_id, name="destroy-platform").one()
    assert step.status == StepStatus.FAILED
    session.close()


def test_destroy_secrets_fails_on_an_old_namespace_with_no_secrets_platform(
    session_factory, config
):
    """Guards destruction safety (issue #193): the check runs whatever the
    secrets platform's state, so the ledger still stops at this step."""
    session, agent = _make_ctx_agent(session_factory)
    ctx = _ctx(session, agent, config, sero=None)
    ctx.clients.kubectl = FakeKubectl(namespaces={AGENT_NAME: OLD_NAMESPACE})

    with pytest.raises(RuntimeError, match="namespace not ours"):
        _destroy_secrets(ctx)


def test_destroy_secrets_says_it_could_not_check_an_unreachable_cluster(
    session_factory, config
):
    """Guards destruction safety (issue #193): an unreachable cluster is not
    reported as someone else's namespace, and nothing stored is removed."""
    session, agent = _make_ctx_agent(session_factory)
    fake_sero = FakeSero()
    ctx = _ctx(session, agent, config, sero=fake_sero, acting_email=OWNER_EMAIL)
    ctx.clients.kubectl = FakeKubectl(namespace_error="Unable to connect to the server")

    with pytest.raises(RuntimeError, match="could not check the namespace"):
        _destroy_secrets(ctx)

    assert (fake_sero.unsyncs, fake_sero.deletes, fake_sero.offboards) == ([], [], [])


def test_destroy_of_an_agent_that_never_onboarded_deletes_nothing_in_an_old_namespace(
    session_factory, config
):
    """Guards destruction safety (issue #193): with no gojira app recorded the
    platform stage has nothing to refuse, so the secrets stage must stop the
    ledger before destroy-access deletes an `agent-<name>` GSA the room never
    made for this agent, and before the row is forgotten."""
    agent_id = _seed_agent(session_factory, status=AgentStatus.FAILED, gojira_app_id=None)
    fake_cloud = FakeCloud(project=config.gcp_project)
    email = f"agent-{AGENT_NAME}@{config.gcp_project}.iam.gserviceaccount.com"
    fake_cloud.gsas.add(email)
    clients = _clients(cloud=fake_cloud)
    clients.kubectl = FakeKubectl(namespaces={AGENT_NAME: OLD_NAMESPACE})

    thread = run_destroy(session_factory, agent_id, clients, config)
    thread.join(timeout=JOIN_TIMEOUT)

    assert email in fake_cloud.gsas
    session = session_factory()
    assert session.get(Agent, agent_id).status == AgentStatus.DESTROY_FAILED
    session.close()


def test_destroy_access_refuses_an_old_namespace(session_factory, config):
    """Guards destruction safety (issue #193): on a resume past destroy-secrets,
    an `agent-<name>` GSA is not proof the room made it for this agent."""
    session, agent = _make_ctx_agent(session_factory)
    fake_cloud = FakeCloud(project=config.gcp_project)
    email = f"agent-{AGENT_NAME}@{config.gcp_project}.iam.gserviceaccount.com"
    fake_cloud.gsas.add(email)
    ctx = _ctx(session, agent, config, cloud=fake_cloud)
    ctx.clients.kubectl = FakeKubectl(namespaces={AGENT_NAME: OLD_NAMESPACE})

    with pytest.raises(RuntimeError):
        _destroy_access(ctx)

    assert email in fake_cloud.gsas


def test_run_destroy_beats_for_the_whole_run(session_factory, config, monkeypatch):
    """Issue #203: a destroy has a heartbeat like a provision, started before
    the first step and stopped after the last, so a pod that dies mid-destroy
    is swept to DESTROY_FAILED instead of staying DESTROYING forever."""
    import kobo_api.destroy as destroy_mod

    events: list[str] = []

    class RecordingBeat:
        def __init__(self, session_factory, agent_id, **kwargs):
            events.append(f"init:{agent_id}")

        def start(self):
            events.append("start")

        def stop(self):
            events.append("stop")

    monkeypatch.setattr(destroy_mod, "_Heartbeat", RecordingBeat)
    agent_id = _seed_agent(session_factory, status=AgentStatus.READY)
    thread = run_destroy(session_factory, agent_id, _clients(cabal=FakeCabal()), config)
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False
    assert events == [f"init:{agent_id}", "start", "stop"]

# --- from test_web_destroy.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


OWNER = "acme-corp"


REPO = f"{OWNER}/agent-{AGENT_NAME}"


IN_FLIGHT_409 = {"error": "the agent is still being created, retry or wait"}


JOIN_DEADLINE_S = 5


POLL_INTERVAL_S = 0.02


@pytest.fixture
def session_factory__web_destroy(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__web_destroy(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
    )


def _make_client(
    session_factory__web_destroy,
    config__web_destroy,
    *,
    github=None,
    gojira=None,
    slack=None,
    groups=None,
    iap_jwks_fetcher=None,
):
    storage = FakeStorage()
    seed_default_personas(storage, bucket=config__web_destroy.personas_bucket)
    return TestClient(
        create_app(
            session_factory__web_destroy,
            config=config__web_destroy,
            clients=Clients(
                github=github if github is not None else FakeGitHub(existing_repos={REPO}),
                gojira=gojira if gojira is not None else FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                slack=slack if slack is not None else FakeSlack(),
                storage=storage,
                groups=groups,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent__web_destroy(
    session_factory__web_destroy,
    config__web_destroy,
    *,
    name: str = AGENT_NAME,
    status=AgentStatus.READY,
    gojira_app_id: int | None = 7,
    slack_app_id: str | None = "A0FAKE",
    repo_full_name: str | None = REPO,
    error: str | None = None,
):
    session = session_factory__web_destroy()
    session.add(
        Agent(
            name=name,
            persona=PERSONA,
            model_id=config__web_destroy.models[0].id,
            status=status,
            gojira_app_id=gojira_app_id,
            slack_app_id=slack_app_id,
            repo_full_name=repo_full_name,
            error=error,
        )
    )
    session.commit()
    session.close()


def _poll_until_gone(client, name, deadline_s=JOIN_DEADLINE_S, headers=None):
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        response = client.get(f"/api/agents/{name}", headers=headers)
        if response.status_code == 404:
            return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"agent {name!r} was never forgotten within {deadline_s}s")


@pytest.mark.parametrize("status", [AgentStatus.PENDING, AgentStatus.PROVISIONING])
def test_destroy_provisioning_in_flight_returns_409(session_factory__web_destroy, config__web_destroy, status):
    """Destruction safety: Destroy provisioning in flight returns 409."""
    config__web_destroy = config__web_destroy.model_copy(update={"operators": []})
    client = _make_client(session_factory__web_destroy, config__web_destroy)
    _seed_agent__web_destroy(session_factory__web_destroy, config__web_destroy, status=status, gojira_app_id=None, slack_app_id=None, repo_full_name=None)

    response = client.delete(f"/api/agents/{AGENT_NAME}")

    assert response.status_code == 409
    assert response.json() == IN_FLIGHT_409


def test_destroy_failed_agent_is_still_destroyable(session_factory__web_destroy, config__web_destroy):
    """Destruction safety: Destroy failed agent is still destroyable.

    A FAILED (never fully provisioned) agent is not blocked — nothing
    about destruction requires a READY agent, only that creation isn't
    still in flight."""
    config__web_destroy = config__web_destroy.model_copy(update={"operators": []})
    client = _make_client(session_factory__web_destroy, config__web_destroy)
    _seed_agent__web_destroy(
        session_factory__web_destroy, config__web_destroy, status=AgentStatus.FAILED,
        gojira_app_id=None, slack_app_id=None, repo_full_name=None,
    )

    response = client.delete(f"/api/agents/{AGENT_NAME}")

    assert response.status_code == 202
    _poll_until_gone(client, AGENT_NAME)


def test_destroy_idempotent_redelete_while_destroy_failed_resumes_to_completion(
    session_factory__web_destroy, config__web_destroy
):
    """Destruction safety: Destroy idempotent redelete while destroy failed resumes to completion.

    Seed the agent already mid-ledger (platform done, access done, slack
    failed) as a prior partial run would leave it, then DELETE again
    ("Destroy again"): 202, and this time slack (now working) completes, and
    the run continues through to full removal.

    Also the destroy-steps payload-label case: a GET taken right after
    seeding (before the redelete) proves the "Model access" label rides the
    payload between the platform and Slack rows, in the ledger's own order —
    not just that DESTROY_STEP_LABELS has the right entry, but that it
    actually reaches the wire for a real destroy-access row.
    """
    config__web_destroy = config__web_destroy.model_copy(update={"operators": []})
    client = _make_client(session_factory__web_destroy, config__web_destroy)
    _seed_agent__web_destroy(
        session_factory__web_destroy, config__web_destroy, status=AgentStatus.DESTROY_FAILED,
        error="slack app deletion failed: slack outage",
    )
    session = session_factory__web_destroy()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    session.add(AgentStep(agent_id=agent.id, name="destroy-platform", status=StepStatus.DONE))
    session.add(AgentStep(agent_id=agent.id, name="destroy-access", status=StepStatus.DONE))
    session.add(
        AgentStep(
            agent_id=agent.id, name="destroy-slack", status=StepStatus.FAILED,
            error="slack app deletion failed: slack outage",
        )
    )
    session.commit()
    session.close()

    before = client.get(f"/api/agents/{AGENT_NAME}")
    names = [s["name"] for s in before.json()["steps"]]
    labels = {s["name"]: s["label"] for s in before.json()["steps"]}
    assert names == ["destroy-platform", "destroy-access", "destroy-slack"]
    assert labels["destroy-platform"] == "Deployment teardown"
    assert labels["destroy-access"] == "Model access"
    assert labels["destroy-slack"] == "Slack app"

    response = client.delete(f"/api/agents/{AGENT_NAME}")

    assert response.status_code == 202
    assert response.json() == {"status": "destroying"}
    _poll_until_gone(client, AGENT_NAME)


def test_destroy_launch_failure_returns_503_and_a_later_destroy_still_succeeds(
    session_factory__web_destroy, config__web_destroy, monkeypatch
):
    """Destruction safety: Destroy launch failure returns 503 and a later destroy still succeeds."""
    import kobo_api.main as main_module

    config__web_destroy = config__web_destroy.model_copy(update={"operators": []})
    client = _make_client(session_factory__web_destroy, config__web_destroy)
    _seed_agent__web_destroy(session_factory__web_destroy, config__web_destroy)

    def _raising_run_destroy(session_factory, agent_id, clients, config, **kwargs):
        raise RuntimeError("no more threads")

    monkeypatch.setattr(main_module, "run_destroy", _raising_run_destroy)

    first = client.delete(f"/api/agents/{AGENT_NAME}")

    assert first.status_code == 503
    assert first.json() == {"error": "failed to start destruction: no more threads"}

    session = session_factory__web_destroy()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    assert agent.status == AgentStatus.DESTROY_FAILED
    assert agent.error == "failed to start destruction: no more threads"
    session.close()

    monkeypatch.undo()

    second = client.delete(f"/api/agents/{AGENT_NAME}")

    assert second.status_code == 202
    _poll_until_gone(client, AGENT_NAME)


def test_redelete_while_destroy_in_flight_returns_202_without_double_launch(
    session_factory__web_destroy, config__web_destroy
):
    """Destruction safety: Redelete while destroy in flight returns 202 without double launch.

    A re-DELETE against an agent already DESTROYING must keep returning
    202 (the idempotent-redelete contract, unchanged) but must NOT launch a
    second concurrent run_destroy thread. Simulated deterministically by
    registering the agent's in-flight slot directly in kobo_api.destroy's
    module-level registry before issuing the DELETE — no mocking of the
    route or of run_destroy itself, just the same registry `run_destroy`
    checks on every call."""
    from kobo_api import destroy as destroy_module

    config__web_destroy = config__web_destroy.model_copy(update={"operators": []})
    client = _make_client(session_factory__web_destroy, config__web_destroy)
    _seed_agent__web_destroy(session_factory__web_destroy, config__web_destroy, status=AgentStatus.DESTROYING)
    session = session_factory__web_destroy()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    agent_id = agent.id
    session.close()

    destroy_module._destroying_agent_ids.add(agent_id)
    try:
        response = client.delete(f"/api/agents/{AGENT_NAME}")
    finally:
        destroy_module._destroying_agent_ids.discard(agent_id)

    assert response.status_code == 202
    assert response.json() == {"status": "destroying"}

    # No duplicate thread launched — the agent is left exactly as it was
    # (still DESTROYING; no teardown work happened from this second call,
    # since run_destroy's own launch was a no-op skip).
    session = session_factory__web_destroy()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    assert agent is not None
    assert agent.status == AgentStatus.DESTROYING
    session.close()


# --- from test_build_log.py ---


class LogGitHub:
    def __init__(self, run=None, log="line1\nline2", raise_on_log=False):
        self._run = run
        self._log = log
        self._raise = raise_on_log

    def latest_run(self, repo):
        return self._run

    def run_log(self, repo, run_id, *, failed_only=True):
        if self._raise:
            from kobo_api.clients import ClientError

            raise ClientError("boom")
        self.failed_only = failed_only
        return self._log


# --- from test_build_log.py ---


def _client(session_factory, config, github):
    clients = Clients(
        github=github,
        gojira=FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=FakeKubectl(),
        storage=FakeStorage(),
    )
    return TestClient(create_app(session_factory, config=config, clients=clients))


# --- from test_api_destroy_claim.py ---


def _seed(session_factory, status):
    session = session_factory()
    session.add(Agent(name="ward", persona="p", model_id="m", status=status, repo_full_name="acme/agent-ward"))
    session.commit()
    session.close()


def test_a_destroying_row_is_not_claimed_again_and_launches_nothing(session_factory, config, monkeypatch):
    """Destruction safety: A destroying row is not claimed again and launches nothing."""
    import kobo_api.main as main_mod

    launches = []
    monkeypatch.setattr(main_mod, "run_destroy", lambda *a, **k: launches.append(a))
    _seed(session_factory, AgentStatus.DESTROYING)
    r = _client(session_factory, config, LogGitHub()).delete("/api/agents/ward")
    assert r.status_code == 202
    assert r.json() == {"status": "destroying"}
    assert launches == []


def test_a_failed_destroy_is_claimed_and_resumed(session_factory, config):
    """Destruction safety: A failed destroy is claimed and resumed."""
    _seed(session_factory, AgentStatus.DESTROY_FAILED)
    client = _client(session_factory, config, LogGitHub())
    r = client.delete("/api/agents/ward")
    assert r.status_code == 202
    session = session_factory()
    row = session.query(Agent).filter_by(name="ward").one_or_none()
    # Either the run is still going (DESTROYING, with a fresh heartbeat from the
    # claim) or the fakes let it finish and forget the row.
    assert row is None or (row.status in (AgentStatus.DESTROYING, AgentStatus.DESTROY_FAILED) and row.run_heartbeat_at is not None)
    session.close()


# --- from test_directory_sweep.py ---


TEMPLATE = "http://agent-{name}.default.svc.cluster.local"


def _room_entry(name: str, **over) -> dict:
    return {"name": name, "status": "active", "kind": "federated", "endpoint": TEMPLATE.format(name=name), **over}


def _session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_sweep_retires_only_the_rooms_own_entries_for_agents_it_no_longer_has(tmp_path):
    """Destruction safety: Sweep retires only the rooms own entries for agents it no longer has.

    Destruction safety: an entry without the room's registration marker
    (another endpoint, or not a graduated row) is reported, never absorbed;
    an entry for any agent row the room still holds is kept, whatever its
    state; a hand-built agent with no row but a Deployment `<name>/<name>`
    in the cluster is live, never absorbed; the dry run absorbs nothing at
    all; and a cluster that cannot be asked retires nothing."""
    factory = _session_factory(tmp_path)
    session = factory()
    session.add(Agent(name="kept", persona="p", model_id="m", status=AgentStatus.DESTROYING, repo_full_name="o/r"))
    session.commit()
    cfg = Config(workdir=tmp_path / "work", agent_endpoint_template=TEMPLATE)
    cabal = FakeCabal(agents_result=[
        _room_entry("kept"),
        _room_entry("gone"),
        _room_entry("handbuilt"),
        _room_entry("bespoke", endpoint="http://somewhere.else"),
        _room_entry("local", kind="member"),
        _room_entry("retired-already", status="retired"),
        {"name": "ops-session", "status": "active", "kind": "session", "endpoint": None},
    ])
    kubectl = FakeKubectl(deployments={("handbuilt", "handbuilt")})

    preview = sweep_directory(cabal, cfg, session, kubectl=kubectl, dry_run=True)
    assert cabal.absorb_calls == []
    assert (preview["retired"], preview["kept"], preview["live"], preview["unknown"]) == (
        ["gone"], ["kept"], ["handbuilt"], ["bespoke", "local"],
    )

    silent = sweep_directory(
        cabal, cfg, session, kubectl=FakeKubectl(deployment_exists_error="no cluster"), dry_run=False
    )
    assert cabal.absorb_calls == [] and silent["retired"] == []
    assert sorted(silent["unknown"]) == ["bespoke", "gone", "handbuilt", "local"]
    assert silent["note"] == CLUSTER_SILENT_NOTE

    result = sweep_directory(cabal, cfg, session, kubectl=kubectl, dry_run=False)
    assert cabal.absorb_calls == ["gone"]
    assert result["retired"] == ["gone"] and result["live"] == ["handbuilt"] and result["failed"] == []
    session.close()


# --- from test_directory_delivery.py ---


def _directory_config(config: Config, tmp_path, **more) -> Config:
    return config.model_copy(update={
        "delivery_backend": "directory", "directory_root": str(tmp_path / "agents"), **more,
    })


def test_destroy_for_a_folder_agent_removes_the_folder_and_touches_no_platform(session_factory, config, tmp_path):
    """Destruction safety: Destroy for a folder agent removes the folder and touches no platform."""
    cfg = _directory_config(config, tmp_path)
    folder = tmp_path / "agents" / "clio"
    (folder / "bundle").mkdir(parents=True)
    (folder / "bundle" / "agent.yaml").write_text("name: clio\n")
    agent_id = _seed_agent(session_factory, status=AgentStatus.READY, kind=KIND_DIRECTORY)
    github, gojira = FakeGitHub(), FakeGojira()

    thread = run_destroy(session_factory, agent_id, _clients(github=github, gojira=gojira), cfg)
    thread.join(timeout=5)

    assert not folder.exists()
    assert gojira.delete_calls == [] and github.rename_calls == []
    session = session_factory()
    assert session.get(Agent, agent_id) is None
    session.close()


# --- from test_pr_broker.py ---


@pytest.fixture
def broker_config(tmp_path):
    key = tmp_path / "pr-app.pem"
    key.write_text("not-a-real-key")
    return Config(
        workdir=tmp_path / "work",
        pr_app_id="4741722",
        pr_app_installation_id="157070479",
        pr_app_private_key_path=key,
        cabal_url="http://cabal.test",
    )


@pytest.fixture
def broker_client(session_factory, broker_config):
    return TestClient(create_app(session_factory, config=broker_config))


def _agent(session_factory, name="ward"):
    session = session_factory()
    agent = Agent(
        name=name, persona="helpful-generalist", model_id="m", status=AgentStatus.READY
    )
    session.add(agent)
    session.commit()
    session.close()
    return name


def test_destroy_forget_deletes_repo_access_rows(broker_client, session_factory, broker_config):
    """Destruction safety: Destroy forget deletes repo access rows.

    A future agent reusing a destroyed agent's name must inherit ZERO
    grants -- destroy-forget clears repo_access by name."""
    from kobo_api.clients import Clients
    from kobo_api.destroy import DestroyCtx, _destroy_forget

    name = _agent(session_factory)
    rows = broker_client.post(
        f"/api/agents/{name}/repo-access", json={"repositories": ["acme/a"]}
    ).json()["requested"]
    broker_client.post(
        "/api/repo-access/decide", json={"ids": [rows[0]["id"]], "decision": "approved"}
    )

    session = session_factory()
    agent = session.query(Agent).filter_by(name=name).one()
    ctx = DestroyCtx(
        agent=agent,
        session=session,
        config=broker_config,
        clients=Clients(github=None, gojira=None, cloud=None, agent=None, kubectl=None),
    )
    _destroy_forget(ctx)
    assert session.query(RepoAccess).count() == 0
    session.close()


# --- from test_api_connect.py ---


AGENT_NAME = "clio"


ORG = "example-org"


REPO__api_connect = f"{ORG}/team-chatbot"


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    import kobo_api.steps as steps_module

    monkeypatch.setattr(steps_module, "_sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _fresh_scan_window():
    """The scan limiter's window is module state shared by every caller in
    the process, so each case starts with an empty one rather than one
    carrying the previous case's scans."""
    rate_limit.reset()
    yield
    rate_limit.reset()


@pytest.fixture
def session_factory__api_connect(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__api_connect(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        github_org=ORG,
    )


@pytest.fixture
def github():
    return FakeGitHub(existing_repos={REPO__api_connect})


@pytest.fixture
def clients(github, fake_storage):
    return Clients(
        github=github,
        gojira=FakeGojira(activate_after=1),
        cloud=FakeCloud(),
        agent=FakeAgent(reply="hello yourself, human"),
        kubectl=FakeKubectl(),
        storage=fake_storage,
        image=FakeImage(),
    )


CONVERT_PATH = f"/api/agents/{AGENT_NAME}/convert-to-connected"


def _seed_composed(
    session_factory__api_connect,
    *,
    status=AgentStatus.READY,
    kind=KIND_COMPOSED,
    name=AGENT_NAME,
    secret_names=None,
    group_name=None,
):
    """One agent row as the create route leaves it: the room's own repository,
    a real persona, and whatever status this case is about."""
    session = session_factory__api_connect()
    session.add(
        Agent(
            name=name,
            persona="helpful-generalist",
            model_id="m",
            skills=[],
            kind=kind,
            repo_full_name=f"{ORG}/agent-{name}",
            status=status,
            group_name=group_name,
            secret_names=secret_names if secret_names is not None else [],
            run_mode=RUN_MODE_HTTP if kind == KIND_CONNECTED else None,
        )
    )
    session.commit()
    session.close()


@pytest.fixture
def switchboard(session_factory__api_connect, config__api_connect, fake_storage, monkeypatch):
    """A room with the switchboard wired, the fakes the convert route must
    not touch, and a client over both.

    The switchboard has to be genuinely on for the retire to be attempted at
    all (`cabal_enabled` reads two config keys and the operator token), and
    the point of most of these cases is what the route does NOT do, which
    only means something when the clients that could have done it are there.
    """
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = config__api_connect.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": "http://agent-{name}.internal",
        }
    )
    fakes = SimpleNamespace(
        github=FakeGitHub(existing_repos={REPO__api_connect}),
        gojira=FakeGojira(),
        kubectl=FakeKubectl(),
        cabal=FakeCabal(),
        config=cfg,
    )
    clients = Clients(
        github=fakes.github,
        gojira=fakes.gojira,
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=fakes.kubectl,
        storage=fake_storage,
        image=FakeImage(),
        cabal=fakes.cabal,
    )
    fakes.client = TestClient(create_app(session_factory__api_connect, config=cfg, clients=clients))
    return fakes


def test_convert_retires_the_switchboard_identity(switchboard, session_factory__api_connect):
    """Destruction safety: Convert retires the switchboard identity.

    A connected agent has no directory entry: the pipeline that builds one
    has no directory step. An agent that was composed does have one, and it
    stops being true the moment the kind changes."""
    _seed_composed(session_factory__api_connect)

    switchboard.client.post(CONVERT_PATH, json={"run_mode": RUN_MODE_HTTP})

    assert switchboard.cabal.absorb_calls == [AGENT_NAME]


# --- from test_api_org_status.py ---


WHOAMI_AUDIENCE = "test-audience"


# --- from test_api_org_status.py ---


def _fetcher(jwk):
    def fetch():
        return {"keys": [jwk]}

    return fetch


# --- from test_api_org_status.py ---


WHOAMI_KID = "test-key-1"


def _generate_jwk(kid: str = WHOAMI_KID):
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


# --- from test_api_org_status.py ---


def _sign_whoami(private_key, *, email: str | None, kid: str = WHOAMI_KID) -> str:
    now = int(time.time())
    claims = {
        "aud": WHOAMI_AUDIENCE,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
    }
    if email is not None:
        claims["email"] = email
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": kid})


# --- from test_api_admin_integrations.py ---


GATEWAY_REPO = "example-org/gateway-config"


TARGETS = '[[target]]\nname = "existing"\ntransport = "http"\naddress = "https://old.example"\n'


def _client__api_admin_integrations(tmp_path, fake_storage, config: Config, *, github: FakeGitHub, sero: FakeSero | None):
    engine = create_engine(f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    private_key, jwk = _generate_jwk()
    client = TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=github, gojira=FakeGojira(), cloud=FakeCloud(), agent=FakeAgent(),
                kubectl=FakeKubectl(), storage=fake_storage, sero=sero,
            ),
            iap_jwks_fetcher=_fetcher(jwk),
        )
    )
    return client, {ASSERTION_HEADER: _sign_whoami(private_key, email="ops@example.com")}


def _config(tmp_path, **overrides) -> Config:
    values = {
        "workdir": tmp_path / "work", "iap_audience": WHOAMI_AUDIENCE, "operators": ["ops@example.com"],
        "admin_panel": True, "sero_url": "http://sero.internal", "gateway_config_repo": GATEWAY_REPO,
        "gateway_namespace": "liminal",
    }
    values.update(overrides)
    return Config(**values)


BODY = {"vendor": "datadog", "name": "Datadog", "base_access": "read", "credential": "dd-secret-token",
        "address": "https://mcp.datadoghq.com", "trust": "untrusted"}


def test_disconnect_reverses_the_three_steps(tmp_path, fake_storage):
    """Destruction safety: Disconnect reverses the three steps."""
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"targets.toml": TARGETS}, track_pushed_content=True)
    sero = FakeSero()
    client, headers = _client__api_admin_integrations(tmp_path, fake_storage, _config(tmp_path), github=github, sero=sero)
    client.post("/api/admin/integrations", json=BODY, headers=headers)

    response = client.delete("/api/admin/integrations/datadog", headers=headers)

    assert response.status_code == 200
    assert response.json()["secret"] == "removed"
    assert sero.deletes == [("ops@example.com", "apps/kobo/integrations/datadog")]
    assert sero.unsyncs[0][1:] == ("kobo", "liminal", "integration-datadog")
    assert client.get("/api/admin/integrations", headers=headers).json() == {"integrations": []}
    assert client.delete("/api/admin/integrations/datadog", headers=headers).status_code == 404


# --- from test_web_secrets.py ---


AGENT = "clio"


REPO__web_secrets = "example-org/agent-clio"


OWNER__web_secrets = "owner@example.com"


PR_URL = "https://example.test/pull/7"


# Never a real credential: a placeholder standing in for one, so a test can
# prove it reaches the lane and appears in no response, error or log.
VALUE = "not-a-real-key-value"


BASE_GOJIRA_YAML = "name: clio\nservice_account: agent-clio\n"


AUDIENCE = "test-audience"


KID = "test-key-1"


@pytest.fixture(scope="module")
def _key():
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": KID, "use": "sig", "alg": "ES256"})
    return private_key, jwk


@pytest.fixture
def session_factory__web_secrets(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__web_secrets(tmp_path, fake_storage):
    # `iap_audience` is what makes the owner-or-operator gate real: without
    # a perimeter the room short-circuits every one of those gates to
    # allowed, so a file about who may read and write an agent's secrets
    # has to turn it on.
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        iap_audience=AUDIENCE,
        operators=[],
    )


def _github(**kwargs) -> FakeGitHub:
    """A repository whose `gojira.yaml` reads the same two ways the room
    reads it: through `read_file` (the panel's own read) and through a
    clone (what a write edits and pushes)."""
    repo_files = kwargs.pop("repo_files", {"gojira.yaml": BASE_GOJIRA_YAML})
    extra = kwargs.pop("extra_clone_files", None)
    if extra is None and "gojira.yaml" in repo_files:
        extra = {"gojira.yaml": repo_files["gojira.yaml"]}
    return FakeGitHub(
        existing_repos={REPO__web_secrets},
        repo_files=repo_files,
        extra_clone_files=extra,
        pr_url=PR_URL,
        **kwargs,
    )


def _client__web_secrets(
    session_factory__web_secrets,
    config__web_secrets,
    fake_storage,
    key,
    *,
    kind: str = KIND_COMPOSED,
    sero: FakeSero | None = None,
    github: FakeGitHub | None = None,
    kubectl: FakeKubectl | None = None,
    created_by: str | None = OWNER,
    status: AgentStatus = AgentStatus.READY,
) -> tuple[TestClient, Clients]:
    github = github or _github()
    gojira = FakeGojira()
    app_id = gojira.create_app(
        name=AGENT, repo_full_name=REPO__web_secrets, cluster="a-cluster", namespace=AGENT
    )
    clients = Clients(
        github=github,
        gojira=gojira,
        cloud=FakeCloud(),
        agent=FakeAgent(reply="sure"),
        kubectl=kubectl or FakeKubectl(),
        storage=fake_storage,
        slack=FakeSlack(),
        cabal=FakeCabal(),
        image=FakeImage(),
        sero=sero,
    )

    session = session_factory__web_secrets()
    session.add(
        Agent(
            name=AGENT,
            persona="connected" if kind == KIND_CONNECTED else "helpful-generalist",
            model_id="vertex_ai/claude-sonnet-4-5",
            skills=[],
            status=status,
            repo_full_name=REPO__web_secrets,
            gojira_app_id=app_id,
            created_by=created_by,
            kind=kind,
        )
    )
    session.commit()
    session.close()

    _, jwk = key
    app = create_app(
        session_factory__web_secrets,
        config=config__web_secrets,
        clients=clients,
        iap_jwks_fetcher=lambda: {"keys": [jwk]},
    )
    return TestClient(app), clients


def _as(key, email: str = OWNER) -> dict[str, str]:
    """A signed assertion naming `email`, the way the perimeter hands one
    to the room. Named once so every case reads as "who is asking" rather
    than as plumbing."""
    private_key, _ = key
    now = int(time.time())
    claims = {
        "aud": AUDIENCE,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
        "email": email,
    }
    return {ASSERTION_HEADER: jwt.encode(claims, private_key, algorithm="ES256",
                                         headers={"kid": KID})}


@pytest.fixture(autouse=True)
def _clear_restart_window():
    rate_limit.reset()
    yield
    rate_limit.reset()


def test_a_composed_agent_commits_the_line_away_before_deleting_the_secret(
    session_factory__web_secrets, config__web_secrets, fake_storage, _key
):
    """Destruction safety: A composed agent commits the line away before deleting the secret."""
    order: list[str] = []

    class _Ordered(FakeSero):
        def unsync(self, acting_email, app, k8s_namespace, name):
            order.append("unsync")
            super().unsync(acting_email, app, k8s_namespace, name)

        def delete_secret(self, acting_email, path):
            order.append("delete")
            super().delete_secret(acting_email, path)

    class _OrderedGitHub(FakeGitHub):
        def commit_push(self, workdir, message):
            order.append("push")
            return super().commit_push(workdir, message)

    sero = _Ordered()
    sero.stored[f"apps/{AGENT}/env"] = {"API_KEY": VALUE}
    github = _OrderedGitHub(
        existing_repos={REPO__web_secrets},
        repo_files={
            "gojira.yaml": BASE_GOJIRA_YAML + f"secrets_from:\n  - {AGENT}-env\n"
        },
        extra_clone_files={
            "gojira.yaml": BASE_GOJIRA_YAML + f"secrets_from:\n  - {AGENT}-env\n"
        },
        pr_url=PR_URL,
    )
    client, clients = _client__web_secrets(
        session_factory__web_secrets, config__web_secrets, fake_storage, _key, sero=sero, github=github
    )

    body = client.put(
        f"/api/agents/{AGENT}/secrets",
        json={"remove": ["API_KEY"]},
        headers=_as(_key),
    ).json()

    assert order == ["push", "unsync", "delete"]
    assert body["emptied"] is True
    assert body["pending_unwire"] is False
    pushed = yaml.safe_load(github.pushed_file("gojira.yaml"))
    assert pushed["secrets_from"] == []
    # And the deploy is asked for after the Secret is gone, not between.
    assert clients.gojira.reprovision_calls


def test_a_branch_the_room_did_not_make_is_never_deleted(
    session_factory__web_secrets, config__web_secrets, fake_storage, _key
):
    """Destruction safety: A branch the room did not make is never deleted."""
    from kobo_api.clients import BranchComparison

    github = _github(
        existing_branches={f"{REPO__web_secrets}#{ENV_BRANCH}"},
        comparisons={
            ENV_BRANCH: BranchComparison(
                found=True,
                files=("gojira.yaml", "src/main.py"),
                authors=("someone@example.com",),
            )
        },
    )
    client, _ = _client__web_secrets(
        session_factory__web_secrets, config__web_secrets, fake_storage, _key, kind=KIND_CONNECTED, github=github
    )

    resp = client.put(
        f"/api/agents/{AGENT}/env",
        json={"set": {"API_BASE": "https://example.test"}},
        headers=_as(_key),
    )

    assert resp.status_code == 409
    assert "the room did not make it" in resp.json()["error"]
    assert github.deleted_branches == []
    assert github.open_pr_calls == []


# --- from test_sero.py ---


URL = "http://secrets.example"


AUDIENCE__sero = "http://secrets.example"


NAMESPACE = "platform"


EMAIL = "someone@example.com"


AGENT = "clio"


def _client__sero(handler, *, token: str = "id-token") -> HttpxSeroClient:
    return HttpxSeroClient(
        URL,
        AUDIENCE__sero,
        NAMESPACE,
        token_provider=lambda: token,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_unsync_raises_when_the_object_belongs_to_another_field_manager():
    """Destruction safety: Unsync raises when the object belongs to another field manager."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": "that object is managed by kustomize"})

    with pytest.raises(ClientError) as exc:
        _client__sero(handler).unsync(EMAIL, AGENT, AGENT, f"{AGENT}-env")

    assert "that object is managed by kustomize" in str(exc.value)


def test_unsync_says_so_when_the_delete_left_a_new_object_standing():
    # `recreated` means what this call removed is gone and something else
    # now stands at the name. A caller that read `deleted` and stopped would
    # leave a live ExternalSecret it does not know about.
    """Destruction safety: Unsync says so when the delete left a new object standing."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"applied": True, "state": "recreated", "recreated": True, "deleted": True}
        )

    with pytest.raises(ClientError) as exc:
        _client__sero(handler).unsync(EMAIL, AGENT, AGENT, f"{AGENT}-env")

    assert f"{AGENT}-env" in str(exc.value)


# --- from test_publish_skills.py ---


_TEMPLATES_DIR = Path(__file__).parent.parent / "kobo_api" / "templates" / "skills_publish"


_SCRIPT_PATH = _TEMPLATES_DIR / "publish_skills.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("publish_skills", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclasses' postponed-annotation resolution (this script uses `from
    # __future__ import annotations`) looks the module up in `sys.modules`
    # by name — without registering it there first, `@dataclass` on `Skill`
    # raises AttributeError the moment this module is exec'd.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ps():
    return _load_module()


class _FakeBlob:
    """Mirrors `google.cloud.storage.Blob`'s surface `sync()` calls: a
    `.metadata` setter (read by `upload_from_string`, matching the real
    client's multipart-upload behavior of sending whatever `.metadata` was
    set to before the upload call) plus `.upload_from_string(data,
    content_type=...)` and `.delete()`, and the `.name` attribute
    `list_blobs()` results are read for.
    """

    def __init__(self, bucket: _FakeBucket, name: str):
        self._bucket = bucket
        self.name = name
        self.metadata: dict | None = None

    def upload_from_string(self, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._bucket.objects[self.name] = {
            "data": data,
            "metadata": dict(self.metadata or {}),
            "content_type": content_type,
        }

    def delete(self) -> None:
        self._bucket.objects.pop(self.name, None)


class _FakeBucket:
    """Mirrors `google.cloud.storage.Bucket`'s surface `sync()` calls:
    `.blob(key)` and `.list_blobs(prefix=...)`."""

    def __init__(self, name: str):
        self.name = name
        self.objects: dict[str, dict] = {}

    def blob(self, key: str) -> _FakeBlob:
        return _FakeBlob(self, key)

    def list_blobs(self, prefix: str = ""):
        return [_FakeBlob(self, name) for name in list(self.objects) if name.startswith(prefix)]


class _FakeBucketClient:
    """Mirrors `google.cloud.storage.Client`'s surface `sync()` calls:
    `.bucket(name) -> Bucket` — exactly what `_real_client()`'s real
    `storage.Client()` would need to support for this script to run
    unmodified against it.
    """

    def __init__(self):
        self._buckets: dict[str, _FakeBucket] = {}

    def bucket(self, name: str) -> _FakeBucket:
        return self._buckets.setdefault(name, _FakeBucket(name))


def _skill_on_disk(ps, tmp_path: Path, *, team: str, skill_id: str, files: dict[str, str]):
    skill_dir = tmp_path / team / skill_id
    for rel, content in files.items():
        path = skill_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return ps.Skill(
        id=skill_id,
        team=team,
        dir=skill_dir,
        files=sorted(Path(rel) for rel in files),
        name=skill_id,
        description=f"{skill_id} description",
        tags=[],
    )


def test_sync_prunes_stale_but_spares_index_json(ps, tmp_path, monkeypatch):
    """Destruction safety: Sync prunes stale but spares index json."""
    monkeypatch.setattr(ps, "_utcnow_iso", lambda: "2026-08-03T00:00:00Z")
    client = _FakeBucketClient()
    bucket = client.bucket("skills-bucket")
    # Pre-seed leftover state from a previous publish: a skill file that no
    # longer exists in this run's discovered set, plus an existing index.json
    # (which must be spared by the prune even though it's never in the
    # "new keys" set either — it always gets rewritten at the end instead).
    bucket.blob("acme/old-skill/SKILL.md").upload_from_string(b"stale", content_type="text/markdown")
    bucket.blob("index.json").upload_from_string(b"{}", content_type="application/json")

    skill = _skill_on_disk(
        ps, tmp_path, team="acme", skill_id="code-review", files={"SKILL.md": "x"}
    )

    result = ps.sync(client, "skills-bucket", [skill], "cafe")

    assert result == {"uploaded": 1, "deleted": 1}
    assert "acme/old-skill/SKILL.md" not in bucket.objects
    assert "index.json" in bucket.objects
    assert "acme/code-review/SKILL.md" in bucket.objects


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


def test_repo_exists_is_false_for_a_name_github_only_redirects():
    """Destruction safety: Repo exists is false for a name github only redirects.

    Destroy renames an agent's repo to archived-agent-<name>-<id>; GitHub
    keeps answering at the old name through a redirect. That name is free."""
    runner = RecordingRunner(
        results=[(0, '{"full_name":"acme/archived-agent-clio-5","archived":true}', "")]
    )
    client = GhCLI(runner=runner)

    assert client.repo_exists("acme/agent-clio") is False
    assert runner.calls == [["gh", "api", "repos/acme/agent-clio"]]


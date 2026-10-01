"""Tests for the metadata HTTP routes (persona-metadata plan, Task 3):
`PATCH /api/agents/{name}/metadata` and `POST
/api/agents/{name}/metadata/regenerate` — ready-gated, any signed-in user
(no operator gate), against the fakes (tests/fakes.py), no real
git/kubectl/Vertex involved.

Same harness shape as test_api_slack.py/test_api_knowledge.py: own
`session_factory`/`config` fixtures, a `TestClient` built from `create_app`.
The 502-consistency proof (a card-commit failure must leave the DB
untouched) follows test_api_slack.py's own `test_put_channels_rewire_
failure_returns_502_and_leaves_original_channels_stored` idiom exactly: a
follow-up GET through the SAME TestClient (a fresh request-scoped session
per call, via `get_session`) is the "fresh-session" proof — it can only see
what the PATCH/regenerate route actually committed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import ClientError, Clients
from kobo_api.config import Config
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus, Base
from kobo_api.personas import seed_default_personas
from kobo_api.purpose_tags import encode_tags
from kobo_api.search import ingest_indexes
from tests.fakes import (
    FakeAgent,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeName,
    FakeStorage,
)

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
REPO = "acme/agent-clio"

SKILLS_BUCKET = "skills-bucket"
PERSONAS_BUCKET = "personas-bucket"


class _RaisingGitHub(FakeGitHub):
    """Every clone raises `ClientError` — the card-commit 502 fixture,
    modeling a transient GitHub outage without a real subprocess."""

    def clone(self, repo: str, dest: Path) -> None:
        raise ClientError("gh: rate limited")


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config(tmp_path):
    return Config(
        workdir=tmp_path / "work", skills_bucket=SKILLS_BUCKET, personas_bucket=PERSONAS_BUCKET
    )


def _make_client(session_factory, config, *, github=None, name=None, storage=None, cabal=None):
    if storage is None:
        storage = FakeStorage()
        seed_default_personas(storage, bucket=PERSONAS_BUCKET)
    return TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=github if github is not None else FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                storage=storage,
                name=name,
                cabal=cabal,
            ),
        )
    )


def _enabled_config(config):
    """`config` plus the two org keys `cabal_enabled` needs — the caller
    must also monkeypatch CABAL_OPERATOR_TOKEN, the third leg, before this
    reads as enabled (colleagues switchboard plan, Task 2's metadata
    tails)."""
    return config.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": "http://agent-{name}.default.svc.cluster.local",
        }
    )


def _seed_agent(
    session_factory,
    config,
    *,
    status=AgentStatus.READY,
    repo_full_name=REPO,
    team="shared",
    purpose_tags=None,
    capability=None,
    skills=None,
    group_name=None,
):
    session = session_factory()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config.models[0].id,
            status=status,
            repo_full_name=repo_full_name,
            team=team,
            purpose_tags=encode_tags(purpose_tags) if purpose_tags is not None else None,
            capability=capability,
            skills=skills or [],
            group_name=group_name,
        )
    )
    session.commit()
    session.close()


def _seed_colleague(session_factory, name: str, *, group_name: str | None) -> None:
    """A second, READY agent (never `AGENT_NAME`) for the roster-refresh
    tests below -- `_seed_agent` above always names its row `AGENT_NAME`,
    so a colleague needs its own row built directly."""
    session = session_factory()
    session.add(
        Agent(
            name=name,
            persona=PERSONA,
            model_id="vertex_ai/claude-sonnet-4-5",
            status=AgentStatus.READY,
            group_name=group_name,
        )
    )
    session.commit()
    session.close()


# --- PATCH /api/agents/{name}/metadata -------------------------------------------


def test_patch_metadata_unknown_agent_404s(session_factory, config):
    client = _make_client(session_factory, config)
    response = client.patch(f"/api/agents/{AGENT_NAME}/metadata", json={"team": "acme-team"})
    assert response.status_code == 404


def test_patch_metadata_not_ready_409s(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=AgentStatus.PROVISIONING)

    response = client.patch(f"/api/agents/{AGENT_NAME}/metadata", json={"team": "acme-team"})

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for directory changes yet"}


def test_patch_metadata_updates_all_three_fields_and_returns_sha(session_factory, config):
    github = FakeGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config, team="shared", purpose_tags=["old"])

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/metadata",
        json={
            "team": "acme-team",
            "purpose_tags": ["billing", "support"],
            "capability": "Helps customers with billing.",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "team": "acme-team",
        "purpose_tags": ["billing", "support"],
        "capability": "Helps customers with billing.",
        "group": "shared",
        "sha": response.json()["sha"],
    }
    assert response.json()["sha"]
    assert github.pushes[-1][1] == "kobo: directory details updated"

    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["team"] == "acme-team"
    assert detail["purpose_tags"] == ["billing", "support"]
    assert detail["capability"] == "Helps customers with billing."


def test_patch_metadata_partial_update_only_touches_submitted_fields(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(
        session_factory, config, team="acme-team", purpose_tags=["billing"],
        capability="Original sentence.",
    )

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/metadata", json={"capability": "A new sentence."}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["team"] == "acme-team"
    assert body["purpose_tags"] == ["billing"]
    assert body["capability"] == "A new sentence."


def test_patch_metadata_empty_body_leaves_everything_unchanged(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(
        session_factory, config, team="acme-team", purpose_tags=["billing"],
        capability="Original sentence.",
    )

    response = client.patch(f"/api/agents/{AGENT_NAME}/metadata", json={})

    assert response.status_code == 200
    assert response.json()["team"] == "acme-team"
    assert response.json()["purpose_tags"] == ["billing"]
    assert response.json()["capability"] == "Original sentence."


def test_patch_metadata_invalid_team_slug_rejected(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/metadata", json={"team": "Not A Valid Slug!"}
    )

    assert response.status_code == 422
    assert response.json() == {"error": "that doesn't look like a team name"}


def test_patch_metadata_purpose_tags_over_cap_rejected(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/metadata",
        json={"purpose_tags": [f"tag{i}" for i in range(9)]},
    )

    assert response.status_code == 422
    assert response.json() == {"error": "Eight tags at most."}


def test_patch_metadata_capability_over_max_len_rejected(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/metadata", json={"capability": "x" * 161}
    )

    assert response.status_code == 422
    assert response.json() == {
        "error": "capability must be plain text, 160 characters or fewer"
    }


def test_patch_metadata_capability_blank_after_cleanup_rejected(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.patch(f"/api/agents/{AGENT_NAME}/metadata", json={"capability": "   "})

    assert response.status_code == 422


def test_patch_metadata_capability_strips_em_dashes_before_storing(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/metadata",
        json={"capability": "Helps with billing—and returns."},
    )

    assert response.status_code == 200
    assert "—" not in response.json()["capability"]
    assert response.json()["capability"] == "Helps with billing-and returns."


def test_patch_metadata_card_commit_failure_returns_502_and_db_not_committed(
    session_factory, config
):
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(
        session_factory, config, team="original-team", purpose_tags=["original"],
        capability="Original sentence.",
    )

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/metadata",
        json={
            "team": "new-team",
            "purpose_tags": ["new-tag"],
            "capability": "A new sentence.",
        },
    )

    assert response.status_code == 502
    assert response.json() == {
        "error": "couldn't update the directory details. Try again."
    }

    # Fresh-session proof: a follow-up GET (its own request-scoped session)
    # still shows the ORIGINAL stored values, not the submitted ones.
    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["team"] == "original-team"
    assert detail["purpose_tags"] == ["original"]
    assert detail["capability"] == "Original sentence."


# --- PATCH .../metadata: best-effort directory tail (colleagues switchboard plan, Task 2) --


def test_patch_metadata_disabled_switchboard_never_attempts_a_sync(session_factory, config):
    """The default `config` fixture carries neither cabal org key — the
    PATCH route's own response/DB behavior must be byte-identical to before
    this tail existed, and no cabal client is ever touched (there isn't
    one: `_make_client` doesn't pass `cabal=`, so a wired attempt would
    AttributeError on a None client)."""
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.patch(f"/api/agents/{AGENT_NAME}/metadata", json={"team": "acme-team"})

    assert response.status_code == 200
    assert response.json()["team"] == "acme-team"


def test_patch_metadata_success_best_effort_syncs_the_directory(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal()
    client = _make_client(session_factory, cfg, cabal=fake_cabal)
    _seed_agent(session_factory, cfg, team="acme-team", purpose_tags=["billing"])

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/metadata", json={"capability": "Helps with billing."}
    )

    assert response.status_code == 200
    assert len(fake_cabal.graduate_calls) == 1
    assert fake_cabal.graduate_calls[0]["name"] == AGENT_NAME


def test_patch_metadata_directory_sync_failure_does_not_change_response_or_status(
    session_factory, config, monkeypatch, caplog
):
    """A raising cabal client (switchboard down) must not alter the PATCH's
    own success response in any way — the card commit already succeeded by
    the time the best-effort tail runs, so the resync failure is only ever
    logged."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal(graduate_error="switchboard unreachable")
    client = _make_client(session_factory, cfg, cabal=fake_cabal)
    _seed_agent(session_factory, cfg, team="acme-team", purpose_tags=["billing"])

    with caplog.at_level("WARNING"):
        response = client.patch(
            f"/api/agents/{AGENT_NAME}/metadata", json={"capability": "Helps with billing."}
        )

    assert response.status_code == 200
    assert response.json() == {
        "team": "acme-team",
        "purpose_tags": ["billing"],
        "capability": "Helps with billing.",
        "group": "shared",
        "sha": response.json()["sha"],
    }
    assert "directory sync failed" in caplog.text

    # The metadata edit itself is durably stored regardless of the sync failure.
    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["capability"] == "Helps with billing."


# --- POST /api/agents/{name}/metadata/regenerate ----------------------------------


def test_regenerate_metadata_unknown_agent_404s(session_factory, config):
    client = _make_client(session_factory, config)
    response = client.post(f"/api/agents/{AGENT_NAME}/metadata/regenerate")
    assert response.status_code == 404


def test_regenerate_metadata_not_ready_409s(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=AgentStatus.FAILED)

    response = client.post(f"/api/agents/{AGENT_NAME}/metadata/regenerate")

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for directory changes yet"}


def test_regenerate_metadata_uses_model_client_stores_and_commits(session_factory, config):
    github = FakeGitHub()
    fake_name = FakeName(capability="Freshly regenerated sentence.")
    client = _make_client(session_factory, config, github=github, name=fake_name)
    _seed_agent(
        session_factory, config, team="acme-team", purpose_tags=["billing"],
        capability="Stale sentence.",
    )

    response = client.post(f"/api/agents/{AGENT_NAME}/metadata/regenerate")

    assert response.status_code == 200
    assert response.json() == {
        "team": "acme-team",
        "purpose_tags": ["billing"],
        "capability": "Freshly regenerated sentence.",
        "sha": response.json()["sha"],
    }
    assert response.json()["sha"]
    assert github.pushes[-1][1] == "kobo: directory details updated"

    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["capability"] == "Freshly regenerated sentence."


def test_regenerate_metadata_uses_current_skills_from_catalog(session_factory, config):
    storage = FakeStorage()
    seed_default_personas(storage, bucket=PERSONAS_BUCKET)
    storage.write_bytes(
        SKILLS_BUCKET,
        "shared/word-games/SKILL.md",
        (
            b"---\nname: word-games\ndescription: Plays word games with the "
            b"user.\n---\nBody.\n"
        ),
        metadata={"source-sha": "seedsha"},
        content_type="text/markdown",
    )
    storage.write_bytes(
        SKILLS_BUCKET,
        "index.json",
        json.dumps(
            {
                "entries": [
                    {
                        "id": "word-games",
                        "name": "Word games",
                        "description": "Plays word games with the user.",
                        "team": "shared",
                        "tags": ["fun"],
                        "sha": "seedsha",
                        "builtin": False,
                    }
                ]
            }
        ).encode(),
        content_type="application/json",
    )
    fake_name = FakeName(capability="Plays games and helps out.")
    client = _make_client(session_factory, config, name=fake_name, storage=storage)
    _seed_agent(
        session_factory, config,
        skills=[{"name": "word-games", "team": "shared", "sha": "seedsha"}],
    )
    # Warm the FTS catalog the same way the create route's own
    # _maybe_refresh_catalogs TTL hook does — regenerate reads it directly
    # via skill_descriptions_for_agent, it doesn't refresh it itself.
    session = session_factory()
    ingest_indexes(session.get_bind(), storage, {"skill": SKILLS_BUCKET})
    session.close()

    response = client.post(f"/api/agents/{AGENT_NAME}/metadata/regenerate")

    assert response.status_code == 200
    assert fake_name.capability_calls[0][1] == ["Plays word games with the user."]


def test_regenerate_metadata_card_commit_failure_returns_502_and_db_not_committed(
    session_factory, config
):
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(session_factory, config, capability="Stale sentence.")

    response = client.post(f"/api/agents/{AGENT_NAME}/metadata/regenerate")

    assert response.status_code == 502
    assert response.json() == {
        "error": "couldn't update the directory details. Try again."
    }

    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["capability"] == "Stale sentence."


# --- Regenerate: best-effort directory tail (colleagues switchboard plan, Task 2) --


def test_regenerate_metadata_success_best_effort_syncs_the_directory(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal()
    fake_name = FakeName(capability="Freshly regenerated sentence.")
    client = _make_client(session_factory, cfg, name=fake_name, cabal=fake_cabal)
    _seed_agent(session_factory, cfg, capability="Stale sentence.")

    response = client.post(f"/api/agents/{AGENT_NAME}/metadata/regenerate")

    assert response.status_code == 200
    assert len(fake_cabal.graduate_calls) == 1
    assert fake_cabal.graduate_calls[0]["name"] == AGENT_NAME


def test_regenerate_metadata_directory_sync_failure_does_not_change_response_or_status(
    session_factory, config, monkeypatch, caplog
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal(graduate_error="switchboard unreachable")
    fake_name = FakeName(capability="Freshly regenerated sentence.")
    client = _make_client(session_factory, cfg, name=fake_name, cabal=fake_cabal)
    _seed_agent(session_factory, cfg, capability="Stale sentence.")

    with caplog.at_level("WARNING"):
        response = client.post(f"/api/agents/{AGENT_NAME}/metadata/regenerate")

    assert response.status_code == 200
    assert response.json()["capability"] == "Freshly regenerated sentence."
    assert "directory sync failed" in caplog.text


# --- Regenerate: roster refresh lane (fast-lane + discovery arc, Task 6) ----
#
# `regenerate_agent_metadata` is the other place (besides HelloStep's own
# tail) capability actually DISTILLS -- `generate_capability` runs above,
# not a caller-typed string. Both refresh halves spec 3.3 names should fire:
# the room's regenerated view reaches affected colleagues, and the agent's
# own self.md is redelivered to itself.


def test_regenerate_metadata_redelivers_roster_and_self_md_without_cross_group_delivery(
    session_factory, config, monkeypatch
):
    """The load-bearing negative test, at the regenerate route: a room with
    agents in two groups must not cross-deliver."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal(
        relay_result={"status": {"state": "completed", "message": {"parts": [{"text": "ok"}]}}}
    )
    fake_name = FakeName(capability="Freshly regenerated sentence.")
    client = _make_client(session_factory, cfg, name=fake_name, cabal=fake_cabal)
    _seed_agent(session_factory, cfg, capability="Stale sentence.", group_name="acme-acl")
    _seed_colleague(session_factory, "same-group", group_name="acme-acl")
    _seed_colleague(session_factory, "other-group", group_name="orbit-acl")

    response = client.post(f"/api/agents/{AGENT_NAME}/metadata/regenerate")

    assert response.status_code == 200
    filenames_by_target: dict[str, set[str]] = {}
    for call in fake_cabal.relay_calls:
        filenames_by_target.setdefault(call["name"], set()).add(
            call["task_envelope"]["metadata"]["filename"]
        )
    assert filenames_by_target.get("same-group") == {"colleagues.md"}
    assert "other-group" not in filenames_by_target
    assert filenames_by_target.get(AGENT_NAME) == {"self.md"}


def test_regenerate_metadata_roster_refresh_failure_does_not_change_response_or_status(
    session_factory, config, monkeypatch, caplog
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal(relay_error="switchboard unreachable")
    fake_name = FakeName(capability="Freshly regenerated sentence.")
    client = _make_client(session_factory, cfg, name=fake_name, cabal=fake_cabal)
    _seed_agent(session_factory, cfg, capability="Stale sentence.", group_name="acme-acl")
    _seed_colleague(session_factory, "same-group", group_name="acme-acl")

    with caplog.at_level("WARNING"):
        response = client.post(f"/api/agents/{AGENT_NAME}/metadata/regenerate")

    assert response.status_code == 200
    assert response.json()["capability"] == "Freshly regenerated sentence."
    assert "roster refresh relay failed" in caplog.text


def test_regenerate_metadata_disabled_switchboard_never_attempts_a_roster_refresh(
    session_factory, config
):
    fake_cabal = FakeCabal()
    fake_name = FakeName(capability="Freshly regenerated sentence.")
    client = _make_client(session_factory, config, name=fake_name, cabal=fake_cabal)
    _seed_agent(session_factory, config, capability="Stale sentence.", group_name="acme-acl")
    _seed_colleague(session_factory, "same-group", group_name="acme-acl")

    response = client.post(f"/api/agents/{AGENT_NAME}/metadata/regenerate")

    assert response.status_code == 200
    assert fake_cabal.relay_calls == []


def test_regenerate_metadata_persona_lookup_failure_returns_502(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    # Overwrite the persona row to reference a persona that isn't in the
    # (bare) storage the client fixture was built with.
    session = session_factory()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    agent.persona = "ghost-persona"
    session.commit()
    session.close()

    response = client.post(f"/api/agents/{AGENT_NAME}/metadata/regenerate")

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't read this agent's persona. Try again."}


# --- group: the ACL namespace an agent sits under --------------------------------


def _grouped_config(config):
    from kobo_api.config import GroupChoice

    return config.model_copy(
        update={
            "groups": [
                GroupChoice(name="marketing", okta_group="okta-marketing", kind="non-engineering"),
                GroupChoice(name="sre", okta_group="okta-sre"),
            ]
        }
    )


def _group_on_row(session_factory):
    session = session_factory()
    try:
        return session.query(Agent).filter_by(name=AGENT_NAME).one().group_name
    finally:
        session.close()


def test_patch_metadata_moves_the_agent_to_a_configured_group(session_factory, config):
    cfg = _grouped_config(config)
    github = FakeGitHub()
    client = _make_client(session_factory, cfg, github=github)
    _seed_agent(session_factory, cfg, group_name=None)

    response = client.patch(f"/api/agents/{AGENT_NAME}/metadata", json={"group": "marketing"})

    assert response.status_code == 200
    assert response.json()["group"] == "marketing"
    assert _group_on_row(session_factory) == "marketing"
    # The group is the room's own filing, not something the bundle carries:
    # no card commit for a move alone.
    assert github.pushes == []


def test_patch_metadata_moves_the_agent_back_to_the_commons(session_factory, config):
    cfg = _grouped_config(config)
    client = _make_client(session_factory, cfg)
    _seed_agent(session_factory, cfg, group_name="marketing")

    response = client.patch(f"/api/agents/{AGENT_NAME}/metadata", json={"group": "shared"})

    assert response.status_code == 200
    assert response.json()["group"] == "shared"
    # The commons is a real namespace with a real name. Storing "no group"
    # here made the agent group-less instead: visible to its owner, its
    # grantees and admins, and to nobody the move was meant to reach.
    assert _group_on_row(session_factory) == "shared"


def _seed_neighbor(session_factory, config, *, name, group_name):
    session = session_factory()
    session.add(
        Agent(
            name=name,
            persona=PERSONA,
            model_id=config.models[0].id,
            status=AgentStatus.READY,
            repo_full_name=f"acme/agent-{name}",
            team="shared",
            group_name=group_name,
        )
    )
    session.commit()
    session.close()


def test_a_group_move_tells_the_switchboard_who_may_call_whom(
    session_factory, config, monkeypatch
):
    """Agents in one group may call each other. A move changes the group, so
    it changes that set, and the switchboard only knows what the room pushes.
    Before this, a moved agent kept its old neighbors and had none of its new
    ones until some unrelated event happened to trigger a sync."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _grouped_config(_enabled_config(config))
    fake_cabal = FakeCabal()
    client = _make_client(session_factory, cfg, cabal=fake_cabal)
    _seed_agent(session_factory, cfg, group_name="marketing")
    _seed_neighbor(session_factory, cfg, name="otto", group_name="sre")

    response = client.patch(f"/api/agents/{AGENT_NAME}/metadata", json={"group": "sre"})

    assert response.status_code == 200
    assert fake_cabal.put_dispatch_pairs_calls, "the move pushed nothing"
    pushed = fake_cabal.put_dispatch_pairs_calls[-1]
    assert (f"agent:{AGENT_NAME}", "otto") in pushed
    assert ("agent:otto", AGENT_NAME) in pushed


def test_a_group_move_survives_a_switchboard_that_is_down(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _grouped_config(_enabled_config(config))
    fake_cabal = FakeCabal(put_dispatch_pairs_error="switchboard unreachable")
    client = _make_client(session_factory, cfg, cabal=fake_cabal)
    _seed_agent(session_factory, cfg, group_name="marketing")

    response = client.patch(f"/api/agents/{AGENT_NAME}/metadata", json={"group": "sre"})

    assert response.status_code == 200
    assert _group_on_row(session_factory) == "sre"


def test_a_change_that_is_not_a_move_pushes_no_pairs(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _grouped_config(_enabled_config(config))
    fake_cabal = FakeCabal()
    client = _make_client(session_factory, cfg, cabal=fake_cabal)
    _seed_agent(session_factory, cfg, group_name="sre")

    response = client.patch(f"/api/agents/{AGENT_NAME}/metadata", json={"group": "sre"})

    assert response.status_code == 200
    assert fake_cabal.put_dispatch_pairs_calls == []


def test_patch_metadata_refuses_a_group_the_room_does_not_know(session_factory, config):
    cfg = _grouped_config(config)
    client = _make_client(session_factory, cfg)
    _seed_agent(session_factory, cfg, group_name="sre")

    response = client.patch(f"/api/agents/{AGENT_NAME}/metadata", json={"group": "finance"})

    assert response.status_code == 422
    assert "finance" in response.json()["error"]
    assert _group_on_row(session_factory) == "sre"



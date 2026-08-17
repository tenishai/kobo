"""Tests for the skills HTTP routes (Task 2): list an agent's carried
skills (with provenance), pull one from the org catalog, remove either
class, and read/write a local guardrail-authored one — all against the
fakes (`tests/fakes.py`), no real git/gcs involved.

Same harness shape as test_api_knowledge.py (own `session_factory`/`config`
fixtures, a `TestClient` built from `create_app`), trimmed to what these
routes actually need: no pipeline/provisioning involved here, so agents are
seeded directly at whatever status a test wants, already carrying a
`repo_full_name` (these routes need one to call into `kobo_api.skills` at
all) and whatever `skills` JSON the test wants on the row.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import ClientError, Clients
from kobo_api.config import Config
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus, Base
from tests.fakes import (
    FAKE_LOCAL_HEAD_DEFAULT,
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeStorage,
)

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
REPO = "acme/agent-clio"
SKILLS_BUCKET = "skills-bucket"

SAMPLE_TEAM = "shared"
SAMPLE_SKILL_ID = "word-games"
SAMPLE_SKILL_SHA = "seedsha"

SAMPLE_CONTENT = "---\nname: notes\ndescription: A local guardrail skill.\n---\nBody.\n"

# 40 hex chars — matches FakeKubectl's default deploy_image tag; unused by
# these routes directly, but SHA is still the meaningful value save/remove/
# add return (github.local_head), so tests assert against it by name rather
# than the literal, mirroring test_api_knowledge.py's own SHA alias.
SHA = FAKE_LOCAL_HEAD_DEFAULT


def _seed_catalog_skill(
    storage: FakeStorage, team: str, skill_id: str, *, source_sha: str = SAMPLE_SKILL_SHA
) -> None:
    storage.write_bytes(
        SKILLS_BUCKET,
        f"{team}/{skill_id}/SKILL.md",
        f"---\nname: {skill_id}\ndescription: A sample skill.\n---\nBody.\n".encode(),
        metadata={"source-sha": source_sha},
        content_type="text/markdown",
    )


class _CapturingGitHub(FakeGitHub):
    """Same snapshot-on-commit_push trick as test_skills.py's own
    `_CapturingGitHub`, replicated locally (rather than imported cross-file,
    which would couple this test module to another test module's internals)
    — captures `bundle/skills/**/*.md` content the moment `commit_push` is
    called, the only window in which the caller's cloned workdir still
    exists before `kobo_api.skills` discards it.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured_skills: dict[str, str] = {}

    def commit_push(self, workdir: Path, message: str) -> bool:
        skills_dir = workdir / "bundle" / "skills"
        if skills_dir.is_dir():
            self.captured_skills = {
                str(path.relative_to(skills_dir)): path.read_text()
                for path in skills_dir.rglob("*")
                if path.is_file()
            }
        return super().commit_push(workdir, message)


class _SkillSeedingGitHub(_CapturingGitHub):
    """A github fake whose clone() also materializes an existing
    bundle/skills/<id>/SKILL.md tree — the local-repo state the delete/read
    tests need already in place before their operation runs. Mirrors
    test_skills.py's own `_SkillSeedingGitHub`."""

    def __init__(self, *, local_skills: dict[str, str] | None = None, **kwargs):
        super().__init__(**kwargs)
        self._local_skills = local_skills or {}

    def clone(self, repo: str, dest: Path) -> None:
        super().clone(repo, dest)
        for rel_path, content in self._local_skills.items():
            target = dest / "bundle" / "skills" / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)


class _RaisingGitHub(FakeGitHub):
    """A GitHubClient whose `clone` raises `ClientError` — the 502 guard's
    fixture. `clone` is the first call every skills.py operation makes, so
    overriding just it covers every mutating route's github-side 502 path.
    """

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
    return Config(workdir=tmp_path / "work", skills_bucket=SKILLS_BUCKET)


def _make_client(session_factory, config, *, github=None, storage=None):
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
                storage=storage if storage is not None else FakeStorage(),
            ),
        )
    )


def _seed_agent(
    session_factory,
    config,
    *,
    status=AgentStatus.READY,
    repo_full_name=REPO,
    skills=None,
):
    session = session_factory()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config.models[0].id,
            status=status,
            repo_full_name=repo_full_name,
            skills=skills if skills is not None else [],
        )
    )
    session.commit()
    session.close()


def _load_agent_skills(session_factory) -> list[dict]:
    session = session_factory()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    skills = agent.skills
    session.close()
    return skills


NON_READY_STATUSES = [AgentStatus.PENDING, AgentStatus.PROVISIONING, AgentStatus.FAILED]


# --- GET /api/agents/{name}/skills ------------------------------------------


def test_list_skills_empty(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/skills")

    assert response.status_code == 200
    assert response.json() == {"skills": []}


def test_list_skills_renders_legacy_entry_without_source_as_catalog(session_factory, config):
    """An assembly-time pick carries no "source" key at all — it must still
    render as a non-editable catalog entry, not crash or show as local."""
    client = _make_client(session_factory, config)
    _seed_agent(
        session_factory,
        config,
        skills=[{"name": SAMPLE_SKILL_ID, "team": SAMPLE_TEAM, "sha": SAMPLE_SKILL_SHA}],
    )

    response = client.get(f"/api/agents/{AGENT_NAME}/skills")

    assert response.status_code == 200
    assert response.json() == {
        "skills": [
            {
                "name": SAMPLE_SKILL_ID,
                "team": SAMPLE_TEAM,
                "source": "catalog",
                "sha": SAMPLE_SKILL_SHA,
                "editable": False,
            }
        ]
    }


def test_list_skills_renders_explicit_catalog_and_local_entries(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(
        session_factory,
        config,
        skills=[
            {"name": "a", "team": "t", "sha": "s1", "source": "catalog"},
            {"name": "b", "source": "local"},
        ],
    )

    response = client.get(f"/api/agents/{AGENT_NAME}/skills")

    assert response.status_code == 200
    assert response.json() == {
        "skills": [
            {"name": "a", "team": "t", "source": "catalog", "sha": "s1", "editable": False},
            {"name": "b", "team": None, "source": "local", "sha": None, "editable": True},
        ]
    }


def test_list_skills_unknown_agent_returns_404(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/skills")

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_list_skills_non_ready_agent_returns_409(session_factory, config, status):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status)

    response = client.get(f"/api/agents/{AGENT_NAME}/skills")

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for memory changes"}


# --- POST /api/agents/{name}/skills/catalog ---------------------------------


def test_add_catalog_skill_pins_the_sha_served_now(session_factory, config):
    """The sha recorded on the row (and returned by add_catalog_skill as its
    source_sha) must be the catalog's CURRENT content pin — distinct from
    the pushed commit sha (github.local_head) the response's "sha" carries.
    A bug that conflated the two would still pass a same-value check, so
    this seeds a source-sha that's deliberately not the commit sha to make
    the two impossible to confuse."""
    storage = FakeStorage()
    _seed_catalog_skill(storage, SAMPLE_TEAM, SAMPLE_SKILL_ID, source_sha="livesha001")
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github, storage=storage)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/skills/catalog",
        json={"name": SAMPLE_SKILL_ID, "team": SAMPLE_TEAM},
    )

    assert response.status_code == 200
    assert response.json() == {"sha": SHA}
    assert SHA != "livesha001"
    assert f"{SAMPLE_SKILL_ID}/SKILL.md" in github.captured_skills
    assert github.pushes[-1][1] == f"kobo: add skill {SAMPLE_SKILL_ID}"

    assert _load_agent_skills(session_factory) == [
        {
            "name": SAMPLE_SKILL_ID,
            "team": SAMPLE_TEAM,
            "sha": "livesha001",
            "source": "catalog",
        }
    ]


def test_add_catalog_skill_gone_from_catalog_returns_404(session_factory, config):
    storage = FakeStorage(buckets={SKILLS_BUCKET: {}})
    github = FakeGitHub()
    client = _make_client(session_factory, config, github=github, storage=storage)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/skills/catalog", json={"name": "gone", "team": SAMPLE_TEAM}
    )

    assert response.status_code == 404
    assert "skill no longer in the catalog" in response.json()["error"]
    assert github.pushes == []
    assert _load_agent_skills(session_factory) == []


def test_add_catalog_skill_invalid_id_returns_422(session_factory, config):
    storage = FakeStorage()
    github = FakeGitHub()
    client = _make_client(session_factory, config, github=github, storage=storage)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/skills/catalog",
        json={"name": "Not-Valid", "team": SAMPLE_TEAM},
    )

    assert response.status_code == 422
    assert "invalid skill id" in response.json()["error"]
    assert github.pushes == []


def test_add_catalog_skill_collision_with_local_entry_returns_409(session_factory, config):
    storage = FakeStorage()
    _seed_catalog_skill(storage, SAMPLE_TEAM, SAMPLE_SKILL_ID)
    github = FakeGitHub()
    client = _make_client(session_factory, config, github=github, storage=storage)
    _seed_agent(session_factory, config, skills=[{"name": SAMPLE_SKILL_ID, "source": "local"}])

    response = client.post(
        f"/api/agents/{AGENT_NAME}/skills/catalog",
        json={"name": SAMPLE_SKILL_ID, "team": SAMPLE_TEAM},
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": f"the agent already carries a skill named {SAMPLE_SKILL_ID!r}"
    }
    assert github.cloned == []
    assert github.pushes == []


def test_add_catalog_skill_collision_with_catalog_entry_returns_409(session_factory, config):
    storage = FakeStorage()
    _seed_catalog_skill(storage, SAMPLE_TEAM, SAMPLE_SKILL_ID)
    github = FakeGitHub()
    client = _make_client(session_factory, config, github=github, storage=storage)
    _seed_agent(
        session_factory,
        config,
        skills=[
            {"name": SAMPLE_SKILL_ID, "team": SAMPLE_TEAM, "sha": "old", "source": "catalog"}
        ],
    )

    response = client.post(
        f"/api/agents/{AGENT_NAME}/skills/catalog",
        json={"name": SAMPLE_SKILL_ID, "team": SAMPLE_TEAM},
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": f"the agent already carries a skill named {SAMPLE_SKILL_ID!r}"
    }
    assert github.cloned == []


def test_add_catalog_skill_unknown_agent_returns_404(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/skills/catalog",
        json={"name": SAMPLE_SKILL_ID, "team": SAMPLE_TEAM},
    )

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_add_catalog_skill_non_ready_agent_returns_409(session_factory, config, status):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/skills/catalog",
        json={"name": SAMPLE_SKILL_ID, "team": SAMPLE_TEAM},
    )

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for memory changes"}


def test_add_catalog_skill_client_error_returns_502(session_factory, config):
    storage = FakeStorage()
    _seed_catalog_skill(storage, SAMPLE_TEAM, SAMPLE_SKILL_ID)
    client = _make_client(session_factory, config, github=_RaisingGitHub(), storage=storage)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/skills/catalog",
        json={"name": SAMPLE_SKILL_ID, "team": SAMPLE_TEAM},
    )

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't reach your agent's files. Try again."}


# --- DELETE /api/agents/{name}/skills/{skill_id} ----------------------------


def test_delete_skill_removes_catalog_entry_and_updates_json(session_factory, config):
    github = _SkillSeedingGitHub(local_skills={f"{SAMPLE_SKILL_ID}/SKILL.md": "content"})
    client = _make_client(session_factory, config, github=github)
    _seed_agent(
        session_factory,
        config,
        skills=[
            {"name": SAMPLE_SKILL_ID, "team": SAMPLE_TEAM, "sha": "s", "source": "catalog"},
            {"name": "keep", "source": "local"},
        ],
    )

    response = client.delete(f"/api/agents/{AGENT_NAME}/skills/{SAMPLE_SKILL_ID}")

    assert response.status_code == 200
    assert response.json() == {"sha": SHA}
    assert github.captured_skills == {}
    assert github.pushes[-1][1] == f"kobo: remove skill {SAMPLE_SKILL_ID}"
    assert _load_agent_skills(session_factory) == [{"name": "keep", "source": "local"}]


def test_delete_skill_removes_local_entry_and_updates_json(session_factory, config):
    github = _SkillSeedingGitHub(local_skills={"notes/SKILL.md": SAMPLE_CONTENT})
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config, skills=[{"name": "notes", "source": "local"}])

    response = client.delete(f"/api/agents/{AGENT_NAME}/skills/notes")

    assert response.status_code == 200
    assert response.json() == {"sha": SHA}
    assert github.captured_skills == {}
    assert _load_agent_skills(session_factory) == []


def test_delete_skill_not_carried_returns_404(session_factory, config):
    github = _SkillSeedingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.delete(f"/api/agents/{AGENT_NAME}/skills/ghost")

    assert response.status_code == 404
    assert response.json() == {"error": "skill not found: ghost"}
    assert github.pushes == []


def test_delete_skill_unknown_agent_returns_404(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.delete(f"/api/agents/{AGENT_NAME}/skills/ghost")

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_delete_skill_non_ready_agent_returns_409(session_factory, config, status):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status)

    response = client.delete(f"/api/agents/{AGENT_NAME}/skills/ghost")

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for memory changes"}


def test_delete_skill_client_error_returns_502(session_factory, config):
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(session_factory, config, skills=[{"name": "notes", "source": "local"}])

    response = client.delete(f"/api/agents/{AGENT_NAME}/skills/notes")

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't reach your agent's files. Try again."}


# --- GET /api/agents/{name}/skills/local/{skill_id} -------------------------


def test_get_local_skill_happy_path(session_factory, config):
    github = _SkillSeedingGitHub(local_skills={"notes/SKILL.md": SAMPLE_CONTENT})
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config, skills=[{"name": "notes", "source": "local"}])

    response = client.get(f"/api/agents/{AGENT_NAME}/skills/local/notes")

    assert response.status_code == 200
    assert response.json() == {"name": "notes", "content": SAMPLE_CONTENT}


def test_get_local_skill_catalog_class_returns_404(session_factory, config):
    github = FakeGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(
        session_factory,
        config,
        skills=[
            {"name": SAMPLE_SKILL_ID, "team": SAMPLE_TEAM, "sha": "s", "source": "catalog"}
        ],
    )

    response = client.get(f"/api/agents/{AGENT_NAME}/skills/local/{SAMPLE_SKILL_ID}")

    assert response.status_code == 404
    assert "error" in response.json()
    # Content is never served for a catalog-class id — checked against the
    # row before any repo work, so this never even clones.
    assert github.cloned == []


def test_get_local_skill_legacy_entry_without_source_returns_404(session_factory, config):
    github = FakeGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(
        session_factory,
        config,
        skills=[{"name": SAMPLE_SKILL_ID, "team": SAMPLE_TEAM, "sha": "s"}],
    )

    response = client.get(f"/api/agents/{AGENT_NAME}/skills/local/{SAMPLE_SKILL_ID}")

    assert response.status_code == 404
    assert github.cloned == []


def test_get_local_skill_missing_returns_404(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/skills/local/ghost")

    assert response.status_code == 404
    assert "error" in response.json()


def test_get_local_skill_unknown_agent_returns_404(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/skills/local/notes")

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_get_local_skill_non_ready_agent_returns_409(session_factory, config, status):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status)

    response = client.get(f"/api/agents/{AGENT_NAME}/skills/local/notes")

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for memory changes"}


def test_get_local_skill_client_error_returns_502(session_factory, config):
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(session_factory, config, skills=[{"name": "notes", "source": "local"}])

    response = client.get(f"/api/agents/{AGENT_NAME}/skills/local/notes")

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't reach your agent's files. Try again."}


# --- PUT /api/agents/{name}/skills/local/{skill_id} -------------------------


def test_put_local_skill_creates_commits_and_updates_json(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/skills/local/notes", json={"content": SAMPLE_CONTENT}
    )

    assert response.status_code == 200
    assert response.json() == {"sha": SHA}
    assert github.captured_skills == {"notes/SKILL.md": SAMPLE_CONTENT}
    assert github.pushes[-1][1] == "kobo: update skill notes"
    assert _load_agent_skills(session_factory) == [{"name": "notes", "source": "local"}]


def test_put_local_skill_overwrites_existing_local_entry(session_factory, config):
    github = _SkillSeedingGitHub(local_skills={"notes/SKILL.md": "old content"})
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config, skills=[{"name": "notes", "source": "local"}])
    replacement = "---\nname: notes\ndescription: Updated.\n---\nNew body.\n"

    response = client.put(
        f"/api/agents/{AGENT_NAME}/skills/local/notes", json={"content": replacement}
    )

    assert response.status_code == 200
    assert github.captured_skills == {"notes/SKILL.md": replacement}
    # Still exactly one entry for "notes" — not duplicated.
    assert _load_agent_skills(session_factory) == [{"name": "notes", "source": "local"}]


def test_put_local_skill_catalog_class_collision_returns_409(session_factory, config):
    github = FakeGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(
        session_factory,
        config,
        skills=[
            {"name": SAMPLE_SKILL_ID, "team": SAMPLE_TEAM, "sha": "s", "source": "catalog"}
        ],
    )

    response = client.put(
        f"/api/agents/{AGENT_NAME}/skills/local/{SAMPLE_SKILL_ID}",
        json={"content": SAMPLE_CONTENT},
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": f"the agent already carries a skill named {SAMPLE_SKILL_ID!r}"
    }
    assert github.cloned == []


def test_put_local_skill_legacy_entry_without_source_collision_returns_409(
    session_factory, config
):
    github = FakeGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(
        session_factory,
        config,
        skills=[{"name": SAMPLE_SKILL_ID, "team": SAMPLE_TEAM, "sha": "s"}],
    )

    response = client.put(
        f"/api/agents/{AGENT_NAME}/skills/local/{SAMPLE_SKILL_ID}",
        json={"content": SAMPLE_CONTENT},
    )

    assert response.status_code == 409
    assert github.cloned == []


def test_put_local_skill_empty_content_returns_422(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/skills/local/notes", json={"content": ""}
    )

    assert response.status_code == 422
    assert "empty" in response.json()["error"]
    assert github.pushes == []


def test_put_local_skill_missing_frontmatter_returns_422(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/skills/local/notes",
        json={"content": "No frontmatter here.\n"},
    )

    assert response.status_code == 422
    assert "frontmatter" in response.json()["error"]
    assert github.pushes == []


def test_put_local_skill_invalid_id_returns_422(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/skills/local/Not-Valid", json={"content": SAMPLE_CONTENT}
    )

    assert response.status_code == 422
    assert "invalid skill id" in response.json()["error"]
    assert github.pushes == []


def test_put_local_skill_unknown_agent_returns_404(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/skills/local/notes", json={"content": SAMPLE_CONTENT}
    )

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_put_local_skill_non_ready_agent_returns_409(session_factory, config, status):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/skills/local/notes", json={"content": SAMPLE_CONTENT}
    )

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for memory changes"}


def test_put_local_skill_client_error_returns_502(session_factory, config):
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(session_factory, config)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/skills/local/notes", json={"content": SAMPLE_CONTENT}
    )

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't reach your agent's files. Try again."}

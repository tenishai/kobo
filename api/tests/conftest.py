import json
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import kobo_api.skills as _skills_module
from kobo_api.clients import Clients
from kobo_api.config import Config
from kobo_api.db import init_db
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus, Base
from kobo_api.personas import seed_default_personas
from tests.fakes import (
    FakeAgent,
    FakeBab,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeStorage,
)


@pytest.fixture(autouse=True)
def _clear_repo_skills_cache():
    """`kobo_api.skills.list_repo_skills`'s TTL cache (review fix B) is a
    plain module-level dict, so it persists across every test in the whole
    suite unless something clears it. Many test modules reuse the same
    literal repo name (e.g. "acme/agent-clio") across unrelated test
    functions with differently-shaped fake repos, so without this, a cache
    entry one test's clone leaves behind can silently answer a later
    test's call instead of that test's own fake ever being asked."""
    _skills_module._repo_skills_cache.clear()
    yield
    _skills_module._repo_skills_cache.clear()

# Bucket-catalog fixture rework (Task 5, done once here): every test module
# that needs a wired catalog can request `fake_storage` (seeded with both
# catalogs) and `config` (bucket names set to match) rather than building its
# own from scratch.
SKILLS_BUCKET = "skills-bucket"
PERSONAS_BUCKET = "personas-bucket"

# The one sample skill every fixture-seeded storage carries: `shared/
# word-games/`, whose SKILL.md's `source-sha` custom metadata ("seedsha") is
# the pin `kobo_api.steps._stage_skills` compares an agent's recorded sha
# against — tests that want to exercise sha-drift/vanished-skill paths do so
# by mutating or removing this fixture's own bucket state.
SAMPLE_SKILL_TEAM = "shared"
SAMPLE_SKILL_ID = "word-games"
SAMPLE_SKILL_SHA = "seedsha"

SKILL_INDEX_ENTRIES = [
    {
        "id": SAMPLE_SKILL_ID,
        "name": "Word games",
        "description": "Plays word games with the user.",
        "team": SAMPLE_SKILL_TEAM,
        "tags": ["fun"],
        "sha": SAMPLE_SKILL_SHA,
        "builtin": False,
    },
]


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def fake_storage():
    """A `FakeStorage` pre-seeded with both catalogs, mirroring what a real
    bootstrap run leaves in a fresh org's buckets: the two packaged personas
    (via `seed_default_personas`, the same seeding the bootstrap CLI runs
    against a real bucket) plus one sample skill (`shared/word-games/`) and
    both catalogs' `index.json`.
    """
    storage = FakeStorage()
    seed_default_personas(storage, bucket=PERSONAS_BUCKET)

    storage.write_bytes(
        SKILLS_BUCKET,
        f"{SAMPLE_SKILL_TEAM}/{SAMPLE_SKILL_ID}/SKILL.md",
        (
            f"---\nname: {SAMPLE_SKILL_ID}\ndescription: Plays word games "
            "with the user.\n---\nLet's play a word game! Pick a category "
            "and I'll start.\n"
        ).encode(),
        metadata={"source-sha": SAMPLE_SKILL_SHA},
        content_type="text/markdown",
    )
    storage.write_bytes(
        SKILLS_BUCKET,
        "index.json",
        json.dumps({"entries": SKILL_INDEX_ENTRIES}).encode(),
        content_type="application/json",
    )
    return storage


@pytest.fixture
def config(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        skills_bucket=SKILLS_BUCKET,
        personas_bucket=PERSONAS_BUCKET,
        # The group road, as a Workspace org runs it; the no-group road has
        # its own tests (test_steps / test_destroy, "without a model group").
        model_group="models@example.com",
    )


@pytest.fixture
def client(session_factory, config):
    return TestClient(create_app(session_factory, config=config))


@pytest.fixture
def a2a_room(session_factory, tmp_path, monkeypatch):
    """A room with the switchboard AND the permission service wired (kobo#310),
    an operator signed in through IAP, and two READY agents in different groups:
    `asker` (growth) and `oracle` (security). Shared by the integrity test (the
    pairs push) and the wire-shape test (the request's shape), so the room is
    built once, here."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    config = Config(workdir=tmp_path / "work", iap_audience="test-audience", operators=["ops@example.com"],
                    admin_panel=True, bab_url="http://bab.bab.svc:8080", cabal_url="http://cabal.internal",
                    agent_endpoint_template="http://agent-{name}.default.svc.cluster.local")
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": "test-key-1", "use": "sig", "alg": "ES256"})
    now = int(time.time())
    token = jwt.encode({"aud": "test-audience", "iss": IAP_ISSUER, "iat": now, "exp": now + 3600,
                        "sub": "accounts.google.com:1234567890", "email": "ops@example.com"},
                       private_key, algorithm="ES256", headers={"kid": "test-key-1"})
    bab, cabal = FakeBab(integrations=[]), FakeCabal()
    clients = Clients(github=FakeGitHub(), gojira=FakeGojira(), cloud=FakeCloud(), agent=FakeAgent(),
                      kubectl=FakeKubectl(), cabal=cabal)
    clients.bab = bab
    app = create_app(session_factory, config=config, clients=clients, iap_jwks_fetcher=lambda: {"keys": [jwk]})
    with session_factory() as s:
        for name, group in (("asker", "growth"), ("oracle", "security")):
            s.add(Agent(name=name, persona="helpful-generalist", model_id="vertex_ai/claude-sonnet-4-5",
                        status=AgentStatus.READY, repo_full_name=f"example-org/agent-{name}", group_name=group,
                        created_by="ops@example.com"))
        s.commit()
    return SimpleNamespace(client=TestClient(app), headers={ASSERTION_HEADER: token}, bab=bab, cabal=cabal,
                           session_factory=session_factory)

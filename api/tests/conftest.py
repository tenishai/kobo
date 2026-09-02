import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import kobo_api.skills as _skills_module
from kobo_api.config import Config
from kobo_api.db import init_db
from kobo_api.main import create_app
from kobo_api.models import Base
from kobo_api.personas import seed_default_personas
from tests.fakes import FakeStorage


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
    )


@pytest.fixture
def client(session_factory, config):
    return TestClient(create_app(session_factory, config=config))

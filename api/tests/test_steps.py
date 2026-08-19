"""Tests for kobo_api.steps: RepoStep (persona/skill resolution against the
bucket catalogs, Task 5), OnboardStep, AccessStep.

Each step is exercised directly — construct a `Ctx`, call `step.run(ctx)` —
against the platform-client fakes in tests/fakes.py, the same style
test_engine.py uses for its toy steps but with the real steps. RepoStep's
persona/skill fixtures come from conftest.py's shared `config`/`fake_storage`
(the two packaged personas + one sample skill, `shared/word-games/`, pinned
at `source-sha: "seedsha"`). No real subprocess/network/sleep: the module's
`_sleep` is monkeypatched to a no-op everywhere a poll loop might run more
than a couple of iterations, so the whole file runs in well under a second.
"""

from __future__ import annotations

import json

import pytest

from kobo_api.clients import Clients
from kobo_api.composer import BundleSpec, compose
from kobo_api.models import Agent
from kobo_api.provisioner import Ctx
from kobo_api.steps import (
    ONBOARD_REPROVISION_GRACE_POLLS,
    POLL_INTERVAL_S,
    POLL_TIMEOUT_S,
    AccessStep,
    HelloStep,
    OnboardStep,
    RepoStep,
    StagedSkill,
    _resolve_persona_body,
    _stage_skills,
)
from tests.fakes import (
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeName,
    FakeStorage,
)

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
MODEL_ID = "vertex_ai/claude-sonnet-4-5"

WORD_GAMES_SKILL = {"name": "word-games", "team": "shared", "sha": "seedsha"}


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Every OnboardStep test runs a poll loop; never let it sleep for real."""
    import kobo_api.steps as steps_module

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
    skills: list | None = None,
    repo_full_name: str | None = None,
    gojira_app_id: int | None = None,
    team: str | None = None,
    purpose_tags: str | None = None,
):
    session = session_factory()
    agent = Agent(
        name=name,
        persona=persona,
        model_id=model_id,
        skills=skills or [],
        repo_full_name=repo_full_name,
        gojira_app_id=gojira_app_id,
        team=team,
        purpose_tags=purpose_tags,
    )
    session.add(agent)
    session.commit()
    return session, agent


def _make_ctx(session, agent, config, clients) -> Ctx:
    workdir = config.workdir / agent.name
    workdir.mkdir(parents=True, exist_ok=True)
    return Ctx(agent=agent, session=session, config=config, clients=clients, workdir=workdir)


def _clients(
    *, github=None, gojira=None, cloud=None, storage=None, agent=None, kubectl=None, name=None
) -> Clients:
    return Clients(
        github=github,
        gojira=gojira,
        cloud=cloud,
        agent=agent,
        kubectl=kubectl,
        storage=storage,
        name=name,
    )


# --- _resolve_persona_body ------------------------------------------------------


def test_resolve_persona_body_reads_bucket_and_substitutes_name(
    session_factory, config, fake_storage
):
    session, agent = _make_agent(session_factory, name="sam", persona="helpful-generalist")
    engine = session.get_bind()

    body = _resolve_persona_body(config, fake_storage, engine, agent)

    assert "sam" in body
    assert "{name}" not in body


def test_resolve_persona_body_strips_description_header(session_factory, config, fake_storage):
    # "helpful-generalist" is the only packaged persona seeded into
    # `fake_storage` since Persona builder v2 dropped "blank" from the
    # packaged defaults (seed_default_personas no longer emits it) — it
    # still carries the same `<!-- description: ... -->` header this test
    # exists to prove gets stripped.
    session, agent = _make_agent(session_factory, name="sam", persona="helpful-generalist")
    engine = session.get_bind()

    body = _resolve_persona_body(config, fake_storage, engine, agent)

    assert not body.startswith("<!--")
    assert "description:" not in body


def test_resolve_persona_body_unknown_persona_raises_clear_message(
    session_factory, config, fake_storage
):
    session, agent = _make_agent(session_factory, persona="ghost-persona")
    engine = session.get_bind()

    with pytest.raises(RuntimeError, match="ghost-persona"):
        _resolve_persona_body(config, fake_storage, engine, agent)


def test_resolve_persona_body_survives_braces_in_purpose(session_factory, config):
    """Regression for the `.replace` swap (closes the ledgered P1a brace
    hazard): a persona body containing OTHER literal braces (not just the
    `{name}` placeholder) must survive untouched — a `.format()`-based
    implementation would choke on (or misinterpret) them."""
    storage = FakeStorage()
    storage.write_bytes(
        config.personas_bucket,
        "brace-persona/persona.md",
        b"<!-- description: Handles curly input. -->\n"
        b"You are {name}. Handles {curly} input and echoes back "
        b"{whatever} the user sends, braces and all.\n",
        content_type="text/markdown",
    )
    session, agent = _make_agent(session_factory, name="sam", persona="brace-persona")
    engine = session.get_bind()

    body = _resolve_persona_body(config, storage, engine, agent)

    assert "sam" in body
    assert "{curly}" in body
    assert "{whatever}" in body
    assert "{name}" not in body


# --- _stage_skills ---------------------------------------------------------------


def test_stage_skills_downloads_into_staging_preserving_relative_paths(
    session_factory, config, fake_storage, tmp_path
):
    _session, agent = _make_agent(session_factory, skills=[dict(WORD_GAMES_SKILL)])
    staging = tmp_path / "staging"

    staged = _stage_skills(fake_storage, config, agent, staging)

    assert staged == [StagedSkill(id="word-games", dir=staging / "word-games")]
    assert (staging / "word-games" / "SKILL.md").is_file()


def test_stage_skills_sha_drift_proceeds_and_updates_recorded_pin(
    session_factory, config, fake_storage, tmp_path, caplog
):
    # The catalog's SKILL.md has moved on since this agent picked it: fresh
    # source-sha metadata that no longer matches the recorded pin.
    fake_storage.write_bytes(
        "skills-bucket",
        "shared/word-games/SKILL.md",
        b"---\nname: word-games\ndescription: Plays word games.\n---\nUpdated body.\n",
        metadata={"source-sha": "newsha"},
        content_type="text/markdown",
    )
    session, agent = _make_agent(session_factory, skills=[dict(WORD_GAMES_SKILL)])
    staging = tmp_path / "staging"

    with caplog.at_level("WARNING"):
        staged = _stage_skills(fake_storage, config, agent, staging)

    # Proceeds with the CURRENT content — never fails on drift.
    assert len(staged) == 1
    assert "Updated body." in (staging / "word-games" / "SKILL.md").read_text()
    assert any("word-games" in message for message in caplog.messages)

    # The row's recorded sha is corrected...
    assert agent.skills[0]["sha"] == "newsha"

    # ...and that correction actually persists via a plain commit (the JSON
    # column was reassigned, not mutated in place).
    session.commit()
    reloaded = session_factory().get(Agent, agent.id)
    assert reloaded.skills[0]["sha"] == "newsha"


def test_stage_skills_vanished_skill_raises_exact_message(
    session_factory, config, fake_storage, tmp_path
):
    _session, agent = _make_agent(
        session_factory, skills=[{"name": "ghost-skill", "team": "shared", "sha": "whatever"}]
    )
    staging = tmp_path / "staging"

    with pytest.raises(
        RuntimeError, match=r"^skill no longer in the catalog: shared/ghost-skill$"
    ):
        _stage_skills(fake_storage, config, agent, staging)


def test_stage_skills_no_drift_leaves_row_untouched(session_factory, config, fake_storage, tmp_path):
    _session, agent = _make_agent(session_factory, skills=[dict(WORD_GAMES_SKILL)])
    staging = tmp_path / "staging"

    _stage_skills(fake_storage, config, agent, staging)

    assert agent.skills == [dict(WORD_GAMES_SKILL)]


def test_stage_skills_local_entry_skipped_and_passed_through(
    session_factory, config, fake_storage, tmp_path
):
    # S3: a local-provenance entry (`{"name", "source": "local"}`, no
    # "team") used to KeyError on `entry["team"]` — nothing today reaches
    # this path (compose only runs pre-repo-creation), but the landmine is
    # real. It must be skipped (no download attempted — local skills live in
    # the repo bundle already) and passed through unchanged in whatever
    # `agent.skills` ends up as, alongside a normal catalog pick staged as
    # usual.
    local_entry = {"name": "custom-thing", "source": "local"}
    _session, agent = _make_agent(
        session_factory, skills=[dict(WORD_GAMES_SKILL), dict(local_entry)]
    )
    staging = tmp_path / "staging"

    staged = _stage_skills(fake_storage, config, agent, staging)

    # Only the catalog skill is staged/downloaded.
    assert staged == [StagedSkill(id="word-games", dir=staging / "word-games")]
    assert not (staging / "custom-thing").exists()

    # No drift here, so agent.skills is untouched — but this also proves no
    # KeyError was raised reaching this point.
    assert agent.skills == [dict(WORD_GAMES_SKILL), dict(local_entry)]


def test_stage_skills_malicious_key_escaping_staging_dir_raises(
    session_factory, config, fake_storage, tmp_path
):
    """A bucket object key is an arbitrary string, not a filesystem path —
    the same tar-slip concern the old git-archive extraction guarded
    against, just at download time. A crafted key that string-prefixes as
    `shared/word-games/` but path-escapes via `..` must not be allowed to
    write outside the skill's own staging directory."""
    evil_key = "shared/word-games/../../../../escape.txt"
    fake_storage.write_bytes("skills-bucket", evil_key, b"pwned\n")
    _session, agent = _make_agent(session_factory, skills=[dict(WORD_GAMES_SKILL)])
    staging = tmp_path / "staging"

    with pytest.raises(RuntimeError, match="word-games"):
        _stage_skills(fake_storage, config, agent, staging)

    assert not (tmp_path / "escape.txt").exists()
    assert not (staging / "escape.txt").exists()


# --- _stage_skills: the marketplace dialect (persona-metadata plan Task 1b) -----

MARKETPLACE_SKILL_ID = "word-games-mp"


def _write_marketplace_index(storage, skill_id, entry, *, bucket="skills-bucket"):
    storage.write_bytes(
        bucket,
        "index.json",
        json.dumps({"skills": {skill_id: entry}, "count": 1}).encode(),
        content_type="application/json",
    )


def test_stage_skills_marketplace_dialect_uses_version_pinned_path(
    session_factory, config, tmp_path
):
    storage = FakeStorage()
    _write_marketplace_index(
        storage,
        MARKETPLACE_SKILL_ID,
        {
            "name": "Word games (marketplace)",
            "version": "7",
            "description": "Plays word games with the user.",
            "tags": ["fun"],
            "content_hash": "mpsha1",
        },
    )
    storage.write_bytes(
        "skills-bucket",
        f"catalog/{MARKETPLACE_SKILL_ID}/versions/7/SKILL.md",
        b"Marketplace pinned body.\n",
        content_type="text/markdown",
    )
    _session, agent = _make_agent(
        session_factory, skills=[{"name": MARKETPLACE_SKILL_ID, "team": "", "sha": "mpsha1"}]
    )
    staging = tmp_path / "staging"

    staged = _stage_skills(storage, config, agent, staging)

    assert staged == [StagedSkill(id=MARKETPLACE_SKILL_ID, dir=staging / MARKETPLACE_SKILL_ID)]
    assert (
        staging / MARKETPLACE_SKILL_ID / "SKILL.md"
    ).read_text() == "Marketplace pinned body.\n"
    assert (
        f"read_bytes:skills-bucket:catalog/{MARKETPLACE_SKILL_ID}/versions/7/SKILL.md"
        in storage.calls
    )
    # the recorded sha came from content_hash, not object custom metadata
    assert agent.skills[0]["sha"] == "mpsha1"


def test_stage_skills_marketplace_head_fallback_logged_when_version_absent(
    session_factory, config, tmp_path, caplog
):
    storage = FakeStorage()
    _write_marketplace_index(
        storage,
        MARKETPLACE_SKILL_ID,
        {
            # no "version" key at all
            "name": "Word games (marketplace)",
            "description": "Plays word games with the user.",
            "tags": ["fun"],
            "content_hash": "headsha",
        },
    )
    storage.write_bytes(
        "skills-bucket",
        f"catalog/{MARKETPLACE_SKILL_ID}/SKILL.md",
        b"Marketplace head body.\n",
        content_type="text/markdown",
    )
    _session, agent = _make_agent(
        session_factory, skills=[{"name": MARKETPLACE_SKILL_ID, "team": "", "sha": "headsha"}]
    )
    staging = tmp_path / "staging"

    with caplog.at_level("WARNING"):
        staged = _stage_skills(storage, config, agent, staging)

    assert staged == [StagedSkill(id=MARKETPLACE_SKILL_ID, dir=staging / MARKETPLACE_SKILL_ID)]
    assert (
        staging / MARKETPLACE_SKILL_ID / "SKILL.md"
    ).read_text() == "Marketplace head body.\n"
    assert (
        f"read_bytes:skills-bucket:catalog/{MARKETPLACE_SKILL_ID}/SKILL.md" in storage.calls
    )
    assert any(MARKETPLACE_SKILL_ID in message for message in caplog.messages)


def test_stage_skills_marketplace_vanished_content_raises(session_factory, config, tmp_path):
    storage = FakeStorage()
    _write_marketplace_index(
        storage,
        MARKETPLACE_SKILL_ID,
        {
            "name": "Word games (marketplace)",
            "version": "7",
            "description": "Plays word games with the user.",
            "tags": ["fun"],
            "content_hash": "mpsha1",
        },
    )
    # No object written at the version-pinned path -- content has vanished.
    _session, agent = _make_agent(
        session_factory, skills=[{"name": MARKETPLACE_SKILL_ID, "team": "", "sha": "mpsha1"}]
    )
    staging = tmp_path / "staging"

    with pytest.raises(RuntimeError, match=MARKETPLACE_SKILL_ID):
        _stage_skills(storage, config, agent, staging)


def test_stage_skills_legacy_fetch_path_byte_untouched(
    session_factory, config, fake_storage, tmp_path
):
    # fake_storage's index.json is still legacy list-shaped -- the
    # marketplace dialect probe this task adds must not change legacy
    # fetch-path behavior at all: same team-prefixed listing, same download.
    _session, agent = _make_agent(session_factory, skills=[dict(WORD_GAMES_SKILL)])
    staging = tmp_path / "staging"

    staged = _stage_skills(fake_storage, config, agent, staging)

    assert staged == [StagedSkill(id="word-games", dir=staging / "word-games")]
    assert (staging / "word-games" / "SKILL.md").is_file()
    assert "list_keys:skills-bucket:shared/word-games/" in fake_storage.calls


# --- RepoStep -----------------------------------------------------------------


def test_repo_step_happy_path_creates_and_records_repo(session_factory, config, fake_storage):
    session, agent = _make_agent(session_factory)
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github, storage=fake_storage))

    RepoStep().run(ctx)

    expected_repo = f"{config.github_org}/agent-{AGENT_NAME}"
    assert agent.repo_full_name == expected_repo
    assert github.calls == [f"repo_exists:{expected_repo}", f"create_repo_from_dir:{expected_repo}"]
    assert len(github.created_from_dir) == 1
    created_repo, created_src = github.created_from_dir[0]
    assert created_repo == expected_repo
    assert (created_src / "bundle" / "agent.yaml").is_file()


def test_repo_step_threads_team_and_purpose_tags_into_baked_card(
    session_factory, config, fake_storage
):
    """Persona metadata (Task 3): the agent row's own team/purpose_tags
    reach the composed bundle's agent-card.json metadata block — never
    hardcoded/defaulted away at compose time."""
    session, agent = _make_agent(
        session_factory, team="acme-team", purpose_tags=json.dumps(["billing", "support"])
    )
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github, storage=fake_storage))

    RepoStep().run(ctx)

    _, created_src = github.created_from_dir[0]
    card = json.loads((created_src / "bundle" / "agent-card.json").read_text())
    assert card["metadata"] == {
        "team": "acme-team",
        "purpose_tags": ["billing", "support"],
        "capability": "",
    }


def test_repo_step_defaults_team_to_shared_when_row_team_is_none(
    session_factory, config, fake_storage
):
    session, agent = _make_agent(session_factory, team=None)
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github, storage=fake_storage))

    RepoStep().run(ctx)

    _, created_src = github.created_from_dir[0]
    card = json.loads((created_src / "bundle" / "agent-card.json").read_text())
    assert card["metadata"]["team"] == "shared"


def test_repo_step_substitutes_persona_name_placeholder(session_factory, config, fake_storage):
    session, agent = _make_agent(session_factory, name="sam")
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github, storage=fake_storage))

    RepoStep().run(ctx)

    persona_text = (ctx.workdir / "repo" / "bundle" / "persona.md").read_text()
    assert "sam" in persona_text
    assert "{name}" not in persona_text


def test_repo_step_resolves_skills_by_id_into_bundle(session_factory, config, fake_storage):
    session, agent = _make_agent(session_factory, skills=[dict(WORD_GAMES_SKILL)])
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github, storage=fake_storage))

    RepoStep().run(ctx)

    assert (ctx.workdir / "repo" / "bundle" / "skills" / "word-games" / "SKILL.md").is_file()


def test_repo_step_unknown_skill_id_raises_clear_message(session_factory, config, fake_storage):
    session, agent = _make_agent(
        session_factory, skills=[{"name": "ghost-skill", "team": "shared", "sha": "whatever"}]
    )
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github, storage=fake_storage))

    with pytest.raises(RuntimeError, match="ghost-skill"):
        RepoStep().run(ctx)

    # got as far as the repo_exists check, but never created anything
    assert github.created_from_dir == []


def test_repo_step_unknown_persona_id_raises_clear_message(session_factory, config, fake_storage):
    session, agent = _make_agent(session_factory, persona="ghost-persona")
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github, storage=fake_storage))

    with pytest.raises(RuntimeError, match="ghost-persona"):
        RepoStep().run(ctx)

    # got as far as the repo_exists check, but never created anything
    assert github.created_from_dir == []


def test_repo_step_existing_repo_not_recorded_raises(session_factory, config):
    expected_repo = f"{config.github_org}/agent-{AGENT_NAME}"
    session, agent = _make_agent(session_factory, repo_full_name=None)
    github = FakeGitHub(existing_repos={expected_repo})
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    with pytest.raises(RuntimeError):
        RepoStep().run(ctx)

    assert github.created_from_dir == []


def test_repo_step_resume_when_row_already_matches_skips_creation(session_factory, config):
    expected_repo = f"{config.github_org}/agent-{AGENT_NAME}"
    session, agent = _make_agent(session_factory, repo_full_name=expected_repo)
    github = FakeGitHub(existing_repos={expected_repo})
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    detail = RepoStep().run(ctx)

    assert "already" in detail
    # idempotent resume: no compose, no create_repo_from_dir call
    assert github.calls == [f"repo_exists:{expected_repo}"]
    assert github.created_from_dir == []
    assert not (ctx.workdir / "repo").exists()


def test_repo_step_rmtrees_stale_compose_dir_before_recomposing(session_factory, config, fake_storage):
    """Retry-safety: a prior partial attempt may have left a compose dir with a
    skills subtree behind; compose() raises FileExistsError on top of that, so
    RepoStep must clear it first."""
    session, agent = _make_agent(session_factory, skills=[dict(WORD_GAMES_SKILL)])
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github, storage=fake_storage))

    # Pre-seed a stale skills subtree that compose() would refuse to overwrite
    stale_dir = ctx.workdir / "repo" / "bundle" / "skills" / "word-games"
    stale_dir.mkdir(parents=True)
    (stale_dir / "stale_file.txt").write_text("leftover from failed attempt\n")

    # RepoStep should succeed despite the stale tree (rmtree clears it first)
    RepoStep().run(ctx)

    # Verify the stale file is gone and the fresh compose succeeded
    assert not (
        ctx.workdir / "repo" / "bundle" / "skills" / "word-games" / "stale_file.txt"
    ).exists()
    assert (ctx.workdir / "repo" / "bundle" / "skills" / "word-games" / "SKILL.md").is_file()
    assert (ctx.workdir / "repo" / "bundle" / "agent.yaml").is_file()


def test_repo_step_compose_raises_without_rmtree_on_stale_skills(tmp_path):
    """Sanity guard: verify that compose() would raise FileExistsError if we
    didn't rmtree the stale skills subtree first."""
    skill_dir = tmp_path / "staged" / "echo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: echo\ndescription: Echoes things.\n---\nEcho skill body.\n"
    )
    skill = StagedSkill(id="echo", dir=skill_dir)

    spec = BundleSpec(
        name="test-agent",
        persona_body="You are {name}.",
        model_id="vertex_ai/claude-sonnet-4-5",
        skills=[skill],
        runtime_image="image:latest",
        vertex_project="proj",
        vertex_location="us-central1",
    )

    compose_dir = tmp_path / "work" / "repo"

    # First compose succeeds
    compose(spec, compose_dir)

    # Second compose to the same location should raise FileExistsError (without rmtree)
    with pytest.raises(FileExistsError):
        compose(spec, compose_dir)


# --- OnboardStep ----------------------------------------------------------------


def _repo_full_name(config) -> str:
    return f"{config.github_org}/agent-{AGENT_NAME}"


def test_onboard_step_happy_path_sets_app_id_and_polls_to_active(session_factory, config):
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    gojira = FakeGojira(activate_after=3)
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira))

    OnboardStep().run(ctx)

    assert agent.gojira_app_id == 1
    assert gojira.calls[0] == f"create_app:{repo}"
    assert gojira.calls.count("app_status:1") == 3


def test_onboard_step_create_app_receives_expected_arguments(session_factory, config, monkeypatch):
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    gojira = FakeGojira(activate_after=1)

    seen = {}
    real_create_app = gojira.create_app

    def _spy_create_app(*, name, repo_full_name, cluster, namespace):
        seen.update(
            name=name, repo_full_name=repo_full_name, cluster=cluster, namespace=namespace
        )
        return real_create_app(
            name=name, repo_full_name=repo_full_name, cluster=cluster, namespace=namespace
        )

    monkeypatch.setattr(gojira, "create_app", _spy_create_app)
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira))

    OnboardStep().run(ctx)

    assert seen == {
        "name": AGENT_NAME,
        "repo_full_name": repo,
        "cluster": config.gojira_cluster,
        "namespace": AGENT_NAME,
    }


def test_onboard_step_raises_with_failed_step_names(session_factory, config):
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    gojira = FakeGojira(
        activate_after=1,
        end_status="failed",
        failed_steps=[
            {"name": "clone", "status": "done"},
            {"name": "provision", "status": "failed", "error": "boom"},
        ],
    )
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira))

    with pytest.raises(RuntimeError) as exc_info:
        OnboardStep().run(ctx)

    message = str(exc_info.value)
    assert "provision" in message
    assert "clone" not in message


def test_onboard_step_conflict_without_recorded_id_raises(session_factory, config):
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=None)
    gojira = FakeGojira(conflict_repos={repo})
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira))

    with pytest.raises(RuntimeError):
        OnboardStep().run(ctx)

    assert agent.gojira_app_id is None


def test_onboard_step_resumes_on_conflict_when_id_already_recorded(session_factory, config):
    repo = _repo_full_name(config)
    gojira = FakeGojira(activate_after=1)
    # Seed the fake as if a prior attempt of this step already created the app
    # (this is what "a matching recorded id" means: the earlier attempt's
    # create_app succeeded and its id made it into agent.gojira_app_id via the
    # engine's commit on that earlier failed/aborted run).
    prior_app_id = gojira.create_app(
        name=AGENT_NAME, repo_full_name=repo, cluster=config.gojira_cluster, namespace=AGENT_NAME
    )
    gojira._conflict_repos.add(repo)  # now gojira reports a live conflict for this repo

    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=prior_app_id)
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira))

    OnboardStep().run(ctx)

    assert agent.gojira_app_id == prior_app_id
    assert f"app_status:{prior_app_id}" in gojira.calls


def test_onboard_step_conflict_resume_reprovisions_a_failed_app(session_factory, config):
    """The live incident, reproduced exactly: a first provisioning attempt's
    OnboardStep created the app, then failed (an environmental kubectl outage
    on gojira's own side) — the app is now stuck in gojira's terminal
    `failed` state. An operator fixes the environment and the product Retry
    re-runs OnboardStep: create_app 409-conflicts (the app already exists)
    and the resume branch adopts the recorded id, exactly like the plain
    resume test above. The difference this test pins: that recorded id's
    live status is `failed`, not provisioning/active — so a bare "trust the
    id and keep polling" would just read the same stale failure straight
    back and raise, and the retry could never converge. Asserts the fix:
    reprovision() is called exactly once (on the right id) before the step
    waits, and the step then succeeds.
    """
    repo = _repo_full_name(config)
    gojira = FakeGojira(activate_after=1, end_status="failed", failed_steps=[
        {"name": "provision", "status": "failed", "error": "kubectl outage"},
    ])
    # Seed the fake exactly like the plain resume test: a prior attempt's
    # create_app already succeeded...
    prior_app_id = gojira.create_app(
        name=AGENT_NAME, repo_full_name=repo, cluster=config.gojira_cluster, namespace=AGENT_NAME
    )
    # ...and that prior attempt's own poll already observed it land in
    # gojira's terminal `failed` state (the stale status this retry must not
    # just trust).
    assert gojira.app_status(prior_app_id)["status"] == "failed"
    gojira._conflict_repos.add(repo)  # now gojira reports a live conflict for this repo
    # The operator has fixed the environment: a reprovision from here on
    # would actually succeed. Flip the fake's target status accordingly
    # (same idiom test_api_agents.py's retry-recovery test uses) so the
    # step's own reprovision -> _wait_active sequence can observe that.
    gojira._end_status = "active"

    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=prior_app_id)
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira))

    result = OnboardStep().run(ctx)

    assert gojira.reprovision_calls == [prior_app_id]
    assert agent.gojira_app_id == prior_app_id
    assert "active" in result


def test_onboard_step_conflict_resume_survives_stale_failed_read_after_reprovision(
    session_factory, config
):
    """Follow-up live incident, verified against the platform's DB: gojira
    DID accept the reprovision() kick above (a fresh provision run started
    ~2s later) but `_wait_active`'s first poll landed in the window before
    that new run flipped the app's status — it read the OLD run's stale
    `failed` (still carrying the old run's failed-steps detail) and raised
    immediately, even though the rerun was genuinely underway. A plain
    read-after-write race, the same class this codebase already learned to
    absorb on CI-run reads (BuildStep's grace, P1c).

    Modeled directly via `status_sequence`: the initial check reads
    `failed` (triggering reprovision), the very next read echoes that SAME
    stale `failed`, then the platform's new run actually shows up as
    `provisioning`, then `active`. Asserts the fix: the step still succeeds,
    and reprovision() is called exactly once — the stale echo must not
    trigger a second reprovision kick.
    """
    repo = _repo_full_name(config)
    gojira = FakeGojira(status_sequence=["failed", "failed", "provisioning", "active"])
    prior_app_id = gojira.create_app(
        name=AGENT_NAME, repo_full_name=repo, cluster=config.gojira_cluster, namespace=AGENT_NAME
    )
    gojira._conflict_repos.add(repo)  # now gojira reports a live conflict for this repo

    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=prior_app_id)
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira))

    result = OnboardStep().run(ctx)

    assert gojira.reprovision_calls == [prior_app_id]
    assert agent.gojira_app_id == prior_app_id
    assert "active" in result


def test_onboard_step_conflict_resume_raises_when_rerun_never_starts(session_factory, config):
    """Companion to the stale-read race above: if the platform's status
    genuinely never leaves `failed` (it never actually started the
    requested rerun at all — as opposed to merely being slow to reflect it),
    the grace budget must eventually give up. The raised message must name
    the truth of what happened — the platform never started the rerun — not
    misreport it as a fresh provisioning failure, since gojira's own
    failed-steps detail here is entirely the stale original run's.
    """
    repo = _repo_full_name(config)
    gojira = FakeGojira(
        activate_after=1,
        end_status="failed",
        failed_steps=[{"name": "provision", "status": "failed", "error": "kubectl outage"}],
    )
    prior_app_id = gojira.create_app(
        name=AGENT_NAME, repo_full_name=repo, cluster=config.gojira_cluster, namespace=AGENT_NAME
    )
    assert gojira.app_status(prior_app_id)["status"] == "failed"
    gojira._conflict_repos.add(repo)  # now gojira reports a live conflict for this repo
    # Unlike the recovering test above, the platform never actually starts a
    # new run here: `_end_status` stays "failed", so reprovision()'s reset to
    # "pending" just lands right back on "failed" on the very next poll,
    # every time — modeling a reprovision that was accepted but never acted
    # on.

    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=prior_app_id)
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira))

    with pytest.raises(RuntimeError) as exc_info:
        OnboardStep().run(ctx)

    message = str(exc_info.value)
    assert "did not start the requested rerun" in message
    assert f"app {prior_app_id}" in message
    assert gojira.reprovision_calls == [prior_app_id]
    # Exactly the dedicated grace budget's worth of post-reprovision polls,
    # plus the one initial-check poll before reprovision() was even called.
    assert gojira.calls.count(f"app_status:{prior_app_id}") == 1 + 1 + ONBOARD_REPROVISION_GRACE_POLLS


def test_onboard_step_conflict_resume_active_app_does_not_reprovision(session_factory, config):
    """Pins the surgical condition: a conflict-resume onto an id that's
    already `active` (not `failed`) must NOT reprovision — reprovision is
    only for re-kicking a terminally-failed platform app, not a routine
    "someone else already onboarded this" resume. Any status other than
    `failed` (provisioning/queued/active) keeps today's plain-wait
    behavior.
    """
    repo = _repo_full_name(config)
    gojira = FakeGojira(activate_after=1)  # default end_status="active"
    prior_app_id = gojira.create_app(
        name=AGENT_NAME, repo_full_name=repo, cluster=config.gojira_cluster, namespace=AGENT_NAME
    )
    assert gojira.app_status(prior_app_id)["status"] == "active"
    gojira._conflict_repos.add(repo)  # now gojira reports a live conflict for this repo

    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=prior_app_id)
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira))

    result = OnboardStep().run(ctx)

    assert gojira.reprovision_calls == []
    assert agent.gojira_app_id == prior_app_id
    assert "active" in result


def test_onboard_step_times_out_after_max_polls(session_factory, config):
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    gojira = FakeGojira(activate_after=999_999)  # never reaches active/failed in time
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira))

    with pytest.raises(RuntimeError, match="timed out"):
        OnboardStep().run(ctx)

    expected_polls = POLL_TIMEOUT_S // POLL_INTERVAL_S
    assert gojira.calls.count("app_status:1") == expected_polls


def test_onboard_step_sleep_called_with_correct_interval(session_factory, config, monkeypatch):
    import kobo_api.steps as steps_module

    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    gojira = FakeGojira(activate_after=3)
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira))

    # Capture sleep calls
    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(steps_module, "_sleep", fake_sleep)

    OnboardStep().run(ctx)

    # Verify _sleep was called with POLL_INTERVAL_S between polls (but not after the final success)
    assert all(call == POLL_INTERVAL_S for call in sleep_calls)
    # activate_after=3 means 3 polls until success, so 2 sleeps (between polls 1-2 and 2-3)
    assert len(sleep_calls) == 2


# --- AccessStep -----------------------------------------------------------------


def test_access_step_calls_fakes_in_order_with_short_gsa_name(session_factory, config):
    session, agent = _make_agent(session_factory)
    cloud = FakeCloud(project=config.gcp_project)
    ctx = _make_ctx(session, agent, config, _clients(cloud=cloud))

    AccessStep().run(ctx)

    expected_email = f"agent-{AGENT_NAME}@{config.gcp_project}.iam.gserviceaccount.com"
    assert cloud.calls == [
        f"ensure_gsa:agent-{AGENT_NAME}",
        f"bind_workload_identity:{expected_email}:{AGENT_NAME}/agent-{AGENT_NAME}",
        f"add_to_group:{expected_email}:{config.model_group}",
    ]
    assert cloud.gsas == {expected_email}
    assert (expected_email, config.model_group) in cloud.group_members


# --- HelloStep (persona-metadata plan, Task 3: capability earned + card lockstep) --

HELLO_REPO = "acme/agent-clio"


def _hello_ctx(session_factory, config, fake_storage, *, github=None, name=None, agent_client=None):
    session, agent = _make_agent(session_factory, repo_full_name=HELLO_REPO)
    clients = _clients(
        github=github if github is not None else FakeGitHub(),
        storage=fake_storage,
        agent=agent_client if agent_client is not None else FakeAgent(),
        kubectl=FakeKubectl(),
        name=name,
    )
    return _make_ctx(session, agent, config, clients), agent


def test_hello_step_records_reply_stores_capability_and_commits_card(
    session_factory, config, fake_storage
):
    github = FakeGitHub()
    fake_name = FakeName(capability="Helps with word games and small talk.")
    ctx, agent = _hello_ctx(session_factory, config, fake_storage, github=github, name=fake_name)

    detail = HelloStep().run(ctx)

    assert agent.first_reply == "hello yourself"
    assert "hello yourself" in detail
    assert agent.capability == "Helps with word games and small talk."
    assert github.pushes[-1][1] == "kobo: directory details updated"


def test_hello_step_capability_generation_uses_persona_body_and_skills(
    session_factory, config, fake_storage
):
    from kobo_api.search import ingest_indexes

    fake_name = FakeName(capability="Helps.")
    ctx, agent = _hello_ctx(
        session_factory, config, fake_storage, name=fake_name
    )
    agent.skills = [dict(WORD_GAMES_SKILL)]
    ingest_indexes(ctx.session.get_bind(), fake_storage, {"skill": config.skills_bucket})

    HelloStep().run(ctx)

    assert len(fake_name.capability_calls) == 1
    persona_body, skill_descriptions, _tooling_filenames = fake_name.capability_calls[0]
    assert "clio" in persona_body
    assert skill_descriptions == ["Plays word games with the user."]


def test_hello_step_model_client_raising_still_succeeds_and_falls_back(
    session_factory, config, fake_storage
):
    """Global Constraints: capability generation must never block readiness —
    a raising model client must not fail the hello step at all."""
    github = FakeGitHub()
    raising_name = FakeName(capability_error="model unavailable")
    ctx, agent = _hello_ctx(session_factory, config, fake_storage, github=github, name=raising_name)

    detail = HelloStep().run(ctx)  # must not raise

    assert agent.first_reply == "hello yourself"
    assert "hello yourself" in detail
    # Fallback tier fired: capability is non-null (never left unset merely
    # because the model client raised).
    assert agent.capability is not None
    assert agent.capability != ""
    # The card still got committed normally — only the model call failed.
    assert github.pushes[-1][1] == "kobo: directory details updated"


def test_hello_step_no_model_client_configured_falls_back(session_factory, config, fake_storage):
    ctx, agent = _hello_ctx(session_factory, config, fake_storage, name=None)

    HelloStep().run(ctx)

    assert agent.capability is not None


def test_hello_step_github_raising_stores_capability_but_skips_card_commit(
    session_factory, config, fake_storage, caplog
):
    """Store-vs-skip: the row is the room's own truth, the card commit is
    best-effort delivery a later Regenerate can retry — a broken GitHub
    client must still let generation succeed and the row get stored, only
    the card commit is skipped (and logged)."""
    raising_github = FakeGitHub(clone_error="gh: rate limited")
    ctx, agent = _hello_ctx(session_factory, config, fake_storage, github=raising_github)

    with caplog.at_level("WARNING"):
        detail = HelloStep().run(ctx)  # must not raise

    assert agent.first_reply == "hello yourself"
    assert "hello yourself" in detail
    assert agent.capability is not None
    assert agent.capability != ""
    # No commit ever happened — clone raised before commit_push could run.
    assert raising_github.pushes == []
    assert "card commit failed" in caplog.text


def test_hello_step_persona_lookup_failure_leaves_capability_null(
    session_factory, config, fake_storage
):
    """The other named outcome: when generation itself can't run at all
    (here, the persona vanished from the catalog), capability stays null —
    there's no input to generate anything from, fallback or otherwise."""
    session, agent = _make_agent(
        session_factory, persona="ghost-persona", repo_full_name=HELLO_REPO
    )
    clients = _clients(
        github=FakeGitHub(), storage=fake_storage, agent=FakeAgent(), kubectl=FakeKubectl()
    )
    ctx = _make_ctx(session, agent, config, clients)

    detail = HelloStep().run(ctx)  # must not raise

    assert agent.first_reply == "hello yourself"
    assert "hello yourself" in detail
    assert agent.capability is None


def test_hello_step_tooling_listing_failure_does_not_block_generation(
    session_factory, config, fake_storage
):
    """tooling_filenames_for's own github-side failures are swallowed
    internally (best-effort input) — generation must still succeed and the
    row must still get stored, distinct from the "github raising" case
    above where the FOLLOW-ON card commit is what fails."""
    github = FakeGitHub(clone_error="gh: rate limited")
    fake_name = FakeName(capability="Helps.")
    ctx, agent = _hello_ctx(session_factory, config, fake_storage, github=github, name=fake_name)

    HelloStep().run(ctx)

    assert agent.capability == "Helps."
    assert fake_name.capability_calls[0][2] == []  # tooling_filenames came back empty

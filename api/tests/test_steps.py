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

import base64
import json
import logging
from pathlib import Path

import pytest
import yaml

from kobo_api.clients import CabalError, ClientError, Clients
from kobo_api.composer import BundleSpec, _colleagues_md, _self_md, compose
from kobo_api.config import Config, ModelChoice
from kobo_api.kinds import KIND_CONNECTED
from kobo_api.marketplace import marketplace_repo
from kobo_api.models import INVITE_ACCEPTED, A2AInvite, Agent, AgentStatus
from kobo_api.provisioner import PIPELINE, Ctx
from kobo_api.steps import (
    AVATAR_MAX_BYTES,
    HELLO_RETRY_ATTEMPTS,
    HELLO_RETRY_BACKOFF_S,
    ONBOARD_REPROVISION_GRACE_POLLS,
    POLL_INTERVAL_S,
    POLL_TIMEOUT_S,
    AccessStep,
    AvatarStep,
    DirectoryStep,
    HelloStep,
    OnboardStep,
    RepoStep,
    StagedSkill,
    StepSkipped,
    _agents_who_can_see,
    _colleague_roster,
    _render_colleagues_md,
    _render_self_md,
    _resolve_persona_body,
    _stage_baseline_tools,
    _stage_skills,
    refresh_after_capability_distill,
    refresh_colleague_rosters,
)
from tests.fakes import (
    FAKE_IMAGE_PNG_BYTES,
    FakeAgent,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeImage,
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
    group_name: str | None = None,
    purpose_tags: str | None = None,
    skip_baseline: bool = False,
    archetype: str | None = None,
):
    session = session_factory()
    agent = Agent(
        archetype=archetype,
        name=name,
        persona=persona,
        model_id=model_id,
        skills=skills or [],
        repo_full_name=repo_full_name,
        gojira_app_id=gojira_app_id,
        team=team,
        group_name=group_name,
        purpose_tags=purpose_tags,
        skip_baseline=skip_baseline,
    )
    session.add(agent)
    session.commit()
    return session, agent


def _make_ctx(session, agent, config, clients) -> Ctx:
    workdir = config.workdir / agent.name
    workdir.mkdir(parents=True, exist_ok=True)
    return Ctx(agent=agent, session=session, config=config, clients=clients, workdir=workdir)


def _clients(
    *,
    github=None,
    gojira=None,
    cloud=None,
    storage=None,
    agent=None,
    kubectl=None,
    name=None,
    cabal=None,
    image=None,
) -> Clients:
    return Clients(
        github=github,
        gojira=gojira,
        cloud=cloud,
        agent=agent,
        kubectl=kubectl,
        storage=storage,
        name=name,
        cabal=cabal,
        image=image,
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


# --- _stage_baseline_tools ------------------------------------------------------

BASELINE_LEAKGUARD_PY = (
    '"""Baseline leakguard."""\n'
    "def post_draft(draft):\n"
    "    return draft.replace('secret', '[redacted]')\n"
)

BASELINE_PROBE_GUARD_PY = (
    '"""Baseline probe guard."""\n'
    "def post_draft(draft):\n"
    "    return draft\n"
)

OTHER_TOOL_PY = '"""Not part of the pack."""\ndef post_draft(draft):\n    return draft\n'

# Unparseable Python: the shape the marketplace's review-gate-free direct-push
# lane can genuinely leave in that tree, and exactly what the runtime's
# enforcement loader would refuse (locking every agent that shipped it).
BROKEN_BASELINE_PY = '"""Broken baseline tool."""\ndef post_draft(draft:\n'

BASELINE_TOOL_MD = (
    "---\nname: baseline-leakguard\ndescription: Redacts credential-shaped strings.\n"
    "owner: sre\ntags: [security]\n---\n"
)


def _marketplace_github(**kwargs) -> FakeGitHub:
    """A FakeGitHub whose clone serves the org marketplace tree: two
    baseline-prefixed tools, one non-baseline tool, and a TOOL.md beside
    one of the baseline entries (only the two prefixed `.py` files belong
    to the pack)."""
    return FakeGitHub(
        clone_tree={
            "tools/baseline-leakguard/TOOL.md": BASELINE_TOOL_MD,
            "tools/baseline-leakguard/baseline-leakguard.py": BASELINE_LEAKGUARD_PY,
            "tools/baseline-probe-guard/baseline-probe-guard.py": BASELINE_PROBE_GUARD_PY,
            "tools/other-tool/other-tool.py": OTHER_TOOL_PY,
        },
        **kwargs,
    )


def test_stage_baseline_tools_copies_only_prefixed_py_files(config, tmp_path):
    config.skills_repo = "org-skills"
    github = _marketplace_github()
    bundle_dir = tmp_path / "repo" / "bundle"

    staged = _stage_baseline_tools(github, config, bundle_dir)

    assert staged == ["baseline-leakguard.py", "baseline-probe-guard.py"]
    # Copied flat into bundle/tools/ by filename -- no per-tool subdirectory,
    # no TOOL.md, nothing from the non-baseline tool.
    assert sorted(p.name for p in (bundle_dir / "tools").iterdir()) == staged
    assert (bundle_dir / "tools" / "baseline-leakguard.py").read_text() == BASELINE_LEAKGUARD_PY
    assert github.cloned[0][0] == marketplace_repo(config)


def test_stage_baseline_tools_unconfigured_marketplace_is_silent(config, tmp_path, caplog):
    """A room that never named a skills repo has no pack to miss: no clone,
    no warning (every create in a dev room would otherwise log one), and
    nothing staged. The warning is reserved for a marketplace that IS
    configured and then fails."""
    config.skills_repo = None
    github = _marketplace_github()
    bundle_dir = tmp_path / "repo" / "bundle"

    with caplog.at_level(logging.WARNING):
        staged = _stage_baseline_tools(github, config, bundle_dir)

    assert staged == []
    assert not (bundle_dir / "tools").exists()
    assert github.cloned == []
    assert caplog.text == ""


def test_stage_baseline_tools_skips_a_file_the_tooling_validator_rejects(
    config, tmp_path, caplog
):
    """The marketplace's publish lane is a direct commit with no review
    gate, so a file that would fail the runtime's enforcement contract can
    be sitting in that tree. Staging one would lock EVERY new agent -- so
    each file is validated independently (mirroring
    `kobo_api.tooling.import_tooling`) and a rejected one is skipped, never
    allowed to sink the rest of the pack."""
    config.skills_repo = "org-skills"
    github = FakeGitHub(
        clone_tree={
            "tools/baseline-leakguard/baseline-leakguard.py": BASELINE_LEAKGUARD_PY,
            "tools/baseline-broken/baseline-broken.py": BROKEN_BASELINE_PY,
        }
    )
    bundle_dir = tmp_path / "repo" / "bundle"

    with caplog.at_level(logging.WARNING):
        staged = _stage_baseline_tools(github, config, bundle_dir)

    assert staged == ["baseline-leakguard.py"]
    assert not (bundle_dir / "tools" / "baseline-broken.py").exists()
    assert "baseline tooling skipped" in caplog.text
    assert "baseline-broken.py" in caplog.text


class _SymlinkMarketplace:
    """The one marketplace shape `FakeGitHub`'s text-only `clone_tree` can't
    express: a pack entry that is a SYMLINK pointing out of the clone. Git
    stores symlinks, so a file in that org-shared tree really can be one --
    and following it would copy content off the cloning machine straight
    into an agent's bundle. Only `clone` is ever called on this."""

    def __init__(self, escape_target):
        self._escape_target = escape_target
        self.cloned: list = []

    def clone(self, repo, dest):
        self.cloned.append((repo, dest))
        entry = dest / "tools" / "baseline-escape"
        entry.mkdir(parents=True)
        (entry / "baseline-escape.py").symlink_to(self._escape_target)


def test_stage_baseline_tools_never_follows_a_symlink_out_of_the_clone(
    config, tmp_path, caplog
):
    config.skills_repo = "org-skills"
    secret = tmp_path / "secret.py"
    secret.write_text('"""Not the marketplace\'s."""\ndef post_draft(draft):\n    return draft\n')
    github = _SymlinkMarketplace(secret)
    bundle_dir = tmp_path / "repo" / "bundle"

    with caplog.at_level(logging.WARNING):
        staged = _stage_baseline_tools(github, config, bundle_dir)

    assert staged == []
    assert not (bundle_dir / "tools").exists()
    assert "baseline tooling skipped" in caplog.text


def test_stage_baseline_tools_configured_but_unreachable_warns(config, tmp_path, caplog):
    config.skills_repo = "org-skills"
    github = _marketplace_github(clone_error="marketplace clone exploded")
    bundle_dir = tmp_path / "repo" / "bundle"

    with caplog.at_level(logging.WARNING):
        staged = _stage_baseline_tools(github, config, bundle_dir)

    assert staged == []
    assert not (bundle_dir / "tools").exists()
    assert "baseline tooling skipped" in caplog.text
    assert marketplace_repo(config) in caplog.text


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


def test_repo_step_threads_group_name_into_spec_group(
    session_factory, config, fake_storage, monkeypatch
):
    """Access-control arc: `agent.group_name` (the ACL namespace,
    `kobo_api.models.Agent.group_name`'s own docstring says it is DISTINCT
    from `team` and "the two must never be conflated") must reach
    `BundleSpec.group` untouched -- composer tests (`test_composer.py`'s own
    `test_compose_writes_self_card_knowledge`) pin that `_self_md` then
    renders it as its own, separately-labelled fact rather than reusing
    `team`'s value."""
    session, agent = _make_agent(
        session_factory, team="acme-team", group_name="acme-acl"
    )
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github, storage=fake_storage))

    captured: dict = {}
    _spy_compose(monkeypatch, captured)

    RepoStep().run(ctx)

    assert captured["spec"].group == "acme-acl"
    assert captured["spec"].team == "acme-team"


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


def test_colleague_roster_includes_skills_for_the_capability_fallback_chain(
    session_factory, config
):
    """Important 6 fix: composer.py's `_colleague_capability_line` falls
    back capability -> purpose_tags -> skills -> "not yet distilled", but
    `_colleague_roster` emitted no "skills" key at all -- that third
    fallback level was permanently unreachable no matter how bare a
    colleague's roster entry was. Populated from `Agent.skills`'s own
    JSON-column shape (a list of `{"name": ..., "team": ..., "sha": ...}`
    dicts -- `_stage_skills`'s own docstring), just the names, pick order
    preserved."""
    session, agent = _make_agent(session_factory, group_name="acme-acl")
    colleague = Agent(
        name="ward",
        persona=PERSONA,
        model_id=MODEL_ID,
        skills=[dict(WORD_GAMES_SKILL), {"name": "code-review", "team": "shared", "sha": "x"}],
        status=AgentStatus.READY,
        group_name="acme-acl",
        capability="",
        purpose_tags=None,
    )
    session.add(colleague)
    session.commit()

    roster = _colleague_roster(session, agent)

    assert roster == [
        {
            "name": "ward",
            "team": None,
            "purpose_tags": [],
            "capability": "",
            "skills": ["word-games", "code-review"],
        }
    ]


def test_colleague_roster_leaves_out_a_connected_agent(session_factory, config):
    """A connected agent is nobody's colleague (connected-agents design
    2026-09-21): the room holds no inbound route to it, so naming it in a
    roster promises a hop that would fail. Same rule `allowed_pairs` keeps
    on the wire, kept here on the page the agent itself reads."""
    session, agent = _make_agent(session_factory, group_name="acme-acl")
    connected = Agent(
        name="ward",
        persona="connected",
        model_id=MODEL_ID,
        skills=[],
        status=AgentStatus.READY,
        group_name="acme-acl",
        kind=KIND_CONNECTED,
    )
    session.add(connected)
    session.commit()

    assert _colleague_roster(session, agent) == []


def test_repo_step_threads_colleague_roster_into_composed_bundle(
    session_factory, config, fake_storage
):
    """Standing-knowledge arc, Task 5: `_colleague_roster(ctx.session, agent)`
    must actually reach the composed bundle's `knowledge/colleagues.md` --
    nothing else in the suite pins this threading (composer tests exercise
    `_colleagues_md` directly against a hand-built `BundleSpec.colleagues`
    list, never through RepoStep). Deleting `colleagues=_colleague_roster(...)`
    from RepoStep's `BundleSpec` construction would leave every other test
    green while every new agent silently got "no colleagues in the room
    yet". Seeds a second, READY agent sharing this agent's (non-shared)
    ACL group -- `implicitly_paired` (via `_colleague_roster`) is what makes
    that agent visible in the roster."""
    session, agent = _make_agent(session_factory, group_name="acme-acl")
    colleague = Agent(
        name="ward",
        persona=PERSONA,
        model_id=MODEL_ID,
        skills=[],
        status=AgentStatus.READY,
        group_name="acme-acl",
        capability="keeps the workshop's digital info safe",
    )
    session.add(colleague)
    session.commit()

    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github, storage=fake_storage))

    RepoStep().run(ctx)

    _, created_src = github.created_from_dir[0]
    text = (created_src / "bundle" / "knowledge" / "colleagues.md").read_text()
    assert "ward" in text
    assert "keeps the workshop's digital info safe" in text


# --- Roster refresh lane (fast-lane + discovery arc, Task 6) -----------------
#
# `_agents_who_can_see` is `_colleague_roster`'s own visibility rule, walked
# backwards: given a changed agent's name/group, who currently has it in
# their own roster. `refresh_colleague_rosters` is what actually redelivers a
# freshly rendered `colleagues.md` to each of those viewers over the fast
# lane. The negative case is load-bearing: a room with agents in two
# different (non-shared) groups must never cross-deliver between them absent
# an accepted invite.


def _ready_agent(session, name, *, group_name=None, capability=None):
    agent = Agent(
        name=name,
        persona=PERSONA,
        model_id=MODEL_ID,
        skills=[],
        status=AgentStatus.READY,
        group_name=group_name,
        capability=capability,
    )
    session.add(agent)
    session.commit()
    return agent


def _make_invite(session, *, from_agent, to_agent, status=INVITE_ACCEPTED):
    invite = A2AInvite(from_agent=from_agent, to_agent=to_agent, status=status)
    session.add(invite)
    session.commit()
    return invite


class _PartialFailCabal:
    """A relay double whose outcome varies by target name -- the shared
    `FakeCabal`'s own `relay_error`/`relay_result` are both uniform across
    every call, which can't express "this target misses, that one
    succeeds" in one test. Mirrors this file's own `_FlakyCabal` precedent
    (HelloStep retry section) for a case the shared fake can't cover."""

    def __init__(self, *, fail_for: set[str]):
        self._fail_for = fail_for
        self.relay_calls: list[dict] = []

    def relay(self, name: str, task_envelope: dict) -> dict:
        self.relay_calls.append({"name": name, "task_envelope": task_envelope})
        if name in self._fail_for:
            raise CabalError("relay failed: switchboard unreachable")
        return {"status": {"state": "completed", "message": {"parts": [{"text": "ok"}]}}}


# --- _agents_who_can_see: the reversed visibility rule -----------------------


def test_agents_who_can_see_same_group_ready_agent(session_factory):
    session, target = _make_agent(session_factory, name="target", group_name="acme-acl")
    target.status = AgentStatus.READY
    session.commit()
    _ready_agent(session, "viewer", group_name="acme-acl")

    result = _agents_who_can_see(session, name=target.name, group_name=target.group_name)

    assert [a.name for a in result] == ["viewer"]


def test_agents_who_can_see_cross_group_without_invite_is_empty(session_factory):
    """The load-bearing negative half: two different (non-shared) groups,
    no invite between them -- the reversed rule must find nobody."""
    session, target = _make_agent(session_factory, name="target", group_name="acme-acl")
    target.status = AgentStatus.READY
    session.commit()
    _ready_agent(session, "stranger", group_name="orbit-acl")

    result = _agents_who_can_see(session, name=target.name, group_name=target.group_name)

    assert result == []


def test_agents_who_can_see_accepted_invite_from_viewer_grants_visibility(session_factory):
    """Directional: an invite the VIEWER sent to the target, accepted,
    means the viewer can now see it even across a group boundary."""
    session, target = _make_agent(session_factory, name="target", group_name="acme-acl")
    target.status = AgentStatus.READY
    session.commit()
    _ready_agent(session, "viewer", group_name="orbit-acl")
    _make_invite(session, from_agent="viewer", to_agent="target", status=INVITE_ACCEPTED)

    result = _agents_who_can_see(session, name=target.name, group_name=target.group_name)

    assert [a.name for a in result] == ["viewer"]


def test_agents_who_can_see_reverse_invite_direction_grants_nothing(session_factory):
    """The other direction of the same invite (target invited someone else)
    must not grant that someone else visibility of the target -- only an
    invite the VIEWER itself sent does."""
    session, target = _make_agent(session_factory, name="target", group_name="acme-acl")
    target.status = AgentStatus.READY
    session.commit()
    _ready_agent(session, "someone", group_name="orbit-acl")
    _make_invite(session, from_agent="target", to_agent="someone", status=INVITE_ACCEPTED)

    result = _agents_who_can_see(session, name=target.name, group_name=target.group_name)

    assert result == []


def test_agents_who_can_see_pending_invite_grants_nothing(session_factory):
    session, target = _make_agent(session_factory, name="target", group_name="acme-acl")
    target.status = AgentStatus.READY
    session.commit()
    _ready_agent(session, "viewer", group_name="orbit-acl")
    _make_invite(session, from_agent="viewer", to_agent="target", status="pending")

    result = _agents_who_can_see(session, name=target.name, group_name=target.group_name)

    assert result == []


def test_agents_who_can_see_excludes_connected_viewers(session_factory):
    """A connected agent has no bundle and no colleagues.md, so there is
    nothing to refresh on it: delivering a roster to one would be a
    knowledge task to an agent that never asked for knowledge."""
    session, target = _make_agent(session_factory, name="target", group_name="acme-acl")
    target.status = AgentStatus.READY
    session.commit()
    connected = _ready_agent(session, "outsider", group_name="acme-acl")
    connected.kind = KIND_CONNECTED
    session.commit()

    result = _agents_who_can_see(session, name=target.name, group_name=target.group_name)

    assert result == []


def test_agents_who_can_see_excludes_non_ready_viewers(session_factory):
    session, target = _make_agent(session_factory, name="target", group_name="acme-acl")
    target.status = AgentStatus.READY
    session.commit()
    provisioning = Agent(
        name="still-provisioning",
        persona=PERSONA,
        model_id=MODEL_ID,
        skills=[],
        status=AgentStatus.PROVISIONING,
        group_name="acme-acl",
    )
    session.add(provisioning)
    session.commit()

    result = _agents_who_can_see(session, name=target.name, group_name=target.group_name)

    assert result == []


def test_agents_who_can_see_ungrouped_target_has_no_implicit_viewers(session_factory):
    """`implicitly_paired` never matches a None group on either side -- two
    ungrouped agents are not implicitly connected to each other."""
    session, target = _make_agent(session_factory, name="target", group_name=None)
    target.status = AgentStatus.READY
    session.commit()
    _ready_agent(session, "viewer", group_name=None)

    result = _agents_who_can_see(session, name=target.name, group_name=target.group_name)

    assert result == []


def test_agents_who_can_see_still_correct_once_target_is_no_longer_ready(session_factory):
    """The exact case the destroy hook relies on: by the time it asks this
    question, the departing agent's own status has already flipped away
    from READY -- this function must still correctly report who could see
    it, since it never reads the target's own status, only compares
    against its name/group."""
    session, target = _make_agent(session_factory, name="target", group_name="acme-acl")
    target.status = AgentStatus.DESTROYING
    session.commit()
    _ready_agent(session, "viewer", group_name="acme-acl")

    result = _agents_who_can_see(session, name=target.name, group_name=target.group_name)

    assert [a.name for a in result] == ["viewer"]


# --- refresh_colleague_rosters: the actual redelivery ------------------------


def test_refresh_colleague_rosters_does_not_cross_deliver_between_two_groups(
    session_factory, config, monkeypatch
):
    """Global Constraints' load-bearing negative test: a room with agents in
    two groups must not cross-deliver. A same-group viewer gets the
    refresh; a viewer in an unrelated group, with no invite, never does."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _hello_cabal_enabled_config(config)
    session, target = _make_agent(session_factory, name="target", group_name="acme-acl")
    target.status = AgentStatus.READY
    session.commit()
    _ready_agent(session, "same-group", group_name="acme-acl")
    _ready_agent(session, "other-group", group_name="orbit-acl")
    fake_cabal = FakeCabal(
        relay_result={"status": {"state": "completed", "message": {"parts": [{"text": "ok"}]}}}
    )
    clients = _clients(cabal=fake_cabal)

    refresh_colleague_rosters(
        clients, cfg, session, name=target.name, group_name=target.group_name
    )

    delivered_to = [call["name"] for call in fake_cabal.relay_calls]
    assert delivered_to == ["same-group"]
    assert "other-group" not in delivered_to


def test_refresh_colleague_rosters_envelope_shape_and_content(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _hello_cabal_enabled_config(config)
    session, target = _make_agent(session_factory, name="target", group_name="acme-acl")
    target.status = AgentStatus.READY
    target.capability = "fixes things"
    session.commit()
    _ready_agent(session, "viewer", group_name="acme-acl")
    fake_cabal = FakeCabal(
        relay_result={"status": {"state": "completed", "message": {"parts": [{"text": "ok"}]}}}
    )
    clients = _clients(cabal=fake_cabal)

    refresh_colleague_rosters(
        clients, cfg, session, name=target.name, group_name=target.group_name
    )

    assert len(fake_cabal.relay_calls) == 1
    call = fake_cabal.relay_calls[0]
    assert call["name"] == "viewer"
    envelope = call["task_envelope"]
    assert envelope["metadata"] == {"system_task": "knowledge", "filename": "colleagues.md"}
    delivered_text = envelope["status"]["message"]["parts"][0]["text"]
    assert "target" in delivered_text
    assert "fixes things" in delivered_text
    # Byte-for-byte the same render a fresh compose would produce for this
    # exact roster (`_render_colleagues_md`/`_colleague_roster`, the same
    # functions RepoStep itself calls at birth).
    viewer = session.query(Agent).filter_by(name="viewer").one()
    assert delivered_text == _render_colleagues_md(viewer, _colleague_roster(session, viewer))


def test_refresh_colleague_rosters_one_viewer_miss_does_not_block_the_rest(
    session_factory, config, monkeypatch, caplog
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _hello_cabal_enabled_config(config)
    session, target = _make_agent(session_factory, name="target", group_name="acme-acl")
    target.status = AgentStatus.READY
    session.commit()
    _ready_agent(session, "flaky-viewer", group_name="acme-acl")
    _ready_agent(session, "steady-viewer", group_name="acme-acl")
    cabal = _PartialFailCabal(fail_for={"flaky-viewer"})
    clients = _clients(cabal=cabal)

    with caplog.at_level("WARNING"):
        refresh_colleague_rosters(
            clients, cfg, session, name=target.name, group_name=target.group_name
        )

    delivered_to = sorted(call["name"] for call in cabal.relay_calls)
    assert delivered_to == ["flaky-viewer", "steady-viewer"]
    assert "roster refresh relay failed" in caplog.text


def test_refresh_colleague_rosters_no_op_when_switchboard_disabled(session_factory, config):
    session, target = _make_agent(session_factory, name="target", group_name="acme-acl")
    target.status = AgentStatus.READY
    session.commit()
    _ready_agent(session, "viewer", group_name="acme-acl")
    fake_cabal = FakeCabal()
    clients = _clients(cabal=fake_cabal)

    refresh_colleague_rosters(
        clients, config, session, name=target.name, group_name=target.group_name
    )

    assert fake_cabal.relay_calls == []


def test_refresh_colleague_rosters_no_op_when_cabal_client_missing(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _hello_cabal_enabled_config(config)
    session, target = _make_agent(session_factory, name="target", group_name="acme-acl")
    target.status = AgentStatus.READY
    session.commit()
    _ready_agent(session, "viewer", group_name="acme-acl")
    clients = _clients(cabal=None)

    # Must not raise (no AttributeError off a None cabal client).
    refresh_colleague_rosters(
        clients, cfg, session, name=target.name, group_name=target.group_name
    )


def test_refresh_colleague_rosters_never_touches_github(session_factory, config, monkeypatch):
    """The fast lane, not a rebuild (Global Constraints): `clients.github`
    is None here -- if this function ever fell back to composing or
    pushing a repo, it would raise on the first attribute access rather
    than silently succeeding."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _hello_cabal_enabled_config(config)
    session, target = _make_agent(session_factory, name="target", group_name="acme-acl")
    target.status = AgentStatus.READY
    session.commit()
    _ready_agent(session, "viewer", group_name="acme-acl")
    fake_cabal = FakeCabal(
        relay_result={"status": {"state": "completed", "message": {"parts": [{"text": "ok"}]}}}
    )
    clients = _clients(github=None, cabal=fake_cabal)

    refresh_colleague_rosters(  # must not raise
        clients, cfg, session, name=target.name, group_name=target.group_name
    )

    assert len(fake_cabal.relay_calls) == 1


# --- refresh_after_capability_distill: self.md plus the roster half ----------


def test_refresh_after_capability_distill_redelivers_self_md_and_roster(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _hello_cabal_enabled_config(config)
    session, agent = _make_agent(
        session_factory, name="distilled", group_name="acme-acl", team="widgets"
    )
    agent.status = AgentStatus.READY
    agent.capability = "fixes things"
    session.commit()
    _ready_agent(session, "colleague", group_name="acme-acl")
    fake_cabal = FakeCabal(
        relay_result={"status": {"state": "completed", "message": {"parts": [{"text": "ok"}]}}}
    )
    clients = _clients(cabal=fake_cabal)

    refresh_after_capability_distill(clients, cfg, session, agent)

    filenames = {call["task_envelope"]["metadata"]["filename"]: call for call in fake_cabal.relay_calls}
    assert set(filenames) == {"self.md", "colleagues.md"}
    # self.md went to the agent itself, colleagues.md to the colleague who
    # can see it -- never swapped.
    self_call = next(c for c in fake_cabal.relay_calls if c["name"] == "distilled")
    roster_call = next(c for c in fake_cabal.relay_calls if c["name"] == "colleague")
    assert self_call["task_envelope"]["metadata"]["filename"] == "self.md"
    assert roster_call["task_envelope"]["metadata"]["filename"] == "colleagues.md"
    self_text = self_call["task_envelope"]["status"]["message"]["parts"][0]["text"]
    assert "fixes things" in self_text
    assert self_text == _render_self_md(agent)


def test_refresh_after_capability_distill_no_op_when_switchboard_disabled(
    session_factory, config
):
    session, agent = _make_agent(session_factory, name="distilled", group_name="acme-acl")
    agent.status = AgentStatus.READY
    agent.capability = "fixes things"
    session.commit()
    fake_cabal = FakeCabal()
    clients = _clients(cabal=fake_cabal)

    refresh_after_capability_distill(clients, config, session, agent)

    assert fake_cabal.relay_calls == []


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


def test_repo_step_composes_with_cabal_auth_when_config_carries_cabal_url(
    session_factory, fake_storage, tmp_path
):
    """Switchboard (cabal) auth-block gate (colleagues arc, Task 4): when
    config.cabal_url is set, RepoStep.run threads it into BundleSpec,
    and compose includes the auth block (jwks_url, issuer, audience) in
    the agent.yaml transport entry."""
    cabal_url = "https://switchboard.example.com"
    config = Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        cabal_url=cabal_url,
    )
    session, agent = _make_agent(session_factory)
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github, storage=fake_storage))

    RepoStep().run(ctx)

    agent_yaml_path = ctx.workdir / "repo" / "bundle" / "agent.yaml"
    assert agent_yaml_path.is_file()
    agent_yaml = yaml.safe_load(agent_yaml_path.read_text())

    # Verify the transport has the auth block with correct values
    transports = agent_yaml.get("transports", [])
    assert len(transports) > 0, "agent.yaml should have transports"
    a2a_transport = transports[0]
    assert a2a_transport["kind"] == "a2a"

    # Auth block must be present when cabal_url is configured
    assert "auth" in a2a_transport, "transport should have auth block when cabal_url is set"
    auth = a2a_transport["auth"]

    # Check the auth fields match the configured cabal_url
    expected_jwks_url = cabal_url.rstrip("/") + "/.well-known/jwks.json"
    assert auth["jwks_url"] == expected_jwks_url
    assert auth["issuer"] == "cabal-core"
    assert auth["audience"] == agent.name


def _archetype_config(tmp_path):
    """Every action coordinate the org can carry, all at once -- the exact
    config that made the Task 2 create lane insufficient on its own."""
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        cabal_url="https://switchboard.example.com",
        broker_url="https://kobo.example.com",
        liminal_url="https://liminal.example.com",
    )


def test_repo_step_threads_archetype_and_composes_no_action_tools(
    session_factory, fake_storage, tmp_path
):
    """The row's archetype marker reaches BundleSpec: a team-guide bundle
    composes with NO github block (open_pr included) and NO liminal block,
    even with both org coordinates configured."""
    config = _archetype_config(tmp_path)
    session, agent = _make_agent(session_factory, archetype="team_guide")
    ctx = _make_ctx(session, agent, config, _clients(github=FakeGitHub(), storage=fake_storage))

    RepoStep().run(ctx)

    text = (ctx.workdir / "repo" / "bundle" / "agent.yaml").read_text()
    tools = yaml.safe_load(text)["tools"]
    assert tools["provider"] == "colleagues"
    assert "github" not in tools and "liminal" not in tools
    assert not any(t.startswith("github.") for t in tools["allow"])


def test_repo_step_keeps_action_tools_for_an_unmarked_agent(
    session_factory, fake_storage, tmp_path
):
    """The control: same org config, no marker -- the birthright blocks
    still compose, so suppression is the archetype's doing alone."""
    config = _archetype_config(tmp_path)
    session, agent = _make_agent(session_factory)
    ctx = _make_ctx(session, agent, config, _clients(github=FakeGitHub(), storage=fake_storage))

    RepoStep().run(ctx)

    tools = yaml.safe_load(
        (ctx.workdir / "repo" / "bundle" / "agent.yaml").read_text()
    )["tools"]
    assert "github" in tools and "liminal" in tools
    assert "github.open_pr" in tools["allow"]


def test_repo_step_composes_without_cabal_auth_when_config_has_no_cabal_url(
    session_factory, config, fake_storage
):
    """Default case (no cabal integration): when config.cabal_url is None,
    RepoStep composes a bundle with no auth block in the transport."""
    session, agent = _make_agent(session_factory)
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github, storage=fake_storage))

    RepoStep().run(ctx)

    agent_yaml_path = ctx.workdir / "repo" / "bundle" / "agent.yaml"
    assert agent_yaml_path.is_file()
    agent_yaml = yaml.safe_load(agent_yaml_path.read_text())

    # Verify the transport has NO auth block when cabal_url is not set
    transports = agent_yaml.get("transports", [])
    assert len(transports) > 0
    a2a_transport = transports[0]
    assert a2a_transport["kind"] == "a2a"
    assert "auth" not in a2a_transport, "transport should not have auth when cabal_url is None"


def _spy_compose(monkeypatch, captured: dict) -> None:
    """Wraps `kobo_api.steps.compose` (the module-level name RepoStep.run
    calls) so a test can inspect the `BundleSpec` a run actually composed
    with, while still letting the real compose() do its work — same seam
    `_no_real_sleep` above patches for `_sleep`, just recording instead of
    no-opping."""
    import kobo_api.steps as steps_module

    real_compose = steps_module.compose

    def fake_compose(spec, dest):
        captured["spec"] = spec
        return real_compose(spec, dest)

    monkeypatch.setattr(steps_module, "compose", fake_compose)


def test_repo_step_uses_model_specific_vertex_location_when_configured(
    session_factory, fake_storage, tmp_path, monkeypatch
):
    """Gemini-3.1-Pro-Preview arc: a `models` entry carrying a `location`
    override threads through RepoStep into the composed BundleSpec — Gemini
    previews serve only on Vertex's global endpoint, not the org's regional
    default."""
    gemini_model_id = "vertex_ai/gemini-3.1-pro-preview"
    config = Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        vertex_location="us-east5",
        models=[
            ModelChoice(id=MODEL_ID, label="Claude Sonnet 4.5"),
            ModelChoice(id=gemini_model_id, label="Gemini 3.1 Pro Preview", location="global"),
        ],
    )
    session, agent = _make_agent(session_factory, model_id=gemini_model_id)
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github, storage=fake_storage))

    captured: dict = {}
    _spy_compose(monkeypatch, captured)

    RepoStep().run(ctx)

    assert captured["spec"].vertex_location == "global"


def test_repo_step_keeps_org_default_vertex_location_for_model_without_override(
    session_factory, config, fake_storage, monkeypatch
):
    """The companion default case: a model with no `location` override (the
    Claude partner models, and the fixture's default MODEL_ID) still
    composes with `config.vertex_location`, unchanged from before this
    field existed."""
    session, agent = _make_agent(session_factory)
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github, storage=fake_storage))

    captured: dict = {}
    _spy_compose(monkeypatch, captured)

    RepoStep().run(ctx)

    assert captured["spec"].vertex_location == config.vertex_location


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


def test_repo_step_stages_baseline_pack_by_default(session_factory, config, fake_storage):
    """Baseline tooling pack (Task 6): every new agent ships the
    marketplace's `baseline-*` tools, and nothing else from that tree."""
    config.skills_repo = "org-skills"
    session, agent = _make_agent(session_factory)
    github = _marketplace_github()
    ctx = _make_ctx(session, agent, config, _clients(github=github, storage=fake_storage))

    RepoStep().run(ctx)

    _, created_src = github.created_from_dir[0]
    tools_dir = created_src / "bundle" / "tools"
    assert sorted(p.name for p in tools_dir.iterdir()) == [
        "baseline-leakguard.py",
        "baseline-probe-guard.py",
    ]
    assert not (tools_dir / "other-tool.py").exists()
    assert (tools_dir / "baseline-leakguard.py").read_text() == BASELINE_LEAKGUARD_PY


def test_repo_step_skip_baseline_stages_nothing(session_factory, config, fake_storage):
    config.skills_repo = "org-skills"
    session, agent = _make_agent(session_factory, skip_baseline=True)
    github = _marketplace_github()
    ctx = _make_ctx(session, agent, config, _clients(github=github, storage=fake_storage))

    RepoStep().run(ctx)

    _, created_src = github.created_from_dir[0]
    assert not (created_src / "bundle" / "tools").exists()
    # The opt-out is checked before the clone: the marketplace is never read.
    assert github.cloned == []


def test_repo_step_invalid_baseline_file_is_dropped_and_create_proceeds(
    session_factory, config, fake_storage, caplog
):
    """The end-to-end shape of the validator guard: one unparseable file in
    the pack must cost that file only. Staging it would ship a bundle the
    runtime's enforcement loader locks, which fails the create outright at
    HelloStep -- the create must instead finish with the good tool aboard."""
    config.skills_repo = "org-skills"
    session, agent = _make_agent(session_factory)
    github = FakeGitHub(
        clone_tree={
            "tools/baseline-leakguard/baseline-leakguard.py": BASELINE_LEAKGUARD_PY,
            "tools/baseline-broken/baseline-broken.py": BROKEN_BASELINE_PY,
        }
    )
    ctx = _make_ctx(session, agent, config, _clients(github=github, storage=fake_storage))

    with caplog.at_level(logging.WARNING):
        RepoStep().run(ctx)

    assert agent.repo_full_name == f"{config.github_org}/agent-{AGENT_NAME}"
    _, created_src = github.created_from_dir[0]
    assert [p.name for p in (created_src / "bundle" / "tools").iterdir()] == [
        "baseline-leakguard.py"
    ]
    assert "baseline-broken.py" in caplog.text


def test_repo_step_marketplace_unreachable_warns_and_proceeds(
    session_factory, config, fake_storage, caplog
):
    """An agent must not fail to exist because the marketplace hiccuped."""
    config.skills_repo = "org-skills"
    session, agent = _make_agent(session_factory)
    github = _marketplace_github(clone_error="marketplace clone exploded")
    ctx = _make_ctx(session, agent, config, _clients(github=github, storage=fake_storage))

    with caplog.at_level(logging.WARNING):
        RepoStep().run(ctx)

    expected_repo = f"{config.github_org}/agent-{AGENT_NAME}"
    assert agent.repo_full_name == expected_repo
    _, created_src = github.created_from_dir[0]
    assert (created_src / "bundle" / "agent.yaml").is_file()
    assert not (created_src / "bundle" / "tools").exists()
    assert "baseline tooling skipped" in caplog.text


# --- AvatarStep -----------------------------------------------------------------


class _CapturingGitHub(FakeGitHub):
    """Snapshots bundle/avatar.png bytes at the moment commit_push is called
    -- the only window in which set_avatar's cloned workdir still exists
    before it's discarded. Same trick as test_api_avatars.py's own
    _CapturingGitHub, replicated locally rather than imported cross-file."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured_avatar_bytes: bytes | None = None

    def commit_push(self, workdir: Path, message: str) -> bool:
        avatar_path = workdir / "bundle" / "avatar.png"
        if avatar_path.is_file():
            self.captured_avatar_bytes = avatar_path.read_bytes()
        return super().commit_push(workdir, message)


def _avatar_agent(session_factory, **kwargs):
    kwargs.setdefault("repo_full_name", f"acme/agent-{AGENT_NAME}")
    return _make_agent(session_factory, **kwargs)


def test_avatar_step_generates_and_stores_the_portrait(session_factory, config):
    session, agent = _avatar_agent(session_factory)
    github = _CapturingGitHub()
    image = FakeImage()
    ctx = _make_ctx(session, agent, config, _clients(github=github, image=image))

    detail = AvatarStep().run(ctx)

    assert detail
    assert github.captured_avatar_bytes == FAKE_IMAGE_PNG_BYTES
    assert agent.avatar_png == base64.b64encode(FAKE_IMAGE_PNG_BYTES).decode("ascii")


def test_avatar_step_skips_when_image_client_missing(session_factory, config):
    session, agent = _avatar_agent(session_factory)
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github, image=None))

    with pytest.raises(StepSkipped, match="not available"):
        AvatarStep().run(ctx)

    assert agent.avatar_png is None
    assert github.cloned == []


def test_avatar_step_skips_on_generation_error(session_factory, config, caplog):
    """The raised StepSkipped reason must be a fixed generic one-liner, never
    `str(exc)` -- it lands verbatim on AgentStep.error, which GET /api/agents
    serves to any viewer, and a backend's raw exception text is not for
    public consumption. The raw exception is still logged server-side."""
    session, agent = _avatar_agent(session_factory)
    github = FakeGitHub()
    raw_error = "the picture didn't come through: backend said no"
    image = FakeImage(generate_error=raw_error)
    ctx = _make_ctx(session, agent, config, _clients(github=github, image=image))

    with caplog.at_level(logging.WARNING), pytest.raises(StepSkipped) as excinfo:
        AvatarStep().run(ctx)

    assert str(excinfo.value) == "picture generation failed"
    assert raw_error not in str(excinfo.value)
    assert any(raw_error in record.message for record in caplog.records)
    assert agent.avatar_png is None
    assert github.cloned == []


def test_avatar_step_skips_when_avatar_already_present(session_factory, config):
    existing = base64.b64encode(b"already-there").decode("ascii")
    session, agent = _avatar_agent(session_factory)
    agent.avatar_png = existing
    session.commit()
    github = FakeGitHub()
    image = FakeImage()
    ctx = _make_ctx(session, agent, config, _clients(github=github, image=image))

    with pytest.raises(StepSkipped, match="already"):
        AvatarStep().run(ctx)

    assert image.prompts == []
    assert github.cloned == []
    assert agent.avatar_png == existing


def test_avatar_step_skips_on_oversize_png(session_factory, config):
    session, agent = _avatar_agent(session_factory)
    github = FakeGitHub()
    oversize = b"x" * (AVATAR_MAX_BYTES + 1)
    image = FakeImage(image_bytes=oversize)
    ctx = _make_ctx(session, agent, config, _clients(github=github, image=image))

    with pytest.raises(StepSkipped, match="cap"):
        AvatarStep().run(ctx)

    assert agent.avatar_png is None
    # Rejected before any store, same as the route's own size check.
    assert github.cloned == []


def test_avatar_step_skips_on_repo_push_failure(session_factory, config, caplog):
    """Same sanitization requirement as the generation-error test above: a
    `set_avatar` ClientError can carry a full git/gh command line and raw
    stderr, so the StepSkipped reason must be the fixed generic copy, never
    that raw text -- which is still logged server-side."""
    session, agent = _avatar_agent(session_factory)
    raw_error = "gh: rate limited (command: gh repo create ...; stderr: 403)"
    github = FakeGitHub(clone_error=raw_error)
    image = FakeImage()
    ctx = _make_ctx(session, agent, config, _clients(github=github, image=image))

    with caplog.at_level(logging.WARNING), pytest.raises(StepSkipped) as excinfo:
        AvatarStep().run(ctx)

    assert str(excinfo.value) == "couldn't save the picture"
    assert raw_error not in str(excinfo.value)
    assert any(raw_error in record.message for record in caplog.records)
    assert agent.avatar_png is None


def test_avatar_step_survives_malformed_purpose_tags(session_factory, config, caplog):
    """Final-review fix: a malformed `purpose_tags` column (`decode_tags`
    raises on bad JSON) must degrade to the same generic, tag-free prompt
    an unrecognized persona already falls back to -- never propagate out
    of this step and fail the whole agent through `_run_pipeline_body`'s
    generic FAILED branch (kobo_api.provisioner)."""
    session, agent = _avatar_agent(session_factory, purpose_tags="{not valid json")
    github = _CapturingGitHub()
    image = FakeImage()
    ctx = _make_ctx(session, agent, config, _clients(github=github, image=image))

    with caplog.at_level(logging.WARNING):
        detail = AvatarStep().run(ctx)

    assert detail
    assert agent.avatar_png == base64.b64encode(FAKE_IMAGE_PNG_BYTES).decode("ascii")
    assert image.prompts
    assert f"A {agent.persona} agent." in image.prompts[0]
    assert "themes:" not in image.prompts[0]


def test_avatar_step_survives_persona_lookup_error(session_factory, config, monkeypatch, caplog):
    """The other half of the same guard: a `get_entry` failure (a catalog/
    search backend hiccup) must degrade exactly like a persona that simply
    isn't there -- the portrait still gets generated with the generic
    fallback description, never a skip and never a failed agent."""
    import kobo_api.steps as steps_module

    def boom(engine, kind, id, team=None):
        raise RuntimeError("catalog backend unavailable")

    monkeypatch.setattr(steps_module, "get_entry", boom)

    session, agent = _avatar_agent(session_factory)
    github = _CapturingGitHub()
    image = FakeImage()
    ctx = _make_ctx(session, agent, config, _clients(github=github, image=image))

    with caplog.at_level(logging.WARNING):
        detail = AvatarStep().run(ctx)

    assert detail
    assert agent.avatar_png == base64.b64encode(FAKE_IMAGE_PNG_BYTES).decode("ascii")
    assert image.prompts
    assert f"A {agent.persona} agent." in image.prompts[0]
    assert any("catalog backend unavailable" in record.message for record in caplog.records)


def test_pipeline_orders_avatar_right_after_repo():
    names = [step.name for step in PIPELINE]
    assert names.index(AvatarStep().name) == names.index(RepoStep().name) + 1


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


def test_onboard_step_create_app_receives_expected_arguments(session_factory, config):
    # `workload_class` became an argument when connected agents arrived
    # (design 2026-09-21), since a team's own code may serve no HTTP port.
    # A composed agent always runs the room's one runtime, which always
    # does -- so it asks for the ordinary class, exactly as before.
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    gojira = FakeGojira(activate_after=1)
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira))

    OnboardStep().run(ctx)

    assert gojira.create_app_calls == [
        {
            "name": AGENT_NAME,
            "repo_full_name": repo,
            "cluster": config.gojira_cluster,
            "namespace": AGENT_NAME,
            "workload_class": "agent",
            "auto_merge_setup_pr": True,
        }
    ]


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


# --- AccessStep: agent memory replication grant (resilience arc, Task 14) --------
#
# The agent's service account gets write access to a per-agent replica
# prefix and nothing wider, granted by this step -- mirroring the exact
# ensure_gsa/bind_workload_identity/add_to_group shape above, just one more
# call, gated on `config.memory_replica_bucket` (empty = feature off).


def test_access_step_grants_memory_replica_prefix_when_bucket_configured(
    session_factory, config
):
    cfg = config.model_copy(update={"memory_replica_bucket": "kobo-memory-replicas"})
    session, agent = _make_agent(session_factory)
    cloud = FakeCloud(project=cfg.gcp_project)
    ctx = _make_ctx(session, agent, cfg, _clients(cloud=cloud))

    AccessStep().run(ctx)

    expected_email = f"agent-{AGENT_NAME}@{cfg.gcp_project}.iam.gserviceaccount.com"
    assert cloud.calls == [
        f"ensure_gsa:agent-{AGENT_NAME}",
        f"bind_workload_identity:{expected_email}:{AGENT_NAME}/agent-{AGENT_NAME}",
        f"add_to_group:{expected_email}:{cfg.model_group}",
        f"grant_bucket_prefix_write:{expected_email}:kobo-memory-replicas:{AGENT_NAME}/",
        f"grant_bucket_list:{expected_email}:kobo-memory-replicas",
    ]


def test_access_step_grants_prefix_scoped_to_this_agents_own_name(session_factory, config):
    """Two different agents against the SAME configured bucket must be
    granted two DIFFERENT prefixes -- never a bucket-wide grant either
    agent's identity could use to reach the other's replica."""
    cfg = config.model_copy(update={"memory_replica_bucket": "kobo-memory-replicas"})
    session, agent = _make_agent(session_factory, name="rae")
    cloud = FakeCloud(project=cfg.gcp_project)
    ctx = _make_ctx(session, agent, cfg, _clients(cloud=cloud))

    AccessStep().run(ctx)

    expected_email = f"agent-rae@{cfg.gcp_project}.iam.gserviceaccount.com"
    # The write grant stays prefix-scoped to this agent's own name; the
    # trailing list grant (Important 9 fix, below) is bucket-wide by
    # design (list is not scopable by object-name condition), so it names
    # only the bucket, never the agent's prefix.
    assert cloud.calls[-2] == f"grant_bucket_prefix_write:{expected_email}:kobo-memory-replicas:rae/"
    assert cloud.calls[-1] == f"grant_bucket_list:{expected_email}:kobo-memory-replicas"


def test_access_step_no_memory_replica_grant_when_bucket_unset(session_factory, config):
    """Off arm: pinned as part of the same call-order assertion the
    original AccessStep test above already makes -- when the bucket is
    unset, the call list is EXACTLY the pre-Task-14 three calls, nothing
    appended."""
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
    assert not any(c.startswith("grant_bucket_prefix_write") for c in cloud.calls)
    assert not any(c.startswith("grant_bucket_list") for c in cloud.calls)


def test_access_step_memory_replica_grant_includes_bucket_level_list_access(
    session_factory, config
):
    """Important 9 fix (paired with Critical 4): a per-prefix conditioned
    `roles/storage.objectAdmin` grant alone denies `storage.objects.list`
    -- list is a bucket-level operation, not scopable by the object-name
    CEL condition the way get/create/delete are -- and litestream needs
    to list the replica prefix's existing generations on startup. The
    grant now ALSO includes an unconditioned, bucket-wide, list-only
    `roles/storage.legacyBucketReader` binding, issued in the same
    `grant_bucket_prefix_write` call AccessStep already made -- widening
    list access room-wide (every agent's GSA can list the bucket's object
    names) while keeping read/write itself conditioned to each agent's
    own prefix. The restore drill remains the live proof this actually
    unblocks litestream; this test only pins that AccessStep asks for
    both bindings."""
    cfg = config.model_copy(update={"memory_replica_bucket": "kobo-memory-replicas"})
    session, agent = _make_agent(session_factory)
    cloud = FakeCloud(project=cfg.gcp_project)
    ctx = _make_ctx(session, agent, cfg, _clients(cloud=cloud))

    AccessStep().run(ctx)

    expected_email = f"agent-{AGENT_NAME}@{cfg.gcp_project}.iam.gserviceaccount.com"
    assert cloud.calls[-2:] == [
        f"grant_bucket_prefix_write:{expected_email}:kobo-memory-replicas:{AGENT_NAME}/",
        f"grant_bucket_list:{expected_email}:kobo-memory-replicas",
    ]


# --- HelloStep (persona-metadata plan, Task 3: capability earned + card lockstep) --

HELLO_REPO = "acme/agent-clio"


def _hello_ctx(
    session_factory,
    config,
    fake_storage,
    *,
    github=None,
    name=None,
    agent_client=None,
    cabal=None,
):
    session, agent = _make_agent(session_factory, repo_full_name=HELLO_REPO)
    clients = _clients(
        github=github if github is not None else FakeGitHub(),
        storage=fake_storage,
        agent=agent_client if agent_client is not None else FakeAgent(),
        kubectl=FakeKubectl(),
        name=name,
        cabal=cabal,
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


# --- HelloStep contact path (colleagues switchboard plan, Task 4b): the relay ride --
#
# Whole-arc review finding C-1 / operator ruling 2026-08-19: agents compose
# LOCKED, so first contact must ride the switchboard's relay once one is
# configured, using the exact same envelope builder and reply-extraction
# helper the chat route uses. Selection is the same two-gate discipline as
# the chat route: `cabal_enabled(config)` AND `ctx.clients.cabal is not
# None`. Disabled (either gate false) leaves the legacy direct path
# completely unchanged (Global Constraints: org keys optional = feature off
# = legacy behavior).

HELLO_CABAL_ENDPOINT_TEMPLATE = "http://agent-{name}.default.svc.cluster.local"


def _hello_cabal_enabled_config(config):
    return config.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": HELLO_CABAL_ENDPOINT_TEMPLATE,
        }
    )


def test_hello_step_direct_path_unchanged_when_switchboard_disabled(
    session_factory, config, fake_storage
):
    """The shared `config` fixture carries neither switchboard org key —
    HelloStep's contact path must be the same direct `ctx.clients.agent.
    hello` call it always was, byte-untouched, even with a cabal client
    sitting unused on `Clients`."""
    fake_agent_client = FakeAgent(reply="hello yourself")
    fake_cabal = FakeCabal()
    ctx, agent = _hello_ctx(
        session_factory, config, fake_storage, agent_client=fake_agent_client, cabal=fake_cabal
    )

    HelloStep().run(ctx)

    assert agent.first_reply == "hello yourself"
    assert fake_agent_client.calls == [(agent.name, agent.name, "hello! how did you get here?")]
    assert fake_cabal.relay_calls == []


def test_hello_step_relays_through_the_switchboard_when_enabled(
    session_factory, config, fake_storage, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _hello_cabal_enabled_config(config)
    fake_agent_client = FakeAgent(reply="never used")
    fake_cabal = FakeCabal(
        relay_result={"status": {"message": {"parts": [{"text": "hi from the switchboard"}]}}}
    )
    ctx, agent = _hello_ctx(
        session_factory, cfg, fake_storage, agent_client=fake_agent_client, cabal=fake_cabal
    )

    HelloStep().run(ctx)

    assert agent.first_reply == "hi from the switchboard"
    # relay_calls[0] is HelloStep's own first-contact relay; relay_calls[1]
    # is the capability-distill self.md refresh tail (Task 6) firing right
    # after -- same `clients.cabal`, a second, distinct relay call. This
    # canned `relay_result` carries no "state" key, so that second call
    # reads as not-completed and is logged and swallowed, same discipline
    # as any other roster-refresh miss (see test_refresh_after_capability_
    # distill_* for that path's own dedicated coverage).
    assert len(fake_cabal.relay_calls) == 2
    assert fake_cabal.relay_calls[0]["name"] == agent.name
    # the legacy direct path never runs when the relay handles first contact
    assert fake_agent_client.calls == []


def test_hello_step_falls_back_to_direct_path_when_cabal_client_missing(
    session_factory, config, fake_storage, monkeypatch
):
    """`cabal_enabled(config)` true but `ctx.clients.cabal` is None (wired
    without a cabal client despite the org config being set) must not
    crash — falls back to the direct path exactly like an unwired room."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _hello_cabal_enabled_config(config)
    fake_agent_client = FakeAgent(reply="hello yourself")
    ctx, agent = _hello_ctx(
        session_factory, cfg, fake_storage, agent_client=fake_agent_client, cabal=None
    )

    HelloStep().run(ctx)

    assert agent.first_reply == "hello yourself"
    assert len(fake_agent_client.calls) == 1


def test_hello_step_ignores_cabal_client_when_switchboard_not_enabled(
    session_factory, config, fake_storage
):
    """A cabal client present on `Clients` but `cabal_enabled(config)` false
    (no org keys configured) must still take the direct path — the
    client's mere presence is not the selection signal."""
    fake_cabal = FakeCabal()
    fake_agent_client = FakeAgent(reply="hello yourself")
    ctx, agent = _hello_ctx(
        session_factory, config, fake_storage, agent_client=fake_agent_client, cabal=fake_cabal
    )

    HelloStep().run(ctx)

    assert agent.first_reply == "hello yourself"
    assert fake_cabal.relay_calls == []
    assert len(fake_agent_client.calls) == 1


def test_hello_step_relay_envelope_matches_the_shared_builder_verbatim(
    session_factory, config, fake_storage, monkeypatch
):
    """The relay path must reuse `_build_task_envelope` (kobo_api.clients)
    verbatim rather than fork the shape — pinned by freezing `uuid4` (the
    envelope's only non-deterministic field) and comparing the envelope the
    relay actually receives against calling that same builder directly."""
    import uuid

    from kobo_api.clients import _build_task_envelope

    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=0))
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _hello_cabal_enabled_config(config)
    fake_cabal = FakeCabal(relay_result={"status": {"message": {"parts": [{"text": "ok"}]}}})
    ctx, _agent = _hello_ctx(session_factory, cfg, fake_storage, cabal=fake_cabal)

    HelloStep().run(ctx)

    expected_envelope = _build_task_envelope(
        [{"role": "user", "text": "hello! how did you get here?"}]
    )
    assert fake_cabal.relay_calls[0]["task_envelope"] == expected_envelope


def test_hello_step_relay_failure_fails_the_step_retryable(
    session_factory, config, fake_storage, monkeypatch
):
    """Relay failure = the hello step FAILS (retryable) exactly like a
    direct-path failure — same failure semantics, plain copy: it raises
    rather than being swallowed, and `first_reply` stays unset since the
    reply never came back."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _hello_cabal_enabled_config(config)
    fake_cabal = FakeCabal(relay_error="switchboard unreachable")
    ctx, agent = _hello_ctx(session_factory, cfg, fake_storage, cabal=fake_cabal)

    with pytest.raises(CabalError):
        HelloStep().run(ctx)

    assert agent.first_reply is None


def test_hello_step_relay_malformed_reply_fails_the_step_retryable(
    session_factory, config, fake_storage, monkeypatch
):
    """A relay response with no usable text part (`_extract_relay_reply`
    finding nothing to return) is exactly as much a failure as a transport
    error — same retryable semantics as the transport-failure case above."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _hello_cabal_enabled_config(config)
    fake_cabal = FakeCabal(relay_result={"status": {}})
    ctx, agent = _hello_ctx(session_factory, cfg, fake_storage, cabal=fake_cabal)

    with pytest.raises(CabalError):
        HelloStep().run(ctx)

    assert agent.first_reply is None


# --- HelloStep retry (live incident 2026-08-27): fresh-identity propagation window --
#
# A brand-new agent's minutes-old service account / workload-identity binding
# can still be propagating when HelloStep makes first contact; a manual Retry
# always cleared it untouched. These pin the automatic absorption: two
# failures then a success ends DONE, three failures ends FAILED with the
# final error, and the backoff between attempts uses the module's own
# `_sleep` seam (never a real sleep here) rather than `time.sleep` directly.


class _FlakyAgent:
    """`hello()` raises `ClientError(error)` for the first `fail_times`
    calls, then returns `reply`: scripts the "fails, fails, then a retry
    succeeds" shape the live incident actually showed, which the shared
    `FakeAgent.hello_error` (a constant, every call) can't express."""

    def __init__(
        self,
        *,
        fail_times: int,
        error: str = "google-auth: 403 minting token",
        reply: str = "hello yourself",
    ):
        self._fail_times = fail_times
        self._error = error
        self._reply = reply
        self.calls: list[tuple[str, str, str]] = []

    def hello(self, namespace: str, service: str, text: str) -> str:
        self.calls.append((namespace, service, text))
        if len(self.calls) <= self._fail_times:
            raise ClientError(self._error)
        return self._reply

    def fetch_card(self, namespace: str, service: str) -> dict:
        return {}


class _FlakyCabal:
    """`relay()` counterpart to `_FlakyAgent`, for pinning that the retry
    wraps the relay path too, not just the legacy direct path."""

    def __init__(self, *, fail_times: int, error: str = "switchboard unreachable"):
        self._fail_times = fail_times
        self._error = error
        self.relay_calls: list[dict] = []

    def relay(self, name: str, task_envelope: dict) -> dict:
        self.relay_calls.append({"name": name, "task_envelope": task_envelope})
        if len(self.relay_calls) <= self._fail_times:
            raise CabalError(self._error)
        return {"status": {"message": {"parts": [{"text": "hi from the switchboard"}]}}}


def _hello_sleep_recorder(monkeypatch):
    import kobo_api.steps as steps_module

    sleep_calls: list[int] = []
    monkeypatch.setattr(steps_module, "_sleep", sleep_calls.append)
    return sleep_calls


def test_hello_step_retries_direct_path_and_succeeds_after_two_failures(
    session_factory, config, fake_storage, monkeypatch
):
    """Pin: two failures then a success ends DONE (the step returns
    normally), the reply from the successful attempt is recorded, and the
    backoff seam fired exactly twice (once between attempts 1-2, once
    between 2-3, never after the final success)."""
    sleep_calls = _hello_sleep_recorder(monkeypatch)
    flaky_agent = _FlakyAgent(fail_times=2)
    ctx, agent = _hello_ctx(session_factory, config, fake_storage, agent_client=flaky_agent)

    detail = HelloStep().run(ctx)  # must not raise

    assert agent.first_reply == "hello yourself"
    assert "hello yourself" in detail
    assert len(flaky_agent.calls) == HELLO_RETRY_ATTEMPTS == 3
    assert sleep_calls == [HELLO_RETRY_BACKOFF_S, HELLO_RETRY_BACKOFF_S]


def test_hello_step_exhausts_retries_and_fails_with_the_last_error(
    session_factory, config, fake_storage, monkeypatch
):
    """Pin: three failures ends FAILED with the final error recorded,
    unchanged shape: still a plain `ClientError` (same as an unretried
    failure always raised), not wrapped or swallowed, after exactly
    `HELLO_RETRY_ATTEMPTS` attempts (not more, not fewer)."""
    sleep_calls = _hello_sleep_recorder(monkeypatch)
    flaky_agent = _FlakyAgent(fail_times=3, error="google-auth: 403 minting token")
    ctx, agent = _hello_ctx(session_factory, config, fake_storage, agent_client=flaky_agent)

    with pytest.raises(ClientError, match="google-auth: 403 minting token"):
        HelloStep().run(ctx)

    assert agent.first_reply is None
    assert len(flaky_agent.calls) == HELLO_RETRY_ATTEMPTS == 3
    assert sleep_calls == [HELLO_RETRY_BACKOFF_S, HELLO_RETRY_BACKOFF_S]


def test_hello_step_retries_the_relay_path_too_and_succeeds(
    session_factory, config, fake_storage, monkeypatch
):
    """The retry wraps whichever contact path is selected, pinned here on
    the relay path (the incident's own wording: "on a relay failure,
    retries with backoff") so the loop isn't accidentally scoped to only
    the legacy direct path."""
    sleep_calls = _hello_sleep_recorder(monkeypatch)
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _hello_cabal_enabled_config(config)
    flaky_cabal = _FlakyCabal(fail_times=2)
    ctx, agent = _hello_ctx(session_factory, cfg, fake_storage, cabal=flaky_cabal)

    HelloStep().run(ctx)  # must not raise

    assert agent.first_reply == "hi from the switchboard"
    # The first HELLO_RETRY_ATTEMPTS calls are hello's own first-contact
    # retries (two failures then a success); the capability-distill self.md
    # refresh tail (Task 6) fires one further, distinct relay call right
    # after, over this same `clients.cabal` -- not itself a hello attempt,
    # so it is not counted against HELLO_RETRY_ATTEMPTS.
    assert len(flaky_cabal.relay_calls) == HELLO_RETRY_ATTEMPTS + 1 == 4
    assert sleep_calls == [HELLO_RETRY_BACKOFF_S, HELLO_RETRY_BACKOFF_S]


# --- HelloStep capability tail: directory resync (colleagues switchboard plan, ----
# Task 4b) --------------------------------------------------------------------------
#
# DirectoryStep now runs BEFORE HelloStep, so the card it registers
# legitimately lacks a capability line yet — this tail backfills the
# directory's own copy once the capability is earned and committed.


def test_hello_step_tail_resyncs_directory_after_successful_card_commit(
    session_factory, config, fake_storage, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _hello_cabal_enabled_config(config)
    github = FakeGitHub()
    fake_name = FakeName(capability="Helps with word games and small talk.")
    fake_cabal = FakeCabal(
        relay_result={"status": {"message": {"parts": [{"text": "hello yourself"}]}}}
    )
    ctx, agent = _hello_ctx(
        session_factory, cfg, fake_storage, github=github, name=fake_name, cabal=fake_cabal
    )

    HelloStep().run(ctx)

    assert agent.capability == "Helps with word games and small talk."
    assert github.pushes[-1][1] == "kobo: directory details updated"
    # the resync re-registers with the switchboard after the card commit
    assert len(fake_cabal.graduate_calls) == 1
    assert fake_cabal.graduate_calls[0]["name"] == agent.name


def test_hello_step_tail_resync_disabled_switchboard_never_touches_cabal(
    session_factory, config, fake_storage
):
    """`cabal_enabled` false (the shared `config` fixture carries neither
    org key) — the tail must never even attempt a resync."""
    fake_cabal = FakeCabal()
    fake_name = FakeName(capability="Helps.")
    ctx, agent = _hello_ctx(session_factory, config, fake_storage, name=fake_name, cabal=fake_cabal)

    HelloStep().run(ctx)

    assert agent.capability == "Helps."
    assert fake_cabal.graduate_calls == []
    assert fake_cabal.relay_calls == []


def test_hello_step_tail_resync_skipped_when_card_commit_fails(
    session_factory, config, fake_storage, monkeypatch
):
    """The resync only fires after a SUCCESSFUL card commit — a raising
    GitHub client (card commit failure) must leave the resync untried."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _hello_cabal_enabled_config(config)
    raising_github = FakeGitHub(clone_error="gh: rate limited")
    fake_cabal = FakeCabal(
        relay_result={"status": {"message": {"parts": [{"text": "hello yourself"}]}}}
    )
    ctx, agent = _hello_ctx(
        session_factory, cfg, fake_storage, github=raising_github, cabal=fake_cabal
    )

    HelloStep().run(ctx)  # must not raise

    assert agent.capability is not None
    assert fake_cabal.graduate_calls == []


def test_hello_step_tail_resync_raising_cabal_never_fails_hello_or_alters_capability(
    session_factory, config, fake_storage, monkeypatch, caplog
):
    """Log-and-swallow: a raising cabal client must never fail the hello
    step nor alter capability storage (Global Constraints)."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _hello_cabal_enabled_config(config)
    github = FakeGitHub()
    fake_name = FakeName(capability="Helps with word games and small talk.")
    fake_cabal = FakeCabal(
        relay_result={"status": {"message": {"parts": [{"text": "hello yourself"}]}}},
        graduate_error="switchboard unreachable",
    )
    ctx, agent = _hello_ctx(
        session_factory, cfg, fake_storage, github=github, name=fake_name, cabal=fake_cabal
    )

    with caplog.at_level("WARNING"):
        detail = HelloStep().run(ctx)  # must not raise

    assert "hello yourself" in detail
    assert agent.first_reply == "hello yourself"
    assert agent.capability == "Helps with word games and small talk."
    assert github.pushes[-1][1] == "kobo: directory details updated"
    assert "directory resync failed" in caplog.text


# --- DirectoryStep (colleagues switchboard plan, Task 2: the directory seam) ------

DIRECTORY_REPO = "acme/agent-clio"
DIRECTORY_ENDPOINT_TEMPLATE = "http://agent-{name}.default.svc.cluster.local"


def _directory_ctx(session_factory, config, *, github=None, cabal=None):
    session, agent = _make_agent(session_factory, repo_full_name=DIRECTORY_REPO)
    clients = _clients(
        github=github if github is not None else FakeGitHub(),
        cabal=cabal if cabal is not None else FakeCabal(),
    )
    return _make_ctx(session, agent, config, clients), agent


def _cabal_enabled_config(config):
    """`config` (the shared fixture) plus the two org keys DirectoryStep's
    `cabal_enabled` check needs — the caller must also monkeypatch
    CABAL_OPERATOR_TOKEN, the third leg, before this reads as enabled."""
    return config.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": DIRECTORY_ENDPOINT_TEMPLATE,
        }
    )


def test_directory_step_disabled_is_a_no_op_done(session_factory, config):
    """`cabal_enabled` false (the shared `config` fixture carries neither
    org key) — the step must complete without raising and without ever
    touching cabal, so a room with no switchboard configured stays exactly
    as green on this step as it was before the step existed."""
    fake_cabal = FakeCabal()
    ctx, _agent = _directory_ctx(session_factory, config, cabal=fake_cabal)

    DirectoryStep().run(ctx)  # must not raise

    assert fake_cabal.graduate_calls == []


def test_directory_step_registers_when_switchboard_enabled(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    fake_cabal = FakeCabal()
    ctx, agent = _directory_ctx(session_factory, cfg, cabal=fake_cabal)

    DirectoryStep().run(ctx)

    assert len(fake_cabal.graduate_calls) == 1
    call = fake_cabal.graduate_calls[0]
    assert call["name"] == agent.name
    assert call["endpoint"] == f"http://agent-{agent.name}.default.svc.cluster.local"
    assert call["audience"] == agent.name


def test_directory_step_card_fetch_failure_raises_for_retry(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    github = FakeGitHub(clone_error="gh: rate limited")
    ctx, _agent = _directory_ctx(session_factory, cfg, github=github)

    with pytest.raises(ClientError):
        DirectoryStep().run(ctx)


def test_directory_step_graduate_failure_raises_for_retry(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    fake_cabal = FakeCabal(graduate_error="switchboard unreachable")
    ctx, _agent = _directory_ctx(session_factory, cfg, cabal=fake_cabal)

    with pytest.raises(CabalError):
        DirectoryStep().run(ctx)


# --- AccessStep without a model group (2026-10-01) ----------------------------------
#
# A project with no Workspace has no Google Groups to join. The room then
# grants the Vertex role on vertex_project to the agent's identity directly,
# and the rest of the step is unchanged.


def test_access_step_without_a_model_group_grants_the_vertex_role_directly(
    session_factory, config
):
    config.model_group = ""
    config.vertex_project = "models-project"
    session, agent = _make_agent(session_factory)
    cloud = FakeCloud(project=config.gcp_project)
    ctx = _make_ctx(session, agent, config, _clients(cloud=cloud))

    AccessStep().run(ctx)

    email = f"agent-{AGENT_NAME}@{config.gcp_project}.iam.gserviceaccount.com"
    assert cloud.calls == [
        f"ensure_gsa:agent-{AGENT_NAME}",
        f"bind_workload_identity:{email}:{AGENT_NAME}/agent-{AGENT_NAME}",
        f"grant_model_access:{email}:models-project",
    ]
    assert (email, "models-project") in cloud.model_grants
    assert cloud.group_members == set()

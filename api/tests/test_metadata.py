"""Tests for kobo_api.metadata (persona-metadata plan, Task 3): capability
generation tiers (model-drafted vs. deterministic fallback), the
PATCH-input cleanup helper, the two generation-input gatherers
(skill_descriptions_for_agent / tooling_filenames_for), and the card-commit
delivery helper (commit_card_metadata) — all against the fakes
(tests/fakes.py), no real Vertex/git/kubectl involved.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kobo_api.clients import ClientError
from kobo_api.metadata import (
    CAPABILITY_MAX_LEN,
    clean_capability_text,
    commit_card_metadata,
    generate_capability,
    skill_descriptions_for_agent,
    tooling_filenames_for,
)
from kobo_api.search import ingest_indexes
from tests.fakes import (
    FAKE_LOCAL_HEAD_DEFAULT,
    FakeAgent,
    FakeGitHub,
    FakeKubectl,
    FakeName,
)

# --- generate_capability: tiers --------------------------------------------------


def test_generate_capability_uses_model_reply_when_available():
    model = FakeName(capability="Helps customers resolve billing issues.")
    result = generate_capability(model, "You are a billing helper.", [], [])
    assert result == "Helps customers resolve billing issues."
    assert model.capability_calls == [("You are a billing helper.", [], [])]


def test_generate_capability_passes_skills_and_tooling_through_to_model():
    model = FakeName(capability="Does things.")
    generate_capability(
        model, "persona body", ["Plays word games."], ["notifier.py"]
    )
    assert model.capability_calls == [("persona body", ["Plays word games."], ["notifier.py"])]


def test_generate_capability_falls_back_when_model_client_is_none():
    result = generate_capability(None, "You are sam. Handles billing.", [], [])
    assert result == "You are sam"


def test_generate_capability_falls_back_when_model_raises_client_error():
    model = FakeName(capability_error="model unavailable")
    result = generate_capability(model, "You are sam. Handles billing.", [], [])
    assert result == "You are sam"


def test_generate_capability_falls_back_when_model_reply_is_blank_after_cleanup():
    model = FakeName(capability="   ")
    result = generate_capability(model, "You are sam. Handles billing.", [], [])
    assert result == "You are sam"


def test_generate_capability_falls_back_when_model_reply_is_not_a_string():
    class _WeirdModel:
        def capability(self, persona_body, skill_descriptions, tooling_filenames):
            return {"not": "a string"}

    result = generate_capability(_WeirdModel(), "You are sam. Handles billing.", [], [])
    assert result == "You are sam"


# --- generate_capability: clamp + strip (model tier) -----------------------------


def test_generate_capability_clamps_model_reply_to_max_len():
    model = FakeName(capability="x" * (CAPABILITY_MAX_LEN + 50))
    result = generate_capability(model, "persona", [], [])
    assert len(result) == CAPABILITY_MAX_LEN


def test_generate_capability_strips_em_and_en_dashes_from_model_reply():
    model = FakeName(capability="Quick—reliable– and steady.")
    result = generate_capability(model, "persona", [], [])
    assert "—" not in result
    assert "–" not in result
    assert result == "Quick-reliable- and steady."


def test_generate_capability_collapses_whitespace_from_model_reply():
    model = FakeName(capability="Helps   with\n\nbilling   questions.")
    result = generate_capability(model, "persona", [], [])
    assert result == "Helps with billing questions."


# --- generate_capability: fallback tier (persona body's first sentence) ---------


def test_generate_capability_fallback_uses_first_sentence_of_persona_body():
    result = generate_capability(
        None, "You are sam. You also handle refunds and returns.", [], []
    )
    assert result == "You are sam"


def test_generate_capability_fallback_clamps_to_max_len():
    long_sentence = "You are sam and you do " + "a lot of stuff " * 20
    result = generate_capability(None, long_sentence, [], [])
    assert len(result) <= CAPABILITY_MAX_LEN


def test_generate_capability_fallback_strips_dashes_from_persona_body():
    result = generate_capability(None, "You are sam—a billing helper.", [], [])
    assert "—" not in result
    assert result == "You are sam-a billing helper"


def test_generate_capability_fallback_whole_text_when_no_sentence_boundary():
    result = generate_capability(None, "You are sam and you help with billing", [], [])
    assert result == "You are sam and you help with billing"


def test_generate_capability_fallback_strips_leading_boundary_punctuation():
    result = generate_capability(None, ". You are sam.", [], [])
    assert result == "You are sam."


def test_generate_capability_fallback_collapses_whitespace():
    result = generate_capability(None, "You   are  sam.   More text.", [], [])
    assert result == "You are sam"


def test_generate_capability_fallback_newline_counts_as_sentence_boundary():
    # A paragraph break is itself a boundary — the fallback never reaches
    # past it into a second paragraph even with no ./!/? in sight yet.
    result = generate_capability(None, "You are sam\nMore text after a break.", [], [])
    assert result == "You are sam"


# --- clean_capability_text (PATCH .../metadata input cleanup) -------------------


def test_clean_capability_text_strips_dashes_and_collapses_whitespace():
    assert clean_capability_text("Helps with—billing   stuff.") == "Helps with-billing stuff."


def test_clean_capability_text_none_when_empty_after_cleanup():
    assert clean_capability_text("   ") is None


def test_clean_capability_text_none_when_still_over_max_len_after_cleanup():
    assert clean_capability_text("x" * (CAPABILITY_MAX_LEN + 1)) is None


def test_clean_capability_text_accepts_exactly_max_len():
    text = "x" * CAPABILITY_MAX_LEN
    assert clean_capability_text(text) == text


def test_clean_capability_text_plain_sentence_passes_through():
    assert clean_capability_text("Helps with billing questions.") == (
        "Helps with billing questions."
    )


# --- skill_descriptions_for_agent -------------------------------------------------

SKILLS_BUCKET = "skills-bucket"
SAMPLE_SKILL_TEAM = "shared"
SAMPLE_SKILL_ID = "word-games"


@pytest.fixture
def engine(session_factory):
    return session_factory().get_bind()


def _ingest_sample_skill(engine, fake_storage, config):
    ingest_indexes(engine, fake_storage, {"skill": config.skills_bucket})


def test_skill_descriptions_for_agent_looks_up_catalog_descriptions(
    engine, fake_storage, config
):
    _ingest_sample_skill(engine, fake_storage, config)
    agent = SimpleNamespace(
        skills=[{"name": SAMPLE_SKILL_ID, "team": SAMPLE_SKILL_TEAM, "sha": "seedsha"}]
    )

    result = skill_descriptions_for_agent(engine, agent)

    assert result == ["Plays word games with the user."]


def test_skill_descriptions_for_agent_empty_when_no_skills(engine, fake_storage, config):
    _ingest_sample_skill(engine, fake_storage, config)
    agent = SimpleNamespace(skills=[])

    assert skill_descriptions_for_agent(engine, agent) == []


def test_skill_descriptions_for_agent_skips_local_source(engine, fake_storage, config):
    _ingest_sample_skill(engine, fake_storage, config)
    agent = SimpleNamespace(skills=[{"name": "custom-skill", "source": "local"}])

    assert skill_descriptions_for_agent(engine, agent) == []


def test_skill_descriptions_for_agent_skips_vanished_skill(engine, fake_storage, config):
    _ingest_sample_skill(engine, fake_storage, config)
    agent = SimpleNamespace(
        skills=[{"name": "ghost-skill", "team": SAMPLE_SKILL_TEAM, "sha": "x"}]
    )

    assert skill_descriptions_for_agent(engine, agent) == []


def test_skill_descriptions_for_agent_multiple_skills_preserve_order(
    engine, fake_storage, config
):
    fake_storage.write_bytes(
        SKILLS_BUCKET,
        "shared/second-skill/SKILL.md",
        b"---\nname: second-skill\ndescription: Does a second thing.\n---\nBody.\n",
        metadata={"source-sha": "seedsha2"},
        content_type="text/markdown",
    )
    fake_storage.write_bytes(
        SKILLS_BUCKET,
        "index.json",
        json.dumps(
            {
                "entries": [
                    {
                        "id": SAMPLE_SKILL_ID,
                        "name": "Word games",
                        "description": "Plays word games with the user.",
                        "team": SAMPLE_SKILL_TEAM,
                        "tags": ["fun"],
                        "sha": "seedsha",
                        "builtin": False,
                    },
                    {
                        "id": "second-skill",
                        "name": "Second skill",
                        "description": "Does a second thing.",
                        "team": SAMPLE_SKILL_TEAM,
                        "tags": [],
                        "sha": "seedsha2",
                        "builtin": False,
                    },
                ]
            }
        ).encode(),
        content_type="application/json",
    )
    ingest_indexes(engine, fake_storage, {"skill": SKILLS_BUCKET})
    agent = SimpleNamespace(
        skills=[
            {"name": SAMPLE_SKILL_ID, "team": SAMPLE_SKILL_TEAM, "sha": "seedsha"},
            {"name": "second-skill", "team": SAMPLE_SKILL_TEAM, "sha": "seedsha2"},
        ]
    )

    result = skill_descriptions_for_agent(engine, agent)

    assert result == ["Plays word games with the user.", "Does a second thing."]


# --- tooling_filenames_for ---------------------------------------------------------

REPO = "acme/agent-clio"
NAMESPACE = "clio"
DEPLOY = "clio"


def test_tooling_filenames_for_returns_sorted_names():
    github = FakeGitHub(tooling_files={"b_tool.py": "b", "a_tool.py": "a"})
    result = tooling_filenames_for(
        github, FakeKubectl(), FakeAgent(), repo=REPO, namespace=NAMESPACE, deploy=DEPLOY
    )
    assert result == ["a_tool.py", "b_tool.py"]


def test_tooling_filenames_for_empty_when_no_tools_dir():
    github = FakeGitHub()
    result = tooling_filenames_for(
        github, FakeKubectl(), FakeAgent(), repo=REPO, namespace=NAMESPACE, deploy=DEPLOY
    )
    assert result == []


def test_tooling_filenames_for_never_raises_on_github_clone_failure():
    github = FakeGitHub(clone_error="rate limited")
    result = tooling_filenames_for(
        github, FakeKubectl(), FakeAgent(), repo=REPO, namespace=NAMESPACE, deploy=DEPLOY
    )
    assert result == []


def test_tooling_filenames_for_never_raises_on_agent_unreachable():
    github = FakeGitHub(tooling_files={"tool.py": "x"})
    agent = FakeAgent(card_error="pod unreachable")
    # The enforcement probe fails, but list_tooling itself still succeeds
    # (its own internal contract) — filenames still come through.
    result = tooling_filenames_for(
        github, FakeKubectl(), agent, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY
    )
    assert result == ["tool.py"]


# --- commit_card_metadata -----------------------------------------------------------


def test_commit_card_metadata_clones_updates_commits_pushes_returns_sha():
    github = FakeGitHub()

    sha = commit_card_metadata(
        github, REPO, team="acme-team", purpose_tags=["billing"], capability="Helps."
    )

    assert sha == FAKE_LOCAL_HEAD_DEFAULT
    assert github.cloned[0][0] == REPO
    assert github.pushes[-1][1] == "kobo: directory details updated"


def test_commit_card_metadata_writes_expected_metadata_block(tmp_path):
    captured = {}

    class _CapturingGitHub(FakeGitHub):
        def commit_push(self, workdir, message):
            card = json.loads((workdir / "bundle" / "agent-card.json").read_text())
            captured["metadata"] = card["metadata"]
            captured["name"] = card["name"]
            return super().commit_push(workdir, message)

    github = _CapturingGitHub()

    commit_card_metadata(
        github, REPO, team="acme-team", purpose_tags=["billing", "support"],
        capability="Helps customers.",
    )

    assert captured["metadata"] == {
        "team": "acme-team",
        "purpose_tags": ["billing", "support"],
        "capability": "Helps customers.",
        "runtime_image": "registry.example/agents/daedalus-runtime:old",
    }
    # Every other field survived — proof the whole card wasn't replaced.
    assert captured["name"] == "clio"


def test_commit_card_metadata_preserves_unknown_card_fields(tmp_path):
    captured = {}

    class _CapturingGitHub(FakeGitHub):
        def commit_push(self, workdir, message):
            card = json.loads((workdir / "bundle" / "agent-card.json").read_text())
            captured["card"] = card
            return super().commit_push(workdir, message)

    github = _CapturingGitHub(
        agent_card={
            "name": "clio",
            "description": "Assembled by kobo.",
            "version": "0.1.0",
            "capabilities": [],
            "skills": [{"id": "echo"}],
            "metadata": {"team": None, "purpose_tags": [], "capability": ""},
            "a_future_field": {"nested": True},
        }
    )

    commit_card_metadata(github, REPO, team="acme-team", purpose_tags=[], capability="Helps.")

    assert captured["card"]["a_future_field"] == {"nested": True}
    # Skills are derived from bundle/skills/, not preserved (issue #147):
    # this fake repo has no skill directories, so a card that claimed one
    # is corrected.
    assert captured["card"]["skills"] == []


def test_commit_card_metadata_raises_client_error_on_clone_failure():
    github = FakeGitHub(clone_error="gh: rate limited")

    with pytest.raises(ClientError):
        commit_card_metadata(github, REPO, team="acme-team", purpose_tags=[], capability="Helps.")


def test_commit_card_metadata_normalizes_none_capability_to_empty_string(tmp_path):
    # Fix round 2 (whole-arc review) residual: a team-only PATCH on an
    # unearned agent passes `agent.capability` (nullable, still None until
    # earned) straight through as `capability`. Without normalizing, that
    # writes a literal `null` into the card's `metadata.capability` — out of
    # step with compose's own baked default (`""`, kobo_api.composer.
    # _agent_card). commit_card_metadata's card-commit seam must normalize
    # `capability or ""` so an unearned agent's card always reads "", never
    # null, matching compose parity exactly.
    captured = {}

    class _CapturingGitHub(FakeGitHub):
        def commit_push(self, workdir, message):
            card = json.loads((workdir / "bundle" / "agent-card.json").read_text())
            captured["metadata"] = card["metadata"]
            return super().commit_push(workdir, message)

    github = _CapturingGitHub()

    commit_card_metadata(github, REPO, team="acme-team", purpose_tags=[], capability=None)

    assert captured["metadata"]["capability"] == ""

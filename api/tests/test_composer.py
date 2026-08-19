import json
from pathlib import Path

import pytest
import yaml

from kobo_api.clients import ClientError
from kobo_api.composer import (
    BundleSpec,
    ComposeError,
    compose,
    update_agent_card_metadata,
    update_agent_yaml_auth,
    validate_agent_yaml,
)
from kobo_api.steps import StagedSkill

_RUNTIME_IMAGE = "registry.example/agents/daedalus-runtime:latest"


def _make_spec(
    skills: list[StagedSkill] | None = None,
    *,
    team: str = "shared",
    purpose_tags: list[str] | None = None,
    cabal_url: str | None = None,
) -> BundleSpec:
    return BundleSpec(
        name="sam",
        persona_body="You are sam, a helpful assistant.\n",
        model_id="vertex_ai/claude-sonnet-4-5",
        skills=skills or [],
        runtime_image=_RUNTIME_IMAGE,
        vertex_project="example-model-project",
        vertex_location="us-east5",
        team=team,
        purpose_tags=purpose_tags if purpose_tags is not None else [],
        cabal_url=cabal_url,
    )


def _make_skill(tmp_path: Path, skill_id: str = "echo") -> StagedSkill:
    """A skill already "staged" on local disk — the shape `kobo_api.steps.
    _stage_skills` hands `compose()` after downloading it from the skills
    bucket. Composer tests build this directly rather than going through
    staging, since staging itself is `_stage_skills`'s own concern
    (`tests/test_steps.py`)."""
    skill_dir = tmp_path / "staged" / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: Echoes things.\n---\nEcho skill body.\n"
    )
    (skill_dir / "reference.py").write_text("def helper():\n    return 1\n")
    return StagedSkill(id=skill_id, dir=skill_dir)


# --- compose(): exact file set -----------------------------------------------


def test_compose_writes_exact_file_set_without_skills(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(), dest)

    written = {p.relative_to(dest) for p in dest.rglob("*") if p.is_file()}
    assert written == {
        Path("Dockerfile"),
        Path("bundle/agent.yaml"),
        Path("bundle/agent-card.json"),
        Path("bundle/persona.md"),
        Path("bundle/knowledge/gotchas.md"),
    }


def test_compose_writes_exact_file_set_with_skills(tmp_path):
    dest = tmp_path / "out"
    skill = _make_skill(tmp_path)

    compose(_make_spec(skills=[skill]), dest)

    written = {p.relative_to(dest) for p in dest.rglob("*") if p.is_file()}
    assert written == {
        Path("Dockerfile"),
        Path("bundle/agent.yaml"),
        Path("bundle/agent-card.json"),
        Path("bundle/persona.md"),
        Path("bundle/knowledge/gotchas.md"),
        Path("bundle/skills/echo/SKILL.md"),
        Path("bundle/skills/echo/reference.py"),
    }


# --- agent.yaml shape ---------------------------------------------------------


def test_compose_agent_yaml_literal_placeholder_and_exact_text(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(), dest)

    text = (dest / "bundle" / "agent.yaml").read_text()
    assert text == (
        "name: sam\n"
        "card: agent-card.json\n"
        "persona: persona.md\n"
        "model:\n"
        "  id: ${AGENT_MODEL:-vertex_ai/claude-sonnet-4-5}\n"
        "  max_tokens: 1024\n"
        "  budget: {per_task_tokens: 50000}\n"
        "transports:\n"
        "  - {kind: a2a, host: 0.0.0.0, port: 8080}\n"
        "tools: {provider: none, allow: []}\n"
        "memory: {personal: techne, home: /techne}\n"
        "checkpoints: sqlite\n"
        "approvals:\n"
        "  ceiling: {write: false}\n"
        '  gates: [{match: "*", require: plan_approval}]\n'
        "  escalation: {route: cli}\n"
    )


def test_compose_agent_yaml_parses_with_expected_transport_and_gate(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(), dest)

    data = yaml.safe_load((dest / "bundle" / "agent.yaml").read_text())
    assert data["transports"] == [{"kind": "a2a", "host": "0.0.0.0", "port": 8080}]
    assert data["tools"] == {"provider": "none", "allow": []}
    assert data["approvals"]["gates"] == [{"match": "*", "require": "plan_approval"}]
    # the placeholder is a literal string pre-validation: not yet substituted
    assert data["model"]["id"] == "${AGENT_MODEL:-vertex_ai/claude-sonnet-4-5}"


# --- a2a auth block (colleagues switchboard arc, Task 4) ------------------------
#
# The composer's own gate is the presence of `cabal_url` alone (a `BundleSpec`
# field), never the fuller `cabal_enabled` truthiness check `kobo_api.clients`
# exposes to callers that also need the operator token — compose is about the
# bundle's shape, not whether the room happens to hold a token right now.


def test_compose_agent_yaml_byte_identical_when_cabal_url_none_both_ways(tmp_path):
    """Pin: whether `cabal_url` is left at its default or passed explicitly as
    `None`, composed output is byte-identical to today's (pre-Task-4) shape —
    the new field must never perturb the off path."""
    default_dest = tmp_path / "default"
    explicit_dest = tmp_path / "explicit"

    compose(_make_spec(), default_dest)
    compose(_make_spec(cabal_url=None), explicit_dest)

    default_text = (default_dest / "bundle" / "agent.yaml").read_text()
    explicit_text = (explicit_dest / "bundle" / "agent.yaml").read_text()

    assert default_text == explicit_text
    assert default_text == (
        "name: sam\n"
        "card: agent-card.json\n"
        "persona: persona.md\n"
        "model:\n"
        "  id: ${AGENT_MODEL:-vertex_ai/claude-sonnet-4-5}\n"
        "  max_tokens: 1024\n"
        "  budget: {per_task_tokens: 50000}\n"
        "transports:\n"
        "  - {kind: a2a, host: 0.0.0.0, port: 8080}\n"
        "tools: {provider: none, allow: []}\n"
        "memory: {personal: techne, home: /techne}\n"
        "checkpoints: sqlite\n"
        "approvals:\n"
        "  ceiling: {write: false}\n"
        '  gates: [{match: "*", require: plan_approval}]\n'
        "  escalation: {route: cli}\n"
    )


def test_compose_agent_yaml_carries_auth_block_when_cabal_url_configured(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(cabal_url="http://cabal.example.com"), dest)

    text = (dest / "bundle" / "agent.yaml").read_text()
    assert text == (
        "name: sam\n"
        "card: agent-card.json\n"
        "persona: persona.md\n"
        "model:\n"
        "  id: ${AGENT_MODEL:-vertex_ai/claude-sonnet-4-5}\n"
        "  max_tokens: 1024\n"
        "  budget: {per_task_tokens: 50000}\n"
        "transports:\n"
        '  - {kind: a2a, host: 0.0.0.0, port: 8080, auth: {jwks_url: '
        '"http://cabal.example.com/.well-known/jwks.json", issuer: "cabal-core", '
        'audience: "sam"}}\n'
        "tools: {provider: none, allow: []}\n"
        "memory: {personal: techne, home: /techne}\n"
        "checkpoints: sqlite\n"
        "approvals:\n"
        "  ceiling: {write: false}\n"
        '  gates: [{match: "*", require: plan_approval}]\n'
        "  escalation: {route: cli}\n"
    )


def test_compose_agent_yaml_auth_jwks_url_strips_single_trailing_slash(tmp_path):
    """A single `rstrip("/")` on `cabal_url` before appending the well-known
    path — a trailing-slash-or-not configured base URL must never yield a
    doubled (or missing) slash in the built jwks_url."""
    dest = tmp_path / "out"

    compose(_make_spec(cabal_url="http://cabal.example.com/"), dest)

    data = yaml.safe_load((dest / "bundle" / "agent.yaml").read_text())
    auth = data["transports"][0]["auth"]
    assert auth["jwks_url"] == "http://cabal.example.com/.well-known/jwks.json"


def test_compose_agent_yaml_auth_audience_is_the_agent_name(tmp_path):
    dest = tmp_path / "out"
    spec = BundleSpec(
        name="rae",
        persona_body="You are rae, a helpful assistant.\n",
        model_id="vertex_ai/claude-sonnet-4-5",
        skills=[],
        runtime_image=_RUNTIME_IMAGE,
        vertex_project="example-model-project",
        vertex_location="us-east5",
        cabal_url="http://cabal.example.com",
    )

    compose(spec, dest)

    data = yaml.safe_load((dest / "bundle" / "agent.yaml").read_text())
    auth = data["transports"][0]["auth"]
    assert auth["audience"] == "rae"
    assert auth["issuer"] == "cabal-core"


def test_compose_agent_yaml_auth_block_parses_with_exactly_three_keys(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(cabal_url="http://cabal.example.com"), dest)

    data = yaml.safe_load((dest / "bundle" / "agent.yaml").read_text())
    transport = data["transports"][0]
    assert transport["kind"] == "a2a"
    assert set(transport["auth"].keys()) == {"jwks_url", "issuer", "audience"}


def test_compose_agent_yaml_with_auth_block_still_validates(tmp_path):
    """The vendored schema's TransportSpec allows additional properties, so
    the new `auth` key doesn't trip schema validation."""
    dest = tmp_path / "out"

    compose(_make_spec(cabal_url="http://cabal.example.com"), dest)

    validate_agent_yaml(dest / "bundle")  # must not raise


# --- agent-card.json -----------------------------------------------------------


def test_compose_agent_card_lists_picked_skills(tmp_path):
    dest = tmp_path / "out"
    skill = _make_skill(tmp_path)

    compose(_make_spec(skills=[skill]), dest)

    card = json.loads((dest / "bundle" / "agent-card.json").read_text())
    assert card == {
        "name": "sam",
        "description": "Assembled by kobo.",
        "version": "0.1.0",
        "capabilities": [],
        "skills": [{"id": "echo"}],
        "metadata": {"team": "shared", "purpose_tags": [], "capability": ""},
    }


def test_compose_agent_card_empty_skills_list(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(), dest)

    card = json.loads((dest / "bundle" / "agent-card.json").read_text())
    assert card["skills"] == []


# --- agent-card.json metadata block (persona-metadata plan, Task 3) -------------


def test_compose_agent_card_metadata_block_present_capability_empty(tmp_path):
    """The card bake at compose time (Global Constraints: capability is
    earned later, never at compose) — the metadata block is always present,
    `capability` always starts `""`, `team`/`purpose_tags` come straight
    from the spec."""
    dest = tmp_path / "out"

    compose(_make_spec(team="acme-team", purpose_tags=["billing", "support"]), dest)

    card = json.loads((dest / "bundle" / "agent-card.json").read_text())
    assert card["metadata"] == {
        "team": "acme-team",
        "purpose_tags": ["billing", "support"],
        "capability": "",
    }


def test_compose_agent_card_metadata_defaults_to_shared_no_tags(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(), dest)

    card = json.loads((dest / "bundle" / "agent-card.json").read_text())
    assert card["metadata"] == {"team": "shared", "purpose_tags": [], "capability": ""}


# --- persona + knowledge --------------------------------------------------------


def test_compose_persona_written_verbatim(tmp_path):
    dest = tmp_path / "out"
    spec = _make_spec()

    compose(spec, dest)

    assert (dest / "bundle" / "persona.md").read_text() == spec.persona_body


def test_compose_knowledge_gotchas_content(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(), dest)

    assert (dest / "bundle" / "knowledge" / "gotchas.md").read_text() == (
        "## Deployment chain\n"
        "You were assembled in the kobo assembly room and deployed through the\n"
        "platform chain: repo, onboarding, build, deploy, first contact. If someone\n"
        "asks how you got here, tell them that.\n"
    )


# --- Dockerfile ------------------------------------------------------------------


def test_compose_dockerfile_matches_runtime_image(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(), dest)

    assert (dest / "Dockerfile").read_text() == (
        "# Agent image: the shared runtime plus this agent's bundle.\n"
        f"FROM {_RUNTIME_IMAGE}\n"
        "COPY bundle/ /bundle\n"
    )


# --- staged skill copy (Task 5: bucket catalogs replace git-archive pinning) ------


def test_compose_copies_whole_staged_skill_tree(tmp_path):
    dest = tmp_path / "out"
    skill = _make_skill(tmp_path)

    compose(_make_spec(skills=[skill]), dest)

    copied = dest / "bundle" / "skills" / "echo"
    assert copied.is_dir()
    assert (copied / "SKILL.md").read_text() == (skill.dir / "SKILL.md").read_text()
    assert (copied / "reference.py").read_text() == "def helper():\n    return 1\n"


def test_compose_copies_multiple_skills_by_id(tmp_path):
    dest = tmp_path / "out"
    echo = _make_skill(tmp_path, "echo")
    other = _make_skill(tmp_path, "other")

    compose(_make_spec(skills=[echo, other]), dest)

    assert (dest / "bundle" / "skills" / "echo" / "SKILL.md").is_file()
    assert (dest / "bundle" / "skills" / "other" / "SKILL.md").is_file()


def test_compose_copies_nested_staged_files(tmp_path):
    skill = _make_skill(tmp_path)
    (skill.dir / "scripts").mkdir()
    (skill.dir / "scripts" / "helper.sh").write_text("#!/bin/sh\necho hi\n")
    dest = tmp_path / "out"

    compose(_make_spec(skills=[skill]), dest)

    assert (dest / "bundle" / "skills" / "echo" / "scripts" / "helper.sh").read_text() == (
        "#!/bin/sh\necho hi\n"
    )


def test_compose_staged_skill_symlink_member_raises_compose_error(tmp_path):
    """Symlink members are rejected loudly rather than silently
    followed/copied — a downloaded bucket skill can never legitimately
    contain one (GCS objects are plain files), but the same defense the old
    git-archive extraction path enforced is kept here verbatim in spirit:
    author the skill without symlinks instead of kobo silently dereferencing
    one."""
    skill = _make_skill(tmp_path)
    (skill.dir / "sneaky-link").symlink_to(skill.dir / "SKILL.md")
    dest = tmp_path / "out"

    with pytest.raises(ComposeError) as exc_info:
        compose(_make_spec(skills=[skill]), dest)

    message = str(exc_info.value)
    assert "sneaky-link" in message
    assert not (dest / "bundle" / "skills" / "echo" / "sneaky-link").exists()


# --- validate_agent_yaml -----------------------------------------------------------


def test_validate_agent_yaml_passes_on_composed_bundle(tmp_path):
    dest = tmp_path / "out"
    compose(_make_spec(), dest)

    validate_agent_yaml(dest / "bundle")  # must not raise


def test_validate_agent_yaml_raises_compose_error_on_corrupted_transports(tmp_path):
    dest = tmp_path / "out"
    compose(_make_spec(), dest)

    agent_yaml_path = dest / "bundle" / "agent.yaml"
    data = yaml.safe_load(agent_yaml_path.read_text())
    data["transports"] = "not-a-list"  # schema requires an array
    agent_yaml_path.write_text(yaml.safe_dump(data, sort_keys=False))

    with pytest.raises(ComposeError):
        validate_agent_yaml(dest / "bundle")


def test_validate_agent_yaml_error_is_not_a_bare_parse_failure(tmp_path):
    """The corrupted-transports fixture must still be well-formed YAML/JSON —
    proving validate_agent_yaml fails on *schema* grounds, not a parse error."""
    dest = tmp_path / "out"
    compose(_make_spec(), dest)

    agent_yaml_path = dest / "bundle" / "agent.yaml"
    data = yaml.safe_load(agent_yaml_path.read_text())
    data["transports"] = "not-a-list"
    agent_yaml_path.write_text(yaml.safe_dump(data, sort_keys=False))

    # parsing (post AGENT_MODEL-substitution) succeeds fine on its own
    import re

    substituted = re.sub(r"\$\{AGENT_MODEL:-([^}]*)\}", r"\1", agent_yaml_path.read_text())
    reparsed = yaml.safe_load(substituted)
    assert reparsed["transports"] == "not-a-list"

    with pytest.raises(ComposeError):
        validate_agent_yaml(dest / "bundle")


# --- update_agent_card_metadata (persona-metadata plan, Task 3) -----------------


def test_update_agent_card_metadata_writes_all_three_fields(tmp_path):
    dest = tmp_path / "out"
    compose(_make_spec(), dest)

    update_agent_card_metadata(
        dest / "bundle",
        team="acme-team",
        purpose_tags=["billing", "support"],
        capability="Helps customers resolve billing issues quickly.",
    )

    card = json.loads((dest / "bundle" / "agent-card.json").read_text())
    assert card["metadata"] == {
        "team": "acme-team",
        "purpose_tags": ["billing", "support"],
        "capability": "Helps customers resolve billing issues quickly.",
    }


def test_update_agent_card_metadata_preserves_every_other_top_level_field(tmp_path):
    """The load-bearing invariant: every field OTHER than `metadata` survives
    byte-faithfully, including a field this module has never heard of."""
    dest = tmp_path / "out"
    compose(_make_spec(), dest)
    card_path = dest / "bundle" / "agent-card.json"
    original = json.loads(card_path.read_text())
    original["a_field_kobo_does_not_know_about"] = {"nested": ["stuff", 1, True]}
    card_path.write_text(json.dumps(original, indent=2) + "\n")

    update_agent_card_metadata(
        dest / "bundle", team="acme-team", purpose_tags=["x"], capability="Does a thing."
    )

    card = json.loads(card_path.read_text())
    assert card["name"] == original["name"]
    assert card["description"] == original["description"]
    assert card["version"] == original["version"]
    assert card["capabilities"] == original["capabilities"]
    assert card["skills"] == original["skills"]
    assert card["a_field_kobo_does_not_know_about"] == {"nested": ["stuff", 1, True]}
    assert card["metadata"] == {
        "team": "acme-team",
        "purpose_tags": ["x"],
        "capability": "Does a thing.",
    }


def test_update_agent_card_metadata_preserves_unknown_metadata_subfield(tmp_path):
    """An unknown key already inside `metadata` itself (a hand-edited or
    future-versioned card) survives too — only the three known keys are ever
    replaced."""
    dest = tmp_path / "out"
    compose(_make_spec(), dest)
    card_path = dest / "bundle" / "agent-card.json"
    card = json.loads(card_path.read_text())
    card["metadata"]["future_field"] = "kept"
    card_path.write_text(json.dumps(card, indent=2) + "\n")

    update_agent_card_metadata(
        dest / "bundle", team="acme-team", purpose_tags=[], capability="Does a thing."
    )

    updated = json.loads(card_path.read_text())
    assert updated["metadata"]["future_field"] == "kept"
    assert updated["metadata"]["team"] == "acme-team"


def test_update_agent_card_metadata_uses_compose_style_json_dump(tmp_path):
    """Round-trip formatting must match compose()'s own `json.dumps(...,
    indent=2) + "\\n"` style exactly, so a compose-then-update cycle never
    drifts the file's own formatting."""
    dest = tmp_path / "out"
    compose(_make_spec(), dest)
    card_path = dest / "bundle" / "agent-card.json"

    update_agent_card_metadata(
        dest / "bundle", team="acme-team", purpose_tags=[], capability="Does a thing."
    )

    text = card_path.read_text()
    reserialized = json.dumps(json.loads(text), indent=2) + "\n"
    assert text == reserialized


def test_update_agent_card_metadata_overwrites_prior_metadata_values(tmp_path):
    """A second call fully replaces the three known keys' values (not a
    merge/append of purpose_tags, say) — the caller always passes the
    complete current set."""
    dest = tmp_path / "out"
    compose(_make_spec(team="old-team", purpose_tags=["old-tag"]), dest)

    update_agent_card_metadata(
        dest / "bundle",
        team="new-team",
        purpose_tags=["new-tag"],
        capability="A fresh sentence.",
    )

    card = json.loads((dest / "bundle" / "agent-card.json").read_text())
    assert card["metadata"] == {
        "team": "new-team",
        "purpose_tags": ["new-tag"],
        "capability": "A fresh sentence.",
    }


# --- update_agent_yaml_auth (colleagues switchboard arc, Task 4) ----------------
#
# The in-place backfill helper for the two live agents (and any future
# already-composed bundle) whose `agent.yaml` predates the switchboard: adds
# or replaces the `auth` block on the existing a2a transport entry, touching
# nothing else in the file — a surgical text edit (matching
# `update_dockerfile_runtime_image`'s own idiom), never a full YAML
# round-trip, since a generic `yaml.safe_load`/`yaml.safe_dump` cycle would
# reorder/reformat every other line this module's custom flow-style dumper
# deliberately controls.

_FIXTURE_AGENT_YAML_NO_AUTH = (
    "# agent.yaml -- generated by kobo, do not hand-edit lightly\n"
    "name: sam\n"
    "card: agent-card.json\n"
    "persona: persona.md\n"
    "model:\n"
    "  id: ${AGENT_MODEL:-vertex_ai/claude-sonnet-4-5}\n"
    "  max_tokens: 1024\n"
    "  budget: {per_task_tokens: 50000}\n"
    "transports:\n"
    "  # the room's own a2a endpoint\n"
    "  - {kind: a2a, host: 0.0.0.0, port: 8080}\n"
    "tools: {provider: none, allow: []}\n"
    "memory: {personal: techne, home: /techne}\n"
    "checkpoints: sqlite\n"
    "approvals:\n"
    "  ceiling: {write: false}\n"
    '  gates: [{match: "*", require: plan_approval}]\n'
    "  escalation: {route: cli}\n"
)

_JWKS_URL = "http://cabal.example.com/.well-known/jwks.json"
_ISSUER = "cabal-core"
_AUDIENCE = "sam"


def _write_fixture(bundle_dir: Path, text: str = _FIXTURE_AGENT_YAML_NO_AUTH) -> Path:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    agent_yaml_path = bundle_dir / "agent.yaml"
    agent_yaml_path.write_text(text)
    return agent_yaml_path


def test_update_agent_yaml_auth_adds_block_and_preserves_every_other_line(tmp_path):
    bundle_dir = tmp_path / "bundle"
    agent_yaml_path = _write_fixture(bundle_dir)

    update_agent_yaml_auth(
        bundle_dir, jwks_url=_JWKS_URL, issuer=_ISSUER, audience=_AUDIENCE
    )

    updated_lines = agent_yaml_path.read_text().splitlines(keepends=True)
    original_lines = _FIXTURE_AGENT_YAML_NO_AUTH.splitlines(keepends=True)
    assert len(updated_lines) == len(original_lines)
    for i, (original, updated) in enumerate(zip(original_lines, updated_lines)):
        if i == original_lines.index("  - {kind: a2a, host: 0.0.0.0, port: 8080}\n"):
            continue
        assert updated == original, f"line {i} changed unexpectedly"

    assert updated_lines[original_lines.index("  - {kind: a2a, host: 0.0.0.0, port: 8080}\n")] == (
        '  - {kind: a2a, host: 0.0.0.0, port: 8080, auth: {jwks_url: '
        '"http://cabal.example.com/.well-known/jwks.json", issuer: "cabal-core", '
        'audience: "sam"}}\n'
    )


def test_update_agent_yaml_auth_replaces_existing_block(tmp_path):
    fixture_with_old_auth = _FIXTURE_AGENT_YAML_NO_AUTH.replace(
        "  - {kind: a2a, host: 0.0.0.0, port: 8080}\n",
        '  - {kind: a2a, host: 0.0.0.0, port: 8080, auth: {jwks_url: '
        '"http://old-cabal.example.com/.well-known/jwks.json", issuer: '
        '"old-issuer", audience: "old-audience"}}\n',
    )
    bundle_dir = tmp_path / "bundle"
    _write_fixture(bundle_dir, fixture_with_old_auth)

    update_agent_yaml_auth(
        bundle_dir, jwks_url=_JWKS_URL, issuer=_ISSUER, audience=_AUDIENCE
    )

    text = (bundle_dir / "agent.yaml").read_text()
    assert "old-cabal.example.com" not in text
    assert "old-issuer" not in text
    assert "old-audience" not in text
    data = yaml.safe_load(text)
    auth = data["transports"][0]["auth"]
    assert auth == {"jwks_url": _JWKS_URL, "issuer": _ISSUER, "audience": _AUDIENCE}
    # every other line is untouched
    other_lines = [
        line
        for line in fixture_with_old_auth.splitlines(keepends=True)
        if "kind: a2a" not in line
    ]
    for line in other_lines:
        assert line in text


def test_update_agent_yaml_auth_is_idempotent(tmp_path):
    bundle_dir = tmp_path / "bundle"
    _write_fixture(bundle_dir)

    update_agent_yaml_auth(
        bundle_dir, jwks_url=_JWKS_URL, issuer=_ISSUER, audience=_AUDIENCE
    )
    once = (bundle_dir / "agent.yaml").read_text()

    update_agent_yaml_auth(
        bundle_dir, jwks_url=_JWKS_URL, issuer=_ISSUER, audience=_AUDIENCE
    )
    twice = (bundle_dir / "agent.yaml").read_text()

    assert once == twice


def test_update_agent_yaml_auth_raises_when_no_a2a_transport(tmp_path):
    fixture_no_a2a = _FIXTURE_AGENT_YAML_NO_AUTH.replace(
        "  # the room's own a2a endpoint\n  - {kind: a2a, host: 0.0.0.0, port: 8080}\n",
        "  - {kind: slack, channel: general}\n",
    )
    bundle_dir = tmp_path / "bundle"
    _write_fixture(bundle_dir, fixture_no_a2a)

    with pytest.raises(ClientError) as exc_info:
        update_agent_yaml_auth(
            bundle_dir, jwks_url=_JWKS_URL, issuer=_ISSUER, audience=_AUDIENCE
        )

    assert "a2a" in str(exc_info.value)
    # unmodified on failure
    assert (bundle_dir / "agent.yaml").read_text() == fixture_no_a2a


def test_update_agent_yaml_auth_leaves_other_transport_kinds_untouched(tmp_path):
    fixture_with_slack = _FIXTURE_AGENT_YAML_NO_AUTH.replace(
        "transports:\n  # the room's own a2a endpoint\n  - {kind: a2a, host: 0.0.0.0, port: 8080}\n",
        "transports:\n"
        "  # the room's own a2a endpoint\n"
        "  - {kind: a2a, host: 0.0.0.0, port: 8080}\n"
        "  - {kind: slack, channel: general}\n",
    )
    bundle_dir = tmp_path / "bundle"
    _write_fixture(bundle_dir, fixture_with_slack)

    update_agent_yaml_auth(
        bundle_dir, jwks_url=_JWKS_URL, issuer=_ISSUER, audience=_AUDIENCE
    )

    text = (bundle_dir / "agent.yaml").read_text()
    assert "  - {kind: slack, channel: general}\n" in text


def test_update_agent_yaml_auth_missing_file_raises_client_error(tmp_path):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir(parents=True)

    with pytest.raises(ClientError):
        update_agent_yaml_auth(
            bundle_dir, jwks_url=_JWKS_URL, issuer=_ISSUER, audience=_AUDIENCE
        )


def test_update_agent_yaml_auth_on_freshly_composed_bundle_round_trips(tmp_path):
    """Integration: compose() without cabal_url, then backfill via the update
    helper (the two live agents' real path) — the result still validates."""
    dest = tmp_path / "out"
    compose(_make_spec(), dest)

    update_agent_yaml_auth(
        dest / "bundle", jwks_url=_JWKS_URL, issuer=_ISSUER, audience=_AUDIENCE
    )

    validate_agent_yaml(dest / "bundle")  # must not raise
    data = yaml.safe_load((dest / "bundle" / "agent.yaml").read_text())
    assert data["transports"][0]["auth"] == {
        "jwks_url": _JWKS_URL,
        "issuer": _ISSUER,
        "audience": _AUDIENCE,
    }


# --- optional: validate against the real daedalus runtime, if available -----------


def test_bundle_loads_with_real_daedalus_if_available(tmp_path):
    daedalus = pytest.importorskip("daedalus")
    from daedalus.bundle import Bundle

    dest = tmp_path / "out"
    compose(_make_spec(), dest)

    bundle = Bundle.load(dest / "bundle", env={})

    assert bundle.spec.name == "sam"
    assert bundle.spec.model.id == "vertex_ai/claude-sonnet-4-5"
    assert bundle.spec.transports[0].kind == "a2a"
    assert bundle.spec.transports[0].options() == {"host": "0.0.0.0", "port": 8080}
    del daedalus  # only imported to prove importorskip's module handle works

import json
from pathlib import Path

import pytest
import yaml

from kobo_api.composer import BundleSpec, ComposeError, compose, validate_agent_yaml
from kobo_api.steps import StagedSkill

_RUNTIME_IMAGE = "registry.example/agents/daedalus-runtime:latest"


def _make_spec(skills: list[StagedSkill] | None = None) -> BundleSpec:
    return BundleSpec(
        name="sam",
        persona_body="You are sam, a helpful assistant.\n",
        model_id="vertex_ai/claude-sonnet-4-5",
        skills=skills or [],
        runtime_image=_RUNTIME_IMAGE,
        vertex_project="example-model-project",
        vertex_location="us-east5",
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
    }


def test_compose_agent_card_empty_skills_list(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(), dest)

    card = json.loads((dest / "bundle" / "agent-card.json").read_text())
    assert card["skills"] == []


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

import json
import subprocess
import tarfile
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest
import yaml

from pnyx_api.catalog import SkillInfo
from pnyx_api.composer import BundleSpec, ComposeError, compose, validate_agent_yaml

_RUNTIME_IMAGE = "registry.example/agents/daedalus-runtime:latest"


def _make_spec(skills: list[SkillInfo] | None = None) -> BundleSpec:
    return BundleSpec(
        name="sam",
        persona_body="You are sam, a helpful assistant.\n",
        model_id="vertex_ai/claude-sonnet-4-5",
        skills=skills or [],
        runtime_image=_RUNTIME_IMAGE,
        vertex_project="example-model-project",
        vertex_location="us-east5",
    )


def _make_skill(tmp_path: Path, skill_id: str = "echo") -> SkillInfo:
    skill_dir = tmp_path / "skills-src" / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: Echoes things.\n---\nEcho skill body.\n"
    )
    (skill_dir / "reference.py").write_text("def helper():\n    return 1\n")
    return SkillInfo(
        id=skill_id,
        description="Echoes things.",
        path=skill_dir / "SKILL.md",
        sha="deadbeef",
    )


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
        "description": "Assembled by pnyx.",
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
        "You were assembled in the pnyx assembly room and deployed through the\n"
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


# --- skill tree copy ---------------------------------------------------------------


def test_compose_copies_whole_skill_tree(tmp_path):
    dest = tmp_path / "out"
    skill = _make_skill(tmp_path)

    compose(_make_spec(skills=[skill]), dest)

    copied = dest / "bundle" / "skills" / "echo"
    assert copied.is_dir()
    assert (copied / "SKILL.md").read_text() == skill.path.read_text()
    assert (copied / "reference.py").read_text() == "def helper():\n    return 1\n"


def test_compose_copies_multiple_skills_by_id(tmp_path):
    dest = tmp_path / "out"
    echo = _make_skill(tmp_path, "echo")
    other = _make_skill(tmp_path, "other")

    compose(_make_spec(skills=[echo, other]), dest)

    assert (dest / "bundle" / "skills" / "echo" / "SKILL.md").is_file()
    assert (dest / "bundle" / "skills" / "other" / "SKILL.md").is_file()


# --- sha-pinned skill materialization (Task 6) --------------------------------
#
# `_make_skill` above (plain directory, no `.git`) exercises the fallback path
# and is left untouched by all of this — the tests below build a *real*
# git-backed skills repo so the sha-aware `git archive` path in
# `_materialize_skill` (pnyx_api.composer) actually activates.


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _head_sha(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_git_skill(tmp_path: Path, skill_id: str = "echo", body: str = "Echo skill body.\n"):
    """Build a real git-backed skills repo with one skill committed; return
    (skills_repo_path, SkillInfo pointing at HEAD's sha)."""
    skills_repo = tmp_path / "skills-repo"
    skill_dir = skills_repo / "skills" / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: Echoes things.\n---\n{body}"
    )
    (skill_dir / "reference.py").write_text("def helper():\n    return 1\n")
    _git(skills_repo, "init", "-q")
    _git(skills_repo, "add", "-A")
    _git(skills_repo, "commit", "-q", "-m", "init")
    skill = SkillInfo(
        id=skill_id,
        description="Echoes things.",
        path=skill_dir / "SKILL.md",
        sha=_head_sha(skills_repo),
    )
    return skills_repo, skill


def _build_tar_bytes(entries: dict[str, bytes]) -> bytes:
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, BytesIO(content))
    return buf.getvalue()


class _RecordingBytesRunner:
    """CommandRunner test double: records exact argv for run_bytes() calls
    and hands back a canned in-memory tar rather than shelling out for
    real — isolates the argv-shape assertion below from actual git archive
    behavior (which the golden-content test further down covers for real)."""

    def __init__(self, tar_bytes: bytes):
        self._tar_bytes = tar_bytes
        self.calls: list[list[str]] = []

    def run_bytes(self, args: list[str], stdin: bytes | None = None) -> bytes:
        self.calls.append(list(args))
        return self._tar_bytes

    def run(self, args, stdin=None):  # pragma: no cover - not expected to be hit
        raise AssertionError("run() should not be called for skill materialization")


def test_compose_pinned_skill_archive_argv_exact(tmp_path):
    skills_repo, skill = _make_git_skill(tmp_path)
    tar_bytes = _build_tar_bytes({f"skills/{skill.id}/SKILL.md": b"pinned content\n"})
    runner = _RecordingBytesRunner(tar_bytes)
    dest = tmp_path / "out"

    compose(_make_spec(skills=[skill]), dest, runner=runner)

    assert runner.calls == [
        [
            "git", "-C", str(skills_repo), "archive", "--format=tar",
            skill.sha, "--", f"skills/{skill.id}",
        ]
    ]
    assert (dest / "bundle" / "skills" / "echo" / "SKILL.md").read_text() == "pinned content\n"


def test_compose_pinned_skill_strips_prefix_for_nested_files(tmp_path):
    _skills_repo, skill = _make_git_skill(tmp_path)
    tar_bytes = _build_tar_bytes(
        {
            f"skills/{skill.id}/SKILL.md": b"pinned SKILL.md\n",
            f"skills/{skill.id}/scripts/helper.py": b"def helper():\n    return 1\n",
        }
    )
    runner = _RecordingBytesRunner(tar_bytes)
    dest = tmp_path / "out"

    compose(_make_spec(skills=[skill]), dest, runner=runner)

    skill_dir = dest / "bundle" / "skills" / "echo"
    assert (skill_dir / "SKILL.md").read_text() == "pinned SKILL.md\n"
    assert (skill_dir / "scripts" / "helper.py").read_text() == "def helper():\n    return 1\n"


def test_compose_pinned_skill_ships_old_content_after_head_advances(tmp_path):
    """Golden-content test, no mocking: real git repo, real CommandRunner.
    Advance the skills repo's HEAD with changed content after recording a
    sha, then compose with that OLD sha — the bundle must carry the OLD
    bytes, proving materialization reads the pinned commit, not the working
    tree gojira/pnyx-api would otherwise see."""
    skills_repo, old_skill = _make_git_skill(tmp_path, body="OLD echo skill body.\n")

    (skills_repo / "skills" / "echo" / "SKILL.md").write_text(
        "---\nname: echo\ndescription: Echoes things.\n---\nNEW echo skill body.\n"
    )
    _git(skills_repo, "add", "-A")
    _git(skills_repo, "commit", "-q", "-m", "advance head")
    assert _head_sha(skills_repo) != old_skill.sha

    dest = tmp_path / "out"
    compose(_make_spec(skills=[old_skill]), dest)  # real CommandRunner, no mocking

    shipped = (dest / "bundle" / "skills" / "echo" / "SKILL.md").read_text()
    assert "OLD echo skill body" in shipped
    assert "NEW echo skill body" not in shipped
    # the reference.py alongside SKILL.md at that same old commit also landed
    assert (dest / "bundle" / "skills" / "echo" / "reference.py").is_file()


def test_compose_unreachable_sha_raises_compose_error_naming_sha_and_id(tmp_path):
    _skills_repo, skill = _make_git_skill(tmp_path)
    bogus_sha = "0" * 40
    bogus = replace(skill, sha=bogus_sha)
    dest = tmp_path / "out"

    with pytest.raises(ComposeError) as exc_info:
        compose(_make_spec(skills=[bogus]), dest)

    message = str(exc_info.value)
    assert bogus_sha in message
    assert skill.id in message


def test_compose_pinned_skill_tar_slip_member_raises_compose_error(tmp_path):
    """A crafted archive member whose name string-prefixes as
    `skills/<id>/` but path-escapes via `..` must not be trusted just
    because `member.name.startswith(prefix)` passes — containment is
    enforced by resolving the actual extraction target against `dest`, not
    by a naive string check. Git itself refuses to commit `..` tree entries,
    but `_materialize_skill` must not rely on that external invariant since
    the tar is only ever handed to it as bytes, not re-validated as
    git-produced."""
    _skills_repo, skill = _make_git_skill(tmp_path)
    # _materialize_skill's own `dest` is `<compose dest>/bundle/skills/<id>` —
    # four levels below tmp_path (out/bundle/skills/<id>) — so four `..`
    # segments walk the crafted member back up to tmp_path itself, well
    # outside the skill's sandboxed destination.
    evil_name = f"skills/{skill.id}/../../../../escape.txt"
    tar_bytes = _build_tar_bytes({evil_name: b"pwned\n"})
    runner = _RecordingBytesRunner(tar_bytes)
    dest = tmp_path / "out"

    with pytest.raises(ComposeError) as exc_info:
        compose(_make_spec(skills=[skill]), dest, runner=runner)

    message = str(exc_info.value)
    assert evil_name in message
    # the escape must never have landed anywhere outside the skill's dest.
    assert not (tmp_path / "escape.txt").exists()
    assert not (dest / "escape.txt").exists()
    assert not (dest / "bundle" / "escape.txt").exists()


def test_compose_pinned_skill_symlink_member_raises_compose_error(tmp_path):
    """Symlink members (and any other non-regular, non-directory member) are
    rejected loudly rather than silently dropped — silent dropping would
    diverge from the unpinned copytree fallback, which dereferences and
    copies symlinks. Full symlink support is deliberately not implemented;
    the failure message names the offending member so the skill can be
    authored without symlinks instead."""
    _skills_repo, skill = _make_git_skill(tmp_path)
    member_name = f"skills/{skill.id}/sneaky-link"
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=member_name)
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    runner = _RecordingBytesRunner(buf.getvalue())
    dest = tmp_path / "out"

    with pytest.raises(ComposeError) as exc_info:
        compose(_make_spec(skills=[skill]), dest, runner=runner)

    message = str(exc_info.value)
    assert member_name in message
    assert not (dest / "bundle" / "skills" / skill.id / "sneaky-link").exists()


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

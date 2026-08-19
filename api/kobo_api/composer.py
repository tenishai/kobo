"""Bundle composer: writes a daedalus agent bundle tree to disk and validates
`agent.yaml` against a vendored copy of daedalus's `docs/bundle.schema.json`.

The schema lives at `kobo_api/vendor/bundle.schema.json`, copied verbatim
from the daedalus repo — never hand-edited.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import jsonschema
import yaml

from kobo_api.clients import ClientError

# Type-only: `kobo_api.steps` imports `BundleSpec`/`compose`/`validate_agent_
# yaml` from this module, so a real (runtime) import the other way round
# would be circular. `from __future__ import annotations` (below) already
# makes every annotation in this file a lazy string, so `StagedSkill` is only
# ever needed by type checkers, never at import time — `_materialize_skill`
# only ever accesses `.id`/`.dir` on it, duck-typed, no isinstance check.
if TYPE_CHECKING:
    from kobo_api.steps import StagedSkill

_VENDOR_SCHEMA_PATH = Path(__file__).parent / "vendor" / "bundle.schema.json"

# Pre-validation substitution for the literal `${AGENT_MODEL:-<default>}` placeholder
# written into agent.yaml's `model.id` — collapses it to its default before jsonschema
# sees it (jsonschema has no notion of shell-style parameter expansion).
_AGENT_MODEL_DEFAULT_RE = re.compile(r"\$\{AGENT_MODEL:-([^}]*)\}")

_GOTCHAS_MD = (
    "## Deployment chain\n"
    "You were assembled in the kobo assembly room and deployed through the\n"
    "platform chain: repo, onboarding, build, deploy, first contact. If someone\n"
    "asks how you got here, tell them that.\n"
)


class ComposeError(Exception):
    """Raised when a composed bundle's agent.yaml fails schema validation."""


@dataclass
class BundleSpec:
    name: str
    persona_body: str  # already {name}-substituted by caller
    model_id: str
    skills: list[StagedSkill]  # may be empty; already staged on local disk
    runtime_image: str
    vertex_project: str
    vertex_location: str
    # Persona metadata (persona-metadata plan, Task 3): the agent's own
    # creating team and purpose tags (already recorded on the Agent row by
    # Task 2), threaded through so the baked agent-card.json's `metadata`
    # block (see `_agent_card` below) carries them from the moment a bundle
    # is first composed — never something bolted on after the fact.
    # Defaulted (rather than required) so every pre-existing `BundleSpec(...)`
    # construction across the test suite that predates this pair keeps
    # working unchanged.
    team: str = "shared"
    purpose_tags: list[str] = field(default_factory=list)


# --- agent.yaml rendering ----------------------------------------------------
#
# The brief's exact target mixes flow-style (`{a: b}`, `[{a: b}]`) and
# block-style YAML collections in a way plain `yaml.safe_dump(sort_keys=False)`
# can't produce on its own (its flow/block heuristic is keyed off "does this
# collection contain only scalars", which picks block style for `tools`
# because of its empty `allow: []` list, and picks block style for
# `approvals.gates` because its item is a mapping — neither matches the
# brief). These marker types force specific nodes to a specific style while
# leaving every other node to PyYAML's usual heuristic.


class _FlowList(list):
    """Rendered as a YAML flow-style sequence: `[a, b]`."""


class _FlowDict(dict):
    """Rendered as a YAML flow-style mapping: `{a: b}`."""


class _DoubleQuoted(str):
    """Rendered as a double-quoted YAML scalar: `"a"`."""


class _AgentYamlDumper(yaml.SafeDumper):
    """SafeDumper that indents block sequences under their parent key.

    PyYAML's default emits block sequences "indentless" (`key:\\n- item`);
    the brief's `transports:` is indented (`transports:\\n  - {...}`).
    """

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def _represent_flow_list(dumper: yaml.SafeDumper, data: _FlowList):
    return dumper.represent_sequence("tag:yaml.org,2002:seq", list(data), flow_style=True)


def _represent_flow_dict(dumper: yaml.SafeDumper, data: _FlowDict):
    return dumper.represent_mapping("tag:yaml.org,2002:map", dict(data), flow_style=True)


def _represent_double_quoted(dumper: yaml.SafeDumper, data: _DoubleQuoted):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style='"')


_AgentYamlDumper.add_representer(_FlowList, _represent_flow_list)
_AgentYamlDumper.add_representer(_FlowDict, _represent_flow_dict)
_AgentYamlDumper.add_representer(_DoubleQuoted, _represent_double_quoted)


def _agent_yaml_data(spec: BundleSpec) -> dict:
    return {
        "name": spec.name,
        "card": "agent-card.json",
        "persona": "persona.md",
        "model": {
            "id": f"${{AGENT_MODEL:-{spec.model_id}}}",
            "max_tokens": 1024,
            "budget": {"per_task_tokens": 50000},
        },
        "transports": [
            {"kind": "a2a", "host": "0.0.0.0", "port": 8080},
        ],
        "tools": _FlowDict(provider="none", allow=[]),
        "memory": {"personal": "techne", "home": "/techne"},
        "checkpoints": "sqlite",
        "approvals": {
            "ceiling": {"write": False},
            "gates": _FlowList([{"match": _DoubleQuoted("*"), "require": "plan_approval"}]),
            "escalation": {"route": "cli"},
        },
    }


def _render_agent_yaml(spec: BundleSpec) -> str:
    return yaml.dump(
        _agent_yaml_data(spec),
        Dumper=_AgentYamlDumper,
        sort_keys=False,
        default_flow_style=None,
    )


def _agent_card(spec: BundleSpec) -> dict:
    return {
        "name": spec.name,
        "description": "Assembled by kobo.",
        "version": "0.1.0",
        "capabilities": [],
        "skills": [{"id": skill.id} for skill in spec.skills],
        # Persona metadata (Task 3): baked in at compose time so every agent
        # card carries this block from birth — `capability` starts empty
        # (never earned yet; HelloStep's tail is what first fills it in),
        # `team`/`purpose_tags` come straight from the spec (Task 2's own
        # values, threaded here by whoever builds the spec —
        # `kobo_api.steps.RepoStep`).
        "metadata": {
            "team": spec.team,
            "purpose_tags": list(spec.purpose_tags),
            "capability": "",
        },
    }


def _dockerfile(spec: BundleSpec) -> str:
    return (
        "# Agent image: the shared runtime plus this agent's bundle.\n"
        f"FROM {spec.runtime_image}\n"
        "COPY bundle/ /bundle\n"
    )


# Matches a Dockerfile's `FROM <image>` line (this module's own template,
# `_dockerfile` above, writes exactly one) anywhere in the file, not just its
# first line — a leading comment line (as that template has) is not itself a
# FROM line. Captures the `FROM` keyword plus the whitespace that follows it,
# so the replacement can preserve that prefix byte-for-byte and only swap the
# image-ref token itself.
_DOCKERFILE_FROM_RE = re.compile(r"^(FROM[ \t]+)\S+", re.MULTILINE)


def update_dockerfile_runtime_image(dockerfile_path: Path, runtime_image: str) -> None:
    """Rewrite an already-cloned repo's `Dockerfile` so its `FROM` line's
    image ref is exactly `runtime_image`, leaving every other line (and any
    trailing content on the FROM line itself) byte-identical.

    Shared by every kobo write path that touches an existing agent repo's
    Dockerfile after compose time — originally `kobo_api.slack_setup.
    wire_slack` (the runtime-bump incident fix: this module bakes the
    runtime image into a repo's Dockerfile once, at compose time, via
    `_dockerfile` above; a later repo edit that never re-pins it can leave a
    stale base in place — see `wire_slack`'s docstring for the crashloop
    that fix closed), hoisted here so `kobo_api.intake.upload_intake_file`
    can reuse the exact same edit for the same reason: an intake upload's
    own delivery must carry an image whose runtime seeder actually runs an
    intake pass, or kobo's ancestor-derived "ingested" status can report a
    file ingested when the pod that booted never ran a seeder capable of
    ingesting it at all.

    Idempotent by construction: when the FROM line already names
    `runtime_image`, the substitution is a no-op and the rewritten text is
    byte-identical to what was already on disk — so this edit alone never
    makes a caller's `commit_push` see a diff on an already-current repo.

    Uses a replacement function (not a plain substitution string) so that
    `runtime_image` is inserted literally even if it happens to contain
    characters (like a backslash) that `re.sub`'s string-replacement syntax
    would otherwise treat specially.

    Raises `ClientError` (never silently no-ops) in the two ways this can
    fail to actually re-pin anything — final-review Finding 2:

    - The Dockerfile is missing entirely: `Path.read_text()` raises
      `OSError`, caught and re-raised as `ClientError` naming only the path,
      same idiom as `kobo_api.clients._read_slack_tokens`.
    - The Dockerfile has no line matching `_DOCKERFILE_FROM_RE` (some
      unrecognized/hand-edited Dockerfile shape): `re.sub` on zero matches
      is itself a silent no-op — it happily writes the file back unchanged
      and returns, giving no signal that the re-pin never happened. That
      used to mean the stale-base crashloop this function exists to fix
      could recur with nothing in the way of a symptom. `re.subn` (which
      reports the match count) lets this catch that here and raise instead,
      so the failure surfaces immediately as the caller's own 502, not as a
      crashloop discovered later with no trail back to its cause.
    """
    try:
        text = dockerfile_path.read_text()
    except OSError as exc:
        raise ClientError(f"Dockerfile not found: {dockerfile_path}") from exc

    updated, count = _DOCKERFILE_FROM_RE.subn(
        lambda match: match.group(1) + runtime_image, text, count=1
    )
    if count == 0:
        raise ClientError(
            f"Dockerfile has no recognizable FROM line to re-pin: {dockerfile_path}"
        )
    dockerfile_path.write_text(updated)


def update_agent_card_metadata(
    bundle_dir: Path, *, team: str | None, purpose_tags: list[str], capability: str
) -> None:
    """In-place edit of `bundle_dir/agent-card.json`'s `metadata` block
    (persona-metadata plan, Task 3): the card delivery lifecycle is bake at
    compose time (`_agent_card` above, `capability` starting `""`), then
    update after every earn/PATCH/Regenerate — this function is that update.

    Reads the whole card as a dict, replaces exactly `metadata.team`/
    `metadata.purpose_tags`/`metadata.capability`, and re-dumps with the
    exact same `json.dumps(..., indent=2) + "\\n"` style `compose()` itself
    writes the file with — every OTHER top-level field (`name`,
    `description`, `version`, `capabilities`, `skills`, or any field a future
    caller adds that this module doesn't know about yet) survives
    byte-faithfully, and any OTHER key already inside `metadata` itself
    (there are none today, but a hand-edited or future-versioned card might
    carry one) survives too — only the three known keys are ever replaced.
    """
    card_path = bundle_dir / "agent-card.json"
    card = json.loads(card_path.read_text())
    metadata = dict(card.get("metadata") or {})
    metadata["team"] = team
    metadata["purpose_tags"] = list(purpose_tags)
    metadata["capability"] = capability
    card["metadata"] = metadata
    card_path.write_text(json.dumps(card, indent=2) + "\n")


def _materialize_skill(skill: StagedSkill, dest: Path) -> None:
    """Copy one already-staged skill directory into the not-yet-existing
    `dest`.

    Bucket catalogs (Task 5) replace the old sha-pinned `git archive`
    materialization entirely: `kobo_api.steps._stage_skills` has already
    downloaded the skill's current bucket content to local disk (`skill.dir`)
    by the time this runs, comparing/repairing the sha pin there — this
    function's only remaining job is copying those already-resolved bytes
    into the bundle tree.

    The same containment/symlink-rejection discipline the old tar-extraction
    path enforced is kept here verbatim in spirit, just walking `skill.dir`'s
    real filesystem entries instead of tar members: (1) containment — every
    entry's target path is verified to resolve inside `dest` before it's
    written; and (2) no symlink (or other non-regular, non-directory) entry
    is ever silently followed/copied — such an entry raises ComposeError
    naming it, rather than being dropped quietly. Directory entries are
    skipped explicitly; they're created via `parents=True` when a file
    beneath them is written.
    """
    dest.mkdir(parents=True)
    resolved_dest = dest.resolve()
    for entry in sorted(skill.dir.rglob("*")):
        relative = entry.relative_to(skill.dir)
        target = dest / relative
        if not target.resolve().is_relative_to(resolved_dest):
            raise ComposeError(
                f"skill {skill.id!r} staged member {relative.as_posix()!r} would "
                f"extract outside of {dest} (path escape); refusing to write it"
            )
        if entry.is_symlink():
            raise ComposeError(
                f"skill {skill.id!r} staged member {relative.as_posix()!r} is a "
                "symlink (symlinks are not supported — author the skill without "
                "symlinks)"
            )
        if entry.is_dir():
            continue
        if not entry.is_file():
            raise ComposeError(
                f"skill {skill.id!r} staged member {relative.as_posix()!r} is not "
                "a regular file or directory"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(entry.read_bytes())


def compose(spec: BundleSpec, dest: Path) -> None:
    """Write a daedalus agent bundle tree to `dest`.

    Produces:
        dest/Dockerfile
        dest/bundle/agent.yaml
        dest/bundle/agent-card.json
        dest/bundle/persona.md
        dest/bundle/knowledge/gotchas.md
        dest/bundle/skills/<id>/**   (one materialized tree per spec.skills
                                      entry — see `_materialize_skill`)
    """
    bundle_dir = dest / "bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    (bundle_dir / "agent.yaml").write_text(_render_agent_yaml(spec))
    (bundle_dir / "agent-card.json").write_text(json.dumps(_agent_card(spec), indent=2) + "\n")
    (bundle_dir / "persona.md").write_text(spec.persona_body)

    knowledge_dir = bundle_dir / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "gotchas.md").write_text(_GOTCHAS_MD)

    if spec.skills:
        skills_dir = bundle_dir / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        for skill in spec.skills:
            _materialize_skill(skill, skills_dir / skill.id)

    (dest / "Dockerfile").write_text(_dockerfile(spec))


def validate_agent_yaml(bundle_dir: Path) -> None:
    """Validate `bundle_dir/agent.yaml` against the vendored daedalus bundle schema.

    Raises ComposeError if the file fails schema validation.
    """
    text = (bundle_dir / "agent.yaml").read_text()
    substituted = _AGENT_MODEL_DEFAULT_RE.sub(r"\1", text)
    data = yaml.safe_load(substituted)

    schema = json.loads(_VENDOR_SCHEMA_PATH.read_text())
    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as exc:
        raise ComposeError(str(exc)) from exc

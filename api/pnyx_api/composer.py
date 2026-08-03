"""Bundle composer: writes a daedalus agent bundle tree to disk and validates
`agent.yaml` against a vendored copy of daedalus's `docs/bundle.schema.json`.

The schema lives at `pnyx_api/vendor/bundle.schema.json`, copied verbatim
from the daedalus repo — never hand-edited.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import jsonschema
import yaml

# Type-only: `pnyx_api.steps` imports `BundleSpec`/`compose`/`validate_agent_
# yaml` from this module, so a real (runtime) import the other way round
# would be circular. `from __future__ import annotations` (below) already
# makes every annotation in this file a lazy string, so `StagedSkill` is only
# ever needed by type checkers, never at import time — `_materialize_skill`
# only ever accesses `.id`/`.dir` on it, duck-typed, no isinstance check.
if TYPE_CHECKING:
    from pnyx_api.steps import StagedSkill

_VENDOR_SCHEMA_PATH = Path(__file__).parent / "vendor" / "bundle.schema.json"

# Pre-validation substitution for the literal `${AGENT_MODEL:-<default>}` placeholder
# written into agent.yaml's `model.id` — collapses it to its default before jsonschema
# sees it (jsonschema has no notion of shell-style parameter expansion).
_AGENT_MODEL_DEFAULT_RE = re.compile(r"\$\{AGENT_MODEL:-([^}]*)\}")

_GOTCHAS_MD = (
    "## Deployment chain\n"
    "You were assembled in the pnyx assembly room and deployed through the\n"
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
        "description": "Assembled by pnyx.",
        "version": "0.1.0",
        "capabilities": [],
        "skills": [{"id": skill.id} for skill in spec.skills],
    }


def _dockerfile(spec: BundleSpec) -> str:
    return (
        "# Agent image: the shared runtime plus this agent's bundle.\n"
        f"FROM {spec.runtime_image}\n"
        "COPY bundle/ /bundle\n"
    )


def _materialize_skill(skill: StagedSkill, dest: Path) -> None:
    """Copy one already-staged skill directory into the not-yet-existing
    `dest`.

    Bucket catalogs (Task 5) replace the old sha-pinned `git archive`
    materialization entirely: `pnyx_api.steps._stage_skills` has already
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

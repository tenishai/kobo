"""Bundle composer: writes a daedalus agent bundle tree to disk and validates
`agent.yaml` against a vendored copy of daedalus's `docs/bundle.schema.json`.

The schema lives at `cortex_api/vendor/bundle.schema.json`, copied verbatim
from the daedalus repo — never hand-edited.
"""

from __future__ import annotations

import json
import re
import shutil
import tarfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import jsonschema
import yaml

from cortex_api.catalog import SkillInfo
from cortex_api.clients import ClientError, CommandRunner

_VENDOR_SCHEMA_PATH = Path(__file__).parent / "vendor" / "bundle.schema.json"

# Pre-validation substitution for the literal `${AGENT_MODEL:-<default>}` placeholder
# written into agent.yaml's `model.id` — collapses it to its default before jsonschema
# sees it (jsonschema has no notion of shell-style parameter expansion).
_AGENT_MODEL_DEFAULT_RE = re.compile(r"\$\{AGENT_MODEL:-([^}]*)\}")

_GOTCHAS_MD = (
    "## Deployment chain\n"
    "You were assembled in the cortex assembly room and deployed through the\n"
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
    skills: list[SkillInfo]  # may be empty
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
        "description": "Assembled by cortex.",
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


def _materialize_skill(skill: SkillInfo, dest: Path, runner: CommandRunner) -> None:
    """Write one skill's tree into the not-yet-existing `dest` directory.

    Pinning (Task 6): when `skill.sha` is set and the skill's source repo is
    a real git repo (has a `.git` entry — `skill.path` is always
    `<skills_repo>/skills/<id>/SKILL.md`, so its second-level parent is the
    repo root), the content is read at that *exact* sha via `git archive`
    piped through `tarfile` in memory — never from whatever the working tree
    currently holds. This is what actually enforces "pinned at assembly":
    a retry (or any later recomposition) that resolves the same recorded sha
    ships identical bytes regardless of where the skills repo's HEAD has
    since moved. `git archive`'s output is captured as raw bytes (`run_bytes`
    — `run()`'s text-mode decode/strip would corrupt a tar archive) and
    extracted without ever shelling out to `tar` or using a shell pipe.

    Skills with no recorded sha, or backed by a plain directory rather than a
    git repo (as in most unit tests' fixtures), fall back to the original
    copytree-from-working-tree behavior — unpinned, but that's the only
    sensible thing to do when there's no commit to pin to.

    Raises ComposeError, naming both the sha and the skill id, if the sha is
    unreachable in the skills repo (e.g. it was recorded, then the repo was
    rewritten/GC'd out from under it).
    """
    skills_repo = skill.path.parents[2]
    if not (skill.sha and (skills_repo / ".git").exists()):
        # Copy the whole skill directory (SKILL.md plus any references/scripts/
        # assets alongside it) rather than symlinking — the sync-plugins
        # copy-don't-symlink pattern, so a composed bundle is self-contained.
        shutil.copytree(skill.path.parent, dest)
        return

    prefix = f"skills/{skill.id}/"
    try:
        archive_bytes = runner.run_bytes(
            [
                "git", "-C", str(skills_repo), "archive", "--format=tar",
                skill.sha, "--", f"skills/{skill.id}",
            ]
        )
    except ClientError as exc:
        raise ComposeError(
            f"skill {skill.id!r} recorded sha {skill.sha!r} is unreachable in "
            f"skills repo {skills_repo}: {exc}"
        ) from exc

    dest.mkdir(parents=True)
    with tarfile.open(fileobj=BytesIO(archive_bytes)) as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.startswith(prefix):
                continue
            relative = member.name[len(prefix):]
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar.extractfile(member)
            target.write_bytes(extracted.read())


def compose(spec: BundleSpec, dest: Path, runner: CommandRunner | None = None) -> None:
    """Write a daedalus agent bundle tree to `dest`.

    Produces:
        dest/Dockerfile
        dest/bundle/agent.yaml
        dest/bundle/agent-card.json
        dest/bundle/persona.md
        dest/bundle/knowledge/gotchas.md
        dest/bundle/skills/<id>/**   (one materialized tree per spec.skills
                                      entry — see `_materialize_skill`)

    `runner` is the CommandRunner used for any sha-pinned skill's `git
    archive` call; defaults to a fresh real one (only ever exercised for
    skills that actually need it — see `_materialize_skill`).
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
        runner = runner or CommandRunner()
        for skill in spec.skills:
            _materialize_skill(skill, skills_dir / skill.id, runner)

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

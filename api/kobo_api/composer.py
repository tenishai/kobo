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
    # Switchboard (cabal) base URL, threaded straight from `Config.cabal_url`
    # (colleagues switchboard arc, Task 4). Presence alone is the composer's
    # own gate for whether the a2a transport carries an `auth` block below —
    # never the fuller `cabal_enabled` truthiness check `kobo_api.clients`
    # exposes to callers that also need the operator token to be present:
    # compose is about the bundle's shape, not whether the room happens to
    # hold a token right now. None (the default) means every pre-existing
    # `BundleSpec(...)` construction (and every call site that never learns
    # about this field) keeps composing byte-identical output.
    cabal_url: str | None = None


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


# Switchboard (cabal) auth-block constants (colleagues switchboard arc, Task
# 4): the issuer is fixed across every agent (it names the switchboard
# itself, never the individual agent), and the JWKS path is the switchboard's
# own well-known discovery endpoint.
_CABAL_ISSUER = "cabal-core"
_CABAL_JWKS_PATH = "/.well-known/jwks.json"


def _cabal_jwks_url(cabal_url: str) -> str:
    """Build the switchboard's JWKS discovery URL from its configured base.

    A single `rstrip("/")` before appending the well-known path, so a
    configured base URL with (or without) a trailing slash never yields a
    doubled (or missing) slash.
    """
    return cabal_url.rstrip("/") + _CABAL_JWKS_PATH


def _a2a_transport(spec: BundleSpec) -> _FlowDict:
    """The bundle's sole a2a transport entry — carries an `auth` block iff
    `spec.cabal_url` is configured (presence alone is the gate; see
    `BundleSpec.cabal_url`'s own docstring for why).

    Always returned as a `_FlowDict` (even in the no-auth case, where a plain
    dict would already render identically under PyYAML's own scalars-only
    flow heuristic) so that adding the nested `auth` mapping — itself
    non-scalar — never flips PyYAML's heuristic to block style: forcing flow
    style here keeps the byte-identical-when-off guarantee and the
    single-line-when-on shape (which `update_agent_yaml_auth` below depends
    on to find and edit this entry) true regardless of that heuristic.
    """
    transport: dict = {"kind": "a2a", "host": "0.0.0.0", "port": 8080}
    if spec.cabal_url:
        transport["auth"] = _FlowDict(
            jwks_url=_DoubleQuoted(_cabal_jwks_url(spec.cabal_url)),
            issuer=_DoubleQuoted(_CABAL_ISSUER),
            audience=_DoubleQuoted(spec.name),
        )
    return _FlowDict(transport)


# The colleague-pass secret key every agent's `tools.colleagues.token_env`
# names (Task 3/B3: every agent is born with a colleague pass) — daedalus's
# `ColleaguesToolProvider` (its own Task 2) reads this exact env var name at
# call time to find its bearer token. A single constant so this module and
# `kobo_api.colleagues.deliver_pass` (which writes the Secret key of the
# same name) can never drift apart.
CABAL_COLLEAGUE_TOKEN_ENV = "CABAL_COLLEAGUE_TOKEN"


def _tools_block(spec: BundleSpec) -> _FlowDict:
    """The bundle's `tools` block — byte-identical to the pre-existing
    `{provider: none, allow: []}` shape when the switchboard isn't
    configured (`spec.cabal_url` unset, the same presence-alone gate
    `_a2a_transport` above uses), or the colleagues tool provider's config
    block (daedalus's `ColleaguesSpec` shape verbatim — `url`/`token_env`/
    `self`, Task 2 of the colleagues switchboard plan) when it is.

    Forced `_FlowDict`/`_DoubleQuoted` the same way `_a2a_transport` forces
    its own `auth` block: keeps the off-path byte-identical guarantee, and
    keeps the on-path rendered on one line — the shape
    `update_agent_yaml_tools` below depends on to find and edit this entry.
    """
    if not spec.cabal_url:
        return _FlowDict(provider="none", allow=[])
    return _FlowDict(
        provider="colleagues",
        colleagues=_FlowDict(
            url=_DoubleQuoted(spec.cabal_url),
            token_env=CABAL_COLLEAGUE_TOKEN_ENV,
            self=_DoubleQuoted(spec.name),
        ),
    )


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
        "transports": [_a2a_transport(spec)],
        "tools": _tools_block(spec),
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
        # Wide enough that the a2a transport entry (including its optional
        # `auth` block) never wraps onto a continuation line: every other
        # emitted line is already well under PyYAML's 80-column default, so
        # this can't change their output, but it keeps the transport entry
        # on one line — the shape `update_agent_yaml_auth`'s single-line
        # regex-based edit depends on.
        width=1000,
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


# Matches the a2a transport's flow-mapping entry anywhere in an already-
# composed `agent.yaml` — this module's own `_render_agent_yaml` (via the
# `width=1000` passed to `yaml.dump`) always renders it on a single line, and
# so does every bundle this function itself ever produces, which is what
# lets a single-line, regex-based surgical edit (matching
# `update_dockerfile_runtime_image`'s own idiom above) find and replace it
# without a full YAML round-trip that would reorder/reformat every other
# line in the file.
#
# `rest` allows any run of non-brace characters interleaved with at most one
# balanced `{...}` group (the entry's own optional `auth: {...}` block,
# whose three values are always plain/quoted strings with no further nested
# braces) — sufficient for every shape this module ever emits or edits, and
# the reason a general-purpose (unbounded-nesting) brace matcher isn't
# needed here.
_A2A_TRANSPORT_LINE_RE = re.compile(
    r"^(?P<prefix>[ \t]*-[ \t]*\{kind:[ \t]*a2a\b)"
    r"(?P<rest>(?:[^{}]|\{[^{}]*\})*)"
    r"\}[ \t]*$",
    re.MULTILINE,
)

# Strips an already-present `, auth: {...}` suffix out of a matched entry's
# `rest` group before a fresh one is appended — the reason a second call
# with the same coordinates reproduces the exact same bytes (idempotence),
# and the reason a call with DIFFERENT coordinates replaces rather than
# appends a second auth block.
_EXISTING_AUTH_BLOCK_RE = re.compile(r",[ \t]*auth:[ \t]*\{[^{}]*\}")


def update_agent_yaml_auth(
    bundle_dir: Path, *, jwks_url: str, issuer: str, audience: str
) -> None:
    """In-place edit of an already-composed `bundle_dir/agent.yaml`'s a2a
    transport entry: add (or replace) its `auth` block, leaving every other
    line in the file byte-identical.

    The backfill path for the two agents composed before the switchboard
    existed (colleagues switchboard arc, Task 6) — and, more generally, for
    any already-composed bundle a caller wants to re-point at a (new) set of
    switchboard coordinates without recomposing the whole bundle (which
    would also require re-downloading every staged skill).

    Same file-editing idiom as `update_dockerfile_runtime_image` above: read
    the whole file as text, apply one targeted `re.subn` (never a generic
    YAML parse/re-dump, which would reorder or reformat lines this module's
    own custom flow-style dumper deliberately controls elsewhere in the
    file), and raise rather than silently no-op when the thing being edited
    isn't found.

    Idempotent by construction: a second call with the same `jwks_url`/
    `issuer`/`audience` first strips the auth block the previous call wrote
    (via `_EXISTING_AUTH_BLOCK_RE`) and then re-adds the identical block, so
    the file's bytes never drift on a repeat call. A call with DIFFERENT
    coordinates replaces the block rather than appending a second one, for
    the same reason.

    Only the a2a transport's own entry is ever touched — any other
    transport kind (slack, cli, ...) elsewhere in the same `transports:`
    list is left byte-for-byte alone, since `_A2A_TRANSPORT_LINE_RE` only
    matches a line whose flow mapping starts `{kind: a2a`.

    Raises `ClientError` (never silently no-ops), mirroring
    `update_dockerfile_runtime_image`'s own two failure modes:

    - `agent.yaml` is missing entirely: `Path.read_text()` raises `OSError`.
    - No line in the file matches `_A2A_TRANSPORT_LINE_RE` (no a2a transport
      entry to update): `re.subn`'s match count is 0.
    """
    agent_yaml_path = bundle_dir / "agent.yaml"
    try:
        text = agent_yaml_path.read_text()
    except OSError as exc:
        raise ClientError(f"agent.yaml not found: {agent_yaml_path}") from exc

    def _replace(match: re.Match) -> str:
        prefix = match.group("prefix")
        rest = _EXISTING_AUTH_BLOCK_RE.sub("", match.group("rest"))
        auth_block = (
            f', auth: {{jwks_url: "{jwks_url}", issuer: "{issuer}", '
            f'audience: "{audience}"}}'
        )
        return f"{prefix}{rest}{auth_block}}}"

    updated, count = _A2A_TRANSPORT_LINE_RE.subn(_replace, text, count=1)
    if count == 0:
        raise ClientError(
            f"agent.yaml has no a2a transport entry to update: {agent_yaml_path}"
        )
    agent_yaml_path.write_text(updated)


# Matches the whole `tools:` top-level line anywhere in an already-composed
# `agent.yaml` — this module's own `_render_agent_yaml` (via the same
# `width=1000` that keeps the a2a transport entry on one line) always
# renders `tools:` on a single line too, whichever shape `_tools_block`
# produced (the off-path `{provider: none, allow: []}` or the on-path
# colleagues config), so a single-line, regex-based surgical edit — same
# idiom as `_A2A_TRANSPORT_LINE_RE` above — can find and replace the whole
# line without a full YAML round-trip that would reorder/reformat every
# other line in the file. `tools:` is a top-level key (column 0, never
# indented under something else), unlike the a2a transport entry (a list
# item under `transports:`), so this pattern anchors on that directly
# rather than mirroring the list-item prefix the transport regex matches.
_TOOLS_LINE_RE = re.compile(r"^tools:[ \t]*\{.*\}[ \t]*$", re.MULTILINE)


def update_agent_yaml_tools(
    bundle_dir: Path, *, url: str, token_env: str, self_name: str
) -> None:
    """In-place edit of an already-composed `bundle_dir/agent.yaml`'s
    `tools:` line: replace it wholesale with the colleagues tool provider's
    config block (daedalus's `ColleaguesSpec` shape verbatim), leaving every
    other line in the file byte-identical.

    The backfill path for agents composed before this feature existed (same
    role `update_agent_yaml_auth` plays for the a2a auth block) — and, more
    generally, for any already-composed bundle a caller wants to re-point at
    a (new) set of switchboard coordinates without recomposing the whole
    bundle.

    Same file-editing idiom as `update_agent_yaml_auth` above: read the
    whole file as text, apply one targeted `re.subn` against a replacement
    function (never a plain substitution string — a literal `url`/
    `self_name` containing a backslash or a `\\g<...>`-shaped run must never
    be misread as a backreference, same reasoning `update_dockerfile_
    runtime_image` documents for its own replacement function), and raise
    rather than silently no-op when the thing being edited isn't found.

    Add-or-replace in one code path: since the whole `tools:` line is always
    replaced wholesale (never patched in place), this works identically
    whether the line being replaced is the composed default
    (`{provider: none, allow: []}`) or an earlier `update_agent_yaml_tools`
    call's own colleagues block — there is no separate "add" branch.

    Idempotent by construction: a second call with the same `(url,
    token_env, self_name)` replaces the line with byte-identical text, so
    the file's contents never drift on a repeat call.

    Raises `ClientError` (never silently no-ops), mirroring
    `update_agent_yaml_auth`'s own two failure modes:

    - `agent.yaml` is missing entirely: `Path.read_text()` raises `OSError`.
    - No line in the file matches `_TOOLS_LINE_RE` (no `tools:` line to
      replace): `re.subn`'s match count is 0.
    """
    agent_yaml_path = bundle_dir / "agent.yaml"
    try:
        text = agent_yaml_path.read_text()
    except OSError as exc:
        raise ClientError(f"agent.yaml not found: {agent_yaml_path}") from exc

    replacement = (
        "tools: {provider: colleagues, colleagues: "
        f'{{url: "{url}", token_env: {token_env}, self: "{self_name}"}}}}'
    )

    updated, count = _TOOLS_LINE_RE.subn(lambda match: replacement, text, count=1)
    if count == 0:
        raise ClientError(f"agent.yaml has no tools: line to update: {agent_yaml_path}")
    agent_yaml_path.write_text(updated)


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

"""Bundle composer: writes a daedalus agent bundle tree to disk and validates
`agent.yaml` against a vendored copy of daedalus's `docs/bundle.schema.json`.

The schema lives at `kobo_api/vendor/bundle.schema.json`, copied verbatim
from the daedalus repo and never hand-edited. The current copy is daedalus
v0.5.5's `docs/bundle.schema.json` (copied 2026-10-18; the copy before it
was 0.4.0.dev0's, issue #152). When daedalus releases a schema change,
re-copy the file and re-run the composer tests: `compose()` validates every
shape kobo writes against it, and `test_vendored_schema_is_the_current_copy`
pins which copy this is.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

import jsonschema
import yaml

from kobo_api.archetypes import ARCHETYPE_TEAM_GUIDE
from kobo_api.clients import ClientError

# Type-only: `kobo_api.steps` imports `BundleSpec`/`compose`/`validate_agent_
# yaml` from this module, so a real (runtime) import the other way round
# would be circular. `from __future__ import annotations` (below) already
# makes every annotation in this file a lazy string, so `StagedSkill` is only
# ever needed by type checkers, never at import time — `_materialize_skill`
# only ever accesses `.id`/`.dir` on it, duck-typed, no isinstance check.
if TYPE_CHECKING:
    from kobo_api.steps import StagedSkill

log = logging.getLogger(__name__)

_VENDOR_SCHEMA_PATH = Path(__file__).parent / "vendor" / "bundle.schema.json"

# Pre-validation substitution for the literal `${AGENT_MODEL:-<default>}` placeholder
# written into agent.yaml's `model.id` — collapses it to its default before jsonschema
# sees it (jsonschema has no notion of shell-style parameter expansion).
_AGENT_MODEL_DEFAULT_RE = re.compile(r"\$\{AGENT_MODEL:-([^}]*)\}")


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
    # The agent's ACL namespace (`kobo_api.models.Agent.group_name`'s own
    # docstring): DISTINCT from `team` above -- `team` is the skills-catalog
    # capability axis, `group` is the access-control namespace, and "the two
    # must never be conflated." `_self_md` below renders both as separate,
    # honestly-labelled facts; a prior version of this module conflated them
    # by rendering `team`'s value under the label "group" and carrying no
    # group fact at all. None (the default) means every pre-existing
    # `BundleSpec(...)` construction keeps composing unchanged; `_self_md`
    # reads it with its own "ungrouped" fallback for that None case.
    group: str | None = None
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
    # The agent's own distilled capability (self-card knowledge, standing-
    # knowledge arc, Task 4): already on the Agent row (`agent.capability`,
    # persona-metadata plan) but never before threaded onto BundleSpec --
    # `_agent_card` below always hardcodes an empty string for the baked
    # agent-card.json (capability is "earned" later, well after compose
    # time), so this is a genuinely new field, not a rename of one that was
    # already here. None (the default) means every pre-existing
    # `BundleSpec(...)` construction keeps composing unchanged; `_self_md`
    # below reads it with its own "not yet distilled" fallback for exactly
    # that None case, which is what a freshly composed agent always is.
    capability: str | None = None
    # The colleague roster (standing-knowledge arc, Task 5): the room's own
    # view of who this agent may reach, as plain dicts shaped
    # `{"name", "team", "purpose_tags", "capability"}` -- matching daedalus's
    # own `_format_colleague` shape so the standing knowledge and the live
    # `list_colleagues` tool read alike. Defaulted empty so every
    # pre-existing `BundleSpec(...)` construction keeps composing unchanged.
    colleagues: list[dict] = field(default_factory=list)
    # Agent memory replication (resilience arc, Task 14): the object-storage
    # bucket this agent's techne database replicates to continuously from
    # birth, threaded straight from `Config.memory_replica_bucket` by
    # `kobo_api.steps.RepoStep`. Empty (the default) means the feature is
    # off -- `compose()` below emits no litestream.yml, no entrypoint.sh,
    # and no Dockerfile ENTRYPOINT override, byte-identical to every
    # pre-existing `BundleSpec(...)` construction across the test suite.
    # Presence alone is the gate, same posture `cabal_url` above already
    # takes: this field describes the bundle's own shape, not whether some
    # fuller "is replication actually wired" check would also want to see
    # (there is no such fuller check today -- the bucket name is the whole
    # contract).
    memory_replica_bucket: str = ""
    # The room's own in-cluster base URL (org key broker_url) -- when set
    # alongside cabal_url, the composed tools block gains the github
    # sidecar (daedalus GitHubSpec: broker_url/token_env/self) and
    # the github tools (open_pr and the read quartet) join the allow list.
    # Empty = no github block at all.
    broker_url: str = ""
    # The MCP gateway's data-plane URL (external-MCP arc). Empty = no
    # liminal block composed. The exchange rides cabal_url (the
    # /auth/liminal-token route), so wiring is gated on BOTH coordinates,
    # the same both-or-nothing rule the github block applies.
    liminal_url: str = ""
    # The create-time archetype this agent was born from (`kobo_api.models.
    # Agent.archetype`, threaded by `kobo_api.steps.RepoStep`), or None for
    # an ordinary agent -- which every pre-existing `BundleSpec(...)`
    # construction across the test suite is, so the default keeps them all
    # composing byte-identical output. Read ONLY by `_tools_block` below,
    # and only to SUPPRESS: a team-guide kiosk composes with no action
    # tools at all, because the github and liminal blocks come from org
    # config (`broker_url`/`liminal_url`) and would otherwise reach every
    # bundle the room composes -- `github.open_pr` included. Compared
    # against the named archetype constant, never truth-tested, so a future
    # archetype composes the ordinary surface until it asks for its own.
    archetype: str | None = None
    # The owner's colleague mode (issue #131), one of `COLLEAGUE_MODES`
    # below, already resolved from the row by `effective_colleague_mode`.
    # Read by `_tools_block` (which colleague tools reach the allow list),
    # `_colleagues_md` (which doctrine the roster carries) and `compose`
    # (whether colleagues.md is written at all). The default composes
    # byte-identical output to every bundle before the setting existed.
    colleague_mode: str = "default"


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

# The colleagues tools allow-list: the set of colleagues-provider tools
# every agent can call (when the switchboard is configured). Shared by both
# the compose-time emission (`_tools_block`) and the backfill helper
# (`update_agent_yaml_tools`) so they stay synchronized.
_COLLEAGUES_TOOLS_ALLOW = ["colleagues.list_colleagues", "colleagues.ask_colleague", "colleagues.remember"]
_COLLEAGUES_ASK_TOOL = "colleagues.ask_colleague"

# daedalus 0.5.0 delivers assigned skills as an index in the system prompt
# plus this tool, and only when the allow list admits it (issue #137).
# Older runtimes only filter offered tools by the allow list, so a name no
# provider offers there matches nothing: no runtime version gate needed.
_SKILLS_LOAD_TOOL = "skills.load"

# Per-agent colleague mode (issue #131), set by the agent's owner:
# - default: every colleague tool and the consult-first doctrine, the
#   standing operator ruling for every agent.
# - roster-only: the roster stays as knowledge, ask_colleague leaves the
#   allow list, and the doctrine becomes "say you don't know, ask your
#   human". For an agent whose job is a narrow evidence chain.
# - none: no colleague tools and no colleagues.md.
# Only the agent's OUTBOUND reach changes. Its a2a transport, its colleague
# pass and the provider block stay, because the github and liminal
# sidecars authenticate with that same pass, and other agents may still
# ask it questions.
COLLEAGUE_MODE_DEFAULT = "default"
COLLEAGUE_MODE_ROSTER_ONLY = "roster-only"
COLLEAGUE_MODE_NONE = "none"
COLLEAGUE_MODES = (COLLEAGUE_MODE_DEFAULT, COLLEAGUE_MODE_ROSTER_ONLY, COLLEAGUE_MODE_NONE)


def effective_colleague_mode(value: str | None) -> str:
    """The row's stored mode, with NULL (every agent from before the
    setting) and anything unrecognised read as `default`."""
    return value if value in COLLEAGUE_MODES else COLLEAGUE_MODE_DEFAULT


def colleague_tools_allow(mode: str) -> list[str]:
    """The colleague tools `mode` puts in the allow list, in the order the
    composer has always written them."""
    if mode == COLLEAGUE_MODE_NONE:
        return []
    if mode == COLLEAGUE_MODE_ROSTER_ONLY:
        return [t for t in _COLLEAGUES_TOOLS_ALLOW if t != _COLLEAGUES_ASK_TOOL]
    return list(_COLLEAGUES_TOOLS_ALLOW)


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
    skills_allow = [_SKILLS_LOAD_TOOL] if spec.skills else []
    if not spec.cabal_url:
        return _FlowDict(provider="none", allow=_FlowList(skills_allow))
    allow = colleague_tools_allow(spec.colleague_mode) + skills_allow
    block = _FlowDict(
        provider="colleagues",
        allow=None,  # placed below, once the github arm has decided
        colleagues=_FlowDict(
            url=_DoubleQuoted(spec.cabal_url),
            token_env=CABAL_COLLEAGUE_TOKEN_ENV,
            self=_DoubleQuoted(spec.name),
        ),
    )
    # A team-guide kiosk carries colleagues + memory ONLY (team-guide spec
    # 2026-09-14). Its constitution says it never acts, and the create lane
    # forces its persona, tags and skills -- but the two sidecars below are
    # composed from ORG coordinates, not from anything the caller sends, so
    # without this gate every kiosk would still be born holding
    # `github.open_pr`. "Never act" is enforced HERE, in the bundle itself,
    # where the runtime reads it -- structurally: the kiosk returns before
    # either sidecar arm below ever runs, so a future org-config sidecar
    # cannot reach a kiosk just by forgetting its own `not never_act` check.
    # Deliberately a suppression of the two action sidecars and nothing
    # else: the colleagues provider, the colleague allow-list and the
    # memory block above/below are untouched (the guide still talks and
    # still remembers).
    never_act = spec.archetype == ARCHETYPE_TEAM_GUIDE
    if never_act:
        block["allow"] = _FlowList(allow)   # colleagues trio (+ skills.load) only
        return block
    # The github sidecar (agent PR broker spec): rides ALONGSIDE the
    # colleagues provider (daedalus composes the two), gated on BOTH
    # coordinates -- the broker route authenticates the agent by the same
    # colleague JWT the cabal wiring issues, so a broker with no
    # switchboard could never authenticate anyone.
    if spec.broker_url:
        # Explicit names, never a github.* glob -- the bundle's allow list is
        # law, and a glob would silently grant every tool the provider grows
        # later. open_pr is the one write (PR-only by ruling); the read
        # quartet (2026-08-28, the schema-survey unblock) rides the
        # same mint-per-call broker token.
        allow.extend(
            [
                "github.open_pr",
                "github.list_prs",
                "github.get_pr",
                "github.list_releases",
                "github.read_file",
            ]
        )
        block["github"] = _FlowDict(
            broker_url=_DoubleQuoted(spec.broker_url),
            token_env=CABAL_COLLEAGUE_TOKEN_ENV,
            self=_DoubleQuoted(spec.name),
        )
    # The liminal sidecar (external-MCP arc): the gateway block is a
    # BIRTHRIGHT, not a grant -- deny-by-default at the gateway means a
    # wired agent with no policy row sees zero tools, so wiring everyone
    # is safe and grants stay the control point. Gated on BOTH
    # coordinates like github: the exchange (/auth/liminal-token) lives
    # at the switchboard, so a gateway with no switchboard could never
    # authenticate anyone. Deliberately NO allow entries here: which
    # gateway tools an agent may call arrives with its grant (K3), never
    # as a composed default.
    if spec.liminal_url:
        block["liminal"] = _FlowDict(
            url=_DoubleQuoted(spec.liminal_url),
            token_env=CABAL_COLLEAGUE_TOKEN_ENV,
            exchange_url=_DoubleQuoted(spec.cabal_url),
        )
    block["allow"] = _FlowList(allow)
    return block


def _approvals_gates(spec: BundleSpec) -> list | _FlowList:
    """The bundle's `approvals.gates` list -- composes EMPTY when the
    colleagues tools block is emitted (`spec.cabal_url` configured, the same
    presence-alone gate `_a2a_transport`/`_tools_block` above use), per the
    "Amendments from live acceptance" section of `docs/superpowers/specs/
    2026-08-19-colleagues-talk-design.md`.

    Rationale (from the amendment): the runtime consults gates via
    first-fnmatch-wins, and a deployed pod's approver is deny-all, so the
    bundle's blanket `match: "*"` gate below blocks every tool outright --
    including the two colleague tools the explicit `tools.allow` list alone
    is meant to gate. Per the operator's open-discovery ruling, colleague
    conversations need no human signature: the law lives in the explicit
    tool allowlist (nothing outside it is even visible to the model) and the
    untouched write ceiling. `ceiling` and `escalation` in `_agent_yaml_data`
    below are never touched by this gate; a future write-capable tool
    composes its own gates deliberately, never by resurrecting this one.

    Byte-identical to the pre-existing shape when off (`spec.cabal_url`
    unset, the same gate as everywhere else in this module) -- a plain `[]`
    (rather than `_FlowList([])`) renders identically to a forced one since
    PyYAML's own flow heuristic already picks flow style for an empty
    collection, mirroring `_tools_block`'s own empty `allow: []`.
    """
    if spec.cabal_url:
        return []
    return _FlowList([{"match": _DoubleQuoted("*"), "require": "plan_approval"}])


# The revision-pass mode this room composes for every agent (belief-revision
# plan, Task 2): daedalus's `MemorySpec.revision` gates a post-boot
# belief-revision pass that auto-promotes operator-attested corrections, per
# `docs/superpowers/specs/2026-08-20-belief-revision-design.md`. Used
# unconditionally in `_agent_yaml_data` below (never gated on a check of the
# memory provider) because `BundleSpec` exposes no field that selects
# anything other than techne memory -- every bundle this room composes
# already IS a techne bundle (`tests/test_composer.py`'s own
# `test_bundle_spec_has_no_memory_provider_selector` pins this). If a future
# `BundleSpec` grows a real memory-provider selector, this constant's use
# below should grow a matching conditional.
_ROOM_MEMORY_REVISION = "operator"


def _agent_yaml_data(spec: BundleSpec) -> dict:
    return {
        "name": spec.name,
        "card": "agent-card.json",
        "persona": "persona.md",
        "model": {
            "id": f"${{AGENT_MODEL:-{spec.model_id}}}",
            "max_tokens": 8192,
            # No per-task token budget (2026-09-08 ruling): max_steps bounds
            # the loop, prompt caching bounds the cost, and a budget sized
            # for chat starved every real multi-tool task -- three live
            # exhaustions (an audit twice, a release survey once) before the
            # pattern was named.
        },
        "transports": [_a2a_transport(spec)],
        "tools": _tools_block(spec),
        "memory": {"personal": "techne", "home": "/techne", "revision": _ROOM_MEMORY_REVISION},
        "checkpoints": "sqlite",
        "approvals": {
            # No `ceiling`: the runtime reads only `ceiling.exec`, and a
            # documented ceiling nobody enforces is worse than none (agora
            # discussion #55, item 7; daedalus will warn, then fail, on it).
            "gates": _approvals_gates(spec),
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


_SKILLS_LOAD_SENTENCE = (
    "The Skills list in your instructions names them, and `skills.load(name)` "
    "reads one in full. "
)


def _self_md(spec: BundleSpec) -> str:
    """Standing self-knowledge, same lane `_GOTCHAS_MD` used to occupy (the
    wire shows agents quote that lane verbatim). Issue #5: asked which
    project it ran in, an agent asked a colleague about ITSELF, and that
    colleague asked it back. Self-targeting was already blocked; what was
    missing is facts about itself and the instruction not to delegate
    questions about itself.

    Group and team are rendered as two separate, honestly-labelled facts:
    `spec.group` is the ACL namespace, `spec.team` is the skills-catalog
    capability axis (`kobo_api.models.Agent.group_name`'s own docstring --
    "the two must never be conflated"). A prior version of this function
    rendered `spec.team`'s value under the "your group" label and carried
    no group fact at all, so every agent confidently misreported its own
    group -- this is what closes that gap.

    The closing paragraph (operator ruling 2026-08-27, Step 1b) is the
    deployment-chain fact that used to live in a standalone gotchas file --
    the operator liked agents knowing their origin, what he ruled out was a
    whole file of it, so it moves here, into the self-card, instead.
    """
    tags = ", ".join(spec.purpose_tags) if spec.purpose_tags else "none"
    return (
        "## Who you are\n"
        f"You are {spec.name}. Your group is {spec.group or 'ungrouped'}. "
        f"Your team is {spec.team}. "
        f"Your purpose tags are: {tags}.\n"
        f"What you are for: {spec.capability or 'not yet distilled'}.\n"
        "\n"
        "The rule: questions about you are yours to answer: your name, your "
        "group, your team, what you are for, what you remember. Never ask a "
        "colleague about yourself. When you genuinely do not have a fact "
        "about yourself, say \"I don't know yet\" and stop, rather than "
        "asking someone else to answer it for you.\n"
        "\n"
        "Your skills are PROCEDURES seeded into your memory, never tools: "
        "each one lives as a '## Skill: <name>' entry you can recall. "
        f"{_SKILLS_LOAD_SENTENCE if spec.skills else ''}When "
        "someone asks you to use a skill, recall that procedure and follow "
        "its steps with the tools you actually have -- there is no function "
        "named after it to invoke, and saying a skill is 'not loaded' when "
        "its procedure sits in your memory is wrong. When a step needs a "
        "capability you lack (a shell, an API you cannot reach), say which "
        "step and ask for that capability, rather than disclaiming the "
        "whole skill.\n"
        "\n"
        "You were assembled in the kobo assembly room and deployed through "
        "the platform chain: repo, onboarding, build, deploy, first contact. "
        "If someone asks how you got here, that is the answer.\n"
    )


def _colleague_capability_line(c: dict) -> str:
    """The "What they are for" fragment for one colleague roster entry
    (capability fallback chain, standing-knowledge arc, Task 7): capability
    text first, falling back to purpose tags when it's empty, and to skills
    when THOSE are also empty -- "not yet distilled" only when all three are
    empty. An agent nobody can match on any of the three is invisible to
    the suggest-a-colleague feature below, which is why this fallback is
    required rather than nice to have: a colleague whose capability has not
    yet been earned (every agent, at birth) must still be describable well
    enough to name and suggest.
    """
    capability = c.get("capability")
    if capability:
        return capability
    purpose_tags = c.get("purpose_tags") or []
    if purpose_tags:
        return ", ".join(purpose_tags)
    skills = c.get("skills") or []
    if skills:
        return ", ".join(skills)
    return "not yet distilled"


def _colleagues_md(spec: BundleSpec) -> str:
    """The roster, as standing knowledge rather than a tool call. The data
    was always available (daedalus's list_colleagues already formats name,
    team, purpose tags and capability) but only if the model chose to call
    it, and issue #5 shows it skipping that and asking a test fixture a GCP
    question. Knowing who exists is not a tool call.

    Point-in-time: refreshed on delivery. list_colleagues stays the live
    read, ask_colleague stays the action. Since issue #274 the roster, not
    the live tool, is the one the agent is told to trust: the switchboard's
    directory can carry agents the room has destroyed or never paired with
    this one, and until it answers per caller the live read over-reports.

    The three closing paragraphs are the consult-first doctrine, its
    own-field/uncovered-subject boundary, and the attribution counterpart.
    History, because each sentence closes a specific observed failure:

    - 2026-08-27 (psc incident): a fresh agent silently asked a colleague,
      presented the answer as its own, and next turn claimed it came from
      training data. Not a lie by intent: the colleague hop is invisible in
      the replayed history, so the agent genuinely cannot remember it
      happened. Naming the colleague at answer time is the only fix that
      survives statelessness -- the attribution paragraph.
    - 2026-08-27 (operator ruling, later the same day): the first cut of
      this instruction said "offer to bring them in" and "do not ask them
      silently" -- and an agent asked about PSC answered from training data
      and was satisfied with that. The ruling: the sin in the psc incident
      was the SILENCE, never the consulting. An agent's general model
      knowledge is what any assistant anywhere would tell anyone; the whole
      value of this room is answers scoped to how THIS organization does
      things, which live with the colleague whose field it is. So the
      default flipped: for organization questions in a colleague's field,
      consult first and attribute, rather than suggest-and-wait.

    Present unconditionally (not just in the `others` branch): a roomful of
    zero colleagues today may gain one after the refresh lane delivers a
    fresh roster, and the instruction should already be standing when that
    happens.

    `roster-only` (issue #131) keeps the snapshot and the roster, but the
    roster is introduced as who is in the room rather than who to ask, and
    the three closing paragraphs become `_ROSTER_ONLY_DOCTRINE`: the agent
    has no ask_colleague tool, so a doctrine telling it to consult first
    would only compete with that. `none` never reaches here (`compose`
    writes no colleagues.md for it).
    """
    if spec.colleague_mode == COLLEAGUE_MODE_ROSTER_ONLY:
        return _colleagues_md_roster_only(spec)
    others = [c for c in spec.colleagues if c["name"] != spec.name]
    lines = ["## Your colleagues", ""]
    lines.append(
        "This roster is who you may talk to. It was written when you were "
        "assembled and the room refreshes it when colleagues join or leave. "
        "The list_colleagues tool reads the switchboard live and may list "
        "agents you cannot reach or that no longer exist; this roster is "
        "the list that counts."
    )
    lines.append("")
    if not others:
        lines.append(
            "You have no colleagues in the room yet. Until you do, every "
            "question is yours: answer from your own knowledge and memory."
        )
    else:
        lines.append(
            "These are the other agents you can reach, and what each is for. "
            "When a question is squarely in someone else's field and outside "
            "yours, ask them rather than guessing. Match on what they are "
            "for, not on whose name you happen to remember."
        )
        lines.append("")
        lines.extend(_colleague_roster_lines(others))
    lines.append("")
    lines.append(
        "You are one specialist in a room of specialists, deployed for one "
        "specific organization. Your own model knowledge is what any "
        "assistant anywhere would say to anyone; it is not how this "
        "organization does things. For a question about this "
        "organization's own setup (its infrastructure, services, tooling, "
        "teams, or process) that might concern a colleague's field, "
        "consult that colleague with ask_colleague FIRST and answer "
        "from what they say, naming them in your reply. When in doubt "
        "whether a question concerns them, lean toward asking: their "
        "answer is scoped to how this organization actually works, and "
        "yours is not. Do not answer an organization question from "
        "general knowledge while a colleague who owns that subject is "
        "reachable."
    )
    lines.append("")
    lines.append(
        "Questions squarely in your own field you answer yourself; that "
        "is what you are for. When no reachable colleague covers a "
        "subject, you may fall back on general knowledge, but say so "
        "plainly: label the answer as general practice, not as how this "
        "organization does it."
    )
    lines.append("")
    lines.append(
        "When a colleague's answer informs yours, say so and name them. "
        "You will not remember the consultation next turn (colleague "
        "exchanges are not in your replayed history), so the attribution "
        "in your reply is the only record you get: without it you will "
        "later misremember their work as your own knowledge."
    )
    return "\n".join(lines) + "\n"


# The paragraph that replaces the consult-first doctrine for a roster-only
# agent (issue #131). Its owner turned delegation off because the agent's
# job is a narrow evidence chain: an answer from a colleague, or from
# general knowledge, is not evidence.
_ROSTER_ONLY_DOCTRINE = (
    "Your owner has set you to answer only from your own tools and memory. "
    "When they can't answer a question, say you don't know, and name what "
    "you would need to answer it (the data, the access, or the tool). Ask "
    "your human for that. Don't ask another agent, even one whose field it "
    "is, and don't fill the gap from general knowledge."
)


def _colleague_roster_lines(others: list[dict]) -> list[str]:
    return [
        f"- {c['name']} (team: {c.get('team') or 'unspecified'}), "
        f"purpose tags: {', '.join(c.get('purpose_tags') or []) or 'none'}. "
        f"What they are for: {_colleague_capability_line(c)}"
        for c in others
    ]


def _colleagues_md_roster_only(spec: BundleSpec) -> str:
    """`_colleagues_md` for a roster-only agent: the same snapshot and
    roster, read as knowledge of who is in the room, and the replacement
    doctrine in place of the consult-first paragraphs."""
    others = [c for c in spec.colleagues if c["name"] != spec.name]
    lines = ["## Your colleagues", ""]
    lines.append(
        "This roster is who is in the room with you. It was written when "
        "you were assembled and the room refreshes it when colleagues join "
        "or leave. The list_colleagues tool reads the switchboard live and "
        "may list agents that are not in your room or no longer exist; this "
        "roster is the list that counts."
    )
    lines.append("")
    if not others:
        lines.append("You have no colleagues in the room yet.")
    else:
        lines.append(
            "These are the other agents in the room, and what each is for. "
            "This is so you know who is here and can tell your human who "
            "owns a subject; it is not a list of agents to consult."
        )
        lines.append("")
        lines.extend(_colleague_roster_lines(others))
    lines.append("")
    lines.append(_ROSTER_ONLY_DOCTRINE)
    return "\n".join(lines) + "\n"


# The card's description is the agent's capability sentence once it has
# one, and until then the persona's own first sentence: the same fallback
# tier `kobo_api.metadata.generate_capability` uses, so the card and the
# capability never disagree about what the agent is (issue #147). The text
# helpers live here, not in `kobo_api.metadata`, because that module imports
# this one; metadata re-exports them.
CAPABILITY_MAX_LEN = 160
_DASH_RE = re.compile("[–—]")
_SENTENCE_END_RE = re.compile(r"[.!?\n]")


def _plain_copy(text: str) -> str:
    """Collapse whitespace to single spaces and strip em/en dashes (replaced
    with a hyphen, so adjacent words stay apart)."""
    return " ".join(_DASH_RE.sub("-", text).split())


def _first_sentence(text: str) -> str:
    """The first sentence of `text` (up to the first ./!/?/newline, or the
    whole text if none), whitespace-collapsed. A leading boundary character
    falls back to the whole (collapsed, boundary-stripped) text rather than
    an empty string."""
    match = _SENTENCE_END_RE.search(text)
    raw = text[: match.start()] if match else text
    sentence = " ".join(raw.split())
    if not sentence:
        sentence = " ".join(text.split()).lstrip(".!?").strip()
    return sentence


def fallback_capability(persona_body: str) -> str:
    """The persona body's first sentence, cleaned and clamped."""
    return _plain_copy(_first_sentence(persona_body))[:CAPABILITY_MAX_LEN]


def runtime_version(runtime_image: str) -> str:
    """The card's `version`: the runtime image's tag (`...:0.5.1` -> `0.5.1`,
    also for `...:0.5.1@sha256:...`), or `sha256:<12 hex>` for a digest-only
    ref, or `latest` for a bare repository (what a registry resolves it to).
    The agent's repository has no version of its own, and the runtime build
    is what changes what an agent can do, so the card names that (issue
    #147).

    A digest-only ref names no version, and the runtime images set no
    `org.opencontainers.image.version` label, so the digest prefix is the
    only honest identifier kobo has without asking the registry (issue
    #156). Pin `tag@digest` to keep both the exact build and a version."""
    name, _, digest = runtime_image.partition("@")
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        return last.split(":", 1)[1]
    if digest:
        algo, _, hexpart = digest.partition(":")
        return f"{algo}:{hexpart[:12]}"
    return "latest"


def _card_skills(skill_ids: list[str]) -> list[dict]:
    return [{"id": skill_id} for skill_id in sorted(skill_ids)]


def _agent_card(spec: BundleSpec) -> dict:
    return {
        "name": spec.name,
        "description": spec.capability or fallback_capability(spec.persona_body),
        "version": runtime_version(spec.runtime_image),
        # A2A's AgentCapabilities is an object of protocol feature flags
        # (streaming, push_notifications, state_transition_history); these
        # agents declare none, so it is empty, never a list (issue #147).
        "capabilities": {},
        "skills": _card_skills([skill.id for skill in spec.skills]),
        # Persona metadata (Task 3): baked in at compose time so every agent
        # card carries this block from birth — `capability` starts empty
        # (never earned yet; HelloStep's tail is what first fills it in),
        # `team`/`purpose_tags` come straight from the spec (Task 2's own
        # values, threaded here by whoever builds the spec —
        # `kobo_api.steps.RepoStep`). `runtime_image` is the full ref the
        # Dockerfile's FROM names (issue #147).
        "metadata": {
            "team": spec.team,
            "purpose_tags": list(spec.purpose_tags),
            "capability": "",
            "runtime_image": spec.runtime_image,
        },
    }


# Agent memory replication (resilience arc, Task 14): litestream release
# fetched by tag directly into the image rather than a multi-stage `COPY
# --from=` (which would need a second `FROM` line) -- `_DOCKERFILE_FROM_RE`
# below (and every caller of `update_dockerfile_runtime_image`, which
# re-pins a REPO'S OWN Dockerfile after compose time) assumes exactly one
# `FROM` line exists; a second one would make that re-pin silently target
# the wrong stage. Keeping a single `FROM` line -- the runtime image's own
# -- regardless of whether replication is on is a load-bearing invariant of
# this module, not just this feature's own preference.
_LITESTREAM_VERSION = "0.3.13"
_LITESTREAM_RELEASE_URL = (
    "https://github.com/benbjohnson/litestream/releases/download/"
    # The release ASSET carries the v prefix too (litestream-v0.3.13-...),
    # not just the tag -- the un-prefixed spelling 404s at image build.
    f"v{_LITESTREAM_VERSION}/litestream-v{_LITESTREAM_VERSION}-linux-amd64.tar.gz"
)

_MEMORY_REPLICA_DOCKERFILE_BLOCK = (
    "\n"
    "# Agent memory replication (resilience arc, Task 14): a sidecar\n"
    "# container cannot ride this bundle (the pod spec belongs to the\n"
    "# platform, not the room), so replication runs INSIDE this image,\n"
    "# supervised by the entrypoint below -- litestream replicates\n"
    "# techne.db to object storage continuously while the runtime's own\n"
    "# command runs as its supervised child.\n"
    "# The runtime image runs as its own nonroot user (USER agent in the\n"
    "# daedalus Dockerfile): ADD lands the tarball root-owned mode 600 and\n"
    "# /usr/local/bin is not agent-writable, so the install needs a root\n"
    "# window -- and must hand the image back to that same user after,\n"
    "# or the runtime's whole nonroot posture silently widens to root.\n"
    "USER root\n"
    f"ADD {_LITESTREAM_RELEASE_URL} /tmp/litestream.tar.gz\n"
    "RUN tar -C /usr/local/bin -xzf /tmp/litestream.tar.gz litestream "
    "&& rm /tmp/litestream.tar.gz\n"
    "COPY litestream.yml /etc/litestream.yml\n"
    "COPY entrypoint.sh /entrypoint.sh\n"
    "RUN chmod +x /entrypoint.sh\n"
    "USER agent\n"
    'ENTRYPOINT ["/entrypoint.sh"]\n'
)

# The techne PVC mount path (matches kobo_api.steps.WireStep's own
# `PVC_YAML_TEMPLATE`, mounted at `/techne`) and the sqlite file daedalus's
# techne memory provider writes there -- litestream.yml's `dbs[0].path`
# below must name the exact same file that mount actually holds.
_TECHNE_DB_PATH = "/techne/techne.db"

_ENTRYPOINT_SH = (
    "#!/bin/sh\n"
    "# Agent memory replication supervision (resilience arc, Task 14):\n"
    "# litestream replicates techne.db to object storage continuously\n"
    "# while the runtime's own command runs as its supervised child -- if\n"
    "# that child exits, litestream exits too (an agent with a dead\n"
    "# runtime should not keep \"replicating\" nothing).\n"
    "#\n"
    "# Critical 4 fix: this ENTRYPOINT REPLACES the base runtime image's\n"
    "# own (docker rule: declaring a new ENTRYPOINT resets the base\n"
    "# image's CMD to nothing), so this script receives no positional\n"
    "# arguments at all to hand off to litestream's -exec, and even a\n"
    "# preserved CMD would just hand -exec the bare /bundle directory\n"
    "# path, not a runnable command. The child is hardcoded instead: the\n"
    "# base image's own entrypoint script (daedalus docker/Dockerfile's\n"
    "# ENTRYPOINT, /usr/local/bin/entrypoint.sh) invoked with its own\n"
    "# default bundle argument -- exactly the command the base image's\n"
    "# ENTRYPOINT+CMD would have run had this image never overridden\n"
    "# either.\n"
    "set -e\n"
    'exec litestream replicate -config /etc/litestream.yml '
    '-exec "/usr/local/bin/entrypoint.sh /bundle"\n'
)


def _litestream_config(spec: BundleSpec) -> str:
    """The litestream config for `spec`'s own agent: one sqlite db
    (techne.db, at the PVC mount path every agent's runtime already has),
    replicated to `spec.memory_replica_bucket` under a PER-AGENT prefix
    (`spec.name`) -- never the bucket root, and never another agent's
    prefix, so the write access `kobo_api.steps.AccessStep` grants this
    agent's own service account (scoped to that same prefix) can never
    reach a sibling agent's replica.
    """
    return (
        "dbs:\n"
        f"  - path: {_TECHNE_DB_PATH}\n"
        "    replicas:\n"
        "      - type: gcs\n"
        f"        bucket: {spec.memory_replica_bucket}\n"
        f"        path: {spec.name}/techne\n"
    )


def _dockerfile(spec: BundleSpec) -> str:
    if not spec.memory_replica_bucket:
        return (
            "# Agent image: the shared runtime plus this agent's bundle.\n"
            f"FROM {spec.runtime_image}\n"
            "COPY bundle/ /bundle\n"
        )
    return (
        "# Agent image: the shared runtime plus this agent's bundle.\n"
        f"FROM {spec.runtime_image}\n"
        "COPY bundle/ /bundle\n"
        f"{_MEMORY_REPLICA_DOCKERFILE_BLOCK}"
    )


# Matches a Dockerfile's `FROM <image>` line (this module's own template,
# `_dockerfile` above, writes exactly one -- see the memory-replication
# constants above for why that invariant is deliberately preserved even with
# replication on) anywhere in the file, not just its first line -- a leading
# comment line (as that template has) is not itself a FROM line. Captures
# the `FROM` keyword plus the whitespace that follows it, so the replacement
# can preserve that prefix byte-for-byte and only swap the image-ref token
# itself.
_DOCKERFILE_FROM_RE = re.compile(r"^(FROM[ \t]+)\S+", re.MULTILINE)


def update_dockerfile_runtime_image(dockerfile_path: Path, runtime_image: str) -> str | None:
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

    Returns the image ref the FROM line named before, when this changed it,
    and `None` when it already named `runtime_image`. A change means the
    repo had drifted from the agent's recorded image, most often a hand edit
    to `FROM` (issue #140); callers that commit pass it to
    `repin_commit_note` so the re-pin is never silent.
    """
    try:
        text = dockerfile_path.read_text()
    except OSError as exc:
        raise ClientError(f"Dockerfile not found: {dockerfile_path}") from exc

    match = _DOCKERFILE_FROM_RE.search(text)
    if match is None:
        raise ClientError(
            f"Dockerfile has no recognizable FROM line to re-pin: {dockerfile_path}"
        )
    previous = match.group(0)[len(match.group(1)) :]
    updated = text[: match.start()] + match.group(1) + runtime_image + text[match.end() :]
    dockerfile_path.write_text(updated)
    if previous == runtime_image:
        return None
    # The card names the runtime (issue #147), so it follows the FROM line
    # in the same commit.
    if (dockerfile_path.parent / "bundle" / "agent-card.json").is_file():
        refresh_agent_card(dockerfile_path.parent / "bundle")
    return previous


def repin_commit_note(repo: str, previous: str | None, runtime_image: str) -> str:
    """The commit-message paragraph for a write path that re-pinned `FROM`
    on its own commit, or "" when it didn't (issue #140).

    The agents row is the authority for an agent's image and the Dockerfile
    is its projection (`kobo_api.runtime`), so an unrelated room commit puts
    a drifted `FROM` back. That stays, because the re-pin is what keeps an
    old base from crashlooping a new transport, but it is said: once in the
    commit that does it and once in the log, both naming the two images.
    """
    if previous is None:
        return ""
    log.warning(
        "runtime image re-pinned on an unrelated commit: repo=%s from=%s to=%s "
        "(the repo's FROM differed from the agent's recorded image)",
        repo,
        previous,
        runtime_image,
    )
    return (
        f"\n\nAlso re-pins the Dockerfile FROM from {previous} to {runtime_image}, "
        "the runtime image the room has recorded for this agent. A FROM edited "
        "by hand is put back by the room's next commit; change an agent's image "
        "with kobo_set_runtime_image instead."
    )


def update_agent_card_metadata(
    bundle_dir: Path, *, team: str | None, purpose_tags: list[str], capability: str
) -> None:
    """In-place edit of `bundle_dir/agent-card.json`'s `metadata` block
    (persona-metadata plan, Task 3): the card delivery lifecycle is bake at
    compose time (`_agent_card` above, `capability` starting `""`), then
    update after every earn/PATCH/Regenerate — this function is that update.

    Replaces exactly `metadata.team`/`metadata.purpose_tags`/
    `metadata.capability`, then runs `refresh_agent_card`, so the fields it
    derives from the repo (skills, description, version, runtime image) are
    current too. Every other field, top-level or inside `metadata`, survives
    byte-faithfully.
    """
    card_path = bundle_dir / "agent-card.json"
    card = json.loads(card_path.read_text())
    metadata = dict(card.get("metadata") or {})
    metadata["team"] = team
    metadata["purpose_tags"] = list(purpose_tags)
    metadata["capability"] = capability
    card["metadata"] = metadata
    card_path.write_text(json.dumps(card, indent=2) + "\n")
    refresh_agent_card(bundle_dir)


def _bundle_skill_ids(bundle_dir: Path) -> list[str]:
    """Every skill the bundle loader will register: a directory under
    `bundle/skills/` holding a SKILL.md (same rule as `kobo_api.skills.
    list_repo_skills`)."""
    skills_dir = bundle_dir / "skills"
    if not skills_dir.is_dir():
        return []
    return [
        entry.name
        for entry in skills_dir.iterdir()
        if entry.is_dir() and (entry / "SKILL.md").is_file()
    ]


def _dockerfile_image(dockerfile_path: Path) -> str | None:
    try:
        match = _DOCKERFILE_FROM_RE.search(dockerfile_path.read_text())
    except OSError:
        return None
    return match.group(0)[len(match.group(1)) :] if match else None


def refresh_agent_card(bundle_dir: Path) -> bool:
    """Re-derive the card fields that describe the agent from the repo the
    card sits in (issue #147), so the card cannot drift from what the agent
    has:

    - `skills`: the skill directories under `bundle/skills/`.
    - `description`: `metadata.capability` when set, else the first sentence
      of `bundle/persona.md`. A card with neither keeps its description.
    - `version` and `metadata.runtime_image`: from the Dockerfile's FROM line
      (`runtime_version`). A repo without a readable FROM keeps both.
    - `capabilities`: an empty list (the pre-#147 bake) becomes `{}`, the
      object shape A2A defines.

    Every other field survives byte-faithfully. Returns whether the card
    changed. Callers run it in the same clone as the change that moves one
    of these inputs, so the card lands in the same commit.
    """
    card_path = bundle_dir / "agent-card.json"
    try:
        before = card_path.read_text()
    except OSError:
        log.warning("agent card missing, not refreshed: %s", card_path)
        return False
    card = json.loads(before)
    card["skills"] = _card_skills(_bundle_skill_ids(bundle_dir))
    if card.get("capabilities") == []:
        card["capabilities"] = {}
    metadata = dict(card.get("metadata") or {})
    capability = metadata.get("capability") or ""
    if capability:
        card["description"] = capability
    else:
        try:
            persona = (bundle_dir / "persona.md").read_text()
        except OSError:
            persona = ""
        if persona.strip():
            card["description"] = fallback_capability(persona)
    image = _dockerfile_image(bundle_dir.parent / "Dockerfile")
    if image:
        card["version"] = runtime_version(image)
        metadata["runtime_image"] = image
    if metadata:
        card["metadata"] = metadata
    after = json.dumps(card, indent=2) + "\n"
    if after == before:
        return False
    card_path.write_text(after)
    log.debug("agent card refreshed: %s", card_path)
    return True


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

# The whole tools SECTION: the `tools:` line plus any following
# more-indented lines (the block-style form hand-edited bundles use). For a
# flow-style line the second alternative matches nothing and the line alone
# is the section.
_TOOLS_SECTION_RE = re.compile(r"^tools:[^\n]*(?:\n[ \t]+[^\n]*)*", re.MULTILINE)


def update_agent_yaml_tools(
    bundle_dir: Path,
    *,
    url: str,
    token_env: str,
    self_name: str,
    colleague_mode: str = COLLEAGUE_MODE_DEFAULT,
) -> None:
    """Re-point an already-composed `bundle_dir/agent.yaml` at a (new) set
    of switchboard coordinates -- PRESERVING everything in the `tools`
    block the composer did not author.

    The original version replaced the whole `tools:` line with a
    colleagues-only block. By the time real agents existed, their tools
    blocks carried more: the github sidecar the composer itself adds,
    hand-added lanes (cloudtrail, slack_read, jira, liminal), and the
    allow entries that gate them -- a rewire silently dropped every one
    of them (and refused outright on a block-style file). This version
    merges instead of replacing:

    - `provider` and `colleagues` are set to the given coordinates.
    - `allow` becomes the colleague allows UNIONED with every existing
      entry, original order preserved -- a rewire never revokes, with one
      exception: a colleague tool the agent's `colleague_mode` (issue #131)
      leaves out is dropped, so a rewire cannot hand a roster-only agent
      its ask tool back.
    - Every other key (`github`, `cloudtrail`, `slack_read`, `jira`,
      `liminal`, `builtin`, `max_steps`, anything future) rides through
      untouched.

    The tools SECTION (flow-style line or block-style stanza) is spliced
    back as one flow-style line, so repeat calls are idempotent and the
    rest of the file stays byte-identical. Raises `ClientError` when the
    file is missing, has no `tools` section, or does not parse.
    """
    agent_yaml_path = bundle_dir / "agent.yaml"
    try:
        text = agent_yaml_path.read_text()
    except OSError as exc:
        raise ClientError(f"agent.yaml not found: {agent_yaml_path}") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ClientError(f"agent.yaml does not parse: {agent_yaml_path}") from exc
    if not isinstance(data, dict) or "tools" not in data:
        raise ClientError(f"agent.yaml has no tools section to update: {agent_yaml_path}")
    existing = data["tools"] if isinstance(data["tools"], dict) else {}

    merged: dict = {"provider": "colleagues"}
    merged["colleagues"] = {"url": url, "token_env": token_env, "self": self_name}
    for key, value in existing.items():
        if key in ("provider", "colleagues", "allow"):
            continue
        merged[key] = value
    allow = colleague_tools_allow(colleague_mode)
    for entry in existing.get("allow") or []:
        if entry not in allow and entry not in _COLLEAGUES_TOOLS_ALLOW:
            allow.append(entry)
    merged["allow"] = allow
    _splice_tools_section(agent_yaml_path, text, merged)


def _splice_tools_section(agent_yaml_path: Path, text: str, tools: dict) -> None:
    """Write `tools` back over the file's tools section as one flow-style
    line, leaving every other line byte-identical."""
    rendered = yaml.dump(
        tools, default_flow_style=True, sort_keys=False, width=10**9
    ).strip()
    replacement = f"tools: {rendered}"

    updated, count = _TOOLS_SECTION_RE.subn(lambda match: replacement, text, count=1)
    if count == 0:
        raise ClientError(f"agent.yaml has no tools section to update: {agent_yaml_path}")
    agent_yaml_path.write_text(updated)


def update_agent_yaml_colleague_mode(bundle_dir: Path, *, mode: str) -> bool:
    """Make an already-composed `bundle_dir/agent.yaml`'s allow list carry
    exactly the colleague tools `mode` grants (issue #131), first and in
    composer order, followed by every other entry as it was. Nothing else
    in the tools block moves: the provider, the colleague coordinates and
    every sidecar ride through.

    Returns whether the file changed. An allow list that already matches
    is left untouched byte for byte, so a default-mode agent's composed
    file is never rewritten. A bundle with no colleagues provider (the
    switchboard was off when it was composed) has no colleague tools to
    add or remove and is also left alone. Raises `ClientError` when the
    file is missing, does not parse, or has no tools section.
    """
    agent_yaml_path = bundle_dir / "agent.yaml"
    try:
        text = agent_yaml_path.read_text()
    except OSError as exc:
        raise ClientError(f"agent.yaml not found: {agent_yaml_path}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ClientError(f"agent.yaml does not parse: {agent_yaml_path}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tools"), dict):
        raise ClientError(f"agent.yaml has no tools section to update: {agent_yaml_path}")
    tools = data["tools"]
    if tools.get("provider") != "colleagues":
        return False
    existing = list(tools.get("allow") or [])
    allow = colleague_tools_allow(mode) + [
        entry for entry in existing if entry not in _COLLEAGUES_TOOLS_ALLOW
    ]
    if allow == existing:
        return False
    tools["allow"] = allow
    _splice_tools_section(agent_yaml_path, text, tools)
    return True


def update_agent_yaml_allow(bundle_dir: Path, *, entries: list[str]) -> bool:
    """Make an already-composed `bundle_dir/agent.yaml`'s allow list admit
    every pattern in `entries`, appending the ones no existing pattern
    already covers (daedalus matches `tools.allow` with fnmatch, so
    `datadog-mcp.*` is one entry for a whole target). Returns whether the
    file changed; nothing else in the tools block moves. Raises
    `ClientError` when the file is missing, does not parse, or has no tools
    section.

    Why: the gateway filters its catalog per grant, and the runtime offers a
    gateway tool only when the bundle's allow list matches it too, so a
    permission approved in the room has to reach the bundle as well
    (2026-10-31: an agent held a Datadog lane, listed the tools at the
    gateway, and offered none of them to its model).
    """
    agent_yaml_path = bundle_dir / "agent.yaml"
    try:
        text = agent_yaml_path.read_text()
    except OSError as exc:
        raise ClientError(f"agent.yaml not found: {agent_yaml_path}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ClientError(f"agent.yaml does not parse: {agent_yaml_path}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tools"), dict):
        raise ClientError(f"agent.yaml has no tools section to update: {agent_yaml_path}")
    tools = data["tools"]
    allow = list(tools.get("allow") or [])
    missing = [e for e in entries if not any(fnmatch(e, str(pattern)) or e == pattern for pattern in allow)]
    if not missing:
        return False
    tools["allow"] = [*allow, *missing]
    _splice_tools_section(agent_yaml_path, text, tools)
    return True


def update_agent_yaml_skills_load(bundle_dir: Path) -> bool:
    """Make an already-composed `bundle_dir/agent.yaml`'s allow list admit
    `skills.load`, appending it when no entry matches (issue #137).

    For a skill added after birth (`kobo_api.skills`): compose() adds the
    tool only for skills the agent was born with, so without this an agent
    that gets its first skill later boots on daedalus 0.5.0 with the
    "tools.allow does not admit 'skills.load'" warning and no skills index.
    Entries are matched as daedalus matches them (fnmatch), so an allow list
    that already admits it through a pattern is left alone.

    Returns whether the file changed; an allow list that already admits the
    tool is left untouched byte for byte. Raises `ClientError` when the file
    is missing, does not parse, or has no tools section.
    """
    agent_yaml_path = bundle_dir / "agent.yaml"
    try:
        text = agent_yaml_path.read_text()
    except OSError as exc:
        raise ClientError(f"agent.yaml not found: {agent_yaml_path}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ClientError(f"agent.yaml does not parse: {agent_yaml_path}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tools"), dict):
        raise ClientError(f"agent.yaml has no tools section to update: {agent_yaml_path}")
    tools = data["tools"]
    allow = list(tools.get("allow") or [])
    if any(fnmatch(_SKILLS_LOAD_TOOL, str(pattern)) for pattern in allow):
        return False
    tools["allow"] = [*allow, _SKILLS_LOAD_TOOL]
    _splice_tools_section(agent_yaml_path, text, tools)
    return True


# Matches the `approvals:` stanza's `gates:` line anywhere in an already-
# composed `agent.yaml` — this module's own `_render_agent_yaml` always
# renders it on a single line (same reasoning as `_TOOLS_LINE_RE` above: the
# nested `{match: ..., require: ...}` mapping is non-scalar, so it forces
# flow style there too), so a single-line, regex-based surgical edit finds
# and replaces the whole line without a full YAML round-trip that would
# reorder/reformat every other line in the file. `gates:` is nested (under
# `approvals:`, unlike `tools:`'s top-level key), so the pattern captures
# its leading indentation (group 1) to reproduce it verbatim in the
# replacement, rather than assuming a fixed column like `_TOOLS_LINE_RE`
# does for its own top-level key.
_APPROVALS_GATES_LINE_RE = re.compile(r"^([ \t]*)gates:[ \t]*\[.*\][ \t]*$", re.MULTILINE)


def _render_gates_flow(gates: list[dict]) -> str:
    """Render `gates` (a list of `{match, require}`-shaped mappings, or
    empty) as the single-line flow-style YAML text `update_agent_yaml_
    approvals_gates` writes onto the `gates:` line.

    Every string value is forced double-quoted via `_DoubleQuoted` (matching
    this module's own convention elsewhere — `_a2a_transport`'s `auth`
    block, `_tools_block`'s `url`/`self`, the compose-time default gate's
    own quoted `"*"` match pattern) so the rendered text is always valid
    YAML regardless of the string's content (an unquoted bare `*`, as in the
    default gate's match pattern, is itself a YAML alias marker and would
    fail to parse). Non-string scalars (for instance a boolean) are left
    bare. An empty list renders `[]`, identically to `_approvals_gates`'s
    own off-path shape and `_tools_block`'s empty `allow: []`.
    """
    flow_items = _FlowList(
        _FlowDict(
            {
                key: _DoubleQuoted(value) if isinstance(value, str) else value
                for key, value in gate.items()
            }
        )
        for gate in gates
    )
    rendered = yaml.dump(
        flow_items, Dumper=_AgentYamlDumper, default_flow_style=None, width=1000
    )
    return rendered.strip()


def update_agent_yaml_approvals_gates(bundle_dir: Path, *, gates: list[dict]) -> None:
    """In-place edit of an already-composed `bundle_dir/agent.yaml`'s
    `approvals.gates` line: replace it wholesale with `gates`, leaving
    `ceiling`, `escalation`, and every other line in the file byte-identical.

    The backfill path for the bundle's blanket `match: "*"` approval gate,
    per the "Amendments from live acceptance" section of `docs/superpowers/
    specs/2026-08-19-colleagues-talk-design.md`: a deployed pod's approver is
    deny-all, so that gate blocks every tool outright, including the two
    colleague tools the tool allowlist alone is meant to gate. Called with
    `gates=[]` for that repair — the write ceiling and escalation route are
    left untouched by this function, exactly as `_approvals_gates` leaves
    them untouched at compose time.

    Same file-editing idiom as `update_agent_yaml_auth`/`update_agent_yaml_
    tools` above: read the whole file as text, apply one targeted `re.subn`
    against a replacement function, and raise rather than silently no-op
    when the thing being edited isn't found. Add-or-replace in one code path
    (mirrors `update_agent_yaml_tools`): the whole `gates:` line is always
    replaced wholesale, so there is no separate "add" branch.

    Idempotent by construction: a second call with the same `gates` value
    replaces the line with byte-identical text, so the file's contents never
    drift on a repeat call.

    Raises `ClientError` (never silently no-ops), mirroring
    `update_agent_yaml_tools`'s own two failure modes:

    - `agent.yaml` is missing entirely: `Path.read_text()` raises `OSError`.
    - No line in the file matches `_APPROVALS_GATES_LINE_RE` (no `approvals`
      stanza, or a hand-edited one with no `gates:` line to replace).
    """
    agent_yaml_path = bundle_dir / "agent.yaml"
    try:
        text = agent_yaml_path.read_text()
    except OSError as exc:
        raise ClientError(f"agent.yaml not found: {agent_yaml_path}") from exc

    rendered = _render_gates_flow(gates)

    def _replace(match: re.Match) -> str:
        return f"{match.group(1)}gates: {rendered}"

    updated, count = _APPROVALS_GATES_LINE_RE.subn(_replace, text, count=1)
    if count == 0:
        raise ClientError(
            f"agent.yaml has no approvals gates line to update: {agent_yaml_path}"
        )
    agent_yaml_path.write_text(updated)


# Matches the whole `memory:` top-level line anywhere in an already-composed
# `agent.yaml` -- this module's own `_render_agent_yaml` (via the same
# `width=1000` that keeps every other flow-style entry on one line) always
# renders `memory:` on a single line too, and every value on it (`personal`,
# `home`, `revision`) is a plain scalar, never anything with a nested
# `{...}` the way the a2a transport's own `auth` sub-block is -- so unlike
# `_A2A_TRANSPORT_LINE_RE` above, this pattern doesn't need the "at most one
# balanced brace group" allowance, a plain `[^{}]*` for the inner content
# suffices. `memory:` is a top-level key (column 0, same as `tools:`),
# unlike the a2a transport entry (a list item under `transports:`) or the
# nested `approvals.gates:` line, so no leading-indentation group is
# captured here either (mirroring `_TOOLS_LINE_RE`).
_MEMORY_LINE_RE = re.compile(
    r"^(?P<prefix>memory:[ \t]*\{)(?P<inner>[^{}]*)\}[ \t]*$",
    re.MULTILINE,
)

# Strips an already-present `, revision: <value>` suffix out of a matched
# line's `inner` group before a fresh one is appended -- the reason a second
# call with the same `revision` reproduces the exact same bytes
# (idempotence), and the reason a call with a DIFFERENT `revision` replaces
# rather than appends a second key. Mirrors `_EXISTING_AUTH_BLOCK_RE`'s own
# role for `update_agent_yaml_auth`'s `auth` block.
_EXISTING_MEMORY_REVISION_RE = re.compile(r",[ \t]*revision:[ \t]*[^,}]+")


def update_agent_yaml_memory_revision(bundle_dir: Path, *, revision: str) -> None:
    """In-place edit of an already-composed `bundle_dir/agent.yaml`'s
    `memory:` line: add (or replace) its `revision` key, leaving every other
    line in the file -- and every other key already on the `memory:` line
    itself (`personal`, `home`, or anything else a future caller adds) --
    byte-identical.

    The backfill path for agents composed before the belief-revision feature
    existed (belief-revision plan, Task 2) -- and, more generally, for any
    already-composed bundle a caller wants to point at a (new) revision mode
    without recomposing the whole bundle (which would also require
    re-downloading every staged skill).

    Same file-editing idiom as `update_agent_yaml_auth` above: read the
    whole file as text, apply one targeted `re.subn` against a replacement
    function, and raise rather than silently no-op when the thing being
    edited isn't found. Add-or-replace via the same "strip the old value,
    then append the new one" trick `update_agent_yaml_auth` uses for its
    `auth` block, rather than the wholesale-line-replace `update_agent_yaml_
    tools`/`update_agent_yaml_approvals_gates` use above -- unlike `tools:`/
    `gates:`, the `memory:` line already carries other keys (`personal`,
    `home`) this function does not own and must preserve.

    Idempotent by construction: a second call with the same `revision` value
    first strips the `revision` key the previous call wrote (via
    `_EXISTING_MEMORY_REVISION_RE`) and then re-adds the identical key, so
    the file's bytes never drift on a repeat call. A call with a DIFFERENT
    `revision` replaces the key rather than appending a second one, for the
    same reason.

    Raises `ClientError` (never silently no-ops), mirroring
    `update_agent_yaml_auth`'s own two failure modes:

    - `agent.yaml` is missing entirely: `Path.read_text()` raises `OSError`.
    - No line in the file matches `_MEMORY_LINE_RE` (no `memory:` stanza to
      update): `re.subn`'s match count is 0.
    """
    agent_yaml_path = bundle_dir / "agent.yaml"
    try:
        text = agent_yaml_path.read_text()
    except OSError as exc:
        raise ClientError(f"agent.yaml not found: {agent_yaml_path}") from exc

    def _replace(match: re.Match) -> str:
        prefix = match.group("prefix")
        inner = _EXISTING_MEMORY_REVISION_RE.sub("", match.group("inner"))
        return f"{prefix}{inner}, revision: {revision}}}"

    updated, count = _MEMORY_LINE_RE.subn(_replace, text, count=1)
    if count == 0:
        raise ClientError(f"agent.yaml has no memory stanza to update: {agent_yaml_path}")
    agent_yaml_path.write_text(updated)


# Matches the `model:` stanza's `max_tokens:` line anywhere in an already-
# composed `agent.yaml` -- this module's own `_render_agent_yaml` always
# renders `model:` as a block-style mapping (`id`, `max_tokens`, and `budget`
# each get their own line), so `max_tokens:` is on its own line nested two
# spaces under `model:`, the same as `_APPROVALS_GATES_LINE_RE`'s own
# `gates:` line is nested under `approvals:` -- a single-line, regex-based
# surgical edit finds and replaces the whole line without a full YAML
# round-trip that would reorder/reformat every other line in the file.
# `max_tokens:` is nested (unlike `tools:`'s top-level key), so the pattern
# captures its leading indentation (group 1) to reproduce it verbatim in the
# replacement, rather than assuming a fixed column the way `_TOOLS_LINE_RE`
# does for its own top-level key.
_MAX_TOKENS_LINE_RE = re.compile(r"^([ \t]*)max_tokens:[ \t]*\d+[ \t]*$", re.MULTILINE)


def update_agent_yaml_max_tokens(bundle_dir: Path, *, max_tokens: int) -> None:
    """In-place edit of an already-composed `bundle_dir/agent.yaml`'s
    `model.max_tokens` line: replace it wholesale with `max_tokens`, leaving
    `id`, `budget`, and every other line in the file byte-identical.

    The backfill path for agents composed before this fix (the truncated-
    answers incident: this room's own composed default of 1024 sat well
    under the runtime's own 8192 default, cutting long or thinking-heavy
    answers off mid-sentence) -- and, more generally, for any already-
    composed bundle a caller wants to point at a different token ceiling
    without recomposing the whole bundle (which would also require
    re-downloading every staged skill).

    Same file-editing idiom as `update_agent_yaml_approvals_gates` above:
    read the whole file as text, apply one targeted `re.subn` against a
    replacement function, and raise rather than silently no-op when the
    thing being edited isn't found. Wholesale line replace, not
    strip-then-append (mirrors `update_agent_yaml_approvals_gates`/
    `update_agent_yaml_tools`, not `update_agent_yaml_auth`/`update_agent_
    yaml_memory_revision`): unlike those two, `max_tokens:` already sits
    alone on its own line with no sibling keys sharing it, so there is
    nothing to strip out before the new value is written.

    Idempotent by construction: a second call with the same `max_tokens`
    value replaces the line with byte-identical text, so the file's contents
    never drift on a repeat call.

    Raises `ClientError` (never silently no-ops), mirroring
    `update_agent_yaml_approvals_gates`'s own two failure modes:

    - `agent.yaml` is missing entirely: `Path.read_text()` raises `OSError`.
    - No line in the file matches `_MAX_TOKENS_LINE_RE` (no `max_tokens:`
      line to replace, e.g. a hand-edited `model:` stanza).

    Ledgered follow-up: this whole `update_agent_yaml_*` family locates its
    target with a hand-rolled single-line regex rather than a real YAML
    round-trip -- hardening that shared idiom (or replacing it with
    something less brittle) is tracked separately and deliberately out of
    scope here.
    """
    agent_yaml_path = bundle_dir / "agent.yaml"
    try:
        text = agent_yaml_path.read_text()
    except OSError as exc:
        raise ClientError(f"agent.yaml not found: {agent_yaml_path}") from exc

    def _replace(match: re.Match) -> str:
        return f"{match.group(1)}max_tokens: {max_tokens}"

    updated, count = _MAX_TOKENS_LINE_RE.subn(_replace, text, count=1)
    if count == 0:
        raise ClientError(f"agent.yaml has no max_tokens line to update: {agent_yaml_path}")
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
        dest/bundle/knowledge/self.md
        dest/bundle/knowledge/colleagues.md (not when colleague_mode is none)
        dest/bundle/skills/<id>/**   (one materialized tree per spec.skills
                                      entry — see `_materialize_skill`)
        dest/litestream.yml          (only when spec.memory_replica_bucket)
        dest/entrypoint.sh           (only when spec.memory_replica_bucket)
    """
    bundle_dir = dest / "bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    (bundle_dir / "agent.yaml").write_text(_render_agent_yaml(spec))
    (bundle_dir / "agent-card.json").write_text(json.dumps(_agent_card(spec), indent=2) + "\n")
    (bundle_dir / "persona.md").write_text(spec.persona_body)

    knowledge_dir = bundle_dir / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "self.md").write_text(_self_md(spec))
    if spec.colleague_mode != COLLEAGUE_MODE_NONE:
        (knowledge_dir / "colleagues.md").write_text(_colleagues_md(spec))

    if spec.skills:
        skills_dir = bundle_dir / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        for skill in spec.skills:
            _materialize_skill(skill, skills_dir / skill.id)

    (dest / "Dockerfile").write_text(_dockerfile(spec))

    if spec.memory_replica_bucket:
        (dest / "litestream.yml").write_text(_litestream_config(spec))
        (dest / "entrypoint.sh").write_text(_ENTRYPOINT_SH)


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

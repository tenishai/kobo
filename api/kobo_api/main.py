"""FastAPI app factory for kobo-api."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Annotated, Literal

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kobo_api.agent_env import (
    MANAGED_PURPOSES,
    MAX_ENV_VALUE_LENGTH,
    MAX_KEYS,
    PULL_REQUEST,
    SECRETS_UNWIRE_PR_BODY,
    SECRETS_UNWIRE_PR_TITLE,
    SECRETS_WIRING_BRANCH,
    SECRETS_WIRING_PR_BODY,
    SECRETS_WIRING_PR_TITLE,
    UNCHANGED,
    WIRING_PR_ALREADY_OPEN,
    DeployTriggerFailed,
    RepoBusy,
    RepoEdit,
    apply_env,
    edit_gojira_yaml,
    env_secret_name,
    has_env_secret,
    managed_secret_name,
    managed_secret_path,
    merged_names,
    parse_gojira_yaml,
    read_default_gojira,
    read_repo_env,
    require_our_branch,
    secret_path,
    sync_state,
    sync_unknown,
    unwire_env_secret,
    wire_env_secret,
    write_refusal,
)
from kobo_api.archetypes import (
    ARCHETYPE_TEAM_GUIDE,
    TEAM_GUIDE_TAG,
    render_team_guide,
    team_guide_persona_id,
)
from kobo_api.avatars import avatar_prompt, set_avatar
from kobo_api.catalog import RESERVED_NAMES, validate_name
from kobo_api.clients import (
    CabalError,
    ClientError,
    StorageNotFound,
    _build_system_task_envelope,
    _build_task_envelope,
    _build_teach_envelope,
    _extract_relay_reply,
    _join_text_parts,
    cabal_enabled,
    filter_cabal_event_stream,
    probe_slack_token_store,
)
from kobo_api.config import SHARED_GROUP, Config
from kobo_api.connect_scan import NAME_TAKEN, Finding, ScanReport, scan_repo
from kobo_api.db import init_db, make_engine, make_session_factory
from kobo_api.metrics import install as install_metrics
from kobo_api.tracing import install as install_tracing
from kobo_api.destroy import DESTROY_STEP_LABELS, run_destroy
from kobo_api.directory import retire_directory, sync_directory
from kobo_api.iap import HttpsOnlyMiddleware, IapMiddleware, IapVerifier
from kobo_api.intake import (
    INTAKE_MAX_BYTES,
    IntakeError,
    delete_intake_file,
    list_intake,
    upload_intake_file,
)
from kobo_api.kinds import (
    KIND_CONNECTED,
    RUN_MODE_HEADLESS,
    RUN_MODE_HTTP,
    is_connected,
)
from kobo_api.knowledge import (
    KnowledgeError,
    delete_knowledge_file,
    delivery_state,
    read_knowledge,
    rollout_in_progress,
    save_knowledge_file,
)
from kobo_api.ladon import acting_email_refusal
from kobo_api.learnings import clean_edited_statement
from kobo_api.learnings import nightly_loop as learnings_nightly_loop
from kobo_api.marketplace import BASELINE_TOOL_PREFIX
from kobo_api.mcp import rate_limit
from kobo_api.mcp.mount import mcp_lifespan, mount_mcp
from kobo_api.metadata import (
    clean_capability_text,
    commit_card_metadata,
    generate_capability,
    skill_descriptions_for_agent,
    tooling_filenames_for,
)
from kobo_api.repo_search import MAX_QUERY_CHARS, RepoIndex, RepoWarmer
from kobo_api.runtime import commit_runtime_image, validate_runtime_image
from kobo_api.models import (
    GROUP_REQUEST_DECLINED,
    LEARNING_APPROVED,
    LEARNING_DENIED,
    LEARNING_PENDING,
    LEARNING_STATUSES,
    REPO_ACCESS_APPROVED,
    REPO_ACCESS_REQUESTED,
    REPO_ACCESS_REVOKED,
    REPO_ACCESS_STATUSES,
    RepoAccess,
    GROUP_REQUEST_DONE,
    GROUP_REQUEST_PENDING,
    INVITE_ACCEPTED,
    INVITE_DECLINED,
    INVITE_PENDING,
    INVITE_REVOKED,
    A2AInvite,
    Agent,
    AgentLearning,
    AgentStatus,
    AgentUserGrant,
    GroupRequest,
    Thread,
    ThreadParticipant,
)
from kobo_api.namegen import SUGGESTION_COUNT, finalize_suggestions
from kobo_api.org import OrgState, load_org_config
from kobo_api.pairs import (
    implicitly_paired,
    sync_dispatch_pairs,
    sync_dispatch_pairs_best_effort,
)
from kobo_api.persona_draft import finalize_distill, finalize_draft
from kobo_api.personas import (
    PERSONA_ID_RE,
    PersonaError,
    PersonaExists,
    PersonaFields,
    create_persona,
    set_persona_archived,
    upsert_persona,
)
from kobo_api.personas import (
    read_index as personas_read_index,
)
from kobo_api.provisioner import (
    STEP_LABELS,
    interrupted_sweep_loop,
    naive_utc,
    pipeline_for,
    reset_failed_steps,
    run_pipeline,
    sweep_interrupted,
)
from kobo_api.jira_broker import fetch_issue as jira_fetch_issue
from kobo_api.jira_broker import search_issues as jira_search_issues
from kobo_api.pr_broker import BrokerAuthError, mint_scoped_token, verify_agent_token
from kobo_api.purpose_tags import (
    MAX_CREATE_TAGS,
    clean_tags,
    decode_tags,
    encode_tags,
    normalize_tags,
)
from kobo_api.search import (
    CATALOG_TTL_S,
    catalog_index_metadata,
    catalog_is_empty,
    distinct_skill_teams,
    get_entry,
    ingest_indexes,
)
from kobo_api.search import search as catalog_search
from kobo_api.search import teams as catalog_teams_query
from kobo_api.skills import (
    SkillsError,
    add_catalog_skill,
    bust_repo_skills_cache,
    list_repo_skills,
    read_local_skill,
    remove_skill,
    save_local_skill,
)
from kobo_api.slack_setup import render_manifest, rewire_slack_channels, wire_slack
from kobo_api.slack_state import (
    SLACK_APP_ID_RE,
    clean_channels,
    decode_channels,
    derive_state,
    encode_channels,
)
from kobo_api.static_ui import mount_ui
from kobo_api.steps import (
    _resolve_persona_body,
    refresh_after_capability_distill,
    refresh_colleague_rosters,
)
from kobo_api.teach import format_note, note_filename
from kobo_api.tooling import (
    ToolingError,
    _validate_content,
    _validate_filename,
    delete_tooling_file,
    existing_tool_filenames,
    import_tooling,
    list_tooling,
    upload_tooling_file,
)

log = logging.getLogger(__name__)

# Type-only: keeps the real (httpx-backed) `Clients` import out of this
# module's own import graph, so tests that only need `create_app` (most of
# them) don't pay for it. `build_default_app` below does the real import,
# right where `Clients.real(...)` is actually constructed.
if TYPE_CHECKING:
    from kobo_api.clients import Clients


# The single source for a team slug's shape: lowercase, starts with a
# letter, up to 24 chars total of letters/digits/hyphens after that — same
# shape a persona/skill tag has always required. `CreatePersonaRequest.team`
# below imports this into its own `Field(pattern=...)`; `CreateAgentRequest.
# team` (a plain `str`, deliberately not a Field pattern — see its own
# docstring) has `create_agent` match against this directly instead, so its
# route can answer a shape mismatch with plain rejection copy rather than
# the generic `RequestValidationError` message a Field pattern would trigger.
TEAM_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")


class SkillPick(BaseModel):
    """One entry of `CreateAgentRequest.skills`: a skill id plus the catalog
    team it belongs to — a skill id is only unique within its own team
    (`kobo_api.search.get_entry`'s (kind, id, team) key), so the team must be
    named alongside the id rather than assumed from the agent's own creating
    team."""

    id: str
    team: str


class CreateAgentRequest(BaseModel):
    # Unknown keys are IGNORED, not refused. Pydantic's own default, stated
    # here because it is a compatibility decision rather than an accident:
    # a browser tab or MCP client loaded before a field was retired keeps
    # sending it, and a create that 422'd (or 500'd) on a key the room no
    # longer reads would break callers that are asking for exactly what
    # they now get anyway. `runtime_image` is the field that made this
    # concrete -- every agent builds on `Config.runtime_image`, and
    # `PATCH /api/agents/{name}/runtime` is where a per-agent pin moves.
    model_config = ConfigDict(extra="ignore")

    name: str
    persona: str
    # The creating team (Task 5, bucket catalogs) — same slug shape as
    # `CreatePersonaRequest.team` below. Optional, defaulting to "shared"
    # (Persona v3 / teamless create addendum): the create modal drops the
    # Team station entirely, so a client that omits this key entirely must
    # still record something — "shared" is what nearly everything in both
    # catalogs already carries. An explicit team is still validated and
    # honored exactly as before; only the "must be present at all" part is
    # gone.
    #
    # Deliberately a plain `str` here, NOT `Field(pattern=...)` (contrast
    # `CreatePersonaRequest.team` below, which still is a Field pattern) —
    # a pydantic pattern mismatch never reaches route code at all (the
    # app-wide `RequestValidationError` handler answers it with the
    # generic "that request wasn't quite right" copy), and the
    # persona-metadata plan (Task 2) wants create_agent's own plain
    # rejection copy for a bad team specifically. `create_agent` validates
    # this against `TEAM_SLUG_RE` itself, same in-route idiom as
    # `SLACK_APP_ID_RE`'s own manual check elsewhere in this file.
    team: str = "shared"
    skills: list[SkillPick] = []
    model: str | None = None
    # purpose_tags (persona-metadata plan, Task 2): normalized/capped
    # server-side by `clean_tags` (kobo_api.purpose_tags) in the route,
    # never at the pydantic layer — de-dupe can bring an over-cap raw list
    # back under the cap, so the cap must be checked AFTER normalization,
    # not against the raw submitted length a pydantic max_length would see.
    purpose_tags: list[str] = []
    # group_name (access-control arc, Task A2, validated in Task B2): the
    # ACL namespace this agent belongs to -- DISTINCT from `team` above (the
    # skills-catalog capability axis), never conflated with it. `create_
    # agent` (below) requires this and checks it against `cfg.groups` once
    # the room has named any groups at all; a room that hasn't stays exactly
    # as before B2 -- optional, stored verbatim, unvalidated.
    group_name: str | None = None
    # Baseline tooling pack (MCP marketplace wave 1, Task 6): the opt-out.
    # Default False, so every new agent ships the marketplace's `baseline-*`
    # tools (staged at compose time by `kobo_api.steps._stage_baseline_tools`);
    # True means this one goes without them. Nothing validates it -- a bool is
    # a bool -- and it is recorded on the row rather than acted on here, since
    # the pack is staged by RepoStep, not by this route.
    skip_baseline: bool = False
    # Archetype create lane (team-guide spec 2026-09-14): when set, the route
    # FORCES persona/tools/tags/visibility/skills and ignores the client's
    # values for them — "cannot act" is enforced in the composed bundle, not
    # in UI defaults. Only "team_guide" exists today.
    archetype: str | None = None
    # Optional style overlay for archetype personas (style-not-identity;
    # subordinate to the constitution — see kobo_api/archetypes.py).
    voice: str | None = None


class ScanRepoRequest(BaseModel):
    """POST /api/connect/scan body (connected-agents design 2026-09-21).

    The same three fields the connect body opens with, because the scan's
    answer depends on all three: the repository it reads, the name it
    checks against the room, and how the agent says it runs (which decides
    whether an unexposed port is worth mentioning, and whether the room has
    a class for it at all).
    """

    model_config = ConfigDict(extra="ignore")

    repo: str
    name: str
    run_mode: str = RUN_MODE_HTTP
    # The same ACL namespace `CreateAgentRequest` carries, gated by the
    # same helper on both routes. The scan carries it too, and not as a
    # formality: it reads private repositories with the room's own
    # credentials, so a caller who could not connect has no business
    # asking it what is in one.
    group_name: str | None = None


class ConnectAgentRequest(ScanRepoRequest):
    """POST /api/agents/connect body.

    `secret_names` are names only. The room never holds a connected agent's
    secrets in this version: it passes the names to the deploy platform so
    the pod mounts them, and the pod does not start until the owner creates
    them, which the scan report says plainly.

    `confirm_repo_owner` is the caller's word that they may connect this
    repository. The room cannot check it (see the connect route's own
    docstring), so it asks, and the connect's log line records who
    answered. An operator is exempt.
    """

    secret_names: list[str] = []
    confirm_repo_owner: bool = False


class ConvertToConnectedRequest(BaseModel):
    """POST /api/agents/{name}/convert-to-connected body.

    One field, and it is required: an agent the room composed has no
    `run_mode` recorded (only a connected agent can bring something that
    serves no HTTP port), so there is no old value to keep and no default
    the room could pick on the operator's behalf without guessing.
    """

    model_config = ConfigDict(extra="ignore")

    run_mode: str


class NameSuggestionsRequest(BaseModel):
    """POST /api/agents/name-suggestions body (create flow v2 addendum): same
    field names as `PersonaFields` (kobo_api.personas); bounds deliberately
    tighter (2000 vs 49152) since suggestions need only a sketch of what_they_do
    — the naming station reads the same persona-shape text the persona builder
    itself takes, just ahead of a persona actually being committed.

    `persona_id`/`exclude`/`round` (create-identity, Task 3): `persona_id`,
    when it resolves against the persona catalog, makes the route use that
    persona's own REAL body (truncated) and purpose tags in place of
    `what_they_do` — see the route's own docstring for the exact fallback
    contract. `exclude` is names already offered this session (an earlier
    round the caller wants to move past); capped at 20, generously above the
    3 the room ever offers in one round but tolerant of a caller
    accumulating several rounds' worth. `round` is a monotonically
    increasing "offer me more" counter (0 on the first call) that salts the
    fallback pool's own ordering so a fully-excluded regenerate doesn't just
    reoffer the same padding names.
    """

    who_they_are: str = Field(min_length=1, max_length=200)
    what_they_do: str = Field(min_length=1, max_length=2000)
    persona_id: str | None = None
    exclude: list[str] = Field(default_factory=list, max_length=20)
    round: int = Field(default=0, ge=0, le=20)


class ChatMessagePayload(BaseModel):
    role: str
    text: str


class ChatRequest(BaseModel):
    messages: list[ChatMessagePayload]


# Chat message text is bounded to keep one a2a task's payload (and its cost)
# sane — matches the brief's guard exactly, not an arbitrary framework default.
CHAT_MESSAGE_TEXT_MAX_LEN = 4000

CHAT_MESSAGE_ROLES = {"user", "agent"}


class InviteCreateRequest(BaseModel):
    """POST /api/agents/{name}/invites body (access-control arc, Task C1):
    the path's `{name}` is always the FROM agent; `to_agent` is the agent
    being asked for colleague access, named the same way every other
    agent-scoped route names one -- a bare agent name, resolved server-side."""

    to_agent: str


class GrantCreateRequest(BaseModel):
    """POST /api/agents/{name}/grants body (access-control arc, Task C1):
    the email being granted visibility into `{name}` as if it owned the
    agent."""

    email: str


class ThreadCreateRequest(BaseModel):
    """POST /api/threads body (threads arc, Task 8): the single agent the
    initiator names to start the thread, a bare name resolved server-side
    the same way every other agent-scoped route names one."""

    agent: str


class ThreadParticipantRequest(BaseModel):
    """POST /api/threads/{id}/participants body (threads arc, Task 8): the
    agent being added to an already-created thread."""

    agent: str


class ThreadTurnRequest(BaseModel):
    """POST /api/threads/{id}/turns body (threads arc, Task 9): the same
    `{"role", "text"}` message shape `ChatRequest` uses for a single-agent
    chat, since a thread turn is that same mechanic run once per
    participant in sequence rather than once against a single agent."""

    messages: list[ChatMessagePayload]


class GroupRequestCreate(BaseModel):
    """POST /api/group-requests body (shared-group + group-requests wave):
    `group_name` names a directory group `cfg.groups` doesn't know about
    yet -- never validated against it, since that's the entire point of
    filing this. `note` is optional free-text context for whoever reviews
    the queue. Both are plain `str`/`str | None` rather than `Field(min_
    length=..., max_length=...)`, matching `GrantCreateRequest.email`'s own
    idiom above: `create_group_request` does its own `.strip()`-then-check
    so a whitespace-only name reads as empty, the same way `add_grant`
    treats a whitespace-only email."""

    group_name: str
    note: str | None = None


class KnowledgeSavePayload(BaseModel):
    content: str


class SkillSavePayload(BaseModel):
    """PUT .../skills/local/{skill_id} body — same one-field shape as
    KnowledgeSavePayload above; kobo_api.skills.save_local_skill does its own
    content validation (size, frontmatter), so there's nothing more to
    declare here."""

    content: str


class CatalogSkillPayload(BaseModel):
    """POST .../skills/catalog body: the catalog id plus the team it belongs
    to — same (id, team) shape as SkillPick above (a skill id is only unique
    within its own team), just with `name` rather than `id` as the field
    label since this is the skill's directory name, not a catalog row's
    primary key."""

    name: str
    team: str


class IntakeUploadPayload(BaseModel):
    """POST .../intake body: the filename alongside its content (unlike
    knowledge/skill saves, which take the filename from the URL path) —
    intake is a one-shot upload, not an edit of a named existing resource,
    so there's no `{filename}` in the route to key off of."""

    filename: str
    content: str


class RepoAccessRequest(BaseModel):
    """POST .../repo-access body: the owner's batch of repositories (agent
    PR broker spec, ruling 1 -- many at once, never one-at-a-time-only).
    Each entry must be "owner/name"."""

    repositories: list[str] = Field(min_length=1, max_length=50)

    @field_validator("repositories")
    @classmethod
    def _owner_slash_name(cls, v: list[str]) -> list[str]:
        for repo in v:
            if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
                raise ValueError(f"not an owner/name repository: {repo!r}")
        return v


class RepoAccessDecision(BaseModel):
    """POST /api/repo-access/decide body: the operator's BATCH decision."""

    ids: list[int] = Field(min_length=1, max_length=200)
    decision: Literal["approved", "revoked"]


class TeachPayload(BaseModel):
    """POST .../teach body: the operator's distilled note straight from a
    chat exchange. The route formats it (kobo_api.teach.format_note) and
    names it (kobo_api.teach.note_filename) before handing both to the
    same intake seam IntakeUploadPayload above already uses; there's no
    filename here for the caller to pick, unlike that upload."""

    text: str


class LearningsBulkPayload(BaseModel):
    """Bulk verdict for the Learnings tab's select-all gesture: one action
    applied to many pending rows in one request."""

    ids: list[int]
    action: Literal["approve", "deny"]


class LearningApprovePayload(BaseModel):
    """POST .../approve body: optional fixed wording (learnings rethink) --
    when present, THIS is what gets taught and what the row shows
    afterwards. `max_length` only stops a multi-megabyte body from being
    parsed at all; the 200-char semantic rule on the actual statement stays
    enforced by `clean_edited_statement`."""

    content: str | None = Field(default=None, max_length=2000)


class ToolingUploadPayload(BaseModel):
    """POST .../tooling body: same one-shot-upload shape as
    IntakeUploadPayload above (filename alongside its content, not a
    `{filename}` path segment) — a tooling upload is a fresh file pick from
    a form, not an edit of an already-named resource."""

    filename: str
    content: str


class ToolingImportPayload(BaseModel):
    """POST .../tooling/import body: the source repo (`owner/repo`) and the
    folder path within it to walk — see kobo_api.tooling.import_tooling for
    the validation and partial-pass semantics."""

    repo: str
    path: str


class SlackConnectRequest(BaseModel):
    """POST .../slack/connect body: the Slack app id plus tokens. Slack
    self-service (2026-08-17) retired kobo's own app-creation route
    (`POST .../slack/app`) — the app already exists by the time this call
    lands, made by a user directly at Slack's own app site from the
    manifest `GET .../slack/manifest` hands back, so its id arrives here
    instead of getting minted by kobo. `app_id` is validated against
    `SLACK_APP_ID_RE` before either token's format, same plain-copy 422
    idiom as the tokens below.

    The channel list stays off this request (Two-lane Slack, 2026-08-17)
    — the channels an agent listens on are whatever's already saved via
    `PUT .../slack/channels` (`decode_channels(agent.slack_channels)`),
    not something the connect call carries. An old-shape body that still
    includes `channel` parses fine (the extra key is ignored, pydantic's
    default) but the route never reads it."""

    app_id: str
    bot_token: str
    app_token: str


class SlackChannelsRequest(BaseModel):
    """PUT .../slack/channels body: the full replacement channel list (not
    a delta) — `clean_channels` validates/dedupes it, except an empty list,
    which the route allows through deliberately as the disconnect path."""

    channels: list[str]


class AgentEnvWrite(BaseModel):
    """PUT .../secrets and PUT .../env body: what to add or change, and
    what to take away, in one call (design 2026-09-22).

    A delta rather than a full replacement, unlike `SlackChannelsRequest`
    above, and for a reason particular to this pair of lists: the secrets
    list is never readable, so a caller cannot send back what it did not
    change. Both routes take the same shape so the panel's two halves and
    the two MCP tools behave identically.

    Bounded at the model rather than the route because a body this shape
    is the first thing a request carries: `MAX_KEYS` entries per side is
    the same cap `kobo_api.agent_env.write_refusal` enforces against what
    a write LEAVES BEHIND, and the value cap keeps a pasted file from
    becoming a request nothing downstream expected.
    """

    set: dict[str, Annotated[str, Field(max_length=MAX_ENV_VALUE_LENGTH)]] = Field(
        default_factory=dict, max_length=MAX_KEYS
    )
    remove: list[str] = Field(default_factory=list, max_length=MAX_KEYS)


class CreatePersonaRequest(PersonaFields):
    """POST /api/personas body: every PersonaFields field plus the creating
    team (`TEAM_SLUG_RE` above, same slug shape as a persona/skill tag).

    Optional, defaulting to "shared" (Persona v3 / teamless create
    addendum) — same team-optional move as `CreateAgentRequest.team`
    above, for the same reason: the create modal's Team station is gone,
    so a client that omits this key entirely must still get a recorded
    team, and "shared" is what nearly every persona already carries.
    """

    team: str = Field(default="shared", pattern=TEAM_SLUG_RE.pattern)


class PersonaDraftRequest(BaseModel):
    """POST /api/personas/draft body (Persona v3 addendum): one plain job
    description, plus optional short "always/never" rule hints — the
    generator (or, failing that, a deterministic fallback) turns this into
    a name/who_they_are/what_they_do draft for the v2 preview to edit.
    `rules` here is never stored; it's only ever a prompt hint for this one
    draft — the room-authored rules a persona actually ships with are
    whatever the operator edits into the v2 preview before Create."""

    job: str = Field(min_length=1, max_length=500)
    # Same 0-150 optional-one-liners shape as PersonaFields.rules
    # (kobo_api.personas, upload/limits addendum) — mirrored here rather
    # than imported, same "mirror the bounds, don't cross-import
    # validation" convention NameSuggestionsRequest above already uses.
    rules: list[str] = Field(default_factory=list, max_length=150)

    @field_validator("rules")
    @classmethod
    def _rules_shape(cls, v: list[str]) -> list[str]:
        # No numeric per-rule length cap any more, mirroring
        # PersonaFields._rules_shape's own drop of its old 200-char
        # ceiling — a rule hint this long is bounded by the merged
        # persona.md's real render gate once it's actually created, not a
        # per-field lookalike of it here.
        for item in v:
            if not item:
                raise ValueError(f"each rule must not be empty: {item!r}")
            if "\n" in item:
                raise ValueError(f"each rule must be a single line (no newlines): {item!r}")
        return v


class PersonaDistillRequest(BaseModel):
    """POST /api/personas/distill body (persona-first station addendum): the
    operator's own ready-made persona document (uploaded or pasted), from
    which the generator (or, failing that, a deterministic fallback) pulls
    discovery metadata for the colleague directory — a name, a one-line
    who-they-are, and purpose tags. Mirrors PersonaFields.what_they_do's own
    1-49152 bound (kobo_api.personas, upload/limits addendum) — same
    "mirror the bounds, don't cross-import validation" convention
    PersonaDraftRequest.job above already uses, since this IS that same
    persona-body text, just arriving before a job description would."""

    body: str = Field(min_length=1, max_length=49152)


class AgentMetadataPatch(BaseModel):
    """PATCH .../metadata body (persona-metadata plan, Task 3): every field
    optional — the route only touches what's actually submitted, leaving
    every omitted field at its current stored value. Plain `str`/`list[str]`
    (no `Field(pattern=...)`/cap here) deliberately, same reasoning as
    `CreateAgentRequest.team`: a shape mismatch should land in the route as
    plain rejection copy, not the generic RequestValidationError message a
    pydantic-level constraint would trigger instead."""

    team: str | None = None
    purpose_tags: list[str] | None = None
    capability: str | None = None


class AgentRuntimePatch(BaseModel):
    """PATCH .../runtime body (#15). One required field, unlike
    `AgentMetadataPatch` above: there is nothing here to leave alone, so an
    omitted `runtime_image` is a malformed request rather than a no-op.

    Plain `str` for the same reason its sibling gives — the registry-root
    rule lives in `kobo_api.runtime.validate_runtime_image`, and a caller
    who names an image this org does not run should read that rule stated
    plainly, not a generic RequestValidationError from a pydantic
    pattern."""

    runtime_image: str


# The delivery-status route's sha path param must look like a real git
# commit sha (as returned by save_knowledge_file/delete_knowledge_file, both
# `github.local_head`) before it ever reaches kobo_api.knowledge's
# delivery_state, which takes any str with no shape check of its own — this
# is purely an HTTP-layer guard against a malformed path segment. Matches
# git's own short-to-full hex sha range.
KNOWLEDGE_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

# The daedalus runtime's own teach-lane filename rule
# (`daedalus.memory.teach._FILENAME_RE`, mirrored byte-for-byte here,
# fast-memory-lane arc3c Task 4): lowercase letters, digits, and hyphens, at
# most 40 chars before the extension, ending in ".md". An intake upload now
# relays through that lane's "teach" system task, so a filename this rule
# would refuse must be refused before any relay is attempted -- see
# `upload_intake`'s own comment for why this can't just be left to
# `kobo_api.intake.INTAKE_FILENAME_RE` (looser on extension, admitting
# ".txt", but stricter on the leading character).
_INTAKE_FAST_LANE_FILENAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}\.md$")

# Slack channel id shape (Global Constraints, P2c plan): format-only — never
# a semantic check that the channel actually exists (that's Slack's job when
# the agent tries to post to it). The rendered manifest's scopes/events
# (channels:history + groups:history, message.channels + message.groups)
# cover both public and private channels, since modern private channels are
# C-prefixed just like public ones. Legacy G-prefixed channel ids are not
# supported: Slack has migrated private channels to C ids, so there's no
# live channel this regex would need to admit. Moved to kobo_api.slack_state
# (two-lane Slack, 2026-08-17) so the channel-list helpers can share it
# without importing back into main.py.

# Avatar generation (P2d, Global Constraint): reject a generated image bigger
# than this BEFORE it's ever pushed to the repo or cached on the Agent row —
# a plain backend-result problem (the model returned something oversized),
# not a user-input problem, hence the 502 rather than a 422.
AVATAR_MAX_BYTES = 4 * 1024 * 1024

# Avatar upload (portrait upload/download): a human-supplied PNG is capped
# tighter than a generated one (AVATAR_MAX_BYTES above) — this IS a
# user-input problem, hence the 422 (not 502) below when it's exceeded. Small
# square icon, 2 MiB is generous.
AVATAR_UPLOAD_MAX_BYTES = 2 * 1024 * 1024

# The 8-byte PNG file signature ("magic bytes") every real PNG starts with —
# used to reject anything that isn't at least PNG-shaped (including an empty
# body) before it's ever pushed to the repo or cached on the Agent row.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _model_label(cfg: Config, model_id: str) -> str:
    """The catalog label for `model_id`, or the raw id itself when it isn't
    (or is no longer) in `cfg.models` — an agent created under an older
    config whose model has since been retired from the catalog must still
    render, not 500."""
    for model in cfg.models:
        if model.id == model_id:
            return model.label
    return model_id


def configured_catalogs(config: Config) -> dict[str, str]:
    """kind -> bucket name for every catalog that actually has one
    configured (Task 3 extensibility refactor): `{"skill":
    config.skills_bucket, "persona": config.personas_bucket}` with
    empty-string values removed. This is the single place that lists the
    known catalog kinds — adding a third (the upcoming "tool" kind) is one
    more entry here; `ingest_indexes` (kobo_api.search) iterates whatever
    mapping it's given, so nothing else needs to change. An empty dict means
    nothing is configured at all.
    """
    catalogs = {"skill": config.skills_bucket, "persona": config.personas_bucket}
    return {kind: bucket for kind, bucket in catalogs.items() if bucket}


def _catalogs_configured(clients: Clients | None, config: Config) -> bool:
    """Whether the FTS catalog has anything to refresh from: storage wired
    AND at least one catalog bucket named — the latter is just the
    truthiness of `configured_catalogs(config)`. Shared by the TTL hook and
    the explicit refresh route's 503 check (Task 3 brief: "storage is None
    or both bucket names empty") so the two can never disagree about what
    "unwired" means.
    """
    if clients is None or clients.storage is None:
        return False
    return bool(configured_catalogs(config))


def _maybe_refresh_catalogs(app: FastAPI, engine) -> None:
    """TTL-gated catalog refresh (Task 3 controller decision #1; made
    stale-while-revalidate by the Task 3 perf fix; fixed round 1 to give the
    ingest itself one true mutual-exclusion lock), shared by both search
    endpoints and the teams endpoint.

    Two branches, in order:

    - Cold start (never loaded AND `catalog_fts` is genuinely empty):
      serving nothing here helps nobody, so this is the one case that still
      pays for a full ingest INLINE, on the request path, exactly as this
      function always behaved before the perf fix. The cold check itself
      (`catalog_loaded_at is None and catalog_is_empty(engine)`) is read
      under `app.state.catalog_lock` — a cheap SELECT, but not a read this
      function should ever make unprotected — and re-read a second time
      after acquiring `app.state.catalog_ingest_lock`, which is what
      actually stays held for the ingest: two cold requests landing at once
      both see `cold=True` from the first (lock-protected) read, but only
      the one that wins the race for `catalog_ingest_lock` performs the
      ingest — the other's second read finds the table no longer empty and
      returns without ever calling `ingest_indexes` itself.
    - Stale-while-revalidate (every other stale case -- the table already
      has *something* to serve, even if it's an older snapshot, INCLUDING
      the warm-restart case where `catalog_loaded_at is None` in this
      process but `catalog_fts` already holds rows a prior process ingested
      -- `catalog_is_empty(engine)` is what actually tells that apart from
      a genuine cold start): the request path NEVER ingests. It returns
      immediately, leaving whatever is already in the table for the caller
      to read, and hands the actual refresh to a background daemon thread.
      `app.state.catalog_refreshing` (checked and set under the same
      `catalog_lock`) makes this single-flight: N stale hits landing at
      once all see the flag already set after the first one claims it, so
      only one background thread ever gets spawned. That thread reuses this
      same `engine` — the app's own, shared, `check_same_thread=False`
      engine (kobo_api.db), not a request-scoped `Session` — the identical
      discipline kobo_api.provisioner's own daemon threads follow for their
      session.

    `app.state.catalog_ingest_lock` is the one lock actually held around
    every `ingest_indexes` call anywhere in this module (this function's
    cold branch, `_background_refresh_catalogs` below, and the manual
    `POST /api/catalogs/refresh` route) — `catalog_lock` itself guards only
    the flags/timestamp and is never held across an ingest, so it can never
    again look like it excludes ingests it doesn't actually exclude.

    A refresh failure (a bucket blip, a transient storage error) is logged
    and swallowed in both branches — these routes must never 500 over it,
    and a failed background refresh must not advance `catalog_loaded_at`,
    so the next stale hit simply tries again. Contrast with the explicit
    `POST /api/catalogs/refresh` route below, which surfaces the same
    failure as a 502 — that route exists precisely for an operator who
    wants to know a refresh failed.
    """
    state = app.state
    if not _catalogs_configured(state.clients, state.config):
        return

    now = time.monotonic()
    if state.catalog_loaded_at is not None and now - state.catalog_loaded_at < CATALOG_TTL_S:
        return

    with state.catalog_lock:
        now = time.monotonic()
        if state.catalog_loaded_at is not None and now - state.catalog_loaded_at < CATALOG_TTL_S:
            return
        cold = state.catalog_loaded_at is None and catalog_is_empty(engine)
        if not cold:
            # An ingest holding `catalog_ingest_lock` right now covers this
            # hit too. Without this, a request landing after a cold ingest
            # commits its rows but before it records `catalog_loaded_at`
            # (both happen under that lock) reads "rows present, never
            # loaded here" as a warm restart and spawns a second refresh.
            if state.catalog_refreshing or state.catalog_ingest_lock.locked():
                return
            state.catalog_refreshing = True

    if cold:
        with state.catalog_ingest_lock:
            with state.catalog_lock:
                still_cold = state.catalog_loaded_at is None and catalog_is_empty(engine)
            if not still_cold:
                return
            try:
                ingest_indexes(engine, state.clients.storage, configured_catalogs(state.config))
            except ClientError as exc:
                log.warning("catalog cold-start ingest failed, serving an empty table: %s", exc)
                return
            with state.catalog_lock:
                state.catalog_loaded_at = time.monotonic()
        return

    threading.Thread(
        target=_background_refresh_catalogs, args=(app, engine), daemon=True
    ).start()


def _background_refresh_catalogs(app: FastAPI, engine) -> None:
    """The single-flighted background refresh `_maybe_refresh_catalogs`
    spawns for a stale-but-nonempty catalog table. Runs entirely off the
    request path -- nothing here has a caller waiting on it, so a failure
    is logged and swallowed exactly like the old inline path's fail-soft
    contract: the table just stays stale until the next request's stale hit
    tries again. `catalog_loaded_at` only advances on success, and
    `catalog_refreshing` is always cleared (under `catalog_lock`) in the
    `finally`, so a failed refresh can never wedge every future hit into
    believing one is permanently in flight.

    The ingest itself runs under `app.state.catalog_ingest_lock` -- fixed
    round 1's concurrency fix -- so this thread can never race a manual
    `POST /api/catalogs/refresh` (or a concurrent cold-start inline ingest)
    into two simultaneous `engine.begin()` writers WITHIN THIS PROCESS. It
    says nothing about another replica: a plain `threading.Lock` is not
    visible outside one interpreter. What serializes replicas is the
    transaction-scoped advisory lock `ingest_indexes` itself takes as the
    first statement of its rebuild (`kobo_api.search`, Postgres only) --
    without it two overlapping refreshes each DELETE what the other has
    not committed yet, and every catalog row ends up duplicated.
    """
    state = app.state
    try:
        with state.catalog_ingest_lock:
            ingest_indexes(engine, state.clients.storage, configured_catalogs(state.config))
    except ClientError as exc:
        log.warning("catalog background refresh failed, serving the stale table: %s", exc)
    else:
        state.catalog_loaded_at = time.monotonic()
    finally:
        with state.catalog_lock:
            state.catalog_refreshing = False


def _fail_launch(
    session_factory: sessionmaker[Session],
    session: Session,
    agent_id: int,
    exc: Exception,
    *,
    status: AgentStatus = AgentStatus.FAILED,
    verb: str = "provisioning",
) -> JSONResponse:
    """Record a `run_pipeline`/`run_destroy` launch failure (e.g.
    `Thread.start()` itself raising) as `status` (`AgentStatus.FAILED` by
    default, for `create_agent`/`retry_agent`; `AgentStatus.DESTROY_FAILED`
    for `destroy_agent`) and return the 503 `JSONResponse` every launch
    guard reports for it. `verb` only changes the error copy ("provisioning"
    vs "destruction"); both keyword-only with `provisioning`-shaped
    defaults so every pre-existing call site is unaffected.

    `session` (the request's own, from `Depends(get_session)`) may itself be
    poisoned by whatever raised `exc` — a dead connection mid-commit, say —
    so a FAILED write on that same session can raise too, turning what should
    be a caller-actionable 503 into an unhandled 500 on top of an agent still
    wedged in its pre-failure status: exactly what this guard exists to
    prevent. Mirrors `provisioner._run_pipeline_body`'s engine-level recovery
    (`recovery_session = session_factory()`) for the same reason: best-effort
    rollback+close the possibly-poisoned `session`, then do the actual FAILED
    write through a brand-new session opened fresh from `session_factory`.
    Even if that fresh-session write itself fails, log it and still return
    the 503 below — this function's return value must never depend on the
    write landing.
    """
    error_message = f"failed to start {verb}: {exc}"

    try:
        session.rollback()
    except Exception:  # best-effort cleanup only
        log.warning(
            "_fail_launch: rollback of possibly-poisoned session failed",
            exc_info=True,
        )
    finally:
        # Guarded the same way as the rollback above: a sufficiently
        # poisoned session (a genuinely dead connection) can raise on
        # close() too, and an unguarded raise here would propagate out of
        # this function before ever reaching the fresh-session FAILED write
        # below — turning the caller-actionable 503 this function exists to
        # guarantee into a raw 500 instead.
        try:
            session.close()
        except Exception:  # best-effort cleanup only
            log.warning(
                "_fail_launch: close of possibly-poisoned session failed",
                exc_info=True,
            )

    recovery_session = session_factory()
    try:
        agent = recovery_session.get(Agent, agent_id)
        if agent is None:
            log.warning(
                "_fail_launch: agent %s not found while recording launch failure",
                agent_id,
            )
        else:
            agent.status = status
            agent.error = error_message
            recovery_session.commit()
    except Exception:
        # Even the fresh session's write failed — still return the 503
        # below rather than let this become a raw 500 on top of the launch
        # failure it was trying to record.
        log.exception(
            "_fail_launch: failed to record FAILED for agent %s after launch failure",
            agent_id,
        )
    finally:
        recovery_session.close()

    return JSONResponse({"error": error_message}, status_code=503)


def _validation_error_message(exc: RequestValidationError | ValidationError) -> str:
    """A plain-language message for a validation failure, built ONLY from
    each error's field location.

    Written for the app-wide `RequestValidationError` handler (`create_app`,
    below) — the P2c final-review fix for FastAPI's *default* 422 handler,
    which was still live (no handler had been registered to override it).
    That default renders `exc.errors()` verbatim, and Pydantic's own
    "missing field" error carries an `input` key holding the ENTIRE
    submitted request body — so a `/slack/connect` call missing `channel`
    but carrying `bot_token`/`app_token` echoed both pasted Slack tokens
    straight back in the response body. This handler replaces that default
    globally (every route, not just Slack's) with the house
    `{"error": ...}` shape, built from `loc` and never from `input`, which
    must never appear here or in anything this function returns.

    It takes a bare pydantic `ValidationError` too, and that is not a
    convenience: `str()` on one of those renders the offending INPUT the
    same way, so every place that builds a model by hand and reports the
    failure needs this rather than an f-string. The MCP write tools
    (`kobo_api.mcp.tools.write`) are those places, and a tool argument can
    be a secret value.

    Field names alone (e.g. "channel", "messages.0.role") are just the
    request's own declared shape, not secret, so naming them is safe and
    more useful than a fully generic message.
    """
    fields = []
    for error in exc.errors():
        loc = [str(part) for part in error.get("loc", ()) if part != "body"]
        if loc:
            fields.append(".".join(loc))
    if not fields:
        return "that request wasn't quite right. Check the fields and try again"
    # dict.fromkeys: de-dupe while preserving first-seen order.
    names = ", ".join(dict.fromkeys(fields))
    return f"that request wasn't quite right. Check: {names}"


def _ready_agent_or_error(
    session: Session,
    request: Request,
    name: str,
    *,
    not_ready_message: str = "agent is not ready for memory changes",
) -> Agent | JSONResponse:
    """Shared 404/409 guard for the four knowledge routes (P2b) and the two
    Slack connect routes (P2c) below.

    Unlike `chat_with_agent`/`retry_agent` — each of which inlines its own
    lookup because their 409 conditions/messages differ (READY-to-chat vs
    FAILED-to-retry) — the knowledge and Slack routes all share the exact
    same 404/READY check, just with different 409 wording per feature (hence
    `not_ready_message`, defaulted to the knowledge routes' original text so
    every existing call site is unaffected), so it's factored out once here
    instead of copy-pasted across routes.

    The fetch itself goes through `_visible_agent_or_404` (access-control
    Task B2) — every one of this function's callers therefore inherits
    visibility filtering for free: an agent outside the caller's visibility
    reads as 404 here, identically to one that never existed, before this
    function's own READY check ever runs.

    Returns the `Agent` row when the guard passes, or the `JSONResponse` a
    route should return immediately otherwise — callers must `isinstance`-
    check the result before using it as an `Agent`.
    """
    agent = _visible_agent_or_404(session, request, name)
    if isinstance(agent, JSONResponse):
        return agent
    if agent.status != AgentStatus.READY:
        return JSONResponse({"error": not_ready_message}, status_code=409)
    return agent


def _room_edits_this_agent_or_409(agent: Agent) -> JSONResponse | None:
    """None when the room may write to this agent's repository; a 409 body
    otherwise (connected-agents design 2026-09-21).

    The design's one rule is that the room never pushes to a connected
    repo's branches. Two steps in the connected pipeline honour that by
    construction, but every route below that edits a bundle -- knowledge,
    skills, intake, teach, learnings, tooling, Slack wiring, runtime,
    metadata, avatar -- clones the agent's repo and pushes to its DEFAULT
    BRANCH, gated only on "ready" and "owner or operator", both of which a
    connected agent passes the moment it goes READY. So the rule has to be
    said once more, here, and called by each of them.

    409 rather than 403: the caller is allowed to ask, and nothing about
    who they are would change the answer. It is the agent that has no such
    thing to change.

    A gate, not a filter: this is called as early as the agent row is in
    hand and always BEFORE any clone, so a refused call costs nothing and
    leaves nothing behind. Read routes do not call it -- listing an empty
    set of skills is honest, and a connected agent's build log and status
    are as real as any other's.
    """
    if is_connected(agent):
        return JSONResponse(
            {
                "error": (
                    "this agent's repository belongs to its team, so the room "
                    "does not change it"
                )
            },
            status_code=409,
        )
    return None


# The two ways an agent can run, and the one sentence the room says when a
# caller names anything else. Both routes below check this before the scan,
# because the run mode decides what the scan even looks for.
RUN_MODES = (RUN_MODE_HTTP, RUN_MODE_HEADLESS)
RUN_MODE_REFUSAL = "pick how this agent runs: it serves HTTP, or it has no HTTP port"

# A Kubernetes Secret name is a DNS-1123 subdomain, and the room checks that
# here rather than letting the deploy platform reject the whole app later
# for one mistyped name.
SECRET_NAME_RE = re.compile(
    r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$"
)
MAX_SECRET_NAME_LENGTH = 63
# Eight is well past what any agent here mounts and low enough that a
# pasted list cannot turn into an unbounded column.
MAX_SECRET_NAMES = 8

# What a connected agent's `persona` column holds. The column is NOT NULL
# and a connected agent has no persona -- the room did not compose it -- so
# this is one fixed value that deliberately matches no catalog entry,
# rather than a real persona id nothing would ever read.
CONNECTED_PERSONA = "connected"

# The room's own team axis does not apply to a repository it did not
# compose: there are no skills to draw from a team's catalog.
CONNECTED_TEAM = "shared"

# What the room says when a connect arrives without the caller's word that
# they may connect this repository. The room cannot check ownership at all
# (the connect route's docstring says why), so this sentence, the log line
# and the scan report's preamble are the whole of what stands there.
REPO_OWNER_REFUSAL = (
    "confirm that you own this repository or have its owner's agreement"
)

# What the Secrets and environment routes say (design 2026-09-22). Written
# out here, together, because the four of them have to sound like one
# feature and because none of them may ever quote the platform's own text:
# a failed write's body carried values, and a response that echoed what
# came back could echo them too.
#
# A room with no secrets platform configured. A standing state rather than
# an outage, so the write says so plainly instead of asking for a retry,
# and the read answers 200 with `platform: false` so the panel can print
# one sentence in place of the list.
NO_SECRETS_PLATFORM = (
    "this room has no secrets platform configured, so it cannot keep "
    "secrets for an agent. Environment variables still work."
)
# The platform answered something other than a success. Which refusal it
# was goes to the log, never to the person, for the reason above.
SECRETS_PLATFORM_UNREACHABLE = "couldn't reach the secrets platform. Try again."
SECRETS_WRITE_FAILED = "couldn't store that on the secrets platform. Try again."
# The value is stored and the one-line repository edit that names it is
# not. A retry of the same save fixes it: the write is a merge, and the
# wiring is append-if-missing.
SECRETS_WIRING_FAILED = (
    "the secret is stored, but the agent's files could not be updated to "
    "read it. Try saving again."
)
# The mirror, for the direction that deletes. Never "couldn't store that":
# nothing was being stored, and a person told their secret failed to save
# would go looking for one that is already gone.
SECRETS_REMOVE_FAILED = (
    "couldn't remove that from the secrets platform. Try saving again."
)
# The removal finished and only the deploy did not fire. Said apart from
# the above because there is nothing left to remove and nothing to retry
# except the deploy itself.
SECRETS_REMOVED_NO_DEPLOY = (
    "the secret is removed, but the agent could not be asked to redeploy. "
    "It picks this up the next time it starts."
)
# What the room leaves on a pull request of its own that a later change
# has made wrong to merge.
WIRING_SUPERSEDED = (
    "Superseded by a later change in the room: this agent's secrets have "
    "changed since this was opened, so merging it would undo that. Closing "
    "it; the room will open a fresh one if it needs to."
)
# The platform refused to regenerate. Two sentences, because a save that
# DID change the agent's files is not the same news as one that changed
# nothing: telling somebody "couldn't save" about a file that is saved
# sends them to redo work already done.
DEPLOY_TRIGGER_FAILED = (
    "the change is saved, but the agent could not be asked to redeploy. It "
    "picks this up the next time it starts."
)
DEPLOY_TRIGGER_ONLY = (
    "the agent's files already said this, so nothing changed. Asking it to "
    "redeploy did not work; try again in a moment."
)
AGENT_FILES_UNREACHABLE = "couldn't reach your agent's files. Try again."

# What the room says when an operator asks to convert an agent it cannot
# convert. Two sentences, because the two reasons are nothing alike: one is
# already what it was asked to become, the other is being worked on right
# now by a thread that reads its kind as it goes.
ALREADY_CONNECTED_REFUSAL = "this agent is already connected"
# Deliberately not `steps.HEADLESS_UNAVAILABLE`: that sentence is about what
# the room can run, and this refusal holds even in a room that runs headless
# agents. See the convert route for why the answer would otherwise lie
# dormant in the column until a much later retry acted on it.
CONVERT_HEADLESS_REFUSAL = (
    "an agent the room composed serves web traffic; convert it as one"
)
CONVERT_IN_FLIGHT_REFUSAL = (
    "this agent is still being created or destroyed, so it cannot be converted yet"
)

# The statuses a convert refuses. READY and FAILED are the two it takes: a
# failed agent is the likelier of the two to want this, since a repository
# that stopped running the room's runtime is how it failed.
CONVERT_REFUSED_STATUSES = (
    AgentStatus.PENDING,
    AgentStatus.PROVISIONING,
    AgentStatus.DESTROYING,
    AgentStatus.DESTROY_FAILED,
)

# How many scans one caller may run in `rate_limit.WINDOW_S`. Far past any
# form filling itself in and far below anything that could walk the
# organisation: the scan reads private repositories with the room's own
# credentials, which makes it worth a ceiling even though it writes
# nothing. Per replica, like everything else that module counts.
SCAN_LIMIT = 10
SCAN_BUCKET = "connect-scan"

# How many repository searches one caller may run in `rate_limit.WINDOW_S`.
# Far past anyone typing a repository name into a form (the field is
# debounced, so a long name costs a handful) and far below anything that
# could walk an organisation a prefix at a time. Its own bucket, so
# somebody who has typed a lot can still scan the repository they were
# looking for.
REPO_SEARCH_LIMIT = 60
REPO_SEARCH_BUCKET = "connect-repos"

# How many restarts one caller may ask for in `rate_limit.WINDOW_S`. A
# restart is the one button here that costs the agent something -- its
# pods go down and come back -- and nobody legitimately presses it twice
# in the same breath, let alone ten times. Its own bucket, so a person who
# has been restarting an agent can still search and scan.
RESTART_LIMIT = 10
RESTART_BUCKET = "agent-restart"

# What the room says about a restart. The 409 is about the agent rather
# than the caller: there is no Deployment to roll until the pipeline has
# finished making one.
NOT_DEPLOYED_YET = "this agent is not running yet, so there is nothing to restart"
RESTART_FAILED = "couldn't ask the cluster to restart this agent. Try again."
TOO_MANY_RESTARTS = "that is a lot of restarting. Wait a minute"

# What a scan or a connect answers when GitHub itself cannot be read. A
# report saying "nothing wrong" would be a lie, and the raw client error
# is never shown to a caller (`generate_avatar`'s precedent, same shape).
REPO_UNREADABLE = "the repository could not be read right now, try again shortly"

# What the connect routes answer when the room has no GitHub client at
# all: not a fact about any repository, and the same sentence wherever it
# is said.
GITHUB_UNREACHABLE = "the room can't reach GitHub right now"

# The repository typeahead's own version of REPO_UNREADABLE: it was asking
# for a listing, not a repository. Said rather than answered with an empty
# list, because an installation with nothing in it and a listing the room
# could not read look identical in a dropdown and are not the same thing.
REPOS_UNREADABLE = "the room could not list its repositories right now, try again shortly"


def _scan_caller(request: Request) -> str:
    """Who this scan (or repository search) counts against.

    The verified IAP email when there is one. A room with no perimeter has
    no identity to count by, so it counts by the client's address instead:
    a local room still gets the speed bump, and nobody in a real room is
    ever counted as somebody else.
    """
    email = (getattr(request.state, "iap_email", None) or "").lower()
    if email:
        return email
    client = request.client
    return f"host:{client.host}" if client is not None else "host:unknown"


def _connect_fields_block(payload: ScanRepoRequest) -> JSONResponse | None:
    """None when the name and run mode are shapes the room can work with.

    Shared by the scan and the connect routes so a name the room would
    refuse cannot come back from a scan looking available.
    """
    name_reason = validate_name(payload.name)
    if name_reason is not None:
        return JSONResponse(
            {"error": f"invalid name ({name_reason}): {payload.name!r}"}, status_code=422
        )
    if payload.run_mode not in RUN_MODES:
        return JSONResponse({"error": RUN_MODE_REFUSAL}, status_code=422)
    return None


def _clean_secret_names(values: list[str]) -> list[str] | str:
    """The secret names to record, or the refusal sentence.

    Returns the message rather than raising, the same shape `clean_tags`
    uses for the same reason: the caller answers 422 with it and has no
    exception to format. Duplicates are dropped rather than refused (a
    pasted list repeating a name means one mount either way), in the order
    they were given, so what the owner sees back is what they wrote.
    """
    # Counted as GIVEN, before anything is dropped: a pasted list of a
    # hundred copies of one name is still a pasted list of a hundred
    # names, and this cap bounds what arrives rather than what survives.
    if len(values) > MAX_SECRET_NAMES:
        return f"this room takes at most {MAX_SECRET_NAMES} secret names"

    cleaned: list[str] = []
    for value in values:
        name = value.strip()
        if len(name) > MAX_SECRET_NAME_LENGTH:
            # The name itself is not echoed back: it is long enough to
            # swamp the sentence, and the caller has it in front of them.
            return (
                f"a secret name can be at most {MAX_SECRET_NAME_LENGTH} characters"
            )
        if not SECRET_NAME_RE.match(name):
            return (
                f"{name!r} does not look like a secret name. Use lowercase "
                "letters, numbers, dashes and dots"
            )
        if name not in cleaned:
            cleaned.append(name)
    return cleaned


def _findings_body(findings: list[Finding]) -> list[dict]:
    return [
        {"code": finding.code, "message": finding.message} for finding in findings
    ]


def _scan_report_body(report: ScanReport) -> dict:
    """The scan as the form reads it: codes for the UI to key on, sentences
    for a person to read, the sentence about what the room cannot check,
    and the list of what will be added."""
    return {
        "blockers": _findings_body(report.blockers),
        "warnings": _findings_body(report.warnings),
        "adds": list(report.adds),
        "adds_preamble": report.adds_preamble,
        "repo": report.repo,
    }


def _group_choice_block(
    app: FastAPI, request: Request, cfg: Config, group_name: str | None
) -> JSONResponse | None:
    """None when this caller may put a new agent in `group_name`; the
    refusal body otherwise.

    Access control (Task B2): once the room has named its groups
    (`cfg.groups` non-empty), every new agent must declare which one it
    belongs to -- this IS the ACL namespace `_visible_agents` filters on,
    so an agent with no group would be invisible to everyone but its owner
    and the room's operators, forever, the moment groups turn on. A room
    that hasn't named any groups yet stays exactly as today: `group_name`
    is optional and stored verbatim, unvalidated (pinned by
    test_create_agent_stores_group_name_verbatim_when_provided /
    test_create_agent_omitted_group_name_defaults_to_none, Task A2).

    Access control (final-review fix round, HIGH-1: consent). Being a NAME
    in cfg.groups was the only thing checked at first -- any signed-in
    caller could claim ANY configured group and inherit its implicit
    same-group wire pairs (kobo_api.pairs.allowed_pairs) the moment this
    agent goes READY, with no consent from anyone already in that group.
    That nullifies spec section 5's request-and-accept handshake for
    cross-group access by letting a caller simply BE the other group
    instead of asking it to accept an invite. An operator is exempt --
    plain email or group-based (`_is_admin`) -- may assign any configured
    group, the same standing every other operator-vs-owner gate in this
    file grants (operator-status-by-group ruling: an admin group is
    operator status everywhere, this consent check included). Everyone else
    must have the claimed group in their OWN resolved membership
    (`clients.groups.groups_for`), fail-closed the same way
    `_visible_agents` treats a `None` answer: a resolver that can't answer
    right now grants nothing, never "assume it's fine."

    IAP-off dev rooms keep today's behavior untouched: the same
    config-level bypass idiom `_owner_or_operator_or_403`/`_operator_block`
    already use elsewhere in this file ("the room trusts its perimeter only
    when it has one") -- with no verified email ever available, there is no
    identity to check membership against, so gating here would just lock
    out every caller in every IAP-off room (local dev, most of this test
    suite) instead of the callers this fix actually targets.

    SHARED_GROUP is exempt from this whole consent check, not just from its
    resolver answer (shared-group + group-requests wave): this gate exists
    to stop a caller CLAIMING someone else's real group without asking;
    "shared" isn't anyone's group to protect -- every signed-in caller is
    already, unconditionally, a member of the commons, so there is nothing
    here for a resolver to confirm one way or the other.

    One helper rather than one copy per route (connected-agents design
    2026-09-21): create and connect both put a new agent into a group, and
    a gate that answered differently in the two places would be a way in
    through whichever one was weaker.
    """
    if not cfg.groups:
        return None

    configured_group_names = {g.name for g in cfg.groups}
    if group_name is None:
        return JSONResponse({"error": "pick a group for this agent"}, status_code=422)
    # SHARED_GROUP (shared-group + group-requests wave) is always a valid
    # choice even though it never appears in `cfg.groups` -- the commons is
    # usable by everyone, room-config-independent.
    if group_name != SHARED_GROUP and group_name not in configured_group_names:
        return JSONResponse(
            {"error": f"unknown group: {group_name!r}"}, status_code=422
        )

    if cfg.iap_audience and group_name != SHARED_GROUP:
        email = (getattr(request.state, "iap_email", None) or "").lower()
        if not _is_admin(request):
            clients: Clients | None = app.state.clients
            resolver = clients.groups if clients is not None else None
            caller_groups = resolver.groups_for(email) if resolver is not None else None
            if caller_groups is None:
                return JSONResponse(
                    {
                        "error": (
                            "Group membership could not be confirmed "
                            "right now. Try again shortly."
                        )
                    },
                    status_code=403,
                )
            if group_name not in caller_groups:
                return JSONResponse(
                    {"error": f"You are not a member of {group_name}."},
                    status_code=403,
                )

    return None


def _room_may_commit_to_repo(app: FastAPI, repo: str) -> bool:
    """Whether the room owns `repo` and so may commit to it, asked by
    repository name rather than by agent row.

    The background archive lanes below take a repo name, not an agent, and
    run on their own thread after their route has returned. Every route
    that queues one already calls `_room_edits_this_agent_or_409`, so this
    can only ever say no if a route stopped calling it -- which is the
    point of asking twice. A WARNING rather than a quiet return, for that
    reason: reaching here means a gate went missing upstream.

    A repo with no agent row is left alone to behave exactly as it did
    before this existed, rather than being refused on a row that was never
    the question.
    """
    session = app.state.session_factory()
    try:
        agent = session.query(Agent).filter_by(repo_full_name=repo).first()
    finally:
        session.close()
    if agent is not None and is_connected(agent):
        log.warning(
            "archive lane reached a connected agent's repository (%s); not "
            "committing",
            repo,
        )
        return False
    return True


def _sync_directory_best_effort(clients: Clients, config: Config, agent: Agent) -> None:
    """Best-effort switchboard resync after a directory-metadata edit (the
    PATCH and Regenerate routes below, right after their own
    `commit_card_metadata` call succeeds) — colleagues switchboard plan,
    Task 2.

    A no-op when the switchboard isn't wired (`cabal_enabled` false), same
    gate every other switchboard-facing caller uses. When it IS wired, any
    `sync_directory` failure (switchboard unreachable, a stale card, ...) is
    logged and swallowed here, never raised: the metadata edit itself
    already committed by the time either caller reaches this call, and a
    directory hiccup must not turn an otherwise-successful PATCH/regenerate
    into an error response, nor roll back what was already saved.
    """
    if not cabal_enabled(config):
        return
    try:
        sync_directory(clients.github, clients.cabal, config, agent)
    except Exception:
        log.warning(
            "directory sync failed for agent %s after a metadata update",
            agent.name,
            exc_info=True,
        )


# Same plain 409 detail `sync_directory_route` already uses (below) — kept
# as a literal here too rather than a shared constant, so the two call
# sites can't accidentally drift apart from a shared-string refactor no one
# asked for.
_SWITCHBOARD_NOT_CONFIGURED = {"error": "the switchboard is not configured"}


def _colleagues_gate(config: Config, clients: Clients) -> JSONResponse | None:
    """None when the three colleagues-proxy routes below (Task 4/B4 "the
    watching window") may proceed; the shared plain 409 otherwise.

    Checks BOTH `cabal_enabled(config)` AND `clients.cabal is not None` —
    same two-part gate `chat_with_agent`'s own switchboard branch uses
    (either alone isn't enough: an enabled-by-config-but-unwired room, or a
    wired client on a room that isn't enabled, must both still 409 rather
    than risk dereferencing a None `clients.cabal`), unlike
    `sync_directory_route`'s single `cabal_enabled` check, which gets away
    with it only because `Clients.real()` always keeps the two in lockstep
    in production.
    """
    if not cabal_enabled(config) or clients.cabal is None:
        return JSONResponse(_SWITCHBOARD_NOT_CONFIGURED, status_code=409)
    return None


def _conversation_has_participant(row: dict, agent: str) -> bool:
    """Whether `agent` participates in switchboard conversation `row`
    (access-tab refinement wave, conversation scoping) -- the `?agent=`
    filter every `/api/colleagues/conversations*` proxy route below
    applies server-side. `row` is either shape those routes see:
    `CabalClient.list_conversations`'s own row (`target_agent`/
    `caller_agent` at the top level), or `get_conversation`'s `
    "conversation"` object -- both carry the identical two keys, so one
    function covers both callers.

    Either side counts: `agent` is the TARGET (a bare name) or the CALLER
    (`caller_agent` is only ever `agent:<name>` for a genuine colleague-to-
    colleague call -- `CabalClient.mint_colleague`'s own `sub` shape -- so
    a human/service caller can never falsely match a bare name here).
    """
    return row.get("target_agent") == agent or row.get("caller_agent") == f"agent:{agent}"


# --- Colleagues: the terminal (terminal redesign) ---------------------------
#
# The operator's ruling: the per-conversation cards go away in favor of "a
# command line terminal, which showed all the conversations the agent was
# having with the other agents in order... and the text in full." `GET
# /api/colleagues/feed` below is the one flattened, chronological feed that
# view reads from; `_flatten_conversation_entries` is the piece that turns
# one `get_conversation` detail into the 0-2 lines it contributes.
#
# The switchboard's own persistence shape (verified against source, `cabal.
# core.broker.bookkeeping.Bookkeeper` -- this room has no swagger for its
# admin surface beyond what `CabalClient.list_conversations`/`get_
# conversation`'s own docstrings already describe): a conversation's `
# "result"` field (`get_conversation`'s "the full, untruncated result") is
# the COMPLETED a2a Task body, and it is ONLY EVER written once dispatch
# actually succeeds (`on_dispatch_success` persists it; `on_dispatch_start`
# persists nothing but routing metadata -- target/generation/caller -- and
# never the message text itself). Concretely, once populated:
#   - `result["status"]["message"]` is the TARGET's reply (the persona's own
#     new message, role `"agent"`).
#   - `result["history"][-1]` is the CALLER's original ask (the turn that
#     request's own `status.message` becomes once the target echoes it back
#     into its reply's history -- confirmed against `cabal.agents.echo.
#     EchoAgent`, the switchboard's own reference persona).
# A conversation still in flight, or one that failed before a reply ever
# landed, carries no `"result"` at all -- there is genuinely no ask text
# recorded anywhere on the switchboard's admin surface until the round trip
# completes, so `_flatten_conversation_entries` contributes ZERO lines for
# one of those, rather than inventing a lone "ask" entry from nothing. Once
# an in-flight conversation later completes, the next SSE frame's refetch
# picks up both its ask and reply together.


def _caller_display(caller: str) -> str:
    """Strip a caller's namespace prefix ("agent:iris" -> "iris",
    "operator:kgreiner@example.com" -> "kgreiner@example.com") for terminal
    display -- a raw caller with no prefix at all passes through unchanged.
    Same convention `ColleagueConversations.svelte`'s own (now retired)
    `callerLabel` used: the prefix is what `_conversation_has_participant`
    reads, not something a human needs to see in a terminal line.
    """
    idx = caller.find(":")
    return caller[idx + 1 :] if idx != -1 else caller


def _task_message_text(message: object) -> str | None:
    """ALL text parts of one a2a Message dict (`{"role": ..., "parts":
    [{"kind": "text", "text": ...}, ...]}`) joined in order -- `_join_text_
    parts` (kobo_api.clients), the family's join-all-parts contract, so a
    multi-part message renders whole and a leading non-text part doesn't
    hide the text behind it. `None` when `message` isn't a dict or nothing
    text-shaped is present (incl. an empty-string-only text part) -- the
    flattener emits no line for that, same as a conversation that never
    completed. Replaced the original `parts[0]`-only read, whose cited
    "no scanning" precedent in `_extract_relay_reply` had already been
    retired for this exact truncation tripwire.
    """
    if not isinstance(message, dict):
        return None
    return _join_text_parts(message.get("parts")) or None


def _conversation_detail_is_thread(detail: dict) -> bool:
    """Whether a `get_conversation`-shaped detail (`{"conversation": {...},
    "events": [...]}`) belongs to a thread turn: every turn a thread relays
    stamps `metadata.thread` on its envelope before relaying (`post_thread_
    turn`), and the switchboard's own completed-Task shape echoes a
    submitted task's `metadata` back verbatim on `result` -- so a thread's
    own turns are told apart from ordinary colleague talk by that one
    marker, never by caller/target shape alone.

    Critical 3 fix (the thread-transcript leak): pulled out of
    `_flatten_conversation_entries` below so every surface that reads a
    conversation's full detail can share the identical check -- a thread is
    a private, initiator-only conversation (operator ruling) and must never
    surface through ANY of them: the flattened colleague feed (below), the
    `kobo_feed` MCP tool (shares `_flatten_conversation_entries`), `GET
    /api/colleagues/conversations/{task_id}` (`get_colleague_conversation`),
    `GET /api/colleagues/conversations?all=true` (`list_colleague_
    conversations`, which fetches each surviving row's detail purely to run
    this check), and the SSE stream (`GET /api/colleagues/stream`, via
    `kobo_api.clients.filter_cabal_event_stream`'s own independent
    `_event_is_thread`, since that surface never has a full detail to
    check against -- only the raw dispatch events).
    """
    conv = detail.get("conversation")
    if not isinstance(conv, dict):
        return False
    result = conv.get("result")
    if not isinstance(result, dict):
        return False
    metadata = result.get("metadata")
    return isinstance(metadata, dict) and "thread" in metadata


def _flatten_conversation_entries(detail: dict) -> list[dict]:
    """One `GET /api/colleagues/conversations/{task_id}`-shaped detail,
    flattened into 0-2 chronological terminal entries: the caller's ask
    (`ts` = `created_at`), then the target's reply (`ts` = `updated_at`) --
    see this section's own module-level comment for exactly where each
    text comes from and why an incomplete conversation yields nothing.

    Each entry is `{"ts", "conversation_id", "from", "to", "role", "text"}`;
    `role` is `"ask"` (from = the caller's display name, to = the target)
    or `"reply"` (direction reversed: from = the target, to = the caller's
    display name) -- the reversal is deliberate, it's what lets the
    terminal color a line by direction (outgoing vs incoming) and read
    right as a real back-and-forth rather than two lines both "from" the
    same party.

    Thread tasks (threads arc, Task 9) contribute ZERO lines, same as an
    incomplete conversation (`_conversation_detail_is_thread` above) -- a
    thread is a private, initiator-only conversation (operator ruling) and
    must never surface in any participant's own colleague feed, whether
    read via `GET /api/colleagues/feed` or the `kobo_feed` MCP tool -- both
    share this exact function, so fixing it here closes both surfaces in
    one place.
    """
    conv = detail.get("conversation")
    if not isinstance(conv, dict):
        return []
    result = conv.get("result")
    if not isinstance(result, dict):
        return []
    if _conversation_detail_is_thread(detail):
        return []

    task_id = conv.get("task_id")
    target = conv.get("target_agent")
    caller_display = _caller_display(conv.get("caller_agent") or "")
    created_at = conv.get("created_at")
    updated_at = conv.get("updated_at")

    status = result.get("status")
    reply_text = _task_message_text(status.get("message")) if isinstance(status, dict) else None
    # The ask is the caller's QUESTION -- the last message with role "user",
    # NOT history[-1]. A completed daedalus conversation persists the FULL
    # transcript ([user question, agent reply, ...]), so history[-1] is the
    # AGENT's reply; taking it as the ask rendered the answer text on the
    # outgoing line (the "ward told gemini-one what a PSC is" display bug).
    # The last user-role message is the ask for every shape: cabal's builtin
    # EchoAgent (history = [user] alone) and real daedalus agents alike, and
    # for a multi-turn chat it pairs the most recent question with the most
    # recent reply (status.message).
    history = result.get("history")
    ask_msg = (
        next((m for m in reversed(history) if isinstance(m, dict) and m.get("role") == "user"), None)
        if isinstance(history, list)
        else None
    )
    ask_text = _task_message_text(ask_msg) if ask_msg is not None else None

    entries: list[dict] = []
    if ask_text is not None and created_at is not None:
        entries.append(
            {
                "ts": created_at,
                "conversation_id": task_id,
                "from": caller_display,
                "to": target,
                "role": "ask",
                "text": ask_text,
            }
        )
    if reply_text is not None and updated_at is not None:
        entries.append(
            {
                "ts": updated_at,
                "conversation_id": task_id,
                "from": target,
                "to": caller_display,
                "role": "reply",
                "text": reply_text,
            }
        )
    return entries


# The feed's scrollback cap -- applied AFTER scoping to `agent`'s own
# conversations, never as a `limit` passed to `list_conversations` itself.
# Fix round: a room-wide `limit` there truncates the ROOM'S most-recent
# conversations before `agent`'s own are ever picked out, so a quiet agent
# whose real conversations are older than the room's other traffic gets
# silently crowded out to an empty feed -- see `colleague_feed`'s own
# docstring for the full tradeoff. Each surviving conversation contributes
# at most 2 entries, so this cap counts flattened entries, not conversations.
_FEED_SCROLLBACK_CAP = 200


# Uncalled by any route since Slack self-service (2026-08-17) — kept because
# the `slack_operators` config field it reads stays live for other uses.
def _slack_operator_block(request: Request, config: Config) -> JSONResponse | None:
    """None when the caller may use slack routes; a 403 body otherwise.

    Empty list = ungated (dev parity). IAP disabled (no iap_audience) =
    operator: the room trusts its perimeter only when it has one.
    """
    if not config.slack_operators:
        return None
    if not config.iap_audience:
        return None
    email = (getattr(request.state, "iap_email", None) or "").lower()
    if email in {e.lower() for e in config.slack_operators}:
        return None
    return JSONResponse(
        {"error": "Slack setup is operator-only on this room"}, status_code=403
    )


def _is_operator(email: str | None, config: Config) -> bool:
    """The room's one operator matcher (access-control Task A1): a
    case-insensitive email match against `config.operators`. Every caller
    that needs to know "is this verified email an operator" -- `_operator_
    block`'s gate below and `/api/whoami`'s status flag -- goes through this
    single function so the comparison never forks between them. `None`/empty
    email is never an operator, no matter what `config.operators` holds.
    """
    if not email:
        return False
    return email.lower() in {o.lower() for o in config.operators}


def _is_admin(request: Request) -> bool:
    """Whether `request`'s verified caller holds operator status ROOM-WIDE
    (operator-status-by-group ruling: "the infra-engineering team should be
    able to send and accept requests for any agents, as they are the admin
    team over kobo"). An email operator (`_is_operator` above) always
    qualifies. ADDITIONALLY, a caller whose resolved directory group
    membership (`clients.groups.groups_for`) intersects `config.
    admin_groups` also qualifies -- group-based operator status, matching
    this room's benchmark model (the org's own infra team administers the
    whole PaaS).

    `_is_operator(email, config)` stays the pure, resolver-free email
    matcher, used as-is wherever no resolver is reachable. This function is
    the one place group membership ALSO grants operator status; every gate
    that has a `Request` in hand (as opposed to a bare email plus `Config`)
    calls this instead, so a room's admin groups take effect everywhere
    operator status already did: visibility, every owner-or-operator gate,
    invites on either side, and whoami's own status flag.

    Fail-closed, the same contract `_visible_agents` already holds group-
    derived visibility to: no `admin_groups` configured, no `Clients`
    reachable at all, or no `GroupResolver` wired on it means group-based
    admin status is simply off -- `_is_operator`'s plain email answer is
    still the last word, never widened by a directory this room can't
    currently reach. `admin_groups` empty short-circuits before the
    resolver is ever consulted, not just before it can grant anything --
    a room that hasn't opted into this at all pays no directory call and
    behaves exactly as it did before this function existed. A resolver
    that fails outright for this email (`groups_for` returning `None`)
    folds into that same "grant nothing" answer via the `or []` below,
    exactly `_visible_agents`'s own None-vs-`[]` discipline.
    """
    cfg: Config = request.app.state.config
    email = (getattr(request.state, "iap_email", None) or "").lower()
    if _is_operator(email, cfg):
        return True
    if not email or not cfg.admin_groups:
        return False
    clients: Clients | None = getattr(request.app.state, "clients", None)
    resolver = clients.groups if clients is not None else None
    if resolver is None:
        return False
    member_groups = resolver.groups_for(email) or []
    return bool(set(member_groups) & set(cfg.admin_groups))


def _operator_block(request: Request, config: Config) -> JSONResponse | None:
    """None when the caller may destroy agents; a 403 body otherwise.

    Same semantics family as `_slack_operator_block` above (empty `operators`
    AND empty `admin_groups` = ungated, dev parity; IAP disabled = operator,
    the room trusts its perimeter only when it has one; otherwise `_is_admin`
    above, which folds in group-based admin status alongside the plain email
    list) -- kept as its own function, not parameterized over the two config
    lists, since a future gate is free to diverge (different copy already,
    here) without the two ever needing to stay byte-identical.
    """
    if not config.operators and not config.admin_groups:
        return None
    if not config.iap_audience:
        return None
    if _is_admin(request):
        return None
    return JSONResponse(
        {"error": "Destroying agents is operator-only on this room"}, status_code=403
    )


def _admin_operator_block(request: Request, config: Config) -> JSONResponse | None:
    """None when the caller may run room-wide admin actions (today, just the
    dispatch-pairs resync route below, final-review fix round HIGH-2b); a
    403 body otherwise. Same semantics family as `_operator_block`/`_slack_
    operator_block` above (empty `operators` AND empty `admin_groups` =
    ungated, dev parity; IAP disabled = operator, "the room trusts its
    perimeter only when it has one"; otherwise `_is_admin`) -- kept as its
    own function rather than reusing `_operator_block` directly, since that
    one's 403 copy names destroying agents specifically and would read as
    flatly wrong copy for an unrelated admin route.
    """
    if not config.operators and not config.admin_groups:
        return None
    if not config.iap_audience:
        return None
    if _is_admin(request):
        return None
    return JSONResponse(
        {"error": "Syncing the switchboard is operator-only on this room"}, status_code=403
    )


def _convert_operator_block(request: Request, config: Config) -> JSONResponse | None:
    """None when the caller may convert a composed agent into a connected one
    (connected-agents design 2026-09-21); a 403 body otherwise. Same
    semantics family as `_operator_block`/`_admin_operator_block` above, and
    kept as its own function for the same reason those are: the copy names
    its own action.

    Its answer is byte-for-byte `_operator_block`'s today, and the page
    depends on that: the agent detail has no `can_convert` flag of its own,
    so the UI reads `destroy_visible` (computed by `_operator_block`) to
    decide whether to offer this action at all. A test pins the two together
    for exactly one caller -- a non-operator refused here sees
    `destroy_visible` false there -- so a future divergence has to be
    deliberate and comes with a flag of its own.
    """
    if not config.operators and not config.admin_groups:
        return None
    if not config.iap_audience:
        return None
    if _is_admin(request):
        return None
    return JSONResponse(
        {"error": "Converting agents is operator-only on this room"}, status_code=403
    )


def _is_reader(request: Request) -> bool:
    """Whether the caller holds the global READER role (reader-role wave,
    2026-08-28): a member of any `config.reader_groups` entry -- RAW Okta
    group names, checked against the resolver's raw (unfiltered) membership
    list. Visibility only: nothing anywhere consults this for a mutation.
    Fail-closed like every group check: no resolver, no email, or a
    directory outage (raw_groups_for -> None) all read as not-a-reader."""
    cfg: Config = request.app.state.config
    if not cfg.reader_groups:
        return False
    email = (getattr(request.state, "iap_email", None) or "").lower()
    if not email:
        return False
    clients: Clients | None = getattr(request.app.state, "clients", None)
    resolver = clients.groups if clients is not None else None
    raw_groups_for = getattr(resolver, "raw_groups_for", None)
    if raw_groups_for is None:
        return False
    raw = raw_groups_for(email)
    if raw is None:
        return False
    return bool(set(raw) & set(cfg.reader_groups))


def _repo_access_operator_block(request: Request, config: Config) -> JSONResponse | None:
    """None when the caller may decide repo-access requests (agent PR broker
    spec, 2026-08-27 ruling 1: the room's operator set -- the kobo owner
    group -- approves, in batch); a 403 body otherwise. Same semantics
    family as `_operator_block`/`_admin_operator_block` above, kept as its
    own function for the same reason those are: the copy names its own
    action."""
    if not config.operators and not config.admin_groups:
        return None
    if not config.iap_audience:
        return None
    if _is_admin(request):
        return None
    return JSONResponse(
        {"error": "Deciding repo access is operator-only on this room"}, status_code=403
    )


def _owner_or_operator_or_403(agent: Agent, request: Request) -> JSONResponse | None:
    """None when the caller may mutate `agent`; a 403 body otherwise
    (access-control Task A3). The gate for every mutating surface EXCEPT
    destroy (which stays operator-only, its own standing rule from the
    destruction arc -- `_operator_block` above, unchanged) and teach/chat
    (open by design to any signed-in user who can see the agent, per the
    arc spec).

    Read surfaces never call this -- only a route about to write something
    calls it, right after its own `_ready_agent_or_error`/agent-fetch guard
    and before any work. A legacy agent with no recorded `created_by` (a
    pre-Task-A2 row, or one created while IAP was off) is operator-only to
    mutate: fail closed rather than treat a missing owner as "anyone may
    claim it", and the rollout task's backfill closes that gap for good.

    IAP-disabled short-circuits to allowed, same escape hatch
    `_operator_block`/`_slack_operator_block` above already use ("the room
    trusts its perimeter only when it has one"): with no verified email
    ever available, `created_by` is never recorded either, so without this
    the gate would permanently lock every one of these routes for a room
    that never turned IAP on at all (local dev, most of this test suite) --
    a far wider blast radius than the fail-closed-on-a-null-owner case above
    is aimed at.
    """
    cfg: Config = request.app.state.config
    if not cfg.iap_audience:
        return None
    email = (getattr(request.state, "iap_email", None) or "").lower()
    # `_is_admin` (above) folds in group-based operator status alongside the
    # plain `_is_operator` email match -- every owner-or-operator gate in
    # this file grants standing to a room admin group, not just a listed
    # email (operator-status-by-group ruling).
    if _is_admin(request):
        return None
    if agent.created_by and email == agent.created_by.lower():
        return None
    return JSONResponse(
        {"error": "Only this agent's owner or a room operator can change it."},
        status_code=403,
    )


def _decider_or_403(
    agent: Agent, request: Request, session: Session
) -> JSONResponse | None:
    """The learnings decide gate (daily-learnings spec): owner or operator
    (the standard mutation gate), widened by a per-user grant -- a grantee
    already sees and chats with this agent as if they owned it, and
    approving a learning is teaching, which any visible caller can already
    do; the extra bar here exists to keep READERS out (see-all,
    mutate-nothing), and readers hold no grants, so they fail this exactly
    as any other non-owner does."""
    gate = _owner_or_operator_or_403(agent, request)
    if gate is None:
        return None
    email = getattr(request.state, "iap_email", None)
    if email:
        grant = (
            session.query(AgentUserGrant)
            .filter(
                AgentUserGrant.agent_name == agent.name,
                func.lower(AgentUserGrant.email) == email.lower(),
            )
            .first()
        )
        if grant is not None:
            return None
    return gate


def _visible_agents(session: Session, request: Request) -> tuple[list[Agent], bool]:
    """Which agents `request`'s caller may SEE (access-control Task B2:
    "each person sees their own aisle"), and whether group-derived
    visibility is degraded right now for them. Returns `(agents, groups_
    unavailable)`.

    Branches, in order:
    - IAP disabled entirely: full visibility, same escape hatch every other
      gate in this file uses (`_operator_block`/`_owner_or_operator_or_403`)
      -- with no verified identity there's nothing to filter by, so the
      room behaves exactly as it always has.
    - An operator, plain email or group-based (`_is_admin`, never forked):
      full visibility. Operators see everything, same as today, now
      including a member of one of `config.admin_groups` (operator-status-
      by-group ruling) -- distinct from the group-derived visibility branch
      below, which is about `config.groups`/`Agent.group_name`, a different
      axis entirely.
    - No `GroupResolver` wired (`clients.groups is None` -- no Okta token/
      domain/groups configured, `kobo_api.clients.Clients.real`'s own
      "absent = feature off" gate): full visibility. This is the honest
      phase-gate the design spec calls for verbatim: "Absent token = groups
      feature off, room behaves as today plus ownership" -- never narrower
      just because groups haven't been turned on in this room yet.
    - A resolver IS wired: the caller sees what they own (`created_by`)
      UNION every agent GRANTED to them by name (`AgentUserGrant`, access-
      control Task C1 -- see below) UNION every agent namespaced into the
      commons (`Agent.group_name == SHARED_GROUP`, shared-group + group-
      requests wave -- see below) UNION every agent in a group
      `groups_for(email)` says they belong to. If the resolver answers
      `None` (fail-closed: the directory itself couldn't be reached,
      kobo_api.groups' own None/`[]` distinction) -- only what the caller
      owns, was directly granted, or sits in the commons survives; group-
      DERIVED visibility vanishes until the directory answers again, never
      wider. `groups_unavailable` is true only in this exact branch. A
      grant is NOT group-derived (it's a row this room's own DB owns
      outright, never Okta's), so it survives a directory outage untouched
      -- the Global Constraint's "fail-closed on grants" is about
      Okta-sourced group membership disappearing, not about `AgentUserGrant`
      rows. A commons agent is the same story for the same reason: `SHARED_
      GROUP` membership was never Okta's to grant in the first place (every
      signed-in caller already has it, unconditionally), so a directory
      outage has nothing to take away from it either.

    Every branch above "no resolver wired" also implicitly covers ownership
    (an owned agent is always in the returned list) -- the design spec's
    "everyone sees agents they created" holds throughout, not just once
    groups are wired.
    """
    cfg: Config = request.app.state.config
    clients: Clients | None = request.app.state.clients
    all_agents = session.query(Agent).order_by(Agent.id).all()

    if not cfg.iap_audience:
        return all_agents, False

    email = (getattr(request.state, "iap_email", None) or "").lower()

    if _is_admin(request):
        return all_agents, False

    # Global READER (reader-role wave, 2026-08-28): full visibility, and
    # ONLY visibility -- every mutation gate below and elsewhere still
    # answers by owner/operator, never by this.
    if _is_reader(request):
        return all_agents, False

    resolver = clients.groups if clients is not None else None
    if resolver is None:
        return all_agents, False

    def is_owned(agent: Agent) -> bool:
        return bool(agent.created_by) and agent.created_by.lower() == email

    # AgentUserGrant (access-control Task C1): a direct, per-user visibility
    # grant, independent of group membership -- matched case-insensitively,
    # same convention `is_owned` above already uses for `created_by`. A
    # plain full-table scan, same idiom `all_agents` itself already is; this
    # room's grant table is never expected to be large enough to matter.
    granted_agent_names = {
        grant.agent_name
        for grant in session.query(AgentUserGrant).all()
        if grant.email.lower() == email
    }

    def is_granted(agent: Agent) -> bool:
        return agent.name in granted_agent_names

    groups = resolver.groups_for(email)
    if groups is None:
        return [
            agent
            for agent in all_agents
            if is_owned(agent) or is_granted(agent) or agent.group_name == SHARED_GROUP
        ], True

    group_set = set(groups)
    visible = [
        agent
        for agent in all_agents
        if is_owned(agent)
        or is_granted(agent)
        or agent.group_name == SHARED_GROUP
        or (agent.group_name is not None and agent.group_name in group_set)
    ]
    return visible, False


def _visible_agent_or_404(session: Session, request: Request, name: str) -> Agent | JSONResponse:
    """Fetch the agent named `name`, collapsing "doesn't exist" and "exists
    but this caller can't see it" into the identical 404 (access-control
    Task B2: existence is itself information a stranger to an agent's
    group must not get for free -- a 403 here would leak that much).

    The single choke point every agent-scoped route in this file resolves
    its agent through: `_ready_agent_or_error` (above) calls this for its
    own fetch step, and every route that inlines its own lookup because it
    needs a status other than READY (`get_agent`, `chat_with_agent`,
    `retry_agent`, `destroy_agent`, `get_avatar`) calls this directly
    instead of querying the table itself. Visibility is therefore enforced
    in exactly one place, never per-route.
    """
    agent = session.query(Agent).filter_by(name=name).first()
    if agent is None:
        return JSONResponse({"error": f"no such agent: {name!r}"}, status_code=404)
    visible, _ = _visible_agents(session, request)
    if agent not in visible:
        return JSONResponse({"error": f"no such agent: {name!r}"}, status_code=404)
    return agent


# --- Org status (Task 8): the room's read-only wiring visibility ------------
#
# Six checks, each individually try/excepted so a single unreachable backend
# never takes the whole route down with it (`ClientError` -> that check's own
# `ok: False` with a plain sentence — the route itself always answers 200).
# Every label/detail here is user-facing copy (Global Constraint: zero infra
# vocabulary) — "bucket"/"storage" appear because the checks ARE about
# storage, not as a leaked implementation detail.


def _org_config_check(org: OrgState) -> dict:
    """ok = org.wired; detail = org.error when set (every unwired reason,
    including the plain "no org configured" default), otherwise the repo
    name itself — `org.error or org.repo` reads as one formula for both
    branches since `load_org_config` (kobo_api.org) only ever sets one of
    the two (a wired OrgState carries no error; an unwired one always
    carries a plain-sentence error)."""
    return {
        "id": "org-config",
        "label": "Organization settings",
        "ok": org.wired,
        "detail": org.error or org.repo or "",
    }


def _bucket_check(check_id: str, label: str, clients: Clients | None, bucket: str) -> dict:
    """Whether `bucket` (the raw `config.skills_bucket`/`personas_bucket`
    value — no computed default) is reachable via `storage.list_keys`
    (Task 8 controller decision #4).

    An empty bucket name means "not configured" outright — `list_keys` is
    never even called — same as `clients`/`clients.storage` being absent
    entirely (an unwired room, or the local dev harness that never wires
    storage at all): both mean there's nothing to check yet, not a broken
    connection. `ok: True`'s detail is always "connected" (no bucket name
    leaked to the UI).
    """
    if not bucket or clients is None or clients.storage is None:
        return {"id": check_id, "label": label, "ok": False, "detail": "not configured"}
    try:
        clients.storage.list_keys(bucket, "")
    except ClientError:
        return {
            "id": check_id,
            "label": label,
            "ok": False,
            "detail": f"can't reach {label.lower()}",
        }
    return {"id": check_id, "label": label, "ok": True, "detail": "connected"}


def _skills_index_check(clients: Clients | None, bucket: str) -> dict:
    """ok = the skills bucket has a catalog index at either of the two
    paths `kobo_api.search.catalog_index_metadata` tries (`index.json`,
    then `catalog/catalog.json` — fix round 2, whole-arc review: the
    marketplace bucket's real layout). The healthy detail is deliberately
    path-agnostic (just the object's server-set `updated` timestamp) — this
    reports wired/unwired, never which filename resolved, so a bucket that
    flips from one layout to the other reads identically here.
    """
    check_id, label = "skills-index", "Skills index"
    if not bucket or clients is None or clients.storage is None:
        return {"id": check_id, "label": label, "ok": False, "detail": "not configured"}
    try:
        meta = catalog_index_metadata(clients.storage, bucket)
    except StorageNotFound:
        return {"id": check_id, "label": label, "ok": False, "detail": "no skills published yet"}
    except ClientError:
        return {"id": check_id, "label": label, "ok": False, "detail": "can't reach the skills index"}
    return {
        "id": check_id,
        "label": label,
        "ok": True,
        "detail": f"updated {meta.get('updated', 'unknown')}",
    }


def _personas_seeded_check(clients: Clients | None, bucket: str) -> dict:
    check_id, label = "personas-seeded", "Personas"
    if not bucket or clients is None or clients.storage is None:
        return {"id": check_id, "label": label, "ok": False, "detail": "not configured"}
    try:
        raw = clients.storage.read_bytes(bucket, "index.json")
    except StorageNotFound:
        return {"id": check_id, "label": label, "ok": False, "detail": "no personas seeded yet"}
    except ClientError:
        return {"id": check_id, "label": label, "ok": False, "detail": "can't read the personas list"}

    # A malformed index.json (bad JSON, or JSON that isn't the expected
    # {"entries": [...]} shape) must never 500 this route (Task 8
    # controller decision #5) — json.loads/`.get` failures aren't
    # ClientError, so they're caught here explicitly.
    try:
        entries = json.loads(raw).get("entries", [])
    except (ValueError, AttributeError, TypeError):
        return {"id": check_id, "label": label, "ok": False, "detail": "the personas list is unreadable"}

    count = len(entries)
    return {"id": check_id, "label": label, "ok": count > 0, "detail": f"{count} personas"}


def _deploy_platform_check(clients: Clients | None, cluster: str) -> dict:
    check_id, label = "deploy-platform", "Deploy platform"
    ping_ok = False
    if clients is not None:
        try:
            ping_ok = clients.gojira.ping()
        except ClientError:
            # HttpxGojiraClient.ping() already never raises (see its own
            # docstring) — this is defense-in-depth for any other
            # GojiraClient implementation (a future one, or a test fake)
            # that doesn't honor that contract.
            ping_ok = False

    if not cluster:
        return {"id": check_id, "label": label, "ok": False, "detail": "no cluster configured"}
    if not ping_ok:
        return {
            "id": check_id, "label": label, "ok": False,
            "detail": "can't reach the deploy platform",
        }
    return {"id": check_id, "label": label, "ok": True, "detail": f"connected to {cluster}"}


def get_session(request: Request) -> Iterator[Session]:
    """Dependency yielding a SQLAlchemy session bound to the app's session factory."""
    session_factory: sessionmaker[Session] = request.app.state.session_factory
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def _start_repo_warmer(app: FastAPI) -> RepoWarmer | None:
    """Put the connect form's repository listing in hand before the first
    person opens the form, and keep it there.

    Filling that cache is many sequential pages of the installation's
    repositories, and it used to happen on the first search -- so whoever
    opened the form first waited out all of them before a single row
    appeared. Nothing about the read is per-caller, so this does it off
    the request path at boot and again every TTL.

    None when the room has no GitHub client: `app.state.repo_index` is
    None in exactly that case, and there is nothing to warm. Boot is
    never blocked and never fails over this -- `RepoWarmer.warm_once`
    swallows and logs, and the next search fills the listing itself.
    """
    index: RepoIndex | None = app.state.repo_index
    if index is None:
        return None
    warmer = RepoWarmer(index)
    app.state.repo_warmer = warmer
    warmer.start()
    return warmer


@asynccontextmanager
async def _room_lifespan(app: FastAPI):
    """mcp_lifespan plus the room's own background work: the learnings
    nightly loop, the periodic sweep for provisioning runs whose process is
    gone (boot-only would never catch a pipeline killed just after the last
    boot), and the repository typeahead's cache warmer. TestClient
    instances used without `with` never enter lifespans at all, so unit
    tests see no background task unless they opt in."""
    async with mcp_lifespan(app):
        nightly = asyncio.create_task(learnings_nightly_loop(app))
        sweeper = asyncio.create_task(interrupted_sweep_loop(app))
        # A thread rather than a task, because the listing it reads is a
        # blocking `gh` call: on the loop it would stall every request in
        # the process for as long as GitHub takes.
        warmer = _start_repo_warmer(app)
        try:
            yield
        finally:
            if warmer is not None:
                # Joined, not just signalled, so a shutdown does not race
                # a listing still writing itself into the index -- but
                # with a timeout, and on a daemon thread, so it can never
                # be the reason this process will not exit.
                warmer.stop()
            for task in (nightly, sweeper):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


def _teach_task_not_completed(raw: object) -> bool:
    """True unless `raw` is a well-formed relayed task whose status state
    is exactly "completed": kobo must not claim the instant lane for a
    note the agent did not actually keep.

    Same unwrap-then-isinstance-guard style as `_extract_relay_reply`
    (kobo_api.clients), deliberately duplicated rather than shared: that
    helper raises on a malformed reply (its caller wants the exception),
    this one only ever needs a bool and must never raise, since the whole
    point is answering the question BEFORE it's safe to call
    `_extract_relay_reply(raw)` at all. A 2xx relay body that's valid JSON
    but not a dict (a bare list, or a `task`/`result` wrapper whose value
    isn't a dict either) counts as not-completed rather than raising
    AttributeError -- that would otherwise escape the route's own `except
    (CabalError, httpx.HTTPError, ValueError)` and turn an instant-lane
    miss into a 500 instead of the archive fallback the spec promises.

    The state check is deliberately an equality against "completed", not
    an inequality against "failed": the daedalus runtime's every success
    reply (including the already-known reply) is always state "completed",
    so anything else -- "canceled" (TaskAborted still carries a text
    part), "input-required" (a clarifying question is not the agent
    confirming it kept anything), "failed", or missing entirely -- means
    the note was not actually kept, even though a text part may still be
    sitting right there ready for `_extract_relay_reply` to happily
    return it.

    Module-level (MCP learnings trio, Task 8) so `_decide_learning` below
    can call it without a route's own closure -- every one of teach_agent,
    upload_intake, and the chat routes still call this exact function,
    resolved through the module scope now rather than an enclosing one; no
    call site there needed to change.
    """
    task: object = raw
    if isinstance(raw, dict):
        if isinstance(raw.get("task"), dict):
            task = raw["task"]
        elif isinstance(raw.get("result"), dict):
            task = raw["result"]

    if not isinstance(task, dict):
        return True

    status = task.get("status")
    if not isinstance(status, dict):
        return True

    return status.get("state") != "completed"


def _runtime_image_for_repo(app, repo: str) -> str:
    """The Dockerfile pin a fast-lane commit should carry for `repo`'s agent:
    the agent's own runtime when its row names one, the org default
    otherwise. Falls back to the org default when the row can't be found --
    a mirror commit must never crash over a lookup.

    The row wins even when it names an image the create-time rule would no
    longer accept: an agent built on a runtime the room has since retired
    must keep rebuilding on the image it actually runs, so this path never
    validates. `PATCH /api/agents/{name}/runtime` is where such an agent
    moves onto the org default, deliberately and once."""
    try:
        with app.state.session_factory() as session:
            row = session.query(Agent).filter_by(repo_full_name=repo).first()
            if row is not None and row.runtime_image:
                return row.runtime_image
    except Exception:
        log.debug("runtime lookup failed for %s; using the org default", repo)
    return app.state.config.runtime_image



def _archive_teach_note(app: FastAPI, repo: str, filename: str, note: str) -> None:
    """The background archive lane. Failure is logged, never surfaced: the
    memory is already stored in the agent; only the repo copy is missing,
    and the intake panel simply won't list the file until a later commit
    lands it.

    `archive_only=True`: this only ever runs after the relay above already
    reported the note "completed" -- the agent has the memory in hand, so
    this commit is purely a repo-side mirror of it. The agent repo's
    generated CI rebuilds and (Recreate strategy) hard-rotates the pod on
    every push; doing that here would just be downtime bought for a memory
    the pod already has. `upload_intake_file` still keeps the normal,
    CI-triggering message if a real Dockerfile re-pin rides along in this
    same push -- that rebuild is wanted regardless of which lane asked for
    the commit.

    Module-level (MCP learnings trio, Task 8), taking `app` explicitly
    instead of closing over it -- `_decide_learning` below needs this same
    background hook and has no enclosing `create_app` scope to close over.
    The three routes that schedule this via `background_tasks.add_task`
    (upload_intake, teach_agent, and `_decide_learning`'s own approve path)
    all now pass `app` as this function's first argument; behavior is
    otherwise byte-identical to the closure this replaces.
    """
    if not _room_may_commit_to_repo(app, repo):
        return
    try:
        upload_intake_file(
            app.state.clients.github,
            repo,
            filename,
            note,
            runtime_image=_runtime_image_for_repo(app, repo),
            archive_only=True,
        )
    except Exception as exc:
        log.warning(
            "teach archive commit failed for %s (%s): %s", repo, filename, exc
        )


def _decide_learning(
    name: str,
    learning_id: int,
    action: str,
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session,
    content: str | None = None,
) -> JSONResponse:
    """Shared verdict path for approve/deny/bulk (daily-learnings spec,
    Task 7). Approve rides the teach lane byte-for-byte (instant relay
    when cabal is wired, the CI-triggering archive commit otherwise) with
    the learning- filename prefix, so techne provenance reads
    intake:learning-<date>-<slug>.

    `content`, approve only (learnings rethink, Task 5): the operator's
    fixed wording, when present, replaces `row.content` BEFORE the note is
    formatted and taught -- this is what gets remembered and what the row
    shows afterwards. Bad wording (empty, over `EDITED_LEARNING_MAX_CHARS`,
    or multi-line) short-circuits with a 422 before anything is taught, and
    the ORIGINAL `content_hash` is kept even when the text changes -- the
    rejected-by-rewriting phrasing stays tombstoned against re-proposal.
    `deny` and the bulk route never pass `content`.

    Module-level (MCP learnings trio, Task 8) rather than a closure inside
    `create_app`: both `approve_learning`/`deny_learning`/
    `bulk_decide_learnings` below AND `kobo_decide_learning`/
    `kobo_decide_learnings_bulk` (kobo_api.mcp.tools.write) call this exact
    function, the same reuse-not-reimplement discipline every other gate
    in this file already holds. `request` stands in for the `app` this
    used to close over -- `request.app.state.config`/`.clients` reads the
    identical values `_ready_agent_or_error`/`_decider_or_403` (both
    already module-level) already read the same way off a real or
    synthetic (`kobo_api.mcp.authz.room_request`) request.
    """
    agent = _ready_agent_or_error(
        session,
        request,
        name,
        not_ready_message="agent is not ready for learnings decisions",
    )
    if isinstance(agent, JSONResponse):
        return agent
    blocked = _room_edits_this_agent_or_409(agent)
    if blocked is not None:
        return blocked
    gate = _decider_or_403(agent, request, session)
    if gate is not None:
        return gate
    row = (
        session.query(AgentLearning)
        .filter_by(agent_name=agent.name, id=learning_id)
        .first()
    )
    if row is None:
        return JSONResponse({"error": "no such learning"}, status_code=404)
    if row.status != LEARNING_PENDING:
        return JSONResponse(
            {"error": f"learning is already {row.status}"}, status_code=409
        )
    email = getattr(request.state, "iap_email", None)
    if action == "deny":
        row.status = LEARNING_DENIED
        row.decided_by = email
        row.decided_at = datetime.now(UTC)
        session.commit()
        return JSONResponse({"id": row.id, "status": row.status})

    if content is not None:
        edited = clean_edited_statement(content)
        if edited is None:
            return JSONResponse(
                {"error": "a learning is one sentence, 200 characters or fewer"},
                status_code=422,
            )
        # The ORIGINAL content_hash is kept on purpose: the phrasing the
        # operator rejected-by-rewriting stays tombstoned against re-proposal.
        row.content = edited

    on_date = datetime.now(UTC).date().isoformat()
    note = format_note(row.content, taught_by=email, on_date=on_date)
    filename = note_filename(row.content, on_date=on_date, prefix="learning")
    cfg: Config = request.app.state.config
    clients = request.app.state.clients
    mode = "archive"
    if cabal_enabled(cfg) and clients.cabal is not None:
        envelope = _build_teach_envelope(note, filename)
        try:
            raw = clients.cabal.relay(agent.name, envelope)
            if _teach_task_not_completed(raw):
                raise CabalError("agent reply was not a completed task")
            _extract_relay_reply(raw)
        except (CabalError, httpx.HTTPError, ValueError) as exc:
            log.warning(
                "learning approve relay failed for %s, archive lane: %s",
                name,
                exc,
            )
        else:
            mode = "instant"
            background_tasks.add_task(
                _archive_teach_note, request.app, agent.repo_full_name, filename, note
            )
    if mode == "archive":
        try:
            upload_intake_file(
                request.app.state.clients.github,
                agent.repo_full_name,
                filename,
                note,
                runtime_image=agent.runtime_image or cfg.runtime_image,
            )
        except (IntakeError, ClientError) as exc:
            log.warning("learning approve archive failed for %s: %s", name, exc)
            return JSONResponse(
                {"error": "couldn't deliver the approved learning. Try again."},
                status_code=502,
            )
    row.status = LEARNING_APPROVED
    row.decided_by = email
    row.decided_at = datetime.now(UTC)
    session.commit()
    return JSONResponse(
        {"id": row.id, "status": row.status, "mode": mode, "filename": filename}
    )


def create_app(
    session_factory: sessionmaker[Session],
    *,
    config: Config,
    clients: Clients | None = None,
    org: OrgState | None = None,
    iap_jwks_fetcher: Callable[[], dict] | None = None,
) -> FastAPI:
    app = FastAPI(title="kobo-api", lifespan=_room_lifespan)
    install_metrics(app)
    install_tracing(app)
    app.state.config = config
    app.state.clients = clients
    app.state.session_factory = session_factory
    # The repository typeahead's cache (kobo_api.repo_search): one listing
    # for the whole room, so it lives on the app rather than being rebuilt
    # per request. None when the room has no GitHub client, which is the
    # same "nothing to ask" the route answers 503 for.
    app.state.repo_index = (
        RepoIndex(clients.github)
        if clients is not None and clients.github is not None
        else None
    )
    # Set by `_start_repo_warmer` when the lifespan runs, which is a real
    # boot and not a TestClient used without `with`. Declared here so the
    # attribute always exists to be read.
    app.state.repo_warmer = None
    # `org` defaults to None in the signature (not a shared OrgState(...)
    # instance) so every create_app() call gets its own object here rather
    # than every caller that omits `org` pointing at one mutable instance in
    # common — see the module's Task 2 wiring in build_default_app for how a
    # real boot constructs and passes one. Every pre-existing create_app call
    # (the whole test suite) keeps working unchanged, reading this same
    # "not configured" state Tasks 3/8 render honestly off of.
    app.state.org = org if org is not None else OrgState(False, None, "no org configured")
    # Catalog FTS TTL/refresh state (Task 3 controller decision #1; extended
    # by the Task 3 perf fix, then split into two locks by the Task 3 fix
    # round 1 concurrency fix): read and written only by
    # `_maybe_refresh_catalogs`/`_background_refresh_catalogs` and the
    # refresh route below. `catalog_lock` guards the flags/timestamp only
    # (`catalog_loaded_at`, `catalog_refreshing`) — `catalog_refreshing` is
    # the single-flight guard, true exactly while a background refresh
    # thread is in flight, checked and set together with `catalog_loaded_at`
    # under `catalog_lock` so N concurrent stale hits spawn exactly one
    # thread. `catalog_ingest_lock` is this PROCESS's mutual exclusion for
    # the ingest itself — held around every `ingest_indexes` call
    # (cold-inline, background, and the manual refresh route) so two of
    # sqlite's `engine.begin()` writers can never run concurrently, past
    # the busy timeout, regardless of which of those three call sites is
    # running. It cannot serialize replicas (a threading.Lock is visible to
    # one interpreter only); the advisory lock inside `ingest_indexes`
    # (kobo_api.search) is the half that does.
    app.state.catalog_loaded_at = None
    app.state.catalog_lock = threading.Lock()
    app.state.catalog_refreshing = False
    app.state.catalog_ingest_lock = threading.Lock()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Identity-proxy assertion verification (deployability plan, Task 2):
    # active only when `config.iap_audience` is non-empty — registered here,
    # explicitly before the built-UI mount at the bottom of this function, so
    # it's clear at a glance that it covers the whole app (every `/api/*`
    # route above AND the UI's static/SPA paths below), not just the API.
    # `iap_jwks_fetcher` is a test-only seam (mirrors `clients`/`org` above):
    # real boots never pass it, leaving `IapVerifier` to fetch IAP's real
    # public keys over the network; tests inject one backed by a
    # locally-generated keypair so the verify path runs against real
    # cryptography instead of a mock. See kobo_api.iap for the full contract.
    if config.iap_audience:
        app.add_middleware(
            IapMiddleware,
            verifier=IapVerifier(config.iap_audience, jwks_fetcher=iap_jwks_fetcher),
        )

    # https-only OAuth/MCP surface (CYBER-1132 ride-along). Registered LAST
    # on purpose: Starlette runs the last-added middleware FIRST, so this
    # sits OUTSIDE IapMiddleware above. That ordering is load-bearing twice
    # over -- the roots it guards (/oauth, /.well-known, /mcp) are exactly
    # the roots IapMiddleware exempts, so a guard installed inside the IAP
    # gate would be reached only via that gate's pass-through; and a request
    # that arrived over plain http should be turned away before any
    # credential in it is read, not after. Unconditional, unlike the IAP
    # gate: these roots are served off the deploy's NO-IAP ingress backend,
    # so a room with `iap_audience` unset is precisely the room that needs
    # it. It is a no-op for every caller that does not set
    # X-Forwarded-Proto: http, which is every in-cluster caller (the
    # /broker lane, the /metrics scraper, kubelet probes). See
    # kobo_api.iap.HttpsOnlyMiddleware for the full three-way contract.
    app.add_middleware(HttpsOnlyMiddleware)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Overrides FastAPI's default handler app-wide — see
        # `_validation_error_message`'s docstring for why (the token-echo
        # fix) and why every shape-422 across the app is harmonized onto the
        # same `{"error": ...}` convention the UI already expects elsewhere.
        return JSONResponse({"error": _validation_error_message(exc)}, status_code=422)

    @app.get("/api/healthz")
    def healthz() -> dict:
        return {"ok": True}

    # --- Whoami (access-control Task A1, groups made real in Task B2) -------
    #
    # Every authorization decision stays server-side off the VERIFIED
    # assertion email (`request.state.iap_email`, set only by IapMiddleware
    # once it has actually checked a signature) — this route just makes that
    # already-authoritative state visible so the UI can render it, never the
    # other way around. `groups` is the caller's CONFIGURED group membership
    # (`kobo_api.groups.GroupResolver.groups_for`) when a resolver is wired
    # (`clients.groups`), else `[]` -- the same "absent = feature off"
    # reading `_visible_agents` below uses. `groups_unavailable` mirrors the
    # list route's own flag: true only when a resolver IS wired but just
    # failed to answer for this email (fail-closed, never guessed as `[]`).
    # `operator` reflects `_is_admin` (operator-status-by-group ruling), not
    # just the plain email list -- a member of a configured admin group sees
    # this flag as true too, since the sidebar (and any future UI) reads it
    # as "does this room's operator standing apply to me," not "am I on a
    # hand-typed list." No UI surface may gate its OWN enforcement on this
    # flag either way -- it is a status hint, never the source of truth the
    # server-side gates above already are.
    #
    # `pending_group_requests` (shared-group + group-requests wave) is the
    # sidebar's badge count -- present ONLY when `operator` is true (an
    # admin is the only caller who may ever act on the queue,
    # `_group_requests_admin_block` below enforces the identical check on
    # the routes themselves); every other caller gets no such key at all,
    # not a `0`, so a non-admin client can never mistake "the room happens
    # to have zero open requests right now" for "I have standing to see the
    # queue."
    @app.get("/api/whoami")
    def whoami(request: Request, session: Session = Depends(get_session)) -> dict:  # noqa: B008
        clients: Clients | None = app.state.clients
        email = getattr(request.state, "iap_email", None)

        groups: list[str] = []
        groups_unavailable = False
        resolver = clients.groups if clients is not None else None
        if email and resolver is not None:
            resolved = resolver.groups_for(email)
            if resolved is None:
                groups_unavailable = True
            else:
                groups = resolved

        is_admin = _is_admin(request)
        result = {
            "email": email,
            "operator": is_admin,
            "groups": groups,
            "groups_unavailable": groups_unavailable,
        }
        if is_admin:
            result["pending_group_requests"] = (
                session.query(GroupRequest)
                .filter(GroupRequest.status == GROUP_REQUEST_PENDING)
                .count()
            )
        return result

    @app.get("/api/catalog/models")
    def catalog_models() -> dict:
        cfg: Config = app.state.config
        return {
            "models": [{"id": m.id, "label": m.label} for m in cfg.models],
            "default": cfg.models[0].id,
        }

    @app.get("/api/catalog/observability")
    def observability_catalog() -> dict:
        """The Argus deep-link config (Task 4). Both-or-nothing: url and
        project only ever surface together -- a half-configured pair reads
        as fully unset, since a link built from just one half is broken."""
        cfg: Config = app.state.config
        both = bool(cfg.argus_url and cfg.argus_project)
        return {
            "url": cfg.argus_url.rstrip("/") if both else None,
            "project": cfg.argus_project if both else None,
        }

    # Access control (Task B2): the create modal's group dropdown reads this
    # -- CONFIG names only, never the Okta group names `cfg.groups` also
    # carries (`GroupChoice.okta_group`), which are an implementation detail
    # the UI has no business knowing. `cfg.groups` alone would be empty when
    # the room hasn't named any groups yet, same "absent = feature off"
    # shape as `/api/catalog/models` would give an empty `cfg.models` (which
    # can't actually happen, but the convention matches every other catalog
    # route here) -- but SHARED_GROUP (shared-group + group-requests wave)
    # is never itself empty-conditional: it's ALWAYS the first entry,
    # regardless of whether the room has named any real groups, since it's
    # usable by everyone whether or not `cfg.groups` is configured at all.
    # `kind` lets the UI section the dropdown ("shared" vs the room's own
    # named groups) without hardcoding SHARED_GROUP's spelling client-side.
    # Create-modal non-engineering-wing ruling: each configured group now
    # carries ITS OWN `kind` (`GroupChoice.kind` -- was hardcoded
    # "engineering" for every entry here). The response still comes back
    # grouped rather than in raw `cfg.groups` order: shared first, then
    # every engineering entry (config order preserved within the group),
    # then every non-engineering entry (config order preserved) -- the UI
    # sections purely on `kind` regardless, but a stable grouped payload
    # keeps the wire shape readable on its own.
    @app.get("/api/catalog/groups")
    def catalog_groups() -> dict:
        cfg: Config = app.state.config
        engineering = [g for g in cfg.groups if g.kind == "engineering"]
        non_engineering = [g for g in cfg.groups if g.kind == "non-engineering"]
        return {
            "groups": [{"name": SHARED_GROUP, "kind": "shared"}]
            + [{"name": g.name, "kind": g.kind} for g in engineering]
            + [{"name": g.name, "kind": g.kind} for g in non_engineering]
        }

    # --- Group requests (shared-group + group-requests wave) ----------------
    #
    # The dropdown's escape hatch for "my team's group isn't listed": a
    # request names a directory group `cfg.groups` doesn't know about yet
    # and asks an admin to add it, out of band, to org config. Filing one is
    # open to any signed-in caller (no owner/operator gate beyond the
    # app-wide IAP middleware every route already carries -- same "sign in
    # and that's it" reach chat/teach already have); reading the queue and
    # closing a request out are both admin-only.
    #
    # `_group_requests_admin_block` calls `_is_admin` DIRECTLY rather than
    # going through `_operator_block`/`_admin_operator_block`'s "empty
    # `operators` AND empty `admin_groups` = ungated" escape hatch those two
    # use elsewhere in this file for dev parity: a room that never got
    # around to naming an operator must still keep other people's names and
    # free-text notes closed by default, not fall open just because nobody
    # configured a gate yet. IAP-disabled rooms read `_is_admin` -> False
    # here for the identical reason, unlike every OTHER gate in this file's
    # "IAP off = trust the perimeter, there's no identity to check anyway"
    # convention -- there IS something worth protecting here even with no
    # perimeter at all.

    GROUP_REQUEST_NAME_MAX = 100

    def _group_requests_admin_block(request: Request) -> JSONResponse | None:
        if _is_admin(request):
            return None
        return JSONResponse(
            {"error": "Group requests are visible to room admins only."},
            status_code=403,
        )

    def _group_request_repr(req: GroupRequest) -> dict:
        return {
            "id": req.id,
            "group_name": req.group_name,
            "requested_by": req.requested_by,
            "note": req.note,
            "status": req.status,
            "decided_by": req.decided_by,
            "created_at": req.created_at.isoformat(),
            "decided_at": (
                req.decided_at.isoformat() if req.decided_at is not None else None
            ),
        }

    @app.post("/api/group-requests", status_code=201)
    def create_group_request(
        payload: GroupRequestCreate,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        name = payload.group_name.strip()
        if not name:
            return JSONResponse({"error": "name a group to request"}, status_code=422)
        if len(name) > GROUP_REQUEST_NAME_MAX:
            return JSONResponse(
                {
                    "error": (
                        f"group name is too long (max {GROUP_REQUEST_NAME_MAX} "
                        "characters)"
                    )
                },
                status_code=422,
            )

        # "Equivalent" is case-insensitive, trimmed-whitespace equality --
        # the same normalization `add_grant`'s own case-insensitive email
        # match already applies, just against `group_name` instead of an
        # email. Only PENDING requests count as a duplicate: a `done`/
        # `declined` row for the same name is history, not an open ask
        # still sitting in the queue, so a new request for it is legitimate
        # (an admin may have declined it once and reconsidered, or the
        # group was added and later renamed/removed and someone wants it
        # back).
        pending = (
            session.query(GroupRequest)
            .filter(GroupRequest.status == GROUP_REQUEST_PENDING)
            .all()
        )
        if any(existing.group_name.strip().lower() == name.lower() for existing in pending):
            return JSONResponse(
                {"error": f"a request for {name!r} is already pending"},
                status_code=409,
            )

        note = payload.note.strip() if payload.note else ""

        group_request = GroupRequest(
            group_name=name,
            requested_by=getattr(request.state, "iap_email", None),
            note=note or None,
            status=GROUP_REQUEST_PENDING,
        )
        session.add(group_request)
        session.commit()

        return JSONResponse(_group_request_repr(group_request), status_code=201)

    @app.get("/api/group-requests")
    def list_group_requests(
        request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        block = _group_requests_admin_block(request)
        if block is not None:
            return block

        requests_ = (
            session.query(GroupRequest)
            .order_by(GroupRequest.created_at.desc(), GroupRequest.id.desc())
            .all()
        )
        return JSONResponse({"requests": [_group_request_repr(r) for r in requests_]})

    def _decide_group_request(
        session: Session, request: Request, request_id: int, *, new_status: str, verb: str
    ) -> JSONResponse:
        block = _group_requests_admin_block(request)
        if block is not None:
            return block

        group_request = session.get(GroupRequest, request_id)
        if group_request is None:
            return JSONResponse(
                {"error": f"no such group request: {request_id}"}, status_code=404
            )

        if group_request.status != GROUP_REQUEST_PENDING:
            return JSONResponse(
                {
                    "error": (
                        f"group request is already {group_request.status}. "
                        f"Cannot {verb} it now."
                    )
                },
                status_code=409,
            )

        group_request.status = new_status
        group_request.decided_by = getattr(request.state, "iap_email", None)
        group_request.decided_at = datetime.now(UTC)
        session.commit()

        return JSONResponse(_group_request_repr(group_request))

    @app.post("/api/group-requests/{request_id}/done")
    def mark_group_request_done(
        request_id: int,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        """Closes the loop on kobo's own side after an admin has ALREADY
        added `group_name` to org config out of band -- this route never
        touches org config itself, it only records that the ask was acted
        on."""
        return _decide_group_request(
            session, request, request_id, new_status=GROUP_REQUEST_DONE, verb="close"
        )

    @app.post("/api/group-requests/{request_id}/declined")
    def decline_group_request(
        request_id: int,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        return _decide_group_request(
            session, request, request_id, new_status=GROUP_REQUEST_DECLINED, verb="decline"
        )

    # People search (access-tab refinement wave): a signed-in-only proxy of
    # the directory's own user search (kobo_api.groups.OktaPeopleSearch) --
    # no owner/operator gate beyond the app-wide IAP middleware every
    # `/api/*` route already carries (whoami's own "no verified identity to
    # gate against" escape hatch applies here too when IAP is off). `q` is
    # required and non-empty (`Query(..., min_length=1)`) -- an empty or
    # missing query answers the app-wide RequestValidationError handler's
    # plain 422, not a route-level check.
    #
    # Fail-soft, deliberately: `clients.people_search` absent (the room
    # never wired OKTA_API_TOKEN/okta_domain) and a live directory failure
    # (`search` returning `None`, kobo_api.groups' own fail-soft contract)
    # both read as the SAME `{"people": [], "unavailable": true}` -- this
    # route never answers 5xx over a directory hiccup, same posture every
    # other best-effort directory call in this room takes.
    @app.get("/api/people/search")
    def search_people(q: str = Query(..., min_length=1)) -> JSONResponse:
        clients: Clients | None = app.state.clients
        searcher = clients.people_search if clients is not None else None
        if searcher is None:
            return JSONResponse({"people": [], "unavailable": True})

        result = searcher.search(q)
        if result is None:
            return JSONResponse({"people": [], "unavailable": True})
        return JSONResponse({"people": result})

    # --- Org status (Task 8): read-only wiring visibility for the room ------

    @app.get("/api/org/status")
    def org_status() -> dict:
        cfg: Config = app.state.config
        clients: Clients | None = app.state.clients
        org: OrgState = app.state.org

        # Six checks, each individually try/excepted so a single unreachable
        # backend never takes the whole route down — the route itself always
        # answers 200, never a 500. Each check callable returns (ok, detail);
        # ids/labels/order stay exactly as-is.
        checks_to_run = [
            ("org-config", "Organization settings", lambda: _org_config_check(org)),
            ("skills-bucket", "Skills catalog storage", lambda: _bucket_check("skills-bucket", "Skills catalog storage", clients, cfg.skills_bucket)),
            ("personas-bucket", "Personas storage", lambda: _bucket_check("personas-bucket", "Personas storage", clients, cfg.personas_bucket)),
            ("skills-index", "Skills index", lambda: _skills_index_check(clients, cfg.skills_bucket)),
            ("personas-seeded", "Personas", lambda: _personas_seeded_check(clients, cfg.personas_bucket)),
            ("deploy-platform", "Deploy platform", lambda: _deploy_platform_check(clients, cfg.gojira_cluster)),
        ]

        checks = []
        for check_id, label, check_callable in checks_to_run:
            try:
                checks.append(check_callable())
            except Exception:
                log.warning("org status check %s failed", check_id, exc_info=True)
                checks.append({
                    "id": check_id,
                    "label": label,
                    "ok": False,
                    "detail": "something went wrong running this check",
                })

        return {
            "org": cfg.github_org if org.wired else None,
            "wired": org.wired,
            "checks": checks,
        }

    # --- Catalog search (Task 3): FTS5 over the bucket-published indexes ---

    @app.get("/api/teams")
    def get_teams(session: Session = Depends(get_session)) -> dict:  # noqa: B008
        # Persona-metadata plan Task 1: the skill-only, owner-derived
        # counterpart to /api/catalog/teams below (which spans both kinds).
        # No gate beyond the app-wide IAP middleware every /api/* route
        # already has, and the same TTL-refresh seam search_skills rides —
        # a newly-published owner shows up here without a manual
        # POST /api/catalogs/refresh kick.
        engine = session.get_bind()
        _maybe_refresh_catalogs(app, engine)
        return {"teams": distinct_skill_teams(engine), "default": "shared"}

    @app.get("/api/skills/search")
    def search_skills(
        q: str = "", team: str | None = None, session: Session = Depends(get_session)  # noqa: B008
    ) -> dict:
        engine = session.get_bind()
        _maybe_refresh_catalogs(app, engine)
        return {
            "results": [
                {
                    "id": e.id,
                    "name": e.name,
                    "description": e.description,
                    "team": e.team,
                    "tags": e.tags,
                    "sha": e.sha,
                }
                for e in catalog_search(engine, "skill", q, team=team)
            ]
        }

    # Recommendation keyword extraction (create-flow polish, 2026-08-28):
    # small and deliberately dumb -- lowercase words of 4+ letters, minus a
    # tiny stopword list, deduped in order, capped. The FTS does the real
    # ranking; this just picks what to ask it about.
    _RECOMMEND_STOPWORDS = frozenset(
        "this that with from your yours their they them have has been will "
        "would should could about into over under never always exactly "
        "before after every each when what where which whose ranks against "
        "judging the and for who are you all can use "
        # Persona-generic words: they describe every persona and select for
        # nothing -- live case: "A GCP expert" reduced to the single token
        # "expert", which literally matched the skill NAMED expert while
        # every gcp-* skill went unmatched.
        "expert experts specialist specialists helper helpers assistant "
        "assistants agent agents persona team work works working helps".split()
    )
    _RECOMMEND_TERM_CAP = 12
    _RECOMMEND_RESULTS = 5

    @app.get("/api/skills/recommended")
    def recommend_skills(  # noqa: B008
        who: str = "",
        what: str = "",
        session: Session = Depends(get_session),
    ) -> dict:
        """Top skills for a persona (create-flow polish, 2026-08-28): the
        persona's own text, keyword-extracted and OR-matched against the
        skills catalog -- one single-token ranked FTS query per keyword
        (both engines' query builders are implicit-AND, so OR semantics
        live here as a merge), scored by how many keywords hit each skill,
        first-hit rank as the tiebreak."""
        engine = session.get_bind()
        _maybe_refresh_catalogs(app, engine)
        # 3+ chars, NOT 4: this org's domain vocabulary is three letters
        # deep -- gcp, gke, iam, aws -- and dropping those was the live
        # failure mode (a GCP-expert persona recommended nothing GCP).
        words = re.findall(r"[a-z][a-z0-9-]{2,}", f"{who} {what}".lower())
        terms = []
        for word in words:
            if word not in _RECOMMEND_STOPWORDS and word not in terms:
                terms.append(word)
            if len(terms) >= _RECOMMEND_TERM_CAP:
                break
        scores: dict[str, list] = {}
        for term in terms:
            for rank, entry in enumerate(catalog_search(engine, "skill", term, limit=5)):
                key = f"{entry.team}/{entry.id}"
                if key not in scores:
                    scores[key] = [0, rank, entry]
                scores[key][0] += 1
        ranked = sorted(scores.values(), key=lambda v: (-v[0], v[1]))
        return {
            "results": [
                {
                    "id": e.id,
                    "name": e.name,
                    "description": e.description,
                    "team": e.team,
                    "tags": e.tags,
                    "sha": e.sha,
                }
                for _, _, e in ranked[:_RECOMMEND_RESULTS]
            ]
        }

    @app.get("/api/personas/search")
    def search_personas(
        q: str = "", team: str | None = None, session: Session = Depends(get_session)  # noqa: B008
    ) -> dict:
        engine = session.get_bind()
        _maybe_refresh_catalogs(app, engine)
        return {
            "results": [
                {
                    "id": e.id,
                    "name": e.name,
                    "description": e.description,
                    "team": e.team,
                    "tags": e.tags,
                    "builtin": e.builtin,
                }
                for e in catalog_search(engine, "persona", q, team=team)
            ]
        }

    @app.get("/api/catalog/teams")
    def get_catalog_teams(session: Session = Depends(get_session)) -> dict:  # noqa: B008
        engine = session.get_bind()
        _maybe_refresh_catalogs(app, engine)
        return {"teams": catalog_teams_query(engine)}

    @app.post("/api/catalogs/refresh")
    def refresh_catalogs(session: Session = Depends(get_session)) -> JSONResponse:  # noqa: B008
        clients = app.state.clients
        cfg: Config = app.state.config
        if not _catalogs_configured(clients, cfg):
            return JSONResponse(
                {"error": "catalogs aren't set up yet"}, status_code=503
            )

        # Fixed round 1's concurrency fix: this route used to hold
        # `catalog_lock` around its own ingest, believing that lock excluded
        # every ingest -- but the background refresh's ingest (spawned by a
        # stale hit) only ever ran outside `catalog_lock` (just its flag
        # flips were locked). Two concurrent `engine.begin()` writers, one
        # from each, could outrun sqlite's busy timeout and raise an
        # uncaught `sqlite3.OperationalError`. Rather than queue invisibly
        # behind an in-flight background refresh, an operator hammering this
        # route gets an honest 409 the moment one is already running --
        # `catalog_ingest_lock` below is what now actually excludes every
        # ingest (this route's own, the background thread's, and the
        # cold-start inline path's), so even outside this fast path a manual
        # refresh can never become a second concurrent ingest.
        with app.state.catalog_lock:
            if app.state.catalog_refreshing:
                return JSONResponse(
                    {"error": "a refresh is already running. Try again in a moment."},
                    status_code=409,
                )

        engine = session.get_bind()
        try:
            with app.state.catalog_ingest_lock:
                counts = ingest_indexes(engine, clients.storage, configured_catalogs(cfg))
            with app.state.catalog_lock:
                app.state.catalog_loaded_at = time.monotonic()
        except ClientError as exc:
            log.warning("catalog refresh failed: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=502)

        # HTTP response shape stays the plural skills/personas keys
        # regardless of `ingest_indexes`'s kind-keyed return — translated
        # here so a future catalog kind (counts.get("tool", 0), say) doesn't
        # have to change this route's contract at all.
        return JSONResponse(
            {"skills": counts.get("skill", 0), "personas": counts.get("persona", 0)}
        )

    # --- Persona builder (Task 4): fixed form -> deterministic template ---

    def _persona_team_gate(request: Request, team: str) -> JSONResponse | None:
        """Persona middle gate (persona-wave spec, operator ruling): team
        "shared" is the commons -- anyone files there and the resolver is
        never consulted, mirroring the teamless PersonaBuilder flow exactly.
        Any OTHER team requires the caller's resolver groups to contain that
        team exactly; operators are exempt; a missing identity, a missing
        resolver, or an unresolvable-right-now answer refuses (fail-closed).
        Deliberately NOT kobo_api.mcp.tools.write._require_team_membership:
        that gate is the marketplace axis (operator-only "shared") and its
        mutation pins must not churn here. Cross-referenced there.

        Unlike _operator_block/_admin_operator_block there is deliberately
        NO iap_audience dev-parity arm: operator ruling (persona wave,
        2026-08-23), named-team filing stays fail-closed even in an
        IAP-less room; the commons arm covers dev flows.
        """
        if team == "shared":
            return None
        if _is_admin(request):
            return None
        refusal = JSONResponse(
            {"error": f"you must be a member of team {team} to file a persona under it"},
            status_code=403,
        )
        email = (getattr(request.state, "iap_email", None) or "").lower()
        if not email:
            return refusal
        clients: Clients | None = request.app.state.clients
        resolver = clients.groups if clients is not None else None
        resolved = resolver.groups_for(email) if resolver is not None else None
        if resolved is None or team not in resolved:
            return refusal
        return None

    @app.post("/api/personas", status_code=201)
    def create_persona_route(
        payload: CreatePersonaRequest,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        cfg: Config = app.state.config
        clients = app.state.clients

        gate = _persona_team_gate(request, payload.team)
        if gate is not None:
            return gate

        if clients is None or clients.storage is None or not cfg.personas_bucket:
            return JSONResponse(
                {"error": "catalogs aren't set up yet"}, status_code=503
            )

        try:
            entry = create_persona(
                clients.storage,
                bucket=cfg.personas_bucket,
                fields=payload,
                team=payload.team,
            )
            # Ingest right away, with the FULL kind->bucket mapping (Task 4
            # controller decision #1) — NOT just {"persona": ...}, since
            # ingest_indexes does a full rebuild of every kind it's handed;
            # a persona-only ingest here would wipe the skills table clean.
            # create_persona itself never does this (see its docstring) —
            # it's storage-only, so this route is the one place that makes
            # a fresh persona instantly searchable.
            engine = session.get_bind()
            ingest_indexes(engine, clients.storage, configured_catalogs(cfg))
            app.state.catalog_loaded_at = time.monotonic()
        except PersonaError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        except PersonaExists:
            return JSONResponse(
                {"error": "a persona with that name already exists"}, status_code=409
            )
        except ClientError as exc:
            log.warning("persona create failed: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=502)

        return JSONResponse(
            {
                "id": entry.id,
                "name": entry.name,
                "description": entry.description,
                "team": entry.team,
                "tags": entry.tags,
                "builtin": entry.builtin,
            },
            status_code=201,
        )

    def _persona_archive_gate(request: Request, entry: dict) -> JSONResponse | None:
        """Who may archive/unarchive (hardening wave rulings): builtin and
        commons personas are operator-only (no ownership axis exists to
        check); a team persona takes a member of that team or an operator.
        The team arm reuses _persona_team_gate's fail-closed resolver
        logic by delegation, with archive-specific refusal copy."""
        if entry.get("builtin") or entry.get("team") == "shared":
            if _is_admin(request):
                return None
            return JSONResponse(
                {"error": "only a room operator can archive a persona filed under the commons"},
                status_code=403,
            )
        gate = _persona_team_gate(request, entry["team"])
        if gate is None:
            return None
        return JSONResponse(
            {
                "error": (
                    f"you must be a member of team {entry['team']} "
                    "to archive a persona filed under it"
                )
            },
            status_code=403,
        )

    def _set_archived_route(
        persona_id: str, request: Request, session: Session, archived: bool
    ) -> JSONResponse:
        cfg: Config = app.state.config
        clients = app.state.clients
        if clients is None or clients.storage is None or not cfg.personas_bucket:
            return JSONResponse({"error": "catalogs aren't set up yet"}, status_code=503)

        try:
            index = personas_read_index(clients.storage, cfg.personas_bucket, "")
        except ClientError as exc:
            # Same 502 as the write path below (minor 4 fix, hardening wave
            # review): this read used to sit outside any error handling, so
            # a storage blip here surfaced as an unhandled 500 instead of
            # the 502 this route advertises.
            log.warning("persona archive index read failed: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=502)
        entry = next((e for e in index if e["id"] == persona_id), None)
        if entry is None:
            return JSONResponse({"error": "no persona with that id"}, status_code=404)

        gate = _persona_archive_gate(request, entry)
        if gate is not None:
            return gate

        try:
            updated = set_persona_archived(
                clients.storage,
                bucket=cfg.personas_bucket,
                persona_id=persona_id,
                archived=archived,
            )
            # Same instant-ingest idiom as create_persona_route: full
            # kind->bucket mapping, so the flip is visible (or invisible)
            # in search immediately.
            engine = session.get_bind()
            ingest_indexes(engine, clients.storage, configured_catalogs(cfg))
            app.state.catalog_loaded_at = time.monotonic()
        except PersonaError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except ClientError as exc:
            log.warning("persona archive flip failed: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse(updated)

    @app.post("/api/personas/{persona_id}/archive")
    def archive_persona_route(
        persona_id: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        return _set_archived_route(persona_id, request, session, archived=True)

    @app.post("/api/personas/{persona_id}/unarchive")
    def unarchive_persona_route(
        persona_id: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        return _set_archived_route(persona_id, request, session, archived=False)

    # Declared before any GET /api/personas/{persona_id} that might arrive
    # later: distinct method+path from the archive/unarchive POSTs above
    # so there's no routing clash today, but a future single-persona GET
    # would need to sit AFTER this literal path, not before it, or FastAPI
    # would try to match "archived" as a {persona_id}.
    @app.get("/api/personas/archived")
    def archived_personas_route() -> JSONResponse:
        # Ungated read (hardening wave review, minor 8): deliberate parity
        # with GET /api/personas/search, which is also readable by any
        # authenticated caller regardless of team membership. The entries
        # here carry no fields beyond that search projection plus
        # "archived", so this is not a new leak class -- a decision, not an
        # omission.
        cfg: Config = app.state.config
        clients = app.state.clients
        if clients is None or clients.storage is None or not cfg.personas_bucket:
            return JSONResponse({"error": "catalogs aren't set up yet"}, status_code=503)
        try:
            index = personas_read_index(clients.storage, cfg.personas_bucket, "")
        except ClientError as exc:
            # Same 502 as the archive/unarchive route's sibling read
            # (minor 4 fix): a storage blip here must not surface as an
            # unhandled 500.
            log.warning("archived persona listing failed: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse(
            {"personas": [e for e in index if e.get("archived")]}
        )

    def _persona_archived_in_index(persona_id: str) -> bool:
        """True only when the bucket index carries this id with the
        archived flag set. Any storage trouble answers False -- the caller
        then gives the same unknown-persona answer as before this wave."""
        cfg: Config = app.state.config
        clients = app.state.clients
        if clients is None or clients.storage is None or not cfg.personas_bucket:
            return False
        try:
            index = personas_read_index(clients.storage, cfg.personas_bucket, "")
            entry = next((e for e in index if e["id"] == persona_id), None)
        except Exception:  # noqa: BLE001 -- failure path must stay quiet
            # minor 5 fix (hardening wave review): the entry lookup used to
            # sit outside this try, so a malformed index entry (missing
            # "id") raised KeyError straight through this create-agent
            # failure path instead of the docstring's promised False.
            return False
        return bool(entry and entry.get("archived"))

    @app.post("/api/personas/draft")
    def draft_persona_route(payload: PersonaDraftRequest) -> JSONResponse:
        """The Persona v3 "describe the job, approve the soul" draft step:
        turns `payload.job` (+ optional `payload.rules` hints) into a
        `{"name", "who_they_are", "what_they_do"}` draft for the v2 preview
        to edit. Always 200 — a generator failure (unconfigured `clients.
        name`, or a raised ClientError) never surfaces as an error here,
        just a full deterministic fallback instead
        (`kobo_api.persona_draft.finalize_draft`'s job); the room always
        drafts SOMETHING editable. Never touches storage/search/DB at
        all — nothing is created until the operator approves the preview
        and calls POST /api/personas, completely untouched by this route.
        """
        clients = app.state.clients

        raw: dict | None = None
        if clients is not None and clients.name is not None:
            try:
                raw = clients.name.draft(payload.job, payload.rules)
            except ClientError as exc:
                log.warning("persona draft generation failed: %s", exc)
                raw = None

        return JSONResponse(finalize_draft(raw, job=payload.job))

    @app.post("/api/personas/distill")
    def distill_persona_route(payload: PersonaDistillRequest) -> JSONResponse:
        """The persona-first station's other entry point (persona-first
        station addendum): the operator has already written (or uploaded)
        the FULL persona document, so this route reads it and pulls
        discovery metadata out in the background — a
        `{"name", "who_they_are", "purpose_tags"}` dict, deliberately no
        `what_they_do` (the submitted document already IS the persona
        body; the operator's own preview textarea holds it verbatim, never
        round-tripped through this route). Same "always 200, a generator
        failure just means a deterministic fallback instead" contract as
        `draft_persona_route` above
        (`kobo_api.persona_draft.finalize_distill`'s job) — the station
        never blocks on this call. Storage/search/DB untouched, same as
        the draft route: nothing is created until the operator approves
        the preview and calls POST /api/personas.
        """
        clients = app.state.clients

        raw: dict | None = None
        if clients is not None and clients.name is not None:
            try:
                raw = clients.name.distill(payload.body)
            except ClientError as exc:
                log.warning("persona distill generation failed: %s", exc)
                raw = None

        return JSONResponse(finalize_distill(raw, body=payload.body))

    @app.get("/api/availability")
    def availability(
        name: str, session: Session = Depends(get_session)  # noqa: B008
    ) -> dict:
        reason = validate_name(name)
        if reason is not None:
            return {"available": False, "reason": reason}

        existing = session.query(Agent).filter_by(name=name).first()
        if existing is not None:
            return {"available": False, "reason": "name taken"}

        return {"available": True, "reason": None}

    @app.post("/api/agents/name-suggestions")
    def name_suggestions(
        payload: NameSuggestionsRequest, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        clients = app.state.clients
        cfg: Config = app.state.config

        # This route carries no owner/operator gate beyond the app-wide IAP
        # middleware every route already has -- any signed-in caller may
        # name a persona_id here, resolved below, same org-wide-ungated
        # posture GET /api/personas/search already holds for the same
        # catalog. The exposure that buys a caller: three slug-shaped names
        # plus three 80-char reasons (SUGGESTION_COUNT, kobo_api.namegen),
        # derived from at most 2000 chars of that persona's own body sent to
        # the org-internal name-suggestion model. A decision, not an
        # omission.
        #
        # persona_id server-side resolution (create-identity, Task 3): when
        # it's set and resolves against the persona catalog, the persona's
        # own REAL body (the same bucket-backed text
        # `kobo_api.steps._resolve_persona_body` reads for a live agent's
        # capability generation, reused here rather than a second fetch
        # lane) replaces `what_they_do` entirely, and the catalog's own
        # purpose tags steer the generator too. A stale/unknown persona_id
        # (or any storage trouble) must never break naming — it just falls
        # back to whatever the client itself submitted, silently.
        what_they_do = payload.what_they_do
        persona_tags: list[str] | None = None
        if (
            payload.persona_id
            and clients is not None
            and clients.storage is not None
            and cfg.personas_bucket
        ):
            try:
                index = personas_read_index(clients.storage, cfg.personas_bucket, "")
                entry = next((e for e in index if e.get("id") == payload.persona_id), None)
                if entry is None:
                    raise PersonaError(f"no persona with id {payload.persona_id!r}")
                engine = session.get_bind()
                body = _resolve_persona_body(
                    cfg,
                    clients.storage,
                    engine,
                    SimpleNamespace(persona=payload.persona_id, name="{name}"),
                )
            except Exception as exc:  # noqa: BLE001 -- an unresolved/stale
                # persona_id (or any storage/index trouble reaching it,
                # e.g. a malformed entry) must never break naming; same
                # "quiet, broad except" idiom as _persona_archived_in_index
                # above, for the identical `e["id"]`-shaped hazard.
                log.warning(
                    "persona lookup for name suggestions failed (persona_id=%r): %s",
                    payload.persona_id, exc,
                )
            else:
                what_they_do = body[:2000]
                persona_tags = entry.get("tags") or None

        # The seed finalize_suggestions' fallback ordering derives from —
        # both submitted fields, joined by a separator that can't itself
        # appear in either (both are free text, so this is purely a
        # determinism input, never parsed back apart): same persona text,
        # same three names every time; a different persona text reliably
        # reorders (and usually reselects) the fallback pool. Computed
        # AFTER persona_id resolution above, so a resolved persona's real
        # body (not the client's thin what_they_do) is what actually seeds
        # it.
        persona_seed = f"{payload.who_they_are}\n{what_they_do}"

        # exclude (create-identity, Task 3): names already offered this
        # session, lowercased into the same drop-set finalize_suggestions
        # itself would otherwise have to normalize alone — handed to both
        # the generator (so it doesn't waste a candidate slot resuggesting
        # one) and the finalizer (which actually enforces the drop).
        exclude_set = {name.strip().lower() for name in payload.exclude}

        candidates: list[dict] = []
        if clients is not None and clients.name is not None:
            try:
                candidates = clients.name.suggest(
                    payload.who_they_are,
                    what_they_do,
                    tags=persona_tags,
                    exclude=sorted(exclude_set),
                )
            except ClientError as exc:
                # The room never fails to offer names (addendum) — a
                # generator failure just means every slot comes from the
                # curated fallback pool instead; never surfaced as an error
                # to the caller.
                log.warning("name suggestion generation failed: %s", exc)
                candidates = []

        taken = {row[0] for row in session.query(Agent.name).all()}

        suggestions = finalize_suggestions(
            candidates,
            taken=taken,
            reserved=RESERVED_NAMES,
            persona_seed=persona_seed,
            exclude=exclude_set,
            round_salt=str(payload.round),
        )

        if len(suggestions) < SUGGESTION_COUNT:
            # "Effectively never" (addendum): the curated pool is large
            # enough that only a pathological amount of prior taken/reserved
            # overlap gets here — the standard 502 body every other
            # backend-result failure in this app uses.
            log.warning(
                "name suggestions came back short (%d of %d) even after fallback",
                len(suggestions), SUGGESTION_COUNT,
            )
            return JSONResponse(
                {"error": "couldn't come up with names right now. Try again."},
                status_code=502,
            )

        return JSONResponse({"suggestions": suggestions})

    @app.post("/api/agents", status_code=201)
    def create_agent(
        payload: CreateAgentRequest, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        cfg: Config = app.state.config

        name_reason = validate_name(payload.name)
        if name_reason is not None:
            return JSONResponse(
                {"error": f"invalid name ({name_reason}): {payload.name!r}"}, status_code=422
            )

        # Persona-metadata plan (Task 2): team is a plain `str` on
        # CreateAgentRequest specifically so a shape mismatch lands here
        # (plain copy) instead of the generic RequestValidationError
        # message a `Field(pattern=...)` would trigger — see the field's
        # own docstring.
        if not TEAM_SLUG_RE.match(payload.team):
            return JSONResponse(
                {"error": "that doesn't look like a team name"}, status_code=422
            )

        # The gate both create and connect run (see `_group_choice_block`
        # for the whole rule, and for why it is one helper rather than
        # one copy per route).
        blocked = _group_choice_block(app, request, cfg, payload.group_name)
        if blocked is not None:
            return blocked

        cleaned_tags = clean_tags(payload.purpose_tags)
        if isinstance(cleaned_tags, str):
            return JSONResponse({"error": cleaned_tags}, status_code=422)

        # Bucket catalogs (Task 5): TTL-refresh via the same hook the search
        # routes use, then validate persona/skills against the FTS table —
        # never the old local-clone scanners. All of this must happen BEFORE
        # any Agent row is written.
        engine = session.get_bind()
        _maybe_refresh_catalogs(app, engine)

        if not configured_catalogs(cfg):
            # Same copy as the search/refresh routes' 503 — deliberate: an
            # unwired org (no catalog bucket named at all) means there is
            # nothing to validate against, and that's a setup problem, not a
            # bad request.
            return JSONResponse(
                {"error": "catalogs aren't set up yet"}, status_code=422
            )

        # Archetype create lane (team-guide spec 2026-09-14). Sits here on
        # purpose: AFTER the group validation + membership-consent block
        # above (so only a member of the picked team -- or an operator --
        # can deploy that team's guide, and the guard runs on the group the
        # caller actually PICKED, before this branch rewrites it), and
        # AFTER the catalog refresh (so the forced skill pick is resolved
        # against a fresh catalog), but BEFORE persona/skill resolution
        # below -- everything downstream reads `payload.*` and
        # `cleaned_tags` and runs completely unchanged.
        if payload.archetype is not None:
            if payload.archetype != ARCHETYPE_TEAM_GUIDE:
                return JSONResponse(
                    {"error": f"unknown archetype: {payload.archetype!r}"},
                    status_code=422,
                )

            # I4: name uniqueness is checked FIRST, before anything below
            # writes a persona -- otherwise the most likely user error (a
            # name that's already taken, caught a few hundred lines down at
            # the very same query) orphans a team-guide-<slug> persona in
            # the catalog with nothing left pointing at it. Same query and
            # same 409 body as that later pre-check (kept in place,
            # unreordered, for the non-archetype path).
            if session.query(Agent).filter_by(name=payload.name).first() is not None:
                return JSONResponse(
                    {"error": f"name taken: {payload.name!r}"}, status_code=409
                )

            # The PICKED group is the team this guide serves. Normalized to
            # its slug ONCE, here, via the exact fold `normalize_tags`
            # applies to any other tag (lowercase, internal whitespace to
            # hyphens) -- reused rather than reimplemented, so "SRE" and
            # "sre" (or "Data Eng" and "data-eng") always collapse to the
            # same slug. Every identity-bearing use below reads `slug`, not
            # the raw `group` -- the duplicate guard, the canonical tag, and
            # `team_guide_persona_id()` -- so two spellings of the same team
            # can never mint two guides or two persona ids. `group` (raw)
            # survives only for the rendered persona prose, where "Data
            # Eng" should still read like a name, not a slug.
            group = payload.group_name or SHARED_GROUP
            normalized_group = normalize_tags([group])
            slug = normalized_group[0] if normalized_group else ""
            # M1: reject before the slug is used as any part of a storage
            # key (the persona id, the canonical tag) -- empty (the raw
            # group folded away to nothing) or shaped unlike every other
            # persona id (kobo_api.personas.PERSONA_ID_RE, the same
            # DNS-ish lowercase-start shape) both fail closed here rather
            # than reaching `upsert_persona` with something like
            # "../../pwned".
            if not slug or not PERSONA_ID_RE.fullmatch(slug):
                return JSONResponse(
                    {"error": "that doesn't look like a group name"},
                    status_code=422,
                )
            if payload.voice is not None and len(payload.voice) > 120:
                return JSONResponse(
                    {"error": "voice should be a short name, not a biography"},
                    status_code=422,
                )
            guide_persona_id = team_guide_persona_id(slug)
            # One guide per team: the team identity lives in the TAG PAIR
            # (team-guide + slug), because the agent's ACL namespace is
            # forced to the commons below. I3: the tag pair alone
            # false-positives on any ordinary agent that merely happens to
            # carry both tags (e.g. a human hand-typed "team-guide" and
            # "sre" into an ordinary agent's purpose_tags) -- requiring the
            # persona match too pins the guard to an actual guide, never a
            # tag coincidence.
            for other in session.query(Agent):
                if other.persona != guide_persona_id:
                    continue
                other_tags = decode_tags(other.purpose_tags)
                if TEAM_GUIDE_TAG in other_tags and slug in other_tags:
                    return JSONResponse(
                        {
                            "error": f"{slug} already has a team guide",
                            "existing": other.name,
                        },
                        status_code=409,
                    )

            archetype_clients = app.state.clients
            if (
                archetype_clients is None
                or archetype_clients.storage is None
                or not cfg.personas_bucket
            ):
                return JSONResponse(
                    {"error": "catalogs aren't set up yet"}, status_code=503
                )
            try:
                # Stored as an ordinary room persona so `_resolve_persona_body`
                # and everything downstream never learn archetypes exist.
                # Overwrite-safe (`upsert_persona`, not `create_persona`):
                # re-creating a guide after a destroy re-renders in place.
                upsert_persona(
                    archetype_clients.storage,
                    bucket=cfg.personas_bucket,
                    id=guide_persona_id,
                    name=f"{group} team guide",
                    description=f"The informational guide for the {group} team.",
                    body=render_team_guide(payload.name, group, payload.voice),
                    team=slug,
                    tags=[TEAM_GUIDE_TAG],
                    creator="kobo",
                )
                # Same instant-ingest idiom as create_persona_route: the FULL
                # kind->bucket mapping (a persona-only ingest would wipe the
                # skills table), so the persona this route just wrote is
                # resolvable by the `get_entry` lookup a few lines below.
                ingest_indexes(engine, archetype_clients.storage, configured_catalogs(cfg))
                app.state.catalog_loaded_at = time.monotonic()
            except PersonaError as exc:
                return JSONResponse({"error": str(exc)}, status_code=422)
            except ClientError as exc:
                log.warning("team-guide persona write failed: %s", exc)
                return JSONResponse({"error": str(exc)}, status_code=502)

            # Force the surface: the rest of the route reads payload.*.
            payload.persona = guide_persona_id
            # Shared visibility, mechanically: the guide lives in the commons
            # namespace so every team's humans (and agents) can find and chat
            # with it. Its team identity is the tag pair + persona, not its
            # ACL namespace.
            payload.group_name = SHARED_GROUP
            payload.team = SHARED_GROUP
            # Attach the packaged guide skill when the marketplace has it;
            # skip silently when it hasn't published it yet (a kiosk without
            # the skill still has its constitution from the persona). Forced
            # BEFORE the resolution loop below, so the loop resolves this pick
            # exactly like a client's own.
            #
            # M2: a bare `team=None` lookup ("match any team", per get_entry's
            # own docstring) is genuinely nondeterministic here -- a skill id
            # is only unique WITHIN a team (SkillPick's own docstring), so
            # two teams each publishing their own "team-guide" skill would
            # leave `.first()` picking whichever row an unordered SQL scan
            # happens to return first, silently, and that pick could shift
            # across catalog rebuilds. Every packaged/marketplace skill in
            # this codebase is published under the shared team (see
            # tests/conftest.py's own `shared/word-games` fixture), so that's
            # the deterministic home to prefer; `team=None` remains only as
            # the fallback for a room where it's been published somewhere
            # else instead (still whatever single team has it, in the common
            # case where only one does).
            guide_skill = get_entry(
                engine, "skill", TEAM_GUIDE_TAG, team=SHARED_GROUP
            ) or get_entry(engine, "skill", TEAM_GUIDE_TAG, team=None)
            payload.skills = (
                [SkillPick(id=guide_skill.id, team=guide_skill.team)]
                if guide_skill is not None
                else []
            )
            # Canonical tags FIRST, then the caller's own (already normalized
            # and cap-checked above). Truncated rather than re-rejected: the
            # cap's usual "never silently drop a tag a human typed" posture
            # aims at the caller's list, and it would be perverse to 422 a
            # caller over two tags the ROUTE added -- so the overflow this
            # branch itself creates is the route's to absorb. The pair leads,
            # so it always survives: the duplicate guard above and every
            # "which team does this guide serve" reader depend on it.
            forced_tags = clean_tags(
                [TEAM_GUIDE_TAG, slug, *cleaned_tags][:MAX_CREATE_TAGS]
            )
            # Unreachable as long as the slice above stands (normalization
            # only ever shrinks a list) -- kept so the union return is
            # handled at every call site rather than assumed away.
            if isinstance(forced_tags, str):
                return JSONResponse({"error": forced_tags}, status_code=422)
            cleaned_tags = forced_tags
            # M3: the kiosk always ships the baseline guardrail tools and
            # the org-default model -- a client-supplied opt-out or model
            # override never reaches an archetype agent.
            payload.skip_baseline = False
            payload.model = None

        # Personas are org-wide — the id alone is unique across the whole
        # personas index, so no team is required (or known) to look one up.
        persona_entry = get_entry(engine, "persona", payload.persona, team=None)
        if persona_entry is None:
            # Ingest drops archived entries, so an archived pick lands here
            # exactly like an unknown one. One storage read on this failure
            # arm buys the honest answer (the persona catalog is
            # room-readable; no anti-enumeration fold applies).
            if _persona_archived_in_index(payload.persona):
                return JSONResponse(
                    {"error": f"persona {payload.persona!r} is archived"}, status_code=422
                )
            return JSONResponse(
                {"error": f"unknown persona: {payload.persona!r}"}, status_code=422
            )

        resolved_skills = []
        for pick in payload.skills:
            skill_entry = get_entry(engine, "skill", pick.id, pick.team)
            if skill_entry is None:
                return JSONResponse(
                    {
                        "error": (
                            f"unknown skill: {pick.id!r} in team {pick.team!r}"
                        )
                    },
                    status_code=422,
                )
            resolved_skills.append((pick, skill_entry))

        model_ids = {m.id for m in cfg.models}
        if payload.model is not None and payload.model not in model_ids:
            return JSONResponse(
                {"error": f"unknown model: {payload.model!r}"}, status_code=422
            )
        model_id = payload.model if payload.model is not None else cfg.models[0].id

        # Checked last (a DB round-trip) rather than first: a malformed request
        # is a 422 regardless of whether the name happens to collide too.
        existing = session.query(Agent).filter_by(name=payload.name).first()
        if existing is not None:
            return JSONResponse({"error": f"name taken: {payload.name!r}"}, status_code=409)

        # Access control (Task A2): the verified IAP email at create time is
        # this agent's owner. `getattr` with a `None` default covers both
        # "IAP disabled entirely" and "IAP on, assertion carried no email
        # claim" the same way `teach`'s `taught_by` does above -- never
        # raises, just omits ownership rather than faking an identity.
        # `group_name` is stored verbatim-if-provided in this phase (no
        # validation yet; see `CreateAgentRequest.group_name`'s own
        # docstring).
        created_by = getattr(request.state, "iap_email", None)

        agent = Agent(
            name=payload.name,
            persona=payload.persona,
            team=payload.team,
            purpose_tags=encode_tags(cleaned_tags),
            model_id=model_id,
            # No `runtime_image` here, deliberately: every agent builds on
            # the org default, and a NULL column keeps following that pin
            # as it moves. Only `PATCH /api/agents/{name}/runtime` fills it.

            # Archetype marker (team-guide spec 2026-09-14, Task 2b). Read
            # straight off the payload, exactly as `payload.persona`/
            # `payload.team` above are: the archetype branch far above
            # either rewrote everything around it and left this field
            # alone, or returned 422 for an unknown value -- so by the time
            # the row is built this is None or the one archetype that
            # exists, never an unvalidated string. The composer reads it to
            # compose a kiosk with no action tools at all.
            archetype=payload.archetype,
            skills=[
                {"name": pick.id, "team": pick.team, "sha": entry.sha}
                for pick, entry in resolved_skills
            ],
            status=AgentStatus.PENDING,
            created_by=created_by,
            group_name=payload.group_name,
            skip_baseline=payload.skip_baseline,
            # Born in flight, so born with a heartbeat: PENDING is a swept
            # status (kobo_api.provisioner.sweep_interrupted), and leaning
            # on the NULL-heartbeat branch's created_at grace would make a
            # new agent's safety depend on a rule written for rows that
            # predate the column.
            run_heartbeat_at=naive_utc(datetime.now(UTC)),
        )
        session.add(agent)
        try:
            session.commit()
        except IntegrityError:
            # The pre-check above is read-then-act, not a lock: a second
            # request for the same name can pass it and lose the race here
            # instead, discovering the collision only when the unique
            # constraint fires on commit. Map that to the same 409 the
            # pre-check returns rather than letting the raw IntegrityError
            # surface as a 500 — same idiom as retry_agent's FAILED gate,
            # just enforced by the DB instead of a status column.
            session.rollback()
            return JSONResponse({"error": f"name taken: {payload.name!r}"}, status_code=409)

        # Capture the just-inserted status into a local BEFORE starting the
        # pipeline thread and use only that in the response below. The
        # pipeline's own session (opened fresh in run_pipeline's daemon
        # thread) flips the agent to PROVISIONING almost immediately — the
        # gojira lesson this mirrors is that reading status again afterward
        # (a fresh query, a refresh, anything that re-hits the row) risks
        # racing that flip and reporting the wrong status for a 201 that's
        # supposed to mean "just accepted, nothing has run yet".
        status = agent.status
        agent_id = agent.id

        try:
            run_pipeline(
                request.app.state.session_factory,
                agent_id,
                config=cfg,
                clients=app.state.clients,
                steps=pipeline_for(agent),
            )
        except Exception as exc:
            # Broad by design: a request boundary, not a specific expected
            # failure mode (this repo's ruff config enables no BLE /
            # flake8-blind-except rules — there's no lint exemption being
            # invoked here) - the pipeline thread never got started (or never
            # got the chance to run), so nothing will ever move this agent
            # out of PENDING — land it in FAILED directly rather than leaving
            # it wedged identically to the restart-mid-provision case
            # `sweep_interrupted` covers (this is that same wedge, just
            # triggered by a `Thread.start()`/launch failure instead of a
            # process restart). `_fail_launch` does the actual recovery (see
            # its docstring for why it needs a fresh session rather than this
            # one) and always returns the 503 below, never a raw 500.
            log.exception(
                "create_agent: failed to start provisioning for agent %s", agent_id
            )
            return _fail_launch(request.app.state.session_factory, session, agent_id, exc)

        return JSONResponse({"name": payload.name, "status": status}, status_code=201)

    # The repository field's typeahead (kobo_api/repo_search.py). Signed-in
    # only, which is the app-wide IAP middleware every `/api/*` route
    # already carries.
    #
    # Not group-gated, unlike the scan next door, for two reasons. The
    # group is chosen AFTER the repository on this form, so a gate here
    # would refuse the first field for an answer the person has not given
    # yet. And there is far less to gate: the scan reads what is INSIDE a
    # private repository, while this only says that a name the caller was
    # already typing belongs to something the room's App can reach.
    #
    # What bounds it instead is the pair below. The prefix rule
    # (`repo_search._rank`) means a typed query only ever names
    # repositories somebody was already typing -- there is no query that
    # returns the organisation -- and the rate limit means nobody walks the
    # alphabet through it anyway. The empty query is bounded by being
    # short: eight rows and a count, which says the room reaches your work
    # without being a listing of it.
    @app.get("/api/connect/repos")
    def list_connect_repos(
        request: Request, q: str = Query("", max_length=MAX_QUERY_CHARS)
    ) -> JSONResponse:
        searching = bool(q.strip())
        if searching and not rate_limit.check(
            _scan_caller(request), limit=REPO_SEARCH_LIMIT, bucket=REPO_SEARCH_BUCKET
        ):
            return JSONResponse(
                {"error": "that is a lot of searching. Wait a minute"}, status_code=429
            )
        # The empty query is deliberately NOT counted. It is one answer
        # per focus, read out of a listing the warmer already holds, so it
        # costs the room nothing and must not eat the ceiling a person
        # needs for the typing that follows.

        clients: Clients | None = app.state.clients
        index: RepoIndex | None = app.state.repo_index
        if clients is None or clients.github is None or index is None:
            return JSONResponse({"error": GITHUB_UNREACHABLE}, status_code=503)

        try:
            matches, truncated, total = index.search(q)
        except ClientError as exc:
            # The client's own text never reaches the caller (same shape as
            # the scan's own backend failures next door).
            log.warning("repository search could not list the installation: %s", exc)
            return JSONResponse({"error": REPOS_UNREADABLE}, status_code=502)

        return JSONResponse(
            {
                # The name and one word about it, which is all the field
                # shows. The default branch is not sent: nothing on this
                # form uses it, and the connect route reads GitHub's own
                # answer for itself rather than a browser's copy of it.
                "repos": [
                    {"full_name": repo.full_name, "private": repo.private}
                    for repo in matches
                ],
                "truncated": truncated,
                # On every answer, not only the empty one: the field's
                # footer says how many repositories the room reaches, and
                # it should not have to ask a second time to learn it.
                "total": total,
            }
        )

    @app.post("/api/connect/scan")
    def scan_connect_repo(
        payload: ScanRepoRequest, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        """What the room can honestly say about a repository before anyone
        commits to it (connected-agents design 2026-09-21).

        Read-only: four GitHub reads and one database question, no clone and
        no row. The connect route runs the same scan again for itself rather
        than trusting the answer the browser is holding.

        Gated and counted even so. It reads PRIVATE repositories with the
        room's own credentials, which makes it an oracle over the
        organisation for anyone who can call it: the same group gate as the
        connect, because a caller who could not connect has no business
        asking what is in one, and a small per-caller ceiling, because a
        form fills itself in with a handful of scans and nothing legitimate
        walks the organisation with them.
        """
        cfg: Config = app.state.config

        blocked = _connect_fields_block(payload)
        if blocked is not None:
            return blocked

        blocked = _group_choice_block(app, request, cfg, payload.group_name)
        if blocked is not None:
            return blocked

        if not rate_limit.check(
            _scan_caller(request), limit=SCAN_LIMIT, bucket=SCAN_BUCKET
        ):
            return JSONResponse(
                {"error": "that is a lot of scans. Wait a minute"}, status_code=429
            )

        clients: Clients | None = app.state.clients
        if clients is None or clients.github is None:
            return JSONResponse(
                {"error": GITHUB_UNREACHABLE}, status_code=503
            )

        try:
            report = scan_repo(
                clients.github,
                clients.gojira,
                session,
                cfg,
                repo=payload.repo,
                name=payload.name,
                run_mode=payload.run_mode,
            )
        except ClientError as exc:
            # GitHub being unreachable is not a finding about the
            # repository, and a report saying "nothing wrong" would be a
            # lie. The client's own text never reaches the caller (same
            # shape as `generate_avatar`'s backend failures).
            log.warning("connect scan could not read %s: %s", payload.repo, exc)
            return JSONResponse({"error": REPO_UNREADABLE}, status_code=502)

        return JSONResponse(_scan_report_body(report))

    @app.post("/api/agents/connect", status_code=201)
    def connect_agent(
        payload: ConnectAgentRequest, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        """Take on a repository a team already owns.

        Same order as create, and for the same reasons: everything that can
        be answered from the request alone, then the group gate, then the
        work (here, the scan), and the name collision last because it costs
        a database round trip and a malformed request is a 422 whether or
        not the name happens to collide too.

        The scan runs here as well as in the form because a report the
        browser holds is a claim about a repository some minutes ago:
        access can be withdrawn, a Dockerfile can be deleted, a repository
        can be connected to the deploy platform by somebody else. The
        refusal carries every blocker's code AND its sentence, so the form
        can say why without scanning a second time.

        THE ROOM CANNOT CHECK WHO OWNS THE REPOSITORY. Its GitHub App can
        write to every repository in the organisation it is installed on,
        and the token it holds cannot see who the caller is on GitHub, so
        there is no honest check to make here -- a collaborator lookup
        would be answering a question about the App, not about the caller.
        What stands in its place is said plainly rather than implied: the
        group gate above, `confirm_repo_owner` (the caller's word, which
        the form asks for as a checkbox), the sentence the scan report
        carries, and the WARNING line below naming who connected what.
        An operator is exempt from the confirmation, the same standing
        every other operator gate in this file grants.
        """
        cfg: Config = app.state.config

        blocked = _connect_fields_block(payload)
        if blocked is not None:
            return blocked

        secret_names = _clean_secret_names(payload.secret_names)
        if isinstance(secret_names, str):
            return JSONResponse({"error": secret_names}, status_code=422)

        blocked = _group_choice_block(app, request, cfg, payload.group_name)
        if blocked is not None:
            return blocked

        if not payload.confirm_repo_owner and not _is_admin(request):
            # Asked before any read: a caller who has not answered for the
            # repository should not have the room looking inside it.
            return JSONResponse({"error": REPO_OWNER_REFUSAL}, status_code=422)

        clients: Clients | None = app.state.clients
        if clients is None or clients.github is None:
            return JSONResponse(
                {"error": GITHUB_UNREACHABLE}, status_code=503
            )

        try:
            report = scan_repo(
                clients.github,
                clients.gojira,
                session,
                cfg,
                repo=payload.repo,
                name=payload.name,
                run_mode=payload.run_mode,
            )
        except ClientError as exc:
            log.warning("connect could not read %s: %s", payload.repo, exc)
            return JSONResponse({"error": REPO_UNREADABLE}, status_code=502)

        # GitHub's own spelling, so one repository cannot become two rows
        # apart from each other's case.
        repo = report.repo
        # A taken name is the one blocker this route does not answer with,
        # because it already has an answer for it: the 409 below, the same
        # one create gives, in the same words. The scan still reports it --
        # the form needs it before anyone presses Connect.
        repo_blockers = [
            finding for finding in report.blockers if finding.code != NAME_TAKEN
        ]
        if repo_blockers:
            return JSONResponse(
                {
                    "error": "this repository is not ready to connect yet",
                    "blockers": _findings_body(repo_blockers),
                },
                status_code=422,
            )

        existing = session.query(Agent).filter_by(name=payload.name).first()
        if existing is not None:
            return JSONResponse({"error": f"name taken: {payload.name!r}"}, status_code=409)

        created_by = getattr(request.state, "iap_email", None)

        agent = Agent(
            name=payload.name,
            # No persona, no skills, no team catalog and no purpose tags:
            # none of them describes a repository the room did not compose,
            # and every one of them is hidden on a connected agent's page.
            persona=CONNECTED_PERSONA,
            team=CONNECTED_TEAM,
            # Model access by default, the same as a composed agent
            # (operator ruling 2026-09-21). `Config.models` carries at
            # least one entry by construction, so there is always a room
            # default to record.
            model_id=cfg.models[0].id,
            skills=[],
            kind=KIND_CONNECTED,
            run_mode=payload.run_mode,
            secret_names=secret_names,
            # The route records the repository; `ConnectStep` checks it and
            # creates nothing.
            repo_full_name=repo,
            status=AgentStatus.PENDING,
            created_by=created_by,
            group_name=payload.group_name,
            run_heartbeat_at=naive_utc(datetime.now(UTC)),
        )
        session.add(agent)
        try:
            session.commit()
        except IntegrityError:
            # Same read-then-act race the create route has, answered the
            # same way: the unique constraint is the real gate.
            session.rollback()
            return JSONResponse({"error": f"name taken: {payload.name!r}"}, status_code=409)

        # WARNING rather than INFO because INFO does not reach the pod logs
        # here (`kobo_api.org`'s `_disclose_overrides` records the same
        # finding). Arbitrary code with the room's model access is the risk
        # this whole feature carries, and who took it on, for which
        # repository, is the part of that a log can answer later.
        log.warning(
            "connect: %s connected %s as agent %s",
            created_by or "an unidentified caller", repo, agent.name,
        )

        status = agent.status
        agent_id = agent.id

        try:
            run_pipeline(
                request.app.state.session_factory,
                agent_id,
                config=cfg,
                clients=clients,
                steps=pipeline_for(agent),
            )
        except Exception as exc:
            # Same launch-failure recovery as create: nothing will ever move
            # this agent out of PENDING, so it lands in FAILED here rather
            # than wedged.
            log.exception(
                "connect_agent: failed to start provisioning for agent %s", agent_id
            )
            return _fail_launch(request.app.state.session_factory, session, agent_id, exc)

        return JSONResponse({"name": payload.name, "status": status}, status_code=201)

    @app.post("/api/agents/{name}/convert-to-connected")
    def convert_to_connected(
        name: str,
        payload: ConvertToConnectedRequest,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        """Say out loud that the room no longer edits this agent.

        For an agent the room composed whose repository has since stopped
        running the room's runtime: a team took the code somewhere of its
        own, and every tab that clones and pushes is now a lie the page
        keeps telling. This is how such an agent gets an honest page
        without being destroyed and recreated (design 2026-09-21,
        "Converting an existing agent").

        It changes two columns and retires one directory entry. It does
        NOT redeploy, rebuild, or touch the repository in any way: what is
        running keeps running, under the same identity, from the same
        image. The agent's own history stays too -- its steps, its
        learnings, its Slack record -- because every one of them really
        happened; what changes is what the room will do NEXT, which is
        nothing. `secret_names` is left as it is for the same reason: the
        deployment is not being rewritten, so the Secrets it already
        mounts are not the room's to edit here.

        Operator-only, and rare. There is no MCP tool for it: an agent
        cannot ask to be cut loose, and a person doing this has the page
        in front of them.

        One way only. The room cannot convert back, because it has no way
        to know whether a bundle it stopped maintaining is still there,
        and a half-composed agent is worse than either kind. The confirm
        copy on the page says so.
        """
        cfg: Config = app.state.config

        # Gate FIRST, before the agent even exists check: info-safe, the
        # same discipline destroy's own gate keeps.
        block = _convert_operator_block(request, cfg)
        if block is not None:
            return block

        if payload.run_mode not in RUN_MODES:
            return JSONResponse({"error": RUN_MODE_REFUSAL}, status_code=422)
        if payload.run_mode == RUN_MODE_HEADLESS:
            # Not "the room has no class for it" (the connect route's
            # reason, `HEADLESS_UNAVAILABLE`): this one is refused even in a
            # room that runs headless agents happily. An agent the room
            # composed serves HTTP by construction, and this route
            # redeploys nothing, so a headless answer here would sit in the
            # column doing nothing until some later retry re-onboarded the
            # app under a class with no probes -- a wrong answer that bites
            # weeks after the person who gave it has forgotten giving it.
            return JSONResponse({"error": CONVERT_HEADLESS_REFUSAL}, status_code=422)

        visible = _visible_agent_or_404(session, request, name)
        if isinstance(visible, JSONResponse):
            return visible

        # Re-read the row under a lock before deciding anything about it.
        # The status checks below are the whole safety of this route, and
        # they are checked against a row another request (a retry launching
        # a composed pipeline, a destroy) may be changing right now. The
        # lock and the write are one transaction, so a status that passes
        # here cannot have moved by the time the kind lands.
        #
        # `populate_existing` because the identity map already holds this
        # row from the visibility read above, and a plain re-query would
        # hand back those same stale attributes rather than the locked
        # read's. On SQLite `with_for_update` compiles to nothing at all --
        # that is accepted (a local room has one writer), and the lock is
        # here for the Postgres deployments where it does hold.
        agent = (
            session.query(Agent)
            .filter(Agent.name == visible.name)
            .populate_existing()
            .with_for_update()
            .first()
        )
        if agent is None:
            # Destroyed between the two reads.
            return JSONResponse({"error": f"no such agent: {name!r}"}, status_code=404)

        if is_connected(agent):
            return JSONResponse(
                {"error": ALREADY_CONNECTED_REFUSAL}, status_code=409
            )
        if agent.status in CONVERT_REFUSED_STATUSES:
            # A provisioning or destroying agent has a thread of its own
            # holding this row, and both of those read the kind as they go
            # (`pipeline_for`, the destroy ledger's repo and directory
            # steps): changing it underneath them would fork a run halfway
            # through. READY and FAILED are both settled.
            return JSONResponse(
                {"error": CONVERT_IN_FLIGHT_REFUSAL}, status_code=409
            )

        group_name = agent.group_name
        agent.kind = KIND_CONNECTED
        agent.run_mode = payload.run_mode
        session.commit()

        caller = getattr(request.state, "iap_email", None)
        # WARNING for the same reason the connect's own line is (INFO does
        # not reach the pod logs here): this is the one record that an
        # agent's page went quiet on purpose rather than by a bug.
        log.warning(
            "convert: %s converted agent %s to connected",
            caller or "an unidentified caller",
            agent.name,
        )

        clients: Clients | None = app.state.clients
        if (
            cabal_enabled(cfg)
            and clients is not None
            and clients.cabal is not None
        ):
            try:
                # Broad on purpose, exactly as the destroy ledger's own
                # directory step is: a raw transport failure never reaches
                # `CabalError`, and the row has already committed. A stale
                # directory entry is resolved by a later sync; holding a
                # finished convert hostage to the switchboard is not.
                retire_directory(clients.cabal, agent)
            except Exception:
                log.warning(
                    "convert: could not retire the directory entry for agent %s",
                    agent.name,
                    exc_info=True,
                )

        # Unplug it from its colleagues, both halves. `allowed_pairs` now
        # leaves connected agents out of the set entirely, so this push is
        # what actually takes away the dispatch rights in both directions;
        # `refresh_colleague_rosters` then hands every agent that had a
        # line naming this one a roster without it, so nobody is left
        # offering a colleague that cannot be reached. Both are fail-soft
        # and neither writes to a repository -- the roster refresh is the
        # relay-only fast lane, and every agent it delivers to is a
        # composed agent the room owns.
        #
        # `group_name` was captured before the write for the same reason
        # the destroy path captures it: the question is who could see this
        # agent, which is asked of the standing it is leaving.
        _sync_dispatch_pairs(session)
        if clients is not None:
            refresh_colleague_rosters(
                clients, cfg, session, name=agent.name, group_name=group_name
            )

        return JSONResponse(
            {"name": agent.name, "kind": agent.kind, "run_mode": agent.run_mode}
        )

    @app.get("/api/agents")
    def list_agents(request: Request, session: Session = Depends(get_session)) -> dict:  # noqa: B008
        cfg: Config = app.state.config
        # Access control (Task B2): "each person sees their own aisle" --
        # `_visible_agents` is the single filter every visibility-aware
        # route in this file shares (see its own docstring for the full
        # branch table). `groups_unavailable` rides alongside the list
        # itself so the UI can render the room's honest banner copy without
        # a second round trip.
        agents, groups_unavailable = _visible_agents(session, request)
        return {
            "agents": [
                {
                    "name": a.name,
                    "status": a.status,
                    "team": a.team,
                    "model": a.model_id,
                    "model_label": _model_label(cfg, a.model_id),
                    "created_at": a.created_at.isoformat(),
                    "has_avatar": a.avatar_png is not None,
                    # Owner-or-operator gate + "Made by <email>" (access-
                    # control Task A3): both columns are nullable (Task A2)
                    # and simply pass through as `None` for a pre-A2 or
                    # IAP-disabled-at-create-time row -- no default, no
                    # placeholder string.
                    "created_by": a.created_by,
                    "group_name": a.group_name,
                    # Team-guide UI (team-guide spec 2026-09-14, Task 5): the
                    # create modal's "deploy your team's guide" pre-check
                    # needs to tell, from the list alone, whether a picked
                    # group already has one -- `archetype` alone can't say
                    # WHICH team's guide a row is (every guide's own
                    # `group_name` is forced to the commons, never the
                    # team), so `purpose_tags` rides along too: the tag
                    # pair (`TEAM_GUIDE_TAG` + the team's slug) is the only
                    # thing that actually identifies it. Both are already
                    # loaded on `a` with no extra query.
                    "archetype": a.archetype,
                    "purpose_tags": decode_tags(a.purpose_tags),
                    # Connected agents (design 2026-09-21): the dashboard
                    # card carries the marker, so the list needs it for
                    # the same reason `archetype` above is here -- a
                    # per-agent fetch to learn it would be one round trip
                    # per card.
                    "kind": a.kind,
                }
                for a in agents
            ],
            "groups_unavailable": groups_unavailable,
        }

    @app.get("/api/agents/{name}")
    def get_agent(
        name: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        cfg: Config = app.state.config
        agent = _visible_agent_or_404(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent

        steps = [
            {
                "name": step.name,
                "label": STEP_LABELS.get(step.name) or DESTROY_STEP_LABELS.get(step.name, step.name),
                "status": step.status,
                "error": step.error,
            }
            for step in agent.steps
        ]

        # Two-lane Slack (2026-08-17): always present now (the UI tab is
        # unconditional) — `channels`/`state` reflect the saved list and
        # wiring status regardless of whether an app has been created yet;
        # `app_id` alone stays None until create_slack_app runs.
        channels = decode_channels(agent.slack_channels)
        slack = {
            "app_id": agent.slack_app_id,
            "channels": channels,
            "state": derive_state(channels, wired=agent.slack_wired),
        }

        return JSONResponse(
            {
                "name": agent.name,
                "persona": agent.persona,
                "team": agent.team,
                "purpose_tags": decode_tags(agent.purpose_tags),
                "capability": agent.capability,
                "model": agent.model_id,
                "model_label": _model_label(cfg, agent.model_id),
                "skills": agent.skills,
                "status": agent.status,
                "repo_full_name": agent.repo_full_name,
                "error": agent.error,
                "created_at": agent.created_at.isoformat(),
                "steps": steps,
                "first_reply": agent.first_reply,
                "slack": slack,
                "destroy_visible": _operator_block(request, cfg) is None,
                "has_avatar": agent.avatar_png is not None,
                "created_by": agent.created_by,
                "group_name": agent.group_name,
                "archetype": agent.archetype,
                # Connected agents (design 2026-09-21): which tabs the page
                # may offer at all is a property of the kind, so the page
                # reads it from the same fetch that draws the header.
                "kind": agent.kind,
                "pending_learnings": session.query(AgentLearning)
                .filter_by(agent_name=agent.name, status=LEARNING_PENDING)
                .count(),
                "can_decide_learnings": _decider_or_403(agent, request, session) is None,
            }
        )

    @app.post("/api/agents/{name}/chat")
    def chat_with_agent(
        name: str, payload: ChatRequest, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        agent = _visible_agent_or_404(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent

        # Chat is the one surface the connected-agents design left open by
        # oversight rather than on purpose. The room never made a connected
        # agent's chat wiring (no colleague pass, no directory entry, and
        # the relay has no inbound route to it), so every attempt walked all
        # the way to a 502 plus a warning and a kubectl probe, for as long
        # as anyone kept asking. It is refused with the same sentence and
        # the same 409 as every other room-edits-it surface: the caller is
        # allowed to ask, and it is the agent that has no such thing. This
        # also covers `kobo_chat`, which delegates to this route rather than
        # holding a gate of its own.
        blocked = _room_edits_this_agent_or_409(agent)
        if blocked is not None:
            return blocked

        if agent.status != AgentStatus.READY:
            return JSONResponse({"error": "agent is not ready to chat"}, status_code=409)

        messages = payload.messages
        if not messages:
            return JSONResponse({"error": "messages must not be empty"}, status_code=422)

        for message in messages:
            if message.role not in CHAT_MESSAGE_ROLES:
                return JSONResponse(
                    {"error": f"unknown role: {message.role!r}"}, status_code=422
                )
            if not message.text:
                return JSONResponse(
                    {"error": "message text must not be empty"}, status_code=422
                )
            if len(message.text) > CHAT_MESSAGE_TEXT_MAX_LEN:
                return JSONResponse(
                    {
                        "error": (
                            f"message text exceeds {CHAT_MESSAGE_TEXT_MAX_LEN} characters"
                        )
                    },
                    status_code=422,
                )

        if messages[-1].role != "user":
            return JSONResponse(
                {"error": "last message must have role 'user'"}, status_code=422
            )

        # Same namespace/service derivation as HelloStep (kobo_api.steps)
        # for this same agent's a2a endpoint — both are just the agent's name.
        clients = app.state.clients
        cfg: Config = app.state.config
        converse_messages = [{"role": m.role, "text": m.text} for m in messages]

        # Switchboard relay (colleagues switchboard plan, Task 3): same
        # `cabal_enabled` truthiness check every other switchboard-facing
        # caller uses, AND clients.cabal actually wired — either gate alone
        # isn't enough (an enabled-by-config-but-unwired room, or a wired
        # client on a room that isn't enabled, must both still take the
        # legacy direct path below, completely unchanged).
        if cabal_enabled(cfg) and clients.cabal is not None:
            # Deliberately NOT stamping a contextId here (operator ruling
            # 2026-08-26). The runtime already sets its colleague-hop context
            # to `task.context_id or task.id`, so every hop within one turn
            # shares the inbound task's id, which is the whole within-turn
            # loop the issue #5 transcript showed. Stamping a derived
            # per-conversation id would only add cross-TURN grouping, and it
            # merges a whole conversation into one guard session: the guard
            # counts repeats per session with a threshold of 3 over ten
            # minutes, so a person sending the same short reply three times
            # would have the third refused. `_build_task_envelope` keeps its
            # optional context_id parameter for a future caller that has a
            # real per-conversation id to pass.
            envelope = _build_task_envelope(converse_messages)
            # Wall time of the relay itself (502 instrumentation): the
            # intermittent 502's cause is not established, and elapsed time is
            # the discriminating signal between a slow model chain and an
            # immediate transport failure. Read once here so the except block
            # below can report it.
            relay_started_at = time.monotonic()
            try:
                raw = clients.cabal.relay(agent.name, envelope)
                reply = _extract_relay_reply(raw)
            except (CabalError, httpx.HTTPError, ValueError) as exc:
                # `CabalClient.relay` never wraps a raw httpx transport
                # error (timeout, connection refused, ...) or a non-JSON
                # body's `json.JSONDecodeError` the way the direct path's
                # `_post_task` wraps its own transport/parse failures — all
                # three, plus a typed `CabalError` (non-2xx) and a malformed-
                # but-parseable reply (`_extract_relay_reply` above), land
                # here and get the same fixed, plain copy; the real cause is
                # only ever logged, never echoed to the caller.
                log.warning(
                    "chat relay failed for agent %s after %.3fs (%s): %s",
                    name,
                    time.monotonic() - relay_started_at,
                    type(exc).__name__,
                    exc,
                )
                if rollout_in_progress(
                    clients.kubectl, namespace=agent.name, deploy=agent.name
                ):
                    return JSONResponse(
                        {
                            "error": (
                                "Your agent is receiving a delivery right now. "
                                "Try again in a minute."
                            )
                        },
                        status_code=503,
                    )
                return JSONResponse(
                    {"error": "The switchboard did not answer. Try again shortly."},
                    status_code=502,
                )
            return JSONResponse({"reply": reply})

        try:
            reply = clients.agent.converse(
                namespace=agent.name,
                service=agent.name,
                messages=converse_messages,
            )
        except ClientError as exc:
            return JSONResponse({"error": f"agent unreachable: {exc}"}, status_code=502)

        return JSONResponse({"reply": reply})

    @app.post("/api/agents/{name}/retry", status_code=202)
    def retry_agent(name: str, request: Request, session: Session = Depends(get_session)) -> JSONResponse:  # noqa: B008
        agent = _visible_agent_or_404(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent

        if agent.status != AgentStatus.FAILED:
            return JSONResponse(
                {"error": f"cannot retry agent in status {agent.status!r}"}, status_code=409
            )

        # Owner-or-operator gate (access-control Task A3 fix round 1: the
        # initial route sweep missed this route, so a stranger could
        # relaunch someone else's failed pipeline). Placed after the FAILED
        # check above -- retry's own readiness gate, inlined here rather
        # than through `_ready_agent_or_error` (see that helper's docstring)
        # -- and before `reset_failed_steps`, the first actual work.
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        reset_failed_steps(session, agent.id)

        # Flip to PROVISIONING (and clear the prior error) synchronously,
        # before starting the pipeline thread — mirrors create_agent's own
        # status-before-thread discipline. The FAILED gate above is
        # read-then-act: without this, two retries racing right after each
        # other both read FAILED and both pass, each firing its own pipeline
        # thread against the same AgentStep rows. Committing PROVISIONING
        # here closes that window — a second retry request now reads
        # PROVISIONING and 409s instead of double-launching. The engine
        # (`_run_pipeline_body`) also sets PROVISIONING itself; redundant but
        # idempotent, so this doesn't change its behavior.
        agent.status = AgentStatus.PROVISIONING
        agent.error = None
        # Stamped in the SAME commit that puts the row back in flight. A
        # retried row still carries the PREVIOUS run's heartbeat, which is
        # already past the sweep's cutoff, and the new run's first beat is
        # a write that is allowed to fail (logged and skipped, never
        # raised) -- so without this, one failed first beat leaves a live
        # retry sweepable on the very next tick, in any replica.
        agent.run_heartbeat_at = naive_utc(datetime.now(UTC))
        session.commit()

        agent_id = agent.id
        cfg: Config = app.state.config

        try:
            run_pipeline(
                request.app.state.session_factory,
                agent_id,
                config=cfg,
                clients=app.state.clients,
                steps=pipeline_for(agent),
            )
        except Exception as exc:
            # Broad by design, same as create_agent's guard above (this
            # repo's ruff config enables no BLE / flake8-blind-except rules —
            # there's no lint exemption being invoked here). Same wedge as
            # create_agent's launch guard: the thread never started, so
            # nothing will ever move this agent out of PROVISIONING on its
            # own — fail it back so it stays retryable rather than getting
            # stuck past the FAILED-only retry gate above. `_fail_launch`
            # does the actual recovery (see its docstring for why it needs a
            # fresh session rather than this one) and always returns the 503
            # below, never a raw 500.
            log.exception(
                "retry_agent: failed to start provisioning for agent %s", agent_id
            )
            return _fail_launch(request.app.state.session_factory, session, agent_id, exc)

        # Same status-before-thread discipline as create_agent: report the
        # state this call put the agent into (about to re-provision), never
        # a value re-read after the pipeline thread is already running.
        return JSONResponse({"name": name, "status": AgentStatus.PROVISIONING}, status_code=202)

    # --- Build-log viewer (2026-08-27 feedback: parity with the org's PaaS) ------------------

    _BUILD_LOG_TAIL_LINES = 400

    @app.get("/api/agents/{name}/build-log")
    def agent_build_log(  # noqa: B008
        name: str,
        request: Request,
        session: Session = Depends(get_session),
    ) -> JSONResponse:
        """The tail of the agent repo's most recent workflow-run log --
        failed steps only when the run failed, so the reason is on top.
        Owner-or-operator: an owner without access to the CI project (the
        feedback's exact situation) reads their own agent's build here
        instead of a GitHub Actions page they cannot open."""
        agent = _visible_agent_or_404(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate
        if not agent.repo_full_name:
            return JSONResponse(
                {"error": "this agent has no repository yet"}, status_code=409
            )
        clients: Clients | None = getattr(request.app.state, "clients", None)
        github = clients.github if clients is not None else None
        if github is None:
            return JSONResponse({"error": "GitHub is not configured"}, status_code=503)
        try:
            run = github.latest_run(agent.repo_full_name)
            if run is None:
                return JSONResponse(
                    {"error": "this agent's repository has no builds yet"},
                    status_code=404,
                )
            failed = run.get("conclusion") not in (None, "success")
            log_text = github.run_log(
                agent.repo_full_name, run["id"], failed_only=failed
            )
        except ClientError:
            return JSONResponse(
                {"error": "the build system did not answer. Try again shortly."},
                status_code=502,
            )
        lines = log_text.splitlines()
        if len(lines) > _BUILD_LOG_TAIL_LINES:
            lines = ["... (%d earlier lines trimmed)" % (len(lines) - _BUILD_LOG_TAIL_LINES)] + lines[-_BUILD_LOG_TAIL_LINES:]
        return JSONResponse({"run": run, "log": "\n".join(lines)})

    # --- Agent PR broker (2026-08-27 spec): repo access + scoped tokens ---------

    def _pr_broker_off(config: Config) -> JSONResponse | None:
        """None when all three pr_app_* config values are set; the 503 body
        otherwise -- the same absent-equals-off convention as cabal_url,
        answered before any gate so an unconfigured room never leaks
        whether a given agent or repo exists on these routes."""
        if config.pr_app_id and config.pr_app_installation_id and config.pr_app_private_key_path:
            return None
        return JSONResponse(
            {"error": "the PR broker is not configured on this room"}, status_code=503
        )

    @app.post("/api/agents/{name}/repo-access")
    def request_repo_access(  # noqa: B008
        name: str,
        payload: RepoAccessRequest,
        request: Request,
        session: Session = Depends(get_session),
    ) -> JSONResponse:
        off = _pr_broker_off(app.state.config)
        if off is not None:
            return off
        agent = _visible_agent_or_404(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        email = (getattr(request.state, "iap_email", None) or "").lower()
        rows = []
        for repository in payload.repositories:
            existing = (
                session.query(RepoAccess)
                .filter(
                    RepoAccess.agent_name == agent.name,
                    RepoAccess.repository == repository,
                )
                .one_or_none()
            )
            if existing is not None and existing.status == REPO_ACCESS_APPROVED:
                # Already approved: asking again must never demote a live
                # grant back to requested.
                rows.append(existing)
                continue
            if existing is not None:
                # Re-request (including a revoked row coming back): same
                # row, fresh request stamp, decision cleared.
                existing.status = REPO_ACCESS_REQUESTED
                existing.requested_by = email
                existing.requested_at = datetime.now(UTC)
                existing.decided_by = None
                existing.decided_at = None
                rows.append(existing)
                continue
            row = RepoAccess(
                agent_name=agent.name, repository=repository, requested_by=email
            )
            session.add(row)
            rows.append(row)
        session.commit()
        return JSONResponse(
            {
                "requested": [
                    {"id": r.id, "repository": r.repository, "status": r.status}
                    for r in rows
                ]
            }
        )

    @app.get("/api/repo-access")
    def list_repo_access(  # noqa: B008
        request: Request,
        status: str | None = None,
        session: Session = Depends(get_session),
    ) -> JSONResponse:
        off = _pr_broker_off(app.state.config)
        if off is not None:
            return off
        query = session.query(RepoAccess)
        if status is not None:
            if status not in REPO_ACCESS_STATUSES:
                return JSONResponse(
                    {"error": f"status must be one of {', '.join(REPO_ACCESS_STATUSES)}"},
                    status_code=422,
                )
            query = query.filter(RepoAccess.status == status)
        # Scoped to what the caller can see: rows for visible agents only,
        # the same anti-enumeration discipline as the agent list itself.
        visible_agents, _ = _visible_agents(session, request)
        visible = {a.name for a in visible_agents}
        rows = [r for r in query.order_by(RepoAccess.requested_at).all() if r.agent_name in visible]
        return JSONResponse(
            {
                "results": [
                    {
                        "id": r.id,
                        "agent": r.agent_name,
                        "repository": r.repository,
                        "status": r.status,
                        "requested_by": r.requested_by,
                        "decided_by": r.decided_by,
                    }
                    for r in rows
                ]
            }
        )

    @app.post("/api/repo-access/decide")
    def decide_repo_access(  # noqa: B008
        payload: RepoAccessDecision,
        request: Request,
        session: Session = Depends(get_session),
    ) -> JSONResponse:
        """The BATCH gate (2026-08-27 ruling 1): the operator set decides
        many rows in one call -- approve and revoke both land here."""
        off = _pr_broker_off(app.state.config)
        if off is not None:
            return off
        gate = _repo_access_operator_block(request, app.state.config)
        if gate is not None:
            return gate

        email = (getattr(request.state, "iap_email", None) or "").lower()
        decided = []
        for row_id in payload.ids:
            row = session.get(RepoAccess, row_id)
            if row is None:
                return JSONResponse(
                    {"error": f"no repo-access row {row_id}"}, status_code=404
                )
            row.status = (
                REPO_ACCESS_APPROVED if payload.decision == "approved" else REPO_ACCESS_REVOKED
            )
            row.decided_by = email
            row.decided_at = datetime.now(UTC)
            decided.append(row)
        session.commit()
        return JSONResponse(
            {
                "decided": [
                    {"id": r.id, "agent": r.agent_name, "repository": r.repository,
                     "status": r.status}
                    for r in decided
                ]
            }
        )

    @app.post("/broker/agents/{name}/github-token")
    def mint_agent_github_token(  # noqa: B008
        name: str,
        request: Request,
        session: Session = Depends(get_session),
    ) -> JSONResponse:
        """THE BROKER (2026-08-27 spec): the caller is the AGENT itself,
        authenticated by its cabal-issued colleague JWT -- never a human
        session. Wall 2: the minted token's `repositories` is pinned to
        exactly this agent's APPROVED rows. 404 for an unknown agent AND
        for one with zero approvals -- an agent with no grants must not be
        distinguishable from one that does not exist, the same
        anti-enumeration answer every agent route gives."""
        config: Config = app.state.config
        off = _pr_broker_off(config)
        if off is not None:
            return off
        if not config.cabal_url:
            return JSONResponse(
                {"error": "the PR broker requires the switchboard"}, status_code=503
            )

        authorization = request.headers.get("authorization", "")
        bearer = authorization.removeprefix("Bearer ").strip()
        if not bearer:
            return JSONResponse({"error": "agent token required"}, status_code=401)
        try:
            verify_agent_token(config.cabal_url, bearer, name)
        except BrokerAuthError:
            return JSONResponse({"error": "agent token was not accepted"}, status_code=401)

        approved = (
            session.query(RepoAccess)
            .filter(
                RepoAccess.agent_name == name,
                RepoAccess.status == REPO_ACCESS_APPROVED,
            )
            .order_by(RepoAccess.repository)
            .all()
        )
        if not approved:
            return JSONResponse({"error": f"no such agent: {name!r}"}, status_code=404)

        try:
            minted = mint_scoped_token(
                app_id=config.pr_app_id,
                installation_id=config.pr_app_installation_id,
                private_key_path=config.pr_app_private_key_path,
                repositories=[r.repository for r in approved],
            )
        except ClientError:
            return JSONResponse(
                {"error": "the token mint did not answer. Try again shortly."},
                status_code=502,
            )
        return JSONResponse(minted)

    # --- Agent Jira read broker (2026-09-04, AREQ lane) -------------------------

    def _jira_broker_off(config: Config) -> JSONResponse | None:
        """None when the jira read broker is configured, the 503 to return
        otherwise -- the PR broker's absent-equals-off convention."""
        if (
            config.jira_domain
            and config.jira_email
            and config.jira_pat_path
            and config.jira_projects
        ):
            return None
        return JSONResponse(
            {"error": "the jira read broker is not configured on this room"},
            status_code=503,
        )

    def _jira_agent_block(config: Config, request: Request, name: str) -> JSONResponse | None:
        """The jira routes' shared gate: broker configured, switchboard
        wired, the bearer is cabal's own signature over this agent (the
        github-token route's exact walls), and the agent is on the lane's
        allowlist. Off-list reads as 404 -- an agent without the lane must
        not be distinguishable from one that does not exist."""
        off = _jira_broker_off(config)
        if off is not None:
            return off
        if not config.cabal_url:
            return JSONResponse(
                {"error": "the jira read broker requires the switchboard"},
                status_code=503,
            )
        authorization = request.headers.get("authorization", "")
        bearer = authorization.removeprefix("Bearer ").strip()
        if not bearer:
            return JSONResponse({"error": "agent token required"}, status_code=401)
        try:
            verify_agent_token(config.cabal_url, bearer, name)
        except BrokerAuthError:
            return JSONResponse({"error": "agent token was not accepted"}, status_code=401)
        if name not in config.jira_read_agents:
            return JSONResponse({"error": f"no such agent: {name!r}"}, status_code=404)
        return None

    @app.get("/broker/agents/{name}/jira/issues/{key}")
    def read_agent_jira_issue(name: str, key: str, request: Request) -> JSONResponse:
        """One issue with its comment history, for an allowlisted agent.
        The reachable projects are pinned server-side: a key outside
        `jira_projects` is refused here, before any tracker call."""
        config: Config = app.state.config
        block = _jira_agent_block(config, request, name)
        if block is not None:
            return block
        project = key.split("-", 1)[0].upper()
        reachable = set(config.jira_projects) | set(config.jira_alias_projects)
        if "-" not in key or project not in reachable:
            return JSONResponse(
                {
                    "error": "that issue is outside the projects this lane "
                    f"reaches ({', '.join(config.jira_projects)})"
                },
                status_code=403,
            )
        try:
            body = jira_fetch_issue(
                domain=config.jira_domain,
                email=config.jira_email,
                pat_path=config.jira_pat_path,
                key=key,
            )
        except ClientError as exc:
            if str(exc) == "no such issue":
                return JSONResponse({"error": f"no issue {key!r}"}, status_code=404)
            return JSONResponse(
                {"error": "the tracker did not answer. Try again shortly."},
                status_code=502,
            )
        # The post-fetch wall for alias keys: a migrated key resolves to its
        # NEW key in the response, and only a resolved key inside the pinned
        # projects leaves the broker. An alias that resolves anywhere else
        # is refused as if the pre-check had caught it -- the alias lane
        # exists for key redirects into the pin, never around it.
        resolved = (body.get("issue") or {}).get("key") or key
        if resolved.split("-", 1)[0].upper() not in config.jira_projects:
            return JSONResponse(
                {
                    "error": "that issue is outside the projects this lane "
                    f"reaches ({', '.join(config.jira_projects)})"
                },
                status_code=403,
            )
        return JSONResponse(body)

    @app.get("/broker/agents/{name}/jira/search")
    def search_agent_jira_issues(name: str, request: Request) -> JSONResponse:
        """Text search pinned to the lane's FIRST configured project (one
        project is the lane's shape today; a multi-project room grows a
        project parameter validated against the list, not a JQL knob).
        The term and limit are bounded inside `jira_broker.search_issues`
        -- no JQL crosses this boundary."""
        config: Config = app.state.config
        block = _jira_agent_block(config, request, name)
        if block is not None:
            return block
        text = request.query_params.get("text", "")
        try:
            limit = int(request.query_params.get("limit", "10"))
        except ValueError:
            limit = 10
        try:
            body = jira_search_issues(
                domain=config.jira_domain,
                email=config.jira_email,
                pat_path=config.jira_pat_path,
                project=config.jira_projects[0],
                text=text,
                limit=limit,
                created_after=request.query_params.get("created_after", ""),
                created_before=request.query_params.get("created_before", ""),
            )
        except ClientError as exc:
            message = str(exc)
            if message == "nothing searchable in the given text" or "bare date" in message:
                return JSONResponse({"error": message}, status_code=400)
            return JSONResponse(
                {"error": "the tracker did not answer. Try again shortly."},
                status_code=502,
            )
        return JSONResponse(body)

    # --- Destroy (Task 4): the resumable four-step teardown ledger --------------

    # Statuses meaning "still being created" — a destroy request against one
    # of these has nothing settled yet to tear down (no repo/app id may even
    # be recorded); the operator is told to retry once creation lands
    # somewhere destroy CAN act on (READY or FAILED), rather than the room
    # trying to race its own provisioning thread.
    _PROVISIONING_IN_FLIGHT = (AgentStatus.PENDING, AgentStatus.PROVISIONING)

    @app.delete("/api/agents/{name}", status_code=202)
    def destroy_agent(
        name: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        cfg: Config = app.state.config
        # Deliberately NOT `_owner_or_operator_or_403` (access-control Task
        # A3): destroy keeps its own, STRICTER `_operator_block` gate --
        # the destruction arc's standing rule that tearing an agent down is
        # operator-only, full stop, even for the agent's own owner. A3's
        # owner-or-operator gate is additive and looser for every OTHER
        # mutating surface; destroy was never meant to loosen alongside them.
        #
        # Gate FIRST, before the agent even exists check: info-safe, same
        # discipline as the Slack gate (a non-operator gets an identical 403
        # whether or not the named agent exists at all).
        block = _operator_block(request, cfg)
        if block is not None:
            return block

        agent = _visible_agent_or_404(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent

        if agent.status in _PROVISIONING_IN_FLIGHT:
            return JSONResponse(
                {"error": "the agent is still being created, retry or wait"},
                status_code=409,
            )

        # Status-before-thread discipline, same as create_agent/retry_agent:
        # commit DESTROYING before launching the thread. This also makes a
        # re-DELETE while already destroying (or destroy_failed) idempotent
        # from the caller's view — it reads a status outside
        # _PROVISIONING_IN_FLIGHT, so it falls through here and simply
        # re-launches run_destroy, which resumes from the first incomplete
        # step rather than redoing finished work.
        agent.status = AgentStatus.DESTROYING
        agent.error = None
        session.commit()

        agent_id = agent.id

        try:
            run_destroy(
                request.app.state.session_factory,
                agent_id,
                app.state.clients,
                cfg,
                # Who pressed destroy. Only the stored-secrets step reads
                # it, and it reads it because the secrets platform records
                # every removal against a person: the operator doing this
                # is that person, and the agent's creator (who may have
                # left) is the fallback for a resume with no request
                # behind it.
                acting_email=getattr(request.state, "iap_email", None),
            )
        except Exception as exc:
            # Same wedge as create_agent's/retry_agent's launch guards: the
            # thread never started, so nothing will ever move this agent out
            # of DESTROYING on its own — fail it back to DESTROY_FAILED so a
            # follow-up DELETE (Destroy again) can still resume it, rather
            # than leaving it wedged. `_fail_launch` does the actual
            # recovery and always returns the 503 below, never a raw 500.
            log.exception(
                "destroy_agent: failed to start destruction for agent %s", agent_id
            )
            return _fail_launch(
                request.app.state.session_factory,
                session,
                agent_id,
                exc,
                status=AgentStatus.DESTROY_FAILED,
                verb="destruction",
            )

        return JSONResponse({"status": AgentStatus.DESTROYING}, status_code=202)

    # --- Knowledge (P2b): bundle/knowledge/*.md seed-memory editing -------------
    #
    # save_knowledge/delete_knowledge are owner-or-operator gated
    # (access-control Task A3 fix round 1: the initial route sweep missed
    # both of these, so a stranger could overwrite or delete another
    # agent's knowledge files -- list_knowledge stays a plain read).
    #
    # Fast-memory-lane (arc3c Task 3): both routes now relay through the
    # switchboard first (system_task "knowledge") before ever touching the
    # repo, mirroring teach_agent's own instant-lane/archive-lane structure
    # below (main.py's `_teach_task_not_completed`/`_archive_teach_note`)
    # byte-for-byte -- reused rather than reimplemented, since the
    # not-completed check and catch set are marker-agnostic. Unlike intake
    # (Task 4), neither route needs an upfront filename pre-check: kobo's
    # own KNOWLEDGE_FILENAME_RE (letter-first, .md-only) is already a strict
    # SUBSET of the runtime's `^[a-z0-9][a-z0-9-]{0,39}\.md$` (digit-first
    # additionally allowed) -- anything kobo's own `_validate_filename`
    # would accept, the runtime accepts too, so there is no filename the
    # archive lane would silently succeed on that the relay would have
    # refused.

    def _archive_knowledge_save(repo: str, filename: str, content: str) -> None:
        """The knowledge write's background archive lane: only ever runs
        after the relay above already reported the write "completed" -- the
        runtime has already retired `filename`'s old entries and stored the
        new content, so this commit is purely a repo-side mirror of it.
        Failure is logged, never surfaced, same posture as teach's
        `_archive_teach_note` below: the memory already landed live: only
        the repo's own copy is missing until a later save lands it.

        Critical 2 fix: archive_only=True -- the runtime already has this
        content live, so rebuilding the agent's image for it would be pure
        downtime bought for nothing (the Recreate-strategy Deployment
        hard-rotates the pod on every push). The degraded synchronous lane
        below in save_knowledge (no relay ever reached the agent) calls
        save_knowledge_file directly without archive_only -- that commit
        IS the delivery, so it must keep CI."""
        if not _room_may_commit_to_repo(app, repo):
            return
        try:
            save_knowledge_file(
                app.state.clients.github, repo, filename, content, archive_only=True
            )
        except Exception as exc:
            log.warning(
                "knowledge archive commit failed for %s (%s): %s", repo, filename, exc
            )

    def _archive_knowledge_delete(repo: str, filename: str) -> None:
        """The knowledge delete's background archive lane: only ever runs
        after the relay above already reported the delete "completed" --
        the runtime has already retired every entry the ledger had for
        `filename` and dropped its ledger row, so this commit purely
        mirrors that removal in the repo. Failure is logged, never
        surfaced, same posture as `_archive_knowledge_save` above.

        Critical 2 fix: archive_only=True, same reasoning as the save side
        -- the degraded synchronous lane below in delete_knowledge keeps
        CI, since that delete IS the delivery."""
        if not _room_may_commit_to_repo(app, repo):
            return
        try:
            delete_knowledge_file(
                app.state.clients.github, repo, filename, archive_only=True
            )
        except Exception as exc:
            log.warning(
                "knowledge archive delete failed for %s (%s): %s", repo, filename, exc
            )

    @app.get("/api/agents/{name}/knowledge")
    def list_knowledge(
        name: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent

        try:
            files = read_knowledge(app.state.clients.github, agent.repo_full_name)
        except ClientError as exc:
            log.warning("knowledge route failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't reach your agent's files. Try again."}, status_code=502
            )

        return JSONResponse(
            {"files": [{"name": f.name, "content": f.content} for f in files]}
        )

    @app.put("/api/agents/{name}/knowledge/{filename}")
    def save_knowledge(
        name: str,
        filename: str,
        payload: KnowledgeSavePayload,
        request: Request,
        background_tasks: BackgroundTasks,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent
        blocked = _room_edits_this_agent_or_409(agent)
        if blocked is not None:
            return blocked
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        cfg: Config = app.state.config
        clients = app.state.clients
        if cabal_enabled(cfg) and clients.cabal is not None:
            envelope = _build_system_task_envelope(
                "knowledge", payload.content, metadata={"filename": filename}
            )
            try:
                raw = clients.cabal.relay(agent.name, envelope)
                if _teach_task_not_completed(raw):
                    raise CabalError("agent reply was not a completed task")
                _extract_relay_reply(raw)
            except (CabalError, httpx.HTTPError, ValueError) as exc:
                # Same catch set as teach_agent's own relay above: ANY
                # instant-lane miss degrades to the archive lane below, so
                # a knowledge save never 502s during a delivery window.
                log.warning(
                    "knowledge save relay failed for agent %s, archive lane instead: %s",
                    name,
                    exc,
                )
            else:
                background_tasks.add_task(
                    _archive_knowledge_save, agent.repo_full_name, filename, payload.content
                )
                return JSONResponse({"mode": "instant"})

        try:
            sha = save_knowledge_file(
                app.state.clients.github, agent.repo_full_name, filename, payload.content
            )
        except KnowledgeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        except ClientError as exc:
            log.warning("knowledge route failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't reach your agent's files. Try again."}, status_code=502
            )

        return JSONResponse({"sha": sha, "mode": "archive"})

    @app.delete("/api/agents/{name}/knowledge/{filename}")
    def delete_knowledge(
        name: str,
        filename: str,
        request: Request,
        background_tasks: BackgroundTasks,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent
        blocked = _room_edits_this_agent_or_409(agent)
        if blocked is not None:
            return blocked
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        cfg: Config = app.state.config
        clients = app.state.clients
        if cabal_enabled(cfg) and clients.cabal is not None:
            # DELETE_KEY on the runtime side (daedalus.memory.teach.
            # DELETE_KEY) is exactly the string "delete" -- a different
            # spelling here would silently fall back to a write instead of
            # a delete, so this key is pinned verbatim in
            # test_delete_knowledge_instant_lane_relays_and_archives_in_background.
            envelope = _build_system_task_envelope(
                "knowledge", "", metadata={"filename": filename, "delete": True}
            )
            try:
                raw = clients.cabal.relay(agent.name, envelope)
                if _teach_task_not_completed(raw):
                    raise CabalError("agent reply was not a completed task")
                _extract_relay_reply(raw)
            except (CabalError, httpx.HTTPError, ValueError) as exc:
                log.warning(
                    "knowledge delete relay failed for agent %s, archive lane instead: %s",
                    name,
                    exc,
                )
            else:
                background_tasks.add_task(
                    _archive_knowledge_delete, agent.repo_full_name, filename
                )
                return JSONResponse({"mode": "instant"})

        try:
            sha = delete_knowledge_file(
                app.state.clients.github, agent.repo_full_name, filename
            )
        except KnowledgeError as exc:
            # knowledge.py carries no structured discriminator between "bad
            # filename" and "file not there to delete" — both are a plain
            # KnowledgeError with only their message text to go on. The two
            # messages have disjoint, stable prefixes ("invalid knowledge
            # filename: " vs "knowledge file not found: "; see
            # kobo_api.knowledge._validate_filename/delete_knowledge_file),
            # so matching on that prefix is the cleanest option available
            # without touching knowledge.py. It can't misfire: a bad filename
            # raises from _validate_filename before delete_knowledge_file
            # ever reaches its own not-found check.
            message = str(exc)
            if message.startswith("knowledge file not found:"):
                return JSONResponse(
                    {"error": f"no such knowledge file: {filename!r}"}, status_code=404
                )
            return JSONResponse({"error": message}, status_code=422)
        except ClientError as exc:
            log.warning("knowledge route failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't reach your agent's files. Try again."}, status_code=502
            )

        return JSONResponse({"sha": sha, "mode": "archive"})

    @app.get("/api/agents/{name}/knowledge/delivery/{sha}")
    def knowledge_delivery(
        name: str, sha: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent

        if not KNOWLEDGE_SHA_RE.match(sha):
            return JSONResponse({"error": f"invalid sha: {sha!r}"}, status_code=422)

        clients = app.state.clients
        try:
            state = delivery_state(
                clients.github,
                clients.kubectl,
                repo=agent.repo_full_name,
                namespace=agent.name,
                deploy=agent.name,
                sha=sha,
            )
        except ClientError as exc:
            log.warning("knowledge route failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't check on the delivery. Try again."}, status_code=502
            )

        return JSONResponse(state)

    # --- Skills (Task 2): post-assembly catalog picks + local guardrails --------
    #
    # `agent.skills` (the JSON column populated at creation/assembly time,
    # kobo_api.steps._stage_skills) is the single source of truth for what an
    # agent carries — each entry a `{"name", "team", "sha"}` dict for a
    # catalog pick (an assembly-era entry carries no "source" key at all;
    # every route below treats that the same as `"source": "catalog"`, never
    # a third state) or a `{"name", "source": "local"}` dict for a
    # guardrail-authored one. Every mutation below reassigns `agent.skills`
    # to a fresh list rather than editing an entry dict in place — same
    # reasoning as `_stage_skills`'s own sha-drift correction: SQLAlchemy's
    # JSON column change tracking only notices a new list object, not a
    # mutated dict already inside the old one.

    def _skill_source(entry: dict) -> str:
        """An entry's provenance, legacy-entry-safe: assembly-time picks
        predate the "source" key entirely, and are exactly the entries a
        real catalog pick would carry, so a missing key reads as "catalog",
        never as some third unclassified state."""
        return entry.get("source", "catalog")

    def _skill_conflict(skill_id: str) -> JSONResponse:
        """The one 409 body both the catalog-add and local-save routes
        return on a name collision — worded identically regardless of which
        direction the collision runs, since from the caller's side it's the
        same fact either way: this agent already has a skill by this name."""
        return JSONResponse(
            {"error": f"the agent already carries a skill named {skill_id!r}"},
            status_code=409,
        )

    @app.get("/api/agents/{name}/skills")
    def list_skills(
        name: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent

        # The bundle is the runtime truth; agent.skills is kobo's ledger,
        # and the direct-PR lane (a human merging skills straight into the
        # agent's repo) writes the bundle without telling the ledger.
        # Reconcile on read: the repo listing is the truth-set of ids, DB
        # entries keep their provenance (team/sha for catalog picks),
        # repo-only ids surface as local AND heal the ledger, DB-only rows
        # prune (removed by PR = not on the agent). A repo read failure
        # degrades to the ledger read and never prunes.
        repo = agent.repo_full_name
        # Everything the pre-clone decision needs, read here and then the
        # transaction RELEASED: reading `agent` at all has autobegun one,
        # and each listing below is a real `git clone` taking seconds. A
        # transaction held open across two of those pins a connection (and,
        # on Postgres, the oldest running snapshot) for the whole round
        # trip. `session.rollback()` writes nothing -- it just ends the read
        # transaction; the authoritative re-read happens after the clones.
        ledger_names = {e["name"] for e in agent.skills}
        session.rollback()

        try:
            repo_ids = list_repo_skills(app.state.clients.github, repo)
        except ClientError as exc:
            log.warning("skills repo listing failed for %s: %s", name, exc)
            repo_ids = None

        if repo_ids is not None and ledger_names - set(repo_ids):
            # A prune looks possible. `list_repo_skills` answers from a 60s
            # PER-PROCESS cache, so a skill added through another replica
            # (or through this one before its own cache filled) is
            # invisible here for up to a minute -- and pruning is
            # destructive: the row's catalog provenance (team, sha) cannot
            # be recovered once it's gone, since a re-discovered skill only
            # ever heals back as "local". Confirm against a fresh listing
            # before removing anything. Only when a prune is on the table:
            # the confirm read is another real clone.
            bust_repo_skills_cache(repo)
            try:
                repo_ids = list_repo_skills(app.state.clients.github, repo)
            except ClientError as exc:
                # Same degrade-to-the-ledger rule the first read has: an
                # unreachable repo is never evidence of absence.
                log.warning("skills repo re-listing failed for %s: %s", name, exc)
                repo_ids = None

        if repo_ids is not None:
            # Both clones are done; open a fresh transaction now. The
            # re-read is load-bearing: a POST to one of the kobo-lane
            # skills-mutation routes can have landed and committed while
            # those clones ran, and the write below must start from
            # whatever the DB holds NOW.
            session.refresh(agent)
            present = set(repo_ids)
            known = [e for e in agent.skills if e["name"] in present]
            known_names = {e["name"] for e in known}
            merged = known + [
                {"name": skill_id, "source": "local"}
                for skill_id in repo_ids
                if skill_id not in known_names
            ]
            pruned = [e["name"] for e in agent.skills if e["name"] not in present]
            if pruned:
                log.info(
                    "skills reconcile pruned %s for %s (absent from bundle)",
                    pruned,
                    name,
                )
            if merged != agent.skills:
                # This write is the room repairing its OWN cache from the
                # bundle's truth, not a caller mutating anything -- a GET
                # that finds the ledger stale just heals it in place before
                # answering, same as any other read-repair.
                agent.skills = merged
                session.commit()

        skills = []
        for entry in agent.skills:
            source = _skill_source(entry)
            skills.append(
                {
                    "name": entry["name"],
                    "team": entry.get("team"),
                    "source": source,
                    "sha": entry.get("sha"),
                    "editable": source == "local",
                }
            )
        return JSONResponse({"skills": skills})

    @app.post("/api/agents/{name}/skills/catalog")
    def add_skill_from_catalog(
        name: str,
        payload: CatalogSkillPayload,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent
        blocked = _room_edits_this_agent_or_409(agent)
        if blocked is not None:
            return blocked
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        # Collision check BEFORE any repo work, either-class: re-picking a
        # name this agent already carries — whether that existing entry is
        # itself a catalog pick or a local guardrail file — is a 409, not a
        # silent overwrite.
        if any(entry["name"] == payload.name for entry in agent.skills):
            return _skill_conflict(payload.name)

        try:
            commit_sha, source_sha = add_catalog_skill(
                app.state.clients.github,
                app.state.clients.storage,
                app.state.config,
                agent.repo_full_name,
                payload.name,
                payload.team,
            )
        except SkillsError as exc:
            # kobo_api.skills carries no structured discriminator between
            # "bad skill id" and "catalog no longer serves this team/id" —
            # both are a plain SkillsError with only their message text to go
            # on. The two messages have disjoint, stable prefixes ("invalid
            # skill id: " vs "skill no longer in the catalog: "; see
            # kobo_api.skills._validate_skill_id/add_catalog_skill), so
            # matching on that prefix is the cleanest option available
            # without touching skills.py — same idiom as delete_knowledge's
            # own KnowledgeError prefix match below.
            message = str(exc)
            if message.startswith("skill no longer in the catalog:"):
                return JSONResponse({"error": message}, status_code=404)
            return JSONResponse({"error": message}, status_code=422)
        except ClientError as exc:
            log.warning("skills route failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't reach your agent's files. Try again."}, status_code=502
            )

        # The repo write just succeeded -- bust this repo's cached skills
        # listing (kobo_api.skills.list_repo_skills's TTL cache) now, before
        # anything else, so a GET landing inside the TTL sees this pick
        # instead of a stale pre-add listing.
        bust_repo_skills_cache(agent.repo_full_name)

        # The sha recorded on the row is the catalog's CURRENT source-sha for
        # this pick (add_catalog_skill's second return value) — the pin a
        # later provision/resume would compare against — never the pushed
        # commit sha (`commit_sha`, this route's own response), which is a
        # different value entirely and answers a different question (what
        # got pushed just now, not what the catalog's content pins to).
        agent.skills = [entry for entry in agent.skills if entry["name"] != payload.name] + [
            {"name": payload.name, "team": payload.team, "sha": source_sha, "source": "catalog"}
        ]
        session.commit()

        return JSONResponse({"sha": commit_sha})

    @app.delete("/api/agents/{name}/skills/{skill_id}")
    def delete_skill(
        name: str,
        skill_id: str,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent
        blocked = _room_edits_this_agent_or_409(agent)
        if blocked is not None:
            return blocked
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        # One remove_skill call handles either class: a catalog pick and a
        # locally-authored skill both live under the same
        # bundle/skills/<id>/ tree, so there's nothing to branch on here —
        # this IS the "unified delete" the brief calls for.
        try:
            sha = remove_skill(app.state.clients.github, agent.repo_full_name, skill_id)
        except SkillsError as exc:
            message = str(exc)
            if message.startswith("skill not found:"):
                return JSONResponse({"error": message}, status_code=404)
            return JSONResponse({"error": message}, status_code=422)
        except ClientError as exc:
            log.warning("skills route failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't reach your agent's files. Try again."}, status_code=502
            )

        # Bust before the ledger write below -- same reasoning as the
        # catalog-add route above: a GET landing inside the TTL must not
        # re-heal this just-deleted skill back in from a stale listing.
        bust_repo_skills_cache(agent.repo_full_name)

        agent.skills = [entry for entry in agent.skills if entry["name"] != skill_id]
        session.commit()

        return JSONResponse({"sha": sha})

    @app.get("/api/agents/{name}/skills/local/{skill_id}")
    def get_local_skill(
        name: str, skill_id: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent

        # Content is never served for a catalog-class id, even if the repo
        # happens to have a SKILL.md there (every catalog pick's tree does) —
        # checked against the row BEFORE calling into the repo at all, same
        # as the collision checks above, so a catalog id costs no clone.
        entry = next((e for e in agent.skills if e["name"] == skill_id), None)
        if entry is None or _skill_source(entry) != "local":
            return JSONResponse(
                {"error": f"no such local skill: {skill_id!r}"}, status_code=404
            )

        try:
            content = read_local_skill(app.state.clients.github, agent.repo_full_name, skill_id)
        except SkillsError as exc:
            message = str(exc)
            if message.startswith("skill not found:"):
                return JSONResponse(
                    {"error": f"no such local skill: {skill_id!r}"}, status_code=404
                )
            return JSONResponse({"error": message}, status_code=422)
        except ClientError as exc:
            log.warning("skills route failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't reach your agent's files. Try again."}, status_code=502
            )

        return JSONResponse({"name": skill_id, "content": content})

    @app.put("/api/agents/{name}/skills/local/{skill_id}")
    def save_local_skill_route(
        name: str,
        skill_id: str,
        payload: SkillSavePayload,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent
        blocked = _room_edits_this_agent_or_409(agent)
        if blocked is not None:
            return blocked
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        # Collision check BEFORE any repo work — but only against a
        # catalog-class entry: overwriting your OWN existing local skill via
        # this same route is the ordinary edit path (save_local_skill's
        # overwrite semantics), never a collision.
        entry = next((e for e in agent.skills if e["name"] == skill_id), None)
        if entry is not None and _skill_source(entry) == "catalog":
            return _skill_conflict(skill_id)

        try:
            sha = save_local_skill(
                app.state.clients.github, agent.repo_full_name, skill_id, payload.content
            )
        except SkillsError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        except ClientError as exc:
            log.warning("skills route failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't reach your agent's files. Try again."}, status_code=502
            )

        # Same bust-before-write reasoning as the catalog-add/delete routes
        # above -- a GET landing inside the TTL must see this save's
        # content, not a stale pre-save listing.
        bust_repo_skills_cache(agent.repo_full_name)

        agent.skills = [e for e in agent.skills if e["name"] != skill_id] + [
            {"name": skill_id, "source": "local"}
        ]
        session.commit()

        return JSONResponse({"sha": sha})

    # --- Intake (Task 3): one-shot memory upload files + ingest status ----------
    #
    # `bundle/intake/*` files are ingested into the agent's live memory
    # exactly ONCE, by a runtime seeder that runs on boot (a later task) —
    # these three routes are just the upload/list/delete plumbing, plus
    # list_intake's ancestor-derived pending/ingested status per file (see
    # its docstring, kobo_api.intake). No JSON column on Agent tracks
    # intake, unlike skills: status is derived fresh from git ancestry vs
    # the live image every time, nothing here to keep in sync.

    @app.get("/api/agents/{name}/intake")
    def list_intake_route(
        name: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent

        # Same namespace/deploy convention as knowledge_delivery above:
        # both are just the agent's own name.
        clients = app.state.clients
        try:
            files = list_intake(
                clients.github,
                clients.kubectl,
                repo=agent.repo_full_name,
                namespace=agent.name,
                deploy=agent.name,
            )
        except ClientError as exc:
            log.warning("intake route failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't reach your agent's files. Try again."}, status_code=502
            )

        return JSONResponse({"files": files})

    @app.post("/api/agents/{name}/intake")
    def upload_intake(
        name: str,
        payload: IntakeUploadPayload,
        request: Request,
        background_tasks: BackgroundTasks,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent
        blocked = _room_edits_this_agent_or_409(agent)
        if blocked is not None:
            return blocked
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        # Fast-memory-lane (arc3c Task 4): a memory upload now relays
        # through the switchboard first, reusing teach's own "teach" system
        # task below (`_build_teach_envelope`/`_archive_teach_note`) rather
        # than a new marker -- there is no separate "intake" marker on the
        # runtime side, and an intake upload is exactly teach's own
        # "append-only operator note" shape, just with a caller-chosen
        # filename instead of an auto-generated one.
        #
        # That caller-chosen filename is the one wrinkle teach itself never
        # has: kobo's own INTAKE_FILENAME_RE additionally accepts a ".txt"
        # extension the runtime's teach-lane filename rule
        # (`^[a-z0-9][a-z0-9-]{0,39}\.md$`, daedalus.memory.teach.
        # _FILENAME_RE) does not. Unlike knowledge's own filename (a strict
        # subset of that runtime rule, so no divergence is possible there),
        # a ".txt" upload here would sail past kobo's own validation, get
        # refused by the runtime, and then silently "succeed" via the
        # archive-lane fallback below -- a delivery the fast lane could
        # never actually have made. So this is checked HERE, before any
        # relay is attempted (and unconditionally, not just when the
        # switchboard happens to be wired), refusing early with a message
        # naming the rule rather than letting that mismatch through.
        if not _INTAKE_FAST_LANE_FILENAME_RE.match(payload.filename):
            return JSONResponse(
                {
                    "error": (
                        f"invalid intake filename: {payload.filename!r}. Use "
                        "lowercase letters, digits, and hyphens, ending in .md."
                    )
                },
                status_code=422,
            )

        cfg: Config = app.state.config
        clients = app.state.clients
        if cabal_enabled(cfg) and clients.cabal is not None:
            envelope = _build_teach_envelope(payload.content, payload.filename)
            try:
                raw = clients.cabal.relay(agent.name, envelope)
                if _teach_task_not_completed(raw):
                    raise CabalError("agent reply was not a completed task")
                _extract_relay_reply(raw)
            except (CabalError, httpx.HTTPError, ValueError) as exc:
                # Same catch set as teach_agent's own relay above: ANY
                # instant-lane miss degrades to the archive lane below, so
                # an intake upload never 502s during a delivery window.
                log.warning(
                    "intake upload relay failed for agent %s, archive lane instead: %s",
                    name,
                    exc,
                )
            else:
                background_tasks.add_task(
                    _archive_teach_note,
                    app,
                    agent.repo_full_name,
                    payload.filename,
                    payload.content,
                )
                return JSONResponse({"mode": "instant"})

        try:
            sha = upload_intake_file(
                app.state.clients.github,
                agent.repo_full_name,
                payload.filename,
                payload.content,
                runtime_image=agent.runtime_image or cfg.runtime_image,
            )
        except IntakeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        except ClientError as exc:
            log.warning("intake route failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't reach your agent's files. Try again."}, status_code=502
            )

        return JSONResponse({"sha": sha, "mode": "archive"})

    @app.delete("/api/agents/{name}/intake/{filename}")
    def delete_intake(
        name: str,
        filename: str,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent
        blocked = _room_edits_this_agent_or_409(agent)
        if blocked is not None:
            return blocked
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        try:
            sha = delete_intake_file(app.state.clients.github, agent.repo_full_name, filename)
        except IntakeError as exc:
            # Same disjoint-prefix idiom as delete_knowledge above: intake.py
            # carries no structured discriminator between "bad filename" and
            # "file not there to delete", so the message's stable prefix
            # ("intake file not found: ", vs "invalid intake filename: ") is
            # what's matched on.
            message = str(exc)
            if message.startswith("intake file not found:"):
                return JSONResponse(
                    {"error": f"no such intake file: {filename!r}"}, status_code=404
                )
            return JSONResponse({"error": message}, status_code=422)
        except ClientError as exc:
            log.warning("intake route failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't reach your agent's files. Try again."}, status_code=502
            )

        # Deleting the source upload never touches whatever the seeder
        # already wrote into the agent's actual memory from it (out of
        # scope for kobo, a runtime concern) — this note says so in plain
        # language rather than leaving that ambiguous.
        return JSONResponse(
            {"sha": sha, "note": "memories are kept. Only the source file was removed"}
        )

    # --- Teach (Task 1: "a chat moment becomes a taught memory") ----------------
    #
    # A thin route over the same intake seam above: format the operator's
    # distilled chat note (kobo_api.teach.format_note), name it
    # (kobo_api.teach.note_filename), and hand both to upload_intake_file
    # verbatim, same runtime_image re-pin as the plain upload route (this is
    # just another way to write into bundle/intake/, so it needs the exact
    # same false-ingested-window fix). Ready-gated, any signed-in user (no
    # operator gate) — same reach as the metadata routes above.

    # `_teach_task_not_completed`/`_archive_teach_note` are now module-level
    # (MCP learnings trio, Task 8, see their own docstrings just above
    # `create_app`) -- both used to be closures defined right here; every
    # call site below still calls them by the same bare name, resolved
    # through the module scope now instead of this enclosing one.

    @app.post("/api/agents/{name}/teach")
    def teach_agent(
        name: str,
        payload: TeachPayload,
        request: Request,
        background_tasks: BackgroundTasks,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent
        blocked = _room_edits_this_agent_or_409(agent)
        if blocked is not None:
            return blocked

        text = payload.text
        if not text.strip():
            return JSONResponse(
                {"error": "teach note text must not be empty"}, status_code=422
            )

        size = len(text.encode("utf-8"))
        if size > INTAKE_MAX_BYTES:
            return JSONResponse(
                {
                    "error": (
                        f"teach note text is {size} bytes, exceeds the "
                        f"{INTAKE_MAX_BYTES}-byte limit"
                    )
                },
                status_code=422,
            )

        # One canonical "now" for this request, UTC, passed as the same
        # on_date to both format_note and note_filename below — never two
        # independently-computed reads that could drift apart across a
        # midnight rollover.
        on_date = datetime.now(UTC).date().isoformat()
        # request.state.iap_email is only ever set by IapMiddleware (active
        # only when config.iap_audience is non-empty), and even then only
        # when the verified assertion actually carried an email claim —
        # getattr's default covers both "IAP disabled entirely" and "IAP on,
        # claim absent", so the attribution line below is simply omitted
        # rather than raising or faking a placeholder identity.
        taught_by = getattr(request.state, "iap_email", None)

        note = format_note(text, taught_by=taught_by, on_date=on_date)
        filename = note_filename(text, on_date=on_date)

        cfg: Config = app.state.config
        clients = app.state.clients
        if cabal_enabled(cfg) and clients.cabal is not None:
            envelope = _build_teach_envelope(note, filename)
            try:
                raw = clients.cabal.relay(agent.name, envelope)
                if _teach_task_not_completed(raw):
                    raise CabalError("agent reply was not a completed task")
                _extract_relay_reply(raw)
            except (CabalError, httpx.HTTPError, ValueError) as exc:
                # Same catch set as the chat relay above, plus the
                # not-completed check: ANY instant-lane miss degrades to
                # the archive lane below, so teaching never 502s during a
                # delivery window. The cause is logged, never echoed.
                log.warning(
                    "teach relay failed for agent %s, archive lane instead: %s",
                    name,
                    exc,
                )
            else:
                background_tasks.add_task(
                    _archive_teach_note, app, agent.repo_full_name, filename, note
                )
                return JSONResponse({"filename": filename, "mode": "instant"})

        # This is the synchronous archive-lane fallback: it only runs when
        # the relay above missed entirely, which means the memory was
        # NEVER stored in the agent -- this upload IS the delivery, not a
        # mirror of one that already landed. archive_only stays at its
        # default (False) so the commit keeps its normal, CI-triggering
        # message: the rebuild it kicks off is the only way this note
        # actually reaches the agent.
        try:
            sha = upload_intake_file(
                app.state.clients.github,
                agent.repo_full_name,
                filename,
                note,
                runtime_image=agent.runtime_image or cfg.runtime_image,
            )
        except IntakeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        except ClientError as exc:
            log.warning("teach route failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't reach your agent's files. Try again."}, status_code=502
            )

        return JSONResponse({"filename": filename, "mode": "archive", "sha": sha})

    # --- Learnings (daily-learnings spec, Task 7): the decide surface over --
    # the nightly summarizer's candidate rows (kobo_api.learnings.
    # store_candidates writes them; kobo_api.clients._build_learnings_
    # envelope feeds the nightly loop that produces them). Approve rides
    # the teach lane byte-for-byte -- same instant/archive split, same
    # `_build_teach_envelope`/`_archive_teach_note` plumbing -- with the
    # `prefix="learning"` note_filename gives it, so techne provenance
    # reads `intake:learning-<date>-<slug>` rather than `intake:chat-...`.

    @app.get("/api/agents/{name}/learnings")
    def list_learnings(
        name: str,
        request: Request,
        status: str = "all",
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        agent = _visible_agent_or_404(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent
        if status != "all" and status not in LEARNING_STATUSES:
            return JSONResponse(
                {"error": f"unknown status {status!r}"}, status_code=422
            )
        query = session.query(AgentLearning).filter_by(agent_name=agent.name)
        if status != "all":
            query = query.filter_by(status=status)
        rows = query.order_by(AgentLearning.created_at.desc()).all()
        return JSONResponse(
            {
                "learnings": [
                    {
                        "id": row.id,
                        "day": row.day,
                        "content": row.content,
                        "kind": row.kind,
                        "source": row.source,
                        "evidence": row.evidence,
                        "said_by": row.said_by,
                        "status": row.status,
                        "decided_by": row.decided_by,
                        "created_at": (
                            row.created_at.isoformat() if row.created_at else None
                        ),
                    }
                    for row in rows
                ]
            }
        )

    # `_decide_learning` is now module-level (MCP learnings trio, Task 8,
    # see its own docstring just above `create_app`) -- both this route's
    # `approve_learning`/`deny_learning`/`bulk_decide_learnings` below AND
    # the two MCP write tools (`kobo_decide_learning`/
    # `kobo_decide_learnings_bulk`) call this exact function.

    @app.post("/api/agents/{name}/learnings/{learning_id}/approve")
    def approve_learning(
        name: str,
        learning_id: int,
        request: Request,
        background_tasks: BackgroundTasks,
        session: Session = Depends(get_session),  # noqa: B008
        payload: LearningApprovePayload | None = None,
    ) -> JSONResponse:
        return _decide_learning(
            name,
            learning_id,
            "approve",
            request,
            background_tasks,
            session,
            content=payload.content if payload else None,
        )

    @app.post("/api/agents/{name}/learnings/{learning_id}/deny")
    def deny_learning(
        name: str,
        learning_id: int,
        request: Request,
        background_tasks: BackgroundTasks,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        return _decide_learning(
            name, learning_id, "deny", request, background_tasks, session
        )

    @app.post("/api/agents/{name}/learnings/bulk")
    def bulk_decide_learnings(
        name: str,
        payload: LearningsBulkPayload,
        request: Request,
        background_tasks: BackgroundTasks,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        # `_decide_learning` below carries the same guard, so every id would
        # be refused anyway -- but this route answers 200 with a per-id
        # status code, and fifty buried 409s is a worse answer than one.
        # Resolved here without `_ready_agent_or_error` so that a name the
        # caller cannot see keeps today's per-id behaviour exactly.
        agent = _visible_agent_or_404(session, request, name)
        if not isinstance(agent, JSONResponse):
            blocked = _room_edits_this_agent_or_409(agent)
            if blocked is not None:
                return blocked

        results = [
            {
                "id": learning_id,
                "status_code": _decide_learning(
                    name, learning_id, payload.action, request, background_tasks, session
                ).status_code,
            }
            for learning_id in payload.ids[:50]
        ]
        return JSONResponse({"results": results})

    # --- Tooling (2026-08-13 addendum): the staged door for an agent's --------
    # LAW-to-be — `bundle/tools/*.{py,go}`, validated and delivered, but with
    # no enforcement yet (a separate, later contract). Same
    # clone/list/upload/delete plumbing as intake above, plus a second
    # write path (import_tooling) that pulls every matching file out of some
    # OTHER repo's folder in one delivery. See kobo_api.tooling's module
    # docstring for the full honesty-guarantee reasoning.
    #
    # Fast tooling lane (arc3d Task 5): a single-file upload now relays
    # through the switchboard first (system_task "tool") before ever
    # touching the repo, same instant-lane/archive-lane shape as
    # knowledge/intake above. Two things are different from those, both
    # from the brief's own ruling ("tools are delivered fast but loaded at
    # boot, no hot-reload into a running process"):
    #
    #   1. Validation runs BEFORE even checking whether the switchboard is
    #      wired, using the exact same `_validate_filename`/`_validate_
    #      content` the archive lane's own `upload_tooling_file` already
    #      calls internally (reused here, not reimplemented, per the
    #      module docstring's own invitation: "any later per-agent write
    #      path" picks these up for free). A tool that fails is refused
    #      before any delivery is attempted, cabal wired or not; the
    #      runtime re-runs this SAME mechanical contract itself before
    #      writing (a ported copy, daedalus.memory.tooling: the fast lane
    #      skips CI by definition, so kobo's own validation here is never
    #      the runtime's only line of defense).
    #   2. A successful instant delivery is not the end of the story the
    #      way a knowledge/intake write is: the tool lands on the agent's
    #      persistent overlay directory, but the enforcement loader only
    #      reads it at boot, so this also requests exactly one rollout
    #      restart (`_request_tool_restart`) and the response says so in
    #      plain copy rather than claiming the tool is already active.
    # `import_tooling` (multi-file) is unchanged: it stays archive-only.

    def _archive_tooling_upload(repo: str, filename: str, content: str) -> None:
        """The tool upload's background archive lane: only ever runs after
        the relay above already reported the write completed, and the runtime
        has already re-validated and written the file into its own overlay
        directory, so this commit is purely a repo-side mirror of it for
        history/CI parity. Failure is logged, never surfaced: the tool
        already landed on the agent's volume; only the repo's own copy is
        missing until a later upload lands it."""
        if not _room_may_commit_to_repo(app, repo):
            return
        try:
            upload_tooling_file(
                app.state.clients.github,
                repo,
                filename,
                content,
                runtime_image=_runtime_image_for_repo(app, repo),
            )
        except Exception as exc:
            log.warning(
                "tooling archive commit failed for %s (%s): %s", repo, filename, exc
            )

    def _request_tool_restart(agent_name: str) -> None:
        """Ask the platform to roll the agent's Deployment so the
        enforcement loader picks up the newly delivered overlay tool at its
        next boot: the fast lane is delivery only, never a hot reload.
        Failure is logged, never surfaced: the tool is already on the
        agent's volume either way; only the restart that would load it
        might need a manual nudge (a later upload, or an unrelated
        deploy, will still pick it up)."""
        try:
            app.state.clients.kubectl.restart_deploy(agent_name, agent_name)
        except ClientError as exc:
            log.warning(
                "tooling restart request failed for agent %s: %s", agent_name, exc
            )

    @app.get("/api/agents/{name}/tooling")
    def list_tooling_route(
        name: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent

        clients = app.state.clients
        try:
            files, enforcement = list_tooling(
                clients.github,
                clients.kubectl,
                clients.agent,
                repo=agent.repo_full_name,
                namespace=agent.name,
                deploy=agent.name,
            )
        except ClientError as exc:
            log.warning("tooling route failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't reach your agent's files. Try again."}, status_code=502
            )

        # Task 4 (card probe + banner graduation): "enforced" is a
        # presentation-layer fact this route composes from list_tooling's two
        # return values, not something list_tooling itself claims — a
        # delivered file only earns the label when the runtime both attests
        # to enforcement (card fetch succeeded, version >= 1) AND isn't
        # currently locked (serving refusals because a hook failed to
        # load). A pending file's dict is left completely untouched either
        # way — delivery truth is unchanged by enforcement status.
        if enforcement["attested"] and not enforcement["locked"]:
            for file in files:
                if file["delivered"]:
                    file["status"] = "enforced"

        return JSONResponse({"files": files, "enforcement": enforcement})

    @app.post("/api/agents/{name}/tooling")
    def upload_tooling(
        name: str,
        payload: ToolingUploadPayload,
        request: Request,
        background_tasks: BackgroundTasks,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent
        blocked = _room_edits_this_agent_or_409(agent)
        if blocked is not None:
            return blocked
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        # Refused BEFORE any delivery is attempted, cabal wired or not (see
        # the section comment above): the same validator upload_tooling_file
        # calls internally for the archive lane, just run here first so an
        # invalid tool is never even relayed.
        try:
            _validate_filename(payload.filename)
            _validate_content(payload.filename, payload.content.encode("utf-8"))
        except ToolingError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

        # Shadowing refusal (Important 7 fix): a filename colliding with
        # either the reserved baseline-tooling-pack namespace or a file
        # already sitting in THIS agent's own composed bundle/tools/ is
        # refused here, before any relay or archive write is attempted --
        # same "before any delivery" discipline as the mechanical checks
        # just above. Without this, an upload naming an existing file (a
        # baseline hook, or an earlier upload) would silently overwrite it
        # -- the fallback-overwrites-hook hole: a collision now never
        # reaches the archive lane at all. The cheap check runs first
        # (no network call); the repo listing only runs if that passes.
        if payload.filename.startswith(BASELINE_TOOL_PREFIX):
            return JSONResponse(
                {
                    "error": (
                        f"{payload.filename!r} is in the baseline tooling pack's "
                        f"reserved {BASELINE_TOOL_PREFIX!r} namespace. Choose a "
                        "different filename."
                    )
                },
                status_code=422,
            )
        try:
            existing = existing_tool_filenames(app.state.clients.github, agent.repo_full_name)
        except ClientError as exc:
            log.warning("tooling shadow-check failed for %s: %s", name, exc)
            return JSONResponse(
                {"error": "couldn't reach your agent's files. Try again."}, status_code=502
            )
        if payload.filename in existing:
            return JSONResponse(
                {
                    "error": (
                        f"a tool named {payload.filename!r} already exists in this "
                        "agent's bundle. Choose a different filename, or delete "
                        "the existing one first."
                    )
                },
                status_code=422,
            )

        cfg: Config = app.state.config
        clients = app.state.clients
        if cabal_enabled(cfg) and clients.cabal is not None:
            envelope = _build_system_task_envelope(
                "tool", payload.content, metadata={"filename": payload.filename}
            )
            try:
                raw = clients.cabal.relay(agent.name, envelope)
                if _teach_task_not_completed(raw):
                    raise CabalError("agent reply was not a completed task")
                _extract_relay_reply(raw)
            except (CabalError, httpx.HTTPError, ValueError) as exc:
                # Same catch set as every other fast-lane caller: ANY
                # instant-lane miss degrades to the archive lane below, so
                # a tooling upload never 502s during a delivery window.
                log.warning(
                    "tooling upload relay failed for agent %s, archive lane instead: %s",
                    name,
                    exc,
                )
            else:
                background_tasks.add_task(
                    _archive_tooling_upload,
                    agent.repo_full_name,
                    payload.filename,
                    payload.content,
                )
                _request_tool_restart(agent.name)
                return JSONResponse(
                    {
                        "mode": "instant",
                        "message": (
                            "Tool delivered. It will be active after your "
                            "agent's next restart."
                        ),
                    }
                )

        try:
            sha = upload_tooling_file(
                app.state.clients.github,
                agent.repo_full_name,
                payload.filename,
                payload.content,
                runtime_image=agent.runtime_image or cfg.runtime_image,
            )
        except ToolingError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        except ClientError as exc:
            log.warning("tooling route failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't reach your agent's files. Try again."}, status_code=502
            )

        return JSONResponse({"sha": sha, "mode": "archive"})

    @app.post("/api/agents/{name}/tooling/import")
    def import_tooling_route(
        name: str,
        payload: ToolingImportPayload,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent
        blocked = _room_edits_this_agent_or_409(agent)
        if blocked is not None:
            return blocked
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        cfg: Config = app.state.config
        try:
            result = import_tooling(
                app.state.clients.github,
                agent.repo_full_name,
                source_repo=payload.repo,
                source_path=payload.path,
                runtime_image=agent.runtime_image or cfg.runtime_image,
            )
        except ToolingError as exc:
            # Request-shape failures (a malformed owner/repo, a traversal
            # attempt in the path, or a nonexistent source path) — distinct
            # from a per-file entry in the result's own "rejected" list, which
            # is a 200: those are candidate files that failed the SAME
            # validator individually, not a bad request overall.
            message = str(exc)
            if message.startswith("tooling import path not found:"):
                return JSONResponse(
                    {"error": f"tooling source path not found: {payload.path!r} in {payload.repo!r}"},
                    status_code=404,
                )
            return JSONResponse({"error": message}, status_code=422)
        except ClientError as exc:
            log.warning("tooling route failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't reach your agent's files. Try again."}, status_code=502
            )

        return JSONResponse(result)

    @app.delete("/api/agents/{name}/tooling/{filename}")
    def delete_tooling(
        name: str,
        filename: str,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent
        blocked = _room_edits_this_agent_or_409(agent)
        if blocked is not None:
            return blocked
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        try:
            sha = delete_tooling_file(app.state.clients.github, agent.repo_full_name, filename)
        except ToolingError as exc:
            # Same disjoint-prefix idiom as delete_intake above: tooling.py
            # carries no structured discriminator between "bad filename" and
            # "file not there to delete", so the message's stable prefix
            # ("tooling file not found: " vs "invalid tooling filename: ") is
            # what's matched on.
            message = str(exc)
            if message.startswith("tooling file not found:"):
                return JSONResponse(
                    {"error": f"no such tooling file: {filename!r}"}, status_code=404
                )
            return JSONResponse({"error": message}, status_code=422)
        except ClientError as exc:
            log.warning("tooling route failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't reach your agent's files. Try again."}, status_code=502
            )

        return JSONResponse({"sha": sha})

    # --- Slack self-service (2026-08-17): manifest + degated connect ------------
    #
    # The config-token API-creation route (`POST .../slack/app`) is retired:
    # the room hands the manifest to whoever's setting Slack up, they create
    # the app themselves at Slack's own app site, and paste its id/tokens
    # into connect below. The config-token `SlackClient` itself stays wired
    # (`clients.slack`) — destruction still uses it to delete legacy,
    # room-created apps — but no route here calls `.create_app` anymore.

    @app.get("/api/agents/{name}/slack/manifest")
    def slack_manifest(
        name: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(
            session, request, name, not_ready_message="agent is not ready to connect to Slack yet"
        )
        if isinstance(agent, JSONResponse):
            return agent

        # persona_first_line source: the persona catalog's own one-line
        # `description` field (bucket catalogs, Task 5 — the FTS entry's
        # `description`, same text the /api/personas/search route returns),
        # looked up by the id recorded on `agent.persona` — NOT the id
        # itself and NOT the persona's full templated body. Falls back to
        # an id-derived phrase only if the persona has since been removed
        # from the catalog, mirroring `_model_label`'s own retired-catalog-
        # entry fallback above. Moved here verbatim from the retired
        # create-app route (this manifest render is its direct successor).
        engine = session.get_bind()
        _maybe_refresh_catalogs(app, engine)
        persona_entry = get_entry(engine, "persona", agent.persona, team=None)
        if persona_entry is not None:
            persona_first_line = persona_entry.description
        else:
            log.warning("persona %r not in the catalog, using generic text for slack manifest", agent.persona)
            persona_first_line = f"A {agent.persona} agent, reachable in Slack."

        manifest = render_manifest(agent.name, persona_first_line)
        return JSONResponse({"manifest": manifest})

    @app.post("/api/agents/{name}/slack/connect")
    def connect_slack(
        name: str,
        payload: SlackConnectRequest,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        cfg: Config = app.state.config
        agent = _ready_agent_or_error(
            session, request, name, not_ready_message="agent is not ready to connect to Slack yet"
        )
        if isinstance(agent, JSONResponse):
            return agent
        blocked = _room_edits_this_agent_or_409(agent)
        if blocked is not None:
            return blocked
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        clients = app.state.clients
        if clients.slack is None:
            return JSONResponse({"error": "Slack is not configured"}, status_code=503)

        # Two-lane Slack (2026-08-17): the channel list is whatever's
        # already saved via PUT .../slack/channels, not something this
        # request carries — connect only delivers tokens (plus, since
        # self-service, the app id). Never echo the submitted token values
        # back in an error body, here or anywhere below.
        channels = decode_channels(agent.slack_channels)
        if not channels:
            return JSONResponse(
                {"error": "Add at least one channel first."}, status_code=409
            )

        # app_id validated FIRST, ahead of either token's format: Slack
        # self-service means the app already exists by connect time (the
        # old "set up a Slack app for this agent first" 409 precondition
        # is gone — connect now works starting from a null slack_app_id).
        if not SLACK_APP_ID_RE.match(payload.app_id):
            return JSONResponse(
                {"error": "that doesn't look like a Slack app ID (it starts with A)"},
                status_code=422,
            )
        if not payload.bot_token.startswith("xoxb-"):
            return JSONResponse(
                {"error": "that doesn't look like a Slack bot token"}, status_code=422
            )
        if not payload.app_token.startswith("xapp-"):
            return JSONResponse(
                {"error": "that doesn't look like a Slack app-level token"}, status_code=422
            )

        # The person doing the wiring, since this IS a request with a
        # verified caller behind it; the agent's creator is the fallback
        # for a room with no identity perimeter in front of it, matching
        # what the background pipeline can name.
        acting_email = getattr(request.state, "iap_email", None) or agent.created_by
        if clients.ladon is not None:
            # Checked BEFORE any work: the secrets platform records every
            # write against a person, and having nobody to name is a
            # standing state rather than a blip -- so it reads as a 409
            # with the reason, not as the retryable 502 a delivery failure
            # gets. Only the lane needs this; a room writing Secrets
            # directly never did.
            refusal = acting_email_refusal(agent.name, acting_email)
            if refusal is not None:
                return JSONResponse({"error": refusal}, status_code=409)

        try:
            sha = wire_slack(
                clients.github,
                clients.kubectl,
                clients.gojira,
                repo=agent.repo_full_name,
                name=agent.name,
                gojira_app_id=agent.gojira_app_id,
                channels=channels,
                bot_token=payload.bot_token,
                app_token=payload.app_token,
                runtime_image=agent.runtime_image or cfg.runtime_image,
                # None unless the secrets platform's machine lane is
                # configured, in which case the token delivery rides it
                # instead of a direct Secret write (kobo_api.ladon).
                ladon=clients.ladon,
                acting_email=acting_email,
            )
        except ClientError as exc:
            log.warning("slack connect failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't set up Slack. Try again."}, status_code=502
            )

        # Self-service provenance (Task 1): the app id comes from the
        # request body now, not something kobo minted itself, so it's
        # (re)stamped here every successful connect; `slack_app_self_service`
        # records that this app was born self-service (never flipped back)
        # so destroy-slack can read it directly rather than inferring the
        # same distinction from Slack's error codes at delete time.
        agent.slack_app_id = payload.app_id
        agent.slack_app_self_service = True
        agent.slack_wired = True
        session.commit()

        return JSONResponse({"sha": sha})

    @app.put("/api/agents/{name}/slack/channels")
    def save_slack_channels(
        name: str,
        payload: SlackChannelsRequest,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        # Owner-or-operator now (access-control Task A3) -- this lane was
        # briefly gateless (Two-lane Slack, 2026-08-17, "for everyone in the
        # room"), same as connect above, until A3 put both back behind a
        # gate: not the old admin-list operator gate, but the room-wide
        # owner-or-operator one every other mutating surface now shares.
        agent = _ready_agent_or_error(
            session, request, name, not_ready_message="agent is not ready for Slack changes yet"
        )
        if isinstance(agent, JSONResponse):
            return agent
        blocked = _room_edits_this_agent_or_409(agent)
        if blocked is not None:
            return blocked
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        # An empty submitted list is the deliberate disconnect path: it
        # skips clean_channels entirely (which would otherwise reject an
        # empty list with "Add at least one channel.") and stores `[]`
        # outright. Any non-empty list goes through clean_channels's
        # dedupe/format/cap checks.
        cleaned = clean_channels(payload.channels) if payload.channels else []
        if isinstance(cleaned, str):
            return JSONResponse({"error": cleaned}, status_code=422)

        agent.slack_channels = encode_channels(cleaned)
        body: dict = {
            "channels": cleaned,
            "state": derive_state(cleaned, wired=agent.slack_wired),
        }
        if agent.slack_wired:
            # Wired and the list changed (including to `[]`, which is the
            # disconnect-while-wired case): rewire the repo's transports to
            # match. Commit-only — no secret delivery, no reprovision, see
            # kobo_api.slack_setup.rewire_slack_channels's own docstring.
            clients = app.state.clients
            try:
                sha = rewire_slack_channels(
                    clients.github, repo=agent.repo_full_name, channels=cleaned
                )
            except ClientError as exc:
                log.warning("slack rewire failed: %s", exc)
                # Return before session.commit(): the request-scoped
                # session (get_session) only ever session.close()s in its
                # finally, no explicit rollback — but SQLAlchemy's
                # Session.close() itself ends any transaction in progress
                # (an implicit rollback) before releasing the connection,
                # so the in-memory `agent.slack_channels` mutation above
                # never reaches the DB. The stored list stays exactly what
                # the repo's transports still say.
                return JSONResponse(
                    {"error": "couldn't update the channels. Try again."},
                    status_code=502,
                )
            body["sha"] = sha
        session.commit()
        return JSONResponse(body)

    # --- Secrets and environment (design 2026-09-22) --------------------------
    #
    # Two lists on one panel, for composed and connected agents alike. The
    # SECRETS half is held on the secrets platform and delivered into the
    # agent's namespace as the Kubernetes Secret `<agent>-env`; the
    # ENVIRONMENT half lives in the agent's own `gojira.yaml`. Both reach
    # the pod as environment variables, which is why one key rule covers
    # both (kobo_api.agent_env).
    #
    # `_room_edits_this_agent_or_409` is deliberately NOT called on these
    # four. That guard says the room never pushes to a connected repo's
    # BRANCHES, and these routes honour it the way the connected pipeline's
    # own wiring step does, by proposing a pull request instead of pushing
    # (`kobo_api.agent_env.edit_gojira_yaml`). Refusing outright would take
    # the panel away from exactly the agents most likely to need it: a
    # team's own service with its own API keys.
    #
    # A VALUE never comes back out of any of these, and never goes into a
    # log line either: a failure's own sentence can quote the request that
    # produced it, so what reaches the log is the exception's CLASS and the
    # agent's name, never `str(exc)`.

    def _secrets_agent_or_error(
        session: Session, request: Request, name: str
    ) -> Agent | JSONResponse:
        """The agent these four routes act on, or the response to return.

        Ready, visible, and the caller's to administer -- the same pair of
        gates every other write surface in this room holds, in the same
        order, so a 404 still comes before a 403 for an agent the caller
        cannot see.

        Owner or operator, and deliberately NOT widened by a per-user
        grant the way the learnings decide gate is. A grantee may talk to
        an agent and teach it; handing them its credentials is a different
        thing, and the narrower gate is the one to start from.
        """
        agent = _ready_agent_or_error(
            session,
            request,
            name,
            not_ready_message="this agent is not ready for environment changes yet",
        )
        if isinstance(agent, JSONResponse):
            return agent
        return _owner_or_operator_or_403(agent, request) or agent

    def _secrets_acting_email(agent: Agent, request: Request) -> str | None:
        """Who the secrets platform records this call against: the verified
        caller, or the agent's creator when the room has no identity
        perimeter in front of it.

        The caller comes first on a READ as well as on a write, and that is
        not a detail: the platform's audit answers "who asked about this
        path", and the person who opened the panel is the honest answer to
        that. `created_by` stands in only where there is no perimeter to
        name anybody.
        """
        return getattr(request.state, "iap_email", None) or agent.created_by

    def _lane_or_error(agent: Agent, request: Request):
        """The secrets lane plus the person to record against, or a
        `JSONResponse` saying why there is nothing to do.

        Two standing states, not blips, so both are answered rather than
        retried: a room with no secrets platform configured at all, and an
        agent with nobody on record to write on behalf of.
        """
        ladon = app.state.clients.ladon
        if ladon is None:
            return JSONResponse({"error": NO_SECRETS_PLATFORM}, status_code=503)
        email = _secrets_acting_email(agent, request)
        refusal = acting_email_refusal(agent.name, email)
        if refusal is not None:
            return JSONResponse({"error": refusal}, status_code=409)
        return ladon, email

    def _lane_failed(what: str, agent: Agent, exc: Exception, message: str) -> JSONResponse:
        """Log that a lane call failed, and answer the house sentence.

        The exception's CLASS and the agent's name, never `str(exc)`: the
        platform's own error text is a sentence the room did not write, and
        a lane that echoed the request it refused would put a secret value
        into this room's logs. Which call failed and against which agent is
        what an operator acts on; the rest is on the platform's own side,
        where its audit trail already has it.
        """
        log.warning(
            "secrets %s failed for agent %s (%s)", what, agent.name, type(exc).__name__
        )
        return JSONResponse({"error": message}, status_code=502)

    def _sync_for(agent: Agent, secret_name: str) -> dict[str, str | None]:
        """Whether `secret_name` is actually on the agent, read off the
        platform's own ExternalSecret in the agent's namespace.

        Namespace and Deployment are both the agent's name, the same pair
        every other cluster call in this room passes.

        A read the room could not make is `syncing` with its own sentence
        rather than a failure: the whole panel must not go red because one
        cluster call timed out, and an agent's secrets are still worth
        listing by name when nobody can say whether they landed.
        """
        kubectl = getattr(app.state.clients, "kubectl", None)
        if kubectl is None:
            return sync_unknown()
        try:
            status = kubectl.external_secret_status(agent.name, secret_name)
        except ClientError as exc:
            # The exception's CLASS and the agent's name, the same rule
            # every other call on this panel holds.
            log.warning(
                "secrets sync read failed for agent %s (%s)",
                agent.name,
                type(exc).__name__,
            )
            return sync_unknown()
        return sync_state(status)

    def _managed_secret_rows(agent: Agent, ladon, email: str) -> list[dict]:
        """The secrets the ROOM set on this agent, as locked rows.

        The colleague pass and the Slack values, only where they exist.
        They are shown for the same reason the key rules refuse their
        names: a list that refuses a name it does not show reads as
        broken, and an owner who cannot see that their agent already has
        a Slack token has no way to know the room put one there.

        A purpose the platform would not answer about is LEFT OUT rather
        than failing the whole read. These two rows are context beside the
        owner's own list; losing the list because the room could not ask
        about the colleague pass would be the wrong trade.
        """
        rows: list[dict] = []
        for purpose in MANAGED_PURPOSES:
            try:
                held = ladon.list_keys(email, managed_secret_path(agent.name, purpose))
            except ClientError as exc:
                log.warning(
                    "secrets %s read failed for agent %s (%s)",
                    purpose,
                    agent.name,
                    type(exc).__name__,
                )
                continue
            if not held.keys:
                continue
            secret_name = managed_secret_name(agent.name, purpose)
            sync = _sync_for(agent, secret_name)
            rows.extend(
                {
                    "name": key,
                    "purpose": purpose,
                    "secret_name": secret_name,
                    "sync": dict(sync),
                }
                for key in held.keys
            )
        return rows

    def _wired_on_default(agent: Agent) -> bool | None:
        """Whether the agent's own files name its secrets Secret, on the
        default branch. None when the room could not look.

        None rather than False, deliberately: false would say the line is
        not there, and "the room cannot read this repository right now" is
        a different fact that the panel must not turn into a claim about
        somebody's file.
        """
        try:
            return has_env_secret(
                read_default_gojira(app.state.clients.github, agent.repo_full_name),
                agent.name,
            )
        except ClientError as exc:
            log.warning(
                "secrets wiring read failed for agent %s (%s)",
                agent.name,
                type(exc).__name__,
            )
            return None

    @app.get("/api/agents/{name}/secrets")
    def list_agent_secrets(
        name: str,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        """Which secrets this agent holds, BY NAME.

        `platform` is false for a room with no secrets platform
        configured -- the panel replaces its Secrets list with one sentence
        rather than offering an add form that could not work. The room
        answers 200 there rather than 503: "there is no place to keep
        these" is an answer about the room, and a read that refuses would
        make the panel look broken instead of unconfigured.

        A read never answers 409 for having nobody to name, unlike a write:
        the caller IS the acting person here (they passed the gate above,
        so the room knows exactly who they are), and an agent whose creator
        has left is still one its owner can open the panel on.

        `updated_at` is one timestamp for the whole list, not one per key:
        the lane versions a path as a whole, so there is no per-key date to
        report and this room does not invent one.
        """
        agent = _secrets_agent_or_error(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent

        ladon = app.state.clients.ladon
        email = _secrets_acting_email(agent, request)
        if ladon is None or not (email or "").strip():
            # No platform at all, or a platform and nobody it would accept
            # as the asker. Neither is a refusal -- a person looking at
            # their own agent can act on neither -- but they are DIFFERENT
            # facts, and an empty list would say the same wrong thing for
            # both. `platform` false means there is nowhere to keep
            # secrets; `unreadable` means there is, and the room cannot
            # ask on anybody's behalf right now. The panel has one line
            # for each.
            return JSONResponse(
                {
                    "keys": [],
                    "updated_at": None,
                    "platform": ladon is not None,
                    "secret_name": None,
                    "unreadable": ladon is not None,
                    # Nothing to ask the cluster about, and nothing to
                    # show: an empty list of locked rows, and no claim
                    # about a file nobody read.
                    "sync": sync_state(None),
                    "wired": None,
                    "managed": [],
                }
            )

        try:
            held = ladon.list_keys(email, secret_path(agent.name))
        except ClientError as exc:
            return _lane_failed("read", agent, exc, SECRETS_PLATFORM_UNREACHABLE)

        return JSONResponse(
            {
                "keys": held.keys,
                "updated_at": held.updated_at,
                "platform": True,
                "secret_name": env_secret_name(agent.name),
                "unreadable": False,
                # The lock. One state for the whole list, because the
                # platform delivers the whole path as ONE Kubernetes
                # Secret: there is no per-key sync to report and this
                # room does not invent one.
                "sync": _sync_for(agent, env_secret_name(agent.name)),
                "wired": _wired_on_default(agent),
                "managed": _managed_secret_rows(agent, ladon, email),
            }
        )

    @app.put("/api/agents/{name}/secrets")
    def set_agent_secrets(
        name: str,
        payload: AgentEnvWrite,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        """Add, change or remove this agent's secrets, in one call.

        ONE invariant runs through everything below, and the order of the
        steps exists to keep it: the agent's `gojira.yaml` must never name
        a Kubernetes Secret that does not exist. The deploy platform turns
        `secrets_from` into `envFrom`, and a pod whose `envFrom` names a
        missing Secret does not start at all. So the two directions run in
        opposite orders:

        - ADDING: store the value, then add the line. A secret stored
          without its line is one the pod cannot see yet, which the next
          save fixes.
        - REMOVING THE LAST ONE: take the line away FIRST, then delete the
          Secret. For a composed agent that is one commit and then the
          deletes, with the deploy asked for at the end. For a connected
          agent the room cannot merge its own pull request, so it deletes
          NOTHING and answers `pending_unwire`: the stored secret stays
          until somebody merges, and the next save finishes the job.

        Before either, the wiring branch is reconciled against what this
        save wants (`_reconcile_wiring`): an unmerged pull request of the
        room's own proposing the OPPOSITE change is closed and its branch
        removed, because merging it later would undo what is being done
        here.
        """
        agent = _secrets_agent_or_error(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent

        lane = _lane_or_error(agent, request)
        if isinstance(lane, JSONResponse):
            return lane
        ladon, email = lane

        clients = app.state.clients
        try:
            # Read and parsed FIRST, and through the same visibility gate
            # the environment read uses: an unreadable repository must be
            # a refusal, never a quiet "not wired" that would have the
            # room propose a line already sitting on the default branch.
            on_default = has_env_secret(
                read_default_gojira(clients.github, agent.repo_full_name), agent.name
            )
        except RepoBusy as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except ClientError as exc:
            return _lane_failed("file read", agent, exc, AGENT_FILES_UNREACHABLE)

        path = secret_path(agent.name)
        try:
            held = ladon.list_keys(email, path)
        except ClientError as exc:
            return _lane_failed("read", agent, exc, SECRETS_PLATFORM_UNREACHABLE)

        refusal = write_refusal(payload.set, payload.remove, held.keys)
        if refusal is not None:
            return JSONResponse({"error": refusal}, status_code=422)

        remaining = merged_names(held.keys, payload.set, payload.remove)
        target = bool(remaining)

        # Whatever the room proposed before and nobody has merged. Settled
        # before any write, because a stale OPPOSITE request is a change
        # waiting to undo this one and has to be closed rather than merely
        # ignored.
        pending = _reconcile_wiring(agent, target=target)
        if isinstance(pending, JSONResponse):
            return pending

        if not remaining:
            return _empty_the_secrets(
                agent,
                ladon=ladon,
                email=email,
                path=path,
                exists=held.exists,
                on_default=on_default,
                pending=pending,
            )

        try:
            if payload.set or payload.remove:
                ladon.put_secret(email, path, dict(payload.set), remove=list(payload.remove))
                ladon.ensure_synced(
                    email, agent.name, path, agent.name, env_secret_name(agent.name)
                )
        except ClientError as exc:
            return _lane_failed("write", agent, exc, SECRETS_WRITE_FAILED)

        wiring = pending
        if wiring is None and not on_default:
            wiring = _edit_wiring(agent, wire=True)
            if isinstance(wiring, JSONResponse):
                return wiring

        return JSONResponse(
            {
                "keys": remaining,
                "platform": True,
                "secret_name": env_secret_name(agent.name),
                "wiring": wiring.how if wiring is not None else UNCHANGED,
                "emptied": False,
                "pending_unwire": False,
                **({"pull_request": wiring.pull_request} if wiring and wiring.pull_request else {}),
            }
        )

    def _empty_the_secrets(
        agent: Agent,
        *,
        ladon,
        email: str,
        path: str,
        exists: bool,
        on_default: bool,
        pending,
    ) -> JSONResponse:
        """Remove an agent's last secret: the line first, then the Secret.

        Three states, and a retry of any of them lands in the right one:

        - the line is still on the default branch, and the room may commit
          (composed): commit it away, delete, then ask for a deploy;
        - the line is still there and the room may NOT commit (connected):
          propose it and delete nothing, so the stored Secret outlives the
          pull request that has to merge first;
        - the line is already gone: delete, or (if the path is gone too)
          answer that it is already done, without writing anything back.

        That last case is what makes a retry safe. A second save of the
        same removal finds nothing at the path and must NOT call
        `put_secret` on it: an empty write would create the path again,
        and the sync behind it would recreate an ExternalSecret over a
        Secret nothing wants.
        """
        clients = app.state.clients
        wiring = pending

        if on_default:
            if is_connected(agent):
                proposed = wiring or _edit_wiring(agent, wire=False)
                if isinstance(proposed, JSONResponse):
                    return proposed
                # Nothing deleted, deliberately: the file still names the
                # Secret until somebody merges, and taking it away now
                # would stop the agent.
                return JSONResponse(
                    {
                        "keys": [],
                        "platform": True,
                        "secret_name": env_secret_name(agent.name),
                        "wiring": proposed.how,
                        "emptied": False,
                        "pending_unwire": True,
                        **(
                            {"pull_request": proposed.pull_request}
                            if proposed.pull_request
                            else {}
                        ),
                    }
                )
            # Composed: the room owns this file, so the line goes now and
            # the deploy waits until the Secret is gone too.
            wiring = _edit_wiring(agent, wire=False, reprovision=False)
            if isinstance(wiring, JSONResponse):
                return wiring

        if exists:
            try:
                ladon.unsync(email, agent.name, agent.name, env_secret_name(agent.name))
                ladon.delete_secret(email, path)
            except ClientError as exc:
                # Never "couldn't store that": nothing was being stored.
                return _lane_failed("removal", agent, exc, SECRETS_REMOVE_FAILED)

        if agent.gojira_app_id is not None and not is_connected(agent):
            try:
                clients.gojira.reprovision(agent.gojira_app_id)
            except ClientError as exc:
                return _lane_failed("deploy", agent, exc, SECRETS_REMOVED_NO_DEPLOY)

        return JSONResponse(
            {
                "keys": [],
                "platform": True,
                "secret_name": None,
                "wiring": wiring.how if wiring is not None else UNCHANGED,
                "emptied": True,
                "pending_unwire": False,
                **({"pull_request": wiring.pull_request} if wiring and wiring.pull_request else {}),
            }
        )

    def _reconcile_wiring(agent: Agent, *, target: bool):
        """Settle the wiring branch against what this save wants.

        Returns the `RepoEdit` for a pull request that already proposes
        `target` (so the caller reuses it and proposes nothing new), None
        when there is nothing open, or a `JSONResponse` to return.

        A request proposing the OPPOSITE is the case this exists for. The
        room opened it, nobody merged it, and a later save has made it
        wrong: an unwire request merged after somebody adds a secret back
        would take the line away from an agent that needs it, and a wire
        request merged after the last secret is gone would name a Secret
        that no longer exists. Either way the file ends up lying about the
        Secret, which is the one thing every order here is arranged to
        prevent. So it is CLOSED, with a line saying why, and its branch
        removed so the next proposal has somewhere to go.

        Composed agents never get here with anything open: the room
        commits to its own repository and opens no request at all.
        """
        if not is_connected(agent):
            return None

        github = app.state.clients.github
        repo = agent.repo_full_name
        try:
            open_pr = github.find_pr(repo, branch=SECRETS_WIRING_BRANCH)
            if open_pr is None:
                return None
            proposes = has_env_secret(
                parse_gojira_yaml(
                    github.read_file(repo, "gojira.yaml", ref=SECRETS_WIRING_BRANCH)
                ),
                agent.name,
            )
            if proposes == target:
                return RepoEdit(how=PULL_REQUEST, pull_request=open_pr)

            log.info(
                "closing the room's stale wiring request for %s (it proposes %s)",
                agent.name,
                "the line" if proposes else "no line",
            )
            github.close_pr(repo, open_pr, WIRING_SUPERSEDED)
            require_our_branch(github, repo, SECRETS_WIRING_BRANCH)
            github.delete_branch(repo, SECRETS_WIRING_BRANCH)
        except RepoBusy as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except ClientError as exc:
            return _lane_failed("wiring", agent, exc, AGENT_FILES_UNREACHABLE)
        return None

    def _edit_wiring(agent: Agent, *, wire: bool, reprovision: bool = True):
        """Add or remove `<agent>-env` in `secrets_from`. The `RepoEdit` on
        success, or the `JSONResponse` a route should return.

        `carries` is what makes reusing an open pull request safe: the
        branch's OWN gojira.yaml is read and asked whether it already says
        this, rather than assuming that any request open on the branch
        must. The wiring gets its own branch for the same reason -- the
        one line it proposes is always the same, and an environment
        request waiting on `kobo/env` is a different change entirely.
        """
        clients = app.state.clients
        try:
            return edit_gojira_yaml(
                clients.github,
                clients.gojira,
                repo=agent.repo_full_name,
                connected=is_connected(agent),
                gojira_app_id=agent.gojira_app_id,
                edit=(
                    (lambda data: wire_env_secret(data, agent.name))
                    if wire
                    else (lambda data: unwire_env_secret(data, agent.name))
                ),
                message=(
                    SECRETS_WIRING_PR_TITLE if wire else SECRETS_UNWIRE_PR_TITLE
                ),
                branch=SECRETS_WIRING_BRANCH,
                title=SECRETS_WIRING_PR_TITLE if wire else SECRETS_UNWIRE_PR_TITLE,
                body=SECRETS_WIRING_PR_BODY if wire else SECRETS_UNWIRE_PR_BODY,
                # A request that already carries this exact change is the
                # answer rather than a refusal. `_reconcile_wiring` has
                # already closed anything proposing the opposite, so what
                # is left open here can only be a match or nothing.
                carries=lambda data: has_env_secret(data, agent.name) is wire,
                already_open=WIRING_PR_ALREADY_OPEN,
                reprovision=reprovision,
            )
        except DeployTriggerFailed as exc:
            return _deploy_trigger_failed(agent, exc)
        except RepoBusy as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except ClientError as exc:
            return _lane_failed("wiring", agent, exc, SECRETS_WIRING_FAILED)

    def _deploy_trigger_failed(agent: Agent, exc: DeployTriggerFailed) -> JSONResponse:
        """The platform would not regenerate. Which sentence depends on
        whether the agent's files actually changed: telling somebody
        "couldn't save" about a file that IS saved sends them to redo work
        that is already done."""
        log.warning(
            "deploy trigger failed for agent %s (%s)", agent.name, type(exc).__name__
        )
        return JSONResponse(
            {"error": DEPLOY_TRIGGER_FAILED if exc.pushed else DEPLOY_TRIGGER_ONLY},
            status_code=502,
        )
    @app.get("/api/agents/{name}/env")
    def list_agent_env(
        name: str,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        """This agent's plain environment variables, keys and values.

        Read straight off the repository's `gojira.yaml` rather than from
        anything the room caches, because the file is where they live and a
        team may have edited it themselves. The two model-access variables
        the room writes are left out: they are not an owner's to change,
        and showing them in an editable list would say otherwise.

        A file the room cannot PARSE, and a repository the room cannot
        READ, are two different 409s with two different sentences. The
        first is the team's to fix; the second means the room lost access,
        and answering "there is no file" for it would send somebody
        hunting for one sitting right there.
        """
        agent = _secrets_agent_or_error(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent

        try:
            env = read_repo_env(app.state.clients.github, agent.repo_full_name)
        except RepoBusy as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except ClientError as exc:
            return _lane_failed("file read", agent, exc, AGENT_FILES_UNREACHABLE)

        return JSONResponse({"env": env})

    @app.put("/api/agents/{name}/env")
    def set_agent_env(
        name: str,
        payload: AgentEnvWrite,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        """Add, change or remove this agent's plain environment variables.

        The same body shape and the same key rules as the secrets route
        above, and the same two ways the change lands: committed to a
        composed agent's own repository, proposed as a pull request against
        a connected agent's. `wiring` says which, and `env` comes back as
        what the file now holds.

        Values ARE returned here, unlike the route above, and that is the
        difference between the two lists rather than an oversight: this one
        is for values that are not sensitive, and a list you cannot read is
        not one you can edit.
        """
        agent = _secrets_agent_or_error(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent

        clients = app.state.clients
        try:
            current = read_repo_env(clients.github, agent.repo_full_name)
        except RepoBusy as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except ClientError as exc:
            return _lane_failed("file read", agent, exc, AGENT_FILES_UNREACHABLE)

        refusal = write_refusal(payload.set, payload.remove, sorted(current))
        if refusal is not None:
            return JSONResponse({"error": refusal}, status_code=422)

        # Worked out here so the response can say what the file now
        # carries without a second read: `apply_env` is the same function
        # the edit below runs, over the same starting point.
        after: dict = {"env": dict(current)}
        apply_env(after, payload.set, payload.remove)

        try:
            result = edit_gojira_yaml(
                clients.github,
                clients.gojira,
                repo=agent.repo_full_name,
                connected=is_connected(agent),
                gojira_app_id=agent.gojira_app_id,
                edit=lambda data: apply_env(data, payload.set, payload.remove),
                message="kobo: environment variables",
            )
        except DeployTriggerFailed as exc:
            return _deploy_trigger_failed(agent, exc)
        except RepoBusy as exc:
            # Something a person can clear, named so they can: a pull
            # request the room opened before, or a file nothing can parse.
            return JSONResponse({"error": str(exc)}, status_code=409)
        except ClientError as exc:
            return _lane_failed("file write", agent, exc, AGENT_FILES_UNREACHABLE)

        body: dict = {"env": after["env"], "wiring": result.how}
        if result.pull_request is not None:
            body["pull_request"] = result.pull_request
        return JSONResponse(body)

    @app.post("/api/agents/{name}/restart")
    def restart_agent(
        name: str,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        """Roll this agent's pods now (design 2026-09-23).

        A secret reaches a running agent at its next boot and not before,
        which used to leave an owner with a stored credential, a panel
        saying it was saved, and no way to make either matter. This is the
        nudge, and it is the SAME primitive the tooling fast lane already
        fires for the same reason (`_request_tool_restart`): a rollout
        restart of the agent's own Deployment in its own namespace, both
        named after the agent.

        Composed and connected alike. Nothing here touches a repository,
        so the one rule that separates those two kinds -- the room never
        pushes to a team's branches -- has nothing to say about it.

        Counted per caller. It is the one control on this panel that costs
        the agent something: its pods go down and come back, and a person
        holding a button down should not be able to keep an agent
        restarting.
        """
        agent = _ready_agent_or_error(
            session, request, name, not_ready_message=NOT_DEPLOYED_YET
        )
        if isinstance(agent, JSONResponse):
            return agent
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        caller = getattr(request.state, "iap_email", None) or agent.created_by or ""
        if not rate_limit.check(caller, limit=RESTART_LIMIT, bucket=RESTART_BUCKET):
            return JSONResponse({"error": TOO_MANY_RESTARTS}, status_code=429)

        try:
            app.state.clients.kubectl.restart_deploy(agent.name, agent.name)
        except ClientError as exc:
            # The exception's CLASS, never its text: kubectl's own failure
            # message is built from the command's output, and this panel's
            # whole discipline is that nothing from the cluster side is
            # repeated into a log or a response.
            log.warning(
                "restart failed for agent %s (%s)", agent.name, type(exc).__name__
            )
            return JSONResponse({"error": RESTART_FAILED}, status_code=502)

        # The room's record of who rolled somebody's agent. The MCP surface
        # writes a row of its own for the same call (`kobo_api.mcp.audit`);
        # this is the line for the panel's half.
        log.info("restart requested for agent %s by %s", agent.name, caller or "nobody")
        return JSONResponse({"restarted": True, "deployment": agent.name})

    # --- Runtime (#15): move an existing agent to a different runtime image ----

    @app.patch("/api/agents/{name}/runtime")
    def update_agent_runtime(
        name: str,
        payload: AgentRuntimePatch,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        """Re-point an agent at a different runtime image without destroying
        it. Agents are built on the org default, so this is how an operator
        pins one to a specific tag or digest of it, and how an agent created
        on a runtime the room has since retired is carried back onto it. The
        alternative is destroy-and-recreate, which discards Slack wiring
        (whose tokens are supplied by hand — `wire_slack` cannot create a
        Slack app), accumulated agent memory, and channel membership. None
        of that is a runtime concern.

        Owner-or-operator and ready-gated, same gate as the metadata route
        below: this mutates the agent's repo and rolls its pod.

        NO SHORT-CIRCUIT WHEN THE VALUE IS UNCHANGED, deliberately. The row
        and the Dockerfile are two stores and can disagree — that is the
        whole failure this route's ordering exists to prevent — so asking
        for the image an agent already has is a legitimate REPAIR, and
        `update_dockerfile_runtime_image` is idempotent, so a genuinely
        in-sync agent pushes nothing and gets its unchanged head back.
        """
        cfg: Config = request.app.state.config
        agent = _ready_agent_or_error(
            session, request, name, not_ready_message="agent is not ready for a runtime change yet"
        )
        if isinstance(agent, JSONResponse):
            return agent
        blocked = _room_edits_this_agent_or_409(agent)
        if blocked is not None:
            return blocked
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        runtime_image = payload.runtime_image.strip()
        if not runtime_image:
            return JSONResponse({"error": "runtime_image can't be blank"}, status_code=422)
        rejection = validate_runtime_image(runtime_image, org_default=cfg.runtime_image)
        if rejection is not None:
            return JSONResponse({"error": rejection}, status_code=422)

        # THE ROW MOVES FIRST, IN MEMORY, AND IS ONLY COMMITTED IF THE PUSH
        # LANDS. `_runtime_image_for_repo` reads this column, and four other
        # write paths (wire_slack, upload_intake_file, and both tooling
        # uploads) pass its value back into `update_dockerfile_runtime_image`
        # on their own commits. So a file moved without the row is not
        # merely untidy: the next teach note or tool upload re-pins the
        # Dockerfile to the stale row value and the generated CI hard-rotates
        # the pod onto it, silently undoing this change. Ordering per
        # `update_agent_metadata` below: return before `session.commit()` on
        # a delivery failure and `get_session`'s rollback-on-close discards
        # the in-memory mutation, so a failed push leaves both stores at the
        # value they already agreed on.
        previous = agent.runtime_image
        agent.runtime_image = runtime_image
        try:
            sha = commit_runtime_image(app.state.clients.github, agent.repo_full_name, runtime_image)
        except ClientError as exc:
            log.warning("runtime image commit failed for %s: %s", name, exc)
            return JSONResponse(
                {"error": "couldn't change the runtime image. Try again."}, status_code=502
            )

        session.commit()
        return JSONResponse(
            {"runtime_image": runtime_image, "previous_runtime_image": previous, "sha": sha}
        )

    # --- Metadata (persona-metadata plan, Task 3): capability earned + card lockstep --

    @app.patch("/api/agents/{name}/metadata")
    def update_agent_metadata(
        name: str,
        payload: AgentMetadataPatch,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        # Owner-or-operator now (access-control Task A3) -- this route used
        # to be ready-gated only, "everyone in the room" reach same as the
        # Slack channels lane; both moved behind the new gate together.
        agent = _ready_agent_or_error(
            session, request, name, not_ready_message="agent is not ready for directory changes yet"
        )
        if isinstance(agent, JSONResponse):
            return agent
        blocked = _room_edits_this_agent_or_409(agent)
        if blocked is not None:
            return blocked
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        team = agent.team
        if payload.team is not None:
            if not TEAM_SLUG_RE.match(payload.team):
                return JSONResponse(
                    {"error": "that doesn't look like a team name"}, status_code=422
                )
            team = payload.team

        tags = decode_tags(agent.purpose_tags)
        if payload.purpose_tags is not None:
            cleaned_tags = clean_tags(payload.purpose_tags)
            if isinstance(cleaned_tags, str):
                return JSONResponse({"error": cleaned_tags}, status_code=422)
            tags = cleaned_tags

        capability = agent.capability
        if payload.capability is not None:
            cleaned_capability = clean_capability_text(payload.capability)
            if cleaned_capability is None:
                return JSONResponse(
                    {"error": "capability must be plain text, 160 characters or fewer"},
                    status_code=422,
                )
            capability = cleaned_capability

        agent.team = team
        agent.purpose_tags = encode_tags(tags)
        agent.capability = capability

        try:
            sha = commit_card_metadata(
                app.state.clients.github,
                agent.repo_full_name,
                team=team,
                purpose_tags=tags,
                capability=capability,
            )
        except ClientError as exc:
            log.warning("metadata card commit failed: %s", exc)
            # Return before session.commit() — same 502-consistency idiom as
            # save_slack_channels above: the in-memory team/purpose_tags/
            # capability mutations made above never reach the DB (get_session's
            # implicit rollback-on-close), so a follow-up GET still shows
            # whatever was stored before this PATCH.
            return JSONResponse(
                {"error": "couldn't update the directory details. Try again."},
                status_code=502,
            )

        session.commit()
        # Best-effort switchboard resync (colleagues switchboard plan, Task
        # 2): the card just committed above is now the source of truth, so a
        # failure here is logged and swallowed, never turned into a failure
        # response (see _sync_directory_best_effort's own docstring).
        _sync_directory_best_effort(app.state.clients, app.state.config, agent)
        return JSONResponse(
            {"team": team, "purpose_tags": tags, "capability": capability, "sha": sha}
        )

    @app.post("/api/agents/{name}/metadata/regenerate")
    def regenerate_agent_metadata(
        name: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(
            session, request, name, not_ready_message="agent is not ready for directory changes yet"
        )
        if isinstance(agent, JSONResponse):
            return agent
        blocked = _room_edits_this_agent_or_409(agent)
        if blocked is not None:
            return blocked
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        clients = app.state.clients
        cfg: Config = app.state.config
        engine = session.get_bind()

        try:
            persona_body = _resolve_persona_body(cfg, clients.storage, engine, agent)
        except RuntimeError as exc:
            log.warning("metadata regenerate persona lookup failed for %s: %s", name, exc)
            return JSONResponse(
                {"error": "couldn't read this agent's persona. Try again."}, status_code=502
            )

        skill_descriptions = skill_descriptions_for_agent(engine, agent)
        tooling_filenames = tooling_filenames_for(
            clients.github,
            clients.kubectl,
            clients.agent,
            repo=agent.repo_full_name,
            namespace=agent.name,
            deploy=agent.name,
        )
        capability = generate_capability(
            clients.name, persona_body, skill_descriptions, tooling_filenames
        )

        team = agent.team
        tags = decode_tags(agent.purpose_tags)
        agent.capability = capability

        try:
            sha = commit_card_metadata(
                clients.github, agent.repo_full_name, team=team, purpose_tags=tags,
                capability=capability,
            )
        except ClientError as exc:
            log.warning("metadata regenerate card commit failed: %s", exc)
            # Same 502-consistency idiom as the PATCH route above: return
            # before session.commit() so the in-memory `agent.capability`
            # regeneration never reaches the DB.
            return JSONResponse(
                {"error": "couldn't update the directory details. Try again."},
                status_code=502,
            )

        session.commit()
        # Best-effort switchboard resync, same call/reasoning as the PATCH
        # route above.
        _sync_directory_best_effort(clients, cfg, agent)
        # Roster refresh lane (Task 6): this route is the other place (besides
        # HelloStep's own tail, kobo_api.steps._earn_capability) capability
        # actually DISTILLS -- `generate_capability` ran just above, not a
        # caller-typed string (contrast the PATCH route above, which never
        # triggers this). The room's regenerated view of this agent reaches
        # every viewer who can see it, and its own self.md is redelivered to
        # itself. Best-effort and switchboard-gated internally; wrapped here
        # too since, unlike the pipeline's background thread, an uncaught
        # exception in a request handler would turn this already-committed
        # regenerate into a 500 instead of the 200 it earned.
        try:
            refresh_after_capability_distill(clients, cfg, session, agent)
        except Exception:
            log.warning(
                "roster/self-knowledge refresh failed for agent %s after capability "
                "regenerated",
                agent.name,
                exc_info=True,
            )
        return JSONResponse(
            {"team": team, "purpose_tags": tags, "capability": capability, "sha": sha}
        )

    # --- Directory sync (colleagues switchboard plan, Task 2): on-demand ------
    # re-registration/backfill route, ready-gated. Owner-or-operator now
    # (access-control Task A3 fix round 1) -- this route used to be "any
    # signed-in user, same reach as the metadata routes above", the initial
    # A3 sweep simply missed it; re-syncing an already-registered agent is
    # idempotent either way, but it's gated for uniformity with every other
    # mutating agent route rather than carrying its own carve-out.

    @app.post("/api/agents/{name}/directory/sync")
    def sync_directory_route(
        name: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(
            session, request, name, not_ready_message="agent is not ready to join the directory yet"
        )
        if isinstance(agent, JSONResponse):
            return agent
        blocked = _room_edits_this_agent_or_409(agent)
        if blocked is not None:
            return blocked
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        cfg: Config = app.state.config
        if not cabal_enabled(cfg):
            return JSONResponse(
                {"error": "the switchboard is not configured"}, status_code=409
            )

        clients = app.state.clients
        try:
            sync_directory(clients.github, clients.cabal, cfg, agent)
        except ClientError as exc:
            log.warning("directory sync route failed for %s: %s", name, exc)
            return JSONResponse(
                {"error": "couldn't sync the directory. Try again."}, status_code=502
            )

        return JSONResponse({"registered": True})

    # --- Colleagues: the watching window (Task 4/B4) ---------------------------
    #
    # Three read-only proxy routes over the switchboard's own admin
    # conversation surface, room-wide (not per-agent) -- any signed-in user
    # may read them (there's no single agent here to own), gated only by
    # `_colleagues_gate` (the switchboard being wired at all). Unlike
    # directory-sync above, these were never in scope for the owner-or-
    # operator gate: read surfaces never call it (see its own docstring),
    # and there's no per-agent argument here to check ownership against.
    # A 409 here is treated by the UI as "this room has no switchboard" and
    # renders nothing (silent absence), never a scary error banner — see
    # ColleagueConversations.svelte.

    @app.get("/api/colleagues/conversations")
    def list_colleague_conversations(
        all: bool = False, limit: int | None = None, agent: str | None = None
    ) -> JSONResponse:
        cfg: Config = app.state.config
        clients = app.state.clients
        gate = _colleagues_gate(cfg, clients)
        if gate is not None:
            return gate

        try:
            conversations = clients.cabal.list_conversations(limit=limit)
        except (CabalError, httpx.HTTPError) as exc:
            log.warning("colleague conversations list failed: %s", exc)
            return JSONResponse(
                {"error": "The switchboard did not answer. Try again shortly."},
                status_code=502,
            )

        if not all:
            # Default view: only the rows an agent itself started (asking a
            # colleague for help) — a room-human chat or a service caller
            # (e.g. `service:kobo` from best-effort directory syncs) is
            # noise here unless the operator explicitly asks to see it via
            # `all=true`.
            conversations = [
                c for c in conversations
                if str(c.get("caller_agent", "")).startswith("agent:")
            ]
        if agent is not None:
            # Conversation scoping (access-tab refinement wave): layered ON
            # TOP of the all/agent-only filtering above, never a substitute
            # for it -- both must hold for a row to survive.
            conversations = [
                c for c in conversations if _conversation_has_participant(c, agent)
            ]

        # Thread exclusion (Critical 3 fix, the thread-transcript leak):
        # this row shape (CabalClient.list_conversations) carries no
        # `metadata` field at all, so the only way to know a row is a
        # thread turn is to fetch its full detail and run the same
        # `_conversation_detail_is_thread` check every other surface
        # shares -- the same per-row `get_conversation` cost `colleague_
        # feed` already pays for every row it renders. A thread is a
        # private, initiator-only conversation (operator ruling) and must
        # never surface here, regardless of the all/agent filtering above:
        # `all=true` is exactly the request that used to leak a thread's
        # own operator-caller row (a bare `all=false` already excludes it,
        # since a thread relay's caller is `operator:<email>`, never
        # `agent:...` -- but that's an accident of the default filter, not
        # a thing to rely on, hence this unconditional final pass).
        visible: list[dict] = []
        for row in conversations:
            task_id = row.get("task_id")
            if not isinstance(task_id, str):
                visible.append(row)
                continue
            try:
                detail = clients.cabal.get_conversation(task_id)
            except (CabalError, httpx.HTTPError) as exc:
                log.warning(
                    "colleague conversations thread-check failed for %s: %s", task_id, exc
                )
                return JSONResponse(
                    {"error": "The switchboard did not answer. Try again shortly."},
                    status_code=502,
                )
            if not _conversation_detail_is_thread(detail):
                visible.append(row)

        return JSONResponse({"conversations": visible})

    @app.get("/api/colleagues/conversations/{task_id}")
    def get_colleague_conversation(task_id: str, agent: str | None = None) -> JSONResponse:
        cfg: Config = app.state.config
        clients = app.state.clients
        gate = _colleagues_gate(cfg, clients)
        if gate is not None:
            return gate

        try:
            detail = clients.cabal.get_conversation(task_id)
        except (CabalError, httpx.HTTPError) as exc:
            log.warning("colleague conversation detail failed for %s: %s", task_id, exc)
            return JSONResponse(
                {"error": "The switchboard did not answer. Try again shortly."},
                status_code=502,
            )

        if _conversation_detail_is_thread(detail):
            # Thread exclusion (Critical 3 fix): the same not-found answer
            # a bogus task_id gets, never a distinguishable 403 or a
            # 200-with-a-warning -- a thread is a private, initiator-only
            # conversation (operator ruling) that must never surface here,
            # for any caller, regardless of ?agent=.
            return JSONResponse({"error": f"no such conversation: {task_id!r}"}, status_code=404)

        if agent is not None and not _conversation_has_participant(
            detail.get("conversation") or {}, agent
        ):
            # Conversation scoping (access-tab refinement wave): a
            # conversation `agent` doesn't participate in reads as "not
            # found" for this filtered view, same existence-hiding
            # discipline `_visible_agent_or_404` uses elsewhere -- this is
            # a scoping view, not an authorization boundary in itself (the
            # 409 gate above already covers whether colleagues data may be
            # read at all).
            return JSONResponse({"error": f"no such conversation: {task_id!r}"}, status_code=404)

        return JSONResponse(detail)

    @app.get("/api/colleagues/stream", response_model=None)
    async def stream_colleague_events(agent: str | None = None) -> StreamingResponse | JSONResponse:
        """SSE passthrough of the switchboard's live event feed.

        The 409 gate below (`_colleagues_gate`) covers a room with no
        switchboard wired at all. Beyond that, `CabalClient.stream_events`
        (kobo_api.clients) owns every other failure mode itself — a non-2xx
        upstream response or a transport error simply ends the generator,
        never raises — so this route never wraps the `StreamingResponse` in
        its own try/except: the operator token this call carries under the
        hood never has a path to end up in anything sent to the browser,
        since the response's status/headers (fixed 200, `text/event-stream`)
        are committed before the upstream connection is even attempted, and
        the body is nothing but a raw byte passthrough of the switchboard's
        own frames.

        Fix round 1: this route function is now `async def` and
        `CabalClient.stream_events` is an async generator, so Starlette's
        `StreamingResponse` consumes it directly on the event loop
        (`isinstance(content, AsyncIterable)`), never via
        `iterate_in_threadpool`. That's the whole point of the change: a
        client disconnect now cancels the awaited read promptly instead of
        leaving a worker thread blocked on a sync socket read until
        upstream happens to send more bytes.

        Conversation scoping (access-tab refinement wave): `?agent=` wraps
        the raw passthrough in `filter_cabal_event_stream` (kobo_api.
        clients), dropping every frame that isn't attributable to a
        conversation `agent` participates in before it ever reaches the
        browser -- see that function's own docstring for exactly how
        "attributable" is decided.

        Critical 3 fix (thread leak): `filter_cabal_event_stream` now runs
        UNCONDITIONALLY, `agent` passed through as-is (`None` when
        omitted) -- it always excludes thread-tagged events regardless of
        `agent`, and additionally scopes to `agent`'s own conversations
        when given. Before this fix, omitting `?agent=` skipped the
        wrapper entirely, so every thread turn's dispatch events -- full
        transcript included, since a thread turn resends the whole
        conversation on every relay -- reached this stream unfiltered for
        any signed-in caller. The stream is no longer byte-for-byte
        untouched when `agent` is omitted: thread-tagged frames are always
        stripped now, the one exception to "otherwise untouched."
        """
        cfg: Config = app.state.config
        clients = app.state.clients
        gate = _colleagues_gate(cfg, clients)
        if gate is not None:
            return gate

        chunks = clients.cabal.stream_events()
        chunks = filter_cabal_event_stream(chunks, agent)
        return StreamingResponse(chunks, media_type="text/event-stream")

    @app.get("/api/colleagues/feed")
    def colleague_feed(
        agent: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        """The terminal's one flattened, chronological feed of `agent`'s own
        colleague talk (terminal redesign, replacing the per-conversation
        card grid): every conversation `agent` participates in, either side,
        flattened into `{"ts", "conversation_id", "from", "to", "role",
        "text"}` entries (`_flatten_conversation_entries` above) and sorted
        ascending by `ts` across every conversation -- not per-conversation,
        the whole room-wide scrollback interleaved as it actually happened.
        Capped to the most recent `_FEED_SCROLLBACK_CAP` (200) entries;
        `truncated` is true whenever more existed than that.

        Fix round 2: scope AT THE SWITCHBOARD, cap after. Round 1 ("scope
        first, cap after") called `list_conversations` with no `limit`,
        believing that meant unbounded -- it never did: the switchboard's
        route DEFAULTS to 50 rows room-wide and clamps any explicit limit
        to 200 (found by the terminal review, reproduced against the cabal
        source), so the quiet-agent crowd-out round 1 claimed to fix was
        quietly back, worse. The fetch below is now TWO server-side-scoped
        queries -- `target=agent` (conversations `agent` answered) and
        `caller=agent:<agent>` (the ones it started; cabal #128 added the
        caller filter), merged and deduped by `task_id` -- so no other
        agent's traffic can ever crowd `agent`'s history out of its own
        feed, and the room-wide window stops mattering entirely.
        `_conversation_has_participant` still runs as a belt, and only
        `agent`'s own scoped, flattened entries are measured against
        `_FEED_SCROLLBACK_CAP`, so `truncated` keeps meaning "`agent`'s OWN
        history overflowed", never the room's.

        Residual bound, stated honestly: each scoped query is still capped
        at the switchboard's 200-row clamp, PER DIRECTION -- an agent with
        more than 200 conversations in one direction under-fetches its
        oldest ones (~400 entries fetched vs. the 200-entry display cap, so
        the terminal itself stays full). The completing fix is pagination
        at the cabal admin API, tracked upstream -- NOT re-widening to a
        room-wide fetch here.

        `agent` is REQUIRED (a plain, non-Optional `str` -- FastAPI 422s a
        request that omits it, before this body ever runs) and is resolved
        through `_visible_agent_or_404`, the room's one visibility choke
        point -- UNLIKE the three room-wide proxy routes right above
        (`list_colleague_conversations`/`get_colleague_conversation`/
        `stream_colleague_events`), which read only snippets, or full text
        gated solely on the switchboard being wired at all, regardless of
        which agents the caller can otherwise see. This route hands back
        FULL, untruncated conversation text -- a strictly bigger exposure
        than a 120-char snippet -- so it additionally follows the same
        per-agent visibility every other agent-scoped route in this file
        already enforces: a caller who cannot see `agent` gets the same 404
        as if it didn't exist, never a peek at its colleague talk by name
        alone. This is a deliberate widening of what's gated, not a
        loosening of anything the older routes already allowed.

        Fail-soft on any cabal trouble, the house 502 copy convention: a
        failed `list_conversations` or a failed `get_conversation` partway
        through the loop below both answer with the same plain "The
        switchboard did not answer. Try again shortly." -- no partial feed
        is ever assembled from a request that didn't fully succeed.
        """
        visible_agent = _visible_agent_or_404(session, request, agent)
        if isinstance(visible_agent, JSONResponse):
            return visible_agent

        cfg: Config = app.state.config
        clients = app.state.clients
        gate = _colleagues_gate(cfg, clients)
        if gate is not None:
            return gate

        try:
            # Two server-side-scoped queries (see the docstring's "Fix
            # round 2"): the conversations `agent` answered, and the ones
            # it started. limit=200 is the switchboard's own clamp maximum,
            # passed explicitly because OMITTING it means 50, not
            # unbounded. Merged + deduped by task_id (a conversation can
            # never genuinely match both filters, but dedupe is cheap and
            # a duplicated row would double its feed lines).
            incoming = clients.cabal.list_conversations(limit=200, target=agent)
            outgoing = clients.cabal.list_conversations(limit=200, caller=f"agent:{agent}")
        except (CabalError, httpx.HTTPError) as exc:
            log.warning("colleague feed list failed for %s: %s", agent, exc)
            return JSONResponse(
                {"error": "The switchboard did not answer. Try again shortly."},
                status_code=502,
            )

        seen_ids: set[str] = set()
        conversations: list[dict] = []
        for row in incoming + outgoing:
            task_id = row.get("task_id")
            if isinstance(task_id, str) and task_id in seen_ids:
                continue
            if isinstance(task_id, str):
                seen_ids.add(task_id)
            conversations.append(row)

        # Belt: the switchboard already scoped both queries, but this
        # room-side predicate is the single definition of "participates",
        # and a filter regression upstream must not widen the feed.
        scoped = [c for c in conversations if _conversation_has_participant(c, agent)]

        entries: list[dict] = []
        for row in scoped:
            task_id = row.get("task_id")
            if not isinstance(task_id, str):
                continue
            try:
                detail = clients.cabal.get_conversation(task_id)
            except (CabalError, httpx.HTTPError) as exc:
                log.warning(
                    "colleague feed detail failed for %s (agent=%s): %s", task_id, agent, exc
                )
                return JSONResponse(
                    {"error": "The switchboard did not answer. Try again shortly."},
                    status_code=502,
                )
            entries.extend(_flatten_conversation_entries(detail))

        entries.sort(key=lambda e: e["ts"])
        truncated = len(entries) > _FEED_SCROLLBACK_CAP
        if truncated:
            entries = entries[-_FEED_SCROLLBACK_CAP:]

        return JSONResponse({"entries": entries, "truncated": truncated})

    # --- Avatar (P2d): persona-derived face, repo-backed with a display cache ---

    @app.post("/api/agents/{name}/avatar")
    def generate_avatar(
        name: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(
            session, request, name, not_ready_message="agent is not ready for an avatar yet"
        )
        if isinstance(agent, JSONResponse):
            return agent
        blocked = _room_edits_this_agent_or_409(agent)
        if blocked is not None:
            return blocked
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        clients = app.state.clients
        if clients.image is None:
            return JSONResponse(
                {"error": "picture generation is not available"}, status_code=503
            )

        # Same catalog-lookup-by-persona-id idiom as create_slack_app's
        # persona_first_line above (the FTS entry's description, looked up by
        # agent.persona), with the same id-derived fallback for a persona
        # that's since been removed from the catalog.
        engine = session.get_bind()
        _maybe_refresh_catalogs(app, engine)
        persona_entry = get_entry(engine, "persona", agent.persona, team=None)
        if persona_entry is not None:
            persona_description = persona_entry.description
        else:
            log.warning("persona %r not in the catalog, using generic text for avatar prompt", agent.persona)
            persona_description = f"A {agent.persona} agent."

        prompt = avatar_prompt(agent.name, persona_description, decode_tags(agent.purpose_tags))

        try:
            png = clients.image.generate(prompt)
        except ClientError as exc:
            log.warning("avatar generation failed for %s: %s", name, exc)
            return JSONResponse({"error": str(exc)}, status_code=502)

        # Size cap enforced BEFORE any store (repo push or DB cache) — a
        # backend-result problem, not a user-input one, hence 502 not 422.
        if len(png) > AVATAR_MAX_BYTES:
            log.warning(
                "generated avatar for %s exceeded the %d-byte cap (%d bytes)",
                name, AVATAR_MAX_BYTES, len(png),
            )
            return JSONResponse(
                {"error": "that picture came out too large. Try again."}, status_code=502
            )

        try:
            sha = set_avatar(clients.github, repo=agent.repo_full_name, png=png)
        except ClientError as exc:
            # The push side's own failure mode (repo unreachable, auth
            # lapsed, ...) — distinct from the image-generation failure
            # above, so it gets the same generic "couldn't reach your
            # agent's files" family of message the knowledge/Slack routes
            # use for this exact class of GitHub-side failure, rather than
            # this exception's own message.
            log.warning("avatar push failed for %s: %s", name, exc)
            return JSONResponse(
                {"error": "couldn't save your agent's picture. Try again."}, status_code=502
            )

        agent.avatar_png = base64.b64encode(png).decode("ascii")
        session.commit()

        return JSONResponse({"sha": sha})

    @app.put("/api/agents/{name}/avatar")
    async def upload_avatar(
        name: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        """Human-provided counterpart to `generate_avatar` above: the human
        supplies the PNG bytes directly (a raw body, Content-Type image/png
        — no JSON envelope, the whole body IS the picture) rather than one
        being generated from the persona. No image client involved at all,
        so this route works even in a room with `clients.image is None`.

        Same ready gate and not-ready copy as generate_avatar, same store
        (repo push via `set_avatar` + the `avatar_png` DB cache), and the
        same `{"sha": ...}` response shape — every difference from
        generate_avatar is in validating what came over the wire, since
        (unlike generate_avatar's model-produced bytes) this body is
        untrusted input: wrong Content-Type, not actually PNG-shaped
        (magic-bytes check, which an empty body also fails), or too big all
        get a plain-copy 422 rather than generate_avatar's backend-result
        502 for its own size cap.
        """
        agent = _ready_agent_or_error(
            session, request, name, not_ready_message="agent is not ready for an avatar yet"
        )
        if isinstance(agent, JSONResponse):
            return agent
        blocked = _room_edits_this_agent_or_409(agent)
        if blocked is not None:
            return blocked
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != "image/png":
            return JSONResponse({"error": "PNG only for now."}, status_code=422)

        png = await request.body()
        if not png.startswith(PNG_SIGNATURE):
            return JSONResponse({"error": "PNG only for now."}, status_code=422)

        if len(png) > AVATAR_UPLOAD_MAX_BYTES:
            return JSONResponse(
                {"error": "That image is too large. Two megabytes at most."}, status_code=422
            )

        clients = app.state.clients
        try:
            sha = set_avatar(clients.github, repo=agent.repo_full_name, png=png)
        except ClientError as exc:
            # Same push-side failure family (and same copy) as
            # generate_avatar's own 502 above.
            log.warning("avatar upload push failed for %s: %s", name, exc)
            return JSONResponse(
                {"error": "couldn't save your agent's picture. Try again."}, status_code=502
            )

        agent.avatar_png = base64.b64encode(png).decode("ascii")
        session.commit()

        return JSONResponse({"sha": sha})

    @app.get("/api/agents/{name}/avatar")
    def get_avatar(
        name: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> Response:
        # Deliberately no READY gate here (unlike every route above): a
        # provisioning agent's page may still render, and an <img> tag just
        # gets a 404 until a cache exists — the guard would only block a
        # legitimate "not there yet" read the frontend is designed to tolerate.
        # Visibility IS still enforced (access-control Task B2): a picture is
        # as much "the agent's stuff" as any other field, and existence is
        # itself information.
        agent = _visible_agent_or_404(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent

        if agent.avatar_png is None:
            return JSONResponse({"error": "no picture yet"}, status_code=404)

        png = base64.b64decode(agent.avatar_png)
        return Response(content=png, media_type="image/png")

    # --- Cross-group colleagueship (access-control arc, Task C1) -----------
    #
    # Invites: `A2AInvite` rows recording a request from one agent (the FROM
    # side) to become an allowed dispatch pair with another (the TO side)
    # outside the pair same-group talk already covers implicitly (see
    # kobo_api.pairs.allowed_pairs, the pure function a later task syncs to
    # the switchboard). Grants: `AgentUserGrant` rows extending an agent's
    # VISIBILITY (not dispatch -- there is no such thing as a human dispatch
    # pair) to a named human, wired straight into `_visible_agents` above so
    # the room keeps exactly one visibility choke point.
    #
    # `_agent_ref`/`_invite_repr` below are the one JSON shape every
    # invite-returning route shares, so the leak-safety rule (an invite may
    # only ever reveal a NAME and a GROUP about the side the caller doesn't
    # already have full standing on) lives in one place, not once per route.

    def _agent_ref(session: Session, name: str) -> dict:
        """A minimal, leak-safe reference to the agent named `name`: its
        name plus its group, nothing else. Fetched fresh, WITHOUT the
        visibility filter -- deliberately: an invite's TO agent is allowed
        to be outside the caller's own visible set (requesting access to a
        visible-in-the-directory-but-not-yours agent is the feature this
        arc ships, spec section 5 -- "the a2a directory is deliberately
        global," so a bare NAME is never itself a leak), but nothing beyond
        name and group about that agent may ride along in the response.
        `name` may no longer resolve to a row at all (the agent was
        destroyed after the invite was created) -- `group_name` reads as
        `None` in that case, same as a row that was always group-less, so
        an invite's history stays renderable rather than erroring on a
        vanished endpoint.
        """
        agent = session.query(Agent).filter_by(name=name).first()
        return {"name": name, "group_name": agent.group_name if agent is not None else None}

    def _invite_repr(session: Session, invite: A2AInvite) -> dict:
        """The one response shape every invite-returning route below uses.
        `from_agent` renders as a bare name (the caller of every route that
        can see this invite at all is already entitled to full standing on
        the FROM side -- its own owner/operator for create/list, or the
        equally-entitled TO side for accept/decline/revoke, which never
        needs more than the name to identify who's asking). `to_agent`
        renders through `_agent_ref` -- the side that may sit outside the
        caller's visibility, capped to name+group as that function's own
        docstring covers.
        """
        return {
            "id": invite.id,
            "from_agent": invite.from_agent,
            "to_agent": _agent_ref(session, invite.to_agent),
            "status": invite.status,
            "requested_by": invite.requested_by,
            "decided_by": invite.decided_by,
            "created_at": invite.created_at.isoformat(),
            "decided_at": (
                invite.decided_at.isoformat() if invite.decided_at is not None else None
            ),
        }

    def _team_pairs(session: Session, agent: Agent) -> list[dict]:
        """The OTHER ready agents implicitly auto-paired with `agent`
        through shared group membership (`kobo_api.pairs.allowed_pairs`'s
        own same-group rule, via the shared `implicitly_paired` predicate
        -- fix round, shared-group wave HIGH) -- access-tab refinement
        wave, read-only facts for the invites view: these colleagues
        already talk freely with `agent`, no invite ever needed or even
        possible between them, so they're surfaced here rather than as a
        revocable invite row. Empty when `agent` has no group (no implicit
        pairing without one), has no OTHER ready agent sharing it, or is
        namespaced into `SHARED_GROUP` -- the commons is humanly open but
        its agents' wires stay invite-gated, so `implicitly_paired` says
        `False` even for two commons agents sharing that exact name, and
        this must not lie that they "talk freely" when they in fact do not.

        Fetched fresh, WITHOUT the visibility filter -- same discipline as
        `_agent_ref` above: whoever can see `agent`'s invites at all (its
        owner or a room operator) already has full standing to know its
        own auto-paired colleagues, so a bare name+group here is never a
        leak (the same "the a2a directory is deliberately global" reasoning
        `_agent_ref` documents, spec section 5).
        """
        if agent.group_name is None:
            return []
        others = (
            session.query(Agent)
            .filter(
                Agent.group_name == agent.group_name,
                Agent.name != agent.name,
                Agent.status == AgentStatus.READY,
            )
            .order_by(Agent.id)
            .all()
        )
        return [
            {"name": other.name, "group": other.group_name}
            for other in others
            if implicitly_paired(agent.group_name, other.group_name)
        ]

    def _no_such_invite_404(invite_id: int) -> JSONResponse:
        """The one 404 body/status every invite-id route (accept/decline/
        revoke) returns for an id with nothing real behind it -- shared so
        `_fetch_invite_or_404`'s missing-id case and the no-standing case
        below (folds 1+2, C1 review) are byte-identical, never two call
        sites that could drift apart.
        """
        return JSONResponse({"error": f"no such invite: {invite_id}"}, status_code=404)

    def _fetch_invite_or_404(session: Session, invite_id: int) -> A2AInvite | JSONResponse:
        invite = session.get(A2AInvite, invite_id)
        if invite is None:
            return _no_such_invite_404(invite_id)
        return invite

    def _invite_side_stand_in(session: Session, agent_name: str) -> Agent | SimpleNamespace:
        """The named agent, or a bare `created_by=None` stand-in when it no
        longer exists (destroyed since the invite was created) -- the
        fail-closed idiom every invite-id route below shares: a vanished
        side is operator-only to claim standing on, never nobody's-problem
        or everybody's.
        """
        agent = session.query(Agent).filter_by(name=agent_name).first()
        return agent if agent is not None else SimpleNamespace(created_by=None)

    def _invite_side_gate(session: Session, request: Request, agent_name: str) -> JSONResponse | None:
        """`_owner_or_operator_or_403` against the named invite side, through
        the stand-in above -- `None` when the caller has standing on that
        side, else the verbatim forbidden-mutation 403 body. The one place
        every invite-id route below asks "does this caller have standing
        on this side," so the answer (and its exact 403 body, when it is
        one) can never fork between accept/decline and revoke.
        """
        return _owner_or_operator_or_403(_invite_side_stand_in(session, agent_name), request)

    def _sync_dispatch_pairs(session: Session) -> None:
        """Thin, session-only wrapper the invite accept/revoke routes below
        call after their own state-changing commit (access-control arc,
        Task C4: the room becomes the switchboard's policy source) --
        pulls `clients`/`config` off `app.state` (same convention the chat
        route's own `cfg`/`clients` fetch already uses) and delegates to
        `kobo_api.pairs.sync_dispatch_pairs_best_effort`, the one fail-soft
        implementation this room's other two triggers (an agent's destroy-
        forget step in `kobo_api.destroy`, an agent's READY flip in
        `kobo_api.provisioner`) call directly with their own already-in-
        scope clients/config. Those two live inside a background-thread
        pipeline in a module `main.py` itself imports, so a `main.py`-side
        entry point could never be their call path without a circular
        import -- see `sync_dispatch_pairs_best_effort`'s own docstring.
        """
        sync_dispatch_pairs_best_effort(app.state.clients, app.state.config, session)

    def _decide_invite(
        session: Session, request: Request, invite_id: int, *, new_status: str, verb: str
    ) -> JSONResponse:
        """Shared body for accept_invite/decline_invite below -- identical
        gate and state-transition shape, differing only in which terminal
        status they move a pending invite to and the verb their 409 copy
        names.

        Folds 1+2 (C1 review): standing is checked on BOTH sides before
        anything else. A caller with standing on NEITHER side (a true
        stranger to this invite) gets the exact same 404
        (`_no_such_invite_404`) a missing invite id gets -- previously this
        path fetched the TO agent through `_visible_agent_or_404`, which
        either leaked the TO agent's real name in a 404 body (when it
        wasn't visible to the caller) or fell through to a 403 the moment
        it was (an oracle either way, distinguishable from a genuinely
        missing id). A caller with standing on the FROM side only (e.g. the
        agent that SENT this request, checking in on it) still gets the
        plain forbidden-mutation 403 -- they already know the invite exists
        from their own `GET .../invites`, so naming that is not a leak;
        accepting/declining is the TO side's call to make regardless.
        """
        invite = _fetch_invite_or_404(session, invite_id)
        if isinstance(invite, JSONResponse):
            return invite

        from_gate = _invite_side_gate(session, request, invite.from_agent)
        to_gate = _invite_side_gate(session, request, invite.to_agent)
        if from_gate is not None and to_gate is not None:
            return _no_such_invite_404(invite_id)
        if to_gate is not None:
            return to_gate

        if invite.status != INVITE_PENDING:
            return JSONResponse(
                {"error": f"invite is already {invite.status}. Cannot {verb} it now."},
                status_code=409,
            )

        invite.status = new_status
        invite.decided_by = getattr(request.state, "iap_email", None)
        invite.decided_at = datetime.now(UTC)
        session.commit()

        # Only an accept actually changes `allowed_pairs` -- a decline
        # leaves a PENDING invite PENDING-shaped-but-terminal, and a
        # pending invite was never in the pairs set to begin with
        # (kobo_api.pairs.allowed_pairs only ever reads ACCEPTED rows), so
        # syncing after a decline would just re-push the same set for no
        # reason. `new_status` (not `invite.status`, though they're the
        # same object right after the assignment above) makes the branch
        # read directly off what this call decided, rather than off a
        # mutation that happened three lines up.
        if new_status == INVITE_ACCEPTED:
            _sync_dispatch_pairs(session)

        return JSONResponse(_invite_repr(session, invite))

    @app.post("/api/agents/{name}/invites", status_code=201)
    def create_invite(
        name: str,
        payload: InviteCreateRequest,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        """Request cross-group colleague access from `name` (the FROM
        agent) to `payload.to_agent` (the TO agent) -- access-control arc,
        Task C1, design spec section 5: "an agent from another group must
        request access, and the RECEIVING agent's owner accepts before the
        handshake exists."

        The two sides of this request are resolved through deliberately
        different fetch disciplines:

        - `name` (FROM) goes through `_visible_agent_or_404`, the single
          choke point every agent-scoped route in this file resolves its
          path agent through, followed by the usual owner-or-operator gate
          -- so an invisible-to-the-caller FROM agent 404s before that gate
          ever runs (the house 404-before-403 discipline), and a visible
          FROM agent the caller doesn't own or operate 403s. Requesting
          access on behalf of somebody else's agent is exactly the
          mutation that gate exists to block.
        - `payload.to_agent` is fetched WITHOUT the visibility filter -- a
          plain, unfiltered lookup by name, on purpose. The adjudicated
          design here: the TO agent is ALLOWED to sit outside the caller's
          own visible set. Asking for access to an agent that shows up in
          the (deliberately global, spec section 5) a2a directory but that
          the caller can't otherwise see is the feature this route exists
          to offer, not a hole in it -- knowing a name the directory
          already surfaced is not itself a leak. What the route must never
          do is hand back anything MORE than that name plus the TO agent's
          group once it exists in this row: every response below embeds
          the TO side through `_agent_ref`, which caps it to exactly that
          shape, never the agent's status, owner, model, or anything else
          a caller without real visibility has no standing to see.
        """
        from_agent = _visible_agent_or_404(session, request, name)
        if isinstance(from_agent, JSONResponse):
            return from_agent
        gate = _owner_or_operator_or_403(from_agent, request)
        if gate is not None:
            return gate

        to_agent = session.query(Agent).filter_by(name=payload.to_agent).first()
        if to_agent is None:
            return JSONResponse(
                {"error": f"no such agent: {payload.to_agent!r}"}, status_code=404
            )

        if to_agent.name == from_agent.name:
            return JSONResponse(
                {"error": "an agent cannot request colleague access to itself"},
                status_code=422,
            )

        # Same-group is already implicit (kobo_api.pairs.allowed_pairs),
        # via the shared `implicitly_paired` predicate (fix round, shared-
        # group wave HIGH) -- so an invite here would be redundant. Two
        # group-less agents share no implicit pair at all (pairs.py's own
        # property), and neither do two SHARED_GROUP agents (the commons is
        # humanly open but its agents' wires stay invite-gated) -- both
        # combinations are real requests, not redundant ones, and must fall
        # through to be created.
        if implicitly_paired(from_agent.group_name, to_agent.group_name):
            return JSONResponse(
                {
                    "error": (
                        "these two agents are already in the same group and "
                        "can talk freely. No invite is needed."
                    )
                },
                status_code=422,
            )

        existing = (
            session.query(A2AInvite)
            .filter_by(from_agent=from_agent.name, to_agent=to_agent.name)
            .filter(A2AInvite.status.in_((INVITE_PENDING, INVITE_ACCEPTED)))
            .first()
        )
        if existing is not None:
            return JSONResponse(
                {
                    "error": (
                        "a colleague request between these two agents is "
                        "already pending or accepted"
                    )
                },
                status_code=409,
            )

        invite = A2AInvite(
            from_agent=from_agent.name,
            to_agent=to_agent.name,
            requested_by=getattr(request.state, "iap_email", None),
            status=INVITE_PENDING,
        )
        session.add(invite)
        session.commit()

        return JSONResponse(_invite_repr(session, invite), status_code=201)

    @app.get("/api/agents/{name}/invites")
    def list_invites(
        name: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        """Every invite naming `name` on either side, split into `incoming`
        (name is the TO side) and `outgoing` (name is the FROM side) --
        visible only to `name`'s own owner or a room operator. Read-only,
        but still owner-or-operator gated rather than open like chat/teach:
        an invite is "the agent's stuff" (who's asking to talk to it, who
        it's asked to talk to), not a colleague conversation.
        """
        agent = _visible_agent_or_404(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        outgoing = (
            session.query(A2AInvite)
            .filter_by(from_agent=agent.name)
            .order_by(A2AInvite.id)
            .all()
        )
        incoming = (
            session.query(A2AInvite)
            .filter_by(to_agent=agent.name)
            .order_by(A2AInvite.id)
            .all()
        )

        return JSONResponse(
            {
                "incoming": [_invite_repr(session, invite) for invite in incoming],
                "outgoing": [_invite_repr(session, invite) for invite in outgoing],
                "team_pairs": _team_pairs(session, agent),
            }
        )

    @app.get("/api/agents/{name}/requestable")
    def list_requestable_agents(
        name: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        """Every other READY agent `name` could ask for colleague access
        (access-tab refinement wave) -- owner-or-operator of `name` only,
        but the candidate list itself deliberately BYPASSES the caller's
        own personal visibility (access-control Task B2's `_visible_agents`
        is never consulted here): consent needs a directory to ask from in
        the first place, and the design spec is explicit that the a2a
        directory of bare names is deliberately global (spec section 5) --
        an agent's mere EXISTENCE and its group/team/capability blurb are
        not a leak the way its status, owner, or model would be, exactly
        the same adjudication `create_invite`'s own TO-agent lookup and
        `_agent_ref` already rest on. This route just makes that same
        directory browsable up front, rather than requiring the caller to
        already know a name to invite it.

        Each candidate is capped to exactly `{"name", "group", "team",
        "capability", "purpose_tags"}` -- the same card-metadata fields
        `list_agents`/`get_agent` already expose about an agent the caller
        CAN see, never anything about status, owner, or model.

        Three exclusions, none of them optional:
        - `name` itself (an agent cannot request colleague access to
          itself, same rule `create_invite` enforces).
        - Its own same-group colleagues -- already auto-paired
          (`kobo_api.pairs.allowed_pairs`'s same-group rule, via the shared
          `implicitly_paired` predicate, the same "read-only facts, not a
          revocable invite row" set `_team_pairs` above surfaces), so
          listing them here as requestABLE would be straightforwardly
          wrong: there is nothing left to request. `SHARED_GROUP` colleagues
          are NOT excluded by this rule (fix round, shared-group wave HIGH):
          `implicitly_paired` says two commons agents are never auto-paired,
          so they remain genuinely requestABLE from each other.
        - Any agent `name` already has a PENDING or ACCEPTED invite
          toward -- same "in flight" definition `create_invite`'s own
          duplicate-request 409 check uses. These are surfaced instead
          under `already`, `{"name", "status"}` each, so the UI can render
          "already asked" rather than silently omitting them.
        """
        agent = _visible_agent_or_404(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        all_ready = (
            session.query(Agent)
            .filter(Agent.status == AgentStatus.READY, Agent.name != agent.name)
            .order_by(Agent.id)
            .all()
        )

        in_flight_status = {
            invite.to_agent: invite.status
            for invite in session.query(A2AInvite)
            .filter_by(from_agent=agent.name)
            .filter(A2AInvite.status.in_((INVITE_PENDING, INVITE_ACCEPTED)))
            .all()
        }

        requestable = []
        already = []
        for other in all_ready:
            if implicitly_paired(agent.group_name, other.group_name):
                continue
            in_flight = in_flight_status.get(other.name)
            if in_flight is not None:
                already.append({"name": other.name, "status": in_flight})
                continue
            requestable.append(
                {
                    "name": other.name,
                    "group": other.group_name,
                    "team": other.team,
                    "capability": other.capability,
                    "purpose_tags": decode_tags(other.purpose_tags),
                }
            )

        return JSONResponse({"requestable": requestable, "already": already})

    @app.post("/api/invites/{invite_id}/accept")
    def accept_invite(
        invite_id: int, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        return _decide_invite(
            session, request, invite_id, new_status=INVITE_ACCEPTED, verb="accept"
        )

    @app.post("/api/invites/{invite_id}/decline")
    def decline_invite(
        invite_id: int, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        return _decide_invite(
            session, request, invite_id, new_status=INVITE_DECLINED, verb="decline"
        )

    @app.delete("/api/invites/{invite_id}")
    def revoke_invite(
        invite_id: int, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        """Revoke a pending or accepted invite -- either side's owner or an
        operator (design spec section 5's decision record: unlike accept/
        decline, which is TO-side only, either party may walk away from an
        arrangement it's already a party to). A declined or already-revoked
        invite has nothing left to revoke, hence the 409 rather than a
        silent no-op or a second write.

        Fold 2 (C1 review): a caller with standing on NEITHER side gets the
        exact same 404 a missing invite id gets (`_no_such_invite_404`) --
        this used to be the 403-vs-404 oracle itself (a stranger to both
        sides got 403 here but 404 against a genuinely missing id), letting
        a caller with zero standing learn "this id names a real invite"
        from the status code alone. For revoke, standing on either side IS
        the permission (no TO-only asymmetry like accept/decline above), so
        there is no separate "wrong side" 403 branch to fall through to.
        """
        invite = _fetch_invite_or_404(session, invite_id)
        if isinstance(invite, JSONResponse):
            return invite

        from_gate = _invite_side_gate(session, request, invite.from_agent)
        to_gate = _invite_side_gate(session, request, invite.to_agent)
        if from_gate is not None and to_gate is not None:
            return _no_such_invite_404(invite_id)

        if invite.status not in (INVITE_PENDING, INVITE_ACCEPTED):
            return JSONResponse(
                {"error": f"invite is already {invite.status}. Nothing to revoke."},
                status_code=409,
            )

        # Captured before the mutation below: only an ACCEPTED -> REVOKED
        # transition actually removes anything from `allowed_pairs` (a
        # PENDING invite was never in that set), so only that transition
        # needs to sync -- same "only sync a real pairs change" discipline
        # `_decide_invite`'s own accept-only branch above follows.
        was_accepted = invite.status == INVITE_ACCEPTED

        invite.status = INVITE_REVOKED
        invite.decided_by = getattr(request.state, "iap_email", None)
        invite.decided_at = datetime.now(UTC)
        session.commit()

        if was_accepted:
            _sync_dispatch_pairs(session)

        return JSONResponse(_invite_repr(session, invite))

    # --- Threads: a shared, multi-agent conversation (threads arc, Tasks 8-9) ---
    #
    # Operator rulings (law): the initiating human accepts an agent's
    # suggestion or adds participants directly; same-group agents join a
    # thread freely; a cross-group agent needs an EXISTING accepted
    # `A2AInvite` with every agent already in the thread (reusing the
    # consent model `kobo_api.pairs.implicitly_paired`/`A2AInvite` already
    # establish -- no new consent concept); a thread is readable by its
    # INITIATOR ONLY and appears in no agent's colleague feed; turns are
    # delivered to participants in sequence (never a fan-out), each seeing
    # every message that came before, and an agent never answers twice in
    # one turn.
    #
    # `_thread_or_404`/`_initiator_or_404` are the one gate every thread
    # route below shares: a missing thread id and a real thread this
    # caller did not start answer with the IDENTICAL 404 body, never a
    # distinguishable 403 -- the ruling's whole point, mirroring
    # `_no_such_invite_404`'s own "no standing on this side" fold above.

    def _no_such_thread_404(thread_id: int) -> JSONResponse:
        return JSONResponse({"error": f"no such thread: {thread_id}"}, status_code=404)

    def _thread_or_404(session: Session, thread_id: int) -> Thread | JSONResponse:
        thread = session.get(Thread, thread_id)
        if thread is None:
            return _no_such_thread_404(thread_id)
        return thread

    def _initiator_or_404(thread: Thread, request: Request) -> JSONResponse | None:
        """None when `request`'s caller is `thread`'s own initiator; the
        thread's own not-found body otherwise -- NEVER a distinguishable
        403, and NEVER an operator carve-out (operator ruling: "a thread is
        readable by its initiator and by nobody else" -- unlike every
        owner-or-operator gate elsewhere in this file, there is no `_is_
        admin` escape hatch here on purpose). A member of a participating
        agent's own group who is not the initiator, or a room operator who
        is not the initiator, learns nothing beyond "no such thread" --
        proven by this room's own test law as the negative case first.

        IAP-disabled short-circuits to allowed, the same escape hatch every
        other gate in this file takes ("the room trusts its perimeter only
        when it has one"): with no verified identity ever available,
        `initiator_email` is never recorded meaningfully either, so without
        this a room that never turned IAP on (local dev, most of this test
        suite) could never read back a thread it just created.
        """
        cfg: Config = request.app.state.config
        if not cfg.iap_audience:
            return None
        email = (getattr(request.state, "iap_email", None) or "").lower()
        if email and thread.initiator_email and email == thread.initiator_email.lower():
            return None
        return _no_such_thread_404(thread.id)

    def _thread_participants(session: Session, thread_id: int) -> list[ThreadParticipant]:
        return (
            session.query(ThreadParticipant)
            .filter_by(thread_id=thread_id)
            .order_by(ThreadParticipant.id)
            .all()
        )

    def _thread_repr(session: Session, thread: Thread) -> dict:
        return {
            "id": thread.id,
            "initiator_email": thread.initiator_email,
            "created_at": thread.created_at.isoformat(),
            "participants": [p.agent_name for p in _thread_participants(session, thread.id)],
        }

    def _thread_consented(session: Session, a: Agent, b: Agent) -> bool:
        """Whether `a` and `b` may share a thread: same-group agents join
        freely (`kobo_api.pairs.implicitly_paired`, the exact predicate
        `allowed_pairs`'s own same-group rule uses), or an ACCEPTED
        `A2AInvite` already exists between them in EITHER direction -- an
        accepted invite is mutual consent to talk at all, so which side
        originally asked does not matter here the way it does for
        `allowed_pairs`'s own directional dispatch pairs. No new consent
        concept: this is the identical predicate `create_invite`'s own
        redundant-invite check and `list_requestable_agents` already share.
        """
        if implicitly_paired(a.group_name, b.group_name):
            return True
        accepted = (
            session.query(A2AInvite)
            .filter(A2AInvite.status == INVITE_ACCEPTED)
            .filter(
                (
                    (A2AInvite.from_agent == a.name) & (A2AInvite.to_agent == b.name)
                )
                | (
                    (A2AInvite.from_agent == b.name) & (A2AInvite.to_agent == a.name)
                )
            )
            .first()
        )
        return accepted is not None

    def _add_thread_participant(
        session: Session, thread: Thread, agent: Agent
    ) -> JSONResponse | None:
        """The shared body `create_thread`/`add_thread_participant` below
        both use to seat one agent: not-ready refuses, an already-seated
        agent is a no-op 409, and every OTHER current participant must be
        consent-compatible with the new one (`_thread_consented`) before
        the row is ever inserted. Returns None on success (the row is
        already added, not yet committed) or the JSONResponse a route
        should return immediately.
        """
        if agent.status != AgentStatus.READY:
            return JSONResponse(
                {"error": "agent is not ready to join a thread"}, status_code=409
            )

        existing = _thread_participants(session, thread.id)
        if any(p.agent_name == agent.name for p in existing):
            return JSONResponse(
                {"error": f"{agent.name} is already in this thread"}, status_code=409
            )

        for participant in existing:
            other = session.query(Agent).filter_by(name=participant.agent_name).first()
            if other is None:
                continue
            if not _thread_consented(session, agent, other):
                return JSONResponse(
                    {
                        "error": (
                            f"{agent.name} and {other.name} are not connected. "
                            "Accept a colleague invite between them first."
                        )
                    },
                    status_code=422,
                )

        session.add(ThreadParticipant(thread_id=thread.id, agent_name=agent.name))
        return None

    @app.post("/api/threads", status_code=201)
    def create_thread(
        payload: ThreadCreateRequest,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        """Start a new thread naming one agent the caller can see (threads
        arc, Task 8). Open to any signed-in user who can see that agent --
        same posture `chat_with_agent`'s own docstring precedent takes for
        teach/chat, never an ownership gate -- since a thread is the
        initiator's own space to manage, not a mutation of the agent
        itself.
        """
        agent = _visible_agent_or_404(session, request, payload.agent)
        if isinstance(agent, JSONResponse):
            return agent

        thread = Thread(initiator_email=getattr(request.state, "iap_email", None))
        session.add(thread)
        session.flush()

        gate = _add_thread_participant(session, thread, agent)
        if gate is not None:
            return gate

        session.commit()
        return JSONResponse(_thread_repr(session, thread), status_code=201)

    @app.post("/api/threads/{thread_id}/participants", status_code=201)
    def add_thread_participant(
        thread_id: int,
        payload: ThreadParticipantRequest,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        """Add one more agent to an already-created thread -- the
        initiator's own call only (`_initiator_or_404`), naming an agent
        the initiator can see (`_visible_agent_or_404`, the room's one
        visibility choke point, anti-enumeration copy included) that is
        consent-compatible with every agent already seated
        (`_add_thread_participant`).
        """
        thread = _thread_or_404(session, thread_id)
        if isinstance(thread, JSONResponse):
            return thread
        gate = _initiator_or_404(thread, request)
        if gate is not None:
            return gate

        agent = _visible_agent_or_404(session, request, payload.agent)
        if isinstance(agent, JSONResponse):
            return agent

        gate = _add_thread_participant(session, thread, agent)
        if gate is not None:
            return gate

        session.commit()
        return JSONResponse(_thread_repr(session, thread), status_code=201)

    @app.get("/api/threads/{thread_id}")
    def get_thread(
        thread_id: int, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        """Thread detail: readable by its initiator, and by nobody else
        (operator ruling) -- a participating agent's own group member who
        is not the initiator gets the identical not-found answer a
        genuinely missing thread id gets, never a distinguishable 403.
        """
        thread = _thread_or_404(session, thread_id)
        if isinstance(thread, JSONResponse):
            return thread
        gate = _initiator_or_404(thread, request)
        if gate is not None:
            return gate
        return JSONResponse(_thread_repr(session, thread))

    @app.post("/api/threads/{thread_id}/turns")
    def post_thread_turn(
        thread_id: int,
        payload: ThreadTurnRequest,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        """Deliver one turn to every thread participant IN SEQUENCE (never
        a fan-out): the first participant sees exactly `payload.messages`;
        each later participant sees that plus every reply already given
        THIS turn, so a later answer can build on an earlier one. Every
        relay's envelope carries `contextId` = the thread's own id (so the
        switchboard's loop guard groups a thread's hops under one real
        key -- `_build_task_envelope`'s `context_id` parameter, unused
        until this caller) and `metadata.thread` = the thread's id, which
        marks the task so `_flatten_conversation_entries` excludes it from
        every participant's own colleague feed (a thread is a private,
        initiator-only conversation, never colleague small talk).

        Initiator-only, same not-found-not-403 gate every other thread
        route takes. Each participant appears in `ThreadParticipant` at
        most once (the table's own unique constraint), and this loop walks
        that list exactly once in id order, so no agent is ever asked to
        answer twice within one turn.
        """
        thread = _thread_or_404(session, thread_id)
        if isinstance(thread, JSONResponse):
            return thread
        gate = _initiator_or_404(thread, request)
        if gate is not None:
            return gate

        messages = payload.messages
        if not messages:
            return JSONResponse({"error": "messages must not be empty"}, status_code=422)
        for message in messages:
            if message.role not in CHAT_MESSAGE_ROLES:
                return JSONResponse(
                    {"error": f"unknown role: {message.role!r}"}, status_code=422
                )
            if not message.text:
                return JSONResponse(
                    {"error": "message text must not be empty"}, status_code=422
                )
            if len(message.text) > CHAT_MESSAGE_TEXT_MAX_LEN:
                return JSONResponse(
                    {
                        "error": (
                            f"message text exceeds {CHAT_MESSAGE_TEXT_MAX_LEN} characters"
                        )
                    },
                    status_code=422,
                )
        if messages[-1].role != "user":
            return JSONResponse(
                {"error": "last message must have role 'user'"}, status_code=422
            )

        participants = _thread_participants(session, thread.id)
        if not participants:
            return JSONResponse(
                {"error": "this thread has no participants yet"}, status_code=409
            )

        cfg: Config = app.state.config
        clients = app.state.clients
        gate = _colleagues_gate(cfg, clients)
        if gate is not None:
            return gate

        transcript = [{"role": m.role, "text": m.text} for m in messages]
        replies: list[dict] = []
        for participant in participants:
            envelope = _build_task_envelope(transcript, context_id=str(thread.id))
            envelope["metadata"] = {"thread": thread.id}
            try:
                raw = clients.cabal.relay(participant.agent_name, envelope)
                reply_text = _extract_relay_reply(raw)
            except (CabalError, httpx.HTTPError, ValueError) as exc:
                log.warning(
                    "thread turn relay failed for thread %s agent %s: %s",
                    thread.id,
                    participant.agent_name,
                    exc,
                )
                return JSONResponse(
                    {"error": "The switchboard did not answer. Try again shortly."},
                    status_code=502,
                )
            transcript.append({"role": "agent", "text": reply_text})
            replies.append({"agent": participant.agent_name, "text": reply_text})

        return JSONResponse({"replies": replies})

    # --- Admin: on-demand dispatch-pairs resync (final-review fix round,
    # HIGH-2b) ---------------------------------------------------------------
    #
    # The lost-sync problem this closes: a revoke or destroy whose push to
    # the switchboard failed (cabal down, a crash mid-request) leaves a
    # stale ALLOW pair there indefinitely unless SOMETHING re-pushes the
    # room's current, correct `allowed_pairs`. Two of the three ways that
    # happens are automatic and already covered -- `_boot_sync_dispatch_
    # pairs` below (every fresh process boot) and the next organic trigger
    # to fire (every one of `sync_dispatch_pairs_best_effort`'s four
    # callers pushes the FULL current set unconditionally, which heals a
    # prior failure the instant any of them next succeeds). This route is
    # the third: an operator who wants to force a resync RIGHT NOW --
    # diagnosing a suspected drift, or confirming a switchboard outage they
    # know just ended, without waiting on either of the other two.

    @app.post("/api/admin/sync-pairs")
    def sync_pairs_route(
        request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        cfg: Config = app.state.config
        # Operator-gated (`_admin_operator_block`, its own copy -- see that
        # function's docstring for why this isn't `_operator_block` itself):
        # this pushes room-wide policy, not one agent's own state, so there
        # is no per-agent owner to fall back to the way the looser `_owner_
        # or_operator_or_403` gate allows elsewhere.
        block = _admin_operator_block(request, cfg)
        if block is not None:
            return block

        clients: Clients | None = app.state.clients
        if not cabal_enabled(cfg) or clients is None or clients.cabal is None:
            return JSONResponse(_SWITCHBOARD_NOT_CONFIGURED, status_code=409)

        # Deliberately the RAISING core (`sync_dispatch_pairs`), not the
        # fail-soft `..._best_effort` wrapper every incidental trigger uses:
        # an operator who explicitly asked for a resync needs to be told it
        # didn't work, not have that silently logged and swallowed the way
        # an accept/revoke/destroy's own incidental sync attempt must be.
        try:
            count = sync_dispatch_pairs(clients.cabal, session, cfg)
        except (CabalError, httpx.HTTPError):
            log.warning("operator-triggered dispatch-pairs resync failed", exc_info=True)
            return JSONResponse(
                {"error": "couldn't sync the switchboard. Try again."}, status_code=502
            )

        return JSONResponse({"count": count})

    # --- User grants: per-user visibility, wired into _visible_agents above -

    @app.get("/api/agents/{name}/grants")
    def list_grants(
        name: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        agent = _visible_agent_or_404(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        grants = (
            session.query(AgentUserGrant)
            .filter_by(agent_name=agent.name)
            .order_by(AgentUserGrant.id)
            .all()
        )
        return JSONResponse(
            {"grants": [{"email": g.email, "added_by": g.added_by} for g in grants]}
        )

    @app.post("/api/agents/{name}/grants", status_code=201)
    def add_grant(
        name: str,
        payload: GrantCreateRequest,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        """Grant `payload.email` visibility into `name` as if they owned it
        (access-control arc, Task C1, design spec section 6's per-user
        additions) -- wired into `_visible_agents` above, the room's single
        visibility choke point, rather than through any route-local check.

        Idempotent by design: re-granting an email that already has a row
        for this agent (matched case-insensitively, same convention
        `_visible_agents`'s own grant lookup uses) returns the EXISTING
        grant with a plain 200 rather than creating a second row or
        erroring -- there is nothing more for a repeat grant to accomplish.
        """
        agent = _visible_agent_or_404(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        email = payload.email.strip()
        if not email:
            return JSONResponse({"error": "an email is required"}, status_code=422)

        existing_grants = session.query(AgentUserGrant).filter_by(agent_name=agent.name).all()
        for existing in existing_grants:
            if existing.email.lower() == email.lower():
                return JSONResponse(
                    {"email": existing.email, "added_by": existing.added_by}, status_code=200
                )

        grant = AgentUserGrant(
            agent_name=agent.name,
            email=email,
            added_by=getattr(request.state, "iap_email", None),
        )
        session.add(grant)
        session.commit()

        return JSONResponse({"email": grant.email, "added_by": grant.added_by}, status_code=201)

    @app.delete("/api/agents/{name}/grants/{email}")
    def remove_grant(
        name: str,
        email: str,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        agent = _visible_agent_or_404(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent
        gate = _owner_or_operator_or_403(agent, request)
        if gate is not None:
            return gate

        grants = session.query(AgentUserGrant).filter_by(agent_name=agent.name).all()
        grant = next((g for g in grants if g.email.lower() == email.lower()), None)
        if grant is None:
            return JSONResponse({"error": f"no such grant: {email!r}"}, status_code=404)

        session.delete(grant)
        session.commit()

        return JSONResponse({"removed": True})

    # --- MCP surface (MCP server arc, Task 9): flag-gated, mounted after
    # every /api/* route above and BEFORE the SPA static catch-all just
    # below, so /mcp, /oauth/*, and /.well-known/* win over the SPA
    # fallback route rather than being swallowed by it. A no-op (returns
    # False, imports nothing from the mcp SDK or kobo_api.mcp.server/
    # oauth_as) when config.mcp_enabled is False, the default -- see
    # kobo_api.mcp.mount's own module docstring for the full mechanism,
    # including why FastAPI(lifespan=_room_lifespan) above (which enters
    # mcp_lifespan itself, plus the learnings nightly loop) is what makes
    # the mounted sub-app's own session-manager lifespan actually run.
    mount_mcp(app)

    # --- Built UI (Task 1): last, so every /api/* route above always wins ---

    if config.ui_dist:
        mount_ui(app, config.ui_dist)

    return app


def _boot_sync_dispatch_pairs(
    clients: Clients, config: Config, session_factory: sessionmaker[Session]
) -> None:
    """Push the room's current `allowed_pairs` to the switchboard once at
    process boot (access-control arc, final-review fix round, HIGH-2a).

    Closes the "enforcement never begins after deploy" gap: without this, a
    revoke/destroy whose push failed just before a crash (or simply a fresh
    deploy against a room that already has agents/invites from before the
    switchboard was ever wired) leaves cabal's policy stale or empty until
    some UNRELATED consent change happens to fire one of `sync_dispatch_
    pairs_best_effort`'s four existing triggers. Every fresh boot now IS a
    trigger of its own, unconditionally, before this process serves a
    single request -- called from `build_default_app`, right beside its own
    boot-time org-config clone (both are "leave the room in the state it
    should already be in before anyone can hit it" boot work).

    Delegates entirely to `sync_dispatch_pairs_best_effort` -- same no-op-
    when-unwired gate, same fail-soft swallowing of a `CabalError`/`httpx.
    HTTPError` (a boot must never crash on a switchboard hiccup, the same
    posture `load_org_config` already takes for the org-config clone: land
    in a degraded-but-running state, never refuse to start). Opens and
    closes its own session (this runs once, before `create_app` wires the
    per-request `get_session` dependency) rather than sharing one with any
    other boot step.

    Logs the pushed count at `log.info` on an actual push -- distinct from
    `CabalClient.put_dispatch_pairs`'s own count log, so an operator
    scanning boot logs can tell AT A GLANCE that the boot-time resync
    specifically ran, without first knowing to go looking for cabal's own
    line buried among everything else `Clients.real(config)` just did.
    """
    if not cabal_enabled(config) or clients.cabal is None:
        return
    session = session_factory()
    try:
        count = sync_dispatch_pairs_best_effort(clients, config, session)
    finally:
        session.close()
    if count is not None:
        log.info("boot-time dispatch-pairs sync pushed %d pair(s) to the switchboard", count)


def build_default_app() -> FastAPI:
    """Wire the real config, org config, database, and platform clients."""
    # Local import: `kobo_api.clients` pulls in httpx/subprocess-backed
    # implementations that most tests (which only need `create_app` and a
    # fake/None clients object) never touch — keeping this import inside the
    # function keeps importing `kobo_api.main` itself cheap for them.
    from kobo_api.clients import (
        Clients,
        CommandRunner,
        GhCLI,
        installation_token_provider,
    )

    local_config = Config.load(None)
    # One GhCLI, built once, just for the org-config clone below — Clients.
    # real() (below) builds its own separate GhCLI for everything else. It's
    # App-authed via the same `installation_token_provider` helper Clients.
    # real() uses, whenever the App is fully configured, so a PRIVATE
    # org-config repo clones at boot instead of falling back to the
    # operator's (absent, in a deployed room) `gh auth` session. Boot never
    # crashes on an org problem: load_org_config catches every clone/parse/
    # validation failure itself and falls back to `local_config` unchanged,
    # paired with an unwired OrgState an unwired room can render honestly
    # (Tasks 3/8 consume `app.state.org`).
    github = GhCLI(
        runner=CommandRunner(), token_provider=installation_token_provider(local_config)
    )
    config, org = load_org_config(local_config, github)

    config.workdir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(config.db_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    # Startup sweep, before any request is served: an agent left in
    # PENDING/PROVISIONING by a prior process's death (killed mid-provision,
    # a bad deploy, ...) has no thread left to ever finish it. Only wired
    # here, never in create_app, so the many tests that build their own
    # isolated fixture (agents seeded directly, no restart involved) aren't
    # affected by it.
    sweep_interrupted(session_factory)
    # One real Clients (and the httpx.Client(s) it owns) per process, built
    # once here rather than per-request: the accepted pattern for this app's
    # lifetime (a longer-lived httpx client pool is the deferred concern, not
    # a correctness issue for the single-process deployment this targets).
    # Built from the MERGED config, same as the engine above — everything
    # downstream of boot sees org values as if they'd always been local.
    clients = Clients.real(config)
    # Boot-time write probe for the Slack token Secret: rotation is the one
    # write whose failure is unrecoverable, and it happens ~12h after boot
    # with nobody watching. One idempotent apply here says now, in the
    # startup logs, whether the room can do it at all. Fail-soft by
    # design -- see the probe's own docstring.
    probe_slack_token_store(clients)
    # Boot-time dispatch-pairs resync (final-review fix round, HIGH-2a): a
    # no-op if the switchboard isn't wired, fail-soft otherwise -- see
    # `_boot_sync_dispatch_pairs`'s own docstring for why this belongs
    # here, right beside the org-config clone above, rather than waiting
    # for an organic consent-change trigger that might never come.
    _boot_sync_dispatch_pairs(clients, config, session_factory)
    return create_app(session_factory, config=config, clients=clients, org=org)

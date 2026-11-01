"""SQLAlchemy models for kobo-api's agent ledger."""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from kobo_api.kinds import KIND_COMPOSED


class Base(DeclarativeBase):
    pass


class AgentStatus(enum.StrEnum):
    PENDING = "pending"
    PROVISIONING = "provisioning"
    READY = "ready"
    FAILED = "failed"
    # Parked on a person, not broken: a connected agent whose setup pull
    # request the platform has opened and its owner has not merged. Nothing
    # runs; the waiting sweep (kobo_api.provisioner.sweep_waiting) resumes
    # the pipeline when the platform reports the merge, and Retry is a
    # Resume. Only connected agents ever hold it: a composed agent's setup
    # PR is merged by the room itself.
    WAITING = "waiting"
    # Agent destruction (Task 4): the mirror of PROVISIONING/FAILED for the
    # destroy ledger (kobo_api.destroy.run_destroy) — DESTROYING while the
    # four-step teardown runs, DESTROY_FAILED when a step fails (resumable:
    # a later DELETE re-invokes run_destroy, which skips DONE steps and
    # retries from the first incomplete one). There is no destroyed-terminal
    # status: a fully-destroyed agent's row (and its steps) are deleted
    # outright by the last step (destroy-forget), not left in some terminal
    # status for a row that no longer exists to hold.
    DESTROYING = "destroying"
    DESTROY_FAILED = "destroy_failed"


class StepStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"
    # The step is parked on a person (see AgentStatus.WAITING); `error`
    # carries what it waits for, the setup pull request's link included.
    WAITING = "waiting"


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    persona: Mapped[str] = mapped_column(String(63), nullable=False)
    # The creating team (Task 5, bucket catalogs): `payload.team` on
    # POST /api/agents. Nullable: this column postdates create_all on any
    # already-running dev DB, same pattern as avatar_png below — those need
    # a manual `ALTER TABLE agents ADD COLUMN team VARCHAR;` (fresh DBs get
    # it for free via create_all), and pre-existing rows simply have no team
    # recorded.
    team: Mapped[str | None] = mapped_column(String(63), nullable=True)
    # Persona metadata (persona-metadata plan, Task 2): purpose_tags is the
    # normalized, order-preserving tag list JSON-encoded for storage (see
    # kobo_api.purpose_tags.encode_tags/decode_tags — same "JSON text in a
    # VARCHAR column" idiom as slack_channels below), and capability is a
    # plain string that stays null until it's "earned" (a later task in the
    # same plan writes it; this task only carries the column and always
    # serves it back null). Both nullable for the same reason team above
    # is: this pair postdates create_all on any already-running dev DB —
    # those need manual ALTERs (fresh DBs get them for free via
    # create_all):
    #   ALTER TABLE agents ADD COLUMN purpose_tags VARCHAR;
    #   ALTER TABLE agents ADD COLUMN capability VARCHAR;
    purpose_tags: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    capability: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Per-agent runtime image (IE-4526): FROM line of the generated
    # Dockerfile when set; None means the org-wide default at compose/re-pin
    # time. Nullable: this column postdates create_all on any already-running
    # DB -- apply manually there (fresh DBs get it free via create_all):
    #   ALTER TABLE agents ADD COLUMN runtime_image VARCHAR;
    runtime_image: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # What the agent is built on right now (2026-11-01): the ref its
    # repository's Dockerfile `FROM` line carries, recorded by every path
    # that writes that line (compose at create, the fast-lane re-pins, the
    # runtime route) and backfilled at boot from the file itself
    # (`kobo_api.runtime_truth`). Distinct from `runtime_image` above, which
    # is the CHOICE (NULL = follow the room); this is the FACT. The Harness
    # page compares this, not the choice, with the room's pin, so an agent
    # that followed the room onto an older runtime reads as behind once the
    # room moves. NULL means the room has not read the file yet.
    built_runtime_image: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Archetype marker (team-guide spec 2026-09-14): the create-time recipe
    # this agent was born from (`kobo_api.archetypes.ARCHETYPE_TEAM_GUIDE`),
    # or None for an ordinary agent. Recorded on the row rather than inferred
    # from tags because the bundle composer reads it: a team-guide kiosk
    # composes with NO action tools at all (no github broker block, no
    # liminal gateway block), and "cannot act" has to be a property of the
    # composed bundle, not of the create form. Nullable: this column
    # postdates create_all on any already-running DB -- apply manually there
    # (fresh DBs get it free via create_all):
    #   ALTER TABLE agents ADD COLUMN archetype VARCHAR(64);
    archetype: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Which of the two kinds of agent this row is (connected-agents design
    # 2026-09-21): `composed`, the room's own repo and bundle, or
    # `connected`, a team's existing repo the room only deploys. Values are
    # `kobo_api.kinds.KIND_COMPOSED`/`KIND_CONNECTED`.
    # NOT nullable, unlike archetype above: this is not a create-time lane
    # marker read at one point, it is the question the pipeline selector,
    # the destroy ledger, both read routes and the UI's tab model each ask,
    # and a NULL would make "does the room own this repo" three-valued at
    # every one of them -- with the wrong answer being destructive at the
    # destroy ledger. Every pre-existing row is composed, which is what the
    # server default records once and for all.
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default=KIND_COMPOSED, server_default=KIND_COMPOSED
    )
    # Connected agents only: how the team's own code runs, `http` (it
    # serves a port the deploy platform can probe) or `headless` (it does
    # not). Nullable because a composed agent has no such question to
    # answer -- it is always the room's one runtime, which always serves
    # HTTP -- so NULL here means "not asked", never "unknown".
    run_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Connected agents only: the names of Kubernetes Secrets the owner
    # creates in the agent's own namespace, passed through to the deploy
    # platform so the pod mounts them. The room never holds their contents.
    # Non-nullable with an empty-list default for the same reason
    # `skills` above is: the readers iterate it, and a NULL would make
    # every one of them spell the same `or []`.
    secret_names: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    skills: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=AgentStatus.PENDING
    )
    repo_full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gojira_app_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Slack connect (P2c): both nullable, set only once an operator walks the
    # agent through Finish-Slack-setup — non-secret state only (the app id,
    # the channel it's wired to). The pasted bot/app tokens themselves are
    # never persisted anywhere; see kobo_api.slack_setup.wire_slack.
    slack_app_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    slack_channel: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Two-lane Slack (2026-08-17): the saved channel list (JSON array of
    # C ids) and whether wiring has completed at least once. Existing DBs
    # need manual ALTERs (fresh DBs get them from create_all):
    #   ALTER TABLE agents ADD COLUMN slack_channels VARCHAR;
    #   ALTER TABLE agents ADD COLUMN slack_wired BOOLEAN DEFAULT 0;
    # `slack_channel` (singular) above is dormant after this change: kept
    # for history, no longer written.
    slack_channels: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    slack_wired: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Slack self-service (2026-08-17): whether this agent's Slack app was
    # born self-service (a user created it directly at Slack's own app
    # site from the rendered manifest, then pasted its id/tokens into
    # POST .../slack/connect) rather than room-created by kobo's own
    # retired POST .../slack/app (a config-token app). Stamped True the
    # moment connect stores a fresh `slack_app_id`; never flipped back.
    # This is the provenance refinement: destroy-slack reads this flag
    # directly instead of inferring the same distinction from Slack's
    # error codes at delete time (see the design spec's Destruction
    # section). Existing DBs need a manual ALTER (fresh DBs get it for
    # free via create_all):
    #   ALTER TABLE agents ADD COLUMN slack_app_self_service BOOLEAN DEFAULT 0;
    slack_app_self_service: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # How Slack wakes this agent in a channel (2026-10-06): "mention" (only an
    # @-mention), "thread" (a mention, then every reply in a thread it was
    # asked in at the top) or "channel" (every message). NULL reads as
    # "mention", which is what every agent did before the choice existed; new
    # agents are born "thread". The runtime enforces it from SLACK_WAKE_MODE
    # in the agent's deploy contract, written at birth and on every change.
    slack_wake: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Colleague mode (issue #131), set by the owner: "default", "roster-only"
    # or "none" (`kobo_api.composer.COLLEAGUE_MODES`). NULL reads as
    # "default", which is what every agent had before the setting existed.
    # The composer reads it at birth; a change rewrites the bundle's allow
    # list and colleagues.md in the agent's repo.
    colleague_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Avatar (P2d): a base64-encoded copy of bundle/avatar.png (the repo is
    # the source of truth, same as first_reply's relationship to the a2a
    # endpoint) — cached here purely so list/detail views can render an
    # <img> without a round trip to GitHub per view. Nullable: most agents
    # won't have generated one. NOTE: this column postdates create_all on
    # any already-running dev DB — those need a manual `ALTER TABLE agents
    # ADD COLUMN avatar_png TEXT;` (fresh DBs get it for free via create_all).
    avatar_png: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Access control (access-control arc, Task A2): who made this agent, and
    # which ACL namespace it lives in. `created_by` is the verified IAP
    # email captured at create time (`request.state.iap_email`) -- the
    # access axis' owner, and the field a later task's owner-or-operator
    # gate reads directly. `group_name` is the ACL namespace a later task
    # validates against Okta-derived group membership -- it is DISTINCT
    # from `team` above (the skills-catalog capability axis); the two must
    # never be conflated. Both nullable for the same reason team/
    # purpose_tags/capability are: this pair postdates create_all on any
    # already-running dev DB -- those need manual ALTERs at rollout (fresh
    # DBs get them for free via create_all):
    #   ALTER TABLE agents ADD COLUMN created_by VARCHAR;
    #   ALTER TABLE agents ADD COLUMN group_name VARCHAR;
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    group_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Baseline tooling pack (MCP marketplace wave 1, Task 6): the create-time
    # opt-out. False (the default, for every agent and every caller who says
    # nothing) means RepoStep stages the marketplace's `baseline-*` tools into
    # this agent's composed bundle; True means it ships without them. Read
    # only at compose time -- flipping it later does not retro-stage anything
    # (the repo is the source of truth for a live agent's tools, and
    # POST .../tooling is the path that adds one after birth).
    # NOT nullable, unlike the columns above it: a boolean gate that could be
    # NULL would make "did this agent opt out" a three-valued question at
    # every read. Existing DBs therefore need the default spelled in the
    # rollout ALTER (fresh DBs get it for free via create_all):
    #   ALTER TABLE agents ADD COLUMN skip_baseline BOOLEAN NOT NULL DEFAULT 0;
    skip_baseline: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    # When the room last minted this agent's colleague pass
    # (`kobo_api.colleagues.deliver_pass`, issue #199). The PR and Jira brokers
    # refuse a pass issued before this instant, so a re-wire ends an older
    # pass's reach into GitHub and Jira as well as into the switchboard.
    # NULL for agents wired before this column existed: no cut, as before.
    pass_minted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Liveness of the process currently running this agent's pipeline
    # (`kobo_api.provisioner`): refreshed on a short interval for as long as
    # a run is in flight, and never touched otherwise. This is what lets
    # `sweep_interrupted` tell a provision whose process died from one that
    # is simply still working -- with rolling updates, a new pod boots while
    # the old one is mid-pipeline, and a sweep without this would fail a
    # live run and let a retry execute the same steps twice. NULL means no
    # run has ever beaten for this row (every row that predates the column
    # included), which the sweep reads against the row's own age.
    run_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    steps: Mapped[list[AgentStep]] = relationship(
        back_populates="agent", order_by="AgentStep.id"
    )


class AgentStep(Base):
    __tablename__ = "agent_steps"
    __table_args__ = (UniqueConstraint("agent_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=StepStatus.PENDING
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    agent: Mapped[Agent] = relationship(back_populates="steps")


# A2AInvite.status values (access-control arc, Task C1). Plain strings on a
# plain `String(20)` column below, deliberately NOT a StrEnum like
# AgentStatus/StepStatus above: those two drive an actual state-machine
# dispatch elsewhere (the provisioner/destroy pipelines branch on every
# member), while an invite's status is read only via direct equality/
# membership checks -- main.py's routes and `kobo_api.pairs.allowed_pairs`'s
# own "accepted" filter -- so a fourth enum class would add indirection
# without buying anything back. Named here anyway (rather than repeating the
# literal at every call site) so every reader points at the same spelling.
INVITE_PENDING = "pending"
INVITE_ACCEPTED = "accepted"
INVITE_DECLINED = "declined"
INVITE_REVOKED = "revoked"


class A2AInvite(Base):
    """Cross-group colleagueship, asked for and granted (access-control arc,
    Task C1, design spec section 5): one row per request from one agent
    (`from_agent`) to become an allowed dispatch pair with another agent
    (`to_agent`) outside the pair same-group talk already covers implicitly.
    `kobo_api.pairs.allowed_pairs` is the pure function that turns an
    ACCEPTED row here into a flat `(from, to)` pair for the switchboard --
    this table is kobo's own record of the ask and the decision, not
    something cabal ever reads directly.

    `from_agent`/`to_agent` are agent NAMES, not foreign keys to `agents.id`
    -- deliberately, mirroring `Agent.slack_app_id`/`slack_channel`'s own
    "free-text handle, not a hard reference" shape: an invite must be able
    to outlive the row it names long enough to render an honest history
    (main.py's list/detail routes resolve the name against the CURRENT
    `agents` table at read time, treating a name with no matching row as
    "that agent is gone" rather than raising), and a destroyed endpoint
    simply drops out of `allowed_pairs`'s own READY-agent query on its own.

    Destroy DOES clean these rows now (cleanup arc, 2026-08-26): the
    forget step deletes every invite naming the departing agent in either
    direction, so the outlive-the-row grace above only ever spans an
    in-flight destroy, not a departed agent's afterlife.
    """

    __tablename__ = "a2a_invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_agent: Mapped[str] = mapped_column(String(63), nullable=False)
    to_agent: Mapped[str] = mapped_column(String(63), nullable=False)
    # The verified IAP email that created this invite -- always the FROM
    # agent's owner or an operator (main.py's create-invite route enforces
    # this before ever constructing a row); this column just records who it
    # was, same "created_by" idiom `Agent.created_by` already established.
    # Nullable for the same IAP-disabled reason `Agent.created_by` is.
    requested_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=INVITE_PENDING
    )
    # The verified IAP email that accepted/declined/revoked this invite --
    # the TO agent's owner or an operator for accept/decline, either side's
    # owner or an operator for revoke. None while status is still pending.
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    # None while pending -- set the moment accept/decline/revoke fires, same
    # "nullable until the event happens" shape as AgentStep's own
    # started_at/finished_at above.
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# AgentUserGrant.role values (issue #309). Plain strings on a `String(16)`
# column, same "not a StrEnum" choice A2AInvite.status made above: a role is
# read by equality in two gates, never dispatched on by a pipeline.
GRANT_ROLE_VIEW = "view"
GRANT_ROLE_MANAGE = "manage"
GRANT_ROLES = (GRANT_ROLE_VIEW, GRANT_ROLE_MANAGE)


class AgentUserGrant(Base):
    """A per-user grant (access-control arc, Task C1, design spec section 6's
    "per-user additions ... for cross-group humans who need to see/chat with
    this agent"): `email` sees `agent_name` as if they owned it -- `main.py`'s
    `_visible_agents` (the single visibility choke point) reads this table
    directly rather than through any separate helper, so an agent's visible
    set never has two places that could drift apart.

    `role` (issue #309) says how far the grant reaches. `view` is the original
    meaning: see the agent, open it, chat with it. `manage` adds the owner's
    mutations: `main.py`'s `_owner_or_operator_or_403` lets a `manage` grantee
    through, so they edit skills, tooling, Slack, secrets and permission
    requests. Neither role reaches destroy (`_operator_block`, operator-only)
    nor the grants routes themselves: only the owner or an operator adds,
    changes or removes a grant, so a manager cannot widen the circle.

    Distinct from group-derived visibility (`Agent.group_name` matched
    against `GroupResolver.groups_for`): a grant is a direct, per-user
    record this room's own DB owns outright, so it survives an Okta outage
    untouched -- the Global Constraint's "fail-closed on grants" is about
    Okta-sourced group membership vanishing when the directory is
    unreachable, not about rows in this table, which were never Okta's to
    begin with.

    `agent_name` is a plain string, not a foreign key, same reasoning as
    `A2AInvite.from_agent`/`to_agent` above. Added and removed only by
    `agent_name`'s owner or an operator (main.py's grants routes).
    """

    __tablename__ = "agent_user_grants"
    __table_args__ = (UniqueConstraint("agent_name", "email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_name: Mapped[str] = mapped_column(String(63), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    # `view` or `manage` (GRANT_ROLES above). Server default so every row from
    # before issue #309 keeps the meaning it had.
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default=GRANT_ROLE_VIEW, server_default=GRANT_ROLE_VIEW
    )
    # The verified IAP email that added this grant (the agent's owner or an
    # operator); nullable for the same IAP-disabled reason as elsewhere.
    added_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )


# GroupRequest.status values (shared-group + group-requests wave). Plain
# strings on a plain `String(20)` column below, same "not a StrEnum" choice
# A2AInvite.status made above and for the identical reason: a request's
# status is read only via direct equality/membership checks (main.py's
# routes), never dispatched on by a state-machine pipeline the way AgentStatus/
# StepStatus are -- a fourth/fifth enum class would add indirection without
# buying anything back. Named here anyway so every reader points at the same
# spelling.
GROUP_REQUEST_PENDING = "pending"
GROUP_REQUEST_DONE = "done"
GROUP_REQUEST_DECLINED = "declined"


class GroupRequest(Base):
    """A request from a signed-in person for a NEW directory group kobo
    doesn't know about yet to be added to org config (shared-group +
    group-requests wave) -- the dropdown's escape hatch for "my team's group
    isn't listed." Anyone signed in may file one (`POST /api/group-requests`,
    main.py); only a room admin (`_is_admin`) may read the queue or close one
    out, since a request carries another human's name and free-text note.

    `done` means an admin added `group_name` to org config OUT-OF-BAND (this
    room never writes org config itself) -- the route that flips a row to
    `done` just closes the loop on kobo's own side, the same "this table
    records the ask and the decision, not the mechanism" posture `A2AInvite`
    already takes for its own accept/decline. `declined` means an admin
    reviewed it and chose not to. Both are terminal; `pending` is the only
    status a `done`/`declined` route may act on (closing an already-decided
    row again is a 409, mirroring `A2AInvite`'s own already-decided guard).

    `group_name` is free text, not validated against `Config.groups` --
    that's the entire point: it names a group the room does NOT yet
    recognize. Capped at 100 characters (`main.py`'s create route), a
    request body, not an agent name or anything with `Agent.name`'s own
    stricter 63-char/DNS-label shape.
    """

    __tablename__ = "group_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # The verified IAP email that filed this request; nullable for the same
    # IAP-disabled reason `A2AInvite.requested_by` is.
    requested_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=GROUP_REQUEST_PENDING
    )
    # The verified IAP email that closed this request out (done/declined);
    # None while pending, same shape as `A2AInvite.decided_by`.
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Thread(Base):
    """A shared, multi-agent conversation (threads arc, Task 8): every
    participant sees the same messages, but the thread itself is a room
    object the initiating human owns, not a colleague pairing between the
    agents in it.

    `initiator_email` is the verified IAP email that created the thread
    (`request.state.iap_email`, same "captured once at create time" idiom
    `Agent.created_by`/`A2AInvite.requested_by` already establish) -- but
    unlike those two, this field is also the room's ONLY visibility check
    for the thread (operator ruling: "a thread is readable by its
    initiator and by nobody else," no owner-or-operator carve-out the way
    every other gate in this room grants). Nullable for the same
    IAP-disabled reason `created_by` is: with no verified email ever
    available, a room that never turned IAP on (local dev, most of this
    test suite) still needs to create and read back its own threads.

    Participants live in `ThreadParticipant` below rather than as a JSON
    column here, matching this file's own relational convention for a
    one-to-many collection (`AgentStep` off `Agent`) rather than reaching
    for `Agent.skills`'s JSON-column shortcut, since a participant needs
    its own identity to query against (`kobo_api.pairs.implicitly_paired`/
    `A2AInvite` lookups when a new one joins) rather than being an opaque
    blob the room only ever reads back whole.

    This table postdates every other one in this file, but -- same as
    `MCPRefreshToken`/`MCPToolCallLog` below -- needs no manual ALTER
    anywhere at rollout: a whole new table is exactly what `Base.metadata.
    create_all` (`kobo_api.db.init_db`) already handles for free on a
    database that has never seen it.
    """

    __tablename__ = "threads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    initiator_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    participants: Mapped[list[ThreadParticipant]] = relationship(
        back_populates="thread", order_by="ThreadParticipant.id"
    )


class ThreadParticipant(Base):
    """One agent's seat in a `Thread` (threads arc, Task 8). Row order
    (`id`, the insertion order) is the turn order every thread-turn relay
    walks -- sequential, never a fan-out, and never the same agent twice in
    one turn, which the unique constraint below makes a schema-level
    guarantee rather than a caller-side promise.

    `agent_name` is a plain string, not a foreign key to `agents.id` --
    same "free-text handle, not a hard reference" shape `A2AInvite.
    from_agent`/`to_agent` already establish, for the identical reason: a
    thread must be able to outlive a participant that is later destroyed
    long enough to render an honest history, rather than cascading the
    delete or raising a foreign-key error.
    """

    __tablename__ = "thread_participants"
    __table_args__ = (UniqueConstraint("thread_id", "agent_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("threads.id"), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(63), nullable=False)

    thread: Mapped[Thread] = relationship(back_populates="participants")


class MCPRefreshToken(Base):
    """One row per live refresh-token `jti` (MCP server arc, Task 3, the
    design spec's "Token mechanics"): the only one of kobo's four MCP
    token types that gets persisted -- state/authorization_code/
    access_token are stateless HS256 JWTs, verified on their own claims
    alone (`kobo_api.mcp.jwt`), but a refresh token's whole point
    (rotate-on-use, replay revokes the family) needs a place to remember
    what already happened to a given jti. See `kobo_api.mcp.refresh_
    tokens` for the create/consume/revoke logic that reads and writes
    this table -- this class is just the shape.

    `family_id` links every jti a single OAuth grant has ever rotated
    through: `create_family` mints it once (a fresh uuid4 hex) for the
    grant's first jti, and every later rotation inserts its new jti into
    that same family rather than starting a new one. `email` is the
    verified Google Workspace email the grant belongs to -- stored here
    rather than trusted from the caller alone, so `consume` can tell a
    jti presented for the wrong email apart from a genuine replay (see
    that function's own docstring). `rotated_at` is set the moment this
    jti is successfully exchanged for a new one; `revoked_at`/
    `revoked_reason` are set together, either by a direct revoke or by
    replay detection, never one without the other.

    This table postdates every other one in this file, but unlike
    `Agent`/`A2AInvite`'s own added-later columns, it needs no manual
    ALTER anywhere: a whole new table is exactly what `Base.metadata.
    create_all` (`kobo_api.db.init_db`) already handles for free on a
    database that has never seen it -- the ALTER-needed comments
    elsewhere in this file are only for a new column landing on an
    EXISTING table; a fresh table needs nothing extra.
    """

    __tablename__ = "mcp_refresh_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    family_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class MCPToolCallLog(Base):
    """One row per MCP tool call attempt (MCP server arc, Task 6): who
    called what, whether it succeeded, how long it took, and a redacted
    copy of the arguments it was called with -- the audit trail
    `kobo_api.mcp.audit.audited` writes on every tool invocation that
    actually ran. A call the rate limiter refused before the tool ever ran
    writes no row here at all -- see that module's own docstring for why.

    `redacted_args` is a JSON-encoded object (TEXT column, the same "JSON
    text in a plain column" idiom `Agent.purpose_tags`/`slack_channels`
    already use elsewhere in this file), with every key `kobo_api.mcp.
    audit.SENSITIVE_KEYS` names replaced by a fixed marker before this row
    is ever written -- this table is safe for an operator to read without
    risking exposure of whatever secret a caller happened to pass a tool
    (a Slack bot token, a teach note that happens to paste one). `error`
    holds the exception's own truncated `str()` and is populated only when
    `status` is `"error"`.

    This table postdates every other one in this file, same as
    `MCPRefreshToken` above, so it needs no manual ALTER anywhere: a fresh
    `Base.metadata.create_all` (`kobo_api.db.init_db`) creates it for free
    on any database that has never seen it.
    """

    __tablename__ = "mcp_tool_call_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    tool: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    redacted_args: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )


class RepoAccess(Base):
    """One row per (agent, repository) PR-access grant request (agent PR
    broker, 2026-08-27 spec). The room-side HALF of the two-wall token
    model: the GitHub App installation's own repo list is the outer wall,
    and these rows -- only ever the APPROVED ones -- are the inner one the
    broker pins each minted token's `repositories` to.

    Lifecycle: requested -> approved | revoked, decided in BATCH by the
    operator set (2026-08-27 ruling 1: the kobo owner group approves, many
    repos at once). Revoked rows stay put as the audit trail; a re-request
    flips the same row back to requested rather than growing a duplicate.
    Statuses live in REPO_ACCESS_STATUSES; `repository` is always
    "owner/name".

    Postdates every table in this file, so `Base.metadata.create_all`
    (`kobo_api.db.init_db`) creates it for free -- same no-ALTER-needed
    convention `MCPToolCallLog` documents above.

    Retired with the broker (kobo#365, 2026-11-01): an agent's GitHub access
    is now a repository set and a level in the permission service
    (`kobo_api.github_access`). The rows stay as the record the boot-time
    migration reads: `migrated_at` marks an approved row whose repository
    has been moved onto the agent's child integration, so a boot never moves
    it twice. Destroy still clears an agent's rows by name.
    """

    __tablename__ = "repo_access"
    __table_args__ = (UniqueConstraint("agent_name", "repository"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_name: Mapped[str] = mapped_column(String(100), nullable=False)
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="requested")
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    migrated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


REPO_ACCESS_REQUESTED = "requested"
REPO_ACCESS_APPROVED = "approved"
REPO_ACCESS_REVOKED = "revoked"
REPO_ACCESS_STATUSES = (REPO_ACCESS_REQUESTED, REPO_ACCESS_APPROVED, REPO_ACCESS_REVOKED)


class AgentLearning(Base):
    """One candidate memory snippet awaiting a human verdict (daily-learnings
    spec, 2026-09-01): the nightly summarizer's output parked for curation.
    Nothing in this table is memory -- a row becomes memory only when the
    approve route pushes it through the teach lane, and a denied or expired
    row is a tombstone whose hash blocks the identical content from ever
    being proposed again (the unique constraint below; the nightly upsert
    skips any existing hash regardless of status).

    Statuses live in LEARNING_STATUSES; `day` is the nightly run's ISO
    date; `content_hash` is SHA-256 of `content`. `agent_name` is a plain
    string, not a foreign key, same reasoning as `A2AInvite.from_agent`
    above.

    Postdates every table in this file, so `Base.metadata.create_all`
    (`kobo_api.db.init_db`) creates it for free -- same no-ALTER-needed
    convention `MCPToolCallLog` documents above. The migration under
    migrations/versions/ exists only so test_migrations.py's baseline-vs-
    models parity check keeps passing; the image ships migrations/ and runs
    `alembic upgrade head` at boot, so columns added after the table's
    birth (like `said_by`) arrive by migration.
    """

    __tablename__ = "agent_learnings"
    __table_args__ = (UniqueConstraint("agent_name", "content_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_name: Mapped[str] = mapped_column(String(100), nullable=False)
    day: Mapped[str] = mapped_column(String(10), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(512), nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Who stated it, as the conversation shows them (learnings rethink,
    # 2026-09-16). Nullable: v1 candidates and every pre-rethink row have none.
    said_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="pending")
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )


class AgentSystemTask(Base):
    """How a system task the room sends an agent (the nightly learnings pass
    today) last went for that agent (issue #142). Without it a task the
    runtime refuses every night ("Unknown system task") left nothing but a
    log line, and the agent looked healthy. One row per (agent, task),
    updated in place by the sweep; `consecutive_failures` resets on success.
    `agent_name` is a plain string, same as `AgentLearning.agent_name`."""

    __tablename__ = "agent_system_tasks"
    __table_args__ = (UniqueConstraint("agent_name", "task"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_name: Mapped[str] = mapped_column(String(100), nullable=False)
    task: Mapped[str] = mapped_column(String(32), nullable=False)
    last_run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


LEARNING_PENDING = "pending"
LEARNING_APPROVED = "approved"
LEARNING_DENIED = "denied"
LEARNING_EXPIRED = "expired"
LEARNING_STATUSES = (
    LEARNING_PENDING,
    LEARNING_APPROVED,
    LEARNING_DENIED,
    LEARNING_EXPIRED,
)


# --- The admin panel's store (design 2026-10-18, Arc 2 data model) ---------
#
# Prefixed `admin_` rather than a Postgres schema: SQLite, the local and test
# lane, has no schemas, and a prefix draws the same line. Typed columns are
# what the panes query on; a JSON body holds what varies by kind, with a
# `spec_version` so code can upgrade old rows on read and no change to a
# body's shape needs a migration. Bodies on Postgres are JSONB (indexable);
# on SQLite plain JSON text.

_JSON_BODY = JSON().with_variant(JSONB(), "postgresql")


class AdminAuditEvent(Base):
    """The admin panel's own event table, before the room had one audit log
    (issue #316). Kept so the schema stays whole; its rows were copied into
    `audit_log` once at boot (`kobo_api.audit.migrate_admin_events`) and
    nothing reads or writes it since."""

    __tablename__ = "admin_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(320), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[str] = mapped_column(String(200), nullable=False)
    landed: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    landed_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    before: Mapped[dict | None] = mapped_column(_JSON_BODY, nullable=True)
    after: Mapped[dict | None] = mapped_column(_JSON_BODY, nullable=True)


class AuditLog(Base):
    """The room's one audit log (issue #316): every action a person, the
    room or an agent took that the room saw, as GitHub's organization log
    shapes it. `actor` is an email, `room`, or `agent:<name>`; `action` is
    dotted (`agent.create`, `skills.add`, `access.approve`); `summary` is
    the sentence a reader sees; `detail` is bounded JSON that never holds a
    secret (`kobo_api.audit.bound_detail`); `landed` says how far an action
    that projects somewhere (a grant, a pull request) has got. Written only
    through `kobo_api.audit.record`; pruned nightly after
    `Config.audit_retention_days`. The admin store's own event table
    (`admin_audit_events`) folded into this one at boot and is read no more."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="person")
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(200), nullable=False)
    agent_name: Mapped[str | None] = mapped_column(String(63), nullable=True, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="panel")
    request_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    detail: Mapped[dict | None] = mapped_column(_JSON_BODY, nullable=True)
    landed: Mapped[str | None] = mapped_column(String(16), nullable=True)
    landed_detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class AdminSetting(Base):
    """One named value the panel owns and no table names yet: the additive
    escape hatch. `key` is from a registry in code (kobo_api.admin_store),
    never free text, so this stays a settings table and not a junk drawer.
    `version` increments on every write for optimistic concurrency."""

    __tablename__ = "admin_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict] = mapped_column(_JSON_BODY, nullable=False)
    spec_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(320), nullable=False)


class SlackRelayApp(Base):
    """One Slack app the room made for an agent (issue #371): the relay id
    Slack posts to, and what the relay needs on every event without a
    round trip to the secrets platform.

    The relay id is minted and the row written BEFORE `apps.manifest.create`
    is called, because Slack posts its `url_verification` challenge to the
    request URL during that call and nothing else about the app exists yet;
    `app_id`, the credentials and `wired_at` fill in as the create and the
    wiring land, and `installed_at` when the owner finishes Install to
    Slack. `oauth_state` is the single-use nonce the install link carries
    (`<relay_id>.<nonce>`), cleared when the callback consumes it.

    What is held here and why (the PR says this too): the signing secret
    (verifies that an event came from Slack), the relay secret (lets the
    room post to the agent) and the client secret (exchanges the install
    code once) are stored as the room's other credentials are, plain in
    the room's own store. The room has no key for anything encrypted at
    rest, and its other held tokens live in a Secret in its namespace with
    the same reach. None of the three can read Slack or post as the agent;
    the bot token can, and it goes through the agent's secrets lane only and
    is never written here."""

    __tablename__ = "slack_relay_apps"

    relay_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    agent_name: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    app_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    client_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    client_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    signing_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    relay_secret: Mapped[str] = mapped_column(Text, nullable=False)
    oauth_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    oauth_state_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    workspace_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    app_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    wired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    installed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AdminIntegration(Base):
    """One vendor the room holds a base credential for (an integration: what
    an agent reaches out to). The credential itself lives in the vault at
    `credential_ref`; this row says it exists, how wide it is, and whether
    the gateway serves it yet. `spec` holds what varies by vendor (the
    gateway target's address, trust tier, scopes) under `spec_version`."""

    __tablename__ = "admin_integrations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    vendor: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    base_access: Mapped[str] = mapped_column(String(16), nullable=False, default="read")
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    state_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    credential_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    spec: Mapped[dict] = mapped_column(_JSON_BODY, nullable=False, default=dict)
    spec_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AdminRole(Base):
    """A named set of permissions a group holds. `spec` carries the lists
    (capabilities, admin permissions, per-integration ceilings, models) so a
    new capability or vendor is a body change, never a migration."""

    __tablename__ = "admin_roles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    spec: Mapped[dict] = mapped_column(_JSON_BODY, nullable=False, default=dict)
    spec_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    groups: Mapped[list[AdminRoleGroup]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )


class AdminRoleGroup(Base):
    """Which groups hold a role: one row per (role, group)."""

    __tablename__ = "admin_role_groups"

    role_id: Mapped[str] = mapped_column(ForeignKey("admin_roles.id"), primary_key=True)
    group_name: Mapped[str] = mapped_column(String(120), primary_key=True)
    role: Mapped[AdminRole] = relationship(back_populates="groups")


class AdminGrant(Base):
    """An agent's lane to one integration: read or write, never wider than
    the integration's base access. `state` follows the projection to the
    gateway (pending, applied, failed, revoked); `spec` holds the pull
    request link and the applied digest."""

    __tablename__ = "admin_grants"
    __table_args__ = (UniqueConstraint("agent_name", "integration_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    integration_id: Mapped[str] = mapped_column(ForeignKey("admin_integrations.id"), nullable=False)
    access: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    state_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    spec: Mapped[dict] = mapped_column(_JSON_BODY, nullable=False, default=dict)
    spec_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    requested_by: Mapped[str] = mapped_column(String(320), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class MCPRedeemedCode(Base):
    """One row per MCP authorization code the room has already exchanged
    (issue #196). The code itself is a stateless JWT, so without this table
    nothing remembers that it was spent, and two `/token` calls within its
    60 s life both succeed. The code's own `jti` is the primary key: the
    second redemption fails the insert, and `kobo_api.mcp.refresh_tokens.
    redeem_code` turns that into "replay", which revokes the family the first
    redemption produced. Rows are small and short-lived in meaning (a code
    lives 60 s), kept for the audit trail rather than pruned."""

    __tablename__ = "mcp_redeemed_codes"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    family_id: Mapped[str] = mapped_column(String(64), nullable=False)
    redeemed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )


class MCPRevocation(Base):
    """A person whose MCP access the room has cut off (issue #197): every
    refresh family they held is revoked at the same moment, and an access
    token issued before `revoked_at` is refused by `kobo_api.mcp.server`
    even though it is otherwise valid. Keyed by email, so revoking twice
    moves the instant forward rather than adding a row. Sign-in again after
    this (a fresh OAuth grant) is allowed: the row is a cut, not a ban."""

    __tablename__ = "mcp_revocations"

    email: Mapped[str] = mapped_column(String(255), primary_key=True)
    revoked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_by: Mapped[str] = mapped_column(String(320), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

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
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


class AgentStatus(enum.StrEnum):
    PENDING = "pending"
    PROVISIONING = "provisioning"
    READY = "ready"
    FAILED = "failed"
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

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
    simply drops out of `allowed_pairs`'s own READY-agent query on its own,
    with no cascade/cleanup step needed here.
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


class AgentUserGrant(Base):
    """A per-user visibility grant (access-control arc, Task C1, design spec
    section 6's "per-user additions ... for cross-group humans who need to
    see/chat with this agent"): `email` sees `agent_name` as if they owned
    it -- `main.py`'s `_visible_agents` (the single visibility choke point)
    reads this table directly rather than through any separate helper, so
    an agent's visible set never has two places that could drift apart.

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

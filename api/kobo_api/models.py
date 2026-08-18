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

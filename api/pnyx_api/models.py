"""SQLAlchemy models for pnyx-api's agent ledger."""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


class AgentStatus(enum.StrEnum):
    PENDING = "pending"
    PROVISIONING = "provisioning"
    READY = "ready"
    FAILED = "failed"


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
    # never persisted anywhere; see pnyx_api.slack_setup.wire_slack.
    slack_app_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    slack_channel: Mapped[str | None] = mapped_column(String(64), nullable=True)
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

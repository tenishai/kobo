"""SQLAlchemy models for cortex-api's agent ledger."""

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
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    skills: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=AgentStatus.PENDING
    )
    repo_full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gojira_app_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
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

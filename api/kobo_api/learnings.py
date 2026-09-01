"""Daily learnings, the room half (2026-09-01 spec): validate what agents
propose, park it in agent_learnings for human curation, expire what nobody
reviews, and know which agents are due a nightly summarization.

The pod-side summarizer (daedalus.memory.learnings) validates its own
output before replying; everything here re-validates from scratch anyway.
Two independent walls: the room never trusts pod output, exactly as the
PR broker never trusts an agent's repo claims.

Nothing in this module writes memory. store_candidates writes CANDIDATE
rows; only the approve route (main.py) ever turns one into a teach.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from kobo_api.models import (
    LEARNING_EXPIRED,
    LEARNING_PENDING,
    AgentLearning,
)

log = logging.getLogger("kobo.learnings")

CANDIDATE_CAP = 10
CONTENT_MAX_CHARS = 400
EVIDENCE_MAX_CHARS = 200
SOURCE_MAX_CHARS = 512
KINDS = ("fact", "procedure")
EXPIRY_DAYS = 14
DUE_AFTER_HOURS = 24


def content_hash(content: str) -> str:
    """SHA-256 of the candidate's content -- the dedupe identity. A denied
    row's hash blocks the identical content forever; a rephrasing is a new
    hash and gets a fresh hearing."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _naive_utc(moment: datetime) -> datetime:
    """DB rows store naive UTC (SQLAlchemy DateTime strips tzinfo on both
    engines this room runs); every comparison here normalizes to match."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(UTC).replace(tzinfo=None)


def parse_reply(reply: str) -> list[dict]:
    """The agent's JSON reply -> validated candidate dicts. Garbage in
    (prose, wrong shape, missing fields) reads as no candidates, never an
    error -- one agent's bad output must not sink the nightly sweep."""
    cleaned = reply.strip()
    if cleaned.startswith("```"):
        inner = cleaned.split("```")
        cleaned = inner[1] if len(inner) > 1 else ""
        cleaned = cleaned.removeprefix("json").strip()
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return []
    raw = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    candidates: list[dict] = []
    for item in raw[:CANDIDATE_CAP]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).strip()
        kind = str(item.get("kind", "")).strip()
        source = str(item.get("source", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        if not content or kind not in KINDS or not source:
            continue
        candidates.append(
            {
                "content": content[:CONTENT_MAX_CHARS],
                "kind": kind,
                "source": source[:SOURCE_MAX_CHARS],
                "evidence": evidence[:EVIDENCE_MAX_CHARS],
            }
        )
    return candidates


def store_candidates(
    session: Session, agent_name: str, day: str, candidates: list[dict]
) -> int:
    """Upsert candidates as pending rows, skipping any content_hash this
    agent has EVER seen (any status) -- denial and expiry are permanent
    tombstones against re-proposal. Returns how many rows were stored."""
    stored = 0
    for candidate in candidates[:CANDIDATE_CAP]:
        digest = content_hash(candidate["content"])
        exists = (
            session.query(AgentLearning)
            .filter_by(agent_name=agent_name, content_hash=digest)
            .first()
        )
        if exists is not None:
            continue
        session.add(
            AgentLearning(
                agent_name=agent_name,
                day=day,
                content=candidate["content"],
                kind=candidate["kind"],
                source=candidate["source"],
                evidence=candidate.get("evidence", ""),
                content_hash=digest,
            )
        )
        stored += 1
    session.commit()
    return stored


def expire_stale(session: Session, now: datetime) -> int:
    """Pending rows older than EXPIRY_DAYS flip to expired -- the queue
    stays livable, and their hashes keep blocking re-proposal."""
    cutoff = _naive_utc(now) - timedelta(days=EXPIRY_DAYS)
    rows = (
        session.query(AgentLearning)
        .filter(
            AgentLearning.status == LEARNING_PENDING,
            AgentLearning.created_at < cutoff,
        )
        .all()
    )
    for row in rows:
        row.status = LEARNING_EXPIRED
    session.commit()
    return len(rows)


def due_agents(session: Session, ready_names: list[str], now: datetime) -> list[str]:
    """Which ready agents get a summarization this sweep: those with no
    rows at all (never summarized) or whose newest row is older than
    DUE_AFTER_HOURS. Ordering follows ready_names."""
    cutoff = _naive_utc(now) - timedelta(hours=DUE_AFTER_HOURS)
    due: list[str] = []
    for name in ready_names:
        latest = (
            session.query(func.max(AgentLearning.created_at))
            .filter(AgentLearning.agent_name == name)
            .scalar()
        )
        if latest is None or latest <= cutoff:
            due.append(name)
    return due


def seconds_until_next_utc_midnight(now: datetime) -> float:
    """Sleep till the next UTC midnight, floored at 60s so a sweep that
    finishes seconds before midnight never busy-loops."""
    moment = now.astimezone(UTC)
    tomorrow = (moment + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(60.0, (tomorrow - moment).total_seconds())

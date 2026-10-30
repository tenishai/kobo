"""The room's audit log (issue #316): one table, one writer, one reader.

Every mutating route and every background decision calls `record` with a
sentence built server-side ("kgreiner created agent ward"), so the Audit
page reads like a log and not like a list of panel edits. The rules here:

- **The actor comes from the request** when there is one: the signed-in
  email, or `room` when nothing signed in (a dev room, a boot task). A
  background pass names itself `room`; an agent acting through the room is
  `agent:<name>`.
- **Detail is bounded and never a secret.** `bound_detail` clips keys and
  values and drops any key named like a token, a secret, a key or a
  password, whatever its value. Nothing else writes the column.
- **Rows are truth, projections land later.** An action that projects
  somewhere (a grant, a pull request) starts `pending` and `land` moves the
  latest row for that target to `applied` or `failed` with one sentence.
- **Retention is a deployment setting.** `prune` deletes rows older than
  `Config.audit_retention_days` and records one `audit.prune` row saying how
  many went, so the log can say what it forgot.

The admin store's own event table folded into this one: `migrate_admin_events`
copies it once at boot (idempotent) and `kobo_api.admin_store.record` is a
thin call into `record` since.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import Request
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from .models import AdminAuditEvent, AuditLog
from .timefmt import rfc3339

ACTOR_PERSON = "person"
ACTOR_ROOM = "room"
ACTOR_AGENT = "agent"

OUTCOME_OK = "ok"
OUTCOME_FAILED = "failed"
OUTCOME_REFUSED = "refused"

SOURCE_PANEL = "panel"
SOURCE_API = "api"
SOURCE_MCP = "mcp"
SOURCE_RUNTIME = "runtime"
SOURCE_PERMISSION_SERVICE = "permission-service"

LANDED_PENDING = "pending"
LANDED_APPLIED = "applied"
LANDED_FAILED = "failed"

ROOM_ACTOR = "room"

# Detail bounds: a row is a line in a log, not a copy of the request.
MAX_DETAIL_KEYS = 40
MAX_DETAIL_DEPTH = 3
MAX_VALUE_CHARS = 400
MAX_LIST_ITEMS = 50
# A key named like any of these never reaches the column, whatever it
# holds: the Slack config token, a sero credential, an app's private key.
_SECRET_KEY = re.compile(r"token|secret|password|passwd|credential|(^|_)key($|_)|api_key|private", re.IGNORECASE)
_REDACTED = "[redacted]"

PAGE_DEFAULT = 50
PAGE_MAX = 200
EXPORT_MAX = 50_000

EXPORT_COLUMNS = (
    "id", "at", "actor", "actor_kind", "action", "target_kind", "target_id", "agent_name",
    "summary", "outcome", "source", "request_ip", "user_agent", "landed", "landed_detail", "detail",
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# --- bounding -----------------------------------------------------------------


def bound_detail(detail: Any, *, _depth: int = 0) -> Any:
    """`detail` as the column may hold it: at most `MAX_DETAIL_KEYS` keys
    per object, `MAX_DETAIL_DEPTH` levels, strings clipped to
    `MAX_VALUE_CHARS`, lists to `MAX_LIST_ITEMS`, and every key named like a
    secret replaced by a marker rather than stored. A non-JSON value becomes
    its `str`, clipped."""
    if detail is None:
        return None
    if _depth >= MAX_DETAIL_DEPTH:
        return _clip(detail if isinstance(detail, str) else json.dumps(detail, default=str))
    if isinstance(detail, dict):
        out: dict[str, Any] = {}
        for i, (key, value) in enumerate(detail.items()):
            if i >= MAX_DETAIL_KEYS:
                out["_more"] = len(detail) - MAX_DETAIL_KEYS
                break
            name = _clip(str(key), 64)
            out[name] = _REDACTED if _SECRET_KEY.search(name) else bound_detail(value, _depth=_depth + 1)
        return out
    if isinstance(detail, (list, tuple, set)):
        items = list(detail)
        bounded = [bound_detail(v, _depth=_depth + 1) for v in items[:MAX_LIST_ITEMS]]
        if len(items) > MAX_LIST_ITEMS:
            bounded.append(f"... {len(items) - MAX_LIST_ITEMS} more")
        return bounded
    if isinstance(detail, (bool, int, float)) or detail is None:
        return detail
    return _clip(str(detail))


def _clip(text: str, limit: int = MAX_VALUE_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- the request's facts ------------------------------------------------------


def actor_from(request: Request | None) -> tuple[str, str]:
    """Who the request is: the signed-in email as a person, else the room."""
    email = (getattr(getattr(request, "state", None), "iap_email", None) or "").strip().lower()
    if email:
        return email, ACTOR_PERSON
    return ROOM_ACTOR, ACTOR_ROOM


def client_ip(request: Request | None) -> str | None:
    """The first hop of X-Forwarded-For (the perimeter writes it), else the
    peer address the server saw."""
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return _clip(first, 64)
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    return _clip(host, 64) if host else None


def user_agent(request: Request | None) -> str | None:
    if request is None:
        return None
    ua = request.headers.get("user-agent", "").strip()
    return _clip(ua, 300) if ua else None


def source_from(request: Request | None, default: str) -> str:
    """`request.state.audit_source` when a bridge set it (the MCP tools stamp
    `mcp`), else the caller's default."""
    stamped = getattr(getattr(request, "state", None), "audit_source", None)
    return stamped or default


def agent_actor(name: str) -> str:
    return f"agent:{name}"


def who(request: Request | None) -> str:
    """The actor's short name for a sentence: the email's local part
    ("kgreiner created agent ward"), or `the room`."""
    actor, kind = actor_from(request)
    return actor.split("@", 1)[0] if kind == ACTOR_PERSON else "the room"


# --- writes -------------------------------------------------------------------


def record(
    session: Session,
    *,
    action: str,
    target_kind: str,
    target_id: str,
    summary: str,
    actor: str | None = None,
    actor_kind: str | None = None,
    agent_name: str | None = None,
    outcome: str = OUTCOME_OK,
    source: str = SOURCE_PANEL,
    request: Request | None = None,
    detail: dict[str, Any] | None = None,
    landed: str | None = None,
    landed_detail: str | None = None,
    at: datetime | None = None,
) -> AuditLog:
    """Insert one row on the caller's session, uncommitted: the caller
    commits it with the change it describes. With `request`, the actor, the
    ip, the user agent and the source are read from it; an explicit `actor`
    still wins (a route acting for someone names them)."""
    derived_actor, _ = actor_from(request)
    actor = (actor or derived_actor).strip()
    if actor_kind is None:
        actor_kind = ACTOR_AGENT if actor.startswith("agent:") else ACTOR_ROOM if actor == ROOM_ACTOR else ACTOR_PERSON
    row = AuditLog(
        at=at or _now(),
        actor=_clip(actor, 320),
        actor_kind=actor_kind,
        action=_clip(action, 64),
        target_kind=_clip(target_kind, 64),
        target_id=_clip(target_id, 200),
        agent_name=_clip(agent_name, 63) if agent_name else None,
        summary=summary,
        outcome=outcome,
        source=source_from(request, source),
        request_ip=client_ip(request),
        user_agent=user_agent(request),
        detail=bound_detail(detail),
        landed=landed,
        landed_detail=landed_detail,
    )
    session.add(row)
    return row


def latest_for(session: Session, target_kind: str, target_id: str) -> AuditLog | None:
    return (
        session.query(AuditLog)
        .filter_by(target_kind=target_kind, target_id=target_id)
        .order_by(AuditLog.at.desc(), AuditLog.id.desc())
        .first()
    )


def land(session: Session, target_kind: str, target_id: str, landed: str, detail: str | None = None) -> None:
    """The row follows its target (issue #307): the most recent row for
    (target_kind, target_id) lands the way the projection reported, with the
    same sentence. Uncommitted."""
    row = latest_for(session, target_kind, target_id)
    if row is not None:
        row.landed, row.landed_detail = landed, detail


def mark_landed(session: Session, row_id: int, *, landed: str, detail: str | None = None) -> None:
    row = session.get(AuditLog, row_id)
    if row is not None:
        row.landed, row.landed_detail = landed, detail


# --- retention ----------------------------------------------------------------


def prune(session: Session, *, retention_days: int, now: datetime | None = None) -> int:
    """Delete every row older than `retention_days` and record one
    `audit.prune` row saying how many went (none when nothing did).
    Uncommitted. A retention of zero or less keeps everything."""
    if retention_days <= 0:
        return 0
    now = now or _now()
    cutoff = now - timedelta(days=retention_days)
    gone = session.query(AuditLog).filter(AuditLog.at < cutoff).delete(synchronize_session=False)
    if gone:
        record(
            session, action="audit.prune", target_kind="audit_log", target_id="audit_log",
            actor=ROOM_ACTOR, actor_kind=ACTOR_ROOM, source=SOURCE_RUNTIME, at=now,
            summary=f"Pruned {gone} audit row{'s' if gone != 1 else ''} older than {retention_days} days",
            detail={"deleted": gone, "retention_days": retention_days, "before": rfc3339(cutoff)},
        )
    return gone


# --- the one-time fold of the admin store's events ----------------------------


def _event_to_row(event: AdminAuditEvent) -> dict[str, Any]:
    from .admin_store import summary as admin_summary  # circular at import time

    return {
        "at": event.at,
        "actor": event.actor,
        "action": event.action,
        "target_kind": event.object_type,
        "target_id": event.object_id,
        "summary": admin_summary(event.action, event.object_type, event.object_id, event.before, event.after),
        "landed": event.landed,
        "landed_detail": event.landed_detail,
        "detail": {"migrated_from": "admin_audit_events", "event_id": event.id, "before": event.before, "after": event.after},
    }


def migrate_admin_events(session: Session) -> int:
    """Copy `admin_audit_events` into `audit_log` once. Idempotent: when the
    newest old event already has a row with its time, actor, action and
    target the copy has been made and nothing is written. Uncommitted."""
    newest = session.query(AdminAuditEvent).order_by(AdminAuditEvent.id.desc()).first()
    if newest is None:
        return 0
    already = (
        session.query(AuditLog)
        .filter_by(at=newest.at, actor=newest.actor, action=newest.action,
                   target_kind=newest.object_type, target_id=newest.object_id)
        .first()
    )
    if already is not None:
        return 0
    copied = 0
    for event in session.query(AdminAuditEvent).order_by(AdminAuditEvent.id.asc()).all():
        fields = _event_to_row(event)
        agent_name = None
        if event.object_type == "grant" and "/" in event.object_id:
            agent_name = event.object_id.partition("/")[0]
        record(
            session, actor=fields["actor"], actor_kind=ACTOR_PERSON if "@" in fields["actor"] else ACTOR_ROOM,
            action=fields["action"], target_kind=fields["target_kind"], target_id=fields["target_id"],
            summary=fields["summary"], agent_name=agent_name, landed=fields["landed"],
            landed_detail=fields["landed_detail"], detail=fields["detail"], at=fields["at"],
        )
        copied += 1
    return copied


# --- reads --------------------------------------------------------------------


def row_dict(row: AuditLog) -> dict[str, Any]:
    """One row as the Audit page and the JSON export read it."""
    return {
        "id": row.id,
        "at": rfc3339(row.at),
        "actor": row.actor,
        "actor_kind": row.actor_kind,
        "action": row.action,
        "target_kind": row.target_kind,
        "target_id": row.target_id,
        "agent_name": row.agent_name,
        "summary": row.summary,
        "outcome": row.outcome,
        "source": row.source,
        "request_ip": row.request_ip,
        "user_agent": row.user_agent,
        "detail": row.detail,
        "landed": row.landed,
        "landed_detail": row.landed_detail,
    }


def encode_cursor(row: AuditLog) -> str:
    raw = f"{row.at.isoformat()}|{row.id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[datetime, int] | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        at_text, _, id_text = base64.urlsafe_b64decode(padded.encode()).decode().partition("|")
        return datetime.fromisoformat(at_text), int(id_text)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("cursor is not one this log handed out") from None


def parse_when(value: str | None, *, end: bool = False) -> datetime | None:
    """A `since`/`until` filter: an RFC 3339 instant, or a bare date, which
    as `until` means the whole of that day."""
    if not value or not value.strip():
        return None
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        day = date.fromisoformat(text)
        start = datetime(day.year, day.month, day.day)
        return start + timedelta(days=1) if end else start
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def filtered(
    session: Session,
    *,
    q: str | None = None,
    actor: str | None = None,
    action: str | None = None,
    agent: str | None = None,
    outcome: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
):
    """The query every read shares: the filters, no order, no page.
    `action` matches a family (`agent` takes `agent.create`) or one exact
    action; `until` is exclusive."""
    query = session.query(AuditLog)
    if q and q.strip():
        needle = f"%{q.strip()}%"
        query = query.filter(or_(
            AuditLog.actor.ilike(needle), AuditLog.action.ilike(needle),
            AuditLog.target_id.ilike(needle), AuditLog.summary.ilike(needle),
        ))
    if actor and actor.strip():
        query = query.filter(AuditLog.actor == actor.strip().lower())
    if action and action.strip():
        wanted = action.strip()
        query = query.filter(or_(AuditLog.action == wanted, AuditLog.action.like(wanted + ".%")))
    if agent and agent.strip():
        query = query.filter(AuditLog.agent_name == agent.strip())
    if outcome and outcome.strip():
        query = query.filter(AuditLog.outcome == outcome.strip())
    if since is not None:
        query = query.filter(AuditLog.at >= since)
    if until is not None:
        query = query.filter(AuditLog.at < until)
    return query


def page(session: Session, *, cursor: str | None = None, limit: int = PAGE_DEFAULT, **filters) -> dict[str, Any]:
    """One page, newest first, keyset on (at, id): `rows`, `next_cursor`
    (None on the last page), `total_in_window` (every row the filter
    matches) and the facets the page's selects offer within that window."""
    limit = max(1, min(int(limit or PAGE_DEFAULT), PAGE_MAX))
    base = filtered(session, **filters)
    total = base.with_entities(func.count(AuditLog.id)).scalar() or 0
    query = base.order_by(AuditLog.at.desc(), AuditLog.id.desc())
    after = decode_cursor(cursor)
    if after is not None:
        at, row_id = after
        query = query.filter(or_(AuditLog.at < at, and_(AuditLog.at == at, AuditLog.id < row_id)))
    rows = query.limit(limit + 1).all()
    more = len(rows) > limit
    rows = rows[:limit]
    actors = [a for (a,) in base.with_entities(AuditLog.actor).distinct().order_by(AuditLog.actor).all()]
    agents = [a for (a,) in base.with_entities(AuditLog.agent_name).distinct().order_by(AuditLog.agent_name).all() if a]
    actions = [a for (a,) in base.with_entities(AuditLog.action).distinct().all()]
    families = sorted({a.split(".", 1)[0] for a in actions})
    return {
        "rows": [row_dict(r) for r in rows],
        "next_cursor": encode_cursor(rows[-1]) if more and rows else None,
        "total_in_window": total,
        "actors": actors,
        "agents": agents,
        "families": families,
    }


def export(session: Session, *, fmt: str = "csv", **filters) -> Iterator[str]:
    """The current filter as a stream, newest first, at most `EXPORT_MAX`
    rows: CSV with a header row, or a JSON array. Read in slices so a large
    window never sits in memory whole."""
    query = filtered(session, **filters).order_by(AuditLog.at.desc(), AuditLog.id.desc())
    if fmt == "json":
        yield "["
        first = True
        for row in _slices(query):
            yield ("" if first else ",\n") + json.dumps(row_dict(row), default=str)
            first = False
        yield "]\n"
        return
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(EXPORT_COLUMNS)
    yield _take(buffer)
    for row in _slices(query):
        body = row_dict(row)
        writer.writerow([
            json.dumps(body[c], default=str) if c == "detail" and body[c] is not None else ("" if body[c] is None else body[c])
            for c in EXPORT_COLUMNS
        ])
        yield _take(buffer)


def _slices(query, size: int = 500) -> Iterator[AuditLog]:
    sent = 0
    offset = 0
    while sent < EXPORT_MAX:
        batch = query.offset(offset).limit(min(size, EXPORT_MAX - sent)).all()
        if not batch:
            return
        yield from batch
        sent += len(batch)
        offset += len(batch)


def _take(buffer: io.StringIO) -> str:
    text = buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)
    return text

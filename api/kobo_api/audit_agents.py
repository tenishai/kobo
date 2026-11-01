"""The Audit page's Agents tab (kobo#371, section 3): what agents did with their
connections, read live from the permission service and shaped like the room's own
audit rows (`kobo_api.audit.row_dict`), so the one pane renders both.

The service keeps leases and events in its own tables and the room never copies
them: a mint is a row of `GET /leases` (here `lease.minted`, outcome `ok`), a
refusal or a failed mint is a row of `GET /events` (`lease.refused`, `lease.failed`).
The two are merged newest first, each with one sentence: who borrowed or was
refused what, where, for how long, and for which task. Nothing here opens a
database session.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from .audit import (
    ACTOR_AGENT,
    OUTCOME_FAILED,
    OUTCOME_OK,
    OUTCOME_REFUSED,
    SOURCE_PERMISSION_SERVICE,
)
from .bab import CLOUD_LABELS, BabClient
from .clients import ClientError
from .timefmt import rfc3339

KIND_MINTED = "lease.minted"
KIND_REFUSED = "lease.refused"
KIND_FAILED = "lease.failed"
EVENT_KINDS = (KIND_REFUSED, KIND_FAILED)
OUTCOME_OF = {KIND_MINTED: OUTCOME_OK, KIND_REFUSED: OUTCOME_REFUSED, KIND_FAILED: OUTCOME_FAILED}
TARGET_KIND = "integration"

PAGE_DEFAULT = 200
PAGE_MAX = 1000

EXPORT_COLUMNS = (
    "id", "at", "actor", "actor_kind", "action", "target_kind", "target_id", "agent_name",
    "summary", "outcome", "source", "detail",
)


def page(bab: BabClient, actor: str, *, q: str = "", agent: str = "", outcome: str = "",
         since: datetime | None = None, until: datetime | None = None, limit: int = PAGE_DEFAULT) -> dict[str, Any]:
    """One window, newest first: `rows`, `total_in_window` and the facets the
    tab's selects offer (`agents`, `outcomes`). An outcome of `ok` reads leases
    only; `refused` or `failed` reads events only; nothing reads both."""
    limit = max(1, min(int(limit or PAGE_DEFAULT), PAGE_MAX))
    where = places(bab, actor)
    rows: list[dict[str, Any]] = []
    if not outcome or outcome == OUTCOME_OK:
        leases = bab.leases(agent, since, actor) if agent else bab.all_leases(since, actor)
        rows.extend(lease_row(lease, where) for lease in leases)
    if not outcome or outcome in (OUTCOME_REFUSED, OUTCOME_FAILED):
        kinds = [k for k in EVENT_KINDS if not outcome or OUTCOME_OF[k] == outcome]
        rows.extend(r for r in (event_row(e, where) for e in bab.events(since, until, kinds, agent or None, limit, actor))
                    if r is not None)
    rows = [r for r in rows if _matches(r, q=q, agent=agent, outcome=outcome, since=since, until=until)]
    rows.sort(key=lambda r: (r["at"], str(r["id"])), reverse=True)
    return {
        "rows": rows[:limit],
        "total_in_window": len(rows),
        "agents": sorted({r["agent_name"] for r in rows if r["agent_name"]}),
        "outcomes": sorted({r["outcome"] for r in rows}),
    }


def places(bab: BabClient, actor: str) -> dict[str, str]:
    """Integration id -> the words a sentence says for it: the vendor's label, and
    for a cloud account its name too ("AWS acme-prod"); a child of a service
    connection (an agent's repository set) reads as the connection. The service
    being unable to list is not a reason to show nothing: the id stands in."""
    try:
        vendors = {v["vendor"]: v for v in bab.vendors(actor)}
        integrations = bab.integrations(actor)
    except ClientError:
        return {}
    out: dict[str, str] = {}
    for row in integrations:
        vendor = row.get("vendor") or ""
        meta = vendors.get(vendor) or {}
        label = meta.get("label") or CLOUD_LABELS.get(vendor) or vendor or row["integration"]
        cloud = meta.get("kind") == "cloud" or (not meta and vendor in CLOUD_LABELS)
        name = row.get("name") or row["integration"]
        out[row["integration"]] = f"{label} {name}" if cloud and name != label else label
    return out


def lease_row(lease: dict, where: dict[str, str]) -> dict[str, Any]:
    agent = str(lease.get("agent") or "")
    integration = str(lease.get("integration") or "")
    permissions = [str(p) for p in (lease.get("permissions") or [])]
    issued, expires = _when(lease.get("issued_at")), _when(lease.get("expires_at"))
    sentence = f"{agent} borrowed {level(permissions)} on {where.get(integration, integration)}"
    if issued and expires:
        sentence += f" for {duration(issued, expires)}"
    if lease.get("task"):
        sentence += f": {lease['task']}"
    return _row(f"lease:{lease.get('id')}", issued, KIND_MINTED, agent, integration, sentence, {
        "permissions": permissions, "task": lease.get("task"), "via": lease.get("via"),
        "expires_at": rfc3339(expires) if expires else None, "lease_id": lease.get("id"), "vendor": lease.get("minter"),
    })


def event_row(event: dict, where: dict[str, str]) -> dict[str, Any] | None:
    kind = str(event.get("kind") or "")
    if kind not in OUTCOME_OF:
        return None
    detail = dict(event.get("detail") or {})
    agent = str(detail.get("agent") or event.get("actor") or "")
    integration = str(detail.get("integration") or "")
    permissions = [str(p) for p in (detail.get("permissions") or [])]
    reason = str(detail.get("reason") or detail.get("error") or "").strip()
    what, place = level(permissions), where.get(integration, integration or "a connection")
    verb = "was refused" if kind == KIND_REFUSED else "could not borrow"
    sentence = f"{agent} {verb} {what} on {place}" + (f": {reason}" if reason else "")
    kept = {k: detail.get(k) for k in ("permissions", "reason", "via", "on_behalf_of", "task") if detail.get(k) is not None}
    return _row(f"event:{event.get('id')}", _when(event.get("at")), kind, agent, integration, sentence, kept)


def level(permissions: list[str]) -> str:
    """What the sentence calls the permissions: `read` when every one reads,
    `write` when any one does more, `access` when the list is empty (the agent
    held nothing there)."""
    if not permissions:
        return "access"
    return "read" if all(p.endswith("_read") for p in permissions) else "write"


def duration(start: datetime, end: datetime) -> str:
    seconds = max(0, int((end - start).total_seconds()))
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 3600:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{max(1, seconds // 60)}m"


def export(rows: list[dict[str, Any]], *, fmt: str = "csv") -> Iterator[str]:
    """The window as a file: CSV with a header row, or a JSON array."""
    if fmt == "json":
        yield "["
        for i, row in enumerate(rows):
            yield ("" if i == 0 else ",\n") + json.dumps(row, default=str)
        yield "]\n"
        return
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(EXPORT_COLUMNS)
    for row in rows:
        writer.writerow([
            json.dumps(row[c], default=str) if c == "detail" and row[c] is not None else ("" if row[c] is None else row[c])
            for c in EXPORT_COLUMNS
        ])
    yield buffer.getvalue()


def _row(row_id: str, at: datetime | None, kind: str, agent: str, integration: str, sentence: str,
         detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row_id,
        "at": rfc3339(at) if at else "",
        "actor": f"agent:{agent}",
        "actor_kind": ACTOR_AGENT,
        "action": kind,
        "target_kind": TARGET_KIND,
        "target_id": integration,
        "agent_name": agent or None,
        "summary": sentence,
        "outcome": OUTCOME_OF[kind],
        "source": SOURCE_PERMISSION_SERVICE,
        "request_ip": None,
        "user_agent": None,
        "detail": detail,
        "landed": None,
        "landed_detail": None,
    }


def _matches(row: dict[str, Any], *, q: str, agent: str, outcome: str,
             since: datetime | None, until: datetime | None) -> bool:
    if agent and row["agent_name"] != agent.strip():
        return False
    if outcome and row["outcome"] != outcome.strip():
        return False
    at = _when(row["at"])
    if since is not None and (at is None or at < _aware(since)):
        return False
    if until is not None and (at is None or at >= _aware(until)):
        return False
    if q and q.strip():
        needle = q.strip().lower()
        return any(needle in str(row[c] or "").lower() for c in ("actor", "action", "target_id", "summary"))
    return True


def _when(value: Any) -> datetime | None:
    """An instant the service wrote (naive UTC) or the room did (RFC 3339), aware."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return _aware(parsed)


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


__all__ = ["EVENT_KINDS", "KIND_FAILED", "KIND_MINTED", "KIND_REFUSED", "PAGE_DEFAULT", "PAGE_MAX", "export", "page"]

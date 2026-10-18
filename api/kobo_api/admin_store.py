"""The admin panel's store: the one way panes write (design 2026-10-18, Arc 2
data model). Two rules, both enforced here:

- **Every write records an audit event in the same transaction**, with the
  before and after bodies. The Audit pane is that table read back.
- **Settings keys come from a registry**, not free text, so `admin_settings`
  stays a settings table. Add a key here when a pane needs one.

Rows are the source of truth; what the outside systems hold (a liminal grant,
a sero secret, a bab tuple, a pull request) is a projection, and the audit
event's `landed` says whether it has caught up.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from .models import AdminAuditEvent, AdminSetting

LANDED_PENDING = "pending"
LANDED_APPLIED = "applied"
LANDED_FAILED = "failed"

# Every settings key the panel may write, with the spec_version its body is
# at. A write with a key outside this map is a programming error, not data.
SETTING_KEYS: dict[str, int] = {
    # The panel's own switches, as a person left them (the org key turns the
    # panel on; this is what is inside it).
    "panel.preferences": 1,
}


class UnknownSettingKey(KeyError):
    pass


def record(
    session: Session,
    *,
    actor: str,
    action: str,
    object_type: str,
    object_id: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    landed: str = LANDED_PENDING,
    landed_detail: str | None = None,
) -> AdminAuditEvent:
    """Insert one audit event on the caller's session, uncommitted: the
    caller commits it with the row it describes."""
    event = AdminAuditEvent(
        at=datetime.now(UTC).replace(tzinfo=None),
        actor=actor,
        action=action,
        object_type=object_type,
        object_id=object_id,
        before=before,
        after=after,
        landed=landed,
        landed_detail=landed_detail,
    )
    session.add(event)
    return event


def mark_landed(session: Session, event_id: int, *, landed: str, detail: str | None = None) -> None:
    """The projection reported back: `applied` or `failed`, with one sentence."""
    event = session.get(AdminAuditEvent, event_id)
    if event is None:
        return
    event.landed = landed
    event.landed_detail = detail


def get_setting(session: Session, key: str) -> dict[str, Any] | None:
    if key not in SETTING_KEYS:
        raise UnknownSettingKey(key)
    row = session.get(AdminSetting, key)
    return None if row is None else dict(row.value)


def put_setting(session: Session, key: str, value: dict[str, Any], *, actor: str) -> AdminSetting:
    """Write one setting and its audit event on the caller's session. The
    version increments so two admins saving at once are visible as two
    events, not one silent overwrite."""
    if key not in SETTING_KEYS:
        raise UnknownSettingKey(key)
    now = datetime.now(UTC).replace(tzinfo=None)
    row = session.get(AdminSetting, key)
    before = None if row is None else dict(row.value)
    if row is None:
        row = AdminSetting(
            key=key, value=value, spec_version=SETTING_KEYS[key], version=1,
            updated_at=now, updated_by=actor,
        )
        session.add(row)
    else:
        row.value = value
        row.spec_version = SETTING_KEYS[key]
        row.version += 1
        row.updated_at = now
        row.updated_by = actor
    record(
        session, actor=actor, action="setting.put", object_type="setting", object_id=key,
        before=before, after=dict(value), landed=LANDED_APPLIED, landed_detail="stored",
    )
    return row


def audit_events(session: Session, *, limit: int = 200) -> list[AdminAuditEvent]:
    return (
        session.query(AdminAuditEvent)
        .order_by(AdminAuditEvent.at.desc(), AdminAuditEvent.id.desc())
        .limit(limit)
        .all()
    )

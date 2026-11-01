"""The admin panel's store: the one way panes write (design 2026-10-18, Arc 2
data model). Two rules, both enforced here:

- **Every write records an audit row in the same transaction**, with the
  before and after bodies, through `kobo_api.audit.record` (issue #316):
  the room's one audit log, which the Audit page reads back.
- **Settings keys come from a registry**, not free text, so `admin_settings`
  stays a settings table. Add a key here when a pane needs one.

Rows are the source of truth; what the outside systems hold (a liminal grant,
a sero secret, a bab tuple, a pull request) is a projection, and the audit
event's `landed` says whether it has caught up.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from . import audit
from .models import (
    AdminGrant,
    AdminIntegration,
    AdminRole,
    AdminRoleGroup,
    AdminSetting,
    AuditLog,
)
from .timefmt import rfc3339

LANDED_PENDING = audit.LANDED_PENDING
LANDED_APPLIED = audit.LANDED_APPLIED
LANDED_FAILED = audit.LANDED_FAILED

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
    request: Request | None = None,
) -> AuditLog:
    """Insert one audit row on the caller's session, uncommitted: the
    caller commits it with the row it describes. A thin call into
    `kobo_api.audit.record` (issue #316): the sentence is built here from
    the before and after bodies, which go into the row's bounded detail."""
    agent_name = object_id.partition("/")[0] if object_type == "grant" and "/" in object_id else None
    return audit.record(
        session, request=request, actor=actor, action=action, target_kind=object_type, target_id=object_id,
        summary=summary(action, object_type, object_id, before, after), agent_name=agent_name,
        detail={"before": before, "after": after}, landed=landed, landed_detail=landed_detail,
    )


def mark_landed(session: Session, event_id: int, *, landed: str, detail: str | None = None) -> None:
    """The projection reported back: `applied` or `failed`, with one sentence."""
    audit.mark_landed(session, event_id, landed=landed, detail=detail)


def _land_latest(session: Session, object_type: str, object_id: str, *, landed: str, detail: str | None) -> None:
    """The event follows the row (issue #307): when a row's state is set, the
    most recent event for that target lands the same way, with the same
    sentence. Without this a connect stayed Pending in the log forever."""
    audit.land(session, object_type, object_id, landed, detail)


def backfill_landed(session: Session) -> int:
    """One best-effort pass over events recorded before the log followed the
    row (issue #307), uncommitted. A pending connect or update whose row has
    since landed takes the row's state and sentence; a pending disconnect
    whose row is gone is applied, since the row going was the change. Cheap:
    two small tables, pending integration events only. Everything else is
    left as it is."""
    pending = (
        session.query(AuditLog)
        .filter_by(target_kind="integration", landed=LANDED_PENDING)
        .order_by(AuditLog.at.asc(), AuditLog.id.asc())
        .all()
    )
    fixed = 0
    for event in pending:
        row = session.get(AdminIntegration, event.target_id)
        if event.action == "integration.disconnect":
            if row is None:
                event.landed = LANDED_APPLIED
                event.landed_detail = "the connection was removed; recorded before the log followed outcomes"
                fixed += 1
        elif row is not None and row.state in (STATE_APPLIED, STATE_FAILED):
            event.landed, event.landed_detail = row.state, row.state_detail
            fixed += 1
    return fixed


# Rows recorded before kobo#377 folded the service rows into connections still read
# back with their own sentences.
_SERVICE_LABELS = {"slack": "Slack", "github": "the GitHub app"}


def _count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    return int(value or 0)


def _integration_phrase(body: dict[str, Any] | None, object_id: str) -> str:
    body = body or {}
    name = body.get("name") or object_id
    spec = body.get("spec") or {}
    if spec.get("kind") == "account":
        return f"account {name} under {spec.get('parent') or 'its organization'}"
    if spec.get("kind") == "org":
        return f"organization {name}"
    return str(name)


def summary(action: str, object_type: str, object_id: str, before: dict[str, Any] | None, after: dict[str, Any] | None) -> str:
    """One plain sentence for the Audit pane's Change column (issue #307),
    built from the event's kind, target and body. The raw `action` stays on
    the row for the quiet second line."""
    body = after if after is not None else before
    if object_type == "integration":
        verb = {"integration.connect": "Connected", "integration.update": "Updated", "integration.disconnect": "Disconnected",
                "integration.check_models": "Checked the models of"}.get(action)
        if verb:
            return f"{verb} {_integration_phrase(body, object_id)}"
    if object_type == "room_service":
        label = _SERVICE_LABELS.get(object_id, object_id)
        if action == "room_service.set":
            return f"Set {label} in room services"
        if action == "room_service.remove":
            return f"Removed {label} from room services"
    if object_type == "role":
        verb = {"role.create": "Created", "role.update": "Updated", "role.delete": "Deleted"}.get(action)
        if verb:
            return f"{verb} role {(body or {}).get('name') or object_id}"
    if object_type == "grant":
        agent, _, integration = object_id.partition("/")
        access = (body or {}).get("access") or "a"
        if action == "grant.create":
            return f"Gave {agent} a {access} lane to {integration}"
        if action == "grant.update":
            return f"Changed {agent}'s lane to {integration} to {access}"
        if action == "grant.revoke":
            return f"Revoked {agent}'s lane to {integration}"
    if action == "directory.sweep":
        retired, failed = _count((body or {}).get("retired")), _count((body or {}).get("failed"))
        return f"Swept the directory: retired {retired}" + (f", {failed} refused" if failed else "")
    if action == "setting.put":
        return "Saved panel preferences" if object_id == "panel.preferences" else f"Saved setting {object_id}"
    if action == "mcp.revoke":
        return f"Cut {object_id} off from the assistant connection"
    return f"{action.replace('.', ' ').capitalize()} {object_id}"


def event_dict(event: AuditLog) -> dict[str, Any]:
    """One audit row as the Audit page reads it (`kobo_api.audit.row_dict`)."""
    return audit.row_dict(event)


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


def audit_events(session: Session, *, limit: int = 200) -> list[AuditLog]:
    """The newest rows of the room's audit log; the Audit page reads
    `GET /api/admin/audit` (filters, pages) rather than this."""
    return (
        session.query(AuditLog)
        .order_by(AuditLog.at.desc(), AuditLog.id.desc())
        .limit(limit)
        .all()
    )


# --- integrations, roles, grants (step 3) -----------------------------------

ACCESS_LEVELS = ("read", "write")
CEILINGS = ("none", "read", "write", "approve")
STATE_PENDING = "pending"
STATE_APPLIED = "applied"
STATE_FAILED = "failed"
STATE_REVOKED = "revoked"

# What a role's body may hold, and the version it is at. The lists are open
# (a new capability or vendor is a new string), the keys are not.
ROLE_SPEC_KEYS = ("capabilities", "admin", "connectors", "models")
ROLE_SPEC_VERSION = 1
INTEGRATION_SPEC_VERSION = 1
GRANT_SPEC_VERSION = 1


class AdminStoreError(ValueError):
    """A write the store refuses, with a sentence a person can act on."""


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _slug(text: str) -> str:
    out = "".join(ch if ch.isalnum() else "-" for ch in text.strip().lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")[:64]


def integration_dict(row: AdminIntegration) -> dict[str, Any]:
    return {
        "id": row.id, "vendor": row.vendor, "name": row.name, "base_access": row.base_access,
        "state": row.state, "state_detail": row.state_detail, "credential_ref": row.credential_ref,
        "spec": dict(row.spec or {}), "created_by": row.created_by,
        "created_at": rfc3339(row.created_at), "updated_at": rfc3339(row.updated_at),
    }


def list_integrations(session: Session) -> list[AdminIntegration]:
    return session.query(AdminIntegration).order_by(AdminIntegration.name).all()


def put_integration(
    session: Session, *, vendor: str, name: str, base_access: str, spec: dict[str, Any],
    credential_ref: str | None, actor: str, integration_id: str | None = None,
) -> AdminIntegration:
    """Create or update one integration row and its audit event. The
    credential is never in the body: `credential_ref` names where the vault
    holds it. State starts `pending` until the projections report back."""
    if base_access not in ACCESS_LEVELS:
        raise AdminStoreError(f"base access must be one of {', '.join(ACCESS_LEVELS)}, not {base_access!r}")
    if not vendor.strip() or not name.strip():
        raise AdminStoreError("an integration needs a vendor and a name")
    now = _now()
    row_id = integration_id or _slug(vendor)
    row = session.get(AdminIntegration, row_id)
    before = None if row is None else integration_dict(row)
    if row is None:
        row = AdminIntegration(
            id=row_id, vendor=vendor, name=name, base_access=base_access, state=STATE_PENDING,
            credential_ref=credential_ref, spec=dict(spec), spec_version=INTEGRATION_SPEC_VERSION,
            created_by=actor, created_at=now, updated_at=now,
        )
        session.add(row)
        action = "integration.connect"
    else:
        row.vendor, row.name, row.base_access = vendor, name, base_access
        row.spec, row.spec_version = dict(spec), INTEGRATION_SPEC_VERSION
        if credential_ref is not None:
            row.credential_ref = credential_ref
        row.state, row.state_detail, row.updated_at = STATE_PENDING, None, now
        action = "integration.update"
    session.flush()
    record(session, actor=actor, action=action, object_type="integration", object_id=row.id,
           before=before, after=integration_dict(row))
    return row


def set_integration_state(session: Session, integration_id: str, *, state: str, detail: str | None) -> None:
    """The row's state, and the latest audit event for it lands the same way."""
    row = session.get(AdminIntegration, integration_id)
    if row is None:
        return
    row.state, row.state_detail, row.updated_at = state, detail, _now()
    _land_latest(session, "integration", integration_id, landed=state, detail=detail)


def remove_integration(
    session: Session, integration_id: str, *, actor: str, landed: str = LANDED_PENDING, detail: str | None = None,
) -> None:
    """Disconnect: the row goes, its grants go with it, and the audit event
    says so. Reversing the projections is the caller's job; a caller that
    has already done so passes how it went as `landed` and `detail`."""
    row = session.get(AdminIntegration, integration_id)
    if row is None:
        raise AdminStoreError(f"no integration {integration_id!r}")
    before = integration_dict(row)
    for grant in session.query(AdminGrant).filter_by(integration_id=integration_id).all():
        session.delete(grant)
    session.delete(row)
    record(session, actor=actor, action="integration.disconnect", object_type="integration",
           object_id=integration_id, before=before, after=None, landed=landed, landed_detail=detail)


def role_dict(row: AdminRole) -> dict[str, Any]:
    spec = dict(row.spec or {})
    return {
        "id": row.id, "name": row.name, "description": row.description or "", "builtin": row.builtin,
        "groups": sorted(g.group_name for g in row.groups),
        "capabilities": list(spec.get("capabilities", [])), "admin": list(spec.get("admin", [])),
        "connectors": list(spec.get("connectors", [])), "models": list(spec.get("models", [])),
        "updated_at": rfc3339(row.updated_at),
    }


BUILTIN_ORDER = ("admin", "operator", "member", "reader")


def list_roles(session: Session) -> list[AdminRole]:
    """Built-ins first in their natural order, then custom roles by name."""
    rows = session.query(AdminRole).all()
    rank = {rid: i for i, rid in enumerate(BUILTIN_ORDER)}
    return sorted(rows, key=lambda r: (0, rank.get(r.id, 99), "") if r.builtin else (1, 0, r.name.lower()))


def put_role(
    session: Session, *, name: str, description: str, groups: list[str], spec: dict[str, Any],
    actor: str, role_id: str | None = None, builtin: bool = False,
) -> AdminRole:
    """Create or update a role, its group rows and its audit event. Every
    connector ceiling is checked against the ceilings the panel knows."""
    if not name.strip():
        raise AdminStoreError("a role needs a name")
    unknown = sorted(set(spec) - set(ROLE_SPEC_KEYS))
    if unknown:
        raise AdminStoreError(f"a role body holds only {', '.join(ROLE_SPEC_KEYS)}; not {', '.join(unknown)}")
    for connector in spec.get("connectors", []):
        if connector.get("ceiling") not in CEILINGS:
            raise AdminStoreError(f"a ceiling must be one of {', '.join(CEILINGS)}")
    now = _now()
    row_id = role_id or _slug(name)
    row = session.get(AdminRole, row_id)
    before = None if row is None else role_dict(row)
    body = {key: list(spec.get(key, [])) for key in ROLE_SPEC_KEYS}
    if row is None:
        row = AdminRole(id=row_id, name=name, description=description, builtin=builtin, spec=body,
                        spec_version=ROLE_SPEC_VERSION, created_at=now, updated_at=now)
        session.add(row)
        action = "role.create"
    else:
        row.name, row.description, row.spec, row.spec_version, row.updated_at = name, description, body, ROLE_SPEC_VERSION, now
        action = "role.update"
    row.groups = [AdminRoleGroup(group_name=g) for g in sorted({g.strip() for g in groups if g.strip()})]
    session.flush()
    record(session, actor=actor, action=action, object_type="role", object_id=row.id,
           before=before, after=role_dict(row), landed=LANDED_APPLIED, landed_detail="stored")
    return row


def delete_role(session: Session, role_id: str, *, actor: str) -> None:
    row = session.get(AdminRole, role_id)
    if row is None:
        raise AdminStoreError(f"no role {role_id!r}")
    if row.builtin:
        raise AdminStoreError("a built-in role cannot be deleted; take its groups away instead")
    before = role_dict(row)
    session.delete(row)
    record(session, actor=actor, action="role.delete", object_type="role", object_id=role_id,
           before=before, after=None, landed=LANDED_APPLIED, landed_detail="stored")


def grant_dict(row: AdminGrant) -> dict[str, Any]:
    return {
        "id": row.id, "agent": row.agent_name, "integration": row.integration_id, "access": row.access,
        "state": row.state, "state_detail": row.state_detail, "spec": dict(row.spec or {}),
        "requested_by": row.requested_by, "decided_by": row.decided_by,
        "created_at": rfc3339(row.created_at), "updated_at": rfc3339(row.updated_at),
    }


def list_grants(session: Session, *, agent_name: str | None = None) -> list[AdminGrant]:
    q = session.query(AdminGrant)
    if agent_name is not None:
        q = q.filter_by(agent_name=agent_name)
    return q.order_by(AdminGrant.agent_name, AdminGrant.integration_id).all()


def put_grant(session: Session, *, agent_name: str, integration_id: str, access: str, actor: str) -> AdminGrant:
    """Give an agent a lane, never wider than the integration's base access.
    One row per (agent, integration); a second write changes the access."""
    integration = session.get(AdminIntegration, integration_id)
    if integration is None:
        raise AdminStoreError(f"no integration {integration_id!r}")
    if access not in ACCESS_LEVELS:
        raise AdminStoreError(f"access must be one of {', '.join(ACCESS_LEVELS)}")
    if access == "write" and integration.base_access != "write":
        raise AdminStoreError(f"{integration.name} is held read-only by the room; a write lane cannot be wider than that")
    now = _now()
    row = session.query(AdminGrant).filter_by(agent_name=agent_name, integration_id=integration_id).first()
    before = None if row is None else grant_dict(row)
    if row is None:
        row = AdminGrant(agent_name=agent_name, integration_id=integration_id, access=access, state=STATE_PENDING,
                         spec={}, spec_version=GRANT_SPEC_VERSION, requested_by=actor, decided_by=actor,
                         created_at=now, updated_at=now)
        session.add(row)
        action = "grant.create"
    else:
        row.access, row.state, row.state_detail, row.decided_by, row.updated_at = access, STATE_PENDING, None, actor, now
        action = "grant.update"
    session.flush()
    record(session, actor=actor, action=action, object_type="grant", object_id=f"{agent_name}/{integration_id}",
           before=before, after=grant_dict(row))
    return row


def set_grant_state(session: Session, grant_id: int, *, state: str, detail: str | None, spec: dict[str, Any] | None = None) -> None:
    row = session.get(AdminGrant, grant_id)
    if row is None:
        return
    row.state, row.state_detail, row.updated_at = state, detail, _now()
    if spec is not None:
        row.spec = {**dict(row.spec or {}), **spec}
    _land_latest(session, "grant", f"{row.agent_name}/{row.integration_id}", landed=state, detail=detail)


def revoke_grant(session: Session, *, agent_name: str, integration_id: str, actor: str) -> AdminGrant:
    row = session.query(AdminGrant).filter_by(agent_name=agent_name, integration_id=integration_id).first()
    if row is None:
        raise AdminStoreError(f"{agent_name} has no lane to {integration_id}")
    before = grant_dict(row)
    row.state, row.state_detail, row.decided_by, row.updated_at = STATE_REVOKED, None, actor, _now()
    record(session, actor=actor, action="grant.revoke", object_type="grant", object_id=f"{agent_name}/{integration_id}",
           before=before, after=grant_dict(row))
    return row

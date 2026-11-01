"""The room's own two credentials, read from their connections (kobo#377).

Slack (the app-configuration token pair the room creates agents' Slack apps
with) and GitHub (the App behind the room's pull requests and agent
repositories) used to be two "service" rows that were always there at the
top of Admin > Integrations, kept apart from the vendor connections
(`kobo_api.room_services`, #284/#304). Now each is one ordinary connection
row (`AdminIntegration`, vendor `slack` or `github`), added through the same
Add integration dialog as every vendor, and the room's own needs read their
credential from that connection. This module is the one place they read it
from, so a call site never knows where the bytes live.

WHERE THE BYTES LIVE. The connection row is the record; its credential is
the Secret `room_services` always wrote, one per vendor in the room's own
namespace (`kobo-room-slack`, `kobo-room-github-app`), named on the row as
`credential_ref = "secret:<namespace>/<name>"`. The secrets add-on cannot
hold them: it has no read-back lane, and the room needs these values back
(the pair to call Slack and rotate, the key to sign an App JWT). A Datadog
or GitHub connection through the permission service keeps its keys in a
Secret too, so this follows the same pattern, in the room's namespace
because the room is the reader. The Secret names and keys are unchanged
from the service rows, so a rollback to the release before this one reads
the same bytes.

THE DEPLOYMENT CONFIG stays the source of last resort for an older install
(`slack_tokens_*`, `pr_app_*`): with no row for the vendor, the listing
shows one marked "set in the deployment config", and Replace (an Add with
new values) moves it into the room, where the room's value wins. Exactly
the semantics the service rows had.

PRESENCE. "A Slack connection exists" and "the room holds the pair" are the
same fact: the connect writes the row and the Secret together, Remove takes
both, and the boot fold below makes an older room's Secret into a row. The
`slack_configured` question the agent pages ask is therefore answered from
the credential (a 5 s cached read), with no session in hand.

Values are never logged and never echoed.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from kobo_api import admin_store
from kobo_api import room_services as store
from kobo_api.clients import ClientError
from kobo_api.config import Config
from kobo_api.models import AdminIntegration
from kobo_api.room_services import (  # noqa: F401  (re-exported for call sites)
    SLACK_NOT_SET_UP,
    GitHubAppCredentials,
    RoomServiceRefused,
    RoomServices,
    ServiceStatus,
)

log = logging.getLogger(__name__)

SLACK_VENDOR = "slack"
GITHUB_VENDOR = "github"
ROOM_VENDORS = (SLACK_VENDOR, GITHUB_VENDOR)
VENDOR_NAMES = {SLACK_VENDOR: "Slack", GITHUB_VENDOR: "GitHub"}
# The connect form's auth kinds for the two (`admin_integrations.AUTH_KINDS`).
SLACK_AUTH = "slack_config_token"
GITHUB_AUTH = "github_app"
AUTH_OF_VENDOR = {SLACK_VENDOR: SLACK_AUTH, GITHUB_VENDOR: GITHUB_AUTH}
_SERVICE_OF = {SLACK_VENDOR: store.SLACK, GITHUB_VENDOR: store.GITHUB}
# `spec.source` on a listing row the deployment config stands behind (no row in the table).
SOURCE_DEPLOYMENT = store.SOURCE_DEPLOYMENT
FROM_DEPLOYMENT = "set in the deployment config; Replace to hold it in the room"
NOWHERE_TO_STORE = store.NOWHERE_TO_STORE
# The connect form's field names (secrets are upper-cased by `admin_integrations.connect`)
# against the Secret's keys.
SLACK_FIELDS = {"CONFIG_TOKEN": "config_token", "REFRESH_TOKEN": "refresh_token"}
GITHUB_KEY_FIELD = "PRIVATE_KEY"
# The permission service's own copy of the GitHub App (its minter reads it from env).
BAB_GITHUB_SECRET = "bab-github"


def services(clients: object | None) -> RoomServices | None:
    """The room's Secret store, or None when the room has no namespace to keep
    its own Secrets in (a local room, say)."""
    return getattr(clients, "room_services", None) if clients is not None else None


def credential_ref(svc: RoomServices, vendor: str) -> str:
    return f"secret:{svc.namespace}/{store.SECRET_NAMES[_SERVICE_OF[vendor]]}"


# --- reads ---------------------------------------------------------------------


def slack_client(clients: object | None):
    """The Slack client this room should call right now: one over the Slack
    connection's pair when the room holds one (so a rotation writes back
    there), else the deployment-config client, else None."""
    return store.slack_client(clients)


def slack_configured(clients: object | None) -> bool:
    """Whether a Slack connection exists for this room: the one question the
    connect routes' 503 and the agent pages' `available` flag ask."""
    return store.slack_configured(clients)


def github_app(clients: object | None, config: Config) -> GitHubAppCredentials | None:
    """The App the room's own pull requests mint with: the GitHub connection's
    first, then the deployment config's `pr_app_*`."""
    svc = services(clients)
    if svc is not None:
        return svc.github_app()
    return store.github_app_from_config(config)


def github_configured(clients: object | None, config: Config) -> bool:
    return github_app(clients, config) is not None


def status(clients: object | None, config: Config, vendor: str) -> ServiceStatus:
    """Held by the room, by the deployment config, or not at all; never a value."""
    svc = services(clients)
    service = _SERVICE_OF[vendor]
    return svc.status(service) if svc is not None else store.deployment_status(config, service)


def held_by_room(clients: object | None, vendor: str) -> bool:
    svc = services(clients)
    return svc is not None and svc.read(_SERVICE_OF[vendor]) is not None


# --- writes (the connect and disconnect flows call these) -----------------------


def values_from_form(vendor: str, identity: dict[str, str], secrets: dict[str, str]) -> dict[str, str]:
    """The Secret's keys from the connect form's fields, shape-checked the way
    the service rows checked them. Raises `RoomServiceRefused` with a sentence
    that names the field, never its value."""
    if vendor == SLACK_VENDOR:
        raw = {key: secrets.get(field, "") for field, key in SLACK_FIELDS.items()}
    else:
        raw = {
            "app_id": identity.get("app_id", ""),
            "installation_id": identity.get("installation_id", ""),
            "private_key": secrets.get(GITHUB_KEY_FIELD, ""),
        }
    return store.validate(_SERVICE_OF[vendor], raw)


def store_values(clients: object | None, vendor: str, values: dict[str, str], *, actor: str) -> ServiceStatus:
    """Hold validated `values` as the connection's credential. Raises
    `ClientError` when the room has nowhere to keep them or the write fails."""
    svc = services(clients)
    if svc is None:
        raise ClientError(NOWHERE_TO_STORE)
    return svc.put(_SERVICE_OF[vendor], values, actor=actor)


def remove_values(clients: object | None, vendor: str) -> None:
    svc = services(clients)
    if svc is None:
        return
    svc.delete(_SERVICE_OF[vendor])


def check_slack_writable(clients: object | None) -> None:
    """The boot probe's question, asked at connect time instead: could this
    room persist a rotation of the pair it just stored? Raises `ClientError`."""
    svc = services(clients)
    if svc is not None:
        svc.check_slack_writable()


# --- the listing --------------------------------------------------------------


def deployment_rows(known_ids: set[str], clients: object | None, config: Config) -> list[dict[str, Any]]:
    """The rows the deployment config stands behind: one per vendor with no row
    of its own whose values the config carries. Shown like any connection,
    marked from the deployment config, with Replace and no Remove."""
    rows: list[dict[str, Any]] = []
    for vendor in ROOM_VENDORS:
        if vendor in known_ids:
            continue
        current = status(clients, config, vendor)
        if not current.configured or current.source != SOURCE_DEPLOYMENT:
            continue
        rows.append({
            "id": vendor, "vendor": vendor, "name": VENDOR_NAMES[vendor], "base_access": "read",
            "state": admin_store.STATE_APPLIED, "state_detail": FROM_DEPLOYMENT, "credential_ref": None,
            "spec": {"auth": AUTH_OF_VENDOR[vendor], "source": SOURCE_DEPLOYMENT, "identity": {}, "secret_keys": []},
            "created_by": "", "created_at": "", "updated_at": "",
        })
    return rows


def annotate(rows: list[dict[str, Any]], clients: object | None, config: Config) -> None:
    """A Slack or GitHub row whose credential the room no longer holds (the
    Secret went by hand, say) says so in place, so the table never reads
    Connected over nothing. Reads are cached; this costs the listing nothing."""
    for row in rows:
        if row.get("vendor") not in ROOM_VENDORS or (row.get("spec") or {}).get("source") == SOURCE_DEPLOYMENT:
            continue
        if row.get("vendor") == GITHUB_VENDOR and (row.get("spec") or {}).get("identity", {}).get("source") not in (None, "room"):
            continue  # an app pasted for the permission service alone is not the room's to hold
        if status(clients, config, row["vendor"]).configured:
            continue
        row["state"] = admin_store.STATE_FAILED
        row["state_detail"] = "the room no longer holds the values; Replace to paste them again"


# --- the boot fold ---------------------------------------------------------------


def migrate_service_rows(session: Session, clients: object | None, config: Config) -> list[str]:
    """Once, idempotent: a room whose Secret still stands behind a service row
    (an install before kobo#377) gets a connection row made from it; a row
    that already exists adopts the Secret as its credential. The Secret is
    never removed here: it IS the connection's credential now, and a rollback
    reads it as before. Returns the sentences for one log line; empty when
    there was nothing to fold. Never raises (a boot never fails on this)."""
    svc = services(clients)
    if svc is None:
        return []
    done: list[str] = []
    for vendor in ROOM_VENDORS:
        try:
            held = svc.read(_SERVICE_OF[vendor], fresh=True)
        except ClientError:
            held = None
        if held is None:
            continue
        ref = credential_ref(svc, vendor)
        row = session.get(AdminIntegration, vendor)
        if row is None:
            actor = held.get("updated_by") or "the room"
            spec = {"auth": AUTH_OF_VENDOR[vendor], "address": "", "trust": "untrusted", "secret_keys": [],
                    "identity": {} if vendor == SLACK_VENDOR else
                    {"app_id": held["app_id"], "installation_id": held["installation_id"], "source": "room"}}
            if vendor == GITHUB_VENDOR:
                spec["room_only"] = True
            row = admin_store.put_integration(
                session, vendor=vendor, name=VENDOR_NAMES[vendor], base_access="read", spec=spec,
                credential_ref=ref, actor=actor, integration_id=vendor,
            )
            admin_store.set_integration_state(
                session, row.id, state=admin_store.STATE_APPLIED,
                detail="moved from the room services row; the room reads it from this connection",
            )
            done.append(f"{VENDOR_NAMES[vendor]} service row became the {vendor} connection")
        elif not row.credential_ref:
            # Both existed: the connection wins and the Secret becomes its credential. A
            # GitHub connection made with a PASTED app (not the service row's) holds a
            # different app in the permission service's Secret; that one is copied over
            # the room's so the room's own pull requests use the connection's app too.
            identity = dict((row.spec or {}).get("identity") or {})
            if vendor == GITHUB_VENDOR and identity.get("source") == "pasted":
                copied = _adopt_connection_app(session, row, svc, clients, config)
                done.append(copied)
            row.credential_ref = ref
            done.append(f"{vendor} connection adopted the room's {VENDOR_NAMES[vendor]} values")
    return done


def _adopt_connection_app(session: Session, row: AdminIntegration, svc: RoomServices, clients: object | None, config: Config) -> str:
    """The pasted app out of the permission service's Secret into the room's, so
    the connection's app wins; the room's old values stay when it cannot be read."""
    kubectl = getattr(clients, "kubectl", None) if clients is not None else None
    try:
        if kubectl is None:
            raise ClientError("no kubectl")
        literals = kubectl.secret_literals(config.bab_namespace, BAB_GITHUB_SECRET)
        values = store.validate(store.GITHUB, {
            "app_id": literals.get("GITHUB_APP_ID", ""), "installation_id": literals.get("GITHUB_APP_INSTALLATION_ID", ""),
            "private_key": literals.get("GITHUB_APP_PRIVATE_KEY", ""),
        })
        svc.put(store.GITHUB, values, actor=row.created_by or "the room")
    except (ClientError, RoomServiceRefused) as exc:
        return f"the github connection's pasted app could not be read back ({exc}); the room keeps the values it held"
    row.spec = {**dict(row.spec or {}), "identity": {**dict((row.spec or {}).get("identity") or {}), "source": "room"}}
    return "the github connection's pasted app now also serves the room's own pull requests"

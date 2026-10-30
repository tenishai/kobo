"""Room services (issue #284): the two credentials the room itself uses,
held by the room rather than by its deployment config.

- `slack`: the Slack app-configuration token pair the room creates and
  deletes Slack apps with (and rotates every ~12h).
- `github`: the GitHub App behind the pull request broker (app id,
  installation id, private key).

Both were deployment-config keys only (`slack_tokens_secret` /
`slack_tokens_path`, `pr_app_*`), so a room that forgot them told owners
"not configured" with no way to fix it from the room. Now an operator pastes
the values under Admin > Integrations > Room services; they are read live,
and the deployment-config keys stay as the fallback (`source:
"deployment"`).

WHERE THE VALUES LIVE. The Secrets add-on (`kobo_api.sero.SeroClient`) has
no read-back lane: it stores, syncs, lists keys and deletes, and never hands
a value back, by design. The room needs these values back (the Slack pair
to call Slack, the key to sign an App JWT), so they live in one Kubernetes
Secret per service in the room's own namespace, written and read through
the same `KubectlClient.apply_secret` / `secret_literals` calls the
`KubeSlackTokenStore` already uses for the deployment-config pair. Writing
them to the Secrets add-on as well would give the pair two writers (the
room rewrites it on every rotation), which the add-on's own invariant
forbids ("an agent whose Secret has two writers has two sources of truth").

Values are never logged and never echoed: `ServiceStatus` carries only
`configured`, `source`, `updated_at`, `updated_by`.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kobo_api.clients import ClientError, KubectlClient, SlackTokenStore
from kobo_api.config import Config

log = logging.getLogger(__name__)

SLACK = "slack"
GITHUB = "github"
SERVICES = (SLACK, GITHUB)

# One Secret per service, named by the room. An operator who meets them in
# the namespace can tell what they are without reading them.
SECRET_NAMES = {SLACK: "kobo-room-slack", GITHUB: "kobo-room-github-app"}
# The value keys each Secret holds. Slack's are the SAME two keys the
# deployment-config `KubeSlackTokenStore` uses, so the two stores read alike.
FIELDS: dict[str, tuple[str, ...]] = {
    SLACK: ("config_token", "refresh_token"),
    GITHUB: ("app_id", "installation_id", "private_key"),
}
# Two more keys beside the values: who set them up and when. Rotation
# rewrites the Slack values and leaves these alone.
_META_AT = "updated_at"
_META_BY = "updated_by"

SOURCE_ROOM = "room"
SOURCE_DEPLOYMENT = "deployment"
SOURCE_NONE = "none"

# How long a "does the room hold this" answer is reused before kubectl is
# asked again. Status and the Slack `available` flag are read on every
# agent detail; a rotation never reads through this cache (see
# `RoomSlackTokenStore.read`).
CACHE_TTL_S = 5.0

# Where a pod learns its own namespace when `room_namespace` is not set.
POD_NAMESPACE_FILE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")

NOWHERE_TO_STORE = (
    "This room has nowhere to keep these values yet. Set room_namespace in its "
    "deployment config, or run it where it can read its own namespace."
)
SLACK_NOT_SET_UP = "Slack is not set up for this room"


class RoomServiceRefused(ValueError):
    """A value whose shape is wrong, said in one sentence (never the value)."""


def room_namespace(config: Config, pod_file: Path = POD_NAMESPACE_FILE) -> str:
    """The namespace the room keeps its own Secrets in: the configured one,
    else the pod's own, else empty (nowhere to store)."""
    if config.room_namespace:
        return config.room_namespace
    try:
        return pod_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def validate(service: str, values: dict[str, Any]) -> dict[str, str]:
    """The fields `service` takes, stripped and shape-checked. Raises
    `RoomServiceRefused` with a sentence that names the field, never its
    value. Unknown fields are dropped."""
    if service not in SERVICES:
        raise RoomServiceRefused(f"no room service called {service!r}")
    out: dict[str, str] = {}
    for field in FIELDS[service]:
        raw = values.get(field)
        text = raw.strip() if isinstance(raw, str) else ""
        if not text:
            raise RoomServiceRefused(f"{field} is required")
        out[field] = text
    if service == SLACK:
        for field in FIELDS[SLACK]:
            if not out[field].startswith("xoxe"):
                raise RoomServiceRefused(
                    f"{field} does not look like a Slack app configuration token (they start with xoxe)"
                )
    else:
        for field in ("app_id", "installation_id"):
            if not out[field].isdigit():
                raise RoomServiceRefused(f"{field} must be a number")
        if not out["private_key"].startswith("-----BEGIN"):
            raise RoomServiceRefused("private_key must be the PEM text, starting with -----BEGIN")
    return out


@dataclass(frozen=True)
class ServiceStatus:
    """What the panel shows about one service: never a value."""

    configured: bool
    source: str
    updated_at: str | None = None
    updated_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GitHubAppCredentials:
    """What the pull request broker mints with, from whichever source holds
    it. `pem()` reads the deployment key file at mint time so a rotated
    file is picked up, and so a missing file is a `ClientError` the route
    turns into a 502 rather than a crash."""

    app_id: str
    installation_id: str
    source: str
    private_key: str = ""
    private_key_path: Path | None = None

    def pem(self) -> str:
        if self.private_key:
            return self.private_key
        try:
            return (self.private_key_path or Path()).read_text()
        except OSError:
            raise ClientError("the pull request app's private key file could not be read") from None


def deployment_status(config: Config, service: str) -> ServiceStatus:
    """The status when the room holds nothing itself: what the deployment
    config says, or none."""
    if service == SLACK:
        configured = bool(config.slack_tokens_secret or config.slack_tokens_path)
    else:
        configured = bool(
            config.pr_app_id and config.pr_app_installation_id and config.pr_app_private_key_path
        )
    return ServiceStatus(configured=configured, source=SOURCE_DEPLOYMENT if configured else SOURCE_NONE)


def github_app_from_config(config: Config) -> GitHubAppCredentials | None:
    if not deployment_status(config, GITHUB).configured:
        return None
    return GitHubAppCredentials(
        app_id=config.pr_app_id,
        installation_id=config.pr_app_installation_id,
        source=SOURCE_DEPLOYMENT,
        private_key_path=config.pr_app_private_key_path,
    )


class RoomServices:
    """The store: one Secret per service in the room's namespace, with a
    short in-process cache for the "is it there" question. Every value read
    stays inside this class or goes to the one caller that needs it (the
    Slack client, the broker mint); nothing here logs or raises one."""

    def __init__(
        self,
        kubectl: KubectlClient,
        namespace: str,
        config: Config,
        *,
        clock=time.monotonic,
    ):
        self._kubectl = kubectl
        self._namespace = namespace
        self._config = config
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict[str, str] | None]] = {}

    @property
    def namespace(self) -> str:
        return self._namespace

    # --- reads -------------------------------------------------------------

    def read(self, service: str, *, fresh: bool = False) -> dict[str, str] | None:
        """The room-held values plus meta for `service`, or None when the
        room holds none (absent Secret, or one missing a field). `fresh`
        bypasses the cache: a rotation must see what is there now."""
        if not fresh:
            with self._lock:
                hit = self._cache.get(service)
            if hit is not None and self._clock() - hit[0] < CACHE_TTL_S:
                return dict(hit[1]) if hit[1] is not None else None
        values = self._read_live(service)
        with self._lock:
            self._cache[service] = (self._clock(), dict(values) if values is not None else None)
        return dict(values) if values is not None else None

    def _read_live(self, service: str) -> dict[str, str] | None:
        try:
            literals = self._kubectl.secret_literals(self._namespace, SECRET_NAMES[service])
        except ClientError:
            # Absent and unreadable fold together in kubectl's answer; both
            # mean the room cannot use what is there, so neither is "room".
            return None
        if not all(literals.get(field) for field in FIELDS[service]):
            return None
        return dict(literals)

    def status(self, service: str) -> ServiceStatus:
        held = self.read(service)
        if held is not None:
            return ServiceStatus(
                configured=True,
                source=SOURCE_ROOM,
                updated_at=held.get(_META_AT) or None,
                updated_by=held.get(_META_BY) or None,
            )
        return deployment_status(self._config, service)

    def github_app(self) -> GitHubAppCredentials | None:
        """The broker's credentials: the room's first, then the deployment's."""
        held = self.read(GITHUB)
        if held is not None:
            return GitHubAppCredentials(
                app_id=held["app_id"],
                installation_id=held["installation_id"],
                private_key=held["private_key"],
                source=SOURCE_ROOM,
            )
        return github_app_from_config(self._config)

    # --- writes ------------------------------------------------------------

    def put(self, service: str, values: dict[str, str], *, actor: str) -> ServiceStatus:
        """Store validated `values` for `service` as this room's own, with
        who and when. Raises `ClientError` when the write fails (its message
        names the Secret, never a value)."""
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        literals = {field: values[field] for field in FIELDS[service]}
        literals[_META_AT] = now
        literals[_META_BY] = actor
        self._kubectl.apply_secret(self._namespace, SECRET_NAMES[service], literals)
        self.invalidate(service)
        return ServiceStatus(configured=True, source=SOURCE_ROOM, updated_at=now, updated_by=actor)

    def rotate_slack(self, config_token: str, refresh_token: str) -> None:
        """The Slack client's write-back after a rotation: new pair, same
        who-and-when (a rotation is the room's doing, not a person's)."""
        held = self.read(SLACK, fresh=True) or {}
        literals = {
            "config_token": config_token,
            "refresh_token": refresh_token,
            _META_AT: held.get(_META_AT, ""),
            _META_BY: held.get(_META_BY, ""),
        }
        self._kubectl.apply_secret(self._namespace, SECRET_NAMES[SLACK], literals)
        self.invalidate(SLACK)

    def check_slack_writable(self) -> None:
        """The boot probe's question for the room-held pair: could this
        process persist a rotation? A server dry-run of what is there,
        persisting nothing (the `KubeSlackTokenStore.check_writable`
        reasoning: a real write-back could overwrite another replica's
        fresher pair)."""
        held = self.read(SLACK, fresh=True)
        if held is None:
            return
        try:
            self._kubectl.apply_secret(self._namespace, SECRET_NAMES[SLACK], held, dry_run=True)
        except ClientError as exc:
            raise ClientError(
                f"room secret {self._namespace}/{SECRET_NAMES[SLACK]} is not writable by this room: {exc}"
            ) from None

    def delete(self, service: str) -> None:
        self._kubectl.delete_secret(self._namespace, SECRET_NAMES[service])
        self.invalidate(service)

    def invalidate(self, service: str | None = None) -> None:
        with self._lock:
            if service is None:
                self._cache.clear()
            else:
                self._cache.pop(service, None)


def build_room_services(config: Config, kubectl: KubectlClient) -> RoomServices | None:
    """The store `Clients.real` wires, or None when the room has no
    namespace to keep its Secrets in (a local room, say). None is what the
    admin routes turn into a 409 and the panel into one line."""
    namespace = room_namespace(config)
    if not namespace:
        return None
    return RoomServices(kubectl, namespace, config)


# --- the Slack token store over this ------------------------------------------


class RoomSlackTokenStore:
    """`SlackTokenStore` over the room-held pair: the third store beside the
    file and the deployment Secret. Reads are live (never through the
    cache), because `HttpxSlackClient` re-reads around a single-use refresh
    token and treats a changed pair as "another process rotated"."""

    def __init__(self, services: RoomServices):
        self._services = services

    def configured(self) -> bool:
        return self._services.read(SLACK) is not None

    def read(self) -> tuple[str, str]:
        held = self._services.read(SLACK, fresh=True)
        if held is None:
            raise ClientError(SLACK_NOT_SET_UP)
        return held["config_token"], held["refresh_token"]

    def write(self, config_token: str, refresh_token: str) -> None:
        self._services.rotate_slack(config_token, refresh_token)

    def check_writable(self) -> None:
        self._services.check_slack_writable()


class LayeredSlackTokenStore:
    """The room-held pair when there is one, else the deployment-config
    store (file or Secret), else nothing. Decided per call, so pasting the
    pair under Room services makes Slack available with no restart, and a
    rotation writes back to the layer it read from."""

    def __init__(self, room: RoomSlackTokenStore, deployment: SlackTokenStore | None):
        self._room = room
        self._deployment = deployment

    def _active(self, *, fresh: bool) -> SlackTokenStore | None:
        held = self._room._services.read(SLACK, fresh=fresh)
        if held is not None:
            return self._room
        return self._deployment

    def configured(self) -> bool:
        return self._active(fresh=False) is not None

    def read(self) -> tuple[str, str]:
        store = self._active(fresh=True)
        if store is None:
            raise ClientError(SLACK_NOT_SET_UP)
        return store.read()

    def write(self, config_token: str, refresh_token: str) -> None:
        store = self._active(fresh=True)
        if store is None:
            raise ClientError(SLACK_NOT_SET_UP)
        store.write(config_token, refresh_token)

    def check_writable(self) -> None:
        store = self._active(fresh=True)
        if store is not None:
            store.check_writable()


def slack_configured(clients: object | None) -> bool:
    """Whether this room can call Slack right now: the one question the
    connect route's 503 and the agent detail's `available` flag ask. A
    `Clients.slack` of None is still "off"; a client whose store can say
    (`configured()`) is asked; any other client (the test fake) is on."""
    slack = getattr(clients, "slack", None) if clients is not None else None
    if slack is None:
        return False
    store = getattr(slack, "token_store", None)
    configured = getattr(store, "configured", None)
    if callable(configured):
        return bool(configured())
    return True

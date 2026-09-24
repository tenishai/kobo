"""The secrets platform's machine lane: the one way this room writes an
agent's secrets without holding cluster write access to the agent's own
namespace.

The room delivers exactly two per-agent secrets today -- the colleague pass
(`kobo_api.colleagues.deliver_pass`) and the pasted Slack tokens
(`kobo_api.slack_setup.wire_slack`) -- and both have always been written
straight into the agent's namespace as a Kubernetes Secret
(`KubectlClient.apply_secret`). The secrets platform runs a MACHINE LANE for
services that write on a person's behalf: the room hands it the values plus
the email of the human whose action produced them, it stores them once under
a path of its own and then syncs them into the agent's namespace as the SAME
Secret name the deploy contract already names. The pod is untouched: same
Secret, same keys, same `secrets_from` entry in `gojira.yaml`.

Two halves live here:

- `LadonClient` (the Protocol every caller depends on) and `HttpxLadonClient`
  (its one real implementation), with `FakeLadon` in tests/fakes.py standing
  in for it the way every other client in this room is faked.
- `MetadataIdentityTokenProvider`, the lane's credential: a Google OIDC ID
  token for THIS workload's own identity, minted by the metadata server for
  the audience the platform expects. Nothing is configured with it and
  nothing is stored -- the identity is the pod's.

Two rules this module exists to keep, both about blast radius rather than
convenience:

- A secret VALUE never leaves this module. It is placed in one request body
  and never logged, never echoed into an exception, never read back out of a
  response. Failures name the path and the platform's own sentence; the
  request body is never part of an error.
- There is NO fallback to the direct Secret write when the lane fails. A
  half-migrated agent would have two sources of truth for the same Secret
  name, and the next sync would silently overwrite whichever one a human
  had just fixed. A lane failure fails the step, and the step is retried.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx

from kobo_api.clients import ClientError

log = logging.getLogger(__name__)

# The GKE/GCE metadata server's IDENTITY endpoint -- a Google-signed OIDC ID
# token for this workload's own service account, for the audience the
# secrets platform pins its callers to. Distinct from the token endpoint
# `kobo_api.clients._METADATA_TOKEN_URL` reads (that one mints ACCESS tokens
# for Google APIs, which this lane does not accept). `format=full` includes
# the workload's identity details in the token's claims, which is what lets
# the platform allow-list this room by subject rather than by shared secret.
_METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/identity"
)

# Re-mint this far ahead of the ID token's own `exp`. Small (the token is
# good for an hour) but not zero: a token that expires in flight would fail
# a secret delivery for no reason other than clock margin.
_TOKEN_EXPIRY_MARGIN_S = 60

# And never hold a token longer than this after it was issued, whatever its
# `exp` says. The secrets platform refuses a machine token issued more than
# fifteen minutes ago (its own ceiling on how long a minted credential stays
# usable, independent of the issuer's generous hour), so a cache keyed on
# `exp` alone handed the platform a token it would not take (live,
# 2026-09-24: a Retry twenty minutes after the first answered "no service
# identity"). Ten minutes leaves clock skew and an in-flight request well
# inside that ceiling.
_TOKEN_MAX_AGE_S = 600

# How long the metadata server is given to answer. It is link-local; if it
# is slow, something is wrong with the node rather than with the network,
# and a secret delivery should say so quickly instead of hanging a step.
_METADATA_TIMEOUT_S = 5.0

# The lane's own request timeout. Same shape as the deploy platform client's
# (`kobo_api.clients.HttpxGojiraClient`): a write reaches a vault and, for a
# sync, the cluster, so 30s is headroom over httpx's bare 5s rather than
# patience for something hung.
_REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# How often the cluster re-reads a delivered secret. See `ensure_synced` for
# why the platform's own hour-long default is wrong for this room: every
# delivery here can be a ROTATION, and a rotation is only really done once
# the pod can no longer read the value a human just replaced.
_REFRESH_INTERVAL = "1m"


@dataclass(frozen=True)
class SecretKeys:
    """What one application path holds, by NAME. Never a value, masked or
    otherwise -- the lane's read route does not return one and this room
    does not ask for one (see `LadonClient.list_keys`).

    `exists` is false for a path with nothing at it, which is the ordinary
    state of an agent whose owner has never added a secret. `updated_at` is
    the path's own last-write timestamp, one per PATH rather than one per
    key: KV versions the whole document, so there is no per-key date to
    report and this room does not invent one.
    """

    keys: list[str]
    updated_at: str | None
    exists: bool


class LadonClient(Protocol):
    """What the room needs from the secrets platform, and nothing more.

    Five calls, not a general-purpose secrets API. The room writes a secret
    it just produced, makes sure the agent's namespace carries it, asks
    which keys a path holds BY NAME, and -- when an agent is destroyed --
    takes the cluster object and then the path itself away. It never reads
    a secret VALUE back: there is no code path in this room that needs a
    value it did not just mint or just receive from a human, and the lane's
    own read route would not answer with one anyway.
    """

    def put_secret(
        self,
        acting_email: str,
        path: str,
        values: dict[str, str],
        remove: list[str] | None = None,
    ) -> None:
        """Store `values` at `path` (`apps/<agent>/<purpose>`) on the human
        `acting_email`'s behalf. Existing keys at that path that are not
        named here are left alone.

        `remove` names keys to take away in the same call, which only the
        Secrets panel ever passes: the room's own two deliveries (the
        colleague pass, the Slack tokens) add and replace and never remove.
        A key named on both sides is refused by the platform, which is why
        `kobo_api.agent_env.write_refusal` catches that first and answers
        the person instead.

        Raises `ClientError` carrying the platform's own error sentence.
        The values are never logged and never reach a raised message.
        """
        ...

    def ensure_synced(
        self, acting_email: str, app: str, path: str, k8s_namespace: str, name: str
    ) -> None:
        """Make the cluster carry `path`'s values as the Secret `name` in
        `k8s_namespace` -- the same Secret name and namespace the room used
        to write itself, so the deploy contract and the running pod are
        untouched.

        Idempotent by the platform's own contract: a second call for an
        already-current target reports exactly that and changes nothing.
        Raises `ClientError` the same way `put_secret` does.
        """
        ...

    def list_keys(self, acting_email: str, path: str) -> SecretKeys:
        """Which keys `path` holds, by name, on `acting_email`'s behalf.

        The one read this room makes, and the lane's read route is narrower
        than the human one beside it: names, a version and a timestamp, and
        no value even masked. A path with nothing at it is an EMPTY answer
        (`exists` false), not a failure -- an agent whose owner has never
        added a secret is the ordinary case.

        Raises `ClientError` for every other refusal, so "I could not look"
        is never mistaken for "there is nothing there".
        """
        ...

    def delete_secret(self, acting_email: str, path: str) -> None:
        """Remove `path` and every version of it, on `acting_email`'s
        behalf. The destroy half: an agent that goes away otherwise leaves
        its stored values behind, still readable and still valid.

        Idempotent by the lane's own contract -- deleting a path that is
        already gone is a success, so a retried destroy does not fail on
        work a prior attempt finished. Raises `ClientError` on a refusal.
        """
        ...

    def unsync(self, acting_email: str, app: str, k8s_namespace: str, name: str) -> None:
        """Remove the ExternalSecret `name` in `k8s_namespace`, and with it
        the Kubernetes Secret it owns.

        Run BEFORE `delete_secret` for the same path: an object left behind
        over a deleted path is one the cluster keeps reconciling and
        failing. Idempotent the same way -- an object already gone is a
        success.

        A Kubernetes namespace that no longer exists is one of those
        already-gone answers, not an error: the lane reads the object,
        finds nothing (a namespace that is gone holds no ExternalSecret),
        and reports `state: "absent"` with a 200. Nothing in the room
        should depend on that, because the destroy ledger removes secrets
        BEFORE it deletes the namespace -- but a resumed run, or a
        namespace an operator removed by hand, must not wedge a teardown,
        and it does not.

        Raises `ClientError` on a refusal, and also when the lane reports
        that a DIFFERENT object now stands at the name (`recreated`): what
        this call removed is gone, and what is there now is not it.
        """
        ...

    def offboard(self, acting_email: str, app: str, k8s_namespace: str) -> None:
        """Remove `app`'s identity on the platform: the store its
        ExternalSecrets read through, the service account that logs in
        for them, and the role and policy behind that login -- the four
        objects the platform made when this room's first sync for the
        agent onboarded it.

        Run LAST, after every `unsync` and `delete_secret` for the agent:
        the platform refuses to take an identity down while an
        ExternalSecret still reads through it, which is the right refusal
        (the alternative is a dangling object made on purpose) and the
        reason the order matters. Idempotent by the lane's contract: an
        identity already gone is a success. Raises `ClientError` on a
        refusal, and when the lane reports a cluster object it could not
        remove.
        """
        ...


def _jwt_times(token: str) -> tuple[int | None, int | None]:
    """The `exp` and `iat` claims of a JWT, each None when this room cannot
    read it.

    Decodes ONLY -- never verifies. The token is not this room's to verify
    (the secrets platform does that, against Google's keys); all that is
    wanted here is how long it is worth caching. An unreadable token is not
    an error at this layer either: it is simply not cached, and the platform
    is left to reject it if it is genuinely malformed.
    """
    try:
        payload_b64 = token.split(".")[1]
        # JWT base64url segments carry no padding; urlsafe_b64decode wants it.
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError, TypeError, binascii.Error):
        return None, None
    if not isinstance(claims, dict):
        return None, None

    def _int(key: str) -> int | None:
        try:
            return int(claims[key])
        except (KeyError, ValueError, TypeError):
            return None

    return _int("exp"), _int("iat")


class MetadataIdentityTokenProvider:
    """A cached Google OIDC ID token for this workload's own identity,
    minted for `audience` by the metadata server.

    Thread-safe and cached until `_TOKEN_EXPIRY_MARGIN_S` before the token's
    own `exp` or `_TOKEN_MAX_AGE_S` after its `iat` (its mint time when the
    token names none), whichever comes first -- the platform's own ceiling
    on a token's age is the shorter of the two by far. A token whose `exp`
    this room cannot read is used once and never cached, rather than pinned
    for a guessed window. The same
    lock-held-across-the-fetch discipline `kobo_api.clients.CachedGcloudToken`
    documents, for the same reason: concurrent provisions must cost one
    fetch, not one each.

    There is no local mode. On a developer machine the metadata server does
    not answer, so this raises -- which is correct and never reached: the
    lane's three settings are deployment-local, so a local room never has
    `Config.ladon_enabled` true and never builds a client that holds one of
    these. `time.time` (not `monotonic`) is deliberate: the window is
    compared against the token's own absolute `exp`.
    """

    def __init__(
        self,
        audience: str,
        *,
        client: httpx.Client | None = None,
        now: Callable[[], float] = time.time,
    ):
        self._audience = audience
        self._client = client
        self._now = now
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float | None = None

    def __call__(self) -> str:
        with self._lock:
            if (
                self._token is not None
                and self._expires_at is not None
                and self._now() < self._expires_at
            ):
                return self._token
            minted_at = self._now()
            token = self._fetch()
            exp, iat = _jwt_times(token)
            self._token = token
            if exp is None:
                self._expires_at = None
            else:
                issued = float(iat) if iat is not None else minted_at
                self._expires_at = min(exp - _TOKEN_EXPIRY_MARGIN_S, issued + _TOKEN_MAX_AGE_S)
            return token

    def _fetch(self) -> str:
        getter = self._client.get if self._client is not None else httpx.get
        try:
            resp = getter(
                _METADATA_IDENTITY_URL,
                params={"audience": self._audience, "format": "full"},
                headers={"Metadata-Flavor": "Google"},
                timeout=_METADATA_TIMEOUT_S,
            )
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            # The same three as the request path below (see `_post`): a
            # failure to mint is a failed delivery, never an unhandled
            # exception on a route.
            raise ClientError(
                f"this room could not reach its own identity service: {exc}"
            ) from None
        if resp.status_code != 200:
            # The body of a failed identity request is a diagnostic, not a
            # credential -- but it is not this room's to repeat either, and
            # the status is what an operator acts on.
            raise ClientError(
                "this room could not mint an identity token for the secrets "
                f"platform ({resp.status_code})"
            )
        token = resp.text.strip()
        if not token:
            raise ClientError(
                "this room's identity service returned an empty identity token"
            )
        return token


class HttpxLadonClient:
    """`LadonClient` over httpx, authenticated as this workload's own
    identity (see `MetadataIdentityTokenProvider`, its default credential).

    `namespace` is the room's own team namespace on the platform -- fixed
    per deployment, never taken from a request, so no caller can aim a write
    at another team's secrets by choosing a path.
    """

    def __init__(
        self,
        url: str,
        audience: str,
        namespace: str,
        token_provider: Callable[[], str] | None = None,
        http_client: httpx.Client | None = None,
    ):
        self._base_url = url.rstrip("/")
        self._namespace = namespace
        self._token_provider = token_provider or MetadataIdentityTokenProvider(audience)
        self._client = http_client or httpx.Client(timeout=_REQUEST_TIMEOUT)

    def put_secret(
        self,
        acting_email: str,
        path: str,
        values: dict[str, str],
        remove: list[str] | None = None,
    ) -> None:
        self._post(
            "/api/service/secrets",
            {
                "acting_email": acting_email,
                "namespace": self._namespace,
                "path": path,
                "values": dict(values),
                # Empty for the room's own two deliveries, which only ever
                # add keys. Sent explicitly rather than omitted either way,
                # so the merge the platform performs is stated by the
                # request rather than inferred from what it is missing.
                "remove_keys": list(remove or []),
                "apply": True,
            },
            f"could not store the secret at {path}",
        )

    def ensure_synced(
        self, acting_email: str, app: str, path: str, k8s_namespace: str, name: str
    ) -> None:
        # One sentence for both ways this call fails (a non-2xx and a 2xx
        # that reports a blocked target), so the two can never drift into
        # describing the same failed delivery differently.
        what = f"could not deliver the secret at {path} to the cluster"
        report = self._post(
            "/api/service/sync",
            {
                "acting_email": acting_email,
                "namespace": self._namespace,
                "app": app,
                "path": path,
                "k8s_namespace": k8s_namespace,
                # Both halves are the Secret name the room has always
                # written: the platform names its own object after the
                # first and the delivered Secret after the second, and the
                # deploy contract already names that string.
                "name": name,
                "target_name": name,
                # How often the cluster re-reads the stored value. The
                # room's deliveries ROTATE: a re-wire stores new tokens
                # but leaves the cluster object identical, so the
                # delivered Secret only catches up on the next re-read --
                # and the platform's own default is an hour, which is an
                # hour of a restarted pod holding tokens a human has
                # already revoked. Minutes is the honest window for a
                # credential someone just replaced by hand.
                "refresh_interval": _REFRESH_INTERVAL,
                "apply": True,
            },
            what,
        )
        blocked = _blocked_targets(report)
        if blocked:
            # A 200 over an undelivered secret: the lane reports what it
            # could not apply (a field-manager conflict on an object
            # someone else owns) rather than failing the whole request.
            # Reading the status alone would mark this delivered.
            raise ClientError(f"{what}: the cluster did not accept {', '.join(blocked)}")

    def list_keys(self, acting_email: str, path: str) -> SecretKeys:
        resp = self._send(
            "GET",
            "/api/service/secrets",
            what=f"could not read which keys are stored at {path}",
            params={
                "acting_email": acting_email,
                "namespace": self._namespace,
                "path": path,
            },
            # A path with nothing at it is the ordinary state of an agent
            # nobody has added a secret to yet, so the lane's 404 is an
            # empty answer here rather than a failure. Every OTHER refusal
            # still raises: "I could not look" and "there is nothing there"
            # are different answers, and a caller acting on the wrong one
            # would report a live secret as gone.
            absent_status=404,
        )
        if resp is None:
            return SecretKeys(keys=[], updated_at=None, exists=False)
        payload = _decoded(resp)
        keys = payload.get("keys") if isinstance(payload, dict) else None
        updated_at = payload.get("updated_at") if isinstance(payload, dict) else None
        return SecretKeys(
            # Sorted here rather than trusted from the wire, so two reads of
            # the same path render the same list whatever order arrived.
            # Only entries that are already strings: by the lane's contract
            # these are key NAMES, and anything else is not this room's to
            # repeat (the same discipline `_blocked_targets` holds).
            keys=sorted(k for k in (keys or []) if isinstance(k, str)),
            updated_at=updated_at if isinstance(updated_at, str) else None,
            exists=True,
        )

    def delete_secret(self, acting_email: str, path: str) -> None:
        self._send(
            "DELETE",
            "/api/service/secrets",
            what=f"could not remove the stored secrets at {path}",
            json={
                "acting_email": acting_email,
                "namespace": self._namespace,
                "path": path,
                "apply": True,
            },
        )

    def unsync(self, acting_email: str, app: str, k8s_namespace: str, name: str) -> None:
        what = f"could not remove the delivered secret {name}"
        resp = self._send(
            "DELETE",
            "/api/service/sync",
            what=what,
            json={
                "acting_email": acting_email,
                "namespace": self._namespace,
                "app": app,
                "k8s_namespace": k8s_namespace,
                # The body names the object and nothing else: no path, no
                # store, no refresh interval. A removal should not depend on
                # values that may have moved since the object was created.
                "name": name,
                "apply": True,
            },
        )
        payload = _decoded(resp)
        if isinstance(payload, dict) and payload.get("recreated") is True:
            # A 200 over a name that is live again: what this call deleted
            # is gone, and what stands there now is not it. Reading
            # `deleted` and stopping would leave an ExternalSecret behind
            # that nothing in this room knows about.
            raise ClientError(f"{what}: something else now stands at {name}")

    def offboard(self, acting_email: str, app: str, k8s_namespace: str) -> None:
        what = f"could not remove the secrets platform identity of {app}"
        report = self._send(
            "DELETE",
            "/api/service/app",
            what=what,
            json={
                "acting_email": acting_email,
                "namespace": self._namespace,
                "app": app,
                "k8s_namespace": k8s_namespace,
                "apply": True,
            },
        )
        blocked = _blocked_targets(_decoded(report))
        if blocked:
            # The same 200-over-an-undelivered-answer the sync can give: a
            # cluster object the platform could not remove is named in the
            # report, and reading the status alone would call it gone.
            raise ClientError(f"{what}: the cluster did not release {', '.join(blocked)}")

    def _post(self, route: str, body: dict, what: str) -> object | None:
        """POST `body` and hand back the decoded 2xx body (None when it is
        not JSON) -- the two write calls above read a report off it. See
        `_send` for everything else this shares with the other three."""
        return _decoded(self._send("POST", route, what=what, json=body))

    def _send(
        self,
        method: str,
        route: str,
        *,
        what: str,
        json: dict | None = None,
        params: dict | None = None,
        absent_status: int | None = None,
    ) -> httpx.Response | None:
        """One call on the lane: raise `ClientError` on anything but a 2xx
        (and on `absent_status`, hand back None instead), else the response.

        `json` may carry secret values, so it is never logged and never part
        of a raised message -- which is also why a transport failure is
        re-raised unchained (`from None`): a chained cause prints the
        `httpx.Request` it belongs to in any traceback. `params` never
        carries one: the lane has no route that takes a value in a query
        string, and this room would not send one there if it did.

        A caller may read NAMES and verbs off the answer (see
        `_blocked_targets`, `list_keys`) and nothing else: the lane never
        returns a secret value, and this room never asks it for one.
        """
        try:
            resp = self._client.request(
                method,
                f"{self._base_url}{route}",
                json=json,
                params=params,
                headers={"Authorization": f"Bearer {self._token_provider()}"},
            )
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            # InvalidURL is NOT an HTTPError, and an address with no host at
            # all (`http://`) never reaches httpx's own URL validation --
            # it surfaces as a plain ValueError from the proxy lookup. A
            # misconfigured `ladon_url` is a failed delivery either way;
            # uncaught, it would surface as a 500 on whichever route
            # happened to trigger the write.
            raise ClientError(f"{what}: the secrets platform did not answer ({exc})") from None

        if resp.status_code // 100 == 2:
            return resp
        if absent_status is not None and resp.status_code == absent_status:
            return None
        raise ClientError(f"{what}: {_server_sentence(resp)}")


def _decoded(resp: httpx.Response | None) -> object | None:
    """A lane answer's JSON body, or None when there isn't one.

    Separate from `_send` so a response this room never reads costs nothing
    to decode, and so one place holds the rule that a body which is not
    JSON is simply absent rather than an error: the lane's contract is the
    status code plus the fields named in it, and nothing here guesses.
    """
    if resp is None:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _blocked_targets(report: object) -> list[str]:
    """The cluster objects a sync report says it could NOT apply, as plain
    strings -- empty when there are none (the ordinary case).

    Only entries that are already strings are quoted: by the lane's
    contract those are target names and verbs, never values. An entry
    shaped like anything else is described rather than repeated, since this
    room cannot know what it carries -- but it still counts as blocked, so
    an unreadable report can never read as a clean delivery.
    """
    if not isinstance(report, dict):
        return []
    blocked = report.get("blocked")
    if not isinstance(blocked, list):
        return []
    return [
        entry if isinstance(entry, str) else "an object it did not name"
        for entry in blocked
    ]


def _server_sentence(resp: httpx.Response) -> str:
    """The platform's own `error` string, and ONLY that.

    Never `resp.text`: a response this room did not expect could be echoing
    the request it was sent, and that request carries secret values. When
    the body has no `error` string to quote, the status is described in
    plain words instead -- enough for an operator to tell a refusal from a
    rate limit from an outage, with nothing quoted back.
    """
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip()
    if resp.status_code == 429:
        return "the secrets platform is rate limiting this room right now"
    if resp.status_code == 503:
        return "the secrets platform is unavailable right now"
    return f"the secrets platform refused the request ({resp.status_code})"


def deliver_secret(
    ladon: LadonClient,
    *,
    acting_email: str | None,
    agent_name: str,
    purpose: str,
    values: dict[str, str],
    secret_name: str,
) -> None:
    """Store `values` for `agent_name` and make its namespace carry them as
    the Secret `secret_name` -- the lane's replacement for one
    `KubectlClient.apply_secret` call, shared by both delivery sites
    (`kobo_api.colleagues.deliver_pass`, `kobo_api.slack_setup.wire_slack`)
    so the path shape and the ordering are stated once.

    `purpose` is the last segment of the path (`apps/<agent>/<purpose>`):
    `cabal` for the colleague pass, `slack` for the Slack tokens. The
    agent's namespace is its own name, which is what the room has always
    written into.

    `acting_email` is the human whose action produced these values -- the
    person the platform records the write against. Missing it is a refusal,
    not a default (`acting_email_refusal` below is the same check a caller
    can make first, to answer a person rather than fail a step): a write
    nobody is accountable for is exactly what this lane exists to prevent,
    and the room would rather refuse than store a secret under no one's
    name.

    Delivery here is EVENTUALLY consistent, unlike the direct write it
    replaces: the value is stored immediately, and the agent's namespace
    catches up when the cluster next re-reads it (`_REFRESH_INTERVAL`). A
    pod restarted in that window can still be holding the previous value.
    """
    refusal = acting_email_refusal(agent_name, acting_email)
    if refusal is not None:
        raise ClientError(refusal)
    email = acting_email.strip()
    path = f"apps/{agent_name}/{purpose}"
    ladon.put_secret(email, path, values)
    # Only after the value is stored: a sync for a path with nothing at it
    # would deliver an empty Secret over a good one.
    ladon.ensure_synced(email, agent_name, path, agent_name, secret_name)
    # One line per delivery, and only once it actually happened: which lane
    # wrote an agent's secret is the first thing an operator asks when a pod
    # comes up without one. Names only -- never a value, and not the
    # person's email either (the platform's own audit trail holds that, and
    # this line would otherwise put it in every pod log that scrapes ours).
    log.info(
        "delivered the %s secret for %s through the secrets platform",
        purpose,
        agent_name,
    )


def acting_email_refusal(agent_name: str, acting_email: str | None) -> str | None:
    """The plain refusal sentence when `acting_email` names nobody, else
    None -- `deliver_secret`'s own precondition, exposed so a route can
    make the same check BEFORE it starts work and answer the person a
    refusal rather than a retryable failure (there is nothing to retry: no
    verified caller and no creator on record is a standing state, not a
    blip). Names the agent and nobody and nothing else."""
    if (acting_email or "").strip():
        return None
    return (
        f"{agent_name} has no person on record to write its secrets for, "
        "so the secrets platform cannot be asked to store them"
    )

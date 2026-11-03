"""Slack events over HTTPS, relayed by the room (issue #371, phase 2 of #304).

Two public routes, served off the same no-identity-proxy backend as the
room's OAuth server and MCP surface (`kobo_api.iap.EXEMPT_PREFIXES`):

- `POST /slack/relay/{relay_id}`: Slack posts an event envelope for one
  agent's app. The relay id is the room's own (minted before the app
  exists, `kobo_api.models.SlackRelayApp`), never the Slack app id, so
  nothing public names the app. The route answers Slack's `url_verification`
  challenge, verifies `X-Slack-Signature` with that app's signing secret
  inside the five-minute `X-Slack-Request-Timestamp` window, drops a
  repeated `event_id`, acks `200` at once (Slack's three-second rule) and
  forwards the envelope to the agent in a background task with
  `X-Kobo-Relay-Secret`. Unknown id and bad signature are a bare 404 and
  401. A token bucket per relay id keeps one app from flooding the room.
  Log lines carry the agent's name and an outcome word, never a body.
- `GET /slack/oauth/callback`: where Slack sends the person after Install
  to Slack. `state` is the single-use nonce the room minted for that agent
  (`<relay_id>.<nonce>`, fifteen minutes); the code is exchanged with
  `oauth.v2.access` using the app's client pair, the bot token goes through
  the agent's secrets lane exactly as the paste flow's does, the workspace
  and app name are recorded, the agent is restarted, and the person lands
  back on the agent's Access tab with the Slack row in view (`vendor=slack`
  in the hash). Any failure lands there too, with `slack=failed` added and
  nothing else.

Both routes answer 404 while the org's `slack_relay` switch is off, so a
room that has not turned the flow on has nothing public here.

The agent-side contract (daedalus): `POST <agent endpoint>/slack/events`
with header `X-Kobo-Relay-Secret` equal to the agent's `SLACK_RELAY_SECRET`
and the envelope as the body; `200 {"accepted": true}`. The agent endpoint
is `config.agent_endpoint_template` with the agent's name, the same address
the switchboard uses.

Dedupe and the rate limit are per process, like the MCP speed bump: with two
replicas a retry can land on the other one, and the agent's own dedupe (the
runtime keeps the last two thousand event ids) is the wall that holds.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session, sessionmaker

from kobo_api import audit, delivery
from kobo_api.clients import ClientError
from kobo_api.config import Config
from kobo_api.models import Agent, SlackRelayApp
from kobo_api.room_credentials import slack_client
from kobo_api.sero import deliver_secret
from kobo_api.slack_setup import bot_scopes

log = logging.getLogger(__name__)

router = APIRouter()

# Header the agent checks on every forwarded event.
RELAY_SECRET_HEADER = "X-Kobo-Relay-Secret"
# Slack's own headers.
SIGNATURE_HEADER = "X-Slack-Signature"
TIMESTAMP_HEADER = "X-Slack-Request-Timestamp"
# How far a Slack timestamp may sit from the room's clock; Slack's own
# recommendation against replay.
SIGNATURE_WINDOW_S = 300
# `secrets.token_urlsafe(16)` is 22 characters: the relay id's size.
RELAY_ID_BYTES = 16
# The per-agent relay secret, 32 random bytes as url-safe text.
RELAY_SECRET_BYTES = 32
# The install nonce lives this long; the person is on the page when it is minted.
STATE_TTL = timedelta(minutes=15)
# Slack retries an event up to three times over a few minutes; keeping this
# many ids per process covers a busy workspace's retries many times over.
DEDUPE_MAX = 5000
# Events per second one app may post, and the burst it may save up.
RATE_PER_S = 30.0
RATE_BURST = 30
# How long the room waits on the agent per forwarded event.
FORWARD_TIMEOUT_S = 10.0
# Slack's install page.
SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
# The one sentence the failed redirect carries, as a hash query value.
FAILED_QUERY = "slack=failed"
# The Access tab scrolls to the Slack row and highlights it once when the
# hash carries this; the page clears it after reading.
VENDOR_QUERY = "vendor=slack"


# --- ids, secrets and urls ------------------------------------------------------


def new_relay_id() -> str:
    return secrets.token_urlsafe(RELAY_ID_BYTES)


def new_relay_secret() -> str:
    return secrets.token_urlsafe(RELAY_SECRET_BYTES)


def public_base_url(request: Request, cfg: Config) -> str:
    """The room's address as the world sees it: the OAuth issuer the room
    already declares (`mcp.issuer_base_url`), else the request's own base.
    The same rule `kobo_api.mcp.oauth_as` uses for its Google redirect, so
    the relay URL and the install callback are on the one host the identity
    proxy leaves open."""
    if cfg.mcp.issuer_base_url:
        return cfg.mcp.issuer_base_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def relay_url(base: str, relay_id: str) -> str:
    return f"{base}/slack/relay/{relay_id}"


def callback_url(base: str) -> str:
    return f"{base}/slack/oauth/callback"


def face_url(base: str, relay_id: str) -> str:
    """Where Slack fetches the agent's picture for every message it posts
    (the runtime's SLACK_ICON_URL): under the relay prefix, which the identity
    proxy leaves open, keyed by the relay id rather than the agent's name so
    the room's agent list is not enumerable from outside."""
    return f"{base}/slack/relay/{relay_id}/face.png"


def agent_events_url(cfg: Config, agent_name: str) -> str | None:
    """Where the agent takes forwarded events, or None when the room does
    not know its agents' addresses (no `agent_endpoint_template`)."""
    if not cfg.agent_endpoint_template:
        return None
    return cfg.agent_endpoint_template.format(name=agent_name).rstrip("/") + "/slack/events"


def install_url(client_id: str, state: str, redirect_uri: str) -> str:
    """Slack's install page for this app: the bot scopes the manifest
    carries, the room's callback and the single-use state."""
    query = urlencode(
        {
            "client_id": client_id,
            "scope": ",".join(bot_scopes()),
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{SLACK_AUTHORIZE_URL}?{query}"


def mint_state(row: SlackRelayApp, *, now: datetime | None = None) -> str:
    """A fresh single-use install nonce on `row` (the caller commits); the
    state the install link carries names the row and the nonce."""
    nonce = secrets.token_urlsafe(24)
    row.oauth_state = nonce
    row.oauth_state_expires_at = (now or datetime.now(UTC).replace(tzinfo=None)) + STATE_TTL
    return f"{row.relay_id}.{nonce}"


def current_state(row: SlackRelayApp, *, now: datetime | None = None) -> str | None:
    """The row's live state when it has one that has not expired, else None."""
    moment = now or datetime.now(UTC).replace(tzinfo=None)
    if row.oauth_state and row.oauth_state_expires_at and row.oauth_state_expires_at > moment:
        return f"{row.relay_id}.{row.oauth_state}"
    return None


def consume_state(session: Session, state: str, *, now: datetime | None = None) -> SlackRelayApp | None:
    """The row a valid, unexpired `state` names, with the nonce cleared so
    the same link cannot finish an install twice; None for anything else."""
    relay_id, sep, nonce = (state or "").partition(".")
    if not sep or not relay_id or not nonce:
        return None
    row = session.get(SlackRelayApp, relay_id)
    if row is None or not row.oauth_state or row.oauth_state_expires_at is None:
        return None
    moment = now or datetime.now(UTC).replace(tzinfo=None)
    if row.oauth_state_expires_at <= moment:
        return None
    if not hmac.compare_digest(row.oauth_state, nonce):
        return None
    row.oauth_state = None
    row.oauth_state_expires_at = None
    return row


# --- signature, dedupe, rate ----------------------------------------------------


def verify_signature(
    signing_secret: str, timestamp: str | None, body: bytes, signature: str | None, *, now: float | None = None
) -> bool:
    """Slack's request signing: `v0=` + HMAC-SHA256 over `v0:<ts>:<body>`
    with the app's signing secret, and the timestamp within the window."""
    if not timestamp or not signature or not signing_secret:
        return False
    try:
        issued = int(timestamp)
    except ValueError:
        return False
    moment = time.time() if now is None else now
    if abs(moment - issued) > SIGNATURE_WINDOW_S:
        return False
    basestring = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class EventDedupe:
    """The last `DEDUPE_MAX` event ids this process saw, per relay id."""

    def __init__(self, capacity: int = DEDUPE_MAX):
        self._capacity = capacity
        self._seen: set[str] = set()
        self._order: collections.deque[str] = collections.deque()
        self._lock = threading.Lock()

    def seen(self, relay_id: str, event_id: str) -> bool:
        """True when `event_id` was already accepted for `relay_id`; records
        it otherwise."""
        key = f"{relay_id}:{event_id}"
        with self._lock:
            if key in self._seen:
                return True
            self._seen.add(key)
            self._order.append(key)
            while len(self._order) > self._capacity:
                self._seen.discard(self._order.popleft())
            return False


class TokenBucket:
    """One bucket per key: `rate` tokens a second up to `burst`."""

    def __init__(self, rate: float = RATE_PER_S, burst: int = RATE_BURST, clock=time.monotonic):
        self._rate = rate
        self._burst = float(burst)
        self._clock = clock
        self._state: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            tokens, last = self._state.get(key, (self._burst, now))
            tokens = min(self._burst, tokens + (now - last) * self._rate)
            if tokens < 1.0:
                self._state[key] = (tokens, now)
                return False
            self._state[key] = (tokens - 1.0, now)
            return True


dedupe = EventDedupe()
bucket = TokenBucket()

# The one pool the relay forwards through; tests swap it for a mock
# transport. Built lazily so importing this module opens nothing.
_http_client: httpx.Client | None = None
_http_lock = threading.Lock()


def _client() -> httpx.Client:
    global _http_client
    with _http_lock:
        if _http_client is None:
            _http_client = httpx.Client(timeout=httpx.Timeout(FORWARD_TIMEOUT_S, connect=3.0))
        return _http_client


# While an agent restarts, the relay's post lands on no pod at all or on the
# old one, which has no Slack token yet and answers 404: the Install to Slack
# flow rebuilds and redeploys the agent (about two minutes, 2026-11-02), and a
# model change or a Wakes on change rolls it too. Slack sends each event once
# (it had its 200), so an event that meets a restart is kept and sent again on
# this schedule, about four minutes in all. A late pod that already took an
# earlier try drops the duplicate itself (the runtime keeps the last two
# thousand event ids). Statuses that mean "not here yet", never a refusal:
# the agent's own 403 on a wrong relay secret, or any other 4xx, ends it.
RETRY_DELAYS_S: tuple[float, ...] = (5.0, 15.0, 30.0, 60.0, 60.0, 60.0)
RETRY_STATUSES: frozenset[int] = frozenset({404, 502, 503, 504})
_sleep = time.sleep  # tests swap it for a recorder


def forward_event(url: str, relay_secret: str, body: bytes, *, agent_name: str) -> None:
    """Hand one envelope to the agent, trying again through a restart (see
    `RETRY_DELAYS_S`). Outcome is logged by word and status only; the body
    never reaches a log line. Runs after Slack has its 200."""
    delays = (0.0, *RETRY_DELAYS_S)
    for attempt, delay in enumerate(delays):
        if delay:
            _sleep(delay)
        try:
            resp = _client().post(
                url,
                content=body,
                headers={RELAY_SECRET_HEADER: relay_secret, "Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            outcome = f"unreachable ({type(exc).__name__})"
        else:
            if resp.status_code < 400:
                log.info("slack relay: forwarded one event to agent %s%s", agent_name,
                         f" after {attempt} more {'try' if attempt == 1 else 'tries'}" if attempt else "")
                return
            if resp.status_code not in RETRY_STATUSES:
                log.warning("slack relay: agent %s refused an event (%s)", agent_name, resp.status_code)
                return
            outcome = f"answered {resp.status_code}"
        last = attempt == len(delays) - 1
        log.log(logging.WARNING if last else logging.INFO, "slack relay: agent %s %s; %s", agent_name, outcome,
                "the event is dropped" if last else "trying again, it may be restarting")


# --- session --------------------------------------------------------------------


def get_session(request: Request) -> Iterator[Session]:
    """The room's request-scoped session, the same way `kobo_api.main` and
    the OAuth server open one: read the factory off app state so this
    router imports nothing from main."""
    session_factory: sessionmaker[Session] = request.app.state.session_factory
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def _bare(status: int) -> Response:
    return Response(status_code=status)


# --- routes ---------------------------------------------------------------------


@router.get("/slack/relay/{relay_id}/face.png")
def face(relay_id: str, request: Request, session: Session = Depends(get_session)) -> Response:  # noqa: B008
    """The agent's picture, for Slack to show on every message the agent posts
    (2026-11-03). Public like the relay itself: Slack fetches it with no
    credential. A picture is not a secret, but the room's agent list is, so a
    relay id that names no app, or an agent with no picture yet, is a bare 404
    with the same shape as every other miss on this prefix."""
    cfg: Config = request.app.state.config
    if not cfg.slack_relay:
        return _bare(404)
    row = session.get(SlackRelayApp, relay_id)
    if row is None:
        return _bare(404)
    agent = session.query(Agent).filter_by(name=row.agent_name).one_or_none()
    if agent is None or not agent.avatar_png:
        return _bare(404)
    return Response(content=base64.b64decode(agent.avatar_png), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=300"})


@router.post("/slack/relay/{relay_id}")
async def relay(
    relay_id: str,
    request: Request,
    background: BackgroundTasks,
    session: Session = Depends(get_session),  # noqa: B008
) -> Response:
    cfg: Config = request.app.state.config
    if not cfg.slack_relay:
        return _bare(404)
    if not bucket.allow(relay_id):
        return _bare(429)
    row = session.get(SlackRelayApp, relay_id)
    if row is None:
        return _bare(404)

    body = await request.body()
    try:
        envelope = json.loads(body)
    except ValueError:
        return _bare(400)
    if not isinstance(envelope, dict):
        return _bare(400)

    signature = request.headers.get(SIGNATURE_HEADER)
    timestamp = request.headers.get(TIMESTAMP_HEADER)
    signed = bool(row.signing_secret) and verify_signature(
        row.signing_secret or "", timestamp, body, signature
    )

    if envelope.get("type") == "url_verification":
        # Slack verifies the request URL during `apps.manifest.create`, when
        # the row exists and the signing secret does not yet. The challenge
        # carries nothing secret, so a pending row answers it; once the
        # secret is stored every challenge is verified like any other post.
        if row.signing_secret and not signed:
            return _bare(401)
        return JSONResponse({"challenge": str(envelope.get("challenge", ""))})

    if not signed:
        return _bare(401)

    event_id = envelope.get("event_id")
    if isinstance(event_id, str) and event_id and dedupe.seen(relay_id, event_id):
        log.info("slack relay: dropped a repeated event for agent %s", row.agent_name)
        return JSONResponse({"accepted": True})

    url = agent_events_url(cfg, row.agent_name)
    if url is None:
        log.warning("slack relay: no agent endpoint template; event for agent %s dropped", row.agent_name)
        return JSONResponse({"accepted": True})

    background.add_task(forward_event, url, row.relay_secret, body, agent_name=row.agent_name)
    return JSONResponse({"accepted": True})


def _agent_page(name: str | None, *, failed: bool) -> str:
    """Where the callback sends the person: the agent's Access tab with the
    Slack row brought into view (`?vendor=slack`, which the page reads once),
    or the agent list when the state named no agent."""
    if name:
        path = f"/#/agents/{quote(name)}/access?{VENDOR_QUERY}"
        return f"{path}&{FAILED_QUERY}" if failed else path
    return f"/#/agents?{FAILED_QUERY}" if failed else "/#/agents"


def _failed(name: str | None) -> RedirectResponse:
    return RedirectResponse(_agent_page(name, failed=True), status_code=302)


@router.get("/slack/oauth/callback")
def oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: Session = Depends(get_session),  # noqa: B008
) -> Response:
    cfg: Config = request.app.state.config
    if not cfg.slack_relay:
        return _bare(404)

    row = consume_state(session, state or "")
    if row is None:
        # Nothing names an agent we can trust, so nothing to show but the list.
        log.info("slack install: callback with no usable state")
        return _failed(None)
    name = row.agent_name
    # The nonce is spent whatever happens next: a second visit with the same
    # link must not finish an install the first one did not.
    session.commit()

    if error or not code or not row.client_id or not row.client_secret:
        log.info("slack install: declined or incomplete for agent %s", name)
        return _failed(name)

    clients = request.app.state.clients
    slack = slack_client(clients)
    if slack is None:
        log.warning("slack install: the room has no Slack integration (agent %s)", name)
        return _failed(name)
    agent = session.query(Agent).filter_by(name=name).first()
    if agent is None:
        return _failed(None)

    try:
        answer = slack.oauth_access(
            client_id=row.client_id,
            client_secret=row.client_secret,
            code=code,
            redirect_uri=callback_url(public_base_url(request, cfg)),
        )
    except ClientError as exc:
        log.warning("slack install: exchange failed for agent %s (%s)", name, type(exc).__name__)
        return _failed(name)

    bot_token = str(answer.get("access_token") or "")
    if not bot_token:
        log.warning("slack install: no bot token in Slack's answer for agent %s", name)
        return _failed(name)

    acting_email = (row.created_by or agent.created_by or "").strip() or None
    try:
        store_bot_token(clients, agent_name=name, bot_token=bot_token, acting_email=acting_email)
    except ClientError as exc:
        log.warning("slack install: could not store the bot token for agent %s (%s)", name, type(exc).__name__)
        return _failed(name)

    team = answer.get("team") if isinstance(answer.get("team"), dict) else {}
    row.workspace_name = str(team.get("name") or "")[:200] or None
    row.workspace_id = str(team.get("id") or "")[:32] or None
    row.app_name = agent.name
    row.installed_at = datetime.now(UTC).replace(tzinfo=None)
    agent.slack_wired = True
    if row.app_id and not agent.slack_app_id:
        agent.slack_app_id = row.app_id

    audit.record(
        session,
        request=request,
        action="slack.install",
        target_kind="agent",
        target_id=name,
        agent_name=name,
        actor=acting_email,
        summary=f"{(acting_email or 'someone').split('@', 1)[0]} installed agent {name}'s Slack app",
        detail={"workspace": row.workspace_name},
        source=audit.SOURCE_API,
    )
    session.commit()

    # The token is stored; the pod reads it at its next start. Through the secrets
    # platform that start must come AFTER the cluster has re-read the value, so the
    # restart waits for the key, off the request (tenishai/kobo#434 follow-up).
    delivery.restart_once_secret_carries(clients.kubectl, name, f"{name}-slack", ["SLACK_BOT_TOKEN"])
    log.info("slack install: agent %s installed to a workspace", name)
    return RedirectResponse(_agent_page(name, failed=False), status_code=302)


def store_bot_token(clients, *, agent_name: str, bot_token: str, acting_email: str | None) -> None:
    """Put `SLACK_BOT_TOKEN` beside the app's other values in the agent's
    Slack secret: through the secrets lane when the room has one (its put
    merges, so the five values Create stored stay), else by reading the
    namespace Secret back and writing it whole. Raises `ClientError`."""
    secret_name = f"{agent_name}-slack"
    if getattr(clients, "sero", None) is not None:
        deliver_secret(
            clients.sero,
            acting_email=acting_email,
            agent_name=agent_name,
            purpose="slack",
            values={"SLACK_BOT_TOKEN": bot_token},
            secret_name=secret_name,
        )
        return
    kubectl = clients.kubectl
    try:
        existing = kubectl.secret_literals(agent_name, secret_name)
    except ClientError:
        existing = {}
    kubectl.apply_secret(agent_name, secret_name, {**existing, "SLACK_BOT_TOKEN": bot_token})

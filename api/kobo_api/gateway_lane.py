"""The gateway lane follows the permission (kobo#327, per permission since kobo#365,
read from the gateway's handler cards since #524).

With the permission service on, an agent holds permissions, not lanes: a
grant on a Datadog connection lets it borrow a scoped application key for an
hour; a grant on its GitHub repository set lets it borrow an installation
token narrowed to that set. The tool gateway still has to let the agent's
calls through to the vendor's handler and to the vendor's MCP target, and it
decides that from its own grant table: a scope per target and an allowed
handler list per persona. This module keeps the two in step, so approving
the permission is the only thing anyone grants.

What a permission opens comes from two places:

- **The room's own targets.** The room puts the vendors' MCP servers on the
  gateway itself (`admin_integrations`), so their lanes are the room's to
  say: `TARGET_LANES` per vendor (any permission of that vendor), and
  `TARGET_PERMISSION_LANES` per (vendor, permission) for a vendor whose
  levels open different targets (GitHub: Read opens the read-only
  `github-mcp`, Review and Open pull requests open `github-mcp-write`).
- **The gateway's handlers, as their cards say** (`GET /admin/cards`, liminal
  0.10.0). A card's `labels.vendor` names the vendor whose permissions open
  it, each tool's `permissions` the ones that open that tool (`*`: any of the
  vendor's), and its `required_scope` (else the card's) the scope a grant
  must hold to call it. `labels.audience: own-code` keeps a handler to agents
  that run their team's own code (kobo#483: `github-clone` hands its caller a
  token, which a model must never hold). A new handler, or a new tool, reaches
  agents with no change to the room.

`follow` recomputes the managed part of the agent's gateway grant from its
grants in the permission service and writes it live through the gateway's
admin plane, leaving whatever else the grant carries (scopes and handlers the
room did not put there) alone, and brings the agent's own bundle allow list
in step both ways: a pattern the agent now earns is added, one it no longer
earns is withdrawn (one commit, one restart). Best effort: it never raises,
it answers one sentence for the audit row.

What counts as the room's to withdraw is everything a lane names now, every
part a card's lane has ever named (`GatewayManaged`, so a handler that leaves
the cards is still withdrawn from every agent that held it), and the few
things retired before the room kept that record. A failed card read keeps the
last cards read (`GatewayCards`): read as "no handlers", one gateway restart
would strip every agent's tools on the next pass.

A lane new to the cards reaches the agents that already hold its vendor
without anyone granting again: the heal pass finds their gateway grant short
of what the lanes now owe, sets it, and brings the bundle allow list in step
in the same pass (`_heal_ready_agents`).

Import-light on purpose: it takes the clients bag and the permission-service
client it is handed, and never imports the app.
"""

from __future__ import annotations

import logging
import tempfile
import time
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path
from typing import Any, NamedTuple

import yaml
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import delivery
from .clients import ClientError
from .composer import update_agent_yaml_allow
from .kinds import KIND_COMPOSED, KIND_CONNECTED
from .liminal_admin import ISSUER, GatewayGrant
from .models import Agent, AgentStatus, GatewayCards, GatewayManaged
from .repo_lock import repo_write_lock

log = logging.getLogger(__name__)

class Lane(NamedTuple):
    """What an agent granted a vendor (or one of its permissions) needs on the
    gateway (scopes, handlers) and in its own bundle (allow patterns, matched by
    the runtime with fnmatch): the gateway filters the catalog per grant, the
    runtime offers only what the bundle allows, so a permission has to open both."""

    scopes: tuple[str, ...]
    handlers: tuple[str, ...]
    allow: tuple[str, ...]


# vendor -> the lane to a target the room puts on the gateway, for ANY permission of that vendor.
TARGET_LANES: dict[str, Lane] = {
    # Datadog's hosted MCP server (kobo#327), each agent's calls carrying its leased key.
    "datadog": Lane(scopes=("datadog:read",), handlers=("datadog-mcp",), allow=("datadog-mcp.*",)),
    # Atlassian (2026-11-02): one shared credential on the gateway's `atlassian` target
    # (`required_scope atlassian:read`, admin_integrations.target_spec); nothing is leased.
    "atlassian": Lane(scopes=("atlassian:read",), handlers=("atlassian",), allow=("atlassian.*",)),
}
# (vendor, permission) -> the lane to the room's targets that one permission needs; wins
# over the vendor's entry above (kobo#365: GitHub's three levels).
TARGET_PERMISSION_LANES: dict[tuple[str, str], Lane] = {
    ("github", "github_repos_read"): Lane(scopes=("github:read",), handlers=("github-mcp",), allow=("github-mcp.*",)),
    # Review and Open pull requests open the vendor's own write target (branches, commits,
    # edits); Open pull requests also earns the `github:write` scope its creating tools need.
    ("github", "github_pulls_review"): Lane(scopes=("github:review",), handlers=("github-mcp-write",),
                                            allow=("github-mcp-write.*",)),
    ("github", "github_pulls_write"): Lane(scopes=("github:review", "github:write"), handlers=("github-mcp-write",),
                                           allow=("github-mcp-write.*",)),
    # Atlassian's one writing level (2026-11-03): the target's Jira field tools (edit, transition)
    # sit behind `atlassian:jira-write` (admin_integrations.TARGET_EXTRAS); its read permissions
    # keep the vendor lane above. The shared credential is the outer wall, so a read-only
    # connection never offers this permission (its ceiling is the read tier).
    ("atlassian", "atlassian_jira_issues_write"): Lane(scopes=("atlassian:read", "atlassian:jira-write"),
                                                       handlers=("atlassian",), allow=("atlassian.*",)),
}
# The permissions a room target serves tools for, where that is not every permission of its
# vendor (kobo#474): Datadog's hosted server serves a fixed default set of tools, and the
# permissions left out here have none in it. A handler's tools say their own (its card).
TARGET_TOOLED: dict[str, frozenset[str]] = {
    "datadog": frozenset({
        "datadog_apm_read", "datadog_dashboards_read", "datadog_events_read", "datadog_hosts_read",
        "datadog_incidents_read", "datadog_logs_read", "datadog_metrics_read", "datadog_monitors_read",
        "datadog_rum_read",
    }),
}
# What the room put there before it kept a record of what it manages, and no lane earns any
# more: withdrawn on the next `follow` (the gateway's `github` handler and the broker sidecar's
# tools, kobo#365). Frozen: from #524 on the room remembers by itself (`GatewayManaged`).
RETIRED_HANDLERS: tuple[str, ...] = ("github",)
RETIRED_ALLOW: dict[str, str] = {"github.*": "github"}
# Allow entries named one by one that the room's own tools used to need and that nothing serves
# any more: the token broker's `github.<tool>` entries (github.search_prs, github.open_pr, ...)
# answer HTTP 405 now that the broker is gone, and the room's own Jira read lane (jira.get_issue,
# jira.find_issues) retired on 2026-11-04 for the Atlassian connection. `sync_allow` withdraws any
# entry under one of these prefixes that no lane earns, on the next sync. "github-mcp." does not
# match "github.".
RETIRED_PREFIXES: tuple[str, ...] = ("github.", "jira.")

# A card's `labels.audience` for a handler only an agent running its team's own code may hold.
OWN_CODE = "own-code"
# A tool's `permissions` entry for "any permission of the card's vendor".
ANY_PERMISSION = "*"
# A tool's `risk`, as the platform proposal names them (agora #68): kept with the card for the
# day calls are registered operations; nothing here decides by it yet.
RISKS = frozenset({"low", "write", "irreversible"})

ALLOW_COMMIT = "kobo: allow the {vendor} tools"
WITHDRAW_COMMIT = "kobo: withdraw the {vendor} tools"

NOT_WIRED = "the gateway admin plane is not wired; the lane did not change"
ALLOW_FAILED = "; the agent's allow list could not be updated"
UNCHANGED = "gateway lane unchanged"
REMOVED = "gateway lane removed"


class CardTool(NamedTuple):
    """One tool as its card says: the scope a grant needs to call it (its own
    `required_scope`, else the card's), the permissions that open it, its risk."""

    name: str
    scope: str | None
    permissions: frozenset[str]
    risk: str | None


class HandlerCard(NamedTuple):
    """A handler a permission can open: its name on the gateway (what a grant's
    `allowed_handlers` and the tool names use), the vendor whose permissions open
    it, whether only an agent that runs its own code may hold it, and its tools."""

    name: str
    vendor: str
    own_code: bool
    tools: tuple[CardTool, ...]

    def opened_by(self, permission: str) -> list[CardTool]:
        return [t for t in self.tools if permission in t.permissions or ANY_PERMISSION in t.permissions]


def parse_cards(raw: Any) -> tuple[HandlerCard, ...]:
    """The handlers a permission can open, from `GET /admin/cards` as the
    gateway answered. A card with no `labels.vendor` is no lane and is left
    out, as is a tool with no `permissions`. A card whose `audience` is not
    one the room knows is left out whole: guessing "anyone" could hand a
    model a credential. Never raises: one bad card costs only its own lane."""
    out: list[HandlerCard] = []
    for entry in raw if isinstance(raw, list) else []:
        name = entry.get("name") if isinstance(entry, dict) else None
        card = entry.get("card") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name or not isinstance(card, dict):
            log.warning("gateway card skipped: no name or card in %r", str(entry)[:120])
            continue
        labels = card.get("labels") if isinstance(card.get("labels"), dict) else {}
        vendor, audience = labels.get("vendor"), labels.get("audience")
        if not isinstance(vendor, str) or not vendor:
            continue
        if audience not in (None, OWN_CODE):
            log.warning("gateway card %s skipped: audience %r is not one the room knows", name, audience)
            continue
        card_scope = card.get("required_scope") if isinstance(card.get("required_scope"), str) else None
        tools: list[CardTool] = []
        for t in card.get("tools") or []:
            if not isinstance(t, dict) or not isinstance(t.get("name"), str):
                continue
            permissions = t.get("permissions")
            if not isinstance(permissions, list) or not permissions or not all(isinstance(x, str) and x for x in permissions):
                if permissions is not None:
                    log.warning("gateway card %s: tool %s has unreadable permissions %r", name, t["name"], permissions)
                continue
            scope = t.get("required_scope") if isinstance(t.get("required_scope"), str) else card_scope
            risk = t.get("risk") if t.get("risk") in RISKS else None
            tools.append(CardTool(t["name"], scope, frozenset(permissions), risk))
        if tools:
            out.append(HandlerCard(name, vendor, audience == OWN_CODE, tuple(tools)))
    return tuple(sorted(out, key=lambda c: c.name))


def _target_lane(vendor: str, permission: str) -> Lane | None:
    return TARGET_PERMISSION_LANES.get((vendor, permission)) or TARGET_LANES.get(vendor)


def _merged(parts: list[Lane]) -> Lane:
    return Lane(*(tuple(sorted({x for lane in parts for x in getattr(lane, field)})) for field in Lane._fields))


@dataclass(frozen=True)
class Lanes:
    """What each permission opens: the room's targets, plus the gateway's
    handlers as `cards` say. `known` is False when the room has never read
    the cards (a gateway older than liminal 0.10.0, or one that has not
    answered since the room first started): then only the targets are
    managed, so nothing the cards would name is ever withdrawn on a guess.
    `remembered` is every (kind, value, vendor) a card's lane has named."""

    cards: tuple[HandlerCard, ...] = ()
    known: bool = False
    remembered: frozenset[tuple[str, str, str]] = frozenset()

    def lane_for(self, vendor: str, permission: str, *, own_code: bool = False) -> Lane | None:
        """The lane one active grant earns: the room's target lane for it, and
        every handler with a tool the permission opens, with the scopes those
        tools need. `own_code` admits the handlers kept to agents that run
        their team's own code. None for a permission that opens nothing."""
        parts = [lane] if (lane := _target_lane(vendor, permission)) is not None else []
        for card in self.cards:
            if card.vendor != vendor or (card.own_code and not own_code):
                continue
            if opened := card.opened_by(permission):
                parts.append(Lane(scopes=tuple(sorted({t.scope for t in opened if t.scope})), handlers=(card.name,),
                                  allow=() if card.own_code else (f"{card.name}.*",)))
        return _merged(parts) if parts else None

    def opens_tool(self, vendor: str, permission: str) -> bool:
        """Whether holding this permission puts any tool in front of an agent
        the room builds (kobo#474), so the picker greys a row that opens
        nothing instead of offering a grant that does nothing. A room target
        serves every permission of its vendor unless `TARGET_TOOLED` says
        otherwise; a handler serves the ones its tools name. Before the room
        has read any cards it cannot tell, and says yes: the grant then opens
        its tools as soon as the cards are read (the heal pass)."""
        tooled = TARGET_TOOLED.get(vendor)
        if _target_lane(vendor, permission) is not None and (tooled is None or permission in tooled):
            return True
        if not self.known:
            return True
        return any(card.vendor == vendor and not card.own_code and card.opened_by(permission) for card in self.cards)

    def parts(self) -> frozenset[tuple[str, str, str]]:
        """(kind, value, vendor) for every scope, handler and allow pattern
        the cards' lanes name now: what `remember` keeps."""
        out: set[tuple[str, str, str]] = set()
        for card in self.cards:
            out.add(("handler", card.name, card.vendor))
            out.update(("scope", t.scope, card.vendor) for t in card.tools if t.scope)
            if not card.own_code:
                out.add(("allow", f"{card.name}.*", card.vendor))
        return frozenset(out)

    @cached_property
    def _managed(self) -> frozenset[tuple[str, str, str]]:
        targets = [*TARGET_LANES.items(), *((v, lane) for (v, _), lane in TARGET_PERMISSION_LANES.items())]
        out = {(kind, value, vendor) for vendor, lane in targets
               for kind, values in (("scope", lane.scopes), ("handler", lane.handlers), ("allow", lane.allow))
               for value in values}
        out.update(("handler", h, "") for h in RETIRED_HANDLERS)
        out.update(("allow", p, v) for p, v in RETIRED_ALLOW.items())
        if self.known:
            out.update(self.parts() | self.remembered)
        return frozenset(out)

    @cached_property
    def managed_scopes(self) -> frozenset[str]:
        return frozenset(value for kind, value, _ in self._managed if kind == "scope")

    @cached_property
    def managed_handlers(self) -> frozenset[str]:
        return frozenset(value for kind, value, _ in self._managed if kind == "handler")

    @cached_property
    def allow_vendor(self) -> dict[str, str]:
        """allow pattern -> the vendor it belongs to, for the commit message."""
        return {value: vendor for kind, value, vendor in sorted(self._managed) if kind == "allow"}

    @property
    def managed_allow(self) -> frozenset[str]:
        return frozenset(self.allow_vendor)


# How long one ask for the cards serves a process before the next, answered or not. The heal
# pass always asks (`current(..., refresh=True)`) and is the one that keeps the answer.
CARDS_FRESH_S = 60.0


class _Read(NamedTuple):
    asked: float  # monotonic time of this process's last ask
    raw: list | None  # the cards from its last answer, as the gateway gave them
    read_at: datetime | None  # when that answer came


# admin client -> this process's last ask.
_READS: weakref.WeakKeyDictionary[Any, _Read] = weakref.WeakKeyDictionary()


def _read_cards(admin) -> list | None:
    """The gateway's cards now, or None when it cannot answer (an older
    gateway, or one restarting): never an empty list for "unknown"."""
    try:
        return admin.cards()
    except ClientError as exc:
        log.warning("the gateway's handler cards could not be read; keeping the last ones read: %s", exc)
    except AttributeError:
        pass  # an admin client from before liminal 0.10.0 has no `cards`
    return None


def _kept_cards(session: Session) -> list | None:
    try:
        row = session.get(GatewayCards, 1)
    except SQLAlchemyError as exc:
        log.warning("the gateway cards the room kept could not be read: %s", exc)
        return None
    return row.cards if row is not None else None


def _remembered(session: Session) -> frozenset[tuple[str, str, str]]:
    try:
        return frozenset((r.kind, r.value, r.vendor) for r in session.query(GatewayManaged).all())
    except SQLAlchemyError as exc:
        log.warning("what the gateway cards' lanes have named could not be read: %s", exc)
        return frozenset()


def current(session: Session | None, clients, *, refresh: bool = False) -> Lanes:
    """The lanes as of now. The cards come from the gateway, asked at most
    once every `CARDS_FRESH_S` per process, answered or not (`refresh` asks
    now); when it cannot answer, the last cards it gave are kept, this
    process's or, after a restart, the database's. A database that cannot be
    read keeps nothing: then only the targets are managed, which withdraws
    nothing a card would have named."""
    admin = getattr(clients, "liminal_admin", None) if clients is not None else None
    raw: list | None = None
    if admin is not None:
        last = _READS.get(admin)
        if refresh or last is None or time.monotonic() - last.asked >= CARDS_FRESH_S:
            got = _read_cards(admin)
            last = (_Read(time.monotonic(), got, datetime.now(UTC).replace(tzinfo=None)) if got is not None
                    else _Read(time.monotonic(), last.raw if last else None, last.read_at if last else None))
            _READS[admin] = last
        raw = last.raw
    if raw is None and session is not None:
        raw = _kept_cards(session)
    remembered = _remembered(session) if session is not None else frozenset()
    return Lanes(cards=parse_cards(raw) if raw is not None else (), known=raw is not None, remembered=remembered)


def remember(session: Session, clients, lanes: Lanes) -> None:
    """Keep this process's last answer from the gateway, with when it came,
    and every part its lanes name, on the caller's session (the caller
    commits). The heal pass calls it after asking afresh."""
    admin = getattr(clients, "liminal_admin", None) if clients is not None else None
    last = _READS.get(admin) if admin is not None else None
    if last is None or last.raw is None or last.read_at is None or not lanes.known:
        return
    row = session.get(GatewayCards, 1)
    if row is None:
        session.add(GatewayCards(id=1, cards=last.raw, read_at=last.read_at))
    elif row.read_at != last.read_at:
        row.cards, row.read_at = last.raw, last.read_at
    for kind, value, vendor in sorted(lanes.parts() - lanes.remembered):
        if session.get(GatewayManaged, (kind, value)) is None:
            session.add(GatewayManaged(kind=kind, value=value, vendor=vendor, first_seen=last.read_at))


def runs_own_code(session: Session, agent_name: str) -> bool:
    """Whether this agent is a team's own code on the team's own runtime, so
    the handlers kept to such agents (`OWN_CODE`) open for it. Compared, never
    truth-tested: any other kind, and an agent the room does not know, gets
    what a built one gets."""
    agent = session.query(Agent).filter_by(name=agent_name).one_or_none()
    return agent is not None and agent.kind == KIND_CONNECTED


def held_permissions(bab, agent_name: str, actor: str) -> list[tuple[str, str]]:
    """(vendor, permission) for every active grant the agent holds. A grant on
    an integration the permission service no longer lists counts for nothing."""
    vendor_of = {i.get("integration"): i.get("vendor") for i in bab.integrations(actor)}
    held: list[tuple[str, str]] = []
    for g in bab.grants(agent_name, actor):
        vendor = vendor_of.get(g.get("integration"))
        if g.get("state", "active") == "active" and vendor and (str(vendor), str(g.get("permission") or "")) not in held:
            held.append((str(vendor), str(g.get("permission") or "")))
    return held


def lanes_of(held: list[tuple[str, str]], lanes: Lanes, *, own_code: bool = False) -> list[Lane]:
    """The lanes those permissions earn, each once."""
    out: list[Lane] = []
    for vendor, permission in held:
        lane = lanes.lane_for(vendor, permission, own_code=own_code)
        if lane is not None and lane not in out:
            out.append(lane)
    return out


def held_lanes(bab, agent_name: str, actor: str, *, lanes: Lanes, own_code: bool = False) -> list[Lane]:
    """The lanes of every (vendor, permission) the agent holds an active grant
    on. `own_code` adds the handlers kept to agents that run their own code."""
    return lanes_of(held_permissions(bab, agent_name, actor), lanes, own_code=own_code)


def desired_allow(lanes: list[Lane]) -> set[str]:
    """The bundle allow patterns the agent's lanes earn."""
    return {p for lane in lanes for p in lane.allow}


def _vendors_of(patterns: set[str], lanes: Lanes) -> str:
    return ", ".join(sorted({lanes.allow_vendor.get(p, p.split(".", 1)[0]) for p in patterns}))


def sync_allow(session: Session, clients, *, agent_name: str, patterns: set[str], lanes: Lanes) -> str:
    """Make the agent's own bundle admit exactly the managed patterns in
    `patterns` (one commit, one restart): the ones it lacks are added, the
    managed ones it carries and no longer earns are withdrawn; entries the
    room never put there stay. Returns a clause for the sentence: "" when
    nothing had to change or the agent is not the room's own, else what was
    pushed or why it could not be."""
    agent = session.query(Agent).filter_by(name=agent_name).one_or_none()
    github = getattr(clients, "github", None) if clients is not None else None
    if agent is None or agent.kind != KIND_COMPOSED or not agent.repo_full_name or github is None:
        return ""
    if agent.status != AgentStatus.READY:
        return ""
    wanted = sorted(patterns & lanes.managed_allow)
    withdraw = sorted(lanes.managed_allow - patterns)
    repo = agent.repo_full_name
    try:
        with repo_write_lock(repo), tempfile.TemporaryDirectory(prefix="kobo-allow-") as tmp:
            workdir = Path(tmp) / "repo"
            github.clone(repo, workdir)
            before = _allow_entries(workdir / "bundle")
            withdraw = sorted(set(withdraw) | {p for p in before if p.startswith(RETIRED_PREFIXES) and p not in patterns})
            if not update_agent_yaml_allow(workdir / "bundle", entries=wanted, remove=withdraw):
                return ""
            added = [p for p in wanted if p not in before]
            gone = [p for p in withdraw if p in before]
            delivery.push(github, workdir, _commit_message(added, gone, lanes))
    except (ClientError, OSError) as exc:
        log.warning("allow list for %s could not be updated: %s", agent_name, exc)
        return f"{ALLOW_FAILED}: {exc}"
    said = []
    if added:
        said.append(f"now allows {', '.join(added)}")
    if gone:
        said.append(f"no longer allows {', '.join(gone)}")
    return f"; the agent {' and '.join(said)} (it restarts once)"


def _commit_message(added: list[str], gone: list[str], lanes: Lanes) -> str:
    """One commit for both halves: "kobo: allow the github tools",
    "kobo: withdraw the github tools", or both joined when a level change
    opens one target and closes another."""
    parts = []
    if added:
        parts.append(ALLOW_COMMIT.format(vendor=_vendors_of(set(added), lanes)))
    if gone:
        parts.append(WITHDRAW_COMMIT.format(vendor=_vendors_of(set(gone), lanes)) if not added
                     else f"withdraw the {_vendors_of(set(gone), lanes)} tools")
    return "; ".join(parts)


def _allow_entries(bundle_dir: Path) -> list[str]:
    """The bundle's allow list as it stands, [] when it cannot be read (the
    editor raises its own `ClientError` for the sentence)."""
    try:
        data = yaml.safe_load((bundle_dir / "agent.yaml").read_text())
        return [str(p) for p in ((data or {}).get("tools") or {}).get("allow") or []]
    except (OSError, yaml.YAMLError, AttributeError):
        return []


def _owed_grant(existing: GatewayGrant | None, held: list[Lane], lanes: Lanes):
    """(old scopes, old handlers, new scopes, new handlers) for one agent:
    the managed part recomputed from the lanes it holds, everything else
    kept. A grant that allows every handler (None) is never narrowed."""
    scopes = {s for lane in held for s in lane.scopes}
    handlers = {h for lane in held for h in lane.handlers}
    old_scopes = set(existing.scopes) if existing is not None else set()
    new_scopes = (old_scopes - lanes.managed_scopes) | scopes
    old_handlers: set[str] | None
    new_handlers: set[str] | None
    if existing is not None and existing.allowed_handlers is None:
        old_handlers = new_handlers = None
    else:
        old_handlers = set(existing.allowed_handlers) if existing is not None else set()
        new_handlers = (old_handlers - lanes.managed_handlers) | handlers
    return old_scopes, old_handlers, new_scopes, new_handlers


def follow(session: Session, clients, bab, *, agent_name: str, actor: str) -> str:
    """Bring the agent's gateway grant in step with its permissions, and its
    bundle's allow list with the lanes it holds, and say what happened in
    one sentence. Scopes and handlers the room does not manage stay; a grant
    that allows every handler (None) is never narrowed; allow entries the
    room manages are added and withdrawn as the lanes say, others stay."""
    admin = getattr(clients, "liminal_admin", None) if clients is not None else None
    if admin is None:
        return NOT_WIRED
    try:
        lanes = current(session, clients)
        held = held_lanes(bab, agent_name, actor, lanes=lanes, own_code=runs_own_code(session, agent_name))
        allowed = sync_allow(session, clients, agent_name=agent_name, patterns=desired_allow(held), lanes=lanes)
        existing = next((g for g in admin.list_grants() if g.issuer == ISSUER and g.persona == agent_name), None)
        old_scopes, old_handlers, new_scopes, new_handlers = _owed_grant(existing, held, lanes)
        if new_scopes == old_scopes and new_handlers == old_handlers:
            return UNCHANGED + allowed
        if not new_scopes and new_handlers is not None and not new_handlers:
            admin.revoke(ISSUER, agent_name)
            return REMOVED + allowed
        admin.grant(GatewayGrant(
            issuer=ISSUER, persona=agent_name, scopes=tuple(sorted(new_scopes)),
            allowed_handlers=None if new_handlers is None else tuple(sorted(new_handlers)),
        ))
        shown_handlers = "every handler" if new_handlers is None else (", ".join(sorted(new_handlers)) or "none")
        return f"gateway lane set: scopes {', '.join(sorted(new_scopes)) or 'none'}; handlers {shown_handlers}{allowed}"
    except ClientError as exc:
        log.warning("gateway lane for %s did not follow its permissions: %s", agent_name, exc)
        return f"the gateway admin plane refused: {exc}"


# How often the room checks the gateway still holds what it granted. Something
# other than the room can rewrite a grant: the gateway's own boot seed
# (grants.toml) re-applies a file stanza over the live row on every restart,
# which took ledger's GitHub lane away for a day (2026-11-04).
HEAL_INTERVAL_S = 300.0


def heal(clients, bab, agent_names: list[str], actor: str, *, lanes: Lanes,
         own_code: frozenset[str] = frozenset()) -> list[str]:
    """Set each agent's gateway grant back to what its permissions owe when
    something else has changed it, or the lanes have, and name the agents it
    set back. Only the gateway grant: the bundle allow list lives in the
    agent's repository, which nothing but the room writes, so the caller
    brings it in step for the agents named here and no others. An agent
    holding no permission is left alone, so an agent whose grant lives only
    in the gateway's own file keeps it; one whose permissions open nothing
    any more (its handler left the cards) is set back like any other.
    `own_code` names the agents that run their team's own code, which the
    same permissions open a little more for (`OWN_CODE`). Best effort: one
    agent's failure never stops the rest."""
    admin = getattr(clients, "liminal_admin", None) if clients is not None else None
    if admin is None or bab is None:
        return []
    try:
        live = {g.persona: g for g in admin.list_grants() if g.issuer == ISSUER}
    except ClientError as exc:
        log.warning("gateway grants could not be read for the heal pass: %s", exc)
        return []
    healed: list[str] = []
    for name in agent_names:
        try:
            held = held_permissions(bab, name, actor)
            if not held:
                continue
            existing = live.get(name)
            old_scopes, old_handlers, new_scopes, new_handlers = _owed_grant(
                existing, lanes_of(held, lanes, own_code=name in own_code), lanes)
            if new_scopes == old_scopes and new_handlers == old_handlers:
                continue
            admin.grant(GatewayGrant(
                issuer=ISSUER, persona=name, scopes=tuple(sorted(new_scopes)),
                allowed_handlers=None if new_handlers is None else tuple(sorted(new_handlers)),
            ))
        except ClientError as exc:
            log.warning("gateway grant for %s could not be checked or set back: %s", name, exc)
            continue
        log.warning(
            "gateway grant for %s had drifted from its permissions (scopes %s, handlers %s); set back to scopes %s, handlers %s",
            name, sorted(old_scopes), "all" if old_handlers is None else sorted(old_handlers),
            sorted(new_scopes), "all" if new_handlers is None else sorted(new_handlers),
        )
        healed.append(name)
    return healed


def _heal_ready_agents(app) -> list[str]:
    # The same pass fills in a live target the gateway lacks for an applied
    # connection (2026-11-06: `github-mcp-write` was never put, since connect
    # is otherwise the only writer). Imported here, not at module top, to
    # keep this module import-light.
    from .admin_integrations import reconcile_targets
    from .bab import ROOM_ACTOR

    clients = app.state.clients
    bab = getattr(clients, "bab", None)
    with app.state.session_factory() as session:
        ready = session.query(Agent).filter(Agent.status == AgentStatus.READY).all()
        names = [a.name for a in ready]
        own_code = frozenset(a.name for a in ready if a.kind == KIND_CONNECTED)
        reconcile_targets(session, clients)
        # The pass reads the cards afresh and is the one that keeps the read: a later
        # request, or a room restarted while the gateway cannot answer, works from it.
        lanes = current(session, clients, refresh=True)
    with app.state.session_factory() as session:
        try:
            remember(session, clients, lanes)
            session.commit()
        except SQLAlchemyError as exc:  # another replica kept the same read a moment before
            session.rollback()
            log.info("the gateway cards were not kept this pass: %s", exc)
    healed = heal(clients, bab, names, ROOM_ACTOR, lanes=lanes, own_code=own_code)
    # A grant that was short is also how a lane new to the cards shows up on an agent
    # that already held its vendor (AWS, 2026-11-13): the bundle has to admit the lane's
    # tools too, or the agent holds a permission it has no tool for. An agent whose
    # repository could not be written is kept and tried again on the next pass.
    owed: set[str] = (getattr(app.state, "allow_owed", None) or set()) & set(names) | set(healed)
    for name in sorted(owed):
        try:
            held = held_permissions(bab, name, ROOM_ACTOR)
            if not held:
                owed.discard(name)  # as `heal`: an agent holding nothing is left alone
                continue
            with app.state.session_factory() as session:
                said = sync_allow(session, clients, agent_name=name, patterns=desired_allow(lanes_of(held, lanes)),
                                  lanes=lanes)
        except ClientError as exc:
            log.warning("allow list for %s could not be brought in step: %s", name, exc)
            continue
        if not said.startswith(ALLOW_FAILED):
            owed.discard(name)
            if said:
                log.info("allow list for %s brought in step with its permissions%s", name, said)
    app.state.allow_owed = owed
    return healed


async def heal_loop(app, *, interval_s: float = HEAL_INTERVAL_S) -> None:
    """`heal` over every ready agent, every `interval_s` for the life of the
    process. Sleeps first: the gateway was in step at boot a minute ago as
    often as not, and a restart of either side is caught one interval later."""
    import asyncio

    while True:
        await asyncio.sleep(interval_s)
        try:
            await asyncio.to_thread(_heal_ready_agents, app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("gateway grant heal pass failed")
        # The same beat keeps agents that run their team's own code wired
        # (`kobo_api.connected_wiring`). Its own try: a repository the room
        # cannot read must not cost the grants their heal, or the reverse.
        try:
            from . import connected_wiring

            await asyncio.to_thread(connected_wiring.keep, app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("connected wiring pass failed")
        # And lists the ones whose code has started answering the
        # switchboard (`kobo_api.directory.keep_connected`), so Chat and
        # other agents can ask them.
        try:
            from . import directory

            await asyncio.to_thread(directory.keep_connected, app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("connected directory pass failed")


def tally(sentence: str) -> str:
    """Which bucket a `follow` sentence falls in: `set`, `unchanged` or `failed`."""
    if sentence.startswith(("gateway lane set", REMOVED)):
        return "set"
    if "could not be updated" in sentence:
        return "failed"
    if sentence == NOT_WIRED or sentence.startswith(UNCHANGED):
        return "unchanged"
    return "failed"


__all__ = [
    "ANY_PERMISSION", "HEAL_INTERVAL_S", "OWN_CODE", "RETIRED_HANDLERS", "RETIRED_PREFIXES", "RISKS", "TARGET_LANES",
    "TARGET_PERMISSION_LANES", "TARGET_TOOLED", "CardTool", "HandlerCard", "Lane", "Lanes", "current", "desired_allow",
    "follow", "heal", "heal_loop", "held_lanes", "held_permissions", "lanes_of", "parse_cards", "remember",
    "runs_own_code", "sync_allow", "tally",
]

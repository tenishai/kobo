"""Derives the flat allow-pairs set the switchboard (cabal) will enforce
dispatch against (access-control arc, Task C1, design spec section 5).

Cabal knows nothing about groups, Okta, or invites -- it stores and checks a
dumb set of caller-may-reach-target pairs. kobo is the policy owner: this
module is the pure computation that turns kobo's own ledger (agents' group
namespaces, accepted colleague invites) into that flat set. A later task
(C4) is the one that actually pushes this set to cabal on every change
(agent created/destroyed, invite accepted/revoked, group change); this
module only computes it, so it can be unit-tested against a session with no
switchboard involved at all.

Pair shape: `("agent:" + caller_name, target_name)` -- the caller side
carries the same `agent:<name>` prefix cabal's own caller identifiers
already use for agent-originated calls (see `kobo_api.colleagues`/cabal's
task envelope), while the target side is the bare agent name cabal's
dispatch routes key on.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx
from sqlalchemy.orm import Session

from kobo_api.clients import CabalClient, CabalError, cabal_enabled
from kobo_api.config import SHARED_GROUP, Config
from kobo_api.kinds import KIND_CONNECTED
from kobo_api.models import INVITE_ACCEPTED, A2AInvite, Agent, AgentStatus

if TYPE_CHECKING:
    # Type-only, same "keep the real Clients import light for callers that
    # don't need it" posture kobo_api.main takes with the identical import --
    # this module only ever touches `clients.cabal` (already typed via the
    # `CabalClient` import above), never anything else `Clients` carries.
    from kobo_api.clients import Clients

log = logging.getLogger(__name__)


def implicitly_paired(a_group: str | None, b_group: str | None) -> bool:
    """True when two agents' `group_name`s alone make them an implicit
    dispatch pair, no invite required -- THE one definition every call site
    that needs this question answered shares: `allowed_pairs`' own same-
    group loop below, and (fix round, shared-group wave HIGH) `kobo_api.
    main`'s `create_invite` (the "already in the same group" 422),
    `list_requestable_agents` (excluding already-auto-paired colleagues from
    the requestable directory), and `_team_pairs` (the auto-paired-
    colleagues list on the invites view). Before this predicate existed,
    those three main.py sites each re-implemented the raw "same non-null
    group_name" check inline, which is only HALF of `allowed_pairs`' actual
    rule -- they never learned about the `SHARED_GROUP` exception below, so
    two commons agents could never invite each other (a false "already
    talking freely" 422), vanished from each other's requestable list, and
    `_team_pairs` lied that they were already auto-paired when they were not.

    Same non-null `group_name` on both sides, AND that shared group is not
    `SHARED_GROUP`: the commons is humanly open (every signed-in caller may
    create an agent there, and every signed-in caller sees every agent
    there, `_visible_agents` in `kobo_api.main`), but that openness is about
    visibility, not wiring -- two agents sharing the commons namespace are
    no more implicitly connected to each other than two strangers are, so
    their wire stays invite-gated like any other cross-boundary pair. A
    group-less agent (`None` on either side) is never implicitly paired
    with anything, including another group-less agent -- "no group" is not
    itself a group.
    """
    return a_group is not None and a_group == b_group and a_group != SHARED_GROUP


def allowed_pairs(session: Session, config: Config) -> set[tuple[str, str]]:
    """Every `(caller, target)` pair currently allowed to talk, computed
    fresh from `session`'s current rows -- nothing cached, nothing carried
    over from a prior call, so a destroyed or renamed agent simply stops
    appearing next time this runs (no separate cleanup step needed anywhere
    else in this arc).

    Two sources, unioned:

    - Same-group, implicit, both directions: for every ordered pair `(a,
      b)` of READY agents sharing a non-null `group_name`, `(agent:a.name,
      b.name)` is in the set. A group-less agent (`group_name is None`)
      never produces a same-group pair with anything, including another
      group-less agent -- "no group" is not itself a group. `SHARED_GROUP`
      ("shared") is the one further exception (shared-group + group-
      requests wave): an agent namespaced into the commons produces NO
      implicit pair with anything either, including another commons agent
      -- commons agents are humanly open (every signed-in caller may create
      one, and every signed-in caller sees every one, `_visible_agents` in
      `kobo_api.main`), but their agent-to-agent connections stay invite-
      gated; the wire remains consent-based even where visibility isn't.
      An explicit `A2AInvite` (below) is still the only way two commons
      agents, or a commons agent and anything else, become a dispatch pair.
    - Accepted invites, directional: an `A2AInvite` row with status
      `accepted` adds exactly `(agent:from_agent, to_agent)`, never the
      reverse -- access granted one direction is not implicitly granted
      back the other way; the receiving agent's owner would need to accept
      a separate invite of its own to open that direction too. Both
      endpoints must currently be READY: an invite naming an agent that has
      since been destroyed (or never finished provisioning) drops out
      naturally here, the same "query current rows only" discipline the
      same-group half above already follows.

    A CONNECTED agent (connected-agents design 2026-09-21) is in neither
    source, as caller or as target: the room never wired it for colleague
    talk, so a pair naming it is a right nothing can exercise. An agent
    converted from composed loses its pairs here the next time this runs,
    which the convert route makes sure is immediately.

    `config` is accepted (not read) for the same reason `_visible_agents`
    takes both `session` and `request` even though not every branch touches
    both -- this is the seam a future policy input (e.g. a config-level
    kill switch) would extend without changing every call site's shape.
    """
    # Connected agents are excluded outright, which covers BOTH loops below
    # at once (the same-group loop reads this list; the invite loop reads
    # the names taken from it). The room holds no colleague wiring for a
    # connected agent -- its pipeline mints no colleague pass and the
    # switchboard has no inbound route to it -- so a pair naming one is a
    # dispatch right that can only ever fail, in whichever direction it
    # points. Spelled as `!= KIND_CONNECTED` rather than `== KIND_COMPOSED`
    # for `kobo_api.kinds`'s own reason: a later kind gets the composed
    # behaviour until it asks for its own.
    ready_agents = (
        session.query(Agent)
        .filter(Agent.status == AgentStatus.READY, Agent.kind != KIND_CONNECTED)
        .all()
    )
    ready_names = {agent.name for agent in ready_agents}

    pairs: set[tuple[str, str]] = set()

    for caller in ready_agents:
        for target in ready_agents:
            if target.name == caller.name:
                continue
            if implicitly_paired(caller.group_name, target.group_name):
                pairs.add((f"agent:{caller.name}", target.name))

    accepted_invites = (
        session.query(A2AInvite).filter(A2AInvite.status == INVITE_ACCEPTED).all()
    )
    for invite in accepted_invites:
        if invite.from_agent in ready_names and invite.to_agent in ready_names:
            pairs.add((f"agent:{invite.from_agent}", invite.to_agent))

    return pairs


def sync_dispatch_pairs(cabal: CabalClient, session: Session, config: Config) -> int:
    """Compute `allowed_pairs(session, config)` fresh and push it to the
    switchboard via `cabal.put_dispatch_pairs` (Task C4: the room becomes
    the switchboard's policy source, syncing on every consent change).
    Returns the pushed pair count (`put_dispatch_pairs`'s own return value)
    verbatim -- final-review fix round, HIGH-2: the operator resync route
    and the boot-time sync both want to report a count, and this is the one
    place that already has it.

    Raises whatever `put_dispatch_pairs` raises (`CabalError` on a non-2xx
    response) or a raw `httpx` transport error -- this function never
    tolerates a failure itself, mirroring `kobo_api.directory.sync_
    directory`'s identical raising contract for the analogous directory-sync
    problem. Every real trigger in this room (`kobo_api.main`'s invite
    accept/revoke, `kobo_api.destroy`'s forget step, `kobo_api.provisioner`'s
    READY flip) calls this only through `sync_dispatch_pairs_best_effort`
    below, never directly -- the operator resync route (`kobo_api.main`,
    HIGH-2b) is the one caller that DOES call this raising core directly,
    on purpose: an operator who explicitly asked for a resync needs to
    learn it failed, not have that swallowed the way an incidental trigger's
    own failure must be. This raising core stays a free-standing,
    independently testable function rather than folding the fail-soft
    wrapping into it, the same split `sync_directory`/`_sync_directory_
    best_effort` already established.
    """
    return cabal.put_dispatch_pairs(allowed_pairs(session, config))


def sync_dispatch_pairs_best_effort(
    clients: Clients, config: Config, session: Session
) -> int | None:
    """Fail-soft wrapper around `sync_dispatch_pairs` -- the ONE seam every
    pair-mutating trigger in this room shares: an invite's accept or an
    accepted invite's revoke (`kobo_api.main`), an agent's destroy-forget
    step completing (`kobo_api.destroy`), an agent's READY flip at the end
    of provisioning (`kobo_api.provisioner`), and now the boot-time sync
    (`kobo_api.main`'s `_boot_sync_dispatch_pairs`, final-review fix round,
    HIGH-2a). New READY agents join their group's implicit pairs through
    the same `allowed_pairs` query every other trigger recomputes from
    scratch; a group change has no trigger of its own because `group_name`
    is immutable once an agent is created (nothing in this room currently
    changes it after create).

    Lives here, next to `allowed_pairs`/`sync_dispatch_pairs` themselves,
    rather than in `kobo_api.main` alongside its structurally identical
    `_sync_directory_best_effort` -- `kobo_api.main` imports `kobo_api.
    destroy` and `kobo_api.provisioner` (for `run_destroy`/`run_pipeline`),
    so a helper defined in `main.py` could never be called back into from
    either of those two modules without a circular import. `kobo_api.pairs`
    sits underneath all three, so it's the one place every caller can reach
    without inverting that dependency direction. `kobo_api.main` still
    exposes its own thin, session-only `_sync_dispatch_pairs` wrapper around
    this function for its two call sites, closing over its own `app.state.
    clients`/`app.state.config` the way every other route-level cabal call
    already does.

    A no-op (returns `None`) when the switchboard isn't wired: `cabal_
    enabled(config)` false, or `clients.cabal` is `None` (both gates
    checked, same two-part test `kobo_api.main._colleagues_gate` already
    uses -- either alone isn't enough to safely dereference `clients.
    cabal`). When wired, any `CabalError` (a non-2xx response) or raw
    `httpx.HTTPError` (a transport failure -- connection refused, timeout,
    DNS) is logged and swallowed here, never raised (also returning `None`):
    by the time any of the triggers above calls this, the state change that
    triggered it has already committed, and a switchboard hiccup must not
    turn an otherwise-successful accept/revoke/destroy/ready-flip/boot into
    a failure this late. `CabalClient.put_dispatch_pairs`'s own `dispatch_
    pairs_dirty` flag is what actually marks a failure here for later
    reconciliation (HIGH-2b) -- this function never reads or clears it
    itself, since (per that flag's own docstring) every call here already
    pushes the full current `allowed_pairs` unconditionally, which is
    itself what heals a prior failure the next time any trigger fires.

    On success, returns the pushed pair count (`sync_dispatch_pairs`'s own
    return value) -- every one of the four original fire-and-forget
    triggers still ignores it, exactly as before; the boot-time sync and
    (indirectly, via the raising core instead) the operator resync route
    are the two callers that use it.
    """
    if not cabal_enabled(config) or clients.cabal is None:
        return None
    try:
        return sync_dispatch_pairs(clients.cabal, session, config)
    except (CabalError, httpx.HTTPError):
        log.warning("dispatch-pairs sync to the switchboard failed", exc_info=True)
        return None

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

from sqlalchemy.orm import Session

from kobo_api.config import Config
from kobo_api.models import INVITE_ACCEPTED, A2AInvite, Agent, AgentStatus


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
      group-less agent -- "no group" is not itself a group.
    - Accepted invites, directional: an `A2AInvite` row with status
      `accepted` adds exactly `(agent:from_agent, to_agent)`, never the
      reverse -- access granted one direction is not implicitly granted
      back the other way; the receiving agent's owner would need to accept
      a separate invite of its own to open that direction too. Both
      endpoints must currently be READY: an invite naming an agent that has
      since been destroyed (or never finished provisioning) drops out
      naturally here, the same "query current rows only" discipline the
      same-group half above already follows.

    `config` is accepted (not read) for the same reason `_visible_agents`
    takes both `session` and `request` even though not every branch touches
    both -- this is the seam a future policy input (e.g. a config-level
    kill switch) would extend without changing every call site's shape.
    """
    ready_agents = session.query(Agent).filter(Agent.status == AgentStatus.READY).all()
    ready_names = {agent.name for agent in ready_agents}

    pairs: set[tuple[str, str]] = set()

    for caller in ready_agents:
        if caller.group_name is None:
            continue
        for target in ready_agents:
            if target.name == caller.name:
                continue
            if target.group_name == caller.group_name:
                pairs.add((f"agent:{caller.name}", target.name))

    accepted_invites = (
        session.query(A2AInvite).filter(A2AInvite.status == INVITE_ACCEPTED).all()
    )
    for invite in accepted_invites:
        if invite.from_agent in ready_names and invite.to_agent in ready_names:
            pairs.add((f"agent:{invite.from_agent}", invite.to_agent))

    return pairs

"""The two kinds of agent this room holds, and how a connected one runs.

A `composed` agent is one the room built: it owns the repo, writes the
bundle into it, and keeps editing it. A `connected` agent is a team's own
repo with the team's own code in it; the room deploys it, gives it an
identity and model access, and never pushes to its branches.

The marker lives on the agent row (`Agent.kind`) rather than being
inferred from anything else on it, because four separate readers consult
it -- the pipeline selector, the destroy ledger, both read routes, and
the UI's tab model -- and every one of them has to agree. It is compared
against the constants below, never truth-tested, so a future kind gets
the composed behaviour until it asks for its own (the `archetype`
precedent in `kobo_api.composer`).

`run_mode` is recorded only for connected agents, since only they can
bring something that does not serve HTTP. It is the deploy platform's
workload class in disguise: `http` is the ordinary probed class, and
`headless` asks for one with no network probes at all, which a room only
offers once its deploy platform names the class (`Config.
connected_headless_class`; empty means the run mode is off).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, typing only
    from kobo_api.models import Agent

KIND_COMPOSED = "composed"
KIND_CONNECTED = "connected"

RUN_MODE_HTTP = "http"
RUN_MODE_HEADLESS = "headless"


def is_connected(agent: Agent) -> bool:
    """Whether this agent's repo belongs to its team rather than the room.

    The one question every reader actually asks, named once so none of
    them spells the comparison itself and none of them ever reaches for
    the falsiness of the column instead of its value.
    """
    return agent.kind == KIND_CONNECTED

"""Colleague pass delivery (colleagues switchboard plan, Task 3/B3): mints a
colleague-scoped switchboard token for an agent and delivers it to the
agent's own namespace as a Secret — the "every agent is born with a
colleague pass" feature, riding the exact same delivery rails
`kobo_api.slack_setup.wire_slack` already proved for delivering pasted
Slack tokens to an agent's namespace as a Secret.

One small function, mirroring `kobo_api.directory`'s own shape (a thin,
named wrapper the pipeline calls rather than reaching for `CabalClient`/
`KubectlClient` primitives directly) — kept in its own module rather than
folded into `kobo_api.directory` or `kobo_api.steps`, since minting and
delivering a secret is a distinct responsibility from either registering an
agent in the directory or running a provisioning step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kobo_api.clients import CabalClient, KubectlClient
from kobo_api.composer import CABAL_COLLEAGUE_TOKEN_ENV

if TYPE_CHECKING:
    from kobo_api.models import Agent


def deliver_pass(kubectl: KubectlClient, cabal: CabalClient, agent: Agent) -> None:
    """Mint a colleague-scoped switchboard token for `agent` and deliver it
    to the agent's own namespace as a Secret named `{agent.name}-cabal`
    (the same `{name}-<suffix>` secret-naming convention `wire_slack`'s own
    `{name}-slack` secret already established), carrying it under the
    `CABAL_COLLEAGUE_TOKEN` key — the literal env var name every composed
    agent's `bundle/agent.yaml` `tools.colleagues.token_env` points at
    (`kobo_api.composer._tools_block`), so the agent's own runtime
    (daedalus's `ColleaguesToolProvider`) can read it straight out of its
    process environment once the platform wires this Secret into its
    envFrom (`gojira.yaml`'s `secrets_from`, appended by
    `kobo_api.steps.WireStep`).

    Idempotent by construction, the same way `wire_slack`'s own secret
    delivery is: `apply_secret` upserts, so calling this again (a retried
    `WireStep`) never fails on an already-existing Secret of this name — it
    just re-mints a fresh token and overwrites it. A re-mint on every re-run
    is accepted (the plan's own call): the switchboard's mint endpoint has
    no "return the existing token" mode, and re-minting a fresh 365-day pass
    on a retry is harmless — any prior token an already-running agent
    process still holds in memory simply expires on its own schedule rather
    than being actively revoked.

    Raises `CabalError` (a `ClientError` subclass) on a minting failure, or
    `ClientError` on a secret-delivery failure — both left unwrapped for the
    caller (`WireStep`) to handle exactly like any other step failure. The
    minted token itself is never logged and never appears in any raised
    message: `CabalClient.mint_colleague`'s own docstring covers the mint
    side of that guarantee, and `KubectlClient.apply_secret`'s docstring
    covers the delivery side (the literal value reaches kubectl only via
    argv/stdin, redacted at debug level, never embedded in a raised
    exception).
    """
    token = cabal.mint_colleague(agent.name)
    kubectl.apply_secret(
        agent.name, f"{agent.name}-cabal", {CABAL_COLLEAGUE_TOKEN_ENV: token}
    )

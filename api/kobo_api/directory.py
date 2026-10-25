"""The directory seam (colleagues switchboard plan, Task 2): registers and
delists agents in the switchboard (cabal)'s directory.

Two small functions, shared by every caller that needs to touch the
directory (`kobo_api.steps.DirectoryStep`, `kobo_api.destroy`'s
`_destroy_directory` stage, the metadata PATCH/regenerate tails, the skill
and runtime-image routes, and the on-demand sync route in `kobo_api.main`) so none of them re-derives the same
"how do I read a card" or "how do I delist an agent" logic independently and
risks the copies drifting apart.

`sync_directory`'s card-fetch step is deliberately the same clone-then-read-
the-file idiom `kobo_api.org.load_org_config` (org.yaml) and
`kobo_api.metadata.commit_card_metadata` (agent-card.json's `metadata` block)
already use for a single file in an agent/org repo — there is no bespoke
single-file GitHub API read anywhere in this codebase to reuse instead, and
inventing one here would be a second way to do the same thing. The card is
read verbatim from the clone, never recomposed from `BundleSpec`/
`kobo_api.composer`: the switchboard must see exactly what the agent's own
runtime serves at `/.well-known/agent-card.json`, not kobo's memory of what
it once baked.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from kobo_api.clients import CabalClient, ClientError, GitHubClient
from kobo_api.config import Config

if TYPE_CHECKING:
    from kobo_api.models import Agent


def sync_directory(github: GitHubClient, cabal: CabalClient, cfg: Config, agent: Agent) -> None:
    """Register `agent` in the switchboard's directory.

    Clones `agent.repo_full_name`, reads its `bundle/agent-card.json`
    verbatim (see module docstring), derives the agent's in-cluster a2a
    endpoint from `cfg.agent_endpoint_template` (its literal `{name}`
    placeholder filled with `agent.name`), and calls `cabal.graduate` with
    the agent's own name doubling as the a2a audience.

    Raises on either failure — a clone/missing-card problem surfaces as
    `ClientError` (from `github.clone`), a non-2xx `graduate` response as
    `CabalError` (a `ClientError` subclass) — and never tolerates either
    itself: callers decide whether a failure here is retryable
    (`DirectoryStep` lets it fail the step) or best-effort (the metadata
    PATCH/regenerate tails, the destroy stage's disabled/unreachable note).

    A malformed `cfg.agent_endpoint_template` (missing its `{name}`
    placeholder, or carrying a positional `{0}`-style one) makes `str.format`
    raise `KeyError`/`IndexError` — neither is a `ClientError`, so left
    unwrapped it would 500 the on-demand sync route instead of the plain 502
    every other failure here gets. Wrapped into `ClientError` here so every
    caller's existing `ClientError` handling (the route's 502, DirectoryStep's
    retryable failure) covers this case too, with no separate branch needed.
    """
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(agent.repo_full_name, workdir)
        card = json.loads((workdir / "bundle" / "agent-card.json").read_text())
    register_card(cabal, cfg, agent, card)


def sync_directory_from_path(cabal: CabalClient, cfg: Config, agent: Agent, bundle_dir: Path) -> None:
    """`sync_directory` for a directory-delivered agent (issue #207): the card
    is read from the composed folder on disk, not from a repository."""
    try:
        card = json.loads((bundle_dir / "agent-card.json").read_text())
    except (OSError, ValueError) as exc:
        raise ClientError(f"no readable agent card at {bundle_dir}: {exc}") from exc
    register_card(cabal, cfg, agent, card)


def register_card(cabal: CabalClient, cfg: Config, agent: Agent, card: dict) -> None:
    """The registration itself: the endpoint from `agent_endpoint_template`,
    the agent's name as its a2a audience, the card verbatim."""
    try:
        endpoint = cfg.agent_endpoint_template.format(name=agent.name)
    except (KeyError, IndexError) as exc:
        raise ClientError(
            f"agent endpoint template is malformed: {cfg.agent_endpoint_template!r}"
        ) from exc
    cabal.graduate(agent.name, endpoint=endpoint, audience=agent.name, card=card)


def retire_directory(cabal: CabalClient, agent: Agent) -> None:
    """Delist `agent` from the switchboard's directory.

    A thin wrapper around `CabalClient.absorb` — which already tolerates a
    404 (already delisted: a repeat destroy, or an agent the switchboard
    never registered) silently — kept as its own function purely so every
    caller (the destroy stage today, a future one tomorrow) names the
    directory concept, not `CabalClient`'s own method name.
    """
    cabal.absorb(agent.name)

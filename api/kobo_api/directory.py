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
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from kobo_api.clients import CabalClient, ClientError, GitHubClient, KubectlClient
from kobo_api.config import Config

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from kobo_api.models import Agent

log = logging.getLogger(__name__)

# What `graduate` registers: the switchboard files a graduated agent as a
# federated row, and the room is the only thing that graduates with an
# endpoint shaped by `agent_endpoint_template`. Together they are the marker
# `sweep_directory` reads back to tell the room's own entries from ones
# registered by hand (which it must never touch).
ROOM_ENTRY_KIND = "federated"


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


def room_registered(cfg: Config, entry: dict) -> bool:
    """Whether the switchboard row `entry` is one this room registered: a
    federated row whose endpoint is exactly what `register_card` would have
    sent for that name. A bespoke agent graduated by hand has another
    endpoint (or none) and reads False, so the sweep leaves it alone. A
    malformed template makes every entry read as not the room's, which is
    the safe answer."""
    if entry.get("kind") != ROOM_ENTRY_KIND:
        return False
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        return False
    try:
        expected = cfg.agent_endpoint_template.format(name=name)
    except (KeyError, IndexError):
        return False
    return bool(expected) and entry.get("endpoint") == expected


CLUSTER_SILENT_NOTE = (
    "The cluster could not be asked which agents run, so nothing was retired."
)


def sweep_directory(
    cabal: CabalClient,
    cfg: Config,
    session: Session,
    *,
    kubectl: KubectlClient | None = None,
    dry_run: bool = True,
) -> dict:
    """Reconcile the switchboard's directory with the room's agents (issue
    #274). Every active, non-session entry lands in one list:

    - `kept`: an agent row of that name exists in any state (a row exists
      until the destroy ledger forgets it, so destroying and failed agents
      count as existing, and so do connected ones).
    - `live`: no row, but a Deployment `<name>/<name>` runs in the cluster.
      A hand-built agent deployed the way the room deploys its own, with
      the same endpoint shape, is indistinguishable from a ghost by its
      directory entry alone; the cluster is what tells them apart. Never
      retired.
    - `retired`: no row, no Deployment, AND the room's own registration
      marker (`room_registered`). On a dry run (the default, and what the
      nightly pass runs) nothing is called; a real run absorbs each one and
      reports the ones the switchboard refused under `failed`.
    - `unknown`: everything else without a row (registered by hand or by
      another room). Listed, never touched.

    `kubectl` None, or a cluster that cannot be asked (`ClientError` from
    `deployment_exists`), retires nothing: every entry without a row goes to
    `unknown` and `note` says why (`CLUSTER_SILENT_NOTE`). The probe runs
    before any absorb, so a cluster that goes silent halfway cannot leave a
    half-swept directory.

    Raises `CabalError` when the switchboard cannot be read at all; the
    caller decides what that means (the route answers 502, the nightly pass
    logs and moves on). The model import is local because
    `kobo_api.clients` imports `kobo_api.learnings`, which will import this
    module; `kobo_api.models` must not be pulled in at import time here.
    """
    from kobo_api.models import Agent

    entries = cabal.list_agents()
    existing = {name for (name,) in session.query(Agent.name).all()}
    kept: list[str] = []
    live: list[str] = []
    unknown: list[str] = []
    candidates: list[str] = []
    for entry in entries:
        if entry.get("kind") == "session" or entry.get("status") != "active":
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name in existing:
            kept.append(name)
        elif room_registered(cfg, entry):
            candidates.append(name)
        else:
            unknown.append(name)

    note: str | None = None
    running: dict[str, bool] = {}
    if kubectl is None:
        note = CLUSTER_SILENT_NOTE
    else:
        try:
            running = {name: kubectl.deployment_exists(name, name) for name in candidates}
        except Exception:
            log.warning("directory sweep: the cluster could not be asked", exc_info=True)
            note = CLUSTER_SILENT_NOTE
    if note is not None:
        unknown.extend(candidates)
        candidates = []

    retired: list[str] = []
    failed: list[str] = []
    for name in candidates:
        if running.get(name):
            live.append(name)
            continue
        if not dry_run:
            try:
                cabal.absorb(name)
            except Exception:
                log.warning("directory sweep: could not retire %s", name, exc_info=True)
                failed.append(name)
                continue
        retired.append(name)
    return {
        "dry_run": dry_run,
        "retired": retired,
        "kept": kept,
        "live": live,
        "unknown": unknown,
        "failed": failed,
        "note": note,
    }

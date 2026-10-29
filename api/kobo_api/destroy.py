"""Agent destruction: the resumable teardown ledger, the mirror of
`kobo_api.provisioner`.

Destruction is a step ledger, not a transaction (design doc,
docs/superpowers/specs/2026-08-17-agent-destruction-design.md,
docs/superpowers/specs/2026-08-17-destruction-completeness-design.md,
docs/superpowers/specs/2026-08-18-colleagues-switchboard-design.md): six
ordered steps, each tolerant of "already gone" (a repeat delete, or a
resume after a prior attempt got partway), writing rows into the SAME
`agent_steps` table provisioning uses (named per `DESTROY_STEPS`) so the
UI's existing checklist machinery reads them for free. Any step failure
marks the run failed at that step with a plain reason (never an em dash —
these land in `AgentStep.error`/`Agent.error`, both user-facing); a later
`run_destroy` call resumes from the first incomplete step, skipping
whatever is already DONE.

Order is load-bearing, same as `provisioner.PIPELINE`: platform teardown
kills the running workload first (the agent visibly stops existing);
model access (`destroy-access`) comes right after, taking back the GSA's
group membership and the GSA itself while the row is still fully
addressable; Slack, the repo, and the directory listing are paper; the
agent's own row goes last (`destroy-forget`) so a failed run always leaves
a resumable record — the row never disappears until every step before it
is each done or proven already-gone. `destroy-directory` (colleagues
switchboard plan, Task 2) is the one exception to "failure marks the run
failed": an unconfigured or unreachable switchboard completes that step
with a note instead (see its own docstring) rather than blocking the rest
of the ledger.

Unlike provisioning, `destroy-forget`'s job (deleting the agent's step rows
and the agent row itself) is terminal in the most literal sense: once it
succeeds there is no row left for anything to update. `_run_destroy_body`'s
loop special-cases this — see its comment at the `destroy-forget` branch.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import or_
from sqlalchemy.orm import Session, sessionmaker

from kobo_api.clients import ClientError, cabal_enabled
from kobo_api.config import Config
from kobo_api.directory import retire_directory
from kobo_api.kinds import KIND_COMPOSED, is_connected, is_directory
from kobo_api.models import (
    A2AInvite,
    Agent,
    AgentStatus,
    AgentStep,
    RepoAccess,
    StepStatus,
)
from kobo_api.pairs import sync_dispatch_pairs_best_effort
from kobo_api.provisioner import _get_or_create_step, _Heartbeat
from kobo_api.steps import refresh_colleague_rosters

if TYPE_CHECKING:
    from kobo_api.clients import Clients

log = logging.getLogger(__name__)

# Order is load-bearing (see module docstring). Named distinctly from
# provisioner.PIPELINE's step names ("repo", "onboard", ...) so the two
# ledgers can never collide inside the same agent_steps table.
#
# "destroy-access" sits right after "destroy-platform": platform teardown
# stops the workload first, then the GSA it ran as is torn back down while
# the agent's row is still fully addressable. It runs before "destroy-slack"
# rather than at the very end, mirroring provisioning's own ordering
# (AccessStep runs early there too) and keeping the GCP identity cleanup
# next to the platform step it is the mirror image of.
# "destroy-directory" (colleagues switchboard plan, Task 2) sits right
# before "destroy-forget": every other step above still addresses a fully
# live agent row, and delisting is cheap/idempotent (CabalClient.absorb
# already tolerates "already delisted"), so there's no reason to rush it
# earlier — but it must run before destroy-forget deletes the row this
# step still reads (agent.name).
# "destroy-secrets" (secrets and environment design, 2026-09-22) runs
# FIRST, before the platform teardown, and that order is the point of it.
# Removing an ExternalSecret is a write into the agent's own Kubernetes
# namespace, and "destroy-platform" is the step that deletes that
# namespace: run after it, every removal would be aimed at a namespace
# that is no longer there. Going first also means the credentials are the
# first thing to stop existing, which is the right order for a teardown
# whose whole point is that a destroyed agent leaves no live secret
# behind. The cost is one moment where a still-running pod has lost its
# Secrets; it is already being destroyed, and a pod that cannot read a
# credential it is about to lose is not a regression.
#
# It closes the gap the secrets platform integration documented in so many
# words -- secrets written through the platform were not removed when the
# agent was destroyed -- which left a live credential at a path nobody was
# watching.
DESTROY_STEPS = (
    "destroy-secrets",
    "destroy-platform",
    "destroy-access",
    "destroy-slack",
    "destroy-repo",
    "destroy-directory",
    "destroy-forget",
)

# The UI checklist vocabulary for destroy steps (mirrors provisioner.
# STEP_LABELS) — kept here, next to DESTROY_STEPS, rather than in main.py,
# so the two can never drift apart.
# The directory backend's teardown (issue #207): the folder, the listing,
# the row. Never a namespace, a repository or a cloud identity: this kind
# has none, and a stage that asked anyway would be a stage that could
# delete something that was never the room's.
DIRECTORY_DESTROY_STEPS = (
    "destroy-files",
    "destroy-directory",
    "destroy-forget",
)


def destroy_steps_for(agent: Agent) -> tuple[str, ...]:
    """The stage list this agent's kind runs, chosen in one place."""
    return DIRECTORY_DESTROY_STEPS if is_directory(agent) else DESTROY_STEPS


DESTROY_STEP_LABELS: dict[str, str] = {
    "destroy-files": "Agent folder",
    "destroy-platform": "Deployment teardown",
    "destroy-secrets": "Stored secrets",
    "destroy-access": "Model access",
    "destroy-slack": "Slack app",
    "destroy-repo": "Repository archive",
    "destroy-directory": "Directory listing",
    "destroy-forget": "Room record",
}

# Every purpose the room writes under `apps/<agent>/`: the colleague pass,
# the pasted Slack tokens, and whatever an owner added in the Secrets and
# environment panel. Each one has a Kubernetes Secret named after it
# (`<agent>-cabal`, `<agent>-slack`, `<agent>-env`), which is what
# `secrets_from` in the agent's gojira.yaml names and what the destroy
# step below takes away with the path.
SECRET_PURPOSES = ("cabal", "slack", "env")


# How much older than the agent's row its namespace may be and still count as
# the one the deploy platform made for it. The row's clock (the room's pod)
# and the namespace's (the cluster's API server) are different clocks.
NAMESPACE_CLOCK_SKEW = timedelta(minutes=2)


@dataclass
class DestroyCtx:
    agent: Agent
    session: Session
    config: Config
    clients: Clients
    # Who asked for this destruction: the verified caller on the DELETE
    # that started it, threaded down from the route (secrets and
    # environment design, 2026-09-22). Only "destroy-secrets" reads it, and
    # it reads it because the secrets platform records every removal
    # against a person and the person who pressed destroy IS that person.
    # None for a resume with no request behind it, or a room with no
    # identity perimeter, where the agent's own creator stands in.
    acting_email: str | None = None


def _require_own_namespace(ctx: DestroyCtx) -> None:
    """Raise unless the namespace named after this agent is absent or no
    older than the agent itself (issue #193).

    An agent's name is its namespace, and the deploy platform adopts a
    namespace that is already there, labelling it as its own. So neither
    gojira's labels nor anything kobo records can say whose a namespace is;
    its creation time can. The pipeline runs after the row is written, so a
    namespace made for this agent is younger than the row. One that is
    older belonged to something else first (a platform service, another
    team), and deleting it, or the secrets and identity stored under the
    same name, would destroy what the room never owned. Create and connect
    now refuse a name whose namespace exists; this guards agents made
    before that. It does not catch a namespace someone made in the minutes
    between the create check and the row (`NAMESPACE_CLOCK_SKEW`).

    Runs at the start of destroy-secrets (whatever the secrets platform's
    state), before the platform teardown, and before destroy-access. The GSA
    `agent-<name>` is the room's naming, but an agent whose onboard failed
    never had one made for it, and the same id can belong to an earlier
    install of the room in that project or to a connected repository that
    already deploys as `agent-<name>`. A refusal raises `RuntimeError`; a
    cluster the room cannot ask raises `ClientError`: unknown is not safe.
    """
    agent = ctx.agent
    # `Clients.real` always wires kubectl; only a test's wiring leaves it out.
    if ctx.clients.kubectl is None:
        return
    created = ctx.clients.kubectl.namespace_created_at(agent.name)
    if created is None:
        return
    born = agent.created_at
    if born.tzinfo is not None:
        born = born.astimezone(UTC).replace(tzinfo=None)
    if created < born - NAMESPACE_CLOCK_SKEW:
        raise RuntimeError(
            f"the namespace {agent.name} is older than this agent, so the room "
            "did not make it. Nothing was deleted; an operator needs to check "
            "whose it is"
        )


def _namespace_refusal(ctx: DestroyCtx) -> str | None:
    """destroy-secrets' reading of `_require_own_namespace`: None when the
    namespace is the agent's (or absent), else the failure to record. The
    reason is logged; the failure label is what lands on the step."""
    try:
        _require_own_namespace(ctx)
    except ClientError as exc:
        # Before RuntimeError: ClientError is one, and an unreachable cluster
        # is not a namespace that belongs to someone else.
        log.warning(
            "destroy-secrets: could not check the namespace of agent %s (%s)",
            ctx.agent.name,
            type(exc).__name__,
        )
        return "stored secrets (could not check the namespace)"
    except RuntimeError as exc:
        log.warning("destroy-secrets: agent %s: %s", ctx.agent.name, exc)
        return "stored secrets (namespace not ours)"
    return None


def _destroy_platform(ctx: DestroyCtx) -> str:
    """No `gojira_app_id` recorded means this agent never got that far —
    nothing to tear down. Otherwise `GojiraClient.delete_app` already
    collapses "the platform never heard of this app id" (a 404) into
    `{"already_gone": True}` (kobo_api.clients); anything else is the
    platform's own 202 teardown summary, JSON-dumped for the record.

    Final-review Finding B: `identity.failures` in that summary is
    gojira's own best-effort report of partial GCP teardown it could not
    finish (a leaked GSA, binding, or AR image) - unretryable once
    gojira's rows are gone, since there's no app id left to re-delete.
    The step still succeeds either way (best-effort is gojira's own
    contract, not this step's to enforce), but a non-empty list is logged
    as a warning here so it's visible in the room's logs rather than only
    reachable by parsing this step's JSON-dumped detail.
    """
    agent = ctx.agent
    if agent.gojira_app_id is None:
        return "no deployment to tear down"

    _require_own_namespace(ctx)
    result = ctx.clients.gojira.delete_app(agent.gojira_app_id)
    if result.get("already_gone"):
        return "already gone"

    failures = result.get("identity", {}).get("failures")
    if failures:
        log.warning(
            "destroy-platform: agent %s left %d GCP identity teardown "
            "failure(s) behind: %s",
            agent.name,
            len(failures),
            "; ".join(failures),
        )
    return json.dumps(result)


def _destroy_secrets(ctx: DestroyCtx) -> str:
    """Remove everything this agent's secrets are held in on the secrets
    platform: for each purpose, the cluster object that delivers it and
    then the path that holds it (secrets and environment design,
    2026-09-22).

    Three purposes, because the room writes three: `cabal` (the colleague
    pass), `slack` (the tokens an owner pasted) and `env` (whatever they
    added in the panel). Then, once all three are gone, the agent's
    identity on the platform (`SeroClient.offboard`): its store, service
    account, role and policy. Named as a fixed list rather than discovered,
    for the reason `destroy-access` gives about the GSA: nothing on the
    agent's row records which of them were ever written, and asking about
    a path that was never there costs one call and answers cleanly.

    Order is load-bearing WITHIN each purpose. The ExternalSecret goes
    first: deleting the path underneath a live one leaves the cluster
    reconciling a path that is gone on purpose, an error in the operator's
    log every interval, and the Kubernetes Secret it owns still sitting
    there. Deleting the object first takes that Secret with it, which is
    the whole point of the step.

    All three are ATTEMPTED whichever ones fail, the same way
    `_destroy_platform` reads gojira's whole failure list rather than
    stopping at the first: a lane that refuses one path is no reason to
    leave the other two live. What failed is collected and raised at the
    end, naming what DID go, so the ledger's error says how far the run
    got and a resume does not have to guess.

    ANY exception counts, not only `ClientError`. A connection reset
    reaching this loop as something else entirely would otherwise abort
    the two purposes after it, which is the failure mode this collection
    exists to prevent -- and "the lane raised something unexpected" is
    exactly when leaving a credential behind matters most.

    Idempotent end to end, by the lane's own contract: an object already
    gone and a path already gone are both successes, so a retried destroy
    does not fail on work a prior attempt finished.

    Two ways this completes with a note rather than doing anything. A room
    with no secrets platform configured never wrote through it, so there
    is nothing there to remove and nothing to say beyond that. An agent
    with nobody on record has no person for the platform to record the
    removal against; the room would rather leave the paths and say so than
    delete credentials under no one's name, and an operator can remove
    them with their own identity. Neither is a failure, for the reason
    `destroy-directory` gives: holding an otherwise-clean destroy hostage
    to something an operator cannot fix mid-run leaves a fully
    teardown-able agent stuck.

    Before any of that, the namespace check (issue #193, `_namespace_refusal`)
    runs in every case. A namespace older than the agent, or a cluster the
    room cannot ask, keeps the stored secrets, still revokes the pass, and
    fails the step, so the ledger stops here for an operator to look.
    """
    agent = ctx.agent
    sero = ctx.clients.sero
    removed: list[str] = []
    failures: list[str] = []
    note: str | None = None

    # Checked first, whatever the branches below find (issue #193): a refusal
    # keeps the stored secrets, still revokes the room's own pass, and fails
    # the step, so no later stage deletes anything under this name.
    refusal = _namespace_refusal(ctx)
    if refusal is not None:
        failures.append(refusal)

    # The operator who pressed destroy, and only then the agent's creator.
    # The platform records this name against the removal, so the person who
    # actually asked for it is the honest answer; `created_by` is the
    # fallback for a resume with no request behind it (the ledger retries
    # itself) and for a room with no identity perimeter.
    acting_email = (ctx.acting_email or agent.created_by or "").strip()
    if sero is None:
        note = "no secrets service configured; nothing was stored through one"
    elif not acting_email:
        note = (
            "no person on record to remove these on behalf of; the stored "
            "secrets stay until an operator removes them"
        )
    elif refusal is None:
        removed, failures = _remove_stored_secrets(ctx, sero, acting_email)

    # The colleague pass, whatever the secrets platform's state: it is the
    # one credential a destroy could not take back (destroy-orphans audit,
    # 2026-09-21), and a destroyed agent's pass still cleared the
    # switchboard. The switchboard now refuses every token the subject was
    # issued before now, and this is where the room asks for that. Strict,
    # like every purpose above: a pass left live is a credential left live.
    if cabal_enabled(ctx.config) and ctx.clients.cabal is not None:
        try:
            ctx.clients.cabal.revoke_colleague(agent.name)
        except Exception as exc:  # noqa: BLE001 - same collection rule as the purposes
            log.warning(
                "destroy-secrets: could not revoke the passes of agent %s (%s)",
                agent.name,
                type(exc).__name__,
            )
            failures.append("passes")
        else:
            removed.append("passes")

    if failures:
        # Raised, not noted: unlike the directory listing, a secret left
        # behind is a live credential, and a step that reported success
        # over one would be the exact gap this step exists to close. The
        # message says what DID go so a resume reads as progress rather
        # than as a run starting over.
        raise RuntimeError(
            f"could not remove {', '.join(failures)}"
            + (f" (removed: {', '.join(removed)})" if removed else "")
        )
    if note is not None and not removed:
        return note
    if note is not None:
        return f"{note}; removed: {', '.join(removed)}"
    return f"removed: {', '.join(removed)}"


def _remove_stored_secrets(ctx: DestroyCtx, sero, acting_email: str) -> tuple[list[str], list[str]]:
    """The secrets-platform half of `_destroy_secrets`: the three purposes,
    then the identity. Returns what went and what did not."""
    agent = ctx.agent
    removed: list[str] = []
    failures: list[str] = []
    for purpose in SECRET_PURPOSES:
        path = f"apps/{agent.name}/{purpose}"
        try:
            sero.unsync(acting_email, agent.name, agent.name, f"{agent.name}-{purpose}")
            sero.delete_secret(acting_email, path)
        except Exception as exc:  # noqa: BLE001 - one purpose failing is not three
            # The exception's CLASS and nothing else, the same rule the
            # secrets routes hold: the platform's own sentence can quote
            # the request it refused, and a destroy step's error string
            # lands on `AgentStep.error`, which any viewer of the agent
            # can read.
            log.warning(
                "destroy-secrets: could not remove %s for agent %s (%s)",
                purpose,
                agent.name,
                type(exc).__name__,
            )
            failures.append(purpose)
            continue
        removed.append(purpose)

    if not failures:
        # The identity last, and only once every object that read through
        # it is gone: the platform refuses to take a store down while an
        # ExternalSecret still points at it (the right refusal; the
        # alternative is a dangling object made on purpose), so the three
        # purposes above are what make this call answerable. What goes:
        # the ClusterSecretStore, the service account, the JWT role and
        # the read-only policy the platform made when this room's first
        # sync onboarded the agent -- the four objects a destroy used to
        # leave behind (registered 2026-09-24, closed here).
        try:
            sero.offboard(acting_email, agent.name, agent.name)
        except Exception as exc:  # noqa: BLE001 - same collection rule as the purposes above
            log.warning(
                "destroy-secrets: could not remove the identity of agent %s (%s)",
                agent.name,
                type(exc).__name__,
            )
            failures.append("identity")
        else:
            removed.append("identity")
    return removed, failures


def _destroy_access(ctx: DestroyCtx) -> str:
    """Tear back down what `AccessStep` (kobo_api.steps) granted: the
    agent's GSA's model-group membership, then the GSA itself.

    Unlike the other destroy steps, there is no `agent.<field>` recorded to
    tell "never provisioned" apart from "provisioned, now gone" - `AccessStep`
    doesn't stamp anything back onto `Agent` the way `gojira_app_id`/
    `slack_app_id`/`repo_full_name` do. So this step always runs, and relies
    entirely on `remove_from_group`/`delete_gsa`'s own idempotent
    already-gone signals (both `False`) to cover an agent that never made it
    past this point in provisioning, or a resume after a prior teardown
    attempt already took one or both away.

    The email is derived via `CloudClient.gsa_email_for`, never recomposed
    by hand here - the exact same `f"agent-{agent.name}"` id `AccessStep`
    passed to `ensure_gsa`, run back through the identical `gsa_email` +
    project composition (`gsa_email_for` delegates to it on both the real
    `GcloudClient` and `FakeCloud`), so the two sides cannot drift apart.

    Membership is removed BEFORE the GSA is deleted: once the GSA itself is
    gone, its membership becomes a ghost entry no longer reachable by email,
    unremovable by this step (or anything else) afterward.
    """
    _require_own_namespace(ctx)
    agent = ctx.agent
    config = ctx.config
    cloud = ctx.clients.cloud

    email = cloud.gsa_email_for(f"agent-{agent.name}")
    if config.model_group:
        removed = cloud.remove_from_group(email, config.model_group)
        what = "membership"
    else:
        removed = cloud.revoke_model_access(email, config.vertex_project)
        what = "model access"
    gone = cloud.delete_gsa(email)
    return f"{what} removed: {removed}, gsa deleted: {gone}"


def _destroy_slack(ctx: DestroyCtx) -> str:
    """No `slack_app_id` recorded means Slack was never set up for this
    agent. Otherwise, provenance decides who is allowed to delete it
    (`agent.slack_app_self_service`, Task 1): a self-service app was
    created directly by its user at Slack's own app site, never by kobo's
    own retired config-token flow — this room has no token for it and no
    business deleting it. That case never calls `delete_app` at all; the
    step simply completes with a fixed provenance note, returned exactly
    like every other step's detail string so it reaches the ledger's logs
    the same way (`_run_destroy_body`'s own `log.info` of this return
    value — nothing logged here).

    Otherwise (a legacy, room-created app) this is byte-identical to
    before: `SlackClient.delete_app` already collapses Slack's own "app
    not found"/"invalid app id" errors into a plain `False` — the
    already-gone signal — never raising for them (kobo_api.clients);
    `True` means Slack genuinely deleted it just now.
    """
    agent = ctx.agent
    if agent.slack_app_id is None:
        return "no slack app to remove"

    if agent.slack_app_self_service:
        return "The Slack app belongs to its creator. Remove it in Slack."

    deleted = ctx.clients.slack.delete_app(agent.slack_app_id)
    if deleted:
        return "slack app deleted"
    return "already gone"


def _is_repo_not_found(exc: ClientError) -> bool:
    """Whether `exc` is `GhCLI.rename_repo`/`archive_repo`'s own 404 rewrap
    (kobo_api.clients: both raise `ClientError(f"repo not found: {repo}")`
    on a real 404) — matched on that stable message prefix rather than
    reaching into `.stderr`, since the rewrap constructs a fresh
    `ClientError` carrying no `.stderr` of its own.
    """
    return str(exc).startswith("repo not found: ")


def _is_already_archived(exc: ClientError) -> bool:
    """Whether `exc` is GitHub's real "you tried to archive an
    already-archived repo" answer: a 403 whose body reads "Repository was
    archived so is read-only." (trailing period, confirmed live).

    Detected as a case-insensitive "archived" substring on a 403-class
    message (the `"(HTTP 403)"` marker `gh` appends, same stable-marker
    idiom as `kobo_api.clients._gh_returned_404`'s own `"(HTTP 404)"`
    check) — deliberately never exact-string equality, so GitHub rewording
    its own message text (or a test fixture spelling it slightly
    differently) still matches. `GhCLI.archive_repo` itself does not
    special-case this shape (see its docstring) — recognizing it is this
    step's own job.
    """
    message = str(exc)
    return "(HTTP 403)" in message and "archived" in message.lower()


def _destroy_repo(ctx: DestroyCtx) -> str:
    """Rename the agent's repo to `archived-agent-<name>-<row_id>`, then
    archive it under that name — both idempotent against a resume from any
    point in a prior partial attempt.

    No `repo_full_name` recorded means this agent never got a repo created
    — nothing to archive.

    `gh` renders a permission-denied PATCH as a literal 404, byte-identical
    to a missing repo (grounded live, review carry-forward) — so a bare 404
    on the rename is never trusted as "already gone" on its own. It's
    corroborated against the archive target name: if that name exists, a
    prior attempt already renamed the repo away and this step proceeds
    straight to archiving it; if NEITHER name exists, this is a genuine
    failure (a real permission problem, or the repo is simply gone) and the
    step fails with a plain reason rather than guessing success.

    The archive call tolerates the one already-gone shape that's actually
    its own to recognize: a 403 because the repo is already archived (see
    `_is_already_archived`) — any other archive failure (including a 404,
    which would be a genuinely unexpected state right after a successful
    rename or a corroborated existence check) propagates as a real failure.

    A connected agent's repository is its team's, and the room has never
    written a line to it (connected-agents design 2026-09-21) — so this
    step does nothing at all there. Unlike destroy-slack, which no-ops on
    its own recorded-field check, this one would NOT neutralise itself:
    `repo_full_name` is set for a connected agent too, and the rename below
    would take somebody else's repository away from them.

    The check here asks for an EXPLICIT `composed`, rather than asking
    `is_connected` the way every other reader of the kind does. Those
    readers fail open to composed, which is the harmless direction for
    them; here it is the destructive one. So a kind this code does not
    recognise — a future one, a bad write, a half-applied migration —
    leaves the repository alone rather than archiving it on a guess. The
    room can always archive a repository later; it cannot give one back.
    """
    agent = ctx.agent
    github = ctx.clients.github

    if agent.kind != KIND_COMPOSED:
        return "the repository belongs to its team and is left as it is"

    if agent.repo_full_name is None:
        return "no repo to archive"

    owner = agent.repo_full_name.rsplit("/", 1)[0]
    target_name = f"archived-agent-{agent.name}-{agent.id}"
    target_repo = f"{owner}/{target_name}"

    try:
        github.rename_repo(agent.repo_full_name, target_name)
    except ClientError as exc:
        if not _is_repo_not_found(exc):
            raise
        if not github.repo_exists(target_repo):
            raise RuntimeError(
                "Could not find the repo under its old or archived name. "
                "Check the room's repo access."
            ) from exc
        # Corroborated: a prior attempt already renamed it away.

    try:
        github.archive_repo(target_repo)
    except ClientError as exc:
        if not _is_already_archived(exc):
            raise
        # Already archived by a prior attempt.

    return f"archived as {target_repo}"


def _destroy_directory(ctx: DestroyCtx) -> str:
    """Delist the agent from the switchboard's directory
    (`kobo_api.directory.retire_directory`, absorb, 404-tolerant).

    Modeled on `_destroy_slack`'s shape, including its done-with-note
    pattern: there's no `agent.<field>` recorded to tell "never registered"
    apart from "registered, now gone" (directory membership, like
    `destroy-access`'s GSA group membership, isn't something kobo's own row
    tracks), so this always attempts the retire when the switchboard is
    configured. Unlike every step above, though, a failure here does NOT
    propagate to FAILED: an unconfigured OR unreachable switchboard both
    complete this step with the exact same plain note instead — the
    directory listing is a nice-to-have, and holding an otherwise-clean
    destroy run hostage to it would leave a fully-teardown-able agent
    permanently stuck for something the operator has no way to fix
    mid-destroy. A later re-registration (`POST .../directory/sync`, or the
    switchboard's own recovery) resolves it independently of this ledger.

    A connected agent is the one case where "nothing to retire" IS knowable
    from the row (connected-agents design 2026-09-21), so the switchboard
    is not asked. Two ways an agent gets here and the note has to be true
    of both: one connected from the start never had an entry (its pipeline
    has no directory step), and one converted from composed had its entry
    retired at the moment it was converted. Hence the note below says there
    is nothing to retire rather than that the agent was never listed, which
    would be false for the converted half. Asking anyway would do no harm
    -- absorb is 404-tolerant -- but it would put a switchboard call on the
    ledger for an agent that has no business with it.
    """
    if is_connected(ctx.agent):
        return "no directory entry to retire"

    if not cabal_enabled(ctx.config):
        return "The directory entry stays until the switchboard returns."

    try:
        # Broad on purpose ("unreachable" covers more than ClientError: a
        # raw connection failure never reaches CabalClient's own
        # CabalError, which only ever wraps a response that DID come back).
        retire_directory(ctx.clients.cabal, ctx.agent)
    except Exception:
        log.warning(
            "destroy-directory: could not reach the switchboard for agent %s",
            ctx.agent.name,
            exc_info=True,
        )
        return "The directory entry stays until the switchboard returns."

    return "removed from the directory"


def _destroy_forget(ctx: DestroyCtx) -> str:
    """Delete every one of this agent's step rows (including this very
    step's own, already committed as RUNNING by the caller), the colleague
    invites naming it in either direction, and the agent row itself, all in
    one transaction. Terminal: nothing survives this call to mark DONE
    afterward. See `_run_destroy_body`'s `destroy-forget` special case.
    """
    session = ctx.session
    agent = ctx.agent
    # Colleague invites are rows this step owns too (cleanup arc): destroy
    # used to leave them behind, so a destroyed agent kept appearing in
    # live agents' invite lists. Both directions, matched on the agent's
    # NAME, which is what the invite columns store.
    session.query(A2AInvite).filter(
        or_(A2AInvite.from_agent == agent.name, A2AInvite.to_agent == agent.name)
    ).delete(synchronize_session=False)
    # Repo-access rows die with the agent (agent PR broker): they are keyed
    # by NAME, so leaving them would hand a future agent that happens to
    # reuse this name every repository this one was ever approved for --
    # the broker would mint for the new agent on the old grants. The audit
    # trail's home is the broker's own record, never a row that can turn
    # back into a live grant.
    session.query(RepoAccess).filter_by(agent_name=agent.name).delete(
        synchronize_session=False
    )
    session.query(AgentStep).filter_by(agent_id=agent.id).delete()
    session.delete(agent)
    session.commit()
    # The permission service learns the agent is gone the same way it learned
    # it existed: the room pushes what remains. A failure is logged, never
    # fatal here; the next push carries the same fact.
    bab = getattr(getattr(ctx, "clients", None), "bab", None)
    if bab is not None:
        from kobo_api.bab import ROOM_ACTOR, push_topology

        push_topology(bab, session.query(Agent).all(), ctx.acting_email or ROOM_ACTOR)
    return "forgotten"


def _destroy_files(ctx: DestroyCtx) -> str:
    """Remove `<directory_root>/<name>/` (issue #207). The folder is the
    room's by construction: create refused a name whose folder already
    existed, so one that exists now was composed by this room for this row.
    Containment is still checked before the rmtree; a folder that is not
    directly under the root is never touched."""
    if not ctx.config.directory_root:
        return "no directory root configured; nothing to remove"
    root = Path(ctx.config.directory_root).resolve()
    target = (root / ctx.agent.name).resolve()
    if target.parent != root:
        raise RuntimeError(f"agent folder {target} is not directly under {root}; refusing")
    if not target.exists():
        return "already gone"
    shutil.rmtree(target)
    return f"removed {target}"


_DESTROY_FUNCS = {
    "destroy-files": _destroy_files,
    "destroy-platform": _destroy_platform,
    "destroy-secrets": _destroy_secrets,
    "destroy-access": _destroy_access,
    "destroy-slack": _destroy_slack,
    "destroy-repo": _destroy_repo,
    "destroy-directory": _destroy_directory,
    "destroy-forget": _destroy_forget,
}


# In-flight registry (review Finding 1): guards against a second concurrent
# `run_destroy` launch for the same agent_id landing on top of one already
# running. Before this, a re-DELETE against an agent already DESTROYING fell
# through the route's status gate and fired a second daemon thread against
# the same AgentStep rows — "safe" only by accident, via layered exception
# handling elsewhere (worst case a spurious DESTROY_FAILED from two threads
# racing the same commit). `run_destroy` below registers `agent_id` here
# before starting the thread (a non-blocking check-and-add under
# `_destroying_lock`) and `_run_destroy_body_and_release` frees it in a
# `finally` once the thread's body returns, however it returns.
#
# In-process only (a plain set + Lock, not a cross-process lock): with more
# than one replica this is best-effort, and two pods CAN run this ledger for
# the same agent at once. What stands between a caller and that is the
# route's own status gate (`kobo_api.main`'s destroy route commits
# DESTROYING before launching), not this registry. The exposure is small
# because every step here tolerates "already gone" by design: the worst case
# is a spurious DESTROY_FAILED from two threads racing the same commits on
# steps that each individually do no harm twice. There is no conditional
# row claim (`provisioner._claim_step`'s shape) because destroy has no
# step-reset path to pair one with -- a claim that only matched PENDING
# would turn every resume into a silent no-op.
_destroying_agent_ids: set[int] = set()
_destroying_lock = threading.Lock()


def _run_destroy_body_and_release(
    session_factory: sessionmaker[Session],
    agent_id: int,
    clients: Clients,
    config: Config,
    acting_email: str | None = None,
) -> None:
    """Runs `_run_destroy_body`, then always frees `agent_id`'s in-flight
    slot (registered by `run_destroy` just before this function's thread was
    started) — a `finally` so the slot is released whether the run finished
    cleanly, failed a step, or hit `_run_destroy_body`'s own engine-level
    recovery path.
    """
    # The same heartbeat a provision has (issue #203): a destroy that dies
    # mid-way stops beating, and `sweep_interrupted` flips the row to
    # DESTROY_FAILED so the next DELETE can resume it, instead of leaving it
    # DESTROYING forever with nothing to notice.
    beat = _Heartbeat(session_factory, agent_id)
    try:
        try:
            beat.start()
        except Exception:
            log.warning(
                "could not start the heartbeat for destroying agent %s; the run "
                "continues but may be swept as interrupted",
                agent_id,
                exc_info=True,
            )
        _run_destroy_body(session_factory, agent_id, clients, config, acting_email)
    finally:
        beat.stop()
        with _destroying_lock:
            _destroying_agent_ids.discard(agent_id)


def _run_destroy_body(
    session_factory: sessionmaker[Session],
    agent_id: int,
    clients: Clients,
    config: Config,
    acting_email: str | None = None,
) -> None:
    session = session_factory()
    try:
        agent = session.get(Agent, agent_id)
        if agent is None:
            # Already forgotten (or never existed) — nothing to destroy and
            # nothing to record a failure against. Mirrors provisioner's
            # own unknown-agent-id tolerance.
            log.warning("run_destroy: agent %s not found, nothing to destroy", agent_id)
            return

        # Captured as plain values now, before "destroy-forget" (below)
        # deletes this row outright: the roster-refresh call at that branch
        # needs the departing agent's own name/group to ask "who could see
        # it", and `kobo_api.steps.refresh_colleague_rosters`/`_agents_who_
        # can_see` deliberately take these as plain values rather than an
        # `Agent` instance for exactly this reason (see their docstrings) --
        # by the time that branch runs, this agent's status is already
        # DESTROYING (set just below, and possibly already set by the route
        # before this function ever ran), so a fresh read of `agent.name`/
        # `agent.group_name` off the ORM object would still work today, but
        # would stop working the moment the row is actually deleted a few
        # lines later.
        departing_name = agent.name
        departing_group_name = agent.group_name

        try:
            agent.status = AgentStatus.DESTROYING
            agent.error = None
            session.commit()

            ctx = DestroyCtx(
                agent=agent,
                session=session,
                config=config,
                clients=clients,
                acting_email=acting_email,
            )

            failed_step_name: str | None = None
            for name in destroy_steps_for(agent):
                step_row = _get_or_create_step(session, agent_id, name)

                if step_row.status == StepStatus.DONE:
                    continue

                if failed_step_name is not None:
                    step_row.status = StepStatus.FAILED
                    step_row.error = f"aborted: {failed_step_name}"
                    session.commit()
                    continue

                step_row.status = StepStatus.RUNNING
                step_row.started_at = datetime.now(UTC)
                session.commit()

                try:
                    detail = _DESTROY_FUNCS[name](ctx)
                except Exception as exc:  # noqa: BLE001 - a step's failure, not a bug here
                    log.warning(
                        "destroy step %r failed for agent %s: %s", name, agent_id, exc
                    )
                    step_row.status = StepStatus.FAILED
                    step_row.error = str(exc)
                    step_row.finished_at = datetime.now(UTC)
                    agent.status = AgentStatus.DESTROY_FAILED
                    agent.error = str(exc)
                    session.commit()
                    failed_step_name = name
                    continue

                if name == "destroy-forget":
                    # _destroy_forget already deleted every AgentStep row
                    # for this agent (including this step's own, just
                    # committed as RUNNING above) and the agent row itself,
                    # then committed — there is nothing left here to mark
                    # DONE. The row disappearing IS the completion signal.
                    log.info("destroy step %r done for agent %s: %s", name, agent_id, detail)
                    # Access-control arc, Task C4: the destroyed agent's row
                    # is already gone, so allowed_pairs (recomputed fresh
                    # inside this call) naturally drops every pair that
                    # named it -- same "query current rows only" discipline
                    # that function's own docstring already documents.
                    # Fail-soft and skip-when-unwired, same as every other
                    # trigger of this shared helper (never lets a
                    # switchboard hiccup undo this step's already-committed
                    # deletion).
                    sync_dispatch_pairs_best_effort(clients, config, session)
                    # Roster refresh lane (Task 6): redeliver colleagues.md
                    # to every agent that could see the now-departed agent
                    # (using its name/group captured above, before this
                    # very step deleted its row) -- their next fresh
                    # `_colleague_roster` read naturally excludes it, since
                    # its row is already gone. Best-effort and switchboard-
                    # gated internally; this outer try/except is belt-and-
                    # suspenders so a bug in the refresh path can never
                    # undo this step's already-committed deletion.
                    try:
                        refresh_colleague_rosters(
                            clients,
                            config,
                            session,
                            name=departing_name,
                            group_name=departing_group_name,
                        )
                    except Exception:
                        log.warning(
                            "roster refresh failed after agent %s was destroyed",
                            departing_name,
                            exc_info=True,
                        )
                    return

                log.info("destroy step %r done for agent %s: %s", name, agent_id, detail)
                step_row.status = StepStatus.DONE
                step_row.finished_at = datetime.now(UTC)
                session.commit()
        except Exception as exc:
            # Engine-level failure: anything outside a step's own body (a
            # commit, a session going bad, ...). Mirrors provisioner's own
            # engine-level recovery (`_run_pipeline_body`) — the session
            # that hit the error may itself be poisoned, so clean it up
            # best-effort and record the failure through a fresh one.
            log.exception("run_destroy: engine-level failure for agent %s", agent_id)

            try:
                session.rollback()
            except Exception:  # best-effort cleanup only
                log.warning("run_destroy: rollback of broken session failed", exc_info=True)

            recovery_session = session_factory()
            try:
                recovered_agent = recovery_session.get(Agent, agent_id)
                if recovered_agent is None:
                    log.warning(
                        "run_destroy: agent %s not found while recording engine failure",
                        agent_id,
                    )
                    return
                recovered_agent.status = AgentStatus.DESTROY_FAILED
                recovered_agent.error = str(exc)
                recovery_session.commit()
            finally:
                recovery_session.close()
    finally:
        session.close()


def run_destroy(
    session_factory: sessionmaker[Session],
    agent_id: int,
    clients: Clients,
    config: Config,
    acting_email: str | None = None,
) -> threading.Thread | None:
    """Fire the destroy ledger for `agent_id` on a daemon thread and return
    it immediately — mirrors `kobo_api.provisioner.run_pipeline`'s exact
    fire-and-return shape (its own session opened fresh from
    `session_factory`, never the caller's, so it's safe to call this from a
    request handler and let the request return before destruction
    finishes).

    Safe to call again for the same `agent_id` once a prior invocation has
    finished (running or failed): it resumes from the first incomplete
    step, same as provisioning's own retry story.

    Safe to call again *while a prior invocation is still running*, too
    (review Finding 1) — unlike `run_pipeline`, which still carries that
    caveat. A non-blocking check-and-add against the module-level
    `_destroying_agent_ids` registry (under `_destroying_lock`) means a
    second call for an `agent_id` already in flight returns `None`
    immediately instead of starting a duplicate thread against the same
    AgentStep rows. The route calling this never inspects the return
    value, so its 202 response contract is unaffected either way — only
    the duplicate launch itself disappears.
    """
    with _destroying_lock:
        if agent_id in _destroying_agent_ids:
            log.info(
                "run_destroy: agent %s already has a destroy in flight, skipping launch",
                agent_id,
            )
            return None
        _destroying_agent_ids.add(agent_id)

    try:
        thread = threading.Thread(
            target=_run_destroy_body_and_release,
            args=(session_factory, agent_id, clients, config, acting_email),
            daemon=True,
        )
        thread.start()
    except Exception:
        # Construction or `.start()` itself failed (e.g. the OS refused a
        # new thread) — the slot was claimed above but the thread whose
        # `finally` would normally release it (_run_destroy_body_and_
        # release) never ran. Without this, the slot stays claimed forever
        # and every later run_destroy call for this agent_id silently
        # returns None.
        with _destroying_lock:
            _destroying_agent_ids.discard(agent_id)
        raise

    return thread

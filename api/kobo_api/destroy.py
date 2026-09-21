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
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import or_
from sqlalchemy.orm import Session, sessionmaker

from kobo_api.clients import ClientError, cabal_enabled
from kobo_api.config import Config
from kobo_api.directory import retire_directory
from kobo_api.models import A2AInvite, Agent, AgentStatus, AgentStep, RepoAccess, StepStatus
from kobo_api.pairs import sync_dispatch_pairs_best_effort
from kobo_api.provisioner import _get_or_create_step
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
DESTROY_STEPS = (
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
DESTROY_STEP_LABELS: dict[str, str] = {
    "destroy-platform": "Platform teardown",
    "destroy-access": "Model access",
    "destroy-slack": "Slack app",
    "destroy-repo": "Repository archive",
    "destroy-directory": "Directory listing",
    "destroy-forget": "Room record",
}


@dataclass
class DestroyCtx:
    agent: Agent
    session: Session
    config: Config
    clients: Clients


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
        return "no platform app to tear down"

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
    agent = ctx.agent
    config = ctx.config
    cloud = ctx.clients.cloud

    email = cloud.gsa_email_for(f"agent-{agent.name}")
    removed = cloud.remove_from_group(email, config.model_group)
    gone = cloud.delete_gsa(email)
    return f"membership removed: {removed}, gsa deleted: {gone}"


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
    """
    agent = ctx.agent
    github = ctx.clients.github

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
    """
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
    return "forgotten"


_DESTROY_FUNCS = {
    "destroy-platform": _destroy_platform,
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
) -> None:
    """Runs `_run_destroy_body`, then always frees `agent_id`'s in-flight
    slot (registered by `run_destroy` just before this function's thread was
    started) — a `finally` so the slot is released whether the run finished
    cleanly, failed a step, or hit `_run_destroy_body`'s own engine-level
    recovery path.
    """
    try:
        _run_destroy_body(session_factory, agent_id, clients, config)
    finally:
        with _destroying_lock:
            _destroying_agent_ids.discard(agent_id)


def _run_destroy_body(
    session_factory: sessionmaker[Session],
    agent_id: int,
    clients: Clients,
    config: Config,
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

            ctx = DestroyCtx(agent=agent, session=session, config=config, clients=clients)

            failed_step_name: str | None = None
            for name in DESTROY_STEPS:
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
            args=(session_factory, agent_id, clients, config),
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

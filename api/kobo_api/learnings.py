"""Daily learnings, the room half (2026-09-01 spec): validate what agents
propose, park it in agent_learnings for human curation, expire what nobody
reviews, and know which agents are due a nightly summarization.

The pod-side summarizer (daedalus.memory.learnings) validates its own
output before replying; everything here re-validates from scratch anyway.
Two independent walls: the room never trusts pod output, exactly as the
PR broker never trusts an agent's repo claims.

Nothing in this module writes memory. store_candidates writes CANDIDATE
rows; only the approve route (main.py) ever turns one into a teach.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from kobo_api.models import (
    LEARNING_APPROVED,
    LEARNING_DENIED,
    LEARNING_EXPIRED,
    LEARNING_PENDING,
    AgentLearning,
    AgentSystemTask,
)

log = logging.getLogger("kobo.learnings")

CANDIDATE_CAP = 10
RAW_SCAN_CAP = 50
CONTENT_MAX_CHARS = 400
EVIDENCE_MAX_CHARS = 200
SOURCE_MAX_CHARS = 512
KINDS = ("fact", "procedure")
V2_CANDIDATE_CAP = 5
STATEMENT_MAX_CHARS = 160
SAID_BY_MAX_CHARS = 120
WHERE_MAX_CHARS = 120
EXPIRY_DAYS = 14
DUE_AFTER_HOURS = 24
NEAR_DUP_THRESHOLD = 0.6
KNOWN_WINDOW_DAYS = 90
KNOWN_LIST_CAP = 40
EDITED_LEARNING_MAX_CHARS = 200
KNOWN_STATEMENT_MAX_CHARS = 400
# The Postgres advisory-lock key the nightly sweep claims, so exactly one
# replica sweeps per round. Any fixed 64-bit integer does; this one is the
# ASCII bytes of "kobolrn", which makes it recognizable in pg_locks and
# unlikely to collide with a key anything else in this database picks.
LEARNINGS_SWEEP_LOCK_ID = 0x6B6F626F6C726E
_KNOWN_LABELS = {
    LEARNING_PENDING: "pending",
    LEARNING_APPROVED: "remembered",
    LEARNING_DENIED: "skipped",
}

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")

# A quote that only says who the speaker is ("i'm Ana ana@example.com",
# "this is Ana") supports no statement about what they did or decided. A
# candidate resting on one is dropped (issue #143).
_IDENTITY_QUOTE_RE = re.compile(
    r"^\s*(?:(?:hi|hey|hello)[\s,!.]+)?(?:i['\u2019]?m|i am|this is|my name is)\s+(?P<rest>.*)$",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
IDENTITY_NAME_MAX_WORDS = 4

# Statements about who may access what are events, not standing facts, and an
# approved one becomes something the agent can cite as authority to grant
# access. Such a candidate is flagged and needs a second confirmation
# (issue #143).
_ACCESS_CHANGE_RE = re.compile(
    r"\b(?:approv\w*|grant\w*|revok\w*|permission\w*|access\w*|admin\w*|authori[sz]\w*)\b",
    re.IGNORECASE,
)
CONCERN_UNATTRIBUTED = "unattributed"
CONCERN_ACCESS_CHANGE = "access_change"
_STOPWORDS = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "has", "have", "if", "in", "into", "is", "it", "its", "not", "of", "on", "only", "or", "so", "that", "the", "their", "them", "then", "there", "these", "they", "this", "to", "was", "when", "which", "will", "with"]
)


def _tokens(text: str) -> frozenset[str]:
    """Lowercased content words. Dotted identifiers survive whole
    (`user.lifecycle.suspend` is one token, not three common words)."""
    return frozenset(
        token.strip("._-") for token in _TOKEN_RE.findall(text.lower())
    ) - _STOPWORDS - {""}


def is_near_duplicate(a: str, b: str) -> bool:
    """Token-set Jaccard >= NEAR_DUP_THRESHOLD. The second wall behind the
    runtime's own 'already proposed' list: deterministic, no model call.
    It catches a REPHRASE, not a related-but-distinct claim, by design --
    dropping a genuinely new fact is the worse failure."""
    left, right = _tokens(a), _tokens(b)
    if not left or not right:
        return False
    return len(left & right) / len(left | right) >= NEAR_DUP_THRESHOLD


def clean_edited_statement(text: str) -> str | None:
    """An operator's fixed wording for a learning, or None when it cannot
    be taught: empty, over EDITED_LEARNING_MAX_CHARS, or carrying any
    whitespace character other than a plain space (a learning is one
    sentence rendered on one line; a tab, vertical tab, form feed, NEL, or
    a Unicode line/paragraph separator all break that as surely as a
    literal newline would, so every one of them is rejected alongside
    \\n and \\r rather than just those two)."""
    cleaned = text.strip()
    if not cleaned or len(cleaned) > EDITED_LEARNING_MAX_CHARS:
        return None
    if any(ch.isspace() and ch != " " for ch in cleaned):
        return None
    return cleaned


def content_hash(content: str) -> str:
    """SHA-256 of the candidate's content -- the dedupe identity. A denied
    row's hash blocks the identical content forever; a rephrasing is a new
    hash and gets a fresh hearing."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _naive_utc(moment: datetime) -> datetime:
    """DB rows store naive UTC (SQLAlchemy DateTime strips tzinfo on both
    engines this room runs); every comparison here normalizes to match."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(UTC).replace(tzinfo=None)


def _clean_str_field(value: object) -> str:
    """`value`, stripped, when it's a plain `str` -- otherwise "" (missing),
    never `str(value)`. A candidate field that's JSON `null` or a list/dict
    coerces to noise like "None" or "['a', 'b']" under a blind `str()`, and
    that noise would otherwise land verbatim in the owner's review queue."""
    return value.strip() if isinstance(value, str) else ""


def _one_line(text: str) -> str:
    """Collapse every whitespace run (newlines, tabs, carriage returns
    included) to a single space. A learning is one sentence; more to the
    point, a statement is later rendered into an agent's prompt one entry
    per line, and must not be able to start a new line and forge a heading."""
    return " ".join(text.split())


def _parse_v1(item: dict) -> dict | None:
    """The 2026-09-01 shape, still spoken by runtimes built before the
    rethink. Agents roll runtimes at different times; both must land."""
    content = _clean_str_field(item.get("content"))
    kind = _clean_str_field(item.get("kind"))
    source = _clean_str_field(item.get("source"))
    if not content or kind not in KINDS or not source:
        return None
    return {
        "content": content[:CONTENT_MAX_CHARS],
        "kind": kind,
        "source": source[:SOURCE_MAX_CHARS],
        "evidence": _clean_str_field(item.get("evidence"))[:EVIDENCE_MAX_CHARS],
        "said_by": None,
        "replaces": None,
        "shape": "v1",
    }


def is_identity_only(quote: str) -> bool:
    """True when `quote` only introduces the speaker: "i'm", "this is" or
    "my name is", then nothing but a name, with or without an email. A name
    is up to IDENTITY_NAME_MAX_WORDS words, capitalised unless an email
    vouches for it, so "i'm approving admin for Bob" is not an introduction.
    The capital-letter test also takes "this is Postgres" or "I'm OOO" for
    an introduction. That costs a missed candidate, never a wrong one, and
    is the trade this rule makes."""
    match = _IDENTITY_QUOTE_RE.match(quote or "")
    if match is None:
        return False
    rest = match.group("rest")
    has_email = _EMAIL_RE.search(rest) is not None
    words = re.sub(r"[^\w\s'-]", " ", _EMAIL_RE.sub(" ", rest)).split()
    if not words:
        return has_email
    if len(words) > IDENTITY_NAME_MAX_WORDS:
        return False
    return has_email or all(word[0].isupper() for word in words)


def learning_concerns(content: str, said_by: str | None) -> list[str]:
    """Why approving this candidate needs a second, explicit confirmation:
    nobody it can be attributed to, or a statement about access. Computed
    from the row, so rows stored before this existed are judged the same."""
    concerns = []
    if not said_by:
        concerns.append(CONCERN_UNATTRIBUTED)
    if _ACCESS_CHANGE_RE.search(content):
        concerns.append(CONCERN_ACCESS_CHANGE)
    return concerns


def _parse_v2(item: dict) -> dict | None:
    """One human-attributed sentence (learnings rethink). An over-long
    statement is DROPPED, never truncated: a clipped sentence teaches half
    a claim. `replaces` must be a real int (bool is an int subclass and
    `True` must not become learning id 1)."""
    statement = _one_line(_clean_str_field(item.get("statement")))
    if not statement or len(statement) > STATEMENT_MAX_CHARS:
        return None
    quote = _one_line(_clean_str_field(item.get("quote")))
    if is_identity_only(quote):
        return None
    replaces = item.get("replaces")
    if isinstance(replaces, bool) or not isinstance(replaces, int):
        replaces = None
    return {
        "content": statement,
        "kind": "fact",
        "source": _one_line(_clean_str_field(item.get("where")))[:WHERE_MAX_CHARS] or "conversation",
        "evidence": _one_line(_clean_str_field(item.get("quote")))[:EVIDENCE_MAX_CHARS],
        "said_by": _one_line(_clean_str_field(item.get("said_by")))[:SAID_BY_MAX_CHARS] or None,
        "replaces": replaces,
        "shape": "v2",
    }


def parse_reply(reply: str) -> list[dict]:
    """The agent's JSON reply -> validated candidate dicts. Garbage in
    (prose, wrong shape, missing fields) reads as no candidates, never an
    error -- one agent's bad output must not sink the nightly sweep.

    Accepts both the legacy v1 shape (content/kind/source/evidence) and the
    v2 shape (statement/said_by/quote/where/replaces) from the learnings
    rethink, normalizing either into the same internal dict.

    CANDIDATE_CAP/V2_CANDIDATE_CAP bound VALID candidates, not raw items --
    a hostile (or just malformed) reply padded with junk ahead of the real
    candidates must not starve the caller of the good ones behind it.
    RAW_SCAN_CAP bounds the raw list itself so a giant list still can't buy
    unbounded parsing work: at most the first RAW_SCAN_CAP items are ever
    looked at, valid or not."""
    cleaned = reply.strip()
    if cleaned.startswith("```"):
        inner = cleaned.split("```")
        cleaned = inner[1] if len(inner) > 1 else ""
        cleaned = cleaned.removeprefix("json").strip()
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return []
    raw = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    candidates: list[dict] = []
    v2_count = 0
    for item in raw[:RAW_SCAN_CAP]:
        if len(candidates) >= CANDIDATE_CAP:
            break
        if not isinstance(item, dict):
            continue
        parsed = _parse_v2(item) if "statement" in item else _parse_v1(item)
        if parsed is None:
            continue
        if parsed["shape"] == "v2":
            if v2_count >= V2_CANDIDATE_CAP:
                continue
            v2_count += 1
        candidates.append(parsed)
    return candidates


def store_candidates(
    session: Session,
    agent_name: str,
    day: str,
    candidates: list[dict],
    *,
    now: datetime | None = None,
    require_attribution: bool = False,
) -> int:
    """Park candidates as pending rows. Per candidate, in order: the exact
    hash tombstone (any status, forever); `replaces` naming one of THIS
    agent's PENDING rows updates that row in place (anything else in
    `replaces` is ignored and the candidate is new); the near-duplicate
    guard against this agent's pending/approved/denied statements from the
    last KNOWN_WINDOW_DAYS; insert. Returns rows stored or updated.

    `require_attribution` (`Config.learnings_require_attribution`) drops a
    candidate with no `said_by` before any of that. It is off by default
    because a runtime that predates daedalus issue #165 also sends turns
    from the room's chat (anyone who can see the agent may chat with it)
    unattributed (issue #143)."""
    cutoff = _naive_utc(now or datetime.now(UTC)) - timedelta(days=KNOWN_WINDOW_DAYS)
    stored = 0
    for candidate in candidates[:CANDIDATE_CAP]:
        content = candidate["content"]
        if require_attribution and not candidate.get("said_by"):
            log.info("learnings: %s dropped an unattributed candidate: %r", agent_name, content[:80])
            continue
        digest = content_hash(content)
        exact = (
            session.query(AgentLearning)
            .filter_by(agent_name=agent_name, content_hash=digest)
            .first()
        )
        if exact is not None:
            log.info(
                "learnings: %s skipped an exact repeat of #%d (%s)",
                agent_name,
                exact.id,
                exact.status,
            )
            continue

        target = None
        if candidate.get("replaces") is not None:
            target = (
                session.query(AgentLearning)
                .filter_by(
                    agent_name=agent_name,
                    id=candidate["replaces"],
                    status=LEARNING_PENDING,
                )
                .first()
            )

        known = (
            session.query(AgentLearning)
            .filter(
                AgentLearning.agent_name == agent_name,
                AgentLearning.status.in_(
                    (LEARNING_PENDING, LEARNING_APPROVED, LEARNING_DENIED)
                ),
                AgentLearning.created_at >= cutoff,
            )
            .all()
        )
        match = next(
            (
                row
                for row in known
                if (target is None or row.id != target.id)
                and is_near_duplicate(content, row.content)
            ),
            None,
        )
        if match is not None:
            log.info(
                "learnings: %s dropped a near-duplicate of #%d (%s): %r",
                agent_name,
                match.id,
                match.status,
                content[:80],
            )
            continue

        if target is not None:
            target.content = content
            target.content_hash = digest
            target.said_by = candidate.get("said_by")
            target.source = candidate["source"]
            # An empty refinement quote must not wipe evidence the original
            # already had -- `or` keeps target.evidence when the incoming
            # value is falsy (missing key or "").
            target.evidence = candidate.get("evidence") or target.evidence
        else:
            session.add(
                AgentLearning(
                    agent_name=agent_name,
                    day=day,
                    content=content,
                    kind=candidate["kind"],
                    source=candidate["source"],
                    evidence=candidate.get("evidence", ""),
                    said_by=candidate.get("said_by"),
                    content_hash=digest,
                )
            )
        session.flush()
        stored += 1
    session.commit()
    return stored


SYSTEM_TASK_LEARNINGS = "learnings"
SYSTEM_TASK_ERROR_MAX_CHARS = 300


def record_system_task(
    session: Session, agent_name: str, task: str, now: datetime, *, error: str | None
) -> None:
    """Record one run of `task` for `agent_name` (issue #142): a success
    when `error` is None, else a failure with its message. Commits, so a
    failure is on record even when nothing else in the sweep is."""
    row = session.query(AgentSystemTask).filter_by(agent_name=agent_name, task=task).first()
    if row is None:
        row = AgentSystemTask(agent_name=agent_name, task=task, consecutive_failures=0)
        session.add(row)
    stamp = _naive_utc(now)
    row.last_run_at = stamp
    if error is None:
        row.last_success_at = stamp
        row.last_error = None
        row.consecutive_failures = 0
    else:
        row.last_error = error[:SYSTEM_TASK_ERROR_MAX_CHARS]
        row.consecutive_failures = (row.consecutive_failures or 0) + 1
    session.commit()


def system_task_status(row: AgentSystemTask, flag_after: int) -> dict:
    """One task's record as the API reports it; `failing` once it has failed
    `flag_after` runs in a row (`Config.system_task_flag_after`)."""
    return {
        "task": row.task,
        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
        "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
        "last_error": row.last_error,
        "consecutive_failures": row.consecutive_failures,
        "failing": row.consecutive_failures >= flag_after,
    }


def known_statements(session: Session, agent_name: str, now: datetime) -> list[dict]:
    """What the agent must not re-propose: its pending, remembered, and
    skipped statements from the last KNOWN_WINDOW_DAYS, newest first,
    capped. Ids ride along so a refinement can name what it `replaces`.
    Expired rows are left out -- nobody ever judged them.

    Ordered by created_at, id both descending: two rows can share an
    identical created_at (fast test seeding, or genuinely simultaneous
    writes), and without the id tiebreaker their relative order is
    whatever SQLite/Postgres happens to return -- not deterministic."""
    cutoff = _naive_utc(now) - timedelta(days=KNOWN_WINDOW_DAYS)
    rows = (
        session.query(AgentLearning)
        .filter(
            AgentLearning.agent_name == agent_name,
            AgentLearning.status.in_(tuple(_KNOWN_LABELS)),
            AgentLearning.created_at >= cutoff,
        )
        .order_by(AgentLearning.created_at.desc(), AgentLearning.id.desc())
        .limit(KNOWN_LIST_CAP)
        .all()
    )
    known = []
    for row in rows:
        statement = _one_line(row.content)[:KNOWN_STATEMENT_MAX_CHARS]
        if not statement:
            continue
        known.append(
            {"id": row.id, "status": _KNOWN_LABELS[row.status], "statement": statement}
        )
    return known


def expire_stale(session: Session, now: datetime) -> int:
    """Pending rows older than EXPIRY_DAYS flip to expired -- the queue
    stays livable, and their hashes keep blocking re-proposal."""
    cutoff = _naive_utc(now) - timedelta(days=EXPIRY_DAYS)
    rows = (
        session.query(AgentLearning)
        .filter(
            AgentLearning.status == LEARNING_PENDING,
            AgentLearning.created_at < cutoff,
        )
        .all()
    )
    for row in rows:
        row.status = LEARNING_EXPIRED
    session.commit()
    return len(rows)


def due_agents(session: Session, ready_names: list[str], now: datetime) -> list[str]:
    """Which ready agents get a summarization this sweep: those with no
    rows at all (never summarized) or whose newest row is older than
    DUE_AFTER_HOURS. Ordering follows ready_names."""
    cutoff = _naive_utc(now) - timedelta(hours=DUE_AFTER_HOURS)
    due: list[str] = []
    for name in ready_names:
        latest = (
            session.query(func.max(AgentLearning.created_at))
            .filter(AgentLearning.agent_name == name)
            .scalar()
        )
        if latest is None or latest <= cutoff:
            due.append(name)
    return due


def seconds_until_next_utc_midnight(now: datetime) -> float:
    """Sleep till the next UTC midnight, floored at 60s so a sweep that
    finishes seconds before midnight never busy-loops."""
    moment = now.astimezone(UTC)
    tomorrow = (moment + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(60.0, (tomorrow - moment).total_seconds())


def _relay_reply_not_completed(raw: object) -> bool:
    """True unless `raw` is a well-formed relayed task whose status state is
    exactly "completed". Deliberately duplicated from `kobo_api.main`'s
    identically-behaved `_teach_task_not_completed` (and `kobo_api.steps`'
    own `_relay_reply_not_completed`) rather than imported -- that function
    is a closure nested inside `create_app` (not reachable from this
    module, which `kobo_api.main` itself imports, so the reverse import
    would be circular anyway), and its own docstring already establishes
    the precedent that this specific check is meant to be copied to each
    new caller rather than shared, the same way `_extract_relay_reply` is
    shared but this narrower bool check is not.

    Without this, an old runtime's FAILED-task refusal to the (to it,
    unknown) "learnings" marker still carries a text part `parse_reply`
    happily turns into `[]` -- store_candidates stores nothing, and the
    sweep log reads exactly like a healthy agent having a quiet day. This
    lets the sweep tell the two apart and warn instead."""
    task: object = raw
    if isinstance(raw, dict):
        if isinstance(raw.get("task"), dict):
            task = raw["task"]
        elif isinstance(raw.get("result"), dict):
            task = raw["result"]

    if not isinstance(task, dict):
        return True

    status = task.get("status")
    if not isinstance(status, dict):
        return True

    return status.get("state") != "completed"


@contextmanager
def single_sweeper(engine) -> Iterator[bool]:
    """Claim the right to sweep this round, and say whether it was won.

    Every replica runs `nightly_loop`, so every replica wakes at boot and
    after every UTC midnight: without this, two pods relay a learnings task
    to the same agents in the same minute and each stores what comes back.

    On Postgres the claim is `pg_try_advisory_lock` -- non-blocking, so the
    loser skips the round rather than queueing to redo work the winner is
    already doing. Advisory locks belong to the SESSION that took them, so
    this holds a DEDICATED connection for the whole sweep: a pooled one
    handed back mid-sweep would carry the lock off to whatever ran next,
    and the explicit unlock in the `finally` is what releases it (returning
    a connection to the pool does not end the backend session, so a missing
    unlock would wedge every later round until the pod restarted).

    On SQLite there is one process by construction, so the guard is a
    no-op that always yields True and never opens a second connection.

    AUTOCOMMIT, deliberately: SQLAlchemy autobegins a transaction on the
    first execute, and this connection is held for the whole sweep -- it
    would otherwise sit idle-in-transaction the entire time, pinning the
    database's oldest running transaction behind a sweep that does its
    real work on a different connection. The lock is session-level, not
    transactional, so it needs no transaction to survive.
    """
    if engine.dialect.name != "postgresql":
        yield True
        return

    connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        acquired = bool(
            connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": LEARNINGS_SWEEP_LOCK_ID},
            ).scalar()
        )
        if not acquired:
            # Never unlock here: this process holds nothing, and
            # pg_advisory_unlock would be a claim on the winner's lock.
            yield False
            return
        try:
            yield True
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": LEARNINGS_SWEEP_LOCK_ID},
            )
    finally:
        connection.close()


def run_nightly(app) -> None:
    """One sweep, synchronous (runs via asyncio.to_thread) and claimed by
    exactly one replica (`single_sweeper` above): expire stale pending
    rows, then relay a learnings task to every due ready agent and store
    what comes back. With cabal unwired the sweep still expires
    (bookkeeping needs no relay) and does nothing else.

    The guard wraps the WHOLE sweep, expiry included: expiry is a write
    like any other, and two replicas flipping the same rows is the same
    duplicated work the relay half is guarded against.
    """
    now = datetime.now(UTC)
    session = app.state.session_factory()
    try:
        with single_sweeper(session.get_bind()) as acquired:
            if not acquired:
                # WARNING, not INFO: the app configures no logging, so INFO
                # from this module never reaches pod logs (same reasoning as
                # kobo_api.org._disclose_overrides) -- and a replica quietly
                # doing nothing every night is exactly what an operator
                # needs to be able to see.
                log.warning(
                    "learnings: another instance is sweeping, skipping this round"
                )
                return
            _sweep(session, app.state.config, app.state.clients, now)
    finally:
        session.close()


def _sweep(session, config, clients, now: datetime) -> None:
    """The sweep itself, once this process has claimed the round. The whole
    per-agent body is wrapped in its own `except Exception` -- one agent's
    DB hiccup (or anything else unanticipated) is logged and skipped, never
    aborting the rest of the fleet, on top of the narrower relay-specific
    catch below (which keeps its own specific log message for that one
    well-understood failure mode).

    The imports are deferred, as they were inside `run_nightly` before this
    split: kobo_api.clients imports this module, so a module-level import
    here would be circular.
    """
    import httpx

    from kobo_api.clients import (
        CabalError,
        _build_learnings_envelope,
        _extract_relay_reply,
        cabal_enabled,
    )
    from kobo_api.kinds import is_connected
    from kobo_api.models import Agent, AgentStatus

    expire_stale(session, now)
    if not (cabal_enabled(config) and clients.cabal is not None):
        return
    # Connected agents are left out (design 2026-09-21): they have no
    # switchboard identity, so the relay below could only ever fail -- once
    # per agent, every night, for as long as the agent exists. Filtered in
    # python rather than in the query so the one reader of the kind stays
    # `is_connected`, and so a future kind is included by default.
    ready = [
        row.name
        for row in session.query(Agent).filter_by(status=AgentStatus.READY).all()
        if not is_connected(row)
    ]
    day = now.date().isoformat()
    for name in due_agents(session, ready, now):
        try:
            envelope = _build_learnings_envelope(
                day, known_statements(session, name, now)
            )
            try:
                raw = clients.cabal.relay(name, envelope)
                reply = _extract_relay_reply(raw)
            except (CabalError, httpx.HTTPError, ValueError) as exc:
                log.warning("learnings relay failed for %s: %s", name, exc)
                record_system_task(
                    session, name, SYSTEM_TASK_LEARNINGS, now,
                    error=f"the switchboard relay failed: {exc}",
                )
                continue
            if _relay_reply_not_completed(raw):
                log.warning(
                    "learnings task not completed for %s (old runtime or "
                    "refusal): %s",
                    name,
                    reply[:200],
                )
                record_system_task(
                    session, name, SYSTEM_TASK_LEARNINGS, now,
                    error=f"the agent did not complete the task: {reply[:200]}",
                )
                continue
            candidates = parse_reply(reply)
            stored = store_candidates(
                session,
                name,
                day,
                candidates,
                now=now,
                require_attribution=config.learnings_require_attribution,
            )
            record_system_task(session, name, SYSTEM_TASK_LEARNINGS, now, error=None)
            log.info(
                "learnings: %s proposed %d, stored %d new",
                name,
                len(candidates),
                stored,
            )
        except Exception as exc:
            log.exception("learnings sweep failed for %s", name)
            try:
                session.rollback()
                record_system_task(
                    session, name, SYSTEM_TASK_LEARNINGS, now,
                    error=f"the room failed to process the reply: {type(exc).__name__}",
                )
            except Exception:
                log.exception("learnings: could not record the failure for %s", name)
            continue


async def nightly_loop(app) -> None:
    """The lifespan-owned loop: sweep now (boot catch-up -- due_agents
    keeps it from re-summarizing anyone already summarized inside 24h),
    then once after every UTC midnight. Cancellation passes through
    untouched; everything else is logged and the loop lives on."""
    import asyncio

    while True:
        try:
            await asyncio.to_thread(run_nightly, app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("learnings nightly sweep failed")
        await asyncio.sleep(seconds_until_next_utc_midnight(datetime.now(UTC)))

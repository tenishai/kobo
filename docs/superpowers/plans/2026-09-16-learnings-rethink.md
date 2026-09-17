# Learnings Rethink Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A learning becomes one human-attributed sentence; near-duplicates never reach the operator; the Learnings tab becomes a short glanceable list with Remember / Skip, inline fix, and a teach box.

**Architecture:** The nightly pipeline skeleton stands (kobo relays a `learnings` system task, the agent replies JSON, kobo re-validates and parks rows, approval rides the teach lane). Kobo gains a v2 wire shape parsed beside v1, an in-place `replaces` update, a deterministic token-Jaccard near-duplicate guard, a `known` list in the task metadata, and approve-with-edited-text. The UI panel is rewritten. The runtime prompt (daedalus `feat/learnings`) is rewritten to propose only what a person explicitly said.

**Tech Stack:** FastAPI + SQLAlchemy 2 + alembic (api/), Svelte 5 + TanStack Query (ui/), daedalus runtime (Python).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-09-16-learnings-rethink-design.md`. Exact values below are copied from it.
- `statement` ≤ **160** chars; `said_by` ≤ **120**; `quote` ≤ **200**; `where` ≤ **120**; edited approve text ≤ **200**.
- v2 nightly cap **5**; v1 (legacy) cap stays **10**.
- Near-duplicate: token-set Jaccard ≥ **0.6**, window **90 days**, statuses pending + approved + denied.
- `known` list: ≤ **40** statements, newest first, last **90 days**.
- Column reuse: `content` = statement, `evidence` = quote, `source` = where. Only new column: `said_by VARCHAR(120) NULL`. `kind` stays NOT NULL; v2 rows store `"fact"`.
- UI copy: **Remember** / **Skip** (never Approve / Deny in visible text). No `kind` shown anywhere. No em dashes in UI copy.
- Run API tests as: `cd api && PYTHONPATH=. .venv/bin/python -m pytest <path> -q > /tmp/out.txt 2>&1; echo "exit=$?"; tail -3 /tmp/out.txt`. **Never pipe pytest into tail/head** (the pipe reports tail's exit code, not pytest's).
- `test_api_agents.py` and other test files may SHADOW conftest fixtures; read the file's own fixtures before building an app in a test.
- Commit messages end with: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Commit with `git -c commit.gpgsign=false commit`.
- No vendor/client names in any file.

---

### Task 1: `said_by` column, migration, and list route

**Files:**
- Modify: `api/kobo_api/models.py` (class `AgentLearning`, ~line 550)
- Create: `api/migrations/versions/d2f6b8a4c1e9_learning_said_by.py`
- Modify: `api/kobo_api/main.py` (`list_learnings`, ~line 5003)
- Test: `api/tests/test_learnings_routes.py` (find the existing learnings route test file with `grep -ln "learnings" api/tests/test_*.py`; add to the one that tests `GET /api/agents/{name}/learnings`)

**Interfaces:**
- Produces: `AgentLearning.said_by: Mapped[str | None]` (String(120), nullable). List route rows gain `"said_by": str | None`.

- [ ] **Step 1: Write the failing test** (in the existing learnings list-route test file, reusing that file's own fixtures for client/agent seeding — copy the arrangement of its nearest existing list test):

```python
def test_list_learnings_includes_said_by(client, session_factory):
    # arrange exactly like this file's existing list test, but add said_by
    session = session_factory()
    session.add(
        AgentLearning(
            agent_name=AGENT_NAME, day="2026-09-16",
            content="Suspend keeps group memberships.", kind="fact",
            source="Slack thread, Sep 4", evidence="suspend retains memberships",
            content_hash="h-said-by", said_by="kgreiner",
        )
    )
    session.commit()
    session.close()
    rows = client.get(f"/api/agents/{AGENT_NAME}/learnings").json()["learnings"]
    assert rows[0]["said_by"] == "kgreiner"
```

- [ ] **Step 2: Run it, expect FAIL** (`TypeError: 'said_by' is an invalid keyword argument`).

- [ ] **Step 3: Add the column.** In `AgentLearning`, after `evidence`:

```python
    # Who stated it, as the conversation shows them (learnings rethink,
    # 2026-09-16). Nullable: v1 candidates and every pre-rethink row have none.
    said_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
```

Also replace the docstring's last paragraph sentence "production still creates this table the same create_all way, since the shipped image carries no migrations/ directory at all." with: "The image ships migrations/ and runs `alembic upgrade head` at boot, so columns added after the table's birth (like `said_by`) arrive by migration."

- [ ] **Step 4: Migration** `api/migrations/versions/d2f6b8a4c1e9_learning_said_by.py`:

```python
"""agent_learnings.said_by -- who stated the learning (learnings rethink)

Revision ID: d2f6b8a4c1e9
Revises: c7d3a9f21b84
"""

import sqlalchemy as sa
from alembic import op

revision = 'd2f6b8a4c1e9'
down_revision = 'c7d3a9f21b84'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_learnings") as batch:
        batch.add_column(sa.Column("said_by", sa.String(length=120), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_learnings") as batch:
        batch.drop_column("said_by")
```

- [ ] **Step 5: Route.** In `list_learnings`'s row dict add after `"evidence"`: `"said_by": row.said_by,`

- [ ] **Step 6: Run** the new test plus `tests/test_migrations.py` and the schema tripwire:

```bash
cd api && PYTHONPATH=. .venv/bin/python -m pytest tests/test_migrations.py <learnings route test file> -q > /tmp/out.txt 2>&1; echo "exit=$?"; tail -3 /tmp/out.txt
S=$(mktemp -d); KOBO_DB_URL="sqlite:///$S/h.db" .venv/bin/alembic upgrade head > /dev/null 2>&1; PYTHONPATH=. .venv/bin/python scripts/schema_tripwire.py "sqlite:///$S/h.db"; echo "tripwire exit=$?"
```
Expected: `exit=0`, `Schema parity`, `tripwire exit=0`.

- [ ] **Step 7: Commit** `feat(learnings): said_by column + migration`.

---

### Task 2: Parse the v2 wire shape beside v1

**Files:**
- Modify: `api/kobo_api/learnings.py` (`parse_reply` and constants)
- Test: `api/tests/test_learnings.py` (the existing unit test file for this module; confirm with `ls api/tests | grep learnings`)

**Interfaces:**
- Produces: `parse_reply(reply: str) -> list[dict]` where every dict has exactly the keys `content, kind, source, evidence, said_by, replaces, shape`. `said_by: str | None`, `replaces: int | None`, `shape: "v1" | "v2"`.
- Produces constants: `STATEMENT_MAX_CHARS = 160`, `SAID_BY_MAX_CHARS = 120`, `WHERE_MAX_CHARS = 120`, `V2_CANDIDATE_CAP = 5`.

- [ ] **Step 1: Failing tests**

```python
import json
from kobo_api.learnings import parse_reply


def _reply(*candidates):
    return json.dumps({"candidates": list(candidates)})


def test_parse_v2_candidate_normalizes_to_internal_keys():
    got = parse_reply(_reply({
        "statement": "Suspend keeps group memberships; only deactivate removes them.",
        "said_by": "kgreiner", "quote": "suspend RETAINS memberships",
        "where": "Slack thread, Sep 4", "replaces": 12,
    }))
    assert got == [{
        "content": "Suspend keeps group memberships; only deactivate removes them.",
        "kind": "fact", "source": "Slack thread, Sep 4",
        "evidence": "suspend RETAINS memberships", "said_by": "kgreiner",
        "replaces": 12, "shape": "v2",
    }]


def test_parse_v2_missing_where_falls_back_and_bad_replaces_is_none():
    got = parse_reply(_reply({"statement": "A plain sentence.", "replaces": "12"}))
    assert got[0]["source"] == "conversation"
    assert got[0]["replaces"] is None
    assert got[0]["said_by"] is None


def test_parse_v2_over_long_statement_is_dropped_not_truncated():
    # A clipped sentence is worse than none: truncation would teach half a claim.
    assert parse_reply(_reply({"statement": "x" * 161})) == []


def test_parse_v2_caps_at_five_and_v1_still_parses_at_ten():
    v2 = parse_reply(_reply(*[{"statement": f"Sentence number {i}."} for i in range(8)]))
    assert len(v2) == 5
    v1 = parse_reply(_reply(*[
        {"content": f"c{i}", "kind": "fact", "source": "s"} for i in range(12)
    ]))
    assert len(v1) == 10
    assert v1[0]["shape"] == "v1" and v1[0]["said_by"] is None and v1[0]["replaces"] is None


def test_parse_bool_replaces_is_rejected():
    # bool is an int subclass; True must not become learning id 1.
    assert parse_reply(_reply({"statement": "A sentence.", "replaces": True}))[0]["replaces"] is None
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement.** Add constants under `CANDIDATE_CAP`:

```python
V2_CANDIDATE_CAP = 5
STATEMENT_MAX_CHARS = 160
SAID_BY_MAX_CHARS = 120
WHERE_MAX_CHARS = 120
```

Replace the candidate loop in `parse_reply` (everything from `candidates: list[dict] = []` to `return candidates`) with:

```python
    candidates: list[dict] = []
    v2_count = 0
    for item in raw[:CANDIDATE_CAP]:
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
```

and add above `parse_reply`:

```python
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


def _parse_v2(item: dict) -> dict | None:
    """One human-attributed sentence (learnings rethink). An over-long
    statement is DROPPED, never truncated: a clipped sentence teaches half
    a claim. `replaces` must be a real int (bool is an int subclass and
    `True` must not become learning id 1)."""
    statement = _clean_str_field(item.get("statement"))
    if not statement or len(statement) > STATEMENT_MAX_CHARS:
        return None
    replaces = item.get("replaces")
    if isinstance(replaces, bool) or not isinstance(replaces, int):
        replaces = None
    return {
        "content": statement,
        "kind": "fact",
        "source": _clean_str_field(item.get("where"))[:WHERE_MAX_CHARS] or "conversation",
        "evidence": _clean_str_field(item.get("quote"))[:EVIDENCE_MAX_CHARS],
        "said_by": _clean_str_field(item.get("said_by"))[:SAID_BY_MAX_CHARS] or None,
        "replaces": replaces,
        "shape": "v2",
    }
```

- [ ] **Step 4: Run the whole module's tests**, expect `exit=0`. Existing v1 tests that assert exact dict equality will now see three extra keys (`said_by`, `replaces`, `shape`) — update those expected dicts; that is the intended contract change.

- [ ] **Step 5: Commit** `feat(learnings): parse the v2 statement shape beside v1`.

---

### Task 3: Store — `replaces` in place, near-duplicate guard

**Files:**
- Modify: `api/kobo_api/learnings.py` (`store_candidates`, new helpers)
- Test: `api/tests/test_learnings.py`

**Interfaces:**
- Consumes: Task 2's candidate dict keys; Task 1's `AgentLearning.said_by`.
- Produces: `is_near_duplicate(a: str, b: str) -> bool`; `NEAR_DUP_THRESHOLD = 0.6`; `KNOWN_WINDOW_DAYS = 90`; `store_candidates(session, agent_name, day, candidates, *, now: datetime | None = None) -> int` (`now` defaults to `datetime.now(UTC)`; existing 4-arg callers keep working).

- [ ] **Step 1: Failing tests** (use this file's existing session fixture; `_row` is a local helper):

```python
from datetime import UTC, datetime, timedelta
from kobo_api.learnings import content_hash, is_near_duplicate, store_candidates
from kobo_api.models import AgentLearning

NOW = datetime(2026, 9, 16, tzinfo=UTC)


def _cand(text, **over):
    base = {"content": text, "kind": "fact", "source": "Slack thread", "evidence": "",
            "said_by": "kgreiner", "replaces": None, "shape": "v2"}
    return {**base, **over}


def _row(session, text, status="pending", age_days=0, agent="ledger"):
    row = AgentLearning(
        agent_name=agent, day="2026-09-10", content=text, kind="fact", source="s",
        evidence="", content_hash=content_hash(text), status=status,
        created_at=(NOW - timedelta(days=age_days)).replace(tzinfo=None),
    )
    session.add(row)
    session.commit()
    return row


def test_near_duplicate_catches_a_rephrase_and_passes_a_distinct_claim():
    a = "Suspend keeps group memberships; only deactivate or delete removes them."
    assert is_near_duplicate(a, "Only deactivate or delete removes group memberships; suspend keeps them.")
    assert is_near_duplicate(a, "Suspend retains group memberships; deactivate removes them.")
    assert not is_near_duplicate(a, "CloudTrail Lake console queries take the bare event data store ID, not the ARN.")
    assert not is_near_duplicate("", "")  # no tokens is never a match


def test_store_drops_rephrase_of_pending_approved_or_denied(session):
    _row(session, "Suspend keeps group memberships; only deactivate or delete removes them.", status="denied")
    stored = store_candidates(session, "ledger", "2026-09-16",
        [_cand("Only deactivate or delete removes group memberships; suspend keeps them.")], now=NOW)
    assert stored == 0


def test_store_ignores_near_duplicates_outside_the_window_or_other_agents(session):
    text = "Suspend keeps group memberships; only deactivate or delete removes them."
    _row(session, text, status="approved", age_days=91)
    _row(session, text + " ", status="approved", agent="clio")
    stored = store_candidates(session, "ledger", "2026-09-16",
        [_cand("Only deactivate or delete removes group memberships; suspend keeps them.")], now=NOW)
    assert stored == 1


def test_replaces_updates_a_pending_row_in_place(session):
    old = _row(session, "Suspend keeps memberships.")
    stored = store_candidates(session, "ledger", "2026-09-16", [_cand(
        "Suspend keeps group memberships; only deactivate or delete removes them.",
        replaces=old.id, said_by="priya", evidence="q", source="Slack thread, Sep 16")], now=NOW)
    session.refresh(old)
    assert stored == 1
    assert old.content.startswith("Suspend keeps group memberships;")
    assert old.said_by == "priya" and old.day == "2026-09-10" and old.status == "pending"
    assert old.content_hash == content_hash(old.content)
    assert session.query(AgentLearning).filter_by(agent_name="ledger").count() == 1


def test_replaces_a_decided_or_foreign_row_is_treated_as_new(session):
    decided = _row(session, "Alpha beta gamma delta.", status="approved")
    foreign = _row(session, "Epsilon zeta eta theta.", agent="clio")
    stored = store_candidates(session, "ledger", "2026-09-16", [
        _cand("Completely unrelated first sentence here.", replaces=decided.id),
        _cand("Another entirely different second claim.", replaces=foreign.id),
    ], now=NOW)
    assert stored == 2
    session.refresh(decided); session.refresh(foreign)
    assert decided.content == "Alpha beta gamma delta." and foreign.content == "Epsilon zeta eta theta."


def test_replaces_does_not_compare_against_the_row_it_replaces(session):
    old = _row(session, "Suspend keeps group memberships.")
    stored = store_candidates(session, "ledger", "2026-09-16", [_cand(
        "Suspend keeps group memberships; deactivate removes them.", replaces=old.id)], now=NOW)
    assert stored == 1  # a refinement is near its own predecessor by definition


def test_v1_candidates_store_without_said_by(session):
    stored = store_candidates(session, "ledger", "2026-09-16", [
        {"content": "legacy", "kind": "procedure", "source": "s", "evidence": "",
         "said_by": None, "replaces": None, "shape": "v1"}], now=NOW)
    assert stored == 1
```

- [ ] **Step 2: Run, expect FAIL** (`ImportError: is_near_duplicate`).

- [ ] **Step 3: Implement.** Add `import re` to the imports, `LEARNING_APPROVED, LEARNING_DENIED` to the models import, and under the constants:

```python
NEAR_DUP_THRESHOLD = 0.6
KNOWN_WINDOW_DAYS = 90

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
_STOPWORDS = frozenset(
    "a an and are as at be but by for from has have if in into is it its not of on "
    "only or so that the their them then there these they this to was when which "
    "will with".split()
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
```

Replace `store_candidates` with:

```python
def store_candidates(
    session: Session,
    agent_name: str,
    day: str,
    candidates: list[dict],
    *,
    now: datetime | None = None,
) -> int:
    """Park candidates as pending rows. Per candidate, in order: the exact
    hash tombstone (any status, forever); `replaces` naming one of THIS
    agent's PENDING rows updates that row in place (anything else in
    `replaces` is ignored and the candidate is new); the near-duplicate
    guard against this agent's pending/approved/denied statements from the
    last KNOWN_WINDOW_DAYS; insert. Returns rows stored or updated."""
    cutoff = _naive_utc(now or datetime.now(UTC)) - timedelta(days=KNOWN_WINDOW_DAYS)
    stored = 0
    for candidate in candidates[:CANDIDATE_CAP]:
        content = candidate["content"]
        digest = content_hash(content)
        if (
            session.query(AgentLearning)
            .filter_by(agent_name=agent_name, content_hash=digest)
            .first()
            is not None
        ):
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
        if any(
            is_near_duplicate(content, row.content)
            for row in known
            if target is None or row.id != target.id
        ):
            continue

        if target is not None:
            target.content = content
            target.content_hash = digest
            target.said_by = candidate.get("said_by")
            target.source = candidate["source"]
            target.evidence = candidate.get("evidence", "")
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
```

(`session.flush()` inside the loop makes two near-identical candidates in ONE reply see each other.)

- [ ] **Step 4: Run the module's tests**, expect `exit=0`. Add one more test proving the in-reply case:

```python
def test_two_rephrases_in_one_reply_store_once(session):
    stored = store_candidates(session, "ledger", "2026-09-16", [
        _cand("Suspend keeps group memberships; only deactivate or delete removes them."),
        _cand("Only deactivate or delete removes group memberships; suspend keeps them."),
    ], now=NOW)
    assert stored == 1
```

- [ ] **Step 5: Commit** `feat(learnings): replaces-in-place and a near-duplicate guard`.

---

### Task 4: The task tells the agent what is already known

**Files:**
- Modify: `api/kobo_api/learnings.py` (new `known_statements`, `run_nightly`)
- Modify: `api/kobo_api/clients.py` (`_build_learnings_envelope`, ~line 1157)
- Test: `api/tests/test_learnings.py`, and the existing envelope test (find with `grep -rn "_build_learnings_envelope" api/tests`)

**Interfaces:**
- Consumes: `KNOWN_WINDOW_DAYS` (Task 3).
- Produces: `known_statements(session, agent_name: str, now: datetime) -> list[dict]` returning `[{"id": int, "status": "pending"|"remembered"|"skipped", "statement": str}]`, newest first, ≤ `KNOWN_LIST_CAP = 40`. `_build_learnings_envelope(on_date: str, known: list[dict] | None = None) -> dict` with `metadata["known"]` always present (a list).

- [ ] **Step 1: Failing tests**

```python
def test_known_statements_maps_statuses_orders_newest_first_and_windows(session):
    _row(session, "Oldest approved sentence here.", status="approved", age_days=5)
    _row(session, "Newer denied sentence here.", status="denied", age_days=2)
    _row(session, "Newest pending sentence here.", status="pending", age_days=1)
    _row(session, "Expired sentence is not listed.", status="expired", age_days=1)
    _row(session, "Too old to matter anymore.", status="approved", age_days=91)
    _row(session, "Another agent entirely.", agent="clio")
    got = known_statements(session, "ledger", NOW)
    assert [k["status"] for k in got] == ["pending", "skipped", "remembered"]
    assert got[0]["statement"] == "Newest pending sentence here."
    assert all(set(k) == {"id", "status", "statement"} for k in got)


def test_known_statements_caps_at_forty(session):
    for i in range(45):
        _row(session, f"Distinct sentence number {i} about topic {i}.", age_days=1)
    assert len(known_statements(session, "ledger", NOW)) == 40
```

Envelope test (in the file that already tests the envelope):

```python
def test_learnings_envelope_carries_known_list():
    from kobo_api.clients import _build_learnings_envelope
    known = [{"id": 3, "status": "pending", "statement": "A sentence."}]
    env = _build_learnings_envelope("2026-09-16", known)
    assert _metadata_of(env)["known"] == known          # use this file's existing metadata accessor
    assert _metadata_of(_build_learnings_envelope("2026-09-16"))["known"] == []
```

(If the file has no metadata accessor, read how its existing learnings-envelope test reaches `metadata["date"]` and reach `known` the same way.)

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement** in `learnings.py`:

```python
KNOWN_LIST_CAP = 40
_KNOWN_LABELS = {
    LEARNING_PENDING: "pending",
    LEARNING_APPROVED: "remembered",
    LEARNING_DENIED: "skipped",
}


def known_statements(session: Session, agent_name: str, now: datetime) -> list[dict]:
    """What the agent must not re-propose: its pending, remembered, and
    skipped statements from the last KNOWN_WINDOW_DAYS, newest first,
    capped. Ids ride along so a refinement can name what it `replaces`.
    Expired rows are left out -- nobody ever judged them."""
    cutoff = _naive_utc(now) - timedelta(days=KNOWN_WINDOW_DAYS)
    rows = (
        session.query(AgentLearning)
        .filter(
            AgentLearning.agent_name == agent_name,
            AgentLearning.status.in_(tuple(_KNOWN_LABELS)),
            AgentLearning.created_at >= cutoff,
        )
        .order_by(AgentLearning.created_at.desc())
        .limit(KNOWN_LIST_CAP)
        .all()
    )
    return [
        {"id": row.id, "status": _KNOWN_LABELS[row.status], "statement": row.content}
        for row in rows
    ]
```

In `clients.py`:

```python
def _build_learnings_envelope(on_date: str, known: list[dict] | None = None) -> dict:
    """The learnings system task: asks the agent for what people told it
    that day. The date and the `known` list (what is already pending,
    remembered, or skipped -- never to be re-proposed) ride metadata; the
    text is only a human-readable label for transcripts. Runtimes that
    predate `known` ignore it."""
    return _build_system_task_envelope(
        "learnings",
        f"Summarize your conversations from {on_date} into candidate memory snippets.",
        metadata={"date": on_date, "known": list(known or [])},
    )
```

In `run_nightly`, replace `envelope = _build_learnings_envelope(day)` with:

```python
                envelope = _build_learnings_envelope(
                    day, known_statements(session, name, now)
                )
```

and replace `stored = store_candidates(session, name, day, candidates)` with `stored = store_candidates(session, name, day, candidates, now=now)`.

- [ ] **Step 4: Run** the learnings + clients test files, expect `exit=0`.
- [ ] **Step 5: Commit** `feat(learnings): the nightly task carries what is already known`.

---

### Task 5: Remember with a fix (approve with edited text) + MCP parity

**Files:**
- Modify: `api/kobo_api/main.py` (`_decide_learning` ~1818, `approve_learning` ~5048, payload models near `LearningsBulkPayload`)
- Modify: `api/kobo_api/mcp/tools/write.py` (`kobo_decide_learning` ~1367)
- Test: the existing learnings route test file and the MCP write-tools test file (`api/tests/test_mcp_tools_write.py`)

**Interfaces:**
- Produces: `_decide_learning(name, learning_id, action, request, background_tasks, session, content: str | None = None)`. `POST …/learnings/{id}/approve` accepts an optional JSON body `{"content": "..."}`. MCP `kobo_decide_learning(ctx, agent, learning_id, action, content: str | None = None)`. `EDITED_LEARNING_MAX_CHARS = 200` in `kobo_api/learnings.py`. New helper `clean_edited_statement(text: str) -> str | None` in `kobo_api/learnings.py`.

- [ ] **Step 1: Failing tests.** Route tests — arrange exactly as this file's existing approve test does (it already fakes the teach relay/intake; reuse that and its way of reading what was taught):

```python
def test_approve_with_content_teaches_the_edited_text_and_keeps_the_hash(client, session_factory, ...):
    # seed one pending row with content "Suspend keeps memberships." as the existing approve test does
    original_hash = row.content_hash
    res = client.post(f"/api/agents/{AGENT_NAME}/learnings/{row.id}/approve",
                      json={"content": "  Suspend keeps group memberships.  "})
    assert res.status_code == 200
    # the taught note body contains the EDITED sentence (assert via the same seam the existing approve test inspects)
    refreshed = <reload row>
    assert refreshed.content == "Suspend keeps group memberships."
    assert refreshed.content_hash == original_hash          # original phrasing stays tombstoned
    assert refreshed.status == "approved"


@pytest.mark.parametrize("bad", ["", "   ", "x" * 201, "two\nlines"])
def test_approve_rejects_bad_edited_content(client, ..., bad):
    res = client.post(f".../learnings/{row.id}/approve", json={"content": bad})
    assert res.status_code == 422
    assert <row still pending and unchanged>


def test_approve_without_body_is_unchanged(client, ...):
    assert client.post(f".../learnings/{row.id}/approve").status_code == 200


def test_deny_ignores_content(client, ...):
    res = client.post(f".../learnings/{row.id}/deny", json={"content": "edited"})
    assert res.status_code == 200 and <row.content unchanged>
```

Unit tests in `tests/test_learnings.py`:

```python
from kobo_api.learnings import clean_edited_statement

def test_clean_edited_statement():
    assert clean_edited_statement("  A sentence.  ") == "A sentence."
    assert clean_edited_statement("") is None
    assert clean_edited_statement("x" * 201) is None
    assert clean_edited_statement("two\nlines") is None
    assert clean_edited_statement("x" * 200) == "x" * 200
```

MCP test (mirror the existing `kobo_decide_learning` approve test in `test_mcp_tools_write.py`): call the tool with `content="Edited sentence."` and assert the row's content is the edited text; call with `content="x"*201` and assert a `ToolError`.

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement.** In `learnings.py`:

```python
EDITED_LEARNING_MAX_CHARS = 200


def clean_edited_statement(text: str) -> str | None:
    """An operator's fixed wording for a learning, or None when it cannot
    be taught: empty, over EDITED_LEARNING_MAX_CHARS, or multi-line (a
    learning is one sentence; a pasted paragraph belongs in the teach box)."""
    cleaned = text.strip()
    if not cleaned or len(cleaned) > EDITED_LEARNING_MAX_CHARS or "\n" in cleaned or "\r" in cleaned:
        return None
    return cleaned
```

In `main.py`, add next to `LearningsBulkPayload`:

```python
class LearningApprovePayload(BaseModel):
    # Optional fixed wording (learnings rethink): when present, THIS is what
    # gets taught and what the row shows afterwards.
    content: str | None = None
```

`_decide_learning`: add the trailing parameter `content: str | None = None`. Immediately after the `if action == "deny": … return` block insert:

```python
    if content is not None:
        edited = clean_edited_statement(content)
        if edited is None:
            return JSONResponse(
                {"error": "a learning is one sentence, 200 characters or fewer"},
                status_code=422,
            )
        # The ORIGINAL content_hash is kept on purpose: the phrasing the
        # operator rejected-by-rewriting stays tombstoned against re-proposal.
        row.content = edited
```

(The existing `format_note(row.content, …)` / `note_filename(row.content, …)` lines that follow now naturally use the edited text. On the 502 path the function returns before `session.commit()`, so an edit that failed to deliver is not persisted — confirm `get_session` rolls back on a non-commit; if the session would otherwise flush the change, call `session.rollback()` before returning the 502.)

Import `clean_edited_statement` from `kobo_api.learnings`.

`approve_learning` route: add a parameter `payload: LearningApprovePayload | None = None` and pass `content=payload.content if payload else None`. `deny_learning` and the bulk route pass nothing.

MCP `kobo_decide_learning`: add `content: str | None = None` to the signature, document it in the docstring ("`content`, approve only: fixed wording to teach instead of the proposed sentence, one line, 200 characters or fewer"), pass `content=content if action == "approve" else None` through `_off_loop(_decide_learning, …)`. A 422 from `_decide_learning` must surface as a `ToolError` through the tool's existing non-200 handling; if that handling maps only specific codes, add 422.

- [ ] **Step 4: Run** route tests + `tests/test_learnings.py` + `tests/test_mcp_tools_write.py`, expect `exit=0`.
- [ ] **Step 5: Commit** `feat(learnings): remember with a fix - approve accepts edited wording`.

---

### Task 6: The surface — LearningsPanel rewrite

**Files:**
- Modify: `ui/src/lib/api.ts` (`Learning` type, `decideLearning`)
- Modify: `ui/src/lib/queries.ts` (`decideLearningMutation`)
- Rewrite: `ui/src/components/LearningsPanel.svelte`

**Interfaces:**
- Consumes: list rows with `said_by`; approve route's optional `{content}` body (Tasks 1, 5); existing `teachAgent(name, text)`.

- [ ] **Step 1: api.ts.** In `Learning` add `said_by: string | null;` after `evidence`. Replace `decideLearning`:

```ts
export async function decideLearning(
  name: string,
  id: number,
  action: "approve" | "deny",
  content?: string,
): Promise<void> {
  const edited = action === "approve" && content !== undefined;
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/learnings/${id}/${action}`,
    edited
      ? {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content }),
        }
      : { method: "POST" },
  );
  await parse(res);
}
```

- [ ] **Step 2: queries.ts.** `decideLearningMutation`'s `mutationFn` becomes:

```ts
    mutationFn: ({
      id,
      action,
      content,
    }: {
      id: number;
      action: "approve" | "deny";
      content?: string;
    }) => decideLearning(name, id, action, content),
```

- [ ] **Step 3: Rewrite `LearningsPanel.svelte`.** Keep the props (`agentName`, `canDecide`, `frozen`), the three query/mutation constructions with their `svelte-ignore` comments, and the file's BEM + CSS-variable conventions (`--space-2xs|xs|sm|md|lg`, `--border`, `--radius`, `--radius-sm`, `--panel`, `--bg`, `--muted`, `--ink`, `--accent`, `--fail`). Replace the header comment's first paragraph with: "Learnings rethink (2026-09-16): a learning is one sentence a person actually said, with their name on it. One list, Remember / Skip, inline fix, and a teach box -- adding a memory and accepting one live in the same place."

Script state and handlers (replace `days`, `decideDay`, and add):

```ts
  import { teachAgent } from "../lib/api";

  let showHistory = $state(false);
  let errorText = $state("");
  let openId = $state<number | null>(null);      // row whose quote is showing
  let editingId = $state<number | null>(null);   // row being reworded
  let draft = $state("");
  let teachText = $state("");
  let teachNote = $state("");

  const all = $derived(learnings.data?.learnings ?? []);
  const pending = $derived(all.filter((l: Learning) => l.status === "pending"));
  const history = $derived(all.filter((l: Learning) => l.status !== "pending"));
  const remembered = $derived(history.filter((l: Learning) => l.status === "approved").length);
  const skipped = $derived(history.length - remembered);

  // svelte-ignore state_referenced_locally
  const teach = createMutation(() => ({
    mutationFn: (text: string) => teachAgent(agentName, text),
    onSuccess: () => {
      teachText = "";
      teachNote = `${agentName} will remember that.`;
      queryClient.invalidateQueries({ queryKey: ["agents", agentName] });
    },
  }));

  const busy = $derived(decide.isPending || bulkDecide.isPending || teach.isPending);

  function byline(l: Learning): string {
    return [l.said_by, l.source].filter(Boolean).join(" · ");
  }

  function startEdit(l: Learning): void {
    editingId = l.id;
    draft = l.content;
  }

  function remember(l: Learning): void {
    errorText = "";
    const edited = editingId === l.id && draft.trim() !== l.content;
    decide.mutate(
      { id: l.id, action: "approve", ...(edited ? { content: draft.trim() } : {}) },
      {
        onSuccess: () => (editingId = null),
        onError: (e: unknown) =>
          (errorText = errorMessage(e, "Couldn't save that. Try again.")),
      },
    );
  }

  function skip(id: number): void {
    errorText = "";
    decide.mutate(
      { id, action: "deny" },
      { onError: (e: unknown) => (errorText = errorMessage(e, "Couldn't skip that. Try again.")) },
    );
  }

  function decideAll(action: "approve" | "deny"): void {
    errorText = "";
    bulkDecide.mutate(
      { ids: pending.map((l: Learning) => l.id), action },
      { onError: (e: unknown) => (errorText = errorMessage(e, "Couldn't finish that. Try again.")) },
    );
  }

  function submitTeach(event: SubmitEvent): void {
    event.preventDefault();
    const text = teachText.trim();
    if (!text) return;
    errorText = "";
    teachNote = "";
    teach.mutate(text, {
      onError: (e: unknown) => (errorText = errorMessage(e, "Couldn't teach that. Try again.")),
    });
  }
```

Markup (replace everything inside `<section>`):

```svelte
<section class="learnings-panel" aria-labelledby="learnings-panel-heading">
  <h2 id="learnings-panel-heading" class="learnings-panel__heading">Learnings</h2>

  {#if canDecide}
    <form class="learnings-teach" onsubmit={submitTeach}>
      <input
        class="learnings-teach__input"
        type="text"
        bind:value={teachText}
        placeholder={`Teach ${agentName} something...`}
        aria-label={`Teach ${agentName} something`}
        maxlength="400"
        disabled={frozen || busy}
      />
      <button class="learnings-panel__button" type="submit" disabled={frozen || busy || !teachText.trim()}>
        Add
      </button>
    </form>
    {#if teachNote}<p class="learnings-panel__note" role="status">{teachNote}</p>{/if}
  {/if}

  {#if errorText}
    <p class="learnings-panel__error" role="alert">{errorText}</p>
  {/if}

  {#if learnings.isPending}
    <p class="learnings-panel__empty">Loading...</p>
  {:else if learnings.isError}
    <ErrorState
      message={errorMessage(learnings.error, "Couldn't load this agent's learnings.")}
      onretry={() => learnings.refetch()}
    />
  {:else if pending.length === 0}
    <p class="learnings-panel__empty">
      Nothing new. When someone tells {agentName} something worth keeping, it shows up here.
    </p>
  {:else}
    <div class="learnings-panel__summary">
      <p class="learnings-panel__count">
        {agentName} picked up {pending.length} {pending.length === 1 ? "thing" : "things"}
      </p>
      {#if canDecide}
        <div class="learnings-panel__bulk">
          <button class="learnings-panel__button learnings-panel__button--quiet" type="button"
            disabled={frozen || busy} onclick={() => decideAll("deny")}>Skip all</button>
          <button class="learnings-panel__button learnings-panel__button--primary" type="button"
            disabled={frozen || busy} onclick={() => decideAll("approve")}>Remember all</button>
        </div>
      {/if}
    </div>

    <ul class="learnings-list">
      {#each pending as l (l.id)}
        <li class="learning-row">
          {#if editingId === l.id}
            <input
              class="learning-row__edit"
              type="text"
              bind:value={draft}
              maxlength="200"
              aria-label="Fix the wording"
              onkeydown={(e) => { if (e.key === "Escape") editingId = null; }}
            />
          {:else}
            <button
              class="learning-row__statement"
              class:learning-row__statement--open={openId === l.id}
              type="button"
              aria-expanded={openId === l.id}
              onclick={() => (openId = openId === l.id ? null : l.id)}
            >{l.content}</button>
          {/if}

          <div class="learning-row__foot">
            <span class="learning-row__byline">{byline(l)}</span>
            {#if canDecide}
              <span class="learning-row__actions">
                {#if editingId === l.id}
                  <button class="learnings-panel__button learnings-panel__button--quiet" type="button"
                    onclick={() => (editingId = null)}>Cancel</button>
                {:else}
                  <button class="learnings-panel__button learnings-panel__button--quiet" type="button"
                    disabled={frozen || busy} aria-label="Fix the wording" title="Fix the wording"
                    onclick={() => startEdit(l)}>✎</button>
                  <button class="learnings-panel__button learnings-panel__button--quiet" type="button"
                    disabled={frozen || busy} onclick={() => skip(l.id)}>Skip</button>
                {/if}
                <button class="learnings-panel__button learnings-panel__button--primary" type="button"
                  disabled={frozen || busy || (editingId === l.id && !draft.trim())}
                  onclick={() => remember(l)}>Remember</button>
              </span>
            {/if}
          </div>

          {#if openId === l.id && l.evidence}
            <blockquote class="learning-row__quote">{l.evidence}</blockquote>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}

  {#if history.length > 0}
    <button class="learnings-panel__history-toggle" type="button"
      aria-expanded={showHistory} onclick={() => (showHistory = !showHistory)}>
      Remembered {remembered} · Skipped {skipped}
      <span aria-hidden="true">{showHistory ? "▾" : "▸"}</span>
    </button>
    {#if showHistory}
      <ul class="learnings-list learnings-list--history">
        {#each history as l (l.id)}
          <li class="learning-row learning-row--decided">
            <p class="learning-row__statement-text">{l.content}</p>
            <span class="learning-row__byline">
              {l.status === "approved" ? "Remembered" : l.status === "denied" ? "Skipped" : "Expired"}{l.decided_by ? ` by ${l.decided_by}` : ""}
            </span>
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</section>
```

Style block (replace the old one; keep `.learnings-panel`, `__heading`, `__error`, `__empty`, `__button`, `__button:hover`, `__button:disabled` rules from the old file verbatim, delete every `.learnings-day*`, `.learning-card*`, `__explainer`, `--deny` rule, and add):

```css
  .learnings-teach { display: flex; gap: var(--space-xs); }
  .learnings-teach__input,
  .learning-row__edit {
    flex: 1;
    min-width: 0;
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
    color: var(--ink);
    font: inherit;
  }
  .learnings-teach__input:focus,
  .learning-row__edit:focus { outline: none; border-color: var(--accent); }
  .learnings-panel__note { margin: 0; color: var(--muted); font-size: 0.85rem; }
  .learnings-panel__summary {
    display: flex; flex-wrap: wrap; align-items: center;
    justify-content: space-between; gap: var(--space-sm);
  }
  .learnings-panel__count { margin: 0; color: var(--ink); font-size: 0.95rem; font-weight: 600; }
  .learnings-panel__bulk { display: flex; gap: var(--space-xs); }
  .learnings-panel__button--primary { border-color: var(--accent); color: var(--accent); }
  .learnings-panel__button--quiet { border-color: transparent; background: transparent; color: var(--muted); }
  .learnings-list { display: flex; flex-direction: column; margin: 0; padding: 0; list-style: none; }
  .learning-row {
    display: flex; flex-direction: column; gap: var(--space-2xs);
    padding: var(--space-sm) 0; border-top: 1px solid var(--border);
  }
  .learning-row:first-child { border-top: 0; }
  .learning-row--decided { opacity: 0.75; }
  .learning-row__statement {
    display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical;
    overflow: hidden; margin: 0; padding: 0; border: 0; background: none;
    color: var(--ink); font: inherit; line-height: 1.4; text-align: left; cursor: pointer;
  }
  .learning-row__statement--open { display: block; overflow: visible; }
  .learning-row__statement-text { margin: 0; color: var(--ink); line-height: 1.4; }
  .learning-row__foot {
    display: flex; flex-wrap: wrap; align-items: center;
    justify-content: space-between; gap: var(--space-xs);
  }
  .learning-row__byline { color: var(--muted); font-size: 0.8rem; }
  .learning-row__actions { display: flex; gap: var(--space-2xs); margin-left: auto; }
  .learning-row__quote {
    margin: 0; padding-left: var(--space-sm); border-left: 2px solid var(--border);
    color: var(--muted); font-size: 0.85rem;
  }
  .learnings-panel__history-toggle {
    align-self: flex-start; padding: 0; border: 0; background: none;
    color: var(--muted); font: inherit; font-size: 0.85rem; cursor: pointer;
  }
  .learnings-panel__history-toggle:hover { color: var(--accent); }
```

- [ ] **Step 4: Verify.**

```bash
cd ui && npm run build > /tmp/ui-build.txt 2>&1; echo "build exit=$?"; npm run check > /tmp/ui-check.txt 2>&1; echo "check exit=$?"; tail -4 /tmp/ui-check.txt
grep -n "Approve\|Deny\|learning-card\|learnings-day\|\.kind" ui/src/components/LearningsPanel.svelte; echo "leftovers exit=$? (1 = none, good)"
```
Expected: both exits 0, zero svelte-check errors, leftovers grep exits 1.

- [ ] **Step 5: Commit** `feat(ui): learnings as a short list - Remember / Skip, inline fix, teach box`.

---

### Task 7: Runtime prompt v2 (daedalus, branch `feat/learnings`)

**Repo:** `~/Documents/GitHub/daedalus`. Start with `git fetch origin && git checkout -B feat/learnings origin/feat/learnings`. Tests: `make test` or the repo's documented pytest invocation (read `Makefile`); never pipe the test command.

**Files:**
- Modify: `src/daedalus/memory/learnings.py`
- Modify: `src/daedalus/agent.py` (`_handle_learnings`, ~line 451)
- Test: `tests/test_memory_learnings.py` (the existing test file for this module; confirm with `ls tests | grep learn`)

**Interfaces:**
- Produces: `build_messages(persona, conversations, known, *, on_date, already: list[dict] | None = None)`; v2 reply `{"candidates": [{"statement","said_by","quote","where","replaces"?}]}`; `MAX_CANDIDATES = 5`, `STATEMENT_MAX_CHARS = 160`, `SAID_BY_MAX_CHARS = 120`, `WHERE_MAX_CHARS = 120`.

- [ ] **Step 1: Failing tests**

```python
def test_instructions_demand_human_statements_not_conclusions():
    text = build_messages("p", [], [], on_date="2026-09-16")[1]["content"]
    assert "a person explicitly said" in text
    assert "never your own conclusion" in text
    assert '"statement"' in text and '"said_by"' in text
    assert '"kind"' not in text


def test_already_block_lists_ids_and_appears_only_when_present():
    none = build_messages("p", [], [], on_date="d")[1]["content"]
    assert "Already proposed or decided" not in none
    some = build_messages("p", [], [], on_date="d", already=[
        {"id": 12, "status": "pending", "statement": "Suspend keeps memberships."}])[1]["content"]
    assert "## Already proposed or decided (do not repeat)" in some
    assert "- [12, pending] Suspend keeps memberships." in some


def test_already_block_survives_garbage_entries():
    text = build_messages("p", [], [], on_date="d", already=[
        "nope", {"id": "x"}, {"id": 3, "status": "skipped", "statement": "Fine."}])[1]["content"]
    assert "- [3, skipped] Fine." in text and "nope" not in text


def test_parse_v2_validates_and_caps():
    raw = json.dumps({"candidates": [
        {"statement": "A sentence.", "said_by": "kgreiner", "quote": "q", "where": "Slack", "replaces": 4},
        {"statement": "x" * 161},
        {"statement": "B sentence.", "replaces": True},
        *[{"statement": f"Sentence {i}."} for i in range(10)],
    ]})
    got = parse_candidates(raw)
    assert len(got) == 5
    assert got[0] == {"statement": "A sentence.", "said_by": "kgreiner", "quote": "q", "where": "Slack", "replaces": 4}
    assert "replaces" not in got[1]
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement.** Constants: `MAX_CANDIDATES = 5`, add `STATEMENT_MAX_CHARS = 160`, `SAID_BY_MAX_CHARS = 120`, `WHERE_MAX_CHARS = 120`; delete `CONTENT_MAX_CHARS`, `SOURCE_MAX_CHARS`, `KINDS` if nothing else in the repo imports them (`git grep`), otherwise leave them. Replace `_INSTRUCTIONS` (the NOTE above it about the phrase "already know" still applies — do not use that exact phrase in the body):

```python
_INSTRUCTIONS = """\
Review your conversations from the last day, shown below. Propose up to
{max_candidates} things worth remembering permanently.

A candidate is ONLY something a person explicitly said or corrected in
these conversations: a fact they stated, a correction they made to you, a
rule or preference they told you. It is never your own conclusion, never
something you inferred by combining threads, never the output of a tool,
and never a general lesson you drew. If no person said it, it is not a
candidate.

Write each one as a single plain sentence, at most {statement_max}
characters, specific enough to act on, in the person's meaning (not
necessarily their exact words). If the same topic came up more than once,
propose it once. Skip anything ephemeral (a deploy in progress, someone's
availability today), anything listed as recalled knowledge, and anything
listed as proposed or decided before. If today's conversation refines
something listed as pending, propose the refined sentence and name the
pending entry's id in "replaces".

Reply with ONLY a JSON object, no prose, in exactly this shape:
{{"candidates": [{{"statement": "...", "said_by": "...",
"quote": "...", "where": "..."}}]}}

- statement: the one sentence
- said_by: who said it, exactly as the conversation shows them; omit only
  if the conversation truly does not show who
- quote: their words, verbatim, at most {quote_max} characters
- where: where it was said, in plain language, e.g.
  "Slack thread, {date}"
- replaces: optional; the id of a pending entry this sentence refines

If nobody told you anything worth keeping, reply {{"candidates": []}}.
"""
```

`build_messages` gains `already: list[dict] | None = None`; format with `max_candidates=MAX_CANDIDATES, statement_max=STATEMENT_MAX_CHARS, quote_max=EVIDENCE_MAX_CHARS, date=on_date`; after the known-facts block append, when any valid entries exist:

```python
    listed = [
        f"- [{entry['id']}, {entry['status']}] {entry['statement']}"
        for entry in (already or [])
        if isinstance(entry, dict)
        and isinstance(entry.get("id"), int) and not isinstance(entry.get("id"), bool)
        and isinstance(entry.get("status"), str)
        and isinstance(entry.get("statement"), str)
    ]
    if listed:
        parts.append("## Already proposed or decided (do not repeat)\n" + "\n".join(listed))
```

`parse_candidates`' per-item body becomes:

```python
        statement = _string_field(item, "statement")
        if not statement or len(statement) > STATEMENT_MAX_CHARS:
            continue
        candidate = {
            "statement": statement,
            "said_by": _string_field(item, "said_by")[:SAID_BY_MAX_CHARS],
            "quote": _string_field(item, "quote")[:EVIDENCE_MAX_CHARS],
            "where": _string_field(item, "where")[:WHERE_MAX_CHARS],
        }
        replaces = item.get("replaces")
        if isinstance(replaces, int) and not isinstance(replaces, bool):
            candidate["replaces"] = replaces
        candidates.append(candidate)
```

(The slice `raw[:MAX_CANDIDATES]` must apply to VALID candidates, not raw items: iterate all of `raw` and `break` once `len(candidates) == MAX_CANDIDATES`, so one over-long entry does not cost a slot.)

`agent.py` `_handle_learnings`: read `already = (task.metadata or {}).get("known")` (use however this handler already reads `metadata["date"]`), pass `already=already if isinstance(already, list) else None` into `build_messages`. Update the module docstring's first line to "propose what people told the agent that day".

- [ ] **Step 4: Run the repo's tests** (whole suite), expect exit 0. Update existing tests that asserted the v1 shape or `MAX_CANDIDATES == 10`.
- [ ] **Step 5: Commit** `feat(learnings): propose only what a person said - v2 statement shape` and push to `origin feat/learnings` (PR #26 updates in place).

---

## Self-review notes

Spec coverage: format/said_by → T1,T2; extraction rules → T7; known list → T4 (+T7 consumer); replaces + guard → T3; approve-with-fix + MCP parity → T5; surface (one list, byline, quote on tap, Remember/Skip, ✎, teach box, empty state, history counts, permission gating) → T6; migration → T1; legacy rows render clamped → T6 (`line-clamp` + tap to open). Not-in-v1 items have no task, by design.

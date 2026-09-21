# Daily Learnings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Agents summarize their last day of conversations into candidate memory snippets; owners approve or deny them in kobo; approved snippets ride the existing teach lane into techne. Nothing is ever auto-learned.

**Architecture:** daedalus gains a conversation journal (SQLite beside the techne db) written on task completion, and a `learnings` system-task handler that summarizes the journal's last 24h with the agent's own model and replies JSON candidates. kobo gains an `agent_learnings` Postgres table, a lifespan-owned nightly loop that relays the learnings task to every due ready agent, approve/deny routes that reuse the teach lane, and a Learnings tab with a pending-count badge.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, sqlite3 (journal), pydantic, litellm (via existing `LiteLLMClient`), Svelte 5 runes, @tanstack/svelte-query v6, vitest.

**Spec:** `docs/superpowers/specs/2026-09-01-learnings-ui-vision-design.md` (Wave 1 section, as amended 2026-09-01: journal replaces checkpoints as the conversation source; hash dedupe is kobo-side only; the decide gate is owner/operator/grantee).

## Global Constraints

- The client's name NEVER appears in daedalus or kobo source, comments, or string literals.
- No em dashes in kobo `.svelte` copy (enforced by `ui/src/lib/copy.test.ts`); house style avoids them everywhere -- use `--` in comments.
- Candidate bounds (both sides validate independently): max 10 per run, content <= 400 chars, evidence <= 200 chars, source <= 512 chars, kind in `("fact", "procedure")`.
- Lifecycle numbers: journal retention 7 days, agent due after 24 hours, pending expiry 14 days.
- Journal writes and the nightly loop degrade gracefully: a failure never fails the task or takes the room down.
- daedalus repo: `ruff check src tests` must pass (wrap long lines); tests run `python -m pytest tests/ -x -q` from the repo root.
- kobo API tests run from `api/`: `python -m pytest tests/<file> -x -q`. kobo UI tests run from `ui/`: `npx vitest run <file>`.
- New kobo table follows the `Base.metadata.create_all` convention (no manual ALTER; see `RepoAccess`'s docstring in `api/kobo_api/models.py`).
- daedalus work happens on branch `feat/learnings` in `~/Documents/GitHub/daedalus`. kobo work happens on branch `feat/daily-learnings` in `~/Documents/GitHub/kobo`.

---

## Repo split

| Tasks | Repo | Branch |
|-------|------|--------|
| 1-3 | `~/Documents/GitHub/daedalus` | `feat/learnings` |
| 4-9 | `~/Documents/GitHub/kobo` | `feat/daily-learnings` |

Tasks 1-3 and 4-9 are independent streams; within each stream, tasks are ordered.

---

### Task 1: Conversation journal module (daedalus)

**Files:**
- Create: `src/daedalus/memory/journal.py`
- Test: `tests/test_memory_journal.py`

**Interfaces:**
- Consumes: nothing (stdlib only: sqlite3, json, datetime, pathlib).
- Produces: `journal_path(memory_db_path: str | Path) -> Path`; `record(path, context_id: str, history: list[dict], *, now: datetime | None = None) -> None`; `recent(path, *, hours: int = 24, now: datetime | None = None) -> list[dict]` returning `[{"context_id": str, "history": list[dict], "created_at": str}]` oldest-first; constants `JOURNAL_FILENAME = "journal.db"`, `RETENTION_DAYS = 7`. Tasks 2 and 3 import all of these.

- [ ] **Step 1: Create the branch**

```bash
git -C ~/Documents/GitHub/daedalus checkout -b feat/learnings
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_memory_journal.py`:

```python
"""The conversation journal: what the learnings summarizer reads.

Plain sqlite + stdlib -- no techne dependency, so no importorskip here.
"""

from datetime import UTC, datetime, timedelta

from daedalus.memory.journal import journal_path, recent, record

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def test_journal_path_is_sibling_of_memory_db(tmp_path):
    assert journal_path(tmp_path / "techne.db") == tmp_path / "journal.db"


def test_record_then_recent_roundtrips(tmp_path):
    path = tmp_path / "journal.db"
    history = [{"role": "user", "text": "hi"}, {"role": "agent", "text": "hello"}]
    record(path, "slack:C1:171", history, now=NOW)

    rows = recent(path, now=NOW)

    assert len(rows) == 1
    assert rows[0]["context_id"] == "slack:C1:171"
    assert rows[0]["history"] == history


def test_recent_excludes_rows_older_than_window(tmp_path):
    path = tmp_path / "journal.db"
    record(path, "old", [{"role": "user", "text": "stale"}], now=NOW - timedelta(hours=25))
    record(path, "new", [{"role": "user", "text": "fresh"}], now=NOW - timedelta(hours=1))

    rows = recent(path, now=NOW)

    assert [r["context_id"] for r in rows] == ["new"]


def test_recent_orders_oldest_first(tmp_path):
    path = tmp_path / "journal.db"
    record(path, "first", [{"role": "user", "text": "a"}], now=NOW - timedelta(hours=3))
    record(path, "second", [{"role": "user", "text": "b"}], now=NOW - timedelta(hours=1))

    rows = recent(path, now=NOW)

    assert [r["context_id"] for r in rows] == ["first", "second"]


def test_record_prunes_past_retention(tmp_path):
    path = tmp_path / "journal.db"
    record(path, "ancient", [{"role": "user", "text": "x"}], now=NOW - timedelta(days=8))
    record(path, "today", [{"role": "user", "text": "y"}], now=NOW)

    # The 8-day-old row was pruned by today's write; widen the read window
    # past it to prove deletion rather than mere filtering.
    rows = recent(path, hours=24 * 30, now=NOW)

    assert [r["context_id"] for r in rows] == ["today"]


def test_recent_missing_file_reads_as_empty(tmp_path):
    assert recent(tmp_path / "journal.db", now=NOW) == []
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd ~/Documents/GitHub/daedalus && python -m pytest tests/test_memory_journal.py -x -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'daedalus.memory.journal'`

- [ ] **Step 4: Write the implementation**

Create `src/daedalus/memory/journal.py`:

```python
"""Conversation journal: the durable record the daily-learnings summarizer
reads. Checkpoints cannot serve this purpose -- a task's checkpoint row is
dropped the moment the task completes (agent.py, _loop), and checkpoints
carry neither timestamps in their payload nor a list API. This journal is
the opposite trade: append-only rows written AT completion, kept for
RETENTION_DAYS, pruned on every write.

It lives in journal.db beside the techne db on the agent's persistent
volume (see journal_path), NOT in ~/.cache -- the journal must survive pod
restarts to answer "what happened yesterday".

Best-effort by contract: the one runtime writer (Agent._journal_conversation)
wraps record() in try/except -- a journal failure must never fail the task
that produced the conversation.

Timestamps are ISO-8601 UTC strings; every stamp is written with the same
offset, so lexicographic comparison in SQL is chronologically correct.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

JOURNAL_FILENAME = "journal.db"
RETENTION_DAYS = 7

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS conversations ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "context_id TEXT NOT NULL, "
    "history TEXT NOT NULL, "
    "created_at TEXT NOT NULL)"
)


def journal_path(memory_db_path: str | Path) -> Path:
    """The journal lives beside the techne db, on the same volume."""
    return Path(memory_db_path).parent / JOURNAL_FILENAME


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(_SCHEMA)
    return conn


def record(
    path: str | Path,
    context_id: str,
    history: list[dict],
    *,
    now: datetime | None = None,
) -> None:
    """Append one completed conversation. `history` is a list of
    {"role": ..., "text": ...} dicts (Message.model_dump() shape). Rows
    older than RETENTION_DAYS are pruned in the same write, so the journal
    is self-limiting without any separate sweeper."""
    moment = now or datetime.now(UTC)
    cutoff = (moment - timedelta(days=RETENTION_DAYS)).isoformat()
    conn = _connect(Path(path))
    try:
        conn.execute(
            "INSERT INTO conversations (context_id, history, created_at) "
            "VALUES (?, ?, ?)",
            (context_id, json.dumps(history), moment.isoformat()),
        )
        conn.execute("DELETE FROM conversations WHERE created_at < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()


def recent(
    path: str | Path, *, hours: int = 24, now: datetime | None = None
) -> list[dict]:
    """Conversations from the last `hours`, oldest first. A missing journal
    file reads as no conversations, never an error; a row whose history
    fails to parse is skipped rather than sinking the read."""
    journal = Path(path)
    if not journal.exists():
        return []
    moment = now or datetime.now(UTC)
    cutoff = (moment - timedelta(hours=hours)).isoformat()
    conn = _connect(journal)
    try:
        rows = conn.execute(
            "SELECT context_id, history, created_at FROM conversations "
            "WHERE created_at >= ? ORDER BY created_at",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    out: list[dict] = []
    for context_id, history, created_at in rows:
        try:
            parsed = json.loads(history)
        except json.JSONDecodeError:
            continue
        out.append(
            {"context_id": context_id, "history": parsed, "created_at": created_at}
        )
    return out
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ~/Documents/GitHub/daedalus && python -m pytest tests/test_memory_journal.py -x -q`
Expected: 6 passed

- [ ] **Step 6: Lint and commit**

```bash
cd ~/Documents/GitHub/daedalus && ruff check src/daedalus/memory/journal.py tests/test_memory_journal.py
git -C ~/Documents/GitHub/daedalus add src/daedalus/memory/journal.py tests/test_memory_journal.py
git -C ~/Documents/GitHub/daedalus commit -m "feat(memory): conversation journal, the learnings summarizer's source"
```

---

### Task 2: Journal completed conversations from Agent (daedalus)

**Files:**
- Modify: `src/daedalus/agent.py` (the `handle_task` return path, around line 356, and a new `_journal_conversation` method)
- Test: `tests/test_agent_journal.py`

**Interfaces:**
- Consumes: Task 1's `journal.record`, `journal.journal_path`.
- Produces: every normally-completed model task (state `COMPLETED`, non-system-task) leaves one journal row. Task 3's handler reads those rows.

- [ ] **Step 1: Read the reference test file**

Read `tests/test_agent.py` lines 1-80 to learn the existing `make_agent` helper, `GOLDEN` bundle path, and `ScriptedLLM` fake. The test below reuses whatever construction pattern that file establishes -- adapt the construction lines (NOT the assertions) if the helper signatures differ from the sketch.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_agent_journal.py`:

```python
"""Completed conversations land in the journal; failures and system tasks
do not; a journal error never fails the task."""

import pytest

from daedalus.memory.journal import recent
from daedalus.task import Task, TaskState

# Reuse the existing agent-construction helpers. If test_agent.py exposes
# make_agent(llm=...) use it; otherwise construct via Agent.load(GOLDEN,
# llm=ScriptedLLM([...]), memory=...) exactly as that file does.
from tests.test_agent import GOLDEN, ScriptedLLM  # adapt to actual names

from daedalus.agent import Agent


class FakeMemory:
    """Just enough memory seam: a db_path for journal_path derivation and
    inert recall/commit."""

    def __init__(self, db_path):
        self.db_path = db_path

    async def recall(self, query, k=5):
        return []

    async def commit(self, content, kind="fact"):
        return None


@pytest.mark.asyncio
async def test_completed_task_journals_conversation(tmp_path):
    agent = Agent.load(
        GOLDEN, llm=ScriptedLLM(["hello there"]), memory=FakeMemory(tmp_path / "techne.db")
    )
    task = Task.from_text("hi")

    result = await agent.handle_task(task)

    assert result.state is TaskState.COMPLETED
    rows = recent(tmp_path / "journal.db")
    assert len(rows) == 1
    assert rows[0]["history"][0]["text"] == "hi"
    assert any(m["role"] == "agent" for m in rows[0]["history"])


@pytest.mark.asyncio
async def test_agent_without_memory_db_does_not_journal(tmp_path):
    agent = Agent.load(GOLDEN, llm=ScriptedLLM(["hello"]))  # NoMemory: no db_path
    task = Task.from_text("hi")

    result = await agent.handle_task(task)

    assert result.state is TaskState.COMPLETED
    assert not (tmp_path / "journal.db").exists()


@pytest.mark.asyncio
async def test_journal_failure_never_fails_the_task(tmp_path, monkeypatch):
    agent = Agent.load(
        GOLDEN, llm=ScriptedLLM(["hello"]), memory=FakeMemory(tmp_path / "techne.db")
    )

    def boom(*args, **kwargs):
        raise OSError("disk full")

    import daedalus.memory.journal as journal_module

    monkeypatch.setattr(journal_module, "record", boom)
    result = await agent.handle_task(Task.from_text("hi"))

    assert result.state is TaskState.COMPLETED
```

If `tests/test_agent.py` uses a different asyncio test convention (e.g. `anyio` or plain `asyncio.run` wrappers instead of `pytest.mark.asyncio`), follow that convention.

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd ~/Documents/GitHub/daedalus && python -m pytest tests/test_agent_journal.py -x -q`
Expected: FAIL -- first test's `recent(...)` returns `[]` (no journaling exists yet)

- [ ] **Step 4: Implement the hook**

In `src/daedalus/agent.py`, `handle_task` currently ends (around line 349-356):

```python
            consulted = consulted_colleagues.get() or []
            unnamed = [c for c in consulted if c not in result.agent_text()]
            if unnamed:
                result.set_agent_text(
                    result.agent_text()
                    + "\n\n_(consulted: " + ", ".join(consulted) + ")_"
                )
        return result
```

Change the tail to:

```python
            consulted = consulted_colleagues.get() or []
            unnamed = [c for c in consulted if c not in result.agent_text()]
            if unnamed:
                result.set_agent_text(
                    result.agent_text()
                    + "\n\n_(consulted: " + ", ".join(consulted) + ")_"
                )
        if result.state is TaskState.COMPLETED:
            await asyncio.to_thread(self._journal_conversation, result)
        return result
```

Add the method right after `handle_task` (before `_dispatch_system_task`):

```python
    def _journal_conversation(self, task: Task) -> None:
        """Append the completed conversation to the journal the learnings
        summarizer reads (daily-learnings spec). Best-effort by contract:
        the task already succeeded, and a journal failure must never turn
        that success into a failure. System tasks never reach here --
        handle_task dispatches them before the model path."""
        db_path = getattr(self.memory, "db_path", None)
        if db_path is None:
            return
        from daedalus.memory import journal

        try:
            journal.record(
                journal.journal_path(Path(db_path)),
                task.context_id or task.id,
                [message.model_dump() for message in task.history],
            )
        except Exception as exc:
            self._emit("journal_error", error=str(exc))
```

Check the imports at the top of `agent.py`: `asyncio` and `Path` are already imported (used by `_handle_teach` and `_checkpoints_from_spec`); add either if somehow absent.

- [ ] **Step 5: Run the new tests and the full suite**

Run: `cd ~/Documents/GitHub/daedalus && python -m pytest tests/test_agent_journal.py -x -q && python -m pytest tests/ -x -q`
Expected: all pass (the full-suite run proves no existing handle_task test broke)

- [ ] **Step 6: Lint and commit**

```bash
cd ~/Documents/GitHub/daedalus && ruff check src/daedalus/agent.py tests/test_agent_journal.py
git -C ~/Documents/GitHub/daedalus add src/daedalus/agent.py tests/test_agent_journal.py
git -C ~/Documents/GitHub/daedalus commit -m "feat(agent): journal completed conversations for the learnings summarizer"
```

---

### Task 3: The learnings system-task handler (daedalus)

**Files:**
- Create: `src/daedalus/memory/learnings.py`
- Modify: `src/daedalus/agent.py` (`_dispatch_system_task`, new `_handle_learnings`)
- Test: `tests/test_memory_learnings.py`, additions to `tests/test_agent_journal.py` OR a new `tests/test_agent_learnings.py`

**Interfaces:**
- Consumes: Task 1's `journal.recent`/`journal_path`; the agent's `self.llm.complete(messages) -> LLMResponse` and `self.memory.recall(query, k)`; `teach_authorized(task)` from `daedalus.memory.teach`.
- Produces: marker constant `LEARNINGS_MARKER = "learnings"`; `build_messages(persona: str, conversations: list[dict], known: list[str], *, on_date: str) -> list[dict]`; `parse_candidates(text: str) -> list[dict]`; `Agent._handle_learnings(task: Task) -> Task` replying `json.dumps({"candidates": [...]})`. kobo's Task 5/6 parse that JSON reply.

- [ ] **Step 1: Write the failing pure-function tests**

Create `tests/test_memory_learnings.py`:

```python
"""The learnings summarizer's pure halves: prompt assembly and reply
parsing. No techne, no model, no sqlite."""

import json

from daedalus.memory.learnings import (
    CONTENT_MAX_CHARS,
    MAX_CANDIDATES,
    build_messages,
    parse_candidates,
)

CONVS = [
    {
        "context_id": "slack:C1:171",
        "history": [
            {"role": "user", "text": "the canary gate is manual now"},
            {"role": "agent", "text": "noted"},
        ],
        "created_at": "2026-09-01T08:00:00+00:00",
    }
]


def test_build_messages_persona_is_system_and_transcript_in_user():
    messages = build_messages("You are wren.", CONVS, [], on_date="2026-09-01")

    assert messages[0] == {"role": "system", "content": "You are wren."}
    assert messages[1]["role"] == "user"
    assert "the canary gate is manual now" in messages[1]["content"]
    assert "2026-09-01" in messages[1]["content"]


def test_build_messages_includes_known_facts_block_only_when_present():
    without = build_messages("p", CONVS, [], on_date="2026-09-01")
    with_known = build_messages("p", CONVS, ["water is wet"], on_date="2026-09-01")

    assert "already know" not in without[1]["content"]
    assert "water is wet" in with_known[1]["content"]


def test_build_messages_budget_drops_oldest_first():
    big = "x" * 40_000
    convs = [
        {"context_id": f"c{i}", "history": [{"role": "user", "text": big}]}
        for i in range(3)
    ]
    messages = build_messages("p", convs, [], on_date="2026-09-01")

    body = messages[1]["content"]
    assert "c2" in body  # newest survives
    assert "c0" not in body  # oldest dropped


def test_parse_candidates_valid_json():
    reply = json.dumps(
        {
            "candidates": [
                {
                    "content": "the canary gate is manual",
                    "kind": "fact",
                    "source": "Slack thread, Sep 1",
                    "evidence": "gate is manual now",
                }
            ]
        }
    )
    parsed = parse_candidates(reply)

    assert len(parsed) == 1
    assert parsed[0]["kind"] == "fact"


def test_parse_candidates_strips_code_fences():
    reply = '```json\n{"candidates": [{"content": "c", "kind": "fact", "source": "s"}]}\n```'
    assert len(parse_candidates(reply)) == 1


def test_parse_candidates_garbage_reads_as_empty():
    assert parse_candidates("I could not find anything today.") == []
    assert parse_candidates("") == []
    assert parse_candidates('{"candidates": "nope"}') == []


def test_parse_candidates_drops_invalid_and_clips_long():
    reply = json.dumps(
        {
            "candidates": [
                {"content": "", "kind": "fact", "source": "s"},  # empty content
                {"content": "c", "kind": "opinion", "source": "s"},  # bad kind
                {"content": "c", "kind": "fact", "source": ""},  # no source
                {"content": "y" * 999, "kind": "fact", "source": "s"},  # clipped
            ]
        }
    )
    parsed = parse_candidates(reply)

    assert len(parsed) == 1
    assert len(parsed[0]["content"]) == CONTENT_MAX_CHARS


def test_parse_candidates_caps_count():
    reply = json.dumps(
        {
            "candidates": [
                {"content": f"c{i}", "kind": "fact", "source": "s"} for i in range(30)
            ]
        }
    )
    assert len(parse_candidates(reply)) == MAX_CANDIDATES
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Documents/GitHub/daedalus && python -m pytest tests/test_memory_learnings.py -x -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'daedalus.memory.learnings'`

- [ ] **Step 3: Write the module**

Create `src/daedalus/memory/learnings.py`:

```python
"""The learnings system task: summarize the journal's last day into
candidate memory snippets for the room's owner-approval queue
(daily-learnings spec, 2026-09-01). Pure functions here; the dispatch
lives on Agent (_handle_learnings) beside the other system-task handlers.

The reply is JSON in the task's reply text: {"candidates": [...]} -- the
a2a envelope carries reply text verbatim (transports/a2a.task_to_a2a), so
the room json.loads it back out on its own side, and re-validates every
field itself (two independent walls, kobo never trusts pod output).

Nothing here writes to techne. Proposing is not learning; only the room's
approval flow (which rides the teach lane) ever stores anything.
"""

from __future__ import annotations

import json

LEARNINGS_MARKER = "learnings"
MAX_CANDIDATES = 10
CONTENT_MAX_CHARS = 400
EVIDENCE_MAX_CHARS = 200
SOURCE_MAX_CHARS = 512
KINDS = ("fact", "procedure")
# Bound the transcript fed to the model. Oldest conversations are dropped
# first when over budget -- the freshest context is the most valuable.
TRANSCRIPT_BUDGET_CHARS = 60_000

_INSTRUCTIONS = """\
Review your conversations from the last day, shown below. Propose up to
{max_candidates} candidate memory snippets: durable facts or procedures you
learned that are worth keeping permanently. Skip anything you already know
(your recalled knowledge is listed when present), anything ephemeral (a
deploy in progress, someone's availability today), and anything you were
explicitly taught already.

Reply with ONLY a JSON object, no prose, in exactly this shape:
{{"candidates": [{{"content": "...", "kind": "fact",
"source": "...", "evidence": "..."}}]}}

- content: the fact or procedure, at most {content_max} characters
- kind: "fact" or "procedure"
- source: where it came from, in plain language, e.g.
  "Slack thread with jsmith, {date}"
- evidence: a short verbatim quote from the conversation supporting it,
  at most {evidence_max} characters

If nothing from the day is worth keeping, reply {{"candidates": []}}.
"""


def build_messages(
    persona: str,
    conversations: list[dict],
    known: list[str],
    *,
    on_date: str,
) -> list[dict]:
    """The one-shot summarization prompt: persona as system (the agent
    summarizes as itself), instructions + recalled knowledge + transcript
    as the single user turn."""
    instructions = _INSTRUCTIONS.format(
        max_candidates=MAX_CANDIDATES,
        content_max=CONTENT_MAX_CHARS,
        evidence_max=EVIDENCE_MAX_CHARS,
        date=on_date,
    )
    parts = [instructions]
    if known:
        parts.append(
            "## What you already know (do not re-propose)\n"
            + "\n".join(f"- {item}" for item in known)
        )
    parts.append(
        "## Conversations from the last day\n" + _render_transcript(conversations)
    )
    return [
        {"role": "system", "content": persona},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def _render_transcript(conversations: list[dict]) -> str:
    """Newest-preserving render: sections are gathered newest-first until
    the budget is spent, then emitted oldest-first so the model reads in
    chronological order. At least one section always survives."""
    sections: list[str] = []
    spent = 0
    for conv in reversed(conversations):
        lines = [f"### Conversation {conv.get('context_id', '?')}"]
        for message in conv.get("history", []):
            lines.append(f"{message.get('role', '?')}: {message.get('text', '')}")
        section = "\n".join(lines)
        if sections and spent + len(section) > TRANSCRIPT_BUDGET_CHARS:
            break
        sections.append(section)
        spent += len(section)
    return "\n\n".join(reversed(sections))


def parse_candidates(text: str) -> list[dict]:
    """The model's reply -> validated candidate dicts. Garbage in (no JSON,
    wrong shape, missing fields) reads as no candidates, never an error --
    the nightly run must survive any model output."""
    cleaned = text.strip()
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
    for item in raw[:MAX_CANDIDATES]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).strip()
        kind = str(item.get("kind", "")).strip()
        source = str(item.get("source", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        if not content or kind not in KINDS or not source:
            continue
        candidates.append(
            {
                "content": content[:CONTENT_MAX_CHARS],
                "kind": kind,
                "source": source[:SOURCE_MAX_CHARS],
                "evidence": evidence[:EVIDENCE_MAX_CHARS],
            }
        )
    return candidates
```

- [ ] **Step 4: Run pure-function tests**

Run: `cd ~/Documents/GitHub/daedalus && python -m pytest tests/test_memory_learnings.py -x -q`
Expected: 8 passed

- [ ] **Step 5: Write the failing dispatch tests**

Create `tests/test_agent_learnings.py` (same construction caveat as Task 2 Step 1 -- reuse `tests/test_agent.py` helpers):

```python
"""Agent dispatch of the learnings system task."""

import json

import pytest

from daedalus.memory.journal import journal_path, record
from daedalus.memory.learnings import LEARNINGS_MARKER
from daedalus.memory.teach import SYSTEM_TASK_KEY
from daedalus.task import Task, TaskState, VERIFIED_SUB_KEY

from tests.test_agent import GOLDEN, ScriptedLLM  # adapt to actual names

from daedalus.agent import Agent
from tests.test_agent_journal import FakeMemory


def _learnings_task(*, sub="service:room", date="2026-09-01"):
    task = Task.from_text("Summarize your conversations.")
    task.metadata[SYSTEM_TASK_KEY] = LEARNINGS_MARKER
    task.metadata["date"] = date
    if sub is not None:
        task.metadata[VERIFIED_SUB_KEY] = sub
    return task


GOOD_REPLY = json.dumps(
    {
        "candidates": [
            {
                "content": "the canary gate is manual",
                "kind": "fact",
                "source": "Slack thread, Sep 1",
                "evidence": "gate is manual now",
            }
        ]
    }
)


@pytest.mark.asyncio
async def test_unauthorized_learnings_refused(tmp_path):
    agent = Agent.load(
        GOLDEN, llm=ScriptedLLM([]), memory=FakeMemory(tmp_path / "techne.db")
    )
    result = await agent.handle_task(_learnings_task(sub=None))

    assert result.state is TaskState.FAILED


@pytest.mark.asyncio
async def test_empty_journal_short_circuits_without_model_call(tmp_path):
    # ScriptedLLM with no scripts raises if complete() is ever called --
    # the pass proves the model was never consulted.
    agent = Agent.load(
        GOLDEN, llm=ScriptedLLM([]), memory=FakeMemory(tmp_path / "techne.db")
    )
    result = await agent.handle_task(_learnings_task())

    assert result.state is TaskState.COMPLETED
    assert json.loads(result.agent_text()) == {"candidates": []}


@pytest.mark.asyncio
async def test_learnings_summarizes_journal_into_candidates(tmp_path):
    db = tmp_path / "techne.db"
    record(
        journal_path(db),
        "slack:C1:171",
        [{"role": "user", "text": "the canary gate is manual now"}],
    )
    agent = Agent.load(GOLDEN, llm=ScriptedLLM([GOOD_REPLY]), memory=FakeMemory(db))

    result = await agent.handle_task(_learnings_task())

    assert result.state is TaskState.COMPLETED
    parsed = json.loads(result.agent_text())
    assert len(parsed["candidates"]) == 1
    assert parsed["candidates"][0]["content"] == "the canary gate is manual"


@pytest.mark.asyncio
async def test_model_garbage_reads_as_empty_candidates(tmp_path):
    db = tmp_path / "techne.db"
    record(journal_path(db), "c", [{"role": "user", "text": "hello"}])
    agent = Agent.load(
        GOLDEN, llm=ScriptedLLM(["nothing structured here"]), memory=FakeMemory(db)
    )

    result = await agent.handle_task(_learnings_task())

    assert result.state is TaskState.COMPLETED
    assert json.loads(result.agent_text()) == {"candidates": []}
```

Note: `VERIFIED_SUB_KEY` lives in `daedalus.task` (module constant). Confirm the import path by checking how `tests/test_memory_teach.py` imports it and mirror that.

- [ ] **Step 6: Run to verify failure**

Run: `cd ~/Documents/GitHub/daedalus && python -m pytest tests/test_agent_learnings.py -x -q`
Expected: FAIL -- unknown system task `'learnings'` is refused (state FAILED where COMPLETED expected)

- [ ] **Step 7: Implement the dispatch and handler**

In `src/daedalus/agent.py`:

1. Extend the existing `daedalus.memory.teach` import block (where `TEACH_MARKER` etc. come from) with:

```python
from daedalus.memory.learnings import LEARNINGS_MARKER
```

2. Add `import json` to the module imports if absent.

3. In `_dispatch_system_task`, add the branch after the `TOOL_MARKER` line:

```python
        if marker == TOOL_MARKER:
            return await self._handle_tool(task)
        if marker == LEARNINGS_MARKER:
            return await self._handle_learnings(task)
```

4. Add the handler after `_handle_teach`:

```python
    async def _handle_learnings(self, task: Task) -> Task:
        """Summarize the journal's last day into candidate memory snippets
        (daily-learnings spec). Proposes only -- nothing is stored here;
        the room's owner-approval flow rides the teach lane for that.
        Replies JSON ({"candidates": [...]}) in the task's reply text,
        which task_to_a2a carries verbatim for the room to parse."""
        from daedalus.memory import journal, learnings

        if not teach_authorized(task):
            self._emit("learnings_refused", sub=task.metadata.get(VERIFIED_SUB_KEY))
            return task.reply(
                "Only the room may ask me to summarize my day.",
                state=TaskState.FAILED,
                reason=ReasonCode.POLICY_VIOLATION,
            )
        empty = json.dumps({"candidates": []})
        db_path = getattr(self.memory, "db_path", None)
        if db_path is None:
            return task.reply(empty)
        conversations = await asyncio.to_thread(
            journal.recent, journal.journal_path(Path(db_path))
        )
        if not conversations:
            return task.reply(empty)
        on_date = str(task.metadata.get("date", "")) or "today"
        # "What you already know": probe techne with the day's own text so
        # the model can skip re-proposing established knowledge.
        probe = " ".join(
            message.get("text", "")
            for conv in conversations
            for message in conv.get("history", [])
        )[:500]
        known = await self.memory.recall(probe, k=10)
        messages = learnings.build_messages(
            self.bundle.persona, conversations, known, on_date=on_date
        )
        try:
            response = await self.llm.complete(messages)
        except Exception as exc:
            self._emit("learnings_error", error=str(exc))
            return task.reply(
                "My summarizer failed today.",
                state=TaskState.FAILED,
                reason=ReasonCode.TOOL_BLOCKED,
            )
        candidates = learnings.parse_candidates(response.text or "")
        self._emit("learnings_proposed", count=len(candidates))
        return task.reply(json.dumps({"candidates": candidates}))
```

`teach_authorized`, `VERIFIED_SUB_KEY`, `ReasonCode`, `TaskState`, `asyncio`, and `Path` are already imported in `agent.py` (used by `_handle_teach`).

- [ ] **Step 8: Run the new tests and the full suite**

Run: `cd ~/Documents/GitHub/daedalus && python -m pytest tests/test_agent_learnings.py tests/test_memory_learnings.py -x -q && python -m pytest tests/ -x -q`
Expected: all pass

- [ ] **Step 9: Lint and commit**

```bash
cd ~/Documents/GitHub/daedalus && ruff check src tests
git -C ~/Documents/GitHub/daedalus add src/daedalus/memory/learnings.py src/daedalus/agent.py tests/test_memory_learnings.py tests/test_agent_learnings.py
git -C ~/Documents/GitHub/daedalus commit -m "feat(memory): learnings system task -- summarize the day into candidates"
```

---

### Task 4: AgentLearning model (kobo)

**Files:**
- Modify: `api/kobo_api/models.py` (append after `REPO_ACCESS_STATUSES`, line 530)
- Test: `api/tests/test_learnings.py` (new file, started here)

**Interfaces:**
- Consumes: existing `Base`, SQLAlchemy column types already imported in models.py.
- Produces: `AgentLearning` model (columns: id, agent_name, day, content, kind, source, evidence, content_hash, status, decided_by, decided_at, created_at; unique on `(agent_name, content_hash)`); constants `LEARNING_PENDING/APPROVED/DENIED/EXPIRED` and `LEARNING_STATUSES`. Tasks 5-7 import all of these from `kobo_api.models`.

- [ ] **Step 1: Create the branch**

```bash
git -C ~/Documents/GitHub/kobo checkout -b feat/daily-learnings
```

- [ ] **Step 2: Write the failing test**

Create `api/tests/test_learnings.py`:

```python
"""Daily learnings: model, validation, storage, lifecycle, scheduling."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from kobo_api.models import (
    LEARNING_PENDING,
    AgentLearning,
    Base,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    yield session
    session.close()


def _learning(**overrides):
    defaults = dict(
        agent_name="wren",
        day="2026-09-01",
        content="the canary gate is manual",
        kind="fact",
        source="Slack thread, Sep 1",
        evidence="gate is manual now",
        content_hash="a" * 64,
    )
    defaults.update(overrides)
    return AgentLearning(**defaults)


def test_learning_row_roundtrips_with_pending_default(session):
    session.add(_learning())
    session.commit()

    row = session.query(AgentLearning).one()

    assert row.status == LEARNING_PENDING
    assert row.decided_by is None
    assert isinstance(row.created_at, datetime)


def test_same_agent_same_hash_is_rejected(session):
    session.add(_learning())
    session.commit()
    session.add(_learning(day="2026-09-02"))

    with pytest.raises(IntegrityError):
        session.commit()
```

- [ ] **Step 3: Run to verify failure**

Run: `cd ~/Documents/GitHub/kobo/api && python -m pytest tests/test_learnings.py -x -q`
Expected: FAIL with `ImportError: cannot import name 'AgentLearning'`

- [ ] **Step 4: Add the model**

Append to `api/kobo_api/models.py` after line 530 (`REPO_ACCESS_STATUSES = ...`):

```python
class AgentLearning(Base):
    """One candidate memory snippet awaiting a human verdict (daily-
    learnings spec, 2026-09-01): the nightly summarizer's output parked
    for curation. Nothing in this table is memory -- a row becomes memory
    only when the approve route pushes it through the teach lane, and a
    denied or expired row is a tombstone whose hash blocks the identical
    content from ever being proposed again (the unique constraint below;
    the nightly upsert skips any existing hash regardless of status).

    Statuses live in LEARNING_STATUSES; `day` is the nightly run's ISO
    date; `content_hash` is SHA-256 of `content`. `agent_name` is a plain
    string, not a foreign key, same reasoning as `A2AInvite.from_agent`
    above.

    Postdates every table in this file, so `Base.metadata.create_all`
    (`kobo_api.db.init_db`) creates it for free -- same no-ALTER-needed
    convention `MCPToolCallLog` documents above.
    """

    __tablename__ = "agent_learnings"
    __table_args__ = (UniqueConstraint("agent_name", "content_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_name: Mapped[str] = mapped_column(String(100), nullable=False)
    day: Mapped[str] = mapped_column(String(10), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(512), nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="pending")
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )


LEARNING_PENDING = "pending"
LEARNING_APPROVED = "approved"
LEARNING_DENIED = "denied"
LEARNING_EXPIRED = "expired"
LEARNING_STATUSES = (
    LEARNING_PENDING,
    LEARNING_APPROVED,
    LEARNING_DENIED,
    LEARNING_EXPIRED,
)
```

- [ ] **Step 5: Run to verify pass**

Run: `cd ~/Documents/GitHub/kobo/api && python -m pytest tests/test_learnings.py -x -q`
Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
git -C ~/Documents/GitHub/kobo add api/kobo_api/models.py api/tests/test_learnings.py
git -C ~/Documents/GitHub/kobo commit -m "feat(learnings): agent_learnings table -- the curation queue"
```

---

### Task 5: Learnings room logic + relay envelope (kobo)

**Files:**
- Create: `api/kobo_api/learnings.py`
- Modify: `api/kobo_api/clients.py` (add `_build_learnings_envelope` right after `_build_teach_envelope`, line ~1154)
- Test: `api/tests/test_learnings.py` (extend)

**Interfaces:**
- Consumes: Task 4's model + constants; `_build_system_task_envelope` in clients.py.
- Produces (all in `kobo_api.learnings`): `content_hash(content: str) -> str`; `parse_reply(reply: str) -> list[dict]`; `store_candidates(session, agent_name: str, day: str, candidates: list[dict]) -> int`; `expire_stale(session, now: datetime) -> int`; `due_agents(session, ready_names: list[str], now: datetime) -> list[str]`; `seconds_until_next_utc_midnight(now: datetime) -> float`; constants `CANDIDATE_CAP=10`, `CONTENT_MAX_CHARS=400`, `EVIDENCE_MAX_CHARS=200`, `SOURCE_MAX_CHARS=512`, `KINDS`, `EXPIRY_DAYS=14`, `DUE_AFTER_HOURS=24`. In clients.py: `_build_learnings_envelope(on_date: str) -> dict`. Task 6 consumes all of these; Task 7 consumes the constants and model.

- [ ] **Step 1: Write the failing tests**

Append to `api/tests/test_learnings.py`:

```python
import json
from datetime import timedelta

from kobo_api.clients import _build_learnings_envelope
from kobo_api.learnings import (
    CANDIDATE_CAP,
    CONTENT_MAX_CHARS,
    content_hash,
    due_agents,
    expire_stale,
    parse_reply,
    seconds_until_next_utc_midnight,
    store_candidates,
)
from kobo_api.models import LEARNING_DENIED, LEARNING_EXPIRED

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _candidate(content="the canary gate is manual", **overrides):
    base = dict(content=content, kind="fact", source="Slack, Sep 1", evidence="quote")
    base.update(overrides)
    return base


def test_parse_reply_valid_and_garbage():
    good = json.dumps({"candidates": [_candidate()]})
    assert len(parse_reply(good)) == 1
    assert parse_reply("not json at all") == []
    assert parse_reply(json.dumps({"candidates": "x"})) == []


def test_parse_reply_clips_and_caps():
    long_one = json.dumps({"candidates": [_candidate(content="y" * 999)]})
    assert len(parse_reply(long_one)[0]["content"]) == CONTENT_MAX_CHARS
    many = json.dumps({"candidates": [_candidate(content=f"c{i}") for i in range(30)]})
    assert len(parse_reply(many)) == CANDIDATE_CAP


def test_store_candidates_skips_known_hash_any_status(session):
    stored = store_candidates(session, "wren", "2026-09-01", [_candidate()])
    assert stored == 1
    row = session.query(AgentLearning).one()
    row.status = LEARNING_DENIED
    session.commit()

    again = store_candidates(session, "wren", "2026-09-02", [_candidate()])

    assert again == 0
    assert session.query(AgentLearning).count() == 1


def test_store_candidates_hash_is_sha256_of_content(session):
    store_candidates(session, "wren", "2026-09-01", [_candidate()])
    row = session.query(AgentLearning).one()
    assert row.content_hash == content_hash("the canary gate is manual")


def test_expire_stale_flips_only_old_pending(session):
    session.add(_learning(content_hash="b" * 64))
    session.commit()
    old = session.query(AgentLearning).one()
    old.created_at = (NOW - timedelta(days=15)).replace(tzinfo=None)
    session.add(_learning(content_hash="c" * 64))
    session.commit()

    flipped = expire_stale(session, NOW)

    assert flipped == 1
    statuses = {r.content_hash[0]: r.status for r in session.query(AgentLearning).all()}
    assert statuses["b"] == LEARNING_EXPIRED
    assert statuses["c"] == LEARNING_PENDING


def test_due_agents_no_rows_or_stale_rows(session):
    session.add(_learning(agent_name="fresh", content_hash="d" * 64))
    session.commit()
    fresh_row = session.query(AgentLearning).filter_by(agent_name="fresh").one()
    fresh_row.created_at = (NOW - timedelta(hours=2)).replace(tzinfo=None)
    session.add(_learning(agent_name="stale", content_hash="e" * 64))
    session.commit()
    stale_row = session.query(AgentLearning).filter_by(agent_name="stale").one()
    stale_row.created_at = (NOW - timedelta(hours=30)).replace(tzinfo=None)
    session.commit()

    due = due_agents(session, ["fresh", "stale", "never"], NOW)

    assert due == ["stale", "never"]


def test_seconds_until_next_utc_midnight():
    assert seconds_until_next_utc_midnight(NOW) == 12 * 3600
    almost = datetime(2026, 9, 1, 23, 59, 59, tzinfo=UTC)
    assert seconds_until_next_utc_midnight(almost) == 60.0  # floor


def test_learnings_envelope_shape():
    envelope = _build_learnings_envelope("2026-09-01")

    assert envelope["metadata"]["system_task"] == "learnings"
    assert envelope["metadata"]["date"] == "2026-09-01"
    assert "2026-09-01" in envelope["status"]["message"]["parts"][0]["text"]
```

Note: the envelope test's `parts` path assumes `_to_a2a_message` produces `{"kind": "message", "role": ..., "parts": [{"kind": "text", "text": ...}]}` -- verify against an existing envelope test (grep `api/tests/` for `_build_teach_envelope`) and mirror its assertion style if it differs.

- [ ] **Step 2: Run to verify failure**

Run: `cd ~/Documents/GitHub/kobo/api && python -m pytest tests/test_learnings.py -x -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'kobo_api.learnings'`

- [ ] **Step 3: Write the module**

Create `api/kobo_api/learnings.py`:

```python
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
from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from kobo_api.models import (
    LEARNING_EXPIRED,
    LEARNING_PENDING,
    AgentLearning,
)

log = logging.getLogger("kobo.learnings")

CANDIDATE_CAP = 10
CONTENT_MAX_CHARS = 400
EVIDENCE_MAX_CHARS = 200
SOURCE_MAX_CHARS = 512
KINDS = ("fact", "procedure")
EXPIRY_DAYS = 14
DUE_AFTER_HOURS = 24


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


def parse_reply(reply: str) -> list[dict]:
    """The agent's JSON reply -> validated candidate dicts. Garbage in
    (prose, wrong shape, missing fields) reads as no candidates, never an
    error -- one agent's bad output must not sink the nightly sweep."""
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
    for item in raw[:CANDIDATE_CAP]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).strip()
        kind = str(item.get("kind", "")).strip()
        source = str(item.get("source", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        if not content or kind not in KINDS or not source:
            continue
        candidates.append(
            {
                "content": content[:CONTENT_MAX_CHARS],
                "kind": kind,
                "source": source[:SOURCE_MAX_CHARS],
                "evidence": evidence[:EVIDENCE_MAX_CHARS],
            }
        )
    return candidates


def store_candidates(
    session: Session, agent_name: str, day: str, candidates: list[dict]
) -> int:
    """Upsert candidates as pending rows, skipping any content_hash this
    agent has EVER seen (any status) -- denial and expiry are permanent
    tombstones against re-proposal. Returns how many rows were stored."""
    stored = 0
    for candidate in candidates[:CANDIDATE_CAP]:
        digest = content_hash(candidate["content"])
        exists = (
            session.query(AgentLearning)
            .filter_by(agent_name=agent_name, content_hash=digest)
            .first()
        )
        if exists is not None:
            continue
        session.add(
            AgentLearning(
                agent_name=agent_name,
                day=day,
                content=candidate["content"],
                kind=candidate["kind"],
                source=candidate["source"],
                evidence=candidate.get("evidence", ""),
                content_hash=digest,
            )
        )
        stored += 1
    session.commit()
    return stored


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
```

Add to `api/kobo_api/clients.py`, immediately after `_build_teach_envelope` (line ~1154):

```python
def _build_learnings_envelope(on_date: str) -> dict:
    """The learnings system task (daily-learnings spec): asks the agent to
    summarize its journal's last day into candidate snippets. The date
    rides metadata so the runtime's prompt can cite it; the text is only a
    human-readable label for transcripts."""
    return _build_system_task_envelope(
        "learnings",
        f"Summarize your conversations from {on_date} into candidate memory snippets.",
        metadata={"date": on_date},
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `cd ~/Documents/GitHub/kobo/api && python -m pytest tests/test_learnings.py -x -q`
Expected: all pass (10 tests)

- [ ] **Step 5: Commit**

```bash
git -C ~/Documents/GitHub/kobo add api/kobo_api/learnings.py api/kobo_api/clients.py api/tests/test_learnings.py
git -C ~/Documents/GitHub/kobo commit -m "feat(learnings): room-side validation, storage, lifecycle, envelope"
```

---

### Task 6: Nightly sweep + lifespan wiring (kobo)

**Files:**
- Modify: `api/kobo_api/learnings.py` (add `run_nightly`, `nightly_loop`)
- Modify: `api/kobo_api/main.py` (wrap `mcp_lifespan` in a `_room_lifespan` that owns the loop task; swap the `FastAPI(...)` lifespan argument, line ~1595)
- Test: `api/tests/test_learnings.py` (extend)

**Interfaces:**
- Consumes: Task 5's functions; `cabal_enabled`, `CabalError`, `_extract_relay_reply`, `_build_learnings_envelope` from clients.py; `Agent`, `AgentStatus` from models.
- Produces: `run_nightly(app) -> None` (sync, thread-run) and `nightly_loop(app)` (async, lifespan-owned). No other task consumes these -- they are the top of the chain.

- [ ] **Step 1: Read the fakes**

Read `api/tests/fakes.py` to see `FakeCabal`'s constructor/relay shape. The tests below use a purpose-built stub instead; confirm nothing in `fakes.py` is a better fit before writing it.

- [ ] **Step 2: Write the failing tests**

Append to `api/tests/test_learnings.py`:

```python
from types import SimpleNamespace

from kobo_api.clients import CabalError
from kobo_api.learnings import run_nightly
from kobo_api.models import Agent, AgentStatus


class StubCabal:
    """Relays are recorded; the reply is the same task_to_a2a shape the
    daedalus runtime actually returns."""

    def __init__(self, reply_text):
        self.reply_text = reply_text
        self.calls = []

    def relay(self, name, envelope):
        self.calls.append((name, envelope))
        if isinstance(self.reply_text, Exception):
            raise self.reply_text
        return {
            "kind": "task",
            "id": "t1",
            "status": {
                "state": "completed",
                "message": {
                    "kind": "message",
                    "role": "agent",
                    "parts": [{"kind": "text", "text": self.reply_text}],
                },
            },
            "history": [],
            "metadata": {},
        }


def _app(session_factory_fn, cabal, config):
    return SimpleNamespace(
        state=SimpleNamespace(
            config=config,
            clients=SimpleNamespace(cabal=cabal),
            session_factory=session_factory_fn,
        )
    )


def _seed_ready(session, name="wren"):
    session.add(
        Agent(name=name, status=AgentStatus.READY)
    )
    session.commit()


def test_run_nightly_stores_candidates_for_due_agent(session, monkeypatch):
    # session fixture gives one Session; run_nightly opens its own from the
    # factory, so hand it a factory returning this same session with a
    # no-op close.
    monkeypatch.setattr(session, "close", lambda: None)
    _seed_ready(session)
    reply = json.dumps({"candidates": [_candidate()]})
    cabal = StubCabal(reply)
    config = _cabal_enabled_config()

    run_nightly(_app(lambda: session, cabal, config))

    assert len(cabal.calls) == 1
    assert cabal.calls[0][0] == "wren"
    assert session.query(AgentLearning).filter_by(agent_name="wren").count() == 1


def test_run_nightly_skips_recently_summarized_agent(session, monkeypatch):
    monkeypatch.setattr(session, "close", lambda: None)
    _seed_ready(session)
    store_candidates(session, "wren", "2026-09-01", [_candidate()])
    cabal = StubCabal(json.dumps({"candidates": []}))

    run_nightly(_app(lambda: session, cabal, _cabal_enabled_config()))

    assert cabal.calls == []


def test_run_nightly_survives_relay_failure(session, monkeypatch):
    monkeypatch.setattr(session, "close", lambda: None)
    _seed_ready(session)
    cabal = StubCabal(CabalError("switchboard down"))

    run_nightly(_app(lambda: session, cabal, _cabal_enabled_config()))  # must not raise

    assert session.query(AgentLearning).count() == 0


def test_run_nightly_without_cabal_still_expires(session, monkeypatch):
    monkeypatch.setattr(session, "close", lambda: None)
    session.add(_learning(content_hash="f" * 64))
    session.commit()
    row = session.query(AgentLearning).one()
    row.created_at = (datetime.now(UTC) - timedelta(days=20)).replace(tzinfo=None)
    session.commit()

    run_nightly(_app(lambda: session, None, _cabal_disabled_config()))

    assert session.query(AgentLearning).one().status == LEARNING_EXPIRED
```

The `_cabal_enabled_config()` / `_cabal_disabled_config()` helpers: read `cabal_enabled` in `api/kobo_api/clients.py:2864` to see exactly which `Config` field it checks (almost certainly `config.cabal_url` truthiness), then write:

```python
from kobo_api.config import Config


def _cabal_enabled_config(tmp_path=None):
    return Config(cabal_url="http://cabal.test")


def _cabal_disabled_config():
    return Config()
```

Adjust `Config(...)` construction to match how `api/tests/test_web_teach.py`'s `config` fixture builds one (it may require `workdir=`). The `Agent` model may also require more non-null columns than `name`/`status` -- copy the seeding shape from `test_web_teach.py`'s `_seed_agent` if the bare version fails.

- [ ] **Step 3: Run to verify failure**

Run: `cd ~/Documents/GitHub/kobo/api && python -m pytest tests/test_learnings.py -x -q`
Expected: FAIL with `ImportError: cannot import name 'run_nightly'`

- [ ] **Step 4: Implement the sweep and loop**

Append to `api/kobo_api/learnings.py`:

```python
def run_nightly(app) -> None:
    """One sweep, synchronous (runs via asyncio.to_thread): expire stale
    pending rows, then relay a learnings task to every due ready agent and
    store what comes back. Any single agent's failure is logged and
    skipped -- the sweep never dies mid-fleet. With cabal unwired the
    sweep still expires (bookkeeping needs no relay) and does nothing
    else."""
    import httpx

    from kobo_api.clients import (
        CabalError,
        _build_learnings_envelope,
        _extract_relay_reply,
        cabal_enabled,
    )
    from kobo_api.models import Agent, AgentStatus

    now = datetime.now(UTC)
    config = app.state.config
    clients = app.state.clients
    session = app.state.session_factory()
    try:
        expire_stale(session, now)
        if not (cabal_enabled(config) and clients.cabal is not None):
            return
        ready = [
            row.name
            for row in session.query(Agent).filter_by(status=AgentStatus.READY).all()
        ]
        day = now.date().isoformat()
        for name in due_agents(session, ready, now):
            envelope = _build_learnings_envelope(day)
            try:
                raw = clients.cabal.relay(name, envelope)
                reply = _extract_relay_reply(raw)
            except (CabalError, httpx.HTTPError, ValueError) as exc:
                log.warning("learnings relay failed for %s: %s", name, exc)
                continue
            candidates = parse_reply(reply)
            stored = store_candidates(session, name, day, candidates)
            log.info(
                "learnings: %s proposed %d, stored %d new",
                name,
                len(candidates),
                stored,
            )
    finally:
        session.close()


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
```

In `api/kobo_api/main.py`:

1. Find the app construction (line ~1595): `app = FastAPI(title="kobo-api", lifespan=mcp_lifespan)`.

2. Directly above `create_app` (module level), add:

```python
@asynccontextmanager
async def _room_lifespan(app: FastAPI):
    """mcp_lifespan plus the room's own background work (the learnings
    nightly loop). TestClient instances used without `with` never enter
    lifespans at all, so unit tests see no background task unless they
    opt in."""
    async with mcp_lifespan(app):
        nightly = asyncio.create_task(learnings_nightly_loop(app))
        try:
            yield
        finally:
            nightly.cancel()
            with suppress(asyncio.CancelledError):
                await nightly
```

3. Swap the lifespan argument: `app = FastAPI(title="kobo-api", lifespan=_room_lifespan)`.

4. Imports: add to main.py's import block as needed --

```python
import asyncio
from contextlib import asynccontextmanager, suppress

from kobo_api.learnings import nightly_loop as learnings_nightly_loop
```

(Check each against the existing import block first; `main.py` may already import some.)

- [ ] **Step 5: Run new tests, then the whole API suite**

Run: `cd ~/Documents/GitHub/kobo/api && python -m pytest tests/test_learnings.py -x -q && python -m pytest tests/ -x -q`
Expected: all pass. The full-suite run is the proof the lifespan swap broke nothing (MCP tests exercise `mcp_lifespan` through `_room_lifespan` now).

- [ ] **Step 6: Commit**

```bash
git -C ~/Documents/GitHub/kobo add api/kobo_api/learnings.py api/kobo_api/main.py api/tests/test_learnings.py
git -C ~/Documents/GitHub/kobo commit -m "feat(learnings): nightly sweep, lifespan-owned"
```

---

### Task 7: Routes, decide gate, teach-lane approval (kobo)

**Files:**
- Modify: `api/kobo_api/teach.py` (`note_filename` gains a `prefix` parameter)
- Modify: `api/kobo_api/main.py` (payload model near line 373; `_decider_or_403` near `_owner_or_operator_or_403`; four routes after the teach route block ~line 4192; two new fields in `get_agent`'s dict ~line 2844)
- Test: `api/tests/test_web_learnings.py` (new), `api/tests/test_teach.py` (extend)

**Interfaces:**
- Consumes: Tasks 4-5's model/constants; existing `_visible_agent_or_404`, `_ready_agent_or_error`, `_owner_or_operator_or_403`, `format_note`, `note_filename`, `upload_intake_file`, `_build_teach_envelope`, `_teach_task_not_completed`, `_extract_relay_reply`, `_archive_teach_note`, `AgentUserGrant`.
- Produces: `GET /api/agents/{name}/learnings?status=...`, `POST .../learnings/{id}/approve`, `POST .../learnings/{id}/deny`, `POST .../learnings/bulk`; `pending_learnings: int` and `can_decide_learnings: bool` on `GET /api/agents/{name}`. Tasks 8-9 consume the routes and fields.

- [ ] **Step 1: Write the failing teach.py test**

Append to `api/tests/test_teach.py`:

```python
def test_note_filename_prefix_parameter():
    from kobo_api.teach import note_filename

    name = note_filename("the canary gate is manual", on_date="2026-09-01", prefix="learning")

    assert name.startswith("learning-20260901-")
    assert name.endswith(".md")
    # Stays inside intake's 40-chars-before-.md cap.
    assert len(name) - len(".md") <= 40
```

Run: `cd ~/Documents/GitHub/kobo/api && python -m pytest tests/test_teach.py -x -q`
Expected: FAIL with `TypeError: note_filename() got an unexpected keyword argument 'prefix'`

- [ ] **Step 2: Add the prefix parameter**

In `api/kobo_api/teach.py`, change the signature and return of `note_filename` (line 79 and 106):

```python
def note_filename(text: str, *, on_date: str, prefix: str = "chat") -> str:
```

and

```python
    return f"{prefix}-{compact_date}-{slug}.md"
```

Add one line to the docstring's first paragraph: `prefix` defaults to `"chat"` (the teach gesture); the learnings approve route passes `"learning"` so its notes read as `intake:learning-...` in techne provenance. Verify the cap comment still holds: "learning-" (9) + 8-digit date + "-" (1) = 18 fixed chars, slug capped at 20 keeps the name at 38 <= 40.

Run: `cd ~/Documents/GitHub/kobo/api && python -m pytest tests/test_teach.py -x -q`
Expected: PASS

- [ ] **Step 3: Write the failing route tests**

Create `api/tests/test_web_learnings.py`. Copy the fixture block from `api/tests/test_web_teach.py` verbatim (the `session_factory`, `config`, `_make_client`, `_seed_agent` helpers and the constants they use -- `AGENT_NAME`, `REPO`, `SHA`, `_CapturingGitHub`), then add:

```python
from datetime import UTC, datetime, timedelta

from kobo_api.learnings import store_candidates
from kobo_api.models import (
    LEARNING_APPROVED,
    LEARNING_DENIED,
    LEARNING_PENDING,
    AgentLearning,
)


def _seed_learning(session_factory, agent_name=AGENT_NAME, **overrides):
    session = session_factory()
    try:
        defaults = dict(
            agent_name=agent_name,
            day="2026-09-01",
            content="the canary gate is manual",
            kind="fact",
            source="Slack, Sep 1",
            evidence="quote",
            content_hash="a" * 64,
        )
        defaults.update(overrides)
        row = AgentLearning(**defaults)
        session.add(row)
        session.commit()
        return row.id
    finally:
        session.close()


def test_list_learnings_empty(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/learnings")

    assert response.status_code == 200
    assert response.json() == {"learnings": []}


def test_list_learnings_returns_rows_and_validates_status(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    _seed_learning(session_factory)

    listed = client.get(f"/api/agents/{AGENT_NAME}/learnings")
    assert listed.status_code == 200
    body = listed.json()["learnings"]
    assert len(body) == 1
    assert body[0]["content"] == "the canary gate is manual"
    assert body[0]["status"] == LEARNING_PENDING

    bogus = client.get(f"/api/agents/{AGENT_NAME}/learnings?status=nonsense")
    assert bogus.status_code == 422


def test_list_learnings_unknown_agent_404(session_factory, config):
    client = _make_client(session_factory, config)

    assert client.get("/api/agents/ghost/learnings").status_code == 404


def test_deny_flips_status_and_second_deny_conflicts(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    learning_id = _seed_learning(session_factory)

    first = client.post(f"/api/agents/{AGENT_NAME}/learnings/{learning_id}/deny")
    assert first.status_code == 200
    assert first.json()["status"] == LEARNING_DENIED

    second = client.post(f"/api/agents/{AGENT_NAME}/learnings/{learning_id}/deny")
    assert second.status_code == 409


def test_approve_rides_teach_lane_archive_fallback(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)
    learning_id = _seed_learning(session_factory)

    response = client.post(f"/api/agents/{AGENT_NAME}/learnings/{learning_id}/approve")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == LEARNING_APPROVED
    assert body["mode"] == "archive"
    assert body["filename"].startswith("learning-")
    # The note landed in bundle/intake with the taught content.
    assert any("the canary gate is manual" in c for c in github.intake_contents)


def test_approve_unknown_id_404(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.post(f"/api/agents/{AGENT_NAME}/learnings/999/approve")

    assert response.status_code == 404


def test_bulk_deny(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    first = _seed_learning(session_factory, content_hash="a" * 64)
    second = _seed_learning(session_factory, content="another fact", content_hash="b" * 64)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/learnings/bulk",
        json={"ids": [first, second], "action": "deny"},
    )

    assert response.status_code == 200
    results = response.json()["results"]
    assert [r["status_code"] for r in results] == [200, 200]


def test_agent_detail_carries_learnings_fields(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    _seed_learning(session_factory)

    body = client.get(f"/api/agents/{AGENT_NAME}").json()

    assert body["pending_learnings"] == 1
    assert body["can_decide_learnings"] is True  # IAP disabled: gate passes
```

Adapt `github.intake_contents` to `_CapturingGitHub`'s actual attribute name (read its definition in `test_web_teach.py:73-94`; it snapshots `bundle/intake/*` contents at commit time -- use whatever attribute it exposes).

- [ ] **Step 4: Run to verify failure**

Run: `cd ~/Documents/GitHub/kobo/api && python -m pytest tests/test_web_learnings.py -x -q`
Expected: FAIL with 404s on the learnings routes (routes don't exist yet)

- [ ] **Step 5: Implement the payload model, gate, routes, and detail fields**

All in `api/kobo_api/main.py`.

1. Near `TeachPayload` (line ~373), add:

```python
class LearningsBulkPayload(BaseModel):
    """Bulk verdict for the Learnings tab's select-all gesture: one action
    applied to many pending rows in one request."""

    ids: list[int]
    action: Literal["approve", "deny"]
```

(`Literal` may need adding to the typing import.)

2. Near `_owner_or_operator_or_403` (after line ~1320), add:

```python
def _decider_or_403(
    agent: Agent, request: Request, session: Session
) -> JSONResponse | None:
    """The learnings decide gate (daily-learnings spec): owner or operator
    (the standard mutation gate), widened by a per-user grant -- a grantee
    already sees and chats with this agent as if they owned it, and
    approving a learning is teaching, which any visible caller can already
    do; the extra bar here exists to keep READERS out (see-all,
    mutate-nothing), and readers hold no grants, so they fail this exactly
    as any other non-owner does."""
    gate = _owner_or_operator_or_403(agent, request)
    if gate is None:
        return None
    email = getattr(request.state, "iap_email", None)
    if email:
        grant = (
            session.query(AgentUserGrant)
            .filter(
                AgentUserGrant.agent_name == agent.name,
                func.lower(AgentUserGrant.email) == email.lower(),
            )
            .first()
        )
        if grant is not None:
            return None
    return gate
```

(`func` comes from sqlalchemy; `AgentUserGrant` is already imported by `_visible_agents` -- verify both in main.py's import block, add if missing. Same for `AgentLearning` and the `LEARNING_*` constants: extend the `kobo_api.models` import.)

3. After the teach route block (after line ~4191), inside `create_app`, add:

```python
    @app.get("/api/agents/{name}/learnings")
    def list_learnings(
        name: str,
        request: Request,
        status: str = "all",
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        agent = _visible_agent_or_404(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent
        if status != "all" and status not in LEARNING_STATUSES:
            return JSONResponse(
                {"error": f"unknown status {status!r}"}, status_code=422
            )
        query = session.query(AgentLearning).filter_by(agent_name=agent.name)
        if status != "all":
            query = query.filter_by(status=status)
        rows = query.order_by(AgentLearning.created_at.desc()).all()
        return JSONResponse(
            {
                "learnings": [
                    {
                        "id": row.id,
                        "day": row.day,
                        "content": row.content,
                        "kind": row.kind,
                        "source": row.source,
                        "evidence": row.evidence,
                        "status": row.status,
                        "decided_by": row.decided_by,
                        "created_at": (
                            row.created_at.isoformat() if row.created_at else None
                        ),
                    }
                    for row in rows
                ]
            }
        )

    def _decide_learning(
        name: str,
        learning_id: int,
        action: str,
        request: Request,
        background_tasks: BackgroundTasks,
        session: Session,
    ) -> JSONResponse:
        """Shared verdict path for approve/deny/bulk. Approve rides the
        teach lane byte-for-byte (instant relay when cabal is wired, the
        CI-triggering archive commit otherwise) with the learning- filename
        prefix, so techne provenance reads intake:learning-<date>-<slug>."""
        agent = _ready_agent_or_error(
            session,
            request,
            name,
            not_ready_message="agent is not ready for learnings decisions",
        )
        if isinstance(agent, JSONResponse):
            return agent
        gate = _decider_or_403(agent, request, session)
        if gate is not None:
            return gate
        row = (
            session.query(AgentLearning)
            .filter_by(agent_name=agent.name, id=learning_id)
            .first()
        )
        if row is None:
            return JSONResponse({"error": "no such learning"}, status_code=404)
        if row.status != LEARNING_PENDING:
            return JSONResponse(
                {"error": f"learning is already {row.status}"}, status_code=409
            )
        email = getattr(request.state, "iap_email", None)
        if action == "deny":
            row.status = LEARNING_DENIED
            row.decided_by = email
            row.decided_at = datetime.now(UTC)
            session.commit()
            return JSONResponse({"id": row.id, "status": row.status})

        on_date = datetime.now(UTC).date().isoformat()
        note = format_note(row.content, taught_by=email, on_date=on_date)
        filename = note_filename(row.content, on_date=on_date, prefix="learning")
        cfg: Config = app.state.config
        clients = app.state.clients
        mode = "archive"
        if cabal_enabled(cfg) and clients.cabal is not None:
            envelope = _build_teach_envelope(note, filename)
            try:
                raw = clients.cabal.relay(agent.name, envelope)
                if _teach_task_not_completed(raw):
                    raise CabalError("agent reply was not a completed task")
                _extract_relay_reply(raw)
            except (CabalError, httpx.HTTPError, ValueError) as exc:
                log.warning(
                    "learning approve relay failed for %s, archive lane: %s",
                    name,
                    exc,
                )
            else:
                mode = "instant"
                background_tasks.add_task(
                    _archive_teach_note, agent.repo_full_name, filename, note
                )
        if mode == "archive":
            try:
                upload_intake_file(
                    app.state.clients.github,
                    agent.repo_full_name,
                    filename,
                    note,
                    runtime_image=cfg.runtime_image,
                )
            except (IntakeError, ClientError) as exc:
                log.warning("learning approve archive failed for %s: %s", name, exc)
                return JSONResponse(
                    {"error": "couldn't deliver the approved learning. Try again."},
                    status_code=502,
                )
        row.status = LEARNING_APPROVED
        row.decided_by = email
        row.decided_at = datetime.now(UTC)
        session.commit()
        return JSONResponse(
            {"id": row.id, "status": row.status, "mode": mode, "filename": filename}
        )

    @app.post("/api/agents/{name}/learnings/{learning_id}/approve")
    def approve_learning(
        name: str,
        learning_id: int,
        request: Request,
        background_tasks: BackgroundTasks,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        return _decide_learning(
            name, learning_id, "approve", request, background_tasks, session
        )

    @app.post("/api/agents/{name}/learnings/{learning_id}/deny")
    def deny_learning(
        name: str,
        learning_id: int,
        request: Request,
        background_tasks: BackgroundTasks,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        return _decide_learning(
            name, learning_id, "deny", request, background_tasks, session
        )

    @app.post("/api/agents/{name}/learnings/bulk")
    def bulk_decide_learnings(
        name: str,
        payload: LearningsBulkPayload,
        request: Request,
        background_tasks: BackgroundTasks,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        results = [
            {
                "id": learning_id,
                "status_code": _decide_learning(
                    name, learning_id, payload.action, request, background_tasks, session
                ).status_code,
            }
            for learning_id in payload.ids[:50]
        ]
        return JSONResponse({"results": results})
```

4. In `get_agent`'s response dict (line ~2823-2844), add two keys before the closing brace:

```python
            "pending_learnings": session.query(AgentLearning)
            .filter_by(agent_name=agent.name, status=LEARNING_PENDING)
            .count(),
            "can_decide_learnings": _decider_or_403(agent, request, session) is None,
```

- [ ] **Step 6: Run the new tests, then the whole API suite**

Run: `cd ~/Documents/GitHub/kobo/api && python -m pytest tests/test_web_learnings.py tests/test_teach.py -x -q && python -m pytest tests/ -x -q`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git -C ~/Documents/GitHub/kobo add api/kobo_api/main.py api/kobo_api/teach.py api/tests/test_web_learnings.py api/tests/test_teach.py
git -C ~/Documents/GitHub/kobo commit -m "feat(learnings): routes -- list, approve via teach lane, deny, bulk, detail fields"
```

---

### Task 8: UI plumbing -- api.ts, queries.ts, tabs.ts (kobo)

**Files:**
- Modify: `ui/src/lib/api.ts` (types + three fetch functions; two fields on `AgentDetail`)
- Modify: `ui/src/lib/queries.ts` (query + two mutation factories; re-export `Learning`)
- Modify: `ui/src/lib/tabs.ts` (`TabId`, `TAB_LABELS`, `visibleTabs`)
- Test: `ui/src/lib/tabs.test.ts` (update the order pin)

**Interfaces:**
- Consumes: Task 7's routes and detail fields.
- Produces: `Learning` type; `listLearnings(name)`, `decideLearning(name, id, action)`, `decideLearningsBulk(name, ids, action)` in api.ts; `learningsQuery(name)` (key `["agents", name, "learnings"]`), `decideLearningMutation(queryClient, name)`, `decideLearningsBulkMutation(queryClient, name)` in queries.ts; `"learnings"` TabId. Task 9 consumes all of these.

- [ ] **Step 1: Update the tabs test (failing first)**

In `ui/src/lib/tabs.test.ts`, change the order pin to:

```ts
    expect(visibleTabs()).toEqual([
      "overview",
      "chat",
      "memory",
      "learnings",
      "skills",
      "tooling",
      "slack",
    ]);
```

Run: `cd ~/Documents/GitHub/kobo/ui && npx vitest run src/lib/tabs.test.ts`
Expected: FAIL (learnings not in the list)

- [ ] **Step 2: Update tabs.ts**

In `ui/src/lib/tabs.ts`:

```ts
export type TabId =
  | "overview"
  | "chat"
  | "memory"
  | "learnings"
  | "skills"
  | "tooling"
  | "slack"
  | "access";

export const TAB_LABELS: Record<TabId, string> = {
  overview: "Overview",
  chat: "Chat",
  memory: "Memory",
  learnings: "Learnings",
  skills: "Skills",
  tooling: "Tooling",
  slack: "Slack",
  access: "Access",
};
```

and in `visibleTabs`:

```ts
  const base: TabId[] = ["overview", "chat", "memory", "learnings", "skills", "tooling", "slack"];
```

Run: `cd ~/Documents/GitHub/kobo/ui && npx vitest run src/lib/tabs.test.ts`
Expected: PASS

- [ ] **Step 3: Add api.ts types and functions**

In `ui/src/lib/api.ts`, near the other agent-scoped functions (after `listKnowledge`-adjacent code is fine; keep the file's grouping conventions):

```ts
/** One candidate memory snippet awaiting a verdict (daily-learnings
 * spec): proposed nightly by the agent from its own conversation journal,
 * parked by the room, inert until a human approves it into memory. */
export type Learning = {
  id: number;
  day: string;
  content: string;
  kind: "fact" | "procedure";
  source: string;
  evidence: string;
  status: "pending" | "approved" | "denied" | "expired";
  decided_by: string | null;
  created_at: string | null;
};

export type LearningsList = { learnings: Learning[] };

export async function listLearnings(name: string): Promise<LearningsList> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/learnings?status=all`,
  );
  return parse<LearningsList>(res);
}

export async function decideLearning(
  name: string,
  id: number,
  action: "approve" | "deny",
): Promise<void> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/learnings/${id}/${action}`,
    { method: "POST" },
  );
  await parse(res);
}

export async function decideLearningsBulk(
  name: string,
  ids: number[],
  action: "approve" | "deny",
): Promise<void> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/learnings/bulk`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, action }),
    },
  );
  await parse(res);
}
```

And extend `AgentDetail` (line ~50) with:

```ts
  /** Count of pending learnings awaiting a verdict; the Learnings tab
   * badge reads this, never a client-side inference. */
  pending_learnings: number;
  /** Whether this caller may approve/deny learnings, computed server-side
   * by the decide gate (destroy_visible precedent): the panel hides its
   * verdict buttons off this; the route stays the real enforcement. */
  can_decide_learnings: boolean;
```

- [ ] **Step 4: Add queries.ts factories**

In `ui/src/lib/queries.ts`, following the `knowledgeQuery`/`saveKnowledgeMutation` pattern (line ~543):

```ts
/** The agent's learnings queue, every status -- the panel splits pending
 * from history itself. No polling; verdicts invalidate. */
export function learningsQuery(name: string) {
  return queryOptions({
    queryKey: ["agents", name, "learnings"] as const,
    queryFn: () => listLearnings(name),
  });
}

/** One verdict. Invalidates ["agents", name] -- prefix-matching covers
 * both the learnings list and the detail (whose pending_learnings badge
 * count changed). */
export function decideLearningMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: ({ id, action }: { id: number; action: "approve" | "deny" }) =>
      decideLearning(name, id, action),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name] });
    },
  };
}

/** The select-all gesture: many verdicts, one request, same invalidation. */
export function decideLearningsBulkMutation(
  queryClient: QueryClient,
  name: string,
) {
  return {
    mutationFn: ({ ids, action }: { ids: number[]; action: "approve" | "deny" }) =>
      decideLearningsBulk(name, ids, action),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name] });
    },
  };
}
```

Add `listLearnings`, `decideLearning`, `decideLearningsBulk` to the imports from `./api`, and `Learning` to the type re-exports at the bottom of queries.ts (line ~1230+).

- [ ] **Step 5: Type-check and run the UI suite**

Run: `cd ~/Documents/GitHub/kobo/ui && npx svelte-check --threshold error 2>/dev/null || npx tsc --noEmit; npx vitest run`
Expected: no new type errors; existing suite passes. (One expected failure class: any test that pins `AgentDetail` fixtures may now need `pending_learnings: 0, can_decide_learnings: false` added -- fix those fixtures.)

- [ ] **Step 6: Commit**

```bash
git -C ~/Documents/GitHub/kobo add ui/src/lib/api.ts ui/src/lib/queries.ts ui/src/lib/tabs.ts ui/src/lib/tabs.test.ts
git -C ~/Documents/GitHub/kobo commit -m "feat(ui): learnings api client, query factories, tab"
```

---

### Task 9: LearningsPanel + AgentDetail wiring + badge (kobo)

**Files:**
- Create: `ui/src/components/LearningsPanel.svelte`
- Modify: `ui/src/pages/AgentDetail.svelte` (import, tabpanel branch, tab badge, badge CSS)
- Test: `ui/src/components/LearningsPanel.test.ts`

**Interfaces:**
- Consumes: Task 8's `learningsQuery`, `decideLearningMutation`, `decideLearningsBulkMutation`, `Learning`; `data.pending_learnings`, `data.can_decide_learnings` from the detail payload.
- Produces: the user-facing Learnings tab. Nothing downstream.

- [ ] **Step 1: Write the failing component tests**

Create `ui/src/components/LearningsPanel.test.ts` (mirror `MemoryPanel.test.ts`'s harness: `renderWithClient`/`stubFetch` from `../test-utils/render`, `vi.unstubAllGlobals()` in `beforeEach`):

```ts
// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";

import LearningsPanel from "./LearningsPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const LEARNINGS = {
  learnings: [
    {
      id: 1,
      day: "2026-09-01",
      content: "the canary gate is manual",
      kind: "fact",
      source: "Slack thread, Sep 1",
      evidence: "gate is manual now",
      status: "pending",
      decided_by: null,
      created_at: "2026-09-01T05:00:00",
    },
    {
      id: 2,
      day: "2026-08-31",
      content: "old denied thing",
      kind: "fact",
      source: "Slack",
      evidence: "",
      status: "denied",
      decided_by: "kyle@example.test",
      created_at: "2026-08-31T05:00:00",
    },
  ],
};

describe("LearningsPanel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch({ "/learnings": LEARNINGS });
  });

  it("renders pending candidates grouped by day", async () => {
    renderWithClient(LearningsPanel, {
      agentName: "wren",
      canDecide: true,
      frozen: false,
    });

    expect(await screen.findByText("the canary gate is manual")).not.toBeNull();
    expect(screen.getByText(/2026-09-01/)).not.toBeNull();
    // Denied row lives in history, not in the pending list.
    expect(screen.queryByText("old denied thing")).toBeNull();
  });

  it("hides verdict buttons when the caller cannot decide", async () => {
    renderWithClient(LearningsPanel, {
      agentName: "wren",
      canDecide: false,
      frozen: false,
    });

    await screen.findByText("the canary gate is manual");
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Deny" })).toBeNull();
  });

  it("posts a deny verdict", async () => {
    const calls: string[] = [];
    stubFetch({
      "/learnings/1/deny": (url: string) => {
        calls.push(url);
        return { id: 1, status: "denied" };
      },
      "/learnings": LEARNINGS,
    });
    renderWithClient(LearningsPanel, {
      agentName: "wren",
      canDecide: true,
      frozen: false,
    });

    await screen.findByText("the canary gate is manual");
    await fireEvent.click(screen.getByRole("button", { name: "Deny" }));

    expect(calls.length).toBe(1);
  });

  it("shows an empty state when nothing is pending", async () => {
    stubFetch({ "/learnings": { learnings: [] } });
    renderWithClient(LearningsPanel, {
      agentName: "wren",
      canDecide: true,
      frozen: false,
    });

    expect(
      await screen.findByText(/Nothing waiting for review/),
    ).not.toBeNull();
  });
});
```

Adapt `stubFetch`'s value convention (static object vs function) to what `render.ts` actually supports -- read it first; if it only supports static JSON bodies, assert the POST via `vi.spyOn`-style fetch inspection the way other panel tests assert mutations.

- [ ] **Step 2: Run to verify failure**

Run: `cd ~/Documents/GitHub/kobo/ui && npx vitest run src/components/LearningsPanel.test.ts`
Expected: FAIL (component does not exist)

- [ ] **Step 3: Write the component**

Create `ui/src/components/LearningsPanel.svelte`. Copy conventions from `MemoryPanel.svelte` (thunk-wrapped `createQuery`/`createMutation` with `// svelte-ignore state_referenced_locally`, BEM CSS in a scoped style block). REMEMBER: no em dashes anywhere in this file (copy.test.ts).

```svelte
<script lang="ts">
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import {
    decideLearningMutation,
    decideLearningsBulkMutation,
    learningsQuery,
  } from "../lib/queries";
  import type { Learning } from "../lib/queries";

  let {
    agentName,
    canDecide = false,
    frozen = false,
  }: { agentName: string; canDecide?: boolean; frozen?: boolean } = $props();

  const queryClient = useQueryClient();
  // svelte-ignore state_referenced_locally
  const learnings = createQuery(() => (learningsQuery(agentName)));
  // svelte-ignore state_referenced_locally
  const decide = createMutation(() => (decideLearningMutation(queryClient, agentName)));
  // svelte-ignore state_referenced_locally
  const bulkDecide = createMutation(() => (
    decideLearningsBulkMutation(queryClient, agentName)
  ));

  let showHistory = $state(false);
  let errorText = $state("");

  const all = $derived(learnings.data?.learnings ?? []);
  const pending = $derived(all.filter((l: Learning) => l.status === "pending"));
  const history = $derived(all.filter((l: Learning) => l.status !== "pending"));
  const days = $derived([...new Set(pending.map((l: Learning) => l.day))]);

  const busy = $derived(decide.isPending || bulkDecide.isPending);

  function decideOne(id: number, action: "approve" | "deny") {
    errorText = "";
    decide.mutate(
      { id, action },
      { onError: (e: unknown) => (errorText = String(e)) },
    );
  }

  function decideDay(day: string, action: "approve" | "deny") {
    errorText = "";
    const ids = pending
      .filter((l: Learning) => l.day === day)
      .map((l: Learning) => l.id);
    bulkDecide.mutate(
      { ids, action },
      { onError: (e: unknown) => (errorText = String(e)) },
    );
  }
</script>

<section class="learnings-panel">
  <p class="learnings-panel__explainer">
    Each night your agent reviews its own conversations and proposes
    snippets worth remembering. Nothing is saved until you approve it:
    approve teaches it to the agent permanently, deny makes sure the same
    snippet is never proposed again.
  </p>

  {#if errorText}
    <p class="learnings-panel__error" role="alert">{errorText}</p>
  {/if}

  {#if learnings.isLoading}
    <p class="learnings-panel__empty">Loading...</p>
  {:else if pending.length === 0}
    <p class="learnings-panel__empty">
      Nothing waiting for review. New candidates appear here after your
      agent's next nightly summary.
    </p>
  {:else}
    {#each days as day (day)}
      <div class="learnings-day">
        <div class="learnings-day__header">
          <h3 class="learnings-day__title">{day}</h3>
          {#if canDecide}
            <div class="learnings-day__bulk">
              <button
                type="button"
                class="learnings-panel__button"
                disabled={frozen || busy}
                onclick={() => decideDay(day, "approve")}
              >
                Approve all
              </button>
              <button
                type="button"
                class="learnings-panel__button learnings-panel__button--deny"
                disabled={frozen || busy}
                onclick={() => decideDay(day, "deny")}
              >
                Deny all
              </button>
            </div>
          {/if}
        </div>
        {#each pending.filter((l: Learning) => l.day === day) as learning (learning.id)}
          <article class="learning-card">
            <span class="learning-card__kind">{learning.kind}</span>
            <p class="learning-card__content">{learning.content}</p>
            <p class="learning-card__source">Source: {learning.source}</p>
            {#if learning.evidence}
              <p class="learning-card__evidence">"{learning.evidence}"</p>
            {/if}
            {#if canDecide}
              <div class="learning-card__actions">
                <button
                  type="button"
                  class="learnings-panel__button"
                  disabled={frozen || busy}
                  onclick={() => decideOne(learning.id, "approve")}
                >
                  Approve
                </button>
                <button
                  type="button"
                  class="learnings-panel__button learnings-panel__button--deny"
                  disabled={frozen || busy}
                  onclick={() => decideOne(learning.id, "deny")}
                >
                  Deny
                </button>
              </div>
            {/if}
          </article>
        {/each}
      </div>
    {/each}
  {/if}

  {#if history.length > 0}
    <button
      type="button"
      class="learnings-panel__history-toggle"
      aria-expanded={showHistory}
      onclick={() => (showHistory = !showHistory)}
    >
      History ({history.length})
    </button>
    {#if showHistory}
      {#each history as learning (learning.id)}
        <article class="learning-card learning-card--decided">
          <span class="learning-card__kind">{learning.status}</span>
          <p class="learning-card__content">{learning.content}</p>
          {#if learning.decided_by}
            <p class="learning-card__source">Decided by {learning.decided_by}</p>
          {/if}
        </article>
      {/each}
    {/if}
  {/if}
</section>

<style>
  .learnings-panel__explainer {
    color: var(--text-muted, #667);
    margin: 0 0 1rem;
  }
  .learnings-panel__error {
    color: var(--danger, #b23);
  }
  .learnings-panel__empty {
    color: var(--text-muted, #667);
  }
  .learnings-day {
    margin-bottom: 1.5rem;
  }
  .learnings-day__header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
  }
  .learnings-day__title {
    margin: 0 0 0.5rem;
    font-size: 0.95rem;
  }
  .learnings-day__bulk {
    display: flex;
    gap: 0.5rem;
  }
  .learning-card {
    border: 1px solid var(--border, #dde);
    border-radius: 8px;
    padding: 0.75rem 1rem;
    margin-bottom: 0.75rem;
  }
  .learning-card--decided {
    opacity: 0.7;
  }
  .learning-card__kind {
    display: inline-block;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted, #667);
  }
  .learning-card__content {
    margin: 0.25rem 0;
  }
  .learning-card__source,
  .learning-card__evidence {
    margin: 0.25rem 0 0;
    font-size: 0.85rem;
    color: var(--text-muted, #667);
  }
  .learning-card__actions {
    display: flex;
    gap: 0.5rem;
    margin-top: 0.5rem;
  }
  .learnings-panel__button {
    cursor: pointer;
  }
  .learnings-panel__button--deny {
    color: var(--danger, #b23);
  }
  .learnings-panel__history-toggle {
    margin-top: 1rem;
    cursor: pointer;
  }
</style>
```

Match the CSS variable names to what MemoryPanel actually uses (read its style block; if the codebase uses different tokens, use those). Follow existing button classes if a shared button style exists.

- [ ] **Step 4: Wire into AgentDetail**

In `ui/src/pages/AgentDetail.svelte`:

1. Import: `import LearningsPanel from "../components/LearningsPanel.svelte";`

2. In the tabpanel `{#if}` chain (after the `memory` branch):

```svelte
          {:else if tab === "learnings"}
            <LearningsPanel
              agentName={data.name}
              canDecide={data.can_decide_learnings}
              frozen={isFrozen(data.status)}
            />
```

3. In the tab-button loop, change the label line from `{TAB_LABELS[tab]}` to:

```svelte
            {TAB_LABELS[tab]}
            {#if tab === "learnings" && data.pending_learnings > 0}
              <span class="detail-tabs__badge">{data.pending_learnings}</span>
            {/if}
```

4. Add badge CSS beside the existing `.detail-tabs__tab` rules:

```css
  .detail-tabs__badge {
    display: inline-block;
    min-width: 1.2em;
    margin-left: 0.35em;
    padding: 0 0.35em;
    border-radius: 999px;
    background: var(--accent, #4a6cf7);
    color: #fff;
    font-size: 0.72rem;
    line-height: 1.5;
    text-align: center;
  }
```

- [ ] **Step 5: Run the panel tests and the whole UI suite**

Run: `cd ~/Documents/GitHub/kobo/ui && npx vitest run src/components/LearningsPanel.test.ts && npx vitest run`
Expected: all pass, including `copy.test.ts` (no em dashes) and any AgentDetail tests (fixtures may need the two new AgentDetail fields from Task 8 Step 5).

- [ ] **Step 6: Commit**

```bash
git -C ~/Documents/GitHub/kobo add ui/src/components/LearningsPanel.svelte ui/src/components/LearningsPanel.test.ts ui/src/pages/AgentDetail.svelte
git -C ~/Documents/GitHub/kobo commit -m "feat(ui): learnings tab -- day-grouped verdict cards with pending badge"
```

---

### Task 10: Skills ledger reconciliation (kobo, rider bugfix)

Independent of learnings: skills merged directly into an agent's repo
(`bundle/skills/<id>/`, the direct-PR lane) are live on the agent but
invisible in kobo, because `GET /api/agents/{name}/skills` reads only the
`agent.skills` DB column (main.py:3652's own comment: "purely a DB-column
read"). The bundle is the runtime truth; reconcile the ledger on read.

**Files:**
- Modify: `api/kobo_api/skills.py` (add `list_repo_skills`)
- Modify: `api/kobo_api/main.py` (`list_skills` route, line ~3644)
- Test: `api/tests/test_web_skills.py` (or wherever the existing skills
  route tests live -- find with `grep -rl "agents/.*/skills" api/tests/`)

**Interfaces:**
- Consumes: existing `GitHubClient.clone`, `ClientError`, the `agent.skills`
  JSON column, `_skill_source`.
- Produces: `list_repo_skills(github: GitHubClient, repo: str) -> list[str]`
  (sorted skill ids present in the repo's bundle). The route's response
  shape is unchanged -- same keys, now truthful.

- [ ] **Step 1: Read the existing skills route tests**

Find and read the test file covering `GET /api/agents/{name}/skills` and
the local-skill routes. Note how its FakeGitHub seeds a cloneable tree
(the local-skill read/save tests must already materialize
`bundle/skills/<id>/SKILL.md` somewhere) -- the new tests reuse that
seeding shape exactly.

- [ ] **Step 2: Write the failing tests**

Add to that test file (adapt fixture/seeding names to what Step 1 found):

```python
def test_list_skills_surfaces_repo_only_skill_and_heals_ledger(session_factory, config):
    # Seed the fake repo with bundle/skills/pr-added/SKILL.md but leave
    # agent.skills WITHOUT a matching entry -- the direct-PR lane.
    github = _github_with_skill_tree({"pr-added": "---\nname: pr-added\ndescription: d\n---\nbody"})
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)  # agent.skills == []

    response = client.get(f"/api/agents/{AGENT_NAME}/skills")

    assert response.status_code == 200
    skills = response.json()["skills"]
    assert [s["name"] for s in skills] == ["pr-added"]
    assert skills[0]["source"] == "local"
    assert skills[0]["editable"] is True
    # The ledger healed: a second read that CANNOT reach the repo still
    # knows the skill.
    session = session_factory()
    try:
        agent = session.query(Agent).filter_by(name=AGENT_NAME).one()
        assert [e["name"] for e in agent.skills] == ["pr-added"]
    finally:
        session.close()


def test_list_skills_prunes_ledger_rows_missing_from_repo(session_factory, config):
    github = _github_with_skill_tree({})  # empty bundle/skills/
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config, skills=[{"name": "ghost", "source": "local"}])

    response = client.get(f"/api/agents/{AGENT_NAME}/skills")

    assert response.json()["skills"] == []


def test_list_skills_repo_failure_degrades_to_ledger_and_never_prunes(
    session_factory, config
):
    github = _FailingCloneGitHub()  # clone raises ClientError
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config, skills=[{"name": "kept", "source": "local"}])

    response = client.get(f"/api/agents/{AGENT_NAME}/skills")

    assert response.status_code == 200
    assert [s["name"] for s in response.json()["skills"]] == ["kept"]
```

Write `_github_with_skill_tree` / `_FailingCloneGitHub` by subclassing the
same FakeGitHub the file already uses, mirroring how its existing clone
fake materializes files. If `_seed_agent` lacks a `skills=` parameter, add
one (default `[]`) or set the column after seeding.

- [ ] **Step 3: Run to verify failure**

Run: `cd ~/Documents/GitHub/kobo/api && python -m pytest tests/<skills test file> -x -q`
Expected: the first two tests FAIL (repo-only skill invisible; ghost row survives)

- [ ] **Step 4: Implement**

Add to `api/kobo_api/skills.py` (beside `read_local_skill`):

```python
def list_repo_skills(github: GitHubClient, repo: str) -> list[str]:
    """Every skill id present in `repo`'s bundle/skills/ -- the runtime
    truth-set the reconciling route reads. A skill is a directory holding
    a SKILL.md; anything else under bundle/skills/ is ignored, matching
    what the bundle loader itself registers. Same clone-a-tempdir pattern
    as every other function in this module."""
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        skills_dir = workdir / "bundle" / "skills"
        if not skills_dir.is_dir():
            return []
        return sorted(
            entry.name
            for entry in skills_dir.iterdir()
            if entry.is_dir() and (entry / "SKILL.md").is_file()
        )
```

Rewrite the `list_skills` route body in `api/kobo_api/main.py` (~3644):

```python
    @app.get("/api/agents/{name}/skills")
    def list_skills(
        name: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(session, request, name)
        if isinstance(agent, JSONResponse):
            return agent

        # The bundle is the runtime truth; agent.skills is kobo's ledger,
        # and the direct-PR lane (a human merging skills straight into the
        # agent's repo) writes the bundle without telling the ledger.
        # Reconcile on read: the repo listing is the truth-set of ids, DB
        # entries keep their provenance (team/sha for catalog picks),
        # repo-only ids surface as local AND heal the ledger, DB-only rows
        # prune (removed by PR = not on the agent). A repo read failure
        # degrades to the ledger read and never prunes.
        entries = {entry["name"]: entry for entry in agent.skills}
        try:
            repo_ids = list_repo_skills(
                app.state.clients.github, agent.repo_full_name
            )
        except ClientError as exc:
            log.warning("skills repo listing failed for %s: %s", name, exc)
            repo_ids = None
        if repo_ids is not None:
            known = [e for e in agent.skills if e["name"] in set(repo_ids)]
            known_names = {e["name"] for e in known}
            discovered = [
                {"name": skill_id, "source": "local"}
                for skill_id in repo_ids
                if skill_id not in known_names
            ]
            merged = known + discovered
            if merged != agent.skills:
                agent.skills = merged
                session.commit()

        skills = []
        for entry in agent.skills:
            source = _skill_source(entry)
            skills.append(
                {
                    "name": entry["name"],
                    "team": entry.get("team"),
                    "source": source,
                    "sha": entry.get("sha"),
                    "editable": source == "local",
                }
            )
        return JSONResponse({"skills": skills})
```

Confirm `list_repo_skills` is added to main.py's `kobo_api.skills` import
line and `ClientError` is already imported (it is -- the teach route
catches it).

- [ ] **Step 5: Run the new tests, then the whole API suite**

Run: `cd ~/Documents/GitHub/kobo/api && python -m pytest tests/<skills test file> -x -q && python -m pytest tests/ -x -q`
Expected: all pass. Existing list_skills tests that seeded only the DB
column may now need their fake repo seeded with matching trees (or the
failing-clone fake) -- fix them to describe the new truth, not the drift.

- [ ] **Step 6: Commit**

```bash
git -C ~/Documents/GitHub/kobo add api/kobo_api/skills.py api/kobo_api/main.py api/tests/
git -C ~/Documents/GitHub/kobo commit -m "fix(skills): reconcile the ledger from the bundle on read -- direct-PR skills surface"
```

---

## Rollout (after all tasks, operator-driven -- NOT part of task execution)

1. daedalus branch -> PR -> coworker review -> merge -> build runtime image tagged `<sha>-t<techne_version>`.
2. kobo branch -> merge -> build kobo image.
3. Kyle rolls kobo (`kubectl -n kobo set image ...` -- classifier-blocked for the agent, Kyle runs it).
4. `runtime_image` re-pin in the deployment's kobo-config org.yaml AFTER the new runtime image exists (sequence law).
5. Agents pick up the journal + learnings handler on their next natural rebuild/roll.
6. Wire acceptance: converse with the wren fixture in Slack, wait for (or manually trigger) the nightly sweep, verify a pending card appears, approve it, verify techne provenance reads `intake:learning-...`.

## Self-review notes

- Spec coverage: journal (Task 1-2), handler (Task 3), table (Task 4), validation/lifecycle/envelope (Task 5), nightly + lifespan (Task 6), routes + gate + teach lane + detail fields (Task 7), UI plumbing (Task 8), panel + badge (Task 9). The spec's "Learnings tab lives in the Advanced group" belongs to Wave 2; Wave 1 appends a flat tab, Wave 2 regroups.
- Known adaptation points (flagged inline, not placeholders): daedalus test-helper names in Tasks 2/3 Step 1, `_CapturingGitHub` attribute in Task 7, `stubFetch` value convention in Task 9, `Config`/`Agent` seeding shapes in Task 6. Each has an explicit "read this reference file first" step.

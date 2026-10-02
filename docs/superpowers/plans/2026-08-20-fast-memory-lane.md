# Fast Memory Lane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teaching an agent writes its techne memory in seconds through the switchboard, with the repo commit demoted to a background archive; no rebuild, no pod rotation.

**Architecture:** kobo's teach route relays a metadata-marked system task through cabal; the daedalus runtime intercepts it pre-model, writes techne + the seeder's content-sha ledger, replies deterministically, and fires the existing revision pass in-session. Chat gains delivery-aware copy; the teach modal gains mode-keyed copy.

**Tech Stack:** Python (FastAPI, pydantic, sqlite3, techne), Svelte 5, pytest, vitest.

**Spec:** `docs/superpowers/specs/2026-08-20-fast-memory-lane-design.md`

## Global Constraints

- Cross-repo wire contract (exact strings, both repos): teach task metadata is `{"system_task": "teach", "filename": "<chat-*.md>"}`; the transport-stamped verified-caller key is `"_verified_sub"`; teach provenance is `intake:{filename}#{sha256hex[:12]}` over the note's UTF-8 bytes.
- Copy, verbatim, NO EM DASHES anywhere in user-facing strings:
  - daedalus confirmations: `"Learned. I will remember that."`, `"I already know this one. Nothing new to keep."`, refusal `"Teaching is operator-only. This connection is not an operator door."`, no-memory `"I have no personal memory configured, so I cannot keep this."`
  - kobo chat delivery 503: `"Your agent is receiving a delivery right now. Try again in a minute."`
  - UI instant success: `"Learned. Filed to the repo in the background."`; archive success (unchanged): `"Saved to memory. It arrives with the next delivery."`
- NO client names in any tracked file of either repo. Run the vendor scan (`.superpowers/sdd/vendor-names.txt`, untracked) against the diff before EVERY commit, as its own step.
- Daedalus work happens on branch `feat/teach-lane` stacked on `feat/colleague-quorum` (create from it if absent). Kobo work happens on `main`.
- Daedalus tests that need techne follow the existing guard pattern in `tests/test_memory_revision.py` (`pytest.importorskip` / skipif) so a techne-less CI stays green.
- Teach tasks never reach the model, enforcement, approvals, or tools. The teach reply must already be sent before any revision work begins.

---

### Task 1: daedalus teach module (`memory/teach.py`) with seeder parity

**Files:**
- Modify: `src/daedalus/task.py` (add one constant near `REASON_KEY`)
- Create: `src/daedalus/memory/teach.py`
- Test: `tests/test_memory_teach.py`

**Interfaces:**
- Consumes: `daedalus.task.Task`; techne `knowledge.store/list_entries/delete`; the seeder's `intake_state` table in `<techne dir>/seed_state.db`.
- Produces (Task 3 relies on these exact names): `is_teach_task(task) -> bool`, `teach_authorized(task) -> bool`, `apply_teach(note: str, filename: str, db_path: Path) -> TeachResult` with fields `stored: bool`, `already_known: bool`, `error: str | None`, `entry_ids: list[int]`, `source: str`; constants `SYSTEM_TASK_KEY`, `TEACH_MARKER`, `FILENAME_KEY`; and `daedalus.task.VERIFIED_SUB_KEY` (Task 2 also imports it).

- [ ] **Step 1: Add the constant to task.py**

In `src/daedalus/task.py`, directly under `CABAL_REASON_KEY`:

```python
# Transport-owned: the a2a transport stamps the verified JWT subject here
# after token verification and unconditionally discards any wire-supplied
# value, so handlers can trust it (daedalus.memory.teach gates on it).
VERIFIED_SUB_KEY = "_verified_sub"
```

- [ ] **Step 2: Write the failing tests**

`tests/test_memory_teach.py`. Techne-backed tests use the same guard as `tests/test_memory_revision.py`. `seeder` is loaded by path since `docker/` is not a package:

```python
import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from daedalus.memory.teach import (
    FILENAME_KEY,
    SYSTEM_TASK_KEY,
    TEACH_MARKER,
    TeachResult,
    apply_teach,
    is_teach_task,
    teach_authorized,
)
from daedalus.task import VERIFIED_SUB_KEY, Task

techne = pytest.importorskip("techne", reason="techne is not installed")
from techne.core import knowledge  # noqa: E402
from techne.core.database import init_db  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_seeder():
    spec = importlib.util.spec_from_file_location(
        "seeder", REPO_ROOT / "docker" / "seeder.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def techne_db(tmp_path):
    db = tmp_path / "techne.db"
    init_db(db).close()
    return db


def _rows(db, source):
    return [
        e
        for e in knowledge.list_entries(db, limit=10_000_000, include_superseded=True)
        if e.get("source") == source
    ]


def _teach_task(text="The dog is named Tsuru.", *, sub="service:room", filename="chat-20260820-dog.md"):
    task = Task.from_text(text)
    task.metadata[SYSTEM_TASK_KEY] = TEACH_MARKER
    task.metadata[FILENAME_KEY] = filename
    if sub is not None:
        task.metadata[VERIFIED_SUB_KEY] = sub
    return task


def test_is_teach_task_only_on_exact_marker():
    assert is_teach_task(_teach_task())
    assert not is_teach_task(Task.from_text("hi"))
    other = Task.from_text("hi")
    other.metadata[SYSTEM_TASK_KEY] = "reboot"
    assert not is_teach_task(other)


def test_teach_authorized_rejects_colleagues_and_absent_sub():
    assert teach_authorized(_teach_task(sub="service:room"))
    assert teach_authorized(_teach_task(sub="kobo-room"))
    assert not teach_authorized(_teach_task(sub="agent:ward"))
    assert not teach_authorized(_teach_task(sub=None))
    empty = _teach_task(sub=None)
    empty.metadata[VERIFIED_SUB_KEY] = ""
    assert not teach_authorized(empty)


def test_apply_teach_stores_fact_with_seeder_provenance(techne_db):
    note = "Taught in chat on 2026-08-20.\n\nThe dog is named Tsuru.\n"
    result = apply_teach(note, "chat-20260820-dog.md", techne_db)
    assert result.stored and not result.already_known and result.error is None
    digest = hashlib.sha256(note.encode("utf-8")).hexdigest()
    assert result.source == f"intake:chat-20260820-dog.md#{digest[:12]}"
    rows = _rows(techne_db, result.source)
    assert len(rows) == 1
    assert rows[0]["content"] == note.strip()
    assert rows[0]["type"] == "fact"
    assert rows[0]["confidence"] == "high"


def test_apply_teach_writes_intake_ledger_row(techne_db):
    note = "A fact worth keeping.\n"
    result = apply_teach(note, "chat-20260820-fact.md", techne_db)
    state = techne_db.parent / "seed_state.db"
    conn = sqlite3.connect(state)
    row = conn.execute(
        "SELECT sha256, filename, entry_ids FROM intake_state"
    ).fetchone()
    conn.close()
    assert row[0] == hashlib.sha256(note.encode("utf-8")).hexdigest()
    assert row[1] == "chat-20260820-fact.md"
    assert json.loads(row[2]) == result.entry_ids


def test_apply_teach_same_note_twice_is_already_known(techne_db):
    note = "A fact worth keeping.\n"
    first = apply_teach(note, "chat-20260820-fact.md", techne_db)
    second = apply_teach(note, "chat-20260820-fact.md", techne_db)
    assert first.stored and second.already_known and not second.stored
    assert len(_rows(techne_db, first.source)) == len(first.entry_ids)


def test_apply_teach_rejects_bad_filename_and_empty_note(techne_db):
    assert apply_teach("x", "../evil.md", techne_db).error
    assert apply_teach("x", "UPPER.md", techne_db).error
    assert apply_teach("x", "", techne_db).error
    assert apply_teach("   \n", "chat-20260820-ok.md", techne_db).error


def test_seeder_skips_what_teach_ledgered(techne_db, tmp_path):
    """The arc's no-double-ingest guarantee: after a teach, the seeder's
    own ingest over the same bytes is a skip, not a re-ingest."""
    seeder = _load_seeder()
    note = "Taught in chat on 2026-08-20.\n\nThe dog is named Tsuru.\n"
    apply_teach(note, "chat-20260820-dog.md", techne_db)
    intake = tmp_path / "intake"
    intake.mkdir()
    (intake / "chat-20260820-dog.md").write_text(note, encoding="utf-8")

    def store_fn(db, content, type, tags, source):
        return knowledge.store(
            db, content, type=type, tags=tags, confidence="high", source=source
        )

    result = seeder.ingest_intake(
        intake, techne_db, techne_db.parent / "seed_state.db", store_fn, knowledge.delete
    )
    assert result == {"ingested": 0, "skipped": 1}


@pytest.mark.parametrize(
    "note",
    [
        "Taught in chat on 2026-08-20.\n\nFlat prose, no headings.\n",
        "Preamble line.\n\n## First\nalpha\n\n## Second\nbeta\n",
    ],
)
def test_teach_and_seeder_produce_identical_rows(techne_db, tmp_path, note):
    """Parity by test, not by import: the teach handler's sectioning, typing,
    source, and ledger row must match what the seeder would have produced
    for the same file. Drift fails here."""
    seeder = _load_seeder()
    filename = "chat-20260820-par.md"
    taught = apply_teach(note, filename, techne_db)
    taught_rows = sorted(
        (r["content"], r["type"], r["source"]) for r in _rows(techne_db, taught.source)
    )

    other_db = tmp_path / "other" / "techne.db"
    other_db.parent.mkdir()
    init_db(other_db).close()
    intake = tmp_path / "intake"
    intake.mkdir()
    (intake / filename).write_text(note, encoding="utf-8")

    def store_fn(db, content, type, tags, source):
        return knowledge.store(
            db, content, type=type, tags=tags, confidence="high", source=source
        )

    seeder.ingest_intake(
        intake, other_db, other_db.parent / "seed_state.db", store_fn, knowledge.delete
    )
    digest = hashlib.sha256(note.encode("utf-8")).hexdigest()
    source = f"intake:{filename}#{digest[:12]}"
    seeded_rows = sorted((r["content"], r["type"], r["source"]) for r in _rows(other_db, source))
    assert taught_rows == seeded_rows and taught_rows

    taught_ledger = sqlite3.connect(techne_db.parent / "seed_state.db").execute(
        "SELECT sha256, filename FROM intake_state"
    ).fetchall()
    seeded_ledger = sqlite3.connect(other_db.parent / "seed_state.db").execute(
        "SELECT sha256, filename FROM intake_state"
    ).fetchall()
    assert taught_ledger == seeded_ledger
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd ../daedalus && python -m pytest tests/test_memory_teach.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'daedalus.memory.teach'`

- [ ] **Step 4: Write the module**

`src/daedalus/memory/teach.py`:

```python
"""The teach system task: an operator's note becomes a techne memory NOW.

kobo relays a metadata-marked a2a task through the switchboard; the agent
intercepts it pre-model (daedalus.agent.Agent.handle_task) and calls
apply_teach here. The write mirrors the boot seeder (docker/seeder.py)
byte-for-byte in provenance and outcome: same sectioning, same type
mapping, same store call, same intake_state ledger row -- so the next
natural rebuild's seeder sees the sha already ledgered and skips the
archived file. Parity is enforced by tests/test_memory_teach.py, which
runs the real seeder against the same bytes; change one side and the
suite says so.

Authorization gates on the transport-stamped verified JWT subject
(daedalus.task.VERIFIED_SUB_KEY): colleagues dispatch as "agent:<name>"
and must never mint operator-grade intake:chat-* facts (the operator
shield's provenance would be forged). No verified sub means refused.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from daedalus.task import VERIFIED_SUB_KEY, Task

SYSTEM_TASK_KEY = "system_task"
TEACH_MARKER = "teach"
FILENAME_KEY = "filename"

# The intake filename alphabet, mirroring the room's own intake rule:
# lowercase [a-z0-9-], at most 40 chars before ".md", no leading dash.
_FILENAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}\.md$")

# Sectioning and typing mirror docker/seeder.py exactly (parity-tested).
_H2 = re.compile(r"^## ", re.MULTILINE)
_TYPE_MAP = {
    "gotchas": "evaluation",
    "playbooks": "procedure",
    "operational-playbooks": "procedure",
    "decisions": "decision",
}

_INTAKE_STATE_SCHEMA = """CREATE TABLE IF NOT EXISTS intake_state (
       sha256 TEXT PRIMARY KEY,
       filename TEXT NOT NULL,
       entry_ids TEXT NOT NULL
   )"""


@dataclass
class TeachResult:
    stored: bool
    already_known: bool = False
    error: str | None = None
    entry_ids: list[int] = field(default_factory=list)
    source: str = ""


def is_teach_task(task: Task) -> bool:
    return task.metadata.get(SYSTEM_TASK_KEY) == TEACH_MARKER


def teach_authorized(task: Task) -> bool:
    sub = task.metadata.get(VERIFIED_SUB_KEY)
    return isinstance(sub, str) and bool(sub) and not sub.startswith("agent:")


def _sections(text: str) -> list[str]:
    starts = [m.start() for m in _H2.finditer(text)]
    if not starts:
        whole = text.strip()
        return [whole] if whole else []
    sections = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        section = text[start:end].strip()
        if section.removeprefix("##").strip():
            sections.append(section)
    return sections


def apply_teach(note: str, filename: str, db_path: Path) -> TeachResult:
    """Store `note` into the techne db at `db_path` with seeder-identical
    provenance, and ledger its sha in the seeder's own intake_state table
    (beside the techne db, `<dir>/seed_state.db`). Synchronous sqlite;
    callers on the event loop run it via asyncio.to_thread."""
    if not _FILENAME_RE.match(filename or ""):
        return TeachResult(stored=False, error=f"invalid teach filename {filename!r}")
    sections = _sections(note)
    if not sections:
        return TeachResult(stored=False, error="the note is empty")

    from techne.core import knowledge

    digest = hashlib.sha256(note.encode("utf-8")).hexdigest()
    source = f"intake:{filename}#{digest[:12]}"
    kind = _TYPE_MAP.get(Path(filename).stem, "fact")
    state_db = Path(db_path).parent / "seed_state.db"
    conn = sqlite3.connect(state_db)
    try:
        conn.execute(_INTAKE_STATE_SCHEMA)
        conn.commit()
        row = conn.execute(
            "SELECT 1 FROM intake_state WHERE sha256 = ?", (digest,)
        ).fetchone()
        if row is not None:
            return TeachResult(stored=False, already_known=True, source=source)

        # Crash recovery, same as the seeder: rows with this exact source
        # from an interrupted earlier attempt are replaced, never doubled.
        for entry in knowledge.list_entries(
            db_path, limit=10_000_000, include_superseded=True
        ):
            if entry.get("source") == source:
                knowledge.delete(db_path, entry["id"])

        ids: list[int] = []
        try:
            for section in sections:
                ids.append(
                    knowledge.store(
                        db_path,
                        section,
                        type=kind,
                        tags="intake",
                        confidence="high",
                        source=source,
                    )
                )
        except Exception:
            for entry_id in ids:
                try:
                    knowledge.delete(db_path, entry_id)
                except Exception:
                    pass
            raise
        conn.execute(
            "INSERT OR REPLACE INTO intake_state "
            "(sha256, filename, entry_ids) VALUES (?, ?, ?)",
            (digest, filename, json.dumps(ids)),
        )
        conn.commit()
        return TeachResult(stored=True, entry_ids=ids, source=source)
    finally:
        conn.close()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ../daedalus && python -m pytest tests/test_memory_teach.py -v`
Expected: PASS (all; skips only if techne is absent locally, in which case install it per the repo README before proceeding)

- [ ] **Step 6: Run the full suite, vendor-scan the diff, commit**

Run: `python -m pytest -q` then scan `git diff` output against every name in the kobo repo's `.superpowers/sdd/vendor-names.txt` (case-insensitive). Then:

```bash
git add src/daedalus/task.py src/daedalus/memory/teach.py tests/test_memory_teach.py
git commit -m "feat: teach system task writes techne + the seeder ledger in-process"
```

---

### Task 2: a2a transport stamps the verified subject

**Files:**
- Modify: `src/daedalus/transports/a2a.py` (the `build_a2a_app` closure: `unauthorized` helper at ~line 141, `message_send`/`tasks_get` at ~172-199, `peer_dispatch` at ~203-229, `rpc` at ~231-250)
- Test: `tests/test_transport_a2a_auth.py` (extend), `tests/test_transport_a2a.py` (extend)

**Interfaces:**
- Consumes: `daedalus.task.VERIFIED_SUB_KEY` (Task 1); `verifier.verify_header(header) -> dict` (existing, returns claims).
- Produces: every Task reaching `handler.handle_task` through either POST surface has transport-owned `metadata["_verified_sub"]`: set to the verified claims' `sub` when a verifier accepted the call, absent otherwise, wire-supplied values always discarded.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_transport_a2a_auth.py` (follow its existing fixtures for a fake verifier; the fake's `verify_header` must return a claims dict like `{"sub": "service:room"}`) and `tests/test_transport_a2a.py`:

```python
def test_peer_dispatch_stamps_verified_sub(...):
    # POST /a2a/tasks with a passing fake verifier whose claims carry
    # sub="service:room"; a capturing handler asserts
    # task.metadata["_verified_sub"] == "service:room".

def test_peer_dispatch_discards_wire_supplied_sub_when_unverified(...):
    # No verifier configured; the posted task body includes
    # metadata={"_verified_sub": "service:forged"}; the capturing handler
    # asserts "_verified_sub" not in task.metadata.

def test_peer_dispatch_overwrites_wire_supplied_sub_when_verified(...):
    # Passing verifier (sub="agent:ward") plus a forged wire value;
    # handler sees "agent:ward".

def test_rpc_message_send_stamps_verified_sub(...):
    # POST / message/send with the passing verifier; handler asserts the
    # stamp, both for a fresh task and for a taskId continuation.

def test_verifier_claims_without_sub_leave_key_absent(...):
    # Passing verifier returning {} claims; key absent.
```

Write them as real tests using the file's existing app/client fixtures and a handler that records the Task it received.

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_transport_a2a_auth.py tests/test_transport_a2a.py -v`
Expected: new tests FAIL (metadata key never stamped)

- [ ] **Step 3: Implement the stamp**

In `src/daedalus/transports/a2a.py`, import `VERIFIED_SUB_KEY` from `daedalus.task` and add a module-level helper above `build_a2a_app`:

```python
def stamp_verified_sub(task: Task, claims: dict | None) -> None:
    """The transport owns metadata[VERIFIED_SUB_KEY]: wire-supplied values
    are discarded unconditionally, and the key exists only when a verifier
    accepted the caller's token and its claims carried a sub. Handlers
    (the teach lane) trust it on that basis."""
    task.metadata.pop(VERIFIED_SUB_KEY, None)
    sub = (claims or {}).get("sub")
    if isinstance(sub, str) and sub:
        task.metadata[VERIFIED_SUB_KEY] = sub
```

Refactor the closure's `unauthorized` into `authorize`, returning claims:

```python
    async def authorize(request: Request) -> tuple[JSONResponse | None, dict | None]:
        """(401, None) when auth is configured and the token fails;
        (None, claims) when it verifies; (None, None) with no verifier."""
        if verifier is None:
            return None, None
        from daedalus.transports.a2a_auth import AuthError

        try:
            claims = await verifier.verify_header(request.headers.get("authorization"))
        except AuthError as exc:
            return (
                JSONResponse(
                    {"error": f"unauthorized: {exc}"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                ),
                None,
            )
        return None, claims
```

`peer_dispatch`: `denied, claims = await authorize(request)`; after `task = task_from_a2a(body)` succeeds, call `stamp_verified_sub(task, claims)` before storing/handling. `rpc`: `denied, claims = await authorize(request)` and pass claims to the method: change `methods` handlers to take `(params, claims)`; in `message_send`, call `stamp_verified_sub(task, claims)` in BOTH branches (fresh task and taskId continuation) before `handler.handle_task(task)`; `tasks_get(params, claims)` ignores claims. Update the dispatch line to `await method(body.get("params") or {}, claims)`.

- [ ] **Step 4: Run to verify green, full suite, vendor scan, commit**

Run: `python -m pytest tests/test_transport_a2a_auth.py tests/test_transport_a2a.py -v` then `python -m pytest -q`. Vendor-scan the diff. Commit:

```bash
git add src/daedalus/transports/a2a.py tests/test_transport_a2a_auth.py tests/test_transport_a2a.py
git commit -m "feat: a2a transport stamps the verified JWT sub onto task metadata"
```

---

### Task 3: Agent intercepts teach tasks and fires in-session revision

**Files:**
- Modify: `src/daedalus/agent.py` (`handle_task` at ~247, `_start_revision_pass` at ~568, `__init__`)
- Test: `tests/test_agent_teach.py` (new)

**Interfaces:**
- Consumes: Task 1's `is_teach_task`, `teach_authorized`, `apply_teach`, `FILENAME_KEY`; existing `self.memory.db_path` (techne stores only), `self._start_revision_pass()`, `self._emit`, `task.reply`, `TaskState`, `ReasonCode`.
- Produces: teach tasks short-circuit `handle_task` entirely (before the enforcement `locked` check, before `run_pre_task`, before recall and `_loop`).

- [ ] **Step 1: Write the failing tests**

`tests/test_agent_teach.py`, using the existing agent fixture style from `tests/test_agent.py` (bundle with `memory.personal: techne` pointing at a tmp db; a fake llm that fails the test if called). Guard techne like Task 1. Cover:

```python
def test_teach_task_stores_and_replies_learned(...):
    # Authorized teach task -> reply text "Learned. I will remember that.",
    # state COMPLETED, techne row present, llm NEVER called.

def test_teach_never_reaches_model_or_enforcement(...):
    # Same as above but with an enforcement hook that would block any task,
    # and enforcement.locked True: teach still succeeds (interception sits
    # above the locked check by design; memory writes are not law-gated).

def test_teach_refused_for_colleague_sub(...):
    # sub "agent:ward" -> reply is the refusal copy, state FAILED,
    # reason POLICY_VIOLATION, NO techne row.

def test_teach_refused_without_verified_sub(...):
    # No _verified_sub -> refusal, no row.

def test_teach_without_memory_replies_no_memory(...):
    # Agent whose memory has no db_path (NullMemory) -> the no-memory copy,
    # state FAILED, no crash.

def test_teach_already_known_copy(...):
    # Teach the same note twice -> second reply
    # "I already know this one. Nothing new to keep."

def test_teach_fires_revision_pass_and_serializes(...):
    # Bundle with memory.revision: operator. Monkeypatch
    # daedalus.memory.revision.run_revision_pass with a recorder coroutine.
    # After a successful teach, the recorder runs exactly once (drain
    # pending tasks). Fire two teaches back-to-back: the recorder's
    # invocations never overlap (record enter/exit timestamps around an
    # asyncio.sleep(0.01) inside the fake; assert no interleave).

def test_teach_invalid_filename_replies_error(...):
    # metadata filename "../evil.md" -> FAILED reply containing
    # "invalid teach filename", no row.
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_agent_teach.py -v`
Expected: FAIL (no interception; the fake llm gets called or replies mismatch)

- [ ] **Step 3: Implement**

In `Agent.__init__`, alongside existing attribute setup:

```python
        self._revision_lock = asyncio.Lock()
        self._revision_fires: set[asyncio.Task] = set()
```

In `_start_revision_pass`, wrap the pass body in the lock (a boot pass and a teach-fired pass must never interleave; the pass is already re-runnable, so queuing is correct):

```python
        async def _pass() -> None:
            async with self._revision_lock:
                await run_revision_pass(
                    db_path, self.llm, policy=policy, emit=self._emit, colleagues=colleagues
                )
```

At the very top of `handle_task` (above the `enforcement.locked` check):

```python
        if is_teach_task(task):
            return await self._handle_teach(task)
```

New method (imports at top of file: `from daedalus.memory.teach import FILENAME_KEY, apply_teach, is_teach_task, teach_authorized` and `from daedalus.task import VERIFIED_SUB_KEY`):

```python
    async def _handle_teach(self, task: Task) -> Task:
        """The operator teach lane: write the note into techne now, no
        model, no enforcement, no rebuild. Sits above the enforcement
        locked check on purpose: memory writes are operator-gated by the
        transport-stamped sub, not law-gated -- a broken orchestrator file
        must not also brick teaching."""
        if not teach_authorized(task):
            self._emit("teach_refused", sub=task.metadata.get(VERIFIED_SUB_KEY))
            return task.reply(
                "Teaching is operator-only. This connection is not an operator door.",
                state=TaskState.FAILED,
                reason=ReasonCode.POLICY_VIOLATION,
            )
        db_path = getattr(self.memory, "db_path", None)
        if db_path is None:
            return task.reply(
                "I have no personal memory configured, so I cannot keep this.",
                state=TaskState.FAILED,
                reason=ReasonCode.TOOL_BLOCKED,
            )
        filename = task.metadata.get(FILENAME_KEY, "")
        result = await asyncio.to_thread(
            apply_teach, task.user_text(), str(filename), Path(db_path)
        )
        if result.error:
            return task.reply(
                f"I could not keep that: {result.error}.", state=TaskState.FAILED
            )
        if result.already_known:
            return task.reply("I already know this one. Nothing new to keep.")
        self._emit("teach_stored", source=result.source, entries=len(result.entry_ids))
        fire = self._start_revision_pass()
        if fire is not None:
            self._revision_fires.add(fire)
            fire.add_done_callback(self._revision_fires.discard)
        return task.reply("Learned. I will remember that.")
```

- [ ] **Step 4: Run to verify green, full suite, vendor scan, commit**

Run: `python -m pytest tests/test_agent_teach.py -v && python -m pytest -q`. Vendor-scan the diff. Commit:

```bash
git add src/daedalus/agent.py tests/test_agent_teach.py
git commit -m "feat: pre-model teach interception with in-session revision fire"
```

---

### Task 4: kobo teach envelope builder

**Files:**
- Modify: `api/kobo_api/clients.py` (directly under `_build_task_envelope`, ~line 1004)
- Test: `api/tests/test_clients.py` (beside the `_build_task_envelope` shape tests at ~1468)

**Interfaces:**
- Consumes: `_build_task_envelope(messages)` (existing).
- Produces (Task 5 relies on it): `_build_teach_envelope(note: str, filename: str) -> dict`.

- [ ] **Step 1: Write the failing test**

In `api/tests/test_clients.py`:

```python
def test_build_teach_envelope_is_a_marked_single_message_task():
    envelope = _build_teach_envelope("Taught in chat.\n\nA fact.\n", "chat-20260820-a.md")
    assert envelope["kind"] == "task"
    assert envelope["status"]["state"] == "submitted"
    assert envelope["status"]["message"]["parts"][0]["text"] == "Taught in chat.\n\nA fact.\n"
    assert envelope["history"] == []
    assert envelope["metadata"] == {
        "system_task": "teach",
        "filename": "chat-20260820-a.md",
    }
```

(Import `_build_teach_envelope` alongside the existing `_build_task_envelope` import.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd api && python -m pytest tests/test_clients.py -k teach_envelope -v`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement**

In `api/kobo_api/clients.py`, directly below `_build_task_envelope`:

```python
def _build_teach_envelope(note: str, filename: str) -> dict:
    """The teach system task (fast-memory-lane spec): a normal
    single-message task envelope whose metadata carries the marker the
    daedalus runtime intercepts pre-model. `note` is byte-for-byte the
    content the background archive commits to bundle/intake/<filename>,
    so both lanes agree on the content sha."""
    envelope = _build_task_envelope([{"role": "user", "text": note}])
    envelope["metadata"] = {"system_task": "teach", "filename": filename}
    return envelope
```

- [ ] **Step 4: Run, full API suite, vendor scan, commit**

Run: `python -m pytest tests/test_clients.py -k teach_envelope -v && python -m pytest -q`. Vendor-scan. Commit:

```bash
git add api/kobo_api/clients.py api/tests/test_clients.py
git commit -m "feat(api): teach task envelope with the system-task marker"
```

---

### Task 5: kobo teach route goes relay-first with a background archive

**Files:**
- Modify: `api/kobo_api/main.py` (teach route at ~2027-2089; imports)
- Test: `api/tests/test_web_teach.py` (extend)

**Interfaces:**
- Consumes: `_build_teach_envelope` (Task 4), `CabalClient.relay`, `_extract_relay_reply`, `cabal_enabled`, `upload_intake_file`, FastAPI `BackgroundTasks`.
- Produces: `POST /api/agents/{name}/teach` returns `{"filename", "mode": "instant"}` (relay lane) or `{"filename", "mode": "archive", "sha"}` (fallback and cabal-off lane, today's behavior plus the mode field).

- [ ] **Step 1: Write the failing tests**

Extend `api/tests/test_web_teach.py`, following its existing fixture style (it already builds an app with fake clients) and the cabal-faking style of `api/tests/test_api_colleagues.py`:

```python
def test_teach_instant_lane_relays_and_archives_in_background(...):
    # cabal wired + relay returns a completed task -> 200
    # {"filename": ..., "mode": "instant"} with no "sha" key; the fake
    # github records that upload_intake_file's commit happened (background
    # tasks run when TestClient's response completes); the relayed
    # envelope carried metadata {"system_task": "teach", "filename": ...}
    # and the note text equals the committed content exactly.

def test_teach_falls_back_to_archive_when_relay_fails(...):
    # cabal wired, relay raises CabalError -> 200
    # {"filename", "mode": "archive", "sha"}; commit happened
    # synchronously; nothing 5xx.

def test_teach_falls_back_when_agent_reply_is_failed_task(...):
    # relay returns a task whose status.state == "failed" (e.g. the
    # runtime refused) -> archive lane, same as a relay error.

def test_teach_without_cabal_is_archive_mode(...):
    # cabal off -> {"filename", "mode": "archive", "sha"} (today's
    # behavior, plus mode).

def test_teach_background_archive_failure_is_logged_not_surfaced(...):
    # instant lane; the fake github's upload raises -> response was
    # already 200 instant; caplog has "teach archive commit failed".
```

Also update any existing tests asserting the old response shape to expect the added `"mode": "archive"` key.

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_web_teach.py -v`
Expected: new tests FAIL (no mode key, no relay attempt)

- [ ] **Step 3: Implement**

In `api/kobo_api/main.py`: add `BackgroundTasks` to the fastapi import; ensure `_build_teach_envelope` is imported beside `_build_task_envelope`. Add a helper near the teach route:

```python
    def _teach_task_failed(raw: dict) -> bool:
        """True when the relayed reply is a FAILED task (the runtime refused
        or errored): kobo must not claim the instant lane for a note the
        agent did not actually keep."""
        task = raw.get("task") or raw.get("result") or raw
        status = task.get("status") or {}
        return status.get("state") == "failed"

    def _archive_teach_note(repo: str, filename: str, note: str) -> None:
        """The background archive lane. Failure is logged, never surfaced:
        the memory is already stored in the agent; only the repo copy is
        missing, and the intake panel simply won't list the file until a
        later commit lands it."""
        try:
            upload_intake_file(
                app.state.clients.github,
                repo,
                filename,
                note,
                runtime_image=app.state.config.runtime_image,
            )
        except Exception as exc:
            log.warning(
                "teach archive commit failed for %s (%s): %s", repo, filename, exc
            )
```

Rework the teach route body after `note`/`filename` are built (validation and note construction stay exactly as they are). Add `background_tasks: BackgroundTasks` to the route signature. Replace the current unconditional upload block with:

```python
        cfg: Config = app.state.config
        clients = app.state.clients
        if cabal_enabled(cfg) and clients.cabal is not None:
            envelope = _build_teach_envelope(note, filename)
            try:
                raw = clients.cabal.relay(agent.name, envelope)
                if _teach_task_failed(raw):
                    raise CabalError("agent replied with a failed task")
                _extract_relay_reply(raw)
            except (CabalError, httpx.HTTPError, ValueError) as exc:
                # Same catch set as the chat relay above, plus the failed-
                # task check: ANY instant-lane miss degrades to the archive
                # lane below, so teaching never 502s during a delivery
                # window. The cause is logged, never echoed.
                log.warning(
                    "teach relay failed for agent %s, archive lane instead: %s",
                    name,
                    exc,
                )
            else:
                background_tasks.add_task(
                    _archive_teach_note, agent.repo_full_name, filename, note
                )
                return JSONResponse({"filename": filename, "mode": "instant"})

        try:
            sha = upload_intake_file(
                app.state.clients.github,
                agent.repo_full_name,
                filename,
                note,
                runtime_image=cfg.runtime_image,
            )
        except IntakeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        except ClientError as exc:
            log.warning("teach route failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't reach your agent's files. Try again."}, status_code=502
            )

        return JSONResponse({"filename": filename, "mode": "archive", "sha": sha})
```

- [ ] **Step 4: Run, full API suite, vendor scan, commit**

Run: `python -m pytest tests/test_web_teach.py -v && python -m pytest -q`. Vendor-scan. Commit:

```bash
git add api/kobo_api/main.py api/tests/test_web_teach.py
git commit -m "feat(api): teach relays through the switchboard, archives in background"
```

---

### Task 6: delivery-aware chat copy

**Files:**
- Modify: `api/kobo_api/clients.py` (KubectlClient Protocol ~1153 and the concrete client ~1229), `api/kobo_api/knowledge.py` (new helper below `delivery_state`), `api/kobo_api/main.py` (chat relay except block at ~1432-1445)
- Test: `api/tests/test_knowledge.py` (or the file holding `delivery_state` tests; extend), plus the chat-route relay-failure tests wherever `"The switchboard did not answer"` is asserted (`api/tests/test_api_colleagues.py` / `test_api_agents.py`)

**Interfaces:**
- Consumes: the existing `CommandRunner`-backed kubectl client.
- Produces: `KubectlClient.deployment_status_json(namespace, deploy) -> dict`; `rollout_in_progress(kubectl, *, namespace: str, deploy: str) -> bool` in knowledge.py; chat relay failures return 503 with the delivery copy when a rollout is in flight, the existing 502 copy otherwise.

- [ ] **Step 1: Write the failing tests**

`rollout_in_progress` truth table (fake kubectl returning canned dicts):

```python
def test_rollout_in_progress_truth_table():
    def dep(generation=2, observed=2, replicas=1, updated=1, available=1):
        return {
            "metadata": {"generation": generation},
            "spec": {"replicas": replicas},
            "status": {
                "observedGeneration": observed,
                "updatedReplicas": updated,
                "availableReplicas": available,
            },
        }

    class Kubectl:
        def __init__(self, payload):
            self.payload = payload
        def deployment_status_json(self, namespace, deploy):
            if isinstance(self.payload, Exception):
                raise self.payload
            return self.payload

    assert rollout_in_progress(Kubectl(dep()), namespace="w", deploy="w") is False
    assert rollout_in_progress(Kubectl(dep(observed=1)), namespace="w", deploy="w") is True
    assert rollout_in_progress(Kubectl(dep(updated=0)), namespace="w", deploy="w") is True
    assert rollout_in_progress(Kubectl(dep(available=0)), namespace="w", deploy="w") is True
    assert rollout_in_progress(Kubectl(dep(replicas=0, updated=0, available=0)), namespace="w", deploy="w") is False
    # A probe error must NEVER mask the switchboard copy:
    assert rollout_in_progress(Kubectl(ClientError("kubectl down")), namespace="w", deploy="w") is False
```

Chat route branch tests (extend the existing relay-failure tests' fake clients with `deployment_status_json`):

```python
def test_chat_relay_failure_during_rollout_says_delivery(...):
    # relay raises CabalError, fake kubectl reports observed < generation
    # -> 503 {"error": "Your agent is receiving a delivery right now. Try again in a minute."}

def test_chat_relay_failure_when_settled_keeps_switchboard_copy(...):
    # relay raises, kubectl reports settled -> the existing 502 copy.

def test_chat_relay_failure_with_probe_error_keeps_switchboard_copy(...):
    # relay raises, kubectl raises -> the existing 502 copy.
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/ -k "rollout_in_progress or delivery_says or switchboard_copy" -v`
Expected: FAIL (helper and branch missing)

- [ ] **Step 3: Implement**

Protocol addition in `clients.py` (inside `class KubectlClient(Protocol)`):

```python
    def deployment_status_json(self, namespace: str, deploy: str) -> dict: ...
```

Concrete implementation beside `deploy_image`:

```python
    def deployment_status_json(self, namespace: str, deploy: str) -> dict:
        out = self._runner.run(
            [self._kubectl, "-n", namespace, "get", "deploy", deploy, "-o", "json"]
        )
        return json.loads(out)
```

In `knowledge.py`, below `delivery_state`:

```python
def rollout_in_progress(kubectl: KubectlClient, *, namespace: str, deploy: str) -> bool:
    """True when the agent's Deployment is genuinely mid-rollout: the
    controller hasn't observed the latest spec yet, or the updated or
    available replica count is short of spec. Conservative on purpose --
    any probe failure reads as False, so the chat route's delivery-aware
    copy can never mask a real switchboard failure behind a friendlier
    message."""
    try:
        dep = kubectl.deployment_status_json(namespace, deploy)
    except Exception:
        return False
    status = dep.get("status") or {}
    spec = dep.get("spec") or {}
    generation = (dep.get("metadata") or {}).get("generation", 0)
    if status.get("observedGeneration", 0) < generation:
        return True
    replicas = spec.get("replicas", 1)
    return (
        status.get("updatedReplicas", 0) < replicas
        or status.get("availableReplicas", 0) < replicas
    )
```

In the chat route's relay except block (main.py ~1440), before the existing 502 return:

```python
                if rollout_in_progress(
                    clients.kubectl, namespace=agent.name, deploy=agent.name
                ):
                    return JSONResponse(
                        {
                            "error": (
                                "Your agent is receiving a delivery right now. "
                                "Try again in a minute."
                            )
                        },
                        status_code=503,
                    )
```

(Import `rollout_in_progress` from `kobo_api.knowledge` alongside the existing imports.)

- [ ] **Step 4: Run, full API suite, vendor scan, commit**

Run: `python -m pytest -q`. Vendor-scan. Commit:

```bash
git add api/kobo_api/clients.py api/kobo_api/knowledge.py api/kobo_api/main.py api/tests/
git commit -m "feat(api): chat says so when a delivery is the reason the agent is away"
```

---

### Task 7: teach modal mode-keyed copy

**Files:**
- Modify: `ui/src/lib/api.ts` (`teachAgent` at ~474), `ui/src/components/TeachModal.svelte` (~55-92)
- Test: `ui/src/components/ChatPanel.test.ts` (the Teach-modal assertions live there; extend)

**Interfaces:**
- Consumes: the teach response `{filename, mode, sha?}` (Task 5).
- Produces: instant saves show "Learned. Filed to the repo in the background."; archive saves keep "Saved to memory. It arrives with the next delivery."

- [ ] **Step 1: Write the failing tests**

In `ui/src/components/ChatPanel.test.ts`, beside the existing teach-save success test (mock the teach fetch response body):

```typescript
it("shows the instant copy when the teach response mode is instant", async () => {
  // mock POST /teach -> {"filename": "chat-x.md", "mode": "instant"}
  // save; expect "Learned. Filed to the repo in the background."
});

it("keeps the archive copy when the teach response mode is archive", async () => {
  // mock -> {"filename": "chat-x.md", "mode": "archive", "sha": "abc"}
  // expect "Saved to memory. It arrives with the next delivery."
});

it("treats a missing mode as archive", async () => {
  // mock -> {"filename": "chat-x.md", "sha": "abc"} (old server)
  // expect the archive copy.
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ui && npx vitest run src/components/ChatPanel.test.ts`
Expected: new tests FAIL

- [ ] **Step 3: Implement**

`ui/src/lib/api.ts`:

```typescript
export async function teachAgent(
  name: string,
  text: string,
): Promise<{ filename: string; mode?: "instant" | "archive"; sha?: string }> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/teach`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  return parse<{ filename: string; mode?: "instant" | "archive"; sha?: string }>(res);
}
```

`TeachModal.svelte`: add `let mode = $state<"instant" | "archive">("archive");`, reset it to `"archive"` where `saved` is reset (the `wasOpen` effect), set it in `onSuccess`:

```typescript
      onSuccess: (data) => {
        mode = data?.mode === "instant" ? "instant" : "archive";
        saved = true;
      },
```

and key the success paragraph:

```svelte
      <p class="teach-modal__success-text">
        {mode === "instant"
          ? "Learned. Filed to the repo in the background."
          : "Saved to memory. It arrives with the next delivery."}
      </p>
```

(`teachMutation`'s `onSuccess` in queries.ts keeps its invalidation; the component's own `onSuccess` receives the same data argument.)

- [ ] **Step 4: Run, full UI suite (includes the em-dash copy guard), vendor scan, commit**

Run: `npx vitest run`. Vendor-scan. Commit:

```bash
git add ui/src/lib/api.ts ui/src/components/TeachModal.svelte ui/src/components/ChatPanel.test.ts
git commit -m "feat(ui): teach modal tells the truth about which lane saved the note"
```

---

### Task 8: rollout and live acceptance (controller-run, not a subagent)

- [ ] Build the daedalus runtime image from `feat/teach-lane` with buildx (`--platform linux/amd64 --push`; machine gcloud is broken), digest-verify the tag.
- [ ] Bump the org runtime pin (org_bump_runtime scratch script pattern: GitHub App token PUT to the org config repo's org.yaml), rebuild/re-pin the agents once (the last memory-shaped rebuild), wait for healthy pods on the new runtime.
- [ ] Build + roll the kobo image (buildx, digest-verify), wait ready.
- [ ] Live acceptance on ward: teach a fact via the UI; confirm 200 `mode: "instant"` in seconds; kubectl exec into the pod (container `app`) and verify the techne knowledge row (source `intake:chat-*#sha12`) and the `intake_state` ledger row exist; confirm pod start time UNCHANGED (no rotation); confirmation reply visible in chat and the exchange recorded in the conversations panel; teach a correction to a previously taught fact and verify the stale row retires in-session (superseded without a boot); verify the background archive commit landed in the agent repo.
- [ ] Negative acceptance: from a colleague context (or a forged metadata POST without a valid operator-lane token), confirm refusal/401 and no memory row.
- [ ] Update `.superpowers/sdd/progress.md` and the daedalus PR stack (new PR stacked on #15).

# Fast Lane, Discovery, and Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the proven fast memory lane to knowledge, uploads, and tools; make agents suggest the right colleague and confer in shared threads; and put the room's data on Postgres with agent memory replicated off-cluster and a restore drill actually performed.

**Architecture:** Three arcs in one plan. Arcs 3 and 4 share a seam (the fast lane is the refresh mechanism discovery needs) and are sequenced together. Arc 5 shares no seam with either and can be worked in parallel by a separate implementer. The fast lane extends an existing marker protocol rather than inventing one: today's teach envelope carries `metadata = {"system_task": "teach", "filename": ...}` and the runtime intercepts on that marker pre-model. Every new lane is another marker on the same rail.

**Tech Stack:** FastAPI + SQLAlchemy (kobo `api/`), async Python (daedalus runtime), SQLite FTS5 moving to Postgres full-text search, litestream, pytest, Svelte 5 (kobo `ui/`).

## Global Constraints

- Specs: `docs/superpowers/specs/2026-08-26-fast-lane-and-discovery-design.md` (arcs 3-4) and `docs/superpowers/specs/2026-08-26-resilience-design.md` (arc 5). Their rulings are law.
- TDD, RED first: every behavior change lands with a test that fails against the old code.
- Every test must discriminate the branch it claims. This project has repeatedly shipped tests that passed on the wrong code path; for each test, know what would fail if its target were deleted.
- **Never patch an attribute on a stdlib module object in a test** (for example `monkeypatch.setattr(main_module.time, "monotonic", ...)`). It swaps that function for the entire process and hangs the suite. Assert on the shape of values instead. This cost a real debugging cycle on 2026-08-26.
- **Three repos.** kobo `~/Documents/GitHub/kobo`, daedalus `~/Documents/GitHub/daedalus`, cabal `~/Documents/GitHub/cabal`. Every task names its repo; never commit one repo's change from another.
- Vendor scan before EVERY kobo commit: `git diff --cached --name-only | xargs grep -l -f .superpowers/sdd/vendor-names.txt` must print nothing. Never write a GCP project name, the marketplace repo name, or any vendor name into a tracked kobo file.
- No em dashes in any user-facing copy, docstring, comment, or knowledge text.
- kobo API suite green before each kobo commit: `cd ~/Documents/GitHub/kobo/api && venv/bin/python -m pytest -q`. Cold imports take about 90 seconds; be patient rather than assuming a hang.
- Arc 5 tasks must not change agent-facing behavior. Arc 3 and 4 tasks must not change the storage engine. If a task appears to need both, stop and report.

## File Structure

**Arc 3 (fast lane):**
- `api/kobo_api/clients.py`: envelope builders, one per system task (extends `_build_teach_envelope`, line 1023)
- `api/kobo_api/main.py`: knowledge save/delete (2914, 2943), intake upload (3284), tooling upload (3586), each gaining an instant lane modeled on `teach_agent` (3444)
- `daedalus/src/daedalus/memory/teach.py`: the marker constants and handler family (`SYSTEM_TASK_KEY` line 30, `TEACH_MARKER` line 31)
- `daedalus/src/daedalus/agent.py`: pre-model interception

**Arc 4 (discovery and threads):**
- `api/kobo_api/steps.py`: `_colleague_roster`, plus a refresh entry point
- `api/kobo_api/composer.py`: the standing-knowledge text that carries the suggest-a-colleague instruction
- `api/kobo_api/main.py`: thread routes
- `api/kobo_api/models.py`: the thread and participant rows
- `ui/src/`: thread UI

**Arc 5 (resilience):**
- `api/kobo_api/search.py`: FTS5 to Postgres full-text
- `api/kobo_api/models.py` + a new migrations directory
- `api/kobo_api/config.py`: `db_url` already exists; no new key needed
- `api/kobo_api/composer.py`: the memory replication sidecar in the composed bundle

---

## ARC 3: THE FAST LANE

### Task 1 (kobo): a system-task envelope family

**Files:**
- Modify: `api/kobo_api/clients.py` (`_build_teach_envelope`, line 1023)
- Test: `api/tests/test_clients.py`

**Interfaces:**
- Produces: `_build_system_task_envelope(marker: str, text: str, *, metadata: dict | None = None) -> dict`, returning a single-message task envelope whose `metadata` carries `{"system_task": marker, **metadata}`. `_build_teach_envelope(note, filename)` becomes a thin caller of it and keeps its exact current output, byte for byte.

- [ ] **Step 1: Write the failing tests**

```python
def test_system_task_envelope_carries_the_marker_and_extra_metadata():
    env = _build_system_task_envelope("knowledge", "body text", metadata={"filename": "a.md"})
    assert env["metadata"] == {"system_task": "knowledge", "filename": "a.md"}
    assert env["status"]["message"]["parts"][0]["text"] == "body text"


def test_teach_envelope_output_is_unchanged_by_the_refactor():
    """The runtime intercepts on this exact shape; a drift here silently
    breaks the lane that already works."""
    env = _build_teach_envelope("the note", "note-name.md")
    assert env["metadata"] == {"system_task": "teach", "filename": "note-name.md"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ~/Documents/GitHub/kobo/api && venv/bin/python -m pytest -q tests/test_clients.py -k system_task`
Expected: FAIL, `_build_system_task_envelope` is not defined.

- [ ] **Step 3: Implement**

```python
def _build_system_task_envelope(
    marker: str, text: str, *, metadata: dict | None = None
) -> dict:
    """A single-message task envelope carrying a system-task marker the
    daedalus runtime intercepts pre-model. `marker` names the lane
    (`teach`, `knowledge`, `tool`); `metadata` carries whatever that lane
    needs alongside it. The fast memory lane proved this shape for teach;
    every other instant write rides the same rail rather than opening a
    second door into the agent."""
    envelope = _build_task_envelope([{"role": "user", "text": text}])
    envelope["metadata"] = {"system_task": marker, **(metadata or {})}
    return envelope


def _build_teach_envelope(note: str, filename: str) -> dict:
    """The teach system task (fast-memory-lane spec). `note` is byte-for-byte
    the content the background archive commits to bundle/intake/<filename>,
    so both lanes agree on the content sha."""
    return _build_system_task_envelope("teach", note, metadata={"filename": filename})
```

- [ ] **Step 4: GREEN, full suite, commit**

```bash
cd ~/Documents/GitHub/kobo/api && venv/bin/python -m pytest -q
cd ~/Documents/GitHub/kobo
git add api/kobo_api/clients.py api/tests/test_clients.py
git diff --cached --name-only | xargs grep -l -f .superpowers/sdd/vendor-names.txt || git commit -m "refactor(chat): extract a system-task envelope family from the teach builder"
```

---

### Task 2 (daedalus): a handler family for system tasks

**Files:**
- Modify: `src/daedalus/memory/teach.py` (constants at lines 30-32; `apply_teach` at 86)
- Modify: `src/daedalus/agent.py` (pre-model interception)
- Test: the repo's existing teach test module (`grep -rln "apply_teach" tests/`)

**Interfaces:**
- Consumes: Task 1's envelope shape (`metadata.system_task`).
- Produces: `KNOWLEDGE_MARKER = "knowledge"` and `TOOL_MARKER = "tool"` beside `TEACH_MARKER`; a dispatch that routes on the marker; `apply_knowledge(text, filename, db_path) -> TeachResult` reusing `apply_teach`'s store call, provenance, and `intake_state` ledger stamp.

**Why knowledge reuses apply_teach's internals:** the seeder's parity test already pins that teach and the boot seeder produce identical entries from identical bytes. Knowledge is the same content shape (an `## H2`-sectioned markdown file) written by a different route, so it must produce identical entries too, and reusing the same store path is what guarantees that rather than a second implementation that drifts.

- [ ] **Step 1: Write the failing tests**

In the existing teach test module, mirroring its fixtures:
- A `knowledge`-marked task stores entries identical to what `apply_teach` produces for the same bytes.
- Its sha lands in the `intake_state` ledger, so the boot seeder will not re-ingest it.
- An unknown marker is refused, and the refusal does NOT fall through to the model.
- The existing teach tests still pass unchanged (the refactor must not move teach's behavior).

- [ ] **Step 2: Run to verify they fail**

Run the repo's test command (confirm it from the README or Makefile first). Expected: FAIL on the missing `apply_knowledge`.

- [ ] **Step 3: Implement**

Add the markers beside `TEACH_MARKER`. Extract the dispatch in `agent.py` so it selects a handler by marker rather than testing for `teach` alone. `apply_knowledge` delegates to the same store call and ledger stamp `apply_teach` uses; do not copy the sectioning or typing logic.

Authorization is unchanged and must stay unchanged: the existing gate on the transport-stamped verified JWT subject, which explicitly refuses `agent:<name>` subjects so colleagues cannot mint operator-grade facts. Knowledge writes carry exactly the same requirement.

- [ ] **Step 4: GREEN, suite, commit on a feature branch**

Cut a branch from `main` (this repo merges via PR; `git log --oneline -5` confirms the convention). Commit:
`feat(memory): knowledge writes ride the teach lane's rail`

---

### Task 3 (kobo): knowledge edits ride the fast lane

**Files:**
- Modify: `api/kobo_api/main.py` (`save_knowledge` 2914, and the delete route 2943)
- Test: `api/tests/test_api_knowledge.py`

**Interfaces:**
- Consumes: Task 1's `_build_system_task_envelope`.

- [ ] **Step 1: Write the failing tests**

Model them on the existing teach-route tests (`grep -n "mode.*instant" api/tests/`), which already pin the two-lane shape:
- With the switchboard wired and the relay completing, the response reports the instant mode AND the archive commit is a background task, not a synchronous CI-triggering commit.
- With the relay failing, it degrades to the archive lane exactly as teach does, and the caller still gets a success rather than a 502.
- The archived bytes are identical to the bytes sent over the relay, so both lanes agree on the content sha.

- [ ] **Step 2: Run to verify they fail**

Expected: FAIL, the route commits synchronously and reports no mode.

- [ ] **Step 3: Implement**

Mirror `teach_agent`'s structure (main.py:3487-3512) exactly: the `cabal_enabled(cfg) and clients.cabal is not None` gate, the same catch set including the not-completed check, the background archive on success, and the synchronous archive fallback on any miss. Do not invent a different degradation path.

- [ ] **Step 4: GREEN, full suite, commit**

`feat(knowledge): knowledge edits ride the fast lane instead of a rebuild`

---

### Task 4 (kobo): memory uploads ride the fast lane

**Files:**
- Modify: `api/kobo_api/main.py` (`/api/agents/{name}/intake`, line 3284)
- Test: `api/tests/test_api_knowledge.py` or the intake test module

Same shape as Task 3. The one difference to handle: an upload carries a filename from the client, so it must pass the room's existing intake filename rule before it is sent (the runtime enforces `^[a-z0-9][a-z0-9-]{0,39}\.md$` and will refuse otherwise; refusing early gives a better message).

Pin: an upload whose filename would be refused by the runtime is refused by the room first, with a message naming the rule.

Commit: `feat(intake): memory uploads ride the fast lane`

---

### Task 5 (kobo + daedalus): tools ride the fast lane, loaded at boot

**Files:**
- Modify: `api/kobo_api/main.py` (`/api/agents/{name}/tooling`, line 3586)
- Modify: daedalus `src/daedalus/memory/teach.py` (or a sibling module for the tool handler)
- Test: `api/tests/test_api_tooling.py`, plus the daedalus handler tests

**The ruling this task encodes:** tools are delivered fast but loaded at boot. No hot-reload into a running process.

- [ ] **Step 1: kobo side, failing tests first**

- A tool upload is validated by the room (the existing tooling contract) and then delivered over the fast lane.
- The response tells the caller the tool arrives on restart, in plain copy, so the UI can say so honestly.
- A tool that fails the room's validation is refused BEFORE any delivery.

- [ ] **Step 2: daedalus side, failing tests first**

- **The runtime re-validates the tool itself before writing it.** The fast lane skips CI by definition, so the runtime must not trust that validation already happened. A tool that fails the tooling contract is refused at the runtime even when the room is bypassed. This is the security-critical test of this task: write it first and make sure it fails for the right reason.
- A valid tool is written into the agent's live bundle tools directory.
- The enforcement loader is NOT re-entered; loading still happens only at boot.

- [ ] **Step 3: Implement both halves**

The runtime's re-validation must be the same mechanical checks the room applies (size, UTF-8, non-empty, ast parse, import allowlist, entrypoint shape). If those checks live in kobo only, port them to the runtime rather than importing across repos; state in your report which you did.

- [ ] **Step 4: Restart the pod to load it**

The room triggers a rollout restart after a successful tool delivery. Pin that the restart is requested exactly once per delivery.

- [ ] **Step 5: GREEN both repos, commit each separately**

kobo: `feat(tooling): tools deliver over the fast lane and load on restart`
daedalus: `feat(tools): accept tool writes over the system-task rail, re-validated`

---

### Task 6 (kobo): the roster refresh lane

**Files:**
- Modify: `api/kobo_api/steps.py` (`_colleague_roster`)
- Modify: `api/kobo_api/main.py` (agent create and destroy paths)
- Test: `api/tests/test_steps.py`, `api/tests/test_api_agents.py`

**This is the seam that joins arcs 3 and 4.** Today `compose()` runs once at agent creation, so `colleagues.md` is frozen at birth. This delivers a regenerated roster over the fast lane when the roster changes.

- [ ] **Step 1: Write the failing tests**

- Creating an agent redelivers the roster to agents that can see it, and NOT to agents that cannot. The negative half is the assertion that matters: a room with agents in two groups must not cross-deliver.
- Destroying an agent removes it from the rosters of agents that could see it.
- Redelivery uses the fast lane, not a rebuild.

- [ ] **Step 2: Implement**

Reuse `_colleague_roster`'s existing visibility rule; do not write a second one. Deliver via a `knowledge` system task carrying the regenerated `colleagues.md`.

- [ ] **Step 3: GREEN, full suite, commit**

`feat(colleagues): regenerate and deliver the roster when it changes`

---

## ARC 4: DISCOVERY AND SHARED THREADS

### Task 7 (kobo): the suggest-a-colleague instruction

**Files:**
- Modify: `api/kobo_api/composer.py` (the standing-knowledge text)
- Test: `api/tests/test_composer.py`

- [ ] **Step 1: Failing test, then implement**

Extend the roster knowledge with the behavior instruction. Exact copy, no em dashes:

```
When a question is squarely in a colleague's field and outside yours, say
so plainly, name the colleague whose capability fits, and offer to bring
them in. Do not ask them silently on the user's behalf, and do not guess
at an answer you do not have. "I don't know yet, but {name} works on
exactly this" is a good answer.
```

Pin the copy by exact substring so drift must be deliberate.

- [ ] **Step 2: Capability fallback**

When an agent's capability text is empty, the roster line falls back to its purpose tags, and when those are also empty, to its skills. Test each fallback level. An agent nobody can match is invisible to this feature, which is why the fallback is required rather than nice to have.

- [ ] **Step 3: Commit**

`feat(composer): agents name a relevant colleague instead of guessing`

---

### Task 8 (kobo): thread rows and creation

**Files:**
- Modify: `api/kobo_api/models.py`
- Modify: `api/kobo_api/main.py`
- Test: `api/tests/test_api_threads.py` (new)

**Interfaces:**
- Produces: a `Thread` row (id, initiator email, created_at) and a `ThreadParticipant` row (thread_id, agent_name). Confirm naming against the file's existing conventions before writing.

- [ ] **Step 1: Failing tests for creation and participation**

- The initiator creates a thread naming one agent it can see.
- Adding a same-group agent succeeds.
- **Adding a cross-group agent WITHOUT an accepted invite is refused**, proven from a non-operator identity. This reuses the existing consent handshake; it introduces no new consent concept and must not bypass the existing one.
- Adding an agent the initiator cannot see is refused with the room's existing anti-enumeration answer, not a distinguishable error.

- [ ] **Step 2: Implement, then GREEN and commit**

`feat(threads): a shared thread with capability-scoped participants`

---

### Task 9 (kobo): thread turns and visibility

**Files:**
- Modify: `api/kobo_api/main.py`
- Test: `api/tests/test_api_threads.py`

- [ ] **Step 1: Failing tests**

- A turn is delivered to participants in sequence, each seeing the messages that came before, so a later answer can build on an earlier one. Not a parallel fan-out.
- **A thread is readable by its initiator and by nobody else** (operator ruling). Specifically: a member of a participating agent's own group who is not the initiator gets the room's not-found answer. Write this negative test first; it is the ruling's whole point.
- The thread does not appear in any participant's conversation feed.
- An agent is never asked to answer twice within one turn.

- [ ] **Step 2: Implement, GREEN, commit**

`feat(threads): sequential turns, initiator-only visibility`

---

### Task 10 (kobo ui): the thread surface

**Files:**
- Create: `ui/src/components/ThreadPanel.svelte` + co-located test
- Modify: `ui/src/lib/api.ts`, `ui/src/lib/queries.ts`, the chat surface

- [ ] **Step 1: api and query wrappers, tests first**

Mirror the existing fetch and query idioms exactly; read the neighbouring wrappers before writing.

- [ ] **Step 2: The panel**

The agent's suggestion renders as an offer with the colleague's name and why they fit; accepting adds them to the thread. Participants' answers are attributed by agent name. Follow the app's existing visual language; do not introduce a new pattern.

- [ ] **Step 3: UI suite, commit**

Run: `cd ~/Documents/GitHub/kobo/ui && npx vitest run`
`feat(ui): shared threads with suggested participants`

---

## ARC 5: RESILIENCE (parallel-safe with arcs 3 and 4)

### Task 11 (kobo): introduce migrations

**Files:**
- Create: `api/migrations/` (alembic), `api/alembic.ini`
- Modify: `api/pyproject.toml` or requirements

**Why first:** the schema currently evolves by `create_all` plus hand-written `ALTER TABLE` run by an operator. Postgres cannot be reached safely without a migration path, and this must exist before any engine change.

- [ ] **Step 1: Add alembic, generate the baseline from the current models**

- [ ] **Step 2: Test that the baseline applies to an empty database and produces the current schema**

- [ ] **Step 3: Commit**

`chore(db): introduce migrations ahead of the engine change`

---

### Task 12 (kobo): port catalog search off FTS5

**Files:**
- Modify: `api/kobo_api/search.py`
- Test: `api/tests/test_search.py`

**This is the substantive part of the migration.** The catalog is a SQLite FTS5 virtual table with `MATCH` queries and a hand-written escaper (`_escape_match_token`, `_build_match`) that turns hostile input into literal phrases.

- [ ] **Step 1: Preserve the escaping contract first**

The existing hostile-input tests are the contract. Read them, and make them engine-agnostic BEFORE changing the backend, so they fail against a naive port. A query containing `OR`, unbalanced parentheses, a stray dash, or an embedded quote must still land as a literal phrase search on the new backend.

- [ ] **Step 2: Implement Postgres full-text alongside, selected by engine**

Keep both paths until cutover: SQLite for local development and existing tests, Postgres for the deployed room. The selection reads from the existing `db_url`; no new config key.

- [ ] **Step 3: The full suite passes on BOTH engines**

This is the acceptance for this task. Report both runs.

- [ ] **Step 4: Commit**

`feat(search): catalog search runs on Postgres full-text as well as FTS5`

---

### Task 13 (kobo): the step engine under two processes

**Files:**
- Modify: `api/kobo_api/steps.py` and/or the engine module
- Test: `api/tests/test_engine.py`

The engine runs daemon threads against a single file today, and `reset_failed_steps`' resume story is process-local in spirit. Multi-replica is the point of the migration.

- [ ] **Step 1: Failing test, two processes against one database, one resuming a failed step**

- [ ] **Step 2: Implement whatever locking or claim the test proves is needed**

Prefer a row-level claim over an advisory lock unless the test shows otherwise; report which and why.

- [ ] **Step 3: Commit**

`fix(engine): a failed step resumes safely with more than one process`

---

### Task 14 (kobo): agent memory replication in the bundle

**Files:**
- Modify: `api/kobo_api/composer.py`
- Test: `api/tests/test_composer.py`

Every agent is born with its techne database replicating continuously to object storage, using the switchboard's sidecar pattern but targeting object storage rather than a neighbouring volume in the same zone.

- [ ] **Step 1: Failing test that a composed bundle carries the replication config**

- [ ] **Step 2: Implement, GREEN, commit**

`feat(composer): agent memory replicates off-cluster from birth`

---

### Task 15 (operator): the restore drill

**Not a code task.** The deliverable is a drill performed end to end, then written down.

- [ ] **Step 1: Restore the room's database to a fresh instance**
- [ ] **Step 2: Re-provision one agent from its repo using the room's own eight-step checklist**
- [ ] **Step 3: Reattach that agent's memory from its replica**
- [ ] **Step 4: Ask the restored agent something only its memory knows, and get it right**

Step 4 is the acceptance. Write the runbook afterward, as the artifact of having done it.

---

### Task 16: docs, spec status, ledger

- [ ] Flip both specs to `Implemented (rollout pending)`, append execution amendments to each, update the README where behavior changed (the fast lane and threads are both user-visible), and record the wave in the ledger.

---

## Rollout

1. **Arcs 3 and 4 together:** daedalus changes merge and the runtime pin moves; kobo ships in its image; agents pick up delivery changes on their next rebuild. Acceptance is a stopwatch (edit knowledge, ask seconds later, confirm no image build ran) plus the thread negative test performed by a second person.
2. **Arc 5 separately and later:** provision Postgres with automated backups and point-in-time recovery; run the migration and the drill against a copy FIRST; cut over with a config change and a restart, retaining the SQLite file read-only until the drill has been repeated against the live instance.
3. Do not roll arc 5 and arcs 3-4 in the same window. If something breaks, you want to know which arc did it.

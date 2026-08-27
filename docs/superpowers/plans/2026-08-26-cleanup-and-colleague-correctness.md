# Cleanup and Colleague Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove destroy-lane leftovers, close the invite-cleanup gap, and fix the colleague behavior issue #5 captured (self-referential delegation, unbounded loops, irrelevant routing) so the smart-discovery arc has a sane foundation.

**Architecture:** Nothing new is built. Every fix connects machinery that already exists: cabal's default-on loop guard gets the `contextId` it needs, the composer's proven standing-knowledge lane swaps its placeholder gotchas content for a self-card and a colleague roster, and destroy's existing row-deletion step gains the invite rows it forgot.

**Tech Stack:** FastAPI + SQLAlchemy (kobo `api/`), async Python + contextvars (daedalus runtime), pytest, gcloud/kubectl for the operator sweep.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-26-cleanup-and-colleague-correctness-design.md`. Its rulings are law.
- TDD, RED first: every behavior change lands with a test that fails against the old code. Run the failing test BEFORE implementing.
- Every test must discriminate the branch it claims. This wave's predecessor shipped tests that passed on the wrong code path; for each test, know what would fail if its target were deleted, and where a cheap in-memory mutation proves it, run it and record the verbatim FAIL then the restored PASS.
- **Two repos.** kobo at `~/Documents/GitHub/kobo`, daedalus at `~/Documents/GitHub/daedalus`. Every task names its repo. Never commit one repo's change from the other.
- Vendor scan before EVERY kobo commit: `git diff --cached --name-only | xargs grep -l -f .superpowers/sdd/vendor-names.txt` must print nothing. Never write a GCP project name, the marketplace repo name, or any vendor name into a tracked kobo file. Resolve those from live coordinates at runtime instead.
- No em dashes in any user-facing copy, docstring, comment, or knowledge text.
- kobo API suite green before each kobo commit: `cd ~/Documents/GitHub/kobo/api && venv/bin/python -m pytest -q`. This environment is slow (cold imports about 90s, full suite about 1 to 7 minutes). Run focused tests while iterating and the full suite once before committing. Be patient rather than assuming a hang.
- daedalus suite green before each daedalus commit: `cd ~/Documents/GitHub/daedalus && python -m pytest -q` (confirm the runner from the repo's own README/Makefile before first use; do not invent one).
- Agents pick up composer changes on their next delivery. No task in this plan rebuilds a live agent.

## File Structure

**kobo:**
- `api/kobo_api/destroy.py`: `_destroy_forget` gains invite deletion (Task 1)
- `api/kobo_api/clients.py`: `_build_task_envelope` gains `context_id` (Task 3)
- `api/kobo_api/composer.py`: standing self-card knowledge (Task 4), roster knowledge (Task 5)
- `api/kobo_api/main.py`: passes a stable context id, supplies roster to the spec (Tasks 3, 5)
- Tests: `api/tests/test_destroy.py`, `api/tests/test_clients.py`, `api/tests/test_composer.py`

**daedalus:**
- `src/daedalus/tools/colleagues.py`: propagate the inbound context id (Task 6)
- `src/daedalus/agent.py`: set the context var per task (Task 6)
- Tests: the repo's existing colleagues test module (locate it before writing; do not create a parallel one)

**Not tracked:** the orphan sweep script (Task 2) lives in the scratchpad because it must name live GCP coordinates, which the vendor rule forbids in tracked files.

---

### Task 1: Destroy deletes the departing agent's colleague invites

**Files:**
- Modify: `api/kobo_api/destroy.py` (`_destroy_forget`, around line 325)
- Test: `api/tests/test_destroy.py`

**Interfaces:**
- Consumes: `A2AInvite` model from `kobo_api.models` (confirm the exact class name by grepping `a2a_invites` in `api/kobo_api/models.py` before writing; use whatever it actually is).
- Produces: no new callable. `_destroy_forget` keeps its `(ctx: DestroyCtx) -> str` signature and its `"forgotten"` return.

- [ ] **Step 1: Confirm the model name and columns**

Run: `grep -n -A12 "a2a_invites" ~/Documents/GitHub/kobo/api/kobo_api/models.py`
Note the class name and confirm the columns are `from_agent` and `to_agent` (both agent NAME strings, per the live rows). Use the real names in every step below.

- [ ] **Step 2: Write the failing test**

Append to `api/tests/test_destroy.py`, reusing that file's existing session/agent fixtures (read its neighbouring destroy-step tests first and mirror their setup exactly):

```python
def test_destroy_forget_deletes_invites_in_both_directions(...):
    """Destroy's forget step owns row deletion; colleague invites naming the
    departing agent are rows it used to leave behind. All three surviving
    production rows referenced destroyed agents."""
    # Seed: the agent being destroyed, plus three invites --
    #   (departing -> other), (other -> departing), (other -> unrelated)
    # Run _destroy_forget(ctx) for the departing agent.
    # Assert: both invites naming the departing agent are gone,
    #         and the unrelated invite SURVIVES.
```

Write it concretely against the file's real fixtures. The surviving-unrelated-invite assertion is required: without it, a `delete()` with no filter would pass.

- [ ] **Step 3: Run to verify it fails**

Run: `cd ~/Documents/GitHub/kobo/api && venv/bin/python -m pytest -q tests/test_destroy.py -k invites`
Expected: FAIL, because the invite rows still exist after the call.

- [ ] **Step 4: Implement**

In `_destroy_forget`, before `session.delete(agent)`:

```python
    # Colleague invites are rows this step owns too (cleanup arc): destroy
    # used to leave them behind, so a destroyed agent kept appearing in
    # live agents' invite lists. Both directions, matched on the agent's
    # NAME, which is what the invite columns store.
    session.query(A2AInvite).filter(
        or_(A2AInvite.from_agent == agent.name, A2AInvite.to_agent == agent.name)
    ).delete(synchronize_session=False)
```

Add `or_` to the existing `sqlalchemy` import line and `A2AInvite` (real name from Step 1) to the models import.

- [ ] **Step 5: Verify GREEN and mutation-pin**

Run the focused test: expect PASS. Then delete the `or_(...)` clause so it matches nothing, re-run, confirm FAIL, restore, confirm PASS. Record both runs verbatim in your report.

- [ ] **Step 6: Full suite and commit**

```bash
cd ~/Documents/GitHub/kobo/api && venv/bin/python -m pytest -q
cd ~/Documents/GitHub/kobo
git add api/kobo_api/destroy.py api/tests/test_destroy.py
git diff --cached --name-only | xargs grep -l -f .superpowers/sdd/vendor-names.txt || git commit -m "fix(destroy): forget step deletes the departing agent's colleague invites"
```

---

### Task 2: Orphan sweep script (operator-run, scratchpad only)

**Files:**
- Create: `<scratchpad>/orphan_sweep.py`, NOT committed to any repo (it names live GCP coordinates; the vendor rule forbids that in tracked files)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: a report-then-confirm script the operator runs.

- [ ] **Step 1: Write the script**

It must, in order:

1. Read the live agent names from the room DB and the live namespaces from the cluster:
   `kubectl -n kobo exec deploy/kobo -- python3 -c "<select name from agents>"` and `kubectl get ns`.
2. List candidate orphans: service accounts matching `agent-*` in the room's GCP project, and Artifact Registry packages under the shared repo path.
3. Subtract, in this order:
   - **the protected list, hardcoded and commented**: `iris`, `cinder` (operator ruling 2026-08-26: another team member's, live, not kobo-built), `cabal`, `daedalus-runtime`, `kobo` (platform images)
   - any id with a live agent row
   - any id with a live namespace
4. Print the resulting delete list and STOP. Deleting requires re-running with `--confirm`.
5. With `--confirm`, delete each and print the result per item.

Resolve the project id, AR repo path, and namespace from live coordinates (`kubectl`/`gcloud config`), never hardcoded.

- [ ] **Step 2: Dry run and read the list**

Run without `--confirm`. Expected candidates, per the spec's verified inventory: service accounts `agent-{sage,muse,hello,warden,clio,echo,runtime}` and AR packages `clio, echo, hello, muse, sage`. If the script proposes anything on the protected list, or anything with a live row or namespace, STOP and report a bug in the filter rather than proceeding.

- [ ] **Step 3: Operator confirms, then delete**

Show the operator the list. Only on explicit approval, re-run with `--confirm`.

- [ ] **Step 4: Verify and report**

Re-run the dry run; expected output is an empty delete list. Report the before and after lists. There is no commit for this task; say so explicitly.

---

### Task 3 (kobo): stamp a stable context id on the task envelope

**Files:**
- Modify: `api/kobo_api/clients.py` (`_build_task_envelope`, lines 984-1004)
- Modify: `api/kobo_api/main.py` (the two `_build_task_envelope(converse_messages)` call sites near lines 2674 and 3470)
- Test: `api/tests/test_clients.py`

**Interfaces:**
- Produces: `_build_task_envelope(messages: list[ChatMessage], *, context_id: str | None = None) -> dict`. When `context_id` is given, the returned envelope carries `"contextId": context_id`. When omitted, the envelope is byte-identical to today's (so `HelloStep` and the direct path are untouched). Task 6 relies on the key being spelled exactly `contextId`.

- [ ] **Step 1: Write the failing tests**

Append to `api/tests/test_clients.py` beside the existing envelope tests:

```python
def test_build_task_envelope_carries_context_id_when_given():
    env = _build_task_envelope([{"role": "user", "text": "hi"}], context_id="ctx-abc")
    assert env["contextId"] == "ctx-abc"


def test_build_task_envelope_omits_context_id_by_default():
    """The direct path and HelloStep must keep their exact envelope."""
    env = _build_task_envelope([{"role": "user", "text": "hi"}])
    assert "contextId" not in env
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ~/Documents/GitHub/kobo/api && venv/bin/python -m pytest -q tests/test_clients.py -k context_id`
Expected: FAIL with `TypeError: _build_task_envelope() got an unexpected keyword argument`.

- [ ] **Step 3: Implement**

```python
def _build_task_envelope(
    messages: list[ChatMessage], *, context_id: str | None = None
) -> dict:
```

and before the return, after building `envelope` as a local:

```python
    envelope = {
        "kind": "task",
        "id": f"chat-{uuid.uuid4().hex}",
        "status": {
            "state": "submitted",
            "message": _to_a2a_message(current),
        },
        "history": [_to_a2a_message(message) for message in prior],
    }
    # The switchboard's loop guard groups hops by contextId and is default-on;
    # without a caller-supplied id every hop looks like a fresh conversation
    # and the guard is inert. Absent when not supplied, so the direct path
    # and first-contact keep their exact envelope.
    if context_id is not None:
        envelope["contextId"] = context_id
    return envelope
```

Extend the docstring with the same reasoning in the file's register.

- [ ] **Step 4: Pass the id from the chat paths**

At both `main.py` call sites, pass a stable per-conversation id. Use the room's existing conversation identity if one is already in scope at that point (grep the surrounding function for a task/conversation id first and PREFER it); only if none exists, derive one deterministically from the agent name plus the conversation, and state in your report which you used and why:

```python
            envelope = _build_task_envelope(converse_messages, context_id=context_id)
```

- [ ] **Step 5: GREEN, mutation-pin, commit**

Run the focused tests (PASS), then delete the `if context_id is not None:` block, confirm the first test FAILS, restore, confirm PASS. Full suite, vendor scan, then:

```bash
git add api/kobo_api/clients.py api/kobo_api/main.py api/tests/test_clients.py
git commit -m "feat(chat): stamp a stable contextId so the switchboard loop guard can see a chain"
```

---

### Task 4 (kobo): standing self-card knowledge, replacing gotchas.md

**Files:**
- Modify: `api/kobo_api/composer.py` (DELETE `_GOTCHAS_MD` lines 37-42; `BundleSpec` around line 50; `compose` around line 896)
- Test: `api/tests/test_composer.py` (three existing gotchas assertions: lines 77, 93, and `test_compose_knowledge_gotchas_content` at 873)

**Interfaces:**
- Consumes: `BundleSpec` fields that already exist for the agent card (grep `_agent_card` in composer.py to see exactly which of name/team/purpose_tags/capability are already on the spec; ADD only what is missing).
- Produces: `bundle/knowledge/self.md` written by `compose`, alongside the existing `gotchas.md`.

**Why this shape:** `_GOTCHAS_MD` proved the lane works. Agents quote it verbatim on the wire ("assembled in the kobo assembly room and deployed through the platform chain"), so composer-injected standing knowledge demonstrably reaches the model. `self.md` reuses that lane for facts that are actually useful.

**And it REPLACES gotchas.md, which is deleted (operator ruling 2026-08-26).** That content was provenance trivia, and issue #5 shows it doing harm: asked which GCP project it ran in, gemini-one padded the answer with "However, I can tell you how I got here: I was assembled in the kobo assembly room...". Its "if someone asks how you got here, tell them that" instruction gets over-applied to questions that are not that, and it makes every agent's first reply identical boilerplate instead of something its own persona would say. Verified dependency surface before removal: only composer.py writes it and only test_composer.py asserts it; `test_knowledge.py`'s "gotchas.md" strings are an arbitrary filename fixture and must NOT be changed.

- [ ] **Step 1: Write the failing test**

In `api/tests/test_composer.py`, beside the existing compose-output tests (reuse their `BundleSpec` construction helper):

```python
def test_compose_writes_self_card_knowledge(tmp_path):
    spec = _spec(name="kestrel", ...)  # this file's existing helper
    compose(spec, tmp_path)
    text = (tmp_path / "bundle" / "knowledge" / "self.md").read_text()
    assert "kestrel" in text
    # the standing instruction, pinned by content so a future edit is deliberate
    assert "questions about you are yours to answer" in text
    assert "I don't know yet" in text
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd ~/Documents/GitHub/kobo/api && venv/bin/python -m pytest -q tests/test_composer.py -k self_card`
Expected: FAIL with `FileNotFoundError` on `knowledge/self.md`.

- [ ] **Step 3: Implement**

Add beside `_GOTCHAS_MD`:

```python
def _self_md(spec: "BundleSpec") -> str:
    """Standing self-knowledge, same lane as _GOTCHAS_MD (which the wire
    shows agents quote verbatim). Issue #5: asked which project it ran in,
    an agent asked a colleague about ITSELF, and that colleague asked it
    back. Self-targeting was already blocked; what was missing is facts
    about itself and the instruction not to delegate questions about
    itself."""
    tags = ", ".join(spec.purpose_tags) if spec.purpose_tags else "none"
    return (
        "## Who you are\n"
        f"You are {spec.name}. Your group is {spec.group}. "
        f"Your purpose tags are: {tags}.\n"
        f"What you are for: {spec.capability or 'not yet distilled'}.\n"
        "\n"
        "Questions about you are yours to answer: your name, your group, "
        "what you are for, what you remember. Never ask a colleague about "
        "yourself. When you genuinely do not have a fact about yourself, "
        "say \"I don't know yet\" and stop, rather than asking someone else "
        "to answer it for you.\n"
    )
```

Use the real `BundleSpec` field names from Step 1's grep. Add any missing field to `BundleSpec` with a default that keeps existing constructions valid, and populate it at the `main.py`/`steps.py` call site that builds the spec (grep `BundleSpec(` to find every construction and update them all).

In `compose`, REPLACE the gotchas write with:

```python
    (knowledge_dir / "self.md").write_text(_self_md(spec))
```

Delete the `_GOTCHAS_MD` constant entirely. In `compose`'s docstring Produces list, replace the `dest/bundle/knowledge/gotchas.md` line with `dest/bundle/knowledge/self.md`.

Then update the three test references that pin the old file: the two bundle-tree assertions (`test_composer.py:77` and `:93`) change `Path("bundle/knowledge/gotchas.md")` to `Path("bundle/knowledge/self.md")`, and `test_compose_knowledge_gotchas_content` (`:873`) is REPLACED by the self-card test from Step 1 rather than kept alongside it. Leave `test_knowledge.py` alone: its "gotchas.md" strings are an arbitrary filename fixture for the knowledge-save path and have nothing to do with the composer.

Watch for one consequence and report it: new agents' first-contact replies will no longer recite the deployment chain, because that content is gone. That is the intended outcome, not a regression. If any test asserts that phrasing in a first_reply, report it rather than silently rewriting the assertion.

- [ ] **Step 4: GREEN, mutation-pin, commit**

Focused test PASS. Then change the write to a different filename, confirm FAIL, restore, confirm PASS. Full suite, vendor scan, then:

```bash
git add api/kobo_api/composer.py api/tests/test_composer.py api/kobo_api/main.py api/kobo_api/steps.py
git commit -m "feat(composer): self-card knowledge replaces gotchas.md so agents answer about themselves"
```

---

### Task 5 (kobo): colleague roster knowledge

**Files:**
- Modify: `api/kobo_api/composer.py` (`BundleSpec`, `compose`)
- Modify: the spec-construction call site (grep `BundleSpec(`)
- Test: `api/tests/test_composer.py`

**Interfaces:**
- Consumes: Task 4's `knowledge_dir` write pattern.
- Produces: `BundleSpec.colleagues: list[ColleagueEntry]` (a small dataclass or plain dicts with `name`, `team`, `purpose_tags`, `capability`, matching the shape `_format_colleague` uses in daedalus so the two read alike), and `bundle/knowledge/colleagues.md`.

- [ ] **Step 1: Write the failing tests**

```python
def test_compose_writes_colleague_roster(tmp_path):
    spec = _spec(name="kestrel", colleagues=[
        {"name": "ward", "team": "infra-engineering",
         "purpose_tags": ["security"], "capability": "keeps our workshop's digital info safe"},
    ])
    compose(spec, tmp_path)
    text = (tmp_path / "bundle" / "knowledge" / "colleagues.md").read_text()
    assert "ward" in text
    assert "keeps our workshop's digital info safe" in text


def test_compose_roster_omits_the_agent_itself(tmp_path):
    spec = _spec(name="kestrel", colleagues=[
        {"name": "kestrel", "team": "vidai", "purpose_tags": [], "capability": "x"},
        {"name": "ward", "team": "infra-engineering", "purpose_tags": [], "capability": "y"},
    ])
    compose(spec, tmp_path)
    text = (tmp_path / "bundle" / "knowledge" / "colleagues.md").read_text()
    assert "ward" in text
    assert "kestrel" not in text.split("## Your colleagues")[1]


def test_compose_roster_handles_no_colleagues(tmp_path):
    spec = _spec(name="kestrel", colleagues=[])
    compose(spec, tmp_path)
    text = (tmp_path / "bundle" / "knowledge" / "colleagues.md").read_text()
    assert "no colleagues" in text.lower()
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ~/Documents/GitHub/kobo/api && venv/bin/python -m pytest -q tests/test_composer.py -k roster`
Expected: FAIL (no such file / unexpected `colleagues` kwarg).

- [ ] **Step 3: Implement**

```python
def _colleagues_md(spec: "BundleSpec") -> str:
    """The roster, as standing knowledge rather than a tool call. The data
    was always available (daedalus's list_colleagues already formats name,
    team, purpose tags and capability) but only if the model chose to call
    it, and issue #5 shows it skipping that and asking a test fixture a GCP
    question. Knowing who exists is not a tool call.

    Point-in-time: refreshed on delivery. list_colleagues stays the live
    read, ask_colleague stays the action."""
    others = [c for c in spec.colleagues if c["name"] != spec.name]
    lines = ["## Your colleagues", ""]
    if not others:
        lines.append("You have no colleagues in the room yet.")
    else:
        lines.append(
            "These are the other agents you can reach, and what each is for. "
            "When a question is squarely in someone else's field and outside "
            "yours, say so and name them rather than guessing. Match on what "
            "they are for, not on whose name you happen to remember."
        )
        lines.append("")
        for c in others:
            tags = ", ".join(c.get("purpose_tags") or []) or "none"
            lines.append(
                f"- {c['name']} (team: {c.get('team') or 'unspecified'}), "
                f"purpose tags: {tags}. "
                f"What they are for: {c.get('capability') or 'not yet distilled'}"
            )
    return "\n".join(lines) + "\n"
```

Add `colleagues: list[dict] = field(default_factory=list)` to `BundleSpec`, write the file in `compose` next to `self.md`, and add it to the docstring.

At the spec-construction call site, populate `colleagues` from the room's own view of who this agent may reach: agents in the same group, plus any with an ACCEPTED invite in either direction. Read the existing visibility helper rather than writing a new rule (grep for the helper the agents-list route uses and reuse it); if none is reusable, use `group_name` equality plus accepted `a2a_invites`, and say so in your report.

- [ ] **Step 4: GREEN, mutation-pin, commit**

All three focused tests PASS. Then remove the `c["name"] != spec.name` filter, confirm the omits-itself test FAILS, restore, confirm PASS. Full suite, vendor scan, then:

```bash
git add api/kobo_api/composer.py api/tests/test_composer.py api/kobo_api/main.py api/kobo_api/steps.py
git commit -m "feat(composer): colleague roster as standing knowledge, not a tool call"
```

---

### Task 6 (daedalus): propagate the inbound context id to colleague asks

**Files:**
- Modify: `src/daedalus/tools/colleagues.py` (`_ask_colleague`, envelope around lines 210-224)
- Modify: `src/daedalus/agent.py` (`handle_task`, line 256)
- Test: the repo's existing colleagues test module (locate with `grep -rln "ask_colleague" tests/`)

**Interfaces:**
- Consumes: `Task.context_id` (already on the model, `src/daedalus/task.py:53`, already parsed from inbound payloads at `src/daedalus/transports/a2a.py:101` and `:205`).
- Produces: a module-level `contextvars.ContextVar[str | None]` in `colleagues.py` named `current_context_id`, set by `Agent.handle_task`, read by `_ask_colleague`.

**Why a contextvar:** `Agent` invokes tools as `self.tools.call(name, call.arguments)` (agent.py:437), arguments only, no task context. A contextvar propagates through the async call chain without changing the tool protocol for every provider, and is safe when an agent handles tasks concurrently. Do NOT store it as mutable state on the provider instance.

- [ ] **Step 1: Write the failing test**

In the existing colleagues test module, mirroring its fake-client idiom:

```python
def test_ask_colleague_propagates_the_inbound_context_id(...):
    """The switchboard's loop guard groups hops by contextId. Without this,
    every hop mints a fresh id and the guard is inert -- which is how
    gemini-one and probe looped five times in issue #5."""
    # set current_context_id to "ctx-from-inbound"
    # call _ask_colleague through the provider with a fake post client
    # assert the captured envelope's task carries contextId == "ctx-from-inbound"
```

Plus a second test asserting that with no context set, the envelope is unchanged from today (no `contextId` key), so nothing else regresses.

- [ ] **Step 2: Run to verify it fails**

Run the focused test. Expected: FAIL, because the envelope has no `contextId`.

- [ ] **Step 3: Implement**

In `colleagues.py`, at module level:

```python
import contextvars

# The task context id currently being handled, set by Agent.handle_task.
# The switchboard's DoomLoopGuardrail is default-on but groups hops by
# contextId; without a threaded id each hop looks like a fresh conversation
# and the guard never fires. A contextvar rather than provider state: tools
# are invoked with arguments only, and an agent may handle tasks
# concurrently.
current_context_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_context_id", default=None
)
```

In `_ask_colleague`, after building `envelope`:

```python
        context_id = current_context_id.get()
        if context_id is not None:
            envelope["task"]["contextId"] = context_id
```

In `agent.py`'s `handle_task`, at the top of the method:

```python
        from daedalus.tools.colleagues import current_context_id
        current_context_id.set(task.context_id or task.id)
```

Match the file's existing import convention (module-level if that is the house style; deferred only if there is a real cycle, and say which in your report).

- [ ] **Step 4: GREEN, mutation-pin, commit**

Focused tests PASS. Then delete the `envelope["task"]["contextId"] = context_id` line, confirm FAIL, restore, confirm PASS.

```bash
cd ~/Documents/GitHub/daedalus && python -m pytest -q
git add src/daedalus/tools/colleagues.py src/daedalus/agent.py tests/
git commit -m "fix(colleagues): thread the inbound contextId onto colleague asks so the loop guard sees the chain"
```

---

### Task 7: End-to-end context-id continuity check

**Files:**
- Test: `api/tests/test_clients.py` (kobo side)

**Interfaces:**
- Consumes: Task 3's `context_id` kwarg.

**Why its own task:** the two halves live in different repos and can drift silently. If either end stops threading the id, the loop guard reverts to inert and nothing fails. This pins the kobo half of the contract explicitly and documents the daedalus half.

- [ ] **Step 1: Write the test**

```python
def test_relay_envelope_context_id_is_the_key_the_switchboard_reads():
    """Contract pin. The switchboard's guard reads exactly "contextId".
    daedalus copies this key onto colleague hops (tools/colleagues.py).
    If this key is ever renamed, the loop guard silently stops working
    and no other test notices."""
    env = _build_task_envelope([{"role": "user", "text": "hi"}], context_id="ctx-1")
    assert "contextId" in env, "the guard reads contextId; any other spelling is inert"
    assert env["contextId"] == "ctx-1"
```

- [ ] **Step 2: Run, confirm PASS, commit**

```bash
cd ~/Documents/GitHub/kobo/api && venv/bin/python -m pytest -q tests/test_clients.py -k context_id
cd ~/Documents/GitHub/kobo
git add api/tests/test_clients.py
git commit -m "test(chat): pin the contextId key the switchboard loop guard reads"
```

---

### Task 8: 502 instrumentation and live capture

**Files:**
- Modify: `api/kobo_api/main.py` (the chat relay `except` block near line 2676)

**Interfaces:**
- Consumes: nothing.
- Produces: no API change. Richer server-side logging only.

**Status:** the 502's root cause is NOT established. This task gathers evidence; it does not implement a fix for an unproven cause. Ruled out already: the room-side timeout (`CabalClient.relay` deliberately uses 180s) and agent instability (gemini-one, 39h uptime, zero restarts).

- [ ] **Step 1: Enrich the failure log**

The existing `except` already logs. Extend it so a captured failure identifies itself: include the agent name (already there), the exception TYPE, and the elapsed wall time of the relay call. Wrap the `clients.cabal.relay(...)` call with a monotonic start/end and log the elapsed seconds on failure. Do not log the message content.

- [ ] **Step 2: Test the logging path**

Add a test asserting a relay that raises produces a log record containing the elapsed time and the exception type, using the file's existing `caplog` idiom. Run it RED first (the elapsed value will be absent), then implement, then GREEN.

- [ ] **Step 3: Full suite, vendor scan, commit**

```bash
git add api/kobo_api/main.py api/tests/
git commit -m "chore(chat): log relay failure type and elapsed time to characterize the 502s"
```

- [ ] **Step 4: Capture (operator, after rollout)**

Reproduce a 502 in the room, then collect: the kobo log line (type + elapsed), the cabal-side log for the same window, and whether the agent was mid colleague-hop. Write the findings into the ledger. Decide there, on evidence, whether Tasks 3 and 6 removed it or whether a separate arc is warranted. Do not guess a fix into this plan.

---

### Task 9: Docs, spec status, ledger

**Files:**
- Modify: `docs/superpowers/specs/2026-08-26-cleanup-and-colleague-correctness-design.md` (Status line, execution amendments)
- Modify: `.superpowers/sdd/progress.md` (untracked; no vendor scan needed)

- [ ] **Step 1: Flip status and record amendments**

Set Status to `Implemented (rollout pending)`. Append an `## Execution amendments` section recording any deviation discovered during execution, in particular: which id Task 3 used for `context_id`, and which visibility rule Task 5 used for the roster.

- [ ] **Step 2: Ledger and commit**

Append the execution record to `.superpowers/sdd/progress.md`. Then:

```bash
cd ~/Documents/GitHub/kobo/api && venv/bin/python -m pytest -q
cd ~/Documents/GitHub/kobo
git add docs/superpowers/specs/2026-08-26-cleanup-and-colleague-correctness-design.md
git diff --cached --name-only | xargs grep -l -f .superpowers/sdd/vendor-names.txt || git commit -m "docs: cleanup + colleague correctness spec status and amendments"
```

---

## Rollout (operator lane, after all tasks)

1. Build and roll the kobo image at wave HEAD; digest-verify. No DB migration.
2. Daedalus ships on its branch-pinned ref per locked decision #11. The runtime pin must move for Task 6 to reach agents; note that the next pin should also pick up daedalus#17's truncation marker, which is merged and still unpinned.
3. Agents pick up Tasks 4 and 5 on their next delivery. Existing live agents need a delivery to gain the self-card and roster.
4. Acceptance, straight from issue #5: ask gemini-one which project it runs in. Correct behavior is a direct answer or an honest "I don't know yet", with NO colleague hop. Then ask something squarely outside its field and confirm it names a relevant colleague rather than the first name it recalls.
5. Then run Task 8 Step 4's capture.

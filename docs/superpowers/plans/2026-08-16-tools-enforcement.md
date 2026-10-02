# Tools Enforcement Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Files in `bundle/tools/` become enforced law: pre_task and post_draft hooks run unskippably around every reply, fail closed, attested by the runtime's agent card, with kobo's Tooling panel graduating from "stored" to "enforced" only on live attestation.

**Architecture:** A new `daedalus/enforcement.py` loads hook modules at Agent construction and wraps the single task choke point (`Agent.handle_task`); both transports inherit the law because neither owns a loop. Kobo upgrades its one tooling validator (stdlib-only imports, real entrypoint check) and probes the agent card to attest enforcement before the UI claims it. Spec: `docs/superpowers/specs/2026-08-16-tools-enforcement-contract-design.md`.

**Tech Stack:** Python 3.12 stdlib (importlib, ast, asyncio.to_thread) in daedalus; FastAPI + Svelte 5 in kobo.

## Global Constraints

- Vendor rule: NO client names in tracked files; scan every touched file with `grep -i -f .superpowers/sdd/vendor-names.txt <files>` (kobo repo root) before staging. Plain grep, not git grep.
- **NO EM DASHES in any new user-facing copy** (operator rule, 2026-08-16). Use periods or commas. Code comments and docs are exempt; UI strings and reply text are not.
- Validate generated/loaded values against the REAL downstream gate, never a lookalike bound (standing house lesson).
- Stage named files only. TDD RED-first with evidence. Report suite counts.
- daedalus: branch `feat/enforcement` FROM `feat/intake-seeding` (b330b9a-tip); NO push, NO PR until the operator says. Suite: `cd ../daedalus && python -m pytest -q` (runtime tests) and `python -m pytest docker/test_seeder.py -q` (28) must both stay green.
- kobo API suite: `cd api && venv/bin/python -m pytest -q` (baseline 1131). kobo UI: `PATH="~/google-cloud-sdk/bin:/opt/homebrew/bin:/usr/local/bin:$PATH" make -C ui check` (svelte-check 0 errors, vitest baseline 241).
- Hook contract shapes are FROZEN by the spec: `pre_task(message: str) -> dict` (key "notices": list[str], optional), `post_draft(draft: str) -> str`. Any raise or a 2.0s timeout blocks the task. Composition strictly by sorted filename.

---

### Task 1: daedalus enforcement module + TOOL_BLOCKED

**Files:**
- Create: `src/daedalus/enforcement.py`
- Modify: `src/daedalus/task.py` (ReasonCode enum)
- Test: `tests/test_enforcement.py`

**Interfaces:**
- Consumes: nothing new (stdlib only).
- Produces (Task 2 relies on these exactly):
  - `ReasonCode.TOOL_BLOCKED = 703` in task.py's enum, right after `POLICY_VIOLATION = 701`.
  - `class Enforcement` in enforcement.py:
    - `Enforcement.load(tools_dir: Path) -> Enforcement` classmethod. Missing/empty dir → `enforcement.active is False`. Import failure → `active is True`, `load_error == (filename, one_line_message)`, `hooks == []`.
    - Properties: `active: bool`, `locked: bool` (True iff load_error), `load_error: tuple[str, str] | None`, `hook_files: list[str]` (sorted basenames that loaded).
    - `async run_pre_task(message: str) -> PreTaskResult` where `PreTaskResult = namedtuple("PreTaskResult", "blocked notices detail")`; blocked True on any hook exception/timeout, detail = `f"blocked by {filename}"` plus a plain reason.
    - `async run_post_draft(draft: str) -> PostDraftResult` (`"blocked text detail"` fields, same semantics; text = chained output when not blocked).
    - `card_stamp() -> dict`: `{"version": 1, "hooks": self.hook_files, "locked": self.locked}` (+ `"locked_file": name` when locked).
  - `HOOK_TIMEOUT_S = 2.0` module constant.

- [ ] **Step 1: Failing tests.** In `tests/test_enforcement.py`, stdlib+pytest only, using `tmp_path` to write real hook files (this exercises the REAL importlib path, per the house real-gate lesson). Cover:

```python
def _write_tool(tmp_path, name, body):
    d = tmp_path / "tools"; d.mkdir(exist_ok=True)
    (d / name).write_text(body)
    return d

async def test_missing_dir_inactive(tmp_path): ...        # active False, run_* pass-through
async def test_pre_task_notices_concat_in_filename_order(tmp_path): ...
async def test_post_draft_chains_in_filename_order(tmp_path): ...
    # a_upper.py upcases, b_suffix.py appends; assert order a-then-b by output shape
async def test_hook_exception_blocks_with_filename_detail(tmp_path): ...
async def test_hook_timeout_blocks(tmp_path): ...
    # hook body: time.sleep(3) with HOOK_TIMEOUT_S monkeypatched to 0.2 for test speed
async def test_import_error_locks_not_raises(tmp_path): ...
    # file with `import definitely_not_a_module`; load() returns locked Enforcement
async def test_module_missing_both_hooks_is_load_shape_error(tmp_path): ...
    # a file with neither pre_task nor post_draft locks with a plain message
    # (upstream validator prevents this, but the runtime must not trust upstream)
async def test_card_stamp_shapes(tmp_path): ...            # normal + locked shapes
def test_reason_code_tool_blocked_is_703(): ...
```

- [ ] **Step 2: RED.** `python -m pytest tests/test_enforcement.py -q` → collection/attribute failures.

- [ ] **Step 3: Implement.** `task.py`: add `TOOL_BLOCKED = 703` under `POLICY_VIOLATION = 701`. `enforcement.py` core (complete this skeleton without changing signatures):

```python
"""Enforcement hooks: the law layer around every reply.

Loads bundle/tools/*.py (kobo's staged Tooling door) and runs their
pre_task/post_draft hooks around the agent loop. Distinct from
daedalus.tools (LLM function-calling providers). Fail-closed everywhere:
an exception, a timeout, or a load failure never lets an unlawful reply
through."""
import asyncio, importlib.util
from collections import namedtuple
from pathlib import Path

HOOK_TIMEOUT_S = 2.0
PreTaskResult = namedtuple("PreTaskResult", "blocked notices detail")
PostDraftResult = namedtuple("PostDraftResult", "blocked text detail")

class Enforcement:
    def __init__(self, modules, load_error=None):
        self._modules = modules          # list[(filename, module)]
        self.load_error = load_error
        self.active = bool(modules) or load_error is not None

    @classmethod
    def load(cls, tools_dir: Path) -> "Enforcement":
        if not tools_dir.is_dir():
            return cls([])
        modules = []
        for path in sorted(tools_dir.glob("*.py")):
            try:
                spec = importlib.util.spec_from_file_location(f"kobo_tool_{path.stem}", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if not callable(getattr(mod, "pre_task", None)) and not callable(getattr(mod, "post_draft", None)):
                    return cls([], load_error=(path.name, "defines neither pre_task nor post_draft"))
                modules.append((path.name, mod))
            except Exception as exc:  # noqa: BLE001  law never half-loads
                return cls([], load_error=(path.name, str(exc).splitlines()[0][:200]))
        return cls(modules)
```

`run_pre_task`/`run_post_draft`: iterate `self._modules` in order, `await asyncio.wait_for(asyncio.to_thread(hook, arg), HOOK_TIMEOUT_S)` per hook, skip modules lacking that hook, collect notices (`result.get("notices", [])` when the hook returns a dict, anything non-dict = block with "returned an unexpected shape") / chain draft text (non-str return = block); wrap `Exception`/`TimeoutError` into the blocked result with the filename. `card_stamp()` per the interface. `locked` property = `self.load_error is not None`.

- [ ] **Step 4: GREEN + both daedalus suites.** `python -m pytest -q` and `python -m pytest docker/test_seeder.py -q` (28).

- [ ] **Step 5: Vendor scan + commit** (branch `feat/enforcement` created first: `git switch -c feat/enforcement`):
```bash
grep -i -f .superpowers/sdd/vendor-names.txt src/daedalus/enforcement.py src/daedalus/task.py tests/test_enforcement.py
git add src/daedalus/enforcement.py src/daedalus/task.py tests/test_enforcement.py
git commit -m "feat(enforcement): the law layer loads, composes, and fails closed"
```

---

### Task 2: wire enforcement into the agent loop + card

**Files:**
- Modify: `src/daedalus/agent.py` (handle_task at :230, _messages, A2aTransport construction at :427-437, Agent.__init__)
- Test: `tests/test_agent_enforcement.py` (new), `tests/test_transport_a2a.py` + `tests/test_transport_slack.py` (additive cases)

**Interfaces:**
- Consumes: Task 1's `Enforcement` exactly.
- Produces: `Agent` gains `self.enforcement = Enforcement.load(bundle.path / "tools")` (find the Bundle's root-path attribute in bundle.py and use it; if Bundle does not retain its path, add `self.path = path` in `Bundle.load` as part of this task). Behavior contract for tests and the acceptance run:
  1. `locked` → every task returns a failed Task, `ReasonCode.TOOL_BLOCKED`, reply text exactly: `"My orchestrator file {name} failed to load. Fix or remove it in the room."`
  2. pre_task blocked → failed Task, TOOL_BLOCKED, reply `"A rule stopped this task: {detail}."`
  3. notices → injected into the model's context via the same mechanism `handle_task` uses for recalled memories (read agent.py:230-235 and `_messages`; notices append AFTER recalled memories, each as its own entry, before the user turn).
  4. post_draft transforms the final agent text on every completion path that carries text; blocked at post stage → failed Task, TOOL_BLOCKED, reply `"A rule stopped this reply: {detail}."` (the draft is discarded, never partially sent).
  5. Card served by a2a includes `"enforcement": enforcement.card_stamp()` when `enforcement.active`, injected at the `A2aTransport(card=...)` construction site; the on-disk card file is not modified.

- [ ] **Step 1: Failing tests.** `tests/test_agent_enforcement.py` uses `tests/fakes.py`'s `ScriptedLLM` + a real Bundle from a `tmp_path` mini-bundle (mirror `tests/golden/greeter/` shape plus a `tools/` dir). Cases: notices reach the LLM's seen messages (assert via `ScriptedLLM.messages`) after recalled entries; post_draft transform applied to the reply text; pre-blocked and post-blocked produce TOOL_BLOCKED failed Tasks with the EXACT reply strings above; locked agent short-circuits without calling the LLM (assert `ScriptedLLM` saw zero calls); no tools dir → behavior byte-identical to pre-enforcement (pin with a no-tools bundle). Transport additions: a2a peer_dispatch and slack handle_event each get one blocked-task case asserting the existing failed-Task surfacing (Slack ❌ + text; a2a failed state, not a 500). Card: `TestClient` on `build_a2a_app` asserts the enforcement stamp appears when active and is absent when not.

- [ ] **Step 2: RED.** Targeted run.

- [ ] **Step 3: Implement.** In `handle_task` (keep the function the single choke point):

```python
async def handle_task(self, task: Task) -> Task:
    if self.enforcement.locked:
        name, _ = self.enforcement.load_error
        return task.reply(
            f"My orchestrator file {name} failed to load. Fix or remove it in the room.",
            state=TaskState.FAILED, reason=ReasonCode.TOOL_BLOCKED)
    pre = await self.enforcement.run_pre_task(task.user_text())
    if pre.blocked:
        return task.reply(f"A rule stopped this task: {pre.detail}.",
                          state=TaskState.FAILED, reason=ReasonCode.TOOL_BLOCKED)
    task.state = TaskState.WORKING
    recalled = await self.memory.recall(task.user_text())
    if recalled:
        self._emit("recall", count=len(recalled))
    result = await self._loop(task, self._messages(task, recalled, notices=pre.notices))
    if result.agent_text():
        post = await self.enforcement.run_post_draft(result.agent_text())
        if post.blocked:
            return task.reply(f"A rule stopped this reply: {post.detail}.",
                              state=TaskState.FAILED, reason=ReasonCode.TOOL_BLOCKED)
        result.set_agent_text(post.text)   # use the Task API's actual mutator; read task.py
    return result
```

Match `task.reply`'s real signature (read agent.py's existing FAILED/ESCALATION usage at :249-257 and mirror it exactly); `_messages` gains an optional `notices` parameter appended after recalled content. If `Task` has no text mutator, replace the final agent message through whatever structure `agent_text()` reads, in the same task; add the smallest method to task.py if genuinely absent and test it.

- [ ] **Step 4: GREEN + full daedalus suites.**

- [ ] **Step 5: Vendor scan + commit:** `git add src/daedalus/agent.py src/daedalus/bundle.py tests/test_agent_enforcement.py tests/test_transport_a2a.py tests/test_transport_slack.py` (bundle.py only if the path attribute was needed) — `git commit -m "feat(agent): every reply passes through the law"`.

---

### Task 3: kobo validator upgrade (stdlib-only + real entrypoints)

**Files:**
- Modify: `api/kobo_api/tooling.py`
- Test: `api/tests/test_tooling.py` (additive), `api/tests/test_web_tooling.py` (one route case per new 422)

**Interfaces:**
- Consumes: existing `_validate_content`/upload/import validator seam in tooling.py.
- Produces: `.py` validation extended (both doors, one validator):
  - Import allowlist: every `import X` / `from X import ...` where `X.split('.')[0]` not in `sys.stdlib_module_names` → `ToolingError("tooling file imports beyond the standard library on line {n}: {module}")` → 422.
  - Entrypoint check: at least one module-level `def pre_task(` or `def post_draft(` (ast.FunctionDef scan, top level only) → else `ToolingError("tooling file defines neither pre_task nor post_draft")` → 422.
  - `.go` files: unchanged (deferred per spec).

- [ ] **Step 1: Failing tests:** httpx-import rejected with the line number; `from requests import get` rejected; `import ast` and `from collections import OrderedDict` accepted; nested function defs do NOT satisfy the entrypoint check; a file with only `post_draft` passes; the reference orchestrator shape (module constants + class + both hooks) passes. Route: one 422 body per new message through the existing prefix mapping.
- [ ] **Step 2: RED → implement inside the existing `.py` validation branch (single `ast.parse` reused for syntax + imports + entrypoints) → GREEN + full API suite** (1131 baseline + new).
- [ ] **Step 3: Vendor scan + commit:** `kobo: tooling validator learns the law's grammar, stdlib only, real entrypoints`.

---

### Task 4: kobo card probe + banner graduation

**Files:**
- Modify: `api/kobo_api/clients.py` (KubectlAgentClient + AgentClient Protocol + fakes consumer), `api/kobo_api/tooling.py` (list_tooling), `api/kobo_api/main.py` (tooling list route response), `api/tests/fakes.py`, `ui/src/lib/api.ts`, `ui/src/lib/queries.ts` (only if types move), `ui/src/components/ToolingPanel.svelte`
- Test: `api/tests/test_clients.py`, `api/tests/test_tooling.py`, `api/tests/test_web_tooling.py`, `ui/src/lib/queries.test.ts`

**Interfaces:**
- Consumes: Task 2's card stamp shape `{"version": int, "hooks": [...], "locked": bool, "locked_file"?: str}`.
- Produces:
  - `AgentClient` Protocol + `KubectlAgentClient` gain `fetch_card(self, namespace: str, service: str) -> dict` (same port-forward machinery as converse, GET `/.well-known/agent-card.json`; ClientError on failure). `FakeAgent` gains a settable card + raisable knob.
  - `list_tooling(...)` returns `(files, enforcement)` where enforcement is `{"attested": bool, "locked": bool, "locked_file": str | None}` derived from `fetch_card` (`attested` = card fetch succeeded AND `card.get("enforcement", {}).get("version", 0) >= 1`; fetch failure → `attested False, locked False` and files keep today's statuses; NEVER guessed).
  - Route response becomes `{"files": [...], "enforcement": {...}}`; each file dict gains `"status": "enforced"` when attested-and-not-locked and delivered, else the existing `delivered`-derived truth.
  - UI: banner copy when attested and not locked, VERBATIM (no em dash, per the standing rule): `"Enforced by the runtime. Every reply passes through these."`; locked state renders, VERBATIM: `"{locked_file} failed to load. The agent is answering with a refusal until it is fixed or removed."`; unattested keeps today's banner. Per-row badge shows `enforced` (accent tone) only under the same conditions.

- [ ] **Step 1: RED tests** (client argv/path pin for fetch_card; list_tooling matrix: attested/locked/unreachable × delivered/pending; route shape; UI factory pins for the extended response type + banner-state helper if extracted).
- [ ] **Step 2: Implement API side → GREEN full API suite.**
- [ ] **Step 3: Implement UI side → gate green** (svelte-check 0, vitest 241 + new).
- [ ] **Step 4: Vendor scan + one commit per repo-side** (`kobo: the banner tells the truth twice, now including when law is live` for api+ui together is fine if staged precisely).

---

### Task 5: rollout + live acceptance (controller-led, no subagent)

- [ ] Build runtime image from `feat/enforcement` tip: `cd daedalus && make image IMAGE=<runtime image path from the org config, tag stripped> TAG=<sha>` then push the pinned tag only. (The registry path is client-specific and lives in the client org's config repo, never in this plan.)
- [ ] Bump org config `runtime_image` to the new tag (gh api contents flow, ftv identity).
- [ ] Build + roll kobo at the Task 4 commit.
- [ ] Deliver to the reference agent: re-upload its orchestrator file through the Tooling panel (same content re-upload no-ops the file but the re-pin rides; if commit_push no-ops entirely, touch the file trivially or upload a second copy) so the Dockerfile FROM moves to the new runtime; wait for rotation.
- [ ] Verify: card shows the enforcement stamp (in-pod fetch); Tooling panel shows the enforced banner; then the acceptance probes over a2a:
  1. any question → reply ends `VERIFIED: checked twice.` (mechanical)
  2. a "probably"-eliciting prompt → the word never appears (mechanical)
  3. the deploy request → reply carries the ticket requirement (model-mediated, asserted on substance)
- [ ] Negative controls: an agent with no tools dir behaves unchanged; upload a deliberately hook-less .py → 422 from the new validator (never reaches the bundle).
- [ ] Results entry in the ledger + runbook; suites reported.

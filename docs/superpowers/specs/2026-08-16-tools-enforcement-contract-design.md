# Tools enforcement contract — law arrives at the runtime (design)

Approved in brainstorm 2026-08-16. This is the second half of the staged
Tooling door (2026-08-13 addendum): files in `bundle/tools/` stop being
inert. The staged door's live "before" transcript — a reference agent
(warden) ignoring its own orchestrator 0-for-3 — is this design's
acceptance test, to be re-run 3-for-3.

## Why this is unskippable, structurally

Both transports (a2a and Slack) execute every task through one function:
`Agent.handle_task` (daedalus `src/daedalus/agent.py:230`), whose `_loop`
holds the only model boundary (`self.llm.complete`, agent.py:276).
Enforcement wraps that choke point — `pre_task` before the model sees the
message, `post_draft` on the drafted text before the Task completes. A
transport cannot route around it because no transport owns a loop of its
own.

## Naming

Daedalus already has `daedalus/tools/` — LLM function-calling providers
(builtin/mcp/limenal). The enforcement layer is deliberately elsewhere:
a new `daedalus/enforcement.py`. It loads the bundle directory named
`tools/` — that name is kobo's shipped contract and does not change. In
daedalus prose and code, these are "enforcement hooks", never "tools".

## Activation: convention over config

Presence of `bundle/tools/*.py` activates enforcement — same convention as
`knowledge/` and `intake/`. No agent.yaml field, no schema change, nothing
for a generator to forget.

## The hook contract (module shape)

A tool file is a Python module exposing either or both, module-level:

```python
def pre_task(message: str) -> dict:
    """Runs before the model sees the user's message.
    Returns {"notices": [str, ...]} (missing key = no notices).
    Raise anything to block the task."""

def post_draft(draft: str) -> str:
    """Runs on the drafted reply before it is sent. Returns the text to
    send. Raise anything to block the task."""
```

- Hooks are synchronous pure functions: text in, text/dict out. v1 files
  are stdlib-only (validator-enforced upstream); no network, no
  filesystem powers are part of this contract.
- Multiple files compose in **filename order**: all `pre_task` notices
  concatenate (in order); `post_draft` chains, each receiving the
  previous output.
- Notices are injected into the model's context through the same path
  recalled memories already ride in `handle_task` — the model receives
  them before drafting; the contract does not promise verbatim echo,
  which is precisely why `post_draft` exists for mechanical guarantees.

## Execution semantics (all fail-closed)

- Each hook invocation runs via `asyncio.to_thread` with a **2-second
  budget**. Timeout, or ANY exception, blocks the task.
- A blocked task returns a failed Task with the new
  `ReasonCode.TOOL_BLOCKED = 703` (alongside `POLICY_VIOLATION = 701`,
  task.py) and a plain reply naming the blocking file — riding the
  existing failed-Task conventions, so Slack shows its ❌-plus-text and
  a2a callers get the standard failed state with no new transport code
  paths.
- `post_draft` applies on every completion path that carries agent text;
  a task that completes without text (pure tool-call escalations) is
  untouched.

## Loading and serve-but-locked

`daedalus/enforcement.py` loads each `bundle/tools/*.py` at Agent
construction via importlib, filename order. A file that fails to import
does NOT crash the boot: the enforcement layer records
`load_error = (filename, message)` and `handle_task` short-circuits every
task into the TOOL_BLOCKED refusal: "my orchestrator file
`<name>` failed to load — fix or remove it in the room." Nothing
unlawful ever executes; the agent always answers; recovery is a Tooling
panel action, not kubectl. (Load errors are logged once at boot with the
real traceback; replies carry only the plain sentence.)

## Attestation: the agent card

At `A2aTransport(card=...)` construction (agent.py:427-437) the runtime
injects into the served card:

```json
"enforcement": {"version": 1, "hooks": ["<file>", ...], "locked": false}
```

(`locked: true` + the failing filename when in serve-but-locked.) The
on-disk card file is untouched — this reflects runtime state.

## Kobo side

- **Validator upgrade** (the staged door's single validator): ast-level
  stdlib-only import check (reject with the offending line, plain 422)
  and a real entrypoint check — at least one of `pre_task`/`post_draft`
  defined at module level. Applies to upload and repo import
  identically.
- **Banner graduation**: `list_tooling` gains a card probe (the agent
  client's existing port-forward machinery fetching
  `/.well-known/agent-card.json`). Per-file status becomes `enforced`
  only when the live card attests version ≥ 1, not locked, and the file
  is delivered. The panel's standing banner swaps to "Enforced by the
  runtime — every reply passes through these." only under the same
  attestation; a locked agent surfaces the load failure in the panel.
  Unreachable card (pod down/rolling) degrades to today's honest
  `stored`/delivered states — never a guessed `enforced`.

## Rollout

Daedalus branch on top of the intake branch; runtime image build + org
`runtime_image` bump. Existing agents pick it up on their next delivery
(any tooling/intake upload re-pins — the 2026-08-13 mechanism). No
migration: agents without `bundle/tools/` see zero behavior change.

## Testing

- daedalus: enforcement module unit tests (composition order, notice
  concat, chain semantics, timeout, exception→TOOL_BLOCKED, load-error
  lock, card injection incl. locked shape) via the existing
  `ScriptedLLM`/EchoHandler idioms; transport tests pin ❌ +
  refusal-text surfacing on both transports.
- kobo: validator matrix additions (non-stdlib import line-numbered 422,
  no-entrypoint 422); card-probe states (attested/locked/unreachable)
  through FakeAgent; banner logic factories.
- Live acceptance (the whole point): the reference agent's orchestrator
  transcript re-run — every reply ends with its verification line
  (post_draft: mechanical), the banned word never survives (post_draft:
  mechanical), and the deploy request's reply carries the ticket
  requirement (pre_task notice is context-injection, so this criterion is
  model-mediated by design — asserted on substance, not verbatim echo).
  0-for-3 becomes 3-for-3 on production.

## Non-goals (v1)

- I/O powers, network access, or package imports for hooks (explicit
  later grant, own design).
- Team-level shared tools (waits for Phase B identity→team).
- Hook access to conversation history, memories, or task metadata
  (message text and draft text only).
- Subprocess/sandbox isolation of hook execution (stdlib-only validator
  + same-trust-domain operator upload is the v1 boundary; sandboxing is
  the v2 hardening).
- Slack-transport-specific hook variants.

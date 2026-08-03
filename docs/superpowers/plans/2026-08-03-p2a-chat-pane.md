# P2a — Chat Pane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Talk to any READY agent from its detail page — a chat panel over the a2a wire, with the transcript owned by the browser and the server stateless.

**Architecture:** Three thin layers over proven seams. The API's `AgentClient` (which already does port-forward + bare-task POST for HelloStep) generalizes to `converse(namespace, service, messages) -> str`, sending the FULL message history in each a2a task envelope (the daedalus bare-task shape accepts `history[]`; server-side statelessness means no session store). A new `POST /api/agents/{name}/chat` endpoint guards on READY status and proxies. The UI adds a `ChatPanel` on the agent detail page: client-state transcript, optimistic user bubbles, pending indicator, error surface via the shared `errorMessage`. **Deliberate rescope:** the SSE reasoning-trace console from the spec's P2 line stays deferred (kaminos-console territory); this plan ships request/reply chat. **Fold-in:** model *labels* (not raw ids) on agent list/detail — the ledgered P1b seam — since the detail header is being touched anyway.

**Tech Stack:** unchanged (FastAPI/pytest; Svelte 5/vitest).

## Global Constraints

- Never the employer's name or work-app name in any tracked file; zero infra vocabulary in UI copy.
- TDD; `make -C api check` and `make -C ui check` fully green before every commit; **git add only named files**.
- The a2a envelope is verified fact (P0 runbook): `POST /a2a/tasks` with `{"kind":"task","id":<str>,"history":[{"kind":"message","role":"user"|"agent","parts":[{"kind":"text","text":...}]}]}`; reply text at `status.message.parts[0].text`.
- Per-request port-forward latency (~2-4s overhead) is accepted for dev; do not build connection pooling (YAGNI; note as a later optimization only).
- Chat input limits: 1..4000 chars server-side (422 outside), transcript capped client-side at 50 turns (oldest dropped with a quiet notice).

---

### Task 1: API — `AgentClient.converse` (generalize the hello machinery)

**Files:** `api/cortex_api/clients.py`, `api/tests/fakes.py`, `api/tests/test_clients.py`

**Interfaces:**
```python
class ChatMessage(TypedDict):  # or a small dataclass — pick one, use it everywhere
    role: str   # "user" | "agent"
    text: str

class AgentClient(Protocol):
    def hello(self, namespace: str, service: str, text: str) -> str: ...   # unchanged
    def converse(self, namespace: str, service: str, messages: list[ChatMessage]) -> str: ...
```
- Real impl: refactor the existing port-forward + POST machinery so `hello` becomes `converse(ns, svc, [{"role":"user","text":text}])` — one shared private `_send_task(ns, svc, history_payload)` doing forward/POST/extract/cleanup (the timeout-proof reader-thread wait and 120s httpx timeout are already there — reuse, don't duplicate). Envelope: task id `f"chat-{uuid4().hex}"`, `history` mapping each ChatMessage to the verified message shape.
- `FakeAgent`: `converse` records the full messages list per call and returns a canned or scripted reply (knob: `replies: list[str]` consumed in order; `hello_error`-style `converse_error` knob).
- Tests: envelope shape with a MULTI-TURN history (two user + one agent message → three history entries, roles/parts exact); `hello` still passes its existing tests via the shared path; ClientError wrapping unchanged (one test re-exercised through converse).

- [ ] TDD steps per house pattern; commit: `feat(api): AgentClient.converse — multi-turn a2a chat over the hello machinery`

### Task 2: API — chat endpoint + model labels fold-in

**Files:** `api/cortex_api/main.py`, `api/tests/test_api_agents.py`

**Interfaces:**
- `POST /api/agents/{name}/chat` body `{"messages": [{"role","text"}...]}` (the full transcript, last entry must be role "user") → `200 {"reply": str}`. Guards: 404 unknown agent; 409 unless status READY (`{"error": "agent is not ready to chat"}`); 422 on empty messages, last-not-user, any text empty or >4000 chars, unknown role. Errors from converse → 502 `{"error": "agent unreachable: ..."}` (ClientError message included).
- Labels fold-in: `GET /api/agents` items and `GET /api/agents/{name}` gain `"model_label"` — resolved from `config.models` by id, falling back to the raw id when the id isn't in the catalog (agents created under an older config must not 500).
- Tests: happy multi-turn chat through FakeAgent (messages recorded match what was POSTed); each guard (404/409/422 variants); 502 on converse_error; label present in list+detail, fallback case covered.

- [ ] TDD; commit: `feat(api): chat endpoint + model labels on agent views`

### Task 3: UI — client + query layer additions

**Files:** `ui/src/lib/api.ts`, `ui/src/lib/queries.ts`, their tests

**Interfaces:**
```ts
export type ChatMessage = { role: "user" | "agent"; text: string }
export async function chatWithAgent(name: string, messages: ChatMessage[]): Promise<{reply: string}>
// queries.ts: chatMutation(name) — NO cache invalidation (chat doesn't change agent state);
// AgentSummary/AgentDetail gain model_label
```
- Tests: request body shape; 409/502 error messages surface via ApiError; types compile (svelte-check).

- [ ] TDD; commit: `feat(ui): chat client + model labels in types`

### Task 4: UI — ChatPanel

**Files:** `ui/src/components/ChatPanel.svelte`, `ui/src/pages/AgentDetail.svelte` (mount + model_label swap)

**Behavior:**
- Rendered on the detail page ONLY when `status === "ready"`, below First words. Heading "Chat".
- Transcript: local `$state<ChatMessage[]>` seeded empty (First words stays its own card — don't duplicate it into the transcript). User sends → optimistic user bubble + pending indicator ("thinking…" with the pulse treatment) → on reply append agent bubble → input refocused. On error: error line in-panel via shared `errorMessage`, user message stays in transcript with a retry-send affordance, pending cleared.
- Send disabled while pending or input empty/whitespace; Enter sends, Shift+Enter newline (textarea). 50-turn client cap with quiet oldest-dropped notice.
- A11y: transcript container `role="log"` `aria-live="polite"` (log role announces additions politely by default — the correct semantic for chat); bubbles carry visually-hidden role prefixes ("You:", "<agent name>:"); input labeled.
- Model label: the detail header (and AgentCard if trivially reachable — else leave, card fold is optional) shows `model_label`.
- Zero infra vocabulary; the panel never claims the agent "learns" from chat (the runtime doesn't wire that yet) — copy is just "Chat".

- [ ] Implement (svelte-check gate; no new pure logic beyond what belongs in Task 3); commit: `feat(ui): chat panel — talk to a ready agent`

### Task 5: Live milestone

- [ ] Stack up (API :8100, UI :5173; agents clio/echo READY). Playwright: open echo's detail, send "what do you remember about how you got here?" — assert an agent bubble arrives containing a non-empty reply; screenshot the exchange; send a second turn referencing the first ("say that again in five words") — assert the reply demonstrates the history actually carried (manual judgment on content, recorded verbatim). Repeat one exchange on clio. Error path: chat with an agent mid-provision is impossible by construction (panel hidden) — instead verify the 409 by curl against a non-READY... if none exists, skip with a note (the unit test covers it).
- [ ] Append `## P2a results` to the runbook with the transcripts; update README Status line ("…and you can chat with deployed agents from the room"); commit: `P2a results: chat pane live`

---

## Self-review notes (applied)

- The envelope/extraction shapes come from the P0-verified contract, reused byte-for-byte; the only new wire behavior is multi-message history, unit-pinned in Task 1.
- Statelessness decision documented (browser owns transcript; server proxies) — no DB schema change anywhere.
- Deferred explicitly: SSE trace console; chat persistence; "training"/memory-extraction claims; connection pooling; AgentCard label swap if it grows beyond trivial.
- P2 remaining after this: knowledge editing (P2b), Slack flow (P2c — needs the workspace app-config token), avatars (P2d — needs the image endpoint), Archive/Library extraction (unscheduled).

# P1b — cortex-ui Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The assembly room's face: a Svelte 5 SPA where Create is a single modal (name · persona · skills · model), progress is a plain-English checklist that polls adaptively, and a finished agent shows its first words — entirely against the live, tested cortex-api.

**Architecture:** `ui/` beside `api/` (the family's kaminos layout). Plain SPA — Vite + Svelte 5 runes + TanStack Query v5 (`@tanstack/svelte-query`), a hand-rolled two-route router (dashboard / agent detail), no SSR, no Tailwind (plain CSS with a token sheet). All server state flows through one typed `api.ts` (fetch wrapper with a non-JSON parse guard) and one `queries.ts` (adaptive `refetchInterval`: 3.5s while any agent/step is non-terminal, off when terminal). The UX contract comes from the spec §3 verbatim: fire-and-return create with toast, pending styled like running (single pulse treatment), zero infra vocabulary anywhere, shared EmptyState/ErrorState-with-Retry/skeleton components. **Scope note:** P1b consumes only endpoints that exist (create/list/detail/retry/catalogs/availability). Composition *editing* panels need API endpoints that don't exist yet and belong to P2 alongside knowledge editing — the spec's P1 exit criterion ("create → working agent entirely from the UI") does not require them.

**Tech Stack:** Svelte 5 (runes), Vite 6, TypeScript, @tanstack/svelte-query v5, vitest (pure-logic tests), svelte-check.

## Global Constraints

- Never write the employer's name or the internal work-app name into any file. UI copy uses no infra vocabulary (no "WIF", "kustomize", "PVC", "namespace" — step labels come from the API and are already plain-English).
- TDD where there is logic: `lib/*.ts` modules get vitest tests written first. Svelte components are covered by `svelte-check` + the Task 7 live smoke (component-level test harness is deliberately out of scope, matching the family precedent).
- `make -C ui check` (created Task 1: `svelte-check` + `vitest run`) green before every commit; **`git add` only named files** — never a repo-level broad add.
- The dev server proxies `/api` → `http://127.0.0.1:8100` (vite proxy) so the UI is origin-clean in dev.
- Visual language: calm, roomy, technical-but-warm; design tokens defined once in `src/app.css` (spacing scale, one accent color, neutral grays, `--pulse` animation). Status colors: done=green, failed=red, running & pending=amber pulse (spec: pending must read as "working", never "stalled").

---

### Task 1: Scaffold — Vite + Svelte 5, typed API client, tokens

**Files:**
- Create: `ui/package.json`, `ui/vite.config.ts`, `ui/tsconfig.json`, `ui/svelte.config.js`, `ui/index.html`, `ui/src/main.ts`, `ui/src/app.css`, `ui/src/App.svelte` (placeholder shell), `ui/src/lib/api.ts`, `ui/Makefile` → no: extend root-level pattern — Create `ui/Makefile` with `install`, `dev`, `build`, `check` (svelte-check + vitest run), mirroring `api/Makefile`'s style
- Test: `ui/src/lib/api.test.ts`
- Modify: `.gitignore` (append `ui/node_modules/` — `node_modules/`/`dist/` already covered; verify, add only if missing)

**Interfaces (every later task consumes these):**
- `api.ts` exports typed functions over `fetch`, all going through one `parse()` guard that turns non-JSON bodies (proxy errors, plain-text 500s) into a readable `Error` with the status code (the kaminos pattern):
  ```ts
  export type AgentSummary = { name: string; status: string; model: string; created_at: string }
  export type StepView = { name: string; label: string; status: string; error: string | null }
  export type AgentDetail = AgentSummary & { persona: string; skills: {name: string; sha: string}[];
    repo_full_name: string | null; first_reply: string | null; error: string | null; steps: StepView[] }
  export type Catalogs = { personas: {id: string; description: string}[];
    skills: {id: string; description: string; sha: string}[];
    models: {id: string; label: string}[]; defaultModel: string }

  export async function listAgents(): Promise<AgentSummary[]>
  export async function getAgent(name: string): Promise<AgentDetail>
  export async function createAgent(body: {name: string; persona: string; skills: string[]; model: string | null}): Promise<{name: string; status: string}>
  export async function retryAgent(name: string): Promise<void>
  export async function fetchCatalogs(): Promise<Catalogs>       // three GETs in parallel, merged
  export async function checkAvailability(name: string): Promise<{available: boolean; reason: string | null}>
  export class ApiError extends Error { status: number; }        // parse guard throws this
  ```
  (Verify response envelope keys against the running API/tests: list returns `{"agents":[...]}`, catalogs return `{"personas":[...]}` etc., models returns `{"models":[...],"default":...}` — unwrap inside api.ts so callers get clean arrays.)
- `vite.config.ts`: svelte plugin + `server.proxy = {"/api": "http://127.0.0.1:8100"}`.
- `app.css`: token sheet (`--bg`, `--panel`, `--ink`, `--muted`, `--accent`, `--ok`, `--fail`, `--warn`, spacing vars, `@keyframes pulse`), light-first with `prefers-color-scheme: dark` overrides.

- [ ] **Step 1:** Scaffold the toolchain (`npm create vite@latest` equivalent written by hand — pin: svelte ^5, vite ^6, @tanstack/svelte-query ^5, typescript, svelte-check, vitest). Write vitest tests for `api.ts` first (mock `fetch`): parse guard converts a text/html 502 into ApiError with status; `listAgents` unwraps `{"agents":[...]}`; `fetchCatalogs` merges three responses and maps `default` → `defaultModel`; `createAgent` surfaces a 409's `{"error": "..."}` message.
- [ ] **Step 2:** `cd ui && npm install && npx vitest run` → red (api.ts missing).
- [ ] **Step 3:** Implement `api.ts` + shell files + tokens css + Makefile.
- [ ] **Step 4:** `make -C ui check` → svelte-check clean, vitest green. `npm run dev` boots and proxies (manual spot check: `curl localhost:5173/api/healthz` through the proxy while the API runs — if the API isn't running locally, note it and skip).
- [ ] **Step 5:** Commit: `feat(ui): scaffold — vite+svelte5, typed api client, design tokens`

### Task 2: Query layer — adaptive polling, availability debounce

**Files:**
- Create: `ui/src/lib/queries.ts`, `ui/src/lib/status.ts`, `ui/src/lib/debounce.ts`
- Test: `ui/src/lib/status.test.ts`, `ui/src/lib/debounce.test.ts`

**Interfaces:**
- `status.ts` (pure, fully unit-tested):
  ```ts
  export const TERMINAL = ["ready", "failed"] as const
  export function isTerminal(status: string): boolean
  export function anyAgentActive(agents: {status: string}[]): boolean       // any non-terminal
  export function listRefetchInterval(agents?: {status: string}[]): number | false  // 3500 while active, else false
  export function detailRefetchInterval(agent?: {status: string}): number | false   // 3500 while non-terminal, else false
  export function stepTone(status: string): "ok" | "fail" | "active"       // done→ok, failed→fail, running|pending→active (spec: pending reads as working)
  ```
- `queries.ts`: exports factory functions returning TanStack query options objects — `agentsQuery()`, `agentQuery(name)`, `catalogsQuery()` (staleTime 5min), each wiring the interval fns above via `refetchInterval: (q) => ...(q.state.data)`. Also `createAgentMutation(queryClient)` (invalidates the list) and `retryAgentMutation(queryClient, name)`.
- `debounce.ts`: `export function debounced<T>(fn: (v: T) => void, ms = 350): (v: T) => void` plus a cancel handle — drives the availability check.

- [ ] **Step 1:** vitest first: interval fns return 3500/false per the truth table (empty list → false; one provisioning agent → 3500; all ready → false; detail pending → 3500; detail failed → false); `stepTone` mapping incl. pending→"active"; debounce fires once after quiet period (fake timers), cancel works.
- [ ] **Step 2:** red. **Step 3:** implement. **Step 4:** `make -C ui check` green. **Step 5:** Commit: `feat(ui): query layer — adaptive polling, availability debounce`

### Task 3: Shared components + toast + router

**Files:**
- Create: `ui/src/lib/router.svelte.ts`, `ui/src/lib/toast.svelte.ts`, `ui/src/components/{EmptyState,ErrorState,Skeleton,Toasts,StatusBadge,Modal}.svelte`
- Test: `ui/src/lib/router.test.ts` (pure parse/format helpers)

**Interfaces:**
- `router.svelte.ts`: hash-based (`#/` dashboard, `#/agents/<name>` detail) — `export const route = $state({...})` style rune store with `parseHash(hash): {page: "dashboard"} | {page: "agent", name: string}` (pure, tested) and `navigate(to)`; listens to `hashchange`.
- `toast.svelte.ts`: `toasts` rune store + `toast.success(title, body?)` / `toast.error(title, body?)`, auto-dismiss 5s; `Toasts.svelte` renders the stack bottom-right.
- `StatusBadge.svelte`: props `status: string` → colored dot + label (uses `stepTone`/terminal mapping; "provisioning"/"pending" get the amber pulse).
- `Modal.svelte`: focus-trapped dialog shell with backdrop close + Escape, slot content.
- `EmptyState/ErrorState/Skeleton`: the family's Empty/Error/Skeleton trio — ErrorState takes `message` + `onretry`.

- [ ] **Step 1:** vitest for `parseHash` (roots, agent names, junk → dashboard). **Step 2-3:** red, implement all files. **Step 4:** `make -C ui check` green. **Step 5:** Commit: `feat(ui): router, toasts, shared state components`

### Task 4: Dashboard — agent cards + wired shell

**Files:**
- Create: `ui/src/pages/Dashboard.svelte`, `ui/src/components/AgentCard.svelte`
- Modify: `ui/src/App.svelte` (QueryClientProvider, route switch, Toasts mount, header)

**Behavior:**
- Header: wordmark "cortex — assembly room", right-aligned **Create agent** button (hover/focus prefetches `catalogsQuery` via `queryClient.prefetchQuery` — the warm-modal prefetch trick).
- Grid of `AgentCard`s from `agentsQuery`: name, StatusBadge, model label, relative created time; failed cards show a one-line error teaser. Click → `#/agents/<name>`.
- Query states: Skeleton grid while loading; ErrorState with retry on failure; EmptyState ("No agents yet — create your first") when empty.
- While any agent is non-terminal the list self-polls (Task 2 wiring) — no manual refresh anywhere.

- [ ] **Step 1:** implement (no new pure logic → no new vitest; svelte-check is the gate). **Step 2:** `make -C ui check` green; dev-server manual check against the live API renders clio's card READY. **Step 3:** Commit: `feat(ui): dashboard with live agent cards`

### Task 5: Create modal — the one-click moment

**Files:**
- Create: `ui/src/components/CreateModal.svelte`
- Modify: `ui/src/pages/Dashboard.svelte` (open/close state), `ui/src/lib/api.ts` only if a gap surfaces (flag it, don't silently extend)

**Behavior (spec §3 verbatim):**
- Four fields, two pre-filled: **Name** (regex hint `a-z 0-9 -`, 2-30 chars; on input → debounced 350ms `checkAvailability`; inline "✓ available" / reason / "checking…"; Create button disabled until available). **Persona** (radio-card list from catalog with descriptions; first item preselected). **Skills** (checkbox list; empty catalog → quiet "No skills in the catalog yet" line, not an error). **Model** (select, preselected to `defaultModel`).
- Submit → `createAgentMutation`: on success `toast.success("Agent created", "Assembling <name>…")`, close modal, invalidate list (card appears with pulsing badge — fire-and-return, never a blocking spinner); on 409/422 → inline error line inside the modal, modal stays open.
- Zero infra vocabulary in every string.

- [ ] **Step 1:** implement; extract any nontrivial validation into `ui/src/lib/validate.ts` with vitest (name regex mirror of the API's NAME_RE, 2-30, no trailing hyphen — client-side mirror can be stricter than server; note: server allows trailing hyphen today, the UI deliberately forbids it per the P1b-backlog finding).
- [ ] **Step 2:** `make -C ui check` green. **Step 3:** Commit: `feat(ui): create modal — name availability, persona/skills/model pickers`

### Task 6: Agent detail — checklist, first words, retry

**Files:**
- Create: `ui/src/pages/AgentDetail.svelte`, `ui/src/components/{StepChecklist,FirstReply}.svelte`

**Behavior:**
- Header: back link, name, StatusBadge, model label, repo link (plain "source" link using `repo_full_name` → `https://github.com/<repo>`) when present.
- `StepChecklist`: vertical rows from `steps[]` — icon per `stepTone` (✓ green / ✕ red / pulsing dot amber), API-provided `label` as the row text, failed rows show `error` in a collapsible block. Pending rows visually identical to running (the spec's "whole queue reads as working").
- `FirstReply`: once `first_reply` is non-null, a quiet card titled "First words" with the reply text — the payoff moment, give it room.
- Status FAILED → banner with **Retry** button → `retryAgentMutation` → toast + resume polling. Detail self-polls while non-terminal (Task 2 wiring).
- Composition summary strip (read-only): persona id, skill chips, model — no editing affordances (P2).

- [ ] **Step 1:** implement. **Step 2:** `make -C ui check` green; manual dev check against live clio: checklist shows 7 green rows + First words card. **Step 3:** Commit: `feat(ui): agent detail — step checklist, first words, retry`

### Task 7: Live smoke + docs (milestone)

- [ ] Playwright smoke (use the webapp-testing toolkit): against the running dev server + live API — dashboard renders clio READY; detail page shows 7-step checklist all green and the First-words card; create-modal opens with catalogs loaded and the name field validates ("clio" → taken, junk → invalid). Screenshot each. Do NOT create an agent from the smoke (that's the operator milestone below).
- [ ] **The exit criterion, live:** create one new agent entirely from the browser (operator or controller): open Create, name it (fresh name, e.g. `muse`), pick helpful-generalist + default model, submit — watch the card pulse through the checklist to READY and read its first words. Record the result + screenshots.
- [ ] README: swap the Status paragraph's "The web UI is next" for a line that the assembly room UI is live in dev (`make -C ui dev`), and add `make -C ui ...` to the Running section. Commit: `docs: assembly room UI dev instructions + P1b smoke results`

---

## Self-review notes (applied)

- Spec §3 coverage: single-modal create w/ 4 inputs + live availability ✓ (T5), fire-and-return + toast + pulsing card ✓ (T4/T5), checklist w/ plain labels + pending-as-working ✓ (T6), adaptive polling 3-4s/off ✓ (T2), zero infra vocabulary ✓ (constraint + copy review in T7), shared Empty/Error/Skeleton trio + hover prefetch ✓ (T3/T4). Detail-page *editing* explicitly rescoped to P2 (no API for it; exit criterion unaffected) — stated in the header.
- Types flow one way: api.ts owns wire types; queries.ts owns caching; status.ts owns the truth tables; components consume. No component fetches directly.
- The UI's stricter name rule (no trailing hyphen) intentionally front-runs the API-side P1b-backlog fix; server remains authoritative.
- P1b-high infra backlog (CI concurrency race, startup sweep, etc.) is deliberately NOT in this plan — it's API/platform work, tracked in the ledger for a hardening pass.

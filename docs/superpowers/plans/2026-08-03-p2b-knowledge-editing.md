# P2b — Knowledge Editing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Edit what a READY agent knows from its detail page — a Memory panel that reads/edits/adds/removes the repo's `bundle/knowledge/*.md` seed files, with a diff preview before every save, and a delivery status that tracks the change through rebuild → redeploy → convergent reseed.

**Architecture:** The agent's git repo stays the bundle of record and the server stays stateless — no new DB tables. Reads clone the repo fresh and list `bundle/knowledge/*.md`; saves clone, write one file, commit ("cortex: update knowledge/<file>"), push, and return the pushed commit's sha read from the LOCAL clone (never `head_sha(repo)` right after a push — the P1c read-after-write lesson). Delivery status is computed on demand from two facts the platform already exposes: the CI run for that sha (`runs_for_commit`, existing) and the live Deployment's image tag (new `KubectlClient.deploy_image`; generated CI tags images `${GITHUB_SHA::12}`, verified in the generator template). The browser owns the pending-delivery sha exactly like it owns the chat transcript, polling a delivery endpoint until `delivered`/`failed`. The boot-time convergent seeder (content-hash per file) is the mechanism that makes an edit land as "only this file's entries replaced" — the milestone proves it from the seeder's own `seeded=/removed=/unchanged=` log line.

**Tech Stack:** unchanged (FastAPI/pytest; Svelte 5/vitest).

## Global Constraints

- Never the employer's name or work-app name in any tracked file; zero infra vocabulary in UI copy (no "commit", "CI", "deploy", "image", "sha", "pipeline" in anything user-visible — copy speaks of saving changes and the agent picking them up).
- TDD; `make -C api check` and `make -C ui check` fully green before every commit; **git add only named files**.
- Server stateless for knowledge: no new tables, no persisted delivery records; the repo is the source of truth.
- Knowledge filename rule (server-side): `^[a-z][a-z0-9-]{0,39}\.md$`; content 1..65536 bytes (422 outside). One file per save; one commit per save/delete; last-write-wins (no conflict detection in the POC).
- Delivery verification fact (from the deploy platform's generated CI, read at planning time): images are tagged with the first 12 chars of the commit sha and applied by the same workflow run. `delivered` means the live Deployment's image tag equals `sha[:12]`.
- Per-request clone latency (~1-3s) is accepted for dev, same budget class as the chat port-forward; do not build caching.

---

### Task 1: API — knowledge module + `deploy_image` / `local_head` client additions

**Files:** `api/cortex_api/knowledge.py` (new), `api/cortex_api/clients.py` (two Protocol additions + real impls), `api/tests/fakes.py` (FakeGitHub/FakeKubectl knobs), `api/tests/test_knowledge.py` (new), `api/tests/test_clients.py` (argv tests for the two new client methods).

**Interfaces:**
```python
# clients.py additions
class GitHubClient(Protocol):
    def local_head(self, workdir: Path) -> str: ...   # `git -C <workdir> rev-parse HEAD` — the pushed sha, no API lag

class KubectlClient(Protocol):
    def deploy_image(self, namespace: str, deploy: str) -> str: ...  # jsonpath .spec.template.spec.containers[0].image

# knowledge.py
KNOWLEDGE_FILENAME_RE  # ^[a-z][a-z0-9-]{0,39}\.md$
KNOWLEDGE_MAX_BYTES = 65536

class KnowledgeError(Exception): ...          # bad filename, missing file on delete, size violations

@dataclass
class KnowledgeFile:
    name: str
    content: str

def read_knowledge(github: GitHubClient, repo: str) -> list[KnowledgeFile]
    # clone to a TemporaryDirectory, list bundle/knowledge/*.md sorted by name; missing dir -> []
def save_knowledge_file(github: GitHubClient, repo: str, filename: str, content: str) -> str
    # validate filename+size, clone, write bundle/knowledge/<filename>, commit_push
    # message f"cortex: update knowledge/{filename}"; return github.local_head(workdir).
    # commit_push returning False (content identical) still returns local_head — the
    # delivery check then reports delivered immediately, which is truthful.
def delete_knowledge_file(github: GitHubClient, repo: str, filename: str) -> str
    # validate, clone, missing file -> KnowledgeError; remove, commit_push
    # f"cortex: remove knowledge/{filename}", return local_head
def delivery_state(github: GitHubClient, kubectl: KubectlClient, *,
                   repo: str, namespace: str, deploy: str, sha: str) -> dict
    # runs = github.runs_for_commit(repo, sha)
    #   no runs yet -> {"state": "building", "detail": None}          (registration lag, same grace BuildStep uses)
    #   any run queued/in_progress -> "building"
    #   newest completed run conclusion != success -> {"state": "failed", "detail": "the update run did not complete — save again to retry"}
    #   success: kubectl.deploy_image(namespace, deploy) tag == sha[:12] -> "delivered", else "rolling"
```
- Fakes: `FakeGitHub` gains `knowledge_files: dict[str, str] | None` (materialized under `bundle/knowledge/` by `clone`, alongside the existing scaffolding) and `local_head_result: str` (default `"editsha0"*5`-style fixed value); `FakeKubectl` gains `images: dict[tuple[str, str], str]` for `deploy_image` (default a wired-happy tag so existing tests are unaffected).
- Tests (test_knowledge.py, against fakes): read lists sorted files / empty-dir case; save writes the file in the pushed workdir + commit message exact + returns local_head; save rejects bad filename, oversize, empty; delete removes + missing-file KnowledgeError; delivery_state each branch (no-runs, in-progress, failure, success+stale-image → rolling, success+matching-image → delivered). Argv tests (test_clients.py): `local_head` exact git argv; `deploy_image` exact kubectl argv.

- [ ] TDD per house pattern; commit: `feat(api): knowledge module — read/edit seed files in the agent repo, stateless delivery check`

### Task 2: API — knowledge endpoints

**Files:** `api/cortex_api/main.py`, `api/tests/test_api_knowledge.py` (new).

**Interfaces:**
- `GET /api/agents/{name}/knowledge` → `200 {"files": [{"name","content"}...]}`.
- `PUT /api/agents/{name}/knowledge/{filename}` body `{"content": str}` → `200 {"sha": str}` (upsert: create or replace).
- `DELETE /api/agents/{name}/knowledge/{filename}` → `200 {"sha": str}`; file absent → `404 {"error": f"no such knowledge file: {filename!r}"}`.
- `GET /api/agents/{name}/knowledge/delivery/{sha}` → `200 {"state": "building"|"rolling"|"delivered"|"failed", "detail": str|null}`; sha must match `^[0-9a-f]{7,40}$` else 422.
- Shared guards on all four: 404 unknown agent (house shape), 409 unless READY (`{"error": "agent is not ready for memory changes"}`). 422s from KnowledgeError/validation carry the reason. `ClientError` → `502 {"error": "..."}` (house convention). namespace=deploy=agent.name; repo from `agent.repo_full_name`.
- Tests: happy read/save/delete against FakeGitHub knobs (assert the pushed workdir content and commit messages via the fake's recorded pushes); each guard per route; delivery endpoint mapping each state; 422 sha-shape; 502 on a ClientError-raising fake.

- [ ] TDD; commit: `feat(api): knowledge endpoints — read, upsert, delete, delivery status`

### Task 3: UI — client, queries, and line diff

**Files:** `ui/src/lib/api.ts`, `ui/src/lib/queries.ts`, `ui/src/lib/diff.ts` (new), and their tests (`api.test.ts`, `queries.test.ts`, `diff.test.ts` new).

**Interfaces:**
```ts
export type KnowledgeFile = { name: string; content: string }
export async function listKnowledge(name: string): Promise<KnowledgeFile[]>
export async function saveKnowledgeFile(name: string, filename: string, content: string): Promise<{ sha: string }>
export async function deleteKnowledgeFile(name: string, filename: string): Promise<{ sha: string }>
export async function fetchDeliveryState(name: string, sha: string): Promise<{ state: string; detail: string | null }>
// queries.ts: knowledgeQuery(name); saveKnowledgeMutation(queryClient, name) and
// deleteKnowledgeMutation(queryClient, name) — both invalidate knowledgeQuery on success;
// deliveryQuery(name, sha) — refetchInterval 3500 while state is building/rolling, false
// once delivered/failed (mirror the existing adaptive-interval idiom), enabled only when sha non-null.
// diff.ts: lineDiff(before: string, after: string): Array<{ kind: "same" | "added" | "removed"; text: string }>
//   LCS over lines; pure, no deps.
```
- Tests: request/URL/body shape per client fn (existing fetch-mock harness); error surfacing via ApiError message for a 409; deliveryQuery interval logic (function form exercised with fake query state, like existing adaptive tests); lineDiff — identical inputs → all same; pure insert; pure delete; replace (remove+add adjacency); empty-before (all added); ordering preserved.

- [ ] TDD; commit: `feat(ui): knowledge client + delivery polling + line diff`

### Task 4: UI — MemoryPanel

**Files:** `ui/src/components/MemoryPanel.svelte` (new), `ui/src/pages/AgentDetail.svelte` (mount below Chat).

**Behavior:**
- READY-only (same gate as ChatPanel), heading "Memory", subtitle copy along the lines of "What your agent knows from the start." Lists each knowledge file as a row (name minus `.md`, first line as preview). Loading/error states via the house skeleton/error idioms.
- Edit: opens the shared `Modal` with a textarea (content preloaded). Primary action "Review changes" swaps the modal body to the `lineDiff` rendering (added lines green-tinted, removed red-tinted, same dimmed; reuse the app's tone variables) with "Save" + "Back to editing". Save fires the mutation; on success the modal closes and the panel shows a delivery chip.
- Add: name input (lowercase letters/digits/hyphens, 1-40 chars; `.md` appended automatically — never shown) + empty textarea, same review-then-save flow. Delete: per-row affordance with an in-modal confirm; also produces a delivery chip.
- Delivery chip: driven by `deliveryQuery(name, sha)` where sha is local `$state` from the last save/delete (the browser owns it, like the chat transcript). Copy mapping — building/rolling: "Delivering changes…" with the pulse treatment; delivered: "Changes are in place" (clears after a few seconds); failed: the server's detail via `errorMessage`, plain-language. Navigating away loses the chip; the edit itself is already safe in the repo — acceptable and documented here.
- Send/save buttons disabled while a save is pending. A11y: modal focus per existing Modal behavior; diff rendering readable without color (prefix "+ "/"− " on lines); rows keyboard-actionable.
- Zero infra vocabulary anywhere, including error fallbacks.

- [ ] Implement (svelte-check gate; pure logic stays in Task 3's diff.ts); commit: `feat(ui): memory panel — edit what an agent knows, with diff preview and delivery status`

### Task 5: Live milestone

- [ ] Stack up (API :8100 restarted at HEAD, UI :5173; clio READY — its repo has `gotchas.md`). From the browser: ADD `word-of-the-day.md` ("## Word of the day\nThe word of the day is periwinkle. If anyone asks, tell them proudly."). Watch the chip run building → delivering → in place. Then `kubectl logs` the NEW clio pod's seeder line — expect `seeded=1 removed=0 unchanged=1` (gotchas untouched: convergence). Chat with clio: "what is the word of the day?" → reply must name periwinkle. Screenshot panel + chat.
- [ ] EDIT the same file (periwinkle → marmalade), deliver, seeder line again `seeded=1 removed=0 unchanged=1`, chat again → marmalade. This is the full edit-converge-answer loop, recorded verbatim.
- [ ] Failure-path note: if no delivery failure occurs naturally, don't manufacture one live — the state mapping is unit-covered (Task 1) and BuildStep-class CI flakes were exhaustively exercised in P1c.
- [ ] Append `## P2b results` to the runbook (transcripts + seeder lines verbatim); README status line gains knowledge editing; commit: `P2b results: knowledge editing live`

---

## Self-review notes (applied)

- Spec coverage: "knowledge-file editing that reseeds convergently on redeploy" (spec P2 line) — Tasks 1-5; "diff preview before commit" (spec detail-page line) — Task 3 lineDiff + Task 4 review step. Deliberately NOT built (spec grants none of these to P2b): ruamel round-trip editing (that's for agent.yaml edits, a later panel — knowledge files are plain text), bulk import, conflict detection, delivery persistence.
- The P1c carry "pre-existing agent repos lack the CI concurrency block" applies to clio: irrelevant for single serialized saves; two rapid-fire saves could race applies. Mitigation already in the design: the delivery check compares the LIVE image tag to the newest sha, so a raced stale apply reports "rolling"/never "delivered" rather than lying — and the P1c DeployStep lesson says that's the honest failure mode. Noted for the milestone; not worth new machinery.
- Type consistency: `local_head`/`deploy_image` extend the same Protocols + fakes as every prior client addition; delivery reuses `runs_for_commit` exactly as BuildStep consumes it; endpoint guard shapes match P2a's chat endpoint conventions verbatim.
- P2 remaining after this: P2c Slack (config token in hand — gitignored local file; rotate via tooling.tokens.rotate, refresh token single-use), P2d avatars (decision: best available image endpoint in the Vertex model garden, enable if needed), Archive/Library extraction (unscheduled).

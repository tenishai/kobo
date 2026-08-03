# P2c — Slack Connect Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect a READY agent to Slack from its detail page — cortex creates a scoped Slack app from a manifest, walks the user through the three steps Slack has no API for (install, tokens, icon) on one "Finish Slack setup" card, delivers the tokens to the cluster as a namespace Secret, wires the runtime's Slack transport through the repo, and the agent answers mentions in a channel.

**Architecture:** The runtime side already exists: daedalus ships a Socket-Mode Slack transport (thread-per-task, redelivery dedupe, reaction lifecycle) activated by an `agent.yaml` transports entry `{kind: slack, channel: <id>}` with tokens from `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` env — but the runtime image pins `daedalus[a2a]` only, so the image grows the `slack` extra first (sibling-branch change, established AR flow). Cortex's side follows two proven patterns end to end: app creation is a new `SlackClient` (httpx against Slack's manifest API, config token from a gitignored local file with rotate-on-expiry since config tokens live ~12h), and wiring is the WireStep pattern verbatim — a repo commit editing `agent.yaml` (transport entry) and `gojira.yaml` (`secrets_from: [<name>-slack]`, honored by the deploy platform's generator), plus a `kubectl create secret` into the agent's namespace; the tokens the user pastes flow through the API straight into that Secret and are never persisted or logged. Delivery reuses P2b's stateless delivery machinery unchanged (commit sha → CI run → live image tag). Socket Mode means no ingress anywhere.

**Tech Stack:** unchanged (FastAPI/pytest; Svelte 5/vitest; runtime-kit branch Docker).

## Global Constraints

- Never the employer's name or work-app name in any tracked file; zero infra vocabulary in UI copy (no "manifest", "OAuth scope", "Socket Mode", "secret", "namespace", "env" in anything user-visible — copy speaks of connecting to Slack and finishing setup in Slack).
- TDD; `make -C api check` / `make -C ui check` / the runtime kit's own suite green before every commit; **git add only named files**; sibling-repo work stays on `feat/runtime-image` (runtime kit) — never main; no PRs until Kyle says.
- Slack tokens (config token pair, pasted bot/app tokens) NEVER appear in tracked files, DB rows, logs, or error messages. The config-token pair lives in `api/slack.local.yaml` (gitignored) and rotates via `tooling.tokens.rotate` (refresh token single-use — always persist the returned pair immediately). Pasted bot/app tokens exist only in the request body and the `kubectl create secret` argv.
- The app manifest cortex renders MUST carry exactly the runtime transport's documented bot scopes (`app_mentions:read`, `channels:history`, `chat:write`, `reactions:read`, `reactions:write`), `socket_mode_enabled: true`, bot events `app_mention` + `message.channels`, `interactivity: false`, `token_rotation_enabled: false` — the canonical template is `docs/slack-app-manifest.yaml` in the runtime kit repo.
- Agent DB rows may store only non-secret Slack state: `slack_app_id`, `slack_channel` (nullable strings).
- Slack Web API base `https://slack.com/api/`; `apps.manifest.create` takes the config token as Bearer + `manifest` (JSON string); replies `{ok, app_id, credentials..., errors...}` — treat `ok: false` with `error` as a typed failure. Config-token expiry surfaces as `token_expired`/`invalid_auth` → rotate once and retry once, then fail.

---

### Task 1: Runtime kit — image gains the slack extra + seeder last-file fix

**Repo:** ~/Documents/GitHub/daedalus, branch `feat/runtime-image`. **Files:** `docker/Dockerfile`, `docker/seeder.py`, `docker/test_seeder.py`.

- Both pip lines pin `daedalus[a2a]` — change both to `daedalus[a2a,slack]` (wheel-build line 25 and install line 32; keep everything else byte-identical).
- **Seeder last-file fix (P2b final-review gap, confirmed by code reading):** `main()` early-returns when `knowledge/` is absent (`seeder.py:184-186`) — but git drops empty dirs, so deleting the LAST knowledge file removes the dir from the image and the removal loop never runs: the deleted file's seeded rows persist in techne forever. Fix: when `knowledge_dir` is missing, do NOT return early — run the same `sync(...)` with an empty current set (simplest: `knowledge_dir.mkdir()` on a temp path, or teach `sync` to accept a missing dir as "no files") so state-tracked removals still purge. Keep the "nothing to seed" log line for the truly-never-seeded case if state is empty. TDD in `docker/test_seeder.py`: seed two files → re-run with knowledge/ absent → both files' entries deleted, state rows gone, result `removed=2`; and a never-seeded bundle with no knowledge/ stays a clean no-op.
- [ ] Build + push the image to the established AR path with tag = the branch's new short sha (the P0 flow: `docker build --platform linux/amd64`, `docker push`); run the kit's own suite unchanged-green plus the new tests; verify the pushed image imports the extra: `docker run --rm --entrypoint python <image> -c "import slack_sdk; print('slack ok')"`.
- [ ] Commit: `feat(docker): slack transport extra + seeder purges when knowledge/ disappears`
- [ ] Controller follow-up (not the implementer): bump `runtime_image` in cortex's gitignored `api/config.local.yaml` to the new tag and record old→new in the ledger. NOTE: existing agents keep their old image until their next delivery; the milestone agent's Slack connect (Task 6) triggers exactly that rebuild — no fleet migration needed.

### Task 2: API — SlackClient (manifest create + config-token rotation)

**Repo:** cortex. **Files:** `api/cortex_api/clients.py` (Protocol + real impl), `api/cortex_api/config.py` (optional `slack_tokens_path: Path | None = None` setting, expanduser-validated like its siblings), `api/tests/fakes.py` (FakeSlack), `api/tests/test_clients.py`, `api/tests/test_config.py`.

**Interfaces:**
```python
class SlackClient(Protocol):
    def create_app(self, manifest: dict) -> str:
        """Create a Slack app from `manifest`; return its app_id."""

class SlackApiError(ClientError):  # subclass so existing 502 mapping catches it
    ...

class HttpxSlackClient:
    def __init__(self, tokens_path: Path, http_client: httpx.Client | None = None): ...
    # create_app: POST apps.manifest.create, Authorization: Bearer <config token>,
    #   form/json body manifest=<json.dumps(manifest)>.
    #   ok:false + error in {"token_expired","invalid_auth"} -> _rotate() once, retry once.
    #   any other ok:false -> SlackApiError(f"slack app creation failed: {error}")
    #   (never include token material in messages).
    # _rotate(): POST tooling.tokens.rotate with refresh_token from tokens_path;
    #   persist BOTH returned tokens back to tokens_path atomically (write tmp + rename)
    #   BEFORE using the new access token — the refresh token is single-use, losing
    #   the new pair locks us out.
```
- tokens_path YAML shape (already in use, gitignored): keys `slack_app_config_token`, `slack_app_config_refresh_token`; preserve leading comment lines on rewrite (read lines, replace the two `key: value` lines — same approach the rotation script used).
- `Clients` container gains `slack: SlackClient | None` (None when `slack_tokens_path` unset — routes must 503 with a clear "Slack is not configured" error, keeping local setups without tokens working).
- `FakeSlack`: records manifests, returns configurable `app_id` (default "A0FAKE"), `create_error` knob; a `rotate_scenario` knob is NOT needed at the Protocol level (rotation is HttpxSlackClient-internal — test it with httpx.MockTransport: first call returns token_expired, rotate endpoint returns a new pair, retried call succeeds; assert the file got the new pair BEFORE the retry, and the old refresh token is gone).
- Tests: manifest POST shape (Bearer header from file, manifest JSON-encoded) via MockTransport; rotate-retry-persist sequence; non-auth error → SlackApiError with the slack error string; missing/malformed tokens file → ClientError naming the path (not the contents).

- [ ] TDD; commit: `feat(api): slack client — manifest app creation with config-token rotation`

### Task 3: API — Slack connect endpoints + wiring

**Repo:** cortex. **Files:** `api/cortex_api/slack_setup.py` (new: manifest render + wire logic), `api/cortex_api/models.py` (two nullable columns), `api/cortex_api/main.py` (routes), `api/tests/test_slack_setup.py` (new), `api/tests/test_api_slack.py` (new).

**models.py:** `Agent` gains `slack_app_id: str | None` and `slack_channel: str | None` (nullable Columns; SQLite ALTER-free — new columns with defaults on a fresh create_all; for the existing dev DB, note in the report that the dev sqlite file needs the two columns added or the DB recreated — the milestone will use a fresh agent, and the controller handles the live DB with a one-time `ALTER TABLE agents ADD COLUMN ...` — record it in the runbook).

**slack_setup.py:**
```python
def render_manifest(agent_name: str, persona_first_line: str) -> dict
    # display name/bot name = agent_name; description = persona_first_line (<=100 chars, trimmed);
    # scopes/events/settings EXACTLY per Global Constraints (copy the canonical template's values).
def wire_slack(github, kubectl, *, repo, name, channel, bot_token, app_token) -> str
    # 1) kubectl create secret generic {name}-slack -n {name}
    #      --from-literal=SLACK_BOT_TOKEN=... --from-literal=SLACK_APP_TOKEN=...
    #      --dry-run=client -o yaml | kubectl apply -f -   (idempotent upsert; KubectlClient
    #      gains apply_secret(namespace, name, literals: dict[str, str]) doing exactly this —
    #      argv-tested; tokens appear in argv only, never in raised messages)
    # 2) clone repo; edit bundle/agent.yaml: transports list gains/updates
    #      {kind: slack, channel: <channel>} (ruamel round-trip like WireStep's gojira edit;
    #      idempotent — replace existing slack entry); edit gojira.yaml: secrets_from gains
    #      "{name}-slack" (append-if-missing, never replace the list).
    # 3) commit_push "cortex: connect slack"; return local_head(workdir)  [P2b lesson]
```

**Routes (main.py), all guarded 404/409-unless-READY like knowledge, 503 when clients.slack is None:**
- `POST /api/agents/{name}/slack/app` → renders manifest, `slack.create_app`, stores `slack_app_id`, returns `{"app_id": ..., "configure_url": f"https://api.slack.com/apps/{app_id}"}`. 409 (distinct message "Slack is already set up for this agent") if `slack_app_id` already set — idempotent guidance, not a crash.
- `POST /api/agents/{name}/slack/connect` body `{"channel": str, "bot_token": str, "app_token": str}` → 422 unless channel matches `^[CG][A-Z0-9]{6,}$`, bot_token starts `xoxb-`, app_token starts `xapp-` (format checks only — never echo the tokens back in error bodies); requires `slack_app_id` set (409 otherwise); calls `wire_slack`, stores `slack_channel`, returns `{"sha": ...}` (the P2b delivery endpoint tracks it from there).
- `GET /api/agents/{name}` detail gains `"slack": {"app_id": ..., "channel": ...} | null` (null when no app yet).
- Tests: manifest render exactness (scopes list byte-equal to the constraint, name/description substitution, description truncation); wire_slack fake-level (secret argv recorded, agent.yaml transport entry added AND idempotently replaced on second call, gojira.yaml secrets_from appended not clobbered, commit message exact, returns local_head); route guards (404/409-not-ready/409-no-app/409-already-app/422 token-format triple/503-unconfigured); token material absent from every error body and from the fake's recorded push contents (assert the tokens do NOT appear in any file written to the repo clone).

- [ ] TDD; commit: `feat(api): slack connect — app creation, secret delivery, transport wiring`

### Task 4: UI — lib additions

**Repo:** cortex. **Files:** `ui/src/lib/api.ts`, `ui/src/lib/queries.ts`, their tests.

```ts
// api.ts: AgentDetail gains slack: { app_id: string; channel: string | null } | null
export async function createSlackApp(name: string): Promise<{ app_id: string; configure_url: string }>
export async function connectSlack(name: string, body: { channel: string; bot_token: string; app_token: string }): Promise<{ sha: string }>
// queries.ts: createSlackAppMutation(queryClient, name) and connectSlackMutation(queryClient, name)
//   — both invalidate agentQuery(name) on success (detail carries the slack state).
```
- Tests: request shapes; 409/503 error surfacing; type additions compile (fixture updates in the four named test files only, disclosed).

- [ ] TDD; commit: `feat(ui): slack connect client + agent slack state`

### Task 5: UI — SlackPanel (the "Finish Slack setup" card)

**Repo:** cortex. **Files:** `ui/src/components/SlackPanel.svelte` (new), `ui/src/pages/AgentDetail.svelte` (mount below Memory).

- READY-only. Heading "Slack". Three visual states driven by `data.slack`:
  1. **Not connected:** one-line pitch ("Let people talk to <name> in Slack.") + "Connect to Slack" button → `createSlackAppMutation`; on success the card advances (detail refetch carries the new state).
  2. **App created, not wired (`slack.channel` null):** the checklist card. Step 1 "Install to your workspace" — external link to `configure_url` (from a re-call of the create endpoint? NO — store `configure_url` derivably: `https://api.slack.com/apps/${slack.app_id}`, computed client-side) with plain-language help ("Install the app, then copy the token that starts with xoxb-"). Step 2 "Turn on its connection" — help text pointing at Basic Information → App-Level Tokens ("create one that can connect, copy the token that starts with xapp-"). Step 3 inputs: channel ID (help: "open the channel's details in Slack and copy its ID"), bot token (password-type input), app token (password-type input) + "Finish setup" → `connectSlackMutation` → delivery chip (REUSE the delivery-chip pattern from MemoryPanel — same deliveryQuery, same copy). Also a quiet note that a picture for the app is added in Slack's settings later (the icon has no API).
  3. **Connected (`slack.channel` set):** "In Slack" state showing the channel id, plus the delivery chip if a delivery is in flight.
- Token inputs: `type="password"`, `autocomplete="off"`, values cleared from local state after a successful submit; never rendered back.
- Failure of connect → in-panel error via `errorMessage`; inputs retained EXCEPT tokens (cleared — the user re-pastes; message says so plainly).
- A11y: labeled inputs, the external link marked with visually-hidden "(opens Slack)" suffix; zero infra vocabulary.

- [ ] Implement (svelte-check gate); commit: `feat(ui): slack panel — connect an agent to slack with a finish-setup card`

### Task 6: Live milestone

- [ ] Preflight (controller): `ALTER TABLE agents ADD COLUMN slack_app_id VARCHAR; ALTER TABLE agents ADD COLUMN slack_channel VARCHAR;` on the dev sqlite (or recreate); restart API; confirm runtime_image bump from Task 1 is in config.local.yaml.
- [ ] From the browser on a READY agent (use echo — keeps clio's P2b state clean): Connect to Slack → app appears in the sandbox workspace (screenshot the card states). Do the manual steps in Slack (install → xoxb token; app-level token with connections:write → xapp token; create/pick a sandbox channel, invite the bot, copy channel ID). Finish setup → delivery chip → in place (this delivery also carries the runtime-image bump → slack extra present).
- [ ] Proof: in the sandbox Slack channel, `@<agent> what do you remember about how you got here?` → the agent replies IN THREAD from seeded memory (screenshot the Slack thread). Verify the reaction lifecycle (eyes → white_check_mark) matches the transport's contract. Also verify a2a chat STILL works (both transports live) via one browser chat exchange.
- [ ] Append `## P2c results` to the runbook (screenshots, the manifest as created, transcript verbatim); README status line gains Slack; commit: `P2c results: slack connect live`

---

## Self-review notes (applied)

- Spec coverage: "Slack integration (manifest-API app creation + 'Finish Slack setup' card)" — Tasks 2/3/5; Socket Mode/no-ingress and the no-public-API trio (install, app token, icon) collapse into the card exactly as the spec's decision log describes. The runtime transport, manifest template, and scopes come from the runtime kit verbatim — nothing invented.
- Token safety is a global constraint with tests (absence asserted in error bodies AND pushed repo bytes), not a convention.
- Deliberately deferred: approver allowlists for reaction gates (the transport's own TODO), multi-channel, uninstall/disconnect flow, Slack app deletion on agent teardown (teardown itself is already out of scope), avatar upload automation (P2d generates the image; applying it to Slack stays manual per API reality).
- Reuses without duplication: P2b delivery machinery (chip + endpoint + sha-from-local-head), WireStep's ruamel-edit/commit/reprovision-free pattern (CI redeploys on push — no reprovision needed since no gojira.yaml *generator* fields change... CHECK: `secrets_from` IS a generator field — the deployment template renders envFrom from it, so the regenerated manifests must land. WireStep handles this by calling `gojira.reprovision` + wait-active after its push. Task 3's wire_slack therefore ALSO needs the reprovision call — CORRECTION applied to Task 3: after commit_push, call `gojira.reprovision(agent.gojira_app_id)` and poll `app_status` active (reuse WireStep's `_wait_active` helper — import it or extract; the returned sha still drives delivery tracking, which now watches the regeneration-inclusive CI). Task 3's tests must assert the reprovision call is made after a changed push and skipped when commit_push returns False.
- P2 remaining after this: P2d avatars (decision: best Vertex model-garden image endpoint, enable if needed), Archive/Library extraction (unscheduled).

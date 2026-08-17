# Slack Two Lanes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Slack tab opens to every user with an editable multi-channel lane, while tokens collapse into an operator-only setup lane; wiring writes one slack transport per channel and stays honest about whose move it is.

**Architecture:** Channel lists become room-database state (JSON column on the agent row) so the two lanes can arrive in either order; a `slack_wired` flag plus the list derives the state line. `wire_slack` grows a channels list and writes one transport entry per channel (the runtime already builds one SlackTransport per entry and filters events per channel — verified in the runtime source, `agent.py` transports loop and `transports/slack.py` channel guard). Channel edits after wiring ride a commit-only re-wire that returns a sha for the existing delivery chips.

**Tech Stack:** FastAPI + SQLAlchemy (SQLite) on the API; Svelte 5 + TanStack svelte-query + the jsdom rig on the UI.

## Global Constraints

- NO em dashes in user-facing copy, INCLUDING API error bodies. The connect route's existing "couldn't set up Slack — try again" is swept by this plan (Task 2) and a guard test keeps API error literals clean.
- NO vendor/client names in tracked files; scan list untracked at `.superpowers/sdd/vendor-names.txt`; the vendor scan is its OWN step gating every commit.
- Tokens are NEVER stored in the room database and NEVER echoed back in any response body (the existing routes' discipline; preserve it verbatim).
- REAL-GATE RULE: channel validation uses the existing `SLACK_CHANNEL_RE` (main.py) as the single source; the transport entry shape is exactly what the runtime consumes: `{"kind": "slack", "channel": "C..."}`, one entry per channel (runtime `agent.py` slack branch reads `options["channel"]`, one transport per entry).
- Channel list cap: 8, deduplicated preserving first occurrence, order preserved. Rationale: each channel is one Socket Mode connection; the Slack app-token limit is about 10. Cap copy: "Eight channels at most for now."
- TDD RED-first per behavior. Baselines: API suite 1266 passed, UI 288 passed, `npm run check` 0, `npm run build` green. Commands: API from `api/` with `venv/bin/python -m pytest -q`; UI from `ui/` with `npm test`.
- Task 1 amends the spec with the cap (one sentence, shown in Task 1) in the same commit as the schema change.

## File Structure

- `api/kobo_api/models.py` — two new Agent columns (`slack_channels` JSON text, `slack_wired` bool) + manual-ALTER docstring notes (house idiom from the `team` column).
- `api/kobo_api/slack_state.py` (new) — pure helpers: encode/decode/validate channel lists, derive the state string. Small on purpose; routes and UI both lean on it through the payload.
- `api/kobo_api/slack_setup.py` — `_update_agent_yaml_transports` (list version), `wire_slack(channels=...)`, new `rewire_slack_channels(...)` commit-only path.
- `api/kobo_api/main.py` — new PUT channels route; connect route slims to tokens; agent payload's `slack` dict and visibility flag change.
- `api/tests/test_slack_state.py` (new), `api/tests/test_web_slack.py` (extend), `api/tests/test_slack_setup.py` (extend), `api/tests/test_copy.py` (new, API error-literal guard).
- `ui/src/lib/tabs.ts` — Slack tab becomes unconditional.
- `ui/src/lib/api.ts` + `ui/src/lib/queries.ts` — payload types, `saveSlackChannelsMutation`, slimmed connect mutation.
- `ui/src/components/SlackPanel.svelte` — the two-lane rework.
- `ui/src/pages/AgentDetail.svelte` — tab list call site, `slack_admin_visible` pass-through.
- UI tests: `tabs.test.ts`, `queries.test.ts`, `SlackPanel.test.ts` (new component test), `AgentDetail.test.ts` fixtures.

---

### Task 1: Channel storage + pure state helpers (API core)

**Files:**
- Modify: `api/kobo_api/models.py` (Agent columns, near the existing `slack_channel` at ~line 66)
- Create: `api/kobo_api/slack_state.py`
- Test: `api/tests/test_slack_state.py`
- Modify: `docs/superpowers/specs/2026-08-17-slack-two-lane-design.md` (cap amendment)

**Interfaces:**
- Consumes: nothing new.
- Produces (contract for Tasks 2-3):
  - `Agent.slack_channels: str | None` (JSON-encoded list) and `Agent.slack_wired: bool` (default False).
  - `slack_state.decode_channels(raw: str | None) -> list[str]`
  - `slack_state.encode_channels(channels: list[str]) -> str`
  - `slack_state.clean_channels(channels: list[str]) -> list[str] | str` — returns the deduped, order-preserving list, or a plain error string on the first invalid entry / cap breach. Validation delegates to `main.SLACK_CHANNEL_RE`... no: to avoid an import cycle, `SLACK_CHANNEL_RE` MOVES into `slack_state.py` and `main.py` imports it from there (single source preserved, direction reversed). Grep main.py for both uses (create/connect) and update the imports.
  - `slack_state.derive_state(channels: list[str], wired: bool) -> str` returning exactly one of `"idle" | "waiting" | "connected"`.
- The cap constant: `MAX_CHANNELS = 8`, error copy exactly `"Eight channels at most for now."`.

- [ ] **Step 1: Spec cap amendment** — append to the Channels lane section of the spec:

```markdown
- At most eight channels per agent for now (each channel holds one
  Socket Mode connection and the app token allows about ten).
```

- [ ] **Step 2: Failing tests `api/tests/test_slack_state.py`**

```python
from kobo_api.slack_state import (
    MAX_CHANNELS,
    SLACK_CHANNEL_RE,
    clean_channels,
    decode_channels,
    derive_state,
    encode_channels,
)


def test_decode_none_is_empty():
    assert decode_channels(None) == []


def test_encode_decode_round_trip():
    channels = ["C0123456789", "C987654321A"]
    assert decode_channels(encode_channels(channels)) == channels


def test_clean_dedupes_preserving_first_occurrence_and_order():
    got = clean_channels(["C0123456789", "C987654321A", "C0123456789"])
    assert got == ["C0123456789", "C987654321A"]


def test_clean_rejects_bad_id_with_plain_reason():
    got = clean_channels(["notachannel"])
    assert isinstance(got, str)
    assert "channel ID" in got


def test_clean_rejects_over_cap():
    ids = [f"C00000000{i:02d}" for i in range(MAX_CHANNELS + 1)]
    assert clean_channels(ids) == "Eight channels at most for now."


def test_clean_rejects_empty_list():
    got = clean_channels([])
    assert isinstance(got, str)


def test_derive_state_matrix():
    assert derive_state([], wired=False) == "idle"
    assert derive_state(["C0123456789"], wired=False) == "waiting"
    assert derive_state(["C0123456789"], wired=True) == "connected"
    assert derive_state([], wired=True) == "waiting"
```

(The last case: wired with zero channels means the list was emptied after
connection — transports are gone, so the truthful state is waiting, not
connected. This is the spec's remove-to-zero rule.)

Run: `venv/bin/python -m pytest tests/test_slack_state.py -q` — FAIL (module missing).

- [ ] **Step 3: Implement `api/kobo_api/slack_state.py`**

```python
"""Channel-list plumbing for the two-lane Slack flow.

Pure helpers only: JSON encode/decode for the Agent column, validation
with plain copy, and the little state machine the payload reports. The
routes own persistence; the UI owns rendering; this module owns truth.
"""
import json
import re

# Single source for channel-ID shape; main.py imports it from here.
# Slack has migrated private channels to C ids, so C-prefixed is enough.
SLACK_CHANNEL_RE = re.compile(r"^C[A-Z0-9]{6,}$")

MAX_CHANNELS = 8
_CAP_MESSAGE = "Eight channels at most for now."


def decode_channels(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [str(entry) for entry in json.loads(raw)]


def encode_channels(channels: list[str]) -> str:
    return json.dumps(channels)


def clean_channels(channels: list[str]) -> list[str] | str:
    """The deduped, order-preserving list, or a plain error string."""
    if not channels:
        return "Add at least one channel."
    seen: list[str] = []
    for entry in channels:
        value = entry.strip()
        if not SLACK_CHANNEL_RE.match(value):
            return "that doesn't look like a channel ID (it starts with C)"
        if value not in seen:
            seen.append(value)
    if len(seen) > MAX_CHANNELS:
        return _CAP_MESSAGE
    return seen


def derive_state(channels: list[str], *, wired: bool) -> str:
    if channels and wired:
        return "connected"
    if channels or wired:
        return "waiting"
    return "idle"
```

Note `derive_state` is keyword-only on `wired`; update the test calls to
`derive_state([], wired=False)` style (as written above).

- [ ] **Step 4: Agent columns in `api/kobo_api/models.py`** — beside the existing slack fields:

```python
    # Two-lane Slack (2026-08-17): the saved channel list (JSON array of
    # C ids) and whether wiring has completed at least once. Existing DBs
    # need manual ALTERs (fresh DBs get them from create_all):
    #   ALTER TABLE agents ADD COLUMN slack_channels VARCHAR;
    #   ALTER TABLE agents ADD COLUMN slack_wired BOOLEAN DEFAULT 0;
    # `slack_channel` (singular) above is dormant after this change: kept
    # for history, no longer written.
    slack_channels: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    slack_wired: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
```

(Check the file's existing imports for `Boolean`; add it to the
sqlalchemy import line if absent.)

- [ ] **Step 5: main.py re-points at the moved regex** — replace main.py's `SLACK_CHANNEL_RE = re.compile(...)` definition with `from kobo_api.slack_state import SLACK_CHANNEL_RE` (keep every existing use site compiling; grep for `SLACK_CHANNEL_RE` to confirm both routes resolve).

- [ ] **Step 6: GREEN + full suite**

Run: `venv/bin/python -m pytest tests/test_slack_state.py -q` (PASS), then `venv/bin/python -m pytest -q` (expect 1266 + 8 new).

- [ ] **Step 7: Vendor scan (own step), commit**

```bash
grep -i -f ../.superpowers/sdd/vendor-names.txt kobo_api/slack_state.py kobo_api/models.py kobo_api/main.py tests/test_slack_state.py ../docs/superpowers/specs/2026-08-17-slack-two-lane-design.md || echo clean
git add kobo_api/slack_state.py kobo_api/models.py kobo_api/main.py tests/test_slack_state.py ../docs/superpowers/specs/2026-08-17-slack-two-lane-design.md
git commit -m "feat(api): channels become room state with a tiny truth machine"
```

---

### Task 2: Transports go plural in slack_setup

**Files:**
- Modify: `api/kobo_api/slack_setup.py` (`_update_agent_yaml_transport` ~line 181, `wire_slack` ~line 229)
- Test: `api/tests/test_slack_setup.py` (extend)

**Interfaces:**
- Consumes: nothing from Task 1 (this layer takes plain lists).
- Produces (contract for Task 3):
  - `_update_agent_yaml_transports(agent_yaml_path: Path, channels: list[str]) -> None` — replaces ALL existing `kind: slack` entries with one `{"kind": "slack", "channel": c}` per channel (order preserved), leaves every other transport untouched; an empty list removes all slack entries.
  - `wire_slack(..., channels: list[str], ...) -> str` (parameter renamed from `channel: str`; all else unchanged).
  - `rewire_slack_channels(github: GitHubClient, *, repo: str, channels: list[str]) -> str` — commit-only: clone/edit `bundle/agent.yaml` via the same workdir helpers `wire_slack` uses, commit with message `"kobo: slack channels updated"`, push, return the commit sha. No secrets, no reprovision, no kubectl.

- [ ] **Step 1: Failing tests (extend `api/tests/test_slack_setup.py`, following its existing tmp-path yaml idioms)**

```python
def test_transports_one_entry_per_channel_preserving_others(tmp_path):
    path = tmp_path / "agent.yaml"
    path.write_text(yaml.safe_dump({
        "transports": [
            {"kind": "a2a", "port": 9200},
            {"kind": "slack", "channel": "COLD000001"},
        ]
    }, sort_keys=False))
    _update_agent_yaml_transports(path, ["CNEW0000001", "CNEW0000002"])
    data = yaml.safe_load(path.read_text())
    kinds = [t["kind"] for t in data["transports"]]
    assert kinds == ["a2a", "slack", "slack"]
    assert [t["channel"] for t in data["transports"] if t["kind"] == "slack"] == [
        "CNEW0000001",
        "CNEW0000002",
    ]


def test_transports_empty_list_removes_slack_only(tmp_path):
    path = tmp_path / "agent.yaml"
    path.write_text(yaml.safe_dump({
        "transports": [
            {"kind": "a2a", "port": 9200},
            {"kind": "slack", "channel": "COLD000001"},
        ]
    }, sort_keys=False))
    _update_agent_yaml_transports(path, [])
    data = yaml.safe_load(path.read_text())
    assert [t["kind"] for t in data["transports"]] == ["a2a"]
```

Plus a `rewire_slack_channels` test using the file's existing fake
GitHub/workdir seam: assert it commits exactly `bundle/agent.yaml`, the
returned sha is the fake's, and NO kubectl/gojira fake was touched.
And update the existing `wire_slack` tests' calls to
`channels=["C..."]`, with one asserting two channels produce two
transport entries in the pushed yaml.

Run: RED (rename/signature errors).

- [ ] **Step 2: Implement** — `_update_agent_yaml_transports`:

```python
def _update_agent_yaml_transports(agent_yaml_path: Path, channels: list[str]) -> None:
    """Replace every slack transport with one entry per channel (order
    preserved), leaving all other transports exactly as found. An empty
    list disconnects: no slack entries remain."""
    data = yaml.safe_load(agent_yaml_path.read_text()) or {}
    transports = [t for t in data.get("transports", []) if t.get("kind") != "slack"]
    transports.extend({"kind": "slack", "channel": channel} for channel in channels)
    data["transports"] = transports
    agent_yaml_path.write_text(yaml.safe_dump(data, sort_keys=False))
```

`wire_slack`: rename the parameter, pass the list through, keep the
secret/reprovision mechanics byte-identical. `rewire_slack_channels`:
the same clone-edit-commit-push helper path `wire_slack` uses, minus
secrets/kubectl/reprovision (read `wire_slack`'s body first and reuse
its private helpers rather than duplicating them).

- [ ] **Step 3: GREEN + full suite** (expect prior count + new tests; zero failures).

- [ ] **Step 4: Vendor scan (own step), commit**

```bash
grep -i -f ../.superpowers/sdd/vendor-names.txt kobo_api/slack_setup.py tests/test_slack_setup.py || echo clean
git add kobo_api/slack_setup.py tests/test_slack_setup.py
git commit -m "feat(api): one transport per channel, and a commit-only rewire"
```

---

### Task 3: Routes — the channels lane, the slimmed connect, the honest payload

**Files:**
- Modify: `api/kobo_api/main.py` (SlackConnectRequest ~191, connect route ~1986, agent payload ~1134, new PUT route beside connect)
- Test: `api/tests/test_web_slack.py` (extend), Create: `api/tests/test_copy.py`

**Interfaces:**
- Consumes: Task 1 (`decode_channels`, `encode_channels`, `clean_channels`, `derive_state`, `SLACK_CHANNEL_RE` import move), Task 2 (`wire_slack(channels=...)`, `rewire_slack_channels`).
- Produces (contract for Task 4's UI):
  - `PUT /api/agents/{name}/slack/channels` body `{"channels": ["C..."]}`: NO operator gate (IAP middleware still applies); `_ready_agent_or_error`; `clean_channels` errors → 422 `{"error": <plain reason>}`; stores; when `agent.slack_wired` and the cleaned list is non-empty → `rewire_slack_channels`, response `{"channels": [...], "state": ..., "sha": <sha>}`; when wired and the list is EMPTY → also rewire (disconnect commit), state returns "waiting"; when not wired → `{"channels": [...], "state": ...}` (no sha key).
  - `SlackConnectRequest` drops `channel`; connect requires `decode_channels(agent.slack_channels)` non-empty else 409 `{"error": "Add at least one channel first."}`; on success sets `agent.slack_wired = True` (keeps setting nothing into the dormant `slack_channel`).
  - The connect route's 502 copy becomes `"couldn't set up Slack. Try again."` (em dash swept).
  - Agent payload: `slack` becomes `{"app_id": ..., "channels": [...], "state": derive_state(...)}` when `slack_app_id` is not None, else `{"app_id": None, "channels": [...], "state": ...}` — ALWAYS present now (the UI tab is unconditional); `slack_visible` is REPLACED by `slack_admin_visible` (same `_slack_operator_block` derivation).

- [ ] **Step 1: Failing route tests (extend `api/tests/test_web_slack.py`, its existing client/fixture idiom)** — the matrix:

```python
# channels lane
def test_put_channels_stores_and_reports_waiting(...): ...      # no tokens yet: 200, state "waiting", no sha
def test_put_channels_rejects_bad_id(...): ...                  # 422, plain reason, nothing stored
def test_put_channels_rejects_over_cap(...): ...                # 422 "Eight channels at most for now."
def test_put_channels_dedupes(...): ...                         # response channels deduped
def test_put_channels_no_operator_gate(...): ...                # non-operator identity: 200
def test_put_channels_rewires_when_wired(...): ...              # wired agent: fake github got the commit, sha in response
def test_put_channels_empty_disconnects_when_wired(...): ...    # wired + []: rewire called with [], state "waiting"
# connect lane
def test_connect_requires_saved_channels(...): ...              # 409 "Add at least one channel first."
def test_connect_body_no_longer_takes_channel(...): ...         # old body shape: 422 from pydantic (extra ignored? assert the route uses stored list, not body)
def test_connect_sets_wired_and_payload_state_connected(...): ...
def test_agent_payload_always_carries_slack_block(...): ...     # app_id None case included
def test_slack_admin_visible_replaces_slack_visible(...): ...
```

Write each against the file's real fixtures (it has IAP-header helpers
and fake clients already — read them first). RED.

- [ ] **Step 2: The API copy guard `api/tests/test_copy.py`**

```python
"""API error copy stays plain: no em dashes in string literals that
reach users. Scans the JSONResponse error bodies in main.py by source
inspection, comments stripped."""
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "kobo_api" / "main.py"


def test_no_em_dashes_in_error_literals():
    stripped = re.sub(r"#[^\n]*", "", SOURCE.read_text())
    offenders = [
        line.strip()
        for line in stripped.splitlines()
        if "—" in line and ('"error"' in line or "error':" in line or '"detail"' in line)
    ]
    assert offenders == [], offenders
```

RED against current code (the connect 502 line), GREEN after the sweep.

- [ ] **Step 3: Implement the routes per the Produces block** — new request model:

```python
class SlackChannelsRequest(BaseModel):
    channels: list[str]
```

The PUT route body (shape, matching the house route idiom):

```python
    @app.put("/api/agents/{name}/slack/channels")
    def save_slack_channels(
        name: str,
        payload: SlackChannelsRequest,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(
            session, name, not_ready_message="agent is not ready for Slack changes yet"
        )
        if isinstance(agent, JSONResponse):
            return agent

        cleaned = clean_channels(payload.channels) if payload.channels else []
        if isinstance(cleaned, str):
            return JSONResponse({"error": cleaned}, status_code=422)

        agent.slack_channels = encode_channels(cleaned)
        body: dict = {
            "channels": cleaned,
            "state": derive_state(cleaned, wired=agent.slack_wired),
        }
        if agent.slack_wired:
            clients = app.state.clients
            try:
                sha = rewire_slack_channels(
                    clients.github, repo=agent.repo_full_name, channels=cleaned
                )
            except ClientError as exc:
                log.warning("slack rewire failed: %s", exc)
                return JSONResponse(
                    {"error": "couldn't update the channels. Try again."},
                    status_code=502,
                )
            body["sha"] = sha
        session.commit()
        return JSONResponse(body)
```

(Note the empty-list nuance: `payload.channels == []` skips
`clean_channels`' non-empty rule and stores `[]` — the disconnect path.
`clean_channels` keeps its "Add at least one channel." reply for the
CONNECT route's use; the PUT route allows empty deliberately. Encode
this exact distinction; the tests above pin both sides.)

Connect route edits and the payload block per Produces. The payload's
slack assembly becomes:

```python
        channels = decode_channels(agent.slack_channels)
        slack = {
            "app_id": agent.slack_app_id,
            "channels": channels,
            "state": derive_state(channels, wired=agent.slack_wired),
        }
```

with `"slack_admin_visible": _slack_operator_block(request, cfg) is None`
replacing the old `slack_visible` line.

- [ ] **Step 4: GREEN + full suite** (expect prior + ~13 new).

- [ ] **Step 5: Vendor scan (own step), commit**

```bash
grep -i -f ../.superpowers/sdd/vendor-names.txt kobo_api/main.py tests/test_web_slack.py tests/test_copy.py || echo clean
git add kobo_api/main.py tests/test_web_slack.py tests/test_copy.py
git commit -m "feat(api): channels for everyone, tokens for operators, one honest state"
```

---

### Task 4: The two-lane SlackPanel

**Files:**
- Modify: `ui/src/lib/tabs.ts` + `ui/src/lib/tabs.test.ts` (Slack unconditional)
- Modify: `ui/src/lib/api.ts` (payload types), `ui/src/lib/queries.ts` (+ `queries.test.ts`)
- Modify: `ui/src/components/SlackPanel.svelte` (rework), `ui/src/pages/AgentDetail.svelte` (call sites + fixtures in its test)
- Create: `ui/src/components/SlackPanel.test.ts`

**Interfaces:**
- Consumes: Task 3's payload (`slack: {app_id, channels, state}` always present; `slack_admin_visible`), routes (`PUT .../slack/channels`, tokens-only connect).
- Produces: `visibleTabs(): TabId[]` (no argument — Slack always last); `saveSlackChannelsMutation(queryClient, name)` posting `{channels}`; SlackPanel props become `{ agentName, slack, adminVisible, frozen }`.

- [ ] **Step 1: tabs first (RED then GREEN)** — `visibleTabs()` drops its parameter and always includes `"slack"`; update `tabs.test.ts` (two cases collapse into one) and the two call sites in `AgentDetail.svelte`. The AgentDetail test's `slack_visible` fixture fields become `slack_admin_visible`, and its "shows the Slack tab when slack_visible" test flips to assert Slack is present regardless.

- [ ] **Step 2: queries + types (RED then GREEN)** — `api.ts`: `AgentSlack = { app_id: string | null; channels: string[]; state: "idle" | "waiting" | "connected" }`; agent type carries `slack: AgentSlack` and `slack_admin_visible: boolean`. `queries.ts`: `saveSlackChannelsMutation` mirroring the existing mutation factories (PUT, invalidates `["agents", name]`); connect mutation's body type drops `channel`. Factory tests in `queries.test.ts` per the existing idiom (URL, method, body, invalidation).

- [ ] **Step 3: SlackPanel rework (component test RED first)** — `SlackPanel.test.ts` with the jsdom rig:

```ts
// @vitest-environment jsdom
// Cases:
// - idle state, non-admin: channels lane renders with one empty row and
//   "Add another channel" button; no token fields anywhere.
// - waiting state: status line "Channels saved. Waiting on an operator
//   to finish the connection."
// - admin (adminVisible: true), waiting: setup lane renders bot/app
//   token fields and Finish setup.
// - add-another adds a row; remove removes it; Save disabled while all
//   rows empty; frozen disables every control (rows, add, save, tokens).
// - connected state: status "Connected." and the channel list shown.
```

Then rework the component: channels editor (local `$state` rows seeded
from `slack.channels`, per-row remove, add-another, Save → mutation,
per-row/route error display), status line from `slack.state`, admin
lane behind `adminVisible` (existing create-app guidance + tokens-only
finish form), all controls carrying `disabled={frozen || ...}`. Copy is
the spec's state table verbatim; no em dashes.

- [ ] **Step 4: Full gates** — `npm test` (expect 288 + new), `npm run check` (0), `npm run build`; also confirm the FrozenPanels matrix still passes with the reworked panel (its SlackPanel case props gain `slack`/`adminVisible` — update the CASES entry accordingly).

- [ ] **Step 5: Vendor scan (own step), commit**

```bash
grep -i -f ../.superpowers/sdd/vendor-names.txt src/lib/tabs.ts src/lib/tabs.test.ts src/lib/api.ts src/lib/queries.ts src/lib/queries.test.ts src/components/SlackPanel.svelte src/components/SlackPanel.test.ts src/pages/AgentDetail.svelte src/pages/AgentDetail.test.ts || echo clean
git add <those files>
git commit -m "feat(ui): the slack tab opens to everyone, tokens keep their velvet rope"
```

---

### Task 5: Rollout + live acceptance

**Files:** none new — operational, plus the deployed DB ALTERs.

- [ ] **Step 1: Full gates both sides** — API suite, UI suite, check, build; counts recorded in the ledger.

- [ ] **Step 2: Deployed DB migration (BEFORE the roll)** — on the room's pod (the SQLite file on the PVC), run the two ALTERs plus the backfill so the one wired agent stays truthful:

```sql
ALTER TABLE agents ADD COLUMN slack_channels VARCHAR;
ALTER TABLE agents ADD COLUMN slack_wired BOOLEAN DEFAULT 0;
UPDATE agents SET slack_wired = 1,
                  slack_channels = '["' || slack_channel || '"]'
              WHERE slack_channel IS NOT NULL;
```

(Old code ignores unknown columns; new code needs them — ALTER-first is
the safe order, mirroring the models.py docstring idiom.)

- [ ] **Step 3: Build + roll per the untracked runbook** (client coordinates never enter tracked files).

- [ ] **Step 4: Live acceptance per the spec** — channels-first path on a scratch or existing agent: save two channels as the operator (the room's one human wears both hats; the gate matrix is proven by tests, the LANES are proven live), finish setup, the agent answers in BOTH channels; edit the list (add/remove), the delivery chip stays honest end to end; remove-to-zero returns the state line to waiting. Record every observation in the ledger.

- [ ] **Step 5: Ledger close** — arc summary line with commits, counts, and any follow-ups discovered live.

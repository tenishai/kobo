# Daily learnings, UI simplification, and agent vision

Three independent waves, each deployable on its own. Wave order reflects
operator priority: daily learnings first (context loss is happening every
day), UI simplification second, vision third.

---

## Wave 1: Daily learnings

### Problem

Agents hold conversations all day -- Slack threads, A2A exchanges, chat
sessions -- and the knowledge in those conversations evaporates when the
thread closes. The agent has no way to consolidate what it learned, and
the operator has no way to curate which facts deserve to persist.

Auto-learning is explicitly ruled out. Slack users (one agent, many
humans) can intentionally or accidentally poison a conversation with a
false fact. An unattended write into techne turns that poison into
permanent memory the agent trusts and cites. The teach lane's human gate
exists for exactly this reason; daily learnings extends that gate to
conversation-derived knowledge.

### Data flow

```
nightly (per ready agent)
  kobo  ──system_task: learnings──>  agent pod
                                       |
                                  runtime reads its own
                                  checkpoint files (Slack
                                  threads, A2A tasks, chat)
                                  from the last 24h
                                       |
                                  resident model summarizes
                                  into candidate snippets
                                  (its own persona, its own
                                  context, its own tokens)
                                       |
                                  <──response──  candidates[]
                                       |
                                  kobo upserts into
                                  agent_learnings table
                                       |
                                  owner/grantee reviews
                                  in the UI (Learnings tab)
                                       |
                              approve ──>  teach lane
                                           (intake commit +
                                            techne store,
                                            same as "Teach
                                            this" gesture)
                              deny ──>  row marked denied,
                                        content_hash remembered,
                                        never re-proposed
```

### Candidate format

Each candidate snippet returned by the agent:

| field      | type   | constraint              | example |
|------------|--------|-------------------------|---------|
| content    | string | max 400 chars           | "The playback team's canary deploy pipeline requires a manual gate after the smoke tests pass -- automated promotion was disabled after the 2026-06-15 incident." |
| kind       | string | `fact` or `procedure`   | `fact` |
| source     | string | plain-language origin    | "Slack thread in #sre with jtremblay, Aug 31" |
| evidence   | string | max 200 chars, verbatim excerpt from the conversation that supports the candidate | "jtremblay: yeah we turned off auto-promote after that outage, manual gate now" |

The agent returns at most **10 candidates per nightly run**. The prompt
instructs it to prefer quality over quantity, skip anything it was
explicitly taught (already in techne), and skip ephemeral operational
state ("deploy is running right now").

### Database: `agent_learnings` table (kobo Postgres)

New SQLAlchemy model in `kobo_api/models.py`. Follows the same
no-ALTER-needed convention as `RepoAccess`, `MCPRefreshToken`, and every
other table that postdates the initial schema: `Base.metadata.create_all`
creates it for free.

| column        | type         | notes |
|---------------|--------------|-------|
| id            | Integer PK   | auto |
| agent_name    | String(100)  | not null |
| day           | String(10)   | ISO date of the nightly run (`YYYY-MM-DD`) |
| content       | Text         | the candidate snippet |
| kind          | String(16)   | `fact` or `procedure` |
| source        | String(512)  | plain-language origin |
| evidence      | Text         | verbatim excerpt |
| content_hash  | String(64)   | SHA-256 of `content`, for dedupe |
| status        | String(10)   | `pending`, `approved`, `denied`, `expired` |
| decided_by    | String(255)  | nullable, email of approver/denier |
| decided_at    | DateTime     | nullable |
| created_at    | DateTime     | default now(UTC) |

Unique constraint on `(agent_name, content_hash)`: a denied snippet's
hash blocks re-proposal; the agent can rephrase, but the identical
content never comes back. Expired rows are never re-proposed either
(the hash stays).

### Nightly job

A background task in kobo's FastAPI lifespan, managed the same way the
existing catalog-refresh loop runs: an `asyncio.create_task` in the
lifespan that sleeps until the next UTC midnight, then iterates all
ready agents. "Last run" is tracked per-agent by the most recent
`created_at` in `agent_learnings` for that agent (no separate state
table needed -- an agent with zero rows has never been summarized and
gets its first run immediately).

For each agent with status `ready`:
1. Build a `system_task: learnings` A2A message through the existing
   switchboard relay (same transport `teach` and `knowledge` already use).
2. The message payload carries the current UTC date. Dedupe against
   already-proposed content happens entirely on the kobo side (the
   upsert's hash check below) -- a hash list would be meaningless to
   the summarizing model.
3. On response, validate each candidate against the format above (max 10,
   field lengths, required fields). Drop malformed candidates silently.
4. Upsert into `agent_learnings`: skip any row whose `content_hash`
   already exists (regardless of status).
5. On relay failure (agent unreachable, timeout, malformed response):
   log a warning and move on. Never crash the nightly run.

### Agent GET response addition

`GET /api/agents/{name}` gains a `pending_learnings` integer field
(default 0) -- the count of `agent_learnings` rows with
`status = pending` for this agent. The UI reads this for the Learnings
tab badge. Computed as a simple `COUNT(*)` in the existing agent-detail
query, not a separate round-trip.

### Daedalus runtime handler

New handler in `daedalus/memory/teach.py` (or a sibling `learnings.py`),
dispatched from `Agent.handle_task` when
`task.metadata["system_task"] == "learnings"`.

**Conversation journal (new, required):** task checkpoints cannot be
the conversation source -- the runtime drops a task's checkpoint row
the moment the task completes, and checkpoints carry no timestamps or
list API. Wave 1 adds a small journal: on every normal task completion,
the agent appends one row (`context_id`, the task's message history as
JSON, an ISO timestamp) to a `journal.db` SQLite file living next to
the techne db on the agent's persistent volume. Rows older than 7 days
are pruned on write. Journaling is best-effort: a journal failure never
fails the task.

The handler:
1. Reads the journal rows from the last 24h. If there are none, it
   returns an empty candidates list immediately -- no model call, so a
   boot-time catch-up run against a quiet agent costs nothing.
2. Builds a summarization prompt for the agent's own model (the same
   model the agent uses for regular tasks, accessed through the same
   `LiteLLMClient`). The prompt:
   - Presents the day's conversations (or a representative sample if
     the total exceeds the model's context budget -- truncate oldest
     first, never newest).
   - Asks for up to 10 candidate memory snippets, each a durable fact
     or procedure the agent learned today that it does not already know
     (the existing techne knowledge is included as "what you already
     know" context via the same `recall()` the agent uses at task time).
   - Instructs the model to return structured JSON matching the candidate
     format above.
3. Parses the model's response, validates the JSON, and returns the
   candidates array as the task's reply text (JSON-encoded in the A2A
   response body).

### Approval flow

When an owner, grantee, or operator approves a candidate:

1. `POST /api/agents/{name}/learnings/{id}/approve`
2. The route calls the **existing teach lane** (`kobo_api.teach.format_note`
   + `kobo_api.intake.upload_intake_file`) with the candidate's content,
   `taught_by` set to the approver's email, and a distinct source prefix:
   `intake:learning-<date>`.
3. The teach lane pushes the note into `bundle/intake/*` and relays it to
   the live agent as a `system_task: teach` message -- the agent writes
   it into techne immediately, same as the "Teach this" gesture.
4. The `agent_learnings` row is updated: `status = approved`,
   `decided_by`, `decided_at`.

Deny:
1. `POST /api/agents/{name}/learnings/{id}/deny`
2. Row updated: `status = denied`, `decided_by`, `decided_at`.
3. The `content_hash` stays in the table. Future nightly runs skip it.

### Expiration

Pending rows older than **14 days** expire automatically. The nightly job
marks them `status = expired` before generating new candidates. Their
`content_hash` remains, so the exact same content is never re-proposed.
The 14-day window keeps the review queue livable; an operator who hasn't
looked in two weeks isn't going to start now, and re-proposing stale
content would just re-fill a queue nobody is reading.

### Auth gate

Approve/deny/bulk: the caller must be the agent's owner, an operator,
or a grantee (an `AgentUserGrant` row for this agent). This is the
existing `_owner_or_operator_or_403` gate widened by the grant check --
NOT the teach gate (teach is deliberately open to any signed-in caller
who can see the agent, which would hand approval power to readers,
violating the reader-role law that every mutation gate answers readers
exactly as it answers anyone else).

The GET list is visibility-gated only (`_visible_agent_or_404`), so
readers can see the queue. The agent detail GET carries a
`can_decide_learnings` boolean (computed server-side by the same
decide gate, following the `destroy_visible` precedent) so the UI can
hide Approve/Deny without ever inferring roles client-side.

### API routes

| method | path | auth | notes |
|--------|------|------|-------|
| GET    | `/api/agents/{name}/learnings` | any caller who can see the agent (readers included) | list candidates, filterable by `status` (default: `all` -- the panel splits pending from history in one fetch), ordered by `created_at` desc |
| POST   | `/api/agents/{name}/learnings/{id}/approve` | owner/grantee/operator | approve and push through teach lane |
| POST   | `/api/agents/{name}/learnings/{id}/deny` | owner/grantee/operator | deny and remember hash |
| POST   | `/api/agents/{name}/learnings/bulk` | owner/grantee/operator | approve/deny multiple by id list in one request (the UI's "approve selected" gesture) |

### UI: Learnings tab

Lives in the Advanced group (see Wave 2). Badge count on the tab label
when `pending > 0`. Day-grouped cards:

```
--- Aug 31 ---
[fact] The playback team's canary deploy pipeline requires     [Approve] [Deny]
       a manual gate after the smoke tests pass...
       Source: Slack thread in #sre with jtremblay, Aug 31
       Evidence: "jtremblay: yeah we turned off auto-promote
       after that outage, manual gate now"

[procedure] When a litestream replica falls behind by more     [Approve] [Deny]
            than 10 minutes, the correct recovery is...
            Source: A2A conversation with ops-sentinel, Aug 31
```

A "Select all" checkbox per day group for bulk approve/deny. Approved
and denied rows move to a collapsed "History" section at the bottom,
filterable.

### Rider bugfix: skills ledger reconciliation

Found 2026-09-01 (Leslie, agent-clio PR #4): skills merged directly into
an agent's repo (`bundle/skills/<id>/`) are live on the agent -- the
bundle is the runtime truth and the seeder ingests them on rebuild --
but invisible in kobo, because `GET /api/agents/{name}/skills` is
"purely a DB-column read" and the `agent.skills` ledger only hears about
kobo's own routes. The direct-PR lane is legitimate; the ledger drifts.

Fix: reconcile on read. The route lists the repo's `bundle/skills/`
directories (the same clone pattern every skills.py function uses) and
merges: repo dir with a DB row keeps the DB's provenance; repo dir with
no row surfaces as `local`/editable and is written back into the ledger;
a DB row with no repo dir is pruned (removed by PR = not on the agent);
a repo read failure degrades to today's DB-only read and never prunes.

Rides the wave 1 branch as its own task and commit; independent of the
learnings feature.

### Out of scope (wave 1)

- Slack notifications when new learnings are pending (registered
  follow-up).
- Folding `colleagues.remember` (agent-to-agent direct writes) into
  this approval queue (registered follow-up -- requires a quorum
  protocol change in cabal).
- Editing a candidate's text before approving (the operator can deny
  and teach the corrected version manually via the existing Teach
  gesture).

---

## Wave 2: UI simplification

### Problem

The agent detail page shows seven tabs to every visitor on first load.
Most of them are irrelevant until the agent is wired up and working --
a new owner looking at their freshly-created agent sees Memory, Skills,
Tooling, and Access before they've even said hello. The create modal
asks about skills before the agent exists. The getting-started guide
describes infra concepts no agent owner needs to think about.

### Agent detail page layout

**Identity header (replaces the current Overview tab's persona card):**
Compact: agent name, status badge, model, persona one-liner, portrait.
The provisioning checklist renders inline below the header only while
at least one step is not green, then collapses to a "View build log"
link. The Overview tab is **removed** -- its content is promoted to the
always-visible header.

**Primary tabs (always visible):** Chat, Slack, Access

**Advanced section:** a collapsible group (collapsed by default for
non-owners, expanded for owners) containing: Learnings (wave 1),
Memory, Skills, Tooling

The tab model in `ui/src/lib/tabs.ts`:
- `TabId` gains `"learnings"`.
- `visibleTabs` splits into `primaryTabs` (chat, slack, access) and
  `advancedTabs` (learnings, memory, skills, tooling).
- The `"overview"` tab ID is retired. `defaultTab` returns `"chat"`
  for ready agents, `"slack"` for non-ready (since the header now
  shows the checklist, Slack is where the owner goes next to wire
  things up).

**Tab labels change:** `TAB_LABELS` updates. Learnings shows a badge
when pending count > 0 (the `pending_learnings` field on
`GET /api/agents/{name}`, added in wave 1).

### Create modal

The skills step is **removed entirely**. The create modal flow becomes:
name, persona, model, Slack. Fewer fields, faster to complete.

The skills combobox (recommended strip + searchable dropdown) moves
intact into the **Skills tab** as the add-skills control, post-create.
The component (`SkillsPanel.svelte`) already exists; the combobox
relocates there.

### Auto-assigned tooling ruling

All three auto-assigned tool families stay:
- `colleagues.list` / `colleagues.ask`: power the switchboard and
  consult-first doctrine. Removing them breaks the room's social fabric.
- `colleagues.remember`: quorum-protected. Folding it into the
  learnings approval queue is a registered follow-up, not this wave.
- `github.*` sidecar: inert until an operator approves a repo, so it
  costs nothing on day one.

The fix for "tooling feels imposing" is moving the Tooling tab behind
Advanced, not stripping capability. The Tooling tab's empty state gets
a one-paragraph explanation of what each tool family does and why it's
there.

### Getting-started guide rewrite

`pages/GettingStarted.svelte` is rewritten as a linear walk-through in
the order a new user actually lives it:

1. **Create** -- name it, give it a persona, pick a model
2. **Say hello** -- open Chat, send a message, see it think and reply
3. **Wire Slack** -- connect a channel, @-mention it there
4. **Grant access** -- invite teammates, approve colleague requests
5. **Grow it** (collapsed by default) -- teach, skills, learnings

Each step links to the real control it describes (deep-link to the
relevant tab on the agent's detail page). The Advanced sections each
get a one-paragraph "what this is for" empty-state card so the page
teaches itself without a separate guide.

### Out of scope (wave 2)

- Changing the sidebar or dashboard layout.
- Renaming existing personas in the catalog.
- The marketplace persona-authoring skill republish (separate lane).

---

## Wave 3: Agent vision (Slack image handling)

### Problem

Users upload screenshots, diagrams, error traces, and photos in Slack
conversations with their agent. Today the agent sees the accompanying
text but the image is silently dropped -- the entire Slack-to-model
pipeline is text-only.

### Constraints

- Every model in the org config supports vision (Claude Sonnet 4.5,
  Gemini 3.1 Pro, Gemini 3.5 Flash via Vertex).
- litellm passes multimodal content blocks through to Vertex without
  changes.
- The gap is entirely in daedalus's own plumbing: event parsing, data
  model, and message composition.

### Changes

**1. Slack event handler** (`daedalus/transports/slack.py`, `handle_event`)

After extracting `text` from the event, check for `event.get("files")`.
For each file:
- Filter to image mimetypes (`image/png`, `image/jpeg`, `image/gif`,
  `image/webp`). Skip non-image files with a text note appended to the
  message: "(attached file `{name}` skipped -- not an image)".
- Enforce per-image size cap: **20 MB** (`file["size"]` from the Slack
  event payload, checked before download). Skip oversized images with
  a text note: "(image `{name}` skipped -- exceeds 20 MB limit)".
- Enforce per-message image cap: **4 images**. Process the first 4,
  skip the rest with a text note: "(N additional images skipped --
  max 4 per message)".
- Download the image bytes from `file["url_private"]` using the bot
  token (`Authorization: Bearer {SLACK_BOT_TOKEN}`) via an async HTTP
  GET. The `SlackApi` class gains a `download_file(url) -> bytes`
  method for this.
- Base64-encode the bytes. Attach as a content part on the `Message`.

**2. Message data model** (`daedalus/task.py`)

`Message` gains an optional `images` field:

```python
class ImagePart(BaseModel):
    data: str       # base64-encoded
    media_type: str # e.g. "image/png"

class Message(BaseModel):
    role: Literal["user", "agent"]
    text: str
    images: list[ImagePart] | None = None
```

`ImagePart` is a minimal carrier. `data` is the raw base64 string
(no `data:` URI prefix -- the LLM message builder adds the provider's
expected wrapper). `media_type` is the MIME type from the Slack file
metadata.

The `images` field defaults to `None` (not `[]`) so every existing
codepath that constructs `Message(role=..., text=...)` continues to
work without changes. Serialization to/from task checkpoints gets the
field for free via pydantic.

**3. LLM message composition** (`daedalus/agent.py`, `_messages`)

When a `Message` has `images`, emit `content` as a list of parts
instead of a flat string:

```python
if message.images:
    parts = [{"type": "text", "text": message.text}]
    for img in message.images:
        parts.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:{img.media_type};base64,{img.data}",
            },
        })
    messages.append({"role": role, "content": parts})
else:
    messages.append({"role": role, "content": message.text})
```

This is the OpenAI-compatible multimodal format that litellm expects
for all providers (Vertex/Anthropic/OpenAI). litellm handles the
provider-specific translation.

**4. A2A transport**

Wave 3 does NOT add image support to the A2A transport. Agent-to-agent
image passing is a separate design question (encoding, bandwidth, and
whether the receiving agent's model even benefits from the image).
Registered as a follow-up.

**5. Checkpoint size**

Task checkpoints that include base64 image data will be larger. At
4 images x 20 MB each = 80 MB worst case per message, this could
produce very large checkpoint files. Mitigations:
- The 20 MB cap and 4-image cap bound the worst case.
- Checkpoints are already per-task (per Slack thread), not global.
- The learnings summarizer (wave 1) reads checkpoint text, not images
  -- it can skip `images` fields when loading checkpoints for
  summarization.
- A follow-up optimization (not this wave): strip base64 data from
  checkpoints after the model has processed the message, keeping only
  a placeholder note "(image was here)". This trades replay fidelity
  for disk, and is optional.

### Scopes

CORRECTION (2026-09-02, verified against `slack_setup.py`'s `_BOT_SCOPES`):
`files:read` is NOT in the current manifest. Wave 3 must add it, and a
scope addition -- unlike the tagged-only event change -- requires each
agent's Slack app to be reinstalled to its workspace after the manifest
re-apply. Existing agents' owners re-apply at api.slack.com; new agents
get it from the updated generator. No new event subscriptions are needed
(the `files` array already rides `app_mention`/`message.im` events).

### Out of scope (wave 3)

- Image generation (the agent producing images back to Slack).
- Non-image file types (PDFs, spreadsheets, code files). Each needs
  its own extraction/handling and is a separate feature.
- A2A image transport (registered follow-up).
- DM file handling -- the same code path applies to `message.im` and
  `message.mpim` events, which also carry `files`. No special handling
  needed; DMs work identically to channel mentions.

---

## Global constraints

- No vendor name in repo contents (the org name never appears in
  daedalus or kobo source, comments, or string literals).
- No em dashes in kobo repo content (enforced by `copy.test.ts`).
- The sequence law holds: a daedalus image rolls BEFORE any org.yaml
  key that the new image introduces.
- New tables use the `Base.metadata.create_all` convention (no manual
  ALTER needed at rollout).
- The nightly job and the Slack image handler both degrade gracefully:
  a failure in either never takes the agent or the room down.

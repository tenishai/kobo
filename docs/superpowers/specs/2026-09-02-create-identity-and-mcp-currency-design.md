# Create-flow identity polish + MCP currency

Ruled 2026-09-02, from live use of the create flow. Three parts, one wave:
persona-resonant name suggestions (three, not five), a generated face at
birth, and an MCP-surface refresh. Design approved in discussion; the
external-MCP/limenal arc is SEPARATE and parked (see
2026-09-02-external-mcp-discovery.md).

## 1. Persona-resonant names -- three of them

Today's suggestions repeat and do not resonate because of three
independent causes, each fixed here:

**Cause 1 -- thin signal.** The route only ever sees `who_they_are`
(<= 200 chars) and, standing in for `what_they_do`, the persona's <= 60
char catalog LABEL; the persona's real body (up to ~49K chars) never
reaches it.

Fix: `NameSuggestionsRequest` gains optional fields:

- `persona_id: str | None` -- set when the user picked a CATALOG persona.
  The server then looks the persona up itself (`get_entry` /
  read_marketplace_catalog lane) and uses the persona's REAL
  `what_they_do`, clipped to its first 2000 characters, plus its
  `purpose_tags`, ignoring the client-sent `what_they_do`.
- `what_they_do` max length rises 500 -> 2000: the persona-builder lane
  holds the full drafted body client-side and now sends its first 2000
  chars instead of the label.
- `exclude: list[str] = []` (<= 20 names) -- every name already offered
  in this create session, so "Offer more" genuinely offers more.
- `round: int = 0` (0-20) -- the offer-round counter, salted into the
  fallback ordering below.

**Cause 2 -- hardcoded theme.** The prompt names its own lexical universe
("craft workshop's bench... tools, materials, and quiet trades")
regardless of persona.

Fix: the prompt is rewritten to mine the persona:

```
You are naming a software agent. Its persona: {who}. What it does:
{what}. Its themes: {tags}.

Suggest exactly three short, lowercase, single-word candidate names that
RESONATE with this persona's character and domain. If the persona evokes
a public figure, a fictional character, a team, or a vivid world, draw
the names from that world -- nicknames, associated places, terms of art.
Otherwise draw from the persona's own domain vocabulary. Rules: each
name is one word, lowercase letters/digits/hyphens only, starting with a
letter; never an employer's or client's company name; never any of:
{exclude}. For each, give a reason of 8 words or fewer tying it to this
persona.

Respond with ONLY a strict JSON array of exactly three objects shaped
{"name": "...", "reason": "..."} -- no prose, no markdown fences,
nothing before or after the array.
```

(temperature stays 1.0; thinking stays off; maxOutputTokens 512.)

**Cause 3 -- deterministic fallback.** The 24-word pool pads shortfalls
in a persona-seeded but otherwise FIXED order, so a failed or colliding
model call re-serves identical names forever.

Fix: `SUGGESTION_COUNT` drops 5 -> 3 everywhere (namegen, prompt, UI
cards, tests). `finalize_suggestions` gains the `exclude` set (dropped
like taken/reserved) and a `round_salt: str` mixed into
`_deterministic_order`'s hash (the UI sends its offer-round counter), so
consecutive fallback rounds rotate through the pool instead of standing
by the same names. The fallback stays deterministic WITHIN a round (no
clock, no random) -- variety comes from the salt and the excludes.

**UI:** three radio cards instead of five. The modal accumulates every
name it has shown into `exclude` and bumps the round counter on each
"Offer more" click; both ride the query key so refetching is honest.
Manual naming ("name it yourself") is untouched. The catalog-pick
prefetch now sends `persona_id`; the builder lane sends the real body.

## 2. A face at birth

**AvatarStep** joins the provisioning pipeline immediately after
`RepoStep` (the repo must exist; nothing else need have run):

```
RepoStep, AvatarStep, OnboardStep, AccessStep, WireStep,
BuildStep, DeployStep, DirectoryStep, HelloStep
```

Checklist label: "Paint the portrait".

Behavior: generate the portrait with the enriched prompt below via the
existing `clients.image.generate` lane, push `bundle/avatar.png` with the
existing `set_avatar` (same clone/commit/push idiom, same 4 MiB cap), and
cache `Agent.avatar_png` exactly as the route does -- the detail header
renders the face as soon as the step lands, mid-provisioning.

**Best-effort by law:** any failure (model error, oversize, push failure,
image client unconfigured) marks the step SKIPPED with a one-line note,
never FAILED -- a face must never block an agent. The step is also
idempotent: an agent that already has `avatar_png` (retry path) skips.

**Enriched prompt** -- one shared `avatar_prompt(agent_name,
persona_description, purpose_tags)` used by BOTH the step and the
existing generate route (the detail page's "New face" gets the same
upgrade for free):

```
friendly square avatar portrait of a character named {agent_name};
persona: {persona_description}; themes: {tags, comma-joined};
the face and styling should feel like a "{agent_name}" -- let the name
and persona set the character's vibe; flat, minimal, centered, plain
background; no text in image.
```

The create modal gains NO new station -- the face just happens, and the
existing Upload-a-portrait / New face / Download lanes on the detail
page stay exactly as they are for anyone who wants their own image.

## 3. MCP currency refresh

The surface is 21 tools (7 read, 14 write); nothing is broken, but the
documentation lies about the count and the flagship feature is missing.

**Doc fixes (no behavior change):**
- `mcp/server.py` wiring comment: "four read tools... six write tools"
  -> the real counts, without enumerating (counts drift; say "the read
  tools and write tools registered by their modules").
- `mcp/tools/write.py:518` "eleven write tools" -> fourteen; the module
  docstring intro enumerates ALL fourteen (it currently omits
  `kobo_create_persona` and the three repo-access tools).
- `write.py` table (A) "seven agent-scoped tools" -> eight
  (`kobo_request_repo_access` rides the same `_agent_route_result`
  table).
- `kobo_create_agent` docstring drops the "same one-click create the
  room's own UI drives" parity claim: the UI no longer offers skills at
  create; the MCP tool still does, deliberately -- say so ("more capable
  at create time than the room's own modal, which adds skills
  post-create").

**`kobo_get_agent` alignment:** the returned card gains the fields the
HTTP detail already serves: `pending_learnings`, `can_decide_learnings`,
`skills` (name/team/source), `slack` (as served), `has_avatar`,
`repo_full_name`, `model_label`, `destroy_visible`.

**New tools -- the learnings trio (write.py):**
- `kobo_list_learnings(agent, status="all")` -> the candidate rows
  (id/day/content/kind/source/evidence/status/decided_by) plus
  `can_decide` -- readable by anyone who can see the agent, exactly like
  the GET route.
- `kobo_decide_learning(agent, learning_id, action)` -- action
  approve|deny; rides the same owner/operator/grantee gate and teach-lane
  delivery as the HTTP route (the tool calls the route helpers, never
  reimplements them).
- `kobo_decide_learnings_bulk(agent, ids, action)` -- the select-all
  gesture; <= 50 ids, per-id status codes returned verbatim.

All three wear the standard audit/redaction wrapper and count updates
ripple into the fixed docstrings (14 -> 17 write tools).

**Backlog, documented not built** (room capabilities with no MCP tool,
recorded here so expansion is a decision, not archaeology): knowledge
files (list/write/delete/delivery), tooling list/import/delete, threads,
grants list/add/revoke, invites list/requestable/decline/delete, destroy,
retry, avatar generate/upload, metadata PATCH/regenerate, skills
search/recommended, models/groups/teams catalogs, persona
archive/unarchive/draft/distill, org status, people search, availability,
directory sync.

## Out of scope

- The external-MCP/limenal arc (parked, its own discovery doc).
- Regenerating names/faces for EXISTING agents in bulk (owners can use
  "New face" and Edit details).
- Persona-body transport to the browser (the server-side persona_id
  lookup deliberately avoids shipping 49K bodies to the client).

## Global constraints

- No vendor/org name in repo contents; no em dashes in kobo content
  (copy.test.ts enforces .svelte).
- Suggestion count is exactly 3; exclude cap 20; what_they_do request cap
  2000; avatar caps unchanged (4 MiB generated, 2 MiB upload).
- AvatarStep failure semantics: SKIPPED, never FAILED; never blocks
  READY.
- The nightly-learnings, tabs, and skills-reconcile behavior shipped this
  week is untouched.

// Typed client over the kobo-api HTTP surface. Every function funnels its
// Response through `parse()`, which turns a non-2xx status into an ApiError
// (message taken from a JSON `{"error": "..."}` body when present, otherwise
// a readable fallback built from the status line) and turns a body that
// isn't valid JSON at all (an HTML proxy-error page, a plain-text 500 from
// some upstream that isn't kobo-api) into an ApiError too, instead of
// letting a raw SyntaxError leak out of `JSON.parse`.

export type AgentSummary = {
  name: string;
  status: string;
  model: string;
  model_label: string;
  created_at: string;
  has_avatar: boolean;
};

export type StepView = {
  name: string;
  label: string;
  status: string;
  error: string | null;
};

export type AgentDetail = AgentSummary & {
  persona: string;
  /** Raw `agent.skills` DB entries, verbatim: a catalog pick carries
   * `team`/`sha`; a locally-authored guardrail skill carries neither (just
   * `source: "local"`) — see `AgentSkill` (the richer, server-normalized
   * shape `GET /api/agents/{name}/skills` serves) for the shape both are
   * mapped into elsewhere in this room. */
  skills: { name: string; sha?: string; team?: string; source?: string }[];
  repo_full_name: string | null;
  first_reply: string | null;
  error: string | null;
  steps: StepView[];
  slack: { app_id: string; channel: string | null } | null;
  /** Whether the Slack panel should render at all — false when an operator
   * config block hides Slack connect from this room entirely (see the
   * server's `_slack_operator_block`). Independent of `slack` itself: a
   * room can have `slack_visible: false` and still carry `slack` state from
   * before the block was applied. */
  slack_visible: boolean;
  /** Whether the Danger zone should render at all, false when an operator
   * config block hides destruction from this room entirely (see the
   * server's `_operator_block`). Same discipline as `slack_visible`: the
   * route itself is the real enforcement boundary, this is UX only. */
  destroy_visible: boolean;
};

/** One catalog search result — a skill or a persona, both served by the
 * same shape (`GET /api/skills/search`, `GET /api/personas/search`). `sha`
 * is set for skills (their content's git blob sha); `builtin` is set for
 * personas (true for the shipped defaults, false/absent for user-created
 * ones). */
export type CatalogEntry = {
  id: string;
  name: string;
  description: string;
  team: string;
  tags: string[];
  sha?: string;
  builtin?: boolean;
};

/** The persona builder's form (Persona builder v2 addendum, then the
 * Persona v3 / teamless-create addendum) — posted verbatim as the body of
 * `POST /api/personas`. Four fields, matching the four fields the v3
 * preview actually shows: no `tags` (the server derives them from
 * name/who_they_are/what_they_do — `kobo_api.personas._derive_tags`), no
 * role/purpose/tone/tone_note (Rules absorbs what Boundaries used to), and
 * (as of the teamless-create addendum) no `team` — the create modal's Team
 * station is gone, so this is never sent; the server defaults the recorded
 * team to `"shared"` (`CreatePersonaRequest.team`) when the key is
 * omitted. */
export type PersonaForm = {
  name: string;
  who_they_are: string;
  what_they_do: string;
  rules: string[];
};

/** The Persona v3 "describe the job, approve the soul" draft step's result
 * (`POST /api/personas/draft`) — the same name/who_they_are/what_they_do
 * shape as `PersonaForm`'s first three fields, prefilled into the v2
 * preview for the operator to edit before Create. The server always
 * returns something creatable — a generator failure (unconfigured/erroring)
 * falls back to a deterministic, job-derived draft server-side
 * (`kobo_api.persona_draft.finalize_draft`) rather than failing the
 * request — so `draftPersona` below only ever rejects on a genuine
 * transport/route failure, never on "the generator was down". */
export type PersonaDraft = {
  name: string;
  who_they_are: string;
  what_they_do: string;
};

/** One name-suggestion card (create flow v2 "naming ceremony" addendum) —
 * the generator's best-effort candidate, or a pick from the deterministic
 * curated-pool fallback when the generator is down/unusable. `reason` is
 * already clamped to <=80 chars server-side (`kobo_api.namegen`'s
 * `_normalize_reason`) — nothing further to trim on this side. */
export type NameSuggestion = { name: string; reason: string };

export type OrgCheck = { id: string; label: string; ok: boolean; detail: string };

export type OrgStatus = { org: string | null; wired: boolean; checks: OrgCheck[] };

export type ChatMessage = { role: "user" | "agent"; text: string };

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/**
 * Human-readable message for a thrown error — `error.message` when there is
 * one, else `fallback`. Shared by every page/component that renders a failed
 * query or mutation (Dashboard, AgentDetail, CreateModal) so the copy and
 * the cleanup below are applied uniformly instead of each callsite rolling
 * its own.
 *
 * Quoted tokens in the message have their surrounding quote marks stripped
 * for display — a backend message quoting a name as `'foo-bar'` (or
 * `"foo-bar"`) reads as `foo-bar`. This is token-scoped (a matched pair of
 * quotes, not every quote character): the single-quote pass only opens a
 * token at an apostrophe that isn't preceded by a letter, so a contraction
 * like "isn't" can't be mistaken for the start of a token — without that
 * guard, a message containing both a contraction and a quoted token (e.g.
 * `"that name isn't available: 'foo'"`) would pair the contraction's
 * apostrophe with the token's opening quote, mangling the contraction and
 * leaving a stray quote behind.
 */
export function errorMessage(error: unknown, fallback: string): string {
  const message = error instanceof Error ? error.message : fallback;
  return message
    .replace(/(?<![A-Za-z])'([^']*)'/g, "$1")
    .replace(/"([^"]*)"/g, "$1");
}

/** Extract a `.error` string from a parsed JSON body, if it has one. */
function errorMessageFrom(data: unknown): string | null {
  if (data !== null && typeof data === "object" && "error" in data) {
    const message = (data as { error: unknown }).error;
    if (typeof message === "string") {
      return message;
    }
  }
  return null;
}

/**
 * Read a fetch Response's body as text, then attempt to parse it as JSON.
 * Throws ApiError (with the response's status) whenever:
 *  - the status is not ok and the body has a JSON `{"error": "..."}` message
 *    (that message is used), or
 *  - the status is not ok and the body isn't JSON at all (proxy error pages,
 *    plain-text 5xx bodies) — falls back to the status line, or
 *  - the status is ok but the body still isn't valid JSON (unexpected, but
 *    surfaced clearly rather than as a raw SyntaxError).
 */
async function parse<T>(res: Response): Promise<T> {
  const text = await res.text();

  let data: unknown = undefined;
  let parseFailed = false;
  if (text.length > 0) {
    try {
      data = JSON.parse(text);
    } catch {
      parseFailed = true;
    }
  }

  if (!res.ok) {
    const message = parseFailed
      ? `${res.status} ${res.statusText}`.trim()
      : (errorMessageFrom(data) ?? `${res.status} ${res.statusText}`.trim());
    throw new ApiError(message, res.status);
  }

  if (parseFailed) {
    throw new ApiError(
      `expected a JSON response, got: ${text.slice(0, 200)}`,
      res.status,
    );
  }

  return data as T;
}

export async function listAgents(): Promise<AgentSummary[]> {
  const res = await fetch("/api/agents");
  const data = await parse<{ agents: AgentSummary[] }>(res);
  return data.agents;
}

export async function getAgent(name: string): Promise<AgentDetail> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}`);
  return parse<AgentDetail>(res);
}

export async function createAgent(body: {
  name: string;
  persona: string;
  skills: { id: string; team: string }[];
  model: string | null;
}): Promise<{ name: string; status: string }> {
  const res = await fetch("/api/agents", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parse<{ name: string; status: string }>(res);
}

export async function retryAgent(name: string): Promise<void> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/retry`, {
    method: "POST",
  });
  await parse(res);
}

/**
 * `DELETE /api/agents/{name}`: starts (or resumes) the four-step destroy
 * ledger. 202 `{"status": "destroying"}` on success; safe to call again
 * while a prior destroy is still running or has failed a step ("Destroy
 * again" is the same call, resume is entirely server-side). The server
 * rejects with 403 (non-operator), 404 (unknown agent), or 409 (the agent
 * is still being created), all surfaced as an `ApiError` through `parse`,
 * same as every other mutation in this file.
 */
export async function destroyAgent(name: string): Promise<{ status: string }> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  return parse<{ status: string }>(res);
}

/** `GET /api/catalog/models` — the one catalog endpoint that survived the
 * move to search: skills and personas are now `searchSkills`/`searchPersonas`
 * below, but the model list is small and fixed, so it's still fetched
 * whole. */
export async function fetchModels(): Promise<{
  models: { id: string; label: string }[];
  default: string;
}> {
  const res = await fetch("/api/catalog/models");
  return parse<{ models: { id: string; label: string }[]; default: string }>(
    res,
  );
}

/** Builds a `?q=...&team=...` query string: `q` is always present (even
 * empty), `team` is omitted entirely when null. Shared by the two catalog
 * search endpoints below. */
function searchQueryString(q: string, team: string | null): string {
  const params = new URLSearchParams();
  params.set("q", q);
  if (team !== null) {
    params.set("team", team);
  }
  return params.toString();
}

export async function searchSkills(
  q: string,
  team: string | null,
): Promise<CatalogEntry[]> {
  const res = await fetch(`/api/skills/search?${searchQueryString(q, team)}`);
  const data = await parse<{ results: CatalogEntry[] }>(res);
  return data.results;
}

export async function searchPersonas(
  q: string,
  team: string | null,
): Promise<CatalogEntry[]> {
  const res = await fetch(
    `/api/personas/search?${searchQueryString(q, team)}`,
  );
  const data = await parse<{ results: CatalogEntry[] }>(res);
  return data.results;
}

export async function fetchTeams(): Promise<string[]> {
  const res = await fetch("/api/catalog/teams");
  const data = await parse<{ teams: string[] }>(res);
  return data.teams;
}

/**
 * `POST /api/personas/draft` — turns one plain job description (plus
 * optional short "always/never" rule hints, a prompt hint only — never
 * stored) into a drafted name/who_they_are/what_they_do for the v2 preview
 * to edit. Always 200 from the server; nothing is created or searched
 * here — `createPersona` below (untouched by v3) is still the only call
 * that actually writes anything.
 */
export async function draftPersona(
  job: string,
  rules: string[],
): Promise<PersonaDraft> {
  const res = await fetch("/api/personas/draft", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job, rules }),
  });
  return parse<PersonaDraft>(res);
}

export async function createPersona(body: PersonaForm): Promise<CatalogEntry> {
  const res = await fetch("/api/personas", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parse<CatalogEntry>(res);
}

/**
 * `POST /api/agents/name-suggestions` — five name+reason candidates for
 * the agent being created, generated from the just-committed persona's own
 * `who_they_are`/`what_they_do` text. The server never fails to return
 * five (a curated fallback pool stands in when its generator errors out),
 * so this only ever rejects on a genuine transport/route failure. Wired as
 * a query factory (queries.ts's `nameSuggestionsQuery`), not a mutation,
 * even though the transport is POST — nothing is created or changed by
 * calling it, the free-text fields just don't fit a query string.
 */
export async function fetchNameSuggestions(
  whoTheyAre: string,
  whatTheyDo: string,
): Promise<NameSuggestion[]> {
  const res = await fetch("/api/agents/name-suggestions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      who_they_are: whoTheyAre,
      what_they_do: whatTheyDo,
    }),
  });
  const data = await parse<{ suggestions: NameSuggestion[] }>(res);
  return data.suggestions;
}

export async function fetchOrgStatus(): Promise<OrgStatus> {
  const res = await fetch("/api/org/status");
  return parse<OrgStatus>(res);
}

export async function chatWithAgent(
  name: string,
  messages: ChatMessage[],
): Promise<{ reply: string }> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages }),
  });
  return parse<{ reply: string }>(res);
}

export async function checkAvailability(
  name: string,
): Promise<{ available: boolean; reason: string | null }> {
  const res = await fetch(`/api/availability?name=${encodeURIComponent(name)}`);
  return parse<{ available: boolean; reason: string | null }>(res);
}

export type KnowledgeFile = { name: string; content: string };

export async function listKnowledge(name: string): Promise<KnowledgeFile[]> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/knowledge`);
  const data = await parse<{ files: KnowledgeFile[] }>(res);
  return data.files;
}

export async function saveKnowledgeFile(
  name: string,
  filename: string,
  content: string,
): Promise<{ sha: string }> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/knowledge/${encodeURIComponent(filename)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    },
  );
  return parse<{ sha: string }>(res);
}

export async function deleteKnowledgeFile(
  name: string,
  filename: string,
): Promise<{ sha: string }> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/knowledge/${encodeURIComponent(filename)}`,
    { method: "DELETE" },
  );
  return parse<{ sha: string }>(res);
}

export async function fetchDeliveryState(
  name: string,
  sha: string,
): Promise<{ state: string; detail: string | null }> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/knowledge/delivery/${encodeURIComponent(sha)}`,
  );
  return parse<{ state: string; detail: string | null }>(res);
}

/** One entry of `GET /api/agents/{name}/skills` — a catalog pick or a
 * locally-authored guardrail skill, both served by the same shape. `team`
 * and `sha` are set for a catalog pick (the catalog team it came from, and
 * the catalog's current pin for it); both are `null` for a local skill,
 * which carries no catalog provenance at all. `editable` is `true` exactly
 * when `source === "local"` — a catalog pick's content lives in the
 * catalog, not something this room edits in place. */
export type AgentSkill = {
  name: string;
  team: string | null;
  source: "catalog" | "local";
  sha: string | null;
  editable: boolean;
};

export async function listSkills(name: string): Promise<AgentSkill[]> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/skills`);
  const data = await parse<{ skills: AgentSkill[] }>(res);
  return data.skills;
}

/**
 * Picks a skill from the org catalog for this agent (`team`/`skillName`
 * identify it there). Returns the pushed commit's sha, same
 * delivery-tracking shape every other mutation here returns.
 */
export async function addCatalogSkill(
  name: string,
  skillName: string,
  team: string,
): Promise<{ sha: string }> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/skills/catalog`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: skillName, team }),
    },
  );
  return parse<{ sha: string }>(res);
}

/** Removes a skill — catalog pick or local guardrail alike, the server's
 * single unified delete path for both classes. */
export async function deleteSkill(
  name: string,
  skillId: string,
): Promise<{ sha: string }> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/skills/${encodeURIComponent(skillId)}`,
    { method: "DELETE" },
  );
  return parse<{ sha: string }>(res);
}

/** The current content of one local (guardrail-authored) skill. Never
 * served for a catalog pick — the server 404s that case instead. */
export async function readLocalSkill(
  name: string,
  skillId: string,
): Promise<{ name: string; content: string }> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/skills/local/${encodeURIComponent(skillId)}`,
  );
  return parse<{ name: string; content: string }>(res);
}

/** Saves (creates or overwrites) one local skill's content. */
export async function saveLocalSkill(
  name: string,
  skillId: string,
  content: string,
): Promise<{ sha: string }> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/skills/local/${encodeURIComponent(skillId)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    },
  );
  return parse<{ sha: string }>(res);
}

/** One `bundle/intake/*` file — a one-shot upload meant to be ingested into
 * the agent's memory exactly once by a runtime seeder. `status` is derived
 * fresh by the server from git ancestry vs. the live image on every list
 * call, never kobo-side state: `"pending"` until the live pod's commit
 * descends from (or is) the commit that last touched this file,
 * `"ingested"` once it does. */
export type IntakeFile = {
  name: string;
  content_bytes: number;
  status: "pending" | "ingested";
};

export async function listIntake(name: string): Promise<IntakeFile[]> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/intake`);
  const data = await parse<{ files: IntakeFile[] }>(res);
  return data.files;
}

/** Uploads (creates or overwrites) one intake file. Unlike knowledge/skill
 * saves, the filename travels in the request body, not the URL path — this
 * is a one-shot upload, not an edit of a named existing resource. */
export async function uploadIntake(
  name: string,
  filename: string,
  content: string,
): Promise<{ sha: string }> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/intake`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename, content }),
  });
  return parse<{ sha: string }>(res);
}

/** Removes an intake file's source upload. `note` is the server's
 * plain-language reminder that this only removes the source file — whatever
 * the runtime seeder already ingested from it into the agent's actual
 * memory stays. */
export async function deleteIntake(
  name: string,
  filename: string,
): Promise<{ sha: string; note: string }> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/intake/${encodeURIComponent(filename)}`,
    { method: "DELETE" },
  );
  return parse<{ sha: string; note: string }>(res);
}

/** One `bundle/tools/*` file — the staged door for an agent's LAW-to-be
 * (2026-08-13 addendum): stored and delivered the same way knowledge/skills
 * are. `delivered` is derived fresh by the server from the same git-ancestry
 * check `IntakeFile.status` uses, never kobo-side state — true once the live
 * pod's commit descends from (or is) the commit that last touched this
 * file. `status` (Task 4: card probe + banner graduation) is present, and
 * always exactly `"enforced"`, only when the server's card probe attests
 * the runtime executes tooling hooks AND isn't currently locked AND this
 * file is delivered — see `ToolingEnforcement`. A pending file, or any file
 * when the runtime isn't attested (or is locked), carries no `status` key
 * at all: delivery truth is never overwritten by enforcement status. */
export type ToolingFile = {
  name: string;
  content_bytes: number;
  delivered: boolean;
  status?: "enforced";
};

/** Whether the running pod actually enforces `bundle/tools/*` hooks around a
 * reply — probed fresh on every list fetch from the pod's own
 * `/.well-known/agent-card.json` (Task 4), never guessed or cached
 * kobo-side. `attested` is true iff that fetch succeeded AND the card's
 * `enforcement.version` is >= 1; false for a pre-enforcement runtime, an
 * unreachable/still-booting pod, or a card kobo couldn't parse — every one
 * of those reads identically, on purpose. `locked`/`locked_file` are
 * independent of `attested`: a runtime can attest to enforcement and still
 * be locked (a hook file failed to load, so it's serving refusals until
 * fixed or removed). */
export type ToolingEnforcement = {
  attested: boolean;
  locked: boolean;
  locked_file: string | null;
};

export type ToolingListResponse = {
  files: ToolingFile[];
  enforcement: ToolingEnforcement;
};

export async function listTooling(name: string): Promise<ToolingListResponse> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/tooling`);
  return parse<ToolingListResponse>(res);
}

/** Uploads (creates or overwrites) one tooling file. Like `uploadIntake`,
 * the filename travels in the request body, not the URL path — this is a
 * fresh file pick from a form, not an edit of an already-named resource. */
export async function uploadTooling(
  name: string,
  filename: string,
  content: string,
): Promise<{ sha: string }> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/tooling`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename, content }),
  });
  return parse<{ sha: string }>(res);
}

/** `POST /api/agents/{name}/tooling/import`'s partial-pass result: every
 * candidate `.py`/`.go` file under `path` in `repo` is validated
 * independently, so one bad file never sinks the rest. `sha` is `null` when
 * nothing passed (nothing was pushed at all — see `kobo_api.tooling.
 * import_tooling`'s docstring). */
export type ToolingImportResult = {
  sha: string | null;
  imported: string[];
  rejected: { name: string; reason: string }[];
};

/** Imports every `.py`/`.go` file that passes validation from `path` in
 * `repo` (cloned via kobo's own GitHub App identity — no separate auth
 * needed) into this agent's `bundle/tools/`, in one delivery. */
export async function importTooling(
  name: string,
  repo: string,
  path: string,
): Promise<ToolingImportResult> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/tooling/import`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo, path }),
    },
  );
  return parse<ToolingImportResult>(res);
}

/** Removes a tooling file. These are files, not memories — deleting removes
 * the file, full stop (no "already ingested elsewhere" caveat like
 * `deleteIntake`'s `note`). */
export async function deleteTooling(
  name: string,
  filename: string,
): Promise<{ sha: string }> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/tooling/${encodeURIComponent(filename)}`,
    { method: "DELETE" },
  );
  return parse<{ sha: string }>(res);
}

/**
 * Creates the Slack app for this agent (server calls out to Slack's app
 * manifest API and returns the app id plus a configure_url for the human
 * to open and finish install). No request body — the agent name in the
 * path is all the server needs.
 */
export async function createSlackApp(
  name: string,
): Promise<{ app_id: string; configure_url: string }> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/slack/app`,
    { method: "POST" },
  );
  return parse<{ app_id: string; configure_url: string }>(res);
}

/**
 * Finishes Slack connect: hands the channel and the two tokens (copied by
 * the human from Slack's app settings page) to the server, which validates
 * and stores them. The tokens pass straight through in the request body and
 * are never logged or otherwise inspected here.
 */
export async function connectSlack(
  name: string,
  body: { channel: string; bot_token: string; app_token: string },
): Promise<{ sha: string }> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/slack/connect`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  return parse<{ sha: string }>(res);
}

/**
 * Generates (or regenerates) an agent's picture: the server derives a
 * prompt from the agent's persona, generates a square image, commits it to
 * the agent's repo, and caches it on the agent row. Returns the commit sha
 * so the caller can cache-bust the `<img src>` that reads the picture back
 * (see `GET /api/agents/{name}/avatar`, a plain URL — no client fn needed
 * for that side, an `<img>` tag fetches it directly).
 */
export async function generateAvatar(name: string): Promise<{ sha: string }> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/avatar`, {
    method: "POST",
  });
  return parse<{ sha: string }>(res);
}

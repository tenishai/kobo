// Typed client over the pnyx-api HTTP surface. Every function funnels its
// Response through `parse()`, which turns a non-2xx status into an ApiError
// (message taken from a JSON `{"error": "..."}` body when present, otherwise
// a readable fallback built from the status line) and turns a body that
// isn't valid JSON at all (an HTML proxy-error page, a plain-text 500 from
// some upstream that isn't pnyx-api) into an ApiError too, instead of
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
  skills: { name: string; sha: string }[];
  repo_full_name: string | null;
  first_reply: string | null;
  error: string | null;
  steps: StepView[];
  slack: { app_id: string; channel: string | null } | null;
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

/** The persona builder's form — posted verbatim as the body of
 * `POST /api/personas`. `tags` is required here (the server enforces 1-6);
 * the form itself enforcing that is Task 10's job. */
export type PersonaForm = {
  name: string;
  role: string;
  purpose: string;
  tone: "warm" | "direct" | "playful" | "formal";
  tone_note: string;
  boundaries: string[];
  tags: string[];
  team: string;
};

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
  team: string;
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

export async function createPersona(body: PersonaForm): Promise<CatalogEntry> {
  const res = await fetch("/api/personas", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parse<CatalogEntry>(res);
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

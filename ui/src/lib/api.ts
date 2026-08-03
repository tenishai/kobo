// Typed client over the cortex-api HTTP surface. Every function funnels its
// Response through `parse()`, which turns a non-2xx status into an ApiError
// (message taken from a JSON `{"error": "..."}` body when present, otherwise
// a readable fallback built from the status line) and turns a body that
// isn't valid JSON at all (an HTML proxy-error page, a plain-text 500 from
// some upstream that isn't cortex-api) into an ApiError too, instead of
// letting a raw SyntaxError leak out of `JSON.parse`.

export type AgentSummary = {
  name: string;
  status: string;
  model: string;
  created_at: string;
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
};

export type Catalogs = {
  personas: { id: string; description: string }[];
  skills: { id: string; description: string; sha: string }[];
  models: { id: string; label: string }[];
  defaultModel: string;
};

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
  skills: string[];
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

export async function fetchCatalogs(): Promise<Catalogs> {
  const [personasRes, skillsRes, modelsRes] = await Promise.all([
    fetch("/api/catalog/personas"),
    fetch("/api/catalog/skills"),
    fetch("/api/catalog/models"),
  ]);

  const [personas, skills, models] = await Promise.all([
    parse<{ personas: Catalogs["personas"] }>(personasRes),
    parse<{ skills: Catalogs["skills"] }>(skillsRes),
    parse<{ models: Catalogs["models"]; default: string }>(modelsRes),
  ]);

  return {
    personas: personas.personas,
    skills: skills.skills,
    models: models.models,
    defaultModel: models.default,
  };
}

export async function checkAvailability(
  name: string,
): Promise<{ available: boolean; reason: string | null }> {
  const res = await fetch(`/api/availability?name=${encodeURIComponent(name)}`);
  return parse<{ available: boolean; reason: string | null }>(res);
}

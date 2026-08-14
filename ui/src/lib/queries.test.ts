import { describe, expect, it, vi } from "vitest";
import { ApiError, readLocalSkill, type AgentSkill, type CatalogEntry, type IntakeFile, type OrgStatus, type ToolingFile } from "./api";
import {
  addCatalogSkillMutation,
  agentSkillsQuery,
  chatMutation,
  connectSlackMutation,
  createPersonaMutation,
  createSlackAppMutation,
  deleteIntakeMutation,
  deleteKnowledgeMutation,
  deleteSkillMutation,
  deleteToolingMutation,
  deliveryQuery,
  deliveryRefetchInterval,
  generateAvatarMutation,
  importToolingMutation,
  intakeQuery,
  intakeRefetchInterval,
  modelsQuery,
  orgStatusQuery,
  personasSearchQuery,
  retryAgentMutation,
  saveKnowledgeMutation,
  saveLocalSkillMutation,
  shouldRetry,
  skillsSearchQuery,
  teamsQuery,
  toolingQuery,
  toolingRefetchInterval,
  uploadIntakeMutation,
  uploadToolingMutation,
} from "./queries";
import type { QueryClient } from "@tanstack/svelte-query";

/**
 * Invokes a `queryOptions()` result's `queryFn` directly, for tests that
 * want to assert the underlying fetch it makes. `queryFn`'s type is
 * `QueryFunction | typeof skipToken` (TanStack's "disable this query"
 * sentinel) — every factory under test here always supplies a real
 * function, never the sentinel, so this cast is safe; it just avoids
 * threading a fabricated `QueryFunctionContext` through every callsite
 * below.
 */
function callQueryFn<T>(options: { queryFn?: unknown }): Promise<T> {
  return (options.queryFn as () => Promise<T>)();
}

describe("shouldRetry", () => {
  it("does not retry a 404 ApiError, even on the first failure", () => {
    expect(shouldRetry(0, new ApiError("not found", 404))).toBe(false);
  });

  it("retries a 500 ApiError up to the default 3 attempts", () => {
    const error = new ApiError("server error", 500);
    expect(shouldRetry(0, error)).toBe(true);
    expect(shouldRetry(1, error)).toBe(true);
    expect(shouldRetry(2, error)).toBe(true);
    expect(shouldRetry(3, error)).toBe(false);
  });

  it("retries a network TypeError up to the default 3 attempts", () => {
    const error = new TypeError("Failed to fetch");
    expect(shouldRetry(0, error)).toBe(true);
    expect(shouldRetry(1, error)).toBe(true);
    expect(shouldRetry(2, error)).toBe(true);
    expect(shouldRetry(3, error)).toBe(false);
  });
});

describe("chatMutation", () => {
  it("mutationFn delegates to chatWithAgent(name, messages)", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ reply: "hi" }),
    } as Response);

    const mutation = chatMutation("clio");
    const messages = [{ role: "user" as const, text: "hello" }];

    await expect(mutation.mutationFn(messages)).resolves.toEqual({
      reply: "hi",
    });
    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages }),
    });

    vi.unstubAllGlobals();
  });

  it("has no onSuccess/cache invalidation — chat doesn't change agent state", () => {
    const mutation = chatMutation("clio");
    expect((mutation as { onSuccess?: unknown }).onSuccess).toBeUndefined();
  });
});

describe("retryAgentMutation", () => {
  it("invalidates only the agent-list key on success", () => {
    // TanStack's default invalidateQueries match is a prefix match, so
    // ["agents"] alone already covers ["agents", name] too — a second call
    // for the detail key would be a redundant no-op. Assert there's only
    // the one call.
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = retryAgentMutation(queryClient, "clio");
    mutation.onSuccess();

    expect(invalidateQueries).toHaveBeenCalledTimes(1);
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ["agents"] });
  });
});

describe("saveKnowledgeMutation", () => {
  it("invalidates the knowledge query for this agent on success", () => {
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = saveKnowledgeMutation(queryClient, "clio");
    mutation.onSuccess();

    expect(invalidateQueries).toHaveBeenCalledTimes(1);
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["agents", "clio", "knowledge"],
    });
  });
});

describe("deleteKnowledgeMutation", () => {
  it("invalidates the knowledge query for this agent on success", () => {
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = deleteKnowledgeMutation(queryClient, "clio");
    mutation.onSuccess();

    expect(invalidateQueries).toHaveBeenCalledTimes(1);
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["agents", "clio", "knowledge"],
    });
  });
});

describe("createSlackAppMutation", () => {
  it("mutationFn delegates to createSlackApp(name)", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () =>
        JSON.stringify({
          app_id: "A123",
          configure_url: "https://slack.com/apps/A123",
        }),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = createSlackAppMutation(queryClient, "clio");

    await expect(mutation.mutationFn()).resolves.toEqual({
      app_id: "A123",
      configure_url: "https://slack.com/apps/A123",
    });
    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/slack/app", {
      method: "POST",
    });

    vi.unstubAllGlobals();
  });

  it("invalidates the agent detail key on success — the detail carries slack state", () => {
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = createSlackAppMutation(queryClient, "clio");
    mutation.onSuccess();

    expect(invalidateQueries).toHaveBeenCalledTimes(1);
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["agents", "clio"],
    });
  });
});

describe("connectSlackMutation", () => {
  it("mutationFn delegates to connectSlack(name, body)", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ sha: "abc123" }),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = connectSlackMutation(queryClient, "clio");
    const body = {
      channel: "C123456",
      bot_token: "xoxb-secret",
      app_token: "xapp-secret",
    };

    await expect(mutation.mutationFn(body)).resolves.toEqual({
      sha: "abc123",
    });
    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/slack/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    vi.unstubAllGlobals();
  });

  it("invalidates the agent detail key on success — the detail carries slack state", () => {
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = connectSlackMutation(queryClient, "clio");
    mutation.onSuccess();

    expect(invalidateQueries).toHaveBeenCalledTimes(1);
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["agents", "clio"],
    });
  });
});

describe("generateAvatarMutation", () => {
  it("mutationFn delegates to generateAvatar(name)", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ sha: "abc123" }),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = generateAvatarMutation(queryClient, "clio");

    await expect(mutation.mutationFn()).resolves.toEqual({ sha: "abc123" });
    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/avatar", {
      method: "POST",
    });

    vi.unstubAllGlobals();
  });

  it("invalidates the broad agent-list key on success — has_avatar shows on both the list and the detail", () => {
    // Unlike createSlackAppMutation/connectSlackMutation (detail-only,
    // since the list carries no slack state), has_avatar is rendered on
    // AgentCard (list) AND AgentDetail's header (detail) — so this must
    // invalidate the ["agents"] prefix, which (per TanStack's prefix-match
    // invalidation, same as retryAgentMutation above) already covers
    // ["agents", name] too. Pinning exactly one call, with the broad key.
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = generateAvatarMutation(queryClient, "clio");
    mutation.onSuccess();

    expect(invalidateQueries).toHaveBeenCalledTimes(1);
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ["agents"] });
  });
});

describe("deliveryQuery", () => {
  it("is disabled when sha is null (nothing to poll for yet)", () => {
    expect(deliveryQuery("clio", null).enabled).toBe(false);
  });

  it("is enabled once a sha is known", () => {
    expect(deliveryQuery("clio", "abc123").enabled).toBe(true);
  });
});

describe("deliveryRefetchInterval", () => {
  it("is false for undefined (no data fetched yet)", () => {
    expect(deliveryRefetchInterval(undefined)).toBe(false);
  });

  it("is 3500 while the delivery is building", () => {
    expect(
      deliveryRefetchInterval({ state: "building", detail: null }),
    ).toBe(3500);
  });

  it("is 3500 while the delivery is rolling", () => {
    expect(deliveryRefetchInterval({ state: "rolling", detail: null })).toBe(
      3500,
    );
  });

  it("is false once the delivery has succeeded", () => {
    expect(
      deliveryRefetchInterval({ state: "delivered", detail: null }),
    ).toBe(false);
  });

  it("is false once the delivery has failed", () => {
    expect(
      deliveryRefetchInterval({ state: "failed", detail: "boom" }),
    ).toBe(false);
  });
});

describe("modelsQuery", () => {
  it("has queryKey ['catalog', 'models']", () => {
    expect(modelsQuery().queryKey).toEqual(["catalog", "models"]);
  });

  it("queryFn GETs /api/catalog/models", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () =>
        JSON.stringify({ models: [{ id: "gpt-5", label: "GPT-5" }], default: "gpt-5" }),
    } as Response);

    await expect(
      callQueryFn<{ models: { id: string; label: string }[]; default: string }>(
        modelsQuery(),
      ),
    ).resolves.toEqual({
      models: [{ id: "gpt-5", label: "GPT-5" }],
      default: "gpt-5",
    });
    expect(fetch).toHaveBeenCalledWith("/api/catalog/models");

    vi.unstubAllGlobals();
  });
});

describe("teamsQuery", () => {
  it("has queryKey ['catalog', 'teams']", () => {
    expect(teamsQuery().queryKey).toEqual(["catalog", "teams"]);
  });

  it("queryFn GETs /api/catalog/teams", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ teams: ["shared"] }),
    } as Response);

    await expect(callQueryFn<string[]>(teamsQuery())).resolves.toEqual([
      "shared",
    ]);
    expect(fetch).toHaveBeenCalledWith("/api/catalog/teams");

    vi.unstubAllGlobals();
  });
});

describe("skillsSearchQuery", () => {
  it("has queryKey ['catalog', 'skills', {q, team}]", () => {
    expect(skillsSearchQuery("read", "shared").queryKey).toEqual([
      "catalog",
      "skills",
      { q: "read", team: "shared" },
    ]);
  });

  it("has staleTime 15000", () => {
    expect(skillsSearchQuery("read", null).staleTime).toBe(15_000);
  });

  it("queryFn delegates to searchSkills(q, team)", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ results: [] }),
    } as Response);

    await expect(
      callQueryFn<CatalogEntry[]>(skillsSearchQuery("read", null)),
    ).resolves.toEqual([]);
    expect(fetch).toHaveBeenCalledWith("/api/skills/search?q=read");

    vi.unstubAllGlobals();
  });
});

describe("personasSearchQuery", () => {
  it("has queryKey ['catalog', 'personas', {q, team}]", () => {
    expect(personasSearchQuery("help", null).queryKey).toEqual([
      "catalog",
      "personas",
      { q: "help", team: null },
    ]);
  });

  it("has staleTime 15000", () => {
    expect(personasSearchQuery("help", null).staleTime).toBe(15_000);
  });

  it("queryFn delegates to searchPersonas(q, team)", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ results: [] }),
    } as Response);

    await expect(
      callQueryFn<CatalogEntry[]>(personasSearchQuery("help", "growth")),
    ).resolves.toEqual([]);
    expect(fetch).toHaveBeenCalledWith(
      "/api/personas/search?q=help&team=growth",
    );

    vi.unstubAllGlobals();
  });
});

describe("orgStatusQuery", () => {
  it("has queryKey ['org', 'status']", () => {
    expect(orgStatusQuery().queryKey).toEqual(["org", "status"]);
  });

  it("has staleTime 30000", () => {
    expect(orgStatusQuery().staleTime).toBe(30_000);
  });

  it("queryFn GETs /api/org/status", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const status = { org: null, wired: false, checks: [] };
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify(status),
    } as Response);

    await expect(callQueryFn<OrgStatus>(orgStatusQuery())).resolves.toEqual(
      status,
    );
    expect(fetch).toHaveBeenCalledWith("/api/org/status");

    vi.unstubAllGlobals();
  });
});

describe("createPersonaMutation", () => {
  it("mutationFn delegates to createPersona(body)", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const body = {
      name: "helper",
      who_they_are: "an unflappable network detective who's seen every outage twice",
      what_they_do: "helps with things",
      rules: [],
      team: "shared",
    };
    const created = {
      id: "helper",
      name: "helper",
      description: "an unflappable network detective who's seen every outage twice",
      team: "shared",
      tags: ["general"],
    };
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 201,
      statusText: "Created",
      text: async () => JSON.stringify(created),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = createPersonaMutation(queryClient);

    await expect(mutation.mutationFn(body)).resolves.toEqual(created);
    expect(fetch).toHaveBeenCalledWith("/api/personas", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    vi.unstubAllGlobals();
  });

  it("invalidates both the personas-search and teams keys on success", () => {
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = createPersonaMutation(queryClient);
    mutation.onSuccess();

    expect(invalidateQueries).toHaveBeenCalledTimes(2);
    expect(invalidateQueries).toHaveBeenNthCalledWith(1, {
      queryKey: ["catalog", "personas"],
    });
    expect(invalidateQueries).toHaveBeenNthCalledWith(2, {
      queryKey: ["catalog", "teams"],
    });
  });
});

describe("agentSkillsQuery", () => {
  it("has queryKey ['agents', name, 'skills']", () => {
    expect(agentSkillsQuery("clio").queryKey).toEqual([
      "agents",
      "clio",
      "skills",
    ]);
  });

  it("queryFn GETs /api/agents/{name}/skills and unwraps {skills: [...]}", async () => {
    const skills: AgentSkill[] = [
      {
        name: "read-code",
        team: "shared",
        source: "catalog",
        sha: "abc123def456",
        editable: false,
      },
      { name: "my-guardrail", team: null, source: "local", sha: null, editable: true },
    ];
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ skills }),
    } as Response);

    await expect(
      callQueryFn<AgentSkill[]>(agentSkillsQuery("clio")),
    ).resolves.toEqual(skills);
    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/skills");

    vi.unstubAllGlobals();
  });
});

describe("addCatalogSkillMutation", () => {
  it("mutationFn POSTs {name, team} to /api/agents/{name}/skills/catalog and returns {sha}", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ sha: "abc123" }),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = addCatalogSkillMutation(queryClient, "clio");

    await expect(
      mutation.mutationFn({ skillName: "read-code", team: "shared" }),
    ).resolves.toEqual({ sha: "abc123" });
    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/skills/catalog", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "read-code", team: "shared" }),
    });

    vi.unstubAllGlobals();
  });

  it("invalidates the agent's skills query on success", () => {
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = addCatalogSkillMutation(queryClient, "clio");
    mutation.onSuccess();

    expect(invalidateQueries).toHaveBeenCalledTimes(1);
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["agents", "clio", "skills"],
    });
  });

  it("surfaces a 409's {error} message (name collision) through parse()", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 409,
      statusText: "Conflict",
      text: async () =>
        JSON.stringify({
          error: "the agent already carries a skill named 'read-code'",
        }),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = addCatalogSkillMutation(queryClient, "clio");

    await expect(
      mutation.mutationFn({ skillName: "read-code", team: "shared" }),
    ).rejects.toThrow("the agent already carries a skill named 'read-code'");

    vi.unstubAllGlobals();
  });
});

describe("deleteSkillMutation", () => {
  it("mutationFn DELETEs /api/agents/{name}/skills/{skillId} and returns {sha}", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ sha: "def456" }),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = deleteSkillMutation(queryClient, "clio");

    await expect(mutation.mutationFn("read-code")).resolves.toEqual({
      sha: "def456",
    });
    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/skills/read-code", {
      method: "DELETE",
    });

    vi.unstubAllGlobals();
  });

  it("invalidates the agent's skills query on success", () => {
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = deleteSkillMutation(queryClient, "clio");
    mutation.onSuccess();

    expect(invalidateQueries).toHaveBeenCalledTimes(1);
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["agents", "clio", "skills"],
    });
  });
});

describe("saveLocalSkillMutation", () => {
  it("mutationFn PUTs {content} to /api/agents/{name}/skills/local/{skillId} and returns {sha}", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ sha: "ghi789" }),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = saveLocalSkillMutation(queryClient, "clio");
    const content = "---\nname: my-guardrail\ndescription: keeps things safe\n---\nBody text.";

    await expect(
      mutation.mutationFn({ skillId: "my-guardrail", content }),
    ).resolves.toEqual({ sha: "ghi789" });
    expect(fetch).toHaveBeenCalledWith(
      "/api/agents/clio/skills/local/my-guardrail",
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      },
    );

    vi.unstubAllGlobals();
  });

  it("invalidates the agent's skills query on success", () => {
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = saveLocalSkillMutation(queryClient, "clio");
    mutation.onSuccess();

    expect(invalidateQueries).toHaveBeenCalledTimes(1);
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["agents", "clio", "skills"],
    });
  });
});

describe("readLocalSkill", () => {
  it("GETs /api/agents/{name}/skills/local/{skillId} and returns {name, content}", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () =>
        JSON.stringify({
          name: "my-guardrail",
          content: "---\nname: my-guardrail\ndescription: x\n---\n",
        }),
    } as Response);

    await expect(readLocalSkill("clio", "my-guardrail")).resolves.toEqual({
      name: "my-guardrail",
      content: "---\nname: my-guardrail\ndescription: x\n---\n",
    });
    expect(fetch).toHaveBeenCalledWith(
      "/api/agents/clio/skills/local/my-guardrail",
    );

    vi.unstubAllGlobals();
  });

  it("surfaces a 404's {error} message through parse() (unknown local skill)", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 404,
      statusText: "Not Found",
      text: async () =>
        JSON.stringify({ error: "no such local skill: 'nope'" }),
    } as Response);

    await expect(readLocalSkill("clio", "nope")).rejects.toThrow(
      "no such local skill: 'nope'",
    );

    vi.unstubAllGlobals();
  });
});

describe("intakeQuery", () => {
  it("has queryKey ['agents', name, 'intake']", () => {
    expect(intakeQuery("clio").queryKey).toEqual(["agents", "clio", "intake"]);
  });

  it("queryFn GETs /api/agents/{name}/intake and unwraps {files: [...]}", async () => {
    const files: IntakeFile[] = [
      { name: "notes.md", content_bytes: 42, status: "pending" },
      { name: "old-notes.txt", content_bytes: 100, status: "ingested" },
    ];
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ files }),
    } as Response);

    await expect(
      callQueryFn<IntakeFile[]>(intakeQuery("clio")),
    ).resolves.toEqual(files);
    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/intake");

    vi.unstubAllGlobals();
  });
});

describe("intakeRefetchInterval", () => {
  it("is false for undefined (no data fetched yet)", () => {
    expect(intakeRefetchInterval(undefined)).toBe(false);
  });

  it("is false once every file is ingested", () => {
    expect(
      intakeRefetchInterval([
        { name: "a.md", content_bytes: 1, status: "ingested" },
        { name: "b.txt", content_bytes: 2, status: "ingested" },
      ]),
    ).toBe(false);
  });

  it("is false for an empty file list", () => {
    expect(intakeRefetchInterval([])).toBe(false);
  });

  it("is 5000 while any file is still pending", () => {
    expect(
      intakeRefetchInterval([
        { name: "a.md", content_bytes: 1, status: "ingested" },
        { name: "b.txt", content_bytes: 2, status: "pending" },
      ]),
    ).toBe(5000);
  });
});

describe("uploadIntakeMutation", () => {
  it("mutationFn POSTs {filename, content} to /api/agents/{name}/intake and returns {sha}", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ sha: "abc123" }),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = uploadIntakeMutation(queryClient, "clio");
    const body = { filename: "notes.md", content: "# hello" };

    await expect(mutation.mutationFn(body)).resolves.toEqual({
      sha: "abc123",
    });
    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/intake", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    vi.unstubAllGlobals();
  });

  it("invalidates the agent's intake query on success", () => {
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = uploadIntakeMutation(queryClient, "clio");
    mutation.onSuccess();

    expect(invalidateQueries).toHaveBeenCalledTimes(1);
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["agents", "clio", "intake"],
    });
  });

  it("surfaces a 422 IntakeError message through parse()", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 422,
      statusText: "Unprocessable Entity",
      text: async () =>
        JSON.stringify({
          error: "invalid intake filename: 'Notes.pdf'",
        }),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = uploadIntakeMutation(queryClient, "clio");

    await expect(
      mutation.mutationFn({ filename: "Notes.pdf", content: "x" }),
    ).rejects.toThrow("invalid intake filename: 'Notes.pdf'");

    vi.unstubAllGlobals();
  });
});

describe("deleteIntakeMutation", () => {
  it("mutationFn DELETEs /api/agents/{name}/intake/{filename} and returns {sha, note}", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () =>
        JSON.stringify({
          sha: "def456",
          note: "memories are kept — only the source file was removed",
        }),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = deleteIntakeMutation(queryClient, "clio");

    await expect(mutation.mutationFn("notes.md")).resolves.toEqual({
      sha: "def456",
      note: "memories are kept — only the source file was removed",
    });
    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/intake/notes.md", {
      method: "DELETE",
    });

    vi.unstubAllGlobals();
  });

  it("invalidates the agent's intake query on success", () => {
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = deleteIntakeMutation(queryClient, "clio");
    mutation.onSuccess();

    expect(invalidateQueries).toHaveBeenCalledTimes(1);
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["agents", "clio", "intake"],
    });
  });
});

describe("toolingQuery", () => {
  it("has queryKey ['agents', name, 'tooling']", () => {
    expect(toolingQuery("clio").queryKey).toEqual([
      "agents",
      "clio",
      "tooling",
    ]);
  });

  it("queryFn GETs /api/agents/{name}/tooling and unwraps {files: [...]}", async () => {
    const files: ToolingFile[] = [
      { name: "guard.py", content_bytes: 128, delivered: true },
      { name: "helper.go", content_bytes: 256, delivered: false },
    ];
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ files }),
    } as Response);

    await expect(
      callQueryFn<ToolingFile[]>(toolingQuery("clio")),
    ).resolves.toEqual(files);
    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/tooling");

    vi.unstubAllGlobals();
  });
});

describe("toolingRefetchInterval", () => {
  it("is false for undefined (no data fetched yet)", () => {
    expect(toolingRefetchInterval(undefined)).toBe(false);
  });

  it("is false once every file is delivered", () => {
    expect(
      toolingRefetchInterval([
        { name: "a.py", content_bytes: 1, delivered: true },
        { name: "b.go", content_bytes: 2, delivered: true },
      ]),
    ).toBe(false);
  });

  it("is false for an empty file list", () => {
    expect(toolingRefetchInterval([])).toBe(false);
  });

  it("is 5000 while any file is still undelivered", () => {
    expect(
      toolingRefetchInterval([
        { name: "a.py", content_bytes: 1, delivered: true },
        { name: "b.go", content_bytes: 2, delivered: false },
      ]),
    ).toBe(5000);
  });
});

describe("uploadToolingMutation", () => {
  it("mutationFn POSTs {filename, content} to /api/agents/{name}/tooling and returns {sha}", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ sha: "abc123" }),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = uploadToolingMutation(queryClient, "clio");
    const body = { filename: "guard.py", content: "def run():\n    pass\n" };

    await expect(mutation.mutationFn(body)).resolves.toEqual({
      sha: "abc123",
    });
    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/tooling", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    vi.unstubAllGlobals();
  });

  it("invalidates the agent's tooling query on success", () => {
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = uploadToolingMutation(queryClient, "clio");
    mutation.onSuccess();

    expect(invalidateQueries).toHaveBeenCalledTimes(1);
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["agents", "clio", "tooling"],
    });
  });

  it("surfaces a 422 syntax-error-with-line-number message through parse(), verbatim", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 422,
      statusText: "Unprocessable Entity",
      text: async () =>
        JSON.stringify({
          error: "tooling file has a syntax error on line 3: invalid syntax",
        }),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = uploadToolingMutation(queryClient, "clio");

    await expect(
      mutation.mutationFn({ filename: "bad.py", content: "def f(:\n  pass" }),
    ).rejects.toThrow(
      "tooling file has a syntax error on line 3: invalid syntax",
    );

    vi.unstubAllGlobals();
  });
});

describe("importToolingMutation", () => {
  it("mutationFn POSTs {repo, path} to /api/agents/{name}/tooling/import and returns the partial-pass result", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const result = {
      sha: "def456",
      imported: ["guard.py"],
      rejected: [
        {
          name: "broken.py",
          reason: "tooling file has a syntax error on line 1: invalid syntax",
        },
      ],
    };
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify(result),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = importToolingMutation(queryClient, "clio");
    const body = { repo: "some-org/tools-repo", path: "shared-guardrails" };

    await expect(mutation.mutationFn(body)).resolves.toEqual(result);
    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/tooling/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    vi.unstubAllGlobals();
  });

  it("invalidates the agent's tooling query on success", () => {
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = importToolingMutation(queryClient, "clio");
    mutation.onSuccess();

    expect(invalidateQueries).toHaveBeenCalledTimes(1);
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["agents", "clio", "tooling"],
    });
  });

  it("propagates a 404 (with its verbatim message) when the source path doesn't exist in the repo", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 404,
      statusText: "Not Found",
      text: async () =>
        JSON.stringify({
          error:
            "tooling source path not found: 'nope' in 'some-org/tools-repo'",
        }),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = importToolingMutation(queryClient, "clio");

    let caught: unknown;
    try {
      await mutation.mutationFn({
        repo: "some-org/tools-repo",
        path: "nope",
      });
    } catch (err) {
      caught = err;
    }

    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).status).toBe(404);
    expect((caught as ApiError).message).toBe(
      "tooling source path not found: 'nope' in 'some-org/tools-repo'",
    );

    vi.unstubAllGlobals();
  });
});

describe("deleteToolingMutation", () => {
  it("mutationFn DELETEs /api/agents/{name}/tooling/{filename} and returns {sha}", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ sha: "ghi789" }),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = deleteToolingMutation(queryClient, "clio");

    await expect(mutation.mutationFn("guard.py")).resolves.toEqual({
      sha: "ghi789",
    });
    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/tooling/guard.py", {
      method: "DELETE",
    });

    vi.unstubAllGlobals();
  });

  it("invalidates the agent's tooling query on success", () => {
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = deleteToolingMutation(queryClient, "clio");
    mutation.onSuccess();

    expect(invalidateQueries).toHaveBeenCalledTimes(1);
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["agents", "clio", "tooling"],
    });
  });
});

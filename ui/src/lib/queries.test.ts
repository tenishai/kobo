import { describe, expect, it, vi } from "vitest";
import { ApiError, readLocalSkill, type AgentSkill, type ArchivedPersona, type CatalogEntry, type GroupRequest, type IntakeFile, type NameSuggestion, type OrgStatus, type ToolingEnforcement, type ToolingListResponse, type Whoami } from "./api";
import {
  addCatalogSkillMutation,
  addThreadParticipantMutation,
  agentSkillsQuery,
  archivePersonaMutation,
  archivedPersonasQuery,
  chatMutation,
  colleagueFeedQuery,
  connectSlackMutation,
  createPersonaMutation,
  createThreadMutation,
  decideGroupRequestMutation,
  deleteIntakeMutation,
  deleteKnowledgeMutation,
  deleteSkillMutation,
  deleteToolingMutation,
  deliveryQuery,
  deliveryRefetchInterval,
  destroyAgentMutation,
  draftPersonaMutation,
  generateAvatarMutation,
  groupRequestsQuery,
  importToolingMutation,
  intakeQuery,
  intakeRefetchInterval,
  modelsQuery,
  nameSuggestionsQuery,
  orgStatusQuery,
  patchMetadataMutation,
  personasSearchQuery,
  postThreadTurnMutation,
  regenerateMetadataMutation,
  requestGroupMutation,
  retryAgentMutation,
  saveKnowledgeMutation,
  saveLocalSkillMutation,
  saveSlackChannelsMutation,
  shouldRetry,
  skillsSearchQuery,
  slackManifestQuery,
  teamOptionsQuery,
  teamsQuery,
  toolingQuery,
  toolingRefetchInterval,
  unarchivePersonaMutation,
  uploadAvatarMutation,
  uploadIntakeMutation,
  uploadToolingMutation,
  whoamiQuery,
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

describe("destroyAgentMutation", () => {
  it("mutationFn DELETEs /api/agents/{name} and returns {status}", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 202,
      statusText: "Accepted",
      text: async () => JSON.stringify({ status: "destroying" }),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = destroyAgentMutation(queryClient, "clio");

    await expect(mutation.mutationFn()).resolves.toEqual({
      status: "destroying",
    });
    expect(fetch).toHaveBeenCalledWith("/api/agents/clio", {
      method: "DELETE",
    });

    vi.unstubAllGlobals();
  });

  it("invalidates the broad agent-list key on success (a prefix match already covers the detail key too)", () => {
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = destroyAgentMutation(queryClient, "clio");
    mutation.onSuccess();

    expect(invalidateQueries).toHaveBeenCalledTimes(1);
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ["agents"] });
  });

  it("surfaces a 403's {error} message (non-operator) through parse()", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 403,
      statusText: "Forbidden",
      text: async () =>
        JSON.stringify({
          error: "Destroying agents is operator-only on this room",
        }),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = destroyAgentMutation(queryClient, "clio");

    let caught: unknown;
    try {
      await mutation.mutationFn();
    } catch (err) {
      caught = err;
    }

    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).status).toBe(403);
    expect((caught as ApiError).message).toBe(
      "Destroying agents is operator-only on this room",
    );

    vi.unstubAllGlobals();
  });

  it("surfaces a 409's {error} message (still being created) through parse()", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 409,
      statusText: "Conflict",
      text: async () =>
        JSON.stringify({
          error: "the agent is still being created, retry or wait",
        }),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = destroyAgentMutation(queryClient, "clio");

    await expect(mutation.mutationFn()).rejects.toThrow(
      "the agent is still being created, retry or wait",
    );

    vi.unstubAllGlobals();
  });

  it("surfaces a 404's {error} message (unknown agent) through parse()", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 404,
      statusText: "Not Found",
      text: async () =>
        JSON.stringify({ error: "no such agent: 'nope'" }),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = destroyAgentMutation(queryClient, "clio");

    await expect(mutation.mutationFn()).rejects.toThrow(
      "no such agent: 'nope'",
    );

    vi.unstubAllGlobals();
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
      app_id: "A1234567",
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

describe("slackManifestQuery", () => {
  it("has queryKey ['agents', name, 'slack', 'manifest']", () => {
    expect(slackManifestQuery("clio", false).queryKey).toEqual([
      "agents",
      "clio",
      "slack",
      "manifest",
    ]);
  });

  it("is disabled until opened", () => {
    expect(slackManifestQuery("clio", false).enabled).toBe(false);
  });

  it("is enabled once opened", () => {
    expect(slackManifestQuery("clio", true).enabled).toBe(true);
  });

  it("queryFn GETs /api/agents/{name}/slack/manifest and unwraps {manifest} into the plain dict", async () => {
    const manifest = { display_information: { name: "clio" } };
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ manifest }),
    } as Response);

    await expect(
      callQueryFn<Record<string, unknown>>(slackManifestQuery("clio", true)),
    ).resolves.toEqual(manifest);
    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/slack/manifest");

    vi.unstubAllGlobals();
  });
});

describe("saveSlackChannelsMutation", () => {
  it("mutationFn delegates to saveSlackChannels(name, channels) via PUT", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () =>
        JSON.stringify({ channels: ["C123456"], state: "waiting" }),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = saveSlackChannelsMutation(queryClient, "clio");

    await expect(mutation.mutationFn(["C123456"])).resolves.toEqual({
      channels: ["C123456"],
      state: "waiting",
    });
    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/slack/channels", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channels: ["C123456"] }),
    });

    vi.unstubAllGlobals();
  });

  it("invalidates the agent detail key on success — the detail carries slack state", () => {
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = saveSlackChannelsMutation(queryClient, "clio");
    mutation.onSuccess();

    expect(invalidateQueries).toHaveBeenCalledTimes(1);
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["agents", "clio"],
    });
  });
});

describe("patchMetadataMutation", () => {
  it("mutationFn delegates to patchAgentMetadata(name, body) via PATCH", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () =>
        JSON.stringify({
          team: "growth",
          purpose_tags: ["billing"],
          capability: "Answers billing questions.",
          sha: "abc123",
        }),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = patchMetadataMutation(queryClient, "clio");
    const body = { team: "growth", purpose_tags: ["billing"] };

    await expect(mutation.mutationFn(body)).resolves.toEqual({
      team: "growth",
      purpose_tags: ["billing"],
      capability: "Answers billing questions.",
      sha: "abc123",
    });
    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/metadata", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    vi.unstubAllGlobals();
  });

  it("invalidates the agent detail key on success", () => {
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = patchMetadataMutation(queryClient, "clio");
    mutation.onSuccess();

    expect(invalidateQueries).toHaveBeenCalledTimes(1);
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["agents", "clio"],
    });
  });
});

describe("regenerateMetadataMutation", () => {
  it("mutationFn delegates to regenerateAgentMetadata(name) via POST, no body", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () =>
        JSON.stringify({
          team: "shared",
          purpose_tags: [],
          capability: "Regenerated capability line.",
          sha: "def456",
        }),
    } as Response);

    const queryClient = {
      invalidateQueries: vi.fn(),
    } as unknown as QueryClient;
    const mutation = regenerateMetadataMutation(queryClient, "clio");

    await expect(mutation.mutationFn()).resolves.toEqual({
      team: "shared",
      purpose_tags: [],
      capability: "Regenerated capability line.",
      sha: "def456",
    });
    expect(fetch).toHaveBeenCalledWith(
      "/api/agents/clio/metadata/regenerate",
      { method: "POST" },
    );

    vi.unstubAllGlobals();
  });

  it("invalidates the agent detail key on success", () => {
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = regenerateMetadataMutation(queryClient, "clio");
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
    // Unlike connectSlackMutation/saveSlackChannelsMutation (detail-only,
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

describe("uploadAvatarMutation", () => {
  it("mutationFn delegates to uploadAvatar(name, png)", async () => {
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
    const mutation = uploadAvatarMutation(queryClient, "clio");
    const bytes = new Uint8Array([1, 2, 3]).buffer;

    await expect(mutation.mutationFn(bytes)).resolves.toEqual({ sha: "def456" });
    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/avatar", {
      method: "PUT",
      headers: { "Content-Type": "image/png" },
      body: bytes,
    });

    vi.unstubAllGlobals();
  });

  it("invalidates the broad agent-list key on success, same as generateAvatarMutation", () => {
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = uploadAvatarMutation(queryClient, "clio");
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

describe("colleagueFeedQuery", () => {
  it("has queryKey ['colleagues', 'feed', agent]", () => {
    expect(colleagueFeedQuery("ward").queryKey).toEqual(["colleagues", "feed", "ward"]);
  });

  it("queryFn GETs /api/colleagues/feed?agent=<agent> and returns the body verbatim", async () => {
    const feed = {
      entries: [
        {
          ts: "2026-08-19T10:00:00Z",
          conversation_id: "t-1",
          from: "ward",
          to: "network",
          role: "ask",
          text: "What's the config?",
        },
      ],
      truncated: false,
    };
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify(feed),
    } as Response);

    await expect(callQueryFn<typeof feed>(colleagueFeedQuery("ward"))).resolves.toEqual(feed);
    expect(fetch).toHaveBeenCalledWith("/api/colleagues/feed?agent=ward");

    vi.unstubAllGlobals();
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

describe("teamOptionsQuery", () => {
  it("has queryKey ['teams'] — distinct from teamsQuery's ['catalog', 'teams']", () => {
    expect(teamOptionsQuery().queryKey).toEqual(["teams"]);
  });

  it("queryFn GETs /api/teams and resolves {teams, default} verbatim", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () =>
        JSON.stringify({ teams: ["growth", "platform"], default: "shared" }),
    } as Response);

    await expect(
      callQueryFn<{ teams: string[]; default: string }>(teamOptionsQuery()),
    ).resolves.toEqual({ teams: ["growth", "platform"], default: "shared" });
    expect(fetch).toHaveBeenCalledWith("/api/teams");

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

describe("archivedPersonasQuery (hardening wave archive lane)", () => {
  it("has queryKey ['personas', 'archived']", () => {
    expect(archivedPersonasQuery(false).queryKey).toEqual(["personas", "archived"]);
  });

  it("is disabled until the 'Show archived' toggle is on", () => {
    expect(archivedPersonasQuery(false).enabled).toBe(false);
  });

  it("is enabled once the toggle is on", () => {
    expect(archivedPersonasQuery(true).enabled).toBe(true);
  });

  it("queryFn GETs /api/personas/archived and unwraps {personas: [...]}", async () => {
    const personas: ArchivedPersona[] = [
      {
        id: "helper",
        name: "Helper",
        description: "a calm, steady presence",
        team: "shared",
        tags: ["general"],
        builtin: true,
        archived: true,
      },
    ];
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ personas }),
    } as Response);

    await expect(
      callQueryFn<ArchivedPersona[]>(archivedPersonasQuery(true)),
    ).resolves.toEqual(personas);
    expect(fetch).toHaveBeenCalledWith("/api/personas/archived");

    vi.unstubAllGlobals();
  });
});

describe("archivePersonaMutation (hardening wave archive lane)", () => {
  it("mutationFn delegates to archivePersona(id) via POST", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const updated = {
      id: "helper",
      name: "Helper",
      description: "a calm, steady presence",
      team: "shared",
      tags: ["general"],
      builtin: true,
      archived: true,
    };
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify(updated),
    } as Response);

    const queryClient = { invalidateQueries: vi.fn() } as unknown as QueryClient;
    const mutation = archivePersonaMutation(queryClient);

    await expect(mutation.mutationFn("helper")).resolves.toEqual(updated);
    expect(fetch).toHaveBeenCalledWith("/api/personas/helper/archive", {
      method: "POST",
    });

    vi.unstubAllGlobals();
  });

  it("invalidates both the active persona search and the archived list on success", () => {
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = archivePersonaMutation(queryClient);
    mutation.onSuccess();

    expect(invalidateQueries).toHaveBeenCalledTimes(2);
    expect(invalidateQueries).toHaveBeenNthCalledWith(1, {
      queryKey: ["catalog", "personas"],
    });
    expect(invalidateQueries).toHaveBeenNthCalledWith(2, {
      queryKey: ["personas", "archived"],
    });
  });
});

describe("unarchivePersonaMutation (hardening wave archive lane)", () => {
  it("mutationFn delegates to unarchivePersona(id) via POST", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const updated = {
      id: "helper",
      name: "Helper",
      description: "a calm, steady presence",
      team: "shared",
      tags: ["general"],
      builtin: true,
    };
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify(updated),
    } as Response);

    const queryClient = { invalidateQueries: vi.fn() } as unknown as QueryClient;
    const mutation = unarchivePersonaMutation(queryClient);

    await expect(mutation.mutationFn("helper")).resolves.toEqual(updated);
    expect(fetch).toHaveBeenCalledWith("/api/personas/helper/unarchive", {
      method: "POST",
    });

    vi.unstubAllGlobals();
  });

  it("invalidates both the active persona search and the archived list on success, same as archivePersonaMutation", () => {
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = unarchivePersonaMutation(queryClient);
    mutation.onSuccess();

    expect(invalidateQueries).toHaveBeenCalledTimes(2);
    expect(invalidateQueries).toHaveBeenNthCalledWith(1, {
      queryKey: ["catalog", "personas"],
    });
    expect(invalidateQueries).toHaveBeenNthCalledWith(2, {
      queryKey: ["personas", "archived"],
    });
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

describe("whoamiQuery", () => {
  it("has queryKey ['whoami']", () => {
    expect(whoamiQuery().queryKey).toEqual(["whoami"]);
  });

  it("has staleTime Infinity", () => {
    expect(whoamiQuery().staleTime).toBe(Infinity);
  });

  it("queryFn GETs /api/whoami", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const whoami = { email: "ops@example.com", operator: true, groups: [] };
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify(whoami),
    } as Response);

    await expect(callQueryFn<Whoami>(whoamiQuery())).resolves.toEqual(whoami);
    expect(fetch).toHaveBeenCalledWith("/api/whoami");

    vi.unstubAllGlobals();
  });
});

describe("nameSuggestionsQuery", () => {
  it("has queryKey ['names', 'suggestions', {who_they_are, what_they_do}]", () => {
    expect(
      nameSuggestionsQuery("a calm troubleshooter", "helps with billing").queryKey,
    ).toEqual([
      "names",
      "suggestions",
      { who_they_are: "a calm troubleshooter", what_they_do: "helps with billing" },
    ]);
  });

  it("is disabled when either field is empty (or whitespace-only)", () => {
    expect(nameSuggestionsQuery("", "helps with billing").enabled).toBe(false);
    expect(nameSuggestionsQuery("a calm troubleshooter", "").enabled).toBe(false);
    expect(nameSuggestionsQuery("  ", "helps with billing").enabled).toBe(false);
    expect(nameSuggestionsQuery("", "").enabled).toBe(false);
  });

  it("is enabled once both fields are non-empty", () => {
    expect(
      nameSuggestionsQuery("a calm troubleshooter", "helps with billing").enabled,
    ).toBe(true);
  });

  it("queryFn POSTs {who_they_are, what_they_do} to /api/agents/name-suggestions and unwraps {suggestions: [...]}", async () => {
    const suggestions: NameSuggestion[] = [
      { name: "ember", reason: "a steady hand" },
      { name: "cedar", reason: "quiet and quick" },
    ];
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ suggestions }),
    } as Response);

    await expect(
      callQueryFn<NameSuggestion[]>(
        nameSuggestionsQuery("a calm troubleshooter", "helps with billing"),
      ),
    ).resolves.toEqual(suggestions);
    expect(fetch).toHaveBeenCalledWith("/api/agents/name-suggestions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        who_they_are: "a calm troubleshooter",
        what_they_do: "helps with billing",
      }),
    });

    vi.unstubAllGlobals();
  });

  it("propagates a 422's {error} message through parse()", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 422,
      statusText: "Unprocessable Entity",
      text: async () =>
        JSON.stringify({ error: "who_they_are must not be empty" }),
    } as Response);

    await expect(
      callQueryFn<NameSuggestion[]>(nameSuggestionsQuery(" ", "x")),
    ).rejects.toThrow("who_they_are must not be empty");

    vi.unstubAllGlobals();
  });

  it("propagates a 502's {error} message through parse() (fallback pool exhausted)", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 502,
      statusText: "Bad Gateway",
      text: async () =>
        JSON.stringify({ error: "couldn't come up with names right now — try again" }),
    } as Response);

    await expect(
      callQueryFn<NameSuggestion[]>(
        nameSuggestionsQuery("a calm troubleshooter", "helps with billing"),
      ),
    ).rejects.toThrow("couldn't come up with names right now — try again");

    vi.unstubAllGlobals();
  });
});

describe("draftPersonaMutation", () => {
  it("mutationFn delegates to draftPersona(job, rules)", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const draft = {
      name: "billing-helper",
      who_they_are: "a calm guide through confusing invoices",
      what_they_do: "Answers billing questions and escalates refunds.",
    };
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify(draft),
    } as Response);

    const mutation = draftPersonaMutation();

    await expect(
      mutation.mutationFn({
        job: "Help customers with billing questions.",
        rules: ["never guess"],
      }),
    ).resolves.toEqual(draft);
    expect(fetch).toHaveBeenCalledWith("/api/personas/draft", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job: "Help customers with billing questions.",
        rules: ["never guess"],
      }),
    });

    vi.unstubAllGlobals();
  });

  it("has no onSuccess/cache invalidation — drafting doesn't create anything", () => {
    const mutation = draftPersonaMutation();
    expect((mutation as { onSuccess?: unknown }).onSuccess).toBeUndefined();
  });

  it("propagates a transport failure (a 502 proxy error) as an ApiError", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 502,
      statusText: "Bad Gateway",
      text: async () => "<html>502 Bad Gateway</html>",
    } as Response);

    const mutation = draftPersonaMutation();

    await expect(
      mutation.mutationFn({ job: "Does the job.", rules: [] }),
    ).rejects.toBeInstanceOf(ApiError);

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

// Task 4 (card probe + banner graduation): the enforcement envelope a
// pre-enforcement (or unreachable) runtime's card reads as — the fixture
// every test below that isn't itself exercising enforcement pins.
const UNATTESTED: ToolingEnforcement = { attested: false, locked: false, locked_file: null };

describe("toolingQuery", () => {
  it("has queryKey ['agents', name, 'tooling']", () => {
    expect(toolingQuery("clio").queryKey).toEqual([
      "agents",
      "clio",
      "tooling",
    ]);
  });

  it("queryFn GETs /api/agents/{name}/tooling and returns {files, enforcement}", async () => {
    const response: ToolingListResponse = {
      files: [
        { name: "guard.py", content_bytes: 128, delivered: true, status: "enforced" },
        { name: "helper.go", content_bytes: 256, delivered: false },
      ],
      enforcement: { attested: true, locked: false, locked_file: null },
    };
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify(response),
    } as Response);

    await expect(
      callQueryFn<ToolingListResponse>(toolingQuery("clio")),
    ).resolves.toEqual(response);
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
      toolingRefetchInterval({
        files: [
          { name: "a.py", content_bytes: 1, delivered: true },
          { name: "b.go", content_bytes: 2, delivered: true },
        ],
        enforcement: UNATTESTED,
      }),
    ).toBe(false);
  });

  it("is false for an empty file list", () => {
    expect(toolingRefetchInterval({ files: [], enforcement: UNATTESTED })).toBe(false);
  });

  it("is 5000 while any file is still undelivered", () => {
    expect(
      toolingRefetchInterval({
        files: [
          { name: "a.py", content_bytes: 1, delivered: true },
          { name: "b.go", content_bytes: 2, delivered: false },
        ],
        enforcement: UNATTESTED,
      }),
    ).toBe(5000);
  });

  it("is unaffected by enforcement state — only file delivery drives polling", () => {
    expect(
      toolingRefetchInterval({
        files: [{ name: "a.py", content_bytes: 1, delivered: true, status: "enforced" }],
        enforcement: { attested: true, locked: false, locked_file: null },
      }),
    ).toBe(false);
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

describe("requestGroupMutation (shared-group + group-requests wave)", () => {
  it("mutationFn POSTs {group_name, note?} to /api/group-requests", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const created: GroupRequest = {
      id: 1,
      group_name: "design-team",
      requested_by: "kai@example.com",
      note: "for the new onboarding agent",
      status: "pending",
      decided_by: null,
      created_at: "2026-08-22T00:00:00Z",
      decided_at: null,
    };
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 201,
      statusText: "Created",
      text: async () => JSON.stringify(created),
    } as Response);

    const mutation = requestGroupMutation();
    await expect(
      mutation.mutationFn({
        groupName: "design-team",
        note: "for the new onboarding agent",
      }),
    ).resolves.toEqual(created);
    expect(fetch).toHaveBeenCalledWith("/api/group-requests", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        group_name: "design-team",
        note: "for the new onboarding agent",
      }),
    });

    vi.unstubAllGlobals();
  });

  it("omits the note key entirely when none is given", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 201,
      statusText: "Created",
      text: async () =>
        JSON.stringify({
          id: 2,
          group_name: "design-team",
          requested_by: null,
          note: null,
          status: "pending",
          decided_by: null,
          created_at: "2026-08-22T00:00:00Z",
          decided_at: null,
        }),
    } as Response);

    const mutation = requestGroupMutation();
    await mutation.mutationFn({ groupName: "design-team" });
    expect(fetch).toHaveBeenCalledWith("/api/group-requests", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ group_name: "design-team" }),
    });

    vi.unstubAllGlobals();
  });
});

describe("groupRequestsQuery (shared-group + group-requests wave)", () => {
  it("has queryKey ['group-requests']", () => {
    expect(groupRequestsQuery(true).queryKey).toEqual(["group-requests"]);
  });

  it("is disabled until the admin card is expanded", () => {
    expect(groupRequestsQuery(false).enabled).toBe(false);
    expect(groupRequestsQuery(true).enabled).toBe(true);
  });

  it("queryFn GETs /api/group-requests and unwraps {requests: [...]}", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const requests: GroupRequest[] = [
      {
        id: 1,
        group_name: "design-team",
        requested_by: "kai@example.com",
        note: null,
        status: "pending",
        decided_by: null,
        created_at: "2026-08-22T00:00:00Z",
        decided_at: null,
      },
    ];
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ requests }),
    } as Response);

    await expect(
      callQueryFn<GroupRequest[]>(groupRequestsQuery(true)),
    ).resolves.toEqual(requests);
    expect(fetch).toHaveBeenCalledWith("/api/group-requests");

    vi.unstubAllGlobals();
  });
});

describe("decideGroupRequestMutation (shared-group + group-requests wave)", () => {
  it("mutationFn POSTs /api/group-requests/{id}/{action}", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () =>
        JSON.stringify({
          id: 7,
          group_name: "design-team",
          requested_by: "kai@example.com",
          note: null,
          status: "done",
          decided_by: "admin@example.com",
          created_at: "2026-08-22T00:00:00Z",
          decided_at: "2026-08-22T01:00:00Z",
        }),
    } as Response);

    const queryClient = { invalidateQueries: vi.fn() } as unknown as QueryClient;
    const mutation = decideGroupRequestMutation(queryClient);

    await mutation.mutationFn({ id: 7, action: "done" });
    expect(fetch).toHaveBeenCalledWith("/api/group-requests/7/done", {
      method: "POST",
    });

    vi.unstubAllGlobals();
  });

  it("invalidates both the group-requests queue and whoami on success", () => {
    const invalidateQueries = vi.fn();
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    const mutation = decideGroupRequestMutation(queryClient);
    mutation.onSuccess();

    expect(invalidateQueries).toHaveBeenCalledTimes(2);
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ["group-requests"] });
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ["whoami"] });
  });
});

describe("createThreadMutation (threads arc, Task 10)", () => {
  it("mutationFn delegates to createThread(agent)", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 201,
      statusText: "Created",
      text: async () =>
        JSON.stringify({
          id: 7,
          initiator_email: "kai@example.com",
          created_at: "2026-08-27T00:00:00+00:00",
          participants: ["ward"],
        }),
    } as Response);

    const mutation = createThreadMutation();

    await expect(mutation.mutationFn("ward")).resolves.toEqual({
      id: 7,
      initiator_email: "kai@example.com",
      created_at: "2026-08-27T00:00:00+00:00",
      participants: ["ward"],
    });
    expect(fetch).toHaveBeenCalledWith("/api/threads", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent: "ward" }),
    });

    vi.unstubAllGlobals();
  });

  it("has no onSuccess/cache invalidation -- a thread has no query cache entry", () => {
    const mutation = createThreadMutation();
    expect((mutation as { onSuccess?: unknown }).onSuccess).toBeUndefined();
  });
});

describe("addThreadParticipantMutation (threads arc, Task 10)", () => {
  it("mutationFn({threadId, agent}) delegates to addThreadParticipant(threadId, agent)", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 201,
      statusText: "Created",
      text: async () =>
        JSON.stringify({
          id: 7,
          initiator_email: "kai@example.com",
          created_at: "2026-08-27T00:00:00+00:00",
          participants: ["ward", "clio"],
        }),
    } as Response);

    const mutation = addThreadParticipantMutation();

    await expect(mutation.mutationFn({ threadId: 7, agent: "clio" })).resolves.toEqual({
      id: 7,
      initiator_email: "kai@example.com",
      created_at: "2026-08-27T00:00:00+00:00",
      participants: ["ward", "clio"],
    });
    expect(fetch).toHaveBeenCalledWith("/api/threads/7/participants", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent: "clio" }),
    });

    vi.unstubAllGlobals();
  });

  it("has no onSuccess/cache invalidation -- a thread has no query cache entry", () => {
    const mutation = addThreadParticipantMutation();
    expect((mutation as { onSuccess?: unknown }).onSuccess).toBeUndefined();
  });
});

describe("postThreadTurnMutation (threads arc, Task 10)", () => {
  it("mutationFn({threadId, messages}) delegates to postThreadTurn(threadId, messages)", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () =>
        JSON.stringify({ replies: [{ agent: "ward", text: "Here's my take." }] }),
    } as Response);

    const mutation = postThreadTurnMutation();
    const messages = [{ role: "user" as const, text: "What do you think?" }];

    await expect(mutation.mutationFn({ threadId: 7, messages })).resolves.toEqual({
      replies: [{ agent: "ward", text: "Here's my take." }],
    });
    expect(fetch).toHaveBeenCalledWith("/api/threads/7/turns", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages }),
    });

    vi.unstubAllGlobals();
  });

  it("has no onSuccess/cache invalidation -- a thread has no query cache entry", () => {
    const mutation = postThreadTurnMutation();
    expect((mutation as { onSuccess?: unknown }).onSuccess).toBeUndefined();
  });
});

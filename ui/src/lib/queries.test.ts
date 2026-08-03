import { describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import {
  chatMutation,
  connectSlackMutation,
  createSlackAppMutation,
  deleteKnowledgeMutation,
  deliveryQuery,
  deliveryRefetchInterval,
  generateAvatarMutation,
  retryAgentMutation,
  saveKnowledgeMutation,
  shouldRetry,
} from "./queries";
import type { QueryClient } from "@tanstack/svelte-query";

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

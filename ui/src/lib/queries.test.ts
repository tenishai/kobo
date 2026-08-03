import { describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import {
  chatMutation,
  deleteKnowledgeMutation,
  deliveryQuery,
  deliveryRefetchInterval,
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

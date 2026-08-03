import { describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import { retryAgentMutation, shouldRetry } from "./queries";
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

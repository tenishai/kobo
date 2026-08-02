import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, createAgent, fetchCatalogs, listAgents } from "./api";

function jsonResponse(
  body: unknown,
  init: { status?: number; statusText?: string } = {},
): Response {
  const status = init.status ?? 200;
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: init.statusText ?? "OK",
    text: async () => JSON.stringify(body),
  } as Response;
}

function textResponse(
  body: string,
  init: { status?: number; statusText?: string } = {},
): Response {
  const status = init.status ?? 200;
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: init.statusText ?? "",
    text: async () => body,
  } as Response;
}

describe("api.ts", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("parse guard converts a non-JSON 502 body into an ApiError carrying the status", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      textResponse("<html>502 Bad Gateway</html>", {
        status: 502,
        statusText: "Bad Gateway",
      }),
    );

    await expect(listAgents()).rejects.toBeInstanceOf(ApiError);

    vi.mocked(fetch).mockResolvedValueOnce(
      textResponse("<html>502 Bad Gateway</html>", {
        status: 502,
        statusText: "Bad Gateway",
      }),
    );

    await expect(listAgents()).rejects.toMatchObject({ status: 502 });
  });

  it("listAgents unwraps {agents: [...]} into a plain array", async () => {
    const agents = [
      { name: "alpha", status: "ready", model: "gpt-5", created_at: "2026-01-01T00:00:00Z" },
    ];
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ agents }));

    await expect(listAgents()).resolves.toEqual(agents);
  });

  it("fetchCatalogs merges three responses and maps default -> defaultModel", async () => {
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("personas")) {
        return jsonResponse({ personas: [{ id: "helper", description: "A helper persona" }] });
      }
      if (url.includes("skills")) {
        return jsonResponse({
          skills: [{ id: "read-code", description: "Read code", sha: "abc123" }],
        });
      }
      if (url.includes("models")) {
        return jsonResponse({
          models: [{ id: "gpt-5", label: "GPT-5" }],
          default: "gpt-5",
        });
      }
      throw new Error(`unexpected url: ${url}`);
    });

    await expect(fetchCatalogs()).resolves.toEqual({
      personas: [{ id: "helper", description: "A helper persona" }],
      skills: [{ id: "read-code", description: "Read code", sha: "abc123" }],
      models: [{ id: "gpt-5", label: "GPT-5" }],
      defaultModel: "gpt-5",
    });
  });

  it("createAgent surfaces a 409's {error} message", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "name taken: 'foo'" },
        { status: 409, statusText: "Conflict" },
      ),
    );

    await expect(
      createAgent({ name: "foo", persona: "helper", skills: [], model: null }),
    ).rejects.toThrow("name taken: 'foo'");
  });
});

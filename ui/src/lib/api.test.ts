import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  chatWithAgent,
  connectSlack,
  createAgent,
  createPersona,
  createSlackApp,
  deleteKnowledgeFile,
  errorMessage,
  fetchDeliveryState,
  fetchModels,
  fetchOrgStatus,
  fetchTeams,
  generateAvatar,
  listAgents,
  listKnowledge,
  saveKnowledgeFile,
  searchPersonas,
  searchSkills,
} from "./api";

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
      {
        name: "alpha",
        status: "ready",
        model: "gpt-5",
        model_label: "GPT-5",
        created_at: "2026-01-01T00:00:00Z",
        has_avatar: false,
      },
    ];
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ agents }));

    await expect(listAgents()).resolves.toEqual(agents);
  });

  it("fetchModels GETs /api/catalog/models and returns {models, default} verbatim", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({
        models: [{ id: "gpt-5", label: "GPT-5" }],
        default: "gpt-5",
      }),
    );

    await expect(fetchModels()).resolves.toEqual({
      models: [{ id: "gpt-5", label: "GPT-5" }],
      default: "gpt-5",
    });

    expect(fetch).toHaveBeenCalledWith("/api/catalog/models");
  });

  it("createAgent POSTs the new body shape ({team, skills: [{id, team}]}) to /api/agents", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({ name: "foo", status: "provisioning" }),
    );

    const body = {
      name: "foo",
      persona: "helper",
      team: "shared",
      skills: [{ id: "read-code", team: "shared" }],
      model: "gpt-5",
    };

    await expect(createAgent(body)).resolves.toEqual({
      name: "foo",
      status: "provisioning",
    });

    expect(fetch).toHaveBeenCalledWith("/api/agents", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
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
      createAgent({
        name: "foo",
        persona: "helper",
        team: "shared",
        skills: [],
        model: null,
      }),
    ).rejects.toThrow("name taken: 'foo'");
  });

  it("chatWithAgent posts {messages} to /api/agents/{name}/chat and returns {reply}", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ reply: "hi there" }));

    const messages = [{ role: "user" as const, text: "hello" }];
    await expect(chatWithAgent("clio", messages)).resolves.toEqual({
      reply: "hi there",
    });

    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages }),
    });
  });

  it("chatWithAgent surfaces a 409's {error} message (agent not ready to chat)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "agent is not ready to chat" },
        { status: 409, statusText: "Conflict" },
      ),
    );

    await expect(
      chatWithAgent("clio", [{ role: "user", text: "hello" }]),
    ).rejects.toThrow("agent is not ready to chat");
  });

  it("chatWithAgent surfaces a 502's {error} message (agent unreachable)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "agent unreachable: connection reset" },
        { status: 502, statusText: "Bad Gateway" },
      ),
    );

    await expect(
      chatWithAgent("clio", [{ role: "user", text: "hello" }]),
    ).rejects.toThrow("agent unreachable: connection reset");
  });
});

describe("knowledge client", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("listKnowledge GETs /api/agents/{name}/knowledge and unwraps {files: [...]} into a plain array", async () => {
    const files = [
      { name: "notes.md", content: "hello" },
      { name: "facts.md", content: "world" },
    ];
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ files }));

    await expect(listKnowledge("clio")).resolves.toEqual(files);

    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/knowledge");
  });

  it("listKnowledge encodes the agent name in the URL", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ files: [] }));

    await listKnowledge("a b");

    expect(fetch).toHaveBeenCalledWith("/api/agents/a%20b/knowledge");
  });

  it("listKnowledge surfaces a 409's {error} message", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "agent repo unavailable" },
        { status: 409, statusText: "Conflict" },
      ),
    );

    await expect(listKnowledge("clio")).rejects.toThrow(
      "agent repo unavailable",
    );
  });

  it("saveKnowledgeFile PUTs {content} to /api/agents/{name}/knowledge/{filename} and returns {sha}", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ sha: "abc123" }));

    await expect(
      saveKnowledgeFile("clio", "notes.md", "updated text"),
    ).resolves.toEqual({ sha: "abc123" });

    expect(fetch).toHaveBeenCalledWith(
      "/api/agents/clio/knowledge/notes.md",
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: "updated text" }),
      },
    );
  });

  it("saveKnowledgeFile encodes the filename in the URL", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ sha: "abc123" }));

    await saveKnowledgeFile("clio", "sub dir/notes.md", "x");

    expect(fetch).toHaveBeenCalledWith(
      "/api/agents/clio/knowledge/sub%20dir%2Fnotes.md",
      expect.objectContaining({ method: "PUT" }),
    );
  });

  it("saveKnowledgeFile surfaces a 409's {error} message", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "knowledge file changed since last read" },
        { status: 409, statusText: "Conflict" },
      ),
    );

    await expect(
      saveKnowledgeFile("clio", "notes.md", "x"),
    ).rejects.toThrow("knowledge file changed since last read");
  });

  it("deleteKnowledgeFile DELETEs /api/agents/{name}/knowledge/{filename} and returns {sha}", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ sha: "def456" }));

    await expect(deleteKnowledgeFile("clio", "notes.md")).resolves.toEqual({
      sha: "def456",
    });

    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/knowledge/notes.md", {
      method: "DELETE",
    });
  });

  it("deleteKnowledgeFile surfaces a 409's {error} message", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "knowledge file changed since last read" },
        { status: 409, statusText: "Conflict" },
      ),
    );

    await expect(deleteKnowledgeFile("clio", "notes.md")).rejects.toThrow(
      "knowledge file changed since last read",
    );
  });

  it("fetchDeliveryState GETs /api/agents/{name}/knowledge/delivery/{sha} and returns {state, detail}", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({ state: "building", detail: null }),
    );

    await expect(fetchDeliveryState("clio", "abc123")).resolves.toEqual({
      state: "building",
      detail: null,
    });

    expect(fetch).toHaveBeenCalledWith(
      "/api/agents/clio/knowledge/delivery/abc123",
    );
  });

  it("fetchDeliveryState surfaces a 404's {error} message (unknown sha)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "no delivery for that sha" },
        { status: 404, statusText: "Not Found" },
      ),
    );

    await expect(fetchDeliveryState("clio", "nope")).rejects.toThrow(
      "no delivery for that sha",
    );
  });
});

describe("slack client", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("createSlackApp POSTs /api/agents/{name}/slack/app and returns {app_id, configure_url}", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({
        app_id: "A123",
        configure_url: "https://slack.com/apps/A123",
      }),
    );

    await expect(createSlackApp("clio")).resolves.toEqual({
      app_id: "A123",
      configure_url: "https://slack.com/apps/A123",
    });

    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/slack/app", {
      method: "POST",
    });
  });

  it("createSlackApp encodes the agent name in the URL", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({
        app_id: "A123",
        configure_url: "https://slack.com/apps/A123",
      }),
    );

    await createSlackApp("a b");

    expect(fetch).toHaveBeenCalledWith("/api/agents/a%20b/slack/app", {
      method: "POST",
    });
  });

  it("createSlackApp surfaces a 409's {error} message (already set up)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "Slack is already set up for this agent" },
        { status: 409, statusText: "Conflict" },
      ),
    );

    await expect(createSlackApp("clio")).rejects.toThrow(
      "Slack is already set up for this agent",
    );
  });

  it("createSlackApp surfaces a 503's {error} message (slack not configured)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "Slack is not configured" },
        { status: 503, statusText: "Service Unavailable" },
      ),
    );

    await expect(createSlackApp("clio")).rejects.toThrow(
      "Slack is not configured",
    );
  });

  it("connectSlack POSTs {channel, bot_token, app_token} to /api/agents/{name}/slack/connect and returns {sha}", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ sha: "abc123" }));

    const body = {
      channel: "C123456",
      bot_token: "xoxb-secret-token",
      app_token: "xapp-secret-token",
    };

    await expect(connectSlack("clio", body)).resolves.toEqual({
      sha: "abc123",
    });

    // Assert the exact request, including the raw body bytes — the token
    // values must pass through untouched, not be redacted or transformed.
    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/slack/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const [, init] = vi.mocked(fetch).mock.calls[0];
    expect(init?.body).toContain("xoxb-secret-token");
    expect(init?.body).toContain("xapp-secret-token");
  });

  it("connectSlack encodes the agent name in the URL", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ sha: "abc123" }));

    await connectSlack("a b", {
      channel: "C123456",
      bot_token: "xoxb-secret",
      app_token: "xapp-secret",
    });

    expect(fetch).toHaveBeenCalledWith(
      "/api/agents/a%20b/slack/connect",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("connectSlack never logs the token values it's given", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ sha: "abc123" }));
    const logSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});

    await connectSlack("clio", {
      channel: "C123456",
      bot_token: "xoxb-secret-token",
      app_token: "xapp-secret-token",
    });

    expect(logSpy).not.toHaveBeenCalled();
    expect(errorSpy).not.toHaveBeenCalled();
    expect(warnSpy).not.toHaveBeenCalled();

    logSpy.mockRestore();
    errorSpy.mockRestore();
    warnSpy.mockRestore();
  });

  it("connectSlack surfaces a 422's {error} message (bad channel)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "that doesn't look like a channel ID (it starts with C)" },
        { status: 422, statusText: "Unprocessable Entity" },
      ),
    );

    await expect(
      connectSlack("clio", {
        channel: "general",
        bot_token: "xoxb-secret",
        app_token: "xapp-secret",
      }),
    ).rejects.toThrow("that doesn't look like a channel ID (it starts with C)");
  });

  it("connectSlack surfaces a 422's {error} message (bad bot token)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "that doesn't look like a Slack bot token" },
        { status: 422, statusText: "Unprocessable Entity" },
      ),
    );

    await expect(
      connectSlack("clio", {
        channel: "C123456",
        bot_token: "wrong-prefix",
        app_token: "xapp-secret",
      }),
    ).rejects.toThrow("that doesn't look like a Slack bot token");
  });

  it("connectSlack surfaces a 422's {error} message (bad app token)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "that doesn't look like a Slack app-level token" },
        { status: 422, statusText: "Unprocessable Entity" },
      ),
    );

    await expect(
      connectSlack("clio", {
        channel: "C123456",
        bot_token: "xoxb-secret",
        app_token: "wrong-prefix",
      }),
    ).rejects.toThrow("that doesn't look like a Slack app-level token");
  });

  it("connectSlack surfaces the plain 502 {error} message (setup failed)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "couldn't set up Slack — try again" },
        { status: 502, statusText: "Bad Gateway" },
      ),
    );

    await expect(
      connectSlack("clio", {
        channel: "C123456",
        bot_token: "xoxb-secret",
        app_token: "xapp-secret",
      }),
    ).rejects.toThrow("couldn't set up Slack — try again");
  });
});

describe("avatar client", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("generateAvatar POSTs /api/agents/{name}/avatar and returns {sha}", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ sha: "abc123" }));

    await expect(generateAvatar("clio")).resolves.toEqual({ sha: "abc123" });

    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/avatar", {
      method: "POST",
    });
  });

  it("generateAvatar encodes the agent name in the URL", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ sha: "abc123" }));

    await generateAvatar("a b");

    expect(fetch).toHaveBeenCalledWith("/api/agents/a%20b/avatar", {
      method: "POST",
    });
  });

  it("generateAvatar surfaces a 409's {error} message (agent not ready)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "agent is not ready for an avatar yet" },
        { status: 409, statusText: "Conflict" },
      ),
    );

    await expect(generateAvatar("clio")).rejects.toThrow(
      "agent is not ready for an avatar yet",
    );
  });

  it("generateAvatar surfaces the plain 502 {error} message (generation failed)", async () => {
    // The server's ImageClient.generate uses one fixed message for every
    // failure mode (safety-blocked, non-200, malformed reply) — see
    // api/kobo_api/clients.py's `_IMAGE_GENERATION_FAILED` — pinned here
    // verbatim rather than a fabricated string.
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "the picture didn't come through — try again" },
        { status: 502, statusText: "Bad Gateway" },
      ),
    );

    await expect(generateAvatar("clio")).rejects.toThrow(
      "the picture didn't come through — try again",
    );
  });
});

describe("catalog search / teams / persona builder / org status client", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("searchSkills GETs /api/skills/search?q=<q> and unwraps {results: [...]}, omitting team when null", async () => {
    const results = [
      {
        id: "read-code",
        name: "Read code",
        description: "Reads source files",
        team: "shared",
        tags: ["fs"],
        sha: "abc123",
      },
    ];
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ results }));

    await expect(searchSkills("read", null)).resolves.toEqual(results);

    expect(fetch).toHaveBeenCalledWith("/api/skills/search?q=read");
  });

  it("searchSkills includes team in the query string when given", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ results: [] }));

    await searchSkills("read", "growth");

    expect(fetch).toHaveBeenCalledWith(
      "/api/skills/search?q=read&team=growth",
    );
  });

  it("searchSkills always sends q, even when empty", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ results: [] }));

    await searchSkills("", null);

    expect(fetch).toHaveBeenCalledWith("/api/skills/search?q=");
  });

  it("searchSkills escapes a hostile q containing '&' and spaces", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ results: [] }));

    await searchSkills("a&b c", "team one");

    expect(fetch).toHaveBeenCalledWith(
      "/api/skills/search?q=a%26b+c&team=team+one",
    );
  });

  it("searchPersonas GETs /api/personas/search?q=<q> and unwraps {results: [...]}, omitting team when null", async () => {
    const results = [
      {
        id: "helper",
        name: "Helper",
        description: "A helpful persona",
        team: "shared",
        tags: ["general"],
        builtin: true,
      },
    ];
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ results }));

    await expect(searchPersonas("help", null)).resolves.toEqual(results);

    expect(fetch).toHaveBeenCalledWith("/api/personas/search?q=help");
  });

  it("searchPersonas escapes a hostile q containing '&' and spaces, and includes team", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ results: [] }));

    await searchPersonas("a&b c", "growth team");

    expect(fetch).toHaveBeenCalledWith(
      "/api/personas/search?q=a%26b+c&team=growth+team",
    );
  });

  it("fetchTeams GETs /api/catalog/teams and unwraps {teams: [...]} into a plain array", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({ teams: ["shared", "growth"] }),
    );

    await expect(fetchTeams()).resolves.toEqual(["shared", "growth"]);

    expect(fetch).toHaveBeenCalledWith("/api/catalog/teams");
  });

  it("createPersona POSTs the form body to /api/personas and returns the created CatalogEntry", async () => {
    const body = {
      name: "helper",
      who_they_are: "an unflappable network detective who's seen every outage twice",
      what_they_do: "helps with things",
      rules: ["no secrets"],
      team: "shared",
    };
    const created = {
      id: "helper",
      name: "helper",
      description: "an unflappable network detective who's seen every outage twice",
      team: "shared",
      tags: ["general"],
    };
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(created, { status: 201 }),
    );

    await expect(createPersona(body)).resolves.toEqual(created);

    expect(fetch).toHaveBeenCalledWith("/api/personas", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  });

  it("createPersona surfaces a 409's {error} message (name collision)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "a persona with that name already exists" },
        { status: 409, statusText: "Conflict" },
      ),
    );

    await expect(
      createPersona({
        name: "helper",
        who_they_are: "a calm assistant",
        what_they_do: "helps",
        rules: [],
        team: "shared",
      }),
    ).rejects.toThrow("a persona with that name already exists");
  });

  it("createPersona surfaces a 503's {error} message (catalogs not set up)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "catalogs aren't set up yet" },
        { status: 503, statusText: "Service Unavailable" },
      ),
    );

    await expect(
      createPersona({
        name: "helper",
        who_they_are: "a calm assistant",
        what_they_do: "helps",
        rules: [],
        team: "shared",
      }),
    ).rejects.toThrow("catalogs aren't set up yet");
  });

  it("fetchOrgStatus GETs /api/org/status and returns {org, wired, checks} verbatim", async () => {
    const status = {
      org: "acme-org",
      wired: true,
      checks: [{ id: "argo", label: "ArgoCD", ok: true, detail: "synced" }],
    };
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(status));

    await expect(fetchOrgStatus()).resolves.toEqual(status);

    expect(fetch).toHaveBeenCalledWith("/api/org/status");
  });
});

describe("errorMessage", () => {
  it("strips a single-quoted token's quote marks", () => {
    expect(errorMessage(new Error("name 'foo-bar' is taken"), "fallback")).toBe(
      "name foo-bar is taken",
    );
  });

  it("strips a double-quoted token's quote marks", () => {
    expect(errorMessage(new Error('name "foo-bar" is taken'), "fallback")).toBe(
      "name foo-bar is taken",
    );
  });

  it("leaves an apostrophe inside a contraction untouched", () => {
    expect(errorMessage(new Error("that name isn't available"), "fallback")).toBe(
      "that name isn't available",
    );
  });

  it("keeps a contraction intact while unwrapping a quoted token in the same message", () => {
    expect(
      errorMessage(
        new Error("that name isn't available: 'foo'"),
        "fallback",
      ),
    ).toBe("that name isn't available: foo");
  });

  it("falls back to the given message when the error isn't an Error", () => {
    expect(errorMessage("boom", "Couldn't load agents.")).toBe(
      "Couldn't load agents.",
    );
    expect(errorMessage(undefined, "Couldn't load agents.")).toBe(
      "Couldn't load agents.",
    );
  });
});

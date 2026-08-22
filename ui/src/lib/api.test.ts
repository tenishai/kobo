import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  chatWithAgent,
  connectSlack,
  createAgent,
  createPersona,
  deleteKnowledgeFile,
  draftPersona,
  errorMessage,
  fetchColleagueConversation,
  fetchDeliveryState,
  fetchModels,
  fetchOrgStatus,
  fetchSlackManifest,
  fetchTeamOptions,
  fetchTeams,
  fetchWhoami,
  generateAvatar,
  listAgents,
  listColleagueConversations,
  listKnowledge,
  patchAgentMetadata,
  regenerateAgentMetadata,
  saveKnowledgeFile,
  saveSlackChannels,
  searchPersonas,
  searchSkills,
  uploadAvatar,
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

  it("createAgent POSTs the body shape ({team, skills: [{id, team}], purpose_tags}) to /api/agents", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({ name: "foo", status: "provisioning" }),
    );

    const body = {
      name: "foo",
      persona: "helper",
      team: "growth",
      skills: [{ id: "read-code", team: "shared" }],
      model: "gpt-5",
      purpose_tags: ["billing", "support"],
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
        purpose_tags: [],
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

  it("fetchSlackManifest GETs /api/agents/{name}/slack/manifest and unwraps {manifest} into the plain dict", async () => {
    const manifest = {
      display_information: { name: "clio", description: "A keeper agent." },
      features: { bot_user: { display_name: "clio" } },
    };
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ manifest }));

    await expect(fetchSlackManifest("clio")).resolves.toEqual(manifest);

    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/slack/manifest");
  });

  it("fetchSlackManifest encodes the agent name in the URL", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ manifest: {} }));

    await fetchSlackManifest("a b");

    expect(fetch).toHaveBeenCalledWith("/api/agents/a%20b/slack/manifest");
  });

  it("fetchSlackManifest surfaces a 409's {error} message (agent not ready)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "agent is not ready to connect to Slack yet" },
        { status: 409, statusText: "Conflict" },
      ),
    );

    await expect(fetchSlackManifest("clio")).rejects.toThrow(
      "agent is not ready to connect to Slack yet",
    );
  });

  it("connectSlack POSTs {app_id, bot_token, app_token} to /api/agents/{name}/slack/connect and returns {sha}", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ sha: "abc123" }));

    const body = {
      app_id: "A1234567",
      bot_token: "xoxb-secret-token",
      app_token: "xapp-secret-token",
    };

    await expect(connectSlack("clio", body)).resolves.toEqual({
      sha: "abc123",
    });

    // Assert the exact request, including the raw body bytes — the id and
    // token values must pass through untouched, not be redacted or
    // transformed.
    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/slack/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const [, init] = vi.mocked(fetch).mock.calls[0];
    expect(init?.body).toContain("A1234567");
    expect(init?.body).toContain("xoxb-secret-token");
    expect(init?.body).toContain("xapp-secret-token");
  });

  it("connectSlack encodes the agent name in the URL", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ sha: "abc123" }));

    await connectSlack("a b", {
      app_id: "A1234567",
      bot_token: "xoxb-secret",
      app_token: "xapp-secret",
    });

    expect(fetch).toHaveBeenCalledWith(
      "/api/agents/a%20b/slack/connect",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("connectSlack never logs the app id or token values it's given", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ sha: "abc123" }));
    const logSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});

    await connectSlack("clio", {
      app_id: "A1234567",
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

  it("connectSlack surfaces a 409's {error} message (no channels saved yet)", async () => {
    // Two-lane Slack (2026-08-17): connect no longer carries a channel at
    // all — the channel-format validation moved entirely to
    // saveSlackChannels/PUT .../slack/channels. What connect itself can
    // still reject on is the server finding no channels saved yet.
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "Add at least one channel first." },
        { status: 409, statusText: "Conflict" },
      ),
    );

    await expect(
      connectSlack("clio", {
        app_id: "A1234567",
        bot_token: "xoxb-secret",
        app_token: "xapp-secret",
      }),
    ).rejects.toThrow("Add at least one channel first.");
  });

  it("connectSlack surfaces a 422's {error} message (bad app id)", async () => {
    // Slack self-service (2026-08-17): app_id is validated first, ahead of
    // either token's format.
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "that doesn't look like a Slack app ID (it starts with A)" },
        { status: 422, statusText: "Unprocessable Entity" },
      ),
    );

    await expect(
      connectSlack("clio", {
        app_id: "wrong-shape",
        bot_token: "xoxb-secret",
        app_token: "xapp-secret",
      }),
    ).rejects.toThrow("that doesn't look like a Slack app ID (it starts with A)");
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
        app_id: "A1234567",
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
        app_id: "A1234567",
        bot_token: "xoxb-secret",
        app_token: "wrong-prefix",
      }),
    ).rejects.toThrow("that doesn't look like a Slack app-level token");
  });

  it("connectSlack surfaces the plain 502 {error} message (setup failed)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "couldn't set up Slack. Try again." },
        { status: 502, statusText: "Bad Gateway" },
      ),
    );

    await expect(
      connectSlack("clio", {
        app_id: "A1234567",
        bot_token: "xoxb-secret",
        app_token: "xapp-secret",
      }),
    ).rejects.toThrow("couldn't set up Slack. Try again.");
  });

  it("saveSlackChannels PUTs {channels} to /api/agents/{name}/slack/channels and returns {channels, state}", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({ channels: ["C123456"], state: "waiting" }),
    );

    await expect(saveSlackChannels("clio", ["C123456"])).resolves.toEqual({
      channels: ["C123456"],
      state: "waiting",
    });

    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/slack/channels", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channels: ["C123456"] }),
    });
  });

  it("saveSlackChannels resolves a sha when the save fires a rewire", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({ channels: ["C123456"], state: "connected", sha: "abc123" }),
    );

    await expect(saveSlackChannels("clio", ["C123456"])).resolves.toEqual({
      channels: ["C123456"],
      state: "connected",
      sha: "abc123",
    });
  });

  it("saveSlackChannels encodes the agent name in the URL", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ channels: [], state: "idle" }));

    await saveSlackChannels("a b", []);

    expect(fetch).toHaveBeenCalledWith(
      "/api/agents/a%20b/slack/channels",
      expect.objectContaining({ method: "PUT" }),
    );
  });

  it("saveSlackChannels surfaces a 422's {error} message (bad channel)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "that doesn't look like a channel ID (it starts with C)" },
        { status: 422, statusText: "Unprocessable Entity" },
      ),
    );

    await expect(saveSlackChannels("clio", ["general"])).rejects.toThrow(
      "that doesn't look like a channel ID (it starts with C)",
    );
  });

  it("saveSlackChannels surfaces a 422's {error} message (too many channels)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "Eight channels at most for now." },
        { status: 422, statusText: "Unprocessable Entity" },
      ),
    );

    await expect(
      saveSlackChannels("clio", ["C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9"]),
    ).rejects.toThrow("Eight channels at most for now.");
  });

  it("saveSlackChannels surfaces the plain 502 {error} message (rewire failed)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "couldn't update the channels. Try again." },
        { status: 502, statusText: "Bad Gateway" },
      ),
    );

    await expect(saveSlackChannels("clio", ["C123456"])).rejects.toThrow(
      "couldn't update the channels. Try again.",
    );
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

  it("uploadAvatar PUTs the raw bytes with an image/png content type and returns {sha}", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ sha: "def456" }));
    const bytes = new Uint8Array([0x89, 0x50, 0x4e, 0x47]).buffer;

    await expect(uploadAvatar("clio", bytes)).resolves.toEqual({ sha: "def456" });

    expect(fetch).toHaveBeenCalledWith("/api/agents/clio/avatar", {
      method: "PUT",
      headers: { "Content-Type": "image/png" },
      body: bytes,
    });
  });

  it("uploadAvatar encodes the agent name in the URL", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ sha: "def456" }));

    await uploadAvatar("a b", new Uint8Array([1]).buffer);

    expect(fetch).toHaveBeenCalledWith(
      "/api/agents/a%20b/avatar",
      expect.objectContaining({ method: "PUT" }),
    );
  });

  it("uploadAvatar surfaces a 422's {error} message verbatim (e.g. PNG only for now.)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "PNG only for now." },
        { status: 422, statusText: "Unprocessable Entity" },
      ),
    );

    await expect(uploadAvatar("clio", new Uint8Array([1]).buffer)).rejects.toThrow(
      "PNG only for now.",
    );
  });

  it("uploadAvatar surfaces the oversize 422's {error} message verbatim", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "That image is too large. Two megabytes at most." },
        { status: 422, statusText: "Unprocessable Entity" },
      ),
    );

    await expect(uploadAvatar("clio", new Uint8Array([1]).buffer)).rejects.toThrow(
      "That image is too large. Two megabytes at most.",
    );
  });

  it("uploadAvatar surfaces a 409's {error} message (agent not ready)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "agent is not ready for an avatar yet" },
        { status: 409, statusText: "Conflict" },
      ),
    );

    await expect(uploadAvatar("clio", new Uint8Array([1]).buffer)).rejects.toThrow(
      "agent is not ready for an avatar yet",
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

  it("fetchTeamOptions GETs /api/teams and returns {teams, default} verbatim", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({ teams: ["growth", "platform"], default: "shared" }),
    );

    await expect(fetchTeamOptions()).resolves.toEqual({
      teams: ["growth", "platform"],
      default: "shared",
    });

    expect(fetch).toHaveBeenCalledWith("/api/teams");
  });

  it("patchAgentMetadata PATCHes the submitted fields to /api/agents/{name}/metadata and returns {team, purpose_tags, capability, sha}", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({
        team: "growth",
        purpose_tags: ["billing"],
        capability: "Answers billing questions.",
        sha: "abc123",
      }),
    );

    const body = { team: "growth", purpose_tags: ["billing"] };
    await expect(patchAgentMetadata("clio", body)).resolves.toEqual({
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
  });

  it("patchAgentMetadata surfaces a 422's {error} message (bad team slug)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "that doesn't look like a team name" },
        { status: 422, statusText: "Unprocessable Entity" },
      ),
    );

    await expect(
      patchAgentMetadata("clio", { team: "Not A Slug" }),
    ).rejects.toThrow("that doesn't look like a team name");
  });

  it("regenerateAgentMetadata POSTs to /api/agents/{name}/metadata/regenerate with no body and returns {team, purpose_tags, capability, sha}", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({
        team: "shared",
        purpose_tags: [],
        capability: "Regenerated capability line.",
        sha: "def456",
      }),
    );

    await expect(regenerateAgentMetadata("clio")).resolves.toEqual({
      team: "shared",
      purpose_tags: [],
      capability: "Regenerated capability line.",
      sha: "def456",
    });

    expect(fetch).toHaveBeenCalledWith(
      "/api/agents/clio/metadata/regenerate",
      { method: "POST" },
    );
  });

  it("regenerateAgentMetadata surfaces a 502's {error} message (card commit failure)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(
        { error: "couldn't update the directory details. Try again." },
        { status: 502, statusText: "Bad Gateway" },
      ),
    );

    await expect(regenerateAgentMetadata("clio")).rejects.toThrow(
      "couldn't update the directory details. Try again.",
    );
  });

  it("createPersona POSTs the teamless form body to /api/personas and returns the created CatalogEntry", async () => {
    const body = {
      name: "helper",
      who_they_are: "an unflappable network detective who's seen every outage twice",
      what_they_do: "helps with things",
      rules: ["no secrets"],
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
      }),
    ).rejects.toThrow("catalogs aren't set up yet");
  });

  it("draftPersona POSTs {job, rules} to /api/personas/draft and returns the draft verbatim, purpose_tags included", async () => {
    const draft = {
      name: "billing-helper",
      who_they_are: "a calm guide through confusing invoices",
      what_they_do: "Answers billing questions and escalates refunds.",
      purpose_tags: ["billing", "support"],
    };
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(draft));

    await expect(
      draftPersona("Help customers with billing questions.", ["never guess"]),
    ).resolves.toEqual(draft);

    expect(fetch).toHaveBeenCalledWith("/api/personas/draft", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job: "Help customers with billing questions.",
        rules: ["never guess"],
      }),
    });
  });

  it("draftPersona sends an empty rules array when none are given", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({
        name: "new-agent",
        who_they_are: "an agent with one clear job",
        what_they_do: "Does the job.",
      }),
    );

    await draftPersona("Does the job.", []);

    expect(fetch).toHaveBeenCalledWith("/api/personas/draft", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job: "Does the job.", rules: [] }),
    });
  });

  it("draftPersona surfaces a transport failure (a 502 proxy error) as an ApiError", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      textResponse("<html>502 Bad Gateway</html>", {
        status: 502,
        statusText: "Bad Gateway",
      }),
    );

    await expect(draftPersona("Does the job.", [])).rejects.toBeInstanceOf(
      ApiError,
    );
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

describe("fetchWhoami", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("GETs /api/whoami and returns {email, operator, groups} verbatim", async () => {
    const whoami = { email: "ops@example.com", operator: true, groups: [] };
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(whoami));

    await expect(fetchWhoami()).resolves.toEqual(whoami);

    expect(fetch).toHaveBeenCalledWith("/api/whoami");
  });

  it("resolves {email: null, operator: false, groups: []} when signed out", async () => {
    const whoami = { email: null, operator: false, groups: [] };
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(whoami));

    await expect(fetchWhoami()).resolves.toEqual(whoami);
  });
});

describe("listColleagueConversations", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("GETs /api/colleagues/conversations (no query string) when all is false", async () => {
    const conversations = [
      {
        task_id: "t-1",
        target_agent: "clio",
        target_generation: 1,
        caller_agent: "agent:iris",
        state: "completed",
        cabal_escalated_from: null,
        created_at: "2026-08-19T10:00:00Z",
        updated_at: "2026-08-19T10:00:05Z",
        result_snippet: "Sure, here's how.",
      },
    ];
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ conversations }));

    await expect(listColleagueConversations(false)).resolves.toEqual(conversations);
    expect(fetch).toHaveBeenCalledWith("/api/colleagues/conversations");
  });

  it("GETs /api/colleagues/conversations?all=true when all is true", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ conversations: [] }));

    await listColleagueConversations(true);

    expect(fetch).toHaveBeenCalledWith("/api/colleagues/conversations?all=true");
  });

  it("a 409 (switchboard not configured) rejects with an ApiError carrying that status", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({ error: "the switchboard is not configured" }, { status: 409 }),
    );

    await expect(listColleagueConversations(false)).rejects.toMatchObject({
      status: 409,
    });
  });
});

describe("fetchColleagueConversation", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("GETs /api/colleagues/conversations/{task_id} and returns the body verbatim", async () => {
    const detail = {
      conversation: { task_id: "t-1", result: "The full answer." },
      events: [
        { id: 1, event_type: "dispatch_start", payload: {}, created_at: "x" },
      ],
    };
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(detail));

    await expect(fetchColleagueConversation("t-1")).resolves.toEqual(detail);
    expect(fetch).toHaveBeenCalledWith("/api/colleagues/conversations/t-1");
  });

  it("encodes the task id in the URL", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({ conversation: {}, events: [] }),
    );

    await fetchColleagueConversation("t/1");

    expect(fetch).toHaveBeenCalledWith("/api/colleagues/conversations/t%2F1");
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

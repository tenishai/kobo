// @vitest-environment jsdom
//
// Connected agents (design 2026-09-21): the one-station Connect form. The
// two things worth pinning here are the order of the work (Connect stays
// shut until a scan of the CURRENT answers came back clean) and that every
// sentence about a repository is the server's own, rendered verbatim.
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ConnectModal from "./ConnectModal.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const HTTP_CHOICE = "It listens on a port (the platform checks its health there)";
const HEADLESS_CHOICE = "It does not listen on a port (a chat bot or a worker)";
const OWNER_CHECKBOX = "I own this repository or have its owner's agreement";

const CLEAN_REPORT = {
  blockers: [],
  warnings: [],
  adds: ["gojira.yaml", "k8s/ (including k8s/base/serviceaccount.yaml)"],
  adds_preamble: "The room cannot check who owns this repository.",
  repo: "acme/release-bot",
};

/** ConnectModal fires `groupsQuery` on mount. Groups empty by default (the
 * group picker then renders nothing to choose and the form does not wait on
 * one), same "most cases predate groups" default CreateModal's own test
 * harness uses; a case that wants the picker overrides the key. */
function stubConnectRoutes(extra: Record<string, unknown> = {}): void {
  stubFetch({
    "/api/catalog/groups": { groups: [] },
    ...extra,
    "/api/": {},
  });
}

/** Fills in a repository and a name, which is everything Scan needs when no
 * groups are configured. Waits for the group list to have actually arrived
 * first (the form will not scan before it has, so that a room that requires
 * a group cannot be scanned without one) -- with no groups configured, the
 * "none are configured" line is that list arriving. */
async function fillTheForm(): Promise<void> {
  await screen.findByText("No groups are configured yet.");
  await fireEvent.input(screen.getByLabelText("Repository"), {
    target: { value: "acme/release-bot" },
  });
  await fireEvent.input(screen.getByLabelText("Name"), {
    target: { value: "release-bot" },
  });
}

function scanButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: "Scan" }) as HTMLButtonElement;
}

function connectButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: "Connect" }) as HTMLButtonElement;
}

describe("ConnectModal — the form", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubConnectRoutes();
  });

  it("asks for a repository, a name, how it runs, and the owner's word", async () => {
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    expect(await screen.findByLabelText("Repository")).not.toBeNull();
    expect(screen.getByLabelText("Name")).not.toBeNull();
    expect(screen.getByRole("radio", { name: HTTP_CHOICE })).not.toBeNull();
    expect(screen.getByRole("radio", { name: HEADLESS_CHOICE })).not.toBeNull();
    expect(screen.getByRole("checkbox", { name: OWNER_CHECKBOX })).not.toBeNull();
  });

  it("says the agent only ever gets an in-cluster address, either way it runs", async () => {
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    expect(
      await screen.findByText(
        "Either way the agent gets an address inside the cluster only. Nothing here makes it reachable from outside.",
      ),
    ).not.toBeNull();
  });

  it("keeps Scan and Connect shut on an empty form", async () => {
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await screen.findByLabelText("Repository");
    expect(scanButton().disabled).toBe(true);
    expect(connectButton().disabled).toBe(true);
  });

  it("keeps Scan shut until a group is picked in a room that has them", async () => {
    stubConnectRoutes({
      "/api/catalog/groups": { groups: [{ name: "platform", kind: "engineering" }] },
    });
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    const groupSelect = (await screen.findByLabelText("Group")) as HTMLSelectElement;
    await fireEvent.input(screen.getByLabelText("Repository"), {
      target: { value: "acme/release-bot" },
    });
    await fireEvent.input(screen.getByLabelText("Name"), {
      target: { value: "release-bot" },
    });
    expect(scanButton().disabled).toBe(true);

    await fireEvent.change(groupSelect, { target: { value: "platform" } });
    expect(scanButton().disabled).toBe(false);
  });

  it("opens Scan once the repository and the name are filled in, and leaves Connect shut", async () => {
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await fillTheForm();

    expect(scanButton().disabled).toBe(false);
    expect(connectButton().disabled).toBe(true);
  });
});

describe("ConnectModal — the scan report", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders every blocker message and keeps Connect shut", async () => {
    stubConnectRoutes({
      "/api/connect/scan": {
        ...CLEAN_REPORT,
        blockers: [
          {
            code: "no_dockerfile",
            message: "There is no Dockerfile at the root of this repository.",
          },
          {
            code: "repo_not_writable",
            message: "This room can read acme/release-bot but cannot change it.",
          },
        ],
      },
    });
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await fillTheForm();
    await fireEvent.click(scanButton());

    expect(
      await screen.findByText(
        "There is no Dockerfile at the root of this repository.",
      ),
    ).not.toBeNull();
    expect(
      screen.getByText("This room can read acme/release-bot but cannot change it."),
    ).not.toBeNull();
    await fireEvent.click(screen.getByRole("checkbox", { name: OWNER_CHECKBOX }));
    expect(connectButton().disabled).toBe(true);
  });

  it("shows warnings and what the room will add, without blocking", async () => {
    stubConnectRoutes({
      "/api/connect/scan": {
        ...CLEAN_REPORT,
        warnings: [
          { code: "env_file_committed", message: "A .env file is committed here." },
        ],
      },
    });
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await fillTheForm();
    await fireEvent.click(scanButton());

    expect(await screen.findByText("A .env file is committed here.")).not.toBeNull();
    expect(
      screen.getByText("The room cannot check who owns this repository."),
    ).not.toBeNull();
    expect(screen.getByText("gojira.yaml")).not.toBeNull();
  });

  it("opens Connect only once a clean scan and the owner's word are both in", async () => {
    stubConnectRoutes({ "/api/connect/scan": CLEAN_REPORT });
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await fillTheForm();
    await fireEvent.click(scanButton());

    await screen.findByText("Nothing is in the way.");
    // Clean, but nobody has answered for the repository yet.
    expect(connectButton().disabled).toBe(true);

    await fireEvent.click(screen.getByRole("checkbox", { name: OWNER_CHECKBOX }));
    expect(connectButton().disabled).toBe(false);
  });

  it("shuts Connect again the moment a field is edited, since the report answered for the old values", async () => {
    stubConnectRoutes({ "/api/connect/scan": CLEAN_REPORT });
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await fillTheForm();
    await fireEvent.click(scanButton());
    await screen.findByText("Nothing is in the way.");
    await fireEvent.click(screen.getByRole("checkbox", { name: OWNER_CHECKBOX }));
    expect(connectButton().disabled).toBe(false);

    await fireEvent.input(screen.getByLabelText("Repository"), {
      target: { value: "acme/other-bot" },
    });

    expect(connectButton().disabled).toBe(true);
    expect(
      screen.getByText("Something changed since this scan. Scan again before connecting."),
    ).not.toBeNull();
  });

  it("disables the no-HTTP-port option, with the room's own reason, once a scan says the room has no class for it", async () => {
    stubConnectRoutes({
      "/api/connect/scan": {
        ...CLEAN_REPORT,
        blockers: [
          {
            code: "headless_unavailable",
            message: "agents with no HTTP port are not available in this room yet",
          },
        ],
      },
    });
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await fillTheForm();
    await fireEvent.click(screen.getByRole("radio", { name: HEADLESS_CHOICE }));
    await fireEvent.click(scanButton());

    await waitFor(() => {
      expect(
        (screen.getByRole("radio", { name: HEADLESS_CHOICE }) as HTMLInputElement)
          .disabled,
      ).toBe(true);
    });
    // The room's sentence, not a reworded one. It appears both as the
    // blocker and beside the option it disables.
    expect(
      screen.getAllByText(
        "agents with no HTTP port are not available in this room yet",
      ).length,
    ).toBeGreaterThan(0);

    const headless = screen.getByRole("radio", {
      name: HEADLESS_CHOICE,
    }) as HTMLInputElement;
    const http = screen.getByRole("radio", { name: HTTP_CHOICE }) as HTMLInputElement;
    expect(http.disabled).toBe(false);
    // The answer moves off the option that was just switched off, so the
    // form is still one a person can finish.
    expect(http.checked).toBe(true);
    expect(headless.checked).toBe(false);
    // And the reason is tied to the control, not only printed under it.
    expect(headless.getAttribute("aria-describedby")).toBe("connect-headless-reason");
    expect(document.getElementById("connect-headless-reason")?.textContent).toContain(
      "agents with no HTTP port are not available in this room yet",
    );
  });

});

describe("ConnectModal — connecting", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts the repository, the name, the run mode, empty secret names and the confirmation", async () => {
    stubConnectRoutes({
      "/api/connect/scan": CLEAN_REPORT,
      "/api/agents/connect": { name: "release-bot", status: "pending" },
    });
    const onclose = vi.fn();
    renderWithClient(ConnectModal, { open: true, onclose });

    await fillTheForm();
    await fireEvent.click(scanButton());
    await screen.findByText("Nothing is in the way.");
    await fireEvent.click(screen.getByRole("checkbox", { name: OWNER_CHECKBOX }));
    await fireEvent.click(connectButton());

    await waitFor(() => {
      expect(
        vi.mocked(fetch).mock.calls.some(
          ([url, init]) =>
            String(url) === "/api/agents/connect" &&
            (init as RequestInit | undefined)?.method === "POST",
        ),
      ).toBe(true);
    });
    const call = vi
      .mocked(fetch)
      .mock.calls.find(
        ([url, init]) =>
          String(url) === "/api/agents/connect" &&
          (init as RequestInit | undefined)?.method === "POST",
      );
    const body = JSON.parse((call?.[1] as RequestInit).body as string);
    expect(body.repo).toBe("acme/release-bot");
    expect(body.name).toBe("release-bot");
    expect(body.run_mode).toBe("http");
    expect(body.secret_names).toEqual([]);
    expect(body.confirm_repo_owner).toBe(true);
    await waitFor(() => {
      expect(onclose).toHaveBeenCalled();
    });
  });

  it("renders the server's own blockers when its re-scan refuses the connect", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/agents/connect") && init?.method === "POST") {
          return new Response(
            JSON.stringify({
              error: "this repository is not ready to connect yet",
              blockers: [
                {
                  code: "already_deployed",
                  message: "acme/release-bot is already connected in this room as bot.",
                },
              ],
            }),
            { status: 422, headers: { "Content-Type": "application/json" } },
          );
        }
        const routes: Record<string, unknown> = {
          "/api/catalog/groups": { groups: [] },
          "/api/connect/scan": CLEAN_REPORT,
          "/api/": {},
        };
        for (const [needle, payload] of Object.entries(routes)) {
          if (url.includes(needle)) {
            return new Response(JSON.stringify(payload), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            });
          }
        }
        throw new Error(`unstubbed fetch: ${url}`);
      }),
    );
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await fillTheForm();
    await fireEvent.click(scanButton());
    await screen.findByText("Nothing is in the way.");
    await fireEvent.click(screen.getByRole("checkbox", { name: OWNER_CHECKBOX }));
    await fireEvent.click(connectButton());

    expect(
      await screen.findByText(
        "acme/release-bot is already connected in this room as bot.",
      ),
    ).not.toBeNull();
    expect(connectButton().disabled).toBe(true);

    // The refusal answered for the repository that was sent, so it stops
    // being shown once a different one is named.
    await fireEvent.input(screen.getByLabelText("Repository"), {
      target: { value: "acme/other-bot" },
    });
    expect(
      screen.queryByText("acme/release-bot is already connected in this room as bot."),
    ).toBeNull();
  });

  it("puts a taken name on the name field", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/agents/connect") && init?.method === "POST") {
          return new Response(
            JSON.stringify({ error: "name taken: 'release-bot'" }),
            { status: 409, headers: { "Content-Type": "application/json" } },
          );
        }
        const routes: Record<string, unknown> = {
          "/api/catalog/groups": { groups: [] },
          "/api/connect/scan": CLEAN_REPORT,
          "/api/": {},
        };
        for (const [needle, payload] of Object.entries(routes)) {
          if (url.includes(needle)) {
            return new Response(JSON.stringify(payload), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            });
          }
        }
        throw new Error(`unstubbed fetch: ${url}`);
      }),
    );
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await fillTheForm();
    await fireEvent.click(scanButton());
    await screen.findByText("Nothing is in the way.");
    await fireEvent.click(screen.getByRole("checkbox", { name: OWNER_CHECKBOX }));
    await fireEvent.click(connectButton());

    expect(
      await screen.findByText("That name is already taken in this room."),
    ).not.toBeNull();
    // Connect stays shut on the name that was refused: pressing it again
    // would only earn the same answer.
    expect(connectButton().disabled).toBe(true);
  });

  it("shows the room's own sentence when GitHub could not be read", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/connect/scan") && init?.method === "POST") {
          return new Response(
            JSON.stringify({
              error: "the repository could not be read right now, try again shortly",
            }),
            { status: 502, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/api/catalog/groups")) {
          return new Response(JSON.stringify({ groups: [] }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response(JSON.stringify({}), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await fillTheForm();
    await fireEvent.click(scanButton());

    expect(
      await screen.findByText(
        "the repository could not be read right now, try again shortly",
      ),
    ).not.toBeNull();
    expect(connectButton().disabled).toBe(true);
  });
});

// --- the repository field's typeahead ---------------------------------------
//
// The field searches the repositories the room's GitHub App can already
// reach (`GET /api/connect/repos`). It never offers a list: nothing is
// shown on focus or on a click, and rows arrive only once somebody types.

const REPO_MATCHES = {
  repos: [
    { full_name: "example-org/team-chatbot", private: true },
    { full_name: "example-org/chat-archive", private: false },
  ],
  truncated: false,
};

const SEARCH_UNAVAILABLE =
  "Repository search is unavailable right now. You can still type owner/name.";

/** How many times the repository search has actually been called. The
 * debounce and the picking rules are both about NOT calling it. */
function repoSearchCalls(): number {
  const stub = globalThis.fetch as unknown as {
    mock: { calls: [RequestInfo | URL][] };
  };
  return stub.mock.calls.filter(([input]) =>
    String(input).includes("/api/connect/repos"),
  ).length;
}

/** Waits out the field's debounce (200ms) and a little more, for the
 * cases that assert nothing was requested. */
function afterTheDebounce(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 350));
}

function repoInput(): HTMLInputElement {
  return screen.getByLabelText("Repository") as HTMLInputElement;
}

async function typeRepo(value: string): Promise<void> {
  await fireEvent.input(repoInput(), { target: { value } });
}

describe("ConnectModal — finding a repository", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubConnectRoutes({ "/api/connect/repos": REPO_MATCHES });
  });

  it("offers the repositories that match what has been typed", async () => {
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await screen.findByText("No groups are configured yet.");
    await typeRepo("chat");

    const options = await screen.findAllByRole("option");
    expect(
      options.map((option) =>
        (option.textContent ?? "").replace(/\s+/g, " ").trim(),
      ),
    ).toEqual([
      "example-org/team-chatbot Private",
      "example-org/chat-archive Public",
    ]);
    expect(repoInput().getAttribute("aria-expanded")).toBe("true");
  });

  it("offers nothing while the field is empty", async () => {
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await screen.findByText("No groups are configured yet.");
    await fireEvent.focus(repoInput());
    await fireEvent.click(repoInput());

    expect(screen.queryByRole("listbox")).toBeNull();
    expect(repoInput().getAttribute("aria-expanded")).toBe("false");
  });

  it("fills the field with the highlighted repository when Enter is pressed", async () => {
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await screen.findByText("No groups are configured yet.");
    await typeRepo("chat");
    await screen.findAllByRole("option");

    await fireEvent.keyDown(repoInput(), { key: "ArrowDown" });
    await fireEvent.keyDown(repoInput(), { key: "Enter" });

    expect(repoInput().value).toBe("example-org/team-chatbot");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("closes the list on Escape and leaves what was typed alone", async () => {
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await screen.findByText("No groups are configured yet.");
    await typeRepo("chat");
    await screen.findAllByRole("option");

    await fireEvent.keyDown(repoInput(), { key: "Escape" });

    expect(screen.queryByRole("listbox")).toBeNull();
    expect(repoInput().value).toBe("chat");
  });

  it("closes the list on Escape without closing the form", async () => {
    // The first Escape is about the list. The second is about the form.
    const onclose = vi.fn();
    renderWithClient(ConnectModal, { open: true, onclose });

    await screen.findByText("No groups are configured yet.");
    await typeRepo("chat");
    await screen.findAllByRole("option");

    await fireEvent.keyDown(repoInput(), { key: "Escape" });

    expect(screen.queryByRole("listbox")).toBeNull();
    expect(onclose).not.toHaveBeenCalled();

    await fireEvent.keyDown(repoInput(), { key: "Escape" });

    expect(onclose).toHaveBeenCalled();
  });

  it("reopens the list on ArrowDown once something has been typed", async () => {
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await screen.findByText("No groups are configured yet.");
    await typeRepo("chat");
    await screen.findAllByRole("option");
    await fireEvent.keyDown(repoInput(), { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();

    await fireEvent.keyDown(repoInput(), { key: "ArrowDown" });

    expect(screen.getByRole("listbox")).not.toBeNull();
  });

  it("offers nothing on ArrowDown while the field is empty", async () => {
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await screen.findByText("No groups are configured yet.");
    await fireEvent.keyDown(repoInput(), { key: "ArrowDown" });

    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("shows no rows for a term that has been typed past", async () => {
    // A keystroke inside the debounce means the rows on screen answer a
    // question nobody is asking any more.
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await screen.findByText("No groups are configured yet.");
    await typeRepo("cha");
    await screen.findAllByRole("option");

    await typeRepo("chat");

    expect(screen.queryByRole("listbox")).toBeNull();
    expect((await screen.findAllByRole("option")).length).toBe(2);
  });

  it("says when the count of matches changes, for anyone listening", async () => {
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await screen.findByText("No groups are configured yet.");
    await typeRepo("chat");

    expect(await screen.findByText("2 repositories")).not.toBeNull();
  });

  it("does not search for the repository it just filled in", async () => {
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await screen.findByText("No groups are configured yet.");
    await typeRepo("chat");
    const options = await screen.findAllByRole("option");
    const searched = repoSearchCalls();

    await fireEvent.click(options[0]);
    await afterTheDebounce();

    expect(repoInput().value).toBe("example-org/team-chatbot");
    expect(repoSearchCalls()).toBe(searched);
  });

  it("does not search for what was typed as the form was closing", async () => {
    const view = renderWithClient(ConnectModal, {
      open: true,
      onclose: () => {},
    });

    await screen.findByText("No groups are configured yet.");
    const searched = repoSearchCalls();
    await typeRepo("chat");
    await view.rerender({ componentProps: { open: false, onclose: () => {} } });
    await afterTheDebounce();

    expect(repoSearchCalls()).toBe(searched);
  });

  it("fills the field with the repository that was clicked", async () => {
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await screen.findByText("No groups are configured yet.");
    await typeRepo("chat");
    const options = await screen.findAllByRole("option");

    await fireEvent.click(options[1]);

    expect(repoInput().value).toBe("example-org/chat-archive");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("says so when the repository search cannot be reached", async () => {
    // Fail-soft on the surface: the field still takes a typed owner/name,
    // and the scan is still the thing that answers for it.
    vi.unstubAllGlobals();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/connect/repos")) {
          return new Response(
            JSON.stringify({ error: "the room could not list its repositories" }),
            { status: 502, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/api/catalog/groups")) {
          return new Response(JSON.stringify({ groups: [] }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response(JSON.stringify({}), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await screen.findByText("No groups are configured yet.");
    await typeRepo("chat");

    expect(await screen.findByText(SEARCH_UNAVAILABLE)).not.toBeNull();
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(repoInput().value).toBe("chat");
  });

  it("does not pick a row that is no longer there", async () => {
    // The rows can change under a highlighted one: a refetch lands, and
    // the list is shorter than it was a moment ago.
    let searches = 0;
    vi.unstubAllGlobals();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/connect/repos")) {
          searches += 1;
          return new Response(
            JSON.stringify(
              searches === 1
                ? REPO_MATCHES
                : { repos: [REPO_MATCHES.repos[0]], truncated: false },
            ),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/api/catalog/groups")) {
          return new Response(JSON.stringify({ groups: [] }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response(JSON.stringify({}), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    const { client } = renderWithClient(ConnectModal, {
      open: true,
      onclose: () => {},
    });

    await screen.findByText("No groups are configured yet.");
    await typeRepo("chat");
    await screen.findAllByRole("option");
    await fireEvent.keyDown(repoInput(), { key: "ArrowDown" });
    await fireEvent.keyDown(repoInput(), { key: "ArrowDown" });
    expect(repoInput().getAttribute("aria-activedescendant")).toBe(
      "connect-repo-list-1",
    );

    await client.invalidateQueries({ queryKey: ["connect", "repos"] });
    await waitFor(() => expect(screen.getAllByRole("option").length).toBe(1));

    // The highlight went with the row it was on, so Enter has nothing to
    // pick and what was typed is left alone.
    expect(repoInput().getAttribute("aria-activedescendant")).toBeNull();
    await fireEvent.keyDown(repoInput(), { key: "Enter" });

    expect(repoInput().value).toBe("chat");
  });

  it("says when more repositories matched than it can show", async () => {
    vi.unstubAllGlobals();
    stubConnectRoutes({
      "/api/connect/repos": { ...REPO_MATCHES, truncated: true },
    });
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await screen.findByText("No groups are configured yet.");
    await typeRepo("chat");

    expect(
      await screen.findByText("More matched. Keep typing to narrow it down."),
    ).not.toBeNull();
  });
});

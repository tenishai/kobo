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

const HTTP_CHOICE = "It serves HTTP on port 8080 and answers /healthz";
const HEADLESS_CHOICE = "It has no HTTP port (a chat bot or a worker)";
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
 * groups are configured. */
async function fillTheForm(): Promise<void> {
  await fireEvent.input(await screen.findByLabelText("Repository"), {
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

  it("asks for a repository, a name, how it runs, secret names, and the owner's word", async () => {
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    expect(await screen.findByLabelText("Repository")).not.toBeNull();
    expect(screen.getByLabelText("Name")).not.toBeNull();
    expect(screen.getByRole("radio", { name: HTTP_CHOICE })).not.toBeNull();
    expect(screen.getByRole("radio", { name: HEADLESS_CHOICE })).not.toBeNull();
    expect(screen.getByLabelText("Secret names (optional)")).not.toBeNull();
    expect(screen.getByRole("checkbox", { name: OWNER_CHECKBOX })).not.toBeNull();
  });

  it("keeps Scan and Connect shut on an empty form", async () => {
    renderWithClient(ConnectModal, { open: true, onclose: () => {} });

    await screen.findByLabelText("Repository");
    expect(scanButton().disabled).toBe(true);
    expect(connectButton().disabled).toBe(true);
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
    expect(
      (screen.getByRole("radio", { name: HTTP_CHOICE }) as HTMLInputElement).disabled,
    ).toBe(false);
  });
});

describe("ConnectModal — connecting", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts the repository, the name, the run mode, the secret names and the confirmation", async () => {
    stubConnectRoutes({
      "/api/connect/scan": CLEAN_REPORT,
      "/api/agents/connect": { name: "release-bot", status: "pending" },
    });
    const onclose = vi.fn();
    renderWithClient(ConnectModal, { open: true, onclose });

    await fillTheForm();
    await fireEvent.input(screen.getByLabelText("Secret names (optional)"), {
      target: { value: "release-token, slack-token" },
    });
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
    expect(body.secret_names).toEqual(["release-token", "slack-token"]);
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

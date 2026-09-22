// @vitest-environment jsdom
//
// The five things the design asks this panel to prove: it shows names and
// never a value, it parses a pasted .env and refuses a bad line, it refuses
// a key the room sets itself, it says so plainly in a room with no secrets
// platform, and it tells a connected agent's owner their change is waiting
// in a pull request.
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SecretsPanel from "./SecretsPanel.svelte";
import { renderWithClient } from "../test-utils/render";

const AGENT = "clio";

// Never a real credential: a placeholder standing in for one, so a test can
// assert it is typed in and never rendered back.
const VALUE = "not-a-real-key-value";

type Routes = {
  secrets?: unknown;
  env?: unknown;
  onSecretsPut?: (body: { set: Record<string, string>; remove: string[] }) => unknown;
  onEnvPut?: (body: { set: Record<string, string>; remove: string[] }) => unknown;
};

/** A fetch stub that answers the panel's two reads and records its writes.
 * Local rather than `stubFetch`, because these cases care about the request
 * BODY a save sent (which keys, which removals), and the shared helper
 * answers by URL alone. */
function stubRoutes(routes: Routes) {
  const puts: { url: string; body: { set: Record<string, string>; remove: string[] } }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const json = (payload: unknown) =>
        new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });

      if (init?.method === "PUT") {
        const body = JSON.parse(String(init.body));
        puts.push({ url, body });
        const handler = url.includes("/secrets") ? routes.onSecretsPut : routes.onEnvPut;
        return json(handler ? handler(body) : { wiring: "committed", keys: [], env: {} });
      }
      if (url.includes("/secrets")) {
        return json(
          routes.secrets ?? {
            keys: [],
            updated_at: null,
            platform: true,
            secret_name: `${AGENT}-env`,
          },
        );
      }
      return json(routes.env ?? { env: {} });
    }),
  );
  return puts;
}

function render(props: Record<string, unknown> = {}) {
  return renderWithClient(SecretsPanel, {
    agentName: AGENT,
    frozen: false,
    ...props,
  });
}

describe("SecretsPanel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows secrets by name, and nothing that could be a value", async () => {
    stubRoutes({
      secrets: {
        keys: ["API_KEY", "DB_URL"],
        updated_at: "2026-09-22T10:00:00.000000Z",
        platform: true,
        secret_name: `${AGENT}-env`,
      },
    });
    const { container } = render();

    await waitFor(() => {
      expect(screen.getByText("API_KEY")).not.toBeNull();
    });
    expect(screen.getByText("DB_URL")).not.toBeNull();
    // No reveal, no mask, no "show" control to add one to.
    expect(screen.queryByRole("button", { name: /show|reveal/i })).toBeNull();
    // The value field is the only place a value ever appears, and it is
    // empty until somebody types in it.
    const fields = container.querySelectorAll("input");
    for (const field of fields) {
      expect((field as HTMLInputElement).value).not.toContain(VALUE);
    }
  });

  it("says so plainly when the room has no secrets platform, and keeps the environment list", async () => {
    stubRoutes({
      secrets: { keys: [], updated_at: null, platform: false, secret_name: null },
      env: { env: { API_BASE: "https://example.test" } },
    });
    render();

    await waitFor(() => {
      expect(
        screen.getByText(/This room has no secrets platform configured/),
      ).not.toBeNull();
    });
    // No add form for a list that cannot be written.
    expect(screen.queryByLabelText("Name")).toBeNull();
    // The other half still works, which is the point of checking rather
    // than hiding the whole panel.
    expect(screen.getByRole("button", { name: "Save variables" })).not.toBeNull();
    expect((screen.getByLabelText("Name 1") as HTMLInputElement).value).toBe("API_BASE");
  });

  it("refuses a key the room sets itself, before anything is sent", async () => {
    const puts = stubRoutes({});
    render();

    await waitFor(() => {
      expect(screen.getByLabelText("Name")).not.toBeNull();
    });
    await fireEvent.input(screen.getByLabelText("Name"), {
      target: { value: "SLACK_BOT_TOKEN" },
    });
    await fireEvent.input(screen.getByLabelText("Value"), { target: { value: VALUE } });

    expect(screen.getByText(/the room sets this one/)).not.toBeNull();
    expect(
      (screen.getByRole("button", { name: "Add secret" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(puts).toHaveLength(0);
  });

  it("refuses a key that is not a name at all", async () => {
    stubRoutes({});
    render();

    await waitFor(() => {
      expect(screen.getByLabelText("Name")).not.toBeNull();
    });
    await fireEvent.input(screen.getByLabelText("Name"), {
      target: { value: "api-key" },
    });

    expect(screen.getByText(/use capital letters, digits and underscores/)).not.toBeNull();
  });

  it("sends one secret with its value exactly once", async () => {
    const puts = stubRoutes({
      onSecretsPut: () => ({
        keys: ["API_KEY"],
        platform: true,
        secret_name: `${AGENT}-env`,
        wiring: "committed",
      }),
    });
    render();

    await waitFor(() => {
      expect(screen.getByLabelText("Name")).not.toBeNull();
    });
    await fireEvent.input(screen.getByLabelText("Name"), { target: { value: "API_KEY" } });
    await fireEvent.input(screen.getByLabelText("Value"), { target: { value: VALUE } });
    await fireEvent.click(screen.getByRole("button", { name: "Add secret" }));

    await waitFor(() => {
      expect(puts).toHaveLength(1);
    });
    expect(puts[0].body).toEqual({ set: { API_KEY: VALUE }, remove: [] });
    await waitFor(() => {
      expect(screen.getByText(/The agent picks this up the next time it starts/)).not.toBeNull();
    });
  });

  it("parses a pasted .env into one save", async () => {
    const puts = stubRoutes({
      onSecretsPut: () => ({
        keys: ["API_KEY", "DB_URL"],
        platform: true,
        secret_name: `${AGENT}-env`,
        wiring: "committed",
      }),
    });
    render();

    await waitFor(() => {
      expect(screen.getByLabelText("One KEY=value per line")).not.toBeNull();
    });
    await fireEvent.input(screen.getByLabelText("One KEY=value per line"), {
      target: {
        value: `# a comment\nAPI_KEY=${VALUE}\n\nexport DB_URL="postgres://example.test/db"\n`,
      },
    });
    await fireEvent.click(screen.getByRole("button", { name: "Add these" }));

    await waitFor(() => {
      expect(puts).toHaveLength(1);
    });
    expect(puts[0].body.set).toEqual({
      API_KEY: VALUE,
      DB_URL: "postgres://example.test/db",
    });
  });

  it("shows a bad .env line back and writes nothing until it is fixed", async () => {
    const puts = stubRoutes({});
    render();

    await waitFor(() => {
      expect(screen.getByLabelText("One KEY=value per line")).not.toBeNull();
    });
    await fireEvent.input(screen.getByLabelText("One KEY=value per line"), {
      target: { value: `API_KEY=${VALUE}\njust some words\nlower=1\n` },
    });
    await fireEvent.click(screen.getByRole("button", { name: "Add these" }));

    expect(screen.getByText("Line 2: write this as KEY=value.")).not.toBeNull();
    expect(
      screen.getByText(/Line 3: lower: use capital letters, digits and underscores/),
    ).not.toBeNull();
    expect(puts).toHaveLength(0);
  });

  it("tells a connected agent's owner the change is waiting in a pull request", async () => {
    stubRoutes({
      onSecretsPut: () => ({
        keys: ["API_KEY"],
        platform: true,
        secret_name: `${AGENT}-env`,
        wiring: "pull_request",
        pull_request: "https://example.test/pull/7",
      }),
    });
    render();

    await waitFor(() => {
      expect(screen.getByLabelText("Name")).not.toBeNull();
    });
    await fireEvent.input(screen.getByLabelText("Name"), { target: { value: "API_KEY" } });
    await fireEvent.input(screen.getByLabelText("Value"), { target: { value: VALUE } });
    await fireEvent.click(screen.getByRole("button", { name: "Add secret" }));

    await waitFor(() => {
      expect(
        screen.getByText(
          "Saved. The agent's files belong to its team, so the change is waiting in a pull request.",
        ),
      ).not.toBeNull();
    });
    const link = screen.getByRole("link", { name: "Open the pull request" });
    expect(link.getAttribute("href")).toBe("https://example.test/pull/7");
  });

  it("removes a secret by name", async () => {
    const puts = stubRoutes({
      secrets: {
        keys: ["API_KEY", "DB_URL"],
        updated_at: null,
        platform: true,
        secret_name: `${AGENT}-env`,
      },
      onSecretsPut: () => ({
        keys: ["DB_URL"],
        platform: true,
        secret_name: `${AGENT}-env`,
        wiring: "unchanged",
      }),
    });
    render();

    await waitFor(() => {
      expect(screen.getByText("API_KEY")).not.toBeNull();
    });
    await fireEvent.click(screen.getByRole("button", { name: "Remove API_KEY" }));

    await waitFor(() => {
      expect(puts).toHaveLength(1);
    });
    expect(puts[0].body).toEqual({ set: {}, remove: ["API_KEY"] });
  });

  it("saves an environment variable, and reports a removal the server never saw", async () => {
    const puts = stubRoutes({
      env: { env: { OLD_ONE: "gone soon", KEPT: "still here" } },
      onEnvPut: () => ({ env: { KEPT: "still here" }, wiring: "committed" }),
    });
    render();

    await waitFor(() => {
      expect((screen.getByLabelText("Name 1") as HTMLInputElement).value).toBe("OLD_ONE");
    });
    // Delete the first row, then save: the removal is worked out on this
    // side, because only this side knows which row a person deleted.
    await fireEvent.click(screen.getByRole("button", { name: "Remove variable 1" }));
    await fireEvent.click(screen.getByRole("button", { name: "Save variables" }));

    await waitFor(() => {
      expect(puts).toHaveLength(1);
    });
    expect(puts[0].body.remove).toContain("OLD_ONE");
  });

  it("says nothing changed when the agent's files already said it", async () => {
    stubRoutes({
      env: { env: { API_BASE: "https://example.test" } },
      onEnvPut: () => ({ env: { API_BASE: "https://example.test" }, wiring: "unchanged" }),
    });
    render();

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Save variables" })).not.toBeNull();
    });
    await fireEvent.click(screen.getByRole("button", { name: "Save variables" }));

    await waitFor(() => {
      expect(screen.getByText("Saved. The agent's files already said this.")).not.toBeNull();
    });
  });

  it("disables every control while the agent is frozen", async () => {
    stubRoutes({
      secrets: {
        keys: ["API_KEY"],
        updated_at: null,
        platform: true,
        secret_name: `${AGENT}-env`,
      },
    });
    renderWithClient(SecretsPanel, { agentName: AGENT, frozen: true });

    await waitFor(() => {
      expect(screen.getByLabelText("Name")).not.toBeNull();
    });
    expect((screen.getByLabelText("Name") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText("Value") as HTMLInputElement).disabled).toBe(true);
    expect(
      (screen.getByRole("button", { name: "Remove API_KEY" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });


  it("asks nothing at all while another tab is the selected one", async () => {
    // Every tabpanel on the agent page stays mounted when another tab is
    // picked. Reading here anyway would ask the secrets platform about an
    // agent nobody was looking at, and leave a row in its audit trail
    // saying somebody did.
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render({ active: false });
    await Promise.resolve();

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("starts reading once the tab becomes the selected one", async () => {
    const puts = stubRoutes({
      secrets: {
        keys: ["API_KEY"],
        updated_at: null,
        platform: true,
        secret_name: `${AGENT}-env`,
      },
    });
    const { rerender, client } = render({ active: false });

    // The harness owns the props, so a rerender goes through it.
    await rerender({
      component: SecretsPanel,
      componentProps: { agentName: AGENT, frozen: false, active: true },
      client,
    });

    await waitFor(() => {
      expect(screen.getByText("API_KEY")).not.toBeNull();
    });
    expect(puts).toHaveLength(0);
  });

  it("refuses a paste that would take the list past its cap, before sending", async () => {
    const puts = stubRoutes({
      secrets: {
        keys: Array.from({ length: 99 }, (_, i) => `K${i}`),
        updated_at: null,
        platform: true,
        secret_name: `${AGENT}-env`,
      },
    });
    render();

    await waitFor(() => {
      expect(screen.getByLabelText("One KEY=value per line")).not.toBeNull();
    });
    await fireEvent.input(screen.getByLabelText("One KEY=value per line"), {
      target: { value: `ONE=${VALUE}\nTWO=${VALUE}\nTHREE=${VALUE}\n` },
    });
    await fireEvent.click(screen.getByRole("button", { name: "Add these" }));

    expect(screen.getByText(/100 is the most a list can hold/)).not.toBeNull();
    expect(puts).toHaveLength(0);
  });

  it("keeps the right row when one in the middle is deleted", async () => {
    // Keyed on a stable id, not the index: keying on the index re-keyed
    // every row after a deletion, so Svelte reused the wrong DOM nodes and
    // an in-progress edit landed on the neighbour.
    stubRoutes({ env: { env: { FIRST: "one", SECOND: "two", THIRD: "three" } } });
    render();

    await waitFor(() => {
      expect((screen.getByLabelText("Name 1") as HTMLInputElement).value).toBe("FIRST");
    });
    await fireEvent.input(screen.getByLabelText("Value 3"), {
      target: { value: "edited in place" },
    });
    await fireEvent.click(screen.getByRole("button", { name: "Remove variable 2" }));

    expect((screen.getByLabelText("Name 1") as HTMLInputElement).value).toBe("FIRST");
    expect((screen.getByLabelText("Name 2") as HTMLInputElement).value).toBe("THIRD");
    expect((screen.getByLabelText("Value 2") as HTMLInputElement).value).toBe(
      "edited in place",
    );
  });

  it("refuses a key the container owns", async () => {
    stubRoutes({});
    render();

    await waitFor(() => {
      expect(screen.getByLabelText("Name")).not.toBeNull();
    });
    await fireEvent.input(screen.getByLabelText("Name"), {
      target: { value: "LD_PRELOAD" },
    });

    expect(screen.getByText(/the container sets this one/)).not.toBeNull();
  });
});

// @vitest-environment jsdom
//
// One table, one Save. What the design asks this panel to prove: it shows
// names and never a value, a new row is a pending change until Save sends
// it, Discard throws the pending changes away, a pasted .env becomes rows
// rather than a write, a bad key is refused before anything is sent, and
// the three sentences a room can owe somebody (no secrets platform, nobody
// to read them as, a removal waiting in a pull request) are said word for
// word.
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SecretsPanel from "./SecretsPanel.svelte";
import { renderWithClient } from "../test-utils/render";

const AGENT = "clio";

// Never a real credential: a placeholder standing in for one, so a test can
// assert it is typed in and never rendered back.
const VALUE = "not-a-real-key-value";

type Routes = {
  secrets?: Record<string, unknown>;
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
        return json({
          keys: [],
          updated_at: null,
          platform: true,
          secret_name: `${AGENT}-env`,
          unreadable: false,
          ...(routes.secrets ?? {}),
        });
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

const save = () => screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
const addSecret = () => screen.getByRole("button", { name: "+ Secret" });
const addVariable = () => screen.getByRole("button", { name: "+ Variable" });

/** Wait for the first read to land, whichever half of the table it fills. */
async function loaded() {
  await waitFor(() => {
    expect(screen.getByRole("button", { name: "+ Variable" })).not.toBeNull();
  });
}

/** Open the paste box and wait for its textarea. */
async function openPaste() {
  await fireEvent.click(screen.getByRole("button", { name: "Paste .env" }));
  return screen.getByLabelText("One KEY=value per line");
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
    // A stored secret has no field at all: there is nothing to edit and
    // nothing to read back.
    for (const field of container.querySelectorAll("input")) {
      expect((field as HTMLInputElement).value).not.toContain(VALUE);
    }
    expect(screen.queryByLabelText("Value 1")).toBeNull();
  });

  it("says so plainly when the room has no secrets platform, and keeps the variables", async () => {
    stubRoutes({
      secrets: { platform: false, secret_name: null },
      env: { env: { API_BASE: "https://example.test" } },
    });
    render();

    await waitFor(() => {
      expect(
        screen.getByText(/This room has no secrets platform configured/),
      ).not.toBeNull();
    });
    // No way to add to a list that cannot be written.
    expect(screen.queryByRole("button", { name: "+ Secret" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Paste .env" })).toBeNull();
    // The other half still works, which is the point of checking rather
    // than hiding the whole panel.
    expect(addVariable()).not.toBeNull();
    expect((screen.getByLabelText("Name 1") as HTMLInputElement).value).toBe("API_BASE");
  });

  it("says so when the room has nobody to read the secrets as", async () => {
    // An empty list and "the room cannot ask" look the same to a person
    // and mean opposite things.
    stubRoutes({ secrets: { unreadable: true } });
    render();

    await waitFor(() => {
      expect(screen.getByText(/nobody to read this agent's secrets as/)).not.toBeNull();
    });
    expect(screen.queryByRole("button", { name: "+ Secret" })).toBeNull();
    // The other half is unaffected.
    expect(addVariable()).not.toBeNull();
  });

  it("says there is nothing here yet when both lists are empty", async () => {
    stubRoutes({});
    render();

    await waitFor(() => {
      expect(screen.getByText("No secrets or variables yet.")).not.toBeNull();
    });
  });

  it("keeps Save out of reach until something is pending", async () => {
    stubRoutes({});
    render();
    await loaded();

    expect(save().disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "Discard" })).toBeNull();

    await fireEvent.click(addVariable());
    await fireEvent.input(screen.getByLabelText("Name 1"), { target: { value: "API_BASE" } });

    expect(save().disabled).toBe(false);
    expect(screen.getByRole("button", { name: "Discard" })).not.toBeNull();
  });

  it("sends a new secret with its value exactly once, and never shows it again", async () => {
    const puts = stubRoutes({
      onSecretsPut: () => ({
        keys: ["API_KEY"],
        platform: true,
        secret_name: `${AGENT}-env`,
        wiring: "committed",
      }),
    });
    const { container } = render();
    await loaded();

    await fireEvent.click(addSecret());
    await fireEvent.input(screen.getByLabelText("Name 1"), { target: { value: "API_KEY" } });
    await fireEvent.input(screen.getByLabelText("Value 1"), { target: { value: VALUE } });
    // Nothing has left the browser yet: a new row is a pending change.
    expect(puts).toHaveLength(0);

    await fireEvent.click(save());

    await waitFor(() => {
      expect(puts).toHaveLength(1);
    });
    expect(puts[0].url).toContain("/secrets");
    expect(puts[0].body).toEqual({ set: { API_KEY: VALUE }, remove: [] });
    await waitFor(() => {
      expect(
        screen.getByText(/The agent picks this up the next time it starts/),
      ).not.toBeNull();
    });
    // The pending row is gone, and with it the only field that ever held
    // the value.
    for (const field of container.querySelectorAll("input")) {
      expect((field as HTMLInputElement).value).not.toContain(VALUE);
    }
    expect(container.textContent).not.toContain(VALUE);
    await waitFor(() => {
      expect(save().disabled).toBe(true);
    });
  });

  it("sends a new variable with the other mutation", async () => {
    const puts = stubRoutes({
      onEnvPut: () => ({ env: { API_BASE: "https://example.test" }, wiring: "committed" }),
    });
    render();
    await loaded();

    await fireEvent.click(addVariable());
    await fireEvent.input(screen.getByLabelText("Name 1"), { target: { value: "API_BASE" } });
    await fireEvent.input(screen.getByLabelText("Value 1"), {
      target: { value: "https://example.test" },
    });
    await fireEvent.click(save());

    await waitFor(() => {
      expect(puts).toHaveLength(1);
    });
    expect(puts[0].url).toContain("/env");
    expect(puts[0].body).toEqual({
      set: { API_BASE: "https://example.test" },
      remove: [],
    });
  });

  it("saves the secrets before the variables when both are pending", async () => {
    const puts = stubRoutes({
      onSecretsPut: () => ({
        keys: ["API_KEY"],
        platform: true,
        secret_name: `${AGENT}-env`,
        wiring: "committed",
      }),
      onEnvPut: () => ({ env: { API_BASE: "https://example.test" }, wiring: "committed" }),
    });
    render();
    await loaded();

    await fireEvent.click(addSecret());
    await fireEvent.input(screen.getByLabelText("Name 1"), { target: { value: "API_KEY" } });
    await fireEvent.input(screen.getByLabelText("Value 1"), { target: { value: VALUE } });
    await fireEvent.click(addVariable());
    await fireEvent.input(screen.getByLabelText("Name 1"), { target: { value: "API_BASE" } });
    await fireEvent.input(screen.getByLabelText("Value 1"), {
      target: { value: "https://example.test" },
    });
    await fireEvent.click(save());

    await waitFor(() => {
      expect(puts).toHaveLength(2);
    });
    expect(puts[0].url).toContain("/secrets");
    expect(puts[1].url).toContain("/env");
  });

  it("marks a removed secret pending, and sends the removal on Save", async () => {
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
    // Pending, not sent: the row is gone from the table and Save is live.
    expect(screen.queryByText("API_KEY")).toBeNull();
    expect(puts).toHaveLength(0);
    expect(save().disabled).toBe(false);

    await fireEvent.click(save());

    await waitFor(() => {
      expect(puts).toHaveLength(1);
    });
    expect(puts[0].body).toEqual({ set: {}, remove: ["API_KEY"] });
  });

  it("works out a removed variable on this side", async () => {
    const puts = stubRoutes({
      env: { env: { OLD_ONE: "gone soon", KEPT: "still here" } },
      onEnvPut: () => ({ env: { KEPT: "still here" }, wiring: "committed" }),
    });
    render();

    await waitFor(() => {
      expect((screen.getByLabelText("Name 1") as HTMLInputElement).value).toBe("OLD_ONE");
    });
    await fireEvent.click(screen.getByRole("button", { name: "Remove OLD_ONE" }));
    await fireEvent.click(save());

    await waitFor(() => {
      expect(puts).toHaveLength(1);
    });
    expect(puts[0].body.remove).toContain("OLD_ONE");
  });

  it("throws the pending changes away on Discard", async () => {
    const puts = stubRoutes({
      secrets: {
        keys: ["API_KEY"],
        updated_at: null,
        platform: true,
        secret_name: `${AGENT}-env`,
      },
      env: { env: { API_BASE: "https://example.test" } },
    });
    render();

    await waitFor(() => {
      expect(screen.getByText("API_KEY")).not.toBeNull();
    });
    await fireEvent.click(screen.getByRole("button", { name: "Remove API_KEY" }));
    await fireEvent.click(addVariable());
    await fireEvent.input(screen.getByLabelText("Name 1"), { target: { value: "NEW_ONE" } });

    await fireEvent.click(screen.getByRole("button", { name: "Discard" }));

    expect(screen.getByText("API_KEY")).not.toBeNull();
    expect(screen.queryByDisplayValue("NEW_ONE")).toBeNull();
    expect(save().disabled).toBe(true);
    expect(puts).toHaveLength(0);
  });

  it("refuses a key the room sets itself, before anything is sent", async () => {
    const puts = stubRoutes({});
    render();
    await loaded();

    await fireEvent.click(addSecret());
    await fireEvent.input(screen.getByLabelText("Name 1"), {
      target: { value: "SLACK_BOT_TOKEN" },
    });
    await fireEvent.input(screen.getByLabelText("Value 1"), { target: { value: VALUE } });

    expect(screen.getByText(/the room sets this one/)).not.toBeNull();
    expect(save().disabled).toBe(true);
    expect(puts).toHaveLength(0);
  });

  it("refuses a key that is not a name at all", async () => {
    stubRoutes({});
    render();
    await loaded();

    await fireEvent.click(addSecret());
    await fireEvent.input(screen.getByLabelText("Name 1"), { target: { value: "api-key" } });

    expect(screen.getByText(/use capital letters, digits and underscores/)).not.toBeNull();
  });

  it("refuses a key the container owns", async () => {
    stubRoutes({});
    render();
    await loaded();

    await fireEvent.click(addSecret());
    await fireEvent.input(screen.getByLabelText("Name 1"), { target: { value: "LD_PRELOAD" } });

    expect(screen.getByText(/the container sets this one/)).not.toBeNull();
  });

  it("parses a pasted .env into rows, and sends them in one save", async () => {
    const puts = stubRoutes({
      onSecretsPut: () => ({
        keys: ["API_KEY", "DB_URL"],
        platform: true,
        secret_name: `${AGENT}-env`,
        wiring: "committed",
      }),
    });
    render();
    await loaded();

    const box = await openPaste();
    await fireEvent.input(box, {
      target: {
        value: `# a comment\nAPI_KEY=${VALUE}\n\nexport DB_URL="postgres://example.test/db"\n`,
      },
    });
    await fireEvent.click(screen.getByRole("button", { name: "Add" }));

    // Rows, not a write: the paste lands in the table like anything else.
    expect(puts).toHaveLength(0);
    expect(screen.getByDisplayValue("API_KEY")).not.toBeNull();
    expect(screen.getByDisplayValue("DB_URL")).not.toBeNull();

    await fireEvent.click(save());

    await waitFor(() => {
      expect(puts).toHaveLength(1);
    });
    expect(puts[0].body.set).toEqual({
      API_KEY: VALUE,
      DB_URL: "postgres://example.test/db",
    });
  });

  it("shows a bad .env line back and adds nothing until it is fixed", async () => {
    const puts = stubRoutes({});
    render();
    await loaded();

    const box = await openPaste();
    await fireEvent.input(box, {
      target: { value: `API_KEY=${VALUE}\njust some words\nlower=1\n` },
    });
    await fireEvent.click(screen.getByRole("button", { name: "Add" }));

    expect(screen.getByText("Line 2: write this as KEY=value.")).not.toBeNull();
    expect(
      screen.getByText(/Line 3: lower: use capital letters, digits and underscores/),
    ).not.toBeNull();
    expect(screen.queryByDisplayValue("API_KEY")).toBeNull();
    expect(save().disabled).toBe(true);
    expect(puts).toHaveLength(0);
  });

  it("refuses a paste that would take the list past its cap, before adding it", async () => {
    stubRoutes({
      secrets: {
        keys: Array.from({ length: 99 }, (_, i) => `K${i}`),
        updated_at: null,
        platform: true,
        secret_name: `${AGENT}-env`,
      },
    });
    render();
    await loaded();

    const box = await openPaste();
    await fireEvent.input(box, {
      target: { value: `ONE=${VALUE}\nTWO=${VALUE}\nTHREE=${VALUE}\n` },
    });
    await fireEvent.click(screen.getByRole("button", { name: "Add" }));

    expect(screen.getByText(/100 is the most a list can hold/)).not.toBeNull();
    expect(screen.queryByDisplayValue("ONE")).toBeNull();
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
    await loaded();

    await fireEvent.click(addSecret());
    await fireEvent.input(screen.getByLabelText("Name 1"), { target: { value: "API_KEY" } });
    await fireEvent.input(screen.getByLabelText("Value 1"), { target: { value: VALUE } });
    await fireEvent.click(save());

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

  it("says nothing changed when the agent's files already said it", async () => {
    stubRoutes({
      env: { env: { API_BASE: "https://example.test" } },
      onEnvPut: () => ({ env: { API_BASE: "https://example.test" }, wiring: "unchanged" }),
    });
    render();

    await waitFor(() => {
      expect((screen.getByLabelText("Value 1") as HTMLInputElement).value).toBe(
        "https://example.test",
      );
    });
    await fireEvent.input(screen.getByLabelText("Value 1"), {
      target: { value: "https://example.test/v2" },
    });
    await fireEvent.click(save());

    await waitFor(() => {
      expect(screen.getByText("Saved. The agent's files already said this.")).not.toBeNull();
    });
  });

  it("says the secret stays until the pull request merges", async () => {
    // The one outcome where the save is not the whole story: the agent's
    // files still name the secret, so it has to stay until somebody
    // merges. "Removed" here would be a lie.
    stubRoutes({
      secrets: { keys: ["API_KEY"] },
      onSecretsPut: () => ({
        keys: [],
        platform: true,
        secret_name: `${AGENT}-env`,
        wiring: "pull_request",
        pull_request: "https://example.test/pull/9",
        emptied: false,
        pending_unwire: true,
      }),
    });
    render();

    await waitFor(() => {
      expect(screen.getByText("API_KEY")).not.toBeNull();
    });
    await fireEvent.click(screen.getByRole("button", { name: "Remove API_KEY" }));
    await fireEvent.click(save());

    await waitFor(() => {
      expect(
        screen.getByText(
          "The secret stays until that pull request merges. Save again after it merges to finish removing it.",
        ),
      ).not.toBeNull();
    });
    expect(
      screen.getByRole("link", { name: "Open the pull request" }).getAttribute("href"),
    ).toBe("https://example.test/pull/9");
  });

  it("says the last secret is gone when it really is", async () => {
    stubRoutes({
      secrets: { keys: ["API_KEY"] },
      onSecretsPut: () => ({
        keys: [],
        platform: true,
        secret_name: null,
        wiring: "committed",
        emptied: true,
        pending_unwire: false,
      }),
    });
    render();

    await waitFor(() => {
      expect(screen.getByText("API_KEY")).not.toBeNull();
    });
    await fireEvent.click(screen.getByRole("button", { name: "Remove API_KEY" }));
    await fireEvent.click(save());

    await waitFor(() => {
      expect(screen.getByText(/That was this agent's last secret/)).not.toBeNull();
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
      env: { env: { API_BASE: "https://example.test" } },
    });
    renderWithClient(SecretsPanel, { agentName: AGENT, frozen: true });

    await waitFor(() => {
      expect(screen.getByText("API_KEY")).not.toBeNull();
    });
    expect((addSecret() as HTMLButtonElement).disabled).toBe(true);
    expect((addVariable() as HTMLButtonElement).disabled).toBe(true);
    expect(
      (screen.getByRole("button", { name: "Remove API_KEY" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect((screen.getByLabelText("Name 2") as HTMLInputElement).disabled).toBe(true);
    expect(save().disabled).toBe(true);
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
    await fireEvent.click(screen.getByRole("button", { name: "Remove SECOND" }));

    expect((screen.getByLabelText("Name 1") as HTMLInputElement).value).toBe("FIRST");
    expect((screen.getByLabelText("Name 2") as HTMLInputElement).value).toBe("THIRD");
    expect((screen.getByLabelText("Value 2") as HTMLInputElement).value).toBe(
      "edited in place",
    );
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
});

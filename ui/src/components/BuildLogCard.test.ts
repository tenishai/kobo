// @vitest-environment jsdom
// The build log sheet's one refusal for a connected agent. The room keeps a
// connected repository's build log to operators (issue #200: it cannot check
// who may read that repository), which left the agent's own owner with a
// sentence telling them only an owner can read it. The sheet now sends them
// to the repository's own builds page, where GitHub checks who may read it.
import { screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";

import BuildLogCard from "./BuildLogCard.svelte";
import { renderWithClient } from "../test-utils/render";

function refuse(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify({ error: "operator-only on this room" }), {
          status: 403,
          headers: { "Content-Type": "application/json" },
        }),
    ),
  );
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

describe("BuildLogCard", () => {
  it("sends a connected agent's owner to the repository's own builds when the room refuses the log", async () => {
    refuse();
    renderWithClient(BuildLogCard, { agentName: "clio", open: true, onclose: () => {}, connectedRepo: "acme/clio" });

    const link = (await screen.findByRole("link", { name: "builds of acme/clio" })) as HTMLAnchorElement;
    expect(link.href).toBe("https://github.com/acme/clio/actions");
    expect(screen.getByText(/the room shows its build log to room operators only/)).toBeTruthy();
    expect(screen.queryByText(/Only this agent's owner/)).toBeNull();
  });

  it("keeps the owner-only sentence for an agent the room made", async () => {
    refuse();
    renderWithClient(BuildLogCard, { agentName: "clio", open: true, onclose: () => {} });

    expect(await screen.findByText(/Only this agent's owner \(or a room operator\) can read its build log/)).toBeTruthy();
    expect(screen.queryByRole("link")).toBeNull();
  });
});

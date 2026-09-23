// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RepoAccessCard from "./RepoAccessCard.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const ROWS = [
  {
    id: 1,
    agent: "warden",
    repository: "your-org/example-repo",
    status: "requested",
    requested_by: "owner@example.com",
    decided_by: null,
  },
  {
    id: 2,
    agent: "warden",
    repository: "your-org/live-repo",
    status: "approved",
    requested_by: "owner@example.com",
    decided_by: "operator@example.com",
  },
  {
    id: 3,
    agent: "other-agent",
    repository: "your-org/not-ours",
    status: "approved",
    requested_by: "someone@example.com",
    decided_by: "operator@example.com",
  },
];

/** Decide is a sub-path of the listing route, so it has to match first. */
function stubRepoAccess(results: unknown[]): void {
  stubFetch({
    "/api/repo-access/decide": { decided: [] },
    "/api/agents/warden/repo-access": { requested: [] },
    "/api/repo-access": { results },
  });
}

async function openRequest(): Promise<void> {
  await fireEvent.click(
    await screen.findByRole("button", { name: "Request repositories" }),
  );
}

describe("RepoAccessCard", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("keeps the request form shut until it is asked for, and opens it on click", async () => {
    stubRepoAccess([]);
    renderWithClient(RepoAccessCard, { agentName: "warden" });

    const toggle = await screen.findByRole("button", { name: "Request repositories" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByLabelText("One owner/name per line")).toBeNull();

    await fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByLabelText("One owner/name per line")).not.toBeNull();
  });

  it("says nothing is there in one table row", async () => {
    stubRepoAccess([]);
    const { container } = renderWithClient(RepoAccessCard, { agentName: "warden" });

    await screen.findByText("No repositories requested yet.");
    const empties = container.querySelectorAll("td.panel__empty");
    expect(empties).toHaveLength(1);
    expect(empties[0].parentElement?.children).toHaveLength(1);
    expect(empties[0].getAttribute("colspan")).toBe("3");
  });

  it("offers at most one filled button with the form open", async () => {
    stubRepoAccess(ROWS);
    const { container } = renderWithClient(RepoAccessCard, { agentName: "warden" });

    await openRequest();
    await screen.findByText("your-org/example-repo");

    expect(container.querySelectorAll(".btn--primary").length).toBeLessThanOrEqual(1);
  });

  it("lists only this agent's repositories with their state", async () => {
    stubRepoAccess(ROWS);
    renderWithClient(RepoAccessCard, { agentName: "warden" });

    expect(await screen.findByText("your-org/example-repo")).not.toBeNull();
    expect(screen.getByText("your-org/live-repo")).not.toBeNull();
    expect(screen.queryByText("your-org/not-ours")).toBeNull();
    expect(screen.getByText("requested")).not.toBeNull();
    expect(screen.getByText("approved")).not.toBeNull();
  });

  it("offers Approve only on a requested row, and Revoke on anything not revoked", async () => {
    stubRepoAccess(ROWS);
    renderWithClient(RepoAccessCard, { agentName: "warden" });

    await screen.findByText("your-org/example-repo");
    expect(
      screen.getByRole("button", { name: "Approve your-org/example-repo" }),
    ).not.toBeNull();
    expect(
      screen.queryByRole("button", { name: "Approve your-org/live-repo" }),
    ).toBeNull();
    expect(screen.getAllByRole("button", { name: /^Revoke / })).toHaveLength(2);
  });

  it("approving posts that one id and the decision", async () => {
    stubRepoAccess(ROWS);
    renderWithClient(RepoAccessCard, { agentName: "warden" });

    await screen.findByText("your-org/example-repo");
    await fireEvent.click(
      screen.getByRole("button", { name: "Approve your-org/example-repo" }),
    );

    const call = vi
      .mocked(fetch)
      .mock.calls.find(
        ([url, init]) =>
          String(url) === "/api/repo-access/decide" &&
          (init as RequestInit | undefined)?.method === "POST",
      );
    expect(call).toBeDefined();
    const body = JSON.parse((call as [RequestInfo | URL, RequestInit])[1].body as string);
    expect(body).toEqual({ ids: [1], decision: "approved" });
  });

  it("revoking posts that one id and the decision", async () => {
    stubRepoAccess(ROWS);
    renderWithClient(RepoAccessCard, { agentName: "warden" });

    await screen.findByText("your-org/live-repo");
    await fireEvent.click(
      screen.getByRole("button", { name: "Revoke your-org/live-repo" }),
    );

    const call = vi
      .mocked(fetch)
      .mock.calls.find(
        ([url, init]) =>
          String(url) === "/api/repo-access/decide" &&
          (init as RequestInit | undefined)?.method === "POST",
      );
    expect(call).toBeDefined();
    const body = JSON.parse((call as [RequestInfo | URL, RequestInit])[1].body as string);
    expect(body).toEqual({ ids: [2], decision: "revoked" });
  });

  it("requests the typed repositories one per line", async () => {
    stubRepoAccess([]);
    renderWithClient(RepoAccessCard, { agentName: "warden" });

    await openRequest();
    await fireEvent.input(screen.getByLabelText("One owner/name per line"), {
      target: { value: "your-org/one\n your-org/two \n\n" },
    });
    await fireEvent.click(screen.getByRole("button", { name: "Request access" }));

    const call = vi
      .mocked(fetch)
      .mock.calls.find(
        ([url, init]) =>
          String(url) === "/api/agents/warden/repo-access" &&
          (init as RequestInit | undefined)?.method === "POST",
      );
    expect(call).toBeDefined();
    const body = JSON.parse((call as [RequestInfo | URL, RequestInit])[1].body as string);
    expect(body).toEqual({ repositories: ["your-org/one", "your-org/two"] });
  });

  it("refuses a line that is not owner/name shaped and says which", async () => {
    stubRepoAccess([]);
    renderWithClient(RepoAccessCard, { agentName: "warden" });

    await openRequest();
    await fireEvent.input(screen.getByLabelText("One owner/name per line"), {
      target: { value: "your-org/ok\nnot-a-repo" },
    });

    expect(screen.getByText("Not owner/name shaped: not-a-repo")).not.toBeNull();
    expect(
      (screen.getByRole("button", { name: "Request access" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("disables what is inside the form on a frozen agent, but still opens it", async () => {
    stubRepoAccess(ROWS);
    renderWithClient(RepoAccessCard, { agentName: "warden", frozen: true });

    await openRequest();
    await screen.findByText("your-org/example-repo");
    expect(
      (screen.getByLabelText("One owner/name per line") as HTMLTextAreaElement).disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: "Request access" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(
      (
        screen.getByRole("button", {
          name: "Approve your-org/example-repo",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
  });
});

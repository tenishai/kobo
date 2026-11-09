// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";
import GcpProjectPicker from "./GcpProjectPicker.svelte";
import { renderWithClient, stubFetch } from "../../test-utils/render";

// The picker's contract (2026-11-09): the whole tree is listed with where each project
// sits, only a readable unconnected project can be ticked, and Connect sends exactly the
// ticked ids to the parent's connect route.
describe("GcpProjectPicker", () => {
  const CHECK = {
    organization: "example-platform",
    parent: "folders/123",
    connected: [],
    already: [],
    without_role: [],
    accounts: [
      { id: "example-prod-1234", name: "Example prod", integration: "example-prod-1234", reason: null, connected: true, reachable: true, project_number: "1", folder_path: [] },
      { id: "example-data-5678", name: "Example data", integration: null, reason: null, connected: false, reachable: true, project_number: "2", folder_path: ["Dev"] },
      { id: "example-lab-9012", name: "Example lab", integration: null, reason: "the identity lacks access on example-lab-9012", connected: false, reachable: false, project_number: "3", folder_path: ["Dev", "Labs"] },
    ],
  };
  const CONNECT = { organization: "example-platform", connected: [{ id: "example-data-5678", name: "Example data", integration: "example-data-5678", reason: null }], already: [], refused: [] };

  it("lists the tree, ticks only what can be picked, and connects the ticked ids", async () => {
    stubFetch({ "/accounts/check": CHECK, "/accounts/connect": CONNECT });
    const onclose = vi.fn();
    renderWithClient(GcpProjectPicker, { integrationId: "example-platform", name: "example-platform", onclose });

    await screen.findByText("Example data");
    expect(screen.getByText(/example-data-5678 · Dev/)).toBeTruthy();
    expect(screen.getByText(/example-lab-9012 · Dev \/ Labs/)).toBeTruthy();
    expect(screen.getByText("1 connected · 1 can be picked · 1 not granted yet")).toBeTruthy();

    const prod = screen.getByLabelText("Connect Example prod") as HTMLInputElement;
    const data = screen.getByLabelText("Connect Example data") as HTMLInputElement;
    const lab = screen.getByLabelText("Connect Example lab") as HTMLInputElement;
    expect(prod.disabled && prod.checked).toBe(true);
    expect(lab.disabled).toBe(true);
    expect(data.disabled).toBe(false);

    const connect = screen.getByRole("button", { name: /Connect .*selected/ }) as HTMLButtonElement;
    expect(connect.disabled).toBe(true);
    await fireEvent.click(data);
    expect(screen.getByRole("button", { name: "Connect 1 selected" })).toBeTruthy();

    // Search narrows by id, name or folder.
    await fireEvent.input(screen.getByLabelText("Search projects"), { target: { value: "labs" } });
    expect(screen.queryByText("Example data")).toBeNull();
    expect(screen.getByText("Example lab")).toBeTruthy();
    await fireEvent.input(screen.getByLabelText("Search projects"), { target: { value: "" } });

    await fireEvent.click(screen.getByRole("button", { name: "Connect 1 selected" }));
    await waitFor(() => expect(onclose).toHaveBeenCalled());
    const calls = (fetch as unknown as { mock: { calls: [RequestInfo | URL, RequestInit?][] } }).mock.calls;
    const post = calls.find(([url]) => String(url).includes("/accounts/connect"));
    expect(post).toBeTruthy();
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ projects: ["example-data-5678"] });
  });
});

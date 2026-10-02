// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import GroupRequestsCard from "./GroupRequestsCard.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const REQUEST = {
  id: 1,
  group_name: "design-team",
  requested_by: "asha@example.com",
  note: "for the new onboarding agent",
  status: "pending",
  decided_by: null,
  created_at: "2026-08-21T12:00:00Z",
  decided_at: null,
};

describe("GroupRequestsCard — admin badge is additive display, driven by pending_group_requests (shared-group + group-requests wave)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders nothing when whoami carries no pending_group_requests key at all (non-admin)", async () => {
    stubFetch({
      "/api/whoami": { email: "a@example.com", operator: false, groups: [], groups_unavailable: false },
    });
    renderWithClient(GroupRequestsCard);

    await waitFor(() => {
      expect(screen.queryByText(/Group requests:/)).toBeNull();
    });
  });

  it("renders even when whoami.operator is false — this badge never gates on operator, only on the count", async () => {
    // The watch item this pins: `operator` reflects the plain admin-groups
    // check, but a caller could in principle read `pending_group_requests`
    // without `operator` also being true depending on how the server
    // derives each — the card must key ONLY off the count, never re-check
    // `operator` itself.
    stubFetch({
      "/api/whoami": {
        email: "ops@example.com",
        operator: false,
        groups: [],
        groups_unavailable: false,
        pending_group_requests: 2,
      },
    });
    renderWithClient(GroupRequestsCard);

    await screen.findByText("Group requests: 2 pending");
  });

  it("shows the pending count collapsed, then expands to the list with Done/Decline per row", async () => {
    stubFetch({
      "/api/whoami": {
        email: "ops@example.com",
        operator: true,
        groups: [],
        groups_unavailable: false,
        pending_group_requests: 1,
      },
      "/api/group-requests": { requests: [REQUEST] },
    });
    renderWithClient(GroupRequestsCard);

    const toggle = await screen.findByText("Group requests: 1 pending");
    expect(screen.queryByText("design-team")).toBeNull();

    await fireEvent.click(toggle);

    await screen.findByText("design-team");
    expect(screen.getByText("for the new onboarding agent")).not.toBeNull();
    expect(screen.getByText(/asha@example\.com/)).not.toBeNull();
    expect(screen.getByRole("button", { name: "Mark design-team done" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "Decline design-team" })).not.toBeNull();
  });

});

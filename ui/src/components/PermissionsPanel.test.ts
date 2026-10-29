// @vitest-environment jsdom
// The Permissions picker walks vendor, then account, then services; ticking a
// service must not move it (the first version's accordion closed on every tick).
import { fireEvent, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import PermissionsPanel from "./PermissionsPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const ACCESS = {
  agent: "ledger",
  team: "shared",
  grants: [],
  pending: [],
  can_request: true,
  can_decide: true,
  vendors: [
    {
      id: "aws",
      kind: "cloud",
      label: "AWS",
      account_noun: "account",
      accounts: [
        { id: "acme-audit", name: "acme-audit", connected: true, ceiling: ["aws_cloudtrail_read", "aws_iam_read"] },
        { id: "acme-data", name: "acme-data", connected: false, ceiling: [] },
      ],
      groups: [
        { name: "Security", permissions: [
          { name: "aws_cloudtrail_read", tier: "read", description: "CloudTrail: trails, event history and Lake queries" },
          { name: "aws_iam_read", tier: "read", description: "IAM users, roles, policies" },
          { name: "aws_kms_read", tier: "read", description: "KMS keys (never decrypt)" },
        ] },
        { name: "Compute", permissions: [
          { name: "aws_ec2_read", tier: "read", description: "EC2 instances" },
          { name: "aws_ecs_update_service", tier: "mutate", description: "Change an ECS service" },
        ] },
      ],
      tiers: [],
    },
    { id: "datadog", kind: "service", label: "Datadog", account_noun: null,
      accounts: [{ id: "datadog", name: "datadog", connected: true, ceiling: ["datadog_monitors_read"] }],
      groups: [{ name: "Monitors", permissions: [{ name: "datadog_monitors_read", tier: "read", description: "Monitors" }] }], tiers: [] },
  ],
};

afterEach(() => vi.unstubAllGlobals());

describe("PermissionsPanel", () => {
  it("walks vendor, account, services and keeps the view while ticking", async () => {
    stubFetch({ "/api/agents/ledger/access": ACCESS });
    renderWithClient(PermissionsPanel, { agentName: "ledger" });

    const aws = await screen.findByRole("button", { name: /AWS/ });
    expect(aws.textContent).toContain("1 of 2 accounts connected");
    await fireEvent.click(aws);

    // The unconnected account is shown but cannot be opened.
    const rds = screen.getByRole("button", { name: /acme-data/ });
    expect((rds as HTMLButtonElement).disabled).toBe(true);
    await fireEvent.click(screen.getByRole("button", { name: /acme-audit/ }));

    // Read permissions only; the write tier is not offered here.
    expect(screen.queryByText("Change an ECS service")).toBeNull();
    const cloudtrail = screen.getByLabelText(/cloudtrail/) as HTMLInputElement;
    await fireEvent.click(cloudtrail);
    expect(cloudtrail.checked).toBe(true);
    // Still on the same account: the search box and the confirm bar are here, with the count.
    expect(screen.getByLabelText("Find a service")).not.toBeNull();
    expect(screen.getByText("1 selected")).not.toBeNull();
    // Above the ceiling is greyed with the reason, not hidden.
    expect(screen.getAllByText("the connection does not allow this").length).toBe(2); // kms, ec2
    expect((screen.getByLabelText(/kms/) as HTMLInputElement).disabled).toBe(true);

    // Search narrows the list.
    await fireEvent.input(screen.getByLabelText("Find a service"), { target: { value: "iam" } });
    expect(screen.queryByLabelText(/cloudtrail/)).toBeNull();
    expect(screen.getByLabelText(/iam/)).not.toBeNull();
  });

  it("opens a service vendor straight on its permissions", async () => {
    stubFetch({ "/api/agents/ledger/access": ACCESS });
    renderWithClient(PermissionsPanel, { agentName: "ledger" });
    await fireEvent.click(await screen.findByRole("button", { name: /Datadog/ }));
    expect(screen.getByLabelText(/monitors/)).not.toBeNull();
  });
});

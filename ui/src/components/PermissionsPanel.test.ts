// @vitest-environment jsdom
// What this agent can reach (issue #365, opened on the vendors by #368,
// compacted by #382): one closed row per vendor with its logo and one line
// on what the agent holds there, or its last failed attempt; a row opens on
// click, one at a time, from a link's `?vendor=` or from the session's
// memory; the picker inside walks account, then permissions; ticking a row
// must not move it (the first version's accordion closed on every tick).
// GitHub stops at the agent's repository set first, saves it, and only then
// offers the level; the request targets the set (the child integration),
// never the connection. The repository list draws the picked ones, then the
// best matches for the typed query, never every repository the room sees.
import { fireEvent, screen, within } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import PermissionsPanel from "./PermissionsPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const GITHUB_ROWS = [
  { name: "github_repos_read", tier: "read", description: "Read code, issues, pull requests and workflow runs", label: "Read" },
  { name: "github_pulls_review", tier: "mutate", description: "Read, plus comment on and review pull requests", label: "Review pull requests" },
  { name: "github_pulls_write", tier: "mutate", description: "Review, plus push branches and open pull requests", label: "Open pull requests" },
];

/** GitHub as the access view reports it before a set is saved: one
 * placeholder account carrying the connection's id. */
const GITHUB_PLACEHOLDER = {
  id: "github",
  kind: "service",
  label: "GitHub",
  connected: true,
  account_noun: null,
  organizations: [],
  accounts: [
    { id: "github", name: "GitHub", connected: true, ceiling: ["github_repos_read", "github_pulls_review", "github_pulls_write"], organization: null, account_id: null, repositories: [], has_repo_set: false },
  ],
  groups: [{ name: "Repositories", permissions: GITHUB_ROWS }],
  tiers: [],
};

/** The same vendor once the agent's set is written: the one account is
 * the child, with the repositories on it. */
const GITHUB_SET = {
  ...GITHUB_PLACEHOLDER,
  accounts: [
    { id: "github-ledger", name: "ledger's repositories", connected: true, ceiling: ["github_repos_read", "github_pulls_review"], organization: null, account_id: null, repositories: ["acme-eng/ledger", "acme-eng/billing"], has_repo_set: true },
  ],
};

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
      connected: true,
      account_noun: "account",
      organizations: [{ id: "acme-aws", name: "acme-aws" }],
      accounts: [
        { id: "acme-audit", name: "acme-audit", connected: true, ceiling: ["aws_cloudtrail_read", "aws_iam_read", "aws_ecs_update_service", "aws_macie_read"], organization: "acme-aws", account_id: "111111111111", repositories: [], has_repo_set: false },
        { id: "acme-data", name: "acme-data", connected: false, ceiling: [], organization: "acme-aws", account_id: "222222222222", repositories: [], has_repo_set: false },
      ],
      groups: [
        { name: "Security", permissions: [
          { name: "aws_cloudtrail_read", tier: "read", description: "CloudTrail: trails, event history and Lake queries", label: null },
          { name: "aws_iam_read", tier: "read", description: "IAM users, roles, policies", label: null },
          { name: "aws_kms_read", tier: "read", description: "KMS keys (never decrypt)", label: null },
          { name: "aws_macie_read", tier: "read", description: "Macie findings", label: null, tool: false },
        ] },
        { name: "Compute", permissions: [
          { name: "aws_ec2_read", tier: "read", description: "EC2 instances", label: null },
          { name: "aws_ecs_update_service", tier: "mutate", description: "Change an ECS service", label: null },
          { name: "aws_ecs_delete_service", tier: "destroy", description: "Remove an ECS service", label: null },
        ] },
      ],
      tiers: [],
    },
    { id: "datadog", kind: "service", label: "Datadog", connected: true, account_noun: null, organizations: [],
      accounts: [{ id: "datadog", name: "datadog", connected: true, ceiling: ["datadog_monitors_read"], organization: null, account_id: null, repositories: [], has_repo_set: false }],
      groups: [{ name: "Monitors", permissions: [{ name: "datadog_monitors_read", tier: "read", description: "Monitors", label: null }] }], tiers: [] },
    GITHUB_PLACEHOLDER,
  ],
};

const REPOSITORIES = {
  available: true,
  repositories: [
    { full_name: "acme-eng/ledger", private: true, default_branch: "main" },
    { full_name: "acme-eng/billing", private: true, default_branch: "main" },
    { full_name: "acme-eng/website", private: false, default_branch: "main" },
  ],
  selected: [],
  note: null,
  total: 3,
};

/** A room with far more repositories than the picker should ever draw. */
const MANY_REPOSITORIES = {
  ...REPOSITORIES,
  repositories: [
    ...REPOSITORIES.repositories,
    ...Array.from({ length: 120 }, (_, i) => ({ full_name: `acme-eng/svc-${String(i).padStart(3, "0")}`, private: true, default_branch: "main" })),
  ],
  total: 2279,
};

type Route = { status?: number; body: unknown } | ((init: RequestInit | undefined) => { status?: number; body: unknown });

/** A fetch stub that can answer by method and status, which the shared one
 * cannot: the repository step saves with a PUT and shows a 422's sentence. */
function stubRoutes(routes: Record<string, Route>): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      for (const [key, route] of Object.entries(routes)) {
        const [m, path] = key.includes(" ") ? key.split(" ") : ["GET", key];
        if (m === method && url.includes(path)) {
          const r = typeof route === "function" ? route(init) : route;
          return new Response(JSON.stringify(r.body), { status: r.status ?? 200, headers: { "Content-Type": "application/json" } });
        }
      }
      throw new Error(`unstubbed fetch: ${method} ${url}`);
    }),
  );
}

function putCalls(): RequestInit[] {
  return vi
    .mocked(fetch)
    .mock.calls.filter(([url, init]) => String(url).endsWith("/github/repositories") && (init as RequestInit | undefined)?.method === "PUT")
    .map(([, init]) => init as RequestInit);
}

/** The rows in the order drawn, by their labels. */
function rowLabels(): string[] {
  return Array.from(document.querySelectorAll("section[aria-label]")).map((s) => s.getAttribute("aria-label") ?? "");
}
function expanded(name: RegExp): string | null {
  return screen.getByRole("button", { name }).getAttribute("aria-expanded");
}

afterEach(() => {
  vi.unstubAllGlobals();
  // The row last opened is remembered for the session; no test inherits another's.
  sessionStorage.clear();
  window.location.hash = "";
});

describe("PermissionsPanel", () => {
  it("walks vendor, account, permissions and keeps the view while ticking", async () => {
    stubFetch({ "/api/agents/ledger/access": ACCESS });
    renderWithClient(PermissionsPanel, { agentName: "ledger" });

    const aws = await screen.findByRole("button", { name: /AWS/ });
    // Every vendor block carries its mark.
    expect(aws.querySelector(".vendor-mark")).not.toBeNull();
    await fireEvent.click(aws);

    // The unconnected account is shown but cannot be opened.
    const rds = screen.getByRole("button", { name: /acme-data/ });
    expect((rds as HTMLButtonElement).disabled).toBe(true);
    await fireEvent.click(screen.getByRole("button", { name: /acme-audit/ }));

    const cloudtrail = screen.getByLabelText(/cloudtrail/) as HTMLInputElement;
    await fireEvent.click(cloudtrail);
    expect(cloudtrail.checked).toBe(true);
    // Still on the same account: the search box and the confirm bar are here, with the count.
    expect(screen.getByLabelText("Find a permission")).not.toBeNull();
    expect(screen.getByText("1 selected")).not.toBeNull();
    // Above the ceiling is greyed with the reason, not hidden: the read rows
    // kms and ec2, and the destroy row the connection does not carry.
    expect(screen.getAllByText("the connection does not allow this").length).toBe(3);
    expect((screen.getByLabelText(/kms/) as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText(/Remove an ECS service/) as HTMLInputElement).disabled).toBe(true);
    // Issue #474: a row the connection allows but no tool uses yet is greyed
    // too, with its own reason, and is not counted as something to ask for.
    expect((screen.getByLabelText(/Macie/) as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText("no tool uses this yet")).not.toBeNull();
    expect(screen.getByPlaceholderText("Find a permission (3 this connection allows)")).not.toBeNull();

    // Search narrows the list.
    await fireEvent.input(screen.getByLabelText("Find a permission"), { target: { value: "iam" } });
    expect(screen.queryByLabelText(/cloudtrail/)).toBeNull();
    expect(screen.getByLabelText(/iam/)).not.toBeNull();
  });

  it("offers a write-tier row the connection's ceiling carries", async () => {
    // Issue #365: the picker is no longer read-only. A mutate row inside the
    // ceiling is a live checkbox; the request carries it like any other.
    stubFetch({ "/api/agents/ledger/access": ACCESS, "/api/agents/ledger/access/requests": { id: 9 } });
    renderWithClient(PermissionsPanel, { agentName: "ledger" });

    await fireEvent.click(await screen.findByRole("button", { name: /AWS/ }));
    await fireEvent.click(screen.getByRole("button", { name: /acme-audit/ }));

    const ecs = screen.getByLabelText(/Change an ECS service/) as HTMLInputElement;
    expect(ecs.disabled).toBe(false);
    await fireEvent.click(ecs);
    await fireEvent.click(screen.getByRole("button", { name: "Confirm request" }));

    await vi.waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(([url]) => String(url).endsWith("/access/requests"));
      expect(call).toBeDefined();
      const body = JSON.parse(String((call as [RequestInfo | URL, RequestInit])[1].body));
      expect(body).toEqual({ integration: "acme-audit", permissions: ["aws_ecs_update_service"], note: null });
    });
  });

  it("opens a service vendor straight on its permissions", async () => {
    stubFetch({ "/api/agents/ledger/access": ACCESS });
    renderWithClient(PermissionsPanel, { agentName: "ledger" });
    await fireEvent.click(await screen.findByRole("button", { name: /Datadog/ }));
    expect(screen.getByLabelText(/monitors/)).not.toBeNull();
  });

  it("lists held and waiting rows under their vendor, by label, with a GitHub set's repositories", async () => {
    stubFetch({
      "/api/agents/ledger/access": {
        ...ACCESS,
        vendors: [...ACCESS.vendors.slice(0, 2), GITHUB_SET],
        grants: [
          { id: 1, agent: "ledger", integration: "github-ledger", permission: "github_repos_read", tier: "read", gated: false, expires_at: null, granted_by: "admin@example.com", created_at: "2026-10-30T10:00:00Z", request_id: 3, state: "active" },
          // An ask grant is a pair under Other agents, not a row here.
          { id: 2, agent: "ledger", integration: "auditor", permission: "a2a_ask", tier: "read", gated: false, expires_at: null, granted_by: "admin@example.com", created_at: "2026-10-30T10:00:00Z", request_id: 4, state: "active" },
        ],
        pending: [
          { id: 7, agent: "ledger", integration: "github-ledger", permissions: ["github_pulls_review"], tiers: { mutate: 1 }, kind: "lease", status: "pending", requested_by: "owner@example.com", note: null, created_at: "2026-10-31T10:00:00Z", decided_at: null, decided_by: null, reason: null, outcome: {} },
        ],
      },
    });
    renderWithClient(PermissionsPanel, { agentName: "ledger" });

    // Under GitHub, once its row is open, not in a Held table of their own:
    // the grant, then the request waiting for review.
    await fireEvent.click(await screen.findByRole("button", { name: /GitHub/ }));
    const github = within(screen.getByRole("region", { name: "GitHub" }));
    expect(github.getByText("Read")).not.toBeNull();
    expect(github.getAllByText("ledger's repositories")).toHaveLength(2);
    // The set's size on both table rows, and once more as the row's grey detail.
    expect(within(github.getByRole("table")).getAllByText("2 repositories")).toHaveLength(2);
    expect(github.getByRole("button", { name: /GitHub/ }).textContent).toContain("2 repositories");
    expect(github.getAllByText("acme-eng/billing")).toHaveLength(2);
    expect(github.getByText("Review pull requests")).not.toBeNull();
    expect(github.getByText("waiting for review")).not.toBeNull();
    expect(github.getByText(/asked by owner@example.com/)).not.toBeNull();
    expect(github.getByRole("button", { name: /GitHub/ }).textContent).toContain("1 held, 1 waiting for review");
    // The other vendors hold nothing, and their closed rows say so; the ask
    // grant is a pair under Agent to agent, not a row here.
    const aws = within(screen.getByRole("region", { name: "AWS" }));
    expect(aws.getByText("Nothing held yet")).not.toBeNull();
    expect(screen.queryByText("auditor")).toBeNull();
    expect(screen.queryByRole("heading", { name: "Held" })).toBeNull();
  });

  it("lists only the vendors the room is connected to, and says where an admin adds one when there are none", async () => {
    // Issue #371: an owner can do nothing with an unconnected vendor, so it is
    // not a row; the room's connections decide the list.
    const unconnected = { ...ACCESS.vendors[1], connected: false, accounts: [{ ...ACCESS.vendors[1].accounts[0], connected: false }] };
    stubFetch({
      "/api/agents/ledger/access": { ...ACCESS, vendors: [unconnected, ACCESS.vendors[0]] },
      "/api/whoami": { email: "ops@example.com", operator: true, admin_panel: true, groups: [], groups_unavailable: false },
    });
    const first = renderWithClient(PermissionsPanel, { agentName: "ledger" });
    expect(await screen.findByRole("button", { name: /AWS/ })).not.toBeNull();
    expect(screen.queryByRole("button", { name: /Datadog/ })).toBeNull();
    expect(screen.queryByText(/not connected yet/)).toBeNull();
    first.unmount();

    stubFetch({
      "/api/agents/ledger/access": { ...ACCESS, vendors: [unconnected] },
      "/api/whoami": { email: "ops@example.com", operator: true, admin_panel: true, groups: [], groups_unavailable: false },
    });
    renderWithClient(PermissionsPanel, { agentName: "ledger" });
    expect(await screen.findByText(/No vendors are connected yet/)).not.toBeNull();
    // The word Integrations is the link into the admin panel for someone who may open it.
    expect((await screen.findByRole("link", { name: "Integrations" })).getAttribute("href")).toBe("#/admin/integrations");
  });

  it("shuts a vendor's picker on a second click", async () => {
    stubFetch({ "/api/agents/ledger/access": ACCESS });
    renderWithClient(PermissionsPanel, { agentName: "ledger" });

    const aws = await screen.findByRole("button", { name: /AWS/ });
    await fireEvent.click(aws);
    expect(aws.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("button", { name: /acme-audit/ })).not.toBeNull();
    await fireEvent.click(aws);
    expect(aws.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("button", { name: /acme-audit/ })).toBeNull();
  });
});

const HELD_ON_DATADOG = {
  ...ACCESS,
  grants: [
    { id: 1, agent: "ledger", integration: "datadog", permission: "datadog_monitors_read", tier: "read", gated: false, expires_at: null, granted_by: "admin@example.com", created_at: "2026-10-30T10:00:00Z", request_id: 3, state: "active" },
  ],
};

describe("PermissionsPanel: compact rows (issue #382)", () => {
  it("draws every vendor as a closed row with its one line, and opens one at a time", async () => {
    stubFetch({ "/api/agents/ledger/access": HELD_ON_DATADOG });
    renderWithClient(PermissionsPanel, { agentName: "ledger" });

    const datadog = await screen.findByRole("button", { name: /Datadog/ });
    // Closed: no table, no picker, each row with its summary and its action.
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByLabelText("Find a permission")).toBeNull();
    expect(document.querySelectorAll('[aria-expanded="true"]')).toHaveLength(0);
    expect(datadog.textContent).toContain("1 held");
    expect(datadog.textContent).toContain("Ask for more");
    expect(screen.getByRole("button", { name: /AWS/ }).textContent).toContain("Nothing held yet");

    await fireEvent.click(screen.getByRole("button", { name: /AWS/ }));
    expect(expanded(/AWS/)).toBe("true");
    expect(screen.getByRole("button", { name: /acme-audit/ })).not.toBeNull();
    await fireEvent.click(datadog);
    expect(expanded(/Datadog/)).toBe("true");
    expect(expanded(/AWS/)).toBe("false");
    expect(document.querySelectorAll('[aria-expanded="true"]')).toHaveLength(1);
    expect(screen.getByRole("table")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Revoke now" })).not.toBeNull();
  });

  it("sorts the vendors with something held or waiting first", async () => {
    stubFetch({ "/api/agents/ledger/access": HELD_ON_DATADOG });
    renderWithClient(PermissionsPanel, { agentName: "ledger" });
    await screen.findByRole("button", { name: /Datadog/ });
    expect(rowLabels()).toEqual(["Datadog", "AWS", "GitHub"]);
  });

  it("opens the row a link names and drops the query from the hash", async () => {
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    window.location.hash = "#/agents/ledger/access?vendor=datadog";
    stubFetch({ "/api/agents/ledger/access": ACCESS });
    renderWithClient(PermissionsPanel, { agentName: "ledger" });

    expect(await screen.findByLabelText(/monitors/)).not.toBeNull();
    expect(expanded(/Datadog/)).toBe("true");
    expect(expanded(/AWS/)).toBe("false");
    await vi.waitFor(() => expect(scroll).toHaveBeenCalled());
    expect(window.location.hash).toBe("#/agents/ledger/access");
  });

  it("keeps the row last opened open for the session", async () => {
    stubFetch({ "/api/agents/ledger/access": ACCESS });
    const first = renderWithClient(PermissionsPanel, { agentName: "ledger" });
    await fireEvent.click(await screen.findByRole("button", { name: /Datadog/ }));
    first.unmount();

    renderWithClient(PermissionsPanel, { agentName: "ledger" });
    expect(await screen.findByLabelText(/monitors/)).not.toBeNull();
    expect(expanded(/Datadog/)).toBe("true");
  });

  it("says the last failed attempt on the row, trimmed, with the whole reason on hover, until a lease clears it", async () => {
    // Owner ask 2026-11-01: the agent only says its credential did not
    // resolve; the owner reads the permission service's reason here. The
    // route already drops a failure a newer lease cleared (`last_failure`
    // null), so this panel reads one route and nothing else.
    const reason = "The vendor answered 422 for the narrowed token: the installation does not allow pull requests write. Ask a room admin to widen the app.";
    const twelveMinutesAgo = new Date(Date.now() - 12 * 60_000).toISOString();
    const failed = { ...ACCESS.vendors[0], last_failure: { at: twelveMinutesAgo, kind: "lease.failed", reason } };
    const fine = { ...ACCESS.vendors[1], last_failure: null };
    stubFetch({ "/api/agents/ledger/access": { ...ACCESS, vendors: [fine, failed] } });
    renderWithClient(PermissionsPanel, { agentName: "ledger" });

    const aws = await screen.findByRole("button", { name: /AWS/ });
    const line = aws.querySelector(".row__summary") as HTMLElement;
    expect(line.textContent?.trim()).toBe(
      "Last attempt failed 12 min ago: The vendor answered 422 for the narrowed token: the installation does not allow pull requests write.",
    );
    expect(line.getAttribute("title")).toBe(reason);
    expect(line.className).toContain("row__summary--warn");
    // A vendor with a failure has something to look at: it sorts first.
    expect(rowLabels()).toEqual(["AWS", "Datadog"]);
    expect(screen.getByRole("button", { name: /Datadog/ }).textContent).toContain("Nothing held yet");
    // Only the access route is read: the clearing is the server's.
    expect(vi.mocked(fetch).mock.calls.every(([url]) => String(url).includes("/access"))).toBe(true);
  });
});

describe("PermissionsPanel: GitHub (issue #365)", () => {
  it("lists the installation's repositories first, saves the set, then requests the level on the child", async () => {
    let access: unknown = ACCESS;
    let repositories: unknown = REPOSITORIES;
    stubRoutes({
      "/api/agents/ledger/access/requests": { body: { id: 9 } },
      "/api/agents/ledger/access": () => ({ body: access }),
      "/api/agents/ledger/github/repositories": () => ({ body: repositories }),
      "PUT /api/agents/ledger/github/repositories": (init) => {
        const sent = JSON.parse(String(init?.body)).repositories as string[];
        // The save makes the child: the access view and the list follow.
        access = { ...ACCESS, vendors: [...ACCESS.vendors.slice(0, 2), { ...GITHUB_SET, accounts: [{ ...GITHUB_SET.accounts[0], repositories: sent }] }] };
        repositories = { ...REPOSITORIES, selected: sent };
        return { body: { integration: "github-ledger", repositories: sent, changed: true } };
      },
    });
    renderWithClient(PermissionsPanel, { agentName: "ledger" });

    const github = await screen.findByRole("button", { name: /GitHub/ });
    expect(github.textContent).toContain("no repositories picked yet");
    await fireEvent.click(github);

    // The repository step, not the permissions: searchable, nothing ticked,
    // Save dead until something is. The placeholder says how many the room
    // can see, from the payload's total.
    const search = await screen.findByLabelText("Find a repository");
    expect(search.getAttribute("placeholder")).toBe("Find a repository (3 the room can see)");
    expect(screen.queryByLabelText("Find a permission")).toBeNull();
    const save = screen.getByRole("button", { name: "Save repositories" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(screen.getByText("Pick at least one repository.")).not.toBeNull();

    await fireEvent.input(search, { target: { value: "acme-eng/l" } });
    expect(screen.queryByLabelText(/billing/)).toBeNull();
    await fireEvent.click(screen.getByLabelText(/acme-eng\/ledger/));
    await fireEvent.input(search, { target: { value: "" } });
    await fireEvent.click(screen.getByLabelText(/acme-eng\/billing/));
    expect(screen.getByText("2 picked")).not.toBeNull();
    expect(save.disabled).toBe(false);

    await fireEvent.click(save);
    await vi.waitFor(() => expect(putCalls()).toHaveLength(1));
    expect(JSON.parse(String(putCalls()[0].body))).toEqual({ repositories: ["acme-eng/ledger", "acme-eng/billing"] });

    // On to the level, with a clear done state for the save: the three rows
    // by label, each with its sentence, Open pull requests greyed because
    // this set's ceiling stops at Review.
    const read = await screen.findByLabelText(/^Read/);
    expect(screen.getByText("Repositories saved. Pick the level next.")).not.toBeNull();
    expect(screen.getByText("Read, plus comment on and review pull requests")).not.toBeNull();
    const review = screen.getByLabelText(/Review pull requests/) as HTMLInputElement;
    const open = screen.getByLabelText(/Open pull requests/) as HTMLInputElement;
    expect(review.disabled).toBe(false);
    expect(open.disabled).toBe(true);
    expect(screen.getByText("the connection does not allow this")).not.toBeNull();
    expect(screen.getByText("acme-eng/ledger, acme-eng/billing")).not.toBeNull();

    await fireEvent.click(read);
    await fireEvent.click(review);
    await fireEvent.click(screen.getByRole("button", { name: "Confirm request" }));

    await vi.waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(([url]) => String(url).endsWith("/access/requests"));
      expect(call).toBeDefined();
      const body = JSON.parse(String((call as [RequestInfo | URL, RequestInit])[1].body));
      // The child, never the room's connection.
      expect(body).toEqual({ integration: "github-ledger", permissions: ["github_repos_read", "github_pulls_review"], note: null });
    });
  });

  // Wire payload (PUT /api/agents/{name}/github/repositories, bab 0.4.26): one box asks for
  // every repository as `{all: true}`, never as a list, and that set offers Read alone.
  it("saves every repository as one set and offers Read alone on it", async () => {
    let access: unknown = ACCESS;
    let repositories: unknown = REPOSITORIES;
    stubRoutes({
      "/api/agents/ledger/access": () => ({ body: access }),
      "/api/agents/ledger/github/repositories": () => ({ body: repositories }),
      "PUT /api/agents/ledger/github/repositories": () => {
        const every = { ...GITHUB_SET.accounts[0], repositories: [], all_repositories: true, ceiling: GITHUB_ROWS.map((r) => r.name) };
        access = { ...ACCESS, vendors: [...ACCESS.vendors.slice(0, 2), { ...GITHUB_SET, accounts: [every] }] };
        repositories = { ...REPOSITORIES, selected: [], all: true };
        return { body: { integration: "github-ledger", repositories: [], all: true, changed: true } };
      },
    });
    renderWithClient(PermissionsPanel, { agentName: "ledger" });

    await fireEvent.click(await screen.findByRole("button", { name: /GitHub/ }));
    await fireEvent.click(await screen.findByLabelText(/Every repository, Read only/));
    expect(screen.queryByLabelText("Find a repository")).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Save repositories" }));
    await vi.waitFor(() => expect(putCalls()).toHaveLength(1));
    expect(JSON.parse(String(putCalls()[0].body))).toEqual({ all: true });

    const read = (await screen.findByLabelText(/^Read/)) as HTMLInputElement;
    expect(read.disabled).toBe(false);
    expect((screen.getByLabelText(/Review pull requests/) as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText(/Open pull requests/) as HTMLInputElement).disabled).toBe(true);
    expect(screen.getAllByText("every repository is Read only; pick repositories for this")).toHaveLength(2);
  });

  it("pre-checks the saved set, keeps Save dead until it changes, and shows the 422 sentence inline", async () => {
    stubRoutes({
      "/api/agents/ledger/access": { body: { ...ACCESS, vendors: [...ACCESS.vendors.slice(0, 2), GITHUB_SET] } },
      "/api/agents/ledger/github/repositories": { body: { ...REPOSITORIES, selected: ["acme-eng/ledger", "acme-eng/billing"] } },
      "PUT /api/agents/ledger/github/repositories": { status: 422, body: { error: "The installation cannot see acme-eng/website." } },
    });
    renderWithClient(PermissionsPanel, { agentName: "ledger" });

    const github = await screen.findByRole("button", { name: /GitHub/ });
    expect(github.textContent).toContain("2 repositories");
    await fireEvent.click(github);

    expect(((await screen.findByLabelText(/acme-eng\/ledger/)) as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText(/acme-eng\/billing/) as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText(/acme-eng\/website/) as HTMLInputElement).checked).toBe(false);
    // Unchanged: the way on is the level, not a save.
    expect(screen.queryByRole("button", { name: "Save repositories" })).toBeNull();
    expect(screen.getByRole("button", { name: "Choose the level" })).not.toBeNull();

    await fireEvent.click(screen.getByLabelText(/acme-eng\/website/));
    const save = screen.getByRole("button", { name: "Save repositories" }) as HTMLButtonElement;
    expect(save.disabled).toBe(false);
    await fireEvent.click(save);

    expect((await screen.findByRole("alert")).textContent).toContain("The installation cannot see acme-eng/website.");
    // Still on the repository step, the set as ticked.
    expect((screen.getByLabelText(/acme-eng\/website/) as HTMLInputElement).checked).toBe(true);
    expect(screen.queryByLabelText("Find a permission")).toBeNull();
  });

  it("goes straight on to the level from a saved set, and back to the repositories", async () => {
    stubRoutes({
      "/api/agents/ledger/access": { body: { ...ACCESS, vendors: [...ACCESS.vendors.slice(0, 2), GITHUB_SET] } },
      "/api/agents/ledger/github/repositories": { body: { ...REPOSITORIES, selected: ["acme-eng/ledger", "acme-eng/billing"] } },
    });
    renderWithClient(PermissionsPanel, { agentName: "ledger" });

    await fireEvent.click(await screen.findByRole("button", { name: /GitHub/ }));
    await fireEvent.click(await screen.findByRole("button", { name: "Choose the level" }));
    expect(screen.getByLabelText("Find a permission")).not.toBeNull();
    expect(screen.getByLabelText(/Open pull requests/)).not.toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Change repositories" }));
    expect(screen.getByLabelText("Find a repository")).not.toBeNull();
  });

  it("draws the picked repositories first, then at most forty matches for the typed query", async () => {
    stubRoutes({
      "/api/agents/ledger/access": { body: { ...ACCESS, vendors: [...ACCESS.vendors.slice(0, 2), GITHUB_SET] } },
      "/api/agents/ledger/github/repositories": { body: { ...MANY_REPOSITORIES, selected: ["acme-eng/ledger", "acme-eng/billing"] } },
    });
    renderWithClient(PermissionsPanel, { agentName: "ledger" });

    await fireEvent.click(await screen.findByRole("button", { name: /GitHub/ }));
    const search = await screen.findByLabelText("Find a repository");
    expect(search.getAttribute("placeholder")).toBe("Find a repository (2279 the room can see)");

    // Nothing typed: the two picked, then the first forty of the rest, and
    // a line saying how many more there are. (The repository rows: the
    // Every repository box above them is not one.)
    const repoBoxes = () => screen.getAllByRole("checkbox").filter((b) => !b.closest("label")?.textContent?.includes("Every repository"));
    let boxes = repoBoxes();
    expect(boxes).toHaveLength(42);
    expect((boxes[0] as HTMLInputElement).checked).toBe(true);
    expect((boxes[1] as HTMLInputElement).checked).toBe(true);
    expect(screen.getByText(/81 more match/)).not.toBeNull();

    // Typing narrows the rest; the picked stay, so they can be unticked.
    await fireEvent.input(search, { target: { value: "svc-00" } });
    boxes = repoBoxes();
    expect(boxes).toHaveLength(12);
    expect(screen.getByLabelText(/acme-eng\/ledger/)).not.toBeNull();
    expect(screen.getByLabelText(/svc-009/)).not.toBeNull();
    expect(screen.queryByLabelText(/svc-010/)).toBeNull();
    expect(screen.queryByText(/more match/)).toBeNull();
  });

  it("shows the note and no list when the room cannot list repositories", async () => {
    stubRoutes({
      "/api/agents/ledger/access": { body: ACCESS },
      "/api/agents/ledger/github/repositories": { body: { available: false, repositories: [], selected: [], note: "The room's GitHub app cannot list repositories: the installation was not found." } },
    });
    renderWithClient(PermissionsPanel, { agentName: "ledger" });

    await fireEvent.click(await screen.findByRole("button", { name: /GitHub/ }));
    expect(await screen.findByText(/cannot list repositories/)).not.toBeNull();
    expect(screen.queryByLabelText("Find a repository")).toBeNull();
    expect(screen.queryByRole("button", { name: "Save repositories" })).toBeNull();
  });

  it("sends a repository added to a set the agent holds a level on for review, and marks it waiting", async () => {
    const saved = ["acme-eng/ledger", "acme-eng/billing"];
    const waiting = { id: 12, added: ["acme-eng/website"], removed: [], requested_by: "dana@example.com", created_at: "2026-11-12T10:00:00" };
    let repositories: unknown = { ...REPOSITORIES, selected: saved };
    stubRoutes({
      "/api/agents/ledger/access": { body: { ...ACCESS, vendors: [...ACCESS.vendors.slice(0, 2), GITHUB_SET] } },
      "/api/agents/ledger/github/repositories": () => ({ body: repositories }),
      "PUT /api/agents/ledger/github/repositories": () => {
        // The set in force is unchanged: the addition is a reviewer's to allow.
        repositories = { ...REPOSITORIES, selected: saved, waiting };
        return { status: 202, body: { integration: "github-ledger", repositories: saved, changed: false, waiting } };
      },
    });
    renderWithClient(PermissionsPanel, { agentName: "ledger" });

    await fireEvent.click(await screen.findByRole("button", { name: /GitHub/ }));
    await fireEvent.click(await screen.findByLabelText(/acme-eng\/website/));
    await fireEvent.click(screen.getByRole("button", { name: "Save repositories" }));
    await vi.waitFor(() => expect(putCalls()).toHaveLength(1));
    expect(JSON.parse(String(putCalls()[0].body))).toEqual({ repositories: [...saved, "acme-eng/website"] });

    // Still on the repositories: the new one stays ticked and says it waits,
    // and the way on is the level for the set the agent has now.
    expect(await screen.findByText(/Waiting for review: acme-eng\/website\./)).not.toBeNull();
    const added = screen.getByLabelText(/acme-eng\/website/) as HTMLInputElement;
    expect(added.checked).toBe(true);
    expect(added.closest("label")?.textContent).toContain("waiting for review");
    expect(screen.queryByText("Repositories saved. Pick the level next.")).toBeNull();
    expect(screen.getByRole("button", { name: "Choose the level" })).not.toBeNull();
  });

  it("says a connected agent is waiting on a merge the room could not make", async () => {
    // An agent that runs its team's own code reaches the gateway only once
    // its repository carries the room's wiring; a granted permission that
    // silently does nothing was the alternative.
    const pull = "https://github.com/acme-eng/ledger/pull/9";
    stubFetch({ "/api/agents/ledger/access": { ...ACCESS, wiring: { state: "waiting", pull_request: pull } } });
    renderWithClient(PermissionsPanel, { agentName: "ledger" });

    const note = await screen.findByText(/The room could not merge its wiring pull request/);
    expect(within(note).getByRole("link", { name: "Open the pull request" }).getAttribute("href")).toBe(pull);
  });
});

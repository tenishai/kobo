<script lang="ts">
  // What this agent can reach (the Access tab, issue #365; opened on the
  // vendors by issue #368). One block per vendor the room is connected to,
  // with its logo: what the agent holds there (each grant with its tier,
  // expiry, who granted it and Revoke) and what is waiting for review, then
  // the picker to ask for more, opened in place under the vendor. The
  // picker walks the way access is granted: for a cloud the account,
  // project or subscription, then a searchable list of permissions. One
  // Confirm sends the request to the room's reviewers (Admin > Review).
  // Approved permissions are grants the agent borrows an hour of access
  // against; the decisions are the reviewers', never this page's.
  //
  // GitHub is the one vendor with a step of its own. An agent reaches
  // GitHub through its own repository set (one set per agent, written here
  // and replaced here), so opening GitHub lists repositories first; Save
  // writes the set, and only then does the level (Read, Review pull
  // requests, Open pull requests) get picked. The request targets the set,
  // never the room's connection. The list never draws every repository the
  // room can see: the ones already picked come first, then the best matches
  // for what was typed, the way the connect-a-repo field works.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { errorMessage, type AccessAccount, type AccessGrant, type AccessPermission, type AccessRequest, type AccessVendor, type GithubRepository } from "../lib/api";
  import { agentAccessQuery, githubRepositoriesQuery, putGithubRepositoriesMutation, requestAgentAccessMutation, revokeAgentAccessMutation, whoamiQuery } from "../lib/queries";
  import { formatHash } from "../lib/router.svelte";
  import { serverDate } from "../lib/time";
  import { toast } from "../lib/toast.svelte";
  import VendorMark from "./VendorMark.svelte";

  let { agentName, frozen = false }: { agentName: string; frozen?: boolean } = $props();

  const GITHUB = "github";
  /** Asking another agent is a permission too, but it is shown under the
   * Harness's Agent to agent section, where the pair it makes lives; the
   * tables here leave it out. */
  const ASK_PERMISSION = "a2a_ask";
  /** How many repository matches the picker draws at once. */
  const REPO_SHOWN = 40;

  const queryClient = useQueryClient();
  // agentName is read once: AgentDetail remounts the tab's panels on a
  // route change (`{#key route.name}`), like every other panel here.
  const access = createQuery(() => (agentAccessQuery(agentName)));
  const ask = createMutation(() => (requestAgentAccessMutation(queryClient, agentName)));
  const revoke = createMutation(() => (revokeAgentAccessMutation(queryClient, agentName)));
  // Read only when the empty state is drawn, to know whether its word
  // Integrations may link into the admin panel; the normal view reads the
  // access route and nothing else.
  const whoami = createQuery(() => ({ ...whoamiQuery(), enabled: access.isSuccess && (access.data?.vendors ?? []).every((v) => !v.connected) }));
  const canOpenAdmin = $derived(Boolean(whoami.data?.operator && whoami.data?.admin_panel));

  // Where the picker is: nothing (every vendor shut), a vendor (its
  // accounts, or for GitHub its repositories), or an account (its
  // permissions). Explicit state, so ticking a box changes nothing here.
  let vendorId = $state<string | null>(null);
  let accountId = $state<string | null>(null);
  let query = $state("");
  let picked = $state<string[]>([]);
  let note = $state("");

  const grants = $derived((access.data?.grants ?? []).filter((g) => g.permission !== ASK_PERMISSION));
  const pending = $derived(
    (access.data?.pending ?? []).filter((r) => (r.permissions ?? []).some((p) => p !== ASK_PERMISSION)),
  );
  // The vendors the ROOM is connected to, nothing else (issue #371): an owner
  // can do nothing with an unconnected vendor, so it is not listed. As admins
  // connect more under Integrations, more appear here.
  const vendors = $derived((access.data?.vendors ?? []).filter((v) => v.connected));
  const vendor = $derived(vendors.find((v) => v.id === vendorId) ?? null);
  const account = $derived(vendor?.accounts.find((a) => a.id === accountId) ?? null);
  const isGithub = $derived(vendor?.id === GITHUB);
  /** GitHub's one account: the agent's repository set, or the placeholder
   * before one is saved. */
  const githubAccount = $derived(isGithub ? (vendor?.accounts[0] ?? null) : null);

  const held = $derived(new Set(grants.filter((g) => g.state !== "expired").map((g) => `${g.integration}:${g.permission}`)));
  const waiting = $derived(new Set(pending.flatMap((r) => (r.permissions ?? []).map((p) => `${r.integration}:${p}`))));

  /** Every permission's label, across vendors, for the tables. */
  const labels = $derived.by(() => {
    const map = new Map<string, string>();
    for (const v of vendors) {
      for (const g of v.groups) {
        for (const p of g.permissions) {
          if (p.label) map.set(p.name, p.label);
        }
      }
    }
    return map;
  });
  /** Every account by id, so a row can name it and, for a GitHub set, list
   * its repositories. */
  const accounts = $derived.by(() => {
    const map = new Map<string, AccessAccount>();
    for (const v of vendors) {
      for (const a of v.accounts) map.set(a.id, a);
    }
    return map;
  });
  /** Which vendor an account (a grant's or a request's `integration`)
   * belongs to. A vendor's own id counts too: a service vendor's one
   * connection is often named by it. */
  const vendorOf = $derived.by(() => {
    const map = new Map<string, string>();
    for (const v of vendors) {
      map.set(v.id, v.id);
      for (const a of v.accounts) map.set(a.id, v.id);
    }
    return map;
  });

  function grantsAt(v: AccessVendor): AccessGrant[] {
    return grants.filter((g) => vendorOf.get(g.integration) === v.id);
  }
  function pendingAt(v: AccessVendor): AccessRequest[] {
    return pending.filter((r) => vendorOf.get(r.integration) === v.id);
  }
  /** Grants and requests naming an account no vendor claims any more: shown
   * at the end rather than lost. */
  const strayGrants = $derived(grants.filter((g) => !vendorOf.has(g.integration)));
  const strayPending = $derived(pending.filter((r) => !vendorOf.has(r.integration)));

  function heldAt(accountIdOrVendor: string): number {
    return grants.filter((g) => g.state !== "expired" && g.integration === accountIdOrVendor).length;
  }
  function openVendor(v: AccessVendor): void {
    vendorId = v.id;
    query = "";
    picked = [];
    repoDraft = null;
    repoError = null;
    repoSaved = false;
    // A service vendor is one connection: go straight to its permissions.
    // GitHub stops at its repositories first.
    accountId = v.kind === "service" && v.accounts.length === 1 && v.id !== GITHUB ? v.accounts[0].id : null;
  }
  function toggleVendor(v: AccessVendor): void {
    if (vendorId === v.id) {
      closePicker();
    } else {
      openVendor(v);
    }
  }
  function openAccount(a: AccessAccount): void {
    if (!a.connected) return;
    accountId = a.id;
    query = "";
    picked = [];
  }
  function back(): void {
    if (accountId !== null && (vendor?.kind !== "service" || isGithub)) {
      accountId = null;
    } else {
      accountId = null;
      vendorId = null;
    }
    query = "";
    picked = [];
  }
  function closePicker(): void {
    accountId = null;
    vendorId = null;
    picked = [];
    query = "";
    repoSaved = false;
  }

  type RowState = "held" | "waiting" | "above" | "open";
  function rowState(a: AccessAccount, p: AccessPermission): RowState {
    const key = `${a.id}:${p.name}`;
    if (held.has(key)) return "held";
    if (waiting.has(key)) return "waiting";
    if (!a.ceiling.includes(p.name)) return "above";
    return "open";
  }
  const WHY: Record<RowState, string> = { held: "held", waiting: "waiting for review", above: "the connection does not allow this", open: "" };

  // Every row of the vendor's tree, read and write tiers alike: a row the
  // connection's ceiling does not carry stays greyed with the reason.
  const matches = $derived.by(() => {
    if (!vendor) return [] as { name: string; permissions: AccessPermission[] }[];
    const q = query.trim().toLowerCase();
    return vendor.groups
      .map((g) => ({
        name: g.name,
        permissions: g.permissions.filter(
          (p) =>
            !q ||
            p.name.includes(q) ||
            (p.label ?? "").toLowerCase().includes(q) ||
            g.name.toLowerCase().includes(q) ||
            (p.description ?? "").toLowerCase().includes(q),
        ),
      }))
      .filter((g) => g.permissions.length > 0);
  });
  const offeredCount = $derived(
    account && vendor ? vendor.groups.reduce((n, g) => n + g.permissions.filter((p) => account.ceiling.includes(p.name)).length, 0) : 0,
  );

  function toggle(name: string): void {
    picked = picked.includes(name) ? picked.filter((n) => n !== name) : [...picked, name];
  }

  function confirm(): void {
    if (!account || picked.length === 0) return;
    const target = account;
    const permissions = [...picked];
    ask.mutate(
      { integration: target.id, permissions, note: note.trim() || null },
      {
        onSuccess: () => {
          toast.success("Requested", `${permissions.length} permission${permissions.length === 1 ? "" : "s"} on ${target.name} wait for a reviewer.`);
          picked = [];
          note = "";
        },
        onError: (err: unknown) => toast.error("Could not request", errorMessage(err, "Try again.")),
      },
    );
  }

  function handleRevoke(g: AccessGrant): void {
    revoke.mutate(g.id, {
      onSuccess: () => toast.success("Revoked", `${agentName} no longer holds ${label(g.permission)} on ${where(g.integration)}; open sessions end within the hour.`),
      onError: (err: unknown) => toast.error("Could not revoke", errorMessage(err, "Try again.")),
    });
  }

  // --- GitHub: the repository step ------------------------------------------
  // The list is read only while the picker is on GitHub. `repoDraft` is what
  // the person has ticked; null means "whatever the room says is selected",
  // so a refetch after Save shows the saved set and an untouched list follows
  // the server.
  const repos = createQuery(() => ({ ...githubRepositoriesQuery(agentName), enabled: isGithub }));
  const saveRepos = createMutation(() => (putGithubRepositoriesMutation(queryClient, agentName)));
  let repoDraft = $state<string[] | null>(null);
  let repoError = $state<string | null>(null);
  let repoQuery = $state("");
  /** True right after a save landed, until the picker moves on or shuts:
   * the level step says the set is saved. */
  let repoSaved = $state(false);

  const repoSelected = $derived(repos.data?.selected ?? []);
  const repoChecked = $derived(repoDraft ?? repoSelected);
  const repoChanged = $derived(
    repoChecked.length !== repoSelected.length || repoChecked.some((r) => !repoSelected.includes(r)),
  );
  const repoTotal = $derived(repos.data?.total ?? repos.data?.repositories.length ?? 0);
  /** The rows the picker draws: what is picked first (every one of them,
   * so each can be unticked), then the best matches for the typed query,
   * capped. A name that starts with the query ranks above one that merely
   * contains it. */
  const repoRows = $derived.by(() => {
    const all = repos.data?.repositories ?? [];
    const byName = new Map(all.map((r) => [r.full_name, r]));
    const q = repoQuery.trim().toLowerCase();
    const pickedRows: GithubRepository[] = repoChecked.map(
      (name) => byName.get(name) ?? { full_name: name, private: true, default_branch: null },
    );
    const rest = all.filter((r) => !repoChecked.includes(r.full_name) && (!q || r.full_name.toLowerCase().includes(q)));
    if (q) {
      rest.sort((a, b) => {
        const ai = a.full_name.toLowerCase().indexOf(q);
        const bi = b.full_name.toLowerCase().indexOf(q);
        return ai - bi || a.full_name.localeCompare(b.full_name);
      });
    }
    return { picked: pickedRows, matches: rest.slice(0, REPO_SHOWN), more: Math.max(0, rest.length - REPO_SHOWN) };
  });

  function toggleRepo(fullName: string): void {
    repoError = null;
    repoDraft = repoChecked.includes(fullName) ? repoChecked.filter((r) => r !== fullName) : [...repoChecked, fullName];
  }
  function saveRepositories(): void {
    if (!repoChanged || repoChecked.length === 0) return;
    repoError = null;
    saveRepos.mutate([...repoChecked], {
      onSuccess: (res) => {
        repoDraft = null;
        repoSaved = true;
        // The set is the account the request targets: step on to the level.
        accountId = res.integration;
        picked = [];
        query = "";
        toast.success("Repositories saved", `${res.repositories.length} repositor${res.repositories.length === 1 ? "y" : "ies"} for ${agentName}. Pick the level next.`);
      },
      onError: (err: unknown) => {
        repoError = errorMessage(err, "The repositories could not be saved. Try again.");
      },
    });
  }
  function chooseLevel(): void {
    if (!githubAccount?.has_repo_set) return;
    accountId = githubAccount.id;
    picked = [];
    query = "";
  }

  function expires(g: AccessGrant): string {
    if (!g.expires_at) return "standing";
    const ms = serverDate(g.expires_at).getTime() - Date.now();
    if (ms <= 0) return "expired";
    const days = Math.ceil(ms / 86_400_000);
    return days <= 1 ? "today" : `in ${days} days`;
  }
  function service(name: string): string {
    // aws_s3_read -> "s3"; datadog_monitors_read -> "monitors"
    return name.replace(/^[a-z]+_/, "").replace(/_read$/, "").replace(/_/g, " ");
  }
  /** A permission as the page names it: its label when the catalogue has
   * one, else the service part of its id. */
  function label(name: string): string {
    return labels.get(name) ?? service(name);
  }
  function permLabel(p: AccessPermission): string {
    return p.label ?? service(p.name);
  }
  /** Where a grant or request points: the account's name when the view
   * knows it, else the id as given. */
  function where(integration: string): string {
    return accounts.get(integration)?.name ?? integration;
  }
  /** The repository set behind an integration, when it is a GitHub set. */
  function repoSet(integration: string): string[] | null {
    const a = accounts.get(integration);
    return a?.has_repo_set ? a.repositories : null;
  }
  function count(n: number, one: string, many: string): string {
    return `${n} ${n === 1 ? one : many}`;
  }
  function requestLabels(r: AccessRequest): string {
    return (r.permissions ?? []).filter((p) => p !== ASK_PERMISSION).map(label).join(", ");
  }
  /** The highest tier a request asks for, for its chip. */
  function requestTier(r: AccessRequest): string {
    const tiers = Object.keys(r.tiers ?? {});
    for (const t of ["destroy", "mutate", "read"]) if (tiers.includes(t)) return t;
    return tiers[0] ?? "read";
  }
  /** GitHub's one line under its name is about the AGENT, not the room: how
   * many repositories it may reach. Other vendors' name and mark stand alone. */
  function connectionNote(v: AccessVendor): string | null {
    if (v.id !== GITHUB) return null;
    return v.accounts[0]?.has_repo_set
      ? count(v.accounts[0].repositories.length, "repository", "repositories")
      : "no repositories picked yet";
  }
</script>

{#snippet repoList(repositories: string[])}
  <details class="repos">
    <summary class="repos__summary" title={repositories.join("\n")}>{count(repositories.length, "repository", "repositories")}</summary>
    <ul class="repos__list">
      {#each repositories as r (r)}<li><code>{r}</code></li>{/each}
    </ul>
  </details>
{/snippet}

<!-- What the agent holds at one vendor and what is waiting there: one table,
     held rows then waiting rows, or one quiet line when there is nothing. -->
{#snippet heldTable(rows: AccessGrant[], asked: AccessRequest[], canDecide: boolean)}
  {#if rows.length === 0 && asked.length === 0}
    <p class="vendor__none">Nothing held yet.</p>
  {:else}
    <table class="panel__table vendor__table">
      <thead><tr><th scope="col">Permission</th><th scope="col">Where</th><th scope="col">Tier</th><th scope="col">Expires</th><th scope="col">Granted by</th><th scope="col" class="panel__cell--right"><span class="panel__sr">Revoke</span></th></tr></thead>
      <tbody>
        {#each rows as g (g.id)}
          {@const set = repoSet(g.integration)}
          <tr class:quiet={g.state === "expired"}>
            <td><span class="perm" class:perm--named={labels.has(g.permission)} title={g.permission}>{label(g.permission)}</span></td>
            <td>
              {where(g.integration)}
              {#if set}{@render repoList(set)}{/if}
            </td>
            <td><span class="tier tier--{g.tier}">{g.tier}</span></td>
            <td>{expires(g)}</td>
            <td class="quiet">{g.granted_by}</td>
            <td class="panel__cell--right">
              {#if canDecide}
                <button type="button" class="btn btn--quiet btn--danger" disabled={frozen || revoke.isPending} onclick={() => handleRevoke(g)}>Revoke now</button>
              {/if}
            </td>
          </tr>
        {/each}
        {#each asked as r (r.id)}
          {@const set = r.repositories ?? repoSet(r.integration)}
          <tr class="vendor__waiting">
            <td><span class="perm" class:perm--named={r.permissions.some((p) => labels.has(p))}>{requestLabels(r)}</span></td>
            <td>
              {where(r.integration)}
              {#if set}{@render repoList(set)}{/if}
            </td>
            <td><span class="tier tier--{requestTier(r)}">{requestTier(r)}</span></td>
            <td><span class="quiet">waiting for review</span></td>
            <td class="quiet">asked by {r.requested_by}{#if r.note}: “{r.note}”{/if}</td>
            <td></td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
{/snippet}

{#if access.isPending}
  <p class="panel__note">Reading the permissions…</p>
{:else if access.isError}
  <p class="panel__note">{errorMessage(access.error, "Access is off for this room.")}</p>
{:else if access.data}
  <div class="vendors">
    <div class="vendors__head">
      <h3 class="panel__subheading">What this agent can reach</h3>
      <p class="vendors__lead">
        What this agent may do at each vendor. It holds no standing credential: when a task
        needs one, it borrows an hour of access scoped to exactly these permissions, and the
        vendor's audit trail names the agent.
        {#if vendors.length > 0 && access.data.can_request}
          Open a vendor to ask for more; one Confirm sends the request to the room's reviewers.
        {:else if vendors.length > 0}
          The agent's owner or a room operator asks for more; you can see what it holds.
        {/if}
      </p>
    </div>

    {#if vendors.length === 0}
      <p class="panel__empty">
        No vendors are connected yet. A kobo admin adds them under
        {#if canOpenAdmin}<a href={formatHash({ page: "admin", pane: "integrations" })}>Integrations</a>{:else}Integrations{/if}.
      </p>
    {/if}

    {#each vendors as v (v.id)}
      {@const open = vendorId === v.id}
      {@const canOpen = access.data.can_request}
      {@const vendorNote = connectionNote(v)}
      {@const heldCount = grantsAt(v).filter((g) => g.state !== "expired").length}
      <section class="vendor" class:vendor--open={open} aria-label={v.label}>
        <button
          type="button"
          class="vendor__head"
          aria-expanded={canOpen ? open : undefined}
          disabled={!canOpen || frozen}
          onclick={() => toggleVendor(v)}
        >
          <VendorMark id={v.id} name={v.label} size={18} />
          <span class="vendor__name">{v.label}</span>
          <span class="vendor__note">
            {#if vendorNote}{vendorNote}{/if}
            {#if heldCount}{#if vendorNote} · {/if}{heldCount} held{/if}
          </span>
          {#if canOpen}
            <span class="vendor__action">{open ? "Close" : "Ask for more"}</span>
          {/if}
        </button>

        {@render heldTable(grantsAt(v), pendingAt(v), access.data.can_decide)}

        {#if open && vendor}
          <div class="vendor__picker">
            {#if isGithub && !account}
              <!-- GitHub: the agent's repositories -->
              {#if repos.isPending}
                <p class="panel__note">Reading the repositories…</p>
              {:else if repos.isError}
                <p class="panel__note panel__note--fail">{errorMessage(repos.error, "The repositories could not be read.")}</p>
              {:else if repos.data && !repos.data.available}
                <p class="panel__note">{repos.data.note ?? "The room cannot list repositories right now."}</p>
              {:else if repos.data}
                <p class="vendor__copy">
                  Which repositories this agent may reach. One set per agent; the level you pick next
                  applies to every repository in it. Saving a changed set replaces the old one.
                </p>
                <div class="search">
                  <input class="field__input" type="search" placeholder={`Find a repository (${repoTotal} the room can see)`} bind:value={repoQuery} aria-label="Find a repository" disabled={frozen} />
                </div>
                {#if repoRows.picked.length === 0 && repoRows.matches.length === 0}
                  <p class="panel__empty">{repoQuery.trim() ? `Nothing matches “${repoQuery}”.` : "Type part of a repository's name to find it."}</p>
                {:else}
                  <div class="services">
                    {#if repoRows.picked.length > 0}
                      <p class="group">Picked</p>
                      {#each repoRows.picked as r (r.full_name)}
                        <label class="svc svc--repo">
                          <input type="checkbox" checked={true} disabled={frozen || saveRepos.isPending} onchange={() => toggleRepo(r.full_name)} />
                          <span class="svc__name">{r.full_name}</span>
                          <span class="svc__desc quiet">{r.private ? "private" : "public"}{#if r.default_branch} · {r.default_branch}{/if}</span>
                        </label>
                      {/each}
                    {/if}
                    {#if repoRows.matches.length > 0}
                      {#if repoRows.picked.length > 0}<p class="group">{repoQuery.trim() ? "Matches" : "More"}</p>{/if}
                      {#each repoRows.matches as r (r.full_name)}
                        <label class="svc svc--repo">
                          <input type="checkbox" checked={false} disabled={frozen || saveRepos.isPending} onchange={() => toggleRepo(r.full_name)} />
                          <span class="svc__name">{r.full_name}</span>
                          <span class="svc__desc quiet">{r.private ? "private" : "public"}{#if r.default_branch} · {r.default_branch}{/if}</span>
                        </label>
                      {/each}
                    {/if}
                    {#if repoRows.more > 0}
                      <p class="panel__note services__more">{repoRows.more} more match{repoRows.more === 1 ? "es" : ""}. Keep typing to narrow the list.</p>
                    {/if}
                  </div>
                {/if}
                <div class="confirm" class:confirm--live={repoChanged}>
                  <span class="confirm__count">{repoChecked.length} picked</span>
                  {#if repoError}
                    <span class="panel__note panel__note--fail confirm__error" role="alert">{repoError}</span>
                  {:else if saveRepos.isPending}
                    <span class="panel__note confirm__error" role="status">Saving the set…</span>
                  {:else if repoChecked.length === 0}
                    <span class="panel__note confirm__error">Pick at least one repository.</span>
                  {:else}
                    <span class="confirm__spacer"></span>
                  {/if}
                  {#if githubAccount?.has_repo_set && !repoChanged}
                    <button type="button" class="btn btn--primary" disabled={frozen} onclick={chooseLevel}>Choose the level</button>
                  {:else}
                    <button type="button" class="btn btn--primary" disabled={frozen || !repoChanged || repoChecked.length === 0 || saveRepos.isPending} onclick={saveRepositories}>
                      {saveRepos.isPending ? "Saving…" : "Save repositories"}
                    </button>
                  {/if}
                </div>
              {/if}
            {:else if !account}
              <!-- A cloud's accounts -->
              <p class="vendor__copy">Pick the {vendor.account_noun ?? "account"} the agent needs something in.</p>
              <div class="table-card picker">
                {#each vendor.accounts as a (a.id)}
                  <button type="button" class="row-link" disabled={!a.connected || frozen} onclick={() => openAccount(a)} title={a.connected ? "" : "Not connected yet; a room admin connects it on Admin › Integrations"}>
                    <span class="row-link__name">{a.name}{#if a.organization}<span class="quiet org"> · {vendor.organizations.find((o) => o.id === a.organization)?.name ?? a.organization}</span>{/if}</span>
                    <span class="row-link__note">{a.connected ? `${heldAt(a.id)} held` : "not connected yet"}</span>
                    <span class="row-link__chevron" aria-hidden="true">›</span>
                  </button>
                {/each}
              </div>
            {:else}
              <!-- The permissions -->
              <nav class="crumbs" aria-label="Where you are">
                {#if vendor.kind !== "service"}<button type="button" class="crumb" onclick={back}>‹ {vendor.account_noun ? `${vendor.account_noun.slice(0, 1).toUpperCase()}${vendor.account_noun.slice(1)}s` : "Accounts"}</button>{/if}
                {#if isGithub}
                  <span class="crumb crumb--here">{count(account.repositories.length, "repository", "repositories")}</span>
                {:else}
                  <span class="crumb crumb--here">{account.name}</span>
                {/if}
              </nav>
              {#if isGithub}
                {#if repoSaved}
                  <p class="vendor__saved" role="status">Repositories saved. Pick the level next.</p>
                {/if}
                <p class="panel__note repo-set">
                  <span class="repo-set__list" title={account.repositories.join("\n")}>{account.repositories.join(", ")}</span>
                  <button type="button" class="crumb" disabled={frozen} onclick={back}>Change repositories</button>
                </p>
                <p class="vendor__copy">
                  The levels add up: Review includes Read, Open pull requests includes Review. The
                  agent can never approve a pull request it opened.
                </p>
              {/if}
              <div class="search">
                <input class="field__input" type="search" placeholder={`Find a permission (${offeredCount} this connection allows)`} bind:value={query} aria-label="Find a permission" disabled={frozen} />
              </div>
              {#if matches.length === 0}
                <p class="panel__empty">Nothing matches “{query}”.</p>
              {:else}
                <div class="services">
                  {#each matches as g (g.name)}
                    {#if !isGithub || matches.length > 1}<p class="group">{g.name}</p>{/if}
                    {#each g.permissions as p (p.name)}
                      {@const st = rowState(account, p)}
                      <label class="svc svc--{st}">
                        <input type="checkbox" disabled={frozen || st !== "open"} checked={st === "held" || picked.includes(p.name)} onchange={() => toggle(p.name)} />
                        <span class="svc__name" class:svc__name--named={p.label}>{permLabel(p)}</span>
                        <span class="svc__desc quiet">{p.description ?? ""}</span>
                        <span class="svc__why quiet">{WHY[st]}</span>
                      </label>
                    {/each}
                  {/each}
                </div>
              {/if}
              <div class="confirm" class:confirm--live={picked.length > 0}>
                <span class="confirm__count">{picked.length} selected</span>
                <input class="field__input" type="text" placeholder="Why (optional), for the reviewer" bind:value={note} disabled={frozen} />
                <button type="button" class="btn btn--primary" disabled={frozen || ask.isPending || picked.length === 0} onclick={confirm}>Confirm request</button>
              </div>
              <p class="panel__note">Read access means configuration and listings, never the data a service holds. Requests go to the room's reviewers (Admin › Review).</p>
            {/if}
          </div>
        {/if}
      </section>
    {/each}

    {#if strayGrants.length > 0 || strayPending.length > 0}
      <section class="vendor" aria-label="Elsewhere">
        <div class="vendor__head vendor__head--plain">
          <span class="vendor__name">Elsewhere</span>
          <span class="vendor__note">on a connection the room no longer lists</span>
        </div>
        {@render heldTable(strayGrants, strayPending, access.data.can_decide)}
      </section>
    {/if}
  </div>
{/if}

<style>
  .vendors { display: flex; flex-direction: column; gap: var(--space-md); }
  .vendors__head { display: flex; flex-direction: column; gap: var(--space-2xs); }
  .vendors__lead { margin: 0; max-width: 60rem; color: var(--ink); font-size: 0.9rem; line-height: 1.5; }
  .vendor { display: flex; flex-direction: column; border: 1px solid var(--border); border-radius: var(--radius); background: var(--panel); overflow: hidden; }
  .vendor--open { border-color: var(--ring); }
  .vendor__head { display: flex; align-items: center; gap: var(--space-sm); width: 100%; padding: var(--space-sm) var(--space-md); border: 0; border-bottom: 1px solid var(--border); background: var(--bg); color: var(--ink); font: inherit; text-align: left; cursor: pointer; }
  .vendor__head:hover:not(:disabled) { background: var(--accent-soft); }
  .vendor__head:disabled { cursor: default; }
  .vendor__head--plain { cursor: default; }
  .vendor__name { font-size: 0.95rem; font-weight: 600; }
  .vendor__note { flex: 1; min-width: 0; color: var(--muted); font-size: 0.82rem; }
  .vendor__action { flex-shrink: 0; color: var(--accent, var(--ink)); font-size: 0.85rem; font-weight: 500; }
  .vendor__none { margin: 0; padding: var(--space-sm) var(--space-md); color: var(--muted); font-size: 0.85rem; }
  .vendor__table { border: 0; }
  .vendor__table th { padding-left: var(--space-md); padding-right: var(--space-md); }
  .vendor__table td { padding-left: var(--space-md); padding-right: var(--space-md); }
  .vendor__table tbody tr:last-child td { border-bottom: none; }
  .vendor__waiting td { color: var(--ink); }
  .vendor__picker { display: flex; flex-direction: column; gap: var(--space-sm); padding: var(--space-md); border-top: 1px solid var(--border); }
  .vendor__copy { margin: 0; color: var(--ink); font-size: 0.9rem; line-height: 1.5; }
  .vendor__saved { margin: 0; color: var(--ok); font-size: 0.85rem; font-weight: 500; }
  .perm { font-family: var(--font-mono); font-size: 0.85rem; font-weight: 600; }
  .perm--named { font-family: inherit; font-size: 0.9rem; }
  .quiet { color: var(--muted); font-size: 0.85rem; }
  .org { font-weight: 400; }
  .tier { display: inline-block; padding: 0.05rem 0.45rem; border: 1px solid var(--border); border-radius: var(--radius-pill); font-size: 0.75rem; }
  .tier--mutate { border-color: var(--warn); }
  .tier--destroy { border-color: var(--danger, var(--warn)); }
  .repos { display: inline-block; margin-left: var(--space-xs); color: var(--muted); font-size: 0.8rem; }
  .repos__summary { cursor: pointer; }
  .repos__list { margin: var(--space-2xs) 0 0; padding-left: 1.1rem; }
  .repos__list code { font-size: 0.8rem; }
  .picker .row-link { width: 100%; text-align: left; background: none; border: 0; border-bottom: 1px solid var(--border); cursor: pointer; font: inherit; color: inherit; }
  .picker .row-link:last-child { border-bottom: 0; }
  .picker .row-link:disabled { cursor: default; }
  .crumbs { display: flex; align-items: center; gap: var(--space-xs); margin: 0; }
  .crumb { background: none; border: 0; padding: 0; font: inherit; color: var(--accent, var(--ink)); cursor: pointer; }
  .crumb--here { color: var(--ink); font-weight: 600; cursor: default; }
  .crumb + .crumb::before { content: "›"; margin-right: var(--space-xs); color: var(--muted); }
  .repo-set { display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--space-xs); }
  .repo-set__list { font-family: var(--font-mono); font-size: 0.8rem; overflow-wrap: anywhere; }
  .search { margin: 0; }
  .search .field__input { width: 100%; }
  .services { max-height: 26rem; overflow: auto; padding: 0 var(--space-xs); border: 1px solid var(--border); border-radius: var(--radius-sm); }
  .services__more { padding: var(--space-xs) 0; }
  .group { margin: var(--space-sm) 0 var(--space-2xs); color: var(--muted); font-size: 0.72rem; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; }
  .svc { display: grid; grid-template-columns: auto 10rem minmax(0, 1fr) auto; gap: var(--space-sm); align-items: baseline; padding: var(--space-2xs) 0; cursor: pointer; }
  .svc--repo { grid-template-columns: auto minmax(0, 1fr) auto; }
  .svc--above, .svc--held, .svc--waiting { cursor: default; }
  .svc--above .svc__name, .svc--above .svc__desc { color: var(--muted); }
  .svc__name { font-family: var(--font-mono); font-size: 0.85rem; font-weight: 600; }
  .svc__name--named { font-family: inherit; font-size: 0.9rem; }
  .svc__desc { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .svc__why { white-space: nowrap; }
  .confirm { position: sticky; bottom: 0; display: flex; align-items: center; gap: var(--space-sm); margin: 0; padding: var(--space-sm); border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--panel, var(--bg)); }
  .confirm--live { border-color: var(--accent, var(--border)); }
  .confirm__count { min-width: 6rem; color: var(--muted); font-size: 0.9rem; }
  .confirm__error, .confirm__spacer { flex: 1; font-size: 0.85rem; }
  .confirm .field__input { flex: 1; }
  .btn--danger { color: var(--danger, inherit); }
  @media (max-width: 48rem) {
    .svc { grid-template-columns: auto minmax(0, 1fr); }
    .svc__desc, .svc__why { grid-column: 2; }
    .vendor__head { flex-wrap: wrap; }
  }
</style>

<script lang="ts">
  // What this agent can reach (the Access tab's second half, issue #365;
  // the Permissions section of the Harness until then). Three settings
  // rows: Held, Waiting for review, and Ask for more. The picker walks the
  // way access is granted: the vendor first (AWS, Google Cloud, Azure,
  // Datadog, GitHub, ...), then for a cloud the account, project or
  // subscription, then a searchable list of permissions. One Confirm sends
  // the request to the room's reviewers (Admin > Review). Approved
  // permissions are grants the agent borrows an hour of access against; the
  // decisions are the reviewers', never this page's.
  //
  // GitHub is the one vendor with a step of its own. An agent reaches
  // GitHub through its own repository set (one set per agent, written here
  // and replaced here), so picking GitHub lists the room installation's
  // repositories first; Save writes the set, and only then does the level
  // (Read, Review pull requests, Open pull requests) get picked. The request
  // targets the set, never the room's connection.
  //
  // Mounted inside AccessPanel, after Who can reach this agent. The rows use
  // the shared settings-row classes (app.css) so the two halves of the tab
  // read as one.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { errorMessage, type AccessAccount, type AccessGrant, type AccessPermission, type AccessRequest, type AccessVendor, type AgentIdentity } from "../lib/api";
  import { agentAccessQuery, githubRepositoriesQuery, putGithubRepositoriesMutation, requestAgentAccessMutation, revokeAgentAccessMutation } from "../lib/queries";
  import { serverDate } from "../lib/time";
  import { toast } from "../lib/toast.svelte";

  let { agentName, identity = null }: { agentName: string; identity?: AgentIdentity | null } = $props();

  const GITHUB = "github";
  /** Asking another agent is a permission too, but it is shown under Other
   * agents, where the pair it makes lives; the tables here leave it out. */
  const ASK_PERMISSION = "a2a_ask";

  const queryClient = useQueryClient();
  // agentName is read once: AgentDetail remounts the tab's panels on a
  // route change (`{#key route.name}`), like every other panel here.
  const access = createQuery(() => (agentAccessQuery(agentName)));
  const ask = createMutation(() => (requestAgentAccessMutation(queryClient, agentName)));
  const revoke = createMutation(() => (revokeAgentAccessMutation(queryClient, agentName)));

  // Where the picker is: nothing (vendors), a vendor (its accounts, or for
  // GitHub its repositories), or an account (its permissions). Explicit
  // state, so ticking a box changes nothing here.
  let vendorId = $state<string | null>(null);
  let accountId = $state<string | null>(null);
  let query = $state("");
  let picked = $state<string[]>([]);
  let note = $state("");

  const grants = $derived((access.data?.grants ?? []).filter((g) => g.permission !== ASK_PERMISSION));
  const pending = $derived(
    (access.data?.pending ?? []).filter((r) => (r.permissions ?? []).some((p) => p !== ASK_PERMISSION)),
  );
  const vendors = $derived(access.data?.vendors ?? []);
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
  /** Every account by id, so a table row can name it and, for a GitHub
   * set, list its repositories. */
  const accounts = $derived.by(() => {
    const map = new Map<string, AccessAccount>();
    for (const v of vendors) {
      for (const a of v.accounts) map.set(a.id, a);
    }
    return map;
  });

  function heldAt(accountIdOrVendor: string): number {
    return grants.filter((g) => g.state !== "expired" && g.integration === accountIdOrVendor).length;
  }
  function heldForVendor(v: AccessVendor): number {
    return v.accounts.reduce((n, a) => n + heldAt(a.id), 0);
  }

  function openVendor(v: AccessVendor): void {
    vendorId = v.id;
    query = "";
    picked = [];
    repoDraft = null;
    repoError = null;
    // A service vendor is one connection: go straight to its permissions.
    // GitHub stops at its repositories first.
    accountId = v.kind === "service" && v.accounts.length === 1 && v.id !== GITHUB ? v.accounts[0].id : null;
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
  function toVendors(): void {
    accountId = null;
    vendorId = null;
    picked = [];
    query = "";
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

  const repoSelected = $derived(repos.data?.selected ?? []);
  const repoChecked = $derived(repoDraft ?? repoSelected);
  const repoChanged = $derived(
    repoChecked.length !== repoSelected.length || repoChecked.some((r) => !repoSelected.includes(r)),
  );
  const repoMatches = $derived.by(() => {
    const q = repoQuery.trim().toLowerCase();
    const all = repos.data?.repositories ?? [];
    return q ? all.filter((r) => r.full_name.toLowerCase().includes(q)) : all;
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
</script>

{#snippet repoList(repositories: string[])}
  <details class="repos">
    <summary class="repos__summary" title={repositories.join("\n")}>{count(repositories.length, "repository", "repositories")}</summary>
    <ul class="repos__list">
      {#each repositories as r (r)}<li><code>{r}</code></li>{/each}
    </ul>
  </details>
{/snippet}

{#if access.isPending}
  <section class="settings-row" aria-labelledby="permissions-heading">
    <div class="panel__header settings-row__head">
      <h4 id="permissions-heading" class="panel__subheading">Permissions</h4>
    </div>
    <div class="settings-row__body"><p class="panel__note">Reading the permissions…</p></div>
  </section>
{:else if access.isError}
  <section class="settings-row" aria-labelledby="permissions-heading">
    <div class="panel__header settings-row__head">
      <h4 id="permissions-heading" class="panel__subheading">Permissions</h4>
    </div>
    <div class="settings-row__body"><p class="panel__note">{errorMessage(access.error, "Access is off for this room.")}</p></div>
  </section>
{:else if access.data}
  <section class="settings-row" aria-labelledby="permissions-held-heading">
    <div class="panel__header settings-row__head">
      <h4 id="permissions-held-heading" class="panel__subheading">Held</h4>
      <p class="panel__lead">
        What this agent may do at each vendor. It holds no standing credential: when a task
        needs one, it borrows an hour of access scoped to exactly these permissions, and the
        vendor's audit trail names the agent.
      </p>
      {#if identity}
        <p class="panel__note">Runs as <code>{identity.gsa_email}</code>.</p>
      {/if}
    </div>
    <div class="settings-row__body">
      {#if grants.length === 0}
        <table class="panel__table">
          <thead><tr><th scope="col">Permission</th><th scope="col">Where</th><th scope="col">Expires</th></tr></thead>
          <tbody><tr><td class="panel__empty" colspan="3">Nothing yet. Ask for more below.</td></tr></tbody>
        </table>
      {:else}
        <table class="panel__table">
          <thead><tr><th scope="col">Permission</th><th scope="col">Where</th><th scope="col">Tier</th><th scope="col">Expires</th><th scope="col">Granted by</th><th scope="col" class="panel__cell--right"><span class="panel__sr">Revoke</span></th></tr></thead>
          <tbody>
            {#each grants as g (g.id)}
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
                  {#if access.data.can_decide}
                    <button type="button" class="btn btn--quiet btn--danger" disabled={revoke.isPending} onclick={() => handleRevoke(g)}>Revoke now</button>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </div>
  </section>

  {#if pending.length > 0}
    <section class="settings-row" aria-labelledby="permissions-waiting-heading">
      <div class="panel__header settings-row__head">
        <h4 id="permissions-waiting-heading" class="panel__subheading">Waiting for review</h4>
        <p class="panel__lead">Asked for and not yet decided. The room's reviewers decide in Admin, Review.</p>
      </div>
      <div class="settings-row__body">
        <table class="panel__table">
          <thead><tr><th scope="col">Permissions</th><th scope="col">Where</th><th scope="col">Asked by</th></tr></thead>
          <tbody>
            {#each pending as r (r.id)}
              {@const set = r.repositories ?? repoSet(r.integration)}
              <tr>
                <td><span class="perm" class:perm--named={r.permissions.some((p) => labels.has(p))}>{requestLabels(r)}</span></td>
                <td>
                  {where(r.integration)}
                  {#if set}{@render repoList(set)}{/if}
                </td>
                <td class="quiet">{r.requested_by}{#if r.note}: “{r.note}”{/if}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>
  {/if}

  <section class="settings-row" aria-labelledby="permissions-ask-heading">
    <div class="panel__header settings-row__head">
      <h4 id="permissions-ask-heading" class="panel__subheading">Ask for more</h4>
      <p class="panel__lead">
        Pick a vendor, then what the agent needs there. One Confirm sends the request to the
        room's reviewers.
      </p>
    </div>
    <div class="settings-row__body">
      {#if vendors.length === 0}
        <p class="panel__empty">No vendor is connected to the room yet.</p>
      {:else if !access.data.can_request}
        <p class="panel__note">The agent's owner or a room operator asks; you can see what it holds.</p>
      {:else if !vendor}
        <!-- Level 1: the vendors -->
        <div class="table-card picker">
          {#each vendors as v (v.id)}
            {@const connected = v.accounts.filter((a) => a.connected).length}
            <button type="button" class="row-link" onclick={() => openVendor(v)} disabled={connected === 0}>
              <span class="row-link__name">{v.label}</span>
              <span class="row-link__note">
                {#if v.kind === "cloud"}
                  {connected} of {v.accounts.length} {v.account_noun ?? "account"}{v.accounts.length === 1 ? "" : "s"} connected
                {:else if v.id === GITHUB && connected}
                  {#if v.accounts[0]?.has_repo_set}
                    {count(v.accounts[0].repositories.length, "repository", "repositories")}
                  {:else}
                    no repositories picked yet
                  {/if}
                {:else if connected}
                  one connection
                {:else}
                  not connected yet
                {/if}
                {#if heldForVendor(v)} · {heldForVendor(v)} held{/if}
              </span>
              <span class="row-link__chevron" aria-hidden="true">›</span>
            </button>
          {/each}
        </div>
        <p class="panel__note">A cloud is connected one {vendors.find((v) => v.kind === "cloud")?.account_noun ?? "account"} at a time. Datadog, Atlassian and the like are one connection each. GitHub is a set of repositories per agent.</p>
      {:else if isGithub && !account}
        <!-- Level 2, GitHub: the agent's repositories -->
        <nav class="crumbs" aria-label="Where you are"><button type="button" class="crumb" onclick={toVendors}>‹ Vendors</button><span class="crumb crumb--here">{vendor.label}</span></nav>
        {#if repos.isPending}
          <p class="panel__note">Reading the repositories…</p>
        {:else if repos.isError}
          <p class="panel__note panel__note--fail">{errorMessage(repos.error, "The repositories could not be read.")}</p>
        {:else if repos.data && !repos.data.available}
          <p class="panel__note">{repos.data.note ?? "The room cannot list repositories right now."}</p>
        {:else if repos.data}
          <p class="panel__note">
            Which repositories this agent may reach. One set per agent; the level you pick next
            applies to every repository in it. Saving a changed set replaces the old one.
          </p>
          <div class="search">
            <input class="field__input" type="search" placeholder={`Find a repository (${repos.data.repositories.length} the room can see)`} bind:value={repoQuery} aria-label="Find a repository" />
          </div>
          {#if repoMatches.length === 0}
            <p class="panel__empty">Nothing matches “{repoQuery}”.</p>
          {:else}
            <div class="services">
              {#each repoMatches as r (r.full_name)}
                <label class="svc svc--repo">
                  <input type="checkbox" checked={repoChecked.includes(r.full_name)} disabled={saveRepos.isPending} onchange={() => toggleRepo(r.full_name)} />
                  <span class="svc__name">{r.full_name}</span>
                  <span class="svc__desc quiet">{r.private ? "private" : "public"}{#if r.default_branch} · {r.default_branch}{/if}</span>
                </label>
              {/each}
            </div>
          {/if}
          <div class="confirm" class:confirm--live={repoChanged}>
            <span class="confirm__count">{repoChecked.length} picked</span>
            {#if repoError}
              <span class="panel__note panel__note--fail confirm__error" role="alert">{repoError}</span>
            {:else if repoChecked.length === 0}
              <span class="panel__note confirm__error">Pick at least one repository.</span>
            {:else}
              <span class="confirm__spacer"></span>
            {/if}
            {#if githubAccount?.has_repo_set && !repoChanged}
              <button type="button" class="btn btn--primary" onclick={chooseLevel}>Choose the level</button>
            {:else}
              <button type="button" class="btn btn--primary" disabled={!repoChanged || repoChecked.length === 0 || saveRepos.isPending} onclick={saveRepositories}>
                {saveRepos.isPending ? "Saving…" : "Save repositories"}
              </button>
            {/if}
          </div>
        {/if}
      {:else if !account}
        <!-- Level 2: a cloud's accounts -->
        <nav class="crumbs" aria-label="Where you are"><button type="button" class="crumb" onclick={back}>‹ Vendors</button><span class="crumb crumb--here">{vendor.label}</span></nav>
        <div class="table-card picker">
          {#each vendor.accounts as a (a.id)}
            <button type="button" class="row-link" onclick={() => openAccount(a)} disabled={!a.connected} title={a.connected ? "" : "Not connected yet; a room admin connects it on Admin › Integrations"}>
              <span class="row-link__name">{a.name}{#if a.organization}<span class="quiet org"> · {vendor.organizations.find((o) => o.id === a.organization)?.name ?? a.organization}</span>{/if}</span>
              <span class="row-link__note">{a.connected ? `${heldAt(a.id)} held` : "not connected yet"}</span>
              <span class="row-link__chevron" aria-hidden="true">›</span>
            </button>
          {/each}
        </div>
      {:else}
        <!-- Level 3: the permissions -->
        <nav class="crumbs" aria-label="Where you are">
          <button type="button" class="crumb" onclick={toVendors}>‹ Vendors</button>
          {#if vendor.kind !== "service"}<button type="button" class="crumb" onclick={back}>{vendor.label}</button>{/if}
          {#if isGithub}
            <button type="button" class="crumb" onclick={back}>{vendor.label}</button>
            <span class="crumb crumb--here">{count(account.repositories.length, "repository", "repositories")}</span>
          {:else}
            <span class="crumb crumb--here">{account.name}</span>
          {/if}
        </nav>
        {#if isGithub}
          <p class="panel__note repo-set">
            <span class="repo-set__list" title={account.repositories.join("\n")}>{account.repositories.join(", ")}</span>
            <button type="button" class="crumb" onclick={back}>Change repositories</button>
          </p>
          <p class="panel__note">
            The levels add up: Review includes Read, Open pull requests includes Review. The
            agent can never approve a pull request it opened.
          </p>
        {/if}
        <div class="search">
          <input class="field__input" type="search" placeholder={`Find a permission (${offeredCount} this connection allows)`} bind:value={query} aria-label="Find a permission" />
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
                  <input type="checkbox" disabled={st !== "open"} checked={st === "held" || picked.includes(p.name)} onchange={() => toggle(p.name)} />
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
          <input class="field__input" type="text" placeholder="Why (optional), for the reviewer" bind:value={note} />
          <button type="button" class="btn btn--primary" disabled={ask.isPending || picked.length === 0} onclick={confirm}>Confirm request</button>
        </div>
        <p class="panel__note">Read access means configuration and listings, never the data a service holds. Requests go to the room's reviewers (Admin › Review).</p>
      {/if}
    </div>
  </section>
{/if}

<style>
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
  .crumbs { display: flex; align-items: center; gap: var(--space-xs); margin: 0 0 var(--space-sm); }
  .crumb { background: none; border: 0; padding: 0; font: inherit; color: var(--accent, var(--ink)); cursor: pointer; }
  .crumb--here { color: var(--ink); font-weight: 600; cursor: default; }
  .crumb + .crumb::before { content: "›"; margin-right: var(--space-xs); color: var(--muted); }
  .repo-set { display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--space-xs); }
  .repo-set__list { font-family: var(--font-mono); font-size: 0.8rem; overflow-wrap: anywhere; }
  .search { margin: 0 0 var(--space-sm); }
  .search .field__input { width: 100%; }
  .services { max-height: 26rem; overflow: auto; padding: 0 var(--space-xs); border: 1px solid var(--border); border-radius: var(--radius-sm); }
  .group { margin: var(--space-sm) 0 var(--space-2xs); color: var(--muted); font-size: 0.72rem; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; }
  .svc { display: grid; grid-template-columns: auto 10rem minmax(0, 1fr) auto; gap: var(--space-sm); align-items: baseline; padding: var(--space-2xs) 0; cursor: pointer; }
  .svc--repo { grid-template-columns: auto minmax(0, 1fr) auto; }
  .svc--above, .svc--held, .svc--waiting { cursor: default; }
  .svc--above .svc__name, .svc--above .svc__desc { color: var(--muted); }
  .svc__name { font-family: var(--font-mono); font-size: 0.85rem; font-weight: 600; }
  .svc__name--named { font-family: inherit; font-size: 0.9rem; }
  .svc__desc { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .svc__why { white-space: nowrap; }
  .confirm { position: sticky; bottom: 0; display: flex; align-items: center; gap: var(--space-sm); margin-top: var(--space-sm); padding: var(--space-sm); border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--panel, var(--bg)); }
  .confirm--live { border-color: var(--accent, var(--border)); }
  .confirm__count { min-width: 6rem; color: var(--muted); font-size: 0.9rem; }
  .confirm__error, .confirm__spacer { flex: 1; font-size: 0.85rem; }
  .confirm .field__input { flex: 1; }
  .btn--danger { color: var(--danger, inherit); }
  @media (max-width: 48rem) {
    .svc { grid-template-columns: auto minmax(0, 1fr); }
    .svc__desc, .svc__why { grid-column: 2; }
  }
</style>

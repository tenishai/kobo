<script lang="ts">
  // What this agent may do at each vendor, and how to ask for more. The
  // picker walks the way access is granted: the vendor first (AWS, Google
  // Cloud, Azure, Datadog, ...), then for a cloud the account, project or
  // subscription, then a searchable list of services with read access. One
  // Confirm sends the request to the room's reviewers (Admin > Review).
  // Approved permissions are grants the agent borrows an hour of access
  // against; the decisions are the reviewers', never this page's.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { errorMessage, type AccessAccount, type AccessGrant, type AccessPermission, type AccessVendor, type AgentIdentity } from "../lib/api";
  import { agentAccessQuery, requestAgentAccessMutation, revokeAgentAccessMutation } from "../lib/queries";
  import { serverDate } from "../lib/time";
  import { toast } from "../lib/toast.svelte";

  let { agentName, identity = null }: { agentName: string; identity?: AgentIdentity | null } = $props();

  const queryClient = useQueryClient();
  const access = createQuery(() => (agentAccessQuery(agentName)));
  const ask = createMutation(() => (requestAgentAccessMutation(queryClient, agentName)));
  const revoke = createMutation(() => (revokeAgentAccessMutation(queryClient, agentName)));

  // Where the picker is: nothing (vendors), a vendor (its accounts), or an
  // account (its services). Explicit state, so ticking a box changes nothing here.
  let vendorId = $state<string | null>(null);
  let accountId = $state<string | null>(null);
  let query = $state("");
  let picked = $state<string[]>([]);
  let note = $state("");

  const grants = $derived(access.data?.grants ?? []);
  const pending = $derived(access.data?.pending ?? []);
  const vendors = $derived(access.data?.vendors ?? []);
  const vendor = $derived(vendors.find((v) => v.id === vendorId) ?? null);
  const account = $derived(vendor?.accounts.find((a) => a.id === accountId) ?? null);

  const held = $derived(new Set(grants.filter((g) => g.state !== "expired").map((g) => `${g.integration}:${g.permission}`)));
  const waiting = $derived(new Set(pending.flatMap((r) => (r.permissions ?? []).map((p) => `${r.integration}:${p}`))));

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
    // A service vendor is one connection: go straight to its permissions.
    accountId = v.kind === "service" && v.accounts.length === 1 ? v.accounts[0].id : null;
  }
  function openAccount(a: AccessAccount): void {
    if (!a.connected) return;
    accountId = a.id;
    query = "";
    picked = [];
  }
  function back(): void {
    if (accountId !== null && vendor?.kind !== "service") {
      accountId = null;
    } else {
      accountId = null;
      vendorId = null;
    }
    query = "";
    picked = [];
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

  // Read access only: the write tiers are asked for by a person, not picked here.
  const matches = $derived.by(() => {
    if (!vendor) return [] as { name: string; permissions: AccessPermission[] }[];
    const q = query.trim().toLowerCase();
    return vendor.groups
      .map((g) => ({
        name: g.name,
        permissions: g.permissions.filter(
          (p) => p.tier === "read" && (!q || p.name.includes(q) || g.name.toLowerCase().includes(q) || (p.description ?? "").toLowerCase().includes(q)),
        ),
      }))
      .filter((g) => g.permissions.length > 0);
  });
  const readCount = $derived(vendor ? vendor.groups.reduce((n, g) => n + g.permissions.filter((p) => p.tier === "read").length, 0) : 0);

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
      onSuccess: () => toast.success("Revoked", `${agentName} no longer holds ${g.permission} on ${g.integration}; open sessions end within the hour.`),
      onError: (err: unknown) => toast.error("Could not revoke", errorMessage(err, "Try again.")),
    });
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
</script>

<section class="panel" aria-labelledby="permissions-heading">
  <h2 id="permissions-heading" class="panel__heading">Permissions</h2>
  <p class="panel__lead">
    What this agent may do at each vendor. It holds no standing credential: when a task needs one,
    it borrows an hour of access scoped to exactly these permissions, and the vendor's audit trail
    names the agent.
  </p>
  {#if identity}
    <p class="panel__note">Runs as <code>{identity.gsa_email}</code>.</p>
  {/if}

  {#if access.isPending}
    <p class="panel__note">Reading the permissions…</p>
  {:else if access.isError}
    <p class="panel__note">{errorMessage(access.error, "Access is off for this room.")}</p>
  {:else if access.data}
    <h3 class="panel__subheading">Held</h3>
    {#if grants.length === 0}
      <p class="panel__empty">Nothing yet. Pick a vendor below to ask.</p>
    {:else}
      <div class="table-card"><table class="panel__table">
        <thead><tr><th>Service</th><th>Where</th><th>Tier</th><th>Expires</th><th>Granted by</th><th class="panel__cell--right"></th></tr></thead>
        <tbody>
          {#each grants as g (g.id)}
            <tr class:quiet={g.state === "expired"}>
              <td><span class="perm" title={g.permission}>{service(g.permission)}</span></td>
              <td>{g.integration}</td>
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
      </table></div>
    {/if}

    {#if pending.length > 0}
      <h3 class="panel__subheading">Waiting for review</h3>
      <ul class="pending">
        {#each pending as r (r.id)}
          <li><span class="perm">{r.permissions.map(service).join(", ")}</span> on {r.integration}, asked by {r.requested_by}{#if r.note}: <span class="quiet">{r.note}</span>{/if}</li>
        {/each}
      </ul>
    {/if}

    <h3 class="panel__subheading">Ask for more</h3>
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
      <p class="panel__note">A cloud is connected one {vendors.find((v) => v.kind === "cloud")?.account_noun ?? "account"} at a time. Datadog, Atlassian and the like are one connection each.</p>
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
      <!-- Level 3: the services, with read access -->
      <nav class="crumbs" aria-label="Where you are">
        <button type="button" class="crumb" onclick={() => { accountId = null; vendorId = null; picked = []; query = ""; }}>‹ Vendors</button>
        {#if vendor.kind !== "service"}<button type="button" class="crumb" onclick={back}>{vendor.label}</button>{/if}
        <span class="crumb crumb--here">{account.name}</span>
      </nav>
      <div class="search">
        <input class="field__input" type="search" placeholder={`Find a service (${readCount} with read access)`} bind:value={query} aria-label="Find a service" />
      </div>
      {#if matches.length === 0}
        <p class="panel__empty">Nothing matches “{query}”.</p>
      {:else}
        <div class="services">
          {#each matches as g (g.name)}
            <p class="group">{g.name}</p>
            {#each g.permissions as p (p.name)}
              {@const st = rowState(account, p)}
              <label class="svc svc--{st}">
                <input type="checkbox" disabled={st !== "open"} checked={st === "held" || picked.includes(p.name)} onchange={() => toggle(p.name)} />
                <span class="svc__name">{service(p.name)}</span>
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
  {/if}
</section>

<style>
  .perm { font-family: var(--font-mono); font-size: 0.85rem; font-weight: 600; }
  .quiet { color: var(--muted); font-size: 0.85rem; }
  .org { font-weight: 400; }
  .tier { display: inline-block; padding: 0.05rem 0.45rem; border: 1px solid var(--border); border-radius: var(--radius-pill); font-size: 0.75rem; }
  .tier--mutate { border-color: var(--warn); }
  .tier--destroy { border-color: var(--danger, var(--warn)); }
  .pending { margin: 0 0 var(--space-md); padding-left: 1.2rem; }
  .picker .row-link { width: 100%; text-align: left; background: none; border: 0; border-bottom: 1px solid var(--border); cursor: pointer; font: inherit; color: inherit; }
  .picker .row-link:last-child { border-bottom: 0; }
  .picker .row-link:disabled { cursor: default; }
  .crumbs { display: flex; align-items: center; gap: var(--space-xs); margin: 0 0 var(--space-sm); }
  .crumb { background: none; border: 0; padding: 0; font: inherit; color: var(--accent, var(--ink)); cursor: pointer; }
  .crumb--here { color: var(--ink); font-weight: 600; cursor: default; }
  .crumb + .crumb::before { content: "›"; margin-right: var(--space-xs); color: var(--muted); }
  .search { margin: 0 0 var(--space-sm); }
  .search .field__input { width: 100%; }
  .services { max-height: 26rem; overflow: auto; padding: 0 var(--space-xs); border: 1px solid var(--border); border-radius: var(--radius-sm); }
  .group { margin: var(--space-sm) 0 var(--space-2xs); color: var(--muted); font-size: 0.72rem; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; }
  .svc { display: grid; grid-template-columns: auto 10rem minmax(0, 1fr) auto; gap: var(--space-sm); align-items: baseline; padding: var(--space-2xs) 0; cursor: pointer; }
  .svc--above, .svc--held, .svc--waiting { cursor: default; }
  .svc--above .svc__name, .svc--above .svc__desc { color: var(--muted); }
  .svc__name { font-family: var(--font-mono); font-size: 0.85rem; font-weight: 600; }
  .svc__desc { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .svc__why { white-space: nowrap; }
  .confirm { position: sticky; bottom: 0; display: flex; align-items: center; gap: var(--space-sm); margin-top: var(--space-sm); padding: var(--space-sm); border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--panel, var(--bg)); }
  .confirm--live { border-color: var(--accent, var(--border)); }
  .confirm__count { min-width: 6rem; color: var(--muted); font-size: 0.9rem; }
  .confirm .field__input { flex: 1; }
  .btn--danger { color: var(--danger, inherit); }
  @media (max-width: 48rem) {
    .svc { grid-template-columns: auto minmax(0, 1fr); }
    .svc__desc, .svc__why { grid-column: 2; }
  }
</style>

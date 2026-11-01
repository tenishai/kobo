<script lang="ts">
  // Agent connections (issue #371): which agents reach which vendors, with
  // what, right now. One card per agent the room knows, newest activity first;
  // inside it one row per vendor with its mark, then what the agent holds
  // there (permission, tier, expires, granted by) and what is borrowed at this
  // moment (permissions, task, issued, expires, via). Revoke on a held row is
  // the same call the agent's own Access tab makes; the permission service
  // ends the open sessions with the grant. Nothing else is decided here: owners
  // ask on the Access tab, reviewers decide in Review, and this page shows
  // the result for the whole room.
  import { Graph } from "phosphor-svelte";
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import StatusBadge from "../StatusBadge.svelte";
  import VendorMark from "../VendorMark.svelte";
  import { errorMessage, type AgentConnectionsEntry, type AgentVendorHoldings } from "../../lib/api";
  import { agentConnectionsQuery, revokeAgentConnectionMutation } from "../../lib/queries";
  import { formatHash } from "../../lib/router.svelte";
  import { serverDate, when } from "../../lib/time";
  import { toast } from "../../lib/toast.svelte";

  let { item = null }: { item?: string | null } = $props();

  const queryClient = useQueryClient();
  // A borrowed credential runs out within the hour, so the list is read often while open.
  const connections = createQuery(() => ({ ...agentConnectionsQuery(), refetchInterval: 30_000 }));
  const revoke = createMutation(() => (revokeAgentConnectionMutation(queryClient)));

  let query = $state("");

  const agents = $derived(connections.data?.agents ?? []);
  const anythingHeld = $derived(agents.some((a) => a.vendors.length > 0));

  /** The cards to draw: the deep-linked agent first (`#/admin/connections/<agent>`),
   * then the server's order, narrowed by the search box over agent, vendor and
   * repository names. */
  const shown = $derived.by(() => {
    const q = query.trim().toLowerCase();
    const rows = q ? agents.filter((a) => matches(a, q)) : agents;
    if (!item) return rows;
    const first = rows.filter((a) => a.name === item);
    return first.length ? [...first, ...rows.filter((a) => a.name !== item)] : rows;
  });

  function matches(a: AgentConnectionsEntry, q: string): boolean {
    if (a.name.toLowerCase().includes(q)) return true;
    return a.vendors.some(
      (v) =>
        v.vendor.toLowerCase().includes(q) ||
        v.label.toLowerCase().includes(q) ||
        v.connections.some((c) => c.name.toLowerCase().includes(q) || c.repositories.some((r) => r.toLowerCase().includes(q))),
    );
  }

  function heldAt(v: AgentVendorHoldings): AgentVendorHoldings["grants"] {
    return v.grants.filter((g) => g.state !== "expired");
  }
  function expires(expiresAt: string | null): string {
    if (!expiresAt) return "standing";
    const ms = serverDate(expiresAt).getTime() - Date.now();
    if (ms <= 0) return "expired";
    const days = Math.ceil(ms / 86_400_000);
    return days <= 1 ? "today" : `in ${days} days`;
  }
  function left(iso: string): string {
    const min = Math.max(0, Math.round((serverDate(iso).getTime() - Date.now()) / 60_000));
    return min >= 60 ? "about an hour" : `${min} min`;
  }
  /** A permission as a person reads it: `github_repos_read` as "repos read". */
  function words(name: string): string {
    return name.replace(/^[a-z]+_/, "").replace(/_/g, " ");
  }
  function count(n: number, one: string, many: string): string {
    return `${n} ${n === 1 ? one : many}`;
  }

  function handleRevoke(agent: string, vendor: AgentVendorHoldings, grant: AgentVendorHoldings["grants"][number]): void {
    revoke.mutate(
      { agent, grantId: grant.id },
      {
        onSuccess: (r) => {
          const ended = (r as { leases_ended?: number }).leases_ended;
          toast.success(
            "Revoked",
            `${agent} no longer holds ${words(grant.permission)} on ${vendor.label}` +
              (typeof ended === "number" ? `; ${count(ended, "open session", "open sessions")} ended.` : "; open sessions end within the hour."),
          );
        },
        onError: (err: unknown) => toast.error("Could not revoke", errorMessage(err, "Try again.")),
      },
    );
  }

  /** The portrait: the agent's picture when it has one, else its initial.
   * The picture route answers 404 before a picture exists, so the image
   * hides itself and the initial stands. */
  function hideBroken(event: Event): void {
    const img = event.currentTarget as HTMLImageElement;
    img.hidden = true;
    img.nextElementSibling?.removeAttribute("hidden");
  }
</script>

<section class="panel page" aria-labelledby="agent-connections-heading">
  <div class="page__header">
    <h2 id="agent-connections-heading" class="page__title">Agent connections</h2>
    <p class="page__lead">
      Which agents reach which vendors, with what, right now. Owners ask on an agent's Access tab and
      reviewers decide in Review; this page is the result for the whole room.
    </p>
  </div>

  {#if connections.isPending}
    <p class="panel__note">Reading what agents hold…</p>
  {:else if connections.isError}
    <p class="panel__note">{errorMessage(connections.error, "Access is off for this room.")}</p>
  {:else if agents.length === 0}
    <p class="panel__empty">No agents yet.</p>
  {:else}
    {#if !anythingHeld}
      <div class="ac__empty">
        <Graph size={28} aria-hidden="true" />
        <p class="ac__empty-title">No agent holds anything yet.</p>
        <p class="ac__empty-text">Owners ask on an agent's Access tab; approvals land here.</p>
      </div>
    {/if}

    <div class="ac__bar">
      <input
        class="field__input ac__search"
        type="search"
        placeholder="Find an agent, vendor or repository"
        aria-label="Find an agent, vendor or repository"
        bind:value={query}
      />
      <span class="ac__count">{count(shown.length, "agent", "agents")}</span>
    </div>

    {#if shown.length === 0}
      <p class="panel__empty">Nothing matches “{query}”.</p>
    {/if}

    <div class="ac__cards">
      {#each shown as a (a.name)}
        <article class="ac__card" class:ac__card--linked={a.name === item} aria-label={a.name}>
          <header class="ac__head">
            <span class="ac__identity">
              <img class="ac__avatar" src={`/api/agents/${encodeURIComponent(a.name)}/avatar`} alt="" onerror={hideBroken} />
              <span class="ac__avatar ac__avatar--initial" aria-hidden="true" hidden>{a.name.slice(0, 1).toUpperCase()}</span>
              <a class="ac__name" href={formatHash({ page: "agent", name: a.name, tab: "access" })}>{a.name}</a>
              {#if a.team}<span class="ac__team">{a.team}</span>{/if}
            </span>
            <StatusBadge status={a.status} />
          </header>

          {#if a.vendors.length === 0}
            <p class="ac__none">Holds nothing at any vendor.</p>
          {:else}
            {#each a.vendors as v (v.vendor)}
              {@const held = heldAt(v)}
              <section class="ac__vendor" aria-label={`${a.name} at ${v.label}`}>
                <div class="ac__vendor-head">
                  <VendorMark id={v.vendor} name={v.label} size={16} />
                  <span class="ac__vendor-name">{v.label}</span>
                  {#each v.connections as c (c.id)}
                    <span class="ac__connection" title={c.repositories.join("\n")}>
                      {c.name}{#if c.repositories.length > 0}<span class="ac__quiet"> · {count(c.repositories.length, "repository", "repositories")}</span>{/if}
                    </span>
                  {/each}
                </div>

                {#if held.length > 0}
                  <table class="panel__table ac__table">
                    <thead><tr><th scope="col">Holds</th><th scope="col">Tier</th><th scope="col">Expires</th><th scope="col">Granted by</th><th scope="col" class="panel__cell--right"><span class="panel__sr">Revoke</span></th></tr></thead>
                    <tbody>
                      {#each held as g (g.id)}
                        <tr>
                          <td><span class="ac__perm" title={g.permission}>{words(g.permission)}</span></td>
                          <td><span class="tier tier--{g.tier}">{g.tier}</span></td>
                          <td>{expires(g.expires_at)}</td>
                          <td class="ac__quiet">{g.granted_by}</td>
                          <td class="panel__cell--right">
                            <button type="button" class="btn btn--quiet btn--danger" disabled={revoke.isPending} onclick={() => handleRevoke(a.name, v, g)}>Revoke</button>
                          </td>
                        </tr>
                      {/each}
                    </tbody>
                  </table>
                {/if}

                {#if v.leases.length > 0}
                  <table class="panel__table ac__table">
                    <thead><tr><th scope="col">Borrowed now</th><th scope="col">Task</th><th scope="col">Issued</th><th scope="col">Ends in</th><th scope="col">Via</th></tr></thead>
                    <tbody>
                      {#each v.leases as l (l.id)}
                        <tr>
                          <td><span class="ac__perm" title={l.permissions.join(", ")}>{l.permissions.map(words).join(", ")}</span></td>
                          <td>{l.task ?? ""}</td>
                          <td class="ac__quiet nowrap">{when(l.issued_at)}</td>
                          <td class="nowrap">{left(l.expires_at)}</td>
                          <td class="ac__quiet">{l.via ?? ""}</td>
                        </tr>
                      {/each}
                    </tbody>
                  </table>
                {/if}

                {#if held.length === 0 && v.leases.length === 0}
                  <p class="ac__none">A connection and nothing held on it yet.</p>
                {/if}
              </section>
            {/each}
          {/if}
        </article>
      {/each}
    </div>
  {/if}
</section>

<style>
  .ac__empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-2xs);
    padding: var(--space-xl) var(--space-lg);
    color: var(--muted);
    text-align: center;
  }

  .ac__empty-title {
    margin: var(--space-xs) 0 0;
    color: var(--ink);
    font-size: 0.95rem;
    font-weight: 600;
  }

  .ac__empty-text {
    max-width: 26rem;
    margin: 0;
    color: var(--ink);
    font-size: 0.85rem;
  }

  .ac__bar {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
    margin-top: var(--space-sm);
  }

  .ac__search {
    flex: 1;
    min-width: 0;
  }

  .ac__count {
    flex-shrink: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .ac__cards {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    margin-top: var(--space-md);
  }

  .ac__card {
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
    overflow: hidden;
  }

  .ac__card--linked {
    border-color: var(--ring);
  }

  .ac__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
    padding: var(--space-sm) var(--space-md);
    border-bottom: 1px solid var(--border);
    background: var(--bg);
  }

  .ac__identity {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
    min-width: 0;
  }

  .ac__avatar {
    width: 1.75rem;
    height: 1.75rem;
    flex-shrink: 0;
    border-radius: 50%;
    object-fit: cover;
  }

  .ac__avatar--initial {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: var(--accent-soft);
    color: var(--ink);
    font-family: var(--font-display-mono);
    font-size: 0.8rem;
    font-weight: 600;
  }

  .ac__avatar--initial[hidden] {
    display: none;
  }

  .ac__name {
    color: var(--ink);
    font-size: 0.95rem;
    font-weight: 600;
    text-decoration: none;
  }

  .ac__name:hover {
    text-decoration: underline;
  }

  .ac__team {
    color: var(--muted);
    font-size: 0.82rem;
  }

  .ac__none {
    margin: 0;
    padding: var(--space-sm) var(--space-md);
    color: var(--ink);
    font-size: 0.85rem;
  }

  .ac__vendor + .ac__vendor {
    border-top: 1px solid var(--border);
  }

  .ac__vendor-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-sm);
    padding: var(--space-sm) var(--space-md);
  }

  .ac__vendor-name {
    color: var(--ink);
    font-size: 0.9rem;
    font-weight: 600;
  }

  .ac__connection {
    color: var(--ink);
    font-size: 0.85rem;
  }

  .ac__table {
    border: 0;
  }

  .ac__table th,
  .ac__table td {
    padding-left: var(--space-md);
    padding-right: var(--space-md);
  }

  .ac__table tbody tr:last-child td {
    border-bottom: none;
  }

  .ac__perm {
    color: var(--ink);
    font-size: 0.9rem;
  }

  .ac__quiet {
    color: var(--muted);
    font-size: 0.85rem;
  }

  .nowrap {
    white-space: nowrap;
  }

  .tier {
    display: inline-block;
    padding: 0.05rem 0.45rem;
    border: 1px solid var(--border);
    border-radius: var(--radius-pill);
    font-size: 0.75rem;
  }

  .tier--mutate {
    border-color: var(--warn);
  }

  .tier--destroy {
    border-color: var(--danger, var(--warn));
  }
</style>

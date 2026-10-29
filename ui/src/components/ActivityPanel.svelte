<script lang="ts">
  // What the agent did through the tool gateway: every call a lane admitted
  // and every refusal, newest first, read live from the gateway. One row per
  // call; the tool, how it went, how long it took, the arguments in brief.
  import { createQuery } from "@tanstack/svelte-query";
  import { activityQuery, agentLeasesQuery } from "../lib/queries";

  let { agentName }: { agentName: string } = $props();

  let days = $state(7);
  let handler = $state("all");
  const activity = createQuery(() => (activityQuery(agentName, days)));
  // Credentials the agent borrowed from the room's permission service, when
  // the room has one: one row per lease, with the task the agent named.
  const leases = createQuery(() => (agentLeasesQuery(agentName, days)));

  function whenIso(iso: string | null): string {
    if (!iso) return "";
    return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  const handlers = $derived(Array.from(new Set((activity.data?.rows ?? []).map((r) => r.handler).filter(Boolean))).sort());
  const rows = $derived((activity.data?.rows ?? []).filter((r) => handler === "all" || r.handler === handler));
  const denied = $derived(rows.filter((r) => r.outcome === "denied").length);

  function when(ts: number | null): string {
    if (!ts) return "";
    return new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  const TONE: Record<string, "on" | "warn" | "fail"> = { ok: "on", error: "warn", denied: "fail" };
</script>

<section class="panel" aria-labelledby="activity-heading">
  <h2 id="activity-heading" class="panel__heading">Activity</h2>
  <p class="panel__lead">
    Every call this agent made through the tool gateway, and every one it was refused. Cloud
    calls also appear in the account's own audit trail under the agent's name.
  </p>

  {#if activity.isPending}
    <p class="panel__note">Reading the gateway…</p>
  {:else if activity.isError}
    <p class="panel__note">Could not read the gateway just now.</p>
  {:else if !activity.data?.available}
    <p class="panel__note">{activity.data?.note}</p>
  {:else}
    <div class="toolbar">
      <label class="pick"><span>Last</span>
        <select bind:value={days}><option value={1}>day</option><option value={7}>7 days</option><option value={30}>30 days</option></select>
      </label>
      <label class="pick"><span>Tool</span>
        <select bind:value={handler}><option value="all">all</option>{#each handlers as h (h)}<option value={h}>{h}</option>{/each}</select>
      </label>
      <span class="count">{rows.length} call{rows.length === 1 ? "" : "s"}{#if denied}, {denied} refused{/if}</span>
    </div>
    {#if rows.length === 0}
      <p class="panel__empty">Nothing in this window.</p>
    {:else}
      <div class="table-card"><table class="panel__table">
        <thead><tr><th>When</th><th>Tool</th><th>Outcome</th><th class="num">ms</th><th>Arguments</th></tr></thead>
        <tbody>
          {#each rows as r, i (i)}
            <tr>
              <td class="quiet nowrap">{when(r.ts)}</td>
              <td><span class="tool">{r.tool || r.handler}</span></td>
              <td>
                <span class="status status--{TONE[r.outcome] ?? "warn"}" title={r.detail}><span class="status__dot" aria-hidden="true"></span>{r.outcome}</span>
                {#if r.detail}<span class="quiet detail">{r.detail}</span>{/if}
              </td>
              <td class="num quiet">{r.ms ?? ""}</td>
              <td><code class="args">{r.args}</code></td>
            </tr>
          {/each}
        </tbody>
      </table></div>
    {/if}
  {/if}

  {#if leases.data?.available}
    <h3 class="panel__subheading">Borrowed credentials</h3>
    <p class="panel__note">
      Each row is one hour of access the agent borrowed for a task, scoped to the permissions it
      holds. The vendor's own audit trail names the agent in the session.
    </p>
    {#if (leases.data.leases ?? []).length === 0}
      <p class="panel__empty">No credentials borrowed in this window.</p>
    {:else}
      <div class="table-card"><table class="panel__table">
        <thead><tr><th>Issued</th><th>Account</th><th>Permissions</th><th>Task</th><th>Until</th></tr></thead>
        <tbody>
          {#each leases.data.leases as l (l.id)}
            <tr>
              <td class="quiet nowrap">{whenIso(l.issued_at)}</td>
              <td><span class="tool">{l.integration}</span></td>
              <td class="quiet">{l.permissions.join(", ")}</td>
              <td class="quiet">{l.task ?? ""}</td>
              <td class="quiet nowrap">{whenIso(l.expires_at)}</td>
            </tr>
          {/each}
        </tbody>
      </table></div>
    {/if}
  {/if}
</section>

<style>
  .toolbar { display: flex; align-items: center; gap: var(--space-md); margin: 0 0 var(--space-sm); }
  .pick { display: inline-flex; align-items: center; gap: var(--space-xs); color: var(--muted); font-size: 0.9rem; }
  .pick select { font: inherit; }
  .count { margin-left: auto; color: var(--muted); font-size: 0.9rem; }
  .tool { font-family: var(--font-mono); font-size: 0.85rem; font-weight: 600; }
  .args { display: block; max-width: 28rem; overflow: hidden; color: var(--muted); font-size: 0.78rem; text-overflow: ellipsis; white-space: nowrap; }
  .detail { display: block; font-size: 0.78rem; }
  .quiet { color: var(--muted); font-size: 0.85rem; }
  .nowrap { white-space: nowrap; }
  .num { text-align: right; }
</style>

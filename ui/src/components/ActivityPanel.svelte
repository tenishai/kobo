<script lang="ts">
  // What the agent did through the tool gateway: every call a lane admitted
  // and every refusal, newest first, read live from the gateway. A summary
  // first, then the rows grouped by day (the newest day open, older ones
  // folded), an errors-only switch, and 25 rows at a time (#300).
  import { createQuery } from "@tanstack/svelte-query";
  import type { AgentActivityRow } from "../lib/api";
  import { activityQuery, agentLeasesQuery } from "../lib/queries";

  let { agentName }: { agentName: string } = $props();

  let days = $state(7);
  let handler = $state("all");
  let problemsOnly = $state(false);
  const activity = createQuery(() => (activityQuery(agentName, days)));
  // Credentials the agent borrowed from the room's permission service, when
  // the room has one: one row per lease, with the task the agent named.
  const leases = createQuery(() => (agentLeasesQuery(agentName, days)));

  function whenIso(iso: string | null): string {
    if (!iso) return "";
    return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  const all = $derived(activity.data?.rows ?? []);
  const handlers = $derived(Array.from(new Set(all.map((r) => r.handler).filter(Boolean))).sort());
  const rows = $derived(
    all.filter((r) => (handler === "all" || r.handler === handler) && (!problemsOnly || r.outcome !== "ok")),
  );
  const denied = $derived(rows.filter((r) => r.outcome === "denied").length);
  const errors = $derived(rows.filter((r) => r.outcome === "error").length);
  const toolsUsed = $derived(new Set(rows.map((r) => r.tool || r.handler)).size);

  const PAGE = 25;
  type Day = { key: string; label: string; rows: AgentActivityRow[]; denied: number; errors: number };
  function dayKey(ts: number | null): string {
    if (!ts) return "unknown";
    const d = new Date(ts * 1000);
    return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
  }
  function dayLabel(ts: number | null): string {
    if (!ts) return "No time";
    const d = new Date(ts * 1000);
    const today = new Date();
    const yesterday = new Date(today.getTime() - 86_400_000);
    const same = (a: Date, b: Date) => a.toDateString() === b.toDateString();
    if (same(d, today)) return "Today";
    if (same(d, yesterday)) return "Yesterday";
    return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  }
  const daysGrouped = $derived.by((): Day[] => {
    const out: Day[] = [];
    for (const r of rows) {
      const key = dayKey(r.ts);
      let day = out[out.length - 1];
      if (!day || day.key !== key) {
        day = { key, label: dayLabel(r.ts), rows: [], denied: 0, errors: 0 };
        out.push(day);
      }
      day.rows.push(r);
      if (r.outcome === "denied") day.denied += 1;
      if (r.outcome === "error") day.errors += 1;
    }
    return out;
  });
  // Open days and how many rows each shows. The newest day starts open; a
  // day with a refusal or an error starts open too, since that is what a
  // reader came for.
  let open = $state<Record<string, boolean>>({});
  let shown = $state<Record<string, number>>({});
  function isOpen(day: Day, index: number): boolean {
    return open[day.key] ?? (index === 0 || day.denied + day.errors > 0);
  }
  function visible(day: Day): AgentActivityRow[] {
    return day.rows.slice(0, shown[day.key] ?? PAGE);
  }

  function when(ts: number | null): string {
    if (!ts) return "";
    return new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
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
      <label class="pick pick--switch">
        <input type="checkbox" bind:checked={problemsOnly} />
        <span>Problems only</span>
      </label>
    </div>

    <dl class="summary">
      <div><dt>Calls</dt><dd>{rows.length}</dd></div>
      <div><dt>Refused</dt><dd class:bad={denied > 0}>{denied}</dd></div>
      <div><dt>Errors</dt><dd class:warn={errors > 0}>{errors}</dd></div>
      <div><dt>Tools used</dt><dd>{toolsUsed}</dd></div>
    </dl>

    {#if rows.length === 0}
      <p class="panel__empty">{problemsOnly ? "No refusals or errors in this window." : "Nothing in this window."}</p>
    {:else}
      {#each daysGrouped as day, index (day.key)}
        <section class="day">
          <button
            type="button"
            class="day__head"
            aria-expanded={isOpen(day, index)}
            onclick={() => (open = { ...open, [day.key]: !isOpen(day, index) })}
          >
            <span class="day__chev" aria-hidden="true">{isOpen(day, index) ? "▾" : "▸"}</span>
            <span class="day__label">{day.label}</span>
            <span class="day__counts">
              {day.rows.length} call{day.rows.length === 1 ? "" : "s"}
              {#if day.denied}<span class="bad">, {day.denied} refused</span>{/if}
              {#if day.errors}<span class="warn">, {day.errors} error{day.errors === 1 ? "" : "s"}</span>{/if}
            </span>
          </button>
          {#if isOpen(day, index)}
            <div class="table-card"><table class="panel__table">
              <thead><tr><th>When</th><th>Tool</th><th>Outcome</th><th class="num">ms</th><th>Arguments</th></tr></thead>
              <tbody>
                {#each visible(day) as r, i (i)}
                  <tr class:row--problem={r.outcome !== "ok"}>
                    <td class="quiet nowrap">{when(r.ts)}</td>
                    <td><span class="tool">{r.tool || r.handler}</span></td>
                    <td>
                      <span class="status status--{TONE[r.outcome] ?? "warn"}" title={r.detail}><span class="status__dot" aria-hidden="true"></span>{r.outcome}</span>
                      {#if r.detail}<span class="quiet detail">{r.detail}</span>{/if}
                    </td>
                    <td class="num quiet">{r.ms ?? ""}</td>
                    <td><code class="args" title={r.args}>{r.args}</code></td>
                  </tr>
                {/each}
              </tbody>
            </table></div>
            {#if day.rows.length > visible(day).length}
              <button
                type="button"
                class="btn btn--quiet day__more"
                onclick={() => (shown = { ...shown, [day.key]: (shown[day.key] ?? PAGE) + PAGE })}
              >
                Show {Math.min(PAGE, day.rows.length - visible(day).length)} more of {day.rows.length}
              </button>
            {/if}
          {/if}
        </section>
      {/each}
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
  .toolbar { display: flex; align-items: center; gap: var(--space-md); margin: 0 0 var(--space-sm); flex-wrap: wrap; }
  .pick { display: inline-flex; align-items: center; gap: var(--space-xs); color: var(--muted); font-size: 0.9rem; }
  .pick select { font: inherit; }
  .pick--switch { margin-left: auto; cursor: pointer; }
  .summary {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: var(--space-sm);
    margin: 0 0 var(--space-md);
  }
  .summary div { padding: var(--space-sm) var(--space-md); border: 1px solid var(--border); border-radius: var(--radius-sm); }
  .summary dt { margin: 0; color: var(--muted); font-size: 0.8rem; }
  .summary dd { margin: 0; color: var(--ink); font-size: 1.25rem; font-weight: 600; }
  .bad { color: var(--fail); }
  .warn { color: var(--warn, var(--ink)); }
  .day { margin: 0 0 var(--space-sm); }
  .day__head {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
    width: 100%;
    padding: var(--space-xs) 0;
    border: 0;
    background: none;
    color: var(--ink);
    font: inherit;
    text-align: left;
    cursor: pointer;
  }
  .day__chev { width: 1rem; color: var(--muted); }
  .day__label { font-weight: 600; }
  .day__counts { margin-left: auto; color: var(--muted); font-size: 0.85rem; }
  .day__more { margin-top: var(--space-xs); }
  .row--problem td { background: color-mix(in srgb, var(--fail) 6%, transparent); }
  .tool { font-family: var(--font-mono); font-size: 0.85rem; font-weight: 600; }
  .args { display: block; max-width: 28rem; overflow: hidden; color: var(--muted); font-size: 0.78rem; text-overflow: ellipsis; white-space: nowrap; }
  .detail { display: block; font-size: 0.78rem; }
  .quiet { color: var(--muted); font-size: 0.85rem; }
  .nowrap { white-space: nowrap; }
  .num { text-align: right; }
</style>

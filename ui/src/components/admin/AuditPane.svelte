<script lang="ts">
  import { DownloadSimple, MagnifyingGlass } from "phosphor-svelte";
  // The room's audit log (issue #316): everything the room saw a person,
  // the room or an agent do, newest first, the way an organization's audit
  // log reads. A search box over actor, action, target and sentence;
  // selects for actor, action family, agent and outcome, and a date range;
  // each row is the actor, the action in mono, the sentence, then a quiet
  // line with where it came from, the ip, how long ago and whether it
  // landed. Pages load on a cursor; Export hands the current filter back as
  // CSV or JSON. The selects offer only what the current window holds, so a
  // filter never names something that is not there. A link may open this
  // pane pre-filtered (`#/admin/audit?agent=ward`, from an agent's page).
  //
  // Two tabs (kobo#371). People is the room's own log, as above. Agents is
  // what agents did with their connections, read live from the permission
  // service in the same row shape: each time one borrowed access, was
  // refused it or could not get it. Actor and Action do not apply there, so
  // those selects hide; the agent and outcome facets come from that answer;
  // Export hands that window back. `?tab=agents` in the link opens it.
  import { createQuery } from "@tanstack/svelte-query";
  import { auditAgentsExportUrl, auditExportUrl, type AuditAgentsFilters, type AuditFilters, type AuditRow } from "../../lib/api";
  import { adminAuditAgentsQuery, adminAuditQuery } from "../../lib/queries";
  import { hashQuery } from "../../lib/router.svelte";
  import { relativeTime, when } from "../../lib/time";

  const initial = typeof location === "undefined" ? {} : hashQuery(location.hash);

  type Tab = "people" | "agents";
  let tab = $state<Tab>(initial.tab === "agents" ? "agents" : "people");
  let q = $state(initial.q ?? "");
  let actor = $state(initial.actor ?? "");
  let action = $state(initial.action ?? "");
  let agent = $state(initial.agent ?? "");
  let outcome = $state(initial.outcome ?? "");
  let since = $state(initial.since ?? "");
  let until = $state(initial.until ?? "");
  // The search box fills `q` after a pause, so typing does not fire a
  // request per keystroke.
  let typed = $state(initial.q ?? "");
  let timer: ReturnType<typeof setTimeout> | undefined;
  function onType(event: Event): void {
    typed = (event.currentTarget as HTMLInputElement).value;
    clearTimeout(timer);
    timer = setTimeout(() => {
      q = typed.trim();
    }, 250);
  }

  const filters: AuditFilters = $derived({ q, actor, action, agent, outcome, since, until });
  const agentFilters: AuditAgentsFilters = $derived({ q, agent, outcome, since, until });
  const filterKey = $derived(JSON.stringify(filters));

  // Pages already loaded for the current filter; a filter change starts over.
  let cursor = $state<string | null>(null);
  let older = $state<AuditRow[]>([]);
  let loadedFor = $state("");
  const page = createQuery(() => ({ ...adminAuditQuery(filters, cursor), enabled: tab === "people" }));
  const agentsPage = createQuery(() => ({ ...adminAuditAgentsQuery(agentFilters), enabled: tab === "agents" }));

  function show(next: Tab): void {
    if (next === tab) {
      return;
    }
    tab = next;
    // Actor and Action belong to People only; a value left behind would
    // narrow nothing on Agents and surprise on the way back.
    actor = "";
    action = "";
  }

  $effect(() => {
    if (filterKey !== loadedFor) {
      loadedFor = filterKey;
      cursor = null;
      older = [];
    }
  });

  const current = $derived(tab === "agents" ? agentsPage : page);
  const rows = $derived(tab === "agents" ? (agentsPage.data?.rows ?? []) : [...older, ...(page.data?.rows ?? [])]);
  const total = $derived(current.data?.total_in_window ?? 0);
  const agentFacet = $derived(tab === "agents" ? (agentsPage.data?.agents ?? []) : (page.data?.agents ?? []));
  const outcomeFacet = $derived(tab === "agents" ? (agentsPage.data?.outcomes ?? []) : ["ok", "failed", "refused"]);
  const hasFilter = $derived(Boolean(q || actor || action || agent || outcome || since || until));

  function loadMore(): void {
    if (!page.data?.next_cursor) {
      return;
    }
    older = [...older, ...page.data.rows];
    cursor = page.data.next_cursor;
  }

  function clear(): void {
    typed = "";
    q = "";
    actor = "";
    action = "";
    agent = "";
    outcome = "";
    since = "";
    until = "";
  }

  const LANDED: Record<string, string> = { pending: "Pending", applied: "Applied", failed: "Failed" };
  const OUTCOME: Record<string, string> = { ok: "", failed: "Failed", refused: "Refused" };
</script>

<section class="panel page" aria-labelledby="audit-heading">
  <div class="audit__head">
    <div class="page__header">
      <h2 id="audit-heading" class="page__title">Audit</h2>
      <p class="page__lead">What people, the room and agents did here, newest first. Kept for the room's retention window.</p>
    </div>
    <div class="audit__export" role="group" aria-label="Export">
      {#if tab === "agents"}
        <a class="btn" href={auditAgentsExportUrl(agentFilters, "csv")} download><DownloadSimple size={14} aria-hidden="true" />CSV</a>
        <a class="btn" href={auditAgentsExportUrl(agentFilters, "json")} download><DownloadSimple size={14} aria-hidden="true" />JSON</a>
      {:else}
        <a class="btn" href={auditExportUrl(filters, "csv")} download><DownloadSimple size={14} aria-hidden="true" />CSV</a>
        <a class="btn" href={auditExportUrl(filters, "json")} download><DownloadSimple size={14} aria-hidden="true" />JSON</a>
      {/if}
    </div>
  </div>

  <div class="audit__tabs" role="tablist" aria-label="Show">
    <button type="button" role="tab" class="audit__tab" aria-selected={tab === "people"} onclick={() => show("people")}>People</button>
    <button type="button" role="tab" class="audit__tab" aria-selected={tab === "agents"} onclick={() => show("agents")}>Agents</button>
  </div>
  <p class="audit__tab-lead">
    {#if tab === "agents"}What agents did with their connections: each time one borrowed access, was refused it or could not get it.{:else}What people and the room did here.{/if}
  </p>

  <!-- The filters, together in one panel: search across the top, then one
       labelled control per field in a grid. -->
  <div class="audit__filters">
    <div class="audit__search">
      <MagnifyingGlass class="audit__search-icon" size={15} aria-hidden="true" />
      <input
        type="search"
        placeholder={tab === "agents" ? "Search agent, connection or sentence" : "Search actor, action, target or sentence"}
        aria-label="Search the audit log"
        value={typed}
        oninput={onType}
      />
    </div>
    <div class="audit__grid" class:audit__grid--agents={tab === "agents"}>
      {#if tab === "people"}
        <label class="audit__field"><span>Actor</span>
          <select bind:value={actor}>
            <option value="">Any</option>
            {#if actor && !(page.data?.actors ?? []).includes(actor)}<option value={actor}>{actor}</option>{/if}
            {#each page.data?.actors ?? [] as a (a)}<option value={a}>{a}</option>{/each}
          </select>
        </label>
        <label class="audit__field"><span>Action</span>
          <select bind:value={action}>
            <option value="">Any</option>
            {#if action && !(page.data?.families ?? []).includes(action)}<option value={action}>{action}</option>{/if}
            {#each page.data?.families ?? [] as f (f)}<option value={f}>{f}</option>{/each}
          </select>
        </label>
      {/if}
      <label class="audit__field"><span>Agent</span>
        <select bind:value={agent}>
          <option value="">Any</option>
          {#if agent && !agentFacet.includes(agent)}<option value={agent}>{agent}</option>{/if}
          {#each agentFacet as a (a)}<option value={a}>{a}</option>{/each}
        </select>
      </label>
      <label class="audit__field"><span>Outcome</span>
        <select bind:value={outcome}>
          <option value="">Any</option>
          {#if outcome && !outcomeFacet.includes(outcome)}<option value={outcome}>{outcome}</option>{/if}
          {#each outcomeFacet as o (o)}<option value={o}>{o}</option>{/each}
        </select>
      </label>
      <label class="audit__field"><span>From</span><input type="date" bind:value={since} aria-label="From date" /></label>
      <label class="audit__field"><span>To</span><input type="date" bind:value={until} aria-label="To date" /></label>
    </div>
  </div>

  <div class="audit__meta">
    <span class="count">
      {#if current.isPending && rows.length === 0}Reading the log…{:else}{total} {total === 1 ? "row" : "rows"}{hasFilter ? " match" : ""}{/if}
    </span>
    {#if hasFilter}<button type="button" class="btn btn--quiet" onclick={clear}>Clear filters</button>{/if}
  </div>

  {#if current.isError}
    <p class="panel__note">{tab === "agents" ? "Could not read what agents did just now." : "Could not read the log just now."}</p>
  {:else if !current.isPending && rows.length === 0}
    <p class="panel__empty">{hasFilter ? "Nothing matches that filter." : tab === "agents" ? "No agent has used a connection yet." : "Nothing has happened here yet."}</p>
  {:else if rows.length > 0}
    <div class="table-card"><table class="panel__table log">
      <thead><tr><th>Who</th><th>Action</th><th>What happened</th></tr></thead>
      <tbody>
        {#each rows as e (e.id)}
          <tr>
            <td class="who" title={e.actor_kind}>{e.actor}</td>
            <td><code class="action">{e.action}</code></td>
            <td>
              <span class="sentence">{e.summary}{#if OUTCOME[e.outcome]} <span class="outcome outcome--{e.outcome}">{OUTCOME[e.outcome]}</span>{/if}</span>
              <span class="meta">
                <span>{e.source}</span>
                {#if e.request_ip}<span>· {e.request_ip}</span>{/if}
                <span title={when(e.at)}>· {relativeTime(e.at)}</span>
                {#if e.landed}<span class="landed landed--{e.landed}" title={e.landed_detail ?? ""}>· {LANDED[e.landed] ?? e.landed}</span>{/if}
              </span>
            </td>
          </tr>
        {/each}
      </tbody>
    </table></div>
    {#if tab === "people" && page.data?.next_cursor}
      <p class="more"><button type="button" class="btn btn--quiet" onclick={loadMore} disabled={page.isFetching}>Load more</button></p>
    {/if}
  {/if}
</section>

<style>
  .audit__head {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-md);
  }

  .audit__head > :global(.page__header) {
    flex: 1 1 24rem;
    min-width: 0;
  }

  .audit__export {
    display: inline-flex;
    flex-shrink: 0;
    gap: var(--space-xs);
  }

  .audit__export :global(.btn) {
    gap: 0.35rem;
  }

  .audit__tabs {
    display: inline-flex;
    align-self: flex-start;
    gap: 2px;
    padding: 3px;
    border-radius: calc(var(--radius-sm) + 3px);
    background: var(--accent-soft);
  }

  .audit__tab {
    display: inline-flex;
    align-items: center;
    height: 2rem;
    padding: 0 var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: none;
    color: var(--muted);
    font: inherit;
    font-size: 0.85rem;
    font-weight: 500;
    cursor: pointer;
  }

  .audit__tab[aria-selected="true"] {
    background: var(--panel);
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.06), 0 0 0 1px var(--border);
    color: var(--ink);
  }

  .audit__tab-lead {
    margin: calc(-1 * var(--space-xs)) 0 0;
    color: var(--ink);
    font-size: 0.9rem;
  }

  .audit__filters {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
  }

  .audit__search {
    position: relative;
    display: flex;
    align-items: center;
  }

  .audit__search input {
    width: 100%;
    padding-left: 2.1rem;
  }

  .audit__search :global(.audit__search-icon) {
    position: absolute;
    left: 0.75rem;
    color: var(--muted);
    pointer-events: none;
  }

  /* Six filters: one row of six when there is room, two rows of three
     when there is not, never five and a stray. */
  .audit__grid {
    display: grid;
    grid-template-columns: repeat(6, minmax(0, 1fr));
    gap: var(--space-sm) var(--space-md);
  }

  /* Agents has four: one row of four, two rows of two when narrow. */
  .audit__grid--agents {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }

  @media (max-width: 90rem) {
    .audit__grid {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }

    .audit__grid--agents {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }

  .audit__field {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 0.3rem;
  }

  .audit__field > span {
    color: var(--muted);
    font-size: 0.75rem;
    font-weight: 500;
  }

  .audit__field select,
  .audit__field input {
    width: 100%;
  }

  .audit__meta {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
    min-height: 2rem;
  }

  .count {
    color: var(--muted);
    font-size: 0.85rem;
  }

  .quiet {
    color: var(--muted);
    font-size: 0.85rem;
  }

  .who {
    color: var(--ink);
    white-space: nowrap;
    max-width: 16rem;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .action {
    font-size: 0.8rem;
    white-space: nowrap;
  }

  .sentence {
    display: block;
    color: var(--ink);
  }

  .meta {
    display: block;
    color: var(--muted);
    font-size: 0.78rem;
  }

  .meta > span + span {
    margin-left: 0.25rem;
  }

  .outcome {
    font-size: 0.78rem;
    margin-left: 0.35rem;
  }

  .outcome--failed,
  .landed--failed {
    color: var(--fail);
  }

  .outcome--refused,
  .landed--pending {
    color: var(--warn);
  }

  .landed--applied {
    color: var(--ok);
  }

  .more {
    margin: var(--space-sm) 0 0;
    text-align: center;
  }
</style>

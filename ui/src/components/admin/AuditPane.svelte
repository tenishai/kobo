<script lang="ts">
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
  import { createQuery } from "@tanstack/svelte-query";
  import { auditExportUrl, type AuditFilters, type AuditRow } from "../../lib/api";
  import { adminAuditQuery } from "../../lib/queries";
  import { hashQuery } from "../../lib/router.svelte";
  import { relativeTime, when } from "../../lib/time";

  const initial = typeof location === "undefined" ? {} : hashQuery(location.hash);

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
  const filterKey = $derived(JSON.stringify(filters));

  // Pages already loaded for the current filter; a filter change starts over.
  let cursor = $state<string | null>(null);
  let older = $state<AuditRow[]>([]);
  let loadedFor = $state("");
  const page = createQuery(() => (adminAuditQuery(filters, cursor)));

  $effect(() => {
    if (filterKey !== loadedFor) {
      loadedFor = filterKey;
      cursor = null;
      older = [];
    }
  });

  const rows = $derived([...older, ...(page.data?.rows ?? [])]);
  const total = $derived(page.data?.total_in_window ?? 0);
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
  <div class="page__header">
    <h2 id="audit-heading" class="page__title">Audit</h2>
    <p class="page__lead">What people, the room and agents did here, newest first. Kept for the room's retention window.</p>
  </div>

  <div class="toolbar">
    <input
      class="search"
      type="search"
      placeholder="Search actor, action, target or sentence"
      aria-label="Search the audit log"
      value={typed}
      oninput={onType}
    />
    <label class="pick"><span>Actor</span>
      <select bind:value={actor}>
        <option value="">any</option>
        {#if actor && !(page.data?.actors ?? []).includes(actor)}<option value={actor}>{actor}</option>{/if}
        {#each page.data?.actors ?? [] as a (a)}<option value={a}>{a}</option>{/each}
      </select>
    </label>
    <label class="pick"><span>Action</span>
      <select bind:value={action}>
        <option value="">any</option>
        {#if action && !(page.data?.families ?? []).includes(action)}<option value={action}>{action}</option>{/if}
        {#each page.data?.families ?? [] as f (f)}<option value={f}>{f}</option>{/each}
      </select>
    </label>
    <label class="pick"><span>Agent</span>
      <select bind:value={agent}>
        <option value="">any</option>
        {#if agent && !(page.data?.agents ?? []).includes(agent)}<option value={agent}>{agent}</option>{/if}
        {#each page.data?.agents ?? [] as a (a)}<option value={a}>{a}</option>{/each}
      </select>
    </label>
    <label class="pick"><span>Outcome</span>
      <select bind:value={outcome}>
        <option value="">any</option>
        <option value="ok">ok</option>
        <option value="failed">failed</option>
        <option value="refused">refused</option>
      </select>
    </label>
    <label class="pick"><span>From</span><input type="date" bind:value={since} aria-label="From date" /></label>
    <label class="pick"><span>To</span><input type="date" bind:value={until} aria-label="To date" /></label>
    {#if hasFilter}<button type="button" class="btn btn--quiet" onclick={clear}>Clear</button>{/if}
  </div>

  <div class="toolbar toolbar--below">
    <span class="count">
      {#if page.isPending && rows.length === 0}Reading the log…{:else}{total} {total === 1 ? "row" : "rows"}{hasFilter ? " match" : ""}{/if}
    </span>
    <span class="export">
      <span class="quiet">Export</span>
      <a class="btn btn--quiet" href={auditExportUrl(filters, "csv")} download>CSV</a>
      <a class="btn btn--quiet" href={auditExportUrl(filters, "json")} download>JSON</a>
    </span>
  </div>

  {#if page.isError}
    <p class="panel__note">Could not read the log just now.</p>
  {:else if !page.isPending && rows.length === 0}
    <p class="panel__empty">{hasFilter ? "Nothing matches that filter." : "Nothing has happened here yet."}</p>
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
    {#if page.data?.next_cursor}
      <p class="more"><button type="button" class="btn btn--quiet" onclick={loadMore} disabled={page.isFetching}>Load more</button></p>
    {/if}
  {/if}
</section>

<style>
  .toolbar {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
    margin: 0 0 var(--space-sm);
    flex-wrap: wrap;
  }

  .toolbar--below {
    margin-bottom: var(--space-sm);
  }

  .search {
    flex: 1 1 16rem;
    min-width: 12rem;
    font: inherit;
    padding: 0.35rem 0.6rem;
    border: 1px solid var(--line);
    border-radius: var(--radius-sm, 6px);
    background: var(--surface);
    color: var(--ink);
  }

  .pick {
    display: inline-flex;
    align-items: center;
    gap: var(--space-xs);
    color: var(--muted);
    font-size: 0.9rem;
  }

  .pick select,
  .pick input {
    font: inherit;
    color: var(--ink);
  }

  .count {
    color: var(--muted);
    font-size: 0.85rem;
  }

  .export {
    margin-left: auto;
    display: inline-flex;
    align-items: center;
    gap: var(--space-xs);
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

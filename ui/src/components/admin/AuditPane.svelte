<script lang="ts">
  // Who changed what from the panel, and whether it has landed: the admin
  // store's audit table read back, newest first. Each row is one plain
  // sentence (built server-side) with the technical event name under it,
  // and a row is pending until the outside system (a grant, a secret, a
  // pull request) reports back. The kind filter narrows to one event name.
  import { createQuery } from "@tanstack/svelte-query";
  import { adminAuditQuery } from "../../lib/queries";
  import { serverDate, when } from "../../lib/time";

  const audit = createQuery(() => (adminAuditQuery()));

  let kind = $state("all");
  const all = $derived(
    [...(audit.data ?? [])].sort((a, b) => serverDate(b.at).getTime() - serverDate(a.at).getTime() || b.id - a.id),
  );
  const kinds = $derived(Array.from(new Set(all.map((e) => e.action))).sort());
  const rows = $derived(all.filter((e) => kind === "all" || e.action === kind));

  const LANDED: Record<string, string> = {
    pending: "Pending",
    applied: "Applied",
    failed: "Failed",
  };
</script>

<section class="panel page" aria-labelledby="audit-heading">
  <div class="page__header">
    <h2 id="audit-heading" class="page__title">Audit</h2>
    <p class="page__lead">Who changed what from this panel, and whether it has landed.</p>
  </div>
  {#if audit.isPending}
    <p class="panel__note">Reading the log…</p>
  {:else if audit.isError}
    <p class="panel__note">Could not read the log just now.</p>
  {:else if all.length === 0}
    <p class="panel__empty">Nothing has been changed from the panel yet.</p>
  {:else}
    <div class="toolbar">
      <label class="pick"><span>Kind</span>
        <select bind:value={kind}>
          <option value="all">all</option>
          {#each kinds as k (k)}<option value={k}>{k}</option>{/each}
        </select>
      </label>
      <span class="count">{rows.length} of {all.length}</span>
    </div>
    <div class="table-card"><table class="panel__table">
      <thead><tr><th>When</th><th>Who</th><th>Change</th><th>Landed</th></tr></thead>
      <tbody>
        {#each rows as e (e.id)}
          <tr>
            <td class="when">{when(e.at)}</td>
            <td>{e.actor}</td>
            <td>
              <span class="change">{e.summary}</span>
              <span class="raw">{e.action} · {e.object_type} <span class="panel__name">{e.object_id}</span></span>
            </td>
            <td class="landed landed--{e.landed}" title={e.landed_detail ?? ""}>
              {LANDED[e.landed] ?? e.landed}
              {#if e.landed_detail}<span class="raw">{e.landed_detail}</span>{/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table></div>
  {/if}
</section>

<style>
  .toolbar {
    display: flex;
    align-items: center;
    gap: var(--space-md);
    margin: 0 0 var(--space-sm);
    flex-wrap: wrap;
  }

  .pick {
    display: inline-flex;
    align-items: center;
    gap: var(--space-xs);
    color: var(--muted);
    font-size: 0.9rem;
  }

  .pick select {
    font: inherit;
  }

  .count {
    margin-left: auto;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .when {
    color: var(--muted);
    white-space: nowrap;
  }

  .change {
    display: block;
    color: var(--ink);
  }

  .raw {
    display: block;
    color: var(--muted);
    font-size: 0.78rem;
  }

  .landed {
    white-space: nowrap;
  }

  .landed .raw {
    max-width: 24rem;
    white-space: normal;
  }

  .landed--applied {
    color: var(--ok);
  }

  .landed--pending {
    color: var(--warn);
  }

  .landed--failed {
    color: var(--fail);
  }
</style>

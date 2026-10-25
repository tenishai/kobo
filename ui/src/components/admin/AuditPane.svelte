<script lang="ts">
  // Who changed what from the panel, and whether it has landed: the admin
  // store's audit table read back, newest first. A row is pending until the
  // outside system (a grant, a secret, a pull request) reports back.
  import { createQuery } from "@tanstack/svelte-query";
  import { adminAuditQuery } from "../../lib/queries";

  const audit = createQuery(() => (adminAuditQuery()));

  function when(iso: string): string {
    const d = new Date(iso);
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

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
  {:else if (audit.data ?? []).length === 0}
    <p class="panel__empty">Nothing has been changed from the panel yet.</p>
  {:else}
    <div class="table-card"><table class="panel__table">
      <thead><tr><th>When</th><th>Who</th><th>Change</th><th>Landed</th></tr></thead>
      <tbody>
        {#each audit.data ?? [] as e (e.id)}
          <tr>
            <td class="when">{when(e.at)}</td>
            <td>{e.actor}</td>
            <td>{e.action} · {e.object_type} <span class="panel__name">{e.object_id}</span></td>
            <td class="landed landed--{e.landed}" title={e.landed_detail ?? ""}>{LANDED[e.landed] ?? e.landed}</td>
          </tr>
        {/each}
      </tbody>
    </table></div>
  {/if}
</section>

<style>
  .when {
    color: var(--muted);
    white-space: nowrap;
  }

  .landed {
    white-space: nowrap;
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

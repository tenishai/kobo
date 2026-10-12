<script lang="ts">
  // What needs a person today, then the room's numbers.
  import { formatHash } from "../../lib/router.svelte";
  import { admin, attention } from "../../lib/admin/store.svelte";

  const items = $derived(attention());
  const connected = $derived(admin.plugins.filter((p) => p.state === "connected").length);
  const granted = $derived(
    admin.connections.filter((c) => c.lanes.some((l) => l.state === "granted")).length,
  );
  const noLane = $derived(admin.connections.filter((c) => c.lanes.length === 0).length);
</script>

<section class="panel" aria-labelledby="overview-heading">
  <div class="panel__header">
    <h2 id="overview-heading" class="panel__heading">Overview</h2>
    <p class="panel__lead">What needs a person today, then the room's numbers.</p>
  </div>

  <div class="tiles">
    <a class="tile" href={formatHash({ page: "admin", pane: "plugins" })}>
      <span class="tile__label">Plugins</span>
      <span class="tile__value">{connected}<span class="tile__of">/{admin.plugins.length}</span></span>
      <span class="tile__note">connected</span>
    </a>
    <a class="tile" href={formatHash({ page: "admin", pane: "connections" })}>
      <span class="tile__label">Agents with a lane</span>
      <span class="tile__value">{granted}<span class="tile__of">/{admin.connections.length}</span></span>
      <span class="tile__note">{noLane} with none</span>
    </a>
    <a class="tile" href={formatHash({ page: "admin", pane: "roles" })}>
      <span class="tile__label">Roles</span>
      <span class="tile__value">{admin.roles.length}</span>
      <span class="tile__note">{admin.roles.filter((r) => !r.builtin).length} custom</span>
    </a>
    <a class="tile" href={formatHash({ page: "admin", pane: "people" })}>
      <span class="tile__label">Pending</span>
      <span class="tile__value">{items.length}</span>
      <span class="tile__note">requests and open grants</span>
    </a>
  </div>

  <h3 class="panel__subheading">Needs attention</h3>
  {#if items.length === 0}
    <p class="panel__empty">Nothing waits on you.</p>
  {:else}
    <ul class="attention">
      {#each items as it (it.text)}
        <li class="attention__row">
          <span class="attention__dot attention__dot--{it.tone}" aria-hidden="true"></span>
          <span class="attention__text">{it.text}</span>
          <a class="btn btn--quiet" href={formatHash({ page: "admin", pane: it.pane, item: it.item })}>Open</a>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .tiles {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr));
    gap: var(--space-sm);
  }

  .tile {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
    padding: var(--space-sm) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    color: var(--ink);
    text-decoration: none;
  }

  .tile:hover {
    border-color: var(--accent);
  }

  .tile__label {
    color: var(--muted);
    font-size: 0.75rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .tile__value {
    font-size: 1.5rem;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }

  .tile__of {
    color: var(--muted);
    font-size: 0.9rem;
    font-weight: 400;
  }

  .tile__note {
    color: var(--muted);
    font-size: 0.8rem;
  }

  .attention {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .attention__row {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
    padding: var(--space-2xs) var(--space-xs);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }

  .attention__dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .attention__dot--warn {
    background: var(--warn);
  }

  .attention__dot--fail {
    background: var(--fail);
  }

  .attention__text {
    flex: 1;
    min-width: 0;
    font-size: 0.9rem;
  }
</style>

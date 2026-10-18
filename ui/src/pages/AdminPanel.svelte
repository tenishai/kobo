<script lang="ts">
  // The admin panel (admin-panel demo, 2026-10-12): a settings window
  // inside the room. A list of panes on the left, one pane on the right,
  // both driven by the hash (`#/admin/<pane>/<item>`), so a pane and the
  // thing open inside it are a link someone can send.
  import AuditPane from "../components/admin/AuditPane.svelte";
  import ConnectionsPane from "../components/admin/ConnectionsPane.svelte";
  import OverviewPane from "../components/admin/OverviewPane.svelte";
  import PeoplePane from "../components/admin/PeoplePane.svelte";
  import PluginsPane from "../components/admin/PluginsPane.svelte";
  import RolesPane from "../components/admin/RolesPane.svelte";
  import { createQuery } from "@tanstack/svelte-query";
  import { formatHash, route } from "../lib/router.svelte";
  import { whoamiQuery } from "../lib/queries";

  // The panel is for kobo admins, and only while the room has it on. The
  // server decides both (`/api/whoami`); this is the same answer, shown.
  const whoami = createQuery(() => (whoamiQuery()));
  const allowed = $derived(Boolean(whoami.data?.operator && whoami.data?.admin_panel));

  const PANES: { id: string; label: string; group: "admin" | "room" }[] = [
    { id: "overview", label: "Overview", group: "admin" },
    { id: "addons", label: "Add-ons", group: "admin" },
    { id: "integrations", label: "Integrations", group: "admin" },
    { id: "connections", label: "Connections", group: "admin" },
    { id: "roles", label: "Roles", group: "admin" },
    { id: "people", label: "People", group: "admin" },
    { id: "audit", label: "Audit", group: "admin" },
  ];

  const pane = $derived(PANES.some((p) => p.id === route.tab) ? (route.tab as string) : "overview");
  const item = $derived(route.section);
</script>

{#if whoami.isPending}
  <p class="admin__gate">Checking who you are…</p>
{:else if !allowed}
  <section class="admin__gate" aria-labelledby="admin-gate-heading">
    <h2 id="admin-gate-heading">Admins only</h2>
    <p>
      This page is the room's admin panel. It opens for the room's admins when the panel is turned
      on in the org config. If you expected to be in, ask a room admin.
    </p>
  </section>
{:else}
<div class="admin">
  <nav class="admin__nav" aria-label="Admin panel">
    <p class="admin__group">Admin</p>
    <ul class="admin__list">
      {#each PANES as p (p.id)}
        <li>
          <a
            class="admin__link"
            href={formatHash({ page: "admin", pane: p.id })}
            aria-current={pane === p.id ? "page" : undefined}
          >
            {p.label}
          </a>
        </li>
      {/each}
    </ul>
    <p class="admin__group">This room</p>
    <ul class="admin__list admin__list--quiet">
      <li><span class="admin__fact">Org config <code>example/kobo-config</code></span></li>
      <li><span class="admin__fact">Runtime <code>daedalus-runtime@789b43…</code></span></li>
      <li><span class="admin__fact">Deploys to <code>agents-dev</code></span></li>
    </ul>
  </nav>

  <section class="admin__content" aria-live="polite">
    {#if pane === "overview"}
      <OverviewPane />
    {:else if pane === "addons"}
      <PluginsPane kind="platform" item={route.section} />
    {:else if pane === "integrations"}
      <PluginsPane kind="vendor" {item} />
    {:else if pane === "connections"}
      <ConnectionsPane {item} />
    {:else if pane === "roles"}
      <RolesPane {item} />
    {:else if pane === "people"}
      <PeoplePane />
    {:else}
      <AuditPane />
    {/if}
  </section>
</div>
{/if}

<style>
  .admin__gate {
    max-width: 36rem;
    margin: var(--space-xl) auto;
    padding: 0 var(--space-md);
    color: var(--muted);
  }

  .admin {
    display: grid;
    grid-template-columns: 12rem minmax(0, 1fr);
    gap: var(--space-xl);
    align-items: start;
  }

  .admin__nav {
    position: sticky;
    top: var(--space-md);
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
  }

  .admin__group {
    margin: var(--space-sm) 0 0;
    color: var(--muted);
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .admin__list {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .admin__link {
    display: block;
    padding: var(--space-xs) var(--space-sm);
    border-radius: var(--radius-sm);
    color: var(--ink);
    text-decoration: none;
  }

  .admin__link:hover {
    background: var(--panel);
  }

  .admin__link[aria-current="page"] {
    background: var(--accent-soft);
    font-weight: 600;
  }

  .admin__fact {
    display: block;
    padding: var(--space-2xs) var(--space-sm);
    color: var(--muted);
    font-size: 0.8rem;
  }

  .admin__fact code {
    display: block;
    overflow: hidden;
    font-size: 0.75rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .admin__content {
    display: flex;
    flex-direction: column;
    gap: var(--space-lg);
    min-width: 0;
  }

  @media (max-width: 48rem) {
    .admin {
      grid-template-columns: 1fr;
    }

    .admin__nav {
      position: static;
    }

    .admin__list:not(.admin__list--quiet) {
      flex-direction: row;
      flex-wrap: wrap;
    }

    .admin__list--quiet {
      display: none;
    }
  }
</style>

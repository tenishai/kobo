<script lang="ts">
  // The admin panel: a list of pages on the left, one page on the right,
  // both driven by the hash (`#/admin/<pane>/<item>`), so a page and the
  // thing open inside it are a link someone can send.
  import AuditPane from "../components/admin/AuditPane.svelte";
  import ConnectionsPane from "../components/admin/ConnectionsPane.svelte";
  import IntegrationsPane from "../components/admin/IntegrationsPane.svelte";
  import OverviewPane from "../components/admin/OverviewPane.svelte";
  import PeoplePane from "../components/admin/PeoplePane.svelte";
  import ServicesPane from "../components/admin/ServicesPane.svelte";
  import RolesPane from "../components/admin/RolesPane.svelte";
  import ReviewPane from "../components/admin/ReviewPane.svelte";
  import { createQuery } from "@tanstack/svelte-query";
  import { formatHash, route } from "../lib/router.svelte";
  import { adminAddonsQuery, adminRoomQuery, whoamiQuery } from "../lib/queries";

  // The panel is for kobo admins, and only while the room has it on. The
  // server decides both (`/api/whoami`); this is the same answer, shown.
  const whoami = createQuery(() => (whoamiQuery()));
  const allowed = $derived(Boolean(whoami.data?.operator && whoami.data?.admin_panel));
  const room = createQuery(() => ({ ...adminRoomQuery(), enabled: allowed }));

  const PANES: { id: string; label: string }[] = [
    { id: "overview", label: "Overview" },
    { id: "services", label: "Services" },
    { id: "integrations", label: "Integrations" },
    { id: "connections", label: "Connections" },
    { id: "review", label: "Review" },
    { id: "roles", label: "Roles" },
    { id: "people", label: "People" },
    { id: "audit", label: "Audit" },
  ];

  // With the permission service on, agents hold permissions and borrow credentials; the
  // gateway's lanes are the older model, so the Connections page steps aside for Review.
  const addons = createQuery(() => ({ ...adminAddonsQuery(), enabled: allowed }));
  const accessOn = $derived((addons.data ?? []).some((a) => a.id === "access" && a.state === "on"));
  // Until the add-ons have answered, Connections is neither shown nor hidden for sure;
  // leaving it out until then stops it flashing on a hard refresh of a room with Access on.
  const accessKnown = $derived(addons.data !== undefined || addons.isError);
  const panes = $derived(PANES.filter((p) => p.id !== "connections" || (accessKnown && !accessOn)));

  // "addons" and "platform" were earlier names of the Services page; old links still open it.
  const pane = $derived(
    route.tab === "addons" || route.tab === "platform" ? "services" : PANES.some((p) => p.id === route.tab) ? (route.tab as string) : "overview",
  );
  const item = $derived(route.section);

  function short(ref: string | null | undefined): string {
    if (!ref) return "not set";
    const at = ref.indexOf("@sha256:");
    const tail = ref.replace(/^.*\//, "");
    return at === -1 ? tail : `${tail.split("@")[0]}@${ref.slice(at + 8, at + 15)}`;
  }
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
      {#each panes as p (p.id)}
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
    {#if room.data}
      <p class="admin__group">This room</p>
      <ul class="admin__list admin__list--quiet">
        <li><span class="admin__fact">Org config <code title={room.data.org_repo ?? ""}>{room.data.org_repo ?? "not set"}</code></span></li>
        <li><span class="admin__fact">Runtime <code title={room.data.runtime_image}>{short(room.data.runtime_image)}</code></span></li>
        <li><span class="admin__fact">Deploys to <code title={room.data.cluster}>{room.data.cluster || "not set"}</code></span></li>
      </ul>
    {/if}
  </nav>

  <section class="admin__content" aria-live="polite">
    {#if pane === "overview"}
      <OverviewPane />
    {:else if pane === "services"}
      <ServicesPane />
    {:else if pane === "integrations"}
      <IntegrationsPane {item} {accessOn} />
    {:else if pane === "connections"}
      <ConnectionsPane {item} />
    {:else if pane === "review"}
      <ReviewPane />
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

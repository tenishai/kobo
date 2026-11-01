<script lang="ts">
  // The admin panel: a list of pages on the left, one page on the right,
  // both driven by the hash (`#/admin/<pane>/<item>`), so a page and the
  // thing open inside it are a link someone can send.
  import AgentConnectionsPane from "../components/admin/AgentConnectionsPane.svelte";
  import AuditPane from "../components/admin/AuditPane.svelte";
  import ConnectionsPane from "../components/admin/ConnectionsPane.svelte";
  import IntegrationsPane from "../components/admin/IntegrationsPane.svelte";
  import OverviewPane from "../components/admin/OverviewPane.svelte";
  import PeoplePane from "../components/admin/PeoplePane.svelte";
  import ServicesPane from "../components/admin/ServicesPane.svelte";
  import RolesPane from "../components/admin/RolesPane.svelte";
  import ReviewPane from "../components/admin/ReviewPane.svelte";
  import { createQuery } from "@tanstack/svelte-query";
  import {
    ClockCounterClockwise,
    Graph,
    Plugs,
    SealCheck,
    SquaresFour,
    Stack,
    UserGear,
    Users,
  } from "phosphor-svelte";

  const PANE_ICONS: Record<string, typeof Stack> = {
    overview: SquaresFour,
    services: Stack,
    integrations: Plugs,
    connections: Graph,
    review: SealCheck,
    roles: UserGear,
    people: Users,
    audit: ClockCounterClockwise,
  };
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

  // The `connections` page is two pages under one id (issue #371). With the permission
  // service on, agents hold permissions and borrow credentials, and the page is Agent
  // connections: what every agent holds at every vendor right now. With it off, the
  // gateway's lanes are the model, and the page is the older Connections.
  const addons = createQuery(() => ({ ...adminAddonsQuery(), enabled: allowed }));
  const accessOn = $derived((addons.data ?? []).some((a) => a.id === "access" && a.state === "on"));
  // Until the add-ons have answered, which of the two it is cannot be known; leaving it
  // out until then stops the label flipping on a hard refresh.
  const accessKnown = $derived(addons.data !== undefined || addons.isError);
  const panes = $derived(
    PANES.filter((p) => p.id !== "connections" || accessKnown).map((p) =>
      p.id === "connections" && accessOn ? { ...p, label: "Agent connections" } : p,
    ),
  );

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
  <header class="admin__head">
    <h1 class="admin__title">Admin</h1>
    <p class="admin__lead">Run the room: what it runs on, the vendors it lends, who may do what, and the record of every change.</p>
  </header>

  <div class="admin__layout">
  <nav class="admin__nav" aria-label="Admin panel">
    <ul class="admin__list">
      {#each panes as p (p.id)}
        {@const Icon = PANE_ICONS[p.id]}
        <li>
          <a
            class="admin__link"
            href={formatHash({ page: "admin", pane: p.id })}
            aria-current={pane === p.id ? "page" : undefined}
          >
            {#if Icon}<Icon class="admin__icon" size={17} aria-hidden="true" />{/if}
            {p.label}
          </a>
        </li>
      {/each}
    </ul>
    {#if room.data}
      <div class="admin__room">
        <p class="admin__group">This room</p>
        <dl class="admin__facts">
          <div>
            <dt>Org config</dt>
            <dd><code title={room.data.org_repo ?? ""}>{room.data.org_repo ?? "not set"}</code></dd>
          </div>
          <div>
            <dt>Runtime</dt>
            <dd><code title={room.data.runtime_image}>{short(room.data.runtime_image)}</code></dd>
          </div>
          <div>
            <dt>Deploys to</dt>
            {#if room.data.cluster_detail}
              <dd>
                <code title={`${room.data.cluster_detail.gcp_project} · ${room.data.cluster_detail.location} · registered as ${room.data.cluster}`}
                  >{room.data.cluster_detail.gke_cluster}</code
                >
              </dd>
            {:else}
              <dd><code title={room.data.cluster}>{room.data.cluster || "not set"}</code></dd>
            {/if}
          </div>
        </dl>
      </div>
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
      {#if accessOn}
        <AgentConnectionsPane {item} />
      {:else}
        <ConnectionsPane {item} />
      {/if}
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
</div>
{/if}

<style>
  .admin__gate {
    max-width: 36rem;
    margin: var(--space-xl) auto;
    padding: 0 var(--space-md);
    color: var(--muted);
  }

  /* The page: a header, then the navigation card beside the open page. */
  /* At least as tall as the frame, so the open page's card reaches the
     bottom of the window even when it has little in it. */
  .admin {
    display: flex;
    min-height: calc(100vh - 2 * var(--space-sm) - 2 * var(--space-xl));
    flex-direction: column;
    gap: var(--space-xl);
    width: 100%;
    max-width: 76rem;
    margin: 0 auto;
  }

  .admin__head {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .admin__title {
    margin: 0;
    color: var(--ink);
    font-size: 1.6rem;
    font-weight: 600;
    letter-spacing: -0.02em;
  }

  .admin__lead {
    margin: 0;
    max-width: 44rem;
    color: var(--ink);
    font-size: 0.92rem;
  }

  .admin__layout {
    display: grid;
    flex: 1;
    grid-template-columns: 14rem minmax(0, 1fr);
    gap: var(--space-xl);
    align-items: stretch;
  }

  /* As tall as the page card beside it, with the room's facts at the
     bottom. */
  .admin__nav {
    align-self: stretch;
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .admin__group {
    margin: 0 0 var(--space-xs);
    color: var(--muted);
    font-family: var(--font-display-mono);
    font-size: 0.65rem;
    letter-spacing: 0.1em;
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
    display: flex;
    align-items: center;
    gap: var(--space-sm);
    min-height: 2.6rem;
    padding: 0 var(--space-sm);
    border-radius: var(--radius-sm);
    color: var(--muted);
    font-size: 0.875rem;
    font-weight: 500;
    text-decoration: none;
    transition: background 0.15s ease, color 0.15s ease;
  }

  .admin__link:hover {
    background: var(--accent-soft);
    color: var(--ink);
  }

  .admin__link[aria-current="page"] {
    background: var(--accent-soft);
    color: var(--ink);
    box-shadow: inset 2px 0 0 var(--accent);
  }

  .admin__link :global(.admin__icon) {
    flex-shrink: 0;
    opacity: 0.8;
  }

  .admin__room {
    margin-top: auto;
    padding: var(--space-md) var(--space-sm) var(--space-xs);
    border-top: 1px solid var(--border);
  }

  .admin__facts {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    margin: 0;
  }

  .admin__facts dt {
    color: var(--muted);
    font-size: 0.75rem;
  }

  .admin__facts dd {
    margin: 0.1rem 0 0;
  }

  .admin__facts code {
    display: block;
    overflow: hidden;
    color: var(--ink);
    font-family: var(--font-display-mono);
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

  /* The open page's own card grows to fill the column. */
  .admin__content > :global(.panel:first-child) {
    flex: 1;
  }

  @media (max-width: 52rem) {
    .admin__layout {
      grid-template-columns: 1fr;
    }

    .admin__nav {
      position: static;
    }

    .admin__list {
      flex-direction: row;
      flex-wrap: wrap;
    }

    .admin__room {
      display: none;
    }
  }
</style>

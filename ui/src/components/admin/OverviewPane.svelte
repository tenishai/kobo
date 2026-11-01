<script lang="ts">
  // One row per page of the panel, each a link, each with the one number or
  // sentence an admin wants before opening it. Everything here is read from
  // the room; nothing is a tile that leads nowhere.
  import { createQuery } from "@tanstack/svelte-query";
  import {
    CaretRight,
    ClockCounterClockwise,
    Graph,
    Plugs,
    SealCheck,
    Stack,
    UserGear,
    Users,
  } from "phosphor-svelte";
  import { formatHash } from "../../lib/router.svelte";
  import {
    accessLeasesQuery,
    accessRequestsQuery,
    accessSummaryQuery,
    adminAddonsQuery,
    adminConnectionsQuery,
    adminIntegrationsQuery,
    adminRolesQuery,
    groupRequestsQuery,
  } from "../../lib/queries";

  const addons = createQuery(() => (adminAddonsQuery()));
  const integrations = createQuery(() => (adminIntegrationsQuery()));
  const connections = createQuery(() => (adminConnectionsQuery()));
  const roles = createQuery(() => (adminRolesQuery()));
  const requests = createQuery(() => (groupRequestsQuery(true)));
  // 404 when Access is off for the room; the row then says so instead of a number.
  const access = createQuery(() => (accessRequestsQuery("pending")));
  const leases = createQuery(() => (accessLeasesQuery()));
  // The permission service decides two things here: whether Connections is
  // the lanes page or Agent connections, and whether the Agent connections
  // card has a figure to show. Its count is the room's cheap one
  // (`/api/admin/access/summary`), read only while Access is on.
  const accessOn = $derived((addons.data ?? []).some((x) => x.id === "access" && x.state === "on"));
  const summary = createQuery(() => ({ ...accessSummaryQuery(), enabled: accessOn }));

  function plural(n: number, one: string, many: string = one + "s"): string {
    return `${n} ${n === 1 ? one : many}`;
  }

  type About = { text: string; icon: typeof Stack };
  type Row = { pane: string; name: string; note: string; warn?: boolean; about?: About };

  // What each page is for, and its icon: the card's second line, so an
  // admin who has not opened the page yet knows why they would.
  const ABOUT: Record<string, About> = {
    services: { text: "What the room runs on, and which add-ons are on.", icon: Stack },
    integrations: { text: "The vendors the room can lend agents access to.", icon: Plugs },
    connections: { text: "Which agent reaches which vendor through the gateway.", icon: Graph },
    // The same page id as `connections` when the permission service is on (#371).
    agentConnections: { text: "Which agents reach which vendors, and what they hold right now.", icon: Graph },
    review: { text: "Permission requests waiting for a decision.", icon: SealCheck },
    roles: { text: "What each role may do in the room.", icon: UserGear },
    people: { text: "Who runs the room, and group requests.", icon: Users },
    audit: { text: "Who changed what, and whether it landed.", icon: ClockCounterClockwise },
  };
  const rows = $derived.by((): Row[] => {
    const a = addons.data ?? [];
    const on = a.filter((x) => x.state === "on").length;
    const broken = a.filter((x) => x.state === "error").length;
    const i = integrations.data ?? [];
    const failed = i.filter((x) => x.state === "failed").length;
    const agents = connections.data?.agents ?? [];
    const noLane = agents.filter((x) => x.lanes.length === 0).length;
    const r = roles.data?.roles ?? [];
    const pending = (requests.data ?? []).filter((x) => x.status === "pending").length;
    const waiting = (access.data ?? []).length;
    const out = (leases.data ?? []).length;
    const holding = summary.data?.agents_with_access ?? 0;
    return [
      {
        pane: "services",
        name: "Services",
        note: addons.isPending ? "…" : broken ? `${broken} not answering` : `${on} of ${a.length} on`,
        warn: broken > 0,
      },
      {
        pane: "integrations",
        name: "Integrations",
        note: integrations.isPending ? "…" : failed ? `${plural(failed, "connection")} failed` : i.length ? `${plural(i.length, "vendor")} connected` : "none yet",
        warn: failed > 0,
      },
      // Agent connections, with the permission service on: how many agents
      // hold access anywhere right now.
      ...(accessOn ? [{
        pane: "connections",
        name: "Agent connections",
        note: summary.isPending ? "…" : `${plural(holding, "agent")} ${holding === 1 ? "holds" : "hold"} access`,
        about: ABOUT.agentConnections,
      }] : []),
      // Connections exists only while the permission service is off; until the
      // add-ons have answered, nobody knows, so the row waits rather than flash
      // and vanish (the nav got the same rule in #315).
      ...(addons.isPending || accessOn ? [] : [{
        pane: "connections",
        name: "Connections",
        note: connections.isPending ? "…" : noLane ? `${plural(agents.length, "agent")}, ${noLane} without a lane` : `${plural(agents.length, "agent")}, all with a lane`,
      }]),
      {
        pane: "review",
        name: "Review",
        note: access.isPending ? "…" : access.isError ? "access is off for this room" : `${waiting ? plural(waiting, "request") + " waiting" : "nothing waiting"}${out ? `, ${plural(out, "credential")} out now` : ""}`,
        warn: waiting > 0,
      },
      { pane: "roles", name: "Roles", note: roles.isPending ? "…" : plural(r.length, "role") },
      {
        pane: "people",
        name: "People",
        note: requests.isPending ? "…" : pending ? `${plural(pending, "group request")} waiting` : "nothing waiting",
        warn: pending > 0,
      },
      { pane: "audit", name: "Audit", note: "Open the log" },
    ];
  });
</script>

<section class="panel page overview" aria-labelledby="overview-heading">
  <header class="overview__head">
    <h2 id="overview-heading" class="page__title">Overview</h2>
    <p class="page__lead">Where things stand. Open a page for the details.</p>
  </header>
  <div class="overview__grid">
    {#each rows as r (r.pane)}
      {@const about = r.about ?? ABOUT[r.pane]}
      <a class="overview__card" href={formatHash({ page: "admin", pane: r.pane })}>
        <span class="overview__top">
          <span class="overview__icon" aria-hidden="true">
            {#if about}<about.icon size={18} />{/if}
          </span>
          <CaretRight class="overview__chevron" size={14} aria-hidden="true" />
        </span>
        <span class="overview__name">{r.name}</span>
        <span class="overview__note" class:overview__note--warn={r.warn}>{r.note}</span>
        {#if about}<span class="overview__about">{about.text}</span>{/if}
      </a>
    {/each}
  </div>
</section>

<style>
  .overview {
    gap: var(--space-lg);
  }

  .overview__head {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .overview__grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(16rem, 1fr));
    gap: var(--space-md);
  }

  .overview__card {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
    color: var(--ink);
    text-decoration: none;
    transition: border-color 0.15s ease, box-shadow 0.15s ease, transform 0.15s ease;
  }

  .overview__card:hover {
    border-color: var(--ring);
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.06);
    transform: translateY(-1px);
  }

  @media (prefers-reduced-motion: reduce) {
    .overview__card:hover {
      transform: none;
    }
  }

  .overview__top {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    margin-bottom: var(--space-sm);
  }

  .overview__icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 2.25rem;
    height: 2.25rem;
    border-radius: var(--radius-sm);
    background: var(--accent-soft);
    color: var(--ink);
  }

  .overview__top :global(.overview__chevron) {
    color: var(--muted);
  }

  .overview__name {
    color: var(--muted);
    font-size: 0.82rem;
    font-weight: 500;
  }

  .overview__note {
    display: block;
    color: var(--ink);
    font-size: 1.05rem;
    font-weight: 600;
    letter-spacing: -0.01em;
  }

  .overview__note::first-letter {
    text-transform: uppercase;
  }

  .overview__note--warn {
    color: var(--warn);
  }

  .overview__about {
    margin-top: var(--space-xs);
    color: var(--muted);
    font-size: 0.8rem;
    line-height: 1.5;
  }
</style>

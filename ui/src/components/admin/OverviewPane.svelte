<script lang="ts">
  // One row per page of the panel, each a link, each with the one number or
  // sentence an admin wants before opening it. Everything here is read from
  // the room; nothing is a tile that leads nowhere.
  import { createQuery } from "@tanstack/svelte-query";
  import { formatHash } from "../../lib/router.svelte";
  import {
    accessLeasesQuery,
    accessRequestsQuery,
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

  function plural(n: number, one: string, many: string = one + "s"): string {
    return `${n} ${n === 1 ? one : many}`;
  }

  type Row = { pane: string; name: string; note: string; warn?: boolean };
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
    const accessOn = a.some((x) => x.id === "access" && x.state === "on");
    const out = (leases.data ?? []).length;
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
      { pane: "audit", name: "Audit", note: "who changed what, and whether it landed" },
    ];
  });
</script>

<section class="panel page" aria-labelledby="overview-heading">
  <h2 id="overview-heading" class="page__title">Overview</h2>
  <p class="page__lead">Where things stand. Open a page for the details.</p>
  <div class="table-card">
    {#each rows as r (r.pane)}
      <a class="row-link" href={formatHash({ page: "admin", pane: r.pane })}>
        <span class="row-link__name">{r.name}</span>
        <span class="row-link__note" class:row-link__note--warn={r.warn}>{r.note}</span>
        <span class="row-link__chevron" aria-hidden="true">›</span>
      </a>
    {/each}
  </div>
</section>

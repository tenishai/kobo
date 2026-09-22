<script lang="ts">
  // The app shell: one QueryClient for the whole tree, the Sidebar
  // (wordmark, nav, Create button), the hash-route switch, and the toast
  // stack. Everything that actually fetches lives further down (Dashboard,
  // AgentDetail); this file just wires the plumbing around them.
  import { QueryClient, QueryClientProvider } from "@tanstack/svelte-query";
  import ConnectModal from "./components/ConnectModal.svelte";
  import CreateModal from "./components/CreateModal.svelte";
  import Sidebar from "./components/Sidebar.svelte";
  import TeamGuideModal from "./components/TeamGuideModal.svelte";
  import Toasts from "./components/Toasts.svelte";
  import AgentDetail from "./pages/AgentDetail.svelte";
  import Dashboard from "./pages/Dashboard.svelte";
  import GettingStarted from "./pages/GettingStarted.svelte";
  import Personas from "./pages/Personas.svelte";
  import {
    groupsQuery,
    modelsQuery,
    orgStatusQuery,
    personasSearchQuery,
    shouldRetry,
    skillsSearchQuery,
    teamsQuery,
  } from "./lib/queries";
  import { route } from "./lib/router.svelte";

  // `shouldRetry` (queries.ts) skips retries for 4xx responses so a 404
  // doesn't burn TanStack's default 3 retries (~7s of backoff) before
  // rendering — this is the one seam all queries in the tree inherit from.
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: shouldRetry,
      },
    },
  });

  // The "warm modal" trick: the catalogs (models, teams, the unfiltered
  // persona/skill searches, org wiring status) are only needed once the
  // Create modal opens, so they're not fetched on page load — only
  // prefetched the moment the user shows intent to create (hovering or
  // focusing the button), so the modal has data the instant it actually
  // opens. Every one of these is exactly what CreateModal's own queries
  // ask for on a fresh, unfiltered open (empty search text, no team
  // filter), so a warm cache means the modal renders instantly instead of
  // a page of skeletons. `groupsQuery` (access-control Task B2) joins for
  // the identical reason: the Create station's group picker reads it on
  // every open. `teamOptionsQuery` (`/api/teams`) used to join this set for
  // the Create station's own team picker (persona-metadata plan); the
  // "team vs group" ruling retired that picker, so this file no longer
  // warms it at all — Edit details (`MetadataDetails`), its only remaining
  // reader, fetches it lazily on its own `open` instead, same as before
  // that picker ever existed.
  function prefetchCatalogs(): void {
    void queryClient.prefetchQuery(modelsQuery());
    void queryClient.prefetchQuery(groupsQuery());
    void queryClient.prefetchQuery(teamsQuery());
    void queryClient.prefetchQuery(orgStatusQuery());
    void queryClient.prefetchQuery(personasSearchQuery("", null));
    void queryClient.prefetchQuery(skillsSearchQuery("", null));
  }

  // `createOpen` is fully controlled here: the Sidebar's Create button sets
  // it true, CreateModal's own onclose (Escape, backdrop click, Cancel, or a
  // successful submit) sets it back to false.
  let createOpen = $state(false);

  function onOpenCreate(): void {
    createOpen = true;
  }

  function onCloseCreate(): void {
    createOpen = false;
  }

  // Team-guide spec 2026-09-14: same fully-controlled contract as
  // `createOpen` above, just for the Sidebar's second, secondary button.
  let guideOpen = $state(false);

  function onOpenGuide(): void {
    guideOpen = true;
  }

  function onCloseGuide(): void {
    guideOpen = false;
  }

  // Connected-agents design 2026-09-21: the third button's own state, same
  // fully-controlled contract as the two above.
  let connectOpen = $state(false);

  function onOpenConnect(): void {
    connectOpen = true;
  }

  function onCloseConnect(): void {
    connectOpen = false;
  }
</script>

<QueryClientProvider client={queryClient}>
  <div class="shell">
    <Sidebar
      active={route.page}
      oncreate={onOpenCreate}
      onopenguide={onOpenGuide}
      onopenconnect={onOpenConnect}
      onprefetch={prefetchCatalogs}
    />

    <main class="shell__main">
      {#if route.page === "dashboard"}
        <Dashboard />
      {:else if route.page === "getting-started"}
        <GettingStarted />
      {:else if route.page === "personas"}
        <Personas />
      {:else}
        <!-- Keyed by name: a direct hash edit from one agent's detail page
             to another's (no trip back through the dashboard in between)
             remounts AgentDetail instead of reusing a stale query. -->
        {#key route.name}
          <AgentDetail />
        {/key}
      {/if}
    </main>
  </div>

  <CreateModal open={createOpen} onclose={onCloseCreate} />
  <TeamGuideModal open={guideOpen} onclose={onCloseGuide} />
  <ConnectModal open={connectOpen} onclose={onCloseConnect} />
</QueryClientProvider>

<Toasts />

<style>
  .shell {
    display: flex;
    flex-direction: row;
    gap: var(--space-xl);
    padding: 0;
    min-height: 100vh;
  }

  .shell__main {
    flex: 1;
    padding: var(--space-xl);
    min-width: 0;
  }
</style>

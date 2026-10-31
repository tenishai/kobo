<script lang="ts">
  // The app shell: one QueryClient for the whole tree, the Sidebar
  // (wordmark, nav, Create button), the hash-route switch, and the toast
  // stack. Everything that actually fetches lives further down (Dashboard,
  // AgentDetail); this file just wires the plumbing around them.
  import { QueryClient, QueryClientProvider } from "@tanstack/svelte-query";
  import ConnectModal from "./components/ConnectModal.svelte";
  import CreateModal from "./components/CreateModal.svelte";
  import NewAgentModal from "./components/NewAgentModal.svelte";
  import type { NewAgentChoice } from "./components/NewAgentModal.svelte";
  import Sidebar from "./components/Sidebar.svelte";
  import Toasts from "./components/Toasts.svelte";
  import AdminPanel from "./pages/AdminPanel.svelte";
  import AgentDetail from "./pages/AgentDetail.svelte";
  import Dashboard from "./pages/Dashboard.svelte";
  import GettingStarted from "./pages/GettingStarted.svelte";
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

  // Every dialog below is fully controlled from here: a flag goes true when
  // something opens it, and the dialog's own onclose (Escape, backdrop
  // click, Cancel, or a successful submit) puts it back to false.
  //
  // New-agent entry wave (2026-09-23): the rail's three buttons became one,
  // so the flags are now set by the chooser rather than by three separate
  // rail handlers. `onOpenCreate` is kept as its own named function because
  // it is the seam anything else that wants the build station directly
  // (rather than by way of the chooser) goes through.
  let chooserOpen = $state(false);
  let createOpen = $state(false);
  let connectOpen = $state(false);

  function onOpenChooser(): void {
    chooserOpen = true;
  }

  function onCloseChooser(): void {
    chooserOpen = false;
  }

  function onOpenCreate(): void {
    createOpen = true;
  }

  function onCloseCreate(): void {
    createOpen = false;
  }

  function onCloseConnect(): void {
    connectOpen = false;
  }

  // Choosing is the whole of the chooser's job: it closes, and the station
  // it names opens in its place. Closing that station lands on the
  // dashboard rather than back here -- a chooser that reappears behind
  // every cancel is a trap, not a step back.
  function onChoose(choice: NewAgentChoice): void {
    chooserOpen = false;
    if (choice === "build") {
      onOpenCreate();
    } else {
      connectOpen = true;
    }
  }
</script>

<QueryClientProvider client={queryClient}>
  <div class="shell">
    <Sidebar
      active={route.page}
      onnewagent={onOpenChooser}
      onprefetch={prefetchCatalogs}
    />

    <main class="shell__main">
      {#if route.page === "dashboard"}
        <Dashboard />
      {:else if route.page === "getting-started"}
        <GettingStarted />
      {:else if route.page === "admin"}
        <AdminPanel />
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

  <NewAgentModal open={chooserOpen} onclose={onCloseChooser} onchoose={onChoose} />
  <CreateModal open={createOpen} onclose={onCloseCreate} />
  <ConnectModal open={connectOpen} onclose={onCloseConnect} />
</QueryClientProvider>

<Toasts />

<style>
  /* White page; the dark rail floats on the left, and the main content sits
     in a light grey frame beside it, so the white cards inside float. */
  .shell {
    display: flex;
    flex-direction: row;
    align-items: flex-start;
    gap: var(--space-sm);
    padding: var(--space-sm);
    min-height: 100vh;
    background: var(--page);
  }

  .shell__main {
    flex: 1;
    min-width: 0;
    min-height: calc(100vh - 2 * var(--space-sm));
    padding: var(--space-xl);
    border-radius: var(--radius);
    background: var(--bg);
  }
</style>

<script lang="ts">
  // The app shell: one QueryClient for the whole tree, the header
  // (wordmark + Create button), the hash-route switch, and the toast
  // stack. Everything that actually fetches lives further down (Dashboard,
  // AgentDetail); this file just wires the plumbing around them.
  import { QueryClient, QueryClientProvider } from "@tanstack/svelte-query";
  import CreateModal from "./components/CreateModal.svelte";
  import Toasts from "./components/Toasts.svelte";
  import AgentDetail from "./pages/AgentDetail.svelte";
  import Dashboard from "./pages/Dashboard.svelte";
  import { catalogsQuery, shouldRetry } from "./lib/queries";
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

  // The "warm modal" trick: catalogs (personas/skills/models) are only
  // needed once the Create modal opens, so they're not fetched on page
  // load — only prefetched the moment the user shows intent to create
  // (hovering or focusing the button), so the modal has data the instant
  // it actually opens.
  function prefetchCatalogs(): void {
    void queryClient.prefetchQuery(catalogsQuery());
  }

  // `createOpen` is fully controlled here: the header button sets it true,
  // CreateModal's own onclose (Escape, backdrop click, Cancel, or a
  // successful submit) sets it back to false.
  let createOpen = $state(false);

  function onOpenCreate(): void {
    createOpen = true;
  }

  function onCloseCreate(): void {
    createOpen = false;
  }
</script>

<QueryClientProvider client={queryClient}>
  <div class="shell">
    <header class="shell__header">
      <h1 class="shell__wordmark">
        cortex <span class="shell__tagline">— assembly room</span>
      </h1>
      <button
        type="button"
        class="shell__create"
        onmouseenter={prefetchCatalogs}
        onfocus={prefetchCatalogs}
        onclick={onOpenCreate}
      >
        Create agent
      </button>
    </header>

    <main class="shell__main">
      {#if route.page === "dashboard"}
        <Dashboard />
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
</QueryClientProvider>

<Toasts />

<style>
  .shell {
    display: flex;
    min-height: 100vh;
    flex-direction: column;
    gap: var(--space-xl);
    padding: var(--space-xl);
  }

  .shell__header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-md);
  }

  .shell__wordmark {
    margin: 0;
    color: var(--ink);
    font-weight: 600;
  }

  .shell__tagline {
    color: var(--muted);
    font-weight: 400;
  }

  .shell__create {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }

  .shell__create:hover {
    filter: brightness(1.05);
  }

  .shell__create:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }

  .shell__main {
    flex: 1;
  }
</style>

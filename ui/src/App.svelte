<script lang="ts">
  // The app shell: one QueryClient for the whole tree, the header
  // (wordmark + Create button), the hash-route switch, and the toast
  // stack. Everything that actually fetches lives further down (Dashboard,
  // and — from Task 6 — the agent detail page); this file just wires the
  // plumbing around them.
  import { QueryClient, QueryClientProvider } from "@tanstack/svelte-query";
  import CreateModal from "./components/CreateModal.svelte";
  import Toasts from "./components/Toasts.svelte";
  import Dashboard from "./pages/Dashboard.svelte";
  import { catalogsQuery } from "./lib/queries";
  import { route } from "./lib/router.svelte";

  const queryClient = new QueryClient();

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
        <!-- Task 6 replaces this with the real agent detail page. -->
        <p class="muted">Detail page for "{route.name}" is on its way.</p>
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

  .muted {
    color: var(--muted);
  }
</style>

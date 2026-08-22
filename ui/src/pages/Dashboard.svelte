<script lang="ts">
  // The agent list. `agentsQuery` already carries its own adaptive polling
  // (queries.ts / status.ts) — while any agent is non-terminal this
  // component's data just keeps arriving on its own, with no manual refresh
  // wired up anywhere. This page only owns the four states that query can
  // be in: loading, error (with retry), empty, and populated. The header
  // (wordmark + Create button) lives one level up, in App.svelte.
  //
  // Access-control Task B2: the response is already server-filtered to
  // what THIS caller may see (`_visible_agents`, kobo_api.main) — this page
  // just renders whatever comes back, no client-side filtering of its own.
  // `groups_unavailable` (true only when a directory outage narrowed the
  // list down to owned-only) renders the Global Constraints' banner copy
  // verbatim, above the grid rather than replacing it — a degraded list is
  // still a real list, not an error state.
  import { createQuery } from "@tanstack/svelte-query";
  import AgentCard from "../components/AgentCard.svelte";
  import EmptyState from "../components/EmptyState.svelte";
  import ErrorState from "../components/ErrorState.svelte";
  import Skeleton from "../components/Skeleton.svelte";
  import { errorMessage } from "../lib/api";
  import { agentsQuery } from "../lib/queries";

  const agents = createQuery(agentsQuery());
</script>

{#if $agents.isPending}
  <div class="dashboard-grid">
    {#each { length: 3 } as _, i (i)}
      <!-- Only the first carries the accessible "Loading…" announcement;
           the rest are shape-only placeholders (aria-hidden) so a screen
           reader doesn't hear "Loading" three times over. -->
      <div class="skeleton-card" aria-hidden={i > 0}>
        <Skeleton rows={3} />
      </div>
    {/each}
  </div>
{:else if $agents.isError}
  <ErrorState
    message={errorMessage($agents.error, "Couldn't load agents.")}
    onretry={() => $agents.refetch()}
  />
{:else}
  {#if $agents.data.groups_unavailable}
    <p class="groups-unavailable-banner" role="status">
      Group lookups are unavailable right now. You are seeing only what you
      own.
    </p>
  {/if}
  {#if $agents.data.agents.length === 0}
    <EmptyState message="No agents yet. Create your first." />
  {:else}
    <div class="dashboard-grid">
      {#each $agents.data.agents as agent (agent.name)}
        <AgentCard {agent} />
      {/each}
    </div>
  {/if}
{/if}

<style>
  .dashboard-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(16rem, 1fr));
    gap: var(--space-md);
  }

  .skeleton-card {
    padding: var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .groups-unavailable-banner {
    margin: 0 0 var(--space-md) 0;
    padding: var(--space-sm) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--muted);
    font-size: 0.85rem;
  }
</style>

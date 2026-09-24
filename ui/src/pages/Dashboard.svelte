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
  import GroupRequestsCard from "../components/GroupRequestsCard.svelte";
  import Skeleton from "../components/Skeleton.svelte";
  import { errorMessage } from "../lib/api";
  import { CONTROLS_OVER, agentGroups, filterAgents } from "../lib/agents";
  import { agentsQuery } from "../lib/queries";

  const agents = createQuery(() => (agentsQuery()));

  // Card wave 2026-09-23: what a person typed and what they picked. Local
  // state and nothing else -- no URL, no storage. A dashboard that
  // remembered a filter would hide agents from the next visit and never say
  // why. Both reset the moment the page is left.
  let query = $state("");
  let group = $state("");

  let all = $derived(agents.data?.agents ?? []);
  // Six cards fit on a screen; the seventh is when looking becomes
  // searching. Below that the controls are not hidden, they do not exist.
  let controls = $derived(all.length > CONTROLS_OVER);
  let groups = $derived(agentGroups(all));
  let shown = $derived(controls ? filterAgents(all, query, group) : all);
</script>

<!-- Shared-group + group-requests wave: the least-invasive existing home
     for the admin queue card. `GroupRequestsCard` renders nothing itself
     unless whoami's own `pending_group_requests` is present and positive
     (see its own doc comment) — always mounted, the card decides its own
     visibility, same "server decides, UI reflects" discipline the
     groups_unavailable banner below already follows. -->
<GroupRequestsCard />

{#if agents.isPending}
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
{:else if agents.isError}
  <ErrorState
    message={errorMessage(agents.error, "Couldn't load agents.")}
    onretry={() => agents.refetch()}
  />
{:else}
  {#if agents.data.groups_unavailable}
    <p class="groups-unavailable-banner" role="status">
      Group lookups are unavailable right now. You are seeing only what you
      own.
    </p>
  {/if}
  {#if all.length === 0}
    <EmptyState message="No agents yet. Create your first." />
  {:else}
    {#if controls}
      <div class="dashboard-controls">
        <label class="dashboard-controls__field">
          <span class="dashboard-controls__label">Search agents</span>
          <input
            class="dashboard-controls__input"
            type="search"
            placeholder="Name, group, what it does"
            bind:value={query}
          />
        </label>
        <label class="dashboard-controls__field">
          <span class="dashboard-controls__label">Group</span>
          <select class="dashboard-controls__input" bind:value={group}>
            <option value="">All groups</option>
            {#each groups as name (name)}
              <option value={name}>{name}</option>
            {/each}
          </select>
        </label>
      </div>
    {/if}
    {#if shown.length === 0}
      <EmptyState message="No agents match that." />
    {:else}
      <div class="dashboard-grid">
        {#each shown as agent (agent.name)}
          <AgentCard {agent} />
        {/each}
      </div>
    {/if}
  {/if}
{/if}

<style>
  /* Cards in one row share a height (grid stretch does that on its own);
     rows are not forced to match each other, so one wordy agent does not
     stretch every card on the page. Columns are wide enough for a name,
     a status and a one-line description without clipping the name. */
  .dashboard-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(19rem, 1fr));
    gap: var(--space-md);
  }

  .dashboard-controls {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: var(--space-sm);
    margin-bottom: var(--space-md);
  }

  .dashboard-controls__field {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .dashboard-controls__label {
    color: var(--muted);
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .dashboard-controls__input {
    min-width: 14rem;
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
    font-size: 0.9rem;
  }

  .dashboard-controls select.dashboard-controls__input {
    min-width: 10rem;
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

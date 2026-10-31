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
  import { CaretDown, MagnifyingGlass } from "phosphor-svelte";
  import AgentCard from "../components/AgentCard.svelte";
  import EmptyState from "../components/EmptyState.svelte";
  import ErrorState from "../components/ErrorState.svelte";
  import GroupRequestsCard from "../components/GroupRequestsCard.svelte";
  import Skeleton from "../components/Skeleton.svelte";
  import { errorMessage } from "../lib/api";
  import { CONTROLS_OVER, STATUS_FILTERS, agentGroups, filterAgents } from "../lib/agents";
  import type { StatusFilter } from "../lib/agents";
  import { agentsQuery } from "../lib/queries";

  const agents = createQuery(() => (agentsQuery()));

  // Card wave 2026-09-23: what a person typed and what they picked. Local
  // state and nothing else -- no URL, no storage. A dashboard that
  // remembered a filter would hide agents from the next visit and never say
  // why. Both reset the moment the page is left.
  let query = $state("");
  let group = $state("");
  let status = $state<StatusFilter>("");

  let all = $derived(agents.data?.agents ?? []);
  // Six cards fit on a screen; the seventh is when looking becomes
  // searching. Below that the controls are not hidden, they do not exist.
  let controls = $derived(all.length > CONTROLS_OVER);
  let groups = $derived(agentGroups(all));
  let shown = $derived(controls ? filterAgents(all, query, group, status) : all);
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
      <!-- One row, no visible labels: each picker's first option already
           says what it filters ("All groups", "All statuses"), and the
           search box carries its icon. The labels stay for screen readers. -->
      <div class="dashboard-controls">
        <div class="dashboard-controls__select">
          <select class="dashboard-controls__input" aria-label="Group" bind:value={group}>
            <option value="">All groups</option>
            {#each groups as name (name)}
              <option value={name}>{name}</option>
            {/each}
          </select>
          <CaretDown class="dashboard-controls__caret" size={14} aria-hidden="true" />
        </div>
        <div class="dashboard-controls__select">
          <select class="dashboard-controls__input" aria-label="Status" bind:value={status}>
            <option value="">All statuses</option>
            {#each STATUS_FILTERS as choice (choice.value)}
              <option value={choice.value}>{choice.label}</option>
            {/each}
          </select>
          <CaretDown class="dashboard-controls__caret" size={14} aria-hidden="true" />
        </div>
        <div class="dashboard-controls__search">
          <MagnifyingGlass class="dashboard-controls__glass" size={16} aria-hidden="true" />
          <input
            class="dashboard-controls__input"
            type="search"
            aria-label="Search agents"
            placeholder="Search"
            bind:value={query}
          />
        </div>
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

  /* Top right, above the grid: filters first, search last, one row. */
  .dashboard-controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: var(--space-xs);
    margin-bottom: var(--space-md);
  }

  .dashboard-controls__select,
  .dashboard-controls__search {
    position: relative;
    display: flex;
    align-items: center;
  }

  .dashboard-controls__input {
    box-sizing: border-box;
    height: 2.25rem;
    padding: 0 var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
    /* Geist Mono, the same face as the agent names on the cards below. */
    font-family: var(--font-display-mono);
    font-size: 0.8rem;
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
  }

  .dashboard-controls__input:hover {
    border-color: var(--ring);
  }

  .dashboard-controls__input:focus-visible {
    border-color: var(--ring);
    outline: none;
    box-shadow: 0 0 0 3px color-mix(in oklab, var(--ring) 30%, transparent);
  }

  /* The selects draw their own caret, so they match the room's controls
     instead of the operating system's. */
  .dashboard-controls select.dashboard-controls__input {
    min-width: 9.5rem;
    padding-right: 2rem;
    appearance: none;
    cursor: pointer;
  }

  .dashboard-controls__select :global(.dashboard-controls__caret) {
    position: absolute;
    right: 0.7rem;
    color: var(--muted);
    pointer-events: none;
  }

  .dashboard-controls__search input.dashboard-controls__input {
    width: 20rem;
    padding-left: 2.1rem;
  }

  .dashboard-controls__search input.dashboard-controls__input::placeholder {
    color: var(--muted);
  }

  .dashboard-controls__search :global(.dashboard-controls__glass) {
    position: absolute;
    left: 0.7rem;
    color: var(--muted);
    pointer-events: none;
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

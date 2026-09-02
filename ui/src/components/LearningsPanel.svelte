<script lang="ts">
  // Daily-learnings spec: each night the agent proposes candidate memory
  // snippets from its own conversation journal (`Learning`, api.ts). Nothing
  // is saved until a human approves it here -- approve teaches it to the
  // agent permanently, deny makes sure the same snippet is never proposed
  // again. Mirrors MemoryPanel's own conventions: thunk-wrapped
  // createQuery/createMutation, BEM-scoped CSS below.
  //
  // Every panel mounts unconditionally inside its own tabpanel regardless of
  // which tab is active (AgentDetail's own tab invariant), so this query
  // fires as soon as the agent's detail page loads, not just when the
  // Learnings tab is selected. `canDecide` is the server's own
  // `can_decide_learnings` gate (AgentDetail passes it straight through) --
  // this panel hides the verdict buttons entirely when it's false, never
  // just disables them, since the route is the real enforcement and a
  // visible-but-dead button would only invite a 403 for nothing. `frozen`
  // (destroying/destroy_failed) disables the buttons instead, same as every
  // other panel's own `frozen` prop.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import ErrorState from "./ErrorState.svelte";
  import { errorMessage } from "../lib/api";
  import {
    decideLearningMutation,
    decideLearningsBulkMutation,
    learningsQuery,
  } from "../lib/queries";
  import type { Learning } from "../lib/queries";

  let {
    agentName,
    canDecide = false,
    frozen = false,
  }: { agentName: string; canDecide?: boolean; frozen?: boolean } = $props();

  const queryClient = useQueryClient();
  // `agentName` is read once here rather than tracked reactively: same
  // reasoning as MemoryPanel's own `agentName` -- AgentDetail (the only
  // caller) remounts this component via `{#key route.name}` whenever the
  // routed agent changes, so this prop is fixed for the component's entire
  // lifetime.
  // svelte-ignore state_referenced_locally
  const learnings = createQuery(() => (learningsQuery(agentName)));
  // svelte-ignore state_referenced_locally
  const decide = createMutation(() => (decideLearningMutation(queryClient, agentName)));
  // svelte-ignore state_referenced_locally
  const bulkDecide = createMutation(() => (
    decideLearningsBulkMutation(queryClient, agentName)
  ));

  let showHistory = $state(false);
  let errorText = $state("");

  const all = $derived(learnings.data?.learnings ?? []);
  const pending = $derived(all.filter((l: Learning) => l.status === "pending"));
  const history = $derived(all.filter((l: Learning) => l.status !== "pending"));
  const days = $derived([...new Set(pending.map((l: Learning) => l.day))]);

  const busy = $derived(decide.isPending || bulkDecide.isPending);

  function decideOne(id: number, action: "approve" | "deny"): void {
    errorText = "";
    decide.mutate(
      { id, action },
      {
        onError: (e: unknown) =>
          (errorText = errorMessage(e, "Couldn't record that verdict. Try again.")),
      },
    );
  }

  function decideDay(day: string, action: "approve" | "deny"): void {
    errorText = "";
    const ids = pending
      .filter((l: Learning) => l.day === day)
      .map((l: Learning) => l.id);
    bulkDecide.mutate(
      { ids, action },
      {
        onError: (e: unknown) =>
          (errorText = errorMessage(e, "Couldn't record those verdicts. Try again.")),
      },
    );
  }
</script>

<section class="learnings-panel" aria-labelledby="learnings-panel-heading">
  <div class="learnings-panel__header">
    <h2 id="learnings-panel-heading" class="learnings-panel__heading">Learnings</h2>
  </div>

  <p class="learnings-panel__explainer">
    Each night your agent reviews its own conversations and proposes snippets
    worth remembering. Nothing is saved until you approve it: approve teaches
    it to the agent permanently, deny makes sure the same snippet is never
    proposed again.
  </p>

  {#if errorText}
    <p class="learnings-panel__error" role="alert">{errorText}</p>
  {/if}

  {#if learnings.isPending}
    <p class="learnings-panel__empty">Loading...</p>
  {:else if learnings.isError}
    <ErrorState
      message={errorMessage(learnings.error, "Couldn't load this agent's learnings.")}
      onretry={() => learnings.refetch()}
    />
  {:else if pending.length === 0}
    <p class="learnings-panel__empty">
      Nothing waiting for review. New candidates appear here after your
      agent's next nightly summary.
    </p>
  {:else}
    {#each days as day (day)}
      <div class="learnings-day">
        <div class="learnings-day__header">
          <h3 class="learnings-day__title">{day}</h3>
          {#if canDecide}
            <div class="learnings-day__bulk">
              <button
                type="button"
                class="learnings-panel__button"
                disabled={frozen || busy}
                onclick={() => decideDay(day, "approve")}
              >
                Approve all
              </button>
              <button
                type="button"
                class="learnings-panel__button learnings-panel__button--deny"
                disabled={frozen || busy}
                onclick={() => decideDay(day, "deny")}
              >
                Deny all
              </button>
            </div>
          {/if}
        </div>
        {#each pending.filter((l: Learning) => l.day === day) as learning (learning.id)}
          <article class="learning-card">
            <span class="learning-card__kind">{learning.kind}</span>
            <p class="learning-card__content">{learning.content}</p>
            <p class="learning-card__source">Source: {learning.source}</p>
            {#if learning.evidence}
              <p class="learning-card__evidence">&quot;{learning.evidence}&quot;</p>
            {/if}
            {#if canDecide}
              <div class="learning-card__actions">
                <button
                  type="button"
                  class="learnings-panel__button"
                  disabled={frozen || busy}
                  onclick={() => decideOne(learning.id, "approve")}
                >
                  Approve
                </button>
                <button
                  type="button"
                  class="learnings-panel__button learnings-panel__button--deny"
                  disabled={frozen || busy}
                  onclick={() => decideOne(learning.id, "deny")}
                >
                  Deny
                </button>
              </div>
            {/if}
          </article>
        {/each}
      </div>
    {/each}
  {/if}

  {#if history.length > 0}
    <button
      type="button"
      class="learnings-panel__history-toggle"
      aria-expanded={showHistory}
      onclick={() => (showHistory = !showHistory)}
    >
      History ({history.length})
    </button>
    {#if showHistory}
      {#each history as learning (learning.id)}
        <article class="learning-card learning-card--decided">
          <span class="learning-card__kind">{learning.status}</span>
          <p class="learning-card__content">{learning.content}</p>
          {#if learning.decided_by}
            <p class="learning-card__source">Decided by {learning.decided_by}</p>
          {/if}
        </article>
      {/each}
    {/if}
  {/if}
</section>

<style>
  .learnings-panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .learnings-panel__header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-md);
  }

  .learnings-panel__heading {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .learnings-panel__explainer {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .learnings-panel__error {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
  }

  .learnings-panel__empty {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .learnings-day {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
  }

  .learnings-day__header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
  }

  .learnings-day__title {
    margin: 0;
    color: var(--ink);
    font-size: 0.9rem;
    font-weight: 600;
  }

  .learnings-day__bulk {
    display: flex;
    gap: var(--space-xs);
  }

  .learning-card {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
  }

  .learning-card--decided {
    opacity: 0.7;
  }

  .learning-card__kind {
    display: inline-block;
    color: var(--muted);
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .learning-card__content {
    margin: 0.15rem 0;
    color: var(--ink);
  }

  .learning-card__source,
  .learning-card__evidence {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .learning-card__actions {
    display: flex;
    gap: var(--space-xs);
    margin-top: var(--space-2xs);
  }

  .learnings-panel__button {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }

  .learnings-panel__button:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }

  .learnings-panel__button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .learnings-panel__button--deny {
    color: var(--fail);
  }

  .learnings-panel__button--deny:hover:not(:disabled) {
    border-color: var(--fail);
    color: var(--fail);
  }

  .learnings-panel__history-toggle {
    align-self: flex-start;
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--muted);
    font-size: 0.85rem;
    font-weight: 600;
    cursor: pointer;
  }

  .learnings-panel__history-toggle:hover {
    border-color: var(--accent);
    color: var(--accent);
  }
</style>

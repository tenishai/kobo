<script lang="ts">
  // The room's watching window (Task 4/B4): every colleague conversation
  // the switchboard has recorded, live. Room-wide (not per-agent) but
  // mounted on the Chat tab under the existing ChatPanel per the
  // operator's ruling (AgentDetail.svelte) — this is the room hearing every
  // agent talk to a colleague, not a per-agent transcript.
  //
  // Renders nothing at all (silent absence) when the list query 409s — that
  // status means this room has no switchboard wired, which isn't an error
  // state worth a banner, just a feature that isn't on here. Any other
  // failure (a 502 upstream hiccup, etc.) still shows a normal error state
  // with retry.
  //
  // Live updates: any frame on `/api/colleagues/stream` (the switchboard's
  // named SSE frames - `hello`, `dispatch_start`, `dispatch_success`,
  // `dispatch_failure`; browsers only dispatch to `onmessage` for frames
  // with NO explicit `event:` field, so each name needs its own listener)
  // just refetches the list query - no client-side event merging, same
  // "simple and correct" call the brief makes explicitly. The connection
  // only opens once the list has loaded successfully at least once (proof
  // the switchboard is actually wired here), so a room with none never
  // opens a doomed EventSource that would otherwise retry forever against
  // a 409.
  import { createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { derived, writable } from "svelte/store";
  import { ApiError, errorMessage } from "../lib/api";
  import { colleagueConversationQuery, colleagueConversationsQuery } from "../lib/queries";
  import { relativeTime } from "../lib/time";
  import EmptyState from "./EmptyState.svelte";
  import ErrorState from "./ErrorState.svelte";

  const queryClient = useQueryClient();

  const showAll = writable(false);
  const conversationsOptions = derived(showAll, ($all) => colleagueConversationsQuery($all));
  const conversations = createQuery(conversationsOptions);

  const expandedTaskId = writable<string | null>(null);
  const detailOptions = derived(expandedTaskId, ($id) => colleagueConversationQuery($id));
  const detail = createQuery(detailOptions);

  const switchboardDisabled = $derived(
    $conversations.isError &&
      $conversations.error instanceof ApiError &&
      $conversations.error.status === 409,
  );

  function handleToggleAll(event: Event): void {
    showAll.set((event.currentTarget as HTMLInputElement).checked);
  }

  function handleExpand(taskId: string): void {
    expandedTaskId.set($expandedTaskId === taskId ? null : taskId);
  }

  /** Strips a caller's namespace prefix ("agent:iris" -> "iris",
   * "operator:kgreiner@example.com" -> "kgreiner@example.com") for display -
   * the prefix is what the agent-only filter reads, not something a human
   * needs to see in the row. */
  function callerLabel(caller: string): string {
    const idx = caller.indexOf(":");
    return idx === -1 ? caller : caller.slice(idx + 1);
  }

  const EVENT_NAMES = ["hello", "dispatch_start", "dispatch_success", "dispatch_failure"];

  $effect(() => {
    if ($conversations.data === undefined) {
      return;
    }
    const source = new EventSource("/api/colleagues/stream");
    const handleFrame = () => {
      queryClient.invalidateQueries({ queryKey: ["colleagues", "conversations", "list"] });
    };
    for (const name of EVENT_NAMES) {
      source.addEventListener(name, handleFrame);
    }
    return () => {
      source.close();
    };
  });
</script>

{#if !switchboardDisabled}
  <section class="colleague-conversations" aria-labelledby="colleague-conversations-heading">
    <div class="colleague-conversations__header">
      <h2 id="colleague-conversations-heading" class="colleague-conversations__heading">
        Colleague conversations
      </h2>
      <label class="colleague-conversations__toggle">
        <input
          type="checkbox"
          checked={$showAll}
          onchange={handleToggleAll}
        />
        Show everything
      </label>
    </div>

    {#if $conversations.isPending}
      <p class="colleague-conversations__status">Loading…</p>
    {:else if $conversations.isError}
      <ErrorState
        message={errorMessage(
          $conversations.error,
          "Couldn't load colleague conversations.",
        )}
        onretry={() => $conversations.refetch()}
      />
    {:else if $conversations.data.length === 0}
      <EmptyState
        message="No colleague conversations yet. They start talking once an agent asks for help."
      />
    {:else}
      <ul class="colleague-conversations__list">
        {#each $conversations.data as convo (convo.task_id)}
          <li class="colleague-conversations__row">
            <button
              type="button"
              class="colleague-conversations__row-toggle"
              aria-expanded={$expandedTaskId === convo.task_id}
              onclick={() => handleExpand(convo.task_id)}
            >
              <span class="colleague-conversations__caller">{callerLabel(convo.caller_agent)}</span>
              <span class="colleague-conversations__asks">asks</span>
              <span class="colleague-conversations__target">{convo.target_agent}</span>
              <span class="colleague-conversations__state-chip">{convo.state}</span>
              <span class="colleague-conversations__time">{relativeTime(convo.updated_at)}</span>
            </button>
            {#if convo.result_snippet}
              <p class="colleague-conversations__snippet">{convo.result_snippet}</p>
            {/if}
            {#if $expandedTaskId === convo.task_id}
              <div class="colleague-conversations__detail">
                {#if $detail.isPending}
                  <p class="colleague-conversations__status">Loading the exchange…</p>
                {:else if $detail.isError}
                  <p class="colleague-conversations__error" role="alert">
                    {errorMessage($detail.error, "Couldn't load this exchange.")}
                  </p>
                {:else if $detail.data}
                  <p class="colleague-conversations__result">
                    {$detail.data.conversation.result ?? convo.result_snippet ?? ""}
                  </p>
                  {#if $detail.data.events.length > 0}
                    <ul class="colleague-conversations__events">
                      {#each $detail.data.events as event (event.id)}
                        <li class="colleague-conversations__event">
                          <span class="colleague-conversations__event-type">{event.event_type}</span>
                          <span class="colleague-conversations__event-time">{relativeTime(event.created_at)}</span>
                        </li>
                      {/each}
                    </ul>
                  {/if}
                {/if}
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  </section>
{/if}

<style>
  .colleague-conversations {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .colleague-conversations__header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
  }

  .colleague-conversations__heading {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .colleague-conversations__toggle {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2xs);
    color: var(--muted);
    font-size: 0.85rem;
    cursor: pointer;
  }

  .colleague-conversations__status {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .colleague-conversations__list {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .colleague-conversations__row {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
  }

  .colleague-conversations__row-toggle {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-xs);
    padding: 0;
    border: none;
    background: none;
    color: var(--ink);
    font: inherit;
    text-align: left;
    cursor: pointer;
  }

  .colleague-conversations__caller,
  .colleague-conversations__target {
    font-weight: 600;
  }

  .colleague-conversations__asks {
    color: var(--muted);
    font-size: 0.85rem;
  }

  .colleague-conversations__state-chip {
    padding: var(--space-2xs) var(--space-xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.75rem;
    text-transform: capitalize;
  }

  .colleague-conversations__time {
    margin-left: auto;
    color: var(--muted);
    font-size: 0.75rem;
    white-space: nowrap;
  }

  .colleague-conversations__snippet {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .colleague-conversations__detail {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    padding-top: var(--space-xs);
    border-top: 1px solid var(--border);
  }

  .colleague-conversations__result {
    margin: 0;
    color: var(--ink);
    font-size: 0.9rem;
    white-space: pre-wrap;
  }

  .colleague-conversations__error {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
  }

  .colleague-conversations__events {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .colleague-conversations__event {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
    color: var(--muted);
    font-size: 0.75rem;
  }

  .colleague-conversations__event-type {
    font-family: var(--font-mono);
  }
</style>

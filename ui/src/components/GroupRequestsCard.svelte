<script lang="ts">
  // The admin surface for the group-requests queue (shared-group +
  // group-requests wave): a quiet "Group requests: N pending" card that
  // expands into the list (group name, requester, note, when) with Done /
  // Decline per row. Lives on the Dashboard (the least-invasive existing
  // home — there's no dedicated org-status/banner page in this room yet,
  // and Dashboard already renders one banner-shaped admin notice, the
  // groups_unavailable line).
  //
  // WATCH ITEM (see `Whoami`'s own doc comment): this renders purely off
  // `pending_group_requests` being PRESENT and > 0 — never off `operator`.
  // The server only ever populates that field for an admin in the first
  // place, so keying display off it is API-driven, not client-side gating;
  // a dev room with no configured operators still gets accurate accounting
  // (no key at all -> no card), same as every other screen in this room
  // that treats `operator` as a hint, never a gate.
  //
  // The list itself is fetched lazily, only once the card is expanded —
  // same "don't fetch until a human actually asks to see it" idiom
  // `slackManifestQuery`'s `opened` flag documents (SlackPanel.svelte).
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { derived, writable } from "svelte/store";
  import { errorMessage } from "../lib/api";
  import { decideGroupRequestMutation, groupRequestsQuery, whoamiQuery } from "../lib/queries";
  import { relativeTime } from "../lib/time";
  import EmptyState from "./EmptyState.svelte";
  import ErrorState from "./ErrorState.svelte";
  import Skeleton from "./Skeleton.svelte";

  const whoami = createQuery(whoamiQuery());
  const pendingCount = $derived($whoami.data?.pending_group_requests);
  const visible = $derived(pendingCount !== undefined && pendingCount > 0);

  const expanded = writable(false);
  const requestsOptions = derived(expanded, ($expanded) => groupRequestsQuery($expanded));
  const requests = createQuery(requestsOptions);

  function toggleExpanded(): void {
    expanded.update((value) => !value);
  }

  const queryClient = useQueryClient();
  const decide = createMutation(decideGroupRequestMutation(queryClient));
  let decideError = $state<string | null>(null);

  function handleDecide(id: number, action: "done" | "declined"): void {
    if ($decide.isPending) {
      return;
    }
    decideError = null;
    $decide.mutate(
      { id, action },
      {
        onError: (err: unknown) => {
          decideError = errorMessage(err, "Couldn't update that request. Try again.");
        },
      },
    );
  }
</script>

{#if visible}
  <div class="group-requests-card">
    <button
      type="button"
      class="group-requests-card__toggle"
      onclick={toggleExpanded}
      aria-expanded={$expanded}
    >
      Group requests: {pendingCount} pending
    </button>

    {#if $expanded}
      {#if $requests.isPending}
        <Skeleton rows={2} />
      {:else if $requests.isError}
        <ErrorState
          message={errorMessage($requests.error, "Couldn't load group requests.")}
          onretry={() => $requests.refetch()}
        />
      {:else if $requests.data.length === 0}
        <EmptyState message="No pending requests right now." />
      {:else}
        {#if decideError}
          <p class="group-requests-card__error" role="alert">{decideError}</p>
        {/if}
        <ul class="group-requests-card__list">
          {#each $requests.data as request (request.id)}
            <li class="group-requests-card__row">
              <div class="group-requests-card__info">
                <span class="group-requests-card__name">{request.group_name}</span>
                <span class="group-requests-card__meta">
                  {request.requested_by ?? "unknown"} - {relativeTime(request.created_at)}
                </span>
                {#if request.note}
                  <p class="group-requests-card__note">{request.note}</p>
                {/if}
              </div>
              <div class="group-requests-card__actions">
                <button
                  type="button"
                  class="group-requests-card__done"
                  disabled={$decide.isPending}
                  aria-label={`Mark ${request.group_name} done`}
                  onclick={() => handleDecide(request.id, "done")}
                >
                  Done
                </button>
                <button
                  type="button"
                  class="group-requests-card__decline"
                  disabled={$decide.isPending}
                  aria-label={`Decline ${request.group_name}`}
                  onclick={() => handleDecide(request.id, "declined")}
                >
                  Decline
                </button>
              </div>
            </li>
          {/each}
        </ul>
      {/if}
    {/if}
  </div>
{/if}

<style>
  .group-requests-card {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    margin: 0 0 var(--space-md) 0;
    padding: var(--space-sm) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
  }

  .group-requests-card__toggle {
    align-self: flex-start;
    padding: 0;
    border: none;
    background: none;
    color: var(--ink);
    font-weight: 600;
    font-size: 0.9rem;
    cursor: pointer;
  }

  .group-requests-card__toggle:hover {
    color: var(--accent);
  }

  .group-requests-card__list {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .group-requests-card__row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-xs);
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
  }

  .group-requests-card__info {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    min-width: 0;
  }

  .group-requests-card__name {
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.9rem;
    font-weight: 600;
  }

  .group-requests-card__meta {
    color: var(--muted);
    font-size: 0.8rem;
  }

  .group-requests-card__note {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .group-requests-card__actions {
    display: flex;
    gap: var(--space-xs);
  }

  .group-requests-card__done {
    padding: var(--space-2xs) var(--space-sm);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }

  .group-requests-card__done:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .group-requests-card__decline {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--muted);
    cursor: pointer;
    white-space: nowrap;
  }

  .group-requests-card__decline:hover:not(:disabled) {
    border-color: var(--fail);
    color: var(--fail);
  }

  .group-requests-card__done:disabled,
  .group-requests-card__decline:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .group-requests-card__error {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
  }
</style>
